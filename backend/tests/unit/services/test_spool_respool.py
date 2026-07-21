"""Unit tests for the reused-tag re-spool service (spool_respool).

Covers the core operation (donor disposal, fresh-full mint, tag move, assignment
rewire, K-profile copy, last-brand persistence, staged release), the sibling-tag
guard both directions, and the three certainty tiers (spent-marking on runout /
backup-swap, auto re-spool, one-click prompt) including the Tier-3 evidence gates
(physical swap evidence, impossible-ledger suppression, remain-jump corroboration).
"""

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
from backend.app.services import spool_respool
from backend.app.services.bambu_mqtt import HMSError
from backend.app.services.spool_respool import (
    RESPOOL_TAG_TYPE,
    RespoolError,
    RespoolSiblingConflict,
    _remain_jump,
    _remain_jump_reading,
    capture_backup_swap,
    mark_spent_on_runout,
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
    yield
    spool_respool._reset_state()
    ams_presence._reset_state()


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


async def _bind_at(db, printer_id, ams_id, tray_id, *, weight_used=500.0):
    spool = Spool(material="PETG", label_weight=1000, core_weight=250, weight_used=weight_used)
    spool.k_profiles = []
    spool.assignments = []
    db.add(spool)
    await db.flush()
    await _assign(db, printer_id, ams_id, tray_id, spool.id)
    await db.commit()
    return spool


async def _establish_stable_feeder(db, printer_id, tray, clock, *, present=(0, 1, 2), subtask_id="job-A"):
    """Make ``tray`` the confirmed stable feeder under one job identity.

    The first push seeds the job/edge state (a boundary that opens nothing); two
    further pushes ≥ _SWAP_CONFIRM_S apart under the SAME subtask confirm the feeder.
    """
    await capture_backup_swap(db, printer_id, _running(tray, present=present, subtask_id=subtask_id))
    await capture_backup_swap(db, printer_id, _running(tray, present=present, subtask_id=subtask_id))
    clock["t"] += spool_respool._SWAP_CONFIRM_S + 1
    await capture_backup_swap(db, printer_id, _running(tray, present=present, subtask_id=subtask_id))
    assert spool_respool._stable_feeder.get(printer_id) == tray


@pytest.mark.asyncio
async def test_backup_swap_genuine_switch_stamps_after_confirm(db_session, printer_factory, fake_clock):
    """A genuine firmware backup switch (the stable feeder ran dry, a sibling feeds
    on for ≥ 60 s, the departed still present) STILL marks the departed spool spent —
    and preserves its true grams (the label floor is gone)."""
    printer = await printer_factory()
    spool = await _bind_at(db_session, printer.id, 0, 0, weight_used=500.0)
    await _establish_stable_feeder(db_session, printer.id, 0, fake_clock)

    # Edge off the stable feeder (0 → 1) opens a pending swap; not yet confirmed.
    assert await capture_backup_swap(db_session, printer.id, _running(1)) is None

    # The new tray feeds stably past the confirm window with tray 0 still present.
    fake_clock["t"] += spool_respool._SWAP_CONFIRM_S + 1
    marked = await capture_backup_swap(db_session, printer.id, _running(1))
    assert marked is not None and marked.id == spool.id
    assert marked.spent_at is not None
    assert marked.weight_used == 500.0  # true ledger preserved


@pytest.mark.asyncio
async def test_backup_swap_no_stamp_before_confirm_window(db_session, printer_factory, fake_clock):
    """Within the confirm window the pending swap has NOT stamped yet."""
    printer = await printer_factory()
    spool = await _bind_at(db_session, printer.id, 0, 0)
    await _establish_stable_feeder(db_session, printer.id, 0, fake_clock)

    assert await capture_backup_swap(db_session, printer.id, _running(1)) is None  # opens pending
    fake_clock["t"] += 10  # still < 60 s
    assert await capture_backup_swap(db_session, printer.id, _running(1)) is None
    assert (await db_session.get(Spool, spool.id)).spent_at is None


@pytest.mark.asyncio
async def test_backup_swap_drops_on_flap_back_to_departed(db_session, printer_factory, fake_clock):
    """tray_now returning to the departed feeder = it's feeding again → drop, no stamp."""
    printer = await printer_factory()
    spool = await _bind_at(db_session, printer.id, 0, 0)
    await _establish_stable_feeder(db_session, printer.id, 0, fake_clock)

    await capture_backup_swap(db_session, printer.id, _running(1))  # pending 0→1
    fake_clock["t"] += spool_respool._SWAP_CONFIRM_S + 1
    marked = await capture_backup_swap(db_session, printer.id, _running(0))  # flapped back to 0
    assert marked is None
    assert (await db_session.get(Spool, spool.id)).spent_at is None
    assert printer.id not in spool_respool._pending_swaps  # dropped


@pytest.mark.asyncio
async def test_backup_swap_drops_when_departed_no_longer_present(db_session, printer_factory, fake_clock):
    """The departed spool physically gone (tray reads empty) = ordinary unload → drop."""
    printer = await printer_factory()
    spool = await _bind_at(db_session, printer.id, 0, 0)
    await _establish_stable_feeder(db_session, printer.id, 0, fake_clock)

    await capture_backup_swap(db_session, printer.id, _running(1))  # pending 0→1
    fake_clock["t"] += spool_respool._SWAP_CONFIRM_S + 1
    # tray 0 no longer present in the AMS payload → departed spool removed.
    marked = await capture_backup_swap(db_session, printer.id, _running(1, present=(1, 2)))
    assert marked is None
    assert (await db_session.get(Spool, spool.id)).spent_at is None


@pytest.mark.asyncio
async def test_backup_swap_drops_on_state_change(db_session, printer_factory, fake_clock):
    """Leaving RUNNING before the window elapses drops the pending swap."""
    printer = await printer_factory()
    spool = await _bind_at(db_session, printer.id, 0, 0)
    await _establish_stable_feeder(db_session, printer.id, 0, fake_clock)

    await capture_backup_swap(db_session, printer.id, _running(1))  # pending 0→1
    fake_clock["t"] += spool_respool._SWAP_CONFIRM_S + 1
    paused = _running(1)
    paused.state = "PAUSE"
    assert await capture_backup_swap(db_session, printer.id, paused) is None
    assert (await db_session.get(Spool, spool.id)).spent_at is None


@pytest.mark.asyncio
async def test_backup_swap_transient_walk_no_false_stamp(db_session, printer_factory, fake_clock):
    """The 011 pattern: stable feeder 2, then tray_now WALKS 2→1→0 during the
    firmware's runout handling. The 1→0 edge departs a NON-stable value (1) so it
    never opens a pending, and the 2→1 pending drops when tray_now moves on — so
    tray 1's still-full spool is NOT falsely stamped."""
    printer = await printer_factory()
    tray1_spool = await _bind_at(db_session, printer.id, 0, 1, weight_used=200.0)  # must NOT be stamped
    await _establish_stable_feeder(db_session, printer.id, 2, fake_clock)

    # Walk 2→1 (opens pending 2→1) then 1→0 (prev 1 is not the stable feeder → nothing).
    await capture_backup_swap(db_session, printer.id, _running(1))
    marked = await capture_backup_swap(db_session, printer.id, _running(0))
    assert marked is None
    assert (await db_session.get(Spool, tray1_spool.id)).spent_at is None  # tray 1 untouched


@pytest.mark.asyncio
async def test_backup_swap_commanded_load_suppressed(db_session, printer_factory, fake_clock):
    """Our own commanded load to the new tray consumes the marker and never opens a
    pending swap — the departed spool is never stamped (the 006 false-stamp mode)."""
    printer = await printer_factory()
    spool = await _bind_at(db_session, printer.id, 0, 0)
    await _establish_stable_feeder(db_session, printer.id, 0, fake_clock)

    note_commanded_load(printer.id, 1)  # WE issued the load to tray 1
    assert await capture_backup_swap(db_session, printer.id, _running(1)) is None  # edge 0→1 suppressed
    assert printer.id not in spool_respool._pending_swaps  # no pending opened
    fake_clock["t"] += spool_respool._SWAP_CONFIRM_S + 1
    assert await capture_backup_swap(db_session, printer.id, _running(1)) is None
    assert (await db_session.get(Spool, spool.id)).spent_at is None


@pytest.mark.asyncio
async def test_backup_swap_commanded_load_ttl_expiry_rearms(db_session, printer_factory, fake_clock):
    """A commanded-load marker older than _COMMANDED_LOAD_TTL_S no longer suppresses:
    a later genuine switch to that same tray stamps normally."""
    printer = await printer_factory()
    spool = await _bind_at(db_session, printer.id, 0, 0)

    note_commanded_load(printer.id, 1)  # stale marker at t0
    fake_clock["t"] += spool_respool._COMMANDED_LOAD_TTL_S + 1  # let it expire
    await _establish_stable_feeder(db_session, printer.id, 0, fake_clock)

    assert await capture_backup_swap(db_session, printer.id, _running(1)) is None  # opens pending (marker expired)
    fake_clock["t"] += spool_respool._SWAP_CONFIRM_S + 1
    marked = await capture_backup_swap(db_session, printer.id, _running(1))
    assert marked is not None and marked.id == spool.id  # stamped — TTL expiry re-armed detection


@pytest.mark.asyncio
async def test_backup_swap_noop_when_not_running(db_session, printer_factory):
    """Not mid-print: the edge tracker updates but nothing stamps (baseline)."""
    printer = await printer_factory()
    idle = MagicMock()
    idle.state = "IDLE"
    idle.tray_now = 1
    idle.raw_data = {"ams": [{"id": 0, "tray": [{"id": 0, **_tray()}]}]}
    assert await capture_backup_swap(db_session, printer.id, idle) is None


# -- Tier 1: job-boundary edge reset (2026-07-20 spool-106 false-stamp) ---------


@pytest.mark.asyncio
async def test_backup_swap_no_stamp_across_job_boundary_incident_pin(db_session, printer_factory, fake_clock):
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
    await _establish_stable_feeder(db_session, printer.id, 0, fake_clock, subtask_id="A")

    # Job B (new subtask) is dispatch-mapped to tray 2; tray 0 is still seated.
    assert await capture_backup_swap(db_session, printer.id, _running(2, subtask_id="B")) is None
    fake_clock["t"] += spool_respool._SWAP_CONFIRM_S + 1
    marked = await capture_backup_swap(db_session, printer.id, _running(2, subtask_id="B"))

    assert marked is None
    assert (await db_session.get(Spool, tray0_spool.id)).spent_at is None
    assert printer.id not in spool_respool._pending_swaps


@pytest.mark.asyncio
async def test_backup_swap_eject_interlude_between_jobs_no_false_stamp(db_session, printer_factory, fake_clock):
    """Eject-shaped interlude: job A feeds tray 0 (stable), then a server-dispatched
    eject job runs RUNNING with tray_now=255 (no filament) under its own subtask, then
    job B is dispatch-mapped to tray 2. The RUNNING eject pushes never fire the
    not-running cleanup (that is why the stale stable feeder survived pre-fix); with the
    fix each subtask change (A→eject→B) resets the edge state, so tray 0 is not stamped.
    """
    printer = await printer_factory()
    tray0_spool = await _bind_at(db_session, printer.id, 0, 0, weight_used=250.0)
    await _establish_stable_feeder(db_session, printer.id, 0, fake_clock, subtask_id="A")

    # Eject job: RUNNING, tray_now=255 (no filament), distinct subtask → no cleanup.
    await capture_backup_swap(db_session, printer.id, _running(255, subtask_id="eject"))
    fake_clock["t"] += 5
    await capture_backup_swap(db_session, printer.id, _running(255, subtask_id="eject"))
    assert printer.id not in spool_respool._stable_feeder  # the boundary reset cleared it

    # Job B mapped to tray 2, tray 0 still seated, past the confirm window.
    assert await capture_backup_swap(db_session, printer.id, _running(2, subtask_id="B")) is None
    fake_clock["t"] += spool_respool._SWAP_CONFIRM_S + 1
    marked = await capture_backup_swap(db_session, printer.id, _running(2, subtask_id="B"))

    assert marked is None
    assert (await db_session.get(Spool, tray0_spool.id)).spent_at is None


@pytest.mark.asyncio
async def test_reset_swap_edge_state_clears_printer_and_opens_no_swap(db_session, printer_factory, fake_clock):
    """The job-boundary reset hook (called from main.on_print_start / on_print_complete)
    drops that printer's edge trackers; the next push re-seeds ``_last_tray_now`` with
    prev ``None`` and opens no pending swap (no confirmed stable feeder survives)."""
    printer = await printer_factory()
    spool = await _bind_at(db_session, printer.id, 0, 0)
    await _establish_stable_feeder(db_session, printer.id, 0, fake_clock)  # stable feeder 0 armed

    reset_swap_edge_state(printer.id)

    assert printer.id not in spool_respool._last_tray_now
    assert printer.id not in spool_respool._feeder_since
    assert printer.id not in spool_respool._stable_feeder
    assert printer.id not in spool_respool._pending_swaps

    # The next push (still subtask job-A) merely re-seeds; the immediate 0→1 edge cannot
    # open a pending because there is no confirmed stable feeder after the reset.
    assert await capture_backup_swap(db_session, printer.id, _running(1)) is None
    assert printer.id not in spool_respool._pending_swaps
    fake_clock["t"] += spool_respool._SWAP_CONFIRM_S + 1
    assert await capture_backup_swap(db_session, printer.id, _running(1)) is None
    assert (await db_session.get(Spool, spool.id)).spent_at is None


@pytest.mark.asyncio
async def test_backup_swap_same_subtask_genuine_switch_still_stamps(db_session, printer_factory, fake_clock):
    """Regression guard for the job-boundary fix: a genuine mid-job firmware backup
    switch happens under an UNCHANGED subtask_id and must STILL stamp. Same subtask 'J'
    throughout: stable feeder 0, edge 0→1, tray 0 still seated, 60 s confirm → stamped.
    """
    printer = await printer_factory()
    spool = await _bind_at(db_session, printer.id, 0, 0, weight_used=500.0)
    await _establish_stable_feeder(db_session, printer.id, 0, fake_clock, subtask_id="J")

    assert await capture_backup_swap(db_session, printer.id, _running(1, subtask_id="J")) is None
    fake_clock["t"] += spool_respool._SWAP_CONFIRM_S + 1
    marked = await capture_backup_swap(db_session, printer.id, _running(1, subtask_id="J"))

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
    await set_setting(db_session, "respool_auto_enabled", "true")  # Tier-2 auto path under test
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
async def test_gate_null_spent_under_threshold_prompts_with_dedup(db_session, printer_factory, monkeypatch):
    printer = await printer_factory()
    donor = await _make_donor(db_session, spent=False, weight_used=990.0)  # remaining 10 <= 30
    await db_session.commit()
    _record_physical_cycle(printer.id)  # somebody cycled a roll through the slot

    broadcasts = _spy_broadcast(monkeypatch)
    tray = _tray()
    await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, tray, donor)
    await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, tray, donor)  # deduped

    prompts = [b for b in broadcasts if b["type"] == "respool_prompt"]
    assert len(prompts) == 1
    assert prompts[0]["donor_spool_id"] == donor.id
    assert prompts[0]["donor_remaining_g"] == pytest.approx(10.0)


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
    await set_setting(db_session, "respool_auto_enabled", "true")  # exercise the auto→sibling-conflict path
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
async def test_gate_tier3_fires_when_not_dismissed(db_session, printer_factory, monkeypatch):
    """Baseline: the SAME near-empty spool DOES prompt while not dismissed."""
    printer = await printer_factory()
    donor = await _make_donor(db_session, spent=False, weight_used=990.0)
    await db_session.commit()
    _record_physical_cycle(printer.id)

    broadcasts = _spy_broadcast(monkeypatch)
    await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, _tray(), donor)

    assert [b for b in broadcasts if b["type"] == "respool_prompt"] != []


@pytest.mark.asyncio
async def test_gate_tier3_dismissal_survives_dedup_clear(db_session, printer_factory, monkeypatch):
    """The persisted dismissal outlives the in-memory dedup: clearing the slot
    dedup (as main.on_ams_change does when a slot reports empty) does NOT re-open
    the prompt for a dismissed spool — the whole point of the new column."""
    printer = await printer_factory()
    donor = await _make_donor(db_session, spent=False, weight_used=990.0)
    donor.respool_dismissed_at = datetime.utcnow()
    await db_session.commit()
    _record_physical_cycle(printer.id)  # evidence present on both passes

    broadcasts = _spy_broadcast(monkeypatch)
    await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, _tray(), donor)
    # Simulate the empty-slot dedup clear that used to re-arm the prompt.
    spool_respool.clear_respool_prompt_dedup(printer.id, 0, 0)
    await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, _tray(), donor)

    assert [b for b in broadcasts if b["type"] == "respool_prompt"] == []


# -- R4: spent-tier dismissal honored per physical cycle ---------------------


@pytest.mark.asyncio
async def test_spent_dismissal_stands_no_cycle_suppresses_auto_and_prompt(db_session, printer_factory, monkeypatch):
    """R4 test 1 (MUTATION PIN). A spent + loaded spool the operator answered
    "Same spool" on, with NO physical cycle recorded since (the accessor returns
    None — e.g. right after a restart), broadcasts NOTHING and does NOT auto-respool
    even with respool_auto_enabled ON and a last brand set. This is the whole fix:
    the false spent stamp on spool 106 re-fired for days because the spent branch
    ignored the dismissal.

    Mutation-verified: with the `_dismissal_stands` gate removed the auto path runs —
    a fresh spool is minted, the donor is archived, and both asserts below flip.
    """
    printer = await printer_factory()
    donor = await _make_donor(db_session, spent=True)
    donor.respool_dismissed_at = datetime.utcnow()
    from backend.app.api.routes.settings import set_setting

    await set_setting(db_session, "respool_last_brand", "Polymaker")  # auto WOULD fire if the gate were gone
    await set_setting(db_session, "respool_auto_enabled", "true")
    await db_session.commit()
    # No _record_physical_cycle: last_physical_cycle_age → None → dismissal stands.

    _patch_pm(monkeypatch, _make_state(0, 0, _tray(state=11)))
    broadcasts = _spy_broadcast(monkeypatch)
    result = await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, _tray(state=11), donor)

    assert result is None  # no auto-respool
    assert [b for b in broadcasts if b["type"] == "respool_prompt"] == []  # no prompt
    assert (await db_session.get(Spool, donor.id)).archived_at is None  # donor untouched


@pytest.mark.asyncio
async def test_spent_dismissal_cycle_after_rearms_prompt_when_auto_off(db_session, printer_factory, monkeypatch):
    """R4 test 2 (auto OFF). A qualified physical cycle STRICTLY AFTER the dismissal
    re-arms the spent branch: the one-click prompt fires again (a genuine roll swap
    on a dismissed slot must surface)."""
    printer = await printer_factory()
    donor = await _make_donor(db_session, spent=True)
    donor.respool_dismissed_at = datetime.utcnow() - timedelta(seconds=100)  # dismissed 100 s ago
    await db_session.commit()
    _record_physical_cycle(printer.id, age_s=0.0)  # cycle just now → age (~0) < 100 → after dismissal

    _patch_pm(monkeypatch, _make_state(0, 0, _tray(state=11)))
    broadcasts = _spy_broadcast(monkeypatch)
    result = await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, _tray(state=11), donor)

    assert result is None  # auto off → prompt, not a mint
    prompts = [b for b in broadcasts if b["type"] == "respool_prompt"]
    assert len(prompts) == 1
    assert prompts[0]["trigger"] == "spent"


@pytest.mark.asyncio
async def test_spent_dismissal_cycle_after_rearms_auto_when_auto_on(db_session, printer_factory, monkeypatch):
    """R4 test 2 (auto ON). The same post-dismissal cycle re-arms the Tier-2 auto
    path — the spent + loaded tag re-spools to a fresh row."""
    printer = await printer_factory()
    donor = await _make_donor(db_session, spent=True)
    donor.respool_dismissed_at = datetime.utcnow() - timedelta(seconds=100)
    from backend.app.api.routes.settings import set_setting

    await set_setting(db_session, "respool_last_brand", "Polymaker")
    await set_setting(db_session, "respool_auto_enabled", "true")
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
    await set_setting(db_session, "respool_auto_enabled", "true")
    await db_session.commit()
    _record_physical_cycle(printer.id, age_s=100.0)  # cycle 100 s ago → age (100) ≥ 0 → predates

    _patch_pm(monkeypatch, _make_state(0, 0, _tray(state=11)))
    broadcasts = _spy_broadcast(monkeypatch)
    result = await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, _tray(state=11), donor)

    assert result is None
    assert [b for b in broadcasts if b["type"] == "respool_prompt"] == []
    assert (await db_session.get(Spool, donor.id)).archived_at is None  # donor untouched


# -- R5: prompt provenance payload fields ------------------------------------


@pytest.mark.asyncio
async def test_respool_prompt_payload_carries_provenance(db_session, printer_factory, monkeypatch):
    """R5. The spent-tier prompt payload carries the additive provenance fields so
    the UI can show the evidence and its age: spent_at + spent_age_s, the live AMS
    remain %, the ledger-implied remain %, and when the roll became bound."""
    printer = await printer_factory()
    # Spent, loaded, NOT dismissed, auto OFF → a clean spent prompt fires.
    donor = await _make_donor(db_session, spent=True, weight_used=990.0)  # ledger 1% of a 1000 g label
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
    """The provenance fields degrade to None cleanly: a non-spent near-empty prompt
    has no spent_at/spent_age_s, and a garbage tray remain yields no ams_remain_pct
    (the parse discipline of _remain_jump_reading), while the ledger % still computes."""
    printer = await printer_factory()
    donor = await _make_donor(db_session, spent=False, weight_used=990.0)  # remaining 10 <= 30 → near_empty
    await db_session.commit()
    _record_physical_cycle(printer.id)  # swap evidence Tier 3 requires

    _patch_pm(monkeypatch, _make_state(0, 0, _tray(state=11)))
    broadcasts = _spy_broadcast(monkeypatch)
    await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, _tray(state=11, tray_type="PETG"), donor)

    prompts = [b for b in broadcasts if b["type"] == "respool_prompt"]
    assert len(prompts) == 1
    p = prompts[0]
    assert p["trigger"] == "near_empty"
    assert p["spent_at"] is None
    assert p["spent_age_s"] is None
    assert p["ams_remain_pct"] == 100  # a valid remain still parses
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
    await set_setting(db_session, "respool_auto_enabled", "true")  # Tier-2 auto brand-fallback under test
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
    await set_setting(db_session, "respool_auto_enabled", "true")  # auto ON, but no brand to auto with
    await db_session.commit()

    _patch_pm(monkeypatch, _make_state(0, 0, _tray(state=11)))
    broadcasts = _spy_broadcast(monkeypatch)
    result = await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, _tray(state=11), donor)

    assert result is None  # did not auto-respool
    assert any(b["type"] == "respool_prompt" for b in broadcasts)


# -- Spoolman mode no-ops ----------------------------------------------------


@pytest.mark.asyncio
async def test_hooks_noop_in_spoolman_mode(db_session, printer_factory, monkeypatch):
    printer = await printer_factory()
    donor = await _make_donor(db_session, spent=True, weight_used=990.0)
    await _assign(db_session, printer.id, 0, 0, donor.id)
    from backend.app.api.routes.settings import set_setting

    await set_setting(db_session, "spoolman_enabled", "true")
    await db_session.commit()

    broadcasts = _spy_broadcast(monkeypatch)
    state = _make_state(0, 0, _tray(), tray_now=0)

    assert await mark_spent_on_runout(db_session, printer.id, {"0700_8011"}, state) is None
    assert await capture_backup_swap(db_session, printer.id, state) is None
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


# -- S1: restart-replay runout suppression (note_status_push seed) -----------


def _runout_err(code="8011", attr=0x07000000):
    """A live runout HMSError → hms_short_code(attr, code) == "0700_8011"."""
    return HMSError(code=code, attr=attr, module=7, severity=2)


@pytest.mark.asyncio
async def test_note_status_push_seeds_first_push_then_mark_spent_noops(db_session, printer_factory):
    """First push carries a live runout code → seeded; mark_spent_on_runout no-ops
    for that code so a swapped-in fresh spool bound to the slot is not mis-stamped."""
    printer = await printer_factory()
    fresh = await _new_spool(db_session, weight_used=0)  # the fresh roll now on the slot
    await _assign(db_session, printer.id, 0, 0, fresh.id)
    await _single_feeder_item(db_session, printer.id, mapping="[0, -1, -1, -1]")
    await db_session.commit()

    state = _make_state(0, 0, _tray(), tray_now=255)
    state.subtask_id = "job-1"
    state.hms_errors = [_runout_err()]  # runout live at the first status push

    spool_respool.note_status_push(printer.id, state)  # seed the replayed code
    assert await mark_spent_on_runout(db_session, printer.id, {"0700_8011"}, state) is None
    assert (await db_session.get(Spool, fresh.id)).spent_at is None  # NOT stamped


@pytest.mark.asyncio
async def test_note_status_push_unknown_state_does_not_consume_seed(db_session, printer_factory):
    """A connect-time broadcast (fresh PrinterState, state="unknown", no HMS yet)
    must NOT consume the one-shot seed — otherwise a still-live runout arriving on
    the next real report would replay as "new" and mis-stamp the fresh spool."""
    printer = await printer_factory()
    fresh = await _new_spool(db_session, weight_used=0)
    await _assign(db_session, printer.id, 0, 0, fresh.id)
    await _single_feeder_item(db_session, printer.id, mapping="[0, -1, -1, -1]")
    await db_session.commit()

    connect_state = _make_state(0, 0, _tray(), gcode_state="unknown", tray_now=255)
    connect_state.hms_errors = []  # no report yet
    spool_respool.note_status_push(printer.id, connect_state)  # must stay unseeded

    report_state = _make_state(0, 0, _tray(), gcode_state="PAUSE", tray_now=255)
    report_state.subtask_id = "job-1"
    report_state.hms_errors = [_runout_err()]  # the still-live replayed code
    spool_respool.note_status_push(printer.id, report_state)  # NOW seeds, with the code

    assert await mark_spent_on_runout(db_session, printer.id, {"0700_8011"}, report_state) is None
    assert (await db_session.get(Spool, fresh.id)).spent_at is None  # NOT stamped


@pytest.mark.asyncio
async def test_note_status_push_later_new_runout_stamps(db_session, printer_factory):
    """A runout NOT live at seed time (first push had zero HMS) is genuinely new and
    stamps normally."""
    printer = await printer_factory()
    spool = await _new_spool(db_session, weight_used=400)
    await _assign(db_session, printer.id, 0, 0, spool.id)
    await _single_feeder_item(db_session, printer.id, mapping="[0, -1, -1, -1]")
    await db_session.commit()

    seed_state = _make_state(0, 0, _tray(), tray_now=255)
    seed_state.hms_errors = []  # zero HMS at the first push → seeds {}
    spool_respool.note_status_push(printer.id, seed_state)

    fire_state = _make_state(0, 0, _tray(), tray_now=255)
    fire_state.subtask_id = "job-1"
    fire_state.hms_errors = [_runout_err()]
    marked = await mark_spent_on_runout(db_session, printer.id, {"0700_8011"}, fire_state)
    assert marked is not None and marked.id == spool.id
    assert marked.spent_at is not None


@pytest.mark.asyncio
async def test_note_status_push_seeded_code_clears_then_refires_stamps(db_session, printer_factory):
    """A seeded code that later clears from HMS is dropped from the seed; a
    subsequent re-fire is treated as new and stamps."""
    printer = await printer_factory()
    spool = await _new_spool(db_session, weight_used=400)
    await _assign(db_session, printer.id, 0, 0, spool.id)
    await _single_feeder_item(db_session, printer.id, mapping="[0, -1, -1, -1]")
    await db_session.commit()

    s1 = _make_state(0, 0, _tray(), tray_now=255)
    s1.hms_errors = [_runout_err()]
    spool_respool.note_status_push(printer.id, s1)  # seeds {0700_8011}

    s2 = _make_state(0, 0, _tray(), tray_now=255)
    s2.hms_errors = []  # code cleared on a later push
    spool_respool.note_status_push(printer.id, s2)  # drops it from the seed

    s3 = _make_state(0, 0, _tray(), tray_now=255)
    s3.subtask_id = "job-1"
    s3.hms_errors = [_runout_err()]
    marked = await mark_spent_on_runout(db_session, printer.id, {"0700_8011"}, s3)
    assert marked is not None and marked.spent_at is not None


@pytest.mark.asyncio
async def test_restart_replay_fresh_spool_not_stamped_end_to_end(db_session, printer_factory):
    """End-to-end restart scenario: the donor is stamped spent pre-restart; a restart
    (_reset_state) drops the in-memory dedup; a fresh spool is re-assigned to the slot;
    the same runout code is still live at the first post-restart push — the fresh
    spool must NOT be stamped (the 18:56 misattribution)."""
    printer = await printer_factory()
    donor = await _new_spool(db_session, weight_used=400)
    await _assign(db_session, printer.id, 0, 0, donor.id)
    await _single_feeder_item(db_session, printer.id, mapping="[0, -1, -1, -1]")
    await db_session.commit()

    pre = _make_state(0, 0, _tray(), tray_now=255)
    pre.subtask_id = "job-1"
    pre.hms_errors = [_runout_err()]
    # Pre-restart there is no seed yet → the donor stamps spent as normal.
    stamped = await mark_spent_on_runout(db_session, printer.id, {"0700_8011"}, pre)
    assert stamped is not None and stamped.id == donor.id
    assert stamped.spent_at is not None

    # Simulate a server restart: the in-memory dedup AND the seed state are lost.
    spool_respool._reset_state()

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

    # First post-restart push still carries the runout code → seed it.
    post = _make_state(0, 0, _tray(), tray_now=255)
    post.subtask_id = "job-1"
    post.hms_errors = [_runout_err()]
    spool_respool.note_status_push(printer.id, post)

    # The replayed runout must NOT stamp the fresh spool.
    assert await mark_spent_on_runout(db_session, printer.id, {"0700_8011"}, post) is None
    assert (await db_session.get(Spool, fresh.id)).spent_at is None


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
async def test_gate_remain_jump_prompts_even_above_threshold(db_session, printer_factory, monkeypatch, fake_clock):
    """spent_at NULL and remaining ABOVE the near-empty threshold, but the tray
    reports a CORROBORATED remain-jump on a physically-cycled slot → the Tier-3
    prompt still fires, labelled ``remain_jump``. Both the bound and the arrival
    call sites funnel through maybe_auto_or_prompt_respool, so this proves the
    prompt for both contexts."""
    printer = await printer_factory()
    donor = await _make_donor(db_session, spent=False, weight_used=958.99)  # remaining 41 > 30
    await db_session.commit()
    _record_physical_cycle(printer.id)

    _patch_pm(monkeypatch, _make_state(0, 0, _tray()))
    broadcasts = _spy_broadcast(monkeypatch)
    # First push only opens the corroboration window.
    assert await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, _tray(), donor) is None
    assert [b for b in broadcasts if b["type"] == "respool_prompt"] == []

    fake_clock["t"] += spool_respool._JUMP_STABLE_S
    result = await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, _tray(), donor)

    assert result is None  # a prompt, not an auto-respool
    prompts = [b for b in broadcasts if b["type"] == "respool_prompt"]
    assert len(prompts) == 1
    assert prompts[0]["donor_spool_id"] == donor.id
    assert prompts[0]["trigger"] == "remain_jump"


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
async def test_near_empty_with_recent_cycle_prompts_as_near_empty(db_session, printer_factory, monkeypatch):
    """The same spool DOES prompt once a roll was physically cycled through the
    slot — and is labelled ``near_empty`` so the UI says "almost empty — replacing
    this roll?" instead of claiming a reused tag was detected."""
    printer = await printer_factory()
    donor = await _make_donor(db_session, spent=False, weight_used=990.0)
    await db_session.commit()
    _record_physical_cycle(printer.id)

    _patch_pm(monkeypatch, _make_state(0, 0, _tray()))
    broadcasts = _spy_broadcast(monkeypatch)
    await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, _tray(), donor)

    prompts = [b for b in broadcasts if b["type"] == "respool_prompt"]
    assert len(prompts) == 1
    assert prompts[0]["trigger"] == "near_empty"


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


async def _fire_tier3_prompt(db, printer, monkeypatch, *, weight_used=990.0):
    """Fire a real Tier-3 prompt so _respool_prompt_dedup is populated exactly as
    the live gate populates it. Returns (donor, broadcasts_spy)."""
    donor = await _make_donor(db, spent=False, weight_used=weight_used)  # remaining 10 <= 30
    await db.commit()
    _record_physical_cycle(printer.id)  # the swap evidence Tier 3 requires
    _patch_pm(monkeypatch, _make_state(0, 0, _tray()))
    broadcasts = _spy_broadcast(monkeypatch)
    await maybe_auto_or_prompt_respool(db, printer.id, 0, 0, _tray(), donor)
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
    donor, _ = await _fire_tier3_prompt(db_session, printer, monkeypatch)

    sent, send = _capture_send()
    n = await rebroadcast_unresolved_respool_prompts(db_session, send)

    assert n == 1
    assert len(sent) == 1
    assert sent[0]["type"] == "respool_prompt"
    assert sent[0]["donor_spool_id"] == donor.id
    assert sent[0]["donor_remaining_g"] == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_rebroadcast_payload_matches_live_prompt(db_session, printer_factory, monkeypatch):
    """The replayed payload is identical to the live gate's payload (one contract)."""
    printer = await printer_factory()
    _donor, broadcasts = await _fire_tier3_prompt(db_session, printer, monkeypatch)
    live = next(b for b in broadcasts if b["type"] == "respool_prompt")

    sent, send = _capture_send()
    await rebroadcast_unresolved_respool_prompts(db_session, send)
    assert sent[0] == live


@pytest.mark.asyncio
async def test_rebroadcast_recomputes_trigger_from_durable_state(db_session, printer_factory, monkeypatch):
    """The replayed prompt is labelled from DURABLE state, so a reconnecting client
    gets the same copy the live one did — and follows the row if it changed while the
    client was away (here: the donor was stamped spent in the meantime)."""
    printer = await printer_factory()
    donor, broadcasts = await _fire_tier3_prompt(db_session, printer, monkeypatch)
    assert next(b for b in broadcasts if b["type"] == "respool_prompt")["trigger"] == "near_empty"

    sent, send = _capture_send()
    await rebroadcast_unresolved_respool_prompts(db_session, send)
    assert sent[0]["trigger"] == "near_empty"

    donor.spent_at = datetime.utcnow()  # hardware runout landed while the client was away
    await db_session.commit()

    sent2, send2 = _capture_send()
    await rebroadcast_unresolved_respool_prompts(db_session, send2)
    assert sent2[0]["trigger"] == "spent"


@pytest.mark.asyncio
async def test_rebroadcast_skips_dismissed_donor(db_session, printer_factory, monkeypatch):
    """The dismissal route stamps respool_dismissed_at WITHOUT clearing the in-memory
    dedup — the replay must still suppress a dismissed prompt (F2 correctness)."""
    printer = await printer_factory()
    donor, _ = await _fire_tier3_prompt(db_session, printer, monkeypatch)
    donor.respool_dismissed_at = datetime.utcnow()
    await db_session.commit()

    sent, send = _capture_send()
    n = await rebroadcast_unresolved_respool_prompts(db_session, send)
    assert n == 0 and sent == []


@pytest.mark.asyncio
async def test_rebroadcast_skips_archived_donor(db_session, printer_factory, monkeypatch):
    """A re-spooled / archived donor is not replayed."""
    printer = await printer_factory()
    donor, _ = await _fire_tier3_prompt(db_session, printer, monkeypatch)
    donor.archived_at = datetime.utcnow()
    await db_session.commit()

    sent, send = _capture_send()
    assert await rebroadcast_unresolved_respool_prompts(db_session, send) == 0
    assert sent == []


@pytest.mark.asyncio
async def test_rebroadcast_skips_when_slot_no_longer_holds_tag(db_session, printer_factory, monkeypatch):
    """A slot now empty (or holding a different tag) is stale → no replay."""
    printer = await printer_factory()
    await _fire_tier3_prompt(db_session, printer, monkeypatch)
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
    await _fire_tier3_prompt(db_session, printer, monkeypatch)
    from backend.app.api.routes.settings import set_setting

    await set_setting(db_session, "spoolman_enabled", "true")
    await db_session.commit()

    sent, send = _capture_send()
    assert await rebroadcast_unresolved_respool_prompts(db_session, send) == 0
    assert sent == []


# -- W3: respool_auto_enabled quarantine (Tier-2 auto OFF by default) ----------


@pytest.mark.asyncio
async def test_gate_spent_loaded_prompts_when_auto_disabled(db_session, printer_factory, monkeypatch):
    """respool_auto_enabled defaults OFF: a spent+loaded tag arrival broadcasts the
    one-click PROMPT instead of silently auto-minting a fresh row. A last brand IS
    set, proving the gate is the toggle — not a missing brand.

    Also pins that the hardware-certain path is untouched by the Phase-C evidence
    gates: no physical cycle is recorded here and it still prompts (the runout IS
    the hardware event), carrying ``trigger="spent"`` so the UI keeps the
    reused-tag framing for it."""
    printer = await printer_factory()
    donor = await _make_donor(db_session, spent=True)
    from backend.app.api.routes.settings import set_setting

    await set_setting(db_session, "respool_last_brand", "Polymaker")  # auto WOULD work if enabled
    await db_session.commit()

    _patch_pm(monkeypatch, _make_state(0, 0, _tray(state=11)))
    broadcasts = _spy_broadcast(monkeypatch)
    result = await maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, _tray(state=11), donor)

    assert result is None  # NOT auto-respooled
    prompts = [b for b in broadcasts if b["type"] == "respool_prompt"]
    assert len(prompts) == 1  # prompted instead
    assert prompts[0]["trigger"] == "spent"
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
