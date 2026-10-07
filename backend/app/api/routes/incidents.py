"""Read API for the equipment-fault ledger (``models.printer_incident``).

``GET /incidents`` answers the question every recovery audit opened with and could
only reconstruct from logs: *what has the farm recovered from by itself, what did a
human have to finish, and what is holding right now?* On 2026-09-11 the zero-human
tally (9 of 122 holds) took a read-only SELECT over the farm's SSH lane plus a day
of log reading; the derivation is now ONE table in ``printer_incidents.outcome_of``
and this route is its query surface — the Stats page's Faults tab, and curl triage.

Read-only by construction — the writers are the recovery machine and the
pause-recovery lane. Its own module (not the large ``printers.py``) per the fork's
large-route-file convention and ``routes/hms.py``'s precedent; permission
``STATS_READ``, the gate of the Statistics page it feeds (every seeded group that held
the former ``PRINTERS_READ`` gate holds it too). The window, the filters, the tally and
the recurring lines are composed by ``printer_incidents.ledger_page``; this route
validates the request and maps the page onto the wire.
"""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.printer_incident import PrinterIncident
from backend.app.schemas.incidents import (
    IncidentRow,
    IncidentsResponse,
    IncidentSummary,
    RecurringLine,
    RecurringWorstPrinter,
)
from backend.app.schemas.printer import PrinterMessageInfo
from backend.app.services import printer_incidents
from backend.app.services.hms_errors import PrinterMessage
from backend.app.utils import site_time

router = APIRouter(prefix="/incidents", tags=["incidents"])

# Page bounds. The ceiling exists so a client cannot ask for the whole table in one
# request; the Faults tab's "All" asks for exactly this many and says so when ``total``
# exceeds it.
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 1000


def _message(message: PrinterMessage) -> PrinterMessageInfo:
    return PrinterMessageInfo(**message.as_payload())


def _row(incident: PrinterIncident, printer_name: str | None, *, now: datetime, recurring: bool) -> IncidentRow:
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
        utc_offset_minutes=site_time.offset_minutes(incident.created_at),
        printer_messages=[_message(message) for message in printer_incidents.printer_messages_of(incident)],
        recurring=recurring,
    )


def _line(line: printer_incidents.RecurringSignature) -> RecurringLine:
    return RecurringLine(
        scope=line.scope,
        printer_id=line.printer_id,
        printer_name=line.printer_name,
        kind=line.kind,
        code=line.code,
        printer_message=_message(line.printer_message) if line.printer_message is not None else None,
        holds=line.holds,
        days=line.days,
        held_s=line.held_s,
        last_at=line.last_at,
        fleet_median=line.fleet_median,
        printers_affected=line.printers_affected,
        roster_size=line.roster_size,
        utc_offset_minutes=line.utc_offset_minutes,
        worst=[
            RecurringWorstPrinter(printer_id=worst.printer_id, printer_name=worst.printer_name, holds=worst.holds)
            for worst in line.worst
        ],
    )


@router.get("", response_model=IncidentsResponse)
async def list_incidents(
    date_from: date | None = Query(default=None, description="First SITE date, inclusive (absent: unbounded)"),
    date_to: date | None = Query(default=None, description="Last SITE date, inclusive (absent: unbounded)"),
    kind: str | None = Query(default=None, description="One incident kind"),
    printer_id: int | None = Query(default=None, description="One printer"),
    outcome: str | None = Query(default=None, description="One derived outcome"),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT, description="Rows per page (1-1000)"),
    offset: int = Query(default=0, ge=0, description="Rows to skip, newest first"),
    db: AsyncSession = Depends(get_db),
    _=RequirePermissionIfAuthEnabled(Permission.STATS_READ),
) -> IncidentsResponse:
    """Incidents OPENED in the site-date window, newest first, with the window's tally.

    ``summary`` and ``total`` are WINDOW-WIDE over the filtered rows, not over the page:
    ``total`` + ``offset`` paging make every row behind the tally reachable, so the figure
    stays checkable row by row (what the 2026-09-11 "summary over the same rows the page
    returns" ruling protected) without a client fetching the whole window to read it.
    ``summary`` honours the kind and printer filters and ignores ``outcome`` (an outcome
    filter would collapse the auto-recovered share to 0 % or 100 %); ``total`` honours all
    three. ``recurring`` is computed over the UNFILTERED window, so no filter changes what
    counts as recurring.
    """
    if kind is not None and not printer_incidents.is_known_kind(kind):
        raise HTTPException(422, f"unknown incident kind {kind!r}")
    if outcome is not None and outcome not in printer_incidents.OUTCOMES:
        raise HTTPException(422, f"unknown incident outcome {outcome!r}")
    if date_from is not None and date_to is not None and date_to < date_from:
        raise HTTPException(422, f"window ends before it starts: {date_from}..{date_to}")

    page = await printer_incidents.ledger_page(
        db,
        date_from=date_from,
        date_to=date_to,
        kind=kind,
        printer_id=printer_id,
        outcome=outcome,
        limit=limit,
        offset=offset,
    )
    return IncidentsResponse(
        date_from=date_from,
        date_to=date_to,
        tz_name=site_time.site_zone_name(),
        total=page.total,
        limit=limit,
        offset=offset,
        summary=IncidentSummary(**page.summary),
        recurring=[_line(line) for line in page.recurring],
        items=[
            _row(
                row,
                page.printer_names.get(row.printer_id),
                now=page.now,
                recurring=row.id in page.recurring_ids,
            )
            for row in page.rows
        ],
    )
