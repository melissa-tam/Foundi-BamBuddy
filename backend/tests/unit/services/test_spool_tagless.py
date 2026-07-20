"""Tests for the tagless (non-RFID) spool lifecycle — services.spool_tagless.

Covers minting from both sources, Hook B slot policy (mint / sticky-rebind /
spent-replace / different-filament / tagged-passthrough), the bare-tray
auto-config (D3b) with its retry dedup, the stale-config firmware-leftover
override, and provisional disposal on RFID takeover.
"""

import json
import logging
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.services import spool_tagless

_VALID_TAG = "AABBCCDD11223344"


@pytest.fixture(autouse=True)
def _clean_state():
    spool_tagless._reset_state()
    yield
    spool_tagless._reset_state()


@pytest.fixture
def env(monkeypatch):
    """Patch settings, the MQTT config push, WS broadcast, and printer_manager.

    ``settings`` is a mutable dict backing a fake ``get_setting`` — leave a key
    unset to exercise the code's default (auto_add on, schema default filament).
    """
    settings: dict[str, str] = {}

    async def fake_get_setting(db, key):
        return settings.get(key)

    monkeypatch.setattr("backend.app.api.routes.settings.get_setting", fake_get_setting)

    apply = AsyncMock(return_value=True)
    monkeypatch.setattr("backend.app.api.routes.inventory.apply_spool_to_slot_via_mqtt", apply)

    ws = AsyncMock()
    monkeypatch.setattr(spool_tagless.ws_manager, "broadcast", ws)

    # No live printer state → auto_assign_spool creates the assignment and skips
    # all MQTT (mirrors the spool_tag_matcher unit tests).
    monkeypatch.setattr(spool_tagless.printer_manager, "get_status", lambda pid: None)
    monkeypatch.setattr(spool_tagless.printer_manager, "get_client", lambda pid: None)

    return SimpleNamespace(settings=settings, apply=apply, ws=ws)


def _tray(material="PETG", *, sub_brands=None, color="112233FF", state=11, tag="0" * 16, uuid="0" * 32):
    return {
        "id": 0,
        "state": state,
        "tray_type": material,
        "tray_sub_brands": sub_brands if sub_brands is not None else f"{material} HF",
        "tray_color": color,
        "tray_id_name": "",
        "tray_info_idx": "",
        "tray_weight": "0",
        "tag_uid": tag,
        "tray_uuid": uuid,
        "remain": 40,
    }


def _bare(*, state=11, tray_type="", tag="0" * 16):
    return {
        "id": 0,
        "state": state,
        "tray_type": tray_type,
        "tray_sub_brands": "",
        "tray_color": "",
        "tray_info_idx": "",
        "tag_uid": tag,
        "tray_uuid": "0" * 32,
    }


async def _assignment(db, printer_id, ams_id=0, tray_id=0):
    res = await db.execute(
        select(SpoolAssignment)
        .options(selectinload(SpoolAssignment.spool))
        .where(
            SpoolAssignment.printer_id == printer_id,
            SpoolAssignment.ams_id == ams_id,
            SpoolAssignment.tray_id == tray_id,
        )
    )
    return res.scalar_one_or_none()


async def _seed_assignment(
    db, printer_id, ams_id, tray_id, *, material="PETG", rgba="112233FF", tag_uid=None, spent=False
):
    """Create a spool + SpoolAssignment at (ams_id, tray_id) and return the spool id.

    Tagless by default (no tag_uid/tray_uuid). The fingerprint is seeded from the
    material/colour so a same-filament tray re-binds on fingerprint match.
    """
    spool = Spool(
        material=material,
        rgba=rgba,
        data_origin="rfid_auto" if tag_uid else "ams_auto",
        tag_uid=tag_uid,
        spent_at=datetime.utcnow() if spent else None,
    )
    spool.k_profiles = []
    spool.assignments = []
    db.add(spool)
    await db.flush()
    db.add(
        SpoolAssignment(
            spool_id=spool.id,
            printer_id=printer_id,
            ams_id=ams_id,
            tray_id=tray_id,
            fingerprint_color=rgba,
            fingerprint_type=material,
        )
    )
    await db.commit()
    return spool.id


def _empty_tray(tray_id):
    """A slot that is present in the AMS payload but reports no filament (empty)."""
    return {"id": tray_id, "state": 9, "tray_type": "", "tag_uid": "0" * 16, "tray_uuid": "0" * 32}


def _ams(ams_id, trays):
    return [{"id": ams_id, "tray": trays}]


# --- mint_tagless_spool ----------------------------------------------------


class TestMint:
    async def test_from_tray_fields(self, db_session):
        spool = await spool_tagless.mint_tagless_spool(db_session, tray=_tray("PETG", sub_brands="PETG HF"))
        assert spool.data_origin == "ams_auto"
        assert spool.tag_type is None
        assert spool.tag_uid is None and spool.tray_uuid is None
        assert spool.weight_used == 0
        assert spool.material == "PETG"
        assert spool.subtype == "HF"
        assert spool.rgba == "112233FF"
        assert spool.brand is None  # tagless: brand unknown until the operator sets it
        assert spool.label_weight == 1000  # tray_weight "0" → Spool model default

    async def test_from_default_filament(self, db_session):
        default = {
            "brand": "Bambu Lab",
            "material": "PETG",
            "subtype": "HF",
            "rgba": "000000FF",
            "slicer_filament": "GFG99",
        }
        spool = await spool_tagless.mint_tagless_spool(db_session, default_filament=default)
        assert spool.data_origin == "ams_auto"
        assert spool.tag_type is None
        assert spool.material == "PETG"
        assert spool.subtype == "HF"
        assert spool.brand == "Bambu Lab"
        assert spool.rgba == "000000FF"
        assert spool.slicer_filament == "GFG99"
        assert spool.weight_used == 0
        assert spool.label_weight == 1000

    async def test_positive_tray_weight_overrides_default(self, db_session):
        tray = _tray("PLA")
        tray["tray_weight"] = "750"
        spool = await spool_tagless.mint_tagless_spool(db_session, tray=tray)
        assert spool.label_weight == 750

    async def test_requires_exactly_one_source(self, db_session):
        with pytest.raises(ValueError):
            await spool_tagless.mint_tagless_spool(db_session)
        with pytest.raises(ValueError):
            await spool_tagless.mint_tagless_spool(db_session, tray={}, default_filament={})


# --- broadcast origin ------------------------------------------------------


class TestBroadcastOrigin:
    """The ``spool_auto_assigned`` broadcast helper carries ``origin: "tagless"``
    ONLY for this module's silent mints; the RFID auto-assign broadcasts
    elsewhere call it with no origin and must stay field-absent so the frontend
    toasts only for a genuinely new untagged spool."""

    async def test_tagless_origin_present(self, env):
        await spool_tagless._broadcast_auto_assigned(1, 0, 2, 5, origin="tagless")
        payload = env.ws.call_args.args[0]
        assert payload["type"] == "spool_auto_assigned"
        assert payload["origin"] == "tagless"

    async def test_rfid_path_omits_origin(self, env):
        # Default (no origin) mirrors the RFID broadcast dicts in main.py /
        # routes.inventory — the key must be ABSENT, not None.
        await spool_tagless._broadcast_auto_assigned(1, 0, 2, 5)
        payload = env.ws.call_args.args[0]
        assert payload["type"] == "spool_auto_assigned"
        assert "origin" not in payload


# --- predicates ------------------------------------------------------------


class TestPredicates:
    def _asg(self, **spool_kwargs):
        return SimpleNamespace(spool=Spool(**spool_kwargs))

    def test_should_keep_on_empty_variants(self):
        # tagless, not spent, plenty remaining → keep.
        assert (
            spool_tagless.should_keep_on_empty(self._asg(material="PETG", label_weight=1000, weight_used=100), 30)
            is True
        )
        # effectively empty (remaining 10 <= 30) → not kept.
        assert (
            spool_tagless.should_keep_on_empty(self._asg(material="PETG", label_weight=1000, weight_used=990), 30)
            is False
        )
        # spent → KEPT (W1: the spent binding is the durable "ran dry" latch until a
        # physical roll swap releases it — a runout-instant flap can't phantom-mint).
        assert (
            spool_tagless.should_keep_on_empty(
                self._asg(material="PETG", label_weight=1000, weight_used=0, spent_at=datetime.utcnow()), 30
            )
            is True
        )
        # tagged (has RFID identity) → not kept.
        assert (
            spool_tagless.should_keep_on_empty(
                self._asg(material="PETG", label_weight=1000, weight_used=0, tag_uid=_VALID_TAG), 30
            )
            is False
        )
        # no spool → not kept.
        assert spool_tagless.should_keep_on_empty(SimpleNamespace(spool=None), 30) is False

    def test_fingerprint_matches(self):
        spool = Spool(material="PETG", rgba="112233FF")
        assert spool_tagless.fingerprint_matches(spool, _tray("PETG", color="112230FF")) is True  # near color
        assert spool_tagless.fingerprint_matches(spool, _tray("PLA", color="112233FF")) is False  # material
        assert spool_tagless.fingerprint_matches(spool, _tray("PETG", color="FF0000FF")) is False  # far color

    def test_is_tagless_spool(self):
        assert spool_tagless.is_tagless_spool(Spool(material="PETG")) is True
        assert spool_tagless.is_tagless_spool(Spool(material="PETG", tag_uid=_VALID_TAG)) is False
        assert spool_tagless.is_tagless_spool(Spool(material="PETG", tray_uuid="A" * 32)) is False
        assert spool_tagless.is_tagless_spool(None) is False


# --- Hook B: handle_tagless_slot -------------------------------------------


class TestHandleTaglessSlot:
    async def test_no_assignment_mints_and_assigns(self, db_session, printer_factory, env):
        printer = await printer_factory()
        handled = await spool_tagless.handle_tagless_slot(db_session, printer.id, 0, 0, _tray("PETG"), None, [])
        assert handled is True
        sa = await _assignment(db_session, printer.id)
        assert sa is not None
        spool = await db_session.get(Spool, sa.spool_id)
        assert spool.data_origin == "ams_auto"
        assert spool.first_loaded_at is not None
        env.ws.assert_awaited()  # spool_auto_assigned broadcast
        # A genuinely NEW tagless mint tags the broadcast so the frontend toasts.
        payload = env.ws.call_args.args[0]
        assert payload["type"] == "spool_auto_assigned"
        assert payload["origin"] == "tagless"

    async def test_auto_add_off_leaves_slot_alone(self, db_session, printer_factory, env):
        env.settings["auto_add_untagged"] = "false"
        printer = await printer_factory()
        handled = await spool_tagless.handle_tagless_slot(db_session, printer.id, 0, 0, _tray("PETG"), None, [])
        assert handled is True  # handled = deliberately do nothing
        assert await _assignment(db_session, printer.id) is None  # nothing minted

    async def test_rebind_preserves_spool_and_operator_edits(self, db_session, printer_factory, env):
        printer = await printer_factory()
        await spool_tagless.handle_tagless_slot(db_session, printer.id, 0, 0, _tray("PETG"), None, [])
        sa = await _assignment(db_session, printer.id)
        spool_id = sa.spool_id
        spool = await db_session.get(Spool, spool_id)
        spool.brand = "Operator Brand"  # operator edit between pushes
        spool.weight_used = 123.0  # ledger progressed
        await db_session.commit()

        # Second push, same filament → rebind, no new spool, no overwrite.
        sa2 = await _assignment(db_session, printer.id)
        handled = await spool_tagless.handle_tagless_slot(db_session, printer.id, 0, 0, _tray("PETG"), sa2, [])
        assert handled is True
        sa3 = await _assignment(db_session, printer.id)
        assert sa3.spool_id == spool_id  # same ledger row rebound
        spool2 = await db_session.get(Spool, spool_id)
        assert spool2.brand == "Operator Brand"  # edit survived
        assert spool2.weight_used == 123.0  # ledger intact
        count = await db_session.scalar(select(func.count(Spool.id)))
        assert count == 1  # no duplicate minted

    async def test_different_filament_unlinks_and_mints(self, db_session, printer_factory, env):
        printer = await printer_factory()
        await spool_tagless.handle_tagless_slot(db_session, printer.id, 0, 0, _tray("PETG", color="112233FF"), None, [])
        sa = await _assignment(db_session, printer.id)
        old_id = sa.spool_id

        sa2 = await _assignment(db_session, printer.id)
        handled = await spool_tagless.handle_tagless_slot(
            db_session, printer.id, 0, 0, _tray("PLA", color="FF0000FF"), sa2, []
        )
        assert handled is True
        sa3 = await _assignment(db_session, printer.id)
        assert sa3.spool_id != old_id  # new spool bound
        old_spool = await db_session.get(Spool, old_id)
        assert old_spool.archived_at is None  # old stays active, just unbound
        new_spool = await db_session.get(Spool, sa3.spool_id)
        assert new_spool.material == "PLA"

    async def test_spent_loaded_no_cycle_latches(self, db_session, printer_factory, env, caplog):
        # W1: spent + loaded but NO qualified physical cycle → keep the binding
        # (latched). No archive, no unlink, no mint — the phantom-mint the incident hit.
        printer = await printer_factory()
        spent = Spool(material="PETG", rgba="112233FF", data_origin="ams_auto", spent_at=datetime.utcnow())
        spent.k_profiles = []
        spent.assignments = []
        db_session.add(spent)
        await db_session.flush()
        db_session.add(SpoolAssignment(spool_id=spent.id, printer_id=printer.id, ams_id=0, tray_id=0))
        await db_session.commit()

        sa = await _assignment(db_session, printer.id)
        with caplog.at_level(logging.INFO, logger="backend.app.services.spool_tagless"):
            handled = await spool_tagless.handle_tagless_slot(db_session, printer.id, 0, 0, _tray("PETG"), sa, [])
        assert handled is True
        await db_session.refresh(spent)
        assert spent.archived_at is None  # latched — NOT archived
        sa2 = await _assignment(db_session, printer.id)
        assert sa2.spool_id == spent.id  # same spent row still bound
        count = await db_session.scalar(select(func.count(Spool.id)))
        assert count == 1  # nothing minted
        env.ws.assert_not_awaited()
        assert "latched" in "\n".join(r.message for r in caplog.records).lower()

    async def test_spent_loaded_with_cycle_tray_mints(self, db_session, printer_factory, env):
        # A qualified physical cycle + a DIFFERENT tray filament → archive spent,
        # mint from the tray, pending cycle consumed (popped).
        printer = await printer_factory()
        spent = Spool(material="PETG", rgba="112233FF", data_origin="ams_auto", spent_at=datetime.utcnow())
        spent.k_profiles = []
        spent.assignments = []
        db_session.add(spent)
        await db_session.flush()
        db_session.add(SpoolAssignment(spool_id=spent.id, printer_id=printer.id, ams_id=0, tray_id=0))
        await db_session.commit()

        spool_tagless._pending_physical_cycles.add((printer.id, 0, 0))
        sa = await _assignment(db_session, printer.id)
        handled = await spool_tagless.handle_tagless_slot(
            db_session, printer.id, 0, 0, _tray("PLA", color="FF0000FF"), sa, []
        )
        assert handled is True
        await db_session.refresh(spent)
        assert spent.archived_at is not None  # archived (grams preserved)
        sa2 = await _assignment(db_session, printer.id)
        assert sa2.spool_id != spent.id
        fresh = await db_session.get(Spool, sa2.spool_id)
        assert fresh.spent_at is None and fresh.data_origin == "ams_auto"
        assert fresh.material == "PLA" and fresh.brand is None  # tray-derived
        assert (printer.id, 0, 0) not in spool_tagless._pending_physical_cycles  # consumed

    async def test_spent_loaded_with_cycle_fingerprint_match_default_mints(self, db_session, printer_factory, env):
        # A qualified cycle where the tray still carries the DEPARTED config (firmware
        # leftover — fingerprint matches) → default-mint (clean identity), config pushed.
        env.settings["tagless_default_filament"] = json.dumps(
            {"brand": "Bambu Lab", "material": "PETG", "subtype": "HF", "rgba": "000000FF", "slicer_filament": "GFG02"}
        )
        printer = await printer_factory()
        spent = Spool(material="PETG", rgba="112233FF", data_origin="ams_auto", spent_at=datetime.utcnow())
        spent.k_profiles = []
        spent.assignments = []
        db_session.add(spent)
        await db_session.flush()
        db_session.add(SpoolAssignment(spool_id=spent.id, printer_id=printer.id, ams_id=0, tray_id=0))
        await db_session.commit()

        spool_tagless._pending_physical_cycles.add((printer.id, 0, 0))
        sa = await _assignment(db_session, printer.id)
        # Tray still reports the departed's PETG/112233 config → fingerprint matches.
        handled = await spool_tagless.handle_tagless_slot(
            db_session, printer.id, 0, 0, _tray("PETG", color="112233FF"), sa, []
        )
        assert handled is True
        sa2 = await _assignment(db_session, printer.id)
        fresh = await db_session.get(Spool, sa2.spool_id)
        assert fresh.brand == "Bambu Lab" and fresh.rgba == "000000FF"  # default identity, not the leftover
        assert fresh.slicer_filament == "GFG02"
        env.apply.assert_awaited_once()  # default-mint pushes config
        assert (printer.id, 0, 0) not in spool_tagless._pending_physical_cycles

    async def test_spent_not_loaded_no_churn(self, db_session, printer_factory, env):
        printer = await printer_factory()
        spent = Spool(material="PETG", rgba="112233FF", data_origin="ams_auto", spent_at=datetime.utcnow())
        spent.k_profiles = []
        spent.assignments = []
        db_session.add(spent)
        await db_session.flush()
        db_session.add(SpoolAssignment(spool_id=spent.id, printer_id=printer.id, ams_id=0, tray_id=0))
        await db_session.commit()

        sa = await _assignment(db_session, printer.id)
        # state 10 = present but filament not fed → not loaded → no churn.
        handled = await spool_tagless.handle_tagless_slot(db_session, printer.id, 0, 0, _tray("PETG", state=10), sa, [])
        assert handled is True
        sa2 = await _assignment(db_session, printer.id)
        assert sa2.spool_id == spent.id  # unchanged
        await db_session.refresh(spent)
        assert spent.archived_at is None

    async def test_tagged_bound_slot_returns_false(self, db_session, printer_factory, env):
        # A spent TAGGED spool must still reach the respool gate: Hook B returns
        # False (not ours) and leaves the assignment untouched.
        printer = await printer_factory()
        tagged = Spool(material="PETG", tag_uid=_VALID_TAG, data_origin="rfid_auto", spent_at=datetime.utcnow())
        tagged.k_profiles = []
        tagged.assignments = []
        db_session.add(tagged)
        await db_session.flush()
        db_session.add(SpoolAssignment(spool_id=tagged.id, printer_id=printer.id, ams_id=0, tray_id=0))
        await db_session.commit()

        sa = await _assignment(db_session, printer.id)
        handled = await spool_tagless.handle_tagless_slot(db_session, printer.id, 0, 0, _tray("PETG"), sa, [])
        assert handled is False  # RFID/respool flows own it
        sa2 = await _assignment(db_session, printer.id)
        assert sa2.spool_id == tagged.id  # untouched

    async def test_orphan_assignment_dropped_then_mints(self, db_session, printer_factory, env):
        # An assignment whose spool row is gone is an orphan — Hook B drops it and
        # mints fresh.
        printer = await printer_factory()
        db_session.add(SpoolAssignment(spool_id=999999, printer_id=printer.id, ams_id=0, tray_id=0))
        await db_session.commit()
        orphan = await _assignment(db_session, printer.id)  # .spool is None (dangling FK)
        handled = await spool_tagless.handle_tagless_slot(db_session, printer.id, 0, 0, _tray("PETG"), orphan, [])
        assert handled is True
        sa = await _assignment(db_session, printer.id)
        spool = await db_session.get(Spool, sa.spool_id)
        assert spool is not None and spool.data_origin == "ams_auto"

    async def test_defers_while_identify_in_flight(self, db_session, printer_factory, env, monkeypatch):
        # Guard 4: while an RFID identify is in flight on this slot, handle_tagless_slot
        # mints/pushes NOTHING and returns True — a falsy return would fall through
        # main.on_ams_change to the MUTATING spent→respool gate + weight-sync.
        monkeypatch.setattr("backend.app.services.ams_presence.identify_in_flight", lambda *a: True)
        printer = await printer_factory()
        handled = await spool_tagless.handle_tagless_slot(db_session, printer.id, 0, 0, _tray("PETG"), None, [])
        assert handled is True  # deferred → caller `continue`s
        assert await _assignment(db_session, printer.id) is None  # nothing minted
        env.ws.assert_not_awaited()  # no spool_auto_assigned broadcast
        env.apply.assert_not_awaited()  # no config push (no filament-setting write)

    async def test_processes_normally_when_not_in_flight(self, db_session, printer_factory, env, monkeypatch):
        # The complement: with no identify in flight the slot is handled as usual.
        monkeypatch.setattr("backend.app.services.ams_presence.identify_in_flight", lambda *a: False)
        printer = await printer_factory()
        handled = await spool_tagless.handle_tagless_slot(db_session, printer.id, 0, 0, _tray("PETG"), None, [])
        assert handled is True
        assert await _assignment(db_session, printer.id) is not None  # minted normally

    async def test_mid_print_no_idle_requirement(self, db_session, printer_factory, env, monkeypatch):
        # Hook B has no idle gate — a tagless spool inserted mid-print is minted
        # and tracked the same as when idle.
        monkeypatch.setattr(
            spool_tagless.printer_manager,
            "get_status",
            lambda pid: SimpleNamespace(state="RUNNING", nozzles=[], ams_extruder_map=None, raw_data={}, kprofiles=[]),
        )
        printer = await printer_factory()
        handled = await spool_tagless.handle_tagless_slot(db_session, printer.id, 0, 0, _tray("PETG"), None, [])
        assert handled is True
        assert await _assignment(db_session, printer.id) is not None


# --- Hook B branch (1): slot-move dedup ------------------------------------


class TestSlotMove:
    """A tagless roll physically MOVED to another slot on the SAME printer must
    re-bind its EXISTING ledger row (no duplicate mint). Phase 1's sticky-keep
    leaves the source assignment over the now-empty slot; branch (1) moves it."""

    async def test_unique_candidate_moves_ledger_row(self, db_session, printer_factory, env):
        printer = await printer_factory()
        # Roll originally at AMS0-T0; sticky-kept there over the now-empty slot.
        spool_id = await _seed_assignment(db_session, printer.id, 0, 0, material="PETG", rgba="112233FF")
        before = await db_session.scalar(select(func.count(Spool.id)))

        # Live payload: T0 now empty, T1 holds the same roll. Handle the NEW slot.
        ams_data = _ams(0, [_empty_tray(0), {**_tray("PETG", color="112233FF"), "id": 1}])
        handled = await spool_tagless.handle_tagless_slot(
            db_session, printer.id, 0, 1, _tray("PETG", color="112233FF"), None, ams_data
        )
        assert handled is True

        moved = await _assignment(db_session, printer.id, ams_id=0, tray_id=1)
        assert moved is not None and moved.spool_id == spool_id  # SAME ledger row re-bound
        assert await _assignment(db_session, printer.id, ams_id=0, tray_id=0) is None  # source released
        assert moved.fingerprint_type == "PETG"  # fingerprint refreshed for the new slot
        after = await db_session.scalar(select(func.count(Spool.id)))
        assert after == before  # NO new spool minted
        payload = env.ws.call_args.args[0]
        assert payload["type"] == "spool_auto_assigned" and payload["origin"] == "tagless"

    async def test_cross_ams_move_same_printer(self, db_session, printer_factory, env):
        printer = await printer_factory()
        spool_id = await _seed_assignment(db_session, printer.id, 0, 0, material="PETG", rgba="112233FF")

        # Moved from AMS0-T0 to AMS1-T0; AMS0-T0 reports empty in the payload.
        ams_data = _ams(0, [_empty_tray(0)]) + _ams(1, [{**_tray("PETG", color="112233FF"), "id": 0}])
        handled = await spool_tagless.handle_tagless_slot(
            db_session, printer.id, 1, 0, _tray("PETG", color="112233FF"), None, ams_data
        )
        assert handled is True
        moved = await _assignment(db_session, printer.id, ams_id=1, tray_id=0)
        assert moved is not None and moved.spool_id == spool_id
        assert await _assignment(db_session, printer.id, ams_id=0, tray_id=0) is None

    async def test_two_candidates_mint_and_warn(self, db_session, printer_factory, env, caplog):
        printer = await printer_factory()
        # Two same-fingerprint tagless rolls, both sticky-kept over empty slots.
        id_a = await _seed_assignment(db_session, printer.id, 0, 0, material="PETG", rgba="112233FF")
        id_b = await _seed_assignment(db_session, printer.id, 0, 2, material="PETG", rgba="112233FF")
        before = await db_session.scalar(select(func.count(Spool.id)))

        ams_data = _ams(0, [_empty_tray(0), {**_tray("PETG", color="112233FF"), "id": 1}, _empty_tray(2)])
        with caplog.at_level(logging.WARNING, logger="backend.app.services.spool_tagless"):
            handled = await spool_tagless.handle_tagless_slot(
                db_session, printer.id, 0, 1, _tray("PETG", color="112233FF"), None, ams_data
            )
        assert handled is True
        # Ambiguous → NO move: both source rows untouched, a fresh row minted at T1.
        assert await _assignment(db_session, printer.id, ams_id=0, tray_id=0) is not None
        assert await _assignment(db_session, printer.id, ams_id=0, tray_id=2) is not None
        new_sa = await _assignment(db_session, printer.id, ams_id=0, tray_id=1)
        assert new_sa is not None and new_sa.spool_id not in (id_a, id_b)
        after = await db_session.scalar(select(func.count(Spool.id)))
        assert after == before + 1  # a duplicate WAS minted (ambiguity = can't safely move)
        warned = "\n".join(r.message for r in caplog.records if r.levelno >= logging.WARNING)
        assert "ambiguous" in warned.lower()
        assert str(id_a) in warned and str(id_b) in warned

    async def test_source_absent_from_payload_not_a_candidate(self, db_session, printer_factory, env):
        printer = await printer_factory()
        id_a = await _seed_assignment(db_session, printer.id, 0, 0, material="PETG", rgba="112233FF")
        before = await db_session.scalar(select(func.count(Spool.id)))

        # AMS0-T0 is ABSENT from the payload (unknowable) → not a candidate → mint.
        ams_data = _ams(0, [{**_tray("PETG", color="112233FF"), "id": 1}])
        handled = await spool_tagless.handle_tagless_slot(
            db_session, printer.id, 0, 1, _tray("PETG", color="112233FF"), None, ams_data
        )
        assert handled is True
        assert await _assignment(db_session, printer.id, ams_id=0, tray_id=0) is not None  # source untouched
        new_sa = await _assignment(db_session, printer.id, ams_id=0, tray_id=1)
        assert new_sa is not None and new_sa.spool_id != id_a
        after = await db_session.scalar(select(func.count(Spool.id)))
        assert after == before + 1  # minted (no confident move)

    async def test_spent_tagged_and_different_fingerprint_not_candidates(self, db_session, printer_factory, env):
        printer = await printer_factory()
        # None of these three empty-source rows may be moved into the new slot:
        await _seed_assignment(db_session, printer.id, 0, 0, material="PETG", rgba="112233FF", spent=True)  # spent
        await _seed_assignment(
            db_session, printer.id, 0, 2, material="PETG", rgba="112233FF", tag_uid=_VALID_TAG
        )  # tagged
        await _seed_assignment(db_session, printer.id, 0, 3, material="PLA", rgba="FF0000FF")  # different filament
        before = await db_session.scalar(select(func.count(Spool.id)))

        ams_data = _ams(
            0, [_empty_tray(0), {**_tray("PETG", color="112233FF"), "id": 1}, _empty_tray(2), _empty_tray(3)]
        )
        handled = await spool_tagless.handle_tagless_slot(
            db_session, printer.id, 0, 1, _tray("PETG", color="112233FF"), None, ams_data
        )
        assert handled is True
        # No candidate qualified → fresh mint, all three source rows intact.
        assert await _assignment(db_session, printer.id, ams_id=0, tray_id=0) is not None
        assert await _assignment(db_session, printer.id, ams_id=0, tray_id=2) is not None
        assert await _assignment(db_session, printer.id, ams_id=0, tray_id=3) is not None
        assert await _assignment(db_session, printer.id, ams_id=0, tray_id=1) is not None
        after = await db_session.scalar(select(func.count(Spool.id)))
        assert after == before + 1

    async def test_nonempty_source_not_a_candidate(self, db_session, printer_factory, env):
        # The source slot STILL holds filament in the payload (not empty) → the roll
        # didn't leave it, so this is a genuinely new roll here → mint, don't move.
        printer = await printer_factory()
        id_a = await _seed_assignment(db_session, printer.id, 0, 0, material="PETG", rgba="112233FF")
        before = await db_session.scalar(select(func.count(Spool.id)))

        ams_data = _ams(
            0,
            [
                {**_tray("PETG", color="112233FF"), "id": 0},  # source STILL loaded
                {**_tray("PETG", color="112233FF"), "id": 1},
            ],
        )
        handled = await spool_tagless.handle_tagless_slot(
            db_session, printer.id, 0, 1, _tray("PETG", color="112233FF"), None, ams_data
        )
        assert handled is True
        assert await _assignment(db_session, printer.id, ams_id=0, tray_id=0) is not None
        new_sa = await _assignment(db_session, printer.id, ams_id=0, tray_id=1)
        assert new_sa is not None and new_sa.spool_id != id_a
        after = await db_session.scalar(select(func.count(Spool.id)))
        assert after == before + 1


# --- D3b: maybe_autoconfigure_bare_tray ------------------------------------


class TestBareTray:
    async def test_trigger_predicate_each_factor_negated(self, db_session, printer_factory, env):
        printer = await printer_factory()
        # state 9 (not present) → skip.
        assert await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare(state=9)) is False
        # non-empty tray_type (already configured) → skip.
        assert (
            await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare(tray_type="PETG"))
            is False
        )
        # valid tag present (RFID) → skip.
        assert (
            await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare(tag=_VALID_TAG))
            is False
        )
        # auto_add_untagged off → skip.
        env.settings["auto_add_untagged"] = "false"
        assert await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare()) is False
        env.settings["auto_add_untagged"] = "true"
        # setting cleared (feature off) → skip.
        env.settings["tagless_default_filament"] = ""
        assert await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare()) is False
        env.apply.assert_not_awaited()

    async def test_mints_seeds_fingerprint_and_pushes(self, db_session, printer_factory, env):
        env.settings["tagless_default_filament"] = json.dumps(
            {"brand": "Bambu Lab", "material": "PETG", "subtype": "HF", "rgba": "000000FF"}
        )
        printer = await printer_factory()
        handled = await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare())
        assert handled is True
        sa = await _assignment(db_session, printer.id)
        assert sa.fingerprint_color == "000000FF"  # seeded from the SETTING, not the empty tray
        assert sa.fingerprint_type == "PETG"
        spool = await db_session.get(Spool, sa.spool_id)
        assert spool.data_origin == "ams_auto"
        assert spool.first_loaded_at is not None
        env.apply.assert_awaited_once()
        assert env.apply.await_args.kwargs["spool"].id == spool.id  # config pushed for the minted spool

    async def test_default_setting_unset_uses_schema_default(self, db_session, printer_factory, env):
        # Setting never written → schema default (Bambu PETG HF) → feature on.
        printer = await printer_factory()
        handled = await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare())
        assert handled is True
        sa = await _assignment(db_session, printer.id)
        spool = await db_session.get(Spool, sa.spool_id)
        assert spool.material == "PETG"

    async def test_retry_dedup_and_clear(self, db_session, printer_factory, env, monkeypatch):
        printer = await printer_factory()
        # First call mints + assigns + pushes.
        assert await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare()) is True
        assert env.apply.await_count == 1
        # Second call within the retry window → skipped.
        assert await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare()) is False
        assert env.apply.await_count == 1
        # Advance past the retry window → re-push for the SAME spool (no re-mint).
        monkeypatch.setattr(spool_tagless, "monotonic", lambda: 1e9)
        assert await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare()) is True
        assert env.apply.await_count == 2
        count = await db_session.scalar(select(func.count(Spool.id)).where(Spool.data_origin == "ams_auto"))
        assert count == 1  # re-push did not mint a duplicate
        # Slot empties → dedup cleared.
        spool_tagless.clear_autoconfig_dedup(printer.id, 0, 0)
        assert (printer.id, 0, 0) not in spool_tagless._autoconfig_attempts

    async def test_never_overwrites_operator_bound_bare_slot(self, db_session, printer_factory, env):
        printer = await printer_factory()
        operator_spool = Spool(material="PLA", data_origin="manual")  # operator setup, no tag
        operator_spool.k_profiles = []
        operator_spool.assignments = []
        db_session.add(operator_spool)
        await db_session.flush()
        db_session.add(SpoolAssignment(spool_id=operator_spool.id, printer_id=printer.id, ams_id=0, tray_id=0))
        await db_session.commit()

        handled = await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare())
        assert handled is False  # operator-bound → never overwrite
        env.apply.assert_not_awaited()


# --- provisional disposal on RFID takeover ---------------------------------


class TestProvisionalDisposal:
    async def test_hard_delete_pristine(self, db_session):
        spool = Spool(material="PETG", data_origin="ams_auto")
        spool.k_profiles = []
        spool.assignments = []
        db_session.add(spool)
        await db_session.commit()
        spool_id = spool.id
        disp = await spool_tagless.dispose_provisional_on_tag(db_session, spool)
        await db_session.commit()
        assert disp == "hard-deleted"
        assert await db_session.get(Spool, spool_id) is None

    async def test_archive_when_ledger_present(self, db_session):
        spool = Spool(material="PETG", data_origin="ams_auto")
        spool.k_profiles = []
        spool.assignments = []
        db_session.add(spool)
        await db_session.flush()
        db_session.add(SpoolUsageHistory(spool_id=spool.id, weight_used=10.0, percent_used=1))
        await db_session.commit()
        disp = await spool_tagless.dispose_provisional_on_tag(db_session, spool)
        await db_session.commit()
        assert disp == "archived"
        await db_session.refresh(spool)
        assert spool.archived_at is not None

    async def test_kept_when_not_ams_auto(self, db_session):
        spool = Spool(material="PETG", data_origin="rfid_auto")
        spool.k_profiles = []
        spool.assignments = []
        db_session.add(spool)
        await db_session.commit()
        disp = await spool_tagless.dispose_provisional_on_tag(db_session, spool)
        assert disp == "kept"
        await db_session.refresh(spool)
        assert spool.archived_at is None


# --- force=True bare-tray sweep (spool_recovery's mid-print enrollment) ------


class TestForceBareTray:
    async def test_force_bypasses_only_the_retry_window(self, db_session, printer_factory, env):
        printer = await printer_factory()
        # First (unforced) call mints + pushes and stamps the retry window.
        assert await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare()) is True
        assert env.apply.await_count == 1
        # Second call INSIDE the window without force → skipped.
        assert await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare()) is False
        assert env.apply.await_count == 1
        # Same window but force=True → re-pushes (window bypassed), no re-mint.
        assert (
            await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare(), force=True) is True
        )
        assert env.apply.await_count == 2
        count = await db_session.scalar(select(func.count(Spool.id)).where(Spool.data_origin == "ams_auto"))
        assert count == 1  # forced re-push did not mint a duplicate

    async def test_force_still_respects_the_other_guards(self, db_session, printer_factory, env):
        printer = await printer_factory()
        # auto_add off → force does NOT override.
        env.settings["auto_add_untagged"] = "false"
        assert (
            await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare(), force=True)
            is False
        )
        env.settings["auto_add_untagged"] = "true"
        # Already-configured (non-bare) tray → force does NOT override.
        assert (
            await spool_tagless.maybe_autoconfigure_bare_tray(
                db_session, printer.id, 0, 0, _bare(tray_type="PETG"), force=True
            )
            is False
        )
        # RFID tray → force does NOT override.
        assert (
            await spool_tagless.maybe_autoconfigure_bare_tray(
                db_session, printer.id, 0, 0, _bare(tag=_VALID_TAG), force=True
            )
            is False
        )
        # Operator-bound slot → force does NOT override.
        operator_spool = Spool(material="PLA", data_origin="manual")
        operator_spool.k_profiles = []
        operator_spool.assignments = []
        db_session.add(operator_spool)
        await db_session.flush()
        db_session.add(SpoolAssignment(spool_id=operator_spool.id, printer_id=printer.id, ams_id=0, tray_id=0))
        await db_session.commit()
        assert (
            await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare(), force=True)
            is False
        )
        env.apply.assert_not_awaited()


# --- apply_spool_to_slot_via_mqtt lazy-load regression (prod 2026-07-17) -----


class TestApplySpoolLazyLoadRegression:
    async def test_db_loaded_spool_does_not_lazyload_and_publishes(self, db_session, printer_factory, monkeypatch):
        """The REAL callee behind the bare-tray push. A DB-loaded spool whose
        k_profiles relationship is NOT eager-loaded must publish the MQTT config
        without a greenlet/lazy-load crash (the deterministic bare-tray failure)."""
        from backend.app.api.routes.inventory import apply_spool_to_slot_via_mqtt
        from backend.app.models.spool_k_profile import SpoolKProfile
        from backend.app.services.printer_manager import printer_manager

        printer = await printer_factory()
        spool = Spool(material="PETG", rgba="00FF00FF", data_origin="ams_auto")
        spool.k_profiles = []
        spool.assignments = []
        db_session.add(spool)
        await db_session.flush()
        db_session.add(
            SpoolKProfile(spool_id=spool.id, printer_id=printer.id, nozzle_diameter="0.4", k_value=0.02, cali_idx=5)
        )
        await db_session.commit()
        spool_id = spool.id

        # Expire ONLY the k_profiles relationship so it is unloaded (columns stay
        # loaded): the old `for kp in spool.k_profiles` walk would greenlet-crash
        # on this object; the explicit-query fix must not.
        loaded = await db_session.get(Spool, spool_id)
        db_session.expire(loaded, ["k_profiles"])

        calls: list[tuple] = []

        class _FakeClient:
            def ams_set_filament_setting(self, **kw):
                calls.append(("set", kw))
                return True  # real client returns True on a successful publish

            def extrusion_cali_sel(self, **kw):
                calls.append(("cali", kw))
                return True

        monkeypatch.setattr(printer_manager, "get_client", lambda pid: _FakeClient())
        monkeypatch.setattr(printer_manager, "get_status", lambda pid: None)

        ok = await apply_spool_to_slot_via_mqtt(
            db=db_session,
            current_user=None,
            spool=loaded,
            printer_id=printer.id,
            ams_id=0,
            tray_id=0,
        )

        assert ok is True  # reached the end without raising
        assert any(c[0] == "set" for c in calls)  # filament setting published
        # The stored K-profile is found via the explicit query (cali_idx 5, not -1).
        cali = [c for c in calls if c[0] == "cali"]
        assert cali and cali[0][1]["cali_idx"] == 5

    async def test_refused_setting_skips_cali_and_preset(self, db_session, printer_factory, monkeypatch):
        # ams_set_filament_setting refused (AMS busy identifying/drying) → apply returns
        # False and NEITHER extrusion_cali_sel NOR the slot-preset persist runs: the DB
        # preset row must not record a write that never reached the printer.
        from backend.app.api.routes import inventory as inv
        from backend.app.services.printer_manager import printer_manager

        printer = await printer_factory()
        spool = Spool(material="PETG", rgba="00FF00FF", data_origin="ams_auto")
        spool.k_profiles = []
        spool.assignments = []
        db_session.add(spool)
        await db_session.commit()

        calls: list[tuple] = []

        class _RefusingClient:
            def ams_set_filament_setting(self, **kw):
                calls.append(("set", kw))
                return False  # refused — identifying/drying/offline

            def extrusion_cali_sel(self, **kw):
                calls.append(("cali", kw))
                return True

        monkeypatch.setattr(printer_manager, "get_client", lambda pid: _RefusingClient())
        monkeypatch.setattr(printer_manager, "get_status", lambda pid: None)
        preset = AsyncMock()
        monkeypatch.setattr("backend.app.services.slot_preset_writer.upsert_slot_preset_for_spool", preset)

        ok = await inv.apply_spool_to_slot_via_mqtt(
            db=db_session, current_user=None, spool=spool, printer_id=printer.id, ams_id=0, tray_id=0
        )
        assert ok is False
        assert [c[0] for c in calls] == ["set"]  # only the setting attempt — no cali
        preset.assert_not_awaited()  # no preset row for a write that never landed


class TestDryingDefers:
    """AMS drying disengages trays (presence flaps to state 10) and fails any config
    write / identify (HMS 0700_C069). handle_tagless_slot and maybe_autoconfigure_
    bare_tray defer while drying; the bare-tray retry window is not burned on the
    defer, and force= does not bypass the drying/identify guards."""

    async def test_handle_tagless_slot_defers_while_drying(self, db_session, printer_factory, env, monkeypatch):
        monkeypatch.setattr("backend.app.services.ams_presence.unit_drying", lambda *a: True)
        printer = await printer_factory()
        handled = await spool_tagless.handle_tagless_slot(db_session, printer.id, 0, 0, _tray("PETG"), None, [])
        assert handled is True  # deferred → caller `continue`s (no respool-gate fall-through)
        assert await _assignment(db_session, printer.id) is None  # nothing minted
        env.apply.assert_not_awaited()  # no config push

    async def test_bare_tray_defers_while_drying(self, db_session, printer_factory, env, monkeypatch):
        monkeypatch.setattr("backend.app.services.ams_presence.unit_drying", lambda *a: True)
        printer = await printer_factory()
        handled = await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare())
        assert handled is False
        assert await _assignment(db_session, printer.id) is None  # nothing minted
        env.apply.assert_not_awaited()
        # Retry window NOT burned: the doomed push never stamped _autoconfig_attempts.
        assert (printer.id, 0, 0) not in spool_tagless._autoconfig_attempts

    async def test_bare_tray_defers_while_identify_in_flight(self, db_session, printer_factory, env, monkeypatch):
        monkeypatch.setattr("backend.app.services.ams_presence.identify_in_flight", lambda *a: True)
        printer = await printer_factory()
        handled = await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare())
        assert handled is False
        assert (printer.id, 0, 0) not in spool_tagless._autoconfig_attempts  # window not burned
        env.apply.assert_not_awaited()

    async def test_bare_tray_force_still_respects_drying(self, db_session, printer_factory, env, monkeypatch):
        # force= bypasses ONLY the retry window — never the drying guard.
        monkeypatch.setattr("backend.app.services.ams_presence.unit_drying", lambda *a: True)
        printer = await printer_factory()
        handled = await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare(), force=True)
        assert handled is False
        env.apply.assert_not_awaited()

    async def test_retry_window_not_burned_processes_after_drying_ends(
        self, db_session, printer_factory, env, monkeypatch
    ):
        # Because the drying defer never stamped the retry window, the first call after
        # drying ends proceeds immediately (no wait for _AUTOCONFIG_RETRY_S).
        drying = {"v": True}
        monkeypatch.setattr("backend.app.services.ams_presence.unit_drying", lambda *a: drying["v"])
        printer = await printer_factory()
        assert await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare()) is False
        assert env.apply.await_count == 0
        drying["v"] = False  # drying ended
        assert await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare()) is True
        assert env.apply.await_count == 1  # processed immediately — window was never armed


# --- W4: mint temp stamping + generic-id override --------------------------


class TestMintIdentityW4:
    async def test_default_branch_stamps_temps(self, db_session):
        default = {
            "brand": "Bambu Lab",
            "material": "PETG",
            "subtype": "HF",
            "rgba": "000000FF",
            "slicer_filament": "GFG02",
            "nozzle_temp_min": 230,
            "nozzle_temp_max": 270,
        }
        spool = await spool_tagless.mint_tagless_spool(db_session, default_filament=default)
        assert spool.slicer_filament == "GFG02"
        assert spool.nozzle_temp_min == 230
        assert spool.nozzle_temp_max == 270

    async def test_tray_generic_id_overridden_by_default(self, db_session, env, monkeypatch):
        # Tray reports a GENERIC id (GFG99) but fingerprint-matches the specific default
        # -> mint the default's id + temps (stops GFG99 self-perpetuation).
        env.settings["tagless_default_filament"] = json.dumps(
            {
                "brand": "Bambu Lab",
                "material": "PETG",
                "subtype": "HF",
                "rgba": "000000FF",
                "slicer_filament": "GFG02",
                "nozzle_temp_min": 230,
                "nozzle_temp_max": 270,
            }
        )
        parsed = SimpleNamespace(
            material="PETG",
            subtype="HF",
            color_name=None,
            rgba="000000FF",
            core_weight=250,
            slicer_filament="GFG99",
            slicer_filament_name="Generic PETG",
            nozzle_temp_min=220,
            nozzle_temp_max=260,
            label_weight=0,
        )
        monkeypatch.setattr(spool_tagless, "parse_tray_fields", AsyncMock(return_value=parsed))
        spool = await spool_tagless.mint_tagless_spool(db_session, tray=_tray("PETG", color="000000FF"))
        assert spool.slicer_filament == "GFG02"  # overridden to the default's specific id
        assert spool.slicer_filament_name is None
        assert spool.nozzle_temp_min == 230 and spool.nozzle_temp_max == 270

    async def test_tray_generic_id_no_fingerprint_match_keeps_generic(self, db_session, env, monkeypatch):
        # Different material -> does NOT fingerprint-match the PETG default -> no override.
        env.settings["tagless_default_filament"] = json.dumps(
            {"brand": "Bambu Lab", "material": "PETG", "subtype": "HF", "rgba": "000000FF", "slicer_filament": "GFG02"}
        )
        parsed = SimpleNamespace(
            material="PLA",
            subtype=None,
            color_name=None,
            rgba="00FF00FF",
            core_weight=250,
            slicer_filament="GFL99",
            slicer_filament_name=None,
            nozzle_temp_min=190,
            nozzle_temp_max=230,
            label_weight=0,
        )
        monkeypatch.setattr(spool_tagless, "parse_tray_fields", AsyncMock(return_value=parsed))
        spool = await spool_tagless.mint_tagless_spool(db_session, tray=_tray("PLA", color="00FF00FF"))
        assert spool.slicer_filament == "GFL99"  # kept - no fingerprint match, no override

    async def test_default_temps_for_fingerprint(self, db_session, env):
        env.settings["tagless_default_filament"] = json.dumps(
            {
                "brand": "Bambu Lab",
                "material": "PETG",
                "subtype": "HF",
                "rgba": "000000FF",
                "nozzle_temp_min": 230,
                "nozzle_temp_max": 270,
            }
        )
        # Fingerprint match (PETG / near-black) -> the default's pair.
        assert await spool_tagless.default_temps_for_fingerprint(db_session, "PETG", "000000FF") == (230, 270)
        # Different material -> None.
        assert await spool_tagless.default_temps_for_fingerprint(db_session, "PLA", "000000FF") is None
        # Far colour -> None.
        assert await spool_tagless.default_temps_for_fingerprint(db_session, "PETG", "FF0000FF") is None


# --- W1: bare-tray spent-binding guard -------------------------------------


class TestBareTraySpentGuard:
    async def _seed_spent_ams_auto(self, db_session, printer_id):
        spent = Spool(material="PETG", rgba="000000FF", data_origin="ams_auto", spent_at=datetime.utcnow())
        spent.k_profiles = []
        spent.assignments = []
        db_session.add(spent)
        await db_session.flush()
        db_session.add(SpoolAssignment(spool_id=spent.id, printer_id=printer_id, ams_id=0, tray_id=0))
        await db_session.commit()
        return spent

    async def test_spent_bound_no_cycle_returns_false(self, db_session, printer_factory, env):
        printer = await printer_factory()
        spent = await self._seed_spent_ams_auto(db_session, printer.id)
        handled = await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare())
        assert handled is False  # latched - never re-push a spent slot's config
        await db_session.refresh(spent)
        assert spent.archived_at is None
        env.apply.assert_not_awaited()
        assert (printer.id, 0, 0) not in spool_tagless._autoconfig_attempts  # window not burned

    async def test_spent_bound_with_cycle_default_mints(self, db_session, printer_factory, env):
        printer = await printer_factory()
        spent = await self._seed_spent_ams_auto(db_session, printer.id)
        spool_tagless._pending_physical_cycles.add((printer.id, 0, 0))
        handled = await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare())
        assert handled is True
        await db_session.refresh(spent)
        assert spent.archived_at is not None  # archived
        sa = await _assignment(db_session, printer.id)
        fresh = await db_session.get(Spool, sa.spool_id)
        assert fresh.spent_at is None and fresh.data_origin == "ams_auto" and fresh.material == "PETG"
        env.apply.assert_awaited_once()  # default-mint pushes config
        assert (printer.id, 0, 0) not in spool_tagless._pending_physical_cycles  # consumed


# --- W5: fresh-roll prompt --------------------------------------------------


async def _seed_fresh_prompt_spool(db_session, printer_id, *, used, spent=False):
    sid = await _seed_assignment(db_session, printer_id, 0, 0, material="PETG", rgba="112233FF", spent=spent)
    spool = await db_session.get(Spool, sid)
    spool.label_weight = 1000
    spool.weight_used = float(used)
    await db_session.commit()
    return sid


class TestFreshRollPrompt:
    async def test_non_spent_past_threshold_prompts_and_pops(self, db_session, printer_factory, env):
        printer = await printer_factory()
        sid = await _seed_fresh_prompt_spool(db_session, printer.id, used=600)  # 60% >= 50%
        key = (printer.id, 0, 0)
        spool_tagless._pending_physical_cycles.add(key)
        await spool_tagless._maybe_prompt_fresh_roll(db_session, printer.id, 0, 0)
        payload = env.ws.call_args.args[0]
        assert payload["type"] == "tagless_fresh_prompt"
        assert payload["spool_id"] == sid and payload["material"] == "PETG"
        assert payload["remaining_g"] == 400.0 and payload["rgba"] == "112233FF"
        assert key in spool_tagless._fresh_prompt_unanswered
        assert key not in spool_tagless._pending_physical_cycles  # popped (processed)

    async def test_spent_silent_keeps_pending(self, db_session, printer_factory, env):
        printer = await printer_factory()
        await _seed_fresh_prompt_spool(db_session, printer.id, used=600, spent=True)
        key = (printer.id, 0, 0)
        spool_tagless._pending_physical_cycles.add(key)
        await spool_tagless._maybe_prompt_fresh_roll(db_session, printer.id, 0, 0)
        env.ws.assert_not_awaited()  # spent -> silent (the W1 spent->mint transition owns it)
        assert key in spool_tagless._pending_physical_cycles  # left for W1

    async def test_sub_threshold_pops_no_prompt(self, db_session, printer_factory, env):
        printer = await printer_factory()
        await _seed_fresh_prompt_spool(db_session, printer.id, used=100)  # 10% < 50%
        key = (printer.id, 0, 0)
        spool_tagless._pending_physical_cycles.add(key)
        await spool_tagless._maybe_prompt_fresh_roll(db_session, printer.id, 0, 0)
        env.ws.assert_not_awaited()
        assert key not in spool_tagless._pending_physical_cycles  # popped, no-op

    async def test_dedup_no_second_prompt_same_cycle(self, db_session, printer_factory, env):
        printer = await printer_factory()
        await _seed_fresh_prompt_spool(db_session, printer.id, used=600)
        key = (printer.id, 0, 0)
        spool_tagless._pending_physical_cycles.add(key)
        await spool_tagless._maybe_prompt_fresh_roll(db_session, printer.id, 0, 0)
        assert env.ws.await_count == 1
        spool_tagless._pending_physical_cycles.add(key)  # another cycle, still unanswered
        await spool_tagless._maybe_prompt_fresh_roll(db_session, printer.id, 0, 0)
        assert env.ws.await_count == 1  # suppressed while unanswered

    async def test_reasks_after_answer_clears(self, db_session, printer_factory, env):
        printer = await printer_factory()
        await _seed_fresh_prompt_spool(db_session, printer.id, used=600)
        key = (printer.id, 0, 0)
        spool_tagless._pending_physical_cycles.add(key)
        await spool_tagless._maybe_prompt_fresh_roll(db_session, printer.id, 0, 0)
        assert env.ws.await_count == 1
        spool_tagless.clear_fresh_prompt(printer.id, 0, 0)  # operator answered
        spool_tagless._pending_physical_cycles.add(key)  # a NEW qualified cycle
        await spool_tagless._maybe_prompt_fresh_roll(db_session, printer.id, 0, 0)
        assert env.ws.await_count == 2  # re-asks


@pytest.fixture
def sessions(test_engine, monkeypatch):
    """Point spool_tagless's own-session opener (note_physical_cycle) at the test
    engine - mirrors the ams_presence AMS-hook fixture."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    import backend.app.core.database as core_db

    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(core_db, "async_session", maker)
    return maker


class TestNotePhysicalCycle:
    async def test_records_pending_and_prompts_non_spent(self, db_session, printer_factory, env, sessions):
        printer = await printer_factory()
        sid = await _seed_fresh_prompt_spool(db_session, printer.id, used=700)
        await spool_tagless.note_physical_cycle(printer.id, 0, 0)
        payload = env.ws.call_args.args[0]
        assert payload["type"] == "tagless_fresh_prompt" and payload["spool_id"] == sid
        assert (printer.id, 0, 0) not in spool_tagless._pending_physical_cycles  # non-spent -> popped

    async def test_records_pending_spent_leaves_it(self, db_session, printer_factory, env, sessions):
        printer = await printer_factory()
        await _seed_fresh_prompt_spool(db_session, printer.id, used=700, spent=True)
        await spool_tagless.note_physical_cycle(printer.id, 0, 0)
        env.ws.assert_not_awaited()  # spent -> silent
        assert (printer.id, 0, 0) in spool_tagless._pending_physical_cycles  # left for the W1 transition


class TestTaglessReplay:
    def _present_state(self):
        return SimpleNamespace(raw_data={"ams": [{"id": 0, "tray": [{"id": 0, "state": 11, "tray_type": "PETG"}]}]})

    async def test_resends_valid_unanswered(self, db_session, printer_factory, monkeypatch):
        printer = await printer_factory()
        sid = await _seed_fresh_prompt_spool(db_session, printer.id, used=700)
        spool_tagless._fresh_prompt_unanswered.add((printer.id, 0, 0))
        monkeypatch.setattr(spool_tagless.printer_manager, "get_status", lambda pid: self._present_state())
        send = AsyncMock()
        n = await spool_tagless.rebroadcast_unresolved_tagless_prompts(db_session, send)
        assert n == 1
        payload = send.await_args.args[0]
        assert payload["type"] == "tagless_fresh_prompt" and payload["spool_id"] == sid

    async def test_drops_stale_spent(self, db_session, printer_factory, monkeypatch):
        printer = await printer_factory()
        await _seed_fresh_prompt_spool(db_session, printer.id, used=700, spent=True)
        spool_tagless._fresh_prompt_unanswered.add((printer.id, 0, 0))
        monkeypatch.setattr(spool_tagless.printer_manager, "get_status", lambda pid: self._present_state())
        send = AsyncMock()
        n = await spool_tagless.rebroadcast_unresolved_tagless_prompts(db_session, send)
        assert n == 0  # spent row -> dropped
        send.assert_not_awaited()

    async def test_drops_when_slot_absent(self, db_session, printer_factory, monkeypatch):
        printer = await printer_factory()
        await _seed_fresh_prompt_spool(db_session, printer.id, used=700)
        spool_tagless._fresh_prompt_unanswered.add((printer.id, 0, 0))
        monkeypatch.setattr(spool_tagless.printer_manager, "get_status", lambda pid: None)  # printer gone
        send = AsyncMock()
        n = await spool_tagless.rebroadcast_unresolved_tagless_prompts(db_session, send)
        assert n == 0
        send.assert_not_awaited()


def test_marker_machinery_removed():
    """W1: the stale-config marker machinery is deleted outright - every symbol gone."""
    for name in (
        "record_stale_marker",
        "record_stale_marker_for_spool",
        "clear_stale_marker",
        "_stale_config_markers",
        "_marker_matches",
    ):
        assert not hasattr(spool_tagless, name), f"{name} should be removed"
