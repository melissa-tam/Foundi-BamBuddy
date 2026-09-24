"""THE owner of every test database resource.

One module owns four things that used to be scattered across the tree:

1. **The model registry.** ``import_all_models()`` imports every module in
   ``backend.app.models`` so ``Base.metadata`` is complete before any
   ``create_all``. It is derived (``pkgutil``), not a hand-maintained list, so it
   cannot drift the way the old inline block in ``conftest.py`` did — that block
   named 39 of the 57 model modules and silently omitted ``eject_profile``,
   ``sku``, ``library``, ``print_batch``, ``printer_model_geometry`` and 13 more,
   which meant the in-memory schema depended on whatever a test happened to
   import first.

2. **The engine.** Module-scoped, created once per test module rather than once
   per test, over a per-engine temp FILE. It is deliberately not ``:memory:``:
   that URL selects ``StaticPool``, where the pool's one connection IS the
   database, so a single test cancelling a task that holds a session destroys
   the module's data and every later test dies on ``no such table``. A file URL
   selects ``AsyncAdaptedQueuePool``, whose connections are disposable.

3. **Per-test isolation.** ``DELETE FROM`` every table in reverse
   ``sorted_tables`` order at test setup, plus ``sqlite_sequence`` so
   autoincrement ids restart. Deliberately NOT savepoint/nested-transaction
   rollback: under ``StaticPool`` a session built from the engine shares the one
   connection, so a service's own ``COMMIT`` would commit the harness's outer
   transaction and isolation would silently evaporate across the 42 test files
   that build their own ``async_sessionmaker``.

4. **The live-app database bootstrap.** ``core/database.py`` binds ``engine``
   and ``async_session`` at import from ``settings.database_url``; 18 modules
   bind ``async_session`` at import and ``conftest`` can only patch 3 of them.
   The other 15 write to the real file DB. Now that the harness points DATA_DIR
   at a per-worker temp dir, that file starts out empty, so a session-scoped
   autouse fixture runs the app's own ``init_db()`` against it once.

Deleted with the per-test engine: ``Base.metadata.drop_all`` (the DB is
``:memory:`` and dies with the engine) and ``await asyncio.sleep(0.1)`` (4,430
executions, ~443 s of pure sleep in a serial run).
"""

from __future__ import annotations

import asyncio
import importlib
import itertools
import pkgutil
import tempfile
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import Table, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from backend.app.core.database import Base, run_migrations

# TWO urls, because the StaticPool hazard is scoped to SHARED ownership.
#
# Under ":memory:" SQLAlchemy selects StaticPool, so the pool's single connection
# IS the database. For the MODULE-scoped engine below that is fatal: a test which
# cancels a task while it holds a session lets the cancelled task's cleanup close
# that connection, the pool's next checkout opens a FRESH, EMPTY database, and
# every later test in the module dies on "no such table".
# test_spool_recovery.test_dedup_blocks_while_incident_active cancels a live
# recovery task on purpose and poisoned the 247 tests after it that way. The
# shared engine therefore takes a FILE url, which selects AsyncAdaptedQueuePool
# where connections are disposable and the data outlives them.
#
# A throwaway engine that ONE test creates, owns and discards cannot hit that --
# there is no later test to poison. Charging those callers the file price to
# protect the shared fixture is not free: `:memory:` + a 69-table create_all
# measures 0.045 s against 0.63-0.98 s file-backed, and every run_migrations call
# pays it again, which cost the 32-file migration family 39% (176.6 s -> 245.6 s).
#
# The files land wherever `tempfile` points (TMPDIR), NOT under DATA_DIR.
_TEST_DB_SEQ = itertools.count()

#: For a throwaway engine owned by a single test. Fast, and unsafe to SHARE.
MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


def _shared_database_url() -> str:
    """A fresh SQLite FILE url, for an engine outliving the test that built it."""
    directory = Path(tempfile.mkdtemp(prefix="bbtestdb_"))
    return f"sqlite+aiosqlite:///{(directory / f'test_{next(_TEST_DB_SEQ)}.db').as_posix()}"


_models_imported = False


def import_all_models() -> None:
    """Import every ``backend.app.models`` submodule so ``Base.metadata`` is complete.

    Derived from the package contents rather than an explicit list: a new model
    module is registered the moment it exists, with nothing to forget to update.
    Idempotent and cheap after the first call (``sys.modules`` hit per module).

    Completeness is not cosmetic. ``run_migrations`` ALTERs tables right across the
    schema and ``_safe_execute`` deliberately RE-RAISES ``no such table`` — that is
    schema corruption, not idempotency — so under a PARTIAL ``create_all`` a migration
    test passes or fails according to which other module happened to import a model
    first. The 29 hand-written registration helpers this replaced ranged from 5 names
    to 33; the shortest could not build the tables its own ALTERs named.
    """
    global _models_imported
    if _models_imported:
        return
    from backend.app import models as models_pkg

    for module_info in pkgutil.iter_modules(models_pkg.__path__):
        importlib.import_module(f"{models_pkg.__name__}.{module_info.name}")
    _models_imported = True


async def create_memory_engine(*, echo: bool = False) -> AsyncEngine:
    """Build a genuinely IN-MEMORY engine with the FULL schema already created.

    For an engine ONE test creates, owns and discards -- the shape every
    migration test wants. Do NOT share the result across tests: `:memory:`
    selects StaticPool, so the single connection IS the database and anything
    that closes it (a cancelled task holding a session) takes the data with it.
    An engine that has to outlive its creating test wants `_shared_database_url`
    instead; `module_database` below is the one such caller.

    The shared replacement for the 23 hand-rolled ``engine()`` fixtures across
    the migration tests. Callers that need a pre-migration schema run their own
    ``ALTER TABLE ... DROP COLUMN`` afterwards:

        eng = await create_memory_engine()
        async with eng.begin() as conn:
            await conn.execute(text(f"ALTER TABLE eject_profiles DROP COLUMN {_NEW_COLUMN}"))
    """
    import_all_models()
    engine = create_async_engine(MEMORY_DATABASE_URL, echo=echo)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine


async def boot_schema(engine: AsyncEngine) -> None:
    """Bring ``engine`` up the way a real boot does: ``create_all`` + ``run_migrations``.

    What ``init_db`` does to the schema, minus the seeders — and in ONE transaction,
    because that is how production runs it: a bad migration statement aborts startup
    with the old schema intact rather than half-applied.

    It exists so a migration test can express "and then the app booted on top of this"
    without spelling ``Base.metadata.create_all`` itself. That spelling is what the
    ownership pins in ``unit/test_fixture_ownership.py`` forbid outside this package,
    and rightly: a test that builds the schema its own way is testing its own
    reimplementation of the boot rather than the boot.

    Idempotent by construction — ``create_all`` skips tables that exist and every
    migration statement is written to be re-runnable — so calling it twice is exactly
    the second boot a test may want to assert on.
    """
    import_all_models()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await run_migrations(conn)


@dataclass(slots=True)
class ModuleDatabase:
    """A module's in-memory database plus the table list to truncate between tests."""

    engine: AsyncEngine
    tables: tuple[Table, ...]

    async def reset(self) -> None:
        """Empty every table and restart autoincrement ids.

        Reverse dependency order first: this fork enforces no foreign keys under
        SQLite, so the order is belt-and-braces rather than load-bearing — but it
        costs nothing and keeps the helper correct if PRAGMA foreign_keys is ever
        turned on or the suite is pointed at Postgres.
        """
        async with self.engine.begin() as conn:
            for table in self.tables:
                await conn.execute(table.delete())
            # Several tables are sqlite_autoincrement; tests assert on ids
            # starting at 1, so the sequence table has to go back to empty too.
            # It only exists once such a table has been written to.
            exists = await conn.execute(
                text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'")
            )
            if exists.first() is not None:
                await conn.execute(text("DELETE FROM sqlite_sequence"))


@pytest.fixture(scope="session", autouse=True)
def live_app_database_schema() -> None:
    """Bring the REAL (per-worker, temp) application database up to schema, once.

    ``conftest`` patches ``async_session`` in 3 modules; 15 more bound it at
    import and cannot be patched, so they talk to ``core.database.engine`` —
    the file DB under DATA_DIR. Before DATA_DIR moved that was the repo's own
    15 MB ``bambuddy.db`` (tests were quietly writing to it); now it is an empty
    file per worker, so without this every one of those paths would raise
    ``no such table``.

    Runs the app's own entry point (``init_db()`` = create_all + run_migrations +
    the seeders) rather than a hand-rolled schema build, so the harness can never
    disagree with production about what a fresh database contains.

    Sync fixture + ``asyncio.run`` on purpose: the engine's pool is disposed
    inside the same throwaway loop, so no connection survives bound to a loop
    that is about to close — the per-module loops each open their own.
    """
    from backend.app.core import database as database_module

    async def _bootstrap() -> None:
        try:
            await database_module.init_db()
        finally:
            await database_module.engine.dispose()
            # Once per session (not per test): let aiosqlite's worker thread
            # finish the close handshake before this loop goes away.
            await asyncio.sleep(0.05)

    asyncio.run(_bootstrap())


@pytest.fixture(scope="module", autouse=True)
async def dispose_live_engine_after_module() -> AsyncGenerator[None, None]:
    """Leave no live-engine connection bound to a loop that is about to close.

    ``core.database.engine`` is a process-global whose pool caches connections on
    whichever loop first used them. With one loop per module, a connection opened
    in module A and reused in module B is the classic "attached to a different
    loop" failure. Disposing at module teardown costs one call per module and
    makes the next module start from a clean pool. Read the attribute at teardown
    time — ``reinitialize_database()`` rebinds it.
    """
    yield
    from backend.app.core import database as database_module

    await database_module.engine.dispose()


@pytest.fixture(scope="module")
async def module_database() -> AsyncGenerator[ModuleDatabase, None]:
    """The module's in-memory engine: built once, torn down once."""
    import_all_models()
    # FILE-backed on purpose: this engine is shared by every test in the module,
    # which is exactly the ownership the StaticPool hazard above bites.
    engine = create_async_engine(_shared_database_url())
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    database = ModuleDatabase(engine=engine, tables=tuple(reversed(Base.metadata.sorted_tables)))
    yield database
    await engine.dispose()


@pytest.fixture
async def test_engine(module_database: ModuleDatabase) -> AsyncEngine:
    """The engine every test sees — module-scoped resource, per-test empty state.

    Truncation happens at SETUP, not teardown: a test that leaves rows behind can
    still be inspected after a failure, and a hard-crashed test cannot skip the
    cleanup its successor depends on.
    """
    await module_database.reset()
    return module_database.engine


@pytest.fixture
async def db_session(test_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Create a test database session."""
    async_session_maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session_maker() as session:
        yield session


@pytest.fixture
def own_session_factory(test_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """An INDEPENDENT session maker on the test engine.

    The shape a service that opens its own session takes in production
    (``core.database.async_session``) — e.g. ``spool_respool.confirm_backup_swaps``,
    which ``main`` fires as a bare task with no session to borrow. Using this instead of
    ``db_session`` is what makes such a service run its real commit boundary: work it
    lands is committed by ANOTHER session, so a test observing it through ``db_session``
    must ``refresh()`` rather than read a stale identity-mapped instance.
    """
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
async def wal_session_factory(tmp_path: Path) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    """Sessions on a FILE database wearing PRODUCTION's connection pragmas.

    The shared test engine deliberately runs the driver defaults (rollback journal, a
    5 s busy handler), which is enough to test what the farm writes but not HOW its
    writers contend: that is decided by ``core.database._set_sqlite_pragmas`` — WAL
    (readers never block the writer, and a stale read snapshot CANNOT be upgraded to a
    write) and ``busy_timeout``. A test about lock contention must run under exactly
    those rules, so this engine is built with the production connect hook attached,
    its own file under ``tmp_path``, and the full schema. Owned and disposed per test.
    """
    from sqlalchemy import event

    from backend.app.core.database import _set_sqlite_pragmas

    import_all_models()
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'wal.db').as_posix()}")
    event.listen(engine.sync_engine, "connect", _set_sqlite_pragmas)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
def force_sqlite_dialect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the SQLite branch regardless of test env settings.

    THE copy, and ``conftest`` re-exports it — a test module needs NO import to reach
    it, only a request. Where the local copy being replaced was ``autouse`` (all 30 of
    the migration-test copies were), the equivalent is a module-level mark, because
    importing a fixture does NOT make it autouse:

        pytestmark = pytest.mark.usefixtures("force_sqlite_dialect")

    ``database.py`` imported ``is_sqlite`` at module load, so the name has to be
    patched there as well as on ``db_dialect``. Note that two files
    (``test_hms_event.py``, ``test_foreign_print_accounting.py``) patch only
    ``database.is_sqlite`` — adopting this fixture there also pins
    ``db_dialect.is_postgres``, which is a behaviour change to check, not a
    rename.
    """
    from backend.app.core import database as database_module, db_dialect

    monkeypatch.setattr(db_dialect, "is_sqlite", lambda: True)
    monkeypatch.setattr(db_dialect, "is_postgres", lambda: False)
    monkeypatch.setattr(database_module, "is_sqlite", lambda: True)
