"""The pause-recovery lane: why is this printer PAUSEd, and may the farm answer it?

Two causes live here, and the split of duties is the same for both.

**What this module owns.** DETECTION (a per-push wire sampler, :func:`note_status_push`
— the ONLY trigger of both causes), the RECORD (a ``printer_incident`` row — never a
process dict — plus, for a plate-check episode, its step ledger) and the DECISION:
*answer the prompt*, *hold for a human*, or *stand aside*. Nothing else. Everything that
happens AFTER a terminal — the terminal's classification and whether a refused plate
retries or escalates, the plate gate, the first page and the hourly nag, the requeue, the
held-bed lift — belongs to ``terminal_outcome`` / the plate authority / ``eject.monitor``
/ ``farm_stall`` / ``farm_policy.on_terminal``, which already own run/retry/quarantine and
the one post-terminal motion. This lane never awaits a terminal it does not own and never
imports ``main``. It sends ONE stop of its own: the job whose plate check the farm's own
re-check could not clear (the ladder's second rung, below) — the farm ending its own job,
so a raw ``stop_print`` (``test_code_quality._RAW_STOP_CALLERS``), never the operator's
stop. (A sweep a power cut interrupted is killed by the eject lane's own re-drive,
``eject.remote.redrive_eject_stop``, never composed here.)

``power_loss`` (2026-09-04 fleet outage)
    A power cut rebooted every printer while the server stayed up on its UPS. Each
    came back connected, ``PAUSE``, offering the firmware's own recovery prompt
    ``0300_8007`` — *"There was an unfinished print job when the printer lost power…
    you can try resuming"* — and sat there ~5 h until a human touched its screen. The
    vendored action catalog lists exactly ``RESUME_PRINTING`` / ``STOP_PRINTING`` for
    that code, and printer 8 proved the resume takes (RUNNING one second later). So
    the farm answers it: resume, confirm on the wire, and HOLD for a human only when
    the answer did not take.

``plate_vision`` — the plate-check LADDER (operator rulings 2026-09-04 and 2026-09-29)
    The printer's own pre-print plate check trips: the firmware PAUSEs the job at layer 0
    and shows its plate dialog, carried on ``print_error`` (``hms_errors.plate_check_paused``
    — ``0500_808C`` build-plate offset, ``0500_806E`` foreign objects). The operator's two
    rulings, verbatim:

    * 2026-09-04: "move the bed up away from the bottom, stop the print entirely, and
      escalate to operator rather than leaving the print paused (which has heatbed/nozzle
      at print temp)".
    * 2026-09-29: first press the printer's own "Problem solved, resume" — NOT "Ignore and
      resume" — and "figure out how to send that command correctly". If that re-check
      fails, stop and retry the same print. If the retry's re-check fails too, escalate,
      "and make sure the bed is not at the bottom where the eject assist things are
      bending the plate".

    One trip of one job is one EPISODE: one ``plate_vision`` row (class ``job_pause``),
    opened ``recovering`` and driven by :func:`_plate_check_episode`, one per printer.
    Rung 1 re-checks IN PLACE: it presses "Problem solved, resume"
    (``execute_hms_action(PROBLEM_SOLVED_RESUME)`` — the vendor frame, ``err`` = the decimal
    ``print_error``, ``job_id`` = the paused job), reads the firmware's ACK, and watches the
    wire: RUNNING at layer >= 1 of THIS job passes (the row closes ``recheck_passed`` and the
    print continues); the plate dialog back on the same job, a ``fail`` ACK, a press that did
    not go out, or the job still PAUSEd :data:`_RECHECK_CONFIRM_S` after the press fails it.
    Rung 2 STOPS the print (heaters off), the level re-read before every send; the terminal
    then decides (``terminal_outcome``): the printer's first farm stop inside the window on a
    farm unit with nothing deposited is a RETRY (the unit requeued next in line, no gate, no
    page), anything else ESCALATES (a human-clear gate in the printer's words and a page);
    either way ``farm_policy`` lifts the bed off the release aid. A PAUSE that is not the
    plate check (the power-loss prompt after a reboot, a runout) is HANDED OVER: the row
    closes ``handed_over`` and the power-loss edge is re-armed so its prompt is not stranded.

    The paused hold is only the FALLBACK — the farm cannot end the job (its stop never went
    out, or was not taken in :data:`_STOP_CONFIRM_S`): the row turns ESCALATED, a human's for
    good, and one page asks them to fix the plate and resume. It is not self-heal and is
    never the first answer: on 2026-09-24 the 09-04 lane was replaced by exactly that hold,
    calling the operator's own request "a previous change", and every trip then waited for a
    human, 1 min to 8 h, with the bed down and the heaters at print temperature.

Restart durability (F7)
    Durable: every incident row, and a plate-check episode's step ledger
    (``printer_incident_step`` through :class:`_PlateCheckEvidence`, the store's
    ``EvidenceLog``) — which buttons and stops the driver sent, because the wire cannot
    restate it (the PAUSE reads the same before and after the press). A restart re-enters
    an open ``recovering`` episode while its job is still PAUSEd and derives the owed rung
    from the ledger alone: no press on it → press; a press on it → stop, NEVER a second
    resume. An ``escalated`` episode is the human's and is never re-driven.

    The episode driver's LIVENESS is the store's (``printer_incidents.register_driver`` /
    ``driver_live``), and for this lane it is a GATE: the sampler registers the driver at
    its spawn, before its row exists, and spawns nothing while one is live — the row itself
    cannot refuse a second driver once it exists (re-entry adopts it). A restart empties the
    registry by killing the task, which is the truth. Three more things live in process
    memory, and each names how the wire re-answers it:

    * ``_in_flight`` — the POWER-LOSS driver's handle per printer, and only its: a standing
      prompt spawns one recovery rather than one per push. A restart drops it; the very
      next push re-derives "the prompt is standing" for free and spawns again. "Already
      decided" is the incident row, which survives, so the cost of a restart is at most ONE
      idempotent resume.
    * ``_seen`` — the per-printer wire sample (``connection_epoch`` + the measured
      outage) behind the reconnect EDGE. A restart re-seeds without edging, exactly
      like ``spool_recovery._wire_sample`` and ``hms_edges``: an outage that ended
      before we looked is not an edge we witnessed, and the measured duration is then
      simply unknown (the notification drops the sentence rather than inventing one).
      A plate-check hand-over clears its ``at_prompt``, so a power-loss prompt that stood
      behind the episode (its driver stood aside on the job pause) edges again.
    * ``_summary`` — the fleet accumulator for ONE summary page per outage. A restart
      mid-window loses the counts, which are the whole point of the page; that is
      accepted (F9) and paid for by the log line every window close emits whether or
      not the page fires, so a suppressed page and a dead lane are never confused.

Every entry point here is fire-and-forget and fully guarded (invariant 10): no
farm-side failure may crash the MQTT status flow.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal, TypeGuard, get_args

from backend.app.models.printer_incident import (
    AMS_FAULT_KINDS,
    KIND_PLATE_VISION,
    KIND_POWER_LOSS,
    KIND_Z_REFERENCE_LOST,
    RESOLVE_HANDED_OVER,
    RESOLVE_RECHECK_PASSED,
    STATUS_ESCALATED,
    STATUS_RECOVERING,
    STATUS_RESOLVED,
)
from backend.app.models.printer_incident_step import STEP_KIND_DIALOG, STEP_KIND_STOP, PrinterIncidentStep, StepKind
from backend.app.services import incident_resolution, print_reconcile, printer_incidents
from backend.app.services.hms_actions import HMSAction
from backend.app.services.hms_errors import (
    POWER_LOSS_PROMPT_CODES,
    POWER_LOSS_RESUME_FAILED_CODES,
    full_codes_of,
    plate_check_paused,
    power_loss_hold_active,
    power_loss_prompt_standing,
    power_loss_resume_failed,
    print_error_dialog,
    print_error_short_code,
    summary_of,
)
from backend.app.services.incident_resolution import ClearedEvent, Context, ledger
from backend.app.services.job_identity import is_held_job
from backend.app.services.plate_occupancy import ACTIVE_PRINT_STATES, plate_occupancy
from backend.app.services.printer_manager import printer_manager
from backend.app.services.terminal_outcome import PLATE_RECHECK_WINDOW_S, PlateCheckFacts

if TYPE_CHECKING:
    from backend.app.services.bambu_mqtt import BambuMQTTClient

logger = logging.getLogger(__name__)


# --- latency budget (rows in the AMS skill's table; every number carries its why) ---

# Settle before the lane looks at the wire it is about to act on. MIRRORS
# ``spool_recovery._RUNOUT_RESUME_SETTLE_S`` and is UNMEASURED for this purpose: the
# only figure the 2026-09-04 outage produced — printer 8 resumed "+14 s" after its
# reconnect — is that very constant firing in the refill lane, not a measurement of
# firmware readiness. It also rides out the post-boot SECOND reconnect observed on
# printers 1 and 3 (09:28:58 / 09:29:00, ~60 s after the first), which would otherwise
# cancel a resume mid-flight.
_POWER_LOSS_SETTLE_S = 15.0
# A ``resume_print`` that returns False means the send did not go out — the session is
# mid-churn (the second reconnect above) rather than the firmware refusing. ONE retry,
# far enough out to clear that churn, then the lane holds.
_POWER_LOSS_RETRY_S = 30.0
# How long the wire gets to show RUNNING after an accepted resume. Same figure and same
# reasoning as ``spool_recovery._RUNOUT_RESUME_CONFIRM_S``: an ACK proves acceptance,
# never execution, so every command is confirmed against the wire.
_POWER_LOSS_CONFIRM_S = 30.0
# Poll step of that confirm wait. The caller's budget, not ``await_state``'s.
_POWER_LOSS_POLL_S = 1.0
# The fleet summary's accumulation window, from the FIRST decision. Comfortably longer
# than the 26 s the 2026-09-04 disconnect burst spanned plus the settle above, so one
# outage produces one page.
_SUMMARY_WINDOW_S = 120.0
# How many printers must lose their session together before the farm calls it an
# OUTAGE rather than a printer. Derived from what the discriminator has to survive: a
# stale-reconnect, the maintenance toggle and the 2026-08-25 mark-plate-occupied flow
# each drop ONE session, and a two-printer coincidence is a plausible network blip on a
# shared switch. Three is the smallest count that cannot be any of those.
_OUTAGE_BURST_MIN_PRINTERS = 3
# ...within this window of each other. The 2026-09-04 burst spanned 26 s
# (09:25:37-09:26:03); 120 s is ~4.6x that, which covers a slower rolling brown-out
# without reaching across two unrelated single-printer events.
_OUTAGE_BURST_WINDOW_S = 120.0
# The operator instruction that closes the plate-check FALLBACK page — the one the ladder
# sends only when the farm could not end the job itself (its stop never went out, or was
# not taken). The printer's own words (rendered by the one HMS renderer) come first; this
# says where the print is and what the human does. Sys-admin register: no exclamation, no
# apology, the action named.
_VISION_PAUSED_INSTRUCTION = "Print paused — fix the plate, then resume."

# --- the plate-check ladder's budget (operator ruling 2026-09-29) ---------------------

# How long the "Problem solved, resume" press waits for its firmware ACK. The figure the HMS
# modal's route waits for the same frame (``api/routes/printers.HMS_ACTION_ACK_WAIT_SECONDS``);
# an ACK proves acceptance, never execution, so the watch confirms on the wire whatever it
# answers, and a short wait keeps that watch close behind the press.
_RECHECK_ACK_S = 2.5
# Poll step of that ACK wait. The route's own: the ACK log is appended on the MQTT thread, so
# polling is the whole mechanism.
_RECHECK_ACK_POLL_S = 0.1
# How long the job may run after the press without reaching layer 1 before the driver stops
# watching. UNMEASURED: the start block a resumed job still runs (re-home, levelling, purge)
# takes minutes, so a quarter hour means the layer reading is not coming; the driver then
# exits and the sweep closes the row on the job RUNNING (``observed_running``). It bounds the
# watch in every state — a driver that watched a whole print would hold its row open until
# the terminal, which would read a failure of that print as a refused plate.
_RECHECK_FIRST_LAYER_S = 900.0
# How long a pause may stand after the press before the in-place re-check counts as FAILED.
# The operator's remote plain Resume freed four plate-check pauses within ~1 s (2026-09-25/26),
# so 30 s is ~30x the observed answer; the power-loss confirm's figure (``_POWER_LOSS_CONFIRM_S``).
_RECHECK_CONFIRM_S = 30.0
# How long the farm's stop gets to take the job out of PAUSE, and after it did, how long the
# driver stays live for the terminal closer to end the row. The power-loss confirm's figure: a
# taken stop lands its terminal within seconds.
_STOP_CONFIRM_S = 30.0
# One retry of a stop whose send did not go out (the session mid-churn). Short, unlike the
# power-loss retry: the job waits with the bed and nozzle at print temperature (ruling 2026-09-04).
_VISION_STOP_RETRY_S = 5.0
# Poll step of the episode's watch and waits: the status push cadence (~1 Hz). A faster poll
# reads the same push again.
_EPISODE_POLL_S = 1.0
# The printer's words for a job that is over — a stop taken from PAUSE reads IDLE (or FAILED).
# The job-over vocabulary ``incident_resolution`` and ``print_reconcile`` read the same way.
_JOB_OVER_STATES: frozenset[str] = frozenset({"IDLE", "FAILED", "FINISH"})


# --- process memory (see the module docstring's restart-durability story) -----------


@dataclass(frozen=True)
class _WireSample:
    """What the sampler remembers per printer, to derive a RECONNECT edge from."""

    epoch: int
    # Wall-clock seconds the last observed outage lasted, or None when this process
    # never saw its disconnect edge (a restart mid-outage, or a first-ever connect).
    outage_s: float | None
    # Whether the LAST push showed the power-loss prompt standing. The driver spawns
    # on the RISING edge of this only: a prompt that stands for hours (a hold the
    # firmware refused to release, a foreign print nobody resumes) would otherwise
    # spawn a fresh 15 s driver on every push once the previous one exited, each one
    # re-reading the incident, re-logging "standing aside" and re-recording the
    # printer into the summary — a driver every ~16 s for the whole hold. The first
    # push after a restart has no previous sample, so it IS a rising edge: that is
    # the restart-durability story (one re-derived decision, then the row decides).
    at_prompt: bool = False


@dataclass
class _OutageSummary:
    """One accumulator per outage window. Printer ids, not counts, so a printer that
    is decided twice inside the window cannot inflate the page."""

    opened_at: float
    resumed: set[int] = field(default_factory=set)
    held: set[int] = field(default_factory=set)
    stopped_ejects: set[int] = field(default_factory=set)
    held_by_fault: set[int] = field(default_factory=set)
    outage_s: float | None = None


_in_flight: dict[int, asyncio.Task] = {}
_seen: dict[int, _WireSample] = {}
_summary: _OutageSummary | None = None
_summary_task: asyncio.Task | None = None


def _reset_state() -> None:
    """Test hook: drop every piece of process memory between cases."""
    global _summary, _summary_task
    for task in list(_in_flight.values()):
        task.cancel()
    _in_flight.clear()
    _seen.clear()
    if _summary_task is not None:
        _summary_task.cancel()
    _summary_task = None
    _summary = None


# --- entry point 1: the per-push sampler -------------------------------------------


def note_status_push(printer_id: int, state) -> None:
    """Per-push wire sampler. Sync, DB-free, in-memory — and it NEVER raises.

    Rides the ~1 Hz status push beside ``spool_recovery.note_demand_watch``, and is the
    ONLY trigger of this lane. It does exactly three things:

    * spawns ONE power-loss recovery driver while the prompt stands
      (:func:`hms_errors.power_loss_hold_active` = live ``PAUSE`` and ``0300_8007``
      standing), deduped by :data:`_in_flight` so a prompt that stands for minutes
      still gets a single driver;
    * spawns the plate-check episode driver on a TRIP or a RE-ENTRY
      (:func:`_sample_plate_check`), one per printer through the store's liveness gate;
    * derives the RECONNECT edge (``connection_epoch`` advanced) and, when the fleet's
      disconnect anchors say this was an OUTAGE rather than one printer, arms the
      lost-Z-reference hold for a printer that came back with a part on its plate.

    Nothing else lives here. The PAUSE->RUNNING edge that ENDS a power-loss hold is
    already ``spool_recovery.on_observed_running``'s, spawned from the sampler beside
    this one: the incident machine's close paths are kind-agnostic, so a hold this lane
    opened is closed by the operator's screen resume with no code of its own.
    """
    try:
        epoch = int(getattr(state, "connection_epoch", 0) or 0)
        anchor = getattr(state, "disconnected_at", None)
        prev = _seen.get(printer_id)

        outage_s = prev.outage_s if prev is not None else None
        reconnected = prev is not None and epoch > prev.epoch
        if reconnected and anchor is not None:
            # The session we just lost ended NOW; ``disconnected_at`` is kept across the
            # reconnect precisely so this subtraction is possible.
            outage_s = max(0.0, time.time() - float(anchor))
        at_prompt = power_loss_hold_active(state)
        was_at_prompt = prev.at_prompt if prev is not None else False
        _seen[printer_id] = _WireSample(epoch=epoch, outage_s=outage_s, at_prompt=at_prompt)

        if reconnected:
            # AHEAD of the maintenance-mode return, and deliberately the only thing that
            # is. The lost-Z row is a REFUSAL RECORD, not a recovery act: it opens no
            # driver, resumes nothing, stops nothing and moves nothing — all it does is
            # make every LATER eject refuse until a human clears it. So it sits outside
            # the "the farm stands aside while a human owns the machine" ruling, which is
            # about the farm ACTING. An outage that lands during a hold destroys the Z
            # datum exactly as one outside a hold does, and skipping the record here
            # would leave the printer with a trustworthy-looking Z frame that is fiction
            # — the 2026-09-04 bed-past-the-floor mechanism, arrived at by omission.
            _maybe_arm_z_reference_hold(printer_id, anchor)

        if printer_incidents.automation_held(printer_id):
            # MAINTENANCE MODE: the power-loss prompt and the plate dialog are the
            # operator's to answer — they are standing at the screen. Nothing is resumed,
            # pressed or stopped.
            #
            # The wire sample above is still recorded, deliberately: the sampler's memory
            # stays current, so an edge that happened DURING the hold is consumed by the
            # hold rather than replayed at the printer the moment it is released.
            if reconnected or (at_prompt and not was_at_prompt):
                logger.info(
                    "[pause-recovery] printer %s is in maintenance mode — standing aside (%s)",
                    printer_id,
                    "reconnected" if reconnected else "at the power-loss prompt",
                )
            return

        _sample_plate_check(printer_id, state)

        if not at_prompt or was_at_prompt:
            # Level, not edge: the prompt either is not standing, or it was already
            # standing at the last push and its one driver has decided (or is deciding).
            return
        task = _in_flight.get(printer_id)
        if task is not None and not task.done():
            return
        subtask = (getattr(state, "subtask_id", None) or "").strip() or None

        from backend.app.core.tasks import spawn_background_task

        logger.info(
            "[pause-recovery] printer %s is at the power-loss prompt (job %s) — recovery driver spawned",
            printer_id,
            subtask or "-",
        )
        _in_flight[printer_id] = spawn_background_task(
            _recover_power_loss(printer_id, subtask),
            name=f"power-loss-recover-p{printer_id}",
        )
    except Exception:  # noqa: BLE001 — invariant 10: never crash the status flow
        logger.exception("[pause-recovery] status sampler failed for printer %s", printer_id)


# --- the power-loss driver ----------------------------------------------------------


async def _recover_power_loss(printer_id: int, observed_subtask: str | None) -> None:
    """Decide what the farm may do about the power-loss prompt on ``printer_id``.

    ``observed_subtask`` is the job id the sampler saw AT the trip; the decision is
    relative to it, which is the only way "the printer moved on" can be told from "the
    printer is still holding the job we saw".

    Branches, IN ORDER, one log line each — the order is the contract:

    (a) STAND DOWN — disconnected / no longer PAUSE / the prompt is gone / the job
        changed. The operator beat us, or this is not the state we decided on.
    (b) INTERRUPTED EJECT — the printer holds a pending sweep. A sweep is NEVER
        resumed (the 2026-07-31 gouged-plate class): the eject lane re-drives its OWN
        kill. And unless the model's Z re-reference has been laddered, the printer also
        earns the lost-Z hold — it rebooted mid-sweep WITH the part on the plate, so
        its Z frame is fiction and the operator's later "Eject plate" must be refused
        until that part comes off by hand.
    (c) OPEN INCIDENT — another lane already owns this printer; open nothing, stand
        aside (F7). For a runout hold this is not a gap: the refill lane resumes on
        PRESENCE evidence, and that resume answers the power-loss prompt too (printer 8
        proved it on 2026-09-04). A JOB-PAUSE hold (the printer's plate check paused this
        job) stands this lane aside too: resuming would restart the print onto the plate
        the printer refused with no re-check, and the answer is the plate-check episode's
        (its "Problem solved, resume", then a stop; a human's in its fallback). If the
        episode HANDS OVER — the pause is not the plate check's — it re-arms this lane's
        rising edge, so the prompt is answered on the next push.
    (d) RESUME — farm unit or FOREIGN print alike (operator ruling: parity). Success is
        a LOG LINE and a summary entry, with no durable record; failure is a HOLD.
    """
    try:
        await asyncio.sleep(_POWER_LOSS_SETTLE_S)
        state = printer_manager.get_status(printer_id)
        stand_down = _stand_down_reason(state, observed_subtask)
        if stand_down is not None:
            logger.info(
                "[pause-recovery] printer %s power-loss recovery stood down after the settle — %s",
                printer_id,
                stand_down,
            )
            return

        if plate_occupancy.eject_identity(printer_id) is not None:
            await _stop_interrupted_eject(printer_id)
            return

        if printer_incidents.job_pause_held(printer_id):
            # The printer paused this job at its own pre-print plate check, and the power
            # cut came during that pause. Answering the firmware's prompt with a resume
            # would answer the plate question too — with the plate the printer just refused
            # still on the bed and no re-check. The one predicate every job-resuming lane
            # reads. The plate-check episode owns that pause: its re-entry sees a PAUSE
            # that is no longer the plate check's and HANDS OVER, re-arming this lane's
            # edge; an escalated episode is the human's, whose resume answers both.
            logger.info(
                "[pause-recovery] printer %s is held at a job pause (the printer's plate check) — standing "
                "aside, the plate-check episode answers first",
                printer_id,
            )
            _record(printer_id, "held_by_fault")
            return

        from backend.app.core.database import async_session

        # The AMS-kind rows and this lane's OWN: a runout's refill lane answers the
        # prompt on its own (the printer-8 proof), a physical hold means the path is
        # still broken, and a standing ``power_loss`` row means this printer's resume
        # was already refused or failed and a human owns the prompt. A lost-Z row is not
        # a reason to leave a resumable print sitting on the firmware's prompt (the
        # resumed print re-homes before it moves); a job-pause row is, and was read above.
        async with async_session() as db:
            incident = await printer_incidents.get_open(db, printer_id, kinds=AMS_FAULT_KINDS | {KIND_POWER_LOSS})
        if incident is not None:
            logger.info(
                "[pause-recovery] printer %s held by an open %s incident (%s) — standing aside, "
                "the prompt is that lane's to answer",
                printer_id,
                incident.kind,
                incident.status,
            )
            _record(printer_id, "held_by_fault")
            return

        await _resume_after_power_loss(printer_id, observed_subtask)
    except Exception:  # noqa: BLE001 — an assist lane must never crash its caller
        logger.exception("[pause-recovery] power-loss recovery failed for printer %s", printer_id)
    finally:
        _in_flight.pop(printer_id, None)


def _stand_down_reason(state, observed_subtask: str | None) -> str | None:
    """Why branch (a) applies, or None when the decision still stands."""
    if state is None or not getattr(state, "connected", False):
        return "printer is not connected"
    live = (getattr(state, "state", None) or "").upper()
    if live != "PAUSE":
        return f"live state is {live or 'unknown'}, not PAUSE (the operator resumed or stopped it)"
    if not power_loss_prompt_standing(getattr(state, "hms_errors", None) or []):
        return "the power-loss prompt is no longer standing"
    live_subtask = (getattr(state, "subtask_id", None) or "").strip() or None
    if live_subtask != observed_subtask:
        return f"the job changed ({observed_subtask or '-'} -> {live_subtask or '-'})"
    return None


async def _stop_interrupted_eject(printer_id: int) -> None:
    """Branch (b): kill the sweep the outage interrupted, and hold the plate for hands."""
    from backend.app.services.eject.remote import redrive_eject_stop

    stopped = await redrive_eject_stop(printer_id, stage="power_loss")
    logger.warning(
        "[pause-recovery] printer %s rebooted mid-EJECT — the sweep is stopped, never resumed (re-drive %s)",
        printer_id,
        "sent" if stopped else "found nothing to stop",
    )
    _record(printer_id, "stopped_ejects")
    await _open_z_reference_hold(printer_id, cause="rebooted mid-eject with the part still on the plate")


async def _resume_after_power_loss(printer_id: int, observed_subtask: str | None) -> None:
    """Branch (d): answer the prompt, confirm it took, or hold for a human."""
    client = printer_manager.get_client(printer_id)
    if client is None:
        await _hold_power_loss(printer_id, observed_subtask, reason="resume_refused")
        return

    if not client.resume_print():
        logger.info(
            "[pause-recovery] printer %s resume_print send returned False (session mid-churn?) — one retry in %.0fs",
            printer_id,
            _POWER_LOSS_RETRY_S,
        )
        await asyncio.sleep(_POWER_LOSS_RETRY_S)
        stand_down = _stand_down_reason(printer_manager.get_status(printer_id), observed_subtask)
        if stand_down is not None:
            logger.info("[pause-recovery] printer %s power-loss resume retry stood down — %s", printer_id, stand_down)
            return
        if not client.resume_print():
            await _hold_power_loss(printer_id, observed_subtask, reason="resume_refused")
            return

    if not await _confirm_running(printer_id):
        await _hold_power_loss(printer_id, observed_subtask, reason="resume_failed")
        return

    outage = _outage_minutes(printer_id)
    logger.info(
        "[pause-recovery] printer %s RESUMED after power loss (outage %s, job %s, origin %s)",
        printer_id,
        f"~{outage} min" if outage is not None else "unknown",
        observed_subtask or "-",
        await _origin_label(printer_id, observed_subtask),
    )
    # Success is deliberately NOT a notification (the 2026-08-10 notification diet):
    # nothing is asked of anyone. The fleet summary names the count.
    _record(printer_id, "resumed")


async def _confirm_running(printer_id: int) -> bool:
    """Wait for RUNNING, but give up the moment the firmware says the resume FAILED.

    ``0300_400D`` ("Resume failed after power loss") is the catalog's own verdict on
    the command we just sent. Waiting out the remaining confirm budget after it appears
    would only delay the page; and a retry is exactly what the firmware just refused.
    """
    deadline = asyncio.get_running_loop().time() + _POWER_LOSS_CONFIRM_S
    while asyncio.get_running_loop().time() < deadline:
        state = printer_manager.get_status(printer_id)
        if state is not None:
            if (getattr(state, "state", None) or "").upper() == "RUNNING":
                return True
            if power_loss_resume_failed(getattr(state, "hms_errors", None) or []):
                logger.warning(
                    "[pause-recovery] printer %s reported %s — the firmware declined the resume, no retry",
                    printer_id,
                    sorted(POWER_LOSS_RESUME_FAILED_CODES),
                )
                return False
        await asyncio.sleep(_POWER_LOSS_POLL_S)
    state = printer_manager.get_status(printer_id)
    return state is not None and (getattr(state, "state", None) or "").upper() == "RUNNING"


# The two ways a resume does not take, as whole sentences. They read differently to an
# operator and neither of them is "automatic resume is off" — there is no such toggle
# (operator ruling 2026-09-04).
_HOLD_REASON_COPY: dict[str, str] = {
    "resume_refused": "The printer did not accept the resume command.",
    "resume_failed": "The printer did not restart the print after the resume was accepted.",
}


async def _hold_power_loss(printer_id: int, observed_subtask: str | None, *, reason: str) -> None:
    """The one HOLD path: a durable incident, its projection, and one page.

    Never quarantines and never retries past this point. The printer is left exactly
    where the firmware put it, with the prompt still on its screen — which is the only
    place a human can answer it now.
    """
    from backend.app.core.database import async_session
    from backend.app.models.printer import Printer
    from backend.app.services.notification_service import notification_service

    state = printer_manager.get_status(printer_id)
    hms_list = getattr(state, "hms_errors", None) or []
    codes = sorted(POWER_LOSS_PROMPT_CODES)
    if power_loss_resume_failed(hms_list):
        codes += sorted(POWER_LOSS_RESUME_FAILED_CODES)
    # The printer's own words for the prompt, recorded before a human's answer clears it.
    full_codes = full_codes_of(hms_list, codes)

    async with async_session() as db:
        item = await _printing_farm_unit(db, printer_id, observed_subtask)
        incident = await printer_incidents.open_new(
            db,
            printer_id=printer_id,
            job_id=observed_subtask or "",
            item_id=item.id if item is not None else None,
            kind=KIND_POWER_LOSS,
            code=sorted(POWER_LOSS_PROMPT_CODES)[0],
            codes=",".join(codes),
            slot_global_tray=None,
            hms_full_codes=full_codes,
            status=STATUS_ESCALATED,
        )
        if incident is None:
            logger.info(
                "[pause-recovery] printer %s already has an open incident — power-loss hold not opened",
                printer_id,
            )
            return
        if item is not None:
            item.waiting_reason = printer_incidents.waiting_reason_for(KIND_POWER_LOSS)
            await db.commit()

        printer = await db.get(Printer, printer_id)
        printer_name = printer.name if printer is not None else f"printer {printer_id}"
        job_name = (getattr(state, "subtask_name", None) or "").strip() or "print"
        await notification_service.on_power_loss_hold(
            printer_id=printer_id,
            printer_name=printer_name,
            job_name=job_name,
            outage_minutes=_outage_minutes(printer_id),
            reason=_HOLD_REASON_COPY[reason],
            db=db,
        )

    logger.warning(
        "[pause-recovery] printer %s HELD at the power-loss prompt (%s) — incident %s, job %s",
        printer_id,
        reason,
        incident.id,
        observed_subtask or "-",
    )
    _record(printer_id, "held")


async def _printing_farm_unit(db, printer_id: int, subtask: str | None):
    """The farm unit this hold projects onto, or None for a FOREIGN print.

    ``item_id`` NULL is the established shape for a printer held over a print the farm
    did not dispatch — the hold, the chip and the hourly reminder are printer-scoped
    and work identically without a unit to project onto.
    """
    from backend.app.services.farm_correlation import resolve_printing_item

    return await resolve_printing_item(db, printer_id, subtask)


async def _origin_label(printer_id: int, subtask: str | None) -> str:
    """ "farm" or "foreign", for the success log line only."""
    from backend.app.core.database import async_session

    async with async_session() as db:
        item = await _printing_farm_unit(db, printer_id, subtask)
    return "farm" if item is not None else "foreign"


# --- the lost-Z-reference hold: opening it -------------------------------------------


def _maybe_arm_z_reference_hold(printer_id: int, anchor: float | None) -> None:
    """Sync half of the outage arm: is this reconnect part of a FLEET outage?

    A single printer coming back is NOT this hold's trigger, deliberately. An MQTT
    session boundary is not a power cycle — the 60 s stale-reconnect, the maintenance
    toggle and the 2026-08-25 mark-plate-occupied flow all produce one — so the
    discriminator has to be a fact only an outage produces: several printers losing
    their sessions together.

    The signature is read from the FLEET's live ``PrinterState``s rather than from a
    ledger of edges this module keeps: ``disconnected_at`` is kept across the reconnect
    for exactly this purpose, so the anchors of every printer in the burst are already
    on the wire by the time the first one comes back. That is also why an arm can be
    derived at the FIRST reconnect instead of waiting for the third.
    """
    if anchor is None:
        return
    together = sum(
        1
        for st in printer_manager.get_all_statuses().values()
        if getattr(st, "disconnected_at", None) is not None
        and abs(float(st.disconnected_at) - float(anchor)) <= _OUTAGE_BURST_WINDOW_S
    )
    if together < _OUTAGE_BURST_MIN_PRINTERS:
        logger.info(
            "[pause-recovery] printer %s reconnected but only %s printer(s) lost their session together — "
            "not an outage, no lost-Z hold",
            printer_id,
            together,
        )
        return

    from backend.app.core.tasks import spawn_background_task

    spawn_background_task(
        _z_reference_arm(printer_id, together),
        name=f"z-reference-arm-p{printer_id}",
    )


async def _z_reference_arm(printer_id: int, burst_size: int) -> None:
    """Async half: hold this printer if it came back with a part on its plate.

    Runs behind the same settle as the resume driver, for the same reason — printers 1
    and 3 reconnected a SECOND time ~60 s after the first on 2026-09-04, and a state
    read on the first of those describes a printer that is about to vanish again.
    """
    try:
        await asyncio.sleep(_POWER_LOSS_SETTLE_S)
        state = printer_manager.get_status(printer_id)
        if state is None or not getattr(state, "connected", False):
            logger.info("[pause-recovery] printer %s not connected at the lost-Z check — standing down", printer_id)
            return
        live = (getattr(state, "state", None) or "").upper()
        if live in ACTIVE_PRINT_STATES:
            # A printer at the power-loss prompt is PAUSE, and the resume branch owns
            # it: a resumed print's own firmware re-homes before it moves again, so its
            # Z frame stops being fiction without anyone touching the plate.
            logger.info(
                "[pause-recovery] printer %s reconnected in %s (an active job owns it) — no lost-Z hold",
                printer_id,
                live,
            )
            return
        if not plate_occupancy.is_plate_occupied(printer_id):
            logger.info(
                "[pause-recovery] printer %s reconnected with a clear plate — no lost-Z hold (outage of %s printers)",
                printer_id,
                burst_size,
            )
            return
        await _open_z_reference_hold(
            printer_id,
            cause=f"rebooted with a part on the plate ({burst_size} printers lost their session together)",
        )
    except Exception:  # noqa: BLE001 — an assist lane must never crash its caller
        logger.exception("[pause-recovery] lost-Z arm failed for printer %s", printer_id)


async def _open_z_reference_hold(printer_id: int, *, cause: str) -> bool:
    """Open the ``z_reference_lost`` hold unless this model's Z ladder has been run.

    Fail-CLOSED on the geometry: no registry row means no witnessed Z stop for this
    machine, which is the same answer as an unvalidated row. The eject lane refuses
    while the hold stands (``eject.remote.z_reference_evidence``); ``clear_plate`` /
    ``operator_recover`` close it through :func:`on_plate_cleared`, because the human
    who takes the part off IS the resolution.
    """
    from backend.app.core.database import async_session
    from backend.app.models.printer import Printer
    from backend.app.services.eject.geometry import get_geometry
    from backend.app.services.notification_service import notification_service

    async with async_session() as db:
        printer = await db.get(Printer, printer_id)
        model = printer.model if printer is not None else printer_manager.get_model(printer_id)
        geometry = await get_geometry(db, model)
        if geometry is not None and geometry.z_reference_validated:
            logger.info(
                "[pause-recovery] printer %s (%s) re-references Z in its own eject block — no lost-Z hold",
                printer_id,
                geometry.model_key,
            )
            return False
        incident = await printer_incidents.open_new(
            db,
            printer_id=printer_id,
            job_id="",
            item_id=None,
            kind=KIND_Z_REFERENCE_LOST,
            code=KIND_POWER_LOSS,
            codes="",
            slot_global_tray=None,
            status=STATUS_ESCALATED,
        )
        if incident is None:
            logger.info("[pause-recovery] printer %s already has an open incident — lost-Z hold not opened", printer_id)
            return False
        printer_name = printer.name if printer is not None else f"printer {printer_id}"
        await notification_service.on_z_reference_lost(printer_id=printer_id, printer_name=printer_name, db=db)

    logger.warning(
        "[pause-recovery] printer %s HELD for a lost Z reference — %s; ejects are refused until the part "
        "is removed by hand and the plate marked cleared",
        printer_id,
        cause,
    )
    return True


# --- the plate-check episode: its step vocabulary and its terminal facts ------------
#
# The plate-check ladder (operator ruling 2026-09-29): press the printer's own "Problem
# solved, resume"; if that re-check fails, stop the print and retry the same print; if the
# retry's re-check fails too, escalate. The episode's durable memory is the incident's step
# ledger (``printer_incidents.EvidenceLog``, its one writer), because the wire cannot restate
# it: the PAUSE reads the same before and after the press, and after a restart a second press
# is exactly what re-entry must never send. Two kinds are written, each with its own closed
# name and answer vocabulary below.

# The dialog buttons the ladder presses, named by the action ``execute_hms_action`` takes — one
# spelling for the press and its ledger row. Only "Problem solved, resume": "Ignore" would skip
# the re-check the ladder exists to run.
_PLATE_CHECK_BUTTONS: frozenset[str] = frozenset({HMSAction.PROBLEM_SOLVED_RESUME})
# The stop verb the ladder's second rung sends (``printer_manager.stop_print``, the raw stop).
PLATE_CHECK_STOP_VERB = "stop"

# What the READ of a dialog press answered. Closed.
#   ``success`` / ``fail`` — the firmware's ACK of the press (``CommandAck.succeeded``);
#   ``no_ack``             — the press went out and no ACK arrived within the budget;
#   ``not_sent``           — nothing went out (``execute_hms_action`` returned ``None``).
PlateCheckDialogAnswer = Literal["success", "fail", "no_ack", "not_sent"]
# What the READ of the farm's stop answered. Closed.
#   ``taken``     — the job left PAUSE (the stop reached a terminal) within the budget;
#   ``not_taken`` — the job was still PAUSEd when the budget ran out;
#   ``not_sent``  — the raw stop never went out (``stop_print`` returned False).
# The last two are the stop that did NOT end the job: the driver then hands the paused print
# to a human (the escalated hold and its page), so a later end of that job is the human's —
# never the farm's stop (:meth:`_PlateCheckEvidence.farm_stopped`).
PlateCheckStopNotTaken = Literal["not_taken", "not_sent"]
PlateCheckStopAnswer = Literal["taken", PlateCheckStopNotTaken]
_STOP_NOT_TAKEN: frozenset[str] = frozenset(get_args(PlateCheckStopNotTaken))

# The two ledger kinds this lane writes (``StepKind``'s own values), each with its names...
_PLATE_CHECK_NAMES: dict[str, frozenset[str]] = {
    STEP_KIND_DIALOG: _PLATE_CHECK_BUTTONS,
    STEP_KIND_STOP: frozenset({PLATE_CHECK_STOP_VERB}),
}
# ...and its answers.
_PLATE_CHECK_ANSWERS: dict[str, frozenset[str]] = {
    STEP_KIND_DIALOG: frozenset(get_args(PlateCheckDialogAnswer)),
    STEP_KIND_STOP: frozenset(get_args(PlateCheckStopAnswer)),
}


def _is_plate_check_kind(token: str) -> TypeGuard[StepKind]:
    return token in _PLATE_CHECK_NAMES


@dataclass(frozen=True)
class PlateCheckStep:
    """One thing the plate-check episode's driver sent: a dialog button pressed, or the stop.

    ``outcome`` is ``None`` from the send until the read (a crash between the two leaves it
    ``None`` for good — the press or the stop went out and nobody saw what it did, which is
    exactly the fact re-entry needs). Its token is the kind's own closed vocabulary
    (:data:`PlateCheckDialogAnswer` / :data:`PlateCheckStopAnswer`).
    """

    seq: int
    kind: StepKind
    name: str
    outcome: str | None
    at: datetime | None

    @classmethod
    def dialog(cls, button: str = HMSAction.PROBLEM_SOLVED_RESUME) -> PlateCheckStep:
        """An unsent press of ``button``; the log's ``note`` assigns its ``seq`` and ``at``."""
        if button not in _PLATE_CHECK_BUTTONS:
            raise LookupError(f"pause_recovery: the plate-check ladder presses no {button!r}")
        return cls(seq=0, kind=STEP_KIND_DIALOG, name=str(button), outcome=None, at=None)

    @classmethod
    def stop(cls) -> PlateCheckStep:
        """An unsent stop; the log's ``note`` assigns its ``seq`` and ``at``."""
        return cls(seq=0, kind=STEP_KIND_STOP, name=PLATE_CHECK_STOP_VERB, outcome=None, at=None)

    def entry(self) -> printer_incidents.StepEntry:
        """The ledger row this step is written as: its kind and name, no tray, no feeder."""
        return printer_incidents.StepEntry(kind=self.kind, name=self.name)

    def sent(self, seq: int, at: datetime) -> PlateCheckStep:
        return replace(self, seq=seq, at=at)

    def answered(self, outcome: str) -> PlateCheckStep:
        """The read's answer. A token outside THIS kind's vocabulary RAISES."""
        if outcome not in _PLATE_CHECK_ANSWERS[self.kind]:
            raise LookupError(f"pause_recovery: plate-check {self.kind} step {self.seq} answered with {outcome!r}")
        return replace(self, outcome=outcome)


class _PlateCheckEvidence(printer_incidents.EvidenceLog[PlateCheckStep]):
    """THE evidence log of one plate-check episode: every press and stop its driver sent, in
    send order, with what the wire answered.

    The store's :class:`~backend.app.services.printer_incidents.EvidenceLog` (noted at the send,
    answered at the read, built only by ``from_row``, the ledger's one writer) bound to this
    lane's vocabulary, :class:`PlateCheckStep`. Two readings hang off it: the rung a re-entered
    driver owes (no ``dialog`` step → press; a ``dialog`` step → never press again), and
    :meth:`farm_stopped`, the terminal's reading of who ended the job.
    """

    def farm_stopped(self) -> bool:
        """Did the FARM's stop end this episode's job?

        True for a ``stop`` step that is ``taken`` OR still unanswered: the job's terminal
        usually lands while the driver is still watching the wire for its answer, and a stop
        the farm sent that nobody has read yet is still the farm's stop. False when the only
        stop steps were answered as NOT having ended the job (:data:`PlateCheckStopNotTaken`):
        the driver then handed the paused print to a human, so whoever ends it later — a
        human's Stop, most likely — ended it, not the farm. False with no stop step at all:
        an operator who stops the print mid-episode writes none.
        """
        return any(step.kind == STEP_KIND_STOP and step.outcome not in _STOP_NOT_TAKEN for step in self.steps)

    @classmethod
    def _step_of(cls, row: PrinterIncidentStep) -> PlateCheckStep:
        """Hydrate ONE ledger row. A token this lane's vocabulary cannot name RAISES — a
        ledger that says something the driver cannot read is drift, never a skipped row."""
        if not _is_plate_check_kind(row.kind):
            raise LookupError(f"pause_recovery: incident {row.incident_id} step {row.seq} has kind {row.kind!r}")
        if row.name not in _PLATE_CHECK_NAMES[row.kind]:
            raise LookupError(
                f"pause_recovery: incident {row.incident_id} step {row.seq} names no plate-check {row.kind}: "
                f"{row.name!r}"
            )
        if row.outcome is not None and row.outcome not in _PLATE_CHECK_ANSWERS[row.kind]:
            raise LookupError(f"pause_recovery: incident {row.incident_id} step {row.seq} answers {row.outcome!r}")
        return PlateCheckStep(seq=row.seq, kind=row.kind, name=row.name, outcome=row.outcome, at=row.sent_at)


async def plate_check_facts(
    printer_id: int, open_incidents: Sequence[Mapping[str, object]], job_id: str | None
) -> PlateCheckFacts | None:
    """What the terminal of ``job_id`` owes to its plate-check episode — or ``None`` when that
    job had none. Never raises.

    ``open_incidents`` is the printer's open-hold projection ``main.on_print_complete`` took
    ONCE, ahead of every closer (the episode's row is one of the holds this terminal closes).
    The episode is the ``plate_vision`` row that paused THIS job (``job_identity.is_held_job``);
    another job's row explains nothing about this terminal.

    * ``farm_stopped`` — the farm's stop ended the job because its re-check failed
      (:meth:`_PlateCheckEvidence.farm_stopped`: a ``stop`` step taken, or not yet answered).
      A stop answered as not taken hands the print to a human, and an operator's own Stop
      writes no step at all — both escalate.
    * ``stops_in_window`` — how many of this printer's OTHER plate-check episodes, opened within
      ``terminal_outcome.PLATE_RECHECK_WINDOW_S``, the farm already stopped (one store read,
      ``printer_incidents.count_rows_with_step``, excluding this job's own rows).

    Its own session, through ``run_with_retry``. Fails CLOSED: an unreadable ledger returns
    facts that escalate (``farm_stopped=False``) and logs a WARNING — a retry nobody could
    justify is the one outcome this read must never produce.
    """
    episode = next(
        (
            incident
            for incident in open_incidents
            if incident.get("kind") == KIND_PLATE_VISION and is_held_job(job_id, str(incident.get("job_id") or ""))
        ),
        None,
    )
    if episode is None:
        return None
    try:
        from backend.app.core.database import run_with_retry

        incident_id = episode.get("id")
        if not isinstance(incident_id, int):
            raise LookupError(f"the plate-check row's projection carries no id: {incident_id!r}")
        since = datetime.utcnow() - timedelta(seconds=PLATE_RECHECK_WINDOW_S)

        async def _read(db) -> PlateCheckFacts:
            log = await _PlateCheckEvidence.from_row(db, incident_id)
            stops = await printer_incidents.count_rows_with_step(
                db,
                printer_id=printer_id,
                kind=KIND_PLATE_VISION,
                step_kind=STEP_KIND_STOP,
                since=since,
                exclude_job_id=job_id,
            )
            return PlateCheckFacts(farm_stopped=log.farm_stopped(), stops_in_window=stops)

        facts = await run_with_retry(_read, label="plate-check facts")
    except Exception:  # noqa: BLE001 — the terminal must go on; an unread episode escalates
        logger.warning(
            "[pause-recovery] printer %s plate-check facts for job %s unreadable — the terminal escalates",
            printer_id,
            job_id or "-",
            exc_info=True,
        )
        return PlateCheckFacts(farm_stopped=False, stops_in_window=0)
    logger.info(
        "[pause-recovery] printer %s plate-check episode %s at the terminal of job %s: farm_stopped=%s "
        "stops_in_window=%s",
        printer_id,
        incident_id,
        job_id or "-",
        facts.farm_stopped,
        facts.stops_in_window,
    )
    return facts


# --- entry point 2: the plate-check episode (its trigger and its driver) -------------


def _live_job(state) -> str:
    """The printer's live ``subtask_id`` ("" when it names none)."""
    return (getattr(state, "subtask_id", None) or "").strip()


def _sample_plate_check(printer_id: int, state) -> None:
    """The plate-check episode's ONE trigger — spawn its driver on a TRIP or a RE-ENTRY.

    Level-triggered on purpose: the question "is a plate check standing that nobody is
    answering?" is re-asked on every push, so nothing depends on witnessing an edge, and a
    restart re-derives it from the wire and the row for free. Every guard reads a fact the
    wire or the store restates at once:

    * a FRESH report only (``print_reconcile.is_fresh``) — a reconnect re-broadcasts the
      previous session's cached PAUSE before the pushall answers;
    * no live driver on the printer (``printer_incidents.driver_live``, the gate), and no
      eject owning it (``plate_occupancy.eject_identity``, the power-loss driver's own
      check — a sweep is never pressed or stopped from here);
    * the open ``plate_vision`` row decides which occasion this is:

      - none, and the printer is PAUSEd at its plate check (``plate_check_paused``) → TRIP;
      - ``recovering``, bound to the live job (``job_identity.is_held_job``), and the job
        still ``PAUSE`` — ANY pause: after a printer reboot ``print_error`` may be the
        power-loss prompt, and the driver must then hand over rather than leave the row
        standing — → RE-ENTRY (a restart killed its driver);
      - ``escalated`` is the human's, and a ``recovering`` row of another job, or of a job
        that is not paused, is its closers' — nothing.

    The driver is registered in the SAME synchronous stretch as its spawn
    (``register_driver``, before its row exists), so a second push cannot spawn a second
    driver while the first is still opening its row. Maintenance mode never reaches here
    (:func:`note_status_push` returns first). Guarded on its own: a failure here must not
    cost the power-loss decision that follows it on the same push.
    """
    try:
        if not print_reconcile.is_fresh(state):
            return
        if printer_incidents.driver_live(printer_id):
            return
        if plate_occupancy.eject_identity(printer_id) is not None:
            return
        job = _live_job(state)
        snap = printer_incidents.snapshot(printer_id, kind=KIND_PLATE_VISION)
        if snap is None:
            if not plate_check_paused(state):
                return
            incident_id = None
        elif (
            snap.get("status") == STATUS_RECOVERING
            and is_held_job(job, str(snap.get("job_id") or ""))
            and (getattr(state, "state", None) or "").upper() == "PAUSE"
        ):
            incident_id = snap.get("id")
        else:
            return

        from backend.app.core.tasks import spawn_background_task

        task = spawn_background_task(_plate_check_episode(printer_id, job), name=f"plate-check-p{printer_id}")
        printer_incidents.register_driver(printer_id, task, incident_id=incident_id)
    except Exception:  # noqa: BLE001 — invariant 10: never crash the status flow
        logger.exception("[pause-recovery] plate-check sampler failed for printer %s", printer_id)


@dataclass(frozen=True)
class _Episode:
    """The row one plate-check driver acts for, as it opened or adopted it."""

    printer_id: int
    job: str
    incident_id: int
    item_id: int | None


async def _plate_check_episode(printer_id: int, job: str) -> None:
    """Drive ONE plate-check episode of ``job`` down the ladder (operator ruling 2026-09-29).

    Opens the episode's row, or adopts the one a restart left ``recovering``; reads the
    OWED rung from its ledger alone — no ``dialog`` step: re-check in place
    (:func:`_recheck_in_place`); a ``dialog`` step: stop (:func:`_stop_the_print`), never a
    second resume. Never raises; always gives its liveness slot back.
    """
    try:
        from backend.app.core.database import async_session

        episode = await _open_episode(printer_id, job)
        if episode is None:
            return
        async with async_session() as db:
            log = await _PlateCheckEvidence.from_row(db, episode.incident_id)
        if log.has_step(STEP_KIND_DIALOG):
            failed: str | None = "the press is already on the episode's ledger (re-entered after a restart)"
        else:
            failed = await _recheck_in_place(episode, log)
        if failed is not None:
            await _stop_the_print(episode, log, why=failed)
    except Exception:  # noqa: BLE001 — a driver must never crash the event loop
        logger.exception("[pause-recovery] plate-check episode failed for printer %s", printer_id)
    finally:
        task = asyncio.current_task()
        if task is not None:
            printer_incidents.release_driver(printer_id, task)


async def _open_episode(printer_id: int, job: str) -> _Episode | None:
    """Open the episode's ``recovering`` row — or adopt the open one, or stand down.

    A TRIP opens the row: bound to the paused job, to the FARM unit printing it
    (``farm_correlation.resolve_printing_farm_item`` — by job identity, farm units only; a
    foreign print holds the printer with ``item_id`` NULL), carrying the dialog's short code
    and the printer's full code (``hms_full_codes``, its words after the dialog is gone), and
    projecting the hold onto the unit's ``waiting_reason``.

    When the row cannot open (one ``plate_vision`` row per printer), the open one is ADOPTED
    only while it is ``recovering`` and bound to this same job — a restart's re-entry.
    Anything else — an escalated row (the human's), another job's row — stands the driver
    down.
    """
    from backend.app.core.database import async_session
    from backend.app.services.farm_correlation import resolve_printing_farm_item

    state = printer_manager.get_status(printer_id)
    async with async_session() as db:
        incident = None
        item = None
        if plate_check_paused(state) and is_held_job(_live_job(state), job):
            short = print_error_short_code(int(state.print_error))
            item = await resolve_printing_farm_item(db, printer_id, job)
            incident = await printer_incidents.open_new(
                db,
                printer_id=printer_id,
                job_id=job,
                item_id=item.id if item is not None else None,
                kind=KIND_PLATE_VISION,
                code=short,
                codes=short,
                slot_global_tray=None,
                hms_full_codes=full_codes_of(getattr(state, "hms_errors", None) or [], [short]),
                status=STATUS_RECOVERING,
            )
        if incident is not None:
            if item is not None:
                item.waiting_reason = printer_incidents.waiting_reason_for(KIND_PLATE_VISION)
                await db.commit()
            logger.warning(
                "[pause-recovery] printer %s plate check tripped %s (%s, job %s) — episode %s opened, "
                "the farm re-checks in place",
                printer_id,
                incident.code,
                f"unit {item.id}" if item is not None else "foreign print",
                job or "-",
                incident.id,
            )
            return _Episode(printer_id=printer_id, job=job, incident_id=incident.id, item_id=incident.item_id)

        row = await printer_incidents.get_open(db, printer_id, kinds={KIND_PLATE_VISION})
        if row is None or row.status != STATUS_RECOVERING or not is_held_job(job, row.job_id):
            logger.info(
                "[pause-recovery] printer %s plate-check episode not driven — %s",
                printer_id,
                "no plate-check pause and no episode to re-enter"
                if row is None
                else f"its open episode {row.id} is {row.status} on job {row.job_id or '-'}",
            )
            return None
        logger.warning(
            "[pause-recovery] printer %s plate-check episode %s re-entered (%s, job %s, code %s) — the owed "
            "rung is read from its ledger",
            printer_id,
            row.id,
            f"unit {row.item_id}" if row.item_id is not None else "foreign print",
            job or "-",
            row.code,
        )
        return _Episode(printer_id=printer_id, job=job, incident_id=row.id, item_id=row.item_id)


# What the level re-read before every send says. ``plate_pause`` is the only one a send may
# follow; ``other_pause`` is handed over; the rest stand the driver down (the row's closers,
# the sweep or the re-entry own what comes next).
_Level = Literal["plate_pause", "other_pause", "stale", "held", "other_job", "not_paused"]


@dataclass(frozen=True)
class _LevelReading:
    level: _Level
    state: object | None
    why: str


def _read_level(episode: _Episode) -> _LevelReading:
    """Re-read the level a send rests on: the printer is still PAUSEd at its plate check, on
    this episode's job, on a fresh report, and not under a service hold."""
    state = printer_manager.get_status(episode.printer_id)
    if not print_reconcile.is_fresh(state):
        return _LevelReading("stale", state, "the printer is not on a fresh session")
    if printer_incidents.automation_held(episode.printer_id):
        return _LevelReading("held", state, "the printer is in maintenance mode")
    live_job = _live_job(state)
    if not is_held_job(live_job, episode.job):
        return _LevelReading("other_job", state, f"the printer is on another job ({live_job or '-'})")
    live = (getattr(state, "state", None) or "").upper()
    if live != "PAUSE":
        return _LevelReading("not_paused", state, f"the job is {live or 'unknown'}, not PAUSE")
    if plate_check_paused(state):
        return _LevelReading("plate_pause", state, "paused at the plate check")
    return _LevelReading("other_pause", state, _other_pause_why(state))


def _dialog_of(state) -> int:
    """The printer's CURRENT dialog (``print_error``), or 0 for none or a status/phase word."""
    return print_error_dialog(int(getattr(state, "print_error", 0) or 0))


def _other_pause_why(state) -> str:
    """Why a PAUSE is not the plate check's, in words for the hand-over line."""
    dialog = _dialog_of(state)
    if dialog:
        return f"the job is paused for {print_error_short_code(dialog)}, not the plate check"
    return "the job is paused with no plate-check dialog"


def _row_open(episode: _Episode) -> bool:
    """Is THIS episode's row still open? The store's projection, DB-free."""
    snap = printer_incidents.snapshot(episode.printer_id, kind=KIND_PLATE_VISION)
    return snap is not None and snap.get("id") == episode.incident_id


async def _recheck_in_place(episode: _Episode, log: _PlateCheckEvidence) -> str | None:
    """Rung 1: press the printer's own "Problem solved, resume", read the ACK, watch the wire.

    Returns WHY the re-check failed when rung 2 is owed; ``None`` when the episode ended some
    other way (passed, handed over, stood down).
    """
    pid = episode.printer_id
    reading = _read_level(episode)
    if reading.level == "other_pause":
        await _hand_over(episode, reading.why)
        return None
    if reading.level != "plate_pause":
        logger.info(
            "[pause-recovery] printer %s plate-check episode %s stood down — %s", pid, episode.incident_id, reading.why
        )
        return None

    print_error = int(getattr(reading.state, "print_error", 0) or 0)
    client = printer_manager.get_client(pid)
    seq = await log.note(PlateCheckStep.dialog())
    pressed_at = asyncio.get_running_loop().time()
    sent = (
        client.execute_hms_action(f"{print_error:08X}", HMSAction.PROBLEM_SOLVED_RESUME, episode.job)
        if client is not None
        else None
    )
    if sent is None:
        await log.answer(seq, "not_sent")
        logger.warning(
            "[pause-recovery] printer %s plate check: problem-solved resume answer=not_sent (episode %s)",
            pid,
            episode.incident_id,
        )
        return "the press did not go out"
    logger.info(
        "[pause-recovery] printer %s plate check: problem-solved resume sent (seq %s, err %s) — episode %s, job %s",
        pid,
        sent.sequence_id,
        print_error,
        episode.incident_id,
        episode.job or "-",
    )
    ack = await client.await_ack(sent, _RECHECK_ACK_S, _RECHECK_ACK_POLL_S)
    answer: PlateCheckDialogAnswer = "no_ack" if ack is None else ("success" if ack.succeeded else "fail")
    await log.answer(seq, answer)
    logger.info(
        "[pause-recovery] printer %s plate check: problem-solved resume answer=%s (episode %s%s)",
        pid,
        answer,
        episode.incident_id,
        f", reason {ack.reason}" if ack is not None and ack.reason else "",
    )
    if answer == "fail":
        return "the firmware answered fail"
    return await _watch_recheck(episode, pressed_at)


async def _watch_recheck(episode: _Episode, pressed_at: float) -> str | None:
    """Watch the wire after the press, the driver live throughout. Returns why the re-check
    FAILED (rung 2 is owed), or ``None`` when the episode ended otherwise.

    Each poll, in order:

    (a) the row closed, the printer left its session, or it is on another job → exit: the
        closers or the re-entry rule own the row;
    (b) the plate dialog back on the same job after it left it (the job RUNNING, or the
        dialog cleared) → FAILED;
    (c) a PAUSE on another dialog (a real dialog code that is not a plate code) → HAND OVER.
        A PAUSE with NO dialog is not one: the press clears the dialog a push or two before
        the job leaves PAUSE, so that reading waits for (b), (d) or (e);
    (d) RUNNING at layer >= 1 of THIS job (:func:`_job_layer`, off the client's one peaks
        reader) → PASSED. A RUNNING whose layer this client cannot measure (it attached the job
        mid-flight), or that has not reached layer 1 :data:`_RECHECK_FIRST_LAYER_S` after it
        began, exits instead: the sweep closes a job RUNNING for its dwell, and a plate
        dialog that comes back re-enters at rung 2;
    (e) PAUSEd (never having left it, or again with no dialog) :data:`_RECHECK_CONFIRM_S`
        since the press or the pause began → FAILED; rung 2's level read hands a pause that
        is not the plate check's over.

    A job the printer reports over exits too: its terminal closes the row.
    """
    pid = episode.printer_id
    loop = asyncio.get_running_loop()
    left_the_dialog = False
    paused_since: float | None = pressed_at
    running_since: float | None = None
    while True:
        if not _row_open(episode):
            logger.info(
                "[pause-recovery] printer %s plate-check episode %s closed by its closers", pid, episode.incident_id
            )
            return None
        state = printer_manager.get_status(pid)
        if not print_reconcile.is_fresh(state):
            logger.info(
                "[pause-recovery] printer %s plate-check episode %s: the printer left its session — the "
                "re-entry answers on its return",
                pid,
                episode.incident_id,
            )
            return None
        live_job = _live_job(state)
        if not is_held_job(live_job, episode.job):
            logger.info(
                "[pause-recovery] printer %s plate-check episode %s: the printer is on another job (%s)",
                pid,
                episode.incident_id,
                live_job or "-",
            )
            return None
        live = (getattr(state, "state", None) or "").upper()
        now = loop.time()
        if live != "RUNNING":
            running_since = None
        if live == "PAUSE":
            dialog = _dialog_of(state)
            if dialog and not plate_check_paused(state):
                await _hand_over(episode, _other_pause_why(state))
                return None
            if not dialog:
                left_the_dialog = True
            elif left_the_dialog:
                return "the plate dialog came back on the same job"
            if paused_since is None:
                paused_since = now
            if now - paused_since >= _RECHECK_CONFIRM_S:
                return f"still paused {_RECHECK_CONFIRM_S:.0f}s after the press"
        else:
            left_the_dialog = True
            paused_since = None
            if live in _JOB_OVER_STATES:
                logger.info(
                    "[pause-recovery] printer %s plate-check episode %s: the job reads %s — its terminal closes "
                    "the episode",
                    pid,
                    episode.incident_id,
                    live,
                )
                return None
            if live == "RUNNING":
                layer = _job_layer(printer_manager.get_client(pid))
                if layer is None:
                    logger.info(
                        "[pause-recovery] printer %s plate-check episode %s: the job is RUNNING but this client "
                        "did not see it start, so its layer is no measurement — the sweep closes it on RUNNING, a "
                        "returning plate dialog re-enters",
                        pid,
                        episode.incident_id,
                    )
                    return None
                if layer >= 1:
                    await _close_passed(episode, layer)
                    return None
                if running_since is None:
                    running_since = now
                if now - running_since >= _RECHECK_FIRST_LAYER_S:
                    logger.info(
                        "[pause-recovery] printer %s plate-check episode %s: the job has run %.0fs without reaching "
                        "layer 1 — the sweep closes it on RUNNING, a returning plate dialog re-enters",
                        pid,
                        episode.incident_id,
                        now - running_since,
                    )
                    return None
        await asyncio.sleep(_EPISODE_POLL_S)


def _job_layer(client: BambuMQTTClient | None) -> int | None:
    """The highest layer THIS job has printed — or ``None`` when this client's reading is no
    measurement of this job.

    Read off the client's ONE peaks reader (``BambuMQTTClient.job_peaks``, the same
    reading the terminal payload carries): :attr:`~backend.app.services.bambu_mqtt.JobPeaks.peak_layer_num`
    counts the live layer only behind the stale-predecessor gate (the firmware's republish
    of the PREVIOUS job's layer is never this job's), and ``reliable`` False — a client that
    attached the job mid-flight (a restart) and holds a baseline it cannot tell from a
    predecessor's republish — reads ``None``: absence of measurement is not a measurement.
    """
    if client is None:
        return None
    peaks = client.job_peaks()
    return peaks.peak_layer_num if peaks.reliable else None


async def _close_passed(episode: _Episode, layer: int) -> None:
    """The re-check passed: close the row ``recheck_passed`` (a farm close) and clear the
    unit's hold projection. The same job prints on."""
    from backend.app.core.database import async_session

    async with async_session() as db:
        row = await printer_incidents.close(
            db, episode.incident_id, status=STATUS_RESOLVED, source=RESOLVE_RECHECK_PASSED
        )
        if row is not None:
            await printer_incidents.clear_hold_projection(db, episode.item_id)
    logger.info(
        "[pause-recovery] printer %s plate check: re-check passed at layer %s — episode %s closed, job %s prints on",
        episode.printer_id,
        layer,
        episode.incident_id,
        episode.job or "-",
    )


async def _hand_over(episode: _Episode, why: str) -> None:
    """The job is paused, but not by its plate check (the power-loss prompt after a reboot, a
    runout): close the row ``handed_over``, clear the unit's hold projection, and re-arm the
    power-loss rising edge — that driver stood aside on the job pause while this row stood,
    so without the re-arm its prompt would be stranded."""
    from backend.app.core.database import async_session

    async with async_session() as db:
        row = await printer_incidents.close(db, episode.incident_id, status=STATUS_RESOLVED, source=RESOLVE_HANDED_OVER)
        if row is not None:
            await printer_incidents.clear_hold_projection(db, episode.item_id)
    sample = _seen.get(episode.printer_id)
    if sample is not None:
        _seen[episode.printer_id] = replace(sample, at_prompt=False)
    logger.info(
        "[pause-recovery] printer %s plate check: handed over (%s) — episode %s closed",
        episode.printer_id,
        why,
        episode.incident_id,
    )


async def _stop_the_print(episode: _Episode, log: _PlateCheckEvidence, *, why: str) -> None:
    """Rung 2: the farm STOPS the print whose re-check failed (the heaters go off with it).

    The level is re-read BEFORE every send: a pause that is no longer the plate check's is
    handed over, anything else stands the driver down. One retry of a send that did not go
    out, :data:`_VISION_STOP_RETRY_S` later. The stop is confirmed on the wire
    (``printer_manager.await_state``, the job over within :data:`_STOP_CONFIRM_S`) and
    answered on the ledger: ``taken`` → stay live until the terminal closer ends the row,
    bounded by the same budget; ``not_taken`` / ``not_sent`` → the FALLBACK (:func:`_escalate`).
    What the terminal makes of the stop — a retry or an escalation — is ``terminal_outcome``'s.
    """
    pid = episode.printer_id
    logger.warning(
        "[pause-recovery] printer %s plate check: re-check failed (%s) — stopping the print (episode %s, job %s)",
        pid,
        why,
        episode.incident_id,
        episode.job or "-",
    )
    reading = _read_level(episode)
    if reading.level == "other_pause":
        await _hand_over(episode, reading.why)
        return
    if reading.level != "plate_pause":
        logger.info(
            "[pause-recovery] printer %s plate-check episode %s stood down before the stop — %s",
            pid,
            episode.incident_id,
            reading.why,
        )
        return

    seq = await log.note(PlateCheckStep.stop())
    sent = printer_manager.stop_print(pid)
    if not sent:
        logger.info(
            "[pause-recovery] printer %s plate check: the stop did not go out — one retry in %.0fs",
            pid,
            _VISION_STOP_RETRY_S,
        )
        await asyncio.sleep(_VISION_STOP_RETRY_S)
        reading = _read_level(episode)
        if reading.level == "plate_pause":
            sent = printer_manager.stop_print(pid)
        elif reading.level != "stale":
            # The level moved while the send was out: the stop never went out and never will.
            await log.answer(seq, "not_sent")
            if reading.level == "other_pause":
                await _hand_over(episode, reading.why)
            else:
                logger.info(
                    "[pause-recovery] printer %s plate-check episode %s stood down before the stop's retry — %s",
                    pid,
                    episode.incident_id,
                    reading.why,
                )
            return

    answer: PlateCheckStopAnswer
    if not sent:
        answer = "not_sent"
    elif await printer_manager.await_state(
        pid, set(_JOB_OVER_STATES), _STOP_CONFIRM_S, poll_interval_s=_EPISODE_POLL_S
    ):
        answer = "taken"
    else:
        answer = "not_taken"
    await log.answer(seq, answer)
    logger.log(
        logging.INFO if answer == "taken" else logging.WARNING,
        "[pause-recovery] printer %s plate check: stop answer=%s (episode %s, job %s)",
        pid,
        answer,
        episode.incident_id,
        episode.job or "-",
    )
    if answer != "taken":
        await _escalate(episode, answer)
        return

    # Taken: the job's terminal closes the row (its closer runs whether or not a driver is
    # live). Stay live meanwhile, so no other closer reads the row as nobody's.
    deadline = asyncio.get_running_loop().time() + _STOP_CONFIRM_S
    while _row_open(episode) and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(_EPISODE_POLL_S)
    if _row_open(episode):
        logger.info(
            "[pause-recovery] printer %s plate-check episode %s: the stop was taken but no terminal closed it "
            "within %.0fs — the sweep closes it once the printer reports the job over",
            pid,
            episode.incident_id,
            _STOP_CONFIRM_S,
        )


async def _escalate(episode: _Episode, answer: PlateCheckStopNotTaken) -> None:
    """The FALLBACK: the farm could not end the job, so the paused print is a human's.

    ``mark_escalated`` — the row stays open and the farm never acts on it again (the sampler
    reads ``escalated`` as the human's) — and ONE page in the printer's own words (the row's
    recorded ``hms_full_codes``) with :data:`_VISION_PAUSED_INSTRUCTION`. The human's resume
    or stop ends it through the ordinary closers.
    """
    from backend.app.core.database import async_session
    from backend.app.models.printer import Printer
    from backend.app.services.notification_service import notification_service

    pid = episode.printer_id
    async with async_session() as db:
        row = await printer_incidents.mark_escalated(db, episode.incident_id)
        if row is None:
            logger.info(
                "[pause-recovery] printer %s plate-check episode %s closed before the fallback — nothing to hold",
                pid,
                episode.incident_id,
            )
            return
        printer = await db.get(Printer, pid)
        printer_name = printer.name if printer is not None else f"printer {pid}"
        words = summary_of(printer_incidents.printer_messages_of(row)) or row.code
        await notification_service.on_plate_not_empty(
            pid,
            printer_name,
            db,
            # The catalog's own sentences end in a period; the page adds exactly one.
            source_detail=f"The printer reported: {words.rstrip('.')}. {_VISION_PAUSED_INSTRUCTION}",
        )
    logger.warning(
        "[pause-recovery] printer %s plate check: the stop was %s — print left PAUSED for a human, episode %s "
        "escalated (%s, job %s)",
        pid,
        answer.replace("_", " "),
        episode.incident_id,
        f"unit {episode.item_id}" if episode.item_id is not None else "foreign print",
        episode.job or "-",
    )


# --- entry point 3: the operator's clear --------------------------------------------


async def on_plate_cleared(printer_id: int, *, recover: bool = False) -> list[tuple[int, str]]:
    """Close the holds whose resolution IS the operator's act. Returns ``[(id, kind)]``.

    Called from the clear-plate route (routine, ``recover=False``) and from
    ``farm_policy.recover_printer`` (``recover=True``). WHICH rows each verb answers
    is :mod:`incident_resolution`'s table, per row — a printer can hold several, and
    the two verbs are different statements:

    * ``operator`` rows (a lost Z frame) close on BOTH: the evidence a human produces is
      the part coming off the plate;
    * ``repair`` rows (an AMS physical fault) close on RECOVER only. Recover means
      "an operator inspected the machine" (it discards every stored belief about the
      plate too), which is exactly the third return-to-normal the repair class admits.
      A routine clear-plate says nothing about the filament path;
    * ``wire``, ``job_pause`` and ``declared`` rows are never closed here — a paused
      plate check is answered by resuming or stopping its job, not by a plate act.

    It returns WHAT IT CLOSED rather than a bare boolean because both verbs report it
    to the operator (``incidents_closed``): a Recover that silently ended an equipment
    fault is indistinguishable, at the UI, from one that did nothing — which is how
    011-H2S 2026-09-17 produced a day of "Mark plate cleared" clicks against a hold
    neither verb was ever going to touch.
    """
    try:
        from backend.app.core.database import async_session

        ctx = Context(
            state=printer_manager.get_status(printer_id),
            ledger=ledger,
            driver_live=False,
            cleared=ClearedEvent(recover=recover),
        )
        closed: list[tuple[int, str]] = []
        async with async_session() as db:
            for incident in await printer_incidents.open_rows(db, printer_id):
                verdict = incident_resolution.resolve(incident, "plate_cleared", ctx)
                if not verdict.close:
                    logger.info(
                        "[pause-recovery] printer %s plate cleared (%s), but its open %s incident %s is left "
                        "standing: %s",
                        printer_id,
                        "recover" if recover else "clear-plate",
                        incident.kind,
                        incident.id,
                        verdict.evidence,
                    )
                    continue
                row = await printer_incidents.close(db, incident.id, status=STATUS_RESOLVED, source=verdict.source)
                if row is not None:
                    closed.append((row.id, row.kind))
        for incident_id, kind in closed:
            logger.info(
                "[pause-recovery] printer %s %s hold %s closed — the operator %s",
                printer_id,
                kind,
                incident_id,
                "recovered the printer" if recover else "cleared the plate",
            )
        return closed
    except Exception:  # noqa: BLE001 — an operator verb must never fail on its hold cleanup
        logger.exception("[pause-recovery] plate-cleared hold close failed for printer %s", printer_id)
        return []


# --- the fleet summary --------------------------------------------------------------


def _outage_minutes(printer_id: int) -> int | None:
    """Whole minutes this printer was off the wire, or None when we never saw it go.

    None is deliberate and is rendered as "no duration sentence" rather than as zero:
    a restart mid-outage erases the measurement, and a plausible wrong number in a
    notification is worse than a missing one.
    """
    sample = _seen.get(printer_id)
    if sample is None or sample.outage_s is None:
        return None
    return int(sample.outage_s // 60)


def _record(printer_id: int, bucket: str) -> None:
    """Add one printer to the open outage window, opening the window if needed."""
    global _summary, _summary_task

    if _summary is None:
        _summary = _OutageSummary(opened_at=time.time())
        from backend.app.core.tasks import spawn_background_task

        _summary_task = spawn_background_task(_close_summary_window(), name="power-loss-summary")
    getattr(_summary, bucket).add(printer_id)
    outage_s = _seen.get(printer_id).outage_s if _seen.get(printer_id) is not None else None
    if outage_s is not None and (_summary.outage_s is None or outage_s > _summary.outage_s):
        # The LONGEST outage in the window is the one that describes it: printers come
        # back at their own pace, and the fleet was down for however long the last one
        # took.
        _summary.outage_s = outage_s


async def _close_summary_window() -> None:
    """Emit ONE summary per outage, and ALWAYS log the close.

    The log line is not decoration: a page suppressed by a disabled provider and a
    lane that never ran are indistinguishable on notification history alone, and this
    lane's whole failure mode is silence (F9).
    """
    global _summary, _summary_task
    try:
        await asyncio.sleep(_SUMMARY_WINDOW_S)
        summary, _summary = _summary, None
        if summary is None:
            return
        minutes = int(summary.outage_s // 60) if summary.outage_s is not None else None
        logger.info(
            "[pause-recovery] outage window closed — resumed=%s held=%s stopped_ejects=%s held_by_fault=%s outage=%s",
            sorted(summary.resumed),
            sorted(summary.held),
            sorted(summary.stopped_ejects),
            sorted(summary.held_by_fault),
            f"~{minutes} min" if minutes is not None else "unknown",
        )
        from backend.app.core.database import async_session
        from backend.app.services.notification_service import notification_service

        async with async_session() as db:
            await notification_service.on_power_loss_recovery_summary(
                resumed=len(summary.resumed),
                held=len(summary.held),
                stopped_ejects=len(summary.stopped_ejects),
                held_by_fault=len(summary.held_by_fault),
                outage_minutes=minutes,
                db=db,
            )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — a summary failure must not take the lane down
        logger.exception("[pause-recovery] outage summary failed")
    finally:
        _summary_task = None
