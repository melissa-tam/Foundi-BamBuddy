"""Cooldown → eject monitor — the server-side cooldown policy engine.

When a farm unit reaches a terminal *success* status the ``awaiting_plate_clear``
gate raised at ``on_print_complete`` blocks the queue until the part is swept off.
The sweep is now a SEPARATE, server-dispatched motion-only job (print files ship
unmodified). This monitor watches the printer's live ``bed_temper`` (via MQTT,
through ``printer_manager``) and enacts the policy:

- bed ≤ the release threshold → dispatch a part-present eject job (``on_release``);
- the bed cooling PLATEAUS (cools < epsilon across two consecutive windows) →
  either RELEASE (the bed equilibrated within the eject margin of the threshold —
  an asymptotic settle just above ambient) or QUARANTINE with NO eject (genuinely
  stuck hot, above threshold + margin);
- still above threshold at a max-hold cap → dispatch the eject anyway.

The monitor NO LONGER clears the plate gate: the gate drops only when the eject
job's own terminal arrives (matched by subtask echo in ``farm_policy.on_terminal``).
Failure/stopped terminal states never arm a watch. The watch's lifetime is the
plate-clear PHASE, not connectivity: a disconnected/unreadable MQTT tick is treated
as an unreadable bed and the watch keeps polling; it ends when the gate clears
(the eject terminal or an operator) or on a bounded stop (plateau / cap / release).

Identity (Phase 1): the watch is armed only with a positively correlated
``queue_item_id`` and resolves its release threshold + eject target from THAT item —
never from "the most recently started print on the printer" (S4/P1-A). A terminal
we cannot attribute, and a gate whose persisted source we cannot tie to the eject
job on restart, never auto-eject — they wait for a human.
``watch_gate_escalation_only`` covers the foreign-deposit case: it holds the gate
(never releases) and just escalates once, exiting when the operator clears it.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from backend.app.core.tasks import spawn_background_task
from backend.app.schemas.settings import AppSettings
from backend.app.services.printer_manager import printer_manager

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


@dataclass
class _ActiveWatch:
    """The in-flight cooldown watch's identity + manual-release channel (W2).

    Stored as the ``_watching`` value for an armed cooldown/FA watch (the
    escalation-only foreign-gate watch and a still-resolving watch keep the ``None``
    sentinel). ``active_watch()`` still exposes only ``threshold_c`` — the UI payload
    is unchanged. ``release_now`` is set by ``request_release_now`` so a manual
    "Eject now" fires the watch's single ``_do_release`` path instantly (no parallel
    dispatch race)."""

    threshold_c: float | None
    # None for the auto FOREIGN-plate eject watch (purpose="foreign"): it releases
    # against a directly-passed threshold + eject profile, not a queue item.
    queue_item_id: int | None
    purpose: str
    release_now: asyncio.Event


# Fallbacks for direct watch_bed_and_clear callers (tests / manual arms) — derived
# from the settings schema so the defaults have exactly one declared origin.
_STALL_EPSILON_FALLBACK = float(AppSettings.model_fields["farm_cooldown_stall_epsilon_c"].default)
_PLATEAU_MARGIN_FALLBACK = float(AppSettings.model_fields["farm_cooldown_plateau_eject_margin_c"].default)

# Terminal statuses that mean the eject sweep ran to completion. Only these
# auto-release the plate-clear gate; everything else leaves it set.
_SUCCESS_TERMINAL = {"completed"}

# Watch bounds. Cooldown from print temp to the release threshold can take a
# while, so each tick re-reads the live MQTT bed temp.
_CHECK_INTERVAL_S = 20
# Escalation, not a stop: if the bed is still above threshold after this long we
# warn + notify ONCE (the dedicated cooldown_escalation event — NOT plate_not_empty),
# then keep polling. The watch exits on eject dispatch ("released"), a cooling
# plateau / triple dispatch failure ("stalled"), or the plate-clear gate being
# cleared mid-watch ("cleared") — never a bare timeout that would strand the plate
# gate and silently stall the farm. A disconnected/unreadable tick does NOT end the
# watch (its lifetime is the gated phase, not connectivity). The plateau watchdog +
# max-hold cap (settings-driven) are the bounded stops; escalation only nudges.
_WATCH_ESCALATE_S = 5400

# Startup reconcile of pending ejects whose terminal was missed during downtime
# (W1.2): poll each hydrated printer until it reconnects, up to the max wait, then
# act on the live state. Named constants per the ``_CHECK_INTERVAL_S`` precedent.
_RECONCILE_POLL_S = 20
_RECONCILE_MAX_WAIT_S = 900


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


async def _default_notify_cooldown_escalation(
    printer_id: int, *, bed_c: float | None, threshold_c: float, max_hold_s: int
) -> None:
    """Fire the dedicated cooldown-escalation notification for a long-running cooldown.

    The cooldown watch's escalation means "the bed has not reached the release
    threshold yet", NOT "objects on the plate" — so it uses the truthful
    ``cooldown_escalation`` event, never ``plate_not_empty``. ``bed_c`` is the live
    bed at fire time (None if unreadable that tick); ``max_hold_s`` is converted to
    the minute-valued cap for the message (0 → "no cap"). Opens its own session
    (mirroring the rest of this module); callers wrap it so a notification failure
    never kills the watch.
    """
    from backend.app.core.database import async_session
    from backend.app.models.printer import Printer
    from backend.app.services.notification_service import notification_service

    async with async_session() as db:
        result = await db.execute(select(Printer.name).where(Printer.id == printer_id))
        printer_name = result.scalar_one_or_none() or f"printer {printer_id}"
        await notification_service.on_cooldown_escalation(
            printer_id,
            printer_name,
            bed_c=bed_c,
            threshold_c=threshold_c,
            max_hold_minutes=(max_hold_s // 60) if max_hold_s else 0,
            db=db,
        )


async def watch_bed_and_clear(
    printer_id: int,
    threshold_c: float,
    *,
    manager=printer_manager,
    escalate_s: int = _WATCH_ESCALATE_S,
    check_interval_s: int = _CHECK_INTERVAL_S,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    notify: Callable[[int], Awaitable[None]] | None = None,
    stall_window_s: int = 0,
    stall_epsilon_c: float = _STALL_EPSILON_FALLBACK,
    plateau_eject_margin_c: float = _PLATEAU_MARGIN_FALLBACK,
    max_hold_s: int = 0,
    on_release: Callable[[], Awaitable[None]] | None = None,
    on_stall: Callable[[str], Awaitable[None]] | None = None,
    release_now: asyncio.Event | None = None,
) -> str:
    """Poll the live bed temperature and enact the cooldown → eject policy.

    The monitor NO LONGER clears the plate gate — the gate drops only when the
    server-dispatched eject job's terminal arrives. Instead, on the release/stall
    conditions below it invokes the injected coroutines and returns a verdict:

    * ``"released"`` — the bed reached ``threshold_c`` (or the ``max_hold_s`` cap
      forced it while still hot; or a plateau settled WITHIN ``plateau_eject_margin_c``
      of the threshold — the bed equilibrated just above ambient): ``on_release()``
      was awaited to dispatch the part-present eject. If ``on_release`` raises it is
      retried on the next poll; after THREE consecutive dispatch failures the watch
      falls to the stall path (``on_stall("eject dispatch failed ×3")``) and returns
      ``"stalled"`` instead.
    * ``"stalled"`` — the bed cooling PLATEAUED while still MORE than
      ``plateau_eject_margin_c`` above the threshold (genuinely stuck hot): across
      two consecutive ``stall_window_s`` windows it failed to cool by at least
      ``stall_epsilon_c`` (a rise counts as a strike). ``on_stall(reason)`` was
      awaited; the plate gate is left SET. Skipped entirely when
      ``stall_window_s == 0`` or ``on_stall`` is None; a None bed reading never strikes.
    * ``"cleared"`` — the plate-clear gate was cleared mid-watch (the eject job's own
      terminal, or an operator): the phase is over, so the watch exits WITHOUT
      dispatching (never ejects onto an already-cleared plate).

    A disconnected/unreadable printer tick is NOT a terminal condition: it is treated
    as an unreadable bed (bed=None) and the watch keeps polling — its lifetime is the
    gated phase, ended by the gate-clear check at the top of each poll.

    The plateau anchor is the first readable bed sample, held across strike windows
    and re-anchored whenever a window DID cool by ≥ epsilon. At a boundary that is
    both a plateau boundary and the cap, the plateau is evaluated FIRST.

    Escalation is unchanged in timing: still above ``threshold_c`` after
    ``escalate_s`` fires ONE ``notify`` (the ``cooldown_escalation`` event, NOT
    plate_not_empty; failures tolerated) with the live bed, then keeps polling.
    ``manager``, ``sleep``, ``notify``, ``on_release`` and ``on_stall`` are all
    injectable for testing.
    """
    if notify is None:
        # The escalation means "bed never reached the release threshold", NOT
        # "objects detected" — fire the dedicated cooldown_escalation event with
        # the live bed at fire time (bound below).
        notify = functools.partial(_default_notify_cooldown_escalation, threshold_c=threshold_c, max_hold_s=max_hold_s)

    plateau_enabled = stall_window_s > 0 and on_stall is not None
    elapsed = 0
    escalated = False
    anchor: float | None = None  # first readable bed sample, held across strikes
    strikes = 0
    next_boundary = stall_window_s  # first plateau comparison point
    release_failures = 0

    async def _do_release(cause: str) -> str:
        """Dispatch the eject (``on_release``) with retry/stall handling.

        Returns ``"released"`` on success (or when no dispatcher is wired),
        ``"retry"`` to keep polling after a dispatch failure (< 3 so far),
        ``"stalled"`` once THREE consecutive failures trip the stall path, or
        ``"cleared"`` when the plate-clear gate dropped between the top-of-poll check
        and here (W2/W3 hardening — never sweep an already-emptied plate)."""
        nonlocal release_failures
        # Re-check the gate at the release boundary: the escalate/plateau/manual
        # branches can reach here after an await, during which an operator (or the
        # eject's own terminal) may have cleared the gate. Sweeping then would eject
        # onto an empty plate.
        if not manager.is_awaiting_plate_clear(printer_id):
            logger.info(
                "Eject monitor: printer %s plate-clear gate cleared at release boundary (cause=%s) — no eject",
                printer_id,
                cause,
            )
            return "cleared"
        if on_release is None:
            return "released"
        try:
            await on_release()
        except Exception:  # noqa: BLE001 — a dispatch failure retries, never kills the watch
            release_failures += 1
            logger.exception(
                "Eject monitor: printer %s eject dispatch failed (attempt %d, cause=%s)",
                printer_id,
                release_failures,
                cause,
            )
            if release_failures >= 3:
                if on_stall is not None:
                    try:
                        await on_stall("eject dispatch failed ×3")
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "Eject monitor: on_stall after 3 dispatch failures raised (printer %s)", printer_id
                        )
                return "stalled"
            return "retry"
        logger.info("Eject monitor: printer %s eject dispatched (cause=%s)", printer_id, cause)
        return "released"

    while True:
        # Phase boundary: the moment the plate-clear gate drops — the eject job's own
        # terminal, or an operator clearing the plate — the phase is over. Exit WITHOUT
        # dispatching so we never sweep an already-cleared plate (mirrors the foreign
        # gate watch). Checked at the TOP of every poll, before reading the bed.
        if not manager.is_awaiting_plate_clear(printer_id):
            logger.info(
                "Eject monitor: printer %s plate-clear gate cleared mid-cooldown — watch exiting (no eject)",
                printer_id,
            )
            return "cleared"

        # W2: an operator "Eject now" during an armed watch sets release_now → sweep
        # immediately through the SAME _do_release path (no parallel dispatch race),
        # bypassing the cooldown threshold. The hot-bed allowance is enforced upstream
        # in the manual-eject service before the event is ever set.
        if release_now is not None and release_now.is_set():
            release_now.clear()
            outcome = await _do_release("manual")
            if outcome != "retry":
                return outcome

        state = manager.get_status(printer_id)
        # A disconnected / missing status this tick = an unreadable bed, NOT a stop:
        # bed=None falls through the None-safe branches below and the watch keeps
        # polling. Its lifetime is the gated phase (above), not connectivity.
        if not state or not getattr(state, "connected", False):
            bed_temp = None
        else:
            bed_temp = state.temperatures.get("bed")
        if anchor is None and bed_temp is not None:
            anchor = bed_temp

        if bed_temp is not None and bed_temp <= threshold_c:
            logger.info(
                "Eject monitor: printer %s bed %.1f°C ≤ %.1f°C — dispatching part-present eject",
                printer_id,
                bed_temp,
                threshold_c,
            )
            outcome = await _do_release("threshold")
            if outcome != "retry":
                return outcome
        else:
            # Still above threshold (or unreadable). Escalate-once, then evaluate
            # the plateau watchdog and finally the max-hold cap.
            if not escalated and elapsed >= escalate_s:
                escalated = True
                logger.warning(
                    "Eject monitor: printer %s bed still above %.1f°C after %ss — escalating "
                    "(cooldown running long), watch continues",
                    printer_id,
                    threshold_c,
                    escalate_s,
                )
                try:
                    await notify(printer_id, bed_c=bed_temp)
                except Exception:  # noqa: BLE001 — a notify failure must not kill the watch
                    logger.exception("Eject monitor: cooldown-escalation notify for printer %s failed", printer_id)

            # Plateau FIRST at a boundary (so a boundary that also satisfies the cap
            # stalls rather than ejects onto a bed that refuses to cool).
            if plateau_enabled and elapsed >= next_boundary:
                next_boundary += stall_window_s
                if bed_temp is not None and anchor is not None:  # None bed never strikes
                    if anchor - bed_temp < stall_epsilon_c:  # < epsilon (a rise → negative → strikes)
                        strikes += 1
                        if strikes >= 2:
                            # Two-armed plateau: a bed that asymptotically settles just
                            # above the threshold (within the eject margin) has reached
                            # ambient — RELEASE it, don't quarantine. Only a bed still
                            # genuinely hot (> threshold + margin) is a real stall.
                            if bed_temp <= threshold_c + plateau_eject_margin_c:
                                logger.warning(
                                    "Eject monitor: printer %s cooling plateaued at %.1f°C, within %.1f°C of the "
                                    "%.1f°C threshold — releasing (near-threshold equilibrium), NO quarantine",
                                    printer_id,
                                    bed_temp,
                                    plateau_eject_margin_c,
                                    threshold_c,
                                )
                                outcome = await _do_release("plateau_near_threshold")
                                if outcome != "retry":
                                    return outcome
                                # dispatch failed (< 3 so far) — keep polling; the next
                                # boundary re-strikes and retries the release.
                            else:
                                logger.warning(
                                    "Eject monitor: printer %s cooling plateaued (<%.2f°C over 2 windows) and bed "
                                    "%.1f°C is still >%.1f°C above the %.1f°C threshold — quarantining, NO eject",
                                    printer_id,
                                    stall_epsilon_c,
                                    bed_temp,
                                    plateau_eject_margin_c,
                                    threshold_c,
                                )
                                try:
                                    await on_stall(
                                        f"bed cooling plateaued (<{stall_epsilon_c:g}°C over "
                                        f"two {stall_window_s // 60}-minute windows)"
                                    )
                                except Exception:  # noqa: BLE001
                                    logger.exception("Eject monitor: plateau on_stall raised (printer %s)", printer_id)
                                return "stalled"
                    else:
                        strikes = 0
                        anchor = bed_temp  # re-anchor: this window DID cool

            if max_hold_s > 0 and elapsed >= max_hold_s and bed_temp is not None:
                logger.warning(
                    "Eject monitor: printer %s still %.1f°C above %.1f°C at the %ss max-hold cap — "
                    "dispatching eject anyway",
                    printer_id,
                    bed_temp,
                    threshold_c,
                    max_hold_s,
                )
                outcome = await _do_release("max_hold_cap")
                if outcome != "retry":
                    return outcome

        if release_now is not None:
            # Event-aware wait so a manual release wakes the poll instantly instead of
            # after the full interval; a timeout is the normal "kept cooling" path.
            try:
                await asyncio.wait_for(release_now.wait(), timeout=check_interval_s)
            except (asyncio.TimeoutError, TimeoutError):
                pass
        else:
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
    polling. Exits ``"cleared"`` when the gate is cleared externally (operator).

    A disconnected/stale tick does NOT end the watch — its lifetime is the gated
    PHASE, not connectivity (mirroring ``watch_bed_and_clear``'s unreadable-bed
    tolerance). A printer that drops off mid-hold and comes back must still find its
    stranded plate escalating, not silently un-watched. ``manager``, ``sleep`` and
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

    First-article items NEVER re-arm even though they carry an eject profile: a
    finished first article holds on the plate for operator inspection, so no
    production cooldown watch may auto-eject it — the gate waits for the
    operator's approve/reject (an approve-with-eject arms its own FA watch).
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


async def _resolve_eject_threshold(queue_item_id: int, *, for_first_article: bool = False) -> float | None:
    """Return the eject cooldown threshold for the queue item that raised the gate,
    or None if that item did not use an eject profile (nothing to auto-clear).

    Resolved from the SPECIFIC item bound to this watch (``db.get``) — not the most
    recently started item on the printer — so a foreign/local print that finished
    after the farm unit can never lend its threshold to the wrong plate (S4/P1-A).

    The run-level ``PrintBatch.cooldown_temp_c_override`` wins over the profile's
    ``cooldown_temp_c`` — the single release-threshold precedence shared by the
    whole server-side eject policy.

    ``for_first_article=False`` (production watch) resolves FA items to None: a
    finished first article holds for operator approval, never auto-ejects.
    ``for_first_article=True`` is the approve-with-remote-eject path, where the
    operator HAS approved — the FA guard is skipped so the approved plate gets a
    real threshold for its deferred (cooldown-gated) eject."""
    from backend.app.core.database import async_session
    from backend.app.models.eject_profile import EjectProfile
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.services.eject.dispatch import resolve_cooldown_override

    async with async_session() as db:
        item = await db.get(PrintQueueItem, queue_item_id)
        if item is None or item.eject_profile_id is None:
            return None
        # First-article items carry an eject profile but must hold on the plate
        # for inspection — the production watch must NOT auto-eject them. Only
        # the explicit post-approval FA path may resolve their threshold.
        if getattr(item, "first_article", False) and not for_first_article:
            return None

        profile = await db.get(EjectProfile, item.eject_profile_id)
        if profile is None:
            return None
        override = await resolve_cooldown_override(db, item.batch_id)
        return override if override is not None else profile.cooldown_temp_c


async def _setting_num(db, key: str, default, cast):
    """get_setting(key) parsed with ``cast``, falling back to ``default``."""
    from backend.app.api.routes.settings import get_setting

    raw = await get_setting(db, key)
    if raw is None:
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return default


async def _resolve_stall_settings() -> tuple[int, float, int, float]:
    """Resolve ``(stall_window_s, stall_epsilon_c, max_hold_s, plateau_eject_margin_c)``
    from farm settings.

    Read ONCE at watch arm. Fallbacks come from the ``AppSettings`` schema field
    defaults (the single origin — no mirrored literals here). Minute-valued
    settings are converted to seconds. ``window == 0`` disables the plateau
    watchdog; ``max_hold == 0`` disables the cap. ``plateau_eject_margin_c`` is the
    °C-above-threshold band inside which a plateaued bed is RELEASED rather than
    quarantined (equilibrated at ambient).

    A settings-store failure (DB unavailable at arm time) must NOT kill the
    watch — a dead watch strands the plate-clear gate and silently stalls the
    farm. On any read failure the schema defaults apply and the watch arms
    with the default policy.
    """
    from backend.app.core.database import async_session

    fields = AppSettings.model_fields
    window_min = fields["farm_cooldown_stall_window_minutes"].default
    epsilon = fields["farm_cooldown_stall_epsilon_c"].default
    max_hold_min = fields["farm_cooldown_max_hold_minutes"].default
    margin = fields["farm_cooldown_plateau_eject_margin_c"].default
    try:
        async with async_session() as db:
            window_min = await _setting_num(db, "farm_cooldown_stall_window_minutes", window_min, int)
            epsilon = await _setting_num(db, "farm_cooldown_stall_epsilon_c", epsilon, float)
            max_hold_min = await _setting_num(db, "farm_cooldown_max_hold_minutes", max_hold_min, int)
            margin = await _setting_num(db, "farm_cooldown_plateau_eject_margin_c", margin, float)
    except Exception:  # noqa: BLE001 — arm with defaults rather than strand the gate
        logger.exception("Eject monitor: cooldown stall settings read failed — arming with schema defaults")
    return int(window_min) * 60, float(epsilon), int(max_hold_min) * 60, float(margin)


async def _dispatch_production_eject(*, printer_id: int, queue_item_id: int) -> None:
    """``on_release`` action: dispatch the part-present motion-only eject for the
    finished unit through the shared dispatcher.

    Opens its own session, resolves the unit's run, and hands off to
    ``eject.remote.dispatch_part_present_eject(purpose="production")``. RAISES on
    any failure so :func:`watch_bed_and_clear` counts a dispatch failure (retry,
    then stall after three)."""
    from backend.app.core.database import async_session
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.services.eject import remote

    async with async_session() as db:
        item = await db.get(PrintQueueItem, queue_item_id)
        run_id = item.batch_id if item is not None else None
        await remote.dispatch_part_present_eject(
            db,
            printer_id=printer_id,
            queue_item_id=queue_item_id,
            purpose="production",
            run_id=run_id,
        )


async def _dispatch_fa_eject(*, printer_id: int, queue_item_id: int, run_id: int | None) -> None:
    """``on_release`` action for an approved first article: dispatch its part-present
    eject through the shared dispatcher once the bed has reached the release
    threshold. RAISES on failure so the watch retries then stalls (same policy as
    the production release)."""
    from backend.app.core.database import async_session
    from backend.app.services.eject import remote

    async with async_session() as db:
        await remote.dispatch_part_present_eject(
            db,
            printer_id=printer_id,
            queue_item_id=queue_item_id,
            purpose="fa",
            run_id=run_id,
        )


async def _act_on_cooldown_stall(reason: str, *, printer_id: int, queue_item_id: int | None) -> None:
    """``on_stall`` action: quarantine the printer + pause the unit's run.

    Mirrors the farm_policy failure pairing (quarantine + ``_maybe_pause_run_no_printers``).
    Opens its own session; NEVER mutates the queue item and NEVER touches the plate
    gate — a plateaued bed's part is still on the plate for a human to clear.
    ``queue_item_id`` is None for the auto foreign-plate watch (no queue unit) — the
    printer is still quarantined, there is simply no run to pause."""
    from backend.app.core.database import async_session
    from backend.app.models.print_batch import PrintBatch
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.services import farm_policy

    async with async_session() as db:
        await farm_policy.quarantine_printer(db, printer_id, reason=f"Cooldown stalled: {reason}", failure_count=1)
        item = await db.get(PrintQueueItem, queue_item_id) if queue_item_id is not None else None
        if item is not None and item.batch_id is not None:
            batch = await db.get(PrintBatch, item.batch_id)
            if batch is not None:
                await farm_policy._maybe_pause_run_no_printers(db, batch)
    logger.warning(
        "Eject monitor: printer %s cooldown stall (%s) — quarantined, run-pause evaluated", printer_id, reason
    )


# --------------------------------------------------------------------------- #
# Startup reconcile of pending ejects (W1.2)
# --------------------------------------------------------------------------- #
async def _reconcile_one(
    printer_id: int,
    *,
    manager,
    poll_s: int,
    max_wait_s: int,
    sleep: Callable[[float], Awaitable[None]],
) -> None:
    """Reconcile ONE hydrated printer's pending eject against the live state (W1.2).

    Polls until the printer reconnects (≤ ``max_wait_s``), then applies the decision
    table: RUNNING/PAUSE+name-match → leave pending (the live terminal resolves it);
    RUNNING/PAUSE+mismatch → drop pending, gate kept; FINISH+match → resolve as a
    completed eject (production gate clear / FA finalise); FAILED+match → resolve as a
    failed eject (quarantine, gate kept); IDLE / unverifiable / never reconnects →
    drop pending, gate kept for a human (never clear a gate on guesswork)."""
    from backend.app.core.database import async_session
    from backend.app.services import farm_policy
    from backend.app.services.eject import remote as eject_remote

    waited = 0
    while True:
        if eject_remote.peek_pending_eject(printer_id) is None:
            return  # a live terminal callback already resolved it
        state = manager.get_status(printer_id)
        if state is not None and getattr(state, "connected", False):
            break
        if waited >= max_wait_s:
            async with async_session() as db:
                await eject_remote.clear_pending_eject(db, printer_id)
            logger.warning(
                "Eject monitor: printer %s never reconnected within %ss — pending eject dropped, gate kept for a human",
                printer_id,
                max_wait_s,
            )
            return
        await sleep(poll_s)
        waited += poll_s

    live = getattr(state, "state", None)
    subtask_id = getattr(state, "subtask_id", None)
    subtask_name = getattr(state, "subtask_name", None)
    # Positive-identity check against the hydrated pending (id lenient post-restart —
    # the client's last_dispatch id is gone — so the echoed name carries the weight).
    name_matches = eject_remote.matches_pending_eject(printer_id, subtask_id, subtask_name=subtask_name)

    if live in ("RUNNING", "PAUSE"):
        if name_matches:
            logger.info(
                "Eject monitor: printer %s eject still in flight post-restart — leaving pending for the live terminal",
                printer_id,
            )
            return
        async with async_session() as db:
            await eject_remote.clear_pending_eject(db, printer_id)
        logger.warning(
            "Eject monitor: printer %s is running a non-eject job (%r) post-restart — pending dropped, gate kept",
            printer_id,
            subtask_name,
        )
        return

    if name_matches and live == "FINISH":
        # The eject FINISHed during downtime → resolve exactly as the live terminal
        # would (production: clear the gate; FA: finalise the approval).
        async with async_session() as db:
            await farm_policy.on_terminal(
                db,
                printer_id,
                None,
                "completed",
                completed_subtask_id=subtask_id,
                completed_subtask_name=subtask_name,
            )
        logger.info(
            "Eject monitor: printer %s eject FINISHed during downtime — reconciled (gate/FA resolved)", printer_id
        )
        return

    if name_matches and live == "FAILED":
        # The eject FAILED during downtime → mirror the live failure branch:
        # quarantine, gate kept (sweep unverified).
        async with async_session() as db:
            await farm_policy.on_terminal(
                db,
                printer_id,
                None,
                "failed",
                completed_subtask_id=subtask_id,
                completed_subtask_name=subtask_name,
            )
        logger.warning(
            "Eject monitor: printer %s eject FAILED during downtime — reconciled (quarantine, gate kept)", printer_id
        )
        return

    # IDLE / unknown state, or a terminal state whose name does not match: never
    # clear a gate on guesswork. Drop the pending and leave the gate for a human.
    async with async_session() as db:
        await eject_remote.clear_pending_eject(db, printer_id)
    logger.warning(
        "Eject monitor: printer %s eject unverifiable post-restart (state=%r, name=%r) — pending dropped, gate kept",
        printer_id,
        live,
        subtask_name,
    )


async def reconcile_pending_ejects_on_startup(
    *,
    manager=printer_manager,
    poll_s: int = _RECONCILE_POLL_S,
    max_wait_s: int = _RECONCILE_MAX_WAIT_S,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> int:
    """Reconcile every hydrated pending eject after a restart (W1.2 background task).

    Spawned from the lifespan AFTER ``hydrate_pending_ejects_from_db`` +
    ``rearm_on_startup``. Returns the number of printers processed. Each printer is
    wrapped so one failure never aborts the sweep. ``manager``/``sleep`` are injectable
    for testing."""
    from backend.app.services.eject import remote as eject_remote

    printer_ids = eject_remote.pending_eject_printer_ids()
    if not printer_ids:
        return 0
    processed = 0
    for printer_id in printer_ids:
        try:
            await _reconcile_one(printer_id, manager=manager, poll_s=poll_s, max_wait_s=max_wait_s, sleep=sleep)
            processed += 1
        except Exception:  # noqa: BLE001 — one printer's reconcile must not abort the sweep
            logger.exception("Eject monitor: pending-eject reconcile failed for printer %s", printer_id)
    return processed


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
        # Value is an _ActiveWatch for an armed cooldown/FA watch, the None sentinel
        # for an escalation-only / still-resolving watch, or (in direct-poke tests)
        # a raw float threshold. active_watch() normalises all three to float | None.
        self._watching: dict[int, _ActiveWatch | float | None] = {}

    def active_watch(self, printer_id: int) -> float | None:
        """The in-flight cooldown watch's release threshold (°C), or None.

        None both when no watch is armed and when the armed watch carries no
        threshold (escalation-only / still resolving) — callers surface the
        cooldown phase only when a real release temperature exists. Unchanged
        contract: callers (and the UI ``eject_watch`` payload) still see float | None."""
        entry = self._watching.get(printer_id)
        if isinstance(entry, _ActiveWatch):
            return entry.threshold_c
        return entry  # None sentinel, or a raw float poked in by a test

    def active_watch_identity(self, printer_id: int) -> _ActiveWatch | None:
        """The in-flight cooldown/FA watch's full identity (item + release channel),
        or None when nothing armed / escalation-only / still resolving (W2)."""
        entry = self._watching.get(printer_id)
        return entry if isinstance(entry, _ActiveWatch) else None

    def request_release_now(self, printer_id: int) -> bool:
        """Signal an armed cooldown/FA watch to release immediately (manual "Eject
        now"). Returns False when no releasable watch is armed (foreign gate / absent
        / still resolving) — the caller then dispatches directly instead."""
        entry = self._watching.get(printer_id)
        if isinstance(entry, _ActiveWatch):
            entry.release_now.set()
            return True
        return False

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
        re-spawn the COOLDOWN watch iff the most-recently-started job was a successful
        eject job (see :func:`should_rearm`) AND that item's ``dispatch_subtask_id``
        matches the printer's persisted ``plate_gate_subtask_id`` (both non-null).
        A gate whose source we cannot positively tie to the eject job — a foreign
        deposit, or a pre-migration NULL-source gate — never auto-clears on restart.
        But it must NOT be left watch-less: a stranded gate with no watch escalates
        NOTHING and silently stalls the farm (the exact failure this closes). So every
        such gate instead arms the ESCALATION-ONLY hold — it never auto-clears, but it
        escalates + notifies until a human clears the plate. The return count is the
        number of COOLDOWN watches re-armed (escalation-only holds are not counted)."""
        from backend.app.core.database import async_session
        from backend.app.models.printer import Printer
        from backend.app.services.eject import remote as eject_remote

        rearmed = 0
        async with async_session() as db:
            result = await db.execute(
                select(Printer.id, Printer.plate_gate_subtask_id)
                .where(Printer.awaiting_plate_clear.is_(True))
                .where(Printer.quarantined.is_(False))
            )
            for printer_id, gate_subtask_id in result.all():
                # A printer with a hydrated pending eject is owned by the reconciler
                # (W1.2): its eject is still in flight (or finished during downtime),
                # so re-arming a cooldown watch here would double-dispatch. Skip it.
                if eject_remote.peek_pending_eject(printer_id) is not None:
                    logger.info(
                        "Eject monitor: printer %s has a hydrated pending eject — rearm skipped (reconciler owns it)",
                        printer_id,
                    )
                    continue
                item = await _latest_started_item(db, printer_id)
                cooldown_rearmable = (
                    item is not None
                    and should_rearm(True, item.status, item.eject_profile_id, getattr(item, "first_article", False))
                    and bool(gate_subtask_id)
                    and item.dispatch_subtask_id == gate_subtask_id
                )
                if cooldown_rearmable:
                    if self._start_watch(printer_id, item.id):
                        rearmed += 1
                    continue
                # Not positively tie-able to the eject job (item None / not a completed
                # eject / gate-subtask mismatch): NEVER auto-clear — but the gate must
                # not sit watch-less and silently stall the farm. Arm the escalation-
                # only hold (deduped against any watch already in flight) so a stranded
                # plate still escalates + notifies until a human clears it.
                logger.info(
                    "Eject monitor: printer %s gate NOT cooldown-re-armed (item=%s, gate=%r, last dispatch=%r) "
                    "— arming escalation-only hold (no auto-clear)",
                    printer_id,
                    item.id if item is not None else None,
                    gate_subtask_id,
                    item.dispatch_subtask_id if item is not None else None,
                )
                self.start_escalation_only_watch(printer_id)
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

    def start_fa_eject_watch(self, printer_id: int, queue_item_id: int, run_id: int | None) -> bool:
        """Arm the cooldown-gated eject for an APPROVED first article.

        The FA remote eject must obey the same thermal policy as production —
        an approval minutes after FINISH would otherwise sweep a hot plate now
        that the eject file is motion-only. Same dedupe/threshold-exposure as
        the production watch; releases into ``_dispatch_fa_eject``."""
        if printer_id in self._watching:
            return False
        self._watching[printer_id] = None
        spawn_background_task(
            self._watch(printer_id, queue_item_id, purpose="fa", run_id=run_id),
            name=f"eject-fa-watch-{printer_id}",
        )
        return True

    def start_foreign_eject_watch(self, printer_id: int, profile_id: int, threshold_c: float) -> bool:
        """Arm an AUTO eject watch for a FOREIGN plate positively identified as the
        farm's OWN file (2026-07-18 decision).

        Like the production cooldown watch it holds the plate gate and dispatches an
        eject once the live bed reaches ``threshold_c`` — but it carries NO queue item.
        The release dispatches a foreign-plate sweep (``dispatch_identified_foreign_eject``,
        the same primitive the manual foreign "Eject now" uses) against the chosen
        ``profile_id``; the gate-clear stays owned by the eject job's own terminal.
        Deduped against any in-flight watch. Deliberately NOT restart-durable (mirrors
        the manual foreign eject): after a restart ``rearm_on_startup`` degrades this to
        an escalation-only hold rather than re-deriving the foreign identity."""
        if printer_id in self._watching:
            return False
        self._watching[printer_id] = None
        spawn_background_task(
            self._watch(printer_id, None, purpose="foreign", threshold_override=threshold_c, profile_id=profile_id),
            name=f"eject-foreign-watch-{printer_id}",
        )
        return True

    async def _watch(
        self,
        printer_id: int,
        queue_item_id: int | None,
        *,
        purpose: str = "production",
        run_id: int | None = None,
        threshold_override: float | None = None,
        profile_id: int | None = None,
    ) -> None:
        try:
            if threshold_override is not None:
                # Foreign auto-eject: the release threshold is the chosen profile's
                # cooldown target, passed directly (there is no queue item to resolve).
                threshold: float | None = threshold_override
            else:
                threshold = await _resolve_eject_threshold(queue_item_id, for_first_article=purpose == "fa")
                if threshold is None:
                    # Not an eject job — leave the manual plate-clear behaviour intact.
                    return
            # Record the full watch identity so active_watch() still exposes the
            # threshold AND a manual "Eject now" can drive this watch's release_now.
            release_now = asyncio.Event()
            self._watching[printer_id] = _ActiveWatch(
                threshold_c=threshold, queue_item_id=queue_item_id, purpose=purpose, release_now=release_now
            )
            # Resolve the plateau/cap policy once at arm; bind the eject dispatch and
            # the stall reaction to THIS unit so the watch stays identity-scoped.
            stall_window_s, stall_epsilon_c, max_hold_s, plateau_eject_margin_c = await _resolve_stall_settings()
            if purpose == "fa":
                on_release = functools.partial(
                    _dispatch_fa_eject, printer_id=printer_id, queue_item_id=queue_item_id, run_id=run_id
                )
            elif purpose == "foreign":
                # Lazy import: manual.py imports monitor at module load, so importing it
                # at module top here would be a circular import.
                from backend.app.services.eject.manual import dispatch_identified_foreign_eject

                on_release = functools.partial(
                    dispatch_identified_foreign_eject, printer_id=printer_id, profile_id=profile_id
                )
            else:
                on_release = functools.partial(
                    _dispatch_production_eject, printer_id=printer_id, queue_item_id=queue_item_id
                )
            on_stall = functools.partial(_act_on_cooldown_stall, printer_id=printer_id, queue_item_id=queue_item_id)
            await watch_bed_and_clear(
                printer_id,
                threshold,
                stall_window_s=stall_window_s,
                stall_epsilon_c=stall_epsilon_c,
                plateau_eject_margin_c=plateau_eject_margin_c,
                max_hold_s=max_hold_s,
                on_release=on_release,
                on_stall=on_stall,
                release_now=release_now,
            )
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
