"""Read API for the measured shop air and the eject line (``services/eject/shop_air``).

Thin by construction: the handler names the permission and maps the owner's ONE read,
``shop_air.current_line``, onto the response — the same read the cooldown watch makes at
arm and the hot-bed gates make on a click, so the Settings readout can never show a line
the farm is not using. Permission ``SETTINGS_READ``: the readout lives on the Settings
page beside the margin it is derived from.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.user import User
from backend.app.schemas.shop_air import ShopAirResponse
from backend.app.services.eject import shop_air

router = APIRouter(prefix="/shop-air", tags=["shop-air"])


@router.get("", response_model=ShopAirResponse)
@router.get("/", response_model=ShopAirResponse)
async def read_shop_air(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_READ),
) -> ShopAirResponse:
    line = await shop_air.current_line(db)
    return ShopAirResponse(
        value_c=line.shop.value_c,
        as_of=line.shop.as_of,
        basis=line.shop.basis,
        printers=line.shop.printers,
        margin_c=line.margin_c,
        eject_line_c=line.line_c,
    )
