"""The one-time runout-reclaim repair migration (2026-08-13, seven printers).

The incident: the AMS clears a drained slot's exist bit ~3 minutes BEFORE the firmware
declares the runout, so since release-on-empty started firing reliably every natural
runout's binding was already gone when the runout HMS arrived — and the single spent
writer resolves its victim from a LIVE assignment. Nothing was stamped for three days,
and the released rows' ``last_location_*`` residue is exactly what the reclaim lane
resurrects: seven printers hung an exhausted roll's ledger row, ~900 g of history and
all, on the brand-new roll the operator had just loaded.

``repair_runout_reclaim_20260813`` states what physics already decided, for those seven
rows and no others: the dead row ran dry (spent + archived) and the roll now in the slot
is a DIFFERENT spool, so it gets its own row carrying only the grams charged since the
reclaim. What these tests pin is mostly the REFUSALS — every fact is re-verified against
live state first, because hours pass between the incident and the deploy that carries the
repair — plus the durable marker that keeps a repair which writes ``spent_at`` from
fighting the operator's un-spend route at every boot.

SQLite-safe and self-contained, mirroring the sibling migration regression tests.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from backend.app.core.database import run_migrations

_MARKER = "repair_runout_reclaim_20260813"

# Two of the seven (spool, printer) pairs the block carries, and the window it accepts
# their reclaim binding in. Naive UTC throughout, matching what the DB stores.
_SPOOL_37, _PRINTER_3 = 37, 3
_SPOOL_249, _PRINTER_8 = 249, 8

_IN_WINDOW = datetime(2026, 8, 13, 23, 42, 0)  # inside [23:30, 00:00]
_AFTER_WINDOW = datetime(2026, 8, 14, 2, 0, 0)  # a later, organic re-bind
_LONG_BEFORE = datetime(2026, 8, 1, 9, 0, 0)  # when the dead row entered service

# The fixture ledger: 500 g charged before the reclaim (the dead roll's own prints) and
# 446.6 g after it (the fresh roll's, misattributed).
_PRE = ((-1200, 500.0),)
_POST = ((10, 401.6), (40, 45.0))
_MOVED = 446.6


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


async def _seed_reclaimed_row(
    engine,
    *,
    spool_id: int,
    printer_id: int,
    ams_id: int = 0,
    tray_id: int = 1,
    bound_at: datetime = _IN_WINDOW,
    weight_used: float = 900.0,
    pre: tuple = _PRE,
    post: tuple = _POST,
    archived_at: datetime | None = None,
    spent_at: datetime | None = None,
) -> None:
    """One incident row: a dead ledger row still bound to the slot that reclaimed it.

    RFID-tagged on purpose — spool 37 was, and the successor must still mint tagless-shaped
    (the fresh roll's own tag has not been read yet). The printer row is not decoration: an
    earlier migration purges bindings whose printer no longer exists.
    """
    from backend.app.models.printer import Printer
    from backend.app.models.spool import Spool
    from backend.app.models.spool_assignment import SpoolAssignment
    from backend.app.models.spool_usage_history import SpoolUsageHistory

    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(
            Printer(
                id=printer_id,
                name=f"{printer_id:03d}-H2S",
                serial_number=f"SN{printer_id:08d}",
                ip_address=f"192.168.2.{printer_id}",
                access_code="00000000",
            )
        )
        session.add(
            Spool(
                id=spool_id,
                material="PETG",
                subtype="Basic",
                color_name="Jade White",
                rgba="00AE42FF",
                brand="Bambu Lab",
                label_weight=1000,
                core_weight=250,
                weight_used=weight_used,
                cost_per_kg=25.0,
                storage_location="Shelf B",
                data_origin="rfid_auto",
                tag_type="bambulab",
                tag_uid=f"{spool_id:016X}",
                last_used=bound_at,
                created_at=_LONG_BEFORE,
                archived_at=archived_at,
                spent_at=spent_at,
            )
        )
        await session.flush()
        session.add(
            SpoolAssignment(
                spool_id=spool_id,
                printer_id=printer_id,
                ams_id=ams_id,
                tray_id=tray_id,
                fingerprint_color="00AE42FF",
                fingerprint_type="PETG",
                created_at=bound_at,
            )
        )
        for offset_minutes, grams in (*pre, *post):
            session.add(
                SpoolUsageHistory(
                    spool_id=spool_id,
                    printer_id=printer_id,
                    print_name="SKU007.01",
                    weight_used=grams,
                    created_at=bound_at + timedelta(minutes=offset_minutes),
                )
            )
        await session.commit()


async def _run(engine) -> None:
    async with engine.begin() as conn:
        await run_migrations(conn)


async def _row(engine, sql: str, params: dict | None = None):
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params or {})).fetchall()


async def _slot_spool_id(engine, printer_id: int, ams_id: int = 0, tray_id: int = 1) -> int | None:
    rows = await _row(
        engine,
        "SELECT spool_id FROM spool_assignment WHERE printer_id = :p AND ams_id = :a AND tray_id = :t",
        {"p": printer_id, "a": ams_id, "t": tray_id},
    )
    return rows[0][0] if rows else None


async def _spool(engine, spool_id: int):
    rows = await _row(
        engine,
        "SELECT spent_at, archived_at, weight_used, material, brand, label_weight, data_origin, tag_uid, "
        "storage_location FROM spool WHERE id = :i",
        {"i": spool_id},
    )
    return rows[0] if rows else None


async def _marker_count(engine) -> int:
    return (await _row(engine, "SELECT COUNT(*) FROM settings WHERE key = :k", {"k": _MARKER}))[0][0]


@pytest.mark.asyncio
async def test_repair_replaces_reclaimed_row(engine):
    """The whole repair on one row: the dead roll is retired at its real ledger and the
    fresh roll gets a row of its own carrying exactly the grams charged since the reclaim.

    Double entry — every gram the donor loses the successor gains, so the repair invents
    and destroys nothing."""
    await _seed_reclaimed_row(engine, spool_id=_SPOOL_37, printer_id=_PRINTER_3)
    await _run(engine)

    donor = await _spool(engine, _SPOOL_37)
    assert donor[0] is not None, "the roll physically ran dry: spent_at is the exhaustion truth"
    assert donor[1] is not None, "and it left service at the reclaim: archived"
    assert donor[2] == pytest.approx(900.0 - _MOVED), "the misattributed grams left the dead row"

    successor_id = await _slot_spool_id(engine, _PRINTER_3)
    assert successor_id is not None and successor_id != _SPOOL_37, "the slot now holds the new roll's row"
    successor = await _spool(engine, successor_id)
    assert successor[2] == pytest.approx(_MOVED), "carrying exactly what moved"
    assert successor[3] == "PETG" and successor[4] == "Bambu Lab" and successor[5] == 1000, "same filament"
    assert successor[8] == "Shelf B", "and the same inventory filing"
    assert successor[6] == "ams_auto" and successor[7] is None, (
        "tagless-shaped: the fresh roll's own tag has not been read yet, so the successor "
        "must never inherit the dead roll's RFID identity"
    )
    assert successor[0] is None and successor[1] is None, "the successor is live, not retired"

    charges = await _row(engine, "SELECT spool_id, weight_used FROM spool_usage_history ORDER BY created_at", None)
    assert [c[0] for c in charges] == [_SPOOL_37, successor_id, successor_id], (
        "the pre-reclaim print stays on the roll that fed it; both post-reclaim prints move"
    )
    assert await _marker_count(engine) == 1


@pytest.mark.asyncio
async def test_repair_moves_zero_charges(engine):
    """A print HELD on the runout has charged nothing yet — and the roll change is still
    real. ``require_positive_moved=False`` is what separates this repair from the live
    overcharge sweep, whose evidence IS the charges."""
    await _seed_reclaimed_row(engine, spool_id=_SPOOL_249, printer_id=_PRINTER_8, post=())
    await _run(engine)

    donor = await _spool(engine, _SPOOL_249)
    assert donor[0] is not None and donor[1] is not None, "retired on the operator's ruling, not on grams"
    assert donor[2] == pytest.approx(900.0), "nothing to move: the donor's ledger is untouched"

    successor_id = await _slot_spool_id(engine, _PRINTER_8)
    assert successor_id is not None and successor_id != _SPOOL_249
    assert (await _spool(engine, successor_id))[2] == pytest.approx(0.0), "a fresh roll that has printed nothing"


@pytest.mark.asyncio
async def test_repair_skips_organically_reconciled_row(engine, caplog):
    """Hours pass between the incident and the deploy. A row a live lane already retired is
    left entirely alone — and says so, because a silent skip and a silent repair look
    identical in a log."""
    await _seed_reclaimed_row(
        engine, spool_id=_SPOOL_37, printer_id=_PRINTER_3, archived_at=datetime(2026, 8, 14, 1, 0, 0)
    )
    with caplog.at_level(logging.INFO):
        await _run(engine)

    donor = await _spool(engine, _SPOOL_37)
    assert donor[0] is None, "no second opinion on a row somebody else already handled"
    assert donor[2] == pytest.approx(900.0)
    assert await _slot_spool_id(engine, _PRINTER_3) == _SPOOL_37, "no successor was minted"
    assert f"[REPAIR] {_MARKER}: skip spool 37: already retired" in caplog.text, (
        "every line carries the marker key — the post-deploy probe greps it and counts seven decisions"
    )


@pytest.mark.asyncio
async def test_repair_skips_rebind_outside_window(engine, caplog):
    """A binding created after the reclaim burst is a DIFFERENT event — the operator has
    moved the roll since, and repairing against it would rewrite a slot the incident never
    touched."""
    await _seed_reclaimed_row(engine, spool_id=_SPOOL_37, printer_id=_PRINTER_3, bound_at=_AFTER_WINDOW)
    with caplog.at_level(logging.INFO):
        await _run(engine)

    donor = await _spool(engine, _SPOOL_37)
    assert donor[0] is None and donor[1] is None and donor[2] == pytest.approx(900.0)
    assert await _slot_spool_id(engine, _PRINTER_3) == _SPOOL_37
    assert "outside the reclaim window" in caplog.text


@pytest.mark.asyncio
async def test_repair_skips_row_bound_to_a_different_printer(engine, caplog):
    """Same row, another printer: the incident named a (spool, printer) PAIR, and only that
    pair is evidence of tonight's reclaim."""
    await _seed_reclaimed_row(engine, spool_id=_SPOOL_37, printer_id=9)
    with caplog.at_level(logging.INFO):
        await _run(engine)

    assert (await _spool(engine, _SPOOL_37))[0] is None
    assert await _slot_spool_id(engine, 9) == _SPOOL_37
    assert "the incident named printer 3" in caplog.text
    assert caplog.text.count(f"[REPAIR] {_MARKER}:") == 8, "seven per-row decisions plus the summary"


@pytest.mark.asyncio
async def test_a_failed_repair_rolls_back_whole_and_keeps_the_marker_unwritten(engine, monkeypatch, caplog):
    """The guard, both halves. Startup migrations must survive a repair that cannot run —
    every other install boots regardless — and the failure must leave NO half-repair and no
    marker, so a fixed build simply tries again. The savepoint is what makes those two the
    same statement."""
    from backend.app.services import spool_tagless

    async def _explode(*args, **kwargs):
        raise RuntimeError("simulated repair failure")

    monkeypatch.setattr(spool_tagless, "replace_bound_row_with_successor", _explode)
    await _seed_reclaimed_row(engine, spool_id=_SPOOL_37, printer_id=_PRINTER_3)

    with caplog.at_level(logging.INFO):
        await _run(engine)  # must NOT raise

    donor = await _spool(engine, _SPOOL_37)
    assert donor[0] is None and donor[1] is None and donor[2] == pytest.approx(900.0)
    assert await _slot_spool_id(engine, _PRINTER_3) == _SPOOL_37, "nothing half-written"
    assert await _marker_count(engine) == 0, "unmarked: a later boot retries"
    assert f"{_MARKER} failed and was rolled back" in caplog.text


@pytest.mark.asyncio
async def test_second_run_is_noop(engine):
    """THE reason for the marker. The repair's own output (a spent+archived donor beside a
    fresh successor) is state a self-predicating version would keep re-deriving, so a
    re-running block would re-mint a successor every boot and undo any operator who cleared
    a stamp by hand."""
    await _seed_reclaimed_row(engine, spool_id=_SPOOL_37, printer_id=_PRINTER_3)
    await _run(engine)
    successor_id = await _slot_spool_id(engine, _PRINTER_3)

    await _run(engine)

    assert await _slot_spool_id(engine, _PRINTER_3) == successor_id, "no second successor"
    assert (await _row(engine, "SELECT COUNT(*) FROM spool"))[0][0] == 2, "one donor, one successor, forever"
    assert (await _spool(engine, successor_id))[2] == pytest.approx(_MOVED), "and its ledger was not moved again"
    assert (await _spool(engine, _SPOOL_37))[2] == pytest.approx(900.0 - _MOVED)
    assert await _marker_count(engine) == 1, "the marker row is written once, never duplicated"
