"""Pydantic schemas for the printer model-geometry registry API."""

from datetime import datetime

from pydantic import BaseModel, Field, computed_field

from backend.app.utils.printer_models import is_bedslinger_model


class ModelGeometryResponse(BaseModel):
    """One registry row as returned by GET /model-geometry."""

    model_key: str
    bed_x: float
    bed_y: float
    env_x_min: float
    env_x_max: float
    env_y_min: float
    env_y_max: float
    max_part_height_mm: float
    z_travel_mm: float | None
    validated: bool
    notes: str | None
    updated_at: datetime

    class Config:
        from_attributes = True

    @computed_field  # type: ignore[prop-decorator]
    @property
    def bedslinger(self) -> bool:
        """Whether this model is a bed-slinger (bed fixed in Z — the gantry carries
        Z). Derived at response construction from ``model_key`` via
        :func:`is_bedslinger_model`; it is NOT a DB column and NOT part of
        :class:`ModelGeometryUpdate`, so a PUT can neither set nor persist it."""
        return is_bedslinger_model(self.model_key)


class ModelGeometryListResponse(BaseModel):
    """GET /model-geometry envelope.

    Carries ``sweep_band_min_width_mm`` alongside the rows so the client reads the
    server's minimum-sweep-band constant from the API instead of hardcoding a copy.
    """

    geometries: list[ModelGeometryResponse]
    sweep_band_min_width_mm: float


class ModelGeometryUpdate(BaseModel):
    """PUT /model-geometry/{model_key} body — every field optional.

    Bed dimensions and the height ceiling must be positive; envelope bounds may be
    negative (Y overhangs run past the bed edge). ``validated`` flips the
    hardware-ladder gate — set True only after the empty-bed dry run + thermal
    cycle for this model has been operator-witnessed.
    """

    bed_x: float | None = Field(default=None, gt=0)
    bed_y: float | None = Field(default=None, gt=0)
    env_x_min: float | None = None
    env_x_max: float | None = None
    env_y_min: float | None = None
    env_y_max: float | None = None
    max_part_height_mm: float | None = Field(default=None, gt=0)
    # Machine bottom for the bed-drop assist; must be positive. An explicit null
    # legitimately clears it (the column is nullable → the assist fails closed).
    z_travel_mm: float | None = Field(default=None, gt=0)
    validated: bool | None = None
    notes: str | None = None
