"""Regression test for the eject_profiles bed-drop column migration.

Adds ``bed_drop_clearance_mm`` (nullable — NULL = the bed-drop release assist is
off) to an existing ``eject_profiles`` table. ``create_all`` would create the
column from the current model and mask the migration, so the fixture drops it
first to simulate a pre-migration schema; the test then proves ``run_migrations``
re-adds it, that a row inserted without it defaults to NULL, and that a second
pass is a no-op. Idempotent and SQLite-safe (mirrors the sweep-columns migration
regression test in this suite).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core.database import run_migrations

_NEW_COLUMN = "bed_drop_clearance_mm"


@pytest.fixture(autouse=True)
def force_sqlite_dialect(monkeypatch):
    """Force the SQLite branch regardless of test env settings."""
    from backend.app.core import db_dialect

    monkeypatch.setattr(db_dialect, "is_sqlite", lambda: True)
    monkeypatch.setattr(db_dialect, "is_postgres", lambda: False)
    from backend.app.core import database as database_module

    monkeypatch.setattr(database_module, "is_sqlite", lambda: True)


def _register_all_models():
    from backend.app.models import (  # noqa: F401
        ams_history,
        ams_label,
        api_key,
        archive,
        color_catalog,
        eject_profile,
        external_link,
        filament,
        group,
        kprofile_note,
        library,
        maintenance,
        notification,
        notification_template,
        print_log,
        print_queue,
        printer,
        printer_model_geometry,
        project,
        project_bom,
        settings,
        slot_preset,
        smart_plug,
        smart_plug_energy_snapshot,
        spool,
        spool_assignment,
        spool_catalog,
        spool_k_profile,
        spool_usage_history,
        spoolbuddy_device,
        user,
        user_email_pref,
        virtual_printer,
    )


@pytest.fixture
async def engine():
    from backend.app.core.database import Base

    _register_all_models()

    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Simulate a pre-migration schema: drop the column the current model
        # created so run_migrations actually has to re-add it (SQLite 3.35+).
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
    """A row inserted without the column gets NULL (assist off, unchanged behaviour)."""
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
                "(name, cooldown_temp_c, clearance_mm, z_offset_mm, "
                "descent_steps, x_passes, x_margin_mm, front_overhang_mm, back_overhang_mm, "
                "eject_speed_mm_min, skim_speed_mm_min, cooling_fan_assist, max_part_height_mm, "
                "sweep_start_frac, final_skim) "
                "VALUES ('migrated', 28, 10, 0.4, 4, 11, 3, 2, 2, 3000, 1500, 1, 42, 1.0, 1)"
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
                "(name, cooldown_temp_c, clearance_mm, z_offset_mm, "
                "descent_steps, x_passes, x_margin_mm, front_overhang_mm, back_overhang_mm, "
                "eject_speed_mm_min, skim_speed_mm_min, cooling_fan_assist, max_part_height_mm, "
                "sweep_start_frac, final_skim, "
                f"{_NEW_COLUMN}) "
                "VALUES ('dropper', 28, 10, 0.4, 4, 11, 3, 2, 2, 3000, 1500, 1, 42, 1.0, 1, 50)"
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
