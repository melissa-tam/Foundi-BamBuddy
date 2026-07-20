"""Durable per-escalation ledger for the repeat-jam printer quarantine.

Production evidence (009-H2S 2026-07-20): the same physical AMS fault escalated
recovery three separate times across the day while the machine kept trying to
swap its way out of a hardware problem (a wedged buffer / feeder). A recurring
jam is NOT a spool the swap machine can fix — it is AMS hardware that wants
hands. The in-memory latch/success counters in ``services.spool_recovery`` reset
on every restart, so they cannot see "this printer has jammed N times today".

One row per ``services.spool_recovery._escalate`` — i.e. one row every time a
recovery attempt gives up (an operator takeover / abort deliberately writes NO
row). Counting a printer's rows inside a rolling window lets recovery quarantine
a printer whose AMS keeps faulting, surviving the restarts the process-memory
latch cannot. Rows older than 30 days are pruned at startup (core/database.py,
beside the table's DDL): a row older than the quarantine window can never
influence a decision, so dropping it is behaviour-preserving and bounds the table.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class RecoveryEscalation(Base):
    """One recorded spool-recovery escalation for a printer, surviving restarts."""

    __tablename__ = "recovery_escalation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # The printer whose recovery escalated. Indexed — every quarantine check is a
    # ``WHERE printer_id = ? AND created_at >= ?`` count.
    printer_id: Mapped[int] = mapped_column(ForeignKey("printers.id"), index=True, nullable=False)
    # Naive UTC, matching the fork's other timestamp columns (datetime.utcnow()).
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # The escalation reason token (e.g. "unload_failed", "stuck_reset_failed") —
    # forensic only; the quarantine decision is count-based, reason-agnostic.
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    # The incident's primary HMS short code (e.g. "0700_8010"), or NULL when none.
    code: Mapped[str | None] = mapped_column(String(32), nullable=True)
