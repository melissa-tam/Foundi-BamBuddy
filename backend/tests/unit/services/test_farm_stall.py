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


class _FakeManager:
    def __init__(self, connected: dict[int, bool]):
        self._connected = connected

    def is_connected(self, pid: int) -> bool:
        return self._connected.get(pid, False)


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
