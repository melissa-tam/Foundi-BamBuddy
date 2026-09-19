"""Shared queue-item creation primitives.

Both the standard add-to-queue route and the farm production-run creator need to
allocate queue positions (advisory-locked to serialize concurrent inserts to the
same scope) and construct a run of :class:`PrintQueueItem` rows. This module is
the single canonical implementation of that logic so the two callers can never
drift — the production run does not copy-paste the route's loop.

It also owns :func:`requeue_fields`, the ONE answer to "print this plate again with
the SAME settings" — an explicit allowlist rather than another hand-kept copy of the
columns somebody remembered.

Neither helper commits; the caller owns the transaction.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, text, update

from backend.app.models.print_queue import PrintQueueItem
from backend.app.services.dispatch_target import target_of

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement


# --------------------------------------------------------------------------- #
# "The same settings" — the requeue allowlist
# --------------------------------------------------------------------------- #
# Every ``PrintQueueItem`` column belongs to EXACTLY ONE of the three sets below, and
# a test pins that their union is the model's column set. The point is the failure
# mode it creates: adding a column to the model without deciding which set it joins
# breaks CI, instead of silently reverting to a model default on every requeue — which
# is what had been happening. ``farm_policy.create_retry_if_absent`` copied 16 columns
# and dropped 19 (``use_ams``, the whole calibration block, ``skip_filament_check``,
# the operator's slot pin…), and ``production_run.top_up_run`` copied 8, so a retried
# plate quietly printed with different settings from the one it replaced.

# Carried onto the new row: the print's CONFIGURATION — what file, which plate, which
# profile, and every option the operator chose for it.
CARRIED_COLUMNS: frozenset[str] = frozenset(
    {
        # what to print
        "library_file_id",
        "archive_id",
        "plate_id",
        "project_id",
        "batch_id",
        "eject_profile_id",
        "print_time_seconds",
        "required_filament_types",
        "target_location",
        "created_by_id",
        "first_article",
        "is_dry_run",
        # how to print it
        "use_ams",
        "ams_mapping",
        "filament_overrides",
        "nozzle_mapping",
        "skip_filament_check",
        "require_previous_success",
        "bed_levelling",
        "flow_cali",
        "vibration_cali",
        "layer_inspect",
        "timelapse",
        "nozzle_offset_cali",
        "gate_acknowledged",
        "auto_off_after",
        "scheduled_time",
        "gcode_injection",
    }
)

# NOT carried, for three distinct reasons — all three live here because the census
# test needs one home per column, and the reason belongs beside the column:
#
# 1. IDENTITY AND TIMESTAMPS of the old attempt (``id``, ``created_at``,
#    ``started_at``, ``completed_at``): a new row is a new attempt.
# 2. PER-ATTEMPT STATE the new row must start clean on — the caller owns ``status``
#    and ``manual_start`` (a paused run stages its requeues), ``position`` is
#    re-allocated at the tail of the run's pending units by
#    :func:`allocate_queue_positions`, and the rest are the old attempt's residue:
#    ``waiting_reason`` / ``error_message`` / ``dispatch_subtask_id`` / ``stop_source``
#    / ``eject_dispatched_at`` / ``been_jumped`` / ``filament_short``.
#    ``cleanup_library_after_dispatch`` is state too, and dangerous state: it is the
#    Direct-Print lane's own "this upload is transient, delete it after dispatch"
#    stamp, and copying it onto a requeue would arm a second deletion of a file the
#    first dispatch already reaped (2026-08-15 durable-file-loss class).
#    ``nozzles_info`` is a dead column — never written, never read.
# 3. THE TARGET COLUMNS (``printer_id``, ``target_model``, ``target_printer_ids``).
#    These are not this allowlist's to give: ``services/dispatch_target.py`` owns
#    them as one value, and every unit-minting path spreads ``target_of(item).fields()``
#    LAST so a row can never wear two kinds at once.
NOT_CARRIED_COLUMNS: frozenset[str] = frozenset(
    {
        "id",
        "created_at",
        "started_at",
        "completed_at",
        "status",
        "manual_start",
        "position",
        "waiting_reason",
        "error_message",
        "dispatch_subtask_id",
        "stop_source",
        "eject_dispatched_at",
        "been_jumped",
        "filament_short",
        "cleanup_library_after_dispatch",
        "nozzles_info",
        "printer_id",
        "target_model",
        "target_printer_ids",
    }
)

# The chain, written by the requeue itself (``farm_policy.create_retry_if_absent``)
# and never copied from the source row: ``retry_of_id`` points AT the source and is
# the DB-backed idempotency guard, ``retry_count`` is the generation index the
# run-detail lineage renders. Note what ``retry_count`` is NOT: the genuine-failure
# cap. That is derived from the statuses of the ancestors in the chain
# (``farm_policy._genuine_failure_count``), so a lineage-only requeue — a plate the
# farm itself refused, or one an operator stopped on a fault-held printer — never
# consumes a unit's one retry.
LINEAGE_COLUMNS: frozenset[str] = frozenset({"retry_of_id", "retry_count"})


def requeue_fields(item: PrintQueueItem) -> dict[str, Any]:
    """The CARRIED columns of ``item``, as a field dict for a fresh queue row.

    The ONE origin of "print this plate again with the same settings". Callers add
    their own ``status`` / ``manual_start`` / lineage and MUST spread
    ``target_of(item).fields()`` last (see :data:`NOT_CARRIED_COLUMNS`, reason 3).

    ``ams_mapping`` is the one column with a condition, and it is a contract, not a
    preference. On a ``pending`` row the column is the operator's slot PIN (2026-08-12:
    "never a cache"), so a requeue of a pending PINNED unit carries it — same machine,
    same trays, same instruction. Two cases deliberately do not:

    * a POOL row (model- or printer-set-targeted). A pin names global tray ids on ONE
      printer, and the requeue may land on any member of the pool, where those ids mean
      different trays or nothing at all. The matcher re-decides from live trays at every
      dispatch (``print_scheduler._compute_ams_mapping_for_printer``), which is the
      right answer for a unit whose machine is not yet chosen.
    * a row past ``pending``. There the column is the DECIDED mapping for the dispatch
      that just ended, written at ``queue_transitions.claim_pending_for_dispatch``'s
      commit — which OVERWROTE the operator's pin. The pin is therefore **gone**, not
      merely "not carried": there is nothing on this row, or anywhere else, to restore
      it from. A future reader must not go looking.
    """
    fields = {name: getattr(item, name) for name in CARRIED_COLUMNS}
    if not (item.status == "pending" and not target_of(item).is_pool):
        fields["ams_mapping"] = None
    return fields


# --------------------------------------------------------------------------- #
# Position SCOPE — the ONE rule, shared by every writer of ``position``
# --------------------------------------------------------------------------- #
# A queue position is unique within its SCOPE, and there are exactly two scope
# shapes: a PINNED row (``printer_id == X``, the operator's machine pin) and the
# single shared sequence every NULL-printer row lives in — model pools, printer-
# subset pools and unassigned rows alike, because none of them has a machine yet
# and the scheduler draws them from one list. Both the allocator
# (:func:`allocate_queue_positions`) and the renumberer (:func:`renumber_pending`)
# read the rule from here; it is never restated at a call site.


def position_scope_of(item: PrintQueueItem) -> int | None:
    """The scope ``item.position`` is numbered within: its pin, or the pool."""
    return item.printer_id


def position_scope_filter(printer_id: int | None) -> tuple[ColumnElement[bool], ...]:
    """WHERE clauses selecting the ``pending`` rows of one position scope."""
    if printer_id is not None:
        return (
            PrintQueueItem.printer_id == printer_id,
            PrintQueueItem.status == "pending",
        )
    return (
        PrintQueueItem.printer_id.is_(None),
        PrintQueueItem.status == "pending",
    )


async def _lock_position_scope(db: AsyncSession, printer_id: int | None) -> None:
    """Serialize concurrent position writers within one scope (Postgres only).

    SQLite serializes writes implicitly, so this is a no-op there. Dialect is
    checked against the LIVE binding, not the ``is_sqlite()`` settings helper,
    because the test fixture overrides ``get_db`` with a SQLite engine while
    ``settings.database_url`` may still point at Postgres.
    """
    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        scope_key = printer_id if printer_id is not None else 0
        await db.execute(text("SELECT pg_advisory_xact_lock(1625, :k)"), {"k": scope_key})


async def allocate_queue_positions(
    db: AsyncSession,
    *,
    printer_id: int | None,
    count: int,
    insert_position: int | None = None,
    insert_at_top: bool = False,
) -> int:
    """Reserve ``count`` contiguous queue positions and return the first one.

    Serializes concurrent inserts to the same :func:`position_scope_filter`
    scope with a transaction-scoped advisory lock. When ``insert_at_top`` or an
    explicit ``insert_position`` is given, existing rows at/after that position
    are shifted up by ``count`` to make room.
    """
    queue_scope = position_scope_filter(printer_id)
    await _lock_position_scope(db, printer_id)

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


async def renumber_pending(db: AsyncSession, ordered_ids: Sequence[int]) -> int:
    """Apply a requested display order to the pending queue. Returns rows applied.

    ``ordered_ids`` is ONE flat list — the order the operator sees and dragged,
    which freely mixes pinned, pool and unassigned rows because the UI shows them
    together. It is also, in general, a PARTIAL view of each scope: the queue page
    filters by printer, status and location, so a scope's other rows — another
    pool's units sharing the NULL-printer sequence, say — are simply off screen.
    A drag must therefore not move work the operator never saw.

    So the rule is a SLOT PERMUTATION, not an append. Within each touched scope,
    take the current sequence (``ORDER BY position, id``); the slots currently
    held by named rows are re-filled with those rows in the requested relative
    order, and every unnamed row keeps its exact slot. The whole scope is then
    numbered ``1..N``, which also heals any duplicate or gapped positions it
    started with. When ``ordered_ids`` happens to name every pending row of a
    scope, this is exactly "apply the requested order".

    An id that is not found, or whose row is no longer ``pending``, is IGNORED
    rather than an error: the list the operator dragged is a snapshot that races
    the dispatcher, and a unit claimed for a print mid-drag must not turn the
    whole reorder into a 4xx. Does not commit — the caller owns the transaction.
    """
    # First occurrence wins: a duplicated id is one row, at the earliest rank the
    # client gave it.
    unique_ids: list[int] = []
    seen: set[int] = set()
    for item_id in ordered_ids:
        if item_id not in seen:
            seen.add(item_id)
            unique_ids.append(item_id)
    if not unique_ids:
        return 0

    result = await db.execute(
        select(PrintQueueItem).where(
            PrintQueueItem.id.in_(unique_ids),
            PrintQueueItem.status == "pending",
        )
    )
    named: dict[int, PrintQueueItem] = {item.id: item for item in result.scalars().all()}
    if not named:
        return 0

    # Lock the touched scopes in a deterministic order (pool first, then printer
    # id ascending) so two concurrent reorders can never deadlock on each other.
    scopes = sorted({position_scope_of(item) for item in named.values()}, key=lambda s: (s is not None, s or 0))
    for scope in scopes:
        await _lock_position_scope(db, scope)

    for scope in scopes:
        result = await db.execute(
            select(PrintQueueItem)
            .where(*position_scope_filter(scope))
            .order_by(PrintQueueItem.position, PrintQueueItem.id)
        )
        existing = list(result.scalars().all())
        requested = [named[i] for i in unique_ids if i in named and position_scope_of(named[i]) == scope]
        requested_ids = {item.id for item in requested}

        # The named rows' own slots, re-filled in the requested order; every other
        # row stays exactly where it was. ``strict=True`` pins the invariant that
        # the two lists are the same length — a named row of this scope is
        # pending, so it IS in ``existing``.
        placed = list(existing)
        slots = [index for index, row in enumerate(existing) if row.id in requested_ids]
        for slot, item in zip(slots, requested, strict=True):
            placed[slot] = item

        for index, item in enumerate(placed, start=1):
            item.position = index

    return len(named)


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
