"""Read API for the durable HMS vocabulary (``models.hms_event``).

``GET /printers/{printer_id}/hms-events`` answers the question every incident audit
opened with and could not answer: *what has this printer actually said?* Codes with no
catalog description never notify and, before ``hms_event``, never persisted either, so
the vocabulary had to be reconstructed from notification side effects.

Read-only by construction — the writer is main's HMS block, the pruner is main's
lifespan hygiene. Its own module (not the 159 KB ``printers.py``) per the fork's
large-route-file convention; permission ``PRINTERS_READ``, the same gate the printer
status read uses, because this is printer telemetry and nothing more.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.hms_event import HMSEvent
from backend.app.models.printer import Printer
from backend.app.services.hms_errors import hms_short_code, lookup_description_any

router = APIRouter(prefix="/printers", tags=["printers"])

# Page bounds. The default is a screenful of vocabulary; the ceiling exists so a client
# cannot ask for the whole table in one request.
_DEFAULT_LIMIT = 100
_MAX_LIMIT = 500


class HMSEventRow(BaseModel):
    """One printer's record of one HMS code.

    ``short_code`` and ``description`` are DERIVED at read time through the same
    one-origin helpers the notification path uses (``hms_short_code``,
    ``lookup_description_any``) — never stored, so a catalog update is reflected the
    next time the row is read. ``description`` is None exactly for the codes this
    endpoint exists for: the ones the vendored catalog does not know.
    """

    full_code: str
    short_code: str
    description: str | None
    attr: int
    code: int
    severity: int | None
    first_seen: datetime
    last_seen: datetime
    count: int


@router.get("/{printer_id}/hms-events", response_model=list[HMSEventRow])
async def list_hms_events(
    printer_id: int,
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT, description="Rows per page (1-500)"),
    offset: int = Query(default=0, ge=0, description="Rows to skip"),
    db: AsyncSession = Depends(get_db),
    _=RequirePermissionIfAuthEnabled(Permission.PRINTERS_READ),
) -> list[HMSEventRow]:
    """The printer's HMS codes, most recently seen first."""
    printer = (await db.execute(select(Printer.id).where(Printer.id == printer_id))).scalar_one_or_none()
    if printer is None:
        raise HTTPException(404, "Printer not found")

    rows = (
        (
            await db.execute(
                select(HMSEvent)
                .where(HMSEvent.printer_id == printer_id)
                # id DESC is the tiebreak, not decoration: every code of one push shares
                # a last_seen stamp, so without it the page boundary is unstable.
                .order_by(HMSEvent.last_seen.desc(), HMSEvent.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )

    return [
        HMSEventRow(
            full_code=row.full_code,
            short_code=hms_short_code(row.attr, row.code),
            description=lookup_description_any(row.attr, row.code),
            attr=row.attr,
            code=row.code,
            severity=row.severity,
            first_seen=row.first_seen,
            last_seen=row.last_seen,
            count=row.count,
        )
        for row in rows
    ]
