"""Unit tests for the canonical queue-item transitions.

Both halves of one race live here: ``cancel_pending_items`` (pending→cancelled)
and ``claim_pending_for_dispatch`` (pending→printing). The point of either is
that it is correct under CONCURRENCY, so the central test of each drives two
independent sessions over a FILE-backed SQLite database. The suite's usual in-memory DB shares a single
connection between every session, which cannot reproduce the race at all — the
"other" session would see this one's uncommitted work.

Production shape being pinned (2026-08-20, run 112 / item 865 on 010-H2S): the
abort request loaded the run BEFORE the scheduler committed the dispatch, so it
saw the unit as pending and wrote ``status='cancelled'`` a second later. The ORM
emitted only the changed column, leaving the row cancelled while wearing the
dispatcher's ``started_at`` and ``ams_mapping`` — and the printer kept printing.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import backend.app.models  # noqa: F401 — registers every table on Base.metadata
from backend.app.core.database import Base
from backend.app.models.print_queue import PrintQueueItem
from backend.app.services.queue_transitions import cancel_pending_items, claim_pending_for_dispatch

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


class TestDispatchClaim:
    """``pending → printing``: the dispatcher's half of the same lost update."""

    async def test_a_pending_item_is_claimed_with_every_dispatch_column(self, session_factory):
        (item_id,) = await _seed(session_factory, "pending")
        async with session_factory() as db:
            item = (await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one()
            item.waiting_reason = "filament_short"
            item.ams_mapping = "[3, -1]"  # the operator's PIN, pre-dispatch
            await db.commit()

            assert await claim_pending_for_dispatch(db, item_id=item_id, started_at=_CLAIMED_AT, ams_mapping="[1, -1]")
            await db.commit()

        after = await _row(session_factory, item_id)
        assert after.status == "printing"
        assert after.started_at.replace(tzinfo=None) == _CLAIMED_AT.replace(tzinfo=None)
        assert after.waiting_reason is None
        # The pin has become the RECORD of the mapping that actually ran.
        assert after.ams_mapping == "[1, -1]"

    async def test_a_mapping_free_dispatch_nulls_the_column(self, session_factory):
        (item_id,) = await _seed(session_factory, "pending")
        async with session_factory() as db:
            item = (await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one()
            item.ams_mapping = "[3, -1]"
            await db.commit()

            assert await claim_pending_for_dispatch(db, item_id=item_id, started_at=_CLAIMED_AT, ams_mapping=None)
            await db.commit()

        assert (await _row(session_factory, item_id)).ams_mapping is None

    async def test_a_cancelled_item_is_refused_and_left_alone(self, session_factory):
        (item_id,) = await _seed(session_factory, "pending")
        async with session_factory() as db:
            assert await cancel_pending_items(db, item_ids=[item_id]) == [item_id]
            await db.commit()
            cancelled_at = (await _row(session_factory, item_id)).completed_at

            assert not await claim_pending_for_dispatch(db, item_id=item_id, started_at=_CLAIMED_AT, ams_mapping="[1]")
            await db.commit()

        after = await _row(session_factory, item_id)
        assert after.status == "cancelled"
        assert after.completed_at == cancelled_at
        assert after.started_at is None
        assert after.ams_mapping is None

    async def test_an_already_printing_item_is_refused(self, session_factory):
        """Double-dispatch protection falls out of the same precondition."""
        (item_id,) = await _seed(session_factory, "printing")
        async with session_factory() as db:
            assert not await claim_pending_for_dispatch(db, item_id=item_id, started_at=_CLAIMED_AT, ams_mapping="[1]")
            await db.commit()

        after = await _row(session_factory, item_id)
        assert after.started_at is None  # the second claim wrote nothing

    async def test_an_unknown_id_is_refused(self, session_factory):
        async with session_factory() as db:
            assert not await claim_pending_for_dispatch(db, item_id=999_999, started_at=_CLAIMED_AT, ams_mapping=None)


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
                dispatcher, item_id=item_id, started_at=_CLAIMED_AT, ams_mapping="[1]"
            )
            await dispatcher.commit()

        after = await _row(session_factory, item_id)
        assert after.status == "cancelled"
        assert after.completed_at is not None
        assert after.started_at is None
