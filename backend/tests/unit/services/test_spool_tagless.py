"""Tests for the tagless (non-RFID) spool lifecycle — services.spool_tagless.

Covers minting from both sources, Hook B slot policy (mint / sticky-rebind /
spent-replace / different-filament / tagged-passthrough), the bare-tray
auto-config (D3b) with its retry dedup, the stale-config firmware-leftover
override, and provisional disposal on RFID takeover.
"""

import json
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
        # spent → not kept.
        assert (
            spool_tagless.should_keep_on_empty(
                self._asg(material="PETG", label_weight=1000, weight_used=0, spent_at=datetime.utcnow()), 30
            )
            is False
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
        handled = await spool_tagless.handle_tagless_slot(db_session, printer.id, 0, 0, _tray("PETG"), None)
        assert handled is True
        sa = await _assignment(db_session, printer.id)
        assert sa is not None
        spool = await db_session.get(Spool, sa.spool_id)
        assert spool.data_origin == "ams_auto"
        assert spool.first_loaded_at is not None
        env.ws.assert_awaited()  # spool_auto_assigned broadcast

    async def test_auto_add_off_leaves_slot_alone(self, db_session, printer_factory, env):
        env.settings["auto_add_untagged"] = "false"
        printer = await printer_factory()
        handled = await spool_tagless.handle_tagless_slot(db_session, printer.id, 0, 0, _tray("PETG"), None)
        assert handled is True  # handled = deliberately do nothing
        assert await _assignment(db_session, printer.id) is None  # nothing minted

    async def test_rebind_preserves_spool_and_operator_edits(self, db_session, printer_factory, env):
        printer = await printer_factory()
        await spool_tagless.handle_tagless_slot(db_session, printer.id, 0, 0, _tray("PETG"), None)
        sa = await _assignment(db_session, printer.id)
        spool_id = sa.spool_id
        spool = await db_session.get(Spool, spool_id)
        spool.brand = "Operator Brand"  # operator edit between pushes
        spool.weight_used = 123.0  # ledger progressed
        await db_session.commit()

        # Second push, same filament → rebind, no new spool, no overwrite.
        sa2 = await _assignment(db_session, printer.id)
        handled = await spool_tagless.handle_tagless_slot(db_session, printer.id, 0, 0, _tray("PETG"), sa2)
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
        await spool_tagless.handle_tagless_slot(db_session, printer.id, 0, 0, _tray("PETG", color="112233FF"), None)
        sa = await _assignment(db_session, printer.id)
        old_id = sa.spool_id

        sa2 = await _assignment(db_session, printer.id)
        handled = await spool_tagless.handle_tagless_slot(
            db_session, printer.id, 0, 0, _tray("PLA", color="FF0000FF"), sa2
        )
        assert handled is True
        sa3 = await _assignment(db_session, printer.id)
        assert sa3.spool_id != old_id  # new spool bound
        old_spool = await db_session.get(Spool, old_id)
        assert old_spool.archived_at is None  # old stays active, just unbound
        new_spool = await db_session.get(Spool, sa3.spool_id)
        assert new_spool.material == "PLA"

    async def test_spent_loaded_archives_and_mints(self, db_session, printer_factory, env):
        printer = await printer_factory()
        spent = Spool(material="PETG", rgba="112233FF", data_origin="ams_auto", spent_at=datetime.utcnow())
        spent.k_profiles = []
        spent.assignments = []
        db_session.add(spent)
        await db_session.flush()
        db_session.add(SpoolAssignment(spool_id=spent.id, printer_id=printer.id, ams_id=0, tray_id=0))
        await db_session.commit()

        sa = await _assignment(db_session, printer.id)
        handled = await spool_tagless.handle_tagless_slot(db_session, printer.id, 0, 0, _tray("PETG"), sa)
        assert handled is True
        await db_session.refresh(spent)
        assert spent.archived_at is not None  # archived (grams preserved)
        sa2 = await _assignment(db_session, printer.id)
        assert sa2.spool_id != spent.id
        fresh = await db_session.get(Spool, sa2.spool_id)
        assert fresh.spent_at is None and fresh.data_origin == "ams_auto"

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
        handled = await spool_tagless.handle_tagless_slot(db_session, printer.id, 0, 0, _tray("PETG", state=10), sa)
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
        handled = await spool_tagless.handle_tagless_slot(db_session, printer.id, 0, 0, _tray("PETG"), sa)
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
        handled = await spool_tagless.handle_tagless_slot(db_session, printer.id, 0, 0, _tray("PETG"), orphan)
        assert handled is True
        sa = await _assignment(db_session, printer.id)
        spool = await db_session.get(Spool, sa.spool_id)
        assert spool is not None and spool.data_origin == "ams_auto"

    async def test_mid_print_no_idle_requirement(self, db_session, printer_factory, env, monkeypatch):
        # Hook B has no idle gate — a tagless spool inserted mid-print is minted
        # and tracked the same as when idle.
        monkeypatch.setattr(
            spool_tagless.printer_manager,
            "get_status",
            lambda pid: SimpleNamespace(state="RUNNING", nozzles=[], ams_extruder_map=None, raw_data={}, kprofiles=[]),
        )
        printer = await printer_factory()
        handled = await spool_tagless.handle_tagless_slot(db_session, printer.id, 0, 0, _tray("PETG"), None)
        assert handled is True
        assert await _assignment(db_session, printer.id) is not None


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


# --- stale-config firmware-leftover override -------------------------------


class TestStaleConfigOverride:
    async def test_leftover_equal_applies_default(self, db_session, printer_factory, env):
        env.settings["tagless_default_filament"] = json.dumps(
            {"brand": "Bambu Lab", "material": "PETG", "subtype": "HF", "rgba": "000000FF"}
        )
        printer = await printer_factory()
        spent = Spool(material="PLA", rgba="FF0000FF", spent_at=datetime.utcnow(), data_origin="ams_auto")
        spool_tagless.record_stale_marker_for_spool(printer.id, 0, 0, spent)

        # Firmware re-reports the SAME leftover config (PLA red) → stale → apply DEFAULT.
        handled = await spool_tagless.handle_tagless_slot(
            db_session, printer.id, 0, 0, _tray("PLA", color="FF0000FF"), None
        )
        assert handled is True
        sa = await _assignment(db_session, printer.id)
        spool = await db_session.get(Spool, sa.spool_id)
        assert spool.material == "PETG"  # default applied, NOT the PLA leftover
        assert sa.fingerprint_type == "PETG"  # seeded from the setting
        env.apply.assert_awaited_once()
        assert (printer.id, 0, 0) not in spool_tagless._stale_config_markers  # consumed

    async def test_differing_config_respected(self, db_session, printer_factory, env):
        env.settings["tagless_default_filament"] = json.dumps(
            {"brand": "Bambu Lab", "material": "PETG", "subtype": "HF", "rgba": "000000FF"}
        )
        printer = await printer_factory()
        spent = Spool(material="PLA", rgba="FF0000FF", spent_at=datetime.utcnow(), data_origin="ams_auto")
        spool_tagless.record_stale_marker_for_spool(printer.id, 0, 0, spent)

        # A genuinely different filament (PLA blue) → not the leftover → normal
        # tray-derived mint, marker cleared.
        handled = await spool_tagless.handle_tagless_slot(
            db_session, printer.id, 0, 0, _tray("PLA", color="0000FFFF"), None
        )
        assert handled is True
        sa = await _assignment(db_session, printer.id)
        spool = await db_session.get(Spool, sa.spool_id)
        assert spool.material == "PLA"  # minted from the REAL tray, not the default
        assert spool.brand is None  # tray-derived (the default would have set a brand)
        assert (printer.id, 0, 0) not in spool_tagless._stale_config_markers  # cleared
        env.apply.assert_not_awaited()  # tray-derived path doesn't push config

    def test_marker_record_and_clear(self):
        spent = Spool(material="PLA", rgba="FF0000FF", spent_at=datetime.utcnow())
        spool_tagless.record_stale_marker_for_spool(1, 0, 0, spent)
        assert (1, 0, 0) in spool_tagless._stale_config_markers
        spool_tagless.clear_stale_marker(1, 0, 0)
        assert (1, 0, 0) not in spool_tagless._stale_config_markers
        # A non-spent spool records nothing.
        spool_tagless.record_stale_marker_for_spool(1, 0, 1, Spool(material="PLA", rgba="00FF00FF", spent_at=None))
        assert (1, 0, 1) not in spool_tagless._stale_config_markers


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
