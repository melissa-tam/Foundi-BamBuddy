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

**Since the 2026-08-30 cut-over the monitor decides nothing about WHICH watch to
arm.** It is the plate-occupancy authority's injected POLICY DRIVER: the authority
owns the plate and the policy attached to it, and calls
:meth:`EjectCooldownMonitor.on_occupancy_change` after every transition; this module
converts the policy into a running task. The four ``start_*_watch`` entry points and
the ``_watching`` registry they deduped against are gone with it — a watch can no
longer outlive, contradict, or double-arm against the plate it was arming for,
because the plate is the only thing that decides it exists.

**A service hold does not stop a cooldown (2026-09-13).** Maintenance mode suspends what
the farm DOES to a printer, not what it observes: a held printer's plate keeps cooling
(``cooldown_prep``'s fans, which are air rather than motion) and the watch WITHHOLDS the
eject until the hold lifts. So the hold is not an arm-time gate — ``_desired_policy`` is a
pure function of the plate — but a per-tick LEVEL inside the watch, minted once per watch
in ``_watch`` and read by ``watch_bed_and_clear``/``watch_gate_escalation_only``. Nothing
is re-armed on release: the running watch sees the level drop on its next tick.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import select

from backend.app.core.tasks import spawn_background_task
from backend.app.schemas.settings import AppSettings
from backend.app.services import printer_incidents
from backend.app.services.eject import cooldown_prep, remote as eject_remote
from backend.app.services.eject.cooldown_prep import WatchVerdict
from backend.app.services.hms_errors import summary_of
from backend.app.services.plate_occupancy import (
    CooldownEject,
    EscalationOnly,
    FirstArticleEject,
    ForeignAutoEject,
    OccupancyPolicy,
    OccupancyView,
    PlateRefusal,
    plate_occupancy,
)
from backend.app.services.printer_manager import printer_manager

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

logger = logging.getLogger(__name__)


@dataclass
class _ArmedWatch:
    """The one watch task armed for a printer's CURRENT occupancy policy.

    ``policy`` is the record's identity: :meth:`EjectCooldownMonitor.on_occupancy_change`
    respawns only when the desired policy differs from this one (the policy types are
    frozen dataclasses, so equality is structural — a re-notification carrying the same
    policy is a no-op, which is what keeps the driver idempotent under the authority's
    fan-out on EVERY transition).

    ``release_now`` is the manual-release channel for the three RELEASING policies
    (cooldown / FA / foreign auto); an escalation-only hold has none, and
    ``request_release_now`` answers False for it. ``threshold_c`` is filled in by the
    watch task once it resolves — it is what the UI's ``eject_watch`` payload renders,
    and it stays None for an escalation-only hold and while a threshold is resolving.
    """

    policy: OccupancyPolicy
    task: asyncio.Task
    queue_item_id: int | None = None
    release_now: asyncio.Event | None = None
    threshold_c: float | None = field(default=None)
    # The Z the cooldown hold parked this printer's plate at, written ONCE by the
    # watch task and only when its hold was actually SENT (``cooldown_prep``). The
    # one store behind both consumers — ``hold_z()`` → the status payload → the printer
    # card's "plate raised" chip, and ``eject_seed_z()`` → the eject estimator — so a
    # plate nobody held cannot be reported as raised or costed as if it were. It is
    # never cleared while the watch lives: the plate IS still up there (the part top
    # sits ~48 mm under the nozzle plane) until a human lowers it, and the chip is
    # exactly the "do not jog the toolhead" warning an operator opening the machine
    # needs — dropping it the moment maintenance mode is entered would remove the
    # warning at the one moment it matters.
    hold_z: float | None = field(default=None)
    # Did a service hold stand at any point during this watch? Written once on the
    # unheld→held edge and never cleared, because it records something that cannot be
    # un-done: a human may have jogged the bed. The farm therefore keeps its position
    # CLAIM (the chip above — the plate is raised, and saying so is the safe error) but
    # stops offering it as a MEASUREMENT: ``eject_seed_z()`` answers None from here on,
    # so the eject's runtime watchdog budgets its drop span from the safe
    # over-statement instead of from a height nobody can still vouch for.
    hold_seen: bool = False
    # "This watch has finished cooling and is holding the eject back, because the
    # printer is in maintenance mode." Written only by the owning watch task (like
    # ``hold_z``), read through :meth:`EjectCooldownMonitor.deferred` and projected as
    # ``eject_watch.deferred``. It lives on the WATCH rather than on the plate's policy
    # because a policy field would flip policy equality and respawn the watch — losing
    # the elapsed cooldown, the plateau anchor and the escalation state, and re-boosting
    # the fans it has just retired.
    deferred: bool = False


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

# The cooldown prep's connect wait (see :func:`_await_printer_connected`). Sized off
# measured reconnects: the virtual printer ~1.5 s, a real printer ~10 s, and the whole
# fleet back inside 3 minutes after the 2026-09-04 power outage.
_PREP_CONNECT_POLL_S = 1.0
_PREP_CONNECT_WAIT_S = 300.0


def should_auto_clear(final_status: str) -> bool:
    """True only for terminal *success* — failures leave the plate gate set."""
    return final_status in _SUCCESS_TERMINAL


async def notify_plate_not_empty(printer_id: int, *, source_detail: str = "") -> None:
    """Fire the plate-not-empty notification for a stuck-plate escalation.

    The ONE implementation of "page a human: this plate is not empty". Public since
    the 2026-08-30 cut-over, because three lanes OUTSIDE this module page through it —
    the eject runtime watchdog's mid-flight stop, the eject start deadline, and the
    occupancy store's last-rung escalation when a plate could not be given any policy
    at all — alongside the escalation-only hold below, which is this module's own.

    Opens its own session (mirroring the rest of this module) and resolves the
    printer name for the message. ``source_detail`` disambiguates the escalation
    source (Phase 3.3) — the watches below bake in their own sentence via a
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


async def _hold_for_human(printer_id: int, exc: eject_remote.EjectDispatchError, *, cause: str) -> str:
    """Page, then convert the plate to an escalation-only hold. Returns ``"stalled"``.

    The reaction to a TERMINAL eject refusal — a state no amount of retrying can cure
    (:data:`~backend.app.services.eject.remote._TERMINAL_REFUSALS`; today: a printer
    that rebooted with a part on the plate, whose Z frame is fiction). Burning the
    watch's three dispatch attempts against it would only delay the page by a minute
    and teach the log nothing.

    **The order is load-bearing.** ``set_policy`` fans out through the authority to
    :meth:`EjectCooldownMonitor.on_occupancy_change`, which cancels the watch task this
    coroutine is running INSIDE — so a page issued after it dies at its first await and
    the operator is never told. Page first, transition second; a test pins exactly that.

    Never a strike and never a quarantine: ``on_stall`` is deliberately NOT called. The
    printer is healthy and its plate is held by a hold that a human resolves; taking it
    out of rotation as well would punish the machine for the farm's own safety refusal.
    """
    logger.warning(
        "Eject monitor: printer %s eject refused TERMINALLY (%s, cause=%s) — paging and holding the plate "
        "for a human; no retry, no quarantine",
        printer_id,
        exc.code,
        cause,
    )
    try:
        await notify_plate_not_empty(printer_id, source_detail=eject_remote.terminal_refusal_detail(exc.code))
    except Exception:  # noqa: BLE001 — a notify failure must not stop the hold below
        logger.exception("Eject monitor: terminal-refusal page for printer %s failed", printer_id)
    plate_occupancy.set_policy(printer_id, EscalationOnly())
    return "stalled"


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
    on_sample: Callable[[Mapping[str, float | None]], None] | None = None,
    held: Callable[[], bool] | None = None,
    on_cooldown_over: Callable[[str], None] | None = None,
) -> WatchVerdict:
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
    ``on_sample`` is handed the live ``temperatures`` map on every tick the state was
    READABLE, and is the cooldown prep's chamber-boost decision: this ONE poll feeds
    it, so the boost ends off the same reading that decides the release and the wave
    adds no second timer, no second cadence and no second connection check. It is
    fire-and-forget by contract — a sampler that raises is logged and the poll carries
    on, because a plate's watch is worth more than a measurement.

    ``manager``, ``sleep``, ``notify``, ``on_release``, ``on_stall`` and ``on_sample``
    are all injectable for testing; ``manager`` supplies the BED reading only — whether
    the plate is still occupied is read from the plate-occupancy authority, the one
    place that knows.

    **``held`` and ``on_cooldown_over``: a cooling-only watch (2026-09-13).** A service
    hold suspends the farm's ACTIONS on a printer (motion, eject, page, quarantine), not
    its OBSERVATION — cooling is air, not motion — so a hold no longer kills this watch.
    ``held`` is read ONCE per tick as a LEVEL (None ⇒ never held, which is what every
    direct caller and every test that does not pass it gets), and its answer decides only
    whether a release is PERMITTED:

    * threshold, near-threshold plateau and the max-hold cap all still DECIDE; while held
      they withhold instead of dispatching — one INFO line, ``on_cooldown_over(cause)``
      (the watch's own retirement of the cooldown actuators: the part is cool, so the fans
      have finished their work) and a watch that keeps polling;
    * a hot plateau while held is NOT a quarantine: the printer is healthy and a human is
      standing in front of it, so it warns, retires the cooldown and withholds;
    * the manual ``release_now`` is ALWAYS permitted — manual verbs stay live under a
      hold, and an operator's own click is answered, never deferred;
    * the escalation page is evaluated only when a release is permitted, so a hold longer
      than ``escalate_s`` cannot burn the once-only page on a printer whose notifications
      are suppressed anyway.

    Nothing re-arms on release: the running watch simply reads ``held() == False`` on its
    next tick and releases (the liveness pin).

    **Two clocks, because there are two questions.** ``elapsed`` is wall time and always
    accrues — it drives the plateau windows, which must keep measuring the cooling rate
    under a hold so a bed that has reached equilibrium still retires its fans. ``held_s``
    accrues on held ticks, and ``waited = elapsed - held_s`` is "how long the FARM has
    waited for this bed": the max-hold cap and the escalation page compare that, so a
    multi-hour hold can neither force-dispatch a re-heated bed the second it lifts nor
    consume the page while nobody could be told.
    """
    if notify is None:
        # The escalation means "bed never reached the release threshold", NOT
        # "objects detected" — fire the dedicated cooldown_escalation event with
        # the live bed at fire time (bound below).
        notify = functools.partial(_default_notify_cooldown_escalation, threshold_c=threshold_c, max_hold_s=max_hold_s)

    plateau_enabled = stall_window_s > 0 and on_stall is not None
    elapsed = 0  # wall time: drives the plateau windows, accrues under a hold too
    held_s = 0  # of which this much was spent held — see ``waited`` below
    escalated = False
    anchor: float | None = None  # first readable bed sample, held across strikes
    strikes = 0
    next_boundary = stall_window_s  # first plateau comparison point
    release_failures = 0
    deferred = False  # the cooldown is over and the eject is waiting on a hold

    def _withhold(cause: str) -> None:
        """The eject this tick decided on is withheld: the printer is in maintenance mode.

        The deferral edge, once per held episode — the SECOND edge of the cooling
        episode's lifetime (the exit ``finally`` is the other). It is the honest answer
        to "the part is cool but hands may be in the machine": the thermal half of the
        wait is finished, so ``on_cooldown_over`` retires the actuators (fans off, no
        second summary), and the release half waits for the hold to lift. Later calls in
        the same episode are no-ops, so a bed that keeps meeting the threshold logs once
        rather than every 20 s.
        """
        nonlocal deferred
        if deferred:
            return
        deferred = True
        logger.info(
            "Eject monitor: printer %s %s while in maintenance mode — eject withheld, "
            "cooldown actuators retired; watch continues",
            printer_id,
            cause,
        )
        if on_cooldown_over is not None:
            try:
                on_cooldown_over(cause)
            except Exception:  # noqa: BLE001 — retiring the prep never costs the plate its watch
                logger.exception("Eject monitor: cooldown retirement for printer %s failed", printer_id)

    async def _do_release(cause: str) -> str:
        """Dispatch the eject (``on_release``) with retry/stall handling.

        Returns ``"released"`` on success (or when no dispatcher is wired),
        ``"retry"`` to keep polling after a dispatch failure (< 3 so far),
        ``"stalled"`` once THREE consecutive failures trip the stall path OR a TERMINAL
        refusal holds the plate for a human (:func:`_hold_for_human` — page then
        escalation-only, no strike, no quarantine), or ``"cleared"`` when the
        plate-clear gate dropped between the top-of-poll check and here (W2/W3
        hardening — never sweep an already-emptied plate)."""
        nonlocal release_failures
        # Re-check the gate at the release boundary: the escalate/plateau/manual
        # branches can reach here after an await, during which an operator (or the
        # eject's own terminal) may have cleared the gate. Sweeping then would eject
        # onto an empty plate.
        if not plate_occupancy.is_plate_occupied(printer_id):
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
        except Exception as exc:  # noqa: BLE001 — a dispatch failure retries, never kills the watch
            if isinstance(exc, eject_remote.EjectDispatchError) and exc.terminal:
                return await _hold_for_human(printer_id, exc, cause=cause)
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
        # The hold is a LEVEL, read ONCE per tick: every decision below asks the same
        # question of the same answer, so a hold entered mid-tick cannot release through
        # one branch and withhold through another.
        is_held = held() if held is not None else False
        release_permitted = not is_held
        # How long the FARM has waited for this bed — wall time minus the time a human
        # owned the machine. The cap and the escalation page are both about the farm's
        # patience, never about the wall clock.
        waited = elapsed - held_s
        if deferred and not is_held:
            # The hold lifted. Release on the VERY NEXT evaluation (nothing re-arms us),
            # and re-measure the plateau from here: a human may have re-heated the bed,
            # and the strikes/anchor/boundary from before the hold describe a different
            # cooldown than the one this watch is now finishing.
            deferred = False
            anchor = None
            strikes = 0
            next_boundary = elapsed + stall_window_s

        # Phase boundary: the moment the plate-clear gate drops — the eject job's own
        # terminal, or an operator clearing the plate — the phase is over. Exit WITHOUT
        # dispatching so we never sweep an already-cleared plate (mirrors the foreign
        # gate watch). Checked at the TOP of every poll, before reading the bed.
        if not plate_occupancy.is_plate_occupied(printer_id):
            logger.info(
                "Eject monitor: printer %s plate-clear gate cleared mid-cooldown — watch exiting (no eject)",
                printer_id,
            )
            return "cleared"

        # W2: an operator "Eject now" during an armed watch sets release_now → sweep
        # immediately through the SAME _do_release path (no parallel dispatch race),
        # bypassing the cooldown threshold. The hot-bed allowance is enforced upstream
        # in the manual-eject service before the event is ever set. Deliberately NOT
        # gated by the hold: every manual verb stays live under maintenance mode, and a
        # click that raised this event IS the human the hold defers to.
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
            if on_sample is not None and not deferred:
                # THIS poll is the whole sampling story: the cooldown prep's chamber
                # boost ends off the same reading that decides the release, so there is
                # no second timer, no second cadence and no second connection check. A
                # sampler that throws must never cost the plate its watch.
                #
                # Not while DEFERRED: the prep is retired, and a retired prep is a
                # no-op by its own invariant — but calling it anyway would say this
                # watch still feeds an actuator it has handed back.
                try:
                    on_sample(state.temperatures)
                except Exception:  # noqa: BLE001 — a sampler never kills the watch feeding it
                    logger.exception("Eject monitor: cooldown sampler for printer %s failed", printer_id)

        if deferred:
            # The cooldown is over and its actuators are retired; the only thing left to
            # decide is the release, and that waits for the hold. The gate check, the
            # manual check and the state read above are the whole tick.
            pass
        elif bed_temp is not None and bed_temp <= threshold_c:
            if anchor is None:
                anchor = bed_temp
            if release_permitted:
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
                _withhold("threshold")
        else:
            if anchor is None and bed_temp is not None:
                anchor = bed_temp
            # Still above threshold (or unreadable). Escalate-once, then evaluate
            # the plateau watchdog and finally the max-hold cap.
            if release_permitted and not escalated and waited >= escalate_s:
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
                                if release_permitted:
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
                                    # dispatch failed (< 3 so far) — keep polling; the
                                    # next boundary re-strikes and retries the release.
                                else:
                                    # Equilibrated at ambient under a hold: the cooling
                                    # is genuinely finished, so the fans stop — the
                                    # sweep is the only half that waits.
                                    _withhold("plateau_near_threshold")
                            elif release_permitted:
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
                                # A bed stuck genuinely hot is a QUARANTINE when the farm
                                # owns the printer — but under a hold the machine is a
                                # human's, and taking it out of rotation for a bed
                                # somebody may be working on would punish it for the
                                # farm's own deferral. Warn, retire the cooldown, wait.
                                logger.warning(
                                    "Eject monitor: printer %s cooling plateaued (<%.2f°C over 2 windows) and bed "
                                    "%.1f°C is still >%.1f°C above the %.1f°C threshold — no quarantine, printer in "
                                    "maintenance mode",
                                    printer_id,
                                    stall_epsilon_c,
                                    bed_temp,
                                    plateau_eject_margin_c,
                                    threshold_c,
                                )
                                _withhold("plateau_hot")
                    else:
                        strikes = 0
                        anchor = bed_temp  # re-anchor: this window DID cool

            if max_hold_s > 0 and waited >= max_hold_s and bed_temp is not None:
                if release_permitted:
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
                else:
                    _withhold("max_hold_cap")

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
        if is_held:
            # Charged to the HUMAN's clock, not the farm's: this interval was one the
            # farm was not allowed to act in.
            held_s += check_interval_s


def escalation_sentence(*, farm_source: bool, refusal: PlateRefusal | None) -> str:
    """The escalation page's sentence for a plate only a human may clear — ONE origin.

    Three causes, most specific first: the printer REFUSED the plate (its plate check
    paused the job and the job was stopped — the printer's own words ride in); a farm
    unit's own part whose eject never ran; a part a print the farm did not dispatch left.
    """
    if refusal is not None:
        words = summary_of(refusal.messages)
        # The catalog's own sentences end in a period; the page adds exactly one.
        reported = f" The printer reported: {words.rstrip('.')}." if words else ""
        return (
            f"The printer's plate check refused the plate and the print was stopped.{reported} "
            "Fix the plate, then Mark plate cleared."
        )
    if farm_source:
        return "A farm unit's eject did not run — the part is still on the plate. Remove it by hand, then Mark plate cleared."
    return "A print the farm did not dispatch left a part on the plate. Clear the bed to resume dispatch."


async def watch_gate_escalation_only(
    printer_id: int,
    *,
    escalate_s: int = _WATCH_ESCALATE_S,
    check_interval_s: int = _CHECK_INTERVAL_S,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    notify: Callable[[int], Awaitable[None]] | None = None,
    held: Callable[[], bool] | None = None,
    farm_source: bool = False,
    refusal: PlateRefusal | None = None,
) -> str:
    """Escalation-only gate watch for a plate only a human may clear — a foreign
    deposit, a farm unit's own plate whose eject never ran, or a plate the printer's
    own plate check refused.

    Unlike :func:`watch_bed_and_clear` this NEVER releases the gate: the plate is
    held by an unknown job and only a human clearing it (which NULLs the gate) may
    resume dispatch. Same poll cadence and escalation constant as the cooldown
    watch; fires ONE plate-not-empty notification at ``escalate_s`` then keeps
    polling. Exits ``"cleared"`` when the gate is cleared externally (operator).

    ``farm_source`` selects the default page's SENTENCE, and nothing else: a plate the
    farm itself deposited must not be paged as "a print the farm did not dispatch"
    (005-H2S 2026-09-17 — a rejected eject left a farm unit's part on the plate and the
    operator was told it was someone else's print). It is passed in by the policy
    driver, read off the SAME occupancy view the policy came from, because the fact —
    ``PlateOccupied.source_subtask_id`` — belongs to the plate record and a watch that
    re-read it mid-flight would be a second opinion with its own timing.

    ``refusal`` (2026-09-24) outranks both: the printer's plate check paused a job and
    the job was then stopped, so the page names the printer's OWN words for what is wrong
    with the plate (:func:`escalation_sentence`) — the stop wiped them off the printer,
    and the cause is the only place they survive. Memory-only: after a restart the gate
    stands and this reads the generic sentence.

    A disconnected/stale tick does NOT end the watch — its lifetime is the gated
    PHASE, not connectivity (mirroring ``watch_bed_and_clear``'s unreadable-bed
    tolerance). A printer that drops off mid-hold and comes back must still find its
    stranded plate escalating, not silently un-watched. ``sleep`` and ``notify`` are
    injectable for testing; there is no ``manager`` here, unlike
    :func:`watch_bed_and_clear`, because this watch reads no bed — its only question
    is whether the plate is still occupied, and the plate-occupancy authority is the
    one place that knows.

    ``held`` (a LEVEL, read once per tick; None ⇒ never held) gates the ONE act this
    watch has. Paging a human about the plate they are standing in front of is exactly
    what maintenance mode is for — and because ``escalated`` is once-only, firing it
    during a hold would ALSO consume it: ``notification_service`` drops a farm-reaction
    event for a held printer, so the page would be logged, suppressed, and never sent
    again after the hold lifted. Holding the page keeps it for the moment it can be read.
    """
    if notify is None:
        # Escalation source, distinct from cooldown_timeout: the plate is held and no
        # sweep is coming. WHY it is held decides the sentence.
        notify = functools.partial(
            notify_plate_not_empty,
            source_detail=escalation_sentence(farm_source=farm_source, refusal=refusal),
        )
    elapsed = 0
    escalated = False
    while True:
        if not plate_occupancy.is_plate_occupied(printer_id):
            logger.info(
                "Eject monitor: printer %s foreign-gate cleared externally — escalation watch exiting", printer_id
            )
            return "cleared"

        if not escalated and elapsed >= escalate_s and not (held() if held is not None else False):
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


async def _setting_bool(db, key: str, default: bool) -> bool:
    """get_setting(key) read as a boolean, falling back to ``default``.

    A sibling of :func:`_setting_num` rather than a ``cast=bool`` call, because
    ``bool("false")`` is True: the settings store keeps booleans as the strings the PUT
    route writes, so the only correct read is the same ``== "true"`` test every other
    boolean setting is read with (``farm_policy``'s ``farm_idle_park_enabled``). Same
    fallback contract — an absent row is the schema default, never False.
    """
    from backend.app.api.routes.settings import get_setting

    raw = await get_setting(db, key)
    return default if raw is None else raw.strip().lower() == "true"


@dataclass(frozen=True)
class CooldownWatchSettings:
    """Everything a cooldown watch reads from farm settings, resolved ONCE at arm.

    A value object rather than a tuple because most of the set is not a plateau/cap
    number at all — the fan switches and speeds and the plate hold belong to the
    cooldown PREP, this watch merely resolves them — and an ever-growing tuple
    unpacked at every call site is a rename waiting to go silently wrong.
    """

    stall_window_s: int
    stall_epsilon_c: float
    max_hold_s: int
    plateau_eject_margin_c: float
    # The two cooldown fans: a switch each, one speed for the aux lane and the
    # boost/sustain pair for the chamber lane. Composed into the prep's own value
    # object by :attr:`fans` — the ONE place the two spellings meet.
    aux_fan_enabled: bool
    aux_fan_percent: int
    chamber_fan_enabled: bool
    chamber_fan_percent: int
    chamber_fan_sustain_percent: int
    # The prep's other two operator inputs: whether the plate is raised at all, and
    # where the part's TOP should sit relative to the nozzle plane while it is.
    hold_enabled: bool
    hold_part_top_mm: int

    @property
    def fans(self) -> cooldown_prep.CooldownFanSettings:
        """The five fan settings as the prep's ONE value. The single composition site.

        The aux lane's sustain is its own speed, by construction rather than by a
        setting nobody offers: the aux fan works by raising the cooling CONSTANT, and
        the cooling law makes the minutes saved per boost-minute the same in the first
        minute of a wait and the last — so there is no step-down point better than any
        other, and one speed for the whole wait is the shortest wait. The chamber fan
        works by lowering the AIR temperature instead, whose reward ends measurably
        (the air reaches the eject line), which is the lane that genuinely steps. See
        :mod:`~backend.app.services.eject.cooldown_prep` for the derivation.
        """
        return cooldown_prep.CooldownFanSettings(
            aux=cooldown_prep.FanRequest(self.aux_fan_enabled, self.aux_fan_percent, self.aux_fan_percent),
            chamber=cooldown_prep.FanRequest(
                self.chamber_fan_enabled, self.chamber_fan_percent, self.chamber_fan_sustain_percent
            ),
        )


async def _resolve_stall_settings() -> CooldownWatchSettings:
    """Resolve the watch's settings-backed policy numbers from farm settings.

    Read ONCE at watch arm. Fallbacks come from the ``AppSettings`` schema field
    defaults (the single origin — no mirrored literals here). Minute-valued
    settings are converted to seconds. ``window == 0`` disables the plateau
    watchdog; ``max_hold == 0`` disables the cap. ``plateau_eject_margin_c`` is the
    °C-above-threshold band inside which a plateaued bed is RELEASED rather than
    quarantined (equilibrated at ambient). The five fan settings are
    :mod:`~backend.app.services.eject.cooldown_prep`'s two actuators — a switch each,
    the aux fan's one speed, and the chamber fan's boost/sustain pair; each switch is
    the ONE on/off owner, so a speed is never read as an off. ``hold_enabled`` /
    ``hold_part_top_mm`` are that module's other two operator inputs — the plate
    hold's switch, and where the part's top is held relative to the nozzle plane
    (capped per model by the geometry registry).

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
    aux_fan_enabled = fields["farm_cooldown_aux_fan_enabled"].default
    aux_fan = fields["farm_cooldown_aux_fan_percent"].default
    chamber_fan_enabled = fields["farm_cooldown_chamber_fan_enabled"].default
    chamber_fan = fields["farm_cooldown_chamber_fan_percent"].default
    chamber_sustain = fields["farm_cooldown_chamber_fan_sustain_percent"].default
    hold_enabled = fields["farm_cooldown_hold_enabled"].default
    hold_part_top = fields["farm_cooldown_hold_part_top_mm"].default
    try:
        async with async_session() as db:
            window_min = await _setting_num(db, "farm_cooldown_stall_window_minutes", window_min, int)
            epsilon = await _setting_num(db, "farm_cooldown_stall_epsilon_c", epsilon, float)
            max_hold_min = await _setting_num(db, "farm_cooldown_max_hold_minutes", max_hold_min, int)
            margin = await _setting_num(db, "farm_cooldown_plateau_eject_margin_c", margin, float)
            aux_fan_enabled = await _setting_bool(db, "farm_cooldown_aux_fan_enabled", bool(aux_fan_enabled))
            aux_fan = await _setting_num(db, "farm_cooldown_aux_fan_percent", aux_fan, int)
            chamber_fan_enabled = await _setting_bool(
                db, "farm_cooldown_chamber_fan_enabled", bool(chamber_fan_enabled)
            )
            chamber_fan = await _setting_num(db, "farm_cooldown_chamber_fan_percent", chamber_fan, int)
            chamber_sustain = await _setting_num(db, "farm_cooldown_chamber_fan_sustain_percent", chamber_sustain, int)
            hold_enabled = await _setting_bool(db, "farm_cooldown_hold_enabled", bool(hold_enabled))
            hold_part_top = await _setting_num(db, "farm_cooldown_hold_part_top_mm", hold_part_top, int)
    except Exception:  # noqa: BLE001 — arm with defaults rather than strand the gate
        logger.exception("Eject monitor: cooldown stall settings read failed — arming with schema defaults")
    return CooldownWatchSettings(
        stall_window_s=int(window_min) * 60,
        stall_epsilon_c=float(epsilon),
        max_hold_s=int(max_hold_min) * 60,
        plateau_eject_margin_c=float(margin),
        aux_fan_enabled=bool(aux_fan_enabled),
        aux_fan_percent=int(aux_fan),
        chamber_fan_enabled=bool(chamber_fan_enabled),
        chamber_fan_percent=int(chamber_fan),
        chamber_fan_sustain_percent=int(chamber_sustain),
        hold_enabled=bool(hold_enabled),
        hold_part_top_mm=int(hold_part_top),
    )


async def _await_printer_connected(printer_id: int) -> bool:
    """Wait, bounded, for the printer's MQTT session before the cooldown prep arms.

    **The gap this closes is a lifespan ORDER, not a race.** ``main``'s lifespan runs
    ``plate_occupancy_store.hydrate()`` BEFORE ``init_printer_connections`` — and that
    order is deliberate: rebuilding every occupied plate first is what lets a terminal
    arriving on a fresh MQTT session find its plate already there, instead of racing a
    hydration that had not run yet. The consequence is that EVERY watch a restart
    re-arms starts life on a printer that is not connected yet. Without this wait, each
    one calls ``cooldown_prep.begin`` into a closed socket and logs
    ``not connected — plate not held`` plus a refusal per cooldown fan (observed live as
    ``aux fan publish refused`` when the aux lane was the only one, with the virtual
    printer connecting 1.5 s later), so every deploy would drop every actuator for every
    plate then mid-cooldown.

    The invisible half is worse than the lost minutes: a plate that WAS held before the
    restart is still physically at ~Z2 afterwards, but with no hold to report the card
    stops saying "plate raised" — and that chip is what tells an operator not to jog the
    toolhead, which is the whole safety case. Re-arming the hold restores the truth, and
    re-holding an already-held plate is safe by construction: the block's first move is
    the transit to ``park_z``, which LOWERS a held plate to the vendor's own ``G150.3``
    height before the toolhead is asked to move (``cooldown_prep``'s "re-entry" note).
    This wait is what makes that note true rather than merely intended.

    Bounded, because a printer that is off, unplugged or out of the farm's network must
    not park a watch forever: its plate still needs the bed poll, whose max-hold cap and
    escalation are the only things that will ever page a human about it. So on timeout
    the caller arms anyway and ``begin`` records an honest ``skipped:disconnected``.
    ``_PREP_CONNECT_WAIT_S`` is 300 s against measured reconnects of ~1.5 s (VP), ~10 s
    (a real printer) and under 3 minutes for a whole rebooted fleet (2026-09-04).

    Returns True if the session came up, False at the bound. Cancellation propagates —
    a gate cleared during the wait cancels this task exactly as it does mid-poll.
    """
    if printer_manager.is_connected(printer_id):
        return True
    waited = 0.0
    while waited < _PREP_CONNECT_WAIT_S:
        await asyncio.sleep(_PREP_CONNECT_POLL_S)
        waited += _PREP_CONNECT_POLL_S
        if printer_manager.is_connected(printer_id):
            logger.info(
                "Eject monitor: printer %s connected after %.0f s — arming the cooldown prep",
                printer_id,
                waited,
            )
            return True
    logger.warning(
        "Eject monitor: printer %s still disconnected after %.0f s — arming the cooldown prep without it "
        "(the plate is not held and the cooldown fans are not commanded; the bed poll still runs)",
        printer_id,
        waited,
    )
    return False


async def _dispatch_production_eject(*, printer_id: int, queue_item_id: int, plate_z: float | None = None) -> None:
    """``on_release`` action: dispatch the part-present motion-only eject for the
    finished unit through the shared dispatcher.

    Opens its own session, resolves the unit's run, and hands off to
    ``eject.remote.dispatch_part_present_eject(purpose="production")``. RAISES on
    any failure so :func:`watch_bed_and_clear` counts a dispatch failure (retry,
    then stall after three).

    ``plate_z`` is where the cooldown hold parked the plate, when it held one — the
    estimator seed, bound at arm from this watch's own prep. None means "unheld", and
    the eject then costs its first Z move at the safe over-statement instead."""
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
            plate_z=plate_z,
        )


async def _dispatch_fa_eject(
    *, printer_id: int, queue_item_id: int, run_id: int | None, plate_z: float | None = None
) -> None:
    """``on_release`` action for an approved first article: dispatch its part-present
    eject through the shared dispatcher once the bed has reached the release
    threshold. RAISES on failure so the watch retries then stalls (same policy as
    the production release). ``plate_z`` carries the hold's parked height, exactly as
    for the production release."""
    from backend.app.core.database import async_session
    from backend.app.services.eject import remote

    async with async_session() as db:
        await remote.dispatch_part_present_eject(
            db,
            printer_id=printer_id,
            queue_item_id=queue_item_id,
            purpose="fa",
            run_id=run_id,
            plate_z=plate_z,
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
    """Reconcile ONE printer's UNOWNED pending eject against the live state (W1.2).

    Polls until the printer reconnects (<= ``max_wait_s``), then applies the decision
    table: RUNNING/PAUSE+name-match -> the sweep is still in flight, so stamp its start
    and leave it for the live terminal — UNLESS the watchdog's verdict is already
    stamped, in which case the kill it could not deliver is re-driven now that there is
    a session; RUNNING/PAUSE+mismatch -> drop the unowned eject, gate kept; FINISH+match
    -> replay the terminal as ``completed`` (production gate clear / FA finalise);
    FAILED+match -> replay it as ``failed`` (quarantine, gate kept); IDLE / unverifiable
    / never reconnects -> drop the unowned eject, gate kept for a human (never clear a
    gate on guesswork).

    Every verdict acts through the plate-occupancy authority, so the PLATE half of each
    decision is the authority's rule rather than this table's: dropping an unowned eject
    deliberately leaves the plate exactly as it stood.

    The enrolment predicate is ``plate_occupancy.unowned_eject`` and the poll is the
    only thing that makes this the STARTUP body — :func:`reconcile_pending_eject` is
    the same table over a printer already known to be connected.
    """
    from backend.app.core.database import async_session
    from backend.app.services import farm_policy

    waited = 0
    while True:
        if plate_occupancy.eject_identity(printer_id) is None:
            return  # a live terminal callback already resolved it
        state = manager.get_status(printer_id)
        if state is not None and getattr(state, "connected", False):
            break
        if waited >= max_wait_s:
            plate_occupancy.drop_unowned_eject(printer_id, "printer never reconnected after restart")
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
            identity = plate_occupancy.eject_identity(printer_id)
            if identity is not None and identity.runtime_exceeded_at is not None:
                # The watchdog already decided this sweep must stop and could not
                # deliver the command — the printer was off the wire. It is back, and
                # still sweeping: re-drive the ONE kill path now rather than leaving a
                # sweep nobody is watching to run itself out (001/009-H2S 2026-09-12).
                logger.warning(
                    "Eject monitor: printer %s reconnected still running its eject after the deadline fired "
                    "(verdict %s) — re-driving the stop",
                    printer_id,
                    identity.runtime_exceeded_at,
                )
                await eject_remote.redrive_eject_stop(printer_id, stage="reconnect")
                return
            # The sweep survived the restart and is still executing. Stamp the START we
            # never observed so the eject's age reads honestly on every operator
            # surface; no watchdog arms off it (a hydrated record carries no estimate).
            plate_occupancy.note_eject_started(printer_id)
            logger.info(
                "Eject monitor: printer %s eject still in flight post-restart — leaving pending for the live terminal",
                printer_id,
            )
            return
        plate_occupancy.drop_unowned_eject(printer_id, f"printer is running a non-eject job ({subtask_name!r})")
        logger.warning(
            "Eject monitor: printer %s is running a non-eject job (%r) post-restart — pending dropped, gate kept",
            printer_id,
            subtask_name,
        )
        return

    if name_matches and live == "FINISH":
        # The eject FINISHed during downtime -> resolve exactly as the live terminal
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
        # The eject FAILED during downtime -> mirror the live failure branch:
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
    plate_occupancy.drop_unowned_eject(printer_id, f"unverifiable post-restart (state={live!r}, name={subtask_name!r})")
    logger.warning(
        "Eject monitor: printer %s eject unverifiable post-restart (state=%r, name=%r) — pending dropped, gate kept",
        printer_id,
        live,
        subtask_name,
    )


async def _reconcile_one_guarded(
    printer_id: int,
    *,
    manager,
    poll_s: int,
    max_wait_s: int,
    sleep: Callable[[float], Awaitable[None]],
) -> None:
    """One printer's reconcile, wrapped so its failure cannot escape its own task."""
    try:
        await _reconcile_one(printer_id, manager=manager, poll_s=poll_s, max_wait_s=max_wait_s, sleep=sleep)
    except Exception:  # noqa: BLE001 — one printer's reconcile must not abort the others
        logger.exception("Eject monitor: pending-eject reconcile failed for printer %s", printer_id)


async def reconcile_pending_eject(
    printer_id: int,
    *,
    manager=printer_manager,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Reconcile ONE printer's unowned eject NOW — the two non-startup triggers.

    The connected edge (``main.on_printer_status_change``, under the same
    ``connection_epoch`` latch as ``reconcile_stale_active_prints``) and the scheduler
    tick's dead-claim sweep (``farm_stall``) both know the printer is CONNECTED, so
    this skips the reconnect poll entirely and runs the decision table against the
    state that is already there: ``max_wait_s=0`` means "answer with what the manager
    reports, or drop the record".

    It is a NO-OP unless ``plate_occupancy.unowned_eject`` holds — the same enrolment
    predicate the startup sweep uses — so a live sweep under a running watchdog is
    never touched by a trigger that merely noticed the printer, and a trigger that
    fires twice costs one predicate read.

    Guarded like :func:`_reconcile_one_guarded`: both callers spawn it as a background
    task beside work that must not be aborted by one printer's reconcile failing.
    """
    if not plate_occupancy.unowned_eject(printer_id):
        return
    await _reconcile_one_guarded(printer_id, manager=manager, poll_s=_RECONCILE_POLL_S, max_wait_s=0, sleep=sleep)


async def reconcile_pending_ejects_on_startup(
    *,
    manager=printer_manager,
    poll_s: int = _RECONCILE_POLL_S,
    max_wait_s: int = _RECONCILE_MAX_WAIT_S,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> int:
    """Reconcile every UNOWNED pending eject after a restart (W1.2 background task).

    Spawned from the lifespan AFTER ``plate_occupancy_store.hydrate()``. Returns the
    number of printers it STARTED reconciling. ``manager``/``sleep`` are injectable for
    testing.

    Enrolment is ``plate_occupancy.unowned_eject`` — at startup that is every hydrated
    record (nothing else can exist yet), and stating it as the authority's predicate is
    what lets the two live triggers share this body instead of growing a second table.

    The printers are reconciled CONCURRENTLY, one background task each. Serially, a
    single disconnected printer held every other printer behind its 900 s reconnect cap
    while their plates sat gated and un-ejectable — on the night of 2026-08-30 three
    printers were in that state at once, and the operator's only way out of the third
    was to wait out the first two. The tasks share no state (each verdict is one
    printer's own transition through the authority), so concurrency costs nothing but
    the wake-ups.
    """
    printer_ids = [pid for pid in plate_occupancy.printers_with_lease_or_eject() if plate_occupancy.unowned_eject(pid)]
    if not printer_ids:
        return 0
    for printer_id in printer_ids:
        spawn_background_task(
            _reconcile_one_guarded(printer_id, manager=manager, poll_s=poll_s, max_wait_s=max_wait_s, sleep=sleep),
            name=f"eject-pending-reconcile-{printer_id}",
        )
    return len(printer_ids)


class EjectCooldownMonitor:
    """The plate-occupancy authority's POLICY DRIVER: one watch task per occupied plate.

    Before the 2026-08-30 cut-over this class owned the decision as well as the task —
    four ``start_*_watch`` entry points, called from six places, each deduping against
    a ``_watching`` registry that was the farm's second opinion about whether a plate
    was occupied. Now the authority owns the plate AND the policy attached to it, and
    calls :meth:`on_occupancy_change` after every transition; this class only makes the
    world match. That is what makes a watch impossible to leak, double-arm, or outlive
    its plate: the record IS the plate's policy.

    Registered at lifespan through :func:`wire_policy_driver`.
    """

    def __init__(self) -> None:
        # printer_id -> the watch armed for that printer's CURRENT policy.
        self._armed: dict[int, _ArmedWatch] = {}

    # -- the injected driver ------------------------------------------------

    def on_occupancy_change(self, printer_id: int, view: OccupancyView, cause: str) -> None:
        """Make the running watch match ``view``'s policy. The authority's policy driver.

        Called SYNCHRONOUSLY from inside the authority's fan-out, after every
        transition, so it must return immediately — it only spawns and cancels.

        Three rules:

        * a plate that is CLEAR, or a printer an EJECT owns, carries no watch. A
          hydrated eject counts: the startup reconciler owns those printers, and a
          cooldown watch armed beside one is the double dispatch the legacy re-arm
          avoided by skipping such printers outright;
        * otherwise the plate's policy decides which watch runs;
        * and it respawns only when that policy CHANGED — the fan-out fires on every
          transition, and cancelling a healthy cooldown watch to start an identical one
          would lose its elapsed cooldown, its plateau anchor and its escalation state.

        Exceptions are deliberately NOT swallowed. A policy that fails to arm leaves the
        plate ARMLESS, which is the one outcome 2026-07-18/07-21 forbids — so the
        authority catches it, repairs the record to ``EscalationOnly`` and calls back;
        swallowing here would report success for a plate nothing is looking after.
        """
        # Level-triggered timer hygiene: whenever no eject is registered on this
        # printer, neither of the eject lane's two timers has anything left to bound.
        # Routing every retirement (matched terminal, unverified resolve, start expiry,
        # reconciler disposal, operator recover) through this one check is what stops a
        # resolved eject leaving a task armed to stop a printer that already finished.
        if not view.eject_present:
            eject_remote.cancel_eject_timers(printer_id)

        desired = self._desired_policy(view)
        armed = self._armed.get(printer_id)
        if desired is None:
            if armed is not None:
                self.stand_down(printer_id, f"{cause} — plate no longer watchable")
            return
        if armed is not None and armed.policy == desired:
            return
        if armed is not None:
            self.stand_down(printer_id, f"{cause} — policy changed to {type(desired).__name__}")
        # Whose part is on this plate, read off the SAME view the policy came from. The
        # escalation hold pages a human, and the sentence differs for a farm unit's own
        # plate; taking it here keeps one origin and one instant for both facts. (A
        # refused plate's cause rides the policy itself, so it needs no second read.)
        self._arm(printer_id, desired, farm_source=view.plate_source_subtask_id is not None)

    def reconsider(self, printer_id: int, cause: str) -> None:
        """Re-run the driver over the authority's CURRENT view. Idempotent.

        The level-triggered escape hatch: :meth:`on_occupancy_change` fires from the
        authority's fan-out, so a fact that changes OUTSIDE the occupancy record changes
        what :meth:`_desired_policy` would answer while producing no transition to carry
        it. Since 2026-09-13 there is exactly ONE such fact left — a printer RE-ACTIVATED
        after a deactivation stood its watch down (``PATCH /printers/{id}``) — because the
        service hold no longer decides whether a watch exists, only what it may do. This
        asks the question again against the same view a transition would have handed over,
        so a still-gated plate gets its watch back with no restart and no second decision
        table.
        """
        self.on_occupancy_change(printer_id, plate_occupancy.current_view(printer_id), cause)

    @staticmethod
    def _desired_policy(view: OccupancyView) -> OccupancyPolicy | None:
        """Which watch this plate calls for, or None for "no watch at all".

        THE gate between a STORED plate policy and a RUNNING watch — the only place one
        becomes the other — and a PURE function of the plate: a plate nothing stands on,
        or one an eject already owns, carries no watch, and every other plate carries the
        watch its policy names. It takes no printer id, because after 2026-09-13 there is
        no per-PRINTER fact left in the answer.

        **The service hold is deliberately NOT read here (2026-09-13).** A hold changes
        what a watch DOES, never whether it exists: the cooldown is air, not motion, so a
        held printer keeps cooling its plate (fans only) and withholds the EJECT — the
        decision that moves the machine — which is a per-tick level inside the watch
        (``watch_bed_and_clear(held=…)``), not an arm-time gate. Reading the hold here
        cost the operator both halves of the wrong answer: entering mid-cooldown killed
        the watch and switched the fans off over a hot bed, and a print that FINISHED
        during a hold got no fans at all for the length of the hold.
        """
        if not view.plate_occupied or view.eject_present:
            return None
        return view.plate_policy

    def _arm(self, printer_id: int, policy: OccupancyPolicy, *, farm_source: bool = False) -> None:
        """Spawn the watch for ``policy`` and record it. Raises if the spawn fails.

        ``farm_source`` is the caller's read of "the farm deposited this plate", used by
        the escalation hold's page copy. It is an ARM-time value, never re-read.
        """
        release_now: asyncio.Event | None = None
        queue_item_id: int | None = None
        if isinstance(policy, CooldownEject):
            release_now = asyncio.Event()
            queue_item_id = policy.unit_id
            coro = self._watch(printer_id, policy.unit_id, purpose="production", release_now=release_now)
            name = f"eject-cooldown-watch-{printer_id}"
        elif isinstance(policy, FirstArticleEject):
            release_now = asyncio.Event()
            queue_item_id = policy.unit_id
            coro = self._watch(printer_id, policy.unit_id, purpose="fa", run_id=policy.run_id, release_now=release_now)
            name = f"eject-fa-watch-{printer_id}"
        elif isinstance(policy, ForeignAutoEject):
            release_now = asyncio.Event()
            coro = self._watch(
                printer_id,
                None,
                purpose="foreign",
                threshold_override=policy.threshold_c,
                profile_id=policy.profile_id,
                release_now=release_now,
            )
            name = f"eject-foreign-watch-{printer_id}"
        else:
            refusal = policy.refusal if isinstance(policy, EscalationOnly) else None
            coro = self._escalation_only(printer_id, farm_source=farm_source, refusal=refusal)
            name = f"eject-gate-escalation-{printer_id}"
        task = spawn_background_task(coro, name=name)
        self._armed[printer_id] = _ArmedWatch(
            policy=policy, task=task, queue_item_id=queue_item_id, release_now=release_now
        )
        logger.info("Eject monitor: printer %s armed %s", printer_id, type(policy).__name__)

    def stand_down(self, printer_id: int, reason: str) -> asyncio.Task | None:
        """Cancel + deregister the armed watch. Returns the cancelled task, or None.

        Public since 2026-09-12, because a SESSION TEARDOWN needs exactly this act and
        must not grow a second one: the cancellation runs the watch task's own
        ``finally``, which retires the :mod:`cooldown_prep` — plate hold released, fans
        commanded OFF — and it asks ``_cooldown_armed`` whether any successor still
        wants them, so the fans-off decision stays a property of the PRINTER. Calling it
        while the MQTT session is still up is the whole point: the 2026-09-12 010-H2S
        fans ran 6.3 h because the session was torn down first and ``prep.end()`` landed
        on ``skipped:no_client``.

        Since 2026-09-13 its only non-driver caller is
        ``service_hold.quiesce_for_teardown`` — entering maintenance mode no longer
        retires the watch (a held printer keeps cooling, fans only, and withholds the
        eject), so retiring one is now exactly what "the session is about to drop" means.

        **A caller that is about to DROP the session must AWAIT the returned task.**
        ``cancel()`` only SCHEDULES the CancelledError; the ``finally`` that publishes
        ``M106 P2 S0`` / ``M106 P3 S0`` runs on a later loop turn. The 2026-09-12 probe
        caught exactly that gap on the deactivate path — the quiesce logged its report at
        07:07:08,545 and then ``[cooldown-prep] … off=skipped:no_client`` at
        07:07:08,546, because ``update_printer`` had already deleted the client. The task
        is returned rather than awaited here so this stays SYNC for its other caller: the
        policy driver runs inside the authority's fan-out and may not await anything.

        The plate's STORED policy is deliberately untouched — this cancels a running
        watch, it does not decide anything about the plate — so :meth:`reconsider` can
        re-arm it when the printer comes back.
        """
        armed = self._armed.pop(printer_id, None)
        if armed is None:
            return None
        if not armed.task.done():
            armed.task.cancel()
        logger.info("Eject monitor: printer %s watch cancelled (%s)", printer_id, reason)
        return armed.task

    def _release_record(self, printer_id: int, task: asyncio.Task) -> None:
        """Drop the record iff it still belongs to ``task`` (never a successor's)."""
        armed = self._armed.get(printer_id)
        if armed is not None and armed.task is task:
            self._armed.pop(printer_id, None)

    # -- queries ------------------------------------------------------------

    def active_watch(self, printer_id: int) -> float | None:
        """The in-flight cooldown watch's release threshold (°C), or None.

        None both when no watch is armed and when the armed watch carries no threshold
        (an escalation-only hold, or a cooldown watch still resolving its item's
        profile) — callers surface the cooldown phase only when a real release
        temperature exists. Unchanged contract: callers (and the UI ``eject_watch``
        payload) still see ``float | None``."""
        armed = self._armed.get(printer_id)
        return armed.threshold_c if armed is not None else None

    def hold_z(self, printer_id: int) -> float | None:
        """The Z the armed cooldown watch is HOLDING this printer's plate at, or None.

        None covers every "the plate is where the end block left it" case at once: no
        watch, an escalation-only hold, a hold the prep skipped, and a cooldown watch
        that has not finished arming.

        **The CLAIM, not the measurement.** This is what the printer card's "plate raised"
        chip renders, and it survives a service hold on purpose: the plate really is still
        up there until a human lowers it, and the chip is the "do not jog the toolhead"
        warning an operator opening the machine needs most at exactly that moment. The
        estimator's question is the different one — see :meth:`eject_seed_z`."""
        armed = self._armed.get(printer_id)
        return armed.hold_z if armed is not None else None

    def eject_seed_z(self, printer_id: int) -> float | None:
        """The plate height the EJECT may budget its first Z move from, or None.

        Same store as :meth:`hold_z`, one question further: not "where does the farm say
        the plate is" but "can the farm still VOUCH for that height". None when no watch is
        armed, when no hold was sent — and when a service hold has stood during this watch
        (``hold_seen``), because a human may have jogged the bed and a seed is a promise to
        the eject's runtime watchdog about the distance its first move has to cover. An
        absent seed makes the watchdog over-state that distance, which costs a few seconds
        of deadline; a wrong one makes it under-state it, which stops a sweep mid-flight
        and gates the plate for a human. The asymmetry is the whole rule.

        Read at RELEASE time, never bound at arm: the hold can be entered at any point in
        a cooldown that may run for an hour."""
        armed = self._armed.get(printer_id)
        if armed is None or armed.hold_seen:
            return None
        return armed.hold_z

    def deferred(self, printer_id: int) -> bool:
        """Has this printer's watch finished cooling and is it holding the eject back?

        True only while a running watch has met its release condition under a service
        hold: the fans are retired and the sweep is waiting for the hold to lift. It is
        what separates "cooling under a hold" from "cooled, fans off, waiting on the
        hold" on the printer card — two states the hold flag and the watch's existence
        cannot tell apart between them. Same access rule as :meth:`hold_z`: written by
        the owning watch task, read here, and nowhere else."""
        armed = self._armed.get(printer_id)
        return armed.deferred if armed is not None else False

    def _cooldown_armed(self, printer_id: int, *, other_than: asyncio.Task | None) -> bool:
        """Is a RELEASING watch other than ``other_than``'s armed for this printer?

        The one arbiter of "does this printer still want its cooldown fans" at a
        watch's exit — ONE answer for both lanes, because it is a question about the
        PRINTER rather than about either fan. Order-independent by construction:
        whether a successor's ``begin`` ran before or after this exit, the record it
        installed is already in ``_armed`` when ``stand_down`` popped ours — so the fans
        end ON under a cooldown-class successor and OFF under an escalation-only hold
        or no watch at all.
        """
        armed = self._armed.get(printer_id)
        return armed is not None and armed.task is not other_than and not isinstance(armed.policy, EscalationOnly)

    def request_release_now(self, printer_id: int) -> bool:
        """Signal an armed RELEASING watch to sweep immediately (manual "Eject now").

        Returns False when the armed watch cannot release (an escalation-only hold) or
        nothing is armed — the caller then dispatches directly instead."""
        armed = self._armed.get(printer_id)
        if armed is None or armed.release_now is None:
            return False
        armed.release_now.set()
        return True

    # -- watch bodies -------------------------------------------------------

    def _held_level(self, printer_id: int) -> Callable[[], bool]:
        """Mint ONE stateful "is this printer held?" callable for the calling watch.

        The eject lane's whole reading of ``printer_incidents.automation_held`` — one
        callable per watch, handed to :func:`cooldown_prep.begin` at arm and to
        :func:`watch_bed_and_clear` as its per-tick level. It is STATEFUL because the two
        EDGES of the hold are facts only the watch that owns the plate can act on, and it
        sees every read the watch makes:

        * unheld → held sets ``hold_seen``, and deliberately LEAVES ``hold_z``. The plate
          is still raised, and the card's chip is the "do not jog the toolhead" warning an
          operator opening the machine needs; what the farm gives up is the right to offer
          that height as a MEASUREMENT, so ``eject_seed_z()`` stops answering it and the
          eject over-states its drop span instead — the safe direction.
        * held → unheld clears ``deferred``. The eject is permitted again, and the card's
          "cooled · eject deferred" phase must go with the hold that caused it.

        Both writes are guarded by ``armed.task is current_task()``: a watch that has
        already been superseded may not reach into its successor's record.
        """
        last_held = False

        def held_now() -> bool:
            nonlocal last_held
            now_held = printer_incidents.automation_held(printer_id)
            if now_held != last_held:
                last_held = now_held
                armed = self._armed.get(printer_id)
                if armed is not None and armed.task is asyncio.current_task():
                    if now_held:
                        armed.hold_seen = True
                    else:
                        armed.deferred = False
            return now_held

        return held_now

    async def _watch(
        self,
        printer_id: int,
        queue_item_id: int | None,
        *,
        purpose: str = "production",
        run_id: int | None = None,
        threshold_override: float | None = None,
        profile_id: int | None = None,
        release_now: asyncio.Event,
    ) -> None:
        try:
            # ONE hold level for this whole watch — arm, every tick, and the fallback
            # escalation watch below. Minted first so no path can grow a second read.
            held_now = self._held_level(printer_id)
            if threshold_override is not None:
                # Foreign auto-eject: the release threshold is the chosen profile's
                # cooldown target, passed directly (there is no queue item to resolve).
                threshold: float | None = threshold_override
            else:
                threshold = await _resolve_eject_threshold(queue_item_id, for_first_article=purpose == "fa")
            if threshold is None:
                # A releasing policy over a unit that carries no usable eject profile
                # (a deleted profile row, an FA item under a production policy). The
                # plate is still occupied, and a plate with no watch is the armless
                # gate 2026-07-18/07-21 forbids — so hold and escalate instead of
                # returning into silence.
                logger.warning(
                    "Eject monitor: printer %s %s policy resolved NO release threshold (item %s) — "
                    "holding the plate with an escalation-only watch instead",
                    printer_id,
                    purpose,
                    queue_item_id,
                )
                # Same sentence rule as the driver's own escalation arm: the purpose this
                # watch was armed with already says whose plate it is — a production/FA
                # policy carries a farm unit, ``foreign`` by definition does not. No
                # occupancy re-read, for the reason given in the watch's docstring.
                await watch_gate_escalation_only(printer_id, held=held_now, farm_source=purpose != "foreign")
                return
            armed = self._armed.get(printer_id)
            if armed is not None and armed.task is asyncio.current_task():
                # Publish the resolved threshold so active_watch() (and the UI's
                # eject_watch payload) can render the cooldown phase.
                armed.threshold_c = threshold
            # Resolve the plateau/cap policy once at arm; bind the eject dispatch and
            # the stall reaction to THIS unit so the watch stays identity-scoped.
            settings = await _resolve_stall_settings()
            # Both actuators speak over MQTT, and on a restart this watch is re-armed
            # from the hydrated plate BEFORE the lifespan opens the sessions — so wait
            # for the wire first, bounded. On timeout we arm anyway: the prep records
            # an honest skip and the bed poll below is what an offline printer's plate
            # actually needs.
            await _await_printer_connected(printer_id)
            # Arm the cooldown actuators (plate hold + cooldown fans) BEFORE the first
            # bed poll — the whole point is to shorten the wait this loop is about to
            # sit through. ``begin`` never raises: a prep failure leaves the cooldown
            # exactly as it was before this wave, which is a slower cooldown, not a
            # stranded gate. Every input it needs is resolved HERE, once: the prep opens
            # no settings session, and the model it gates the chamber fan on comes from
            # the manager's own cache rather than a second lookup inside it.
            prep = await cooldown_prep.begin(
                printer_id,
                queue_item_id=queue_item_id,
                fans=settings.fans,
                release_threshold_c=threshold,
                model=printer_manager.get_model(printer_id),
                hold_enabled=settings.hold_enabled,
                hold_part_top_mm=settings.hold_part_top_mm,
                # The plate hold MOVES the machine, so it is the one actuator a service
                # hold refuses: hands may be in the printer. The fans are air, and run.
                held=held_now(),
            )
            armed = self._armed.get(printer_id)
            if armed is not None and armed.task is asyncio.current_task():
                armed.hold_z = prep.hold_z if prep.hold == "sent" else None

            def _seeded_release(dispatch, **bound):
                """Wrap a dispatcher so its plate seed is read at RELEASE time.

                Never bound at arm: a service hold entered at any point in a cooldown
                withdraws the farm's right to offer the parked height as a MEASUREMENT
                (``eject_seed_z`` — the plate stays claimed as raised for the card, but a
                human may have jogged the bed), and a cooldown can run for an hour. Read
                back off the record rather than off ``prep``, so the seed and the chip the
                UI renders come from one store, and so a watch whose record was already
                taken over seeds nothing.
                """

                async def _release() -> None:
                    await dispatch(plate_z=self.eject_seed_z(printer_id), **bound)

                return _release

            if purpose == "fa":
                on_release = _seeded_release(
                    _dispatch_fa_eject, printer_id=printer_id, queue_item_id=queue_item_id, run_id=run_id
                )
            elif purpose == "foreign":
                # No hold ever happens on a foreign plate (no unit, no donor, no
                # measured part height), so its eject takes no seed at all — there is
                # nothing to read at release time either.
                on_release = functools.partial(
                    eject_remote.dispatch_identified_foreign_eject, printer_id=printer_id, profile_id=profile_id
                )
            else:
                on_release = _seeded_release(
                    _dispatch_production_eject, printer_id=printer_id, queue_item_id=queue_item_id
                )
            on_stall = functools.partial(_act_on_cooldown_stall, printer_id=printer_id, queue_item_id=queue_item_id)

            def _cooldown_over(cause: str) -> None:
                """The deferral edge: the bed is done, the eject is not allowed yet.

                ONE of the cooling episode's two ends (the exit ``finally`` is the other),
                and the only one that happens with the watch still running: it records the
                deferral on our own record — for the card's "cooled, eject deferred" phase
                — and retires the prep, so the fans stop when the cooling stops instead of
                running for the length of the hold.

                The verdict is ``released``, not a deferral word: the bed DID meet its
                release condition, which is the whole of what a cooldown measures. That
                the sweep is being withheld is the hold's business, and the hold has its
                own durable record.
                """
                record = self._armed.get(printer_id)
                if record is not None and record.task is asyncio.current_task():
                    record.deferred = True
                prep.outcome = "released"
                prep.end(fan_off=True)

            # How the COOLING ended, for the episode ledger. None until the poll
            # returns one, so a cancel or an exception records no verdict rather than
            # an invented one.
            verdict: WatchVerdict | None = None
            try:
                # The fan witness waits INSIDE the guarded span: a cancel during its
                # settle must still retire the prep below, or a fan commanded ON in
                # ``begin`` would outlive the watch that switched it on. The poll it
                # guards also feeds the prep its samples, which is what ends the
                # chamber lane's boost — one loop, two consumers, no second timer.
                await prep.observe_start()
                verdict = await watch_bed_and_clear(
                    printer_id,
                    threshold,
                    stall_window_s=settings.stall_window_s,
                    stall_epsilon_c=settings.stall_epsilon_c,
                    plateau_eject_margin_c=settings.plateau_eject_margin_c,
                    max_hold_s=settings.max_hold_s,
                    on_release=on_release,
                    on_stall=on_stall,
                    release_now=release_now,
                    on_sample=prep.note_sample,
                    held=held_now,
                    on_cooldown_over=_cooldown_over,
                )
            finally:
                # Retire the prep however the poll ended — release, stall, gate clear,
                # exception or cancellation. This inner ``finally`` runs BEFORE the
                # outer one's ``_release_record``, so ``_armed[printer_id]`` still
                # holds OUR record on a normal exit and the SUCCESSOR's (or nothing)
                # on a cancel — which is exactly the question the fans-off asks, for
                # both lanes at once (it is a question about the printer).
                #
                # On a watch that DEFERRED (a service hold withheld its eject) the prep
                # is already retired: this call emits no second summary and only
                # re-attempts a fan whose OFF did not land — the deferral has no eject
                # job to carry the durable ``M106 P2 S0``, so this is that retry.
                #
                # The verdict is written only while the prep is UNRETIRED, which makes
                # the FIRST ``end()`` the measurement in fact and not merely by
                # convention: a deferred cooldown already recorded ``released`` when the
                # cooling ended, and whatever this watch goes on to return hours later
                # — including nothing, on a cancel — cannot rewrite it.
                if not prep.retired:
                    prep.outcome = verdict
                prep.end(fan_off=not self._cooldown_armed(printer_id, other_than=asyncio.current_task()))
        except asyncio.CancelledError:
            raise  # the driver cancelled us because the policy changed — not a failure
        except Exception:  # noqa: BLE001 — a watch failure must not crash the callback loop
            logger.exception("Eject monitor: cooldown watch for printer %s failed", printer_id)
        finally:
            self._release_record(printer_id, asyncio.current_task())

    async def _escalation_only(
        self, printer_id: int, *, farm_source: bool = False, refusal: PlateRefusal | None = None
    ) -> None:
        try:
            # The hold as a per-tick LEVEL, same shape as the cooldown watch's — a plain
            # partial here, because this watch owns no ``hold_z`` and no ``deferred`` and
            # so has no edges of its own to detect. ``farm_source`` and ``refusal`` ride in
            # from the arm, where the plate record was read: they select the page's
            # sentence only.
            await watch_gate_escalation_only(
                printer_id,
                held=functools.partial(printer_incidents.automation_held, printer_id),
                farm_source=farm_source,
                refusal=refusal,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a watch failure must not crash the callback loop
            logger.exception("Eject monitor: foreign-gate escalation watch for printer %s failed", printer_id)
        finally:
            self._release_record(printer_id, asyncio.current_task())


# Module-level singleton, mirroring the other service singletons.
eject_cooldown_monitor = EjectCooldownMonitor()


def wire_policy_driver() -> None:
    """Inject the monitor as the authority's policy driver. Called once at lifespan.

    Only the ``policy_driver`` slot is filled: ``configure`` leaves an omitted callable
    untouched, so this and ``plate_occupancy_store.wire_core`` wire independently. It
    must run BEFORE ``hydrate()``, because hydrating an occupied plate notifies its
    policy and that notification is what re-arms the watch a restart lost.
    """
    plate_occupancy.configure(policy_driver=eject_cooldown_monitor.on_occupancy_change)
