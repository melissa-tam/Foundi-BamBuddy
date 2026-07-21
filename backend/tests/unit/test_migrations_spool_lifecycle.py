"""Regression tests for the spool-lifecycle migration (WI-1 / WI-5).

Two migrations, both appended to ``run_migrations``:

WI-1 (FIFO substrate): add ``spool.first_loaded_at`` and backfill it with
``created_at`` for any spool that has ever been in service (has an assignment,
usage history, a ``last_used`` timestamp, or consumed grams). Pristine,
never-assigned inventory spools stay NULL.

WI-5 (settings remap): the boolean ``prefer_lowest_filament`` setting is
replaced by the tri-state ``spool_selection_policy``. A truthy legacy flag maps
to ``spool_selection_policy = 'lowest_remaining'``; false/absent maps to
nothing (the new ``first_loaded`` default applies); the old key is always
dropped. Both migrations are idempotent and SQLite-safe (mirrors the other
migration regression tests in this suite).
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.core.database import run_migrations

_FIXED_CREATED = datetime(2026, 1, 1, 12, 0, 0)


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
    yield eng
    await eng.dispose()


@pytest.fixture
def session_maker(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _spool_columns(conn) -> set[str]:
    rows = (await conn.execute(text("PRAGMA table_info(spool)"))).fetchall()
    return {row[1] for row in rows}


async def _make_spool(session: AsyncSession, material: str = "PETG", **kwargs):
    from backend.app.models.spool import Spool

    spool = Spool(material=material, created_at=_FIXED_CREATED, **kwargs)
    session.add(spool)
    await session.flush()
    return spool


# ---------------------------------------------------------------------------
# WI-1: first_loaded_at column + backfill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_readds_first_loaded_at_column(engine):
    """Dropping the column simulates a pre-migration schema; run_migrations re-adds it."""
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE spool DROP COLUMN first_loaded_at"))
        assert "first_loaded_at" not in await _spool_columns(conn)
    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.connect() as conn:
        assert "first_loaded_at" in await _spool_columns(conn)


@pytest.mark.asyncio
async def test_backfill_stamps_in_service_spools_only(engine, session_maker):
    """Assigned / used / last_used / consumed spools get first_loaded_at=created_at;
    a pristine never-assigned spool stays NULL."""
    from backend.app.models.spool_assignment import SpoolAssignment
    from backend.app.models.spool_usage_history import SpoolUsageHistory

    async with session_maker() as session:
        assigned = await _make_spool(session)
        used = await _make_spool(session)
        consumed = await _make_spool(session, weight_used=5.0)
        recently_used = await _make_spool(session, last_used=datetime(2026, 2, 2, 9, 0, 0))
        pristine = await _make_spool(session)

        session.add(SpoolAssignment(spool_id=assigned.id, printer_id=1, ams_id=0, tray_id=0))
        session.add(SpoolUsageHistory(spool_id=used.id, weight_used=10.0, percent_used=1))
        await session.commit()

        ids = {
            "assigned": assigned.id,
            "used": used.id,
            "consumed": consumed.id,
            "recently_used": recently_used.id,
            "pristine": pristine.id,
        }

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT id, first_loaded_at, created_at FROM spool"))).fetchall()
    by_id = {r[0]: (r[1], r[2]) for r in rows}

    for label in ("assigned", "used", "consumed", "recently_used"):
        first_loaded, created = by_id[ids[label]]
        assert first_loaded is not None, f"{label} should be backfilled"
        assert first_loaded == created, f"{label} first_loaded_at should equal created_at"

    assert by_id[ids["pristine"]][0] is None, "pristine unassigned spool must stay NULL"


@pytest.mark.asyncio
async def test_backfill_is_idempotent(engine, session_maker):
    """Running the migration twice never re-stamps or clobbers; pristine stays NULL."""
    from backend.app.models.spool_assignment import SpoolAssignment

    async with session_maker() as session:
        assigned = await _make_spool(session)
        pristine = await _make_spool(session)
        session.add(SpoolAssignment(spool_id=assigned.id, printer_id=1, ams_id=0, tray_id=0))
        await session.commit()
        assigned_id, pristine_id = assigned.id, pristine.id

    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.connect() as conn:
        first_pass = {
            r[0]: r[1] for r in (await conn.execute(text("SELECT id, first_loaded_at FROM spool"))).fetchall()
        }

    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.connect() as conn:
        second_pass = {
            r[0]: r[1] for r in (await conn.execute(text("SELECT id, first_loaded_at FROM spool"))).fetchall()
        }

    assert first_pass == second_pass
    assert first_pass[assigned_id] is not None
    assert first_pass[pristine_id] is None


# ---------------------------------------------------------------------------
# WI-5: prefer_lowest_filament -> spool_selection_policy remap
# ---------------------------------------------------------------------------


async def _seed_settings(session: AsyncSession, **kv):
    from backend.app.models.settings import Settings

    for key, value in kv.items():
        session.add(Settings(key=key, value=value))
    await session.commit()


async def _settings_map(conn) -> dict[str, str]:
    rows = (await conn.execute(text("SELECT key, value FROM settings"))).fetchall()
    return {r[0]: r[1] for r in rows}


@pytest.mark.asyncio
async def test_remap_true_becomes_lowest_remaining(engine, session_maker):
    async with session_maker() as session:
        await _seed_settings(session, prefer_lowest_filament="true")

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        settings = await _settings_map(conn)
    assert settings.get("spool_selection_policy") == "lowest_remaining"
    assert "prefer_lowest_filament" not in settings


@pytest.mark.asyncio
async def test_remap_true_case_insensitive(engine, session_maker):
    """Bool settings are stored 'true' but a capitalized 'True' must still remap."""
    async with session_maker() as session:
        await _seed_settings(session, prefer_lowest_filament="True")

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        settings = await _settings_map(conn)
    assert settings.get("spool_selection_policy") == "lowest_remaining"
    assert "prefer_lowest_filament" not in settings


@pytest.mark.asyncio
async def test_remap_false_creates_no_policy(engine, session_maker):
    async with session_maker() as session:
        await _seed_settings(session, prefer_lowest_filament="false")

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        settings = await _settings_map(conn)
    assert "spool_selection_policy" not in settings
    assert "prefer_lowest_filament" not in settings


@pytest.mark.asyncio
async def test_remap_absent_creates_no_policy(engine):
    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.connect() as conn:
        settings = await _settings_map(conn)
    assert "spool_selection_policy" not in settings
    assert "prefer_lowest_filament" not in settings


@pytest.mark.asyncio
async def test_remap_preserves_existing_policy(engine, session_maker):
    """If a policy row already exists, the truthy legacy flag must NOT overwrite it."""
    async with session_maker() as session:
        await _seed_settings(session, prefer_lowest_filament="true", spool_selection_policy="slot_order")

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        settings = await _settings_map(conn)
    assert settings.get("spool_selection_policy") == "slot_order"
    assert "prefer_lowest_filament" not in settings


@pytest.mark.asyncio
async def test_remap_is_idempotent(engine, session_maker):
    async with session_maker() as session:
        await _seed_settings(session, prefer_lowest_filament="true")

    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        settings = await _settings_map(conn)
    assert settings.get("spool_selection_policy") == "lowest_remaining"
    assert "prefer_lowest_filament" not in settings


# ---------------------------------------------------------------------------
# FIFO seating-order fix: loaded_at column + backfill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_readds_loaded_at_column(engine):
    """Dropping the column simulates a pre-migration schema; run_migrations re-adds it
    (idempotent ADD COLUMN)."""
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE spool DROP COLUMN loaded_at"))
        assert "loaded_at" not in await _spool_columns(conn)
    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.connect() as conn:
        assert "loaded_at" in await _spool_columns(conn)
    # Re-add is idempotent — a second run does not raise on the existing column.
    async with engine.begin() as conn:
        await run_migrations(conn)


@pytest.mark.asyncio
async def test_loaded_at_backfill_coalesce_order(engine, session_maker):
    """Backfill = COALESCE(first_loaded_at, created_at) for in-service rows; a pristine
    never-assigned spool stays NULL. A distinct first_loaded_at proves COALESCE prefers
    it over created_at (not just that both happen to equal created_at)."""
    from backend.app.models.spool_assignment import SpoolAssignment

    distinct_first = datetime(2026, 6, 15, 8, 0, 0)  # != _FIXED_CREATED
    async with session_maker() as session:
        # first_loaded_at pre-set and distinct → loaded_at must copy IT, not created_at.
        with_first = await _make_spool(session, first_loaded_at=distinct_first)
        # bound, no first_loaded_at: WI-1 stamps first_loaded_at=created_at, so loaded_at
        # resolves to created_at (the in-service COALESCE-fallback value).
        bound = await _make_spool(session)
        pristine = await _make_spool(session)
        session.add(SpoolAssignment(spool_id=bound.id, printer_id=1, ams_id=0, tray_id=0))
        await session.commit()
        ids = {"with_first": with_first.id, "bound": bound.id, "pristine": pristine.id}

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT id, loaded_at, first_loaded_at, created_at FROM spool"))).fetchall()
    by_id = {r[0]: (r[1], r[2], r[3]) for r in rows}

    loaded, first, _created = by_id[ids["with_first"]]
    assert loaded is not None and loaded == first, "loaded_at should copy the distinct first_loaded_at"

    loaded, first, created = by_id[ids["bound"]]
    assert loaded is not None and loaded == created == first, "bound row → loaded_at = created_at"

    assert by_id[ids["pristine"]][0] is None, "pristine unassigned spool must stay NULL"


@pytest.mark.asyncio
async def test_loaded_at_backfill_never_overwrites_and_is_idempotent(engine, session_maker):
    """An existing loaded_at stamp is never clobbered (even when first_loaded_at differs),
    and running the migration twice is a no-op."""
    prior_loaded = datetime(2026, 7, 20, 10, 0, 0)
    older_first = datetime(2026, 1, 1, 12, 0, 0)
    async with session_maker() as session:
        stamped = await _make_spool(session, loaded_at=prior_loaded, first_loaded_at=older_first)
        pristine = await _make_spool(session)
        await session.commit()
        stamped_id, pristine_id = stamped.id, pristine.id

    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.connect() as conn:
        first_pass = {r[0]: r[1] for r in (await conn.execute(text("SELECT id, loaded_at FROM spool"))).fetchall()}

    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.connect() as conn:
        second_pass = {r[0]: r[1] for r in (await conn.execute(text("SELECT id, loaded_at FROM spool"))).fetchall()}

    assert first_pass == second_pass, "second run must not change any loaded_at"
    # The pre-existing stamp survived (not overwritten to first_loaded_at).
    assert first_pass[stamped_id] is not None and first_pass[stamped_id] != str(older_first)
    assert first_pass[pristine_id] is None


# ---------------------------------------------------------------------------
# R1: min_start_spool_g default 120 -> 150 migration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_min_start_old_default_migrated(engine, session_maker):
    """A stored value still on the OLD default (120) is bumped to the new default 150."""
    async with session_maker() as session:
        await _seed_settings(session, min_start_spool_g="120")

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        settings = await _settings_map(conn)
    assert settings.get("min_start_spool_g") == "150"


@pytest.mark.asyncio
async def test_min_start_custom_value_untouched(engine, session_maker):
    """A deliberately-different operator value (not the old default) is left alone."""
    async with session_maker() as session:
        await _seed_settings(session, min_start_spool_g="80")

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        settings = await _settings_map(conn)
    assert settings.get("min_start_spool_g") == "80"


@pytest.mark.asyncio
async def test_min_start_absent_stays_absent(engine):
    """No stored row → the migration creates nothing (schema default 150 materialises at
    read time)."""
    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.connect() as conn:
        settings = await _settings_map(conn)
    assert "min_start_spool_g" not in settings


@pytest.mark.asyncio
async def test_min_start_migration_idempotent(engine, session_maker):
    """Running twice keeps 150 (the second pass no longer matches value='120')."""
    async with session_maker() as session:
        await _seed_settings(session, min_start_spool_g="120")

    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        settings = await _settings_map(conn)
    assert settings.get("min_start_spool_g") == "150"
