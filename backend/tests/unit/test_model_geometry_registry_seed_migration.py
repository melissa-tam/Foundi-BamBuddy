"""Regression test for the printer_model_geometry fleet-seed migration.

``run_migrations`` seeds five additional (published-spec, ``validated=FALSE``)
geometry rows alongside the existing H2S/H2C seeds — P1S, P2S, H2D, X2D and the
A2L bed-slinger — via the same idempotent ``INSERT .. SELECT .. WHERE NOT EXISTS``
shape. This test pins:

* exactly SEVEN rows after the migration, with no duplicates on a re-run
  (idempotency);
* the A2L row's ``z_travel_mm`` is a literal NULL (bed-slinger — the bed-drop
  assist must fail closed independently of the bedslinger guard);
* the X2D dual-mode X envelope is the per-side intersection 20.5–235.5;
* all five new rows are ``validated=FALSE`` (MEASURE AT LADDER);
* an operator-edited row survives a re-run (WHERE NOT EXISTS never clobbers it);
* the FRESH-DB path (no table) also ends with seven seeded rows.

Idempotent and SQLite-safe, mirroring ``test_model_geometry_z_travel_migration``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core.database import run_migrations

_EXPECTED_KEYS = {"H2S", "H2C", "P1S", "P2S", "H2D", "X2D", "A2L"}


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
        ams_history,
        ams_label,
        api_key,
        archive,
        color_catalog,
        eject_profile,
        external_link,
        filament,
        group,
        kprofile_note,
        library,
        maintenance,
        notification,
        notification_template,
        print_log,
        print_queue,
        printer,
        printer_model_geometry,
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


async def _build_all_tables(conn):
    """create_all every registered table so run_migrations' other ALTERs have a
    table to act on (a missing-table error is NOT swallowed by _safe_execute)."""
    from backend.app.core.database import Base

    _register_all_models()
    await conn.run_sync(Base.metadata.create_all)


async def _keys(conn) -> list[str]:
    rows = (await conn.execute(text("SELECT model_key FROM printer_model_geometry"))).fetchall()
    return [r[0] for r in rows]


async def _row(conn, model_key: str):
    row = (
        await conn.execute(
            text(
                "SELECT env_x_min, env_x_max, z_travel_mm, validated FROM printer_model_geometry WHERE model_key = :k"
            ),
            {"k": model_key},
        )
    ).fetchone()
    return row


@pytest.fixture
async def existing_engine():
    """A DB with the current-model geometry table present but unseeded."""
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await _build_all_tables(conn)
    yield eng
    await eng.dispose()


@pytest.fixture
async def fresh_engine():
    """A DB with NO geometry table — run_migrations must CREATE + seed it."""
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await _build_all_tables(conn)
        await conn.execute(text("DROP TABLE printer_model_geometry"))
    yield eng
    await eng.dispose()


@pytest.mark.asyncio
async def test_seeds_exactly_seven_rows_no_dupes(existing_engine):
    async with existing_engine.begin() as conn:
        await run_migrations(conn)
    async with existing_engine.begin() as conn:
        await run_migrations(conn)  # second boot — must not duplicate
    async with existing_engine.connect() as conn:
        keys = await _keys(conn)
        assert len(keys) == 7, keys
        assert set(keys) == _EXPECTED_KEYS
        assert len(set(keys)) == len(keys), f"duplicate rows: {keys}"


@pytest.mark.asyncio
async def test_a2l_z_travel_is_null_and_unvalidated(existing_engine):
    async with existing_engine.begin() as conn:
        await run_migrations(conn)
    async with existing_engine.connect() as conn:
        env_x_min, env_x_max, z_travel, validated = await _row(conn, "A2L")
        assert z_travel is None  # bed-slinger — bed-drop fails closed
        assert not validated
        assert (env_x_min, env_x_max) == (0.0, 330.0)


@pytest.mark.asyncio
async def test_x2d_dual_mode_envelope_intersection(existing_engine):
    async with existing_engine.begin() as conn:
        await run_migrations(conn)
    async with existing_engine.connect() as conn:
        env_x_min, env_x_max, z_travel, validated = await _row(conn, "X2D")
        assert (env_x_min, env_x_max) == (20.5, 235.5)
        assert z_travel == 256.0
        assert not validated


@pytest.mark.asyncio
async def test_all_five_new_rows_are_unvalidated(existing_engine):
    async with existing_engine.begin() as conn:
        await run_migrations(conn)
    async with existing_engine.connect() as conn:
        for key in ("P1S", "P2S", "H2D", "X2D", "A2L"):
            _lo, _hi, _z, validated = await _row(conn, key)
            assert not validated, f"{key} should seed validated=False"


@pytest.mark.asyncio
async def test_operator_edit_survives_rerun(existing_engine):
    """An operator PUT that flips P1S to validated + a measured envelope must not be
    clobbered by the WHERE-NOT-EXISTS seed on the next boot."""
    async with existing_engine.begin() as conn:
        await run_migrations(conn)
    async with existing_engine.begin() as conn:
        await conn.execute(
            text("UPDATE printer_model_geometry SET validated = 1, env_x_min = 5 WHERE model_key = 'P1S'")
        )
    async with existing_engine.begin() as conn:
        await run_migrations(conn)  # second boot
    async with existing_engine.connect() as conn:
        env_x_min, _hi, _z, validated = await _row(conn, "P1S")
        assert validated  # operator value preserved
        assert env_x_min == 5.0
        assert len(await _keys(conn)) == 7  # still no duplicate


@pytest.mark.asyncio
async def test_fresh_db_creates_and_seeds_seven(fresh_engine):
    async with fresh_engine.begin() as conn:
        await run_migrations(conn)
    async with fresh_engine.connect() as conn:
        keys = await _keys(conn)
        assert set(keys) == _EXPECTED_KEYS
        assert len(keys) == 7
        # NULL z_travel carried through the fresh CREATE + seed path too.
        _lo, _hi, z_travel, _v = await _row(conn, "A2L")
        assert z_travel is None
