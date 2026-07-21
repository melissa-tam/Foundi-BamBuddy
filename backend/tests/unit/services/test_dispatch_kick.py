"""Unit tests for the event-driven scheduler wakeup (``services.dispatch_kick``).

Covers coalescing (N kicks → one wake), the no-lost-wakeup contract (a kick after
clear() re-wakes), timeout semantics, cross-thread safety (``kick`` off the loop),
the bounded ring buffer, and reason summarisation.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from backend.app.services.dispatch_kick import DispatchKick


class TestWaitAndKick:
    pytestmark = pytest.mark.asyncio

    async def test_multiple_kicks_coalesce_to_one_wake(self):
        dk = DispatchKick()
        dk.kick("a")
        dk.kick("b")
        dk.kick("c")
        # The event is level-triggered: all three kicks resolve to a single set
        # that stays set until the consumer clears it explicitly.
        assert await dk.wait(0.5) is True
        assert await dk.wait(0.5) is True  # still set — wait() does NOT clear
        dk.clear()
        assert await dk.wait(0.2) is False  # cleared → next wait times out

    async def test_wait_timeout_returns_false(self):
        dk = DispatchKick()
        assert await dk.wait(0.1) is False

    async def test_kick_after_clear_wakes_again_no_lost_wakeup(self):
        """A kick landing after clear() (the mid-check case) must still wake."""
        dk = DispatchKick()
        dk.kick("first")
        assert await dk.wait(0.5) is True
        dk.clear()
        dk.kick("second")  # arrives after the clear, before the next wait
        assert await dk.wait(0.5) is True

    async def test_kick_from_foreign_thread_wakes_loop(self):
        dk = DispatchKick()
        # Prime the loop capture: wait() records the running loop so an off-loop
        # kick can marshal the Event.set back onto it.
        task = asyncio.create_task(dk.wait(2.0))
        await asyncio.sleep(0.05)  # let wait() start and capture the loop
        threading.Thread(target=lambda: dk.kick("from_thread"), daemon=True).start()
        assert await task is True

    async def test_kick_before_first_wait_is_retained(self):
        """A kick before any wait() (scheduler not started yet) is not lost."""
        dk = DispatchKick()
        dk.kick("early")
        assert await dk.wait(0.5) is True


class TestRingBufferAndReasons:
    def test_ring_buffer_bounded_and_drain_clears(self):
        dk = DispatchKick()
        for i in range(60):
            dk.kick(f"r{i}")
        records = dk.drain_reasons()
        assert len(records) == 50  # deque maxlen — oldest evicted
        assert dk.drain_reasons() == []  # drained → empty

    def test_drain_reasons_returns_reason_and_printer_id(self):
        dk = DispatchKick()
        dk.kick("plate_gate_release", 7)
        records = dk.drain_reasons()
        assert len(records) == 1
        _ts, reason, printer_id = records[0]
        assert reason == "plate_gate_release"
        assert printer_id == 7

    def test_summarize_coalesces_reasons(self):
        records = [
            (0.0, "enqueue", None),
            (0.1, "enqueue", 3),
            (0.2, "plate_gate_release", 2),
        ]
        summary = DispatchKick.summarize(records)
        assert "enqueue x2" in summary
        assert "plate_gate_release" in summary
        assert "plate_gate_release x" not in summary  # count 1 → no "xN" suffix

    def test_summarize_empty_is_unknown(self):
        assert DispatchKick.summarize([]) == "unknown"
