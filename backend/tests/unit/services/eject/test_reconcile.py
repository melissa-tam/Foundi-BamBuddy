"""Startup reconcile of pending ejects missed during downtime (W1.2).

Since the 2026-08-30 cut-over every verdict acts through the plate-occupancy
authority: the reconciler decides only what became of the EJECT, and the PLATE
half of each row is the authority's rule (``drop_hydrated_eject`` deliberately
leaves the plate exactly as the durable columns rebuilt it). So the post-restart
shape is seeded with ``hydrate_eject`` + ``hydrate_plate`` — no registry, no
manager gate flag — and each row is asserted against ``eject_identity`` /
``is_plate_occupied``.

The sweep itself changed shape too: it SPAWNS one task per printer and returns
the number of printers it STARTED, so a single unreachable printer can no longer
hold every other plate behind its 900 s reconnect cap.
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
