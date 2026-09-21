"""Run-length-compressed history of what each printer was OBSERVED to be.

Printer state is otherwise current-value-only: connectivity, gcode state, activation,
plate phase, quarantine, USB presence and model mismatch live in process memory or on
the live status push, so "how many printers were down last Tuesday" has no answer at
all — not an approximate one, none. This table is that history.

**Raw observations, not a state.** Every column is an ORTHOGONAL fact the sampler read
off one printer at one instant. There is deliberately no ``state`` column and no
write-time precedence between the columns: classification ("printing / down / idle …")
happens at READ time, so the definition of *down* can change — and it will, as the farm
grows lanes — without invalidating a single stored row. A writer that decided the
question would have to re-record history to answer it differently.

**One row per unchanged tuple.** A sampler visits every printer on a fixed interval.
While the observed tuple is unchanged it only bumps :attr:`last_observed_at`; when any
column changes it closes the span and opens a new one. A day of a quiet fleet is
therefore a handful of rows rather than thousands of samples, and the closed span still
carries its exact start and end.

**A gap means "no data", and says so.** A span whose ``last_observed_at`` has gone stale
is closed AT its own ``last_observed_at`` — never extended to now. The uncovered stretch
that follows is an honest hole (controller restart, host sleep, a stalled loop), and the
read-time classifier reports it as unobserved rather than inventing a state for it. This
is why ``ended_at`` is written explicitly and never defaulted from a clock.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base

# The plate axis — ONE authority over what the plate is doing, four mutually exclusive
# readings. Exported as constants (the way ``printer_incident`` exports its kinds)
# because the recorder writes them and the read-time classifier branches on them: a
# magic string in either place is a silent mismatch no test can see.
PLATE_PHASE_CLEAR = "clear"  # nothing on the plate the farm knows of
PLATE_PHASE_COOLING = "cooling"  # a finished part is cooling toward the eject threshold
PLATE_PHASE_EJECTING = "ejecting"  # a sweep job owns the printer right now
PLATE_PHASE_HELD = "held"  # the plate is occupied and waiting on a person

PLATE_PHASES: frozenset[str] = frozenset(
    {PLATE_PHASE_CLEAR, PLATE_PHASE_COOLING, PLATE_PHASE_EJECTING, PLATE_PHASE_HELD}
)

# The partial predicate the open-span exclusivity index is built from — one string, so
# the ORM's ``create_all`` cannot drift from any hand-written DDL naming the same index.
OPEN_SPAN_PREDICATE = "ended_at IS NULL"


class PrinterObservationSpan(Base):
    """One stretch of time over which one printer read exactly the same way."""

    __tablename__ = "printer_observation_span"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # NO ForeignKey, deliberately. This fork runs SQLite with FK enforcement OFF, so a
    # declared ``ondelete="CASCADE"`` would be inert on SQLite and live on PostgreSQL:
    # deleting a printer would erase its whole recorded history on one engine and keep
    # it on the other. History outlives the equipment it is about, on both engines, so
    # the column is a plain indexed Integer and a row for a deleted printer is read as
    # history about a printer that no longer exists.
    printer_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # Naive UTC, second precision, written EXPLICITLY by the recorder — no
    # ``server_default``, no ``default=`` clock, no ``onupdate``. The recorder samples on
    # its own clock and has to be able to close a span at a time in the PAST (the stale
    # case above); a column that stamped itself would overwrite exactly that decision.
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # The last sample that still read this tuple. Bumped on every unchanged sample, so
    # it is the freshness of the span as well as its provisional end.
    last_observed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # NULL = the OPEN span: this printer still reads this way. Exactly one such row per
    # printer can exist (the partial unique index below).
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # ── the observed tuple ──────────────────────────────────────────────────────────
    # Each column is a separate question about the same instant. They are stored side
    # by side and ranked nowhere; the classifier ranks them.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    connected: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # The printer's own word for what it is doing (RUNNING / PAUSE / FINISH / …).
    # Nullable: a printer the controller has never reached names no state, and a
    # fabricated one would be indistinguishable from a real reading.
    gcode_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    plate_phase: Mapped[str] = mapped_column(String(12), nullable=False)
    quarantined: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # NULL = UNKNOWN, which is not False: "no USB drive" stops every dispatch and is a
    # reportable cause, while "we could not tell" must never be charged to the printer.
    usb_present: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    model_mismatch: Mapped[bool] = mapped_column(Boolean, nullable=False)

    __table_args__ = (
        # ONE open span per printer, enforced by the DATABASE rather than by the
        # recorder's own bookkeeping — the recorder is a loop that a restart, a crash or
        # a slow tick can re-enter, and two open spans would make the whole run-length
        # encoding ambiguous for every reader afterwards. PARTIAL (SQLite >= 3.8 and
        # PostgreSQL both support the WHERE clause), so closed spans accumulate freely
        # as history while a second concurrent open row dies with IntegrityError.
        Index(
            "ux_printer_observation_span_open",
            "printer_id",
            unique=True,
            sqlite_where=text(OPEN_SPAN_PREDICATE),
            postgresql_where=text(OPEN_SPAN_PREDICATE),
        ),
        # THE read path: every window query is "this printer's spans overlapping
        # [from, to)". printer_id is the leftmost column here and in the partial index
        # above, so a separate single-column index on it would be pure duplication on a
        # table that is written every sampling tick.
        Index("ix_printer_observation_span_printer_started", "printer_id", "started_at"),
        # The window query's other half: "ended after the window opened". The recorder's
        # open-span lookup needs no help from it — the partial index above IS that set.
        Index("ix_printer_observation_span_ended", "ended_at"),
        # NEVER index ``last_observed_at``. It is rewritten on every sample of every
        # printer — the single hottest write in this table — and no reader searches by
        # it: staleness is judged on a row the reader already has in hand.
    )
