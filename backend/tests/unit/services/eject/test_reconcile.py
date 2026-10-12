"""The reconcile of a pending eject whose terminal the farm never saw.

Since the 2026-08-30 cut-over every verdict acts through the plate-occupancy
authority: the reconciler decides only what became of the EJECT, and the PLATE
half of each row is the authority's rule (``drop_unowned_eject`` deliberately
leaves the plate exactly as the durable columns rebuilt it). So the post-restart
shape is seeded with ``hydrate_eject`` + ``hydrate_plate`` — no registry, no
manager gate flag — and each row is asserted against ``eject_identity`` /
``is_plate_occupied``.

**ONE reconciler, TWO triggers, ONE enrolment predicate** (``plate_occupancy.unowned_eject``):
the printer's session start (``session_start``, on its first fresh report of every MQTT
session — a restart's first included, so a hydrated eject is reconciled the moment its
printer has said what it is doing) and the scheduler tick's dwell-gated sweep. Enrolment
is "no watchdog is going to act on this" — hydrated OR runtime-verdict-stamped — because
001/009-H2S sat with a stamped, undeliverable kill for hours without ever restarting
(2026-09-12). Since 2026-10-11 there is no startup sweep polling for a reconnect and no
900 s give-up: a disconnected printer's eject waits for that printer's next session.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services import farm_policy
from backend.app.services.eject import monitor as monitor_mod
from backend.app.services.eject.monitor import EjectCooldownMonitor
from backend.app.services.plate_occupancy import (
    CooldownEject,
    EscalationOnly,
    Evidence,
    PendingEject,
    plate_occupancy,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clean_occupancy():
    """Every test starts with an empty fleet and NO injected callables."""
    plate_occupancy.reset_for_tests()
    yield
    plate_occupancy.reset_for_tests()


def _patch_session(monkeypatch, db_session):
    @contextlib.asynccontextmanager
    async def _fake_session():
        yield db_session

    monkeypatch.setattr("backend.app.core.database.async_session", _fake_session, raising=False)


async def _mk_printer(db, name, *, awaiting=True, gate="SUB-1", quarantined=False):
    p = Printer(
        name=name,
        serial_number=f"S{name}",
        ip_address="1.2.3.4",
        access_code="x",
        model="H2S",
        awaiting_plate_clear=awaiting,
        plate_gate_subtask_id=gate,
        quarantined=quarantined,
    )
    db.add(p)
    await db.flush()
    return p


async def _mk_eject_item(db, *, printer_id, dispatch_subtask="SUB-1"):
    item = PrintQueueItem(
        printer_id=printer_id,
        status="completed",
        eject_profile_id=None,
        plate_id=1,
        position=1,
        started_at=datetime.now(timezone.utc),
        dispatch_subtask_id=dispatch_subtask,
        eject_dispatched_at=datetime.now(timezone.utc),
    )
    db.add(item)
    await db.flush()
    return item


def _live_gated_eject_with_verdict(printer_id, queue_item_id, *, purpose="production", policy=None, started=True):
    """Seed the 2026-09-12 001/009-H2S shape: a LIVE eject whose watchdog already fired.

    Not a restart: the record was minted by a live dispatch, the whole-job deadline
    fired while the printer was off the wire, both ``stop_print`` sends returned False
    and the watchdog task exited. ``hydrated`` is False, so only the runtime verdict
    makes it ``unowned_eject`` — and that verdict alone enrols it.
    """
    plate_occupancy.hydrate_plate(printer_id, "SUB-1", policy or EscalationOnly())
    assert (
        plate_occupancy.claim_for_eject(
            printer_id,
            PendingEject(purpose=purpose, run_id=None, queue_item_id=queue_item_id, expected_runtime_s=83.0),
            Evidence(),
        )
        is None
    )
    if started:
        plate_occupancy.note_eject_started(printer_id)
    plate_occupancy.note_eject_runtime_exceeded(printer_id, datetime.now(timezone.utc), "total")
    identity = plate_occupancy.eject_identity(printer_id)
    assert identity is not None and identity.hydrated is False and identity.runtime_exceeded_at is not None


def _hydrate_gated_eject(printer_id, queue_item_id, *, purpose="production", policy=None):
    """Seed the post-restart shape: a hydrated pending eject on a gated plate.

    Ejects FIRST, then the plate — the order ``plate_occupancy_store.hydrate()``
    itself uses, so the plate's own notification already sees the eject."""
    plate_occupancy.hydrate_eject(printer_id, PendingEject(purpose=purpose, run_id=None, queue_item_id=queue_item_id))
    plate_occupancy.hydrate_plate(printer_id, "SUB-1", policy or EscalationOnly())


def _status(state, *, connected=True, subtask_name=None, bed=25.0):
    return SimpleNamespace(
        connected=connected, state=state, subtask_name=subtask_name, subtask_id=None, temperatures={"bed": bed}
    )


class _RecMgr:
    """Scripted manager: yields the next status per ``get_status`` call (last repeats).

    ``per_printer`` scripts each printer separately (the concurrency sweep needs one
    printer connected and another not); ``raises_for`` blows up for exactly one
    printer, so a test can prove one reconcile cannot abort another's."""

    def __init__(self, statuses=None, *, per_printer=None, raises_for=None):
        self._statuses = list(statuses or [])
        self._per_printer = {pid: list(script) for pid, script in (per_printer or {}).items()}
        self._i: dict[int, int] = {}
        self._raises_for = raises_for

    def get_status(self, pid):
        if self._raises_for is not None and pid == self._raises_for:
            raise RuntimeError("status boom")
        script = self._per_printer.get(pid, self._statuses)
        i = self._i.get(pid, 0)
        self._i[pid] = i + 1
        if not script:
            return None
        return script[i] if i < len(script) else script[-1]


class _FakeTask:
    """Inert stand-in for a watch task (see test_monitor's twin)."""

    def __init__(self, name):
        self.name = name
        self.cancelled = False
        self._done = False

    def done(self):
        return self._done

    def cancel(self):
        self.cancelled = True
        self._done = True


@pytest.fixture()
def watch_spawns(monkeypatch):
    """Record the watches the policy driver arms, without running any of them."""
    records: list[_FakeTask] = []

    def _fake_spawn(coro, *, name=None):
        coro.close()
        task = _FakeTask(name)
        records.append(task)
        return task

    monkeypatch.setattr(monitor_mod, "spawn_background_task", _fake_spawn)
    return records


class TestReconcileDecisionTable:
    """Every row acts through the authority; the plate is NEVER cleared on guesswork."""

    @pytest.mark.parametrize("live", ["RUNNING", "PAUSE"])
    async def test_active_matching_keeps_the_eject_and_stamps_its_start(self, db_session, monkeypatch, live):
        # Window (c): the sweep survived the restart and is still executing → leave
        # the pending for the normal live terminal callback, but stamp the START we
        # never observed so the eject's age reads honestly on every operator surface.
        _patch_session(monkeypatch, db_session)
        printer = await _mk_printer(db_session, f"RUN{live}")
        item = await _mk_eject_item(db_session, printer_id=printer.id)
        await db_session.commit()
        _hydrate_gated_eject(printer.id, item.id)

        mgr = _RecMgr([_status(live, subtask_name=f"eject_production_item{item.id}")])
        await monitor_mod._reconcile_one(printer.id, manager=mgr)

        identity = plate_occupancy.eject_identity(printer.id)
        assert identity is not None  # kept
        assert identity.started_at is not None  # note_eject_started stamped
        assert identity.hydrated is True  # provenance untouched by an observed start
        assert plate_occupancy.is_plate_occupied(printer.id) is True

    async def test_running_mismatch_drops_pending_keeps_gate(self, db_session, monkeypatch):
        _patch_session(monkeypatch, db_session)
        printer = await _mk_printer(db_session, "RUNX")
        item = await _mk_eject_item(db_session, printer_id=printer.id)
        await db_session.commit()
        _hydrate_gated_eject(printer.id, item.id)

        mgr = _RecMgr([_status("RUNNING", subtask_name="OperatorLocalPrint")])
        await monitor_mod._reconcile_one(printer.id, manager=mgr)

        assert plate_occupancy.eject_identity(printer.id) is None  # dropped
        assert plate_occupancy.is_plate_occupied(printer.id) is True  # gate KEPT for a human

    async def test_finish_matching_clears_the_gate(self, db_session, monkeypatch):
        # Window (d) FINISH: the eject finished during downtime → resolve exactly as
        # the live terminal would (production: the plate clears).
        _patch_session(monkeypatch, db_session)
        printer = await _mk_printer(db_session, "FIN")
        item = await _mk_eject_item(db_session, printer_id=printer.id)
        await db_session.commit()
        _hydrate_gated_eject(printer.id, item.id)

        mgr = _RecMgr([_status("FINISH", subtask_name=f"eject_production_item{item.id}")])
        await monitor_mod._reconcile_one(printer.id, manager=mgr)

        assert plate_occupancy.is_plate_occupied(printer.id) is False  # gate released
        assert plate_occupancy.eject_identity(printer.id) is None

    async def test_failed_matching_quarantines_keeps_gate(self, db_session, monkeypatch):
        _patch_session(monkeypatch, db_session)
        printer = await _mk_printer(db_session, "FAIL")
        item = await _mk_eject_item(db_session, printer_id=printer.id)
        await db_session.commit()
        _hydrate_gated_eject(printer.id, item.id)

        with (
            patch.object(farm_policy.printer_manager, "set_quarantined"),
            patch.object(farm_policy.notification_service, "on_printer_quarantined", new_callable=AsyncMock),
        ):
            mgr = _RecMgr([_status("FAILED", subtask_name=f"eject_production_item{item.id}")])
            await monitor_mod._reconcile_one(printer.id, manager=mgr)

        assert plate_occupancy.is_plate_occupied(printer.id) is True  # gate KEPT
        assert plate_occupancy.eject_identity(printer.id) is None
        await db_session.refresh(printer)
        assert printer.quarantined is True
        assert "sweep unverified" in (printer.quarantine_reason or "")

    @pytest.mark.parametrize(("live", "replayed"), [("FINISH", "completed"), ("FAILED", "failed")])
    async def test_terminal_is_replayed_with_the_echoed_identity(self, db_session, monkeypatch, live, replayed):
        """The reconciler does not re-implement the terminal — it REPLAYS it, with the
        echoed subtask id/name so ``farm_policy`` can re-run its own matching."""
        _patch_session(monkeypatch, db_session)
        printer = await _mk_printer(db_session, f"RPL{live}")
        item = await _mk_eject_item(db_session, printer_id=printer.id)
        await db_session.commit()
        _hydrate_gated_eject(printer.id, item.id)
        calls: list[tuple] = []

        async def _fake_on_terminal(db, printer_id, queue_item_id, final_status, **kwargs):
            calls.append((printer_id, queue_item_id, final_status, kwargs.get("completed_subtask_name")))

        monkeypatch.setattr(farm_policy, "on_terminal", _fake_on_terminal)
        name = f"eject_production_item{item.id}"
        mgr = _RecMgr([_status(live, subtask_name=name)])

        await monitor_mod._reconcile_one(printer.id, manager=mgr)

        assert calls == [(printer.id, None, replayed, name)]

    async def test_idle_drops_pending_keeps_gate(self, db_session, monkeypatch):
        # IDLE / unverifiable → never clear a gate on guesswork.
        _patch_session(monkeypatch, db_session)
        printer = await _mk_printer(db_session, "IDLE")
        item = await _mk_eject_item(db_session, printer_id=printer.id)
        await db_session.commit()
        _hydrate_gated_eject(printer.id, item.id)

        mgr = _RecMgr([_status("IDLE", subtask_name=f"eject_production_item{item.id}")])
        await monitor_mod._reconcile_one(printer.id, manager=mgr)

        assert plate_occupancy.eject_identity(printer.id) is None
        assert plate_occupancy.is_plate_occupied(printer.id) is True

    async def test_a_disconnected_printer_keeps_its_eject_for_its_next_session(self, db_session, monkeypatch):
        """No reconnect wait and no give-up: a printer that is not reporting has said nothing, so
        nothing is decided — the record and the gate stand until its next first fresh report."""
        _patch_session(monkeypatch, db_session)
        printer = await _mk_printer(db_session, "OFF")
        item = await _mk_eject_item(db_session, printer_id=printer.id)
        await db_session.commit()
        _hydrate_gated_eject(printer.id, item.id)

        mgr = _RecMgr([_status("IDLE", connected=False)])
        await monitor_mod._reconcile_one(printer.id, manager=mgr)

        assert plate_occupancy.eject_identity(printer.id) is not None  # eject kept
        assert plate_occupancy.is_plate_occupied(printer.id) is True  # plate kept

    async def test_already_resolved_is_noop(self, db_session, monkeypatch):
        # A live terminal already retired the eject before the reconciler ran.
        _patch_session(monkeypatch, db_session)
        mgr = _RecMgr([_status("FINISH", subtask_name="eject_production_item1")])
        # No eject registered for printer 777 → immediate return, no crash.
        await monitor_mod._reconcile_one(777, manager=mgr)
        assert plate_occupancy.eject_identity(777) is None


class TestAHydratedEjectAtItsSessionStart:
    """A restart's hydrated eject is the session start's to reconcile, per printer: each printer
    answers on its own first fresh report, so one that never comes back holds nobody else."""

    async def test_each_printer_is_reconciled_from_its_own_report(self, db_session, monkeypatch):
        _patch_session(monkeypatch, db_session)
        p1 = await _mk_printer(db_session, "SW1")
        i1 = await _mk_eject_item(db_session, printer_id=p1.id)
        await db_session.commit()
        _hydrate_gated_eject(p1.id, i1.id)
        _hydrate_gated_eject(999, 424242)  # its printer has not reported

        mgr = _RecMgr(
            per_printer={
                p1.id: [_status("FINISH", subtask_name=f"eject_production_item{i1.id}")],
                999: [_status("IDLE", connected=False)],
            }
        )
        await monitor_mod.reconcile_pending_eject(p1.id, manager=mgr)

        assert plate_occupancy.eject_identity(p1.id) is None  # FINISH+match resolved
        assert plate_occupancy.eject_identity(999) is not None  # untouched: no report, no decision

    async def test_a_live_eject_is_not_reconciled(self, db_session, monkeypatch):
        """Only an UNOWNED eject is the reconciler's business — a live dispatch owns itself."""
        _patch_session(monkeypatch, db_session)
        assert plate_occupancy.declare_occupied(5, Evidence()) is None
        assert (
            plate_occupancy.claim_for_eject(
                5, PendingEject(purpose="production", run_id=None, queue_item_id=1), Evidence()
            )
            is None
        )

        await monitor_mod.reconcile_pending_eject(5, manager=_RecMgr([_status("IDLE")]))

        assert plate_occupancy.eject_identity(5) is not None  # untouched


class TestAStoppedSweepIsAlwaysRecoverable:
    """The 2026-09-12 001/009-H2S shape: an eject whose WATCHDOG already gave its
    verdict is enrolled by the same reconciler, with no restart involved.

    With "hydrated" as the only enrolment predicate, the only cure for a
    watchdog whose stop went undelivered was a process restart — and 001/009 had not
    restarted: ``occupancy.eject`` read ``{production, started, age ≥ 2400 s}``,
    ``clear-plate`` answered 409 ``eject_in_flight`` six times and the operator had no
    exit. The predicate is now ``plate_occupancy.unowned_eject`` — hydrated OR
    verdict-stamped — because both mean "nothing is coming to retire this record".
    """

    async def test_a_verdict_stamped_live_eject_is_enrolled(self, db_session, monkeypatch):
        _patch_session(monkeypatch, db_session)
        _live_gated_eject_with_verdict(11, 4242)

        await monitor_mod.reconcile_pending_eject(
            11, manager=_RecMgr([_status("IDLE", subtask_name="OperatorLocalPrint")])
        )

        assert plate_occupancy.eject_identity(11) is None  # disposed of
        assert plate_occupancy.is_plate_occupied(11) is True  # gate KEPT for a human

    async def test_finish_and_match_replays_the_terminal_and_the_gate_stays(self, db_session, monkeypatch):
        """``farm_policy.on_terminal`` HONORS the mark: whatever the printer echoed, a
        sweep the farm had to stop is unverified, so the plate stays gated under
        EscalationOnly rather than being cleared."""
        _patch_session(monkeypatch, db_session)
        printer = await _mk_printer(db_session, "VERDFIN")
        item = await _mk_eject_item(db_session, printer_id=printer.id)
        await db_session.commit()
        _live_gated_eject_with_verdict(printer.id, item.id, policy=CooldownEject(unit_id=item.id, run_id=None))

        mgr = _RecMgr([_status("FINISH", subtask_name=f"eject_production_item{item.id}")])
        await monitor_mod._reconcile_one(printer.id, manager=mgr)

        assert plate_occupancy.eject_identity(printer.id) is None  # the terminal retired it
        assert plate_occupancy.is_plate_occupied(printer.id) is True  # ...unverified: gate kept
        assert isinstance(plate_occupancy.snapshot(printer.id).plate_policy, EscalationOnly)

    @pytest.mark.parametrize("live", ["RUNNING", "PAUSE"])
    async def test_still_sweeping_on_reconnect_redrives_the_stop(self, db_session, monkeypatch, live):
        """The printer is BACK and still sweeping after its deadline fired — the kill the
        watchdog could not deliver is re-driven on the session that just opened, under
        its own stage so the page says what actually happened."""
        _patch_session(monkeypatch, db_session)
        printer = await _mk_printer(db_session, f"VERD{live}")
        item = await _mk_eject_item(db_session, printer_id=printer.id)
        await db_session.commit()
        _live_gated_eject_with_verdict(printer.id, item.id)
        calls: list[dict] = []

        async def _fake_redrive(printer_id, *, stage, **kwargs):
            calls.append({"printer_id": printer_id, "stage": stage})
            return True

        monkeypatch.setattr(monitor_mod.eject_remote, "redrive_eject_stop", _fake_redrive)
        mgr = _RecMgr([_status(live, subtask_name=f"eject_production_item{item.id}")])

        await monitor_mod._reconcile_one(printer.id, manager=mgr)

        assert calls == [{"printer_id": printer.id, "stage": "reconnect"}]
        # The record is NOT dropped: the stopped sweep's own terminal resolves it
        # through farm_policy, which is still the one resolve_eject caller.
        assert plate_occupancy.eject_identity(printer.id) is not None
        assert plate_occupancy.is_plate_occupied(printer.id) is True

    async def test_still_sweeping_with_NO_verdict_is_left_alone(self, db_session, monkeypatch):
        """The negative of the same branch: a hydrated sweep nobody has judged is noted
        as started and left for its live terminal — never stopped."""
        _patch_session(monkeypatch, db_session)
        printer = await _mk_printer(db_session, "NOVERD")
        item = await _mk_eject_item(db_session, printer_id=printer.id)
        await db_session.commit()
        _hydrate_gated_eject(printer.id, item.id)
        calls: list[str] = []

        async def _fake_redrive(printer_id, *, stage, **kwargs):
            calls.append(stage)
            return True

        monkeypatch.setattr(monitor_mod.eject_remote, "redrive_eject_stop", _fake_redrive)
        mgr = _RecMgr([_status("RUNNING", subtask_name=f"eject_production_item{item.id}")])

        await monitor_mod._reconcile_one(printer.id, manager=mgr)

        assert calls == []
        identity = plate_occupancy.eject_identity(printer.id)
        assert identity is not None and identity.started_at is not None

    async def test_a_mismatched_job_drops_the_record_and_keeps_the_gate(self, db_session, monkeypatch):
        _patch_session(monkeypatch, db_session)
        printer = await _mk_printer(db_session, "VERDMIS")
        item = await _mk_eject_item(db_session, printer_id=printer.id)
        await db_session.commit()
        _live_gated_eject_with_verdict(printer.id, item.id)

        mgr = _RecMgr([_status("RUNNING", subtask_name="OperatorLocalPrint")])
        await monitor_mod._reconcile_one(printer.id, manager=mgr)

        assert plate_occupancy.eject_identity(printer.id) is None
        assert plate_occupancy.is_plate_occupied(printer.id) is True

    async def test_a_live_eject_with_no_verdict_is_never_enrolled(self, db_session, monkeypatch):
        """Its watchdog is running and owns the outcome — the reconciler must not race it."""
        _patch_session(monkeypatch, db_session)
        assert plate_occupancy.declare_occupied(12, Evidence()) is None
        assert (
            plate_occupancy.claim_for_eject(
                12, PendingEject(purpose="production", run_id=None, queue_item_id=1), Evidence()
            )
            is None
        )

        await monitor_mod.reconcile_pending_eject(12, manager=_RecMgr())

        assert plate_occupancy.eject_identity(12) is not None  # untouched


class TestReconcilePendingEjectEntryPoint:
    """Both triggers share one body, answered from the state already there: each caller knows
    the printer is connected."""

    async def test_it_is_a_no_op_without_an_unowned_eject(self, db_session, monkeypatch):
        _patch_session(monkeypatch, db_session)
        mgr = _RecMgr([_status("IDLE")])

        await monitor_mod.reconcile_pending_eject(99, manager=mgr)  # nothing registered

        assert plate_occupancy.eject_identity(99) is None

    async def test_it_answers_from_the_live_state(self, db_session, monkeypatch):
        """A printer that just said hello is answered from what it reports — there is no poll."""
        _patch_session(monkeypatch, db_session)
        printer = await _mk_printer(db_session, "EDGE")
        item = await _mk_eject_item(db_session, printer_id=printer.id)
        await db_session.commit()
        _live_gated_eject_with_verdict(printer.id, item.id)

        mgr = _RecMgr([_status("IDLE", subtask_name="OperatorLocalPrint")])
        await monitor_mod.reconcile_pending_eject(printer.id, manager=mgr)

        assert plate_occupancy.eject_identity(printer.id) is None
        assert plate_occupancy.is_plate_occupied(printer.id) is True

    async def test_one_printers_failure_cannot_escape_the_trigger(self, db_session, monkeypatch):
        """Guarded: both callers run it beside work that must not be aborted by one printer's
        reconcile failing (the session start's other lanes, the tick's other printers)."""
        _patch_session(monkeypatch, db_session)
        _live_gated_eject_with_verdict(13, 424245)

        await monitor_mod.reconcile_pending_eject(13, manager=_RecMgr(raises_for=13))

        assert plate_occupancy.eject_identity(13) is not None  # kept, and nothing raised


class TestTheSessionStartTrigger:
    """Trigger (a): the printer's first fresh report of every MQTT session
    (``printer_events.FirstFreshReport`` → ``session_start``).

    This is what heals the 001/009-H2S shape without a restart — the printers came back at
    03:17:55 reporting FINISH and nothing reconciled, because the only enrolment was at startup.
    Once per session, never per push."""

    @staticmethod
    async def _session(printer_id: int, epoch: int) -> None:
        from backend.app.services.printer_events import FirstFreshReport
        from backend.app.services.session_start import SessionStart

        session = SessionStart(downtime_reconcile=AsyncMock(return_value=0))
        with (
            patch("backend.app.services.spool_recovery.reevaluate_session", new=AsyncMock(return_value=0)),
            patch("backend.app.services.pause_recovery.rearm_decision_deadlines", new=AsyncMock(return_value=0)),
            patch("backend.app.services.pause_recovery.note_session_start"),
        ):
            session.on_first_fresh_report(FirstFreshReport(printer_id=printer_id, connection_epoch=epoch))
            await session._sessions[printer_id]  # noqa: SLF001 — the session's own task, to await it

    async def test_it_fires_once_per_session(self, monkeypatch):
        calls: list[int] = []

        async def _fake_reconcile(printer_id, **kwargs):
            calls.append(printer_id)

        monkeypatch.setattr(monitor_mod, "reconcile_pending_eject", _fake_reconcile)
        _live_gated_eject_with_verdict(21, 424250)

        await self._session(21, 1)
        assert calls == [21]

        # A new session (a reconnect) is a new occasion.
        await self._session(21, 2)
        assert calls == [21, 21]

    async def test_it_does_not_reach_a_live_eject_with_no_verdict(self, db_session, monkeypatch):
        """The predicate is the authority's, so a sweep under a running watchdog is not touched by
        a session start that merely noticed the printer."""
        _patch_session(monkeypatch, db_session)
        assert plate_occupancy.declare_occupied(22, Evidence()) is None
        assert (
            plate_occupancy.claim_for_eject(
                22, PendingEject(purpose="production", run_id=None, queue_item_id=1), Evidence()
            )
            is None
        )

        await self._session(22, 1)

        assert plate_occupancy.eject_identity(22) is not None  # the record is untouched


class TestTheTickTrigger:
    """``farm_stall`` answers the shape the session start cannot see: the printer never
    dropped its session, so there is no first fresh report and no restart — the terminal
    echo simply never arrived."""

    @pytest.fixture(autouse=True)
    def _stall_state(self):
        from backend.app.services import farm_stall

        farm_stall._reset_state()
        yield
        farm_stall._reset_state()

    class _Mgr:
        def __init__(self, connected=True):
            self._connected = connected

        def is_connected(self, _pid):
            return self._connected

    @staticmethod
    def _spawns(monkeypatch):
        """Record the tasks the trigger spawns without running them.

        Patched on ``core.tasks`` because ``farm_stall`` imports the helper inside the
        function body (the module's convention for a lazily-reached service), so the
        name is resolved at call time."""
        names: list[str] = []

        def _fake_spawn(coro, *, name=None):
            coro.close()
            names.append(name or "")
            return None

        monkeypatch.setattr("backend.app.core.tasks.spawn_background_task", _fake_spawn)
        return names

    async def test_it_waits_out_the_dwell_then_fires_once(self, monkeypatch):
        """The dwell keeps it clear of the LIVE terminal handler, which is the first
        responder to a stopped sweep; and the trigger is level-shaped, so without the
        per-verdict latch it would re-fire (and re-page) every tick."""
        from backend.app.services import farm_stall

        names = self._spawns(monkeypatch)
        _live_gated_eject_with_verdict(14, 424246)
        verdict = plate_occupancy.pending_eject_view(14).runtime_exceeded_at
        now = verdict.timestamp()

        await farm_stall._reconcile_unowned_ejects(manager=self._Mgr(), now=now + 1.0)
        assert names == []  # inside the dwell

        await farm_stall._reconcile_unowned_ejects(manager=self._Mgr(), now=now + farm_stall._UNOWNED_EJECT_DWELL_S + 1)
        assert names == ["eject-pending-reconcile-14"]

        # Level-triggered: the record is still there on the next tick, and it must not
        # fire again for the same verdict.
        await farm_stall._reconcile_unowned_ejects(
            manager=self._Mgr(), now=now + farm_stall._UNOWNED_EJECT_DWELL_S + 60
        )
        assert names == ["eject-pending-reconcile-14"]

    async def test_a_disconnected_printer_is_the_other_triggers_business(self, monkeypatch):
        from backend.app.services import farm_stall

        names = self._spawns(monkeypatch)
        _live_gated_eject_with_verdict(15, 424247)
        verdict = plate_occupancy.pending_eject_view(15).runtime_exceeded_at

        await farm_stall._reconcile_unowned_ejects(
            manager=self._Mgr(connected=False), now=verdict.timestamp() + farm_stall._UNOWNED_EJECT_DWELL_S + 1
        )

        assert names == []

    async def test_a_record_with_no_verdict_is_not_a_tick_trigger(self, monkeypatch):
        """A hydrated sweep its session start left in flight has been judged by
        nobody — there is nothing to conclude from a tick."""
        from backend.app.services import farm_stall

        names = self._spawns(monkeypatch)
        _hydrate_gated_eject(16, 424248)

        await farm_stall._reconcile_unowned_ejects(manager=self._Mgr(), now=1_000_000.0)

        assert names == []


class TestReconcileAndThePolicyDriver:
    """A hydrated-eject printer carries NO plate watch while the reconciler owns it
    (a cooldown watch armed beside a possibly-still-running sweep is the double
    dispatch the legacy re-arm avoided by skipping such printers) — and the
    reconciler's disposal is what hands the plate back to its own policy."""

    def _wire(self, monitor):
        plate_occupancy.configure(policy_driver=monitor.on_occupancy_change)

    async def test_hydrated_eject_suppresses_the_plate_watch(self, watch_spawns):
        mon = EjectCooldownMonitor()
        self._wire(mon)

        _hydrate_gated_eject(1, 42, policy=CooldownEject(unit_id=42, run_id=None))

        assert watch_spawns == []  # eject-first hydration never arms anything
        assert mon._armed == {}

    async def test_a_plate_armed_before_the_eject_hydrates_is_cancelled(self, watch_spawns):
        """The invariant holds in either hydration order, not just the store's."""
        mon = EjectCooldownMonitor()
        self._wire(mon)

        plate_occupancy.hydrate_plate(1, "SUB-1", CooldownEject(unit_id=42, run_id=None))
        armed = mon._armed[1].task
        plate_occupancy.hydrate_eject(1, PendingEject(purpose="production", run_id=None, queue_item_id=42))

        assert armed.cancelled is True
        assert mon._armed == {}

    async def test_dropping_the_hydrated_eject_re_arms_the_plates_policy(self, db_session, monkeypatch, watch_spawns):
        """Liveness: the printer must not be left gated AND watch-less afterwards."""
        _patch_session(monkeypatch, db_session)
        printer = await _mk_printer(db_session, "REARM")
        item = await _mk_eject_item(db_session, printer_id=printer.id)
        await db_session.commit()
        _hydrate_gated_eject(printer.id, item.id, policy=CooldownEject(unit_id=item.id, run_id=None))
        mon = EjectCooldownMonitor()
        self._wire(mon)

        mgr = _RecMgr([_status("IDLE", subtask_name="OperatorLocalPrint")])
        await monitor_mod._reconcile_one(printer.id, manager=mgr)

        assert plate_occupancy.eject_identity(printer.id) is None
        assert plate_occupancy.is_plate_occupied(printer.id) is True
        assert [t.name for t in watch_spawns] == [f"eject-cooldown-watch-{printer.id}"]
        assert mon._armed[printer.id].policy == CooldownEject(unit_id=item.id, run_id=None)
