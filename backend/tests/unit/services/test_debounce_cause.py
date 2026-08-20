"""The de-bounce lane's CAUSE test, driven through the real pipeline (2026-08-19, shape 32).

`test_slot_state.py` pins the decision TABLE's arms by forcing `runout_suspect` /
`reseat_within_window` directly. That proves the table branches correctly and proves nothing
about whether the pipeline ever computes those booleans as True — and a predicate that never
fires is indistinguishable from one that is working, which is the exact failure class this
whole wave exists to close (memory `liveness-paired-verification`).

So these cases drive `run_slot_pipeline` and seed only what the WIRE would have produced:
the presence lane's loss→gain ledger and, for the post-HMS arm, a real `PrinterIncident` row.

Scenario contract: `bambu-ams-behavior/resources/spool-subsystem.md` §4.1 rows T1, T7, T8.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select

from backend.app.models.printer_incident import KIND_JAM, KIND_RUNOUT, STATUS_ESCALATED
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services import ams_presence, printer_incidents, slot_pipeline, spool_tagless
from backend.app.services.slot_pipeline import PipelineDeps, run_slot_pipeline
from backend.app.services.slot_state import DecisionKind
from backend.app.services.tray_observation import observe_tray

_PETG = {"state": 11, "tray_type": "PETG", "tray_color": "000000FF"}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    async def fake_get_setting(_db, _key):
        return None

    monkeypatch.setattr("backend.app.api.routes.settings.get_setting", fake_get_setting)
    monkeypatch.setattr(spool_tagless.printer_manager, "get_status", lambda pid: None)
    monkeypatch.setattr(spool_tagless.printer_manager, "get_client", lambda pid: None)
    slot_pipeline._reset_state()
    spool_tagless._reset_state()
    ams_presence._reset_state()
    yield
    slot_pipeline._reset_state()
    spool_tagless._reset_state()
    ams_presence._reset_state()


async def _departed_roll(db, printer_id, ams_id, tray_id, *, weight_used=940.0):
    """A tagless row that was released FROM this slot and is not yet spent.

    This is the exact residue the ~3-min bay-clear→HMS gap leaves behind: the binding is
    already gone, but the runout has not been declared, so nothing has stamped `spent_at`
    and the `spent_at IS NULL` donor filter cannot exclude it (shape 32, Path B).
    """
    spool = Spool(
        material="PETG",
        rgba="000000FF",
        label_weight=1000,
        weight_used=weight_used,
        data_origin="ams_auto",
        last_location_printer_id=printer_id,
        last_location_ams_id=ams_id,
        last_location_tray_id=tray_id,
        last_location_at=datetime.utcnow(),
    )
    db.add(spool)
    await db.commit()
    await db.refresh(spool)
    return spool


def _obs(printer_id, tray_id):
    return observe_tray(printer_id, 0, {"id": tray_id, **_PETG})


def _seed_gain(printer_id, ams_id, tray_id, *, absent_for, under_active_feed=False):
    """What the presence lane records at a gain: the measured absence, and whether the slot
    was the ACTIVE FEEDER of a live print when its presence was lost."""
    ams_presence._reseat[(printer_id, ams_id, tray_id)] = ams_presence._Reseat(absent_for, under_active_feed)


async def _open_incident(db, printer_id, *, kind, global_tray, code="0700_8011"):
    """The durable fault row the recovery machine opens, created through the STORE'S OWN
    entry point rather than by hand.

    The post-HMS half of the cause test reads this row, not a transient in-memory flag —
    which is why a hold that outlives a deploy still disqualifies the slot. Going through
    ``open_new`` also means these cases exercise the same one-open-incident-per-printer
    invariant production does, instead of a hand-built row that could drift from it.
    """
    row = await printer_incidents.open_new(
        db,
        printer_id=printer_id,
        job_id="job-A",
        item_id=None,
        kind=kind,
        code=code,
        codes=code,
        slot_global_tray=global_tray,
        status=STATUS_ESCALATED,
    )
    await db.commit()
    return row


async def _no_settings(_key):
    """Every setting unset — the de-bounce lane reads none of them, and an unset
    ``tagless_default_filament`` keeps the mint arm on its tray-derived identity."""
    return None


def _deps(db):
    return PipelineDeps(db=db, get_setting=_no_settings)


@pytest.mark.asyncio
async def test_a_short_uncaused_absence_still_de_bounces(db_session, printer_factory):
    """T1 — the liveness arm. The lane's ONE real job must still fire end to end.

    Deliberately first in the file: every case below asserts a MINT, and a suite of
    absence assertions is exactly how the 2026-08-07 wave shipped a starved deadlock
    that its own metrics called a cured storm.
    """
    printer = await printer_factory()
    donor = await _departed_roll(db_session, printer.id, 0, 3)
    _seed_gain(printer.id, 0, 3, absent_for=25.0)

    transitions = await run_slot_pipeline(printer.id, [_obs(printer.id, 3)], _deps(db_session))

    assert transitions[0].decision.kind is DecisionKind.RECLAIM
    assert transitions[0].decision.reason == "reseat_debounce"
    assert transitions[0].decision.spool_id == donor.id
    await db_session.refresh(donor)
    assert donor.weight_used == 940.0, "the de-bounced roll keeps its grams"
    assert await db_session.scalar(select(Spool.id).where(Spool.id != donor.id)) is None, "no row was minted"


@pytest.mark.asyncio
async def test_a_refill_inside_the_gap_mints_when_the_slot_was_feeding(db_session, printer_factory):
    """T7 — the hole this wave exists to close, and the case scoping ALONE made worse.

    The AMS clears a drained slot's exist bit ~3 min BEFORE it declares the runout, so the
    departed row is released but not yet spent and the `spent_at` filter is blind. The
    absence is SHORT (the operator refilled promptly), so the window alone would de-bounce
    an exhausted row onto a brand-new roll. The loss-edge feeder evidence is what refuses it.
    """
    printer = await printer_factory()
    donor = await _departed_roll(db_session, printer.id, 0, 2)
    _seed_gain(printer.id, 0, 2, absent_for=90.0, under_active_feed=True)

    transitions = await run_slot_pipeline(printer.id, [_obs(printer.id, 2)], _deps(db_session))

    assert transitions[0].decision.kind is DecisionKind.MINT
    assert transitions[0].decision.reason == "runout_suspect_mint"
    await db_session.refresh(donor)
    assert donor.weight_used == 940.0, "the exhausted row is left alone, not re-charged"
    minted = await db_session.scalar(select(Spool).where(Spool.id != donor.id))
    assert minted is not None and (minted.weight_used or 0) == 0, "the fresh roll gets a fresh ledger"


@pytest.mark.asyncio
async def test_a_refill_on_the_demanded_slot_mints_after_the_hms(db_session, printer_factory):
    """T8 — mid-print runout, the AMS demands slot N, the operator refills slot N.

    The post-HMS arm: once the runout is declared there is an OPEN incident naming the slot,
    which is definitive regardless of what the loss edge managed to observe.
    """
    printer = await printer_factory()
    await _departed_roll(db_session, printer.id, 0, 1)
    _seed_gain(printer.id, 0, 1, absent_for=120.0)  # short, and NO feeder evidence
    await _open_incident(db_session, printer.id, kind=KIND_RUNOUT, global_tray=0 * 4 + 1)

    transitions = await run_slot_pipeline(printer.id, [_obs(printer.id, 1)], _deps(db_session))

    assert transitions[0].decision.kind is DecisionKind.MINT
    assert transitions[0].decision.reason == "runout_suspect_mint"


@pytest.mark.asyncio
async def test_a_runout_on_a_DIFFERENT_slot_does_not_disqualify_this_one(db_session, printer_factory):
    """The over-suspicion guard, and the reason the incident is matched PER SLOT.

    A blanket per-printer test would be the tempting simplification, and it is unsafe in the
    expensive direction: a genuine glitch on slot 3 while slot 1 is held for a runout would
    mint, resetting a part-used roll to label weight and walking it through the start floor.
    """
    printer = await printer_factory()
    donor = await _departed_roll(db_session, printer.id, 0, 3)
    _seed_gain(printer.id, 0, 3, absent_for=25.0)
    await _open_incident(db_session, printer.id, kind=KIND_RUNOUT, global_tray=0 * 4 + 1)  # slot 1, not 3

    transitions = await run_slot_pipeline(printer.id, [_obs(printer.id, 3)], _deps(db_session))

    assert transitions[0].decision.kind is DecisionKind.RECLAIM
    assert transitions[0].decision.spool_id == donor.id


@pytest.mark.asyncio
async def test_a_JAM_incident_on_this_slot_does_not_disqualify_it(db_session, printer_factory):
    """Only a RUNOUT release is never a glitch (operator ruling 15). A jam hold is a
    different fault class — its slot may well have been pulled and re-seated by hand while
    clearing, which is the very case the de-bounce exists for."""
    printer = await printer_factory()
    donor = await _departed_roll(db_session, printer.id, 0, 2)
    _seed_gain(printer.id, 0, 2, absent_for=40.0)
    await _open_incident(db_session, printer.id, kind=KIND_JAM, global_tray=0 * 4 + 2)

    transitions = await run_slot_pipeline(printer.id, [_obs(printer.id, 2)], _deps(db_session))

    assert transitions[0].decision.kind is DecisionKind.RECLAIM
    assert transitions[0].decision.spool_id == donor.id


@pytest.mark.asyncio
async def test_an_unmeasured_absence_mints(db_session, printer_factory):
    """T11 — a restart landed while the roll was out, so no loss edge was ever observed.

    `None` is UNKNOWN, never "short". The farm asserts nothing about a roll it did not watch
    leave, and the early-runout correction is what makes that safe rather than lossy.
    """
    printer = await printer_factory()
    await _departed_roll(db_session, printer.id, 0, 0)
    _seed_gain(printer.id, 0, 0, absent_for=None)

    transitions = await run_slot_pipeline(printer.id, [_obs(printer.id, 0)], _deps(db_session))

    assert transitions[0].decision.kind is DecisionKind.MINT
    assert transitions[0].decision.reason == "tagless_mint"


@pytest.mark.asyncio
async def test_no_gain_record_at_all_mints(db_session, printer_factory):
    """The slot was never seen to lose presence in this process's lifetime — the shape the
    three-days-empty incident actually presented after a restart. Structurally a mint."""
    printer = await printer_factory()
    await _departed_roll(db_session, printer.id, 0, 0)

    transitions = await run_slot_pipeline(printer.id, [_obs(printer.id, 0)], _deps(db_session))

    assert transitions[0].decision.kind is DecisionKind.MINT
    assert transitions[0].decision.reason == "tagless_mint"


@pytest.mark.asyncio
async def test_a_tagged_donor_is_never_de_bounced(db_session, printer_factory):
    """G6/G1 — doctrine rule 11: a roll that can speak for itself is never spoken for by a
    breadcrumb. Pinned with the SIBLING chip only, because that is the case a two-column
    tag-ness test silently misclassifies (spool 250 reached 1246 g that way)."""
    printer = await printer_factory()
    donor = await _departed_roll(db_session, printer.id, 0, 3)
    donor.sibling_tag_uid = "C93B67FE00000100"
    await db_session.commit()
    _seed_gain(printer.id, 0, 3, absent_for=20.0)

    transitions = await run_slot_pipeline(printer.id, [_obs(printer.id, 3)], _deps(db_session))

    assert transitions[0].decision.kind is DecisionKind.MINT
    await db_session.refresh(donor)
    assert donor.weight_used == 940.0
    assert (
        await db_session.scalar(select(SpoolAssignment.spool_id).where(SpoolAssignment.printer_id == printer.id))
    ) != donor.id
