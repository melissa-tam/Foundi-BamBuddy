"""Exact durations of the farm's short cycle episodes, as measured by their own owners.

Between two prints the printer spends its time in two farm-run processes — the cooldown
wait and the eject sweep — and both are far too short to be seen by a state sampler:
an eject runs about a minute and a half, so a 30 s sampler would round it to anything
between one and three samples and a whole cycle could fall between two ticks entirely.
Sampling is the wrong instrument for them, and the right one already exists: the
cooldown and the eject each know precisely when they started and ended, because each
measures itself to decide whether it is over. This table is where that measurement
lands instead of being written to a log line and discarded.

Append-only. A row is a closed, finished episode — there is no open-episode state here
(the observation-span log already carries "an eject is in flight right now"), so a row
is never updated after it is written and a reader never has to reason about half a row.

``expected_s`` carries the generator's OWN prediction for an eject, so "this sweep ran
long" is answerable from the ledger rather than from a human remembering what normal
looks like. A cooldown has no expectation — it ends when the bed reaches a temperature,
which depends on the part, the ambient and the fans — so the column is NULL there, and
NULL means "nothing to compare against", never zero.
"""

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base

# The two episode kinds. Constants because the two writers and every reader that groups
# by kind must spell them identically.
KIND_COOLDOWN = "cooldown"
KIND_EJECT = "eject"

EPISODE_KINDS: frozenset[str] = frozenset({KIND_COOLDOWN, KIND_EJECT})

# The cooldown half of ``variant`` — the one thing that actually separates cooldown
# durations on either printer model: whether the plate was held up toward the nozzle
# plane with the fans, or only fanned. An eject's ``variant`` is its own purpose
# instead, which is open vocabulary and therefore has no constants here.
COOLDOWN_VARIANT_HOLD = "hold"
COOLDOWN_VARIANT_FAN_ONLY = "fan_only"

COOLDOWN_VARIANTS: frozenset[str] = frozenset({COOLDOWN_VARIANT_HOLD, COOLDOWN_VARIANT_FAN_ONLY})


class FarmCycleEpisode(Base):
    """One measured cooldown or eject, start to end."""

    __tablename__ = "farm_cycle_episode"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # NO ForeignKey, for the same reason the observation span has none: SQLite runs here
    # with FK enforcement OFF, so a declared CASCADE would be inert on SQLite and live on
    # PostgreSQL — the same deleted printer would keep its measurement history on one
    # engine and lose it on the other. Indexed on its own because this table's other
    # index leads with ``kind``, so no composite covers a per-printer lookup.
    printer_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    # Naive UTC, second precision, written explicitly by the measuring owner — no
    # ``server_default``, no ``default=`` clock. The episode's clock is the owner's, and
    # a row is written AFTER the episode ends, so a self-stamping column would record
    # when the ledger was told rather than when the process ran.
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ended_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # The eject generator's predicted runtime. NULL for a cooldown — it has no prediction
    # to be measured against.
    expected_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    # How the episode finished, in the owner's own vocabulary. NULL when the owner
    # reported a duration without a verdict.
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # The grouping axis WITHIN a kind: an eject's purpose, or a cooldown's
    # hold/fan-only mode. NULL when the owner named none.
    variant: Mapped[str | None] = mapped_column(String(24), nullable=True)

    __table_args__ = (
        # THE read path: "every eject that ended in this window", per kind. ``ended_at``
        # rather than ``started_at`` is the window column because an episode belongs to
        # the bucket it COMPLETED in — that is when its duration became a fact.
        Index("ix_farm_cycle_episode_kind_ended", "kind", "ended_at"),
    )
