"""Response shape of ``GET /api/v1/shop-air`` — the measured shop air and the eject line."""

from typing import Literal

from pydantic import BaseModel, Field

from backend.app.schemas.print_queue import UTCDatetime


class ShopAirResponse(BaseModel):
    """The shop air right now, how it is known, and the eject line derived from it.

    ``value_c`` / ``eject_line_c`` are None exactly when ``basis`` is ``unknown`` (no
    at-rest sample in the last 7 days). ``as_of`` is the newest contributing sample for
    ``fresh`` and the carried sample's minute for ``carried`` (UTC). ``printers`` is how
    many printers the value rests on. ``margin_c`` is ``farm_cooldown_margin_c``.
    """

    value_c: float | None
    as_of: UTCDatetime = None
    basis: Literal["fresh", "carried", "unknown"]
    printers: int = Field(ge=0)
    margin_c: float
    eject_line_c: float | None
