"""The toolhead FEED STATE — ONE owner, a LEAF (2026-10-10).

"Is filament reaching the nozzle, and if not, is the FIRMWARE about to put it back or is the print
on AIR?" had no owner: ~12 sites re-derived it from single-frame snapshots of separate wire fields,
each with its own rule. A per-push air detector acted on the FIRST frame of a firmware runout
AUTO-SWITCH — RUNNING, ``tray_now = 255``, the change posture (``ams_status_main == 1``) not yet
raised — and broke into 3 of that day's 7 switches (005/015/001-H2S: paused, refilled, resumed
into the firmware's own change; two were left PAUSED mid-print). The firmware's runout sequence
spans minutes, and no snapshot sees it whole. As measured (H2S, 2026-10-10):

    the drained slot's exist bit clears while the roll's TAIL still feeds (2.5-4.7 min) → on some
    printers ``tray_now`` PRE-FLIPS to the backup ~1 s later (the target can be an EMPTY slot) →
    ``tray_now`` reads 255 while RUNNING with ``ams_status_main`` not yet 1 (the tray field leads
    the posture by up to ~3 s, shape 41) → HMS 0x00030001 1-3 s later → 0x00030002 ~15 s later
    with the backup load landing. The layer does not advance during the change; printing on air
    DOES advance layers (shapes 39, 46).

So ONE per-printer machine owns the question: :class:`FeedTracker`, composed into the MQTT client,
stepped ONCE per status push with ONE :class:`FeedFrame` (``BambuMQTTClient`` is the only caller of
:meth:`FeedTracker.step` — ``test_code_quality``), and read by everyone as the frozen
:class:`FeedReading` it returns (``BambuMQTTClient.feed()``).

* ONE total transition table, :data:`_TRANSITIONS` (phase × signal → handler), in the house idiom
  of ``pause_recovery._OWED`` and ``spool_recovery._RESUME_DECISION``: a missing cell RAISES at the
  lookup, and ``test_feed_state`` proves the table complete so that raise can never fire on the
  MQTT thread. A pure classifier, :func:`_classify`, with a FIXED priority order, turns the frame
  and the tracker's previous facts into ONE :class:`FeedSignal`.
* The phase is the ACTIVE extruder's; the last feeder, the drained trays and the feeder segments
  are kept PER EXTRUDER (H2C: two extruders, ``nozzles[0]`` = right/main).
* Events are EDGES, each stamped with a PROCESS-GLOBAL sequence number: a reconnect builds a new
  client, and a per-instance counter would restart under a consumer's cursor.

Imports stdlib, ``tray_fields`` and ``hms_errors`` only (``test_import_graph``): pure, sync and
TOTAL — it rides the status callback (invariant 10), so :meth:`FeedTracker.step` never raises.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import ClassVar

from backend.app.services import hms_errors, tray_fields

logger = logging.getLogger(__name__)

# The process-global event sequence. ``itertools.count`` advances atomically under the GIL, and
# every printer's client steps its tracker on its own MQTT network thread.
_SEQUENCE = itertools.count(1)


class FeedPhase(str, Enum):
    """Where the ACTIVE extruder's feed stands — CLOSED; every reader maps it, none re-derives it.

    * ``IDLE`` — no job (the client's job-lifecycle gate is shut).
    * ``START_BLOCK`` — a job before its first printed layer (RUNNING or PAUSE): a 255 there is
      the start block's own load still to come.
    * ``END_RETRACT`` — RUNNING at the last layer (``total_layers`` known): the end-of-print retract.
    * ``FED`` — fed from an AMS tray or the external holder.
    * ``TAIL`` — the feeding roll ran dry at the AMS (its tray reads trusted-absent) and its tail
      still feeds; a ``tray_now`` pre-flip to the backup stays INSIDE the tail.
    * ``CHANGING`` — the firmware owns the next motion (:class:`ChangeCause`).
    * ``RUNOUT_HELD`` — PAUSEd on a runout demand: the firmware asks for filament.
    * ``EMPTY_UNCONFIRMED`` — RUNNING, empty, no firmware sequence YET.
    * ``AIR`` — RUNNING, empty, and printing: a layer advanced while empty (air advances layers; a
      firmware change never does — shape 47). No count of quiet reports decides it (2026-10-11: a
      frame count is a clock).
    * ``EMPTY_PAUSED`` — PAUSEd empty, no demand, no change posture.
    * ``UNKNOWN`` — not a live reading, or no readable feed.
    """

    IDLE = "idle"
    START_BLOCK = "start_block"
    END_RETRACT = "end_retract"
    FED = "fed"
    TAIL = "tail"
    CHANGING = "changing"
    RUNOUT_HELD = "runout_held"
    EMPTY_UNCONFIRMED = "empty_unconfirmed"
    AIR = "air"
    EMPTY_PAUSED = "empty_paused"
    UNKNOWN = "unknown"


class ChangeCause(str, Enum):
    """Why the firmware owns the next motion (a ``CHANGING`` phase). Every cause has the SAME exit
    to AIR — a layer advance while empty — so a false or missed TAIL can never hide air to the
    FINISH: printing on air advances layers, a change never does.

    * ``POSTURE`` — the change posture reads 1 (``bambu_mqtt.ams_mid_filament_change``).
    * ``RUNOUT_SWITCH`` — the toolhead read empty out of a TAIL: the firmware's backup switch.
    * ``RUNOUT_RESUME`` — RUNNING out of a RUNOUT_HELD: the firmware loads the refilled slot.
    * ``TOOL_CHANGE`` — the active extruder changed (dual nozzle).
    """

    POSTURE = "posture"
    RUNOUT_SWITCH = "runout_switch"
    RUNOUT_RESUME = "runout_resume"
    TOOL_CHANGE = "tool_change"


class JobBoundary(str, Enum):
    """The job boundary a push drove, as the client's lifecycle decided it — the points where the
    client resets its peaks (``_update_state``): a print start or file change (``NEW_JOB``), and
    the restart-recovery ATTACH of a job it never saw begin."""

    NONE = "none"
    NEW_JOB = "new_job"
    ATTACH = "attach"


class FeedSignal(str, Enum):
    """What ONE frame says, given the tracker's previous facts — :func:`_classify`'s closed answer,
    in its priority order (first match wins)."""

    # 1. Not a reading of the printer now: UNKNOWN, re-seeded.
    NOT_LIVE = "not_live"
    # 2. The job-lifecycle gate is shut.
    NO_JOB = "no_job"
    # 3. A job, but neither RUNNING nor PAUSE (PREPARE, P2S's transient during an auto-fallback
    #    #957, a FINISH / FAILED before its completion fires): the phase stands.
    SETUP = "setup"
    # 4. Before the first printed layer.
    BEFORE_FIRST_LAYER = "before_first_layer"
    # 5. RUNNING at the last layer.
    LAST_LAYER = "last_layer"
    # 6. PAUSE.
    PAUSED_DEMAND = "paused_demand"
    PAUSED_POSTURE = "paused_posture"
    PAUSED_FED_DRAINED = "paused_fed_drained"  # fed, and the held feeder still reads absent
    PAUSED_FED = "paused_fed"
    PAUSED_EMPTY = "paused_empty"
    PAUSED_UNREADABLE = "paused_unreadable"
    # 7. RUNNING, a printed layer below the last.
    TOOL_CHANGE = "tool_change"
    POSTURE = "posture"
    FED_DRAINED_SWITCHED = "fed_drained_switched"  # held feeder absent; 0x30002 names a switch to y
    FED_DRAINED = "fed_drained"  # the held feeder reads trusted-absent
    FED_RESTORED = "fed_restored"  # y IS the held feeder, and it reads present again
    FED_SWITCHED = "fed_switched"  # 0x30002 standing, y is not the held feeder
    FED_ABSENT = "fed_absent"  # y itself reads trusted-absent
    FED_PLAIN = "fed_plain"
    EMPTY_ADVANCED = "empty_advanced"  # the layer advanced past the open empty episode's layer
    EMPTY_HELD = "empty_held"  # empty, the layer has not advanced: the firmware may still feed it
    UNREADABLE = "unreadable"


_RUNNING = "RUNNING"
_PAUSE = "PAUSE"


def _feeder(feed: tray_fields.ExtruderFeed) -> int | None:
    """A real feeder as the projections spell it — the AMS tray, the external holder as
    ``tray_fields.TRAY_NOW_EXTERNAL_SPOOL`` — or ``None`` for nothing fed / nothing read."""
    if feed.kind == "fed":
        return feed.tray
    if feed.kind == "external":
        return tray_fields.TRAY_NOW_EXTERNAL_SPOOL
    return None


@dataclass(frozen=True)
class FeedFrame:
    """What ONE status push says about the feed — built ONCE per push by the client
    (:meth:`of`), after every field of the push is applied.

    ``live`` is the client's ``live_reading.reads_live(state)`` (a pre-first-report frame of a new
    session carries the old session's merge); ``epoch`` the session (``connection_epoch``).
    ``feed`` is EVERY extruder's feed and the active one (``tray_fields.toolhead_feed``).
    ``posture``: whether the merged value reads the change posture
    (``bambu_mqtt.ams_mid_filament_change`` stays its origin; the client passes the bool). ``presence`` is the trusted per-tray presence
    (``tray_fields.tray_presence_map``; absent means ``is False`` only). The runout words standing,
    each read through ``hms_errors``: the slotted demand, whether any demand stands (the
    slot-agnostic hold included), the pull-back notice and the auto-switch report. ``layer`` is
    the job-gated layer (``job_peaks().layer_num``, behind the stale-predecessor gate);
    ``job_active`` the lifecycle gate (``_was_running and not _completion_triggered`` — P2S leaves
    RUNNING during an auto-fallback, #957).
    """

    live: bool
    epoch: int
    gcode_state: str
    feed: tray_fields.ToolheadFeed
    posture: bool
    presence: Mapping[tuple[int, int], bool | None]
    runout_demand: tuple[int, int] | None
    runout_demand_standing: bool
    pull_back: bool
    auto_switched: bool
    layer: int
    total_layers: int
    job_active: bool
    boundary: JobBoundary = JobBoundary.NONE

    @classmethod
    def of(
        cls,
        state: object,
        *,
        live: bool,
        posture: bool,
        layer: int,
        job_active: bool,
        boundary: JobBoundary,
    ) -> FeedFrame:
        """THE one build of a frame off a printer state (duck-typed, like every wire reader here):
        the wire's fields through their owners, the client's own facts as arguments."""
        hms_list = getattr(state, "hms_errors", None)
        if not isinstance(hms_list, list):
            hms_list = []
        return cls(
            live=live,
            epoch=tray_fields.parse_int_field(getattr(state, "connection_epoch", None)) or 0,
            gcode_state=str(getattr(state, "state", None) or "").upper(),
            feed=tray_fields.toolhead_feed(state),
            posture=posture,
            presence=tray_fields.tray_presence_map(getattr(state, "raw_data", None)),
            runout_demand=hms_errors.current_runout_demand(hms_list),
            runout_demand_standing=hms_errors.runout_demand_standing(hms_list),
            pull_back=hms_errors.pull_back_standing(hms_list),
            auto_switched=hms_errors.auto_switch_standing(hms_list),
            layer=layer,
            total_layers=tray_fields.parse_int_field(getattr(state, "total_layers", None)) or 0,
            job_active=job_active,
            boundary=boundary,
        )

    def reads_absent(self, tray: int | None) -> bool:
        """Does ``tray`` read TRUSTED-absent — ``tray_presence`` ``is False``, never unknown (fails
        open)? The external holder and an unaddressable tray are no AMS slot: never absent."""
        if tray is None:
            return False
        return self.presence.get(tray_fields.decode_global_tray(tray)) is False  # type: ignore[call-overload]

    def reads_present(self, tray: int | None) -> bool:
        """Does ``tray`` read present (``is True``)?"""
        if tray is None:
            return False
        return self.presence.get(tray_fields.decode_global_tray(tray)) is True  # type: ignore[call-overload]

    @property
    def runout_word(self) -> bool:
        """Is any runout word standing — a demand (slotted or not), the pull-back, the switch?"""
        return self.runout_demand_standing or self.pull_back or self.auto_switched


# --- events --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RollDrained:
    """A feeding tray of ``extruder`` turned trusted-absent while that extruder was fed and the
    print RUNNING — the roll ran dry at the AMS (for the active extruder: entering TAIL). Once per
    drained tray per extruder, until that tray reads present again."""

    kind: ClassVar[str] = "roll_drained"

    seq: int
    extruder: int
    tray: int


@dataclass(frozen=True)
class LoadLanded:
    """The active extruder's EFFECTIVE feeder became a real AMS tray other than the previous
    push's, within one session and one active extruder — in ANY gcode state (a person's Load on an
    idle printer is repair evidence). A pre-flip inside a TAIL is no landing (the landing fires when
    the backup actually feeds).
    ``from_phase`` is the phase before this push. WHOSE load it was is the record's
    (``printer_action``: an outstanding farm or operator load of the tray, else ``observed_load``)."""

    kind: ClassVar[str] = "load_landed"

    seq: int
    extruder: int
    tray: int
    from_phase: FeedPhase


@dataclass(frozen=True)
class AutoSwitched:
    """The firmware's runout switch, read off the wire sequence ONLY:
    ``TAIL(departed) → CHANGING(…, departed) → FED(arrived)``, ``arrived != departed``. Never from
    an HMS word — the HMS-edge lane already stamps on 0x00030002."""

    kind: ClassVar[str] = "auto_switched"

    seq: int
    extruder: int
    departed: int
    arrived: int


@dataclass(frozen=True)
class AirBegan:
    """The print entered AIR; ``layer`` is the layer its empty episode began at (never below 1)."""

    kind: ClassVar[str] = "air_began"

    seq: int
    layer: int


FeedEvent = RollDrained | LoadLanded | AutoSwitched | AirBegan


# --- the reading ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtruderReading:
    """One extruder's record this job: its last real feeder (a tray, or the external holder as
    254), the trays drained under it, and its feeder segments ``(feeder, from_layer)``."""

    extruder: int
    last_fed: int | None
    drained: frozenset[int]
    segments: tuple[tuple[int, int], ...]


_NO_EXTRUDER = ExtruderReading(extruder=-1, last_fed=None, drained=frozenset(), segments=())


@dataclass(frozen=True)
class FeedReading:
    """THE snapshot every reader asks — frozen, replaced whole on every step.

    ``phase`` with its payload: ``cause`` / ``departed`` (CHANGING), ``tail`` (TAIL: the drained
    roll still feeding), ``held_slot`` (RUNOUT_HELD: the demanded ``(ams_id, tray_id)``, ``None``
    for the slot-agnostic hold). ``feeder`` is the active extruder's EFFECTIVE feeder this push —
    the active feed's tray (254 = the external holder), except in a TAIL, where it stays the
    draining roll (a pre-flip target is not fed yet). ``feeder_log`` is the job's chronological
    ``(extruder, feeder, from_layer)`` record under the lifecycle gate: a new entry when the
    effective feeder becomes a real feeder other than ``last_loaded`` (the backup's segment starts
    where its load LANDS, so a tail is charged to the drained roll). ``last_loaded`` is the last
    real feeder (-1 none), reset at a new job only. ``first_unfed_layer`` is the lowest layer the
    job PRINTED with nothing fed (``JobPeaks.first_unfed_layer``). The four events are each the
    LAST of its kind, ``None`` before the first; ``events`` are the ones THIS step produced, in
    their within-push order (drains, the landing, the switch, air) — what the client re-publishes
    (``printer_events.FeedEventReported``) and logs, one per event.
    """

    phase: FeedPhase
    active_extruder: int | None
    feeder: int | None
    cause: ChangeCause | None
    departed: int | None
    tail: int | None
    held_slot: tuple[int, int] | None
    extruders: tuple[ExtruderReading, ...]
    feeder_log: tuple[tuple[int, int, int], ...]
    last_loaded: int
    first_unfed_layer: int | None
    roll_drained: RollDrained | None
    load_landed: LoadLanded | None
    auto_switched: AutoSwitched | None
    air_began: AirBegan | None
    events: tuple[FeedEvent, ...] = ()

    def extruder(self, extruder_id: int) -> ExtruderReading:
        """One extruder's record; an empty one for an extruder this job never read."""
        return next((e for e in self.extruders if e.extruder == extruder_id), _NO_EXTRUDER)

    @property
    def tray_change_log(self) -> tuple[tuple[int, int], ...]:
        """The job's ``(feeder, from_layer)`` record in order — ``PrinterState.tray_change_log``'s
        one source (the client's projection)."""
        return tuple((feeder, layer) for _extruder, feeder, layer in self.feeder_log)


# --- the phase, its handlers and the table -------------------------------------------------------


@dataclass(frozen=True)
class _Phase:
    phase: FeedPhase
    cause: ChangeCause | None = None
    departed: int | None = None  # CHANGING: the roll that ran dry before it
    tail: int | None = None  # TAIL: the drained roll whose tail still feeds
    held_slot: tuple[int, int] | None = None  # RUNOUT_HELD: the demanded slot


_UNKNOWN = _Phase(FeedPhase.UNKNOWN)


@dataclass(frozen=True)
class _Facts:
    """The tracker's previous facts the classifier reads (and nothing else)."""

    active_extruder: int | None  # of the previous LIVE frame
    held: int | None  # the previous push's effective feeder while FED / TAIL on this extruder
    episode: int | None  # the open empty episode's run layer


@dataclass(frozen=True)
class _Step:
    """What a handler sees: the phase it leaves, the frame, and the held feeder."""

    prev: _Phase
    frame: FeedFrame
    held: int | None


_Handler = Callable[[_Step], _Phase]


def _stay(step: _Step) -> _Phase:
    return step.prev


def _unknown(_step: _Step) -> _Phase:
    return _UNKNOWN


def _idle(_step: _Step) -> _Phase:
    return _Phase(FeedPhase.IDLE)


def _start_block(_step: _Step) -> _Phase:
    return _Phase(FeedPhase.START_BLOCK)


def _end_retract(_step: _Step) -> _Phase:
    return _Phase(FeedPhase.END_RETRACT)


def _fed(_step: _Step) -> _Phase:
    return _Phase(FeedPhase.FED)


def _tail_held(step: _Step) -> _Phase:
    """FED(x) → TAIL(x): the feeding roll ran dry — even when this push already pre-flipped
    ``tray_now`` to the backup."""
    return _Phase(FeedPhase.TAIL, tail=step.held)


def _tail_self(step: _Step) -> _Phase:
    """A fed toolhead whose feeding tray already reads absent — the attach seed."""
    return _Phase(FeedPhase.TAIL, tail=_feeder(step.frame.feed.active))


def _runout_held(step: _Step) -> _Phase:
    return _Phase(FeedPhase.RUNOUT_HELD, held_slot=step.frame.runout_demand)


def _empty_paused(_step: _Step) -> _Phase:
    return _Phase(FeedPhase.EMPTY_PAUSED)


def _unconfirmed(_step: _Step) -> _Phase:
    return _Phase(FeedPhase.EMPTY_UNCONFIRMED)


def _air(_step: _Step) -> _Phase:
    return _Phase(FeedPhase.AIR)


def _carried_departed(prev: _Phase) -> int | None:
    """The drained roll a change carries forward: a TAIL's tray, or a CHANGING's own."""
    if prev.phase is FeedPhase.TAIL:
        return prev.tail
    if prev.phase is FeedPhase.CHANGING:
        return prev.departed
    return None


def _posture(step: _Step) -> _Phase:
    """RUNNING with the posture up: CHANGING(posture), carrying the departed roll."""
    return _Phase(FeedPhase.CHANGING, cause=ChangeCause.POSTURE, departed=_carried_departed(step.prev))


def _posture_kept(step: _Step) -> _Phase:
    """PAUSE with the posture up: an existing CHANGING keeps its cause and departed roll."""
    return step.prev if step.prev.phase is FeedPhase.CHANGING else _posture(step)


def _tool_change(_step: _Step) -> _Phase:
    return _Phase(FeedPhase.CHANGING, cause=ChangeCause.TOOL_CHANGE)


def _runout_switch(step: _Step) -> _Phase:
    """TAIL(x) → empty: the firmware's backup switch, departing x."""
    return _Phase(FeedPhase.CHANGING, cause=ChangeCause.RUNOUT_SWITCH, departed=step.prev.tail)


def _runout_resume(_step: _Step) -> _Phase:
    return _Phase(FeedPhase.CHANGING, cause=ChangeCause.RUNOUT_RESUME)


_FED_SIGNALS = (
    FeedSignal.FED_DRAINED_SWITCHED,
    FeedSignal.FED_DRAINED,
    FeedSignal.FED_RESTORED,
    FeedSignal.FED_SWITCHED,
    FeedSignal.FED_ABSENT,
    FeedSignal.FED_PLAIN,
)
_EMPTY_SIGNALS = (
    FeedSignal.EMPTY_ADVANCED,
    FeedSignal.EMPTY_HELD,
)

# The fed cells (7c), by the phase they leave. The held-feeder signals (drained / restored /
# switched) arise only out of FED and TAIL — the classifier reads no held feeder anywhere else —
# so the other rows answer them as plainly fed.
_FED_FROM_ELSEWHERE: dict[FeedSignal, _Handler] = {
    FeedSignal.FED_DRAINED_SWITCHED: _fed,
    FeedSignal.FED_DRAINED: _fed,
    FeedSignal.FED_RESTORED: _fed,
    FeedSignal.FED_SWITCHED: _fed,
    FeedSignal.FED_ABSENT: _tail_self,  # the attach seed — no event at an attach
    FeedSignal.FED_PLAIN: _fed,
}
_FED_FROM_FED: dict[FeedSignal, _Handler] = {
    FeedSignal.FED_DRAINED_SWITCHED: _tail_held,  # x drained: TAIL(x), whatever the push pre-flipped to
    FeedSignal.FED_DRAINED: _tail_held,
    FeedSignal.FED_RESTORED: _fed,
    FeedSignal.FED_SWITCHED: _fed,
    FeedSignal.FED_ABSENT: _fed,  # x did not drain: FED(y); y's own drain is the next push's TAIL
    FeedSignal.FED_PLAIN: _fed,
}
_FED_FROM_TAIL: dict[FeedSignal, _Handler] = {
    FeedSignal.FED_DRAINED_SWITCHED: _fed,  # the firmware REPORTED the switch: FED(y), no event
    FeedSignal.FED_DRAINED: _stay,  # the tail still feeds — a pre-flip stays inside it
    FeedSignal.FED_RESTORED: _fed,  # a false drain (a presence flap): FED(x)
    FeedSignal.FED_SWITCHED: _fed,
    FeedSignal.FED_ABSENT: _stay,  # a pre-flip onto an EMPTY slot is no feeder
    FeedSignal.FED_PLAIN: _stay,
}
_FED_FROM_CHANGING: dict[FeedSignal, _Handler] = {
    FeedSignal.FED_DRAINED_SWITCHED: _fed,
    FeedSignal.FED_DRAINED: _fed,
    FeedSignal.FED_RESTORED: _fed,
    FeedSignal.FED_SWITCHED: _fed,
    FeedSignal.FED_ABSENT: _fed,
    FeedSignal.FED_PLAIN: _fed,
}

# The empty cells (7d), by the phase they leave.
_EMPTY_UNOWNED: dict[FeedSignal, _Handler] = dict.fromkeys(_EMPTY_SIGNALS, _unconfirmed)
_EMPTY_FROM_TAIL: dict[FeedSignal, _Handler] = dict.fromkeys(_EMPTY_SIGNALS, _runout_switch)
_EMPTY_FROM_HELD: dict[FeedSignal, _Handler] = dict.fromkeys(_EMPTY_SIGNALS, _runout_resume)
# EMPTY_UNCONFIRMED and EVERY CHANGING cause: a layer advance while empty is air at once; any other
# empty report keeps waiting for the firmware — however many of them, however long (2026-10-11:
# "using time as a gate is not the correct way to discern status complete or incompleteness").
_EMPTY_CONFIRMING: dict[FeedSignal, _Handler] = {
    FeedSignal.EMPTY_ADVANCED: _air,
    FeedSignal.EMPTY_HELD: _stay,
}
_EMPTY_ON_AIR: dict[FeedSignal, _Handler] = dict.fromkeys(_EMPTY_SIGNALS, _stay)


def _row(
    phase: FeedPhase,
    *,
    paused_fed_drained: _Handler,
    fed: Mapping[FeedSignal, _Handler],
    empty: Mapping[FeedSignal, _Handler],
) -> dict[tuple[FeedPhase, FeedSignal], _Handler]:
    """ONE phase's whole line: the rules every phase shares (1-6, the tool change, the posture,
    an unreadable feed) and the phase's own PAUSE-fed, fed and empty cells."""
    line: dict[tuple[FeedPhase, FeedSignal], _Handler] = {
        (phase, FeedSignal.NOT_LIVE): _unknown,
        (phase, FeedSignal.NO_JOB): _idle,
        (phase, FeedSignal.SETUP): _stay,
        (phase, FeedSignal.BEFORE_FIRST_LAYER): _start_block,
        (phase, FeedSignal.LAST_LAYER): _end_retract,
        (phase, FeedSignal.PAUSED_DEMAND): _runout_held,
        (phase, FeedSignal.PAUSED_POSTURE): _posture_kept,
        (phase, FeedSignal.PAUSED_FED_DRAINED): paused_fed_drained,
        (phase, FeedSignal.PAUSED_FED): _fed,
        (phase, FeedSignal.PAUSED_EMPTY): _empty_paused,
        (phase, FeedSignal.PAUSED_UNREADABLE): _unknown,
        (phase, FeedSignal.TOOL_CHANGE): _tool_change,
        (phase, FeedSignal.POSTURE): _posture,
        (phase, FeedSignal.UNREADABLE): _unknown,
    }
    line.update({(phase, signal): handler for signal, handler in fed.items()})
    line.update({(phase, signal): handler for signal, handler in empty.items()})
    return line


# THE transition table — TOTAL over FeedPhase × FeedSignal (``test_feed_state`` pins it), one
# explicit line per phase, no default for a new phase or signal to inherit an answer from.
_TRANSITIONS: dict[tuple[FeedPhase, FeedSignal], _Handler] = {
    **_row(FeedPhase.IDLE, paused_fed_drained=_fed, fed=_FED_FROM_ELSEWHERE, empty=_EMPTY_UNOWNED),
    **_row(FeedPhase.START_BLOCK, paused_fed_drained=_fed, fed=_FED_FROM_ELSEWHERE, empty=_EMPTY_UNOWNED),
    **_row(FeedPhase.END_RETRACT, paused_fed_drained=_fed, fed=_FED_FROM_ELSEWHERE, empty=_EMPTY_UNOWNED),
    **_row(FeedPhase.FED, paused_fed_drained=_fed, fed=_FED_FROM_FED, empty=_EMPTY_UNOWNED),
    # PAUSE keeps a TAIL while its roll still reads absent.
    **_row(FeedPhase.TAIL, paused_fed_drained=_stay, fed=_FED_FROM_TAIL, empty=_EMPTY_FROM_TAIL),
    **_row(FeedPhase.CHANGING, paused_fed_drained=_fed, fed=_FED_FROM_CHANGING, empty=_EMPTY_CONFIRMING),
    **_row(FeedPhase.RUNOUT_HELD, paused_fed_drained=_fed, fed=_FED_FROM_ELSEWHERE, empty=_EMPTY_FROM_HELD),
    **_row(FeedPhase.EMPTY_UNCONFIRMED, paused_fed_drained=_fed, fed=_FED_FROM_ELSEWHERE, empty=_EMPTY_CONFIRMING),
    **_row(FeedPhase.AIR, paused_fed_drained=_fed, fed=_FED_FROM_ELSEWHERE, empty=_EMPTY_ON_AIR),
    **_row(FeedPhase.EMPTY_PAUSED, paused_fed_drained=_fed, fed=_FED_FROM_ELSEWHERE, empty=_EMPTY_UNOWNED),
    **_row(FeedPhase.UNKNOWN, paused_fed_drained=_fed, fed=_FED_FROM_ELSEWHERE, empty=_EMPTY_UNOWNED),
}


def _transition(phase: FeedPhase, signal: FeedSignal) -> _Handler:
    """The handler for ``(phase, signal)``. A cell the table does not spell out RAISES — a shape
    nobody wrote a rule for is drift to be seen, never a default to fall into."""
    try:
        return _TRANSITIONS[(phase, signal)]
    except KeyError:
        raise LookupError(
            f"feed_state: no transition for {phase!r} on {signal!r} — every cell needs its own row"
        ) from None


def _classify(frame: FeedFrame, facts: _Facts) -> FeedSignal:
    """ONE signal for this frame, by the FIXED priority order of :class:`FeedSignal`. Pure."""
    if not frame.live:
        return FeedSignal.NOT_LIVE
    if not frame.job_active:
        return FeedSignal.NO_JOB
    if frame.gcode_state not in (_RUNNING, _PAUSE):
        return FeedSignal.SETUP
    if frame.layer < 1:
        return FeedSignal.BEFORE_FIRST_LAYER
    running = frame.gcode_state == _RUNNING
    if running and 0 < frame.total_layers <= frame.layer:
        return FeedSignal.LAST_LAYER
    active = frame.feed.active
    held = facts.held if facts.active_extruder == frame.feed.active_extruder else None
    if not running:
        if frame.runout_demand_standing:
            return FeedSignal.PAUSED_DEMAND
        if frame.posture:
            return FeedSignal.PAUSED_POSTURE
        if active.kind in ("fed", "external"):
            return FeedSignal.PAUSED_FED_DRAINED if frame.reads_absent(held) else FeedSignal.PAUSED_FED
        if active.kind == "empty":
            return FeedSignal.PAUSED_EMPTY
        return FeedSignal.PAUSED_UNREADABLE
    if facts.active_extruder is not None and frame.feed.active_extruder != facts.active_extruder:
        return FeedSignal.TOOL_CHANGE
    if frame.posture:
        return FeedSignal.POSTURE
    if active.kind in ("fed", "external"):
        fed = _feeder(active)
        if frame.reads_absent(held):
            return FeedSignal.FED_DRAINED_SWITCHED if frame.auto_switched and fed != held else FeedSignal.FED_DRAINED
        if held is not None and fed == held and frame.reads_present(held):
            return FeedSignal.FED_RESTORED
        if held is not None and frame.auto_switched and fed != held:
            return FeedSignal.FED_SWITCHED
        if frame.reads_absent(fed):
            return FeedSignal.FED_ABSENT
        return FeedSignal.FED_PLAIN
    if active.kind == "empty":
        if facts.episode is not None and frame.layer > facts.episode:
            return FeedSignal.EMPTY_ADVANCED
        return FeedSignal.EMPTY_HELD
    return FeedSignal.UNREADABLE


# The phases an empty episode runs through, and the phases a held feeder belongs to.
_EPISODE_PHASES = frozenset({FeedPhase.EMPTY_UNCONFIRMED, FeedPhase.CHANGING, FeedPhase.AIR})
_HOLDING_PHASES = frozenset({FeedPhase.FED, FeedPhase.TAIL})


class FeedTracker:
    """ONE printer's feed state, stepped once per status push — by its MQTT client alone.

    Process memory with the client's own rehydrate story: a restart re-reads the phase from the
    first live frame (the ATTACH seed: TAIL when the active extruder is fed and its feeding tray
    reads absent — no event at an attach), and the job's record resets where the client resets its
    peaks (:class:`JobBoundary`). A frame that is not live reads UNKNOWN and re-seeds. The first
    live frame of a session (a new client's first report, a reconnect's) RE-SEEDS and is READ: its
    phase is the LEVEL that frame shows — a paused print fed from a slot reads FED off the session's
    first fresh report (001-H2S incident 574, 2026-10-11: the resume it was owed needed a level the
    tracker only reached one report later) — and it emits no event: an edge needs two readings of
    one session.

    The feeder BOOKKEEPING (``last_loaded``, ``last_fed``, the feeder log) follows every frame,
    live or not — it is the per-push record the client's ``tray_change_log`` / ``last_loaded_tray``
    projections have always kept; the PHASE and the EVENTS are evidence, and need a live reading.
    """

    def __init__(self) -> None:
        self._epoch: int | None = None
        self._seeding = True  # the next live frame re-seeds: it emits no event
        self._phase = _UNKNOWN
        self._active: int | None = None  # the active extruder of the previous LIVE frame
        self._episode: int | None = None
        # The previous push's effective feeder and its active extruder (a landing's edge).
        self._prev_feeder: int | None = None
        self._prev_extruder: int | None = None
        # The job's record.
        self._log: list[tuple[int, int, int]] = []
        self._last_loaded = -1
        self._last_fed: dict[int, int] = {}
        self._drained: dict[int, set[int]] = {}
        self._first_unfed: int | None = None
        # The last event of each kind.
        self._roll_drained: RollDrained | None = None
        self._load_landed: LoadLanded | None = None
        self._auto_switched: AutoSwitched | None = None
        self._air_began: AirBegan | None = None
        self._reading = self._read(active=None, feeder=None)

    @property
    def reading(self) -> FeedReading:
        """The latest reading (UNKNOWN before the first step)."""
        return self._reading

    def step(self, frame: FeedFrame) -> FeedReading:
        """Apply ONE push and return the new reading. TOTAL: a frame it cannot read fails closed to
        UNKNOWN and re-seeds — it never raises into the MQTT callback (invariant 10)."""
        try:
            self._reading = self._step(frame)
        except Exception:  # noqa: BLE001 — the status callback must never break on a frame
            logger.exception("feed_state: a frame could not be read — UNKNOWN, re-seeding")
            self._fail_closed()
        return self._reading

    # --- one push ----------------------------------------------------------------------------------

    def _step(self, frame: FeedFrame) -> FeedReading:
        attach = frame.boundary is JobBoundary.ATTACH
        if frame.boundary is JobBoundary.NEW_JOB or attach:
            self._begin_job(frame, attach=attach)
        if frame.live and frame.epoch != self._epoch:
            # The session's first live frame: re-seed what a previous session held, then READ it.
            self._reseed(frame.epoch)
        facts = _Facts(
            active_extruder=self._active,
            held=self._prev_feeder if self._phase.phase in _HOLDING_PHASES else None,
            episode=self._episode,
        )
        signal = _classify(frame, facts)
        prev = self._phase
        nxt = _transition(prev.phase, signal)(_Step(prev, frame, facts.held))
        live = signal is not FeedSignal.NOT_LIVE
        quiet = not live or self._seeding or attach  # an attach / re-seed reading is no event
        active = frame.feed.active_extruder
        feeder = nxt.tail if nxt.phase is FeedPhase.TAIL else _feeder(frame.feed.active)
        # The events, in their within-push order: drains, the landing, the switch, air.
        drained: list[RollDrained] = []
        if live:
            self._note_drains(frame, nxt, active, quiet, drained)
        landed = switched = None
        arrived = tray_fields.valid_feeder(feeder)  # the external holder is no AMS tray
        if not quiet and active is not None and arrived is not None:
            if arrived != self._prev_feeder and active == self._prev_extruder:
                landed = LoadLanded(next(_SEQUENCE), active, arrived, prev.phase)
            if (
                prev.phase is FeedPhase.CHANGING
                and prev.departed is not None
                and nxt.phase is FeedPhase.FED
                and arrived != prev.departed
            ):
                switched = AutoSwitched(next(_SEQUENCE), active, prev.departed, arrived)
        air = None
        if not quiet and nxt.phase is FeedPhase.AIR and prev.phase is not FeedPhase.AIR:
            air = AirBegan(next(_SEQUENCE), facts.episode if facts.episode is not None else max(frame.layer, 1))

        self._note_unfed(frame, signal, nxt, facts)
        self._note_feeder(frame, active, feeder, attach)
        self._note_episode(frame, signal, nxt)
        self._phase = nxt
        self._prev_feeder = feeder
        self._prev_extruder = active
        if live:
            self._active = active
            self._seeding = False
        else:
            self._epoch = frame.epoch
            self._active = None
            self._seeding = True
        self._roll_drained = drained[-1] if drained else self._roll_drained
        self._load_landed = landed or self._load_landed
        self._auto_switched = switched or self._auto_switched
        self._air_began = air or self._air_began
        events = tuple(event for event in (*drained, landed, switched, air) if event is not None)
        return self._read(active=active, feeder=feeder, events=events)

    def _begin_job(self, frame: FeedFrame, *, attach: bool) -> None:
        """The client reset its peaks: a NEW job starts its record from nothing; an ATTACH keeps
        where the last feeder was (``last_loaded`` / ``last_fed`` — the client's ``last_loaded_tray``
        is never reset at an attach) and seeds the active extruder's last feeder from it. Either
        way the phase re-reads from this frame."""
        self._log = []
        self._drained = {}
        self._first_unfed = None
        if attach:
            active = frame.feed.active_extruder
            if active is not None and active not in self._last_fed and self._last_loaded >= 0:
                self._last_fed[active] = self._last_loaded
        else:
            self._last_loaded = -1
            self._last_fed = {}
        self._phase = _UNKNOWN
        self._active = None
        self._episode = None

    def _reseed(self, epoch: int) -> None:
        """A new session's first live frame: nothing a previous session held is a fact about this one
        — the phase, the active extruder, the held feeder and the open episode go; the job's record
        (the feeder log, the last feeder, the unfed layer) stays: it is the job's, not the session's.
        The frame is then READ for its level, quietly (``_seeding``: no event)."""
        self._epoch = epoch
        self._seeding = True
        self._phase = _UNKNOWN
        self._active = None
        self._episode = None
        self._prev_feeder = None
        self._prev_extruder = None

    def _note_drains(
        self, frame: FeedFrame, nxt: _Phase, active: int | None, quiet: bool, events: list[RollDrained]
    ) -> None:
        """Per extruder: a tray that reads present again leaves the drained set (unless it is the
        TAIL the phase still holds); the active extruder's TAIL enters it; an INACTIVE extruder's
        feeding tray that reads absent while the print RUNS enters it too."""
        for extruder_id, drained in self._drained.items():
            for tray in list(drained):
                still_held = extruder_id == active and nxt.phase is FeedPhase.TAIL and nxt.tail == tray
                if frame.reads_present(tray) and not still_held:
                    drained.discard(tray)
        if active is not None and nxt.phase is FeedPhase.TAIL and nxt.tail is not None:
            self._drain(active, nxt.tail, quiet, events)
        if frame.gcode_state != _RUNNING:
            return
        for extruder_id, feed in frame.feed.extruders:
            if extruder_id != active and feed.tray is not None and frame.reads_absent(feed.tray):
                self._drain(extruder_id, feed.tray, quiet, events)

    def _drain(self, extruder_id: int, tray: int, quiet: bool, events: list[RollDrained]) -> None:
        """Mark ``tray`` drained under ``extruder_id``: once, an event unless the push is quiet."""
        drained = self._drained.setdefault(extruder_id, set())
        if tray in drained:
            return
        drained.add(tray)
        if not quiet:
            events.append(RollDrained(next(_SEQUENCE), extruder_id, tray))

    def _note_feeder(self, frame: FeedFrame, active: int | None, feeder: int | None, attach: bool) -> None:
        """The record the projections read: a new segment when the effective feeder becomes a real
        feeder other than ``last_loaded`` under the lifecycle gate (never on the attach reading),
        and every extruder's last real feeder."""
        if active is None:
            return
        if feeder is not None:
            if feeder != self._last_loaded and frame.job_active and not attach:
                self._log.append((active, feeder, max(frame.layer, 0)))
            self._last_loaded = feeder
            self._last_fed[active] = feeder
        for extruder_id, feed in frame.feed.extruders:
            other = _feeder(feed)
            if extruder_id != active and other is not None:
                self._last_fed[extruder_id] = other

    def _note_unfed(self, frame: FeedFrame, signal: FeedSignal, nxt: _Phase, facts: _Facts) -> None:
        """``first_unfed_layer``: the open episode's run layer, stamped when the layer ADVANCED past
        it while empty and the advance made it AIR — only below ``total_layers`` (an unknown total,
        0, stamps nothing; the end retract happens AT the last layer). The lowest is kept."""
        if signal is not FeedSignal.EMPTY_ADVANCED or nxt.phase is not FeedPhase.AIR or facts.episode is None:
            return
        if frame.total_layers > 0 and facts.episode < frame.total_layers:
            if self._first_unfed is None or facts.episode < self._first_unfed:
                self._first_unfed = facts.episode

    def _note_episode(self, frame: FeedFrame, signal: FeedSignal, nxt: _Phase) -> None:
        """The empty episode opens on entry to EMPTY_UNCONFIRMED or to a CHANGING whose active feed
        reads empty (at ``max(layer, 1)``: layer 0 is the start block), runs through AIR, and
        closes on any other phase or a fed toolhead — a sub-second pause-and-refill inside one
        layer never stamps. A tool change opens a new one."""
        if nxt.phase in _EPISODE_PHASES and frame.feed.active.kind == "empty":
            if self._episode is None or signal is FeedSignal.TOOL_CHANGE:
                self._episode = max(frame.layer, 1)
        else:
            self._episode = None

    def _fail_closed(self) -> None:
        """A frame that could not be read: UNKNOWN, and the next live frame re-seeds."""
        self._phase = _UNKNOWN
        self._seeding = True
        self._active = None
        self._episode = None
        self._prev_feeder = None
        self._prev_extruder = None
        self._reading = self._read(active=None, feeder=None)

    def _read(self, *, active: int | None, feeder: int | None, events: tuple[FeedEvent, ...] = ()) -> FeedReading:
        extruder_ids = sorted(
            set(self._last_fed) | set(self._drained) | {extruder for extruder, _feeder, _layer in self._log}
        )
        phase = self._phase
        return FeedReading(
            phase=phase.phase,
            active_extruder=active,
            feeder=feeder,
            cause=phase.cause,
            departed=phase.departed,
            tail=phase.tail,
            held_slot=phase.held_slot,
            extruders=tuple(
                ExtruderReading(
                    extruder=extruder_id,
                    last_fed=self._last_fed.get(extruder_id),
                    drained=frozenset(self._drained.get(extruder_id, ())),
                    segments=self._segments(extruder_id),
                )
                for extruder_id in extruder_ids
            ),
            feeder_log=tuple(self._log),
            last_loaded=self._last_loaded,
            first_unfed_layer=self._first_unfed,
            roll_drained=self._roll_drained,
            load_landed=self._load_landed,
            auto_switched=self._auto_switched,
            air_began=self._air_began,
            events=events,
        )

    def _segments(self, extruder_id: int) -> tuple[tuple[int, int], ...]:
        """One extruder's ``(feeder, from_layer)`` segments: its entries in the job's log, a run of
        the same feeder (a tool change away and back) counted once."""
        segments: list[tuple[int, int]] = []
        for extruder, feeder, layer in self._log:
            if extruder == extruder_id and (not segments or segments[-1][0] != feeder):
                segments.append((feeder, layer))
        return tuple(segments)
