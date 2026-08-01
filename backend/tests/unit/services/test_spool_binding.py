"""Tests for services.spool_binding — the single binding writer + the FIFO stamps.

Structural invariant under test: **one spool row is bound to at most ONE AMS slot,
fleet-wide, at every instant**. A physical roll is in exactly one place, so every
re-bind is a MOVE (sweep the spool's old rows, then create the new one) — never a
copy. 012-H2S (2026-07-30): a copy left spool 120 bound to tray 0 AND tray 1 of one
printer for 22 h, both trays presented that single ledger to the ``min_start_spool_g``
start gate, and a ~22 g roll cleared the 150 g gate then ran out on layer 1.

Two layers are covered:

* :func:`bind_spool_to_slot` — the move-semantics sweep (S1 same-printer, S2
  cross-printer), the plain bind into an empty slot (S3), the same-slot replay stamp
  semantics (S4), and slot-upsert replacement of a different spool.
* the DB's ``ux_spool_assignment_spool_id`` unique index — a RAW insert that bypasses
  the helper must die loudly with an IntegrityError (fail-loud proof).

The three FIFO stamps (``stamp_first_loaded`` / ``stamp_loaded`` /
``stamp_loaded_for_slot``) live in this module too, so their unit tests live here —
they were relocated from ``test_spool_tag_matcher.py`` when the stamps moved out of
``spool_tag_matcher`` into ``spool_binding``.
"""

import logging
from datetime import datetime

import pytest
from sqlalchemy import func, inspect as sa_inspect, select
from sqlalchemy.exc import IntegrityError

from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services.spool_binding import (
    NEVER_FED_MAX_G,
    bind_spool_to_slot,
    stamp_loaded,
    stamp_loaded_for_slot,
)

_BINDING_LOGGER = "backend.app.services.spool_binding"

# A stamp value no clock can produce during a test run, so "unchanged" and
# "re-stamped" are decidable without sleeping on wall-clock resolution.
_SENTINEL = datetime(2020, 1, 2, 3, 4, 5)


# -- helpers -----------------------------------------------------------------


async def _fresh_spool(db_session, **kwargs):
    spool = Spool(material="PLA", label_weight=1000, core_weight=250, **kwargs)
    spool.k_profiles = []
    spool.assignments = []
    db_session.add(spool)
    await db_session.flush()
    return spool


async def _bind_spool(db_session, printer_id, ams_id, tray_id, spool):
    """Directly create a SpoolAssignment (no helper, no MQTT) so an adjudication test
    can start from a bound slot."""
    db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=printer_id, ams_id=ams_id, tray_id=tray_id))
    await db_session.commit()


async def _bind(db_session, spool, printer_id, ams_id, tray_id, *, origin="rfid_auto"):
    """Bind through the production writer and commit (callers own the commit)."""
    assignment = await bind_spool_to_slot(
        db_session,
        spool,
        printer_id=printer_id,
        ams_id=ams_id,
        tray_id=tray_id,
        fingerprint_color="112233FF",
        fingerprint_type="PLA",
        origin=origin,
    )
    await db_session.commit()
    return assignment


async def _rows_for_spool(db_session, spool_id) -> list[SpoolAssignment]:
    res = await db_session.execute(select(SpoolAssignment).where(SpoolAssignment.spool_id == spool_id))
    return list(res.scalars().all())


def _slot(row: SpoolAssignment) -> tuple[int, int, int]:
    return (row.printer_id, row.ams_id, row.tray_id)


async def _total_assignments(db_session) -> int:
    return await db_session.scalar(select(func.count(SpoolAssignment.id)))


def _info_messages(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == _BINDING_LOGGER and r.levelno == logging.INFO]


# -- S1: same-printer tray -> tray move (THE incident) ------------------------


@pytest.mark.asyncio
async def test_move_same_printer_tray_to_tray(db_session, printer_factory, caplog):
    """A roll re-detected on another tray of the SAME printer MOVED: the old row is
    swept, exactly one binding survives, and the move is logged from→to at INFO."""
    printer = await printer_factory()
    spool = await _fresh_spool(db_session)
    await _bind(db_session, spool, printer.id, 0, 0)

    with caplog.at_level(logging.INFO, logger=_BINDING_LOGGER):
        await _bind(db_session, spool, printer.id, 0, 1)

    rows = await _rows_for_spool(db_session, spool.id)
    assert len(rows) == 1, "a re-bind is a MOVE, not a copy"
    assert _slot(rows[0]) == (printer.id, 0, 1)
    assert await _total_assignments(db_session) == 1, "no stale row left anywhere in the table"

    expected = (
        f"spool {spool.id} moved: unbound from printer {printer.id} AMS0-T0 "
        f"-> printer {printer.id} AMS0-T1 (origin=rfid_auto)"
    )
    assert expected in _info_messages(caplog)


# -- S2: cross-printer move (the sweep carries no printer filter) -------------


@pytest.mark.asyncio
async def test_move_across_printers(db_session, printer_factory, caplog):
    """The sweep is fleet-wide: a roll carried to another PRINTER leaves no binding
    behind on the first one."""
    source = await printer_factory()
    destination = await printer_factory()
    spool = await _fresh_spool(db_session)
    await _bind(db_session, spool, source.id, 0, 0)

    with caplog.at_level(logging.INFO, logger=_BINDING_LOGGER):
        await _bind(db_session, spool, destination.id, 1, 2)

    rows = await _rows_for_spool(db_session, spool.id)
    assert len(rows) == 1
    assert _slot(rows[0]) == (destination.id, 1, 2)
    assert await _total_assignments(db_session) == 1

    expected = (
        f"spool {spool.id} moved: unbound from printer {source.id} AMS0-T0 "
        f"-> printer {destination.id} AMS1-T2 (origin=rfid_auto)"
    )
    assert expected in _info_messages(caplog)


# -- S3: plain bind of an unbound spool into an empty slot --------------------


@pytest.mark.asyncio
async def test_plain_bind_into_empty_slot_stamps_both(db_session, printer_factory, caplog):
    """Nothing to sweep and nothing to evict: one row is created and BOTH FIFO stamps
    are set (first_loaded_at write-once substrate, loaded_at the seating ordinal)."""
    printer = await printer_factory()
    spool = await _fresh_spool(db_session)
    assert spool.first_loaded_at is None and spool.loaded_at is None

    with caplog.at_level(logging.INFO, logger=_BINDING_LOGGER):
        await _bind(db_session, spool, printer.id, 0, 0)

    rows = await _rows_for_spool(db_session, spool.id)
    assert len(rows) == 1
    assert _slot(rows[0]) == (printer.id, 0, 0)

    await db_session.refresh(spool)
    assert spool.first_loaded_at is not None
    assert spool.loaded_at is not None
    assert _info_messages(caplog) == [], "nothing moved — a plain bind logs no move line"


# -- S4: same-spool same-slot upsert replay -----------------------------------


@pytest.mark.asyncio
async def test_same_slot_replay_replaces_row_without_restamping(db_session, printer_factory, caplog):
    """Re-detecting the SAME roll on the SAME slot replaces the row (delete+recreate
    upsert) but must NOT touch the stamps: the pairing did not change, so the seating
    order must not reset and first_loaded_at stays write-once. Nothing moved and nothing
    was displaced either, so the replay must stay silent."""
    printer = await printer_factory()
    spool = await _fresh_spool(db_session)
    first = await _bind(db_session, spool, printer.id, 0, 0)

    # Pin both stamps to a value no clock can produce, so any re-stamp is visible
    # without a sleep.
    spool.first_loaded_at = _SENTINEL
    spool.loaded_at = _SENTINEL
    await db_session.commit()

    with caplog.at_level(logging.INFO, logger=_BINDING_LOGGER):
        second = await _bind(db_session, spool, printer.id, 0, 0)

    assert _info_messages(caplog) == [], "a same-spool replay is a non-event — no move, no displacement line"

    rows = await _rows_for_spool(db_session, spool.id)
    assert len(rows) == 1
    assert _slot(rows[0]) == (printer.id, 0, 0)
    # Delete-then-recreate upsert (not an in-place mutation): the original row object
    # was deleted and the surviving row is the one this call created.
    assert second is not first
    assert sa_inspect(first).was_deleted is True
    assert rows[0] is second

    await db_session.refresh(spool)
    assert spool.loaded_at == _SENTINEL, "a same-spool replay must not reset the seating ordinal"
    assert spool.first_loaded_at == _SENTINEL, "first_loaded_at is write-once"


# -- slot upsert: a DIFFERENT spool takes over an occupied slot ---------------


@pytest.mark.asyncio
async def test_different_spool_onto_occupied_slot_unbinds_the_old_one(db_session, printer_factory, caplog):
    """Binding a different roll into an occupied slot evicts the incumbent (it becomes
    unbound — the slot holds one spool) and re-stamps the INCOMING spool's loaded_at,
    because the slot→spool pairing changed. The eviction is the one binding-state change
    with no destination, so it must leave its own INFO trail (a displacement line)."""
    printer = await printer_factory()
    incumbent = await _fresh_spool(db_session)
    incoming = await _fresh_spool(db_session)
    await _bind(db_session, incumbent, printer.id, 0, 0)

    # The incoming roll has been in service before (a real re-stamp, not a first stamp).
    incoming.loaded_at = _SENTINEL
    incoming.first_loaded_at = _SENTINEL
    await db_session.commit()

    with caplog.at_level(logging.INFO, logger=_BINDING_LOGGER):
        await _bind(db_session, incoming, printer.id, 0, 0, origin="manual_api")

    assert await _rows_for_spool(db_session, incumbent.id) == [], "the evicted spool is now unbound"
    rows = await _rows_for_spool(db_session, incoming.id)
    assert len(rows) == 1
    assert _slot(rows[0]) == (printer.id, 0, 0)
    assert await _total_assignments(db_session) == 1

    await db_session.refresh(incoming)
    assert incoming.loaded_at != _SENTINEL, "a pairing change re-stamps the seating ordinal"
    assert incoming.first_loaded_at == _SENTINEL, "first_loaded_at is write-once"

    expected = (
        f"spool {incumbent.id} displaced: unbound from printer {printer.id} AMS0-T0 "
        f"by spool {incoming.id} (origin=manual_api)"
    )
    # Exactly one line, and it is the displacement: the incoming roll was not bound
    # anywhere else, so nothing MOVED.
    assert _info_messages(caplog) == [expected]


# -- fail-loud: the DB refuses a duplicate binding ---------------------------


@pytest.mark.asyncio
async def test_raw_duplicate_binding_raises_integrity_error(db_session, printer_factory):
    """``ux_spool_assignment_spool_id`` (unique=True on the model) is what makes a write
    path that bypasses ``bind_spool_to_slot`` die loudly instead of silently forking a
    ledger. A raw second row for an already-bound spool must not reach the DB."""
    printer = await printer_factory()
    spool = await _fresh_spool(db_session)
    await _bind(db_session, spool, printer.id, 0, 0)
    spool_id, printer_id = spool.id, printer.id  # the rollback below expires both rows

    db_session.add(SpoolAssignment(spool_id=spool_id, printer_id=printer_id, ams_id=0, tray_id=1))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()

    rows = await _rows_for_spool(db_session, spool_id)
    assert len(rows) == 1
    assert _slot(rows[0]) == (printer_id, 0, 0)


# -- stamp_loaded_for_slot: presence-gain re-seat adjudication ---------------
# (relocated from test_spool_tag_matcher.py with the stamps themselves)


@pytest.mark.asyncio
async def test_stamp_loaded_for_slot_stamps_never_fed(db_session, printer_factory):
    """A never-fed bound row (weight_used < NEVER_FED_MAX_G, no tag, not spent) is
    re-stamped on a re-seat — it holds no consumption seniority (rule 2)."""
    printer = await printer_factory()
    spool = await _fresh_spool(db_session, weight_used=0.0)
    await _bind_spool(db_session, printer.id, 0, 0, spool)
    assert spool.loaded_at is None
    stamped = await stamp_loaded_for_slot(db_session, printer.id, 0, 0)
    assert stamped is True
    assert spool.loaded_at is not None


@pytest.mark.asyncio
async def test_stamp_loaded_for_slot_keeps_mid_life(db_session, printer_factory):
    """A mid-life bound row (has fed >= floor) keeps position on a re-seat — the
    dominant flow is maintenance of the SAME roll (rule 3). ANY absence, no stamp."""
    printer = await printer_factory()
    spool = await _fresh_spool(db_session, weight_used=400.0, loaded_at=None)
    await _bind_spool(db_session, printer.id, 0, 0, spool)
    stamped = await stamp_loaded_for_slot(db_session, printer.id, 0, 0)
    assert stamped is False
    assert spool.loaded_at is None


@pytest.mark.asyncio
async def test_stamp_loaded_for_slot_skips_spent(db_session, printer_factory):
    """A spent bound row is skipped — the spent latch owns the slot; its replacement
    mint stamps at bind time, not here (even though weight_used is 0)."""
    printer = await printer_factory()
    spool = await _fresh_spool(db_session, weight_used=0.0, spent_at=datetime.utcnow())
    await _bind_spool(db_session, printer.id, 0, 0, spool)
    stamped = await stamp_loaded_for_slot(db_session, printer.id, 0, 0)
    assert stamped is False
    assert spool.loaded_at is None


@pytest.mark.asyncio
async def test_stamp_loaded_for_slot_skips_tagged(db_session, printer_factory):
    """An RFID-tagged bound row is skipped — identity is adjudicated by the reconcile
    (a same-tag re-seat keeps position; a tag change re-binds and stamps there)."""
    printer = await printer_factory()
    spool = await _fresh_spool(db_session, weight_used=0.0, tag_uid="AABBCCDD11223344")
    await _bind_spool(db_session, printer.id, 0, 0, spool)
    stamped = await stamp_loaded_for_slot(db_session, printer.id, 0, 0)
    assert stamped is False
    assert spool.loaded_at is None


@pytest.mark.asyncio
async def test_stamp_loaded_for_slot_boundary_at_never_fed_max(db_session, printer_factory):
    """The boundary is exclusive: weight_used == NEVER_FED_MAX_G is 'has fed' (no
    stamp); just below it is never-fed (stamp)."""
    printer = await printer_factory()
    at_floor = await _fresh_spool(db_session, weight_used=NEVER_FED_MAX_G)
    below = await _fresh_spool(db_session, weight_used=NEVER_FED_MAX_G - 0.1)
    await _bind_spool(db_session, printer.id, 0, 0, at_floor)
    await _bind_spool(db_session, printer.id, 0, 1, below)
    assert await stamp_loaded_for_slot(db_session, printer.id, 0, 0) is False
    assert at_floor.loaded_at is None
    assert await stamp_loaded_for_slot(db_session, printer.id, 0, 1) is True
    assert below.loaded_at is not None


@pytest.mark.asyncio
async def test_stamp_loaded_for_slot_noops_unbound(db_session, printer_factory):
    """An unbound slot has nothing to adjudicate → no-op (False)."""
    printer = await printer_factory()
    assert await stamp_loaded_for_slot(db_session, printer.id, 3, 2) is False


def test_stamp_loaded_for_slot_takes_no_timing_input():
    """The adjudicator's signature carries NO duration/absence parameter — the decision
    is grams-state + identity only, never elapsed time (v3 semantics)."""
    import inspect

    params = list(inspect.signature(stamp_loaded_for_slot).parameters)
    assert params == ["db", "printer_id", "ams_id", "tray_id"]


def test_stamp_loaded_is_unconditional():
    """stamp_loaded always writes (callers own the churn guard) — unlike write-once
    stamp_first_loaded."""
    from types import SimpleNamespace

    s = SimpleNamespace(loaded_at="OLD")
    stamp_loaded(s)
    assert s.loaded_at != "OLD" and s.loaded_at is not None
