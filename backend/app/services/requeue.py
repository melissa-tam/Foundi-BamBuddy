"""THE owner of "put a plate back in the queue" (operator ruling 2026-09-24).

Every automatic path that puts a plate back goes through a verb here, so there is one
answer to three questions that used to be answered — or not — at each call site:
WHERE the plate lands, WHAT it carries, and whether its RUN still wants it.

Three shapes of "back in the queue", one module:

* :func:`requeue_attempt` — a NEW row for an attempt that ended (a genuine failure, a
  dispatch-time failure, an operator stop over an equipment fault, a plate the farm
  refused). It carries the lineage (``retry_of_id`` / ``retry_count``) and is its own
  unit of work.
* :func:`return_to_queue` — the SAME row, un-claimed: a dispatch that never started
  (a refused commit, the start watchdog, a dead claim). ``queue_transitions
  .release_unstarted_claim`` stays the storage writer of ``printing → pending``; this
  verb is its only caller and adds the position and the run gate the transition
  deliberately knows nothing about.
* :func:`mint_replacements` — fresh, lineage-FREE rows for a run's deficit on RESUME.
  Lineage-free is load-bearing: ``production_run.planned_plate_count`` and the top-up
  shortfall both count ``retry_count == 0`` rows as the run's primaries.

…and the ONE walk of a lineage chain, :func:`lineage_root` / :func:`failed_ancestor_count`.

**Next in line.** Every plate this module puts back lands at the HEAD of its target's
position scope — ``create_queue_items(insert_at_top=True)`` for a new row,
``queue_builder.seat_at_head`` for a returned one — and carries ``been_jumped=True``,
which the shortest-job-first ordering reads BEFORE print time, so both orderings
serve it first. Before this module a requeue appended at ``max + 1`` (the back of a
120-unit run) and a returned claim kept whatever stale number it had. Plan
materialisation (``farm_policy.create_remaining_plates`` / ``create_new_first_article``)
still appends: those rows are the run's plan being released, not a plate put back.

**Decisions stay with their owners.** Whether a plate is requeued at all — the stop
verdict, the genuine-failure cap, the run's state — is ``farm_policy``'s, and the
deficit is ``production_run``'s. This module owns only how a plate is put back.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.app.core.database import run_with_retry
from backend.app.models.print_batch import PrintBatch
from backend.app.models.print_queue import PrintQueueItem
from backend.app.services import queue_transitions
from backend.app.services.dispatch_target import target_of
from backend.app.services.queue_builder import create_queue_items, requeue_fields, seat_at_head

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Why a NEW attempt row is being minted — the log vocabulary of :func:`requeue_attempt`.
#   failed      — a genuine failure (mid-print, or at dispatch) under the retry cap;
#   fault_stop  — an operator stopped a print its printer was already holding;
#   plate_check — the farm stopped a print at the printer's pre-print plate check.
RequeueCause = Literal["failed", "fault_stop", "plate_check"]

# Why a claimed row is being RETURNED — the log vocabulary of :func:`return_to_queue`.
#   refused_commit — the plate gate rose between the claim and the print command;
#   start_watchdog — the printer never started the job it was sent;
#   dead_claim     — a claim nothing will ever retire (``dispatch_claim.judge`` → dead).
ReturnCause = Literal["refused_commit", "start_watchdog", "dead_claim"]

# What :func:`return_to_queue` did with the row.
#   returned  — pending again, at the head of its scope, dispatchable;
#   staged    — pending again, at the head, ``manual_start`` because its run is PAUSED
#               (a pause stages the run's pending rows; this one was not pending then);
#   cancelled — its run is over (aborted or completed), so it will never print —
#               an aborted run must not print one more plate;
#   moved_on  — the row was no longer ``printing``: the print landed or a terminal
#               moved it, so it is somebody else's and nothing was written.
ReturnOutcome = Literal["returned", "staged", "cancelled", "moved_on"]

# The terminal statuses an attempt can be requeued FROM. The queue row's terminal is
# normalised to one of these before any requeue decision (``aborted`` becomes
# ``cancelled`` at ``main.on_print_complete``); ``completed`` produced its plate.
_REQUEUEABLE_STATUSES: frozenset[str] = frozenset({"failed", "cancelled"})

# Run statuses under which a returned claim must never print again.
_ENDED_RUN_STATUSES: frozenset[str] = frozenset({"cancelled", "completed"})

# How far a lineage walk will climb before giving up. A retry chain is bounded by the
# per-unit cap in practice; the guard exists for a corrupted self-referencing chain,
# and its bound is also the answer such a chain produces — no honest lineage is a
# thousand attempts deep, which is what makes the corruption legible.
_LINEAGE_WALK_MAX = 1000


@dataclass(frozen=True, slots=True)
class RequeueResult:
    """The row :func:`requeue_attempt` minted — a value, never the ORM row of a
    session that has already closed."""

    item_id: int
    position: int
    retry_count: int


# --------------------------------------------------------------------------- #
# A new attempt
# --------------------------------------------------------------------------- #
async def requeue_attempt(source_id: int, *, cause: RequeueCause, stage_manual: bool) -> RequeueResult | None:
    """Mint exactly one requeue of the ended attempt ``source_id``, NEXT in line.

    The new row carries the source's SETTINGS through the one allowlist
    (``queue_builder.requeue_fields`` — including ``first_article``: a failed first
    article is re-attempted as a first article), the source's TARGET spread last
    (``target_of(source).fields()``: a pool unit returns to its pool, a pinned unit
    keeps its pin — the failed row's ``printer_id`` is only the record of where the
    attempt ran), and the LINEAGE written here and nowhere else: ``retry_of_id`` (the
    unique, DB-backed idempotency guard) and ``retry_count`` (the generation index the
    run detail renders — NOT the retry cap, which is :func:`failed_ancestor_count`).
    It lands at position 1 of its scope with ``been_jumped`` set. ``stage_manual``
    stages it (``manual_start``) for a paused run; the resume sweep releases it.

    **Its own unit of work.** A fresh session under ``core.database.run_with_retry``
    (the codebase's lock-contention owner), and no savepoint: the scope lock takes
    SQLite's write lock BEFORE the allocation reads (``hold_write_lock``), so a
    concurrent writer — the plate authority's persist task was the production one —
    queues behind it instead of invalidating its snapshot, and two pool requeues can
    no longer read the same ``max(position)``.

    **Caller contract: COMMIT the source row's terminal state first.** This session
    must be able to take the write lock, and it cannot while the caller's own
    transaction holds it — SQLite would park this call on ``busy_timeout`` behind its
    own caller. The source's committed status is therefore this verb's precondition,
    read before any lock: a source that is not ``failed``/``cancelled`` in the
    database is refused with a WARNING and nothing is written.

    Returns None when nothing was minted: the source is gone, not terminal, or already
    requeued (the existence check, or the unique ``retry_of_id`` when another writer
    won the race — both are logged).
    """

    async def _unit_of_work(db: AsyncSession) -> RequeueResult | None:
        source = await db.get(PrintQueueItem, source_id)
        if source is None:
            logger.warning("requeue: unit %s is gone — nothing requeued (cause=%s)", source_id, cause)
            return None
        if source.status not in _REQUEUEABLE_STATUSES:
            logger.warning(
                "requeue: unit %s reads '%s' in the database — only a committed failed/cancelled attempt is "
                "requeued; nothing minted (cause=%s)",
                source_id,
                source.status,
                cause,
            )
            return None
        existing = (
            await db.execute(select(PrintQueueItem.id).where(PrintQueueItem.retry_of_id == source_id))
        ).scalar_one_or_none()
        if existing is not None:
            logger.info(
                "requeue: unit %s was already requeued as %s — nothing minted (cause=%s)", source_id, existing, cause
            )
            return None

        target = target_of(source)
        retry_count = (source.retry_count or 0) + 1
        fields = {
            **requeue_fields(source),
            "status": "pending",
            "manual_start": stage_manual,
            "been_jumped": True,
            "retry_count": retry_count,
            "retry_of_id": source.id,
            # Spread LAST: all three target columns, so the requeue can never wear a
            # leftover column from a kind it does not claim.
            **target.fields(),
        }
        try:
            (created,) = await create_queue_items(
                db, count=1, printer_id=target.printer_id, fields=fields, insert_at_top=True
            )
            await db.commit()
        except IntegrityError:
            await db.rollback()
            logger.info(
                "requeue: unit %s lost the idempotency race (unique retry_of_id) — another writer requeued it "
                "(cause=%s)",
                source_id,
                cause,
            )
            return None
        logger.info(
            "requeue: unit %s requeued as %s (generation %d) at position %d of its scope%s (cause=%s)",
            source_id,
            created.id,
            retry_count,
            created.position,
            ", staged" if stage_manual else "",
            cause,
        )
        return RequeueResult(item_id=created.id, position=created.position, retry_count=retry_count)

    return await run_with_retry(_unit_of_work, label=f"requeue item={source_id}")


# --------------------------------------------------------------------------- #
# The same row, un-claimed
# --------------------------------------------------------------------------- #
async def return_to_queue(db: AsyncSession, item_id: int, *, cause: ReturnCause) -> ReturnOutcome:
    """Return a ``printing`` row whose print never started to the queue, NEXT in line.

    ``queue_transitions.release_unstarted_claim`` performs the storage transition
    (conditional on ``printing``, clearing every dispatch-shaped column and a pool
    row's ``printer_id``); this verb is its only caller and adds what the transition
    deliberately does not know:

    * **the position** — the head of the scope the row now belongs to
      (``queue_builder.seat_at_head``) plus ``been_jumped``, instead of the stale
      number it was claimed from;
    * **the run gate** — the scheduler never reads the run, a pause stages only the
      rows pending AT pause time, and an abort cancels only pending rows, so a claim
      returned under a paused or ended run would dispatch anyway. Paused → ``staged``;
      aborted or completed → cancelled through ``queue_transitions.cancel_pending_items``.

    Does not commit — the caller owns the transaction, as with every transition. The
    caller's instance of the row is refreshed in place (the reload populates the
    session's identity map), so it reads the returned state afterwards.
    """
    if not await queue_transitions.release_unstarted_claim(db, item_id=item_id):
        return "moved_on"

    # The conditional UPDATE ran with ``synchronize_session=False``: reload so the
    # scope is read from the row as it now is (a pool row's printer_id was cleared).
    item = (
        await db.execute(
            select(PrintQueueItem).where(PrintQueueItem.id == item_id).execution_options(populate_existing=True)
        )
    ).scalar_one()
    run_status = (
        (await db.execute(select(PrintBatch.status).where(PrintBatch.id == item.batch_id))).scalar_one_or_none()
        if item.batch_id is not None
        else None
    )

    if run_status in _ENDED_RUN_STATUSES:
        await queue_transitions.cancel_pending_items(db, item_ids=[item_id])
        await db.refresh(item)
        logger.info(
            "requeue: unit %s returned from a dispatch that never started, but its run %s is %s — cancelled, "
            "not requeued (cause=%s)",
            item_id,
            item.batch_id,
            run_status,
            cause,
        )
        return "cancelled"

    await seat_at_head(db, item)
    item.been_jumped = True
    staged = run_status == "paused"
    if staged:
        item.manual_start = True
    logger.info(
        "requeue: unit %s returned to the queue at position %d of its scope%s (cause=%s)",
        item_id,
        item.position,
        f", staged — run {item.batch_id} is paused" if staged else "",
        cause,
    )
    return "staged" if staged else "returned"


# --------------------------------------------------------------------------- #
# A run's deficit
# --------------------------------------------------------------------------- #
async def mint_replacements(
    db: AsyncSession, run: PrintBatch, *, count: int, template: PrintQueueItem
) -> list[PrintQueueItem]:
    """Mint ``count`` lineage-FREE replacement plates for ``run``, NEXT in line.

    The create half of ``production_run.top_up_run`` — the shortfall math stays there.
    A replacement is the SAME plate, so it carries ``template``'s settings through the
    one allowlist and ``template``'s TARGET columns (never the ``printer_id`` a
    dispatched row merely records), spread last. ``first_article`` is forced False —
    a replacement is never the run's first article, whatever the template was — and
    no lineage is written: a replacement is a new primary of the plan, and the run's
    planned-plate count and deficit both count primaries as ``retry_count == 0``.

    The rows take the head of their scope, contiguously, with ``been_jumped`` set.
    Does not commit — the caller owns the transaction.
    """
    target = target_of(template)
    fields = {
        **requeue_fields(template),
        "batch_id": run.id,
        "status": "pending",
        "first_article": False,
        "been_jumped": True,
        **target.fields(),
    }
    return await create_queue_items(db, count=count, printer_id=target.printer_id, fields=fields, insert_at_top=True)


# --------------------------------------------------------------------------- #
# The lineage chain
# --------------------------------------------------------------------------- #
async def _ancestors(db: AsyncSession, item: PrintQueueItem) -> AsyncIterator[PrintQueueItem]:
    """THE walk up a ``retry_of_id`` chain, nearest ancestor first.

    ``db.get`` answers from the identity map when the chain is already loaded (a run's
    ``queue_items``), so walking a loaded run costs no queries. The walk ends at a row
    with no parent, at a parent that no longer exists (``retry_of_id`` is ``ON DELETE
    SET NULL``, and an unsynchronised session can still name a deleted id), or after
    :data:`_LINEAGE_WALK_MAX` steps.
    """
    cursor = item
    for _ in range(_LINEAGE_WALK_MAX):
        if cursor.retry_of_id is None:
            return
        parent = await db.get(PrintQueueItem, cursor.retry_of_id)
        if parent is None:
            return
        yield parent
        cursor = parent


async def lineage_root(db: AsyncSession, item: PrintQueueItem) -> PrintQueueItem:
    """The first attempt of ``item``'s plate — the chain's primary (``item`` itself if it has none)."""
    root = item
    async for ancestor in _ancestors(db, item):
        root = ancestor
    return root


async def failed_ancestor_count(db: AsyncSession, item: PrintQueueItem) -> int:
    """How many GENUINE failures ``item``'s plate has already produced.

    The genuine-failure cap (``farm_retry_max_per_unit``) bounds retries of a plate that
    FAILED — a print that burned filament and produced nothing. It is derived, never
    stored: the ancestors whose status is ``failed``. A lineage-only requeue's ancestor
    is ``cancelled`` and contributes nothing, so a plate the farm refused, or one an
    operator stopped on a fault-held printer, never spends the retry a later genuine
    failure needs — while a genuine failure counts exactly as it always did (an original
    attempt has 0 failed ancestors and gets its retry; that retry has 1).
    """
    return len([ancestor async for ancestor in _ancestors(db, item) if ancestor.status == "failed"])
