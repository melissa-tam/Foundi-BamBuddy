"""Durable "when did we last send this alert" ledger.

Production incident (2026-07-20, plan phase D / RC-D): the HMS re-notify dedup
(``services.notify_dedup``) kept its per-(printer, code) last-seen map in PROCESS
memory only, so every deploy re-notified EVERY standing HMS code fleet-wide — the
00:45 deploy re-announced six printers' standing codes within 7 seconds. Several
of those codes are permanent-until-power-cycle (a failed read on a tagless slot
can never clear), so operators got the identical alert at every single deploy.

One row per ``(scope, dedup_key)`` records the wall-clock instant an alert was
last ACTUALLY sent. It is deliberately NOT a second dedup engine: the in-memory
ledger still decides the fast path (flap collapsing within one process). This
table answers exactly one question the in-memory ledger cannot — *"was this alert
already delivered before the restart?"* — which is what lets
``notify_dedup.seed_standing`` pre-mark standing pre-restart incidents as seen
while a fault that arose DURING the downtime (no row) still notifies.

``scope`` namespaces the key space (``"hms"`` today) so a future durable-dedup
consumer needs no schema change. Rows are pruned at startup past
``notify_dedup.LEDGER_PRUNE_DAYS`` — a key untouched that long re-notifies as new
anyway, so dropping it is behaviour-preserving and bounds the table.
"""

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class NotificationLedger(Base):
    """Last-sent stamp for one deduplicated alert key, surviving restarts."""

    __tablename__ = "notification_ledger"

    # Key-space namespace, e.g. "hms". Part of the composite primary key — the
    # natural key IS the identity here, so there is no surrogate id column.
    scope: Mapped[str] = mapped_column(String(32), primary_key=True)
    # Scope-defined key. For "hms" this is ``f"{printer_id}:{full_code}"``
    # (see ``notify_dedup.hms_ledger_key``) — the printer-scoped, LOSSLESS code
    # identifier; the legacy attr-only key collided for distinct codes sharing
    # an attr and so could never key a durable ledger.
    dedup_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    # Naive UTC, matching the fork's other timestamp columns (server_default
    # func.now() / datetime.utcnow()).
    last_sent_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
