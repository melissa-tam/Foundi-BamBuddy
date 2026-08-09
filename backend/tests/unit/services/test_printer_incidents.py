"""Unit tests for the durable AMS-incident store (WS2b).

The store is what replaced ``spool_recovery``'s process-lifetime dicts, so the
properties pinned here are the ones a restart used to destroy: ONE open incident
per printer (enforced by the database, not by a dict), an already-handled test that
survives a deploy, a flap cap counted from durable rows, and a projection cache the
~1 Hz WebSocket serializer can read without touching the DB.

The migration is exercised against a throwaway engine, twice, because
``run_migrations`` runs on every boot.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from backend.app.models.printer_incident import (
    KIND_JAM,
    KIND_RUNOUT,
    RESOLVE_OBSERVED_RUNNING,
    STATUS_ABORTED,
    STATUS_ESCALATED,
    STATUS_RECOVERING,
    STATUS_RESOLVED,
    PrinterIncident,
)
from backend.app.services import printer_incidents

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _reset():
    printer_incidents._reset_state()
    yield
    printer_incidents._reset_state()


async def _open(db, printer_id, **kw):
    kw.setdefault("job_id", "task-1")
    kw.setdefault("item_id", None)
    kw.setdefault("kind", KIND_RUNOUT)
    kw.setdefault("code", "0700_8011")
    kw.setdefault("codes", "runout:0700_8011")
    kw.setdefault("slot_global_tray", None)
    return await printer_incidents.open_new(db, printer_id=printer_id, **kw)


class TestOneOpenIncidentPerPrinter:
    async def test_a_second_open_incident_is_refused(self, db_session, printer_factory):
        printer = await printer_factory()
        first = await _open(db_session, printer.id)
        assert first is not None

        second = await _open(db_session, printer.id, kind=KIND_JAM, codes="jam:0700_8010")

        assert second is None  # the pre-check reported it; nothing was written
        rows = (await db_session.execute(text("SELECT COUNT(*) FROM printer_incident"))).scalar()
        assert rows == 1

    async def test_the_partial_index_refuses_a_bypassing_write(self, db_session, printer_factory):
        """The real enforcement: a caller that bypasses ``open_new`` dies loudly.

        A dict could be emptied by a restart; this cannot. The index is PARTIAL, so
        it constrains only rows with ``resolved_at IS NULL``."""
        from datetime import datetime

        printer = await printer_factory()
        await _open(db_session, printer.id)

        db_session.add(
            PrinterIncident(
                printer_id=printer.id,
                job_id="task-2",
                item_id=None,
                kind=KIND_JAM,
                code="0700_8010",
                codes="jam:0700_8010",
                status=STATUS_RECOVERING,
                created_at=datetime.utcnow(),
            )
        )
        with pytest.raises(IntegrityError):
            await db_session.commit()
        await db_session.rollback()

    async def test_closed_incidents_do_not_hold_the_slot(self, db_session, printer_factory):
        printer = await printer_factory()
        first = await _open(db_session, printer.id)
        await printer_incidents.close(db_session, first.id, status=STATUS_RESOLVED, source=RESOLVE_OBSERVED_RUNNING)

        second = await _open(db_session, printer.id, kind=KIND_JAM, codes="jam:0700_8010")

        assert second is not None  # history accumulates; only OPEN rows are exclusive
        assert await printer_incidents.get_open(db_session, printer.id) is not None

    async def test_two_printers_hold_their_own_incidents(self, db_session, printer_factory):
        a = await printer_factory()
        b = await printer_factory()
        assert await _open(db_session, a.id) is not None
        assert await _open(db_session, b.id) is not None
        assert len(await printer_incidents.all_open(db_session)) == 2


class TestClose:
    async def test_close_is_idempotent(self, db_session, printer_factory):
        printer = await printer_factory()
        row = await _open(db_session, printer.id)
        closed = await printer_incidents.close(db_session, row.id, status=STATUS_RESOLVED, source="terminal")
        stamped = closed.resolved_at

        again = await printer_incidents.close(db_session, row.id, status=STATUS_ABORTED, source="operator")

        # A second resolver must not re-stamp the close time or rewrite the verdict.
        assert again.resolved_at == stamped
        assert again.status == STATUS_RESOLVED

    async def test_escalated_stays_open(self, db_session, printer_factory):
        """An escalation is a live HOLD, not a closed fault — that is what keeps the
        printer un-re-enterable and the hourly reminder armed."""
        printer = await printer_factory()
        row = await _open(db_session, printer.id)

        escalated = await printer_incidents.mark_escalated(db_session, row.id)

        assert escalated.status == STATUS_ESCALATED
        assert escalated.resolved_at is None
        assert await printer_incidents.get_open(db_session, printer.id) is not None

    async def test_mark_escalated_keeps_the_original_stamp(self, db_session, printer_factory):
        printer = await printer_factory()
        row = await _open(db_session, printer.id, status=STATUS_ESCALATED)
        first_stamp = row.escalated_at

        again = await printer_incidents.mark_escalated(db_session, row.id)

        assert again.escalated_at == first_stamp  # the hold must not look younger

    async def test_close_open_for_printer_reports_nothing_to_close(self, db_session, printer_factory):
        printer = await printer_factory()
        assert await printer_incidents.close_open_for_printer(db_session, printer.id, source="terminal") is None


class TestAlreadyHandledAndFlapCap:
    async def test_find_closed_matches_on_the_fault_fingerprint(self, db_session, printer_factory):
        printer = await printer_factory()
        row = await _open(db_session, printer.id, codes="runout:0700_8011@0-2")
        await printer_incidents.close(db_session, row.id, status=STATUS_ABORTED, source="operator")

        assert await printer_incidents.find_closed(db_session, printer.id, "task-1", "runout:0700_8011@0-2") is not None
        # A DIFFERENT slot is a different fault — the fingerprint is slot-qualified so
        # a second roll emptying in one job is never swallowed as a duplicate.
        assert await printer_incidents.find_closed(db_session, printer.id, "task-1", "runout:0700_8011@0-3") is None
        # ...and so is the same fault on another job.
        assert await printer_incidents.find_closed(db_session, printer.id, "task-2", "runout:0700_8011@0-2") is None

    async def test_open_incidents_are_not_found_as_closed(self, db_session, printer_factory):
        printer = await printer_factory()
        await _open(db_session, printer.id, codes="runout:x")
        assert await printer_incidents.find_closed(db_session, printer.id, "task-1", "runout:x") is None

    async def test_count_resolved_counts_only_resolved_of_that_kind(self, db_session, printer_factory):
        printer = await printer_factory()
        for n, (kind, status) in enumerate(
            [(KIND_JAM, STATUS_RESOLVED), (KIND_JAM, STATUS_ABORTED), (KIND_RUNOUT, STATUS_RESOLVED)]
        ):
            row = await _open(db_session, printer.id, kind=kind, codes=f"c{n}")
            await printer_incidents.close(db_session, row.id, status=status, source=None)

        assert await printer_incidents.count_resolved(db_session, printer.id, "task-1", KIND_JAM) == 1
        assert await printer_incidents.count_resolved(db_session, printer.id, "task-2", KIND_JAM) == 0


class TestSnapshotProjection:
    async def test_open_populates_and_close_clears_the_cache(self, db_session, printer_factory):
        printer = await printer_factory()
        row = await _open(db_session, printer.id, kind=KIND_RUNOUT, slot_global_tray=2)

        snap = printer_incidents.snapshot(printer.id)
        assert snap["kind"] == KIND_RUNOUT
        assert snap["status"] == STATUS_RECOVERING
        assert snap["slot_desc"] == "AMS A slot 3"
        assert snap["created_at"] is not None

        await printer_incidents.mark_escalated(db_session, row.id)
        assert printer_incidents.snapshot(printer.id)["status"] == STATUS_ESCALATED

        await printer_incidents.close(db_session, row.id, status=STATUS_RESOLVED, source="terminal")
        assert printer_incidents.snapshot(printer.id) is None

    async def test_an_external_runout_reads_external_not_unknown(self, db_session, printer_factory):
        """An external-spool runout names no AMS slot BY NATURE — rendering "unknown"
        would read as a farm failure to attribute rather than the fact it is."""
        printer = await printer_factory()
        await _open(db_session, printer.id, code="07FF_8011", codes="runout_external:07FF_8011")

        assert printer_incidents.snapshot(printer.id)["slot_desc"] == "external"

    async def test_a_jam_names_no_slot(self, db_session, printer_factory):
        printer = await printer_factory()
        await _open(db_session, printer.id, kind=KIND_JAM, code="0700_8010", codes="mechanical_feed:0700_8010")
        assert printer_incidents.snapshot(printer.id)["slot_desc"] is None

    async def test_snapshot_is_none_without_a_printer_id(self):
        assert printer_incidents.snapshot(None) is None
        assert printer_incidents.snapshot(0) is None

    async def test_rehydrate_rebuilds_the_cache_from_the_db(self, db_session, printer_factory):
        printer = await printer_factory()
        await _open(db_session, printer.id, kind=KIND_JAM, codes="jam:x")
        printer_incidents._reset_state()  # the restart
        assert printer_incidents.snapshot(printer.id) is None

        assert await printer_incidents.rehydrate(db_session) == 1

        assert printer_incidents.snapshot(printer.id)["kind"] == KIND_JAM


class TestMigration:
    """``run_migrations`` runs on EVERY boot, so it must be idempotent — and it is
    the only path that builds this table on a pre-existing database."""

    async def test_double_run_is_idempotent_and_builds_the_partial_index(self, tmp_path):
        from sqlalchemy.ext.asyncio import create_async_engine

        import backend.app.core.database as core_db

        db_path = tmp_path / "migrate.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        try:
            async with engine.begin() as conn:
                # printer_incident references printers(id) / print_queue(id); build the
                # full schema the way init_db does, then run the migrations over it.
                await conn.run_sync(core_db.Base.metadata.create_all)
                await core_db.run_migrations(conn)
            async with engine.begin() as conn:
                await core_db.run_migrations(conn)  # second boot

            async with engine.connect() as conn:
                names = [
                    r[0]
                    for r in (
                        await conn.execute(
                            text("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='printer_incident'")
                        )
                    ).all()
                ]
                sql = (
                    await conn.execute(
                        text("SELECT sql FROM sqlite_master WHERE type='index' AND name='ux_printer_incident_open'")
                    )
                ).scalar()
            assert "ux_printer_incident_open" in names
            assert "WHERE resolved_at IS NULL" in (sql or "")
        finally:
            await engine.dispose()
