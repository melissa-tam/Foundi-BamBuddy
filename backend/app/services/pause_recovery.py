"""The pause-recovery lane: why is this printer PAUSEd, and may the farm answer it?

Two causes live here, and the split of duties is the same for both.

**What this module owns.** DETECTION (a per-push wire sampler, :func:`note_status_push`
— the ONLY trigger of both causes), the RECORD (a ``printer_incident`` row — never a
process dict — plus, for a plate-check episode, its step ledger) and the DECISION:
*answer the prompt*, *hold for a human*, or *stand aside*. Nothing else. Everything that
happens AFTER a terminal — the terminal's classification and its reading of a refused
plate's recorded retry-or-escalate decision, the plate gate, the first page and the hourly
nag, the requeue, the held-bed lift — belongs to ``terminal_outcome`` / the plate authority
/ ``eject.monitor`` / ``farm_stall`` / ``farm_policy.on_terminal``, which already own
run/retry/quarantine and the one post-terminal motion. This lane never awaits a terminal it
does not own and never imports ``main``. It sends ONE stop of its own: the job whose plate
check the farm's own re-check could not clear (the ladder's rungs, below) — the farm ending
its own job, so a raw ``stop_print`` (``test_code_quality._RAW_STOP_CALLERS``), never the
operator's stop. (A sweep a power cut interrupted is killed by the eject lane's own re-drive,
``eject.remote.redrive_eject_stop``, never composed here.)

``power_loss`` (2026-09-04 fleet outage)
    A power cut rebooted every printer while the server stayed up on its UPS. Each
    came back connected, ``PAUSE``, offering the firmware's own recovery prompt
    ``0300_8007`` — *"There was an unfinished print job when the printer lost power…
    you can try resuming"* — and sat there ~5 h until a human touched its screen. The
    vendored action catalog lists exactly ``RESUME_PRINTING`` / ``STOP_PRINTING`` for
    that code, and printer 8 proved the resume takes (RUNNING one second later). So
    the farm answers it on the session's first fresh report showing the prompt: its
    resume is an ACTION read through the record (``printer_action`` — RUNNING reaches it,
    however late), and the farm HOLDS for a human when the printer REFUSED it (its
    fail ACK, or its own "Resume failed after power loss", ``0300_400D``) or did not
    carry it out within production's bound (``_POWER_LOSS_SELF_HEAL``, online time).

``plate_vision`` — the plate-check LADDER (operator rulings 2026-09-04, 2026-09-29, 2026-10-05)
    The printer's own pre-print plate check trips: the firmware PAUSEs the job at layer 0
    and shows its plate dialog, carried on ``print_error`` (``hms_errors.plate_check_paused``
    — ``0500_808C`` build-plate offset, ``0500_806E`` foreign objects). The operator's
    rulings, verbatim where quoted:

    * 2026-09-04: "move the bed up away from the bottom, stop the print entirely, and
      escalate to operator rather than leaving the print paused (which has heatbed/nozzle
      at print temp)".
    * 2026-09-29: first press the printer's own "Problem solved, resume" — NOT "Ignore and
      resume" — and "figure out how to send that command correctly". If that re-check
      fails, stop and retry the same print. If the retry's re-check fails too, escalate,
      "and make sure the bed is not at the bottom where the eject assist things are
      bending the plate".
    * 2026-10-05 (the last rung, settled): rungs 1–2 stand. When the stop would be an
      ESCALATION, the farm leaves the print PAUSED at the dialog, raises the hold and pages
      once; a person may Ignore and resume, press Problem solved, or Stop; after
      ``farm_plate_check_decision_minutes`` (default 10) the farm stops the print and the
      terminal escalates as before (gate, page, held-bed lift). It amends the 09-04 "stop,
      never leave paused" ruling by a bounded window.

    One trip of one job is one EPISODE: one ``plate_vision`` row (class ``job_pause``),
    opened ``recovering`` and driven by :func:`_plate_check_episode`, one per printer. What is
    owed at every step is ONE transition table over (row × ledger × wire), :func:`_owed_rung`.
    Rung 1 re-checks IN PLACE: it presses "Problem solved, resume"
    (``execute_hms_action(PROBLEM_SOLVED_RESUME)`` — the vendor frame, ``err`` = the decimal
    ``print_error``, ``job_id`` = the paused job) as an action read through the record, and
    watches the wire: RUNNING at layer >= 1 of THIS job passes (the row closes ``recheck_passed``
    and the print continues); the plate dialog back on the same job, or the printer's ``fail``
    ACK, fails it. Events decide first, however late they land; production's self-heal steps stay
    as the fallback behind them (operator ruling 2026-10-12: "if it was self heal, it should stay
    self heal"): a job still PAUSEd on its job when :data:`_RECHECK_SELF_HEAL` passes after the
    press — accepted or not, dialog standing or not — has FAILED its re-check (rung 2), and a job
    RUNNING that has not reached layer 1 when :data:`_RECHECK_FIRST_LAYER_SELF_HEAL` passes is left
    to the row's closers. A press that never left the farm, or one the record voided, is pressed
    again on the printer's next session.
    Rung 2 DECIDES ONCE (:func:`_decide`) and records the decision as the name of the stop it
    sends: a farm unit with nothing deposited that is not itself the retry of a plate the farm
    already stopped and retried (the lineage, ruling 2026-09-29), on a printer whose last
    plate-check stop was followed by a completed print (the operator's "2 failures between a
    successful print"), is a RETRY — the print is stopped (heaters off) as ``retry_stop`` and the
    terminal requeues the unit next in line, no gate, no page. Anything else is the DECISION
    HOLD (:func:`_enter_hold`, ruling 2026-10-05): the print stays PAUSED at the dialog, the
    row turns ESCALATED, one page, the driver exits, and a person's turn is on
    (``printer_incidents.plate_check_human_turn``; their press goes through
    :func:`human_dialog_action`). The window's end is a TIMER (:func:`_decision_deadline`):
    the farm stops the print as ``deadline_stop`` and the terminal ESCALATES (a human-clear
    gate in the printer's words and a page). Either stop's terminal has ``farm_policy`` lift
    the bed off the release aid. A PAUSE that is not the plate check (the power-loss prompt
    after a reboot, a runout) is HANDED OVER while the ladder acts: the row closes
    ``handed_over`` and the power-loss edge is re-armed so its prompt is not stranded.

    The FALLBACK — the farm's stop did not end the job (the printer REFUSED it, a person's command
    took it over, or it went unanswered past :data:`STOP_SELF_HEAL`, production's figure; a stop is
    TAKEN when the job is over, however late — a terminal after the fallback is still credited to
    the farm's stop — and one that never left the farm is sent again on the next session): the row
    turns ESCALATED, a person's for good
    (the table reads
    it ``human`` and is never re-entered), and one page asks them to Ignore and resume or stop
    the print. Neither hold is the first answer: on 2026-09-24 the 09-04 lane was replaced by
    an unbounded paused hold, calling the operator's own request "a previous change", and
    every trip then waited for a human, 1 min to 8 h, with the bed down and the heaters at
    print temperature — the decision hold is bounded by its timer for exactly that reason.

Restart durability (F7)
    Durable: every incident row, and a plate-check episode's step ledger
    (``printer_incident_step`` through :class:`_PlateCheckEvidence`, the store's
    ``EvidenceLog``) — which buttons and stops the driver sent, because the wire cannot
    restate it (the PAUSE reads the same before and after the press). A restart re-enters
    an open ``recovering`` episode while its job is still PAUSEd and derives the owed rung
    from the ledger alone (:func:`_owed_rung`): no press on it → press; a press on it → the
    decision, NEVER a second resume; a stop on it unanswered → that stop again, never a
    second decision. An ``escalated`` episode is a person's: the sampler re-enters it only for
    a decision-hold stop a crash left unanswered, and the hold's end is its timer's.

    The episode driver's LIVENESS is the store's (``printer_incidents.register_driver`` /
    ``driver_live``), and for this lane it is a GATE: the sampler registers the driver at
    its spawn, before its row exists, and spawns nothing while one is live — the row itself
    cannot refuse a second driver once it exists (re-entry adopts it). A restart empties the
    registry by killing the task, which is the truth. Four more things live in process
    memory, and each names how the wire or the row re-answers it:

    * ``_in_flight`` — the POWER-LOSS driver's handle per printer, and only its: a standing
      prompt spawns one recovery rather than one per push, and that driver lives until the
      record answers its resume (across sessions) or the resume's self-heal bound passes. A restart drops it; the session's first
      fresh report re-derives "the prompt is standing" for free and spawns again. "Already
      decided" is the incident row and the record's answer, which survive, so the cost of a
      restart is at most ONE idempotent resume.
    * ``_seen`` — the per-printer wire sample (``connection_epoch`` + the measured
      outage). A restart re-seeds without measuring, exactly like
      ``spool_recovery._wire_sample`` and ``hms_edges``: an outage that ended before we
      looked is not one we witnessed, and the measured duration is then simply unknown
      (the notification drops the sentence rather than inventing one). The lost-Z arm is
      NOT this sample's edge: it runs on the session's first fresh report
      (:func:`note_session_start`).
      A plate-check hand-over clears its ``at_prompt``, so a power-loss prompt that stood
      behind the episode (its driver stood aside on the job pause) edges again.
    * ``_summary`` — the fleet accumulator for ONE summary page per outage. A restart
      mid-window loses the counts, which are the whole point of the page; that is
      accepted (F9) and paid for by the log line every window close emits whether or
      not the page fires, so a suppressed page and a dead lane are never confused.
    * ``_deadline_timers`` — a decision hold's deadline TIMER per printer (operator ruling
      2026-10-05). A restart kills it; :func:`rearm_decision_deadlines` (the printer's session
      start) re-arms its standing hold, and the hourly reminder pass re-arms a past-due hold that has none
      (:func:`rearm_overdue_decision_deadlines`), both from the row's ``escalated_at`` + the
      setting — so a window that ran out during the downtime is stopped at once.

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
    PrinterIncident,
)
from backend.app.models.printer_incident_step import STEP_KIND_DIALOG, STEP_KIND_STOP, StepKind
from backend.app.services import incident_resolution, live_reading, printer_action, printer_incidents
from backend.app.services.command_outcome import POWER_LOSS_RESUME_FAILED, Answer
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
from backend.app.services.incident_resolution import ClearedEvent, Context
from backend.app.services.job_identity import is_held_job
from backend.app.services.plate_occupancy import (
    ACTIVE_PRINT_STATES,
    DEPOSIT_TOKENS,
    DepositEvidence,
    plate_occupancy,
)
from backend.app.services.printer_action import STOP_SELF_HEAL
from backend.app.services.printer_manager import printer_manager
from backend.app.services.terminal_outcome import (
    PLATE_CHECK_DEADLINE_STOP,
    PLATE_CHECK_LEGACY_STOP,
    PLATE_CHECK_RETRY_STOP,
    PLATE_CHECK_STOP_NAMES,
    PLATE_CHECK_STOP_NOT_TAKEN,
    PlateCheckFacts,
    PlateCheckStopNotTaken,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.app.services.bambu_mqtt import BambuMQTTClient

logger = logging.getLogger(__name__)


# --- latency budget (rows in the AMS skill's table; every number carries its why) ---
#
# Events decide first. The power-loss resume, the ladder's press and its stop are ACTIONS answered by the
# record (``printer_action``) on an event — the ACK, the state the printer reports, the job's terminal, a
# reboot, a person's act — however late; a frame that never left the farm is sent again on the printer's
# next session. Timers are never the only signal (2026-10-11), and self-heal stays self-heal (operator
# ruling 2026-10-12: "The behaviour should stay the same for the most part, im not asking to change
# behaviour, i was asking to not rely on timers only as it's finnikey. so if it was self heal, it should
# stay self heal."): each wait production bounded keeps its bound as a NAMED fallback
# (``printer_action.SelfHealBound``, the witness beside its number), counted in the printer's ONLINE
# seconds by the record (``bound_due`` / ``await_outcome(bound=)``); when it passes with no event the
# ladder takes production's next step, and the action stays the record's, so a late answer still lands.
# What else remains is page accumulation, the lost-Z outage discriminator (a named exception: no
# per-printer reboot tell exists on the H2S wire), the decision hold's window (operator ruling
# 2026-10-05) and the cadence a driver re-reads the wire at.

# Rung 1's re-check: a job still PAUSEd on its job this long after the press — the press accepted or
# not, the dialog standing or cleared — has FAILED its re-check, and rung 2 decides (production's
# verdict). Its witness: production judged it failed at 30 s, and a real pass took 45 s.
_RECHECK_SELF_HEAL = printer_action.SelfHealBound(
    "_RECHECK_SELF_HEAL",
    120.0,
    "014-H2S episode 475 (2026-10-05): the re-check passed 45 s after the press; production judged failure "
    "at 30 s; 120 s equals the stop bound",
)
# Rung 1's first layer: a job RUNNING after the press that has not printed layer 1 this long after it was
# first seen RUNNING is left to the row's closers (the sweep closes a ``job_pause`` row on RUNNING; a plate
# dialog that comes back re-enters at rung 2) — a driver that watched a whole print would hold its row
# open to the terminal, which would read that print's failure as a refused plate.
_RECHECK_FIRST_LAYER_SELF_HEAL = printer_action.SelfHealBound(
    "_RECHECK_FIRST_LAYER_SELF_HEAL",
    900.0,
    "production's _RECHECK_FIRST_LAYER_S (337f4954): the start block a resumed job still runs (re-home, "
    "levelling, purge) takes minutes; unmeasured in production",
)
# The farm's stop: unanswered past the record's stop bound (:data:`STOP_SELF_HEAL`, ``printer_action``'s —
# the one origin of the measured figure), the FALLBACK — the row escalates, the person's turn is on. The
# stop's action stays outstanding, so a terminal after it is still the farm's stop.

# The power-loss resume: accepted (or unacknowledged) and the job not RUNNING this long after the send, the
# resume FAILED — production's verdict, ``_hold_power_loss(reason="resume_failed")``: the hold, its page and its
# chip. The action stays the record's, so a RUNNING after the bound still answers it, and the hold's own
# closer (the RUNNING edge, ``incident_resolution``'s wire class) ends the hold. The fleet's printer 8 resumed
# one second after its resume on 2026-09-04; no figure of the firmware's slowest resume was ever measured.
_POWER_LOSS_SELF_HEAL = printer_action.SelfHealBound(
    "_POWER_LOSS_SELF_HEAL",
    30.0,
    "production's _POWER_LOSS_CONFIRM_S (337f4954; the same figure as its _RUNOUT_RESUME_CONFIRM_S); printer 8 "
    "reached RUNNING 1 s after its resume on 2026-09-04; the slowest resume was never measured",
)


def rehydrated_power_loss_resume_bound(
    action: printer_action.OutstandingAction,
) -> tuple[printer_action.SelfHealBound, datetime] | None:
    """The record's bound factory for a REHYDRATED power-loss resume (``printer_action.register_bound_factory``,
    registered by ``printer_event_wiring`` under the command ``power_loss_resume``): a farm resume still out
    when the farm restarted gets :data:`_POWER_LOSS_SELF_HEAL` back, anchored at its send (the record counts
    online seconds from the printer's first fresh report in the new process).

    Its lane died with the old process, so nothing acts on the bound's ``SelfHealDue`` — nothing needs to:
    if the prompt still stands on the first fresh report, this lane's sampler re-spawns the driver, whose NEW
    resume holds the printer on its own bound and opens the hold itself (``_resume_after_power_loss``). What
    the bound does is mark the orphaned resume as its lane's, so the attention owner (``farm_stall``) skips
    it rather than paging the printer a second time beside the re-spawned driver. Its late answer still
    lands on the record. A person's power-loss resume carries none."""
    if action.actor != printer_action.ACTOR_FARM:
        return None
    return _POWER_LOSS_SELF_HEAL, action.sent_at


# The fleet summary's accumulation window, from the FIRST decision — PAGE accumulation, never an
# outcome. Comfortably longer than the 26 s the 2026-09-04 disconnect burst spanned, so one outage
# produces one page.
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
# says where the print is and the two exits a person has (operator ruling 2026-10-05: the
# farm offers Ignore and resume, and Stop). Sys-admin register: no exclamation, no apology,
# the actions named.
_VISION_PAUSED_INSTRUCTION = "Print paused at the plate check. Ignore and resume, or stop the print."

# --- the plate-check ladder's cadence ----------------------------------------------------------

# The step at which a waiting driver re-reads the record and the wire: the status push cadence (~1 Hz) —
# a faster read sees the same push again. A cadence, never a decision: every wait here ends on an event.
_EPISODE_POLL_S = 1.0
# The decision hold's window (operator ruling 2026-10-05): how long a print the ladder would
# escalate waits PAUSED at the plate dialog for a person before the farm stops it. A Farm setting
# (1-120 min), read through ``farm_stall._grace_seconds`` (:func:`_decision_window_s`).
_DECISION_WINDOW_KEY = "farm_plate_check_decision_minutes"
_DECISION_WINDOW_DEFAULT_MIN = 10
# The deadline timer's re-read step: it notices a hold a person ended (their resume, their stop)
# within this, and once the window has run out it re-asks the table this often until the stop
# is owed (a printer that disconnected at the deadline is stopped when it comes back paused).
# DB-free reads only; UNMEASURED, a few status pushes.
_DEADLINE_POLL_S = 5.0
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
    # Whether the LAST push of THIS session showed the power-loss prompt standing on a
    # FRESH report. The driver spawns on the RISING edge of this only: a prompt that
    # stands for hours (a hold the firmware refused to release, a foreign print nobody
    # resumes) would otherwise spawn a fresh driver on every push once the previous one
    # exited, each one re-reading the incident, re-logging "standing aside" and
    # re-recording the printer into the summary. The edge is per SESSION: the first fresh
    # report of a new session showing the prompt is the event the farm answers it on
    # (the reconnect's re-broadcast of the last session's cache is not fresh, and reads
    # False). The first push after a restart has no previous sample, so it IS a rising
    # edge: that is the restart-durability story (one re-derived decision, then the row
    # decides).
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
# printer_id -> (incident_id, the decision hold's deadline timer) — one per printer.
_deadline_timers: dict[int, tuple[int, asyncio.Task]] = {}


def _reset_state() -> None:
    """Test hook: drop every piece of process memory between cases."""
    global _summary, _summary_task
    for task in list(_in_flight.values()):
        task.cancel()
    _in_flight.clear()
    for _incident_id, timer in list(_deadline_timers.values()):
        timer.cancel()
    _deadline_timers.clear()
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

    * spawns ONE power-loss recovery driver on the session's first fresh report showing the
      prompt (:func:`hms_errors.power_loss_hold_active` = live ``PAUSE`` and ``0300_8007``
      standing), deduped by :data:`_in_flight` so a prompt that stands for minutes still
      gets a single driver;
    * spawns the plate-check episode driver when the ladder's table owes an act — a TRIP or a
      RE-ENTRY (:func:`_sample_plate_check`), one per printer through the store's liveness gate;
    * measures the outage a RECONNECT ended (``connection_epoch`` advanced) for the
      power-loss page's sentence (:func:`_outage_minutes`).

    The lost-Z-reference arm is not here: it decides on the session's first FRESH report
    (:func:`note_session_start`), never on the reconnect's first push — which re-broadcasts
    the PREVIOUS session's cached state.

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
        # The prompt on a FRESH report of this session (``live_reading.is_fresh``): the event the farm
        # answers it on. A new session's prompt is a new edge (``prev`` of another session reads False).
        at_prompt = power_loss_hold_active(state) and live_reading.is_fresh(state)
        was_at_prompt = prev.at_prompt if prev is not None and prev.epoch == epoch else False
        _seen[printer_id] = _WireSample(epoch=epoch, outage_s=outage_s, at_prompt=at_prompt)

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
    printer is still holding the job we saw". Spawned on the session's first FRESH report
    showing the prompt — the event; no settle window precedes it (2026-10-11): a session the
    post-boot second reconnect ends is answered by the record (an unacknowledged resume is
    VOID at the next session and sent again there).

    Branches, IN ORDER, one log line each — the order is the contract:

    (a) STAND DOWN — disconnected / not a fresh report / no longer PAUSE / the prompt is
        gone / the job changed. The operator beat us, or this is not the state we decided on.
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
        (its "Problem solved, resume", then a stop or the decision hold; a person's in its
        holds). If the
        episode HANDS OVER — the pause is not the plate check's — it re-arms this lane's
        rising edge, so the prompt is answered on the next push.
    (d) RESUME — farm unit or FOREIGN print alike (operator ruling: parity). Success is
        a LOG LINE and a summary entry, with no durable record; a REFUSAL, or a resume not
        carried out within :data:`_POWER_LOSS_SELF_HEAL`, is a HOLD.
    """
    try:
        state = printer_manager.get_status(printer_id)
        stand_down = _stand_down_reason(state, observed_subtask)
        if stand_down is not None:
            logger.info(
                "[pause-recovery] printer %s power-loss recovery stood down — %s",
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
        # was already refused and a human owns the prompt. A lost-Z row is not
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
    if not live_reading.is_fresh(state):
        return "the report is not fresh (the session's first fresh report answers the prompt)"
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
    """Branch (d): answer the prompt with the farm's resume — an ACTION read through the record
    (``printer_action``), never a confirm window (2026-10-11):

    * REACHED — the printer reports RUNNING on the job, however late: resumed;
    * REFUSED — the printer's fail ACK, or its own "Resume failed after power loss" (``0300_400D``,
      ``command_outcome.POWER_LOSS_RESUME_FAILED`` — the catalog's verdict on the command; a second
      resume cannot un-fail it): the HOLD, with the printer's words;
    * TAKEN_OVER — a person's command, or another job on the printer: stand down;
    * VOID — a later session began with the resume unacknowledged (the printer may never have
      received it), or the job ended: the level is re-read on that session, and while the prompt
      still stands on the same job the resume is sent again.

    A frame that never left the farm (the session mid-churn — the post-boot second reconnect) is
    sent again on the printer's next session. The events above decide first; production's self-heal
    stays the fallback behind them (operator ruling 2026-10-12): a resume no event answered within
    :data:`_POWER_LOSS_SELF_HEAL` of the printer's online time FAILED — the hold, ``resume_failed``.
    The action stays outstanding, so a RUNNING after the bound still answers it, and the RUNNING edge
    closes the hold (``incident_resolution``'s wire class).
    """
    while True:
        sent = await printer_action.send(
            printer_id, printer_action.power_loss_resume(), actor=printer_action.ACTOR_FARM
        )
        if sent.published:
            answer = await printer_action.await_outcome(sent, bound=_POWER_LOSS_SELF_HEAL)
            if isinstance(answer, printer_action.Unanswered):
                logger.warning(
                    "[pause-recovery] printer %s power-loss resume (action %s, %s) not RUNNING after %.0fs online "
                    "— resume failed, holding",
                    printer_id,
                    sent.action_id,
                    answer.reading or "no reading",
                    _POWER_LOSS_SELF_HEAL.seconds,
                )
                await _hold_power_loss(printer_id, observed_subtask, reason="resume_failed")
                return
            reason = await printer_action.reason_of(sent.action_id)
            if answer is Answer.REACHED:
                outage = _outage_minutes(printer_id)
                logger.info(
                    "[pause-recovery] printer %s RESUMED after power loss (action %s, outage %s, job %s, origin %s)",
                    printer_id,
                    sent.action_id,
                    f"~{outage} min" if outage is not None else "unknown",
                    observed_subtask or "-",
                    await _origin_label(printer_id, observed_subtask),
                )
                # Success is deliberately NOT a notification (the 2026-08-10 notification diet):
                # nothing is asked of anyone. The fleet summary names the count.
                _record(printer_id, "resumed")
                return
            if answer is Answer.REFUSED:
                logger.warning(
                    "[pause-recovery] printer %s refused the power-loss resume (action %s%s) — no retry",
                    printer_id,
                    sent.action_id,
                    f": {reason}" if reason else "",
                )
                await _hold_power_loss(
                    printer_id,
                    observed_subtask,
                    reason="resume_failed" if reason == POWER_LOSS_RESUME_FAILED else "resume_refused",
                )
                return
            if answer is Answer.TAKEN_OVER:
                logger.info(
                    "[pause-recovery] printer %s power-loss resume (action %s) taken over — %s; standing down",
                    printer_id,
                    sent.action_id,
                    reason or "a person acted",
                )
                return
            logger.info(
                "[pause-recovery] printer %s power-loss resume (action %s) was void (%s) — the prompt is re-read "
                "on this session",
                printer_id,
                sent.action_id,
                reason or "no reason",
            )
        else:
            logger.info(
                "[pause-recovery] printer %s power-loss resume (action %s) did not go out (%s) — sent again on "
                "the printer's next session",
                printer_id,
                sent.action_id,
                sent.refusal,
            )
            await printer_action.await_next_session(printer_id, sent.entry.connection_epoch, recheck_s=_EPISODE_POLL_S)
        stand_down = _stand_down_reason(printer_manager.get_status(printer_id), observed_subtask)
        if stand_down is not None:
            logger.info("[pause-recovery] printer %s power-loss resume stood down — %s", printer_id, stand_down)
            return


# The two ways the printer refuses a resume, as whole sentences: its fail ACK, and its own "Resume
# failed after power loss" (``0300_400D``). They read differently to an operator and neither of them is
# "automatic resume is off" — there is no such toggle (operator ruling 2026-09-04).
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


def note_session_start(printer_id: int, connection_epoch: int) -> None:
    """SESSION START: the lost-Z-reference arm, on the printer's first FRESH report of the session
    ``connection_epoch`` (``session_start``, ``printer_events.FirstFreshReport``). Sync, never raises.

    Decided here and not on the reconnect's first push: ``_on_connect`` re-broadcasts the PREVIOUS
    session's cached state before the pushall answers, so a decision taken there read a printer
    that was not reporting yet. Only a session that FOLLOWS another on this client can end an
    outage this process saw begin (``connection_epoch`` > 1): the first session after a restart has
    no disconnect anchor of its own to set against a burst — a restart re-seeds without edging.

    Ahead of maintenance mode, deliberately. The lost-Z row is a REFUSAL RECORD, not a recovery
    act: it opens no driver, resumes nothing, stops nothing and moves nothing — all it does is make
    every LATER eject refuse until a human clears it. So it sits outside the "the farm stands aside
    while a human owns the machine" ruling, which is about the farm ACTING. An outage that lands
    during a hold destroys the Z datum exactly as one outside a hold does, and skipping the record
    would leave the printer with a trustworthy-looking Z frame that is fiction — the 2026-09-04
    bed-past-the-floor mechanism, arrived at by omission.
    """
    try:
        if connection_epoch <= 1:
            return
        state = printer_manager.get_status(printer_id)
        _maybe_arm_z_reference_hold(printer_id, getattr(state, "disconnected_at", None))
    except Exception:  # noqa: BLE001 — the session start must re-evaluate the printer's other lanes
        logger.exception("[pause-recovery] session-start lost-Z arm failed for printer %s", printer_id)


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

    Reads the session's first FRESH report (the arm runs from ``note_session_start``), never after a
    settle window (2026-10-11). Printers 1 and 3 reconnected a SECOND time ~60 s after the first on
    2026-09-04: that second session is another first fresh report and re-runs the arm, and the hold
    opens at most once (one open ``z_reference_lost`` row per printer).
    """
    try:
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
# The plate-check ladder (operator rulings 2026-09-29 and 2026-10-05): press the printer's own
# "Problem solved, resume"; if that re-check fails, decide ONCE — stop the print and retry it, or,
# when the stop would be an escalation, leave the print PAUSED at the dialog for a person to decide
# (Ignore and resume / Problem solved / Stop) and stop it when the decision window runs out. The
# episode's durable memory is the incident's step ledger (``printer_incidents.EvidenceLog``, its one
# writer), because the wire cannot restate it: the PAUSE reads the same before and after a press,
# and after a restart a second press is exactly what re-entry must never send. Two kinds are
# written, each with its own closed name and answer vocabulary below.

# The dialog buttons on the ledger, named by the action ``execute_hms_action`` takes — one spelling
# for the press and its ledger row. The LADDER presses only "Problem solved, resume": "Ignore" would
# skip the re-check the ladder exists to run. A HUMAN, on their turn, presses either
# (``printer_incidents.PLATE_CHECK_HUMAN_ACTIONS``, the human's-turn predicate's own ``actions``).
_LADDER_BUTTONS: frozenset[str] = frozenset({HMSAction.PROBLEM_SOLVED_RESUME})
_HUMAN_BUTTONS: frozenset[str] = frozenset(printer_incidents.PLATE_CHECK_HUMAN_ACTIONS)

# What the READ of a dialog press answered — the record's answer to it (``printer_action``). Closed.
#   ``success`` / ``fail`` — the firmware's ACK of the press (REACHED / REFUSED);
#   ``void``               — the record voided it: a reboot, or a later session began with it
#                            unacknowledged — the printer may never have received it;
#   ``not_sent``           — nothing went out (the record answered the press ``refused`` at the publish);
#   ``bound_passed``       — the job was still PAUSEd on its job when :data:`_RECHECK_SELF_HEAL` passed and
#                            the printer had not answered the press (or the record voided it): the
#                            re-check FAILED (production's verdict) and the press is SPENT — rung 2 is
#                            owed, after a restart too, never a second press. The action stays the
#                            record's; a late answer is the record's alone and changes nothing here;
#   ``no_ack``             — written before 2026-10-11, when a 2.5 s window read the ACK: read, never written.
# A ``void`` or ``not_sent`` press is not one the printer answered: the ladder presses again while the
# table owes the press (:meth:`_PlateCheckEvidence.pressed`).
PlateCheckDialogAnswer = Literal["success", "fail", "void", "not_sent", "bound_passed", "no_ack"]
_PRESS_BOUND_PASSED: PlateCheckDialogAnswer = "bound_passed"
# What the READ of the farm's stop answered. Closed.
#   ``taken``       — the job is over: the printer reported it holds no job, or the job's terminal
#                     arrived, however late (the record's REACHED);
#   ``not_taken`` / ``not_sent`` — :data:`terminal_outcome.PlateCheckStopNotTaken`: the stop did NOT end
#   the job (refused by the printer, taken over by a person's command, voided or never sent and no
#   longer owed). A refused or taken-over stop hands the paused print to a human (the FALLBACK), so a
#   later end of that job is the human's — never the farm's stop (``PlateCheckFacts.farm_stopped``);
#   ``unconfirmed`` — no event answered the stop before :data:`STOP_SELF_HEAL` passed: the FALLBACK
#                     escalates (production's verdict), but the stop's ACTION stays outstanding, so a
#                     terminal that lands later is still credited to the farm's stop — the terminal
#                     reads the action, never this token (``PlateCheckFacts.stop_action``).
PlateCheckStopUnconfirmed = Literal["unconfirmed"]
_STOP_UNCONFIRMED: PlateCheckStopUnconfirmed = "unconfirmed"
PlateCheckStopAnswer = Literal["taken", PlateCheckStopNotTaken, PlateCheckStopUnconfirmed]
# What the FALLBACK is handed (:func:`_escalate`): why the farm's stop did not end the job by itself.
FallbackAnswer = Literal[PlateCheckStopNotTaken, PlateCheckStopUnconfirmed]

# The record's answer to a dialog press, as the ledger writes it. A person's command never takes a
# dialog press over (the dialog family reads no person's act); TAKEN_OVER reads as answered, and the
# wire decides what follows.
_DIALOG_ANSWER: dict[Answer, PlateCheckDialogAnswer] = {
    Answer.REACHED: "success",
    Answer.REFUSED: "fail",
    Answer.VOID: "void",
    Answer.TAKEN_OVER: "success",
}
# The press answers that are NOT a press the printer answered: pressed again while the table owes it.
_UNANSWERED_PRESSES: frozenset[str] = frozenset({"void", "not_sent"})

# The two ledger kinds this lane writes (``StepKind``'s own values), each with its names...
_PLATE_CHECK_NAMES: dict[str, frozenset[str]] = {
    STEP_KIND_DIALOG: _LADDER_BUTTONS | _HUMAN_BUTTONS,
    STEP_KIND_STOP: PLATE_CHECK_STOP_NAMES,
}
# ...and its answers.
_PLATE_CHECK_ANSWERS: dict[str, frozenset[str]] = {
    STEP_KIND_DIALOG: frozenset(get_args(PlateCheckDialogAnswer)),
    STEP_KIND_STOP: frozenset(get_args(PlateCheckStopAnswer)),
}


def _is_plate_check_kind(token: str) -> TypeGuard[StepKind]:
    return token in _PLATE_CHECK_NAMES


def _is_fallback_answer(token: str | None) -> TypeGuard[FallbackAnswer]:
    return token in get_args(FallbackAnswer)


@dataclass(frozen=True)
class PlateCheckStep:
    """One thing sent on a plate-check episode's dialog or job: a button pressed (by the ladder or,
    on their turn, by a human through the farm), or a farm stop.

    ``outcome`` is ``None`` from the send until the read (a crash between the two leaves it
    ``None`` for good — the press or the stop went out and nobody saw what it did, which is
    exactly the fact re-entry needs). Its token is the kind's own closed vocabulary
    (:data:`PlateCheckDialogAnswer` / :data:`PlateCheckStopAnswer`). ``deposit`` is the plate a
    LADDER press was sent over (``plate_occupancy.DepositEvidence.ledger_token``), recorded so the
    ladder's one decision reads the trip's own measurement after a restart (:meth:`_PlateCheckEvidence
    .trip_deposit`); ``None`` on every other step.
    """

    seq: int
    kind: StepKind
    name: str
    outcome: str | None
    at: datetime | None
    action_id: int | None = None
    deposit: str | None = None

    @classmethod
    def dialog(
        cls,
        button: str = HMSAction.PROBLEM_SOLVED_RESUME,
        *,
        action_id: int | None = None,
        deposit: str | None = None,
    ) -> PlateCheckStep:
        """The LADDER's unsent press of ``button`` (its recorded ``printer_action``) over the plate it read
        (``deposit``, a ``plate_occupancy.DEPOSIT_TOKENS`` token or ``None``); the log's ``note`` assigns its
        ``seq`` and ``at``. Only "Problem solved, resume" — any other button, or a deposit token outside the
        vocabulary, RAISES."""
        if button not in _LADDER_BUTTONS:
            raise LookupError(f"pause_recovery: the plate-check ladder presses no {button!r}")
        if deposit is not None and deposit not in DEPOSIT_TOKENS:
            raise LookupError(f"pause_recovery: a plate-check press records no deposit {deposit!r}")
        return cls(
            seq=0,
            kind=STEP_KIND_DIALOG,
            name=str(button),
            outcome=None,
            at=None,
            action_id=action_id,
            deposit=deposit,
        )

    @classmethod
    def human(cls, action: str, *, action_id: int | None = None) -> PlateCheckStep:
        """A HUMAN's unsent press on their turn (:func:`human_dialog_action`) — a button outside
        ``printer_incidents.PLATE_CHECK_HUMAN_ACTIONS`` RAISES."""
        if action not in _HUMAN_BUTTONS:
            raise LookupError(f"pause_recovery: a human's plate-check press is never {action!r}")
        return cls(seq=0, kind=STEP_KIND_DIALOG, name=str(action), outcome=None, at=None, action_id=action_id)

    @classmethod
    def stop(cls, name: str, *, action_id: int | None = None) -> PlateCheckStep:
        """An unsent farm stop NAMED by the decision it carries (:data:`PLATE_CHECK_RETRY_STOP` /
        :data:`PLATE_CHECK_DEADLINE_STOP`) — the decision's ONE writer (AST-pinned,
        ``TestPlateCheckDialogOwnership``). Any other name RAISES."""
        if name not in PLATE_CHECK_STOP_NAMES:
            raise LookupError(f"pause_recovery: the plate-check ladder sends no stop named {name!r}")
        return cls(seq=0, kind=STEP_KIND_STOP, name=name, outcome=None, at=None, action_id=action_id)

    def entry(self) -> printer_incidents.StepEntry:
        """The ledger row this step is written as: its kind, name, action and the plate a ladder press was
        sent over — no tray, no feeder."""
        return printer_incidents.StepEntry(
            kind=self.kind, name=self.name, action_id=self.action_id, deposit=self.deposit
        )

    def sent(self, seq: int, at: datetime | None) -> PlateCheckStep:
        return replace(self, seq=seq, at=at)

    def answered(self, outcome: str) -> PlateCheckStep:
        """The read's answer. A token outside THIS kind's vocabulary RAISES."""
        if outcome not in _PLATE_CHECK_ANSWERS[self.kind]:
            raise LookupError(f"pause_recovery: plate-check {self.kind} step {self.seq} answered with {outcome!r}")
        return replace(self, outcome=outcome)


class _PlateCheckEvidence(printer_incidents.EvidenceLog[PlateCheckStep]):
    """THE evidence log of one plate-check episode: every press and stop sent on it, in send
    order, with what the wire answered.

    The store's :class:`~backend.app.services.printer_incidents.EvidenceLog` (noted at the send,
    answered at the read, built only by ``from_row``, the ledger's one writer) bound to this
    lane's vocabulary, :class:`PlateCheckStep`. Three readings hang off it: :meth:`pressed` and
    :meth:`last_stop`, the ledger half of :func:`_owed_rung`'s key (a re-entered driver never
    presses again; the decision the ladder recorded and what its stop answered — also the
    terminal's reading, ``plate_check_facts``), and :meth:`trip_deposit` (the plate the trip read,
    for the one decision).
    """

    def pressed(self) -> bool:
        """Has a "Problem solved, resume" (or a human's press) gone out on this episode that the printer
        may have answered? A press the record voided or one that never went out
        (:data:`_UNANSWERED_PRESSES`) is not one: the ladder presses again. An UNANSWERED press (a crash
        between the send and the read) counts — nobody saw what it did, so it is never sent twice."""
        return any(step.kind == STEP_KIND_DIALOG and step.outcome not in _UNANSWERED_PRESSES for step in self.steps)

    def last_stop(self) -> tuple[str, str | None] | None:
        """The LAST farm stop as ``(name, outcome)`` — the outcome ``None`` while unanswered — or
        ``None`` when the farm sent none. The last one is the one that speaks: a stop re-sent after
        a crash, or on the next session, supersedes the one before it, and nothing is sent after an
        answered stop."""
        stops = [step for step in self.steps if step.kind == STEP_KIND_STOP]
        return (stops[-1].name, stops[-1].outcome) if stops else None

    def last_stop_action(self) -> int | None:
        """The ``printer_action`` the LAST farm stop was sent as — what the terminal credits the stop by
        (``PlateCheckFacts.stop_action``) — or ``None`` with no stop, or a stop no action records (a
        ledger written before the record)."""
        stops = [step for step in self.steps if step.kind == STEP_KIND_STOP]
        return stops[-1].action_id if stops else None

    def trip_deposit(self) -> str | None:
        """The plate the episode's FIRST ladder press recorded (``plate_occupancy.DEPOSIT_TOKENS``), or
        ``None`` when no press recorded one (a ledger written before 2026-10-11, or a press with no client
        to read the job from). Read by the one decision (:func:`_decide`) when the live job cannot vouch
        for its peaks."""
        return next(
            (step.deposit for step in self.steps if step.kind == STEP_KIND_DIALOG and step.deposit is not None),
            None,
        )

    @classmethod
    def _step_of(cls, row: printer_incidents.LedgerRow) -> PlateCheckStep:
        """Hydrate ONE ledger row. A token this lane's vocabulary cannot name RAISES — a ledger
        that says something the driver cannot read is drift, never a skipped row. A
        pre-2026-10-05 ``stop`` reads as the retry rung's stop (``terminal_outcome.PLATE_CHECK_LEGACY_STOP``)."""
        if not _is_plate_check_kind(row.kind):
            raise LookupError(f"pause_recovery: incident {row.incident_id} step {row.seq} has kind {row.kind!r}")
        name = (
            PLATE_CHECK_RETRY_STOP if row.kind == STEP_KIND_STOP and row.name == PLATE_CHECK_LEGACY_STOP else row.name
        )
        if name not in _PLATE_CHECK_NAMES[row.kind]:
            raise LookupError(
                f"pause_recovery: incident {row.incident_id} step {row.seq} names no plate-check {row.kind}: "
                f"{row.name!r}"
            )
        if row.outcome is not None and row.outcome not in _PLATE_CHECK_ANSWERS[row.kind]:
            raise LookupError(f"pause_recovery: incident {row.incident_id} step {row.seq} answers {row.outcome!r}")
        return PlateCheckStep(
            seq=row.seq,
            kind=row.kind,
            name=name,
            outcome=row.outcome,
            at=row.sent_at,
            action_id=row.action_id,
            deposit=row.deposit if row.deposit in DEPOSIT_TOKENS else None,
        )


async def plate_check_facts(
    printer_id: int, open_incidents: Sequence[Mapping[str, object]], job_id: str | None
) -> PlateCheckFacts | None:
    """What the terminal of ``job_id`` owes to its plate-check episode — or ``None`` when that
    job had none. Never raises.

    ``open_incidents`` is the printer's open-hold projection ``main.on_print_complete`` took
    ONCE, ahead of every closer (the episode's row is one of the holds this terminal closes).
    The episode is the ``plate_vision`` row that paused THIS job (``job_identity.is_held_job``);
    another job's row explains nothing about this terminal.

    * ``last_stop`` — the episode's last farm stop as the ladder RECORDED it
      (:meth:`_PlateCheckEvidence.last_stop`): its name is the retry-or-escalate decision made
      once at the second rung (operator ruling 2026-10-05), its outcome what the read answered.
      The terminal reads the decision back (``terminal_outcome.farm_retries``) and never
      re-decides it.
    * ``item_id`` — the farm unit the episode's ROW bound at its trip.
    * ``stop_action`` — what the record holds for that last stop's ACTION
      (``printer_action.answer_of``: ``outstanding`` while no event answered it, else its answer),
      which is what credits the stop to the farm (``PlateCheckFacts.farm_stopped``): a terminal that
      lands after the stop's self-heal bound passed is still the farm's stop, and a person's own Stop
      after the fallback took it over. ``None`` for a stop no action records.

    Its own session, through ``run_with_retry``. Fails CLOSED: an unreadable ledger returns
    facts with no stop and no unit (they escalate) and logs a WARNING — a retry nobody could
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

        async def _read(db) -> tuple[PlateCheckFacts, int | None]:
            row = await db.get(PrinterIncident, incident_id)
            log = await _PlateCheckEvidence.from_row(db, incident_id)
            facts = PlateCheckFacts(last_stop=log.last_stop(), item_id=row.item_id if row is not None else None)
            return facts, log.last_stop_action()

        facts, stop_action_id = await run_with_retry(_read, label="plate-check facts")
        if stop_action_id is not None:
            answer = await printer_action.answer_of(stop_action_id)
            facts = replace(facts, stop_action="outstanding" if answer is None else answer.value)
    except Exception:  # noqa: BLE001 — the terminal must go on; an unread episode escalates
        logger.warning(
            "[pause-recovery] printer %s plate-check facts for job %s unreadable — the terminal escalates",
            printer_id,
            job_id or "-",
            exc_info=True,
        )
        return PlateCheckFacts(last_stop=None, item_id=None)
    logger.info(
        "[pause-recovery] printer %s plate-check episode %s at the terminal of job %s: last stop %s%s, %s",
        printer_id,
        incident_id,
        job_id or "-",
        "none" if facts.last_stop is None else f"{facts.last_stop[0]}={facts.last_stop[1] or 'unanswered'}",
        f" (action {facts.stop_action})" if facts.stop_action is not None else "",
        f"unit {facts.item_id}" if facts.item_id is not None else "no unit",
    )
    return facts


# --- the plate-check ladder as ONE transition table (operator rulings 2026-09-29 / 2026-10-05) --
#
# What the farm owes a plate-check episode is a function of three readings and nothing else: the
# ROW (none / ``recovering`` — the ladder acting / ``escalated`` — a person's), its LEDGER (what was
# pressed and stopped, and what those answered; for a decision hold, whether its window ran out),
# and the LIVE wire. :data:`_OWED` spells out every reachable cell and a missing key RAISES — the
# ``incident_resolution._TABLE`` lesson: no ``else`` for a new case to inherit an answer from. The
# sampler asks it before spawning a driver, the driver before every send, the deadline timer
# before it spawns the decision hold's stop.

_RowReading = Literal["none", "recovering", "escalated"]
# ``none``: no row. A ``recovering`` row: ``fresh`` (nothing sent), ``pressed`` (a press, no stop).
# An ``escalated`` row with no stop: ``hold`` (the decision window still open) / ``due`` (it ran
# out). Any row with a stop: ``retry_out`` / ``deadline_out`` (that stop unanswered — a crash after
# the send), ``taken`` (it ended the job), ``refused`` (it did not: not taken / not sent),
# ``unconfirmed`` (no event answered it before its self-heal bound passed — the FALLBACK's).
_LedgerReading = Literal[
    "none", "fresh", "pressed", "hold", "due", "retry_out", "deadline_out", "taken", "refused", "unconfirmed"
]
# ``plate_pause``: PAUSEd at the plate dialog on the row's job; ``other_pause``: PAUSEd on that job
# for something else (the power-loss prompt, a runout, no dialog); ``not_paused``: that job RUNNING
# or over; ``other_job``: the printer is on another job; ``stale``: no fresh report (a reconnect
# replays the last session's PAUSE); ``held``: maintenance mode (the operator is at the screen).
_LiveReading = Literal["plate_pause", "other_pause", "not_paused", "other_job", "stale", "held"]
# What is owed. The ACTS: ``trip`` (open the episode, then rung 1), ``press`` (rung 1: "Problem
# solved, resume"), ``decide`` (rung 2: retry or hold, decided once), ``retry_stop`` /
# ``deadline_stop`` (send — or re-send — the stop of that name), ``hand_over`` (the pause is not
# the plate check's), ``fallback`` (the farm's stop did not end the job: escalate). The rest are
# NOT the farm's: ``hold`` (the decision window is open — a person's turn), ``human`` (the
# FALLBACK, a person's for good), ``stand`` (the row's closers own what comes next).
Rung = Literal[
    "trip", "press", "decide", "retry_stop", "deadline_stop", "hand_over", "fallback", "hold", "human", "stand"
]
_ACTS: frozenset[str] = frozenset({"trip", "press", "decide", "retry_stop", "deadline_stop", "hand_over", "fallback"})
# The wire readings no rung acts on: every row's closers (the job RUNNING, its terminal, the
# sweep) or a fresh report own what comes next.
_REST_OF_THE_WIRE: tuple[_LiveReading, ...] = ("not_paused", "other_job", "stale", "held")


def _cells(
    status: _RowReading, ledger: _LedgerReading, *, plate_pause: Rung, other_pause: Rung
) -> dict[tuple[_RowReading, _LedgerReading, _LiveReading], Rung]:
    """One (row, ledger) line of the table: its two PAUSE columns, and ``stand`` for the rest of
    the wire — the farm acts on a plate-check episode only while that job is PAUSEd."""
    line: dict[tuple[_RowReading, _LedgerReading, _LiveReading], Rung] = {
        (status, ledger, "plate_pause"): plate_pause,
        (status, ledger, "other_pause"): other_pause,
    }
    line.update({(status, ledger, live): "stand" for live in _REST_OF_THE_WIRE})
    return line


_OWED: dict[tuple[_RowReading, _LedgerReading, _LiveReading], Rung] = {
    # No row, and the printer is paused at its plate dialog: the TRIP.
    **_cells("none", "none", plate_pause="trip", other_pause="stand"),
    # Rung 1, and a pause that is not the plate check's is handed over (unchanged since 09-29).
    **_cells("recovering", "fresh", plate_pause="press", other_pause="hand_over"),
    # Rung 2: the re-check failed — decide ONCE, recorded as the stop's name (ruling 2026-10-05).
    **_cells("recovering", "pressed", plate_pause="decide", other_pause="hand_over"),
    # The retry was decided and its stop is unanswered (a crash after the send, or the send did
    # not go out): it is owed again on any pause of that job — the decision is never re-made.
    **_cells("recovering", "retry_out", plate_pause=PLATE_CHECK_RETRY_STOP, other_pause=PLATE_CHECK_RETRY_STOP),
    **_cells("recovering", "taken", plate_pause="stand", other_pause="stand"),
    # A stop answered as not ending the job, on a row a crash left recovering: the FALLBACK is owed.
    **_cells("recovering", "refused", plate_pause="fallback", other_pause="fallback"),
    # A stop no event answered before its self-heal bound passed, on a row a crash left recovering
    # (between the ledger's answer and the escalation): the FALLBACK is owed (2026-10-12 ruling).
    **_cells("recovering", "unconfirmed", plate_pause="fallback", other_pause="fallback"),
    # The decision hold: the print waits paused for a person. No driver lives (the timer waits).
    **_cells("escalated", "hold", plate_pause="hold", other_pause="hold"),
    # The window ran out: the farm stops the print — only at the plate dialog; a pause the hold
    # does not own stays the person's.
    **_cells("escalated", "due", plate_pause=PLATE_CHECK_DEADLINE_STOP, other_pause="hold"),
    **_cells("escalated", "deadline_out", plate_pause=PLATE_CHECK_DEADLINE_STOP, other_pause=PLATE_CHECK_DEADLINE_STOP),
    # A retry stop the FALLBACK escalated over whose answer never reached the ledger (the write is
    # best-effort; the driver escalates on its in-memory answer): the row is a person's.
    **_cells("escalated", "retry_out", plate_pause="human", other_pause="human"),
    **_cells("escalated", "taken", plate_pause="stand", other_pause="stand"),
    # The FALLBACK: the person's for good, never re-entered; their turn stays on.
    **_cells("escalated", "refused", plate_pause="human", other_pause="human"),
    # The FALLBACK over a stop its self-heal bound passed on: the person's for good too (the stop's
    # action stays outstanding — a late terminal is still the farm's stop, read by the terminal).
    **_cells("escalated", "unconfirmed", plate_pause="human", other_pause="human"),
}


@dataclass(frozen=True)
class _LadderSnapshot:
    """The row and ledger half of :func:`_owed_rung`'s key, as one value.

    Built from the store's DB-free projection by the sampler and the timer
    (:meth:`of_projection`: the projection carries the row's status, job and last stop), and by
    the driver from the hydrated ledger. ``pressed`` is the driver's alone — the projection does
    not carry presses, and the sampler only asks whether ANY act is owed, which a recovering row's
    press and decision both are. ``deadline_due`` is the TIMER's alone: the decision window is a
    timer, never a reading of liveness or the wire.
    """

    printer_id: int
    status: _RowReading
    job: str | None
    pressed: bool
    last_stop: tuple[str, str | None] | None
    deadline_due: bool

    @classmethod
    def of_projection(cls, printer_id: int, row: Mapping[str, object] | None) -> _LadderSnapshot:
        if row is None:
            return cls(
                printer_id=printer_id, status="none", job=None, pressed=False, last_stop=None, deadline_due=False
            )
        last = row.get("last_stop")
        return cls(
            printer_id=printer_id,
            status=str(row.get("status")),  # type: ignore[arg-type] — an open row's status IS a _RowReading
            job=str(row.get("job_id") or ""),
            pressed=False,
            last_stop=(str(last["name"]), last.get("outcome")) if isinstance(last, Mapping) else None,
            deadline_due=False,
        )


def _ledger_reading(snapshot: _LadderSnapshot) -> _LedgerReading:
    if snapshot.status == "none":
        return "none"
    if snapshot.last_stop is None:
        if snapshot.status == STATUS_ESCALATED:
            return "due" if snapshot.deadline_due else "hold"
        return "pressed" if snapshot.pressed else "fresh"
    name, outcome = snapshot.last_stop
    if outcome is None:
        return "deadline_out" if name == PLATE_CHECK_DEADLINE_STOP else "retry_out"
    if outcome == _STOP_UNCONFIRMED:
        return "unconfirmed"
    return "refused" if outcome in PLATE_CHECK_STOP_NOT_TAKEN else "taken"


def _live_reading(snapshot: _LadderSnapshot, state) -> _LiveReading:
    if not live_reading.is_fresh(state):
        return "stale"
    if printer_incidents.automation_held(snapshot.printer_id):
        return "held"
    if snapshot.job is not None and not is_held_job(_live_job(state), snapshot.job):
        return "other_job"
    if (getattr(state, "state", None) or "").upper() != "PAUSE":
        return "not_paused"
    return "plate_pause" if plate_check_paused(state) else "other_pause"


def _owed_rung(snapshot: _LadderSnapshot, state) -> Rung:
    """THE answer to "what does the farm owe this plate-check episode now?" — :data:`_OWED` over
    (row, ledger, live). A key the table does not spell out RAISES ``KeyError``: an episode in a
    shape nobody wrote a rule for is drift to be seen, never a default to fall into."""
    return _OWED[(snapshot.status, _ledger_reading(snapshot), _live_reading(snapshot, state))]


# --- entry point 2: the plate-check episode (its trigger and its driver) -------------


def _live_job(state) -> str:
    """The printer's live ``subtask_id`` ("" when it names none)."""
    return (getattr(state, "subtask_id", None) or "").strip()


def _sample_plate_check(printer_id: int, state) -> None:
    """The plate-check episode's ONE per-push trigger — spawn its driver when the table owes an ACT.

    Level-triggered on purpose: the question "is a plate check standing that nobody is answering?"
    is re-asked on every push, so nothing depends on witnessing an edge, and a restart re-derives
    it from the wire and the row for free. DB-free: every guard reads a fact the wire or the
    store's projection restates at once:

    * a FRESH report only (``live_reading.is_fresh``) — a reconnect re-broadcasts the
      previous session's cached PAUSE before the pushall answers;
    * no live driver on the printer (``printer_incidents.driver_live``, the gate), and no
      eject owning it (``plate_occupancy.eject_identity``, the power-loss driver's own
      check — a sweep is never pressed or stopped from here);
    * :func:`_owed_rung` over the open ``plate_vision`` row's projection says an ACT is owed:
      a TRIP (no row, paused at the plate dialog); a ``recovering`` row of the live job and any
      pause of it (a restart killed its driver — the driver reads which rung from the ledger);
      an ``escalated`` row whose decision-hold stop went out unanswered. A STANDING decision
      hold is never this trigger's: its window is a timer (:func:`_decision_deadline`), and the
      FALLBACK is a person's for good.

    The driver is registered in the SAME synchronous stretch as its spawn
    (``register_driver``, before its row exists), so a second push cannot spawn a second
    driver while the first is still opening its row. Maintenance mode never reaches here
    (:func:`note_status_push` returns first). Guarded on its own: a failure here must not
    cost the power-loss decision that follows it on the same push.
    """
    try:
        if not live_reading.is_fresh(state):
            return
        if printer_incidents.driver_live(printer_id):
            return
        if plate_occupancy.eject_identity(printer_id) is not None:
            return
        row = printer_incidents.snapshot(printer_id, kind=KIND_PLATE_VISION)
        if _owed_rung(_LadderSnapshot.of_projection(printer_id, row), state) not in _ACTS:
            return

        from backend.app.core.tasks import spawn_background_task

        task = spawn_background_task(
            _plate_check_episode(printer_id, _live_job(state)), name=f"plate-check-p{printer_id}"
        )
        printer_incidents.register_driver(printer_id, task, incident_id=row.get("id") if row is not None else None)
    except Exception:  # noqa: BLE001 — invariant 10: never crash the status flow
        logger.exception("[pause-recovery] plate-check sampler failed for printer %s", printer_id)


@dataclass(frozen=True)
class _Episode:
    """The row one plate-check driver acts for, as it opened or adopted it."""

    printer_id: int
    job: str
    incident_id: int
    item_id: int | None


@dataclass(frozen=True)
class _Owed:
    """One reading of the table for a driver: the rung, the live state it was read over, and the
    reading in words for the log line."""

    rung: Rung
    state: object | None
    why: str


def _owed(episode: _Episode, log: _PlateCheckEvidence, *, deadline_due: bool) -> _Owed:
    """The rung this episode owes RIGHT NOW: the row's status off the store's projection, the
    ledger off the driver's own log, the wire re-read. Read before every send — the level a send
    rests on is never older than the read before it. A row the closers ended owes nothing."""
    state = printer_manager.get_status(episode.printer_id)
    row = printer_incidents.snapshot(episode.printer_id, kind=KIND_PLATE_VISION)
    if row is None or row.get("id") != episode.incident_id:
        return _Owed("stand", state, "its row was closed by its closers")
    snapshot = _LadderSnapshot(
        printer_id=episode.printer_id,
        status=str(row.get("status")),  # type: ignore[arg-type] — an open row's status IS a _RowReading
        job=episode.job,
        pressed=log.pressed(),
        last_stop=log.last_stop(),
        deadline_due=deadline_due,
    )
    rung = _owed_rung(snapshot, state)
    why = f"{snapshot.status} row, ledger {_ledger_reading(snapshot)}, printer {_live_reading(snapshot, state)}"
    return _Owed(rung, state, why)


async def _plate_check_episode(printer_id: int, job: str, *, deadline_due: bool = False) -> None:
    """Drive ONE plate-check episode of ``job`` down the ladder (operator rulings 2026-09-29 and
    2026-10-05), asking :func:`_owed_rung` before every act.

    Opens the episode's row, or adopts the open one the table still owes an act on; then:
    ``press`` → re-check in place (:func:`_recheck_in_place`); ``decide`` → retry or hold, decided
    ONCE (:func:`_decide`): a retry is sent as :data:`PLATE_CHECK_RETRY_STOP`, a hold leaves the
    print paused for a person and the driver EXITS (:func:`_enter_hold` — no driver lives during a
    hold); a stop owed (a decided retry, a re-entry after a crash, the decision deadline that
    spawned this driver with ``deadline_due``) → :func:`_stop_the_print`. Never raises; always
    gives its liveness slot back.
    """
    try:
        opened = await _open_episode(printer_id, job, deadline_due=deadline_due)
        if opened is None:
            return
        episode, log = opened
        owed = _owed(episode, log, deadline_due=deadline_due)
        if owed.rung == "press":
            failed = await _recheck_in_place(episode, log, owed.state)
            if failed is None:
                return
            logger.warning(
                "[pause-recovery] printer %s plate check: re-check failed (%s) — episode %s, job %s",
                printer_id,
                failed,
                episode.incident_id,
                episode.job or "-",
            )
            owed = _owed(episode, log, deadline_due=deadline_due)
        if owed.rung == "decide":
            retry = await _decide(episode, log)
            # The level re-read before the send the decision leads to.
            owed = _owed(episode, log, deadline_due=deadline_due)
            if owed.rung == "decide":
                if not retry:
                    await _enter_hold(episode)
                    return
                owed = replace(owed, rung=PLATE_CHECK_RETRY_STOP)
        await _act(episode, log, owed)
    except Exception:  # noqa: BLE001 — a driver must never crash the event loop
        logger.exception("[pause-recovery] plate-check episode failed for printer %s", printer_id)
    finally:
        task = asyncio.current_task()
        if task is not None:
            printer_incidents.release_driver(printer_id, task)


async def _act(episode: _Episode, log: _PlateCheckEvidence, owed: _Owed) -> None:
    """Carry out a rung after the ladder's own (press, decide): a stop, a hand-over, the FALLBACK
    escalation — or stand down, saying what the table read."""
    if owed.rung in PLATE_CHECK_STOP_NAMES:
        await _stop_the_print(episode, log, owed.rung)
    elif owed.rung == "hand_over":
        await _hand_over(episode, _other_pause_why(owed.state))
    elif owed.rung == "fallback":
        last = log.last_stop()
        outcome = last[1] if last is not None else None
        await _escalate(episode, outcome if _is_fallback_answer(outcome) else "not_taken")
    else:
        logger.info(
            "[pause-recovery] printer %s plate-check episode %s stood down — owed %s (%s)",
            episode.printer_id,
            episode.incident_id,
            owed.rung,
            owed.why,
        )


async def _open_episode(
    printer_id: int, job: str, *, deadline_due: bool
) -> tuple[_Episode, _PlateCheckEvidence] | None:
    """Open the episode's ``recovering`` row — or adopt the open one, or stand down.

    A TRIP opens the row: bound to the paused job, to the FARM unit printing it
    (``farm_correlation.resolve_printing_farm_item`` — by job identity, farm units only; a
    foreign print holds the printer with ``item_id`` NULL), carrying the dialog's short code
    and the printer's full code (``hms_full_codes``, its words after the dialog is gone), and
    projecting the hold onto the unit's ``waiting_reason``.

    When the row cannot open (one ``plate_vision`` row per printer), the open one of this same
    job is ADOPTED only while :func:`_owed_rung` owes an ACT on it — a restart's re-entry, an
    unanswered stop, the decision deadline. A standing decision hold, the FALLBACK, another job's
    row: the driver stands down.
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
            log = await _PlateCheckEvidence.from_row(db, incident.id)
            return _Episode(printer_id=printer_id, job=job, incident_id=incident.id, item_id=incident.item_id), log

        row = await printer_incidents.get_open(db, printer_id, kinds={KIND_PLATE_VISION})
        if row is None or not is_held_job(job, row.job_id):
            logger.info(
                "[pause-recovery] printer %s plate-check episode not driven — %s",
                printer_id,
                "no plate-check pause and no episode to re-enter"
                if row is None
                else f"its open episode {row.id} is on job {row.job_id or '-'}",
            )
            return None
        log = await _PlateCheckEvidence.from_row(db, row.id)
    episode = _Episode(printer_id=printer_id, job=job, incident_id=row.id, item_id=row.item_id)
    owed = _owed(episode, log, deadline_due=deadline_due)
    if owed.rung not in _ACTS:
        logger.info(
            "[pause-recovery] printer %s plate-check episode not driven — its open episode %s owes %s (%s)",
            printer_id,
            row.id,
            owed.rung,
            owed.why,
        )
        return None
    logger.warning(
        "[pause-recovery] printer %s plate-check episode %s re-entered (%s, job %s, code %s) — owed %s, read from "
        "its ledger",
        printer_id,
        row.id,
        f"unit {row.item_id}" if row.item_id is not None else "foreign print",
        job or "-",
        row.code,
        owed.rung,
    )
    return episode, log


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


@dataclass(frozen=True)
class _RecheckRead:
    """What the watch after one press read. ``failed`` — the re-check failed (``why``: rung 2 is owed);
    ``ended`` — the episode ended some other way (passed, handed over, closed, the job over or moved on);
    ``again`` — the printer never answered the press (``why``) and still shows its plate dialog: the
    table is re-read and the press sent again while it owes one."""

    verdict: Literal["failed", "ended", "again"]
    why: str = ""


def _deposit_token(printer_id: int) -> str | None:
    """The plate as the farm can read it NOW (``DepositEvidence.live`` over the client's one peaks
    reader), as the press records it (``DepositEvidence.ledger_token``) — ``None`` with no client or an
    unreadable reading (the decision then reads the live job at rung 2, failing closed)."""
    client = printer_manager.get_client(printer_id)
    if client is None:
        return None
    try:
        return DepositEvidence.live(client.job_peaks()).ledger_token
    except Exception:  # noqa: BLE001 — a reading nobody can take records nothing (the decision fails closed)
        logger.warning("[pause-recovery] printer %s plate check: job peaks unreadable at the press", printer_id)
        return None


async def _recheck_in_place(episode: _Episode, log: _PlateCheckEvidence, state) -> str | None:
    """Rung 1: press the printer's own "Problem solved, resume" and read the re-check's answer — the
    press as an ACTION on the record (``printer_action``: the firmware's ACK, a reboot, a session that
    began with it unacknowledged) and the printer's own next reports first; production's self-heal
    bounds (:data:`_RECHECK_SELF_HEAL`, :data:`_RECHECK_FIRST_LAYER_SELF_HEAL`) are the fallback behind
    them (operator ruling 2026-10-12), read by :func:`_watch_recheck`.

    ``state`` is the reading the table owed the press over, re-read just before this call. Each press
    records the plate it was sent over (:func:`_deposit_token`) — the one decision reads the first.
    Returns WHY the re-check failed when rung 2 is owed; ``None`` when the episode ended some other way
    (passed, handed over, stood down). A press that never left the farm is pressed again on the
    printer's next session, and one the printer never answered while its dialog still stands (the record
    VOIDED it) is pressed again while the table still owes the press: neither is a press the printer
    answered (:meth:`_PlateCheckEvidence.pressed`).
    """
    pid = episode.printer_id
    while True:
        print_error = int(getattr(state, "print_error", 0) or 0)
        prepared = await printer_action.prepare(
            pid,
            printer_action.dialog_press(f"{print_error:08X}", HMSAction.PROBLEM_SOLVED_RESUME, episode.job),
            actor=printer_action.ACTOR_FARM,
            incident_id=episode.incident_id,
        )
        seq = await log.note(PlateCheckStep.dialog(action_id=prepared.action_id, deposit=_deposit_token(pid)))
        sent = await printer_action.publish(prepared)
        if sent.published:
            logger.info(
                "[pause-recovery] printer %s plate check: problem-solved resume sent (seq %s, err %s) — episode %s, "
                "job %s",
                pid,
                sent.sequence_id,
                print_error,
                episode.incident_id,
                episode.job or "-",
            )
            read = await _watch_recheck(episode, log, seq, sent, pressed_at=prepared.sent_at)
        else:
            await log.answer(seq, "not_sent")
            logger.warning(
                "[pause-recovery] printer %s plate check: problem-solved resume answer=not_sent (%s, episode %s) — "
                "pressed again on the printer's next session",
                pid,
                sent.refusal,
                episode.incident_id,
            )
            await printer_action.await_next_session(pid, sent.entry.connection_epoch, recheck_s=_EPISODE_POLL_S)
            read = _RecheckRead("again", "the press did not go out")
        if read.verdict == "failed":
            return read.why
        if read.verdict == "ended":
            return None
        owed = _owed(episode, log, deadline_due=False)
        if owed.rung != "press":
            await _act(episode, log, owed)
            return None
        logger.info(
            "[pause-recovery] printer %s plate check: %s — the press is owed again (episode %s)",
            pid,
            read.why,
            episode.incident_id,
        )
        state = owed.state


async def _watch_recheck(
    episode: _Episode, log: _PlateCheckEvidence, seq: int, sent: printer_action.Sent, *, pressed_at: datetime
) -> _RecheckRead:
    """Watch the record and the wire after the press, the driver live throughout — the printer's events
    first, production's self-heal bounds as the fallback behind them (operator ruling 2026-10-12: "if it
    was self heal, it should stay self heal").

    Each read, in order:

    (r) the record's answer to the press, once it lands (``printer_action.settled``), goes on the ledger
        (:data:`_DIALOG_ANSWER`): ``fail`` (the printer's fail ACK) → FAILED; ``void`` → the press is
        owed again, read below against the wire;
    (a) the row closed, or the printer is on another job → ENDED: the closers or the re-entry rule own
        the row. A report that is not FRESH (the printer left its session) WAITS — the press's answer
        and the printer's report on its return decide, never a gap;
    (b) the plate dialog back on the same job after it left it (the job RUNNING, or the dialog
        cleared) → FAILED;
    (c) a PAUSE on another dialog (a real dialog code that is not a plate code) → HAND OVER;
    (d) RUNNING at layer >= 1 of THIS job (:func:`_job_layer`, off the client's one peaks reader) →
        PASSED, however late. A RUNNING whose layer this client cannot measure (it attached the job
        mid-flight) exits: the sweep closes a job RUNNING on its live report, and a plate dialog that comes
        back re-enters at rung 2. A RUNNING job before layer 1 (the start block a resumed job still runs)
        is waited for until :data:`_RECHECK_FIRST_LAYER_SELF_HEAL` passes, counted from the first RUNNING
        sighting after the press (re-anchored when the job leaves RUNNING): the driver then RELEASES the
        row to its closers (ENDED) — the sweep closes a ``job_pause`` row on RUNNING;
    (s) still PAUSEd on this job — the press accepted or not, the dialog standing or cleared — when
        :data:`_RECHECK_SELF_HEAL` has passed since the press (``pressed_at``) → FAILED, rung 2 decides
        (production's verdict). A press the printer never answered, or one the record voided, is first
        answered ``bound_passed`` on the ledger, so the table owes rung 2 — after a restart too — and never
        a second press; the press's ACTION stays the record's, and a late answer to it changes nothing
        here (the decision already made stands);
    (e) a press the record voided while the plate dialog still stands, never having left it → AGAIN.

    Everything else WAITS for the printer's next event: a PAUSE with no dialog right after the press (the
    press clears the dialog before the job leaves PAUSE) and a PAUSE still at the dialog before the record
    answered, each until its bound. Both bounds count the printer's ONLINE seconds
    (``printer_action.bound_due``): an offline stretch never counts toward them. A job the printer reports
    over exits too: its terminal closes the row.
    """
    pid = episode.printer_id
    left_the_dialog = False
    answered = False
    voided = False
    gap_noted = False
    # The first RUNNING sighting after the press (the record's clock): the first-layer bound's anchor.
    running_at: datetime | None = None
    while True:
        if not _row_open(episode):
            logger.info(
                "[pause-recovery] printer %s plate-check episode %s closed by its closers", pid, episode.incident_id
            )
            return _RecheckRead("ended")
        if not answered:
            answer = await printer_action.settled(sent.action_id)
            if answer is not None:
                answered = True
                token = _DIALOG_ANSWER[answer]
                reason = await printer_action.reason_of(sent.action_id)
                await log.answer(seq, token)
                logger.info(
                    "[pause-recovery] printer %s plate check: problem-solved resume answer=%s (episode %s%s)",
                    pid,
                    token,
                    episode.incident_id,
                    f", reason {reason}" if reason else "",
                )
                if token == "fail":
                    return _RecheckRead("failed", "the firmware answered fail" + (f" ({reason})" if reason else ""))
                voided = token == "void"
        state = printer_manager.get_status(pid)
        if not live_reading.is_fresh(state):
            if not gap_noted:
                gap_noted = True
                logger.info(
                    "[pause-recovery] printer %s plate-check episode %s: the printer left its session — the press's "
                    "answer and its next report decide on its return",
                    pid,
                    episode.incident_id,
                )
            await asyncio.sleep(_EPISODE_POLL_S)
            continue
        gap_noted = False
        live_job = _live_job(state)
        if not is_held_job(live_job, episode.job):
            logger.info(
                "[pause-recovery] printer %s plate-check episode %s: the printer is on another job (%s)",
                pid,
                episode.incident_id,
                live_job or "-",
            )
            return _RecheckRead("ended")
        live = (getattr(state, "state", None) or "").upper()
        if live != "RUNNING":
            running_at = None
        if live == "PAUSE":
            dialog = _dialog_of(state)
            if dialog and not plate_check_paused(state):
                await _hand_over(episode, _other_pause_why(state))
                return _RecheckRead("ended")
            if not dialog:
                left_the_dialog = True
            elif left_the_dialog:
                return _RecheckRead("failed", "the plate dialog came back on the same job")
            if printer_action.bound_due(pid, _RECHECK_SELF_HEAL, pressed_at):
                if not answered or voided:
                    # The press is SPENT: the table owes rung 2 from here on, never a second press.
                    await log.answer(seq, _PRESS_BOUND_PASSED)
                printer_action.note_bound_passed(
                    pid, _RECHECK_SELF_HEAL, lane="plate-check re-check", action_id=sent.action_id
                )
                return _RecheckRead(
                    "failed",
                    f"still paused {_RECHECK_SELF_HEAL.seconds:.0f}s online after the press "
                    f"({'the dialog standing' if dialog else 'no dialog'}, "
                    f"{'the press answered' if answered and not voided else 'the press unanswered'})",
                )
            if dialog and voided:
                return _RecheckRead("again", "the printer never answered the press and still shows its plate dialog")
        else:
            left_the_dialog = True
            if live in _JOB_OVER_STATES:
                logger.info(
                    "[pause-recovery] printer %s plate-check episode %s: the job reads %s — its terminal closes "
                    "the episode",
                    pid,
                    episode.incident_id,
                    live,
                )
                return _RecheckRead("ended")
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
                    return _RecheckRead("ended")
                if layer >= 1:
                    if not answered:
                        # The printer printed past its dialog: the press was answered on the wire.
                        await log.answer(seq, "success")
                        logger.info(
                            "[pause-recovery] printer %s plate check: problem-solved resume answer=success (episode "
                            "%s, the job printed past its dialog before the ACK)",
                            pid,
                            episode.incident_id,
                        )
                    await _close_passed(episode, layer)
                    return _RecheckRead("ended")
                if running_at is None:
                    running_at = printer_action.now()
                elif printer_action.bound_due(pid, _RECHECK_FIRST_LAYER_SELF_HEAL, running_at):
                    printer_action.note_bound_passed(
                        pid, _RECHECK_FIRST_LAYER_SELF_HEAL, lane="plate-check first layer"
                    )
                    logger.info(
                        "[pause-recovery] printer %s plate-check episode %s: the job has run %.0fs online without "
                        "reaching layer 1 — released to its closers (the sweep closes it on RUNNING, a returning "
                        "plate dialog re-enters)",
                        pid,
                        episode.incident_id,
                        _RECHECK_FIRST_LAYER_SELF_HEAL.seconds,
                    )
                    return _RecheckRead("ended")
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


async def _decide(episode: _Episode, log: _PlateCheckEvidence) -> bool:
    """Rung 2's ONE decision (operator ruling 2026-10-05): RETRY the print, or HOLD it paused for a
    person. True = retry. Made once, here, and recorded as the name of the stop that follows
    (:data:`PLATE_CHECK_RETRY_STOP`) — the terminal reads it back and never re-decides.

    A retry needs all four:

    * the episode's row bound a FARM unit (a foreign print has nothing to requeue);
    * that unit is not itself the RETRY of a plate the farm already stopped and retried at its plate
      check — the lineage (``requeue.retries_a_plate_check_retry``), the ruling of 2026-09-29 verbatim:
      "If that re-check fails, stop and retry the same print. If the retry's re-check fails too,
      escalate";
    * the plate is not a REPEAT on this printer — the operator's "2 failures between a successful
      print": the printer's last plate-check stop on another episode
      (``printer_incidents.last_plate_check_stop_at``, read through the stop's action) has a completed
      print after it (``print_binding.completed_since``), or there is none. A bad plate that trips unit
      after unit (003-H2S) is held at its second failure whichever unit it is. It replaces production's
      60-minute window, which stays deleted: a window counted another job's stop an hour back and
      missed one an hour and a minute back;
    * the job deposited nothing (:func:`_deposit_at_decision`: the live job when this client can vouch
      for its peaks, else what the trip recorded — so a restart between the press and this rung keeps
      the silent retry).

    Anything else would be an escalation, and an escalation now leaves the print paused for the
    decision window instead of stopping it. Fails closed toward the person: an unreadable lineage or
    printer history, or a plate nobody can vouch for, HOLDS — never a retry nobody could justify. The
    decision's log line names every rule that held. Reads: the lineage walk, then two queries for the
    repeat rule (the last stop, then a completed print since it) — none for a foreign print.
    """
    from backend.app.core.database import async_session
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.services import print_binding, requeue

    pid = episode.printer_id
    retry_of_a_retry: bool | None = None
    repeat: bool | None = None
    last_stop_at: datetime | None = None
    if episode.item_id is not None:
        try:
            async with async_session() as db:
                item = await db.get(PrintQueueItem, episode.item_id)
                if item is not None:
                    retry_of_a_retry = await requeue.retries_a_plate_check_retry(db, item)
        except Exception:  # noqa: BLE001 — an unreadable lineage holds; it never retries blind
            logger.warning(
                "[pause-recovery] printer %s plate check: the unit's lineage is unreadable — the print is held for "
                "a person, never retried blind (episode %s)",
                pid,
                episode.incident_id,
                exc_info=True,
            )
        try:
            async with async_session() as db:
                last_stop_at = await printer_incidents.last_plate_check_stop_at(
                    db, pid, exclude_incident_id=episode.incident_id
                )
                repeat = last_stop_at is not None and not await print_binding.completed_since(db, pid, last_stop_at)
        except Exception:  # noqa: BLE001 — an unreadable printer history holds; it never retries blind
            logger.warning(
                "[pause-recovery] printer %s plate check: the printer's plate-check history is unreadable — the "
                "print is held for a person, never retried blind (episode %s)",
                pid,
                episode.incident_id,
                exc_info=True,
            )
    deposited, deposit_source = _deposit_at_decision(pid, log)
    retry = episode.item_id is not None and retry_of_a_retry is False and repeat is False and not deposited
    held_by: list[str] = []
    if episode.item_id is None:
        held_by.append("a foreign print")
    else:
        if retry_of_a_retry is None:
            held_by.append("the unit's lineage is unreadable")
        elif retry_of_a_retry:
            held_by.append(f"unit {episode.item_id} is the retry of a plate the farm already stopped and retried")
        if repeat is None:
            held_by.append("the printer's plate-check history is unreadable")
        elif repeat:
            held_by.append(
                f"a repeat on this printer: its last plate-check stop ({last_stop_at:%Y-%m-%d %H:%M:%S} UTC) "
                "has no completed print since"
            )
    if deposited:
        held_by.append(f"a deposit on the plate ({deposit_source})")
    logger.warning(
        "[pause-recovery] printer %s plate check: decided %s — %s (episode %s, job %s)",
        pid,
        PLATE_CHECK_RETRY_STOP if retry else "the decision hold",
        f"unit {episode.item_id}, a first failure on this printer, nothing deposited ({deposit_source})"
        if retry
        else "held by: " + "; ".join(held_by),
        episode.incident_id,
        episode.job or "-",
    )
    return retry


def _deposit_at_decision(printer_id: int, log: _PlateCheckEvidence) -> tuple[bool, str]:
    """Has the job deposited anything, as rung 2 decides it — and where that answer came from.

    The LIVE job (``DepositEvidence.live`` over the client's one peaks reader) when the client can vouch
    for its peaks (``reliable`` — it watched the job start); else the plate the trip RECORDED on its first
    press (``DepositEvidence.recorded`` over :meth:`_PlateCheckEvidence.trip_deposit`) — a restart between
    the press and this rung leaves a client that attached the job mid-flight, whose reading proves
    nothing, while the trip's own measurement still stands. No record either: fails CLOSED (a deposit)."""
    client = printer_manager.get_client(printer_id)
    if client is not None:
        try:
            peaks = client.job_peaks()
            if peaks.reliable:
                return DepositEvidence.live(peaks).deposited, "the live job"
        except Exception:  # noqa: BLE001 — an unreadable reading falls to the trip's record
            logger.warning("[pause-recovery] printer %s plate check: job peaks unreadable", printer_id, exc_info=True)
    recorded = log.trip_deposit()
    source = "the trip's record" if recorded is not None else "no reading, failing closed"
    return DepositEvidence.recorded(recorded).deposited, source


def _decision_hold_sentence(words: str, window_s: float) -> str:
    """The decision hold's ONE page, in the printer's words first (operator ruling 2026-10-05).
    Sys-admin register: where the print is, what a person may do, what the farm does if nobody
    does. "Failed twice": the trip was the printer's own check, the farm's re-check the second."""
    minutes = int(window_s // 60)
    return (
        f"The printer reported: {words.rstrip('.')}. Plate check failed twice. Print paused for {minutes} min: "
        f"Ignore and resume, or Stop. After {minutes} min the farm stops the print and holds the plate."
    )


async def _decision_window_s(db: AsyncSession) -> float:
    """THE reading of the decision window, ``farm_plate_check_decision_minutes`` (seconds), through
    the farm's one minutes resolver (``farm_stall._grace_seconds``). A different concern from
    ``farm_pause_stall_minutes`` (when an unattended pause starts paging): this is how long a
    plate-check print waits paused for a person before the farm stops it."""
    from backend.app.services.farm_stall import _grace_seconds

    return await _grace_seconds(db, _DECISION_WINDOW_KEY, _DECISION_WINDOW_DEFAULT_MIN)


async def _enter_hold(episode: _Episode) -> None:
    """The DECISION HOLD (operator ruling 2026-10-05): the stop would be an escalation, so the farm
    leaves the print PAUSED at the plate dialog for a person to decide.

    ``mark_escalated`` (the row becomes a person's; its ``escalated_at`` starts the window), the
    deadline timer armed (:func:`_decision_deadline`), ONE page in the printer's own words saying
    what a person may do and what the farm does when the window runs out — and the driver EXITS:
    no driver lives during a hold, so every closer (the person's resume, their stop, the job's
    terminal) ends it through the rule table, and the person's turn (``printer_incidents
    .plate_check_human_turn``) is on. The window's end is the timer's, never this driver's.
    """
    from backend.app.core.database import async_session
    from backend.app.models.printer import Printer
    from backend.app.services.notification_service import notification_service

    pid = episode.printer_id
    async with async_session() as db:
        row = await printer_incidents.mark_escalated(db, episode.incident_id)
        if row is None:
            logger.info(
                "[pause-recovery] printer %s plate-check episode %s closed before the decision hold — nothing to hold",
                pid,
                episode.incident_id,
            )
            return
        # Armed before the page: a page that fails must not cost the hold its end.
        _arm_decision_deadline(pid, episode.incident_id)
        window_s = await _decision_window_s(db)
        printer = await db.get(Printer, pid)
        printer_name = printer.name if printer is not None else f"printer {pid}"
        words = summary_of(printer_incidents.printer_messages_of(row)) or row.code
        await notification_service.on_plate_not_empty(
            pid, printer_name, db, source_detail=_decision_hold_sentence(words, window_s)
        )
    logger.warning(
        "[pause-recovery] printer %s plate check: DECISION HOLD — print left PAUSED for Ignore and resume or Stop; "
        "the farm stops it %.0f min after the hold (episode %s escalated, %s, job %s)",
        pid,
        window_s / 60,
        episode.incident_id,
        f"unit {episode.item_id}" if episode.item_id is not None else "foreign print",
        episode.job or "-",
    )


# Why the farm sends each stop, in words for its log line.
_STOP_WHY: dict[str, str] = {
    PLATE_CHECK_RETRY_STOP: "the retry rung",
    PLATE_CHECK_DEADLINE_STOP: "the decision window ran out with nobody pressing Ignore and resume or Stop",
}


async def _stop_the_print(episode: _Episode, log: _PlateCheckEvidence, name: str) -> None:
    """The farm STOPS the print (the heaters go off with it), the stop NAMED by the decision it
    carries — :data:`PLATE_CHECK_RETRY_STOP` (rung 2) or :data:`PLATE_CHECK_DEADLINE_STOP` (the
    decision window ran out). The caller read :func:`_owed_rung` just before.

    The stop is an ACTION read through the record (``printer_action``), its events first: it is TAKEN
    when the job is over — the printer reporting it holds no job, or the job's terminal — however late
    (014-H2S 2026-10-05: 56–62 s, where a 30 s window answered both stops ``not_taken``). Production's
    self-heal bound is the fallback behind them (operator ruling 2026-10-12): no event by
    :data:`STOP_SELF_HEAL` in the printer's online time and the stop is answered ``unconfirmed`` on the
    ledger and the FALLBACK runs (:func:`_escalate` — the person's turn on, the same page). The action
    stays outstanding, so its terminal landing later is still the farm's stop to the terminal
    (``PlateCheckFacts.farm_stopped`` reads the action), and a person's own Stop takes it over.
    Each send is one ledger step; a later send supersedes the one before it (the last speaks, and the
    earlier is answered only once its successor is on the ledger, so a crash in between leaves the stop
    owed). A frame that never left the farm is sent again on the printer's next session while the table
    still owes THIS stop, and one the record VOIDED (a reboot, or a later session began with it
    unacknowledged) likewise; no longer owed, it is answered ``not_sent`` / ``not_taken`` and the row's
    closers own what comes next. A stop the printer REFUSED (its reason), or one a person's command took
    over, did not end the job: the FALLBACK (:func:`_escalate`). The driver stays live until the stop's
    answer; after a TAKEN stop the job's terminal closes the row — its in-flight mark keeps every looking
    closer off the row until it has (``printer_manager.terminal_in_flight``). What the terminal makes of
    the stop is ``terminal_outcome.farm_retries``' reading of its name.
    """
    pid = episode.printer_id
    logger.warning(
        "[pause-recovery] printer %s plate check: %s — stopping the print (%s, episode %s, job %s)",
        pid,
        _STOP_WHY[name],
        name,
        episode.incident_id,
        episode.job or "-",
    )
    superseded: tuple[int, PlateCheckStopNotTaken] | None = None
    while True:
        prepared = await printer_action.prepare(
            pid, printer_action.stop(), actor=printer_action.ACTOR_FARM, incident_id=episode.incident_id
        )
        seq = await log.note(PlateCheckStep.stop(name, action_id=prepared.action_id))
        if superseded is not None:
            await log.answer(*superseded)
        sent = await printer_action.publish(prepared)
        unanswered: PlateCheckStopNotTaken
        if sent.published:
            answer = await printer_action.await_outcome(sent, bound=STOP_SELF_HEAL)
            if isinstance(answer, printer_action.Unanswered):
                await log.answer(seq, _STOP_UNCONFIRMED)
                logger.warning(
                    "[pause-recovery] printer %s plate check: stop answer=unconfirmed (%s, action %s, no event in "
                    "%.0fs online, %s; episode %s, job %s) — the fallback",
                    pid,
                    name,
                    sent.action_id,
                    STOP_SELF_HEAL.seconds,
                    answer.reading or "no reading",
                    episode.incident_id,
                    episode.job or "-",
                )
                await _escalate(episode, _STOP_UNCONFIRMED)
                return
            reason = await printer_action.reason_of(sent.action_id)
            if answer is Answer.REACHED:
                await log.answer(seq, "taken")
                logger.info(
                    "[pause-recovery] printer %s plate check: stop answer=taken (%s, action %s, episode %s, job %s) "
                    "— the job's terminal closes the episode",
                    pid,
                    name,
                    sent.action_id,
                    episode.incident_id,
                    episode.job or "-",
                )
                return
            if answer is not Answer.VOID:
                await log.answer(seq, "not_taken")
                logger.warning(
                    "[pause-recovery] printer %s plate check: stop answer=not_taken (%s, action %s %s%s, episode %s, "
                    "job %s)",
                    pid,
                    name,
                    sent.action_id,
                    answer.value,
                    f": {reason}" if reason else "",
                    episode.incident_id,
                    episode.job or "-",
                )
                await _escalate(episode, "not_taken")
                return
            unanswered = "not_taken"
            logger.info(
                "[pause-recovery] printer %s plate check: the stop (action %s) was void (%s) — the table is re-read",
                pid,
                sent.action_id,
                reason or "no reason",
            )
        else:
            unanswered = "not_sent"
            logger.info(
                "[pause-recovery] printer %s plate check: the stop (action %s) did not go out (%s) — sent again on "
                "the printer's next session",
                pid,
                sent.action_id,
                sent.refusal,
            )
            await printer_action.await_next_session(pid, sent.entry.connection_epoch, recheck_s=_EPISODE_POLL_S)
        owed = _owed(episode, log, deadline_due=False)
        if owed.rung != name:
            await log.answer(seq, unanswered)
            logger.info(
                "[pause-recovery] printer %s plate-check episode %s stood down before the stop was sent again — owed "
                "%s (%s)",
                pid,
                episode.incident_id,
                owed.rung,
                owed.why,
            )
            return
        superseded = (seq, unanswered)


async def _escalate(episode: _Episode, answer: FallbackAnswer) -> None:
    """The FALLBACK: the farm could not end the job, so the paused print is a person's for good.

    ``answer`` is why (:data:`FallbackAnswer`): the stop not taken, never sent, or ``unconfirmed`` —
    no event answered it before :data:`STOP_SELF_HEAL` (production's verdict at its bound).
    ``mark_escalated`` — the row stays open and the farm never acts on it again (the table reads
    an escalated row whose stop did not end the job as ``human``) — and ONE page in the printer's
    own words (the row's recorded ``hms_full_codes``) with :data:`_VISION_PAUSED_INSTRUCTION`. The
    person's turn stays on (Ignore and resume / Problem solved / Stop); their resume or stop ends
    the row through the ordinary closers. A decision-hold row is already escalated: its stop not
    landing pages the same way, and its window is over.
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


# --- the decision hold's deadline: a TIMER, never liveness (operator ruling 2026-10-05) ---------


def _standing_hold(printer_id: int, incident_id: int) -> Mapping[str, object] | None:
    """The projection of ``incident_id`` while it is a STANDING decision hold — open, escalated,
    no farm stop on its ledger — else ``None`` (it closed, or a stop went out on it)."""
    row = printer_incidents.snapshot(printer_id, kind=KIND_PLATE_VISION)
    if (
        row is None
        or row.get("id") != incident_id
        or row.get("status") != STATUS_ESCALATED
        or row.get("last_stop") is not None
    ):
        return None
    return row


def _timer_armed(printer_id: int, incident_id: int) -> bool:
    """Is a deadline timer for ``incident_id`` running on this printer right now?"""
    held = _deadline_timers.get(printer_id)
    return held is not None and held[0] == incident_id and not held[1].done()


def _arm_decision_deadline(printer_id: int, incident_id: int) -> None:
    """Arm the decision hold's deadline timer (one per printer), replacing a timer of another hold.
    A timer already running for this hold is kept — re-arming is idempotent."""
    held = _deadline_timers.get(printer_id)
    if held is not None and not held[1].done():
        if held[0] == incident_id:
            return
        held[1].cancel()
    from backend.app.core.tasks import spawn_background_task

    _deadline_timers[printer_id] = (
        incident_id,
        spawn_background_task(_decision_deadline(printer_id, incident_id), name=f"plate-check-deadline-p{printer_id}"),
    )


def _deadline_stop_owed(printer_id: int, row: Mapping[str, object]) -> bool:
    """Does the table owe the decision deadline's stop on this printer NOW? The window ran out
    (``deadline_due``), and nothing else owns the printer: no live driver, no eject."""
    if printer_incidents.driver_live(printer_id) or plate_occupancy.eject_identity(printer_id) is not None:
        return False
    snapshot = replace(_LadderSnapshot.of_projection(printer_id, row), deadline_due=True)
    return _owed_rung(snapshot, printer_manager.get_status(printer_id)) == PLATE_CHECK_DEADLINE_STOP


async def _decision_deadline(printer_id: int, incident_id: int) -> None:
    """The decision hold's deadline (operator ruling 2026-10-05: a bounded window): a TIMER, so the
    window ends on time whether or not anything reports.

    Computes the deadline ONCE — the hold's ``escalated_at`` (its projection) plus
    ``farm_plate_check_decision_minutes`` (:func:`_decision_window_s`) — and projects it
    (``printer_incidents.note_deadline``, its one writer) so the card can say when the farm stops
    the print. Then sleeps to it, re-reading the projection every :data:`_DEADLINE_POLL_S`: a hold
    a person ended (their resume, their stop, the job's terminal) stops the timer within that step.
    Once due, the timer spawns the episode's driver with ``deadline_due`` — registered in the same
    synchronous stretch (``driver_live``: the person's turn is OFF while the stop is in flight) —
    the moment the table owes :data:`PLATE_CHECK_DEADLINE_STOP` (the print still paused at the
    plate dialog on that job, a fresh report, no maintenance mode, no driver, no eject); until
    then it keeps waiting, so a printer that comes back from a disconnect still paused is stopped
    then. ``note_deadline(None)`` on every exit.

    Rehydrate story: process memory, re-armed at the printer's session start
    (:func:`rearm_decision_deadlines`) and by the hourly reminder pass for a past-due hold with no timer
    (:func:`rearm_overdue_decision_deadlines`), both from ``escalated_at`` + the setting.
    """
    try:
        row = _standing_hold(printer_id, incident_id)
        if row is None or not row.get("escalated_at"):
            return
        escalated_at = datetime.fromisoformat(str(row["escalated_at"]))
        from backend.app.core.database import async_session

        async with async_session() as db:
            window_s = await _decision_window_s(db)
        due = escalated_at + timedelta(seconds=window_s)
        printer_incidents.note_deadline(printer_id, incident_id, due)
        logger.info(
            "[pause-recovery] printer %s plate check: decision deadline %s UTC (%.0f min after the hold) — episode %s",
            printer_id,
            due.isoformat(timespec="seconds"),
            window_s / 60,
            incident_id,
        )
        while True:
            row = _standing_hold(printer_id, incident_id)
            if row is None:
                logger.info(
                    "[pause-recovery] printer %s plate-check decision hold %s ended — its deadline timer stops",
                    printer_id,
                    incident_id,
                )
                return
            now = datetime.utcnow()
            if now >= due and _deadline_stop_owed(printer_id, row):
                from backend.app.core.tasks import spawn_background_task

                job = str(row.get("job_id") or "")
                task = spawn_background_task(
                    _plate_check_episode(printer_id, job, deadline_due=True), name=f"plate-check-p{printer_id}"
                )
                printer_incidents.register_driver(printer_id, task, incident_id=incident_id)
                logger.warning(
                    "[pause-recovery] printer %s plate check: the decision window ran out — the farm stops the "
                    "print (episode %s, job %s)",
                    printer_id,
                    incident_id,
                    job or "-",
                )
                return
            remaining = (due - now).total_seconds()
            await asyncio.sleep(min(remaining, _DEADLINE_POLL_S) if remaining > 0 else _DEADLINE_POLL_S)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — a deadline failure must never escape the task
        logger.exception("[pause-recovery] plate-check decision deadline failed for printer %s", printer_id)
    finally:
        held = _deadline_timers.get(printer_id)
        if held is not None and held[1] is asyncio.current_task():
            del _deadline_timers[printer_id]
        printer_incidents.note_deadline(printer_id, incident_id, None)


async def rearm_decision_deadlines(printer_id: int) -> int:
    """SESSION START: re-arm the deadline timer of ``printer_id``'s standing decision hold — an open,
    escalated ``plate_vision`` row with no farm stop on its ledger. Returns how many. Never raises.

    Run once per MQTT session by ``session_start`` (``printer_events.FirstFreshReport``), after the
    lifespan rehydrated the incident projection the timer reads. The timer is process memory and a
    restart killed it; the hold is a row and survived. Each timer re-derives its deadline from the
    row's ``escalated_at`` and the setting, so a hold whose window ran out during the downtime is
    stopped at once (the printer is reporting, so the table can see it still paused at the plate
    dialog) and one still open waits out the rest. A timer already running is kept
    (:func:`_arm_decision_deadline` is idempotent), so a reconnect re-arms nothing.
    """
    try:
        from backend.app.core.database import async_session

        holds: list[int] = []
        async with async_session() as db:
            for row in await printer_incidents.open_rows(db, printer_id):
                if row.kind != KIND_PLATE_VISION or row.status != STATUS_ESCALATED:
                    continue
                try:
                    log = await _PlateCheckEvidence.from_row(db, row.id)
                except LookupError:
                    logger.exception(
                        "[pause-recovery] session start — incident %s has an unreadable step ledger", row.id
                    )
                    continue
                if log.last_stop() is None:
                    holds.append(row.id)
        for incident_id in holds:
            _arm_decision_deadline(printer_id, incident_id)
        if holds:
            logger.info(
                "[pause-recovery] printer %s session start — plate-check decision hold(s) re-armed: episode %s",
                printer_id,
                ", ".join(str(incident_id) for incident_id in holds),
            )
        return len(holds)
    except Exception:  # noqa: BLE001 — the session start must re-evaluate the printer's other lanes
        logger.exception("[pause-recovery] session-start decision-deadline re-arm failed for printer %s", printer_id)
        return 0


async def rearm_overdue_decision_deadlines(db: AsyncSession, open_rows: Sequence[PrinterIncident]) -> int:
    """The hourly reminder pass's re-arm (``farm_stall.check_attention_reminders``): a standing
    decision hold whose deadline has PASSED with no timer running — the timer died, or the printer's
    session-start re-arm never ran — gets one, which stops the print at once. Returns how many.

    ``open_rows`` is the pass's own read of the open rows, and the hold facts come off the DB-free
    projection, so this costs nothing unless a candidate exists (then one settings read). Never
    raises: a reminder pass must not die on it.
    """
    try:
        candidates: list[tuple[int, int, datetime]] = []
        for row in open_rows:
            if row.kind != KIND_PLATE_VISION or row.status != STATUS_ESCALATED:
                continue
            if _timer_armed(row.printer_id, row.id):
                continue
            hold = _standing_hold(row.printer_id, row.id)
            if hold is None or not hold.get("escalated_at"):
                continue
            candidates.append((row.printer_id, row.id, datetime.fromisoformat(str(hold["escalated_at"]))))
        if not candidates:
            return 0
        window_s = await _decision_window_s(db)
        now = datetime.utcnow()
        armed = 0
        for printer_id, incident_id, escalated_at in candidates:
            if escalated_at + timedelta(seconds=window_s) > now:
                continue
            logger.warning(
                "[pause-recovery] printer %s plate-check decision hold %s is past its deadline with no timer — re-armed",
                printer_id,
                incident_id,
            )
            _arm_decision_deadline(printer_id, incident_id)
            armed += 1
        return armed
    except Exception:  # noqa: BLE001 — a reminder pass must never die on the re-arm
        logger.exception("[pause-recovery] decision-deadline re-arm failed")
        return 0


# --- entry point 2b: a person's press on the plate-check dialog, through the farm -----------

# Why a person's press was NOT sent: the human's-turn rule's own refusals
# (``printer_incidents.PlateCheckTurnRefusal``) plus ``unknown_action`` — a button outside the
# turn's ``actions``.
HumanDialogRefusal = Literal["farm_acting", "no_hold", "not_paused", "other_job", "unknown_action"]


@dataclass(frozen=True)
class HumanDialogVerdict:
    """What the farm did with a person's press on the plate-check dialog; the route maps it to HTTP.

    ``refusal`` — why nothing was sent, or ``None``. ``sent`` — the action that was recorded and
    published (``None`` when refused). What the printer answered is the RECORD's (``printer_action``),
    written on the episode's ledger when it lands; the route reads the ACK for its own reply
    (``printer_action.ack_within`` — it shapes only what the person is told, never the ledger).
    """

    refusal: HumanDialogRefusal | None = None
    sent: printer_action.Sent | None = None


async def human_dialog_action(
    printer_id: int, action: str, print_error_hex: str, job_id: str | None
) -> HumanDialogVerdict:
    """A person presses a plate-check dialog button through the farm — "Ignore and resume" or
    "Problem solved, resume" (operator ruling 2026-10-05). Never the ladder's: the ladder presses
    only its own re-check, and never "Ignore".

    Refused unless it is the person's turn (``printer_incidents.plate_check_human_turn``, the ONE
    predicate the card's buttons render from): ``farm_acting`` while the ladder is still working
    (a ``recovering`` row, or a live driver — the decision deadline's stop in flight); ``no_hold``
    / ``not_paused`` / ``other_job`` when no paused plate check of the hold's job stands; and
    ``unknown_action`` for a button outside the turn's ``actions``. Otherwise the press is noted
    on the episode's ledger as a dialog step named by the action (what was sent on a person's
    behalf, which the wire cannot restate) and sent through the record as the operator's action
    (``printer_action.dialog_press`` over ``client.execute_hms_action``, the one frame builder) for the
    LIVE dialog (``print_error``) and the hold's job; its answer goes on the ledger when the record has
    it (:func:`_answer_on_the_ledger` — the firmware's ACK however late, a reboot, a new session),
    never off a window. The request's own ``print_error_hex`` / ``job_id`` name what the person saw;
    the frame carries what the printer shows now, and a mismatch is logged. What the press does next is
    the wire's: a resume ends the row through the rule table (``observed_running``) and the deadline
    timer stops with it.
    """
    from backend.app.core.database import async_session
    from backend.app.core.tasks import spawn_background_task

    state = printer_manager.get_status(printer_id)
    turn = printer_incidents.plate_check_human_turn(printer_id, state)
    if turn is None:
        refusal: HumanDialogRefusal = printer_incidents.plate_check_turn_refusal(printer_id, state) or "no_hold"
        logger.info(
            "[pause-recovery] printer %s plate check: a person's %s refused — %s (dialog %s, job %s)",
            printer_id,
            action,
            refusal,
            print_error_hex,
            job_id or "-",
        )
        return HumanDialogVerdict(refusal=refusal)
    if action not in turn["actions"]:
        logger.info(
            "[pause-recovery] printer %s plate check: a person's %s refused — unknown_action (the turn offers %s)",
            printer_id,
            action,
            ", ".join(turn["actions"]),
        )
        return HumanDialogVerdict(refusal="unknown_action")
    if print_error_hex.upper() != turn["print_error"] or (job_id or "") != turn["job_id"]:
        logger.info(
            "[pause-recovery] printer %s plate check: the press named dialog %s job %s; the printer shows %s on "
            "job %s — the frame answers what the printer shows",
            printer_id,
            print_error_hex,
            job_id or "-",
            turn["print_error"],
            turn["job_id"] or "-",
        )
    row = printer_incidents.snapshot(printer_id, kind=KIND_PLATE_VISION)
    if row is None or not isinstance(row.get("id"), int):
        return HumanDialogVerdict(refusal="no_hold")
    incident_id = int(row["id"])
    async with async_session() as db:
        log = await _PlateCheckEvidence.from_row(db, incident_id)
    prepared = await printer_action.prepare(
        printer_id,
        printer_action.dialog_press(turn["print_error"], action, turn["job_id"]),
        actor=printer_action.ACTOR_OPERATOR,
        incident_id=incident_id,
    )
    seq = await log.note(PlateCheckStep.human(action, action_id=prepared.action_id))
    sent = await printer_action.publish(prepared)
    if not sent.published:
        await log.answer(seq, "not_sent")
        logger.warning(
            "[pause-recovery] printer %s plate check: a person's %s answer=not_sent (episode %s)",
            printer_id,
            action,
            incident_id,
        )
        return HumanDialogVerdict(sent=sent)
    logger.info(
        "[pause-recovery] printer %s plate check: a person's %s sent (seq %s, episode %s, job %s)",
        printer_id,
        action,
        sent.sequence_id,
        incident_id,
        turn["job_id"] or "-",
    )
    spawn_background_task(
        _answer_on_the_ledger(incident_id, seq, sent, action), name=f"plate-check-press-p{printer_id}"
    )
    return HumanDialogVerdict(sent=sent)


async def _answer_on_the_ledger(incident_id: int, seq: int, sent: printer_action.Sent, action: str) -> None:
    """A person's press, answered on the episode's ledger by the RECORD's answer to it
    (``printer_action.await_outcome`` — an event, however late; :data:`_DIALOG_ANSWER`). Never raises: a
    ledger write must never escape its task."""
    from backend.app.core.database import async_session

    try:
        answer = await printer_action.await_outcome(sent)
        async with async_session() as db:
            log = await _PlateCheckEvidence.from_row(db, incident_id)
        await log.answer(seq, _DIALOG_ANSWER[answer])
        logger.info(
            "[pause-recovery] printer %s plate check: a person's %s answer=%s (action %s, episode %s)",
            sent.printer_id,
            action,
            _DIALOG_ANSWER[answer],
            sent.action_id,
            incident_id,
        )
    except Exception:  # noqa: BLE001 — a ledger write must never escape its task
        logger.exception("[pause-recovery] a person's press could not be answered on episode %s", incident_id)


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
