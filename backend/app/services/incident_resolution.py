"""THE rule table: does THIS occasion end THIS equipment-fault row, and on what evidence?

One owner for a question that used to be answered in six places. Before this module
each closer (``spool_recovery.on_observed_running`` / ``on_job_terminal`` /
``sweep_open_incidents`` / the session start's ``reevaluate_session``, ``_resume_after_repair``'s
readiness test and ``pause_recovery.on_plate_cleared``) carried its own
``if resolution == …`` chain, and the motion evidence the ``repair`` class turns on
lived in ``spool_recovery`` module globals (and, until 2026-10-11, in a process ledger
here). Adding an evidence therefore meant editing several closers — which is how 011-H2S 2026-09-17 came to sit escalated with a clean
wire, an idle printer and a completed print through the repaired path, because the
commonest fleet repair (hand-clear the path, resume, the job runs to ``completed``)
was not in anybody's vocabulary.

The domain is **one rule table plus its evidence predicates** — not a strategy family. Every
evidence a cell reads is an EVENT the printer or a person produced, or the durable record of one
(2026-10-11: "using time as a gate is not the correct way to discern status complete or
incompleteness") — no dwell, and no process memory a restart empties. The variance is
``(resolution_class × occasion) → verdict`` and most cells are "stand"; :data:`_TABLE` spells every cell out and a
missing key RAISES. That is the ``declared``-class lesson made structural: three
closers once treated "not operator / not repair" as the WIRE lane through a bare
``else``, so a fourth class inherited evidence that said nothing about it. There is no
``else`` here to inherit from.

**Dependency direction.** This module reads the wire (:mod:`hms_errors`,
:mod:`bambu_mqtt`, :mod:`plate_occupancy`) and the store's pure rule table
(:mod:`printer_incidents`). It NEVER imports ``spool_recovery`` or ``printer_manager``
— the printer's live state, the job's layer, "is a driver live" and "is a terminal being handled"
all arrive as a :class:`Context`, so the closers depend on the rule and the rule depends on nothing
that closes. ``printer_incidents`` must never import this module back.

**One lane ends rows of this family OUTSIDE the table, by declaration:**
``service_hold.exit`` closes the ``declared`` hold it opened. A declared hold ends ONLY
through the verb that opened it; giving the table a cell that could close one would be
a second way to end it, which is precisely what the class exists to refuse. (The
2026-09-04 plate-vision first-trip re-check in ``farm_policy`` was the other declared
lane, deleted on 2026-09-24. Since 2026-09-29 the plate check is a ``job_pause`` episode
row: its driver closes it on its own outcomes, as the AMS driver does, and otherwise its
job's resume or terminal closes it here.)

It is named in the AST pin's allowlist (``test_code_quality`` /
``test_incident_resolution``), so a second closer appearing anywhere fails the suite
rather than quietly becoming another owner.

**One verdict beside the close table: is a RESUME owed** (:func:`resume_owed`; doctrine rule 1, and
Raymond 2026-10-11: "will i have to click resume on a filament swap after this is shipped? the answer
should be no"). A row the farm holds over a PAUSEd print whose toolhead the printer or a person put
back — a person's load after a physical fault, a toolhead that reads fed again after an empty-toolhead
hold — is resumed ONCE by the farm, through the one resume body (``spool_recovery.resume_paused_print``).
Which classes may, and on what evidence, is a second total table keyed by the same resolution class
(:data:`_RESUME_OWED`), so the class literals still never leave this module; the guards every class
shares (maintenance mode, the plate check, a runout row, the once) sit over it, so the spawner and the
hold chip (``hold_chip``) ask ONE question.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from backend.app.models.printer_incident import (
    AMS_FAULT_KINDS,
    KIND_PLATE_VISION,
    KIND_RUNOUT,
    RESOLUTION_DECLARED,
    RESOLUTION_JOB_PAUSE,
    RESOLUTION_OPERATOR,
    RESOLUTION_REPAIR,
    RESOLUTION_TOOLHEAD,
    RESOLUTION_WIRE,
    RESOLVE_JOB_ENDED_UNSEEN,
    RESOLVE_OBSERVED_RUNNING,
    RESOLVE_OPERATOR,
    RESOLVE_PLATE_REFUSED,
    RESOLVE_REARM,
    RESOLVE_REPAIR_COMPLETED,
    RESOLVE_REPAIR_OBSERVED,
    RESOLVE_TERMINAL,
    RESOLVE_WIRE_CLEAR,
    STATUS_RECOVERING,
    PrinterIncident,
)
from backend.app.services import printer_incidents
from backend.app.services.bambu_mqtt import PrinterState, ams_mid_filament_change
from backend.app.services.command_outcome import Answer
from backend.app.services.feed_state import FeedReading
from backend.app.services.hms_errors import (
    fault_tokens,
    fingerprint_tokens,
    live_candidates,
    plate_check_paused,
    power_loss_prompt_standing,
)
from backend.app.services.job_identity import is_held_job, same_job
from backend.app.services.plate_occupancy import plate_occupancy
from backend.app.services.tray_fields import valid_feeder

logger = logging.getLogger(__name__)


# Every occasion on which somebody asks the question. They are OCCASIONS, not events:
# ``sweep_tick`` and ``startup`` are the farm looking, the other four are the farm
# being told. The distinction is load-bearing: a "looking" occasion reads a LIVE report
# (the sweep's own gate) and stands while the job's terminal is being handled
# (:attr:`Context.terminal_in_flight`) — the terminal is the event that decides, and a
# level read in the moment between the printer's report and its handling must never
# pre-empt it (the open fault it classifies, the plate it refuses).
#
# ``new_fault`` is asked by the AMS entry gate (``spool_recovery.on_ams_fault``) of the
# open AMS row, BEFORE it decides whether that row swallows the fault now on the wire.
# Its meaning is a LEVEL comparison, never an appearance edge (``hms_edges`` owns those,
# and an edge would not survive a restart): none of the row's OWN fault tokens stands
# on the live wire any more, and a DIFFERENT actionable fault stands now
# (:func:`_fault_is_new`).
Occasion = Literal["running_edge", "job_terminal", "sweep_tick", "startup", "plate_cleared", "new_fault"]


@dataclass(frozen=True)
class TerminalEvent:
    """A print ended. What the FIRMWARE said, plus the two facts that qualify it.

    ``status`` is ``main.on_print_complete``'s ``_raw_status`` — captured before the
    operator-UI rewrite, so it is the printer's own word and not the farm's reading of
    it. ``eject`` is that callback's own eject-job flag (a sweep is filament-less and
    can never be repair evidence). ``job_id`` is the firmware ``subtask_id``, which is
    what binds a terminal to the JOB a fault interrupted rather than to the printer.

    ``plate_refused`` is the terminal outcome's own answer (``terminal_outcome``, built
    before any closer runs): this terminal REFUSED the plate a plate check paused, so a
    human-clear gate and a page follow. It changes only the ``job_pause`` close's SOURCE
    (``plate_refused`` rather than ``terminal``), so the outcome ledger can tell the
    farm's own stop-and-retry from a refusal handed to a human.

    ``printed_unfed`` is the terminal payload's own measurement (``job_extent.JobExtent.
    printed_unfed``, read by ``main.on_print_complete`` off the same parse the outcome was
    built from): the job printed layers with NOTHING fed — a FINISH over an empty toolhead
    (011-H2S and 014-H2S, 2026-10-09). ``False`` when nothing measured it (an older payload,
    the downtime reconcile's synthesis). Read only by the ``repair`` class's completed arm.

    ``last_layer`` is the payload's own peak (``job_extent.JobExtent.last_layer_num``): how far the
    job got. The ``repair`` class's completed arm reads it against the row's ``fault_layer`` — a job
    that completed having printed PAST the layer the fault stopped it at ran filament through the
    path after the fault. ``0`` when nothing measured it.
    """

    status: str
    eject: bool
    job_id: str | None
    plate_refused: bool = False
    printed_unfed: bool = False
    last_layer: int = 0


@dataclass(frozen=True)
class ClearedEvent:
    """A human acted on the plate. ``recover`` distinguishes the two verbs.

    ``False`` = the routine clear-plate ack ("the bed is empty"); ``True`` = Recover
    ("I inspected this machine"), which is the stronger statement and the only one
    that may end a ``repair`` row.
    """

    recover: bool


@dataclass(frozen=True)
class Context:
    """Everything a verdict may read, passed IN so this module imports no closer.

    ``state`` is the printer's live status (``None`` when it is not speaking to us —
    a cached read during a disconnect is a memory, not evidence). ``driver_live`` is
    ``printer_incidents.driver_live`` for this printer — the ONE liveness store — asked by the
    caller and passed in, so a verdict is a pure function of what it is handed.

    ``restart_owed`` is what the row's STEP LEDGER says (``spool_recovery``'s evidence log,
    read by the startup rearm): the recovery driver sent its restart stop on this still-open
    row — the job is over by the farm's own act and the restart continuation owes the row
    what is left of it: the unload (the stalled filament may still be in the tube), the park
    and the close (operator ruling 2026-09-29). Only the ``startup`` occasion reads it.

    ``command_pending`` is the row's step ledger too, the same precedent (K11, 011/014-H2S
    2026-10-09/10): the farm's last motion command on this row has not run — the AMS
    acknowledged it and has not moved, or nobody read its answer
    (``printer_incidents.pending_command``, the open-row projection; at startup the hydrated
    log). An accepted pull-back ran ON ITS OWN ~4.5 min after the send, with the print still
    paused, and emptied the toolhead; while one is queued the row is the farm's, because the
    consequence of its command running is the farm's to undo (the refill). So :func:`resolve`
    stands EVERY cell while it holds except ``job_terminal`` (a job that is over has no toolhead
    left to print air with) and ``plate_cleared`` (a person declared the printer fixed —
    :data:`_PENDING_EXEMPT`).

    ``feed`` is the printer's toolhead FEED STATE (``feed_state.FeedReading``, the client's
    ``feed()``; invariant 16 — the one answer to "is filament reaching the nozzle"), read by the
    caller with the state so the verdicts that ask whether the print FEEDS (:func:`feeding_through_path`,
    the ``toolhead`` class's cells, :func:`resume_owed`) read it off the phase, never off a frame.
    ``None`` — no client, or a caller whose occasion reads no feed (``pause_recovery``'s
    ``plate_cleared``, whose cells read only the verb) — and nothing then reads as fed.

    ``observed_load_at`` is the RECORD's last load on this printer that NOBODY commanded
    (``printer_action.observed_load_at``: the feed state's landing that answered no farm or operator
    load — the printer's own backup switch, a person's hand at the AMS), naive UTC; ``None`` when the
    record holds none. Durable (2026-10-11): it survives the restart that wiped the motion ledger's
    copy and stranded 001-H2S incident 574. Read by the ``repair`` class's repair
    evidence; ``None`` for a caller whose occasion reads none.

    ``job_layer`` is the tracked job's current layer (``BambuMQTTClient.job_peaks().layer_num`` — a
    predecessor's republished layer is never this job's), ``None`` with no client. The ``repair``
    class reads it against the row's ``fault_layer``: a layer printed FED past the one the fault
    stopped the job at is the print running through the path after the fault
    (:func:`_printed_past_fault`) — the job's own record, which a restart does not empty.

    ``terminal_in_flight`` is ``printer_manager.terminal_in_flight`` for this printer: the printer
    reported a job's end and that terminal is still being handled (``main.on_print_complete`` has
    not returned). The terminal decides every row it answers — and it reads the open holds BEFORE it
    closes them, so the open fault classifies the stop and the plate check's refusal reaches the
    plate — so a LOOKING occasion stands while one is in flight (:func:`resolve`).

    ``job_ran`` — the row's OWN job was seen RUNNING since the row opened (:func:`own_job_running`,
    recorded by the caller at the RUNNING edge: ``spool_recovery``'s process memory of an edge, which a
    restart empties). Read only by the ``wire`` class's new-fault cell, whose durable arms still answer
    after a restart.
    """

    state: PrinterState | None
    driver_live: bool
    terminal: TerminalEvent | None = None
    cleared: ClearedEvent | None = None
    restart_owed: bool = False
    command_pending: bool = False
    feed: FeedReading | None = None
    observed_load_at: datetime | None = None
    job_layer: int | None = None
    terminal_in_flight: bool = False
    job_ran: bool = False


@dataclass(frozen=True)
class Verdict:
    """Close or stand, and why.

    ``evidence`` is a SENTENCE in both directions: the closers log it verbatim, so a
    close line names what decided it and a stand line names what did not. No verdict waits
    out a window (2026-10-11): what used to be the sweep's 120 s dwell is the printer's own
    report — a LIVE one (the sweep's gate), read only while no terminal is being handled
    (:attr:`Context.terminal_in_flight`).
    """

    close: bool
    source: str | None = None
    evidence: str = ""


# The two motion evidences the ``repair`` class has always admitted, the completed arm
# (2026-09-17) and the new-fault arm (2026-09-25). Named constants rather than prose so
# a caller can ask WHICH one answered without matching on a sentence (the self-heal arm
# asks for the load).
_REPAIR_EVIDENCE_LOAD = "load completed after the fault"
_REPAIR_EVIDENCE_RUNNING = "print feeding through the path"
_REPAIR_NOT_FEEDING = (
    "a print is RUNNING with no AMS feeder at the active extruder — a print on air, or off the external "
    "spool, moves no filament through the path"
)
_REPAIR_EVIDENCE_COMPLETED = "print completed through the path after the fault"
_REPAIR_EVIDENCE_NEW_FAULT = "a print ran through the path after the fault, and a different fault stands now"
# The ``wire`` class's new-fault close (R27): the print ran on past the row's fault, by the job's own layer or
# by the farm's resume reaching the printer, and a different fault stands now.
_WIRE_EVIDENCE_NEW_FAULT_PAST = "the print printed a layer past the fault, and a different fault stands now"
_WIRE_EVIDENCE_NEW_FAULT_RESUMED = "the job resumed past this hold, and a different fault stands now"


def path_quiet(state: PrinterState | None) -> bool:
    """Is the filament path quiet RIGHT NOW — no actionable fault, no wedged AMS?

    The close-time guard, whole-printer and evaluated at VERDICT time only. Never at
    stamp time: evidence is not a latch, and a printer that moved filament an hour ago
    and is faulted now is faulted. Whole-printer rather than per-code because incident
    #60 carried the pair ``0700_8006`` + ``0700_0006`` — a test on the incident's own
    code would have closed the hold with its sibling still standing (doctrine
    invariant 1: one taxonomy, never a new HMS frozenset).

    The wedge test is separate from the fault test because a wedge OUTLIVES the code
    that caused it: the wire can read fault-free while the AMS is still mid-change.
    """
    return not live_candidates(state) and not ams_mid_filament_change(state)


def running_without_eject(state: PrinterState | None, printer_id: int) -> bool:
    """Is a PRINT running on this printer right now — RUNNING, and no eject owns it?

    THE one reading of "a print is running" — NOT "a print is feeding": a print resumed
    onto an empty toolhead runs on air (:func:`feeding_through_path` is that question, and
    composes this one). It serves the ``job_pause`` class's resume evidence (the plate
    check's answer is the paused job printing again; a refill is never its business) and the
    ``toolhead`` class's fed print. The eject exclusion is not incidental: a sweep is
    filament-LESS and is not the job a hold paused, so a toolhead crossing the plate says
    nothing about either.
    """
    return _live_state(state).upper() == "RUNNING" and plate_occupancy.eject_identity(printer_id) is None


def feeding_through_path(state: PrinterState | None, printer_id: int, feed: FeedReading | None) -> bool:
    """Is a print FEEDING through the AMS path right now — :func:`running_without_eject`, AND
    the toolhead's feed state FED or TAIL (``feed_state``, invariant 16) from an AMS feeder?

    THE ``repair`` class's reading of a print feeding right now (K12, 2026-10-10): its (b) cell
    (:func:`_repair_motion`), paired there with the job's own record of having printed past the fault
    (:func:`_printed_past_fault`). 011-H2S and 014-H2S, 2026-10-09/10:
    resumed onto an EMPTY toolhead, the print read RUNNING and laid down air — RUNNING alone
    had been "a print ran through the path".

    The feed state is the ACTIVE extruder's, never "any extruder": on a dual nozzle an empty
    active hotend beside a loaded deputy prints air, and the deputy's filament is not feeding
    the print. Its effective feeder must be a real AMS tray (``tray_fields.valid_feeder``):
    the EXTERNAL holder is not feeding by this question — the class is AMS-side physical
    holds only (``RESOLVES_ON[(physical, False)]``; a holder fault is the ``wire`` class), and
    the external holder bypasses the AMS path entirely, so a print fed from it says nothing
    about the path the hold is for. (The ``toolhead`` class counts it as fed — its question
    is only "is it printing air", :func:`_toolhead_fed`.)
    """
    return (
        running_without_eject(state, printer_id)
        and feed is not None
        and feed.toolhead_fed
        and valid_feeder(feed.feeder) is not None
    )


def driver_owns(row: PrinterIncident, *, live: bool) -> bool:
    """Is a recovery driver the owner of this row's outcome?

    THE one spelling, replacing two that had drifted: the ``status == recovering``
    test the sweep and the startup rearm used, and the live-task test the running-edge
    closer used. ``recovering`` is a PROMISE that a task is acting and ``live`` is the
    task slot answering whether one actually is; either makes the outcome somebody
    else's to write, and closing a row from under a driver destroys entry exclusivity
    (006-H2S 2026-09-04: the re-PAUSE found no open incident and spawned a SECOND
    driver onto one AMS).

    **Class-aware since 2026-09-29.** For a ``job_pause`` row the answer is ``live``
    ALONE. A ``recovering`` plate-check episode is a promise only a LIVE driver keeps:
    its re-entry (``pause_recovery``) runs only while the printer still holds the job
    PAUSEd, so a row whose job ended while no driver lived has nobody left to keep it —
    and read as owned it would be skipped by the sweep forever, blocking dispatch on its
    printer. Every other class keeps the promise reading: an AMS ``recovering`` row with
    no live task is the startup re-entry's to drive (``spool_recovery``), never the
    sweep's to close.

    ``printer_incidents.driver_live`` is the store and it does not decide — callers
    pass its answer in as ``live``.
    """
    if _resolution_of(row) == RESOLUTION_JOB_PAUSE:
        return live
    return row.status == STATUS_RECOVERING or live


class HoldRow(Protocol):
    """The row facts the resolution class and the resume verdict read. A :class:`PrinterIncident`
    satisfies it, and so does the hold chip's view of the store's cached row (``hold_chip``): the
    chip asks the spawner's own question, DB-free, on every status broadcast."""

    @property
    def id(self) -> int: ...
    @property
    def printer_id(self) -> int: ...
    @property
    def kind(self) -> str: ...
    @property
    def code(self) -> str: ...
    @property
    def job_id(self) -> str | None: ...


def _resolution_of(row: HoldRow) -> str:
    """The row's resolution class, read from the store's pure rule — the one read
    :func:`resolve` and :func:`driver_owns` share."""
    return printer_incidents.resolution_class(row.kind, external=printer_incidents.row_external(row))


def _live_state(state: PrinterState | None) -> str:
    """The printer's reported state as a plain string ("" when it has not reported)."""
    return (getattr(state, "state", None) or "") if state is not None else ""


def _reporting(state: PrinterState | None) -> bool:
    """Is the printer saying anything at all? ``""`` / ``UNKNOWN`` are not evidence."""
    return _live_state(state).upper() not in ("", "UNKNOWN")


Handler = Callable[[PrinterIncident, Context], Verdict]


def _stand(reason: str) -> Handler:
    """A cell that never closes, carrying the sentence its closer logs."""

    def _handler(_row: PrinterIncident, _ctx: Context) -> Verdict:
        return Verdict(close=False, evidence=reason)

    return _handler


# --- the ``wire`` class -------------------------------------------------------------
#
# R27 (2026-10-12): a wire-class AMS row (a jam, a runout, an external-holder physical prompt) ends over
# a RUNNING print only once the job has printed a layer past the row's ``fault_layer`` — or at the job's
# terminal. Production closed it over RUNNING after the sweep's 120 s dwell (``_HOLD_OVER_DWELL_S``, 337f4954;
# its running edge and its refill resume's confirm closed at once); the rewrite deleted the dwell, so a
# fault that flapped around the RUNNING edge closed its row, re-opened as a NEW row and was owed a fresh
# farm resume (the 574 reopen loop). The layer advance is the event that replaces the dwell: a firmware
# change, a re-pause or a flap never advances a layer, and a print running on past the fault does. The once
# stays per ROW (``resume_spent``): a genuine new fault after the advance opens a new row with its own.


def own_job_running(row: PrinterIncident, ctx: Context) -> bool:
    """Is the row's OWN job RUNNING right now, as its own (no recovery driver live, no eject owning the
    printer, the row's job or an id-less one)? THE sighting a RUNNING edge records for the ``wire`` class's
    new-fault cell (:attr:`Context.job_ran`): the job resumed past the hold, a person's touchscreen resume
    included, which the record never sees. A driver's own lever resume is its procedure, not the hold
    answered (the running edge stands aside for it on the same rule)."""
    return (
        not ctx.driver_live
        and running_without_eject(ctx.state, row.printer_id)
        and same_job(_live_job(ctx.state), row.job_id) != "other"
    )


def _wire_not_past_fault(row: PrinterIncident, ctx: Context) -> str | None:
    """May a wire-class row close over the printer's report now (R27)? ``None`` when it may, else the
    sentence why it stands.

    Only an AMS row over a RUNNING PRINT (:func:`running_without_eject`) of its own job waits for the layer
    advance (:func:`_job_printed_past_fault`; an id-less job is read as the row's own, compared against
    ``fault_layer`` all the same — a layer past it is past it). Every other reading keeps its cell's own
    rule: a power-loss row is not an AMS fault (its hold ends on the RUNNING its resume produces), and a
    printer running ANOTHER job, reporting the job over, idle, or under an eject sweep is not "over RUNNING"
    of the interrupted job (that job is over, which is the terminal's statement too).

    A row with NO recorded ``fault_layer`` (opened by a build before the column, or with no client to read
    the layer from) cannot measure the advance on its own job: it closes as production did over RUNNING,
    with no dwell. The AMS entry records one on every row it opens on a printer with a client (the layer's
    reader), so a new row takes this branch only when it had nothing to read the layer from."""
    if row.kind not in AMS_FAULT_KINDS or not running_without_eject(ctx.state, row.printer_id):
        return None
    if same_job(_live_job(ctx.state), row.job_id) == "other":
        return None  # another job runs: the interrupted one is over, which its terminal says too
    if _job_printed_past_fault(row, ctx, id_less_is_same=True):
        return None
    if row.fault_layer is None:
        return None
    return (
        f"the print is RUNNING but has not printed a layer fed past the fault yet (layer {ctx.job_layer}, "
        f"fault at layer {row.fault_layer}) — a flap or a re-pause never advances one"
    )


def _wire_running_edge(row: PrinterIncident, ctx: Context) -> Verdict:
    """A PAUSE->RUNNING edge closes a wire hold, whoever produced it — once the print has printed past
    the fault (:func:`_wire_not_past_fault`, R27).

    Screen, UI, auto-resume or the firmware recovering by itself — the breadth is the
    point, because a hold clearable only by the one path that set it strands every other resume.
    The edge usually lands on the fault's own layer: the row then stands, and the sweep closes it on the
    first layer printed past the fault (:func:`_wire_sweep_tick`), so a fault that flaps around this edge
    re-pauses into the SAME row, whose farm resume is already spent.

    It stands only while a recovery DRIVER is live: a RUNNING sample taken during a
    resume the driver ITSELF published (a release lever of the wedge ladder, or the
    swap round's resume, both read by ``_read_after``) is an intermediate reading, and the
    driver consumes it. The deferred edge is not lost — ``_run_recovery``'s handover
    re-reads the level once the slot is free, and that handover is why the test here
    is the LIVE TASK and not :func:`driver_owns`: a row still reading ``recovering``
    because its driver crashed (an R1 orphan) has no owner left, and must still close.
    """
    if ctx.driver_live:
        return Verdict(close=False, evidence="a recovery driver is live and owns the outcome; closer stands aside")
    not_past = _wire_not_past_fault(row, ctx)
    if not_past is not None:
        return Verdict(close=False, evidence=not_past)
    return Verdict(close=True, source=RESOLVE_OBSERVED_RUNNING, evidence="the printer is RUNNING again")


def _wire_job_terminal(_row: PrinterIncident, ctx: Context) -> Verdict:
    """A JOB HOLD cannot outlive the job. A terminal is as good a statement as the
    wire makes that the fault it interrupted is over.

    It stands aside while a recovery DRIVER is live — the running edge's test, for the
    running edge's reason (2026-09-23). A terminal the driver's OWN verb
    produced (a release lever that ended the print) is a reading of that procedure, and
    the driver must record it: its reader returns ``ended`` and the driver closes the row
    with its own source token (``printer_incidents.RESOLVE_DRIVER_ENDED``) and pages.
    Closing it here would free the row from under a task still writing outcomes for it —
    the 006-H2S 2026-09-04 shape, now at the terminal instead of the resume.

    The test is the LIVE TASK, not :func:`driver_owns`, exactly as at the running edge: a
    row still reading ``recovering`` because its driver died (an R1 orphan) has no owner
    left, and its job's terminal must still close it.
    """
    if ctx.driver_live:
        return Verdict(close=False, evidence="a recovery driver is live and owns the outcome; closer stands aside")
    return Verdict(close=True, source=RESOLVE_TERMINAL, evidence="the job it held reached a terminal")


def _hold_over(state: PrinterState | None) -> tuple[bool, str]:
    """Does the printer's LIVE STATE say a wire hold is over? ``(verdict, as reported)``.

    True only for a POSITIVE non-PAUSE state. ``""`` / ``UNKNOWN`` are not evidence of
    anything (the printer has not reported) and ``PAUSE`` is the hold itself. One
    reading, two occasions (the sweep and the startup rearm), so a restart and a
    running process can never disagree about what "the hold is over" means — before it
    was extracted the rearm was the only place that could answer it at all, which is
    why a hold whose fault cleared while the process was UP had no close path (001-H2S
    incident #60: escalated 01:25, clean and IDLE by 15:30, still holding at 16:00).
    """
    reported = _live_state(state)
    return bool(reported) and reported.upper() not in ("", "UNKNOWN", "PAUSE"), reported


def _wire_sweep_tick(row: PrinterIncident, ctx: Context) -> Verdict:
    """The wire lane's ladder: the printer's LIVE report (the sweep reads no other) of a positive
    non-PAUSE state AND zero actionable faults — the fault cleared on the printer's current session —
    and, over a RUNNING print, a layer printed past the fault (:func:`_wire_not_past_fault`, R27).

    No dwell (2026-10-11). What the old 120 s guarded against is events now: a reading in the instant
    a printer passes through a non-PAUSE state is answered by the report being live (a reconnect's
    cached re-broadcast is not evidence) and, over a running print, by the layer advance (a flap
    around the RUNNING edge never advances one); and while the printer's terminal is being handled the
    sweep stands (:func:`resolve`), because that terminal reads this very hold to classify the stop
    (the 2026-09-11 fault-stop requeue) before it closes it.
    """
    over, reported = _hold_over(ctx.state)
    if not over:
        return Verdict(close=False, evidence=f"state is {reported or 'unreported'}, not a positive non-PAUSE")
    if live_candidates(ctx.state):
        return Verdict(close=False, evidence="an actionable fault is still standing")
    not_past = _wire_not_past_fault(row, ctx)
    if not_past is not None:
        return Verdict(close=False, evidence=not_past)
    return Verdict(close=True, source=RESOLVE_WIRE_CLEAR, evidence="no actionable fault")


def _wire_startup(row: PrinterIncident, ctx: Context) -> Verdict:
    """The same ladder with no fault-liveness guard: a session start re-derives every wire fact
    from scratch, and a printer already running has answered.

    One reading of "not PAUSE" is not the hold ending: a row whose driver STOPPED the job
    to restart it (``ctx.restart_owed``) reads IDLE / FAILED because of that stop, while its
    continuation has not finished — closing it here hands the printer to the dispatcher with
    the stalled filament possibly still in the tube, or the stalled spool unparked. It
    stands, and the rearm re-enters the continuation (the row still reads ``recovering``)."""
    if ctx.restart_owed:
        return Verdict(
            close=False,
            evidence="the driver stopped this job to restart it and has not finished — "
            "the restart continuation re-enters",
        )
    over, reported = _hold_over(ctx.state)
    if not over:
        return Verdict(close=False, evidence=f"state is {reported or 'unreported'}, not a positive non-PAUSE")
    # Over a RUNNING print the first fresh report answers it only once the job printed past the fault
    # (R27) — the layer is the job's own record, so it reads the same after a restart as before one.
    not_past = _wire_not_past_fault(row, ctx)
    if not_past is not None:
        return Verdict(close=False, evidence=not_past)
    return Verdict(close=True, source=RESOLVE_REARM, evidence=f"printer is {reported} not PAUSE")


def _wire_new_fault(row: PrinterIncident, ctx: Context) -> Verdict:
    """A DIFFERENT fault after the print ran on past this row's: its own incident (R27).

    Held open past the RUNNING edge until the layer advance, a wire row is still open when a fault arrives
    that has nothing to do with it — a fault the row's flap protection must not swallow, because a swallowed
    fault gets no row, no driver and no resume of its own (production closed the row at its resume, so such
    a fault always opened its own). The row ends here when, and only when:

    * the fault is NEW (:func:`_fault_is_new`) — none of the row's own tokens stands. The row's own fault
      standing again is the flap the row stays open for, and a code standing beside the row's own is the
      same hold, better described (the entry gate's outrank test re-classifies the row if it is worse).
      Asked first: it is the sentence the entry's log needs for the commonest re-evaluation, the row's own
      fault still standing — a driver's re-pause on its own fault included;
    * no recovery driver is LIVE (the told-occasion ownership rule, :func:`_wire_running_edge`);
    * the print RAN after the fault: it printed a layer past ``fault_layer`` (:func:`_job_printed_past_fault`,
      the R27 event), or the job RESUMED past the hold: the farm's resume on this row REACHED the printer
      (the record, durable), or the row's own job was seen RUNNING since the row opened
      (:attr:`Context.job_ran`, a person's touchscreen resume included; process memory of the RUNNING
      edge, so after a restart the two durable arms answer). Both are the event production closed the row
      on. Without any, the new fault arrived INSIDE the hold (a jam raised while the operator refills a
      runout slot) and does not end it.
    """
    not_new = _fault_is_new(row, ctx.state)
    if not_new is not None:
        return Verdict(close=False, evidence=not_new)
    if ctx.driver_live:
        return Verdict(close=False, evidence="a recovery driver is live and owns the outcome; closer stands aside")
    if _job_printed_past_fault(row, ctx, id_less_is_same=True):
        return Verdict(close=True, source=RESOLVE_OBSERVED_RUNNING, evidence=_WIRE_EVIDENCE_NEW_FAULT_PAST)
    if ctx.job_ran or any(
        action.command == "resume" and action.answer == Answer.REACHED.value
        for action in printer_incidents.farm_actions(row.printer_id, row.id)
    ):
        return Verdict(close=True, source=RESOLVE_OBSERVED_RUNNING, evidence=_WIRE_EVIDENCE_NEW_FAULT_RESUMED)
    return Verdict(
        close=False,
        evidence="the printer has not run since the fault — the new fault arrived inside the hold, and the "
        "outrank test re-classifies the row if it is worse",
    )


# --- the ``repair`` class -----------------------------------------------------------


def _loaded_since(row: PrinterIncident, ctx: Context) -> bool:
    """Did a load NOBODY commanded land after this row opened (``Context.observed_load_at``, the
    record's ``observed_load`` — durable)? The operator's commonest repair: free the path by hand,
    then load a slot. The farm's own loads are farm actions, never evidence."""
    loaded_at = ctx.observed_load_at
    return loaded_at is not None and row.created_at is not None and loaded_at > row.created_at


def _job_printed_past_fault(row: PrinterIncident, ctx: Context, *, id_less_is_same: bool = False) -> bool:
    """Has the job printed a layer FED past the one the fault stopped it at? THE one reader of the
    layer advance after a fault, shared by the ``repair`` class (:func:`_printed_past_fault`, which adds
    the AMS-path question) and the ``wire`` class's close over a RUNNING print (:func:`_wire_not_past_fault`,
    R27). The job's OWN record, never a sighting held in memory: the row's ``fault_layer`` (recorded at
    the open, re-stamped at a re-classification) against the tracked job's layer (``Context.job_layer``),
    on the toolhead's feed state (invariant 16).

    * the row's own job: its layer is past ``fault_layer`` (a row with no recorded layer — opened
      before the column existed — proves nothing here);
    * another job on the printer (the interrupted one is over): it has printed a layer;
    * an id-less job either side (``job_identity`` ``unknown``): nothing — unless the caller reads it as
      the row's own job (``id_less_is_same``), comparing its layer against ``fault_layer`` all the same.

    FED: the feed state measured no layer printed with nothing fed in this job (``first_unfed_layer`` —
    a print resumed onto an empty toolhead advances its layers on air, K12). The printer must be
    reporting.
    """
    feed = ctx.feed
    layer = ctx.job_layer
    if feed is None or layer is None or not _reporting(ctx.state):
        return False
    if feed.first_unfed_layer is not None:
        return False
    identity = same_job(_live_job(ctx.state), row.job_id)
    if identity == "unknown" and id_less_is_same:
        identity = "same"
    match identity:
        case "same":
            fault_layer = getattr(row, "fault_layer", None)
            return fault_layer is not None and layer > fault_layer
        case "other":
            return layer >= 1
        case _:
            return False


def _printed_past_fault(row: PrinterIncident, ctx: Context) -> bool:
    """Has the print run filament through the AMS PATH since the fault — :func:`_job_printed_past_fault`,
    AND the toolhead's feeder — the effective one now, else the job's last feeder segment (an attach
    after a restart starts the segment log empty) — is a real AMS tray (the external holder bypasses the
    path this class holds)."""
    if not _job_printed_past_fault(row, ctx):
        return False
    feed = ctx.feed
    assert feed is not None  # _job_printed_past_fault answered True, which needs a feed reading
    segments = feed.tray_change_log
    feeder = feed.feeder if feed.feeder is not None else (segments[-1][0] if segments else None)
    return valid_feeder(feeder) is not None


def _ran_through_path_since(row: PrinterIncident, ctx: Context) -> bool:
    """Did filament run through the path AFTER this row opened? THE one spelling, read by the
    new-fault arm (:func:`_repair_new_fault`): a load nobody commanded landed (:func:`_loaded_since`),
    or the print ran a layer FED past the fault (:func:`_printed_past_fault`). Both are durable — the
    record, and the job's own layer against the row's — so a restart no longer leaves the row
    standing until this process happens to see the path run (2026-10-11)."""
    return _loaded_since(row, ctx) or _printed_past_fault(row, ctx)


def _repair_motion(row: PrinterIncident, ctx: Context) -> Verdict:
    """The two OBSERVED motion evidences, shared by the sweep and the startup rearm.

    Either is sufficient and both sit behind :func:`path_quiet` — whatever moved, the
    fault must not be standing now and the AMS must not be wedged.

    (a) a load NOBODY commanded landed after this row opened (:func:`_loaded_since`, the
        record's ``observed_load``): the operator's commonest repair is to free the path by hand
        and then load a slot — the farm's own loads are farm actions, never evidence;
    (b) a print is FEEDING through the same path (:func:`feeding_through_path`: RUNNING,
        no eject, the feed state FED or TAIL from an AMS feeder) AND it has printed a layer past
        the fault (:func:`_printed_past_fault`) — the layer advance is the event, so no reading
        waits out a dwell (2026-10-11). The eject exclusion is not incidental: a sweep is
        filament-LESS, so a toolhead crossing the plate says nothing about whether filament
        moves — and neither does a print on air (K12).

    The printer must also be REPORTING. A hold is not ended on a printer we cannot
    hear, however good the memory of its last load.
    """
    if not _reporting(ctx.state):
        return Verdict(close=False, evidence="the printer has not reported a state")
    if not path_quiet(ctx.state):
        return Verdict(close=False, evidence="the path is not quiet (a fault stands, or the AMS is mid-change)")
    if _loaded_since(row, ctx):
        if _held_job_paused(row, ctx) is None:
            # The row's own job sits PAUSEd on the repaired path: its resume — the farm's once
            # (:func:`resume_owed`), or a person's — is what answers the hold, and the print running
            # again closes it here. Closing on the load first would strand the PAUSE with nobody owed
            # a resume (the row is the resume's own guard), which is what a dwell used to paper over.
            return Verdict(
                close=False,
                evidence="the path was repaired and the job is PAUSEd on it — its resume answers the hold",
            )
        return Verdict(close=True, source=RESOLVE_REPAIR_OBSERVED, evidence=_REPAIR_EVIDENCE_LOAD)
    if feeding_through_path(ctx.state, row.printer_id, ctx.feed):
        if _printed_past_fault(row, ctx):
            return Verdict(close=True, source=RESOLVE_REPAIR_OBSERVED, evidence=_REPAIR_EVIDENCE_RUNNING)
        return Verdict(close=False, evidence="the print feeds but has not printed a layer past the fault yet")
    if running_without_eject(ctx.state, row.printer_id):
        return Verdict(close=False, evidence=_REPAIR_NOT_FEEDING)
    return Verdict(close=False, evidence="no repair evidence")


def _repair_sweep_tick(row: PrinterIncident, ctx: Context) -> Verdict:
    """The motion evidence, on the sweep's live report."""
    return _repair_motion(row, ctx)


def _repair_startup(row: PrinterIncident, ctx: Context) -> Verdict:
    """The same evidences on the session's first fresh report, closed under the rearm's own source
    token, because the edge itself was never witnessed by this session. ``tray_now`` naming a slot is
    a LEVEL a stuck-filament printer reports just as readily, so neither a feeder reading nor a
    RUNNING edge closes one: a load nobody commanded, or a layer printed fed past the fault.
    """
    verdict = _repair_motion(row, ctx)
    return Verdict(close=True, source=RESOLVE_REARM, evidence=verdict.evidence) if verdict.close else verdict


def _repair_job_terminal(row: PrinterIncident, ctx: Context) -> Verdict:
    """The 2026-09-17 evidence: the job the fault interrupted ran to ``completed``, FED.

    23 of the 40 physical rows in this farm's history ended as a hand repair plus a
    resume, and the resumed job then ran to completion (011-H2S 2026-09-17 sat escalated
    for a day with exactly that behind it) — strong positive evidence about the path, and
    until 2026-09-17 nothing counted it, because the completion landed inside the sweep's
    (since deleted) 120 s dwell and the terminal closer had no repair vocabulary at all.

    ``completed`` alone is NOT "filament fed to the end", and this docstring used to say it
    was. 011-H2S and 014-H2S, 2026-10-09: resumed onto an EMPTY toolhead, both jobs printed
    the rest of the plate on air and the printer said FINISH. What proves the path fed is the
    completion PLUS a payload that measured no layer printed with nothing fed
    (``TerminalEvent.printed_unfed``, ``job_extent.JobExtent.printed_unfed``).

    Six qualifications, each closing a way the reading could be wrong:

    * ``completed`` ONLY. A stop, a failure and an abort are the shape 38 pin: they are
      very often the operator stopping the very print the fault broke.
    * FED to the end: a FINISH whose payload measured layers printed with nothing fed is a
      print on air, never a statement about the path.
    * not an EJECT. A sweep is filament-less; its ``completed`` proves nothing.
    * the SAME job (``job_identity.same_job`` answers ``same``: the terminal must name
      one). The row blocks the DISPATCHER, not the touchscreen: a screen-started print
      completing on another slot must not launder a blocked shared path.
    * the job printed PAST the fault: the payload's own peak (``TerminalEvent.last_layer``) is beyond
      the row's ``fault_layer`` — or a load nobody commanded landed after the row opened
      (:func:`_loaded_since`). This excludes a pull-back timeout raised at end-of-print unload
      with no post-fault resume — there the job completes, but no layer ran after the fault. Both
      readings are durable (the payload and the row; the record), so a restart between the fault
      and the completion no longer costs the evidence (2026-10-11).
    * :func:`path_quiet` now. Evidence is not a latch.
    """
    terminal = ctx.terminal
    if terminal is None:
        return Verdict(close=False, evidence="no terminal event was supplied")
    if (terminal.status or "").lower() != "completed":
        return Verdict(close=False, evidence=f"terminal status is {terminal.status!r}, not 'completed'")
    if terminal.printed_unfed:
        return Verdict(
            close=False,
            evidence="the job completed but printed on air (layers with nothing fed) — not filament fed to the end",
        )
    if terminal.eject:
        return Verdict(close=False, evidence="the terminal is an eject sweep — filament-less, not repair evidence")
    # ``same`` only: an id-less terminal (``""`` / ``"0"``) or an id-less row is
    # ``unknown``, and repair evidence needs the terminal to NAME the interrupted job.
    if same_job(terminal.job_id, row.job_id) != "same":
        return Verdict(close=False, evidence="the completed job is not the one the fault interrupted")
    fault_layer = row.fault_layer
    printed_past = fault_layer is not None and terminal.last_layer > fault_layer
    if not printed_past and not _loaded_since(row, ctx):
        return Verdict(close=False, evidence="the job printed no layer past the fault, and nothing loaded since")
    if not path_quiet(ctx.state):
        return Verdict(close=False, evidence="the path is not quiet (a fault stands, or the AMS is mid-change)")
    return Verdict(close=True, source=RESOLVE_REPAIR_COMPLETED, evidence=_REPAIR_EVIDENCE_COMPLETED)


def _fault_is_new(row: PrinterIncident, state: PrinterState | None) -> str | None:
    """The ``new_fault`` occasion's LEVEL comparison: ``None`` when a DIFFERENT fault stands
    and none of the row's own does, else the sentence saying why the fault is not new.

    Both sides are the taxonomy's tokens over BOTH wire lanes — the live side
    ``fault_tokens(live_candidates(state))``, the row side the fingerprint its ``codes``
    were written from (``hms_errors.fingerprint_tokens``) — so the comparison is the same
    slot-qualified ``class:short[@ams-tray]`` identity the entry gate opens rows on. A
    level, never an edge: it reads the same after a restart as before one.
    """
    live = fault_tokens(live_candidates(state))
    if not live:
        return "no actionable fault stands on the wire"
    if fingerprint_tokens(row.codes) & live:
        return "the row's own fault still stands on the wire"
    return None


def _repair_new_fault(row: PrinterIncident, ctx: Context) -> Verdict:
    """A DIFFERENT fault arrived after a print ran through the path: the blockage this row
    holds for is over, and the new fault is its own incident.

    The defect this closes (2026-09-15/16, 002-H2S: three ``0700_8010`` jams swallowed
    behind ``0700_0011`` + ``0700_8004`` pull-back rows; 2026-09-21, 006-H2S: a
    ``0300_801E`` behind incident 289): the running edge is not repair evidence for this
    class, so the row outlived the resume that had in fact repaired the path, and the
    entry gate then refused every new fault on the printer as "already held" — and
    suppressed its raw alert. A print RAN through the path after the fault, which is the
    motion this class has always closed on.

    Three conditions, and nothing else:

    * no recovery driver is LIVE — the told-occasion cells' ownership rule
      (:func:`_wire_running_edge`). A repair row gets no driver of its own (a physical
      fault escalates at entry), but a jam row UPGRADED under its live driver does, for
      the moment the driver takes to hand over; closing it then would let the entry gate
      spawn a second driver onto one AMS (006-H2S 2026-09-04). The next evaluation after
      the handover answers it;
    * the fault is NEW (:func:`_fault_is_new`) — the row's own tokens no longer stand. A
      new code standing BESIDE the row's own is the same blockage, better described,
      and the entry gate's outrank test re-classifies the row if it is worse;
    * :func:`_ran_through_path_since` — a load nobody commanded since the row opened, or a layer
      printed FED past the row's ``fault_layer`` (a re-classification re-stamps it, so a driver's
      own lever resumes before an upgrade are not "after the fault"; a print on air is no evidence —
      K12). Durable, so a restart reads the same answer.
    """
    if ctx.driver_live:
        return Verdict(close=False, evidence="a recovery driver is live and owns the outcome; closer stands aside")
    not_new = _fault_is_new(row, ctx.state)
    if not_new is not None:
        return Verdict(close=False, evidence=not_new)
    if not _ran_through_path_since(row, ctx):
        return Verdict(
            close=False,
            evidence="no print ran through the path after the fault — the new fault may be the same blockage",
        )
    return Verdict(close=True, source=RESOLVE_REPAIR_OBSERVED, evidence=_REPAIR_EVIDENCE_NEW_FAULT)


def _repair_plate_cleared(_row: PrinterIncident, ctx: Context) -> Verdict:
    """RECOVER only. "An operator inspected the machine" is the third return-to-normal
    the repair class admits; a routine clear-plate says nothing about the filament path.
    """
    if ctx.cleared is not None and ctx.cleared.recover:
        return Verdict(close=True, source=RESOLVE_OPERATOR, evidence="the operator recovered the printer")
    return Verdict(close=False, evidence="a routine plate clear is not a statement about the filament path")


# --- the ``operator`` class ---------------------------------------------------------


def _operator_plate_cleared(_row: PrinterIncident, ctx: Context) -> Verdict:
    """Both verbs. The evidence a human produces for a lost Z datum is the part coming
    off the plate, and Recover is the stronger form of the same statement."""
    recovered = ctx.cleared is not None and ctx.cleared.recover
    return Verdict(
        close=True,
        source=RESOLVE_OPERATOR,
        evidence="the operator recovered the printer" if recovered else "the operator cleared the plate",
    )


# --- the ``job_pause`` class --------------------------------------------------------
#
# The printer paused ONE job at its own pre-print plate check. The answer is that job:
# resumed — its RUNNING ends the hold and the same job continues — or stopped — its
# terminal ends the hold, and the terminal's outcome decides the plate (a retry, or a
# refusal handed to the plate authority). Every cell is bound to the row's OWN job
# (``job_identity.is_held_job``), because another job's edge or terminal says nothing
# about this pause. While the farm's own episode driver acts (``recovering``, a live
# driver), the resume-edge cell stands aside for it like the wire cell does.
#
# ...and a job pause cannot outlive its job. When the farm never saw that job's terminal
# (a restart or a dropped session swallowed it and no reconcile synthesised one), the
# per-tick sweep closes the row on the printer's POSITIVE report that the job is over.

# The states in which a printer is positively holding NO job paused: the job it had ran
# to a terminal, or it is idle. ``PAUSE`` is this hold's own reading and the active
# states (``PREPARE`` / ``SLICING`` / ``RUNNING``) belong to a job; ``""`` / ``UNKNOWN``
# are the printer saying nothing at all.
_JOB_OVER_STATES: frozenset[str] = frozenset({"FINISH", "FAILED", "IDLE"})


def _live_job(state: PrinterState | None) -> str:
    """The printer's live ``subtask_id`` ("" when it names none or is not reporting)."""
    return ((getattr(state, "subtask_id", None) or "") if state is not None else "").strip()


def _job_pause_running_edge(row: PrinterIncident, ctx: Context) -> Verdict:
    """The paused job RUNNING again ends the hold — whoever resumed it.

    Stands aside while a recovery DRIVER is live, the wire cell's rule for the wire
    cell's reason (a RUNNING sample taken during a resume a driver published is its
    reading, not this hold's answer). And only for the row's OWN job.
    """
    if ctx.driver_live:
        return Verdict(close=False, evidence="a recovery driver is live and owns the outcome; closer stands aside")
    if not is_held_job(_live_job(ctx.state), row.job_id):
        return Verdict(close=False, evidence="the RUNNING job is not the one the printer paused")
    return Verdict(close=True, source=RESOLVE_OBSERVED_RUNNING, evidence="the paused job is RUNNING again")


def _job_pause_job_terminal(row: PrinterIncident, ctx: Context) -> Verdict:
    """The paused job's terminal ends the hold — the job it asked about is over.

    What happens to the PLATE is not this cell's question: the terminal's outcome
    (``terminal_outcome``, built BEFORE this closer runs) decided it, and this cell only
    records which answer the episode got, as its close source. ``plate_refused`` is the
    terminal that refused the plate — a human-clear gate and a page follow, so the row is
    the human's (``printer_incidents.outcome_of``); ``terminal`` is every other end,
    including the farm's own first-failure stop whose unit is requeued with no gate.
    """
    terminal = ctx.terminal
    if terminal is None:
        return Verdict(close=False, evidence="no terminal event was supplied")
    if terminal.eject:
        return Verdict(close=False, evidence="the terminal is an eject sweep — not the job the printer paused")
    if not is_held_job(terminal.job_id, row.job_id):
        return Verdict(close=False, evidence="the terminal is not the job the printer paused")
    if terminal.plate_refused:
        return Verdict(
            close=True,
            source=RESOLVE_PLATE_REFUSED,
            evidence="the paused job reached a terminal that refused the plate",
        )
    return Verdict(close=True, source=RESOLVE_TERMINAL, evidence="the paused job reached a terminal")


def _job_pause_running(row: PrinterIncident, ctx: Context) -> Verdict:
    """The two LOOKING occasions' evidence: a positive RUNNING of the paused job, with no
    eject owning the printer. A pause is this hold's NORMAL reading, so nothing short of
    the job printing again answers it here."""
    if not running_without_eject(ctx.state, row.printer_id):
        return Verdict(close=False, evidence="the paused job is not RUNNING")
    if not is_held_job(_live_job(ctx.state), row.job_id):
        return Verdict(close=False, evidence="the RUNNING job is not the one the printer paused")
    return Verdict(close=True, source=RESOLVE_OBSERVED_RUNNING, evidence="the paused job is RUNNING again")


def _job_pause_ended_unseen(row: PrinterIncident, ctx: Context) -> Verdict | None:
    """The printer POSITIVELY reports the paused job over — or None when it does not.

    Two readings, each a statement the printer makes, never an absence: a terminal or
    idle state (:data:`_JOB_OVER_STATES`), or a DIFFERENT job's subtask id on a printer
    that is reporting (a printer runs one job, so another one means this one ended). A
    pause of the same job, a silent printer and a disconnected one say nothing.

    No plate gate follows from this close: the farm never saw the job end, so it holds no
    refusal to record — and the printer's own plate check runs again at the next job's
    start, which is exactly the check that raised this hold.
    """
    live = _live_state(ctx.state).upper()
    if live in _JOB_OVER_STATES:
        return Verdict(
            close=True,
            source=RESOLVE_JOB_ENDED_UNSEEN,
            evidence=f"the printer reports {live} — the paused job is over",
        )
    job = _live_job(ctx.state)
    if _reporting(ctx.state) and job and not is_held_job(job, row.job_id):
        return Verdict(
            close=True,
            source=RESOLVE_JOB_ENDED_UNSEEN,
            evidence="the printer is on another job — the paused job is over",
        )
    return None


def _job_pause_sweep_tick(row: PrinterIncident, ctx: Context) -> Verdict:
    """The paused job printing again, or the printer's positive report that it is over.

    The edge closer and the terminal closer are the first responders; this catches what
    they missed: a reconnect straight into RUNNING, and a terminal the farm never saw. The
    terminal stays first by EVENT, never by a dwell: while the printer's terminal is being
    handled the sweep stands (:attr:`Context.terminal_in_flight`), because that terminal is
    the one that carries the ``plate_refused`` verdict to the plate authority.
    """
    verdict = _job_pause_running(row, ctx)
    if verdict.close:
        return verdict
    ended = _job_pause_ended_unseen(row, ctx)
    return ended if ended is not None else verdict


def _job_pause_startup(row: PrinterIncident, ctx: Context) -> Verdict:
    """The same RUNNING evidence, under the rearm's own token — the resume itself was never
    witnessed by this session.

    Deliberately NOT the ended-unseen reading: a job stopped while the farm was down is
    answered by the downtime reconcile's synthesised terminal, which classifies it
    ``plate_refused`` only while this row is still OPEN — closing it here first would
    lose the refused plate's gate."""
    verdict = _job_pause_running(row, ctx)
    return Verdict(close=True, source=RESOLVE_REARM, evidence=verdict.evidence) if verdict.close else verdict


# --- the ``toolhead`` class (K10, 2026-10-10) -----------------------------------------
#
# The paused job's ACTIVE extruder read EMPTY and the farm took it on (``spool_recovery``'s refill
# of a printer no AMS row held): the hold is "this job would print air". So it ends on a print
# running FED — never on a RUNNING edge alone, because a screen resume onto an empty toolhead runs
# and prints air, which is the hold itself (T4) — on its job's end, or on Recover. "Fed" is the
# toolhead's feed state (``Context.feed``, invariant 16: the ACTIVE extruder's phase; an empty
# active nozzle beside a loaded one prints air).


def _toolhead_fed(feed: FeedReading | None) -> bool:
    """Does the toolhead's feed state read FED or TAIL (``feed_state``, invariant 16) — from an AMS
    feeder or the external holder alike: this class's question is only "is it printing air"."""
    return feed is not None and feed.toolhead_fed


def _toolhead_running_edge(_row: PrinterIncident, ctx: Context) -> Verdict:
    """A RUNNING edge with the toolhead FED ends the hold, whoever resumed. It stands while a
    driver is live (the refill driver's own resume is its reading — the wire cell's rule — and the
    driver closes its own row), and on a print running with nothing fed (the hold itself)."""
    if ctx.driver_live:
        return Verdict(close=False, evidence="a recovery driver is live and owns the outcome; closer stands aside")
    if not _toolhead_fed(ctx.feed):
        return Verdict(close=False, evidence="the print is RUNNING with nothing fed — a print on air is the hold")
    return Verdict(close=True, source=RESOLVE_OBSERVED_RUNNING, evidence="the printer is RUNNING with the toolhead fed")


def _toolhead_job_terminal(_row: PrinterIncident, ctx: Context) -> Verdict:
    """The job it held is over: there is no paused job left to print air. Stands while a driver is
    live, the wire cell's rule (a terminal inside the refill driver's window is its takeover)."""
    if ctx.driver_live:
        return Verdict(close=False, evidence="a recovery driver is live and owns the outcome; closer stands aside")
    return Verdict(close=True, source=RESOLVE_TERMINAL, evidence="the job it held reached a terminal")


def _toolhead_fed_print(row: PrinterIncident, ctx: Context) -> Verdict:
    """The looking occasions' positive evidence: a print RUNNING (no eject owning the printer — a
    sweep is filament-less and is not the job) with the toolhead fed."""
    if not running_without_eject(ctx.state, row.printer_id):
        return Verdict(close=False, evidence="no print is running")
    if not _toolhead_fed(ctx.feed):
        return Verdict(close=False, evidence="the print is RUNNING with nothing fed — a print on air is the hold")
    return Verdict(close=True, source=RESOLVE_OBSERVED_RUNNING, evidence="a print is running with the toolhead fed")


def _toolhead_sweep_tick(row: PrinterIncident, ctx: Context) -> Verdict:
    """A print running fed, or the printer's POSITIVE report that the job is over (a terminal the
    farm never saw — the one being handled stands the sweep aside, :func:`resolve`). A toolhead
    hold cannot outlive its job."""
    verdict = _toolhead_fed_print(row, ctx)
    if verdict.close:
        return verdict
    live = _live_state(ctx.state).upper()
    if live in _JOB_OVER_STATES:
        return Verdict(
            close=True,
            source=RESOLVE_JOB_ENDED_UNSEEN,
            evidence=f"the printer reports {live} — the job it held is over",
        )
    return verdict


def _toolhead_startup(row: PrinterIncident, ctx: Context) -> Verdict:
    """The same evidence, under the rearm's own token: a print running fed, or a job over. A PAUSEd
    printer is the hold — the row's driver is re-entered (``spool_recovery``)."""
    verdict = _toolhead_fed_print(row, ctx)
    if verdict.close:
        return Verdict(close=True, source=RESOLVE_REARM, evidence=verdict.evidence)
    live = _live_state(ctx.state).upper()
    if live in _JOB_OVER_STATES:
        return Verdict(close=True, source=RESOLVE_REARM, evidence=f"printer is {live} — the job it held is over")
    return verdict


def _toolhead_plate_cleared(_row: PrinterIncident, ctx: Context) -> Verdict:
    """RECOVER only — "an operator inspected this machine" is a statement about its toolhead; a
    routine clear-plate is not."""
    if ctx.cleared is not None and ctx.cleared.recover:
        return Verdict(close=True, source=RESOLVE_OPERATOR, evidence="the operator recovered the printer")
    return Verdict(close=False, evidence="a routine plate clear is not a statement about the toolhead")


# --- the table ----------------------------------------------------------------------

# EXPLICIT, every cell, no default. A missing key raises ``KeyError`` at the call —
# which is the whole point: a fifth resolution class must be given its own row here
# rather than inheriting the wire's evidence from an ``else`` it was never considered
# for. The ``declared`` class is the proof that the failure mode is real.
_TABLE: dict[tuple[str, Occasion], Handler] = {
    (RESOLUTION_WIRE, "running_edge"): _wire_running_edge,
    (RESOLUTION_WIRE, "job_terminal"): _wire_job_terminal,
    (RESOLUTION_WIRE, "sweep_tick"): _wire_sweep_tick,
    (RESOLUTION_WIRE, "startup"): _wire_startup,
    (RESOLUTION_WIRE, "plate_cleared"): _stand("a runout hold is not answered by somebody clearing a plate"),
    # A wire hold (a jam, a runout, an external-holder physical prompt) stays open over a RUNNING
    # print until the layer advance (R27), so a different fault after the print ran on is its own
    # incident (:func:`_wire_new_fault`); one that arrived INSIDE the hold (a jam raised while the
    # operator refills a runout slot), or a live driver's reading, ends nothing. What the new fault may
    # do there is RE-CLASSIFY the row, and the entry gate's outrank test (``spool_recovery._outranks``)
    # already owns that — a runout must never be routed into the swap machine (doctrine invariant 9).
    (RESOLUTION_WIRE, "new_fault"): _wire_new_fault,
    (RESOLUTION_REPAIR, "running_edge"): _stand(
        "a RUNNING edge is not repair evidence — an eject sweep makes one and moves no filament; "
        "the sweep closes it once the print has printed a layer fed past the fault"
    ),
    (RESOLUTION_REPAIR, "job_terminal"): _repair_job_terminal,
    (RESOLUTION_REPAIR, "sweep_tick"): _repair_sweep_tick,
    (RESOLUTION_REPAIR, "startup"): _repair_startup,
    (RESOLUTION_REPAIR, "plate_cleared"): _repair_plate_cleared,
    (RESOLUTION_REPAIR, "new_fault"): _repair_new_fault,
    (RESOLUTION_OPERATOR, "running_edge"): _stand("a RUNNING edge is not a human clearing a plate"),
    (RESOLUTION_OPERATOR, "job_terminal"): _stand(
        "a part on the plate after a reboot is not answered by a job ending — only a human clearing it is"
    ),
    (RESOLUTION_OPERATOR, "sweep_tick"): _stand(
        "a part on the plate and a lost Z datum are facts no HMS list reports — 'clean and idle' is "
        "this hold's NORMAL reading"
    ),
    (RESOLUTION_OPERATOR, "startup"): _stand("a restart is not a human clearing a plate"),
    (RESOLUTION_OPERATOR, "plate_cleared"): _operator_plate_cleared,
    # A part on the plate and a lost Z datum are facts about the PLATE and the motion
    # frame; an AMS fault appearing says nothing about either. (The AMS entry gate asks
    # this occasion only of an AMS-kind row, so this cell is the totality statement.)
    (RESOLUTION_OPERATOR, "new_fault"): _stand(
        "a part on the plate and a lost Z datum are not answered by an AMS fault appearing — only a human "
        "clearing the plate is"
    ),
    (RESOLUTION_JOB_PAUSE, "running_edge"): _job_pause_running_edge,
    (RESOLUTION_JOB_PAUSE, "job_terminal"): _job_pause_job_terminal,
    (RESOLUTION_JOB_PAUSE, "sweep_tick"): _job_pause_sweep_tick,
    (RESOLUTION_JOB_PAUSE, "startup"): _job_pause_startup,
    (RESOLUTION_JOB_PAUSE, "plate_cleared"): _stand(
        "the printer paused a JOB — the answer is resuming or stopping it, and the plate "
        "question is the plate authority's"
    ),
    # The printer paused ONE job to ask a human about its plate; the answer is that job
    # resumed or stopped. An AMS fault appearing answers neither — and while this row
    # stands the AMS entry opens no incident at all (``printer_incidents.job_pause_held``),
    # because every act the recovery machine owns ends in a resume onto the refused plate.
    (RESOLUTION_JOB_PAUSE, "new_fault"): _stand(
        "the printer paused a JOB for a human — an AMS fault appearing neither resumes nor stops it"
    ),
    (RESOLUTION_DECLARED, "running_edge"): _stand(
        "an operator may start a print from the screen WHILE the printer is held — that is what "
        "maintenance mode keeps legal"
    ),
    (RESOLUTION_DECLARED, "job_terminal"): _stand("no fault opened this hold, so no job ending closes it"),
    (RESOLUTION_DECLARED, "sweep_tick"): _stand("a held printer reads clean and idle — that is the hold, not its end"),
    (RESOLUTION_DECLARED, "startup"): _stand("a restart is not a human saying they have finished with the machine"),
    (RESOLUTION_DECLARED, "plate_cleared"): _stand(
        "'the plate is clear' and 'I am done working on this machine' are two statements, and only "
        "the second may release the automation"
    ),
    # Declared holds end ONLY through the verb that opened them (``service_hold.exit`` —
    # module docstring); a fault raised during the hold opens its own row beside it.
    (RESOLUTION_DECLARED, "new_fault"): _stand("no fault opened this hold, so no new fault closes it"),
    (RESOLUTION_TOOLHEAD, "running_edge"): _toolhead_running_edge,
    (RESOLUTION_TOOLHEAD, "job_terminal"): _toolhead_job_terminal,
    (RESOLUTION_TOOLHEAD, "sweep_tick"): _toolhead_sweep_tick,
    (RESOLUTION_TOOLHEAD, "startup"): _toolhead_startup,
    (RESOLUTION_TOOLHEAD, "plate_cleared"): _toolhead_plate_cleared,
    # An empty toolhead is not answered by an AMS fault appearing; the fault opens its own AMS row
    # beside it (the AMS entry asks this occasion only of an AMS row — this is the totality cell).
    (RESOLUTION_TOOLHEAD, "new_fault"): _stand(
        "an empty toolhead is not answered by an AMS fault appearing — the fault opens its own row"
    ),
}


# The LOOKING occasions — the farm reading levels rather than being told an event. While the
# printer's terminal is being handled (``Context.terminal_in_flight``) they stand: the terminal is
# the event that decides every row it answers, and it reads the open holds before it closes them.
_LOOKING: frozenset[Occasion] = frozenset({"sweep_tick", "startup"})

# The occasions a PENDING farm command does not stand (K11 + 2026-10-10 item 7a):
#
# * ``job_terminal`` — a job that is over has no toolhead left to print air with;
# * ``plate_cleared`` — Recover / Mark plate cleared: a PERSON declared the printer fixed, and the
#   rows their verb's class ends close on their word. What the queued command can still do is
#   answered by events, never by holding the row against them: the next Bambuddy resume refills the
#   toolhead before it publishes (T3), and a print that runs onto an empty toolhead is stopped on the
#   first layer it prints on air (T4, 2026-10-12).
#
# The same two are the only occasions a row carrying the farm's AIR stop the printer did not take is
# ended on (:func:`resolve`'s third guard): the job's end, or a person's Recover.
_PENDING_EXEMPT: frozenset[Occasion] = frozenset({"job_terminal", "plate_cleared"})


def resolve(row: PrinterIncident, occasion: Occasion, ctx: Context) -> Verdict:
    """Does ``occasion`` end ``row``? THE answer, for every closer.

    The row's resolution class is read ONCE, from the store's pure rule over the
    model's ``RESOLVES_ON`` table, and the class literal never leaves this module — the
    closers ask about an OCCASION and get a verdict, which is what lets a new evidence
    be added in one place instead of six.

    Raises ``KeyError`` for an unregistered class: a hold nobody has written a rule for
    must fail loudly, never fall through to the wire's evidence.

    ONE guard sits over the table, read after the lookup so an unregistered class still
    raises: while the farm's own motion command on the row has not run
    (``ctx.command_pending``), every occasion but the two in :data:`_PENDING_EXEMPT` stands —
    whatever the cell would answer (:class:`Context`). The exemption lives HERE, never with the
    caller: ``pause_recovery.on_plate_cleared`` passes no ``command_pending`` at all, and a
    person's clear must not depend on whether a caller happened to.

    And a second, by EVENT where a dwell used to stand (2026-10-11): a LOOKING occasion stands while
    the printer's terminal is being handled (``ctx.terminal_in_flight``) — that terminal decides the
    rows it answers, from the holds it read before closing them.

    And a third (2026-10-12): a row carrying the farm's AIR stop (``printer_incidents.air_stop_on`` — a
    stop the printer took ends the job and its driver closes the row, so an open row carrying one is a
    stop the printer did NOT take, escalated to a person) stands on every occasion but the two in
    :data:`_PENDING_EXEMPT`: the print may still be running on air, and a running edge or a quiet wire is
    no evidence it stopped. Held open, it also keeps the per-push air detector off the print
    (``printer_incidents.air_stop_sent``): one escalation, never a page loop.
    """
    handler = _TABLE[(_resolution_of(row), occasion)]
    if ctx.terminal_in_flight and occasion in _LOOKING:
        return Verdict(
            close=False,
            evidence="the printer's terminal is being handled — it decides the holds it answers",
        )
    if ctx.command_pending and occasion not in _PENDING_EXEMPT:
        return Verdict(
            close=False,
            evidence="a farm AMS command on this row has not run (held or unanswered) — the row stands "
            "until it runs, a reboot voids it, or the job ends",
        )
    if occasion not in _PENDING_EXEMPT and printer_incidents.air_stop_on(row.printer_id, row.id):
        return Verdict(
            close=False,
            evidence="the farm stopped this print for printing on air and the printer did not take the stop — "
            "the row stands until the job ends or a person recovers the printer",
        )
    return handler(row, ctx)


# --- is a RESUME owed (doctrine rule 1; Raymond 2026-10-11: "will i have to click resume on a ------
# filament swap after this is shipped? the answer should be no") ------------------------------------
#
# A row the farm holds over a PAUSEd print whose toolhead was put back by the PRINTER or a PERSON —
# never by the farm — is resumed ONCE: a print paused on a fixed toolhead is waiting for a button
# nobody is standing there to press. The resume itself is never this module's: the caller spawns the
# one resume body (``spool_recovery.resume_paused_print``, which asks K7 again at publish time).
#
# "Once" is the RECORD's (``printer_action``, the row's farm ``resume`` actions — durable across a
# restart, which the process set it replaced was not): a farm resume on the row that is outstanding,
# reached, refused by the printer or taken over by a person spends it; one the printer never received
# (``void``, or a frame that never left the farm) does not — the hold is still owed its resume.


#: :func:`resume_owed`'s closed verdict. ``owed`` — the farm resumes it now (the spawner acts on this
#: alone); ``waiting`` — a guard the farm itself waits out (its own AMS command still pending, the path
#: not quiet, the AMS mid-change, the plate-check episode not open yet) — once it clears the sweep
#: resumes it; ``declined`` — the farm never resumes this row (maintenance mode, another row owns the
#: pause, the once spent, not the row's job, no evidence) — a person's Resume, or another lane's.
ResumeVerdict = Literal["owed", "waiting", "declined"]


@dataclass(frozen=True)
class ResumeOwed:
    """:func:`resume_owed`'s answer: the closed :data:`ResumeVerdict`, the SENTENCE that decided it (the
    spawner logs a yes; a no is a level re-asked every sweep tick, so it is read, never logged), and —
    for a ``declined`` whose pause another open row owns (the plate check, a runout, an AMS hold) — that
    row's kind, so the hold chip names that row rather than asking a person to press Resume."""

    verdict: ResumeVerdict
    evidence: str
    owner: str | None = None

    @property
    def owed(self) -> bool:
        return self.verdict == "owed"


def _owed(evidence: str) -> ResumeOwed:
    return ResumeOwed("owed", evidence)


def _waiting(evidence: str) -> ResumeOwed:
    return ResumeOwed("waiting", evidence)


def _declined(evidence: str, *, owner: str | None = None) -> ResumeOwed:
    return ResumeOwed("declined", evidence, owner)


ResumeRule = Callable[[HoldRow, Context], ResumeOwed]


def _never_resumes(reason: str) -> ResumeRule:
    """A class the farm never resumes on a load, carrying why."""

    def _rule(_row: HoldRow, _ctx: Context) -> ResumeOwed:
        return _declined(reason)

    return _rule


def _held_job_paused(row: HoldRow, ctx: Context) -> str | None:
    """``None`` when the printer holds the row's OWN job PAUSEd — the job the fault interrupted,
    named on both sides (``job_identity.same_job`` answers ``same``: an id-less echo is no job) —
    else the sentence why not."""
    if _live_state(ctx.state).upper() != "PAUSE":
        return "the printer is not PAUSEd"
    if same_job(_live_job(ctx.state), row.job_id) != "same":
        return "the paused job is not the one the fault interrupted"
    return None


def _repair_resume_owed(row: HoldRow, ctx: Context) -> ResumeOwed:
    """``physical`` (the ``repair`` class): today's repair evidence — a load NOBODY commanded that
    landed after the row opened (``Context.observed_load_at``, the record; :func:`_repair_motion`'s
    (a) arm: the printer reporting, the path quiet), the row's own job PAUSEd. Somebody freed the
    path at the printer and loaded a slot; the repair is their "go". (b) — a print feeding — needs
    RUNNING, which a resume never does."""
    not_held = _held_job_paused(row, ctx)
    if not_held is not None:
        return _declined(not_held)
    if not _reporting(ctx.state):
        return _declined("the printer has not reported a state")
    if not path_quiet(ctx.state):
        return _waiting("the path is not quiet (a fault stands, or the AMS is mid-change)")
    if _loaded_since(row, ctx):
        return _owed("the filament path was repaired: a load completed after the fault, the job PAUSEd")
    return _declined("no repair evidence")


def _toolhead_resume_owed(row: HoldRow, ctx: Context) -> ResumeOwed:
    """``toolhead``: a LEVEL, readable off the printer's first fresh report after a restart — the row's
    own job PAUSEd, the path quiet, the toolhead's feed state FED or TAIL NOW (:func:`_toolhead_fed`), no
    farm load on this hold reached the toolhead, and no AMS hold open beside it.

    No landing EDGE is asked for: the hold's fact was "this job would print air", and a toolhead that
    reads fed has answered it, whoever fed it — the printer's own backup switch, a person's load, or a
    load no record saw. 001-H2S incident 574 (2026-10-11): the firmware's switch landed while the
    build that opened the hold had no record to write it to, so a rule asking for the landing would
    have stranded it again after the deploy.

    The farm's OWN refill is excluded on the record (a farm ``load`` on this row answered ``reached``):
    that episode publishes its own resume. An AMS row beside it is that fault's lane's pause to answer
    (a give-up's refill sits on the AMS row, a class that never resumes)."""
    not_held = _held_job_paused(row, ctx)
    if not_held is not None:
        return _declined(not_held)
    if not path_quiet(ctx.state):
        return _waiting("the path is not quiet (a fault stands, or the AMS is mid-change)")
    if not _toolhead_fed(ctx.feed):
        return _declined("the toolhead does not read fed")
    ams = sorted(printer_incidents.open_kinds(row.printer_id) & AMS_FAULT_KINDS)
    if ams:
        return _declined("an AMS hold stands beside it — that fault's own lane answers the pause", owner=ams[0])
    if any(
        action.command == "load" and action.answer == Answer.REACHED.value
        for action in printer_incidents.farm_actions(row.printer_id, row.id)
    ):
        return _declined("the farm's own refill loaded this toolhead — its episode publishes the resume")
    return _owed("the toolhead reads fed and the job is PAUSEd on a quiet path")


# THE resume table — TOTAL over the resolution classes, keyed like :data:`_TABLE` and with no
# default: a class nobody wrote a resume rule for RAISES at the lookup (``KeyError``,
# :func:`resume_owed`).
_RESUME_OWED: dict[str, ResumeRule] = {
    # A jam's page exits through the printer's own Retry; a runout is a same-slot refill the refill
    # lane resumes (invariant 9); a holder fault and an unanswered power-loss prompt are the wire's.
    RESOLUTION_WIRE: _never_resumes(
        "a wire hold is not resumed on a load — a jam's exit is the printer's Retry and a runout is the "
        "refill lane's (invariant 9)"
    ),
    RESOLUTION_REPAIR: _repair_resume_owed,
    RESOLUTION_OPERATOR: _never_resumes("a part on the plate or a lost Z datum is answered by a human clearing it"),
    RESOLUTION_JOB_PAUSE: _never_resumes("the plate check is answered by its own ladder or a person, never a load"),
    RESOLUTION_DECLARED: _never_resumes("maintenance mode resumes nothing"),
    RESOLUTION_TOOLHEAD: _toolhead_resume_owed,
}


# How many farm resumes a RUNOUT row's refill may send before the farm gives up and pages (R14, measured
# 2026-10-12 on production's support/logs over 90 days: 56 refill auto-resume episodes, the farm published 2
# resumes and both landed RUNNING in 7–11 s; no refusal was ever logged, so no refusal shape exists to
# measure and the cap only bounds a loop): the first resume plus two re-owes. Every other class: one.
RUNOUT_RESUME_CAP = 3


@dataclass(frozen=True)
class ResumeSpent:
    """:func:`resume_spent`'s answer when the row is owed no further farm resume: the SENTENCE why, and
    ``gave_up`` — the row's last farm resume was NOT carried out (the printer refused it, or the record
    passed its self-heal bound with no answer) and the row's budget is spent, so the farm's give-up is
    owed: the page (``spool_recovery``'s refused / unanswered resume escalation). ``gave_up`` is False
    while a resume is still out (awaiting the printer), once one REACHED, or once a person took it over."""

    why: str
    gave_up: bool = False


def resume_spent(printer_id: int, incident_id: int, *, unanswered: frozenset[int] = frozenset()) -> ResumeSpent | None:
    """The farm's resume budget on ONE row, off the record: ``None`` while the row is still owed a resume,
    else why not (:class:`ResumeSpent`). THE one decider of it — :func:`resume_owed`'s classes here, and
    the runout row's refill resume (``spool_recovery``: the level read, the publish under the printer's
    resume lock, the printer's answer) — never a second spelling.

    The row's farm resumes that SPEND are the ones the printer may have received: a frame that never left
    the farm, or a resume the record answered ``void`` (never received, a reboot, the job ended), spends
    nothing and is owed again. Of the rest:

    * one ``reached`` spends the row (the print ran; the row closes on it);
    * a person's command that took the last one over spends it (theirs now);
    * the last one still OUTSTANDING spends it — awaiting the printer, nothing is sent behind it — unless
      the record passed its self-heal bound (``unanswered``: the row's outstanding farm resumes whose bound
      passed, read off the record by the caller; this module never imports the record's owner);
    * a last one the printer REFUSED, or one past its bound unanswered: for a RUNOUT row the row is owed
      again until :data:`RUNOUT_RESUME_CAP` resumes went out (R14, 2026-10-12: a refused or unconfirmed
      refill resume is resent on the next evidence, then the farm pages); every other class is spent at
      once (one resume per row — a per-kind-and-job budget was ruled out as a new human gate, AR-H6).
      Spent there, the give-up is owed (``gave_up``)."""
    resumes = [
        action
        for action in printer_incidents.farm_actions(printer_id, incident_id)
        if action.command == "resume" and not action.unsent and action.answer != Answer.VOID.value
    ]
    if not resumes:
        return None
    reached = next((a for a in resumes if a.answer == Answer.REACHED.value), None)
    if reached is not None:
        return ResumeSpent(f"the farm's resume reached the printer (action {reached.action_id})")
    last = resumes[-1]
    if last.answer == Answer.TAKEN_OVER.value:
        return ResumeSpent(f"a person's command took the farm's resume over (action {last.action_id})")
    if last.answer is None and last.action_id not in unanswered:
        return ResumeSpent(f"the farm's resume is out, awaiting the printer (action {last.action_id})")
    how = "refused by the printer" if last.answer == Answer.REFUSED.value else "unanswered past its self-heal bound"
    cap = RUNOUT_RESUME_CAP if printer_incidents.cached_kind(printer_id, incident_id) == KIND_RUNOUT else 1
    if len(resumes) < cap:
        return None
    return ResumeSpent(
        f"the farm sent {len(resumes)} of {cap} resume(s) on this hold; the last (action {last.action_id}) was {how}",
        gave_up=True,
    )


def _resume_withheld(row: HoldRow, ctx: Context) -> ResumeOwed | None:
    """The guards every class shares, ``None`` when none holds: never in maintenance mode (the evidence
    IS the hands' own work), never at the printer's plate-check pause (a resume would answer the plate
    question with no re-check — the plate-check row owns the pause; before its episode has opened the
    row, the farm waits for it), never beside a runout row (a runout-held print is the refill lane's to
    resume, invariant 9), never at the firmware's post-reboot prompt (``0300_8007``: the power-loss
    recovery, ``pause_recovery``, answers it and resumes the interrupted print — the farm waits for it),
    and once (:func:`resume_spent`)."""
    pid = row.printer_id
    if printer_incidents.automation_held(pid):
        return _declined("maintenance mode — the farm resumes nothing under a person's hands")
    if ctx.state is not None and power_loss_prompt_standing(getattr(ctx.state, "hms_errors", None) or []):
        return _waiting("the printer's power-loss prompt stands — the power-loss recovery resumes it")
    plate_row = printer_incidents.snapshot(pid, kind=KIND_PLATE_VISION)
    if plate_row is not None:
        return _declined(
            "the printer paused the job at its plate check — its own ladder or a person answers it",
            owner=KIND_PLATE_VISION,
        )
    if plate_check_paused(ctx.state):
        return _waiting("the printer paused the job at its plate check — its episode opens on the next report")
    if KIND_RUNOUT in printer_incidents.open_kinds(pid):
        return _declined(
            "a runout row stands — the refill lane resumes a runout-held print (invariant 9)", owner=KIND_RUNOUT
        )
    spent = resume_spent(row.printer_id, row.id)
    return _declined(spent.why) if spent is not None else None


def resume_owed(row: HoldRow, ctx: Context) -> ResumeOwed:
    """Is a RESUME of the paused print owed on ``row``, and why (not)? THE one answer — asked by the
    one spawner (``spool_recovery``'s sweep and session start, for every open row), re-asked by the
    resume's own readiness check after its settle, and read by the hold chip (``hold_chip``: amber
    "Resuming" while it is owed or waiting, the owning row's chip or "Press Resume." once declined).
    The spawner acts on ``owed`` alone.

    Raises ``KeyError`` for an unregistered class, like :func:`resolve`. Two guards sit over the table,
    read after the lookup: while the farm's own motion command on the row has not run
    (``ctx.command_pending``) nothing is resumed — the AMS can still run it on its own and empty the
    toolhead under the print (K11) — and the shared guards (:func:`_resume_withheld`)."""
    rule = _RESUME_OWED[_resolution_of(row)]
    if ctx.command_pending:
        return _waiting("a farm AMS command on this row has not run (held or unanswered)")
    withheld = _resume_withheld(row, ctx)
    if withheld is not None:
        return withheld
    return rule(row, ctx)
