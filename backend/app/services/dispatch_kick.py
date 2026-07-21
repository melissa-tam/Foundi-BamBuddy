"""Event-driven wakeup for the single print-scheduler loop (latency Phase A).

The scheduler normally waits out a fixed polling interval between passes, so work
that arrives just after a tick (an enqueue, a manual start, a plate-gate release,
a freed printer) sits idle for up to the whole interval. A *kick* lets any
mutation that creates dispatchable work wake the loop immediately; the interval
becomes a fallback safety-net poll rather than the primary cadence.

Design constraints (all load-bearing):

* ``kick()`` is synchronous and callable from ANY thread — routes/services run on
  the event loop, but MQTT/paho callbacks run on their own thread — so it
  marshals the ``Event.set`` onto the captured loop via ``call_soon_threadsafe``.
* The loop is captured lazily on the first ``wait()`` (the consumer side), so a
  kick fired before the scheduler has started sets the event directly and is
  retained (``asyncio.Event`` is level-triggered) until that first ``wait()``.
* This is a LEAF module: it imports nothing from the scheduler, routes or main —
  only stdlib + logging — so every producer can lazily import it with no risk of
  an import cycle back through the scheduler.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter, deque

logger = logging.getLogger(__name__)

# (monotonic_ts, reason, printer_id) — one entry per kick, oldest evicted.
KickRecord = tuple[float, str, "int | None"]

_RING_MAXLEN = 50


class DispatchKick:
    """Coalescing wakeup signal shared by the scheduler and every dispatch producer."""

    def __init__(self) -> None:
        self._event: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._reasons: deque[KickRecord] = deque(maxlen=_RING_MAXLEN)

    def kick(self, reason: str, printer_id: int | None = None) -> None:
        """Wake the scheduler loop. Sync; safe to call from any thread.

        Records the reason in the bounded ring buffer (for the coalesced wake
        log) and sets the internal event. When called off the loop thread the
        ``Event.set`` is marshalled with ``call_soon_threadsafe``; when the loop
        has not been captured yet (kick before the first ``wait()``) the event is
        set directly and its level-triggered state carries the wake forward.
        """
        self._reasons.append((time.monotonic(), reason, printer_id))
        logger.debug("dispatch kick: reason=%s printer_id=%s", reason, printer_id)

        event = self._event
        if event is None:
            # No consumer has waited yet — create the event now so this early
            # wake is not lost before the scheduler's first wait().
            event = self._event = asyncio.Event()

        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                # Loop closed underneath us (shutdown race) — nothing to wake.
                pass
        else:
            event.set()

    async def wait(self, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds for a kick.

        Returns ``True`` when woken by a kick, ``False`` on timeout. Captures the
        running loop on first call so off-loop ``kick()`` calls can marshal onto
        it. Does NOT clear the event — the consumer clears explicitly after
        draining, so a kick landing between the clear and the next wait is not
        lost.
        """
        self._loop = asyncio.get_running_loop()
        event = self._event
        if event is None:
            event = self._event = asyncio.Event()
        if event.is_set():
            return True
        try:
            await asyncio.wait_for(event.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def clear(self) -> None:
        """Reset the event so the next wait blocks until a fresh kick."""
        if self._event is not None:
            self._event.clear()

    def drain_reasons(self) -> list[KickRecord]:
        """Snapshot and clear the ring buffer of kicks accumulated since the last drain."""
        records = list(self._reasons)
        self._reasons.clear()
        return records

    @staticmethod
    def summarize(records: list[KickRecord]) -> str:
        """Coalesce drained kick records into a compact reason string.

        e.g. ``[(_, "enqueue", _), (_, "enqueue", _), (_, "plate_gate_release", _)]``
        → ``"enqueue x2, plate_gate_release"``. Empty (a spurious wake) → ``"unknown"``.
        """
        if not records:
            return "unknown"
        counts = Counter(reason for _ts, reason, _pid in records)
        return ", ".join(f"{reason} x{n}" if n > 1 else reason for reason, n in counts.items())


# Module-level singleton — the one wakeup shared across the process.
dispatch_kick = DispatchKick()
