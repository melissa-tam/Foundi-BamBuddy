"""The operator's "Re-check slot" intent — doctrine rule 12's durable half.

Doctrine rule 6 has always named TWO identity oracles: *"an RFID tag **or a human
answer**"*. Only the tag was ever wired up. Rule 12 (operator-ratified 2026-08-19,
incident shape 32) wires up the other one: **the click is evidence** — nobody presses
it unless something physically happened — and it concludes **on the tag-ness ANSWER,
never on the click alone**, so a new or reused RFID roll inserted mid-print is never
mis-minted as tagless.

That "never on the click alone" is what makes this table exist. Mid-print the farm never
commands an RFID read (doctrine rule 5; ``ams_presence.command_identify`` defers on
engaged filament), so a slot holding a brand-new Bambu roll is indistinguishable from one
holding a third-party roll. A print runs for hours and a restart inside it is ordinary, so
the operator's answer has to outlive both — otherwise the click silently evaporates, which
is the very failure shape it was added to cure.

**Declared as an exception, not smuggled in.** The 2026-08-09 restart-durability verdict
(skill ``bambu-ams-behavior``, "Restart durability") says the incident row is the only
durable addition because *"everything else is a timer or an edge the wire re-answers for
free"*. A human's click is neither: no push, no edge and no reconnect can ever restate it.
Same justification class as ``printer_incident``, and the same physical shape — one OPEN
row per key, enforced by a PARTIAL unique index rather than by a dict a restart empties.

**3NF.** The key is the slot triple. ``requested_at`` and ``requested_by`` are facts about
that key. ``resolved_at`` is the lifecycle column the partial index needs. ``minted_spool_id``
is the intent's OUTCOME, and it is here because it cannot be derived: a click-driven mint
and an automatic long-gap mint produce byte-identical ``spool`` rows, and only the
click-driven one may raise the acknowledgement (the 2026-08-10 wave demoted six
non-actionable surfaces to log lines precisely so routine roll changes stay quiet).

Nothing else belongs. In particular there is no stored verdict, no cached slot state and no
copy of the previous roll: the undo re-derives its predecessor through
``spool_binding.last_released_from_slot_stmt``, the ONE origin for "which rows last left
this slot" (derive-don't-store; the same shared statement the de-bounce donor query and the
tier-2 spent stamp adjudicate through).
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, text
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class SlotRecheckIntent(Base):
    """One operator re-check request for one AMS slot, from click to conclusion."""

    __tablename__ = "slot_recheck_intent"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # CASCADE: the intent is a property of the printer's slot, so a deleted printer
    # takes its slots' pending questions with it (the fork's hms_event /
    # printer_incident convention).
    printer_id: Mapped[int] = mapped_column(ForeignKey("printers.id", ondelete="CASCADE"), nullable=False)
    ams_id: Mapped[int] = mapped_column(Integer, nullable=False)
    tray_id: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # Who asked. NULL when auth is disabled — the fork's other operator-attributed
    # columns take the same shape. SET NULL, not CASCADE: deleting a user must not
    # erase a slot's identity history.
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # NULL = OPEN: the farm still owes this slot an answer. Set the moment the decision
    # table concludes for the slot, whatever the conclusion was.
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # The row a CLICK-DRIVEN mint created, or NULL when the intent resolved any other
    # way (a tag landed and the identity lane decided; the roll was pulled). The only
    # thing that scopes the acknowledgement + undo to mints the operator caused.
    # SET NULL so purging a spool row cannot orphan the intent's history.
    #
    # It is also the RETRACTION: ``slot_recheck.undo`` NULLs it once the mint has been
    # handed back, because "resolved, nothing standing" is exactly what NULL already
    # means here. That is why the undo needs no ``undone_at`` column — a second boolean
    # for a state this one already expresses would be two sources for one fact, and the
    # act's own log line plus the archived row carry the history a flag could not.
    minted_spool_id: Mapped[int | None] = mapped_column(ForeignKey("spool.id", ondelete="SET NULL"), nullable=True)

    __table_args__ = (
        # ONE open intent per slot. PARTIAL (SQLite >= 3.8 and PostgreSQL both honour the
        # WHERE clause), so resolved intents accumulate as history while a second
        # concurrent open row dies with IntegrityError — the same enforcement shape
        # ``printer_incident`` uses for one-open-per-printer. A second click on a slot
        # that already has an open intent is therefore idempotent BY CONSTRUCTION rather
        # than by a check the caller might forget.
        Index(
            "ux_slot_recheck_intent_open",
            "printer_id",
            "ams_id",
            "tray_id",
            unique=True,
            sqlite_where=text("resolved_at IS NULL"),
            postgresql_where=text("resolved_at IS NULL"),
        ),
        # The acknowledgement lookup: "what did this slot's most recent re-check do?"
        Index("ix_slot_recheck_intent_slot", "printer_id", "ams_id", "tray_id"),
    )
