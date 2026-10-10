"""The durable EQUIPMENT-FAULT store — open, close and read a printer's fault holds.

``spool_recovery`` owns the *machine* (what to do about a fault); this module owns
its *record* (:class:`~backend.app.models.printer_incident.PrinterIncident`) —
creation under the one-open-per-kind rule, the close and UPGRADE transitions, the
queries the watchdogs ask, and the in-memory projection the WebSocket payload reads.

Since 2026-09-12 it also holds the DECLARED kinds — a hold a human opened with a verb
(maintenance mode), which no fault produced and no evidence closes. They ride the same
row, the same per-kind exclusivity index and the same projection cache, and they are
told apart by exactly two things: :func:`open_declared` (the code-less constructor)
and :func:`automation_held` (the one predicate every automatic lane reads). The
equipment-fault LEDGER deliberately excludes them (:func:`summary`).

Why a separate module rather than more of ``spool_recovery``: three unrelated
callers need incident FACTS without wanting the state machine — ``farm_stall``
(hourly reminders + "is this pause owned"), ``printer_manager.printer_state_to_dict``
(the printer-card chip, a SYNC function that may not touch the DB), and main's
lifespan (startup rehydration). Routing those through the machine would drag the
whole recovery import graph — and its printer_manager dependency — into a WS
serializer.

**Exclusivity is the database's job, and it is now PER KIND.**
``ux_printer_incident_open`` is a partial UNIQUE index over
``(printer_id, kind) WHERE resolved_at IS NULL`` and ``ux_printer_incident_open_ams``
a second partial one over ``printer_id`` restricted to the AMS kinds — so a printer
may carry a lost-Z hold BESIDE a jam (an asset carries concurrent alarms), while the
three AMS kinds stay mutually exclusive among themselves because they are three
readings of one AMS. Either index firing means a race, and the loser gets an
IntegrityError which :func:`open_new` reports as "someone else owns it" instead of
crashing. The old exclusivity (a process-lifetime ``_active_tasks`` dict) was
erased by every restart while the standing HMS came straight back.

**The snapshot cache** mirrors EVERY open row per printer so the ~1 Hz WS serializer
never queries. It is a projection, never a source: every write path here refreshes
it, and :func:`rehydrate` rebuilds it from the DB at startup.

Its failure direction INVERTED on 2026-09-11, and that is worth stating plainly.
While it only fed a chip, a stale-empty cache under-reported and could never invent
a hold. It now also GATES DISPATCH (:func:`hold_blocks_dispatch`), so a miss
UN-GATES a printer that is held — which is why every mutator here refreshes it and
:func:`rehydrate` rebuilds it at startup, and why the scheduler reads the WIRE
beside it: the wire owns "a fault stands NOW", this row owns "an unresolved hold
exists", and neither is derivable from the other.

**Liveness lives here too (2026-09-23, 012-H2S shape 41).** Two different facts answer
"is somebody acting on this printer's fault", and this module holds both:

* the open row is the durable PROMISE — ``recovering`` says a driver undertook to produce
  an outcome, and it survives a restart because the obligation does;
* the driver registry (:func:`register_driver` / :func:`release_driver`) is LIVENESS — a
  task is running right now. Process memory by design: a restart kills the task, so an
  empty registry after one is the truth; and ``.done()`` is asked rather than membership
  trusted, so a driver that died without releasing reads not-live (the R1 orphan).

:func:`driver_live` is the ONE spelling of the second fact, and every reader projects it
from here: the printer card through :func:`snapshot` / :func:`snapshots` (computed at READ
time and never cached — the projection is filled at open and at rehydrate, and would go
stale the moment a driver spawned or exited), the resolution closers through
``incident_resolution.Context.driver_live``, and the stall watchdog.
``incident_resolution.driver_owns`` keeps taking ``live`` as a PARAMETER, so the rule
table stays a pure function of what it is handed. For the AMS driver registration is
observability, never a gate — entry exclusivity stays the partial unique index above; the
plate-check episode driver's re-entry gates on it (:func:`register_driver`). The slot used
to live in ``spool_recovery``, and it moved for the reason this module exists at all: a
status serializer must not import the recovery machine to ask whether the machine is running.

**The step ledger is written here as well** (:func:`note_step` / :func:`answer_step`, the
ONE writer of ``printer_incident_step``, called only by :class:`EvidenceLog`): what a
driver SENT against an incident, one row per step. The wire cannot restate it — a stalled
feeder answers every release lever with the same re-PAUSE in the same change, and a plate
check reads PAUSE before and after the farm pressed its re-check — so a restart must read it
here to resume at the next unsent step instead of re-sending what already went out. The ledger
facts a DB-free reader needs ride the open-row projection (:func:`_ledger_projections`): the
AMS driver's restart stop, a plate-check episode's last stop, and the AMS driver's motion command
that has not run (:class:`PendingCommand`, read by :func:`pending_command`), flipped at the send
and the read and re-derived at every rebuild of the projection — never a column.

**The plate-check human's turn is decided here too** (:func:`plate_check_human_turn`, operator
ruling 2026-10-05): a status serializer must offer "Ignore and resume" only while the print waits
paused at the plate check for a person, and it may not import the ladder to ask.
"""

from __future__ import annotations

import logging
import statistics
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Generic, Literal, Protocol, TypeVar

from sqlalchemy import exists, func as sa_func, or_, select
from sqlalchemy.exc import IntegrityError

from backend.app.models.printer import Printer
from backend.app.models.printer_incident import (
    AMS_FAULT_KINDS,
    DECLARED_KINDS,
    JOB_PAUSE_KINDS,
    KIND_JAM,
    KIND_PHYSICAL,
    KIND_PLATE_VISION,
    KIND_POWER_LOSS,
    KIND_PRECEDENCE,
    KIND_RUNOUT,
    KIND_SERVICE_HOLD,
    KIND_TOOLHEAD_REFILL,
    KIND_Z_REFERENCE_LOST,
    RECOVER_ENDS,
    RECURRENCE_KINDS,
    RESOLUTION_WIRE,
    RESOLVE_AUTO_RESUME,
    RESOLVE_DRIVER_RESTART,
    RESOLVE_DRIVER_SELF_HEAL,
    RESOLVE_DRIVER_SWAP,
    RESOLVE_PLATE_REFUSED,
    RESOLVE_RECHECK_PASSED,
    RESOLVE_REFILL_RESUMED,
    RESOLVE_TERMINAL,
    RESOLVES_ON,
    STATUS_ABORTED,
    STATUS_ESCALATED,
    STATUS_RECOVERING,
    STATUS_RESOLVED,
    PrinterIncident,
)
from backend.app.models.printer_incident_step import (
    STEP_KIND_COMMAND,
    STEP_KIND_LEVER,
    STEP_KIND_STOP,
    PrinterIncidentStep,
    StepKind,
)

# ``hms_actions`` is a LEAF (the vendored action catalog), the one spelling of a dialog button.
from backend.app.services.hms_actions import HMSAction

# ``hms_errors`` is a LEAF (it imports only the vendored catalogs), so the one message
# renderer and the plate-check pause predicate are imported at module level — no cycle can
# form through it.
from backend.app.services.hms_errors import (
    PrinterMessage,
    messages_from_full_codes,
    plate_check_paused,
    printer_message_from_short_code,
)

# ``job_identity`` is stdlib-only by construction, so the held-job rule is imported the same way.
from backend.app.services.job_identity import is_held_job

# ``site_time`` is a stdlib-only leaf: the ONE site calendar the ledger read model's window and
# recurrence days are counted on.
from backend.app.utils import site_time

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Iterable, Mapping, Sequence
    from typing import Self

    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# The recovery DRIVER's own close when a release verb it pulled ENDED the print (the
# reader's ``ended`` verdict, 2026-09-23): the terminal closer stands aside while a driver
# is live (``incident_resolution._wire_job_terminal``), so the driver records the terminal
# itself and closes its row with this token. It joins the model's ``RESOLVE_*`` vocabulary
# in the driver capsule; ``resolve_source`` is free text, so the token needs no migration.
# Deliberately NOT in :data:`_FARM_CLOSES`: a print the farm's own verb ended is not a
# recovery, whatever else the close says.
RESOLVE_DRIVER_ENDED = "driver_ended"

# The step-ledger NAME of the recovery driver's last release rung: ``print.stop`` on a job
# that has deposited nothing (operator ruling 2026-09-29). ONE origin for the spelling —
# ``spool_recovery._LEVERS`` keys the rung by it and the open-row projection derives
# :data:`PAYLOAD_FAULT_RESTART_STOP` from a step carrying it — because the terminal
# classifier reads the farm's own stop off that projection, and a second spelling is how the
# verdict would quietly stop being reached. 10 characters — inside the step name's VARCHAR(32).
FAULT_RESTART_STEP = "print_stop"

# The open-row projection key that says "the driver has SENT its restart stop on this row"
# (a step named :data:`FAULT_RESTART_STEP` is on the row's ledger). Derived from the ledger
# by the ledger's one writer, at rehydrate and at every re-projection of the row
# (:func:`_ledger_projections`), never stored on the row: the ledger IS the record of what a
# driver sent.
PAYLOAD_FAULT_RESTART_STOP = "fault_restart_stop"

# The classifier's answer for a motion command the firmware ACKNOWLEDGED and has not run
# (``ams_command.Answer``'s ``held`` — behind the print's own filament change, or, outside one,
# at the step bound: 011-H2S 2026-10-09 04:31:27 and 014-H2S 2026-10-10 02:45:58, both run by
# the AMS on its own ~4.5 min later). Spelled here because this store may not import
# ``ams_command`` (that module imports this one); ``test_printer_incidents`` pins the token into
# the classifier's answer vocabulary, so the two spellings cannot drift.
COMMAND_HELD = "held"

# The dialog buttons a HUMAN may press on the printer's plate-check dialog through the farm, on
# their turn (:func:`plate_check_human_turn`, operator ruling 2026-10-05): the printer's own
# "Problem solved, resume" (it re-checks) and "Ignore and resume" (it skips the check). Ordered
# as the card offers them; plain ``str`` so the projection stays JSON primitives. The ladder
# itself never presses "Ignore" (``pause_recovery``).
PLATE_CHECK_HUMAN_ACTIONS: tuple[str, ...] = (
    str(HMSAction.PROBLEM_SOLVED_RESUME),
    str(HMSAction.IGNORE_RESUME),
)

# The statuses that mean "closed" — both stamp ``resolved_at`` (see the model
# docstring's lifecycle table), so the open/closed question is asked of that column
# and never of this tuple.
CLOSED_STATUSES: tuple[str, ...] = (STATUS_RESOLVED, STATUS_ABORTED)

# ``printer_incident.hms_full_codes`` is VARCHAR(512); :func:`join_full_codes` bounds to
# it at a code boundary.
_HMS_FULL_CODES_MAX = 512

# printer_id -> {incident_id -> the WS/REST projection of that OPEN row}. Rebuilt
# from the DB at startup (:func:`rehydrate`) and maintained by every write below.
# Keyed by incident id rather than by kind because :func:`cached_kind` — how a live
# recovery driver learns its own row was re-classified — must be able to ask about
# ONE row while the kind under it changes.
_open_cache: dict[int, dict[int, dict]] = {}

# printer_id -> the recovery DRIVER task acting on that printer's fault. LIVENESS, not
# ownership (see the module docstring): the open row is the promise, this is whether a
# task is keeping it right now. Written only by :func:`register_driver` /
# :func:`release_driver`, read only through :func:`driver_live`.
_drivers: dict[int, asyncio.Task[object]] = {}


# --- waiting_reason vocabulary (rendered by the queue UI, mapped in waitingReason.ts) ---
#
# The kind -> token table lives HERE, with the store that owns the kinds, because a
# `waiting_reason` on a queue unit is a PROJECTION of an incident row and nothing else
# (model docstring: never a second source of truth). It was `spool_recovery`'s while
# every incident was an AMS fault; three pause-cause kinds later that module is one
# consumer among several, and a table that lives in one consumer is how a kind ends up
# registered for the reminder and not for the projection.
WAITING_REASON_RECOVERING = "spool_jam_recovering"
WAITING_REASON_FAILED = "spool_jam_recovery_failed"
# A filament RUNOUT that escalated (distinct copy from a jam: the fix is to insert
# filament into the SAME slot, not swap). farm_stall treats it as attended so the
# pause-stall watchdog doesn't double-escalate.
WAITING_REASON_RUNOUT = "filament_runout_recovery_failed"
# An EXTERNAL-spool runout. Distinct from the AMS runout because the instruction is
# different IN KIND, not in tone: there is no slot to refill — the roll on the spool
# HOLDER has to be replaced — and the AMS copy ("refill the AMS slot") sends the
# operator to a tray that never fed this print.
WAITING_REASON_EXTERNAL_RUNOUT = "external_spool_runout"
# An EXTERNAL-spool FEED fault (003-H2S 2026-08-11). The jam token is wrong here in
# the same way the AMS runout copy is wrong for an external runout: it promises a
# swap machine that has nothing to swap — no AMS, no sibling tray, no slot. The fix
# is at the holder ("feed filament into the PTFE tube until it can not be pushed any
# farther", which is literally what the firmware is asking).
WAITING_REASON_EXTERNAL_FEED = "external_feed_fault"
# A PHYSICAL filament fault (broken filament, clogged extruder, failed pull-back).
# Distinct from both: there is no swap to attempt and no slot to refill — the copy
# must send the operator to the printer rather than to the spool inventory.
WAITING_REASON_PHYSICAL = "spool_physical_fault"
# The printer is holding at the firmware's power-loss recovery prompt and the farm's
# resume was refused or failed. Nothing on the AMS is wrong; the operator answers the
# prompt on the printer's own screen.
WAITING_REASON_POWER_LOSS = "power_loss_hold"
# The printer's own pre-print vision check says the plate is not empty. This token's
# ONE origin moved here from ``farm_correlation`` when the plate-vision hold became an
# incident kind — the string is unchanged, so every rendered surface and locale key is
# untouched.
WAITING_REASON_PLATE_VISION = "plate_not_empty_printer_detected"
# The printer rebooted with a part on the plate, so its Z datum is fiction and no eject
# may run against it. Cleared by the human who removes the part.
WAITING_REASON_Z_REFERENCE_LOST = "z_reference_lost"
# An operator put this printer in maintenance mode, so every automatic lane stands
# down. Nothing is wrong with the machine — the token exists for vocabulary hygiene
# (``waiting_reason_for`` RAISES on an unregistered kind and ``RECOVERY_WAITING_REASONS``
# is derived from the table below), and because a unit left pending on a held printer
# should say why rather than reading as an unexplained wait.
WAITING_REASON_SERVICE_HOLD = "printer_service_hold"
# The paused job's toolhead reads EMPTY and the farm is refilling it — or could not, and a person
# must load a slot before the resume (K10, 2026-10-10). Distinct from every AMS token: no fault
# is named, and the exit is filament at the toolhead, not a swap, a refill of a demanded slot or
# a repaired path.
WAITING_REASON_TOOLHEAD_EMPTY = "toolhead_empty"

# The tokens an INCIDENT owns. A hold that resolves clears only these — an unrelated
# hold another owner stamped (low filament, a stagger wait) must survive a resume the
# recovery lane happened to observe. ``farm_stall`` derives its ATTENDED-pause set from
# this one, so a token missing here would let the pause-stall watchdog double-escalate a
# hold that already alerted. DERIVED from the table below rather than re-listed: a kind
# registered for the projection is attended by construction, which is the coupling that
# the two-list version kept losing.
RECOVERY_WAITING_REASONS: frozenset[str]

# kind -> the token its hold projects onto the farm queue unit.
_WAITING_REASON_BY_KIND: dict[str, str] = {
    KIND_JAM: WAITING_REASON_FAILED,
    KIND_RUNOUT: WAITING_REASON_RUNOUT,
    KIND_PHYSICAL: WAITING_REASON_PHYSICAL,
    KIND_POWER_LOSS: WAITING_REASON_POWER_LOSS,
    KIND_PLATE_VISION: WAITING_REASON_PLATE_VISION,
    KIND_Z_REFERENCE_LOST: WAITING_REASON_Z_REFERENCE_LOST,
    KIND_SERVICE_HOLD: WAITING_REASON_SERVICE_HOLD,
    KIND_TOOLHEAD_REFILL: WAITING_REASON_TOOLHEAD_EMPTY,
}

# The EXTERNAL overrides of the table above, by kind. ``external`` never changes
# WHAT happened (the kind does that) — it changes WHERE the operator must go, and
# only for the kinds whose AMS copy names a place that does not exist on a spool
# holder. ``physical`` is deliberately absent: "a broken filament / a clog, go to the
# printer" is already the right instruction on either hardware, so it keeps its one
# token rather than minting a synonym. The three pause-cause kinds are absent for a
# stronger reason — they are not AMS faults at all, so there is no holder variant.
_EXTERNAL_WAITING_REASON_BY_KIND: dict[str, str] = {
    KIND_RUNOUT: WAITING_REASON_EXTERNAL_RUNOUT,
    KIND_JAM: WAITING_REASON_EXTERNAL_FEED,
}

RECOVERY_WAITING_REASONS = (
    frozenset(_WAITING_REASON_BY_KIND.values())
    | frozenset(_EXTERNAL_WAITING_REASON_BY_KIND.values())
    | {WAITING_REASON_RECOVERING}
)


def resolution_class(kind: str, *, external: bool = False) -> str:
    """What ENDS a hold of this kind on this hardware: ``wire`` / ``repair`` / ``operator`` / ``declared``.

    The one reading of the model's ``RESOLVES_ON`` table, so the four literals are
    spelled once and every close path reads the same rule. It REPLACED the boolean
    ``resolves_on_operator``, which could only ever answer one of three questions:
    both of its callers were in fact asking "may the WIRE close this?", and with a
    third class that is no longer the complement of "does a human close this?".

    ``external`` is the HARDWARE the fault sits on (:func:`row_external` derives it
    from a row), because the same class returns to normal differently on the two: an
    AMS physical fault is repaired by hands and the wire never says so, while an
    external-holder one is a firmware PROMPT whose clearing IS the human's answer.
    An unregistered ``(kind, True)`` falls back to that kind's own rule rather than
    raising — the pause-cause kinds have no holder variant at all, so asking for one
    is a caller being uniform, not a caller being wrong.

    An unregistered KIND answers ``wire`` — the pre-existing safe direction: a hold
    that closes too readily is visible, one that never closes blocks the printer
    forever. A ``declared`` hold is the one class that reverses that preference, which
    is precisely why it is registered rather than defaulted: it must NOT close on its
    own, because the printer it holds may have a human's hands inside it.
    """
    resolution = RESOLVES_ON.get((kind, external))
    if resolution is not None:
        return resolution
    return RESOLVES_ON.get((kind, False), RESOLUTION_WIRE)


def closed_by_recover(kind: str, *, external: bool = False) -> bool:
    """Does the operator's **Recover** verb end a hold of this kind on this hardware?

    A READ of the model's per-class ``RECOVER_ENDS`` attribute over the class
    :func:`resolution_class` resolves — never a second hand-written reading of the rule
    table. The attribute sits beside ``RESOLVES_ON`` with each class's reason, and a test
    pins it cell-for-cell against ``incident_resolution``'s own ``plate_cleared``-with-
    Recover verdict for every registered ``(kind, external)``: this store may not import
    the rule table, so the pin is what keeps "can Recover end it" one answer.

    It exists because the printer card had to derive the affordance from the CLASS to
    know whether Recover applies, and the class vocabulary must not reach the wire:
    ``_payload`` projects this boolean as ``operator_exits`` instead (011-H2S
    2026-09-17 — a physical hold on an idle printer offered no Recover at all, because
    the card gated it on an occupancy claim the printer did not have).
    """
    return RECOVER_ENDS[resolution_class(kind, external=external)]


def runout_slot_desc(global_tray: int | None) -> str | None:
    """Human slot name for a regular AMS global tray ("AMS A slot 1").

    Letter = ``A + g//4``, slot = ``g%4 + 1``. ``None`` for AMS-HT / external /
    unresolved trays — they have no clean letter+slot mapping, and rendering a wrong
    place is worse than rendering none.

    It lives HERE, with the store that owns the kinds, because it is the vocabulary of
    an incident's location and every consumer of it is an incident reader: the chip
    (:func:`slot_desc`), ``spool_recovery``'s escalation + guidance refresh, and
    ``farm_stall``'s hourly reminder. It used to live in ``spool_recovery`` and be
    call-time-imported from here, which made the edge bidirectional for one pure
    three-liner — one origin for the wording, one direction for the import.
    """
    if global_tray is None or not (0 <= global_tray <= 127):
        return None
    return f"AMS {chr(ord('A') + global_tray // 4)} slot {global_tray % 4 + 1}"


class OutcomeFacts(Protocol):
    """The stored facts :func:`outcome_of`, :func:`held_seconds` and :func:`summary` read.

    A :class:`PrinterIncident` satisfies it, and so does a light column-select ``Row``
    (:func:`ledger_page`'s window read), so the ledger's one derivation runs over either
    without a second spelling of it. Read-only properties, because a ``Row`` is.
    """

    @property
    def kind(self) -> str: ...
    @property
    def status(self) -> str: ...
    @property
    def created_at(self) -> datetime | None: ...
    @property
    def escalated_at(self) -> datetime | None: ...
    @property
    def resolved_at(self) -> datetime | None: ...
    @property
    def resolve_source(self) -> str | None: ...


class LedgerFacts(OutcomeFacts, Protocol):
    """:class:`OutcomeFacts` plus the identity columns the ledger read model groups on:
    :func:`row_external` reads ``code``, :func:`recurring_signatures` the printer and id."""

    @property
    def id(self) -> int: ...
    @property
    def printer_id(self) -> int: ...
    @property
    def code(self) -> str: ...


def row_external(incident: LedgerFacts) -> bool:
    """Is this row's fault on the EXTERNAL spool holder?

    ONE derivation, read from the taxonomy's own verdict over the row's durable
    ``code`` (doctrine invariant 1: the classifier decides what hardware a code
    names, never a second test at a call site). :func:`_slot_desc` calls it, and so
    does every consumer that has to pick a ``RESOLVES_ON`` row.
    """
    # Function-level import: spool_recovery imports THIS module at module level.
    from backend.app.services.hms_errors import classify_short_code

    verdict = classify_short_code(incident.code or "")
    return verdict is not None and verdict.external


def waiting_reason_for(kind: str, *, external: bool = False) -> str:
    """The hold token an incident of ``kind`` projects onto a farm queue unit.

    ``external`` splits the kinds whose OPERATOR INSTRUCTION differs from their kind
    on the spool holder. An external-spool runout is a ``runout`` incident in every
    other respect (same hold, same guidance lane, same dual-evidence auto-resume) but
    has no AMS slot to send anyone to; an external FEED fault is a ``jam`` incident
    that no swap machine will ever touch (003-H2S 2026-08-11 — the jam token promised
    exactly the swap the incident could not perform). A ``physical`` fault reads the
    same on both, so it keeps one token; the pause-cause kinds have no holder variant
    at all, so ``external`` is simply irrelevant to them and defaults False.

    An UNKNOWN kind RAISES. It used to fall back to the spool-jam token, which is a
    vocabulary trap rather than a safe default: a newly registered kind would silently
    project "jam recovery failed" onto a unit held for something else, and the wrong
    copy is worse than a loud failure at the one call site that forgot to register.
    """
    if kind not in _WAITING_REASON_BY_KIND:
        raise KeyError(f"no waiting_reason registered for incident kind {kind!r}")
    if external:
        return _EXTERNAL_WAITING_REASON_BY_KIND.get(kind, _WAITING_REASON_BY_KIND[kind])
    return _WAITING_REASON_BY_KIND[kind]


async def clear_hold_projection(db: AsyncSession, item_id: int | None) -> bool:
    """Drop an incident's ``waiting_reason`` projection from a farm unit. True when it cleared one.

    Only the tokens an incident owns (:data:`RECOVERY_WAITING_REASONS`): a unit waiting for
    something else — a filament deficit, a stagger — keeps its own reason, because an
    incident closing says nothing about those. ``None`` (a foreign print's hold) clears
    nothing. Commits.

    The ONE clear, beside the table it reads: every lane that closes an incident whose hold
    projected onto a unit — ``spool_recovery``'s closers and the plate-check episode driver
    (``pause_recovery``) — clears through here. It moved from ``spool_recovery`` on
    2026-09-29 when the episode driver became its second caller.
    """
    if item_id is None:
        return False
    from backend.app.models.print_queue import PrintQueueItem

    item = await db.get(PrintQueueItem, item_id)
    if item is None or item.waiting_reason not in RECOVERY_WAITING_REASONS:
        return False
    item.waiting_reason = None
    await db.commit()
    return True


def _reset_state() -> None:
    """Test hook: drop the projection cache AND the driver liveness slots between cases.

    One hook for the store's whole process state, so no test can reset half of it.
    :func:`rehydrate` deliberately clears only the cache: it re-reads ROWS, and a restart
    has already emptied the registry by killing the tasks it held.
    """
    _open_cache.clear()
    _drivers.clear()


def is_known_kind(kind: str) -> bool:
    """Is ``kind`` a registered incident kind? (The projection table is the registry.)"""
    return kind in _WAITING_REASON_BY_KIND


def slot_desc(incident: PrinterIncident) -> str | None:
    """Human slot name for the incident's fault, or ``None`` when it names none.

    ``"external"`` for ANY fault on the external spool holder: those name no AMS slot
    by nature, and rendering "unknown" for a fault whose location IS known — the
    spool holder — would read as a farm failure to attribute rather than the fact it
    is.

    Externality is read from the taxonomy's own ``external`` verdict over the
    incident's durable ``code`` (doctrine invariant 1: one origin — the classifier
    decides what hardware a code names, never a second test here). It is deliberately
    NOT the ``runout_external`` CLASS: since 2026-08-11 the holder speaks in every
    class it can — a runout, a feed fault (``07FF_8006``) and a physical fault
    (``07FF_8003``) — and a class test rendered the chip for the first of those only,
    leaving an external feed fault looking like a jam whose tray the farm had failed
    to identify. That is precisely the misreading the 003-H2S incident acted on.
    """
    if incident.slot_global_tray is not None:
        # :func:`runout_slot_desc` above is the one origin for the wording — the
        # escalation, the reminder and this chip must never disagree.
        return runout_slot_desc(incident.slot_global_tray)
    return "external" if row_external(incident) else None


def command_pends(outcome: str | None) -> bool:
    """THE rule of a pending motion command, over one command step's recorded ``outcome``: it
    pends while nobody read an answer (``None`` — sent, never read: a crash between the send and
    the read, or a read still in progress) and while the classifier's answer is
    :data:`COMMAND_HELD` (acknowledged, not run). Every other answer settled it — it ran
    (``complete``), moved and stopped (``acted``), never moved or was never acknowledged
    (``no_movement``), cannot be answered (``undecidable``), or its answer was lost with its MQTT
    session (``session_changed`` — also how a firmware reboot voids one, ``spool_recovery``).

    ONE rule for both readings of the ledger: the open-row projection's (:func:`_ledger_projections`,
    over the table) and the recovery driver's evidence log (over its in-memory steps)."""
    return outcome is None or outcome == COMMAND_HELD


@dataclass(frozen=True, slots=True)
class PendingCommand:
    """A motion command the farm SENT on a row that has not run as far as anybody measured — the
    row's LAST command step, while :func:`command_pends` (K11, 011/014-H2S 2026-10-09/10).

    It is the farm's command until it runs, even after a hand-over: the AMS ran an accepted
    pull-back ON ITS OWN ~4.5 min after the send, with the print still paused (011-H2S 04:31:27 →
    04:36:01, 014-H2S 02:45:58 → 02:50:38), emptying the toolhead under a page that said "slot N is
    loaded". The step's own ledger columns: ``seq`` (the step to answer once it settles), ``name``
    (``load`` / ``unload``), ``target`` (a load's tray), ``feeder`` (the feeder kind read AT the
    send — what ``ams_command.ran`` reads back), ``sent_at`` (naive UTC). ``incident_id`` names the
    row, because the printer-scoped reader (:func:`pending_command`) answers about one row.
    """

    incident_id: int
    seq: int
    name: str
    target: int | None
    feeder: str | None
    sent_at: datetime | None

    def as_payload(self) -> dict:
        """The projection's form — JSON primitives only (the WS lane dumps it bare)."""
        return {
            "incident_id": self.incident_id,
            "seq": self.seq,
            "name": self.name,
            "target": self.target,
            "feeder": self.feeder,
            "sent_at": self.sent_at.isoformat() if self.sent_at is not None else None,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> PendingCommand:
        """The value back from :meth:`as_payload`'s form."""
        sent_at = payload.get("sent_at")
        target = payload.get("target")
        feeder = payload.get("feeder")
        return cls(
            incident_id=int(payload["incident_id"]),  # type: ignore[call-overload]
            seq=int(payload["seq"]),  # type: ignore[call-overload]
            name=str(payload["name"]),
            target=target if isinstance(target, int) else None,
            feeder=feeder if isinstance(feeder, str) else None,
            sent_at=datetime.fromisoformat(sent_at) if isinstance(sent_at, str) else None,
        )


# The classifier's answers for a motion command that went out and did NOT reach its end — moved
# and stopped (``acted``) or never moved (``no_movement``): what the "Toolhead empty" chip's
# ``failed`` phase reads off a row's LAST command (``ams_command.Answer``'s tokens, spelled here
# because this store may not import ``ams_command``; ``test_printer_incidents`` pins them into the
# classifier's vocabulary). ``session_changed`` is not one: its answer was LOST (a reconnect, or a
# reboot voiding a queued command), which says nothing about the toolhead.
COMMAND_FAILED_ANSWERS: frozenset[str] = frozenset({"acted", "no_movement"})


@dataclass(frozen=True, slots=True)
class FailedCommand:
    """A row's LAST motion command step when the classifier answered it without the command reaching
    its end (:data:`COMMAND_FAILED_ANSWERS`) — the refill's (or the swap's) load that did not reach the
    toolhead, or the unload before it. The step's own ledger columns; a projection of the ledger,
    re-derived at every send and read and at :func:`rehydrate`, never a column."""

    incident_id: int
    seq: int
    name: str
    target: int | None
    answer: str

    def as_payload(self) -> dict:
        """The projection's form — JSON primitives only (the WS lane dumps it bare)."""
        return {
            "incident_id": self.incident_id,
            "seq": self.seq,
            "name": self.name,
            "target": self.target,
            "answer": self.answer,
        }


@dataclass(frozen=True, slots=True)
class _LedgerProjection:
    """The step-ledger facts the open-row projection carries for DB-free readers.

    ``fault_restart_stop`` — the AMS recovery driver has SENT its restart stop on the row (a
    lever step named :data:`FAULT_RESTART_STEP`), read by the terminal classifier.

    ``last_stop`` — a plate-check episode's LAST farm stop step as ``{"name", "outcome"}`` (the
    outcome ``None`` while unanswered), or ``None`` when the farm sent none. Read by the episode's
    sampler (an unanswered decision-hold stop re-enters; a standing hold is not the sampler's) and
    by the deadline timer (a hold with a stop on it is no longer waiting for a decision). The name
    is the ledger's as written: the ladder's own hydration reads a pre-2026-10-05 ``stop`` as the
    retry rung's (``pause_recovery``), and no DB-free reader branches on it.

    ``pending_command`` — the row's last motion command step while it pends
    (:class:`PendingCommand`, :func:`command_pends`), else ``None``. Read DB-free through
    :func:`pending_command`: by the closers' ``incident_resolution.Context.command_pending`` (every
    cell but the job terminal stands while it holds), by the recovery driver's stand-aside (a row
    whose command still pends stays open), and by the per-push detector that refills the toolhead
    when the farm's command runs after the hand-over.

    ``failed_command`` — the row's last motion command step when it did not reach its end
    (:class:`FailedCommand`), else ``None``. Read DB-free through :func:`refill_state`, the "Toolhead
    empty" chip's ``failed`` phase (K10/C3c, 2026-10-10).
    """

    fault_restart_stop: bool = False
    last_stop: dict | None = None
    pending_command: PendingCommand | None = None
    failed_command: FailedCommand | None = None


def _payload(
    incident: PrinterIncident,
    *,
    ledger: _LedgerProjection | None = None,
    deadline_at: str | None = None,
) -> dict:
    """The projection the printer card renders.

    ``id`` rides along so a reader can ask about ONE row rather than about whatever
    is open now (see :func:`cached_kind`); the UI ignores it.

    ``operator_exits`` is :func:`closed_by_recover` — "would Recover end this hold" —
    and it is deliberately the BOOLEAN rather than the class: the card needs the
    affordance, not the vocabulary, and a UI that branched on ``"repair"`` would own a
    copy of the rule table. It is what lets the printer card offer Recover on a hold
    that raised no plate gate and no quarantine (011-H2S 2026-09-17, and the
    ``z_reference_lost`` hold before it).

    ``driver_live`` is deliberately ABSENT: it is liveness, not a fact of the row, and a
    cached copy would go stale the moment a driver spawned or exited. :func:`snapshot`
    adds it at read time (:func:`_with_liveness`).

    ``printer_messages`` is ALWAYS present (an empty list for a code-less hold): the
    printer's own words for this hold, rendered once (:func:`printer_messages_of`) and
    cached with the row — they are a fact recorded at open, so the write-time cache can
    never go stale on them. ``job_id`` rides along for the terminal classifier
    (``farm_correlation.classify_stop``), which asks "is this terminal the job this hold
    paused?" of this DB-free projection; the card ignores it.

    The STEP-LEDGER facts (:class:`_LedgerProjection`) ride along for the DB-free readers:
    :data:`PAYLOAD_FAULT_RESTART_STOP` (True once the recovery driver has SENT its restart
    stop on this row, so the terminal of that stop classifies as the farm's restart rather
    than the cancel echo H2S sends for any remote stop — H2C sends none), ``last_stop`` (a
    plate-check episode's last farm stop) and ``pending_command`` (the farm's motion command
    that has not run, :meth:`PendingCommand.as_payload`, read back through
    :func:`pending_command`). Projections of the ledger, never columns: the
    caller passes what the ledger says (:func:`_ledger_projections`; :func:`note_step` /
    :func:`answer_step` re-derive at the send and the read), and a row nothing has derived
    them for reads the defaults.

    ``escalated_at`` — when the row became a human's (ISO, naive UTC like ``created_at``), the
    start of a plate-check decision hold's window. ``deadline_at`` — when that window ends and
    the farm stops the print (ISO, naive UTC), written only by the hold's deadline timer
    (:func:`note_deadline`); ``None`` while no timer has computed it.

    Only JSON PRIMITIVES: the WS lane serializes this dict with a bare ``json.dumps``.
    """
    ledger = ledger if ledger is not None else _LedgerProjection()
    return {
        "id": incident.id,
        "kind": incident.kind,
        "status": incident.status,
        "job_id": incident.job_id or "",
        "slot_desc": slot_desc(incident),
        "created_at": incident.created_at.isoformat() if incident.created_at else None,
        "escalated_at": incident.escalated_at.isoformat() if incident.escalated_at else None,
        "operator_exits": closed_by_recover(incident.kind, external=row_external(incident)),
        "printer_messages": [message.as_payload() for message in printer_messages_of(incident)],
        PAYLOAD_FAULT_RESTART_STOP: ledger.fault_restart_stop,
        "last_stop": dict(ledger.last_stop) if ledger.last_stop is not None else None,
        "pending_command": ledger.pending_command.as_payload() if ledger.pending_command is not None else None,
        "failed_command": ledger.failed_command.as_payload() if ledger.failed_command is not None else None,
        "deadline_at": deadline_at,
    }


async def _ledger_projections(db: AsyncSession, incident_ids: Iterable[int]) -> dict[int, _LedgerProjection]:
    """THE derivation of the projection's ledger facts from the table, for every projection that
    is rebuilt from a row (:func:`rehydrate`, the re-projections after an escalation or an
    upgrade) and for the one writer's own re-derivation at the send and the read. ONE query over
    the rows' restart-stop levers, stop steps and motion command steps, in send order; a row with
    none reads the defaults. A command step's place is the LAST one's: only it can still pend
    (:func:`command_pends`) — the driver never sends behind a pending command. The ledger only
    ever grows, so a fact the send set is never lost by a rebuild."""
    ids = list(incident_ids)
    if not ids:
        return {}
    result = await db.execute(
        select(
            PrinterIncidentStep.incident_id,
            PrinterIncidentStep.kind,
            PrinterIncidentStep.name,
            PrinterIncidentStep.outcome,
            PrinterIncidentStep.seq,
            PrinterIncidentStep.target,
            PrinterIncidentStep.feeder,
            PrinterIncidentStep.sent_at,
        )
        .where(PrinterIncidentStep.incident_id.in_(ids))
        .where(
            or_(
                (PrinterIncidentStep.kind == STEP_KIND_LEVER) & (PrinterIncidentStep.name == FAULT_RESTART_STEP),
                PrinterIncidentStep.kind == STEP_KIND_STOP,
                PrinterIncidentStep.kind == STEP_KIND_COMMAND,
            )
        )
        .order_by(PrinterIncidentStep.incident_id, PrinterIncidentStep.seq)
    )
    restart: set[int] = set()
    last_stop: dict[int, dict] = {}
    last_command: dict[int, PendingCommand | None] = {}
    last_failed: dict[int, FailedCommand | None] = {}
    for incident_id, kind, name, outcome, seq, target, feeder, sent_at in result.all():
        if kind == STEP_KIND_LEVER:
            restart.add(incident_id)
        elif kind == STEP_KIND_STOP:
            last_stop[incident_id] = {"name": name, "outcome": outcome}
        else:
            last_command[incident_id] = (
                PendingCommand(
                    incident_id=incident_id, seq=seq, name=name, target=target, feeder=feeder, sent_at=sent_at
                )
                if command_pends(outcome)
                else None
            )
            last_failed[incident_id] = (
                FailedCommand(incident_id=incident_id, seq=seq, name=name, target=target, answer=outcome)
                if outcome in COMMAND_FAILED_ANSWERS
                else None
            )
    return {
        incident_id: _LedgerProjection(
            fault_restart_stop=incident_id in restart,
            last_stop=last_stop.get(incident_id),
            pending_command=last_command.get(incident_id),
            failed_command=last_failed.get(incident_id),
        )
        for incident_id in restart | set(last_stop) | set(last_command)
    }


async def _reproject(db: AsyncSession, incident: PrinterIncident) -> None:
    """Rebuild ONE open row's cached projection after a write to the row, its ledger facts
    derived from the ledger (:func:`_ledger_projections`). The deadline is the hold timer's
    note, not a fact of the row, so the cached one is carried over."""
    ledger = (await _ledger_projections(db, [incident.id])).get(incident.id)
    rows = _open_cache.setdefault(incident.printer_id, {})
    deadline_at = rows.get(incident.id, {}).get("deadline_at")
    rows[incident.id] = _payload(incident, ledger=ledger, deadline_at=deadline_at)


async def _rederive_ledger(db: AsyncSession, incident_id: int) -> None:
    """Re-derive ONE cached row's ledger facts after a step write — the projection follows the
    ledger at the send and at the read. A row not in the cache (closed) is left alone."""
    for rows in _open_cache.values():
        if incident_id in rows:
            ledger = (await _ledger_projections(db, [incident_id])).get(incident_id, _LedgerProjection())
            rows[incident_id] = {
                **rows[incident_id],
                PAYLOAD_FAULT_RESTART_STOP: ledger.fault_restart_stop,
                "last_stop": dict(ledger.last_stop) if ledger.last_stop is not None else None,
                "pending_command": (
                    ledger.pending_command.as_payload() if ledger.pending_command is not None else None
                ),
                "failed_command": (ledger.failed_command.as_payload() if ledger.failed_command is not None else None),
            }
            return


def note_deadline(printer_id: int, incident_id: int, at: datetime | None) -> None:
    """Project a plate-check decision hold's DEADLINE onto its open row (``deadline_at``), or
    clear it (``None``). Projection ONLY — no column: the deadline is ``escalated_at`` plus the
    setting, re-derived by the timer at every arm, so storing it would be a second copy that a
    changed setting could contradict. ONE writer: the hold's deadline timer
    (``pause_recovery._decision_deadline``). A row not open in the cache is left alone."""
    rows = _open_cache.get(printer_id)
    if not rows or incident_id not in rows:
        return
    rows[incident_id] = {**rows[incident_id], "deadline_at": at.isoformat() if at is not None else None}


def printer_messages_of(incident: PrinterIncident) -> tuple[PrinterMessage, ...]:
    """The printer's words for this hold, through the one catalog renderer.

    The recorded full codes first (``hms_full_codes`` — the firmware's own identifiers,
    full-code catalog lookup, short-code fallback). A row recorded before that column
    existed falls back to its representative short ``code``; a code-less hold (a
    declared hold, a lost Z frame) has none. De-duplicated by short code, in record
    order, so two full codes naming one short code read once.
    """
    stored = [code for code in (incident.hms_full_codes or "").split(",") if code.strip()]
    return messages_from_full_codes(stored, fallback_short_codes=[incident.code] if incident.code else [])


def join_full_codes(full_codes) -> str | None:
    """The ``hms_full_codes`` column value for a set of firmware full codes, or None.

    Comma-joined in the order given, bounded to the column's 512 characters at a CODE
    boundary (a truncated hex code would render as garbage); None for no codes, so a
    code-less opener stores the honest NULL rather than an empty string.
    """
    joined = ""
    for code in full_codes or ():
        code = str(code).strip().upper()
        if not code:
            continue
        candidate = f"{joined},{code}" if joined else code
        if len(candidate) > _HMS_FULL_CODES_MAX:
            break
        joined = candidate
    return joined or None


def _precedence(kind: str | None) -> int:
    """Where ``kind`` sits in :data:`KIND_PRECEDENCE`; unregistered kinds sort last."""
    try:
        return KIND_PRECEDENCE.index(kind or "")
    except ValueError:
        return len(KIND_PRECEDENCE)


def snapshot(printer_id: int | None, *, kind: str | None = None) -> dict | None:
    """One of the printer's OPEN rows as a wire dict, or ``None``.

    With ``kind``, that kind's row — the question every consumer that cares about a
    SPECIFIC hold asks (``eject.remote.z_reference_evidence``, the plate-vision
    readers), and the one they could not ask while a printer had a single row.
    Without it, the highest-:data:`KIND_PRECEDENCE` open row: the printer card shows
    ONE chip, and it should name the fault that interrupted the work.

    The dict carries the cached row payload plus ``driver_live``, read LIVE from the
    registry (:func:`_with_liveness`) — a fresh dict, so no reader can write into the cache.

    Pure and DB-free — read by ``printer_state_to_dict`` on every status broadcast.
    """
    if not printer_id:
        return None
    rows = _open_cache.get(printer_id)
    if not rows:
        return None
    if kind is not None:
        payload = next((payload for payload in rows.values() if payload.get("kind") == kind), None)
    else:
        payload = min(rows.values(), key=lambda payload: _precedence(payload.get("kind")))
    return None if payload is None else _with_liveness(printer_id, payload)


def snapshots(printer_id: int | None) -> list[dict]:
    """EVERY open row of the printer as wire dicts, highest precedence first.

    The diagnostic-line reader (``print_scheduler._incident_summary``): a printer
    holding a jam AND a lost-Z frame must name both, or the line that is supposed to
    explain a refusal explains half of it. Each dict carries ``driver_live`` exactly as
    :func:`snapshot`'s does. Pure, DB-free, sync.
    """
    if not printer_id:
        return []
    rows = _open_cache.get(printer_id)
    if not rows:
        return []
    ordered = sorted(rows.values(), key=lambda payload: _precedence(payload.get("kind")))
    return [_with_liveness(printer_id, payload) for payload in ordered]


def _with_liveness(printer_id: int, payload: dict) -> dict:
    """``payload`` plus the printer's LIVE ``driver_live`` — a new dict; the cache is untouched.

    Computed per read because the cache is filled at open and at :func:`rehydrate` and
    knows nothing of tasks. It is the PRINTER's liveness, projected onto every row of it:
    the registry holds one slot per printer, and the question the card asks with it —
    would a manual AMS command end a recovery that is running — is about the printer's
    AMS, not about one row.
    """
    return {**payload, "driver_live": driver_live(printer_id)}


# Why the plate-check human's turn does NOT hold — :func:`plate_check_turn_refusal`.
PlateCheckTurnRefusal = Literal["no_hold", "farm_acting", "not_paused", "other_job"]


def _plate_check_turn(printer_id: int, state) -> tuple[PlateCheckTurnRefusal | None, dict | None]:
    """THE rule of the human's turn, read once: ``(refusal, row)`` — the refusal ``None`` and the
    plate-check row's projection when the turn holds. Every check reads a fact the store or the
    wire restates for free, in order:

    * the printer carries an open ``plate_vision`` row (:func:`snapshot` by kind, so an AMS or
      power-loss row ranked above it never hides it) — else ``no_hold``;
    * the row is ESCALATED and no driver is live on the printer — else ``farm_acting``: a
      ``recovering`` episode is the ladder's to answer, and a live driver (the decision
      deadline's stop is out) owns the outcome; a human press would race it;
    * the printer is PAUSEd at its plate check right now (``hms_errors.plate_check_paused``) —
      else ``not_paused``;
    * on the job the row paused (``job_identity.is_held_job`` over the live ``subtask_id``) —
      else ``other_job``.
    """
    row = snapshot(printer_id, kind=KIND_PLATE_VISION)
    if row is None:
        return "no_hold", None
    if row.get("status") != STATUS_ESCALATED or row.get("driver_live"):
        return "farm_acting", row
    if not plate_check_paused(state):
        return "not_paused", row
    live_job = (getattr(state, "subtask_id", None) or "").strip()
    if not is_held_job(live_job, str(row.get("job_id") or "")):
        return "other_job", row
    return None, row


def plate_check_turn_refusal(printer_id: int, state) -> PlateCheckTurnRefusal | None:
    """Why the human's turn at the plate-check dialog does NOT hold (:func:`_plate_check_turn`),
    or ``None`` when it does. Pure, DB-free, sync. The refusal reading of the ONE rule
    :func:`plate_check_human_turn` projects, for the press that is refused
    (``pause_recovery.human_dialog_action``)."""
    refusal, _row = _plate_check_turn(printer_id, state)
    return refusal


def plate_check_human_turn(printer_id: int, state) -> dict | None:
    """Is it a HUMAN's turn at the printer's plate-check dialog? The dict the card and the HMS
    modal offer the buttons from, or ``None``. Pure, DB-free, sync.

    Operator ruling 2026-10-05: when the ladder's stop would be an escalation, the farm leaves the
    print PAUSED at the dialog and a person decides — Ignore and resume, Problem solved, or Stop —
    until the decision window runs out and the farm stops it. The turn holds while the printer's
    plate-check row is ESCALATED, no driver is live, and the printer is PAUSEd at its plate check
    on the row's own job (:func:`_plate_check_turn`): the decision hold, and the FALLBACK hold
    whose stop the farm could not land. ``recovering`` (the ladder still acting) and a live driver
    (the deadline's stop in flight) are the farm's — a press then would race it.

    ``{"print_error": "%08X" of the live dialog, "job_id": the row's job, "actions":
    PLATE_CHECK_HUMAN_ACTIONS, "deadline_at": the hold timer's ISO deadline or None}`` — JSON
    primitives (the WS frame dumps it bare). EXACTLY two callers, both pinned
    (``test_code_quality.TestPlateCheckDialogOwnership``): the status projection
    (``printer_manager.plate_check_exit_payload``) and the press
    (``pause_recovery.human_dialog_action``). Beside :func:`_with_liveness` because a status
    serializer may not import the ladder to ask.
    """
    refusal, row = _plate_check_turn(printer_id, state)
    if refusal is not None or row is None:
        return None
    return {
        "print_error": f"{int(state.print_error):08X}",
        "job_id": str(row.get("job_id") or ""),
        "actions": list(PLATE_CHECK_HUMAN_ACTIONS),
        "deadline_at": row.get("deadline_at"),
    }


def open_kinds(printer_id: int | None) -> frozenset[str]:
    """The kinds this printer currently holds open. Pure, DB-free, sync."""
    if not printer_id:
        return frozenset()
    return frozenset(payload["kind"] for payload in _open_cache.get(printer_id, {}).values() if payload.get("kind"))


def hold_blocks_dispatch(printer_id: int | None) -> bool:
    """Does this printer carry an unresolved hold? Pure, DB-free, sync.

    THE one origin of that question, on the model of
    ``eject.remote.z_reference_evidence``: the scheduler reads it beside the WIRE
    gate, and the two are a union of two facts with one owner each — the wire owns
    "a fault stands right now", this row owns "an unresolved hold exists". Neither
    can answer the other's question: on 003-H2S the firmware wiped the HMS list at
    every terminal, so the wire read clean while filament was still physically stuck
    in the shared PTFE path, and the next unit dispatched into it. Three times.

    EVERY open kind blocks, deliberately. A ``plate_vision`` row is a job PAUSED at the
    printer's plate check — no plate gate stands while it does (the gate rises only if
    the job is then stopped), so this is what keeps work off the printer; a
    ``z_reference_lost`` row is already plate-gated, so this only makes the refusal
    legible; a ``power_loss`` row means the firmware's prompt is still unanswered,
    which is not a printer to put work on.
    """
    if not printer_id:
        return False
    return bool(_open_cache.get(printer_id))


def automation_held(printer_id: int | None) -> bool:
    """Has a human DECLARED this printer out of every automatic lane? Pure, DB-free, sync.

    THE one predicate the automation lanes read — the :func:`snapshot` /
    :func:`hold_blocks_dispatch` idiom, for the same reason: it is asked from ~1 Hz
    poll loops and from synchronous decision points (the plate-policy driver, the
    notification fan-out, the hourly nag) that may not touch the DB.

    It is deliberately NOT :func:`hold_blocks_dispatch`. That question is "may work go
    onto this printer", which EVERY open hold answers no to; this one is "is this
    printer's automation standing down because somebody said so" — a jam blocks
    dispatch while the farm keeps trying to recover it, whereas a declared hold means
    hands are in the machine and nothing automatic may act at all.

    Membership in :data:`DECLARED_KINDS` is the whole rule, so a second declared kind
    joins every lane by registering in the model rather than by editing them.
    """
    return bool(open_kinds(printer_id) & DECLARED_KINDS)


def job_pause_held(printer_id: int | None) -> bool:
    """Is this printer's job PAUSED at a plate check that only its OWN answerers may answer? Pure, DB-free, sync.

    THE one predicate every lane that RESUMES a job reads — the :func:`automation_held`
    idiom, and for the same reason (it is asked from the ~1 Hz sampler's drivers and from
    the AMS entry gate, neither of which may wait on the DB). True while an open row's
    class is ``job_pause`` — today the printer's own pre-print plate check
    (``plate_vision``): the printer said the plate is wrong and paused the job. Its
    answerers are named (2026-09-29): the farm's plate-check episode driver
    (``pause_recovery`` — the printer's own "Problem solved, resume", which RE-CHECKS the
    plate, then a stop) and a human (fix the plate and resume, or stop). A power-loss
    resume, an AMS release lever or a refill auto-resume answering that pause instead
    would restart the print onto the plate the printer just refused with no re-check at
    all (2026-09-24, 003-H2S) — so every OTHER resume lane stands aside on this.

    It is deliberately NOT :func:`automation_held`: a job pause stands the farm's
    RESUMES down and nothing else — the printer still takes its HMS pages, its reminders
    and its terminal. Membership in the model's derived ``JOB_PAUSE_KINDS`` is the whole
    rule, so a second job-pause kind joins every lane by registering its class.
    """
    return bool(open_kinds(printer_id) & JOB_PAUSE_KINDS)


def refill_in_progress(printer_id: int | None) -> bool:
    """Is the farm refilling this printer's toolhead on its own ``toolhead_refill`` row — the row
    OPEN and ``recovering``, the durable PROMISE of the refill driver (K10, 2026-10-10)? Pure,
    DB-free, sync — the :func:`job_pause_held` idiom, for the same reader: the AMS entry gate
    (``spool_recovery.on_ams_fault`` and its mirror ``owned_full_codes``), which opens no AMS incident
    while it holds, because a fault the refill's own load raises is that refill's reading and a
    second incident would put a second driver onto one AMS. The ROW, never liveness: for the AMS
    entry, registration stays observability (:func:`register_driver`). An escalated refill row (the
    refill failed — a person's hold) does not hold the entry: a fault then opens its own row."""
    return (
        any(
            payload.get("kind") == KIND_TOOLHEAD_REFILL and payload.get("status") == STATUS_RECOVERING
            for payload in (_open_cache.get(printer_id) or {}).values()
        )
        if printer_id
        else False
    )


def cached_kind(printer_id: int, incident_id: int) -> str | None:
    """The KIND the open-incident cache holds for ``incident_id``, or ``None``.

    Sync and DB-free (the :func:`snapshot` idiom — this is read from poll loops that
    run once a second), and deliberately IDENTITY-SCOPED: it answers only while the
    printer's open row is still the one the caller names. A live recovery driver
    carries an immutable context resolved at its entry gate, and this is how it learns
    that the store re-classified THAT row underneath it (a jam the taxonomy later
    upgraded to a physical fault). A reader that answered about "whatever is open now"
    would report a re-classification every time a different incident opened.

    ``None`` for a closed row, a different open row, or an empty cache. None of those
    is a re-classification, and each already has its own lifecycle path — a driver
    learns a CLOSED row through the wire (the job ended, the printer resumed), never
    by inferring it from an absent projection.
    """
    cached = _open_cache.get(printer_id, {}).get(incident_id)
    if cached is None:
        return None
    return cached.get("kind")


def refill_state(printer_id: int | None) -> dict | None:
    """The farm's refill of this printer's toolhead, as the "Toolhead empty" chip reads it — DERIVED
    from the open-row projection and the liveness slot at READ time, never stored (K10/C3c,
    2026-10-10). Pure, DB-free, sync: ``printer_manager.toolhead_payload`` reads it on every status
    broadcast. ``{"phase", "command", "slot", "answer"}`` or ``None``:

    * ``loading`` — a driver is LIVE and it is loading this toolhead: a ``toolhead_refill`` row it is
      ``recovering`` (the slot the row records — the job's last feeder), or a load in flight on any
      open row (``pending_command`` a load: the slot it names); ``command`` is ``load``, ``answer``
      ``None``;
    * ``failed`` — an open row's LAST command did not reach its end (``failed_command``, the ledger's
      own derivation): ``command`` is that step's own name (``load`` / ``unload`` — the ledger's
      command steps are ``ams_command``'s alone), the slot it named (an unload names none — the
      row's slot), and the answer;
    * ``None`` — neither.

    Rows are read in :data:`KIND_PRECEDENCE` order, so a printer carrying an AMS row beside a refill
    row names the AMS row's fact first.
    """
    if not printer_id:
        return None
    rows = sorted((_open_cache.get(printer_id) or {}).values(), key=lambda payload: _precedence(payload.get("kind")))
    if driver_live(printer_id):
        for payload in rows:
            pending = payload.get("pending_command") or {}
            if pending.get("name") == "load":
                return {
                    "phase": "loading",
                    "command": "load",
                    "slot": runout_slot_desc(pending.get("target")),
                    "answer": None,
                }
            if payload.get("kind") == KIND_TOOLHEAD_REFILL and payload.get("status") == STATUS_RECOVERING:
                return {"phase": "loading", "command": "load", "slot": payload.get("slot_desc"), "answer": None}
    for payload in rows:
        failed = payload.get("failed_command")
        if failed:
            command = failed.get("name")
            slot = runout_slot_desc(failed.get("target")) if command == "load" else payload.get("slot_desc")
            return {"phase": "failed", "command": command, "slot": slot, "answer": failed.get("answer")}
    return None


def pending_command(printer_id: int | None, *, incident_id: int | None = None) -> PendingCommand | None:
    """The farm's motion command that has not run on this printer's open row, or ``None``.
    Pure, DB-free, sync — the projection the ledger's one derivation keeps
    (:func:`_ledger_projections`, re-derived at every send and read and at :func:`rehydrate`).

    ``incident_id`` scopes the question to ONE row (the driver asking about its own); without it,
    any open row of the printer that holds one — only the AMS recovery driver writes motion command
    steps, and a printer holds at most one open AMS row, so there is at most one. ``None`` for a
    printer with no open row, a row the cache does not hold, or a row whose last command settled.
    """
    if not printer_id:
        return None
    rows = _open_cache.get(printer_id) or {}
    payloads = [rows.get(incident_id)] if incident_id is not None else list(rows.values())
    for payload in payloads:
        pending = (payload or {}).get("pending_command")
        if pending:
            return PendingCommand.from_payload(pending)
    return None


# --- driver liveness (2026-09-23) ---------------------------------------------------


def register_driver(printer_id: int, task: asyncio.Task[object], *, incident_id: int | None) -> None:
    """Take this printer's liveness slot for a freshly spawned recovery driver.

    For the AMS recovery driver it is OBSERVABILITY, not a gate: entry exclusivity stays
    the DB's partial unique index, so no liveness check guards that spawn — the durable
    row is the authority and a second, in-memory one would only drift from it.

    For the plate-check episode driver (``pause_recovery``, 2026-09-29) :func:`driver_live`
    IS a gate. That lane re-enters an open ``recovering`` row after a restart, and its
    episode row already exists when it does, so the unique index cannot refuse a second
    driver: the lane refuses to spawn while :func:`driver_live` holds, and a
    ``recovering`` ``job_pause`` row is owned only while a driver is live
    (``incident_resolution.driver_owns``). It stays one slot per printer for both lanes.

    ``incident_id`` is ``None`` when the driver registers at its SPAWN, before its row
    exists — the plate-check trip: the ~1 Hz sampler spawns the task and registers it in the
    same synchronous stretch, so the next status push already reads it live and cannot spawn
    a second driver while the first one is still opening its row (a task runs nothing until
    the loop next turns). The registry stores no id — it is liveness, not ownership — so
    there is nothing to bind once the row opens: the id serves only the violation line
    below, and the driver's own log lines name its row from the moment it opens.

    Either way a driver spawning over a LIVE one means a closer freed the open row from
    under that one, so if it ever happens it must be one grep away: 006-H2S 17:23:55
    (2026-09-04) produced no line at all, and the single line naming the moment is the
    difference between a grep and a 15 h triage.
    """
    live = _drivers.get(printer_id)
    if live is not None and not live.done():
        logger.warning(
            "printer_incidents: printer %s: spawning a recovery driver while one is live — invariant violated (%s)",
            printer_id,
            f"incident {incident_id if incident_id is not None else '(not yet opened)'} spawned over a live driver",
        )
    _drivers[printer_id] = task


def release_driver(printer_id: int, task: asyncio.Task[object]) -> None:
    """Give the slot back — ONLY if it still holds ``task``.

    A driver exits in its own ``finally``, and by then a SUCCESSOR may already hold the
    slot (the spawn-over-live case :func:`register_driver` warns about). Popping by
    printer alone would erase the successor's liveness, and every closer would then read
    "no driver" under a task that is running — so the release is identity-checked, and a
    release by a task that does not hold the slot is a no-op.
    """
    if _drivers.get(printer_id) is task:
        del _drivers[printer_id]


def driver_live(printer_id: int | None) -> bool:
    """Is a recovery driver task RUNNING on this printer right now? Pure, DB-free, sync.

    THE one spelling of liveness (module docstring). True iff a task holds the slot and
    ``not task.done()``: an empty slot and a finished task both read False, so a driver
    that crashed without releasing — the R1 orphan — never silences a closer forever.
    """
    if not printer_id:
        return False
    task = _drivers.get(printer_id)
    return task is not None and not task.done()


async def open_rows(db: AsyncSession, printer_id: int) -> list[PrinterIncident]:
    """Every OPEN row this printer carries, oldest first.

    The shape every lifecycle path uses now that a printer can hold more than one
    hold: each row is adjudicated on its own ``RESOLVES_ON`` rule, in Python, with a
    named verdict — never filtered away in SQL, where a row that is invisible reads
    as a row that is absent.
    """
    result = await db.execute(
        select(PrinterIncident)
        .where(PrinterIncident.printer_id == printer_id)
        .where(PrinterIncident.resolved_at.is_(None))
        .order_by(PrinterIncident.created_at, PrinterIncident.id)
    )
    return list(result.scalars().all())


async def get_open(db: AsyncSession, printer_id: int, *, kinds: Iterable[str] | None = None) -> PrinterIncident | None:
    """The printer's highest-precedence OPEN row, or None.

    ``kinds`` narrows it to the holds the CALLER means, and every call site says
    which: an AMS-fault lane must not be answered with a plate-vision row (it would
    refuse to open a real fault), and the plate-vision lane must not be answered with
    a jam (it would leave its own hold undecided). Both happened while a printer
    could only carry one row, and both were invisible because the wrong answer was
    always a plausible one.

    Precedence is applied in PYTHON over :data:`KIND_PRECEDENCE` rather than in SQL:
    a printer holds at most six rows, and the one order lives in the model.
    """
    rows = await open_rows(db, printer_id)
    if kinds is not None:
        wanted = frozenset(kinds)
        rows = [row for row in rows if row.kind in wanted]
    if not rows:
        return None
    return min(rows, key=lambda row: _precedence(row.kind))


async def all_open(db: AsyncSession) -> list[PrinterIncident]:
    """Every OPEN incident, oldest first — the watchdogs' fleet view."""
    result = await db.execute(
        select(PrinterIncident).where(PrinterIncident.resolved_at.is_(None)).order_by(PrinterIncident.created_at)
    )
    return list(result.scalars().all())


async def find_closed(db: AsyncSession, printer_id: int, job_id: str, codes: str) -> PrinterIncident | None:
    """A CLOSED incident for this exact ``(printer, job, fault fingerprint)``.

    The durable replacement for the ``_handled`` / ``_escalated`` module dicts. Only
    an ABORTED close bars re-entry (see :func:`~backend.app.services.spool_recovery.on_ams_fault`);
    the caller decides, this only reports.
    """
    result = await db.execute(
        select(PrinterIncident)
        .where(PrinterIncident.printer_id == printer_id)
        .where(PrinterIncident.job_id == job_id)
        .where(PrinterIncident.codes == codes)
        .where(PrinterIncident.resolved_at.is_not(None))
        .order_by(PrinterIncident.resolved_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def last_closed_by(db: AsyncSession, printer_id: int, source: str) -> PrinterIncident | None:
    """This printer's most recently CLOSED row whose close was ``source``, or None.

    The recovery driver's printer bound asks it with ``RESOLVE_DRIVER_RESTART``: the last
    time this printer's path stalled on a first load and the driver restarted the job
    (``spool_recovery._restart_refault``) — a second such stall with no print completed
    since is the path's, not the spool's. An open row has no close, so the incident asking
    is never its own answer.
    """
    result = await db.execute(
        select(PrinterIncident)
        .where(PrinterIncident.printer_id == printer_id)
        .where(PrinterIncident.resolve_source == source)
        .where(PrinterIncident.resolved_at.is_not(None))
        .order_by(PrinterIncident.resolved_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def count_resolved(db: AsyncSession, printer_id: int, job_id: str, kind: str) -> int:
    """How many incidents of ``kind`` this job has already RECOVERED from.

    The durable successor of ``_success_counts``: it bounds the
    jam→recover→jam ping-pong a dying extruder can otherwise sustain all day, and
    unlike the dict it survives the restart that used to hand a sick printer a fresh
    budget.
    """
    return int(
        await db.scalar(
            select(sa_func.count())
            .select_from(PrinterIncident)
            .where(PrinterIncident.printer_id == printer_id)
            .where(PrinterIncident.job_id == job_id)
            .where(PrinterIncident.kind == kind)
            .where(PrinterIncident.status == STATUS_RESOLVED)
        )
        or 0
    )


async def open_new(
    db: AsyncSession,
    *,
    printer_id: int,
    job_id: str,
    item_id: int | None,
    kind: str,
    code: str,
    codes: str,
    slot_global_tray: int | None,
    hms_full_codes: Iterable[str] = (),
    status: str = STATUS_RECOVERING,
) -> PrinterIncident | None:
    """Create an open FAULT incident for this printer, or ``None`` when one owns it.

    The fault-shaped constructor: ``code``/``codes`` are the triggering HMS
    fingerprint and ``slot_global_tray`` the slot the firmware attributed. A kind with
    no fault behind it uses :func:`open_declared` instead of passing empty strings
    through here.

    ``hms_full_codes`` are the firmware's full codes for the fault, read off the live
    wire by the opener at the moment the hold opens (``hms_errors.full_codes_of``) — the
    printer's own words, recorded because the printer will stop showing them long before
    the hold ends. Every fault opener passes them; empty stores NULL.

    Two guards, deliberately both: a pre-check (the ordinary case, so the common path
    logs a reason instead of raising) and the partial unique indexes (the race). A
    caller that gets ``None`` must treat the printer as already owned FOR THIS KIND.
    """
    return await _open_row(
        db,
        printer_id=printer_id,
        job_id=job_id,
        item_id=item_id,
        kind=kind,
        code=code,
        codes=codes,
        slot_global_tray=slot_global_tray,
        hms_full_codes=join_full_codes(hms_full_codes),
        status=status,
    )


async def open_declared(
    db: AsyncSession,
    printer_id: int,
    *,
    kind: str,
    status: str = STATUS_ESCALATED,
) -> PrinterIncident | None:
    """Open a DECLARED hold — a human's statement, with no fault behind it.

    The constructor for the code-less kinds (:data:`DECLARED_KINDS`), so a caller
    that has nothing to say about a job, a code or a slot does not have to pass empty
    strings through the fault-shaped :func:`open_new`: ``job_id=""``, ``code=""``,
    ``codes=""``, ``item_id=None``, ``slot_global_tray=None`` are properties of the
    KIND and belong here rather than at every call site.

    ``status`` defaults to ``escalated`` because a declared hold is a human's from the
    instant it opens — nothing is "recovering" it — and ``escalated_at`` is then when
    the human took the printer, which is the age its banner and the ledger read.

    Same idempotency and race contract as :func:`open_new` (it shares the body):
    ``None`` means this printer already carries an open row of this kind, i.e. the
    hold the caller was about to open is already standing. Raises ``ValueError`` for
    any kind outside :data:`DECLARED_KINDS` — a fault opened with no fault
    fingerprint would be a row nothing can classify.
    """
    if kind not in DECLARED_KINDS:
        raise ValueError(f"{kind!r} is not a declared incident kind (expected one of {sorted(DECLARED_KINDS)})")
    return await _open_row(
        db,
        printer_id=printer_id,
        job_id="",
        item_id=None,
        kind=kind,
        code="",
        codes="",
        slot_global_tray=None,
        hms_full_codes=None,
        status=status,
    )


async def _open_row(
    db: AsyncSession,
    *,
    printer_id: int,
    job_id: str,
    item_id: int | None,
    kind: str,
    code: str,
    codes: str,
    slot_global_tray: int | None,
    hms_full_codes: str | None,
    status: str,
) -> PrinterIncident | None:
    """The ONE open transition behind :func:`open_new` and :func:`open_declared`.

    The two public constructors differ only in what a caller is expected to KNOW, so
    the exclusivity pre-check, the IntegrityError race report, the cache refresh and
    the OPENED log line live here once.

    The pre-check is kind-SCOPED and mirrors the database exactly: an AMS kind is
    refused by any open AMS row (the three are readings of one AMS), and every other
    kind only by an open row of its own kind — so a lost-Z hold opens beside a jam,
    which is the collision that let an eject run against a fabricated Z datum, and a
    service hold stands beside whatever fault the printer already carries.
    """
    scope = AMS_FAULT_KINDS if kind in AMS_FAULT_KINDS else {kind}
    if await get_open(db, printer_id, kinds=scope) is not None:
        return None
    now = datetime.utcnow()
    incident = PrinterIncident(
        printer_id=printer_id,
        job_id=job_id or "",
        item_id=item_id,
        kind=kind,
        code=code or "",
        codes=codes[:256],
        slot_global_tray=slot_global_tray,
        hms_full_codes=hms_full_codes,
        status=status,
        created_at=now,
        escalated_at=now if status == STATUS_ESCALATED else None,
    )
    db.add(incident)
    try:
        await db.commit()
    except IntegrityError:
        # One of the two partial unique indexes fired: another callback opened this
        # printer's incident between the pre-check and the flush. Not an error — the
        # other actor owns it. Either index reports as the same race, because both
        # mean "somebody else already holds this printer for this fault".
        await db.rollback()
        logger.info(
            "printer_incidents: printer %s already has an open incident (index race) — %s %s not opened",
            printer_id,
            kind,
            code,
        )
        return None
    _open_cache.setdefault(printer_id, {})[incident.id] = _payload(incident)
    logger.info(
        "printer_incidents: printer %s incident %s OPENED kind=%s status=%s code=%s codes=%s item=%s slot=%s job=%s",
        printer_id,
        incident.id,
        kind,
        status,
        code,
        incident.codes,
        item_id if item_id is not None else "foreign",
        slot_global_tray,
        job_id or "-",
    )
    return incident


async def mark_escalated(db: AsyncSession, incident_id: int) -> PrinterIncident | None:
    """Move an open incident to ESCALATED — still open, now a human's hold.

    Idempotent: an incident already escalated keeps its ORIGINAL ``escalated_at``.
    The lanes that escalate at entry open the row escalated and then run the shared
    ``_escalate`` path, and a re-stamp would make the hold look younger than it is
    to anything reading the age.
    """
    incident = await db.get(PrinterIncident, incident_id)
    if incident is None or incident.resolved_at is not None:
        return None
    if incident.status == STATUS_ESCALATED:
        return incident
    incident.status = STATUS_ESCALATED
    incident.escalated_at = datetime.utcnow()
    await db.commit()
    await _reproject(db, incident)
    return incident


async def upgrade(
    db: AsyncSession,
    incident_id: int,
    *,
    kind: str,
    code: str,
    codes: str,
    slot_global_tray: int | None,
    hms_full_codes: Iterable[str] = (),
) -> PrinterIncident | None:
    """Re-classify an OPEN row onto a worse fault. ``None`` when it is gone or closed.

    003-H2S 2026-09-11: ``0700_0012`` arrived 1.2 s before ``0700_8004`` (filament
    physically stuck in the shared PTFE path), so the row opened ``jam`` — and
    because an open incident could never be re-classified, the swap machine ran a
    CONTINUE, stamped a healthy spool out of rotation and sent two unloads against
    filament that cannot retract. The alternative to upgrading is not "refuse the
    worse fault"; it is "act on the milder classification for the rest of the hold".

    A COMPARE-AND-SET on openness, and it writes the LIVE fingerprint — kind, code,
    codes and slot together — because ``_reenter_recovering_incident`` already states
    the rule this depends on: the stored fault and the live one must name the same
    thing, or the aborted-close ledger and the wire sampler loop against each other.

    The printer's recorded words (``hms_full_codes``) follow the live fingerprint for the
    same reason: a row re-classified onto a physical fault must not keep showing the
    milder fault's text under the new chip.

    The row keeps its ID, its ``created_at`` and its ``escalated_at``: it is the same
    equipment fault, better understood. A live recovery driver learns of the change
    through :func:`cached_kind` (the ``reclassified`` takeover token) and hands over
    without aborting.
    """
    incident = await db.get(PrinterIncident, incident_id)
    if incident is None or incident.resolved_at is not None:
        return None
    previous = incident.kind
    incident.kind = kind
    incident.code = code or ""
    incident.codes = codes[:256]
    incident.slot_global_tray = slot_global_tray
    incident.hms_full_codes = join_full_codes(hms_full_codes)
    await db.commit()
    await _reproject(db, incident)
    logger.info(
        "printer_incidents: printer %s incident %s UPGRADED kind=%s->%s code=%s codes=%s slot=%s",
        incident.printer_id,
        incident.id,
        previous,
        kind,
        incident.code,
        incident.codes,
        slot_global_tray,
    )
    return incident


async def close(
    db: AsyncSession,
    incident_id: int,
    *,
    status: str,
    source: str | None,
) -> PrinterIncident | None:
    """Close an incident (``resolved`` or ``aborted``) and drop it from the cache.

    Returns the row when THIS CALL closed it, and ``None`` when it did not — a
    missing row, or one somebody else had already closed. Idempotent either way: an
    already-closed incident is left untouched, so two racing resolvers cannot
    re-stamp a close time or double-log.

    The return is the OWNERSHIP answer, mirroring :func:`mark_escalated`'s contract:
    a caller about to write an outcome for this incident can tell "I closed it" from
    "somebody else already had". 006-H2S 2026-09-04 is why it has to be tellable —
    the observed-running closer freed a row from under a live recovery driver, and
    the driver's own sinks went on writing the token, the page and the durable
    escalation row for an incident they no longer owned.
    """
    incident = await db.get(PrinterIncident, incident_id)
    if incident is None:
        return None
    if incident.resolved_at is not None:
        return None
    incident.status = status
    incident.resolved_at = datetime.utcnow()
    incident.resolve_source = source
    await db.commit()
    rows = _open_cache.get(incident.printer_id)
    if rows is not None:
        # ONE row, not the printer's whole entry: closing a jam must not take the
        # lost-Z hold standing beside it out of the chip and out of the dispatch gate.
        rows.pop(incident.id, None)
        if not rows:
            _open_cache.pop(incident.printer_id, None)
    logger.info(
        "printer_incidents: printer %s incident %s CLOSED status=%s source=%s kind=%s code=%s",
        incident.printer_id,
        incident.id,
        status,
        source or "-",
        incident.kind,
        incident.code,
    )
    return incident


async def close_open_for_printer(
    db: AsyncSession,
    printer_id: int,
    *,
    source: str,
    status: str = STATUS_RESOLVED,
    kinds: Iterable[str] | None = None,
) -> list[PrinterIncident]:
    """Close EVERY open incident this printer carries (of ``kinds``, when given).

    Returns the rows THIS CALL closed, empty when it closed none — a printer-scoped
    verb over a printer that can now hold several holds, so a single-row answer would
    silently leave the rest standing.
    """
    closed: list[PrinterIncident] = []
    for incident in await open_rows(db, printer_id):
        if kinds is not None and incident.kind not in frozenset(kinds):
            continue
        row = await close(db, incident.id, status=status, source=source)
        if row is not None:
            closed.append(row)
    return closed


# --- the step ledger (2026-09-23) --------------------------------------------------
#
# What a driver SENT against an incident, one ``printer_incident_step`` row per step (the
# model docstring has the why). This module is its ONE writer, and :class:`EvidenceLog`
# the one caller of that writer: a step is noted at the SEND and answered at the READ, so
# a crash between the two leaves "sent, never read" on disk — exactly what re-entry must
# know, because that step is spent and nobody saw what it did.


async def note_step(
    db: AsyncSession,
    incident_id: int,
    *,
    seq: int,
    kind: StepKind,
    name: str,
    target: int | None = None,
    feeder: str | None = None,
) -> PrinterIncidentStep:
    """Persist one step AT THE SEND and commit. Returns the row.

    ``seq`` is the driver's own send order within the incident; a duplicate
    ``(incident_id, seq)`` is a driver that lost count of its own log, and the unique
    index raises ``IntegrityError`` for it — after this rolls the failed commit back, so
    the caller's session stays usable.

    The open row's cached ledger facts are re-derived here (:func:`_rederive_ledger`), at
    the one writer, once the step is durable and BEFORE the driver publishes: a lever step
    named :data:`FAULT_RESTART_STEP` sets :data:`PAYLOAD_FAULT_RESTART_STOP`, so the terminal
    that stop produces is always classified against a projection that already says the farm
    sent it; a plate-check ``stop`` step becomes the row's unanswered ``last_stop``, so the
    sampler re-enters a stop a crash left unanswered.
    """
    step = PrinterIncidentStep(
        incident_id=incident_id,
        seq=seq,
        kind=kind,
        name=name,
        target=target,
        feeder=feeder,
        sent_at=datetime.utcnow(),
    )
    db.add(step)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise
    logger.info("printer_incidents: incident %s step %s %s=%s sent", incident_id, seq, kind, name)
    await _rederive_ledger(db, incident_id)
    return step


async def answer_step(db: AsyncSession, incident_id: int, seq: int, *, outcome: str) -> PrinterIncidentStep | None:
    """Record what the READ of step ``seq`` found, stamp ``read_at`` and commit.

    ``None`` when the incident has no such step — nothing was sent, so there is nothing
    to answer. The open row's cached ledger facts follow the answer (:func:`_rederive_ledger`):
    a plate-check stop answered here is the ``last_stop`` outcome the deadline timer and the
    sampler read.
    """
    step = await db.scalar(
        select(PrinterIncidentStep)
        .where(PrinterIncidentStep.incident_id == incident_id)
        .where(PrinterIncidentStep.seq == seq)
    )
    if step is None:
        return None
    step.outcome = outcome
    step.read_at = datetime.utcnow()
    await db.commit()
    logger.info(
        "printer_incidents: incident %s step %s %s=%s outcome=%s", incident_id, seq, step.kind, step.name, outcome
    )
    await _rederive_ledger(db, incident_id)
    return step


async def steps_of(db: AsyncSession, incident_id: int) -> list[PrinterIncidentStep]:
    """Every step of this incident, in send order (``seq`` ascending)."""
    result = await db.execute(
        select(PrinterIncidentStep)
        .where(PrinterIncidentStep.incident_id == incident_id)
        .order_by(PrinterIncidentStep.seq)
    )
    return list(result.scalars().all())


async def count_rows_with_step(
    db: AsyncSession,
    *,
    printer_id: int,
    kind: str,
    step_kind: StepKind,
    since: datetime,
    exclude_job_id: str | None,
) -> int:
    """How many rows of ``kind`` on this printer, opened at or after ``since``, hold at
    least one step of ``step_kind`` on the ledger — leaving out the row(s) of the job
    ``exclude_job_id``.

    The store read behind "how many OTHER plate-check episodes on this printer did the farm
    already stop in the window" (``pause_recovery``'s second rung, where the retry-or-escalate
    decision is made once): it counts the farm's own ``stop`` steps, so an operator's stop or
    a foreign terminal — neither of which writes one — never counts. Open and closed rows
    alike: an episode is counted by what the farm SENT in it, not by how it ended.

    The excluded job is decided by :func:`~backend.app.services.job_identity.is_held_job`,
    in Python — the held-job rule has one spelling, and SQL cannot spell its ``unknown``
    arm. So ``None`` / ``""`` / ``"0"`` all name the id-less job, and exclude the rows that
    recorded none. The step test is an ``EXISTS`` over the ledger, so a row with several
    steps of that kind is counted once.
    """
    has_step = (
        exists()
        .where(PrinterIncidentStep.incident_id == PrinterIncident.id)
        .where(PrinterIncidentStep.kind == step_kind)
    )
    rows = (
        await db.execute(
            select(PrinterIncident.job_id)
            .where(PrinterIncident.printer_id == printer_id)
            .where(PrinterIncident.kind == kind)
            .where(PrinterIncident.created_at >= since)
            .where(has_step)
        )
    ).scalars()
    return sum(1 for job in rows if not is_held_job(job, exclude_job_id))


# --- the evidence log: the step ledger's ONE writer --------------------------------


@dataclass(frozen=True, slots=True)
class StepEntry:
    """The ledger columns a step is WRITTEN with at the send (``note_step``'s arguments)."""

    kind: StepKind
    name: str
    target: int | None = None
    feeder: str | None = None


class LoggedStep(Protocol):
    """What :class:`EvidenceLog` needs of a driver's own step type.

    A step is a frozen value the driver drafts (``seq`` 0, unsent); the log places it
    (:meth:`sent`: its ``seq`` and send time), writes it as :meth:`entry`, and fills its
    read (:meth:`answered`, which RAISES ``LookupError`` for a token the driver's own
    vocabulary cannot name — an answer the driver cannot read back is drift, never data).
    """

    @property
    def seq(self) -> int: ...

    def entry(self) -> StepEntry: ...

    def sent(self, seq: int, at: datetime) -> Self: ...

    def answered(self, outcome: str) -> Self: ...


S = TypeVar("S", bound=LoggedStep)


@dataclass
class EvidenceLog(ABC, Generic[S]):
    """THE evidence log of one incident: every step a driver sent against it, in send
    order, with what the wire answered — the RECORDS a driver's next step, its give-up
    reason and its page are derived from.

    Durable, because the wire cannot restate it: on a feeder-stall wedge every release
    verb's consequence is identical (012-H2S 2026-09-23 — both CONTINUE spellings re-ran
    the stalled feed and re-held in ``1/5``), and a plate check's PAUSE reads the same
    before and after the farm pressed "Problem solved, resume" — so a driver restarted
    from the wire alone re-sends what it already sent. Every step is persisted at the
    SEND (:meth:`note`) and answered at the READ (:meth:`answer`) through
    :func:`note_step` / :func:`answer_step` — this class is their ONE caller — and a log
    is built ONLY by :meth:`from_row` (AST-pinned), so a driver never starts from an
    in-memory log that forgot what the incident already sent.

    Generic over the driver's step type, because the VOCABULARY is the driver's (a lever
    and its reading, an AMS command and its answer, a dialog button and its ACK) while
    the write discipline is the store's. A driver binds its vocabulary by subclassing and
    implementing :meth:`_step_of`; everything it derives from its log (budgets, the
    give-up reason, the rung it owes) lives on that subclass, as projections of
    :attr:`steps`, never as a second count.

    Moved here from ``spool_recovery`` on 2026-09-29, when the plate-check episode
    driver became the ledger's second driver: a second writer is exactly what the
    single-writer pin forbids, so the writer moved to the store both drivers already use.
    """

    incident_id: int
    steps: list[S] = field(default_factory=list)

    @classmethod
    async def from_row(cls, db: AsyncSession, incident_id: int) -> Self:
        """The log of ``incident_id`` as the ledger holds it — empty for a fresh incident,
        the previous driver's steps for a re-entered one. Hydration RAISES (through
        :meth:`_step_of`) on a row the driver's vocabulary cannot name."""
        return cls(incident_id=incident_id, steps=[cls._step_of(row) for row in await steps_of(db, incident_id)])

    @classmethod
    @abstractmethod
    def _step_of(cls, row: PrinterIncidentStep) -> S:
        """Hydrate ONE ledger row into the driver's step type. A token the driver's
        vocabulary cannot name RAISES ``LookupError`` — a ledger that says something the
        driver cannot read is drift, never a skipped row."""

    async def note(self, step: S) -> int:
        """Append ``step`` (a driver's unsent draft) with the next ``seq`` and PERSIST it,
        in its own session, at the send. Returns the seq.

        Best-effort on the write: a failed commit is logged and the in-memory step stands,
        so the live driver's own budget still holds — what is lost is only the durability
        a re-entry would read. A ledger write must never crash a driver.
        """
        from backend.app.core.database import async_session

        seq = max((s.seq for s in self.steps), default=0) + 1
        at = datetime.utcnow()
        entry = step.entry()
        try:
            async with async_session() as db:
                row = await note_step(
                    db,
                    self.incident_id,
                    seq=seq,
                    kind=entry.kind,
                    name=entry.name,
                    target=entry.target,
                    feeder=entry.feeder,
                )
                at = row.sent_at
        except Exception:  # noqa: BLE001 — a ledger write must never crash the driver
            logger.exception("printer_incidents: incident %s step %s could not be persisted", self.incident_id, seq)
        self.steps.append(step.sent(seq, at))
        return seq

    async def answer(self, seq: int, outcome: str) -> None:
        """Fill step ``seq``'s outcome in memory AND on the ledger. The step validates the
        token (:meth:`LoggedStep.answered`) before anything is written."""
        from backend.app.core.database import async_session

        index = next((i for i, s in enumerate(self.steps) if s.seq == seq), None)
        if index is None:
            raise LookupError(f"printer_incidents: incident {self.incident_id} has no step {seq} to answer")
        self.steps[index] = self.steps[index].answered(outcome)
        try:
            async with async_session() as db:
                await answer_step(db, self.incident_id, seq, outcome=outcome)
        except Exception:  # noqa: BLE001 — a ledger write must never crash the driver
            logger.exception(
                "printer_incidents: incident %s step %s answer could not be persisted", self.incident_id, seq
            )

    def has_step(self, kind: StepKind) -> bool:
        """Does this log hold a step of ``kind`` — sent, answered or not? The per-episode
        reading of :func:`count_rows_with_step`'s ledger test."""
        return any(step.entry().kind == kind for step in self.steps)


# --- the outcome ledger (2026-09-11) ------------------------------------------------
#
# WHAT a closed row means, derived from three stored facts and nothing else:
# ``status`` (resolved / aborted), ``escalated_at`` (did a human get paged) and
# ``resolve_source`` (who produced the close). The 2026-09-11 audit had to hand-count
# "how many holds did the farm end by itself" from logs and a SELECT; this table is
# that count's ONE origin, and ``GET /api/v1/incidents`` its query surface.
OUTCOME_RECOVERING = "recovering"  # open; the machine is acting
OUTCOME_HELD = "held"  # open; escalated — a human's
OUTCOME_AUTO_RECOVERED = "auto_recovered"  # closed without ever paging, by the farm's own act
OUTCOME_HUMAN_RESOLVED = "human_resolved"  # paged, then closed — a human was in the loop
OUTCOME_RESOLVED_UNPAGED = "resolved_unpaged"  # closed without a page, on wire/terminal/rearm evidence
OUTCOME_TAKEN_OVER = "taken_over"  # aborted: an external actor took over mid-procedure
OUTCOME_TRANSIENT = "transient"  # aborted with no source: it never held the printer

OUTCOMES: tuple[str, ...] = (
    OUTCOME_RECOVERING,
    OUTCOME_HELD,
    OUTCOME_AUTO_RECOVERED,
    OUTCOME_HUMAN_RESOLVED,
    OUTCOME_RESOLVED_UNPAGED,
    OUTCOME_TAKEN_OVER,
    OUTCOME_TRANSIENT,
)

# The closes the FARM performed. A refill auto-resume on a row that was never paged
# cannot happen today (a runout escalates before its refill lane can fire), so its
# membership here is the rule, not an observed count. The restart
# (``RESOLVE_DRIVER_RESTART``) is the driver's third recovery beside the swap and the
# self-heal: the job it stopped had deposited nothing, and the unit goes back to the queue.
# ``recheck_passed`` (2026-09-29) is the plate-check driver's: it pressed the printer's own
# "Problem solved, resume" and the same job printed on.
_FARM_CLOSES: frozenset[str] = frozenset(
    {
        RESOLVE_DRIVER_SWAP,
        RESOLVE_DRIVER_SELF_HEAL,
        RESOLVE_DRIVER_RESTART,
        RESOLVE_AUTO_RESUME,
        RESOLVE_RECHECK_PASSED,
        RESOLVE_REFILL_RESUMED,
    }
)


def outcome_of(incident: OutcomeFacts) -> str:
    """Which :data:`OUTCOMES` bucket this row is in. Pure; total over every row shape.

    ``escalated_at`` is the human axis: once a page went out, the close — whatever
    produced it — had a human in the loop (they refilled, resumed, fixed the path,
    stopped the print, or pressed Recover). Only a row that closed WITHOUT paging can
    be the farm's own recovery, and only when the close came from the farm's own act
    (:data:`_FARM_CLOSES`) or, for a plate-vision trip, from the terminal of the stop
    the farm itself sent — the stop-and-retry that requeues without a page.
    Everything else that closed unpaged closed on evidence nobody produced (a wire
    edge, a job ending, a restart) and is counted honestly as neither.

    An ABORTED row that names its source is ``taken_over`` whatever the source: the
    driver stood aside because another actor holds the printer — ``operator`` (a human
    took it mid-procedure) and ``paused_elsewhere`` (the print was PAUSEd on a quiet path
    after the driver's own swap resume ran, by an actor the wire does not name) alike. One
    rule for every sourced abort, so a new stand-aside token lands in the bucket by
    construction; only a source-less abort (the fault never held the printer) is
    ``transient``.

    The ``plate_vision`` arms are LIVE (2026-09-29, the plate-check ladder). A plate-check
    episode opens ``recovering`` and the farm's driver acts, so an unpaged row is read by
    who answered it:

    * ``recheck_passed`` — the farm's re-check press let the job print on: in
      :data:`_FARM_CLOSES`, ``auto_recovered``.
    * ``terminal`` — the paused job's terminal with no refusal: the farm's own stop and
      its retry (the unit requeued next in line, no gate, no page), so ``auto_recovered``.
      The rows the retired 2026-09-04 lane closed at the terminal of its own stop read
      the same way, as they always did.
    * ``plate_refused`` — the terminal REFUSED the plate: a human-clear gate and a page
      follow, the printer is a human's to clear, so ``human_resolved`` whether or not the
      row itself was escalated first (the page rides the plate gate, not this row).
      ``human_resolved`` rather than ``held``: the row is closed, and ``held`` is an open
      row's bucket.
    * ``handed_over`` — the job paused for another owner (the power-loss prompt); nobody
      recovered anything, so it falls through to ``resolved_unpaged`` like any close on
      evidence nobody produced.
    """
    if incident.resolved_at is None:
        return OUTCOME_HELD if incident.status == STATUS_ESCALATED else OUTCOME_RECOVERING
    if incident.status == STATUS_ABORTED:
        return OUTCOME_TAKEN_OVER if incident.resolve_source else OUTCOME_TRANSIENT
    if incident.escalated_at is not None:
        return OUTCOME_HUMAN_RESOLVED
    if incident.resolve_source in _FARM_CLOSES:
        return OUTCOME_AUTO_RECOVERED
    if incident.kind == KIND_PLATE_VISION and incident.resolve_source == RESOLVE_TERMINAL:
        return OUTCOME_AUTO_RECOVERED
    if incident.kind == KIND_PLATE_VISION and incident.resolve_source == RESOLVE_PLATE_REFUSED:
        return OUTCOME_HUMAN_RESOLVED
    return OUTCOME_RESOLVED_UNPAGED


def held_seconds(incident: OutcomeFacts, now: datetime) -> float:
    """How long this row has HELD its printer: ``created_at`` → close, or → ``now`` while open.

    THE one derivation of an incident's duration, so the row a reader sees on
    ``GET /incidents`` and the seconds a metrics overlay sums are the same number.
    Clamped at zero: a close stamped before the open (a clock step between two
    ``utcnow`` calls) is a broken row, not negative downtime.

    ``created_at`` is ``nullable=False``, so the missing-stamp arm answers for an
    instance that has not been flushed yet rather than for anything the table can
    hold — it returns 0.0 rather than raising, because a ledger read must not die
    on one malformed row.

    It measures the LEDGER's interval, not the hardware's: a close can trail the
    physical clear by the recovery lane's own dwell. That is the honest figure for
    "how long was this printer held", which is the question every caller asks.
    """
    if not incident.created_at:
        return 0.0
    end = incident.resolved_at or now
    return max(0.0, (end - incident.created_at).total_seconds())


@dataclass(frozen=True, slots=True)
class HeldStats:
    """The TIME facts about one kind's rows. The outcome tally stays :func:`summary`'s.

    ``total_held_s`` sums every row, open ones clamped at ``now``; the two
    percentiles describe RESOLVED rows only, because how long a still-open hold
    will take to end is not a measurement — including it would drag every figure
    toward zero exactly while a printer is down.
    """

    #: Rows of this kind in the window.
    count: int
    #: ...of which still open at ``now``.
    open_count: int
    #: Printer-seconds held, open rows counted up to ``now``.
    total_held_s: float
    #: Time to recover over the CLOSED rows; ``None`` when none has closed yet.
    median_recover_s: float | None
    p90_recover_s: float | None


def nearest_rank_p90(ordered: list[float]) -> float:
    """Nearest-rank p90 of an already-sorted, non-empty list.

    Nearest rank rather than an interpolating quantile for two reasons: it is
    total from a single sample (a kind often has one or two closed rows, and
    ``statistics.quantiles`` raises below two), and every value it returns is a
    recovery that actually happened — a figure an operator can go and find in the
    log, rather than one interpolated between two that did. The rank is computed
    in integer arithmetic so it cannot land a position early on a float a hair
    under the boundary.
    """
    rank = -(-len(ordered) * 9 // 10)
    return ordered[rank - 1]


def held_stats(rows: list[PrinterIncident], now: datetime) -> dict[str, HeldStats]:
    """Per-kind hold durations over ``rows``. Pure; the caller chooses the window.

    Keyed by kind over EVERY row, declared kinds included — the same reach as
    :func:`summary`'s ``by_kind``, and for the same reason: a maintenance hold
    genuinely holds a printer for a duration, so its seconds are real even though
    it is no equipment fault. What its percentiles measure is how long the hold
    STOOD, not a recovery from anything; a caller that wants faults alone filters
    on :data:`~backend.app.models.printer_incident.FAULT_KINDS` before calling.

    Deliberately NOT a second outcome tally: which rows the farm ended by itself
    is :func:`summary`'s question, over :func:`outcome_of`, and one fact keeps one
    owner. ``open_count`` here is read straight off ``resolved_at`` — the same
    column :func:`held_seconds` reads — because it is what makes the percentiles'
    denominator legible, not an outcome classification.
    """
    held: dict[str, list[float]] = {}
    recovered: dict[str, list[float]] = {}
    open_counts: dict[str, int] = {}
    for row in rows:
        seconds = held_seconds(row, now)
        held.setdefault(row.kind, []).append(seconds)
        if row.resolved_at is None:
            open_counts[row.kind] = open_counts.get(row.kind, 0) + 1
        else:
            recovered.setdefault(row.kind, []).append(seconds)
    stats: dict[str, HeldStats] = {}
    for kind, seconds_held in held.items():
        closed = sorted(recovered.get(kind, []))
        stats[kind] = HeldStats(
            count=len(seconds_held),
            open_count=open_counts.get(kind, 0),
            total_held_s=sum(seconds_held),
            median_recover_s=float(statistics.median(closed)) if closed else None,
            p90_recover_s=nearest_rank_p90(closed) if closed else None,
        )
    return stats


def summary(rows: Sequence[OutcomeFacts]) -> dict:
    """The tally over ``rows``: total, zero-human count, declared count, and the two breakdowns.

    **A DECLARED row is counted in ``by_kind`` and nowhere else** (2026-09-12). This
    is the EQUIPMENT-FAULT ledger — "what has the farm recovered from by itself, and
    what did a human have to finish" — and a planned maintenance hold is neither: it
    has no fault, it produced no page, and nobody "recovered" from it. Leaving it in
    ``total`` would dilute the zero-human ratio by exactly the amount of maintenance
    the shop does, and its ``by_outcome`` bucket would read ``held`` / ``human_resolved``
    as though a machine had broken. It still appears under ``by_kind`` (the rows exist
    and are worth seeing) and its own count comes back as ``declared``, so the two
    figures stay reconcilable: ``total + declared == len(rows)``.
    """
    by_outcome = dict.fromkeys(OUTCOMES, 0)
    by_kind: dict[str, dict[str, int]] = {}
    declared = 0
    for row in rows:
        outcome = outcome_of(row)
        by_kind.setdefault(row.kind, dict.fromkeys(OUTCOMES, 0))[outcome] += 1
        if row.kind in DECLARED_KINDS:
            declared += 1
            continue
        by_outcome[outcome] += 1
    return {
        "total": len(rows) - declared,
        "zero_human": by_outcome[OUTCOME_AUTO_RECOVERED],
        "declared": declared,
        "by_outcome": by_outcome,
        "by_kind": by_kind,
    }


# --- the ledger read model (2026-10-07) ---------------------------------------------
#
# ``GET /api/v1/incidents`` (the Stats page's Faults tab) reads the ledger through ONE
# composition, :func:`ledger_page`: a site-date window, the kind / printer / outcome
# filters, offset paging, the window-wide :func:`summary` and the recurring-issue lines.
# The window is resolved HERE (``site_time.day_bounds``, as ``/fleet-metrics`` resolves
# its own), so the Faults tab's tally and the Fleet tab's recovery summary count the same
# rows for the same dates; the route validates and maps only.


@dataclass(frozen=True, slots=True)
class Recurrence:
    """The thresholds of the recurring-issue rule (:func:`recurring_signatures`).

    Deliberately strict, so the Faults tab's strip is empty on most days. Over the
    farm's 2026-10-07 ledger a 30-day window flags one printer line (011-H2S extruder
    overload ``0300_801E``: 9 holds on 4 days, last the day before) and one fleet line
    (foreign objects on the plate ``0500_806E`` on 13 of 16 printers); a 14-day window
    flags the fleet line only. The one-day bursts that night (014-H2S plate offset on
    its arrival day, 012-H2S's shape-41 wedge day, 013-H2S ``0700_0019``) are correctly
    not raised.

    Recency is SEVEN site days, for printer and fleet lines alike (operator, 2026-10-07):
    a signature silent for over a week is not recurring, and a 14-day horizon diluted the
    strip with lines like 012-H2S ``0700_0012`` (last hold 14 days back) and 009-H2S
    ``0700_8010`` (last hold 10 days back, 6 min held in all).
    """

    #: Holds of one signature on one printer before it can be a printer line.
    min_holds: int
    #: Distinct SITE days those holds fell on — a one-day burst is not recurrence.
    min_days: int
    #: The last hold's site date may be at most this many site days before the window end.
    recency_days: int
    #: Share of the active roster that must carry a ``(kind, code)`` for it to be ONE fleet line.
    fleet_share: float
    #: Distinct carriers a fleet line needs whatever the share — on a roster of one or two, ONE
    #: printer is half the fleet, and a fleet line about a single printer is a printer line.
    fleet_min_carriers: int


_RECURRENCE = Recurrence(min_holds=3, min_days=3, recency_days=7, fleet_share=0.5, fleet_min_carriers=2)

RecurrenceScope = Literal["printer", "fleet"]
SCOPE_PRINTER: RecurrenceScope = "printer"
SCOPE_FLEET: RecurrenceScope = "fleet"


@dataclass(frozen=True, slots=True)
class RecurringWorst:
    """One of a fleet line's worst carriers."""

    printer_id: int
    printer_name: str
    holds: int


@dataclass(frozen=True, slots=True)
class RecurringSignature:
    """One recurring-issue line: a ``(kind, code)`` that keeps holding a printer, or the fleet.

    ``scope == "printer"`` names one printer (``worst`` is empty); ``scope == "fleet"``
    names none and carries its two worst carriers in ``worst``. ``fleet_median`` is a
    DISPLAYED figure — the median holds of this ``(kind, code)`` per ACTIVE printer, a
    printer without one counting as 0 — and decides nothing.
    """

    scope: RecurrenceScope
    printer_id: int | None
    printer_name: str | None
    kind: str
    code: str
    printer_message: PrinterMessage | None
    holds: int
    days: int
    held_s: float
    last_at: datetime
    fleet_median: float
    printers_affected: int
    roster_size: int
    #: The site's UTC offset AT ``last_at`` (``site_time.offset_minutes``), for a client
    #: that renders the site-local wall clock without a tz database.
    utc_offset_minutes: int
    worst: tuple[RecurringWorst, ...] = ()


@dataclass(frozen=True, slots=True)
class _Episodes:
    """What one group of rows amounts to: count, distinct site days, held time, last hold."""

    holds: int
    days: int
    held_s: float
    last_at: datetime
    last_day: date

    @classmethod
    def of(cls, rows: Sequence[LedgerFacts], now: datetime) -> _Episodes:
        stamps = [row.created_at for row in rows if row.created_at is not None]
        last_at = max(stamps)
        return cls(
            holds=len(rows),
            days=len({site_time.to_site(stamp).date() for stamp in stamps}),
            held_s=sum(held_seconds(row, now) for row in rows),
            last_at=last_at,
            last_day=site_time.to_site(last_at).date(),
        )

    def recent(self, date_to: date) -> bool:
        """Site-DATE arithmetic against the window end — the site calendar is ``site_time``'s."""
        return (date_to - self.last_day).days <= _RECURRENCE.recency_days


def recurring_signatures(
    rows: Iterable[LedgerFacts],
    *,
    date_to: date,
    roster: Mapping[int, str],
    now: datetime,
) -> list[RecurringSignature]:
    """The recurring-issue lines over a window's UNFILTERED rows. Pure; sorted by held time.

    A signature is ``(printer_id, kind, code)`` over rows whose kind is in
    :data:`~backend.app.models.printer_incident.RECURRENCE_KINDS` and whose code is not an
    external-spool instruction (:func:`row_external`). ``roster`` is the ACTIVE printers
    (id → name); a row from any other printer counts toward nothing, and an EMPTY roster
    (every dev stack, whose real printers are all inactive) has no lines.

    * **Fleet line** — a ``(kind, code)`` carried by at least :attr:`Recurrence.fleet_share`
      of the roster AND by at least :attr:`Recurrence.fleet_min_carriers` printers (so one
      printer on a one- or two-printer roster is never "the fleet"), on at least :attr:`Recurrence.min_days` distinct site days overall,
      the last within :attr:`Recurrence.recency_days` site days of ``date_to``: ONE line
      naming the two worst carriers, never one line per printer.
    * **Printer line** — a signature that is NOT fleet-wide with at least
      :attr:`Recurrence.min_holds` holds on at least :attr:`Recurrence.min_days` distinct
      site days, the last within :attr:`Recurrence.recency_days` site days of ``date_to``.

    A fleet-wide ``(kind, code)`` never yields a printer line, whether or not its fleet line
    passes — the cause is the process, not the printer. ``now`` measures the open rows'
    held time (:func:`held_seconds`).
    """
    if not roster:
        return []
    carriers_of: dict[tuple[str, str], dict[int, list[LedgerFacts]]] = {}
    for row in rows:
        if row.printer_id not in roster or row.kind not in RECURRENCE_KINDS or row.created_at is None:
            continue
        if row_external(row):
            continue
        carriers_of.setdefault((row.kind, row.code), {}).setdefault(row.printer_id, []).append(row)

    lines: list[RecurringSignature] = []
    for (kind, code), carriers in carriers_of.items():
        fleet_median = float(statistics.median(len(carriers.get(printer_id, ())) for printer_id in roster))
        message = printer_message_from_short_code(code)
        fleet_wide = len(carriers) >= _RECURRENCE.fleet_min_carriers and len(carriers) >= _RECURRENCE.fleet_share * len(
            roster
        )
        if fleet_wide:
            episodes = _Episodes.of([row for printer_rows in carriers.values() for row in printer_rows], now)
            if episodes.days < _RECURRENCE.min_days or not episodes.recent(date_to):
                continue
            ranked = sorted(carriers.items(), key=lambda item: (-len(item[1]), roster[item[0]], item[0]))
            lines.append(
                RecurringSignature(
                    scope=SCOPE_FLEET,
                    printer_id=None,
                    printer_name=None,
                    kind=kind,
                    code=code,
                    printer_message=message,
                    holds=episodes.holds,
                    days=episodes.days,
                    held_s=episodes.held_s,
                    last_at=episodes.last_at,
                    fleet_median=fleet_median,
                    printers_affected=len(carriers),
                    roster_size=len(roster),
                    utc_offset_minutes=site_time.offset_minutes(episodes.last_at),
                    worst=tuple(
                        RecurringWorst(printer_id=printer_id, printer_name=roster[printer_id], holds=len(held))
                        for printer_id, held in ranked[:2]
                    ),
                )
            )
            continue
        for printer_id, printer_rows in carriers.items():
            episodes = _Episodes.of(printer_rows, now)
            if (
                episodes.holds < _RECURRENCE.min_holds
                or episodes.days < _RECURRENCE.min_days
                or not episodes.recent(date_to)
            ):
                continue
            lines.append(
                RecurringSignature(
                    scope=SCOPE_PRINTER,
                    printer_id=printer_id,
                    printer_name=roster[printer_id],
                    kind=kind,
                    code=code,
                    printer_message=message,
                    holds=episodes.holds,
                    days=episodes.days,
                    held_s=episodes.held_s,
                    last_at=episodes.last_at,
                    fleet_median=fleet_median,
                    printers_affected=len(carriers),
                    roster_size=len(roster),
                    utc_offset_minutes=site_time.offset_minutes(episodes.last_at),
                )
            )
    lines.sort(key=lambda line: (-line.held_s, -line.holds, line.scope, line.printer_id or 0, line.kind, line.code))
    return lines


@dataclass(frozen=True, slots=True)
class LedgerPage:
    """One page of the ledger read model, with the window-wide figures beside it.

    ``total`` counts the rows matching every filter across the window (so
    ``total == summary["total"] + summary["declared"]`` when no ``outcome`` filter is
    set); ``summary`` is over the kind + printer filtered population only, so an outcome
    filter cannot collapse the auto-recovered share to 0 % / 100 %; ``recurring`` is over
    the UNFILTERED window. ``rows`` are full ORM rows in page order, newest first.
    ``recurring_ids`` are the page rows whose ``(printer_id, kind, code)`` matches a
    printer-scope line. ``now`` is the one instant every held time on the page is measured at.
    """

    total: int
    summary: dict
    recurring: tuple[RecurringSignature, ...]
    rows: tuple[PrinterIncident, ...]
    printer_names: dict[int, str]
    recurring_ids: frozenset[int]
    now: datetime


async def ledger_page(
    db: AsyncSession,
    *,
    date_from: date | None,
    date_to: date | None,
    kind: str | None,
    printer_id: int | None,
    outcome: str | None,
    limit: int,
    offset: int,
) -> LedgerPage:
    """Rows OPENED in the inclusive site-date window, filtered, paged newest first.

    ``date_from`` / ``date_to`` are site dates resolved through ``site_time.day_bounds``
    (an absent bound leaves that side open). Three queries per call, whatever the page:

    * Q3 — ``printers`` (id, name, is_active): names for every row, and the ACTIVE roster
      :func:`recurring_signatures` counts over;
    * Q1 — the window's rows as nine narrow columns, ``(created_at DESC, id DESC)`` (rows
      opened in one push share a stamp, so the id is the tiebreak). Recurrence, the
      filters, :func:`summary` and :func:`outcome_of` run over these light rows in Python —
      the one derivation of each, never a SQL twin;
    * Q2 — the page's full rows by primary key, re-ordered to the page.
    """
    now = datetime.utcnow()
    printers = (await db.execute(select(Printer.id, Printer.name, Printer.is_active))).all()
    names = {printer.id: printer.name for printer in printers}
    active = {printer.id: printer.name for printer in printers if printer.is_active}

    stmt = select(
        PrinterIncident.id,
        PrinterIncident.printer_id,
        PrinterIncident.kind,
        PrinterIncident.code,
        PrinterIncident.status,
        PrinterIncident.escalated_at,
        PrinterIncident.resolve_source,
        PrinterIncident.resolved_at,
        PrinterIncident.created_at,
    )
    if date_from is not None:
        stmt = stmt.where(PrinterIncident.created_at >= site_time.day_bounds(date_from)[0])
    if date_to is not None:
        stmt = stmt.where(PrinterIncident.created_at < site_time.day_bounds(date_to)[1])
    window = (await db.execute(stmt.order_by(PrinterIncident.created_at.desc(), PrinterIncident.id.desc()))).all()

    recurring = recurring_signatures(
        window, date_to=date_to if date_to is not None else site_time.site_today(now), roster=active, now=now
    )
    population = [
        row
        for row in window
        if (kind is None or row.kind == kind) and (printer_id is None or row.printer_id == printer_id)
    ]
    kept = population if outcome is None else [row for row in population if outcome_of(row) == outcome]
    page_ids = [row.id for row in kept[offset : offset + limit]]

    rows: list[PrinterIncident] = []
    if page_ids:
        loaded = {
            incident.id: incident
            for incident in (await db.execute(select(PrinterIncident).where(PrinterIncident.id.in_(page_ids))))
            .scalars()
            .all()
        }
        rows = [loaded[incident_id] for incident_id in page_ids if incident_id in loaded]

    printer_lines = {(line.printer_id, line.kind, line.code) for line in recurring if line.scope == SCOPE_PRINTER}
    return LedgerPage(
        total=len(kept),
        summary=summary(population),
        recurring=tuple(recurring),
        rows=tuple(rows),
        printer_names=names,
        recurring_ids=frozenset(row.id for row in rows if (row.printer_id, row.kind, row.code) in printer_lines),
        now=now,
    )


async def list_overlapping(db: AsyncSession, *, start: datetime, end: datetime) -> list[PrinterIncident]:
    """Every row whose hold INTERSECTS ``[start, end)``, oldest first, uncapped.

    The question a timeline asks, which a "rows opened in the window" read cannot answer:
    an incident that opened before the window and was still holding right through it —
    the longest outages, exactly the ones a downtime figure must not miss — was not opened
    in it. Here an incident is the half-open interval ``[created_at, resolved_at or +inf)``
    and the test is the standard overlap: it began before the window ended, and it had not
    ended when the window began.

    **Or it OPENED in the window** (``created_at >= start``), so "opened in the window" —
    the Fleet tab's recovery tally, and :func:`ledger_page`'s window — is a SUBSET of
    "overlapping" by construction: a zero-length row at a day edge (closed at the instant
    it opened) would otherwise be counted by one read and not the other.

    No ``limit``: a cap on a row set a caller is about to SUM would silently
    under-report. The window is the bound.

    ``(created_at, id)`` ordering so a sweep over the rows is deterministic —
    incidents opened in one push share a stamp. Plain SQL comparisons on two
    columns, valid on SQLite and Postgres alike.
    """
    stmt = (
        select(PrinterIncident)
        .where(PrinterIncident.created_at < end)
        .where(
            or_(
                PrinterIncident.resolved_at.is_(None),
                PrinterIncident.resolved_at > start,
                PrinterIncident.created_at >= start,
            )
        )
        .order_by(PrinterIncident.created_at, PrinterIncident.id)
    )
    return list((await db.execute(stmt)).scalars().all())


async def rehydrate(db: AsyncSession) -> int:
    """Rebuild the projection cache from the DB. Returns the number of open rows.

    Called at startup, after the stale-incident sweep, so a restart mid-hold still
    renders the chip and still answers "is this printer owned". The ledger facts are
    derived from each row's ledger (:func:`_ledger_projections`), so a terminal classified
    after a restart still reads the farm's own stop. ``deadline_at`` starts ``None``: a
    decision hold's deadline is re-armed from ``escalated_at`` and the setting by its timer
    (``pause_recovery.rearm_decision_deadlines``), which writes it back.
    """
    _open_cache.clear()
    rows = await all_open(db)
    ledgers = await _ledger_projections(db, [incident.id for incident in rows])
    for incident in rows:
        _open_cache.setdefault(incident.printer_id, {})[incident.id] = _payload(
            incident, ledger=ledgers.get(incident.id)
        )
    return len(rows)
