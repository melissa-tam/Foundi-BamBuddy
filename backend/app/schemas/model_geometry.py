"""Pydantic schemas for the printer model-geometry registry API."""

from datetime import datetime

from pydantic import BaseModel, Field


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
    validated: bool
    notes: str | None
    updated_at: datetime

    class Config:
        from_attributes = True


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
    validated: bool | None = None
    notes: str | None = None
