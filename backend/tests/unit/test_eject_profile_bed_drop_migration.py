"""Regression test for the eject_profiles bed-drop column migration.

``bed_drop_clearance_mm`` is nullable: NULL = the bed-drop release assist is off.

``create_all`` builds the column from the current model and would mask the
migration entirely, so the fixture DROPs it (SQLite 3.35+) to reconstruct the
pre-migration schema ``run_migrations`` actually has to repair.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from backend.app.core.database import run_migrations
from backend.tests._fixtures.db import create_memory_engine

pytestmark = pytest.mark.usefixtures("force_sqlite_dialect")

_NEW_COLUMN = "bed_drop_clearance_mm"


@pytest.fixture
async def engine():
    eng = await create_memory_engine()
    async with eng.begin() as conn:
        await conn.execute(text(f"ALTER TABLE eject_profiles DROP COLUMN {_NEW_COLUMN}"))
    yield eng
    await eng.dispose()


async def _columns(conn) -> set[str]:
    rows = (await conn.execute(text("PRAGMA table_info(eject_profiles)"))).fetchall()
    return {row[1] for row in rows}


@pytest.mark.asyncio
async def test_pre_migration_table_lacks_bed_drop_column(engine):
    """Sanity check: the fixture's simulated old schema is missing the column."""
    async with engine.connect() as conn:
        cols = await _columns(conn)
    assert _NEW_COLUMN not in cols


@pytest.mark.asyncio
async def test_migration_adds_bed_drop_column(engine):
    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.connect() as conn:
        cols = await _columns(conn)
    assert _NEW_COLUMN in cols, f"{_NEW_COLUMN} not added by run_migrations"


@pytest.mark.asyncio
async def test_bed_drop_defaults_to_null(engine):
    """A row inserted without the column gets NULL (assist off)."""
    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                # sweep_start_frac / final_skim are NOT NULL without a SQL-level
                # default under create_all (SQLAlchemy defaults are Python-side), so
                # a raw INSERT must supply them; bed_drop_clearance_mm is omitted on
                # purpose to prove it defaults to NULL.
                "INSERT INTO eject_profiles "
                "(name, clearance_mm, z_offset_mm, "
                "descent_steps, x_passes, x_margin_mm, front_overhang_mm, back_overhang_mm, "
                "eject_speed_mm_min, skim_speed_mm_min, max_part_height_mm, "
                "sweep_start_frac, final_skim) "
                "VALUES ('migrated', 10, 0.4, 4, 11, 3, 2, 2, 3000, 1500, 42, 1.0, 1)"
            )
        )
    async with engine.connect() as conn:
        row = (await conn.execute(text(f"SELECT {_NEW_COLUMN} FROM eject_profiles WHERE name = 'migrated'"))).fetchone()
    assert row[0] is None


@pytest.mark.asyncio
async def test_bed_drop_value_round_trips(engine):
    """A set clearance stores + reads back (the column is a real FLOAT)."""
    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO eject_profiles "
                "(name, clearance_mm, z_offset_mm, "
                "descent_steps, x_passes, x_margin_mm, front_overhang_mm, back_overhang_mm, "
                "eject_speed_mm_min, skim_speed_mm_min, max_part_height_mm, "
                "sweep_start_frac, final_skim, "
                f"{_NEW_COLUMN}) "
                "VALUES ('dropper', 10, 0.4, 4, 11, 3, 2, 2, 3000, 1500, 42, 1.0, 1, 50)"
            )
        )
    async with engine.connect() as conn:
        row = (await conn.execute(text(f"SELECT {_NEW_COLUMN} FROM eject_profiles WHERE name = 'dropper'"))).fetchone()
    assert row[0] == 50.0


@pytest.mark.asyncio
async def test_migration_is_idempotent(engine):
    """Every boot re-runs the migration set; a second pass must be a no-op."""
    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.connect() as conn:
        cols = await _columns(conn)
    assert _NEW_COLUMN in cols
