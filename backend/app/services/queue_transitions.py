"""Canonical queue-item status transitions, evaluated at the STORAGE boundary.

Four transitions live here: ``pending → cancelled``, its mirror
``pending → printing``, the un-claim ``printing → pending`` for a dispatch that
never landed, and the row's outright DELETION. The first exists as a
module because it had three hand-rolled copies (run abort, batch cancel,
single-item cancel) and every one of them was a read-then-write over an ORM row
loaded earlier in the request — a lost update waiting for a dispatch to land in
the gap. The second is the SAME lost update seen from the dispatcher's side, and
lives here so both halves of the race read the state machine's preconditions off
one page. The third is that same read-then-write once more, with the row's
DESTRUCTION as the write instead of its status — and it is the least forgiving of
the three, because a wrongly-cancelled row can still be read afterwards to work
out what happened, and a wrongly-deleted one cannot be read at all.

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
into the 409 it always meant to raise; the dispatch claim declines to send the
print command at all; the delete refuses WHOLESALE and rolls back).

Both supported engines carry UPDATE/DELETE…RETURNING — SQLite ≥ 3.35 (the
embedded Python 3.13 ships 3.45+) and Postgres — so there is deliberately no
dialect fallback path to drift out of sync with this one.

Two query builders sit beside the transitions —
:func:`printing_items_referencing` and :func:`live_prints_blocking` — for the
deletes that destroy a queue item's BACKING ROW rather than the item: an
archive, a library file, a batch. Those statements live in the services that own
those tables, but the question they must ask first is this module's ("what does
the QUEUE say?"), and asking it twice in two shapes is how the guard and the
number shown to the operator drift apart. They are query builders, not
transitions: nothing here writes another table's rows.

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

from sqlalchemy import Exists, delete, select, update

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer

if TYPE_CHECKING:
    from sqlalchemy import ColumnElement
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import InstrumentedAttribute

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


async def release_unstarted_claim(db: AsyncSession, *, item_id: int) -> bool:
    """Un-claim a ``printing`` row whose print never started: ``printing → pending``.

    The inverse of :func:`claim_pending_for_dispatch`, and the only one of the four
    transitions here that is not about a race between two humans-with-intent — it is
    about a claim nobody ever redeemed. ``printing`` is an optimistic status: the
    dispatcher writes it and COMMITS before the print command goes out, so a command
    the printer never acts on leaves a row asserting a print that does not exist.
    That row then seeds the scheduler's ``busy_printers`` set every tick, and the
    printer takes no work for as long as the row stands. Production 2026-08-29,
    001-H2S item 1010: dispatched into a standing AMS fault at 01:25:13, the print
    never started, no terminal could ever arrive for it, and the printer sat out
    15 hours with seven pending units behind it.

    ``printing`` is in the WHERE for the reason the module exists: between the
    caller's decision and this write the print may genuinely have landed, or
    ``on_print_complete`` may have moved the row to a terminal. Returns True iff a
    row actually moved; False means somebody else owns it now and the caller must
    treat its own reading as stale.

    **Every dispatch-shaped column is cleared, and ``ams_mapping`` deliberately so.**
    On a ``printing`` row that value is the DECIDED mapping for a dispatch that is
    being un-made; on a ``pending`` row the same column means an operator PIN
    (2026-08-12 pin contract: "never a cache"). Leaving it would hand the next tick's
    matcher its own previous decision dressed as a human's instruction — the one
    thing that contract forbids. The accepted cost is narrow and stated: a pin an
    operator set on this unit BEFORE it was dispatched is lost, and the matcher
    re-decides from live trays. ``printer_id`` is left alone (it is an assignment,
    not a dispatch record), as is the row's queue position.

    Does not commit — the caller owns the transaction boundary, as with every
    sibling here.
    """
    result = await db.execute(
        update(PrintQueueItem)
        .where(
            PrintQueueItem.id == item_id,
            PrintQueueItem.status == "printing",
        )
        .values(
            status="pending",
            started_at=None,
            ams_mapping=None,
            waiting_reason=None,
        )
        .returning(PrintQueueItem.id)
        .execution_options(synchronize_session=False)
    )
    return result.scalar_one_or_none() is not None


async def delete_items_unless_printing(db: AsyncSession, *, batch_id: int) -> tuple[int, list[int]]:
    """Delete a batch's queue items, refusing WHOLESALE if any of them is ``printing``.

    Returns ``(deleted_count, still_printing_item_ids)``. A non-empty id list means
    the delete must not stand: the caller rolls back and raises its 409. There is
    deliberately no partial outcome — either every one of the batch's items is gone,
    or the transaction is discarded and none of them are.

    **The row is the only durable link to a running print.** ``PrintQueueItem`` is
    what carries ``dispatch_subtask_id``, and ``farm_correlation.resolve_terminal_item``
    matches a printer's terminal echo back to a farm unit by finding the ``printing``
    row holding that id. Deleting the row does not stop the print — it keeps running
    on the printer, with nothing left on this side that knows the job is ours. Hours
    later the terminal echo arrives, finds zero printing candidates, and is classified
    FOREIGN: the plate-clear gate goes up, no eject is armed, nothing dispatches, and
    the printer waits for a human to click "Mark plate as cleared". Production
    2026-08-22: run 114 was aborted at 02:34:31 and hard-deleted at 02:34:36 with
    three units still printing on 001-H2S, 002-H2S and 003-H2S. All three prints ran
    to completion into that severed state, and the three printers idled roughly 27
    printer-hours between them.

    **Why the obvious ORM guard is not the fix.** Refusing on
    ``[it for it in run.queue_items if it.status == "printing"]`` reads rows loaded
    earlier in the request and only then writes — and ``print_scheduler`` sets
    ``status='printing'`` and COMMITS *before* it sends the print command, precisely
    so that a crash in between cannot silently reprint. A unit that dispatches
    between the caller's SELECT and its delete is therefore invisible to that
    comprehension and gets deleted anyway: the same incident reproduced under a
    narrower race — and a narrower race is the worse outcome, because it will be
    believed fixed. So ``status <> 'printing'`` belongs in the DELETE's own WHERE,
    where the database evaluates the precondition and performs the write as one
    statement. No version column, no ``SELECT … FOR UPDATE``, no retry loop.

    The survivors are then read back inside the SAME transaction. That list reports
    what the DELETE declined to touch, which is the only honest answer to "may this
    delete stand?" — and because ANY survivor refuses, a row that reached this scope
    by some route the module did not anticipate fails safe: it blocks the delete
    instead of being quietly destroyed.

    Does not commit — the caller owns the transaction boundary, as with both
    siblings above. On the refusal path the caller MUST ``rollback()``: the DELETE
    has already run against the transaction, so merely raising would leave the
    terminal items destroyed by whatever commits that session next.
    """
    return await _delete_unless_printing(db, scope=PrintQueueItem.batch_id == batch_id, scope_label=f"batch {batch_id}")


async def delete_user_items_unless_printing(db: AsyncSession, *, user_id: int) -> tuple[int, list[int]]:
    """The same refusal, scoped to one user's items rather than one batch.

    ``DELETE /users/{id}?delete_items=true`` wipes every queue item a user created,
    fleet-wide and across every run — so it carries the 2026-08-22 hazard documented
    on :func:`delete_items_unless_printing` at a strictly wider blast radius, and can
    reach live prints the deleting admin never saw. Same contract, same wholesale
    refusal; the scope predicate is the only difference, so the precondition cannot
    drift between the two call sites.
    """
    return await _delete_unless_printing(
        db, scope=PrintQueueItem.created_by_id == user_id, scope_label=f"user {user_id}"
    )


async def _delete_unless_printing(
    db: AsyncSession,
    *,
    scope: ColumnElement[bool],
    scope_label: str,
) -> tuple[int, list[int]]:
    """Shared body for the two delete scopes: destroy everything in *scope* that is
    not ``printing``, then report what survived.

    Private because ``scope`` is a raw SQL predicate: the two public wrappers name
    the only scopes the farm deletes queue items by, so a third one cannot be
    invented at a call site without being declared here first.
    """
    deleted = await db.execute(
        delete(PrintQueueItem)
        .where(scope, PrintQueueItem.status != "printing")
        .returning(PrintQueueItem.id)
        .execution_options(synchronize_session=False)
    )
    deleted_ids = [row[0] for row in deleted.all()]

    survived = await db.execute(select(PrintQueueItem.id).where(scope))
    still_printing = sorted(row[0] for row in survived.all())

    if still_printing:
        # The refusal is the whole point of the module, so it is never silent:
        # "the operator asked to delete and nothing happened" is otherwise
        # indistinguishable from a bug in triage.
        logger.info(
            "delete_unless_printing: refusing %s — %d item(s) printing at write time: %s",
            scope_label,
            len(still_printing),
            still_printing,
        )
    return len(deleted_ids), still_printing


def printing_items_referencing(ref_col: InstrumentedAttribute[int | None]) -> Exists:
    """A correlated ``EXISTS`` over ``printing`` queue items whose *ref_col* names the outer row.

    The predicate a bulk delete of a queue item's BACKING ROW puts in its own
    WHERE, so the database evaluates "is a print still running off this row?"
    and performs the delete as one statement — the same construction the three
    transitions above use, moved one table outwards. ``ref_col`` is the
    ``PrintQueueItem`` column that points at the table being deleted from
    (``archive_id``, ``library_file_id``, ``batch_id``); the outer table is read
    off that column's DECLARED foreign key rather than passed in, so the
    correlation can only ever name the table the schema says the column
    references.

    **It must be negated as a correlated NOT EXISTS, never rewritten as
    ``NOT IN (subquery)``.** The two look interchangeable and are not: SQL's
    ``NOT IN`` is ``<> ALL``, and a single NULL among the subquery's rows makes
    every comparison UNKNOWN, so the predicate is never true and the DELETE
    silently matches nothing. Every one of these columns is nullable BY DESIGN —
    ``print_queue`` rows carry either ``archive_id`` or ``library_file_id`` and
    NULL the other, and ``batch_id`` is NULL on every un-batched item — so one
    ordinary row is enough to turn the whole delete into a no-op that raises
    nothing, logs nothing, and returns 204. That is precisely the class of
    invisible failure this module exists to remove, which is why the shape is
    fixed here once instead of being spelled out at each call site.

    Raises ``ValueError`` when *ref_col* declares no single foreign key: there is
    then no outer row to correlate to, and a predicate that silently correlated
    to nothing would be the no-op again.
    """
    foreign_keys = ref_col.property.columns[0].foreign_keys
    if len(foreign_keys) != 1:
        raise ValueError(
            f"printing_items_referencing({ref_col}) needs exactly one declared foreign key "
            f"to name the outer row; found {len(foreign_keys)}"
        )
    target = next(iter(foreign_keys)).column
    return (
        select(1)
        .select_from(PrintQueueItem)
        .where(PrintQueueItem.status == "printing", ref_col == target)
        # Explicit: the outer table is a CORRELATION, not a second FROM. Left to
        # auto-correlation this reads identically until some caller nests it
        # somewhere the guess differs, and then it is a cross join that matches
        # every row.
        .correlate(target.table)
        .exists()
    )


async def live_prints_blocking(db: AsyncSession, *, scope: ColumnElement[bool]) -> list[int]:
    """The ids of ``printing`` queue items that satisfy *scope*.

    ONE origin for "which live prints stand in the way of this delete?", so the
    guard a caller enforces and the number it reports to the operator beforehand
    can never disagree. Three callers ask it of three different scopes: the
    archive delete route (one archive), its ``delete-impact`` pre-flight (the
    same archive), and ``user_deletion`` (a union across every archive, library
    file and batch the user owns).

    Read this AFTER the delete, inside the SAME transaction, when the answer must
    be authoritative: a row that appears between the caller's read and its write
    is exactly the race the module is about, and only a post-delete read sees it.
    Read BEFORE, as the pre-flight endpoints do, it is a forecast — honest at the
    instant it was taken and never a substitute for the guard.
    """
    result = await db.execute(select(PrintQueueItem.id).where(scope, PrintQueueItem.status == "printing"))
    return sorted(row[0] for row in result.all())


async def printing_units_conflict(
    db: AsyncSession,
    item_ids: Sequence[int],
    *,
    code: str,
    subject: str,
) -> dict[str, object]:
    """Build the structured refusal body for a delete the live units blocked.

    ONE implementation, because both refusing callers must speak in the same
    terms: a service (``production_run.delete_production_run``) and a route
    (``api.routes.users.delete_user``) share it, and a second copy would be a
    second sentence to keep in step with the frontend's i18n. *code* and
    *subject* are the only things that differ — what was asked to be deleted.

    Resolves the PRINTER NAMES behind the refusing ids, because a printer name is
    the only part of this an operator can act on; an item id names nothing they
    can walk up to. Shaped ``{code, message, printers}`` after this fork's other
    coded refusals (``foreign_plate``, ``bed_hot``, the slot re-check undo): the
    UI renders the sentence from ``code`` via i18n and interpolates ``printers``,
    while ``message`` is the English fallback for curl and scripts.

    Call this on the REFUSAL path only, AFTER the caller has rolled back. The
    refusal itself was already decided atomically inside
    :func:`_delete_unless_printing`; this is a second, later read purely to put
    names on it, so a printer renamed between the two can only mis-name a printer
    in the sentence — it can never change the verdict.
    """
    rows = await db.execute(
        select(Printer.name)
        .select_from(PrintQueueItem)
        .join(Printer, Printer.id == PrintQueueItem.printer_id)
        .where(PrintQueueItem.id.in_(list(item_ids)))
    )
    names = sorted({name for (name,) in rows.all() if name})
    # A ``printing`` row always carries a printer_id, so the nameless form is
    # unreachable in practice — but it refuses just as hard when it is reached,
    # rather than emitting a sentence with a hole where the printers should be.
    where = f" on {', '.join(names)}" if names else ""
    return {
        "code": code,
        "message": f"{subject} still has units printing{where}. Stop them or wait for them to finish, then delete.",
        "printers": names,
    }
