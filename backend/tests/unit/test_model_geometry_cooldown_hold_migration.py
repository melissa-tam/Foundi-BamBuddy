"""Regression test for the printer_model_geometry cooldown plate-hold columns.

Adds ``cooldown_hold_keepout_y_mm`` + ``cooldown_hold_clear_above_mm`` (both nullable,
no default) and seeds the H2S row with the operator's 2026-09-10 numbers — keep-out
Y 285 (the vendor's rear service area Y295 less 10 mm of margin) and the MEASURED clear
height of 100 mm above the nozzle plane with the toolhead parked at the chute.

This file is the ONLY place the clear height's VALUE is asserted, so it also covers the
one-time migration that lifts installs which already ran the first wave off its 51 mm
witnessed-safe placeholder — including the two things a value-keyed rewrite must never
do: run twice, or undo a later re-measure.

The properties that matter, and why each is pinned here:

* every OTHER model stays NULL — the clearance is a physical fact per machine and has
  been measured on H2S only, so every other model ships hold-OFF (fan only). A seed that
  fanned the H2S numbers across the registry would authorise a hold nobody witnessed;
* the pair is written TOGETHER — ``ModelGeometry`` refuses a one-sided pair outright, so
  a seed that set one column would fail the geometry read for that model;
* re-running is a no-op and an operator/DB-set value survives (the ``IS NULL`` guard on
  the seed, the settings marker on the migration), matching the z_travel backfill's own
  contract;
* both upgrade paths work: an EXISTING DB whose table predates the columns (the ALTERs
  add them, the seed fills H2S) and a FRESH DB with no table at all (``CREATE TABLE``
  builds it and the seed fills H2S).

Idempotent and SQLite-safe, mirroring the other column-migration regression tests.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core.database import _H2S_COOLDOWN_HOLD_CLEAR_ABOVE_MM, run_migrations

# The placeholder the first cooldown-hold wave shipped, and what the marker key is. Both
# spelled once here because the migration is keyed on the VALUE and the tests below have
# to be able to state that fact without re-typing it.
_PLACEHOLDER_CLEAR_MM = 51.0
_MIGRATION_MARKER = "migration_cooldown_hold_clear_h2s_20260910"

# The pre-cooldown-hold geometry table, recreated verbatim to simulate an old DB.
_OLD_SCHEMA_NO_COOLDOWN_HOLD = """
CREATE TABLE printer_model_geometry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_key VARCHAR(50) NOT NULL UNIQUE,
    bed_x FLOAT NOT NULL,
    bed_y FLOAT NOT NULL,
    env_x_min FLOAT NOT NULL,
    env_x_max FLOAT NOT NULL,
    env_y_min FLOAT NOT NULL,
    env_y_max FLOAT NOT NULL,
    max_part_height_mm FLOAT NOT NULL,
    z_travel_mm FLOAT,
    validated BOOLEAN NOT NULL DEFAULT 0,
    notes TEXT,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""

_SEED_H2S = (
    "INSERT INTO printer_model_geometry "
    "(model_key, bed_x, bed_y, env_x_min, env_x_max, env_y_min, env_y_max, max_part_height_mm, z_travel_mm, "
    "validated, notes) "
    "VALUES ('H2S', 340, 320, 0, 340, -16, 325, 42, 340, 1, 'existing')"
)
_SEED_H2C = (
    "INSERT INTO printer_model_geometry "
    "(model_key, bed_x, bed_y, env_x_min, env_x_max, env_y_min, env_y_max, max_part_height_mm, z_travel_mm, "
    "validated, notes) "
    "VALUES ('H2C', 330, 320, 15, 325, 0, 320, 42, 325, 1, 'existing')"
)


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
    table a partial import never created raises ``no such table`` — which ``_safe_execute``
    does NOT swallow. The package ``__init__`` alone is not enough either: a dozen model
    modules are registered by ``core.database.init_db``'s own import list and not
    re-exported from the package, so both lists are imported here.
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


async def _build_all_tables(conn):
    from backend.app.core.database import Base

    _register_all_models()
    await conn.run_sync(Base.metadata.create_all)


async def _columns(conn) -> set[str]:
    rows = (await conn.execute(text("PRAGMA table_info(printer_model_geometry)"))).fetchall()
    return {row[1] for row in rows}


async def _hold(conn, model_key: str) -> tuple | None:
    """``(keepout_y, clear_above)`` for one model, or None when the row is absent."""
    row = (
        await conn.execute(
            text(
                "SELECT cooldown_hold_keepout_y_mm, cooldown_hold_clear_above_mm "
                "FROM printer_model_geometry WHERE model_key = :k"
            ),
            {"k": model_key},
        )
    ).fetchone()
    return None if row is None else (row[0], row[1])


@pytest.fixture
async def existing_engine():
    """A DB whose geometry table predates the columns, pre-seeded with H2S + H2C."""
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await _build_all_tables(conn)
        await conn.execute(text("DROP TABLE printer_model_geometry"))
        await conn.execute(text(_OLD_SCHEMA_NO_COOLDOWN_HOLD))
        await conn.execute(text(_SEED_H2S))
        await conn.execute(text(_SEED_H2C))
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


@pytest.fixture
async def placeholder_engine():
    """The REAL upgrade shape: an install that already ran the first cooldown-hold wave.

    Its geometry table has both columns and H2S carries the 51 mm placeholder, which the
    seed's ``IS NULL`` guard will never touch — so without the one-time migration such an
    install would go on holding every plate 49 mm lower than the machine allows.
    """
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await _build_all_tables(conn)
        await conn.execute(text(_SEED_H2S))
        await conn.execute(text(_SEED_H2C))
        await conn.execute(
            text(
                "UPDATE printer_model_geometry "
                "SET cooldown_hold_keepout_y_mm = 285.0, cooldown_hold_clear_above_mm = :v "
                "WHERE model_key = 'H2S'"
            ),
            {"v": _PLACEHOLDER_CLEAR_MM},
        )
    yield eng
    await eng.dispose()


async def _marker_rows(conn) -> int:
    return (await conn.execute(text("SELECT COUNT(*) FROM settings WHERE key = :k"), {"k": _MIGRATION_MARKER})).scalar()


@pytest.mark.asyncio
async def test_pre_migration_table_lacks_the_columns(existing_engine):
    async with existing_engine.connect() as conn:
        cols = await _columns(conn)
        assert "cooldown_hold_keepout_y_mm" not in cols
        assert "cooldown_hold_clear_above_mm" not in cols


@pytest.mark.asyncio
async def test_migration_adds_columns_and_seeds_h2s(existing_engine):
    async with existing_engine.begin() as conn:
        await run_migrations(conn)
    async with existing_engine.connect() as conn:
        cols = await _columns(conn)
        assert "cooldown_hold_keepout_y_mm" in cols
        assert "cooldown_hold_clear_above_mm" in cols
        # The 2026-09-10 numbers: keep-out = vendor rear service Y295 - 10, clear height
        # MEASURED by the operator the same day.
        assert await _hold(conn, "H2S") == (285.0, _H2S_COOLDOWN_HOLD_CLEAR_ABOVE_MM)


@pytest.mark.asyncio
async def test_every_other_model_stays_null(existing_engine):
    """Fan-only until measured. H2C explicitly gets NO numbers, and neither does any
    provisional row the registry seeds — a clearance is a per-machine physical fact."""
    async with existing_engine.begin() as conn:
        await run_migrations(conn)
    async with existing_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT model_key FROM printer_model_geometry "
                    "WHERE cooldown_hold_keepout_y_mm IS NOT NULL OR cooldown_hold_clear_above_mm IS NOT NULL"
                )
            )
        ).fetchall()
        assert [r[0] for r in rows] == ["H2S"]
        assert await _hold(conn, "H2C") == (None, None)


@pytest.mark.asyncio
async def test_no_row_is_ever_one_sided(existing_engine):
    """``ModelGeometry`` raises on a one-sided pair, so a row carrying exactly one of the
    two numbers would fail the geometry read for that model outright."""
    async with existing_engine.begin() as conn:
        await run_migrations(conn)
    async with existing_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT model_key FROM printer_model_geometry "
                    "WHERE (cooldown_hold_keepout_y_mm IS NULL) != (cooldown_hold_clear_above_mm IS NULL)"
                )
            )
        ).fetchall()
        assert rows == []


@pytest.mark.asyncio
async def test_existing_value_survives_rerun(existing_engine):
    """The IS NULL guard means a re-run never rewrites a value already in place — the
    same contract the z_travel backfill carries."""
    async with existing_engine.begin() as conn:
        await run_migrations(conn)
    async with existing_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE printer_model_geometry "
                "SET cooldown_hold_keepout_y_mm = 270, cooldown_hold_clear_above_mm = 64 "
                "WHERE model_key = 'H2S'"
            )
        )
    async with existing_engine.begin() as conn:
        await run_migrations(conn)  # second boot
    async with existing_engine.connect() as conn:
        assert await _hold(conn, "H2S") == (270.0, 64.0)


@pytest.mark.asyncio
async def test_fresh_create_and_seed(fresh_engine):
    async with fresh_engine.begin() as conn:
        await run_migrations(conn)
    async with fresh_engine.connect() as conn:
        cols = await _columns(conn)
        assert "cooldown_hold_keepout_y_mm" in cols
        assert "cooldown_hold_clear_above_mm" in cols
        assert await _hold(conn, "H2S") == (285.0, _H2S_COOLDOWN_HOLD_CLEAR_ABOVE_MM)
        assert await _hold(conn, "H2C") == (None, None)


@pytest.mark.asyncio
async def test_migration_is_idempotent(existing_engine):
    async with existing_engine.begin() as conn:
        await run_migrations(conn)
    async with existing_engine.begin() as conn:
        await run_migrations(conn)
    async with existing_engine.connect() as conn:
        assert "cooldown_hold_keepout_y_mm" in await _columns(conn)
        assert await _hold(conn, "H2S") == (285.0, _H2S_COOLDOWN_HOLD_CLEAR_ABOVE_MM)
        # Exactly one H2S row — the seeds never duplicate on a re-run.
        count = (
            await conn.execute(text("SELECT COUNT(*) FROM printer_model_geometry WHERE model_key = 'H2S'"))
        ).scalar()
        assert count == 1


# --------------------------------------------------------------------------- #
# The one-time migration: 51 (placeholder) -> 100 (measured)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_placeholder_is_lifted_to_the_measured_value(placeholder_engine, caplog):
    """The whole point: an install already carrying 51.0 gets the measured 100.0, once,
    with a marker row and one greppable ``[MIGRATION]`` line naming the rowcount."""
    import logging

    with caplog.at_level(logging.INFO, logger="backend.app.core.database"):
        async with placeholder_engine.begin() as conn:
            await run_migrations(conn)
    async with placeholder_engine.connect() as conn:
        assert await _hold(conn, "H2S") == (285.0, _H2S_COOLDOWN_HOLD_CLEAR_ABOVE_MM)
        assert await _marker_rows(conn) == 1
    line = next(m for m in (r.getMessage() for r in caplog.records) if "cooldown-hold clearance" in m)
    assert "51.0 -> 100.0 mm (measured) on 1 row(s)" in line


@pytest.mark.asyncio
async def test_a_second_boot_never_rewrites_a_re_measure(placeholder_engine):
    """Value-as-identity is exactly what the marker exists to prevent: once the migration
    has run, a clearance that is LATER re-measured (or hand-repaired) back to 51.0 is the
    operator's number and must survive every subsequent boot."""
    async with placeholder_engine.begin() as conn:
        await run_migrations(conn)
    async with placeholder_engine.begin() as conn:
        await conn.execute(
            text("UPDATE printer_model_geometry SET cooldown_hold_clear_above_mm = :v WHERE model_key = 'H2S'"),
            {"v": _PLACEHOLDER_CLEAR_MM},
        )
    async with placeholder_engine.begin() as conn:
        await run_migrations(conn)  # second boot
    async with placeholder_engine.connect() as conn:
        assert await _hold(conn, "H2S") == (285.0, _PLACEHOLDER_CLEAR_MM)
        assert await _marker_rows(conn) == 1  # and the marker is never duplicated


@pytest.mark.asyncio
async def test_a_hand_set_clearance_is_never_touched(placeholder_engine):
    """The migration is keyed on the placeholder VALUE alone, so any other number — an
    operator PUT, a partial re-measure — is out of its scope by construction."""
    async with placeholder_engine.begin() as conn:
        await conn.execute(
            text("UPDATE printer_model_geometry SET cooldown_hold_clear_above_mm = 60.0 WHERE model_key = 'H2S'")
        )
        await run_migrations(conn)
    async with placeholder_engine.connect() as conn:
        assert await _hold(conn, "H2S") == (285.0, 60.0)
        assert await _marker_rows(conn) == 1  # ran (and did nothing), so it never re-runs


@pytest.mark.asyncio
async def test_null_rows_are_left_to_the_seed(placeholder_engine):
    """A fan-only model has no clearance to lift: the migration touches H2S alone, and a
    row it emptied would be a one-sided pair the geometry read refuses outright."""
    async with placeholder_engine.begin() as conn:
        await run_migrations(conn)
    async with placeholder_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT model_key FROM printer_model_geometry "
                    "WHERE cooldown_hold_clear_above_mm IS NOT NULL OR cooldown_hold_keepout_y_mm IS NOT NULL"
                )
            )
        ).fetchall()
        assert [r[0] for r in rows] == ["H2S"]


@pytest.mark.asyncio
async def test_a_fresh_db_is_seeded_at_the_measured_value_and_the_migration_is_a_no_op(fresh_engine, caplog):
    """A new install never meets the placeholder at all — the seed writes the constant
    directly, and the migration runs once, matches nothing, and marks itself done."""
    import logging

    with caplog.at_level(logging.INFO, logger="backend.app.core.database"):
        async with fresh_engine.begin() as conn:
            await run_migrations(conn)
    async with fresh_engine.connect() as conn:
        assert await _hold(conn, "H2S") == (285.0, _H2S_COOLDOWN_HOLD_CLEAR_ABOVE_MM)
        assert await _marker_rows(conn) == 1
    line = next(m for m in (r.getMessage() for r in caplog.records) if "cooldown-hold clearance" in m)
    assert "on 0 row(s)" in line
