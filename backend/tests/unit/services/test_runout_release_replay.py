"""The 2026-08-13 003-H2S terminal runout, replayed end to end through REAL entry points.

The incident: slot 4 ran dry with no backup left, the print held PAUSED ~12.5 h, the
operator loaded a brand-new roll — and the farm re-bound the DEAD row (spool 37, 900/1000 g
used, ``spent_at`` NULL) onto the fresh roll and reported "100 g left". The same
resurrection then happened on seven printers in one evening.

The mechanism was an interaction, not a bug in any one lane. The AMS clears a drained
slot's exist bit ~3 MINUTES BEFORE it declares the runout (the tail is still traversing the
feed path — three timed pairs that day: 03:55:46 → 03:58:20, 06:41:47 → 06:44:44,
07:02:52 → 07:05:33). Since the 2026-08-10 wave made bit-clear releases fire reliably,
every natural runout's binding is therefore GONE by the time the exhaustion evidence lands
— and the spent writer required a live binding, so all three stamp lanes became silent
no-ops, while the release's ``last_location_*`` residue is exactly what the reclaim lane
then used to resurrect the un-spent dead row onto the refill.

These cases drive the PRODUCTION entry points — ``spool_binding.release_spool_from_slot``,
``hms_edges.note_push``, ``spool_respool.apply_runout_edges``,
``spool_respool.mark_spent_on_runout_hold`` and ``slot_pipeline.run_slot_pipeline`` — and
never seed a module's internal set. A test that hand-places the state its subject is
supposed to derive proves only that the assertion matches the seed (house lesson,
memory ``liveness-paired-verification``): a cured storm and a starved lane look identical
from the outside, so the pins here are that the stamp HAPPENS, on the right row, once.
"""

import logging
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services import (
    ams_presence,
    hms_edges,
    slot_pipeline,
    spool_binding,
    spool_respool,
    spool_tagless,
)
from backend.app.services.bambu_mqtt import HMSError
from backend.app.services.slot_pipeline import PipelineDeps, run_slot_pipeline
from backend.app.services.tray_observation import observe_tray

_RESPOOL_LOGGER = "backend.app.services.spool_respool"

# The incident's own filament, so a reclaim donor would fingerprint-MATCH the refill: the
# only reason the refill must mint is the spent stamp, never a mismatched fingerprint.
_MATERIAL, _RGBA = "PETG", "000000FF"


# --- wire fixtures ----------------------------------------------------------
#
# Strictly-increasing wire stamp: ``hms_edges.note_push`` consumes a frame only when
# ``hms_wire_at`` ADVANCES — the stamp bambu_mqtt writes whenever a push carries an ``hms``
# list (an empty one included, since a wire all-clear is evidence) and never on a local clear.
_WIRE_AT = [1000.0]


def _hms(attr: int, code: int) -> HMSError:
    """An HMS entry in the shape bambu_mqtt's ``hms[]`` branch appends, carrying the
    LOSSLESS ``full_code`` — the short code drops both the code word's high bits and the
    slot byte, so it can tell neither 0x00020001 from 0x00030001 nor slot 0 from slot 3."""
    return HMSError(
        code=hex(code),
        attr=attr,
        module=(attr >> 24) & 0xFF,
        severity=2,
        full_code=f"{attr:08X}{code:08X}",
    )


def _slot_attr(tray_id: int, ams_id: int = 0) -> int:
    """The slot-attributed AMS attr layout: module 0x07, unit byte, slot as 0x20 + tray."""
    return 0x07000000 | (ams_id << 16) | ((0x20 + tray_id) << 8)


def _pullback(tray_id: int, ams_id: int = 0) -> HMSError:
    """0x00030001 — "Slot N has run out, and the printer is pulling the filament back".

    Per-event, slot-attributed, transient, and the ONLY slot-attributed word a TERMINAL
    runout ever raises (the auto-switch report can never come: there is no backup left)."""
    return _hms(_slot_attr(tray_id, ams_id), 0x00030001)


def _auto_switch(tray_id: int, ams_id: int = 0) -> HMSError:
    """0x00030002 — "…has run out and automatically switched to the slot with the same
    filament". The RESCUED sequel to the pull-back, naming the same slot."""
    return _hms(_slot_attr(tray_id, ams_id), 0x00030002)


def _demand(tray_id: int, ams_id: int = 0) -> HMSError:
    """0x00020001 — the standing "please refill THIS slot" demand.

    Deliberately NOT spent evidence anywhere: 006-H2S 2026-07-26 proved the firmware can
    latch a demand for a slot that never ran dry, and a demand-driven stamp would archive
    a healthy roll permanently."""
    return _hms(_slot_attr(tray_id, ams_id), 0x00020001)


def _insert_filament(ams_id: int = 0) -> HMSError:
    """07xx_8011 — the UNRESCUED, slot-AGNOSTIC "insert filament into the same AMS slot".

    Names its unit but no slot (so Lane A must resolve the tray itself) and LATCHES, which
    is why a deploy inside a hold re-seeds it and it can never edge again."""
    return _hms(0x07000000 | (ams_id << 16), 0x8011)


def _cleared(tray_id: int) -> dict:
    """The drained bay as the wire really renders it minutes before the runout HMS: the
    exist bit dropped, so the merge wrote the FULL cleared shape."""
    return {"id": tray_id, "state": 9, "tray_type": "", "tag_uid": "", "tray_uuid": "", "remain": 0}


def _seated(tray_id: int) -> dict:
    return {"id": tray_id, "state": 11, "tray_type": _MATERIAL, "tray_color": _RGBA, "remain": 42}


def _state(*, gcode_state="PAUSE", trays=(), hms=(), subtask_id="job-terminal", tray_now=255, ams_id=0):
    _WIRE_AT[0] += 1.0
    return SimpleNamespace(
        state=gcode_state,
        hms_errors=list(hms),
        hms_wire_at=_WIRE_AT[0],
        subtask_id=subtask_id,
        tray_now=tray_now,
        nozzles=[],
        ams_extruder_map={},
        raw_data={"ams": [{"id": ams_id, "tray": list(trays)}]},
    )


async def _push(printer_id: int, state, session_factory) -> None:
    """One status push through the production seam: the edge tracker decides what is NEW,
    and only an appearance reaches the stampers."""
    edges = hms_edges.note_push(printer_id, state)
    if edges is not None:
        await spool_respool.apply_runout_edges(printer_id, edges, state, session_factory=session_factory)


# --- DB fixtures ------------------------------------------------------------


async def _bind(db, printer_id: int, ams_id: int, tray_id: int, **kwargs) -> Spool:
    spool = Spool(material=_MATERIAL, rgba=_RGBA, label_weight=1000, core_weight=250, **kwargs)
    spool.k_profiles = []
    spool.assignments = []
    db.add(spool)
    await db.flush()
    db.add(SpoolAssignment(spool_id=spool.id, printer_id=printer_id, ams_id=ams_id, tray_id=tray_id))
    await db.commit()
    return spool


async def _release(db, spool: Spool) -> None:
    """Empty the bay through the REAL unbind writer — the residue under test has to be the
    one production stamps, never a hand-written ``last_location_*`` triple."""
    assignment = (
        await db.execute(select(SpoolAssignment).where(SpoolAssignment.spool_id == spool.id))
    ).scalar_one()
    await spool_binding.release_spool_from_slot(db, assignment, reason="cleared_tray")
    await db.commit()


async def _spent_count(db) -> int:
    db.expunge_all()
    return await db.scalar(select(func.count(Spool.id)).where(Spool.spent_at.is_not(None)))


async def _assignment_spool(db, printer_id: int, ams_id: int, tray_id: int) -> Spool:
    """The spool row the slot is bound to RIGHT NOW, read back from the database."""
    row = (
        await db.execute(
            select(SpoolAssignment).where(
                SpoolAssignment.printer_id == printer_id,
                SpoolAssignment.ams_id == ams_id,
                SpoolAssignment.tray_id == tray_id,
            )
        )
    ).scalar_one()
    return await db.get(Spool, row.spool_id)


@pytest.fixture(autouse=True)
def _clean_state():
    for mod in (spool_respool, hms_edges, ams_presence, slot_pipeline, spool_tagless):
        mod._reset_state()
    spool_binding._move_damper.reset()
    yield
    for mod in (spool_respool, hms_edges, ams_presence, slot_pipeline, spool_tagless):
        mod._reset_state()
    spool_binding._move_damper.reset()


class _Recorder:
    """The pipeline's dependency hooks, recorded instead of reaching the wire."""

    def __init__(self):
        self.settings: dict[str, str] = {}
        self.broadcasts: list[dict] = []
        self.identifies: list[tuple] = []

    async def get_setting(self, key: str) -> str | None:
        return self.settings.get(key)

    async def broadcast(self, payload: dict) -> None:
        self.broadcasts.append(payload)

    async def schedule_identify(self, printer_id, ams_id, tray_id, reason) -> None:
        self.identifies.append((printer_id, ams_id, tray_id, reason))

    async def push_config(self, spool, printer_id, ams_id, tray_id, tray) -> bool:
        return True


@pytest.fixture
def pipeline_env(monkeypatch):
    recorder = _Recorder()

    async def fake_get_setting(db, key):
        return recorder.settings.get(key)

    monkeypatch.setattr("backend.app.api.routes.settings.get_setting", fake_get_setting)
    monkeypatch.setattr(spool_tagless.printer_manager, "get_status", lambda pid: None)
    monkeypatch.setattr(spool_tagless.printer_manager, "get_client", lambda pid: None)
    return recorder


def _deps(db, recorder: _Recorder) -> PipelineDeps:
    return PipelineDeps(
        db=db,
        client=None,
        get_setting=recorder.get_setting,
        schedule_identify=recorder.schedule_identify,
        broadcast=recorder.broadcast,
        push_config=recorder.push_config,
    )


# --- ① the incident itself ---------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_runout_release_then_runout_edges_stamps_released_row(
    db_session, printer_factory, own_session_factory, caplog
):
    """003-H2S, slot 4, verbatim: the bay empties, the binding is released three minutes
    before the firmware admits why, and the runout evidence then arrives on an UNBOUND
    slot. The roll that LEFT that slot is the one that ran out, and it is stamped."""
    printer = await printer_factory()
    exhausted = await _bind(db_session, printer.id, 0, 3, weight_used=900.0)

    # 07:02:52 — the exist bit drops; the pipeline releases the binding. Correct wire
    # truth (doctrine rule 9: an assignment claims where a roll physically IS).
    await _release(db_session, exhausted)
    assert await _spent_count(db_session) == 0

    # A quiet frame first: the tracker's first consumed frame per printer SEEDS without
    # edging, so the runout must APPEAR after it to be a genuine appearance.
    await _push(printer.id, _state(trays=[_cleared(3)]), own_session_factory)

    # 07:05:33 — the runout lands: the pull-back word, the standing demand, and the
    # latching slot-agnostic "insert filament".
    with caplog.at_level(logging.INFO, logger=_RESPOOL_LOGGER):
        await _push(
            printer.id,
            _state(trays=[_cleared(3)], hms=[_pullback(3), _demand(3), _insert_filament()]),
            own_session_factory,
        )

    db_session.expunge_all()
    row = await db_session.get(Spool, exhausted.id)
    assert row.spent_at is not None
    assert row.weight_used == 900.0  # the gram ledger stays raw — spent_at is the truth
    assert any("tier=last_location" in r.getMessage() for r in caplog.records)
    assert await _spent_count(db_session) == 1  # exactly one roll called exhausted


# --- ② the resurrection that followed ---------------------------------------


@pytest.mark.asyncio
async def test_refill_after_stamp_gains_mint_not_reclaim(
    db_session, printer_factory, own_session_factory, pipeline_env
):
    """The operator's fresh roll goes in — and gets its OWN ledger row.

    This is the half the operator SAW: an un-spent dead row parked in ``last_location_*``
    is a perfect reclaim donor (same slot, same fingerprint), so the refill inherited
    900 g of somebody else's history and displayed "100 g left". Nothing in the reclaim
    lane changed to fix it; the spent stamp landing is what makes the donor ineligible,
    and the table falls through to a mint (doctrine rule: new filament in a slot after a
    runout is a DIFFERENT spool).
    """
    printer = await printer_factory()
    exhausted = await _bind(db_session, printer.id, 0, 3, weight_used=900.0)
    await _release(db_session, exhausted)
    await _push(printer.id, _state(trays=[_cleared(3)]), own_session_factory)
    await _push(
        printer.id,
        _state(trays=[_cleared(3)], hms=[_pullback(3), _demand(3), _insert_filament()]),
        own_session_factory,
    )
    db_session.expunge_all()
    assert (await db_session.get(Spool, exhausted.id)).spent_at is not None

    # The refill: a seated, configured, identity-less tray — the reclaim lane's shape.
    obs = observe_tray(printer.id, 0, _seated(3))
    transitions = await run_slot_pipeline(printer.id, [obs], _deps(db_session, pipeline_env))

    assert transitions[0].decision.reason == "tagless_mint"  # NOT last_location_reclaim
    minted = await _assignment_spool(db_session, printer.id, 0, 3)
    assert minted.id != exhausted.id
    assert minted.weight_used in (0, 0.0, None)  # a fresh roll starts at zero
    db_session.expunge_all()
    donor = await db_session.get(Spool, exhausted.id)
    assert donor.spent_at is not None  # stays spent...
    still_bound = await db_session.scalar(
        select(func.count(SpoolAssignment.id)).where(SpoolAssignment.spool_id == donor.id)
    )
    assert still_bound == 0  # ...and unbound: the mint never touched it

    # Control arm — the donor was otherwise a PERFECT match for this slot, so the mint is
    # caused by the spent stamp and by nothing else (fingerprint, slot or boundness).
    donor.spent_at = None
    await db_session.commit()
    assert await slot_pipeline._last_location_candidate(db_session, obs) is donor


# --- ③ the chained, RESCUED shape -------------------------------------------


@pytest.mark.asyncio
async def test_rescued_cascade_stamps_each_departed_slot_once(
    db_session, printer_factory, own_session_factory, caplog
):
    """Two rolls drained inside ONE job, each stamped exactly once.

    A rescued runout raises the pull-back and then, once the firmware has switched to the
    backup, the auto-switch report naming the SAME slot — two words, one exhaustion, and
    ``_spent_dedup`` (keyed per printer/job/TRAY) absorbs the second. When the backup
    itself later runs dry, its own words are a different tray and stamp again: the dedup
    is per-slot precisely so a cascade cannot collapse into one stamp (005-H2S 2026-07-30
    ran three rolls dry in a single print).
    """
    printer = await printer_factory()
    first = await _bind(db_session, printer.id, 0, 0, weight_used=980.0)
    second = await _bind(db_session, printer.id, 0, 1, weight_used=960.0)
    await _release(db_session, first)

    running = {"gcode_state": "RUNNING", "subtask_id": "job-cascade", "tray_now": 1}
    await _push(printer.id, _state(trays=[_cleared(0), _seated(1)], **running), own_session_factory)

    # Slot 1 (tray 0) drains; the print keeps running off tray 1.
    await _push(
        printer.id,
        _state(trays=[_cleared(0), _seated(1)], hms=[_pullback(0)], **running),
        own_session_factory,
    )
    with caplog.at_level(logging.INFO, logger=_RESPOOL_LOGGER):
        await _push(
            printer.id,
            _state(trays=[_cleared(0), _seated(1)], hms=[_pullback(0), _auto_switch(0)], **running),
            own_session_factory,
        )
    assert await _spent_count(db_session) == 1
    # The absorbed second word says so — "absorbed" and "never fired" must not read the
    # same in a prod log, which is what cost the 2026-08-13 triage three days.
    assert any("already booked" in r.getMessage() and "tray 0" in r.getMessage() for r in caplog.records)

    # Slot 2 (tray 1) drains in turn — its own bay clears first, exactly like the first.
    await _release(db_session, second)
    await _push(
        printer.id,
        _state(
            trays=[_cleared(0), _cleared(1)],
            hms=[_pullback(0), _auto_switch(0), _pullback(1), _auto_switch(1)],
            **running,
        ),
        own_session_factory,
    )

    db_session.expunge_all()
    assert (await db_session.get(Spool, first.id)).spent_at is not None
    assert (await db_session.get(Spool, second.id)).spent_at is not None
    assert await _spent_count(db_session) == 2  # two rolls, two stamps, no doubles


# --- ④ the deploy that landed inside the hold -------------------------------


@pytest.mark.asyncio
async def test_deploy_restart_midway_escalation_stamp_lands(
    db_session, printer_factory, own_session_factory, caplog
):
    """A restart mid-hold: the edges are seeded away, and the ESCALATION carries the stamp.

    A terminal runout holds for hours, so a deploy inside it is ordinary. Every edge lane
    is ephemeral by construction — the first frame a process consumes seeds instead of
    edging — and the pull-back word fired seconds after the runout began, i.e. before the
    deploy. So the durable, incident-anchored escalation is the second trigger, and its
    stamp resolves the same released row through the same one writer.
    """
    printer = await printer_factory()
    exhausted = await _bind(db_session, printer.id, 0, 3, weight_used=900.0)
    await _release(db_session, exhausted)

    # --- the deploy: a fresh process knows nothing about this printer's wire history ---
    spool_respool._reset_state()
    hms_edges._reset_state()

    standing = [_pullback(3), _demand(3), _insert_filament()]
    await _push(printer.id, _state(trays=[_cleared(3)], hms=standing), own_session_factory)
    assert await _spent_count(db_session) == 0  # the seeding frame stamps NOTHING

    # The recovery escalation for the still-standing hold (``runout_needs_refill``).
    with caplog.at_level(logging.INFO, logger=_RESPOOL_LOGGER):
        await spool_respool.mark_spent_on_runout_hold(
            printer.id,
            _state(trays=[_cleared(3)], hms=standing),
            subtask_id="job-terminal",
            session_factory=own_session_factory,
        )

    db_session.expunge_all()
    assert (await db_session.get(Spool, exhausted.id)).spent_at is not None
    assert any("tier=last_location" in r.getMessage() for r in caplog.records)
    assert await _spent_count(db_session) == 1

    # A later demand re-appearance (wire all-clear, then the demand again) edges — and
    # still stamps nothing: the demand is not spent evidence in any lane.
    await _push(printer.id, _state(trays=[_cleared(3)]), own_session_factory)
    await _push(printer.id, _state(trays=[_cleared(3)], hms=[_demand(3)]), own_session_factory)
    assert await _spent_count(db_session) == 1


# --- ⑤ the bogus latch that must never stamp --------------------------------


@pytest.mark.asyncio
async def test_bogus_demand_replay_never_stamps_loaded_slot(
    db_session, printer_factory, own_session_factory, caplog
):
    """006-H2S 2026-07-26 replayed against the new durable lane: zero stamps.

    A UI Load click during a runout hold published a change-filament that moved nothing,
    LATCHED in firmware and resurfaced 12 h later as a demand for a slot that never ran
    dry. The demand alone therefore proves nothing — which is why it is excluded from both
    edge lanes and why the escalation lane additionally requires the demanded slot to read
    wire-asserted EMPTY. Here it reads seated, with a healthy half-used roll bound to it.
    """
    printer = await printer_factory()
    healthy = await _bind(db_session, printer.id, 0, 3, weight_used=300.0)

    latched = _state(trays=[_seated(3)], hms=[_demand(3)])
    await _push(printer.id, _state(trays=[_seated(3)]), own_session_factory)
    await _push(printer.id, latched, own_session_factory)

    with caplog.at_level(logging.INFO, logger=_RESPOOL_LOGGER):
        await spool_respool.mark_spent_on_runout_hold(
            printer.id,
            _state(trays=[_seated(3)], hms=[_demand(3)]),
            subtask_id="job-terminal",
            session_factory=own_session_factory,
        )

    db_session.expunge_all()
    assert (await db_session.get(Spool, healthy.id)).spent_at is None
    assert await _spent_count(db_session) == 0
    assert any("OCCUPIED" in r.getMessage() and "bogus-latch" in r.getMessage() for r in caplog.records)
    # And the binding is untouched — a stand-down changes nothing at all.
    assert (await _assignment_spool(db_session, printer.id, 0, 3)).id == healthy.id
