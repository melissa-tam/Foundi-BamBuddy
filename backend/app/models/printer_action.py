"""One action on a printer — what was done to it, by whom, and what the printer answered.

THE durable record of every state-changing command the farm or an operator sent a printer, and of
every load the printer made that nobody commanded (2026-10-11). Before it the farm decided "did the
printer do what it was told" from clocks and process memory: per-process sequence ids restarting at
0, a motion ledger and a commanded-load TTL a restart wiped, ten-odd waiters each with its own
window. 001-H2S incident 574 sat paused all night because the resume it was owed rested on a memory
the deploy erased. Now the send is a row, and the answer is an event read into it.

**The row id IS the wire ``sequence_id``.** ``services/printer_action`` INSERTs and COMMITs the row
BEFORE the publish and publishes with ``str(id)``, so the firmware's ACK names this row — after a
restart too — and two sends can never share an id. ``AUTOINCREMENT`` (``core.database
._AUTOINCREMENT_TABLES``): a deleted id is never reissued, so a late ACK can never answer a stranger.

An action is OUTSTANDING while ``answered_at`` is NULL. ``answer`` is one of the four final answers
(``command_outcome.Answer``: reached / refused / void / taken_over), decided only by an event — the
ACK, a reported state, a feed event, the job's terminal, a reboot, a person's act — never by a clock;
``reason`` carries the printer's own words for a refusal (or why it can never be reached).
``acked_at`` stamps the firmware's success ACK of a verb that is answered by a later state (a resume
accepted, waiting for RUNNING; an AMS command accepted and held), so a restart knows it was accepted.
``refusal`` is the FARM's own reason the frame never went out (``not_connected`` / ``not_sent`` /
``runout_hold`` — the record answers such a command ``refused`` at the send); ``NULL`` for every command
that went out. It is what tells "the printer refused it" (its words in ``reason``) from "it never left"
(a resume that never left is still owed — 2026-10-11, the hold that must never wait for a click).
``bound_passed_at`` stamps when the action's SELF-HEAL bound first passed in the printer's online time
with no event answering it (2026-10-12, "if it was self heal, it should stay self heal"): the lane took
its fallback, the action stayed outstanding, and a late answer still fills ``answer``. Written once.

3NF: every column is a fact about ONE action. ``entry`` is the family's own reading at the send (an
AMS motion's posture — ``command_outcome.posture`` — or any other verb's job word); ``job_id`` the
job it was sent against (a start's: the job it dispatched). ``incident_id`` / ``queue_item_id`` name
the hold or the unit it acted for. ``printer_incident_step.action_id`` references this row: a
driver's step keeps its own vocabulary and reading, the send itself lives here.

``actor`` is closed (``command_outcome.Actor``): ``farm`` (a farm lane), ``operator`` (a person's
route), ``printer`` (a landing nobody commanded — ``command`` ``observed_load``, recorded answered).
``command`` is the record's vocabulary (``command_outcome.COMMANDS``; a name outside it is refused
at the send).

Written ONLY by ``services/printer_action`` (``TestPrinterActionOwnership``).
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class PrinterAction(Base):
    """One action on a printer: the send (or the uncommanded landing) and the printer's answer."""

    __tablename__ = "printer_action"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    printer_id: Mapped[int] = mapped_column(ForeignKey("printers.id", ondelete="CASCADE"), nullable=False)
    actor: Mapped[str] = mapped_column(String(8), nullable=False)
    command: Mapped[str] = mapped_column(String(32), nullable=False)
    # The tray a load named, the decimal dialog code a press answered; NULL when the verb names none.
    target: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The family's own reading at the send (an AMS motion's posture, another verb's job word).
    entry: Mapped[str | None] = mapped_column(String(24), nullable=True)
    # The job the command was sent against (``subtask_id``); a start's, the job it dispatched (the
    # submission id minted at the send).
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # A start's: the job id the printer reported at the send — the job it replaces, which a frame can
    # still carry before the printer adopts the dispatched id. NULL for every other verb.
    prior_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    incident_id: Mapped[int | None] = mapped_column(
        ForeignKey("printer_incident.id", ondelete="SET NULL"), nullable=True
    )
    queue_item_id: Mapped[int | None] = mapped_column(ForeignKey("print_queue.id", ondelete="SET NULL"), nullable=True)
    # Naive UTC, matching ``printer_incident``'s timestamp columns.
    sent_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    acked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    answer: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Why the farm never put the frame on the wire; NULL for a command that went out.
    refusal: Mapped[str | None] = mapped_column(String(16), nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # When the self-heal bound first passed unanswered (``printer_action._pass_bound``); NULL for never.
    bound_passed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        # The outstanding actions of one printer (``answered_at IS NULL``): the hydrate and the pages.
        Index("ix_printer_action_printer_answered", "printer_id", "answered_at"),
        # The actions of one hold: the open-row projection (``printer_incidents``).
        Index("ix_printer_action_incident", "incident_id"),
        {"sqlite_autoincrement": True},
    )
