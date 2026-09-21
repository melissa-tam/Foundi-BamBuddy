"""Read API for the equipment-fault ledger (``models.printer_incident``).

``GET /incidents`` answers the question every recovery audit opened with and could
only reconstruct from logs: *what has the farm recovered from by itself, what did a
human have to finish, and what is holding right now?* On 2026-09-11 the zero-human
tally (9 of 122 holds) took a read-only SELECT over the farm's SSH lane plus a day
of log reading; the derivation is now ONE table in ``printer_incidents.outcome_of``
and this route is its query surface.

Read-only by construction — the writers are the recovery machine and the
pause-recovery lane. Its own module (not the 159 KB ``printers.py``) per the fork's
large-route-file convention and ``routes/hms.py``'s precedent; permission
``PRINTERS_READ``, the same gate the printer status read uses, because this is
printer telemetry and nothing more.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.printer import Printer
from backend.app.models.printer_incident import PrinterIncident
from backend.app.services import printer_incidents

router = APIRouter(prefix="/incidents", tags=["incidents"])

# Page bounds. The default window is the same one the 2026-09-11 audit used; the
# ceiling exists so a client cannot ask for the whole table in one request.
_DEFAULT_WINDOW = timedelta(days=30)
_DEFAULT_LIMIT = 200
_MAX_LIMIT = 1000


class IncidentRow(BaseModel):
    """One equipment-fault record, with its DERIVED outcome.

    ``outcome``, ``resolution_class``, ``external``, ``slot_desc`` and ``held_s``
    are computed at read time through the store's own one-origin helpers — never
    stored — so a change to the derivation table is reflected the next time the row
    is read.
    """

    id: int
    printer_id: int
    printer_name: str | None
    job_id: str
    item_id: int | None
    kind: str
    external: bool
    code: str
    codes: str
    slot_desc: str | None
    status: str
    outcome: str
    resolution_class: str
    created_at: datetime
    escalated_at: datetime | None
    resolved_at: datetime | None
    resolve_source: str | None
    held_s: float


class IncidentSummary(BaseModel):
    """The tally the audit had to hand-count: how many holds the farm ended alone.

    ``total`` counts EQUIPMENT FAULTS. ``declared`` counts the planned holds an
    operator opened by hand (maintenance mode), which are excluded from ``total``,
    ``zero_human`` and ``by_outcome`` — see ``printer_incidents.summary`` — and appear
    in ``by_kind`` like any other row. The two add up to the rows the page returned.
    """

    since: datetime
    total: int
    zero_human: int
    declared: int
    by_outcome: dict[str, int]
    by_kind: dict[str, dict[str, int]]


class IncidentsResponse(BaseModel):
    summary: IncidentSummary
    items: list[IncidentRow]


def _row(incident: PrinterIncident, printer_name: str | None, *, now: datetime) -> IncidentRow:
    external = printer_incidents.row_external(incident)
    return IncidentRow(
        id=incident.id,
        printer_id=incident.printer_id,
        printer_name=printer_name,
        job_id=incident.job_id,
        item_id=incident.item_id,
        kind=incident.kind,
        external=external,
        code=incident.code,
        codes=incident.codes,
        slot_desc=printer_incidents.slot_desc(incident),
        status=incident.status,
        outcome=printer_incidents.outcome_of(incident),
        resolution_class=printer_incidents.resolution_class(incident.kind, external=external),
        created_at=incident.created_at,
        escalated_at=incident.escalated_at,
        resolved_at=incident.resolved_at,
        resolve_source=incident.resolve_source,
        held_s=printer_incidents.held_seconds(incident, now),
    )


@router.get("", response_model=IncidentsResponse)
async def list_incidents(
    since: datetime | None = Query(
        default=None, description="Rows opened at or after this instant (default: 30 days ago)"
    ),
    kind: str | None = Query(default=None, description="One incident kind"),
    printer_id: int | None = Query(default=None, description="One printer"),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT, description="Rows per page (1-1000)"),
    db: AsyncSession = Depends(get_db),
    _=RequirePermissionIfAuthEnabled(Permission.PRINTERS_READ),
) -> IncidentsResponse:
    """Incidents opened since ``since``, newest first, with the outcome tally.

    The summary is computed over the SAME rows the page returns, so a client that
    wants the tally for a longer window asks with a larger ``limit`` — a summary
    over rows the page cannot show would be a number nobody can check.
    """
    if kind is not None and not printer_incidents.is_known_kind(kind):
        raise HTTPException(422, f"unknown incident kind {kind!r}")
    now = datetime.utcnow()
    window_start = since or (now - _DEFAULT_WINDOW)

    rows = await printer_incidents.list_recent(db, since=window_start, kind=kind, printer_id=printer_id, limit=limit)
    printer_ids = {row.printer_id for row in rows}
    names: dict[int, str] = {}
    if printer_ids:
        result = await db.execute(select(Printer.id, Printer.name).where(Printer.id.in_(printer_ids)))
        names = dict(result.all())

    tally = printer_incidents.summary(rows)
    return IncidentsResponse(
        summary=IncidentSummary(since=window_start, **tally),
        items=[_row(row, names.get(row.printer_id), now=now) for row in rows],
    )
