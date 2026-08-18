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


def _validate_jitter_pair(cycles: int | None, mm: float | None) -> None:
    """Raise if the bed-drop jitter (cycles, mm) pair is one-sided.

    Both None (no jitter) or both set is allowed. The stroke depth vs the drop
    span is checked at generation time — only the generator knows the drop target
    (model ``z_travel_mm`` minus the clearance) and the lift height.
    """
    if (cycles is None) != (mm is None):
        raise ValueError("bed_drop_jitter_cycles and bed_drop_jitter_mm must both be set or both be null")


def validate_drop_floor_requires_drop(clearance: float | None, dwell_s: int | None, jitter_cycles: int | None) -> None:
    """Raise if a drop-floor behaviour is set without the bed-drop release assist.

    Both behaviours are motions AT the drop floor, so neither means anything
    without the drop itself. Checked against the values that will actually be
    STORED — a create body here, the merged row in the update route — because a
    profile that cannot generate must be rejected at save time, not discovered
    when a finished plate is already waiting on the eject.
    """
    if clearance is None and (dwell_s is not None or jitter_cycles is not None):
        raise ValueError("bed_drop_dwell_s and bed_drop_jitter_* require the bed-drop release assist")


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
    back_overhang_mm: float = Field(default=15.0, ge=0)
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
    # Bed-drop floor behaviours; both require the bed-drop assist to be on.
    bed_drop_dwell_s: int | None = Field(
        default=None, ge=1, le=300, description="Hold at the drop floor, whole seconds. Null = no dwell."
    )
    bed_drop_jitter_cycles: int | None = Field(
        default=None, ge=1, le=10, description="Up-then-back strokes at the drop floor. Null = no jitter."
    )
    bed_drop_jitter_mm: float | None = Field(
        default=None, ge=1, le=50, description="Depth of each jitter stroke (mm), set with the cycle count."
    )
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

    @model_validator(mode="after")
    def validate_bed_drop_floor(self) -> "EjectProfileBase":
        _validate_jitter_pair(self.bed_drop_jitter_cycles, self.bed_drop_jitter_mm)
        validate_drop_floor_requires_drop(
            self.bed_drop_clearance_mm, self.bed_drop_dwell_s, self.bed_drop_jitter_cycles
        )
        return self


class EjectProfileCreate(EjectProfileBase):
    """Schema for creating an eject profile."""


class EjectProfileUpdate(BaseModel):
    """Schema for updating an eject profile — every field optional.

    The bed-drop floor behaviours (``bed_drop_dwell_s``, ``bed_drop_jitter_*``)
    carry only the checks a PARTIAL body can express: the jitter pair must be
    updated together. Their "requires the bed-drop assist" cross-check needs the
    MERGED row — a partial body may clear the clearance while the dwell already
    sits in the DB — so the update ROUTE runs
    :func:`validate_drop_floor_requires_drop` after applying the patch (422). The
    generator repeats the check as a fail-closed raise at build time, the same
    schema/generator duplication the sweep band and bedslinger guards use.
    """

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
    # NULL = off; explicit null in an update clears that drop-floor behaviour.
    bed_drop_dwell_s: int | None = Field(default=None, ge=1, le=300)
    bed_drop_jitter_cycles: int | None = Field(default=None, ge=1, le=10)
    bed_drop_jitter_mm: float | None = Field(default=None, ge=1, le=50)
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

    @model_validator(mode="after")
    def validate_jitter_pair(self) -> "EjectProfileUpdate":
        # Same partial-update rule as the sweep band: touching one jitter field
        # requires supplying both, so the merged row is never left one-sided (both
        # null explicitly clears the jitter).
        fields_set = self.model_fields_set
        if "bed_drop_jitter_cycles" in fields_set or "bed_drop_jitter_mm" in fields_set:
            if "bed_drop_jitter_cycles" not in fields_set or "bed_drop_jitter_mm" not in fields_set:
                raise ValueError("bed_drop_jitter_cycles and bed_drop_jitter_mm must be updated together")
            _validate_jitter_pair(self.bed_drop_jitter_cycles, self.bed_drop_jitter_mm)
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
