"""The scheduled-return migration — ``printer_incident.release_at`` (2026-10-16).

``create_all`` builds the column from the model and would mask the migration, so the fixture
DROPs it to reconstruct the schema an existing install boots with. The column is additive and
nullable: a hold written before it reads NULL (no scheduled return), and running the migration
twice is a no-op.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import text

from backend.app.core.database import Base, run_migrations
from backend.tests._fixtures.db import create_memory_engine

pytestmark = pytest.mark.usefixtures("force_sqlite_dialect")


@pytest.fixture
async def engine():
    eng = await create_memory_engine()
    async with eng.begin() as conn:
        await conn.execute(text("ALTER TABLE printer_incident DROP COLUMN release_at"))
    yield eng
    await eng.dispose()


async def _columns(conn) -> set[str]:
    return {row[1] for row in await conn.execute(text("PRAGMA table_info(printer_incident)"))}


@pytest.mark.asyncio
async def test_the_fixture_builds_the_pre_migration_schema(engine):
    async with engine.connect() as conn:
        assert "release_at" not in await _columns(conn)


@pytest.mark.asyncio
async def test_the_migration_adds_the_column_idempotently(engine):
    async with engine.begin() as conn:
        await run_migrations(conn)
        await run_migrations(conn)

    async with engine.connect() as conn:
        assert "release_at" in await _columns(conn)


@pytest.mark.asyncio
async def test_a_hold_written_before_the_column_reads_null(engine):
    async with engine.begin() as conn:
        printers = Base.metadata.tables["printers"]
        printer_id = (
            await conn.execute(
                printers.insert().values(name="009-H2S", serial_number="SN-009", ip_address="10.0.0.9", access_code="x")
            )
        ).inserted_primary_key[0]
        await conn.execute(
            text(
                "INSERT INTO printer_incident (printer_id, job_id, kind, code, codes, status, created_at, escalated_at) "
                "VALUES (:p, '', 'service_hold', '', '', 'escalated', :t, :t)"
            ),
            {"p": printer_id, "t": datetime(2026, 10, 15, 8, 0, 0)},
        )
        await run_migrations(conn)

    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT release_at FROM printer_incident"))).scalar() is None
