"""Shared queue-item creation primitives.

Both the standard add-to-queue route and the farm production-run creator need to
allocate queue positions (advisory-locked to serialize concurrent inserts to the
same scope) and construct a run of :class:`PrintQueueItem` rows. This module is
the single canonical implementation of that logic so the two callers can never
drift — the production run does not copy-paste the route's loop.

Neither helper commits; the caller owns the transaction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, text, update

from backend.app.models.print_queue import PrintQueueItem

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def allocate_queue_positions(
    db: AsyncSession,
    *,
    printer_id: int | None,
    count: int,
    insert_position: int | None = None,
    insert_at_top: bool = False,
) -> int:
    """Reserve ``count`` contiguous queue positions and return the first one.

    Serializes concurrent inserts to the same scope (a specific printer, or the
    shared unassigned/model-based pool) with a Postgres transaction-scoped
    advisory lock — SQLite serializes writes implicitly so it's a no-op there.
    When ``insert_at_top`` or an explicit ``insert_position`` is given, existing
    rows at/after that position are shifted up by ``count`` to make room.
    """
    if printer_id is not None:
        queue_scope = (
            PrintQueueItem.printer_id == printer_id,
            PrintQueueItem.status == "pending",
        )
    else:
        queue_scope = (
            PrintQueueItem.printer_id.is_(None),
            PrintQueueItem.status == "pending",
        )

    # Dialect is checked against the live binding, NOT the is_sqlite() settings
    # helper, because the test fixture overrides get_db with a SQLite engine
    # while settings.database_url may still point at Postgres.
    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        scope_key = printer_id if printer_id is not None else 0
        await db.execute(text("SELECT pg_advisory_xact_lock(1625, :k)"), {"k": scope_key})

    if insert_at_top or insert_position is not None:
        pos = max(1, insert_position or 1)
        result = await db.execute(select(func.max(PrintQueueItem.position)).where(*queue_scope))
        max_pos = result.scalar() or 0
        pos = min(pos, max_pos + 1)
        await db.execute(
            update(PrintQueueItem)
            .where(*queue_scope)
            .where(PrintQueueItem.position >= pos)
            .values(position=PrintQueueItem.position + count)
        )
        return pos

    result = await db.execute(select(func.max(PrintQueueItem.position)).where(*queue_scope))
    max_pos = result.scalar() or 0
    return max_pos + 1


async def create_queue_items(
    db: AsyncSession,
    *,
    count: int,
    printer_id: int | None,
    fields: dict[str, Any],
    insert_position: int | None = None,
    insert_at_top: bool = False,
) -> list[PrintQueueItem]:
    """Allocate positions and create ``count`` queue items sharing ``fields``.

    ``fields`` are the per-item column values (everything except ``position``,
    which is assigned contiguously from the allocated start). Items are added to
    the session but NOT committed. Returns the created items in position order.
    """
    start_position = await allocate_queue_positions(
        db,
        printer_id=printer_id,
        count=count,
        insert_position=insert_position,
        insert_at_top=insert_at_top,
    )
    items: list[PrintQueueItem] = []
    for i in range(count):
        item = PrintQueueItem(position=start_position + i, **fields)
        db.add(item)
        items.append(item)
    return items
