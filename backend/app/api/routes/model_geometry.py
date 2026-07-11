"""REST API for the printer model-geometry registry (Phase 2).

Makes the eject bed/envelope DB-config (red line #3): geometry that used to be
two in-code dicts is now editable per model without a code change, and the
``validated`` flag gates production dispatch. GET is eject-read; PUT is
eject-update and WARNING-logs every field it changes (envelope edits move the
toolhead — they must be auditable).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.printer_model_geometry import PrinterModelGeometry
from backend.app.models.user import User
from backend.app.schemas.model_geometry import (
    ModelGeometryListResponse,
    ModelGeometryResponse,
    ModelGeometryUpdate,
)
from backend.app.services.eject.generator import SWEEP_BAND_MIN_WIDTH_MM
from backend.app.utils.printer_models import canon_model

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/model-geometry", tags=["model-geometry"])


@router.get("", response_model=ModelGeometryListResponse)
@router.get("/", response_model=ModelGeometryListResponse)
async def list_model_geometry(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.EJECT_PROFILES_READ),
):
    result = await db.execute(select(PrinterModelGeometry).order_by(PrinterModelGeometry.model_key))
    rows = list(result.scalars().all())
    return ModelGeometryListResponse(
        geometries=[ModelGeometryResponse.model_validate(row) for row in rows],
        sweep_band_min_width_mm=SWEEP_BAND_MIN_WIDTH_MM,
    )


@router.put("/{model_key}", response_model=ModelGeometryResponse)
async def update_model_geometry(
    model_key: str,
    data: ModelGeometryUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.EJECT_PROFILES_UPDATE),
):
    """Update an existing model's geometry. 404 if the (canonicalised) key has no
    row — the registry is seeded; this endpoint edits, it does not create models.

    Every changed field is WARNING-logged old→new so envelope/validation changes
    (which move the toolhead / unlock production) leave an audit trail.
    """
    key = canon_model(model_key)
    if key is None:
        raise HTTPException(status_code=404, detail="Unknown model key")
    result = await db.execute(select(PrinterModelGeometry).where(PrinterModelGeometry.model_key == key))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"No geometry row for model {key!r}")

    updates = data.model_dump(exclude_unset=True)
    changes: list[str] = []
    for field, new_value in updates.items():
        old_value = getattr(row, field)
        if old_value != new_value:
            changes.append(f"{field}: {old_value!r} -> {new_value!r}")
            setattr(row, field, new_value)

    if changes:
        logger.warning("model-geometry %s updated: %s", key, "; ".join(changes))
        await db.commit()
        await db.refresh(row)

    return ModelGeometryResponse.model_validate(row)
