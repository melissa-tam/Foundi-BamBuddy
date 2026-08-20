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
  semantics (S4), slot-upsert replacement of a different spool, the RECLAIM lane's
  ``preserve_ordinal`` opt-out, and the MOVE damper.
* :func:`release_spool_from_slot` — the ONE unbind writer: last-location stamp +
  row deletion + the structured ``[slot-state]`` release line.
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

import backend.app.utils.retry_window as rw
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services import spool_binding
from backend.app.services.spool_binding import (
    NEVER_FED_MAX_G,
    OPERATOR_ORIGIN,
    bind_spool_to_slot,
    release_spool_from_slot,
    stamp_loaded,
    stamp_loaded_for_slot,
)

_BINDING_LOGGER = "backend.app.services.spool_binding"


@pytest.fixture(autouse=True)
def _fresh_move_damper():
    """Hand every test an un-armed move damper.

    The damper is a process-lifetime singleton keyed on ``spool.id`` alone, and each
    test's in-memory DB restarts ids at 1 — so without this a legitimate move in one
    test would silence the next test's move. Reset on BOTH sides so no test leaks a
    stamp in either direction.
    """
    spool_binding._move_damper.reset()
    yield
    spool_binding._move_damper.reset()


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


async def _bind(
    db_session,
    spool,
    printer_id,
    ams_id,
    tray_id,
    *,
    origin="rfid_auto",
    preserve_ordinal=False,
    bind_moment=None,
):
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
        preserve_ordinal=preserve_ordinal,
        bind_moment=bind_moment,
    )
    await db_session.commit()
    return assignment


async def _assignment_for_slot(db_session, printer_id, ams_id, tray_id) -> SpoolAssignment | None:
    res = await db_session.execute(
        select(SpoolAssignment).where(
            SpoolAssignment.printer_id == printer_id,
            SpoolAssignment.ams_id == ams_id,
            SpoolAssignment.tray_id == tray_id,
        )
    )
    return res.scalar_one_or_none()


def _last_location(spool: Spool) -> tuple:
    return (
        spool.last_location_printer_id,
        spool.last_location_ams_id,
        spool.last_location_tray_id,
    )


async def _rows_for_spool(db_session, spool_id) -> list[SpoolAssignment]:
    res = await db_session.execute(select(SpoolAssignment).where(SpoolAssignment.spool_id == spool_id))
    return list(res.scalars().all())


def _slot(row: SpoolAssignment) -> tuple[int, int, int]:
    return (row.printer_id, row.ams_id, row.tray_id)


async def _total_assignments(db_session) -> int:
    return await db_session.scalar(select(func.count(SpoolAssignment.id)))


def _info_messages(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == _BINDING_LOGGER and r.levelno == logging.INFO]


def _warning_messages(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == _BINDING_LOGGER and r.levelno == logging.WARNING]


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


# -- release_spool_from_slot: the ONE unbind writer ---------------------------


@pytest.mark.asyncio
async def test_release_stamps_last_location_deletes_the_row_and_logs(db_session, printer_factory, caplog):
    """An assignment is a LOCATION claim (doctrine rule 9): releasing it frees the slot,
    keeps the roll's grams, and leaves the two artefacts the reclaim lane and the
    forensics lane depend on — the ``last_location_*`` stamp and one ``[slot-state]``
    release line naming the reason."""
    printer = await printer_factory()
    spool = await _fresh_spool(db_session, weight_used=412.5)
    await _bind(db_session, spool, printer.id, 2, 3)
    assignment = await _assignment_for_slot(db_session, printer.id, 2, 3)
    assert _last_location(spool) == (None, None, None), "never released yet"

    with caplog.at_level(logging.INFO, logger=_BINDING_LOGGER):
        await release_spool_from_slot(db_session, assignment, reason="operator_clear")
    await db_session.commit()

    assert await _assignment_for_slot(db_session, printer.id, 2, 3) is None
    assert await _rows_for_spool(db_session, spool.id) == [], "the roll is unbound inventory now"

    await db_session.refresh(spool)
    assert _last_location(spool) == (printer.id, 2, 3), "the departed slot is stamped on the row"
    assert spool.last_location_at is not None
    assert spool.weight_used == 412.5, "grams live on the spool row — a release costs no history"

    expected = f"[slot-state] printer={printer.id} A2T3 release spool={spool.id} reason=operator_clear"
    assert expected in _info_messages(caplog)


@pytest.mark.asyncio
async def test_release_frees_the_slot_even_without_a_spool_row(db_session, printer_factory, caplog):
    """A binding whose spool row is gone (hand-delete / cascade race) is a bogus location
    claim either way: the slot is still freed and still logged — only the stamp is
    skipped, because there is nothing to stamp."""
    printer = await printer_factory()
    bystander = await _fresh_spool(db_session)
    db_session.add(SpoolAssignment(spool_id=999_999, printer_id=printer.id, ams_id=0, tray_id=1))
    await db_session.commit()
    assignment = await _assignment_for_slot(db_session, printer.id, 0, 1)

    with caplog.at_level(logging.INFO, logger=_BINDING_LOGGER):
        await release_spool_from_slot(db_session, assignment, reason="cleared_tray")
    await db_session.commit()

    assert await _assignment_for_slot(db_session, printer.id, 0, 1) is None
    assert f"[slot-state] printer={printer.id} A0T1 release spool=999999 reason=cleared_tray" in _info_messages(caplog)
    await db_session.refresh(bystander)
    assert _last_location(bystander) == (None, None, None), "no row to stamp — and none invented"


@pytest.mark.asyncio
async def test_release_clears_a_pending_fresh_roll_prompt_and_reports_it(db_session, printer_factory, caplog):
    """A pending fresh-roll prompt asks the operator about the roll in ONE named slot, so
    the release that ends that roll's location claim also ends the question — and RETURNS
    the fact, because the cross-client toast dismissal is the caller's to send.

    This is a deliberate reversal of the replay lane's "skip stale rows without mutating
    them" rule (``spool_tagless.pending_fresh_prompts``): nothing durable is lost, because
    the prompt is a PER-CYCLE contract — any roll returning to that slot raises a qualified
    physical cycle, which re-stamps and re-asks (2026-08-07: a stale stamp kept replaying a
    toast for a slot whose roll had left days earlier)."""
    printer = await printer_factory()
    spool = await _fresh_spool(db_session, fresh_prompt_pending_at=datetime.utcnow())
    await _bind(db_session, spool, printer.id, 0, 1)
    assignment = await _assignment_for_slot(db_session, printer.id, 0, 1)

    with caplog.at_level(logging.INFO, logger=_BINDING_LOGGER):
        prompt_cleared = await release_spool_from_slot(db_session, assignment, reason="cleared_tray")
    await db_session.commit()

    assert prompt_cleared is True
    await db_session.refresh(spool)
    assert spool.fresh_prompt_pending_at is None
    assert _last_location(spool) == (printer.id, 0, 1), "the location residue is unaffected"
    line = next(m for m in _info_messages(caplog) if "release spool=" in m)
    assert line.endswith("reason=cleared_tray (fresh-roll prompt cleared)")


@pytest.mark.asyncio
async def test_release_reports_no_clear_when_no_prompt_was_pending(db_session, printer_factory, caplog):
    """The signal is a FACT about this release, not a habit: an unstamped row reports
    False, so a caller can never emit a dismissal for a toast that never existed."""
    printer = await printer_factory()
    spool = await _fresh_spool(db_session)
    await _bind(db_session, spool, printer.id, 0, 1)
    assignment = await _assignment_for_slot(db_session, printer.id, 0, 1)

    with caplog.at_level(logging.INFO, logger=_BINDING_LOGGER):
        prompt_cleared = await release_spool_from_slot(db_session, assignment, reason="cleared_tray")
    await db_session.commit()

    assert prompt_cleared is False
    assert f"[slot-state] printer={printer.id} A0T1 release spool={spool.id} reason=cleared_tray" in _info_messages(
        caplog
    )


@pytest.mark.asyncio
async def test_release_without_a_spool_row_reports_no_clear(db_session, printer_factory):
    """No row, no stamp to clear — the orphan path answers honestly instead of guessing."""
    printer = await printer_factory()
    db_session.add(SpoolAssignment(spool_id=999_999, printer_id=printer.id, ams_id=0, tray_id=1))
    await db_session.commit()
    assignment = await _assignment_for_slot(db_session, printer.id, 0, 1)

    assert await release_spool_from_slot(db_session, assignment, reason="orphaned_assignment") is False


# -- the sweep is a release too: last-location stamped on both branches --------


@pytest.mark.asyncio
async def test_move_stamps_the_last_location_of_the_slot_left_behind(db_session, printer_factory):
    """A MOVE is also a release of the old slot, so the sweep stamps the OLD triple —
    not the new one. Without this a roll moved (rather than cleanly released) would
    carry no reclaim hint for the slot it actually vacated."""
    source = await printer_factory()
    destination = await printer_factory()
    spool = await _fresh_spool(db_session)
    await _bind(db_session, spool, source.id, 0, 2)

    await _bind(db_session, spool, destination.id, 1, 3)

    await db_session.refresh(spool)
    assert _last_location(spool) == (source.id, 0, 2), "the VACATED slot is the last location"
    assert spool.last_location_at is not None


@pytest.mark.asyncio
async def test_displacement_stamps_the_evicted_spools_last_location(db_session, printer_factory):
    """The incumbent evicted from an occupied slot goes unbound fleet-wide — the one
    binding-state change with no destination — so it gets the stamp that lets it
    reclaim its grams if it is re-inserted."""
    printer = await printer_factory()
    incumbent = await _fresh_spool(db_session)
    incoming = await _fresh_spool(db_session)
    await _bind(db_session, incumbent, printer.id, 0, 0)

    await _bind(db_session, incoming, printer.id, 0, 0)

    await db_session.refresh(incumbent)
    await db_session.refresh(incoming)
    assert _last_location(incumbent) == (printer.id, 0, 0)
    assert incumbent.last_location_at is not None
    assert _last_location(incoming) == (None, None, None), "the arriving roll released nothing"


@pytest.mark.asyncio
async def test_same_slot_replay_stamps_no_last_location(db_session, printer_factory):
    """A same-spool same-slot upsert replay is a non-event: the roll never left, so
    stamping a last location would fabricate a departure that never happened."""
    printer = await printer_factory()
    spool = await _fresh_spool(db_session)
    await _bind(db_session, spool, printer.id, 0, 0)

    await _bind(db_session, spool, printer.id, 0, 0)

    await db_session.refresh(spool)
    assert _last_location(spool) == (None, None, None)
    assert spool.last_location_at is None


# -- preserve_ordinal: the RECLAIM lane keeps FIFO position (rule 7) ----------


@pytest.mark.asyncio
async def test_preserve_ordinal_skips_the_seating_restamp(db_session, printer_factory):
    """A pulled-and-returned roll is the SAME roll: rebinding it must not reset its
    seating order just because the binding row was rebuilt (doctrine rule 7 — a
    mid-life re-seat keeps position). first_loaded_at stays write-once as always."""
    printer = await printer_factory()
    incumbent = await _fresh_spool(db_session)
    returning = await _fresh_spool(db_session)
    await _bind(db_session, incumbent, printer.id, 0, 0)  # slot occupied → a real pairing change
    returning.loaded_at = _SENTINEL
    returning.first_loaded_at = _SENTINEL
    await db_session.commit()

    await _bind(db_session, returning, printer.id, 0, 0, preserve_ordinal=True)

    await db_session.refresh(returning)
    assert returning.loaded_at == _SENTINEL, "a reclaim keeps FIFO position"
    assert returning.first_loaded_at == _SENTINEL


@pytest.mark.asyncio
async def test_pairing_change_restamps_without_preserve_ordinal(db_session, printer_factory):
    """The default is unchanged — the opt-out is explicit, never implicit."""
    printer = await printer_factory()
    incumbent = await _fresh_spool(db_session)
    incoming = await _fresh_spool(db_session)
    await _bind(db_session, incumbent, printer.id, 0, 0)
    incoming.loaded_at = _SENTINEL
    await db_session.commit()

    await _bind(db_session, incoming, printer.id, 0, 0)

    await db_session.refresh(incoming)
    assert incoming.loaded_at != _SENTINEL


# -- bind_moment: a de-bounce re-states a binding, it does not begin one ------


@pytest.mark.asyncio
async def test_bind_moment_is_carried_onto_the_rebuilt_row(db_session, printer_factory):
    """``SpoolAssignment.created_at`` is read as "an unobserved swap happened at THIS
    instant" (``spool_tagless.reconcile_ledger_overcharges``), so a lane that has just
    certified that NOTHING physically happened must hand the old moment back rather than
    stamp a new one. The writer carries whatever it is given, verbatim."""
    printer = await printer_factory()
    returning = await _fresh_spool(db_session)

    assignment = await _bind(db_session, returning, printer.id, 0, 0, bind_moment=_SENTINEL)

    assert assignment.created_at == _SENTINEL
    await db_session.refresh(assignment)
    assert assignment.created_at == _SENTINEL, "and it survives the round trip, not just the flush"


@pytest.mark.asyncio
async def test_an_ordinary_bind_still_stamps_its_own_moment(db_session, printer_factory):
    """The liveness half: omitting ``bind_moment`` leaves the column's server default in
    charge, which is what every genuine bind — an identity read, a mint, an operator
    assign — depends on to give the reconciler a real boundary to adjudicate across."""
    printer = await printer_factory()
    arriving = await _fresh_spool(db_session)

    assignment = await _bind(db_session, arriving, printer.id, 0, 0)

    await db_session.refresh(assignment)
    assert assignment.created_at is not None
    assert assignment.created_at != _SENTINEL


# -- the MOVE damper (007-H2C spool-194 flip-flop storm) ----------------------


@pytest.mark.asyncio
async def test_second_move_inside_the_window_is_damped(db_session, printer_factory, caplog):
    """The storm shape: one roll presented on two trays flips the binding back and forth,
    each pass a delete+insert plus a FIFO rewrite (spool 194: 51 moves in 5 m 21 s). The
    second move inside the window is REFUSED — None returned, DB untouched, one WARNING."""
    printer = await printer_factory()
    spool = await _fresh_spool(db_session)
    await _bind(db_session, spool, printer.id, 1, 0)
    await _bind(db_session, spool, printer.id, 1, 1)  # first move: allowed, arms the window
    spool.loaded_at = _SENTINEL
    await db_session.commit()

    with caplog.at_level(logging.WARNING, logger=_BINDING_LOGGER):
        damped = await _bind(db_session, spool, printer.id, 1, 0)  # the flip back

    assert damped is None, "a damped bind reports that nothing happened"
    rows = await _rows_for_spool(db_session, spool.id)
    assert len(rows) == 1 and _slot(rows[0]) == (printer.id, 1, 1), "the binding did not move"
    await db_session.refresh(spool)
    assert spool.loaded_at == _SENTINEL, "and the FIFO ordinal was not rewritten"

    warnings = _warning_messages(caplog)
    assert len(warnings) == 1
    assert warnings[0].startswith(f"[slot-state] damped move: spool {spool.id} -> printer {printer.id} A1T0 ")
    assert "within 10s" in warnings[0]


@pytest.mark.asyncio
async def test_first_bind_is_never_damped(db_session, printer_factory, caplog):
    """A first bind is not a move: there is no other slot to flip from, and refusing it
    would simply lose a state change. Two different rolls binding back-to-back both land."""
    printer = await printer_factory()
    first = await _fresh_spool(db_session)
    second = await _fresh_spool(db_session)

    with caplog.at_level(logging.WARNING, logger=_BINDING_LOGGER):
        assert await _bind(db_session, first, printer.id, 0, 0) is not None
        assert await _bind(db_session, second, printer.id, 0, 1) is not None

    assert _warning_messages(caplog) == []
    assert await _total_assignments(db_session) == 2


@pytest.mark.asyncio
async def test_same_slot_rebind_is_never_damped(db_session, printer_factory, caplog):
    """A re-detect of the roll already seated is not a move either — the upsert replay
    must keep working at wire cadence."""
    printer = await printer_factory()
    spool = await _fresh_spool(db_session)
    await _bind(db_session, spool, printer.id, 0, 0)

    with caplog.at_level(logging.WARNING, logger=_BINDING_LOGGER):
        for _ in range(3):
            assert await _bind(db_session, spool, printer.id, 0, 0) is not None

    assert _warning_messages(caplog) == []
    rows = await _rows_for_spool(db_session, spool.id)
    assert len(rows) == 1 and _slot(rows[0]) == (printer.id, 0, 0)


@pytest.mark.asyncio
async def test_operator_move_is_never_damped(db_session, printer_factory, caplog):
    """An operator's explicit assign is a statement of fact, not a wire observation, so
    it always wins — even immediately after a wire move of the same roll. This is the
    ONE behaviour ``origin`` changes."""
    printer = await printer_factory()
    spool = await _fresh_spool(db_session)
    await _bind(db_session, spool, printer.id, 0, 0)
    await _bind(db_session, spool, printer.id, 0, 1)  # wire move arms the window

    with caplog.at_level(logging.WARNING, logger=_BINDING_LOGGER):
        assignment = await _bind(db_session, spool, printer.id, 0, 2, origin=OPERATOR_ORIGIN)

    assert assignment is not None
    assert _warning_messages(caplog) == []
    rows = await _rows_for_spool(db_session, spool.id)
    assert len(rows) == 1 and _slot(rows[0]) == (printer.id, 0, 2)


@pytest.mark.asyncio
async def test_move_is_allowed_again_once_the_window_elapses(db_session, printer_factory):
    """The damper is a damper, not a lock: a genuine later move still lands. Clock
    injected (the fork's RetryWindow reads the module-level monotonic at call time)."""
    printer = await printer_factory()
    spool = await _fresh_spool(db_session)
    await _bind(db_session, spool, printer.id, 0, 0)
    await _bind(db_session, spool, printer.id, 0, 1)
    assert await _bind(db_session, spool, printer.id, 0, 2) is None, "inside the window"

    original = rw.monotonic
    rw.monotonic = lambda: original() + spool_binding._MOVE_DAMPER_S + 1
    try:
        assert await _bind(db_session, spool, printer.id, 0, 2) is not None
    finally:
        rw.monotonic = original

    rows = await _rows_for_spool(db_session, spool.id)
    assert len(rows) == 1 and _slot(rows[0]) == (printer.id, 0, 2)


@pytest.mark.asyncio
async def test_damped_move_writes_no_last_location(db_session, printer_factory):
    """A refused move must be a genuine no-op: the sweep never runs, so the roll's
    last-location residue is not touched either."""
    printer = await printer_factory()
    spool = await _fresh_spool(db_session)
    await _bind(db_session, spool, printer.id, 0, 0)
    await _bind(db_session, spool, printer.id, 0, 1)
    await db_session.refresh(spool)
    stamped_before = (_last_location(spool), spool.last_location_at)

    assert await _bind(db_session, spool, printer.id, 0, 0) is None

    await db_session.refresh(spool)
    assert (_last_location(spool), spool.last_location_at) == stamped_before
