"""Read API for fleet availability and throughput (``services.fleet_metrics``).

Three questions, three routes: what is the fleet doing RIGHT NOW, what did it do over
a window, and what exactly was one printer doing inside one cell of that window. All
three answer through the same classifier, so a printer cannot read one way on the live
tile and another way in its own history.

Thin by construction — these handlers settle the window's defaults, name the
permission and translate the reader's refusals into status codes. Every figure,
every calendar cut and every division is the service's; a route that did arithmetic
would be a second definition of *uptime* sitting where nobody tests it.

Its own module (not the 159 KB ``printers.py``) per the fork's large-route-file
convention, and named for the service it exposes — ``routes/metrics.py`` is the
Prometheus exporter and answers a different audience entirely. Permission
``STATS_READ``, the same gate ``/archives/stats`` reads under: this is farm
statistics and nothing more.
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.user import User
from backend.app.schemas.fleet_metrics import (
    FleetOverview,
    FleetStatus,
    PrinterIntervalsResponse,
)
from backend.app.services import fleet_metrics as fleet_metrics_service
from backend.app.services.fleet_metrics import InvalidWindow, PrinterUnknown, WindowTooLong
from backend.app.utils.site_time import Bucket, site_today

router = APIRouter(prefix="/fleet-metrics", tags=["fleet-metrics"])

# The default history window, in SITE DAYS. Thirty inclusive dates ending today, which
# is 29 days before the last one — counted on the calendar, never as a timedelta over
# an instant, because a window is a range of the site's dates and a transition day is
# still one date.
_DEFAULT_WINDOW_DAYS = 30


@router.get("/status", response_model=FleetStatus)
async def read_status(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.STATS_READ),
) -> FleetStatus:
    """Every printer's class right now, the fleet counts, and what "all time" means.

    Live only, and deliberately separate from ``/overview``: this is the polled half
    of the tab, and a reader must never wait on a year-long sweep to learn that a
    printer is down. ``recording_since`` and ``history_since`` come back with it so
    the client can say how far back its own range picker is allowed to reach.
    """
    return await fleet_metrics_service.status_now(db)


@router.get("/overview", response_model=FleetOverview)
async def read_overview(
    date_from: date | None = Query(
        default=None,
        description="First site date, inclusive (default: 29 days before date_to)",
    ),
    date_to: date | None = Query(
        default=None,
        description="Last site date, inclusive (default: the site's today)",
    ),
    bucket: Bucket | None = Query(
        default=None,
        description="hour | day | week (default: chosen from the window length and echoed)",
    ),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.STATS_READ),
) -> FleetOverview:
    """Every projection over one window, from one timeline.

    The window is INCLUSIVE SITE DATES. Each bound has its own default and they are
    applied in that order, so supplying one does not change the other's rule:
    ``date_to`` defaults to the site's today, and ``date_from`` to 29 days before
    whatever ``date_to`` resolved to — a 30-day window either way.

    ``bucket`` is optional because the server owns the choice: omitted, the service
    picks it from the window length and echoes what it used, and the client lays its
    columns out from the echo rather than from a guess that could disagree.

    Refused with 422 when the window ends before it starts, exceeds the sweep ceiling,
    or asks for hour buckets over more days than one request may cut that finely.
    """
    resolved_to = date_to if date_to is not None else site_today()
    resolved_from = date_from if date_from is not None else resolved_to - timedelta(days=_DEFAULT_WINDOW_DAYS - 1)
    try:
        return await fleet_metrics_service.overview(
            db,
            date_from=resolved_from,
            date_to=resolved_to,
            bucket=bucket,
        )
    except (InvalidWindow, WindowTooLong) as error:
        raise HTTPException(422, str(error)) from error


@router.get("/printers/{printer_id}/intervals", response_model=PrinterIntervalsResponse)
async def read_printer_intervals(
    printer_id: int = Path(description="The printer whose intervals to list"),
    date_from: date | None = Query(default=None, description="First site date, inclusive (default: the site's today)"),
    date_to: date | None = Query(default=None, description="Last site date, inclusive (default: the site's today)"),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.STATS_READ),
) -> PrinterIntervalsResponse:
    """One printer's classified intervals — what a matrix cell was summed from.

    Both bounds default to the site's today, because this is the drill-down a reader
    opens on a cell rather than a series they browse: the useful default is one day,
    and the range is capped short by the service for the same reason.

    404 when no printer, span or incident in the range names the id — history about a
    DELETED printer is still readable, so a 404 here means the id is unknown to the
    farm entirely, not merely that the machine is gone.
    """
    today = site_today()
    try:
        return await fleet_metrics_service.printer_intervals(
            db,
            printer_id,
            date_from=date_from if date_from is not None else today,
            date_to=date_to if date_to is not None else today,
        )
    except PrinterUnknown as error:
        raise HTTPException(404, str(error)) from error
    except (InvalidWindow, WindowTooLong) as error:
        raise HTTPException(422, str(error)) from error
