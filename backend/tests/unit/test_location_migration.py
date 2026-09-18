"""Regression tests for storage-location migration backfill (#1004).

Legacy installs may have free-text storage_location values that differ only
by case. The backfill must collapse them to one catalog row and stay
idempotent across restarts.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from backend.app.core.database import run_migrations
from backend.tests._fixtures.db import create_memory_engine

pytestmark = pytest.mark.usefixtures("force_sqlite_dialect")


@pytest.fixture
async def engine_with_case_variant_spools():
    engine = await create_memory_engine()
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM locations"))
        await conn.execute(
            text(
                """
                INSERT INTO spool (
                    material, storage_location, label_weight, core_weight,
                    weight_used, weight_used_baseline, weight_locked
                )
                VALUES ('PLA', 'Drybox 1', 1000, 250, 0, 0, 0),
                       ('PETG', 'DRYBOX 1', 1000, 250, 0, 0, 0)
                """
            )
        )
    yield engine
    await engine.dispose()


async def test_backfill_collapses_case_variant_storage_locations(engine_with_case_variant_spools):
    async with engine_with_case_variant_spools.begin() as conn:
        await run_migrations(conn)

    async with engine_with_case_variant_spools.connect() as conn:
        loc_rows = (await conn.execute(text("SELECT id, name, name_key FROM locations ORDER BY id"))).all()
        spool_rows = (await conn.execute(text("SELECT id, storage_location, location_id FROM spool ORDER BY id"))).all()

    assert len(loc_rows) == 1
    assert loc_rows[0].name_key == "drybox 1"
    location_id = loc_rows[0].id
    assert all(row.location_id == location_id for row in spool_rows)


async def test_backfill_is_idempotent_with_existing_locations(engine_with_case_variant_spools):
    async with engine_with_case_variant_spools.begin() as conn:
        await run_migrations(conn)
    async with engine_with_case_variant_spools.begin() as conn:
        await run_migrations(conn)

    async with engine_with_case_variant_spools.connect() as conn:
        loc_count = (await conn.execute(text("SELECT COUNT(*) FROM locations"))).scalar_one()
        linked = (await conn.execute(text("SELECT COUNT(*) FROM spool WHERE location_id IS NOT NULL"))).scalar_one()

    assert loc_count == 1
    assert linked == 2


@pytest.fixture
async def engine_with_null_storage_location():
    """A spool with NULL storage_location must NOT produce a phantom location row
    or get linked to anything — it stays NULL on both fields."""
    engine = await create_memory_engine()
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM locations"))
        await conn.execute(
            text(
                """
                INSERT INTO spool (
                    material, storage_location, label_weight, core_weight,
                    weight_used, weight_used_baseline, weight_locked
                )
                VALUES ('PLA', NULL, 1000, 250, 0, 0, 0),
                       ('PETG', '   ', 1000, 250, 0, 0, 0),
                       ('TPU', 'Real Shelf', 1000, 250, 0, 0, 0)
                """
            )
        )
    yield engine
    await engine.dispose()


async def test_backfill_skips_null_and_whitespace_storage_location(
    engine_with_null_storage_location,
):
    """NULL / whitespace-only `storage_location` rows must NOT create catalog
    rows; only the 'Real Shelf' value gets a location row + spool link."""
    async with engine_with_null_storage_location.begin() as conn:
        await run_migrations(conn)

    async with engine_with_null_storage_location.connect() as conn:
        loc_rows = (await conn.execute(text("SELECT name FROM locations"))).all()
        unlinked = (
            await conn.execute(text("SELECT material FROM spool WHERE location_id IS NULL ORDER BY material"))
        ).all()

    # Only the row with a real storage_location should be in the catalog.
    assert [r.name for r in loc_rows] == ["Real Shelf"]
    # The NULL and whitespace-only spools stay unlinked (no phantom row).
    assert [r.material for r in unlinked] == ["PETG", "PLA"]


@pytest.fixture
async def engine_with_legacy_null_name_key_location():
    """A `locations` row inserted BEFORE the name_key column existed.

    The migration must backfill the legacy row's name_key BEFORE the dedup
    INSERT, so the spool-link UPDATE can join on the new key.
    """
    engine = await create_memory_engine()
    async with engine.begin() as conn:
        # The model-shaped table has NOT NULL on name_key, which would mask the
        # backfill; the pre-migration shape has no name_key column at all. The
        # migration's idempotent ALTER TABLE adds it without a NOT NULL
        # constraint, so the legacy row can legally hold NULL until the
        # backfill UPDATE runs.
        await conn.execute(text("DROP TABLE locations"))
        await conn.execute(
            text(
                """
                CREATE TABLE locations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name VARCHAR(255) NOT NULL UNIQUE,
                    identifier VARCHAR(100),
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        await conn.execute(text("INSERT INTO locations (name) VALUES ('Drybox 1')"))
        await conn.execute(
            text(
                """
                INSERT INTO spool (
                    material, storage_location, label_weight, core_weight,
                    weight_used, weight_used_baseline, weight_locked
                )
                VALUES ('PLA', 'Drybox 1', 1000, 250, 0, 0, 0)
                """
            )
        )
    yield engine
    await engine.dispose()


async def test_backfill_links_spool_to_legacy_null_name_key_location(
    engine_with_legacy_null_name_key_location,
):
    async with engine_with_legacy_null_name_key_location.begin() as conn:
        await run_migrations(conn)

    async with engine_with_legacy_null_name_key_location.connect() as conn:
        loc_rows = (await conn.execute(text("SELECT id, name, name_key FROM locations"))).all()
        spool_rows = (await conn.execute(text("SELECT location_id FROM spool"))).all()

    # Exactly one location row (the pre-existing legacy one); its name_key
    # got backfilled by the FIRST step of the migration.
    assert len(loc_rows) == 1
    assert loc_rows[0].name_key == "drybox 1"
    assert spool_rows[0].location_id == loc_rows[0].id
