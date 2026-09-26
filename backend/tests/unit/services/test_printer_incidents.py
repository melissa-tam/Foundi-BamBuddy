"""Unit tests for the durable AMS-incident store (WS2b).

The store is what replaced ``spool_recovery``'s process-lifetime dicts, so the
properties pinned here are the ones a restart used to destroy: ONE open incident
per printer (enforced by the database, not by a dict), an already-handled test that
survives a deploy, a flap cap counted from durable rows, and a projection cache the
~1 Hz WebSocket serializer can read without touching the DB.

The migration is exercised against a throwaway engine, twice, because
``run_migrations`` runs on every boot.
"""

import asyncio
import logging

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from backend.app.models.printer_incident import (
    KIND_JAM,
    KIND_PHYSICAL,
    KIND_PLATE_VISION,
    KIND_RUNOUT,
    RESOLVE_OBSERVED_RUNNING,
    STATUS_ABORTED,
    STATUS_ESCALATED,
    STATUS_RECOVERING,
    STATUS_RESOLVED,
    PrinterIncident,
)
from backend.app.models.printer_incident_step import STEP_KIND_COMMAND, STEP_KIND_LEVER
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
    """Exclusivity is per (printer, KIND) since 2026-09-11 — an asset carries
    concurrent alarms — with the three AMS kinds still mutually exclusive among
    themselves, because they are three readings of ONE AMS."""

    async def test_a_second_open_AMS_incident_is_refused(self, db_session, printer_factory):
        printer = await printer_factory()
        first = await _open(db_session, printer.id)
        assert first is not None

        second = await _open(db_session, printer.id, kind=KIND_JAM, codes="jam:0700_8010")

        assert second is None  # the pre-check reported it; nothing was written
        rows = (await db_session.execute(text("SELECT COUNT(*) FROM printer_incident"))).scalar()
        assert rows == 1

    async def test_the_ams_index_refuses_a_bypassing_write(self, db_session, printer_factory):
        """The real enforcement: a caller that bypasses ``open_new`` dies loudly.

        A dict could be emptied by a restart; this cannot. Both indexes are PARTIAL,
        so they constrain only rows with ``resolved_at IS NULL`` — and this one is
        what keeps ONE AMS from carrying a jam row and a physical row at once."""
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

    async def test_a_pause_cause_hold_opens_beside_an_ams_fault(self, db_session, printer_factory):
        """THE 2026-09-04 collision, closed. ``pause_recovery._open_z_reference_hold``
        used to get ``None`` from ``open_new`` on a printer that already carried a jam
        — and ``eject.remote.z_reference_evidence`` then let a sweep run against a Z
        datum the reboot had destroyed."""
        from backend.app.models.printer_incident import KIND_Z_REFERENCE_LOST

        printer = await printer_factory()
        assert await _open(db_session, printer.id, kind=KIND_JAM, codes="jam:0700_8010") is not None

        z_hold = await _open(db_session, printer.id, kind=KIND_Z_REFERENCE_LOST, code="", codes="")

        assert z_hold is not None
        assert {row.kind for row in await printer_incidents.open_rows(db_session, printer.id)} == {
            KIND_JAM,
            KIND_Z_REFERENCE_LOST,
        }

    async def test_a_second_row_of_the_SAME_kind_is_still_refused(self, db_session, printer_factory):
        from datetime import datetime

        from backend.app.models.printer_incident import KIND_PLATE_VISION

        printer = await printer_factory()
        await _open(db_session, printer.id, kind=KIND_PLATE_VISION, code="0500_808C", codes="0500_808C")

        db_session.add(
            PrinterIncident(
                printer_id=printer.id,
                job_id="task-2",
                item_id=None,
                kind=KIND_PLATE_VISION,
                code="0500_806E",
                codes="0500_806E",
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

        await printer_incidents.close(db_session, row.id, status=STATUS_ABORTED, source="operator")

        # A second resolver must not re-stamp the close time or rewrite the verdict.
        db_session.expunge_all()
        again = await db_session.get(PrinterIncident, row.id)
        assert again.resolved_at == stamped
        assert again.status == STATUS_RESOLVED

    async def test_close_returns_none_when_it_did_not_close_the_row(self, db_session, printer_factory):
        """The contract mirrors ``mark_escalated``: the return says whether THIS CALL
        closed the row, so a caller can tell "I closed it" from "somebody else already
        had". A recovery driver reads it to know whether it still owns the outcome it
        is about to write (006-H2S 2026-09-04: the observed-running closer freed a row
        from under a live driver, and both sinks wrote anyway)."""
        printer = await printer_factory()
        row = await _open(db_session, printer.id)

        assert await printer_incidents.close(db_session, row.id, status=STATUS_RESOLVED, source="terminal") is not None
        assert await printer_incidents.close(db_session, row.id, status=STATUS_RESOLVED, source="terminal") is None
        assert await printer_incidents.close(db_session, 987654, status=STATUS_RESOLVED, source="terminal") is None

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
        assert await printer_incidents.close_open_for_printer(db_session, printer.id, source="terminal") == []

    async def test_close_open_for_printer_closes_every_open_row(self, db_session, printer_factory):
        """It is a printer-scoped verb, and a printer can now hold more than one."""
        from backend.app.models.printer_incident import KIND_Z_REFERENCE_LOST

        printer = await printer_factory()
        await _open(db_session, printer.id, kind=KIND_JAM, codes="jam:x")
        await _open(db_session, printer.id, kind=KIND_Z_REFERENCE_LOST, code="", codes="")

        closed = await printer_incidents.close_open_for_printer(db_session, printer.id, source="terminal")

        assert {row.kind for row in closed} == {KIND_JAM, KIND_Z_REFERENCE_LOST}
        assert await printer_incidents.open_rows(db_session, printer.id) == []

    async def test_close_open_for_printer_can_be_scoped_to_kinds(self, db_session, printer_factory):
        from backend.app.models.printer_incident import AMS_FAULT_KINDS, KIND_Z_REFERENCE_LOST

        printer = await printer_factory()
        await _open(db_session, printer.id, kind=KIND_JAM, codes="jam:x")
        await _open(db_session, printer.id, kind=KIND_Z_REFERENCE_LOST, code="", codes="")

        closed = await printer_incidents.close_open_for_printer(
            db_session, printer.id, source="terminal", kinds=AMS_FAULT_KINDS
        )

        assert [row.kind for row in closed] == [KIND_JAM]
        assert [row.kind for row in await printer_incidents.open_rows(db_session, printer.id)] == [
            KIND_Z_REFERENCE_LOST
        ]


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

    async def test_an_external_feed_fault_reads_external_too(self, db_session, printer_factory):
        """003-H2S 2026-08-11: the holder speaks in more than one class. A FEED fault
        on it (``07FF_8006``, incident kind ``jam``) names no AMS slot for exactly the
        same reason its runout does, so the chip must say so — a bare "jam" with no
        slot reads as "the farm could not identify the tray", which is the misreading
        that sent this incident into the swap machine in the first place."""
        printer = await printer_factory()
        await _open(db_session, printer.id, kind=KIND_JAM, code="07FF_8006", codes="mechanical_feed:07FF_8006")

        assert printer_incidents.snapshot(printer.id)["slot_desc"] == "external"

    async def test_an_external_physical_fault_reads_external_too(self, db_session, printer_factory):
        """The third class the holder speaks in ("Please pull out the filament on the
        spool holder")."""
        printer = await printer_factory()
        await _open(db_session, printer.id, kind=KIND_PHYSICAL, code="07FF_8003", codes="physical_fault:07FF_8003")

        assert printer_incidents.snapshot(printer.id)["slot_desc"] == "external"

    async def test_an_ams_physical_fault_names_no_external_holder(self, db_session, printer_factory):
        """The liveness half: the marker must follow the HARDWARE, not the absence of
        a slot. An AMS-side fault with no slot attribution stays unnamed."""
        printer = await printer_factory()
        await _open(db_session, printer.id, kind=KIND_PHYSICAL, code="0700_8003", codes="physical_fault:0700_8003")

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

    async def test_the_full_codes_column_is_added_to_an_existing_table(self, tmp_path):
        """``hms_full_codes`` (2026-09-24) is additive and nullable, idempotent across boots."""
        from sqlalchemy.ext.asyncio import create_async_engine

        import backend.app.core.database as core_db

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cols.db'}")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(core_db.Base.metadata.create_all)
                await conn.execute(text("ALTER TABLE printer_incident DROP COLUMN hms_full_codes"))
            for _boot in range(2):
                async with engine.begin() as conn:
                    await core_db.run_migrations(conn)
            async with engine.connect() as conn:
                columns = {r[1] for r in (await conn.execute(text("PRAGMA table_info('printer_incident')"))).all()}
            assert "hms_full_codes" in columns
        finally:
            await engine.dispose()

    async def test_the_legacy_open_plate_vision_rows_are_closed_once(self, tmp_path, caplog):
        """The retired lane opened a ``plate_vision`` row and then STOPPED the print, so no
        open row of it is a live job pause — and under ``job_pause`` it would never close.
        The repair closes those, leaves every other kind alone, and (marker-keyed) never
        touches a plate-check hold opened after it ran."""
        from datetime import datetime

        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

        import backend.app.core.database as core_db
        from backend.app.models.printer import Printer

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'repair.db'}")

        def _row(kind, *, closed=False):
            now = datetime.utcnow()
            return PrinterIncident(
                printer_id=1,
                job_id="job",
                kind=kind,
                code="0500_808C" if kind == KIND_PLATE_VISION else "0700_8011",
                codes="x",
                status=STATUS_RESOLVED if closed else STATUS_ESCALATED,
                created_at=now,
                resolved_at=now if closed else None,
            )

        try:
            async with engine.begin() as conn:
                await conn.run_sync(core_db.Base.metadata.create_all)
            async with AsyncSession(engine, expire_on_commit=False) as s:
                s.add(Printer(id=1, name="P", ip_address="1.1.1.1", access_code="x", serial_number="S1", model="H2S"))
                legacy, runout, history = (
                    _row(KIND_PLATE_VISION),
                    _row(KIND_RUNOUT),
                    _row(KIND_PLATE_VISION, closed=True),
                )
                s.add_all([legacy, runout, history])
                await s.commit()
                ids = (legacy.id, runout.id, history.id)

            with caplog.at_level(logging.INFO):
                async with engine.begin() as conn:
                    await core_db.run_migrations(conn)
            assert "repair_open_plate_vision_20260924" in caplog.text

            async with AsyncSession(engine, expire_on_commit=False) as s:
                fresh = _row(KIND_PLATE_VISION)  # a GENUINE paused plate check, after the deploy
                s.add(fresh)
                await s.commit()
                fresh_id = fresh.id
            async with engine.begin() as conn:
                await core_db.run_migrations(conn)  # the next boot: marker says done

            async with AsyncSession(engine, expire_on_commit=False) as s:
                rows = {r.id: r for r in (await s.execute(select(PrinterIncident))).scalars().all()}
            assert (rows[ids[0]].status, rows[ids[0]].resolve_source) == (STATUS_RESOLVED, "legacy_vision_stop")
            assert rows[ids[0]].resolved_at is not None
            assert rows[ids[1]].resolved_at is None  # an AMS hold is not the retired lane's
            assert rows[ids[2]].resolve_source is None  # history untouched
            assert rows[fresh_id].resolved_at is None  # never re-run over a live pause
        finally:
            await engine.dispose()

    async def test_an_old_single_column_index_is_re_keyed_once(self, tmp_path):
        """The 2026-09-11 re-key, against a database carrying the OLD shape.

        ``ux_printer_incident_open`` was ``(printer_id) WHERE resolved_at IS NULL``,
        which is what made a lost-Z hold unopenable beside an AMS fault. The migration
        drops it, re-creates it on ``(printer_id, kind)`` and adds the AMS-exclusion
        index beside it — once, marker-keyed, on the same boot the new DDL runs."""
        from sqlalchemy.ext.asyncio import create_async_engine

        import backend.app.core.database as core_db

        db_path = tmp_path / "rekey.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(core_db.Base.metadata.create_all)
                # Re-create the PRE-cutover shape the way the old code left it.
                await conn.execute(text("DROP INDEX IF EXISTS ux_printer_incident_open"))
                await conn.execute(text("DROP INDEX IF EXISTS ux_printer_incident_open_ams"))
                await conn.execute(
                    text(
                        "CREATE UNIQUE INDEX ux_printer_incident_open "
                        "ON printer_incident (printer_id) WHERE resolved_at IS NULL"
                    )
                )

            async with engine.begin() as conn:
                await core_db.run_migrations(conn)

            async with engine.connect() as conn:
                cols = {
                    name: [r[2] for r in (await conn.execute(text(f"PRAGMA index_info('{name}')"))).all()]
                    for (_seq, name, _unique, _origin, _partial) in (
                        await conn.execute(text("PRAGMA index_list('printer_incident')"))
                    ).all()
                }
                ams_sql = (
                    await conn.execute(
                        text("SELECT sql FROM sqlite_master WHERE type='index' AND name='ux_printer_incident_open_ams'")
                    )
                ).scalar()
                marker = (
                    await conn.execute(
                        text("SELECT COUNT(*) FROM settings WHERE key = 'migration_incident_index_per_kind_20260911'")
                    )
                ).scalar()

            assert cols["ux_printer_incident_open"] == ["printer_id", "kind"]
            assert cols["ux_printer_incident_open_ams"] == ["printer_id"]
            for kind in ("jam", "physical", "runout"):
                assert f"'{kind}'" in (ams_sql or "")
            assert marker == 1

            # A second boot is a no-op: the marker is written, nothing is dropped.
            async with engine.begin() as conn:
                await core_db.run_migrations(conn)
            async with engine.connect() as conn:
                again = {
                    name: [r[2] for r in (await conn.execute(text(f"PRAGMA index_info('{name}')"))).all()]
                    for (_seq, name, _unique, _origin, _partial) in (
                        await conn.execute(text("PRAGMA index_list('printer_incident')"))
                    ).all()
                }
                marker_again = (
                    await conn.execute(
                        text("SELECT COUNT(*) FROM settings WHERE key = 'migration_incident_index_per_kind_20260911'")
                    )
                ).scalar()
            assert again == cols
            assert marker_again == 1
        finally:
            await engine.dispose()

    async def test_the_migrated_indexes_enforce_the_new_contract(self, tmp_path):
        """The indexes are the ENFORCEMENT, so the pin is what the database refuses:
        a second open AMS row dies, a pause-cause row beside a jam commits."""
        from datetime import datetime

        from sqlalchemy.ext.asyncio import create_async_engine

        import backend.app.core.database as core_db

        db_path = tmp_path / "enforce.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(core_db.Base.metadata.create_all)
                await core_db.run_migrations(conn)

            def _row(kind: str, code: str) -> dict:
                return {
                    "printer_id": 1,
                    "job_id": "task-1",
                    "kind": kind,
                    "code": code,
                    "codes": code,
                    "status": STATUS_RECOVERING,
                    "created_at": datetime.utcnow(),
                }

            insert = text(
                "INSERT INTO printer_incident (printer_id, job_id, kind, code, codes, status, created_at) "
                "VALUES (:printer_id, :job_id, :kind, :code, :codes, :status, :created_at)"
            )
            # No ``printers`` row: the fork sets no ``PRAGMA foreign_keys=ON`` (see
            # ``core/database`` ~4042), and what is under test here is the two partial
            # UNIQUE indexes, not referential integrity.
            async with engine.begin() as conn:
                await conn.execute(insert, _row(KIND_JAM, "0700_8010"))
                # A pause-cause hold beside it: this is the collision the re-key opens.
                await conn.execute(insert, _row("z_reference_lost", ""))

            with pytest.raises(IntegrityError):
                async with engine.begin() as conn:
                    await conn.execute(insert, _row(KIND_PHYSICAL, "0700_8004"))
        finally:
            await engine.dispose()


class TestTheKindVocabularies:
    """``FAULT_KINDS`` is DERIVED, so a new kind cannot land on the wrong side of it.

    The distinction is load-bearing exactly once — ``farm_policy._requeues_gracefully``
    asks "was this printer FAULTED when the operator stopped the print?" — and it has to
    answer no for a ``service_hold``, which is an operator statement that nothing is
    broken. Read through the un-narrowed "any open incident" question, a Stop under
    maintenance mode silently requeued the plate and the run never held for RESUME.
    """

    async def test_all_kinds_is_the_union_of_the_three_vocabularies(self):
        from backend.app.models.printer_incident import (
            ALL_KINDS,
            AMS_FAULT_KINDS,
            DECLARED_KINDS,
            PAUSE_CAUSE_KINDS,
        )

        assert ALL_KINDS == AMS_FAULT_KINDS | PAUSE_CAUSE_KINDS | DECLARED_KINDS

    async def test_fault_kinds_is_everything_a_human_did_not_declare(self):
        from backend.app.models.printer_incident import ALL_KINDS, DECLARED_KINDS, FAULT_KINDS, KIND_SERVICE_HOLD

        assert FAULT_KINDS == ALL_KINDS - DECLARED_KINDS
        assert KIND_SERVICE_HOLD not in FAULT_KINDS
        assert FAULT_KINDS, "a subtraction that emptied the set would silence the requeue lane"

    async def test_a_service_hold_alone_is_not_an_open_fault(self, db_session, printer_factory):
        """The consequence, over the real store: the row is OPEN and the printer IS
        held, but a FAULT-scoped read answers None."""
        from backend.app.models.printer_incident import FAULT_KINDS, KIND_SERVICE_HOLD

        printer = await printer_factory()
        assert await printer_incidents.open_declared(db_session, printer.id, kind=KIND_SERVICE_HOLD) is not None

        assert await printer_incidents.get_open(db_session, printer.id) is not None  # the un-narrowed question
        assert await printer_incidents.get_open(db_session, printer.id, kinds=FAULT_KINDS) is None
        assert printer_incidents.automation_held(printer.id) is True


class TestWaitingReasonVocabulary:
    """The kind -> token table, moved here 2026-09-04 from ``spool_recovery``.

    A ``waiting_reason`` is a PROJECTION of an incident row, so the table belongs with
    the store that owns the kinds. While it lived in one consumer, a kind could be
    registered for the hourly reminder and not for the projection — and the fallback hid
    it, because the missing kind rendered the spool-jam token instead of failing.
    """

    async def test_every_kind_has_a_token(self):
        """The pin that makes registration total. A new kind added to the model without
        a row here fails HERE, not in production as jam copy on an unrelated hold."""
        from backend.app.models.printer_incident import AMS_FAULT_KINDS, DECLARED_KINDS, PAUSE_CAUSE_KINDS

        for kind in AMS_FAULT_KINDS | PAUSE_CAUSE_KINDS | DECLARED_KINDS:
            assert printer_incidents.waiting_reason_for(kind)

    async def test_the_service_hold_token(self):
        """Registered for vocabulary hygiene: ``waiting_reason_for`` RAISES on an
        unregistered kind and ``RECOVERY_WAITING_REASONS`` is derived from the table, so
        an unregistered declared kind would make the projection throw at whichever call
        site met it first."""
        from backend.app.models.printer_incident import KIND_SERVICE_HOLD

        assert printer_incidents.waiting_reason_for(KIND_SERVICE_HOLD) == "printer_service_hold"
        assert printer_incidents.WAITING_REASON_SERVICE_HOLD == "printer_service_hold"
        assert "printer_service_hold" in printer_incidents.RECOVERY_WAITING_REASONS

    async def test_an_unknown_kind_raises(self):
        """It used to return the spool-jam token — a vocabulary trap: the wrong copy on
        a unit held for something else is worse than a loud failure at the one call site
        that forgot to register."""
        with pytest.raises(KeyError):
            printer_incidents.waiting_reason_for("no_such_kind")

    async def test_external_defaults_to_false(self):
        """It was keyword-only with NO default, so ``waiting_reason_for(KIND_POWER_LOSS)``
        was a TypeError — and the pause-cause kinds have no holder variant to pass."""
        from backend.app.models.printer_incident import KIND_POWER_LOSS

        assert printer_incidents.waiting_reason_for(KIND_POWER_LOSS) == "power_loss_hold"

    async def test_external_only_overrides_the_two_kinds_that_need_it(self):
        assert printer_incidents.waiting_reason_for(KIND_RUNOUT, external=True) == "external_spool_runout"
        assert printer_incidents.waiting_reason_for(KIND_JAM, external=True) == "external_feed_fault"
        # A physical fault reads the same on either hardware — one token, no synonym.
        physical = printer_incidents.waiting_reason_for(KIND_PHYSICAL)
        assert printer_incidents.waiting_reason_for(KIND_PHYSICAL, external=True) == physical

    async def test_the_owned_set_is_derived_from_the_table(self):
        """``farm_stall._ATTENDED_PAUSE_REASONS`` derives from this set, so a token
        missing from it lets the pause-stall watchdog double-escalate a hold that has
        already alerted. Deriving it removes the possibility."""
        every_token = set(printer_incidents._WAITING_REASON_BY_KIND.values()) | set(
            printer_incidents._EXTERNAL_WAITING_REASON_BY_KIND.values()
        )
        assert every_token <= printer_incidents.RECOVERY_WAITING_REASONS

    async def test_the_plate_vision_token_string_is_unchanged(self):
        """Its ORIGIN moved from ``farm_correlation`` (which keeps NO alias — one
        origin, no dual path); the STRING must not move, or every rendered surface
        and locale key that keys off it goes blank."""
        from backend.app.models.printer_incident import KIND_PLATE_VISION

        assert printer_incidents.waiting_reason_for(KIND_PLATE_VISION) == "plate_not_empty_printer_detected"
        assert printer_incidents.WAITING_REASON_PLATE_VISION == "plate_not_empty_printer_detected"


class TestResolutionClass:
    """``RESOLVES_ON`` keyed on ``(kind, external)`` — the return-to-normal rule.

    ``resolves_on_operator`` is DELETED: with three classes a boolean could only ever
    answer one of the three questions, and the two paths that used it were already
    asking "may the wire close this?", which is not the complement of "does a human
    close this?" any more.
    """

    async def test_a_lost_z_frame_is_operator_resolved(self):
        """The part coming off the plate after a reboot is the human act that ends it."""
        from backend.app.models.printer_incident import KIND_Z_REFERENCE_LOST, RESOLUTION_OPERATOR

        assert printer_incidents.resolution_class(KIND_Z_REFERENCE_LOST) == RESOLUTION_OPERATOR

    async def test_a_plate_check_trip_is_a_JOB_PAUSE(self):
        """2026-09-24: the printer paused ONE job and is asking a human. Its answer is that
        job — resumed or stopped — never a plate act (the retired lane made it
        ``operator`` because the farm stopped the print itself)."""
        from backend.app.models.printer_incident import JOB_PAUSE_KINDS, KIND_PLATE_VISION, RESOLUTION_JOB_PAUSE

        assert printer_incidents.resolution_class(KIND_PLATE_VISION) == RESOLUTION_JOB_PAUSE
        assert frozenset({KIND_PLATE_VISION}) == JOB_PAUSE_KINDS

    async def test_wire_resolved_kinds(self):
        """Power loss included: the prompt clearing IS a wire fact, so that hold closes
        itself when the printer starts printing again."""
        from backend.app.models.printer_incident import KIND_POWER_LOSS, RESOLUTION_WIRE

        for kind in (KIND_JAM, KIND_RUNOUT, KIND_POWER_LOSS):
            assert printer_incidents.resolution_class(kind) == RESOLUTION_WIRE

    async def test_an_ams_physical_fault_resolves_on_REPAIR(self):
        """The 003-H2S finding: every AMS-side physical row ever closed on this farm
        closed at a TERMINAL (the laundering) or at a resume — never because the wire
        went quiet, which it does at every terminal whether or not anything was
        fixed."""
        from backend.app.models.printer_incident import RESOLUTION_REPAIR

        assert printer_incidents.resolution_class(KIND_PHYSICAL) == RESOLUTION_REPAIR
        assert printer_incidents.resolution_class(KIND_PHYSICAL, external=False) == RESOLUTION_REPAIR

    async def test_an_EXTERNAL_physical_fault_resolves_on_the_wire(self):
        """All 8 physical rows ever closed ``wire_clear`` were external-holder PROMPT
        codes (``07FF_C012`` x3, ``07FF_C011`` x4, ``07FF_0004`` x1, each open
        126-254 s): the human presses Continue on the screen and the code clears, so
        the wire IS their return-to-normal."""
        from backend.app.models.printer_incident import RESOLUTION_WIRE

        assert printer_incidents.resolution_class(KIND_PHYSICAL, external=True) == RESOLUTION_WIRE

    async def test_an_external_variant_falls_back_to_the_registered_kind(self):
        """The pause-cause kinds have no external row at all — asking for one must not
        raise, it must answer the kind's own rule."""
        from backend.app.models.printer_incident import KIND_PLATE_VISION, RESOLUTION_JOB_PAUSE

        assert printer_incidents.resolution_class(KIND_PLATE_VISION, external=True) == RESOLUTION_JOB_PAUSE

    async def test_a_service_hold_resolves_on_the_verb_that_opened_it(self):
        """The FOURTH class (2026-09-12). A declared hold has no fault behind it, so no
        evidence can end it: not a plate act, not a wire edge, not a terminal, not
        repair evidence. Only the operator leaving maintenance mode."""
        from backend.app.models.printer_incident import KIND_SERVICE_HOLD, RESOLUTION_DECLARED

        assert printer_incidents.resolution_class(KIND_SERVICE_HOLD) == RESOLUTION_DECLARED
        # ...and asking for a holder variant answers the kind's own rule, like the
        # pause-cause kinds: there is no spool holder for a maintenance hold to sit on.
        assert printer_incidents.resolution_class(KIND_SERVICE_HOLD, external=True) == RESOLUTION_DECLARED

    async def test_an_unregistered_kind_is_wire_resolved(self):
        """The safe direction, unchanged: a hold that closes too readily is visible,
        one that never closes blocks the printer forever."""
        from backend.app.models.printer_incident import RESOLUTION_WIRE

        assert printer_incidents.resolution_class("no_such_kind") == RESOLUTION_WIRE

    async def test_row_external_is_read_from_the_taxonomy(self, db_session, printer_factory):
        """ONE derivation of a row's externality — the classifier's own verdict over
        the row's durable ``code`` (doctrine invariant 1), the same one the chip's
        ``slot_desc`` reads."""
        printer = await printer_factory()
        ams = await _open(db_session, printer.id, kind=KIND_PHYSICAL, code="0700_8004", codes="physical_fault:x")
        assert printer_incidents.row_external(ams) is False

        await printer_incidents.close(db_session, ams.id, status=STATUS_RESOLVED, source="terminal")
        holder = await _open(db_session, printer.id, kind=KIND_PHYSICAL, code="07FF_C011", codes="physical_fault:y")
        assert printer_incidents.row_external(holder) is True


class TestJobPauseHeld:
    """``job_pause_held`` — THE predicate every job-resuming lane reads (the
    ``automation_held`` idiom): pure, DB-free, over the projection cache."""

    async def test_an_open_plate_check_hold_holds_the_printer(self, db_session, printer_factory):
        printer = await printer_factory()
        await _open(db_session, printer.id, kind=KIND_PLATE_VISION, code="0500_808C", codes="0500_808C")

        assert printer_incidents.job_pause_held(printer.id) is True

    async def test_a_fault_or_a_declared_hold_is_not_a_job_pause(self, db_session, printer_factory):
        from backend.app.models.printer_incident import KIND_SERVICE_HOLD

        printer = await printer_factory()
        await _open(db_session, printer.id)  # a runout
        await printer_incidents.open_declared(db_session, printer.id, kind=KIND_SERVICE_HOLD)

        assert printer_incidents.job_pause_held(printer.id) is False

    async def test_closing_the_hold_releases_the_lanes(self, db_session, printer_factory):
        printer = await printer_factory()
        row = await _open(db_session, printer.id, kind=KIND_PLATE_VISION, code="0500_808C", codes="0500_808C")
        await printer_incidents.close(db_session, row.id, status=STATUS_RESOLVED, source=RESOLVE_OBSERVED_RUNNING)

        assert printer_incidents.job_pause_held(printer.id) is False

    async def test_no_printer_is_never_held(self):
        assert printer_incidents.job_pause_held(None) is False
        assert printer_incidents.job_pause_held(0) is False


class TestPrinterMessagesProjection:
    """``printer_messages`` — the printer's OWN words for a hold, recorded at open and
    rendered by the one catalog renderer; ALWAYS present on the projection (the shipped
    frontend reads it unconditionally), JSON primitives only."""

    _FULL = "0500080C0000808C"  # the hms[] lane's 16-hex identifier for 0500_808C

    async def test_the_recorded_full_codes_render_the_printers_words(self, db_session, printer_factory):
        import json

        printer = await printer_factory()
        await _open(
            db_session,
            printer.id,
            kind=KIND_PLATE_VISION,
            code="0500_808C",
            codes="0500_808C",
            hms_full_codes=[self._FULL],
        )

        payload = printer_incidents.snapshot(printer.id, kind=KIND_PLATE_VISION)

        assert payload["printer_messages"] == [
            {
                "short_code": "0500_808C",
                "description": (
                    "Detected build plate offset. Please align the build plate with the heatbed, and then continue."
                ),
            }
        ]
        assert payload["job_id"] == "task-1"
        json.dumps(payload)  # the WS lane has no encoder

    async def test_the_codes_survive_on_disk_and_through_rehydrate(self, db_session, printer_factory):
        printer = await printer_factory()
        row = await _open(
            db_session,
            printer.id,
            kind=KIND_PLATE_VISION,
            code="0500_808C",
            codes="0500_808C",
            hms_full_codes=[self._FULL],
        )
        assert row.hms_full_codes == self._FULL

        printer_incidents._reset_state()
        await printer_incidents.rehydrate(db_session)

        payload = printer_incidents.snapshot(printer.id, kind=KIND_PLATE_VISION)
        assert [m["short_code"] for m in payload["printer_messages"]] == ["0500_808C"]

    async def test_a_legacy_row_falls_back_to_its_short_code(self, db_session, printer_factory):
        """A row opened before the column existed renders from ``code``."""
        printer = await printer_factory()
        await _open(db_session, printer.id, kind=KIND_PLATE_VISION, code="0500_808C", codes="0500_808C")

        payload = printer_incidents.snapshot(printer.id, kind=KIND_PLATE_VISION)

        assert payload["printer_messages"][0]["short_code"] == "0500_808C"
        assert payload["printer_messages"][0]["description"].startswith("Detected build plate offset")

    async def test_a_code_less_hold_projects_an_empty_list(self, db_session, printer_factory):
        """Always present: a declared hold, and a lost-Z hold whose ``code`` is a kind
        token rather than anything the printer said."""
        from backend.app.models.printer_incident import KIND_SERVICE_HOLD, KIND_Z_REFERENCE_LOST

        printer = await printer_factory()
        await printer_incidents.open_declared(db_session, printer.id, kind=KIND_SERVICE_HOLD)
        await _open(db_session, printer.id, kind=KIND_Z_REFERENCE_LOST, job_id="", code="power_loss", codes="")

        for payload in printer_incidents.snapshots(printer.id):
            assert payload["printer_messages"] == []

    async def test_an_upgrade_carries_the_worse_faults_words(self, db_session, printer_factory):
        """A row re-classified onto a physical fault must not keep the milder fault's text."""
        printer = await printer_factory()
        row = await _open(db_session, printer.id, kind=KIND_JAM, code="0700_8006", codes="jam:x")

        await printer_incidents.upgrade(
            db_session,
            row.id,
            kind=KIND_PHYSICAL,
            code="0700_8004",
            codes="physical:y",
            slot_global_tray=None,
            hms_full_codes=["0700200000008004"],
        )

        payload = printer_incidents.snapshot(printer.id)
        assert [m["short_code"] for m in payload["printer_messages"]] == ["0700_8004"]

    async def test_the_column_value_is_bounded_at_a_code_boundary(self):
        """VARCHAR(512): a truncated hex code would render as garbage, so the join stops
        at the last whole code; no codes stores NULL, not an empty string."""
        joined = printer_incidents.join_full_codes([self._FULL] * 40)
        assert joined is not None and len(joined) <= 512
        assert all(len(code) == 16 for code in joined.split(","))
        assert printer_incidents.join_full_codes([]) is None


class TestCachedKind:
    """The identity-scoped read a LIVE recovery driver uses to learn that the store
    re-classified the row it is working on. It answers about ONE row — the driver's
    own — because a driver holds an immutable context resolved at its entry gate, and
    a projection that answered about "whatever is open now" would report a re-class
    every time a different incident opened on that printer."""

    async def test_the_payload_carries_the_row_id(self, db_session, printer_factory):
        printer = await printer_factory()
        row = await _open(db_session, printer.id, kind=KIND_JAM, codes="jam:x")

        assert printer_incidents.snapshot(printer.id)["id"] == row.id

    async def test_it_answers_for_the_row_the_caller_names(self, db_session, printer_factory):
        printer = await printer_factory()
        row = await _open(db_session, printer.id, kind=KIND_JAM, codes="jam:x")

        assert printer_incidents.cached_kind(printer.id, row.id) == KIND_JAM

    async def test_it_answers_none_for_a_different_row(self, db_session, printer_factory):
        printer = await printer_factory()
        row = await _open(db_session, printer.id, kind=KIND_JAM, codes="jam:x")

        assert printer_incidents.cached_kind(printer.id, row.id + 1) is None

    async def test_it_answers_none_with_no_open_row(self, db_session, printer_factory):
        printer = await printer_factory()
        row = await _open(db_session, printer.id, kind=KIND_JAM, codes="jam:x")
        await printer_incidents.close(db_session, row.id, status=STATUS_RESOLVED, source="terminal")

        assert printer_incidents.cached_kind(printer.id, row.id) is None

    async def test_an_upgraded_kind_is_what_comes_back(self, db_session, printer_factory):
        """The point of the reader: the row's kind CHANGED while its id did not."""
        printer = await printer_factory()
        row = await _open(db_session, printer.id, kind=KIND_JAM, codes="jam:x")
        row.kind = KIND_PHYSICAL
        await db_session.commit()
        await printer_incidents.rehydrate(db_session)

        assert printer_incidents.cached_kind(printer.id, row.id) == KIND_PHYSICAL


class TestPrecedenceAndDispatchGate:
    """A printer may hold several faults; a SINGLE-SLOT reader must pick one, always
    the same one. ``KIND_PRECEDENCE`` is that order, and its AMS head mirrors
    ``spool_recovery._CLASS_PRECEDENCE``."""

    async def test_get_open_returns_the_highest_precedence_row(self, db_session, printer_factory):
        from backend.app.models.printer_incident import KIND_Z_REFERENCE_LOST

        printer = await printer_factory()
        await _open(db_session, printer.id, kind=KIND_Z_REFERENCE_LOST, code="", codes="")
        await _open(db_session, printer.id, kind=KIND_JAM, code="0700_8010", codes="jam:x")

        # The AMS fault interrupted the running print — it is named first.
        assert (await printer_incidents.get_open(db_session, printer.id)).kind == KIND_JAM

    async def test_get_open_can_be_scoped_to_kinds(self, db_session, printer_factory):
        from backend.app.models.printer_incident import AMS_FAULT_KINDS, KIND_Z_REFERENCE_LOST

        printer = await printer_factory()
        await _open(db_session, printer.id, kind=KIND_Z_REFERENCE_LOST, code="", codes="")
        await _open(db_session, printer.id, kind=KIND_JAM, code="0700_8010", codes="jam:x")

        scoped = await printer_incidents.get_open(db_session, printer.id, kinds={KIND_Z_REFERENCE_LOST})
        assert scoped.kind == KIND_Z_REFERENCE_LOST
        assert (await printer_incidents.get_open(db_session, printer.id, kinds=AMS_FAULT_KINDS)).kind == KIND_JAM
        assert await printer_incidents.get_open(db_session, printer.id, kinds={KIND_RUNOUT}) is None

    async def test_open_rows_is_oldest_first(self, db_session, printer_factory):
        from backend.app.models.printer_incident import KIND_PLATE_VISION

        printer = await printer_factory()
        first = await _open(db_session, printer.id, kind=KIND_PLATE_VISION, code="0500_808C", codes="a")
        second = await _open(db_session, printer.id, kind=KIND_JAM, code="0700_8010", codes="b")

        assert [row.id for row in await printer_incidents.open_rows(db_session, printer.id)] == [
            first.id,
            second.id,
        ]

    async def test_snapshot_picks_by_precedence_and_can_be_asked_by_kind(self, db_session, printer_factory):
        from backend.app.models.printer_incident import KIND_Z_REFERENCE_LOST

        printer = await printer_factory()
        await _open(db_session, printer.id, kind=KIND_Z_REFERENCE_LOST, code="", codes="")
        await _open(db_session, printer.id, kind=KIND_PHYSICAL, code="0700_8004", codes="physical_fault:x")

        assert printer_incidents.snapshot(printer.id)["kind"] == KIND_PHYSICAL
        assert printer_incidents.snapshot(printer.id, kind=KIND_Z_REFERENCE_LOST)["kind"] == KIND_Z_REFERENCE_LOST
        assert printer_incidents.snapshot(printer.id, kind=KIND_RUNOUT) is None

    async def test_open_kinds_and_the_dispatch_gate(self, db_session, printer_factory):
        """``hold_blocks_dispatch`` is THE one origin of "this printer carries an
        unresolved hold", read by the scheduler beside the WIRE gate. EVERY open kind
        blocks: a plate-vision or lost-Z row is already plate-gated, and a power-loss
        row means the prompt is still unanswered."""
        from backend.app.models.printer_incident import KIND_POWER_LOSS

        printer = await printer_factory()
        assert printer_incidents.open_kinds(printer.id) == frozenset()
        assert printer_incidents.hold_blocks_dispatch(printer.id) is False

        row = await _open(db_session, printer.id, kind=KIND_POWER_LOSS, code="0300_8007", codes="0300_8007")

        assert printer_incidents.open_kinds(printer.id) == frozenset({KIND_POWER_LOSS})
        assert printer_incidents.hold_blocks_dispatch(printer.id) is True

        await printer_incidents.close(db_session, row.id, status=STATUS_RESOLVED, source="terminal")
        assert printer_incidents.hold_blocks_dispatch(printer.id) is False

    async def test_the_cache_holds_every_open_row_of_a_printer(self, db_session, printer_factory):
        from backend.app.models.printer_incident import KIND_PLATE_VISION

        printer = await printer_factory()
        await _open(db_session, printer.id, kind=KIND_PLATE_VISION, code="0500_808C", codes="a")
        jam = await _open(db_session, printer.id, kind=KIND_JAM, code="0700_8010", codes="b")
        assert printer_incidents.open_kinds(printer.id) == frozenset({KIND_PLATE_VISION, KIND_JAM})

        await printer_incidents.close(db_session, jam.id, status=STATUS_RESOLVED, source="terminal")

        # Closing ONE row must not take the printer's other hold out of the chip.
        assert printer_incidents.open_kinds(printer.id) == frozenset({KIND_PLATE_VISION})
        assert printer_incidents.snapshot(printer.id)["kind"] == KIND_PLATE_VISION

    async def test_rehydrate_rebuilds_every_open_row(self, db_session, printer_factory):
        from backend.app.models.printer_incident import KIND_Z_REFERENCE_LOST

        printer = await printer_factory()
        await _open(db_session, printer.id, kind=KIND_JAM, code="0700_8010", codes="b")
        await _open(db_session, printer.id, kind=KIND_Z_REFERENCE_LOST, code="", codes="")
        printer_incidents._reset_state()  # the restart

        assert await printer_incidents.rehydrate(db_session) == 2

        assert printer_incidents.open_kinds(printer.id) == frozenset({KIND_JAM, KIND_Z_REFERENCE_LOST})


class TestUpgrade:
    """A standing fault that turns out to be WORSE re-classifies the row it is already
    carrying instead of being refused. 003-H2S: ``0700_0012`` arrived 1.2 s before
    ``0700_8004``, so the row opened ``jam`` — CONTINUE, an out-of-rotation stamp on a
    healthy spool, two unloads against filament that cannot retract."""

    async def test_it_rewrites_the_live_fingerprint_and_refreshes_the_cache(self, db_session, printer_factory):
        printer = await printer_factory()
        row = await _open(db_session, printer.id, kind=KIND_JAM, code="0700_0012", codes="mechanical_feed:0700_0012")

        upgraded = await printer_incidents.upgrade(
            db_session,
            row.id,
            kind=KIND_PHYSICAL,
            code="0700_8004",
            codes="physical_fault:0700_8004,mechanical_feed:0700_0012",
            slot_global_tray=1,
        )

        assert upgraded is not None
        assert (upgraded.kind, upgraded.code, upgraded.slot_global_tray) == (KIND_PHYSICAL, "0700_8004", 1)
        assert upgraded.codes == "physical_fault:0700_8004,mechanical_feed:0700_0012"
        # A live driver learns of the re-classification through exactly this reader.
        assert printer_incidents.cached_kind(printer.id, row.id) == KIND_PHYSICAL
        assert printer_incidents.snapshot(printer.id)["kind"] == KIND_PHYSICAL

    async def test_it_keeps_the_row_open_and_its_id(self, db_session, printer_factory):
        printer = await printer_factory()
        row = await _open(db_session, printer.id, kind=KIND_JAM, code="0700_0012", codes="a")

        upgraded = await printer_incidents.upgrade(
            db_session, row.id, kind=KIND_PHYSICAL, code="0700_8004", codes="b", slot_global_tray=None
        )

        assert upgraded.id == row.id
        assert upgraded.resolved_at is None
        assert len(await printer_incidents.open_rows(db_session, printer.id)) == 1

    async def test_a_closed_row_cannot_be_upgraded(self, db_session, printer_factory):
        printer = await printer_factory()
        row = await _open(db_session, printer.id, kind=KIND_JAM, code="0700_0012", codes="a")
        await printer_incidents.close(db_session, row.id, status=STATUS_RESOLVED, source="terminal")

        assert (
            await printer_incidents.upgrade(
                db_session, row.id, kind=KIND_PHYSICAL, code="0700_8004", codes="b", slot_global_tray=None
            )
            is None
        )

    async def test_a_missing_row_answers_none(self, db_session):
        assert (
            await printer_incidents.upgrade(
                db_session, 987654, kind=KIND_PHYSICAL, code="0700_8004", codes="b", slot_global_tray=None
            )
            is None
        )


class TestOutcomeDerivation:
    """The zero-human tally's ONE origin (2026-09-11). Pure over the three stored facts:
    status, escalated_at, resolve_source — and total over every token the model
    defines, so a new close token cannot fall into a bucket by accident."""

    @staticmethod
    def _row(*, status, escalated=False, source=None, resolved=True, kind=KIND_JAM):
        from datetime import datetime, timedelta

        now = datetime.utcnow()
        return PrinterIncident(
            printer_id=1,
            job_id="task-1",
            item_id=None,
            kind=kind,
            code="0700_8010",
            codes="x",
            status=status,
            created_at=now - timedelta(minutes=5),
            escalated_at=now - timedelta(minutes=4) if escalated else None,
            resolved_at=now if resolved else None,
            resolve_source=source,
        )

    def test_open_rows(self):
        from backend.app.models.printer_incident import STATUS_RECOVERING

        assert printer_incidents.outcome_of(self._row(status=STATUS_RECOVERING, resolved=False)) == "recovering"
        assert (
            printer_incidents.outcome_of(self._row(status=STATUS_ESCALATED, escalated=True, resolved=False)) == "held"
        )

    def test_the_farm_recovered_it_only_when_nobody_was_paged(self):
        from backend.app.models.printer_incident import (
            RESOLVE_AUTO_RESUME,
            RESOLVE_DRIVER_SELF_HEAL,
            RESOLVE_DRIVER_SWAP,
        )

        for source in (RESOLVE_DRIVER_SWAP, RESOLVE_DRIVER_SELF_HEAL, RESOLVE_AUTO_RESUME):
            assert printer_incidents.outcome_of(self._row(status=STATUS_RESOLVED, source=source)) == "auto_recovered"
            # The same close AFTER a page had a human in the loop (a refill, a fix).
            assert (
                printer_incidents.outcome_of(self._row(status=STATUS_RESOLVED, escalated=True, source=source))
                == "human_resolved"
            )

    def test_a_first_trip_recheck_is_the_farms_own(self):
        from backend.app.models.printer_incident import RESOLVE_TERMINAL

        row = self._row(status=STATUS_RESOLVED, source=RESOLVE_TERMINAL, kind=KIND_PLATE_VISION)
        assert printer_incidents.outcome_of(row) == "auto_recovered"
        # ...but a jam closed by a terminal without a page closed on nobody's act.
        row = self._row(status=STATUS_RESOLVED, source=RESOLVE_TERMINAL, kind=KIND_JAM)
        assert printer_incidents.outcome_of(row) == "resolved_unpaged"

    async def test_a_print_the_drivers_own_verb_ended_is_not_a_recovery(self):
        """``driver_ended`` (2026-09-23) is the driver's close when a release lever ENDED
        the print. The farm acted, but nothing was recovered — the unit is requeued — so
        it must never land in the zero-human ``auto_recovered`` bucket. The token lives in
        the store until it joins the model's vocabulary, so the model-scanning totality
        case above cannot see it; this case pins it by name."""
        source = printer_incidents.RESOLVE_DRIVER_ENDED

        assert printer_incidents.outcome_of(self._row(status=STATUS_RESOLVED, source=source)) == "resolved_unpaged"
        assert (
            printer_incidents.outcome_of(self._row(status=STATUS_RESOLVED, escalated=True, source=source))
            == "human_resolved"
        )

    async def test_the_driver_ended_token_is_new_and_fits_its_column(self):
        import backend.app.models.printer_incident as model

        model_tokens = {getattr(model, name) for name in dir(model) if name.startswith("RESOLVE_")}
        column = PrinterIncident.__table__.c.resolve_source

        assert printer_incidents.RESOLVE_DRIVER_ENDED not in model_tokens
        assert len(printer_incidents.RESOLVE_DRIVER_ENDED) <= column.type.length

    def test_every_paged_close_is_human_resolved(self):
        import backend.app.models.printer_incident as model

        tokens = [getattr(model, name) for name in dir(model) if name.startswith("RESOLVE_")]
        assert len(tokens) >= 8
        for token in tokens:
            row = self._row(status=STATUS_RESOLVED, escalated=True, source=token)
            assert printer_incidents.outcome_of(row) == "human_resolved", token

    def test_aborts(self):
        from backend.app.models.printer_incident import RESOLVE_OPERATOR

        assert printer_incidents.outcome_of(self._row(status=STATUS_ABORTED, source=RESOLVE_OPERATOR)) == "taken_over"
        assert printer_incidents.outcome_of(self._row(status=STATUS_ABORTED, source=None)) == "transient"

    def test_another_actors_pause_is_taken_over_and_fits_its_column(self):
        """``paused_elsewhere`` (F5, 2026-09-25): the recovery driver stood aside from a
        quiet PAUSE after its own swap resume ran. The printer was taken over — by an actor
        the wire does not name — so the row is ``taken_over``, never ``transient`` (it held
        the printer) and never a recovery."""
        from backend.app.models.printer_incident import RESOLVE_PAUSED_ELSEWHERE

        column = PrinterIncident.__table__.c.resolve_source

        assert RESOLVE_PAUSED_ELSEWHERE == "paused_elsewhere"
        assert len(RESOLVE_PAUSED_ELSEWHERE) <= column.type.length
        assert (
            printer_incidents.outcome_of(self._row(status=STATUS_ABORTED, source=RESOLVE_PAUSED_ELSEWHERE))
            == "taken_over"
        )
        assert (
            printer_incidents.outcome_of(
                self._row(status=STATUS_ABORTED, escalated=True, source=RESOLVE_PAUSED_ELSEWHERE)
            )
            == "taken_over"
        )

    def test_every_token_lands_in_exactly_one_bucket(self):
        import backend.app.models.printer_incident as model

        tokens = [getattr(model, name) for name in dir(model) if name.startswith("RESOLVE_")]
        for token in tokens:
            for status in (STATUS_RESOLVED, STATUS_ABORTED):
                for escalated in (False, True):
                    outcome = printer_incidents.outcome_of(self._row(status=status, escalated=escalated, source=token))
                    assert outcome in printer_incidents.OUTCOMES, (token, status, escalated)

    def test_summary_counts_by_outcome_and_kind(self):
        from backend.app.models.printer_incident import RESOLVE_DRIVER_SWAP

        rows = [
            self._row(status=STATUS_RESOLVED, source=RESOLVE_DRIVER_SWAP),
            self._row(status=STATUS_ESCALATED, escalated=True, resolved=False, kind=KIND_PHYSICAL),
        ]
        tally = printer_incidents.summary(rows)
        assert tally["total"] == 2
        assert tally["zero_human"] == 1
        assert tally["declared"] == 0
        assert tally["by_outcome"]["auto_recovered"] == 1
        assert tally["by_outcome"]["held"] == 1
        assert tally["by_kind"][KIND_PHYSICAL]["held"] == 1

    def test_summary_excludes_declared_holds_from_the_fault_ledger(self):
        """This is the EQUIPMENT-FAULT ledger. A planned maintenance hold has no fault,
        paged nobody and was "recovered" by nobody, so counting it in ``total`` would
        dilute the zero-human ratio by exactly the amount of maintenance the shop does,
        and its ``by_outcome`` bucket would read like a machine that broke. It stays
        visible under ``by_kind`` and gets its own count."""
        from backend.app.models.printer_incident import KIND_SERVICE_HOLD, RESOLVE_DRIVER_SWAP

        rows = [
            self._row(status=STATUS_RESOLVED, source=RESOLVE_DRIVER_SWAP),
            self._row(status=STATUS_ESCALATED, escalated=True, resolved=False, kind=KIND_SERVICE_HOLD),
            self._row(status=STATUS_RESOLVED, escalated=True, source="operator", kind=KIND_SERVICE_HOLD),
        ]
        tally = printer_incidents.summary(rows)

        assert tally["total"] == 1  # the one real fault
        assert tally["declared"] == 2
        assert tally["total"] + tally["declared"] == len(rows)
        assert tally["zero_human"] == 1
        assert tally["by_outcome"]["held"] == 0  # the open hold is NOT an equipment hold
        assert tally["by_outcome"]["human_resolved"] == 0
        # ...but the rows are still visible, under their own kind.
        assert tally["by_kind"][KIND_SERVICE_HOLD]["held"] == 1
        assert tally["by_kind"][KIND_SERVICE_HOLD]["human_resolved"] == 1


class TestDeclaredHolds:
    """The FOURTH resolution class: a hold a human opened with a verb (2026-09-12).

    Maintenance mode is a ``printer_incident`` of kind ``service_hold``, and the whole
    point of the ``declared`` class is that NOTHING closes it except the verb that
    opened it. The 001/009/010-H2S incident is why: an operator toggling maintenance
    mode lifts the part out and marks the plate cleared — so a hold that ``clear_plate``
    or Recover could close would end at the exact moment the machine is most open, and
    the scheduler would dispatch onto a printer with hands in it.
    """

    async def test_open_declared_writes_the_code_less_shape(self, db_session, printer_factory):
        from backend.app.models.printer_incident import KIND_SERVICE_HOLD

        printer = await printer_factory()

        row = await printer_incidents.open_declared(db_session, printer.id, kind=KIND_SERVICE_HOLD)

        assert row is not None
        assert (row.kind, row.job_id, row.code, row.codes) == (KIND_SERVICE_HOLD, "", "", "")
        assert row.item_id is None and row.slot_global_tray is None
        # A human's hold is a human's from the instant it opens — nothing is recovering
        # it — so ``escalated_at`` is when the operator took the printer.
        assert row.status == STATUS_ESCALATED
        assert row.escalated_at is not None
        assert row.resolved_at is None

    async def test_open_declared_is_idempotent(self, db_session, printer_factory):
        """Same contract as ``open_new``: ``None`` means the hold is already standing."""
        from backend.app.models.printer_incident import KIND_SERVICE_HOLD

        printer = await printer_factory()
        assert await printer_incidents.open_declared(db_session, printer.id, kind=KIND_SERVICE_HOLD) is not None

        assert await printer_incidents.open_declared(db_session, printer.id, kind=KIND_SERVICE_HOLD) is None

        rows = await printer_incidents.open_rows(db_session, printer.id)
        assert [row.kind for row in rows] == [KIND_SERVICE_HOLD]

    async def test_open_declared_refuses_a_fault_kind(self, db_session, printer_factory):
        """A fault opened with no fault fingerprint is a row nothing can classify."""
        printer = await printer_factory()

        with pytest.raises(ValueError, match="not a declared incident kind"):
            await printer_incidents.open_declared(db_session, printer.id, kind=KIND_JAM)

        assert await printer_incidents.open_rows(db_session, printer.id) == []

    async def test_a_declared_hold_stands_beside_a_fault(self, db_session, printer_factory):
        """Per-KIND exclusivity: a printer taken for maintenance while it carries a jam
        holds both rows, and the jam keeps its own lifecycle."""
        from backend.app.models.printer_incident import KIND_SERVICE_HOLD

        printer = await printer_factory()
        assert await _open(db_session, printer.id, kind=KIND_JAM, codes="jam:0700_8010") is not None

        assert await printer_incidents.open_declared(db_session, printer.id, kind=KIND_SERVICE_HOLD) is not None

        assert {row.kind for row in await printer_incidents.open_rows(db_session, printer.id)} == {
            KIND_JAM,
            KIND_SERVICE_HOLD,
        }

    async def test_automation_held_is_the_declared_kinds_and_only_those(self, db_session, printer_factory):
        """THE one predicate every automation lane reads. It is NOT
        ``hold_blocks_dispatch``: a jam blocks work while the farm keeps trying to
        recover it; a declared hold means nothing automatic may act at all."""
        from backend.app.models.printer_incident import KIND_SERVICE_HOLD

        faulted = await printer_factory()
        held = await printer_factory()
        await _open(db_session, faulted.id, kind=KIND_JAM, codes="jam:0700_8010")
        await printer_incidents.open_declared(db_session, held.id, kind=KIND_SERVICE_HOLD)

        assert printer_incidents.automation_held(held.id) is True
        assert printer_incidents.automation_held(faulted.id) is False
        assert printer_incidents.automation_held(None) is False
        assert printer_incidents.automation_held(424242) is False
        # ...while BOTH block dispatch, which is the other question.
        assert printer_incidents.hold_blocks_dispatch(held.id) is True
        assert printer_incidents.hold_blocks_dispatch(faulted.id) is True

    async def test_automation_held_clears_when_the_hold_closes(self, db_session, printer_factory):
        from backend.app.models.printer_incident import KIND_SERVICE_HOLD

        printer = await printer_factory()
        row = await printer_incidents.open_declared(db_session, printer.id, kind=KIND_SERVICE_HOLD)
        assert row is not None

        await printer_incidents.close(db_session, row.id, status=STATUS_RESOLVED, source="operator")

        assert printer_incidents.automation_held(printer.id) is False

    async def test_automation_held_survives_a_restart_through_rehydrate(self, db_session, printer_factory):
        """The cache is a projection; the ROW is the hold. Startup order depends on this
        (main's lifespan rehydrates before the plates hydrate, because the plate policy
        driver reads this predicate when it arms)."""
        from backend.app.models.printer_incident import KIND_SERVICE_HOLD

        printer = await printer_factory()
        await printer_incidents.open_declared(db_session, printer.id, kind=KIND_SERVICE_HOLD)
        printer_incidents._reset_state()
        assert printer_incidents.automation_held(printer.id) is False  # cache emptied

        assert await printer_incidents.rehydrate(db_session) == 1

        assert printer_incidents.automation_held(printer.id) is True

    async def test_the_chip_never_names_the_hold_over_a_fault(self, db_session, printer_factory):
        """``KIND_PRECEDENCE`` puts it LAST: the chip names what stopped the WORK, and
        maintenance mode stopped nothing — the operator did. The banner reads its own
        row by kind."""
        from backend.app.models.printer_incident import KIND_SERVICE_HOLD

        printer = await printer_factory()
        await _open(db_session, printer.id, kind=KIND_JAM, codes="jam:0700_8010")
        await printer_incidents.open_declared(db_session, printer.id, kind=KIND_SERVICE_HOLD)

        assert printer_incidents.snapshot(printer.id)["kind"] == KIND_JAM
        assert printer_incidents.snapshot(printer.id, kind=KIND_SERVICE_HOLD)["kind"] == KIND_SERVICE_HOLD


class TestNoCloserEndsADeclaredHold:
    """The closer matrix, asserted against the REAL closers rather than reasoned about.

    Every lifecycle path that can close somebody else's row is exercised on a printer
    presenting exactly the evidence that closes a fault of another class — connected,
    IDLE, wire clean, plate cleared, job terminal, restart — and the declared row must
    survive all of them. The plan's claim that "the closers need no edits" is what this
    measures; where a closer selected the wire lane by falling out of an ``else`` it is
    now selected by NAME, and these cases are the pins.
    """

    @pytest.fixture(autouse=True)
    def _recovery_state(self):
        from backend.app.services import spool_recovery

        spool_recovery._reset_state()
        yield
        spool_recovery._reset_state()

    @pytest.fixture(autouse=True)
    def _own_sessions(self, test_engine, monkeypatch):
        """Every closer opens its own session (module convention) — point them at the
        test engine, the shape ``test_incident_wire_sweep`` uses."""
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        import backend.app.core.database as core_db

        monkeypatch.setattr(
            core_db, "async_session", async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        )

    @staticmethod
    def _wire_idle(monkeypatch):
        """Connected, IDLE, no HMS — the evidence every wire-class closer acts on."""
        from backend.app.services import spool_recovery
        from backend.app.services.bambu_mqtt import PrinterState

        state = PrinterState()
        state.state = "IDLE"
        state.subtask_id = ""
        state.hms_errors = []
        monkeypatch.setattr(spool_recovery.printer_manager, "get_status", lambda _pid: state)
        monkeypatch.setattr(spool_recovery.printer_manager, "is_connected", lambda _pid: True)
        return state

    async def _held(self, db, printer_factory):
        from backend.app.models.printer_incident import KIND_SERVICE_HOLD

        printer = await printer_factory()
        row = await printer_incidents.open_declared(db, printer.id, kind=KIND_SERVICE_HOLD)
        assert row is not None
        return printer, row

    async def _still_open(self, db, printer_id):
        from backend.app.models.printer_incident import KIND_SERVICE_HOLD

        db.expunge_all()
        rows = await printer_incidents.open_rows(db, printer_id)
        assert [row.kind for row in rows] == [KIND_SERVICE_HOLD], "the declared hold was CLOSED by a closer"
        assert printer_incidents.automation_held(printer_id) is True

    @pytest.mark.parametrize("recover", [False, True])
    async def test_pause_recovery_on_plate_cleared(self, db_session, printer_factory, recover):
        """Both operator verbs. Marking the plate cleared is the FIRST thing an operator
        does after lifting the part out, and Recover is the override for a stuck gate —
        neither is "I have finished working on this machine"."""
        from backend.app.services import pause_recovery

        printer, _ = await self._held(db_session, printer_factory)

        assert await pause_recovery.on_plate_cleared(printer.id, recover=recover) == []

        await self._still_open(db_session, printer.id)

    async def test_spool_recovery_on_job_terminal(self, db_session, printer_factory):
        from backend.app.services import spool_recovery
        from backend.app.services.incident_resolution import TerminalEvent

        printer, _ = await self._held(db_session, printer_factory)

        terminal = TerminalEvent(status="completed", eject=False, job_id="task-1")
        assert await spool_recovery.on_job_terminal(printer.id, terminal) is False

        await self._still_open(db_session, printer.id)

    async def test_spool_recovery_on_observed_running(self, db_session, printer_factory, monkeypatch):
        """An operator may start a print from the screen WHILE the printer is held —
        maintenance mode keeps every manual verb legal — so a RUNNING edge must not be
        read as the end of the hold."""
        from backend.app.services import spool_recovery

        printer, _ = await self._held(db_session, printer_factory)
        self._wire_idle(monkeypatch)

        assert await spool_recovery.on_observed_running(printer.id) is False

        await self._still_open(db_session, printer.id)

    async def test_spool_recovery_wire_clear_sweep(self, db_session, printer_factory, monkeypatch):
        """Connected, positive, no actionable fault IS the normal reading of a printer
        somebody is working on. Swept twice, past the dwell, so a survival cannot be a
        close merely deferred."""
        from backend.app.services import spool_recovery

        printer, _ = await self._held(db_session, printer_factory)
        self._wire_idle(monkeypatch)

        assert await spool_recovery.sweep_open_incidents(now=0.0) == 0
        assert await spool_recovery.sweep_open_incidents(now=spool_recovery._HOLD_OVER_DWELL_S + 1) == 0

        await self._still_open(db_session, printer.id)

    async def test_the_startup_rearm(self, db_session, printer_factory, monkeypatch):
        """A restart is not a human saying they are finished — and a held printer reads
        IDLE, which is the wire ladder's own "the hold is over" evidence."""
        from backend.app.services import spool_recovery

        printer, _ = await self._held(db_session, printer_factory)
        self._wire_idle(monkeypatch)

        assert await spool_recovery.rearm_incidents_on_startup() == 0

        await self._still_open(db_session, printer.id)


# --- 2026-09-23: liveness has ONE store, and the driver's step ledger ---------------


@pytest.fixture
async def driver_tasks():
    """Spawn REAL tasks that stay running until teardown.

    ``driver_live`` asks ``task.done()``, so a stand-in that cannot finish (or cannot
    run) would pin nothing about the one question the store answers.
    """
    release = asyncio.Event()
    spawned: list[asyncio.Task[object]] = []

    def _spawn() -> asyncio.Task[object]:
        task = asyncio.create_task(release.wait())
        spawned.append(task)
        return task

    yield _spawn
    release.set()
    await asyncio.gather(*spawned)


async def _finished_task() -> asyncio.Task[object]:
    task = asyncio.create_task(asyncio.sleep(0))
    await task
    return task


_SPAWN_OVER_LIVE = "spawning a recovery driver while one is live"


def _spawn_warnings(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING and _SPAWN_OVER_LIVE in record.getMessage()
    ]


class TestDriverLiveness:
    """THE liveness store. The open row is the durable PROMISE that somebody will produce
    an outcome; this slot is whether a task is keeping it RIGHT NOW. ``driver_live`` is the
    one spelling, and it asks ``.done()`` rather than trusting membership."""

    async def test_no_printer_and_an_empty_slot_read_not_live(self):
        assert printer_incidents.driver_live(None) is False
        assert printer_incidents.driver_live(0) is False
        assert printer_incidents.driver_live(7) is False

    async def test_a_registered_running_task_is_live_on_its_printer_only(self, driver_tasks):
        printer_incidents.register_driver(7, driver_tasks(), incident_id=1)

        assert printer_incidents.driver_live(7) is True
        assert printer_incidents.driver_live(8) is False

    async def test_a_finished_task_reads_not_live(self):
        """The R1 orphan: a driver that died without releasing its slot must not read as
        live, or every closer would stand aside under a task nobody is running."""
        printer_incidents.register_driver(7, await _finished_task(), incident_id=1)

        assert printer_incidents.driver_live(7) is False

    async def test_release_frees_the_slot_it_holds(self, driver_tasks):
        task = driver_tasks()
        printer_incidents.register_driver(7, task, incident_id=1)

        printer_incidents.release_driver(7, task)

        assert printer_incidents.driver_live(7) is False

    async def test_a_release_by_a_task_not_holding_the_slot_is_a_no_op(self, driver_tasks):
        """A driver's ``finally`` can run after a SUCCESSOR took the slot. Popping by
        printer alone would erase the successor's liveness while it runs."""
        predecessor, successor = driver_tasks(), driver_tasks()
        printer_incidents.register_driver(7, predecessor, incident_id=1)
        printer_incidents.register_driver(7, successor, incident_id=2)

        printer_incidents.release_driver(7, predecessor)

        assert printer_incidents.driver_live(7) is True
        printer_incidents.release_driver(7, successor)
        assert printer_incidents.driver_live(7) is False

    async def test_a_release_of_an_empty_slot_is_a_no_op(self, driver_tasks):
        printer_incidents.release_driver(7, driver_tasks())

        assert printer_incidents.driver_live(7) is False

    async def test_spawning_over_a_live_driver_warns_once_and_still_takes_the_slot(self, driver_tasks, caplog):
        """Observability, not a gate: the spawn goes through, and the moment is one grep
        away (006-H2S 17:23:55 on 2026-09-04 produced no line at all)."""
        successor = driver_tasks()
        printer_incidents.register_driver(7, driver_tasks(), incident_id=1)

        with caplog.at_level(logging.WARNING, logger=printer_incidents.logger.name):
            printer_incidents.register_driver(7, successor, incident_id=2)

        warnings = _spawn_warnings(caplog)
        assert len(warnings) == 1
        assert "invariant violated (incident 2 spawned over a live driver)" in warnings[0]
        printer_incidents.release_driver(7, successor)
        assert printer_incidents.driver_live(7) is False  # the slot was the successor's

    async def test_spawning_over_a_finished_driver_is_silent(self, driver_tasks, caplog):
        printer_incidents.register_driver(7, await _finished_task(), incident_id=1)

        with caplog.at_level(logging.WARNING, logger=printer_incidents.logger.name):
            printer_incidents.register_driver(7, driver_tasks(), incident_id=2)

        assert _spawn_warnings(caplog) == []
        assert printer_incidents.driver_live(7) is True

    async def test_the_reset_hook_clears_the_slots(self, driver_tasks):
        printer_incidents.register_driver(7, driver_tasks(), incident_id=1)

        printer_incidents._reset_state()

        assert printer_incidents.driver_live(7) is False

    async def test_rehydrate_leaves_the_slots_alone(self, db_session, driver_tasks):
        """A rehydrate re-reads ROWS. It neither invents a task nor kills one."""
        printer_incidents.register_driver(7, driver_tasks(), incident_id=1)

        await printer_incidents.rehydrate(db_session)

        assert printer_incidents.driver_live(7) is True


class TestSnapshotProjectsDriverLiveness:
    """``driver_live`` rides the wire dict, computed at READ time: the cache is filled at
    open and at rehydrate and would be stale the moment a driver spawned or exited."""

    async def test_snapshot_follows_the_registry_live(self, db_session, printer_factory, driver_tasks):
        printer = await printer_factory()
        row = await _open(db_session, printer.id, kind=KIND_JAM, codes="jam:0700_8010")
        task = driver_tasks()

        assert printer_incidents.snapshot(printer.id)["driver_live"] is False

        printer_incidents.register_driver(printer.id, task, incident_id=row.id)
        assert printer_incidents.snapshot(printer.id)["driver_live"] is True
        assert printer_incidents.snapshot(printer.id, kind=KIND_JAM)["driver_live"] is True

        printer_incidents.release_driver(printer.id, task)
        assert printer_incidents.snapshot(printer.id)["driver_live"] is False

    async def test_snapshots_carry_it_on_every_row(self, db_session, printer_factory, driver_tasks):
        from backend.app.models.printer_incident import KIND_Z_REFERENCE_LOST

        printer = await printer_factory()
        row = await _open(db_session, printer.id, kind=KIND_JAM, codes="jam:0700_8010")
        await _open(db_session, printer.id, kind=KIND_Z_REFERENCE_LOST, code="", codes="")

        assert [snap["driver_live"] for snap in printer_incidents.snapshots(printer.id)] == [False, False]

        printer_incidents.register_driver(printer.id, driver_tasks(), incident_id=row.id)
        assert [snap["driver_live"] for snap in printer_incidents.snapshots(printer.id)] == [True, True]

    async def test_a_finished_driver_projects_false(self, db_session, printer_factory):
        printer = await printer_factory()
        row = await _open(db_session, printer.id, kind=KIND_JAM, codes="jam:0700_8010")

        printer_incidents.register_driver(printer.id, await _finished_task(), incident_id=row.id)

        assert printer_incidents.snapshot(printer.id)["driver_live"] is False

    async def test_the_cached_payload_never_holds_it_and_a_read_is_a_copy(
        self, db_session, printer_factory, driver_tasks
    ):
        printer = await printer_factory()
        row = await _open(db_session, printer.id, kind=KIND_JAM, codes="jam:0700_8010")
        printer_incidents.register_driver(printer.id, driver_tasks(), incident_id=row.id)

        snap = printer_incidents.snapshot(printer.id)
        listed = printer_incidents.snapshots(printer.id)[0]
        snap["kind"] = "mutated-by-a-reader"
        listed["status"] = "mutated-by-a-reader"

        cached = printer_incidents._open_cache[printer.id][row.id]
        assert "driver_live" not in cached
        assert cached["kind"] == KIND_JAM
        assert cached["status"] == STATUS_RECOVERING
        assert printer_incidents.cached_kind(printer.id, row.id) == KIND_JAM


class TestStepLedger:
    """What a recovery driver SENT against an incident — noted at the send, answered at
    the read, read back in send order. ``printer_incidents`` is the one writer."""

    @staticmethod
    async def _incident_id(db_session, printer_factory) -> int:
        printer = await printer_factory()
        row = await _open(db_session, printer.id, kind=KIND_JAM, codes="jam:0700_8010")
        return row.id

    async def test_a_step_is_persisted_at_the_send_unanswered(
        self, db_session, printer_factory, own_session_factory, caplog
    ):
        incident_id = await self._incident_id(db_session, printer_factory)

        with caplog.at_level(logging.INFO, logger=printer_incidents.logger.name):
            step = await printer_incidents.note_step(
                db_session, incident_id, seq=1, kind=STEP_KIND_LEVER, name="resume", feeder="jammed"
            )

        assert (step.incident_id, step.seq, step.kind, step.name, step.target, step.feeder) == (
            incident_id,
            1,
            "lever",
            "resume",
            None,
            "jammed",
        )
        assert step.sent_at is not None
        assert (step.outcome, step.read_at) == (None, None)
        # COMMITTED, not merely flushed: a second session sees it.
        async with own_session_factory() as other:
            assert [s.name for s in await printer_incidents.steps_of(other, incident_id)] == ["resume"]
        assert f"printer_incidents: incident {incident_id} step 1 lever=resume sent" in caplog.text

    async def test_answer_step_fills_the_read(self, db_session, printer_factory, own_session_factory, caplog):
        incident_id = await self._incident_id(db_session, printer_factory)
        await printer_incidents.note_step(
            db_session, incident_id, seq=1, kind=STEP_KIND_COMMAND, name="unload", target=3, feeder="jammed"
        )

        with caplog.at_level(logging.INFO, logger=printer_incidents.logger.name):
            step = await printer_incidents.answer_step(db_session, incident_id, 1, outcome="held")

        assert step is not None
        assert (step.outcome, step.target) == ("held", 3)
        assert step.read_at is not None and step.read_at >= step.sent_at
        async with own_session_factory() as other:
            (stored,) = await printer_incidents.steps_of(other, incident_id)
            assert (stored.outcome, stored.read_at) == ("held", step.read_at)
        assert f"printer_incidents: incident {incident_id} step 1 command=unload outcome=held" in caplog.text

    async def test_answering_a_step_that_was_never_sent_answers_none(self, db_session, printer_factory):
        incident_id = await self._incident_id(db_session, printer_factory)
        await printer_incidents.note_step(db_session, incident_id, seq=1, kind=STEP_KIND_LEVER, name="resume")

        assert await printer_incidents.answer_step(db_session, incident_id, 2, outcome="wedged") is None
        assert await printer_incidents.answer_step(db_session, incident_id + 1000, 1, outcome="wedged") is None

    async def test_steps_come_back_in_send_order_and_only_their_incidents(self, db_session, printer_factory):
        first = await self._incident_id(db_session, printer_factory)
        second = await self._incident_id(db_session, printer_factory)
        # Written out of order on purpose: the log's order is ``seq``, never insertion.
        await printer_incidents.note_step(db_session, first, seq=2, kind=STEP_KIND_LEVER, name="ams_control_resume")
        await printer_incidents.note_step(db_session, second, seq=1, kind=STEP_KIND_LEVER, name="resume")
        await printer_incidents.note_step(db_session, first, seq=3, kind=STEP_KIND_COMMAND, name="unload", target=3)
        await printer_incidents.note_step(db_session, first, seq=1, kind=STEP_KIND_LEVER, name="resume")

        steps = await printer_incidents.steps_of(db_session, first)

        assert [(s.seq, s.name) for s in steps] == [(1, "resume"), (2, "ams_control_resume"), (3, "unload")]
        assert [s.seq for s in await printer_incidents.steps_of(db_session, second)] == [1]

    async def test_a_duplicate_seq_raises_and_leaves_the_session_usable(self, db_session, printer_factory):
        """A driver that lost count of its own log dies loudly — and the failed commit is
        rolled back inside the writer, so the caller's session goes on working."""
        incident_id = await self._incident_id(db_session, printer_factory)
        await printer_incidents.note_step(db_session, incident_id, seq=1, kind=STEP_KIND_LEVER, name="resume")

        with pytest.raises(IntegrityError):
            await printer_incidents.note_step(
                db_session, incident_id, seq=1, kind=STEP_KIND_LEVER, name="ams_control_resume"
            )

        await printer_incidents.note_step(
            db_session, incident_id, seq=2, kind=STEP_KIND_LEVER, name="ams_control_resume"
        )
        steps = await printer_incidents.steps_of(db_session, incident_id)
        assert [(s.seq, s.name) for s in steps] == [(1, "resume"), (2, "ams_control_resume")]


class TestStepLedgerCascade:
    """The steps are a property of their incident: ``ON DELETE CASCADE``.

    The fork opens SQLite with foreign keys OFF and no code path deletes an incident row,
    so on the farm's SQLite the clause is the declared contract; PostgreSQL enforces it.
    This case runs the ORM-built schema with enforcement ON, which is the only way to see
    the clause act rather than merely exist."""

    async def test_deleting_the_incident_deletes_its_steps(self):
        from sqlalchemy.ext.asyncio import AsyncSession

        from backend.app.models.printer import Printer
        from backend.tests._fixtures.db import create_memory_engine

        engine = await create_memory_engine()
        try:
            async with engine.connect() as conn:
                await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
                # Guard against a vacuous pass: the pragma is silently ignored inside a
                # transaction, and then a cascade would "work" by never being asked.
                assert (await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar() == 1
            async with AsyncSession(engine, expire_on_commit=False) as db:
                printer = Printer(
                    name="Cascade Printer",
                    serial_number="00M09A999999999",
                    ip_address="192.168.1.250",
                    access_code="12345678",
                    model="H2S",
                )
                db.add(printer)
                await db.commit()
                incident = await _open(db, printer.id, kind=KIND_JAM, codes="jam:0700_8010")
                incident_id = incident.id
                await printer_incidents.note_step(db, incident_id, seq=1, kind=STEP_KIND_LEVER, name="resume")
                await printer_incidents.note_step(
                    db, incident_id, seq=2, kind=STEP_KIND_COMMAND, name="unload", target=3
                )

                await db.delete(incident)
                await db.commit()

                assert await printer_incidents.steps_of(db, incident_id) == []
                assert (await db.execute(text("SELECT COUNT(*) FROM printer_incident_step"))).scalar() == 0
        finally:
            await engine.dispose()
