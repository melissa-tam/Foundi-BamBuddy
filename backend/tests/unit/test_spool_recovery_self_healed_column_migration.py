"""Regression test for the ``notification_providers.on_spool_recovery_self_healed`` migration.

The truth-ordered out-of-rotation remediation introduced a dedicated
``spool_recovery_self_healed`` event (truthful "cleared on the same spool" copy,
distinct from the swap-framed ``spool_recovery_succeeded``). Its provider toggle is
a boolean column added by ``run_migrations`` via ``_safe_execute`` (ADD COLUMN,
default TRUE). ``create_all`` would create the column from the current model and
mask the migration, so the fixture drops it first to simulate a pre-migration
schema; the test then proves ``run_migrations`` re-adds it and that a second pass is
a no-op. Idempotent and SQLite-safe (mirrors the sibling migration regression tests).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core.database import run_migrations

_NEW_COLUMN = "on_spool_recovery_self_healed"


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
        api_key,
        notification,
        notification_template,
        printer,
        user,
    )


@pytest.fixture
async def engine():
    from backend.app.core.database import Base

    _register_all_models()

    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Simulate a pre-migration schema: drop the column the current model created
        # so run_migrations actually has to re-add it (SQLite 3.35+ DROP COLUMN).
        await conn.execute(text(f"ALTER TABLE notification_providers DROP COLUMN {_NEW_COLUMN}"))
    yield eng
    await eng.dispose()


async def _columns(conn) -> set[str]:
    rows = (await conn.execute(text("PRAGMA table_info(notification_providers)"))).fetchall()
    return {row[1] for row in rows}


@pytest.mark.asyncio
async def test_pre_migration_table_lacks_column(engine):
    """Sanity check: the fixture's simulated old schema is missing the column."""
    async with engine.connect() as conn:
        assert _NEW_COLUMN not in await _columns(conn)


@pytest.mark.asyncio
async def test_migration_adds_column(engine):
    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.connect() as conn:
        assert _NEW_COLUMN in await _columns(conn)


@pytest.mark.asyncio
async def test_migration_default_is_true(engine):
    """A row inserted without the toggle gets the DEFAULT 1 (subscribed by default)."""
    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO notification_providers (name, provider_type, config) VALUES ('migrated', 'webhook', '{}')"
            )
        )
    async with engine.connect() as conn:
        row = (
            await conn.execute(text(f"SELECT {_NEW_COLUMN} FROM notification_providers WHERE name = 'migrated'"))
        ).fetchone()
    assert row[0] == 1


@pytest.mark.asyncio
async def test_migration_is_idempotent(engine):
    """Every boot re-runs the migration set; a second pass must be a no-op."""
    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.connect() as conn:
        assert _NEW_COLUMN in await _columns(conn)
