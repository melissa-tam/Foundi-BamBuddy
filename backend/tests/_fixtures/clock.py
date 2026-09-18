"""THE steerable clock for tests that must not sleep.

Promoted from ``unit/test_retry_window.py:12`` (the cleanest of the hand-rolled
copies) and widened to cover every contract found in the tree, so the eight-plus
private ``_Clock`` classes and ``{"t": 1000.0}`` dicts can be deleted rather than
re-invented:

* ``__call__() -> float`` — the production DI seam. ``RetryWindow`` takes
  ``clock=`` (``backend/app/utils/retry_window.py:36``) and falls back to
  ``time.monotonic`` when it is ``None``; several services construct their
  ``RetryWindow`` at import, so those tests instead install this object over the
  module-level name (``backend.app.utils.retry_window.monotonic``,
  ``plate_occupancy._now_mono``, ``spool_respool._monotonic``,
  ``usage_tracker.monotonic``). Both routes take the instance itself.
* ``.t`` — the float, readable and writable. Several tests hand the raw value
  back into production code (``reconcile_slot_config(..., now=clock.t)``).
* ``clock["t"]`` — the mapping façade the dict-shaped fixtures use, including
  ``clock["t"] += CONST`` and absolute assignment (``now[0] = 6401.0``).
* ``advance(seconds)`` / ``tick()`` — manual stepping. ``tick()`` steps by the
  clock's configured ``step``, which is how the retry-window tests move past one
  full settle window without repeating the constant at every call site.
* ``await clock.sleep(delay)`` — for code under test that awaits a sleep. Two
  flavours, because both are load-bearing: ``sleep_mode="delay"`` advances by the
  requested delay and records it in ``.delays`` (the watchdog tests assert on the
  delays), ``sleep_mode="step"`` advances by a fixed step and ignores the request
  (the poll-driven recovery tests assert that a dwell is only ever reached by
  polling).

Every clock in this tree is a **monotonic float** stand-in — none returns a
``datetime`` and none is timezone-aware. No freezegun/time-machine is installed
and none is needed.

NOT a replacement for real ``time.monotonic()`` in tests that deliberately
backdate against the live clock (``unit/services/test_ams_presence.py`` documents
why at :689 — freezing the process-wide monotonic also freezes the event loop's
clock).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Literal

import pytest

SleepMode = Literal["delay", "step"]


class FakeClock:
    """A manually advanced ``time.monotonic`` stand-in."""

    __slots__ = ("t", "step", "sleep_mode", "yield_on_sleep", "delays")

    def __init__(
        self,
        start: float = 1000.0,
        *,
        step: float = 1.0,
        sleep_mode: SleepMode = "delay",
        yield_on_sleep: bool = True,
    ) -> None:
        self.t = float(start)
        self.step = float(step)
        self.sleep_mode: SleepMode = sleep_mode
        self.yield_on_sleep = yield_on_sleep
        self.delays: list[float] = []

    # -- reading -----------------------------------------------------------
    def __call__(self) -> float:
        """The ``Callable[[], float]`` seam — ``RetryWindow(clock=...)`` and friends."""
        return self.t

    def now(self) -> float:
        """Bound-method form, for seams that take ``_now`` rather than a callable object."""
        return self.t

    def __getitem__(self, key: str) -> float:
        if key != "t":
            raise KeyError(key)
        return self.t

    # -- advancing ---------------------------------------------------------
    def __setitem__(self, key: str, value: float) -> None:
        if key != "t":
            raise KeyError(key)
        self.t = float(value)

    def advance(self, seconds: float) -> None:
        """Move forward by ``seconds``."""
        self.t += float(seconds)

    def tick(self, step: float | None = None) -> None:
        """Move forward by one configured step (or the given one)."""
        self.t += float(self.step if step is None else step)

    async def sleep(self, delay: float) -> None:
        """Stand in for ``asyncio.sleep`` in code under test. No wall time passes."""
        self.delays.append(delay)
        self.t += delay if self.sleep_mode == "delay" else self.step
        if self.yield_on_sleep:
            # Let the loop interleave, so a test can cancel mid-run exactly as it
            # could against the real sleep.
            await asyncio.sleep(0)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"FakeClock(t={self.t!r}, step={self.step!r}, sleep_mode={self.sleep_mode!r})"


@pytest.fixture
def make_clock() -> Callable[..., FakeClock]:
    """Factory for clocks that need a non-default start, step or sleep mode."""
    return FakeClock


@pytest.fixture
def clock() -> FakeClock:
    """A bare clock at t=1000.0, wired to nothing.

    Pass it where production takes a clock (``RetryWindow(30.0, clock=clock)``),
    or install it over a module-level seam with ``monkeypatch.setattr``.
    """
    return FakeClock()


@pytest.fixture
def retry_window_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """A clock installed over the shared retry-window time source.

    The route for services that build their ``RetryWindow`` at import time, so
    constructor injection is not available to the test. ``step`` is left at the
    default; set ``retry_window_clock.step`` (or call ``tick(seconds)``) to move
    past a specific settle window.
    """
    clock = FakeClock()
    monkeypatch.setattr("backend.app.utils.retry_window.monotonic", clock)
    return clock
