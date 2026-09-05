"""Regression test for the ``notification_providers.on_power_loss_recovery`` migration.

The 2026-09-04 fleet outage added ONE provider column carrying the whole pause-recovery
lane: the per-outage fleet summary and the per-printer HOLD pages (a resume the firmware
refused or failed, and a printer that rebooted with a part on its plate). It is a boolean
added by ``run_migrations`` via ``_safe_execute`` (ADD COLUMN, default TRUE — every
message on it asks a human for something, unlike the success-class toggles), and its
template is seeded from ``DEFAULT_TEMPLATES``.

``create_all`` would create the column from the current model and mask the migration, so
the fixture drops it first to simulate a pre-migration schema; the test then proves
``run_migrations`` re-adds it and that a second pass is a no-op. Idempotent and
SQLite-safe (mirrors ``test_backup_group_split_column_migration``).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core.database import run_migrations

_NEW_COLUMN = "on_power_loss_recovery"


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

    ``run_migrations`` is one list run top to bottom, and an ``ALTER TABLE`` naming a
    table a partial import never created raises ``no such table`` — which
    ``_safe_execute`` does NOT swallow (only already-exists / duplicate-column shapes).
    Both import lists are needed: a dozen model modules are registered by
    ``core.database.init_db``'s own list and not re-exported from the package.
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
    """A row inserted without the toggle gets DEFAULT 1.

    Deliberately opposite to the success-class toggles beside it: those default OFF
    because a recovery that SUCCEEDED asked nothing of a human. Everything on this
    column is a request for a human, and a power-loss resume that WORKS is a log line
    that never reaches it."""
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
    one ``notification_service.on_power_loss_hold`` actually supplies."""
    from backend.app.models.notification_template import DEFAULT_TEMPLATES

    row = next(t for t in DEFAULT_TEMPLATES if t["event_type"] == "power_loss_recovery")
    assert row["name"] == "Power-Loss Recovery"
    supplied = {"printer_name", "job_name", "reason", "outage"}
    body = row["body_template"].format(**{k: k for k in supplied})
    title = row["title_template"].format(**{k: k for k in supplied})
    assert "{" not in body and "{" not in title
