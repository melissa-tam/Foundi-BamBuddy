"""Pydantic schemas for eject profiles and the preview / dry-run endpoints."""

from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from backend.app.services.eject.generator import SWEEP_BAND_MIN_WIDTH_MM

# The z_offset floor (mm). The sweep must never descend below this — it is the
# minimum safe toolhead gap above the plate. Enforced here and re-checked by the
# validator against generated G-code.
Z_OFFSET_FLOOR_MM = 0.4


def _validate_band_pair(lo: float | None, hi: float | None) -> None:
    """Raise if the (min, max) X sweep band pair is inconsistent or unsafe.

    ``lo``/``hi`` are the two band bounds. Both None (full-width sweep) or both
    set is allowed; exactly one set, an inverted band, or a sub-``SWEEP_BAND_MIN
    _WIDTH_MM`` band is rejected. The upper bound vs bed width is checked at
    generation time (the generator/validator know the printer's bed_x).
    """
    if (lo is None) != (hi is None):
        raise ValueError("sweep_x_min_mm and sweep_x_max_mm must both be set or both be null")
    if lo is not None:
        if lo >= hi:
            raise ValueError("sweep_x_min_mm must be less than sweep_x_max_mm")
        if hi - lo < SWEEP_BAND_MIN_WIDTH_MM:
            raise ValueError(f"sweep band width must be at least {SWEEP_BAND_MIN_WIDTH_MM} mm")


class EjectProfileBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    # Server-side cooldown release threshold: the eject monitor holds the plate
    # gate until the live bed drops here, then dispatches the motion-only eject.
    cooldown_temp_c: float = Field(default=28.0, gt=0, le=100)
    clearance_mm: float = Field(default=10.0, ge=0)
    z_offset_mm: float = Field(default=0.4, gt=0)
    descent_steps: int = Field(default=4, ge=1, le=100)
    x_passes: int = Field(default=11, ge=1, le=100)
    x_margin_mm: float = Field(default=3.0, ge=0)
    front_overhang_mm: float = Field(default=2.0, ge=0)
    back_overhang_mm: float = Field(default=2.0, ge=0)
    eject_speed_mm_min: int = Field(default=3000, gt=0)
    skim_speed_mm_min: int = Field(default=1500, gt=0)
    cooling_fan_assist: bool = True
    # Append the final slow skim pass after the descent sweeps (True = prior
    # behaviour); False pushes exactly once.
    final_skim: bool = True
    max_part_height_mm: float = Field(default=42.0, gt=0)
    # Optional bed-drop release assist (mm). NULL = off; the clearance is kept from
    # the machine bottom (model z_travel_mm) during the drop.
    bed_drop_clearance_mm: float | None = Field(default=None, ge=0, le=200)
    # Optional X sweep sub-band (mm); both null = full-width sweep (default).
    sweep_x_min_mm: float | None = Field(default=None, ge=0)
    sweep_x_max_mm: float | None = Field(default=None, ge=0)
    # Fraction of the part height the descending sweep starts at (1.0 = top).
    sweep_start_frac: float = Field(default=1.0, gt=0, le=1)

    @field_validator("z_offset_mm")
    @classmethod
    def validate_z_offset(cls, v: float) -> float:
        if v < Z_OFFSET_FLOOR_MM:
            raise ValueError(f"z_offset_mm must be at least {Z_OFFSET_FLOOR_MM} mm (plate-safety floor)")
        return v

    @model_validator(mode="after")
    def validate_sweep_band(self) -> "EjectProfileBase":
        _validate_band_pair(self.sweep_x_min_mm, self.sweep_x_max_mm)
        return self


class EjectProfileCreate(EjectProfileBase):
    """Schema for creating an eject profile."""


class EjectProfileUpdate(BaseModel):
    """Schema for updating an eject profile — every field optional."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    cooldown_temp_c: float | None = Field(default=None, gt=0, le=100)
    clearance_mm: float | None = Field(default=None, ge=0)
    z_offset_mm: float | None = Field(default=None, gt=0)
    descent_steps: int | None = Field(default=None, ge=1, le=100)
    x_passes: int | None = Field(default=None, ge=1, le=100)
    x_margin_mm: float | None = Field(default=None, ge=0)
    front_overhang_mm: float | None = Field(default=None, ge=0)
    back_overhang_mm: float | None = Field(default=None, ge=0)
    eject_speed_mm_min: int | None = Field(default=None, gt=0)
    skim_speed_mm_min: int | None = Field(default=None, gt=0)
    cooling_fan_assist: bool | None = None
    final_skim: bool | None = None
    max_part_height_mm: float | None = Field(default=None, gt=0)
    # NULL = off; explicit null in an update clears the bed-drop assist.
    bed_drop_clearance_mm: float | None = Field(default=None, ge=0, le=200)
    sweep_x_min_mm: float | None = Field(default=None, ge=0)
    sweep_x_max_mm: float | None = Field(default=None, ge=0)
    sweep_start_frac: float | None = Field(default=None, gt=0, le=1)

    @field_validator("z_offset_mm")
    @classmethod
    def validate_z_offset(cls, v: float | None) -> float | None:
        if v is not None and v < Z_OFFSET_FLOOR_MM:
            raise ValueError(f"z_offset_mm must be at least {Z_OFFSET_FLOOR_MM} mm (plate-safety floor)")
        return v

    @model_validator(mode="after")
    def validate_sweep_band(self) -> "EjectProfileUpdate":
        # Partial update: only validate the band when at least one bound is
        # explicitly supplied. Touching one bound requires supplying both so the
        # merged row is never left one-sided (both null explicitly clears it).
        fields_set = self.model_fields_set
        if "sweep_x_min_mm" in fields_set or "sweep_x_max_mm" in fields_set:
            if "sweep_x_min_mm" not in fields_set or "sweep_x_max_mm" not in fields_set:
                raise ValueError("sweep_x_min_mm and sweep_x_max_mm must be updated together")
            _validate_band_pair(self.sweep_x_min_mm, self.sweep_x_max_mm)
        return self


class EjectProfileResponse(EjectProfileBase):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class EjectPreviewRequest(BaseModel):
    """Body for the preview and dry-run (download) endpoints.

    Geometry is resolved from EXACTLY ONE of ``printer_id`` (the target printer's
    registered model) or ``model`` (an explicit model key) — supplying both or
    neither is a 422. These are ladder tools, so an UNVALIDATED geometry row is
    allowed; the response carries a warning naming the model.
    """

    library_file_id: int
    plate_index: int = Field(default=1, ge=1)
    printer_id: int | None = None
    model: str | None = None

    @model_validator(mode="after")
    def _exactly_one_target(self) -> "EjectPreviewRequest":
        if (self.printer_id is None) == (self.model is None):
            raise ValueError("provide exactly one of printer_id or model")
        return self


class EjectDryRunDispatchRequest(BaseModel):
    """Body for the one-click dry-run dispatch endpoint.

    Builds the thermal-less eject dry-run 3MF for ``(library_file_id,
    plate_index)`` and queues it on ``printer_id`` for an empty-bed sweep test.
    Geometry is resolved from the TARGET printer's model: 422 when that model has
    no geometry row, 409 when the row is not hardware-validated UNLESS
    ``allow_unvalidated`` is set (hardware-ladder step 4 only).
    """

    library_file_id: int
    plate_index: int = Field(default=1, ge=1)
    printer_id: int
    allow_unvalidated: bool = False


class EjectDryRunDispatchResponse(BaseModel):
    """Result of a one-click dry-run dispatch."""

    queue_item_id: int
    library_file_id: int
    message: str


class EjectValidationResponse(BaseModel):
    ok: bool
    errors: list[str]
    warnings: list[str]


class EjectPreviewResponse(BaseModel):
    gcode: str
    validation: EjectValidationResponse
    max_z_height: float
    # Geometry-level warnings independent of G-code validation — e.g. the target
    # model's geometry row is not hardware-validated yet (ladder pending).
    warnings: list[str] = []
