"""The ``ux_print_archives_live_printer`` migration — one ``printing`` archive per printer.

``create_all`` builds the index from the model and would mask the migration, so the fixture DROPs
it to reconstruct the schema an existing install boots with. The migration runs BELOW the replay
repair, which closes a printer's extra ``printing`` archives first; should that repair have rolled
back, duplicates remain and the build must fail SOFT — a UNIQUE index that aborts startup on a
production farm is a far worse failure than a boot without the pin.
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import text

from backend.app.core.database import Base, run_migrations
from backend.tests._fixtures.db import create_memory_engine

pytestmark = pytest.mark.usefixtures("force_sqlite_dialect")

_INDEX = "ux_print_archives_live_printer"
_REPAIR_MARKER = "repair_foreign_replay_20260925"


@pytest.fixture
async def engine():
    eng = await create_memory_engine()
    async with eng.begin() as conn:
        await conn.execute(text(f"DROP INDEX {_INDEX}"))
    yield eng
    await eng.dispose()


async def _index_sql(conn) -> str | None:
    return (
        await conn.execute(text("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = :n"), {"n": _INDEX})
    ).scalar()


async def _seed_printing(conn, printer_id: int, n: int) -> None:
    archives = Base.metadata.tables["print_archives"]
    for i in range(n):
        await conn.execute(
            archives.insert().values(
                printer_id=printer_id, filename=f"a{i}.3mf", file_path="", file_size=0, status="printing"
            )
        )


@pytest.mark.asyncio
async def test_the_fixture_builds_the_pre_migration_schema(engine):
    async with engine.connect() as conn:
        assert await _index_sql(conn) is None


@pytest.mark.asyncio
async def test_the_migration_builds_the_partial_index(engine):
    async with engine.begin() as conn:
        await _seed_printing(conn, printer_id=1, n=1)
        await _seed_printing(conn, printer_id=2, n=1)
        await run_migrations(conn)
        await run_migrations(conn)  # idempotent

    async with engine.connect() as conn:
        sql = await _index_sql(conn)
    assert sql is not None
    assert "UNIQUE" in sql.upper() and "WHERE" in sql.upper() and "'printing'" in sql


@pytest.mark.asyncio
async def test_duplicates_the_repair_did_not_close_do_not_abort_startup(engine, caplog):
    """The repair's marker is already written (it will not run again) yet a printer still holds
    two live archives: the migration logs, skips the index and lets the farm boot."""
    settings = Base.metadata.tables["settings"]
    async with engine.begin() as conn:
        await conn.execute(settings.insert().values(key=_REPAIR_MARKER, value="true"))
        await _seed_printing(conn, printer_id=3, n=2)

    with caplog.at_level(logging.ERROR, logger="backend.app.core.database"):
        async with engine.begin() as conn:
            await run_migrations(conn)

    async with engine.connect() as conn:
        assert await _index_sql(conn) is None
        live = (
            await conn.execute(text("SELECT COUNT(*) FROM print_archives WHERE printer_id = 3 AND status = 'printing'"))
        ).scalar()
    assert live == 2, "the migration never touches the rows — closing them is the repair's job"
    assert any(_INDEX in record.getMessage() and "not built" in record.getMessage() for record in caplog.records)
