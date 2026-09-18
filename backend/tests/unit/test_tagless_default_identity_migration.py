"""The tagless-default identity migration.

The shipped ``tagless_default_filament`` default gained a specific Bambu PETG HF
slicer id (GFG02) + a 230/270 nozzle range so every bare-tray config push emits a
byte-identical firmware backup-group peer. The migration rewrites a stored row that
still holds the UNEDITED old default (Bambu Lab / PETG / HF / 000000FF / no
slicer_filament) to the new JSON, via a SEMANTIC field compare so frontend
JSON.stringify key order and an omitted slicer_filament both match. Re-running is
idempotent because the rewritten row no longer matches the old-default predicate.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from backend.app.core.database import run_migrations
from backend.app.schemas.settings import _DEFAULT_TAGLESS_FILAMENT_JSON
from backend.tests._fixtures.db import create_memory_engine

pytestmark = pytest.mark.usefixtures("force_sqlite_dialect")


@pytest.fixture
async def engine():
    eng = await create_memory_engine()
    yield eng
    await eng.dispose()


async def _get_tagless_default(engine) -> str | None:
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT value FROM settings WHERE key = 'tagless_default_filament'"))
        return result.scalar()


# The exact unedited old default: the pydantic key order the frontend GET returned.
_OLD_DEFAULT_PYDANTIC = json.dumps(
    {"brand": "Bambu Lab", "material": "PETG", "subtype": "HF", "rgba": "000000FF", "slicer_filament": None}
)
# A frontend JSON.stringify with a different key order AND no slicer_filament key.
_OLD_DEFAULT_SHUFFLED = json.dumps({"material": "PETG", "rgba": "000000FF", "brand": "Bambu Lab", "subtype": "HF"})


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [_OLD_DEFAULT_PYDANTIC, _OLD_DEFAULT_SHUFFLED])
async def test_old_default_rewritten_regardless_of_key_order(engine, stored):
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO settings (key, value) VALUES ('tagless_default_filament', :v)"), {"v": stored}
        )

    async with engine.begin() as conn:
        await run_migrations(conn)

    value = await _get_tagless_default(engine)
    assert value == _DEFAULT_TAGLESS_FILAMENT_JSON
    parsed = json.loads(value)
    assert parsed["slicer_filament"] == "GFG02"
    assert parsed["nozzle_temp_min"] == 230 and parsed["nozzle_temp_max"] == 270


@pytest.mark.asyncio
async def test_operator_edited_row_untouched(engine):
    edited = json.dumps({"brand": "Polymaker", "material": "PETG", "subtype": "HF", "rgba": "112233FF"})
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO settings (key, value) VALUES ('tagless_default_filament', :v)"), {"v": edited}
        )

    async with engine.begin() as conn:
        await run_migrations(conn)

    assert await _get_tagless_default(engine) == edited  # operator's brand survives


@pytest.mark.asyncio
async def test_absent_row_is_noop(engine):
    async with engine.begin() as conn:
        await run_migrations(conn)
    assert await _get_tagless_default(engine) is None  # no row materialised


@pytest.mark.asyncio
async def test_migration_is_idempotent(engine):
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO settings (key, value) VALUES ('tagless_default_filament', :v)"),
            {"v": _OLD_DEFAULT_PYDANTIC},
        )

    async with engine.begin() as conn:
        await run_migrations(conn)
    first = await _get_tagless_default(engine)
    # Second boot re-runs the whole migration set on the already-rewritten row.
    async with engine.begin() as conn:
        await run_migrations(conn)
    assert await _get_tagless_default(engine) == first == _DEFAULT_TAGLESS_FILAMENT_JSON
