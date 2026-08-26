"""The de-bounce lane's CAUSE test, driven through the real pipeline (2026-08-19, shape 32).

`test_slot_state.py` pins the decision TABLE's arms by forcing `runout_suspect` /
`reseat_within_window` directly. That proves the table branches correctly and proves nothing
about whether the pipeline ever computes those booleans as True — and a predicate that never
fires is indistinguishable from one that is working, which is the exact failure class this
whole wave exists to close (memory `liveness-paired-verification`).

So these cases drive `run_slot_pipeline` and seed only what the WIRE would have produced:
the presence lane's loss→gain ledger and, for the post-HMS arm, a real `PrinterIncident` row.

Scenario contract: `bambu-ams-behavior/resources/spool-subsystem.md` §4.1 rows T1, T4, T7, T8.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from backend.app.models.printer_incident import KIND_JAM, KIND_RUNOUT, STATUS_ESCALATED
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services import ams_presence, printer_incidents, slot_pipeline, spool_tagless
from backend.app.services.bambu_mqtt import HMSError
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


async def _departed_roll(db, printer_id, ams_id, tray_id, *, weight_used=940.0, released_at=None, spent=False):
    """A tagless row that was released FROM this slot and is not yet spent.

    This is the exact residue the ~3-min bay-clear→HMS gap leaves behind: the binding is
    already gone, but the runout has not been declared, so nothing has stamped `spent_at`
    and the `spent_at IS NULL` donor filter cannot exclude it (shape 32, Path B).

    ``released_at`` and ``spent`` exist for the two-residue cases: a slot that has released
    more than one roll over its life is the ordinary shape, and the ORDER of those releases
    is the whole question the adjudication answers.
    """
    spool = Spool(
        material="PETG",
        rgba="000000FF",
        label_weight=1000,
        weight_used=weight_used,
        data_origin="ams_auto",
        spent_at=datetime.utcnow() if spent else None,
        last_location_printer_id=printer_id,
        last_location_ams_id=ams_id,
        last_location_tray_id=tray_id,
        last_location_at=released_at if released_at is not None else datetime.utcnow(),
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


def _deps(db, client=None):
    return PipelineDeps(db=db, client=client, get_setting=_no_settings)


# --- the WIRE-DRIVEN half (no ledger seeding at all) -------------------------
#
# Everything above states the loss→gain pair via ``_seed_gain``, which pins the TABLE's
# arms. Everything below derives it, because the two are not the same claim: on
# 2026-08-20 the release-evidence record was found reading the wrong one of
# ``ams_presence``'s two feeder ledgers, and every seeded test agreed with it. A
# predicate that never fires is indistinguishable from one that works (memory
# ``liveness-paired-verification``), so the cause test owes a probe that drives
# ``ams_presence.on_tray_observations`` — the production entry ``printer_manager`` calls
# one pass ahead of the pipeline — and lets the stamps be written by the code under test.


def _state(*, running=True, last_loaded_tray=-1, tray_now=255, hms=(), snow=None):
    """The printer state the presence pass and the pipeline read at an edge."""
    return SimpleNamespace(
        state="RUNNING" if running else "IDLE",
        ams_status_main=0,
        last_loaded_tray=last_loaded_tray,
        tray_now=tray_now,
        hms_errors=list(hms),
        h2d_extruder_snow=dict(snow or {}),
        raw_data={},
    )


def _patch_wire(monkeypatch, state, *, dual=False):
    """Point BOTH wire readers at ``state``; ``dual`` makes the client a Vortek machine."""
    client = SimpleNamespace(
        state=state,
        is_dual_nozzle=dual,
        ams_unit_drying=lambda _ams_id: False,
        ams_write_refusal=lambda _ams_id: None,
    )
    monkeypatch.setattr(ams_presence.printer_manager, "get_status", lambda pid: state)
    monkeypatch.setattr(ams_presence.printer_manager, "get_client", lambda pid: client)
    return client


async def _wire_cycle(db, printer_id, ams_id, tray_id):
    """A REAL present→absent→present edge pair on one slot, through the presence pass.

    Three pushes, because the first batch per printer only SEEDS (a refill done while the
    server was down must not read as a gain). The absence is sub-second, so it is measured
    and inside the de-bounce window while staying well under the 5 s flap filter — which is
    the same shape the fleet's spurious releases have, and the reason the lane exists.
    """
    seated = observe_tray(printer_id, ams_id, {"id": tray_id, **_PETG})
    cleared = observe_tray(printer_id, ams_id, {"id": tray_id, "state": 9, "tray_type": ""})
    await ams_presence.on_tray_observations(printer_id, [seated], db)
    await ams_presence.on_tray_observations(printer_id, [cleared], db)
    await ams_presence.on_tray_observations(printer_id, [seated], db)
    # Step the GAIN CLOCK past the F1 mint-settle window (``_MINT_SETTLE_S``), which defers
    # any fresh mint for 5 s so the firmware's own post-insert RFID read can land first.
    # Production pays that with one more push; a test cannot sleep it out. This moves a
    # TIMESTAMP and nothing else — no reseat entry, no feeder stamp, no cycle is invented,
    # so every fact the cause test reads is still one the code under test derived.
    key = (printer_id, ams_id, tray_id)
    ams_presence._gain_at[key] -= spool_tagless._MINT_SETTLE_S + 1.0
    return seated


def _hms(attr: int, code: int) -> HMSError:
    return HMSError(
        code=hex(code), attr=attr, module=(attr >> 24) & 0xFF, severity=2, full_code=f"{attr:08X}{code:08X}"
    )


def _demand(ams_id: int, tray_id: int) -> HMSError:
    """``0x00020001`` — "AMS A Slot N filament has run out. Please insert a new filament."

    The firmware's own standing, slot-attributed ask. Admitted as runout SUSPICION and
    deliberately never as spent evidence: the cost of a false suspicion is a fresh ledger
    row at label weight, the cost of a false stamp is a permanently archived roll.
    """
    return _hms(0x07000000 | (ams_id << 16) | ((0x20 + tray_id) << 8), 0x00020001)


@pytest.mark.asyncio
async def test_a_live_feeder_losing_presence_mints_through_the_real_presence_lane(
    db_session, printer_factory, monkeypatch
):
    """T7 LIVENESS: the feeder arm, with the loss-edge stamp DERIVED rather than stated.

    The printer is RUNNING and the wire says this slot is the tray that last actually fed
    the job; the bay empties and the operator refills it seconds later. Nothing seeds
    ``_reseat`` or ``_absent_under_active_feed`` — the presence pass writes them from the
    same pushes the pipeline then resolves, exactly as production orders the two.
    """
    printer = await printer_factory()
    donor = await _departed_roll(db_session, printer.id, 0, 2)
    _patch_wire(monkeypatch, _state(last_loaded_tray=2, tray_now=2))

    seated = await _wire_cycle(db_session, printer.id, 0, 2)
    transitions = await run_slot_pipeline(printer.id, [seated], _deps(db_session))

    assert transitions[0].decision.kind is DecisionKind.MINT
    assert transitions[0].decision.reason == "runout_suspect_mint"
    await db_session.refresh(donor)
    assert donor.weight_used == 940.0, "the exhausted row is left alone, not re-charged"


@pytest.mark.asyncio
async def test_a_slot_that_was_not_feeding_still_de_bounces_through_the_real_presence_lane(
    db_session, printer_factory, monkeypatch
):
    """The negative half of the same probe — and the one that keeps it honest.

    Identical wire shape, identical timing, live print: the ONLY difference is that the
    job's feeder is a different slot. Without this case a feeder test that answered "yes"
    for everything would pass just as loudly.
    """
    printer = await printer_factory()
    donor = await _departed_roll(db_session, printer.id, 0, 3)
    _patch_wire(monkeypatch, _state(last_loaded_tray=0, tray_now=0))  # slot 0 is feeding, not 3

    seated = await _wire_cycle(db_session, printer.id, 0, 3)
    transitions = await run_slot_pipeline(printer.id, [seated], _deps(db_session))

    assert transitions[0].decision.kind is DecisionKind.RECLAIM
    assert transitions[0].decision.reason == "reseat_debounce"
    assert transitions[0].decision.spool_id == donor.id


@pytest.mark.asyncio
async def test_a_standing_firmware_demand_for_this_slot_mints(db_session, printer_factory, monkeypatch):
    """The WIRE arm: the firmware is asking for filament in this exact slot, right now.

    Covers what the incident arm structurally cannot. A ``printer_incident`` row is bounded
    to ONE OPEN per printer by design, so a cascade's second dry slot never gets one — and
    a printer already holding a JAM has no runout row at all. The HMS list keeps naming
    every dry slot independently, so the decision reads the wire.

    Here the loss edge carries NO feeder evidence (a firmware auto-refill had already moved
    the feed to a backup before the bit cleared, which is the T8b shape) and there is no
    incident: the demand alone must disqualify the de-bounce.
    """
    printer = await printer_factory()
    donor = await _departed_roll(db_session, printer.id, 0, 1)
    state = _state(last_loaded_tray=-1, tray_now=255, hms=[_demand(0, 1)])
    client = _patch_wire(monkeypatch, state)

    seated = await _wire_cycle(db_session, printer.id, 0, 1)
    transitions = await run_slot_pipeline(printer.id, [seated], _deps(db_session, client))

    assert transitions[0].decision.kind is DecisionKind.MINT
    assert transitions[0].decision.reason == "runout_suspect_mint"
    await db_session.refresh(donor)
    assert donor.weight_used == 940.0


@pytest.mark.asyncio
async def test_a_standing_demand_for_a_DIFFERENT_slot_does_not_disqualify_this_one(
    db_session, printer_factory, monkeypatch
):
    """Per-slot, never per-printer — the same over-suspicion guard the incident arm keeps.

    A blanket "is this printer holding a runout?" test would mint a part-used roll back to
    label weight every time a neighbour slot ran dry, and walk it through the start floor.
    """
    printer = await printer_factory()
    donor = await _departed_roll(db_session, printer.id, 0, 3)
    state = _state(last_loaded_tray=-1, tray_now=255, hms=[_demand(0, 1)])  # slot 1, not 3
    client = _patch_wire(monkeypatch, state)

    seated = await _wire_cycle(db_session, printer.id, 0, 3)
    transitions = await run_slot_pipeline(printer.id, [seated], _deps(db_session, client))

    assert transitions[0].decision.kind is DecisionKind.RECLAIM
    assert transitions[0].decision.spool_id == donor.id


@pytest.mark.asyncio
async def test_a_dual_nozzle_slot_feeding_the_OTHER_hotend_counts_as_feeding(db_session, printer_factory, monkeypatch):
    """H2C/H2D: ``tray_now`` describes ONE hotend, so a two-nozzle machine needs both.

    On a Vortek printer the single ``tray_now`` / ``last_loaded_tray`` pair speaks for the
    ACTIVE extruder only. A slot feeding the deputy nozzle therefore answered "not feeding"
    for every consumer of the feeder resolution — so a mid-print pull there de-bounced onto
    the row that was still printing. The firmware's own per-extruder map
    (``PrinterState.h2d_extruder_snow``, normalized to global tray ids) is the answer, and a
    slot feeding EITHER nozzle was feeding.
    """
    printer = await printer_factory()
    donor = await _departed_roll(db_session, printer.id, 0, 2)
    # The active hotend (0) is feeding slot 0; the deputy (1) is feeding slot 2 — ours.
    state = _state(last_loaded_tray=0, tray_now=0, snow={0: 0, 1: 2})
    _patch_wire(monkeypatch, state, dual=True)

    seated = await _wire_cycle(db_session, printer.id, 0, 2)
    transitions = await run_slot_pipeline(printer.id, [seated], _deps(db_session))

    assert transitions[0].decision.kind is DecisionKind.MINT
    assert transitions[0].decision.reason == "runout_suspect_mint"
    await db_session.refresh(donor)
    assert donor.weight_used == 940.0


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
async def test_an_idle_swap_inside_the_window_de_bounces_and_that_cost_is_accepted(
    db_session, printer_factory, monkeypatch
):
    """§4.1 row T4 — the amendment's ACCEPTED WRONG ANSWER, pinned as such.

    The operator swaps rolls on an IDLE printer and the bay is empty ~3 minutes: inside
    `_RESEAT_WINDOW_S`, with no runout cause anywhere (idle machine, no HMS, no incident,
    nothing feeding). The de-bounce therefore fires and the NEW roll inherits the old
    row's grams. That verdict is wrong about the filament and RIGHT about the doctrine:
    the window decides only "did anything physically happen?" (rule 7 as amended), and
    every cause the farm can actually observe says no.

    It is pinned so it is a decision rather than a surprise. The cost is bounded and
    recoverable — C3's early-runout reverse correction hands the charges back when the
    mis-attributed roll runs out short — and the row exists to stop a later session
    "fixing" it by widening the cause test into a duration guess, which is exactly the
    move rule 7's amendment forbids.

    The window's far side is not re-proved here: T2/T3/T5 own it
    (`test_slot_state.py::TestRow4TaglessLane::test_a_donor_outside_the_window_mints_instead`).
    """
    printer = await printer_factory()
    donor = await _departed_roll(db_session, printer.id, 0, 3)
    client = _patch_wire(monkeypatch, _state(running=False))  # IDLE, no HMS, no feeder
    _seed_gain(printer.id, 0, 3, absent_for=180.0)  # ~3 minutes, measured

    transitions = await run_slot_pipeline(printer.id, [_obs(printer.id, 3)], _deps(db_session, client))

    assert transitions[0].decision.kind is DecisionKind.RECLAIM
    assert transitions[0].decision.reason == "reseat_debounce"
    assert transitions[0].decision.spool_id == donor.id
    await db_session.refresh(donor)
    assert donor.weight_used == 940.0, "the de-bounced row keeps its grams — that IS the accepted cost"
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
async def test_a_spent_newest_residue_is_never_scanned_past_to_an_older_healthy_one(db_session, printer_factory):
    """The "single last occupant" clause, pinned against the failure it names.

    The lane's own docstring said "adjudicated, NEVER SCANNED PAST" while the query applied
    ``archived_at IS NULL`` / ``spent_at IS NULL`` / ``~assignments.any()`` in SQL, ahead of
    the ``LIMIT 1``. A ``WHERE`` clause does not skip an ineligible row — it makes the row
    INVISIBLE — so the query answered with the newest row that happened to PASS, and the
    "adjudication" adjudicated a roll that was never this slot's last occupant.

    The shape below is the ordinary end of a natural runout: the roll that just drained is
    released and stamped spent, and an older healthy roll that once sat in this tray is still
    on the shelf carrying its own breadcrumb. The spent filter hides the drained row, the
    query returns the SHELF roll, and the de-bounce reclaims 400 g of somebody else's ledger
    onto the brand-new roll the operator just loaded. Nothing heals it afterwards either —
    the reclaimed row is healthy, so it never runs out and never gets stamped.

    Scenario contract §4.1 row T6: a runout whose stamp has landed MINTS.
    """
    printer = await printer_factory()
    older_healthy = await _departed_roll(
        db_session,
        printer.id,
        0,
        3,
        weight_used=400.0,
        released_at=datetime.utcnow() - timedelta(hours=1),
    )
    drained = await _departed_roll(db_session, printer.id, 0, 3, weight_used=990.0, spent=True)
    _seed_gain(printer.id, 0, 3, absent_for=25.0)

    transitions = await run_slot_pipeline(printer.id, [_obs(printer.id, 3)], _deps(db_session))

    assert transitions[0].decision.kind is DecisionKind.MINT
    assert transitions[0].decision.reason == "tagless_mint"

    await db_session.refresh(older_healthy)
    assert older_healthy.weight_used == 400.0, "the shelf roll's ledger is untouched"
    bound = await db_session.scalar(select(SpoolAssignment.spool_id).where(SpoolAssignment.printer_id == printer.id))
    assert bound not in (older_healthy.id, drained.id), "neither residue may take the slot"
    minted = await db_session.get(Spool, bound)
    assert (minted.weight_used or 0) == 0, "the fresh roll gets a fresh ledger"


@pytest.mark.asyncio
async def test_a_newest_residue_bound_elsewhere_mints(db_session, printer_factory):
    """The subtlest of the three SQL filters, and the reason the shared stmt lost it.

    A slot MOVE stamps ``last_location_* = the OLD slot`` at move time, so the row that most
    recently left this bay is routinely one that is bound in ANOTHER tray right now.
    ``~assignments.any()`` in SQL did not make that row ineligible, it made it
    UNREPRESENTABLE — every reader of the residue silently got the next row down, an older
    occupant of the same slot and a different physical roll.

    The verdict itself is unchanged and is invariant 11: assumption-tier evidence may
    displace nothing a live binding holds (shape 26, the spool-211 ping-pong). What changed
    is that the refusal is now a stated conclusion about the right row, and the fall-through
    is a mint rather than a reach past it.
    """
    printer = await printer_factory()
    older_healthy = await _departed_roll(
        db_session,
        printer.id,
        0,
        3,
        weight_used=400.0,
        released_at=datetime.utcnow() - timedelta(hours=1),
    )
    moved = await _departed_roll(db_session, printer.id, 0, 3, weight_used=250.0)
    db_session.add(SpoolAssignment(spool_id=moved.id, printer_id=printer.id, ams_id=0, tray_id=1))
    await db_session.commit()
    _seed_gain(printer.id, 0, 3, absent_for=25.0)

    transitions = await run_slot_pipeline(printer.id, [_obs(printer.id, 3)], _deps(db_session))

    assert transitions[0].decision.kind is DecisionKind.MINT
    assert transitions[0].decision.reason == "tagless_mint"

    await db_session.refresh(older_healthy)
    assert older_healthy.weight_used == 400.0
    # The moved roll stayed exactly where the wire says it is — one roll, one slot.
    still_there = await db_session.scalar(select(SpoolAssignment.tray_id).where(SpoolAssignment.spool_id == moved.id))
    assert still_there == 1


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


@pytest.mark.asyncio
async def test_a_row_the_operator_lane_retired_is_never_de_bounced_onto_its_own_slot(db_session, printer_factory):
    """The operator lane's roll swap leaves a breadcrumb, and the ARCHIVE stamp is what
    stops that breadcrumb resurrecting the retired roll onto its replacement.

    ``spool_tagless._replace_row_after_cycle`` unbinds through
    ``spool_binding.release_spool_from_slot`` — the ONE unbind writer — so the departure
    finally leaves the residue, the cleared prompt and the forensic line every other
    departure leaves. That residue is also a de-bounce DONOR, and for the instant between
    the release and the successor's bind the retired row IS this slot's newest one. A donor
    there is shape 32 exactly: a dead row reclaimed onto the brand-new roll that replaced
    it, with nothing downstream able to heal it.

    What forbids it is the ARCHIVED refusal, and it can only fire because the stamp is
    written BEFORE the release, in the same transaction. The mutation check below is the
    point of this test: with the stamp cleared the row IS handed over, so the ordering is
    load-bearing and not incidental. The departed row here is deliberately NOT spent — the
    "New roll" verb answers a prompt raised at 70 % of label, so ``archived_at`` is the
    only thing standing between that lane and the resurrection.
    """
    printer = await printer_factory()
    departed = Spool(
        material="PETG",
        rgba="000000FF",
        label_weight=1000,
        weight_used=980.0,
        data_origin="ams_auto",
    )
    departed.k_profiles = []
    departed.assignments = []
    db_session.add(departed)
    await db_session.flush()
    db_session.add(SpoolAssignment(spool_id=departed.id, printer_id=printer.id, ams_id=0, tray_id=3))
    await db_session.commit()

    successor = await spool_tagless._replace_row_after_cycle(db_session, printer.id, 0, 3, {"id": 3, **_PETG}, departed)
    await db_session.refresh(departed)

    assert successor.id != departed.id
    assert departed.archived_at is not None, "the retired row keeps its ledger, archived"
    assert (
        departed.last_location_printer_id,
        departed.last_location_ams_id,
        departed.last_location_tray_id,
    ) == (printer.id, 0, 3), "the unbind went through the writer, so the residue is stamped"

    obs = _obs(printer.id, 3)
    assert await slot_pipeline._debounce_candidate(db_session, obs) is None

    # The stamp is the whole containment: clear it and this row is a donor again.
    departed.archived_at = None
    await db_session.commit()
    assert await slot_pipeline._debounce_candidate(db_session, obs) is departed
    departed.archived_at = datetime.utcnow()
    await db_session.commit()

    # End to end: a qualifying gain on that slot must not resolve onto the retired row.
    _seed_gain(printer.id, 0, 3, absent_for=25.0)
    transitions = await run_slot_pipeline(printer.id, [obs], _deps(db_session))
    assert transitions[0].decision.kind is not DecisionKind.RECLAIM
    assert transitions[0].decision.spool_id != departed.id
