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
    monkeypatch.setattr("backend.app.api.routes.inventory.apply_spool_to_slot_via_mqtt", apply)

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
    """Injectable ``printer_manager`` stand-in — the reconcile reads ``get_status`` only."""

    def __init__(self, states: dict[int, object]):
        self._states = states
        self.status_calls: list[int] = []

    def get_status(self, printer_id: int):
        self.status_calls.append(printer_id)
        return self._states.get(printer_id)


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


async def _seed_spool(db, *, data_origin="ams_auto", spent=False, tag_uid=None):
    spool = Spool(
        material="PETG",
        rgba="000000FF",
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
