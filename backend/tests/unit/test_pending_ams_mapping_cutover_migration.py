"""The pin-cutover repair migration (2026-08-12; 003-H2S external-spool incident).

``ams_mapping`` changed meaning in this release: it used to hold a derivation the
dialog computed at queue time and the scheduler replayed verbatim hours later; it now
holds an operator INSTRUCTION the matcher takes as an input while deciding afresh at
every dispatch. Every value written before the cutover is therefore mis-typed data — a
derivation sitting in a field that now reads as a human's choice — so a one-time repair
clears them on PENDING rows and lets those items decide against live state.

ONE-TIME is the whole point, and it is what this file mostly exists to pin: after the
cutover a pending mapping IS an operator pin, and a migration that re-ran would delete
a human's slot choice on every restart. Same durable-marker shape as the WS-F posture
migration (a settings row written in the same nested transaction as the clear).

SQLite-safe and self-contained, mirroring the sibling migration regression tests.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core.database import run_migrations

_MARKER = "migration_clear_pending_ams_mapping_20260812"


@pytest.fixture(autouse=True)
def force_sqlite_dialect(monkeypatch):
    """Force the SQLite branch regardless of test env settings."""
    from backend.app.core import db_dialect

    monkeypatch.setattr(db_dialect, "is_sqlite", lambda: True)
    monkeypatch.setattr(db_dialect, "is_postgres", lambda: False)
    from backend.app.core import database as database_module

    monkeypatch.setattr(database_module, "is_sqlite", lambda: True)


def _register_all_models():
    """Import EVERY model module so `create_all` builds the whole schema (see the
    sibling migration tests: `run_migrations` ALTERs across the schema and
    `_safe_execute` re-raises "no such table")."""
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


async def _add_item(conn, *, status: str, mapping: str | None, position: int = 1) -> int:
    """Insert one queue row through the model's table so SQLAlchemy applies the
    Python-side column defaults (the NOT NULL booleans have no server default)."""
    from backend.app.models.print_queue import PrintQueueItem

    result = await conn.execute(
        PrintQueueItem.__table__.insert().values(status=status, ams_mapping=mapping, position=position, plate_id=1)
    )
    return result.inserted_primary_key[0]


async def _mapping(conn, item_id: int) -> str | None:
    return (await conn.execute(text("SELECT ams_mapping FROM print_queue WHERE id = :i"), {"i": item_id})).scalar()


@pytest.mark.asyncio
async def test_clears_cached_mappings_on_pending_items(engine):
    """The eight landmines: pending rows carrying a pre-cutover derivation."""
    async with engine.begin() as conn:
        landmine = await _add_item(conn, status="pending", mapping="[254]", position=1)
        other = await _add_item(conn, status="pending", mapping="[0, -1]", position=2)
    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        assert await _mapping(conn, landmine) is None
        assert await _mapping(conn, other) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["printing", "completed", "failed", "cancelled", "skipped"])
async def test_leaves_the_dispatch_record_on_non_pending_rows(engine, status):
    """Past pending, the column is the RECORD of what actually fed the print — read
    back by usage tracking, the spent ledger and recovery. Clearing it would destroy
    ledger evidence, so the repair is scoped to the pre-dispatch vocabulary only."""
    async with engine.begin() as conn:
        item = await _add_item(conn, status=status, mapping="[3]")
    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        assert await _mapping(conn, item) == "[3]"


@pytest.mark.asyncio
async def test_records_a_durable_marker_so_it_runs_once(engine):
    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        marker = (await conn.execute(text("SELECT value FROM settings WHERE key = :k"), {"k": _MARKER})).scalar()
    assert marker == "true"


@pytest.mark.asyncio
async def test_never_clears_an_operator_pin_made_after_the_cutover(engine):
    """THE reason for the marker. Post-cutover a pending mapping is an INSTRUCTION; a
    migration that re-ran every boot would silently delete what the operator asked
    for — the exact class of bug this whole change exists to remove."""
    async with engine.begin() as conn:
        await _add_item(conn, status="pending", mapping="[254]")
    async with engine.begin() as conn:
        await run_migrations(conn)

    # The operator now pins a slot deliberately, in the fixed dialog.
    async with engine.begin() as conn:
        pinned = await _add_item(conn, status="pending", mapping="[1]", position=2)

    # Reboot.
    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        assert await _mapping(conn, pinned) == "[1]"


@pytest.mark.asyncio
async def test_is_idempotent_on_a_second_pass(engine):
    """A second run matches nothing and changes nothing — no error, no churn."""
    async with engine.begin() as conn:
        item = await _add_item(conn, status="pending", mapping="[254]")
    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        assert await _mapping(conn, item) is None
        count = (await conn.execute(text("SELECT COUNT(*) FROM settings WHERE key = :k"), {"k": _MARKER})).scalar()
    assert count == 1, "the marker row is written once, never duplicated"


@pytest.mark.asyncio
async def test_logs_the_affected_item_ids(engine, caplog):
    """Post-deploy the log is how the operator sees WHICH queued units were repaired
    (S9's witness: the migration line names them, then each dispatches with a freshly
    computed mapping)."""
    import logging

    async with engine.begin() as conn:
        a = await _add_item(conn, status="pending", mapping="[254]", position=1)
        b = await _add_item(conn, status="pending", mapping="[254]", position=2)
    with caplog.at_level(logging.INFO):
        async with engine.begin() as conn:
            await run_migrations(conn)

    assert "cleared pre-cutover cached AMS mappings" in caplog.text
    assert str(a) in caplog.text and str(b) in caplog.text


@pytest.mark.asyncio
async def test_says_nothing_when_there_is_nothing_to_repair(engine, caplog):
    """A fresh install must not log a repair it did not perform."""
    import logging

    async with engine.begin() as conn:
        await _add_item(conn, status="pending", mapping=None)
    with caplog.at_level(logging.INFO):
        async with engine.begin() as conn:
            await run_migrations(conn)

    assert "cleared pre-cutover cached AMS mappings" not in caplog.text
