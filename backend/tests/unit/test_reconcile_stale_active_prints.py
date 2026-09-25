"""The downtime reconcile (#1542 follow-up; rebuilt 2026-09-25 for RC3).

Background: the PRINT COMPLETE callback reacts to one state transition. A print that ends during an
MQTT disconnect window — or across a Bambuddy restart — is never observed ending, and its archive
stays ``printing``. Once per MQTT session, on the session's first FRESH report, ``main``'s
connected-edge hook asks the binding owner what became of the printer's live archive
(``services/print_reconcile``).

RC3 (production 2026-09-16 → 09-24): the old reconcile replayed such an archive as a PRINTER
terminal even while the printer ran ANOTHER job — a plate gate raised mid-print, a foreign-job page,
~7.2 kg of phantom filament charged from the live job's progress, the running job's usage session
popped. These tests pin the rebuilt shape:

* ``judge`` — the pure verdict table, every live-state × freshness × identity × run-unit cell;
* the evidence it is fed and the payload the ``ended`` verdict synthesises (the 2026-08-29
  deposit pins moved here from the old synthesiser);
* the reconcile THROUGH ``main.reconcile_stale_active_prints`` (the hook's body) and the REAL
  ``main.on_print_complete`` over the test database (``_fixtures/print_callbacks``): superseded,
  ended, ended-unattributed and observed, each with what it must NOT do.

The hook's trigger (the cached-state broadcast does not fire it, the first fresh report does, once
per session) is pinned in ``test_printer_offline_notification.TestReconcileOncePerSession``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from backend.app.services.plate_occupancy import (
    CooldownEject,
    DepositEvidence,
    EscalationOnly,
    PendingEject,
    plate_occupancy,
)
from backend.app.services.print_reconcile import ArchiveEvidence, ended_payload, evidence_of, judge
from backend.tests._fixtures.print_callbacks import (
    STORAGE_HASH_FILENAME,
    archive_row,
    drain_new_tasks,
    live_state,
    print_callbacks,
    seed_archive,
    seed_printer,
)

EARLIER = datetime.now(timezone.utc) - timedelta(hours=5)

# ---------------------------------------------------------------------------
# The judge — a table
# ---------------------------------------------------------------------------

_STATES = {
    "active": ("PREPARE", "SLICING", "RUNNING", "PAUSE"),
    "ended": ("IDLE", "FINISH", "FAILED"),
    "unknown": ("", "UNKNOWN", "OFFLINE"),
}
# (live_job, archive_job) per identity. ``unknown`` is either side naming no job — "" and "0" are
# the printer's two words for that, None the archive's.
_IDENTITIES = {
    "same": (("J1", "J1"),),
    "other": (("J2", "J1"),),
    "unknown": (("", "J1"), ("0", "J1"), (None, "J1"), ("J1", None)),
}
# The run unit: none, or still printing (both "not ended"), or ended.
_UNIT = {False: (None,), True: ("completed", "failed", "cancelled", "skipped")}

# THE table: (state group, fresh, identity, run unit ended) -> verdict. Written out cell by cell so
# the precedence is read here, not re-derived.
_EXPECTED: dict[tuple[str, bool, str, bool], str] = {
    # A state that is not this session's is no evidence — whatever else it says.
    **{("active", False, i, u): "running" for i in _IDENTITIES for u in (False, True)},
    **{("ended", False, i, u): "running" for i in _IDENTITIES for u in (False, True)},
    **{("unknown", f, i, u): "running" for f in (False, True) for i in _IDENTITIES for u in (False, True)},
    # Active on this job, or on one it cannot compare: leave it to its own terminal — ahead of observed.
    ("active", True, "same", False): "running",
    ("active", True, "same", True): "running",
    ("active", True, "unknown", False): "running",
    ("active", True, "unknown", True): "running",
    # Active on ANOTHER job: over, outcome unknown — unless the run already recorded its end.
    ("active", True, "other", False): "superseded",
    ("active", True, "other", True): "observed",
    # Ended, naming this job: the full terminal — unless the run already recorded its end.
    ("ended", True, "same", False): "ended",
    ("ended", True, "same", True): "observed",
    # Ended, naming another job or none: the plate held source-less + the superseded job phase.
    ("ended", True, "other", False): "ended_unattributed",
    ("ended", True, "other", True): "observed",
    ("ended", True, "unknown", False): "ended_unattributed",
    ("ended", True, "unknown", True): "observed",
}


def _cells():
    for (group, fresh, identity, unit_ended), verdict in _EXPECTED.items():
        for state in _STATES[group]:
            for live_job, archive_job in _IDENTITIES[identity]:
                for unit_status in _UNIT[unit_ended]:
                    yield pytest.param(
                        ArchiveEvidence(
                            live_state=state,
                            live_fresh=fresh,
                            live_job=live_job,
                            archive_job=archive_job,
                            run_unit_status=unit_status,
                        ),
                        verdict,
                        id=f"{state or 'EMPTY'}-{'fresh' if fresh else 'stale'}-{identity}-{live_job}/{archive_job}-{unit_status}",
                    )


def test_the_table_covers_every_cell():
    """Every (group, freshness, identity, unit) combination has a written verdict."""
    assert len(_EXPECTED) == len(_STATES) * 2 * len(_IDENTITIES) * 2


@pytest.mark.parametrize(("evidence", "verdict"), list(_cells()))
def test_judge(evidence, verdict):
    assert judge(evidence) == verdict


class TestEvidence:
    """What the judge is fed: the state upper-cased, freshness from THIS session's first report,
    and the run unit's status only once it ended."""

    @staticmethod
    def _state(**overrides):
        fields = {
            "connected": True,
            "connection_epoch": 3,
            "report_epoch": 3,
            "state": "running",
            "subtask_id": "J2",
        }
        fields.update(overrides)
        return SimpleNamespace(**fields)

    @staticmethod
    def _archive(subtask_id="J1"):
        return SimpleNamespace(subtask_id=subtask_id)

    def test_the_state_is_upper_cased_and_the_ids_carried_raw(self):
        ev = evidence_of(self._state(), self._archive(), None)
        assert (ev.live_state, ev.live_job, ev.archive_job, ev.live_fresh) == ("RUNNING", "J2", "J1", True)

    @pytest.mark.parametrize(
        "overrides",
        [
            {"connected": False},
            {"report_epoch": None},  # _on_connect cleared it: the cached broadcast
            {"report_epoch": 2},  # the previous session's report
        ],
        ids=["disconnected", "no-report-yet", "previous-session"],
    )
    def test_a_state_that_is_not_this_sessions_is_not_fresh(self, overrides):
        assert evidence_of(self._state(**overrides), self._archive(), None).live_fresh is False

    @pytest.mark.parametrize(
        ("unit", "status"),
        [
            (None, None),
            (SimpleNamespace(status="printing"), None),
            (SimpleNamespace(status="completed"), "completed"),
            (SimpleNamespace(status="cancelled"), "cancelled"),
        ],
    )
    def test_the_run_unit_counts_only_once_it_ended(self, unit, status):
        assert evidence_of(self._state(), self._archive(), unit).run_unit_status == status


# ---------------------------------------------------------------------------
# The terminal an ``ended`` verdict synthesises (the deposit pins of 2026-08-29)
# ---------------------------------------------------------------------------


def _ended_state(state: str, *, progress: float = 0.0, layer: int = 0):
    return SimpleNamespace(state=state, subtask_id="ARCHIVE_ID", progress=progress, layer_num=layer, raw_data={})


_GHOST = SimpleNamespace(filename="ghost.3mf", print_name="ghost", subtask_id="ARCHIVE_ID")


class TestEndedPayload:
    """FINISH / FAILED of THIS job is real evidence; IDLE says only that it ended. Both are reported
    as un-measured (``peaks_reliable: False``) — nobody observed this print's peaks."""

    def test_finish_synthesises_completed_with_the_live_evidence(self):
        payload = ended_payload(_ended_state("FINISH", progress=100.0, layer=250), _GHOST)
        assert payload["status"] == "completed"
        assert payload["subtask_id"] == "ARCHIVE_ID"
        assert (payload["last_progress"], payload["last_layer_num"]) == (100.0, 250)
        assert payload["peaks_reliable"] is False
        assert payload["_reconciled"] is True

    def test_failed_synthesises_failed(self):
        payload = ended_payload(_ended_state("FAILED", progress=42.0, layer=88), _GHOST)
        assert payload["status"] == "failed"
        assert payload["last_layer_num"] == 88

    def test_idle_synthesises_an_unknown_outcome(self):
        """The one classifier turns ``outcome_unknown`` into ``reconcile_unknown`` — the run HOLDS for
        a human instead of finishing one plate short (2026-09-19)."""
        payload = ended_payload(_ended_state("IDLE"), _GHOST)
        assert payload["status"] == "aborted"
        assert payload["outcome_unknown"] is True
        assert payload["subtask_id"] == "ARCHIVE_ID"
        assert "last_progress" not in payload  # no fabricated evidence
        assert payload["peaks_reliable"] is False


class TestReconciledTerminalsGateThePlate:
    """The 2026-08-29 → 08-30 restart-recovery cascade, pinned at its source.

    A reconciled terminal is synthesised for a print NOBODY observed, so its peaks are not a
    measurement of the job. The ``aborted`` shape once read its ABSENT peaks as zeros — "nothing on
    the plate" — and six physically FINISHED prints on printers 1-6 went ungated, recorded
    ``cancelled``, with the next unit dispatched onto each finished part. ``DepositEvidence`` fails
    closed on unreliable peaks, so a reconciled terminal GATES the plate and a human decides.
    """

    def test_the_aborted_shape_deposits_and_gates_the_plate(self):
        evidence = DepositEvidence.from_terminal_payload(ended_payload(_ended_state("IDLE"), _GHOST), is_dry_run=False)
        assert evidence.deposited is True, (
            "an unobserved terminal must gate the plate — reading its absent peaks as zeros is the "
            "2026-08-29 cascade (six ungated plates, six units recorded cancelled though they completed)"
        )

    @pytest.mark.parametrize("state", ["FINISH", "FAILED"])
    def test_a_reconciled_finish_or_failure_deposits(self, state):
        payload = ended_payload(_ended_state(state, progress=42.0, layer=88), _GHOST)
        assert DepositEvidence.from_terminal_payload(payload, is_dry_run=False).deposited is True

    def test_a_reconciled_dry_run_still_never_deposits(self):
        """The one exemption the fix did NOT widen: the eject dry-run file is motion-only by design."""
        payload = ended_payload(_ended_state("IDLE"), _GHOST)
        assert DepositEvidence.from_terminal_payload(payload, is_dry_run=True).deposited is False


# ---------------------------------------------------------------------------
# Through main's hook and the real terminal
# ---------------------------------------------------------------------------


async def _seed_farm_unit(
    maker,
    printer_id: int,
    *,
    dispatch: str,
    status: str = "printing",
    eject_profile_id: int | None = None,
    completed_at: datetime | None = None,
    archive_id: int | None = None,
) -> tuple[int, int]:
    """A FARM unit (its run carries a ``sku_file_id``) dispatched as ``dispatch``; (unit, run) ids."""
    from backend.app.models.print_batch import PrintBatch
    from backend.app.models.print_queue import PrintQueueItem

    async with maker() as s:
        batch = PrintBatch(name="run", sku_file_id=1, status="active")
        s.add(batch)
        await s.commit()
        unit = PrintQueueItem(
            printer_id=printer_id,
            batch_id=batch.id,
            archive_id=archive_id,
            status=status,
            dispatch_subtask_id=dispatch,
            eject_profile_id=eject_profile_id,
            started_at=EARLIER,
            completed_at=completed_at,
        )
        s.add(unit)
        await s.commit()
        return unit.id, batch.id


async def _row(maker, model, ident):
    async with maker() as s:
        return await s.get(model, ident)


async def _log_statuses(maker, archive_id: int) -> list[str]:
    from backend.app.models.print_log import PrintLogEntry

    async with maker() as s:
        rows = await s.execute(select(PrintLogEntry.status).where(PrintLogEntry.archive_id == archive_id))
        return [status for (status,) in rows.all()]


class _Reconnect:
    """One reconnect of ``printer_id`` onto ``state``: ``main``'s hook body, the real terminal it may
    run, and spies on the lanes the verdicts must (not) reach.

    ``usage`` spies the filament charge (``usage_tracker.on_print_complete``, the lane the phantom
    7.2 kg came through); ``on_terminal`` / ``on_unit_terminal`` spy the farm policy's two entries
    (``wraps`` — the real policy still runs); ``deletes`` records the file cleanup."""

    def __init__(self, maker, state):
        self.maker = maker
        self.state = state

    async def run(self, printer_id: int) -> int:
        from backend.app.main import reconcile_stale_active_prints
        from backend.app.services import farm_policy
        from backend.app.services.bambu_ftp import DeleteResult

        tasks_before = set(asyncio.all_tasks())
        with (
            print_callbacks(self.maker, status=self.state) as mocks,
            patch("backend.app.services.usage_tracker.on_print_complete", new=AsyncMock(return_value=[])) as usage,
            patch(
                "backend.app.services.bambu_ftp.delete_file_async",
                new=AsyncMock(return_value=DeleteResult.DELETED),
            ) as deletes,
            patch.object(farm_policy, "on_terminal", wraps=farm_policy.on_terminal) as on_terminal,
            patch.object(farm_policy, "on_unit_terminal", wraps=farm_policy.on_unit_terminal) as on_unit_terminal,
            patch.object(farm_policy.notification_service, "on_run_unit_stopped", new=AsyncMock()),
        ):
            count = await reconcile_stale_active_prints(printer_id)
            await drain_new_tasks(tasks_before)
        self.mocks, self.usage, self.deletes = mocks, usage, deletes
        self.on_terminal, self.on_unit_terminal = on_terminal, on_unit_terminal
        return count

    @property
    def deleted_paths(self) -> list[str]:
        return [call.args[2] for call in self.deletes.await_args_list]


@pytest.fixture
def running_job_session():
    """The RUNNING job's usage session, seeded — what the old replay popped."""
    from backend.app.services import usage_tracker

    sessions: list[int] = []

    def _seed(printer_id: int):
        session = usage_tracker.PrintSession(
            printer_id=printer_id, print_name="the running job", started_at=datetime.now(timezone.utc)
        )
        usage_tracker._active_sessions[printer_id] = session
        sessions.append(printer_id)
        return session

    yield _seed
    for printer_id in sessions:
        usage_tracker._active_sessions.pop(printer_id, None)


@pytest.mark.asyncio
class TestSuperseded:
    """(a) Archive A printing job 1 while the printer RUNS job 2 with no archive — the RC3 shape."""

    async def test_the_record_ends_unknown_and_the_running_job_is_untouched(
        self, own_session_factory, running_job_session
    ):
        from backend.app.models.print_batch import PrintBatch
        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.services.print_binding import SUPERSEDED_REASON

        maker = own_session_factory
        pid = await seed_printer(maker)
        archive_id = await seed_archive(maker, printer_id=pid, status="printing", started_at=EARLIER, subtask_id="J1")
        unit_id, run_id = await _seed_farm_unit(maker, pid, dispatch="J1", eject_profile_id=7)
        session = running_job_session(pid)

        reconnect = _Reconnect(maker, live_state(subtask_id="J2", state="RUNNING", subtask_name="Other_Job"))
        assert await reconnect.run(pid) == 1

        archive = await archive_row(maker, archive_id)
        assert (archive.status, archive.failure_reason) == ("cancelled", SUPERSEDED_REASON)
        assert archive.completed_at is not None
        assert await _log_statuses(maker, archive_id) == ["cancelled"]
        # The unit: ended cancelled / reconcile_unknown, and its run HOLDS (the operator-stop disposition).
        unit = await _row(maker, PrintQueueItem, unit_id)
        assert (unit.status, unit.stop_source) == ("cancelled", "reconcile_unknown")
        assert unit.completed_at is not None
        assert (await _row(maker, PrintBatch, run_id)).pause_reason == "operator_stop"
        reconnect.on_unit_terminal.assert_awaited_once()
        # ...and NOTHING of the printer's: no gate, no charge, no foreign page, no printer step, the
        # running job's usage session in place.
        assert plate_occupancy.is_plate_occupied(pid) is False
        reconnect.usage.assert_not_awaited()
        reconnect.mocks.ws.send_print_complete.assert_not_awaited()  # no terminal ran
        reconnect.mocks.notif.on_foreign_job_detected.assert_not_awaited()
        reconnect.on_terminal.assert_not_awaited()
        from backend.app.services import usage_tracker

        assert usage_tracker._active_sessions.get(pid) is session
        # The job's uploaded file is removed (#1542) — its storage-hash upload path first.
        assert reconnect.deleted_paths[0] == f"/{STORAGE_HASH_FILENAME}"

    async def test_the_running_jobs_own_upload_is_kept(self, own_session_factory):
        """A farm runs one file N times: the stale job and the running one share ONE upload path, and
        deleting "the stale job's file" would delete the running job's. The live job's paths are kept;
        the stale job's name fallbacks still go."""
        maker = own_session_factory
        pid = await seed_printer(maker)
        donor = await seed_archive(maker, printer_id=None, status="completed")  # the run's shared bytes
        archive_id = await seed_archive(maker, printer_id=pid, status="printing", started_at=EARLIER, subtask_id="J1")
        await _seed_farm_unit(maker, pid, dispatch="J1", archive_id=donor)
        await _seed_farm_unit(maker, pid, dispatch="J2", archive_id=donor)  # the running job

        reconnect = _Reconnect(maker, live_state(subtask_id="J2", state="RUNNING", subtask_name="Other_Job"))
        assert await reconnect.run(pid) == 1

        assert (await archive_row(maker, archive_id)).status == "cancelled"
        assert f"/{STORAGE_HASH_FILENAME}" not in reconnect.deleted_paths
        assert reconnect.deleted_paths == ["/Fast_Half_Shell.3mf", "/Fast_Half_Shell.gcode"]

    async def test_a_pending_eject_on_the_printer_is_never_touched(self, own_session_factory):
        """The reviewer's case: ``on_terminal``'s step 1 would resolve the printer's pending eject
        ``unverified`` and quarantine it after a restart. The superseded path runs ONLY the unit half."""
        from backend.app.models.printer import Printer

        maker = own_session_factory
        pid = await seed_printer(maker)
        await seed_archive(maker, printer_id=pid, status="printing", started_at=EARLIER, subtask_id="J1")
        unit_id, run_id = await _seed_farm_unit(maker, pid, dispatch="J1", eject_profile_id=7)
        plate_occupancy.hydrate_plate(pid, "J0", EscalationOnly())
        plate_occupancy.hydrate_eject(pid, PendingEject("production", run_id, 559, hydrated=True))
        before = plate_occupancy.eject_identity(pid)

        reconnect = _Reconnect(maker, live_state(subtask_id="EJECT-7", state="RUNNING"))
        assert await reconnect.run(pid) == 1

        reconnect.on_unit_terminal.assert_awaited_once()
        reconnect.on_terminal.assert_not_awaited()
        assert plate_occupancy.eject_identity(pid) == before
        assert plate_occupancy.plate_source(pid) == "J0"
        assert (await _row(maker, Printer, pid)).quarantined is False


@pytest.mark.asyncio
class TestEnded:
    """(b) The printer ENDED this job: the full terminal, bound to the archive by id — the liveness
    pair of the superseded row (the reconcile still closes what it should, both phases)."""

    async def test_finish_of_this_job_is_the_true_terminal(self, own_session_factory):
        from backend.app.models.print_queue import PrintQueueItem

        maker = own_session_factory
        pid = await seed_printer(maker)
        archive_id = await seed_archive(maker, printer_id=pid, status="printing", started_at=EARLIER, subtask_id="J1")
        unit_id, _run = await _seed_farm_unit(maker, pid, dispatch="J1", eject_profile_id=7)

        reconnect = _Reconnect(maker, live_state(subtask_id="J1", state="FINISH", progress=100.0))
        assert await reconnect.run(pid) == 1

        assert (await archive_row(maker, archive_id)).status == "completed"
        assert await _log_statuses(maker, archive_id) == ["completed"]
        assert (await _row(maker, PrintQueueItem, unit_id)).status == "completed"
        # Both phases: the plate is gated and the cooldown eject is armed for THIS unit, and the
        # terminal charges (it is the job's own FINISH).
        assert plate_occupancy.is_plate_occupied(pid) is True
        assert plate_occupancy.snapshot(pid).plate_policy == CooldownEject(unit_id=unit_id, run_id=_run)
        reconnect.usage.assert_awaited_once()
        reconnect.on_terminal.assert_awaited_once()

    async def test_idle_on_this_job_is_an_unknown_outcome_that_holds_the_run(self, own_session_factory):
        from backend.app.models.print_queue import PrintQueueItem

        maker = own_session_factory
        pid = await seed_printer(maker)
        archive_id = await seed_archive(maker, printer_id=pid, status="printing", started_at=EARLIER, subtask_id="J1")
        unit_id, _run = await _seed_farm_unit(maker, pid, dispatch="J1")

        reconnect = _Reconnect(maker, live_state(subtask_id="J1", state="IDLE", progress=0.0))
        assert await reconnect.run(pid) == 1

        assert (await archive_row(maker, archive_id)).status == "cancelled"
        unit = await _row(maker, PrintQueueItem, unit_id)
        assert (unit.status, unit.stop_source) == ("cancelled", "reconcile_unknown")
        assert plate_occupancy.is_plate_occupied(pid) is True  # fail-closed: nobody measured the deposit


@pytest.mark.asyncio
class TestEndedUnattributed:
    """(c) The printer ended a job it cannot name as this archive's: the plate is held for a human,
    SOURCE-LESS — the part there may be another job's — and the archive takes the superseded job phase."""

    @pytest.mark.parametrize("live_job", ["J9", ""], ids=["another-job", "no-job"])
    async def test_the_plate_is_held_sourceless_and_the_record_ends_unknown(self, own_session_factory, live_job):
        from backend.app.models.print_batch import PrintBatch
        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.services.print_binding import SUPERSEDED_REASON

        maker = own_session_factory
        pid = await seed_printer(maker)
        archive_id = await seed_archive(maker, printer_id=pid, status="printing", started_at=EARLIER, subtask_id="J1")
        unit_id, run_id = await _seed_farm_unit(maker, pid, dispatch="J1", eject_profile_id=7)

        reconnect = _Reconnect(maker, live_state(subtask_id=live_job, state="IDLE", progress=0.0))
        assert await reconnect.run(pid) == 1

        view = plate_occupancy.snapshot(pid)
        assert view.plate_occupied is True
        assert view.plate_source_subtask_id is None
        assert isinstance(view.plate_policy, EscalationOnly) and view.plate_policy.refusal is None
        archive = await archive_row(maker, archive_id)
        # "Superseded" is said only when the printer names ANOTHER job.
        assert (archive.status, archive.failure_reason) == ("cancelled", SUPERSEDED_REASON if live_job else None)
        unit = await _row(maker, PrintQueueItem, unit_id)
        assert (unit.status, unit.stop_source) == ("cancelled", "reconcile_unknown")
        assert (await _row(maker, PrintBatch, run_id)).pause_reason == "operator_stop"
        reconnect.usage.assert_not_awaited()
        reconnect.on_terminal.assert_not_awaited()


@pytest.mark.asyncio
class TestObserved:
    """(d) The run already recorded how it ended (the legacy leak: its terminal could not find the
    archive). The record takes the unit's outcome — whatever the printer does now."""

    @pytest.mark.parametrize(("state", "live_job"), [("IDLE", "J1"), ("RUNNING", "J2")], ids=["ended", "running-other"])
    async def test_the_record_takes_the_units_outcome_and_nothing_else_moves(
        self, own_session_factory, state, live_job
    ):
        from backend.app.models.print_queue import PrintQueueItem

        maker = own_session_factory
        pid = await seed_printer(maker)
        archive_id = await seed_archive(maker, printer_id=pid, status="printing", started_at=EARLIER, subtask_id="J1")
        ended_at = datetime(2026, 9, 24, 21, 30)
        unit_id, _run = await _seed_farm_unit(maker, pid, dispatch="J1", status="completed", completed_at=ended_at)
        before = await _row(maker, PrintQueueItem, unit_id)

        reconnect = _Reconnect(maker, live_state(subtask_id=live_job, state=state))
        assert await reconnect.run(pid) == 1

        archive = await archive_row(maker, archive_id)
        assert (archive.status, archive.completed_at) == ("completed", ended_at)
        assert await _log_statuses(maker, archive_id) == ["completed"]
        after = await _row(maker, PrintQueueItem, unit_id)
        assert (after.status, after.completed_at, after.stop_source) == (
            before.status,
            before.completed_at,
            before.stop_source,
        )
        reconnect.usage.assert_not_awaited()
        reconnect.on_terminal.assert_not_awaited()
        reconnect.on_unit_terminal.assert_not_awaited()
        assert plate_occupancy.is_plate_occupied(pid) is False
        assert reconnect.deleted_paths == []

    async def test_a_run_that_already_has_its_row_gets_no_second(self, own_session_factory):
        from backend.app.models.print_log import PrintLogEntry

        maker = own_session_factory
        pid = await seed_printer(maker)
        archive_id = await seed_archive(maker, printer_id=pid, status="printing", started_at=EARLIER, subtask_id="J1")
        await _seed_farm_unit(maker, pid, dispatch="J1", status="completed", completed_at=datetime(2026, 9, 24, 21))
        async with maker() as s:
            s.add(PrintLogEntry(archive_id=archive_id, status="completed", printer_id=pid))
            await s.commit()

        assert await _Reconnect(maker, live_state(subtask_id="J1", state="IDLE")).run(pid) == 1

        async with maker() as s:
            count = await s.scalar(select(func.count(PrintLogEntry.id)).where(PrintLogEntry.archive_id == archive_id))
        assert count == 1


@pytest.mark.asyncio
class TestNothingToDo:
    """The reconcile leaves alone what it cannot judge, and never breaks the status flow."""

    async def test_no_status_or_a_disconnected_printer_is_a_no_op(self, own_session_factory):
        maker = own_session_factory
        pid = await seed_printer(maker)
        archive_id = await seed_archive(maker, printer_id=pid, status="printing", started_at=EARLIER, subtask_id="J1")
        disconnected = live_state(subtask_id="J2", state="IDLE")
        disconnected.connected = False

        for status in (None, disconnected):
            assert await _Reconnect(maker, status).run(pid) == 0
        assert (await archive_row(maker, archive_id)).status == "printing"

    async def test_no_live_archive_is_a_no_op(self, own_session_factory):
        maker = own_session_factory
        pid = await seed_printer(maker)
        assert await _Reconnect(maker, live_state(state="IDLE")).run(pid) == 0

    @pytest.mark.parametrize(
        "state",
        [
            live_state(subtask_id="J1", state="RUNNING"),
            live_state(subtask_id="J1", state="PAUSE"),
            live_state(subtask_id="", state="RUNNING"),
            live_state(subtask_id="J2", state="IDLE", fresh=False),  # the previous session's cache
        ],
        ids=["running-same", "paused-same", "running-unknown", "stale-cache"],
    )
    async def test_a_print_that_may_still_run_is_left_alone(self, own_session_factory, state):
        maker = own_session_factory
        pid = await seed_printer(maker)
        archive_id = await seed_archive(maker, printer_id=pid, status="printing", started_at=EARLIER, subtask_id="J1")

        reconnect = _Reconnect(maker, state)
        assert await reconnect.run(pid) == 0

        assert (await archive_row(maker, archive_id)).status == "printing"
        assert plate_occupancy.is_plate_occupied(pid) is False
        reconnect.usage.assert_not_awaited()

    async def test_a_failure_does_not_propagate_and_the_record_stays_live(self, own_session_factory):
        """The connected edge is a hot path; the next session retries."""
        maker = own_session_factory
        pid = await seed_printer(maker)
        archive_id = await seed_archive(maker, printer_id=pid, status="printing", started_at=EARLIER, subtask_id="J1")

        with patch(
            "backend.app.services.job_terminal.close_superseded", new=AsyncMock(side_effect=RuntimeError("boom"))
        ):
            assert await _Reconnect(maker, live_state(subtask_id="J2", state="RUNNING")).run(pid) == 0

        assert (await archive_row(maker, archive_id)).status == "printing"
