"""THE record of what was done to a printer, by whom, and what the printer answered (GoF Command + Memento).

Why it exists (2026-10-11)
--------------------------
001-H2S incident 574 sat paused all night over a toolhead the wire reported fed: the resume it was
owed rested on process memory a deploy erased, and the farm's other answers — "did the resume take",
"was the stop taken", "did the start start" — were each read off a clock by a waiter of its own.
Raymond: "using time as a gate is not the correct way to discern status complete or incompleteness".
This module is the ONE owner of every state-changing command a printer is sent, and of the printer's
answer to it:

* **The send is a row** (``models/printer_action``), INSERTed and COMMITted BEFORE the publish; the
  row id IS the wire ``sequence_id``, so the firmware's ACK names the row across a restart and no two
  sends ever share an id (the per-client counter that restarted at 0 is gone).
* **The answer is an event** read through the family's table (``command_outcome.read``): the ACK, a
  reported state, a feed event, the job's terminal, a reboot, a person's act — never a clock. An
  action no event has answered stays OUTSTANDING (``answered_at IS NULL``); the attention owner pages
  a person about it, it never decides it. A command with NO ECHO by design (``command_outcome.echoes``:
  ``set_airduct``, and ``gcode_line`` / ``print_speed`` until an echo is measured) is recorded and never
  outstanding: its row keeps a NULL answer with the reason ``command_outcome.NO_ECHO_REASON``.
* **The self-heal bound is the fallback, never the answer** (2026-10-12, Raymond: "if it was self
  heal, it should stay self heal"). A lane hands its wait a :class:`SelfHealBound` — the bound
  production measured, with its witness — on the send (``bound=``), later (:func:`arm_bound`), or at
  its await (:func:`await_outcome`); a rehydrated action gets its family's from a registered factory
  (:func:`register_bound_factory`). The bound counts the printer's ONLINE seconds
  (:func:`online_seconds_since`: offline is a lost MQTT session, never a quiet report, and an anchor
  older than this process restarts at the printer's first fresh report). When it first passes with no
  event, ``bound_passed_at`` is written once, one ``[self-heal]`` line is logged, ``SelfHealDue`` is
  published and every bounded waiter gets :class:`Unanswered` — and the lane takes production's next
  self-heal step. The action is NOT answered: a late event still answers it, persists and publishes
  ``ActionAnswered``. It is evaluated on every status push of the printer and on the attention
  owner's pass (:func:`check_bounds` — an idle printer reports rarely); every elapsed-time read against
  a send or a bound's anchor lives here (``TestSelfHealBoundOwnership``).
* **The only caller of the client's command publishers** (``TestPrinterActionOwnership``): a lane
  builds a :class:`Command` and hands it to :func:`send` (or :func:`prepare` + :func:`publish` where
  its own record must name the action before the frame goes out — the restart rung's stop).
* **Whose load landed** has one store: a feed-state landing that answers no outstanding load is
  recorded as an ``observed_load`` (actor ``printer``) — the printer's own backup switch, a person's
  hand at the AMS.

Awaiting (owner 4 of the plan): :func:`await_outcome` resolves with an answer only when :func:`_answer`
does, i.e. on a hub event read through the table — or, for a waiter that named a bound, with
:class:`Unanswered` when the bound passes (:func:`_pass_bound`). Outstanding actions live in a per-printer in-memory registry
(:data:`_outstanding`), hydrated from the DB by :func:`rehydrate` (a lifespan rehydrator) and re-read on
each printer's first fresh report of a session (:func:`reread_session`, a ``session_start`` step).
Per status push the cost is in-memory only: the printers' outstanding actions, usually none.

Process memory and its rehydrate story: :data:`_outstanding` (rebuilt from the rows by
:func:`rehydrate`; an ACK's acceptance from ``acked_at``, a passed bound from ``bound_passed_at``; a
bound not yet passed is re-armed by its family's factory at the first evaluation; a motion command's
entry snapshot and its observation are not rebuilt — a rehydrated action's :class:`Unanswered` carries
:func:`reading_of`'s value), :data:`_last_operator_motion` and
:data:`_observed_load_at` (rebuilt from the rows by :func:`rehydrate`), :data:`_last_landing` /
:data:`_landing_owner` (one landing's attribution, consumed within the push that produced it — a
restart between the two halves of one push loses nothing a later push needs). The online clock's
slots (:data:`_first_fresh`, :data:`_offline_since`, :data:`_gaps`) need none: a restart restarts the
clock at each printer's first fresh report in the new process, which is the rule itself.
:data:`_bound_factories` is composition, registered by ``printer_event_wiring.wire`` at every boot.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Literal, overload

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.tasks import spawn_background_task
from backend.app.models.printer_action import PrinterAction
from backend.app.schemas.printer import AmsCommandOutcome
from backend.app.services import command_outcome, live_reading, printer_events, printer_incidents
from backend.app.services.bambu_mqtt import CommandAck
from backend.app.services.command_outcome import (
    ACTOR_FARM,
    ACTOR_OPERATOR,
    ACTOR_PRINTER,
    ActionFacts,
    Actor,
    AmsWireSnapshot,
    Answer,
    Family,
    MotionCommand,
    MotionReading,
    Observation,
    PostureFamily,
    Reading,
    Running,
    WireReading,
)
from backend.app.services.feed_state import LoadLanded
from backend.app.services.hms_errors import current_runout_demand, runout_hold_active
from backend.app.services.printer_events import (
    ActionAnswered,
    CommandAcked,
    FeedEventReported,
    JobTerminal,
    SelfHealDue,
    SessionLost,
    StatusReported,
)
from backend.app.services.printer_manager import printer_manager
from backend.app.services.tray_fields import TRAY_NOW_EXTERNAL_SPOOL, TRAY_NOW_NOTHING_FED, valid_feeder

if TYPE_CHECKING:
    from backend.app.services.bambu_mqtt import BambuMQTTClient, PrinterState

logger = logging.getLogger(__name__)

__all__ = [
    "ACTOR_FARM",
    "ACTOR_OPERATOR",
    "ACTOR_PRINTER",
    "Command",
    "Prepared",
    "SelfHealBound",
    "Sent",
    "Unanswered",
]

# --- the command (GoF Command) --------------------------------------------------------------------


@dataclass(frozen=True)
class Command:
    """One state-changing verb, ready to be published under the sequence id the record allocates.

    ``name`` is the record's vocabulary (``command_outcome.COMMANDS`` — an unknown name RAISES here, at
    construction); ``publish`` puts the frame on the wire through the client's publisher with the given
    ``sequence_id`` and answers whether it went out; ``target`` is what the command names (a load's
    tray, a dialog's decimal code)."""

    name: str
    publish: Callable[[BambuMQTTClient, str], object]
    target: int | None = None

    def __post_init__(self) -> None:
        command_outcome.spec_of(self.name)

    @property
    def family(self) -> Family:
        return command_outcome.spec_of(self.name).family


def resume() -> Command:
    """The plain ``print.resume`` of a paused print."""
    return Command("resume", lambda client, seq: client.resume_print(sequence_id=seq))


def power_loss_resume() -> Command:
    """The plain resume pressed for the firmware's post-reboot prompt (its own family: the prompt is its
    precondition, never a void)."""
    return Command("power_loss_resume", lambda client, seq: client.resume_print(sequence_id=seq))


def pause() -> Command:
    """``print.pause``."""
    return Command("pause", lambda client, seq: client.pause_print(sequence_id=seq))


def stop() -> Command:
    """``print.stop``."""
    return Command("stop", lambda client, seq: client.stop_print(sequence_id=seq))


def clean_print_error() -> Command:
    """The dialog's OK / close frame."""
    return Command("clean_print_error", lambda client, seq: client.clean_print_error(sequence_id=seq))


AmsControlParam = Literal["resume", "abort", "reset", "pause", "done"]


def ams_control(param: AmsControlParam) -> Command:
    """``print.ams_control`` with ``param`` (the dialog's CONTINUE / Retry is ``resume``)."""
    return Command(f"ams_control_{param}", lambda client, seq: client.ams_control(param, sequence_id=seq))


def load(tray: int) -> Command:
    """An AMS load of global tray ``tray``."""
    return Command("load", lambda client, seq: client.ams_load_filament(tray, sequence_id=seq), target=tray)


def unload() -> Command:
    """An AMS unload of whatever the active extruder holds."""
    return Command("unload", lambda client, seq: client.ams_unload_filament(sequence_id=seq))


def dialog_press(print_error: str, action: str, job_id: str) -> Command:
    """A press of the printer's own dialog button ``action`` for the dialog ``print_error`` (8 hex chars)."""
    try:
        code = int(print_error, 16)
    except (TypeError, ValueError):
        code = None
    return Command(
        "dialog_press",
        lambda client, seq: client.execute_hms_action(print_error, action, job_id, sequence_id=seq),
        target=code,
    )


def gcode(text: str) -> Command:
    """A ``gcode_line`` frame (one or more lines)."""
    return Command("gcode_line", lambda client, seq: client.send_gcode(text, sequence_id=seq))


def cali_sel(ams_id: int, tray_id: int, *, cali_idx: int, filament_id: str, nozzle_diameter: str = "0.4") -> Command:
    """An ``extrusion_cali_sel`` K-profile selection on a slot (the client's guarded write)."""
    return Command(
        "extrusion_cali_sel",
        lambda client, seq: client.extrusion_cali_sel(
            ams_id=ams_id,
            tray_id=tray_id,
            cali_idx=cali_idx,
            filament_id=filament_id,
            nozzle_diameter=nozzle_diameter,
            sequence_id=seq,
        ),
        target=ams_id * 4 + tray_id if ams_id < 128 else ams_id,
    )


def filament_setting(ams_id: int, tray_id: int, **setting: object) -> Command:
    """An ``ams_filament_setting`` slot identity write (the client's guarded write)."""
    return Command(
        "ams_filament_setting",
        lambda client, seq: client.ams_set_filament_setting(ams_id=ams_id, tray_id=tray_id, **setting, sequence_id=seq),
        target=ams_id * 4 + tray_id if ams_id < 128 else ams_id,
    )


def fan_percent(fan: int, percent: int) -> Command:
    """A fan speed (``M106`` through ``gcode_line``), as a percentage."""
    return Command("gcode_line", lambda client, seq: client.set_fan_percent(fan, percent, sequence_id=seq))


def print_start(filename: str, **options: object) -> Command:
    """A farm unit's ``project_file`` start of ``filename`` (``BambuMQTTClient.start_print``'s options)."""
    return Command("print_start", lambda client, seq: client.start_print(filename, **options, sequence_id=seq))


def eject_start(filename: str, **options: object) -> Command:
    """An eject sweep's ``project_file`` start of ``filename``."""
    return Command("eject_start", lambda client, seq: client.start_print(filename, **options, sequence_id=seq))


def drying(
    ams_id: int, temp: int, duration: int, *, mode: int = 1, filament: str = "", rotate_tray: bool = False
) -> Command:
    """An AMS drying start (``mode`` 1) or stop (``mode`` 0, temp and duration 0)."""
    return Command(
        "ams_filament_drying",
        lambda client, seq: client.send_drying_command(
            ams_id, temp, duration, mode, filament, rotate_tray, sequence_id=seq
        ),
        target=ams_id,
    )


def slicer_forward(frame: Mapping[str, object]) -> Command:
    """A slicer's own frame, forwarded to the real printer by a virtual printer's bridge
    (``virtual_printer.mqtt_bridge``): recorded as the OPERATOR's action (a person at a slicer) and published
    under the record's id. The slicer's own ``sequence_id`` never reaches the wire — an id the record did not
    allocate could be answered as a farm action that shares it; the bridge restores it on the reply."""
    wire = copy.deepcopy(dict(frame))
    return Command("slicer_forward", lambda client, seq: client.publish_forwarded(wire, sequence_id=seq))


# --- the self-heal bound ----------------------------------------------------------------------------


@dataclass(frozen=True)
class SelfHealBound:
    """A wait's self-heal fallback: after ``seconds`` of the printer's ONLINE time with no event answering
    the action, the lane takes production's next self-heal step. ``name`` is the lane's constant
    (``_RECHECK_SELF_HEAL``, the step bound), ``witness`` the measured evidence behind the number. Built
    only at module scope or inside a lane function named ``*_bound`` (``TestSelfHealBoundOwnership``). A
    bound with no name, no witness or no positive length RAISES — a fallback nobody can trace back to a
    measurement is a guess."""

    name: str
    seconds: float
    witness: str

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("SelfHealBound: a bound needs a name")
        if not self.witness.strip():
            raise ValueError(f"SelfHealBound {self.name}: a bound needs the measured evidence behind its number")
        if not self.seconds > 0:
            raise ValueError(f"SelfHealBound {self.name}: a bound is positive, got {self.seconds!r}")


@dataclass(frozen=True)
class Unanswered:
    """:func:`await_outcome`'s word when the action's self-heal bound passed before any event answered it:
    the bound's name and the record's NON-final reading of the action at that moment — an AMS motion
    command's motion reading (``acted`` / ``no_movement`` / ``held`` …, ``command_outcome.classify`` over
    the snapshot taken at the publish), any other action's :func:`reading_of` (``None`` when nothing can
    be read). The action itself stays outstanding."""

    bound_name: str
    reading: MotionReading | Reading | None


#: The record's knowledge of how long a printer takes to ACT on a farm ``print.stop``: unanswered this long in
#: the printer's online seconds, the stop's lane takes its fallback (the plate-check ladder's escalation,
#: ``pause_recovery``; the air stop's one resend, then its escalation, ``spool_recovery``). ONE origin for the
#: measured figure, because both lanes send the same verb to the same printers.
STOP_SELF_HEAL = SelfHealBound(
    "STOP_SELF_HEAL",
    120.0,
    "014-H2S 2026-10-05, episodes 472/473: an H2S stopped at the plate dialog reaches FAILED in 56–62 s; "
    "production _STOP_CONFIRM_S",
)


#: A family's bound for a REHYDRATED action that carries none (``printer_event_wiring`` registers them):
#: the bound and its anchor, or ``None`` for an action the family leaves unbounded.
BoundFactory = Callable[["OutstandingAction"], "tuple[SelfHealBound, datetime] | None"]


# --- the handle -----------------------------------------------------------------------------------

#: Why a command did not go out: no client, a publish the client refused, a load under a standing
#: runout hold (the firmware LATCHES it — 006-H2S 2026-07-26).
Refusal = Literal["not_connected", "not_sent", "runout_hold"]

_REFUSAL_REASON: dict[Refusal, str] = {
    "not_connected": "Printer not connected",
    "not_sent": "Printer not connected or busy. Command not sent.",
    "runout_hold": "the printer is paused for a same-slot refill; the AMS latches a load in this state",
}


@dataclass(frozen=True)
class Prepared:
    """A recorded, not yet published action (:func:`prepare`): its row exists, so its id — the sequence id
    the frame will carry — can be named by the caller's own record before the frame goes out."""

    action_id: int
    printer_id: int
    command: Command
    actor: Actor
    incident_id: int | None
    queue_item_id: int | None
    entry: AmsWireSnapshot
    entry_token: str | None
    job_id: str | None
    sent_at: datetime
    refusal: Refusal | None = None
    refusal_detail: str | None = None
    #: A start's: the job id the printer reported at the send (``PrinterAction.prior_job_id``).
    prior_job_id: str | None = None
    #: The printer's power-loss prompt stood at the send (``command_outcome.rebooted``): that reboot
    #: preceded the command and voids nothing of it (``command_outcome.Running.rebooted_at_send``).
    rebooted_at_send: bool = False
    #: The action's self-heal bound, armed at the publish with ``sent_at`` as its anchor.
    bound: SelfHealBound | None = None


@dataclass(frozen=True)
class Sent:
    """The handle of one action: its row id (the wire ``sequence_id``), what was sent, and whether it went
    out. ``entry`` is the wire at the send (the posture an AMS motion was sent into). ``refusal`` names why
    it did not go out (``published`` False), ``refusal_detail`` the operator sentence for it."""

    action_id: int
    printer_id: int
    command: str
    target: int | None
    actor: Actor
    entry: AmsWireSnapshot
    published: bool
    refusal: Refusal | None = None
    refusal_detail: str | None = None
    #: The client the frame went out on — the one its echo lands on (process memory, never recorded).
    client: BambuMQTTClient | None = field(default=None, compare=False, repr=False)

    @property
    def sequence_id(self) -> str:
        return str(self.action_id)


# --- the in-memory registry of outstanding actions --------------------------------------------------


@dataclass
class _Outstanding:
    facts: ActionFacts
    running: Running
    incident_id: int | None
    sent_at: datetime
    queue_item_id: int | None = None
    waiters: list[asyncio.Future[Answer]] = field(default_factory=list)
    #: :func:`await_outcome`'s waiters that named a bound: resolved by the answer or by the bound passing.
    bounded_waiters: list[asyncio.Future[Answer | Unanswered]] = field(default_factory=list)
    #: The self-heal bound and the instant its online seconds count from (``sent_at`` unless re-armed).
    bound: SelfHealBound | None = None
    anchor_at: datetime | None = None
    #: The bound passed (once; ``bound_passed_at``); ``passed_name`` is its name — ``None`` for a pass
    #: rehydrated from the row, whose bound this process never held.
    bound_passed: bool = False
    passed_name: str | None = None
    #: Rebuilt by :func:`rehydrate`: a bound-less one is offered to its family's factory, once one exists.
    rehydrated: bool = False
    #: An AMS motion command's entry snapshot (taken at the publish) and the observation folded from every
    #: status push since — what :class:`Unanswered` reads its motion reading off. In memory only.
    motion_entry: AmsWireSnapshot | None = None
    observation: Observation | None = None


# printer_id -> action_id -> the outstanding action.
_outstanding: dict[int, dict[int, _Outstanding]] = {}
# (command name | family) -> the factory that arms a rehydrated action's bound (``printer_event_wiring``).
_bound_factories: dict[str | Family, BoundFactory] = {}
# The online clock, per printer (naive UTC): the first fresh report in this process, the start of the
# current offline stretch (a lost session, until the next session's first fresh report), and the
# closed offline gaps an anchor may still reach.
_first_fresh: dict[int, datetime] = {}
_offline_since: dict[int, datetime] = {}
_gaps: dict[int, list[tuple[datetime, datetime]]] = {}
# The closed gaps kept per printer however old: the floor for a bound anchored on a non-action instant
# (:func:`bound_due`), which no registry entry names. Older gaps are kept only while an outstanding
# action's anchor still reaches them.
_GAP_KEEP = 16
# printer_id -> when an operator's AMS motion command last went out (naive UTC).
_last_operator_motion: dict[int, datetime] = {}
# printer_id -> when the printer last landed a load nobody commanded (naive UTC).
_observed_load_at: dict[int, datetime] = {}
# printer_id -> (feed-event seq, tray, actor) of the last landing this module attributed.
_last_landing: dict[int, tuple[int, int, Actor]] = {}
# printer_id -> (tray, actor) of a load a REPORT reached before its landing event was read.
_landing_owner: dict[int, tuple[int, Actor]] = {}
# action_id -> its final answer and the printer's reason, for actions answered in this process
# (bounded; the DB is the record).
_recent_answers: dict[int, Answer] = {}
_recent_reasons: dict[int, str | None] = {}
_RECENT_KEEP = 512
# printer_id -> the wake every :func:`await_next_session` waiter of the printer sleeps on, set (and
# dropped) by the record's session re-read (:func:`reread_session`). Process memory of coroutines in
# flight: a restart has none waiting, so nothing to rehydrate.
_session_wakes: dict[int, asyncio.Event] = {}
# The push cadence (~1 Hz) at which a next-session waiter re-reads the level as the backstop of its
# wake — a cadence, never a decision: the wait ends only on the new session's first fresh report.
_SESSION_RECHECK_S = 1.0


def _reset_state() -> None:
    """Drop every in-memory slot (tests)."""
    _outstanding.clear()
    _last_operator_motion.clear()
    _observed_load_at.clear()
    _last_landing.clear()
    _landing_owner.clear()
    _recent_answers.clear()
    _recent_reasons.clear()
    _session_wakes.clear()
    _bound_factories.clear()
    _first_fresh.clear()
    _offline_since.clear()
    _gaps.clear()


def _now() -> datetime:
    """Naive UTC, the record's clock: its timestamps, and the online seconds a self-heal bound counts
    (never an answer)."""
    return datetime.utcnow()


def _wire_word(state: object, name: str) -> str | None:
    """A wire string field of ``state`` — ``None`` when the state carries no string there."""
    value = getattr(state, name, None)
    return value if isinstance(value, str) and value else None


#: The families whose job is minted at the publish (the client's submission id).
_START_FAMILIES: frozenset[Family] = frozenset({Family.PRINT_START, Family.EJECT_START})


def _entry_token(command: Command, entry: AmsWireSnapshot, state: object) -> str | None:
    """The family's own reading at the send: an AMS motion's posture, any other verb's job word."""
    if command.family is Family.AMS_MOTION:
        return command_outcome.posture(entry)
    word = _wire_word(state, "state")
    return word.upper() if word is not None else None


# --- sending ----------------------------------------------------------------------------------------


async def prepare(
    printer_id: int,
    command: Command,
    *,
    actor: Actor,
    incident_id: int | None = None,
    queue_item_id: int | None = None,
    job_id: str | None = None,
    client: BambuMQTTClient | None = None,
    bound: SelfHealBound | None = None,
) -> Prepared:
    """RECORD ``command`` as an action of ``actor`` on ``printer_id`` — the row INSERTed and COMMITTED, its id
    the sequence id the frame will carry — without publishing it (:func:`publish` does). The entry is read
    off the printer's live state; ``job_id`` defaults to the job it runs. A start is sent against no job:
    the job it dispatches is minted at the publish (:func:`publish`), and the job the printer reports now
    is recorded as the one it replaces (``prior_job_id``). ``bound`` is the action's self-heal bound, armed
    at the publish (anchor: ``sent_at``)."""
    from backend.app.core.database import async_session

    live_client = client if client is not None else printer_manager.get_client(printer_id)
    # The entry is read off the client the frame goes out on (the registry's status IS its state).
    state = getattr(live_client, "state", None) if live_client is not None else None
    if state is None:
        state = printer_manager.get_status(printer_id)
    entry = command_outcome.snapshot(state)
    token = _entry_token(command, entry, state)
    starts = command.family in _START_FAMILIES
    reported_job = _wire_word(state, "subtask_id")
    job = job_id if job_id is not None else (None if starts else reported_job)
    prior_job = reported_job if starts else None
    refusal: Refusal | None = None
    detail: str | None = None
    if live_client is None:
        refusal, detail = "not_connected", _REFUSAL_REASON["not_connected"]
    elif command.name == "load" and runout_hold_active(state):
        refusal, detail = "runout_hold", _runout_hold_detail(printer_id, state)
    sent_at = _now()

    async def _insert() -> int:
        async with async_session() as db:
            row = PrinterAction(
                printer_id=printer_id,
                actor=actor,
                command=command.name,
                target=command.target,
                entry=token,
                job_id=job,
                prior_job_id=prior_job,
                incident_id=incident_id,
                queue_item_id=queue_item_id,
                sent_at=sent_at,
            )
            if refusal is not None:
                # The farm's own refusal: the token alone, ``reason`` stays the printer's words
                # (NULL here — the display text is derived from the token, :func:`reason_of`).
                row.answer = Answer.REFUSED.value
                row.refusal = refusal
                row.answered_at = sent_at
            elif not command_outcome.echoes(command.name):
                # No echo by design: nothing will ever answer it, and no answer is fabricated — the
                # row keeps a NULL answer and says why (:func:`publish` never registers it outstanding).
                row.reason = command_outcome.NO_ECHO_REASON
            db.add(row)
            await db.commit()
            if incident_id is not None:
                await printer_incidents.rederive_actions(db, incident_id)
            return row.id

    # Shielded: a decided send is recorded whole even when its caller is cancelled mid-write (a
    # cancelled caller then publishes nothing — the row stays outstanding until an event answers it).
    action_id = await asyncio.shield(_insert())
    return Prepared(
        action_id=action_id,
        printer_id=printer_id,
        command=command,
        actor=actor,
        incident_id=incident_id,
        queue_item_id=queue_item_id,
        entry=entry,
        entry_token=token,
        job_id=job,
        sent_at=sent_at,
        refusal=refusal,
        refusal_detail=detail,
        prior_job_id=prior_job,
        rebooted_at_send=command_outcome.rebooted(getattr(state, "hms_errors", None)),
        bound=bound,
    )


async def publish(
    prepared: Prepared, *, client: BambuMQTTClient | None = None, bound: SelfHealBound | None = None
) -> Sent:
    """Put a :func:`prepare`\\ d action on the wire under its own id. The client is the registry's CURRENT one
    unless the caller hands the one it validated. A publish that does not go out answers the row REFUSED
    at once; one that does is registered OUTSTANDING in the same synchronous stretch (no await between),
    so an ACK the loop reads next always finds it — with its self-heal bound (``bound``, else the one
    :func:`prepare` was given) anchored at ``sent_at``."""
    command = prepared.command
    if prepared.refusal is not None:
        logger.warning(
            "[printer-action] %s actor=%s printer=%s command=%s target=%s not sent (%s)",
            prepared.action_id,
            prepared.actor,
            prepared.printer_id,
            command.name,
            command.target,
            prepared.refusal,
        )
        _remember(prepared.action_id, Answer.REFUSED, _REFUSAL_REASON[prepared.refusal])
        return _sent(prepared, published=False, refusal=prepared.refusal, detail=prepared.refusal_detail)
    live_client = client if client is not None else printer_manager.get_client(prepared.printer_id)
    published = False
    if live_client is not None:
        try:
            published = bool(command.publish(live_client, str(prepared.action_id)))
        except Exception:  # noqa: BLE001 — a publisher failure is a command that did not go out
            logger.exception(
                "[printer-action] %s publish of %s raised on printer %s",
                prepared.action_id,
                command.name,
                prepared.printer_id,
            )
    if not published:
        refusal: Refusal = "not_connected" if live_client is None else "not_sent"
        logger.warning(
            "[printer-action] %s actor=%s printer=%s command=%s target=%s not sent (%s)",
            prepared.action_id,
            prepared.actor,
            prepared.printer_id,
            command.name,
            command.target,
            refusal,
        )
        _remember(prepared.action_id, Answer.REFUSED, _REFUSAL_REASON[refusal])
        # Answered before the send returns, so the hold's projection never reads a command that never
        # left as one still pending.
        await _write_answer(prepared.action_id, prepared.incident_id, Answer.REFUSED, None, refusal=refusal)
        return _sent(prepared, published=False, refusal=refusal, detail=_REFUSAL_REASON[refusal], client=live_client)
    if not command_outcome.echoes(command.name):
        # No echo by design (``command_outcome.CommandSpec.echoes``): never registered outstanding, so
        # never rehydrated, never paged, never answered — its row keeps the NULL answer :func:`prepare`
        # wrote with its reason.
        logger.info(
            "[printer-action] %s actor=%s printer=%s command=%s target=%s seq=%s (%s)",
            prepared.action_id,
            prepared.actor,
            prepared.printer_id,
            command.name,
            command.target,
            prepared.action_id,
            command_outcome.NO_ECHO_REASON,
        )
        if prepared.actor == ACTOR_OPERATOR:
            _person_acted(prepared.printer_id, prepared.action_id, command.family)
        return _sent(prepared, published=True, client=live_client)
    # The job a start dispatched is minted inside the client's publisher (its submission id).
    minted = _wire_word(live_client, "last_dispatch_subtask_id") if command.family in _START_FAMILIES else None
    job_id = minted or prepared.job_id
    armed = bound if bound is not None else prepared.bound
    motion = command.family is Family.AMS_MOTION
    entry = _Outstanding(
        facts=ActionFacts(
            action_id=prepared.action_id,
            printer_id=prepared.printer_id,
            command=command.name,
            actor=prepared.actor,
            target=command.target,
            entry=prepared.entry_token,
            job_id=job_id,
            prior_job_id=prepared.prior_job_id,
        ),
        running=Running(sent_epoch=prepared.entry.connection_epoch, rebooted_at_send=prepared.rebooted_at_send),
        incident_id=prepared.incident_id,
        sent_at=prepared.sent_at,
        queue_item_id=prepared.queue_item_id,
        bound=armed,
        anchor_at=prepared.sent_at if armed is not None else None,
        motion_entry=prepared.entry if motion else None,
        observation=Observation() if motion else None,
    )
    _outstanding.setdefault(prepared.printer_id, {})[prepared.action_id] = entry
    logger.info(
        "[printer-action] %s actor=%s printer=%s command=%s target=%s seq=%s entry=%s job=%s incident=%s%s",
        prepared.action_id,
        prepared.actor,
        prepared.printer_id,
        command.name,
        command.target,
        prepared.action_id,
        prepared.entry_token,
        job_id,
        prepared.incident_id,
        f" bound={armed.name}:{armed.seconds:g}s" if armed is not None else "",
    )
    if minted is not None:
        spawn_background_task(_write_job(prepared.action_id, minted), name=f"printer-action-job-{prepared.action_id}")
    if prepared.actor == ACTOR_OPERATOR:
        _person_acted(prepared.printer_id, prepared.action_id, command.family)
    return _sent(prepared, published=True, client=live_client)


async def send(
    printer_id: int,
    command: Command,
    *,
    actor: Actor,
    incident_id: int | None = None,
    queue_item_id: int | None = None,
    job_id: str | None = None,
    client: BambuMQTTClient | None = None,
    bound: SelfHealBound | None = None,
) -> Sent:
    """Record and publish ``command`` (:func:`prepare` + :func:`publish`) — THE send of every state-changing
    command a printer gets. Returns the handle; ``published`` says whether the frame went out. ``bound``:
    the action's self-heal bound, anchored at the send."""
    prepared = await prepare(
        printer_id,
        command,
        actor=actor,
        incident_id=incident_id,
        queue_item_id=queue_item_id,
        job_id=job_id,
        client=client,
        bound=bound,
    )
    return await publish(prepared, client=client)


def send_soon(printer_id: int, *commands: Command, actor: Actor, incident_id: int | None = None) -> bool:
    """The send of a SYNC caller (a sampler on the status callback that cannot await): :func:`send` of
    each command, IN ORDER, in one spawned task — the row is still written BEFORE each publish, so each
    frame still carries its action id, and a command whose publish does not go out ends the sequence
    (a vendor pair that half-lands is never half-sent). Answers whether a client with a live session
    exists to send on — the refusal a sync caller can still act on (the cooldown's off-retry lane); any
    later refusal of the publish lands on the record (``refused``), never with the caller. A caller that needs the publish's answer before it moves on awaits :func:`send`."""
    client = printer_manager.get_client(printer_id)
    if client is None or getattr(getattr(client, "state", None), "connected", True) is False:
        return False

    async def _in_order() -> None:
        for command in commands:
            if not (await send(printer_id, command, actor=actor, incident_id=incident_id)).published:
                return

    spawn_background_task(_in_order(), name=f"printer-action-send-p{printer_id}")
    return True


async def operator_verb(
    printer_id: int,
    name: str,
    publish: Callable[[BambuMQTTClient, str], object],
    *,
    target: int | None = None,
    client: BambuMQTTClient | None = None,
) -> bool:
    """An operator's route sending one verb (``name`` in the record's vocabulary): recorded as the
    operator's action and published under its id. True when the frame went out."""
    sent = await send(printer_id, Command(name, publish, target), actor=ACTOR_OPERATOR, client=client)
    return sent.published


def _sent(
    prepared: Prepared,
    *,
    published: bool,
    refusal: Refusal | None = None,
    detail: str | None = None,
    client: BambuMQTTClient | None = None,
) -> Sent:
    return Sent(
        action_id=prepared.action_id,
        printer_id=prepared.printer_id,
        command=prepared.command.name,
        target=prepared.command.target,
        actor=prepared.actor,
        entry=prepared.entry,
        published=published,
        refusal=refusal,
        refusal_detail=detail,
        client=client,
    )


def _runout_hold_detail(printer_id: int, state: PrinterState | None) -> str:
    info = printer_manager.get_printer(printer_id)
    name = info.name if info is not None else f"Printer {printer_id}"
    demand = current_runout_demand(getattr(state, "hms_errors", None) or [])
    where = printer_incidents.runout_slot_desc(demand[0] * 4 + demand[1]) if demand else None
    return (
        f"{name} is PAUSEd waiting for a filament refill in "
        f"{where or 'the slot that ran out'}. The AMS executes no load in this state — the "
        "request would latch in the firmware and resurface at the resume as a bogus demand for "
        f"this slot (006-H2S 2026-07-26). Insert filament into {where or 'the slot that ran out'} "
        "and the print resumes from there."
    )


# --- awaiting -------------------------------------------------------------------------------------


@overload
async def await_outcome(sent: Sent) -> Answer: ...


@overload
async def await_outcome(sent: Sent, *, bound: SelfHealBound | None) -> Answer | Unanswered: ...


async def await_outcome(sent: Sent, *, bound: SelfHealBound | None = None) -> Answer | Unanswered:
    """The action's final answer, once an EVENT gives it. A command that never went out answers
    ``refused`` at once.

    Without ``bound``: no timeout — an action nothing answers stays outstanding (the attention owner pages
    a person); a caller that must stop waiting cancels its task.

    With ``bound`` (the lane's self-heal fallback): armed on the action when it carries none (anchor:
    ``sent_at``), and :class:`Unanswered` the moment the action's bound passes in online time with no
    answer — at once when it already has. The action stays outstanding either way; its late answer is
    the record's (``ActionAnswered``).

    A command with no echo by design (``command_outcome.echoes``) has nothing to await: asking RAISES
    (``ValueError``) rather than fabricating a ``void`` for a row no event will ever answer."""
    if not sent.published:
        return Answer.REFUSED
    if not command_outcome.echoes(sent.command):
        raise ValueError(f"printer_action: {sent.command} has {command_outcome.NO_ECHO_REASON}; nothing answers it")
    answered = _recent_answers.get(sent.action_id)
    if answered is not None:
        return answered
    entry = _outstanding.get(sent.printer_id, {}).get(sent.action_id)
    if entry is None:
        answer = await answer_of(sent.action_id)
        return answer if answer is not None else Answer.VOID
    loop = asyncio.get_running_loop()
    if bound is None:
        future: asyncio.Future[Answer] = loop.create_future()
        entry.waiters.append(future)
        return await future
    if entry.bound is None and not entry.bound_passed:
        _arm(entry, bound, entry.sent_at)
    if not entry.bound_passed:
        _check_entry(entry, None)
    if entry.bound_passed:
        return _unanswered(entry, None, fallback_name=bound.name)
    bounded: asyncio.Future[Answer | Unanswered] = loop.create_future()
    entry.bounded_waiters.append(bounded)
    return await bounded


async def ack_within(sent: Sent, seconds: float) -> CommandAck | None:
    """The firmware's ACK of an ACK-answered verb (a dialog press), read for ``seconds`` by a caller that
    must answer a person NOW (the HMS modal's click, the plate-check press's watch). The bound decides
    only what the caller says; the ACTION's answer is the record's, on the ACK whenever it lands.
    ``None`` = no ACK in the window (or the command never went out)."""
    if not sent.published:
        return None
    ack = ack_of(sent)
    if ack is not None:
        return ack
    try:
        await asyncio.wait_for(await_outcome(sent), seconds)
    except TimeoutError:
        pass
    return ack_of(sent)


def outstanding(action_id: int | None) -> bool:
    """Is this action outstanding (sent, no event has answered it)? In-memory; ``None`` is no action."""
    if action_id is None:
        return False
    return any(action_id in actions for actions in _outstanding.values())


def bound_passed(action_id: int | None) -> bool:
    """Has the record passed this OUTSTANDING action's self-heal bound (evaluated on the printer's status
    pushes and the attention owner's pass, in online seconds)? THE in-memory read, for a lane awaiting its
    own send: one registry probe per printer, no copy of the registry built. False for an action this
    process holds no longer outstanding — an ANSWERED action's bound is the row's column, read through
    :func:`bound_passed_of` (its registry entry is gone once an event answered it)."""
    if action_id is None:
        return False
    for actions in _outstanding.values():
        entry = actions.get(action_id)
        if entry is not None:
            return entry.bound_passed
    return False


async def settled(action_id: int) -> Answer | None:
    """The action's final answer if an EVENT has given it, else ``None`` — it is still outstanding.
    Never waits: the read a driver makes at its own push cadence while it watches the printer for
    everything else its wait owes (a takeover, the session gate) — the answer itself is still only
    an event's (:func:`await_outcome`'s rule). A command that never went out reads ``refused``; an
    action this process holds neither outstanding nor answered reads the row (``void`` for a row an
    event never answered and no registry holds — :func:`await_outcome`'s rule; a command with no echo by
    design RAISES ``ValueError``, as there)."""
    answered = _recent_answers.get(action_id)
    if answered is not None:
        return answered
    if outstanding(action_id):
        return None
    from backend.app.core.database import async_session

    async with async_session() as db:
        row = (
            await db.execute(select(PrinterAction.answer, PrinterAction.command).where(PrinterAction.id == action_id))
        ).first()
    if row is not None and row.answer is not None:
        return Answer(row.answer)
    if row is not None and not command_outcome.echoes(row.command):
        raise ValueError(f"printer_action: action {action_id} ({row.command}) has {command_outcome.NO_ECHO_REASON}")
    return Answer.VOID


async def reason_of(action_id: int) -> str | None:
    """The record's reason for an answered action — a refusal's printer words, why it can never be
    reached, or, for a frame that never left the farm, the sentence its ``refusal`` token names
    (:data:`_REFUSAL_REASON`; ``reason`` itself is never a function of ``refusal``). ``None`` for none,
    or no such row."""
    if action_id in _recent_reasons:
        return _recent_reasons[action_id]
    from backend.app.core.database import async_session

    async with async_session() as db:
        row = (
            await db.execute(select(PrinterAction.reason, PrinterAction.refusal).where(PrinterAction.id == action_id))
        ).first()
    if row is None:
        return None
    reason, refusal = row
    return reason if refusal is None else _REFUSAL_REASON.get(refusal, refusal)


async def answer_of(action_id: int) -> Answer | None:
    """The action's final answer as the record holds it (``None`` while outstanding, or no such row)."""
    answered = _recent_answers.get(action_id)
    if answered is not None:
        return answered
    from backend.app.core.database import async_session

    async with async_session() as db:
        value = await db.scalar(select(PrinterAction.answer).where(PrinterAction.id == action_id))
    return Answer(value) if value is not None else None


async def unit_of(action_id: int) -> int | None:
    """The queue unit action ``action_id`` was sent for (``PrinterAction.queue_item_id``); ``None`` for
    none, or no such row. ``services/dispatch_start``'s read of an answered ``print_start``."""
    from backend.app.core.database import async_session

    async with async_session() as db:
        return await db.scalar(select(PrinterAction.queue_item_id).where(PrinterAction.id == action_id))


async def last_start_of(db: AsyncSession, queue_item_id: int) -> int | None:
    """The newest ``print_start`` action recorded for unit ``queue_item_id`` — its dispatch's durable send
    record — or ``None`` when the record holds none (a claim committed before the record, or a crash
    between the claim and the send)."""
    return await db.scalar(
        select(func.max(PrinterAction.id)).where(
            PrinterAction.queue_item_id == queue_item_id, PrinterAction.command == "print_start"
        )
    )


async def bound_passed_of(db: AsyncSession, action_id: int) -> bool:
    """Did action ``action_id``'s self-heal bound pass before it was answered (``bound_passed_at``)? The read
    of an ANSWERED action, whose registry entry is gone — ``services/dispatch_start``'s late-answer guard (a
    start answered after its bound already returned the unit touches no claim and no lease). An action still
    outstanding is read in memory, :func:`bound_passed`."""
    return await db.scalar(select(PrinterAction.bound_passed_at).where(PrinterAction.id == action_id)) is not None


def reading_of(action_id: int) -> command_outcome.Reading | None:
    """The NON-final reading of an outstanding action, ``None`` when it is not outstanding: ``landed`` — a start
    whose job id the printer reported in a non-active word (``command_outcome.Running.start_landed``, it outranks
    the ACK: the printer demonstrably has the start); ``acked``; else ``sent``."""
    for actions in _outstanding.values():
        entry = actions.get(action_id)
        if entry is not None:
            if entry.running.start_landed:
                return command_outcome.Reading.LANDED
            if entry.running.acked is True:
                return command_outcome.Reading.ACKED
            return command_outcome.Reading.SENT
    return None


@dataclass(frozen=True)
class OutstandingAction:
    """One outstanding action as the attention owner reads it (``farm_stall``, owner 5) and a bound factory
    is offered it: what was sent, by whom, on which hold or unit, when, whether the printer accepted it,
    and whether its self-heal bound passed (the lane then pages its own hand-over). A read-only copy of
    the registry's entry — the record is still the only thing that answers it."""

    action_id: int
    printer_id: int
    command: str
    actor: Actor
    target: int | None
    incident_id: int | None
    sent_at: datetime
    acked: bool
    bound_passed: bool = False
    queue_item_id: int | None = None


def _copy_of(entry: _Outstanding) -> OutstandingAction:
    facts = entry.facts
    return OutstandingAction(
        action_id=facts.action_id,
        printer_id=facts.printer_id,
        command=facts.command,
        actor=facts.actor,
        target=facts.target,
        incident_id=entry.incident_id,
        sent_at=entry.sent_at,
        acked=entry.running.acked is True,
        bound_passed=entry.bound_passed,
        queue_item_id=entry.queue_item_id,
    )


def outstanding_actions() -> list[OutstandingAction]:
    """Every outstanding action, oldest first — the in-memory registry the record rehydrates at startup, so
    the attention owner's read costs no query (O(outstanding), independent of the fleet's size)."""
    rows = [_copy_of(entry) for actions in _outstanding.values() for entry in actions.values()]
    return sorted(rows, key=lambda row: row.sent_at)


def ack_of(sent: Sent) -> CommandAck | None:
    """The firmware's ACK for ``sent``, from the ACK log of the client the frame went out on (else the
    registry's current one) — the ONE caller of ``BambuMQTTClient.ack_for``. ``None`` = none has
    arrived yet, or no client."""
    client = sent.client if sent.client is not None else printer_manager.get_client(sent.printer_id)
    if client is None:
        return None
    ack = client.ack_for(sent.sequence_id)
    return ack if isinstance(ack, CommandAck) else None


def operator_commanded_since(printer_id: int, since: datetime) -> bool:
    """Did an OPERATOR's AMS motion command go out on this printer at or after ``since`` (naive UTC)? The
    record's answer (durable: rebuilt from the rows at startup)."""
    at = _last_operator_motion.get(printer_id)
    return at is not None and at >= since


def observed_load_at(printer_id: int) -> datetime | None:
    """When the printer last landed a load NOBODY commanded (its own backup switch, a person's hand) — the
    ONE store of "whose load landed" (naive UTC; durable)."""
    return _observed_load_at.get(printer_id)


def landed_by_command(printer_id: int, tray: int) -> bool:
    """Did a farm or operator load of ``tray`` cause the landing being read now? An outstanding load of it,
    or the landing this module already attributed to one. The backup-swap lane's question (never a
    firmware runout)."""
    for entry in _outstanding.get(printer_id, {}).values():
        if entry.facts.command == "load" and valid_feeder(entry.facts.target) == tray:
            return True
    owner = _landing_owner.get(printer_id)
    if owner is not None and owner[0] == tray:
        return True
    last = _last_landing.get(printer_id)
    return last is not None and last[1] == tray and last[2] != ACTOR_PRINTER


# --- the self-heal bound: the online clock and the evaluation ---------------------------------------


def online_seconds_since(printer_id: int, anchor: datetime) -> float:
    """How many seconds printer ``printer_id`` has been ONLINE since ``anchor`` (naive UTC, the record's
    clock). Offline is a lost MQTT session (``SessionLost``) until the next session's first fresh report
    (:func:`reread_session`), never a quiet report. The effective anchor is the later of ``anchor`` and
    the printer's first fresh report in this process: an anchor older than this process's session counts
    from there, and a printer that has not reported fresh in this process has been online 0 s."""
    first = _first_fresh.get(printer_id)
    if first is None:
        return 0.0
    start = max(anchor, first)
    end = _offline_since.get(printer_id) or _now()
    if end <= start:
        return 0.0
    online = (end - start).total_seconds()
    for gap_start, gap_end in _gaps.get(printer_id, ()):
        overlap_start, overlap_end = max(gap_start, start), min(gap_end, end)
        if overlap_end > overlap_start:
            online -= (overlap_end - overlap_start).total_seconds()
    return max(online, 0.0)


def now() -> datetime:
    """The record's clock (naive UTC) — the instant a lane takes as the anchor of a bound no action's send
    anchors (an edge it witnessed, e.g. the first RUNNING sighting after a press), so the anchor and the
    online seconds :func:`bound_due` counts from it read ONE clock."""
    return _now()


def bound_due(printer_id: int, bound: SelfHealBound, anchor_at: datetime) -> bool:
    """Has ``bound`` passed in ``printer_id``'s online time since ``anchor_at``? For a bound anchored on an
    instant that is no action's send (a fault edge, an eject's start echo); the lane that asks reports the
    pass through :func:`note_bound_passed`."""
    return online_seconds_since(printer_id, anchor_at) >= bound.seconds


def note_bound_passed(printer_id: int, bound: SelfHealBound, *, lane: str, action_id: int | None = None) -> None:
    """A lane's non-action bound (:func:`bound_due`) passed: the one ``[self-heal]`` line, naming the lane,
    and — for an action it names — ``bound_passed_at`` (written once) and the action marked passed, so the
    attention owner leaves it to the lane's own hand-over and its bounded waiters get :class:`Unanswered`.
    Publishes nothing: the lane acts itself."""
    logger.warning(
        "[self-heal] action %s printer=%s lane=%s bound=%s unanswered after %.0fs online (witness: %s)",
        action_id if action_id is not None else "-",
        printer_id,
        lane,
        bound.name,
        bound.seconds,
        bound.witness,
    )
    if action_id is None:
        return
    entry = _outstanding.get(printer_id, {}).get(action_id)
    if entry is not None and not entry.bound_passed:
        entry.bound_passed = True
        entry.passed_name = bound.name
        _resolve_bounded(entry, None, bound.name)
    spawn_background_task(_write_bound_passed(action_id, _now()), name=f"printer-action-bound-{action_id}")


def arm_bound(action_id: int, bound: SelfHealBound, *, anchor_at: datetime) -> bool:
    """Arm — or re-arm — the self-heal bound of OUTSTANDING action ``action_id``, counted from ``anchor_at``
    (an anchor that arrives after the send: a start's landed reading, a RUNNING edge). False when no such
    action is outstanding, or its bound already passed: a fallback fires once per action (its
    ``bound_passed_at`` is written once), so a passed bound is never re-armed."""
    entry = next((actions[action_id] for actions in _outstanding.values() if action_id in actions), None)
    if entry is None or entry.bound_passed:
        return False
    _arm(entry, bound, anchor_at)
    _check_entry(entry, None)
    return True


def register_bound_factory(key: str | Family, factory: BoundFactory) -> Callable[[], None]:
    """Register the factory that arms a REHYDRATED action's bound, for a command name or a whole family (a
    name wins over its family). Offered at the action's first evaluation after a factory exists, so the
    lifespan's order — :func:`rehydrate` before ``printer_event_wiring.wire`` — never matters. Called only
    by the composition root. Returns the unregister."""
    if isinstance(key, str) and not isinstance(key, Family):
        command_outcome.spec_of(key)  # a name outside the vocabulary RAISES here, at registration
    _bound_factories[key] = factory

    def unregister() -> None:
        if _bound_factories.get(key) is factory:
            del _bound_factories[key]

    return unregister


def check_bounds() -> None:
    """Evaluate every outstanding action's bound on every printer — the attention owner's pass
    (``farm_stall.check_waiting_on_printers``), for the printer that reports rarely. In memory, O(outstanding)."""
    for printer_id in list(_outstanding):
        _check_bounds(printer_id)


def _arm(entry: _Outstanding, bound: SelfHealBound, anchor_at: datetime) -> None:
    entry.bound = bound
    entry.anchor_at = anchor_at


def _factory_of(entry: _Outstanding) -> BoundFactory | None:
    return _bound_factories.get(entry.facts.command) or _bound_factories.get(entry.facts.family)


def _offer_to_factory(entry: _Outstanding) -> None:
    """A rehydrated action with no bound gets its family's (once a factory is registered). A factory that
    raises leaves it unbounded, logged — never the event chain broken."""
    factory = _factory_of(entry)
    if factory is None:
        return
    entry.rehydrated = False  # offered once
    try:
        armed = factory(_copy_of(entry))
    except Exception:  # noqa: BLE001 — a factory must never break the status chain (invariant 10)
        logger.exception("[printer-action] %s bound factory raised", entry.facts.action_id)
        return
    if armed is not None:
        bound, anchor_at = armed
        _arm(entry, bound, anchor_at)


def _check_bounds(printer_id: int, state: PrinterState | None = None) -> None:
    """Evaluate the bounds of ``printer_id``'s outstanding actions (none = no work)."""
    for entry in list(_outstanding.get(printer_id, {}).values()):
        try:
            _check_entry(entry, state)
        except Exception:  # noqa: BLE001 — one action's bound must never break the event chain
            logger.exception("[printer-action] %s bound could not be evaluated", entry.facts.action_id)


def _check_entry(entry: _Outstanding, state: PrinterState | None) -> None:
    if entry.bound_passed:
        return
    if entry.bound is None and entry.rehydrated:
        _offer_to_factory(entry)
    if entry.bound is None or entry.anchor_at is None:
        return
    online = online_seconds_since(entry.facts.printer_id, entry.anchor_at)
    if online >= entry.bound.seconds:
        _pass_bound(entry, online, state)


def _pass_bound(entry: _Outstanding, online: float, state: PrinterState | None) -> None:
    """The bound FIRST passed with no event answering the action: the column, the line, the hub event, the
    bounded waiters — and NOT the answer (the action stays outstanding; a late event answers it)."""
    facts = entry.facts
    bound = entry.bound
    assert bound is not None
    entry.bound_passed = True
    entry.passed_name = bound.name
    logger.warning(
        "[self-heal] action %s printer=%s command=%s bound=%s unanswered after %.0fs online (witness: %s)",
        facts.action_id,
        facts.printer_id,
        facts.command,
        bound.name,
        online,
        bound.witness,
    )
    spawn_background_task(_write_bound_passed(facts.action_id, _now()), name=f"printer-action-bound-{facts.action_id}")
    spawn_background_task(
        printer_events.publish(
            SelfHealDue(
                printer_id=facts.printer_id,
                action_id=facts.action_id,
                command=facts.command,
                bound_name=bound.name,
                incident_id=entry.incident_id,
                queue_item_id=entry.queue_item_id,
            )
        ),
        name=f"printer-action-self-heal-{facts.action_id}",
    )
    _resolve_bounded(entry, state, bound.name)


def _resolve_bounded(entry: _Outstanding, state: PrinterState | None, name: str) -> None:
    """Every bounded waiter of a passed bound gets :class:`Unanswered` (the unbounded ones keep waiting for
    the answer)."""
    if not entry.bounded_waiters:
        return
    unanswered = _unanswered(entry, state, fallback_name=name)
    for waiter in entry.bounded_waiters:
        if not waiter.done():
            waiter.set_result(unanswered)
    entry.bounded_waiters.clear()


def _unanswered(entry: _Outstanding, state: PrinterState | None, *, fallback_name: str) -> Unanswered:
    """The action's NON-final reading at its bound: an AMS motion command's motion reading (the classifier
    over the entry snapshot, the live snapshot and the observation), any other — or a rehydrated motion
    command, whose snapshot is gone — :func:`reading_of`."""
    name = entry.passed_name or fallback_name
    facts = entry.facts
    if entry.motion_entry is None or entry.observation is None or facts.command not in ("load", "unload"):
        return Unanswered(name, reading_of(facts.action_id))
    live = state if state is not None else printer_manager.get_status(facts.printer_id)
    entry.observation.fold_ack(entry.running.acked)
    command: MotionCommand = "load" if facts.command == "load" else "unload"
    reading = command_outcome.classify(
        command,
        facts.target,
        entry.motion_entry,
        command_outcome.snapshot(live),
        observation=entry.observation,
        elapsed_s=online_seconds_since(facts.printer_id, entry.sent_at),
        deadline_s=entry.bound.seconds if entry.bound is not None else 0.0,
    )
    return Unanswered(name, reading if reading is not None else reading_of(facts.action_id))


def _fold_motion(entry: _Outstanding, state: PrinterState) -> None:
    """Fold one status push into an AMS motion command's observation, on the online timeline since its send
    (a push of another session is the classifier's cross-session read, never folded)."""
    if entry.motion_entry is None or entry.observation is None:
        return
    now = command_outcome.snapshot(state)
    if now.connection_epoch != entry.motion_entry.connection_epoch:
        return
    entry.observation.fold(entry.motion_entry, now, online_seconds_since(entry.facts.printer_id, entry.sent_at))


def on_session_lost(event: SessionLost) -> None:
    """The printer's MQTT session ended: its online clock stops here until the next session's first fresh
    report (:func:`reread_session`). Before any fresh report in this process there is nothing to stop."""
    pid = event.printer_id
    if pid in _first_fresh and pid not in _offline_since:
        _offline_since[pid] = _now()


def _session_began(printer_id: int) -> None:
    """The online clock at a session's first fresh report: the process's first one is the floor of every
    anchor; a lost session's offline stretch becomes a closed gap, and gaps no anchor can reach are pruned."""
    now = _now()
    _first_fresh.setdefault(printer_id, now)
    offline_since = _offline_since.pop(printer_id, None)
    if offline_since is None:
        return
    gaps = _gaps.setdefault(printer_id, [])
    gaps.append((offline_since, now))
    # An entry reads the clock from its send (its motion observation) and from its bound's anchor.
    anchors = [
        min(entry.sent_at, entry.anchor_at or entry.sent_at) for entry in _outstanding.get(printer_id, {}).values()
    ]
    oldest = min(anchors) if anchors else now
    kept_by_anchor = [gap for gap in gaps[:-_GAP_KEEP] if gap[1] > oldest]
    _gaps[printer_id] = kept_by_anchor + gaps[-_GAP_KEEP:]


# --- reading events --------------------------------------------------------------------------------


def _evaluate(printer_id: int, event: command_outcome.Event, *, only: Iterable[int] | None = None) -> None:
    """Read ``event`` into every outstanding action of ``printer_id`` (or the named ones). Sync and
    in-memory; a final answer is written by a spawned task."""
    actions = _outstanding.get(printer_id)
    if not actions:
        return
    for action_id in list(only if only is not None else actions):
        entry = actions.get(action_id)
        if entry is None:
            continue
        try:
            outcome = command_outcome.read(entry.facts, entry.running, event)
        except Exception:  # noqa: BLE001 — one action's table must never break the event chain
            logger.exception("[printer-action] %s could not read %s", action_id, type(event).__name__)
            continue
        if outcome.final:
            assert isinstance(outcome.verdict, Answer)
            _answer(entry, outcome.verdict, outcome.reason)


def _answer(entry: _Outstanding, answer: Answer, reason: str | None) -> None:
    """Answer one outstanding action: off the registry, its waiters completed, the row written."""
    facts = entry.facts
    actions = _outstanding.get(facts.printer_id, {})
    if actions.pop(facts.action_id, None) is None:
        return
    _remember(facts.action_id, answer, reason)
    if entry.incident_id is not None:
        printer_incidents.project_action_answer(
            facts.printer_id, entry.incident_id, facts.action_id, answer.value, reason
        )
    if answer is Answer.REACHED and facts.command == "load" and facts.target is not None:
        tray = valid_feeder(facts.target)
        if tray is not None:
            _landing_owner[facts.printer_id] = (tray, facts.actor)
    for waiter in entry.waiters:
        if not waiter.done():
            waiter.set_result(answer)
    for bounded in entry.bounded_waiters:
        if not bounded.done():
            bounded.set_result(answer)
    logger.info(
        "[printer-action] %s printer=%s command=%s answer=%s%s",
        facts.action_id,
        facts.printer_id,
        facts.command,
        answer.value,
        f" ({reason})" if reason else "",
    )
    spawn_background_task(
        _write_answer(facts.action_id, entry.incident_id, answer, reason), name=f"printer-action-{facts.action_id}"
    )
    spawn_background_task(
        printer_events.publish(
            ActionAnswered(
                printer_id=facts.printer_id,
                action_id=facts.action_id,
                command=facts.command,
                actor=facts.actor,
                incident_id=entry.incident_id,
                answer=answer.value,
                reason=reason,
            )
        ),
        name=f"printer-action-answered-{facts.action_id}",
    )


def _remember(action_id: int, answer: Answer, reason: str | None) -> None:
    _recent_answers[action_id] = answer
    _recent_reasons[action_id] = reason
    while len(_recent_answers) > _RECENT_KEEP:
        oldest = next(iter(_recent_answers))
        _recent_answers.pop(oldest)
        _recent_reasons.pop(oldest, None)


def _person_acted(printer_id: int, action_id: int, family: Family) -> None:
    """An operator's command went out: it may take over the farm's outstanding actions of a conflicting
    family; an AMS motion command is also the record of the operator's hand on the AMS."""
    if family is Family.AMS_MOTION:
        _last_operator_motion[printer_id] = _now()
    others = [other for other in _outstanding.get(printer_id, {}) if other != action_id]
    _evaluate(printer_id, command_outcome.PersonActed(family), only=others)


def on_command_acked(event: CommandAcked) -> None:
    """The firmware's ACK, matched to its action by sequence id (the row id)."""
    try:
        action_id = int(event.ack.sequence_id)
    except (TypeError, ValueError):
        return
    entry = _outstanding.get(event.printer_id, {}).get(action_id)
    if entry is None:
        return
    if entry.observation is not None:
        entry.observation.fold_ack(event.ack.succeeded)
    outcome = command_outcome.read(
        entry.facts, entry.running, command_outcome.Acked(event.ack.succeeded, event.ack.reason)
    )
    if outcome.final:
        assert isinstance(outcome.verdict, Answer)
        _answer(entry, outcome.verdict, outcome.reason)
    elif event.ack.succeeded:
        spawn_background_task(_write_acked(action_id), name=f"printer-action-ack-{action_id}")


def on_forward_answered(printer_id: int, action_id: int) -> None:
    """The printer's RESULT-LESS reply to a forwarded slicer frame (:func:`slicer_forward`) — a data reply
    (``get_access_code``, a read) carrying no ``result`` — relayed by the bridge and named by its sequence id,
    the action's id: the printer's success, since the ACK lane records only a reply that carries a
    ``result``. A reply that carries one is the ACK lane's alone (``BambuMQTTClient._record_command_acks``
    reads every block), so it is answered once. Called on the event loop (the bridge hops off its thread)."""
    entry = _outstanding.get(printer_id, {}).get(action_id)
    if entry is None or entry.facts.command != "slicer_forward":
        return
    outcome = command_outcome.read(entry.facts, entry.running, command_outcome.Acked(True, None))
    if outcome.final:
        assert isinstance(outcome.verdict, Answer)
        _answer(entry, outcome.verdict, outcome.reason)


def on_status_reported(event: StatusReported) -> None:
    """A status push: every outstanding action of the printer reads it, then each still outstanding folds
    it into its motion observation and has its self-heal bound evaluated (in-memory, O(outstanding on the
    printer); none = no work)."""
    if not _outstanding.get(event.printer_id):
        return
    _evaluate(event.printer_id, command_outcome.Reported(WireReading.of(event.state)))
    for entry in list(_outstanding.get(event.printer_id, {}).values()):
        try:
            _fold_motion(entry, event.state)
        except Exception:  # noqa: BLE001 — one action's observation must never break the event chain
            logger.exception("[printer-action] %s could not fold the push", entry.facts.action_id)
    _check_bounds(event.printer_id, event.state)


def on_job_terminal(event: JobTerminal) -> None:
    """A job terminal — live, or synthesised by the downtime reconcile — answers a stop or a start however
    late it lands."""
    _evaluate(event.printer_id, command_outcome.Ended(event.job_id, event.status))


def on_feed_event(event: FeedEventReported) -> None:
    """A feed-state landing reaches the load that commanded it; a landing no command explains is the
    printer's own (``observed_load``)."""
    landed = event.event
    if not isinstance(landed, LoadLanded):
        return
    pid = event.printer_id
    tray = landed.tray
    owner: Actor | None = None
    for entry in list(_outstanding.get(pid, {}).values()):
        if entry.facts.command == "load" and valid_feeder(entry.facts.target) == tray:
            owner = entry.facts.actor
    if owner is not None:
        _evaluate(pid, command_outcome.Landed(tray))
        _landing_owner.pop(pid, None)
    else:
        claimed = _landing_owner.pop(pid, None)
        if claimed is not None and claimed[0] == tray:
            owner = claimed[1]
    if owner is None:
        owner = ACTOR_PRINTER
        _observed_load_at[pid] = _now()
        spawn_background_task(_record_observed_load(pid, tray), name=f"printer-action-observed-p{pid}")
    _last_landing[pid] = (landed.seq, tray, owner)


async def reread_session(printer_id: int, connection_epoch: int) -> None:
    """SESSION START (``session_start``): every outstanding action of ``printer_id`` re-reads the printer at
    its first fresh report of session ``connection_epoch`` — a command unacknowledged when a later session
    began is VOID; an accepted one is answered by what the printer reports now. Then the online clock
    resumes (a lost session's offline stretch closes here — after the re-read, so a command the new session
    voids or answers never accrues a second of the gap), and every :func:`await_next_session` waiter of the
    printer is woken: this IS the event they wait for."""
    state = printer_manager.get_status(printer_id)
    _evaluate(printer_id, command_outcome.SessionBegan(connection_epoch, WireReading.of(state)))
    _session_began(printer_id)
    wake = _session_wakes.pop(printer_id, None)
    if wake is not None:
        wake.set()


def _session_past(state: object, epoch: int | None) -> bool:
    """Is ``state`` a LIVE reading (``live_reading.reads_live``) of a session other than ``epoch``?"""
    return state is not None and getattr(state, "connection_epoch", None) != epoch and live_reading.reads_live(state)


async def await_next_session(printer_id: int, epoch: int | None, *, recheck_s: float = _SESSION_RECHECK_S) -> None:
    """Wait for the printer's NEXT session — the first fresh report of a session past ``epoch`` (the session a
    frame failed to go out on, read off its send's entry snapshot, never after it). The event a command
    that never left the farm waits for before it is sent again: never a retry clock. THE one spelling for
    every lane that re-sends (2026-10-11) — the AMS recovery driver (``spool_recovery._send_until_published``
    and its lever read) and the pause-recovery lane (the plate-check press and stop, the power-loss resume);
    each lane keeps its own policy for what follows (still owed? stood down?).

    Event-driven: the record's own session re-read (:func:`reread_session`, the printer's
    ``FirstFreshReport`` step in ``session_start``) wakes every waiter of the printer. The level is read
    FIRST — a session that already began answers at once — and re-read at the caller's push cadence
    (``recheck_s``) as the backstop for a first fresh report whose re-read never reached the record (a
    session start that failed before its step). Never raises; a caller that must stop waiting cancels."""
    while True:
        if _session_past(printer_manager.get_status(printer_id), epoch):
            return
        wake = _session_wakes.setdefault(printer_id, asyncio.Event())
        try:
            await asyncio.wait_for(wake.wait(), recheck_s)
        except TimeoutError:
            pass


# --- the record's writes ----------------------------------------------------------------------------


async def _write_answer(
    action_id: int, incident_id: int | None, answer: Answer, reason: str | None, *, refusal: Refusal | None = None
) -> None:
    """Persist a final answer — once (``answered_at IS NULL``) — and re-derive the hold's projection.
    ``refusal``: the farm's own reason the frame never went out (a ``refused`` answer at the send).
    Shielded like the insert: an answer is recorded whole even when its caller is cancelled."""
    await asyncio.shield(_persist_answer(action_id, incident_id, answer, reason, refusal))


async def _persist_answer(
    action_id: int, incident_id: int | None, answer: Answer, reason: str | None, refusal: Refusal | None
) -> None:
    from backend.app.core.database import async_session

    try:
        async with async_session() as db:
            await db.execute(
                update(PrinterAction)
                .where(PrinterAction.id == action_id, PrinterAction.answered_at.is_(None))
                .values(
                    answer=answer.value,
                    reason=(reason or None) and reason[:255],
                    refusal=refusal,
                    answered_at=_now(),
                )
            )
            await db.commit()
            if incident_id is not None:
                await printer_incidents.rederive_actions(db, incident_id)
    except Exception:  # noqa: BLE001 — a record write must never break the event chain
        logger.exception("[printer-action] %s answer %s could not be persisted", action_id, answer.value)


async def _write_bound_passed(action_id: int, at: datetime) -> None:
    """Persist WHEN the action's self-heal bound first passed — once (``bound_passed_at IS NULL``), the one
    writer of the column. Shielded like the answer: recorded whole even when its caller is cancelled."""
    await asyncio.shield(_persist_bound_passed(action_id, at))


async def _persist_bound_passed(action_id: int, at: datetime) -> None:
    from backend.app.core.database import async_session

    try:
        async with async_session() as db:
            await db.execute(
                update(PrinterAction)
                .where(PrinterAction.id == action_id, PrinterAction.bound_passed_at.is_(None))
                .values(bound_passed_at=at)
            )
            await db.commit()
    except Exception:  # noqa: BLE001 — a record write must never break the event chain
        logger.exception("[printer-action] %s bound pass could not be persisted", action_id)


async def _write_acked(action_id: int) -> None:
    from backend.app.core.database import async_session

    try:
        async with async_session() as db:
            await db.execute(
                update(PrinterAction)
                .where(PrinterAction.id == action_id, PrinterAction.acked_at.is_(None))
                .values(acked_at=_now())
            )
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("[printer-action] %s ACK could not be persisted", action_id)


async def _write_job(action_id: int, job_id: str | None) -> None:
    from backend.app.core.database import async_session

    try:
        async with async_session() as db:
            await db.execute(update(PrinterAction).where(PrinterAction.id == action_id).values(job_id=job_id))
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("[printer-action] %s job id could not be persisted", action_id)


async def _record_observed_load(printer_id: int, tray: int) -> None:
    """Record a landing nobody commanded: an ``observed_load`` action of the printer, answered at once."""
    from backend.app.core.database import async_session

    try:
        at = _now()
        async with async_session() as db:
            db.add(
                PrinterAction(
                    printer_id=printer_id,
                    actor=ACTOR_PRINTER,
                    command="observed_load",
                    target=tray,
                    sent_at=at,
                    answer=Answer.REACHED.value,
                    answered_at=at,
                )
            )
            await db.commit()
        logger.info(
            "[printer-action] printer=%s observed_load tray=%s (no farm or operator load explains it)", printer_id, tray
        )
    except Exception:  # noqa: BLE001
        logger.exception("[printer-action] observed load on printer %s could not be persisted", printer_id)


# --- rehydrate ---------------------------------------------------------------------------------------


async def rehydrate(db: AsyncSession) -> int:
    """LIFESPAN REHYDRATOR: rebuild the outstanding registry, the operator's last AMS command and the last
    uncommanded landing per printer from the rows. Returns how many actions are outstanding. Decides
    nothing: each outstanding action is re-read on its printer's first fresh report (:func:`reread_session`).
    A passed bound stays passed (``bound_passed_at``); an action with none is offered to its family's bound
    factory at its first evaluation (:func:`register_bound_factory` — the factories are wired after this)."""
    _outstanding.clear()
    rows = (await db.execute(select(PrinterAction).where(PrinterAction.answered_at.is_(None)))).scalars().all()
    for row in rows:
        try:
            spec = command_outcome.spec_of(row.command)
        except LookupError:
            logger.error(
                "[printer-action] %s outstanding with an unknown command %r — left unread", row.id, row.command
            )
            continue
        if not spec.echoes:
            continue  # no echo by design: its NULL answer is no outstanding action (never rehydrated)
        actor: Actor = row.actor if row.actor in (ACTOR_FARM, ACTOR_OPERATOR, ACTOR_PRINTER) else ACTOR_FARM  # type: ignore[assignment]
        _outstanding.setdefault(row.printer_id, {})[row.id] = _Outstanding(
            facts=ActionFacts(
                action_id=row.id,
                printer_id=row.printer_id,
                command=row.command,
                actor=actor,
                target=row.target,
                entry=row.entry,
                job_id=row.job_id,
                prior_job_id=row.prior_job_id,
            ),
            running=Running(acked=True if row.acked_at is not None else None, sent_epoch=None),
            incident_id=row.incident_id,
            sent_at=row.sent_at,
            queue_item_id=row.queue_item_id,
            bound_passed=row.bound_passed_at is not None,
            rehydrated=True,
        )
    _last_operator_motion.clear()
    for printer_id, at in (
        await db.execute(
            select(PrinterAction.printer_id, func.max(PrinterAction.sent_at))
            .where(PrinterAction.actor == ACTOR_OPERATOR, PrinterAction.command.in_(("load", "unload")))
            .group_by(PrinterAction.printer_id)
        )
    ).all():
        _last_operator_motion[printer_id] = at
    _observed_load_at.clear()
    for printer_id, at in (
        await db.execute(
            select(PrinterAction.printer_id, func.max(PrinterAction.sent_at))
            .where(PrinterAction.command == "observed_load")
            .group_by(PrinterAction.printer_id)
        )
    ).all():
        _observed_load_at[printer_id] = at
    count = sum(len(actions) for actions in _outstanding.values())
    if count:
        logger.info("[printer-action] rehydrated %d outstanding action(s)", count)
    return count


# --- the operator's AMS Load / Unload (absorbed from ams_command, 2026-10-11) ------------------------


@dataclass(frozen=True)
class AmsCommandResult:
    """The operator facade's answer: the closed outcome, the sentence for it, and the posture family the
    command was sent into (``None`` for a refusal — nothing was sent, no posture was read)."""

    outcome: AmsCommandOutcome
    message: str
    family: PostureFamily | None


_REFUSAL_OUTCOME: dict[Refusal, AmsCommandOutcome] = {
    "not_connected": "refused_not_connected",
    "not_sent": "refused_not_connected",
    "runout_hold": "refused_runout_hold",
}

_LOADING = "Loading filament from {target}"
_UNLOADING = "Unloading filament"
_LOAD_UNMOVED = "Load sent. AMS did not move."
_UNLOAD_UNMOVED = "Unload sent. AMS did not move."
_RECONNECTED = "Printer reconnected during the command. Check the AMS."
_ANSWER_COPY: dict[tuple[MotionCommand, MotionReading, PostureFamily], str] = {
    ("load", "complete", "mid_change"): _LOADING,
    ("load", "acted", "mid_change"): _LOADING,
    ("load", "no_movement", "mid_change"): _LOAD_UNMOVED,
    ("load", "held", "mid_change"): "Load accepted. Held behind the paused print's filament change.",
    ("load", "session_changed", "mid_change"): _RECONNECTED,
    ("load", "complete", "outside_change"): _LOADING,
    ("load", "acted", "outside_change"): _LOADING,
    ("load", "no_movement", "outside_change"): _LOAD_UNMOVED,
    ("load", "held", "outside_change"): "Load accepted. Not run yet.",
    ("load", "session_changed", "outside_change"): _RECONNECTED,
    ("unload", "complete", "mid_change"): _UNLOADING,
    ("unload", "acted", "mid_change"): _UNLOADING,
    ("unload", "no_movement", "mid_change"): _UNLOAD_UNMOVED,
    ("unload", "held", "mid_change"): "Unload accepted. Held behind the paused print's filament change.",
    ("unload", "undecidable", "mid_change"): "Unload sent. Nothing was loaded.",
    ("unload", "session_changed", "mid_change"): _RECONNECTED,
    ("unload", "complete", "outside_change"): _UNLOADING,
    ("unload", "acted", "outside_change"): _UNLOADING,
    ("unload", "no_movement", "outside_change"): _UNLOAD_UNMOVED,
    (
        "unload",
        "held",
        "outside_change",
    ): "Unload accepted. Not run yet. The AMS can run it later without another command.",
    ("unload", "session_changed", "outside_change"): _RECONNECTED,
}


def _target_label(tray_id: int) -> str:
    if tray_id == TRAY_NOW_EXTERNAL_SPOOL:
        return "external spool"
    if tray_id == TRAY_NOW_NOTHING_FED:
        return "Ext-R"
    return f"AMS {tray_id // 4} slot {tray_id % 4 + 1}"


def _answer_message(command: MotionCommand, tray_id: int | None, answer: MotionReading, family: PostureFamily) -> str:
    template = _ANSWER_COPY[(command, answer, family)]
    return template.format(target=_target_label(tray_id)) if tray_id is not None else template


async def observe_motion(
    sent: Sent, command: MotionCommand, target: int | None, *, timeout_s: float, poll_s: float = 0.25
) -> MotionReading:
    """Poll the live state and return the motion classifier's first reading for the operator's click (the
    button's answer — the ACTION stays outstanding on the record until an event answers it). ``elapsed_s``
    is measured from the send. Each poll folds the ACK before the snapshot."""
    entry = sent.entry
    observation = command_outcome.Observation()
    while True:
        ack = ack_of(sent)
        observation.fold_ack(ack.succeeded if ack is not None else None)
        now = command_outcome.snapshot(printer_manager.get_status(sent.printer_id))
        elapsed_s = now.taken_at - entry.taken_at
        answer = command_outcome.classify(
            command, target, entry, now, observation=observation, elapsed_s=elapsed_s, deadline_s=timeout_s
        )
        if answer is not None:
            command_outcome.log_answer(
                actor="operator",
                printer_id=sent.printer_id,
                command=command,
                target=target,
                entry=entry,
                now=now,
                answer=answer,
                elapsed_s=elapsed_s,
            )
            return answer
        await asyncio.sleep(min(poll_s, max(0.0, timeout_s - elapsed_s)))


async def command_for_operator(printer_id: int, command: MotionCommand, tray_id: int | None = None) -> AmsCommandResult:
    """An operator's Load / Unload click: send it as the operator's action, read the wire for the click's
    window (``command_outcome.OPERATOR_ACK_S``), and answer the button with the measured reading."""
    if command == "load":
        if tray_id is None:
            raise ValueError("printer_action.command_for_operator: a load needs a tray_id")
        sent = await send(printer_id, load(tray_id), actor=ACTOR_OPERATOR)
    elif command == "unload":
        sent = await send(printer_id, unload(), actor=ACTOR_OPERATOR)
    else:
        raise LookupError(f"printer_action.command_for_operator: unknown command {command!r}")
    if not sent.published:
        refusal = sent.refusal or "not_sent"
        return AmsCommandResult(
            outcome=_REFUSAL_OUTCOME[refusal], message=sent.refusal_detail or _REFUSAL_REASON[refusal], family=None
        )
    answer = await observe_motion(sent, command, tray_id, timeout_s=command_outcome.OPERATOR_ACK_S)
    family = command_outcome.posture_family(sent.entry)
    return AmsCommandResult(outcome=answer, message=_answer_message(command, tray_id, answer, family), family=family)
