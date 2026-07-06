"""Eject profile model — per-SKU parameters for the farm auto-eject pipeline.

An eject profile parameterises the cooldown → toolhead-sweep → park sequence
that is injected into a print's machine-end G-code so a finished part is pushed
off the plate before the next queued unit starts (lights-out farming). Geometry
is resolved against the printer's bed dimensions at generation time; every value
here is UI-configurable so no sweep coordinate or temperature is hardcoded.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class EjectProfile(Base):
    """Named set of cooldown + sweep parameters for automatic plate ejection."""

    __tablename__ = "eject_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)

    # Cooldown gate — bed must reach this before the sweep runs. A single
    # `M190 R` stalls ~42 °C (Marlin cooling-slope timeout), so the generator
    # emits `cooldown_retries` consecutive waits.
    cooldown_temp_c: Mapped[float] = mapped_column(Float, default=28.0, nullable=False)
    cooldown_retries: Mapped[int] = mapped_column(Integer, default=5, nullable=False)

    # Sweep geometry (mm).
    clearance_mm: Mapped[float] = mapped_column(Float, default=10.0, nullable=False)
    # z_offset is the toolhead floor for the sweep — never generate a move below it.
    z_offset_mm: Mapped[float] = mapped_column(Float, default=0.4, nullable=False)
    descent_steps: Mapped[int] = mapped_column(Integer, default=4, nullable=False)
    x_passes: Mapped[int] = mapped_column(Integer, default=11, nullable=False)
    x_margin_mm: Mapped[float] = mapped_column(Float, default=3.0, nullable=False)
    front_overhang_mm: Mapped[float] = mapped_column(Float, default=2.0, nullable=False)
    back_overhang_mm: Mapped[float] = mapped_column(Float, default=2.0, nullable=False)

    # Optional X sweep sub-band (mm). When BOTH are set, the sweep lanes span
    # [sweep_x_min_mm, sweep_x_max_mm] instead of the full margin-inset bed
    # width; when BOTH are NULL the full-width sweep is unchanged. Exactly one
    # set is rejected (schema 422 / EjectGenerationError at generate time).
    sweep_x_min_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    sweep_x_max_mm: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Fraction of the part height at which the descending sweep begins. 1.0 =
    # start at the part top (default, unchanged behaviour); lower begins the
    # first sweep pass partway down the part, never below the z_offset floor.
    sweep_start_frac: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)

    # Feed rates (mm/min).
    eject_speed_mm_min: Mapped[int] = mapped_column(Integer, default=3000, nullable=False)
    skim_speed_mm_min: Mapped[int] = mapped_column(Integer, default=1500, nullable=False)

    cooling_fan_assist: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Append the final slow skim pass at the z_offset floor after the descent
    # sweeps. True (default) = prior behaviour (every sweep ends with a skim to
    # clear thin remnants); False pushes exactly once (e.g. a single mid-height
    # lane for a tall part), leaving no skim pass at the plate.
    final_skim: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Safety guard — refuse to eject parts taller than this (bottom-biased sweep).
    max_part_height_mm: Mapped[float] = mapped_column(Float, default=42.0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
