"""THE rule table: does THIS occasion end THIS equipment-fault row, and on what evidence?

One owner for a question that used to be answered in six places. Before this module
each closer (``spool_recovery.on_observed_running`` / ``on_job_terminal`` /
``sweep_open_incidents`` / ``rearm_incidents_on_startup``, ``_resume_after_repair``'s
readiness test and ``pause_recovery.on_plate_cleared``) carried its own
``if resolution == …`` chain, and the motion evidence the ``repair`` class turns on
lived in ``spool_recovery`` module globals. Adding an evidence therefore meant editing
several closers — which is how 011-H2S 2026-09-17 came to sit escalated with a clean
wire, an idle printer and a completed print through the repaired path, because the
commonest fleet repair (hand-clear the path, resume, the job runs to ``completed``)
was not in anybody's vocabulary.

The domain is **one rule table plus three evidence predicates and one per-printer
ledger** — not a strategy family. The variance is ``(resolution_class × occasion) →
verdict`` and most cells are "stand"; :data:`_TABLE` spells every cell out and a
missing key RAISES. That is the ``declared``-class lesson made structural: three
closers once treated "not operator / not repair" as the WIRE lane through a bare
``else``, so a fourth class inherited evidence that said nothing about it. There is no
``else`` here to inherit from.

**Dependency direction.** This module reads the wire (:mod:`hms_errors`,
:mod:`bambu_mqtt`, :mod:`plate_occupancy`) and the store's pure rule table
(:mod:`printer_incidents`). It NEVER imports ``spool_recovery`` or ``printer_manager``
— the printer's live state, the ledger and "is a driver live" all arrive as a
:class:`Context`, so the closers depend on the rule and the rule depends on nothing
that closes. ``printer_incidents`` must never import this module back.

**One lane ends rows of this family OUTSIDE the table, by declaration:**
``service_hold.exit`` closes the ``declared`` hold it opened. A declared hold ends ONLY
through the verb that opened it; giving the table a cell that could close one would be
a second way to end it, which is precisely what the class exists to refuse. (The
2026-09-04 plate-vision first-trip re-check in ``farm_policy`` was the other declared
lane; it was deleted on 2026-09-24 with the lane that STOPPED a paused print — the
plate-check hold is now a ``job_pause`` row, closed here by its own job's resume or
terminal.)

It is named in the AST pin's allowlist (``test_code_quality`` /
``test_incident_resolution``), so a second closer appearing anywhere fails the suite
rather than quietly becoming another owner.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from backend.app.models.printer_incident import (
    RESOLUTION_DECLARED,
    RESOLUTION_JOB_PAUSE,
    RESOLUTION_OPERATOR,
    RESOLUTION_REPAIR,
    RESOLUTION_WIRE,
    RESOLVE_JOB_ENDED_UNSEEN,
    RESOLVE_OBSERVED_RUNNING,
    RESOLVE_OPERATOR,
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
from backend.app.services.hms_errors import fault_tokens, fingerprint_tokens, live_candidates
from backend.app.services.job_identity import job_id, same_job
from backend.app.services.plate_occupancy import plate_occupancy
from backend.app.services.tray_fields import valid_feeder

logger = logging.getLogger(__name__)


# Every occasion on which somebody asks the question. They are OCCASIONS, not events:
# ``sweep_tick`` and ``startup`` are the farm looking, the other four are the farm
# being told. The distinction is load-bearing — the two "looking" occasions differ
# only in whether a dwell applies.
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
    """

    status: str
    eject: bool
    job_id: str | None


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
    a cached read during a disconnect is a memory, not evidence). ``ledger`` is the
    process's motion memory. ``driver_live`` is ``printer_incidents.driver_live`` for
    this printer — the ONE liveness store — asked by the caller and passed in, so a
    verdict is a pure function of what it is handed.
    """

    state: PrinterState | None
    ledger: MotionLedger
    driver_live: bool
    terminal: TerminalEvent | None = None
    cleared: ClearedEvent | None = None


@dataclass(frozen=True)
class Verdict:
    """Close or stand, why, and whether the sweep must hold the reading first.

    ``evidence`` is a SENTENCE in both directions: the closers log it verbatim, so a
    close line names what decided it and a stand line names what did not. ``dwell``
    is True only for the per-tick sweep's cells — the two "looking" occasions differ
    in exactly that, and a restart is itself the fresh derivation a dwell substitutes
    for.
    """

    close: bool
    source: str | None = None
    evidence: str = ""
    dwell: bool = False


# The two motion evidences the ``repair`` class has always admitted, the completed arm
# (2026-09-17) and the new-fault arm (2026-09-25). Named constants rather than prose so
# a caller can ask WHICH one answered without matching on a sentence (the self-heal arm
# asks for the load).
_REPAIR_EVIDENCE_LOAD = "load completed after the fault"
_REPAIR_EVIDENCE_RUNNING = "print running through the path"
_REPAIR_EVIDENCE_COMPLETED = "print completed through the path after the fault"
_REPAIR_EVIDENCE_NEW_FAULT = "a print ran through the path after the fault, and a different fault stands now"


class MotionLedger:
    """Per-printer process memory of filament actually MOVING. One writer.

    The ``repair`` class closes on motion, never on silence: the firmware wipes its
    standing HMS list at every terminal, so a stuck filament and a repaired one read
    identically on the wire (003-H2S 2026-09-11 dispatched into that reading three
    times). This is where the positive readings are kept.

    Both facts are EDGES or POSITIVE readings, never levels:

    * ``load_completed_at`` — the AMS carried a filament change through onto a real
      feeder. The LEVEL "``tray_now`` names a slot" proves nothing: 003-H2S read
      ``tray_now == 1`` before, during and after its fault. The transition is the
      event, and it is scoped to ONE MQTT session (``connection_epoch``) because a
      reconnect re-seeds every wire fact at once — a tray "changing" across a session
      boundary is a reading, not a load.
    * ``path_ran_at`` — this process saw a print RUN THROUGH THE PATH: RUNNING, no eject
      owning the printer, :func:`path_quiet`, and no recovery driver live
      (``printer_incidents.driver_live``). Stamped on that POSITIVE reading only, which
      is what makes it usable as evidence AFTER the fact (no seed, no epoch test — a
      reconnect cannot fabricate a printer demonstrably printing on a quiet path).

      QUALIFIED at write time, because the sighting is read back long after the sample
      and a bare RUNNING reading is two things that are not filament feeding through a
      repaired path. (1) The fault-before-PAUSE window: a row opens on the HMS push that
      carries the fault, typically BEFORE the PAUSE lands, and this sampler stamps that
      push before the entry task even runs — so the next RUNNING samples carry the very
      fault the row is about. The path is not quiet there. (2) A recovery driver's own
      lever resume: a RUNNING sample taken during a resume the driver published is an
      intermediate reading of its procedure — the running-edge cells' own rule
      (:func:`_wire_running_edge`) — and an UPGRADED row keeps the jam's ``created_at``
      (``printer_incidents.upgrade``), so a driver's resume after that open would
      otherwise read as "after the fault" on the physical row it became.
      :func:`_ran_through_path_since` is the ONE reader.

    Process-lifetime by design, and the safe direction: a restart empties it, so the
    only repair evidence left after one is a print actually running — which is why the
    startup rearm cannot close a physical hold on an idle printer however clean the
    wire reads, and why a new fault arriving before this process has sampled a quiet
    RUNNING leaves a physical row standing (the sweep, Recover, or the next qualified
    sighting then answers it).
    """

    def __init__(self) -> None:
        self._load_completed_at: dict[int, datetime] = {}
        self._path_ran_at: dict[int, datetime] = {}
        # printer_id -> (connection_epoch, tray_now feeder) of the previous push. The
        # ledger owns the same-session test rather than borrowing the sampler's
        # tuple: the epoch is what makes the edge an event, so it belongs with the
        # fact it qualifies.
        self._feeder: dict[int, tuple[int, int | None]] = {}

    def observe(self, printer_id: int, state: PrinterState | None, epoch: int) -> None:
        """Sample one status push. Sync, DB-free, never raises for the caller's sake.

        Called from ``spool_recovery.note_demand_watch`` — the ONE writer, riding the
        ~1 Hz push. The first sample for a printer SEEDS only: an edge that happened
        before we looked is not an edge we witnessed.
        """
        feeder = valid_feeder(getattr(state, "tray_now", None))
        prev = self._feeder.get(printer_id)
        self._feeder[printer_id] = (epoch, feeder)

        if prev is not None:
            prev_epoch, prev_feeder = prev
            if epoch == prev_epoch and feeder is not None and prev_feeder != feeder:
                self._load_completed_at[printer_id] = datetime.utcnow()
                logger.info(
                    "incident_resolution: printer %s load completed edge tray_now %s->%s",
                    printer_id,
                    prev_feeder if prev_feeder is not None else "none",
                    feeder,
                )

        # POSITIVE, QUALIFIED reading only — no edge, no epoch test (class docstring).
        # Every exclusion is asked HERE, at the sample, because the reading is only
        # evidence while it is current: a sweep's RUNNING, a RUNNING with the fault still
        # standing and a driver's own resume must never enter the ledger at all. Cheapest
        # test first — the quiet-path test classifies the HMS list, and this runs ~1 Hz.
        if (
            running_without_eject(state, printer_id)
            and path_quiet(state)
            and not printer_incidents.driver_live(printer_id)
        ):
            self._path_ran_at[printer_id] = datetime.utcnow()

    def load_completed_at(self, printer_id: int) -> datetime | None:
        """When this process last saw a filament change COMPLETE onto a real feeder."""
        return self._load_completed_at.get(printer_id)

    def path_ran_at(self, printer_id: int) -> datetime | None:
        """When this process last saw a print run through the path (class docstring)."""
        return self._path_ran_at.get(printer_id)

    def reset(self) -> None:
        """Test hook: drop every motion memory. ``spool_recovery._reset_state`` delegates here."""
        self._load_completed_at.clear()
        self._path_ran_at.clear()
        self._feeder.clear()


# THE ledger. One per process, living with the rule that reads it rather than with the
# closers that no longer decide anything (``spool_recovery`` imports this name).
ledger = MotionLedger()


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

    THE one reading of "a print is feeding", shared by the motion ledger's positive
    stamp (which qualifies it further — :class:`MotionLedger`), the ``repair`` class's
    (b) evidence and the ``job_pause`` class's resume evidence. The eject exclusion is
    not incidental: a sweep is filament-LESS and is not the job a hold paused, so a
    toolhead crossing the plate says nothing about either.
    """
    return _live_state(state).upper() == "RUNNING" and plate_occupancy.eject_identity(printer_id) is None


def driver_owns(row: PrinterIncident, *, live: bool) -> bool:
    """Is a recovery driver the owner of this row's outcome?

    THE one spelling, replacing two that had drifted: the ``status == recovering``
    test the sweep and the startup rearm used, and the live-task test the running-edge
    closer used. ``recovering`` is a PROMISE that a task is acting and ``live`` is the
    task slot answering whether one actually is; either makes the outcome somebody
    else's to write, and closing a row from under a driver destroys entry exclusivity
    (006-H2S 2026-09-04: the re-PAUSE found no open incident and spawned a SECOND
    driver onto one AMS).

    ``printer_incidents.driver_live`` is the store and it does not decide — callers
    pass its answer in as ``live``.
    """
    return row.status == STATUS_RECOVERING or live


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


def _wire_running_edge(row: PrinterIncident, ctx: Context) -> Verdict:
    """A PAUSE->RUNNING edge closes a wire hold, whoever produced it.

    Screen, UI, auto-resume or the firmware recovering by itself — the breadth is the
    point, because the pre-WS2b hold could only be cleared by the one path that set it.

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
    return Verdict(close=True, source=RESOLVE_OBSERVED_RUNNING, evidence="the printer is RUNNING again")


def _wire_job_terminal(_row: PrinterIncident, ctx: Context) -> Verdict:
    """A JOB HOLD cannot outlive the job. A terminal is as good a statement as the
    wire makes that the fault it interrupted is over.

    It stands aside while a recovery DRIVER is live — the running edge's test, for the
    running edge's reason (review F2, 2026-09-23 wave). A terminal the driver's OWN verb
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


def _wire_sweep_tick(_row: PrinterIncident, ctx: Context) -> Verdict:
    """The wire lane's ladder: a positive non-PAUSE state AND zero actionable faults.

    Both guards, then the caller's dwell — the fault that OPENED #60 was evaluated
    78 ms after a dispatch, on a printer reading non-PAUSE for an instant, and a
    level-triggered close with no dwell would make the same mistake in the opposite
    direction.
    """
    over, reported = _hold_over(ctx.state)
    if not over:
        return Verdict(close=False, evidence=f"state is {reported or 'unreported'}, not a positive non-PAUSE")
    if live_candidates(ctx.state):
        return Verdict(close=False, evidence="an actionable fault is still standing")
    return Verdict(close=True, source=RESOLVE_WIRE_CLEAR, evidence="no actionable fault", dwell=True)


def _wire_startup(_row: PrinterIncident, ctx: Context) -> Verdict:
    """The same ladder with no dwell and no fault-liveness guard: a restart re-derives
    every wire fact from scratch, and a printer already running has answered."""
    over, reported = _hold_over(ctx.state)
    if not over:
        return Verdict(close=False, evidence=f"state is {reported or 'unreported'}, not a positive non-PAUSE")
    return Verdict(close=True, source=RESOLVE_REARM, evidence=f"printer is {reported} not PAUSE")


# --- the ``repair`` class -----------------------------------------------------------


def _ran_through_path_since(row: PrinterIncident, ctx: Context) -> bool:
    """Did a print run through the path AFTER this row opened? THE one spelling.

    Read by the two cells that turn on history rather than on the printer's current
    reading — the completed arm (:func:`_repair_job_terminal`) and the new-fault arm
    (:func:`_repair_new_fault`) — off the ledger's QUALIFIED sighting
    (:meth:`MotionLedger.path_ran_at`: RUNNING, no eject, the path quiet, no driver
    live — every qualification asked at the sample). Strictly after ``created_at``: the
    print that was running when the fault arrived is the one it interrupted.
    """
    ran = ctx.ledger.path_ran_at(row.printer_id)
    return ran is not None and row.created_at is not None and ran > row.created_at


def _repair_motion(row: PrinterIncident, ctx: Context) -> Verdict:
    """The two OBSERVED motion evidences, shared by the sweep and the startup rearm.

    Either is sufficient and both sit behind :func:`path_quiet` — whatever moved, the
    fault must not be standing now and the AMS must not be wedged.

    (a) a completed-load EDGE stamped after this row opened: the operator's commonest
        repair is to free the path by hand and then load a slot;
    (b) the printer is RUNNING and no eject owns it — a print is feeding through the
        same path. The eject exclusion is not incidental: a sweep is filament-LESS, so
        a toolhead crossing the plate says nothing about whether filament moves.

    The printer must also be REPORTING. A hold is not ended on a printer we cannot
    hear, however good the memory of its last load.
    """
    if not _reporting(ctx.state):
        return Verdict(close=False, evidence="the printer has not reported a state")
    if not path_quiet(ctx.state):
        return Verdict(close=False, evidence="the path is not quiet (a fault stands, or the AMS is mid-change)")
    loaded_at = ctx.ledger.load_completed_at(row.printer_id)
    if loaded_at is not None and row.created_at is not None and loaded_at > row.created_at:
        return Verdict(close=True, source=RESOLVE_REPAIR_OBSERVED, evidence=_REPAIR_EVIDENCE_LOAD)
    if running_without_eject(ctx.state, row.printer_id):
        return Verdict(close=True, source=RESOLVE_REPAIR_OBSERVED, evidence=_REPAIR_EVIDENCE_RUNNING)
    return Verdict(close=False, evidence="no repair evidence")


def _repair_sweep_tick(row: PrinterIncident, ctx: Context) -> Verdict:
    """Motion evidence, then the sweep's dwell."""
    verdict = _repair_motion(row, ctx)
    return (
        Verdict(close=True, source=verdict.source, evidence=verdict.evidence, dwell=True) if verdict.close else verdict
    )


def _repair_startup(row: PrinterIncident, ctx: Context) -> Verdict:
    """The same evidences with no dwell — the restart IS the fresh derivation the
    dwell substitutes for elsewhere — and closed under the rearm's own source token,
    because the edge itself was never witnessed by this process.

    After a restart the ledger is empty by construction, so in practice only a RUNNING
    print can close one. That is correct: ``tray_now`` naming a slot is a LEVEL a
    stuck-filament printer reports just as readily.
    """
    verdict = _repair_motion(row, ctx)
    return Verdict(close=True, source=RESOLVE_REARM, evidence=verdict.evidence) if verdict.close else verdict


def _repair_job_terminal(row: PrinterIncident, ctx: Context) -> Verdict:
    """THE 011-H2S evidence: the job the fault interrupted ran to ``completed``.

    23 of the 40 physical rows in this farm's history ended as a hand repair plus a
    resume; the resumed job then ran to completion, which means filament fed to the
    end of it. That is the strongest positive statement the path ever makes — and
    until 2026-09-17 nothing counted it, because the completion landed inside the
    sweep's 120 s dwell and the terminal closer had no repair vocabulary at all.

    Five qualifications, each closing a way the reading could be wrong:

    * ``completed`` ONLY. A stop, a failure and an abort are the shape 38 pin: they are
      very often the operator stopping the very print the fault broke.
    * not an EJECT. A sweep is filament-less; its ``completed`` proves nothing.
    * the SAME job (``subtask_id`` == the row's ``job_id``, and the terminal must name
      one). The row blocks the DISPATCHER, not the touchscreen: a screen-started print
      completing on another slot must not launder a blocked shared path.
    * a print ran through the path AFTER the row opened (:func:`_ran_through_path_since`
      — the qualified sighting, the new-fault arm's own evidence). This excludes a
      pull-back timeout raised at end-of-print unload with no post-fault resume — there
      the job completes, but nothing ever ran through the path after the fault — and,
      since the sighting is qualified, the RUNNING samples of the fault-before-PAUSE
      window and a recovery driver's own lever resumes.
    * :func:`path_quiet` now. Evidence is not a latch.
    """
    terminal = ctx.terminal
    if terminal is None:
        return Verdict(close=False, evidence="no terminal event was supplied")
    if (terminal.status or "").lower() != "completed":
        return Verdict(close=False, evidence=f"terminal status is {terminal.status!r}, not 'completed'")
    if terminal.eject:
        return Verdict(close=False, evidence="the terminal is an eject sweep — filament-less, not repair evidence")
    job = (terminal.job_id or "").strip()
    if not job or job != (row.job_id or ""):
        return Verdict(close=False, evidence="the completed job is not the one the fault interrupted")
    if not _ran_through_path_since(row, ctx):
        return Verdict(close=False, evidence="no print was seen RUNNING through the path after the fault")
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
    motion this class has always closed on; the sweep would have closed the row on the
    same running print after its dwell, and the new fault simply arrived inside that
    dwell.

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
    * :func:`_ran_through_path_since` — a QUALIFIED sighting after the row opened. Not
      the fault-before-PAUSE window (the path was not quiet), not a driver's own lever
      resume (a driver was live). After a restart the ledger is empty, so the row
      STANDS until this process has itself seen the path run — the safe direction: a
      hold is never ended on motion nobody here witnessed.
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
# The printer paused ONE job and is asking a human about it (its own pre-print plate
# check). The answer is that job: resumed — its RUNNING ends the hold and the same job
# continues — or stopped — its terminal ends the hold, and the terminal's verdict
# (``plate_refused``) hands the plate to the plate authority. Every cell is bound to the
# row's OWN job, because another job's edge or terminal says nothing about this pause.
#
# ...and a job pause cannot outlive its job. When the farm never saw that job's terminal
# (a restart or a dropped session swallowed it and no reconcile synthesised one), the
# per-tick sweep closes the row on the printer's POSITIVE report that the job is over.

# The states in which a printer is positively holding NO job paused: the job it had ran
# to a terminal, or it is idle. ``PAUSE`` is this hold's own reading and the active
# states (``PREPARE`` / ``SLICING`` / ``RUNNING``) belong to a job; ``""`` / ``UNKNOWN``
# are the printer saying nothing at all.
_JOB_OVER_STATES: frozenset[str] = frozenset({"FINISH", "FAILED", "IDLE"})


def _same_job(row: PrinterIncident, job: str | None) -> bool:
    """Is ``job`` the job this row paused? ``job_identity.same_job``, with ``unknown``
    read the way this rule table reads a missing id: the row stores ``''`` for "the
    printer named no job", and an echo that names none either is that same id-less
    job — while an id on only ONE side is a different job, never a match."""
    verdict = same_job(job, row.job_id)
    if verdict == "unknown":
        return job_id(job) is None and job_id(row.job_id) is None
    return verdict == "same"


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
    if not _same_job(row, _live_job(ctx.state)):
        return Verdict(close=False, evidence="the RUNNING job is not the one the printer paused")
    return Verdict(close=True, source=RESOLVE_OBSERVED_RUNNING, evidence="the paused job is RUNNING again")


def _job_pause_job_terminal(row: PrinterIncident, ctx: Context) -> Verdict:
    """The paused job's terminal ends the hold — the job it asked about is over.

    What happens to the PLATE is not this cell's question: the terminal's classification
    (``farm_correlation.classify_stop`` → ``plate_refused``) was captured BEFORE this
    closer ran, and the plate authority acts on it.
    """
    terminal = ctx.terminal
    if terminal is None:
        return Verdict(close=False, evidence="no terminal event was supplied")
    if terminal.eject:
        return Verdict(close=False, evidence="the terminal is an eject sweep — not the job the printer paused")
    if not _same_job(row, terminal.job_id):
        return Verdict(close=False, evidence="the terminal is not the job the printer paused")
    return Verdict(close=True, source=RESOLVE_TERMINAL, evidence="the paused job reached a terminal")


def _job_pause_running(row: PrinterIncident, ctx: Context) -> Verdict:
    """The two LOOKING occasions' evidence: a positive RUNNING of the paused job, with no
    eject owning the printer. A pause is this hold's NORMAL reading, so nothing short of
    the job printing again answers it here."""
    if not running_without_eject(ctx.state, row.printer_id):
        return Verdict(close=False, evidence="the paused job is not RUNNING")
    if not _same_job(row, _live_job(ctx.state)):
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
            dwell=True,
        )
    job = _live_job(ctx.state)
    if _reporting(ctx.state) and job and not _same_job(row, job):
        return Verdict(
            close=True,
            source=RESOLVE_JOB_ENDED_UNSEEN,
            evidence="the printer is on another job — the paused job is over",
            dwell=True,
        )
    return None


def _job_pause_sweep_tick(row: PrinterIncident, ctx: Context) -> Verdict:
    """The paused job printing again, or the printer's positive report that it is over —
    either way after the sweep's dwell.

    The edge closer and the terminal closer are the first responders; this catches what
    they missed: a reconnect straight into RUNNING, and a terminal the farm never saw. The
    dwell is what keeps it second — a stopped job's own terminal lands within a second of
    the printer reporting FAILED, and that terminal is the one that carries the
    ``plate_refused`` verdict to the plate authority.
    """
    verdict = _job_pause_running(row, ctx)
    if verdict.close:
        return Verdict(close=True, source=verdict.source, evidence=verdict.evidence, dwell=True)
    ended = _job_pause_ended_unseen(row, ctx)
    return ended if ended is not None else verdict


def _job_pause_startup(row: PrinterIncident, ctx: Context) -> Verdict:
    """The same RUNNING evidence with no dwell, under the rearm's own token — the resume
    itself was never witnessed by this process.

    Deliberately NOT the ended-unseen reading: a job stopped while the farm was down is
    answered by the downtime reconcile's synthesised terminal, which classifies it
    ``plate_refused`` only while this row is still OPEN — closing it here first would
    lose the refused plate's gate."""
    verdict = _job_pause_running(row, ctx)
    return Verdict(close=True, source=RESOLVE_REARM, evidence=verdict.evidence) if verdict.close else verdict


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
    # A wire hold (a jam, a runout, an external-holder physical prompt) already closes on
    # the printer running again or on its job's terminal — so a wire row still OPEN when
    # a new fault reaches the entry gate means the printer has not run since it opened:
    # the new fault arrived INSIDE the hold (a jam raised while the operator refills a
    # runout slot), or a live driver owns the row and the fault is its reading. Neither
    # ends the hold. What the new fault may do is RE-CLASSIFY the row, and the entry
    # gate's outrank test (``spool_recovery._outranks``) already owns that — a runout
    # must never be routed into the swap machine (doctrine invariant 9).
    (RESOLUTION_WIRE, "new_fault"): _stand(
        "a wire hold ends when its printer runs again or its job ends — a new fault while it is open "
        "arrived inside the hold, and the outrank test re-classifies the row if it is worse"
    ),
    (RESOLUTION_REPAIR, "running_edge"): _stand(
        "a RUNNING edge is not repair evidence — an eject sweep makes one and moves no filament; "
        "the sweep closes it after the dwell"
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
}


def resolve(row: PrinterIncident, occasion: Occasion, ctx: Context) -> Verdict:
    """Does ``occasion`` end ``row``? THE answer, for every closer.

    The row's resolution class is read ONCE, from the store's pure rule over the
    model's ``RESOLVES_ON`` table, and the class literal never leaves this module — the
    closers ask about an OCCASION and get a verdict, which is what lets a new evidence
    be added in one place instead of six.

    Raises ``KeyError`` for an unregistered class: a hold nobody has written a rule for
    must fail loudly, never fall through to the wire's evidence.
    """
    resolution = printer_incidents.resolution_class(row.kind, external=printer_incidents.row_external(row))
    return _TABLE[(resolution, occasion)](row, ctx)
