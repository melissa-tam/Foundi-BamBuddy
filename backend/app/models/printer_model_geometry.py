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

from sqlalchemy import Boolean, DateTime, Float, String, Text, false, func, text
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

    # Commandable Z travel (mm) — the machine bottom (printable height) the bed-drop
    # release assist drives the bed down to. NULL ⇒ the assist fails closed for this
    # model until an operator sets it (PUT /model-geometry). Never hardcoded in code:
    # the value lives here (migration seed/backfill), like the bed dims.
    z_travel_mm: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Hardware-ladder gate: False until an operator has witnessed the empty-bed
    # dry run + thermal cycle for this model. Production dispatch requires True;
    # the preview/dry-run ladder tools allow False (with a warning).
    validated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # SECOND, independent hardware-ladder gate (2026-09-04): may the eject block open
    # with the contact-free Z RE-REFERENCE prologue? False for every seeded model, so
    # a deploy changes no eject motion anywhere and each model keeps today's recipe
    # until its own ladder flips this through PUT /model-geometry (red line 2).
    #
    # Separate from ``validated`` on purpose: that flag says "the XY envelope is
    # proven", this one says "the guarded Z drive to the bottom stop was witnessed on
    # this machine, WITH the operator's plate-release aid installed". A model can have
    # a proven envelope and an unproven Z stop; conflating them would unlock motion no
    # one has watched.
    #
    # ``server_default`` is REQUIRED here, unlike on ``validated`` above: the registry's
    # idempotent seed in ``run_migrations`` is an ``INSERT ... SELECT`` that names its
    # columns explicitly, and it names ``validated`` but not this one. On a fresh DB —
    # which ``create_all`` builds from THIS declaration, not from the ALTER TABLE that
    # carries the DEFAULT for existing installs — a NOT NULL column the seed omits and
    # the ORM defaults only in Python fails the insert outright. ``false()`` renders per
    # dialect (SQLite ``0`` / Postgres ``false``), matching the migration's own ``_false``.
    z_reference_validated: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)

    # How far (mm) the bed rises off its bottom stop while the printer is HELD after a
    # confirmed plate-check trip. A clearance measured from the PHYSICAL stop, not from
    # the firmware's frame, so it stays true whatever the frame says. The distance that
    # clears the operator's plate-release aid is a hardware fact the code cannot know
    # (red line 3), hence a registry column; 12.0 is the vendor's own value from the
    # stock start block's ``G380 S2 Z-12`` and is the seeded default for every model.
    # ``server_default`` for the same reason as the column above — the seed omits it.
    hold_lift_mm: Mapped[float] = mapped_column(Float, default=12.0, server_default=text("12.0"), nullable=False)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
