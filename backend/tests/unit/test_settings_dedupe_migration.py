"""Regression test for the settings table dedupe + unique-index migration.

Legacy SQLite installs created the `settings` table without a UNIQUE constraint
on `key`. With nothing to conflict on, the seed loop's `INSERT OR IGNORE` is a
plain INSERT: every restart duplicates a row, and any `scalar_one_or_none()` on
`SELECT settings WHERE key = :k` (e.g. `is_advanced_auth_enabled`) then raises
`MultipleResultsFound`.

`run_migrations` deletes dup rows (keeping MIN(id) per key) and creates the
missing unique index before the seed loop, on both fresh and legacy schemas.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from backend.app.core.database import run_migrations
from backend.tests._fixtures.db import create_memory_engine

pytestmark = pytest.mark.usefixtures("force_sqlite_dialect")


@pytest.fixture
async def legacy_engine():
    """A pre-UNIQUE install.

    `create_all` emits the unique index, which would mask the migration, so the
    settings table is dropped and re-created in its legacy shape (no UNIQUE on
    key) — real upgrades look exactly like this, modern everywhere else.
    """
    engine = await create_memory_engine()
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE settings"))
        await conn.execute(
            text("""
            CREATE TABLE settings (
                id INTEGER PRIMARY KEY,
                key TEXT,
                value TEXT,
                created_at TEXT,
                updated_at TEXT
            )
            """)
        )
    yield engine
    await engine.dispose()


@pytest.fixture
async def fresh_engine():
    """A fresh install: created from the models, so settings.key already carries
    the unique index the migration would otherwise add."""
    engine = await create_memory_engine()
    yield engine
    await engine.dispose()


async def test_legacy_schema_allows_duplicate_keys_before_migration(legacy_engine):
    """Sanity check: the legacy schema really does permit duplicates — protects
    the migration test below from becoming a false-positive if the fixture drifts."""
    async with legacy_engine.begin() as conn:
        await conn.execute(text("INSERT INTO settings (key, value) VALUES ('advanced_auth_enabled', 'false')"))
        await conn.execute(text("INSERT INTO settings (key, value) VALUES ('advanced_auth_enabled', 'false')"))
        result = await conn.execute(text("SELECT COUNT(*) FROM settings WHERE key = 'advanced_auth_enabled'"))
        assert result.scalar_one() == 2


async def test_migration_dedupes_and_adds_unique_index(legacy_engine):
    """Given a legacy DB with duplicate rows for the same key, run_migrations
    should (a) delete duplicates keeping the lowest id, (b) add the unique index,
    (c) make future duplicate inserts fail with IntegrityError."""
    # Seed: two duplicate rows for the same key, with distinguishable values.
    async with legacy_engine.begin() as conn:
        await conn.execute(text("INSERT INTO settings (id, key, value) VALUES (1, 'advanced_auth_enabled', 'old')"))
        await conn.execute(text("INSERT INTO settings (id, key, value) VALUES (2, 'advanced_auth_enabled', 'new')"))
        # Also seed an unrelated key that should survive untouched.
        await conn.execute(text("INSERT INTO settings (id, key, value) VALUES (3, 'other_key', 'keep_me')"))

    async with legacy_engine.begin() as conn:
        await run_migrations(conn)

    async with legacy_engine.begin() as conn:
        # Only the MIN(id) row for the duplicated key remains.
        rows = (await conn.execute(text("SELECT id, value FROM settings WHERE key = 'advanced_auth_enabled'"))).all()
        assert len(rows) == 1
        assert rows[0].id == 1
        assert rows[0].value == "old"

        # Untouched key still present.
        other = (await conn.execute(text("SELECT value FROM settings WHERE key = 'other_key'"))).scalar_one()
        assert other == "keep_me"

        # Unique constraint is now enforced — inserting a duplicate fails.
        with pytest.raises(IntegrityError):
            await conn.execute(text("INSERT INTO settings (key, value) VALUES ('advanced_auth_enabled', 'x')"))


async def test_migration_is_idempotent_on_already_clean_legacy(legacy_engine):
    """Running the migration twice must not crash — the second run finds no
    duplicates and the CREATE UNIQUE INDEX IF NOT EXISTS is a no-op."""
    async with legacy_engine.begin() as conn:
        await conn.execute(text("INSERT INTO settings (key, value) VALUES ('k', 'v')"))

    async with legacy_engine.begin() as conn:
        await run_migrations(conn)
    async with legacy_engine.begin() as conn:
        await run_migrations(conn)

    async with legacy_engine.begin() as conn:
        count = (await conn.execute(text("SELECT COUNT(*) FROM settings WHERE key = 'k'"))).scalar_one()
        assert count == 1


async def test_migration_is_noop_on_fresh_install(fresh_engine):
    """Fresh installs get the unique index from `create_all`. Running the
    migration must not crash and must not alter the schema."""
    async with fresh_engine.begin() as conn:
        await run_migrations(conn)

    async with fresh_engine.begin() as conn:
        # Unique constraint still present — duplicate insert fails.
        await conn.execute(text("INSERT INTO settings (key, value) VALUES ('k', 'v1')"))
        with pytest.raises(IntegrityError):
            await conn.execute(text("INSERT INTO settings (key, value) VALUES ('k', 'v2')"))
