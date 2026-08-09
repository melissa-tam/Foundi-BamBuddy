"""Durable per-printer HMS vocabulary — which codes a printer has ACTUALLY emitted.

Production gap (2026-08-09 audit): HMS codes were persisted NOWHERE. The durable
:class:`~backend.app.models.notification_ledger.NotificationLedger` records only that an
ALERT was sent, and main's HMS block deliberately notifies solely on codes with a catalog
description — an UNDESCRIBED code was dropped after one DEBUG line, which production
logging (INFO) never writes. Printer 2 carried live ``0500_0051`` / ``0500_0005`` with
ZERO occurrences across 914k log lines, so every incident audit had to reconstruct the
fleet's vocabulary from notification side effects.

One row per ``(printer_id, full_code)``, where ``full_code`` is the LOSSLESS 16-hex
``attr``+``code`` identifier the MQTT parser composes onto ``HMSError.full_code`` (the
same string ``notify_dedup.hms_ledger_key`` keys its ledger off — one origin, and the
legacy attr-only key collided for distinct codes sharing an attr).

This is a COUNTER, not an event log. A standing code rides every ~1 Hz status push, so a
row-per-observation table would grow without bound and answer nothing extra; the writer
in main's HMS block throttles per ``(printer, code)`` and this table answers the question
an audit actually asks — *which codes has this printer emitted, how often, and when did
it last say so*. ``count`` is therefore a count of THROTTLED WRITES, not of pushes.

Rows untouched past the retention window are pruned at startup (main's lifespan hygiene,
beside the notification-ledger prune) — an HMS code a month stale is history, not state.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class HMSEvent(Base):
    """One printer's record of one HMS code: when first/last seen, and how often."""

    __tablename__ = "hms_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # CASCADE: the vocabulary is a property of the printer, so a deleted printer takes
    # its codes with it (the fork's ams_history / kprofile_note convention).
    printer_id: Mapped[int] = mapped_column(ForeignKey("printers.id", ondelete="CASCADE"), nullable=False)
    # The lossless identifier: 16 hex chars from the ``hms[]`` path, 8 from the 32-bit
    # ``print_error`` path — never the lossy "MMMM_CCCC" short code, which discards the
    # attr low word and the code high word (severity + slot attribution live there).
    full_code: Mapped[str] = mapped_column(String(16), nullable=False)
    # The raw wire words, kept separately so a query can decode/group without re-parsing
    # the hex string. BigInteger because both reach 0xFFFFFFFF and Postgres INTEGER is
    # SIGNED 32-bit — a real 0x07002000-class attr fits, but the code word's severity
    # nibble pushes past the ceiling and would overflow on insert.
    attr: Mapped[int] = mapped_column(BigInteger, nullable=False)
    code: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Bambu severity 1=fatal, 2=serious, 3=common, 4=info, decoded by the ONE origin
    # ``hms_errors.hms_severity``. Nullable: a wire shape that carries no decodable code
    # word must still leave a trace rather than be dropped for lack of a severity.
    severity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Naive UTC, matching the fork's other timestamp columns.
    first_seen: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    # The natural key. UNIQUE so the writer's upsert can never race two rows for one
    # code, and it doubles as the read path's index: every query is
    # ``WHERE printer_id = ? ORDER BY last_seen DESC`` and printer_id is the leftmost
    # column. Declared as an INDEX (not a UniqueConstraint) so create_all and the
    # ``run_migrations`` DDL produce the identically-named object on both dialects.
    __table_args__ = (Index("ux_hms_event_printer_code", "printer_id", "full_code", unique=True),)
