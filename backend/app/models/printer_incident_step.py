"""One step a recovery DRIVER sent against an equipment-fault incident — the evidence log.

The wire cannot restate which verbs a driver already sent. The firmware echoes a
command once and then shows only its CONSEQUENCES, and on a feeder-stall wedge the
consequence of every release verb it holds is identical: the print re-PAUSEs in the
same change (012-H2S 2026-09-23 — ``resume`` and ``ams_control resume`` each re-ran
slot 4 and re-held in ``1/5``). A driver that restarts from the wire alone therefore
starts its ladder again from the first lever and re-grinds a stalled feeder it has
already proved stalled. This table is that memory, durably: **one row per step**, in
send order, so a re-entering driver resumes at the next UNPULLED lever instead.

A step is written at the SEND (``sent_at``; ``outcome`` and ``read_at`` NULL) and
answered at the READ, so a publish-then-crash leaves a row that says "sent, never
read" — which is precisely the fact re-entry needs: that lever is spent, and nobody
knows what it did. ``printer_incidents.note_step`` / ``answer_step`` are the ONE writer.

3NF child of ``printer_incident``: every column is a fact about ONE step, the incident
is referenced by key and never copied, and nothing here is a JSON blob. ``seq`` is the
driver's own send order within the incident, unique per incident, so the log's order is
a column rather than a timestamp race (two steps can share a clock tick).

``kind`` is closed (:data:`StepKind`): a ``lever`` is a release verb the driver pulled
(``name`` = the lever's name in the driver's lever table), a ``command`` is an AMS
motion command it sent (``name`` = the command, ``target`` = the tray it named).
``feeder`` is the feeder-position kind the driver read AT SEND; ``outcome`` is the
reader's verdict for a lever or the classifier's answer for a command. Both
vocabularies belong to the modules that produce them, so they are stored as tokens.
"""

from datetime import datetime
from typing import Literal

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base

# The two things a driver sends. CLOSED: a third needs a decision here, not a string.
StepKind = Literal["lever", "command"]
STEP_KIND_LEVER: StepKind = "lever"
STEP_KIND_COMMAND: StepKind = "command"


class PrinterIncidentStep(Base):
    """One lever pulled or one command sent by a recovery driver, in send order."""

    __tablename__ = "printer_incident_step"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # CASCADE: a step is a fact about its incident and means nothing without it. The
    # clause is enforced by PostgreSQL; this fork opens SQLite without
    # ``PRAGMA foreign_keys=ON`` and no code path deletes an incident row, so on SQLite
    # it is the declared contract rather than the mechanism.
    incident_id: Mapped[int] = mapped_column(ForeignKey("printer_incident.id", ondelete="CASCADE"), nullable=False)
    # The driver's send order within the incident, 1-based, unique per incident.
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    # The lever name or the command name.
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    # The AMS global tray a command named; NULL for a lever (a lever names no tray).
    target: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The feeder-position kind the driver read at the send; NULL when it read none.
    feeder: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Filled at the READ. NULL = sent and never read (a crash between the two).
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Naive UTC, matching ``printer_incident``'s timestamp columns.
    sent_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        # One step per (incident, seq). Declared as a unique INDEX rather than a
        # UniqueConstraint — the ``hms_event`` idiom — so ``create_all`` and the
        # ``run_migrations`` DDL build the identically-named object on both dialects
        # (on SQLite a table constraint is backed by an anonymous autoindex, and the
        # migration's CREATE UNIQUE INDEX would then add a second one beside it).
        Index("ux_printer_incident_step_seq", "incident_id", "seq", unique=True),
        # The re-entry read: every step of one incident.
        Index("ix_printer_incident_step_incident", "incident_id"),
    )
