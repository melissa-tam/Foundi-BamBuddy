"""Spent-attribution safety: the topology gate, the backup-swap corroboration,
the contradiction detector, and ledger-corrupt-aware prompts (WS3).

The incident these pin: spools 185 and 205 on printer 12 — an H2C running THREE AMS
units behind a dual nozzle — were stamped SPENT on 2026-07-31 with ``weight_used = 0``
while their trays kept reporting LOADED at ``remain = 100 %``. They stayed hard-excluded
from selection for NINE DAYS. Root cause: ``_resolve_exhausted_tray``'s inference tier
trusted ``tray_now``, which on a dual-nozzle machine is a bare SLOT number the MQTT
client has to guess a unit for — a guess whose own fallbacks log "no AMS on extruder N,
using slot M".

The asymmetry every test here defends: a MISSED spent stamp self-heals forward (the next
runout re-fires, the fresh-roll prompt is the backstop), while a FALSE one is effectively
permanent — there is no AUTOMATIC un-spend lane by operator ruling. (The single
exception is operator-ANSWERED and evidence-gated: ``POST
/inventory/spools/{id}/respool-dismiss`` NULLs ``spent_at`` when the live AMS remain
contradicts the stamp.) So the gates all fail toward NOT stamping, and the detector that
finds the residue is forbidden from writing a spool row.
"""

import logging
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services import spool_respool
from backend.app.services.bambu_mqtt import BambuMQTTClient, HMSError
from backend.app.services.notification_service import notification_service
from backend.app.services.spool_respool import (
    _ams_hint_from_short_codes,
    detect_spent_contradictions,
    mark_spent_on_runout,
    mark_spent_on_slot_runout,
)

# --- wire fixtures -----------------------------------------------------------

TAG_UID = "AABBCCDD11223344"
TRAY_UUID = "AABBCCDD11223344AABBCCDD11223344"

# `0700_2X00` runout attrs: high byte 0x07 = AMS module, next byte = unit, third byte
# = 0x20 + slot. Verified decodes live in hms_errors.ams_slot_from_attr's docstring.
_DEMAND_CODE = 0x00020001  # the "fill THIS slot now" family
_AUTO_SWITCH_CODE = 0x00030002  # "…ran out and automatically switched" (spent evidence)


def _attr(ams_id: int, slot: int) -> int:
    return (0x07 << 24) | (ams_id << 16) | ((0x20 + slot) << 8)


def _hms(ams_id: int, slot: int, code: int) -> HMSError:
    """A slot-attributed AMS HMS entry, shaped as the wire delivers it."""
    return HMSError(code=code, attr=_attr(ams_id, slot), module=7, severity=2)


def _tray(*, tray_id=0, state=11, remain=100, tag_uid=TAG_UID, tray_uuid=TRAY_UUID, tray_type="PETG"):
    return {
        "id": tray_id,
        "tray_type": tray_type,
        "tray_sub_brands": "PETG HF",
        "tray_color": "00FF00FF",
        "tray_info_idx": "GFG02",
        "tag_uid": tag_uid,
        "tray_uuid": tray_uuid,
        "state": state,
        "remain": remain,
    }


def _h2c_state(*, tray_now, units=(0, 1, 2), hms=(), gcode_state="PAUSE", subtask_id="job-A"):
    """A three-AMS H2C-shaped push. ``tray_now`` is what the client resolved it to —
    on this topology that is a bare slot number the H2D disambiguation had to guess a
    unit for, which is exactly the value the inference tier used to trust."""
    state = MagicMock()
    state.state = gcode_state
    state.tray_now = tray_now
    state.subtask_id = subtask_id
    state.hms_errors = list(hms)
    state.raw_data = {"ams": [{"id": u, "tray": [_tray(tray_id=t) for t in range(4)]} for u in units]}
    return state


def _single_ams_state(*, tray_now, hms=(), gcode_state="PAUSE", subtask_id="job-A"):
    state = MagicMock()
    state.state = gcode_state
    state.tray_now = tray_now
    state.subtask_id = subtask_id
    state.hms_errors = list(hms)
    state.raw_data = {"ams": [{"id": 0, "tray": [_tray(tray_id=t) for t in range(4)]}]}
    return state


def _client(*, model: str, dual_runtime: bool = False) -> BambuMQTTClient:
    """A REAL MQTT client, so the topology gate exercises the real ``is_dual_nozzle``
    property (runtime flag OR model fallback) rather than a stub of it."""
    client = BambuMQTTClient(ip_address="192.168.1.50", serial_number="TESTH2C", access_code="12345678")
    client.model = model
    client._is_dual_nozzle = dual_runtime
    return client


@pytest.fixture(autouse=True)
def _reset_module_state():
    from backend.app.services import ams_presence

    spool_respool._reset_state()
    ams_presence._reset_state()
    yield
    spool_respool._reset_state()
    ams_presence._reset_state()


@pytest.fixture
def wire(monkeypatch):
    """Patch printer_manager's status/client/pushall surface and record the pushalls."""
    from backend.app.services.printer_manager import printer_manager

    box = {"state": None, "client": None, "pushalls": []}

    def _pushall(pid, reason):
        box["pushalls"].append((pid, reason))
        return True

    monkeypatch.setattr(printer_manager, "get_status", lambda _pid: box["state"])
    monkeypatch.setattr(printer_manager, "get_client", lambda _pid: box["client"])
    monkeypatch.setattr(printer_manager, "request_evidence_pushall", _pushall)
    return box


async def _bind(db, printer_id, ams_id, tray_id, **kwargs):
    fields = {
        "material": "PETG",
        "color_name": "Green",
        "rgba": "00FF00FF",
        "brand": "Bambu Lab",
        "label_weight": 1000,
        "core_weight": 250,
        "weight_used": 0.0,
    }
    fields.update(kwargs)
    spool = Spool(**fields)
    spool.k_profiles = []
    spool.assignments = []
    db.add(spool)
    await db.flush()
    db.add(SpoolAssignment(spool_id=spool.id, printer_id=printer_id, ams_id=ams_id, tray_id=tray_id))
    await db.commit()
    return spool


# ===========================================================================
# D1 — the 185/205 reproduction and the topology gate
# ===========================================================================


def test_ams_hint_decodes_the_unit_the_trigger_names():
    """``07XX_8011`` names its AMS unit in the short code's module half; the
    extruder-module runout names none; disagreement is unknown, not a tie to break."""
    assert _ams_hint_from_short_codes({"0700_8011"}) == 0
    assert _ams_hint_from_short_codes({"0701_8011"}) == 1
    assert _ams_hint_from_short_codes({"0702_8011"}) == 2
    assert _ams_hint_from_short_codes({"0300_8004"}) is None  # extruder module, not AMS
    assert _ams_hint_from_short_codes({"0701_8011", "0702_8011"}) is None  # two units disagree
    assert _ams_hint_from_short_codes({"0300_8004", "0701_8011"}) == 1  # only one NAMES a unit
    assert _ams_hint_from_short_codes(set()) is None
    assert _ams_hint_from_short_codes({"garbage", None}) is None


@pytest.mark.asyncio
async def test_h2c_wrong_unit_inference_does_not_stamp_incident_pin(db_session, printer_factory, wire, caplog):
    """THE 185/205 REPRODUCTION.

    Printer 12's shape: three AMS units, dual nozzle. The firmware raises ``0701_8011``
    (AMS 1 ran dry — the slot-agnostic "refill the same slot" runout, so no slot is
    named) while the client's ``tray_now`` reads a bare ``2``, which decodes to AMS 0
    slot 2 — the WRONG UNIT. The old inference tier stamped that spool.

    Now: nothing is stamped, the refusal is loud, a fresh report is requested, and the
    hinted unit's seated slots get a read occasion so the next pass can learn the truth.
    """
    printer = await printer_factory(model="H2C")
    victim = await _bind(db_session, printer.id, 0, 2)  # what tray_now WRONGLY points at
    wire["state"] = _h2c_state(tray_now=2)
    wire["client"] = _client(model="H2C")

    with caplog.at_level(logging.WARNING, logger="backend.app.services.spool_respool"):
        marked = await mark_spent_on_runout(db_session, printer.id, {"0701_8011"}, wire["state"])

    assert marked is None
    await db_session.refresh(victim)
    assert victim.spent_at is None, "a healthy loaded roll must never be stamped on a guessed unit"
    assert "runout attribution ambiguous (multi-AMS/dual-nozzle) — not stamping; owe evidence" in caplog.text
    assert (printer.id, "spent_attribution") in wire["pushalls"]

    # The hinted unit (AMS 1) owes a read: one occasion per PRESENT slot, and none
    # anywhere else — an occasion is permission for one read, not a read.
    from backend.app.services import ams_presence

    opened = set(ams_presence._read_occasion_at)
    assert opened == {(printer.id, 1, t) for t in range(4)}


@pytest.mark.asyncio
async def test_h2c_dual_nozzle_detected_from_runtime_flag_alone(db_session, printer_factory, wire):
    """The gate must not depend on the printer being REGISTERED as a dual model: the
    runtime ``device.extruder.info`` flag is the primary signal, and a mis-modelled real
    dual is exactly the machine that would otherwise be mis-attributed."""
    printer = await printer_factory(model="Mislabelled")
    victim = await _bind(db_session, printer.id, 0, 1)
    wire["state"] = _h2c_state(tray_now=1, units=(0,))  # ONE unit — only the nozzle is ambiguous
    wire["client"] = _client(model="Mislabelled", dual_runtime=True)

    assert await mark_spent_on_runout(db_session, printer.id, {"0701_8011"}, wire["state"]) is None
    await db_session.refresh(victim)
    assert victim.spent_at is None


@pytest.mark.asyncio
async def test_h2c_demand_entry_still_stamps_the_slot_the_firmware_named(db_session, printer_factory, wire):
    """Wire truth outranks the gate. A standing DEMAND names AMS 1 slot 3 outright, so
    step 1 answers and the inference tier — with all its topology doubt — is never
    reached. The gate narrows a GUESS; it must never suppress an attribution."""
    printer = await printer_factory(model="H2C")
    named = await _bind(db_session, printer.id, 1, 3)
    decoy = await _bind(db_session, printer.id, 0, 2)
    demand = _hms(1, 3, _DEMAND_CODE)
    wire["state"] = _h2c_state(tray_now=2, hms=[demand])
    wire["client"] = _client(model="H2C")

    marked = await mark_spent_on_runout(db_session, printer.id, {"0701_8011"}, wire["state"])

    assert marked is not None and marked.id == named.id
    await db_session.refresh(decoy)
    assert decoy.spent_at is None
    assert named.weight_used == 0.0, "the gram ledger stays intact — emptiness is derived from spent_at"


@pytest.mark.asyncio
async def test_hint_disagreeing_with_the_attr_decode_warns_but_never_overrides(
    db_session, printer_factory, wire, caplog
):
    """A hint that disagrees with slot-attributed wire truth is misattribution
    TELEMETRY, not a veto — the firmware naming a slot outranks a module byte."""
    printer = await printer_factory(model="H2C")
    named = await _bind(db_session, printer.id, 0, 1)
    wire["state"] = _h2c_state(tray_now=255, hms=[_hms(0, 1, _DEMAND_CODE)])
    wire["client"] = _client(model="H2C")

    with caplog.at_level(logging.WARNING, logger="backend.app.services.spool_respool"):
        marked = await mark_spent_on_runout(db_session, printer.id, {"0702_8011"}, wire["state"])

    assert marked is not None and marked.id == named.id
    assert "runout unit hint disagrees with the firmware's slot attribution" in caplog.text


@pytest.mark.asyncio
async def test_ambiguous_topology_accepts_tray_now_inside_the_hinted_unit(db_session, printer_factory, wire):
    """Corroboration, not paralysis: when the inferred tray decodes INTO the unit the
    firmware named, the two independent signals agree and the stamp stands."""
    printer = await printer_factory(model="H2C")
    spool = await _bind(db_session, printer.id, 1, 2)
    wire["state"] = _h2c_state(tray_now=6)  # global 6 -> AMS 1 slot 2, matching the hint
    wire["client"] = _client(model="H2C")

    marked = await mark_spent_on_runout(db_session, printer.id, {"0701_8011"}, wire["state"])

    assert marked is not None and marked.id == spool.id


@pytest.mark.asyncio
async def test_single_ams_single_nozzle_inference_still_stamps_regression_pin(db_session, printer_factory, wire):
    """REGRESSION PIN. On one AMS unit behind one nozzle ``tray_now`` IS the global tray
    — nothing was guessed — so the inference tier keeps working exactly as before. The
    fix must narrow the ambiguous case only."""
    printer = await printer_factory(model="H2S")
    spool = await _bind(db_session, printer.id, 0, 1)
    wire["state"] = _single_ams_state(tray_now=1)
    wire["client"] = _client(model="H2S")

    marked = await mark_spent_on_runout(db_session, printer.id, {"0300_8004"}, wire["state"])

    assert marked is not None and marked.id == spool.id
    assert marked.spent_at is not None


@pytest.mark.asyncio
async def test_auto_switch_trigger_never_falls_through_to_inference(db_session, printer_factory, wire):
    """``mark_spent_on_slot_runout`` needs no topology gate because it has no inference
    path: an event whose attr does not decode to a slot is SKIPPED, even with a live
    ``tray_now`` sitting right there. This pins that property so a future fallback
    cannot be added without noticing it inherits the 185/205 class."""
    printer = await printer_factory(model="H2C")
    spool = await _bind(db_session, printer.id, 0, 2)
    wire["state"] = _h2c_state(tray_now=2)
    wire["client"] = _client(model="H2C")

    # attr 0 decodes to no AMS slot at all.
    stamped = await mark_spent_on_slot_runout(
        db_session, printer.id, [("00000000" + f"{_AUTO_SWITCH_CODE:08X}", 0, _AUTO_SWITCH_CODE)], wire["state"]
    )

    assert stamped == []
    await db_session.refresh(spool)
    assert spool.spent_at is None


# ===========================================================================
# D2 — backup-swap corroboration on an ambiguous topology
# ===========================================================================


@pytest.fixture
def fake_clock(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(spool_respool, "_monotonic", lambda: clock["t"])
    return clock


def _swap_push(tray_now, *, units=(0, 1, 2), filam_bak=None, device_bak=None, subtask_id="job-A"):
    state = MagicMock()
    state.state = "RUNNING"
    state.tray_now = tray_now
    state.subtask_id = subtask_id
    raw = {"ams": [{"id": u, "tray": [_tray(tray_id=t) for t in range(4)]} for u in units]}
    if filam_bak is not None:
        raw["filam_bak"] = filam_bak
    if device_bak is not None:
        raw["device"] = {"extruder": {"info": [{"id": i, "filam_bak": g} for i, g in enumerate(device_bak)]}}
    state.raw_data = raw
    return state


def _sample(printer_id, tray, **kw) -> list[int]:
    """One status push through the REAL sampler — sync and DB-less, exactly as
    ``main.on_printer_status_change`` drives it ~1 Hz per printer."""
    return spool_respool.sample_status_push(printer_id, _swap_push(tray, **kw))


def _stable_feeder(printer_id, tray, clock, **kw):
    """Confirm ``tray`` as the stable feeder: two same-value samples ≥ _SWAP_CONFIRM_S
    apart after a seeding push. Nothing confirms, so no session is involved at all."""
    assert _sample(printer_id, tray, **kw) == []
    assert _sample(printer_id, tray, **kw) == []
    clock["t"] += spool_respool._SWAP_CONFIRM_S + 1
    assert _sample(printer_id, tray, **kw) == []
    assert spool_respool._stable_feeder.get(printer_id) == tray


async def _drive_swap(session_factory, printer_id, tray, **kw) -> Spool | None:
    """One status push through BOTH production halves, wired as ``main`` wires them:
    the sync sampler decides, and — only on a confirmation — the async confirmer stamps
    on its OWN session. Returns the first spool stamped by this push, else None."""
    departed = _sample(printer_id, tray, **kw)
    if not departed:
        return None
    stamped = await spool_respool.confirm_backup_swaps(printer_id, departed, session_factory=session_factory)
    return stamped[0] if stamped else None


@pytest.mark.asyncio
async def test_ambiguous_swap_without_filam_bak_declines_and_warns(
    db_session, printer_factory, wire, fake_clock, caplog, own_session_factory
):
    """No grouping evidence at all → no stamp. On a bare-slot topology a feeder change
    is not by itself proof a roll ran dry, and the fat-remainder WARNING plus the
    fresh-roll prompt remain the backstops."""
    printer = await printer_factory(model="H2C")
    wire["client"] = _client(model="H2C")
    departed = await _bind(db_session, printer.id, 0, 0, weight_used=500.0)

    _stable_feeder(printer.id, 0, fake_clock)
    assert _sample(printer.id, 1) == []  # open the pending swap
    fake_clock["t"] += spool_respool._SWAP_CONFIRM_S + 1
    with caplog.at_level(logging.WARNING, logger="backend.app.services.spool_respool"):
        stamped = await _drive_swap(own_session_factory, printer.id, 1)

    assert stamped is None
    await db_session.refresh(departed)
    assert departed.spent_at is None
    assert "backup-swap spent stamp declined" in caplog.text
    assert "was never reported" in caplog.text


@pytest.mark.asyncio
async def test_ambiguous_swap_with_filam_bak_group_stamps(
    db_session, printer_factory, wire, fake_clock, own_session_factory
):
    """The firmware's OWN grouping pairs the two trays → the switch it performed between
    them IS an auto-refill, and the departed roll ran dry. Stamp."""
    printer = await printer_factory(model="H2C")
    wire["client"] = _client(model="H2C")
    departed = await _bind(db_session, printer.id, 0, 0, weight_used=500.0)

    _stable_feeder(printer.id, 0, fake_clock, filam_bak=[0, 1])
    assert _sample(printer.id, 1, filam_bak=[0, 1]) == []
    fake_clock["t"] += spool_respool._SWAP_CONFIRM_S + 1
    stamped = await _drive_swap(own_session_factory, printer.id, 1, filam_bak=[0, 1])

    assert stamped is not None and stamped.id == departed.id
    assert stamped.spent_at is not None
    assert stamped.weight_used == 500.0, "the true ledger survives the stamp"


@pytest.mark.asyncio
async def test_ambiguous_swap_with_group_not_pairing_the_trays_declines(
    db_session, printer_factory, wire, fake_clock, caplog, own_session_factory
):
    """A group exists but does NOT contain both trays — the feeder change crossed
    groups, so it is a tool change or a remap, never an auto-refill."""
    printer = await printer_factory(model="H2C")
    wire["client"] = _client(model="H2C")
    departed = await _bind(db_session, printer.id, 0, 0, weight_used=500.0)

    _stable_feeder(printer.id, 0, fake_clock, filam_bak=[0, 3])
    assert _sample(printer.id, 1, filam_bak=[0, 3]) == []
    fake_clock["t"] += spool_respool._SWAP_CONFIRM_S + 1
    with caplog.at_level(logging.WARNING, logger="backend.app.services.spool_respool"):
        stamped = await _drive_swap(own_session_factory, printer.id, 1, filam_bak=[0, 3])

    assert stamped is None
    await db_session.refresh(departed)
    assert departed.spent_at is None
    assert "does not pair these trays" in caplog.text


@pytest.mark.asyncio
async def test_per_extruder_filam_bak_shape_is_read(db_session, printer_factory, wire, fake_clock, own_session_factory):
    """Shape B: ``print.device.extruder.info[i].filam_bak``. A dual-nozzle machine
    reports groups per EXTRUDER, and each extruder is its own group — two nozzles do not
    back each other up."""
    printer = await printer_factory(model="H2C")
    wire["client"] = _client(model="H2C")
    departed = await _bind(db_session, printer.id, 0, 0, weight_used=500.0)
    groups = [[0, 1], [8, 9]]  # right nozzle pairs 0/1; left nozzle pairs 8/9

    _stable_feeder(printer.id, 0, fake_clock, device_bak=groups)
    assert _sample(printer.id, 1, device_bak=groups) == []
    fake_clock["t"] += spool_respool._SWAP_CONFIRM_S + 1
    stamped = await _drive_swap(own_session_factory, printer.id, 1, device_bak=groups)

    assert stamped is not None and stamped.id == departed.id


@pytest.mark.asyncio
async def test_filam_bak_cache_survives_pushes_that_omit_it(
    db_session, printer_factory, wire, fake_clock, own_session_factory
):
    """``bambu_mqtt`` replaces ``raw_data`` wholesale and preserves only ams / vt_tray /
    ams_extruder_map / mapping, so an incremental push drops ``filam_bak`` entirely. The
    LAST-SEEN grouping must stand, or corroboration would be a coin flip on push timing."""
    printer = await printer_factory(model="H2C")
    wire["client"] = _client(model="H2C")
    departed = await _bind(db_session, printer.id, 0, 0, weight_used=500.0)

    _stable_feeder(printer.id, 0, fake_clock, filam_bak=[0, 1])
    # Every push from here on omits the field.
    assert _sample(printer.id, 1) == []
    fake_clock["t"] += spool_respool._SWAP_CONFIRM_S + 1
    stamped = await _drive_swap(own_session_factory, printer.id, 1)

    assert stamped is not None and stamped.id == departed.id


@pytest.mark.asyncio
async def test_unambiguous_topology_swap_needs_no_corroboration(
    db_session, printer_factory, wire, fake_clock, own_session_factory
):
    """Single AMS, single nozzle: unchanged behavior, no ``filam_bak`` required. The
    corroboration is a narrowing of the ambiguous case ONLY."""
    printer = await printer_factory(model="H2S")
    wire["client"] = _client(model="H2S")
    departed = await _bind(db_session, printer.id, 0, 0, weight_used=500.0)

    _stable_feeder(printer.id, 0, fake_clock, units=(0,))
    assert _sample(printer.id, 1, units=(0,)) == []
    fake_clock["t"] += spool_respool._SWAP_CONFIRM_S + 1
    stamped = await _drive_swap(own_session_factory, printer.id, 1, units=(0,))

    assert stamped is not None and stamped.id == departed.id


# ===========================================================================
# D3 — the spent-contradiction detector
# ===========================================================================


def _live(printer_id, ams_id, tray_id, tray):
    manager = MagicMock()
    state = MagicMock()
    state.raw_data = {"ams": [{"id": ams_id, "tray": [tray]}]}
    manager.get_status = lambda pid: state if pid == printer_id else None
    return manager


@pytest.mark.asyncio
async def test_detector_reports_contradiction_and_never_mutates_the_row(db_session, printer_factory):
    """R2 PIN: the 185/205 shape — spent, still assigned, tray present with the same
    identity, wire says 100 % full. It must be LOUD and it must change NOTHING. There is
    no AUTOMATIC un-spend lane by operator ruling, and inventing one here would replace a
    visible wrong stamp with an invisible one; the one deliberate un-spend is
    operator-ANSWERED and evidence-gated (``POST /inventory/spools/{id}/respool-dismiss``
    NULLs ``spent_at`` when the live AMS remain contradicts the stamp)."""
    printer = await printer_factory(model="H2C")
    spool = await _bind(
        db_session, printer.id, 1, 0, weight_used=0.0, tag_uid=TAG_UID, tray_uuid=TRAY_UUID, spent_at=datetime.utcnow()
    )
    spent_before = spool.spent_at
    manager = _live(printer.id, 1, 0, _tray(remain=100))

    with patch.object(notification_service, "on_spent_contradiction", new_callable=AsyncMock) as notify:
        found = await detect_spent_contradictions(db_session, manager)

    assert found == 1
    notify.assert_awaited_once()
    args = notify.await_args.args
    assert args[2] == spool.id
    assert args[4] == "AMS B slot 1"  # ams_id 1 -> letter B, tray 0 -> slot 1
    assert args[5] == 100

    await db_session.refresh(spool)
    assert spool.spent_at == spent_before, "the detector must NEVER clear or move a spent stamp"
    assert spool.weight_used == 0.0
    assert spool.archived_at is None


SIBLING_TAG_UID = "3CF1F3E700000100"


@pytest.mark.asyncio
async def test_detector_still_fires_when_the_tray_shows_the_sibling_chip(db_session, printer_factory):
    """A Bambu roll carries two chips, and which one the AMS reads is a coin flip on how
    the roll was seated. The identity check therefore has to accept EITHER, or the
    detector silently skips a genuine contradiction for as long as the roll faces its far
    side — a loud-by-design lane going quiet on roll orientation.

    Same 185/205 fixture as above; only the chip on the wire differs.
    """
    printer = await printer_factory(model="H2C")
    spool = await _bind(
        db_session,
        printer.id,
        1,
        0,
        weight_used=0.0,
        tag_uid=TAG_UID,
        sibling_tag_uid=SIBLING_TAG_UID,
        tray_uuid=TRAY_UUID,
        spent_at=datetime.utcnow(),
    )
    # The push carries the FAR chip and no uuid — the incremental-push shape.
    manager = _live(printer.id, 1, 0, _tray(remain=100, tag_uid=SIBLING_TAG_UID, tray_uuid=None))

    with patch.object(notification_service, "on_spent_contradiction", new_callable=AsyncMock) as notify:
        found = await detect_spent_contradictions(db_session, manager)

    assert found == 1, "the far chip identifies the bound roll just as the near one does"
    notify.assert_awaited_once()
    assert notify.await_args.args[2] == spool.id


@pytest.mark.asyncio
async def test_detector_still_ignores_a_genuinely_different_roll(db_session, printer_factory):
    """The widening must not become "any tag counts". A chip belonging to neither
    recorded side is an ordinary swap the binding has not caught up with — not a
    contradiction."""
    printer = await printer_factory(model="H2C")
    await _bind(
        db_session,
        printer.id,
        1,
        0,
        weight_used=0.0,
        tag_uid=TAG_UID,
        sibling_tag_uid=SIBLING_TAG_UID,
        tray_uuid=TRAY_UUID,
        spent_at=datetime.utcnow(),
    )
    manager = _live(printer.id, 1, 0, _tray(remain=100, tag_uid="A5E7210D00000100", tray_uuid=None))

    with patch.object(notification_service, "on_spent_contradiction", new_callable=AsyncMock) as notify:
        found = await detect_spent_contradictions(db_session, manager)

    assert found == 0
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_detector_dedupes_within_the_renotify_window(db_session, printer_factory):
    """The contradiction is a STANDING state that re-derives on every pass, so the
    durable ledger — not an in-memory gate that a deploy would clear — paces it."""
    printer = await printer_factory(model="H2C")
    await _bind(
        db_session, printer.id, 0, 0, weight_used=0.0, tag_uid=TAG_UID, tray_uuid=TRAY_UUID, spent_at=datetime.utcnow()
    )
    manager = _live(printer.id, 0, 0, _tray(remain=100))

    with patch.object(notification_service, "on_spent_contradiction", new_callable=AsyncMock) as notify:
        assert await detect_spent_contradictions(db_session, manager, now=1000.0) == 1
        # Second pass, far past the scan floor but well inside the 7-day notify window.
        assert await detect_spent_contradictions(db_session, manager, now=1_000_000.0) == 1
    assert notify.await_count == 1, "one alert per contradiction per re-notify window"


@pytest.mark.asyncio
async def test_detector_notifies_per_spool(db_session, printer_factory):
    """Dedup is keyed by SPOOL, so a second contradicted roll gets its own alert rather
    than hiding behind the first."""
    printer = await printer_factory(model="H2C")
    await _bind(
        db_session, printer.id, 0, 0, weight_used=0.0, tag_uid=TAG_UID, tray_uuid=TRAY_UUID, spent_at=datetime.utcnow()
    )
    other_uuid = "BBBBCCCC11223344AABBCCDD11223344"
    await _bind(
        db_session,
        printer.id,
        0,
        1,
        weight_used=0.0,
        tag_uid="BBBBCCCC11223344",
        tray_uuid=other_uuid,
        spent_at=datetime.utcnow(),
    )
    manager = MagicMock()
    state = MagicMock()
    state.raw_data = {
        "ams": [
            {
                "id": 0,
                "tray": [
                    _tray(tray_id=0, remain=100),
                    _tray(tray_id=1, remain=100, tag_uid="BBBBCCCC11223344", tray_uuid=other_uuid),
                ],
            }
        ]
    }
    manager.get_status = lambda _pid: state

    with patch.object(notification_service, "on_spent_contradiction", new_callable=AsyncMock) as notify:
        assert await detect_spent_contradictions(db_session, manager) == 2
    assert notify.await_count == 2


@pytest.mark.asyncio
async def test_detector_untagged_binding_uses_the_fingerprint_arm(db_session, printer_factory):
    """An untagged (tagless-minted) binding has no RFID to compare, so the codebase's
    existing fingerprint idiom — canonical material + colour within tolerance — is the
    identity test. Weak evidence, tolerable ONLY because the detector cannot mutate."""
    printer = await printer_factory(model="H2S")
    spool = await _bind(
        db_session, printer.id, 0, 0, weight_used=0.0, tag_uid=None, tray_uuid=None, spent_at=datetime.utcnow()
    )
    # The seated roll IS tagged (that is how a remain% exists at all) and matches the
    # untagged row's material + colour.
    manager = _live(printer.id, 0, 0, _tray(remain=95))

    with patch.object(notification_service, "on_spent_contradiction", new_callable=AsyncMock) as notify:
        assert await detect_spent_contradictions(db_session, manager) == 1
    notify.assert_awaited_once()
    await db_session.refresh(spool)
    assert spool.spent_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tray_kwargs", "why"),
    [
        ({"remain": 5}, "a genuinely spent roll reading near-empty is not a contradiction"),
        ({"remain": -1}, "a tagless tray reports no fullness at all — nothing to contradict"),
        ({"state": 9, "tray_type": ""}, "an empty tray is a RELEASE question, not a contradiction"),
        (
            {"tag_uid": "9999888877776666", "tray_uuid": "99998888777766665555444433332222"},
            "a DIFFERENT roll in the slot is an ordinary swap the binding has not caught up with",
        ),
    ],
)
async def test_detector_stays_quiet_when_the_facts_do_not_contradict(db_session, printer_factory, tray_kwargs, why):
    printer = await printer_factory(model="H2S")
    await _bind(
        db_session, printer.id, 0, 0, weight_used=0.0, tag_uid=TAG_UID, tray_uuid=TRAY_UUID, spent_at=datetime.utcnow()
    )
    manager = _live(printer.id, 0, 0, _tray(**tray_kwargs))

    with patch.object(notification_service, "on_spent_contradiction", new_callable=AsyncMock) as notify:
        assert await detect_spent_contradictions(db_session, manager) == 0, why
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_detector_ignores_healthy_and_archived_rows(db_session, printer_factory):
    """Only SPENT, un-archived rows are candidates."""
    printer = await printer_factory(model="H2S")
    await _bind(db_session, printer.id, 0, 0, weight_used=0.0, tag_uid=TAG_UID, tray_uuid=TRAY_UUID)  # not spent
    await _bind(
        db_session,
        printer.id,
        0,
        1,
        weight_used=0.0,
        tag_uid=TAG_UID,
        tray_uuid=TRAY_UUID,
        spent_at=datetime.utcnow(),
        archived_at=datetime.utcnow(),
    )
    manager = MagicMock()
    state = MagicMock()
    state.raw_data = {"ams": [{"id": 0, "tray": [_tray(tray_id=0), _tray(tray_id=1)]}]}
    manager.get_status = lambda _pid: state

    with patch.object(notification_service, "on_spent_contradiction", new_callable=AsyncMock) as notify:
        assert await detect_spent_contradictions(db_session, manager) == 0
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_detector_is_throttled_between_passes(db_session, printer_factory):
    """Its host tick runs every ~20 s; this walk is worth ~15 minutes."""
    printer = await printer_factory(model="H2S")
    await _bind(
        db_session, printer.id, 0, 0, weight_used=0.0, tag_uid=TAG_UID, tray_uuid=TRAY_UUID, spent_at=datetime.utcnow()
    )
    manager = _live(printer.id, 0, 0, _tray(remain=100))

    with patch.object(notification_service, "on_spent_contradiction", new_callable=AsyncMock):
        assert await detect_spent_contradictions(db_session, manager, now=500.0) == 1
        assert await detect_spent_contradictions(db_session, manager, now=600.0) == 0, "inside the floor"
        assert (
            await detect_spent_contradictions(
                db_session, manager, now=500.0 + spool_respool._SPENT_CONTRADICTION_MIN_INTERVAL_S
            )
            == 1
        )


@pytest.mark.asyncio
async def test_detector_survives_an_unreadable_printer(db_session, printer_factory):
    """One bad row must not abort the sweep, and nothing here may kill the tick."""
    printer = await printer_factory(model="H2S")
    await _bind(
        db_session, printer.id, 0, 0, weight_used=0.0, tag_uid=TAG_UID, tray_uuid=TRAY_UUID, spent_at=datetime.utcnow()
    )
    manager = MagicMock()
    manager.get_status = MagicMock(side_effect=RuntimeError("printer exploded"))

    assert await detect_spent_contradictions(db_session, manager) == 0


@pytest.mark.asyncio
async def test_reconcile_slot_config_actually_invokes_the_detector_liveness_pin(db_session, printer_factory):
    """LIVENESS PIN. A suppression/detection feature that is never CALLED is
    indistinguishable from one that finds nothing — the lesson the 2026-08-07 slot
    deadlock shipped green on. So drive the REAL ``reconcile_slot_config`` and observe
    the detector run, rather than trusting the call site by inspection."""
    from backend.app.services import spool_tagless

    printer = await printer_factory(model="H2S")
    await _bind(
        db_session, printer.id, 0, 0, weight_used=0.0, tag_uid=TAG_UID, tray_uuid=TRAY_UUID, spent_at=datetime.utcnow()
    )
    manager = _live(printer.id, 0, 0, _tray(remain=100))
    spool_tagless._reset_state()

    with patch.object(notification_service, "on_spent_contradiction", new_callable=AsyncMock) as notify:
        await spool_tagless.reconcile_slot_config(db_session, manager=manager)

    assert notify.await_count == 1, "the reconcile tail must actually reach the detector"


# ===========================================================================
# D4 — ledger-corrupt-aware spent prompts
# ===========================================================================


@pytest.mark.asyncio
async def test_spent_prompt_flags_an_impossible_ledger_and_omits_the_number(
    db_session, printer_factory, wire, caplog, monkeypatch
):
    """Prod prompts announced "remaining −792.9 g". The question still deserves asking —
    a spent+loaded spool is exactly what the respool prompt is for — so the prompt is NOT
    suppressed; only the impossible NUMBER is withdrawn, from the payload flag and the
    log line alike."""
    printer = await printer_factory(model="H2S")
    donor = Spool(
        material="PETG",
        color_name="Green",
        rgba="00FF00FF",
        brand="Bambu Lab",
        label_weight=1000,
        core_weight=250,
        weight_used=1792.9,  # impossible: remaining = -792.9 g
        tag_uid=TAG_UID,
        tray_uuid=TRAY_UUID,
        spent_at=datetime.utcnow(),
    )
    donor.k_profiles = []
    donor.assignments = []
    db_session.add(donor)
    await db_session.commit()

    tray = _tray()
    state = MagicMock()
    state.raw_data = {"ams": [{"id": 0, "tray": [tray]}]}
    wire["state"] = state
    sent = []
    monkeypatch.setattr(spool_respool.ws_manager, "broadcast", AsyncMock(side_effect=lambda p: sent.append(p)))

    with caplog.at_level(logging.INFO, logger="backend.app.services.spool_respool"):
        await spool_respool.maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, tray, donor)

    assert len(sent) == 1, "a spent+loaded spool must still raise the prompt"
    assert sent[0]["ledger_unreliable"] is True
    assert "remaining=UNRELIABLE" in caplog.text
    assert "-792.9" not in caplog.text and "−792.9" not in caplog.text


@pytest.mark.asyncio
async def test_healthy_ledger_spent_prompt_keeps_its_numbers(db_session, printer_factory, wire, monkeypatch):
    """The flag is FALSE on a plausible row — the provenance numbers are what let an
    operator tell a stale question from a fresh detection, and must not be lost."""
    printer = await printer_factory(model="H2S")
    donor = Spool(
        material="PETG",
        color_name="Green",
        rgba="00FF00FF",
        brand="Bambu Lab",
        label_weight=1000,
        core_weight=250,
        weight_used=960.0,
        tag_uid=TAG_UID,
        tray_uuid=TRAY_UUID,
        spent_at=datetime.utcnow(),
    )
    donor.k_profiles = []
    donor.assignments = []
    db_session.add(donor)
    await db_session.commit()

    tray = _tray()
    state = MagicMock()
    state.raw_data = {"ams": [{"id": 0, "tray": [tray]}]}
    wire["state"] = state
    sent = []
    monkeypatch.setattr(spool_respool.ws_manager, "broadcast", AsyncMock(side_effect=lambda p: sent.append(p)))

    await spool_respool.maybe_auto_or_prompt_respool(db_session, printer.id, 0, 0, tray, donor)

    assert len(sent) == 1
    assert sent[0]["ledger_unreliable"] is False
    assert sent[0]["donor_remaining_g"] == pytest.approx(40.0)
