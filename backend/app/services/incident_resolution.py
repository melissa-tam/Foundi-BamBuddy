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

**Two lanes end rows of this family OUTSIDE the table, by declaration:**

* ``farm_policy``'s plate-vision FIRST-TRIP re-check closes its own
  ``plate_vision`` row with ``source=terminal`` when the farm requeues the unit for
  the printer's own second opinion. It stays there because it owns the state the
  decision is made of — the windowed trip count, whether the farm can vouch for the
  re-check, and the gate it raises when it cannot. Ending the row is one step of that
  disposition, not an occasion anybody else observes.
* ``service_hold.exit`` closes the ``declared`` hold it opened. A declared hold ends
  ONLY through the verb that opened it; giving the table a cell that could close one
  would be a second way to end it, which is precisely what the class exists to refuse.

Both are named in the AST pin's allowlist (``test_code_quality`` /
``test_incident_resolution``), so a THIRD closer appearing anywhere fails the suite
rather than quietly becoming a second owner.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from backend.app.models.printer_incident import (
    RESOLUTION_DECLARED,
    RESOLUTION_OPERATOR,
    RESOLUTION_REPAIR,
    RESOLUTION_WIRE,
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
from backend.app.services.hms_errors import live_candidates
from backend.app.services.plate_occupancy import plate_occupancy
from backend.app.services.tray_fields import valid_feeder

logger = logging.getLogger(__name__)


# Every occasion on which somebody asks the question. They are OCCASIONS, not events:
# ``sweep_tick`` and ``startup`` are the farm looking, the other three are the farm
# being told. The distinction is load-bearing — the two "looking" occasions differ
# only in whether a dwell applies.
Occasion = Literal["running_edge", "job_terminal", "sweep_tick", "startup", "plate_cleared"]


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
    process's motion memory. ``driver_live`` is ``spool_recovery.has_live_recovery``
    for this printer, asked by the caller because the task slot is the caller's store.
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


# The two motion evidences the ``repair`` class has always admitted, plus the third
# this wave adds. Named constants rather than prose so the self-heal arm can ask WHICH
# one answered without matching on a sentence.
_REPAIR_EVIDENCE_LOAD = "load completed after the fault"
_REPAIR_EVIDENCE_RUNNING = "print running through the path"
_REPAIR_EVIDENCE_COMPLETED = "print completed through the path after the fault"


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
    * ``running_seen_at`` — this process saw the printer RUNNING with no eject owning
      it. Stamped on the POSITIVE reading ONLY, which is what makes it usable as
      evidence AFTER the fact: the negative guards (an eject is filament-less, the
      wire must be quiet) are asked again at verdict time, where the answer is
      current.

    Process-lifetime by design, and the safe direction: a restart empties it, so the
    only repair evidence left after one is a print actually running — which is why the
    startup rearm cannot close a physical hold on an idle printer however clean the
    wire reads.
    """

    def __init__(self) -> None:
        self._load_completed_at: dict[int, datetime] = {}
        self._running_seen_at: dict[int, datetime] = {}
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
        live = ((getattr(state, "state", None) or "") if state is not None else "").upper()
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

        # POSITIVE reading only — no edge, no epoch test. "A print was running on this
        # printer at some point after the fault opened" is a fact a reconnect cannot
        # fabricate (the printer is demonstrably printing), and the eject exclusion is
        # asked HERE because a sweep's RUNNING must never enter the ledger at all.
        if live == "RUNNING" and plate_occupancy.eject_identity(printer_id) is None:
            self._running_seen_at[printer_id] = datetime.utcnow()

    def load_completed_at(self, printer_id: int) -> datetime | None:
        """When this process last saw a filament change COMPLETE onto a real feeder."""
        return self._load_completed_at.get(printer_id)

    def running_seen_at(self, printer_id: int) -> datetime | None:
        """When this process last saw a non-eject print RUNNING on this printer."""
        return self._running_seen_at.get(printer_id)

    def reset(self) -> None:
        """Test hook: drop every motion memory. ``spool_recovery._reset_state`` delegates here."""
        self._load_completed_at.clear()
        self._running_seen_at.clear()
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
    that caused it and drops every subsequent move.
    """
    return not live_candidates(state) and not ams_mid_filament_change(state)


def driver_owns(row: PrinterIncident, *, live: bool) -> bool:
    """Is a recovery driver the owner of this row's outcome?

    THE one spelling, replacing two that had drifted: the ``status == recovering``
    test the sweep and the startup rearm used, and the live-task test the running-edge
    closer used. ``recovering`` is a PROMISE that a task is acting and ``live`` is the
    task slot answering whether one actually is; either makes the outcome somebody
    else's to write, and closing a row from under a driver destroys entry exclusivity
    (006-H2S 2026-09-04: the re-PAUSE found no open incident and spawned a SECOND
    driver onto one AMS).

    ``_active_tasks`` stays the store and stops deciding — callers pass its answer in.
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
    resume the driver ITSELF published (the W1 stuck-change reset,
    ``_resume_and_confirm``) is an intermediate reading of that procedure, and the
    driver consumes it. The deferred edge is not lost — ``_run_recovery``'s handover
    re-reads the level once the slot is free, and that handover is why the test here
    is the LIVE TASK and not :func:`driver_owns`: a row still reading ``recovering``
    because its driver crashed (an R1 orphan) has no owner left, and must still close.
    """
    if ctx.driver_live:
        return Verdict(close=False, evidence="a recovery driver is live and owns the outcome; closer stands aside")
    return Verdict(close=True, source=RESOLVE_OBSERVED_RUNNING, evidence="the printer is RUNNING again")


def _wire_job_terminal(_row: PrinterIncident, _ctx: Context) -> Verdict:
    """A JOB HOLD cannot outlive the job. A terminal is as good a statement as the
    wire makes that the fault it interrupted is over."""
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
    if _live_state(ctx.state).upper() == "RUNNING" and plate_occupancy.eject_identity(row.printer_id) is None:
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
    * a non-eject RUNNING sighting AFTER the row opened. This excludes a pull-back
      timeout raised at end-of-print unload with no post-fault resume — there the job
      completes, but nothing ever ran through the path after the fault.
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
    seen = ctx.ledger.running_seen_at(row.printer_id)
    if seen is None or row.created_at is None or seen <= row.created_at:
        return Verdict(close=False, evidence="no print was seen RUNNING through the path after the fault")
    if not path_quiet(ctx.state):
        return Verdict(close=False, evidence="the path is not quiet (a fault stands, or the AMS is mid-change)")
    return Verdict(close=True, source=RESOLVE_REPAIR_COMPLETED, evidence=_REPAIR_EVIDENCE_COMPLETED)


def _repair_plate_cleared(_row: PrinterIncident, ctx: Context) -> Verdict:
    """RECOVER only. "An operator inspected the machine" is the third return-to-normal
    the repair class admits; a routine clear-plate says nothing about the filament path.
    """
    if ctx.cleared is not None and ctx.cleared.recover:
        return Verdict(close=True, source=RESOLVE_OPERATOR, evidence="the operator recovered the printer")
    return Verdict(close=False, evidence="a routine plate clear is not a statement about the filament path")


# --- the ``operator`` class ---------------------------------------------------------


def _operator_plate_cleared(_row: PrinterIncident, ctx: Context) -> Verdict:
    """Both verbs. The evidence a human produces for a confirmed plate-check trip or a
    lost Z datum is the part coming off the plate, and Recover is the stronger form of
    the same statement."""
    recovered = ctx.cleared is not None and ctx.cleared.recover
    return Verdict(
        close=True,
        source=RESOLVE_OPERATOR,
        evidence="the operator recovered the printer" if recovered else "the operator cleared the plate",
    )


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
    (RESOLUTION_REPAIR, "running_edge"): _stand(
        "a RUNNING edge is not repair evidence — an eject sweep makes one and moves no filament; "
        "the sweep closes it after the dwell"
    ),
    (RESOLUTION_REPAIR, "job_terminal"): _repair_job_terminal,
    (RESOLUTION_REPAIR, "sweep_tick"): _repair_sweep_tick,
    (RESOLUTION_REPAIR, "startup"): _repair_startup,
    (RESOLUTION_REPAIR, "plate_cleared"): _repair_plate_cleared,
    (RESOLUTION_OPERATOR, "running_edge"): _stand("a RUNNING edge is not a human clearing a plate"),
    (RESOLUTION_OPERATOR, "job_terminal"): _stand(
        "the terminal is frequently the farm's OWN stop of that print — it cannot answer the hold"
    ),
    (RESOLUTION_OPERATOR, "sweep_tick"): _stand(
        "a part on the plate and a lost Z datum are facts no HMS list reports — 'clean and idle' is "
        "this hold's NORMAL reading"
    ),
    (RESOLUTION_OPERATOR, "startup"): _stand("a restart is not a human clearing a plate"),
    (RESOLUTION_OPERATOR, "plate_cleared"): _operator_plate_cleared,
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
