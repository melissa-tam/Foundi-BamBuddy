"""Regression test for the ``notification_providers.on_backup_group_split`` migration.

010-H2S (2026-08-21, incident shape 33) added a ``backup_group_split`` event: a dispatch
picked a tray the firmware can pair with NOTHING while a near-identical tray sat one slot
over, so AMS Filament Backup never rescued the runout. Its provider toggle is a boolean
column added by ``run_migrations`` via ``_safe_execute`` (ADD COLUMN, default TRUE), and
its template is seeded from ``DEFAULT_TEMPLATES``.

``create_all`` would create the column from the current model and mask the migration, so
the fixture drops it first to simulate a pre-migration schema; the test then proves
``run_migrations`` re-adds it and that a second pass is a no-op. Idempotent and
SQLite-safe (mirrors ``test_cooldown_escalation_column_migration``).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core.database import run_migrations

_NEW_COLUMN = "on_backup_group_split"


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

    Deliberately not the hand-picked five the older column-migration tests import:
    ``run_migrations`` is one list run top to bottom, and an ``ALTER TABLE`` naming a
    table those five did not create raises ``no such table``, which ``_safe_execute``
    does NOT swallow (it swallows only already-exists / duplicate-column shapes). The
    partial import therefore makes the migration pass fail for reasons that have
    nothing to do with the column under test.

    The package ``__init__`` alone is not enough either: a dozen model modules are
    registered by ``core.database.init_db``'s own import list and not re-exported from
    the package, so both lists are imported here — the same set the real boot registers
    before it runs the migrations.
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


def test_template_is_registered_for_seeding():
    """The toggle without a template would page nobody: the seeder only INSERTs event
    types it finds in ``DEFAULT_TEMPLATES``, and every placeholder the copy uses must be
    one ``notification_service.on_backup_group_split`` actually supplies."""
    from backend.app.models.notification_template import DEFAULT_TEMPLATES

    row = next(t for t in DEFAULT_TEMPLATES if t["event_type"] == "backup_group_split")
    assert row["name"] == "AMS Backup Group Split"
    supplied = {"printer_name", "slot", "partner_slot", "dimension", "picked_value", "partner_value"}
    body = row["body_template"].format(**{k: k for k in supplied})
    title = row["title_template"].format(**{k: k for k in supplied})
    assert "{" not in body and "{" not in title
