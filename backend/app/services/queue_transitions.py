"""Canonical queue-item status transitions, evaluated at the STORAGE boundary.

Two transitions live here: ``pending → cancelled`` and its mirror
``pending → printing``. The first exists as a module because it had three
hand-rolled copies (run abort, batch cancel, single-item cancel) and every one of
them was a read-then-write over an ORM row loaded earlier in the request — a lost
update waiting for a dispatch to land in the gap. The second is the SAME lost
update seen from the dispatcher's side, and lives here so both halves of the race
read the state machine's preconditions off one page.

**Why the WHERE clause is the state machine's precondition.** ``pending`` is not
a fact the caller may carry in from an earlier ``SELECT``; between that read and
the write, the scheduler can dispatch the unit (``print_scheduler`` sets
``status='printing'`` + ``started_at`` and COMMITS before it sends the print
command). An ORM write of ``item.status = "cancelled"`` on a row loaded as
pending emits ``UPDATE … SET status='cancelled' WHERE id=?`` — SQLAlchemy sends
only the CHANGED columns, so the dispatcher's ``started_at`` / ``ams_mapping``
survive and the row lands in a state no state machine produces: cancelled, with
a start time, no completion, and a printer physically running it. Production
2026-08-20 21:21 on 010-H2S: run 112's unit was dispatched at 21:21:56 and the
abort request (whose session had loaded the run BEFORE that commit) cancelled it
at 21:21:57. The print ran to completion as a FOREIGN job — no farm unit was
printing, so nothing correlated to the start echo — and the next run's unit
queued behind the un-cleared plate for 16 hours.

Putting ``status == "pending"`` in the WHERE makes the transition correct under
concurrency **by construction**: the database evaluates the precondition and the
write in one atomic statement, and ``RETURNING`` reports which rows actually
moved. No version column, no ``SELECT … FOR UPDATE``, no retry loop — those all
answer the same question later and more expensively. A row that raced away is
simply absent from the result, and the caller decides what that means (the run
abort ignores it: the unit is genuinely printing; the single-item cancel turns it
into the 400 it always meant to raise; the dispatch claim declines to send the
print command at all).

Both supported engines carry UPDATE…RETURNING — SQLite ≥ 3.35 (the embedded
Python 3.13 ships 3.45+) and Postgres — so there is deliberately no dialect
fallback path to drift out of sync with this one.

**Caller obligation:** the UPDATE runs with ``synchronize_session=False``, so ORM
instances the caller already holds keep their pre-transition attribute values
(the fork's sessions are ``expire_on_commit=False``, so a commit does not clear
them either). Refresh or expire what you hold — ``db.refresh(item)``, an explicit
``db.expire(item)`` before a re-``SELECT`` that repopulates it, or a reload
helper like ``production_run._load_run``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import update

from backend.app.models.print_queue import PrintQueueItem

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def cancel_pending_items(db: AsyncSession, *, item_ids: Sequence[int]) -> list[int]:
    """Transition every still-``pending`` item among ``item_ids`` to ``cancelled``.

    The caller's notion of "pending" is only a candidate list; the WHERE clause
    is what decides. Returns the ids that ACTUALLY transitioned — an id that had
    already left ``pending`` (dispatched, completed, cancelled) is simply absent.

    Also stamps ``completed_at`` (the row is terminal) and NULLs
    ``waiting_reason``: a cancelled unit never flows through
    ``farm_policy.on_terminal``, so any scheduler hold token it carried
    (``filament_short``, a capability block, ``stagger_hold`` …) would otherwise
    survive on a terminal row forever.

    Does not commit — the caller owns the transaction boundary, because every
    call site cancels items as part of a larger transition (a run becoming
    ``cancelled``, a batch becoming ``cancelled``).
    """
    ids = list(item_ids)
    if not ids:
        return []

    result = await db.execute(
        update(PrintQueueItem)
        .where(
            PrintQueueItem.id.in_(ids),
            PrintQueueItem.status == "pending",
        )
        .values(
            status="cancelled",
            waiting_reason=None,
            completed_at=datetime.now(timezone.utc),
        )
        .returning(PrintQueueItem.id)
        .execution_options(synchronize_session=False)
    )
    cancelled = [row[0] for row in result.all()]

    if len(cancelled) != len(set(ids)):
        # Not an error: the difference is exactly the rows that raced away (or
        # were never pending). Logged because "the operator cancelled it but it
        # kept printing" is otherwise invisible in triage.
        raced = sorted(set(ids) - set(cancelled))
        logger.info(
            "cancel_pending_items: %d/%d transitioned; not pending at write time: %s",
            len(cancelled),
            len(set(ids)),
            raced,
        )
    return cancelled


async def claim_pending_for_dispatch(
    db: AsyncSession,
    *,
    item_id: int,
    started_at: datetime,
    ams_mapping: str | None,
) -> bool:
    """Claim a still-``pending`` item for THIS dispatch: ``pending → printing``.

    The mirror of :func:`cancel_pending_items`, and the other half of the very
    same lost update. ``print_scheduler._start_print`` loads the item, then spends
    seconds inside an FTPS upload, and only afterwards writes the dispatch. An
    operator cancel landing in that gap (queue cancel, batch cancel, run abort —
    all of which now go through ``cancel_pending_items``) used to be overwritten
    straight back to ``printing`` by the dispatcher's ORM ``UPDATE``, the print
    command went out anyway, and the operator's cancel left no trace whatsoever:
    the unit printed, and nothing in the row said anyone had ever said stop.

    So ``pending`` belongs in the WHERE here exactly as it does on the cancel
    side — the caller's ``item.status`` is a several-seconds-old read of a row
    other sessions can move, never an authority over it. Returns True iff a row
    actually moved; on False the caller MUST NOT send the print command, because
    the unit is either terminal or now somebody else's.

    ``ams_mapping`` arrives already serialised (or ``None`` for a mapping-free
    dispatch): this module writes columns, it does not decide them.

    Does not commit — the caller owns the transaction boundary, and on the
    dispatch path that boundary is load-bearing: the status must be COMMITTED
    before the print command is sent, so that a crash in between leaves a unit
    wrongly marked printing rather than one that silently reprints hours later.
    """
    result = await db.execute(
        update(PrintQueueItem)
        .where(
            PrintQueueItem.id == item_id,
            PrintQueueItem.status == "pending",
        )
        .values(
            status="printing",
            started_at=started_at,
            waiting_reason=None,
            ams_mapping=ams_mapping,
        )
        .returning(PrintQueueItem.id)
        .execution_options(synchronize_session=False)
    )
    # Deliberately silent on the miss: the dispatcher is the only caller and it
    # logs the refusal WITH the status it lost the row to, which is the line
    # triage actually needs. A second line here would only split that story.
    return result.scalar_one_or_none() is not None
