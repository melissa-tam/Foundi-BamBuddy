"""Regression test for the W4 tagless-default identity migration.

The shipped ``tagless_default_filament`` default gained a specific Bambu PETG HF
slicer id (GFG02) + a 230/270 nozzle range so every bare-tray config push emits a
byte-identical firmware backup-group peer. The migration in ``run_migrations``
rewrites a stored row that still holds the UNEDITED old default (Bambu Lab / PETG /
HF / 000000FF / no slicer_filament) to the new JSON, via a SEMANTIC field compare
so frontend JSON.stringify key order and an omitted slicer_filament both match. An
operator-customised row is untouched, an absent row is a no-op, and re-running is
idempotent (the rewritten row no longer matches the old-default predicate).
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core.database import run_migrations
from backend.app.schemas.settings import _DEFAULT_TAGLESS_FILAMENT_JSON


@pytest.fixture(autouse=True)
def force_sqlite_dialect(monkeypatch):
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
        external_link,
        filament,
        group,
        kprofile_note,
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


async def _get_tagless_default(engine) -> str | None:
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT value FROM settings WHERE key = 'tagless_default_filament'"))
        return result.scalar()


# The exact unedited old default (pre-W4): the pydantic key order the frontend GET
# returned before the schema gained the temp fields.
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
