"""REST API for the reused-tag re-spool (fork farm feature).

``POST /api/v1/inventory/spools/respool`` performs the same core operation the
Tier-2 auto path and the Tier-3 prompt confirm run: dispose the spent donor,
mint a fresh full third-party spool on the reused Bambu tag, re-assign the slot
and release low-spool-staged farm items. Covers dismissed prompts and the
PrintersPage "Re-spool tag…" tray action.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.api.routes.settings import get_setting
from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.spool import Spool
from backend.app.models.user import User
from backend.app.schemas.spool import SpoolResponse
from backend.app.services.spool_respool import RespoolError, respool_tag

router = APIRouter(prefix="/inventory", tags=["inventory"])


class RespoolRequest(BaseModel):
    printer_id: int
    ams_id: int
    tray_id: int
    brand: str = Field(min_length=1, max_length=100)
    label_weight: int | None = None
    cost_per_kg: float | None = None
    note: str | None = None


@router.post("/spools/respool", response_model=SpoolResponse)
async def respool_spool(
    req: RespoolRequest,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.INVENTORY_UPDATE),
):
    """Re-spool a reused Bambu tag onto a fresh full third-party spool.

    409 in Spoolman mode (Spoolman owns the lifecycle); 404 printer not
    connected; 400 empty slot / no readable tray / no valid RFID tag; 409 when
    the tray_uuid's sibling tag already lives on another re-spooled roll.
    """
    spoolman_enabled = await get_setting(db, "spoolman_enabled")
    if spoolman_enabled and spoolman_enabled.lower() == "true":
        raise HTTPException(
            409,
            "Re-spool is unavailable in Spoolman mode — Spoolman owns the spool lifecycle.",
        )

    try:
        spool = await respool_tag(
            db,
            printer_id=req.printer_id,
            ams_id=req.ams_id,
            tray_id=req.tray_id,
            brand=req.brand,
            label_weight=req.label_weight,
            cost_per_kg=req.cost_per_kg,
            note=req.note,
        )
    except RespoolError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc

    result = await db.execute(select(Spool).options(selectinload(Spool.k_profiles)).where(Spool.id == spool.id))
    return result.scalar_one()
