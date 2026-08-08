"""Tests for the tagless (non-RFID) spool SUPPORT lanes — services.spool_tagless.

Scope after the W3b cutover: minting from both sources, the bare-tray auto-config
(D3b) with its retry dedup and wire-safety defers, the stale-config
firmware-leftover override, the W5 fresh-roll prompt + its "New roll" executor, and
provisional disposal on RFID takeover.

Slot IDENTITY is no longer decided here — the ``handle_tagless_slot`` branch tree and
the in-place slot-move rebind were deleted, and their behaviours are pinned against
the decision table (``test_slot_state.py``) and the orchestrator
(``test_slot_pipeline.py``) instead.
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
        monkeypatch.setattr("backend.app.utils.retry_window.monotonic", lambda: 1e9)
        assert await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare()) is True
        assert env.apply.await_count == 2
        count = await db_session.scalar(select(func.count(Spool.id)).where(Spool.data_origin == "ams_auto"))
        assert count == 1  # re-push did not mint a duplicate
        # Slot empties → dedup cleared.
        spool_tagless.clear_autoconfig_dedup(printer.id, 0, 0)
        assert (printer.id, 0, 0) not in spool_tagless._autoconfig_window

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


# --- move semantics at the tagless bind site (012-H2S) ---------------------


class TestAssignFromSettingMoves:
    """``_assign_from_setting`` binds through ``spool_binding.bind_spool_to_slot``, so a
    spool already bound to another slot is MOVED, never copied — one spool ⇔ at most one
    slot, fleet-wide. The pre-fix helper deleted only the target slot's row."""

    async def test_moves_spool_bound_to_another_slot(self, db_session, printer_factory):
        printer = await printer_factory()
        spool_id = await _seed_assignment(db_session, printer.id, 0, 0, material="PETG", rgba="112233FF")
        spool = await db_session.get(Spool, spool_id)

        await spool_tagless._assign_from_setting(
            db_session, spool, printer.id, 0, 2, {"material": "PETG", "rgba": "000000FF"}
        )
        await db_session.commit()

        rows = (
            (await db_session.execute(select(SpoolAssignment).where(SpoolAssignment.spool_id == spool_id)))
            .scalars()
            .all()
        )
        assert len(rows) == 1, "the roll moved — the source binding must be gone"
        assert (rows[0].ams_id, rows[0].tray_id) == (0, 2)
        assert rows[0].fingerprint_color == "000000FF"  # seeded from the SETTING
        assert rows[0].fingerprint_type == "PETG"
        assert await _assignment(db_session, printer.id, 0, 0) is None  # source slot released


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
    write / identify (HMS 0700_C069). ``maybe_autoconfigure_bare_tray`` defers while
    drying, the retry window is not burned on the defer, and force= does not bypass the
    drying/identify guards. (The pipeline's own drying defer is row 1 of the decision
    table — pinned in ``test_slot_state.py``.)"""

    async def test_bare_tray_defers_while_drying(self, db_session, printer_factory, env, monkeypatch):
        monkeypatch.setattr("backend.app.services.ams_presence.unit_drying", lambda *a: True)
        printer = await printer_factory()
        handled = await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare())
        assert handled is False
        assert await _assignment(db_session, printer.id) is None  # nothing minted
        env.apply.assert_not_awaited()
        # Retry window NOT burned: the doomed push never stamped the retry window.
        assert (printer.id, 0, 0) not in spool_tagless._autoconfig_window

    async def test_bare_tray_defers_while_identify_in_flight(self, db_session, printer_factory, env, monkeypatch):
        monkeypatch.setattr("backend.app.services.ams_presence.identify_in_flight", lambda *a: True)
        printer = await printer_factory()
        handled = await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare())
        assert handled is False
        assert (printer.id, 0, 0) not in spool_tagless._autoconfig_window  # window not burned
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
        assert (printer.id, 0, 0) not in spool_tagless._autoconfig_window  # window not burned

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


async def _pending_stamp(db, spool_id):
    """The durable fresh-roll pending stamp for a row, read back from the DB."""
    return (await db.get(Spool, spool_id)).fresh_prompt_pending_at


@pytest.fixture
def seated(env, monkeypatch):
    """Make AMS0-T0 read PRESENT on the LIVE merged state.

    The fresh-roll prompt re-checks presence at stamp time (2026-08-07): a cycle whose
    slot is EMPTY by the time the question would be asked is discarded, because "is this
    a fresh roll?" has no answer for an empty tray — and the stamp is DURABLE, so the
    operator was left holding a toast about nothing. ``env`` deliberately leaves the
    printer state None, so every prompting case seats its tray explicitly. Depends on
    ``env`` so this patch is applied after (and wins over) that fixture's.
    """
    status = SimpleNamespace(raw_data={"ams": [{"id": 0, "tray": [{"id": 0, "state": 11, "tray_type": "PETG"}]}]})
    monkeypatch.setattr(spool_tagless.printer_manager, "get_status", lambda pid: status)
    return status


class TestFreshRollPrompt:
    async def test_non_spent_past_threshold_prompts_and_stamps(self, db_session, printer_factory, env, seated):
        printer = await printer_factory()
        sid = await _seed_fresh_prompt_spool(db_session, printer.id, used=750)  # 75% >= 70%
        key = (printer.id, 0, 0)
        spool_tagless._pending_physical_cycles.add(key)
        await spool_tagless._maybe_prompt_fresh_roll(db_session, printer.id, 0, 0)
        payload = env.ws.call_args.args[0]
        assert payload["type"] == "tagless_fresh_prompt"
        assert payload["spool_id"] == sid and payload["material"] == "PETG"
        assert payload["remaining_g"] == 250.0 and payload["rgba"] == "112233FF"
        assert await _pending_stamp(db_session, sid) is not None  # DURABLE, not process memory
        assert key not in spool_tagless._pending_physical_cycles  # popped (processed)

    async def test_absent_tray_at_prompt_time_discards_the_cycle(self, db_session, printer_factory, env, monkeypatch):
        """2026-08-07: the prompt stamped + broadcast without re-checking that the slot
        still HOLDS a roll. "Is this a fresh roll?" has no answer for an empty tray, and
        the stamp is DURABLE — a phantom edge (an AMS engage transient, an identify flap)
        or an operator who pulled the roll straight back out left a toast nobody could
        honestly answer. Absent at prompt time ⇒ no stamp, no broadcast, cycle discarded.
        """
        printer = await printer_factory()
        sid = await _seed_fresh_prompt_spool(db_session, printer.id, used=750)  # well past 70 %
        empty = SimpleNamespace(raw_data={"ams": [{"id": 0, "tray": [{"id": 0, "state": 9, "tray_type": ""}]}]})
        monkeypatch.setattr(spool_tagless.printer_manager, "get_status", lambda pid: empty)

        key = (printer.id, 0, 0)
        spool_tagless._pending_physical_cycles.add(key)
        await spool_tagless._maybe_prompt_fresh_roll(db_session, printer.id, 0, 0)

        env.ws.assert_not_awaited()
        assert await _pending_stamp(db_session, sid) is None  # nothing durable was written
        assert key not in spool_tagless._pending_physical_cycles  # phantom cycle discarded

    async def test_unreachable_printer_at_prompt_time_discards_the_cycle(self, db_session, printer_factory, env):
        # ``env`` leaves get_status returning None (printer gone / never connected). No
        # live tray ⇒ presence is not established ⇒ the same fail-closed outcome.
        printer = await printer_factory()
        sid = await _seed_fresh_prompt_spool(db_session, printer.id, used=750)
        key = (printer.id, 0, 0)
        spool_tagless._pending_physical_cycles.add(key)
        await spool_tagless._maybe_prompt_fresh_roll(db_session, printer.id, 0, 0)

        env.ws.assert_not_awaited()
        assert await _pending_stamp(db_session, sid) is None
        assert key not in spool_tagless._pending_physical_cycles

    async def test_spent_silent_keeps_pending(self, db_session, printer_factory, env):
        printer = await printer_factory()
        await _seed_fresh_prompt_spool(db_session, printer.id, used=750, spent=True)
        key = (printer.id, 0, 0)
        spool_tagless._pending_physical_cycles.add(key)
        await spool_tagless._maybe_prompt_fresh_roll(db_session, printer.id, 0, 0)
        env.ws.assert_not_awaited()  # spent -> silent (the W1 spent->mint transition owns it)
        assert key in spool_tagless._pending_physical_cycles  # left for W1

    async def test_sub_threshold_pops_no_prompt(self, db_session, printer_factory, env, seated):
        printer = await printer_factory()
        sid = await _seed_fresh_prompt_spool(db_session, printer.id, used=100)  # 10% < 70%
        key = (printer.id, 0, 0)
        spool_tagless._pending_physical_cycles.add(key)
        await spool_tagless._maybe_prompt_fresh_roll(db_session, printer.id, 0, 0)
        env.ws.assert_not_awaited()
        assert await _pending_stamp(db_session, sid) is None  # nothing asked → nothing stamped
        assert key not in spool_tagless._pending_physical_cycles  # popped, no-op

    async def test_every_new_cycle_reasks_and_refreshes_the_stamp(self, db_session, printer_factory, env, seated):
        """Operator contract (2026-07-25): every new qualified physical cycle re-asks.
        This function only runs ON a cycle edge, so an outstanding stamp is REFRESHED,
        never used to suppress — the old in-memory dedup made a stuck entry silence the
        slot for the rest of the process."""
        printer = await printer_factory()
        sid = await _seed_fresh_prompt_spool(db_session, printer.id, used=750)
        key = (printer.id, 0, 0)
        spool_tagless._pending_physical_cycles.add(key)
        await spool_tagless._maybe_prompt_fresh_roll(db_session, printer.id, 0, 0)
        assert env.ws.await_count == 1
        first = await _pending_stamp(db_session, sid)

        spool_tagless._pending_physical_cycles.add(key)  # another roll swap, still unanswered
        await spool_tagless._maybe_prompt_fresh_roll(db_session, printer.id, 0, 0)
        assert env.ws.await_count == 2  # re-asks
        assert await _pending_stamp(db_session, sid) >= first  # stamp refreshed, never dropped

    async def test_reasks_after_answer_clears(self, db_session, printer_factory, env, seated):
        printer = await printer_factory()
        sid = await _seed_fresh_prompt_spool(db_session, printer.id, used=750)
        key = (printer.id, 0, 0)
        spool_tagless._pending_physical_cycles.add(key)
        await spool_tagless._maybe_prompt_fresh_roll(db_session, printer.id, 0, 0)
        assert env.ws.await_count == 1
        await spool_tagless.clear_fresh_prompt(db_session, await db_session.get(Spool, sid))  # operator answered
        assert await _pending_stamp(db_session, sid) is None
        spool_tagless._pending_physical_cycles.add(key)  # a NEW qualified cycle
        await spool_tagless._maybe_prompt_fresh_roll(db_session, printer.id, 0, 0)
        assert env.ws.await_count == 2  # re-asks
        assert await _pending_stamp(db_session, sid) is not None

    async def test_threshold_is_seventy_percent(self, db_session, printer_factory, env, seated):
        """F4 (operator 2026-07-20): the prompt waits until the roll is ≥70 % consumed
        (≤300 g left on a 1000 g label) — a swap earlier in the roll's life is routine
        and asking then is noise."""
        assert spool_tagless._FRESH_ROLL_PROMPT_USED_FRAC == 0.7
        printer = await printer_factory()
        key = (printer.id, 0, 0)

        # 60 % used — under the new threshold → no prompt (it DID prompt at 0.5).
        await _seed_fresh_prompt_spool(db_session, printer.id, used=600)
        spool_tagless._pending_physical_cycles.add(key)
        await spool_tagless._maybe_prompt_fresh_roll(db_session, printer.id, 0, 0)
        env.ws.assert_not_awaited()
        assert key not in spool_tagless._pending_physical_cycles  # popped, no-op

        # Same slot at 75 % used → prompts.
        sa = await _assignment(db_session, printer.id)
        spool = await db_session.get(Spool, sa.spool_id)
        spool.weight_used = 750.0
        await db_session.commit()
        spool_tagless._pending_physical_cycles.add(key)
        await spool_tagless._maybe_prompt_fresh_roll(db_session, printer.id, 0, 0)
        assert env.ws.await_count == 1
        assert env.ws.call_args.args[0]["type"] == "tagless_fresh_prompt"


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
    async def test_records_pending_and_prompts_non_spent(self, db_session, printer_factory, env, sessions, seated):
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
    """The snapshot + replay pair, driven ENTIRELY from the durable stamp — no module
    state is armed anywhere in this class (``_clean_state`` wipes it), which is the
    point: the 19:39 prompt reached zero websocket clients and a 21:20 restart wiped
    the RAM set, so the replay had nothing to replay."""

    def _present_state(self):
        return SimpleNamespace(raw_data={"ams": [{"id": 0, "tray": [{"id": 0, "state": 11, "tray_type": "PETG"}]}]})

    async def _stamp(self, db_session, spool_id):
        spool = await db_session.get(Spool, spool_id)
        spool.fresh_prompt_pending_at = datetime.utcnow()
        await db_session.commit()

    async def test_resends_stamped_prompt_after_a_restart(self, db_session, printer_factory, monkeypatch):
        printer = await printer_factory()
        sid = await _seed_fresh_prompt_spool(db_session, printer.id, used=700)
        await self._stamp(db_session, sid)
        spool_tagless._reset_state()  # "restart": every in-memory ledger is gone
        monkeypatch.setattr(spool_tagless.printer_manager, "get_status", lambda pid: self._present_state())
        send = AsyncMock()
        n = await spool_tagless.rebroadcast_unresolved_tagless_prompts(db_session, send)
        assert n == 1
        payload = send.await_args.args[0]
        assert payload["type"] == "tagless_fresh_prompt" and payload["spool_id"] == sid

    async def test_unstamped_row_is_not_replayed(self, db_session, printer_factory, monkeypatch):
        printer = await printer_factory()
        await _seed_fresh_prompt_spool(db_session, printer.id, used=700)  # eligible but never asked
        monkeypatch.setattr(spool_tagless.printer_manager, "get_status", lambda pid: self._present_state())
        send = AsyncMock()
        assert await spool_tagless.rebroadcast_unresolved_tagless_prompts(db_session, send) == 0
        send.assert_not_awaited()

    @pytest.mark.parametrize("invalidate", ["spent", "archived", "tagged", "unbound"])
    async def test_snapshot_drops_rows_the_prompt_no_longer_applies_to(
        self, db_session, printer_factory, monkeypatch, invalidate
    ):
        printer = await printer_factory()
        sid = await _seed_fresh_prompt_spool(db_session, printer.id, used=700)
        await self._stamp(db_session, sid)
        spool = await db_session.get(Spool, sid)
        if invalidate == "spent":
            spool.spent_at = datetime.utcnow()  # W1 owns it silently, no prompt
        elif invalidate == "archived":
            spool.archived_at = datetime.utcnow()
        elif invalidate == "tagged":
            spool.tag_uid = _VALID_TAG  # RFID row — the tagless prompt never applies
        else:
            sa = await _assignment(db_session, printer.id)
            await db_session.delete(sa)
        await db_session.commit()
        monkeypatch.setattr(spool_tagless.printer_manager, "get_status", lambda pid: self._present_state())
        assert await spool_tagless.pending_fresh_prompts(db_session) == []

    async def test_snapshot_drops_when_slot_absent(self, db_session, printer_factory, monkeypatch):
        printer = await printer_factory()
        sid = await _seed_fresh_prompt_spool(db_session, printer.id, used=700)
        await self._stamp(db_session, sid)
        monkeypatch.setattr(spool_tagless.printer_manager, "get_status", lambda pid: None)  # printer gone
        assert await spool_tagless.pending_fresh_prompts(db_session) == []
        # The stamp is NOT cleared by a failed validation — an offline printer must
        # still prompt once it is back.
        assert await _pending_stamp(db_session, sid) is not None

    async def test_ram_set_is_gone(self):
        """No dual path: the in-memory unanswered set is deleted outright."""
        assert not hasattr(spool_tagless, "_fresh_prompt_unanswered")


# --- E1: shared generic-identity override ----------------------------------


class TestGenericIdentityOverride:
    """The ONE generic->specific substitution, consumed by the mint AND the wire
    resolver. Its mint-side behaviour is pinned by TestMintIdentityW4 above (which
    now runs through this helper); these cases pin the helper's own contract."""

    _DEFAULT = {
        "brand": "Bambu Lab",
        "material": "PETG",
        "subtype": "HF",
        "rgba": "000000FF",
        "slicer_filament": "GFG02",
        "nozzle_temp_min": 230,
        "nozzle_temp_max": 270,
    }

    async def test_generic_id_matching_fingerprint_overrides(self, db_session, env):
        env.settings["tagless_default_filament"] = json.dumps(self._DEFAULT)
        out = await spool_tagless.override_generic_identity(db_session, "GFG99", "PETG", "000000FF")
        assert out == {"slicer_filament": "GFG02", "nozzle_temp_min": 230, "nozzle_temp_max": 270}

    async def test_specific_id_is_never_overridden(self, db_session, env):
        env.settings["tagless_default_filament"] = json.dumps(self._DEFAULT)
        assert await spool_tagless.override_generic_identity(db_session, "GFG02", "PETG", "000000FF") is None
        assert await spool_tagless.override_generic_identity(db_session, "", "PETG", "000000FF") is None
        assert await spool_tagless.override_generic_identity(db_session, None, "PETG", "000000FF") is None

    async def test_non_matching_fingerprint_keeps_generic(self, db_session, env):
        env.settings["tagless_default_filament"] = json.dumps(self._DEFAULT)
        # Different material and far colour each veto the substitution.
        assert await spool_tagless.override_generic_identity(db_session, "GFL99", "PLA", "000000FF") is None
        assert await spool_tagless.override_generic_identity(db_session, "GFG99", "PETG", "FF0000FF") is None

    async def test_feature_off_or_default_without_id(self, db_session, env):
        env.settings["tagless_default_filament"] = ""  # operator turned it off
        assert await spool_tagless.override_generic_identity(db_session, "GFG99", "PETG", "000000FF") is None
        env.settings["tagless_default_filament"] = json.dumps(
            {"brand": "Bambu Lab", "material": "PETG", "rgba": "000000FF"}  # no slicer_filament
        )
        assert await spool_tagless.override_generic_identity(db_session, "GFG99", "PETG", "000000FF") is None


# --- E3: one-shot slot-identity reconcile ----------------------------------


class _FakeClient:
    """Client stub exposing only the AMS-write pre-flight the reconcile consults."""

    def __init__(self, refusal=None):
        self.refusal = refusal

    def ams_write_refusal(self, ams_id):
        return self.refusal


# --- F1: fresh-mint settle defer -------------------------------------------


class TestMintSettleDefer:
    """An insertion's FIRST push carries the slot config but not yet the tag — the
    firmware's own RFID read lands ~1 s later. Minting on that push creates the
    provisional row the tag read then hard-deletes ("hard-deleted on RFID takeover",
    3× on 2026-07-19). Fresh mints wait out _MINT_SETTLE_S; existing bindings don't."""

    def _gain(self, monkeypatch, age):
        monkeypatch.setattr("backend.app.services.ams_presence.recent_gain_age", lambda *a: age)

    async def test_bare_tray_first_mint_defers_without_burning_the_window(
        self, db_session, printer_factory, env, monkeypatch
    ):
        self._gain(monkeypatch, 2.0)
        printer = await printer_factory()
        assert await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare()) is False
        assert await _assignment(db_session, printer.id) is None
        env.apply.assert_not_awaited()
        assert (printer.id, 0, 0) not in spool_tagless._autoconfig_window  # window not burned
        # Settled → mints + pushes immediately (no wait for the retry cadence).
        self._gain(monkeypatch, spool_tagless._CONFIG_SETTLE_S + 1.0)
        assert await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare()) is True
        env.apply.assert_awaited_once()


class TestConfigSettleGate:
    """2026-07-25 CLOBBER PIN — a fresh Bambu-tagged roll dropped into a slot that still
    carries a surviving tagless binding reads BARE for ~1 s while the firmware runs its
    own RFID read. The re-push arm had NO settle defer (only fresh MINTS waited), so the
    config write landed inside that window, destroyed the RFID-detected state — which the
    firmware never retries — and left the slot as the tagless default with a phantom
    binding for six hours. Nothing may publish an identity into an unresolved slot."""

    def _gain(self, monkeypatch, age):
        monkeypatch.setattr("backend.app.services.ams_presence.recent_gain_age", lambda *a: age)

    def _unanswered(self, monkeypatch, unanswered, cycle_age=None):
        monkeypatch.setattr("backend.app.services.ams_presence.identity_unanswered", lambda *a: unanswered)
        monkeypatch.setattr("backend.app.services.ams_presence.last_physical_cycle_age", lambda *a: cycle_age)

    async def _track_slot(self, db_session, printer, env, monkeypatch):
        """Mint + bind our own ams_auto default on the slot, then reset the retry
        window — leaving exactly the tracked-slot RE-PUSH state the incident hit."""
        self._gain(monkeypatch, None)
        assert await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare()) is True
        env.apply.reset_mock()
        spool_tagless.clear_autoconfig_dedup(printer.id, 0, 0)

    async def test_tracked_slot_re_push_defers_inside_the_gain_window(
        self, db_session, printer_factory, env, monkeypatch
    ):
        printer = await printer_factory()
        await self._track_slot(db_session, printer, env, monkeypatch)

        self._gain(monkeypatch, 1.0)  # inserted 1 s ago — the firmware read is in flight
        assert await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare()) is False
        env.apply.assert_not_awaited()  # the clobber that made the phantom binding
        assert (printer.id, 0, 0) not in spool_tagless._autoconfig_window  # window not burned

        # Past the settle window the same slot re-pushes immediately — one delay, not a
        # stall, and no fresh row minted for the roll already tracked.
        self._gain(monkeypatch, spool_tagless._CONFIG_SETTLE_S + 1.0)
        assert await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare()) is True
        env.apply.assert_awaited_once()
        count = await db_session.scalar(select(func.count(Spool.id)).where(Spool.data_origin == "ams_auto"))
        assert count == 1

    async def test_unanswered_cycle_defers_then_fails_open_past_the_cap(
        self, db_session, printer_factory, env, monkeypatch
    ):
        # Second arm: the gain is old, but the farm still does not know WHAT is in the
        # slot (a qualified cycle nothing has answered). Publishing an identity there is
        # a guess that overwrites the real one.
        printer = await printer_factory()
        await self._track_slot(db_session, printer, env, monkeypatch)
        self._gain(monkeypatch, 300.0)

        self._unanswered(monkeypatch, True, cycle_age=300.0)
        assert await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare()) is False
        env.apply.assert_not_awaited()
        assert (printer.id, 0, 0) not in spool_tagless._autoconfig_window

        # Past _CONFIG_SETTLE_MAX_S no firmware read can still be running: fail OPEN so a
        # genuinely tagless roll (whose read can never succeed) is delayed once, not forever.
        self._unanswered(monkeypatch, True, cycle_age=spool_tagless._CONFIG_SETTLE_MAX_S + 1.0)
        assert await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare()) is True
        env.apply.assert_awaited_once()

    async def test_push_config_is_the_funnel(self, db_session, printer_factory, env, monkeypatch):
        # Every tagless config-write path goes through _push_config, so the gate is
        # enforced there too — a caller that reaches it while settling publishes nothing.
        printer = await printer_factory()
        sid = await _seed_assignment(db_session, printer.id, 0, 0, material="PETG", rgba="112233FF")
        spool = await db_session.get(Spool, sid)
        self._gain(monkeypatch, 1.0)
        assert await spool_tagless._push_config(db_session, spool, printer.id, 0, 0, _tray("PETG")) is False
        env.apply.assert_not_awaited()
        self._gain(monkeypatch, spool_tagless._CONFIG_SETTLE_S + 1.0)
        assert await spool_tagless._push_config(db_session, spool, printer.id, 0, 0, _tray("PETG")) is True
        env.apply.assert_awaited_once()

    async def test_settled_slot_with_no_history_is_never_gated(self, db_session, printer_factory, env, monkeypatch):
        # A restart / never-observed slot (no gain stamp, no pending question) reads as
        # settled: the gate can never wedge a slot it has no evidence about.
        printer = await printer_factory()
        self._gain(monkeypatch, None)
        self._unanswered(monkeypatch, False)
        assert await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare()) is True
        env.apply.assert_awaited_once()

    async def test_unreadable_printer_state_keeps_the_gate_on(self, db_session, printer_factory, env, monkeypatch):
        """The mid-print carve-out below fails SAFE: a printer whose state cannot be
        read (disconnected — ``env`` already returns None here) is NOT assumed
        mid-print, so the gate stays active and the wire stays protected."""
        printer = await printer_factory()
        await self._track_slot(db_session, printer, env, monkeypatch)
        self._gain(monkeypatch, 1.0)

        assert await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare()) is False
        env.apply.assert_not_awaited()

    def test_busy_read_failure_direction_is_stated_per_caller(self, monkeypatch):
        """One predicate, two safe directions: the idle-only identity reconcile resolves
        an unreadable state to BUSY (never write), the settle gate to NOT-mid-print (keep
        the gate on). Both mean "protect the wire"; only the polarity differs."""

        def _raise(pid):
            raise RuntimeError("offline")

        monkeypatch.setattr(spool_tagless.printer_manager, "get_status", _raise)
        assert spool_tagless._printer_busy(1) is True
        assert spool_tagless._printer_busy(1, on_error=False) is False


class TestMidPrintIsNotGated:
    """The gate protects the firmware's post-insert AUTO-READ window, and that window
    only exists while IDLE: mid-print insertions get no automatic RFID read and no
    retroactive read at FINISH (`bambu-ams-behavior/resources/ams-wire-behavior.md`
    §"Mid-print insertions are not auto-read", live-verified H2S fw 01.01.02.00).
    Gating there would protect nothing AND starve spool_recovery's forced bare-tray
    enrollment — a jam is exactly PAUSE + a freshly-gained slot — which doctrine rule 1
    forbids (never defer a recoverable state to a human)."""

    def _gain(self, monkeypatch, age):
        monkeypatch.setattr("backend.app.services.ams_presence.recent_gain_age", lambda *a: age)

    def _printer_state(self, monkeypatch, gcode_state):
        monkeypatch.setattr(spool_tagless.printer_manager, "get_status", lambda pid: SimpleNamespace(state=gcode_state))

    async def test_jam_recovery_enrollment_publishes_during_a_pause(
        self, db_session, printer_factory, env, monkeypatch
    ):
        """RECOVERY PIN — spool_recovery's forced sweep enrolling an operator-inserted
        backup spool mid-jam: printer PAUSEd, slot gained seconds ago. It must configure
        the slot NOW so the roll joins the firmware backup pool and the print can resume;
        deferring a recoverable state to a human is doctrine rule 1's whole prohibition.

        Gain age 8 s clears the pre-existing F1 mint settle (5 s, untouched by this wave)
        and sits deep INSIDE the 30 s config-settle window — so this pins the carve-out
        and nothing else."""
        printer = await printer_factory()
        self._printer_state(monkeypatch, "PAUSE")
        self._gain(monkeypatch, 8.0)

        assert (
            await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare(), force=True) is True
        )
        env.apply.assert_awaited_once()

    async def test_paused_re_push_of_a_tracked_slot_is_immediate(self, db_session, printer_factory, env, monkeypatch):
        """The other half of the jam flow: the backup spool is ALREADY our ams_auto row
        (enrolled on a previous pass) and only needs its config re-pushed. No mint is
        involved, so not even F1 applies — a 1 s-old gain publishes at once."""
        printer = await printer_factory()
        await _seed_assignment(db_session, printer.id, 0, 0, material="PETG", rgba="112233FF")
        self._printer_state(monkeypatch, "PAUSE")
        self._gain(monkeypatch, 1.0)

        assert (
            await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare(), force=True) is True
        )
        env.apply.assert_awaited_once()

    async def test_push_config_publishes_while_running_despite_an_unanswered_cycle(
        self, db_session, printer_factory, env, monkeypatch
    ):
        printer = await printer_factory()
        sid = await _seed_assignment(db_session, printer.id, 0, 0, material="PETG", rgba="112233FF")
        spool = await db_session.get(Spool, sid)
        self._printer_state(monkeypatch, "RUNNING")
        self._gain(monkeypatch, 1.0)
        monkeypatch.setattr("backend.app.services.ams_presence.identity_unanswered", lambda *a: True)
        monkeypatch.setattr("backend.app.services.ams_presence.last_physical_cycle_age", lambda *a: 5.0)

        assert await spool_tagless._push_config(db_session, spool, printer.id, 0, 0, _tray("PETG")) is True
        env.apply.assert_awaited_once()

    @pytest.mark.parametrize("gcode_state", ["IDLE", "FINISH"])
    async def test_idle_and_finish_are_still_gated(self, db_session, printer_factory, env, monkeypatch, gcode_state):
        """Control: the carve-out is RUNNING/PAUSE only — an idle printer between prints
        is exactly when the firmware DOES auto-read an insert."""
        printer = await printer_factory()
        self._printer_state(monkeypatch, gcode_state)
        self._gain(monkeypatch, 1.0)

        assert (
            await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare(), force=True)
            is False
        )
        env.apply.assert_not_awaited()


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


# --- loaded_at re-stampable FIFO ordinal (006-H2S) -------------------------


class TestLoadedAtStamp:
    """``loaded_at`` (the re-stampable FIFO ordinal) is stamped when a tagless roll
    first binds a slot (auto_assign / bare-tray auto-config) and RE-stamped when a
    slot-move re-binds an existing ledger row in place."""

    async def test_bare_tray_autoconfig_stamps_loaded_at(self, db_session, printer_factory, env):
        env.settings["tagless_default_filament"] = json.dumps(
            {"brand": "Bambu Lab", "material": "PETG", "subtype": "HF", "rgba": "000000FF"}
        )
        printer = await printer_factory()
        handled = await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare())
        assert handled is True
        sa = await _assignment(db_session, printer.id)
        spool = await db_session.get(Spool, sa.spool_id)
        assert spool.loaded_at is not None


# --- W1: the spent latch is decided by the RUNOUT, not by the row's origin ---


class TestBareTraySpentGuardOutranksTheOriginVeto:
    """2026-08-07, printer 4 tray 2 — the third lock on the deadlocked slot.

    ``maybe_autoconfigure_bare_tray`` checked the data-origin veto FIRST, so a spent
    ``rfid_auto`` row (spool 212, 1121.5 g on a 1000 g label) returned False before the
    spent+cycle escape could ever run: the replacement roll seated in that slot could not
    take the archive→unlink→default-mint→push transition even with a qualified physical
    cycle recorded. Spent-ness is decided by the runout, not by who minted the row.

    The veto itself is unchanged and still first for every LIVE row — that is what it was
    written for (never overwrite an operator's or the firmware's own identity).
    """

    async def _seed(self, db_session, printer_id, *, origin, spent):
        spool = Spool(
            material="PETG",
            rgba="000000FF",
            data_origin=origin,
            tag_uid=_VALID_TAG if origin == "rfid_auto" else None,
            spent_at=datetime.utcnow() if spent else None,
        )
        spool.k_profiles = []
        spool.assignments = []
        db_session.add(spool)
        await db_session.flush()
        db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=printer_id, ams_id=0, tray_id=0))
        await db_session.commit()
        return spool

    async def test_spent_rfid_row_with_a_cycle_takes_the_replacement_transition(self, db_session, printer_factory, env):
        printer = await printer_factory()
        spent = await self._seed(db_session, printer.id, origin="rfid_auto", spent=True)
        spool_tagless._pending_physical_cycles.add((printer.id, 0, 0))

        handled = await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare())

        assert handled is True
        await db_session.refresh(spent)
        assert spent.archived_at is not None  # the drained core retires, grams kept
        sa = await _assignment(db_session, printer.id)
        fresh = await db_session.get(Spool, sa.spool_id)
        assert fresh.id != spent.id
        assert fresh.spent_at is None and fresh.data_origin == "ams_auto"
        env.apply.assert_awaited_once()  # the fresh row's config goes out to the slot
        assert (printer.id, 0, 0) not in spool_tagless._pending_physical_cycles  # consumed exactly once

    async def test_spent_rfid_row_without_a_cycle_stays_latched(self, db_session, printer_factory, env):
        """The latch is not weakened by the reorder: no qualified physical cycle, no
        transition — and the retry window is still not burned by a latched slot."""
        printer = await printer_factory()
        spent = await self._seed(db_session, printer.id, origin="rfid_auto", spent=True)

        handled = await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare())

        assert handled is False
        await db_session.refresh(spent)
        assert spent.archived_at is None
        env.apply.assert_not_awaited()
        assert (printer.id, 0, 0) not in spool_tagless._autoconfig_window

    async def test_a_LIVE_rfid_row_is_still_vetoed_even_with_a_cycle(self, db_session, printer_factory, env):
        """The veto's actual job, unchanged: a live non-``ams_auto`` row is never
        overwritten, and a pending physical cycle is not a licence to overwrite one."""
        printer = await printer_factory()
        live = await self._seed(db_session, printer.id, origin="rfid_auto", spent=False)
        spool_tagless._pending_physical_cycles.add((printer.id, 0, 0))

        handled = await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare())

        assert handled is False
        await db_session.refresh(live)
        assert live.archived_at is None
        sa = await _assignment(db_session, printer.id)
        assert sa.spool_id == live.id  # untouched
        env.apply.assert_not_awaited()
        assert (printer.id, 0, 0) in spool_tagless._pending_physical_cycles  # not consumed either


# --- W1: a SPENT binding's release SIGNAL survives any tag-ness ---------------


class TestSpentCycleSurvivesAnyTagness:
    """2026-08-07 #2, spool 226 / 001-H2S slot 1 — the sibling lock of the class above.

    Same root shape, one lane over: ``_maybe_prompt_fresh_roll`` checked TAG-NESS before
    spent-ness, and it runs inside the very await that ARMS the pending cycle
    (``note_physical_cycle``). A spent binding whose row carries an RFID identity therefore
    destroyed its own release signal on the way in, so neither consumer — the pipeline's
    ``REPLACE_SPENT`` arm nor ``maybe_autoconfigure_bare_tray``'s spent gate — ever saw a
    cycle to spend, and the class above's reorder could not fire. Spent-ness is decided by
    the RUNOUT, never by who minted the row.

    Every case drives the REAL entry point ``note_physical_cycle``. Seeding
    ``_pending_physical_cycles`` by hand is exactly how the bug shipped green: the existing
    spent-swap cases start one await too late to see the arming path destroy it.
    """

    async def _seed_tagged(self, db_session, printer_id, *, spent, used=700):
        """A slot bound to an RFID-identified row (both tag fields), 70 % consumed.

        Past :data:`_FRESH_ROLL_PROMPT_USED_FRAC`, so a TAGLESS row in the same state
        would prompt — any silence here is the tag-ness/spent decision, not the threshold.
        """
        sid = await _seed_assignment(
            db_session, printer_id, 0, 0, material="PETG", rgba="112233FF", tag_uid=_VALID_TAG, spent=spent
        )
        spool = await db_session.get(Spool, sid)
        spool.tray_uuid = "1" * 32
        spool.label_weight = 1000
        spool.weight_used = float(used)
        await db_session.commit()
        assert not spool_tagless.is_tagless_spool(spool)  # the case's whole premise
        return spool

    async def test_spent_tagged_row_leaves_the_cycle(self, db_session, printer_factory, env, sessions):
        """The shipped bug's exact repro: the cycle must still be there afterwards.

        ``env`` leaves the live printer state None, so the presence re-check would DISCARD
        if the spent path fell through to it — the surviving cycle therefore also pins that
        the spent return sits ABOVE that check (its consumers re-verify presence
        themselves: the bare-tray gate needs ``tray_present``, ``REPLACE_SPENT`` its own
        tray check).
        """
        printer = await printer_factory()
        await self._seed_tagged(db_session, printer.id, spent=True)

        await spool_tagless.note_physical_cycle(printer.id, 0, 0)

        assert (printer.id, 0, 0) in spool_tagless._pending_physical_cycles  # left for W1
        env.ws.assert_not_awaited()  # spent -> silent, never a fresh-roll prompt

    async def test_non_spent_tagged_row_discards_the_cycle(self, db_session, printer_factory, env, sessions, seated):
        """Today's correct behaviour, preserved by the reorder: a LIVE tagged row has no
        tagless prompt to raise and no spent latch to release, so its cycle is spent right
        here. The tray is SEATED and past the threshold, so the discard can only be the
        tag-ness veto."""
        printer = await printer_factory()
        await self._seed_tagged(db_session, printer.id, spent=False)

        await spool_tagless.note_physical_cycle(printer.id, 0, 0)

        assert (printer.id, 0, 0) not in spool_tagless._pending_physical_cycles
        env.ws.assert_not_awaited()

    async def test_cycle_survives_and_releases_the_latch(self, db_session, printer_factory, env, sessions):
        """End-to-end: a signal armed by the REAL entry point reaches the real consumer.

        A spent TAGGED row plus the fresh roll physically seated in the slot (bare tray) —
        the incident's exact state. The surviving cycle is the only thing that lets
        ``maybe_autoconfigure_bare_tray`` take the archive → unlink → default-mint → push
        transition instead of leaving the slot deadlocked.
        """
        printer = await printer_factory()
        spent = await self._seed_tagged(db_session, printer.id, spent=True)
        spent_id = spent.id

        await spool_tagless.note_physical_cycle(printer.id, 0, 0)
        handled = await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, _bare())

        assert handled is True
        await db_session.refresh(spent)
        assert spent.archived_at is not None  # the drained core retires...
        rows = await db_session.scalar(select(func.count(Spool.id)).where(Spool.id == spent_id))
        assert rows == 1  # ...and is NEVER deleted — the ledger row and its grams stay

        sa = await _assignment(db_session, printer.id)
        fresh = await db_session.get(Spool, sa.spool_id)
        assert fresh.id != spent_id
        assert fresh.spent_at is None and fresh.data_origin == "ams_auto"
        assert spool_tagless.is_tagless_spool(fresh)  # the replacement is an untagged mint
        assert fresh.loaded_at is not None  # FIFO ordinal stamped by the binding writer
        env.apply.assert_awaited_once()  # the fresh row's identity goes out to the slot
        assert (printer.id, 0, 0) not in spool_tagless._pending_physical_cycles  # consumed exactly once
