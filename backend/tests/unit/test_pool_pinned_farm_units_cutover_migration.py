"""The pool cutover migration (2026-09-04; the printer-subset POOL wave).

``printer_id`` changed meaning on a PENDING row. A run targeting a SUBSET of the fleet
used to round-robin its plates into hard ``printer_id`` values at creation time — a
placement decision materialised hours before any printer was asked whether it was free.
It now holds an operator PIN and nothing else: a subset is a POOL
(``target_printer_ids``), ``printer_id`` stays NULL while the unit waits, and the
scheduler places each unit against live fleet state at dispatch.

Every pre-release value is therefore mis-typed data, the same class as the 2026-08-12
``ams_mapping`` cutover — with one difference this file exists to pin: that migration
CLEARED to NULL, this one WRITES a reconstruction, so the reconstruction is decided per
BATCH from the only evidence that survived (the batch's own units) and the tests below
are mostly about that decision being right in each shape.

SQLite-safe and self-contained, mirroring the sibling migration regression tests.
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core.database import run_migrations

_MARKER = "migration_pool_pinned_farm_units_20260904"


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


async def _add_batch(conn, *, sku_file_id: int | None, name: str = "Run") -> int:
    """Insert one batch. ``sku_file_id`` non-null is what makes a batch a production
    RUN — and is the join that scopes the whole migration to farm work. FK enforcement
    is off in this engine, so an arbitrary id needs no ``sku_files`` row."""
    from backend.app.models.print_batch import PrintBatch

    result = await conn.execute(PrintBatch.__table__.insert().values(name=name, sku_file_id=sku_file_id))
    return result.inserted_primary_key[0]


async def _add_item(
    conn,
    *,
    batch_id: int | None = None,
    printer_id: int | None = None,
    position: int = 1,
    status: str = "pending",
    target_model: str | None = None,
    target_printer_ids: str | None = None,
) -> int:
    """Insert one queue row through the model's table so SQLAlchemy applies the
    Python-side column defaults (the NOT NULL booleans have no server default)."""
    from backend.app.models.print_queue import PrintQueueItem

    result = await conn.execute(
        PrintQueueItem.__table__.insert().values(
            batch_id=batch_id,
            printer_id=printer_id,
            position=position,
            status=status,
            target_model=target_model,
            target_printer_ids=target_printer_ids,
            plate_id=1,
        )
    )
    return result.inserted_primary_key[0]


async def _target(conn, item_id: int) -> tuple[int | None, str | None, str | None, int]:
    """(printer_id, target_model, target_printer_ids, position) — the row's whole target."""
    row = (
        await conn.execute(
            text("SELECT printer_id, target_model, target_printer_ids, position FROM print_queue WHERE id = :i"),
            {"i": item_id},
        )
    ).one()
    return (row[0], row[1], row[2], row[3])


@pytest.mark.asyncio
async def test_subset_batch_becomes_a_printers_pool_over_every_member(engine):
    """THE production shape: a run round-robined across a selected subset.

    The pool is reconstructed from the DISTINCT printer_ids of ALL the batch's units,
    any status — the round-robin touched every member once ``n_plates >= len(subset)``,
    so a completed unit is as good a witness of the operator's selection as a pending
    one. Positions are re-allocated contiguously in the NULL scope in OLD-position order
    so the round-robin's interleaving survives as queue order.
    """
    async with engine.begin() as conn:
        batch = await _add_batch(conn, sku_file_id=11)
        first = await _add_item(conn, batch_id=batch, printer_id=4, position=1)
        second = await _add_item(conn, batch_id=batch, printer_id=5, position=1)
        third = await _add_item(conn, batch_id=batch, printer_id=6, position=2)
        finished = await _add_item(conn, batch_id=batch, printer_id=7, position=1, status="completed")

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        for item_id in (first, second, third):
            printer_id, target_model, pool, _position = await _target(conn, item_id)
            assert printer_id is None, "the creation-time derivation must not survive as a pin"
            assert target_model is None
            assert pool == "[4,5,6,7]", "the completed unit's printer is part of the operator's subset"
        assert [(await _target(conn, i))[3] for i in (first, second, third)] == [1, 2, 3]
        assert await _target(conn, finished) == (7, None, None, 1), "a terminal row's printer_id is a RECORD"


@pytest.mark.asyncio
async def test_model_batch_top_up_rows_become_the_model_pool_not_a_printers_pool(engine):
    """A batch whose units name a target_model was a MODEL run all along.

    Its pinned pending rows are ``top_up_run``'s mis-pinned replacements of failed
    units. Reading them as a printers pool would silently narrow a whole-fleet run down
    to whichever machines happened to fail on it.
    """
    async with engine.begin() as conn:
        batch = await _add_batch(conn, sku_file_id=12)
        await _add_item(conn, batch_id=batch, printer_id=3, position=1, status="completed", target_model="H2S")
        first = await _add_item(conn, batch_id=batch, printer_id=4, position=1)
        second = await _add_item(conn, batch_id=batch, printer_id=5, position=2)

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        for item_id in (first, second):
            printer_id, target_model, pool, _position = await _target(conn, item_id)
            assert printer_id is None
            assert target_model == "H2S"
            assert pool is None


@pytest.mark.asyncio
async def test_single_plate_run_becomes_a_one_member_pool(engine):
    """Honest limit 1: a x1 run cannot witness the rest of its subset, so its
    recoverable set is the ONE printer it was pinned to. Identical behaviour to
    today — no worse, and stated rather than guessed at."""
    async with engine.begin() as conn:
        batch = await _add_batch(conn, sku_file_id=13)
        only = await _add_item(conn, batch_id=batch, printer_id=7, position=1)

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        printer_id, target_model, pool, _position = await _target(conn, only)
        assert (printer_id, target_model, pool) == (None, None, "[7]")


@pytest.mark.asyncio
async def test_non_farm_items_and_non_pending_rows_are_untouched(engine):
    """The ``sku_file_id`` join is the whole scope: on a plain batch or a standalone
    item a printer_id was always the operator's own choice. Past pending, it is the
    RECORD of where the dispatch actually ran."""
    async with engine.begin() as conn:
        plain_batch = await _add_batch(conn, sku_file_id=None, name="Plain batch")
        plain_item = await _add_item(conn, batch_id=plain_batch, printer_id=2, position=1)
        standalone = await _add_item(conn, batch_id=None, printer_id=3, position=1)

        farm_batch = await _add_batch(conn, sku_file_id=14)
        running = await _add_item(conn, batch_id=farm_batch, printer_id=8, position=1, status="printing")
        waiting = await _add_item(conn, batch_id=farm_batch, printer_id=4, position=2)

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        assert await _target(conn, plain_item) == (2, None, None, 1)
        assert await _target(conn, standalone) == (3, None, None, 1)
        assert await _target(conn, running) == (8, None, None, 1)
        # ...but the printing row still WITNESSES the subset the operator selected.
        printer_id, target_model, pool, _position = await _target(conn, waiting)
        assert (printer_id, target_model, pool) == (None, None, "[4,8]")


@pytest.mark.asyncio
async def test_migrated_rows_land_after_existing_null_scope_pending_work(engine):
    """Positions come from ``queue_builder.allocate_queue_positions`` — the ONE position
    rule — so a re-typed block appends to the NULL scope instead of colliding with the
    unassigned/model-based work already queued there."""
    async with engine.begin() as conn:
        await _add_item(conn, batch_id=None, printer_id=None, position=5)
        batch = await _add_batch(conn, sku_file_id=15)
        first = await _add_item(conn, batch_id=batch, printer_id=4, position=1)
        second = await _add_item(conn, batch_id=batch, printer_id=5, position=2)

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        assert [(await _target(conn, i))[3] for i in (first, second)] == [6, 7]


@pytest.mark.asyncio
async def test_clears_the_old_schedulers_transient_pick_from_pending_pool_rows(engine):
    """A pre-cutover MODEL unit held mid-dispatch still carries the old scheduler's
    pick; the pending-row invariant says a pool row carries no printer until its
    dispatch claims one. The pool columns are untouched; a PRINTING row keeps its
    dispatch record; a plain pinned row (no pool) is not a pool row and keeps its pin."""
    async with engine.begin() as conn:
        farm = await _add_batch(conn, sku_file_id=1)
        held_model = await _add_item(conn, batch_id=farm, printer_id=4, target_model="H2S", position=1)
        held_pool = await _add_item(conn, batch_id=None, printer_id=5, target_printer_ids="[5,6]", position=2)
        printing = await _add_item(conn, batch_id=farm, printer_id=6, target_model="H2S", status="printing", position=3)
        plain_pin = await _add_item(conn, batch_id=None, printer_id=7, position=4)
        await run_migrations(conn)

    async with engine.connect() as conn:
        assert (await _target(conn, held_model))[:3] == (None, "H2S", None)
        assert (await _target(conn, held_pool))[:3] == (None, None, "[5,6]")
        assert (await _target(conn, printing))[:3] == (6, "H2S", None)
        assert (await _target(conn, plain_pin))[:3] == (7, None, None)


@pytest.mark.asyncio
async def test_records_a_durable_marker_so_it_runs_once(engine):
    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        marker = (await conn.execute(text("SELECT value FROM settings WHERE key = :k"), {"k": _MARKER})).scalar()
    assert marker == "true"


@pytest.mark.asyncio
async def test_never_converts_an_operator_pin_made_after_the_cutover(engine):
    """THE reason for the marker. Post-cutover a pending farm printer_id IS an operator
    pin; a migration that re-ran every boot would convert away what the operator asked
    for on every restart."""
    async with engine.begin() as conn:
        batch = await _add_batch(conn, sku_file_id=16)
        await _add_item(conn, batch_id=batch, printer_id=4, position=1)
    async with engine.begin() as conn:
        await run_migrations(conn)

    # The operator now pins a farm unit deliberately, which the release allows again.
    async with engine.begin() as conn:
        pinned = await _add_item(conn, batch_id=batch, printer_id=9, position=7)

    # Reboot.
    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        assert await _target(conn, pinned) == (9, None, None, 7)
        count = (await conn.execute(text("SELECT COUNT(*) FROM settings WHERE key = :k"), {"k": _MARKER})).scalar()
    assert count == 1, "the marker row is written once, never duplicated"


@pytest.mark.asyncio
async def test_logs_one_line_per_batch_and_a_summary_with_its_honest_limits(engine, caplog):
    """Post-deploy the log is how the operator sees WHICH units were re-typed and what
    the reconstruction does NOT know."""
    async with engine.begin() as conn:
        batch = await _add_batch(conn, sku_file_id=17)
        first = await _add_item(conn, batch_id=batch, printer_id=4, position=1)
        second = await _add_item(conn, batch_id=batch, printer_id=5, position=2)

    with caplog.at_level(logging.INFO):
        async with engine.begin() as conn:
            await run_migrations(conn)

    assert f"{_MARKER}: batch {batch}" in caplog.text
    assert "kind=printers" in caplog.text
    assert f"[{first}, {second}]" in caplog.text
    assert "Honest limits" in caplog.text


@pytest.mark.asyncio
async def test_says_nothing_when_there_is_nothing_to_re_type(engine, caplog):
    """A fresh install must not log a cutover it did not perform."""
    with caplog.at_level(logging.INFO):
        async with engine.begin() as conn:
            await run_migrations(conn)

    assert _MARKER not in caplog.text
    async with engine.connect() as conn:
        marker = (await conn.execute(text("SELECT value FROM settings WHERE key = :k"), {"k": _MARKER})).scalar()
    assert marker == "true", "the marker is still written on a fresh install"
