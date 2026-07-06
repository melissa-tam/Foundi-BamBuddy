"""Pydantic schemas for the SKU catalog API (Phase 2).

Capability facts (nozzle/filament/model/max-Z) on :class:`SkuFileResponse` are
read live from the linked ``LibraryFile.file_metadata`` (with a 3MF-parse
fallback) by the route layer — they are never stored on ``sku_files``.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class SkuFileResponse(BaseModel):
    id: int
    sku_id: int
    library_file_id: int
    library_file_name: str | None = None
    plate_index: int
    units_per_plate: int
    # Read live from LibraryFile.file_metadata (fallback: parse the 3MF).
    nozzle_diameter: float | None = None
    filament_type: str | None = None
    printer_model: str | None = None
    max_z_height: float | None = None


class SkuStatsResponse(BaseModel):
    """Lifetime production stats derived from completed/failed queue items."""

    units_completed: int
    units_failed: int
    plates_completed: int
    plates_failed: int
    success_rate: float
    median_cycle_seconds: float | None = None


class SkuResponse(BaseModel):
    id: int
    code: str
    name: str
    part_number: str | None = None
    notes: str | None = None
    default_eject_profile_id: int | None = None
    files: list[SkuFileResponse] = Field(default_factory=list)
    # Lifetime production stats summary included on list/detail responses.
    stats: SkuStatsResponse | None = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class SkuCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=255)
    part_number: str | None = Field(default=None, max_length=64)
    notes: str | None = None
    default_eject_profile_id: int | None = None


class SkuUpdate(BaseModel):
    """Update a SKU — every field optional."""

    code: str | None = Field(default=None, min_length=1, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    part_number: str | None = Field(default=None, max_length=64)
    notes: str | None = None
    default_eject_profile_id: int | None = None


class SkuFileCreate(BaseModel):
    """Link a library file+plate to a SKU."""

    library_file_id: int
    plate_index: int = Field(default=1, ge=1)
    units_per_plate: int = Field(default=1, ge=1)


class SkuSuggestResponse(BaseModel):
    """Best-effort SKU auto-suggestion parsed from a library file."""

    code: str | None = None
    part_number: str | None = None
    name: str | None = None
    matched_from: str | None = None
