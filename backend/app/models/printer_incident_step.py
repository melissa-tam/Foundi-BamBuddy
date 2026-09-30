"""One step a DRIVER sent against an equipment-fault incident — the evidence log.

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
knows what it did. ``printer_incidents.note_step`` / ``answer_step`` are the ONE writer,
called only by ``printer_incidents.EvidenceLog``.

3NF child of ``printer_incident``: every column is a fact about ONE step, the incident
is referenced by key and never copied, and nothing here is a JSON blob. ``seq`` is the
driver's own send order within the incident, unique per incident, so the log's order is
a column rather than a timestamp race (two steps can share a clock tick).

``kind`` is closed (:data:`StepKind`): a ``lever`` is a release verb the driver pulled
(``name`` = the lever's name in the driver's lever table), a ``command`` is an AMS
motion command it sent (``name`` = the command, ``target`` = the tray it named), a
``dialog`` is a button of the printer's own dialog the farm pressed (``name`` = the
button's action), and a ``stop`` is the farm ending the job itself (``name`` = the stop
verb). ``feeder`` is the feeder-position kind the driver read AT SEND; ``outcome`` is the
reader's verdict for a lever, the classifier's answer for a command, and the firmware's
command answer for a dialog button or a stop. Every vocabulary belongs to the module that
produces it, so they are stored as tokens.

A driver is any lane that SENDS against an incident: the AMS recovery driver
(``spool_recovery``) and the plate-check episode driver (``pause_recovery``).
"""

from datetime import datetime
from typing import Literal

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base

# The things a driver sends. CLOSED: another needs a decision here, not a string.
#
# Decided 2026-09-29 (the plate-check ladder, operator ruling of that day): ``dialog`` and
# ``stop``. The plate-check episode driver presses the printer's own "Problem solved,
# resume" button and, when the re-check fails, stops the job — and the wire can restate
# neither: after a restart the printer shows a PAUSE either way, and a second resume is
# exactly what re-entry must never send. So both are ledger facts, one kind each, because
# re-entry derives the owed rung from WHICH was sent (no ``dialog`` → press it; a
# ``dialog`` → the stop rung), and the retry-or-escalate verdict counts ``stop`` steps.
# Neither is a ``lever`` (a release verb over a wedged AMS change, budgeted per lever) nor
# a ``command`` (an AMS motion with a tray and a feeder): folding them in would make every
# reader of those two kinds filter the plate lane's rows back out. The AMS driver's OWN
# stop is the other way round, and stays a ``lever``: its restart rung
# (``printer_incidents.FAULT_RESTART_STEP``, the same 2026-09-29 day) is the last row of
# its release ladder, budgeted, pulled and read like every other rung, and the open-row
# projection derives the restart-stop flag from that lever. The kinds are per driver
# vocabulary, never per verb. All four fit ``kind``'s VARCHAR(8), and nothing constrains
# the column in either dialect, so no migration.
StepKind = Literal["lever", "command", "dialog", "stop"]
STEP_KIND_LEVER: StepKind = "lever"
STEP_KIND_COMMAND: StepKind = "command"
# A button of the printer's own dialog, pressed by the farm (``name`` = the action).
STEP_KIND_DIALOG: StepKind = "dialog"
# The farm ended the job itself (``name`` = the stop verb).
STEP_KIND_STOP: StepKind = "stop"


class PrinterIncidentStep(Base):
    """One step a driver sent against an incident — a lever, a command, a dialog button or a stop — in send order."""

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
