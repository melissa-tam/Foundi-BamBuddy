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

# Watch bounds. Cooldown from print temp to the release threshold can take a
# while, so each tick re-reads the live MQTT bed temp.
_CHECK_INTERVAL_S = 20
# Escalation, not a stop: if the bed is still above threshold after this long we
# warn + notify ONCE, then keep polling. The watch only ever exits on release
# ("cleared") or printer stale/offline ("stale") — a permanent stop here would
# strand the plate-clear gate and silently stall the farm.
_WATCH_ESCALATE_S = 5400


def should_auto_clear(final_status: str) -> bool:
    """True only for terminal *success* — failures leave the plate gate set."""
    return final_status in _SUCCESS_TERMINAL


def deposited_nothing(*, is_dry_run: bool, last_layer_num: int | None, last_progress: float | None) -> bool:
    """A terminal job left nothing on the plate: a dry-run eject (never deposits), OR a print
    that reached terminal having produced zero layers AND zero progress. Uses the reset-surviving
    peaks from the on_print_complete payload."""
    return bool(is_dry_run) or ((last_layer_num or 0) == 0 and (last_progress or 0) == 0)


async def _default_notify_plate_not_empty(printer_id: int) -> None:
    """Fire the plate-not-empty notification for a stuck-plate escalation.

    Opens its own session (mirroring the rest of this module) and resolves the
    printer name for the message. Kept side-effect-only; callers wrap it so a
    notification failure never kills the watch.
    """
    from backend.app.core.database import async_session
    from backend.app.models.printer import Printer
    from backend.app.services.notification_service import notification_service

    async with async_session() as db:
        result = await db.execute(select(Printer.name).where(Printer.id == printer_id))
        printer_name = result.scalar_one_or_none() or f"printer {printer_id}"
        await notification_service.on_plate_not_empty(printer_id, printer_name, db)


async def watch_bed_and_clear(
    printer_id: int,
    threshold_c: float,
    *,
    manager=printer_manager,
    escalate_s: int = _WATCH_ESCALATE_S,
    check_interval_s: int = _CHECK_INTERVAL_S,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    notify: Callable[[int], Awaitable[None]] = _default_notify_plate_not_empty,
) -> str:
    """Poll the live bed temperature until it drops below `threshold_c`.

    On release, clears the plate-clear gate via
    ``manager.set_awaiting_plate_clear(printer_id, False)`` and returns
    ``"cleared"``. Returns ``"stale"`` if the printer disconnects / goes stale
    mid-watch (gate left set) — the only other exit.

    The watch never stops on time. If the bed is still above ``threshold_c``
    after ``escalate_s`` elapsed, it emits ONE warning and fires ``notify`` (the
    plate-not-empty escalation) — tolerating any notify failure — then keeps
    polling so the gate still releases within one tick of the temp finally
    crossing. ``manager``, ``sleep`` and ``notify`` are injectable for testing.
    """
    elapsed = 0
    escalated = False
    while True:
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

        if not escalated and elapsed >= escalate_s:
            escalated = True
            logger.warning(
                "Eject monitor: printer %s bed still above %.1f°C after %ss — escalating "
                "(plate-not-empty), gate stays set but watch continues",
                printer_id,
                threshold_c,
                escalate_s,
            )
            try:
                await notify(printer_id)
            except Exception:  # noqa: BLE001 — a notify failure must not kill the watch
                logger.exception("Eject monitor: plate-not-empty escalation notify for printer %s failed", printer_id)

        await sleep(check_interval_s)
        elapsed += check_interval_s


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
    recently started job on this printer did not use an eject profile.

    The run-level ``PrintBatch.cooldown_temp_c_override`` wins over the profile's
    ``cooldown_temp_c`` (same precedence dispatch/generator/validator apply to
    the emitted ``M190 R``) so the server-side release gate matches the threshold
    the in-file cooldown wait was generated with."""
    from backend.app.core.database import async_session
    from backend.app.models.eject_profile import EjectProfile
    from backend.app.services.eject.dispatch import resolve_cooldown_override

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
        override = await resolve_cooldown_override(db, item.batch_id)
        return override if override is not None else profile.cooldown_temp_c


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
