"""``services/print_binding`` — THE owner of which archive records which print.

Pinned here, over the real test database (the owner's decisions are conditional SQL):

* the job-identity comparison (``same_job``) as a table;
* ``attach``'s decision table — resume by id, the #972 revive, the atomic adopt of a never-printed
  dispatch copy, the refusal that makes a retry a NEW record, the id-less name resume and its
  stale rule, the supersede — and the adopt race between two printers, run for real under
  production's WAL pragmas;
* the partial unique index: one ``printing`` archive per printer;
* ``resolve_terminal`` (same / unknown / other) and ``close_archive`` (closes only a live row);
* the restart regression (RC1, 2026-09-23) THROUGH ``main.on_print_complete``: a print whose
  archive carries the library storage hash, completing after a restart with no process state,
  closes ITS archive by subtask id and writes its print-log row.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.app.services.print_binding import (
    SUPERSEDED_REASON,
    Adopted,
    CreateNeeded,
    LiveJob,
    Resumed,
    attach,
    bind_created,
    close_archive,
    count_live_prints,
    live_print_archive,
    printers_with_live_print,
    resolve_terminal,
    same_job,
)
from backend.tests._fixtures.print_callbacks import (
    STORAGE_HASH_FILENAME,
    archive_row,
    drain_new_tasks,
    live_state,
    print_callbacks,
    seed_archive,
    seed_printer,
    seed_unit,
)

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
EARLIER = NOW - timedelta(hours=5)


def _naive(value: datetime | None) -> datetime | None:
    """SQLite hands a DateTime back naive; compare in UTC."""
    if value is None:
        return None
    return value.replace(tzinfo=None) if value.tzinfo is None else value.astimezone(timezone.utc).replace(tzinfo=None)


async def _attach(maker, printer_id: int, job: LiveJob, *, now: datetime = NOW):
    async with maker() as s:
        return await attach(s, printer_id, job, now=now)


# ---------------------------------------------------------------------------
# Job identity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("live", "record", "expected"),
    [
        ("2103771517", "2103771517", "same"),
        (" 2103771517 ", "2103771517", "same"),
        ("2103771517", "1844213296", "other"),
        (None, "2103771517", "unknown"),
        ("", "2103771517", "unknown"),
        ("0", "2103771517", "unknown"),
        ("2103771517", None, "unknown"),
        ("2103771517", "", "unknown"),
        ("2103771517", "0", "unknown"),
        (None, None, "unknown"),
        ("0", "0", "unknown"),
    ],
)
def test_same_job_table(live, record, expected):
    """``""``, ``"0"`` and None name no job on EITHER side — an absent id is not a different job."""
    assert same_job(live, record) == expected


# ---------------------------------------------------------------------------
# attach — the print-start binding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestAttach:
    async def test_resumes_the_jobs_own_live_archive_by_id(self, own_session_factory):
        maker = own_session_factory
        pid = await seed_printer(maker)
        archive_id = await seed_archive(maker, printer_id=pid, status="printing", started_at=EARLIER, subtask_id="S1")

        binding = await _attach(maker, pid, LiveJob(subtask_id="S1", subtask_name="anything else"))

        assert binding == Resumed(archive_id)
        row = await archive_row(maker, archive_id)
        assert (row.status, row.subtask_id, _naive(row.started_at)) == ("printing", "S1", _naive(EARLIER))

    async def test_adopts_a_never_printed_dispatch_copy(self, own_session_factory):
        """The scheduler's dispatch copy — archived, never printed, no printer yet — becomes the
        print's record, stamped with the unit's DURABLE dispatch id and the running printer."""
        maker = own_session_factory
        pid = await seed_printer(maker)
        archive_id = await seed_archive(maker, printer_id=None, status="archived", completed_at=EARLIER)
        unit_id = await seed_unit(maker, printer_id=pid, archive_id=archive_id, dispatch_subtask_id="D1")

        binding = await _attach(maker, pid, LiveJob(subtask_id="D1", subtask_name="Box.gcode"))

        assert binding == Adopted(archive_id, unit_id)
        row = await archive_row(maker, archive_id)
        assert row.status == "printing"
        assert _naive(row.started_at) == _naive(NOW)
        assert row.completed_at is None
        assert row.subtask_id == "D1"
        assert row.printer_id == pid

    async def test_adopt_stamps_the_units_id_when_the_echo_names_no_job_yet(self, own_session_factory):
        """The echo can lag the start; the sole printing unit is the attribution and ITS id — the
        one its terminal will carry — is what the record keeps."""
        maker = own_session_factory
        pid = await seed_printer(maker)
        archive_id = await seed_archive(maker, printer_id=pid)
        unit_id = await seed_unit(maker, printer_id=pid, archive_id=archive_id, dispatch_subtask_id="D2")

        binding = await _attach(maker, pid, LiveJob(subtask_id=None, subtask_name="normalised_name"))

        assert binding == Adopted(archive_id, unit_id)
        assert (await archive_row(maker, archive_id)).subtask_id == "D2"

    async def test_an_archive_that_already_recorded_a_print_is_never_adopted(self, own_session_factory):
        """One archive per ATTEMPT: a retry carries its parent's archive as the donor, and that row
        already holds the parent's print — the retry gets a new record, the parent is untouched."""
        maker = own_session_factory
        pid = await seed_printer(maker)
        parent = await seed_archive(
            maker, printer_id=pid, status="completed", started_at=EARLIER, completed_at=EARLIER, subtask_id="OLD"
        )
        unit_id = await seed_unit(maker, printer_id=pid, archive_id=parent, dispatch_subtask_id="NEW")

        binding = await _attach(maker, pid, LiveJob(subtask_id="NEW"))

        assert binding == CreateNeeded(unit_id)
        row = await archive_row(maker, parent)
        assert (row.status, row.subtask_id, _naive(row.started_at)) == ("completed", "OLD", _naive(EARLIER))

    async def test_a_screen_start_is_not_the_claimed_units_print(self, own_session_factory):
        """A job the printer names as ANOTHER one is not the unit sitting in ``printing`` — even as
        the sole candidate. The unit's dispatch copy stays unbound for its own start."""
        maker = own_session_factory
        pid = await seed_printer(maker)
        archive_id = await seed_archive(maker, printer_id=None)
        await seed_unit(maker, printer_id=pid, archive_id=archive_id, dispatch_subtask_id="D3")

        binding = await _attach(maker, pid, LiveJob(subtask_id="SCREEN-9"))

        assert binding == CreateNeeded(None)
        row = await archive_row(maker, archive_id)
        assert (row.status, row.started_at) == ("archived", None)

    async def test_two_printers_starting_copies_of_one_archive_one_adopts(self, own_session_factory):
        maker = own_session_factory
        p1 = await seed_printer(maker, serial="H2S-1")
        p2 = await seed_printer(maker, serial="H2S-2")
        shared = await seed_archive(maker, printer_id=None)
        u1 = await seed_unit(maker, printer_id=p1, archive_id=shared, dispatch_subtask_id="D-P1")
        u2 = await seed_unit(maker, printer_id=p2, archive_id=shared, dispatch_subtask_id="D-P2")

        first = await _attach(maker, p1, LiveJob(subtask_id="D-P1"))
        second = await _attach(maker, p2, LiveJob(subtask_id="D-P2"))

        assert first == Adopted(shared, u1)
        assert second == CreateNeeded(u2)
        row = await archive_row(maker, shared)
        assert (row.printer_id, row.subtask_id) == (p1, "D-P1")

    async def test_the_adopt_race_under_production_pragmas(self, wal_session_factory):
        """The same race run CONCURRENTLY on two connections under WAL: the write lock is taken
        before the first read, so the loser waits for the winner's commit, reads it, and creates."""
        maker = wal_session_factory
        p1 = await seed_printer(maker, serial="H2S-1")
        p2 = await seed_printer(maker, serial="H2S-2")
        shared = await seed_archive(maker, printer_id=None)
        u1 = await seed_unit(maker, printer_id=p1, archive_id=shared, dispatch_subtask_id="D-P1")
        u2 = await seed_unit(maker, printer_id=p2, archive_id=shared, dispatch_subtask_id="D-P2")

        results = await asyncio.gather(
            _attach(maker, p1, LiveJob(subtask_id="D-P1")),
            _attach(maker, p2, LiveJob(subtask_id="D-P2")),
        )

        assert sorted(type(r).__name__ for r in results) == ["Adopted", "CreateNeeded"]
        winner = next(r for r in results if isinstance(r, Adopted))
        loser = next(r for r in results if isinstance(r, CreateNeeded))
        assert {winner.unit_id, loser.unit_id} == {u1, u2}
        row = await archive_row(maker, shared)
        assert row.status == "printing"
        assert row.printer_id == (p1 if winner.unit_id == u1 else p2)

    async def test_adopting_supersedes_another_live_archive_on_the_printer(self, own_session_factory):
        """A printer runs one job: a second live archive there is a print whose terminal the farm
        never saw. It is closed ``cancelled`` in the same transaction, before the bind."""
        maker = own_session_factory
        pid = await seed_printer(maker)
        leaked = await seed_archive(maker, printer_id=pid, status="printing", started_at=EARLIER, subtask_id="GONE")
        archive_id = await seed_archive(maker, printer_id=None)
        await seed_unit(maker, printer_id=pid, archive_id=archive_id, dispatch_subtask_id="D4")

        binding = await _attach(maker, pid, LiveJob(subtask_id="D4"))

        assert isinstance(binding, Adopted)
        old = await archive_row(maker, leaked)
        assert old.status == "cancelled"
        assert old.failure_reason == SUPERSEDED_REASON
        assert old.completed_at is not None
        assert (await archive_row(maker, archive_id)).status == "printing"

    async def test_revives_a_stale_cancelled_archive_of_the_same_job(self, own_session_factory):
        """#972: an earlier build stale-cancelled a print that kept running; its id reappearing is
        proof, so the row is revived instead of duplicated."""
        maker = own_session_factory
        pid = await seed_printer(maker)
        archive_id = await seed_archive(
            maker,
            printer_id=pid,
            status="cancelled",
            started_at=EARLIER,
            subtask_id="S972",
            failure_reason="Stale - print likely cancelled or failed without status update",
        )

        binding = await _attach(maker, pid, LiveJob(subtask_id="S972"))

        assert binding == Resumed(archive_id)
        row = await archive_row(maker, archive_id)
        assert (row.status, row.failure_reason, row.completed_at) == ("printing", None, None)

    async def test_a_really_cancelled_archive_of_the_same_id_is_not_revived(self, own_session_factory):
        maker = own_session_factory
        pid = await seed_printer(maker)
        archive_id = await seed_archive(
            maker,
            printer_id=pid,
            status="cancelled",
            started_at=EARLIER,
            completed_at=EARLIER,
            subtask_id="S972",
            failure_reason="User cancelled",
        )

        binding = await _attach(maker, pid, LiveJob(subtask_id="S972"))

        assert binding == CreateNeeded(None)
        assert (await archive_row(maker, archive_id)).status == "cancelled"

    async def test_an_id_less_print_resumes_by_name(self, own_session_factory):
        maker = own_session_factory
        pid = await seed_printer(maker)
        archive_id = await seed_archive(
            maker,
            printer_id=pid,
            status="printing",
            started_at=NOW,
            print_name="Box",
            created_at=NOW - timedelta(minutes=10),
        )

        binding = await _attach(maker, pid, LiveJob(subtask_id=None, subtask_name="Box", progress=50.0))

        assert binding == Resumed(archive_id)

    async def test_an_id_less_stale_leftover_is_closed_and_replaced(self, own_session_factory):
        """#1485's rule, unchanged: near-0 % progress on a name-matched archive far too old to
        still be at 0 % is a leftover, not this print."""
        maker = own_session_factory
        pid = await seed_printer(maker)
        archive_id = await seed_archive(
            maker,
            printer_id=pid,
            status="printing",
            started_at=EARLIER,
            print_name="Box",
            created_at=NOW - timedelta(hours=3),
        )

        binding = await _attach(maker, pid, LiveJob(subtask_id=None, subtask_name="Box", progress=0.5))

        assert binding == CreateNeeded(None)
        row = await archive_row(maker, archive_id)
        assert row.status == "cancelled"
        assert row.failure_reason.startswith("Stale")
        assert row.completed_at is None, "the stale verdict records no completion time (upstream)"

    async def test_unknown_progress_never_cancels_an_id_less_match(self, own_session_factory):
        maker = own_session_factory
        pid = await seed_printer(maker)
        archive_id = await seed_archive(
            maker,
            printer_id=pid,
            status="printing",
            started_at=EARLIER,
            print_name="Box",
            created_at=NOW - timedelta(hours=3),
        )

        binding = await _attach(maker, pid, LiveJob(subtask_id=None, subtask_name="Box", progress=None))

        assert binding == Resumed(archive_id)

    async def test_a_job_with_an_id_is_never_matched_by_name(self, own_session_factory):
        """Name matching survives only for prints that name no job at all."""
        maker = own_session_factory
        pid = await seed_printer(maker)
        archive_id = await seed_archive(maker, printer_id=pid, status="printing", started_at=EARLIER, print_name="Box")

        binding = await _attach(maker, pid, LiveJob(subtask_id="FRESH-1", subtask_name="Box", progress=50.0))

        assert binding == CreateNeeded(None)
        assert (await archive_row(maker, archive_id)).status == "printing", "left for the create's supersede"


# ---------------------------------------------------------------------------
# bind_created — the create branch's row becomes the live print
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestBindCreated:
    async def test_binds_with_the_units_durable_id_and_supersedes(self, own_session_factory):
        maker = own_session_factory
        pid = await seed_printer(maker)
        leaked = await seed_archive(maker, printer_id=pid, status="printing", started_at=EARLIER, subtask_id="GONE")
        parent = await seed_archive(maker, printer_id=pid, status="completed", started_at=EARLIER, subtask_id="OLD")
        unit_id = await seed_unit(maker, printer_id=pid, archive_id=parent, dispatch_subtask_id="RETRY-1")
        created = await seed_archive(maker, printer_id=pid, status="archived", completed_at=NOW)

        async with maker() as s:
            bound = await bind_created(s, created, pid, LiveJob(subtask_id="ECHO"), unit_id, now=NOW)

        assert bound is True
        row = await archive_row(maker, created)
        assert (row.status, row.subtask_id, row.completed_at) == ("printing", "RETRY-1", None)
        assert _naive(row.started_at) == _naive(NOW)
        assert (await archive_row(maker, leaked)).failure_reason == SUPERSEDED_REASON
        assert (await archive_row(maker, parent)).subtask_id == "OLD"

    async def test_a_foreign_print_keeps_the_printers_echo(self, own_session_factory):
        maker = own_session_factory
        pid = await seed_printer(maker)
        created = await seed_archive(maker, printer_id=pid)

        async with maker() as s:
            assert await bind_created(s, created, pid, LiveJob(subtask_id="FOREIGN-77"), None, now=NOW)

        assert (await archive_row(maker, created)).subtask_id == "FOREIGN-77"

    async def test_a_row_already_bound_is_not_rebound(self, own_session_factory):
        maker = own_session_factory
        pid = await seed_printer(maker)
        done = await seed_archive(maker, printer_id=pid, status="completed", started_at=EARLIER, subtask_id="S")

        async with maker() as s:
            assert await bind_created(s, done, pid, LiveJob(subtask_id="S2"), None, now=NOW) is False

        row = await archive_row(maker, done)
        assert (row.status, row.subtask_id) == ("completed", "S")


# ---------------------------------------------------------------------------
# The pin: one printing archive per printer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestOneLivePrintPerPrinter:
    async def test_the_index_refuses_a_second_printing_archive_on_a_printer(self, own_session_factory):
        maker = own_session_factory
        pid = await seed_printer(maker)
        await seed_archive(maker, printer_id=pid, status="printing", started_at=EARLIER, subtask_id="A")

        with pytest.raises(IntegrityError):
            await seed_archive(maker, printer_id=pid, status="printing", started_at=NOW, subtask_id="B")

    async def test_the_index_constrains_nothing_else(self, own_session_factory):
        """Finished archives accumulate freely; other printers and printer-less rows are free."""
        maker = own_session_factory
        p1 = await seed_printer(maker, serial="H2S-1")
        p2 = await seed_printer(maker, serial="H2S-2")
        await seed_archive(maker, printer_id=p1, status="printing", started_at=NOW, subtask_id="A")
        await seed_archive(maker, printer_id=p2, status="printing", started_at=NOW, subtask_id="B")
        await seed_archive(maker, printer_id=p1, status="completed", started_at=EARLIER)
        await seed_archive(maker, printer_id=p1, status="completed", started_at=EARLIER)
        await seed_archive(maker, printer_id=None, status="printing", started_at=NOW)
        await seed_archive(maker, printer_id=None, status="printing", started_at=NOW)

        async with maker() as s:
            assert await printers_with_live_print(s) == {p1, p2}
            assert await count_live_prints(s) == 4
            assert (await live_print_archive(s, p1)).subtask_id == "A"
            assert await live_print_archive(s, 999) is None

    @pytest.mark.usefixtures("force_sqlite_dialect")
    async def test_the_index_survives_the_autoincrement_rebuild(self):
        """The id-reuse retrofit DROPs and recreates ``print_archives``; the partial index is
        captured from ``sqlite_master`` with its WHERE clause, replayed, and still enforces."""
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        from backend.app.core.database import Base, _rebuild_table_with_autoincrement
        from backend.tests._fixtures.db import MEMORY_DATABASE_URL, import_all_models

        import_all_models()
        table = Base.metadata.tables["print_archives"]
        engine = create_async_engine(MEMORY_DATABASE_URL)
        table.dialect_options["sqlite"]["autoincrement"] = False
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        finally:
            table.dialect_options["sqlite"]["autoincrement"] = True
        try:
            async with engine.begin() as conn:
                before = (
                    await conn.execute(text("SELECT sql FROM sqlite_master WHERE name = 'print_archives'"))
                ).scalar()
                assert "AUTOINCREMENT" not in before.upper(), "the fixture must build the pre-retrofit shape"
                await _rebuild_table_with_autoincrement(conn, table)
                after = (
                    await conn.execute(text("SELECT sql FROM sqlite_master WHERE name = 'print_archives'"))
                ).scalar()
                index_sql = (
                    await conn.execute(
                        text(
                            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'ux_print_archives_live_printer'"
                        )
                    )
                ).scalar()
            assert "AUTOINCREMENT" in after.upper()
            assert index_sql is not None and "printing" in index_sql and "WHERE" in index_sql.upper()

            row = {"printer_id": 1, "filename": "a.3mf", "file_path": "", "file_size": 0, "status": "printing"}
            async with engine.begin() as conn:
                await conn.execute(table.insert().values(**row))
            with pytest.raises(IntegrityError):
                async with engine.begin() as conn:
                    await conn.execute(table.insert().values(**row))
        finally:
            await engine.dispose()


# ---------------------------------------------------------------------------
# resolve_terminal / close_archive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestTerminal:
    @pytest.mark.parametrize(
        ("payload_subtask", "resolves"),
        [("S1", True), (None, True), ("", True), ("0", True), ("OTHER", False)],
        ids=["same", "none", "empty", "zero", "other"],
    )
    async def test_resolve_terminal(self, own_session_factory, payload_subtask, resolves):
        maker = own_session_factory
        pid = await seed_printer(maker)
        archive_id = await seed_archive(maker, printer_id=pid, status="printing", started_at=EARLIER, subtask_id="S1")

        async with maker() as s:
            resolved = await resolve_terminal(s, pid, payload_subtask)

        assert resolved == (archive_id if resolves else None)

    async def test_an_id_less_archive_closes_on_any_terminal(self, own_session_factory):
        maker = own_session_factory
        pid = await seed_printer(maker)
        archive_id = await seed_archive(maker, printer_id=pid, status="printing", started_at=EARLIER, subtask_id=None)

        async with maker() as s:
            assert await resolve_terminal(s, pid, "ANY") == archive_id
            assert await resolve_terminal(s, pid + 1, "ANY") is None

    async def test_close_archive_closes_only_a_live_row(self, own_session_factory):
        maker = own_session_factory
        pid = await seed_printer(maker)
        live = await seed_archive(maker, printer_id=pid, status="printing", started_at=EARLIER, subtask_id="S1")
        done = await seed_archive(maker, printer_id=pid, status="completed", started_at=EARLIER, completed_at=EARLIER)

        async with maker() as s:
            assert await close_archive(s, live, status="failed", completed_at=NOW, failure_reason="Clog") is True
            assert await close_archive(s, done, status="failed", completed_at=NOW, failure_reason="Clog") is False
            assert await close_archive(s, live, status="completed", completed_at=NOW) is False, "closes once"
            await s.commit()

        closed = await archive_row(maker, live)
        assert (closed.status, closed.failure_reason, _naive(closed.completed_at)) == ("failed", "Clog", _naive(NOW))
        untouched = await archive_row(maker, done)
        assert (untouched.status, untouched.failure_reason, _naive(untouched.completed_at)) == (
            "completed",
            None,
            _naive(EARLIER),
        )

    async def test_close_archive_refuses_to_open_a_print(self, own_session_factory):
        async with own_session_factory() as s:
            with pytest.raises(ValueError):
                await close_archive(s, 1, status="printing", completed_at=None)


# ---------------------------------------------------------------------------
# RC1 — the restart regression, through main.on_print_complete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestTerminalAcrossARestart:
    """A fresh process holds NO registry: the archive's subtask id is all that binds the print."""

    _PAYLOAD_NAME = "Fast_Half_Shell_v3_x4_spliced"

    async def _terminal(self, maker, pid: int, subtask: str):
        from backend.app.main import on_print_complete

        tasks_before = set(asyncio.all_tasks())
        with print_callbacks(maker, status=live_state(subtask_id=subtask, state="FINISH", progress=100.0)):
            await on_print_complete(
                pid,
                {
                    "status": "completed",
                    "subtask_id": subtask,
                    "subtask_name": self._PAYLOAD_NAME,
                    "filename": "/data/Metadata/plate_2.gcode",
                    "timelapse_was_active": False,
                    "peaks_reliable": True,
                    "last_layer_num": 120,
                    "last_progress": 100.0,
                },
            )
            await drain_new_tasks(tasks_before)

    async def _log_rows(self, maker, archive_id: int):
        from backend.app.models.print_log import PrintLogEntry

        async with maker() as s:
            rows = await s.execute(select(PrintLogEntry).where(PrintLogEntry.archive_id == archive_id))
            return list(rows.scalars().all())

    async def test_the_completion_closes_its_own_archive_and_logs_it(self, own_session_factory):
        maker = own_session_factory
        pid = await seed_printer(maker)
        archive_id = await seed_archive(
            maker,
            printer_id=pid,
            status="printing",
            started_at=EARLIER,
            subtask_id="2103771517",
            filename=STORAGE_HASH_FILENAME,
            print_name="Fast_Half_Shell",
        )

        await self._terminal(maker, pid, "2103771517")

        row = await archive_row(maker, archive_id)
        assert row.status == "completed", "the restart-spanning print's archive must not leak in 'printing'"
        assert row.completed_at is not None
        logs = await self._log_rows(maker, archive_id)
        assert [entry.status for entry in logs] == ["completed"]

    async def test_the_downtime_reconcile_closes_the_stale_archive_by_id(self, own_session_factory):
        """The printer came back IDLE: the reconcile's synthesised terminal carries the ARCHIVE's own
        subtask id, so ``resolve_terminal`` binds it to exactly that record — never to a same-named
        one — and ``close_archive`` records the unknown outcome once."""
        from backend.app.main import reconcile_stale_active_prints

        maker = own_session_factory
        pid = await seed_printer(maker)
        archive_id = await seed_archive(
            maker, printer_id=pid, status="printing", started_at=EARLIER, subtask_id="LOST-IN-DOWNTIME"
        )

        tasks_before = set(asyncio.all_tasks())
        with print_callbacks(maker, status=live_state(subtask_id="", state="IDLE", progress=0.0)):
            assert await reconcile_stale_active_prints(pid) == 1
            await drain_new_tasks(tasks_before)

        row = await archive_row(maker, archive_id)
        assert row.status == "cancelled", "an unknown outcome is recorded cancelled (terminal_outcome)"
        assert [entry.status for entry in await self._log_rows(maker, archive_id)] == ["cancelled"]

    async def test_another_jobs_terminal_leaves_the_live_archive_alone(self, own_session_factory):
        """The liveness pair: binding by id must also REFUSE — a terminal naming another job never
        closes the printer's live record (the 2026-09 replays closed the current print's)."""
        maker = own_session_factory
        pid = await seed_printer(maker)
        archive_id = await seed_archive(
            maker, printer_id=pid, status="printing", started_at=EARLIER, subtask_id="LIVE-JOB"
        )

        await self._terminal(maker, pid, "STALE-JOB")

        assert (await archive_row(maker, archive_id)).status == "printing"
        assert await self._log_rows(maker, archive_id) == []
