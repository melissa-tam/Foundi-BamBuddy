from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base


class SpoolAssignment(Base):
    """Assignment of a spool to a specific AMS slot on a printer."""

    __tablename__ = "spool_assignment"

    id: Mapped[int] = mapped_column(primary_key=True)
    # UNIQUE: one spool row is bound to at most ONE AMS slot, fleet-wide, at every
    # instant — a physical roll is in exactly one place, so a re-bind is a MOVE
    # (``services.spool_binding.bind_spool_to_slot``), never a copy. 012-H2S
    # (2026-07-30): a copy left spool 120 on two trays for 22 h and both presented
    # the same ledger to the start gate. Fresh installs get the constraint from
    # here; already-migrated DBs from the dedupe-then-index block in
    # ``core.database.run_migrations`` (index ``ux_spool_assignment_spool_id``).
    spool_id: Mapped[int] = mapped_column(ForeignKey("spool.id", ondelete="CASCADE"), unique=True)
    printer_id: Mapped[int] = mapped_column(ForeignKey("printers.id", ondelete="CASCADE"))
    ams_id: Mapped[int] = mapped_column(Integer)  # 0-3, 128+ (HT), 254/255 (ext)
    tray_id: Mapped[int] = mapped_column(Integer)  # 0-3
    fingerprint_color: Mapped[str | None] = mapped_column(String(8))  # tray_color snapshot
    fingerprint_type: Mapped[str | None] = mapped_column(String(50))  # tray_type snapshot
    # Explicit "this binding is a PRE-CONFIGURED intent, not a physical location
    # claim" marker: the operator deliberately bound a spool to an EMPTY slot
    # (SpoolBuddy weigh-then-assign) so the next insert adopts that row. NULL = an
    # ordinary location claim.
    #
    # Replaces the blank-fingerprint INFERENCE (``fingerprint_type in ("", None)``)
    # that carried this meaning before 2026-08-01. That inference was fragile in both
    # directions — a bind whose live tray fields simply were not readable yet looked
    # pre-configured, and any writer that filled a fingerprint silently destroyed the
    # intent — and release-on-empty must exempt these rows, so the meaning has to be
    # asserted, not guessed. Cleared by the one-shot apply-on-insert.
    pre_configured_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    spool: Mapped["Spool"] = relationship(back_populates="assignments")
    printer: Mapped["Printer"] = relationship()

    __table_args__ = (UniqueConstraint("printer_id", "ams_id", "tray_id"),)

    @property
    def printer_name(self) -> str | None:
        """Get printer name from loaded relationship."""
        return self.printer.name if self.printer else None


from backend.app.models.printer import Printer  # noqa: E402, F401
from backend.app.models.spool import Spool  # noqa: E402, F401
