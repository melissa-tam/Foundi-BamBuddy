"""Cooldown-verified plate-clear monitor — the server-side double gate.

When a job that used an eject profile reaches a terminal *success* status, the
part has been swept off but the ``awaiting_plate_clear`` gate raised at
``on_print_complete`` would otherwise block the queue forever waiting for a
manual confirm. This monitor watches the printer's live ``bed_temper`` (via MQTT,
through ``printer_manager``) and, once the bed is confirmed below the profile's
``cooldown_temp_c``, releases the gate through the same mechanism the manual
"clear plate" endpoint uses — so the next unit dispatches automatically.

Failure/stopped terminal states are left ALONE: the plate is presumed occupied
and the gate stays set for a human to inspect and clear. If the MQTT feed goes
stale mid-watch the watch aborts (leaving the gate set); printer-offline handling
elsewhere is untouched.

The bed-watch loop mirrors ``PrinterManager.wait_for_cooldown`` (the proven
cooldown-wait pattern) but keys on ``"bed"`` and clears the gate on success.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from sqlalchemy import select

from backend.app.core.tasks import spawn_background_task
from backend.app.services.printer_manager import printer_manager

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# Terminal statuses that mean the eject sweep ran to completion. Only these
# auto-release the plate-clear gate; everything else leaves it set.
_SUCCESS_TERMINAL = {"completed"}

# Watch bounds. Cooldown from print temp to ~28 °C can take a while, so the
# window is generous; each tick re-reads the live MQTT bed temp.
_WATCH_TIMEOUT_S = 1800
_CHECK_INTERVAL_S = 10


def should_auto_clear(final_status: str) -> bool:
    """True only for terminal *success* — failures leave the plate gate set."""
    return final_status in _SUCCESS_TERMINAL


async def watch_bed_and_clear(
    printer_id: int,
    threshold_c: float,
    *,
    manager=printer_manager,
    timeout_s: int = _WATCH_TIMEOUT_S,
    check_interval_s: int = _CHECK_INTERVAL_S,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> str:
    """Poll the live bed temperature until it drops below `threshold_c`.

    On success, releases the plate-clear gate via
    ``manager.set_awaiting_plate_clear(printer_id, False)`` and returns
    ``"cleared"``. Returns ``"stale"`` if the printer disconnects / goes stale
    mid-watch (gate left set) and ``"timeout"`` if the bed never cools within the
    window (gate left set). ``manager`` and ``sleep`` are injectable for testing.
    """
    elapsed = 0
    while elapsed < timeout_s:
        state = manager.get_status(printer_id)
        if not state or not getattr(state, "connected", False):
            logger.info("Eject monitor: printer %s stale/offline during cooldown watch — aborting", printer_id)
            return "stale"

        bed_temp = state.temperatures.get("bed")
        if bed_temp is not None and bed_temp <= threshold_c:
            manager.set_awaiting_plate_clear(printer_id, False)
            logger.info(
                "Eject monitor: printer %s bed %.1f°C ≤ %.1f°C — plate-clear gate released",
                printer_id,
                bed_temp,
                threshold_c,
            )
            return "cleared"

        await sleep(check_interval_s)
        elapsed += check_interval_s

    logger.warning(
        "Eject monitor: printer %s bed did not reach %.1f°C within %ss — leaving plate gate set",
        printer_id,
        threshold_c,
        timeout_s,
    )
    return "timeout"


def should_rearm(
    awaiting_plate_clear: bool,
    item_status: str | None,
    eject_profile_id: int | None,
    first_article: bool = False,
) -> bool:
    """Startup re-arm decision for one printer.

    A cooldown watch is re-armed after a restart only when the plate-clear gate
    is still raised AND the most-recently-started job on that printer was a
    successful eject job. Failed/aborted/cancelled jobs (or non-eject jobs)
    never re-arm — the plate is presumed occupied and stays gated for a human.

    First-article items NEVER re-arm even though they carry an eject profile: the
    eject block is deliberately not injected for them (the part stays on the
    plate for inspection), so there is nothing to auto-clear — the gate waits for
    the operator's approve/reject.
    """
    if first_article:
        return False
    return bool(awaiting_plate_clear) and item_status == "completed" and eject_profile_id is not None


async def _latest_started_item(db, printer_id: int):
    """The most-recently-started queue item on `printer_id`, or None."""
    from backend.app.models.print_queue import PrintQueueItem

    result = await db.execute(
        select(PrintQueueItem)
        .where(PrintQueueItem.printer_id == printer_id)
        .where(PrintQueueItem.started_at.is_not(None))
        .order_by(PrintQueueItem.started_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _resolve_eject_threshold(printer_id: int) -> float | None:
    """Return the finished job's eject cooldown threshold, or None if the most
    recently started job on this printer did not use an eject profile."""
    from backend.app.core.database import async_session
    from backend.app.models.eject_profile import EjectProfile

    async with async_session() as db:
        item = await _latest_started_item(db, printer_id)
        if item is None or item.eject_profile_id is None:
            return None
        # First-article items carry an eject profile but their eject block is
        # never injected — the part stays on the plate for inspection, so the
        # gate must NOT auto-clear. Resolve to no-auto-clear explicitly.
        if getattr(item, "first_article", False):
            return None

        prof_result = await db.execute(select(EjectProfile).where(EjectProfile.id == item.eject_profile_id))
        profile = prof_result.scalar_one_or_none()
        if profile is None:
            return None
        return profile.cooldown_temp_c


class EjectCooldownMonitor:
    """Owns the per-printer cooldown watch tasks.

    Watches are armed from two sites that share the same ``_start_watch`` →
    ``_watch`` code path: the terminal-status callback (normal operation) and
    ``rearm_on_startup`` (restart resilience — an app restart mid-cooldown must
    not leave ``awaiting_plate_clear`` set forever and silently stall the farm).
    """

    def __init__(self) -> None:
        # Printers with an in-flight watch, so a duplicate terminal callback
        # doesn't spawn a second watcher for the same finish.
        self._watching: set[int] = set()

    def on_terminal_status(self, printer_id: int, final_status: str) -> None:
        """Hook called once from ``main.on_print_complete`` for every terminal
        status. Spawns a background cooldown watch when the finished job used an
        eject profile and completed successfully; otherwise does nothing (the
        plate-clear gate stays as-is)."""
        if not should_auto_clear(final_status):
            return
        self._start_watch(printer_id)

    async def rearm_on_startup(self) -> int:
        """Re-arm cooldown watches lost to a restart. Returns the count re-armed.

        For every printer whose persisted ``awaiting_plate_clear`` gate is set,
        re-spawn the watch iff the most-recently-started job was a successful
        eject job (see :func:`should_rearm`)."""
        from backend.app.core.database import async_session
        from backend.app.models.printer import Printer

        rearmed = 0
        async with async_session() as db:
            result = await db.execute(select(Printer.id).where(Printer.awaiting_plate_clear.is_(True)))
            gated_printer_ids = [row[0] for row in result.all()]
            for printer_id in gated_printer_ids:
                item = await _latest_started_item(db, printer_id)
                if item is None or not should_rearm(
                    True, item.status, item.eject_profile_id, getattr(item, "first_article", False)
                ):
                    continue
                if self._start_watch(printer_id):
                    rearmed += 1
        if rearmed:
            logger.info("Eject monitor: re-armed %d cooldown watch(es) after restart", rearmed)
        return rearmed

    def _start_watch(self, printer_id: int) -> bool:
        """Spawn the cooldown watch unless one is already in flight."""
        if printer_id in self._watching:
            return False
        self._watching.add(printer_id)
        spawn_background_task(self._watch(printer_id), name=f"eject-cooldown-watch-{printer_id}")
        return True

    async def _watch(self, printer_id: int) -> None:
        try:
            threshold = await _resolve_eject_threshold(printer_id)
            if threshold is None:
                # Not an eject job — leave the manual plate-clear behaviour intact.
                return
            await watch_bed_and_clear(printer_id, threshold)
        except Exception:  # noqa: BLE001 — a watch failure must not crash the callback loop
            logger.exception("Eject monitor: cooldown watch for printer %s failed", printer_id)
        finally:
            self._watching.discard(printer_id)


# Module-level singleton, mirroring the other service singletons.
eject_cooldown_monitor = EjectCooldownMonitor()
