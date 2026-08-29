"""Durable per-printer AMS incident — the one lifecycle record of a fault hold.

Production gap (2026-08-09 audit, WS2b): the whole recovery/hold/auto-resume/
escalation machine lived in ``spool_recovery``'s PROCESS-LIFETIME dicts
(``_handled`` / ``_escalated`` / ``_success_counts``) and was reachable only through a
matching FARM queue item. Three consequences, all observed:

* 12 foreign-print runouts got ``spent_at`` stamps but no alert, no hold and no
  resume — the entry gate returned early because no farm unit was printing;
* ``_escalated`` never expired, so a LATER, different fault on the same job could
  never be recovered — the latch outlived the incident it was written for;
* a restart erased every latch and every "we already told the operator", while the
  standing HMS came straight back.

This table is that state, durably: **one row per fault incident**, farm or foreign.

Lifecycle — ``status`` says HOW, ``resolved_at`` says WHETHER it is still live:

===============  ==============  ==================================================
status           resolved_at     meaning
===============  ==============  ==================================================
``recovering``   NULL (OPEN)     the machine is acting on it right now
``escalated``    NULL (OPEN)     given up on; the printer is HELD for a human
``resolved``     set (CLOSED)    the fault is over (resumed / recovered / terminal)
``aborted``      set (CLOSED)    someone/something else took it over, or it proved
                                 transient — never re-entered for the same fault
===============  ==============  ==================================================

An ESCALATED incident stays OPEN on purpose: the hold IS the incident, so the
hourly attention reminder, the printer-card chip and the "one open incident per
printer" exclusion all read the same row. It closes when the printer is observed
RUNNING again (any resume source — including an operator pressing Resume on the
screen, which nothing used to notice), when the job reaches a terminal, or when the
refill auto-resume lands.

**3NF note.** ``PrintQueueItem.waiting_reason`` is a PROJECTION of this row for
farm prints — a display token derived from ``kind``/``status``, written for the
queue UI's benefit. It is never a second source of truth: every ownership decision
(is this printer held? may a new incident start? should the reminder fire?) reads
the incident, and a foreign print has no queue row to project onto at all.

``item_id`` is NULL for a foreign print and ON DELETE SET NULL for a farm one — the
incident is a fact about the PRINTER and outlives the queue row it happened to
interrupt.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base

# Incident kinds — the farm's reaction vocabulary, derived from the WS2a fault
# taxonomy (``hms_errors.AmsFaultClass``) and NOT a second classification: the
# mapping lives in ``spool_recovery._KIND_BY_CLASS`` (one origin).
KIND_JAM = "jam"  # mechanical_feed — the swap machine's territory (farm prints only)
KIND_RUNOUT = "runout"  # runout / runout_external — hold for a SAME-slot refill
KIND_PHYSICAL = "physical"  # physical_fault — hands needed, never a swap

STATUS_RECOVERING = "recovering"
STATUS_ESCALATED = "escalated"
STATUS_RESOLVED = "resolved"
STATUS_ABORTED = "aborted"

# How an incident closed. ``None`` on a transient close (the fault never held the
# printer — the firmware handled it before we could act), which is an outcome no
# actor can claim.
RESOLVE_AUTO_RESUME = "auto_resume"
RESOLVE_OBSERVED_RUNNING = "observed_running"
RESOLVE_TERMINAL = "terminal"
RESOLVE_OPERATOR = "operator"
# The wire says the hold is over: the printer is live in a positive non-PAUSE state
# AND no actionable AMS fault stands on it any more (``spool_recovery.
# sweep_open_incidents``). Its own token rather than ``observed_running`` because it
# is the ONLY close nobody performed — no resume, no terminal, no human — and 001-H2S
# incident #60 (2026-08-29) sat open 15 h precisely because that close had no path.
# ``resolve_source`` is free text, so the token needs no migration.
RESOLVE_WIRE_CLEAR = "wire_clear"


class PrinterIncident(Base):
    """One AMS fault incident on one printer, from detection to close."""

    __tablename__ = "printer_incident"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # CASCADE: an incident is a property of the printer (the fork's hms_event /
    # ams_history convention).
    printer_id: Mapped[int] = mapped_column(ForeignKey("printers.id", ondelete="CASCADE"), nullable=False)
    # The printer's live ``subtask_id`` when the fault arrived. NOT NULL with a ''
    # default rather than nullable: '' means "the printer named no job" (a degenerate
    # screen-restart echo does exactly that), and a NULL would make the
    # already-handled lookup below need IS NULL branches on both dialects.
    job_id: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    # The farm queue unit the fault interrupted; NULL = a FOREIGN print (nothing the
    # farm dispatched). SET NULL, not CASCADE — deleting a queue row must not erase
    # the printer's fault history.
    item_id: Mapped[int | None] = mapped_column(
        ForeignKey("print_queue.id", ondelete="SET NULL"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # The representative short code (``MMMM_CCCC``) the notifications name.
    code: Mapped[str] = mapped_column(String(16), nullable=False)
    # The sorted fingerprint of the whole triggering candidate set — the identity of
    # THIS fault, used to decide whether a later push is the same incident coming
    # back or a genuinely new one. Slot-qualified, so a second slot running dry in
    # one job is a new incident rather than a swallowed duplicate.
    codes: Mapped[str] = mapped_column(String(256), nullable=False)
    # The AMS global tray the fault names (``ams_id * 4 + tray_id``), when the
    # firmware attributed one. NULL for slot-agnostic faults and every external-spool
    # runout (there is no AMS slot to name).
    slot_global_tray: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # Naive UTC, matching the fork's other timestamp columns.
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # The CLOSE stamp for any terminal status (resolved AND aborted) — an escalated
    # incident is still open, so this column alone answers "is this printer held".
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolve_source: Mapped[str | None] = mapped_column(String(24), nullable=True)

    __table_args__ = (
        # ONE open incident per printer — the durable successor of the in-memory
        # ``_active_tasks`` exclusivity, enforced by the database instead of by a
        # dict that a restart empties. PARTIAL (SQLite ≥3.8 and PostgreSQL both
        # support the WHERE clause), so closed incidents accumulate freely as
        # history while a second concurrent open row dies with IntegrityError.
        Index(
            "ux_printer_incident_open",
            "printer_id",
            unique=True,
            sqlite_where=text("resolved_at IS NULL"),
            postgresql_where=text("resolved_at IS NULL"),
        ),
        # The already-handled lookup: "has this printer already finished with this
        # exact fault on this exact job?"
        Index("ix_printer_incident_job", "printer_id", "job_id"),
    )
