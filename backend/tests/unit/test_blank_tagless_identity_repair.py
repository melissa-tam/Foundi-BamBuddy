"""The one-time blank-tagless-identity repair migration (2026-09-10).

The defect: ``mint_tagless_spool``'s TRAY arm hardcoded ``brand = None`` while its
DEFAULT arm read the setting's brand — two identities out of one function for one
filament. Every farm-configured tagless tray reports ``tray_sub_brands=""`` on the
wire, so the parse yields no subtype either, and the row landed with both fields NULL:
208 of 269 live ``ams_auto`` rows, 205 of them the fleet default. The operator-visible
cost was not cosmetic — the inventory form requires brand AND subtype on every save,
so correcting a weight on one of those rows silently did nothing.

``repair_blank_tagless_identity_20260910`` fills the pair the mint owed, and only
where the row's own identity IS the default filament — adjudicated per row through
``spool_tagless.default_row_identity``, the same predicate tomorrow's mints use, so
the repair and the code can never disagree about what the default is. What these tests
pin is mostly the REFUSALS, plus the durable marker: the state this repair writes is
exactly what a self-predicating UPDATE would test for, so a re-running version would
re-fill a brand the operator had deliberately cleared at every boot.

Named apart from the neighbouring ``test_tagless_default_identity_migration``, which
rewrites the SETTING — a different fact.

SQLite-safe and self-contained, mirroring the sibling migration regression tests.
"""

from __future__ import annotations

import json
import logging

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from backend.app.core.database import run_migrations

_MARKER = "repair_blank_tagless_identity_20260910"

# The production tagless default, verified live 2026-07-25 and unchanged since.
_DEFAULT = {
    "brand": "Bambu Lab",
    "material": "PETG",
    "subtype": "HF",
    "rgba": "000000FF",
    "slicer_filament": "GFG02",
    "nozzle_temp_min": 230,
    "nozzle_temp_max": 270,
}


@pytest.fixture(autouse=True)
def force_sqlite_dialect(monkeypatch):
    """Force the SQLite branch regardless of test env settings."""
    from backend.app.core import db_dialect

    monkeypatch.setattr(db_dialect, "is_sqlite", lambda: True)
    monkeypatch.setattr(db_dialect, "is_postgres", lambda: False)
    from backend.app.core import database as database_module

    monkeypatch.setattr(database_module, "is_sqlite", lambda: True)


def _register_all_models():
    """Import EVERY model module so ``create_all`` builds the whole schema (see the
    sibling migration tests: ``run_migrations`` ALTERs across the schema and
    ``_safe_execute`` re-raises "no such table")."""
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


async def _set_default(engine, value: str | None = None) -> None:
    """Write the ``tagless_default_filament`` setting.

    Unset resolves to the schema default (which IS ``_DEFAULT``), so the tests that
    want the feature ON still write it explicitly — a test must not depend on a
    schema default it is not pinning.
    """
    from backend.app.models.settings import Settings

    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(Settings(key="tagless_default_filament", value=json.dumps(_DEFAULT) if value is None else value))
        await session.commit()


async def _seed_spool(
    engine,
    *,
    spool_id: int,
    brand: str | None = None,
    subtype: str | None = None,
    material: str = "PETG",
    rgba: str = "000000FF",
    slicer_filament: str | None = "GFG02",
    data_origin: str = "ams_auto",
    archived_at=None,
    spent_at=None,
    weight_used: float = 120.0,
) -> None:
    from backend.app.models.spool import Spool

    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(
            Spool(
                id=spool_id,
                material=material,
                subtype=subtype,
                brand=brand,
                rgba=rgba,
                slicer_filament=slicer_filament,
                nozzle_temp_min=230,
                nozzle_temp_max=270,
                label_weight=1000,
                core_weight=250,
                weight_used=weight_used,
                data_origin=data_origin,
                archived_at=archived_at,
                spent_at=spent_at,
            )
        )
        await session.commit()


async def _run(engine) -> None:
    async with engine.begin() as conn:
        await run_migrations(conn)


async def _row(engine, sql: str, params: dict | None = None):
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params or {})).fetchall()


async def _identity(engine, spool_id: int):
    rows = await _row(
        engine,
        "SELECT brand, subtype, material, rgba, weight_used FROM spool WHERE id = :i",
        {"i": spool_id},
    )
    return rows[0] if rows else None


async def _marker_count(engine) -> int:
    return (await _row(engine, "SELECT COUNT(*) FROM settings WHERE key = :k", {"k": _MARKER}))[0][0]


@pytest.mark.asyncio
async def test_fills_both_null_fields_on_the_default_filament(engine):
    """The row the operator could not save: the production wire shape, both fields
    NULL, and nothing else about it touched."""
    await _set_default(engine)
    await _seed_spool(engine, spool_id=1)

    await _run(engine)

    brand, subtype, material, rgba, weight_used = await _identity(engine, 1)
    assert (brand, subtype) == ("Bambu Lab", "HF")
    assert (material, rgba) == ("PETG", "000000FF"), "identity dimensions are the mint's, never the repair's"
    assert weight_used == pytest.approx(120.0), "the ledger is not a field this repair has any opinion about"


@pytest.mark.asyncio
async def test_fills_only_the_null_field(engine):
    """A row that already carries the default's subtype needs only its brand. The
    repair fills holes; it does not restate what the row already says."""
    await _set_default(engine)
    await _seed_spool(engine, spool_id=2, subtype="HF")

    await _run(engine)

    assert (await _identity(engine, 2))[:2] == ("Bambu Lab", "HF")


@pytest.mark.asyncio
async def test_reaches_archived_and_spent_rows(engine):
    """Deliberately in scope. Corrected code re-mints only at the NEXT roll change, so
    a retired row is exactly the one it can never reach — and the operator still opens
    those to correct a weight."""
    from datetime import datetime

    await _set_default(engine)
    await _seed_spool(engine, spool_id=3, archived_at=datetime(2026, 9, 1, 12, 0, 0))
    await _seed_spool(engine, spool_id=4, spent_at=datetime(2026, 9, 2, 12, 0, 0))

    await _run(engine)

    assert (await _identity(engine, 3))[:2] == ("Bambu Lab", "HF")
    assert (await _identity(engine, 4))[:2] == ("Bambu Lab", "HF")


@pytest.mark.asyncio
async def test_skips_a_row_that_is_not_the_default_filament(engine, caplog):
    """A grey PLA roll is a third-party roll as far as the farm knows, and inventing a
    brand for it would be worse than the blank. The edit form asks the operator once."""
    await _set_default(engine)
    await _seed_spool(engine, spool_id=5, material="PLA", rgba="808080FF", slicer_filament="GFA00")

    with caplog.at_level(logging.INFO):
        await _run(engine)

    assert (await _identity(engine, 5))[:2] == (None, None)
    assert f"[REPAIR] {_MARKER}: filled 0 row(s), skipped 1" in caplog.text, (
        "a silent skip and a silent fill look identical in a log — the counts are the probe"
    )


@pytest.mark.asyncio
async def test_skips_a_row_asserting_a_different_subtype(engine):
    """PAIR-OR-NOTHING. "PETG Basic" on a black GFG02 row is the tray having said
    otherwise (doctrine rule 2); stamping "Bambu Lab" beside it would compose an
    identity neither source ever stated."""
    await _set_default(engine)
    await _seed_spool(engine, spool_id=6, subtype="Basic")

    await _run(engine)

    assert (await _identity(engine, 6))[:2] == (None, "Basic")


@pytest.mark.asyncio
async def test_leaves_non_ams_auto_rows_alone(engine):
    """The RFID lane sets its own brand and an operator row is the operator's. Scope is
    the origin that had the defect."""
    await _set_default(engine)
    await _seed_spool(engine, spool_id=7, data_origin="rfid_auto")
    await _seed_spool(engine, spool_id=8, data_origin="manual")

    await _run(engine)

    assert (await _identity(engine, 7))[:2] == (None, None)
    assert (await _identity(engine, 8))[:2] == (None, None)


@pytest.mark.asyncio
async def test_feature_off_writes_the_marker_and_nothing_else(engine, caplog):
    """No default configured ⇒ no default identity exists to project. The marker is
    still written: the repair RAN and concluded, and a later boot must not re-decide it
    against a setting the operator has since changed."""
    await _set_default(engine, value="")
    await _seed_spool(engine, spool_id=9)

    with caplog.at_level(logging.INFO):
        await _run(engine)

    assert (await _identity(engine, 9))[:2] == (None, None)
    assert await _marker_count(engine) == 1
    assert "tagless default filament is off" in caplog.text


@pytest.mark.asyncio
async def test_second_run_is_a_noop(engine):
    """THE reason for the marker. A re-running self-predicating UPDATE would test for
    exactly the state this wrote, so an operator who cleared a brand by hand would find
    it back at the next restart."""
    await _set_default(engine)
    await _seed_spool(engine, spool_id=10)
    await _run(engine)

    async with AsyncSession(engine) as session:
        await session.execute(text("UPDATE spool SET brand = NULL WHERE id = 10"))
        await session.commit()

    await _run(engine)

    assert (await _identity(engine, 10))[0] is None, "the operator's clear survives the next boot"
    assert await _marker_count(engine) == 1, "the marker row is written once, never duplicated"


@pytest.mark.asyncio
async def test_a_failed_repair_rolls_back_whole_and_keeps_the_marker_unwritten(engine, monkeypatch, caplog):
    """The guard, both halves. Startup migrations must survive a repair that cannot run
    — every other install boots regardless — and the failure must leave NO half-repair
    and no marker, so a fixed build simply tries again. The savepoint is what makes
    those two the same statement."""
    from backend.app.services import spool_tagless

    def _explode(*args, **kwargs):
        raise RuntimeError("simulated repair failure")

    monkeypatch.setattr(spool_tagless, "default_row_identity", _explode)
    await _set_default(engine)
    await _seed_spool(engine, spool_id=11)

    with caplog.at_level(logging.INFO):
        await _run(engine)  # must NOT raise

    assert (await _identity(engine, 11))[:2] == (None, None), "nothing half-written"
    assert await _marker_count(engine) == 0, "unmarked: a later boot retries"
    assert f"{_MARKER} failed and was rolled back" in caplog.text
