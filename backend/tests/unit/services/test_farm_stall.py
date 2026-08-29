"""Offline-stall watch tests (Phase 3.2).

``check_stalled_prints`` flags a farm unit still ``printing`` whose printer has
been offline past the grace window — a one-shot ``on_print_stalled`` notification
plus ``waiting_reason="printer_offline_stalled"`` — and NEVER writes a terminal
status. Reconnect clears the flag. Clock + manager are injected. FK enforcement is
off in the test engine.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.models.print_queue import PrintQueueItem
from backend.app.services import farm_stall, notify_dedup
from backend.app.services.notification_service import notification_service

pytestmark = pytest.mark.asyncio

_GRACE_S = 30 * 60  # default farm_offline_stall_minutes = 30
_PAUSE_GRACE_S = 15 * 60  # default farm_pause_stall_minutes = 15
_W = farm_stall._ATTENTION_REMINDER_S  # attention-reminder window (3600 s)


class _FakeState:
    """Minimal live-status stand-in — the pause watch reads ``.state``, the runout
    reminder additionally decodes ``.hms_errors`` for the demanded slot, and the
    dead-claim watch corroborates with ``.subtask_id``."""

    def __init__(self, state: str | None, hms_errors: list | None = None, subtask_id: str | None = None):
        self.state = state
        self.hms_errors = hms_errors or []
        self.subtask_id = subtask_id


def _runout_demand(ams_id: int, tray_id: int):
    """A slot-attributed runout DEMAND ("AMS X Slot N ... Please insert a new
    filament.") in the HMSError shape the decoder consumes."""
    attr = 0x07000000 | (ams_id << 16) | ((0x20 + tray_id) << 8)
    return SimpleNamespace(attr=attr, code="0x20001", full_code=f"{attr:08X}00020001")


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
    notify_dedup._reset_state()  # the attention reminders ride notify_dedup.allow()
    yield
    farm_stall._reset_state()
    notify_dedup._reset_state()


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


async def _add_paused_held(db, printer_id, reason, pos=1):
    """A printing farm unit already carrying an ESCALATED hold token."""
    it = await _add_printing(db, printer_id, pos=pos)
    it.waiting_reason = reason
    await db.commit()
    await db.refresh(it)
    return it


async def _add_incident_held(db, printer_id, kind, *, pos=1, slot_global_tray=None, item=True, code=None):
    """A printer held by an ESCALATED AMS incident (WS2b).

    The AMS holds (jam / runout / physical) are reminded from their INCIDENT, not
    from a queue token: an incident is a fact about the PRINTER, so a FOREIGN print's
    hold nags exactly like a farm one. ``item=False`` seeds the foreign case (no
    queue unit at all).
    """
    from backend.app.models.printer_incident import STATUS_ESCALATED
    from backend.app.services import printer_incidents

    row = None
    if item:
        row = await _add_paused_held(db, printer_id, f"spool_{kind}_hold", pos=pos)
    await printer_incidents.open_new(
        db,
        printer_id=printer_id,
        job_id="task-1",
        item_id=row.id if row is not None else None,
        kind=kind,
        code=code or ("0700_8010" if kind == "jam" else "0700_8011"),
        codes=f"{kind}:seeded",
        slot_global_tray=slot_global_tray,
        status=STATUS_ESCALATED,
    )
    return row


class TestAttentionReminders:
    """W3: an unresolved escalated hold on a still-PAUSEd printer re-fires its OWN
    notification once per window until a human clears it (the 2026-07-20 5-hour
    single-alert incident). First reminder lands ONE window after first-seen."""

    async def test_first_tick_seeds_no_immediate_reminder(self, db_session):
        await _add_paused_held(db_session, 7, "print_paused_stalled")
        mgr = _FakeManager({7: True}, {7: _FakeState("PAUSE")})
        with patch.object(notification_service, "on_print_paused_stalled", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=0.0)
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=_W - 1)  # still inside window
            mock_n.assert_not_awaited()

    async def test_three_windows_three_reminders(self, db_session):
        await _add_paused_held(db_session, 7, "print_paused_stalled")
        mgr = _FakeManager({7: True}, {7: _FakeState("PAUSE")})
        with patch.object(notification_service, "on_print_paused_stalled", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=0.0)  # seed only
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=_W)  # reminder 1
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=2 * _W)  # reminder 2
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=3 * _W)  # reminder 3
            assert mock_n.await_count == 3  # exactly one per elapsed window, none at seed

    async def test_pause_ends_resets_then_new_incident_reminds(self, db_session):
        item = await _add_paused_held(db_session, 7, "print_paused_stalled")
        paused = _FakeManager({7: True}, {7: _FakeState("PAUSE")})
        running = _FakeManager({7: True}, {7: _FakeState("RUNNING")})
        with patch.object(notification_service, "on_print_paused_stalled", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_attention_reminders(db_session, manager=paused, now=0.0)  # seed
            await farm_stall.check_attention_reminders(db_session, manager=paused, now=_W)  # reminder 1
            assert mock_n.await_count == 1
            # Pause ends → tracking resets (the key leaves the remindable condition).
            await farm_stall.check_attention_reminders(db_session, manager=running, now=_W + 1)
            assert (7, "print_paused_stalled") not in farm_stall._attention_first_seen
            # A NEW pause incident re-seeds and reminds again a window later.
            await farm_stall.check_attention_reminders(db_session, manager=paused, now=_W + 2)  # re-seed
            await farm_stall.check_attention_reminders(db_session, manager=paused, now=2 * _W + 2)  # reminder 2
            assert mock_n.await_count == 2
        await db_session.refresh(item)
        assert item.status == "printing"  # never terminal

    async def test_non_escalated_pause_no_reminder(self, db_session):
        # A benign hold token (not one of the escalated four) is never reminded.
        await _add_paused_held(db_session, 7, "stagger_hold")
        mgr = _FakeManager({7: True}, {7: _FakeState("PAUSE")})
        with patch.object(notification_service, "on_print_paused_stalled", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=0.0)
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=_W)
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=2 * _W)
            mock_n.assert_not_awaited()
        assert (7, "stagger_hold") not in farm_stall._attention_first_seen

    async def test_none_waiting_reason_no_reminder(self, db_session):
        await _add_printing(db_session, 7)  # waiting_reason is None
        mgr = _FakeManager({7: True}, {7: _FakeState("PAUSE")})
        with patch.object(notification_service, "on_print_paused_stalled", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=0.0)
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=_W)
            mock_n.assert_not_awaited()

    async def test_running_printer_never_reminds(self, db_session):
        await _add_paused_held(db_session, 7, "print_paused_stalled")
        mgr = _FakeManager({7: True}, {7: _FakeState("RUNNING")})  # connected but not paused
        with patch.object(notification_service, "on_print_paused_stalled", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=0.0)
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=_W)
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=2 * _W)
            mock_n.assert_not_awaited()

    async def test_offline_printer_never_reminds(self, db_session):
        await _add_incident_held(db_session, 7, "jam")
        mgr = _FakeManager({7: False}, {7: _FakeState("PAUSE")})  # PAUSE but OFFLINE
        with patch.object(notification_service, "on_spool_recovery_failed", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=0.0)
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=_W)
            mock_n.assert_not_awaited()

    async def test_jam_incident_refires_spool_recovery_failed(self, db_session):
        await _add_incident_held(db_session, 7, "jam")
        mgr = _FakeManager({7: True}, {7: _FakeState("PAUSE")})
        with patch.object(notification_service, "on_spool_recovery_failed", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=0.0)
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=_W)
            mock_n.assert_awaited_once()
            assert mock_n.await_args.kwargs["kind"] == "jam"

    async def test_runout_incident_refires_with_the_runout_kind(self, db_session):
        await _add_incident_held(db_session, 8, "runout")
        mgr = _FakeManager({8: True}, {8: _FakeState("PAUSE")})
        with patch.object(notification_service, "on_spool_recovery_failed", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=0.0)
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=_W)
            mock_n.assert_awaited_once()
            assert mock_n.await_args.kwargs["kind"] == "runout"

    async def test_a_foreign_print_hold_reminds_too(self, db_session):
        """The WS2b reason the reminder moved off queue tokens: a print the farm did
        not dispatch has NO unit to carry one, so a foreign hold nagged nobody."""
        await _add_incident_held(db_session, 11, "runout", item=False)
        mgr = _FakeManager({11: True}, {11: _FakeState("PAUSE")})
        with patch.object(notification_service, "on_spool_recovery_failed", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=0.0)
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=_W)
            mock_n.assert_awaited_once()
            assert mock_n.await_args.kwargs["foreign"] is True

    async def test_a_recovering_incident_is_not_nagged(self, db_session):
        """'recovering' is the machine still acting — not a hold to nag a human about."""
        from backend.app.services import printer_incidents

        await printer_incidents.open_new(
            db_session,
            printer_id=12,
            job_id="task-1",
            item_id=None,
            kind="jam",
            code="0700_8010",
            codes="jam:seeded",
            slot_global_tray=None,
        )
        mgr = _FakeManager({12: True}, {12: _FakeState("PAUSE")})
        with patch.object(notification_service, "on_spool_recovery_failed", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=0.0)
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=_W)
            mock_n.assert_not_awaited()

    async def test_plate_vision_reason_refires_plate_not_empty(self, db_session):
        await _add_paused_held(db_session, 9, "plate_not_empty_printer_detected")
        mgr = _FakeManager({9: True}, {9: _FakeState("PAUSE")})
        with patch.object(notification_service, "on_plate_not_empty", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=0.0)
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=_W)
            mock_n.assert_awaited_once()

    async def test_notify_failure_does_not_abort_tick(self, db_session):
        # One bad printer's notify blowing up must not stop the other's reminder.
        await _add_paused_held(db_session, 7, "print_paused_stalled")
        await _add_paused_held(db_session, 8, "print_paused_stalled", pos=2)
        mgr = _FakeManager({7: True, 8: True}, {7: _FakeState("PAUSE"), 8: _FakeState("PAUSE")})

        async def _boom(printer_id, *a, **k):
            if printer_id == 7:
                raise RuntimeError("boom")

        with patch.object(notification_service, "on_print_paused_stalled", new=AsyncMock(side_effect=_boom)) as mock_n:
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=0.0)
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=_W)
            # Both printers attempted (7 raised, 8 succeeded) — the tick survived.
            assert mock_n.await_count == 2

    async def test_reason_set_membership_pinned(self):
        # async only to satisfy the module-level asyncio mark; no awaits needed.
        # WS2b: the AMS holds moved OUT of the token lane and are reminded from
        # their open escalated incident instead (a foreign print has no token).
        expected = {
            "print_paused_stalled",
            "plate_not_empty_printer_detected",
        }
        assert set(farm_stall._ATTENTION_REASONS) == expected
        # The dispatch dict IS the reason set — they cannot drift.
        assert set(farm_stall._ATTENTION_DISPATCH) == farm_stall._ATTENTION_REASONS
        # ...and every incident kind has reminder copy, so a new kind cannot land
        # silently.
        from backend.app.models.printer_incident import KIND_JAM, KIND_PHYSICAL, KIND_RUNOUT

        assert set(farm_stall._INCIDENT_REMINDER_DETAIL) == {KIND_JAM, KIND_RUNOUT, KIND_PHYSICAL}


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

    async def test_check_queue_survives_attention_watch_exception(self):
        """The attention-reminder watch has its OWN guard: an exception in it must
        not propagate out of check_queue."""
        from unittest.mock import MagicMock

        from backend.app.services.print_scheduler import PrintScheduler

        scheduler = PrintScheduler()
        empty = MagicMock()
        empty.scalars.return_value.all.return_value = []
        with (
            patch("backend.app.services.print_scheduler.async_session") as mock_session,
            patch("backend.app.services.farm_stall.check_stalled_prints", new=AsyncMock()),
            patch("backend.app.services.farm_stall.check_paused_prints", new=AsyncMock()),
            patch(
                "backend.app.services.farm_stall.check_attention_reminders",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
        ):
            mock_db = AsyncMock()
            mock_db.execute = AsyncMock(return_value=empty)
            mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_session.return_value.__aexit__ = AsyncMock(return_value=False)
            # Must not raise despite the attention watch blowing up.
            await scheduler.check_queue()


class TestRunoutReminderNamesTheLiveSlot:
    """006-H2S 2026-07-26: the hourly runout nag hardcoded ``runout_slot=None``, so
    every re-fire degraded to "the SAME slot" and told an operator who had already
    been sent to the WRONG slot nothing new for 12 h. It now decodes the firmware's
    CURRENT demand from the live state the tick already read."""

    async def test_reminder_carries_the_demanded_slot(self, db_session):
        await _add_incident_held(db_session, 8, "runout")
        # Firmware demands AMS A slot 2 (0-indexed tray 1).
        mgr = _FakeManager({8: True}, {8: _FakeState("PAUSE", [_runout_demand(0, 1)])})
        with patch.object(notification_service, "on_spool_recovery_failed", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=0.0)
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=_W)
            mock_n.assert_awaited_once()
            assert mock_n.await_args.kwargs["runout_slot"] == "AMS A slot 2"

    async def test_reminder_self_updates_when_the_demand_moves(self, db_session):
        """The whole point: a demand that MOVES between windows must move the nag."""
        await _add_incident_held(db_session, 8, "runout")
        state = _FakeState("PAUSE", [_runout_demand(0, 2)])  # AMS A slot 3
        mgr = _FakeManager({8: True}, {8: state})
        with patch.object(notification_service, "on_spool_recovery_failed", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=0.0)
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=_W)
            assert mock_n.await_args.kwargs["runout_slot"] == "AMS A slot 3"
            # A second roll empties; the firmware appends a demand for slot 2.
            state.hms_errors.append(_runout_demand(0, 1))
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=2 * _W)
            assert mock_n.await_args.kwargs["runout_slot"] == "AMS A slot 2"
            assert mock_n.await_count == 2

    async def test_slot_agnostic_runout_degrades_honestly_to_none(self, db_session):
        """The bare 07xx_8011 names no slot, so ``None`` (the copy falls back to "the
        SAME slot") is the honest answer — not a guess."""
        await _add_incident_held(db_session, 8, "runout")
        bare = SimpleNamespace(attr=0x07000000, code="0x8011", full_code="0700000000008011")
        mgr = _FakeManager({8: True}, {8: _FakeState("PAUSE", [bare])})
        with patch.object(notification_service, "on_spool_recovery_failed", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=0.0)
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=_W)
            mock_n.assert_awaited_once()
            assert mock_n.await_args.kwargs["runout_slot"] is None

    async def test_jam_reminder_never_borrows_a_runout_demand_slot(self, db_session):
        """A live runout demand on the wire must not leak into a JAM's copy — the
        8010 family carries no slot attribution, so naming one would be a guess."""
        await _add_incident_held(db_session, 7, "jam")
        mgr = _FakeManager({7: True}, {7: _FakeState("PAUSE", [_runout_demand(0, 1)])})
        with patch.object(notification_service, "on_spool_recovery_failed", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=0.0)
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=_W)
            mock_n.assert_awaited_once()
            assert mock_n.await_args.kwargs["kind"] == "jam"
            assert mock_n.await_args.kwargs["runout_slot"] is None


class TestForeignPausedPrinters:
    """WS2b: the R1 minimum for a print the farm did NOT dispatch.

    Every other watch in this module starts from a ``printing`` farm queue item, so a
    LAN print, a screen restart or a USB re-run that PAUSEs — a vision trip, a
    door-open, an HMS outside the AMS-actionable classes — was visible to nothing at
    all and sat until a human happened to look.
    """

    async def test_a_foreign_pause_past_grace_alerts_once(self, db_session, printer_factory):
        printer = await printer_factory()
        mgr = _FakeManager({printer.id: True}, {printer.id: _FakeState("PAUSE")})
        with patch.object(notification_service, "on_foreign_print_paused", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_foreign_paused_printers(db_session, manager=mgr, now=0.0)  # edge
            await farm_stall.check_foreign_paused_printers(db_session, manager=mgr, now=60.0)  # inside grace
            mock_n.assert_not_awaited()
            await farm_stall.check_foreign_paused_printers(db_session, manager=mgr, now=16 * 60.0)
            mock_n.assert_awaited_once()
            # ONE alert per episode, however many ticks the pause survives.
            await farm_stall.check_foreign_paused_printers(db_session, manager=mgr, now=40 * 60.0)
            assert mock_n.await_count == 1

    async def test_leaving_pause_resets_the_episode(self, db_session, printer_factory):
        printer = await printer_factory()
        paused = _FakeManager({printer.id: True}, {printer.id: _FakeState("PAUSE")})
        running = _FakeManager({printer.id: True}, {printer.id: _FakeState("RUNNING")})
        with patch.object(notification_service, "on_foreign_print_paused", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_foreign_paused_printers(db_session, manager=paused, now=0.0)
            await farm_stall.check_foreign_paused_printers(db_session, manager=paused, now=16 * 60.0)
            assert mock_n.await_count == 1
            # The operator clears it; the episode ends.
            await farm_stall.check_foreign_paused_printers(db_session, manager=running, now=17 * 60.0)
            assert printer.id not in farm_stall._foreign_paused_at
            # A NEW pause episode alerts again.
            await farm_stall.check_foreign_paused_printers(db_session, manager=paused, now=18 * 60.0)
            await farm_stall.check_foreign_paused_printers(db_session, manager=paused, now=40 * 60.0)
            assert mock_n.await_count == 2

    async def test_a_farm_print_is_not_this_watch_s_business(self, db_session, printer_factory):
        """The farm-unit pause-stall watch owns that one — double-alerting a hold a
        human already knows about is exactly what the ownership checks prevent."""
        printer = await printer_factory()
        await _add_printing(db_session, printer.id)
        mgr = _FakeManager({printer.id: True}, {printer.id: _FakeState("PAUSE")})
        with patch.object(notification_service, "on_foreign_print_paused", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_foreign_paused_printers(db_session, manager=mgr, now=0.0)
            await farm_stall.check_foreign_paused_printers(db_session, manager=mgr, now=60 * 60.0)
            mock_n.assert_not_awaited()

    async def test_an_open_incident_owns_the_pause(self, db_session, printer_factory):
        """An AMS hold has its own alert AND its own hourly nag — this watch is the
        catch-all behind everything the taxonomy could not explain."""
        printer = await printer_factory()
        await _add_incident_held(db_session, printer.id, "runout", item=False)
        mgr = _FakeManager({printer.id: True}, {printer.id: _FakeState("PAUSE")})
        with patch.object(notification_service, "on_foreign_print_paused", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_foreign_paused_printers(db_session, manager=mgr, now=0.0)
            await farm_stall.check_foreign_paused_printers(db_session, manager=mgr, now=60 * 60.0)
            mock_n.assert_not_awaited()

    async def test_an_offline_printer_is_not_alerted(self, db_session, printer_factory):
        printer = await printer_factory()
        mgr = _FakeManager({printer.id: False}, {printer.id: _FakeState("PAUSE")})
        with patch.object(notification_service, "on_foreign_print_paused", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_foreign_paused_printers(db_session, manager=mgr, now=0.0)
            await farm_stall.check_foreign_paused_printers(db_session, manager=mgr, now=60 * 60.0)
            mock_n.assert_not_awaited()

    async def test_a_notify_failure_does_not_abort_the_watch(self, db_session, printer_factory):
        a = await printer_factory()
        b = await printer_factory()
        mgr = _FakeManager({a.id: True, b.id: True}, {a.id: _FakeState("PAUSE"), b.id: _FakeState("PAUSE")})

        async def _boom(printer_id, *args, **kwargs):
            if printer_id == a.id:
                raise RuntimeError("boom")

        with patch.object(notification_service, "on_foreign_print_paused", new=AsyncMock(side_effect=_boom)) as mock_n:
            await farm_stall.check_foreign_paused_printers(db_session, manager=mgr, now=0.0)
            await farm_stall.check_foreign_paused_printers(db_session, manager=mgr, now=60 * 60.0)
            assert mock_n.await_count == 2  # both attempted; the tick survived


# --------------------------------------------------------------------------- #
# The dead dispatch claim (2026-08-29, 001-H2S item 1010)
# --------------------------------------------------------------------------- #
_NOW = 1_800_000_000.0  # a plausible epoch, since this watch compares against started_at
_MIN_AGE = farm_stall._DEAD_CLAIM_MIN_AGE_S
_CLAIM_DWELL = farm_stall._DEAD_CLAIM_DWELL_S


def _stamp(age_s: float):
    """A naive-UTC ``started_at`` that is ``age_s`` old at ``_NOW`` — the fork stores
    these in a tz-naive DateTime column, which is exactly what the watch normalizes."""
    return datetime.fromtimestamp(_NOW - age_s, tz=timezone.utc).replace(tzinfo=None)


async def _add_claim(db, printer_id, *, age_s=_MIN_AGE + 60, dispatch_subtask_id="dispatch-1", pos=1):
    """A unit claiming ``printing`` on ``printer_id`` since ``age_s`` ago."""
    it = PrintQueueItem(
        printer_id=printer_id,
        status="printing",
        first_article=False,
        plate_id=1,
        position=pos,
        started_at=_stamp(age_s),
        dispatch_subtask_id=dispatch_subtask_id,
        ams_mapping="[0, -1, -1, -1]",
    )
    db.add(it)
    await db.commit()
    await db.refresh(it)
    return it


async def _busy_printer_ids(db):
    """Re-run the scheduler's OWN busy seed — the query that decides whether a
    printer is even considered for dispatch."""
    from sqlalchemy import select as sa_select

    rows = await db.execute(
        sa_select(PrintQueueItem.printer_id)
        .where(PrintQueueItem.status == "printing")
        .where(PrintQueueItem.printer_id.is_not(None))
    )
    return {pid for (pid,) in rows.all() if pid is not None}


async def _mature(db, mgr, *, base=_NOW):
    """Drive the watch past its dwell: one seeding tick, then one past the window."""
    await farm_stall.check_dead_dispatch_claims(db, manager=mgr, now=base)
    await farm_stall.check_dead_dispatch_claims(db, manager=mgr, now=base + _CLAIM_DWELL + 1)


class TestDeadDispatchClaims:
    """001-H2S 2026-08-29: item 1010 was dispatched into a standing AMS fault at
    01:25:13, the print never started, and NOTHING could ever retire the row — no
    terminal echo arrives for a print that never began. It seeded ``busy_printers``
    every 30 s for 15 h with seven pending units queued behind it."""

    async def test_matured_never_started_claim_is_released_to_pending(self, db_session):
        """LIVENESS — the whole point. Every dispatch-shaped column resets."""
        item = await _add_claim(db_session, 21)
        item.waiting_reason = "stagger_hold"
        await db_session.commit()
        mgr = _FakeManager({21: True}, {21: _FakeState("IDLE", subtask_id="some-other-job")})

        await _mature(db_session, mgr)

        db_session.expunge_all()
        after = await db_session.get(PrintQueueItem, item.id)
        assert after.status == "pending"
        assert after.started_at is None
        assert after.ams_mapping is None  # a decided mapping must never survive as a "pin"
        assert after.waiting_reason is None

    async def test_released_claim_frees_the_printer_for_dispatch(self, db_session):
        """LIVENESS — the CONSEQUENCE, not the row. "The status changed" and "the
        printer can take work again" are the cured-storm/starved-deadlock trap: this
        re-runs the scheduler's own busy seed."""
        await _add_claim(db_session, 22)
        mgr = _FakeManager({22: True}, {22: _FakeState("IDLE")})
        assert 22 in await _busy_printer_ids(db_session)

        await _mature(db_session, mgr)

        assert 22 not in await _busy_printer_ids(db_session)

    async def test_a_young_claim_is_untouched(self, db_session):
        """Clock A: the dispatch watchdog owns the first 270 s and is always the
        first responder — this watch only ever sees what it missed."""
        item = await _add_claim(db_session, 23, age_s=120)
        mgr = _FakeManager({23: True}, {23: _FakeState("IDLE")})

        await _mature(db_session, mgr)

        db_session.expunge_all()
        assert (await db_session.get(PrintQueueItem, item.id)).status == "printing"

    async def test_the_dwell_must_hold_continuously(self, db_session):
        """Clock B: one poll is not evidence — a printer's state flaps around a
        dispatch, and a glimpse of IDLE must not accumulate toward a release."""
        item = await _add_claim(db_session, 24)
        idle = _FakeManager({24: True}, {24: _FakeState("IDLE")})
        running = _FakeManager({24: True}, {24: _FakeState("RUNNING")})

        await farm_stall.check_dead_dispatch_claims(db_session, manager=idle, now=_NOW)  # seed
        await farm_stall.check_dead_dispatch_claims(db_session, manager=running, now=_NOW + 10)  # guard breaks
        assert farm_stall._dead_claim_since == {}
        await farm_stall.check_dead_dispatch_claims(db_session, manager=idle, now=_NOW + _CLAIM_DWELL)  # re-seed only

        db_session.expunge_all()
        assert (await db_session.get(PrintQueueItem, item.id)).status == "printing"

    @pytest.mark.parametrize("live", ["PREPARE", "SLICING", "RUNNING", "PAUSE"])
    async def test_an_active_state_is_never_released(self, db_session, live):
        """PAUSE is ACTIVE on purpose: a native-vision trip pauses at print start
        with the plate occupied, and releasing that unit would re-dispatch onto it.
        The set is ``print_scheduler.ACTIVE_PRINT_STATES`` — one origin."""
        item = await _add_claim(db_session, 25)
        mgr = _FakeManager({25: True}, {25: _FakeState(live)})

        await _mature(db_session, mgr)

        db_session.expunge_all()
        assert (await db_session.get(PrintQueueItem, item.id)).status == "printing"

    async def test_a_live_subtask_matching_our_dispatch_id_stands_down(self, db_session):
        """The print DID land — the printer is echoing the id we minted for it."""
        item = await _add_claim(db_session, 26, dispatch_subtask_id="dispatch-1")
        mgr = _FakeManager({26: True}, {26: _FakeState("FINISH", subtask_id="dispatch-1")})

        await _mature(db_session, mgr)

        db_session.expunge_all()
        assert (await db_session.get(PrintQueueItem, item.id)).status == "printing"

    async def test_a_null_dispatch_subtask_id_still_releases(self, db_session):
        """The corrected guard. Requiring the id would strand exactly the rows that
        need this most — a claim with no id is the SAME bug in a different row
        shape, so the id is corroboration when present and never a precondition."""
        item = await _add_claim(db_session, 27, dispatch_subtask_id=None)
        mgr = _FakeManager({27: True}, {27: _FakeState("IDLE", subtask_id="whatever")})

        await _mature(db_session, mgr)

        db_session.expunge_all()
        assert (await db_session.get(PrintQueueItem, item.id)).status == "pending"

    async def test_a_printing_archive_on_the_printer_stands_down(self, db_session):
        """Hard disjointness with ``main.reconcile_stale_active_prints``, which is
        archive-scoped: the two reconcilers can never both act on one printer."""
        from backend.app.models.archive import PrintArchive

        item = await _add_claim(db_session, 28)
        db_session.add(
            PrintArchive(printer_id=28, filename="live.3mf", file_path="/tmp/live.3mf", file_size=1, status="printing")
        )
        await db_session.commit()
        mgr = _FakeManager({28: True}, {28: _FakeState("IDLE")})

        await _mature(db_session, mgr)

        db_session.expunge_all()
        assert (await db_session.get(PrintQueueItem, item.id)).status == "printing"

    async def test_an_open_incident_owns_the_printer(self, db_session):
        """Guard 6 — and the reason the wire-clear sweep runs FIRST in the tick: an
        incident can only DELAY a release, never cause a wrong one."""
        item = await _add_claim(db_session, 29)
        await _add_incident_held(db_session, 29, "physical", item=False, pos=2)
        mgr = _FakeManager({29: True}, {29: _FakeState("IDLE")})

        await _mature(db_session, mgr)

        db_session.expunge_all()
        assert (await db_session.get(PrintQueueItem, item.id)).status == "printing"

    async def test_a_live_recovery_task_owns_the_printer(self, db_session, monkeypatch):
        from backend.app.services import spool_recovery

        class _Live:
            def done(self) -> bool:
                return False

        item = await _add_claim(db_session, 30)
        monkeypatch.setitem(spool_recovery._active_tasks, 30, _Live())
        mgr = _FakeManager({30: True}, {30: _FakeState("IDLE")})

        await _mature(db_session, mgr)

        db_session.expunge_all()
        assert (await db_session.get(PrintQueueItem, item.id)).status == "printing"

    async def test_an_offline_printer_is_left_to_the_offline_watch(self, db_session):
        item = await _add_claim(db_session, 31)
        mgr = _FakeManager({31: False}, {31: _FakeState("IDLE")})

        await _mature(db_session, mgr)

        db_session.expunge_all()
        assert (await db_session.get(PrintQueueItem, item.id)).status == "printing"

    async def test_an_offline_stalled_unit_is_left_to_the_offline_watch(self, db_session):
        """Reconnect-and-still-stalled: that story has its own token and its own
        watch, and reconciliation resolves its TRUE outcome — not a re-dispatch."""
        item = await _add_claim(db_session, 32)
        item.waiting_reason = "printer_offline_stalled"
        await db_session.commit()
        mgr = _FakeManager({32: True}, {32: _FakeState("IDLE")})

        await _mature(db_session, mgr)

        db_session.expunge_all()
        assert (await db_session.get(PrintQueueItem, item.id)).status == "printing"

    async def test_a_claim_with_no_started_at_is_left_alone(self, db_session):
        """Without ``started_at`` the age is unknowable, and an unknowable age is not
        evidence — clock A fails closed."""
        item = await _add_claim(db_session, 33)
        item.started_at = None
        await db_session.commit()
        mgr = _FakeManager({33: True}, {33: _FakeState("IDLE")})

        await _mature(db_session, mgr)

        db_session.expunge_all()
        assert (await db_session.get(PrintQueueItem, item.id)).status == "printing"

    async def test_dwell_state_is_pruned_when_the_item_leaves_printing(self, db_session):
        """The dwell dict is keyed by item id and must not outlive the claim."""
        item = await _add_claim(db_session, 34)
        mgr = _FakeManager({34: True}, {34: _FakeState("IDLE")})
        await farm_stall.check_dead_dispatch_claims(db_session, manager=mgr, now=_NOW)
        assert item.id in farm_stall._dead_claim_since

        item.status = "completed"
        await db_session.commit()
        await farm_stall.check_dead_dispatch_claims(db_session, manager=mgr, now=_NOW + 1)
        assert farm_stall._dead_claim_since == {}

    async def test_zombie_claim_cascade_end_to_end(self, db_session, monkeypatch, test_engine):
        """LIVENESS — the one test that fails on the 2026-08-29 build.

        The full incident: an ESCALATED incident holding a printer whose fault has
        cleared, a zombie ``printing`` claim behind it, and an idle fault-free
        printer taking no work. Sweep, then watch: the incident closes, the claim is
        released, and the printer is dispatchable again — the exact sequence the
        deploy performs on 001-H2S."""
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        import backend.app.core.database as core_db
        from backend.app.services import printer_incidents, spool_recovery

        monkeypatch.setattr(
            core_db, "async_session", async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        )
        item = await _add_claim(db_session, 41)
        await _add_incident_held(db_session, 41, "physical", item=False, pos=2)
        state = _FakeState("IDLE")  # connected, idle, wire CLEAN
        monkeypatch.setattr(spool_recovery.printer_manager, "get_status", lambda _pid: state)
        monkeypatch.setattr(spool_recovery.printer_manager, "is_connected", lambda _pid: True)
        mgr = _FakeManager({41: True}, {41: state})

        # Tick 1: the sweep seeds its dwell; the watch is blocked by the open incident.
        await spool_recovery.sweep_open_incidents(now=_NOW)
        await farm_stall.check_dead_dispatch_claims(db_session, manager=mgr, now=_NOW)
        assert await printer_incidents.get_open(db_session, 41) is not None

        # Tick 2: the incident closes on wire-clear; the watch may now seed.
        t2 = _NOW + spool_recovery._HOLD_OVER_DWELL_S + 1
        assert await spool_recovery.sweep_open_incidents(now=t2) == 1
        await farm_stall.check_dead_dispatch_claims(db_session, manager=mgr, now=t2)

        # Tick 3: past the claim dwell — the unit is released.
        await farm_stall.check_dead_dispatch_claims(db_session, manager=mgr, now=t2 + _CLAIM_DWELL + 1)

        db_session.expunge_all()
        assert await printer_incidents.get_open(db_session, 41) is None
        assert printer_incidents.snapshot(41) is None  # the chip cleared
        assert (await db_session.get(PrintQueueItem, item.id)).status == "pending"
        assert 41 not in await _busy_printer_ids(db_session)

    async def test_one_bad_item_does_not_abort_the_watch(self, db_session):
        """Invariant 10 — the watch runs from the scheduler tick."""
        bad = await _add_claim(db_session, 51, pos=1)
        good = await _add_claim(db_session, 52, pos=2)
        mgr = _FakeManager({51: True, 52: True}, {51: _FakeState("IDLE"), 52: _FakeState("IDLE")})

        from backend.app.services import queue_transitions

        real = queue_transitions.release_unstarted_claim

        async def _boom(db, *, item_id):
            if item_id == bad.id:
                raise RuntimeError("boom")
            return await real(db, item_id=item_id)

        with patch.object(queue_transitions, "release_unstarted_claim", new=AsyncMock(side_effect=_boom)):
            await _mature(db_session, mgr)

        db_session.expunge_all()
        assert (await db_session.get(PrintQueueItem, bad.id)).status == "printing"
        assert (await db_session.get(PrintQueueItem, good.id)).status == "pending"


class TestRemindOpenIncidentsNonPause:
    """2026-08-29: the incident nag skipped any printer not reading PAUSE, which
    silently exempted the worst shape there is — 001-H2S incident #60 held an IDLE
    printer for 15 h with seven pending units behind it and fired ZERO
    notifications. It was found by eye."""

    async def test_an_idle_held_printer_nags_with_the_non_pause_wording(self, db_session):
        """LIVENESS — fails on the 2026-08-29 build (15 h, zero notifications)."""
        await _add_incident_held(db_session, 61, "physical", item=False)
        mgr = _FakeManager({61: True}, {61: _FakeState("IDLE")})
        with patch.object(notification_service, "on_spool_recovery_failed", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=0.0)  # seed
            mock_n.assert_not_awaited()
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=_W)
            mock_n.assert_awaited_once()
            detail = mock_n.await_args.kwargs["detail"]
            assert "still PAUSED" not in detail  # it is NOT paused — that copy would be a lie
            assert "idle" in detail
            # ...once per window, not once per tick.
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=_W + 60)
            assert mock_n.await_count == 1

    async def test_the_paused_copy_is_unchanged(self, db_session):
        await _add_incident_held(db_session, 62, "jam")
        mgr = _FakeManager({62: True}, {62: _FakeState("PAUSE")})
        with patch.object(notification_service, "on_spool_recovery_failed", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=0.0)
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=_W)
            assert mock_n.await_args.kwargs["detail"] == farm_stall._JAM_REMINDER_DETAIL

    @pytest.mark.parametrize("live", ["", "UNKNOWN", None])
    async def test_no_state_is_not_evidence_and_never_nags(self, db_session, live):
        await _add_incident_held(db_session, 63, "runout", item=False)
        mgr = _FakeManager({63: True}, {63: _FakeState(live) if live is not None else None})
        with patch.object(notification_service, "on_spool_recovery_failed", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=0.0)
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=_W)
            mock_n.assert_not_awaited()

    async def test_a_disconnected_printer_never_nags(self, db_session):
        await _add_incident_held(db_session, 64, "runout", item=False)
        mgr = _FakeManager({64: False}, {64: _FakeState("IDLE")})
        with patch.object(notification_service, "on_spool_recovery_failed", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=0.0)
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=_W)
            mock_n.assert_not_awaited()

    async def test_a_recovering_incident_never_nags_however_the_printer_reads(self, db_session):
        from backend.app.services import printer_incidents

        await printer_incidents.open_new(
            db_session,
            printer_id=65,
            job_id="task-1",
            item_id=None,
            kind="jam",
            code="0700_8010",
            codes="jam:seeded",
            slot_global_tray=None,
        )
        mgr = _FakeManager({65: True}, {65: _FakeState("IDLE")})
        with patch.object(notification_service, "on_spool_recovery_failed", new_callable=AsyncMock) as mock_n:
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=0.0)
            await farm_stall.check_attention_reminders(db_session, manager=mgr, now=_W)
            mock_n.assert_not_awaited()

    async def test_every_incident_kind_has_non_pause_copy(self):
        """A new incident kind must not land with a hole where its nag should be."""
        from backend.app.models.printer_incident import KIND_JAM, KIND_PHYSICAL, KIND_RUNOUT

        assert set(farm_stall._INCIDENT_REMINDER_DETAIL_UNPAUSED) == {KIND_JAM, KIND_RUNOUT, KIND_PHYSICAL}
