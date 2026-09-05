"""The durable AMS-incident store — open, close and read one printer's fault hold.

``spool_recovery`` owns the *machine* (what to do about a fault); this module owns
its *record* (:class:`~backend.app.models.printer_incident.PrinterIncident`) —
creation under the one-open-per-printer rule, the close transitions, the queries the
watchdogs ask, and the in-memory projection the WebSocket payload reads.

Why a separate module rather than more of ``spool_recovery``: three unrelated
callers need incident FACTS without wanting the state machine — ``farm_stall``
(hourly reminders + "is this pause owned"), ``printer_manager.printer_state_to_dict``
(the printer-card chip, a SYNC function that may not touch the DB), and main's
lifespan (startup rehydration). Routing those through the machine would drag the
whole recovery import graph — and its printer_manager dependency — into a WS
serializer.

**Exclusivity is the database's job.** ``ux_printer_incident_open`` is a partial
UNIQUE index over ``printer_id WHERE resolved_at IS NULL``, so a second open
incident for one printer cannot exist even if two callbacks race: the loser gets an
IntegrityError, which :func:`open_new` reports as "someone else owns it" instead of
crashing. The pre-WS2b exclusivity (a process-lifetime ``_active_tasks`` dict) was
erased by every restart while the standing HMS came straight back.

**The snapshot cache** (:func:`snapshot`) mirrors the open row per printer so the
~1 Hz WS serializer never queries. It is a projection, never a source: every write
path here refreshes it, and :func:`rehydrate` rebuilds it from the DB at startup.
A cache miss renders no chip — it can never invent a hold.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import func as sa_func, select
from sqlalchemy.exc import IntegrityError

from backend.app.models.printer_incident import (
    KIND_JAM,
    KIND_PHYSICAL,
    KIND_PLATE_VISION,
    KIND_POWER_LOSS,
    KIND_RUNOUT,
    KIND_Z_REFERENCE_LOST,
    RESOLVES_ON,
    STATUS_ABORTED,
    STATUS_ESCALATED,
    STATUS_RECOVERING,
    STATUS_RESOLVED,
    PrinterIncident,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# The statuses that mean "closed" — both stamp ``resolved_at`` (see the model
# docstring's lifecycle table), so the open/closed question is asked of that column
# and never of this tuple.
CLOSED_STATUSES: tuple[str, ...] = (STATUS_RESOLVED, STATUS_ABORTED)

# printer_id -> the WS/REST projection of that printer's OPEN incident. Rebuilt from
# the DB at startup (:func:`rehydrate`) and maintained by every write below.
_open_cache: dict[int, dict] = {}


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


def resolves_on_operator(kind: str) -> bool:
    """Does an incident of ``kind`` end ONLY when a human acts?

    The one reading of the model's ``RESOLVES_ON`` table, so the ``"operator"`` literal
    is spelled once and the two close paths that must honour it (``on_job_terminal`` and
    ``sweep_open_incidents``) cannot drift apart. An unregistered kind answers False —
    the pre-existing behaviour for every AMS kind, and the safe direction: a hold that
    closes too readily is visible, one that never closes blocks the printer forever.
    """
    return RESOLVES_ON.get(kind) == "operator"


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


def _slot_desc(incident: PrinterIncident) -> str | None:
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
        # Function-level import: spool_recovery imports THIS module at module level,
        # and its ``runout_slot_desc`` is the one origin for the wording (the
        # escalation, the reminder and this chip must never disagree).
        from backend.app.services.spool_recovery import runout_slot_desc

        return runout_slot_desc(incident.slot_global_tray)
    from backend.app.services.hms_errors import classify_short_code

    verdict = classify_short_code(incident.code or "")
    if verdict is not None and verdict.external:
        return "external"
    return None


def _payload(incident: PrinterIncident) -> dict:
    """The projection the printer card renders."""
    return {
        "kind": incident.kind,
        "status": incident.status,
        "slot_desc": _slot_desc(incident),
        "created_at": incident.created_at.isoformat() if incident.created_at else None,
    }


def snapshot(printer_id: int | None) -> dict | None:
    """The printer's OPEN incident as a wire dict, or ``None``.

    Pure and DB-free — this is read by ``printer_state_to_dict`` on every status
    broadcast. A stale-empty cache under-reports (no chip) and never invents a hold.
    """
    if not printer_id:
        return None
    return _open_cache.get(printer_id)


async def get_open(db: AsyncSession, printer_id: int) -> PrinterIncident | None:
    """The printer's OPEN incident row (``resolved_at IS NULL``), or None."""
    result = await db.execute(
        select(PrinterIncident)
        .where(PrinterIncident.printer_id == printer_id)
        .where(PrinterIncident.resolved_at.is_(None))
        .limit(1)
    )
    return result.scalar_one_or_none()


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
    """Create the printer's open incident, or ``None`` when one already exists.

    Two guards, deliberately both: a pre-check (the ordinary case, so the common path
    logs a reason instead of raising) and the partial unique index (the race). A
    caller that gets ``None`` must treat the printer as already owned.
    """
    if await get_open(db, printer_id) is not None:
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
        # The partial unique index fired: another callback opened this printer's
        # incident between the pre-check and the flush. Not an error — the other
        # actor owns it.
        await db.rollback()
        logger.info(
            "printer_incidents: printer %s already has an open incident (index race) — %s %s not opened",
            printer_id,
            kind,
            code,
        )
        return None
    _open_cache[printer_id] = _payload(incident)
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
    _open_cache[incident.printer_id] = _payload(incident)
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
    if _open_cache.get(incident.printer_id) is not None:
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
    db: AsyncSession, printer_id: int, *, source: str, status: str = STATUS_RESOLVED
) -> PrinterIncident | None:
    """Close whatever incident this printer has open. ``None`` when it has none."""
    incident = await get_open(db, printer_id)
    if incident is None:
        return None
    return await close(db, incident.id, status=status, source=source)


async def rehydrate(db: AsyncSession) -> int:
    """Rebuild the projection cache from the DB. Returns the number of open rows.

    Called at startup, after the stale-incident sweep, so a restart mid-hold still
    renders the chip and still answers "is this printer owned".
    """
    _open_cache.clear()
    rows = await all_open(db)
    for incident in rows:
        _open_cache[incident.printer_id] = _payload(incident)
    return len(rows)
