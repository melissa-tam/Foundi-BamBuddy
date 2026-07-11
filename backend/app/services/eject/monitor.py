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

Identity (Phase 1): the auto-clear watch is armed only with a positively
correlated ``queue_item_id`` and resolves its release threshold from THAT item —
never from "the most recently started print on the printer", which let a foreign
or local print lend its threshold to the wrong plate (S4/P1-A). A terminal we
cannot attribute (foreign/none), and a gate whose persisted source we cannot tie
to the eject job on restart, never auto-clear — they wait for a human.
``watch_gate_escalation_only`` covers the foreign-deposit case: it holds the gate
(never releases) and just escalates once, exiting when the operator clears it.
"""

from __future__ import annotations

import asyncio
import functools
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


async def _default_notify_plate_not_empty(printer_id: int, *, source_detail: str = "") -> None:
    """Fire the plate-not-empty notification for a stuck-plate escalation.

    Opens its own session (mirroring the rest of this module) and resolves the
    printer name for the message. ``source_detail`` disambiguates the escalation
    source (Phase 3.3) — the two watches below bake in their own sentence via a
    ``functools.partial`` before handing this to the loop. Kept side-effect-only;
    callers wrap it so a notification failure never kills the watch.
    """
    from backend.app.core.database import async_session
    from backend.app.models.printer import Printer
    from backend.app.services.notification_service import notification_service

    async with async_session() as db:
        result = await db.execute(select(Printer.name).where(Printer.id == printer_id))
        printer_name = result.scalar_one_or_none() or f"printer {printer_id}"
        await notification_service.on_plate_not_empty(printer_id, printer_name, db, source_detail=source_detail)


async def watch_bed_and_clear(
    printer_id: int,
    threshold_c: float,
    *,
    manager=printer_manager,
    escalate_s: int = _WATCH_ESCALATE_S,
    check_interval_s: int = _CHECK_INTERVAL_S,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    notify: Callable[[int], Awaitable[None]] | None = None,
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
    if notify is None:
        # cooldown_timeout source (Phase 3.3): the escalation actually means "bed
        # never reached the release threshold", NOT "objects detected" — say so.
        cooldown_detail = (
            f"Bed still above {threshold_c:.0f} °C after {escalate_s // 60} minutes — a part may remain "
            "on the plate, or the cooldown threshold is set at/below shop ambient."
        )
        notify = functools.partial(_default_notify_plate_not_empty, source_detail=cooldown_detail)
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


async def watch_gate_escalation_only(
    printer_id: int,
    *,
    manager=printer_manager,
    escalate_s: int = _WATCH_ESCALATE_S,
    check_interval_s: int = _CHECK_INTERVAL_S,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    notify: Callable[[int], Awaitable[None]] | None = None,
) -> str:
    """Escalation-only gate watch for a FOREIGN deposit — a terminal print Bambuddy
    did not dispatch that left material on the plate.

    Unlike :func:`watch_bed_and_clear` this NEVER releases the gate: the plate is
    held by an unknown job and only a human clearing it (which NULLs the gate) may
    resume dispatch. Same poll cadence and escalation constant as the cooldown
    watch; fires ONE plate-not-empty notification at ``escalate_s`` then keeps
    polling. Exits ``"cleared"`` when the gate is cleared externally (operator) or
    ``"stale"`` when the printer disconnects/goes stale. ``manager``, ``sleep`` and
    ``notify`` are injectable for testing.
    """
    if notify is None:
        # Foreign-deposit escalation — distinct from the cooldown_timeout source: a
        # print the farm did not dispatch left a part on the plate.
        notify = functools.partial(
            _default_notify_plate_not_empty,
            source_detail="A print the farm did not dispatch left a part on the plate. Clear the bed to resume dispatch.",
        )
    elapsed = 0
    escalated = False
    while True:
        if not manager.is_awaiting_plate_clear(printer_id):
            logger.info(
                "Eject monitor: printer %s foreign-gate cleared externally — escalation watch exiting", printer_id
            )
            return "cleared"

        state = manager.get_status(printer_id)
        if not state or not getattr(state, "connected", False):
            logger.info("Eject monitor: printer %s stale/offline during foreign-gate watch — aborting", printer_id)
            return "stale"

        if not escalated and elapsed >= escalate_s:
            escalated = True
            logger.warning(
                "Eject monitor: printer %s foreign deposit still gated after %ss — escalating "
                "(plate-not-empty), gate stays set until an operator clears it",
                printer_id,
                escalate_s,
            )
            try:
                await notify(printer_id)
            except Exception:  # noqa: BLE001 — a notify failure must not kill the watch
                logger.exception("Eject monitor: foreign-gate plate-not-empty notify for printer %s failed", printer_id)

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


async def _resolve_eject_threshold(queue_item_id: int) -> float | None:
    """Return the eject cooldown threshold for the queue item that raised the gate,
    or None if that item did not use an eject profile (nothing to auto-clear).

    Resolved from the SPECIFIC item bound to this watch (``db.get``) — not the most
    recently started item on the printer — so a foreign/local print that finished
    after the farm unit can never lend its threshold to the wrong plate (S4/P1-A).

    The run-level ``PrintBatch.cooldown_temp_c_override`` wins over the profile's
    ``cooldown_temp_c`` (same precedence dispatch/generator/validator apply to
    the emitted ``M190 R``) so the server-side release gate matches the threshold
    the in-file cooldown wait was generated with."""
    from backend.app.core.database import async_session
    from backend.app.models.eject_profile import EjectProfile
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.services.eject.dispatch import resolve_cooldown_override

    async with async_session() as db:
        item = await db.get(PrintQueueItem, queue_item_id)
        if item is None or item.eject_profile_id is None:
            return None
        # First-article items carry an eject profile but their eject block is
        # never injected — the part stays on the plate for inspection, so the
        # gate must NOT auto-clear. Resolve to no-auto-clear explicitly.
        if getattr(item, "first_article", False):
            return None

        profile = await db.get(EjectProfile, item.eject_profile_id)
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
        # printer_id -> release threshold (°C) of the in-flight watch, so a
        # duplicate terminal callback doesn't spawn a second watcher for the
        # same finish AND the UI can surface the cooldown phase (Phase 4.3c).
        # None = watch armed but with no release threshold: either the
        # escalation-only foreign-gate watch, or a cooldown watch still
        # resolving its item's threshold.
        self._watching: dict[int, float | None] = {}

    def active_watch(self, printer_id: int) -> float | None:
        """The in-flight cooldown watch's release threshold (°C), or None.

        None both when no watch is armed and when the armed watch carries no
        threshold (escalation-only / still resolving) — callers surface the
        cooldown phase only when a real release temperature exists."""
        return self._watching.get(printer_id)

    def on_terminal_status(self, printer_id: int, final_status: str, queue_item_id: int | None = None) -> None:
        """Hook called once from ``main.on_print_complete`` for every terminal status.

        Arms a background cooldown watch ONLY when the finished job was positively
        correlated to a queue item (``queue_item_id`` is not None) AND completed
        successfully; the watch then keys its release threshold off THAT item. A
        terminal that could not be attributed to the dispatched unit (foreign/none,
        or an upgrade-day NULL-key fallback → ``queue_item_id`` None) never
        auto-clears — the plate-clear gate stays set for a human."""
        if not should_auto_clear(final_status) or queue_item_id is None:
            return
        self._start_watch(printer_id, queue_item_id)

    def start_escalation_only_watch(self, printer_id: int) -> bool:
        """Arm the foreign-deposit gate watch: holds the gate (never auto-clears),
        escalates once, and exits when the operator clears it or the printer goes
        stale. Deduped against any in-flight watch on the same printer."""
        if printer_id in self._watching:
            return False
        # Sentinel None: the foreign-gate watch never releases, so it exposes
        # no cooldown threshold to the UI.
        self._watching[printer_id] = None
        spawn_background_task(self._escalation_only(printer_id), name=f"eject-gate-escalation-{printer_id}")
        return True

    async def rearm_on_startup(self) -> int:
        """Re-arm cooldown watches lost to a restart. Returns the count re-armed.

        For every printer whose persisted ``awaiting_plate_clear`` gate is set,
        re-spawn the watch iff the most-recently-started job was a successful eject
        job (see :func:`should_rearm`) AND that item's ``dispatch_subtask_id``
        matches the printer's persisted ``plate_gate_subtask_id`` (both non-null).
        A gate whose source we cannot positively tie to the eject job — a foreign
        deposit, or a pre-migration NULL-source gate — never auto-clears on restart;
        it is left set for a human."""
        from backend.app.core.database import async_session
        from backend.app.models.printer import Printer

        rearmed = 0
        async with async_session() as db:
            result = await db.execute(
                select(Printer.id, Printer.plate_gate_subtask_id).where(Printer.awaiting_plate_clear.is_(True))
            )
            for printer_id, gate_subtask_id in result.all():
                item = await _latest_started_item(db, printer_id)
                if item is None or not should_rearm(
                    True, item.status, item.eject_profile_id, getattr(item, "first_article", False)
                ):
                    continue
                if not gate_subtask_id or item.dispatch_subtask_id != gate_subtask_id:
                    logger.info(
                        "Eject monitor: printer %s gate NOT re-armed — source subtask %r != last dispatch %r "
                        "(left gated for manual clear)",
                        printer_id,
                        gate_subtask_id,
                        item.dispatch_subtask_id,
                    )
                    continue
                if self._start_watch(printer_id, item.id):
                    rearmed += 1
        if rearmed:
            logger.info("Eject monitor: re-armed %d cooldown watch(es) after restart", rearmed)
        return rearmed

    def _start_watch(self, printer_id: int, queue_item_id: int) -> bool:
        """Spawn the identity-bound cooldown watch unless one is already in flight."""
        if printer_id in self._watching:
            return False
        # Threshold not resolved yet (needs the item's profile/override) — the
        # spawned watch records it once known so active_watch() can expose it.
        self._watching[printer_id] = None
        spawn_background_task(self._watch(printer_id, queue_item_id), name=f"eject-cooldown-watch-{printer_id}")
        return True

    async def _watch(self, printer_id: int, queue_item_id: int) -> None:
        try:
            threshold = await _resolve_eject_threshold(queue_item_id)
            if threshold is None:
                # Not an eject job — leave the manual plate-clear behaviour intact.
                return
            self._watching[printer_id] = threshold
            await watch_bed_and_clear(printer_id, threshold)
        except Exception:  # noqa: BLE001 — a watch failure must not crash the callback loop
            logger.exception("Eject monitor: cooldown watch for printer %s failed", printer_id)
        finally:
            self._watching.pop(printer_id, None)

    async def _escalation_only(self, printer_id: int) -> None:
        try:
            await watch_gate_escalation_only(printer_id)
        except Exception:  # noqa: BLE001 — a watch failure must not crash the callback loop
            logger.exception("Eject monitor: foreign-gate escalation watch for printer %s failed", printer_id)
        finally:
            self._watching.pop(printer_id, None)


# Module-level singleton, mirroring the other service singletons.
eject_cooldown_monitor = EjectCooldownMonitor()
