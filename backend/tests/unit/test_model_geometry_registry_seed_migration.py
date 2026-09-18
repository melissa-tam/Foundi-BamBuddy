"""Regression test for the printer_model_geometry fleet-seed migration.

``run_migrations`` seeds five published-spec, ``validated=FALSE`` (MEASURE AT
LADDER) rows alongside the existing H2S/H2C seeds — P1S, P2S, H2D, X2D and the
A2L bed-slinger — through the same idempotent ``INSERT .. SELECT .. WHERE NOT
EXISTS`` shape, so a re-run neither duplicates a row nor clobbers an operator
edit. Both upgrade paths end at the same seven rows: a DB that already has the
table, and a FRESH one where ``CREATE TABLE`` has to build it first.

Two seeded values are not derivable from the rest:

* A2L's ``z_travel_mm`` is a literal NULL — it is a bed-slinger, so the bed-drop
  assist must fail closed independently of the bedslinger guard;
* X2D's X envelope 20.5–235.5 is the per-side intersection of its dual-mode
  envelopes, not one head's full range.

The full schema is built (``create_memory_engine``) before ``run_migrations``
because its many other table ALTERs raise ``no such table`` on a table that was
never created — an error ``_safe_execute`` does NOT swallow.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from backend.app.core.database import run_migrations
from backend.tests._fixtures.db import create_memory_engine

pytestmark = pytest.mark.usefixtures("force_sqlite_dialect")

_EXPECTED_KEYS = {"H2S", "H2C", "P1S", "P2S", "H2D", "X2D", "A2L"}


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
    eng = await create_memory_engine()
    yield eng
    await eng.dispose()


@pytest.fixture
async def fresh_engine():
    """A DB with NO geometry table — run_migrations must CREATE + seed it."""
    eng = await create_memory_engine()
    async with eng.begin() as conn:
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
