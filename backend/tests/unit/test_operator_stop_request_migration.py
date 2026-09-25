"""The durable operator stop's migration — ``print_queue.operator_stop_requested_at`` / ``stop_answered_at``.

``create_all`` builds both columns from the model and would mask the migration, so the fixture
DROPs them to reconstruct the schema an existing install boots with. The backfill gives a row an
operator's UI Stop already ended its request — the row a deploy lands on: stopped on the old build,
its printer's terminal arriving on the new one — and touches nothing else.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import text

from backend.app.core.database import Base, run_migrations
from backend.tests._fixtures.db import create_memory_engine

pytestmark = pytest.mark.usefixtures("force_sqlite_dialect")

_COLUMNS = ("operator_stop_requested_at", "stop_answered_at")
_STOPPED_AT = datetime(2026, 9, 25, 1, 30, 0)


@pytest.fixture
async def engine():
    eng = await create_memory_engine()
    async with eng.begin() as conn:
        for column in _COLUMNS:
            await conn.execute(text(f"ALTER TABLE print_queue DROP COLUMN {column}"))
    yield eng
    await eng.dispose()


async def _columns(conn) -> set[str]:
    return {row[1] for row in await conn.execute(text("PRAGMA table_info(print_queue)"))}


async def _seed(conn, **values) -> int:
    queue = Base.metadata.tables["print_queue"]
    present = await _columns(conn)
    row = {key: value for key, value in values.items() if key in present}
    result = await conn.execute(queue.insert().values(position=1, **row))
    return result.inserted_primary_key[0]


async def _request_of(conn, item_id: int):
    return (
        await conn.execute(text("SELECT operator_stop_requested_at FROM print_queue WHERE id = :i"), {"i": item_id})
    ).scalar()


@pytest.mark.asyncio
async def test_the_fixture_builds_the_pre_migration_schema(engine):
    async with engine.connect() as conn:
        assert not set(_COLUMNS) & await _columns(conn)


@pytest.mark.asyncio
async def test_the_migration_adds_both_columns_idempotently(engine):
    async with engine.begin() as conn:
        await run_migrations(conn)
        await run_migrations(conn)

    async with engine.connect() as conn:
        assert set(_COLUMNS) <= await _columns(conn)


@pytest.mark.asyncio
async def test_only_a_unit_an_operators_ui_stop_ended_gets_its_request(engine):
    async with engine.begin() as conn:
        queue_stopped = await _seed(conn, status="cancelled", stop_source="operator_ui", completed_at=_STOPPED_AT)
        no_stop_time = await _seed(conn, status="cancelled", stop_source="operator_ui")
        reconciled = await _seed(conn, status="cancelled", stop_source="reconcile_unknown", completed_at=_STOPPED_AT)
        screen = await _seed(conn, status="cancelled", stop_source="operator_screen", completed_at=_STOPPED_AT)
        printing = await _seed(conn, status="printing")
        failed = await _seed(conn, status="failed", completed_at=_STOPPED_AT)
        await run_migrations(conn)

    async with engine.connect() as conn:
        assert str(await _request_of(conn, queue_stopped)).startswith("2026-09-25 01:30:00")  # the stop's own time
        assert await _request_of(conn, no_stop_time) is not None
        for untouched in (reconciled, screen, printing, failed):
            assert await _request_of(conn, untouched) is None
        answered = await conn.execute(text("SELECT COUNT(*) FROM print_queue WHERE stop_answered_at IS NOT NULL"))
        assert answered.scalar() == 0  # nobody knows whether those stops were answered


@pytest.mark.asyncio
async def test_a_second_boot_never_rewrites_a_request(engine):
    async with engine.begin() as conn:
        await run_migrations(conn)
        item_id = await _seed(
            conn,
            status="cancelled",
            stop_source="operator_ui",
            completed_at=_STOPPED_AT,
            operator_stop_requested_at=datetime(2026, 9, 25, 1, 29, 0),
        )
        await run_migrations(conn)

    async with engine.connect() as conn:
        assert str(await _request_of(conn, item_id)).startswith("2026-09-25 01:29:00")
