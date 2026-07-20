"""Offline-stall watch tests (Phase 3.2).

``check_stalled_prints`` flags a farm unit still ``printing`` whose printer has
been offline past the grace window — a one-shot ``on_print_stalled`` notification
plus ``waiting_reason="printer_offline_stalled"`` — and NEVER writes a terminal
status. Reconnect clears the flag. Clock + manager are injected. FK enforcement is
off in the test engine.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.models.print_queue import PrintQueueItem
from backend.app.services import farm_stall
from backend.app.services.notification_service import notification_service

pytestmark = pytest.mark.asyncio

_GRACE_S = 30 * 60  # default farm_offline_stall_minutes = 30
_PAUSE_GRACE_S = 15 * 60  # default farm_pause_stall_minutes = 15


class _FakeState:
    """Minimal live-status stand-in — the pause watch reads only ``.state``."""

    def __init__(self, state: str | None):
        self.state = state


class _FakeManager:
    def __init__(self, connected: dict[int, bool], states: dict[int, _FakeState] | None = None):
        self._connected = connected
        self._states = states or {}

    def is_connected(self, pid: int) -> bool:
        return self._connected.get(pid, False)

    def get_status(self, pid: int):
        return self._states.get(pid)


@pytest.fixture(autouse=True)
def _clean_state():
    farm_stall._reset_state()
    yield
    farm_stall._reset_state()


async def _add_printing(db, printer_id, pos=1):
    it = PrintQueueItem(
        printer_id=printer_id,
        status="printing",
        first_article=False,
        plate_id=1,
        position=pos,
        started_at=datetime.now(timezone.utc),
    )
    db.add(it)
    await db.commit()
    await db.refresh(it)
    return it


class TestOfflineStallWatch:
    async def test_no_flag_before_grace(self, db_session):
        item = await _add_printing(db_session, 5)
        mgr = _FakeManager({5: False})
        with patch.object(notification_service, "on_print_stalled", new_callable=AsyncMock) as mock_n:
            # First observation records first-offline; still inside grace on the next tick.
            await farm_stall.check_stalled_prints(db_session, manager=mgr, now=1000.0)
            await farm_stall.check_stalled_prints(db_session, manager=mgr, now=1000.0 + _GRACE_S - 5)
            mock_n.assert_not_awaited()
        await db_session.refresh(item)
        assert item.waiting_reason is None
        assert item.status == "printing"  # never terminal

    async def test_flag_and_notify_after_grace(self, db_session):
        item = await _add_printing(db_session, 5)
        mgr = _FakeManager({5: False})
        with patch.object(notification_service, "on_print_stalled", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_stalled_prints(db_session, manager=mgr, now=1000.0)
            await farm_stall.check_stalled_prints(db_session, manager=mgr, now=1000.0 + _GRACE_S + 1)
            mock_n.assert_awaited_once()
        await db_session.refresh(item)
        assert item.waiting_reason == "printer_offline_stalled"
        assert item.status == "printing"  # STILL printing — never fabricate a terminal

    async def test_single_fire_per_incident(self, db_session):
        await _add_printing(db_session, 5)
        mgr = _FakeManager({5: False})
        with patch.object(notification_service, "on_print_stalled", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_stalled_prints(db_session, manager=mgr, now=1000.0)
            await farm_stall.check_stalled_prints(db_session, manager=mgr, now=1000.0 + _GRACE_S + 1)
            await farm_stall.check_stalled_prints(db_session, manager=mgr, now=1000.0 + _GRACE_S + 500)
            assert mock_n.await_count == 1  # dedup: not re-fired while still stalled

    async def test_reconnect_clears_waiting_reason(self, db_session):
        item = await _add_printing(db_session, 5)
        offline = _FakeManager({5: False})
        with patch.object(notification_service, "on_print_stalled", new_callable=AsyncMock):
            await farm_stall.check_stalled_prints(db_session, manager=offline, now=1000.0)
            await farm_stall.check_stalled_prints(db_session, manager=offline, now=1000.0 + _GRACE_S + 1)
        await db_session.refresh(item)
        assert item.waiting_reason == "printer_offline_stalled"

        # Printer comes back → the stall flag is cleared (reconcile owns the outcome).
        online = _FakeManager({5: True})
        await farm_stall.check_stalled_prints(db_session, manager=online, now=1000.0 + _GRACE_S + 100)
        await db_session.refresh(item)
        assert item.waiting_reason is None
        assert item.status == "printing"

    async def test_reconnect_rearms_incident(self, db_session):
        # After a reconnect clears state, a fresh offline stint fires again.
        await _add_printing(db_session, 5)
        with patch.object(notification_service, "on_print_stalled", new_callable=AsyncMock) as mock_n:
            off = _FakeManager({5: False})
            await farm_stall.check_stalled_prints(db_session, manager=off, now=0.0)
            await farm_stall.check_stalled_prints(db_session, manager=off, now=_GRACE_S + 1)
            on = _FakeManager({5: True})
            await farm_stall.check_stalled_prints(db_session, manager=on, now=_GRACE_S + 2)
            # New offline incident.
            await farm_stall.check_stalled_prints(db_session, manager=off, now=_GRACE_S + 3)
            await farm_stall.check_stalled_prints(db_session, manager=off, now=2 * _GRACE_S + 10)
            assert mock_n.await_count == 2

    async def test_connected_printer_never_flags(self, db_session):
        item = await _add_printing(db_session, 5)
        mgr = _FakeManager({5: True})
        with patch.object(notification_service, "on_print_stalled", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_stalled_prints(db_session, manager=mgr, now=1000.0)
            await farm_stall.check_stalled_prints(db_session, manager=mgr, now=1000.0 + _GRACE_S + 100)
            mock_n.assert_not_awaited()
        await db_session.refresh(item)
        assert item.waiting_reason is None


class TestPauseStallWatch:
    async def test_no_flag_before_grace(self, db_session):
        item = await _add_printing(db_session, 7)
        mgr = _FakeManager({7: True}, {7: _FakeState("PAUSE")})
        with patch.object(notification_service, "on_print_paused_stalled", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_paused_prints(db_session, manager=mgr, now=1000.0)
            await farm_stall.check_paused_prints(db_session, manager=mgr, now=1000.0 + _PAUSE_GRACE_S - 5)
            mock_n.assert_not_awaited()
        await db_session.refresh(item)
        assert item.waiting_reason is None
        assert item.status == "printing"  # never terminal

    async def test_flag_and_notify_after_grace(self, db_session):
        item = await _add_printing(db_session, 7)
        mgr = _FakeManager({7: True}, {7: _FakeState("PAUSE")})
        with patch.object(notification_service, "on_print_paused_stalled", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_paused_prints(db_session, manager=mgr, now=1000.0)
            await farm_stall.check_paused_prints(db_session, manager=mgr, now=1000.0 + _PAUSE_GRACE_S + 1)
            mock_n.assert_awaited_once()
        await db_session.refresh(item)
        assert item.waiting_reason == "print_paused_stalled"
        assert item.status == "printing"  # STILL printing — never fabricate a terminal

    async def test_single_fire_per_incident(self, db_session):
        await _add_printing(db_session, 7)
        mgr = _FakeManager({7: True}, {7: _FakeState("PAUSE")})
        with patch.object(notification_service, "on_print_paused_stalled", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_paused_prints(db_session, manager=mgr, now=1000.0)
            await farm_stall.check_paused_prints(db_session, manager=mgr, now=1000.0 + _PAUSE_GRACE_S + 1)
            await farm_stall.check_paused_prints(db_session, manager=mgr, now=1000.0 + _PAUSE_GRACE_S + 500)
            assert mock_n.await_count == 1  # dedup while still paused

    async def test_resume_clears_and_rearms(self, db_session):
        item = await _add_printing(db_session, 7)
        paused = _FakeManager({7: True}, {7: _FakeState("PAUSE")})
        with patch.object(notification_service, "on_print_paused_stalled", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_paused_prints(db_session, manager=paused, now=0.0)
            await farm_stall.check_paused_prints(db_session, manager=paused, now=_PAUSE_GRACE_S + 1)
            await db_session.refresh(item)
            assert item.waiting_reason == "print_paused_stalled"

            # Resume: live state leaves PAUSE → the stale pause flag clears.
            running = _FakeManager({7: True}, {7: _FakeState("RUNNING")})
            await farm_stall.check_paused_prints(db_session, manager=running, now=_PAUSE_GRACE_S + 2)
            await db_session.refresh(item)
            assert item.waiting_reason is None
            assert item.status == "printing"

            # A second pause re-arms and fires again.
            await farm_stall.check_paused_prints(db_session, manager=paused, now=_PAUSE_GRACE_S + 3)
            await farm_stall.check_paused_prints(db_session, manager=paused, now=2 * _PAUSE_GRACE_S + 10)
            assert mock_n.await_count == 2

    async def test_skips_vision_gate_reason(self, db_session):
        item = await _add_printing(db_session, 7)
        item.waiting_reason = "plate_not_empty_printer_detected"
        await db_session.commit()
        mgr = _FakeManager({7: True}, {7: _FakeState("PAUSE")})
        with patch.object(notification_service, "on_print_paused_stalled", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_paused_prints(db_session, manager=mgr, now=0.0)
            await farm_stall.check_paused_prints(db_session, manager=mgr, now=_PAUSE_GRACE_S + 100)
            mock_n.assert_not_awaited()
        await db_session.refresh(item)
        assert item.waiting_reason == "plate_not_empty_printer_detected"  # untouched

    async def test_failed_reason_still_muted(self, db_session):
        """A ``spool_jam_recovery_failed`` pause stays muted — escalation already
        fired its one-shot notification and left the printer PAUSED for a human."""
        item = await _add_printing(db_session, 8)
        item.waiting_reason = "spool_jam_recovery_failed"
        await db_session.commit()
        mgr = _FakeManager({8: True}, {8: _FakeState("PAUSE")})
        with patch.object(notification_service, "on_print_paused_stalled", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_paused_prints(db_session, manager=mgr, now=0.0)
            await farm_stall.check_paused_prints(db_session, manager=mgr, now=_PAUSE_GRACE_S + 100)
            mock_n.assert_not_awaited()
        await db_session.refresh(item)
        assert item.waiting_reason == "spool_jam_recovery_failed"  # untouched

    async def test_runout_reason_still_muted(self, db_session):
        """A ``filament_runout_recovery_failed`` pause stays muted — the runout
        escalation already fired its one-shot notification and left the printer
        PAUSED for a same-slot refill; re-notifying would double up."""
        item = await _add_printing(db_session, 9)
        item.waiting_reason = "filament_runout_recovery_failed"
        await db_session.commit()
        mgr = _FakeManager({9: True}, {9: _FakeState("PAUSE")})
        with patch.object(notification_service, "on_print_paused_stalled", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_paused_prints(db_session, manager=mgr, now=0.0)
            await farm_stall.check_paused_prints(db_session, manager=mgr, now=_PAUSE_GRACE_S + 100)
            mock_n.assert_not_awaited()
        await db_session.refresh(item)
        assert item.waiting_reason == "filament_runout_recovery_failed"  # untouched

    async def test_recovering_with_live_task_owned(self, db_session, monkeypatch):
        """A ``spool_jam_recovering`` pause backed by a LIVE recovery task is owned —
        no flag, and the token is left for the recovery driver."""
        from backend.app.services import spool_recovery

        class _Live:
            def done(self) -> bool:
                return False

        item = await _add_printing(db_session, 7)
        item.waiting_reason = "spool_jam_recovering"
        await db_session.commit()
        monkeypatch.setitem(spool_recovery._active_tasks, 7, _Live())
        mgr = _FakeManager({7: True}, {7: _FakeState("PAUSE")})
        with patch.object(notification_service, "on_print_paused_stalled", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_paused_prints(db_session, manager=mgr, now=0.0)
            await farm_stall.check_paused_prints(db_session, manager=mgr, now=_PAUSE_GRACE_S + 100)
            mock_n.assert_not_awaited()
        await db_session.refresh(item)
        assert item.waiting_reason == "spool_jam_recovering"  # owned by the live task → untouched

    async def test_orphaned_recovering_no_task_cleared_and_flagged(self, db_session):
        """R1: a ``spool_jam_recovering`` token with NO live recovery task (a restart/
        crash orphan) is cleared, the grace timer starts, and the unattended-pause
        notification fires after grace — the exact indefinite-stall this closes."""
        item = await _add_printing(db_session, 7)
        item.waiting_reason = "spool_jam_recovering"
        await db_session.commit()
        mgr = _FakeManager({7: True}, {7: _FakeState("PAUSE")})
        with patch.object(notification_service, "on_print_paused_stalled", new_callable=AsyncMock) as mock_n:
            # First tick: orphan reclaimed (token cleared) + grace timer starts.
            await farm_stall.check_paused_prints(db_session, manager=mgr, now=0.0)
            await db_session.refresh(item)
            assert item.waiting_reason is None  # orphan token cleared regardless of grace
            mock_n.assert_not_awaited()  # still inside grace
            # After grace: the reclaimed pause escalates as an ordinary unattended stall.
            await farm_stall.check_paused_prints(db_session, manager=mgr, now=_PAUSE_GRACE_S + 1)
            mock_n.assert_awaited_once()
        await db_session.refresh(item)
        assert item.waiting_reason == "print_paused_stalled"
        assert item.status == "printing"  # never terminal

    async def test_orphaned_recovering_on_running_printer_cleared(self, db_session):
        """R1: an orphaned ``spool_jam_recovering`` token on a RUNNING (not paused)
        printer is cleared too — it must not sit in the UI forever."""
        item = await _add_printing(db_session, 7)
        item.waiting_reason = "spool_jam_recovering"
        await db_session.commit()
        mgr = _FakeManager({7: True}, {7: _FakeState("RUNNING")})
        with patch.object(notification_service, "on_print_paused_stalled", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_paused_prints(db_session, manager=mgr, now=0.0)
            mock_n.assert_not_awaited()  # RUNNING → no pause-stall flag
        await db_session.refresh(item)
        assert item.waiting_reason is None  # orphan token cleared despite RUNNING
        assert item.status == "printing"

    async def test_skips_live_recovery_task_fires_when_done(self, db_session, monkeypatch):
        item = await _add_printing(db_session, 7)
        mgr = _FakeManager({7: True}, {7: _FakeState("PAUSE")})
        from backend.app.services import spool_recovery

        class _FakeTask:
            def __init__(self, done: bool):
                self._done = done

            def done(self) -> bool:
                return self._done

        with patch.object(notification_service, "on_print_paused_stalled", new_callable=AsyncMock) as mock_n:
            # A LIVE recovery task owns the pause → skip (no flag).
            monkeypatch.setitem(spool_recovery._active_tasks, 7, _FakeTask(done=False))
            await farm_stall.check_paused_prints(db_session, manager=mgr, now=0.0)
            await farm_stall.check_paused_prints(db_session, manager=mgr, now=_PAUSE_GRACE_S + 100)
            mock_n.assert_not_awaited()
            await db_session.refresh(item)
            assert item.waiting_reason is None

            # Task DONE → no longer owns the pause → the watch fires.
            monkeypatch.setitem(spool_recovery._active_tasks, 7, _FakeTask(done=True))
            await farm_stall.check_paused_prints(db_session, manager=mgr, now=_PAUSE_GRACE_S + 200)
            await farm_stall.check_paused_prints(db_session, manager=mgr, now=2 * _PAUSE_GRACE_S + 300)
            mock_n.assert_awaited_once()

    async def test_offline_printer_owned_by_offline_watch(self, db_session):
        item = await _add_printing(db_session, 7)
        mgr = _FakeManager({7: False}, {7: _FakeState("PAUSE")})  # PAUSEd but OFFLINE
        with patch.object(notification_service, "on_print_paused_stalled", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_paused_prints(db_session, manager=mgr, now=0.0)
            await farm_stall.check_paused_prints(db_session, manager=mgr, now=_PAUSE_GRACE_S + 100)
            mock_n.assert_not_awaited()  # offline watch owns it
        await db_session.refresh(item)
        assert item.waiting_reason is None

    async def test_get_status_none_reads_as_not_paused(self, db_session):
        item = await _add_printing(db_session, 7)
        mgr = _FakeManager({7: True}, {})  # connected but get_status returns None (startup race)
        with patch.object(notification_service, "on_print_paused_stalled", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_paused_prints(db_session, manager=mgr, now=0.0)
            await farm_stall.check_paused_prints(db_session, manager=mgr, now=_PAUSE_GRACE_S + 100)
            mock_n.assert_not_awaited()
        await db_session.refresh(item)
        assert item.waiting_reason is None

    async def test_grace_override_honored(self, db_session):
        from backend.app.api.routes.settings import set_setting

        await set_setting(db_session, "farm_pause_stall_minutes", "5")
        await db_session.commit()
        await _add_printing(db_session, 7)
        mgr = _FakeManager({7: True}, {7: _FakeState("PAUSE")})
        with patch.object(notification_service, "on_print_paused_stalled", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_paused_prints(db_session, manager=mgr, now=0.0)
            await farm_stall.check_paused_prints(db_session, manager=mgr, now=4 * 60)  # < 5 min → no flag
            mock_n.assert_not_awaited()
            await farm_stall.check_paused_prints(db_session, manager=mgr, now=6 * 60)  # > 5 min → fires
            mock_n.assert_awaited_once()

    async def test_appsettings_default_pause_stall_minutes(self):
        # async only to satisfy the module-level asyncio mark; no awaits needed.
        from backend.app.schemas.settings import AppSettings

        assert AppSettings().farm_pause_stall_minutes == 15


class TestSchedulerHookGuard:
    async def test_check_queue_survives_stall_watch_exception(self):
        """The scheduler-tick hook is guarded: a stall-watch exception must NOT
        propagate out of check_queue (it must not kill the dispatch tick)."""
        from unittest.mock import MagicMock

        from backend.app.services.print_scheduler import PrintScheduler

        scheduler = PrintScheduler()
        empty = MagicMock()
        empty.scalars.return_value.all.return_value = []
        with (
            patch("backend.app.services.print_scheduler.async_session") as mock_session,
            patch(
                "backend.app.services.farm_stall.check_stalled_prints", new=AsyncMock(side_effect=RuntimeError("boom"))
            ),
        ):
            mock_db = AsyncMock()
            mock_db.execute = AsyncMock(return_value=empty)
            mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_session.return_value.__aexit__ = AsyncMock(return_value=False)
            # Must not raise despite the stall watch blowing up.
            await scheduler.check_queue()

    async def test_check_queue_survives_pause_watch_exception(self):
        """The pause-stall watch has its OWN guard: an exception in it must not
        propagate out of check_queue (one watch can't kill the tick)."""
        from unittest.mock import MagicMock

        from backend.app.services.print_scheduler import PrintScheduler

        scheduler = PrintScheduler()
        empty = MagicMock()
        empty.scalars.return_value.all.return_value = []
        with (
            patch("backend.app.services.print_scheduler.async_session") as mock_session,
            patch("backend.app.services.farm_stall.check_stalled_prints", new=AsyncMock()),
            patch(
                "backend.app.services.farm_stall.check_paused_prints", new=AsyncMock(side_effect=RuntimeError("boom"))
            ),
        ):
            mock_db = AsyncMock()
            mock_db.execute = AsyncMock(return_value=empty)
            mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_session.return_value.__aexit__ = AsyncMock(return_value=False)
            # Must not raise despite the pause watch blowing up.
            await scheduler.check_queue()
