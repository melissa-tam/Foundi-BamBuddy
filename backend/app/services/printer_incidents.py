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
crashing. The pre-WS2b exclusivity (a process-lifetime ``_active_tasks`` dict) was
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
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import func as sa_func, or_, select
from sqlalchemy.exc import IntegrityError

from backend.app.models.printer_incident import (
    AMS_FAULT_KINDS,
    DECLARED_KINDS,
    KIND_JAM,
    KIND_PHYSICAL,
    KIND_PLATE_VISION,
    KIND_POWER_LOSS,
    KIND_PRECEDENCE,
    KIND_RUNOUT,
    KIND_SERVICE_HOLD,
    KIND_Z_REFERENCE_LOST,
    RESOLUTION_OPERATOR,
    RESOLUTION_REPAIR,
    RESOLUTION_WIRE,
    RESOLVE_AUTO_RESUME,
    RESOLVE_DRIVER_SELF_HEAL,
    RESOLVE_DRIVER_SWAP,
    RESOLVE_TERMINAL,
    RESOLVES_ON,
    STATUS_ABORTED,
    STATUS_ESCALATED,
    STATUS_RECOVERING,
    STATUS_RESOLVED,
    PrinterIncident,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# The statuses that mean "closed" — both stamp ``resolved_at`` (see the model
# docstring's lifecycle table), so the open/closed question is asked of that column
# and never of this tuple.
CLOSED_STATUSES: tuple[str, ...] = (STATUS_RESOLVED, STATUS_ABORTED)

# printer_id -> {incident_id -> the WS/REST projection of that OPEN row}. Rebuilt
# from the DB at startup (:func:`rehydrate`) and maintained by every write below.
# Keyed by incident id rather than by kind because :func:`cached_kind` — how a live
# recovery driver learns its own row was re-classified — must be able to ask about
# ONE row while the kind under it changes.
_open_cache: dict[int, dict[int, dict]] = {}


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

    Pure over the same ``RESOLVES_ON`` table :func:`resolution_class` reads, stated
    ONCE here so the rule table and the WIRE agree by construction. Two classes say
    yes, for two different reasons, and both are the same statement from the operator's
    side — "I went to the machine and dealt with it":

    * ``operator`` — the evidence IS a human act (a part off the plate, a Z datum
      re-established), so both plate verbs end it;
    * ``repair`` — Recover means "an operator inspected this machine", which is the
      third return-to-normal the class admits beside its two motion evidences. (A
      ROUTINE clear-plate does not; this answers the weaker question "can Recover end
      it", which is what the card needs to decide whether to offer the verb.)

    ``wire`` says no — a runout hold is not answered by somebody clearing a plate —
    and ``declared`` says no by definition: a hold a human declared ends only through
    the verb that declared it.

    It exists because the printer card had to derive the affordance from the CLASS to
    know whether Recover applies, and the class vocabulary must not reach the wire:
    ``_payload`` projects this boolean as ``operator_exits`` instead (011-H2S
    2026-09-17 — a physical hold on an idle printer offered no Recover at all, because
    the card gated it on an occupancy claim the printer did not have).
    """
    return resolution_class(kind, external=external) in (RESOLUTION_OPERATOR, RESOLUTION_REPAIR)


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


def row_external(incident: PrinterIncident) -> bool:
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


def _reset_state() -> None:
    """Test hook: drop the projection cache between cases."""
    _open_cache.clear()


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


def _payload(incident: PrinterIncident) -> dict:
    """The projection the printer card renders.

    ``id`` rides along so a reader can ask about ONE row rather than about whatever
    is open now (see :func:`cached_kind`); the UI ignores it.

    ``operator_exits`` is :func:`closed_by_recover` — "would Recover end this hold" —
    and it is deliberately the BOOLEAN rather than the class: the card needs the
    affordance, not the vocabulary, and a UI that branched on ``"repair"`` would own a
    copy of the rule table. It is what lets the printer card offer Recover on a hold
    that raised no plate gate and no quarantine (011-H2S 2026-09-17, and the
    ``z_reference_lost`` hold before it).

    Only JSON PRIMITIVES: the WS lane serializes this dict with a bare ``json.dumps``.
    """
    return {
        "id": incident.id,
        "kind": incident.kind,
        "status": incident.status,
        "slot_desc": slot_desc(incident),
        "created_at": incident.created_at.isoformat() if incident.created_at else None,
        "operator_exits": closed_by_recover(incident.kind, external=row_external(incident)),
    }


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

    Pure and DB-free — read by ``printer_state_to_dict`` on every status broadcast.
    """
    if not printer_id:
        return None
    rows = _open_cache.get(printer_id)
    if not rows:
        return None
    if kind is not None:
        return next((payload for payload in rows.values() if payload.get("kind") == kind), None)
    return min(rows.values(), key=lambda payload: _precedence(payload.get("kind")))


def snapshots(printer_id: int | None) -> list[dict]:
    """EVERY open row of the printer as wire dicts, highest precedence first.

    The diagnostic-line reader (``print_scheduler._incident_summary``): a printer
    holding a jam AND a lost-Z frame must name both, or the line that is supposed to
    explain a refusal explains half of it. Pure, DB-free, sync.
    """
    if not printer_id:
        return []
    rows = _open_cache.get(printer_id)
    if not rows:
        return []
    return sorted(rows.values(), key=lambda payload: _precedence(payload.get("kind")))


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

    EVERY open kind blocks, deliberately. A ``plate_vision`` or ``z_reference_lost``
    row is already plate-gated, so this only makes the refusal legible rather than
    changing it; a ``power_loss`` row means the firmware's prompt is still
    unanswered, which is not a printer to put work on.
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


async def count_recent(db: AsyncSession, printer_id: int, kind: str, since: datetime) -> int:
    """How many incidents of ``kind`` this PRINTER has opened since ``since``.

    PRINTER-scoped and WINDOWED — the ``recovery_escalation`` 24 h-window shape, and
    deliberately NOT :func:`count_resolved`'s shape, which is JOB-scoped. The
    distinction is load-bearing for the plate-vision re-check: every requeue is a NEW
    job, so a job-scoped count can never see the first trip and "has this printer just
    tripped twice?" would always answer no. ``recent_terminal_farm_items`` is likewise
    unusable there — it excludes ``cancelled`` by design, which is exactly what a
    farm-stopped unit becomes.

    Counts every incident opened in the window whatever its status: a trip that has
    already RESOLVED (the re-check requeued and the incident closed at the terminal)
    is precisely the first trip the second one must see.
    """
    return int(
        await db.scalar(
            select(sa_func.count())
            .select_from(PrinterIncident)
            .where(PrinterIncident.printer_id == printer_id)
            .where(PrinterIncident.kind == kind)
            .where(PrinterIncident.created_at >= since)
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
    status: str = STATUS_RECOVERING,
) -> PrinterIncident | None:
    """Create an open FAULT incident for this printer, or ``None`` when one owns it.

    The fault-shaped constructor: ``code``/``codes`` are the triggering HMS
    fingerprint and ``slot_global_tray`` the slot the firmware attributed. A kind with
    no fault behind it uses :func:`open_declared` instead of passing empty strings
    through here.

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
    _open_cache.setdefault(incident.printer_id, {})[incident.id] = _payload(incident)
    return incident


async def upgrade(
    db: AsyncSession,
    incident_id: int,
    *,
    kind: str,
    code: str,
    codes: str,
    slot_global_tray: int | None,
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
    await db.commit()
    _open_cache.setdefault(incident.printer_id, {})[incident.id] = _payload(incident)
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
# membership here is the rule, not an observed count.
_FARM_CLOSES: frozenset[str] = frozenset({RESOLVE_DRIVER_SWAP, RESOLVE_DRIVER_SELF_HEAL, RESOLVE_AUTO_RESUME})


def outcome_of(incident: PrinterIncident) -> str:
    """Which :data:`OUTCOMES` bucket this row is in. Pure; total over every row shape.

    ``escalated_at`` is the human axis: once a page went out, the close — whatever
    produced it — had a human in the loop (they refilled, resumed, fixed the path,
    stopped the print, or pressed Recover). Only a row that closed WITHOUT paging can
    be the farm's own recovery, and only when the close came from the farm's own act
    (:data:`_FARM_CLOSES`) or, for a plate-vision trip, from the terminal of the stop
    the farm itself sent — the first-trip re-check that requeues without a page.
    Everything else that closed unpaged closed on evidence nobody produced (a wire
    edge, a job ending, a restart) and is counted honestly as neither.
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
    return OUTCOME_RESOLVED_UNPAGED


def held_seconds(incident: PrinterIncident, now: datetime) -> float:
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


def summary(rows: list[PrinterIncident]) -> dict:
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


async def list_recent(
    db: AsyncSession,
    *,
    since: datetime,
    kind: str | None = None,
    printer_id: int | None = None,
    limit: int = 200,
) -> list[PrinterIncident]:
    """Rows opened at or after ``since``, newest first, optionally narrowed."""
    stmt = select(PrinterIncident).where(PrinterIncident.created_at >= since)
    if kind is not None:
        stmt = stmt.where(PrinterIncident.kind == kind)
    if printer_id is not None:
        stmt = stmt.where(PrinterIncident.printer_id == printer_id)
    # id DESC is the tiebreak, not decoration: rows opened in one push share a stamp.
    stmt = stmt.order_by(PrinterIncident.created_at.desc(), PrinterIncident.id.desc()).limit(limit)
    return list((await db.execute(stmt)).scalars().all())


async def list_overlapping(db: AsyncSession, *, start: datetime, end: datetime) -> list[PrinterIncident]:
    """Every row whose hold INTERSECTS ``[start, end)``, oldest first, uncapped.

    The question a timeline asks, which :func:`list_recent` cannot answer: that one
    filters on ``created_at >= since``, so an incident that opened before the window
    and was still holding right through it — the longest outages, exactly the ones a
    downtime figure must not miss — is invisible to it. Here an incident is the
    half-open interval ``[created_at, resolved_at or +inf)`` and the test is the
    standard overlap: it began before the window ended, and it had not ended when the
    window began.

    No ``limit``: a cap on a row set a caller is about to SUM would silently
    under-report, which is the failure mode :func:`list_recent`'s cap is acceptable
    for (a page a human reads) and this one's would not be. The window is the bound.

    ``(created_at, id)`` ordering so a sweep over the rows is deterministic —
    incidents opened in one push share a stamp. Plain SQL comparisons on two
    columns, valid on SQLite and Postgres alike.
    """
    stmt = (
        select(PrinterIncident)
        .where(PrinterIncident.created_at < end)
        .where(or_(PrinterIncident.resolved_at.is_(None), PrinterIncident.resolved_at > start))
        .order_by(PrinterIncident.created_at, PrinterIncident.id)
    )
    return list((await db.execute(stmt)).scalars().all())


async def rehydrate(db: AsyncSession) -> int:
    """Rebuild the projection cache from the DB. Returns the number of open rows.

    Called at startup, after the stale-incident sweep, so a restart mid-hold still
    renders the chip and still answers "is this printer owned".
    """
    _open_cache.clear()
    rows = await all_open(db)
    for incident in rows:
        _open_cache.setdefault(incident.printer_id, {})[incident.id] = _payload(incident)
    return len(rows)
