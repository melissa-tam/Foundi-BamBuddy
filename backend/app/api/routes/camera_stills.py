"""``GET /printers/{id}/camera/still`` — the printer's CACHED camera still (``services/camera_stills``).

For surfaces that show many printers at once (the touch page's tiles), under the owner's VIEW policy
(today's camera behaviour — the snapshot route's): every caller gets the still
the owner already holds, and the owner captures a new one only when its cadence says one is due
(1 → 5 → 15 min while nothing about the printer changes, back to 1 min on a change). So the captures
a printer sees do not grow with the number of browsers polling it.

Response: ``200 image/jpeg`` with ``X-Captured-At`` (ISO-8601 UTC: when the frame was taken),
``X-Next-Capture-At`` (when the owner next refreshes it while nothing changes; absent when unknown)
and an ``ETag`` naming the frame; a request carrying that ETag in ``If-None-Match`` gets ``304`` with
no body. ``404`` unknown printer; ``503`` no still could be had (camera off, capture failed, or a live
stream with no fresh frame). Camera read permission.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.printer import Printer
from backend.app.models.user import User
from backend.app.services import camera_stills

router = APIRouter(prefix="/printers", tags=["camera"])


@router.get("/{printer_id}/camera/still")
async def camera_still(
    printer_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.CAMERA_VIEW),
):
    """The printer's cached camera still, refreshed only when due (module docstring)."""
    printer = await db.get(Printer, printer_id)
    if printer is None:
        raise HTTPException(status_code=404, detail="Printer not found")
    still = await camera_stills.latest_still(
        printer_id, policy=camera_stills.VIEW, source=camera_stills.CameraSource.of(printer)
    )
    if isinstance(still, camera_stills.NoStill):
        raise HTTPException(status_code=503, detail="No camera still available")

    etag = f'"{printer_id}-{int(still.captured_ts * 1000)}"'
    headers = {
        "ETag": etag,
        "X-Captured-At": still.captured_at.isoformat(),
        "Cache-Control": "no-cache",
    }
    next_at = camera_stills.next_capture_at(printer_id)
    if next_at is not None:
        headers["X-Next-Capture-At"] = next_at.isoformat()
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=still.jpeg, media_type="image/jpeg", headers=headers)
