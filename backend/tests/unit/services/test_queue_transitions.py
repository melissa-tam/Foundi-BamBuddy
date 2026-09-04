"""Unit tests for the canonical queue-item transitions.

All three transitions live here: ``cancel_pending_items`` (pending→cancelled),
``claim_pending_for_dispatch`` (pending→printing), and the delete pair
``delete_items_unless_printing`` / ``delete_user_items_unless_printing``. The
point of every one of them is that it is correct under CONCURRENCY, so the
central test of each drives two independent sessions over a FILE-backed SQLite database. The suite's usual in-memory DB shares a single
connection between every session, which cannot reproduce the race at all — the
"other" session would see this one's uncommitted work.

Production shape being pinned (2026-08-20, run 112 / item 865 on 010-H2S): the
abort request loaded the run BEFORE the scheduler committed the dispatch, so it
saw the unit as pending and wrote ``status='cancelled'`` a second later. The ORM
emitted only the changed column, leaving the row cancelled while wearing the
dispatcher's ``started_at`` and ``ams_mapping`` — and the printer kept printing.

The delete pair pins the same race one step further along (2026-08-22, run 114 on
001/002/003-H2S): there the request DELETED the rows of three units that were
still printing, and since the row is the only durable link between a live print
and the farm, all three finished as FOREIGN jobs behind a raised plate gate.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import backend.app.models  # noqa: F401 — registers every table on Base.metadata
from backend.app.core.database import Base
from backend.app.models.print_queue import PrintQueueItem
from backend.app.services.queue_transitions import (
    cancel_pending_items,
    claim_pending_for_dispatch,
    delete_items_unless_printing,
    delete_user_items_unless_printing,
    release_unstarted_claim,
)

pytestmark = pytest.mark.unit


@pytest.fixture
async def file_engine(tmp_path):
    """A FILE-backed SQLite engine so two sessions get two real connections."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'queue_transitions.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def session_factory(file_engine):
    """Independent sessions on the file DB — the fork's production shape."""
    return async_sessionmaker(file_engine, class_=AsyncSession, expire_on_commit=False)


async def _seed(session_factory, *statuses: str) -> list[int]:
    """Create one queue item per status; return their ids in order."""
    async with session_factory() as db:
        items = [PrintQueueItem(status=status, position=i) for i, status in enumerate(statuses)]
        db.add_all(items)
        await db.commit()
        return [item.id for item in items]


async def _row(session_factory, item_id: int) -> PrintQueueItem:
    async with session_factory() as db:
        return (await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one()


class TestDispatchRace:
    async def test_an_item_dispatched_in_the_gap_is_not_cancelled(self, session_factory):
        """The incident, reproduced: session A reads pending, B dispatches, A cancels."""
        (item_id,) = await _seed(session_factory, "pending")

        async with session_factory() as session_a, session_factory() as session_b:
            # A loads the item while it is genuinely pending (the abort request's
            # view of the run).
            loaded = (await session_a.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one()
            assert loaded.status == "pending"

            # B is the scheduler: status + started_at + ams_mapping, COMMITTED
            # before the print command goes out (print_scheduler._start_print).
            dispatched = (
                await session_b.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))
            ).scalar_one()
            dispatched.status = "printing"
            dispatched.started_at = datetime(2026, 8, 20, 21, 21, 56, tzinfo=timezone.utc)
            dispatched.ams_mapping = "[1]"
            await session_b.commit()

            # A now runs the transition against its stale view.
            assert await cancel_pending_items(session_a, item_ids=[loaded.id]) == []
            await session_a.commit()

        after = await _row(session_factory, item_id)
        assert after.status == "printing"
        assert after.started_at is not None
        assert after.ams_mapping == "[1]"
        assert after.completed_at is None

    async def test_the_stale_orm_copy_is_not_what_decides(self, session_factory):
        """Even an ORM instance that still SAYS pending cannot force the write."""
        (item_id,) = await _seed(session_factory, "pending")

        async with session_factory() as session_a, session_factory() as session_b:
            loaded = (await session_a.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one()

            other = (await session_b.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one()
            other.status = "completed"
            await session_b.commit()

            # expire_on_commit=False everywhere in this fork: A's copy is still 'pending'.
            assert loaded.status == "pending"
            assert await cancel_pending_items(session_a, item_ids=[item_id]) == []
            await session_a.commit()

        assert (await _row(session_factory, item_id)).status == "completed"


class TestTransition:
    async def test_a_pending_item_transitions_with_terminal_hygiene(self, session_factory):
        (item_id,) = await _seed(session_factory, "pending")
        async with session_factory() as db:
            item = (await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one()
            item.waiting_reason = "filament_short"
            await db.commit()

            assert await cancel_pending_items(db, item_ids=[item_id]) == [item_id]
            await db.commit()

        after = await _row(session_factory, item_id)
        assert after.status == "cancelled"
        assert after.completed_at is not None
        assert after.waiting_reason is None

    async def test_mixed_ids_return_only_the_ones_that_moved(self, session_factory):
        pending_id, printing_id, done_id = await _seed(session_factory, "pending", "printing", "completed")

        async with session_factory() as db:
            moved = await cancel_pending_items(db, item_ids=[pending_id, printing_id, done_id])
            await db.commit()

        assert moved == [pending_id]
        assert (await _row(session_factory, printing_id)).status == "printing"
        assert (await _row(session_factory, done_id)).status == "completed"
        assert (await _row(session_factory, pending_id)).status == "cancelled"

    async def test_all_pending_ids_move_in_one_statement(self, session_factory):
        ids = await _seed(session_factory, "pending", "pending", "pending")
        async with session_factory() as db:
            moved = await cancel_pending_items(db, item_ids=ids)
            await db.commit()
        assert sorted(moved) == sorted(ids)

    async def test_a_second_call_is_a_no_op(self, session_factory):
        """Idempotent by the same precondition — a cancelled row is not pending."""
        (item_id,) = await _seed(session_factory, "pending")
        async with session_factory() as db:
            assert await cancel_pending_items(db, item_ids=[item_id]) == [item_id]
            await db.commit()
            first_completed_at = (await _row(session_factory, item_id)).completed_at

            assert await cancel_pending_items(db, item_ids=[item_id]) == []
            await db.commit()

        assert (await _row(session_factory, item_id)).completed_at == first_completed_at

    async def test_unknown_ids_are_simply_absent(self, session_factory):
        async with session_factory() as db:
            assert await cancel_pending_items(db, item_ids=[999_999]) == []


class TestEmptyInput:
    async def test_empty_ids_returns_without_touching_the_database(self):
        class _ExplodingSession:
            async def execute(self, *_args, **_kwargs):  # pragma: no cover - must not run
                raise AssertionError("cancel_pending_items must not query for an empty id list")

        assert await cancel_pending_items(_ExplodingSession(), item_ids=[]) == []


_CLAIMED_AT = datetime(2026, 8, 21, 9, 15, 0, tzinfo=timezone.utc)
# The printer the dispatch ran on. Required on the claim since the 2026-09-04
# pool-target wave: a POOL unit's row learns its printer HERE and nowhere earlier.
_DISPATCH_PRINTER_ID = 4


class TestDispatchClaim:
    """``pending → printing``: the dispatcher's half of the same lost update."""

    async def test_a_pending_item_is_claimed_with_every_dispatch_column(self, session_factory):
        (item_id,) = await _seed(session_factory, "pending")
        async with session_factory() as db:
            item = (await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one()
            item.waiting_reason = "filament_short"
            item.ams_mapping = "[3, -1]"  # the operator's PIN, pre-dispatch
            await db.commit()

            assert await claim_pending_for_dispatch(
                db,
                item_id=item_id,
                started_at=_CLAIMED_AT,
                ams_mapping="[1, -1]",
                printer_id=_DISPATCH_PRINTER_ID,
            )
            await db.commit()

        after = await _row(session_factory, item_id)
        assert after.status == "printing"
        assert after.started_at.replace(tzinfo=None) == _CLAIMED_AT.replace(tzinfo=None)
        assert after.waiting_reason is None
        # The pin has become the RECORD of the mapping that actually ran.
        assert after.ams_mapping == "[1, -1]"
        # And the same sentence about ``printer_id``: on a pending row it was an
        # operator pin (here: nothing), from this write on it records where the
        # dispatch went. A pool unit's row learns its printer at exactly this point.
        assert after.printer_id == _DISPATCH_PRINTER_ID

    async def test_a_pool_units_printer_is_written_by_the_claim(self, session_factory):
        """The pool row arrives unassigned and leaves carrying the dispatch record.

        The target columns are untouched: they say what the unit may run on, and the
        claim records where it did — the two never merge into one another.
        """
        async with session_factory() as db:
            item = PrintQueueItem(status="pending", position=0, target_printer_ids="[1,4,5]")
            db.add(item)
            await db.commit()
            item_id = item.id
            assert item.printer_id is None

            assert await claim_pending_for_dispatch(
                db,
                item_id=item_id,
                started_at=_CLAIMED_AT,
                ams_mapping=None,
                printer_id=_DISPATCH_PRINTER_ID,
            )
            await db.commit()

        after = await _row(session_factory, item_id)
        assert after.printer_id == _DISPATCH_PRINTER_ID
        assert after.target_printer_ids == "[1,4,5]"

    async def test_a_mapping_free_dispatch_nulls_the_column(self, session_factory):
        (item_id,) = await _seed(session_factory, "pending")
        async with session_factory() as db:
            item = (await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one()
            item.ams_mapping = "[3, -1]"
            await db.commit()

            assert await claim_pending_for_dispatch(
                db,
                item_id=item_id,
                started_at=_CLAIMED_AT,
                ams_mapping=None,
                printer_id=_DISPATCH_PRINTER_ID,
            )
            await db.commit()

        assert (await _row(session_factory, item_id)).ams_mapping is None

    async def test_a_cancelled_item_is_refused_and_left_alone(self, session_factory):
        (item_id,) = await _seed(session_factory, "pending")
        async with session_factory() as db:
            assert await cancel_pending_items(db, item_ids=[item_id]) == [item_id]
            await db.commit()
            cancelled_at = (await _row(session_factory, item_id)).completed_at

            assert not await claim_pending_for_dispatch(
                db,
                item_id=item_id,
                started_at=_CLAIMED_AT,
                ams_mapping="[1]",
                printer_id=_DISPATCH_PRINTER_ID,
            )
            await db.commit()

        after = await _row(session_factory, item_id)
        assert after.status == "cancelled"
        assert after.completed_at == cancelled_at
        assert after.started_at is None
        assert after.ams_mapping is None
        assert after.printer_id is None

    async def test_an_already_printing_item_is_refused(self, session_factory):
        """Double-dispatch protection falls out of the same precondition."""
        (item_id,) = await _seed(session_factory, "printing")
        async with session_factory() as db:
            assert not await claim_pending_for_dispatch(
                db,
                item_id=item_id,
                started_at=_CLAIMED_AT,
                ams_mapping="[1]",
                printer_id=_DISPATCH_PRINTER_ID,
            )
            await db.commit()

        after = await _row(session_factory, item_id)
        assert after.started_at is None  # the second claim wrote nothing
        assert after.printer_id is None

    async def test_an_unknown_id_is_refused(self, session_factory):
        async with session_factory() as db:
            assert not await claim_pending_for_dispatch(
                db,
                item_id=999_999,
                started_at=_CLAIMED_AT,
                ams_mapping=None,
                printer_id=_DISPATCH_PRINTER_ID,
            )


class TestDispatchClaimRace:
    async def test_a_cancel_landing_during_the_upload_wins(self, session_factory):
        """The mirror incident: A is mid-dispatch, B cancels, A must NOT claim.

        Two real connections, in production order: the dispatcher loads the item
        while it is genuinely pending, spends seconds in the FTPS upload, and by
        the time it writes, the operator's cancel has committed. Before the
        transition existed, A's ORM UPDATE put the row back to 'printing' and the
        print command went out anyway.
        """
        (item_id,) = await _seed(session_factory, "pending")

        async with session_factory() as dispatcher, session_factory() as operator:
            loaded = (await dispatcher.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one()
            assert loaded.status == "pending"

            assert await cancel_pending_items(operator, item_ids=[item_id]) == [item_id]
            await operator.commit()

            # expire_on_commit=False everywhere in this fork: the dispatcher's copy
            # still SAYS pending, and that is precisely what must not decide.
            assert loaded.status == "pending"
            assert not await claim_pending_for_dispatch(
                dispatcher,
                item_id=item_id,
                started_at=_CLAIMED_AT,
                ams_mapping="[1]",
                printer_id=_DISPATCH_PRINTER_ID,
            )
            await dispatcher.commit()

        after = await _row(session_factory, item_id)
        assert after.status == "cancelled"
        assert after.completed_at is not None
        assert after.started_at is None


_BATCH_ID = 114  # the incident's run; no PrintBatch row is needed for a column filter
_OTHER_BATCH_ID = 115
_USER_ID = 7
_OTHER_USER_ID = 8


async def _seed_batch(session_factory, batch_id: int, *statuses: str) -> list[int]:
    """Create one queue item per status inside *batch_id*; return their ids."""
    async with session_factory() as db:
        items = [PrintQueueItem(status=status, position=i, batch_id=batch_id) for i, status in enumerate(statuses)]
        db.add_all(items)
        await db.commit()
        return [item.id for item in items]


async def _surviving(session_factory) -> list[int]:
    async with session_factory() as db:
        return sorted((await db.execute(select(PrintQueueItem.id))).scalars().all())


class TestDeleteUnlessPrinting:
    async def test_terminal_items_are_deleted_and_nothing_is_reported(self, session_factory):
        ids = await _seed_batch(session_factory, _BATCH_ID, "completed", "failed", "cancelled", "pending")

        async with session_factory() as db:
            deleted, still_printing = await delete_items_unless_printing(db, batch_id=_BATCH_ID)
            await db.commit()

        assert deleted == len(ids)
        assert still_printing == []
        assert await _surviving(session_factory) == []

    async def test_a_printing_unit_refuses_and_nothing_is_lost(self, session_factory):
        ids = await _seed_batch(session_factory, _BATCH_ID, "printing", "completed", "pending")

        async with session_factory() as db:
            _deleted, still_printing = await delete_items_unless_printing(db, batch_id=_BATCH_ID)
            assert still_printing == [ids[0]]
            # The caller's obligation: the DELETE already ran against this
            # transaction, so only a rollback keeps the terminal siblings.
            await db.rollback()

        assert await _surviving(session_factory) == sorted(ids)

    async def test_the_scope_is_the_batch_and_nothing_wider(self, session_factory):
        mine = await _seed_batch(session_factory, _BATCH_ID, "completed")
        theirs = await _seed_batch(session_factory, _OTHER_BATCH_ID, "completed", "printing")

        async with session_factory() as db:
            deleted, still_printing = await delete_items_unless_printing(db, batch_id=_BATCH_ID)
            await db.commit()

        assert (deleted, still_printing) == (1, [])
        assert await _surviving(session_factory) == sorted(theirs)
        assert mine[0] not in theirs


class TestDeleteUnlessPrintingRace:
    async def test_a_unit_dispatched_in_the_gap_is_not_deleted(self, session_factory):
        """The 2026-08-22 incident, reproduced over two real connections.

        Run 114 was aborted at 02:34:31 and deleted at 02:34:36 with three units
        printing on 001/002/003-H2S. Session A is the delete request: it loads the
        run, and while it is still holding that view session B — the scheduler —
        commits a dispatch. A guard reading A's loaded rows cannot see that write;
        the DELETE's own WHERE clause can, which is the whole point.
        """
        ids = await _seed_batch(session_factory, _BATCH_ID, "pending", "completed")
        dispatched_id = ids[0]

        async with session_factory() as session_a, session_factory() as session_b:
            # A loads the run's items while the unit is genuinely pending.
            loaded = (
                await session_a.execute(select(PrintQueueItem).where(PrintQueueItem.id == dispatched_id))
            ).scalar_one()
            assert loaded.status == "pending"

            # B is the scheduler: status + started_at, COMMITTED before the print
            # command goes out (print_scheduler._start_print).
            racing = (
                await session_b.execute(select(PrintQueueItem).where(PrintQueueItem.id == dispatched_id))
            ).scalar_one()
            racing.status = "printing"
            racing.started_at = datetime(2026, 8, 22, 2, 34, 33, tzinfo=timezone.utc)
            await session_b.commit()

            # expire_on_commit=False everywhere in this fork: A's copy still SAYS
            # pending, and that is precisely what must not decide.
            assert loaded.status == "pending"
            _deleted, still_printing = await delete_items_unless_printing(session_a, batch_id=_BATCH_ID)
            assert still_printing == [dispatched_id]
            await session_a.rollback()

        assert await _surviving(session_factory) == sorted(ids)
        after = await _row(session_factory, dispatched_id)
        assert after.status == "printing"
        assert after.started_at is not None


class TestDeleteUserItemsUnlessPrinting:
    async def test_terminal_items_go_and_other_users_are_untouched(self, session_factory):
        async with session_factory() as db:
            mine = PrintQueueItem(status="completed", position=0, created_by_id=_USER_ID)
            theirs = PrintQueueItem(status="completed", position=1, created_by_id=_OTHER_USER_ID)
            db.add_all([mine, theirs])
            await db.commit()
            theirs_id = theirs.id

        async with session_factory() as db:
            deleted, still_printing = await delete_user_items_unless_printing(db, user_id=_USER_ID)
            await db.commit()

        assert (deleted, still_printing) == (1, [])
        assert await _surviving(session_factory) == [theirs_id]

    async def test_a_printing_unit_refuses_the_whole_user_delete(self, session_factory):
        async with session_factory() as db:
            live = PrintQueueItem(status="printing", position=0, created_by_id=_USER_ID)
            done = PrintQueueItem(status="completed", position=1, created_by_id=_USER_ID)
            db.add_all([live, done])
            await db.commit()
            ids = sorted([live.id, done.id])
            live_id = live.id

        async with session_factory() as db:
            _deleted, still_printing = await delete_user_items_unless_printing(db, user_id=_USER_ID)
            assert still_printing == [live_id]
            await db.rollback()

        assert await _surviving(session_factory) == ids


async def _seed_claimed_pool_row(
    session_factory,
    *,
    target_model: str | None = None,
    target_printer_ids: str | None = None,
) -> int:
    """A ``printing`` row as the dispatcher leaves it: the printer recorded, plus
    whatever target it was dispatched FROM (none = a pinned unit)."""
    async with session_factory() as db:
        item = PrintQueueItem(
            status="printing",
            position=0,
            printer_id=_DISPATCH_PRINTER_ID,
            started_at=_CLAIMED_AT,
            target_model=target_model,
            target_printer_ids=target_printer_ids,
        )
        db.add(item)
        await db.commit()
        return item.id


class TestReleaseUnstartedClaim:
    """``printing → pending``: un-claiming a dispatch that never landed.

    The third face of the same lost update, and the only one whose loser is not a
    person: 001-H2S 2026-08-29, item 1010 was committed ``printing`` at 01:25:13,
    the print never started, and nothing could ever move the row again — no terminal
    echo arrives for a print that never began. The printer sat out 15 hours.

    The precondition matters as much here as anywhere else: the caller decides a
    claim is dead over several ticks of evidence, and the print may genuinely land,
    or complete, inside that window.
    """

    async def test_a_printing_claim_is_released_with_every_dispatch_column_cleared(self, session_factory):
        (item_id,) = await _seed(session_factory, "printing")
        async with session_factory() as db:
            item = (await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one()
            item.started_at = _CLAIMED_AT
            item.ams_mapping = "[1, -1]"
            item.waiting_reason = "stagger_hold"
            await db.commit()

            assert await release_unstarted_claim(db, item_id=item_id) is True
            await db.commit()

        after = await _row(session_factory, item_id)
        assert after.status == "pending"
        assert after.started_at is None
        # The DECIDED mapping must not survive onto a pending row, where the same
        # column means an operator PIN (2026-08-12 pin contract: never a cache).
        assert after.ams_mapping is None
        assert after.waiting_reason is None

    async def test_a_model_targeted_row_goes_back_to_the_pool(self, session_factory):
        """On a POOL row ``printer_id`` was the dispatch RECORD, so un-making the
        dispatch un-makes it: leaving it would hand the next tick a scheduler decision
        wearing the shape of an operator PIN."""
        item_id = await _seed_claimed_pool_row(session_factory, target_model="H2S")

        async with session_factory() as db:
            assert await release_unstarted_claim(db, item_id=item_id) is True
            await db.commit()

        after = await _row(session_factory, item_id)
        assert after.status == "pending"
        assert after.printer_id is None
        assert after.target_model == "H2S"  # the TARGET is untouched — only the record goes

    async def test_a_printer_subset_row_goes_back_to_the_pool(self, session_factory):
        item_id = await _seed_claimed_pool_row(session_factory, target_printer_ids="[1,4,5]")

        async with session_factory() as db:
            assert await release_unstarted_claim(db, item_id=item_id) is True
            await db.commit()

        after = await _row(session_factory, item_id)
        assert after.status == "pending"
        assert after.printer_id is None
        assert after.target_printer_ids == "[1,4,5]"

    async def test_a_pinned_row_keeps_its_pin(self, session_factory):
        """No pool target ⇒ the ``printer_id`` predates the dispatch and is a human's
        instruction. An unwound claim must not silently unpin the unit."""
        item_id = await _seed_claimed_pool_row(session_factory)

        async with session_factory() as db:
            assert await release_unstarted_claim(db, item_id=item_id) is True
            await db.commit()

        after = await _row(session_factory, item_id)
        assert after.status == "pending"
        assert after.printer_id == _DISPATCH_PRINTER_ID
        assert (after.target_model, after.target_printer_ids) == (None, None)

    @pytest.mark.parametrize("status", ["pending", "completed", "cancelled", "failed", "skipped"])
    async def test_only_a_printing_row_moves(self, session_factory, status):
        (item_id,) = await _seed(session_factory, status)
        async with session_factory() as db:
            assert await release_unstarted_claim(db, item_id=item_id) is False
            await db.commit()

        assert (await _row(session_factory, item_id)).status == status

    async def test_an_unknown_id_is_refused(self, session_factory):
        async with session_factory() as db:
            assert await release_unstarted_claim(db, item_id=999_999) is False

    async def test_a_row_that_raced_to_completed_is_untouched(self, session_factory):
        """The race that matters: the watch decided over two ticks, and in the gap
        ``on_print_complete`` moved the row. The caller's reading is stale, and the
        WHERE clause — not the caller — is what decides."""
        (item_id,) = await _seed(session_factory, "printing")

        async with session_factory() as watch, session_factory() as terminal:
            loaded = (await watch.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one()
            assert loaded.status == "printing"

            done = (await terminal.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one()
            done.status = "completed"
            done.completed_at = _CLAIMED_AT
            await terminal.commit()

            # expire_on_commit=False: the watch's copy still SAYS printing.
            assert loaded.status == "printing"
            assert await release_unstarted_claim(watch, item_id=item_id) is False
            await watch.commit()

        after = await _row(session_factory, item_id)
        assert after.status == "completed"
        assert after.completed_at is not None

    async def test_a_second_call_is_a_no_op(self, session_factory):
        """Idempotent by the same precondition: a released row is no longer
        ``printing``, so a duplicate release from a retry or a second tick cannot
        touch it — and, critically, cannot wipe the columns of the NEXT dispatch."""
        (item_id,) = await _seed(session_factory, "printing")
        async with session_factory() as db:
            assert await release_unstarted_claim(db, item_id=item_id) is True
            await db.commit()

            assert await release_unstarted_claim(db, item_id=item_id) is False
            await db.commit()

            # Re-dispatched: the fresh claim's columns stand.
            assert await claim_pending_for_dispatch(
                db,
                item_id=item_id,
                started_at=_CLAIMED_AT,
                ams_mapping="[2]",
                printer_id=_DISPATCH_PRINTER_ID,
            )
            await db.commit()

        after = await _row(session_factory, item_id)
        assert after.status == "printing"
        assert after.ams_mapping == "[2]"
        assert after.started_at is not None
