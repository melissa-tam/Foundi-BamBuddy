"""Regression tests for the cooldown-fan startup migrations (2026-09-11).

Two migrations ship with the cooldown-fan wave, and both change what an operator
sees, so both are pinned here:

1. The retired "0 = off" encoding on ``farm_cooldown_aux_fan_percent`` is folded
   onto the new ``farm_cooldown_aux_fan_enabled`` switch. The speed setting used
   to carry two facts (how fast, and whether at all) and now carries only the
   speed. This one is MANDATORY rather than cosmetic: the update schema refuses 0
   for that key from here on, and the Settings -> Farm card PUTs every farm
   setting in one payload, so an install still storing '0' would 422 every save of
   that card — including saves that change something else entirely.

   Unlike the one-time repairs in the same file it is deliberately NOT
   marker-guarded: it is idempotent BY CONSTRUCTION because the mis-typed datum IS
   the '0' value, and once that row is deleted there is nothing left to fold. The
   percent row is DELETED rather than rewritten so that "full speed" keeps exactly
   one origin (an absent row materialises the schema default).

2. ``eject_profiles.cooling_fan_assist`` is dropped. It backed an operator toggle
   promising "runs the part-cooling fan during the sweep" that no backend code ever
   read, in either position. ``create_all`` builds the table WITHOUT the column now
   that the model has lost it, so the pre-migration shape is built explicitly here —
   otherwise the test would pass without the migration existing at all.

SQLite-safe and self-contained, mirroring the sibling migration regression tests.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core.database import run_migrations

_SPEED_KEY = "farm_cooldown_aux_fan_percent"
_SWITCH_KEY = "farm_cooldown_aux_fan_enabled"
_DEAD_COLUMN = "cooling_fan_assist"


@pytest.fixture(autouse=True)
def force_sqlite_dialect(monkeypatch):
    """Force the SQLite branch regardless of test env settings."""
    from backend.app.core import db_dialect

    monkeypatch.setattr(db_dialect, "is_sqlite", lambda: True)
    monkeypatch.setattr(db_dialect, "is_postgres", lambda: False)
    from backend.app.core import database as database_module

    monkeypatch.setattr(database_module, "is_sqlite", lambda: True)


def _register_all_models():
    """Import EVERY model module so `create_all` builds the whole schema.

    Not a hand-picked subset, and not the package `__init__` either (it does not
    re-export every module — `virtual_printer` is one it misses). `run_migrations`
    ALTERs tables across the schema and `_safe_execute` deliberately RE-RAISES
    "no such table" (that is schema corruption, not idempotency), so a partial
    `create_all` leaves this file passing or failing according to which other test
    module happened to import a model first.
    """
    import importlib
    import pkgutil

    import backend.app.models as models_pkg

    for module in pkgutil.iter_modules(models_pkg.__path__):
        importlib.import_module(f"{models_pkg.__name__}.{module.name}")


@pytest.fixture
async def engine():
    from backend.app.core.database import Base

    _register_all_models()

    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


async def _set_setting(conn, key: str, value: str) -> None:
    await conn.execute(text("INSERT INTO settings (key, value) VALUES (:k, :v)"), {"k": key, "v": value})


async def _setting(conn, key: str) -> str | None:
    return (await conn.execute(text("SELECT value FROM settings WHERE key = :k"), {"k": key})).scalar()


async def _eject_profile_columns(conn) -> set[str]:
    rows = (await conn.execute(text("PRAGMA table_info(eject_profiles)"))).fetchall()
    return {row[1] for row in rows}


# ============================================================================
# 1. The "0 = off" fold
# ============================================================================


@pytest.mark.asyncio
async def test_stored_zero_becomes_the_switch_and_the_row_goes(engine):
    """'0' meant OFF; the switch now owns that fact and the speed row is deleted."""
    async with engine.begin() as conn:
        await _set_setting(conn, _SPEED_KEY, "0")
    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        assert await _setting(conn, _SWITCH_KEY) == "false"
        # Deleted, not rewritten: an absent row materialises the schema default, so
        # "full speed" keeps exactly one origin.
        assert await _setting(conn, _SPEED_KEY) is None


@pytest.mark.asyncio
async def test_fold_is_idempotent_by_construction(engine):
    """Every boot re-runs the migration set. The second pass finds no '0' row left to
    fold, so it must change nothing and raise nothing — no marker row needed."""
    async with engine.begin() as conn:
        await _set_setting(conn, _SPEED_KEY, "0")
    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        assert await _setting(conn, _SWITCH_KEY) == "false"
        assert await _setting(conn, _SPEED_KEY) is None


@pytest.mark.asyncio
async def test_a_real_speed_is_untouched_and_mints_no_switch(engine):
    """Only the mis-typed '0' is a fold candidate. A genuine speed is a speed: it stays,
    and it must NOT cause a switch row to be written (that would freeze the default
    where an absent row is meant to track it)."""
    async with engine.begin() as conn:
        await _set_setting(conn, _SPEED_KEY, "60")
    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        assert await _setting(conn, _SPEED_KEY) == "60"
        assert await _setting(conn, _SWITCH_KEY) is None


@pytest.mark.asyncio
async def test_an_existing_switch_row_is_never_overwritten(engine):
    """An operator who has already set the switch owns it — their value wins over the
    fold's inference. The stale '0' speed row still goes, because it is meaningless
    either way now."""
    async with engine.begin() as conn:
        await _set_setting(conn, _SPEED_KEY, "0")
        await _set_setting(conn, _SWITCH_KEY, "true")
    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        assert await _setting(conn, _SWITCH_KEY) == "true"
        assert await _setting(conn, _SPEED_KEY) is None


# ============================================================================
# 2. The dead eject-profile column
# ============================================================================


@pytest.mark.asyncio
async def test_drops_the_dead_cooling_fan_assist_column(engine):
    """The toggle never had a backend reader; the column goes with it.

    The model no longer declares the column, so ``create_all`` does not build it —
    the pre-migration shape has to be constructed explicitly or this test would pass
    against a migration that does not exist.
    """
    async with engine.begin() as conn:
        await conn.execute(text(f"ALTER TABLE eject_profiles ADD COLUMN {_DEAD_COLUMN} BOOLEAN NOT NULL DEFAULT 1"))
    async with engine.connect() as conn:
        assert _DEAD_COLUMN in await _eject_profile_columns(conn)  # sanity: the old shape exists

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        assert _DEAD_COLUMN not in await _eject_profile_columns(conn)


@pytest.mark.asyncio
async def test_drop_is_idempotent_on_an_already_migrated_table(engine):
    """The drop is presence-guarded (SQLite has no DROP COLUMN IF EXISTS and Postgres's
    "does not exist" is outside the _safe_execute swallow set), so a second boot — and a
    first boot on a fresh install that never had the column — must both be no-ops."""
    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        assert _DEAD_COLUMN not in await _eject_profile_columns(conn)
