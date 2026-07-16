"""Regression test for the eject_profiles sweep-tuning column migration.

Adds ``sweep_x_min_mm`` / ``sweep_x_max_mm`` (optional X sweep sub-band),
``sweep_start_frac`` (descending-sweep start height, NOT NULL DEFAULT 1.0) and
``final_skim`` (trailing skim-pass toggle, NOT NULL DEFAULT 1) to an existing
``eject_profiles`` table. ``create_all`` would create these from the current
model and mask the migration, so the fixture drops them first to simulate a
pre-migration schema; the test then proves ``run_migrations`` re-adds them, that
the frac default is 1.0 and final_skim default is 1 (True), and that a second
pass is a no-op. Idempotent and SQLite-safe (mirrors the other migration
regression tests in this suite).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core.database import run_migrations

_NEW_COLUMNS = ("sweep_x_min_mm", "sweep_x_max_mm", "sweep_start_frac", "final_skim")


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
        # created so run_migrations actually has to re-add them (SQLite 3.35+)...
        for col in _NEW_COLUMNS:
            await conn.execute(text(f"ALTER TABLE eject_profiles DROP COLUMN {col}"))
        # ...and RE-ADD the legacy cooldown_retries column the drop-migration removes
        # (the current model no longer has it, so create_all won't create it).
        await conn.execute(text("ALTER TABLE eject_profiles ADD COLUMN cooldown_retries INTEGER NOT NULL DEFAULT 5"))
    yield eng
    await eng.dispose()


async def _columns(conn) -> set[str]:
    rows = (await conn.execute(text("PRAGMA table_info(eject_profiles)"))).fetchall()
    return {row[1] for row in rows}


@pytest.mark.asyncio
async def test_pre_migration_table_lacks_new_columns(engine):
    """Sanity check: the fixture's simulated old schema is missing the columns."""
    async with engine.connect() as conn:
        cols = await _columns(conn)
    for col in _NEW_COLUMNS:
        assert col not in cols


@pytest.mark.asyncio
async def test_migration_adds_sweep_tuning_columns(engine):
    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.connect() as conn:
        cols = await _columns(conn)
    for col in _NEW_COLUMNS:
        assert col in cols, f"{col} not added by run_migrations"


@pytest.mark.asyncio
async def test_sweep_start_frac_defaults_to_one(engine):
    """A row inserted without the new columns gets frac=1.0 and null band bounds."""
    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO eject_profiles "
                "(name, cooldown_temp_c, clearance_mm, z_offset_mm, "
                "descent_steps, x_passes, x_margin_mm, front_overhang_mm, back_overhang_mm, "
                "eject_speed_mm_min, skim_speed_mm_min, cooling_fan_assist, max_part_height_mm) "
                "VALUES ('migrated', 28, 10, 0.4, 4, 11, 3, 2, 2, 3000, 1500, 1, 42)"
            )
        )
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT sweep_x_min_mm, sweep_x_max_mm, sweep_start_frac "
                    "FROM eject_profiles WHERE name = 'migrated'"
                )
            )
        ).fetchone()
    assert row[0] is None
    assert row[1] is None
    assert row[2] == 1.0


@pytest.mark.asyncio
async def test_final_skim_defaults_to_true(engine):
    """A row inserted without final_skim gets the NOT NULL DEFAULT 1 (True)."""
    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO eject_profiles "
                "(name, cooldown_temp_c, clearance_mm, z_offset_mm, "
                "descent_steps, x_passes, x_margin_mm, front_overhang_mm, back_overhang_mm, "
                "eject_speed_mm_min, skim_speed_mm_min, cooling_fan_assist, max_part_height_mm) "
                "VALUES ('skimdefault', 28, 10, 0.4, 4, 11, 3, 2, 2, 3000, 1500, 1, 42)"
            )
        )
    async with engine.connect() as conn:
        row = (await conn.execute(text("SELECT final_skim FROM eject_profiles WHERE name = 'skimdefault'"))).fetchone()
    assert row[0] == 1


@pytest.mark.asyncio
async def test_migration_drops_cooldown_retries(engine):
    """The eject went server-dispatched motion-only: run_migrations DROPs the
    legacy cooldown_retries column (cooldown_temp_c stays as the release threshold)."""
    async with engine.connect() as conn:
        assert "cooldown_retries" in await _columns(conn)  # fixture seeded the legacy column
    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.connect() as conn:
        cols = await _columns(conn)
    assert "cooldown_retries" not in cols  # dropped
    assert "cooldown_temp_c" in cols  # release threshold retained


@pytest.mark.asyncio
async def test_migration_is_idempotent(engine):
    """Every boot re-runs the migration set; a second pass must be a no-op."""
    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.connect() as conn:
        cols = await _columns(conn)
    for col in _NEW_COLUMNS:
        assert col in cols
    assert "cooldown_retries" not in cols  # stays dropped across re-runs
