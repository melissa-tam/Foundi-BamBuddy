"""Service for writing independent print log entries, and the outcome vocabulary they are read with.

Log entries are written to a separate table and never touch archives or queue items.
"""

import logging
from datetime import datetime
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.print_log import PrintLogEntry

logger = logging.getLogger(__name__)


# --- print OUTCOME buckets ----------------------------------------------------
#
# ``print_log_entries.status`` records what ENDED a print. These three sets are the
# ONE fold of that vocabulary onto the question "how did it turn out": the printer
# delivered the part, the printer failed to, or somebody interrupted it. Any
# comparison of ``PrintLogEntry.status`` against those outcomes imports them —
# ``test_print_log_outcome.TestNoBareOutcomeLiterals`` scans for the ones that do
# not — because a re-spelled set drifts silently: one tally that folds ``aborted``
# into failed beside another that does not is the same reprint failing on the
# archive card and succeeding in the stats.
#
# Two status sets in the tree answer DIFFERENT questions and deliberately stay
# where they are: ``routes/print_log._STATUS_KEYS`` is which statuses an OPERATOR
# may assign to a row by hand (a subset of these — nobody hand-writes ``aborted``),
# and ``routes/projects._FAILURE_STATUSES`` is "did this run fail to deliver a
# part", which is wider on purpose because a cancelled run delivers none either.
COMPLETED_STATUS = "completed"
FAILED_STATUSES: tuple[str, ...] = ("failed", "aborted")
CANCELLED_STATUSES: tuple[str, ...] = ("stopped", "cancelled", "skipped")


def outcome_bucket(status: str | None) -> Literal["completed", "failed", "cancelled"] | None:
    """Which outcome bucket ``status`` falls in, or ``None`` when it falls in none.

    Pure and total over every string. An unregistered status answers ``None``
    rather than being folded into a bucket it was never a member of: a tally that
    silently absorbed an unknown status would be a number nobody could check, and
    the three sets are disjoint precisely so that fold has one answer.
    """
    if status == COMPLETED_STATUS:
        return "completed"
    if status in FAILED_STATUSES:
        return "failed"
    if status in CANCELLED_STATUSES:
        return "cancelled"
    return None


async def write_log_entry(
    db: AsyncSession,
    *,
    status: str,
    archive_id: int | None = None,
    print_name: str | None = None,
    printer_name: str | None = None,
    printer_id: int | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    filament_type: str | None = None,
    filament_color: str | None = None,
    filament_used_grams: float | None = None,
    cost: float | None = None,
    energy_kwh: float | None = None,
    energy_cost: float | None = None,
    failure_reason: str | None = None,
    thumbnail_path: str | None = None,
    created_by_id: int | None = None,
    created_by_username: str | None = None,
) -> PrintLogEntry:
    """Write a print log entry."""
    duration = None
    if started_at and completed_at:
        duration = int((completed_at - started_at).total_seconds())

    entry = PrintLogEntry(
        archive_id=archive_id,
        print_name=print_name,
        printer_name=printer_name,
        printer_id=printer_id,
        status=status,
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=duration,
        filament_type=filament_type,
        filament_color=filament_color,
        filament_used_grams=filament_used_grams,
        cost=cost,
        energy_kwh=energy_kwh,
        energy_cost=energy_cost,
        failure_reason=failure_reason,
        thumbnail_path=thumbnail_path,
        created_by_id=created_by_id,
        created_by_username=created_by_username,
    )
    db.add(entry)
    await db.flush()
    return entry
