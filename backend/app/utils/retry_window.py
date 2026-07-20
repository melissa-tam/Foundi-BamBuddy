"""Per-key retry window — one implementation of "don't re-fire this yet".

Several AMS-facing flows re-attempt an action on every MQTT push until the
firmware reflects it (bare-tray config pushes, K-profile drift re-applies).
Un-gated, that is one publish per push — a write storm at the worst possible
moment (an identify's tray-state flap re-fires the drift detector on every
message). Each consumer used to hand-roll a ``dict[key] -> monotonic`` plus
inline window arithmetic; this class is that bookkeeping, once.

Semantics: :meth:`allow` is the whole gate — it returns True when the key is
outside its window and STAMPS it in the same step, so a caller can never check
without arming (the drift of the hand-rolled versions). :meth:`clear` forgets a
key (slot emptied, or a caller that wants to bypass the window for one call);
:meth:`reset` drops every key (test hook / shutdown).

State is process-lifetime and in-memory, like every other edge ledger in the
fork: a restart re-allows immediately, which is the safe direction (one extra
attempt, never a suppressed one).
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from time import monotonic


class RetryWindow:
    """Rate-limit repeated attempts on a key to one per ``seconds``.

    ``clock`` is the time indirection: leave it None to read the module-level
    :func:`time.monotonic` at call time (so a test can patch the module global),
    or inject a callable to drive the window deterministically.
    """

    def __init__(self, seconds: float, *, clock: Callable[[], float] | None = None) -> None:
        self._seconds = float(seconds)
        self._clock = clock
        self._stamps: dict[Hashable, float] = {}

    @property
    def seconds(self) -> float:
        """The window width in seconds."""
        return self._seconds

    def _now(self) -> float:
        return self._clock() if self._clock is not None else monotonic()

    def allow(self, key: Hashable) -> bool:
        """True when ``key`` is outside its window — and stamps it as attempted.

        A key whose last stamp is EXACTLY ``seconds`` old is allowed (the window
        is half-open: an attempt is suppressed only while ``now - last <
        seconds``).
        """
        now = self._now()
        last = self._stamps.get(key)
        if last is not None and (now - last) < self._seconds:
            return False
        self._stamps[key] = now
        return True

    def clear(self, key: Hashable) -> None:
        """Forget ``key``'s last attempt — the next :meth:`allow` succeeds."""
        self._stamps.pop(key, None)

    def reset(self) -> None:
        """Forget every key."""
        self._stamps.clear()

    def __contains__(self, key: Hashable) -> bool:
        """True while ``key`` carries a stamp (i.e. an attempt was recorded)."""
        return key in self._stamps
