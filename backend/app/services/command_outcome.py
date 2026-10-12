"""How each printer command's answer is read — THE outcome tables, a LEAF (GoF Strategy + State).

Why this module exists (2026-10-11)
-----------------------------------
"using time as a gate is not the correct way to discern status complete or incompleteness, in case
things take longer to update/complete or is stalled temporarily" (Raymond, 2026-10-11, after 001-H2S
sat paused all night over a fed toolhead). Before this module ten-odd deciders in eight modules read
"did the printer do what it was told" off a clock each: a resume "unconfirmed" at 30 s, a stop
"not taken" at 120 s, a start reverted at 90+180 s. A printer outcome is decided by an EVENT — the
command's own ACK, a state the printer reported, a feed-state event, the job's terminal (live or
reconciled), a reboot that voids it, or a person's act — and this module is where each command
family's events are turned into its answer, in one total table per family.

Three readers, one vocabulary
-----------------------------
* :func:`read` — the RECORD's reading (``services/printer_action``): every outstanding action is
  evaluated against every relevant hub event (``printer_events``) and answered once, finally, as one
  of the four :class:`Answer` values. No clock reaches it: an action nothing has answered stays
  outstanding. Where production self-healed after a wait, the record's named self-heal bound is the
  lane's FALLBACK (``printer_action.SelfHealBound``, 2026-10-12: "if it was self heal, it should stay
  self heal") — it hands the lane its next step and still never answers the action, so a late event
  lands here as usual.
* :func:`classify` — the operator click's reading of one AMS motion command
  (``printer_action.observe_motion``, absorbed from ``ams_command``, 2026-10-11, with its
  ``(command, posture)`` table): what the Load / Unload BUTTON says after the click's short window
  (:data:`OPERATOR_ACK_S`). Its deadline cells (``held`` at the bound, ``no_movement`` / ``acted`` at the
  deadline, :data:`UNLOAD_GRACE_S`) decide only those words — the click's ACTION stays outstanding on
  the record until an event answers it. The recovery driver reads the record's answer instead
  (``spool_recovery._observe_command``), and — with no event by its step bound — the record's own
  motion reading at that bound (``printer_action.Unanswered``: these cells over the entry snapshot the
  record took at the publish); it never calls this function, and the record's FINAL answer never
  reads these cells.
* :func:`ran` / :func:`pending_settles` — "did this recorded motion command run" over the live
  toolhead (absorbed from ``refill_verdict``); :func:`takeover` — "has this driver lost its job"
  (the recovery driver's, absorbed out of ``spool_recovery._takeover``); :func:`rebooted` — the
  reboot witness that voids a command (absorbed from ``spool_recovery._void_on_reboot``'s decision).

A LEAF (``test_import_graph._LEAVES``): pure, sync, DB-free and registry-free — every fact comes in as
an argument; the wire is read only through the pure wire leaves (``tray_fields``, ``live_reading``,
``hms_errors``, ``job_identity``) and ``bambu_mqtt``'s constants.

The table rule
--------------
Every table here is EXPLICIT over its key — ``(family, event kind)`` for the record, ``(command,
posture)`` for the AMS motion reading — and a missing key RAISES (``LookupError``): a new family, event
or posture is given its own row, never an ``else`` nobody considered it for (the
``incident_resolution`` rule-table pattern).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from backend.app.services import live_reading
from backend.app.services.bambu_mqtt import (
    AMS_STATUS_ASSIST,
    AMS_STATUS_IDLE,
    PrinterState,
    ams_mid_filament_change,
)
from backend.app.services.hms_errors import power_loss_prompt_standing, power_loss_resume_failed
from backend.app.services.job_identity import same_job
from backend.app.services.tray_fields import (
    ExtruderFeed,
    extruder_feed,
    parse_int_field,
    toolhead_feed,
    valid_feeder,
)

logger = logging.getLogger(__name__)


# --- the record's vocabulary ----------------------------------------------------------------------


class Answer(StrEnum):
    """An action's FINAL answer — what ``printer_action.answer`` holds once ``answered_at`` is set.

    * ``reached`` — the printer did what it was told (the state, the feed or the terminal it was
      sent for was reported; for an ACK-answered verb, a success ACK).
    * ``refused`` — the printer said no (a fail ACK, with its reason), or the command never left
      (no client, a publish the client refused).
    * ``void`` — the command can never be reached: the printer rebooted under it (the power-loss
      prompt), the job it acted on ended, or a later MQTT session began with it unacknowledged.
    * ``taken_over`` — a person's act (an operator command of a conflicting family) or another job
      took the printer while it was outstanding.
    """

    REACHED = "reached"
    REFUSED = "refused"
    VOID = "void"
    TAKEN_OVER = "taken_over"


class Reading(StrEnum):
    """A NON-final reading of an outstanding action, for the lanes that show it (never persisted as an
    answer): ``sent`` — out, no ACK yet; ``acked`` — the printer accepted it; ``held`` — an AMS motion
    command accepted and not yet run (012-H2S 2026-09-23; 011/014-H2S 2026-10-09/10); ``landed`` — a
    START whose dispatched job id the printer reports in a word that is neither active nor that job's
    terminal (IDLE; FINISH for a print): the file landed and the job has not begun (production's Phase B
    of ``_watchdog_print_start``, #1678 — not the feed state's AMS :class:`Landed` event)."""

    SENT = "sent"
    ACKED = "acked"
    HELD = "held"
    LANDED = "landed"


Verdict = Answer | Reading


class Family(StrEnum):
    """The command families, each with its own outcome table (:data:`_TABLE`)."""

    RESUME = "resume"
    PAUSE = "pause"
    STOP = "stop"
    PRINT_START = "print_start"
    EJECT_START = "eject_start"
    AMS_MOTION = "ams_motion"
    AMS_LEVER = "ams_lever"
    DIALOG = "dialog"
    POWER_LOSS_RESUME = "power_loss_resume"
    ACK_ONLY = "ack_only"
    OBSERVED = "observed"


#: Who acted on the printer: the farm's own lanes, an operator's route, or the printer itself (a
#: landing nobody commanded — the firmware's backup switch, a person's hand at the AMS).
Actor = Literal["farm", "operator", "printer"]
ACTOR_FARM: Actor = "farm"
ACTOR_OPERATOR: Actor = "operator"
ACTOR_PRINTER: Actor = "printer"


@dataclass(frozen=True)
class CommandSpec:
    """One command name of the record's vocabulary: its outcome family, the wire command its ACK
    echoes (``None`` where the frame varies — a dialog button — or none is sent), and ``echoes``: does
    the printer answer it with an event the record reads (its echo, or the state its family is answered
    by)? ``False`` = no echo BY DESIGN — a frame the firmware is known not to answer, or one whose echo
    was never measured: the record never registers it outstanding, never rehydrates it and never pages
    it; its row keeps a NULL answer with the reason :data:`NO_ECHO_REASON` (no answer is fabricated)."""

    family: Family
    wire: str | None
    echoes: bool = True


#: The reason a no-echo command's row carries (``printer_action.prepare``) beside its NULL answer.
NO_ECHO_REASON = "no echo by design"


# THE record's command vocabulary, every name explicit (``spec_of`` raises on any other). Farm-semantic
# verbs carry farm names; the upstream config verbs, answered by their ACK alone, carry the wire's own.
COMMANDS: Mapping[str, CommandSpec] = {
    "resume": CommandSpec(Family.RESUME, "resume"),
    "pause": CommandSpec(Family.PAUSE, "pause"),
    "stop": CommandSpec(Family.STOP, "stop"),
    "power_loss_resume": CommandSpec(Family.POWER_LOSS_RESUME, "resume"),
    "print_start": CommandSpec(Family.PRINT_START, "project_file"),
    "eject_start": CommandSpec(Family.EJECT_START, "project_file"),
    "load": CommandSpec(Family.AMS_MOTION, "ams_change_filament"),
    "unload": CommandSpec(Family.AMS_MOTION, "ams_change_filament"),
    "ams_control_resume": CommandSpec(Family.AMS_LEVER, "ams_control"),
    "ams_control_abort": CommandSpec(Family.AMS_LEVER, "ams_control"),
    "ams_control_reset": CommandSpec(Family.AMS_LEVER, "ams_control"),
    "ams_control_pause": CommandSpec(Family.AMS_LEVER, "ams_control"),
    "ams_control_done": CommandSpec(Family.AMS_LEVER, "ams_control"),
    "clean_print_error": CommandSpec(Family.AMS_LEVER, "clean_print_error"),
    "dialog_press": CommandSpec(Family.DIALOG, None),
    "observed_load": CommandSpec(Family.OBSERVED, None),
    # The upstream config verbs: answered by their ACK — except three with no echo by design:
    # ``set_airduct`` (the UI's JSON flap writer: no ack, ``bambu-ecosystem`` h2s-gcode-dialect), and
    # ``gcode_line`` / ``print_speed`` until an echo is measured on this fleet.
    "gcode_line": CommandSpec(Family.ACK_ONLY, "gcode_line", echoes=False),
    "print_speed": CommandSpec(Family.ACK_ONLY, "print_speed", echoes=False),
    "set_airduct": CommandSpec(Family.ACK_ONLY, "set_airduct", echoes=False),
    "ledctrl": CommandSpec(Family.ACK_ONLY, "ledctrl"),
    "select_extruder": CommandSpec(Family.ACK_ONLY, "select_extruder"),
    "skip_objects": CommandSpec(Family.ACK_ONLY, "skip_objects"),
    "xcam_control_set": CommandSpec(Family.ACK_ONLY, "xcam_control_set"),
    "print_option": CommandSpec(Family.ACK_ONLY, "print_option"),
    "calibration": CommandSpec(Family.ACK_ONLY, "calibration"),
    "ams_filament_drying": CommandSpec(Family.ACK_ONLY, "ams_filament_drying"),
    "extrusion_cali_set": CommandSpec(Family.ACK_ONLY, "extrusion_cali_set"),
    "extrusion_cali_del": CommandSpec(Family.ACK_ONLY, "extrusion_cali_del"),
    "extrusion_cali_sel": CommandSpec(Family.ACK_ONLY, "extrusion_cali_sel"),
    "ams_filament_setting": CommandSpec(Family.ACK_ONLY, "ams_filament_setting"),
    "ams_get_rfid": CommandSpec(Family.ACK_ONLY, "ams_get_rfid"),
    "ipcam_record_set": CommandSpec(Family.ACK_ONLY, "ipcam_record_set"),
    # A slicer's own frame, forwarded by a virtual printer's bridge (``printer_action.slicer_forward``):
    # whatever its verb, it is answered by the printer's reply to it — its ACK in whatever block it rides,
    # or, for a data reply carrying no ``result``, the bridge's relay (``printer_action.on_forward_answered``)
    # — or voided by a later session.
    "slicer_forward": CommandSpec(Family.ACK_ONLY, None),
}


def spec_of(command: str) -> CommandSpec:
    """The spec of ``command``. A name the vocabulary does not hold RAISES — a command nobody gave an
    outcome table is drift, never a default."""
    try:
        return COMMANDS[command]
    except KeyError:
        raise LookupError(f"command_outcome: {command!r} is not in the record's command vocabulary") from None


def echoes(command: str) -> bool:
    """Does the printer answer ``command`` (:attr:`CommandSpec.echoes`)? THE question every reader of a
    NULL answer asks before reading it as "outstanding": a no-echo command's NULL answer means nothing
    will ever answer it. A name outside the vocabulary (a row an older build wrote) reads as answered by
    an echo — its NULL then stays "outstanding", the conservative reading."""
    spec = COMMANDS.get(command)
    return spec is None or spec.echoes


# --- the AMS motion reading (absorbed from ams_command, 2026-10-11) --------------------------------

MotionCommand = Literal["load", "unload"]
Posture = Literal[
    "idle_loaded",
    "idle_empty",
    "assist_loaded",
    "assist_empty",
    "other_loaded",
    "other_empty",
    "mid_change_loaded",
    "mid_change_empty",
]
#: The posture's FAMILY — inside a job-owned filament change or outside one. What a reading MEANS
#: differs between them (a ``held`` mid-change waits behind the print's own change; a ``held`` outside
#: one is accepted and not yet run), so the operator copy is keyed by it.
PostureFamily = Literal["mid_change", "outside_change"]
#: The operator click's reading of one AMS motion command (:func:`classify`); also the vocabulary of a
#: recovery step's recorded answer (``printer_incident_step.outcome`` — a ledger written before
#: 2026-10-11 can carry ``held``).
MotionReading = Literal["complete", "acted", "no_movement", "held", "undecidable", "session_changed"]

#: How long the AMS must hold its "nothing fed" reading before an unload with NO observed change cycle
#: counts as complete inside the operator click's window (and how long a mid-change unload's empty
#: reading must hold). The operator's proven manual recovery left 16 s between the unload and the
#: load that worked; 15 s is that gap, floored. A REPLY-SHAPING bound, never an outcome: read only by
#: :func:`classify` (the Load / Unload button's words, ``printer_action.command_for_operator`` → the
#: route's HTTP reply) — never by the record's :func:`read`, nor by the recovery driver, and the click's
#: ACTION stays outstanding on the record until an event answers it.
UNLOAD_GRACE_S: float = 15.0

#: The operator facade's observe window for a Load / Unload click (``printer_action.command_for_operator``):
#: how long the click waits for the wire before answering the button. UNMEASURED — sized to a human
#: click, not a firmware latency. It decides only what the button says; the click's ACTION stays
#: outstanding on the record until an event answers it.
OPERATOR_ACK_S: float = 5.0


def _monotonic() -> float:
    """The module's clock (a seam: tests install ``FakeClock`` over it)."""
    return time.monotonic()


def clock() -> float:
    """The clock every :class:`AmsWireSnapshot` is stamped on (``taken_at``) — public for a caller that
    measures its own time on the SAME timeline as the ``elapsed_s`` it hands :func:`classify`, and for
    the answer line's ``after N s`` (:func:`log_answer`)."""
    return _monotonic()


@dataclass(frozen=True)
class AmsWireSnapshot:
    """The wire fields a motion command's answer is read from, at one instant.

    Taken BEFORE every send (the entry) and at every read after it (``now``). ``None`` means the state
    did not carry a parseable value — never a stand-in for a reading. ``feed`` is the ACTIVE extruder's
    feed (``tray_fields.toolhead_feed``); :func:`posture` reads the entry feeder from it. ``fresh`` is
    whether the state describes its CURRENT MQTT session (``live_reading.is_fresh``). ``taken_at`` is
    :func:`clock`."""

    tray_now: int | None
    ams_status_main: int | None
    ams_status_sub: int | None
    tray_tar: int | None
    connection_epoch: int | None
    fresh: bool
    taken_at: float
    feed: ExtruderFeed


def snapshot(state: PrinterState | None) -> AmsWireSnapshot:
    """Read the snapshot off a live ``PrinterState``. Getattr-safe and total: a ``None`` state and a state
    without a field both give ``None`` fields, and neither is fresh."""
    return AmsWireSnapshot(
        tray_now=parse_int_field(getattr(state, "tray_now", None)),
        ams_status_main=parse_int_field(getattr(state, "ams_status_main", None)),
        ams_status_sub=parse_int_field(getattr(state, "ams_status_sub", None)),
        tray_tar=parse_int_field(getattr(state, "tray_tar", None)),
        connection_epoch=parse_int_field(getattr(state, "connection_epoch", None)),
        fresh=live_reading.is_fresh(state),
        taken_at=_monotonic(),
        feed=toolhead_feed(state).active,
    )


def posture(snap: AmsWireSnapshot) -> Posture:
    """The posture a command is sent INTO — the key :func:`classify`'s rows are read by.

    The AMS state: mid-change is ``bambu_mqtt.ams_mid_filament_change``, ``assist`` / ``idle`` the
    ``AMS_STATUS_*`` constants, anything else (an unreadable state included) ``other``. The entry
    feeder: ``loaded`` when the ACTIVE extruder is fed from a real AMS feeder, else ``empty``. Split in
    every family because what can MOVE differs: an unload pulls back only a loaded feeder, a load feeds
    into an empty path."""
    loaded = snap.feed.kind == "fed"
    if ams_mid_filament_change(snap):
        return "mid_change_loaded" if loaded else "mid_change_empty"
    if snap.ams_status_main == AMS_STATUS_ASSIST:
        return "assist_loaded" if loaded else "assist_empty"
    if snap.ams_status_main == AMS_STATUS_IDLE:
        return "idle_loaded" if loaded else "idle_empty"
    return "other_loaded" if loaded else "other_empty"


def posture_family(snap: AmsWireSnapshot) -> PostureFamily:
    """The family of the posture a command was sent into (``bambu_mqtt.ams_mid_filament_change``)."""
    return "mid_change" if ams_mid_filament_change(snap) else "outside_change"


def entry_loaded(entry_posture: str | None) -> bool | None:
    """Was the ACTIVE extruder fed from a real AMS feeder when the command went out? Read off a
    recorded posture token (``printer_action.entry``); ``None`` when no posture was recorded."""
    if entry_posture is None:
        return None
    return entry_posture.endswith("_loaded")


@dataclass
class Observation:
    """The running facts of ONE command's observation window inside the operator click (:func:`classify`).

    Caller-owned and threaded through every :func:`classify` call for that command. :func:`classify`
    folds each ``now`` into it, the CALLER folds the firmware's ACK for the send into it
    (:meth:`fold_ack`, before every call). Every time in it is on the CALLER's elapsed timeline."""

    #: Some wire field read differently from the entry on at least one read. Sticky.
    moved: bool = False
    #: A non-idle ``ams_status_main`` was READ on some read — the change cycle ran.
    cycle_seen: bool = False
    #: ``elapsed_s`` at which the current unbroken run of "idle AND nothing fed" began.
    idle_empty_since_s: float | None = None
    #: ``elapsed_s`` at which the current unbroken run of "nothing fed" began, whatever the AMS state.
    empty_since_s: float | None = None
    #: The firmware's ACK for THIS send: ``None`` none seen yet, ``True`` success, ``False`` any other.
    acked: bool | None = None

    def fold_ack(self, succeeded: bool | None) -> None:
        """Fold the firmware's ACK for this send (``CommandAck.succeeded``, or ``None`` for no ACK yet).
        An ACK once seen stays seen."""
        if succeeded is None:
            return
        self.acked = succeeded

    def fold(self, entry: AmsWireSnapshot, now: AmsWireSnapshot, elapsed_s: float) -> None:
        """Fold one read into the running facts. Idempotent for a repeated ``now``."""
        if _moving_fields(now) != _moving_fields(entry):
            self.moved = True
        idle = now.ams_status_main == AMS_STATUS_IDLE
        empty = now.feed.kind == "empty"
        if now.ams_status_main is not None and not idle:
            self.cycle_seen = True
        if idle and empty:
            if self.idle_empty_since_s is None:
                self.idle_empty_since_s = elapsed_s
        else:
            self.idle_empty_since_s = None
        if empty:
            if self.empty_since_s is None:
                self.empty_since_s = elapsed_s
        else:
            self.empty_since_s = None


def _moving_fields(snap: AmsWireSnapshot) -> tuple[int | None, int | None, int | None, int | None]:
    """The fields whose change counts as "the AMS moved" (the epoch selects the cross-session table)."""
    return (snap.tray_now, snap.ams_status_main, snap.ams_status_sub, snap.tray_tar)


#: A motion row: ``(target, now, observation, elapsed_s, deadline_s) -> MotionReading | None``.
MotionRow = Callable[[int | None, AmsWireSnapshot, Observation, float, float], "MotionReading | None"]


def _unload_settled(
    target: int | None, now: AmsWireSnapshot, obs: Observation, elapsed_s: float, deadline_s: float
) -> MotionReading | None:
    """An unload sent into an AMS that is NOT mid-change: complete on idle + nothing fed, (a) after an
    observed change cycle, or (b) held for :data:`UNLOAD_GRACE_S` with none observed. "Nothing fed"
    alone is never completion: after a feed fault the toolhead already reads empty (009-H2S
    2026-07-20)."""
    if obs.idle_empty_since_s is None:
        return None
    if obs.cycle_seen or elapsed_s - obs.idle_empty_since_s >= UNLOAD_GRACE_S:
        return "complete"
    return None


def _held_behind_the_change(obs: Observation, elapsed_s: float, deadline_s: float) -> MotionReading | None:
    """``held`` for a command sent into a mid-change AMS: ACKNOWLEDGED and unmoved by
    :data:`UNLOAD_GRACE_S` (or by the deadline when the window is shorter — the operator's) — it sits
    behind the print's own change and runs at the next release (012-H2S 2026-09-23)."""
    if obs.acked is True and not obs.moved and elapsed_s >= min(UNLOAD_GRACE_S, deadline_s):
        return "held"
    return None


def _held_at_the_bound(obs: Observation, elapsed_s: float, deadline_s: float) -> MotionReading | None:
    """``held`` OUTSIDE a filament change: ACKNOWLEDGED and unmoved by the DEADLINE (011-H2S / 014-H2S
    2026-10-09/10 — the AMS ran it on its own ~4.5 min later). Never earlier."""
    if obs.acked is True and not obs.moved and elapsed_s >= deadline_s:
        return "held"
    return None


def _unload_loaded_outside_a_change(
    target: int | None, now: AmsWireSnapshot, obs: Observation, elapsed_s: float, deadline_s: float
) -> MotionReading | None:
    """An unload sent with filament loaded, outside a change: complete as :func:`_unload_settled`, else
    ``held`` at the bound when acknowledged and unmoved."""
    return _unload_settled(target, now, obs, elapsed_s, deadline_s) or _held_at_the_bound(obs, elapsed_s, deadline_s)


def _unload_mid_change_loaded(
    target: int | None, now: AmsWireSnapshot, obs: Observation, elapsed_s: float, deadline_s: float
) -> MotionReading | None:
    """An unload sent into a mid-change AMS with a feeder loaded: complete when the active extruder reads
    empty and has held it for :data:`UNLOAD_GRACE_S` (the AMS may stay in state 1 — the print still owes
    the change); else ``held`` when acknowledged and unmoved."""
    if obs.empty_since_s is not None and elapsed_s - obs.empty_since_s >= UNLOAD_GRACE_S:
        return "complete"
    return _held_behind_the_change(obs, elapsed_s, deadline_s)


def _unload_mid_change_empty(
    target: int | None, now: AmsWireSnapshot, obs: Observation, elapsed_s: float, deadline_s: float
) -> MotionReading | None:
    """An unload sent into a mid-change AMS with NOTHING loaded: nothing physical can move, so
    ``undecidable`` once :data:`UNLOAD_GRACE_S` passed unmoved (or the shorter deadline)."""
    if not obs.moved and elapsed_s >= min(UNLOAD_GRACE_S, deadline_s):
        return "undecidable"
    return None


def _load_reached_target(
    target: int | None, now: AmsWireSnapshot, obs: Observation, elapsed_s: float, deadline_s: float
) -> MotionReading | None:
    """A load, in any posture: complete when the ACTIVE extruder reads the commanded feeder — never on the
    "nothing fed" sentinel (a load of Ext-R reading nothing fed is the absence of a load)."""
    want = extruder_feed(target)
    if want.kind in ("fed", "external") and now.feed == want:
        return "complete"
    return None


def _load_into_an_empty_path_outside_a_change(
    target: int | None, now: AmsWireSnapshot, obs: Observation, elapsed_s: float, deadline_s: float
) -> MotionReading | None:
    """A load into an EMPTY path outside a change: complete as :func:`_load_reached_target`, else ``held``
    at the bound when acknowledged and unmoved — the posture of the farm's refill load."""
    return _load_reached_target(target, now, obs, elapsed_s, deadline_s) or _held_at_the_bound(
        obs, elapsed_s, deadline_s
    )


def _load_mid_change(
    target: int | None, now: AmsWireSnapshot, obs: Observation, elapsed_s: float, deadline_s: float
) -> MotionReading | None:
    """A load sent into a mid-change AMS: complete as :func:`_load_reached_target`, else ``held`` when
    acknowledged and unmoved (012-H2S 2026-09-23's load of slot 3)."""
    return _load_reached_target(target, now, obs, elapsed_s, deadline_s) or _held_behind_the_change(
        obs, elapsed_s, deadline_s
    )


_MOTION_ROWS: dict[tuple[MotionCommand, Posture], MotionRow] = {
    ("unload", "idle_loaded"): _unload_loaded_outside_a_change,
    ("unload", "idle_empty"): _unload_settled,
    ("unload", "assist_loaded"): _unload_loaded_outside_a_change,
    ("unload", "assist_empty"): _unload_settled,
    ("unload", "other_loaded"): _unload_loaded_outside_a_change,
    ("unload", "other_empty"): _unload_settled,
    ("unload", "mid_change_loaded"): _unload_mid_change_loaded,
    ("unload", "mid_change_empty"): _unload_mid_change_empty,
    ("load", "idle_loaded"): _load_reached_target,
    ("load", "idle_empty"): _load_into_an_empty_path_outside_a_change,
    ("load", "assist_loaded"): _load_reached_target,
    ("load", "assist_empty"): _load_into_an_empty_path_outside_a_change,
    ("load", "other_loaded"): _load_reached_target,
    ("load", "other_empty"): _load_into_an_empty_path_outside_a_change,
    ("load", "mid_change_loaded"): _load_mid_change,
    ("load", "mid_change_empty"): _load_mid_change,
}


def _motion_row(command: MotionCommand, entry_posture: Posture) -> MotionRow:
    try:
        return _MOTION_ROWS[(command, entry_posture)]
    except KeyError:
        raise LookupError(
            f"command_outcome: no motion row for (command={command!r}, posture={entry_posture!r}) — "
            "every (command, posture) pair must be given its own row in _MOTION_ROWS"
        ) from None


def _unload_idle_and_empty(
    target: int | None, now: AmsWireSnapshot, obs: Observation, elapsed_s: float, deadline_s: float
) -> MotionReading | None:
    """An unload read across a reconnect, from ONE fresh snapshot: ``complete`` when the AMS reads idle with
    nothing fed (the strict reading: a mid-change empty toolhead is not completion here)."""
    if now.ams_status_main == AMS_STATUS_IDLE and now.feed.kind == "empty":
        return "complete"
    return None


def _unload_nothing_was_loaded(
    target: int | None, now: AmsWireSnapshot, obs: Observation, elapsed_s: float, deadline_s: float
) -> MotionReading:
    """An unload sent into a mid-change AMS with NOTHING loaded, read across a reconnect: ``undecidable``."""
    return "undecidable"


_ACROSS_ROWS: dict[tuple[MotionCommand, Posture], MotionRow] = {
    ("unload", "idle_loaded"): _unload_idle_and_empty,
    ("unload", "idle_empty"): _unload_idle_and_empty,
    ("unload", "assist_loaded"): _unload_idle_and_empty,
    ("unload", "assist_empty"): _unload_idle_and_empty,
    ("unload", "other_loaded"): _unload_idle_and_empty,
    ("unload", "other_empty"): _unload_idle_and_empty,
    ("unload", "mid_change_loaded"): _unload_idle_and_empty,
    ("unload", "mid_change_empty"): _unload_nothing_was_loaded,
    ("load", "idle_loaded"): _load_reached_target,
    ("load", "idle_empty"): _load_reached_target,
    ("load", "assist_loaded"): _load_reached_target,
    ("load", "assist_empty"): _load_reached_target,
    ("load", "other_loaded"): _load_reached_target,
    ("load", "other_empty"): _load_reached_target,
    ("load", "mid_change_loaded"): _load_reached_target,
    ("load", "mid_change_empty"): _load_reached_target,
}


def _across_row(command: MotionCommand, entry_posture: Posture) -> MotionRow:
    try:
        return _ACROSS_ROWS[(command, entry_posture)]
    except KeyError:
        raise LookupError(
            f"command_outcome: no cross-session row for (command={command!r}, posture={entry_posture!r}) — "
            "every (command, posture) pair must be given its own row in _ACROSS_ROWS"
        ) from None


def _across_sessions(
    command: MotionCommand,
    target: int | None,
    entry: AmsWireSnapshot,
    now: AmsWireSnapshot,
    *,
    observation: Observation,
    elapsed_s: float,
    deadline_s: float,
) -> MotionReading | None:
    """A command whose MQTT session ended under it, read on a newer one: nothing to read until that
    session's first FRESH report (``session_changed`` at the deadline); then the cross-session row for the
    posture it was SENT into, else the motion rule (moved → ``acted``, nothing moved →
    ``session_changed``). Never ``held``: the ACK belonged to the session that is gone."""
    row = _across_row(command, posture(entry))
    if not now.fresh:
        return "session_changed" if elapsed_s >= deadline_s else None
    answer = row(target, now, observation, elapsed_s, deadline_s)
    if answer is not None:
        return answer
    moved = observation.moved or _moving_fields(now) != _moving_fields(entry)
    return "acted" if moved else "session_changed"


def classify(
    command: MotionCommand,
    target: int | None,
    entry: AmsWireSnapshot,
    now: AmsWireSnapshot,
    *,
    observation: Observation,
    elapsed_s: float,
    deadline_s: float,
) -> MotionReading | None:
    """THE operator click's reading of one AMS motion command (the button's words). ``None`` = keep
    reading.

    Pure over its arguments, with ONE declared exception: it folds ``now`` into the caller-owned
    ``observation``. The ACK is the CALLER's to fold (``observation.fold_ack(...)``) before every call.

    1. ``now.connection_epoch != entry.connection_epoch`` → :func:`_across_sessions`.
    2. The row for ``(command, posture(entry))`` (a missing row raises).
    3. At ``elapsed_s >= deadline_s``: a moved field → ``acted``, nothing moved → ``no_movement``.

    The deadline cells are the click's window and decide only what the button says; the record's own
    answer is :func:`read`, which reads no clock, and the recovery driver reads that answer.
    """
    if now.connection_epoch != entry.connection_epoch:
        return _across_sessions(
            command, target, entry, now, observation=observation, elapsed_s=elapsed_s, deadline_s=deadline_s
        )
    row = _motion_row(command, posture(entry))
    observation.fold(entry, now, elapsed_s)
    answer = row(target, now, observation, elapsed_s, deadline_s)
    if answer is not None:
        return answer
    if elapsed_s >= deadline_s:
        return "acted" if observation.moved else "no_movement"
    return None


def log_answer(
    *,
    actor: str,
    printer_id: int,
    command: MotionCommand,
    target: int | None,
    entry: AmsWireSnapshot,
    now: AmsWireSnapshot,
    answer: str,
    elapsed_s: float,
) -> None:
    """THE motion answer line — the measurement's grep token (``answer=``), one format for every reader
    of a motion command's answer: the operator facade and the recovery driver's read of the record."""
    logger.info(
        "[ams-command] actor=%s printer=%s command=%s target=%s posture=%s answer=%s after %.1fs "
        "(tray_now %s→%s ams_status %s/%s→%s/%s tray_tar %s→%s)",
        actor,
        printer_id,
        command,
        target,
        posture(entry),
        answer,
        elapsed_s,
        entry.tray_now,
        now.tray_now,
        entry.ams_status_main,
        entry.ams_status_sub,
        now.ams_status_main,
        now.ams_status_sub,
        entry.tray_tar,
        now.tray_tar,
    )


# --- did a recorded motion command run (absorbed from refill_verdict, 2026-10-11) -------------------


def ran(command: str, target: int | None, loaded_at_send: bool | None, live: ExtruderFeed) -> bool | None:
    """Did this RECORDED motion command run? Read off what was recorded at the send — the command, the
    load's ``target``, whether the active extruder was fed from a real AMS feeder (``loaded_at_send``,
    ``None`` = nothing was read) — and the LIVE feed of the ACTIVE extruder
    (``tray_fields.toolhead_feed(state).active``). Pure.

    * unload — ran ⇔ the active extruder was LOADED at the send and now reads empty; sent with nothing
      loaded, or with no reading of what was: ``None`` (nothing physical can answer it).
    * load — ran ⇔ the active feeder now IS the target; a target that is no AMS feeder: ``None``.
    * a live reading of ``unknown``: ``None``.

    The measured reason it exists: an accepted pull-back ran ON ITS OWN ~4.5 min after the send
    (011-H2S 2026-10-09 04:31:27 → 04:36:01; 014-H2S 2026-10-10 02:45:58 → 02:50:38). A name that is no
    motion command RAISES ``LookupError``."""
    if command not in ("load", "unload"):
        raise LookupError(f"command_outcome.ran: {command!r} is not a motion command")
    if live.kind == "unknown":
        return None
    if command == "unload":
        if loaded_at_send is not True:
            return None
        return live.kind == "empty"
    want = valid_feeder(target)
    if want is None:
        return None
    return live.kind == "fed" and live.tray == want


# What settles a pending motion command, read off the live toolhead: ``complete`` (:func:`ran` says it
# ran) or ``undecidable`` (an unload sent with NOTHING loaded, or with no reading of what was, while the
# toolhead reads empty). ``None``: still queued.
PendingSettle = Literal["complete", "undecidable"]


def pending_settles(
    command: str, target: int | None, loaded_at_send: bool | None, live: ExtruderFeed
) -> PendingSettle | None:
    """Has the pending motion command stopped being a queue? THE one rule, read by the driver's own wait,
    the refill verdict and the per-push detectors. Pure."""
    answer = ran(command, target, loaded_at_send, live)
    if answer is True:
        return "complete"
    if answer is None and command == "unload" and live.kind == "empty":
        return "undecidable"
    return None


# --- the reboot witness (absorbed from spool_recovery._void_on_reboot, 2026-10-11) ------------------


def rebooted(hms_list: object) -> bool:
    """Did the printer REBOOT under an outstanding command? The witness is the firmware's power-loss
    prompt (``0300_8007``, ``hms_errors.power_loss_prompt_standing`` — the one reader the power-loss lane
    reads too): a power cycle empties the AMS's queue, so a command it had accepted and not run will never
    run. A reconnect alone voids nothing (a network blip keeps the firmware's queue)."""
    return power_loss_prompt_standing(hms_list if isinstance(hms_list, list) else [])


# --- has a recovery driver lost its job (absorbed from spool_recovery._takeover, 2026-10-11) --------

TakeoverToken = Literal[
    "reclassified", "job_changed", "job_ended", "hijacked_load", "operator_command", "resumed", "paused_elsewhere"
]
# What a driver's loop is waiting in: ``"PAUSE"`` — a step of the swap round, the job paused under it;
# ``None`` — a release verb's window (RUNNING is its expected reading); ``"job_over"`` — the restart
# continuation, where a terminal is the driver's OWN doing (its stop) and a job on the printer is not.
Awaiting = Literal["PAUSE", "job_over"] | None

#: The two live states the driver's PROCEDURE runs in; everything else is a terminal.
DRIVER_STATES: frozenset[str] = frozenset({"PAUSE", "RUNNING"})
#: The live states in which the printer positively holds NO job (``incident_resolution``'s own set).
JOB_OVER_STATES: frozenset[str] = frozenset({"FINISH", "FAILED", "IDLE"})


@dataclass(frozen=True)
class DriverReading:
    """What :func:`takeover` reads of one live reading, gathered by the driver (every input a fact the
    caller owns: the live state word, the job comparison, the store's cached kind, the record's operator
    command, the rule table's quiet-path reading)."""

    live: str
    job: Literal["same", "other", "unknown"]
    reclassified: bool
    pending_tray_target: int | None
    operator_commanded: bool
    path_quiet: bool


def takeover(
    reading: DriverReading,
    *,
    awaiting: Awaiting,
    target: int | None = None,
    after_contract_resume: bool = False,
) -> TakeoverToken | None:
    """Has this driver lost the job it was bound to? The token that says so, else None. ONE predicate for
    every loop of the AMS recovery driver (002-H2S 2026-09-11: the operator STOPPED the print and the
    driver went on to load a slot and stamp a healthy spool out of rotation). First match wins:

    ``reclassified`` the row this driver names is open with a DIFFERENT kind (the one non-abort token);
    ``job_changed`` the printer echoes a different job — or, in the restart continuation, ANY job;
    ``job_ended`` the live state is neither PAUSE nor RUNNING (outside the restart continuation);
    ``hijacked_load`` the firmware honours a load for a tray this step did not command;
    ``operator_command`` an operator's AMS motion command went out since this driver began (the record);
    ``resumed`` a step waiting for PAUSE found the printer RUNNING;
    ``paused_elsewhere`` after the swap contract's resume left PAUSE, the printer is back at PAUSE on a
    QUIET path (no actionable code, not mid-change) — another actor's pause.
    """
    if reading.reclassified:
        return "reclassified"
    if reading.job == "other":
        return "job_changed"
    if awaiting == "job_over":
        if reading.live not in JOB_OVER_STATES:
            return "job_changed"
    elif reading.live not in DRIVER_STATES:
        return "job_ended"
    ptt = reading.pending_tray_target
    if target is not None and ptt is not None and ptt != target:
        return "hijacked_load"
    if reading.operator_commanded:
        return "operator_command"
    if awaiting == "PAUSE" and reading.live == "RUNNING":
        return "resumed"
    if after_contract_resume and reading.live == "PAUSE" and reading.path_quiet:
        return "paused_elsewhere"
    return None


# --- the record's reading: one total table per family -----------------------------------------------


@dataclass(frozen=True)
class WireReading:
    """One status report as the record's tables read it: the job word and job, the AMS snapshot (with the
    active extruder's feed and the session's freshness), and the reboot witness."""

    gcode_state: str
    subtask_id: str | None
    ams: AmsWireSnapshot
    rebooted: bool
    #: The stamp of the last AMS block the client applied (``PrinterState.last_ams_update`` — stamped only
    #: by a push that carries the ``ams`` block, the one that sets ``tray_now``). A per-push MARKER, never
    #: a duration: a value different from an earlier reading's says THIS push brought a fresh AMS block.
    ams_block_at: float = 0.0
    #: The firmware declared the power-loss resume FAILED (``0300_400D``,
    #: ``hms_errors.power_loss_resume_failed``) — its own verdict on the resume it was sent.
    resume_failed: bool = False

    @classmethod
    def of(cls, state: PrinterState | None) -> WireReading:
        """THE build of a reading off a live ``PrinterState`` (getattr-safe)."""
        return cls(
            gcode_state=str(getattr(state, "state", None) or "").upper(),
            subtask_id=getattr(state, "subtask_id", None),
            ams=snapshot(state),
            rebooted=rebooted(getattr(state, "hms_errors", None)),
            ams_block_at=float(getattr(state, "last_ams_update", 0.0) or 0.0),
            resume_failed=power_loss_resume_failed(getattr(state, "hms_errors", None) or []),
        )

    @property
    def live(self) -> bool:
        """Is this a reading of the printer NOW — its current session's report, a reported job word?"""
        return self.ams.fresh and self.gcode_state not in ("", "UNKNOWN")


@dataclass(frozen=True)
class ActionFacts:
    """What the record holds of one action — the facts written at the send (``printer_action`` columns).
    ``entry`` is the family's own entry token (an AMS motion's posture; any other family's job word at the
    send); ``job_id`` the job the command was sent against (a start's: the job it dispatched — the
    submission id minted at the send, never the job the printer held); ``prior_job_id`` a start's job id
    the printer reported at the send (the job it replaces; ``None`` for every other verb)."""

    action_id: int
    printer_id: int
    command: str
    actor: Actor
    target: int | None
    entry: str | None
    job_id: str | None
    prior_job_id: str | None = None

    @property
    def family(self) -> Family:
        return spec_of(self.command).family


@dataclass
class Running:
    """The in-memory running facts of one OUTSTANDING action (``printer_action``'s registry; never a
    column — a restart rebuilds them from the row: ``acked`` from ``acked_at``, ``sent_epoch`` unknown).

    ``acked`` — the ACK seen (``True`` success / ``False`` fail / ``None`` none yet); ``sent_epoch`` — the
    client's MQTT session at the send, ``None`` for an action rehydrated after a restart (its session is
    unknowable: a restart builds a new client); ``change_block_at`` — an AMS motion command sent OUTSIDE
    a filament change was last reported INSIDE one on the push whose AMS block was applied at this stamp
    (``PrinterState.last_ams_update``; ``None`` = never seen in a change, as after a restart);
    ``rebooted_at_send`` — the printer's power-loss prompt already stood at the send (``None`` after a
    restart: unknown), so that reboot preceded the command and voids nothing of it; ``start_landed`` — a
    start's dispatched job id was reported in a non-active word (:data:`Reading.LANDED`): the printer HAS the
    start, so a later session never voids it as unreceived (after a restart the first fresh report re-reads
    it)."""

    acked: bool | None = None
    sent_epoch: int | None = None
    change_block_at: float | None = None
    rebooted_at_send: bool | None = None
    start_landed: bool = False


@dataclass(frozen=True)
class Acked:
    """The firmware's ACK for this action's sequence id."""

    succeeded: bool
    reason: str | None


@dataclass(frozen=True)
class Reported:
    """A status report of the action's printer."""

    reading: WireReading


@dataclass(frozen=True)
class Landed:
    """The feed state's ``load_landed``: the active extruder's feeder became ``tray``."""

    tray: int


@dataclass(frozen=True)
class Ended:
    """A job terminal on the action's printer (live, or synthesised by the downtime reconcile)."""

    job_id: str | None
    status: str


@dataclass(frozen=True)
class SessionBegan:
    """The printer's first fresh report of MQTT session ``connection_epoch`` (``FirstFreshReport``)."""

    connection_epoch: int
    reading: WireReading


@dataclass(frozen=True)
class PersonActed:
    """An operator command of ``family`` went out on the action's printer after it."""

    family: Family


Event = Acked | Reported | Landed | Ended | SessionBegan | PersonActed
EventKind = Literal["acked", "reported", "landed", "ended", "session_began", "person_acted"]


def _kind(event: Event) -> EventKind:
    match event:
        case Acked():
            return "acked"
        case Reported():
            return "reported"
        case Landed():
            return "landed"
        case Ended():
            return "ended"
        case SessionBegan():
            return "session_began"
        case PersonActed():
            return "person_acted"


@dataclass(frozen=True)
class Outcome:
    """:func:`read`'s verdict for one event: the final :class:`Answer` (with the printer's reason for a
    refusal) or a non-final :class:`Reading`, or nothing (``verdict`` ``None``: this event says nothing
    about this action)."""

    verdict: Verdict | None
    reason: str | None = None

    @property
    def final(self) -> bool:
        return isinstance(self.verdict, Answer)


_NOTHING = Outcome(None)
#: The job words of a job in setup or in progress (``bambu_mqtt._ACTIVE_JOB_STATES``' meaning; production's
#: ``plate_occupancy.ACTIVE_PRINT_STATES``, the set ``_watchdog_print_start`` released a start on).
_ACTIVE_JOB_STATES: frozenset[str] = frozenset({"PREPARE", "SLICING", "RUNNING", "PAUSE"})
#: The job words in which a start's OWN job id is that job's TERMINAL, per start family: a print's FAILED
#: (how it ended is the terminal's to classify); an eject sweep's FINISH or FAILED (a short motion-only sweep
#: can end between two reads). A print's FINISH with its own id is NOT its terminal here: the H2D sits at
#: FINISH ~50 s after it took ``project_file`` before PREPARE (#1078), so that word is a landing
#: (:data:`Reading.LANDED`); the print's real end reaches the start through its terminal event
#: (:func:`_start_ended`).
_START_TERMINAL_WORDS: Mapping[Family, frozenset[str]] = {
    Family.PRINT_START: frozenset({"FAILED"}),
    Family.EJECT_START: frozenset({"FINISH", "FAILED"}),
}
#: The families of a person's act that take over the farm's job control.
_JOB_CONTROL: frozenset[Family] = frozenset(
    {Family.RESUME, Family.PAUSE, Family.STOP, Family.POWER_LOSS_RESUME, Family.DIALOG}
)

Cell = Callable[[ActionFacts, Running, "Event"], Outcome]

#: The REFUSED reason of a power-loss resume the firmware declared failed (``0300_400D``,
#: :func:`_power_loss_report`) — the one spelling the power-loss lane reads its hold copy off.
POWER_LOSS_RESUME_FAILED = "the printer reported Resume failed after power loss (0300_400D)"

#: The VOID reason of an AMS motion command whose own change cycle ran and ended without completing
#: (:func:`_motion_report`) — the one spelling the recovery driver reads ``acted`` off.
UNFINISHED_CYCLE = "the AMS ran the command's change and it ended without completing"


def _ack_answers(facts: ActionFacts, running: Running, event: Event) -> Outcome:
    """An ACK-answered verb: success → REACHED, any other result → REFUSED with the printer's reason."""
    assert isinstance(event, Acked)
    running.acked = event.succeeded
    if event.succeeded:
        return Outcome(Answer.REACHED)
    return Outcome(Answer.REFUSED, event.reason or "the printer refused the command")


def _ack_then_state(facts: ActionFacts, running: Running, event: Event) -> Outcome:
    """A verb answered by the state it was sent for: a fail ACK refuses it, a success is a reading."""
    assert isinstance(event, Acked)
    running.acked = event.succeeded
    if not event.succeeded:
        return Outcome(Answer.REFUSED, event.reason or "the printer refused the command")
    return Outcome(Reading.ACKED)


def _nothing(facts: ActionFacts, running: Running, event: Event) -> Outcome:
    """This event says nothing about this family's action."""
    return _NOTHING


def _reading_of(event: Event) -> WireReading:
    """The status report a ``reported`` or ``session_began`` event carries."""
    assert isinstance(event, (Reported, SessionBegan))
    return event.reading


def _unacked_at_a_new_session(facts: ActionFacts, running: Running, event: Event) -> Outcome:
    """A LATER session began (or the farm restarted) with this action unacknowledged: the printer may never
    have received it, and its ACK can no longer arrive — VOID. An acknowledged action keeps waiting for the
    state it was sent for (the firmware's queue survives a network blip), and so does a start whose job id
    the printer reported (``Running.start_landed``): only a printer that received it can report that id."""
    assert isinstance(event, SessionBegan)
    if running.acked is not None or running.start_landed:
        return _NOTHING
    if running.sent_epoch is not None and event.connection_epoch <= running.sent_epoch:
        return _NOTHING
    return Outcome(Answer.VOID, "a new MQTT session began with the command unacknowledged")


def _at_session_begin(report: Cell) -> Cell:
    """A family's ``session_began`` cell. The session's first fresh report is read FIRST, by the family's
    own ``reported`` cell: an unacknowledged command the printer carried out reads REACHED (a resume now
    RUNNING on its job, a stop on a printer that holds no job, a start whose job is reported, a load whose
    target feeds), and a reboot, another job or the job's end answers it as on any report. Only when that
    reading answers nothing final does an unacknowledged command VOID (:func:`_unacked_at_a_new_session`).
    The other order voided a start the printer was running, and the unit was requeued over its own
    print (the FOREIGN class)."""

    def cell(facts: ActionFacts, running: Running, event: Event) -> Outcome:
        now = report(facts, running, event)
        if now.final:
            return now
        new_session = _unacked_at_a_new_session(facts, running, event)
        return new_session if new_session.final else now

    return cell


def _person_takes_job_control(facts: ActionFacts, running: Running, event: Event) -> Outcome:
    """A person's job-control command after a FARM action of job control takes it over."""
    assert isinstance(event, PersonActed)
    if facts.actor == ACTOR_FARM and event.family in _JOB_CONTROL:
        return Outcome(Answer.TAKEN_OVER, "an operator command took over")
    return _NOTHING


def _person_stops_the_start(facts: ActionFacts, running: Running, event: Event) -> Outcome:
    """A person's STOP while the farm's start is still unanswered takes the dispatch over (2026-10-11):
    the printer reported no job of ours, so the stop is the person's answer to a dispatch that did not
    land — the unit goes back to the queue (``dispatch_start``) instead of waiting on a start the person
    has ended. Any other command of theirs says nothing about a start."""
    assert isinstance(event, PersonActed)
    if facts.actor == ACTOR_FARM and event.family is Family.STOP:
        return Outcome(Answer.TAKEN_OVER, "an operator stopped the dispatch")
    return _NOTHING


def _job_of(facts: ActionFacts, reading: WireReading) -> Literal["same", "other", "unknown"]:
    return same_job(reading.subtask_id, facts.job_id)


def _rebooted_since_send(running: Running, reading: WireReading) -> bool:
    """Did the printer reboot UNDER this action? The power-loss prompt stands on this report and did not
    stand at the send (``Running.rebooted_at_send``): a command sent while the prompt already stood was
    sent AFTER that reboot, whose emptied queue it is not in. Unknown at the send (a restart) reads as
    under it — the prompt is the only witness left."""
    return reading.rebooted and running.rebooted_at_send is not True


def _state_report(want: frozenset[str]) -> Cell:
    """A job-control verb answered by the job word it was sent for (``want``), on the job it was sent
    against: a reboot voids it, another job takes it over, a job-over word voids it."""

    def cell(facts: ActionFacts, running: Running, event: Event) -> Outcome:
        reading = _reading_of(event)
        if not reading.live:
            return _NOTHING
        if _rebooted_since_send(running, reading):
            return Outcome(Answer.VOID, "the printer rebooted")
        if _job_of(facts, reading) == "other":
            return Outcome(Answer.TAKEN_OVER, "another job is on the printer")
        if reading.gcode_state in want:
            return Outcome(Answer.REACHED)
        if reading.gcode_state in JOB_OVER_STATES:
            return Outcome(Answer.VOID, f"the job ended ({reading.gcode_state})")
        return _NOTHING

    return cell


def _power_loss_report(facts: ActionFacts, running: Running, event: Event) -> Outcome:
    """The power-loss resume: RUNNING reaches it, however late; the firmware's own "Resume failed after
    power loss" (``0300_400D``) refuses it — the catalog's verdict on the command, after which a second
    resume cannot un-fail the firmware's recovery; the reboot prompt is its PRECONDITION, never a void."""
    reading = _reading_of(event)
    if not reading.live:
        return _NOTHING
    if _job_of(facts, reading) == "other":
        return Outcome(Answer.TAKEN_OVER, "another job is on the printer")
    if reading.gcode_state == "RUNNING":
        return Outcome(Answer.REACHED)
    if reading.resume_failed:
        return Outcome(Answer.REFUSED, POWER_LOSS_RESUME_FAILED)
    if reading.gcode_state in JOB_OVER_STATES:
        return Outcome(Answer.VOID, f"the job ended ({reading.gcode_state})")
    return _NOTHING


def _job_ended_voids(facts: ActionFacts, running: Running, event: Event) -> Outcome:
    """The job a resume / pause was sent against ended — however late the terminal: VOID."""
    assert isinstance(event, Ended)
    if same_job(event.job_id, facts.job_id) == "other":
        return _NOTHING
    return Outcome(Answer.VOID, f"the job ended ({event.status})")


def _stop_report(facts: ActionFacts, running: Running, event: Event) -> Outcome:
    """A stop is taken when the printer holds no job — however late (014-H2S 2026-10-05: 56–62 s, where a
    30 s window answered both stops ``not_taken``); another job on the printer means the stopped job is
    over too. A reboot preempts it (the printer may offer the job back): VOID."""
    reading = _reading_of(event)
    if not reading.live:
        return _NOTHING
    if _rebooted_since_send(running, reading):
        return Outcome(Answer.VOID, "the printer rebooted")
    if reading.gcode_state in JOB_OVER_STATES or _job_of(facts, reading) == "other":
        return Outcome(Answer.REACHED)
    return _NOTHING


def _stop_ended(facts: ActionFacts, running: Running, event: Event) -> Outcome:
    """The terminal of the stopped job (live or reconciled) reaches the stop."""
    assert isinstance(event, Ended)
    if same_job(event.job_id, facts.job_id) == "other":
        return _NOTHING
    return Outcome(Answer.REACHED)


def _start_report(facts: ActionFacts, running: Running, event: Event) -> Outcome:
    """A start is REACHED only when the printer reports the DISPATCHED job's own id in an ACTIVE job word
    (:data:`_ACTIVE_JOB_STATES`) or in that job's terminal word (:data:`_START_TERMINAL_WORDS`: a print's
    FAILED; an eject sweep's FINISH / FAILED). The id is minted at the send (``BambuMQTTClient.start_print``'s
    submission id), so only a printer that received the start can report it.

    The same id in any other word (IDLE, or a print's FINISH) is the non-final :data:`Reading.LANDED`
    (``Running.start_landed``): the printer took the file and has not begun the job. Production's
    ``_watchdog_print_start`` Phase B at ``337f4954`` (#1678): a printer that accepts ``project_file`` and
    stays IDLE/FINISH has not started a print, and the any-word rule that reached it stranded the unit as
    ``printing`` forever. The landing's self-heal bound is the dispatch lane's
    (``dispatch_start._START_ACTIVE_SELF_HEAL``); an eject start keeps the bound it was sent with.

    A reboot voids it. Another job took the printer only when an ACTIVE job word carries an id positively
    other than BOTH the dispatched id and the id the printer reported at the send (``prior_job_id``). A
    PREPARE / SLICING frame still carrying the previous job's id answers nothing, and so does any frame
    when the printer reported no id at the send."""
    reading = _reading_of(event)
    if not reading.live:
        return _NOTHING
    ours = _job_of(facts, reading) == "same"
    if ours and (
        reading.gcode_state in _ACTIVE_JOB_STATES or reading.gcode_state in _START_TERMINAL_WORDS[facts.family]
    ):
        return Outcome(Answer.REACHED)
    if _rebooted_since_send(running, reading):
        return Outcome(Answer.VOID, "the printer rebooted")
    if ours:
        running.start_landed = True
        return Outcome(Reading.LANDED)
    if (
        reading.gcode_state in _ACTIVE_JOB_STATES
        and _job_of(facts, reading) == "other"
        and same_job(reading.subtask_id, facts.prior_job_id) == "other"
    ):
        return Outcome(Answer.TAKEN_OVER, "another job started on the printer")
    return _NOTHING


def _start_ended(facts: ActionFacts, running: Running, event: Event) -> Outcome:
    """The dispatched job's own terminal, live or reconciled: it started (how it ended is the terminal's to
    classify), a landed start included.

    And the BUSY job's terminal: a start sent while the printer was running another job (its job word
    at the send ACTIVE, ``facts.entry``) that the printer reported as the job it held
    (``prior_job_id``) never ran — the firmware silently ignores a ``project_file`` sent while it is
    busy (2026-08-30: three manual ejects dispatched into prints that had just started), so when that
    job ends with ours never reported the start is VOID: the event that answers the shape, where a
    start deadline used to guess at it."""
    assert isinstance(event, Ended)
    if same_job(event.job_id, facts.job_id) == "same":
        return Outcome(Answer.REACHED)
    if (facts.entry or "").upper() in _ACTIVE_JOB_STATES and same_job(event.job_id, facts.prior_job_id) == "same":
        return Outcome(Answer.VOID, "the printer was busy with another job and never started it")
    return _NOTHING


def _motion_report(facts: ActionFacts, running: Running, event: Event) -> Outcome:
    """An AMS motion command answered by the toolhead (:func:`ran` — the one rule): a load reached when the
    active feeder is the target; an unload reached when the active extruder, loaded at the send, reads
    empty — or, sent with nothing loaded, reads empty once the printer accepted it (or across a restart,
    where its ACK cannot be re-read). A reboot voids it: the AMS's queue is gone. A command sent OUTSIDE a
    filament change whose own change cycle RAN (reported inside a change since the send) and ENDED
    without it reaching is void too (:data:`UNFINISHED_CYCLE`): the AMS ran it, it did not complete, and
    it never will — the recovery driver reads that ending as ``acted``. The end is read only on a push
    that APPLIED A FRESH AMS BLOCK since the change was last seen (``WireReading.ams_block_at``): the job
    word's ``ams_status`` rides the print level and can leave the change on a push that carries no
    ``ams`` block, so ``tray_now`` — the landing — may arrive one push later; voiding there would void a
    load that completed."""
    reading = _reading_of(event)
    if not reading.ams.fresh:
        return _NOTHING
    if _rebooted_since_send(running, reading):
        return Outcome(Answer.VOID, "the printer rebooted — the AMS's queue is gone")
    loaded = entry_loaded(facts.entry)
    if ran(facts.command, facts.target, loaded, reading.ams.feed) is True:
        return Outcome(Answer.REACHED)
    sent_outside = facts.entry is not None and not facts.entry.startswith("mid_change")
    in_change = ams_mid_filament_change(reading.ams)
    if sent_outside and in_change:
        running.change_block_at = reading.ams_block_at
    elif (
        sent_outside
        and running.change_block_at is not None
        and reading.ams_block_at != running.change_block_at
        and not (facts.command == "unload" and loaded is not True and reading.ams.feed.kind == "empty")
    ):
        return Outcome(Answer.VOID, UNFINISHED_CYCLE)
    if (
        facts.command == "unload"
        and loaded is not True
        and reading.ams.feed.kind == "empty"
        and (running.acked is True or running.sent_epoch is None)
    ):
        return Outcome(Answer.REACHED)
    if running.acked is True:
        return Outcome(Reading.HELD)
    return _NOTHING


def _motion_landed(facts: ActionFacts, running: Running, event: Event) -> Outcome:
    """The feed state's landing of the commanded tray reaches a load."""
    assert isinstance(event, Landed)
    if facts.command == "load" and valid_feeder(facts.target) == event.tray:
        return Outcome(Answer.REACHED)
    return _NOTHING


def _reboot_voids(facts: ActionFacts, running: Running, event: Event) -> Outcome:
    """An ACK-answered verb still unanswered when the printer rebooted: VOID."""
    reading = _reading_of(event)
    if reading.live and _rebooted_since_send(running, reading):
        return Outcome(Answer.VOID, "the printer rebooted")
    return _NOTHING


_resume_report = _state_report(frozenset({"RUNNING"}))
_pause_report = _state_report(frozenset({"PAUSE"}))


# THE record's table: (family, event kind) → cell. EXPLICIT, every pair spelled, no default — a missing
# pair RAISES at the lookup (:func:`_cell`), and ``test_command_outcome`` pins totality.
_TABLE: dict[tuple[Family, EventKind], Cell] = {
    # resume / pause: the job word they were sent for, on the job they were sent against.
    (Family.RESUME, "acked"): _ack_then_state,
    (Family.RESUME, "reported"): _resume_report,
    (Family.RESUME, "session_began"): _at_session_begin(_resume_report),
    (Family.RESUME, "landed"): _nothing,
    (Family.RESUME, "ended"): _job_ended_voids,
    (Family.RESUME, "person_acted"): _person_takes_job_control,
    (Family.PAUSE, "acked"): _ack_then_state,
    (Family.PAUSE, "reported"): _pause_report,
    (Family.PAUSE, "session_began"): _at_session_begin(_pause_report),
    (Family.PAUSE, "landed"): _nothing,
    (Family.PAUSE, "ended"): _job_ended_voids,
    (Family.PAUSE, "person_acted"): _person_takes_job_control,
    (Family.POWER_LOSS_RESUME, "acked"): _ack_then_state,
    (Family.POWER_LOSS_RESUME, "reported"): _power_loss_report,
    (Family.POWER_LOSS_RESUME, "session_began"): _at_session_begin(_power_loss_report),
    (Family.POWER_LOSS_RESUME, "landed"): _nothing,
    (Family.POWER_LOSS_RESUME, "ended"): _job_ended_voids,
    (Family.POWER_LOSS_RESUME, "person_acted"): _person_takes_job_control,
    # stop: the job's end, however late.
    (Family.STOP, "acked"): _ack_then_state,
    (Family.STOP, "reported"): _stop_report,
    (Family.STOP, "session_began"): _at_session_begin(_stop_report),
    (Family.STOP, "landed"): _nothing,
    (Family.STOP, "ended"): _stop_ended,
    (Family.STOP, "person_acted"): _person_takes_job_control,
    # starts: the dispatched job reported ACTIVE, or its terminal; its id in any other word is a landing
    # (the non-final ``Reading.LANDED``).
    (Family.PRINT_START, "acked"): _ack_then_state,
    (Family.PRINT_START, "reported"): _start_report,
    (Family.PRINT_START, "session_began"): _at_session_begin(_start_report),
    (Family.PRINT_START, "landed"): _nothing,
    (Family.PRINT_START, "ended"): _start_ended,
    (Family.PRINT_START, "person_acted"): _person_stops_the_start,
    (Family.EJECT_START, "acked"): _ack_then_state,
    (Family.EJECT_START, "reported"): _start_report,
    (Family.EJECT_START, "session_began"): _at_session_begin(_start_report),
    (Family.EJECT_START, "landed"): _nothing,
    (Family.EJECT_START, "ended"): _start_ended,
    (Family.EJECT_START, "person_acted"): _nothing,
    # AMS motion: the toolhead's answer; a terminal does not answer it (held commands run after the
    # print's terminal — 012-H2S 2026-09-23), and neither does a person's AMS command (K11, production
    # ``337f4954``): a farm command the AMS accepted still runs when it runs, so it stays the farm's —
    # the row stays open on it, and its landing refills the toolhead (``spool_recovery``'s D1). The
    # driver reads the person's click as its own takeover (``DriverReading.operator_commanded``).
    (Family.AMS_MOTION, "acked"): _ack_then_state,
    (Family.AMS_MOTION, "reported"): _motion_report,
    (Family.AMS_MOTION, "session_began"): _at_session_begin(_motion_report),
    (Family.AMS_MOTION, "landed"): _motion_landed,
    (Family.AMS_MOTION, "ended"): _nothing,
    (Family.AMS_MOTION, "person_acted"): _nothing,
    # ACK-answered verbs: the release levers, the dialog buttons, the upstream config verbs.
    (Family.AMS_LEVER, "acked"): _ack_answers,
    (Family.AMS_LEVER, "reported"): _reboot_voids,
    (Family.AMS_LEVER, "session_began"): _at_session_begin(_reboot_voids),
    (Family.AMS_LEVER, "landed"): _nothing,
    (Family.AMS_LEVER, "ended"): _nothing,
    (Family.AMS_LEVER, "person_acted"): _nothing,
    (Family.DIALOG, "acked"): _ack_answers,
    (Family.DIALOG, "reported"): _reboot_voids,
    (Family.DIALOG, "session_began"): _at_session_begin(_reboot_voids),
    (Family.DIALOG, "landed"): _nothing,
    (Family.DIALOG, "ended"): _nothing,
    (Family.DIALOG, "person_acted"): _nothing,
    (Family.ACK_ONLY, "acked"): _ack_answers,
    (Family.ACK_ONLY, "reported"): _reboot_voids,
    (Family.ACK_ONLY, "session_began"): _at_session_begin(_reboot_voids),
    (Family.ACK_ONLY, "landed"): _nothing,
    (Family.ACK_ONLY, "ended"): _nothing,
    (Family.ACK_ONLY, "person_acted"): _nothing,
    # observed: recorded answered at the insert (a landing nobody commanded) — never outstanding.
    (Family.OBSERVED, "acked"): _nothing,
    (Family.OBSERVED, "reported"): _nothing,
    (Family.OBSERVED, "session_began"): _nothing,
    (Family.OBSERVED, "landed"): _nothing,
    (Family.OBSERVED, "ended"): _nothing,
    (Family.OBSERVED, "person_acted"): _nothing,
}


def _cell(family: Family, kind: EventKind) -> Cell:
    try:
        return _TABLE[(family, kind)]
    except KeyError:
        raise LookupError(
            f"command_outcome: no outcome cell for ({family.value}, {kind}) — every pair needs its own row"
        ) from None


def read(facts: ActionFacts, running: Running, event: Event) -> Outcome:
    """THE record's reading of one event for one outstanding action. Pure over its arguments with ONE
    declared exception: an ACK is folded into the caller-owned ``running`` facts. No clock: an action no
    event answers stays outstanding."""
    return _cell(facts.family, _kind(event))(facts, running, event)


__all__ = [
    "ACTOR_FARM",
    "ACTOR_OPERATOR",
    "ACTOR_PRINTER",
    "COMMANDS",
    "DRIVER_STATES",
    "JOB_OVER_STATES",
    "NO_ECHO_REASON",
    "OPERATOR_ACK_S",
    "UNLOAD_GRACE_S",
    "Acked",
    "ActionFacts",
    "Actor",
    "AmsWireSnapshot",
    "Answer",
    "Awaiting",
    "CommandSpec",
    "DriverReading",
    "Ended",
    "Event",
    "Family",
    "Landed",
    "MotionCommand",
    "MotionReading",
    "Observation",
    "Outcome",
    "PendingSettle",
    "PersonActed",
    "Posture",
    "PostureFamily",
    "Reading",
    "Reported",
    "Running",
    "SessionBegan",
    "TakeoverToken",
    "UNFINISHED_CYCLE",
    "WireReading",
    "classify",
    "clock",
    "echoes",
    "entry_loaded",
    "log_answer",
    "pending_settles",
    "posture",
    "posture_family",
    "ran",
    "read",
    "rebooted",
    "snapshot",
    "spec_of",
    "takeover",
]
