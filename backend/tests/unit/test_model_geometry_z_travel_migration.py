"""Regression test for the printer_model_geometry z_travel_mm column migration.

``z_travel_mm`` is nullable — the machine bottom the bed-drop release assist
drives to — and the seeded H2S (340) / H2C (325) rows are backfilled. An
operator's PUT-set value survives a re-run (the backfill is WHERE IS NULL).

Two traps the migration has to survive, one per fixture:

* on an EXISTING DB whose table predates the column, the seed INSERTs name
  ``z_travel_mm`` and would raise unless the ALTER has added it first;
* a FRESH DB has no table at all, so ``CREATE TABLE`` must build it WITH the
  column and the seeds carry the value directly.

The full schema is built (``create_memory_engine``) before ``run_migrations``
because its many other table ALTERs raise ``no such table`` on a table that was
never created — an error ``_safe_execute`` does NOT swallow.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from backend.app.core.database import run_migrations
from backend.tests._fixtures.db import create_memory_engine

pytestmark = pytest.mark.usefixtures("force_sqlite_dialect")

# The pre-z_travel_mm geometry table, recreated verbatim to simulate an old DB.
_OLD_SCHEMA_NO_Z_TRAVEL = """
CREATE TABLE printer_model_geometry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_key VARCHAR(50) NOT NULL UNIQUE,
    bed_x FLOAT NOT NULL,
    bed_y FLOAT NOT NULL,
    env_x_min FLOAT NOT NULL,
    env_x_max FLOAT NOT NULL,
    env_y_min FLOAT NOT NULL,
    env_y_max FLOAT NOT NULL,
    max_part_height_mm FLOAT NOT NULL,
    validated BOOLEAN NOT NULL DEFAULT 0,
    notes TEXT,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""

_SEED_H2S = (
    "INSERT INTO printer_model_geometry "
    "(model_key, bed_x, bed_y, env_x_min, env_x_max, env_y_min, env_y_max, max_part_height_mm, validated, notes) "
    "VALUES ('H2S', 340, 320, 0, 340, -16, 325, 42, 1, 'existing')"
)
_SEED_H2C = (
    "INSERT INTO printer_model_geometry "
    "(model_key, bed_x, bed_y, env_x_min, env_x_max, env_y_min, env_y_max, max_part_height_mm, validated, notes) "
    "VALUES ('H2C', 330, 320, 15, 325, 0, 320, 42, 1, 'existing')"
)


async def _columns(conn) -> set[str]:
    rows = (await conn.execute(text("PRAGMA table_info(printer_model_geometry)"))).fetchall()
    return {row[1] for row in rows}


async def _z_travel(conn, model_key: str):
    row = (
        await conn.execute(
            text("SELECT z_travel_mm FROM printer_model_geometry WHERE model_key = :k"), {"k": model_key}
        )
    ).fetchone()
    return None if row is None else row[0]


@pytest.fixture
async def existing_engine():
    """A DB whose geometry table predates z_travel_mm, pre-seeded with H2S + H2C."""
    eng = await create_memory_engine()
    async with eng.begin() as conn:
        # Replace the current-model table with the pre-migration schema + seed rows.
        await conn.execute(text("DROP TABLE printer_model_geometry"))
        await conn.execute(text(_OLD_SCHEMA_NO_Z_TRAVEL))
        await conn.execute(text(_SEED_H2S))
        await conn.execute(text(_SEED_H2C))
    yield eng
    await eng.dispose()


@pytest.fixture
async def fresh_engine():
    """A DB with NO geometry table — run_migrations must CREATE + seed it."""
    eng = await create_memory_engine()
    async with eng.begin() as conn:
        await conn.execute(text("DROP TABLE printer_model_geometry"))
    yield eng
    await eng.dispose()


@pytest.mark.asyncio
async def test_pre_migration_table_lacks_z_travel(existing_engine):
    async with existing_engine.connect() as conn:
        assert "z_travel_mm" not in await _columns(conn)


@pytest.mark.asyncio
async def test_migration_adds_column_and_backfills(existing_engine):
    async with existing_engine.begin() as conn:
        await run_migrations(conn)
    async with existing_engine.connect() as conn:
        assert "z_travel_mm" in await _columns(conn)
        assert await _z_travel(conn, "H2S") == 340.0
        assert await _z_travel(conn, "H2C") == 325.0


@pytest.mark.asyncio
async def test_operator_value_survives_rerun(existing_engine):
    """An operator PUT sets z_travel_mm; the WHERE-IS-NULL backfill must not clobber
    it on the next boot's re-run."""
    async with existing_engine.begin() as conn:
        await run_migrations(conn)
    async with existing_engine.begin() as conn:
        await conn.execute(text("UPDATE printer_model_geometry SET z_travel_mm = 111 WHERE model_key = 'H2S'"))
    async with existing_engine.begin() as conn:
        await run_migrations(conn)  # second boot
    async with existing_engine.connect() as conn:
        assert await _z_travel(conn, "H2S") == 111.0  # operator value preserved
        assert await _z_travel(conn, "H2C") == 325.0  # untouched neighbour still backfilled


@pytest.mark.asyncio
async def test_fresh_create_and_seed_carries_z_travel(fresh_engine):
    async with fresh_engine.begin() as conn:
        await run_migrations(conn)
    async with fresh_engine.connect() as conn:
        assert "z_travel_mm" in await _columns(conn)
        assert await _z_travel(conn, "H2S") == 340.0
        assert await _z_travel(conn, "H2C") == 325.0


@pytest.mark.asyncio
async def test_migration_is_idempotent(existing_engine):
    async with existing_engine.begin() as conn:
        await run_migrations(conn)
    async with existing_engine.begin() as conn:
        await run_migrations(conn)
    async with existing_engine.connect() as conn:
        assert "z_travel_mm" in await _columns(conn)
        assert await _z_travel(conn, "H2S") == 340.0
        assert await _z_travel(conn, "H2C") == 325.0
