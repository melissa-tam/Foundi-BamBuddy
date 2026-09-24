"""``services/requeue.py`` — THE owner of "put a plate back in the queue" (2026-09-24).

Two operator rulings are pinned here. Every automatic requeue lands NEXT in line — the
head of its target's position scope, ``been_jumped`` set so the shortest-job-first
ordering serves it first too — where it used to append at ``max + 1`` behind a whole
run. And every path that puts a plate back goes through this one module, so a claim
returned under a paused or aborted run no longer dispatches as if the run were live.

The last class runs on a FILE database under production's connection pragmas (WAL +
``busy_timeout``), because the lost requeues of 2026-09-06..24 were a LOCKING failure:
a read made inside an open savepoint pinned a snapshot, the plate authority's persist
task committed, and SQLite refused to upgrade the stale snapshot to a writer.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core import database as core_db
from backend.app.core.database import hold_write_lock
from backend.app.models.print_batch import PrintBatch
from backend.app.models.print_queue import PrintQueueItem
from backend.app.services import queue_builder, requeue
from backend.app.services.dispatch_target import encode_printer_ids
from backend.app.services.print_scheduler import pending_queue_order

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _requeue_sessions(own_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch) -> None:
    """``requeue_attempt`` is its own unit of work: it opens its session through
    ``core.database.async_session`` (via ``run_with_retry``), so point that at the
    test engine."""
    monkeypatch.setattr(core_db, "async_session", own_session_factory)


async def _batch(db: AsyncSession, *, status: str = "active") -> PrintBatch:
    batch = PrintBatch(name="run", quantity=4, status=status)
    db.add(batch)
    await db.commit()
    await db.refresh(batch)
    return batch


async def _row(db: AsyncSession, *, position: int, status: str = "pending", **columns: Any) -> PrintQueueItem:
    item = PrintQueueItem(position=position, status=status, plate_id=1, **columns)
    if status in ("failed", "cancelled", "completed"):
        item.completed_at = datetime.now(timezone.utc)
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


async def _fresh(db: AsyncSession, item_id: int) -> PrintQueueItem:
    """The row as the DATABASE holds it — never an identity-mapped instance another
    session's commit could have left stale."""
    db.expunge_all()
    row = await db.get(PrintQueueItem, item_id)
    assert row is not None
    return row


async def _positions(db: AsyncSession, ids: list[int]) -> list[int]:
    db.expunge_all()
    rows = (await db.execute(select(PrintQueueItem).where(PrintQueueItem.id.in_(ids)))).scalars().all()
    by_id = {row.id: row.position for row in rows}
    return [by_id[i] for i in ids]


# --------------------------------------------------------------------------- #
# requeue_attempt — a NEW attempt, next in line
# --------------------------------------------------------------------------- #
class TestRequeueAttemptLandsNextInLine:
    async def test_a_pinned_requeue_takes_position_one_ahead_of_its_scope(self, db_session):
        batch = await _batch(db_session)
        waiting = [await _row(db_session, position=p, printer_id=3, batch_id=batch.id) for p in (1, 2)]
        pool_row = await _row(db_session, position=1, target_model="H2S", batch_id=batch.id)
        source = await _row(db_session, position=9, status="failed", printer_id=3, batch_id=batch.id)

        result = await requeue.requeue_attempt(source.id, cause="failed", stage_manual=False)

        assert result is not None
        assert result.position == 1
        new = await _fresh(db_session, result.item_id)
        assert (new.status, new.printer_id, new.position) == ("pending", 3, 1)
        assert new.been_jumped is True  # SJF serves it first as well
        assert (new.retry_of_id, new.retry_count) == (source.id, 1)
        assert new.manual_start is False
        # The scope's waiting rows moved back one; the OTHER scope was not touched.
        assert await _positions(db_session, [w.id for w in waiting]) == [2, 3]
        assert await _positions(db_session, [pool_row.id]) == [1]

    async def test_a_pool_requeue_heads_the_one_shared_null_printer_sequence(self, db_session):
        """Model pools, printer-set pools and unassigned rows share ONE sequence, so a
        pool requeue goes ahead of all of them — and leaves pinned scopes alone."""
        batch = await _batch(db_session)
        other_pool = await _row(db_session, position=1, target_printer_ids=encode_printer_ids([1, 2]))
        unassigned = await _row(db_session, position=2)
        pinned = await _row(db_session, position=1, printer_id=7)
        # A dispatched pool unit: ``printer_id`` is only the RECORD of where it ran.
        source = await _row(
            db_session, position=5, status="failed", printer_id=7, target_model="H2S", batch_id=batch.id
        )

        result = await requeue.requeue_attempt(source.id, cause="failed", stage_manual=False)

        assert result is not None
        new = await _fresh(db_session, result.item_id)
        assert (new.printer_id, new.target_model, new.position) == (None, "H2S", 1)
        assert await _positions(db_session, [other_pool.id, unassigned.id]) == [2, 3]
        assert await _positions(db_session, [pinned.id]) == [1]

    async def test_stage_manual_stages_it(self, db_session):
        source = await _row(db_session, position=1, status="failed", printer_id=3)

        result = await requeue.requeue_attempt(source.id, cause="failed", stage_manual=True)

        assert result is not None
        assert (await _fresh(db_session, result.item_id)).manual_start is True

    async def test_a_second_requeue_of_the_same_attempt_mints_nothing(self, db_session):
        source = await _row(db_session, position=1, status="cancelled", printer_id=3)

        first = await requeue.requeue_attempt(source.id, cause="fault_stop", stage_manual=False)
        second = await requeue.requeue_attempt(source.id, cause="fault_stop", stage_manual=False)

        assert first is not None
        assert second is None
        retries = (
            await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.retry_of_id == source.id))
        ).all()
        assert len(retries) == 1

    @pytest.mark.parametrize("status", ["printing", "pending", "completed"])
    async def test_an_attempt_whose_terminal_is_not_committed_is_refused(self, db_session, status):
        """The caller contract, enforced: requeue only a COMMITTED failed/cancelled
        attempt. A source still reading ``printing`` means the caller has not committed
        its terminal — this refuses at once instead of waiting on the caller's own lock."""
        source = await _row(db_session, position=1, status=status, printer_id=3)

        assert await requeue.requeue_attempt(source.id, cause="failed", stage_manual=False) is None

        rows = (await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.retry_of_id == source.id))).all()
        assert rows == []

    async def test_a_missing_attempt_mints_nothing(self, db_session):
        assert await requeue.requeue_attempt(424242, cause="failed", stage_manual=False) is None

    async def test_the_idempotency_race_loser_mints_nothing_and_moves_nothing(
        self, db_session, own_session_factory, monkeypatch
    ):
        """R4 as it really happens: another writer commits the same requeue between this
        verb's existence check and its INSERT. The unique ``retry_of_id`` refuses the
        second row, and the rollback also undoes the head-of-line shift."""
        source = await _row(db_session, position=1, status="failed", printer_id=3)
        waiting = await _row(db_session, position=1, printer_id=3)
        real_create = queue_builder.create_queue_items

        async def _another_writer_wins_first(db: AsyncSession, **kwargs: Any) -> list[PrintQueueItem]:
            async with own_session_factory() as rival:
                rival.add(PrintQueueItem(position=50, status="pending", printer_id=3, retry_of_id=source.id))
                await rival.commit()
            return await real_create(db, **kwargs)

        monkeypatch.setattr(requeue, "create_queue_items", _another_writer_wins_first)

        assert await requeue.requeue_attempt(source.id, cause="failed", stage_manual=False) is None
        assert await _positions(db_session, [waiting.id]) == [1]
        rows = (await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.retry_of_id == source.id))).all()
        assert len(rows) == 1  # the rival's, and only it

    async def test_the_requeue_carries_the_target_never_the_attribution(self, db_session):
        source = await _row(
            db_session,
            position=1,
            status="failed",
            printer_id=9,
            target_printer_ids=encode_printer_ids([9, 11]),
            ams_mapping="[0,-1,-1,-1]",
        )

        result = await requeue.requeue_attempt(source.id, cause="failed", stage_manual=False)

        assert result is not None
        new = await _fresh(db_session, result.item_id)
        assert new.printer_id is None
        assert new.target_printer_ids == encode_printer_ids([9, 11])
        assert new.ams_mapping is None  # a past-pending mapping is a decision, never a pin


# --------------------------------------------------------------------------- #
# return_to_queue — the SAME row, un-claimed
# --------------------------------------------------------------------------- #
async def _claim(db: AsyncSession, **columns: Any) -> PrintQueueItem:
    return await _row(
        db,
        status="printing",
        started_at=datetime(2026, 9, 24, 15, 49, 0),
        ams_mapping="[0,-1,-1,-1]",
        dispatch_subtask_id="1802207420",
        **columns,
    )


class TestReturnToQueue:
    async def test_a_returned_pinned_claim_heads_its_scope(self, db_session):
        claim = await _claim(db_session, position=7, printer_id=5)
        waiting = [await _row(db_session, position=p, printer_id=5) for p in (1, 2)]

        outcome = await requeue.return_to_queue(db_session, claim.id, cause="start_watchdog")
        await db_session.commit()

        assert outcome == "returned"
        row = await _fresh(db_session, claim.id)
        assert (row.status, row.printer_id, row.position, row.been_jumped) == ("pending", 5, 1, True)
        assert (row.started_at, row.ams_mapping) == (None, None)
        assert await _positions(db_session, [w.id for w in waiting]) == [2, 3]

    async def test_a_returned_pool_claim_heads_the_shared_sequence(self, db_session):
        """The release clears a pool row's ``printer_id`` (it was the dispatch's record,
        and on a pending row that column means an operator PIN) — so the row is seated
        in the NULL-printer scope it now belongs to, not the printer it left."""
        claim = await _claim(db_session, position=4, printer_id=5, target_model="H2S")
        pool_waiting = await _row(db_session, position=1, target_model="H2S")
        pinned_waiting = await _row(db_session, position=1, printer_id=5)

        outcome = await requeue.return_to_queue(db_session, claim.id, cause="dead_claim")
        await db_session.commit()

        assert outcome == "returned"
        row = await _fresh(db_session, claim.id)
        assert (row.printer_id, row.position) == (None, 1)
        assert await _positions(db_session, [pool_waiting.id, pinned_waiting.id]) == [2, 1]

    async def test_a_paused_runs_claim_comes_back_staged(self, db_session):
        """A pause stages only the rows pending AT pause time; a claim returned after it
        would otherwise dispatch into a paused run."""
        batch = await _batch(db_session, status="paused")
        claim = await _claim(db_session, position=2, printer_id=5, batch_id=batch.id)

        assert await requeue.return_to_queue(db_session, claim.id, cause="refused_commit") == "staged"
        await db_session.commit()

        row = await _fresh(db_session, claim.id)
        assert (row.status, row.manual_start, row.position) == ("pending", True, 1)

    @pytest.mark.parametrize("run_status", ["cancelled", "completed"])
    async def test_an_ended_runs_claim_is_cancelled_not_requeued(self, db_session, run_status):
        """An abort cancels only PENDING rows, so a claim returned afterwards used to go
        straight back into dispatch: the aborted run printed one more plate."""
        batch = await _batch(db_session, status=run_status)
        claim = await _claim(db_session, position=3, printer_id=5, batch_id=batch.id)
        waiting = await _row(db_session, position=1, printer_id=5)

        assert await requeue.return_to_queue(db_session, claim.id, cause="start_watchdog") == "cancelled"
        await db_session.commit()

        row = await _fresh(db_session, claim.id)
        assert row.status == "cancelled"
        assert row.completed_at is not None
        assert await _positions(db_session, [waiting.id]) == [1]  # nobody was shifted for it

    async def test_a_row_that_moved_on_is_left_alone(self, db_session):
        done = await _row(db_session, position=3, status="completed", printer_id=5)

        assert await requeue.return_to_queue(db_session, done.id, cause="dead_claim") == "moved_on"
        await db_session.commit()

        row = await _fresh(db_session, done.id)
        assert (row.status, row.position) == ("completed", 3)

    async def test_the_callers_instance_reads_the_returned_state(self, db_session):
        """Callers hold the row they un-claimed; the verb refreshes it in place so no
        later read on that session believes the dispatch still stands."""
        claim = await _claim(db_session, position=6, printer_id=5)

        await requeue.return_to_queue(db_session, claim.id, cause="refused_commit")

        assert (claim.status, claim.position, claim.started_at) == ("pending", 1, None)


# --------------------------------------------------------------------------- #
# mint_replacements — a run's deficit, lineage-free
# --------------------------------------------------------------------------- #
class TestMintReplacements:
    async def test_replacements_head_their_scope_contiguously_and_carry_no_lineage(self, db_session):
        batch = await _batch(db_session)
        waiting = await _row(db_session, position=1, target_model="H2S", batch_id=batch.id)
        template = await _row(
            db_session,
            position=2,
            status="cancelled",
            printer_id=4,  # the record of where it ran — never the replacement's pin
            target_model="H2S",
            batch_id=batch.id,
            first_article=True,
            skip_filament_check=True,
            retry_count=2,
            retry_of_id=None,
        )

        created = await requeue.mint_replacements(db_session, batch, count=2, template=template)
        await db_session.commit()

        rows = [await _fresh(db_session, item.id) for item in created]
        assert [row.position for row in rows] == [1, 2]
        for row in rows:
            assert (row.status, row.printer_id, row.target_model) == ("pending", None, "H2S")
            assert (row.retry_of_id, row.retry_count) == (None, 0)  # primaries of the plan
            assert row.first_article is False
            assert row.been_jumped is True
            assert row.skip_filament_check is True  # the same plate's settings
        assert await _positions(db_session, [waiting.id]) == [3]


# --------------------------------------------------------------------------- #
# The one lineage walk
# --------------------------------------------------------------------------- #
class TestLineage:
    async def _chain(self, db: AsyncSession, statuses: list[str]) -> list[PrintQueueItem]:
        rows: list[PrintQueueItem] = []
        for generation, status in enumerate(statuses):
            rows.append(
                await _row(
                    db,
                    position=10 + generation,
                    status=status,
                    printer_id=3,
                    retry_count=generation,
                    retry_of_id=rows[-1].id if rows else None,
                )
            )
        return rows

    async def test_the_root_is_the_first_attempt(self, db_session):
        chain = await self._chain(db_session, ["failed", "cancelled", "pending"])
        assert (await requeue.lineage_root(db_session, chain[-1])).id == chain[0].id
        assert (await requeue.lineage_root(db_session, chain[0])).id == chain[0].id

    async def test_only_failed_ancestors_count_toward_the_cap(self, db_session):
        """A lineage-only requeue (a cancelled ancestor) never spends the retry."""
        chain = await self._chain(db_session, ["cancelled", "failed", "cancelled", "failed"])
        assert await requeue.failed_ancestor_count(db_session, chain[-1]) == 1
        assert await requeue.failed_ancestor_count(db_session, chain[0]) == 0

    async def test_a_deleted_ancestor_ends_the_walk(self, db_session):
        chain = await self._chain(db_session, ["failed", "failed", "failed"])
        await db_session.delete(chain[1])
        await db_session.commit()
        db_session.expunge_all()
        tail = await db_session.get(PrintQueueItem, chain[2].id)
        assert (await requeue.lineage_root(db_session, tail)).id == tail.id
        assert await requeue.failed_ancestor_count(db_session, tail) == 0

    async def test_a_self_referencing_chain_ends_at_the_walk_guard(self, db_session):
        (row,) = await self._chain(db_session, ["failed"])
        row.retry_of_id = row.id
        await db_session.commit()
        assert await requeue.failed_ancestor_count(db_session, row) == requeue._LINEAGE_WALK_MAX
        assert (await requeue.lineage_root(db_session, row)).id == row.id


# --------------------------------------------------------------------------- #
# The dispatcher's order — "next" means the same thing in both orderings
# --------------------------------------------------------------------------- #
class TestTheSchedulerServesARequeuedPlateFirst:
    async def _order(self, db: AsyncSession, *, shortest_first: bool) -> list[int]:
        db.expunge_all()
        rows = await db.execute(
            select(PrintQueueItem.id)
            .where(PrintQueueItem.status == "pending")
            .order_by(*pending_queue_order(shortest_first=shortest_first))
        )
        return list(rows.scalars().all())

    async def test_shortest_job_first_serves_the_requeue_before_a_shorter_plate(self, db_session):
        short = await _row(db_session, position=1, target_model="H2S", print_time_seconds=600)
        source = await _row(db_session, position=9, status="failed", target_model="H2S", print_time_seconds=36_000)

        result = await requeue.requeue_attempt(source.id, cause="failed", stage_manual=False)

        assert result is not None
        assert await self._order(db_session, shortest_first=True) == [result.item_id, short.id]

    async def test_position_order_serves_the_requeue_first(self, db_session):
        waiting = await _row(db_session, position=1, printer_id=2)
        claim = await _claim(db_session, position=8, printer_id=2)

        await requeue.return_to_queue(db_session, claim.id, cause="start_watchdog")
        await db_session.commit()

        assert await self._order(db_session, shortest_first=False) == [claim.id, waiting.id]

    async def test_equal_positions_break_on_id(self, db_session):
        first = await _row(db_session, position=3, target_model="H2S")
        second = await _row(db_session, position=3, target_model="H2S")
        assert await self._order(db_session, shortest_first=False) == [first.id, second.id]

    @pytest.mark.parametrize("shortest_first", [False, True])
    async def test_null_printer_placement_is_stated_not_left_to_the_dialect(self, shortest_first):
        """SQLite sorts NULL first by default and PostgreSQL last — the pool must head
        the queue on both, so the order says so in the SQL itself."""
        sql = str(
            select(PrintQueueItem.id)
            .order_by(*pending_queue_order(shortest_first=shortest_first))
            .compile(dialect=postgresql.dialect())
        )
        assert "print_queue.printer_id NULLS FIRST" in sql
        assert sql.rstrip().endswith("print_queue.id ASC")


# --------------------------------------------------------------------------- #
# The write lock, under production's pragmas
# --------------------------------------------------------------------------- #
class TestTheWriteLockUnderWal:
    @pytest.fixture(autouse=True)
    def _wal_sessions(self, wal_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(core_db, "async_session", wal_session_factory)

    @staticmethod
    async def _in_transaction(db: AsyncSession) -> bool:
        raw = await (await db.connection()).get_raw_connection()
        return bool(raw.driver_connection.in_transaction)

    async def test_the_old_shape_a_read_inside_a_savepoint_cannot_write_after_another_commit(self, wal_session_factory):
        """The 2026-09-06..24 failure, reproduced: the SAVEPOINT opens a deferred
        transaction, the read pins its snapshot, another connection commits, and the
        write fails AT ONCE — no busy wait can save a stale snapshot."""
        async with wal_session_factory() as a, wal_session_factory() as b:
            with pytest.raises(OperationalError, match="database is locked"):
                async with a.begin_nested():
                    await a.execute(select(PrintQueueItem.position))
                    b.add(PrintQueueItem(position=1, status="pending"))
                    await b.commit()
                    a.add(PrintQueueItem(position=2, status="pending"))
                    await a.flush()

    async def test_holding_the_lock_first_serializes_the_read_and_the_write(self, wal_session_factory):
        """The fix: the lock is taken BEFORE the read, so a concurrent writer waits on
        ``busy_timeout`` instead of invalidating the reader — and reads a max that
        already includes this write."""
        async with wal_session_factory() as a:
            await hold_write_lock(a)
            before = (await a.execute(select(PrintQueueItem.position))).scalars().all()
            assert before == []

            async def _other_writer() -> None:
                async with wal_session_factory() as b:
                    await queue_builder.create_queue_items(b, count=1, printer_id=None, fields={"status": "pending"})
                    await b.commit()

            other = asyncio.create_task(_other_writer())
            await asyncio.sleep(0.3)
            assert not other.done(), "the second writer must queue behind the held lock"

            a.add(PrintQueueItem(position=1, status="pending"))
            await a.commit()
            await asyncio.wait_for(other, timeout=10)

        async with wal_session_factory() as check:
            positions = sorted((await check.execute(select(PrintQueueItem.position))).scalars().all())
        assert positions == [1, 2]  # the waiter read the committed max, no duplicate

    async def test_two_concurrent_tail_appends_take_distinct_positions(self, wal_session_factory, monkeypatch):
        """What the scope lock is FOR on SQLite: an append reads ``max(position)`` and
        writes ``max + 1``. Stretch the gap between that read and the write and, without
        the lock, both writers read the same max and write the same number."""
        real_allocate = queue_builder.allocate_queue_positions

        async def _slow_allocate(db: AsyncSession, **kwargs: Any) -> int:
            position = await real_allocate(db, **kwargs)
            await asyncio.sleep(0.2)  # the other writer gets every chance to read now
            return position

        monkeypatch.setattr(queue_builder, "allocate_queue_positions", _slow_allocate)

        async def _append(plate: int) -> None:
            async with wal_session_factory() as db:
                await queue_builder.create_queue_items(
                    db, count=1, printer_id=None, fields={"status": "pending", "plate_id": plate}
                )
                await db.commit()

        await asyncio.gather(_append(1), _append(2))

        async with wal_session_factory() as check:
            positions = sorted((await check.execute(select(PrintQueueItem.position))).scalars().all())
        assert positions == [1, 2]

    async def test_commit_and_rollback_end_the_immediate_transaction(self, wal_session_factory):
        async with wal_session_factory() as a:
            await hold_write_lock(a)
            assert await self._in_transaction(a)
            a.add(PrintQueueItem(position=1, status="pending", plate_id=1))
            await a.commit()
            assert not await self._in_transaction(a)

            await hold_write_lock(a)
            a.add(PrintQueueItem(position=2, status="pending", plate_id=2))
            await a.rollback()
            assert not await self._in_transaction(a)

        async with wal_session_factory() as check:
            plates = (await check.execute(select(PrintQueueItem.plate_id))).scalars().all()
        assert plates == [1]

    async def test_inside_a_write_transaction_it_changes_nothing(self, wal_session_factory):
        async with wal_session_factory() as a:
            a.add(PrintQueueItem(position=1, status="pending"))
            await a.flush()  # the driver opened a transaction for the INSERT
            assert await self._in_transaction(a)
            await hold_write_lock(a)  # no nested BEGIN, no error
            await a.commit()

    async def test_off_sqlite_it_touches_nothing(self):
        class _PostgresSession:
            def get_bind(self) -> SimpleNamespace:
                return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

            async def connection(self) -> None:
                raise AssertionError("PostgreSQL needs no write lock from this helper")

        await hold_write_lock(_PostgresSession())  # type: ignore[arg-type]

    async def test_a_requeue_waits_out_a_writer_holding_the_lock(self, wal_session_factory):
        async with wal_session_factory() as seed:
            source = PrintQueueItem(position=1, status="failed", printer_id=3)
            seed.add(source)
            await seed.commit()

        async with wal_session_factory() as writer:
            await writer.execute(text("BEGIN IMMEDIATE"))
            await writer.execute(text("UPDATE printers SET name = name"))
            task = asyncio.create_task(requeue.requeue_attempt(source.id, cause="failed", stage_manual=False))
            await asyncio.sleep(0.3)
            assert not task.done(), "the requeue must wait on busy_timeout, not fail"
            await writer.commit()
            result = await asyncio.wait_for(task, timeout=10)

        assert result is not None and result.position == 1

    async def test_a_writer_that_arrives_between_allocation_and_insert_queues_behind_the_requeue(
        self, wal_session_factory, monkeypatch
    ):
        """The production interleaving: the requeue has read ``max(position)`` and the
        plate authority's persist task tries to commit before the INSERT. Under the old
        savepoint that commit landed and the INSERT died; now the writer waits for the
        requeue's commit, and both land."""
        async with wal_session_factory() as seed:
            source = PrintQueueItem(position=1, status="failed", printer_id=3)
            seed.add(source)
            await seed.commit()

        events: list[str] = []
        real_allocate = queue_builder.allocate_queue_positions
        rival: list[asyncio.Task[None]] = []

        async def _persist_task_write() -> None:
            async with wal_session_factory() as other:
                other.add(PrintQueueItem(position=99, status="pending", printer_id=42))
                await other.commit()
            events.append("rival committed")

        async def _allocate_then_let_a_rival_in(db: AsyncSession, **kwargs: Any) -> int:
            position = await real_allocate(db, **kwargs)
            rival.append(asyncio.create_task(_persist_task_write()))
            await asyncio.sleep(0.3)  # the rival runs now — and must block
            events.append("requeue inserting")
            return position

        monkeypatch.setattr(queue_builder, "allocate_queue_positions", _allocate_then_let_a_rival_in)

        result = await requeue.requeue_attempt(source.id, cause="plate_check", stage_manual=False)
        await asyncio.wait_for(rival[0], timeout=10)

        assert result is not None
        assert events == ["requeue inserting", "rival committed"]
        async with wal_session_factory() as check:
            rows = (await check.execute(select(PrintQueueItem.printer_id))).scalars().all()
        assert sorted(rows) == [3, 3, 42]

    async def test_two_concurrent_pool_requeues_take_distinct_positions(self, wal_session_factory):
        """Without the lock both read the same ``max(position)`` of the shared
        NULL-printer sequence and wrote the same number."""
        async with wal_session_factory() as seed:
            waiting = PrintQueueItem(position=1, status="pending", target_model="H2S")
            a = PrintQueueItem(position=2, status="failed", printer_id=1, target_model="H2S")
            b = PrintQueueItem(position=3, status="failed", printer_id=2, target_model="H2S")
            seed.add_all([waiting, a, b])
            await seed.commit()

        results = await asyncio.gather(
            requeue.requeue_attempt(a.id, cause="failed", stage_manual=False),
            requeue.requeue_attempt(b.id, cause="failed", stage_manual=False),
        )

        assert all(result is not None for result in results)
        async with wal_session_factory() as check:
            pending = (
                (
                    await check.execute(
                        select(PrintQueueItem.position).where(
                            PrintQueueItem.status == "pending", PrintQueueItem.printer_id.is_(None)
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert sorted(pending) == [1, 2, 3]  # two requeues at the head, the waiting row behind
