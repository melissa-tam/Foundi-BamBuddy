"""SKU catalog models — the farm's product/part catalog (Phase 2).

A :class:`Sku` is an authoritative product code (e.g. ``SKU007.01``) with an
optional part number. Each SKU links to one or more printable library files via
:class:`SkuFile`, where a link pins a specific ``(library_file, plate_index)``
job identity and records how many finished units that plate yields
(``units_per_plate``). Production runs reference a ``SkuFile`` to know exactly
what to print and how many plates a unit target implies.

Nozzle/filament/model/max-Z capability facts are NOT stored here — they are read
live from ``LibraryFile.file_metadata`` (falling back to parsing the 3MF) so the
catalog never drifts from the actual sliced file.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base


class Sku(Base):
    """A catalog product/part code with its default farm eject behaviour."""

    __tablename__ = "skus"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Canonical product code, e.g. "SKU007.01". Unique across the catalog.
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Optional manufacturer part number, e.g. "2656-20".
    part_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Default eject profile applied to production runs of this SKU when the run
    # doesn't specify one. SET NULL so deleting a profile leaves the SKU intact
    # (runs then must supply their own profile).
    default_eject_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("eject_profiles.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationships
    files: Mapped[list["SkuFile"]] = relationship(
        back_populates="sku",
        cascade="all, delete-orphan",
    )
    default_eject_profile: Mapped["EjectProfile | None"] = relationship()


class SkuFile(Base):
    """Link between a SKU and a specific printable file+plate, with unit yield.

    Job identity is ``(library_file_id, plate_index)`` — a single 3MF may hold
    several plates, only some of which are sliced. ``units_per_plate`` (operator
    confirmed) captures the "one plate yields N finished parts" case, e.g. a
    plate carrying multiple battery holders.
    """

    __tablename__ = "sku_files"
    __table_args__ = (UniqueConstraint("sku_id", "library_file_id", "plate_index", name="uq_sku_file_plate"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    sku_id: Mapped[int] = mapped_column(ForeignKey("skus.id", ondelete="CASCADE"), nullable=False, index=True)
    library_file_id: Mapped[int] = mapped_column(
        ForeignKey("library_files.id", ondelete="CASCADE"), nullable=False, index=True
    )
    plate_index: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    units_per_plate: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Relationships
    sku: Mapped["Sku"] = relationship(back_populates="files")
    library_file: Mapped["LibraryFile"] = relationship()


from backend.app.models.eject_profile import EjectProfile  # noqa: E402, F401
from backend.app.models.library import LibraryFile  # noqa: E402, F401
