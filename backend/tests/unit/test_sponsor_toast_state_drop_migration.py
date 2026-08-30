"""Regression test for the ``sponsor_toast_state`` drop migration (2026-08-30).

The in-app sponsor toast ("Bambuddy stays free thanks to its supporters") and its
whole lane — check/dismiss routes, trigger service, model, hook — were removed. The
model is gone, so ``create_all`` no longer creates the table on a fresh install; an
EXISTING database still carries it, holding nothing but which nag milestones had
already fired. ``run_migrations`` drops it.

Two things are pinned. A pre-existing table (the upgrade shape) is actually dropped,
and a database that never had one (the fresh-install shape) survives the same pass —
``DROP TABLE IF EXISTS`` is the only form that is safe on both, and every boot re-runs
the whole migration list.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core.database import run_migrations

_DROPPED_TABLE = "sponsor_toast_state"


@pytest.fixture(autouse=True)
def force_sqlite_dialect(monkeypatch):
    """Force the SQLite branch regardless of test env settings."""
    from backend.app.core import db_dialect

    monkeypatch.setattr(db_dialect, "is_sqlite", lambda: True)
    monkeypatch.setattr(db_dialect, "is_postgres", lambda: False)
    from backend.app.core import database as database_module

    monkeypatch.setattr(database_module, "is_sqlite", lambda: True)


def _register_all_models():
    """Import the whole model package so ``create_all`` builds EVERY table.

    ``run_migrations`` is one list run top to bottom and an ``ALTER TABLE`` naming a
    table a partial import never created raises ``no such table``, which
    ``_safe_execute`` does NOT swallow. Same set the real boot registers.
    """
    import backend.app.models  # noqa: F401
    from backend.app.models import (  # noqa: F401
        active_print_spoolman,
        bug_report,
        external_link,
        filament_sku_settings,
        print_log,
        print_queue,
        project_bom,
        shopping_list,
        slot_preset,
        spoolman_k_profile,
        spoolman_slot_assignment,
        virtual_printer,
    )


async def _make_engine(*, with_legacy_table: bool):
    from backend.app.core.database import Base

    _register_all_models()

    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if with_legacy_table:
            # Simulate the pre-removal schema: the model is deleted, so create_all
            # cannot make this table any more — the upgrade shape has to be built by
            # hand, rows and all.
            await conn.execute(
                text(
                    f"CREATE TABLE {_DROPPED_TABLE} ("
                    "id INTEGER PRIMARY KEY, "
                    "user_id INTEGER REFERENCES users(id) ON DELETE CASCADE, "
                    "last_shown_at DATETIME, "
                    "milestones_seen TEXT NOT NULL DEFAULT '[]', "
                    "last_seen_version VARCHAR(50))"
                )
            )
            await conn.execute(
                text(f"INSERT INTO {_DROPPED_TABLE} (user_id, milestones_seen) VALUES (1, '[\"prints-100\"]')")
            )
    return eng


async def _tables(conn) -> set[str]:
    rows = (await conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))).fetchall()
    return {row[0] for row in rows}


@pytest.mark.asyncio
async def test_fresh_install_never_creates_the_table():
    """The model is gone, so ``create_all`` must not produce it in the first place."""
    engine = await _make_engine(with_legacy_table=False)
    try:
        async with engine.connect() as conn:
            assert _DROPPED_TABLE not in await _tables(conn)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_drops_a_pre_existing_table():
    engine = await _make_engine(with_legacy_table=True)
    try:
        async with engine.connect() as conn:
            assert _DROPPED_TABLE in await _tables(conn)
        async with engine.begin() as conn:
            await run_migrations(conn)
        async with engine.connect() as conn:
            assert _DROPPED_TABLE not in await _tables(conn)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_is_idempotent_without_the_table():
    """Every boot re-runs the list: the second pass, and a fresh install's first, no-op."""
    engine = await _make_engine(with_legacy_table=True)
    try:
        async with engine.begin() as conn:
            await run_migrations(conn)
        async with engine.begin() as conn:
            await run_migrations(conn)
        async with engine.connect() as conn:
            assert _DROPPED_TABLE not in await _tables(conn)
    finally:
        await engine.dispose()
