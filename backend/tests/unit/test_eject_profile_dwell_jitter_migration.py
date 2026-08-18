"""Regression test for the eject_profiles bed-drop dwell/jitter column migration.

Adds ``bed_drop_dwell_s``, ``bed_drop_jitter_cycles`` and ``bed_drop_jitter_mm``
(all nullable — NULL = that drop-floor behaviour is off) to an existing
``eject_profiles`` table. ``create_all`` would create them from the current model
and mask the migration, so the fixture drops them first to simulate a
pre-migration schema; the test then proves ``run_migrations`` re-adds all three,
that a row inserted without them defaults to NULL, that set values round-trip,
and that a second pass is a no-op. Idempotent and SQLite-safe (mirrors the
bed-drop clearance migration regression test in this suite).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core.database import run_migrations

_NEW_COLUMNS = ("bed_drop_dwell_s", "bed_drop_jitter_cycles", "bed_drop_jitter_mm")


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
        # Simulate a pre-migration schema: drop the columns the current model
        # created so run_migrations actually has to re-add them (SQLite 3.35+).
        for column in _NEW_COLUMNS:
            await conn.execute(text(f"ALTER TABLE eject_profiles DROP COLUMN {column}"))
    yield eng
    await eng.dispose()


async def _columns(conn) -> set[str]:
    rows = (await conn.execute(text("PRAGMA table_info(eject_profiles)"))).fetchall()
    return {row[1] for row in rows}


@pytest.mark.asyncio
async def test_pre_migration_table_lacks_dwell_jitter_columns(engine):
    """Sanity check: the fixture's simulated old schema is missing all three."""
    async with engine.connect() as conn:
        cols = await _columns(conn)
    assert not (set(_NEW_COLUMNS) & cols)


@pytest.mark.asyncio
async def test_migration_adds_dwell_jitter_columns(engine):
    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.connect() as conn:
        cols = await _columns(conn)
    for column in _NEW_COLUMNS:
        assert column in cols, f"{column} not added by run_migrations"


@pytest.mark.asyncio
async def test_dwell_jitter_default_to_null(engine):
    """A row inserted without them gets NULL (behaviours off, unchanged G-code)."""
    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                # sweep_start_frac / final_skim are NOT NULL without a SQL-level
                # default under create_all (SQLAlchemy defaults are Python-side), so
                # a raw INSERT must supply them; the three new columns are omitted on
                # purpose to prove they default to NULL.
                "INSERT INTO eject_profiles "
                "(name, cooldown_temp_c, clearance_mm, z_offset_mm, "
                "descent_steps, x_passes, x_margin_mm, front_overhang_mm, back_overhang_mm, "
                "eject_speed_mm_min, skim_speed_mm_min, cooling_fan_assist, max_part_height_mm, "
                "sweep_start_frac, final_skim, bed_drop_clearance_mm) "
                "VALUES ('migrated', 28, 10, 0.4, 4, 11, 3, 2, 2, 3000, 1500, 1, 42, 1.0, 1, 50)"
            )
        )
    async with engine.connect() as conn:
        row = (
            await conn.execute(text(f"SELECT {', '.join(_NEW_COLUMNS)} FROM eject_profiles WHERE name = 'migrated'"))
        ).fetchone()
    assert list(row) == [None, None, None]


@pytest.mark.asyncio
async def test_dwell_jitter_values_round_trip(engine):
    """Set values store + read back (INTEGER, INTEGER, FLOAT columns)."""
    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO eject_profiles "
                "(name, cooldown_temp_c, clearance_mm, z_offset_mm, "
                "descent_steps, x_passes, x_margin_mm, front_overhang_mm, back_overhang_mm, "
                "eject_speed_mm_min, skim_speed_mm_min, cooling_fan_assist, max_part_height_mm, "
                "sweep_start_frac, final_skim, bed_drop_clearance_mm, "
                f"{', '.join(_NEW_COLUMNS)}) "
                "VALUES ('dropdj', 28, 10, 0.4, 4, 11, 3, 2, 2, 3000, 1500, 1, 42, 1.0, 1, 50, 5, 3, 10.5)"
            )
        )
    async with engine.connect() as conn:
        row = (
            await conn.execute(text(f"SELECT {', '.join(_NEW_COLUMNS)} FROM eject_profiles WHERE name = 'dropdj'"))
        ).fetchone()
    assert list(row) == [5, 3, 10.5]


@pytest.mark.asyncio
async def test_migration_is_idempotent(engine):
    """Every boot re-runs the migration set; a second pass must be a no-op."""
    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.connect() as conn:
        cols = await _columns(conn)
    for column in _NEW_COLUMNS:
        assert column in cols
