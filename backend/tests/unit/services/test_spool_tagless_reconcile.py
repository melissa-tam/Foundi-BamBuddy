"""Durable slot-config reconcile — ``spool_tagless.reconcile_slot_config``.

2026-07-24 incident: three slots (printer 3 AMS0-T0/T2, printer 5 AMS0-T3) sat with
minted+assigned ledger rows against a BLANK firmware tray config for 15+ minutes.
Their ``ams_filament_setting`` writes were refused by the wire-safety gates and
nothing retried — every re-push trigger hangs off ``main.on_ams_change``, which
``bambu_mqtt`` fires only on an AMS state-hash CHANGE, so a settled AMS silenced the
retry entirely. These tests drive the scheduler-tick backstop with NO AMS callback
anywhere, and pin that it re-drives the two owning service functions without
loosening any of their guards.

Fixture/mocking style mirrors ``test_spool_tagless.py`` (settings dict + patched
``apply_spool_to_slot_via_mqtt``) and ``test_farm_stall.py`` (injected manager/clock).
"""

import json
import logging
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spool_k_profile import SpoolKProfile
from backend.app.services import ams_presence, spool_tag_matcher, spool_tagless

pytestmark = pytest.mark.asyncio

_BAMBU_UUID = "A1B2C3D4" * 4  # non-zero tray_uuid → is_bambu_tag() True
_BAMBU_TAG = "AABBCCDD11223344"
_ZERO_TAG = "0" * 16
_ZERO_UUID = "0" * 32

_T0 = 10_000.0  # arbitrary monotonic origin for the injected clock
_PAST_WINDOW = _T0 + spool_tagless._RECONCILE_MIN_INTERVAL_S + 1.0


@pytest.fixture(autouse=True)
def _clean_state():
    spool_tagless._reset_state()
    ams_presence._reset_state()  # the owed-identify arm reads its change-evidence ledgers
    spool_tag_matcher._kdrift_window.reset()  # the K-drift arm rides this window
    yield
    spool_tagless._reset_state()
    ams_presence._reset_state()
    spool_tag_matcher._kdrift_window.reset()


@pytest.fixture
def env(monkeypatch):
    """Settings dict, patched MQTT config push, WS broadcast, and printer client.

    Mirrors ``test_spool_tagless.env``: leave a settings key unset to exercise the
    code's default (auto_add on, schema default filament, Spoolman off).
    """
    settings: dict[str, str] = {}

    async def fake_get_setting(db, key):
        return settings.get(key)

    monkeypatch.setattr("backend.app.api.routes.settings.get_setting", fake_get_setting)

    apply = AsyncMock(return_value=True)
    monkeypatch.setattr("backend.app.services.spool_tagless.apply_spool_to_slot_via_mqtt", apply)

    ws = AsyncMock()
    monkeypatch.setattr(spool_tagless.ws_manager, "broadcast", ws)

    client = _FakeClient()
    monkeypatch.setattr(spool_tagless.printer_manager, "get_client", lambda pid: client)

    return SimpleNamespace(settings=settings, apply=apply, ws=ws, client=client)


class _FakeClient:
    """Client stub capturing the only publish the K-drift arm makes."""

    def __init__(self):
        self.cali_calls: list[dict] = []

    def extrusion_cali_sel(self, **kw):
        self.cali_calls.append(kw)
        return True


class _FakeManager:
    """Injectable ``printer_manager`` stand-in — ``get_status`` plus the report request
    the bound-presence arm makes (paced + connected-gated in the real client, so the
    lane only ever asks)."""

    def __init__(self, states: dict[int, object], *, pushall_ok: bool = True):
        self._states = states
        self._pushall_ok = pushall_ok
        self.status_calls: list[int] = []
        self.pushall_calls: list[tuple[int, str]] = []

    def get_status(self, printer_id: int):
        self.status_calls.append(printer_id)
        return self._states.get(printer_id)

    def request_evidence_pushall(self, printer_id: int, reason: str) -> bool:
        self.pushall_calls.append((printer_id, reason))
        return self._pushall_ok


def _bare_tray(tray_id=0, *, state=10):
    """A spool physically seated with nothing configured (the incident shape)."""
    return {
        "id": tray_id,
        "state": state,
        "tray_type": "",
        "tray_sub_brands": "",
        "tray_color": "",
        "tray_info_idx": "",
        "tag_uid": _ZERO_TAG,
        "tray_uuid": _ZERO_UUID,
    }


def _unknown_tray(tray_id=0):
    """A merged tray that asserts NOTHING about content — the boot-forgotten / reduced
    mid-print shape. Tri-state presence is None (unknown), which fails OPEN everywhere:
    no consumer skips it, no release fires, and nothing says a word."""
    return {"id": tray_id, "state": 9}


def _cleared_tray(tray_id=0):
    """The wire's asserted-empty shape — presence resolves False. Under a LIVE binding
    that is the printer-1 shape (2026-08-09): the merged lane holds the release evidence
    and the binding still stands, so the deciding RAW lane never saw it."""
    return {"id": tray_id, "state": 9, "tray_type": ""}


def _a1_tray(tray_id=0):
    """The A1/P1S dialect: ``state`` is the constant 3, so presence is PERMANENTLY
    unknown for a configured slot. No report will ever settle it."""
    return {"id": tray_id, "state": 3, "tray_type": "PLA", "tray_color": "112233FF"}


def _present_tray(tray_id=0):
    """A seated, configured tray — presence True. Non-empty ``tray_type`` keeps the
    bare-tray arm out, and the absent Bambu tag keeps the K-drift arm out, so this
    isolates the presence arm."""
    return {
        "id": tray_id,
        "state": 11,
        "tray_type": "PETG",
        "tray_color": "112233FF",
        "tray_info_idx": "GFG02",
        "tag_uid": _ZERO_TAG,
        "tray_uuid": _ZERO_UUID,
    }


def _tagged_tray(tray_id=0, *, cali_idx=3):
    """A configured Bambu-RFID tray (drives the K-drift arm)."""
    return {
        "id": tray_id,
        "state": 11,
        "tray_type": "PETG",
        "tray_sub_brands": "PETG HF",
        "tray_color": "112233FF",
        "tray_info_idx": "GFG02",
        "tag_uid": _BAMBU_TAG,
        "tray_uuid": _BAMBU_UUID,
        "cali_idx": cali_idx,
    }


def _state(trays, *, ams_id=0, wrapped=False, gcode_state="IDLE", tray_now=255):
    """Live merged state carrying one AMS unit. ``wrapped`` uses the dict-wrapper shape.

    ``tray_now`` defaults to 255 (nothing engaged) so the owed-identify arm's
    engaged-filament pre-check reads as clear; engaged cases pass a real tray id.
    """
    ams = [{"id": ams_id, "tray": trays}]
    return SimpleNamespace(
        state=gcode_state,
        raw_data={"ams": {"ams": ams} if wrapped else ams},
        ams_extruder_map=None,
        nozzles=[],
        tray_now=tray_now,
        ams_status_main=0,
    )


async def _seed_spool(
    db,
    *,
    data_origin="ams_auto",
    spent=False,
    tag_uid=None,
    rgba="000000FF",
    slicer_filament=None,
    nozzle_temp_min=None,
    nozzle_temp_max=None,
):
    spool = Spool(
        material="PETG",
        rgba=rgba,
        slicer_filament=slicer_filament,
        nozzle_temp_min=nozzle_temp_min,
        nozzle_temp_max=nozzle_temp_max,
        data_origin=data_origin,
        tag_uid=tag_uid,
        tray_uuid=_BAMBU_UUID if tag_uid else None,
        spent_at=datetime.utcnow() if spent else None,
    )
    spool.k_profiles = []
    spool.assignments = []
    db.add(spool)
    await db.flush()
    return spool


async def _seed_assignment(db, printer_id, ams_id=0, tray_id=0, **spool_kw):
    spool = await _seed_spool(db, **spool_kw)
    db.add(
        SpoolAssignment(
            spool_id=spool.id,
            printer_id=printer_id,
            ams_id=ams_id,
            tray_id=tray_id,
            fingerprint_color="000000FF",
            fingerprint_type="PETG",
        )
    )
    await db.commit()
    return spool


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


# --- A: the incident pin ---------------------------------------------------


class TestIncidentPin:
    async def test_settled_ams_bare_slot_is_republished(self, db_session, printer_factory, env):
        """2026-07-24 INCIDENT PIN — a minted+assigned slot whose config write was
        refused, on an AMS that has since settled (so ``on_ams_change`` never fires
        again). No callback is involved anywhere in this test: the scheduler-tick
        reconcile alone must re-push the config."""
        printer = await printer_factory()
        spool = await _seed_assignment(db_session, printer.id, 0, 2)
        manager = _FakeManager({printer.id: _state([_bare_tray(2)])})
        assert (printer.id, 0, 2) not in spool_tagless._autoconfig_window  # window clear

        pushed = await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0)

        assert pushed == 1
        env.apply.assert_awaited_once()
        kw = env.apply.await_args.kwargs
        assert (kw["printer_id"], kw["ams_id"], kw["tray_id"]) == (printer.id, 0, 2)
        assert kw["spool"].id == spool.id  # re-pushed for the row already bound

    async def test_dict_wrapped_ams_shape(self, db_session, printer_factory, env):
        """The merged AMS also arrives as ``{"ams": {"ams": [...]}}`` — same outcome."""
        printer = await printer_factory()
        await _seed_assignment(db_session, printer.id, 0, 0)
        manager = _FakeManager({printer.id: _state([_bare_tray()], wrapped=True)})
        assert await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0) == 1
        env.apply.assert_awaited_once()


# --- C/D/E: the bare arm keeps every guard ---------------------------------


class TestBareArmGuards:
    async def test_unbound_bare_slot_mints_and_pushes(self, db_session, printer_factory, env, monkeypatch):
        """C — no assignment yet: the reconcile reaches the mint path (settle window
        controlled, as in ``TestMintSettleDefer``)."""
        monkeypatch.setattr("backend.app.services.ams_presence.recent_gain_age", lambda *a: None)
        env.settings["tagless_default_filament"] = json.dumps(
            {"brand": "Bambu Lab", "material": "PETG", "subtype": "HF", "rgba": "000000FF"}
        )
        printer = await printer_factory()
        manager = _FakeManager({printer.id: _state([_bare_tray()])})

        assert await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0) == 1

        sa = await _assignment(db_session, printer.id)
        assert sa is not None and sa.spool.data_origin == "ams_auto"
        env.apply.assert_awaited_once()

    async def test_operator_bound_slot_never_republished(self, db_session, printer_factory, env):
        """D — an operator/RFID-bound slot is NOT ours to overwrite: a config write
        racing a firmware tag read is the 2026-07-18 HMS 0700_0081 class."""
        printer = await printer_factory()
        await _seed_assignment(db_session, printer.id, data_origin="manual")
        manager = _FakeManager({printer.id: _state([_bare_tray()])})

        assert await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0) == 0
        env.apply.assert_not_awaited()

    async def test_spent_row_stays_latched(self, db_session, printer_factory, env):
        """E — W1: a spent ams_auto binding is the durable 'ran dry' latch; only a
        qualified physical cycle releases it, never a reconcile pass."""
        printer = await printer_factory()
        await _seed_assignment(db_session, printer.id, spent=True)
        manager = _FakeManager({printer.id: _state([_bare_tray()])})

        assert await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0) == 0
        env.apply.assert_not_awaited()


# --- F: hardware-state defers do not burn the per-slot window ---------------


class TestDeferredSlot:
    @pytest.mark.parametrize("gate", ["identify_in_flight", "unit_drying"])
    async def test_defer_leaves_window_unburned_next_pass_publishes(
        self, db_session, printer_factory, env, monkeypatch, gate
    ):
        """A slot deferred by an in-flight identify or a drying unit publishes
        nothing this pass and does NOT consume its retry window — the next pass
        (gate cleared) publishes immediately instead of waiting out the cadence."""
        blocked = {"v": True}
        monkeypatch.setattr(f"backend.app.services.ams_presence.{gate}", lambda *a: blocked["v"])
        printer = await printer_factory()
        await _seed_assignment(db_session, printer.id)
        manager = _FakeManager({printer.id: _state([_bare_tray()])})

        assert await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0) == 0
        env.apply.assert_not_awaited()
        assert (printer.id, 0, 0) not in spool_tagless._autoconfig_window  # window not burned

        blocked["v"] = False
        assert await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_PAST_WINDOW) == 1
        env.apply.assert_awaited_once()


# --- F2: busy-AMS backoff ---------------------------------------------------


class TestBusyBackoff:
    """2026-07-27 (002-H2S AMS0-T1): four hours of 30-second config re-pushes — 998
    log lines — into a busy AMS that silently drops every one. The lane keeps the
    OCCASION on a printing/paused printer but skips the PUBLISH."""

    @pytest.mark.parametrize("gcode_state", ["RUNNING", "PAUSE"])
    async def test_busy_printer_skips_the_bare_tray_publish(self, db_session, printer_factory, env, gcode_state):
        printer = await printer_factory()
        await _seed_assignment(db_session, printer.id)
        manager = _FakeManager({printer.id: _state([_bare_tray()], gcode_state=gcode_state)})

        assert await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0) == 0
        env.apply.assert_not_awaited()

    async def test_the_retry_window_is_left_unburned(self, db_session, printer_factory, env):
        """A skipped publish must not consume the slot's retry window: the FIRST pass
        after the printer settles re-pushes immediately instead of waiting out the
        cadence (same contract as the identify/drying defers)."""
        printer = await printer_factory()
        await _seed_assignment(db_session, printer.id)
        busy = _FakeManager({printer.id: _state([_bare_tray()], gcode_state="RUNNING")})

        assert await spool_tagless.reconcile_slot_config(db_session, manager=busy, now=_T0) == 0
        assert (printer.id, 0, 0) not in spool_tagless._autoconfig_window

        idle = _FakeManager({printer.id: _state([_bare_tray()])})
        assert await spool_tagless.reconcile_slot_config(db_session, manager=idle, now=_PAST_WINDOW) == 1
        env.apply.assert_awaited_once()

    @pytest.mark.parametrize("gcode_state", ["IDLE", "FINISH", "FAILED", "PREPARE"])
    async def test_settled_and_pre_print_states_still_publish(self, db_session, printer_factory, env, gcode_state):
        """Only RUNNING/PAUSE are the ignoring states — nothing else is held back
        (PREPARE has not engaged the AMS yet, and the callee owns its own guards)."""
        printer = await printer_factory()
        await _seed_assignment(db_session, printer.id)
        manager = _FakeManager({printer.id: _state([_bare_tray()], gcode_state=gcode_state)})

        assert await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0) == 1
        env.apply.assert_awaited_once()


# --- G/H/I: pass-level gates ------------------------------------------------


class TestPassGates:
    async def test_interval_throttle(self, db_session, printer_factory, env):
        """G — a second call inside ``_RECONCILE_MIN_INTERVAL_S`` returns 0 without
        walking the fleet at all; a call past the window works again."""
        printer = await printer_factory()
        await _seed_assignment(db_session, printer.id)
        manager = _FakeManager({printer.id: _state([_bare_tray()])})

        assert await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0) == 1
        walked = len(manager.status_calls)

        inside = _T0 + spool_tagless._RECONCILE_MIN_INTERVAL_S - 0.1
        assert await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=inside) == 0
        assert len(manager.status_calls) == walked  # returned before walking the fleet
        assert env.apply.await_count == 1

        # Past the window the slot's own _AUTOCONFIG_RETRY_S still applies, so clear
        # it to prove the PASS ran (the throttle under test is the pass-level one).
        spool_tagless.clear_autoconfig_dedup(printer.id, 0, 0)
        assert await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_PAST_WINDOW) == 1
        assert env.apply.await_count == 2

    async def test_spoolman_mode_owns_the_slots(self, db_session, printer_factory, env):
        """H — in Spoolman mode the AMS slots are Spoolman's; the lane stays out."""
        env.settings["spoolman_enabled"] = "true"
        printer = await printer_factory()
        await _seed_assignment(db_session, printer.id)
        manager = _FakeManager({printer.id: _state([_bare_tray()])})

        assert await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0) == 0
        assert manager.status_calls == []
        env.apply.assert_not_awaited()

    async def test_disconnected_printer_skipped(self, db_session, printer_factory, env):
        """I — no live state (disconnected / never connected) → skipped cleanly."""
        printer = await printer_factory()
        await _seed_assignment(db_session, printer.id)
        manager = _FakeManager({})  # get_status → None

        assert await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0) == 0
        assert manager.status_calls == [printer.id]
        env.apply.assert_not_awaited()

    async def test_malformed_trays_are_skipped(self, db_session, printer_factory, env):
        """Defensive walk: a non-dict unit/tray or a tray with no id never raises."""
        printer = await printer_factory()
        state = SimpleNamespace(
            state="IDLE",
            raw_data={"ams": ["not-a-unit", {"id": "x", "tray": []}, {"id": 0, "tray": ["nope", {}]}]},
            ams_extruder_map=None,
            nozzles=[],
        )
        manager = _FakeManager({printer.id: state})

        assert await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0) == 0
        env.apply.assert_not_awaited()


# --- J: the K-drift arm -----------------------------------------------------


class TestKDriftArm:
    async def test_drifted_calibration_republished(self, db_session, printer_factory, env):
        """J — a Bambu-tagged bound slot whose live ``cali_idx`` differs from the
        stored K-profile gets ``extrusion_cali_sel`` re-published. That publish is
        fire-and-forget at its own call site, so this tick is its durable retry."""
        printer = await printer_factory()
        spool = await _seed_assignment(db_session, printer.id, tag_uid=_BAMBU_TAG, data_origin="rfid_auto")
        db_session.add(
            SpoolKProfile(
                spool_id=spool.id,
                printer_id=printer.id,
                extruder=0,
                nozzle_diameter="0.4",
                k_value=0.02,
                cali_idx=7,
            )
        )
        await db_session.commit()
        manager = _FakeManager({printer.id: _state([_tagged_tray(cali_idx=3)])})

        assert await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0) == 1

        assert len(env.client.cali_calls) == 1
        assert env.client.cali_calls[0]["cali_idx"] == 7
        assert (env.client.cali_calls[0]["ams_id"], env.client.cali_calls[0]["tray_id"]) == (0, 0)
        env.apply.assert_not_awaited()  # tagged slot: never a filament-setting write

    async def test_converged_calibration_is_silent(self, db_session, printer_factory, env):
        """No drift (live cali_idx == stored) → nothing published."""
        printer = await printer_factory()
        spool = await _seed_assignment(db_session, printer.id, tag_uid=_BAMBU_TAG, data_origin="rfid_auto")
        db_session.add(
            SpoolKProfile(
                spool_id=spool.id,
                printer_id=printer.id,
                extruder=0,
                nozzle_diameter="0.4",
                k_value=0.02,
                cali_idx=7,
            )
        )
        await db_session.commit()
        manager = _FakeManager({printer.id: _state([_tagged_tray(cali_idx=7)])})

        assert await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0) == 0
        assert env.client.cali_calls == []


# --- K: the owed-discovery-read arm ----------------------------------------


class _IdentifyClient:
    """Client stub for the identify arm — wire-safe by default, records every read."""

    def __init__(self, *, refusal=None, drying=False):
        self.refusal = refusal
        self.drying = drying
        self.reads: list[tuple[int, int]] = []

    def ams_write_refusal(self, ams_id):
        return self.refusal

    def ams_unit_drying(self, ams_id):
        return self.drying

    def ams_refresh_tray(self, ams_id, tray_id):
        self.reads.append((ams_id, tray_id))
        return (True, "ok")

    def extrusion_cali_sel(self, **kw):
        return True


class TestOwedIdentifyArm:
    """2026-07-25 INCIDENT PIN — a slot the farm KNEW had physically changed sat
    unidentified for six hours. ``identify_needed`` returned "discovery" the whole time,
    but both event-driven lanes refused it: the idle-gain lane only fires on the gain
    itself, and the terminal sweep's engaged-filament pre-check defers every slot while
    filament sits in the extruder path — which on a continuously-loaded printer is
    always. The scheduler-tick walk is the drain that has no such dependency."""

    def _wire(self, monkeypatch, state, *, client=None):
        client = client or _IdentifyClient()
        monkeypatch.setattr(ams_presence.printer_manager, "get_client", lambda pid: client)
        monkeypatch.setattr(ams_presence.printer_manager, "get_status", lambda pid: state)
        return client

    def _arm_cycle(self, printer_id, ams_id=0, tray_id=0):
        """An unanswered QUALIFIED physical cycle — somebody swapped a roll here and
        nothing has established what it is."""
        ams_presence._physical_cycle_at[(printer_id, ams_id, tray_id)] = ams_presence.time.monotonic()

    async def test_owed_read_is_commanded_and_the_config_write_waits_for_it(
        self, db_session, printer_factory, env, monkeypatch
    ):
        """Both halves of the fix in one pass: an unidentified slot is READ, and it is
        NOT configured meanwhile — publishing an identity into a slot whose identity is
        an open question is the clobber that cost 6 h of phantom binding."""
        printer = await printer_factory()
        await _seed_assignment(db_session, printer.id)
        state = _state([_bare_tray()])
        client = self._wire(monkeypatch, state)
        self._arm_cycle(printer.id)
        manager = _FakeManager({printer.id: state})

        pushed = await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0)

        assert client.reads == [(0, 0)]  # the owed discovery read finally goes out
        assert pushed == 0
        env.apply.assert_not_awaited()  # nothing published into the unresolved slot
        assert (printer.id, 0, 0) not in spool_tagless._autoconfig_window  # window not burned

    async def test_engaged_filament_defers_and_preserves_the_evidence(
        self, db_session, printer_factory, env, monkeypatch
    ):
        printer = await printer_factory()
        await _seed_assignment(db_session, printer.id)
        state = _state([_bare_tray()], tray_now=1)  # filament engaged — the client would refuse
        client = self._wire(monkeypatch, state)
        self._arm_cycle(printer.id)
        manager = _FakeManager({printer.id: state})

        await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0)

        assert client.reads == []
        assert ams_presence._unanswered_cycle(printer.id, 0, 0) is True  # retried next pass

    async def test_printing_printer_is_never_read(self, db_session, printer_factory, env, monkeypatch):
        printer = await printer_factory()
        await _seed_assignment(db_session, printer.id)
        state = _state([_bare_tray()], gcode_state="RUNNING")
        client = self._wire(monkeypatch, state)
        self._arm_cycle(printer.id)
        manager = _FakeManager({printer.id: state})

        await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0)

        assert client.reads == []

    async def test_client_refusal_defers(self, db_session, printer_factory, env, monkeypatch):
        printer = await printer_factory()
        await _seed_assignment(db_session, printer.id)
        state = _state([_bare_tray()])
        client = self._wire(monkeypatch, state, client=_IdentifyClient(refusal="AMS is drying"))
        self._arm_cycle(printer.id)
        manager = _FakeManager({printer.id: state})

        await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0)

        assert client.reads == []
        assert ams_presence._unanswered_cycle(printer.id, 0, 0) is True

    async def test_at_most_one_read_per_printer_per_pass(self, db_session, printer_factory, env, monkeypatch):
        """The client's per-printer identify gate would refuse the rest anyway — and a
        lane that provokes its own refusal WARNINGs is the noise this fork keeps out."""
        printer = await printer_factory()
        await _seed_assignment(db_session, printer.id, 0, 0)
        await _seed_assignment(db_session, printer.id, 0, 1)
        state = _state([_bare_tray(0), _bare_tray(1)])
        client = self._wire(monkeypatch, state)
        self._arm_cycle(printer.id, 0, 0)
        self._arm_cycle(printer.id, 0, 1)
        manager = _FakeManager({printer.id: state})

        await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0)

        assert len(client.reads) == 1

    async def test_untouched_tagless_slot_is_never_read(self, db_session, printer_factory, env, monkeypatch):
        """DOCTRINE GUARD: no physical cycle ⇒ no read. A commanded RFID read on an
        untouched tagless slot can only fail, and that failure is the standing
        0700_2X00_0001_0081 this fork spent a wave eliminating. The config re-push still
        happens — the slot's identity was never in question."""
        printer = await printer_factory()
        await _seed_assignment(db_session, printer.id)
        state = _state([_bare_tray()])
        client = self._wire(monkeypatch, state)
        manager = _FakeManager({printer.id: state})

        assert await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0) == 1
        assert client.reads == []

    async def test_tagged_slot_gets_no_refresh_from_this_lane(self, db_session, printer_factory, env, monkeypatch):
        """DOCTRINE GUARD: ``rfid_refresh`` is between-prints policy (the terminal
        sweep's). A tagged slot yields it on EVERY evaluation, so honouring it on a 20 s
        cadence would re-flap every tagged tray in the fleet forever."""
        printer = await printer_factory()
        await _seed_assignment(db_session, printer.id, tag_uid=_BAMBU_TAG, data_origin="rfid_auto")
        state = _state([_tagged_tray()])
        client = self._wire(monkeypatch, state)
        manager = _FakeManager({printer.id: state})

        await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0)

        assert client.reads == []


# --- L: the bound-but-presence-stale arm ------------------------------------


class TestBoundPresenceStaleArm:
    """A binding claims WHERE a roll is (doctrine rule 9), so it is only as good as the
    presence signal that can contradict it. TWO readings leave the claim unchecked and
    both are stale: UNKNOWN (nothing asserts anything, so every consumer fails open) and
    ASSERTED-EMPTY UNDER A LIVE BINDING (the merged lane has the release evidence and the
    binding still stands — printer 1, 2026-08-09: four bound slots merged at state 9, two
    asserting ``tray_type: ""``, zero releases in two days, because H2S omits a stable-
    empty tray from its incrementals and nothing ever asked for a full report).

    The arm ASKS UNTIL ANSWERED, on a backoff ladder within one episode — an episode
    being one continuous run of the same reading on the same binding. It ends on a
    presence VALUE change or a binding change, never on a timer. The OPERATOR is told
    once, at the third ask, once the machine has had every chance the ladder allows."""

    _PAST = _T0 + spool_tagless._BOUND_PRESENCE_STALE_AFTER_S + 1.0

    @staticmethod
    def _rungs() -> list[float]:
        """Ages (from episode open) at which the first four asks are due."""
        due = spool_tagless._BOUND_PRESENCE_STALE_AFTER_S
        out = [due]
        for gap in (*spool_tagless._PRESENCE_ASK_GAPS_S, spool_tagless._PRESENCE_ASK_INTERVAL_S):
            due += gap
            out.append(due)
        return out

    @pytest.mark.parametrize(
        ("shape", "tray"),
        [("unknown", _unknown_tray(1)), ("asserted_empty", _cleared_tray(1))],
    )
    async def test_a_persistent_stale_reading_asks_the_printer_then_flags_the_slot(
        self, db_session, printer_factory, env, shape, tray
    ):
        printer = await printer_factory(name="003-H2S")
        await _seed_assignment(db_session, printer.id, 0, 1)
        manager = _FakeManager({printer.id: _state([tray])})

        await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0)
        assert manager.pushall_calls == [], "first sighting only opens the episode"
        env.ws.assert_not_awaited()

        rungs = self._rungs()
        # Asks 1 and 2 are quiet: the machine may simply not have answered yet.
        for rung in rungs[:2]:
            await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0 + rung + 1.0)
        assert manager.pushall_calls == [(printer.id, "bound_presence_unknown")] * 2
        env.ws.assert_not_awaited()

        # Ask 3 escalates to the human, once.
        await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0 + rungs[2] + 1.0)
        assert len(manager.pushall_calls) == 3
        payload = env.ws.await_args.args[0]
        assert payload["type"] == "slot_standing_unknown"
        assert payload["case"] == "bound_presence_unknown"
        assert (payload["printer_id"], payload["ams_id"], payload["tray_id"]) == (printer.id, 0, 1)
        assert payload["printer_name"] == "003-H2S"

    async def test_the_asks_follow_the_backoff_ladder_and_never_the_pass_cadence(
        self, db_session, printer_factory, env
    ):
        """Between rungs the pass runs and asks NOTHING — the ladder paces the requests,
        not the reconcile tick."""
        printer = await printer_factory()
        await _seed_assignment(db_session, printer.id, 0, 1)
        manager = _FakeManager({printer.id: _state([_cleared_tray(1)])})

        # Passes are spaced past the lane's own _RECONCILE_MIN_INTERVAL_S floor, so what
        # is being measured here is the ASK ladder and not that floor.
        gap = spool_tagless._RECONCILE_MIN_INTERVAL_S + 10.0
        await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0)
        for expected, rung in enumerate(self._rungs(), start=1):
            # A pass just BEFORE the rung is due changes nothing…
            await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0 + rung - gap)
            assert len(manager.pushall_calls) == expected - 1
            # …and the rung itself buys exactly one request, however often the lane runs.
            await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0 + rung)
            await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0 + rung + gap)
            assert len(manager.pushall_calls) == expected

    async def test_the_a1_family_constant_state_costs_one_toast_per_episode(self, db_session, printer_factory, env):
        """A1/P1S report a constant ``state=3``, so presence there is unknowable BY
        DIALECT and no report can ever settle it. The quiet hourly asks continue (they
        cost one report each and cannot be distinguished, from inside, from a printer
        that is about to start answering), but the OPERATOR is told exactly once."""
        printer = await printer_factory(model="A1")
        await _seed_assignment(db_session, printer.id, 0, 1)
        manager = _FakeManager({printer.id: _state([_a1_tray(1)])})

        await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0)
        for tick in range(1, 25):  # a day of passes on a printer that can never answer
            await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=self._PAST + tick * 3600.0)

        assert len(manager.pushall_calls) > 1, "the ladder keeps asking — silence is not an answer"
        assert all(call == (printer.id, "bound_presence_unknown") for call in manager.pushall_calls)
        assert env.ws.await_count == 1, "…but the human is told once per episode"

    async def test_a_presence_value_change_closes_the_episode_and_re_arms(self, db_session, printer_factory, env):
        """PRESENT ends the episode outright; the next stale reading is a NEW situation,
        timed from when it started and starting the ladder over."""
        printer = await printer_factory()
        await _seed_assignment(db_session, printer.id, 0, 1)
        key = (printer.id, 0, 1)
        stale = _FakeManager({printer.id: _state([_cleared_tray(1)])})
        present = _FakeManager({printer.id: _state([_present_tray(1)])})

        await spool_tagless.reconcile_slot_config(db_session, manager=stale, now=_T0)
        await spool_tagless.reconcile_slot_config(db_session, manager=stale, now=self._PAST)
        assert len(stale.pushall_calls) == 1
        assert spool_tagless._presence_stale_episodes[key].asks == 1

        await spool_tagless.reconcile_slot_config(db_session, manager=present, now=self._PAST + 60)
        assert key not in spool_tagless._presence_stale_episodes, "PRESENT closes the episode"

        # Stale again — a fresh episode, so the age restarts and one more ask is earned.
        restart = self._PAST + 120
        await spool_tagless.reconcile_slot_config(db_session, manager=stale, now=restart)
        assert len(stale.pushall_calls) == 1, "the new episode has not aged yet"
        await spool_tagless.reconcile_slot_config(
            db_session, manager=stale, now=restart + spool_tagless._BOUND_PRESENCE_STALE_AFTER_S + 1
        )
        assert len(stale.pushall_calls) == 2

    async def test_a_reading_that_changes_value_restarts_the_clock(self, db_session, printer_factory, env):
        """None→False is a different situation, not a continuation: the new reading is
        timed from when IT started, so an old age cannot fire it instantly."""
        printer = await printer_factory()
        await _seed_assignment(db_session, printer.id, 0, 1)
        key = (printer.id, 0, 1)
        unknown = _FakeManager({printer.id: _state([_unknown_tray(1)])})
        empty = _FakeManager({printer.id: _state([_cleared_tray(1)])})

        await spool_tagless.reconcile_slot_config(db_session, manager=unknown, now=_T0)
        flip = _T0 + spool_tagless._BOUND_PRESENCE_STALE_AFTER_S - 1
        await spool_tagless.reconcile_slot_config(db_session, manager=empty, now=flip)

        episode = spool_tagless._presence_stale_episodes[key]
        assert (episode.presence, episode.first_seen, episode.asks) == (False, flip, 0)
        assert empty.pushall_calls == []

        await spool_tagless.reconcile_slot_config(db_session, manager=empty, now=flip + 60)
        assert empty.pushall_calls == [], "the old age belonged to the old reading"

    @pytest.mark.parametrize("exempt", ["pre_configured", "spent"])
    @pytest.mark.parametrize("shape", ["unknown", "asserted_empty"])
    async def test_exempt_bindings_are_never_flagged(self, db_session, printer_factory, env, exempt, shape):
        """The same two rule 9 exempts from release: a PRE-CONFIGURED binding is an
        intent over a deliberately empty slot, and a SPENT one is the ran-dry latch.
        Neither is a location claim presence could contradict."""
        printer = await printer_factory()
        await _seed_assignment(db_session, printer.id, 0, 1, spent=(exempt == "spent"))
        if exempt == "pre_configured":
            row = await _assignment(db_session, printer.id, 0, 1)
            row.pre_configured_at = datetime.utcnow()
            await db_session.commit()
        tray = _unknown_tray(1) if shape == "unknown" else _cleared_tray(1)
        manager = _FakeManager({printer.id: _state([tray])})

        await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0)
        await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=self._PAST)

        assert manager.pushall_calls == []
        env.ws.assert_not_awaited()
        assert (printer.id, 0, 1) not in spool_tagless._presence_stale_episodes

    @pytest.mark.parametrize("shape", ["unknown", "asserted_empty"])
    async def test_an_unbound_slot_is_never_flagged(self, db_session, printer_factory, env, shape):
        """Nothing claims the slot, so a stale reading contradicts nothing."""
        printer = await printer_factory()
        tray = _unknown_tray(1) if shape == "unknown" else _cleared_tray(1)
        manager = _FakeManager({printer.id: _state([tray])})

        await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0)
        await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=self._PAST)

        assert manager.pushall_calls == []
        env.ws.assert_not_awaited()

    async def test_a_refused_report_request_spends_its_rung_and_the_ladder_retries(
        self, db_session, printer_factory, env
    ):
        """A refusal (disconnected / inside the client's pacing floor) spends its rung and
        is NOT re-tried on the next pass — but the LADDER retries it, which is the whole
        point: pacing and disconnection are exactly the conditions the later rungs exist
        to outlast, and from inside, a refused ask and an unanswered one look the same."""
        printer = await printer_factory()
        await _seed_assignment(db_session, printer.id, 0, 1)
        manager = _FakeManager({printer.id: _state([_cleared_tray(1)])}, pushall_ok=False)

        await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0)
        await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=self._PAST)
        assert len(manager.pushall_calls) == 1
        await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=self._PAST + 60)
        assert len(manager.pushall_calls) == 1, "not re-asked at the pass cadence"

        rungs = self._rungs()
        await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0 + rungs[1] + 1.0)
        assert len(manager.pushall_calls) == 2, "the next rung retries the refused ask"

    async def test_a_rebind_opens_a_new_episode(self, db_session, printer_factory, env):
        """Episode identity includes the SPOOL: a different roll bound to the same stale
        slot is a new claim, and it earns its own single ask."""
        printer = await printer_factory()
        await _seed_assignment(db_session, printer.id, 0, 1)
        key = (printer.id, 0, 1)
        manager = _FakeManager({printer.id: _state([_cleared_tray(1)])})

        await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0)
        await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=self._PAST)
        assert len(manager.pushall_calls) == 1

        row = await _assignment(db_session, printer.id, 0, 1)
        await db_session.delete(row)
        await db_session.commit()
        replacement = await _seed_assignment(db_session, printer.id, 0, 1)

        rebound = self._PAST + 60
        await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=rebound)
        assert spool_tagless._presence_stale_episodes[key].spool_id == replacement.id
        assert len(manager.pushall_calls) == 1, "the new episode starts its own clock"

        await spool_tagless.reconcile_slot_config(
            db_session, manager=manager, now=rebound + spool_tagless._BOUND_PRESENCE_STALE_AFTER_S + 1
        )
        assert len(manager.pushall_calls) == 2


class TestSpentSwapParkArm:
    """WS2 — the spent-swap park must not be a SILENT deadlock.

    ``slot_state`` row 4a releases the W1 spent latch on a QUALIFIED PHYSICAL CYCLE, and
    4a′/5a on an ANSWERED no-tag read over a binding that CLAIMS a tag. A spent binding
    under a CONFIGURED, seated tray with neither can reach neither — and for a TAGLESS
    incumbent the answered-read escape does not exist by design (doctrine rule 11's
    one-way clause: over a binding that claims no identity a no-tag read proves nothing,
    because the same bare core reads identically before and after a swap). So the row
    returns ``KEEP("spent_latch")`` forever, on a production slot, saying nothing.

    This arm makes that LOUD. It is a SIBLING of the bound-presence arm, not a branch of
    it: opposite on spent-ness, opposite on presence, and it needs configuration where the
    other needs nothing. Only the operator SURFACE is shared. It asks the printer for
    NOTHING — the wire has already said everything it can — so there is no ladder here,
    only the single escalation, once per episode.
    """

    _PAST = _T0 + spool_tagless._SPENT_SWAP_PARK_AFTER_S + 1.0

    async def test_a_parked_spent_slot_tells_the_operator_once_per_episode(self, db_session, printer_factory, env):
        printer = await printer_factory(name="004-H2S")
        await _seed_assignment(db_session, printer.id, 0, 2, spent=True)
        manager = _FakeManager({printer.id: _state([_present_tray(2)])})

        await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0)
        env.ws.assert_not_awaited()  # first sighting only opens the episode

        await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=self._PAST)
        payload = env.ws.await_args.args[0]
        assert payload["type"] == "slot_standing_unknown"
        assert payload["case"] == "spent_swap_park"
        assert (payload["printer_id"], payload["ams_id"], payload["tray_id"]) == (printer.id, 0, 2)
        assert payload["printer_name"] == "004-H2S"
        assert env.ws.await_count == 1
        # Nothing is asked of the PRINTER: it is already answering perfectly.
        assert manager.pushall_calls == []

        # …and the park stays exactly one surface for as long as it stands.
        for extra in (60.0, 3600.0, 86400.0):
            await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=self._PAST + extra)
        assert env.ws.await_count == 1

    async def test_the_two_arms_do_not_merge(self, db_session, printer_factory, env):
        """LIVENESS PAIR + separation, in one pass.

        A NON-spent slot whose presence reads absent still takes the ORIGINAL arm — the
        one that asks the printer for a fresh report and escalates only after its whole
        ladder — while a spent+present+configured slot takes the new one. Merging the two
        predicates under one name is the ``_is_tagless`` mistake in a different costume,
        so the two must remain distinguishable by what they emit and what they ask for.
        """
        printer = await printer_factory()
        await _seed_assignment(db_session, printer.id, 0, 0)  # live row, absent tray
        await _seed_assignment(db_session, printer.id, 0, 1, spent=True)  # parked row, present tray
        manager = _FakeManager({printer.id: _state([_cleared_tray(0), _present_tray(1)])})

        await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0)
        assert manager.pushall_calls == []
        env.ws.assert_not_awaited()

        # Past BOTH maturities: the park escalates immediately, the presence arm only asks.
        await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=self._PAST)
        assert manager.pushall_calls == [(printer.id, "bound_presence_unknown")]  # slot 0's ask
        assert [c.args[0]["case"] for c in env.ws.await_args_list] == ["spent_swap_park"]  # slot 1's surface

        # Each keeps its own episode state, keyed to its own slot.
        assert (printer.id, 0, 0) in spool_tagless._presence_stale_episodes
        assert (printer.id, 0, 0) not in spool_tagless._spent_swap_park_episodes
        assert (printer.id, 0, 1) in spool_tagless._spent_swap_park_episodes
        assert (printer.id, 0, 1) not in spool_tagless._presence_stale_episodes

    async def test_a_pending_qualified_cycle_is_not_a_park(self, db_session, printer_factory, env):
        """A slot holding the release evidence is one push from resolving, not parked.

        Row 4a fires ``REPLACE_SPENT`` on the next push for exactly this state, so telling
        the operator about it would be an interruption about something already in hand.
        The peek must also not CONSUME the cycle — that would destroy the very evidence
        that ends the park.
        """
        printer = await printer_factory()
        await _seed_assignment(db_session, printer.id, 0, 1, spent=True)
        spool_tagless._pending_physical_cycles.add((printer.id, 0, 1))
        manager = _FakeManager({printer.id: _state([_present_tray(1)])})

        await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0)
        await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=self._PAST)

        env.ws.assert_not_awaited()
        assert (printer.id, 0, 1) not in spool_tagless._spent_swap_park_episodes
        assert spool_tagless.qualified_cycle_pending(printer.id, 0, 1), "the peek must not consume it"

    @pytest.mark.parametrize(
        ("why", "tray", "spent", "pre_configured"),
        [
            # The latch is not this arm's business when the row is LIVE — that slot is
            # the bound-presence arm's, and only when its presence stops answering.
            ("a live binding", _present_tray(1), False, False),
            # A BARE tray under a spent binding is rows 5/5a's constellation: the
            # answered-read escape reaches it, so it is not a park.
            ("a bare tray", _bare_tray(1), True, False),
            # Presence absent → row 3's ``spent_latch_on_empty``, which is the latch doing
            # its job over an empty bay. There is no roll to be a different roll.
            ("an absent tray", _cleared_tray(1), True, False),
            ("an unknown presence", _unknown_tray(1), True, False),
            # CONFIGURED but presence UNKNOWN (the A1/P1S constant ``state=3`` dialect).
            # This is the case that isolates the presence gate from the config gate: an
            # unknown is never resolved toward action (invariant 3), so a slot the farm
            # cannot confirm is even occupied is not a slot it may call parked.
            ("a configured tray whose presence is unknown", _a1_tray(1), True, False),
            # Operator intent is never guessed over (scenario T13).
            ("a pre-configured binding", _present_tray(1), True, True),
        ],
    )
    async def test_only_the_park_itself_is_flagged(
        self, db_session, printer_factory, env, why, tray, spent, pre_configured
    ):
        printer = await printer_factory()
        await _seed_assignment(db_session, printer.id, 0, 1, spent=spent)
        if pre_configured:
            row = await _assignment(db_session, printer.id, 0, 1)
            row.pre_configured_at = datetime.utcnow()
            await db_session.commit()
        manager = _FakeManager({printer.id: _state([tray])})

        await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0)
        await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=self._PAST)

        assert [c for c in env.ws.await_args_list if c.args[0].get("case") == "spent_swap_park"] == [], why
        assert (printer.id, 0, 1) not in spool_tagless._spent_swap_park_episodes, why

    async def test_an_unbound_slot_is_never_flagged(self, db_session, printer_factory, env):
        """Nothing holds the slot, so there is no latch to be parked behind."""
        printer = await printer_factory()
        manager = _FakeManager({printer.id: _state([_present_tray(1)])})

        await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0)
        await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=self._PAST)

        env.ws.assert_not_awaited()
        assert (printer.id, 0, 1) not in spool_tagless._spent_swap_park_episodes

    async def test_the_park_ending_closes_the_episode_and_re_arms(self, db_session, printer_factory, env):
        """The episode is one continuous run of the SAME park. Un-spending the row ends it;
        a later park on the same slot is a NEW situation and earns its own surface."""
        printer = await printer_factory()
        spool = await _seed_assignment(db_session, printer.id, 0, 1, spent=True)
        key = (printer.id, 0, 1)
        manager = _FakeManager({printer.id: _state([_present_tray(1)])})

        await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0)
        await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=self._PAST)
        assert env.ws.await_count == 1

        refreshed = await db_session.get(Spool, spool.id)
        refreshed.spent_at = None  # the row is live again → the park is over
        await db_session.commit()
        await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=self._PAST + 60)
        assert key not in spool_tagless._spent_swap_park_episodes

        refreshed.spent_at = datetime.utcnow()  # …and a NEW park opens on the same slot
        await db_session.commit()
        reopened = self._PAST + 120
        await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=reopened)
        assert env.ws.await_count == 1, "the new episode starts its own clock"
        await spool_tagless.reconcile_slot_config(
            db_session, manager=manager, now=reopened + spool_tagless._SPENT_SWAP_PARK_AFTER_S + 1
        )
        assert env.ws.await_count == 2

    async def test_the_park_matures_on_its_OWN_threshold(self, db_session, printer_factory, env, monkeypatch):
        """The two arms' maturities agree today, and that must stay a COINCIDENCE.

        They measure different situations — one a wire that has stopped answering, the
        other a wire answering perfectly with no evidence the table may act on — so the
        park reads its own constant. Moving the presence arm's must not move the park's.
        """
        monkeypatch.setattr(spool_tagless, "_SPENT_SWAP_PARK_AFTER_S", 60.0)
        printer = await printer_factory()
        await _seed_assignment(db_session, printer.id, 0, 1, spent=True)
        manager = _FakeManager({printer.id: _state([_present_tray(1)])})

        await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0)
        await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=_T0 + 61.0)
        assert env.ws.await_count == 1, "matured on the park's own threshold, not the presence arm's"


# --- B: scheduler registration ---------------------------------------------


class TestSchedulerRegistration:
    async def test_check_queue_drives_the_reconcile(self):
        """B — REGISTRATION PIN: ``check_queue`` must invoke the reconcile every tick.
        Fails if the guarded block in ``print_scheduler.check_queue`` is removed.
        Mirrors ``test_farm_stall.TestSchedulerHookGuard``'s check_queue drive."""
        from unittest.mock import MagicMock

        from backend.app.services.print_scheduler import PrintScheduler

        reconcile = AsyncMock(return_value=0)
        empty = MagicMock()
        empty.scalars.return_value.all.return_value = []
        with (
            patch("backend.app.services.print_scheduler.async_session") as mock_session,
            patch("backend.app.services.farm_stall.check_stalled_prints", new=AsyncMock()),
            patch("backend.app.services.farm_stall.check_paused_prints", new=AsyncMock()),
            patch("backend.app.services.farm_stall.check_attention_reminders", new=AsyncMock()),
            patch("backend.app.services.spool_tagless.reconcile_slot_config", new=reconcile),
        ):
            mock_db = AsyncMock()
            mock_db.execute = AsyncMock(return_value=empty)
            mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_session.return_value.__aexit__ = AsyncMock(return_value=False)
            await PrintScheduler().check_queue()

        reconcile.assert_awaited_once()
        assert reconcile.await_args.args[0] is not None  # driven with the tick's session

    async def test_check_queue_survives_reconcile_exception(self):
        """The block has its OWN guard: a reconcile failure must not kill the tick."""
        from unittest.mock import MagicMock

        from backend.app.services.print_scheduler import PrintScheduler

        empty = MagicMock()
        empty.scalars.return_value.all.return_value = []
        with (
            patch("backend.app.services.print_scheduler.async_session") as mock_session,
            patch("backend.app.services.farm_stall.check_stalled_prints", new=AsyncMock()),
            patch("backend.app.services.farm_stall.check_paused_prints", new=AsyncMock()),
            patch("backend.app.services.farm_stall.check_attention_reminders", new=AsyncMock()),
            patch(
                "backend.app.services.spool_tagless.reconcile_slot_config",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
        ):
            mock_db = AsyncMock()
            mock_db.execute = AsyncMock(return_value=empty)
            mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_session.return_value.__aexit__ = AsyncMock(return_value=False)
            await PrintScheduler().check_queue()  # must not raise


# --- 010-H2S: the backup-group harmonise arm -------------------------------

_TAGLESS_DEFAULT = {
    "brand": "Bambu Lab",
    "material": "PETG",
    "subtype": "HF",
    "rgba": "000000FF",
    "slicer_filament": "GFG02",
    "nozzle_temp_min": 230,
    "nozzle_temp_max": 270,
}


def _configured_tray(tray_id=0, *, color="161616FF", info_idx="GFG02", tmin=230, tmax=270, state=11, tag=False):
    """A CONFIGURED tagless tray. Default colour is the incident's ``161616FF`` — what a
    Bambu Studio / touchscreen slot edit leaves behind when it accepts the PETG-HF
    preset's own default colour."""
    return {
        "id": tray_id,
        "state": state,
        "tray_type": "PETG",
        "tray_sub_brands": "PETG HF",
        "tray_color": color,
        "tray_info_idx": info_idx,
        "nozzle_temp_min": tmin,
        "nozzle_temp_max": tmax,
        "tag_uid": _BAMBU_TAG if tag else _ZERO_TAG,
        "tray_uuid": _BAMBU_UUID if tag else _ZERO_UUID,
    }


class _Clock:
    """Drives the shared retry window so consecutive walks are one call apart, not 30 s."""

    def __init__(self):
        self.t = 1_000.0

    def __call__(self):
        return self.t

    def tick(self):
        self.t += spool_tagless._AUTOCONFIG_RETRY_S + 1.0


class TestBackupGroupHarmonise:
    """010-H2S (2026-08-21) INCIDENT PINS. The printer ran out on slot 2 twice in 28 h
    with black PETG loaded in slot 4 and AMS Filament Backup ON, and the firmware never
    auto-switched: slots 1+2 carried tray colour ``161616FF``, slots 3+4 carried the
    farm's ``000000FF``, and the firmware pairs backup slots only on an EXACT colour
    match. ``hms-events`` proves the split — 69 auto-switches inside 1<->2, 14 inside
    3->4, none across the pair.

    The farm already harmonised the PRESET dimension (the 011-H2S GFG99 fix) and the
    TEMPS dimension (W4). This arm is the third: it exists so a slot an operator edited
    on the touchscreen can come back to the fleet's identity on its own."""

    @pytest.fixture
    def clock(self, monkeypatch):
        c = _Clock()
        monkeypatch.setattr("backend.app.utils.retry_window.monotonic", c)
        return c

    @staticmethod
    async def _walk(db, printer_id, tray, manager_state=None, *, now=_T0):
        manager = _FakeManager({printer_id: manager_state or _state([tray])})
        return await spool_tagless.reconcile_slot_config(db, manager=manager, now=now)

    async def test_off_canonical_colour_is_rewritten_and_pushed(self, db_session, printer_factory, env):
        """THE PIN. Row and tray both read ``161616FF``: the row is corrected to the
        default's ``000000FF`` and that identity is published to the slot, which is what
        puts it back in one backup group with its peers."""
        env.settings["tagless_default_filament"] = json.dumps(_TAGLESS_DEFAULT)
        printer = await printer_factory()
        spool = await _seed_assignment(
            db_session, printer.id, rgba="161616FF", slicer_filament="GFG02", nozzle_temp_min=230, nozzle_temp_max=270
        )

        assert await self._walk(db_session, printer.id, _configured_tray()) == 1

        await db_session.refresh(spool)
        assert spool.rgba == "000000FF"
        env.apply.assert_awaited_once()
        kw = env.apply.await_args.kwargs
        assert (kw["printer_id"], kw["ams_id"], kw["tray_id"]) == (printer.id, 0, 0)
        assert kw["spool"].id == spool.id

    async def test_a_canonical_slot_is_silent_and_marks_adoption(self, db_session, printer_factory, env):
        """Liveness's other half: a slot already in the fleet's group is written to
        never, and its write epoch is closed rather than left to accumulate strikes."""
        env.settings["tagless_default_filament"] = json.dumps(_TAGLESS_DEFAULT)
        printer = await printer_factory()
        await _seed_assignment(
            db_session, printer.id, rgba="000000FF", slicer_filament="GFG02", nozzle_temp_min=230, nozzle_temp_max=270
        )

        assert await self._walk(db_session, printer.id, _configured_tray(color="000000FF")) == 0
        env.apply.assert_not_awaited()
        assert spool_tagless._autoconfig_epochs == {}

    async def test_rfid_tray_is_never_touched(self, db_session, printer_factory, env):
        """A config write racing a firmware tag read is the 2026-07-18 HMS 0700_0081
        class — the arm's ownership test excludes a tagged tray before anything else."""
        env.settings["tagless_default_filament"] = json.dumps(_TAGLESS_DEFAULT)
        printer = await printer_factory()
        spool = await _seed_assignment(db_session, printer.id, rgba="161616FF", tag_uid=_BAMBU_TAG)

        assert await self._walk(db_session, printer.id, _configured_tray(tag=True)) == 0
        await db_session.refresh(spool)
        assert spool.rgba == "161616FF"  # the row is not ours to rewrite either
        env.apply.assert_not_awaited()

    async def test_operator_bound_row_is_never_touched(self, db_session, printer_factory, env):
        """An operator- or RFID-bound row is somebody's STATEMENT about that slot."""
        env.settings["tagless_default_filament"] = json.dumps(_TAGLESS_DEFAULT)
        printer = await printer_factory()
        spool = await _seed_assignment(db_session, printer.id, data_origin="manual", rgba="161616FF")

        assert await self._walk(db_session, printer.id, _configured_tray()) == 0
        await db_session.refresh(spool)
        assert spool.rgba == "161616FF"
        env.apply.assert_not_awaited()

    async def test_a_different_specific_preset_is_left_alone(self, db_session, printer_factory, env):
        """GFG00 beside a GFG02 default is an operator statement (doctrine rule 2) — not
        canonicalised on the preset dimension, and therefore not on colour either."""
        env.settings["tagless_default_filament"] = json.dumps(_TAGLESS_DEFAULT)
        printer = await printer_factory()
        spool = await _seed_assignment(db_session, printer.id, rgba="161616FF", slicer_filament="GFG00")

        assert await self._walk(db_session, printer.id, _configured_tray(info_idx="GFG00")) == 0
        await db_session.refresh(spool)
        assert (spool.rgba, spool.slicer_filament) == ("161616FF", "GFG00")
        env.apply.assert_not_awaited()

    async def test_spent_row_stays_latched(self, db_session, printer_factory, env):
        """W1: a spent binding is the durable 'ran dry' latch. Harmonising a dead roll's
        identity would re-push config for a slot waiting on a physical swap."""
        env.settings["tagless_default_filament"] = json.dumps(_TAGLESS_DEFAULT)
        printer = await printer_factory()
        spool = await _seed_assignment(db_session, printer.id, spent=True, rgba="161616FF")

        assert await self._walk(db_session, printer.id, _configured_tray()) == 0
        await db_session.refresh(spool)
        assert spool.rgba == "161616FF"
        env.apply.assert_not_awaited()

    async def test_busy_printer_skips_the_publish_without_burning_the_window(self, db_session, printer_factory, env):
        """Same backoff as the bare arm: a RUNNING/PAUSE AMS silently drops config
        writes, so the pass skips the PUBLISH and leaves the cadence unarmed — the first
        settled walk pushes immediately instead of waiting one out."""
        env.settings["tagless_default_filament"] = json.dumps(_TAGLESS_DEFAULT)
        printer = await printer_factory()
        await _seed_assignment(db_session, printer.id, rgba="161616FF", slicer_filament="GFG02")
        tray = _configured_tray()

        busy = _state([tray], gcode_state="RUNNING")
        assert await self._walk(db_session, printer.id, tray, busy) == 0
        env.apply.assert_not_awaited()
        assert (printer.id, 0, 0) not in spool_tagless._autoconfig_window

        assert await self._walk(db_session, printer.id, tray, now=_PAST_WINDOW) == 1
        env.apply.assert_awaited_once()

    async def test_a_latched_epoch_stops_the_arm(self, db_session, printer_factory, env, clock):
        """The wire's own verdict. A ``result:"fail"`` ack ends the slot's write epoch,
        and nothing writes to it again until a presence/identity edge re-arms it —
        exactly as for the bare arm, because it is the same epoch."""
        env.settings["tagless_default_filament"] = json.dumps(_TAGLESS_DEFAULT)
        printer = await printer_factory()
        await _seed_assignment(db_session, printer.id, rgba="161616FF", slicer_filament="GFG02")
        tray = _configured_tray()

        assert await self._walk(db_session, printer.id, tray) == 1
        spool_tagless.on_ams_command_result(
            printer.id, {"command": "ams_filament_setting", "result": "fail", "ams_id": 0, "tray_id": 0}
        )

        clock.tick()
        assert await self._walk(db_session, printer.id, tray, now=_PAST_WINDOW) == 0
        assert env.apply.await_count == 1

    async def test_three_unadopted_pushes_end_the_epoch(self, db_session, printer_factory, env, clock, caplog):
        """The strike ladder, on this arm. A tray that keeps reporting its own colour has
        NOT adopted our config — which is exactly why adoption must be derived from the
        identity the firmware reflects rather than from a non-empty ``tray_type``.
        Without that, the lane would re-publish every 30 s forever (shape 28)."""
        env.settings["tagless_default_filament"] = json.dumps(_TAGLESS_DEFAULT)
        printer = await printer_factory()
        await _seed_assignment(db_session, printer.id, rgba="161616FF", slicer_filament="GFG02")
        tray = _configured_tray()

        with caplog.at_level(logging.INFO, logger="backend.app.services.spool_tagless"):
            for i in range(spool_tagless._AUTOCONFIG_MAX_PUBLISHES):
                assert await self._walk(db_session, printer.id, tray, now=_T0 + i * 100.0) == 1
                clock.tick()
            assert env.apply.await_count == spool_tagless._AUTOCONFIG_MAX_PUBLISHES

            # A fourth eligible walk: the ladder ends the epoch instead of publishing.
            assert await self._walk(db_session, printer.id, tray, now=_T0 + 1000.0) == 0
            clock.tick()
            assert await self._walk(db_session, printer.id, tray, now=_T0 + 2000.0) == 0

        assert env.apply.await_count == spool_tagless._AUTOCONFIG_MAX_PUBLISHES
        stops = [r for r in caplog.records if "auto-config STOPPED" in r.message]
        assert len(stops) == 1
        assert "still reports its own" in stops[0].getMessage()

    async def test_a_reflected_identity_marks_adoption_and_stops_the_arm(self, db_session, printer_factory, env, clock):
        """The success path the ladder is measured against: once the tray reports the
        canonical identity the epoch closes, the arm goes quiet, and a LATER episode
        starts with a full ladder instead of inheriting stale strikes."""
        env.settings["tagless_default_filament"] = json.dumps(_TAGLESS_DEFAULT)
        printer = await printer_factory()
        await _seed_assignment(db_session, printer.id, rgba="161616FF", slicer_filament="GFG02")

        assert await self._walk(db_session, printer.id, _configured_tray()) == 1
        clock.tick()

        # The firmware now reflects what we asked for.
        assert await self._walk(db_session, printer.id, _configured_tray(color="000000FF"), now=_PAST_WINDOW) == 0
        assert env.apply.await_count == 1
        assert spool_tagless._autoconfig_epochs == {}
