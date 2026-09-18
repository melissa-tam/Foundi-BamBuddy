"""The VP mode wire-value rename migration (#1429 follow-up).

The UI buttons "Archive" and "Queue" saved the wire values `immediate` and
`print_queue`, which name a different concept in every support bundle. The
migration rewrites stored rows to the canonical names — both the per-VP
column and the legacy single-VP setting row.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from backend.app.core.database import run_migrations
from backend.tests._fixtures.db import create_memory_engine

pytestmark = pytest.mark.usefixtures("force_sqlite_dialect")


@pytest.fixture
async def engine():
    eng = await create_memory_engine()
    yield eng
    await eng.dispose()


@pytest.mark.asyncio
async def test_legacy_mode_rows_get_canonical_names(engine):
    """Existing rows with `immediate` / `print_queue` get rewritten to
    `archive` / `queue` while canonical values and unrelated modes pass
    through untouched."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO virtual_printers (id, name, enabled, mode, serial_suffix, position) VALUES "
                "(1, 'A', 0, 'immediate', '391800001', 1),"
                "(2, 'B', 0, 'print_queue', '391800002', 2),"
                "(3, 'C', 0, 'review', '391800003', 3),"
                "(4, 'D', 0, 'proxy', '391800004', 4),"
                "(5, 'E', 0, 'archive', '391800005', 5),"
                "(6, 'F', 0, 'queue', '391800006', 6)"
            )
        )

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT id, mode FROM virtual_printers ORDER BY id"))
        rows = dict(result.fetchall())

    assert rows[1] == "archive"  # immediate → archive
    assert rows[2] == "queue"  # print_queue → queue
    assert rows[3] == "review"  # untouched
    assert rows[4] == "proxy"  # untouched
    assert rows[5] == "archive"  # already canonical
    assert rows[6] == "queue"  # already canonical


@pytest.mark.asyncio
async def test_legacy_settings_row_gets_canonical_name(engine):
    """The legacy single-VP `virtual_printer_mode` setting also gets renamed
    so the GET response (which feeds the support bundle and the settings
    page) reads the canonical name."""
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO settings (key, value) VALUES ('virtual_printer_mode', 'immediate')"))

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT value FROM settings WHERE key = 'virtual_printer_mode'"))
        value = result.scalar()

    assert value == "archive"


@pytest.mark.asyncio
async def test_migration_is_idempotent(engine):
    """Running the migration twice must be a no-op on canonical values —
    every boot re-runs the migration set."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO virtual_printers (id, name, enabled, mode, serial_suffix, position) "
                "VALUES (1, 'A', 0, 'immediate', '391800001', 1)"
            )
        )

    async with engine.begin() as conn:
        await run_migrations(conn)
    # Second run on already-canonical values.
    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT mode FROM virtual_printers WHERE id = 1"))
        assert result.scalar() == "archive"
