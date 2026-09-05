"""Unit tests for the reused-tag re-spool service (spool_respool).

Covers the core operation (donor disposal, fresh-full mint, tag move, assignment
rewire, K-profile copy, last-brand persistence, staged release), the sibling-tag
guard both directions, and the three certainty tiers (spent-marking on runout /
backup-swap, auto re-spool, one-click prompt) including the Tier-3 evidence gates
(physical swap evidence, impossible-ledger suppression, remain-jump corroboration).
"""

import logging
import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from backend.app.models.print_batch import PrintBatch
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spool_k_profile import SpoolKProfile
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.services import hms_edges, spool_binding, spool_respool
from backend.app.services.bambu_mqtt import HMSError
from backend.app.services.hms_errors import hms_short_code
from backend.app.services.spool_respool import (
    RESPOOL_TAG_TYPE,
    RespoolError,
    RespoolSiblingConflict,
    _remain_jump,
    _remain_jump_reading,
    mark_spent_on_runout,
    mark_spent_on_slot_runout,
    maybe_auto_or_prompt_respool,
    note_commanded_load,
    rebroadcast_unresolved_respool_prompts,
    reset_swap_edge_state,
    respool_tag,
    should_evaluate_respool,
)

DONOR_TAG_UID = "AABBCCDD11223344"
DONOR_TRAY_UUID = "AABBCCDD11223344AABBCCDD11223344"
SIBLING_TAG_UID = "1111222233334444"


def _tray(tag_uid=DONOR_TAG_UID, tray_uuid=DONOR_TRAY_UUID, state=11, tray_type="PETG", tray_weight="1000"):
    return {
        "tray_type": tray_type,
        "tray_sub_brands": "PETG HF",
        "tray_color": "00FF00FF",
        "tray_id_name": "",
        "tag_uid": tag_uid,
        "tray_uuid": tray_uuid,
        "tray_info_idx": "GFG99",
        "tray_weight": tray_weight,
        "state": state,
        "remain": 100,
    }


def _make_state(ams_id, tray_id, tray, *, gcode_state="IDLE", tray_now=255):
    state = MagicMock()
    state.state = gcode_state
    state.tray_now = tray_now
    state.nozzles = []
    state.ams_extruder_map = {}
    state.raw_data = {"ams": [{"id": ams_id, "tray": [{"id": tray_id, **tray}]}]}
    return state


def _patch_pm(monkeypatch, state):
    from backend.app.services.printer_manager import printer_manager

    monkeypatch.setattr(printer_manager, "get_status", lambda _pid: state)
    monkeypatch.setattr(printer_manager, "get_client", lambda _pid: None)


async def _make_donor(db, *, data_origin="rfid_auto", tag_type="bambulab", spent=False, weight_used=990.0):
    donor = Spool(
        material="PETG",
        subtype="HF",
        color_name="Green",
        rgba="00FF00FF",
        brand="Bambu Lab",
        label_weight=1000,
        core_weight=250,
        weight_used=weight_used,
        slicer_filament="GFG99",
        tag_uid=DONOR_TAG_UID,
        tray_uuid=DONOR_TRAY_UUID,
        data_origin=data_origin,
        tag_type=tag_type,
        spent_at=datetime.utcnow() if spent else None,
    )
    donor.k_profiles = []
    donor.assignments = []
    db.add(donor)
    await db.flush()
    return donor


@pytest.fixture(autouse=True)
def _reset_module_state():
    from backend.app.services import ams_presence

    spool_respool._reset_state()
    ams_presence._reset_state()  # the Tier-3 swap-evidence ledger lives there
    hms_edges._reset_state()  # the runout triggers now fire off its appearance edges
    yield
    spool_respool._reset_state()
    ams_presence._reset_state()
    hms_edges._reset_state()


def _record_physical_cycle(printer_id, ams_id=0, tray_id=0, *, age_s=0.0):
    """Stamp a QUALIFIED physical presence cycle on a slot in ams_presence's real
    ledger — the swap evidence Tier 3 requires before it may prompt.

    Writes the same map the presence tracker writes (and that
    ``last_physical_cycle_age`` reads), so the tests exercise the real accessor
    rather than a stub of it. ``age_s`` backdates the stamp.
    """
    from backend.app.services import ams_presence

    ams_presence._physical_cycle_at[(printer_id, ams_id, tray_id)] = time.monotonic() - age_s


@pytest.fixture
def fake_clock(monkeypatch):
    """Drive spool_respool._monotonic so its monotonic windows — the 60 s
    swap-confirm and the 10 s remain-jump corroboration — advance without
    wall-clock waits."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(spool_respool, "_monotonic", lambda: clock["t"])
    return clock


# -- core happy path ---------------------------------------------------------


@pytest.mark.asyncio
async def test_respool_core_happy_path(db_session, printer_factory, monkeypatch):
    """Donor archived (has history), fresh row locked+empty+reused-type, tag moved,
    assignment rewired to the new spool, respool_last_brand persisted."""
    printer = await printer_factory()
    donor = await _make_donor(db_session, spent=True)
    # History-bearing donor → archived (not hard-deleted).
    db_session.add(SpoolUsageHistory(spool_id=donor.id, weight_used=500, status="completed"))
    db_session.add(SpoolAssignment(spool_id=donor.id, printer_id=printer.id, ams_id=0, tray_id=0))
    await db_session.commit()
    donor_id = donor.id

    _patch_pm(monkeypatch, _make_state(0, 0, _tray()))

    new_spool = await respool_tag(db_session, printer_id=printer.id, ams_id=0, tray_id=0, brand="Polymaker")

    assert new_spool.id != donor_id
    assert new_spool.brand == "Polymaker"
    assert new_spool.weight_used == 0
    assert new_spool.weight_locked is True
    assert new_spool.spent_at is None
    assert new_spool.tag_type == RESPOOL_TAG_TYPE
    assert new_spool.data_origin == "rfid_linked"
    assert new_spool.tag_uid == DONOR_TAG_UID
    assert new_spool.tray_uuid == DONOR_TRAY_UUID
    assert new_spool.material == "PETG"  # inherited from donor

    # Donor archived + tags stripped.
    refreshed_donor = await db_session.get(Spool, donor_id)
    assert refreshed_donor is not None
    assert refreshed_donor.archived_at is not None
    assert refreshed_donor.tag_uid is None
    assert refreshed_donor.tray_uuid is None

    # Assignment rewired to the new spool.
    assignment = (
        await db_session.execute(
            select(SpoolAssignment).where(
                SpoolAssignment.printer_id == printer.id,
                SpoolAssignment.ams_id == 0,
                SpoolAssignment.tray_id == 0,
            )
        )
    ).scalar_one()
    assert assignment.spool_id == new_spool.id

    # respool_last_brand persisted.
    from backend.app.api.routes.settings import get_setting

    assert await get_setting(db_session, "respool_last_brand") == "Polymaker"


@pytest.mark.asyncio
async def test_respool_pristine_rfid_auto_donor_hard_deleted(db_session, printer_factory, monkeypatch):
    """A drive-by rfid_auto donor with zero usage history is hard-deleted."""
    printer = await printer_factory()
    await _make_donor(db_session, data_origin="rfid_auto", spent=True)
    await db_session.commit()

    _patch_pm(monkeypatch, _make_state(0, 0, _tray()))
    new_spool = await respool_tag(db_session, printer_id=printer.id, ams_id=0, tray_id=0, brand="Sunlu")

    # Donor hard-deleted → the only remaining row is the fresh reused spool.
    # (Assert by count, not by donor id: SQLite reuses the freed rowid.)
    db_session.expire_all()
    remaining = (await db_session.execute(select(Spool))).scalars().all()
    assert len(remaining) == 1
    assert remaining[0].id == new_spool.id
    assert new_spool.tag_type == RESPOOL_TAG_TYPE


@pytest.mark.asyncio
async def test_respool_history_bearing_donor_archived(db_session, printer_factory, monkeypatch):
    """A donor with usage history is archived (ledger preserved), not deleted."""
    printer = await printer_factory()
    donor = await _make_donor(db_session, data_origin="rfid_auto", spent=True)
    db_session.add(SpoolUsageHistory(spool_id=donor.id, weight_used=123, status="completed"))
    await db_session.commit()
    donor_id = donor.id

    _patch_pm(monkeypatch, _make_state(0, 0, _tray()))
    await respool_tag(db_session, printer_id=printer.id, ams_id=0, tray_id=0, brand="Sunlu")

    refreshed = await db_session.get(Spool, donor_id)
    assert refreshed is not None and refreshed.archived_at is not None


@pytest.mark.asyncio
async def test_respool_donor_none_fresh_full(db_session, printer_factory, monkeypatch):
    """No matching donor row → mint a fresh full spool straight from the tray."""
    printer = await printer_factory()
    _patch_pm(monkeypatch, _make_state(0, 0, _tray()))

    new_spool = await respool_tag(db_session, printer_id=printer.id, ams_id=0, tray_id=0, brand="eSun")

    assert new_spool.material == "PETG"
    assert new_spool.brand == "eSun"
    assert new_spool.weight_used == 0
    assert new_spool.weight_locked is True
    assert new_spool.tag_type == RESPOOL_TAG_TYPE
    assert new_spool.tag_uid == DONOR_TAG_UID


@pytest.mark.asyncio
async def test_respool_label_weight_override_and_weight_used_zero(db_session, printer_factory, monkeypatch):
    """An explicit label_weight is honored; weight_used is always a fresh 0."""
    printer = await printer_factory()
    await _make_donor(db_session, spent=True)
    await db_session.commit()

    _patch_pm(monkeypatch, _make_state(0, 0, _tray(tray_weight="1000")))
    new_spool = await respool_tag(
        db_session, printer_id=printer.id, ams_id=0, tray_id=0, brand="Polymaker", label_weight=750
    )

    assert new_spool.label_weight == 750
    assert new_spool.weight_used == 0


@pytest.mark.asyncio
async def test_respool_copies_k_profiles(db_session, printer_factory, monkeypatch):
    """Donor K-profiles are copied onto the fresh spool."""
    printer = await printer_factory()
    donor = await _make_donor(db_session, data_origin="manual", spent=True)  # manual → archived, not deleted
    donor.k_profiles.append(SpoolKProfile(printer_id=printer.id, nozzle_diameter="0.6", k_value=0.021, cali_idx=5))
    await db_session.commit()

    _patch_pm(monkeypatch, _make_state(0, 0, _tray()))
    new_spool = await respool_tag(db_session, printer_id=printer.id, ams_id=0, tray_id=0, brand="Polymaker")
    new_id = new_spool.id

    db_session.expire_all()
    copied = (await db_session.execute(select(SpoolKProfile).where(SpoolKProfile.spool_id == new_id))).scalars().all()
    assert len(copied) == 1
    assert copied[0].k_value == 0.021
    assert copied[0].cali_idx == 5
    assert copied[0].nozzle_diameter == "0.6"


@pytest.mark.asyncio
async def test_respool_calls_release_filament_staged_after_commit(db_session, printer_factory, monkeypatch):
    """release_filament_staged runs after the atomic commit (staged units freed)."""
    printer = await printer_factory()
    await _make_donor(db_session, spent=True)
    await db_session.commit()

    spy = AsyncMock(return_value=0)
    monkeypatch.setattr("backend.app.services.farm_staging.release_filament_staged", spy)

    _patch_pm(monkeypatch, _make_state(0, 0, _tray()))
    new_spool = await respool_tag(db_session, printer_id=printer.id, ams_id=0, tray_id=0, brand="Polymaker")

    spy.assert_awaited_once()
    assert spy.await_args.args[1] == printer.id
    # New spool is durably committed before the release runs.
    assert (await db_session.get(Spool, new_spool.id)) is not None


# -- sibling-tag guard -------------------------------------------------------


@pytest.mark.asyncio
async def test_respool_sibling_conflict_raises_409(db_session, printer_factory, monkeypatch):
    """A tray_uuid-matching ACTIVE reused-type row with a DIFFERENT tag_uid = 409."""
    printer = await printer_factory()
    sibling = Spool(
        material="PETG",
        brand="Polymaker",
        label_weight=1000,
        core_weight=250,
        tag_uid=SIBLING_TAG_UID,  # the OTHER factory tag, already re-spooled
        tray_uuid=DONOR_TRAY_UUID,
        data_origin="rfid_linked",
        tag_type=RESPOOL_TAG_TYPE,
    )
    sibling.k_profiles = []
    sibling.assignments = []
    db_session.add(sibling)
    await db_session.commit()

    _patch_pm(monkeypatch, _make_state(0, 0, _tray(tag_uid=DONOR_TAG_UID)))
    with pytest.raises(RespoolSiblingConflict) as exc:
        await respool_tag(db_session, printer_id=printer.id, ams_id=0, tray_id=0, brand="Polymaker")
    assert exc.value.status_code == 409
    assert str(sibling.id) in exc.value.detail


@pytest.mark.asyncio
async def test_respool_bambu_different_tag_proceeds_as_donor(db_session, printer_factory, monkeypatch):
    """A Bambu-branded row with a DIFFERENT tag_uid but same tray_uuid IS the donor."""
    printer = await printer_factory()
    donor = Spool(
        material="PETG",
        brand="Bambu Lab",
        label_weight=1000,
        core_weight=250,
        weight_used=980,
        tag_uid=SIBLING_TAG_UID,  # donor holds the other sibling tag
        tray_uuid=DONOR_TRAY_UUID,
        data_origin="rfid_auto",
        tag_type="bambulab",
        spent_at=datetime.utcnow(),
    )
    donor.k_profiles = []
    donor.assignments = []
    db_session.add(donor)
    await db_session.commit()

    _patch_pm(monkeypatch, _make_state(0, 0, _tray(tag_uid=DONOR_TAG_UID)))
    new_spool = await respool_tag(db_session, printer_id=printer.id, ams_id=0, tray_id=0, brand="Polymaker")

    assert new_spool.tag_type == RESPOOL_TAG_TYPE
    assert new_spool.tag_uid == DONOR_TAG_UID  # the scanned tag, not the sibling
    # Pristine rfid_auto donor hard-deleted → only the fresh reused row remains.
    db_session.expire_all()
    remaining = (await db_session.execute(select(Spool))).scalars().all()
    assert len(remaining) == 1
    assert remaining[0].id == new_spool.id


# -- empty-slot / no-tag guards ---------------------------------------------


@pytest.mark.asyncio
async def test_respool_printer_not_connected_404(db_session, printer_factory, monkeypatch):
    printer = await printer_factory()
    _patch_pm(monkeypatch, None)
    with pytest.raises(RespoolError) as exc:
        await respool_tag(db_session, printer_id=printer.id, ams_id=0, tray_id=0, brand="X")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_respool_empty_slot_400(db_session, printer_factory, monkeypatch):
    printer = await printer_factory()
    _patch_pm(monkeypatch, _make_state(0, 0, _tray(tray_type="")))
    with pytest.raises(RespoolError) as exc:
        await respool_tag(db_session, printer_id=printer.id, ams_id=0, tray_id=0, brand="X")
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_respool_no_valid_tag_400(db_session, printer_factory, monkeypatch):
    printer = await printer_factory()
    _patch_pm(
        monkeypatch,
        _make_state(0, 0, _tray(tag_uid="0000000000000000", tray_uuid="00000000000000000000000000000000")),
    )
    with pytest.raises(RespoolError) as exc:
        await respool_tag(db_session, printer_id=printer.id, ams_id=0, tray_id=0, brand="X")
    assert exc.value.status_code == 400


# -- Tier 1: spent-certain marking ------------------------------------------


async def _assign(db, printer_id, ams_id, tray_id, spool_id):
    db.add(SpoolAssignment(spool_id=spool_id, printer_id=printer_id, ams_id=ams_id, tray_id=tray_id))
    await db.flush()


@pytest.mark.asyncio
async def test_mark_spent_via_ams_mapping(db_session, printer_factory):
    """A NEW runout HMS marks the spool feeding the dispatched farm ams_mapping."""
    printer = await printer_factory()
    spool = Spool(material="PETG", label_weight=1000, core_weight=250, weight_used=400)
    spool.k_profiles = []
    spool.assignments = []
    db_session.add(spool)
    await db_session.flush()
    await _assign(db_session, printer.id, 0, 0, spool.id)

    batch = PrintBatch(name="run", sku_file_id=1, status="active")
    db_session.add(batch)
    await db_session.flush()
    item = PrintQueueItem(printer_id=printer.id, batch_id=batch.id, status="printing", ams_mapping="[0, -1, -1, -1]")
    db_session.add(item)
    await db_session.commit()

    state = _make_state(0, 0, _tray(), tray_now=255)  # tray_now unloaded → single-feeder ams_mapping fallback wins
    marked = await mark_spent_on_runout(db_session, printer.id, {"0700_8011"}, state)

    assert marked is not None and marked.id == spool.id
    assert marked.spent_at is not None
    assert marked.weight_used == 400  # true ledger PRESERVED — the label floor is gone


@pytest.mark.asyncio
async def test_mark_spent_via_tray_now_fallback(db_session, printer_factory):
    """No farm ams_mapping → fall back to the live tray_now."""
    printer = await printer_factory()
    spool = Spool(material="PETG", label_weight=1000, core_weight=250, weight_used=200)
    spool.k_profiles = []
    spool.assignments = []
    db_session.add(spool)
    await db_session.flush()
    await _assign(db_session, printer.id, 0, 1, spool.id)
    await db_session.commit()

    state = _make_state(0, 1, _tray(), tray_now=1)  # global 1 → ams 0 tray 1
    marked = await mark_spent_on_runout(db_session, printer.id, {"0300_8004"}, state)

    assert marked is not None and marked.id == spool.id
    assert marked.spent_at is not None


@pytest.mark.asyncio
async def test_mark_spent_multi_feeder_uses_tray_now_when_in_mapping(db_session, printer_factory):
    """Multi-filament farm job: the mapping alone is ambiguous — the live
    tray_now decides, but only when it is one of the job's feeders."""
    printer = await printer_factory()
    spool = Spool(material="PETG", label_weight=1000, core_weight=250, weight_used=400)
    spool.k_profiles = []
    spool.assignments = []
    db_session.add(spool)
    await db_session.flush()
    await _assign(db_session, printer.id, 0, 2, spool.id)  # global tray 2

    batch = PrintBatch(name="run", sku_file_id=1, status="active")
    db_session.add(batch)
    await db_session.flush()
    item = PrintQueueItem(printer_id=printer.id, batch_id=batch.id, status="printing", ams_mapping="[0, 2]")
    db_session.add(item)
    await db_session.commit()

    state = _make_state(0, 2, _tray(), tray_now=2)  # feeding tray 2 at runout
    marked = await mark_spent_on_runout(db_session, printer.id, {"0700_8011"}, state)

    assert marked is not None and marked.id == spool.id
    assert marked.spent_at is not None


@pytest.mark.asyncio
async def test_mark_spent_multi_feeder_tray_now_outside_mapping_marks_nothing(db_session, printer_factory):
    """Multi-filament job with tray_now NOT among the feeders → fail-safe: no
    spent stamp (a wrong stamp would auto-reset a half-full spool later)."""
    printer = await printer_factory()
    spool = Spool(material="PETG", label_weight=1000, core_weight=250, weight_used=400)
    spool.k_profiles = []
    spool.assignments = []
    db_session.add(spool)
    await db_session.flush()
    await _assign(db_session, printer.id, 0, 0, spool.id)

    batch = PrintBatch(name="run", sku_file_id=1, status="active")
    db_session.add(batch)
    await db_session.flush()
    item = PrintQueueItem(printer_id=printer.id, batch_id=batch.id, status="printing", ams_mapping="[0, 2]")
    db_session.add(item)
    await db_session.commit()

    state = _make_state(0, 0, _tray(), tray_now=99)  # already switched off-map
    assert await mark_spent_on_runout(db_session, printer.id, {"0700_8011"}, state) is None

    refreshed = await db_session.get(Spool, spool.id)
    assert refreshed.spent_at is None


@pytest.mark.asyncio
async def test_respool_double_submit_is_noop(db_session, printer_factory, monkeypatch):
    """A second respool of a tag whose row is already the fresh re-spooled
    record (untouched: weight_used=0, spent_at NULL) returns that row unchanged
    instead of archiving it and minting a duplicate."""
    printer = await printer_factory()
    await _make_donor(db_session, spent=True)
    await db_session.commit()
    _patch_pm(monkeypatch, _make_state(0, 0, _tray()))

    first = await respool_tag(db_session, printer_id=printer.id, ams_id=0, tray_id=0, brand="Polymaker")
    second = await respool_tag(db_session, printer_id=printer.id, ams_id=0, tray_id=0, brand="Sunlu")

    assert second.id == first.id
    assert second.brand == "Polymaker"  # unchanged — brand edits go through spool edit
    active_rows = (
        (await db_session.execute(select(Spool).where(Spool.tag_uid == DONOR_TAG_UID, Spool.archived_at.is_(None))))
        .scalars()
        .all()
    )
    assert len(active_rows) == 1


@pytest.mark.asyncio
async def test_mark_spent_idempotent(db_session, printer_factory):
    printer = await printer_factory()
    first = datetime(2026, 1, 1)
    spool = Spool(material="PETG", label_weight=1000, core_weight=250, weight_used=1000, spent_at=first)
    spool.k_profiles = []
    spool.assignments = []
    db_session.add(spool)
    await db_session.flush()
    await _assign(db_session, printer.id, 0, 0, spool.id)
    await db_session.commit()

    state = _make_state(0, 0, _tray(), tray_now=0)
    marked = await mark_spent_on_runout(db_session, printer.id, {"0700_8011"}, state)
    assert marked is not None
    assert marked.spent_at == first  # unchanged — idempotent no-op


@pytest.mark.asyncio
async def test_mark_spent_ignores_non_runout_codes(db_session, printer_factory):
    printer = await printer_factory()
    state = _make_state(0, 0, _tray(), tray_now=0)
    assert await mark_spent_on_runout(db_session, printer.id, {"0300_4057"}, state) is None


# -- Tier 1: attribution across the release-before-runout gap ------------------
#
# The AMS clears a drained slot's exist bit ~3 min BEFORE it declares the runout, so on a
# natural runout the binding is ALREADY released when the evidence lands (2026-08-13, three
# timed pairs). Requiring a live assignment made every stamp fleet-wide a silent no-op from
# the 2026-08-10 release wave onward; these pin the resolver that survives the release.

_RESPOOL_LOGGER = "backend.app.services.spool_respool"


def _empty_bay(*, tray_now=0):
    """The runout push as the wire really delivers it: the drained bay already reads
    cleared (its exist bit dropped minutes ago), while ``tray_now`` still names the slot."""
    return _make_state(0, 0, _tray(tag_uid="", tray_uuid="", state=9, tray_type=""), tray_now=tray_now)


async def _seed_released_row(db, printer_id, ams_id, tray_id, **kwargs):
    """A row that was bound to the slot and then RELEASED through the real unbind writer.

    Never hand-sets ``last_location_*``: the residue under test has to be the one
    production stamps, or the test pins a fiction instead of the lane.
    """
    spool = Spool(material="PETG", label_weight=1000, core_weight=250, **kwargs)
    spool.k_profiles = []
    spool.assignments = []
    db.add(spool)
    await db.flush()
    await _assign(db, printer_id, ams_id, tray_id, spool.id)
    await db.commit()
    assignment = (await db.execute(select(SpoolAssignment).where(SpoolAssignment.spool_id == spool.id))).scalar_one()
    await spool_binding.release_spool_from_slot(db, assignment, reason="cleared_tray")
    await db.commit()
    return spool


def _runout(state, *, subtask_id="job-A"):
    state.subtask_id = subtask_id
    return state


@pytest.mark.asyncio
async def test_mark_spent_released_slot_resolves_last_location_victim(db_session, printer_factory, caplog):
    """The runout evidence lands on an already-unbound slot → the release residue names
    the victim, and the stamp says which tier answered."""
    printer = await printer_factory()
    spool = await _seed_released_row(db_session, printer.id, 0, 0, weight_used=990.0)

    with caplog.at_level(logging.INFO, logger=_RESPOOL_LOGGER):
        marked = await mark_spent_on_runout(db_session, printer.id, {"0700_8011"}, _runout(_empty_bay()))

    assert marked is not None and marked.id == spool.id
    assert marked.spent_at is not None
    assert marked.weight_used == 990.0  # the gram ledger stays raw (doctrine rule 8)
    assert any("tier=last_location" in r.getMessage() for r in caplog.records)


async def _seed_bound_row(db, printer_id, ams_id, tray_id, **kwargs) -> Spool:
    """A row bound to the slot RIGHT NOW — tier 1's incumbent."""
    spool = Spool(material="PETG", label_weight=1000, core_weight=250, **kwargs)
    spool.k_profiles = []
    spool.assignments = []
    db.add(spool)
    await db.flush()
    await _assign(db, printer_id, ams_id, tray_id, spool.id)
    await db.commit()
    return spool


async def _backdate_release(db, spool: Spool, *, minutes: float) -> None:
    """Age a release stamp to where the scenario's physics actually puts it.

    The bay clears MINUTES before the runout HMS, so a test that leaves both stamps at
    ``utcnow()`` is not the production timeline. It is also not decidable: ``last_location_at``
    is a Python ``utcnow()`` with microseconds while ``SpoolAssignment.created_at`` is
    SQLite's ``CURRENT_TIMESTAMP``, truncated to the second — the same precedent (and the same
    reason) as the D1 idempotency case above.
    """
    spool.last_location_at = datetime.utcnow() - timedelta(minutes=minutes)
    await db.commit()


@pytest.mark.asyncio
async def test_mark_spent_prefers_a_live_assignment_seated_before_the_bay_cleared(db_session, printer_factory, caplog):
    """T6 — the ordinary runout, and the LIVENESS half of the eligibility test below.

    The drained roll is still bound when the evidence lands, so tier 1 names it. The slot
    deliberately also carries an OLD residue from a previous occupant, because EVERY
    incumbent was bound after some clear: if ordering alone decided eligibility, tier 1 would
    stand aside on every runout on the fleet and the fix would be worse than the regression
    (memory ``liveness-paired-verification`` — a cured storm and a starved lane are identical
    on an absence metric).
    """
    printer = await printer_factory()
    previous = await _seed_released_row(db_session, printer.id, 0, 0, weight_used=970.0)
    await _backdate_release(db_session, previous, minutes=2 * 24 * 60)  # left two days ago
    drained = await _seed_bound_row(db_session, printer.id, 0, 0, weight_used=990.0)

    with caplog.at_level(logging.INFO, logger=_RESPOOL_LOGGER):
        marked = await mark_spent_on_runout(db_session, printer.id, {"0700_8011"}, _runout(_empty_bay()))

    assert marked is not None and marked.id == drained.id
    assert any("tier=assignment" in r.getMessage() for r in caplog.records)
    await db_session.refresh(previous)
    assert previous.spent_at is None  # an ancient residue is not this runout's victim


@pytest.mark.asyncio
async def test_mark_spent_stands_aside_when_the_incumbent_was_seated_after_the_bay_cleared(
    db_session, printer_factory, caplog
):
    """T7/T8 — the 2026-08-19 regression, inverted.

    The spool-identity wave made a refill inside the ~3-minute bay-clear→HMS gap MINT a fresh
    row and bind it ("a runout release is never a glitch"), which is correct — and it changed
    the precondition tier 1 rests on. Tier 1 resolved its victim from the LIVE assignment, so
    the runout arriving two minutes later stamped the operator's brand-new roll: probe output
    ``FRESH spent_at: 2026-08-20 10:29:14 used: 0.0`` / ``DRAINED spent_at: None used:
    900.0``. The fresh roll then reads 0 g remaining (rule 8 derives emptiness from
    ``spent_at``) and is hard-excluded from selection with no automatic un-spend lane, while
    the roll that actually ran dry stays selectable and reclaimable.

    A roll seated after the bay emptied cannot be the roll that emptied it, so tier 1 stands
    aside and tier 2 resolves the row that actually left.
    """
    printer = await printer_factory()
    drained = await _seed_released_row(db_session, printer.id, 0, 0, weight_used=900.0)
    await _backdate_release(db_session, drained, minutes=3)  # the measured firmware gap
    fresh = await _seed_bound_row(db_session, printer.id, 0, 0, weight_used=0)

    with caplog.at_level(logging.INFO, logger=_RESPOOL_LOGGER):
        marked = await mark_spent_on_runout(db_session, printer.id, {"0700_8011"}, _runout(_empty_bay()))

    assert marked is not None and marked.id == drained.id
    assert marked.spent_at is not None
    assert marked.weight_used == 900.0  # the ledger stays raw (doctrine rule 8)
    await db_session.refresh(fresh)
    assert fresh.spent_at is None, "the operator's brand-new roll must never be called exhausted"
    assert fresh.remaining_g == pytest.approx(1000.0), "…and must still price as a full roll to selection"
    assert any("tier=last_location" in r.getMessage() for r in caplog.records)
    assert any("stands aside" in r.getMessage() for r in caplog.records), "a suppressed stamp always says why"


@pytest.mark.asyncio
async def test_a_residue_bound_elsewhere_is_no_victim_and_never_hands_tier_1_to_an_older_row(
    db_session, printer_factory, caplog
):
    """The slot's last occupant MOVED to another tray — so this slot has no victim at all.

    A move stamps ``last_location_* = the OLD slot`` (``spool_binding.bind_spool_to_slot``'s
    sweep), so the row that most recently left this bay is routinely one the wire now seats
    somewhere else. The shared residue query used to exclude such a row in SQL, which did not
    make it INELIGIBLE — it made it INVISIBLE, and the lane then answered with the next row
    down: an OLDER occupant of the same slot, a different physical roll, still healthy on the
    shelf.

    That silent substitution reached further than tier 2, because tier 1's eligibility test
    weighs the incumbent's bind moment against *this* residue's clear. Handed the older row's
    ancient-but-in-window clear, tier 1 stood aside from a perfectly good live binding and
    tier 2 stamped the shelf roll spent — permanently, with no un-spend lane (operator ruling
    2026-08-09).

    With the residue adjudicated rather than filtered, both halves come out right: no victim
    is named, and tier 1 keeps the roll that was actually feeding.
    """
    printer = await printer_factory()
    older = await _seed_released_row(db_session, printer.id, 0, 0, weight_used=400.0)
    await _backdate_release(db_session, older, minutes=4)
    moved = await _seed_released_row(db_session, printer.id, 0, 0, weight_used=250.0)
    await _backdate_release(db_session, moved, minutes=2)
    await _assign(db_session, printer.id, 0, 2, moved.id)  # re-seated in a DIFFERENT tray
    await db_session.commit()
    drained = await _seed_bound_row(db_session, printer.id, 0, 0, weight_used=980.0)

    with caplog.at_level(logging.INFO, logger=_RESPOOL_LOGGER):
        marked = await mark_spent_on_runout(db_session, printer.id, {"0700_8011"}, _runout(_empty_bay()))

    assert marked is not None and marked.id == drained.id
    assert any("tier=assignment" in r.getMessage() for r in caplog.records)
    assert any("bound elsewhere" in r.getMessage() for r in caplog.records), "a refused residue says why"
    for row in (older, moved):
        await db_session.refresh(row)
    assert older.spent_at is None, "the shelf roll is not this slot's last occupant and is never stamped"
    assert moved.spent_at is None, "…and a roll the wire seats in another tray is not exhausted either"


@pytest.mark.asyncio
async def test_a_clear_older_than_the_episode_window_leaves_tier_1_alone(db_session, printer_factory):
    """The window is what says WHICH of a slot's clears the arriving evidence is about.

    Outside it the farm asserts nothing new: the incumbent has been seated far longer than
    any bay-clear→HMS gap the firmware has ever shown, so it is the roll that has been
    feeding, and it is the one the runout is about. Pinned as the deliberate cost it is —
    the alternative reading (an HMS half an hour late) has never been observed.
    """
    printer = await printer_factory()
    departed = await _seed_released_row(db_session, printer.id, 0, 0, weight_used=400.0)
    await _backdate_release(db_session, departed, minutes=(spool_respool._BAY_CLEAR_TO_RUNOUT_GAP_S / 60) + 5)
    incumbent = await _seed_bound_row(db_session, printer.id, 0, 0, weight_used=800.0)

    marked = await mark_spent_on_runout(db_session, printer.id, {"0700_8011"}, _runout(_empty_bay()))

    assert marked is not None and marked.id == incumbent.id
    await db_session.refresh(departed)
    assert departed.spent_at is None


@pytest.mark.asyncio
async def test_a_debounced_roll_that_came_back_keeps_the_stamp(db_session, printer_factory):
    """T8b — the same roll re-bound after a spurious release is still tier 1's victim.

    Two independent facts keep it eligible, and both are the 2026-08-19 wave's own work: the
    de-bounce carries the incumbent's ORIGINAL bind moment forward
    (``bind_spool_to_slot(..., bind_moment=...)``), so its binding predates the clear; and its
    breadcrumb records that IT vacated this bay more recently than the stranger residue did,
    which puts that residue before its tenure. Either way the stranger is not the drained roll.
    """
    printer = await printer_factory()
    stranger = await _seed_released_row(db_session, printer.id, 0, 0, weight_used=120.0)
    await _backdate_release(db_session, stranger, minutes=8)
    drained = await _seed_released_row(db_session, printer.id, 0, 0, weight_used=900.0)
    await _backdate_release(db_session, drained, minutes=3)
    # The de-bounce: the SAME row re-bound, handing back the moment its seating began.
    await spool_binding.bind_spool_to_slot(
        db_session,
        drained,
        printer_id=printer.id,
        ams_id=0,
        tray_id=0,
        fingerprint_color=None,
        fingerprint_type="PETG",
        origin="tagless_setting",
        preserve_ordinal=True,
        bind_moment=datetime.utcnow() - timedelta(minutes=40),
    )
    await db_session.commit()

    marked = await mark_spent_on_runout(db_session, printer.id, {"0700_8011"}, _runout(_empty_bay()))

    assert marked is not None and marked.id == drained.id
    await db_session.refresh(stranger)
    assert stranger.spent_at is None


class TestBayClearEligibility:
    """Every clause of :func:`spool_respool._bound_after_the_bay_cleared`, in isolation.

    The scenario cases above prove the ladder as a whole; these say WHICH fact each answer
    rests on, so a later edit that keeps the end-to-end behaviour by accident still fails
    here. Pure function, no session — the two timestamps and the slot are all it reads.
    """

    SLOT = {"printer_id": 1, "ams_id": 0, "tray_id": 0}

    def _residue(self, *, minutes_ago: float, slot=(1, 0, 0)) -> Spool:
        printer_id, ams_id, tray_id = slot
        return Spool(
            material="PETG",
            label_weight=1000,
            last_location_printer_id=printer_id,
            last_location_ams_id=ams_id,
            last_location_tray_id=tray_id,
            last_location_at=datetime.utcnow() - timedelta(minutes=minutes_ago),
        )

    def _binding(self, *, minutes_ago: float, spool: Spool | None = None) -> SpoolAssignment:
        return SpoolAssignment(
            spool=spool if spool is not None else Spool(material="PETG", label_weight=1000),
            printer_id=1,
            ams_id=0,
            tray_id=0,
            created_at=datetime.utcnow() - timedelta(minutes=minutes_ago),
        )

    def test_seated_after_a_clear_inside_the_window_is_disqualified(self):
        """T7/T8: the bay emptied three minutes ago and this roll arrived after it."""
        assert (
            spool_respool._bound_after_the_bay_cleared(
                self._binding(minutes_ago=1), self._residue(minutes_ago=3), **self.SLOT
            )
            is True
        )

    def test_a_clear_outside_the_window_is_a_different_episode(self):
        """T6: every incumbent was bound after SOME clear — the window says which one
        this runout is about, and an ancient one is not it."""
        outside = (spool_respool._BAY_CLEAR_TO_RUNOUT_GAP_S / 60) + 1
        assert (
            spool_respool._bound_after_the_bay_cleared(
                self._binding(minutes_ago=1), self._residue(minutes_ago=outside), **self.SLOT
            )
            is False
        )

    def test_a_binding_that_predates_the_clear_is_never_disqualified(self):
        """The de-bounce's bind moment is carried back across the spurious release
        (``bind_spool_to_slot(..., bind_moment=...)``), and that is what keeps it eligible."""
        assert (
            spool_respool._bound_after_the_bay_cleared(
                self._binding(minutes_ago=40), self._residue(minutes_ago=3), **self.SLOT
            )
            is False
        )

    def test_an_incumbent_that_left_this_bay_more_recently_wins(self):
        """A returning roll's own breadcrumb puts the residue's departure BEFORE its tenure,
        so the residue cannot be the roll that just drained — only the incumbent can."""
        returner = self._residue(minutes_ago=2)  # its own release, from this same slot
        assert (
            spool_respool._bound_after_the_bay_cleared(
                self._binding(minutes_ago=1, spool=returner), self._residue(minutes_ago=6), **self.SLOT
            )
            is False
        )

    def test_a_breadcrumb_from_another_slot_does_not_count_as_this_bay_clearing(self):
        """``_last_clear_of`` is slot-exact: a roll that left AMS0-T2 says nothing about
        whether AMS0-T0's bay emptied."""
        elsewhere = self._residue(minutes_ago=2, slot=(1, 0, 2))
        assert (
            spool_respool._bound_after_the_bay_cleared(
                self._binding(minutes_ago=1, spool=elsewhere), self._residue(minutes_ago=6), **self.SLOT
            )
            is True
        )

    @pytest.mark.parametrize(
        ("assignment", "residue", "why"),
        [
            (None, "residue", "no incumbent — tier 2 is already the answer"),
            ("binding", None, "no residue: tier 1 stands aside only in favour of a NAMED alternative"),
        ],
    )
    def test_a_missing_half_keeps_tier_1(self, assignment, residue, why):
        binding = self._binding(minutes_ago=1) if assignment == "binding" else None
        left = self._residue(minutes_ago=3) if residue == "residue" else None
        assert spool_respool._bound_after_the_bay_cleared(binding, left, **self.SLOT) is False, why

    def test_an_unstamped_column_decides_nothing(self):
        """Neither half may be inferred: a NULL breadcrumb is silence, and silence keeps
        the ordinary path rather than redirecting a permanent stamp onto a guess."""
        residue = self._residue(minutes_ago=3)
        residue.last_location_at = None
        assert spool_respool._bound_after_the_bay_cleared(self._binding(minutes_ago=1), residue, **self.SLOT) is False


@pytest.mark.asyncio
async def test_mark_spent_last_location_newest_already_spent_is_idempotent(db_session, printer_factory):
    """D1: adjudicate the NEWEST released row and never scan past it.

    A second trigger for the same slot under a NEW job escapes ``_spent_dedup`` (that key
    carries the job), so it re-enters the resolver. It must land on the already-spent
    victim and no-op. A ``WHERE spent_at IS NULL`` query would step OVER that row onto the
    older residue — a healthy roll that merely sat in this slot once — and stamp it
    permanently, and a false stamp has no automatic way back (invariant 11).
    """
    printer = await printer_factory()
    older = await _seed_released_row(db_session, printer.id, 0, 0, weight_used=120.0)
    # Age the OLDER release. Both stamps come from the writer's own utcnow(), and the
    # Windows clock can render two same-millisecond releases as a tie; backdating makes
    # "newest" decidable without sleeping on clock resolution.
    older.last_location_at = datetime.utcnow() - timedelta(hours=1)
    await db_session.commit()
    newer = await _seed_released_row(db_session, printer.id, 0, 0, weight_used=990.0)

    first = await mark_spent_on_runout(db_session, printer.id, {"0700_8011"}, _runout(_empty_bay()))
    assert first is not None and first.id == newer.id
    stamped_at = first.spent_at

    second = await mark_spent_on_runout(
        db_session, printer.id, {"0700_8011"}, _runout(_empty_bay(), subtask_id="job-B")
    )

    assert second is not None and second.id == newer.id  # the same victim, returned for dedup
    assert second.spent_at == stamped_at  # no re-stamp
    await db_session.refresh(older)
    assert older.spent_at is None  # the healthy older residue is untouched


@pytest.mark.asyncio
async def test_mark_spent_last_location_archived_stands_down(db_session, printer_factory, caplog):
    """Retired inventory is never stamped — and the refusal is a log line, not a silent
    exit (six silent exits are how this failure hid for three days)."""
    printer = await printer_factory()
    spool = await _seed_released_row(db_session, printer.id, 0, 0, weight_used=990.0)
    spool.archived_at = datetime.utcnow()
    await db_session.commit()

    with caplog.at_level(logging.INFO, logger=_RESPOOL_LOGGER):
        assert await mark_spent_on_runout(db_session, printer.id, {"0700_8011"}, _runout(_empty_bay())) is None

    await db_session.refresh(spool)
    assert spool.spent_at is None
    assert any("archived" in r.getMessage() and "stood down" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_mark_spent_no_victim_logs_suppression(db_session, printer_factory, caplog):
    """A slot with no history at all stamps nothing, and says so."""
    printer = await printer_factory()

    with caplog.at_level(logging.INFO, logger=_RESPOOL_LOGGER):
        assert await mark_spent_on_runout(db_session, printer.id, {"0700_8011"}, _runout(_empty_bay())) is None

    assert any("no last-location victim" in r.getMessage() for r in caplog.records)


# -- Tier 1: backup-swap detector (stable-feeder + pending-confirm rebuild) ----


def _running(tray_now, *, present=(0, 1, 2), subtask_id="job-A"):
    """A RUNNING printer state with every ``present`` AMS tray seated (non-empty
    tray_type), feeding ``tray_now`` under job ``subtask_id``.

    The backup-swap detector is now per-job (a subtask_id change is a boundary that
    discards cross-job edge state), so pushes within a single test share a stable
    ``subtask_id`` by default — otherwise a bare ``MagicMock`` auto-creates a distinct
    ``subtask_id`` per instance and every push would read as a job boundary.
    """
    state = MagicMock()
    state.state = "RUNNING"
    state.tray_now = tray_now
    state.subtask_id = subtask_id
    state.raw_data = {"ams": [{"id": 0, "tray": [{"id": t, **_tray()} for t in present]}]}
    return state


def _running_wiped(tray_now, *, seated=(), wiped=(), subtask_id="job-A"):
    """A RUNNING push where ``seated`` trays hold a spool and ``wiped`` trays have run
    fully to empty — the exist-bits wipe (``bambu_mqtt.apply_tray_exist_bits``) forced
    state 9 / blank tray_type, so any presence read of them is ABSENT though the tray
    dict is still in the AMS payload. Models the run-to-empty backup switch behind the
    2026-07-21 003-H2S incident. The detector no longer reads tray presence at all (the
    open-time veto and its ``_tray_present`` helper are deleted): a departed stable
    feeder that vanishes IS the run-dry signature, at open time and at confirm time
    alike, which is exactly what these pushes assert.
    """
    state = MagicMock()
    state.state = "RUNNING"
    state.tray_now = tray_now
    state.subtask_id = subtask_id
    trays = [{"id": t, **_tray()} for t in seated]
    trays += [
        {
            "id": t,
            "state": 9,
            "tray_type": "",
            "tray_color": "",
            "tag_uid": "0000000000000000",
            "tray_uuid": "00000000000000000000000000000000",
            "remain": 0,
        }
        for t in wiped
    ]
    trays.sort(key=lambda d: d["id"])
    state.raw_data = {"ams": [{"id": 0, "tray": trays}]}
    return state


async def _bind_at(db, printer_id, ams_id, tray_id, *, weight_used=500.0):
    spool = Spool(material="PETG", label_weight=1000, core_weight=250, weight_used=weight_used)
    spool.k_profiles = []
    spool.assignments = []
    db.add(spool)
    await db.flush()
    await _assign(db, printer_id, ams_id, tray_id, spool.id)
    await db.commit()
    return spool


def _establish_stable_feeder(printer_id, tray, clock, *, present=(0, 1, 2), subtask_id="job-A"):
    """Make ``tray`` the confirmed stable feeder under one job identity.

    The first push seeds the job/edge state (a boundary that opens nothing); two
    further pushes ≥ _SWAP_CONFIRM_S apart under the SAME subtask confirm the feeder.

    Sync and DB-less because the SAMPLER is: nothing confirms here, so the session-owning
    half never runs. That is the production shape — the sampler carries every push at
    ~1 Hz, the confirmer only a confirmation.
    """
    for _ in range(2):
        assert _sample(printer_id, _running(tray, present=present, subtask_id=subtask_id)) == []
    clock["t"] += spool_respool._SWAP_CONFIRM_S + 1
    assert _sample(printer_id, _running(tray, present=present, subtask_id=subtask_id)) == []
    assert spool_respool._stable_feeder.get(printer_id) == tray


def _sample(printer_id, state) -> list[int]:
    """One status push through the REAL sampler — the sync, DB-less half that
    ``main.on_printer_status_change`` calls on every push. Returns the departed global
    trays whose pending swap CONFIRMED on this push."""
    return spool_respool.sample_status_push(printer_id, state)


async def _drive_swap(session_factory, printer_id, state) -> Spool | None:
    """One status push through BOTH production halves, wired as ``main`` wires them.

    The sync sampler owns every decision (commanded-load suppression, the stable-feeder
    requirement, the job boundary, the ambiguous-topology corroboration); the async
    confirmer stamps what it is handed, on its OWN session. Returns the first spool
    stamped by this push, else None.

    Because the stamp is committed by a DIFFERENT session, a caller inspecting the row
    through ``db_session`` must ``refresh()`` it — the returned instance is the
    confirmer's own and is always current.
    """
    departed = _sample(printer_id, state)
    if not departed:
        return None
    stamped = await spool_respool.confirm_backup_swaps(printer_id, departed, session_factory=session_factory)
    return stamped[0] if stamped else None


@pytest.mark.asyncio
async def test_backup_swap_genuine_switch_stamps_after_confirm(
    db_session, printer_factory, fake_clock, own_session_factory
):
    """A genuine firmware backup switch (the stable feeder ran dry, a sibling feeds
    on for ≥ 60 s, the departed still present) STILL marks the departed spool spent —
    and preserves its true grams (the label floor is gone)."""
    printer = await printer_factory()
    spool = await _bind_at(db_session, printer.id, 0, 0, weight_used=500.0)
    _establish_stable_feeder(printer.id, 0, fake_clock)

    # Edge off the stable feeder (0 → 1) opens a pending swap; not yet confirmed.
    assert await _drive_swap(own_session_factory, printer.id, _running(1)) is None

    # The new tray feeds stably past the confirm window with tray 0 still present.
    fake_clock["t"] += spool_respool._SWAP_CONFIRM_S + 1
    marked = await _drive_swap(own_session_factory, printer.id, _running(1))
    assert marked is not None and marked.id == spool.id
    assert marked.spent_at is not None
    assert marked.weight_used == 500.0  # true ledger preserved


@pytest.mark.asyncio
async def test_backup_swap_no_stamp_before_confirm_window(db_session, printer_factory, fake_clock, own_session_factory):
    """Within the confirm window the pending swap has NOT stamped yet."""
    printer = await printer_factory()
    spool = await _bind_at(db_session, printer.id, 0, 0)
    _establish_stable_feeder(printer.id, 0, fake_clock)

    assert await _drive_swap(own_session_factory, printer.id, _running(1)) is None  # opens pending
    fake_clock["t"] += 10  # still < 60 s
    assert await _drive_swap(own_session_factory, printer.id, _running(1)) is None
    assert (await db_session.get(Spool, spool.id)).spent_at is None


@pytest.mark.asyncio
async def test_backup_swap_drops_on_flap_back_to_departed(db_session, printer_factory, fake_clock, own_session_factory):
    """tray_now returning to the departed feeder = it's feeding again → drop, no stamp."""
    printer = await printer_factory()
    spool = await _bind_at(db_session, printer.id, 0, 0)
    _establish_stable_feeder(printer.id, 0, fake_clock)

    await _drive_swap(own_session_factory, printer.id, _running(1))  # pending 0→1
    fake_clock["t"] += spool_respool._SWAP_CONFIRM_S + 1
    marked = await _drive_swap(own_session_factory, printer.id, _running(0))  # flapped back to 0
    assert marked is None
    assert (await db_session.get(Spool, spool.id)).spent_at is None
    assert printer.id not in spool_respool._pending_swaps  # dropped


@pytest.mark.asyncio
async def test_backup_swap_run_to_empty_departed_absent_stamps_incident_pin(
    db_session, printer_factory, fake_clock, own_session_factory
):
    """INCIDENT PIN (2026-07-21 12:54:55, 003-H2S). A tagless roll on the stable feeder
    (tray 1) runs FULLY dry mid-print: the firmware backup-switches to tray 0, then the
    exist-bits wipe forces tray 1 to state 9 / blank tray_type WITHIN the confirm window
    (the departed tray reads absent). Tray 0 keeps feeding past _SWAP_CONFIRM_S. A
    departed-tray absence right after a mid-print backup switch IS the run-to-empty
    signal, so the departed spool STILL gets stamped spent.

    Pre-fix (drop-on-absent): the first push where tray 1 read absent dropped the
    pending swap before the confirm window elapsed, so nothing was ever stamped — the
    incident's unstamped rows. Today ``_resolve_pending_swap`` reads no presence at
    all — CONFIRM-TIME TOLERANCE is the mechanism. Mutation-verified: re-adding a
    departed-tray absence drop there (the deleted ``_tray_present`` check) makes this
    assert False (no stamp).
    """
    printer = await printer_factory()
    spool = await _bind_at(db_session, printer.id, 0, 1, weight_used=500.0)  # the run-dry feeder
    _establish_stable_feeder(printer.id, 1, fake_clock, present=(0, 1))

    # Edge 1→0 opens the pending swap; tray 1 still seated at the edge (the wipe lags).
    assert await _drive_swap(own_session_factory, printer.id, _running(0, present=(0, 1))) is None

    # Within the window tray 1's exist bit clears → state 9 / blank tray_type (absent).
    fake_clock["t"] += 10
    assert await _drive_swap(own_session_factory, printer.id, _running_wiped(0, seated=(0,), wiped=(1,))) is None

    # Tray 0 keeps feeding past the confirm window with tray 1 still absent → STAMP.
    fake_clock["t"] += spool_respool._SWAP_CONFIRM_S + 1
    marked = await _drive_swap(own_session_factory, printer.id, _running_wiped(0, seated=(0,), wiped=(1,)))
    assert marked is not None and marked.id == spool.id  # departed run-dry spool stamped though absent
    assert marked.spent_at is not None
    assert marked.weight_used == 500.0  # true ledger preserved (the label floor is gone)


@pytest.mark.asyncio
async def test_backup_swap_chained_double_switch_stamps_both(
    db_session, printer_factory, fake_clock, own_session_factory
):
    """The full 003-H2S sequence: tray 1 runs dry (→ tray 0), then ~151 s later tray 0
    runs dry (→ tray 3). Each switch opens and confirms its OWN pending swap (the gap
    exceeds _SWAP_CONFIRM_S), and each departed tray goes absent via the exist-bits wipe
    within its window — BOTH departed spools are stamped spent. Pins that the age-alone
    confirm handles the chained switch (the second edge does not cancel the first)."""
    printer = await printer_factory()
    spool1 = await _bind_at(db_session, printer.id, 0, 1, weight_used=400.0)  # first to run dry
    spool0 = await _bind_at(db_session, printer.id, 0, 0, weight_used=600.0)  # second to run dry
    _establish_stable_feeder(printer.id, 1, fake_clock, present=(0, 1, 3))

    # Switch 1: 1→0. Tray 1 seated at the edge, then wiped within its window.
    assert await _drive_swap(own_session_factory, printer.id, _running(0, present=(0, 1, 3))) is None
    fake_clock["t"] += spool_respool._SWAP_CONFIRM_S + 1
    marked1 = await _drive_swap(own_session_factory, printer.id, _running_wiped(0, seated=(0, 3), wiped=(1,)))
    assert marked1 is not None and marked1.id == spool1.id  # tray 1 stamped though absent
    assert spool_respool._stable_feeder.get(printer.id) == 0  # tray 0 is now the confirmed feeder

    # Switch 2: 0→3. Tray 0 seated at the edge, then wiped within its window.
    assert await _drive_swap(own_session_factory, printer.id, _running_wiped(3, seated=(0, 3), wiped=(1,))) is None
    fake_clock["t"] += spool_respool._SWAP_CONFIRM_S + 1
    marked0 = await _drive_swap(own_session_factory, printer.id, _running_wiped(3, seated=(3,), wiped=(0, 1)))
    assert marked0 is not None and marked0.id == spool0.id  # tray 0 stamped though absent

    # Re-read, not identity-map: the confirmer committed on its OWN session, so this
    # session's cached instances are stale until refreshed.
    await db_session.refresh(spool1)
    await db_session.refresh(spool0)
    assert spool1.spent_at is not None
    assert spool0.spent_at is not None


@pytest.mark.asyncio
async def test_backup_swap_transient_walk_within_window_departed_present_no_stamp(
    db_session,
    printer_factory,
    fake_clock,
    own_session_factory,
):
    """Keep-drop pin: a pending swap whose tray_now moves off ``cur`` to a THIRD tray
    BEFORE the confirm window elapses is a transient walk, not a settled backup switch —
    it drops and stamps nothing (the departed tray still present). Contrasts with the
    run-to-empty absence, which now stamps: the transient `current != cur` drop survives
    the fix intact."""
    printer = await printer_factory()
    spool = await _bind_at(db_session, printer.id, 0, 0, weight_used=500.0)
    _establish_stable_feeder(printer.id, 0, fake_clock, present=(0, 1, 2))

    await _drive_swap(own_session_factory, printer.id, _running(1, present=(0, 1, 2)))  # pending 0→1
    fake_clock["t"] += 10  # still within the window
    marked = await _drive_swap(own_session_factory, printer.id, _running(2, present=(0, 1, 2)))  # walked to tray 2
    assert marked is None
    assert printer.id not in spool_respool._pending_swaps  # dropped as a transient walk
    assert (await db_session.get(Spool, spool.id)).spent_at is None


@pytest.mark.asyncio
async def test_backup_swap_drops_on_state_change(db_session, printer_factory, fake_clock, own_session_factory):
    """Leaving RUNNING before the window elapses drops the pending swap."""
    printer = await printer_factory()
    spool = await _bind_at(db_session, printer.id, 0, 0)
    _establish_stable_feeder(printer.id, 0, fake_clock)

    await _drive_swap(own_session_factory, printer.id, _running(1))  # pending 0→1
    fake_clock["t"] += spool_respool._SWAP_CONFIRM_S + 1
    paused = _running(1)
    paused.state = "PAUSE"
    assert await _drive_swap(own_session_factory, printer.id, paused) is None
    assert (await db_session.get(Spool, spool.id)).spent_at is None


@pytest.mark.asyncio
async def test_backup_swap_transient_walk_no_false_stamp(db_session, printer_factory, fake_clock, own_session_factory):
    """The 011 pattern: stable feeder 2, then tray_now WALKS 2→1→0 during the
    firmware's runout handling. The 1→0 edge departs a NON-stable value (1) so it
    never opens a pending, and the 2→1 pending drops when tray_now moves on — so
    tray 1's still-full spool is NOT falsely stamped."""
    printer = await printer_factory()
    tray1_spool = await _bind_at(db_session, printer.id, 0, 1, weight_used=200.0)  # must NOT be stamped
    _establish_stable_feeder(printer.id, 2, fake_clock)

    # Walk 2→1 (opens pending 2→1) then 1→0 (prev 1 is not the stable feeder → nothing).
    await _drive_swap(own_session_factory, printer.id, _running(1))
    marked = await _drive_swap(own_session_factory, printer.id, _running(0))
    assert marked is None
    assert (await db_session.get(Spool, tray1_spool.id)).spent_at is None  # tray 1 untouched


@pytest.mark.asyncio
async def test_backup_swap_commanded_load_suppressed(db_session, printer_factory, fake_clock, own_session_factory):
    """Our own commanded load to the new tray consumes the marker and never opens a
    pending swap — the departed spool is never stamped (the 006 false-stamp mode)."""
    printer = await printer_factory()
    spool = await _bind_at(db_session, printer.id, 0, 0)
    _establish_stable_feeder(printer.id, 0, fake_clock)

    note_commanded_load(printer.id, 1)  # WE issued the load to tray 1
    assert await _drive_swap(own_session_factory, printer.id, _running(1)) is None  # edge 0→1 suppressed
    assert printer.id not in spool_respool._pending_swaps  # no pending opened
    fake_clock["t"] += spool_respool._SWAP_CONFIRM_S + 1
    assert await _drive_swap(own_session_factory, printer.id, _running(1)) is None
    assert (await db_session.get(Spool, spool.id)).spent_at is None


@pytest.mark.asyncio
async def test_backup_swap_commanded_load_ttl_expiry_rearms(
    db_session, printer_factory, fake_clock, own_session_factory
):
    """A commanded-load marker older than _COMMANDED_LOAD_TTL_S no longer suppresses:
    a later genuine switch to that same tray stamps normally."""
    printer = await printer_factory()
    spool = await _bind_at(db_session, printer.id, 0, 0)

    note_commanded_load(printer.id, 1)  # stale marker at t0
    fake_clock["t"] += spool_respool._COMMANDED_LOAD_TTL_S + 1  # let it expire
    _establish_stable_feeder(printer.id, 0, fake_clock)

    assert await _drive_swap(own_session_factory, printer.id, _running(1)) is None  # opens pending (marker expired)
    fake_clock["t"] += spool_respool._SWAP_CONFIRM_S + 1
    marked = await _drive_swap(own_session_factory, printer.id, _running(1))
    assert marked is not None and marked.id == spool.id  # stamped — TTL expiry re-armed detection


@pytest.mark.asyncio
async def test_backup_swap_noop_when_not_running(db_session, printer_factory, own_session_factory):
    """Not mid-print: the edge tracker updates but nothing stamps (baseline)."""
    printer = await printer_factory()
    idle = MagicMock()
    idle.state = "IDLE"
    idle.tray_now = 1
    idle.raw_data = {"ams": [{"id": 0, "tray": [{"id": 0, **_tray()}]}]}
    assert await _drive_swap(own_session_factory, printer.id, idle) is None


# -- Tier 1: job-boundary edge reset (2026-07-20 spool-106 false-stamp) ---------


@pytest.mark.asyncio
async def test_backup_swap_no_stamp_across_job_boundary_incident_pin(
    db_session, printer_factory, fake_clock, own_session_factory
):
    """INCIDENT PIN (2026-07-20 02:40, spool 106 falsely stamped spent).

    Job A feeds tray 0 for > 60 s (stable feeder 0). The next job B is dispatch-mapped
    to tray 2 — a NORMAL FIFO spool selection, not a runout. Pre-fix the per-printer
    edge state crossed the job boundary, so the 0→2 feeder change read as a mid-job
    firmware backup switch and stamped tray 0's still-full spool spent after the 60 s
    confirm (the roll never emptied — same tag bound, ~250 g fed afterward). With the
    fix the subtask A→B change resets the edge state, so nothing is stamped.

    Mutation-verified: with the boundary check disabled this asserts False (the pending
    swap confirms and stamps tray 0).
    """
    printer = await printer_factory()
    tray0_spool = await _bind_at(db_session, printer.id, 0, 0, weight_used=250.0)  # must NOT be stamped
    _establish_stable_feeder(printer.id, 0, fake_clock, subtask_id="A")

    # Job B (new subtask) is dispatch-mapped to tray 2; tray 0 is still seated.
    assert await _drive_swap(own_session_factory, printer.id, _running(2, subtask_id="B")) is None
    fake_clock["t"] += spool_respool._SWAP_CONFIRM_S + 1
    marked = await _drive_swap(own_session_factory, printer.id, _running(2, subtask_id="B"))

    assert marked is None
    assert (await db_session.get(Spool, tray0_spool.id)).spent_at is None
    assert printer.id not in spool_respool._pending_swaps


@pytest.mark.asyncio
async def test_backup_swap_eject_interlude_between_jobs_no_false_stamp(
    db_session, printer_factory, fake_clock, own_session_factory
):
    """Eject-shaped interlude: job A feeds tray 0 (stable), then a server-dispatched
    eject job runs RUNNING with tray_now=255 (no filament) under its own subtask, then
    job B is dispatch-mapped to tray 2. The RUNNING eject pushes never fire the
    not-running cleanup (that is why the stale stable feeder survived pre-fix); with the
    fix each subtask change (A→eject→B) resets the edge state, so tray 0 is not stamped.
    """
    printer = await printer_factory()
    tray0_spool = await _bind_at(db_session, printer.id, 0, 0, weight_used=250.0)
    _establish_stable_feeder(printer.id, 0, fake_clock, subtask_id="A")

    # Eject job: RUNNING, tray_now=255 (no filament), distinct subtask → no cleanup.
    await _drive_swap(own_session_factory, printer.id, _running(255, subtask_id="eject"))
    fake_clock["t"] += 5
    await _drive_swap(own_session_factory, printer.id, _running(255, subtask_id="eject"))
    assert printer.id not in spool_respool._stable_feeder  # the boundary reset cleared it

    # Job B mapped to tray 2, tray 0 still seated, past the confirm window.
    assert await _drive_swap(own_session_factory, printer.id, _running(2, subtask_id="B")) is None
    fake_clock["t"] += spool_respool._SWAP_CONFIRM_S + 1
    marked = await _drive_swap(own_session_factory, printer.id, _running(2, subtask_id="B"))

    assert marked is None
    assert (await db_session.get(Spool, tray0_spool.id)).spent_at is None


@pytest.mark.asyncio
async def test_reset_swap_edge_state_clears_printer_and_opens_no_swap(
    db_session, printer_factory, fake_clock, own_session_factory
):
    """The job-boundary reset hook (called from main.on_print_start / on_print_complete)
    drops that printer's edge trackers; the next push re-seeds ``_last_tray_now`` with
    prev ``None`` and opens no pending swap (no confirmed stable feeder survives)."""
    printer = await printer_factory()
    spool = await _bind_at(db_session, printer.id, 0, 0)
    _establish_stable_feeder(printer.id, 0, fake_clock)  # stable feeder 0 armed

    reset_swap_edge_state(printer.id)

    assert printer.id not in spool_respool._last_tray_now
    assert printer.id not in spool_respool._feeder_since
    assert printer.id not in spool_respool._stable_feeder
    assert printer.id not in spool_respool._pending_swaps

    # The next push (still subtask job-A) merely re-seeds; the immediate 0→1 edge cannot
    # open a pending because there is no confirmed stable feeder after the reset.
    assert await _drive_swap(own_session_factory, printer.id, _running(1)) is None
    assert printer.id not in spool_respool._pending_swaps
    fake_clock["t"] += spool_respool._SWAP_CONFIRM_S + 1
    assert await _drive_swap(own_session_factory, printer.id, _running(1)) is None
    assert (await db_session.get(Spool, spool.id)).spent_at is None


@pytest.mark.asyncio
async def test_backup_swap_same_subtask_genuine_switch_still_stamps(
    db_session, printer_factory, fake_clock, own_session_factory
):
    """Regression guard for the job-boundary fix: a genuine mid-job firmware backup
    switch happens under an UNCHANGED subtask_id and must STILL stamp. Same subtask 'J'
    throughout: stable feeder 0, edge 0→1, tray 0 still seated, 60 s confirm → stamped.
    """
    printer = await printer_factory()
    spool = await _bind_at(db_session, printer.id, 0, 0, weight_used=500.0)
    _establish_stable_feeder(printer.id, 0, fake_clock, subtask_id="J")

    assert await _drive_swap(own_session_factory, printer.id, _running(1, subtask_id="J")) is None
    fake_clock["t"] += spool_respool._SWAP_CONFIRM_S + 1
    marked = await _drive_swap(own_session_factory, printer.id, _running(1, subtask_id="J"))

    assert marked is not None and marked.id == spool.id
    assert marked.spent_at is not None


# -- Tier 2 / 3 gate ---------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_spent_and_loaded_auto_respools(db_session, printer_factory, monkeypatch):
    printer = await printer_factory()
    donor = await _make_donor(db_session, spent=True)
    await db_session.commit()

    from backend.app.api.routes.settings import set_setting

    await set_setting(db_session, "respool_last_brand", "Polymaker")
    await db_session.commit()

    _patch_pm(monkeypatch, _make_state(0, 0, _tray(state=11)))
    result = await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, _tray(state=11), donor)

    assert result is not None
    assert result is not donor  # a distinct fresh row (SQLite may reuse the id)
    assert result.tag_type == RESPOOL_TAG_TYPE
    assert result.weight_locked is True
    assert result.weight_used == 0


@pytest.mark.asyncio
async def test_gate_spent_not_loaded_does_nothing(db_session, printer_factory, monkeypatch):
    printer = await printer_factory()
    donor = await _make_donor(db_session, spent=True)
    await db_session.commit()

    broadcasts = _spy_broadcast(monkeypatch)
    # state=10 → present but NOT loaded (dead spool re-inserted).
    result = await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, _tray(state=10), donor)

    assert result is None
    assert broadcasts == []


@pytest.mark.asyncio
async def test_gate_tier3_logs_once_and_never_prompts(db_session, printer_factory, monkeypatch, caplog):
    """Tier 3 is a GUESS (grams + a timing window, no hardware statement), and on
    this fleet every respool prompt ever raised was a false positive — so since
    2026-08-10 it OBSERVES instead of interrupting. One INFO per (slot, tag), on
    the ~1 Hz status stream, and never a broadcast."""
    printer = await printer_factory()
    donor = await _make_donor(db_session, spent=False, weight_used=990.0)  # remaining 10 <= 30
    await db_session.commit()
    _record_physical_cycle(printer.id)  # somebody cycled a roll through the slot

    broadcasts = _spy_broadcast(monkeypatch)
    tray = _tray()
    with caplog.at_level("INFO", logger="backend.app.services.spool_respool"):
        await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, tray, donor)
        await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, tray, donor)  # deduped

    assert [b for b in broadcasts if b["type"] == "respool_prompt"] == []
    observations = [r for r in caplog.records if "Re-spool heuristic fired" in r.getMessage()]
    assert len(observations) == 1
    msg = observations[0].getMessage()
    assert f"spool {donor.id}" in msg
    assert "reason=near_empty" in msg


@pytest.mark.asyncio
async def test_gate_null_spent_over_threshold_does_nothing(db_session, printer_factory, monkeypatch):
    printer = await printer_factory()
    donor = await _make_donor(db_session, spent=False, weight_used=100.0)  # remaining 900 > 30
    await db_session.commit()
    _record_physical_cycle(printer.id)  # evidence present — the THRESHOLD is what blocks

    broadcasts = _spy_broadcast(monkeypatch)
    result = await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, _tray(), donor)

    assert result is None
    assert [b for b in broadcasts if b["type"] == "respool_prompt"] == []


@pytest.mark.asyncio
async def test_gate_auto_sibling_conflict_falls_back_to_prompt(db_session, printer_factory, monkeypatch):
    """An auto attempt that hits the sibling guard warns + prompts instead of raising."""
    printer = await printer_factory()
    # The tray_uuid-matching active reused row IS what get_spool_by_tag returns;
    # its tag_uid differs from the scanned tag → sibling conflict inside respool_tag.
    sibling = Spool(
        material="PETG",
        brand="Polymaker",
        label_weight=1000,
        core_weight=250,
        weight_used=1000,
        tag_uid=SIBLING_TAG_UID,
        tray_uuid=DONOR_TRAY_UUID,
        data_origin="rfid_linked",
        tag_type=RESPOOL_TAG_TYPE,
        spent_at=datetime.utcnow(),
    )
    sibling.k_profiles = []
    sibling.assignments = []
    db_session.add(sibling)
    await db_session.commit()

    from backend.app.api.routes.settings import set_setting

    await set_setting(db_session, "respool_last_brand", "Polymaker")
    await db_session.commit()

    _patch_pm(monkeypatch, _make_state(0, 0, _tray(tag_uid=DONOR_TAG_UID, state=11)))
    broadcasts = _spy_broadcast(monkeypatch)

    result = await maybe_auto_or_prompt_respool(
        db_session, printer.id, 0, 0, _tray(tag_uid=DONOR_TAG_UID, state=11), sibling
    )

    assert result is None  # did not auto-respool
    assert any(b["type"] == "respool_prompt" for b in broadcasts)


# -- Tier 3 dismissal persistence (respool_dismissed_at) ---------------------


@pytest.mark.asyncio
async def test_gate_tier3_suppressed_when_dismissed(db_session, printer_factory, monkeypatch):
    """A tier-3-eligible spool (spent_at NULL, near-empty) the operator already
    answered 'Same spool' on (respool_dismissed_at stamped) does NOT re-prompt."""
    printer = await printer_factory()
    donor = await _make_donor(db_session, spent=False, weight_used=990.0)  # remaining 10 <= 30
    donor.respool_dismissed_at = datetime.utcnow()
    await db_session.commit()
    _record_physical_cycle(printer.id)  # evidence present — the DISMISSAL is what blocks

    broadcasts = _spy_broadcast(monkeypatch)
    result = await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, _tray(), donor)

    assert result is None
    assert [b for b in broadcasts if b["type"] == "respool_prompt"] == []


@pytest.mark.asyncio
async def test_gate_tier3_fires_when_not_dismissed(db_session, printer_factory, monkeypatch, caplog):
    """Baseline for the dismissal test below: the SAME near-empty spool still
    reaches the tier-3 observation while it is not dismissed (the line is what the
    dismissal suppresses now that the prompt is gone)."""
    printer = await printer_factory()
    donor = await _make_donor(db_session, spent=False, weight_used=990.0)
    await db_session.commit()
    _record_physical_cycle(printer.id)

    broadcasts = _spy_broadcast(monkeypatch)
    with caplog.at_level("INFO", logger="backend.app.services.spool_respool"):
        await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, _tray(), donor)

    assert [b for b in broadcasts if b["type"] == "respool_prompt"] == []
    assert [r for r in caplog.records if "Re-spool heuristic fired" in r.getMessage()]


@pytest.mark.asyncio
async def test_gate_tier3_dismissal_survives_dedup_clear(db_session, printer_factory, monkeypatch, caplog):
    """The persisted dismissal outlives the in-memory dedup: clearing the slot
    dedup (as main.on_ams_change does when a slot reports empty) does NOT re-open
    the prompt for a dismissed spool — the whole point of the new column."""
    printer = await printer_factory()
    donor = await _make_donor(db_session, spent=False, weight_used=990.0)
    donor.respool_dismissed_at = datetime.utcnow()
    await db_session.commit()
    _record_physical_cycle(printer.id)  # evidence present on both passes

    broadcasts = _spy_broadcast(monkeypatch)
    with caplog.at_level("INFO", logger="backend.app.services.spool_respool"):
        await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, _tray(), donor)
        # Simulate the empty-slot dedup clear that used to re-arm the prompt.
        spool_respool.clear_respool_prompt_dedup(printer.id, 0, 0)
        await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, _tray(), donor)

    assert [b for b in broadcasts if b["type"] == "respool_prompt"] == []
    # The dismissal is upstream of the observation too — a dismissed row is silent
    # on both surfaces, so the demotion did not smuggle the noise into the log.
    assert [r for r in caplog.records if "Re-spool heuristic fired" in r.getMessage()] == []


# -- R4: spent-tier dismissal honored per physical cycle ---------------------


@pytest.mark.asyncio
async def test_spent_dismissal_stands_no_cycle_suppresses_auto_and_prompt(db_session, printer_factory, monkeypatch):
    """R4 test 1 (MUTATION PIN). A spent + loaded spool the operator answered
    "Same spool" on, with NO physical cycle recorded since (the accessor returns
    None — e.g. right after a restart), broadcasts NOTHING and does NOT auto-respool
    even with a last brand set and Tier 2 otherwise ready to conclude. This is the whole fix:
    the false spent stamp on spool 106 re-fired for days because the spent branch
    ignored the dismissal.

    Mutation-verified: with the `_dismissal_stands` gate removed the auto path runs —
    a fresh spool is minted, the donor is archived, and both asserts below flip.
    """
    printer = await printer_factory()
    donor = await _make_donor(db_session, spent=True)
    donor.respool_dismissed_at = datetime.utcnow()
    from backend.app.api.routes.settings import set_setting

    await set_setting(db_session, "respool_last_brand", "Polymaker")  # Tier 2 WOULD conclude if the gate were gone
    await db_session.commit()
    # No _record_physical_cycle: last_physical_cycle_age → None → dismissal stands.

    _patch_pm(monkeypatch, _make_state(0, 0, _tray(state=11)))
    broadcasts = _spy_broadcast(monkeypatch)
    result = await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, _tray(state=11), donor)

    assert result is None  # no auto-respool
    assert [b for b in broadcasts if b["type"] == "respool_prompt"] == []  # no prompt
    assert (await db_session.get(Spool, donor.id)).archived_at is None  # donor untouched


@pytest.mark.asyncio
async def test_spent_dismissal_cycle_after_rearms_the_escalation_when_unexecutable(
    db_session, printer_factory, monkeypatch
):
    """R4 test 2 (the surviving prompt arm). A qualified physical cycle STRICTLY AFTER the
    dismissal re-arms the spent branch, and when the CONCLUSION cannot be executed — no
    brand anywhere to mint the fresh row with — it escalates to the one-click prompt.

    This test used to prove "auto OFF → prompt". That toggle is deleted (operator ruling 3,
    2026-08-19): tier 2 concludes on evidence. The prompt did not go with it, because
    "I know what happened but cannot carry it out" is an escalation, not an alternative
    verdict — and the re-arm contract it pins (a genuine roll swap on a dismissed slot
    must surface) is unchanged.
    """
    printer = await printer_factory()
    donor = await _make_donor(db_session, spent=True)
    donor.respool_dismissed_at = datetime.utcnow() - timedelta(seconds=100)  # dismissed 100 s ago
    from backend.app.api.routes.settings import set_setting

    await set_setting(db_session, "respool_last_brand", "")  # no prefill…
    await set_setting(db_session, "tagless_default_filament", "")  # …and no default to fall back on
    await db_session.commit()
    _record_physical_cycle(printer.id, age_s=0.0)  # cycle just now → age (~0) < 100 → after dismissal

    _patch_pm(monkeypatch, _make_state(0, 0, _tray(state=11)))
    broadcasts = _spy_broadcast(monkeypatch)
    result = await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, _tray(state=11), donor)

    assert result is None  # nothing minted — there was no brand to mint with
    prompts = [b for b in broadcasts if b["type"] == "respool_prompt"]
    assert len(prompts) == 1
    assert prompts[0]["trigger"] == "spent"
    assert (await db_session.get(Spool, donor.id)).archived_at is None  # donor untouched


@pytest.mark.asyncio
async def test_spent_dismissal_cycle_after_rearms_the_tier2_conclusion(db_session, printer_factory, monkeypatch):
    """R4 test 2 (the conclusion). The same post-dismissal cycle re-arms Tier 2 and it
    CONCLUDES — the spent + loaded tag re-spools to a fresh row, with nothing asked and no
    toggle in front of it (operator ruling 3, 2026-08-19)."""
    printer = await printer_factory()
    donor = await _make_donor(db_session, spent=True)
    donor.respool_dismissed_at = datetime.utcnow() - timedelta(seconds=100)
    from backend.app.api.routes.settings import set_setting

    await set_setting(db_session, "respool_last_brand", "Polymaker")
    await db_session.commit()
    _record_physical_cycle(printer.id, age_s=0.0)  # cycle after the dismissal → re-arm

    _patch_pm(monkeypatch, _make_state(0, 0, _tray(state=11)))
    result = await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, _tray(state=11), donor)

    assert result is not None and result is not donor  # a fresh re-spooled row
    assert result.tag_type == RESPOOL_TAG_TYPE
    assert result.weight_locked is True
    assert result.weight_used == 0  # the auto path ran and minted a fresh full spool


@pytest.mark.asyncio
async def test_spent_dismissal_cycle_before_stays_suppressed(db_session, printer_factory, monkeypatch):
    """R4 test 3. A cycle that predates the dismissal (age > seconds since the
    dismissal) does NOT re-arm — the operator answered "Same spool" AFTER that cycle,
    so the branch stays suppressed."""
    printer = await printer_factory()
    donor = await _make_donor(db_session, spent=True)
    donor.respool_dismissed_at = datetime.utcnow()  # dismissed now (seconds since ≈ 0)
    from backend.app.api.routes.settings import set_setting

    await set_setting(db_session, "respool_last_brand", "Polymaker")
    await db_session.commit()
    _record_physical_cycle(printer.id, age_s=100.0)  # cycle 100 s ago → age (100) ≥ 0 → predates

    _patch_pm(monkeypatch, _make_state(0, 0, _tray(state=11)))
    broadcasts = _spy_broadcast(monkeypatch)
    result = await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, _tray(state=11), donor)

    assert result is None
    assert [b for b in broadcasts if b["type"] == "respool_prompt"] == []
    assert (await db_session.get(Spool, donor.id)).archived_at is None  # donor untouched


# -- R5: prompt provenance payload fields ------------------------------------


async def _clear_respool_brands(db) -> None:
    """Remove every brand Tier 2 could mint a fresh row with.

    Since 2026-08-19 Tier 2 CONCLUDES on a spent + loaded arrival (operator ruling 3), so
    the only remaining way to observe the prompt payload from the live gate is the
    ESCALATION arm — the conclusion is right but unexecutable. Both brand sources have to
    go: the ``respool_last_brand`` prefill and the ``tagless_default_filament`` fallback
    it drops to.
    """
    from backend.app.api.routes.settings import set_setting

    await set_setting(db, "respool_last_brand", "")
    await set_setting(db, "tagless_default_filament", "")


@pytest.mark.asyncio
async def test_respool_prompt_payload_carries_provenance(db_session, printer_factory, monkeypatch):
    """R5. The spent-tier prompt payload carries the additive provenance fields so
    the UI can show the evidence and its age: spent_at + spent_age_s, the live AMS
    remain %, the ledger-implied remain %, and when the roll became bound."""
    printer = await printer_factory()
    # Spent, loaded, NOT dismissed, and NO brand anywhere: Tier 2's conclusion stands but
    # cannot be executed, which is the one arm that still raises this prompt (WS3).
    donor = await _make_donor(db_session, spent=True, weight_used=990.0)  # ledger 1% of a 1000 g label
    await _clear_respool_brands(db_session)
    await db_session.commit()

    _patch_pm(monkeypatch, _make_state(0, 0, _tray(state=11)))  # tray remain=100
    broadcasts = _spy_broadcast(monkeypatch)
    await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, _tray(state=11), donor)

    prompts = [b for b in broadcasts if b["type"] == "respool_prompt"]
    assert len(prompts) == 1
    p = prompts[0]
    assert p["trigger"] == "spent"
    assert isinstance(p["spent_at"], str) and p["spent_at"]  # ISO string
    assert isinstance(p["spent_age_s"], float) and p["spent_age_s"] >= 0.0
    assert p["ams_remain_pct"] == 100  # live tray remain
    assert p["ledger_remain_pct"] == pytest.approx(1.0)  # (1000 - 990) / 1000 * 100
    assert isinstance(p["bound_since"], str) and p["bound_since"]  # created_at fallback


@pytest.mark.asyncio
async def test_respool_prompt_payload_nulls_when_absent(db_session, printer_factory, monkeypatch):
    """The provenance fields degrade to None cleanly: a garbage tray remain yields
    no ams_remain_pct (the parse discipline of _remain_jump_reading) while the
    ledger % still computes from the durable row."""
    printer = await printer_factory()
    donor = await _make_donor(db_session, spent=True, weight_used=990.0)  # ledger 1% of a 1000 g label
    await _clear_respool_brands(db_session)  # no brand → the conclusion escalates to the prompt
    await db_session.commit()

    garbage = _tray(state=11)
    garbage["remain"] = "n/a"  # firmware junk — must not be quoted as a percentage
    _patch_pm(monkeypatch, _make_state(0, 0, garbage))
    broadcasts = _spy_broadcast(monkeypatch)
    await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, garbage, donor)

    prompts = [b for b in broadcasts if b["type"] == "respool_prompt"]
    assert len(prompts) == 1
    p = prompts[0]
    assert p["ams_remain_pct"] is None
    assert p["ledger_remain_pct"] == pytest.approx(1.0)


# -- Tier 2 auto-brand fallback to the tagless default (3b-5) ----------------


@pytest.mark.asyncio
async def test_gate_tier2_empty_last_brand_uses_tagless_default(db_session, printer_factory, monkeypatch):
    """Before the first-ever manual re-spool (respool_last_brand empty), a
    spent+loaded spool auto-respools using the configured tagless-default brand
    instead of prompting (3b-5)."""
    printer = await printer_factory()
    donor = await _make_donor(db_session, spent=True)
    from backend.app.api.routes.settings import set_setting

    # respool_last_brand intentionally NOT set (empty) → the fallback engages.
    await set_setting(
        db_session,
        "tagless_default_filament",
        '{"brand": "eSun", "material": "PETG", "subtype": "HF", "rgba": "00FF00FF"}',
    )
    await db_session.commit()

    _patch_pm(monkeypatch, _make_state(0, 0, _tray(state=11)))
    broadcasts = _spy_broadcast(monkeypatch)
    result = await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, _tray(state=11), donor)

    assert result is not None
    assert result.tag_type == RESPOOL_TAG_TYPE
    assert result.brand == "eSun"  # sourced from the tagless default, not last-brand
    assert [b for b in broadcasts if b["type"] == "respool_prompt"] == []


@pytest.mark.asyncio
async def test_gate_tier2_both_empty_falls_back_to_prompt(db_session, printer_factory, monkeypatch):
    """respool_last_brand empty AND the tagless default explicitly OFF (empty
    string) → no brand to auto with, so surface the one-click prompt (today's
    behaviour is preserved when the parser yields nothing)."""
    printer = await printer_factory()
    donor = await _make_donor(db_session, spent=True)
    from backend.app.api.routes.settings import set_setting

    await set_setting(db_session, "tagless_default_filament", "")  # explicit off
    await db_session.commit()

    _patch_pm(monkeypatch, _make_state(0, 0, _tray(state=11)))
    broadcasts = _spy_broadcast(monkeypatch)
    result = await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, _tray(state=11), donor)

    assert result is None  # did not auto-respool
    assert any(b["type"] == "respool_prompt" for b in broadcasts)


# -- Spoolman mode no-ops ----------------------------------------------------


@pytest.mark.asyncio
async def test_hooks_noop_in_spoolman_mode(db_session, printer_factory, monkeypatch, own_session_factory):
    printer = await printer_factory()
    donor = await _make_donor(db_session, spent=True, weight_used=990.0)
    await _assign(db_session, printer.id, 0, 0, donor.id)
    from backend.app.api.routes.settings import set_setting

    await set_setting(db_session, "spoolman_enabled", "true")
    await db_session.commit()

    broadcasts = _spy_broadcast(monkeypatch)
    state = _make_state(0, 0, _tray(), tray_now=0)

    assert await mark_spent_on_runout(db_session, printer.id, {"0700_8011"}, state) is None
    assert await _drive_swap(own_session_factory, printer.id, state) is None
    assert await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, _tray(), donor) is None
    assert broadcasts == []


# -- Bug C: live-tray-now resolution + per-incident spent dedup --------------


async def _single_feeder_item(db, printer_id, *, mapping="[0, -1, -1, -1]"):
    batch = PrintBatch(name="run", sku_file_id=1, status="active")
    db.add(batch)
    await db.flush()
    item = PrintQueueItem(printer_id=printer_id, batch_id=batch.id, status="printing", ams_mapping=mapping)
    db.add(item)
    await db.flush()
    return item


async def _new_spool(db, **kwargs):
    spool = Spool(
        material="PETG", label_weight=1000, core_weight=250, weight_used=kwargs.pop("weight_used", 100), **kwargs
    )
    spool.k_profiles = []
    spool.assignments = []
    db.add(spool)
    await db.flush()
    return spool


@pytest.mark.asyncio
async def test_resolve_prefers_live_tray_now_over_mapping(db_session, printer_factory):
    """Single-feeder job: the live feeding tray_now (a real 0-254 tray) wins over
    the dispatched ams_mapping — the mapping can be stale after a reload/swap."""
    printer = await printer_factory()
    spool0 = await _new_spool(db_session)  # mapping target (global 0)
    spool1 = await _new_spool(db_session)  # live tray_now (global 1)
    await _assign(db_session, printer.id, 0, 0, spool0.id)
    await _assign(db_session, printer.id, 0, 1, spool1.id)
    await _single_feeder_item(db_session, printer.id, mapping="[0, -1, -1, -1]")
    await db_session.commit()

    state = _make_state(0, 1, _tray(), tray_now=1)  # feeding tray 1, mapping says 0
    state.subtask_id = "job-1"
    marked = await mark_spent_on_runout(db_session, printer.id, {"0700_8011"}, state)

    assert marked is not None and marked.id == spool1.id  # live tray_now won
    assert (await db_session.get(Spool, spool0.id)).spent_at is None  # mapping target untouched


@pytest.mark.asyncio
async def test_resolve_tray_now_255_falls_back_to_mapping(db_session, printer_factory):
    """tray_now unloaded (255) → the single-feeder ams_mapping is the fallback."""
    printer = await printer_factory()
    spool0 = await _new_spool(db_session)
    await _assign(db_session, printer.id, 0, 0, spool0.id)
    await _single_feeder_item(db_session, printer.id, mapping="[0, -1, -1, -1]")
    await db_session.commit()

    state = _make_state(0, 0, _tray(), tray_now=255)
    state.subtask_id = "job-1"
    marked = await mark_spent_on_runout(db_session, printer.id, {"0700_8011"}, state)

    assert marked is not None and marked.id == spool0.id


@pytest.mark.asyncio
async def test_incident_dedup_no_second_stamp_same_job(db_session, printer_factory):
    """A re-raised runout on the SAME (printer, job, tray) must not stamp the
    operator's freshly-inserted replacement spool (the 18:56 misattribution)."""
    printer = await printer_factory()
    spool_a = await _new_spool(db_session)
    await _assign(db_session, printer.id, 0, 0, spool_a.id)
    await _single_feeder_item(db_session, printer.id, mapping="[0, -1, -1, -1]")
    await db_session.commit()

    state = _make_state(0, 0, _tray(), tray_now=255)
    state.subtask_id = "job-1"
    first = await mark_spent_on_runout(db_session, printer.id, {"0700_8011"}, state)
    assert first is not None and first.id == spool_a.id

    # Operator inserts a fresh spool → auto re-assigned to the same slot.
    assignment = (
        await db_session.execute(
            select(SpoolAssignment).where(
                SpoolAssignment.printer_id == printer.id,
                SpoolAssignment.ams_id == 0,
                SpoolAssignment.tray_id == 0,
            )
        )
    ).scalar_one()
    spool_b = await _new_spool(db_session, weight_used=0)
    assignment.spool_id = spool_b.id
    await db_session.commit()

    # Re-raised runout on the same job/tray → dedup → the fresh spool is untouched.
    second = await mark_spent_on_runout(db_session, printer.id, {"0700_8011"}, state)
    assert second is None
    assert (await db_session.get(Spool, spool_b.id)).spent_at is None


@pytest.mark.asyncio
async def test_incident_dedup_different_subtask_stamps(db_session, printer_factory):
    """A DIFFERENT job (new subtask_id) on the same tray naturally misses the
    dedup and stamps — a genuine later exhaustion is still recorded."""
    printer = await printer_factory()
    spool_a = await _new_spool(db_session)
    await _assign(db_session, printer.id, 0, 0, spool_a.id)
    await _single_feeder_item(db_session, printer.id, mapping="[0, -1, -1, -1]")
    await db_session.commit()

    state1 = _make_state(0, 0, _tray(), tray_now=255)
    state1.subtask_id = "job-1"
    assert (await mark_spent_on_runout(db_session, printer.id, {"0700_8011"}, state1)) is not None

    assignment = (
        await db_session.execute(
            select(SpoolAssignment).where(
                SpoolAssignment.printer_id == printer.id,
                SpoolAssignment.ams_id == 0,
                SpoolAssignment.tray_id == 0,
            )
        )
    ).scalar_one()
    spool_b = await _new_spool(db_session, weight_used=0)
    assignment.spool_id = spool_b.id
    await db_session.commit()

    state2 = _make_state(0, 0, _tray(), tray_now=255)
    state2.subtask_id = "job-2"  # a new print → new dedup key
    marked = await mark_spent_on_runout(db_session, printer.id, {"0700_8011"}, state2)
    assert marked is not None and marked.id == spool_b.id
    assert (await db_session.get(Spool, spool_b.id)).spent_at is not None


def _spy_broadcast(monkeypatch):
    from backend.app.core.websocket import ws_manager

    collected: list[dict] = []

    async def _spy(msg):
        collected.append(msg)

    monkeypatch.setattr(ws_manager, "broadcast", _spy)
    return collected


# -- The runout-edge seam: hms_edges.note_push → apply_runout_edges ----------
#
# "NEW" is the wire-HMS APPEARANCE tracker's answer, never notify_dedup's 600 s
# re-notify window (a level-triggered alert policy, the wrong axis for a state
# decision). Restart-replay suppression is that tracker's first-consumed-frame seed,
# which is why neither stamper carries a seed check any more. These drive the real
# seam — the two stampers' own decisions are pinned directly further up/down.


def _runout_err(code="8011", attr=0x07000000):
    """A live runout HMSError → hms_short_code(attr, code) == "0700_8011".

    Carries the LOSSLESS ``full_code`` (the dataclass default is ``""``) because that
    is the identity the edge tracker keys on: entries left at the default would all
    collide into one code."""
    return HMSError(code=code, attr=attr, module=7, severity=2, full_code=f"{attr:08X}{int(code, 16):08X}")


# Strictly-increasing wire stamp. ``note_push`` consumes a frame only when
# ``hms_wire_at`` ADVANCES, mirroring bambu_mqtt: the stamp is written whenever a push
# carries an ``hms`` list, INCLUDING an empty one (a wire all-clear is evidence), and
# left alone by local clears.
_WIRE_AT = [1000.0]


def _wire(state, *hms):
    """Stamp ``state`` as a frame carrying fresh wire-HMS evidence, and return it."""
    _WIRE_AT[0] += 1.0
    state.hms_errors = list(hms)
    state.hms_wire_at = _WIRE_AT[0]
    return state


async def _push(printer_id, state, session_factory):
    """One status push through the production seam; returns the edge report or None."""
    edges = hms_edges.note_push(printer_id, state)
    if edges is not None:
        await spool_respool.apply_runout_edges(printer_id, edges, state, session_factory=session_factory)
    return edges


@pytest.mark.asyncio
async def test_apply_runout_edges_swallows_a_stamper_failure(
    db_session, printer_factory, own_session_factory, monkeypatch, caplog
):
    """Invariant 10: the orchestrator is a fire-and-forget task hanging off the MQTT
    status chain, so a failure anywhere inside it is logged and swallowed — an exception
    would land in an orphaned task and take the push's other consumers with it."""
    printer = await printer_factory()
    spool = await _new_spool(db_session, weight_used=400)
    await _assign(db_session, printer.id, 0, 0, spool.id)
    await _single_feeder_item(db_session, printer.id, mapping="[0, -1, -1, -1]")
    await db_session.commit()

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(spool_respool, "mark_spent_on_runout", _boom)
    await _push(printer.id, _wire(_make_state(0, 0, _tray(), tray_now=255)), own_session_factory)  # seed

    fire = _make_state(0, 0, _tray(), tray_now=255)
    fire.subtask_id = "job-1"
    with caplog.at_level(logging.WARNING, logger="backend.app.services.spool_respool"):
        await _push(printer.id, _wire(fire, _runout_err()), own_session_factory)

    assert any("Runout-edge apply failed" in m for m in caplog.messages)
    await db_session.refresh(spool)
    assert spool.spent_at is None


@pytest.mark.asyncio
async def test_runout_live_on_the_first_consumed_frame_never_stamps(db_session, printer_factory, own_session_factory):
    """Restart replay, Lane A: the first frame the process consumes carries a live
    runout → it SEEDS instead of edging, so the fresh roll now bound to the slot is
    not mis-stamped (the 2026-07-17 18:56 misattribution)."""
    printer = await printer_factory()
    fresh = await _new_spool(db_session, weight_used=0)  # the fresh roll now on the slot
    await _assign(db_session, printer.id, 0, 0, fresh.id)
    await _single_feeder_item(db_session, printer.id, mapping="[0, -1, -1, -1]")
    await db_session.commit()

    state = _make_state(0, 0, _tray(), tray_now=255)
    state.subtask_id = "job-1"

    assert await _push(printer.id, _wire(state, _runout_err()), own_session_factory) is None  # seed, no edge
    await db_session.refresh(fresh)
    assert fresh.spent_at is None  # NOT stamped


@pytest.mark.asyncio
async def test_connect_time_frame_does_not_consume_the_seed(db_session, printer_factory, own_session_factory):
    """A connect-time broadcast (fresh PrinterState, state="unknown") must NOT consume
    the seeding frame — otherwise the still-live runout on the next REAL report would
    look like an appearance and mis-stamp the fresh spool."""
    printer = await printer_factory()
    fresh = await _new_spool(db_session, weight_used=0)
    await _assign(db_session, printer.id, 0, 0, fresh.id)
    await _single_feeder_item(db_session, printer.id, mapping="[0, -1, -1, -1]")
    await db_session.commit()

    connect_state = _make_state(0, 0, _tray(), gcode_state="unknown", tray_now=255)
    assert await _push(printer.id, _wire(connect_state), own_session_factory) is None  # not consumed

    report_state = _make_state(0, 0, _tray(), gcode_state="PAUSE", tray_now=255)
    report_state.subtask_id = "job-1"
    # The first REAL report still carries the replayed code → THIS frame seeds.
    assert await _push(printer.id, _wire(report_state, _runout_err()), own_session_factory) is None

    await db_session.refresh(fresh)
    assert fresh.spent_at is None  # NOT stamped


@pytest.mark.asyncio
async def test_runout_appearing_after_the_seed_stamps(db_session, printer_factory, own_session_factory):
    """Liveness, Lane A: a runout absent from the seeding frame is a genuine
    appearance and stamps end-to-end through the seam."""
    printer = await printer_factory()
    spool = await _new_spool(db_session, weight_used=400)
    await _assign(db_session, printer.id, 0, 0, spool.id)
    await _single_feeder_item(db_session, printer.id, mapping="[0, -1, -1, -1]")
    await db_session.commit()

    seed_state = _make_state(0, 0, _tray(), tray_now=255)
    await _push(printer.id, _wire(seed_state), own_session_factory)  # zero HMS → seeds {}

    fire_state = _make_state(0, 0, _tray(), tray_now=255)
    fire_state.subtask_id = "job-1"
    edges = await _push(printer.id, _wire(fire_state, _runout_err()), own_session_factory)

    assert edges is not None
    await db_session.refresh(spool)
    assert spool.spent_at is not None


@pytest.mark.asyncio
async def test_seeded_runout_clearing_on_the_wire_then_refiring_stamps(
    db_session, printer_factory, own_session_factory
):
    """A seeded code leaves the live set on a wire ALL-CLEAR frame, so its return is a
    genuine appearance and stamps — the flap the seed must not swallow forever."""
    printer = await printer_factory()
    spool = await _new_spool(db_session, weight_used=400)
    await _assign(db_session, printer.id, 0, 0, spool.id)
    await _single_feeder_item(db_session, printer.id, mapping="[0, -1, -1, -1]")
    await db_session.commit()

    s1 = _make_state(0, 0, _tray(), tray_now=255)
    await _push(printer.id, _wire(s1, _runout_err()), own_session_factory)  # seeds the code

    s2 = _make_state(0, 0, _tray(), tray_now=255)
    await _push(printer.id, _wire(s2), own_session_factory)  # wire all-clear

    s3 = _make_state(0, 0, _tray(), tray_now=255)
    s3.subtask_id = "job-1"
    assert await _push(printer.id, _wire(s3, _runout_err()), own_session_factory) is not None

    await db_session.refresh(spool)
    assert spool.spent_at is not None


@pytest.mark.asyncio
async def test_runout_reappearing_under_a_new_job_stamps_again(db_session, printer_factory, own_session_factory):
    """THE flap-window bug, Lane A. Under notify_dedup a code clearing and returning
    inside the 600 s re-notify window is ONE continuing incident — right for paging a
    human, wrong for spent evidence: the second exhaustion is physically real and the
    roll the operator inserted in between runs dry unrecorded. On the appearance edge
    both stamp."""
    printer = await printer_factory()
    first = await _new_spool(db_session, weight_used=900)
    await _assign(db_session, printer.id, 0, 0, first.id)
    await _single_feeder_item(db_session, printer.id, mapping="[0, -1, -1, -1]")
    await db_session.commit()

    await _push(printer.id, _wire(_make_state(0, 0, _tray(), tray_now=255)), own_session_factory)  # seed

    job_a = _make_state(0, 0, _tray(), tray_now=255)
    job_a.subtask_id = "job-a"
    await _push(printer.id, _wire(job_a, _runout_err()), own_session_factory)
    await db_session.refresh(first)
    assert first.spent_at is not None

    # Operator refills; the code clears on the wire and comes back on the NEXT job,
    # well inside the 600 s window.
    await _push(printer.id, _wire(_make_state(0, 0, _tray(), tray_now=255)), own_session_factory)
    assignment = (
        await db_session.execute(
            select(SpoolAssignment).where(
                SpoolAssignment.printer_id == printer.id,
                SpoolAssignment.ams_id == 0,
                SpoolAssignment.tray_id == 0,
            )
        )
    ).scalar_one()
    second = await _new_spool(db_session, weight_used=900)
    assignment.spool_id = second.id
    await db_session.commit()

    job_b = _make_state(0, 0, _tray(), tray_now=255)
    job_b.subtask_id = "job-b"
    await _push(printer.id, _wire(job_b, _runout_err()), own_session_factory)

    await db_session.refresh(second)
    assert second.spent_at is not None  # TWO spent rows, one per physical exhaustion


# -- §4.1 row T14: the EXTERNAL spool holder ---------------------------------
#
# A NO-OP row, so its pin has to assert an absence — and an absence assertion is only
# worth anything beside a liveness arm that proves the same harness stamps when it
# should (memory ``liveness-paired-verification``). Both arms below share one fixture
# and differ in a single wire byte: the module half of the HMS attr.


async def _external_and_ams_rows(db, printer_id):
    """One roll on the external vt_tray holder (``ams_id=255``, the ``tray_id + 254``
    convention) and one in AMS 0 slot 0, with a farm job feeding the AMS slot."""
    external = await _new_spool(db, weight_used=900)
    await _assign(db, printer_id, 255, 0, external.id)
    in_ams = await _new_spool(db, weight_used=900)
    await _assign(db, printer_id, 0, 0, in_ams.id)
    await _single_feeder_item(db, printer_id, mapping="[0, -1, -1, -1]")
    await db.commit()
    return external, in_ams


@pytest.mark.asyncio
async def test_an_external_holder_runout_stamps_nothing_spent(db_session, printer_factory, own_session_factory):
    """§4.1 row T14 — the external spool holder runs out: **no spent stamp**, ever.

    Deliberate and unchanged since the lane was written. External rows are bindable, but
    the left/right vt-tray attribution convention on a dual-holder machine is UNCONFIRMED,
    and a wrong-side stamp is permanent (there is no automatic un-spend). A missed stamp
    self-heals forward; a false one does not — so this fault class stands down entirely.

    The suppression is STRUCTURAL, which is what makes it durable: ``07FF``/``07FE``
    classify as :class:`AmsFaultClass.RUNOUT_EXTERNAL`, a class disjoint from the
    AMS-slot runout vocabulary Lane A intersects, and the external code words are not in
    Lane B's per-event set either. Neither lane has anything to consume, so nothing
    resolves a tray and nothing writes.

    Both rows are checked, not just the external one: an external runout must not be
    laundered onto the AMS roll by any inference tier either.
    """
    from backend.app.services.hms_errors import RUNOUT_HMS_CODES, runout_external_short_codes

    printer = await printer_factory()
    external, in_ams = await _external_and_ams_rows(db_session, printer.id)

    await _push(printer.id, _wire(_make_state(0, 0, _tray(), tray_now=255)), own_session_factory)  # seed

    fire = _make_state(0, 0, _tray(), tray_now=255)
    fire.subtask_id = "job-ext"
    # "External filament has run out; please load a new filament." — module 07FF.
    edges = await _push(printer.id, _wire(fire, _runout_err(attr=0x07FF0000)), own_session_factory)

    assert edges is not None, "the code DID appear on the wire — this is a suppression, not a miss"
    assert "07FF_8011" in edges.appeared_short
    assert "07FF_8011" in runout_external_short_codes()
    assert not (runout_external_short_codes() & RUNOUT_HMS_CODES), "the two runout vocabularies stay disjoint"

    await db_session.refresh(external)
    await db_session.refresh(in_ams)
    assert external.spent_at is None, "the holder's own row is never stamped (attribution unconfirmed)"
    assert in_ams.spent_at is None, "and the fault is never re-attributed to an AMS slot"


@pytest.mark.asyncio
async def test_an_ams_slot_runout_on_the_same_fixture_still_stamps(db_session, printer_factory, own_session_factory):
    """T14's liveness pair: one byte of the attr apart (``0700`` instead of ``07FF``) and
    the very same push stamps the AMS roll. Without this, a dead edge seam and a correct
    external stand-down are indistinguishable."""
    printer = await printer_factory()
    external, in_ams = await _external_and_ams_rows(db_session, printer.id)

    await _push(printer.id, _wire(_make_state(0, 0, _tray(), tray_now=255)), own_session_factory)  # seed

    fire = _make_state(0, 0, _tray(), tray_now=255)
    fire.subtask_id = "job-ams"
    await _push(printer.id, _wire(fire, _runout_err(attr=0x07000000)), own_session_factory)

    await db_session.refresh(external)
    await db_session.refresh(in_ams)
    assert in_ams.spent_at is not None, "the AMS-slot runout stamps — the seam is alive"
    assert external.spent_at is None


@pytest.fixture
def recovery_own_sessions(test_engine, monkeypatch):
    """Point the recovery driver's own-session openers at the test engine (the shape
    ``test_spool_recovery.py`` uses); scoped to the escalation-lane case below."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    import backend.app.core.database as core_db

    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(core_db, "async_session", maker)
    return maker


async def _runout_incident(db, printer_id, *, external):
    """A runout driver context over a REAL open incident row.

    Since 2026-09-04 every outcome sink verifies OWNERSHIP at write time —
    ``_escalate`` stands down (no token, no page, no ledger row, no spent stamp) when
    ``mark_escalated`` reports the row already closed or missing — so a pin that
    asserts what an escalation WRITES must own a row. One open row per printer: the
    caller closes it before minting the next.
    """
    from backend.app.models.printer_incident import STATUS_RECOVERING
    from backend.app.services import printer_incidents, spool_recovery

    row = await printer_incidents.open_new(
        db,
        printer_id=printer_id,
        job_id="task-ext",
        item_id=None,
        kind=spool_recovery.KIND_RUNOUT,
        code="07FF_8011" if external else "0700_8011",
        codes="runout",
        slot_global_tray=254 if external else 0,
        status=STATUS_RECOVERING,
    )
    assert row is not None, "the printer already holds an open incident — close it first"
    return spool_recovery.RecoveryIncident(
        incident_id=row.id,
        printer_id=printer_id,
        job_id="task-ext",
        codes=frozenset({"07FF_8011" if external else "0700_8011"}),
        fingerprint="runout",
        item_id=None,
        settings=spool_recovery.RecoverySettings(enabled=True, max_attempts=2, step_timeout_s=0.05, protect_layers=7),
        jammed_global_tray=254 if external else 0,
        kind=spool_recovery.KIND_RUNOUT,
        external=external,
        extruder_side_only=False,
        layer_at_fault=50,
        code="07FF_8011" if external else "0700_8011",
        printer_name="003-H2S",
        job_name="SKU007",
    )


@pytest.mark.asyncio
async def test_the_durable_escalation_lane_also_stands_down_for_the_external_holder(
    db_session, printer_factory, recovery_own_sessions, monkeypatch, caplog
):
    """§4.1 row T14, the OTHER lane — and the one a reader is most likely to miss.

    The wire edges are ephemeral (every restart re-seeds them), so a runout HOLD that
    spans a deploy stamps from its ESCALATION instead. That lane is gated on the
    escalation REASON, and ``external_spool_runout`` is excluded for the same permanence
    argument as above. The kind/reason pairing is what the gate reads, so the pin drives
    ``_escalate`` itself rather than asserting the shape of a condition.

    The control arm is the whole point: an AMS-slot runout hold on an otherwise identical
    incident DOES reach the stamper.
    """
    from backend.app.services import spool_recovery
    from backend.app.services.notification_service import notification_service

    printer = await printer_factory()
    monkeypatch.setattr(notification_service, "on_spool_recovery_failed", AsyncMock())
    _patch_pm(monkeypatch, _make_state(0, 0, _tray(), tray_now=255))
    stamps = AsyncMock()
    monkeypatch.setattr(spool_respool, "mark_spent_on_runout_hold", stamps)

    with caplog.at_level(logging.WARNING, logger="backend.app.services.spool_recovery"):
        await spool_recovery._escalate(
            await _runout_incident(db_session, printer.id, external=True), "external_spool_runout"
        )

    stamps.assert_not_awaited()
    assert any("external_spool_runout" in m for m in caplog.messages), "the hold names its reason in the log"

    # Liveness: the AMS-slot hold on the same driver still carries the durable stamp.
    # The external hold is closed first — one open incident per printer — the way a
    # lifecycle event would close it.
    from backend.app.services import printer_incidents

    db_session.expunge_all()
    await printer_incidents.close_open_for_printer(db_session, printer.id, source="terminal")
    await spool_recovery._escalate(
        await _runout_incident(db_session, printer.id, external=False), "runout_needs_refill"
    )
    stamps.assert_awaited_once()


@pytest.mark.asyncio
async def test_restart_replay_fresh_spool_not_stamped_end_to_end(db_session, printer_factory, own_session_factory):
    """End-to-end restart scenario: the donor is stamped spent pre-restart; a restart
    drops the process state (both the dedup and the edge ledger); a fresh spool is
    re-assigned to the slot; the same runout code is still live on the first frame the
    new process consumes — the fresh spool must NOT be stamped (the 18:56
    misattribution)."""
    printer = await printer_factory()
    donor = await _new_spool(db_session, weight_used=400)
    await _assign(db_session, printer.id, 0, 0, donor.id)
    await _single_feeder_item(db_session, printer.id, mapping="[0, -1, -1, -1]")
    await db_session.commit()

    await _push(printer.id, _wire(_make_state(0, 0, _tray(), tray_now=255)), own_session_factory)  # seed
    pre = _make_state(0, 0, _tray(), tray_now=255)
    pre.subtask_id = "job-1"
    await _push(printer.id, _wire(pre, _runout_err()), own_session_factory)
    await db_session.refresh(donor)
    assert donor.spent_at is not None

    # Simulate a server restart: the in-memory dedup AND the edge ledger are lost.
    spool_respool._reset_state()
    hms_edges._reset_state()

    # Operator swapped a fresh roll into the slot during the pause.
    assignment = (
        await db_session.execute(
            select(SpoolAssignment).where(
                SpoolAssignment.printer_id == printer.id,
                SpoolAssignment.ams_id == 0,
                SpoolAssignment.tray_id == 0,
            )
        )
    ).scalar_one()
    fresh = await _new_spool(db_session, weight_used=0)
    assignment.spool_id = fresh.id
    await db_session.commit()

    # The first post-restart frame still carries the runout code → it seeds, never edges.
    post = _make_state(0, 0, _tray(), tray_now=255)
    post.subtask_id = "job-1"
    assert await _push(printer.id, _wire(post, _runout_err()), own_session_factory) is None
    assert await _push(printer.id, _wire(post, _runout_err()), own_session_factory) is None

    await db_session.refresh(fresh)
    assert fresh.spent_at is None


# -- Part 2: remain-jump refill detection (reused core carries tag onto a fresh
#    roll; the gram ledger never notices) --------------------------------------


def _pure_spool(label_weight=1000, weight_used=0, spent=False):
    """A detached Spool object for the pure-helper truth-table tests (no session)."""
    return Spool(
        material="PETG",
        label_weight=label_weight,
        core_weight=250,
        weight_used=weight_used,
        spent_at=datetime.utcnow() if spent else None,
    )


def test_remain_jump_reading_true_for_reused_core_stale_ledger():
    """Production case: 958.99/1000 g used (ledger ~4%) while the tray reads
    remain=100% (a fresh roll on a reused core) → jump detected."""
    assert _remain_jump_reading(_pure_spool(1000, 958.99), _tray()) is True


def test_remain_jump_reading_true_when_over_used_ledger_clamped_to_zero():
    """weight_used > label (1850.99 on a 1000 g label) clamps ledger_pct to 0 → jump."""
    assert _remain_jump_reading(_pure_spool(1000, 1850.99), _tray()) is True


def test_remain_jump_reading_false_for_weight_locked_fresh_row():
    """A fresh row (weight_used 0 → ledger ~100%) cannot jump: remain ≤ 100, so
    remain − 100 is never ≥ 30. No weight_locked special-case needed."""
    assert _remain_jump_reading(_pure_spool(1000, 0), _tray()) is False


def test_remain_jump_reading_boundary_at_30_fires_just_under_does_not():
    """remain − ledger_pct == 30 fires (inclusive); 29.9 does not."""
    # used 300 → ledger_pct 70; remain 100 → jump exactly 30.
    assert _remain_jump_reading(_pure_spool(1000, 300), {**_tray(), "remain": 100}) is True
    # used 299 → ledger_pct 70.1 → jump 29.9 < 30.
    assert _remain_jump_reading(_pure_spool(1000, 299), {**_tray(), "remain": 100}) is False


def test_remain_jump_reading_false_for_out_of_range_or_missing_remain():
    for bad in (-1, 0, 101, 255, None, "x"):
        assert _remain_jump_reading(_pure_spool(1000, 990), {**_tray(), "remain": bad}) is False


def test_remain_jump_reading_false_for_zero_or_none_label_weight():
    for lw in (0, None):
        assert _remain_jump_reading(_pure_spool(lw, 990), _tray()) is False


def test_remain_jump_reading_false_for_invalid_tag():
    tray = _tray(tag_uid="0000000000000000", tray_uuid="00000000000000000000000000000000")
    assert _remain_jump_reading(_pure_spool(1000, 990), tray) is False


# -- Phase C: remain-jump corroboration (a single push is never evidence) ------


def test_remain_jump_single_push_does_not_qualify(fake_clock):
    """One observation of a jump proves nothing — the AMS re-reports a tray on every
    state change. The corroborated gate stays False until the window is satisfied."""
    spool = _pure_spool(1000, 958.99)
    assert _remain_jump(spool, _tray(), 1, 0, 0) is False


def test_remain_jump_two_pushes_spanning_window_qualifies(fake_clock):
    """Two pushes ≥ _JUMP_STABLE_S apart with the jump still reading → corroborated."""
    spool = _pure_spool(1000, 958.99)
    assert _remain_jump(spool, _tray(), 1, 0, 0) is False
    fake_clock["t"] += spool_respool._JUMP_STABLE_S
    assert _remain_jump(spool, _tray(), 1, 0, 0) is True


def test_remain_jump_pushes_inside_window_do_not_qualify(fake_clock):
    """Push count alone is not enough — the observations must SPAN the window, so a
    burst of pushes 1 s apart never corroborates."""
    spool = _pure_spool(1000, 958.99)
    for _ in range(5):
        assert _remain_jump(spool, _tray(), 1, 0, 0) is False
        fake_clock["t"] += 1.0


def test_remain_jump_window_restarts_when_jump_stops_reading(fake_clock):
    """The condition must HOLD: a push where the jump no longer reads drops the
    window, so the next jump starts corroborating from scratch."""
    spool = _pure_spool(1000, 958.99)
    assert _remain_jump(spool, _tray(), 1, 0, 0) is False
    fake_clock["t"] += 5.0
    # A push whose tray reports no jump (remain matches the ledger) clears the window.
    assert _remain_jump(spool, {**_tray(), "remain": 4}, 1, 0, 0) is False
    fake_clock["t"] += 6.0
    # 11 s after the FIRST observation, but this is the restarted window's first push.
    assert _remain_jump(spool, _tray(), 1, 0, 0) is False
    fake_clock["t"] += spool_respool._JUMP_STABLE_S
    assert _remain_jump(spool, _tray(), 1, 0, 0) is True


def test_remain_jump_rejected_while_identify_in_flight(fake_clock, monkeypatch):
    """A reading taken while a commanded identify is running is in flux — it neither
    fires nor counts toward corroboration."""
    from backend.app.services import ams_presence

    spool = _pure_spool(1000, 958.99)
    monkeypatch.setattr(ams_presence, "identify_in_flight", lambda *_a: True)
    for _ in range(3):
        assert _remain_jump(spool, _tray(), 1, 0, 0) is False
        fake_clock["t"] += 30.0
    # Once the identify is done, corroboration starts from zero rather than
    # inheriting the untrusted observations.
    monkeypatch.setattr(ams_presence, "identify_in_flight", lambda *_a: False)
    assert _remain_jump(spool, _tray(), 1, 0, 0) is False
    fake_clock["t"] += spool_respool._JUMP_STABLE_S
    assert _remain_jump(spool, _tray(), 1, 0, 0) is True


def test_remain_jump_rejected_while_unit_drying(fake_clock, monkeypatch):
    """Drying disengages trays and re-reports them — same untrusted-reading rule."""
    from backend.app.services import ams_presence

    spool = _pure_spool(1000, 958.99)
    monkeypatch.setattr(ams_presence, "unit_drying", lambda *_a: True)
    assert _remain_jump(spool, _tray(), 1, 0, 0) is False
    fake_clock["t"] += 60.0
    assert _remain_jump(spool, _tray(), 1, 0, 0) is False


def test_remain_jump_ledger_clears_on_slot_empty_edge(fake_clock):
    """The slot-empty edge (main.on_ams_change) drops the corroboration window with
    the prompt dedup — a new roll must re-earn its evidence."""
    spool = _pure_spool(1000, 958.99)
    assert _remain_jump(spool, _tray(), 1, 0, 0) is False
    fake_clock["t"] += 30.0
    spool_respool.clear_respool_prompt_dedup(1, 0, 0)  # slot reported empty
    assert (1, 0, 0) not in spool_respool._jump_seen
    # First push after the clear starts a fresh window instead of firing.
    assert _remain_jump(spool, _tray(), 1, 0, 0) is False


def test_remain_jump_corroborates_per_slot(fake_clock):
    """The ledger is keyed per slot — one slot's history never corroborates another."""
    spool = _pure_spool(1000, 958.99)
    assert _remain_jump(spool, _tray(), 1, 0, 0) is False
    fake_clock["t"] += 30.0
    assert _remain_jump(spool, _tray(), 1, 0, 1) is False  # different tray, own window
    assert _remain_jump(spool, _tray(), 1, 0, 0) is True


def test_should_evaluate_respool_truth_table(fake_clock):
    """spent OR a CORROBORATED jump opens the gate; a fresh/invalid-tag non-spent
    slot does not."""
    jump_tray = _tray()  # remain 100, valid tag
    # spent → True regardless of the tray (short-circuits before the jump test).
    assert should_evaluate_respool(_pure_spool(1000, 0, spent=True), {**_tray(), "remain": 0}, 1, 0, 0) is True
    # spent_at None + remain-jump → True only once corroborated across the window.
    jumping = _pure_spool(1000, 958.99)
    assert should_evaluate_respool(jumping, jump_tray, 1, 0, 0) is False
    fake_clock["t"] += spool_respool._JUMP_STABLE_S
    assert should_evaluate_respool(jumping, jump_tray, 1, 0, 0) is True
    # spent_at None + no jump (fresh row) → False.
    assert should_evaluate_respool(_pure_spool(1000, 0), jump_tray, 1, 0, 1) is False
    # spent_at None + invalid tag → False.
    assert (
        should_evaluate_respool(
            _pure_spool(1000, 958.99),
            _tray(tag_uid="0000000000000000", tray_uuid="00000000000000000000000000000000"),
            1,
            0,
            2,
        )
        is False
    )


@pytest.mark.asyncio
async def test_gate_remain_jump_logs_even_above_threshold(db_session, printer_factory, monkeypatch, fake_clock, caplog):
    """spent_at NULL and remaining ABOVE the near-empty threshold, but the tray
    reports a CORROBORATED remain-jump on a physically-cycled slot → the Tier-3
    OBSERVATION fires, reasoned ``remain_jump``. The corroboration state machine is
    the part that must not regress: it is stateful, so the demotion kept the
    original short-circuit (the jump is consulted only when near-empty did not
    already answer) and the first push still only opens the window."""
    printer = await printer_factory()
    donor = await _make_donor(db_session, spent=False, weight_used=958.99)  # remaining 41 > 30
    await db_session.commit()
    _record_physical_cycle(printer.id)

    _patch_pm(monkeypatch, _make_state(0, 0, _tray()))
    broadcasts = _spy_broadcast(monkeypatch)
    with caplog.at_level("INFO", logger="backend.app.services.spool_respool"):
        # First push only opens the corroboration window.
        assert await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, _tray(), donor) is None
        assert [r for r in caplog.records if "Re-spool heuristic fired" in r.getMessage()] == []

        fake_clock["t"] += spool_respool._JUMP_STABLE_S
        result = await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, _tray(), donor)

    assert result is None  # neither a prompt nor an auto-respool
    assert [b for b in broadcasts if b["type"] == "respool_prompt"] == []
    observations = [r for r in caplog.records if "Re-spool heuristic fired" in r.getMessage()]
    assert len(observations) == 1
    assert "reason=remain_jump" in observations[0].getMessage()


@pytest.mark.asyncio
async def test_gate_remain_jump_suppressed_when_dismissed(db_session, printer_factory, monkeypatch, fake_clock):
    """The durable dismissal still suppresses a remain-jump prompt (both routes
    share the respool_dismissed_at gate)."""
    printer = await printer_factory()
    donor = await _make_donor(db_session, spent=False, weight_used=958.99)
    donor.respool_dismissed_at = datetime.utcnow()
    await db_session.commit()
    _record_physical_cycle(printer.id)

    _patch_pm(monkeypatch, _make_state(0, 0, _tray()))
    broadcasts = _spy_broadcast(monkeypatch)
    await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, _tray(), donor)
    fake_clock["t"] += spool_respool._JUMP_STABLE_S
    result = await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, _tray(), donor)

    assert result is None
    assert [b for b in broadcasts if b["type"] == "respool_prompt"] == []


# -- Phase C: Tier-3 evidence gates (the two false "reused tag" popups) --------


@pytest.mark.asyncio
async def test_near_empty_without_swap_evidence_never_prompts(db_session, printer_factory, monkeypatch):
    """THE regression pin. A near-empty spool nobody has touched raises NOTHING.

    Production 2026-07-20: 13 rows sat at ≤50 g remaining, every one of them a
    standing "A reused Bambu tag was detected…" modal waiting for the next AMS push,
    on a farm that reuses no tags. Being printed down is not evidence that the roll
    changed — only a physical cycle on the slot is.
    """
    printer = await printer_factory()
    donor = await _make_donor(db_session, spent=False, weight_used=990.0)  # remaining 10 <= 30
    await db_session.commit()
    # No _record_physical_cycle: the slot has not been touched.

    _patch_pm(monkeypatch, _make_state(0, 0, _tray()))
    broadcasts = _spy_broadcast(monkeypatch)
    result = await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, _tray(), donor)

    assert result is None
    assert [b for b in broadcasts if b["type"] == "respool_prompt"] == []


@pytest.mark.asyncio
async def test_near_empty_with_recent_cycle_logs_as_near_empty(db_session, printer_factory, monkeypatch, caplog):
    """The same spool DOES reach the observation once a roll was physically cycled
    through the slot, reasoned ``near_empty`` — the evidence line states the grams,
    the label and the threshold, so a future re-promotion can argue from data
    instead of from the absence of it."""
    printer = await printer_factory()
    donor = await _make_donor(db_session, spent=False, weight_used=990.0)
    await db_session.commit()
    _record_physical_cycle(printer.id)

    _patch_pm(monkeypatch, _make_state(0, 0, _tray()))
    broadcasts = _spy_broadcast(monkeypatch)
    with caplog.at_level("INFO", logger="backend.app.services.spool_respool"):
        await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, _tray(), donor)

    assert [b for b in broadcasts if b["type"] == "respool_prompt"] == []
    msg = next(r for r in caplog.records if "Re-spool heuristic fired" in r.getMessage()).getMessage()
    assert "reason=near_empty" in msg
    assert "remaining=10.0g of a 1000.0g label" in msg


@pytest.mark.asyncio
async def test_stale_physical_cycle_is_not_swap_evidence(db_session, printer_factory, monkeypatch):
    """Evidence expires: a cycle older than _RESPOOL_SWAP_EVIDENCE_S no longer
    explains a prompt now (otherwise one desiccant check would arm the slot for good)."""
    printer = await printer_factory()
    donor = await _make_donor(db_session, spent=False, weight_used=990.0)
    await db_session.commit()
    _record_physical_cycle(printer.id, age_s=spool_respool._RESPOOL_SWAP_EVIDENCE_S + 1)

    _patch_pm(monkeypatch, _make_state(0, 0, _tray()))
    broadcasts = _spy_broadcast(monkeypatch)
    await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, _tray(), donor)

    assert [b for b in broadcasts if b["type"] == "respool_prompt"] == []


@pytest.mark.asyncio
async def test_swap_evidence_is_per_slot(db_session, printer_factory, monkeypatch):
    """A cycle on a NEIGHBOURING slot is not evidence about this one."""
    printer = await printer_factory()
    donor = await _make_donor(db_session, spent=False, weight_used=990.0)
    await db_session.commit()
    _record_physical_cycle(printer.id, tray_id=1)  # the other slot was touched

    _patch_pm(monkeypatch, _make_state(0, 0, _tray()))
    broadcasts = _spy_broadcast(monkeypatch)
    await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, _tray(), donor)

    assert [b for b in broadcasts if b["type"] == "respool_prompt"] == []


@pytest.mark.asyncio
async def test_corrupt_ledger_warns_and_never_prompts(db_session, printer_factory, monkeypatch, caplog):
    """The live donor shape: label 1000 g, weight_used 1243 g ⇒ −243 g remaining.

    An impossible row is REPORTED, never prompted — and nothing is auto-corrected
    (operator decision 2026-07-20: the offline repair tool owns the data).
    """
    printer = await printer_factory()
    donor = await _make_donor(db_session, spent=False, weight_used=1243.0)
    await db_session.commit()
    _record_physical_cycle(printer.id)  # evidence present — the CORRUPTION is what blocks

    _patch_pm(monkeypatch, _make_state(0, 0, _tray()))
    broadcasts = _spy_broadcast(monkeypatch)
    with caplog.at_level("WARNING", logger="backend.app.services.spool_respool"):
        result = await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, _tray(), donor)

    assert result is None
    assert [b for b in broadcasts if b["type"] == "respool_prompt"] == []
    warnings = [
        r for r in caplog.records if r.levelname == "WARNING" and r.name == "backend.app.services.spool_respool"
    ]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert str(donor.id) in message and "1243" in message and "-243" in message
    assert "AMS0-T0" in message  # the slot is named

    # No auto-correction of any column.
    refreshed = await db_session.get(Spool, donor.id)
    assert refreshed.weight_used == pytest.approx(1243.0)
    assert refreshed.label_weight == 1000
    assert refreshed.spent_at is None
    assert refreshed.respool_dismissed_at is None


@pytest.mark.asyncio
async def test_zero_label_with_charged_grams_is_corrupt(db_session, printer_factory, monkeypatch):
    """A 0 label carrying charged grams computes negative remaining too — same class
    of impossible row, same suppression, no auto-correction.

    (A NULL label cannot reach this path from the DB — ``spool.label_weight`` is NOT
    NULL — but an in-memory row can, so :func:`_ledger_corrupt` handles both; the
    NULL arm is pinned in :func:`test_ledger_corrupt_treats_absent_label_as_corrupt`.)
    """
    printer = await printer_factory()
    donor = await _make_donor(db_session, spent=False, weight_used=120.0)
    donor.label_weight = 0
    await db_session.commit()
    _record_physical_cycle(printer.id)

    _patch_pm(monkeypatch, _make_state(0, 0, _tray()))
    broadcasts = _spy_broadcast(monkeypatch)
    assert await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, _tray(), donor) is None
    assert [b for b in broadcasts if b["type"] == "respool_prompt"] == []
    refreshed = await db_session.get(Spool, donor.id)
    assert refreshed.weight_used == pytest.approx(120.0) and refreshed.label_weight == 0  # untouched


def test_ledger_corrupt_treats_absent_label_as_corrupt():
    """No label but grams charged against it → remaining computes negative → corrupt.
    An unused row with no label is merely unknown, not corrupt."""
    assert spool_respool._ledger_corrupt(_pure_spool(None, 120)) is True
    assert spool_respool._ledger_corrupt(_pure_spool(0, 120)) is True
    assert spool_respool._ledger_corrupt(_pure_spool(None, 0)) is False


def test_ledger_corrupt_tolerance_boundary():
    """Ordinary over-charge rounding inside the tolerance is NOT corruption (it stays
    an ordinary near-empty row); beyond it, the row is impossible."""
    assert spool_respool._ledger_corrupt(_pure_spool(1000, 1000 + spool_respool._LEDGER_CORRUPT_TOL_G)) is False
    assert spool_respool._ledger_corrupt(_pure_spool(1000, 1000 + spool_respool._LEDGER_CORRUPT_TOL_G + 0.1)) is True
    assert spool_respool._ledger_corrupt(_pure_spool(1000, 990)) is False


# -- F2: fire-once respool_prompt re-broadcast on (re)connect -----------------


async def _fire_live_prompt(db, printer, monkeypatch, *, weight_used=990.0):
    """Fire a real prompt so _respool_prompt_dedup is populated exactly as the live
    gate populates it.

    Since 2026-08-19 (operator ruling 3) tier 2 CONCLUDES on a spent ∧ loaded arrival
    instead of asking, so the only way the live gate still arms a prompt is the
    ESCALATION arm: the conclusion is correct but cannot be EXECUTED. Both brand sources
    are cleared here to produce exactly that — no ``respool_last_brand``, no
    ``tagless_default_filament`` — because a re-spool with no brand to mint with is the
    one thing tier 2 will not invent. Arming through the path that can actually arm it is
    the point of this helper; a hand-stuffed dedup entry would pin nothing.
    Returns (donor, broadcasts_spy)."""
    from backend.app.api.routes.settings import set_setting

    await set_setting(db, "respool_last_brand", "")
    await set_setting(db, "tagless_default_filament", "")
    donor = await _make_donor(db, spent=True, weight_used=weight_used)  # remaining 10
    await db.commit()
    _patch_pm(monkeypatch, _make_state(0, 0, _tray(state=11)))
    broadcasts = _spy_broadcast(monkeypatch)
    await maybe_auto_or_prompt_respool(db, printer.id, 0, 0, _tray(state=11), donor)
    assert [b for b in broadcasts if b["type"] == "respool_prompt"]  # dedup now armed
    return donor, broadcasts


def _capture_send():
    sent: list[dict] = []

    async def _send(payload):
        sent.append(payload)

    return sent, _send


@pytest.mark.asyncio
async def test_rebroadcast_replays_unresolved_prompt(db_session, printer_factory, monkeypatch):
    """A client that missed the fire-once prompt gets it replayed on (re)connect."""
    printer = await printer_factory()
    donor, _ = await _fire_live_prompt(db_session, printer, monkeypatch)

    sent, send = _capture_send()
    n = await rebroadcast_unresolved_respool_prompts(db_session, send)

    assert n == 1
    assert len(sent) == 1
    assert sent[0]["type"] == "respool_prompt"
    assert sent[0]["donor_spool_id"] == donor.id
    assert sent[0]["donor_remaining_g"] == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_rebroadcast_payload_matches_live_prompt(db_session, printer_factory, monkeypatch):
    """The replayed payload is identical to the live gate's payload (one contract).

    The ``*_age_s`` fields are excluded by construction, not by convenience: they
    are now-relative by design (``_build_respool_prompt_payload`` says so), so a
    replay measured later MUST differ there and nowhere else."""
    printer = await printer_factory()
    _donor, broadcasts = await _fire_live_prompt(db_session, printer, monkeypatch)
    live = next(b for b in broadcasts if b["type"] == "respool_prompt")

    sent, send = _capture_send()
    await rebroadcast_unresolved_respool_prompts(db_session, send)

    def _frozen(payload):
        return {k: v for k, v in payload.items() if not k.endswith("_age_s")}

    assert _frozen(sent[0]) == _frozen(live)
    # The excluded fields still have to BE there, and to have advanced rather than
    # gone missing or reset.
    age_keys = [k for k in live if k.endswith("_age_s")]
    assert age_keys
    for k in age_keys:
        assert sent[0][k] >= live[k]


@pytest.mark.asyncio
async def test_rebroadcast_recomputes_payload_from_durable_state(db_session, printer_factory, monkeypatch):
    """The replayed prompt is rebuilt from DURABLE state rather than replayed from a
    cached copy, so a reconnecting client sees the row as it is NOW — not as it was
    when the live prompt fired."""
    printer = await printer_factory()
    donor, broadcasts = await _fire_live_prompt(db_session, printer, monkeypatch)
    assert next(b for b in broadcasts if b["type"] == "respool_prompt")["trigger"] == "spent"

    sent, send = _capture_send()
    await rebroadcast_unresolved_respool_prompts(db_session, send)
    assert sent[0]["trigger"] == "spent"
    assert sent[0]["donor_remaining_g"] == pytest.approx(10.0)

    donor.weight_used = 950.0  # the ledger moved while the client was away
    await db_session.commit()

    sent2, send2 = _capture_send()
    await rebroadcast_unresolved_respool_prompts(db_session, send2)
    assert sent2[0]["trigger"] == "spent"
    assert sent2[0]["donor_remaining_g"] == pytest.approx(50.0)


@pytest.mark.asyncio
async def test_rebroadcast_skips_dismissed_donor(db_session, printer_factory, monkeypatch):
    """The dismissal route stamps respool_dismissed_at WITHOUT clearing the in-memory
    dedup — the replay must still suppress a dismissed prompt (F2 correctness)."""
    printer = await printer_factory()
    donor, _ = await _fire_live_prompt(db_session, printer, monkeypatch)
    donor.respool_dismissed_at = datetime.utcnow()
    await db_session.commit()

    sent, send = _capture_send()
    n = await rebroadcast_unresolved_respool_prompts(db_session, send)
    assert n == 0 and sent == []


@pytest.mark.asyncio
async def test_rebroadcast_skips_archived_donor(db_session, printer_factory, monkeypatch):
    """A re-spooled / archived donor is not replayed."""
    printer = await printer_factory()
    donor, _ = await _fire_live_prompt(db_session, printer, monkeypatch)
    donor.archived_at = datetime.utcnow()
    await db_session.commit()

    sent, send = _capture_send()
    assert await rebroadcast_unresolved_respool_prompts(db_session, send) == 0
    assert sent == []


@pytest.mark.asyncio
async def test_rebroadcast_skips_when_slot_no_longer_holds_tag(db_session, printer_factory, monkeypatch):
    """A slot now empty (or holding a different tag) is stale → no replay."""
    printer = await printer_factory()
    await _fire_live_prompt(db_session, printer, monkeypatch)
    _patch_pm(monkeypatch, _make_state(0, 0, _tray(tray_type="")))  # slot went empty

    sent, send = _capture_send()
    assert await rebroadcast_unresolved_respool_prompts(db_session, send) == 0
    assert sent == []


@pytest.mark.asyncio
async def test_rebroadcast_no_entries_sends_nothing(db_session, printer_factory):
    """No unresolved prompts tracked → nothing replayed."""
    await printer_factory()
    sent, send = _capture_send()
    assert await rebroadcast_unresolved_respool_prompts(db_session, send) == 0
    assert sent == []


@pytest.mark.asyncio
async def test_rebroadcast_noop_in_spoolman_mode(db_session, printer_factory, monkeypatch):
    """Spoolman owns the lifecycle → the replay hook is a no-op even with a dedup entry."""
    printer = await printer_factory()
    await _fire_live_prompt(db_session, printer, monkeypatch)
    from backend.app.api.routes.settings import set_setting

    await set_setting(db_session, "spoolman_enabled", "true")
    await db_session.commit()

    sent, send = _capture_send()
    assert await rebroadcast_unresolved_respool_prompts(db_session, send) == 0
    assert sent == []


# -- WS3: Tier 2 CONCLUDES on a reused core; it never asks --------------------


@pytest.mark.asyncio
async def test_gate_spent_loaded_concludes_without_asking(db_session, printer_factory, monkeypatch):
    """The INVERSION of the old ``respool_auto_enabled`` quarantine (operator ruling 3).

    This test used to be ``test_gate_spent_loaded_prompts_when_auto_disabled`` and pinned
    the toggle's OFF default: a spent+loaded tag arrival broadcast the one-click prompt
    instead of minting. The toggle is deleted. A FINISHED roll
    (``Spool.is_finished_roll``) reading LOADED is a physical impossibility unless the
    filament is on a different roll, so the tier concludes: the donor is disposed and a
    fresh full row carrying the tag takes the slot, with no question asked.

    Still pinned from the original: the hardware-certain path needs no Phase-C physical
    evidence — no cycle is recorded here and it still fires, because the runout IS the
    hardware event.
    """
    printer = await printer_factory()
    donor = await _make_donor(db_session, spent=True)
    from backend.app.api.routes.settings import set_setting

    await set_setting(db_session, "respool_last_brand", "Polymaker")
    await db_session.commit()

    _patch_pm(monkeypatch, _make_state(0, 0, _tray(state=11)))
    broadcasts = _spy_broadcast(monkeypatch)
    result = await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, _tray(state=11), donor)

    assert result is not None and result is not donor  # concluded: a fresh re-spooled row
    assert result.tag_type == RESPOOL_TAG_TYPE
    assert result.weight_used == 0
    assert [b for b in broadcasts if b["type"] == "respool_prompt"] == []  # nothing asked


@pytest.mark.asyncio
async def test_gate_spent_but_not_loaded_still_concludes_nothing(db_session, printer_factory, monkeypatch):
    """LIVENESS PAIR — "reading LOADED" is half the evidence and it is still enforced.

    A spent row re-inserted with no filament fed is a dead roll back in its slot, not a
    reused core: the tier stands down exactly as before. Deleting the toggle widened WHEN
    the tier concludes, never WHAT counts as evidence."""
    printer = await printer_factory()
    donor = await _make_donor(db_session, spent=True)
    from backend.app.api.routes.settings import set_setting

    await set_setting(db_session, "respool_last_brand", "Polymaker")
    await db_session.commit()

    _patch_pm(monkeypatch, _make_state(0, 0, _tray(state=9)))
    broadcasts = _spy_broadcast(monkeypatch)
    result = await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, _tray(state=9), donor)

    assert result is None
    assert [b for b in broadcasts if b["type"] == "respool_prompt"] == []
    assert (await db_session.get(Spool, donor.id)).archived_at is None  # donor untouched


# -- W3: firmware slot attribution outranks tray_now/mapping inference ----------


@pytest.mark.asyncio
async def test_resolve_prefers_decoded_hms_slot_over_tray_now(db_session, printer_factory):
    """A live 0700_2X00 runout HMS naming AMS0 slot3 (global tray 2) stamps THAT
    spool even while tray_now and the mapping both point at tray 0."""
    printer = await printer_factory()
    at_tray0 = await _new_spool(db_session, weight_used=100)
    at_tray2 = await _new_spool(db_session, weight_used=400)
    await _assign(db_session, printer.id, 0, 0, at_tray0.id)
    await _assign(db_session, printer.id, 0, 2, at_tray2.id)
    await _single_feeder_item(db_session, printer.id, mapping="[0, -1, -1, -1]")
    await db_session.commit()

    state = _make_state(0, 0, _tray(), tray_now=0)  # tray_now/mapping both say tray 0
    state.subtask_id = "job-hms"
    # 0700_8011 trigger + the slot-naming fault (attr 0x07002200, code 0x20001 → AMS0 slot2).
    state.hms_errors = [
        HMSError(code="8011", attr=0x07000000, module=7, severity=2),
        HMSError(code="0x20001", attr=0x07002200, module=7, severity=2),
    ]
    marked = await mark_spent_on_runout(db_session, printer.id, {"0700_8011"}, state)

    assert marked is not None and marked.id == at_tray2.id  # firmware-named slot won
    assert (await db_session.get(Spool, at_tray0.id)).spent_at is None  # tray_now target untouched


@pytest.mark.asyncio
async def test_resolve_falls_back_to_tray_now_on_8011_only(db_session, printer_factory):
    """The slot-agnostic 0700_8011 runout (no slot-naming HMS) falls back to the
    live tray_now inference."""
    printer = await printer_factory()
    at_tray1 = await _new_spool(db_session, weight_used=400)
    await _assign(db_session, printer.id, 0, 1, at_tray1.id)
    await db_session.commit()

    state = _make_state(0, 1, _tray(), tray_now=1)
    state.subtask_id = "job-8011"
    state.hms_errors = [HMSError(code="8011", attr=0x07000000, module=7, severity=2)]  # no slot attribution
    marked = await mark_spent_on_runout(db_session, printer.id, {"0700_8011"}, state)

    assert marked is not None and marked.id == at_tray1.id  # tray_now fallback used


@pytest.mark.asyncio
async def test_resolve_8011_prefers_current_demand_over_first_decode_hit(db_session, printer_factory):
    """006-H2S list shape: an OLDER auto-switched slot-1 entry sits ahead of the NEWER
    slot-3 demand (firmware APPENDS). First-hit decode returns the oldest — the slot
    the auto-switch trigger has already stamped — so ``_spent_dedup`` would swallow the
    stamp and the actually-unrescued roll would never be marked. The standing DEMAND is
    the unrescued slot and must win."""
    printer = await printer_factory()
    rescued = await _new_spool(db_session, weight_used=990)  # slot 1 = tray 0, already switched away from
    unrescued = await _new_spool(db_session, weight_used=980)  # slot 3 = tray 2, the one held for
    await _assign(db_session, printer.id, 0, 0, rescued.id)
    await _assign(db_session, printer.id, 0, 2, unrescued.id)
    await db_session.commit()

    state = _make_state(0, 0, _tray(), gcode_state="PAUSE", tray_now=0)
    state.subtask_id = "job-chained"
    state.hms_errors = [
        _slot_runout_err(attr=0x07002000, code=0x00030002),  # older: slot 1 auto-switched
        _slot_runout_err(attr=0x07002200, code=0x00020001),  # newer: slot 3 demand (standing)
        HMSError(code="8011", attr=0x07000000, module=7, severity=2),  # the trigger
    ]
    marked = await mark_spent_on_runout(db_session, printer.id, {"0700_8011"}, state)

    assert marked is not None and marked.id == unrescued.id  # the demanded slot
    assert (await db_session.get(Spool, rescued.id)).spent_at is None  # first-hit target untouched


# -- Fix 1: the firmware AUTO-SWITCH runout (a RESCUED runout stamps spent too) --
# ``RUNOUT_HMS_CODES`` is the UNRESCUED vocabulary, so a successful AMS auto-refill
# raised only the slot-attributed 0700_2X00 family and stamped nothing (fleet evidence
# 2026-07-30: four confirmed auto-refills, zero stamps). The un-stamped rows then
# surfaced as spurious "Fresh roll?" prompts on the eventual physical swap.


def _slot_runout_err(*, attr: int, code: int) -> HMSError:
    """A slot-attributed runout HMSError carrying the LOSSLESS ``full_code`` the new
    family is identified by (the short code drops both the code-word high bits and the
    slot byte, so it cannot tell 0x00020001 from 0x00030001, or slot 0 from slot 3)."""
    return HMSError(
        code=hex(code),
        attr=attr,
        module=7,
        severity=2,
        full_code=f"{attr:08X}{code:08X}",
    )


def _slot_event(err: HMSError) -> tuple[str, int, int]:
    """The ``(full_code, attr, code_word)`` tuple main's pipeline hands the trigger."""
    return (err.full_code, err.attr, int(err.code, 16))


@pytest.mark.asyncio
async def test_slot_runout_auto_switch_stamps_decoded_slot(db_session, printer_factory):
    """The 009-H2S 2026-07-30 08:32 shape: 0x00030002 on attr 0x07002200 (AMS0 slot 3 =
    tray 2) while the print keeps RUNNING off the backup slot. The attr names the
    exhausted slot outright — tray_now points at the slot now FEEDING, so inference
    would stamp the wrong (healthy) roll."""
    printer = await printer_factory()
    feeding = await _new_spool(db_session, weight_used=100)  # the backup that took over
    exhausted = await _new_spool(db_session, weight_used=970)
    await _assign(db_session, printer.id, 0, 0, feeding.id)
    await _assign(db_session, printer.id, 0, 2, exhausted.id)
    await db_session.commit()

    state = _make_state(0, 2, _tray(), gcode_state="RUNNING", tray_now=0)  # feeding elsewhere
    state.subtask_id = "job-auto"
    err = _slot_runout_err(attr=0x07002200, code=0x00030002)
    state.hms_errors = [err]

    stamped = await mark_spent_on_slot_runout(db_session, printer.id, [_slot_event(err)], state)

    assert [s.id for s in stamped] == [exhausted.id]
    assert (await db_session.get(Spool, exhausted.id)).spent_at is not None
    assert (await db_session.get(Spool, feeding.id)).spent_at is None  # tray_now target untouched


@pytest.mark.asyncio
async def test_slot_runout_chained_multi_slot_each_stamps(db_session, printer_factory):
    """The 005-H2S 2026-07-30 three-roll print: several slots run dry inside ONE job.
    Attribution is per EVENT, so distinct attrs stamp distinct spools — resolving once
    per push (or via tray_now) would stamp only one of them."""
    printer = await printer_factory()
    at_t0 = await _new_spool(db_session, weight_used=950)
    at_t1 = await _new_spool(db_session, weight_used=960)
    await _assign(db_session, printer.id, 0, 0, at_t0.id)
    await _assign(db_session, printer.id, 0, 1, at_t1.id)
    await db_session.commit()

    state = _make_state(0, 0, _tray(), gcode_state="RUNNING", tray_now=2)
    state.subtask_id = "job-chained"  # ONE job — the dedup key must still separate them
    e0 = _slot_runout_err(attr=0x07002000, code=0x00030002)  # AMS0 slot 1 → tray 0
    e1 = _slot_runout_err(attr=0x07002100, code=0x00030002)  # AMS0 slot 2 → tray 1
    state.hms_errors = [e0, e1]

    stamped = await mark_spent_on_slot_runout(db_session, printer.id, [_slot_event(e0), _slot_event(e1)], state)

    assert sorted(s.id for s in stamped) == sorted([at_t0.id, at_t1.id])
    assert (await db_session.get(Spool, at_t0.id)).spent_at is not None
    assert (await db_session.get(Spool, at_t1.id)).spent_at is not None


@pytest.mark.asyncio
async def test_bare_demand_still_never_stamps(db_session, printer_factory):
    """006-H2S latched-load pin, kept after the 2026-08-13 vocabulary promotion.

    Every OTHER member of the slot-attributed family decodes a slot perfectly well, so
    only the code-word gate stops them: a bare demand can be a firmware-latched bogus ask
    for a slot that never ran dry, and purge-abnormal is entangled with a tool-head fault
    where the runout read itself may be wrong. (The pull-back notice 0x00030001 LEFT this
    list on 2026-08-13 — it is the firmware's own per-event statement that the roll
    ended, and the only one a terminal runout raises.) Pinned at BOTH layers — the
    trigger's own re-assert and main's pipeline filter."""
    from backend.app.services.hms_errors import _RUNOUT_SLOT_SPENT_CODE32, _code_word, runout_slot_from_hms

    printer = await printer_factory()
    healthy = await _new_spool(db_session, weight_used=300)
    await _assign(db_session, printer.id, 0, 2, healthy.id)
    await db_session.commit()

    state = _make_state(0, 2, _tray(), gcode_state="PAUSE", tray_now=255)
    state.subtask_id = "job-latched"
    errs = [
        _slot_runout_err(attr=0x07002200, code=0x00020001),  # bare demand
        _slot_runout_err(attr=0x07002200, code=0x00020005),  # purge-abnormal
    ]

    # Layer 1 — main's pipeline filter would never build an event for either of them,
    # even though each decodes to a real slot (so the gate is the code word alone).
    for err in errs:
        code_word = _code_word(err.code)
        assert runout_slot_from_hms(err.attr, code_word) is not None
        assert code_word not in _RUNOUT_SLOT_SPENT_CODE32

    # Layer 2 — handed to the trigger anyway, it fails closed on its own contract.
    stamped = await mark_spent_on_slot_runout(db_session, printer.id, [_slot_event(e) for e in errs], state)

    assert stamped == []
    assert (await db_session.get(Spool, healthy.id)).spent_at is None


@pytest.mark.asyncio
async def test_pullback_word_0x30001_stamps_per_event(db_session, printer_factory):
    """003-H2S 2026-08-13, slot 4: a TERMINAL runout (no backup slot left) raises the
    pull-back word 0x00030001 ONCE, slot-attributed, and then only the excluded bare
    demand and the latching slot-agnostic 8011. Promoting it is what gives the terminal
    class any per-event evidence at all — the auto-switch report structurally cannot
    fire when the slot that ran dry was the last eligible one."""
    printer = await printer_factory()
    exhausted = await _new_spool(db_session, weight_used=900)
    healthy = await _new_spool(db_session, weight_used=100)
    await _assign(db_session, printer.id, 0, 3, exhausted.id)  # AMS0 slot 4 → tray 3
    await _assign(db_session, printer.id, 0, 0, healthy.id)
    await db_session.commit()

    state = _make_state(0, 3, _tray(), gcode_state="PAUSE", tray_now=3)
    state.subtask_id = "job-terminal"
    err = _slot_runout_err(attr=0x07002300, code=0x00030001)
    state.hms_errors = [err]

    stamped = await mark_spent_on_slot_runout(db_session, printer.id, [_slot_event(err)], state)

    assert [s.id for s in stamped] == [exhausted.id]
    assert (await db_session.get(Spool, exhausted.id)).spent_at is not None
    assert (await db_session.get(Spool, healthy.id)).spent_at is None  # attr-exact, never fleet-wide


@pytest.mark.asyncio
async def test_pullback_then_auto_switch_same_slot_stamps_once(db_session, printer_factory):
    """The RESCUED sequence: 0x30001 ("pulling the old filament back") is followed by
    0x30002 ("switched to the slot with the same filament") naming the SAME slot. Two
    words, one physical exhaustion — ``_spent_dedup`` is keyed per (printer, job, tray),
    so the second is absorbed and the stamp time never moves."""
    printer = await printer_factory()
    exhausted = await _new_spool(db_session, weight_used=970)
    await _assign(db_session, printer.id, 0, 2, exhausted.id)
    await db_session.commit()

    state = _make_state(0, 2, _tray(), gcode_state="RUNNING", tray_now=0)  # backup already feeding
    state.subtask_id = "job-rescued"
    pullback = _slot_runout_err(attr=0x07002200, code=0x00030001)
    switched = _slot_runout_err(attr=0x07002200, code=0x00030002)

    first = await mark_spent_on_slot_runout(db_session, printer.id, [_slot_event(pullback)], state)
    assert [s.id for s in first] == [exhausted.id]
    stamped_at = first[0].spent_at

    second = await mark_spent_on_slot_runout(db_session, printer.id, [_slot_event(switched)], state)

    assert second == []  # dedup absorbed it — not a second stamp
    assert (await db_session.get(Spool, exhausted.id)).spent_at == stamped_at


# -- WS2b: the DURABLE, incident-anchored stamp for HELD runouts ---------------
#
# Every lane above rides an ``hms_edges`` APPEARANCE edge, and those are ephemeral by
# construction: the first frame a process consumes seeds instead of edging, so a deploy
# landing inside a hold re-seeds the standing runout and no later frame can ever edge it.
# A terminal runout holds for hours (003-H2S 2026-08-13: 12.5 h), so the durable event is
# the recovery ESCALATION, and it carries its own stamp. These pin what that lane may
# conclude — and, just as load-bearing, what it must refuse.


def _held_runout_state(tray_dict, *, tray_id=3, subtask_id="job-held"):
    """A PAUSEd printer standing on a same-slot refill demand for AMS0 slot ``tray_id``,
    with that slot rendered as ``tray_dict`` — the one input the 006 stand-down reads.

    The DEMAND is what the resolver answers from (step 1, ahead of any inference), which
    is exactly the provenance the 006 class poisons: firmware can latch a demand for a
    slot that never ran dry, so the slot alone is never enough to stamp on.
    """
    state = _make_state(0, tray_id, tray_dict, gcode_state="PAUSE", tray_now=tray_id)
    state.subtask_id = subtask_id
    state.hms_errors = [_slot_runout_err(attr=0x07000000 | ((0x20 + tray_id) << 8), code=0x00020001)]
    return state


_CLEARED_TRAY = {"state": 9, "tray_type": "", "tag_uid": "", "tray_uuid": ""}
_SEATED_TRAY = {"state": 11, "tray_type": "PETG", "tag_uid": DONOR_TAG_UID, "tray_uuid": DONOR_TRAY_UUID}
_SILENT_TRAY: dict = {}  # a partial push asserting neither state nor type → presence UNKNOWN


@pytest.mark.asyncio
async def test_runout_hold_stamp_stamps_wire_empty_slot(db_session, printer_factory, own_session_factory, caplog):
    """The 003-H2S shape: the demanded slot reads wire-asserted EMPTY (its bay cleared
    minutes before the firmware admitted why), and the roll that was released from it is
    the victim — resolved through WS1's tier 2, not a live binding."""
    printer = await printer_factory()
    spool = await _seed_released_row(db_session, printer.id, 0, 3, weight_used=990.0)

    with caplog.at_level(logging.INFO, logger=_RESPOOL_LOGGER):
        await spool_respool.mark_spent_on_runout_hold(
            printer.id, _held_runout_state(_CLEARED_TRAY), subtask_id="job-held", session_factory=own_session_factory
        )

    await db_session.refresh(spool)
    assert spool.spent_at is not None
    assert spool.weight_used == 990.0  # the gram ledger stays raw
    assert any("tier=last_location" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_runout_hold_stamp_stands_down_on_loaded_slot(db_session, printer_factory, own_session_factory, caplog):
    """006-H2S 2026-07-26: a latched demand can name a slot that never ran dry. A roll
    that truly ran out left its bay minutes ago, so an OCCUPIED demanded slot is the
    bogus-latch shape — and stamping it would archive a healthy roll permanently."""
    printer = await printer_factory()
    spool = await _new_spool(db_session, weight_used=300)
    await _assign(db_session, printer.id, 0, 3, spool.id)
    await db_session.commit()

    with caplog.at_level(logging.INFO, logger=_RESPOOL_LOGGER):
        await spool_respool.mark_spent_on_runout_hold(
            printer.id, _held_runout_state(_SEATED_TRAY), subtask_id="job-held", session_factory=own_session_factory
        )

    await db_session.refresh(spool)
    assert spool.spent_at is None
    assert any("OCCUPIED" in r.getMessage() and "bogus-latch" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_runout_hold_stamp_stands_down_on_unknown_presence(
    db_session, printer_factory, own_session_factory, caplog
):
    """Unknown is not empty. A partial push that asserts neither state nor tray_type
    proves nothing about the bay, and this lane exists to cover the case the edges miss —
    not to guess where they were silent. Its own log, distinct from the 006 refusal."""
    printer = await printer_factory()
    spool = await _seed_released_row(db_session, printer.id, 0, 3, weight_used=990.0)

    with caplog.at_level(logging.INFO, logger=_RESPOOL_LOGGER):
        await spool_respool.mark_spent_on_runout_hold(
            printer.id, _held_runout_state(_SILENT_TRAY), subtask_id="job-held", session_factory=own_session_factory
        )

    await db_session.refresh(spool)
    assert spool.spent_at is None
    assert any("presence is UNKNOWN" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_runout_hold_stamp_noop_in_spoolman_mode(db_session, printer_factory, own_session_factory):
    """Spoolman owns the ledger — every spent lane is gated off, this one included."""
    from backend.app.api.routes.settings import set_setting

    printer = await printer_factory()
    spool = await _seed_released_row(db_session, printer.id, 0, 3, weight_used=990.0)
    await set_setting(db_session, "spoolman_enabled", "true")
    await db_session.commit()

    await spool_respool.mark_spent_on_runout_hold(
        printer.id, _held_runout_state(_CLEARED_TRAY), subtask_id="job-held", session_factory=own_session_factory
    )

    await db_session.refresh(spool)
    assert spool.spent_at is None


@pytest.mark.asyncio
async def test_runout_hold_stamp_dedups_with_edge_lane(db_session, printer_factory, own_session_factory, caplog):
    """One physical exhaustion, two triggers: the edge fires at the runout instant and
    the escalation follows minutes later on the same job and slot. ``_spent_dedup`` is
    shared, so the second is a no-op that says so — the stamp time never moves."""
    printer = await printer_factory()
    spool = await _new_spool(db_session, weight_used=970)
    await _assign(db_session, printer.id, 0, 3, spool.id)
    await db_session.commit()

    edge_state = _held_runout_state(_CLEARED_TRAY)
    pullback = _slot_runout_err(attr=0x07002300, code=0x00030001)
    stamped = await mark_spent_on_slot_runout(db_session, printer.id, [_slot_event(pullback)], edge_state)
    assert [s.id for s in stamped] == [spool.id]
    stamped_at = stamped[0].spent_at

    with caplog.at_level(logging.INFO, logger=_RESPOOL_LOGGER):
        await spool_respool.mark_spent_on_runout_hold(
            printer.id, _held_runout_state(_CLEARED_TRAY), subtask_id="job-held", session_factory=own_session_factory
        )

    await db_session.refresh(spool)
    assert spool.spent_at == stamped_at  # no re-stamp
    assert any("the edge lane got there first" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_slot_runout_live_on_the_first_consumed_frame_never_stamps(
    db_session, printer_factory, own_session_factory
):
    """Restart replay, Lane B: a code live on the first frame the process consumes is a
    replay of a PRE-restart runout, not a fresh exhaustion. It stays suppressed until it
    clears from the wire, after which a genuine re-fire stamps."""
    printer = await printer_factory()
    spool = await _new_spool(db_session, weight_used=400)
    await _assign(db_session, printer.id, 0, 2, spool.id)
    await db_session.commit()

    err = _slot_runout_err(attr=0x07002200, code=0x00030002)

    s1 = _make_state(0, 2, _tray(), gcode_state="RUNNING", tray_now=0)
    s1.subtask_id = "job-1"
    assert await _push(printer.id, _wire(s1, err), own_session_factory) is None  # seeds, no edge
    await db_session.refresh(spool)
    assert spool.spent_at is None

    s2 = _make_state(0, 2, _tray(), gcode_state="RUNNING", tray_now=0)
    await _push(printer.id, _wire(s2), own_session_factory)  # wire all-clear

    s3 = _make_state(0, 2, _tray(), gcode_state="RUNNING", tray_now=0)
    s3.subtask_id = "job-2"
    assert await _push(printer.id, _wire(s3, err), own_session_factory) is not None

    await db_session.refresh(spool)
    assert spool.spent_at is not None


@pytest.mark.asyncio
async def test_slot_runout_edges_are_full_code_scoped_so_another_slot_still_stamps(
    db_session, printer_factory, own_session_factory
):
    """Cross-push multi-slot: slot 1's code stands from the seeding frame while slot 3's
    APPEARS on a later one. Both render the short code ``0700_0002``, so a short-code
    identity would suppress a genuinely new runout on a DIFFERENT slot — exactly the
    chained case this trigger exists for. The tracker keys on the lossless full_code."""
    printer = await printer_factory()
    seeded_slot = await _new_spool(db_session, weight_used=990)
    other_slot = await _new_spool(db_session, weight_used=970)
    await _assign(db_session, printer.id, 0, 0, seeded_slot.id)
    await _assign(db_session, printer.id, 0, 2, other_slot.id)
    await db_session.commit()

    standing = _slot_runout_err(attr=0x07002000, code=0x00030002)  # live across the restart
    fresh = _slot_runout_err(attr=0x07002200, code=0x00030002)  # a NEW slot running dry
    assert hms_short_code(standing.attr, standing.code) == hms_short_code(fresh.attr, fresh.code)

    seed = _make_state(0, 0, _tray(), gcode_state="RUNNING", tray_now=1)
    seed.subtask_id = "job-1"
    await _push(printer.id, _wire(seed, standing), own_session_factory)

    fire = _make_state(0, 2, _tray(), gcode_state="RUNNING", tray_now=1)
    fire.subtask_id = "job-1"
    edges = await _push(printer.id, _wire(fire, standing, fresh), own_session_factory)

    assert edges is not None and edges.appeared_full == frozenset({fresh.full_code})
    await db_session.refresh(other_slot)
    await db_session.refresh(seeded_slot)
    assert other_slot.spent_at is not None  # only the slot that APPEARED
    assert seeded_slot.spent_at is None


@pytest.mark.asyncio
async def test_slot_runout_reappearing_under_a_new_job_stamps_again(db_session, printer_factory, own_session_factory):
    """THE flap-window bug, Lane B: the firmware's auto-switch statement clears and
    returns under a NEW job inside notify_dedup's 600 s window. That is a second roll
    physically running dry on the same slot, and it must stamp again."""
    printer = await printer_factory()
    first = await _new_spool(db_session, weight_used=990)
    await _assign(db_session, printer.id, 0, 2, first.id)
    await db_session.commit()

    err = _slot_runout_err(attr=0x07002200, code=0x00030002)
    await _push(
        printer.id, _wire(_make_state(0, 2, _tray(), gcode_state="RUNNING", tray_now=0)), own_session_factory
    )  # seed

    job_a = _make_state(0, 2, _tray(), gcode_state="RUNNING", tray_now=0)
    job_a.subtask_id = "job-a"
    await _push(printer.id, _wire(job_a, err), own_session_factory)
    await db_session.refresh(first)
    assert first.spent_at is not None

    await _push(
        printer.id, _wire(_make_state(0, 2, _tray(), gcode_state="RUNNING", tray_now=0)), own_session_factory
    )  # wire all-clear
    assignment = (
        await db_session.execute(
            select(SpoolAssignment).where(
                SpoolAssignment.printer_id == printer.id,
                SpoolAssignment.ams_id == 0,
                SpoolAssignment.tray_id == 2,
            )
        )
    ).scalar_one()
    second = await _new_spool(db_session, weight_used=990)
    assignment.spool_id = second.id
    await db_session.commit()

    job_b = _make_state(0, 2, _tray(), gcode_state="RUNNING", tray_now=0)
    job_b.subtask_id = "job-b"
    await _push(printer.id, _wire(job_b, err), own_session_factory)

    await db_session.refresh(second)
    assert second.spent_at is not None  # TWO spent rows, one per physical exhaustion


@pytest.mark.asyncio
async def test_slot_runout_restart_replay_fresh_spool_not_stamped(db_session, printer_factory, own_session_factory):
    """End-to-end restart scenario for Lane B (mirror of the 8011 pin): the donor stamps
    pre-restart; the restart drops the process state; the operator swaps a fresh roll
    onto the slot; the same auto-switch code is still live on the first frame the new
    process consumes — the fresh roll must NOT be stamped."""
    printer = await printer_factory()
    donor = await _new_spool(db_session, weight_used=400)
    await _assign(db_session, printer.id, 0, 2, donor.id)
    await db_session.commit()

    err = _slot_runout_err(attr=0x07002200, code=0x00030002)
    await _push(
        printer.id, _wire(_make_state(0, 2, _tray(), gcode_state="RUNNING", tray_now=0)), own_session_factory
    )  # seed
    pre = _make_state(0, 2, _tray(), gcode_state="RUNNING", tray_now=0)
    pre.subtask_id = "job-1"
    await _push(printer.id, _wire(pre, err), own_session_factory)
    await db_session.refresh(donor)
    assert donor.spent_at is not None

    spool_respool._reset_state()  # a server restart loses the dedup...
    hms_edges._reset_state()  # ...and the edge ledger

    assignment = (
        await db_session.execute(
            select(SpoolAssignment).where(
                SpoolAssignment.printer_id == printer.id,
                SpoolAssignment.ams_id == 0,
                SpoolAssignment.tray_id == 2,
            )
        )
    ).scalar_one()
    fresh = await _new_spool(db_session, weight_used=0)
    assignment.spool_id = fresh.id
    await db_session.commit()

    post = _make_state(0, 2, _tray(), gcode_state="RUNNING", tray_now=0)
    post.subtask_id = "job-1"
    assert await _push(printer.id, _wire(post, err), own_session_factory) is None  # seeds it
    assert await _push(printer.id, _wire(post, err), own_session_factory) is None  # still standing

    await db_session.refresh(fresh)
    assert fresh.spent_at is None


@pytest.mark.asyncio
async def test_slot_runout_same_job_dedup_one_stamp(db_session, printer_factory):
    """A re-raised auto-switch on the SAME (printer, job, slot) must not stamp the
    replacement roll the operator has since inserted (the 18:56 misattribution class)."""
    printer = await printer_factory()
    donor = await _new_spool(db_session, weight_used=990)
    await _assign(db_session, printer.id, 0, 2, donor.id)
    await db_session.commit()

    state = _make_state(0, 2, _tray(), gcode_state="RUNNING", tray_now=0)
    state.subtask_id = "job-1"
    err = _slot_runout_err(attr=0x07002200, code=0x00030002)
    state.hms_errors = [err]

    first = await mark_spent_on_slot_runout(db_session, printer.id, [_slot_event(err)], state)
    assert [s.id for s in first] == [donor.id]

    assignment = (
        await db_session.execute(
            select(SpoolAssignment).where(
                SpoolAssignment.printer_id == printer.id,
                SpoolAssignment.ams_id == 0,
                SpoolAssignment.tray_id == 2,
            )
        )
    ).scalar_one()
    replacement = await _new_spool(db_session, weight_used=0)
    assignment.spool_id = replacement.id
    await db_session.commit()

    assert await mark_spent_on_slot_runout(db_session, printer.id, [_slot_event(err)], state) == []
    assert (await db_session.get(Spool, replacement.id)).spent_at is None


@pytest.mark.asyncio
async def test_slot_runout_spoolman_noop(db_session, printer_factory):
    """Spoolman owns the spool lifecycle → every Tier-1 entry point no-ops."""
    from backend.app.api.routes.settings import set_setting

    printer = await printer_factory()
    spool = await _new_spool(db_session, weight_used=990)
    await _assign(db_session, printer.id, 0, 2, spool.id)
    await set_setting(db_session, "spoolman_enabled", "true")
    await db_session.commit()

    state = _make_state(0, 2, _tray(), gcode_state="RUNNING", tray_now=0)
    state.subtask_id = "job-1"
    err = _slot_runout_err(attr=0x07002200, code=0x00030002)

    assert await mark_spent_on_slot_runout(db_session, printer.id, [_slot_event(err)], state) == []
    assert (await db_session.get(Spool, spool.id)).spent_at is None


@pytest.mark.asyncio
async def test_slot_runout_ams_ht_attr_fails_closed(db_session, printer_factory):
    """AMS-HT units carry unit ids ≥ 0x80, which ``ams_slot_from_attr`` rejects (unit
    must be ≤ 7). The decode fails closed rather than folding the id into a wrong AMS,
    so nothing is stamped — Path B's tray_now sampler remains the cover there."""
    from backend.app.services.hms_errors import ams_slot_from_attr

    printer = await printer_factory()
    spool = await _new_spool(db_session, weight_used=990)
    await _assign(db_session, printer.id, 0, 2, spool.id)
    await db_session.commit()

    state = _make_state(0, 2, _tray(), gcode_state="RUNNING", tray_now=0)
    state.subtask_id = "job-1"
    err = _slot_runout_err(attr=0x07802200, code=0x00030002)  # unit id 0x80 = AMS-HT
    assert ams_slot_from_attr(err.attr) is None

    assert await mark_spent_on_slot_runout(db_session, printer.id, [_slot_event(err)], state) == []
    assert (await db_session.get(Spool, spool.id)).spent_at is None


# -- Fix 2: Path B per-push sampling + the deleted open-time absence veto --------
# The backup-swap detector used to hang off the AMS-change callback, which fires only
# on an AMS HASH change — and tray_now is deliberately not hashed. The switch therefore
# became visible only when the drained slot's exist-bit wipe moved the hash, and at that
# exact observation the open-time ``_tray_present(state, prev)`` gate read the departed
# slot ABSENT and vetoed the pending swap: the event that revealed the edge was the event
# the gate rejected. Fleet evidence 2026-07-30/31: four confirmed auto-refills, zero
# stamps. The detector is now a per-push sampler (no DB) + a confirm task (own session).


@pytest.mark.asyncio
async def test_backup_swap_open_time_absent_departed_still_stamps(
    db_session, printer_factory, fake_clock, own_session_factory
):
    """INCIDENT PIN (2026-07-30 08:32, 009-H2S / printer 7). The stable feeder (tray 1)
    runs dry and the firmware auto-switches to tray 0. The exist-bit wipe lands WITH the
    push that first reveals the edge, so the departed tray already reads absent (state 9
    / blank tray_type) at OPEN time, not merely at confirm time. The pending swap must
    still open and confirm — a stable feeder that vanished exactly as tray_now left it IS
    the run-to-empty signature, which is why the open-time veto was deleted.

    Distinct from the 2026-07-21 003-H2S pin above: there the departed tray was still
    seated at the edge and only wiped inside the window (the CONFIRM-time tolerance).
    Mutation-verified: restoring ``if not _tray_present(state, prev): return confirmed``
    in ``sample_status_push`` makes this assert False (no pending, no stamp).
    """
    printer = await printer_factory()
    spool = await _bind_at(db_session, printer.id, 0, 1, weight_used=500.0)  # the run-dry feeder
    _establish_stable_feeder(printer.id, 1, fake_clock, present=(0, 1))

    # The edge push itself already carries the wipe: tray_now 1→0 AND tray 1 absent.
    assert await _drive_swap(own_session_factory, printer.id, _running_wiped(0, seated=(0,), wiped=(1,))) is None
    assert spool_respool._pending_swaps.get(printer.id) == (1, 0, fake_clock["t"])  # opened despite absence

    fake_clock["t"] += spool_respool._SWAP_CONFIRM_S + 1
    marked = await _drive_swap(own_session_factory, printer.id, _running_wiped(0, seated=(0,), wiped=(1,)))

    assert marked is not None and marked.id == spool.id
    assert marked.spent_at is not None
    assert marked.weight_used == 500.0  # true ledger preserved


@pytest.mark.asyncio
async def test_sample_status_push_confirms_and_confirm_task_stamps(
    db_session,
    test_engine,
    printer_factory,
    fake_clock,
    own_session_factory,
):
    """The production wiring end to end: the sync sampler carries every push, reports the
    departed tray on the push that confirms, and the async confirmer — on its OWN session,
    as ``main`` fires it — turns that into the spent stamp."""
    printer = await printer_factory()
    spool = await _bind_at(db_session, printer.id, 0, 0, weight_used=500.0)

    _establish_stable_feeder(printer.id, 0, fake_clock)

    # Edge 0→1 opens the pending swap; the sampler reports nothing yet.
    assert spool_respool.sample_status_push(printer.id, _running(1)) == []
    fake_clock["t"] += spool_respool._SWAP_CONFIRM_S + 1
    departed = spool_respool.sample_status_push(printer.id, _running(1))
    assert departed == [0]  # the departed GLOBAL tray, not the new feeder

    stamped = await spool_respool.confirm_backup_swaps(printer.id, departed, session_factory=own_session_factory)

    assert [s.id for s in stamped] == [spool.id]
    await db_session.refresh(spool)  # the stamp was committed by the confirmer's session
    assert spool.spent_at is not None
    assert spool.weight_used == 500.0


@pytest.mark.asyncio
async def test_confirm_backup_swaps_spoolman_noop(db_session, test_engine, printer_factory, own_session_factory):
    """Spoolman owns the spool lifecycle → the confirmer stamps nothing even with a
    confirmed departure. The gate lives HERE and not in the sampler: it is a settings
    read, and the sampler runs on every status push."""
    from backend.app.api.routes.settings import set_setting

    printer = await printer_factory()
    spool = await _bind_at(db_session, printer.id, 0, 0, weight_used=500.0)
    await set_setting(db_session, "spoolman_enabled", "true")
    await db_session.commit()

    stamped = await spool_respool.confirm_backup_swaps(printer.id, [0], session_factory=own_session_factory)

    assert stamped == []
    await db_session.refresh(spool)
    assert spool.spent_at is None


@pytest.mark.asyncio
async def test_sample_status_push_job_boundary_discards(db_session, printer_factory, fake_clock):
    """The 2026-07-20 spool-106 boundary pin, through the sampler: job A's stable feeder
    must not make job B's dispatch-mapped feeder change look like a firmware backup
    switch. A subtask change (incl. ``None``↔value) discards the edge state, so no
    departure is ever reported and the still-full roll is not stamped."""
    printer = await printer_factory()
    tray0_spool = await _bind_at(db_session, printer.id, 0, 0, weight_used=250.0)  # must NOT be stamped

    _establish_stable_feeder(printer.id, 0, fake_clock, subtask_id="A")

    # Job B: the boundary resets the edge trackers and re-seeds tray_now — no pending.
    assert spool_respool.sample_status_push(printer.id, _running(2, subtask_id="B")) == []
    assert printer.id not in spool_respool._stable_feeder
    assert printer.id not in spool_respool._pending_swaps

    fake_clock["t"] += spool_respool._SWAP_CONFIRM_S + 1
    assert spool_respool.sample_status_push(printer.id, _running(2, subtask_id="B")) == []
    assert (await db_session.get(Spool, tray0_spool.id)).spent_at is None


# --- the runout closes the roll's TRUE capacity (2026-08-19, operator ruling 18) --------


class TestDeliveredCapacity:
    """Scenario C2. A minted row's ``label_weight`` is an ASSUMPTION, not a reading of the
    roll in the tray; the hardware runout is the only moment its real capacity becomes
    knowable, and the answer is simply what the farm watched it feed.

    Both directions of the gap are ORDINARY STOCK, which is the whole point of the ruling:
    a part-used roll an operator seated delivers well under its label, and some brands ship
    ~1100 g on a 1000 g label. Neither is an error, so neither may be warned about.
    """

    def test_an_unspent_row_has_delivered_nothing_final(self):
        """A running total is not a capacity — callers must not read one as the other."""
        assert Spool(label_weight=1000, weight_used=400.0).delivered_g is None

    def test_a_part_used_roll_delivers_what_it_fed(self):
        spool = Spool(label_weight=1000, weight_used=800.0, spent_at=datetime.utcnow())
        assert spool.delivered_g == pytest.approx(800.0)
        assert spool.remaining_g == 0.0  # spent ⇒ empty on every surface (rule 8)
        assert spool.weight_used == pytest.approx(800.0)  # …while the ledger stays RAW

    def test_an_overfilled_roll_delivers_more_than_its_label(self):
        """~1100 g on a 1000 g label is ordinary stock, not misattribution."""
        assert Spool(label_weight=1000, weight_used=1100.0, spent_at=datetime.utcnow()).delivered_g == pytest.approx(
            1100.0
        )

    def test_the_label_is_never_written_back(self):
        """One origin, no stored duplicate: overwriting the label would conflate 'what we
        assumed' with 'what it delivered' and destroy the nominal figure cost-per-kg reads."""
        spool = Spool(label_weight=1000, weight_used=800.0, spent_at=datetime.utcnow())
        assert spool.label_weight == 1000


@pytest.mark.asyncio
async def test_a_short_delivery_is_RECORDED_not_warned(db_session, printer_factory, caplog):
    """The old WARNING fired on the NORMAL shape and so read as noise — 14 live rows carried
    843/580/576/453/418/417 g 'remaining' at their stamp, every one a part-used roll minted
    as full. The stamp now produces the durable artifact of the delivered figure instead."""
    printer = await printer_factory()
    spool = await _seed_released_row(db_session, printer.id, 0, 0, weight_used=300.0)

    with caplog.at_level(logging.INFO, logger=_RESPOOL_LOGGER):
        marked = await mark_spent_on_runout(db_session, printer.id, {"0700_8011"}, _runout(_empty_bay()))

    assert marked is not None and marked.id == spool.id
    assert marked.delivered_g == pytest.approx(300.0)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], "a normal short delivery must not warn"
    assert any("delivered 300 g against an assumed" in r.getMessage() for r in caplog.records)


class TestGlobalTrayCodec:
    """The ONE global-tray encoding (invariant 1), and its exact inverse.

    A bare ``ams_id * 4 + tray_id`` is correct for a regular AMS and silently wrong for the
    other two conventions the fleet actually runs — which is why the arithmetic is not to be
    re-spelled at call sites.
    """

    def _codec(self):
        from backend.app.services.spool_respool import decode_global_tray, encode_global_tray

        return encode_global_tray, decode_global_tray

    def test_regular_ams_round_trips(self):
        encode, decode = self._codec()
        for ams_id in range(4):
            for tray_id in range(4):
                assert decode(encode(ams_id, tray_id)) == (ams_id, tray_id)
        assert encode(0, 3) == 3 and encode(1, 0) == 4

    def test_ams_ht_is_its_own_unit_id_not_the_multiplication(self):
        encode, decode = self._codec()
        assert encode(128, 0) == 128, "a single-tray AMS-HT reports global == ams_id"
        assert decode(128) == (128, 0)
        assert encode(128, 1) is None, "an AMS-HT has no second tray to name"

    def test_the_external_holder_uses_the_254_convention(self):
        encode, decode = self._codec()
        assert (encode(255, 0), encode(255, 1)) == (254, 255)
        assert decode(254) == (255, 0) and decode(255) == (255, 1)
        assert encode(255, 2) is None

    def test_an_unaddressable_slot_fails_closed(self):
        encode, _decode = self._codec()
        # A fabricated global id would compare EQUAL to some real slot and mis-attribute a
        # fault to it, so every unrepresentable input answers None rather than guessing.
        assert encode(None, 0) is None
        assert encode(0, None) is None
        assert encode(-1, 0) is None
        assert encode(0, 4) is None
        assert encode(200, 0) is None
        assert encode("x", 0) is None
