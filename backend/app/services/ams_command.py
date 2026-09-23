"""THE owner of "an AMS motion command, and what the wire answered".

Why this module exists
----------------------
On 2026-09-11 the farm adopted the premise "the firmware drops every load AND unload
while the AMS is mid filament-change (``ams_status_main == 1``)" and built two
refusals on it: a 409 on the operator's Load / Unload, and a recovery give-up
(``stuck_reset_failed``) that never sent an unload at all. The premise was ASSERTED,
not measured per posture. What the wire record actually holds:

* a LOAD into an empty path while the change-error modal stands was dropped — 3
  witnesses (009-H2S 2026-07-20, 002-H2S 2026-09-11 05:44 and 05:47);
* an UNLOAD with nothing loaded moved nothing 8 times out of 8 — physically a no-op,
  so "no movement" there answers nothing (undecidable, not "dropped");
* a command with filament LOADED behind a mechanical feed fault had never been
  measured — and that is the posture of the three wedges (003/006/012-H2S,
  2026-09-19..22) that gave up and waited hours for a human.

This module replaces the assertion with a measurement: every AMS load / unload is
published, the wire is observed, and ONE classifier names the answer for the posture
the command was sent into, recorded on one greppable ``[ams-command]`` line.

The measured fact behind ``held``
---------------------------------
Commands sent into a job-owned filament change are ACKNOWLEDGED and HELD behind it,
then run in order at the next release. 012-H2S 2026-09-23: six commands sent into the
loaded wedge (the driver's unload, four operator unloads, one load of slot 3) each
echoed ``result=success`` and moved nothing; after the print's terminal the AMS ran
them in order from 02:06:36 to 02:10:14 (3→255, →2, a full cycle, back to 255 because
the last one was an unload) with no farm command and no touchscreen use
(operator-confirmed). :func:`classify` names that measurement ``held`` in the
mid-change rows: the firmware's ACK for THIS send — correlated by the send's own
``sequence_id`` through ``BambuMQTTClient.ack_for`` (:func:`ack_of`) — reads success,
AND nothing moved. A held command is a hazard, not a tool: a held UNLOAD that drains
while the print is RUNNING empties the extruder mid-print.

What it owns
------------
* The two verbs :func:`load` and :func:`unload` — the ONLY callers of
  ``BambuMQTTClient.ams_load_filament`` / ``ams_unload_filament`` in ``backend/app``
  (AST-pinned in ``tests/unit/services/test_ams_command.py``). They own the pre-send
  mark (``spool_respool.note_commanded_load``), the entry snapshot, the send's
  sequence id (:class:`Sent`), the only two pre-publish refusals kept (no client or a
  publish that did not go out; a standing runout hold, which LATCHES a load in
  firmware — 006-H2S 2026-07-26), the operator attribution stamp, and the INFO send
  line.
* :func:`classify` — the ONE reading of "what did the wire answer this command" — and
  :func:`ack_of`, the ONE correlation of a send to the firmware's ACK for it.
* The operator facade :func:`command_for_operator` and its wait :func:`observe`.
* :func:`operator_commanded_since` — the attribution a recovery driver's takeover
  predicate reads, so a driver's verdict can never be an operator's move.

What it deliberately does NOT own
---------------------------------
The recovery LADDER — which lever to pull, when to resend, when to give up — stays in
``spool_recovery``. This module answers "what happened to THIS command"; the driver
decides what to do about the answer. No refusal is ever derived from a posture here.

The table rule
--------------
:data:`_ROWS` is keyed by ``(command, posture(entry))`` and spells out every cell. A
missing key RAISES (``LookupError``) — the ``incident_resolution`` rule-table pattern:
a new command or posture must be given its own row, never inherit another row's
evidence from an ``else`` nobody considered it for.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from backend.app.schemas.printer import AmsCommandOutcome
from backend.app.services import spool_respool
from backend.app.services.bambu_mqtt import (
    AMS_STATUS_ASSIST,
    AMS_STATUS_IDLE,
    BambuMQTTClient,
    CommandAck,
    PrinterState,
    ams_mid_filament_change,
)
from backend.app.services.hms_errors import current_runout_demand, runout_hold_active
from backend.app.services.printer_incidents import runout_slot_desc
from backend.app.services.printer_manager import printer_manager
from backend.app.services.tray_fields import (
    TRAY_NOW_EXTERNAL_SPOOL,
    TRAY_NOW_NOTHING_FED,
    parse_int_field,
    valid_feeder,
)

logger = logging.getLogger(__name__)

Command = Literal["load", "unload"]
Actor = Literal["driver", "operator"]
Posture = Literal["idle", "assist", "mid_change_loaded", "mid_change_empty", "other"]
Answer = Literal["complete", "acted", "no_movement", "held", "undecidable", "session_changed"]
RefusalReason = Literal["not_connected", "runout_hold"]

#: The wire command both verbs publish: a load and an unload are one frame,
#: ``ams_change_filament``, told apart by ``target`` 255 — so it is also the command
#: name the firmware's ACK for either carries.
_WIRE_COMMAND = "ams_change_filament"

#: The ACK ``result`` spellings that mean success, compared lower-cased — the wire has
#: shown both ``result=success`` (the motion echoes) and ``result=SUCCESS`` (the
#: ``ams_control resume`` echo), 012-H2S 2026-09-23. Anything else is a failure.
_ACK_SUCCESS_RESULTS = frozenset({"success", "ok"})

#: How long the AMS must hold its "nothing fed" reading before an unload with NO
#: observed change cycle counts as complete (and how long a mid-change unload's empty
#: reading must hold). The operator's proven manual recovery left 16 s between the
#: unload and the load that worked; 15 s is that gap, floored. Also the point at which
#: an unload sent with nothing loaded into a mid-change AMS is called ``undecidable``
#: (nothing physical can answer it, so waiting longer measures nothing).
UNLOAD_GRACE_S: float = 15.0

#: The operator facade's observe window: how long a Load / Unload click waits for the
#: wire before answering. UNMEASURED — a first guess sized to a human click, not a
#: firmware latency. The grace-held completions (:data:`UNLOAD_GRACE_S`) cannot land
#: inside it, so an accepted operator unload usually answers ``acted``; a click into a
#: job-owned change that the firmware acknowledged and did not act on answers ``held``
#: at the end of it (the held rule reads the shorter of the grace and the window; an
#: ACK that lands after the window leaves the click at ``no_movement``). The recovery
#: driver observes over its own, longer step timeout.
OPERATOR_ACK_S: float = 5.0

_NOT_CONNECTED = "Printer not connected"
_NOT_SENT = "Printer not connected or AMS busy. Command not sent."


def _monotonic() -> float:
    """The module's clock (a seam: tests install ``FakeClock`` over it)."""
    return time.monotonic()


# --- the wire snapshot ----------------------------------------------------------------


@dataclass(frozen=True)
class AmsWireSnapshot:
    """The five wire fields a motion command's answer is read from, at one instant.

    Taken BEFORE every send (the entry) and at every poll after it (``now``). ``None``
    means the state did not carry a parseable value — never a stand-in for a reading.
    ``taken_at`` is :func:`time.monotonic` (via :func:`_monotonic`).
    """

    tray_now: int | None
    ams_status_main: int | None
    ams_status_sub: int | None
    tray_tar: int | None
    connection_epoch: int | None
    taken_at: float


def snapshot(state: PrinterState | None) -> AmsWireSnapshot:
    """Read the snapshot off a live ``PrinterState``. Getattr-safe and total: a ``None``
    state (no status yet) and a state without a field both give ``None`` fields."""
    return AmsWireSnapshot(
        tray_now=parse_int_field(getattr(state, "tray_now", None)),
        ams_status_main=parse_int_field(getattr(state, "ams_status_main", None)),
        ams_status_sub=parse_int_field(getattr(state, "ams_status_sub", None)),
        tray_tar=parse_int_field(getattr(state, "tray_tar", None)),
        connection_epoch=parse_int_field(getattr(state, "connection_epoch", None)),
        taken_at=_monotonic(),
    )


#: The postures :func:`posture` returns for a mid filament-change AMS
#: (``bambu_mqtt.ams_mid_filament_change``) — the ONE statement of which postures those
#: are, for readers that hold a posture rather than the snapshot it was read from (a
#: recovery driver's command records). Pinned against :func:`posture` by its own tests.
MID_CHANGE_POSTURES: frozenset[Posture] = frozenset({"mid_change_loaded", "mid_change_empty"})


def posture(snap: AmsWireSnapshot) -> Posture:
    """The posture a command is sent INTO — the key the classifier's rows are read by.

    Mid-change is ``bambu_mqtt.ams_mid_filament_change`` (the one origin of that
    comparison), split by whether a real feeder is loaded (``tray_fields.valid_feeder``);
    ``assist`` / ``idle`` are the ``AMS_STATUS_*`` constants; anything else — including
    an unreadable state — is ``other``.
    """
    if ams_mid_filament_change(snap):
        return "mid_change_loaded" if valid_feeder(snap.tray_now) is not None else "mid_change_empty"
    if snap.ams_status_main == AMS_STATUS_ASSIST:
        return "assist"
    if snap.ams_status_main == AMS_STATUS_IDLE:
        return "idle"
    return "other"


# --- the classifier -------------------------------------------------------------------


@dataclass
class Observation:
    """The running facts of ONE command's observation window.

    Caller-owned and threaded through every :func:`classify` call for that command
    (one instance per command, never shared). It is the classifier's ONLY state:
    :func:`classify` folds each ``now`` into it, the CALLER folds the firmware's ACK
    for the send into it (:meth:`fold_ack`, before every call), and the table rows read
    it, so the rows themselves stay pure functions of their arguments. Every time in it
    is on the CALLER's elapsed timeline (``elapsed_s``) — no clock is read here, so a
    driver on a fake clock and the operator facade on the real one classify identically.
    """

    #: Some wire field (tray_now / ams_status_main / ams_status_sub / tray_tar) read
    #: differently from the entry on at least one poll. Sticky: a field that moved and
    #: moved back still moved.
    moved: bool = False
    #: A non-idle ``ams_status_main`` was READ on some poll — the change cycle ran.
    cycle_seen: bool = False
    #: ``elapsed_s`` at which the current unbroken run of "idle AND nothing fed" began;
    #: ``None`` when the latest poll broke it.
    idle_empty_since_s: float | None = None
    #: ``elapsed_s`` at which the current unbroken run of "nothing fed" (tray_now 255)
    #: began, whatever the AMS state; ``None`` when the latest poll broke it.
    empty_since_s: float | None = None
    #: The firmware's ACK for THIS send: ``None`` = no ACK seen yet, ``True`` = a
    #: success result, ``False`` = any other result.
    acked: bool | None = None

    def fold_ack(self, ack: CommandAck | None) -> None:
        """Fold the firmware's ACK for this send (:func:`ack_of`) into the running facts.

        A ``result`` that lower-cases to ``success`` or ``ok`` is success; anything else
        (``fail``, a null result) is a failure. ``None`` — no ACK yet, or one already
        rolled out of the client's bounded ACK log — changes nothing: an ACK once seen
        stays seen.
        """
        if ack is None:
            return
        self.acked = ack.result is not None and ack.result.lower() in _ACK_SUCCESS_RESULTS

    def fold(self, entry: AmsWireSnapshot, now: AmsWireSnapshot, elapsed_s: float) -> None:
        """Fold one poll into the running facts. Idempotent for a repeated ``now``."""
        if _moving_fields(now) != _moving_fields(entry):
            self.moved = True
        idle = now.ams_status_main == AMS_STATUS_IDLE
        empty = now.tray_now == TRAY_NOW_NOTHING_FED
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
    """The fields whose change counts as "the AMS moved" (the epoch is not one of them —
    an epoch change is a different answer, ``session_changed``)."""
    return (snap.tray_now, snap.ams_status_main, snap.ams_status_sub, snap.tray_tar)


#: A table row: ``(target, now, observation, elapsed_s, deadline_s) -> Answer | None``.
#: ``None`` = this row has no verdict yet; the deadline rule then decides or waits.
Row = Callable[[int | None, AmsWireSnapshot, Observation, float, float], Answer | None]


def _unload_settled(
    target: int | None, now: AmsWireSnapshot, obs: Observation, elapsed_s: float, deadline_s: float
) -> Answer | None:
    """An unload sent into an AMS that is NOT mid-change: complete on idle + nothing fed,
    through the two evidence paths the recovery driver has always used (the former
    ``spool_recovery._confirm_unloaded``):

    (a) a non-idle cycle was observed, and the AMS is now idle with nothing fed;
    (b) no cycle was observed (command latency, or an unload the firmware treats as a
        no-op): idle + nothing fed must HOLD for :data:`UNLOAD_GRACE_S`; any contrary
        poll restarts the dwell.

    ``tray_now == 255`` alone is never completion: after a feed fault it already reads
    255 before the unload (009-H2S 2026-07-20).
    """
    if obs.idle_empty_since_s is None:
        return None
    if obs.cycle_seen or elapsed_s - obs.idle_empty_since_s >= UNLOAD_GRACE_S:
        return "complete"
    return None


def _held_behind_the_change(obs: Observation, elapsed_s: float, deadline_s: float) -> Answer | None:
    """``held`` — the measured fact in the module docstring, for a command sent into a
    mid-change AMS: the firmware ACKNOWLEDGED this send (``obs.acked is True``) and
    nothing moved by :data:`UNLOAD_GRACE_S` (or by the deadline when the window is
    shorter — the operator's), so the command sits behind the print's own change and
    runs at the next release. An EARLY exit, the :func:`_unload_mid_change_empty`
    pattern: an acknowledged, unmoved command answers at the grace, never after a full
    step timeout. No ACK, or a failure ACK, is not ``held`` — the deadline rule answers
    ``no_movement``; a movement is never ``held``."""
    if obs.acked is True and not obs.moved and elapsed_s >= min(UNLOAD_GRACE_S, deadline_s):
        return "held"
    return None


def _unload_mid_change_loaded(
    target: int | None, now: AmsWireSnapshot, obs: Observation, elapsed_s: float, deadline_s: float
) -> Answer | None:
    """An unload sent into a mid-change AMS with a real feeder loaded: complete when
    ``tray_now`` left that feeder for 255 and the empty reading has held for
    :data:`UNLOAD_GRACE_S`. The AMS may legitimately STAY in state 1 — the print still
    owes the change — so idle is not required. Otherwise ``held`` when acknowledged
    and unmoved (:func:`_held_behind_the_change`) — the 012-H2S 2026-09-23 posture."""
    if obs.empty_since_s is not None and elapsed_s - obs.empty_since_s >= UNLOAD_GRACE_S:
        return "complete"
    return _held_behind_the_change(obs, elapsed_s, deadline_s)


def _unload_mid_change_empty(
    target: int | None, now: AmsWireSnapshot, obs: Observation, elapsed_s: float, deadline_s: float
) -> Answer | None:
    """An unload sent into a mid-change AMS with NOTHING loaded: nothing physical can
    move, so "no movement" answers nothing. ``undecidable`` as soon as
    :data:`UNLOAD_GRACE_S` has passed without movement — or at the deadline when the
    window is shorter than the grace (the operator facade's) — never a full recovery
    step timeout of waiting. A movement falls through to the deadline rule (``acted``)."""
    if not obs.moved and elapsed_s >= min(UNLOAD_GRACE_S, deadline_s):
        return "undecidable"
    return None


def _load_reached_target(
    target: int | None, now: AmsWireSnapshot, obs: Observation, elapsed_s: float, deadline_s: float
) -> Answer | None:
    """A load, in any posture: complete when ``tray_now`` reads the commanded tray.

    Never on the "nothing fed" sentinel: a load of Ext-R (``target`` 255 on a
    dual-nozzle H2D) reading ``tray_now == 255`` is the absence of a load, not its
    evidence (``tray_fields`` names that sentinel for exactly this reason)."""
    if target is not None and target != TRAY_NOW_NOTHING_FED and now.tray_now == target:
        return "complete"
    return None


def _load_mid_change(
    target: int | None, now: AmsWireSnapshot, obs: Observation, elapsed_s: float, deadline_s: float
) -> Answer | None:
    """A load sent into a mid-change AMS, loaded or empty: ``complete`` exactly as
    :func:`_load_reached_target`; otherwise ``held`` when acknowledged and unmoved
    (:func:`_held_behind_the_change` — the 012-H2S 2026-09-23 load of slot 3)."""
    return _load_reached_target(target, now, obs, elapsed_s, deadline_s) or _held_behind_the_change(
        obs, elapsed_s, deadline_s
    )


# EXPLICIT, every cell, no default: a missing key RAISES at the lookup (``_row``).
# ``held`` is reachable only from the mid-change rows that can MOVE: an unload with
# nothing loaded (``mid_change_empty``) has no physical answer, so it stays
# ``undecidable``, and outside a change a command is never held behind one.
_ROWS: dict[tuple[Command, Posture], Row] = {
    ("unload", "idle"): _unload_settled,
    ("unload", "assist"): _unload_settled,
    ("unload", "other"): _unload_settled,
    ("unload", "mid_change_loaded"): _unload_mid_change_loaded,
    ("unload", "mid_change_empty"): _unload_mid_change_empty,
    ("load", "idle"): _load_reached_target,
    ("load", "assist"): _load_reached_target,
    ("load", "other"): _load_reached_target,
    ("load", "mid_change_loaded"): _load_mid_change,
    ("load", "mid_change_empty"): _load_mid_change,
}


def _row(command: Command, entry_posture: Posture) -> Row:
    try:
        return _ROWS[(command, entry_posture)]
    except KeyError:
        raise LookupError(
            f"ams_command: no classifier row for (command={command!r}, posture={entry_posture!r}) — "
            "every (command, posture) pair must be given its own row in _ROWS"
        ) from None


def classify(
    command: Command,
    target: int | None,
    entry: AmsWireSnapshot,
    now: AmsWireSnapshot,
    *,
    observation: Observation,
    elapsed_s: float,
    deadline_s: float,
) -> Answer | None:
    """THE reading of what the wire answered one AMS motion command. ``None`` = keep
    waiting.

    Pure over its arguments — no I/O, no clock, no module state — with ONE declared
    exception: it folds ``now`` into the caller-owned ``observation`` (the running
    "moved / cycle seen / settled since" facts a single snapshot cannot carry). That is
    the design chosen over handing the rows the whole poll history: one object threaded
    through the caller's loop, O(1) per poll, and the rows stay pure readers of it.

    The firmware's ACK is the CALLER's to fold: before every call, the caller folds the
    ACK for its send into ``observation`` (``observation.fold_ack(ack_of(printer_id,
    sent))``) — :func:`observe` does, and so must any loop that runs this classifier
    itself. A caller that never folds one never gets ``held``: the mid-change rows then
    answer as if no ACK arrived (``no_movement`` at the deadline).

    Order, for every command:

    1. ``now.connection_epoch != entry.connection_epoch`` → ``session_changed`` (the
       session the command was sent on is gone; nothing read after it answers it).
    2. The row for ``(command, posture(entry))`` — a missing row raises ``LookupError``.
       The mid-change rows that can move answer ``held`` early (acknowledged + unmoved,
       :func:`_held_behind_the_change`).
    3. At ``elapsed_s >= deadline_s``: a moved field → ``acted``, nothing moved →
       ``no_movement``.
    """
    if now.connection_epoch != entry.connection_epoch:
        return "session_changed"
    row = _row(command, posture(entry))
    observation.fold(entry, now, elapsed_s)
    answer = row(target, now, observation, elapsed_s, deadline_s)
    if answer is not None:
        return answer
    if elapsed_s >= deadline_s:
        return "acted" if observation.moved else "no_movement"
    return None


# --- the verbs ------------------------------------------------------------------------


@dataclass(frozen=True)
class Refusal:
    """A command that did NOT go out. ``detail`` is the operator-facing sentence."""

    reason: RefusalReason
    detail: str


@dataclass(frozen=True)
class Sent:
    """A command that WENT OUT.

    ``entry`` is the snapshot taken BEFORE the publish (the posture the command was sent
    into, the baseline movement is read against). ``sequence_id`` is the id the client
    published it under — ``BambuMQTTClient.last_sent_sequence_id`` read in the same
    synchronous stretch as the publish, so it is this send's own — and the key the
    firmware's ACK is correlated by (:func:`ack_of`). ``None`` only when the client
    recorded no id for the publish, which leaves the send with no ACK to read.
    """

    entry: AmsWireSnapshot
    sequence_id: str | None


@dataclass(frozen=True)
class _OperatorStamp:
    connection_epoch: int | None
    at: float


# printer_id -> the last operator-commanded AMS motion command that WENT OUT.
_operator_commanded: dict[int, _OperatorStamp] = {}


def load(printer_id: int, tray_id: int, *, actor: Actor) -> Sent | Refusal:
    """Publish an AMS load of ``tray_id``. Returns the :class:`Sent` (the ENTRY snapshot
    taken before the publish, and the send's sequence id) or the :class:`Refusal` that
    kept it from going out.

    Refused only for no client / a publish the client did not send (``not_connected``)
    and a standing runout hold (``runout_hold``): while the printer is PAUSEd for a
    same-slot refill the AMS executes no load, and the request LATCHES in firmware and
    resurfaces at the resume as a bogus demand (006-H2S 2026-07-26) — a refusal that
    prevents a fault, not one that guesses at a posture.
    """
    client = printer_manager.get_client(printer_id)
    if client is None:
        return Refusal("not_connected", _NOT_CONNECTED)
    state = printer_manager.get_status(printer_id)
    if runout_hold_active(state):
        return Refusal("runout_hold", _runout_hold_detail(printer_id, state))
    # Mark BEFORE the publish so the backup-swap detector never reads our own tray_now
    # edge as a firmware runout and spends the departed spool (the 006 false-stamp mode).
    spool_respool.note_commanded_load(printer_id, tray_id)
    entry = snapshot(state)
    published = client.ams_load_filament(tray_id)
    return _after_publish(printer_id, "load", tray_id, entry, client=client, actor=actor, published=published)


def unload(printer_id: int, *, actor: Actor) -> Sent | Refusal:
    """Publish an AMS unload. Returns the :class:`Sent` (the ENTRY snapshot taken before
    the publish, and the send's sequence id) or the ``not_connected`` :class:`Refusal`.
    Sent in every posture — no posture refuses an unload (doctrine invariant 8: the
    unload is unconditional before a load)."""
    client = printer_manager.get_client(printer_id)
    if client is None:
        return Refusal("not_connected", _NOT_CONNECTED)
    entry = snapshot(printer_manager.get_status(printer_id))
    published = client.ams_unload_filament()
    return _after_publish(printer_id, "unload", None, entry, client=client, actor=actor, published=published)


def _after_publish(
    printer_id: int,
    command: Command,
    target: int | None,
    entry: AmsWireSnapshot,
    *,
    client: BambuMQTTClient,
    actor: Actor,
    published: bool,
) -> Sent | Refusal:
    if not published:
        logger.warning(
            "[ams-command] actor=%s command=%s printer=%s target=%s not sent (the client refused the publish)",
            actor,
            command,
            printer_id,
            target,
        )
        return Refusal("not_connected", _NOT_SENT)
    # The send's own id: read in the same synchronous stretch as the publish (no await
    # between), so no other publish can have been recorded over it.
    sequence_id = client.last_sent_sequence_id(_WIRE_COMMAND)
    # Stamped only once the command actually went out: an unsent click must never make
    # a driver stand down. Same synchronous stretch as the publish (no await between),
    # so no reader can observe the wire's answer before the stamp.
    if actor == "operator":
        _operator_commanded[printer_id] = _OperatorStamp(connection_epoch=entry.connection_epoch, at=_monotonic())
    logger.info(
        "[ams-command] actor=%s command=%s printer=%s target=%s seq=%s posture=%s entry tray_now=%s "
        "ams_status=%s/%s tray_tar=%s epoch=%s",
        actor,
        command,
        printer_id,
        target,
        sequence_id,
        posture(entry),
        entry.tray_now,
        entry.ams_status_main,
        entry.ams_status_sub,
        entry.tray_tar,
        entry.connection_epoch,
    )
    return Sent(entry=entry, sequence_id=sequence_id)


def _runout_hold_detail(printer_id: int, state: PrinterState | None) -> str:
    info = printer_manager.get_printer(printer_id)
    name = info.name if info is not None else f"Printer {printer_id}"
    demand = current_runout_demand(getattr(state, "hms_errors", None) or [])
    where = runout_slot_desc(demand[0] * 4 + demand[1]) if demand else None
    return (
        f"{name} is PAUSEd waiting for a filament refill in "
        f"{where or 'the slot that ran out'}. The AMS executes no load in this state — the "
        "request would latch in the firmware and resurface at the resume as a bogus demand for "
        f"this slot (006-H2S 2026-07-26). Insert filament into {where or 'the slot that ran out'} "
        "and the print resumes from there."
    )


def ack_of(printer_id: int, sent: Sent) -> CommandAck | None:
    """The firmware's ACK for ``sent`` — the ONE correlation of a motion command to its
    ACK: the live client's ``ack_for`` keyed by the wire command both verbs publish and
    the send's own sequence id (invariant 14: the ACK is consumed into the classifier).

    ``None`` = no ACK has arrived yet, or there is nothing to correlate by (the client
    is gone, or the send carries no id). Any loop that runs :func:`classify` folds this
    into its :class:`Observation` before every call (``observation.fold_ack(...)``).
    """
    if sent.sequence_id is None:
        return None
    client = printer_manager.get_client(printer_id)
    if client is None:
        return None
    return client.ack_for(_WIRE_COMMAND, sent.sequence_id)


def operator_commanded_since(printer_id: int, since_monotonic: float) -> bool:
    """Did an OPERATOR's AMS motion command go out on this printer at or after
    ``since_monotonic`` (a :func:`time.monotonic` reading)?

    Session-epoch scoped: a stamp made on an older MQTT session is not "since" anything
    on the current one — the command it records was answered (or lost) on a session
    that no longer exists.
    """
    stamp = _operator_commanded.get(printer_id)
    if stamp is None:
        return False
    current_epoch = snapshot(printer_manager.get_status(printer_id)).connection_epoch
    return stamp.connection_epoch == current_epoch and stamp.at >= since_monotonic


def _reset_state() -> None:
    """Drop every operator stamp (tests)."""
    _operator_commanded.clear()


# --- the operator facade --------------------------------------------------------------


def log_answer(
    *,
    actor: Actor,
    printer_id: int,
    command: Command,
    target: int | None,
    entry: AmsWireSnapshot,
    now: AmsWireSnapshot,
    answer: Answer,
    elapsed_s: float,
) -> None:
    """THE answer line — the measurement's grep token (``answer=``), one format string
    for every reader of the wire's answer: the operator facade (:func:`observe`) and the
    recovery driver's own confirm loops. It carries the posture the command was sent
    into and every moving field's entry→now edge."""
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


async def observe(
    printer_id: int,
    command: Command,
    target: int | None,
    sent: Sent,
    *,
    timeout_s: float,
    poll_s: float = 0.25,
) -> Answer:
    """Poll the live state and return the classifier's first answer (the operator
    facade's wait; the recovery driver runs :func:`classify` inside its own confirm
    loops). ``elapsed_s`` is measured from the entry snapshot, i.e. from the send.

    Each poll folds the firmware's ACK for ``sent`` (:func:`ack_of`) into the
    observation BEFORE reading the snapshot, so an ACK is never paired with a snapshot
    older than itself: a movement that began before the ACK was read is in the snapshot
    the held rule reads."""
    entry = sent.entry
    observation = Observation()
    while True:
        observation.fold_ack(ack_of(printer_id, sent))
        now = snapshot(printer_manager.get_status(printer_id))
        elapsed_s = now.taken_at - entry.taken_at
        answer = classify(
            command, target, entry, now, observation=observation, elapsed_s=elapsed_s, deadline_s=timeout_s
        )
        if answer is not None:
            log_answer(
                actor="operator",
                printer_id=printer_id,
                command=command,
                target=target,
                entry=entry,
                now=now,
                answer=answer,
                elapsed_s=elapsed_s,
            )
            return answer
        await asyncio.sleep(min(poll_s, max(0.0, timeout_s - elapsed_s)))


@dataclass(frozen=True)
class AmsCommandResult:
    """The operator facade's answer: the closed outcome plus the sentence for it (the
    refusal's own detail, or the non-UI-client fallback for a wire answer)."""

    outcome: AmsCommandOutcome
    message: str


_REFUSAL_OUTCOME: dict[RefusalReason, AmsCommandOutcome] = {
    "not_connected": "refused_not_connected",
    "runout_hold": "refused_runout_hold",
}

# The non-UI-client fallback sentence per (command, answer); ``{target}`` is the load's
# tray. No ("load", "undecidable") cell, on purpose: only an unload into a mid-change AMS
# with nothing loaded is undecidable (``_unload_mid_change_empty``), so that lookup
# raising is the classifier having changed shape without its copy.
_ANSWER_COPY: dict[tuple[Command, Answer], str] = {
    ("load", "complete"): "Loading filament from {target}",
    ("load", "acted"): "Loading filament from {target}",
    ("load", "no_movement"): "Load sent. AMS did not move.",
    ("load", "held"): "Load accepted. Held behind the paused print's filament change.",
    ("load", "session_changed"): "Printer reconnected during the command. Check the AMS.",
    ("unload", "complete"): "Unloading filament",
    ("unload", "acted"): "Unloading filament",
    ("unload", "no_movement"): "Unload sent. AMS did not move.",
    ("unload", "held"): "Unload accepted. Held behind the paused print's filament change.",
    ("unload", "undecidable"): "Unload sent. Nothing was loaded.",
    ("unload", "session_changed"): "Printer reconnected during the command. Check the AMS.",
}


def _target_label(tray_id: int) -> str:
    if tray_id == TRAY_NOW_EXTERNAL_SPOOL:
        return "external spool"
    if tray_id == TRAY_NOW_NOTHING_FED:
        return "Ext-R"
    return f"AMS {tray_id // 4} slot {tray_id % 4 + 1}"


def _answer_message(command: Command, tray_id: int | None, answer: Answer) -> str:
    template = _ANSWER_COPY[(command, answer)]
    return template.format(target=_target_label(tray_id)) if tray_id is not None else template


async def command_for_operator(printer_id: int, command: Command, tray_id: int | None = None) -> AmsCommandResult:
    """An operator's Load / Unload click: publish through the verb, observe the wire for
    :data:`OPERATOR_ACK_S`, and answer with the measured outcome."""
    if command == "load":
        if tray_id is None:
            raise ValueError("ams_command.command_for_operator: a load needs a tray_id")
        sent = load(printer_id, tray_id, actor="operator")
    elif command == "unload":
        sent = unload(printer_id, actor="operator")
    else:
        raise LookupError(f"ams_command.command_for_operator: unknown command {command!r}")
    if isinstance(sent, Refusal):
        return AmsCommandResult(outcome=_REFUSAL_OUTCOME[sent.reason], message=sent.detail)
    answer = await observe(printer_id, command, tray_id, sent, timeout_s=OPERATOR_ACK_S)
    return AmsCommandResult(outcome=answer, message=_answer_message(command, tray_id, answer))
