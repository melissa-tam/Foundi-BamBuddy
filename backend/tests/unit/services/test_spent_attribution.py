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
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.services import spool_respool
from backend.app.services.bambu_mqtt import BambuMQTTClient, HMSError
from backend.app.services.notification_service import notification_service
from backend.app.services.spool_respool import (
    _ams_hint_from_short_codes,
    detect_spent_contradictions,
    mark_spent_on_runout,
    mark_spent_on_slot_runout,
)
from backend.app.services.spool_tagless import reattribute_early_runout

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
    """``filam_bak`` / ``device_bak`` carry the wire's own words: each element of either
    array is one backup group's slot BITMASK (``[3]`` = slots 0+1, ``[9]`` = slots 0+3),
    confirmed for AMS unit 0 on 2026-08-25 — see ``tray_fields.parse_filam_bak``."""
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

    _stable_feeder(printer.id, 0, fake_clock, filam_bak=[3])  # 0b0011 — slots 0+1
    assert _sample(printer.id, 1, filam_bak=[3]) == []
    fake_clock["t"] += spool_respool._SWAP_CONFIRM_S + 1
    stamped = await _drive_swap(own_session_factory, printer.id, 1, filam_bak=[3])

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

    _stable_feeder(printer.id, 0, fake_clock, filam_bak=[9])  # 0b1001 — slots 0+3, not 1
    assert _sample(printer.id, 1, filam_bak=[9]) == []
    fake_clock["t"] += spool_respool._SWAP_CONFIRM_S + 1
    with caplog.at_level(logging.WARNING, logger="backend.app.services.spool_respool"):
        stamped = await _drive_swap(own_session_factory, printer.id, 1, filam_bak=[9])

    assert stamped is None
    await db_session.refresh(departed)
    assert departed.spent_at is None
    assert "does not pair these trays" in caplog.text


@pytest.mark.asyncio
async def test_per_extruder_filam_bak_shape_is_read(db_session, printer_factory, wire, fake_clock, own_session_factory):
    """Shape B: ``print.device.extruder.info[i].filam_bak``. A dual-nozzle machine
    reports groups per EXTRUDER, and each extruder's masks stay their own groups — two
    nozzles do not back each other up."""
    printer = await printer_factory(model="H2C")
    wire["client"] = _client(model="H2C")
    departed = await _bind(db_session, printer.id, 0, 0, weight_used=500.0)
    # Right nozzle pairs slots 0+1 (0b0011); left nozzle pairs 2+3 (0b1100). Both masks
    # stay inside AMS unit 0, the only unit whose bit index is measured.
    groups = [[3], [12]]

    _stable_feeder(printer.id, 0, fake_clock, device_bak=groups)
    assert _sample(printer.id, 1, device_bak=groups) == []
    fake_clock["t"] += spool_respool._SWAP_CONFIRM_S + 1
    stamped = await _drive_swap(own_session_factory, printer.id, 1, device_bak=groups)

    assert stamped is not None and stamped.id == departed.id


def test_a_group_bit_outside_ams_unit_0_is_logged_once(caplog):
    """The open question — does bit N index a global tray id or a per-unit slot? — can
    only be answered by a group that reaches past slot 3, and every multi-AMS printer
    sampled 2026-08-25 reported no groups at all. So the FIRST one to arrive gets a line
    carrying the raw masks beside the live tray identities, exactly once per printer:
    a sample this lane never emits leaves the question open forever, and one it emits at
    ~1 Hz is a log nobody reads."""
    printer_id = 4242
    with caplog.at_level(logging.INFO, logger="backend.app.services.spool_respool"):
        _sample(printer_id, 0, filam_bak=[0x30])  # bits 4+5 — past AMS unit 0
        _sample(printer_id, 0, filam_bak=[0x30])

    lines = [r.getMessage() for r in caplog.records if "filam_bak encoding sample" in r.getMessage()]
    assert len(lines) == 1
    assert "masks=[48]" in lines[0]
    assert "groups=[[4, 5]]" in lines[0]
    assert "0/0=GFG02:00FF00FF:11" in lines[0], "the tray identities the two readings disagree about"


def test_a_group_inside_ams_unit_0_is_not_logged(caplog):
    """The measured case is not a question, and must not narrate itself every push."""
    with caplog.at_level(logging.INFO, logger="backend.app.services.spool_respool"):
        _sample(4243, 0, filam_bak=[15])
    assert not [r for r in caplog.records if "filam_bak encoding sample" in r.getMessage()]


@pytest.mark.asyncio
async def test_a_pair_outside_ams_unit_0_declines_rather_than_guessing_the_encoding(
    db_session, printer_factory, wire, fake_clock, caplog, own_session_factory
):
    """Bit N is the SLOT for AMS unit 0 and unmeasured beyond it — every multi-AMS
    printer sampled 2026-08-25 reported no groups at all, so nothing discriminates a
    global tray id from a per-unit slot. Trays 8 -> 9 are AMS unit 2, where a match would
    assert that encoding. Decline, and say so."""
    printer = await printer_factory(model="H2C")
    wire["client"] = _client(model="H2C")
    departed = await _bind(db_session, printer.id, 2, 0, weight_used=500.0)

    _stable_feeder(printer.id, 8, fake_clock, filam_bak=[15])
    assert _sample(printer.id, 9, filam_bak=[15]) == []
    fake_clock["t"] += spool_respool._SWAP_CONFIRM_S + 1
    with caplog.at_level(logging.WARNING, logger="backend.app.services.spool_respool"):
        stamped = await _drive_swap(own_session_factory, printer.id, 9, filam_bak=[15])

    assert stamped is None
    await db_session.refresh(departed)
    assert departed.spent_at is None
    assert "backup-swap corroboration declined" in caplog.text
    assert "confirmed only for AMS unit 0" in caplog.text


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

    _stable_feeder(printer.id, 0, fake_clock, filam_bak=[3])
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


async def _no_brand_to_respool_with(db) -> None:
    """Force Tier 2's ESCALATION arm, which is the only one that still prompts.

    Since 2026-08-19 (operator ruling 3) a spent + loaded tag arrival CONCLUDES — the
    ``respool_auto_enabled`` toggle that used to make it ask is deleted. The prompt did
    not go with it: "I know what happened but cannot carry it out" is an escalation, and
    a re-spool with no brand to mint the fresh row from is exactly that. Both brand
    sources have to go — the ``respool_last_brand`` prefill and the
    ``tagless_default_filament`` fallback it drops to.
    """
    from backend.app.api.routes.settings import set_setting

    await set_setting(db, "respool_last_brand", "")
    await set_setting(db, "tagless_default_filament", "")


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
    await _no_brand_to_respool_with(db_session)
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
    await _no_brand_to_respool_with(db_session)
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


# ===========================================================================
# D5 â€” the ledger-overcharge reconcile (009-H2S spool 290, 2026-08-12)
# ===========================================================================
#
# The incident replayed by every fixture below: a tagless row was re-bound to its slot by
# a ``last_location_reclaim`` after a ~23-minute absence that was really a physical roll
# swap. The old row then absorbed the NEW roll's charges and reached 1200.48 g used on a
# 1000 g label â€” impossible â€” which drove ``remaining_g`` to 0, failed the 150 g start
# floor, and staged the whole production run silently for six hours.
#
# Nothing here second-guesses the lane that produced that binding; what is pinned is the
# reconcile at the moment the ledger becomes PROVABLY impossible: overshoot AND a recorded
# re-bind, exact attribution of the post-boundary charges, one WARNING as the only surface,
# and TAGGED rows never auto-reconciled.
#
# The sentence that used to stand here — "the reclaim itself is doctrine-correct (rule 7
# forbids a duration threshold from deciding tagless identity)" — is superseded by rule 7's
# 2026-08-19 amendment, the same claim (and the same cost) as the module header it echoed.
# The reclaim is now a scoped de-bounce for a SPURIOUS release, and a de-bounce stamps no
# re-bind boundary at all (``slot_pipeline._debounce_bind_moment``, pinned in
# ``test_slot_pipeline``), which leaves an operator's manual re-assign as this lane's
# remaining reachable trigger. The fixtures below therefore describe a boundary that is
# still real, not one the de-bounce would create.

_T0 = datetime(2026, 8, 11, 9, 0, 0)  # the row entered service
_T1 = datetime(2026, 8, 12, 9, 56, 0)  # the reclaim re-bind â€” the boundary
_PRE = (429.2,)  # charges this row genuinely fed, before the boundary
_POST = (406.9, 364.4)  # the successor roll's charges: 771.3 g
_MOVED = 771.3


async def _overcharged(
    db,
    printer_id,
    ams_id=0,
    tray_id=0,
    *,
    pre=_PRE,
    post=_POST,
    created_at=_T0,
    bound_at=_T1,
    label_weight=1000,
    weight_used=None,
    bind=True,
    **kwargs,
):
    """A tagless row whose ledger overshot its label, with per-print history either side
    of a re-bind boundary. Timestamps are explicit because the boundary IS the evidence."""
    fields = {
        "material": "PETG",
        "color_name": "Black",
        "rgba": "000000FF",
        "brand": "Bambu Lab",
        "label_weight": label_weight,
        "core_weight": 250,
        "cost_per_kg": 24.5,
        "category": "Production",
        "slicer_filament": "GFG02",
        "nozzle_temp_min": 240,
        "nozzle_temp_max": 270,
        "data_origin": "ams_auto",
        "created_at": created_at,
        "last_used": _T1,
        "weight_used": sum(pre) + sum(post) if weight_used is None else weight_used,
    }
    fields.update(kwargs)
    spool = Spool(**fields)
    spool.k_profiles = []
    spool.assignments = []
    db.add(spool)
    await db.flush()
    for i, grams in enumerate(pre):
        db.add(
            SpoolUsageHistory(
                spool_id=spool.id,
                printer_id=printer_id,
                print_name=f"pre-{spool.id}-{i}",
                weight_used=grams,
                created_at=created_at + timedelta(hours=1 + i),
            )
        )
    for i, grams in enumerate(post):
        db.add(
            SpoolUsageHistory(
                spool_id=spool.id,
                printer_id=printer_id,
                print_name=f"post-{spool.id}-{i}",
                weight_used=grams,
                created_at=bound_at + timedelta(hours=1 + i),
            )
        )
    if bind:
        db.add(
            SpoolAssignment(
                spool_id=spool.id,
                printer_id=printer_id,
                ams_id=ams_id,
                tray_id=tray_id,
                fingerprint_color="000000FF",
                fingerprint_type="PETG",
                created_at=bound_at,
            )
        )
    await db.commit()
    return spool


async def _slot_spool(db, printer_id, ams_id, tray_id):
    """The spool currently bound to a slot, or None."""
    res = await db.execute(
        select(SpoolAssignment)
        .options(selectinload(SpoolAssignment.spool))
        .where(
            SpoolAssignment.printer_id == printer_id,
            SpoolAssignment.ams_id == ams_id,
            SpoolAssignment.tray_id == tray_id,
        )
    )
    assignment = res.scalar_one_or_none()
    return None if assignment is None else assignment.spool


async def _history_owners(db, spool_id):
    """{print_name: spool_id} for the history rows one fixture row created."""
    res = await db.execute(
        select(SpoolUsageHistory.print_name, SpoolUsageHistory.spool_id).where(
            SpoolUsageHistory.print_name.endswith(f"-{spool_id}-0")
            | SpoolUsageHistory.print_name.endswith(f"-{spool_id}-1")
        )
    )
    return dict(res.all())


@pytest.fixture
def silent_surfaces(monkeypatch):
    """Assert the operator ruling: ONE warning log is the only surface. No notification of
    any kind, and no websocket announcement either."""
    from backend.app.services import notification_service as notification_module, spool_tagless

    notifier = AsyncMock()
    broadcast = AsyncMock()
    monkeypatch.setattr(notification_module, "notification_service", notifier)
    monkeypatch.setattr(spool_tagless.ws_manager, "broadcast", broadcast)
    return {"notifier": notifier, "broadcast": broadcast}


@pytest.mark.asyncio
async def test_reconcile_moves_the_post_boundary_charges_to_a_minted_successor(
    db_session, printer_factory, silent_surfaces, caplog
):
    """THE 009-H2S SPOOL 290 REPRODUCTION. Overshoot plus a recorded re-bind is the proof;
    the attribution is the EXACT sum of the post-boundary charges, booked double-entry (the
    successor gains what the old row loses) so no gram is invented or lost."""
    from backend.app.services import spool_tagless

    printer = await printer_factory(model="H2S")
    old = await _overcharged(db_session, printer.id, 0, 2)
    assert old.weight_used == pytest.approx(1200.5), "the fixture is the impossible ledger"

    with caplog.at_level(logging.WARNING, logger="backend.app.services.spool_tagless"):
        assert await spool_tagless.reconcile_ledger_overcharges(db_session) == 1

    successor = await _slot_spool(db_session, printer.id, 0, 2)
    assert successor is not None and successor.id != old.id, "the slot now holds the new roll's row"
    assert successor.weight_used == pytest.approx(_MOVED)
    assert successor.remaining_g == pytest.approx(1000 - _MOVED), "it clears the 150 g start floor again"
    assert successor.spent_at is None and successor.archived_at is None
    assert successor.tag_uid is None and successor.tray_uuid is None
    assert successor.data_origin == "ams_auto"
    # Identity + pricing ride across: the physical filament did not change, only the roll.
    for field in ("material", "color_name", "rgba", "brand", "label_weight", "core_weight"):
        assert getattr(successor, field) == getattr(old, field), field
    assert successor.cost_per_kg == pytest.approx(24.5)
    assert successor.category == "Production"
    assert successor.slicer_filament == "GFG02"
    assert (successor.nozzle_temp_min, successor.nozzle_temp_max) == (240, 270)
    assert successor.last_used == old.last_used, "the charges that moved are prints this roll fed"
    assert successor.loaded_at is not None, "a binding change to a different row re-stamps the FIFO ordinal"

    await db_session.refresh(old)
    assert old.weight_used == pytest.approx(sum(_PRE)), "the old row keeps exactly what it genuinely fed"
    assert old.archived_at is not None, "it left service at the boundary"
    assert old.spent_at is None, "runout evidence is the exhaustion truth â€” this lane never stamps spent"

    owners = await _history_owners(db_session, old.id)
    assert owners[f"pre-{old.id}-0"] == old.id
    assert owners[f"post-{old.id}-0"] == successor.id
    assert owners[f"post-{old.id}-1"] == successor.id

    assert "LEDGER-OVERCHARGE RECONCILED" in caplog.text
    assert f"moved 771g (2 charges) to successor spool {successor.id}" in caplog.text
    assert silent_surfaces["notifier"].method_calls == [], "no notification of any kind (operator ruling)"
    silent_surfaces["broadcast"].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pre", "post"),
    [
        pytest.param((1100.0,), (), id="c1_a_fresh_roll_delivers_1100g_on_a_1000g_label"),
        pytest.param(_PRE, _POST, id="the_incident_ledger_without_its_boundary"),
    ],
)
async def test_overshoot_without_a_rebind_is_left_completely_alone(db_session, printer_factory, caplog, pre, post):
    """§4.1 row C1 — a fresh roll delivering more than its label is **silent**.

    Manufacturers overfill â€” some ship ~1100 g on a 1000 g label. A row bound
    CONTINUOUSLY since it entered service therefore proves nothing by overshooting, and the
    operator ruled that even a WARNING there is noise: rule 8 governs it until hardware
    runout stamps it spent.

    Two shapes, one verdict: C1's literal vendor overfill, and the 009-H2S incident ledger
    with its re-bind boundary removed. The second is the discriminator — the same
    impossible number that IS reconciled two tests up is left alone here — so what the
    lane acts on is provably the BOUNDARY and never the overshoot."""
    from backend.app.services import spool_tagless

    printer = await printer_factory(model="H2S")
    # The bind is the ORIGINAL one (mint â†’ bind, seconds apart) — no boundary to prove.
    old = await _overcharged(db_session, printer.id, 0, 1, pre=pre, post=post, bound_at=_T0 + timedelta(seconds=3))
    delivered = sum(pre) + sum(post)
    assert delivered > 1000 + 20, "the fixture must clear the overcharge margin, or it proves nothing"

    with caplog.at_level(logging.WARNING, logger="backend.app.services.spool_tagless"):
        assert await spool_tagless.reconcile_ledger_overcharges(db_session) == 0

    await db_session.refresh(old)
    assert old.archived_at is None and old.weight_used == pytest.approx(delivered)
    assert (await _slot_spool(db_session, printer.id, 0, 1)).id == old.id
    assert caplog.text == "", "no warning: a continuously-bound overshoot is not news"


@pytest.mark.asyncio
async def test_c5_an_operator_assigning_an_old_row_over_a_new_roll_is_reconciled(
    db_session, printer_factory, silent_surfaces, caplog
):
    """§4.1 row C5 — the ONE remaining reachable trigger for this lane, driven the way an
    operator reaches it.

    Since the de-bounce carries the incumbent's bind moment forward
    (``test_slot_pipeline.py::test_a_debounce_stamps_no_swap_boundary_for_the_overcharge_reconciler``),
    the farm's own lanes no longer manufacture a re-bind boundary. What still does is a
    human: `POST /assignments` puts a part-used row from the shelf onto a slot that is
    physically holding a different, newer roll. The next prints then charge the NEW roll's
    grams to the OLD row, and the ledger becomes provably impossible.

    So the boundary here is written by the REAL binding writer at ``OPERATOR_ORIGIN`` —
    the same call the route makes — rather than stated as a fixture timestamp. That is the
    difference between this case and the generic sweep tests above: it proves the trigger
    is REACHABLE through a production path, not merely that the sweep works when handed a
    boundary. The displaced incumbent is part of the shape too: the writer sweeps its
    binding, which is exactly why the operator's mistake goes unnoticed until the grams
    give it away.
    """
    from backend.app.services import spool_binding, spool_tagless

    printer = await printer_factory(model="H2S")
    # The slot physically holds a NEW roll, minted at label weight and bound to it.
    incumbent = await _bind(db_session, printer.id, 0, 2, weight_used=0.0, data_origin="ams_auto")
    # The operator's pick from the shelf: an old, part-used, unbound row.
    old = await _overcharged(
        db_session, printer.id, 0, 2, pre=(900.0,), post=(), weight_used=900.0, bind=False, created_at=_T0
    )

    assignment = await spool_binding.bind_spool_to_slot(
        db_session,
        old,
        printer_id=printer.id,
        ams_id=0,
        tray_id=2,
        fingerprint_color="000000FF",
        fingerprint_type="PETG",
        origin=spool_binding.OPERATOR_ORIGIN,
    )
    await db_session.commit()
    assert assignment is not None
    boundary = assignment.created_at
    assert (boundary - _T0).total_seconds() > 120, "the manual assign IS the re-bind boundary"
    assert await _slot_spool(db_session, printer.id, 0, 2) is not None
    assert (await _slot_spool(db_session, printer.id, 0, 2)).id == old.id, "the incumbent was displaced"

    # The next two prints run on the NEW roll and are charged to the OLD row: 900 + 140 g
    # used against a 1000 g label — past the 20 g margin, so no vendor overfill explains it.
    for i, grams in enumerate((80.0, 60.0)):
        db_session.add(
            SpoolUsageHistory(
                spool_id=old.id,
                printer_id=printer.id,
                print_name=f"post-{old.id}-{i}",
                weight_used=grams,
                created_at=boundary + timedelta(minutes=10 * (i + 1)),
            )
        )
    old.weight_used = 1040.0
    await db_session.commit()

    with caplog.at_level(logging.WARNING, logger="backend.app.services.spool_tagless"):
        assert await spool_tagless.reconcile_ledger_overcharges(db_session) == 1

    successor = await _slot_spool(db_session, printer.id, 0, 2)
    assert successor is not None and successor.id not in (old.id, incumbent.id)
    assert successor.weight_used == pytest.approx(140.0), "the successor carries exactly the post-boundary charges"
    assert successor.remaining_g == pytest.approx(860.0), "and clears the start floor again"
    await db_session.refresh(old)
    assert old.weight_used == pytest.approx(900.0), "the old row keeps only what it genuinely fed"
    assert old.archived_at is not None

    owners = await _history_owners(db_session, old.id)
    assert owners[f"post-{old.id}-0"] == successor.id
    assert owners[f"post-{old.id}-1"] == successor.id
    assert "LEDGER-OVERCHARGE RECONCILED" in caplog.text
    assert silent_surfaces["notifier"].method_calls == [], "log-only, as everywhere in this lane"


@pytest.mark.asyncio
async def test_no_post_boundary_charges_means_nothing_to_attribute(db_session, printer_factory, caplog):
    """With a boundary but no charges recorded after it there is no exact attribution to
    make â€” and an inexact one is exactly what this lane refuses to do."""
    from backend.app.services import spool_tagless

    printer = await printer_factory(model="H2S")
    old = await _overcharged(db_session, printer.id, 0, 0, pre=(1200.5,), post=())

    with caplog.at_level(logging.WARNING, logger="backend.app.services.spool_tagless"):
        assert await spool_tagless.reconcile_ledger_overcharges(db_session) == 0

    await db_session.refresh(old)
    assert old.archived_at is None and old.weight_used == pytest.approx(1200.5)
    assert (await _slot_spool(db_session, printer.id, 0, 0)).id == old.id
    assert caplog.text == ""


@pytest.mark.asyncio
async def test_tagged_row_is_warned_once_and_never_reconciled(db_session, printer_factory, silent_surfaces, caplog):
    """§4.1 row G5 — a TAGGED roll over-spending its label is **WARN only**.

    A tagged row's identity is RFID-bound, and by rule 10 no tag has ever been reused on
    this farm â€” so an impossible ledger there is MISATTRIBUTION evidence to root-cause, not
    a roll change to book. Warn once per re-notify window, mutate nothing, ever.

    Everything the tagless arm does — successor mint, charge re-pointing, archival — is
    asserted here NOT to happen, which is G5's whole content: the reconciler's reach stops
    where factual identity begins."""
    from backend.app.services import spool_tagless

    printer = await printer_factory(model="H2S")
    old = await _overcharged(db_session, printer.id, 1, 3, tag_uid=TAG_UID, tray_uuid=TRAY_UUID)

    with caplog.at_level(logging.WARNING, logger="backend.app.services.spool_tagless"):
        assert await spool_tagless.reconcile_ledger_overcharges(db_session) == 0
    assert "LEDGER-OVERCHARGE (TAGGED, NOT RECONCILED)" in caplog.text
    assert "RECONCILED:" not in caplog.text

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="backend.app.services.spool_tagless"):
        assert await spool_tagless.reconcile_ledger_overcharges(db_session) == 0
    assert caplog.text == "", "the durable ledger paces a STANDING complaint â€” one warning per window"

    await db_session.refresh(old)
    assert old.archived_at is None and old.weight_used == pytest.approx(1200.5)
    assert (await _slot_spool(db_session, printer.id, 1, 3)).id == old.id, "no successor was minted"
    assert silent_surfaces["notifier"].method_calls == [], "log-only: never a notification"


@pytest.mark.asyncio
async def test_a_sibling_chip_alone_still_counts_as_tagged(db_session, printer_factory, caplog):
    """A Bambu roll carries two chips and either identifies it (invariant 1). A row whose
    only recorded chip is the far one must take the tagged branch, or the auto-reconcile
    would replace a tag-identified row on assumption-tier evidence."""
    from backend.app.services import spool_tagless

    printer = await printer_factory(model="H2S")
    old = await _overcharged(db_session, printer.id, 0, 3, sibling_tag_uid=SIBLING_TAG_UID)

    with caplog.at_level(logging.WARNING, logger="backend.app.services.spool_tagless"):
        assert await spool_tagless.reconcile_ledger_overcharges(db_session) == 0

    assert "TAGGED, NOT RECONCILED" in caplog.text
    await db_session.refresh(old)
    assert old.archived_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("over", "expected", "why"),
    [
        (19.0, 0, "inside the overfill + attribution-quantum margin: not provably impossible"),
        (21.0, 1, "past the margin with a recorded re-bind: the two-fact proof is complete"),
    ],
)
async def test_the_overcharge_margin_is_the_dividing_line(db_session, printer_factory, over, expected, why):
    from backend.app.services import spool_tagless

    printer = await printer_factory(model="H2S")
    old = await _overcharged(db_session, printer.id, 0, 0, pre=(600.0,), post=(400.0 + over,))

    assert await spool_tagless.reconcile_ledger_overcharges(db_session) == expected, why
    await db_session.refresh(old)
    assert (old.archived_at is not None) is bool(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"spent_at": _T1}, "a spent row's exhaustion is already hardware-established (rule 8)"),
        ({"archived_at": _T1}, "an archived row is out of service â€” nothing to protect"),
        ({"bind": False}, "with no live assignment there is no boundary and no slot to re-bind"),
        ({"label_weight": 0}, "a zero label cannot be overshot"),
    ],
)
async def test_rows_outside_the_candidate_set_are_never_touched(db_session, printer_factory, kwargs, why):
    from backend.app.services import spool_tagless

    printer = await printer_factory(model="H2S")
    old = await _overcharged(db_session, printer.id, 0, 0, **kwargs)
    archived_before = old.archived_at

    assert await spool_tagless.reconcile_ledger_overcharges(db_session) == 0, why
    await db_session.refresh(old)
    assert old.archived_at == archived_before
    assert old.weight_used == pytest.approx(1200.5)


@pytest.mark.asyncio
async def test_reconcile_is_idempotent(db_session, printer_factory):
    """The successor is minted and bound in the same instant, so it carries no re-bind
    boundary of its own â€” a second pass finds nothing to prove and writes nothing."""
    from backend.app.services import spool_tagless

    printer = await printer_factory(model="H2S")
    await _overcharged(db_session, printer.id, 0, 2)

    assert await spool_tagless.reconcile_ledger_overcharges(db_session) == 1
    successor = await _slot_spool(db_session, printer.id, 0, 2)
    assert await spool_tagless.reconcile_ledger_overcharges(db_session) == 0

    still = await _slot_spool(db_session, printer.id, 0, 2)
    assert still.id == successor.id
    assert still.weight_used == pytest.approx(_MOVED)
    assert still.archived_at is None


@pytest.mark.asyncio
async def test_one_bad_row_cannot_abort_the_sweep(db_session, printer_factory, monkeypatch):
    """Invariant 10: an entry hook owns its guard. The first candidate explodes mid-write;
    the second must still be reconciled and the caller must never see the exception."""
    from backend.app.services import spool_tagless

    printer = await printer_factory(model="H2S")
    bad = await _overcharged(db_session, printer.id, 0, 0)
    good = await _overcharged(db_session, printer.id, 0, 1)

    real_mint = spool_tagless._mint_successor_row
    calls = {"n": 0}
    # Bare id, captured up front: the sweep's per-candidate rollback expires every object
    # in the session, so holding the ORM row here would make the closure itself lazy-load.
    bad_id = bad.id

    async def _explode_once(db, departed, *, weight_used):
        calls["n"] += 1
        if departed.id == bad_id:
            raise RuntimeError("history re-point exploded")
        return await real_mint(db, departed, weight_used=weight_used)

    monkeypatch.setattr(spool_tagless, "_mint_successor_row", _explode_once)

    assert await spool_tagless.reconcile_ledger_overcharges(db_session) == 1
    assert calls["n"] == 2, "the sweep continued past the bad row"
    await db_session.refresh(bad)
    await db_session.refresh(good)
    assert bad.archived_at is None, "the failed candidate rolled back cleanly"
    assert good.archived_at is not None


@pytest.mark.asyncio
async def test_spoolman_installs_are_skipped(db_session, printer_factory):
    """Spoolman owns the spool lifecycle there, so rewriting its ledger is not ours to do.
    Driven through the REAL entry, which is where that gate lives."""
    from backend.app.api.routes.settings import set_setting

    printer = await printer_factory(model="H2S")
    old = await _overcharged(db_session, printer.id, 0, 0)
    await set_setting(db_session, "spoolman_enabled", "true")

    assert await detect_spent_contradictions(db_session, MagicMock()) == 0
    await db_session.refresh(old)
    assert old.archived_at is None and old.weight_used == pytest.approx(1200.5)


@pytest.mark.asyncio
async def test_the_reconcile_rides_the_detector_entry_and_its_throttle(db_session, printer_factory):
    """LIVENESS PIN + throttle. The sweep is reachable through the REAL entry hook (a lane
    that is never CALLED is indistinguishable from one that finds nothing), and it shares
    that entry's 15-minute floor rather than bringing a second one."""
    printer = await printer_factory(model="H2S")
    first = await _overcharged(db_session, printer.id, 0, 0)
    manager = MagicMock()
    manager.get_status = lambda _pid: None

    await detect_spent_contradictions(db_session, manager, now=500.0)
    await db_session.refresh(first)
    assert first.archived_at is not None, "the reconcile ran from the detector entry"

    second = await _overcharged(db_session, printer.id, 0, 1)
    await detect_spent_contradictions(db_session, manager, now=600.0)
    await db_session.refresh(second)
    assert second.archived_at is None, "inside the shared floor: the sweep did not run"

    await detect_spent_contradictions(
        db_session, manager, now=500.0 + spool_respool._SPENT_CONTRADICTION_MIN_INTERVAL_S
    )
    await db_session.refresh(second)
    assert second.archived_at is not None, "past the floor it runs again"


# ===========================================================================
# D6 — the BACKWARD direction: an early runout hands the charges back (C3/C4)
# ===========================================================================
#
# The mirror of D5, and the price of doctrine rule 7's 2026-08-19 amendment. Scoping the
# breadcrumb reclaim to a 5-minute glitch filter made the farm MINT a fresh row at label
# weight for every longer absence — right for a genuine roll change (T2/T3, the 002/005-H2S
# incident), wrong for a roll pulled for an external dry, a jam clear or an inspection and
# returned later (T5), and wrong for a restart that lands while the roll is out (T11).
# Those write an ASSUMED full roll over a part-used one and so OVER-PROMISE the 150 g start
# floor, which starts prints that die instead of staging them.
#
# The runout is the only moment the hardware states that roll's real capacity, so it is
# where the mistake becomes recoverable: a row that ran dry having delivered far less than
# its assumed label either was a part-used roll seated as full (C2 — ordinary stock,
# operator ruling 4) or is the farm's own mistaken mint, in which case its delivered grams
# ARE the remainder the row it displaced still had on the books.
#
# Rulings 4 and 5 genuinely conflict in the band where a DIFFERENT part-used roll happens
# to deliver roughly what the departed row had left, and every test below is written so
# that band resolves to ruling 4. The stand-down arm is the feature; the acting arm is the
# optimisation.

_R0 = datetime(2026, 8, 17, 8, 0, 0)  # the departed roll left this slot (pulled for a dry)
_M0 = datetime(2026, 8, 17, 8, 40, 0)  # 40 min later — outside the window — its return MINTED
_R1 = datetime(2026, 8, 18, 12, 0, 0)  # the mint's own bay-clear, ~3 min before its runout


async def _released(db, printer_id, ams_id, tray_id, *, at, **kwargs):
    """An UNBOUND row whose last recorded location is this slot.

    Exactly the residue a release leaves behind (``spool_binding._stamp_last_location``),
    which is the only thing still naming the victim once the AMS has cleared the bay —
    ~3 minutes before it declares the runout (incident shape 31).
    """
    fields = {
        "material": "PETG",
        "color_name": "Black",
        "rgba": "000000FF",
        "brand": "Bambu Lab",
        "label_weight": 1000,
        "core_weight": 250,
        "weight_used": 0.0,
        "data_origin": "ams_auto",
        "created_at": _R0,
        "last_location_printer_id": printer_id,
        "last_location_ams_id": ams_id,
        "last_location_tray_id": tray_id,
        "last_location_at": at,
    }
    fields.update(kwargs)
    spool = Spool(**fields)
    spool.k_profiles = []
    spool.assignments = []
    db.add(spool)
    await db.flush()
    await db.commit()
    return spool


async def _mistaken_mint(db, printer_id, ams_id, tray_id, *, delivered, at=_R1, **kwargs):
    """The row rule 7's window minted when the roll came back, one history row per print."""
    spool = await _released(
        db, printer_id, ams_id, tray_id, at=at, created_at=_M0, weight_used=sum(delivered), **kwargs
    )
    for i, grams in enumerate(delivered):
        db.add(
            SpoolUsageHistory(
                spool_id=spool.id,
                printer_id=printer_id,
                print_name=f"mint-{spool.id}-{i}",
                weight_used=grams,
                created_at=_M0 + timedelta(hours=1 + i),
            )
        )
    await db.commit()
    return spool


async def _charges_of(db, spool_id):
    """[(print_name, grams)] currently pointing at a row — the double-entry witness."""
    res = await db.execute(
        select(SpoolUsageHistory.print_name, SpoolUsageHistory.weight_used)
        .where(SpoolUsageHistory.spool_id == spool_id)
        .order_by(SpoolUsageHistory.print_name)
    )
    return [(name, round(float(grams), 1)) for name, grams in res.all()]


async def _spool_rows(db):
    res = await db.execute(select(Spool.id))
    return sorted(r[0] for r in res.all())


async def _runout(db, printer_id, ams_id, tray_id):
    """Drive the one spent writer for a slot, exactly as every trigger lane does."""
    return await spool_respool._mark_tray_spent(db, printer_id, ams_id * 4 + tray_id)


@pytest.mark.asyncio
async def test_an_early_runout_hands_the_charges_back_to_the_row_it_displaced(
    db_session, printer_factory, wire, caplog
):
    """SCENARIO C3, driven through the REAL firmware trigger.

    A 300 g part-roll is pulled for a dry, comes back 40 minutes later and mints a fresh
    row assumed to hold 1000 g. It runs out after 300 g — exactly the remainder the
    departed row still had — so the roll never changed and the mint did. The charges go
    back with their own weights (RE-POINTED, not duplicated and not apportioned), the mint
    is retired, and the departed row carries both the true total and the runout itself.
    """
    printer = await printer_factory(model="H2S")
    departed = await _released(db_session, printer.id, 0, 2, at=_R0, weight_used=700.0)
    mint = await _mistaken_mint(db_session, printer.id, 0, 2, delivered=(180.0, 120.0))
    before = await _spool_rows(db_session)

    with caplog.at_level(logging.INFO, logger="backend.app.services.spool_tagless"):
        stamped = await mark_spent_on_slot_runout(
            db_session,
            printer.id,
            [("00000000" + f"{_AUTO_SWITCH_CODE:08X}", _attr(0, 2), _AUTO_SWITCH_CODE)],
            wire["state"],
        )

    assert [s.id for s in stamped] == [mint.id], "tier 2 names the row that just left the slot"

    await db_session.refresh(departed)
    await db_session.refresh(mint)
    assert departed.weight_used == pytest.approx(1000.0), "700 g it genuinely fed + the mint's 300 g"
    assert departed.spent_at == mint.spent_at, "ONE runout, re-attributed — never re-stamped off a fresh clock"
    assert departed.archived_at is None, "the surviving row stays in inventory"
    assert departed.remaining_g == pytest.approx(0.0), "spent ⇒ zero is derived, rule 8"
    assert mint.archived_at is not None, "the mistaken mint is retired"
    assert mint.weight_used == pytest.approx(0.0), "it kept nothing it never fed"

    assert await _charges_of(db_session, mint.id) == [], "every charge left the mint"
    assert await _charges_of(db_session, departed.id) == [
        (f"mint-{mint.id}-0", 180.0),
        (f"mint-{mint.id}-1", 120.0),
    ], "RE-POINTED with their own weights: double entry, not duplicated and not apportioned"
    assert await _spool_rows(db_session) == before, "the backward direction mints nothing"

    assert "EARLY-RUNOUT RE-ATTRIBUTED" in caplog.text
    assert "300 g went back to it" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("over", "acts", "why"),
    [
        (21.0, False, "21 g over the remainder: a DIFFERENT part-used roll, ruling 4 wins the band"),
        (-21.0, False, "21 g under: the same band on the other side — stand-down is the default"),
        (19.0, True, "inside the fit margin: one roll's worth of filament left this slot in total"),
        (-19.0, True, "inside the fit margin on the low side"),
    ],
)
async def test_the_fit_margin_is_the_dividing_line_and_the_band_belongs_to_ruling_4(
    db_session, printer_factory, caplog, over, acts, why
):
    """SCENARIO C4 (and its two acting neighbours), pinned at both band edges.

    The two failing cases are the hard ones on purpose: a genuinely different part-used
    roll that happens to deliver almost exactly what the departed row had left is
    INDISTINGUISHABLE in the data from the same roll coming back. A generous margin would
    resolve that toward ruling 5 and silently merge two physical rolls into one ledger row,
    permanently. So the margin is tight and the band is treated as ruling 4's case.
    """
    printer = await printer_factory(model="H2S")
    departed = await _released(db_session, printer.id, 0, 1, at=_R0, weight_used=700.0)
    delivered = 300.0 + over
    mint = await _mistaken_mint(db_session, printer.id, 0, 1, delivered=(delivered,))

    with caplog.at_level(logging.INFO, logger="backend.app.services.spool_tagless"):
        assert (await _runout(db_session, printer.id, 0, 1)).id == mint.id

    await db_session.refresh(departed)
    await db_session.refresh(mint)
    if acts:
        assert departed.weight_used == pytest.approx(700.0 + delivered), why
        assert departed.spent_at is not None and mint.archived_at is not None
        assert "EARLY-RUNOUT RE-ATTRIBUTED" in caplog.text
        return

    assert departed.weight_used == pytest.approx(700.0), why
    assert departed.spent_at is None, "the departed row is not declared exhausted on a guess"
    assert departed.archived_at is None
    assert mint.archived_at is None, "the mint stands as the roll it recorded"
    assert mint.weight_used == pytest.approx(delivered), "its grams stand as delivered (ruling 4)"
    assert await _charges_of(db_session, mint.id) == [(f"mint-{mint.id}-0", round(delivered, 1))]
    assert "EARLY-RUNOUT NOT RE-ATTRIBUTED" in caplog.text
    assert "operator ruling 4" in caplog.text


@pytest.mark.asyncio
async def test_a_tagged_successor_is_never_touched(db_session, printer_factory, caplog):
    """Rule 10: no tag has ever been reused on this farm, so a tagged row's identity is
    FACTUAL. An early runout there is misattribution evidence to root-cause by hand, never
    a mistaken mint to unwind — and the grams would fit perfectly if the gate were open."""
    printer = await printer_factory(model="H2S")
    departed = await _released(db_session, printer.id, 0, 3, at=_R0, weight_used=700.0)
    mint = await _mistaken_mint(db_session, printer.id, 0, 3, delivered=(300.0,), tag_uid=TAG_UID, tray_uuid=TRAY_UUID)

    with caplog.at_level(logging.INFO, logger="backend.app.services.spool_tagless"):
        assert (await _runout(db_session, printer.id, 0, 3)).id == mint.id

    await db_session.refresh(departed)
    await db_session.refresh(mint)
    assert departed.weight_used == pytest.approx(700.0) and departed.spent_at is None
    assert mint.archived_at is None and mint.weight_used == pytest.approx(300.0)
    assert "EARLY-RUNOUT" not in caplog.text, "an ordinary tagged runout is not news"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "fragment", "why"),
    [
        ({"tag_uid": TAG_UID, "tray_uuid": TRAY_UUID}, "tag-identified", "rule 10, the departed side"),
        ({"sibling_tag_uid": SIBLING_TAG_UID}, "tag-identified", "either chip identifies a roll (invariant 1)"),
        ({"archived_at": _T1}, "archived", "retired inventory is never resurrected"),
        ({"spent_at": _T1}, "ran out itself", "a row that already ran dry had no remainder to hand over"),
        ({"label_weight": 0}, "no label to price", "an unpriceable row cannot state a remainder"),
    ],
)
async def test_the_single_candidate_is_adjudicated_wherever_it_leads(
    db_session, printer_factory, caplog, kwargs, fragment, why
):
    """Rule 7's amendment bounds this lane to the slot's SINGLE last occupant. When that
    one row cannot be adjudicated the answer is "nothing happens", never "look further" —
    the grams fit perfectly in every case below and are still refused."""
    printer = await printer_factory(model="H2S")
    departed = await _released(db_session, printer.id, 1, 0, at=_R0, weight_used=700.0, **kwargs)
    mint = await _mistaken_mint(db_session, printer.id, 1, 0, delivered=(300.0,))

    with caplog.at_level(logging.INFO, logger="backend.app.services.spool_tagless"):
        assert (await _runout(db_session, printer.id, 1, 0)).id == mint.id

    await db_session.refresh(departed)
    await db_session.refresh(mint)
    assert departed.weight_used == pytest.approx(700.0), why
    assert mint.archived_at is None and mint.weight_used == pytest.approx(300.0)
    assert "EARLY-RUNOUT NOT RE-ATTRIBUTED" in caplog.text
    assert fragment in caplog.text


@pytest.mark.asyncio
async def test_the_lane_never_scans_past_its_one_candidate_for_a_row_that_fits(db_session, printer_factory, caplog):
    """THE discipline that separates an adjudication from a search through noise.

    An OLDER residue of the same slot fits the delivered grams exactly; the slot's actual
    last occupant does not. Scanning would find the flattering answer and permanently merge
    two rolls that never met. ``_mark_tray_spent`` tier 2 refuses the same walk for the same
    reason: a missed correction self-heals, a false one does not.
    """
    printer = await printer_factory(model="H2S")
    flattering = await _released(db_session, printer.id, 0, 0, at=_R0 - timedelta(days=2), weight_used=700.0)
    last_occupant = await _released(db_session, printer.id, 0, 0, at=_R0, weight_used=100.0)
    mint = await _mistaken_mint(db_session, printer.id, 0, 0, delivered=(300.0,))

    with caplog.at_level(logging.INFO, logger="backend.app.services.spool_tagless"):
        assert (await _runout(db_session, printer.id, 0, 0)).id == mint.id

    for row in (flattering, last_occupant, mint):
        await db_session.refresh(row)
    assert flattering.weight_used == pytest.approx(700.0), "the older residue was never even considered"
    assert flattering.spent_at is None
    assert last_occupant.weight_used == pytest.approx(100.0), "the one candidate simply did not fit"
    assert mint.archived_at is None
    assert f"spool {last_occupant.id}'s 900 g remainder" in caplog.text


@pytest.mark.asyncio
async def test_a_spent_row_bound_elsewhere_stands_the_lane_down(db_session, printer_factory, caplog):
    """The same invariant-11 dependency, through the hole the SQL filter used to plug.

    This lane may adjudicate two rows at all ONLY because both are DEAD by the time a spent
    stamp exists — the bay clears ~3 minutes before the runout is declared, so the assignment
    is already gone. That was enforced by ``~Spool.assignments.any()`` living inside the
    shared residue query, i.e. by a filter this lane did not write and could not see.

    The filter is gone (in SQL it did not skip an ineligible row, it made the row invisible
    and silently substituted an OLDER occupant of the same slot), so the requirement is now
    stated HERE. Pinned with the spent row itself re-bound in another tray: the exemption
    has evaporated, and the lane must write nothing rather than re-point the charges of a
    roll the wire says is loaded somewhere else.
    """
    printer = await printer_factory(model="H2S")
    departed = await _released(db_session, printer.id, 0, 2, at=_R0, weight_used=700.0)
    mint = await _mistaken_mint(db_session, printer.id, 0, 2, delivered=(180.0, 120.0))
    mint.spent_at = datetime(2026, 8, 18, 12, 3, 0)  # the runout that would normally trigger it
    db_session.add(SpoolAssignment(spool_id=mint.id, printer_id=printer.id, ams_id=0, tray_id=3))
    await db_session.commit()
    before = await _charges_of(db_session, mint.id)

    with caplog.at_level(logging.INFO, logger="backend.app.services.spool_tagless"):
        handed_back = await reattribute_early_runout(db_session, mint, printer_id=printer.id, ams_id=0, tray_id=2)

    assert handed_back is None
    for row in (departed, mint):
        await db_session.refresh(row)
    assert await _charges_of(db_session, mint.id) == before, "its charges stay exactly where they are"
    assert mint.archived_at is None, "a row the wire seats in a tray is never retired by an assumption"
    assert departed.weight_used == pytest.approx(700.0)
    assert departed.spent_at is None
    assert "not this slot's most recent UNBOUND departure" in caplog.text
    assert "invariant 11" in caplog.text


@pytest.mark.asyncio
async def test_a_live_binding_stands_the_lane_down(db_session, printer_factory, caplog):
    """CROSS-CUTTING INVARIANT 11, pinned as the dependency this lane rests on.

    An arithmetic fit is assumption-tier evidence, which may displace NOTHING a live
    binding holds. This lane escapes that only because the AMS clears a drained slot's
    exist bit ~3 minutes BEFORE declaring the runout, so by spent-stamp time the assignment
    is already gone and both rows are dead. Here the stamp lands on a STILL-BOUND row —
    tier 1 — and the lane must find no candidate and write nothing. If a future change ever
    makes spent stamping fire while the binding is live, this test is the one that says the
    exemption has evaporated with it.

    The slot deliberately carries TWO earlier departures, the OLDER of which fits the bound
    row's delivered grams exactly. That is what makes this a safety test rather than a
    message test: drop the "the spent row must be this slot's own most recent departure"
    guard and the lane does not merely explain itself badly, it charges 300 g onto a
    healthy shelf roll that was never in this slot at the time.
    """
    printer = await printer_factory(model="H2S")
    fitting_older = await _released(db_session, printer.id, 0, 1, at=_R0 - timedelta(days=2), weight_used=700.0)
    # Departed long enough ago that _bound_after_the_bay_cleared keeps tier 1.
    departed = await _released(db_session, printer.id, 0, 1, at=_R0, weight_used=100.0)
    bound = await _bind(db_session, printer.id, 0, 1, weight_used=300.0, data_origin="ams_auto")

    with caplog.at_level(logging.INFO, logger="backend.app.services.spool_tagless"):
        assert (await _runout(db_session, printer.id, 0, 1)).id == bound.id

    for row in (fitting_older, departed, bound):
        await db_session.refresh(row)
    assert bound.spent_at is not None, "LIVENESS: the stamp itself is untouched by this lane"
    assert bound.archived_at is None, "a bound row is never retired by an assumption"
    assert bound.weight_used == pytest.approx(300.0), "its charges stay on it"
    assert fitting_older.weight_used == pytest.approx(700.0), "the flattering fit is never reached"
    assert fitting_older.spent_at is None
    assert departed.weight_used == pytest.approx(100.0) and departed.spent_at is None
    assert "not this slot's most recent UNBOUND departure" in caplog.text
    assert "invariant 11" in caplog.text


@pytest.mark.asyncio
async def test_the_two_re_attribution_directions_cannot_ping_pong(db_session, printer_factory, caplog):
    """INCIDENT SHAPE 26, asserted rather than argued.

    Two lanes acting on one row set from mirror evidence is the spool-211 oscillation. The
    fixture is built to TEMPT the forward sweep — the merged row lands at 1019 g on a
    1000 g label, genuinely past it — and three independent guards keep it standing down,
    in the order they actually bite:

    1. the surviving row now carries ``spent_at``, and the sweep's candidate query filters
       ``spent_at IS NULL``. This is the dominant guard and the one that holds even when
       the departed row was already over its own label before the merge;
    2. it holds no live assignment, which the sweep JOINs on — this direction never
       re-binds, which is the layering reason it cannot feed the other one;
    3. arithmetically, a set of grams that FITS cannot overshoot the label by more than the
       same margin, because the fit gap and the overshoot are the same quantity.

    The second call re-seats the row by hand to remove guard 2 and pin that the answer is
    still "no action" — asserted, not argued.
    """
    from backend.app.services import spool_tagless

    printer = await printer_factory(model="H2S")
    departed = await _released(db_session, printer.id, 0, 2, at=_R0, weight_used=800.0)
    mint = await _mistaken_mint(db_session, printer.id, 0, 2, delivered=(219.0,))

    assert (await _runout(db_session, printer.id, 0, 2)).id == mint.id
    await db_session.refresh(departed)
    assert departed.weight_used == pytest.approx(1019.0), "past the label, and correctly so"

    caplog.clear()  # the correction above logs its own line; only the SWEEP is on trial here
    with caplog.at_level(logging.WARNING, logger="backend.app.services.spool_tagless"):
        assert await spool_tagless.reconcile_ledger_overcharges(db_session) == 0
    await db_session.refresh(departed)
    assert departed.weight_used == pytest.approx(1019.0), "the forward sweep took no action"
    assert await _charges_of(db_session, departed.id) == [(f"mint-{mint.id}-0", 219.0)]
    assert "LEDGER-OVERCHARGE" not in caplog.text, "not even a warning: there is nothing here to reconcile"

    # And with the row re-seated by hand — the one state the forward sweep needs.
    db_session.add(SpoolAssignment(spool_id=departed.id, printer_id=printer.id, ams_id=0, tray_id=2))
    await db_session.commit()
    assert await spool_tagless.reconcile_ledger_overcharges(db_session) == 0
    await db_session.refresh(departed)
    assert departed.weight_used == pytest.approx(1019.0), "a fitting merge can never clear the margin"


@pytest.mark.asyncio
async def test_a_duplicate_runout_trigger_changes_nothing_further(db_session, printer_factory):
    """The retired mint is ARCHIVED, not deleted, so it stays the row this slot's next
    runout trigger resolves onto — which is what makes the whole lane idempotent. A second
    trigger must not charge the departed row twice."""
    printer = await printer_factory(model="H2S")
    departed = await _released(db_session, printer.id, 0, 3, at=_R0, weight_used=700.0)
    mint = await _mistaken_mint(db_session, printer.id, 0, 3, delivered=(300.0,))

    assert (await _runout(db_session, printer.id, 0, 3)).id == mint.id
    again = await _runout(db_session, printer.id, 0, 3)

    assert again is not None and again.id == mint.id, "the tombstone answers the duplicate"
    await db_session.refresh(departed)
    assert departed.weight_used == pytest.approx(1000.0), "charged exactly once"
    assert await _charges_of(db_session, departed.id) == [(f"mint-{mint.id}-0", 300.0)]


@pytest.mark.asyncio
async def test_an_ordinary_runout_still_stamps_and_this_lane_stays_out_of_it(db_session, printer_factory, caplog):
    """LIVENESS PAIR, half one. A suppression that is too eager is indistinguishable from a
    lane that never fires, so the positive path is re-proven: a full roll running out
    normally still gets its stamp, and the correction does not so much as log."""
    printer = await printer_factory(model="H2S")
    departed = await _released(db_session, printer.id, 1, 1, at=_R0, weight_used=700.0)
    normal = await _mistaken_mint(db_session, printer.id, 1, 1, delivered=(600.0, 350.0))

    with caplog.at_level(logging.INFO, logger="backend.app.services.spool_tagless"):
        stamped = await _runout(db_session, printer.id, 1, 1)

    assert stamped is not None and stamped.id == normal.id
    await db_session.refresh(normal)
    await db_session.refresh(departed)
    assert normal.spent_at is not None and normal.archived_at is None
    assert normal.delivered_g == pytest.approx(950.0), "it delivered about a full roll — no mistake to undo"
    assert departed.weight_used == pytest.approx(700.0) and departed.spent_at is None
    assert "EARLY-RUNOUT" not in caplog.text, "every ordinary runout would be noise"


@pytest.mark.asyncio
async def test_the_forward_overcharge_reconcile_still_fires_on_its_own_trigger(db_session, printer_factory):
    """LIVENESS PAIR, half two. The backward direction shares ``_hand_charges_back`` and the
    ``last_released_from_slot_stmt`` origin with its neighbours; this re-proves the forward
    sweep still reconciles its own genuine trigger after that surgery."""
    from backend.app.services import spool_tagless

    printer = await printer_factory(model="H2S")
    old = await _overcharged(db_session, printer.id, 0, 0)

    assert await spool_tagless.reconcile_ledger_overcharges(db_session) == 1
    await db_session.refresh(old)
    assert old.archived_at is not None and old.weight_used == pytest.approx(sum(_PRE))


@pytest.mark.asyncio
async def test_the_acknowledgement_undo_still_hands_charges_back_and_re_binds(
    db_session, printer_factory, silent_surfaces
):
    """LIVENESS PAIR, half three — the OTHER caller of the extracted double entry.

    Rule 12's undo (scenario R8) and the early-runout correction are one concept from two
    evidences, so they share ``_hand_charges_back``. What is only true of the undo is
    checked here: the predecessor goes back INTO the slot and the charges follow it.
    """
    from backend.app.services import spool_tagless

    printer = await printer_factory(model="H2S")
    predecessor = await _released(db_session, printer.id, 0, 0, at=_R0, weight_used=700.0)
    mint = await _mistaken_mint(db_session, printer.id, 0, 0, delivered=(120.0,))

    moved = await spool_tagless.replace_bound_row_with_predecessor(
        db_session,
        mint,
        predecessor,
        _M0,
        printer_id=printer.id,
        ams_id=0,
        tray_id=0,
        fingerprint_color="000000FF",
        fingerprint_type="PETG",
        origin="recheck_undo",
    )
    await db_session.commit()

    assert moved == pytest.approx(120.0)
    await db_session.refresh(predecessor)
    assert predecessor.weight_used == pytest.approx(820.0)
    assert (await _slot_spool(db_session, printer.id, 0, 0)).id == predecessor.id
    assert await _charges_of(db_session, predecessor.id) == [(f"mint-{mint.id}-0", 120.0)]
