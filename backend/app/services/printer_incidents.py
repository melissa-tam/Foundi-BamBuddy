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

    Idempotent: an already-closed incident is returned untouched, so two racing
    resolvers cannot re-stamp a close time or double-log.
    """
    incident = await db.get(PrinterIncident, incident_id)
    if incident is None:
        return None
    if incident.resolved_at is not None:
        return incident
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
