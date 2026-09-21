"""The one-time extruder-side out-of-rotation repair migration.

The recovery driver has always held that an EXTRUDER-side fault's common factor is the
extruder, not the spool — but it applied that rule only to the replacement roll it
picked, so the jammed slot's spool was stamped out of rotation regardless. A printer
working through repeat ``0300_801E`` overloads walked its own inventory down that way
(printer 8, 09-11/12: 2 → 1 → 3 → 0 eligible spools).

``repair_extruder_side_oor_20260921`` states the same fact about the stamps already
standing, which no live lane will revisit: nothing re-reads a parked roll's diagnosis,
and physical re-insertion is the only clear. Scope is the LIVE population — every
``feed_fault_at IS NOT NULL`` row — classified through the taxonomy that decided the
stamp in the first place; extruder-side clears the PAIR, everything else is left exactly
as it stands. What these tests pin is the partition (clear / skip / skip), the durable
marker, and that a stamp landing AFTER the marker is a new, correct stamp the repair
never revisits.
"""

from __future__ import annotations

import logging
from datetime import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.database import run_migrations
from backend.tests._fixtures.db import create_memory_engine

pytestmark = pytest.mark.usefixtures("force_sqlite_dialect")

_MARKER = "repair_extruder_side_oor_20260921"

# ``0300_801E`` (extruder overload) is the 006-H2S code and the reason this repair
# exists; ``0700_8010`` is an AMS-side feed fault where the roll IS the common factor.
_EXTRUDER_SIDE = "0300_801E"
_AMS_SIDE = "0700_8010"

_STAMPED_AT = datetime(2026, 9, 21, 4, 57, 35)  # naive UTC, matching what the DB stores


@pytest.fixture
async def engine():
    eng = await create_memory_engine()
    yield eng
    await eng.dispose()


async def _seed_parked_spool(
    engine,
    *,
    spool_id: int,
    feed_fault_code: str | None,
    feed_fault_at: datetime | None = _STAMPED_AT,
) -> None:
    """One out-of-rotation roll, stamped the way ``_mark_out_of_rotation`` stamps it."""
    from backend.app.models.spool import Spool

    async with AsyncSession(engine, expire_on_commit=False) as session:
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
                weight_used=120.0,
                data_origin="rfid_auto",
                feed_fault_at=feed_fault_at,
                feed_fault_code=feed_fault_code,
            )
        )
        await session.commit()


async def _run(engine) -> None:
    async with engine.begin() as conn:
        await run_migrations(conn)


async def _fault_pair(engine, spool_id: int) -> tuple[datetime | None, str | None]:
    """The two columns as the application sees them — read through the ORM so the
    DATETIME comes back typed (raw ``text()`` hands SQLite's storage string straight on)."""
    from sqlalchemy import select

    from backend.app.models.spool import Spool

    async with AsyncSession(engine) as session:
        return (
            await session.execute(select(Spool.feed_fault_at, Spool.feed_fault_code).where(Spool.id == spool_id))
        ).one()


async def _marker_count(engine) -> int:
    async with engine.connect() as conn:
        return (await conn.execute(text("SELECT COUNT(*) FROM settings WHERE key = :k"), {"k": _MARKER})).scalar()


@pytest.mark.asyncio
async def test_repair_clears_the_pair_on_an_extruder_side_stamp(engine, caplog):
    """The whole repair on one row: both columns, together. Clearing the flag alone
    would leave a stale diagnosis on a roll the farm has just declared healthy — the
    002-H2S pair bug, restated."""
    await _seed_parked_spool(engine, spool_id=817, feed_fault_code=_EXTRUDER_SIDE)

    with caplog.at_level(logging.INFO):
        await _run(engine)

    assert await _fault_pair(engine, 817) == (None, None), "the flag and the code it was stamped with"
    assert f"[REPAIR] {_MARKER}: spool 817 returned to rotation" in caplog.text
    assert f"feed_fault_code='{_EXTRUDER_SIDE}'" in caplog.text, (
        "the pre-image rides the line — the log is the only rollback this repair has"
    )
    assert await _marker_count(engine) == 1


@pytest.mark.asyncio
async def test_repair_skips_an_ams_side_stamp(engine, caplog):
    """An AMS-side feed fault is a verdict about the ROLL, and it stands. Scope is read
    from the live population, so the skip is a decision the repair has to make and say."""
    await _seed_parked_spool(engine, spool_id=791, feed_fault_code=_AMS_SIDE)

    with caplog.at_level(logging.INFO):
        await _run(engine)

    assert await _fault_pair(engine, 791) == (_STAMPED_AT, _AMS_SIDE), "untouched, both columns"
    assert f"[REPAIR] {_MARKER}: skip spool 791" in caplog.text
    assert "not extruder-side" in caplog.text


@pytest.mark.asyncio
async def test_repair_skips_a_null_code(engine, caplog):
    """A stamp with no code names nothing the taxonomy can rule on, so the repair does
    not rule on it either — a NULL code is a logged skip, never an inferred clear."""
    await _seed_parked_spool(engine, spool_id=599, feed_fault_code=None)

    with caplog.at_level(logging.INFO):
        await _run(engine)

    assert await _fault_pair(engine, 599) == (_STAMPED_AT, None), "the flag stands"
    assert f"[REPAIR] {_MARKER}: skip spool 599" in caplog.text
    assert "no taxonomy row" in caplog.text


@pytest.mark.asyncio
async def test_every_decision_is_logged_under_the_marker(engine, caplog):
    """The post-deploy probe is "grep the marker, count the decisions" — a skip that only
    said ``[REPAIR]`` would be indistinguishable from the other repairs in the file."""
    await _seed_parked_spool(engine, spool_id=817, feed_fault_code=_EXTRUDER_SIDE)
    await _seed_parked_spool(engine, spool_id=791, feed_fault_code=_AMS_SIDE)
    await _seed_parked_spool(engine, spool_id=599, feed_fault_code=None)

    with caplog.at_level(logging.INFO):
        await _run(engine)

    assert caplog.text.count(f"[REPAIR] {_MARKER}:") == 4, "three per-row decisions plus the summary"
    assert "1 of 3 out-of-rotation spool(s) returned, 2 left parked" in caplog.text


@pytest.mark.asyncio
async def test_a_spool_in_rotation_is_never_in_scope(engine, caplog):
    """``feed_fault_at IS NOT NULL`` is the whole scope: a healthy roll is not a decision,
    and a code left standing beside a NULL flag is not this repair's business."""
    await _seed_parked_spool(engine, spool_id=42, feed_fault_code=_EXTRUDER_SIDE, feed_fault_at=None)

    with caplog.at_level(logging.INFO):
        await _run(engine)

    assert await _fault_pair(engine, 42) == (None, _EXTRUDER_SIDE), "not read, not written"
    assert f"[REPAIR] {_MARKER}: skip spool 42" not in caplog.text
    assert f"[REPAIR] {_MARKER}: spool 42" not in caplog.text


@pytest.mark.asyncio
async def test_a_failed_repair_rolls_back_whole_and_keeps_the_marker_unwritten(engine, monkeypatch, caplog):
    """The guard, both halves. Startup migrations must survive a repair that cannot run —
    every other install boots regardless — and the failure must leave NO half-repair and
    no marker, so a fixed build simply tries again. The savepoint is what makes the DML
    and the marker one statement."""
    from backend.app.services import hms_errors

    def _explode(*args, **kwargs):
        raise RuntimeError("simulated taxonomy failure")

    monkeypatch.setattr(hms_errors, "classify_short_code", _explode)
    await _seed_parked_spool(engine, spool_id=817, feed_fault_code=_EXTRUDER_SIDE)

    with caplog.at_level(logging.INFO):
        await _run(engine)  # must NOT raise

    assert await _fault_pair(engine, 817) == (_STAMPED_AT, _EXTRUDER_SIDE), "nothing half-written"
    assert await _marker_count(engine) == 0, "unmarked: a later boot retries"
    assert f"{_MARKER} failed and was rolled back" in caplog.text


@pytest.mark.asyncio
async def test_second_run_is_noop_even_for_a_fresh_stamp(engine):
    """THE reason for the marker. A roll parked AFTER the repair ran carries a new,
    correct stamp — the driver's verdict about a fault the farm is handling now — and a
    self-predicating version, which would find the same ``0300_801E`` shape, would
    silently undo it at the next boot."""
    await _seed_parked_spool(engine, spool_id=817, feed_fault_code=_EXTRUDER_SIDE)
    await _run(engine)
    assert await _fault_pair(engine, 817) == (None, None)

    await _seed_parked_spool(engine, spool_id=900, feed_fault_code=_EXTRUDER_SIDE)
    await _run(engine)

    assert await _fault_pair(engine, 900) == (_STAMPED_AT, _EXTRUDER_SIDE), "the marker never revisits"
    assert await _marker_count(engine) == 1, "the marker row is written once, never duplicated"
