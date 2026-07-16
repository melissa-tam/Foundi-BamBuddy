"""Startup reconcile of pending ejects missed during downtime (W1.2)."""

import contextlib
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services import farm_policy
from backend.app.services.eject import monitor as monitor_mod, remote
from backend.app.services.eject.monitor import EjectCooldownMonitor, reconcile_pending_ejects_on_startup

pytestmark = pytest.mark.asyncio


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


def _status(state, *, connected=True, subtask_name=None, bed=25.0):
    return SimpleNamespace(
        connected=connected, state=state, subtask_name=subtask_name, subtask_id=None, temperatures={"bed": bed}
    )


class _RecMgr:
    """Scripted manager: yields the next status per get_status call (last repeats)."""

    def __init__(self, statuses):
        self._statuses = list(statuses)
        self._i = 0

    def get_status(self, _pid):
        if self._i < len(self._statuses):
            s = self._statuses[self._i]
        else:
            s = self._statuses[-1] if self._statuses else None
        self._i += 1
        return s


class TestReconcileDecisionTable:
    async def test_running_matching_leaves_pending(self, db_session, monkeypatch):
        # Window (c): eject still RUNNING post-restart with a matching name → leave
        # the pending for the normal live terminal callback.
        _patch_session(monkeypatch, db_session)
        printer = await _mk_printer(db_session, "RUN")
        item = await _mk_eject_item(db_session, printer_id=printer.id)
        await db_session.commit()
        remote.register_pending_eject(printer.id, remote.PendingEject("production", None, item.id))
        try:
            mgr = _RecMgr([_status("RUNNING", subtask_name=f"eject_production_item{item.id}")])
            await monitor_mod._reconcile_one(printer.id, manager=mgr, poll_s=20, max_wait_s=100, sleep=_noop_sleep)
            assert remote.peek_pending_eject(printer.id) is not None  # kept
        finally:
            remote.pop_pending_eject(printer.id)

    async def test_running_mismatch_drops_pending_keeps_gate(self, db_session, monkeypatch):
        _patch_session(monkeypatch, db_session)
        printer = await _mk_printer(db_session, "RUNX")
        item = await _mk_eject_item(db_session, printer_id=printer.id)
        await db_session.commit()
        remote.register_pending_eject(printer.id, remote.PendingEject("production", None, item.id))
        try:
            mgr = _RecMgr([_status("RUNNING", subtask_name="OperatorLocalPrint")])
            await monitor_mod._reconcile_one(printer.id, manager=mgr, poll_s=20, max_wait_s=100, sleep=_noop_sleep)
            assert remote.peek_pending_eject(printer.id) is None  # dropped
            await db_session.refresh(item)
            assert item.eject_dispatched_at is None  # stamp cleared
        finally:
            remote.pop_pending_eject(printer.id)

    async def test_finish_matching_clears_gate(self, db_session, monkeypatch):
        # Window (d) FINISH: eject finished during downtime → production gate clears.
        _patch_session(monkeypatch, db_session)
        printer = await _mk_printer(db_session, "FIN")
        item = await _mk_eject_item(db_session, printer_id=printer.id)
        await db_session.commit()
        remote.register_pending_eject(printer.id, remote.PendingEject("production", None, item.id))
        cleared = []
        try:
            with patch.object(
                farm_policy.printer_manager,
                "set_awaiting_plate_clear",
                side_effect=lambda pid, v, **kw: cleared.append((pid, v)),
            ):
                mgr = _RecMgr([_status("FINISH", subtask_name=f"eject_production_item{item.id}")])
                await monitor_mod._reconcile_one(printer.id, manager=mgr, poll_s=20, max_wait_s=100, sleep=_noop_sleep)
            assert cleared == [(printer.id, False)]  # gate released
            assert remote.peek_pending_eject(printer.id) is None
            await db_session.refresh(item)
            assert item.eject_dispatched_at is None
        finally:
            remote.pop_pending_eject(printer.id)

    async def test_failed_matching_quarantines_keeps_gate(self, db_session, monkeypatch):
        _patch_session(monkeypatch, db_session)
        printer = await _mk_printer(db_session, "FAIL")
        item = await _mk_eject_item(db_session, printer_id=printer.id)
        await db_session.commit()
        remote.register_pending_eject(printer.id, remote.PendingEject("production", None, item.id))
        cleared = []
        try:
            with (
                patch.object(
                    farm_policy.printer_manager,
                    "set_awaiting_plate_clear",
                    side_effect=lambda pid, v, **kw: cleared.append((pid, v)),
                ),
                patch.object(farm_policy.printer_manager, "set_quarantined"),
                patch.object(farm_policy.notification_service, "on_printer_quarantined", new_callable=AsyncMock),
            ):
                mgr = _RecMgr([_status("FAILED", subtask_name=f"eject_production_item{item.id}")])
                await monitor_mod._reconcile_one(printer.id, manager=mgr, poll_s=20, max_wait_s=100, sleep=_noop_sleep)
            assert cleared == []  # gate KEPT
            assert remote.peek_pending_eject(printer.id) is None
            await db_session.refresh(printer)
            assert printer.quarantined is True
            assert "sweep unverified" in (printer.quarantine_reason or "")
        finally:
            remote.pop_pending_eject(printer.id)

    async def test_idle_drops_pending_keeps_gate(self, db_session, monkeypatch):
        # IDLE / unverifiable → never clear a gate on guesswork.
        _patch_session(monkeypatch, db_session)
        printer = await _mk_printer(db_session, "IDLE")
        item = await _mk_eject_item(db_session, printer_id=printer.id)
        await db_session.commit()
        remote.register_pending_eject(printer.id, remote.PendingEject("production", None, item.id))
        try:
            mgr = _RecMgr([_status("IDLE", subtask_name=f"eject_production_item{item.id}")])
            await monitor_mod._reconcile_one(printer.id, manager=mgr, poll_s=20, max_wait_s=100, sleep=_noop_sleep)
            assert remote.peek_pending_eject(printer.id) is None
            await db_session.refresh(item)
            assert item.eject_dispatched_at is None
        finally:
            remote.pop_pending_eject(printer.id)

    async def test_never_connects_drops_pending(self, db_session, monkeypatch):
        _patch_session(monkeypatch, db_session)
        printer = await _mk_printer(db_session, "OFF")
        item = await _mk_eject_item(db_session, printer_id=printer.id)
        await db_session.commit()
        remote.register_pending_eject(printer.id, remote.PendingEject("production", None, item.id))
        try:
            mgr = _RecMgr([_status("IDLE", connected=False)])
            await monitor_mod._reconcile_one(printer.id, manager=mgr, poll_s=20, max_wait_s=40, sleep=_noop_sleep)
            assert remote.peek_pending_eject(printer.id) is None
            await db_session.refresh(item)
            assert item.eject_dispatched_at is None
        finally:
            remote.pop_pending_eject(printer.id)

    async def test_already_resolved_is_noop(self, db_session, monkeypatch):
        # A live terminal already popped the registry before the reconciler ran.
        _patch_session(monkeypatch, db_session)
        mgr = _RecMgr([_status("FINISH", subtask_name="eject_production_item1")])
        # No pending registered for printer 777 → immediate return, no crash.
        await monitor_mod._reconcile_one(777, manager=mgr, poll_s=20, max_wait_s=40, sleep=_noop_sleep)


class TestReconcileSweep:
    async def test_sweep_processes_all_and_survives_one_failure(self, db_session, monkeypatch):
        _patch_session(monkeypatch, db_session)
        p1 = await _mk_printer(db_session, "SW1")
        i1 = await _mk_eject_item(db_session, printer_id=p1.id)
        await db_session.commit()
        remote.register_pending_eject(p1.id, remote.PendingEject("production", None, i1.id))
        remote.register_pending_eject(999, remote.PendingEject("production", None, 424242))  # will resolve as no-connect
        try:
            mgr = _RecMgr([_status("RUNNING", subtask_name=f"eject_production_item{i1.id}")])
            processed = await reconcile_pending_ejects_on_startup(
                manager=mgr, poll_s=20, max_wait_s=0, sleep=_noop_sleep
            )
            assert processed == 2  # both printers processed (p1 left pending, 999 dropped)
            assert remote.peek_pending_eject(p1.id) is not None  # RUNNING+match kept
        finally:
            remote.pop_pending_eject(p1.id)
            remote.pop_pending_eject(999)


class TestRearmSkipsHydratedPending:
    """rearm_on_startup must skip a printer whose eject is hydrated in-flight —
    the reconciler owns it (no double dispatch / false 3-failure quarantine)."""

    @staticmethod
    def _patch(monkeypatch, db_session):
        @contextlib.asynccontextmanager
        async def _fake_session():
            yield db_session

        monkeypatch.setattr("backend.app.core.database.async_session", _fake_session, raising=False)
        mon = EjectCooldownMonitor()
        started = []
        monkeypatch.setattr(mon, "_start_watch", lambda pid, qid: started.append((pid, qid)) or True)
        return mon, started

    async def test_rearm_skips_when_pending_hydrated(self, db_session, monkeypatch):
        printer = await _mk_printer(db_session, "RS1", awaiting=True, gate="SUB-1")
        item = PrintQueueItem(
            printer_id=printer.id,
            status="completed",
            eject_profile_id=42,
            plate_id=1,
            position=1,
            started_at=datetime.now(timezone.utc),
            dispatch_subtask_id="SUB-1",
        )
        db_session.add(item)
        await db_session.commit()
        mon, started = self._patch(monkeypatch, db_session)
        remote.register_pending_eject(printer.id, remote.PendingEject("production", None, item.id))
        try:
            rearmed = await mon.rearm_on_startup()
            assert rearmed == 0
            assert started == []  # skipped — reconciler owns this printer
        finally:
            remote.pop_pending_eject(printer.id)

    async def test_rearm_proceeds_without_pending(self, db_session, monkeypatch):
        # Sanity: same setup WITHOUT a hydrated pending re-arms as normal.
        printer = await _mk_printer(db_session, "RS2", awaiting=True, gate="SUB-1")
        item = PrintQueueItem(
            printer_id=printer.id,
            status="completed",
            eject_profile_id=42,
            plate_id=1,
            position=1,
            started_at=datetime.now(timezone.utc),
            dispatch_subtask_id="SUB-1",
        )
        db_session.add(item)
        await db_session.commit()
        mon, started = self._patch(monkeypatch, db_session)
        rearmed = await mon.rearm_on_startup()
        assert rearmed == 1
        assert started == [(printer.id, item.id)]
