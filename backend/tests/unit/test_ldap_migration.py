"""Regression test for #794 — LDAP auto-provisioning on legacy SQLite schemas.

Pre-LDAP databases created the `users` table with `password_hash VARCHAR(255) NOT NULL`.
The LDAP provisioning path inserts users with `password_hash=None`, which crashes on
upgrade until the migration strips the NOT NULL constraint.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core.database import run_migrations
from backend.tests._fixtures.db import create_memory_engine

pytestmark = pytest.mark.usefixtures("force_sqlite_dialect")


@pytest.fixture
async def legacy_engine():
    """An install whose `users` table still carries the pre-LDAP NOT NULL constraint.

    `create_all` builds the current (nullable) schema, which would mask the migration
    entirely, so `users` is dropped and re-created in its legacy shape. That is the real
    upgrade path: everything else in the DB looks modern, only this one table is stale.
    """
    engine = await create_memory_engine()
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS user_groups"))
        await conn.execute(text("DROP TABLE users"))
        await conn.execute(
            text("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                username VARCHAR(100) NOT NULL UNIQUE,
                password_hash VARCHAR(255) NOT NULL,
                role VARCHAR(20) NOT NULL DEFAULT 'user',
                is_active BOOLEAN NOT NULL DEFAULT 1,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """)
        )
    yield engine
    await engine.dispose()


async def test_legacy_schema_rejects_null_password_before_migration(legacy_engine):
    """Sanity check: without the migration, inserting a NULL password_hash fails.

    Guards against a false-positive where a future schema change silently allows NULL
    and the real migration test below becomes meaningless.
    """
    async with legacy_engine.begin() as conn:
        with pytest.raises(IntegrityError):
            await conn.execute(
                text(
                    "INSERT INTO users (username, password_hash, role, is_active) "
                    "VALUES ('ldap_alice', NULL, 'user', 1)"
                )
            )


async def test_migration_allows_null_password_hash_for_ldap_users(legacy_engine):
    """A migrated legacy DB accepts the LDAP provisioning insert (password_hash NULL)."""
    async with legacy_engine.begin() as conn:
        await run_migrations(conn)

    session_maker = async_sessionmaker(legacy_engine, class_=AsyncSession, expire_on_commit=False)
    async with session_maker() as session:
        await session.execute(
            text(
                "INSERT INTO users (username, email, password_hash, role, auth_source, is_active) "
                "VALUES (:u, :e, NULL, 'user', 'ldap', 1)"
            ),
            {"u": "ldap_bob", "e": "bob@example.com"},
        )
        await session.commit()

        result = await session.execute(
            text("SELECT username, password_hash, auth_source FROM users WHERE username = 'ldap_bob'")
        )
        row = result.one()
        assert row.username == "ldap_bob"
        assert row.password_hash is None
        assert row.auth_source == "ldap"


async def test_migration_is_idempotent(legacy_engine):
    """Running migrations twice must not break the writable_schema patch."""
    async with legacy_engine.begin() as conn:
        await run_migrations(conn)
    async with legacy_engine.begin() as conn:
        await run_migrations(conn)

    session_maker = async_sessionmaker(legacy_engine, class_=AsyncSession, expire_on_commit=False)
    async with session_maker() as session:
        await session.execute(
            text(
                "INSERT INTO users (username, password_hash, role, auth_source, is_active) "
                "VALUES ('ldap_carol', NULL, 'user', 'ldap', 1)"
            )
        )
        await session.commit()
