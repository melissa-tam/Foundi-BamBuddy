"""Printer model geometry registry — DB-config bed/envelope per printer model.

Replaces the two in-code dicts the eject generator used to carry
(``PRINTER_BED_DIMS`` / ``PRINTER_TRAVEL_ENVELOPE``). Making geometry a table
means a new farm model (e.g. the incoming H2C) is enabled by editing a row —
no code change — and the ``validated`` flag fail-closes production dispatch on a
model whose envelope has not been through the hardware ladder yet (red line #2 /
#3: no hardcoded coords, ladder before unattended use).

One row per canonical model key (``utils.printer_models.canon_model`` output, e.g.
``H2S``). The eject geometry accessor (``services.eject.geometry``) reads these
rows into the frozen :class:`~backend.app.services.eject.geometry.ModelGeometry`
the generator/validator consume.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class PrinterModelGeometry(Base):
    """Bed rectangle + machine XY travel envelope + height ceiling for one model."""

    __tablename__ = "printer_model_geometry"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Canonical model key (canon_model output: uppercase, space-stripped, e.g.
    # "H2S"). Unique — the accessor matches the printer's canonicalised model here.
    model_key: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)

    # Bed rectangle (mm).
    bed_x: Mapped[float] = mapped_column(Float, nullable=False)
    bed_y: Mapped[float] = mapped_column(Float, nullable=False)

    # Machine XY travel envelope (mm) — the box the toolhead may reach without
    # tripping the firmware soft limits. Distinct from the bed rectangle: sweep
    # overhangs can (and do) run negative in Y past the bed edge.
    env_x_min: Mapped[float] = mapped_column(Float, nullable=False)
    env_x_max: Mapped[float] = mapped_column(Float, nullable=False)
    env_y_min: Mapped[float] = mapped_column(Float, nullable=False)
    env_y_max: Mapped[float] = mapped_column(Float, nullable=False)

    # Physical part-height ceiling (mm) for the door-open sweep on this model.
    max_part_height_mm: Mapped[float] = mapped_column(Float, nullable=False)

    # Hardware-ladder gate: False until an operator has witnessed the empty-bed
    # dry run + thermal cycle for this model. Production dispatch requires True;
    # the preview/dry-run ladder tools allow False (with a warning).
    validated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
