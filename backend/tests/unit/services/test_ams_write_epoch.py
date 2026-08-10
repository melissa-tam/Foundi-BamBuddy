"""The firmware's ACK of an AMS write, and the write epoch it bounds.

2026-08-10, phantom-presence incident. The AMS answers every ``ams_filament_setting``
on the REPORT topic with the request's own command plus a ``result``. During the
incident that answer was ``"fail"`` EVERY time and nothing in the farm consumed it, so
the bare-tray auto-config lane re-published into a slot that was refusing the write —
roughly 40k writes a day, for hours, with no exit. "Retry until the firmware reflects
it" has no termination condition when the firmware's answer is no.

Two things are pinned here, in the order the wire produces them:

* the client ROUTES the echo (and only an echo) to ``on_ams_command_result``, as an
  owned scalar payload that no later merge can rewrite, without disturbing the status
  duties of the same message — these echoes carry a full ``ams`` section too;
* ``spool_tagless`` ACCOUNTS for it per slot: a refusal, or three attempts the tray
  never reflects, ends that slot's write epoch — no more writes, one INFO line, and a
  pushall owed — and only a presence/identity EDGE re-arms it. Never a timer: a latch
  that expires on its own is the same unbounded loop, slower.
"""

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.services import spool_tagless
from backend.app.services.bambu_mqtt import BambuMQTTClient

# No module-level asyncio mark: this file mixes sync client tests with async lane tests
# and the project runs pytest-asyncio in auto mode (pyproject `asyncio_mode = "auto"`).


# --- the wire ---------------------------------------------------------------


def _ack(result="fail", *, command="ams_filament_setting", ams_id=0, tray_id=1, **extra):
    """A command echo in the shape captured off the fleet on 2026-08-10.

    The ``ams`` section is carried because the real echoes carry it: an ACK is also a
    status push, and a routing change that quietly stopped the status half from being
    handled would be a regression the ACK tests could not see.
    """
    echo = {
        "command": command,
        "result": result,
        "ams_id": ams_id,
        "tray_id": tray_id,
        "sequence_id": "0",
        "tray_type": "PETG",
        "tray_info_idx": "GFG02",
        "ams": [
            {
                "id": "0",
                "humidity": "4",
                "temp": "27.6",
                "tray": [
                    {"id": "0", "state": 9},
                    {"id": "1", "state": 10, "tray_type": "PETG", "remain": 62},
                ],
            }
        ],
    }
    echo.update(extra)
    return echo


@pytest.fixture
def client():
    """A client with the ACK hook armed, recording every delivery."""
    delivered: list[dict] = []
    c = BambuMQTTClient(
        ip_address="192.168.1.100",
        serial_number="TEST123",
        access_code="12345678",
        on_ams_command_result=delivered.append,
    )
    c.delivered = delivered  # type: ignore[attr-defined]
    return c


# --- B1: the client routes the ACK -----------------------------------------


class TestAckRouting:
    def test_a_fail_echo_reaches_the_consumer_with_the_wire_verdict(self, client):
        client._process_message({"print": _ack("fail", ams_id=0, tray_id=2)})

        assert len(client.delivered) == 1
        echo = client.delivered[0]
        assert echo["command"] == "ams_filament_setting"
        assert echo["result"] == "fail"
        assert (echo["ams_id"], echo["tray_id"]) == (0, 2)

    def test_the_cali_echo_rides_the_same_lane(self, client):
        """Routing both write commands is the client's contract; judging them is not."""
        client._process_message({"print": _ack("fail", command="extrusion_cali_sel", cali_idx=3)})

        assert [e["command"] for e in client.delivered] == ["extrusion_cali_sel"]

    def test_the_payload_is_owned_and_carries_no_ams_section(self, client):
        """The opposite contract to ``on_ams_push_raw``: there the aliasing is the point.

        A consumer keeps this dict across pushes (a write epoch outlives the echo that
        ends it), so it must not be a window onto wire dicts the merge is about to
        rewrite — and the ``ams`` section is deliberately absent: it went through
        ``_handle_ams_data`` on the ordinary status path, which stays its one origin.
        """
        payload = _ack("fail")
        client._process_message({"print": payload})

        echo = client.delivered[0]
        assert "ams" not in echo
        assert all(not isinstance(v, (dict, list)) for v in echo.values())
        payload["result"] = "success"  # a later mutation of the wire payload
        assert echo["result"] == "fail"

    def test_the_echos_ams_section_still_merges(self, client):
        """An ACK is also a status push — the routing must not divert it."""
        client._process_message({"print": _ack("fail")})

        trays = client.state.raw_data["ams"][0]["tray"]
        assert {t["id"] for t in trays} == {"0", "1"}
        assert next(t for t in trays if t["id"] == "1")["tray_type"] == "PETG"

    @pytest.mark.parametrize(
        "payload",
        [
            {"command": "ams_filament_setting"},  # no result → not an ACK, just a request echo
            {"command": "push_status", "result": "success"},  # unrelated command
            {"result": "fail"},  # no command at all
            {"command": "ams_filament_drying", "result": "fail"},  # a write we do not account for
        ],
        ids=["no-result", "unknown-command", "no-command", "unaccounted-command"],
    )
    def test_malformed_or_unrelated_payloads_are_ignored(self, client, payload):
        client._process_message({"print": payload})

        assert client.delivered == []

    def test_a_non_dict_payload_is_ignored(self, client):
        client._deliver_command_result(["ams_filament_setting", "fail"])  # type: ignore[arg-type]

        assert client.delivered == []

    def test_a_raising_consumer_never_breaks_the_callback_chain(self):
        """Entry-hook rule: a farm-side failure must not cost the message its status half."""

        def boom(_echo):
            raise RuntimeError("consumer exploded")

        c = BambuMQTTClient(
            ip_address="192.168.1.100",
            serial_number="TEST123",
            access_code="12345678",
            on_ams_command_result=boom,
        )

        c._process_message({"print": _ack("fail")})

        assert c.state.raw_data["ams"][0]["tray"][1]["tray_type"] == "PETG"

    def test_no_hook_is_the_upstream_default(self):
        """An install that wires nothing behaves exactly as before."""
        c = BambuMQTTClient(ip_address="192.168.1.100", serial_number="TEST123", access_code="12345678")

        c._process_message({"print": _ack("fail")})  # must not raise

        assert c.on_ams_command_result is None

    async def test_the_manager_wires_the_printer_id_into_the_hook(self, monkeypatch):
        """The id lives in the closure: the client is keyed by serial and knows no DB."""
        from backend.app.services import printer_manager as pm_module

        seen: list[tuple] = []
        monkeypatch.setattr(spool_tagless, "on_ams_command_result", lambda pid, echo: seen.append((pid, echo)))
        captured: dict = {}

        class _Client:
            state = SimpleNamespace(connected=True)

            def __init__(self, **kwargs):
                captured.update(kwargs)

            def connect(self):
                return None

        monkeypatch.setattr(pm_module, "BambuMQTTClient", _Client)
        monkeypatch.setattr(pm_module.asyncio, "sleep", AsyncMock())
        manager = pm_module.PrinterManager()
        printer = SimpleNamespace(id=7, ip_address="1.2.3.4", serial_number="S", access_code="A", model="H2S", name="p")

        await manager.connect_printer(printer)

        captured["on_ams_command_result"]({"command": "ams_filament_setting", "result": "fail"})
        assert seen == [(7, {"command": "ams_filament_setting", "result": "fail"})]


# --- B3: the captured mapping is public ------------------------------------


class TestCapturedAmsMapping:
    def test_the_property_mirrors_the_capture(self, client):
        assert client.captured_ams_mapping is None

        client._handle_request_message({"print": {"command": "project_file", "ams_mapping": [0, 4, -1, -1]}})

        assert client.captured_ams_mapping == [0, 4, -1, -1]

    def test_the_property_is_read_only(self, client):
        with pytest.raises(AttributeError):
            client.captured_ams_mapping = [1]  # type: ignore[misc]


# --- B2: the write epoch ----------------------------------------------------


def _bare(*, state=11):
    """A tray holding a spool with nothing configured — the lane's whole trigger."""
    return {
        "id": 1,
        "state": state,
        "tray_type": "",
        "tray_sub_brands": "",
        "tray_color": "",
        "tray_info_idx": "",
        "tag_uid": "0" * 16,
        "tray_uuid": "0" * 32,
    }


class _Clock:
    """Drives the retry window so consecutive attempts are one call apart, not 30 s."""

    def __init__(self):
        self.t = 1_000.0

    def __call__(self):
        return self.t

    def tick(self):
        """Step past one full settle window — the spacing a strike is measured across."""
        self.t += spool_tagless._AUTOCONFIG_RETRY_S + 1.0


@pytest.fixture(autouse=True)
def _clean_state():
    spool_tagless._reset_state()
    yield
    spool_tagless._reset_state()


@pytest.fixture
def env(monkeypatch):
    """Settings dict, patched config push, WS broadcast, evidence lane and clock."""
    settings: dict[str, str] = {}

    async def fake_get_setting(db, key):
        return settings.get(key)

    monkeypatch.setattr("backend.app.api.routes.settings.get_setting", fake_get_setting)

    apply = AsyncMock(return_value=True)
    monkeypatch.setattr("backend.app.api.routes.inventory.apply_spool_to_slot_via_mqtt", apply)
    monkeypatch.setattr(spool_tagless.ws_manager, "broadcast", AsyncMock())
    monkeypatch.setattr(spool_tagless.printer_manager, "get_status", lambda pid: None)
    monkeypatch.setattr(spool_tagless.printer_manager, "get_client", lambda pid: None)

    pushalls: list[tuple[int, str]] = []
    monkeypatch.setattr(
        spool_tagless.printer_manager,
        "request_evidence_pushall",
        lambda pid, reason: bool(pushalls.append((pid, reason))) or True,
    )

    clock = _Clock()
    monkeypatch.setattr("backend.app.utils.retry_window.monotonic", clock)

    settings["tagless_default_filament"] = json.dumps(
        {"brand": "Bambu Lab", "material": "PETG", "subtype": "HF", "rgba": "000000FF"}
    )
    return SimpleNamespace(settings=settings, apply=apply, pushalls=pushalls, clock=clock)


async def _attempt(db, printer_id, tray=None, *, ams_id=0, tray_id=1):
    return await spool_tagless.maybe_autoconfigure_bare_tray(
        db, printer_id, ams_id, tray_id, tray if tray is not None else _bare()
    )


class TestFailAckEndsTheEpoch:
    async def test_a_refusal_stops_the_lane_writing_to_that_slot(self, db_session, printer_factory, env):
        printer = await printer_factory()
        assert await _attempt(db_session, printer.id) is True
        assert env.apply.await_count == 1

        spool_tagless.on_ams_command_result(printer.id, _ack("fail", ams_id=0, tray_id=1))

        # Every later occasion, each a fresh retry window, publishes nothing.
        for _ in range(3):
            env.clock.tick()
            assert await _attempt(db_session, printer.id) is False
        assert env.apply.await_count == 1  # nothing further reached the wire

    async def test_force_does_not_bypass_the_latch(self, db_session, printer_factory, env):
        """``force=`` supplies a fresh OCCASION; a refusing slot needs new EVIDENCE."""
        printer = await printer_factory()
        await _attempt(db_session, printer.id)
        spool_tagless.on_ams_command_result(printer.id, _ack("fail"))

        assert (
            await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 1, _bare(), force=True)
            is False
        )
        assert env.apply.await_count == 1

    async def test_the_refusal_is_logged_once_and_owes_evidence(self, db_session, printer_factory, env, caplog):
        printer = await printer_factory()
        await _attempt(db_session, printer.id)

        with caplog.at_level(logging.INFO, logger="backend.app.services.spool_tagless"):
            spool_tagless.on_ams_command_result(printer.id, _ack("fail", reason="0x0700_2000"))
            env.clock.tick()
            await _attempt(db_session, printer.id)  # a second occasion must not re-log

        stops = [r for r in caplog.records if "auto-config STOPPED" in r.message]
        assert len(stops) == 1
        assert 'result="fail"' in stops[0].getMessage()
        assert "reason=0x0700_2000" in stops[0].getMessage()
        assert env.pushalls == [(printer.id, "config_refused")]

    async def test_an_ack_for_a_slot_the_lane_never_wrote_mints_no_latch(self, db_session, printer_factory, env):
        """An operator assign's refusal is not this lane's business — and latching on it
        would suppress attempts that were never attempted."""
        printer = await printer_factory()

        spool_tagless.on_ams_command_result(printer.id, _ack("fail"))

        assert spool_tagless._autoconfig_epochs == {}
        assert env.pushalls == []
        assert await _attempt(db_session, printer.id) is True

    @pytest.mark.parametrize(
        "echo",
        [
            _ack("fail", command="extrusion_cali_sel"),  # K selection, GLOBAL tray id space
            _ack("fail", ams_id=255, tray_id=254),  # external spool: coordinates not invertible
            _ack("weird"),  # not a verdict
            _ack("fail", ams_id="x"),  # unparseable coordinate
        ],
        ids=["cali", "external", "non-verdict", "unparseable"],
    )
    async def test_echoes_this_lane_cannot_judge_are_ignored(self, db_session, printer_factory, env, echo):
        printer = await printer_factory()
        await _attempt(db_session, printer.id)

        spool_tagless.on_ams_command_result(printer.id, echo)

        env.clock.tick()
        assert await _attempt(db_session, printer.id) is True
        assert env.apply.await_count == 2

    async def test_the_consumer_never_raises(self, printer_factory, env):
        """It runs on the MQTT thread; a farm-side failure must not break the chain."""
        spool_tagless.on_ams_command_result(1, {"command": "ams_filament_setting", "result": None})
        spool_tagless.on_ams_command_result(1, {})


class TestEdgesReArmTheEpoch:
    async def test_a_qualified_physical_cycle_re_arms(self, db_session, printer_factory, env, monkeypatch):
        printer = await printer_factory()
        await _attempt(db_session, printer.id)
        spool_tagless.on_ams_command_result(printer.id, _ack("fail"))
        env.clock.tick()
        assert await _attempt(db_session, printer.id) is False

        # The presence GAIN edge: ams_presence calls this on a measured roll swap.
        monkeypatch.setattr(spool_tagless, "_maybe_prompt_fresh_roll", AsyncMock())
        await spool_tagless.note_physical_cycle(printer.id, 0, 1)

        assert await _attempt(db_session, printer.id) is True
        assert env.apply.await_count == 2

    async def test_the_slot_emptying_re_arms(self, db_session, printer_factory, env):
        printer = await printer_factory()
        await _attempt(db_session, printer.id)
        spool_tagless.on_ams_command_result(printer.id, _ack("fail"))

        spool_tagless.clear_autoconfig_dedup(printer.id, 0, 1)  # the LOSS edge

        assert await _attempt(db_session, printer.id) is True
        assert env.apply.await_count == 2

    async def test_nothing_else_re_arms_it(self, db_session, printer_factory, env):
        """No timer: time alone is exactly what the unbounded loop already had."""
        printer = await printer_factory()
        await _attempt(db_session, printer.id)
        spool_tagless.on_ams_command_result(printer.id, _ack("fail"))

        for _ in range(20):
            env.clock.tick()
            assert await _attempt(db_session, printer.id) is False
        assert env.apply.await_count == 1

    async def test_a_restart_re_derives_a_fresh_epoch(self, db_session, printer_factory, env):
        """Documented process-memory behaviour: a restart costs one more ladder, never a
        suppressed attempt — and the wire re-states its verdict on that attempt."""
        printer = await printer_factory()
        await _attempt(db_session, printer.id)
        spool_tagless.on_ams_command_result(printer.id, _ack("fail"))

        spool_tagless._reset_state()  # what a process restart leaves behind

        assert await _attempt(db_session, printer.id) is True
        assert env.apply.await_count == 2


class TestStrikeLadder:
    async def test_three_unanswered_attempts_end_the_epoch(self, db_session, printer_factory, env, caplog):
        printer = await printer_factory()

        with caplog.at_level(logging.INFO, logger="backend.app.services.spool_tagless"):
            for _ in range(spool_tagless._AUTOCONFIG_MAX_PUBLISHES):
                assert await _attempt(db_session, printer.id) is True
                env.clock.tick()
            assert env.apply.await_count == spool_tagless._AUTOCONFIG_MAX_PUBLISHES

            # The window is open for a fourth attempt; the ladder ends the epoch instead.
            assert await _attempt(db_session, printer.id) is False
            env.clock.tick()
            assert await _attempt(db_session, printer.id) is False

        assert env.apply.await_count == spool_tagless._AUTOCONFIG_MAX_PUBLISHES
        stops = [r for r in caplog.records if "auto-config STOPPED" in r.message]
        assert len(stops) == 1
        assert "still reports none applied" in stops[0].getMessage()
        assert env.pushalls == [(printer.id, "config_unadopted")]

    async def test_a_success_ack_alone_does_not_stop_the_ladder(self, db_session, printer_factory, env, caplog):
        """The incident lives in the gap between "accepted" and "applied": an ack the
        tray never reflects is the same conclusion as no ack, reached politely."""
        printer = await printer_factory()

        with caplog.at_level(logging.INFO, logger="backend.app.services.spool_tagless"):
            for _ in range(spool_tagless._AUTOCONFIG_MAX_PUBLISHES):
                assert await _attempt(db_session, printer.id) is True
                spool_tagless.on_ams_command_result(printer.id, _ack("success"))
                env.clock.tick()
            assert await _attempt(db_session, printer.id) is False

        stops = [r for r in caplog.records if "auto-config STOPPED" in r.message]
        assert len(stops) == 1
        assert f"{spool_tagless._AUTOCONFIG_MAX_PUBLISHES} success ack(s)" in stops[0].getMessage()

    async def test_adoption_keeps_the_lane_healthy(self, db_session, printer_factory, env):
        """Success = the tray REFLECTS the config. The epoch closes and a later bare
        episode starts with a full ladder instead of inheriting stale strikes."""
        printer = await printer_factory()
        for _ in range(spool_tagless._AUTOCONFIG_MAX_PUBLISHES - 1):
            assert await _attempt(db_session, printer.id) is True
            spool_tagless.on_ams_command_result(printer.id, _ack("success"))
            env.clock.tick()

        # The firmware now reports the configuration: the lane's own guard sees it.
        configured = _bare()
        configured["tray_type"] = "PETG"
        assert await _attempt(db_session, printer.id, configured) is False
        assert spool_tagless._autoconfig_epochs == {}

        # A later bare episode (a firmware slot reset) gets the full ladder back.
        for _ in range(spool_tagless._AUTOCONFIG_MAX_PUBLISHES):
            assert await _attempt(db_session, printer.id) is True
            env.clock.tick()
        assert env.apply.await_count == (2 * spool_tagless._AUTOCONFIG_MAX_PUBLISHES) - 1

    async def test_the_reconcile_walk_observes_adoption(self, db_session, printer_factory, env, monkeypatch):
        """The RELIABLE observer: both bare-tray call sites pre-filter on an empty
        tray_type, so an adopted slot stops reaching the lane entirely."""
        printer = await printer_factory()
        await _attempt(db_session, printer.id)
        assert spool_tagless._autoconfig_epochs[(printer.id, 0, 1)].publishes == 1

        tray = _bare()
        tray["tray_type"] = "PETG"
        state = SimpleNamespace(state="IDLE", raw_data={"ams": [{"id": 0, "tray": [tray]}]})
        manager = SimpleNamespace(
            get_status=lambda pid: state,
            request_evidence_pushall=lambda pid, reason: True,
        )
        monkeypatch.setattr(spool_tagless.ams_presence, "maybe_command_owed_identify", AsyncMock(return_value=False))
        monkeypatch.setattr(spool_tagless.spool_respool, "detect_spent_contradictions", AsyncMock())

        await spool_tagless.reconcile_slot_config(db_session, manager=manager, now=env.clock.t)

        assert spool_tagless._autoconfig_epochs == {}
