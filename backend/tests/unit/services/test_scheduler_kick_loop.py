"""Unit tests for the event-driven scheduler loop (latency Phase A).

Covers the settings-driven interval/debounce clamps and the ``run()`` loop's
woke vs timeout paths (debounce → clear before the next check_queue; timeout
leaves the event untouched).
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services import print_scheduler as ps_module
from backend.app.services.print_scheduler import PrintScheduler

pytestmark = pytest.mark.asyncio


@contextlib.contextmanager
def _dummy_session():
    """Patch the module-level ``async_session`` with a no-op async context manager
    so the loop-level setting reads don't touch a real engine (the setting value
    itself is stubbed via ``_get_*_setting``)."""

    class _CM:
        async def __aenter__(self):
            return MagicMock()

        async def __aexit__(self, *a):
            return False

    with patch.object(ps_module, "async_session", MagicMock(return_value=_CM())):
        yield


class TestIntervalClamp:
    @pytest.mark.parametrize("raw,expected", [(1, 5), (4, 5), (5, 5), (30, 30), (45, 45), (300, 300), (999, 300)])
    async def test_check_interval_clamped_5_to_300(self, raw, expected):
        s = PrintScheduler()
        with _dummy_session(), patch.object(s, "_get_int_setting", AsyncMock(return_value=raw)):
            assert await s._read_check_interval() == expected

    @pytest.mark.parametrize(
        "raw,expected", [(0.0, 0.2), (0.1, 0.2), (0.2, 0.2), (1.0, 1.0), (10.0, 10.0), (99.0, 10.0)]
    )
    async def test_debounce_clamped_02_to_10(self, raw, expected):
        s = PrintScheduler()
        with _dummy_session(), patch.object(s, "_get_float_setting", AsyncMock(return_value=raw)):
            assert await s._read_kick_debounce() == expected


class TestRunLoop:
    async def test_woke_path_debounces_then_clears_before_next_check(self, monkeypatch):
        s = PrintScheduler()
        calls: list[str] = []

        async def fake_check_queue():
            calls.append("check")
            s._running = False  # one iteration is enough to observe the woke path

        monkeypatch.setattr(s, "check_queue", fake_check_queue)
        monkeypatch.setattr(s, "_read_check_interval", AsyncMock(return_value=30))
        monkeypatch.setattr(s, "_read_kick_debounce", AsyncMock(return_value=0.5))

        wait_mock = AsyncMock(return_value=True)  # woke by a kick
        clear_mock = MagicMock()
        drain_mock = MagicMock(return_value=[(0.0, "enqueue", None)])
        monkeypatch.setattr(ps_module.dispatch_kick, "wait", wait_mock)
        monkeypatch.setattr(ps_module.dispatch_kick, "clear", clear_mock)
        monkeypatch.setattr(ps_module.dispatch_kick, "drain_reasons", drain_mock)
        sleep_mock = AsyncMock()
        monkeypatch.setattr(ps_module.asyncio, "sleep", sleep_mock)

        await s.run()

        assert calls == ["check"]
        sleep_mock.assert_awaited_once_with(0.5)  # debounce window
        clear_mock.assert_called_once()  # event cleared (before any follow-up check_queue)
        drain_mock.assert_called_once()

    async def test_timeout_path_does_not_debounce_or_clear(self, monkeypatch):
        s = PrintScheduler()
        calls: list[str] = []

        async def fake_check_queue():
            calls.append("check")
            s._running = False

        monkeypatch.setattr(s, "check_queue", fake_check_queue)
        monkeypatch.setattr(s, "_read_check_interval", AsyncMock(return_value=30))
        monkeypatch.setattr(s, "_read_kick_debounce", AsyncMock(return_value=0.5))

        wait_mock = AsyncMock(return_value=False)  # timed out (fallback poll)
        clear_mock = MagicMock()
        monkeypatch.setattr(ps_module.dispatch_kick, "wait", wait_mock)
        monkeypatch.setattr(ps_module.dispatch_kick, "clear", clear_mock)
        sleep_mock = AsyncMock()
        monkeypatch.setattr(ps_module.asyncio, "sleep", sleep_mock)

        await s.run()

        assert calls == ["check"]
        sleep_mock.assert_not_awaited()  # no debounce on the timeout path
        clear_mock.assert_not_called()  # nothing to clear

    async def test_interval_read_each_iteration(self, monkeypatch):
        """The interval is re-read every pass so a live retune takes effect."""
        s = PrintScheduler()
        read_mock = AsyncMock(return_value=30)
        calls: list[int] = []

        async def fake_check_queue():
            calls.append(1)
            if len(calls) >= 3:
                s._running = False

        monkeypatch.setattr(s, "check_queue", fake_check_queue)
        monkeypatch.setattr(s, "_read_check_interval", read_mock)
        monkeypatch.setattr(s, "_read_kick_debounce", AsyncMock(return_value=0.5))
        monkeypatch.setattr(ps_module.dispatch_kick, "wait", AsyncMock(return_value=False))
        monkeypatch.setattr(ps_module.asyncio, "sleep", AsyncMock())

        await s.run()

        assert read_mock.await_count == 3  # once per iteration
