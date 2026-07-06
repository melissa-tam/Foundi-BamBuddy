"""State-transition tests for the cooldown-verified plate-clear monitor."""

from types import SimpleNamespace

import pytest

from backend.app.services.eject.monitor import (
    EjectCooldownMonitor,
    should_auto_clear,
    should_rearm,
    watch_bed_and_clear,
)


class _FakeManager:
    """Scripted printer_manager: yields the next status per get_status call and
    records set_awaiting_plate_clear calls."""

    def __init__(self, statuses):
        self._statuses = list(statuses)
        self._i = 0
        self.clear_calls = []

    def get_status(self, printer_id):
        if self._i < len(self._statuses):
            s = self._statuses[self._i]
        else:
            s = self._statuses[-1] if self._statuses else None
        self._i += 1
        return s

    def set_awaiting_plate_clear(self, printer_id, awaiting):
        self.clear_calls.append((printer_id, awaiting))


def _status(bed, connected=True):
    return SimpleNamespace(connected=connected, temperatures={"bed": bed})


async def _noop_sleep(_seconds):
    return None


class TestShouldAutoClear:
    def test_completed_clears(self):
        assert should_auto_clear("completed") is True

    @pytest.mark.parametrize("status", ["failed", "aborted", "cancelled", "printing"])
    def test_non_success_does_not_clear(self, status):
        assert should_auto_clear(status) is False


class TestShouldRearm:
    """Startup re-arm decision: gate raised + last job completed + eject profile."""

    def test_rearms_completed_eject_job_with_gate_set(self):
        assert should_rearm(True, "completed", 5) is True

    def test_no_rearm_when_gate_not_set(self):
        assert should_rearm(False, "completed", 5) is False

    @pytest.mark.parametrize("status", ["failed", "aborted", "cancelled", "printing", "pending", None])
    def test_no_rearm_on_non_completed_status(self, status):
        # Failures/stops presume an occupied plate — the gate stays for a human.
        assert should_rearm(True, status, 5) is False

    def test_no_rearm_without_eject_profile(self):
        # Non-eject jobs keep the manual plate-clear flow untouched.
        assert should_rearm(True, "completed", None) is False

    def test_first_article_never_rearms(self):
        # A completed first-article item carries an eject profile but its eject
        # block is deliberately NOT injected — the part stays on the plate, so the
        # gate must not auto-clear.
        assert should_rearm(True, "completed", 5, first_article=True) is False
        # Non-FA item with the same inputs still re-arms.
        assert should_rearm(True, "completed", 5, first_article=False) is True


class TestStartWatchDedup:
    """Terminal-status and startup re-arm share _start_watch; it must not
    double-spawn for a printer whose watch is already in flight."""

    def test_second_start_is_a_noop(self, monkeypatch):
        from backend.app.services.eject import monitor as monitor_mod

        spawned = []

        def fake_spawn(coro, *, name=None):
            spawned.append(name)
            coro.close()  # never run it — we only count spawns

        monkeypatch.setattr(monitor_mod, "spawn_background_task", fake_spawn)
        mon = EjectCooldownMonitor()
        assert mon._start_watch(7) is True
        assert mon._start_watch(7) is False  # dedup while in flight
        assert mon._start_watch(8) is True  # other printers unaffected
        assert spawned == ["eject-cooldown-watch-7", "eject-cooldown-watch-8"]


class TestResolveEjectThresholdFirstArticle:
    """`_resolve_eject_threshold` must resolve first-article items to no-auto-clear
    even though they carry an eject profile."""

    async def test_first_article_resolves_to_none(self, monkeypatch):
        import contextlib

        from backend.app.services.eject import monitor as monitor_mod

        @contextlib.asynccontextmanager
        async def _fake_session():
            yield object()

        async def _fake_latest(_db, _pid):
            return SimpleNamespace(first_article=True, eject_profile_id=5)

        monkeypatch.setattr(monitor_mod, "async_session", _fake_session, raising=False)
        monkeypatch.setattr(monitor_mod, "_latest_started_item", _fake_latest)
        # Patched via the function's local import path too.
        monkeypatch.setattr("backend.app.core.database.async_session", _fake_session, raising=False)

        threshold = await monitor_mod._resolve_eject_threshold(7)
        assert threshold is None


class TestWatchBedAndClear:
    async def test_clears_when_bed_reaches_threshold(self):
        mgr = _FakeManager([_status(60), _status(40), _status(27)])
        outcome = await watch_bed_and_clear(7, 28.0, manager=mgr, timeout_s=100, check_interval_s=10, sleep=_noop_sleep)
        assert outcome == "cleared"
        assert mgr.clear_calls == [(7, False)]

    async def test_clears_at_exact_threshold(self):
        mgr = _FakeManager([_status(28.0)])
        outcome = await watch_bed_and_clear(3, 28.0, manager=mgr, timeout_s=100, check_interval_s=10, sleep=_noop_sleep)
        assert outcome == "cleared"
        assert mgr.clear_calls == [(3, False)]

    async def test_stale_when_status_none(self):
        mgr = _FakeManager([None])
        outcome = await watch_bed_and_clear(9, 28.0, manager=mgr, timeout_s=100, check_interval_s=10, sleep=_noop_sleep)
        assert outcome == "stale"
        assert mgr.clear_calls == []  # gate left SET

    async def test_stale_when_disconnected(self):
        mgr = _FakeManager([_status(60, connected=False)])
        outcome = await watch_bed_and_clear(9, 28.0, manager=mgr, timeout_s=100, check_interval_s=10, sleep=_noop_sleep)
        assert outcome == "stale"
        assert mgr.clear_calls == []

    async def test_timeout_when_never_cools(self):
        mgr = _FakeManager([_status(60)])
        outcome = await watch_bed_and_clear(9, 28.0, manager=mgr, timeout_s=30, check_interval_s=10, sleep=_noop_sleep)
        assert outcome == "timeout"
        assert mgr.clear_calls == []  # gate left SET

    async def test_missing_bed_reading_keeps_waiting_then_times_out(self):
        # bed key absent -> treated as "not yet cool", never clears.
        mgr = _FakeManager([SimpleNamespace(connected=True, temperatures={})])
        outcome = await watch_bed_and_clear(9, 28.0, manager=mgr, timeout_s=20, check_interval_s=10, sleep=_noop_sleep)
        assert outcome == "timeout"
        assert mgr.clear_calls == []
