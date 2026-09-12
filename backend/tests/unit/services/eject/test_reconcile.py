"""Startup reconcile of pending ejects missed during downtime (W1.2).

Since the 2026-08-30 cut-over every verdict acts through the plate-occupancy
authority: the reconciler decides only what became of the EJECT, and the PLATE
half of each row is the authority's rule (``drop_unowned_eject`` deliberately
leaves the plate exactly as the durable columns rebuilt it). So the post-restart
shape is seeded with ``hydrate_eject`` + ``hydrate_plate`` — no registry, no
manager gate flag — and each row is asserted against ``eject_identity`` /
``is_plate_occupied``.

The sweep itself changed shape too: it SPAWNS one task per printer and returns
the number of printers it STARTED, so a single unreachable printer can no longer
hold every other plate behind its 900 s reconnect cap.

**Since 2026-09-12 there is ONE reconciler with THREE triggers and ONE enrolment
predicate** (``plate_occupancy.unowned_eject``): startup, the printer's connected
edge, and the scheduler tick's dwell-gated sweep. Enrolment is no longer "hydrated"
but "no watchdog is going to act on this" — hydrated OR runtime-verdict-stamped —
because 001/009-H2S sat with a stamped, undeliverable kill for hours without ever
restarting, and a restart was the only thing that could enrol them.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.core import tasks as core_tasks
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services import farm_policy
from backend.app.services.eject import monitor as monitor_mod
from backend.app.services.eject.monitor import EjectCooldownMonitor, reconcile_pending_ejects_on_startup
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


async def _noop_sleep(_s):
    return None


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
    makes it ``unowned_eject`` — and that is the enrolment this wave adds.
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


@pytest.fixture()
def spawned(monkeypatch):
    """Capture the REAL tasks the sweep spawns, so a test can await them."""
    tasks: list[asyncio.Task] = []

    def _spy(coro, *, name=None):
        task = core_tasks.spawn_background_task(coro, name=name)
        tasks.append(task)
        return task

    monkeypatch.setattr(monitor_mod, "spawn_background_task", _spy)
    return tasks


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
        await monitor_mod._reconcile_one(printer.id, manager=mgr, poll_s=20, max_wait_s=100, sleep=_noop_sleep)

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
        await monitor_mod._reconcile_one(printer.id, manager=mgr, poll_s=20, max_wait_s=100, sleep=_noop_sleep)

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
        await monitor_mod._reconcile_one(printer.id, manager=mgr, poll_s=20, max_wait_s=100, sleep=_noop_sleep)

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
            await monitor_mod._reconcile_one(printer.id, manager=mgr, poll_s=20, max_wait_s=100, sleep=_noop_sleep)

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

        await monitor_mod._reconcile_one(printer.id, manager=mgr, poll_s=20, max_wait_s=100, sleep=_noop_sleep)

        assert calls == [(printer.id, None, replayed, name)]

    async def test_idle_drops_pending_keeps_gate(self, db_session, monkeypatch):
        # IDLE / unverifiable → never clear a gate on guesswork.
        _patch_session(monkeypatch, db_session)
        printer = await _mk_printer(db_session, "IDLE")
        item = await _mk_eject_item(db_session, printer_id=printer.id)
        await db_session.commit()
        _hydrate_gated_eject(printer.id, item.id)

        mgr = _RecMgr([_status("IDLE", subtask_name=f"eject_production_item{item.id}")])
        await monitor_mod._reconcile_one(printer.id, manager=mgr, poll_s=20, max_wait_s=100, sleep=_noop_sleep)

        assert plate_occupancy.eject_identity(printer.id) is None
        assert plate_occupancy.is_plate_occupied(printer.id) is True

    async def test_never_connects_drops_pending_keeps_gate(self, db_session, monkeypatch):
        _patch_session(monkeypatch, db_session)
        printer = await _mk_printer(db_session, "OFF")
        item = await _mk_eject_item(db_session, printer_id=printer.id)
        await db_session.commit()
        _hydrate_gated_eject(printer.id, item.id)

        mgr = _RecMgr([_status("IDLE", connected=False)])
        await monitor_mod._reconcile_one(printer.id, manager=mgr, poll_s=20, max_wait_s=40, sleep=_noop_sleep)

        assert plate_occupancy.eject_identity(printer.id) is None  # eject gone
        assert plate_occupancy.is_plate_occupied(printer.id) is True  # plate KEPT

    async def test_already_resolved_is_noop(self, db_session, monkeypatch):
        # A live terminal already retired the eject before the reconciler ran.
        _patch_session(monkeypatch, db_session)
        mgr = _RecMgr([_status("FINISH", subtask_name="eject_production_item1")])
        # No eject registered for printer 777 → immediate return, no crash.
        await monitor_mod._reconcile_one(777, manager=mgr, poll_s=20, max_wait_s=40, sleep=_noop_sleep)
        assert plate_occupancy.eject_identity(777) is None


class TestReconcileSweep:
    async def test_starts_one_task_per_hydrated_printer_and_returns_the_count(self, db_session, monkeypatch, spawned):
        _patch_session(monkeypatch, db_session)
        p1 = await _mk_printer(db_session, "SW1")
        i1 = await _mk_eject_item(db_session, printer_id=p1.id)
        await db_session.commit()
        _hydrate_gated_eject(p1.id, i1.id)
        _hydrate_gated_eject(999, 424242)  # will resolve as no-connect

        mgr = _RecMgr(
            per_printer={
                p1.id: [_status("RUNNING", subtask_name=f"eject_production_item{i1.id}")],
                999: [_status("IDLE", connected=False)],
            }
        )
        started = await reconcile_pending_ejects_on_startup(manager=mgr, poll_s=20, max_wait_s=0, sleep=_noop_sleep)

        assert started == 2  # the count of printers STARTED, not finished
        assert len(spawned) == 2
        await asyncio.gather(*spawned)
        assert plate_occupancy.eject_identity(p1.id) is not None  # RUNNING+match kept
        assert plate_occupancy.eject_identity(999) is None  # never reconnected → dropped

    async def test_a_live_eject_is_not_reconciled(self, db_session, monkeypatch, spawned):
        """Only HYDRATED ejects are the reconciler's business — a live dispatch owns itself."""
        _patch_session(monkeypatch, db_session)
        assert plate_occupancy.declare_occupied(5, Evidence()) is None
        assert (
            plate_occupancy.claim_for_eject(
                5, PendingEject(purpose="production", run_id=None, queue_item_id=1), Evidence()
            )
            is None
        )

        started = await reconcile_pending_ejects_on_startup(
            manager=_RecMgr(), poll_s=20, max_wait_s=0, sleep=_noop_sleep
        )

        assert started == 0
        assert spawned == []
        assert plate_occupancy.eject_identity(5) is not None  # untouched

    async def test_one_printers_failure_cannot_abort_the_others(self, db_session, monkeypatch, spawned):
        _patch_session(monkeypatch, db_session)
        p1 = await _mk_printer(db_session, "SWOK")
        i1 = await _mk_eject_item(db_session, printer_id=p1.id)
        await db_session.commit()
        _hydrate_gated_eject(p1.id, i1.id)
        _hydrate_gated_eject(998, 424243)

        mgr = _RecMgr(
            per_printer={p1.id: [_status("IDLE", subtask_name="OperatorLocalPrint")]},
            raises_for=998,
        )
        started = await reconcile_pending_ejects_on_startup(manager=mgr, poll_s=20, max_wait_s=0, sleep=_noop_sleep)

        assert started == 2
        await asyncio.gather(*spawned)  # the guard swallowed the failure — nothing escaped
        assert plate_occupancy.eject_identity(p1.id) is None  # the healthy printer still resolved
        assert plate_occupancy.eject_identity(998) is not None  # the failed one kept its record

    async def test_the_sweep_is_concurrent(self, db_session, monkeypatch, spawned):
        """A printer that never reconnects must not hold the others behind its cap.

        Serially, one disconnected printer parked every other plate for the full
        900 s reconnect wait — on the night of 2026-08-30 three printers were in
        that state at once and the operator's only way out of the third was to wait
        out the first two. The blocking sleep here belongs to the disconnected
        printer alone, so the other one finishing PROVES it never waited."""
        _patch_session(monkeypatch, db_session)
        fast = await _mk_printer(db_session, "FAST")
        slow = await _mk_printer(db_session, "SLOW")
        item = await _mk_eject_item(db_session, printer_id=fast.id)
        await db_session.commit()
        _hydrate_gated_eject(fast.id, item.id)
        _hydrate_gated_eject(slow.id, 424244)

        fast_done = asyncio.Event()

        async def blocking_sleep(_s):
            # Only the disconnected printer ever polls; hold it until the other one
            # has finished so a pass below cannot be a scheduling accident.
            await fast_done.wait()

        mgr = _RecMgr(
            per_printer={
                fast.id: [_status("IDLE", subtask_name="OperatorLocalPrint")],
                slow.id: [_status("IDLE", connected=False)],
            }
        )
        started = await reconcile_pending_ejects_on_startup(
            manager=mgr, poll_s=20, max_wait_s=900, sleep=blocking_sleep
        )
        assert started == 2

        for _ in range(10):
            await asyncio.sleep(0)  # let both tasks reach their first suspension

        assert plate_occupancy.eject_identity(fast.id) is None  # resolved while the other waits
        assert plate_occupancy.eject_identity(slow.id) is not None  # still on its reconnect poll

        fast_done.set()
        await asyncio.gather(*spawned)
        assert plate_occupancy.eject_identity(slow.id) is None  # dropped once its cap elapsed


class TestAStoppedSweepIsAlwaysRecoverable:
    """The 2026-09-12 001/009-H2S shape: an eject whose WATCHDOG already gave its
    verdict is enrolled by the same reconciler, with no restart involved.

    Before this wave the enrolment predicate was "hydrated", so the only cure for a
    watchdog whose stop went undelivered was a process restart — and 001/009 had not
    restarted: ``occupancy.eject`` read ``{production, started, age ≥ 2400 s}``,
    ``clear-plate`` answered 409 ``eject_in_flight`` six times and the operator had no
    exit. The predicate is now ``plate_occupancy.unowned_eject`` — hydrated OR
    verdict-stamped — because both mean "nothing is coming to retire this record".
    """

    async def test_a_verdict_stamped_live_eject_is_enrolled(self, db_session, monkeypatch, spawned):
        _patch_session(monkeypatch, db_session)
        _live_gated_eject_with_verdict(11, 4242)

        started = await reconcile_pending_ejects_on_startup(
            manager=_RecMgr([_status("IDLE", subtask_name="OperatorLocalPrint")]),
            poll_s=20,
            max_wait_s=0,
            sleep=_noop_sleep,
        )

        assert started == 1
        await asyncio.gather(*spawned)
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
        await monitor_mod._reconcile_one(printer.id, manager=mgr, poll_s=20, max_wait_s=0, sleep=_noop_sleep)

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

        await monitor_mod._reconcile_one(printer.id, manager=mgr, poll_s=20, max_wait_s=0, sleep=_noop_sleep)

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

        await monitor_mod._reconcile_one(printer.id, manager=mgr, poll_s=20, max_wait_s=0, sleep=_noop_sleep)

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
        await monitor_mod._reconcile_one(printer.id, manager=mgr, poll_s=20, max_wait_s=0, sleep=_noop_sleep)

        assert plate_occupancy.eject_identity(printer.id) is None
        assert plate_occupancy.is_plate_occupied(printer.id) is True

    async def test_a_live_eject_with_no_verdict_is_never_enrolled(self, db_session, monkeypatch, spawned):
        """Its watchdog is running and owns the outcome — the reconciler must not race it."""
        _patch_session(monkeypatch, db_session)
        assert plate_occupancy.declare_occupied(12, Evidence()) is None
        assert (
            plate_occupancy.claim_for_eject(
                12, PendingEject(purpose="production", run_id=None, queue_item_id=1), Evidence()
            )
            is None
        )

        started = await reconcile_pending_ejects_on_startup(
            manager=_RecMgr(), poll_s=20, max_wait_s=0, sleep=_noop_sleep
        )
        await monitor_mod.reconcile_pending_eject(12, manager=_RecMgr())

        assert started == 0
        assert spawned == []
        assert plate_occupancy.eject_identity(12) is not None  # untouched


class TestReconcilePendingEjectEntryPoint:
    """The two non-startup triggers share the startup body — no reconnect poll, because
    the caller already knows the printer is connected."""

    async def test_it_is_a_no_op_without_an_unowned_eject(self, db_session, monkeypatch):
        _patch_session(monkeypatch, db_session)
        mgr = _RecMgr([_status("IDLE")])

        await monitor_mod.reconcile_pending_eject(99, manager=mgr)  # nothing registered

        assert plate_occupancy.eject_identity(99) is None

    async def test_it_answers_from_the_live_state_without_waiting(self, db_session, monkeypatch):
        """``max_wait_s=0``: a caller on the connected edge must not sit in a 900 s
        reconnect poll — that cap exists for a restart, not for a printer that just
        said hello."""
        _patch_session(monkeypatch, db_session)
        printer = await _mk_printer(db_session, "EDGE")
        item = await _mk_eject_item(db_session, printer_id=printer.id)
        await db_session.commit()
        _live_gated_eject_with_verdict(printer.id, item.id)
        slept: list[float] = []

        async def _recording_sleep(seconds):
            slept.append(seconds)

        mgr = _RecMgr([_status("IDLE", subtask_name="OperatorLocalPrint")])
        await monitor_mod.reconcile_pending_eject(printer.id, manager=mgr, sleep=_recording_sleep)

        assert slept == []
        assert plate_occupancy.eject_identity(printer.id) is None
        assert plate_occupancy.is_plate_occupied(printer.id) is True

    async def test_one_printers_failure_cannot_escape_the_trigger(self, db_session, monkeypatch):
        """Guarded like the startup sweep: both callers spawn it beside work that must
        not be aborted by one printer's reconcile failing."""
        _patch_session(monkeypatch, db_session)
        _live_gated_eject_with_verdict(13, 424245)

        await monitor_mod.reconcile_pending_eject(13, manager=_RecMgr(raises_for=13))

        assert plate_occupancy.eject_identity(13) is not None  # kept, and nothing raised


class TestTheConnectedEdgeTrigger:
    """Trigger (b): the printer's connected edge, under the SAME ``connection_epoch``
    latch as ``reconcile_stale_active_prints``.

    This is what heals the 001/009-H2S shape without a restart — the printers came back
    at 03:17:55 reporting FINISH and nothing reconciled, because the only enrolment was
    at startup. Once per connection, never per push.
    """

    @pytest.fixture(autouse=True)
    def _main_state(self):
        from backend.app import main as main_module

        main_module._printer_reconciled_epoch.clear()
        main_module._last_status_broadcast.clear()
        yield
        main_module._printer_reconciled_epoch.clear()
        main_module._last_status_broadcast.clear()

    @staticmethod
    def _edge_state(epoch: int, layer: int):
        from backend.tests.unit.test_main_hms_pipeline import _state

        state = _state([], layer_num=layer)
        state.connection_epoch = epoch
        return state

    async def test_it_fires_once_per_connection_epoch(self, monkeypatch):
        from backend.app import main as main_module
        from backend.tests.unit.test_main_hms_pipeline import _Harness

        calls: list[int] = []

        async def _fake_reconcile(printer_id, **kwargs):
            calls.append(printer_id)

        monkeypatch.setattr(monitor_mod, "reconcile_pending_eject", _fake_reconcile)
        _live_gated_eject_with_verdict(21, 424250)

        with _Harness() as h:
            await main_module.on_printer_status_change(21, self._edge_state(1, 1))
            await main_module.on_printer_status_change(21, self._edge_state(1, 2))
            await asyncio.sleep(0)  # let the spawned task reach its first line
            assert calls == [21]  # one per CONNECTION, not per push
            assert h.spawned.count("eject-pending-reconcile-21") == 1

            # A new session (a reconnect) is a new occasion.
            await main_module.on_printer_status_change(21, self._edge_state(2, 3))
            await asyncio.sleep(0)

        assert calls == [21, 21]

    async def test_it_does_not_fire_for_a_live_eject_with_no_verdict(self, monkeypatch):
        """The predicate is the authority's, so a sweep under a running watchdog is not
        touched by a trigger that merely noticed the printer."""
        from backend.app import main as main_module
        from backend.tests.unit.test_main_hms_pipeline import _Harness

        calls: list[int] = []

        async def _fake_reconcile(printer_id, **kwargs):
            calls.append(printer_id)

        monkeypatch.setattr(monitor_mod, "reconcile_pending_eject", _fake_reconcile)
        assert plate_occupancy.declare_occupied(22, Evidence()) is None
        assert (
            plate_occupancy.claim_for_eject(
                22, PendingEject(purpose="production", run_id=None, queue_item_id=1), Evidence()
            )
            is None
        )

        with _Harness():
            await main_module.on_printer_status_change(22, self._edge_state(1, 1))
            await asyncio.sleep(0)

        assert calls == []
        assert plate_occupancy.eject_identity(22) is not None  # and the record is untouched


class TestTheTickTrigger:
    """``farm_stall`` answers the shape neither other trigger can see: the printer never
    dropped its session, so there is no connected edge and no restart — the terminal
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

        await farm_stall._reconcile_unowned_ejects(manager=self._Mgr(), now=now + farm_stall._DEAD_CLAIM_DWELL_S + 1)
        assert names == ["eject-pending-reconcile-14"]

        # Level-triggered: the record is still there on the next tick, and it must not
        # fire again for the same verdict.
        await farm_stall._reconcile_unowned_ejects(manager=self._Mgr(), now=now + farm_stall._DEAD_CLAIM_DWELL_S + 60)
        assert names == ["eject-pending-reconcile-14"]

    async def test_a_disconnected_printer_is_the_other_triggers_business(self, monkeypatch):
        from backend.app.services import farm_stall

        names = self._spawns(monkeypatch)
        _live_gated_eject_with_verdict(15, 424247)
        verdict = plate_occupancy.pending_eject_view(15).runtime_exceeded_at

        await farm_stall._reconcile_unowned_ejects(
            manager=self._Mgr(connected=False), now=verdict.timestamp() + farm_stall._DEAD_CLAIM_DWELL_S + 1
        )

        assert names == []

    async def test_a_record_with_no_verdict_is_not_a_tick_trigger(self, monkeypatch):
        """A hydrated sweep the startup reconciler left in flight has been judged by
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
        await monitor_mod._reconcile_one(printer.id, manager=mgr, poll_s=20, max_wait_s=40, sleep=_noop_sleep)

        assert plate_occupancy.eject_identity(printer.id) is None
        assert plate_occupancy.is_plate_occupied(printer.id) is True
        assert [t.name for t in watch_spawns] == [f"eject-cooldown-watch-{printer.id}"]
        assert mon._armed[printer.id].policy == CooldownEject(unit_id=item.id, run_id=None)
