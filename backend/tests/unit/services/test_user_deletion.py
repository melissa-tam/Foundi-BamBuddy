"""Unit tests for ``services.user_deletion`` — the one place a user is deleted.

Two properties are pinned here, and both are about things that fail SILENTLY.

**The refusal is correct under concurrency, in all four scopes.** The delete's
guard is a correlated ``NOT EXISTS`` inside each statement's own WHERE, so a unit
that dispatches between the request's read and its write is seen by the database
rather than by a stale Python list. Every race test below drives TWO independent
sessions over a FILE-backed SQLite database, because the suite's usual in-memory
DB shares one connection between sessions and cannot reproduce the race at all —
the "other" session would see this one's uncommitted work. Production shape being
pinned: 2026-08-22, run 114 aborted at 02:34:31 and hard-deleted at 02:34:36 with
three units still printing on 001/002/003-H2S, all three finishing as FOREIGN jobs
behind a raised plate gate, roughly 27 printer-hours idle. The user delete carries
that hazard at a strictly wider radius: it is fleet-wide, spans every run, and —
because ``SkuFile`` has no owner and runs take ``created_by_id`` from whoever
STARTED them — routinely reaches a print the deleting admin never saw.

**The delete is not quietly a no-op.** ``NOT IN (subquery)`` is ``<> ALL``, so one
NULL among the subquery's rows makes the predicate false for every row and the
statement matches nothing — no error, no log line, a 204 and an untouched
database. Every column the guard rides is nullable by design (a queue item carries
``archive_id`` XOR ``library_file_id``; ``batch_id`` is NULL on every un-batched
item), so that is not a corner case, it is the norm. ``TestNullSafety`` asserts the
behaviour rather than the SQL text, because a future rewrite that reintroduces the
bug would still pass a string comparison against the current statement.

The remaining classes pin the parts of the transition no HTTP-level test can see:
which dependent rows the service performs the declared FK policy for (SQLite
enforces none of them), and that bytes leave disk exactly when the transaction
commits and never when it refuses.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

import backend.app.models  # noqa: F401 — registers every table on Base.metadata
from backend.app.core.config import settings
from backend.app.core.database import Base
from backend.app.models import (  # noqa: F401 — not re-exported by models/__init__
    active_print_spoolman,
    print_log,
    print_queue,
    project_bom,
)
from backend.app.models.active_print_spoolman import ActivePrintSpoolman
from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile, LibraryFileTag, LibraryTag
from backend.app.models.print_batch import PrintBatch
from backend.app.models.print_log import PrintLogEntry
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.sku import Sku, SkuFile
from backend.app.models.slot_recheck import SlotRecheckIntent
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.models.user import User
from backend.app.services.user_deletion import delete_impact, delete_user

pytestmark = pytest.mark.unit


@pytest.fixture
async def file_engine(tmp_path):
    """A FILE-backed SQLite engine so two sessions get two real connections."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'user_deletion.db'}")
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


@pytest.fixture
def estate(tmp_path, monkeypatch):
    """Point the file-removing rules at a scratch tree.

    ``resolve_archive_dir_for_delete`` refuses anything that is not exactly
    ``<printer>/<archive>`` under ``archive_dir``, and the library paths resolve
    relative to ``base_dir``; both read the settings SINGLETON, so the tests get a
    real directory layout rather than a mock of one.
    """
    base = tmp_path / "estate"
    (base / "archive").mkdir(parents=True)
    monkeypatch.setattr(settings, "base_dir", base)
    monkeypatch.setattr(settings, "archive_dir", base / "archive")
    return base


async def _seed_user(session_factory, username: str = "departing") -> int:
    async with session_factory() as db:
        user = User(username=username)
        db.add(user)
        await db.commit()
        return user.id


async def _two_users(db: AsyncSession) -> tuple[int, int]:
    """The cross-user shape: an artefact OWNER, and the operator whose print runs off it."""
    owner = User(username="departing")
    other = User(username="operator-b")
    db.add_all([owner, other])
    await db.flush()
    return owner.id, other.id


async def _load_user(db: AsyncSession, user_id: int) -> User:
    """The route's own load, groups eagerly attached as the route does it."""
    return (await db.execute(select(User).where(User.id == user_id).options(selectinload(User.groups)))).scalar_one()


async def _archive(db: AsyncSession, *, owner: int | None, file_path: str = "") -> PrintArchive:
    archive = PrintArchive(
        filename="unit.gcode.3mf",
        print_name="Unit",
        file_path=file_path,
        file_size=1,
        status="completed",
        created_by_id=owner,
    )
    db.add(archive)
    return archive


async def _library_file(db: AsyncSession, *, owner: int | None, file_path: str = "", thumb: str | None = None):
    row = LibraryFile(
        filename="unit.gcode.3mf",
        file_path=file_path,
        file_type="gcode.3mf",
        file_size=1,
        thumbnail_path=thumb,
        created_by_id=owner,
    )
    db.add(row)
    return row


async def _surviving_ids(session_factory, model) -> list[int]:
    async with session_factory() as db:
        return sorted(row[0] for row in (await db.execute(select(model.id))).all())


async def _delete(session_factory, user_id: int, *, delete_items: bool = True) -> None:
    async with session_factory() as db:
        await delete_user(db, user=await _load_user(db, user_id), delete_items=delete_items)


class TestPrintingUnitRefusesEveryScope:
    """One test per scope: a unit that goes ``printing`` in the gap refuses the delete.

    Each drives the incident's exact shape — session A holds the request's view,
    session B commits the dispatch, A then runs the delete — and asserts the two
    things that matter afterwards: the request 409s, and NOTHING was destroyed.
    """

    async def test_the_users_own_queue_item(self, session_factory, estate):
        user_id = await _seed_user(session_factory)
        async with session_factory() as db:
            item = PrintQueueItem(status="pending", position=0, created_by_id=user_id)
            db.add(item)
            await db.commit()
            item_id = item.id

        await self._race_and_assert_refusal(session_factory, user_id, item_id)

    async def test_another_operators_print_running_off_this_users_archive(self, session_factory, estate):
        """The cross-user shape: the blocker is not the deleted user's own row.

        ``production_run.create_production_run`` takes ``library_file_id`` from the
        SKU catalog and ``created_by_id`` from whoever started the run, so this is
        the ordinary case, not an exotic one.
        """
        user_id = await _seed_user(session_factory)
        other_id = await _seed_user(session_factory, "operator-b")
        async with session_factory() as db:
            archive = await _archive(db, owner=user_id)
            await db.flush()
            item = PrintQueueItem(status="pending", position=0, created_by_id=other_id, archive_id=archive.id)
            db.add(item)
            await db.commit()
            item_id, archive_id = item.id, archive.id

        await self._race_and_assert_refusal(session_factory, user_id, item_id)
        assert await _surviving_ids(session_factory, PrintArchive) == [archive_id]

    async def test_another_operators_print_running_off_this_users_library_file(self, session_factory, estate):
        user_id = await _seed_user(session_factory)
        other_id = await _seed_user(session_factory, "operator-b")
        async with session_factory() as db:
            lib = await _library_file(db, owner=user_id)
            await db.flush()
            item = PrintQueueItem(status="pending", position=0, created_by_id=other_id, library_file_id=lib.id)
            db.add(item)
            await db.commit()
            item_id, lib_id = item.id, lib.id

        await self._race_and_assert_refusal(session_factory, user_id, item_id)
        assert await _surviving_ids(session_factory, LibraryFile) == [lib_id]

    async def test_a_print_running_in_this_users_batch(self, session_factory, estate):
        user_id = await _seed_user(session_factory)
        other_id = await _seed_user(session_factory, "operator-b")
        async with session_factory() as db:
            batch = PrintBatch(name="run", created_by_id=user_id)
            db.add(batch)
            await db.flush()
            item = PrintQueueItem(status="pending", position=0, created_by_id=other_id, batch_id=batch.id)
            db.add(item)
            await db.commit()
            item_id, batch_id = item.id, batch.id

        await self._race_and_assert_refusal(session_factory, user_id, item_id)
        assert await _surviving_ids(session_factory, PrintBatch) == [batch_id]

    @staticmethod
    async def _race_and_assert_refusal(session_factory, user_id: int, racing_item_id: int) -> None:
        async with session_factory() as session_a, session_factory() as session_b:
            # A is the delete request, holding the view it loaded the user with.
            user = await _load_user(session_a, user_id)
            loaded = (
                await session_a.execute(select(PrintQueueItem).where(PrintQueueItem.id == racing_item_id))
            ).scalar_one()
            assert loaded.status == "pending"

            # B is the scheduler: status + started_at, COMMITTED before the print
            # command goes out (print_scheduler._start_print).
            racing = (
                await session_b.execute(select(PrintQueueItem).where(PrintQueueItem.id == racing_item_id))
            ).scalar_one()
            racing.status = "printing"
            racing.started_at = datetime(2026, 8, 22, 2, 34, 33, tzinfo=timezone.utc)
            await session_b.commit()

            # expire_on_commit=False everywhere in this fork: A's copy still SAYS
            # pending, and that is precisely what must not decide.
            assert loaded.status == "pending"
            with pytest.raises(HTTPException) as refusal:
                await delete_user(session_a, user=user, delete_items=True)

        assert refusal.value.status_code == 409
        assert refusal.value.detail["code"] == "user_has_printing_units"

        # Nothing on the path was destroyed — the refusal rolled the whole thing back.
        assert await _surviving_ids(session_factory, PrintQueueItem) == [racing_item_id]
        assert user_id in await _surviving_ids(session_factory, User)


class TestNullSafety:
    async def test_a_null_archive_id_does_not_suppress_the_archive_delete(self, session_factory, estate):
        """The ``NOT IN`` trap, asserted as behaviour.

        A single queue item with ``archive_id IS NULL`` — a library-file dispatch,
        i.e. half of everything the farm queues — is enough to make
        ``NOT IN (SELECT archive_id …)`` false for every row. The archive delete
        would match nothing, return 204, and leave the estate untouched with no
        error anywhere. The correlated ``NOT EXISTS`` is unaffected by NULLs.
        """
        user_id = await _seed_user(session_factory)
        async with session_factory() as db:
            archive = await _archive(db, owner=user_id)
            lib = await _library_file(db, owner=user_id)
            await db.flush()
            # A printing item that names NO archive: the NULL that poisons NOT IN.
            db.add(PrintQueueItem(status="printing", position=0, library_file_id=lib.id, archive_id=None))
            await db.commit()
            archive_id = archive.id

        # The library file is blocked (a live print runs off it) so the whole
        # request refuses — but that refusal must come from the LIBRARY scope.
        async with session_factory() as db:
            with pytest.raises(HTTPException):
                await delete_user(db, user=await _load_user(db, user_id), delete_items=True)

        # Now clear the blocker and confirm the archive really does go: if the
        # NULL had suppressed the statement, this would still find the row.
        async with session_factory() as db:
            item = (await db.execute(select(PrintQueueItem))).scalar_one()
            item.status = "completed"
            await db.commit()

        await _delete(session_factory, user_id)
        assert archive_id not in await _surviving_ids(session_factory, PrintArchive)
        assert await _surviving_ids(session_factory, PrintArchive) == []


class TestDeclaredForeignKeyPolicies:
    """SQLite enforces no FK, so every ``ondelete`` is a policy this service runs."""

    async def test_the_gram_ledger_outlives_the_archive_it_came_from(self, session_factory, estate):
        user_id = await _seed_user(session_factory)
        async with session_factory() as db:
            archive = await _archive(db, owner=user_id)
            await db.flush()
            db.add(SpoolUsageHistory(spool_id=1, archive_id=archive.id, weight_used=42.5, percent_used=4))
            await db.commit()

        await _delete(session_factory, user_id)

        async with session_factory() as db:
            usage = (await db.execute(select(SpoolUsageHistory))).scalars().all()
        assert len(usage) == 1
        assert usage[0].archive_id is None
        assert usage[0].weight_used == 42.5

    async def test_print_history_survives_with_its_attribution_and_thumbnail_cleared(self, session_factory, estate):
        user_id = await _seed_user(session_factory)
        async with session_factory() as db:
            archive = await _archive(db, owner=user_id)
            await db.flush()
            db.add(
                PrintLogEntry(
                    archive_id=archive.id,
                    status="completed",
                    print_name="Unit",
                    thumbnail_path="archive/1/2/thumb.png",
                    created_by_id=user_id,
                )
            )
            await db.commit()

        await _delete(session_factory, user_id)

        async with session_factory() as db:
            entries = (await db.execute(select(PrintLogEntry))).scalars().all()
        assert len(entries) == 1
        assert (entries[0].archive_id, entries[0].thumbnail_path, entries[0].created_by_id) == (None, None, None)

    async def test_cascading_children_of_an_archive_and_a_library_file_are_gone(self, session_factory, estate):
        user_id = await _seed_user(session_factory)
        async with session_factory() as db:
            archive = await _archive(db, owner=user_id)
            lib = await _library_file(db, owner=user_id)
            sku = Sku(code="SKU007.01", name="thing")
            tag = LibraryTag(name="petg", name_key="petg")
            db.add_all([sku, tag])
            await db.flush()
            db.add_all(
                [
                    ActivePrintSpoolman(printer_id=1, archive_id=archive.id, ams_trays={}),
                    SkuFile(sku_id=sku.id, library_file_id=lib.id, plate_index=1),
                    LibraryFileTag(file_id=lib.id, tag_id=tag.id),
                ]
            )
            await db.commit()

        await _delete(session_factory, user_id)

        async with session_factory() as db:
            assert (await db.execute(select(ActivePrintSpoolman))).scalars().all() == []
            assert (await db.execute(select(SkuFile))).scalars().all() == []
            assert (await db.execute(select(LibraryFileTag))).scalars().all() == []
            # The SKU itself is farm-wide and has no owner — only its file link goes.
            assert len((await db.execute(select(Sku))).scalars().all()) == 1

    @pytest.mark.parametrize("delete_items", [True, False])
    async def test_operator_attribution_is_handled_on_both_branches(self, session_factory, estate, delete_items):
        """The rows BOTH branches used to leave dangling."""
        user_id = await _seed_user(session_factory)
        async with session_factory() as db:
            db.add_all(
                [
                    SlotRecheckIntent(
                        printer_id=1,
                        ams_id=0,
                        tray_id=1,
                        requested_at=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc),
                        requested_by=user_id,
                    ),
                    PrintLogEntry(status="completed", print_name="Unit", created_by_id=user_id),
                ]
            )
            await db.commit()

        await _delete(session_factory, user_id, delete_items=delete_items)

        async with session_factory() as db:
            intents = (await db.execute(select(SlotRecheckIntent))).scalars().all()
            log = (await db.execute(select(PrintLogEntry))).scalars().all()
            # SET NULL: deleting an operator must not erase a slot's identity history.
            assert len(intents) == 1 and intents[0].requested_by is None
            assert len(log) == 1 and log[0].created_by_id is None

    async def test_the_non_destructive_branch_disowns_instead_of_deleting(self, session_factory, estate):
        user_id = await _seed_user(session_factory)
        async with session_factory() as db:
            archive = await _archive(db, owner=user_id)
            await _library_file(db, owner=user_id)
            batch = PrintBatch(name="run", created_by_id=user_id)
            db.add(batch)
            await db.flush()
            db.add(PrintQueueItem(status="completed", position=0, created_by_id=user_id, archive_id=archive.id))
            await db.commit()

        await _delete(session_factory, user_id, delete_items=False)

        async with session_factory() as db:
            assert [a.created_by_id for a in (await db.execute(select(PrintArchive))).scalars()] == [None]
            assert [f.created_by_id for f in (await db.execute(select(LibraryFile))).scalars()] == [None]
            assert [b.created_by_id for b in (await db.execute(select(PrintBatch))).scalars()] == [None]
            assert [i.created_by_id for i in (await db.execute(select(PrintQueueItem))).scalars()] == [None]
            assert (await db.execute(select(User))).scalars().all() == []


class TestBytesLeaveDiskExactlyWhenTheTransactionCommits:
    @staticmethod
    async def _seed_estate(session_factory, base) -> tuple[int, list]:
        """One archive directory and one library file+thumbnail, all present on disk."""
        archive_dir = base / "archive" / "3" / "20260822_120000_unit"
        archive_dir.mkdir(parents=True)
        archive_file = archive_dir / "unit.gcode.3mf"
        archive_file.write_bytes(b"3mf")

        library_dir = base / "archive" / "library" / "files"
        library_dir.mkdir(parents=True)
        library_file = library_dir / "unit.gcode.3mf"
        library_file.write_bytes(b"3mf")
        thumbnail = library_dir / "unit.png"
        thumbnail.write_bytes(b"png")

        user_id = await _seed_user(session_factory)
        async with session_factory() as db:
            await _archive(db, owner=user_id, file_path=str(archive_file.relative_to(base)))
            await _library_file(
                db,
                owner=user_id,
                file_path=str(library_file.relative_to(base)),
                thumb=str(thumbnail.relative_to(base)),
            )
            await db.commit()
        return user_id, [archive_dir, library_file, thumbnail]

    async def test_a_successful_delete_takes_the_bytes(self, session_factory, estate):
        user_id, paths = await self._seed_estate(session_factory, estate)
        assert all(p.exists() for p in paths)

        await _delete(session_factory, user_id)

        assert not any(p.exists() for p in paths)
        # The managed library ROOT is not an archive directory and must survive:
        # ``resolve_archive_dir_for_delete`` refuses it structurally.
        assert (estate / "archive" / "library" / "files").is_dir()

    async def test_a_refused_delete_purges_nothing(self, session_factory, estate):
        user_id, paths = await self._seed_estate(session_factory, estate)
        async with session_factory() as db:
            archive = (await db.execute(select(PrintArchive))).scalar_one()
            db.add(PrintQueueItem(status="printing", position=0, archive_id=archive.id))
            await db.commit()

        async with session_factory() as db:
            with pytest.raises(HTTPException) as refusal:
                await delete_user(db, user=await _load_user(db, user_id), delete_items=True)
        assert refusal.value.status_code == 409

        assert all(p.exists() for p in paths)


class TestDeleteImpact:
    async def test_it_counts_what_the_delete_would_actually_destroy(self, session_factory, estate):
        """Including the soft-deleted library file the old ``items-count`` hid."""
        user_id = await _seed_user(session_factory)
        other_id = await _seed_user(session_factory, "operator-b")
        async with session_factory() as db:
            await _archive(db, owner=user_id)
            live = await _library_file(db, owner=user_id)
            trashed = await _library_file(db, owner=user_id)
            trashed.deleted_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
            batch = PrintBatch(name="run", created_by_id=user_id)
            sku = Sku(code="SKU007.01", name="thing")
            db.add_all([batch, sku])
            await db.flush()
            db.add_all(
                [
                    SkuFile(sku_id=sku.id, library_file_id=live.id, plate_index=1),
                    SkuFile(sku_id=sku.id, library_file_id=live.id, plate_index=2),
                    PrintQueueItem(status="completed", position=0, created_by_id=user_id),
                    PrintQueueItem(status="printing", position=1, created_by_id=other_id, library_file_id=live.id),
                ]
            )
            await db.commit()

        async with session_factory() as db:
            impact = await delete_impact(db, user_id=user_id)

        assert impact.archives == 1
        # BOTH library files — the trashed one is destroyed too, and the endpoint
        # this replaced filtered it out and under-reported.
        assert impact.library_files == 2
        assert impact.queue_items == 1
        assert impact.production_runs == 1
        # Two SkuFile rows, ONE distinct SKU broken by the deletion.
        assert impact.dependent_skus == 1
        # Another operator's live print, running off this user's library file.
        assert impact.currently_printing == 1

    async def test_an_untouched_user_forecasts_nothing(self, session_factory, estate):
        user_id = await _seed_user(session_factory)
        async with session_factory() as db:
            impact = await delete_impact(db, user_id=user_id)
        assert impact.model_dump() == {
            "archives": 0,
            "library_files": 0,
            "queue_items": 0,
            "production_runs": 0,
            "dependent_skus": 0,
            "currently_printing": 0,
        }


class TestDispatchAfterTheRequestsLoad:
    """A dispatch committed by a SECOND session after the request loaded the user.

    The 2026-08-22 incident's own shape: the abort request loaded the run BEFORE
    the scheduler committed the dispatch, then wrote against that view a second
    later. Here session A performs the route's load, session B commits
    ``pending -> printing`` over its own connection, and A then runs the delete.
    A guard derived from the request's earlier load cannot see B's write; the
    DELETE's own WHERE can.

    This is as late as a real second connection can commit. SQLite allows ONE
    writer: the moment phase 1's DELETE runs, session A holds the RESERVED lock
    and every other connection's write blocks until A commits or rolls back
    (measured: ``OperationalError: database is locked`` on B's UPDATE). The window
    *inside* the service is therefore unreachable cross-connection, and is pinned
    by :class:`TestDispatchInsideTheDeleteWindow` instead.
    """

    async def test_a_dispatch_onto_this_users_archive(self, session_factory, estate):
        async with session_factory() as db:
            owner_id, other_id = await _two_users(db)
            archive = await _archive(db, owner=owner_id)
            await db.flush()
            item = PrintQueueItem(status="pending", position=0, created_by_id=other_id, archive_id=archive.id)
            db.add(item)
            await db.commit()
            item_id, archive_id = item.id, archive.id

        await _delete_racing_a_second_session(session_factory, owner_id, item_id)
        assert await _surviving_ids(session_factory, PrintArchive) == [archive_id]

    async def test_a_dispatch_onto_this_users_library_file(self, session_factory, estate):
        async with session_factory() as db:
            owner_id, other_id = await _two_users(db)
            lib = await _library_file(db, owner=owner_id)
            await db.flush()
            item = PrintQueueItem(status="pending", position=0, created_by_id=other_id, library_file_id=lib.id)
            db.add(item)
            await db.commit()
            item_id, lib_id = item.id, lib.id

        await _delete_racing_a_second_session(session_factory, owner_id, item_id)
        assert await _surviving_ids(session_factory, LibraryFile) == [lib_id]

    async def test_a_dispatch_into_this_users_batch(self, session_factory, estate):
        async with session_factory() as db:
            owner_id, other_id = await _two_users(db)
            batch = PrintBatch(name="run", created_by_id=owner_id)
            db.add(batch)
            await db.flush()
            item = PrintQueueItem(status="pending", position=0, created_by_id=other_id, batch_id=batch.id)
            db.add(item)
            await db.commit()
            item_id, batch_id = item.id, batch.id

        await _delete_racing_a_second_session(session_factory, owner_id, item_id)
        assert await _surviving_ids(session_factory, PrintBatch) == [batch_id]


async def _delete_racing_a_second_session(session_factory, user_id: int, racing_item_id: int) -> None:
    """A loads the user, B commits the dispatch over its own connection, A deletes."""
    async with session_factory() as session_a, session_factory() as session_b:
        user = await _load_user(session_a, user_id)
        loaded = (
            await session_a.execute(select(PrintQueueItem).where(PrintQueueItem.id == racing_item_id))
        ).scalar_one()
        assert loaded.status == "pending"

        racer = (
            await session_b.execute(select(PrintQueueItem).where(PrintQueueItem.id == racing_item_id))
        ).scalar_one()
        racer.status = "printing"
        racer.started_at = datetime(2026, 8, 22, 2, 34, 33, tzinfo=timezone.utc)
        await session_b.commit()

        # expire_on_commit=False everywhere in this fork: A's copy still SAYS
        # pending, and that is precisely what must not decide.
        assert loaded.status == "pending"
        with pytest.raises(HTTPException) as refusal:
            await delete_user(session_a, user=user, delete_items=True)

    assert refusal.value.status_code == 409
    assert refusal.value.detail["code"] == "user_has_printing_units"

    assert await _surviving_ids(session_factory, PrintQueueItem) == [racing_item_id]
    async with session_factory() as db:
        after = (await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == racing_item_id))).scalar_one()
    assert after.status == "printing"
    assert after.started_at is not None
    assert user_id in await _surviving_ids(session_factory, User)


class TestDispatchInsideTheDeleteWindow:
    """The unit goes ``printing`` INSIDE the service, after every read it performs.

    The property under test is narrow and is the reason
    ``printing_items_referencing`` sits in the DELETE's WHERE at all: the predicate
    is evaluated BY THE DATABASE at write time, never carried in from an earlier
    read. A Python pre-check placed anywhere in phases 1-3 — which is exactly what
    a future tidy-up would reach for — reads ``pending`` here and lets the delete
    through; the statement's own WHERE reads ``printing`` and refuses.

    The flip is applied on the service's OWN session rather than a second one, and
    that is forced, not preferred: SQLite permits one writer, so from phase 1's
    DELETE onwards no other connection can commit at all
    (:class:`TestDispatchAfterTheRequestsLoad` documents the measurement). Within
    one transaction a statement sees its own prior writes, which is all this
    property needs — what it deliberately does NOT claim to exercise is
    cross-connection isolation.

    The window is entered at ``null_print_log_thumbnail_paths``: phase 3's first
    statement, after every one of phase 2's estate reads and before any phase 4
    DELETE. If that function ever stops being called there the seam is gone, so
    each test asserts the hook actually fired rather than passing vacuously.
    """

    async def test_a_dispatch_onto_this_users_archive(self, session_factory, estate, monkeypatch):
        async with session_factory() as db:
            owner_id, other_id = await _two_users(db)
            archive = await _archive(db, owner=owner_id)
            await db.flush()
            item = PrintQueueItem(status="pending", position=0, created_by_id=other_id, archive_id=archive.id)
            db.add(item)
            await db.commit()
            item_id, archive_id = item.id, archive.id

        await _delete_flipping_inside_the_window(session_factory, monkeypatch, owner_id, item_id)
        assert await _surviving_ids(session_factory, PrintArchive) == [archive_id]

    async def test_a_dispatch_onto_this_users_library_file(self, session_factory, estate, monkeypatch):
        async with session_factory() as db:
            owner_id, other_id = await _two_users(db)
            lib = await _library_file(db, owner=owner_id)
            await db.flush()
            item = PrintQueueItem(status="pending", position=0, created_by_id=other_id, library_file_id=lib.id)
            db.add(item)
            await db.commit()
            item_id, lib_id = item.id, lib.id

        await _delete_flipping_inside_the_window(session_factory, monkeypatch, owner_id, item_id)
        assert await _surviving_ids(session_factory, LibraryFile) == [lib_id]

    async def test_a_dispatch_into_this_users_batch(self, session_factory, estate, monkeypatch):
        async with session_factory() as db:
            owner_id, other_id = await _two_users(db)
            batch = PrintBatch(name="run", created_by_id=owner_id)
            db.add(batch)
            await db.flush()
            item = PrintQueueItem(status="pending", position=0, created_by_id=other_id, batch_id=batch.id)
            db.add(item)
            await db.commit()
            item_id, batch_id = item.id, batch.id

        await _delete_flipping_inside_the_window(session_factory, monkeypatch, owner_id, item_id)
        assert await _surviving_ids(session_factory, PrintBatch) == [batch_id]


async def _delete_flipping_inside_the_window(session_factory, monkeypatch, user_id: int, racing_item_id: int) -> None:
    """Run the delete, flipping the unit to ``printing`` at phase 3's door."""
    import backend.app.services.user_deletion as ud

    real = ud.null_print_log_thumbnail_paths
    fired: list[int] = []

    async def dispatch_then_continue(db, archive_ids):
        if not fired:
            fired.append(racing_item_id)
            await db.execute(
                update(PrintQueueItem)
                .where(PrintQueueItem.id == racing_item_id)
                .values(status="printing", started_at=datetime(2026, 8, 22, 2, 34, 33, tzinfo=timezone.utc))
                .execution_options(synchronize_session=False)
            )
        return await real(db, archive_ids)

    monkeypatch.setattr(ud, "null_print_log_thumbnail_paths", dispatch_then_continue)

    async with session_factory() as session_a:
        user = await _load_user(session_a, user_id)
        with pytest.raises(HTTPException) as refusal:
            await delete_user(session_a, user=user, delete_items=True)

    assert fired, "the racing dispatch never ran — the window moved, so this test proves nothing"
    assert refusal.value.status_code == 409
    assert refusal.value.detail["code"] == "user_has_printing_units"

    # Nothing was destroyed. The flip itself is rolled back with everything else —
    # it lived only inside the refused transaction — so the row is ``pending``
    # again afterwards, which is correct: the refusal is what had to survive.
    assert await _surviving_ids(session_factory, PrintQueueItem) == [racing_item_id]
    assert user_id in await _surviving_ids(session_factory, User)


class TestPhaseOrderingIsLoadBearing:
    """The queue rows ARE the refusal evidence, so nothing may consume them first.

    ``delete_related_queue_items`` deletes queue rows regardless of status, and the
    ``batch_id`` SET NULL erases the link the batch scope is guarded by. Either one
    run BEFORE the blocker read destroys the very rows that were supposed to refuse
    the delete — and the failure mode is the worst in this module: no exception, no
    log line, HTTP 204, and a live print severed from the farm exactly as in the
    2026-08-22 incident. Nothing enforces the ordering except the order of
    statements in one function, so it is asserted here as BEHAVIOUR: someone
    tidying that function must see a test go red, not a comment they can move.

    Each case gives the user an estate whose only ``printing`` unit is reachable
    solely through the scope under test, and requires two things together — the
    409, and that queue row still being present afterwards. The second half is what
    a premature phase 7 breaks.
    """

    @staticmethod
    async def _assert_refused_with_evidence_intact(session_factory, user_id: int, item_id: int) -> None:
        async with session_factory() as db:
            with pytest.raises(HTTPException) as refusal:
                await delete_user(db, user=await _load_user(db, user_id), delete_items=True)
        assert refusal.value.status_code == 409

        # The evidence survives its own verdict. A phase 7 hoisted above the
        # blocker read consumes this row, finds nothing to refuse, and returns 204.
        async with session_factory() as db:
            survivor = (
                await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))
            ).scalar_one_or_none()
        assert survivor is not None, "the delete consumed the queue row that was supposed to refuse it"
        assert survivor.status == "printing"

    async def test_evidence_reachable_only_through_the_archive_scope(self, session_factory, estate):
        async with session_factory() as db:
            owner_id, other_id = await _two_users(db)
            archive = await _archive(db, owner=owner_id)
            await db.flush()
            item = PrintQueueItem(status="printing", position=0, created_by_id=other_id, archive_id=archive.id)
            db.add(item)
            await db.commit()
            item_id = item.id

        await self._assert_refused_with_evidence_intact(session_factory, owner_id, item_id)

    async def test_evidence_reachable_only_through_the_library_file_scope(self, session_factory, estate):
        async with session_factory() as db:
            owner_id, other_id = await _two_users(db)
            lib = await _library_file(db, owner=owner_id)
            await db.flush()
            item = PrintQueueItem(status="printing", position=0, created_by_id=other_id, library_file_id=lib.id)
            db.add(item)
            await db.commit()
            item_id = item.id

        await self._assert_refused_with_evidence_intact(session_factory, owner_id, item_id)

    async def test_evidence_reachable_only_through_the_batch_scope(self, session_factory, estate):
        """The batch scope's evidence is a COLUMN, not a row — and it is only NULLed.

        Worth its own case because the damage is quieter than the other two: the
        queue row survives a premature phase 7 intact, it simply stops naming the
        batch, so ``batch_id.in_(batch_ids)`` matches nothing and the refusal never
        fires. Asserting the row's survival alone would not catch that; the 409 is
        what does, and the final assertion names the column directly.
        """
        async with session_factory() as db:
            owner_id, other_id = await _two_users(db)
            batch = PrintBatch(name="run", created_by_id=owner_id)
            db.add(batch)
            await db.flush()
            item = PrintQueueItem(status="printing", position=0, created_by_id=other_id, batch_id=batch.id)
            db.add(item)
            await db.commit()
            item_id, batch_id = item.id, batch.id

        await self._assert_refused_with_evidence_intact(session_factory, owner_id, item_id)
        async with session_factory() as db:
            after = (await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one()
        assert after.batch_id == batch_id, "the batch link was erased before it could refuse the delete"


class TestPrintFinishingBetweenTheDeleteAndTheBlockerRead:
    """A print live at phase 4 but OVER by phase 5 must still refuse.

    The window between the guarded DELETE and the blocker read. The unit is
    ``printing`` when phase 4 runs, so the correlated NOT EXISTS correctly skips
    its archive; the print then finishes, so phase 5's read finds nothing live.
    Deciding on that read alone, the request commits — and leaves the archive
    sitting there with ``created_by_id`` pointing at a user who no longer exists.
    That is a dangling row of exactly the class this module exists to stop
    creating, manufactured by the module itself, and it is why the two signals are
    OR-ed: what the DELETE declined to touch is a refusal on its own evidence,
    independent of what is still live a few statements later.

    It also makes the guard OBSERVABLE. Until the survivors were read back,
    removing ``~printing_items_referencing(ref_col)`` from the DELETE changed
    nothing any test could see, because the outcome was decided entirely by the
    later query — the predicate could be deleted outright and the suite stayed
    green.
    """

    @staticmethod
    async def _delete_finishing_in_the_window(session_factory, monkeypatch, user_id: int, item_id: int) -> None:
        import backend.app.services.user_deletion as ud

        real = ud.live_prints_blocking
        fired: list[int] = []

        async def finish_then_read(db, *, scope):
            # Land the completion BEFORE the first blocker query runs, so every
            # one of phase 5's three reads sees a terminal unit.
            if not fired:
                fired.append(item_id)
                await db.execute(
                    update(PrintQueueItem)
                    .where(PrintQueueItem.id == item_id)
                    .values(status="completed", completed_at=datetime(2026, 8, 22, 3, 0, tzinfo=timezone.utc))
                    .execution_options(synchronize_session=False)
                )
            return await real(db, scope=scope)

        monkeypatch.setattr(ud, "live_prints_blocking", finish_then_read)

        async with session_factory() as session_a:
            user = await _load_user(session_a, user_id)
            with pytest.raises(HTTPException) as refusal:
                await delete_user(session_a, user=user, delete_items=True)

        assert fired, "the completion never landed — the window moved, so this test proves nothing"
        assert refusal.value.status_code == 409
        # The print is OVER, so there is no printer left to name — and it refuses
        # anyway. Committing because the sentence would be thin is the failure
        # mode; a thin sentence is not.
        assert refusal.value.detail["code"] == "user_has_printing_units"
        assert refusal.value.detail["printers"] == []

    async def test_an_archive_is_never_left_behind_owning_a_deleted_user(self, session_factory, estate, monkeypatch):
        async with session_factory() as db:
            owner_id, other_id = await _two_users(db)
            archive = await _archive(db, owner=owner_id)
            await db.flush()
            item = PrintQueueItem(status="printing", position=0, created_by_id=other_id, archive_id=archive.id)
            db.add(item)
            await db.commit()
            item_id, archive_id = item.id, archive.id

        await self._delete_finishing_in_the_window(session_factory, monkeypatch, owner_id, item_id)

        # The whole transaction rolled back: the archive is still owned by a user
        # who still exists. The forbidden state is the pair — archive present with
        # its owner gone — so both halves are asserted together.
        async with session_factory() as db:
            survivor = (
                await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))
            ).scalar_one_or_none()
            owners = await _surviving_ids(session_factory, User)
        assert survivor is not None
        assert survivor.created_by_id == owner_id
        assert owner_id in owners, "archive left behind pointing at a deleted user"

    async def test_a_library_file_is_never_left_behind_owning_a_deleted_user(
        self, session_factory, estate, monkeypatch
    ):
        async with session_factory() as db:
            owner_id, other_id = await _two_users(db)
            lib = await _library_file(db, owner=owner_id)
            await db.flush()
            item = PrintQueueItem(status="printing", position=0, created_by_id=other_id, library_file_id=lib.id)
            db.add(item)
            await db.commit()
            item_id, lib_id = item.id, lib.id

        await self._delete_finishing_in_the_window(session_factory, monkeypatch, owner_id, item_id)

        async with session_factory() as db:
            survivor = (await db.execute(select(LibraryFile).where(LibraryFile.id == lib_id))).scalar_one_or_none()
            owners = await _surviving_ids(session_factory, User)
        assert survivor is not None
        assert survivor.created_by_id == owner_id
        assert owner_id in owners, "library file left behind pointing at a deleted user"


class TestDispatchBetweenTheDeleteAndTheBlockerRead:
    """A unit that becomes ``printing`` AFTER phase 4 must still refuse.

    The mirror of :class:`TestPrintFinishingBetweenTheDeleteAndTheBlockerRead`,
    and the reason phase 5 reads over the ids phase 2 captured rather than over
    what phase 4 declined to touch. Here the unit is ``pending`` when the DELETE
    runs, so the guard correctly lets its archive go and there is no survivor to
    refuse on; the dispatch lands immediately afterwards. Only the later read —
    against the ORIGINAL ids, whose rows are already gone — still sees it.

    This is also what makes the phase ordering load-bearing. The evidence phase 5
    reads is queue rows, and phase 7 deletes queue rows: hoist phase 7 above the
    blocker read and this unit's row is destroyed before anything can consult it,
    so the request commits and severs a live print — silently, with HTTP 204. The
    survivor read added at phase 4 cannot cover this case, because at phase 4
    there was genuinely nothing to survive.
    """

    @staticmethod
    async def _delete_dispatching_in_the_window(session_factory, monkeypatch, user_id: int, item_id: int) -> None:
        import backend.app.services.user_deletion as ud

        real = ud.live_prints_blocking
        fired: list[int] = []

        async def dispatch_then_read(db, *, scope):
            # Land the dispatch BEFORE the first blocker query runs: after phase 4
            # has already deleted the parent rows, which is the whole point.
            if not fired:
                fired.append(item_id)
                await db.execute(
                    update(PrintQueueItem)
                    .where(PrintQueueItem.id == item_id)
                    .values(status="printing", started_at=datetime(2026, 8, 22, 2, 34, 33, tzinfo=timezone.utc))
                    .execution_options(synchronize_session=False)
                )
            return await real(db, scope=scope)

        monkeypatch.setattr(ud, "live_prints_blocking", dispatch_then_read)

        async with session_factory() as session_a:
            user = await _load_user(session_a, user_id)
            with pytest.raises(HTTPException) as refusal:
                await delete_user(session_a, user=user, delete_items=True)

        assert fired, "the racing dispatch never ran — the window moved, so this test proves nothing"
        assert refusal.value.status_code == 409
        assert refusal.value.detail["code"] == "user_has_printing_units"

    async def test_a_dispatch_onto_this_users_archive(self, session_factory, estate, monkeypatch):
        async with session_factory() as db:
            owner_id, other_id = await _two_users(db)
            archive = await _archive(db, owner=owner_id)
            await db.flush()
            item = PrintQueueItem(status="pending", position=0, created_by_id=other_id, archive_id=archive.id)
            db.add(item)
            await db.commit()
            item_id, archive_id = item.id, archive.id

        await self._delete_dispatching_in_the_window(session_factory, monkeypatch, owner_id, item_id)

        # Rolled back whole: the archive is back, and so is its owner.
        assert await _surviving_ids(session_factory, PrintArchive) == [archive_id]
        assert owner_id in await _surviving_ids(session_factory, User)
        assert await _surviving_ids(session_factory, PrintQueueItem) == [item_id]

    async def test_a_dispatch_onto_this_users_library_file(self, session_factory, estate, monkeypatch):
        async with session_factory() as db:
            owner_id, other_id = await _two_users(db)
            lib = await _library_file(db, owner=owner_id)
            await db.flush()
            item = PrintQueueItem(status="pending", position=0, created_by_id=other_id, library_file_id=lib.id)
            db.add(item)
            await db.commit()
            item_id, lib_id = item.id, lib.id

        await self._delete_dispatching_in_the_window(session_factory, monkeypatch, owner_id, item_id)

        assert await _surviving_ids(session_factory, LibraryFile) == [lib_id]
        assert owner_id in await _surviving_ids(session_factory, User)
        assert await _surviving_ids(session_factory, PrintQueueItem) == [item_id]
