"""At-rest shop-air samples — a DOCUMENTED derived cache over ``printer_sensor_history``.

Every row is one minute at which a printer's own sensors met the at-rest qualification in
:mod:`backend.app.services.eject.shop_air` (no heater target for 90 minutes, bed and
chamber within 1.5 °C of each other, both flat over 20 minutes, no AMS drying), and
``value_c`` is the lower of the two readings at that minute. It is DERIVED, and therefore
a denormalization with a stated reason rather than a fact of its own:

* Answering "what is the shop air right now" straight from the evidence means a windowed
  scan of the sensor history — seven days (the day curve) × four sensor kinds × every
  printer at 60 s, ~480k rows at the ten-printer fleet — re-run at every cooldown arm and
  every refetch of the Settings readout. The cache is a few hundred rows a week.
* The qualification itself needs 90 minutes of history per printer per minute evaluated;
  doing that once, as the minute is written, is the only place it is cheap.

The cache can always be rebuilt from its evidence: the bootstrap migration re-derives the
last seven days through the SAME function, keyed by ``rule_version``, and a rule change
bumps the version so the whole cache is dropped and re-derived. ONE writer — the owner
service and its migration (``test_code_quality.TestEjectLineOwnership``) — and pruned with
the sensor history it came from, at the same retention.
"""

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base


class ShopAirSample(Base):
    """One qualified at-rest minute of one printer: the idle-enclosure air it measured."""

    __tablename__ = "shop_air_sample"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # CASCADE is declared for PostgreSQL; with SQLite's FK enforcement off, the ORM
    # relationship on ``Printer`` (cascade="all, delete-orphan", the same one its evidence
    # rows ride) is what removes a deleted printer's samples.
    printer_id: Mapped[int] = mapped_column(ForeignKey("printers.id", ondelete="CASCADE"), nullable=False)
    # Naive UTC, the evidence MINUTE the sample describes — written explicitly by the owner,
    # never a server clock, so a backfilled sample carries the minute it was measured at.
    recorded_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    value_c: Mapped[float] = mapped_column(Float, nullable=False)
    # The qualification rule that produced this row. A cache built under another rule is
    # dropped and re-derived by the bootstrap migration, never mixed with this one.
    rule_version: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        # The estimate's read: every sample in the last seven days, fleet-wide.
        Index("ix_shop_air_sample_recorded_at", "recorded_at"),
        # The live writer's throttle ("this printer's newest sample") and the per-printer prune.
        Index("ix_shop_air_sample_printer_recorded_at", "printer_id", "recorded_at"),
    )

    printer: Mapped["Printer"] = relationship(back_populates="shop_air_samples")


from backend.app.models.printer import Printer  # noqa: E402
