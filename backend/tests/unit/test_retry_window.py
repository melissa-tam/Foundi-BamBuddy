"""Tests for ``utils.retry_window.RetryWindow`` — the one per-key retry gate.

Pure logic: the window is driven by an injected clock so every case is exact
(no sleeps, no monkeypatched process clock).
"""

from __future__ import annotations

from backend.app.utils.retry_window import RetryWindow


class _Clock:
    """Manually advanced monotonic stand-in."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_first_attempt_allowed_then_suppressed_inside_window():
    clock = _Clock()
    window = RetryWindow(30.0, clock=clock)
    assert window.allow("a") is True  # nothing recorded yet
    assert window.allow("a") is False  # same instant — inside the window
    clock.advance(29.9)
    assert window.allow("a") is False  # still inside


def test_allowed_again_after_the_window_elapses():
    clock = _Clock()
    window = RetryWindow(30.0, clock=clock)
    assert window.allow("a") is True
    clock.advance(30.1)
    assert window.allow("a") is True
    assert window.allow("a") is False  # the second allow re-stamped


def test_boundary_at_exactly_the_window_edge_is_allowed():
    # The window is half-open: suppressed only while (now - last) < seconds.
    clock = _Clock()
    window = RetryWindow(30.0, clock=clock)
    assert window.allow("a") is True
    clock.advance(30.0)
    assert window.allow("a") is True


def test_keys_are_independent():
    clock = _Clock()
    window = RetryWindow(30.0, clock=clock)
    assert window.allow(("p1", 0, 0)) is True
    assert window.allow(("p1", 0, 1)) is True  # a different slot is not gated
    assert window.allow(("p1", 0, 0)) is False


def test_clear_forgets_only_that_key():
    clock = _Clock()
    window = RetryWindow(30.0, clock=clock)
    window.allow("a")
    window.allow("b")
    window.clear("a")
    assert "a" not in window
    assert "b" in window
    assert window.allow("a") is True  # cleared → immediately allowed again
    assert window.allow("b") is False  # untouched → still gated
    window.clear("missing")  # clearing an unknown key is a no-op


def test_reset_forgets_every_key():
    clock = _Clock()
    window = RetryWindow(30.0, clock=clock)
    window.allow("a")
    window.allow("b")
    window.reset()
    assert "a" not in window and "b" not in window
    assert window.allow("a") is True
    assert window.allow("b") is True


def test_default_clock_reads_the_module_monotonic():
    # No injected clock → the module-level monotonic is read at CALL time, which is
    # what lets a consumer's tests drive the window by patching it.
    import backend.app.utils.retry_window as rw

    fake = {"t": 500.0}
    original = rw.monotonic
    rw.monotonic = lambda: fake["t"]
    try:
        window = rw.RetryWindow(30.0)
        assert window.allow("a") is True
        assert window.allow("a") is False
        fake["t"] += 31.0
        assert window.allow("a") is True
    finally:
        rw.monotonic = original


def test_seconds_is_exposed():
    assert RetryWindow(12.5).seconds == 12.5
