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
    KIND_PHYSICAL,
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
        from backend.app.models.printer_incident import AMS_FAULT_KINDS, PAUSE_CAUSE_KINDS

        for kind in AMS_FAULT_KINDS | PAUSE_CAUSE_KINDS:
            assert printer_incidents.waiting_reason_for(kind)

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
        """Its ORIGIN moved from ``farm_correlation``; the STRING must not, or every
        rendered surface and locale key that keys off it goes blank."""
        from backend.app.models.printer_incident import KIND_PLATE_VISION
        from backend.app.services import farm_correlation

        assert printer_incidents.waiting_reason_for(KIND_PLATE_VISION) == "plate_not_empty_printer_detected"
        assert farm_correlation.WAITING_REASON_PLATE_VISION is printer_incidents.WAITING_REASON_PLATE_VISION


class TestResolvesOnOperator:
    async def test_the_pause_cause_kinds_that_need_hands_are_operator_resolved(self):
        from backend.app.models.printer_incident import KIND_PLATE_VISION, KIND_Z_REFERENCE_LOST

        assert printer_incidents.resolves_on_operator(KIND_PLATE_VISION) is True
        assert printer_incidents.resolves_on_operator(KIND_Z_REFERENCE_LOST) is True

    async def test_wire_resolved_kinds_are_not(self):
        """Power loss included: the prompt clearing IS a wire fact, so that hold closes
        itself when the printer starts printing again."""
        from backend.app.models.printer_incident import KIND_POWER_LOSS

        for kind in (KIND_JAM, KIND_RUNOUT, KIND_PHYSICAL, KIND_POWER_LOSS):
            assert printer_incidents.resolves_on_operator(kind) is False

    async def test_an_unregistered_kind_is_wire_resolved(self):
        """The safe direction: a hold that closes too readily is visible, one that never
        closes blocks the printer forever."""
        assert printer_incidents.resolves_on_operator("no_such_kind") is False


class TestCountRecent:
    """``count_recent`` — PRINTER-scoped and windowed, unlike ``count_resolved``."""

    async def test_counts_only_this_printer_and_kind_inside_the_window(self, db_session, printer_factory):
        from datetime import datetime, timedelta

        from backend.app.models.printer_incident import KIND_PLATE_VISION

        one = await printer_factory()
        two = await printer_factory()
        now = datetime.utcnow()

        inc = await _open(db_session, one.id, kind=KIND_PLATE_VISION, job_id="job-a")
        await printer_incidents.close(db_session, inc.id, status=STATUS_RESOLVED, source=RESOLVE_OBSERVED_RUNNING)
        # A different printer, and a different kind on the same printer: neither counts.
        await _open(db_session, two.id, kind=KIND_PLATE_VISION, job_id="job-a")
        stale = await _open(db_session, one.id, kind=KIND_JAM, job_id="job-b", codes="jam:0700_8010")
        await printer_incidents.close(db_session, stale.id, status=STATUS_RESOLVED, source=RESOLVE_OBSERVED_RUNNING)

        since = now - timedelta(hours=1)
        assert await printer_incidents.count_recent(db_session, one.id, KIND_PLATE_VISION, since) == 1
        assert await printer_incidents.count_recent(db_session, two.id, KIND_PLATE_VISION, since) == 1

    async def test_a_resolved_incident_still_counts(self, db_session, printer_factory):
        """The first trip is RESOLVED by the time the second happens — the requeue closed
        it at the terminal — so a status filter would make the second trip invisible and
        the printer would re-check forever."""
        from datetime import datetime, timedelta

        from backend.app.models.printer_incident import KIND_PLATE_VISION

        printer = await printer_factory()
        first = await _open(db_session, printer.id, kind=KIND_PLATE_VISION, job_id="job-1")
        await printer_incidents.close(db_session, first.id, status=STATUS_RESOLVED, source=RESOLVE_OBSERVED_RUNNING)
        # The requeue is a NEW job — which is exactly why the job-scoped `count_resolved`
        # cannot answer this question.
        await _open(db_session, printer.id, kind=KIND_PLATE_VISION, job_id="job-2")

        since = datetime.utcnow() - timedelta(hours=1)
        assert await printer_incidents.count_recent(db_session, printer.id, KIND_PLATE_VISION, since) == 2
        assert await printer_incidents.count_resolved(db_session, printer.id, "job-2", KIND_PLATE_VISION) == 0

    async def test_the_window_excludes_older_rows(self, db_session, printer_factory):
        from datetime import datetime, timedelta

        from backend.app.models.printer_incident import KIND_PLATE_VISION

        printer = await printer_factory()
        old = await _open(db_session, printer.id, kind=KIND_PLATE_VISION, job_id="job-1")
        old.created_at = datetime.utcnow() - timedelta(days=2)
        await db_session.commit()

        since = datetime.utcnow() - timedelta(hours=1)
        assert await printer_incidents.count_recent(db_session, printer.id, KIND_PLATE_VISION, since) == 0
