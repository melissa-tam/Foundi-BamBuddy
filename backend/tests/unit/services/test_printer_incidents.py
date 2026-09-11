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
    KIND_PLATE_VISION,
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

    async def test_the_pause_cause_kinds_that_need_hands_are_operator_resolved(self):
        from backend.app.models.printer_incident import (
            KIND_PLATE_VISION,
            KIND_Z_REFERENCE_LOST,
            RESOLUTION_OPERATOR,
        )

        assert printer_incidents.resolution_class(KIND_PLATE_VISION) == RESOLUTION_OPERATOR
        assert printer_incidents.resolution_class(KIND_Z_REFERENCE_LOST) == RESOLUTION_OPERATOR

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
        from backend.app.models.printer_incident import KIND_PLATE_VISION, RESOLUTION_OPERATOR

        assert printer_incidents.resolution_class(KIND_PLATE_VISION, external=True) == RESOLUTION_OPERATOR

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
        assert tally["by_outcome"]["auto_recovered"] == 1
        assert tally["by_outcome"]["held"] == 1
        assert tally["by_kind"][KIND_PHYSICAL]["held"] == 1
