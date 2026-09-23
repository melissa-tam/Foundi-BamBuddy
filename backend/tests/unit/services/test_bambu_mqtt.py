"""Tests for ``BambuMQTTClient`` — the LAN MQTT transport and its wire dialect.

What lives here: terminal/completion detection, AMS payload merging and
``tray_exist_bits`` presence authority, ``tray_now`` disambiguation per printer
family, request-topic mirroring, developer-mode probing, ``start_print`` command
shaping, HMS decode, reconnect handling, the AMS write guards, and the deposit
evidence a terminal hands to the plate gate.

Every client under test is built by ``_make_client``; a class states its own
differences in a ``client_kwargs`` class attribute, which the ``mqtt_client``
fixture reads.
"""

import ast
import asyncio
import copy
import inspect
import json
import logging
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import paho.mqtt.client as mqtt
import pytest
from paho.mqtt.reasoncodes import ReasonCode

from backend.app.services import bambu_mqtt as mqtt_mod
from backend.app.services.bambu_mqtt import (
    _AMS_REFRESH_REFUSAL_MESSAGE,
    _AMS_REFUSAL_LOG_TEXT,
    _ZERO_EXIST_BITS_TRUST_PUSHES,
    BambuMQTTClient,
    HMSError,
    ams_mid_filament_change,
    apply_tray_exist_bits,
)
from backend.app.services.plate_occupancy import DepositEvidence
from backend.app.services.tray_observation import observe_ams_push

_CLIENT_ADDRESS = {"ip_address": "192.168.1.100", "access_code": "12345678"}


def _make_client(*, serial="TEST123", connected=False, publish_target=False, tray_now=None, **kwargs):
    """THE client under test.

    ``connected`` installs the publish target as well, because publishing checks
    ``_client`` presence — a client that is "up" needs both; ``publish_target``
    alone gives one that can publish but still reads as disconnected. ``tray_now``
    seeds the fed slot, 255 being the "nothing loaded" the AMS load checks want.
    """
    client = BambuMQTTClient(serial_number=serial, **_CLIENT_ADDRESS, **kwargs)
    if connected or publish_target:
        client._client = MagicMock()
    if connected:
        client.state.connected = True
    if tray_now is not None:
        client.state.tray_now = tray_now
    return client


@pytest.fixture
def mqtt_client(request):
    """The client under test, built from the class's own ``client_kwargs``."""
    return _make_client(**getattr(request.cls, "client_kwargs", {}))


def _published_payloads(client):
    """Every payload the client published, parsed, in order.

    Not every frame is a print command — ``pushing``/``system`` topics ride the same
    publisher — so this returns the whole envelope.
    """
    return [json.loads(call.args[1]) for call in client._client.publish.call_args_list]


def _published_command(client, index=-1):
    """The ``print`` command of one published frame (the last one by default)."""
    return _published_payloads(client)[index]["print"]


class TestTimelapseTracking:
    """``_timelapse_during_print`` latches while a print runs and survives the stop.

    The flag is set from three wire locations because the field arrives in three:
    ``xcam.timelapse`` and ``ipcam.timelapse`` carry the string ``"enable"`` (the H2D
    reports it under ``ipcam`` and nowhere else), while the top-level
    ``print.timelapse`` is a real bool. All three only latch while ``_was_running``.
    """

    ENABLED = {
        "xcam": {"xcam": {"timelapse": "enable"}},
        "print_field": {"timelapse": True},
        "ipcam_h2d": {"ipcam": {"ipcam_record": "enable", "timelapse": "enable"}},
    }

    def test_timelapse_flag_initializes_to_false(self, mqtt_client):
        assert mqtt_client._timelapse_during_print is False

    @pytest.mark.parametrize("source", list(ENABLED), ids=[f"from_{k}" for k in ENABLED])
    @pytest.mark.parametrize("was_running", [True, False], ids=["printing", "idle"])
    def test_the_flag_latches_only_while_a_print_is_running(self, mqtt_client, source, was_running):
        """Enabled timelapse sets ``state.timelapse`` either way; only a RUNNING print
        latches ``_timelapse_during_print``, which is what the terminal reports."""
        mqtt_client._was_running = was_running

        mqtt_client._process_message({"print": self.ENABLED[source]})

        assert mqtt_client.state.timelapse is True
        assert mqtt_client._timelapse_during_print is was_running

    def test_timelapse_flag_persists_after_timelapse_stops(self, mqtt_client):
        """Recording stops at the end of the print, so the latch must outlive it —
        the terminal is fired after the ``disable`` arrives."""
        mqtt_client._was_running = True
        mqtt_client._parse_xcam_data({"timelapse": "enable"})
        assert mqtt_client._timelapse_during_print is True

        mqtt_client._parse_xcam_data({"timelapse": "disable"})

        assert mqtt_client.state.timelapse is False
        assert mqtt_client._timelapse_during_print is True


class TestPrintCompletionWithTimelapse:
    """The terminal payload carries the latch as ``timelapse_was_active``."""

    @pytest.mark.parametrize(
        "xcam, expected",
        [
            pytest.param({"timelapse": "enable"}, True, id="timelapse_ran"),
            pytest.param({"timelapse": "disable"}, False, id="timelapse_off"),
        ],
    )
    def test_the_terminal_reports_whether_timelapse_ran(self, mqtt_client, xcam, expected):
        complete_data = {}
        mqtt_client.on_print_start = lambda data: None
        mqtt_client.on_print_complete = complete_data.update
        mqtt_client._previous_gcode_state = "IDLE"

        mqtt_client._process_message(
            {"print": {"gcode_state": "RUNNING", "gcode_file": "/data/Metadata/test.gcode", "xcam": xcam}}
        )
        mqtt_client._process_message({"print": {"gcode_state": "FINISH", "gcode_file": "/data/Metadata/test.gcode"}})

        assert complete_data["timelapse_was_active"] is expected

    def test_the_latch_is_reset_for_the_next_print(self, mqtt_client):
        """A latch left standing would attribute this print's recording to the next one."""
        mqtt_client.on_print_start = lambda data: None
        mqtt_client.on_print_complete = lambda data: None
        mqtt_client._previous_gcode_state = "IDLE"

        mqtt_client._process_message(
            {
                "print": {
                    "gcode_state": "RUNNING",
                    "gcode_file": "/data/Metadata/test.gcode",
                    "xcam": {"timelapse": "enable"},
                }
            }
        )
        mqtt_client._process_message({"print": {"gcode_state": "FINISH", "gcode_file": "/data/Metadata/test.gcode"}})

        assert mqtt_client._timelapse_during_print is False
        assert mqtt_client._was_running is False


class TestRealisticMessageFlow:
    """Whole-message sequences through ``_process_message``, where the parse ORDER
    matters: xcam is parsed before the state transition is detected."""

    def test_timelapse_detected_at_print_start_in_same_message(self, mqtt_client):
        """The race the parse order creates: the first RUNNING push usually carries the
        xcam block too, and xcam is read BEFORE ``_was_running`` is set — so the latch
        has to be re-evaluated after the transition, not only at the xcam read."""
        mqtt_client.on_print_start = lambda data: None

        mqtt_client._process_message(
            {
                "print": {
                    "gcode_state": "RUNNING",
                    "gcode_file": "/data/Metadata/test_print.gcode",
                    "subtask_name": "Test_Print",
                    "xcam": {"timelapse": "enable", "printing_monitor": True},
                    "mc_percent": 0,
                    "mc_remaining_time": 3600,
                }
            }
        )

        assert mqtt_client._was_running is True
        assert mqtt_client.state.timelapse is True
        assert mqtt_client._timelapse_during_print is True, "same-message xcam must still latch"

    def test_timelapse_not_detected_when_disabled(self, mqtt_client):
        mqtt_client.on_print_start = lambda data: None

        mqtt_client._process_message(
            {
                "print": {
                    "gcode_state": "RUNNING",
                    "gcode_file": "/data/Metadata/test_print.gcode",
                    "xcam": {"timelapse": "disable", "printing_monitor": True},
                }
            }
        )

        assert mqtt_client._was_running is True
        assert mqtt_client.state.timelapse is False
        assert mqtt_client._timelapse_during_print is False

    def test_timelapse_detected_when_enabled_after_print_start(self, mqtt_client):
        """The operator can enable timelapse mid-print: a later xcam push latches too."""
        mqtt_client.on_print_start = lambda data: None
        running = {"print": {"gcode_state": "RUNNING", "gcode_file": "/data/Metadata/test_print.gcode"}}

        mqtt_client._process_message(running)
        assert mqtt_client._was_running is True
        assert mqtt_client._timelapse_during_print is False

        mqtt_client._process_message({"print": {**running["print"], "xcam": {"timelapse": "enable"}}})

        assert mqtt_client._timelapse_during_print is True

    @pytest.mark.parametrize(
        "terminal, status",
        [pytest.param("FINISH", "completed", id="finish"), pytest.param("FAILED", "failed", id="failed")],
    )
    def test_the_flag_survives_a_whole_print_to_either_terminal(self, mqtt_client, terminal, status):
        """Full lifecycle: the latch has to ride every mid-print push that carries no
        xcam block at all, and reach both terminals."""
        start_data, complete_data = {}, {}
        mqtt_client.on_print_start = start_data.update
        mqtt_client.on_print_complete = complete_data.update
        # A prior state, so the first RUNNING push is a real transition and not a
        # Bambuddy-restart catch-up (#1304).
        mqtt_client._previous_gcode_state = "IDLE"
        job = {"gcode_file": "/data/Metadata/test.gcode", "subtask_name": "Test"}

        mqtt_client._process_message({"print": {**job, "gcode_state": "RUNNING", "xcam": {"timelapse": "enable"}}})
        assert mqtt_client._timelapse_during_print is True
        assert "subtask_name" in start_data

        for _ in range(3):
            mqtt_client._process_message({"print": {**job, "gcode_state": "RUNNING", "mc_percent": 50}})
        assert mqtt_client._timelapse_during_print is True

        mqtt_client._process_message({"print": {**job, "gcode_state": terminal}})

        assert complete_data["timelapse_was_active"] is True
        assert complete_data["status"] == status
        assert mqtt_client._timelapse_during_print is False
        assert mqtt_client._was_running is False


class TestPrePrintFailureCompletion:
    """A print that dies before RUNNING is still a terminal (#1111).

    A file sliced for the wrong nozzle diameter takes the printer
    IDLE → PREPARE → FAILED without ever entering RUNNING; completion detection that
    required RUNNING left the queue item at ``printing`` forever. What makes the
    difference decidable is the state the failure came FROM: the setup states are a
    dispatched job, IDLE and a first-push-after-connect are not.
    """

    @staticmethod
    def _terminals(client):
        calls: list[dict] = []
        client.on_print_start = lambda data: None
        client.on_print_complete = calls.append
        return calls

    @pytest.mark.parametrize(
        "previous_state, fires",
        [
            pytest.param("PREPARE", True, id="prepare_is_a_dispatched_job"),
            pytest.param("SLICING", True, id="slicing_is_a_dispatched_job"),
            pytest.param("IDLE", False, id="idle_never_dispatched"),
            # A stale FAILED on the first push after Bambuddy starts must not be read as
            # a fresh failure and fail an unrelated queue item.
            pytest.param(None, False, id="first_push_after_connect_is_stale"),
        ],
    )
    def test_only_a_setup_state_makes_failed_a_terminal(self, mqtt_client, previous_state, fires):
        calls = self._terminals(mqtt_client)
        mqtt_client._previous_gcode_state = previous_state
        assert mqtt_client._was_running is False

        mqtt_client._process_message(
            {"print": {"gcode_state": "FAILED", "gcode_file": "/data/Metadata/plate_1.gcode", "subtask_name": "X"}}
        )

        assert [c["status"] for c in calls] == (["failed"] if fires else [])

    def test_the_failure_terminal_carries_the_hms_list(self, mqtt_client):
        """The queue handler builds its error_message from these codes, and the
        rejection arrives in the SAME push as the PREPARE → FAILED transition."""
        calls = self._terminals(mqtt_client)
        mqtt_client._previous_gcode_state = "PREPARE"

        mqtt_client._process_message(
            {
                "print": {
                    "gcode_state": "FAILED",
                    "gcode_file": "/data/Metadata/plate_1.gcode",
                    "hms": [{"attr": 0x05000000, "code": 0x4038}],  # nozzle-size mismatch
                }
            }
        )

        assert calls[-1]["status"] == "failed"
        assert any(e.get("code") == "0x4038" for e in calls[-1].get("hms_errors") or [])


class TestJobBoundaryCompletionReset:
    """The per-job flags reset on the JOB BOUNDARY, not on RUNNING.

    ``_completion_triggered`` cleared only under a RUNNING push means a job that dies
    in PREPARE inherits the PREVIOUS print's flag, so the #1111 pre-print-failure arm
    can never fire after a completed print — a rejected eject then produces NO terminal
    at all and the plate escalates as a foreign deposit.

    Every push here is a real ``_process_message``, so the flags are only ever moved by
    the code under test, and the sequences are the ones the printer actually produces.
    """

    @staticmethod
    def _recorder(client):
        """Capture every terminal the client fires, in order."""
        calls: list[dict] = []
        client.on_print_start = lambda data: None
        client.on_print_complete = lambda data: calls.append(data)
        return calls

    @staticmethod
    def _push(client, state, *, file=None, subtask=None, hms=None):
        payload: dict = {"gcode_state": state}
        if file is not None:
            payload["gcode_file"] = file
        if subtask is not None:
            payload["subtask_name"] = subtask
        if hms is not None:
            payload["hms"] = hms
        client._process_message({"print": payload})

    def _run_a_print_to_finish(self, client):
        """The predecessor every eject follows: RUNNING then FINISH."""
        self._push(client, "RUNNING", file="/data/Metadata/plate_1.gcode", subtask="Unit-2200")
        self._push(client, "FINISH", file="/data/Metadata/plate_1.gcode", subtask="Unit-2200")

    def test_finish_then_prepare_then_failed_fires_the_failure_once(self, mqtt_client):
        """THE incident shape: the eject the printer refused at setup is a TERMINAL."""
        calls = self._recorder(mqtt_client)
        self._run_a_print_to_finish(mqtt_client)

        self._push(mqtt_client, "PREPARE", file="/data/Metadata/eject.gcode", subtask="eject_production_item2200")
        self._push(
            mqtt_client,
            "FAILED",
            file="/data/Metadata/eject.gcode",
            subtask="eject_production_item2200",
            hms=[{"attr": 0x05000400, "code": 0x00010003}],
        )

        assert [c["status"] for c in calls] == ["completed", "failed"]
        failed = calls[-1]
        assert failed["subtask_name"] == "eject_production_item2200"
        # The codes ride the payload — farm_policy pages with them.
        assert failed["hms_errors"], "the rejection's HMS list must reach the terminal callback"

    def test_a_second_failed_push_does_not_fire_again(self, mqtt_client):
        """The flag is per JOB: the firmware republishes FAILED, the farm reacts once."""
        calls = self._recorder(mqtt_client)
        self._run_a_print_to_finish(mqtt_client)
        self._push(mqtt_client, "PREPARE", file="/data/Metadata/eject.gcode")
        self._push(mqtt_client, "FAILED", file="/data/Metadata/eject.gcode")
        assert [c["status"] for c in calls] == ["completed", "failed"]

        self._push(mqtt_client, "FAILED", file="/data/Metadata/eject.gcode")

        assert [c["status"] for c in calls] == ["completed", "failed"]  # unchanged

    def test_two_rejected_dispatches_fire_two_terminals(self, mqtt_client):
        """The operator's retry is a NEW job, and it gets its own terminal.

        On 2026-09-17 the operator re-pressed "Eject plate" eight times; every one of
        them was silent. FAILED → PREPARE is a boundary like any other."""
        calls = self._recorder(mqtt_client)
        self._run_a_print_to_finish(mqtt_client)

        for _ in range(2):
            self._push(mqtt_client, "PREPARE", file="/data/Metadata/eject.gcode")
            self._push(mqtt_client, "FAILED", file="/data/Metadata/eject.gcode")

        assert [c["status"] for c in calls] == ["completed", "failed", "failed"]

    def test_a_healthy_next_print_still_fires_exactly_one_terminal(self, mqtt_client):
        """Liveness pair: FINISH → PREPARE → RUNNING → FINISH is unchanged.

        The reset must not fabricate a terminal or double one — the ordinary job that
        does reach RUNNING still ends with exactly one completion."""
        calls = self._recorder(mqtt_client)
        self._run_a_print_to_finish(mqtt_client)

        self._push(mqtt_client, "PREPARE", file="/data/Metadata/plate_2.gcode", subtask="Unit-2201")
        self._push(mqtt_client, "RUNNING", file="/data/Metadata/plate_2.gcode", subtask="Unit-2201")
        self._push(mqtt_client, "FINISH", file="/data/Metadata/plate_2.gcode", subtask="Unit-2201")

        assert [c["status"] for c in calls] == ["completed", "completed"]
        assert calls[-1]["subtask_name"] == "Unit-2201"

    def test_first_connect_failed_still_never_fires(self, mqtt_client):
        """The #1111 guard survives the reset: no PREPARE was seen, so no boundary.

        A stale FAILED on the first push after Bambuddy starts must not be mistaken for
        a fresh failure — the reset only fires on entry INTO setup."""
        calls = self._recorder(mqtt_client)
        assert mqtt_client._previous_gcode_state is None

        self._push(mqtt_client, "FAILED", file="/data/Metadata/plate_1.gcode", subtask="Stale")

        assert calls == []

    def test_a_boundary_after_an_unfired_terminal_still_fires(self, mqtt_client):
        """Composition with the diagnostic branch, which sets the flag on a terminal it
        did NOT fire (so the next print starts clean). That write must not outlive the
        job either: the PREPARE after it is a boundary and clears it."""
        calls = self._recorder(mqtt_client)
        self._push(mqtt_client, "FAILED", file="/data/Metadata/plate_1.gcode")  # prev=None → unfired
        assert calls == []
        assert mqtt_client._completion_triggered is True  # the diagnostic branch's mark

        self._push(mqtt_client, "PREPARE", file="/data/Metadata/eject.gcode")
        self._push(mqtt_client, "FAILED", file="/data/Metadata/eject.gcode")

        assert [c["status"] for c in calls] == ["failed"]

    def test_pause_is_not_a_boundary(self, mqtt_client):
        """A PAUSEd job is the SAME job: its flags must survive.

        PREPARE is never re-entered from PAUSE in the field, but the exclusion is the
        reason the set names four states rather than two — it says what an ACTIVE job
        is, and a paused print is one."""
        self._push(mqtt_client, "RUNNING", file="/data/Metadata/plate_1.gcode")
        self._push(mqtt_client, "PAUSE", file="/data/Metadata/plate_1.gcode")
        assert mqtt_client._was_running is True

        self._push(mqtt_client, "PREPARE", file="/data/Metadata/plate_1.gcode")

        assert mqtt_client._was_running is True  # still the same job


class TestAMSDataMerging:
    """``_handle_ams_data`` merge semantics: what a push may overwrite, and what
    ``tray_exist_bits`` is allowed to decide (cross-cutting invariant 12)."""

    def test_empty_slot_clears_tray_type(self, mqtt_client):
        """An OLD AMS reports a removal as empty CONTENT, and those empty values must
        overwrite — a merge that only takes truthy fields shows a spool that is gone
        (#147)."""
        mqtt_client._handle_ams_data(
            {
                "ams": [
                    {
                        "id": 0,
                        "tray": [
                            {
                                "id": 0,
                                "tray_type": "PLA",
                                "tray_sub_brands": "Bambu PLA Basic",
                                "tray_color": "FF0000",
                                "tag_uid": "1234567890ABCDEF",
                                "remain": 80,
                            }
                        ],
                    }
                ]
            }
        )
        assert mqtt_client.state.raw_data["ams"][0]["tray"][0]["tray_type"] == "PLA"

        mqtt_client._handle_ams_data(
            {
                "ams": [
                    {
                        "id": 0,
                        "tray": [
                            {
                                "id": 0,
                                "tray_type": "",
                                "tray_sub_brands": "",
                                "tray_color": "",
                                "tag_uid": "0000000000000000",
                                "remain": 0,
                            }
                        ],
                    }
                ]
            }
        )

        tray = mqtt_client.state.raw_data["ams"][0]["tray"][0]
        assert tray["tray_type"] == ""
        assert tray["tray_color"] == ""
        assert tray["tray_sub_brands"] == ""
        assert tray["tag_uid"] == "0000000000000000"

    def test_partial_update_preserves_other_fields(self, mqtt_client):
        """The ~1 Hz push carries only what changed, so a field the push omits must
        survive the merge."""
        mqtt_client._handle_ams_data(
            {
                "ams": [
                    {
                        "id": 0,
                        "humidity": "3",
                        "temp": "25.5",
                        "tray": [{"id": 0, "tray_type": "PLA", "tray_color": "00FF00", "remain": 90, "k": 0.02}],
                    }
                ]
            }
        )

        mqtt_client._handle_ams_data({"ams": [{"id": 0, "tray": [{"id": 0, "remain": 85}]}]})

        tray = mqtt_client.state.raw_data["ams"][0]["tray"][0]
        assert tray["remain"] == 85
        assert tray["tray_type"] == "PLA"
        assert tray["tray_color"] == "00FF00"
        assert tray["k"] == 0.02

    # An AMS 2 Pro reports a removal ONLY through tray_exist_bits: the tray dict keeps
    # arriving populated (or as a bare {"id": N}) and nothing in its content says empty.
    # `power_on_flag` is carried in these rows because two shipped guards keyed on it
    # (#765, narrowed by #1365) and both were wrong: False is the ordinary steady state
    # of a healthy idle AMS across the fleet, so a guard keyed on it discards true
    # all-empty reports indefinitely. The flag is recorded and never acted on.
    @pytest.mark.parametrize(
        "extra_fields",
        [
            pytest.param({"power_on_flag": True}, id="invariant12_clear_bit_releases_power_flag_true"),
            pytest.param(
                {"power_on_flag": False, "insert_flag": True}, id="invariant12_clear_bit_releases_idle_flag_false"
            ),
            pytest.param({}, id="invariant12_clear_bit_releases_no_flag_at_all"),
        ],
    )
    def test_a_clear_bit_empties_the_slot_whatever_the_power_flag_says(self, mqtt_client, extra_fields):
        loaded = {
            "ams": [
                {
                    "id": 0,
                    "tray": [
                        {"id": 0, "tray_type": "PLA", "tray_color": "FF0000FF", "state": 11, "remain": 80},
                        {"id": 1, "tray_type": "PETG", "tray_color": "00FF00FF", "state": 11, "remain": 60},
                    ],
                }
            ],
            "tray_exist_bits": "3",  # 0b11 — both occupied
            **extra_fields,
        }
        mqtt_client._handle_ams_data(loaded)
        assert mqtt_client.state.raw_data["ams"][0]["tray"][1]["tray_type"] == "PETG"

        mqtt_client._handle_ams_data(
            {"ams": [{"id": 0, "tray": [{"id": 0}, {"id": 1}]}], "tray_exist_bits": "1", **extra_fields}
        )

        trays = mqtt_client.state.raw_data["ams"][0]["tray"]
        assert trays[1]["tray_type"] == ""
        assert trays[1]["tray_color"] == ""
        assert trays[1]["remain"] == 0
        # int 9, not "9": downstream `tray_state in {9, 10}` compares with ==.
        assert trays[1]["state"] == 9
        assert isinstance(trays[1]["state"], int)
        # The slot whose bit stayed set keeps content AND firmware state.
        assert trays[0]["tray_type"] == "PLA"
        assert trays[0]["state"] == 11

    def test_the_mask_addresses_a_second_unit_by_its_high_nibble(self, mqtt_client):
        """global_bit = ams_id * 4 + tray_id at the merge level too: 0x7f clears AMS 1
        slot 3 (bit 7) and nothing else."""
        mqtt_client._handle_ams_data(
            {
                "ams": [
                    {"id": 0, "tray": [{"id": i, "tray_type": "PLA", "remain": 80} for i in range(4)]},
                    {"id": 1, "tray": [{"id": i, "tray_type": "PETG", "remain": 60} for i in range(4)]},
                ],
                "tray_exist_bits": "ff",
            }
        )
        assert mqtt_client.state.raw_data["ams"][1]["tray"][3]["tray_type"] == "PETG"

        mqtt_client._handle_ams_data(
            {
                "ams": [
                    {"id": 0, "tray": [{"id": i} for i in range(4)]},
                    {"id": 1, "tray": [{"id": i} for i in range(4)]},
                ],
                "tray_exist_bits": "7f",
            }
        )

        ams = mqtt_client.state.raw_data["ams"]
        assert ams[1]["tray"][3]["tray_type"] == ""
        assert ams[1]["tray"][3]["remain"] == 0
        assert ams[0]["tray"][0]["tray_type"] == "PLA"
        assert ams[1]["tray"][0]["tray_type"] == "PETG"

    def test_a_set_bit_never_rewrites_a_transitional_state(self, mqtt_client):
        """The promotion fires only on a CLEAR bit. A loaded slot reporting a
        transitional firmware state (3 = unloading here) must pass through untouched."""
        mqtt_client._handle_ams_data(
            {
                "ams": [{"id": 0, "tray": [{"id": 0, "tray_type": "PLA", "state": 3, "remain": 80}]}],
                "tray_exist_bits": "1",
            }
        )

        assert mqtt_client.state.raw_data["ams"][0]["tray"][0]["state"] == 3

    def test_shutdown_message_preserves_ams_data(self, mqtt_client):
        """A printer shutting down sends a final push with ``tray_exist_bits='0'`` and
        NO ``ams`` list at all. What protects the slots is that last fact — a push
        describing no trays reaches no merge — not the flag beside it (#765)."""
        mqtt_client._handle_ams_data(
            {
                "ams": [
                    {"id": 0, "tray": [{"id": 0, "tray_type": "PLA", "tray_color": "FF0000FF", "remain": 80}]},
                    {"id": 1, "tray": [{"id": 0, "tray_type": "PETG", "tray_color": "DBDDD9FF", "remain": 90}]},
                ],
                "tray_exist_bits": "11",
                "power_on_flag": True,
            }
        )

        mqtt_client._handle_ams_data(
            {
                "ams_exist_bits": "0",
                "tray_exist_bits": "0",
                "power_on_flag": False,
                "insert_flag": False,
                "tray_now": "0",
                "version": 0,
            }
        )

        ams = mqtt_client.state.raw_data["ams"]
        assert ams[0]["tray"][0]["tray_type"] == "PLA"
        assert ams[0]["tray"][0]["tray_color"] == "FF0000FF"
        assert ams[1]["tray"][0]["tray_type"] == "PETG"

    def test_an_all_zero_mask_clears_once_it_has_repeated(self, mqtt_client):
        """The trust ladder at the merge level: a single all-zero mask is not acted on —
        that value is what a boot frame or a truncated report degrades to, and it
        authorizes emptying every slot. After ``_ZERO_EXIST_BITS_TRUST_PUSHES``
        consecutive pushes say it, the slot is cleared to the full cleared shape.
        """
        mqtt_client._handle_ams_data(
            {
                "ams": [{"id": 0, "tray": [{"id": 0, "tray_type": "PLA", "tray_color": "FF0000", "remain": 80}]}],
                "tray_exist_bits": "1",
            }
        )

        zero_push = {"ams": [{"id": 0, "tray": [{"id": 0}]}], "tray_exist_bits": "0"}
        for _ in range(_ZERO_EXIST_BITS_TRUST_PUSHES - 1):
            mqtt_client._handle_ams_data(zero_push)
            assert mqtt_client.state.raw_data["ams"][0]["tray"][0]["tray_type"] == "PLA", (
                "an unrepeated all-zero mask must not empty the slot"
            )
            assert mqtt_client.state.ams_bits_trusted is False

        mqtt_client._handle_ams_data(zero_push)
        tray = mqtt_client.state.raw_data["ams"][0]["tray"][0]
        assert mqtt_client.state.ams_bits_trusted is True
        assert tray["tray_type"] == ""
        assert tray["state"] == 9
        assert tray["remain"] == 0

    def test_one_anomalous_zero_frame_never_empties_a_slot(self, mqtt_client):
        """The streak is CONSECUTIVE: 'f' … '0' … 'f' leaves the slot untouched."""
        loaded = {
            "ams": [{"id": 0, "tray": [{"id": 0, "state": 11, "tray_type": "PLA", "remain": 80}]}],
            "tray_exist_bits": "f",
        }
        mqtt_client._handle_ams_data(loaded)
        mqtt_client._handle_ams_data({"ams": [{"id": 0, "tray": [{"id": 0}]}], "tray_exist_bits": "0"})
        mqtt_client._handle_ams_data(loaded)
        assert mqtt_client.state.raw_data["ams"][0]["tray"][0]["tray_type"] == "PLA"
        assert mqtt_client._zero_exist_bits_streak == 0

    def test_the_status_surface_reports_what_the_wire_said(self, mqtt_client):
        """Triage fields: the raw hex, the firmware's flag, and OUR verdict — the flag
        is RECORDED, never acted on."""
        mqtt_client._handle_ams_data(
            {
                "ams": [{"id": 0, "tray": [{"id": 0, "state": 11, "tray_type": "PLA"}]}],
                "tray_exist_bits": "2",
                "power_on_flag": False,
            }
        )
        assert mqtt_client.state.ams_tray_exist_bits == "2"
        assert mqtt_client.state.ams_power_on_flag is False
        assert mqtt_client.state.ams_bits_trusted is True, "a set bit is believed at once, flag or no flag"


class TestAMSTrayStateClearning:
    """A ``{id, state}``-only tray push decides presence, and presence preserves
    identity (#784, cross-cutting invariant 3).

    Some printers (the H2D here) send nothing but ``{id, state}`` in incremental
    updates whenever a tray is not fully loaded. 11 = loaded and 10 = present but not
    fed are both PRESENT and must keep the identity an earlier pushall established;
    9 and the other values are not presence and clear the stale content. Wiping a
    state-10 tray is what drove the AMS-drying incident — drying deliberately
    disengages trays to state 10 (HMS 0700_C069) — and it wipes routine load/unload
    transit too.
    """

    client_kwargs = {"serial": "TEST_H2D"}

    IDENTITY = {
        "tray_type": "PETG",
        "tray_sub_brands": "PETG HF",
        "tray_color": "00FF00FF",
        "tray_id_name": "A00-G1",
        "tray_info_idx": "GFG99",
        "tag_uid": "AABBCCDD11223344",
        "tray_uuid": "AABBCCDD11223344AABBCCDD11223344",
        "remain": 75,
    }

    def _seed_loaded_tray(self, mqtt_client):
        """AMS 0 with a fully identified tray in slot 0 and a plain one in slot 1."""
        mqtt_client._handle_ams_data(
            {
                "ams": [
                    {
                        "id": 0,
                        "tray": [
                            {"id": 0, "state": 11, "k": 0.02, "cali_idx": 5, **self.IDENTITY},
                            {"id": 1, "state": 11, "tray_type": "PLA", "tray_color": "FF0000FF", "remain": 50},
                        ],
                    }
                ],
                "power_on_flag": False,  # the H2D always sends False
            }
        )
        assert mqtt_client.state.raw_data["ams"][0]["tray"][0]["tray_type"] == "PETG"

    def _push_state(self, mqtt_client, state):
        mqtt_client._handle_ams_data(
            {
                "ams": [{"id": 0, "tray": [{"id": 0, "state": state}, {"id": 1, "state": 11}]}],
                "power_on_flag": False,
            }
        )
        return mqtt_client.state.raw_data["ams"][0]["tray"]

    @pytest.mark.parametrize(
        "state, keeps_identity",
        [
            pytest.param(11, True, id="state11_loaded_is_presence"),
            pytest.param(10, True, id="state10_present_not_fed_is_presence"),
            pytest.param(9, False, id="state9_empty_is_not_presence"),
        ],
    )
    def test_presence_states_preserve_identity_and_the_rest_clear_it(self, mqtt_client, state, keeps_identity):
        self._seed_loaded_tray(mqtt_client)

        trays = self._push_state(mqtt_client, state)

        assert trays[0]["state"] == state
        if keeps_identity:
            for field, value in self.IDENTITY.items():
                assert trays[0][field] == value, f"{field} must survive a presence state"
        else:
            assert trays[0]["tray_type"] == ""
            assert trays[0]["remain"] == 0
        # The other slot is never collateral.
        assert trays[1]["tray_type"] == "PLA"
        assert trays[1]["remain"] == 50

    def test_no_clearing_when_tray_type_already_empty(self, mqtt_client):
        """Re-clearing an already-empty tray is a no-op (it would log on every push)."""
        self._seed_loaded_tray(mqtt_client)
        assert self._push_state(mqtt_client, 9)[0]["tray_type"] == ""

        assert self._push_state(mqtt_client, 9)[0]["tray_type"] == ""

    def test_reload_after_unload_restores_data(self, mqtt_client):
        """A cleared slot is not a latch: the next full tray payload restores it."""
        self._seed_loaded_tray(mqtt_client)
        self._push_state(mqtt_client, 9)

        mqtt_client._handle_ams_data(
            {
                "ams": [{"id": 0, "tray": [{"id": 0, "state": 11, **self.IDENTITY}, {"id": 1, "state": 11}]}],
                "power_on_flag": False,
            }
        )

        tray0 = mqtt_client.state.raw_data["ams"][0]["tray"][0]
        assert tray0["tray_type"] == "PETG"
        assert tray0["tray_color"] == "00FF00FF"
        assert tray0["remain"] == 75


_DEFAULT = object()  # call apply_tray_exist_bits without allow_demote, pinning its default


class TestApplyTrayExistBitsHelper:
    """Direct contract pinning for ``apply_tray_exist_bits`` — the ONE place
    ``tray_exist_bits`` is turned into per-tray presence (cross-cutting invariant 12).

    The same logic runs end-to-end through ``_handle_ams_data`` and through the
    bridge's ``_on_printer_raw``, but both go via merge/cache layers; the helper is
    pinned directly so a refactor cannot quietly change the contract they share
    (#1726). ``allow_demote`` is the fresh-vs-cached axis: bits carried by THIS push
    have full authority, a cached mask may promote but never demote.
    """

    @pytest.mark.parametrize(
        "bits",
        [
            pytest.param(None, id="bits_absent"),
            pytest.param("", id="bits_empty_string"),
            pytest.param("garbage", id="bits_unparseable"),
        ],
    )
    def test_bits_that_say_nothing_change_nothing(self, bits):
        """Unknown is not empty: a mask the helper cannot read must take no evidence."""
        units = [{"id": 0, "tray": [{"id": 0, "tray_type": "PLA"}]}]

        assert apply_tray_exist_bits(units, bits) == 0
        assert units[0]["tray"][0]["tray_type"] == "PLA"

    def test_power_on_flag_is_not_a_parameter(self):
        """The #765 "printer shutdown" guard is gone, premise and all: it skipped an
        all-zero mask whenever ``power_on_flag`` was False, and that flag reads False
        as the ordinary steady state on a healthy answering AMS, so the guard
        discarded true all-empty reports indefinitely. Whether a zero mask is
        TRUSTED is a question about the push history, settled by the client before
        it calls here (``_note_exist_bits_trust``); this helper applies what it is
        given."""
        assert "power_on_flag" not in inspect.signature(apply_tray_exist_bits).parameters

    @pytest.mark.parametrize(
        "units, bits, allow_demote, cleared, expect",
        [
            # --- the mask's own readings -------------------------------------------
            pytest.param(
                [{"id": 0, "tray": [{"id": 0, "state": 11, "tray_type": "PLA"}]}],
                0,
                _DEFAULT,
                1,
                [{"state": 9}],
                id="invariant12_int_zero_mask_is_a_real_answer_not_an_absent_one",
            ),
            pytest.param(
                [
                    {
                        "id": 0,
                        "tray": [
                            {"id": 0, "tray_type": "PLA", "tray_color": "FF0000FF"},
                            {"id": 1, "tray_type": "PETG", "tray_color": "00FF00FF"},
                        ],
                    }
                ],
                "1",
                _DEFAULT,
                1,
                [{"tray_type": "PLA"}, {"tray_type": ""}],
                id="a_partial_mask_clears_only_its_clear_bits",
            ),
            pytest.param(
                [{"id": 0, "tray": [{"id": "0", "tray_type": "PLA"}, {"id": "1", "tray_type": "PETG"}]}],
                "1",
                _DEFAULT,
                1,
                [{"tray_type": "PLA"}, {"tray_type": ""}],
                id="string_wire_ids_are_addressed_the_same",
            ),
            # A clear bit forces int 9 — downstream `tray_state in {9, 10}` compares
            # with ==, so a string would silently miss. A promotion/wipe of a tray that
            # carried no content is still not a WIPE: the counter stays 0.
            pytest.param(
                [{"id": 0, "tray": [{"id": 0, "state": "11"}]}],
                "0",
                _DEFAULT,
                0,
                [{"state": 9}],
                id="clear_bit_forces_int_nine_and_counts_no_wipe",
            ),
            pytest.param(
                [{"id": 128, "tray": [{"id": 0, "tray_type": "PLA"}]}],
                "0",
                _DEFAULT,
                0,
                [{"tray_type": "PLA"}],
                id="ams_ht_id_128_is_out_of_this_masks_reach",
            ),
            # --- the promotion direction (003-H2S: a mid-print insert gets no auto-read,
            # so the tray sits at 9 while the mask already reports it occupied) --------
            pytest.param(
                [{"id": 0, "tray": [{"id": 2, "state": 9}]}],
                "4",
                _DEFAULT,
                0,
                [{"state": 10}],
                id="invariant12_set_bit_promotes_stuck_nine_to_ten",
            ),
            pytest.param(
                [{"id": 0, "tray": [{"id": 0, "state": "9", "tray_type": "", "tag_uid": "0000000000000000"}]}],
                "1",
                _DEFAULT,
                None,
                [{"state": 10, "tray_type": "", "tag_uid": "0000000000000000"}],
                id="promotion_takes_the_string_form_and_touches_no_identity",
            ),
            pytest.param(
                [{"id": 0, "tray": [{"id": 0, "state": 0}]}],
                "1",
                _DEFAULT,
                None,
                [{"state": 0}],
                id="state_zero_is_the_h2c_long_idle_dialect_not_a_stuck_nine",
            ),
            pytest.param(
                [{"id": 0, "tray": [{"id": 0, "state": 11}, {"id": 1, "state": 10}]}],
                "3",
                _DEFAULT,
                None,
                [{"state": 11}, {"state": 10}],
                id="a_set_bit_leaves_the_present_states_alone",
            ),
            pytest.param(
                [{"id": 0, "tray": [{"id": 0, "state": 9}]}],
                None,
                _DEFAULT,
                0,
                [{"state": 9}],
                id="no_mask_no_promotion",
            ),
            # --- fresh vs cached: the allow_demote asymmetry ------------------------
            pytest.param(
                [{"id": 0, "tray": [{"id": 0, "state": 11, "tray_type": "PETG", "remain": 75}]}],
                "0",
                True,
                1,
                [{"state": 9, "tray_type": "", "remain": 0}],
                id="invariant12_fresh_clear_bit_demotes_and_wipes_a_present_tray",
            ),
            pytest.param(
                [{"id": 0, "tray": [{"id": 0, "state": 10, "tray_type": "PETG", "remain": 75}]}],
                "0",
                False,
                0,
                [{"state": 10, "tray_type": "PETG", "remain": 75}],
                id="invariant12_cached_bits_never_demote_seated_state_ten",
            ),
            pytest.param(
                [
                    {
                        "id": 0,
                        "tray": [
                            {"id": 0, "state": 11, "tray_type": "PLA"},
                            {"id": 1, "state": "11", "tray_type": "PETG"},
                        ],
                    }
                ],
                "0",
                False,
                0,
                # Untouched means untouched — the string state is not even normalized.
                [{"state": 11, "tray_type": "PLA"}, {"state": "11", "tray_type": "PETG"}],
                id="invariant12_cached_bits_never_demote_seated_state_eleven",
            ),
            pytest.param(
                [{"id": 0, "tray": [{"id": 0, "state": 9, "tray_type": "", "remain": 0}]}],
                "0",
                False,
                0,
                [{"state": 9, "tray_type": ""}],
                id="cached_bits_over_an_asserted_empty_tray_are_an_idempotent_noop",
            ),
            pytest.param(
                [{"id": 0, "tray": [{"id": 0, "state": 9, "tray_type": "PETG", "remain": 75}]}],
                "0",
                False,
                1,
                [{"state": 9, "tray_type": "", "remain": 0}],
                id="cached_bits_still_clear_a_tray_that_asserts_no_presence",
            ),
            # The asymmetry is deliberate: a stale SET bit at worst delays a removal by
            # one push, while a stale CLEAR bit blinds the farm to a physical insert.
            pytest.param(
                [{"id": 0, "tray": [{"id": 0, "state": 9}, {"id": 1, "state": 9}]}],
                "1",
                False,
                0,
                [{"state": 10}, {"state": 9}],
                id="invariant12_cached_bits_still_promote_a_stuck_nine",
            ),
        ],
    )
    def test_the_mask_and_the_tray_state_decide_presence(self, units, bits, allow_demote, cleared, expect):
        kwargs = {} if allow_demote is _DEFAULT else {"allow_demote": allow_demote}

        count = apply_tray_exist_bits(units, bits, **kwargs)

        if cleared is not None:
            assert count == cleared
        for tray, wanted in zip(units[0]["tray"], expect, strict=True):
            for field, value in wanted.items():
                assert tray[field] == value, f"{field}"
                assert type(tray[field]) is type(value), f"{field} type"

    def test_a_unit_absent_from_ams_exist_bits_takes_no_evidence(self):
        """Its slice of the tray mask is zero because it is not being described, not
        because its trays are bare — reading those zeros would invent a release. An
        ABSENT ``ams_exist_bits`` gates nothing, because unknown fails open."""
        units = [
            {"id": 0, "tray": [{"id": 0, "state": 11, "tray_type": "PLA"}]},
            {"id": 1, "tray": [{"id": 0, "state": 11, "tray_type": "PETG"}]},
        ]

        assert apply_tray_exist_bits(units, "0", ams_exist_bits="1") == 1
        assert units[0]["tray"][0]["tray_type"] == ""
        assert units[1]["tray"][0]["tray_type"] == "PETG", "unit 1 was never described"

        assert apply_tray_exist_bits(units, "0") == 1
        assert units[1]["tray"][0]["tray_type"] == ""

    def test_multi_ams_global_bit_math(self):
        """global_bit = ams_id * 4 + tray_id: AMS 1 reads bits 4-7, not 0-3."""
        units = [
            {"id": 0, "tray": [{"id": i, "tray_type": "PLA"} for i in range(4)]},
            {"id": 1, "tray": [{"id": i, "tray_type": "PETG"} for i in range(4)]},
        ]

        cleared = apply_tray_exist_bits(units, "f")  # AMS 0 all occupied, AMS 1 all empty

        assert cleared == 4
        for i in range(4):
            assert units[0]["tray"][i]["tray_type"] == "PLA"
            assert units[1]["tray"][i]["tray_type"] == ""


class TestAmsCachedExistBitsNeverDemote:
    """`_handle_ams_data` call-site level: the cached-bitmask fallback may promote
    but never demote (2026-08-07 001-H2S slot 1 — the farm was blind to a physical
    spool insert for 38 minutes because every push that omitted tray_exist_bits
    re-demoted the seated tray using bits cached while the slot was still empty).
    """

    client_kwargs = {"serial": "TEST_H2S"}

    @staticmethod
    def _tray(mqtt_client, tray_id, ams_id=0):
        """Merged tray by ID — never by position: a partial push rebuilds the unit's
        tray list from the trays IT carries, so positions are not stable."""
        unit = next(u for u in mqtt_client.state.raw_data["ams"] if u.get("id") == ams_id)
        return next(t for t in unit["tray"] if t.get("id") == tray_id)

    @staticmethod
    def _seed(mqtt_client, bits):
        """Full push: slot 0 loaded, slot 1 empty — with `bits` on the wire."""
        mqtt_client._handle_ams_data(
            {
                "ams": [
                    {
                        "id": 0,
                        "tray": [
                            {"id": 0, "tray_type": "PLA", "tray_color": "FF0000FF", "remain": 50, "state": 11},
                            {"id": 1, "tray_type": "", "remain": 0, "state": 9},
                        ],
                    }
                ],
                "tray_exist_bits": bits,
                "power_on_flag": True,
            }
        )

    def test_insert_survives_pushes_that_omit_tray_exist_bits(self, mqtt_client):
        """Incident repro. (a) bits "1" while slot 1 is empty seeds the cache with
        that slot's bit CLEAR. (b) a partial that omits tray_exist_bits and asserts
        state 10 for slot 1 (the physical insert) must keep state 10, and the
        AMS-change callback must fire because the presence token flipped a→p."""
        changes = []
        mqtt_client.on_ams_change = changes.append

        self._seed(mqtt_client, "1")  # bit 0 set, bit 1 clear
        assert mqtt_client._last_tray_exist_bits == 1
        assert self._tray(mqtt_client, 1)["state"] == 9
        seeded_changes = len(changes)

        # Physical insert into slot 1. H2S's follow-up pushes carry no bitmask.
        mqtt_client._handle_ams_data(
            {
                "ams": [
                    {
                        "id": 0,
                        "tray": [
                            {"id": 0, "state": 11},
                            {"id": 1, "state": 10},
                        ],
                    }
                ]
            }
        )

        assert self._tray(mqtt_client, 1)["state"] == 10, "cached clear bit must not re-demote a seated tray"
        assert self._tray(mqtt_client, 0)["state"] == 11
        assert len(changes) > seeded_changes, "presence token a→p must fire on_ams_change"

    def test_repeated_bitless_pushes_keep_the_insert_visible(self, mqtt_client):
        """The incident's shape was RECURRENT: ~1 Hz bitless pushes each re-demoted
        the slot. Ten of them must leave the seated tray at 10."""
        mqtt_client.on_ams_change = None
        self._seed(mqtt_client, "1")

        for _ in range(10):
            mqtt_client._handle_ams_data({"ams": [{"id": 0, "tray": [{"id": 0, "state": 11}, {"id": 1, "state": 10}]}]})

        assert self._tray(mqtt_client, 1)["state"] == 10

    def test_fresh_bits_still_demote_a_removed_slot(self, mqtt_client):
        """The fix must not strand a genuine removal: a push that CARRIES the
        bitmask keeps full demote authority even on a state-10 tray."""
        self._seed(mqtt_client, "3")  # both bits set — slot 1's 9 promotes to 10
        assert self._tray(mqtt_client, 1)["state"] == 10

        # Roll pulled from slot 1; this push carries the fresh bitmask (bit 1 clear).
        mqtt_client._handle_ams_data(
            {
                "ams": [{"id": 0, "tray": [{"id": 0, "state": 11}, {"id": 1, "state": 10}]}],
                "tray_exist_bits": "1",
                "power_on_flag": True,
            }
        )
        assert self._tray(mqtt_client, 1)["state"] == 9, "fresh bits keep full demote authority"
        assert self._tray(mqtt_client, 0)["state"] == 11

    def test_cached_bits_still_promote_stuck_state_nine(self, mqtt_client):
        """003-H2S regression: the cached fallback's promotion duty is intact."""
        self._seed(mqtt_client, "3")  # bits 0,1 set — slot 1 occupied per firmware

        # Bitless partial that sticks slot 1 at the empty code with its bit set.
        mqtt_client._handle_ams_data({"ams": [{"id": 0, "tray": [{"id": 0, "state": 11}, {"id": 1, "state": 9}]}]})

        assert self._tray(mqtt_client, 1)["state"] == 10, "cached set bit must still promote 9→10"


class TestNozzleRackData:
    """``device.nozzle.info`` IS the rack: whatever it lists, sorted by id.

    The id convention is pinned hardware fact — 0 and 1 are the mounted hotends
    (on O1C2, 0 = RIGHT/main and 1 = LEFT/deputy) and ids >= 16 are rack positions.
    An empty rack position still ships an entry, with its fields blank.
    """

    @staticmethod
    def _nozzle_info(ids):
        def entry(i):
            if i >= 19:  # an empty rack position reports blanks, not absence
                return {"id": i, "type": "", "diameter": "", "wear": None, "stat": None, "max_temp": 0}
            return {"id": i, "type": "HS", "diameter": "0.4", "wear": 5, "stat": 0, "max_temp": 300}

        return {"print": {"device": {"nozzle": {"info": [entry(i) for i in ids]}}}}

    @pytest.mark.parametrize(
        "ids",
        [
            pytest.param([0, 1, 16, 17, 18, 19, 20, 21], id="h2c_two_hotends_plus_six_rack_positions"),
            pytest.param([0, 1], id="h2d_two_hotends_no_rack"),
            pytest.param([0], id="h2s_single_nozzle"),
        ],
    )
    def test_the_rack_mirrors_what_the_printer_lists(self, mqtt_client, ids):
        mqtt_client._process_message(self._nozzle_info(ids))

        assert [n["id"] for n in mqtt_client.state.nozzle_rack] == ids

    def test_empty_nozzle_info_does_not_populate_rack(self, mqtt_client):
        mqtt_client._process_message({"print": {"device": {"nozzle": {"info": []}}}})

        assert mqtt_client.state.nozzle_rack == []

    def test_nozzle_rack_sorted_by_id(self, mqtt_client):
        """The wire order is not the rack order."""
        mqtt_client._process_message(
            {
                "print": {
                    "device": {
                        "nozzle": {
                            "info": [
                                {"id": 17, "type": "HS", "diameter": "0.6"},
                                {"id": 0, "type": "HS", "diameter": "0.4"},
                                {"id": 16, "type": "HS", "diameter": "0.4"},
                                {"id": 1, "type": "HS", "diameter": "0.4"},
                            ]
                        }
                    }
                }
            }
        )

        assert [n["id"] for n in mqtt_client.state.nozzle_rack] == [0, 1, 16, 17]

    def test_nozzle_rack_field_mapping(self, mqtt_client):
        """Three wire names are renamed on the way in — colour/id/type all gain a
        ``filament_`` reading — and the rest pass through."""
        mqtt_client._process_message(
            {
                "print": {
                    "device": {
                        "nozzle": {
                            "info": [
                                {
                                    "id": 16,
                                    "type": "HH01",
                                    "diameter": "0.6",
                                    "wear": 15,
                                    "stat": 0,
                                    "max_temp": 320,
                                    "serial_number": "SN-ABC123",
                                    "filament_colour": "FF8800",
                                    "filament_id": "F42",
                                    "tray_type": "ABS",
                                }
                            ]
                        }
                    }
                }
            }
        )

        slot = mqtt_client.state.nozzle_rack[0]
        assert slot["id"] == 16
        assert slot["type"] == "HH01"
        assert slot["diameter"] == "0.6"
        assert slot["wear"] == 15
        assert slot["stat"] == 0
        assert slot["max_temp"] == 320
        assert slot["serial_number"] == "SN-ABC123"
        assert slot["filament_color"] == "FF8800"
        assert slot["filament_id"] == "F42"
        assert slot["filament_type"] == "ABS"

    def test_nozzle_info_updates_nozzle_state(self, mqtt_client):
        """Ids 0/1 are the mounted hotends, so they also update ``state.nozzles``."""
        mqtt_client._process_message(
            {
                "print": {
                    "device": {
                        "nozzle": {
                            "info": [
                                {"id": 0, "type": "HS", "diameter": "0.4"},
                                {"id": 1, "type": "HH01", "diameter": "0.6"},
                            ]
                        }
                    }
                }
            }
        )

        assert mqtt_client.state.nozzles[0].nozzle_type == "HS"
        assert mqtt_client.state.nozzles[0].nozzle_diameter == "0.4"
        assert mqtt_client.state.nozzles[1].nozzle_type == "HH01"
        assert mqtt_client.state.nozzles[1].nozzle_diameter == "0.6"


class TestRequestTopicFailSafe:
    """Not every broker allows the request topic, and the farm must degrade instead
    of reconnect-looping. The verdict is cached per SERIAL and outlives the client
    object, so a reconnect does not re-probe a broker that already said no."""

    @pytest.fixture(autouse=True)
    def clear_request_topic_cache(self):
        """The cache is class-level state — one printer's verdict must not leak."""
        BambuMQTTClient._request_topic_cache.clear()

    def test_request_topic_supported_by_default(self, mqtt_client):
        """Attempted until refused: supported, but not yet confirmed."""
        assert mqtt_client._request_topic_supported is True
        assert mqtt_client._request_topic_confirmed is False

    # SUBACK carries packetType 9; the per-topic identifier is the QoS on success and
    # 0x80 on refusal.
    @pytest.mark.parametrize(
        "suback_mid, identifier, supported, confirmed",
        [
            pytest.param(42, 0, True, True, id="suback_success_confirms"),
            pytest.param(42, 0x80, False, False, id="suback_0x80_disables"),
            # A SUBACK for another subscription (the report topic) says nothing here.
            pytest.param(99, 0x80, True, False, id="a_foreign_mid_is_ignored"),
        ],
    )
    def test_the_suback_decides_whether_the_request_topic_lives(
        self, mqtt_client, suback_mid, identifier, supported, confirmed
    ):
        mqtt_client._request_topic_sub_mid = 42

        mqtt_client._on_subscribe(None, None, suback_mid, [ReasonCode(9, identifier=identifier)], None)

        assert mqtt_client._request_topic_supported is supported
        assert mqtt_client._request_topic_confirmed is confirmed
        if suback_mid == 42:
            assert mqtt_client._request_topic_sub_mid is None
            assert mqtt_client._request_topic_sub_time == 0.0

    # Some brokers refuse by dropping the connection rather than by NAKing, so an
    # early unconfirmed disconnect is read as a refusal. Only an early one: a
    # disconnect long after the attempt, or one after a SUBACK already confirmed the
    # topic, has some other cause and must not disable a working feature.
    @pytest.mark.parametrize(
        "sub_age_s, confirmed, supported",
        [
            pytest.param(0.0, False, False, id="early_unconfirmed_disconnect_is_a_refusal"),
            pytest.param(0.0, True, True, id="confirmed_topic_survives_a_disconnect"),
            pytest.param(30.0, False, True, id="a_late_disconnect_is_not_the_topics_fault"),
        ],
    )
    def test_only_an_early_unconfirmed_disconnect_disables_the_topic(
        self, mqtt_client, sub_age_s, confirmed, supported
    ):
        mqtt_client._request_topic_sub_time = time.time() - sub_age_s
        mqtt_client._request_topic_confirmed = confirmed
        mqtt_client._last_message_time = 0.0

        mqtt_client._on_disconnect(None, None)

        assert mqtt_client._request_topic_supported is supported

    def test_on_connect_skips_request_topic_when_unsupported(self, mqtt_client):
        """The whole point: a reconnect subscribes to the report topic only."""
        mqtt_client._request_topic_supported = False
        subscribe_calls = []
        mock_client = type(
            "MockClient", (), {"subscribe": lambda self, topic: subscribe_calls.append(topic) or (0, 1)}
        )()

        mqtt_client._on_connect(mock_client, None, None, 0)

        assert subscribe_calls == [mqtt_client.topic_subscribe]

    def test_cache_persists_across_instances(self):
        """A new client for the same printer inherits the verdict."""
        client1 = _make_client(serial="TEST_CACHE")
        assert client1._request_topic_supported is True

        client1._request_topic_sub_time = time.time()
        client1._request_topic_confirmed = False
        client1._last_message_time = 0.0
        client1._on_disconnect(None, None)
        assert client1._request_topic_supported is False

        assert _make_client(serial="TEST_CACHE")._request_topic_supported is False

    def test_cache_does_not_affect_different_serial(self):
        BambuMQTTClient._request_topic_cache["SERIAL_A"] = False

        assert _make_client(serial="SERIAL_B")._request_topic_supported is True

    @pytest.mark.parametrize(
        "identifier, cached",
        [pytest.param(0, True, id="success_cached"), pytest.param(0x80, False, id="rejection_cached")],
    )
    def test_the_suback_verdict_is_written_to_the_cache(self, identifier, cached):
        client = _make_client(serial="TEST_SUBACK")
        client._request_topic_sub_mid = 42

        client._on_subscribe(None, None, 42, [ReasonCode(9, identifier=identifier)], None)

        assert BambuMQTTClient._request_topic_cache["TEST_SUBACK"] is cached


class TestRequestTopicAmsMapping:
    """The slicer's own ``ams_mapping`` is mirrored off the REQUEST topic.

    The farm never sees a screen- or Studio-started print's slot mapping any other
    way: it rides the command the slicer publishes, not the status the printer
    reports. It is captured at publish time, delivered on both terminals, and
    cleared afterwards so one print's mapping can never be attributed to the next.
    """

    def test_captured_ams_mapping_initializes_to_none(self, mqtt_client):
        assert mqtt_client._captured_ams_mapping is None

    @pytest.mark.parametrize(
        "message, captured",
        [
            pytest.param(
                {"print": {"command": "project_file", "ams_mapping": [0, 4, -1, -1], "url": "ftp://h/t.3mf"}},
                [0, 4, -1, -1],
                id="project_file_with_a_mapping_is_captured",
            ),
            pytest.param({"print": {"command": "pause"}}, None, id="another_command_carries_no_mapping"),
            pytest.param(
                {"print": {"command": "project_file", "url": "ftp://h/t.3mf"}},
                None,
                id="project_file_without_a_mapping",
            ),
            pytest.param({"print": "not_a_dict"}, None, id="a_non_dict_print_value_is_survivable"),
            pytest.param({"pushing": {"command": "pushall"}}, None, id="a_message_with_no_print_key"),
        ],
    )
    def test_only_a_project_file_command_yields_a_mapping(self, mqtt_client, message, captured):
        mqtt_client._handle_request_message(message)

        assert mqtt_client._captured_ams_mapping == captured

    def test_captured_mapping_overwrites_previous(self, mqtt_client):
        """Last dispatch wins — a stale mapping would mis-attribute this print's slots."""
        mqtt_client._captured_ams_mapping = [0, -1, -1, -1]

        mqtt_client._handle_request_message({"print": {"command": "project_file", "ams_mapping": [4, 8, -1, -1]}})

        assert mqtt_client._captured_ams_mapping == [4, 8, -1, -1]

    @pytest.mark.parametrize(
        "captured", [pytest.param([0, 4, -1, -1], id="mapping_captured"), pytest.param(None, id="no_mapping_captured")]
    )
    def test_print_start_reports_the_captured_mapping(self, mqtt_client, captured):
        """The key is always present, so a consumer can tell "no mapping" from "no
        field"."""
        start_data = {}
        mqtt_client.on_print_start = start_data.update
        mqtt_client._captured_ams_mapping = captured
        # A prior state, so the first RUNNING push is a real transition and not a
        # Bambuddy-restart catch-up (#1304).
        mqtt_client._previous_gcode_state = "IDLE"

        mqtt_client._process_message(
            {"print": {"gcode_state": "RUNNING", "gcode_file": "/data/Metadata/test.gcode", "subtask_name": "Test"}}
        )

        assert "ams_mapping" in start_data
        assert start_data["ams_mapping"] == captured

    def test_first_running_push_after_bambuddy_restart_does_not_fire_print_start(self, mqtt_client):
        """A restart mid-print leaves ``_previous_gcode_state`` None, and the printer's
        first push says RUNNING. Treating that as a new print re-ran plate detection
        (which paused the live print) and re-archived the file (#1304). ``_was_running``
        must still track it, or completion detection loses the job.
        """
        start_data = {}
        mqtt_client.on_print_start = start_data.update
        mqtt_client._previous_gcode_state = None
        mqtt_client._was_running = False

        mqtt_client._process_message(
            {
                "print": {
                    "gcode_state": "RUNNING",
                    "gcode_file": "/data/Metadata/big_print.gcode",
                    "subtask_name": "big_print",
                }
            }
        )

        assert start_data == {}, "on_print_start must not fire on a Bambuddy-restart catch-up"
        assert mqtt_client._was_running is True
        assert mqtt_client._previous_gcode_state == "RUNNING", "the next push must not look fresh too"

    @staticmethod
    def _run_a_print(mqtt_client, terminal, mapping):
        """Whole flow: the slicer publishes the mapping, then the printer runs and ends."""
        complete_data = {}
        mqtt_client.on_print_start = lambda d: None
        mqtt_client.on_print_complete = complete_data.update
        job = {"gcode_file": "/data/Metadata/model.gcode", "subtask_name": "Model"}

        mqtt_client._handle_request_message(
            {"print": {"command": "project_file", "ams_mapping": mapping, "url": "ftp://h/model.3mf"}}
        )
        assert mqtt_client._captured_ams_mapping == mapping

        mqtt_client._process_message({"print": {**job, "gcode_state": "RUNNING"}})
        mqtt_client._process_message({"print": {**job, "gcode_state": terminal}})
        return complete_data

    @pytest.mark.parametrize(
        "terminal, status",
        [pytest.param("FINISH", "completed", id="finish"), pytest.param("FAILED", "failed", id="failed")],
    )
    def test_either_terminal_delivers_the_mapping(self, mqtt_client, terminal, status):
        """A failed print needs its mapping just as much as a completed one — it is
        what says which slots the attempt consumed from."""
        complete_data = self._run_a_print(mqtt_client, terminal, [4, 9, -1, -1])

        assert complete_data["ams_mapping"] == [4, 9, -1, -1]
        assert complete_data["status"] == status

    def test_the_capture_is_cleared_after_a_terminal(self, mqtt_client):
        """A mapping left standing would be attributed to the next print, which may be
        a screen-started one that published none."""
        self._run_a_print(mqtt_client, "FINISH", [0, 4, -1, -1])

        assert mqtt_client._captured_ams_mapping is None


# tray_now disambiguation helpers


def _ams_payload(tray_now, ams_units=None, tray_exist_bits=None, ams_exist_bits=None):
    """Build minimal print.ams payload for tray_now disambiguation tests."""
    ams = {"tray_now": str(tray_now)}
    if ams_units is not None:
        ams["ams"] = ams_units
    if tray_exist_bits is not None:
        ams["tray_exist_bits"] = tray_exist_bits
    if ams_exist_bits is not None:
        ams["ams_exist_bits"] = ams_exist_bits
    return {"print": {"ams": ams}}


def _extruder_info_payload(extruders):
    """Build device.extruder.info payload (dual-nozzle detection + snow).

    Each entry in *extruders* is a dict with at least ``id`` and ``snow``.
    """
    return {
        "print": {
            "device": {
                "extruder": {
                    "info": extruders,
                }
            }
        }
    }


def _extruder_state_payload(state_val):
    """Build device.extruder.state payload (active extruder via bit 8)."""
    return {
        "print": {
            "device": {
                "extruder": {
                    "state": state_val,
                }
            }
        }
    }


# 1. Single-nozzle X1E — direct passthrough


class TestTrayNowSingleNozzleX1E:
    """Single-nozzle, 1 AMS — tray_now is a direct passthrough."""

    client_kwargs = {"serial": "TEST_X1E"}

    def test_tray_now_direct_passthrough_slot_0_to_3(self, mqtt_client):
        """Each tray_now 0-3 maps 1:1 on single-nozzle printers."""
        for slot in range(4):
            mqtt_client._process_message(_ams_payload(slot))
            assert mqtt_client.state.tray_now == slot

    def test_tray_now_255_means_unloaded(self, mqtt_client):
        """tray_now=255 means no filament loaded."""
        mqtt_client._process_message(_ams_payload(255))
        assert mqtt_client.state.tray_now == 255

    def test_single_extruder_does_not_trigger_dual_nozzle(self, mqtt_client):
        """device.extruder.info with 1 entry must NOT set _is_dual_nozzle."""
        mqtt_client._process_message(_extruder_info_payload([{"id": 0, "snow": 0xFF00FF}]))
        assert mqtt_client._is_dual_nozzle is False

    def test_last_loaded_tray_survives_unload(self, mqtt_client):
        """Load tray 2, unload → last_loaded_tray stays 2."""
        mqtt_client._process_message(_ams_payload(2))
        assert mqtt_client.state.last_loaded_tray == 2

        mqtt_client._process_message(_ams_payload(255))
        assert mqtt_client.state.tray_now == 255
        assert mqtt_client.state.last_loaded_tray == 2


# 2. Single-nozzle P2S — multiple AMS, global IDs pass through


class TestTrayNowSingleNozzleP2S:
    """Single-nozzle, 2 AMS — tray_now > 3 passes through as global ID."""

    client_kwargs = {"serial": "TEST_P2S"}

    def test_tray_now_ams1_global_ids_4_to_7(self, mqtt_client):
        """tray_now 4-7 are global IDs for AMS 1 on single-nozzle printers."""
        for global_id in range(4, 8):
            mqtt_client._process_message(_ams_payload(global_id))
            assert mqtt_client.state.tray_now == global_id

    def test_tray_change_across_ams_units(self, mqtt_client):
        """Switch from AMS 0 slot 1 → AMS 1 slot 2 (global 6)."""
        mqtt_client._process_message(_ams_payload(1))
        assert mqtt_client.state.tray_now == 1

        mqtt_client._process_message(_ams_payload(6))
        assert mqtt_client.state.tray_now == 6


class TestTrayNowP2SMultiAmsDisambiguation:
    """A multi-AMS P2S reports a LOCAL slot in ``tray_now``, so the farm resolves it
    against the slicer's mapping (#420).

    Two encodings meet here and must not be confused: the ``mapping`` entries are
    snow-encoded, ``ams_hw_id * 256 + slot`` (65535 = that colour is unmapped), while
    a global tray id is ``ams_id * 4 + slot``. Resolution is attempted only when
    ``ams_exist_bits`` says more than one unit is present AND ``tray_now`` is 0-3 —
    anything else is already unambiguous. When the mapping cannot name exactly one
    unit the local slot STANDS: a guess here would charge another AMS's spool.
    """

    client_kwargs = {"serial": "TEST_P2S_DUAL"}

    @pytest.mark.parametrize(
        "mapping, tray_now, ams_exist_bits, resolved",
        [
            # ams_exist_bits "3" = 0b11 = units 0 and 1 present.
            pytest.param([257], 1, "3", 5, id="ams1_slot1_snow257_to_global5"),
            pytest.param([256], 0, "3", 4, id="ams1_slot0_snow256_to_global4"),
            pytest.param([259], 3, "3", 7, id="ams1_slot3_snow259_to_global7"),
            pytest.param([1], 1, "3", 1, id="mapping_confirms_ams0_so_local_stands"),
            pytest.param([0, 257], 1, "3", 5, id="multicolour_only_the_matching_entry_counts"),
            # The exact mapping from the #420 support package.
            pytest.param([65535, 65535, 65535, 257], 1, "3", 5, id="multicolour_unmapped_65535_entries_skipped"),
            # AMS0-T1 and AMS1-T1 both carry local slot 1 — nothing can decide it.
            pytest.param([1, 257], 1, "3", 1, id="ambiguous_mapping_falls_back_to_local"),
            pytest.param(None, 1, "3", 1, id="no_mapping_falls_back_to_local"),
            pytest.param([], 1, "3", 1, id="empty_mapping_falls_back_to_local"),
            pytest.param(None, 2, "1", 2, id="single_ams_needs_no_resolution"),
            pytest.param(None, 1, None, 1, id="without_ams_exist_bits_nothing_is_resolved"),
            pytest.param([257], 255, "3", 255, id="unloaded_255_passes_through"),
            pytest.param(None, 6, "3", 6, id="above_3_is_already_a_global_id"),
        ],
    )
    def test_the_mapping_resolves_a_local_slot_or_the_local_slot_stands(
        self, mqtt_client, mapping, tray_now, ams_exist_bits, resolved
    ):
        if mapping is not None:
            mqtt_client.state.raw_data["mapping"] = mapping

        mqtt_client._process_message(_ams_payload(tray_now, ams_exist_bits=ams_exist_bits))

        assert mqtt_client.state.tray_now == resolved

    def test_last_loaded_tray_uses_resolved_global_id(self, mqtt_client):
        """The resolved id is what gets remembered — a local slot recorded as
        last-loaded would name the wrong unit's slot after the print."""
        mqtt_client.state.raw_data["mapping"] = [257]
        mqtt_client.state.state = "RUNNING"

        mqtt_client._process_message(_ams_payload(1, ams_exist_bits="3"))

        assert mqtt_client.state.tray_now == 5
        assert mqtt_client.state.last_loaded_tray == 5


class TestResolveLocalSlotFromMapping:
    """``_resolve_local_slot_from_mapping`` directly: snow entries in, global id out,
    and None whenever the answer is not unique (the caller then keeps the local slot).
    """

    @pytest.mark.parametrize(
        "local_slot, mapping, resolved",
        [
            pytest.param(1, [1], 1, id="ams0_slot1_stays_1"),
            pytest.param(1, [257], 5, id="ams1_slot1_snow257_to_global5"),
            pytest.param(2, [514], 10, id="ams2_slot2_snow514_to_global10"),
            # AMS-HT is unit 128, so its snow is 128*256 and its global id is 128 too.
            pytest.param(0, [32768], 128, id="ams_ht_slot0_snow32768_to_global128"),
            pytest.param(1, [65535, 65535, 65535, 257], 5, id="unmapped_65535_entries_skipped"),
            pytest.param(2, [0], None, id="no_entry_names_this_slot"),
            pytest.param(1, [1, 257], None, id="two_units_name_it_so_it_is_ambiguous"),
            pytest.param(1, None, None, id="mapping_absent"),
            pytest.param(1, [], None, id="mapping_empty"),
        ],
    )
    def test_a_unique_mapping_entry_resolves_the_global_id(self, local_slot, mapping, resolved):
        assert BambuMQTTClient._resolve_local_slot_from_mapping(local_slot, mapping) == resolved


class TestTrayNowDualNozzleH2DSetup:
    """What makes a client dual-nozzle, and how each AMS learns which nozzle it feeds.

    Dual-nozzle is detected from ``device.extruder.info`` carrying two entries — never
    from the serial. Each AMS unit's ``info`` field is a HEX STRING (BambuStudio reads
    it with ``stoull(str, 16)``) whose bits 8-11 are the extruder id it feeds:
    ``(int(info, 16) >> 8) & 0xF``. 0xE there means the AMS has not been initialized
    yet and must be skipped rather than mapped to extruder 14.
    """

    client_kwargs = {"serial": "TEST_H2D"}

    @staticmethod
    def _ams_info_payload(infos, tray_exist_bits):
        """One AMS unit per (id, info) pair, with the H2D's own unit ids."""
        return {
            "print": {
                "ams": {
                    "ams": [
                        {"id": ams_id, "info": info, "tray": [{"id": i} for i in range(4 if ams_id == 0 else 1)]}
                        for ams_id, info in infos
                    ],
                    "tray_now": "255",
                    "tray_exist_bits": tray_exist_bits,
                }
            }
        }

    def test_dual_nozzle_detected_from_extruder_info(self, mqtt_client):
        mqtt_client._process_message(_extruder_info_payload([{"id": 0, "snow": 0xFF00FF}, {"id": 1, "snow": 0xFF00FF}]))

        assert mqtt_client._is_dual_nozzle is True

    @pytest.mark.parametrize(
        "infos, bits, extruder_map",
        [
            pytest.param([(0, "2003"), (128, "2104")], "1000f", {"0": 0, "128": 1}, id="ams0_right_ams_ht_left"),
            # The same nibble, in the longer form a real H2D publishes.
            pytest.param(
                [(0, "10001003"), (128, "10002104")],
                "1000a",
                {"0": 0, "128": 1},
                id="real_h2d_values_high_word_ignored",
            ),
            pytest.param([(0, "e03")], "f", {}, id="extruder_id_0xE_is_uninitialized_and_skipped"),
        ],
    )
    def test_each_ams_unit_maps_to_the_nozzle_its_info_field_names(self, mqtt_client, infos, bits, extruder_map):
        mqtt_client._process_message(self._ams_info_payload(infos, bits))

        assert mqtt_client.state.ams_extruder_map == extruder_map

    def test_ams_extruder_map_partial_update_preserves_entries(self, mqtt_client):
        """A partial push carries one unit and no ``info`` at all; the map is fleet
        topology, not per-push content, so the absent unit must survive."""
        mqtt_client._process_message(self._ams_info_payload([(0, "2003"), (128, "2104")], "1000f"))
        assert mqtt_client.state.ams_extruder_map == {"0": 0, "128": 1}

        mqtt_client._process_message(
            {
                "print": {
                    "ams": {
                        "ams": [{"id": 0, "tray": [{"id": 0, "remain": 50}]}],
                        "tray_now": "0",
                        "tray_exist_bits": "1000f",
                    }
                }
            }
        )

        assert mqtt_client.state.ams_extruder_map == {"0": 0, "128": 1}

    def test_dual_nozzle_detection_before_ams_in_same_message(self, mqtt_client):
        """Parse ORDER: the extruder block is read before the AMS block, so a single
        push that carries both already resolves ``tray_now`` with dual-nozzle logic.
        Here snow is the unloaded sentinel, so resolution falls through to the
        extruder map: one AMS on extruder 0, slot 2 → global 0*4+2."""
        mqtt_client._process_message(
            {
                "print": {
                    "device": {
                        "extruder": {
                            "info": [{"id": 0, "snow": 0xFF00FF}, {"id": 1, "snow": 0xFF00FF}],
                            "state": 0x0001,
                        }
                    },
                    "ams": {
                        "ams": [{"id": 0, "info": "2003", "tray": [{"id": i} for i in range(4)]}],
                        "tray_now": "2",
                        "tray_exist_bits": "f",
                    },
                }
            }
        )

        assert mqtt_client._is_dual_nozzle is True
        assert mqtt_client.state.tray_now == 2


class _H2DFixtureMixin:
    """An H2D Pro under test: dual-nozzle detected, both AMS units mapped.

    Overrides the module ``mqtt_client`` fixture rather than adding a second name for
    the same object — the classes below all want the configured client.
    """

    client_kwargs = {"serial": "TEST_H2D"}

    @pytest.fixture
    def mqtt_client(self, mqtt_client):
        mqtt_client._process_message(
            {
                "print": {
                    "device": {
                        "extruder": {
                            "info": [{"id": 0, "snow": 0xFF00FF}, {"id": 1, "snow": 0xFF00FF}],
                            "state": 0x0001,  # right extruder active
                        }
                    },
                    "ams": {
                        "ams": [
                            {"id": 0, "info": "2003", "tray": [{"id": i} for i in range(4)]},
                            {"id": 128, "info": "2104", "tray": [{"id": 0}]},
                        ],
                        "tray_now": "255",
                        "tray_exist_bits": "1000f",
                    },
                }
            }
        )
        assert mqtt_client._is_dual_nozzle is True
        assert mqtt_client.state.ams_extruder_map == {"0": 0, "128": 1}
        return mqtt_client


class TestTrayNowDualNozzleH2DSnow(_H2DFixtureMixin):
    """``snow`` is the primary disambiguation path on a dual-nozzle printer.

    Per extruder it encodes what that nozzle is fed from as ``ams_id << 8 | slot``,
    which the farm stores as a global tray id. Two values are not locations:
    0xFFFF (ams 255, slot 255) means unloaded, and 0xFF00FF is the firmware's initial
    sentinel, which matches no branch and must not be stored at all — storing it
    would claim the nozzle is fed from AMS 65280.
    """

    @pytest.mark.parametrize(
        "snow_ext0, snow_ext1, stored",
        [
            pytest.param(1 << 8 | 3, 0 << 8 | 0, {0: 7, 1: 0}, id="ams1_slot3_to_global7_and_ams0_slot0_to_global0"),
            pytest.param(0xFFFF, 0xFFFF, {0: 255, 1: 255}, id="0xffff_is_unloaded"),
            pytest.param(0xFF00FF, 0xFF00FF, {}, id="firmware_initial_sentinel_is_not_a_location"),
        ],
    )
    def test_snow_decodes_to_a_global_tray_id_per_extruder(self, mqtt_client, snow_ext0, snow_ext1, stored):
        mqtt_client._process_message(
            _extruder_info_payload([{"id": 0, "snow": snow_ext0}, {"id": 1, "snow": snow_ext1}])
        )

        assert mqtt_client.state.h2d_extruder_snow == stored

    def test_snow_disambiguates_ams0_slot(self, mqtt_client):
        """snow must arrive in an EARLIER push than the ``tray_now`` it resolves: in one
        message snow is parsed after tray_now."""
        mqtt_client._process_message(
            _extruder_info_payload([{"id": 0, "snow": 0 << 8 | 2}, {"id": 1, "snow": 0xFF00FF}])
        )
        assert mqtt_client.state.h2d_extruder_snow.get(0) == 2

        mqtt_client._process_message(_ams_payload(2))

        assert mqtt_client.state.tray_now == 2

    def test_snow_disambiguates_ams_ht_to_128(self, mqtt_client):
        """The AMS-HT is unit 128 with a single slot, so a local ``tray_now`` of 0 on the
        left nozzle resolves to global 128 — not to AMS 0 slot 0."""
        mqtt_client._process_message(
            _extruder_info_payload([{"id": 0, "snow": 0xFF00FF}, {"id": 1, "snow": 128 << 8 | 0}])
        )
        assert mqtt_client.state.h2d_extruder_snow.get(1) == 128

        mqtt_client._process_message(_extruder_state_payload(0x0100))
        assert mqtt_client.state.active_extruder == 1

        mqtt_client._process_message(_ams_payload(0))

        assert mqtt_client.state.tray_now == 128


class TestTrayNowDualNozzleH2DPendingTarget(_H2DFixtureMixin):
    """When the farm itself initiated the load it knows the target, so a matching
    local slot resolves to it. Either way the target is consumed: a pending value
    left standing would resolve a later, unrelated load."""

    @pytest.mark.parametrize(
        "tray_now, resolved",
        [
            pytest.param(1, 5, id="local_slot_matches_pending_5_mod_4"),
            pytest.param(2, 2, id="mismatch_keeps_the_raw_slot"),
        ],
    )
    def test_a_pending_target_resolves_a_matching_slot_and_is_always_cleared(self, mqtt_client, tray_now, resolved):
        mqtt_client.state.pending_tray_target = 5

        mqtt_client._process_message(_ams_payload(tray_now))

        assert mqtt_client.state.tray_now == resolved
        assert mqtt_client.state.pending_tray_target is None

    def test_pending_target_takes_priority_over_snow(self, mqtt_client):
        """When both pending and snow are set, pending wins."""
        # Set up snow for extruder 0 → AMS 0 slot 1 → global 1
        snow_val = 0 << 8 | 1
        mqtt_client._process_message(
            _extruder_info_payload(
                [
                    {"id": 0, "snow": snow_val},
                    {"id": 1, "snow": 0xFF00FF},
                ]
            )
        )
        assert mqtt_client.state.h2d_extruder_snow.get(0) == 1

        # Set pending target to AMS 1 slot 1 (global 5)
        mqtt_client.state.pending_tray_target = 5
        # tray_now="1" — matches pending (5%4=1), pending should win over snow
        mqtt_client._process_message(_ams_payload(1))
        assert mqtt_client.state.tray_now == 5


class TestTrayNowDualNozzleH2DFallback(_H2DFixtureMixin):
    """With no pending target and no usable snow, the map alone must resolve the slot.

    The narrowing is: only AMS units mapped to the ACTIVE extruder are candidates; a
    tray that already matches the reported slot stays put; and an AMS-HT (unit id >=
    128) has ONE slot, so its global id is the unit id itself — never ``id * 4 + slot``
    — which also means a reported slot above 0 cannot be an AMS-HT at all. When the
    candidates do not narrow to one, the raw slot stands rather than being guessed.
    """

    @pytest.mark.parametrize(
        "extruder_map, current_tray, switch_left, slot, resolved",
        [
            pytest.param(None, None, False, 2, 2, id="single_ams_on_the_active_extruder"),
            pytest.param({"0": 0, "1": 0}, 5, False, 1, 5, id="current_tray_matching_the_slot_is_kept"),
            pytest.param({"0": 1, "128": 1}, None, False, 2, 2, id="no_ams_on_the_active_extruder_raw_slot"),
            pytest.param(None, None, True, 0, 128, id="lone_ams_ht_resolves_to_unit_id_128_not_512"),
            pytest.param({"129": 0}, None, False, 1, 129, id="ams_ht_ignores_a_nonzero_slot"),
            pytest.param({"0": 0, "128": 0}, 128, False, 0, 128, id="current_ams_ht_tray_is_kept"),
            pytest.param({"0": 0, "128": 0}, 255, False, 2, 2, id="slot_above_zero_excludes_the_ams_ht"),
            # Two regular units remain after excluding the HT — still ambiguous.
            pytest.param({"0": 0, "1": 0, "128": 0}, 255, False, 3, 3, id="ambiguous_candidates_keep_the_raw_slot"),
        ],
    )
    def test_the_extruder_map_resolves_the_slot_or_the_raw_slot_stands(
        self, mqtt_client, extruder_map, current_tray, switch_left, slot, resolved
    ):
        # The mixin leaves snow at the unloaded sentinel, so the snow path is skipped.
        if extruder_map is not None:
            mqtt_client.state.ams_extruder_map = extruder_map
        if current_tray is not None:
            mqtt_client.state.tray_now = current_tray
        if switch_left:
            mqtt_client._process_message(_extruder_state_payload(0x0100))

        mqtt_client._process_message(_ams_payload(slot))

        assert mqtt_client.state.tray_now == resolved


class TestLastLoadedTrayValidation(_H2DFixtureMixin):
    """``last_loaded_tray`` remembers the last PHYSICAL location, so the unloaded
    sentinel must not overwrite it — it is what the farm reads to attribute a
    print's consumption after the slot has already been released."""

    def test_regular_ams_tray_stored(self, mqtt_client):
        mqtt_client._process_message(
            _extruder_info_payload([{"id": 0, "snow": 1 << 8 | 3}, {"id": 1, "snow": 0xFF00FF}])
        )

        mqtt_client._process_message(_ams_payload(3))

        assert mqtt_client.state.tray_now == 7
        assert mqtt_client.state.last_loaded_tray == 7

    def test_ams_ht_tray_stored(self, mqtt_client):
        mqtt_client._process_message(_extruder_state_payload(0x0100))
        mqtt_client._process_message(
            _extruder_info_payload([{"id": 0, "snow": 0xFF00FF}, {"id": 1, "snow": 128 << 8 | 0}])
        )

        mqtt_client._process_message(_ams_payload(0))

        assert mqtt_client.state.tray_now == 128
        assert mqtt_client.state.last_loaded_tray == 128

    def test_unloaded_not_stored(self, mqtt_client):
        mqtt_client.state.last_loaded_tray = 5

        mqtt_client._process_message(_ams_payload(255))

        assert mqtt_client.state.tray_now == 255
        assert mqtt_client.state.last_loaded_tray == 5


class TestTrayNowDualNozzleH2DActiveExtruder(_H2DFixtureMixin):
    """Which nozzle is live rides BIT 8 of ``device.extruder.state``, and it decides
    which extruder's snow answers the next ``tray_now``."""

    @pytest.mark.parametrize(
        "state_val, active",
        [
            pytest.param(0x0001, 0, id="bit8_clear_is_right"),
            pytest.param(0x0100, 1, id="bit8_set_is_left"),
        ],
    )
    def test_bit_eight_names_the_active_extruder(self, mqtt_client, state_val, active):
        assert mqtt_client.state.active_extruder == 0, "right is the default"

        mqtt_client._process_message(_extruder_state_payload(state_val))

        assert mqtt_client.state.active_extruder == active

    def test_the_switch_is_reversible(self, mqtt_client):
        mqtt_client._process_message(_extruder_state_payload(0x0100))
        assert mqtt_client.state.active_extruder == 1

        mqtt_client._process_message(_extruder_state_payload(0x0001))

        assert mqtt_client.state.active_extruder == 0

    def test_extruder_switch_changes_tray_disambiguation(self, mqtt_client):
        """The point of tracking it: the same reported slot resolves differently
        depending on which nozzle is feeding."""
        mqtt_client._process_message(
            _extruder_info_payload([{"id": 0, "snow": 0 << 8 | 1}, {"id": 1, "snow": 128 << 8 | 0}])
        )

        mqtt_client._process_message(_ams_payload(1))
        assert mqtt_client.state.tray_now == 1

        mqtt_client._process_message(_extruder_state_payload(0x0100))
        mqtt_client._process_message(_ams_payload(0))

        assert mqtt_client.state.tray_now == 128


# 8. Device identification probe (#1684 enabler)


class TestDeviceIdentificationProbe:
    """One-shot INFO log of any device.* identification fields the firmware
    sends. Lets a new-model support bundle self-disclose the internal model
    code (e.g. dev_model_name='N2L') without a separate debug build.
    """

    client_kwargs = {"serial": "TEST_PROBE"}

    def _device_payload(self, device):
        return {"print": {"device": device}}

    def test_logs_known_id_fields_once(self, mqtt_client, caplog):
        caplog.set_level(logging.INFO, logger="backend.app.services.bambu_mqtt")
        mqtt_client._process_message(
            self._device_payload({"dev_model_name": "N2S", "dev_product_name": "Bambu Lab A1"})
        )
        matches = [r for r in caplog.records if "Device identification" in r.getMessage()]
        assert len(matches) == 1
        msg = matches[0].getMessage()
        assert "dev_model_name" in msg and "N2S" in msg
        assert "dev_product_name" in msg

    def test_one_shot_does_not_repeat(self, mqtt_client, caplog):
        caplog.set_level(logging.INFO, logger="backend.app.services.bambu_mqtt")
        payload = self._device_payload({"dev_model_name": "N2S"})
        mqtt_client._process_message(payload)
        mqtt_client._process_message(payload)
        mqtt_client._process_message(payload)
        matches = [r for r in caplog.records if "Device identification" in r.getMessage()]
        assert len(matches) == 1

    def test_fallback_dumps_keys_when_no_known_fields(self, mqtt_client, caplog):
        """Future Bambu rename (e.g. model_name without dev_ prefix) still surfaces."""
        caplog.set_level(logging.INFO, logger="backend.app.services.bambu_mqtt")
        mqtt_client._process_message(self._device_payload({"model_name": "MysteryModel", "extruder": {"state": 0}}))
        matches = [r for r in caplog.records if "Device identification" in r.getMessage()]
        assert len(matches) == 1
        msg = matches[0].getMessage()
        assert "no known id fields" in msg
        assert "model_name" in msg and "extruder" in msg


# 8. H2D Full multi-message sequences


class TestTrayNowDualNozzleH2DFullSequence(_H2DFixtureMixin):
    """Multi-message sequences simulating real H2D Pro prints."""

    def test_h2d_right_nozzle_ams0_lifecycle(self, mqtt_client):
        """Setup → load AMS 0 slot 1 → verify tray_now=1."""
        # Snow update: extruder 0 loading AMS 0 slot 1
        mqtt_client._process_message(
            _extruder_info_payload(
                [
                    {"id": 0, "snow": 0 << 8 | 1},
                    {"id": 1, "snow": 0xFF00FF},
                ]
            )
        )
        # Printer reports tray_now="1"
        mqtt_client._process_message(_ams_payload(1))
        assert mqtt_client.state.tray_now == 1
        assert mqtt_client.state.last_loaded_tray == 1

    def test_h2d_left_nozzle_ams_ht_lifecycle(self, mqtt_client):
        """Setup → switch left → load AMS HT → verify tray_now=128."""
        # Switch to left extruder
        mqtt_client._process_message(_extruder_state_payload(0x0100))

        # Snow: ext 1 → AMS HT slot 0
        mqtt_client._process_message(
            _extruder_info_payload(
                [
                    {"id": 0, "snow": 0xFF00FF},
                    {"id": 1, "snow": 128 << 8 | 0},
                ]
            )
        )

        # Printer reports tray_now="0" (AMS HT single slot)
        mqtt_client._process_message(_ams_payload(0))
        assert mqtt_client.state.tray_now == 128
        assert mqtt_client.state.last_loaded_tray == 128

    def test_h2d_multi_color_alternating_nozzles(self, mqtt_client):
        """Multi-color print alternating between right and left nozzles.

        Sequence:
        1. Right loads AMS 0 slot 0 (tray=0)
        2. Switch left, load AMS HT (tray=128)
        3. Switch right, snow updates, load AMS 0 slot 2 (tray=2)
        4. Unload (255)
        """
        # Step 1: Right extruder loads AMS 0 slot 0
        mqtt_client._process_message(
            _extruder_info_payload(
                [
                    {"id": 0, "snow": 0 << 8 | 0},
                    {"id": 1, "snow": 0xFF00FF},
                ]
            )
        )
        mqtt_client._process_message(_ams_payload(0))
        assert mqtt_client.state.tray_now == 0

        # Step 2: Switch to left, load AMS HT
        mqtt_client._process_message(_extruder_state_payload(0x0100))
        mqtt_client._process_message(
            _extruder_info_payload(
                [
                    {"id": 0, "snow": 0 << 8 | 0},
                    {"id": 1, "snow": 128 << 8 | 0},
                ]
            )
        )
        mqtt_client._process_message(_ams_payload(0))
        assert mqtt_client.state.tray_now == 128

        # Step 3: Switch back to right, load AMS 0 slot 2
        mqtt_client._process_message(_extruder_state_payload(0x0001))
        mqtt_client._process_message(
            _extruder_info_payload(
                [
                    {"id": 0, "snow": 0 << 8 | 2},
                    {"id": 1, "snow": 128 << 8 | 0},
                ]
            )
        )
        mqtt_client._process_message(_ams_payload(2))
        assert mqtt_client.state.tray_now == 2

        # Step 4: Unload
        mqtt_client._process_message(_ams_payload(255))
        assert mqtt_client.state.tray_now == 255
        assert mqtt_client.state.last_loaded_tray == 2


class TestTrayChangeLog:
    """``tray_change_log`` is the per-feeder gram split's only evidence.

    Each entry is ``(tray, layer)``, seeded at layer 0 when the print starts and
    appended whenever the fed tray changes mid-print. The usage tracker splits the
    3MF estimate across those layer segments, so a missed entry double-credits the
    departing tray and an entry recorded outside the print pollutes the next one.

    The gate is the print-LIFECYCLE flags (``_was_running`` and not
    ``_completion_triggered``), never ``state in ("RUNNING", "PAUSE")``: P2S firmware
    drops out of RUNNING for a moment during an AMS auto-fallback (#957), and a
    literal-string gate misses exactly the switch it most needs to see.
    """

    client_kwargs = {"serial": "TRAYLOG1"}

    @staticmethod
    def _start_a_print(mqtt_client, tray):
        mqtt_client.state.tray_now = tray
        mqtt_client.state.last_loaded_tray = tray
        mqtt_client._previous_gcode_state = "IDLE"
        mqtt_client._process_message({"print": {"gcode_state": "RUNNING", "gcode_file": "test.3mf"}})

    def test_tray_change_log_defaults_empty(self, mqtt_client):
        assert mqtt_client.state.tray_change_log == []

    def test_tray_change_log_seeded_on_print_start(self, mqtt_client):
        """Layer 0 of the print names the tray it started on."""
        self._start_a_print(mqtt_client, 2)

        assert mqtt_client.state.tray_change_log == [(2, 0)]

    def test_tray_change_log_cleared_on_new_print(self, mqtt_client):
        """The previous print's segments must not be charged to this one."""
        mqtt_client.state.tray_change_log = [(5, 0), (3, 100)]

        self._start_a_print(mqtt_client, 1)

        assert mqtt_client.state.tray_change_log == [(1, 0)]

    @pytest.mark.parametrize(
        "gcode_state, was_running, completion_triggered, logged",
        [
            pytest.param("RUNNING", True, False, True, id="running_is_mid_print"),
            # The AMS can swap while the print is paused for a refill.
            pytest.param("PAUSE", True, False, True, id="pause_is_still_mid_print"),
            # #957: the transient state a P2S passes through during auto-fallback.
            pytest.param("LOADING", True, False, True, id="a_transient_state_is_still_mid_print"),
            pytest.param("IDLE", False, False, False, id="between_prints_nothing_is_logged"),
            # Post-print self-cleaning moves the tray; that is not consumption.
            pytest.param("FINISH", True, True, False, id="after_the_terminal_nothing_is_logged"),
        ],
    )
    def test_only_a_change_inside_the_print_lifecycle_is_logged(
        self, mqtt_client, gcode_state, was_running, completion_triggered, logged
    ):
        mqtt_client.state.state = gcode_state
        mqtt_client._was_running = was_running
        mqtt_client._completion_triggered = completion_triggered
        mqtt_client.state.layer_num = 50
        mqtt_client.state.last_loaded_tray = 0
        mqtt_client.state.tray_change_log = [(0, 0)]

        mqtt_client._process_message(_ams_payload(1))

        assert mqtt_client.state.tray_change_log == ([(0, 0), (1, 50)] if logged else [(0, 0)])
        # Either way the location is tracked — it is what attributes consumption once
        # the slot has already been released.
        assert mqtt_client.state.last_loaded_tray == 1

    def test_same_tray_not_logged_twice(self, mqtt_client):
        """A ~1 Hz push repeats the fed tray forever; only a CHANGE is a segment."""
        mqtt_client._was_running = True
        mqtt_client._completion_triggered = False
        mqtt_client.state.layer_num = 30
        mqtt_client.state.last_loaded_tray = 2
        mqtt_client.state.tray_change_log = [(2, 0)]

        mqtt_client._process_message(_ams_payload(2))

        assert mqtt_client.state.tray_change_log == [(2, 0)]

    def test_multiple_tray_changes(self, mqtt_client):
        """A multi-colour print's whole history, in order."""
        mqtt_client._was_running = True
        mqtt_client._completion_triggered = False
        mqtt_client.state.last_loaded_tray = 0
        mqtt_client.state.tray_change_log = [(0, 0)]

        for tray, layer in [(1, 50), (3, 120), (0, 200)]:
            mqtt_client.state.layer_num = layer
            mqtt_client._process_message(_ams_payload(tray))

        assert mqtt_client.state.tray_change_log == [(0, 0), (1, 50), (3, 120), (0, 200)]


class TestDeveloperModeDetection:
    """Developer (LAN) mode is read off the ``fun`` capability bitfield.

    ``fun`` is a hex STRING; bit 29 (0x20000000) of its lower 32 bits SET means
    developer mode is OFF — i.e. the printer demands cloud encryption and will not
    take LAN commands. Unknown must not be guessed either way: an unparseable or
    absent field leaves the previous reading standing, because the farm refuses
    dispatch on a printer it believes is not in developer mode.
    """

    @pytest.mark.parametrize(
        "fun, developer_mode",
        [
            pytest.param("1C8187FF9CFF", True, id="bit29_clear_is_developer_mode"),
            pytest.param("1C81A7FF9CFF", False, id="bit29_set_is_cloud_only"),
            pytest.param("000020000000", False, id="bit29_alone_is_enough_to_say_no"),
            pytest.param("000000000000", True, id="no_bits_set_at_all_is_yes"),
        ],
    )
    def test_bit_29_of_fun_decides_developer_mode(self, mqtt_client, fun, developer_mode):
        assert mqtt_client.state.developer_mode is None, "unknown until a fun field arrives"

        mqtt_client._process_message({"print": {"gcode_state": "IDLE", "fun": fun}})

        assert mqtt_client.state.developer_mode is developer_mode

    @pytest.mark.parametrize(
        "print_data",
        [
            pytest.param({"gcode_state": "IDLE", "fun": "not_a_hex_value"}, id="unparseable_fun"),
            pytest.param({"gcode_state": "RUNNING", "mc_percent": 50}, id="no_fun_field"),
        ],
    )
    @pytest.mark.parametrize("previous", [True, False], ids=["previously_yes", "previously_no"])
    def test_an_unreadable_fun_field_leaves_the_reading_standing(self, mqtt_client, print_data, previous):
        mqtt_client.state.developer_mode = previous

        mqtt_client._process_message({"print": print_data})

        assert mqtt_client.state.developer_mode is previous

    def test_developer_mode_persists_across_messages(self, mqtt_client):
        """Only a push that CARRIES ``fun`` re-decides it."""
        mqtt_client._process_message({"print": {"gcode_state": "IDLE", "fun": "3EC1AFFF9CFF"}})
        assert mqtt_client.state.developer_mode is False

        for _ in range(3):
            mqtt_client._process_message({"print": {"gcode_state": "RUNNING", "mc_percent": 50}})

        assert mqtt_client.state.developer_mode is False


class TestDeveloperModeProbeTimeout:
    """The probe that decides developer mode, and what happens when it gets no answer.

    A half-broken MQTT session still publishes status while ignoring every command,
    which is indistinguishable from a healthy one until something is asked of it. The
    probe is that ask: it rides the first full report (a pushall, >30 keys) but not
    until 5 s after connect, and its answer is matched by ``sequence_id``. One
    unanswered probe is retried; the second force-closes the socket, because a session
    that ignores commands will never recover on its own.
    """

    client_kwargs = {"connected": True}

    @pytest.fixture
    def mqtt_client(self, mqtt_client):
        """The paho mock needs a socket to force-close, and a connect old enough that
        the 5 s probe delay is already satisfied."""
        mqtt_client._client.socket.return_value = MagicMock()
        mqtt_client._connect_time = time.monotonic() - 10.0
        return mqtt_client

    PUSHALL = {f"key_{i}": i for i in range(35)}  # >30 keys and no `fun` field
    INCREMENTAL = {"gcode_state": "IDLE", "mc_percent": 0}

    @staticmethod
    def _expire_the_probe(mqtt_client):
        mqtt_client._dev_mode_probe_time = time.monotonic() - 11.0

    @pytest.mark.parametrize(
        "connect_age_s, probed",
        [
            pytest.param(1.0, False, id="deferred_inside_the_5s_connect_delay"),
            pytest.param(6.0, True, id="fires_once_the_delay_has_passed"),
        ],
    )
    def test_a_pushall_arms_the_probe_but_the_connect_delay_gates_it(self, mqtt_client, connect_age_s, probed):
        mqtt_client._connect_time = time.monotonic() - connect_age_s

        mqtt_client._update_state(self.PUSHALL)

        assert mqtt_client._dev_mode_needs_probe is True
        assert mqtt_client._dev_mode_probed is probed
        assert (mqtt_client._dev_mode_probe_seq is not None) is probed

    def test_an_incremental_stream_never_arms_the_probe(self, mqtt_client):
        """Only a full report is a probe opportunity."""
        mqtt_client._update_state(self.INCREMENTAL)

        assert mqtt_client._dev_mode_needs_probe is False
        assert mqtt_client._dev_mode_probe_failures == 0

    def test_probe_fires_on_incremental_after_delay(self, mqtt_client):
        """The arming survives the delay: a pushall seen too early is remembered, and
        the next message of any size fires the probe."""
        mqtt_client._connect_time = time.monotonic() - 1.0
        mqtt_client._update_state(self.PUSHALL)
        assert mqtt_client._dev_mode_probed is False

        mqtt_client._connect_time = time.monotonic() - 6.0
        mqtt_client._update_state(self.INCREMENTAL)

        assert mqtt_client._dev_mode_probed is True
        assert mqtt_client._dev_mode_probe_seq is not None

    def test_no_reprobe_when_developer_mode_cached(self, mqtt_client):
        """A reconnect keeps the answer, so it must not ask again."""
        mqtt_client.state.developer_mode = True

        mqtt_client._update_state(self.PUSHALL)

        assert mqtt_client._dev_mode_needs_probe is False
        assert mqtt_client._dev_mode_probed is False
        assert mqtt_client._dev_mode_probe_seq is None
        assert mqtt_client.state.developer_mode is True

    def test_first_timeout_allows_retry(self, mqtt_client):
        """One silence is not a verdict: the probe is re-armed and the session kept."""
        mqtt_client._update_state(self.PUSHALL)
        assert mqtt_client._dev_mode_probed is True
        assert mqtt_client._dev_mode_probe_seq is not None
        assert mqtt_client.state.developer_mode is None

        self._expire_the_probe(mqtt_client)
        mqtt_client._update_state(self.PUSHALL)

        assert mqtt_client._dev_mode_probe_failures == 1
        assert mqtt_client._dev_mode_probe_seq is None
        assert mqtt_client._dev_mode_probed is False
        assert mqtt_client.state.connected is True

    def test_second_timeout_forces_reconnect(self, mqtt_client):
        """Timeout detection runs on paho's network thread, where there is no asyncio
        loop, so the teardown takes the socket-close path — calling ``loop_stop`` from
        inside the loop deadlocks."""
        state_changes = []
        mqtt_client.on_state_change = state_changes.append

        mqtt_client._update_state(self.PUSHALL)
        self._expire_the_probe(mqtt_client)
        mqtt_client._update_state(self.PUSHALL)
        assert mqtt_client._dev_mode_probe_failures == 1

        mqtt_client._update_state(self.PUSHALL)  # the retry
        assert mqtt_client._dev_mode_probed is True
        self._expire_the_probe(mqtt_client)
        mqtt_client._update_state(self.PUSHALL)

        assert mqtt_client._dev_mode_probe_failures == 2
        assert mqtt_client.state.connected is False
        assert mqtt_client._stale_reconnecting is True
        mqtt_client._client.socket().close.assert_called()
        assert state_changes

    def test_successful_probe_resets_failure_counter(self, mqtt_client):
        """The answer is matched by ``sequence_id``, and the retry mints a new one."""
        mqtt_client._update_state(self.PUSHALL)
        first_seq = mqtt_client._dev_mode_probe_seq
        self._expire_the_probe(mqtt_client)
        mqtt_client._update_state(self.PUSHALL)
        assert mqtt_client._dev_mode_probe_failures == 1

        mqtt_client._update_state(self.PUSHALL)
        retry_seq = mqtt_client._dev_mode_probe_seq
        assert retry_seq is not None and retry_seq != first_seq

        mqtt_client._handle_dev_mode_probe_response(
            {"command": "ams_filament_setting", "sequence_id": retry_seq, "result": "success"}
        )

        assert mqtt_client._dev_mode_probe_failures == 0
        assert mqtt_client.state.developer_mode is True
        assert mqtt_client._dev_mode_probe_seq is None

    def test_on_connect_resets_probe_state_but_preserves_developer_mode(self, mqtt_client):
        """Reconnecting clears every probe-tracking field so the next session starts
        clean — but the ANSWER survives, which is what stops a reconnect loop from
        re-probing a printer whose mode is already known (#887)."""
        mqtt_client._dev_mode_probed = True
        mqtt_client._dev_mode_needs_probe = True
        mqtt_client._dev_mode_probe_seq = "42"
        mqtt_client._dev_mode_probe_time = time.monotonic()
        mqtt_client._dev_mode_probe_failures = 2
        mqtt_client.state.developer_mode = True
        mqtt_client._client.subscribe.return_value = (0, 1)  # (result, mid)

        mqtt_client._on_connect(mqtt_client._client, None, None, 0)

        assert mqtt_client.state.developer_mode is True
        assert mqtt_client._dev_mode_probed is False
        assert mqtt_client._dev_mode_needs_probe is False
        assert mqtt_client._dev_mode_probe_seq is None
        assert mqtt_client._dev_mode_probe_time == 0.0
        assert mqtt_client._dev_mode_probe_failures == 0
        assert mqtt_client._connect_time > 0


class TestVtTrayNormalization:
    """``vt_tray`` arrives as a DICT on single-slot printers and as a list elsewhere;
    every consumer expects a list.

    The normalization has to happen before any callback can read ``raw_data``, because
    the dev-mode probe publishes mid-update and can release the GIL — letting the
    event loop observe partially-updated state.
    """

    @pytest.mark.parametrize(
        "vt_tray, length",
        [
            pytest.param({"id": "254", "tray_type": "PLA", "tray_color": "FF0000"}, 1, id="a_dict_is_wrapped"),
            pytest.param(
                [{"id": "254", "tray_type": "PLA"}, {"id": "255", "tray_type": "PETG"}], 2, id="a_list_is_left_alone"
            ),
        ],
    )
    def test_vt_tray_always_lands_as_a_list(self, mqtt_client, vt_tray, length):
        mqtt_client._update_state({"gcode_state": "IDLE", "vt_tray": vt_tray})

        stored = mqtt_client.state.raw_data.get("vt_tray")
        assert isinstance(stored, list)
        assert len(stored) == length

    def test_preserved_vt_tray_restored_before_probe(self, mqtt_client):
        """``_update_state`` replaces ``raw_data`` wholesale, so the wrapped list the
        incremental handler already stored must be restored BEFORE the probe publishes
        — and it outranks the dict in the new data."""
        mqtt_client.state.raw_data = {"vt_tray": [{"id": "254", "tray_type": "PLA", "tray_color": "00FF00"}]}

        mqtt_client._update_state(
            {"gcode_state": "IDLE", "vt_tray": {"id": "254", "tray_type": "PETG", "tray_color": "FF0000"}}
        )

        stored = mqtt_client.state.raw_data["vt_tray"]
        assert stored[0]["tray_type"] == "PLA"
        assert stored[0]["tray_color"] == "00FF00"


class TestSendDryingCommand:
    """``ams_filament_drying`` payload shape, and the target cache the badge renders."""

    client_kwargs = {"publish_target": True}

    @pytest.mark.parametrize(
        "kwargs, rotate_tray",
        [
            pytest.param({"mode": 1, "filament": "PLA"}, False, id="start_does_not_rotate_by_default"),
            pytest.param({"mode": 1, "filament": "PLA", "rotate_tray": True}, True, id="start_with_rotation"),
            pytest.param({"mode": 0}, False, id="stop_never_rotates"),
        ],
    )
    def test_rotate_tray_is_sent_explicitly(self, mqtt_client, kwargs, rotate_tray):
        mqtt_client.send_drying_command(ams_id=0, temp=55, duration=4, **kwargs)

        assert _published_command(mqtt_client)["rotate_tray"] is rotate_tray

    def test_all_required_fields_present(self, mqtt_client):
        """The firmware wants the whole frame, including the fields the farm never
        varies — a missing key is rejected, not defaulted."""
        mqtt_client.send_drying_command(ams_id=128, temp=75, duration=8, mode=1, filament="ABS", rotate_tray=True)

        cmd = _published_command(mqtt_client)
        assert cmd["command"] == "ams_filament_drying"
        assert cmd["ams_id"] == 128
        assert cmd["temp"] == 75
        assert cmd["duration"] == 8
        assert cmd["mode"] == 1
        assert cmd["rotate_tray"] is True
        assert cmd["filament"] == "ABS"
        assert cmd["cooling_temp"] == 20
        assert cmd["humidity"] == 0
        assert cmd["close_power_conflict"] is False
        assert "sequence_id" in cmd

    def test_publishes_with_qos_1(self, mqtt_client):
        """A dropped drying command would leave the operator's UI claiming a dry that
        never started."""
        mqtt_client.send_drying_command(ams_id=0, temp=55, duration=4)

        call_args = mqtt_client._client.publish.call_args
        qos = call_args.kwargs.get("qos", call_args[0][2] if len(call_args[0]) > 2 else None)
        assert qos == 1

    def test_start_caches_target_for_badge(self, mqtt_client):
        """The cache carries a monotonic ``latched_until`` as well, which drives
        ``ams_unit_drying`` until the first ``dry_time`` push arrives."""
        mqtt_client.send_drying_command(ams_id=2, temp=65, duration=12, mode=1, filament="PETG")

        entry = mqtt_client._drying_targets[2]
        assert entry["filament"] == "PETG"
        assert entry["temp"] == 65
        assert isinstance(entry["latched_until"], float)

    def test_start_overwrites_prior_target_for_same_ams(self, mqtt_client):
        mqtt_client.send_drying_command(ams_id=0, temp=55, duration=4, mode=1, filament="PLA")

        mqtt_client.send_drying_command(ams_id=0, temp=70, duration=6, mode=1, filament="ABS")

        assert mqtt_client._drying_targets[0]["filament"] == "ABS"
        assert mqtt_client._drying_targets[0]["temp"] == 70

    def test_stop_clears_target(self, mqtt_client):
        mqtt_client.send_drying_command(ams_id=1, temp=55, duration=4, mode=1, filament="PLA")
        assert 1 in mqtt_client._drying_targets

        mqtt_client.send_drying_command(ams_id=1, temp=0, duration=0, mode=0)

        assert 1 not in mqtt_client._drying_targets

    def test_targets_isolated_per_ams_id(self, mqtt_client):
        """Units dry independently — stopping one must not blank another's badge."""
        mqtt_client.send_drying_command(ams_id=0, temp=55, duration=4, mode=1, filament="PLA")
        mqtt_client.send_drying_command(ams_id=128, temp=80, duration=6, mode=1, filament="PA-CF")

        mqtt_client.send_drying_command(ams_id=0, temp=0, duration=0, mode=0)

        assert 0 not in mqtt_client._drying_targets
        assert mqtt_client._drying_targets[128]["filament"] == "PA-CF"
        assert mqtt_client._drying_targets[128]["temp"] == 80


class TestStartPrintAmsMapping:
    """``ams_mapping`` / ``ams_mapping2`` construction in ``start_print``.

    Two representations of one intent go on the wire together. The FLAT
    ``ams_mapping`` takes real tray ids, but every virtual tray (254/255) must appear
    there as -1 — raw 254/255 in the flat array makes H2D firmware fail the job with
    0700_8012 "Failed to get AMS mapping table". The detailed ``ams_mapping2`` carries
    ``{ams_id, slot_id}`` per colour, where a regular global tray id splits as
    ``ams_id = id // 4, slot = id % 4``, an AMS-HT is its own unit id with slot 0, and
    an unmapped colour is 0xFF/0xFF.

    The external-spool sentinels are where the families differ. On a SINGLE-nozzle
    printer both 254 and 255 address the one external holder and must be sent as
    ams_id 255 (VIRTUAL_TRAY_MAIN_ID); passing 254 through makes firmware target AMS
    tray 0 instead and raise 07FF_8012. On DUAL-nozzle hardware 254 is the deputy
    (left) nozzle's holder and must be preserved. H2S sits on the wrong side of the
    obvious guess: it shares the "094" serial prefix and the H-family frame quirks
    with the H2D, but it has ONE extruder (#1386).
    """

    client_kwargs = {"connected": True}

    @pytest.mark.parametrize(
        "model, mapping, flat, detailed",
        [
            pytest.param(
                None,
                [0, 5, 11],
                [0, 5, 11],
                [{"ams_id": 0, "slot_id": 0}, {"ams_id": 1, "slot_id": 1}, {"ams_id": 2, "slot_id": 3}],
                id="regular_ams_trays_split_into_unit_and_slot",
            ),
            pytest.param(
                None,
                [-1, 4],
                [-1, 4],
                [{"ams_id": 255, "slot_id": 255}, {"ams_id": 1, "slot_id": 0}],
                id="an_unmapped_colour_is_0xff_0xff",
            ),
            pytest.param(
                None,
                [128, 131],
                [128, 131],
                [{"ams_id": 128, "slot_id": 0}, {"ams_id": 131, "slot_id": 0}],
                id="ams_ht_ids_pass_through_with_slot_0",
            ),
            pytest.param(
                None, [255], [-1], [{"ams_id": 255, "slot_id": 0}], id="main_nozzle_external_255_flattens_to_minus_one"
            ),
            pytest.param(
                None, [254], [-1], [{"ams_id": 255, "slot_id": 0}], id="single_nozzle_external_254_becomes_255"
            ),
            pytest.param(
                None,
                [254, 255],
                [-1, -1],
                [{"ams_id": 255, "slot_id": 0}, {"ams_id": 255, "slot_id": 0}],
                id="single_nozzle_both_sentinels_mean_the_one_holder",
            ),
            # The #797 shape: a 5-colour 3MF with only the last colour assigned, to the
            # external holder.
            pytest.param(
                None,
                [-1, -1, -1, -1, 255],
                [-1, -1, -1, -1, -1],
                [{"ams_id": 255, "slot_id": 255}] * 4 + [{"ams_id": 255, "slot_id": 0}],
                id="unmapped_colours_beside_one_external",
            ),
            pytest.param(
                "H2D",
                [254, 255],
                [-1, -1],
                [{"ams_id": 254, "slot_id": 0}, {"ams_id": 255, "slot_id": 0}],
                id="h2d_deputy_254_is_preserved",
            ),
            pytest.param(
                "H2D Pro", [254], [-1], [{"ams_id": 254, "slot_id": 0}], id="h2d_pro_lone_deputy_is_preserved"
            ),
            # X2D launched April 2026 on the H2D-style dual-extruder convention (#988).
            pytest.param(
                "X2D",
                [254, 255],
                [-1, -1],
                [{"ams_id": 254, "slot_id": 0}, {"ams_id": 255, "slot_id": 0}],
                id="x2d_shares_the_h2d_deputy_convention",
            ),
            pytest.param(
                "H2S", [254], [-1], [{"ams_id": 255, "slot_id": 0}], id="h2s_is_single_nozzle_despite_the_h2_prefix"
            ),
        ],
    )
    def test_virtual_trays_flatten_to_minus_one_and_detail_per_family(
        self, mqtt_client, model, mapping, flat, detailed
    ):
        if model is not None:
            mqtt_client.model = model

        mqtt_client.start_print("test.3mf", ams_mapping=mapping)

        cmd = _published_command(mqtt_client)
        assert cmd["ams_mapping"] == flat
        assert cmd["ams_mapping2"] == detailed

    # `use_ams=False` states "this print feeds from the external holder". It must never
    # be produced by an all-NEGATIVE mapping: "no tray feeds this filament" is not the
    # same claim, and restating it that way paused a printer whose external holder was
    # unconfigured, demanding filament that was never there (003-H2S).
    @pytest.mark.parametrize(
        "model, mapping, use_ams",
        [
            pytest.param(None, [254], False, id="a_lone_external_spool_does_not_use_the_ams"),
            pytest.param(None, [254, 254], False, id="all_external_does_not_use_the_ams"),
            pytest.param(None, [0, 254], True, id="one_ams_tray_in_the_mix_still_uses_it"),
            pytest.param(None, [], True, id="an_empty_mapping_overrides_nothing"),
            pytest.param("H2S", [254], False, id="h2s_external_only_takes_the_single_nozzle_fallback"),
            # On dual-nozzle hardware the AMS path still does the nozzle routing.
            pytest.param("H2D", [254, 255], True, id="h2d_both_external_still_uses_the_ams"),
        ],
    )
    def test_use_ams_says_whether_a_tray_feeds_this_print(self, mqtt_client, model, mapping, use_ams):
        if model is not None:
            mqtt_client.model = model

        mqtt_client.start_print("test.3mf", ams_mapping=mapping, use_ams=True)

        assert _published_command(mqtt_client)["use_ams"] is use_ams

    def test_no_ams_mapping_omits_fields(self, mqtt_client):
        """Absent, not empty: the fields are left out entirely so firmware falls back
        to whatever the file itself specifies."""
        mqtt_client.start_print("test.3mf", ams_mapping=None)

        cmd = _published_command(mqtt_client)
        assert "ams_mapping" not in cmd
        assert "ams_mapping2" not in cmd

    # The calibration switches are JSON BOOLEANS on every model. An earlier revision
    # integer-encoded them for the H2 family on the belief that H2 firmware required
    # 0/1; a BambuStudio request-topic capture from a real H2D disproved it. The
    # companion `extrude_cali_flag` is an INT and pairs with flow_cali: 1 runs the
    # flow-dynamics pass, 0 skips it. `2` does NOT skip — on H2D 01.x stage 8
    # ("Calibrating dynamic flow") stayed in the `stg` queue and ran anyway (#1721,
    # verified live against the queue), which is why the skip value is 0.
    @pytest.mark.parametrize(
        "model, flow_cali, extrude_cali_flag",
        [
            pytest.param("X2D", True, 1, id="x2d_booleans_and_flow_cali_on"),
            pytest.param("H2S", True, 1, id="h2s_booleans_and_flow_cali_on"),
            pytest.param("P2S", False, 0, id="p2s_booleans_and_flow_cali_off"),
        ],
    )
    def test_calibration_switches_ride_as_booleans(self, mqtt_client, model, flow_cali, extrude_cali_flag):
        mqtt_client.model = model

        mqtt_client.start_print(
            "test.3mf",
            timelapse=True,
            bed_levelling=False,
            flow_cali=flow_cali,
            vibration_cali=False,
            layer_inspect=True,
        )

        cmd = _published_command(mqtt_client)
        assert cmd["timelapse"] is True
        assert cmd["bed_leveling"] is False
        assert cmd["flow_cali"] is flow_cali
        assert cmd["vibration_cali"] is False
        assert cmd["layer_inspect"] is True
        assert cmd["extrude_cali_flag"] == extrude_cali_flag

    # `nozzle_offset_cali` has no physical meaning on a single-nozzle machine, so the
    # transport downgrades it rather than trusting the caller: a stale queue item from
    # when a printer was misidentified as dual must not make firmware calibrate a head
    # it does not have (#1682). Same #1721 finding as above — 0 is the skip value that
    # is actually honoured, 2 left stage 39 in the queue.
    @pytest.mark.parametrize(
        "model, requested, wire_value",
        [
            pytest.param("P1S", None, 0, id="single_nozzle_default_is_skip"),
            pytest.param("P1S", True, 0, id="single_nozzle_downgrades_a_request_to_skip"),
            pytest.param("H2D", True, 1, id="dual_nozzle_honours_the_request"),
            pytest.param("H2D Pro", False, 0, id="dual_nozzle_honours_a_refusal"),
        ],
    )
    def test_nozzle_offset_calibration_is_dual_nozzle_only(self, mqtt_client, model, requested, wire_value):
        mqtt_client.model = model
        kwargs = {} if requested is None else {"nozzle_offset_cali": requested}

        mqtt_client.start_print("test.3mf", **kwargs)

        assert _published_command(mqtt_client)["nozzle_offset_cali"] == wire_value


class TestStartPrintUniqueIdentityFields:
    """Every submission needs its own identity triplet (#1011).

    Hardcoded "0" made third-party MQTT observers read an archive reprint as a
    continuation of the same job and report compounding durations; the printer also
    reuses ``gcode_start_time`` from the prior job when it cannot tell replays apart.
    The three fields share ONE value per submission, as Studio does.
    """

    client_kwargs = {"connected": True}

    def test_identity_fields_are_non_zero(self, mqtt_client):
        mqtt_client.start_print("test.3mf")

        cmd = _published_command(mqtt_client)
        assert cmd["project_id"] != "0"
        assert cmd["subtask_id"] != "0"
        assert cmd["task_id"] != "0"

    def test_identity_fields_are_all_equal_per_submission(self, mqtt_client):
        mqtt_client.start_print("test.3mf")

        cmd = _published_command(mqtt_client)
        assert cmd["project_id"] == cmd["subtask_id"] == cmd["task_id"]

    def test_md5_stays_empty(self, mqtt_client):
        """Firmware treats "" as "skip validation", and the real digest is not
        available here — a synthetic one would switch validation ON against a value
        that cannot match."""
        mqtt_client.start_print("test.3mf")

        assert _published_command(mqtt_client)["md5"] == ""

    def test_identity_fields_change_between_submissions(self, mqtt_client):
        mqtt_client.start_print("test.3mf")
        first = _published_command(mqtt_client)

        time.sleep(0.002)
        mqtt_client.start_print("test.3mf")
        second = _published_command(mqtt_client)

        assert first["task_id"] != second["task_id"]
        assert first["subtask_id"] != second["subtask_id"]
        assert first["project_id"] != second["project_id"]

    def test_submission_id_is_numeric_string(self, mqtt_client):
        """Digits-only, like Studio's cloud task ids: the DB column is VARCHAR(64) and
        the farm's own subtask_id parser reads '0' and '' as absent."""
        mqtt_client.start_print("test.3mf")

        task_id = _published_command(mqtt_client)["task_id"]
        assert task_id.isdigit()
        assert int(task_id) > 0
        assert len(task_id) <= 64

    def test_submission_id_fits_signed_int32(self, mqtt_client):
        """P1S firmware CLAMPS an oversized identity to signed int32 max (#1042). Send
        raw epoch-ms (~1.7e12) and every submission arrives as the same saturated
        constant, so fresh dispatches read as continuations of the last FAILED job and
        the printer never leaves IDLE."""
        mqtt_client.start_print("test.3mf")

        cmd = _published_command(mqtt_client)
        assert int(cmd["task_id"]) < 2**31
        assert int(cmd["project_id"]) < 2**31
        assert int(cmd["subtask_id"]) < 2**31

    def test_last_dispatch_subtask_id_records_the_minted_id(self, mqtt_client):
        """The farm has to know the id BEFORE the printer echoes it, so a restart can
        resume the job by id (#1485)."""
        assert mqtt_client.last_dispatch_subtask_id is None

        mqtt_client.start_print("test.3mf")

        assert mqtt_client.last_dispatch_subtask_id == _published_command(mqtt_client)["subtask_id"]

    def test_last_dispatch_subtask_id_updates_per_submission(self, mqtt_client):
        mqtt_client.start_print("test.3mf")
        first = mqtt_client.last_dispatch_subtask_id

        time.sleep(0.002)
        mqtt_client.start_print("test.3mf")

        assert mqtt_client.last_dispatch_subtask_id != first
        assert mqtt_client.last_dispatch_subtask_id == _published_command(mqtt_client)["subtask_id"]

    def test_unrelated_payload_fields_untouched(self, mqtt_client):
        """The rest of the frame is what the printer actually acts on."""
        mqtt_client.start_print("test.3mf")

        cmd = _published_command(mqtt_client)
        assert cmd["sequence_id"] == "20000"
        assert cmd["command"] == "project_file"
        assert cmd["param"] == "Metadata/plate_1.gcode"
        assert cmd["url"] == "ftp://test.3mf"
        assert cmd["file"] == "test.3mf"
        assert cmd["profile_id"] == "0"
        assert cmd["cfg"] == "0"
        assert cmd["subtask_name"] == "test"


class TestDeleteKProfileDualNozzleDetection:
    """``delete_kprofile`` picks its wire format by NOZZLE COUNT, and the question is
    answered by the runtime flag first, the model name second, and the serial never.

    ``_is_dual_nozzle`` (from ``device.extruder.info``) is the source of truth and
    covers models nobody has seen yet; the model name is the fallback before any push
    has arrived. Serial prefixes cannot decide it: H2S carries the same "094" prefix
    as the H2D but has one extruder (#1386, real H2S serials are "093…"), and
    post-2026 H2C batches ship "31B8B" instead of "094" (#1105). The dual-nozzle frame
    names an ``extruder_id`` and omits ``setting_id`` entirely.
    """

    @pytest.mark.parametrize(
        "serial, model, dual_runtime, dual_format",
        [
            pytest.param("09400A000000001", "H2D", False, True, id="h2d_by_model"),
            pytest.param("20P90A000000001", "X2D", False, True, id="x2d_by_model"),
            pytest.param("31B8BP000000001", "H2C", False, True, id="h2c_by_model_despite_new_prefix"),
            pytest.param("UNKNOWN", None, True, True, id="runtime_flag_covers_an_unknown_model"),
            pytest.param("09400S000000001", "H2S", False, False, id="h2s_is_single_despite_the_094_prefix"),
            pytest.param("22E00A000000001", "P2S", False, False, id="p2s_single"),
            pytest.param("00M00A000000001", "X1C", False, False, id="x1c_single"),
        ],
    )
    def test_the_frame_format_follows_the_nozzle_count(self, serial, model, dual_runtime, dual_format):
        client = _make_client(serial=serial, connected=True)
        client.model = model
        client._is_dual_nozzle = dual_runtime

        client.delete_kprofile(cali_idx=1, filament_id="GFA00", nozzle_id="HH00-0.4", setting_id="PFB123")

        cmd = _published_command(client)
        if dual_format:
            assert "setting_id" not in cmd
            assert cmd["extruder_id"] == 0
        else:
            assert cmd["setting_id"] == "PFB123"


class TestStaleReconnect:
    """Tests for stale connection detection and reconnect without UI bouncing."""

    client_kwargs = {"serial": "TEST_STALE"}

    def test_check_staleness_sets_flag_and_broadcasts_once(self, mqtt_client):
        """check_staleness() should set connected=False, broadcast, and set _stale_reconnecting."""
        state_changes = []
        mqtt_client.on_state_change = lambda s: state_changes.append(s.connected)
        mqtt_client.state.connected = True
        mqtt_client._last_message_time = time.time() - 120  # well past 60s threshold

        result = mqtt_client.check_staleness()

        assert result is False
        assert mqtt_client.state.connected is False
        assert mqtt_client._stale_reconnecting is True
        assert state_changes == [False]  # Exactly one broadcast

    def test_check_staleness_noop_when_not_connected(self, mqtt_client):
        """check_staleness() should not set flag when already disconnected."""
        mqtt_client.state.connected = False
        mqtt_client._last_message_time = time.time() - 120

        mqtt_client.check_staleness()

        assert mqtt_client._stale_reconnecting is False

    def test_check_staleness_noop_when_not_stale(self, mqtt_client):
        """check_staleness() should not set flag when messages are recent."""
        mqtt_client.state.connected = True
        mqtt_client._last_message_time = time.time() - 5  # 5s ago, well within 60s

        result = mqtt_client.check_staleness()

        assert result is True
        assert mqtt_client.state.connected is True
        assert mqtt_client._stale_reconnecting is False

    def test_check_staleness_logs_serial_hint_when_no_reports(self, mqtt_client, caplog):
        """#1465 — a stale connection that never received a status report logs
        an actionable serial-number hint, exactly once."""
        mqtt_client.state.connected = True
        mqtt_client._last_message_time = time.time() - 120
        mqtt_client._report_messages_since_connect = 0

        with caplog.at_level(logging.WARNING):
            mqtt_client.check_staleness()

        assert mqtt_client._zero_report_hint_logged is True
        assert any("zero status reports" in r.getMessage() for r in caplog.records)

        # Re-arm staleness — the hint must not log a second time.
        caplog.clear()
        mqtt_client.state.connected = True
        mqtt_client._last_message_time = time.time() - 120
        mqtt_client._last_stale_reconnect = 0.0  # bypass the reconnect cooldown
        with caplog.at_level(logging.WARNING):
            mqtt_client.check_staleness()
        assert not any("zero status reports" in r.getMessage() for r in caplog.records)

    def test_check_staleness_no_serial_hint_when_reports_received(self, mqtt_client, caplog):
        """A stale connection that DID receive reports (a normal mid-session
        quiet gap) must not log the serial-number hint."""
        mqtt_client.state.connected = True
        mqtt_client._last_message_time = time.time() - 120
        mqtt_client._report_messages_since_connect = 5

        with caplog.at_level(logging.WARNING):
            mqtt_client.check_staleness()

        assert mqtt_client._zero_report_hint_logged is False
        assert not any("zero status reports" in r.getMessage() for r in caplog.records)

    def test_on_disconnect_skipped_during_stale_reconnect(self, mqtt_client):
        """_on_disconnect should not broadcast state when _stale_reconnecting is set."""
        state_changes = []
        mqtt_client.on_state_change = lambda s: state_changes.append(s.connected)
        mqtt_client._stale_reconnecting = True
        mqtt_client.state.connected = False

        mqtt_client._on_disconnect(None, None)

        # No state change broadcast — check_staleness() already did it
        assert state_changes == []
        assert mqtt_client.state.connected is False

    def test_on_disconnect_fires_event_during_stale_reconnect(self, mqtt_client):
        """_on_disconnect must still fire _disconnection_event even during stale reconnect.

        If disconnect() is called while _stale_reconnecting is True (e.g. user removes
        the printer before paho reconnects), the event must fire so disconnect() doesn't hang.
        """
        mqtt_client._stale_reconnecting = True
        mqtt_client._disconnection_event = threading.Event()

        mqtt_client._on_disconnect(None, None)

        assert mqtt_client._disconnection_event.is_set()

    def test_on_connect_clears_stale_reconnecting_flag(self, mqtt_client):
        """_on_connect should clear _stale_reconnecting and restore connected=True."""
        mqtt_client._stale_reconnecting = True
        mqtt_client.state.connected = False

        subscribe_calls = []
        mock_client = type(
            "MockClient",
            (),
            {
                "subscribe": lambda self, topic: subscribe_calls.append(topic) or (0, 1),
            },
        )()

        mqtt_client._on_connect(mock_client, None, None, 0)

        assert mqtt_client._stale_reconnecting is False
        assert mqtt_client.state.connected is True

    def test_full_stale_reconnect_cycle_no_bounce(self, mqtt_client):
        """Full cycle: stale → disconnect callback → reconnect. UI should see exactly one disconnect."""
        state_changes = []
        mqtt_client.on_state_change = lambda s: state_changes.append(s.connected)
        mqtt_client.state.connected = True
        mqtt_client._last_message_time = time.time() - 120

        # Step 1: Stale detection triggers
        mqtt_client.check_staleness()
        assert state_changes == [False]

        # Step 2: Paho fires disconnect callback (from socket close)
        mqtt_client._on_disconnect(None, None)
        # Should NOT add another state change
        assert state_changes == [False]

        # Step 3: Paho reconnects
        subscribe_calls = []
        mock_client = type(
            "MockClient",
            (),
            {
                "subscribe": lambda self, topic: subscribe_calls.append(topic) or (0, 1),
            },
        )()
        mqtt_client._on_connect(mock_client, None, None, 0)
        assert state_changes == [False, True]  # Now connected again
        assert mqtt_client._stale_reconnecting is False

    def test_spurious_disconnect_suppressed_when_recent_messages(self, mqtt_client):
        """Non-error disconnect with recent messages should be suppressed."""
        state_changes = []
        mqtt_client.on_state_change = lambda s: state_changes.append(s.connected)
        mqtt_client.state.connected = True
        mqtt_client._last_message_time = time.time() - 3  # 3s ago

        # Non-error disconnect (rc=None)
        mqtt_client._on_disconnect(None, None)

        assert state_changes == []
        assert mqtt_client.state.connected is True

    def test_error_disconnect_not_suppressed_despite_recent_messages(self, mqtt_client):
        """Error disconnect should always be processed, even with recent messages."""
        state_changes = []
        mqtt_client.on_state_change = lambda s: state_changes.append(s.connected)
        mqtt_client.state.connected = True
        mqtt_client._last_message_time = time.time() - 3  # 3s ago

        # Error disconnect (rc.is_failure = True)
        rc = ReasonCode(mqtt.CONNACK >> 4, identifier=0x80)  # Failure code
        mqtt_client._on_disconnect(None, None, rc=rc)

        assert state_changes == [False]
        assert mqtt_client.state.connected is False


class TestDoorOpenParsing:
    """The enclosure door is bit 23 — of ``home_flag`` on the X1 family and of
    ``stat`` everywhere else, and each family must IGNORE the other's field.

    ``home_flag`` arrives as an int and ``stat`` as a hex STRING, so the non-X1 lane
    also has to survive a value that will not parse.
    """

    @pytest.mark.parametrize(
        "model, print_data, door_open",
        [
            pytest.param("X1C", {"home_flag": 0xC0E5CD98}, True, id="x1c_home_flag_bit23_set"),
            pytest.param("X1C", {"home_flag": 0xC065CD98}, False, id="x1c_home_flag_bit23_clear"),
            pytest.param(
                "X1C", {"home_flag": 0xC065CD98, "stat": "47A58000"}, False, id="x1c_ignores_stat_home_flag_wins"
            ),
            pytest.param("H2D", {"stat": "640A58000"}, True, id="h2d_stat_bit23_set"),
            pytest.param("H2D", {"stat": "640258000"}, False, id="h2d_stat_bit23_clear"),
            pytest.param(
                "H2D", {"home_flag": 0xC0E5CD98, "stat": "640258000"}, False, id="h2d_ignores_home_flag_stat_wins"
            ),
        ],
    )
    def test_the_door_bit_comes_from_the_field_its_family_uses(self, model, print_data, door_open):
        client = _make_client(serial="TEST", model=model)
        client.state.door_open = True  # so a False result is a real transition, not a default

        client._update_state(print_data)

        assert client.state.door_open is door_open

    @pytest.mark.parametrize("previous", [True, False], ids=["previously_open", "previously_closed"])
    def test_an_unparseable_stat_leaves_the_reading_alone(self, previous):
        """``stat`` is a hex string, so a malformed one must neither raise nor be read
        as a closed door — the last known reading stands."""
        client = _make_client(serial="TEST", model="H2D")
        client.state.door_open = previous

        client._update_state({"stat": "not-hex"})

        assert client.state.door_open is previous


class TestSdCardParsing:
    """Storage presence comes from the top-level ``sdcard`` field ONLY.

    On the H2 series this flag is what reports the USB drive the farm dispatches
    from (the field name is legacy; there is no microSD slot), so a false negative
    reads as "no storage" and every upload fails. ``home_flag`` is deliberately not
    consulted: heartbeat pushes clear those bits even with storage inserted, and no
    reliable heartbeat-vs-full-push heuristic existed.
    """

    @pytest.mark.parametrize(
        "sdcard, present",
        [
            pytest.param("HAS_SDCARD_NORMAL", True, id="the_string_form"),
            # `1 is True` is False — an identity check here flapped on this value.
            pytest.param(1, True, id="the_int_form"),
            pytest.param(True, True, id="the_bool_form"),
            pytest.param(False, False, id="bool_false_clears_it"),
        ],
    )
    def test_storage_presence_reads_the_sdcard_field_in_every_form(self, sdcard, present):
        client = _make_client(serial="TEST", model="H2D")

        client._update_state({"sdcard": sdcard})

        assert client.state.sdcard is present

    def test_home_flag_alone_does_not_touch_sdcard(self):
        client = _make_client(serial="TEST", model="H2D")
        client.state.sdcard = True

        for home_flag in (0x00000000, 0x00000100, 0x00000200):
            client._update_state({"home_flag": home_flag})

        assert client.state.sdcard is True


class TestZombieSessionDetection:
    """A session where telemetry flows but commands never arrive (#887).

    Nothing in the status stream distinguishes it from a healthy session, so the
    detector counts ``ams_filament_setting`` commands that go unanswered for 10 s and
    force-reconnects on the second. ANY response resets both the timer and the
    counter, because the response itself proves the channel is alive.
    """

    client_kwargs = {"connected": True}

    @pytest.fixture
    def mqtt_client(self, mqtt_client):
        mqtt_client._client.socket.return_value = MagicMock()
        mqtt_client._connect_time = time.monotonic() - 10.0
        mqtt_client.state.developer_mode = True  # keep the dev-mode probe out of the way
        return mqtt_client

    RESPONSE = {"print": {"command": "ams_filament_setting", "sequence_id": "0", "result": "success"}}

    @staticmethod
    def _let_the_watchdog_see_a_timeout(mqtt_client):
        mqtt_client._last_ams_cmd_time = time.monotonic() - 11.0
        mqtt_client._update_state({"gcode_state": "IDLE"})

    def test_initial_state_is_clean(self, mqtt_client):
        assert mqtt_client._last_ams_cmd_time == 0.0
        assert mqtt_client._ams_cmd_unanswered == 0

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("set_filament_setting", id="ams_set_filament_setting"),
            pytest.param("reset_slot", id="reset_ams_slot"),
        ],
    )
    def test_every_ams_write_arms_the_watchdog(self, mqtt_client, command):
        before = time.monotonic()

        if command == "set_filament_setting":
            mqtt_client.ams_set_filament_setting(
                ams_id=0,
                tray_id=0,
                tray_info_idx="GFL99",
                tray_type="PLA",
                tray_sub_brands="",
                tray_color="FF0000FF",
                nozzle_temp_min=190,
                nozzle_temp_max=230,
            )
        else:
            mqtt_client.reset_ams_slot(ams_id=0, tray_id=0)

        assert mqtt_client._last_ams_cmd_time >= before

    def test_response_clears_pending(self, mqtt_client):
        mqtt_client._last_ams_cmd_time = time.monotonic()
        mqtt_client._ams_cmd_unanswered = 1

        mqtt_client._process_message(self.RESPONSE)

        assert mqtt_client._last_ams_cmd_time == 0.0
        assert mqtt_client._ams_cmd_unanswered == 0

    def test_single_timeout_increments_counter(self, mqtt_client):
        """One silence is not a verdict; the timer is zeroed so the same command
        cannot be counted again on the next push."""
        self._let_the_watchdog_see_a_timeout(mqtt_client)

        assert mqtt_client._ams_cmd_unanswered == 1
        assert mqtt_client._last_ams_cmd_time == 0.0
        assert mqtt_client.state.connected is True

    def test_two_timeouts_force_reconnect(self, mqtt_client):
        """Detection runs on paho's network thread, where there is no asyncio loop, so
        the teardown takes the socket-close path — ``loop_stop`` from inside the loop
        would deadlock. Hard-reset is for async callers on the dispatch path."""
        state_changes = []
        mqtt_client.on_state_change = state_changes.append

        self._let_the_watchdog_see_a_timeout(mqtt_client)
        assert mqtt_client._ams_cmd_unanswered == 1
        assert mqtt_client.state.connected is True

        self._let_the_watchdog_see_a_timeout(mqtt_client)

        assert mqtt_client._ams_cmd_unanswered == 0, "reset after the reconnect"
        assert mqtt_client.state.connected is False
        assert mqtt_client._stale_reconnecting is True
        mqtt_client._client.socket().close.assert_called()
        assert state_changes

    def test_late_response_after_watchdog_clears_counter_issue_1164(self, mqtt_client):
        """#1164: the response that arrives AFTER the watchdog already counted the
        command must still reset the counter. While the reset required a non-zero
        timer, one sluggish response left the counter armed at 1 forever, and the next
        slow response — hours later, on an unrelated command — took it to 2 and
        force-reconnected. It surfaced as "AMS slot config stops reaching the printer
        about six changes in"."""
        self._let_the_watchdog_see_a_timeout(mqtt_client)
        assert mqtt_client._ams_cmd_unanswered == 1
        assert mqtt_client._last_ams_cmd_time == 0.0, "the watchdog zeroed it"

        mqtt_client._process_message(self.RESPONSE)
        assert mqtt_client._ams_cmd_unanswered == 0, "a response proves the channel is alive"

        # And the count starts from scratch, so one later timeout cannot reconnect.
        self._let_the_watchdog_see_a_timeout(mqtt_client)
        assert mqtt_client._ams_cmd_unanswered == 1
        assert mqtt_client.state.connected is True

    def test_on_connect_resets_tracking(self, mqtt_client):
        mqtt_client._last_ams_cmd_time = time.monotonic()
        mqtt_client._ams_cmd_unanswered = 5
        mqtt_client._client.subscribe.return_value = (0, 1)

        mqtt_client._on_connect(mqtt_client._client, None, None, 0)

        assert mqtt_client._last_ams_cmd_time == 0.0
        assert mqtt_client._ams_cmd_unanswered == 0

    def test_no_check_when_no_command_pending(self, mqtt_client):
        assert mqtt_client._last_ams_cmd_time == 0.0

        mqtt_client._update_state({"gcode_state": "IDLE"})

        assert mqtt_client._ams_cmd_unanswered == 0

    def test_no_timeout_within_window(self, mqtt_client):
        mqtt_client._last_ams_cmd_time = time.monotonic() - 5.0

        mqtt_client._update_state({"gcode_state": "IDLE"})

        assert mqtt_client._ams_cmd_unanswered == 0
        assert mqtt_client._last_ams_cmd_time > 0, "still pending"


class TestHMSUserActionFiltering:
    """The cancel echoes are status, not faults, and must never reach
    ``state.hms_errors``.

    The firmware reports a user stop as an HMS code. Left in, it keeps the printer
    card on "1 problem" after every stop and lights the red pip. Both routes into the
    list have to filter: the ``hms`` array and the separate ``print_error`` scalar.
    Filtering is per ENTRY, because a user who cancels mid-fault gets the real fault
    alongside the echo in the same push.
    """

    client_kwargs = {"serial": "TEST_HMS"}

    @pytest.mark.parametrize(
        "print_data, codes",
        [
            # 0300_400C "The task was canceled." — the user-cancel echo.
            pytest.param({"hms": [{"attr": 0x03000300, "code": 0x400C}]}, [], id="hms_0300_400c_task_cancelled"),
            # 0500_400E "Printing was cancelled." — the nozzle-module echo of the same act.
            pytest.param({"hms": [{"attr": 0x05000300, "code": 0x400E}]}, [], id="hms_0500_400e_printing_cancelled"),
            # 0300_4057 is Z-axis step loss: a real fault.
            pytest.param(
                {"hms": [{"attr": 0x03000100, "code": 0x4057}]}, ["0x4057"], id="hms_0300_4057_layer_shift_is_a_fault"
            ),
            pytest.param(
                {"hms": [{"attr": 0x03000300, "code": 0x400C}, {"attr": 0x07FF0200, "code": 0x8011}]},
                ["0x8011"],
                id="a_cancel_echo_beside_a_runout_drops_only_the_echo",
            ),
            pytest.param({"print_error": 0x0500_400E}, [], id="print_error_lane_filters_the_echo_too"),
            pytest.param({"print_error": 0x0500_8061}, ["0x8061"], id="print_error_lane_passes_a_real_fault"),
        ],
    )
    def test_user_action_echoes_are_dropped_on_both_routes(self, mqtt_client, print_data, codes):
        mqtt_client._update_state(print_data)

        assert [e.code for e in mqtt_client.state.hms_errors] == codes


class TestHMSFullCode:
    """``full_code`` is the key the FIRMWARE matches on, so it must not be truncated.

    An ``hms[]`` entry is a 64-bit identifier (``attr`` and ``code``, 32 bits each) —
    the 16-char hex BambuStudio matches against for ``err`` on ``idle_ignore``.
    Truncating it to the 8-char short code drops 32 bits and the firmware silently
    rejects the command (#1830, on H2C). ``print_error`` is already 32 bits, so there
    its ``full_code`` is the 8-char form. The action catalog is consulted long-form
    FIRST (the specific variant) and falls back to the short code, where the
    base-class entries live.
    """

    client_kwargs = {"serial": "TEST_FULLCODE"}

    @pytest.mark.parametrize(
        "print_data, full_code",
        [
            # Displayed as 07FF_0200_0000_8011 in the wiki.
            pytest.param(
                {"hms": [{"attr": 0x07FF0200, "code": 0x8011}]}, "07FF02000000" + "8011", id="hms_array_16_char"
            ),
            pytest.param({"print_error": 0x05008051}, "05008051", id="print_error_8_char"),
        ],
    )
    def test_each_route_records_the_identifier_the_firmware_matches(self, mqtt_client, print_data, full_code):
        mqtt_client._update_state(print_data)

        assert len(mqtt_client.state.hms_errors) == 1
        assert mqtt_client.state.hms_errors[0].full_code == full_code

    @pytest.mark.parametrize(
        "long_form_hits, lookups, actions",
        [
            pytest.param(True, ["07FF020000008011"], ["RESUME_PRINTING"], id="the_specific_variant_wins"),
            pytest.param(
                False,
                ["07FF020000008011", "07FF8011"],
                ["CHECK_ASSISTANT"],
                id="falls_back_to_the_base_class_short_code",
            ),
        ],
    )
    def test_the_catalog_is_asked_long_form_first(self, mqtt_client, monkeypatch, long_form_hits, lookups, actions):
        calls = []

        def fake_lookup(device, code):
            calls.append(code)
            if len(code) == 16:
                return ["RESUME_PRINTING"] if long_form_hits else []
            return ["CHECK_ASSISTANT"]

        monkeypatch.setattr(mqtt_mod, "get_actions_for_error_code", fake_lookup)

        mqtt_client._update_state({"hms": [{"attr": 0x07FF0200, "code": 0x8011}]})

        assert calls == lookups
        assert mqtt_client.state.hms_errors[0].actions == actions


class TestHMSWireStamp:
    """``hms_wire_at`` is the transport truth behind the appearance detector: it
    advances ONLY on a push that carried wire HMS evidence.

    A LOCAL clear (a new print, the operator's "clear errors") empties
    ``hms_errors`` without the printer having said anything, so it must not advance
    the clock — otherwise a code still standing on the wire reads as a fresh
    appearance on the next push and re-fires every consumer.

    The "does not advance" cases pin the clock to SENTINEL first and assert exact
    equality: a real stamp lands a large ``time.monotonic()`` value, so the assertion
    still discriminates when two ``monotonic()`` calls fall inside one Windows clock
    tick — where ``== previous_stamp`` would silently pass.
    """

    SENTINEL = 1.0
    STANDING = {"hms": [{"attr": 0x07FF0200, "code": 0x8011}]}

    client_kwargs = {"serial": "TEST_WIRE"}

    def test_starts_unstamped(self, mqtt_client):
        assert mqtt_client.state.hms_wire_at == 0.0

    def test_hms_key_advances_the_stamp(self, mqtt_client):
        mqtt_client._update_state(self.STANDING)

        assert mqtt_client.state.hms_wire_at > self.SENTINEL

    def test_empty_hms_list_advances_the_stamp(self, mqtt_client):
        """An all-clear IS wire evidence — without it the detector could never see a
        code leave, so every flap-and-return would be missed."""
        mqtt_client._update_state(self.STANDING)
        assert len(mqtt_client.state.hms_errors) == 1
        mqtt_client.state.hms_wire_at = 0.0

        mqtt_client._update_state({"hms": []})

        assert mqtt_client.state.hms_errors == []
        assert mqtt_client.state.hms_wire_at > self.SENTINEL

    def test_print_error_append_advances_the_stamp(self, mqtt_client):
        mqtt_client._update_state({"print_error": 0x05008061})

        assert len(mqtt_client.state.hms_errors) == 1
        assert mqtt_client.state.hms_wire_at > self.SENTINEL

    def test_report_without_hms_key_does_not_advance_the_stamp(self, mqtt_client):
        mqtt_client.state.hms_wire_at = self.SENTINEL

        mqtt_client._update_state({"gcode_state": "RUNNING", "layer_num": 5})

        assert mqtt_client.state.hms_wire_at == self.SENTINEL

    def test_new_print_local_clear_does_not_advance_the_stamp(self, mqtt_client):
        """The new-print branch wipes ``hms_errors`` locally; the printer said nothing."""
        mqtt_client._update_state({"gcode_state": "IDLE", "gcode_file": "part.gcode", **self.STANDING})
        assert len(mqtt_client.state.hms_errors) == 1
        mqtt_client.state.hms_wire_at = self.SENTINEL

        mqtt_client._update_state({"gcode_state": "RUNNING", "gcode_file": "part.gcode"})

        assert mqtt_client.state.hms_errors == [], "a new print must clear errors locally"
        assert mqtt_client.state.hms_wire_at == self.SENTINEL

    def test_clear_hms_errors_command_does_not_advance_the_stamp(self, mqtt_client):
        mqtt_client._update_state(self.STANDING)
        assert len(mqtt_client.state.hms_errors) == 1
        mqtt_client.state.hms_wire_at = self.SENTINEL
        mqtt_client._client = MagicMock()
        mqtt_client.state.connected = True

        assert mqtt_client.clear_hms_errors() is True

        assert mqtt_client.state.hms_errors == []
        assert mqtt_client.state.hms_wire_at == self.SENTINEL

    def test_deduped_print_error_does_not_advance_the_stamp(self, mqtt_client):
        """A ``print_error`` repeating a code already listed appends nothing, so it is
        not new evidence — stamping would re-fire the standing code."""
        mqtt_client._update_state({"hms": [{"attr": 0x05000000, "code": 0x8061}]})
        assert len(mqtt_client.state.hms_errors) == 1
        mqtt_client.state.hms_wire_at = self.SENTINEL

        mqtt_client._update_state({"print_error": 0x05008061})

        assert len(mqtt_client.state.hms_errors) == 1, "a duplicate must not append"
        assert mqtt_client.state.hms_wire_at == self.SENTINEL

    def test_print_error_cancel_echo_does_not_advance_the_stamp(self, mqtt_client):
        """The echo is filtered out of the list, so it appends nothing."""
        mqtt_client._update_state({"print_error": 0x0500400E})

        assert mqtt_client.state.hms_errors == []
        assert mqtt_client.state.hms_wire_at == 0.0


class TestHMSSeverityDecode:
    """Severity is the HIGH 16 BITS OF ``code`` — 1 fatal, 2 serious, 3 common, 4
    info — not ``(attr >> 8) & 0xF``, which read every real fault as fatal.

    Below 0x4000 the full 32-bit code is a status/phase indicator rather than a
    fault, and that filter must not swallow a genuine severity-3 fault: the live
    MicroSD fault (attr 0x05000100, code 0x00030004) sits above it.
    """

    client_kwargs = {"serial": "TEST_SEV"}

    @pytest.mark.parametrize(
        "attr, code, severity, full_code",
        [
            pytest.param(0x05000100, 0x00030004, 3, "0500010000030004", id="microsd_fault_is_severity_3_common"),
            pytest.param(0x03000100, 0x00024057, 2, None, id="high16_0x0002_is_severity_2_serious"),
            pytest.param(0x03000100, 0x0002, None, None, id="below_0x4000_is_a_status_code_not_a_fault"),
            pytest.param(0x03000300, 0x400C, None, None, id="cancel_echo_is_still_filtered_here"),
        ],
    )
    def test_severity_comes_from_the_code_high_word(self, mqtt_client, attr, code, severity, full_code):
        mqtt_client._update_state({"hms": [{"attr": attr, "code": code}]})

        if severity is None:
            assert mqtt_client.state.hms_errors == []
            return
        assert len(mqtt_client.state.hms_errors) == 1
        assert mqtt_client.state.hms_errors[0].severity == severity
        if full_code is not None:
            assert mqtt_client.state.hms_errors[0].full_code == full_code

    def test_completion_event_carries_full_code(self, mqtt_client):
        """The terminal's HMS dicts carry ``full_code`` too, so the failure-reason
        chain can do a lossless catalog lookup."""
        captured = {}
        mqtt_client.on_print_start = lambda data: None
        mqtt_client.on_print_complete = captured.update
        mqtt_client._previous_gcode_state = "PREPARE"

        mqtt_client._process_message(
            {
                "print": {
                    "gcode_state": "FAILED",
                    "gcode_file": "/data/Metadata/plate_1.gcode",
                    "hms": [{"attr": 0x05000100, "code": 0x00030004}],
                }
            }
        )

        assert captured.get("status") == "failed"
        hms = captured.get("hms_errors") or []
        assert hms and hms[0]["full_code"] == "0500010000030004"
        assert hms[0]["severity"] == 3


class TestForceReconnectRouting:
    """#1136 — force_reconnect_stale_session routes between hard-reset (full
    paho-client teardown, wipes the QoS 1 queue) and socket-close (the legacy
    behaviour, safe to call from paho's own network thread). The routing
    decision is based on whether an asyncio loop is running: hard-reset
    requires loop_stop() which would deadlock if called from inside the
    network thread itself."""

    client_kwargs = {"serial": "TEST_HARD_RESET", "connected": True}

    def test_routing_falls_back_to_socket_close_without_running_loop(self, mqtt_client):
        """Sync caller → no asyncio loop → socket-close path (legacy behaviour
        preserved for paho-thread callers like zombie detection)."""
        mqtt_client.force_reconnect_stale_session("test")
        mqtt_client._client.socket().close.assert_called()
        # Old client is NOT torn down on this path; same-instance reconnect
        # via paho's auto-reconnect handles it.
        assert mqtt_client._client is not None

    def test_routing_uses_hard_reset_when_loop_is_running(self, mqtt_client):
        """Async caller → loop available → hard-reset path wipes the queue."""
        original = mqtt_client._client
        # Stub connect() so the rebuild doesn't open a real socket.
        mqtt_client.connect = lambda loop=None: None

        async def _trigger():
            mqtt_client.force_reconnect_stale_session("test")

        asyncio.run(_trigger())
        original.disconnect.assert_called()
        original.loop_stop.assert_called()
        # connect() stub didn't repopulate _client, so it's None — the contract
        # in production is that connect() builds a fresh mqtt.Client here.
        assert mqtt_client._client is None

    def test_marks_state_disconnected_and_broadcasts(self, mqtt_client):
        """Both routing paths must broadcast the disconnected state once."""
        broadcasts: list[bool] = []
        mqtt_client.on_state_change = lambda s: broadcasts.append(s.connected)
        mqtt_client.force_reconnect_stale_session("test")
        assert mqtt_client.state.connected is False
        assert mqtt_client._stale_reconnecting is True
        assert broadcasts == [False]


class TestHardResetClientDirect:
    """``_hard_reset_client`` itself, driven directly so the routing decision above
    cannot mask it.

    The old paho client must be told to DISCONNECT (so the broker drops the session)
    and then stopped (so its network thread exits, taking the QoS 1 queue with it),
    and the reference must be dropped — anything still publishing through a dying
    client publishes into nothing.
    """

    client_kwargs = {"serial": "TEST_HARD_DIRECT", "connected": True}

    @pytest.fixture
    def mqtt_client(self, mqtt_client):
        """``connect`` is stubbed so the rebuild does not open a real socket."""
        mqtt_client.connect = lambda loop=None: None
        return mqtt_client

    def test_disconnects_and_stops_old_client(self, mqtt_client):
        original = mqtt_client._client

        mqtt_client._hard_reset_client()

        original.disconnect.assert_called()
        original.loop_stop.assert_called()

    def test_clears_client_reference(self, mqtt_client):
        mqtt_client._hard_reset_client()

        assert mqtt_client._client is None

    def test_swallows_disconnect_exception(self, mqtt_client):
        """A paho client already in an error state must not take the whole dispatch
        path down with it — one broken client would brick every future dispatch."""
        original = mqtt_client._client
        original.disconnect.side_effect = RuntimeError("boom")

        mqtt_client._hard_reset_client()

        original.loop_stop.assert_called(), "the stop is still attempted after a failed disconnect"
        assert mqtt_client._client is None


class TestStartPrintRecordsDispatchedPlate:
    """``start_print`` records WHICH PLATE it dispatched, because the printer's echo
    may not say (#1166).

    Some firmware (P1S 01.10.00.00) puts only the .3mf filename in
    ``print.gcode_file``, so the plate regex falls back to plate 1 and the card shows
    the wrong thumbnail. Recording it at the publish site lets ``resolve_plate_id``
    answer without introspecting the 3MF, and the subtask name is recorded beside it
    so the record can be matched against the printer's own echo.
    """

    client_kwargs = {"connected": True}

    @pytest.mark.parametrize(
        "kwargs, plate_id, subtask",
        [
            pytest.param({"plate_id": 2}, 2, "Luigi", id="an_explicit_plate"),
            # The legacy single-plate flow still records a dispatch.
            pytest.param({}, 1, "Luigi", id="defaults_to_plate_one"),
        ],
    )
    def test_dispatched_plate_recorded_after_start_print(self, mqtt_client, kwargs, plate_id, subtask):
        assert mqtt_client.state.dispatched_plate_id is None
        assert mqtt_client.state.dispatched_subtask is None

        mqtt_client.start_print("Luigi.3mf", **kwargs)

        assert mqtt_client.state.dispatched_plate_id == plate_id
        assert mqtt_client.state.dispatched_subtask == subtask

    def test_dispatched_plate_overwritten_by_subsequent_dispatch(self, mqtt_client):
        """Never serve a stale plate from an older print."""
        mqtt_client.start_print("First.3mf", plate_id=4)

        mqtt_client.start_print("Second.3mf", plate_id=2)

        assert mqtt_client.state.dispatched_plate_id == 2
        assert mqtt_client.state.dispatched_subtask == "Second"

    def test_dispatched_plate_not_recorded_when_publish_skipped(self, mqtt_client):
        """A refused dispatch must leave no record — the next /cover call would
        otherwise believe a phantom dispatch happened."""
        mqtt_client.state.connected = False

        assert mqtt_client.start_print("Phantom.3mf", plate_id=3) is False

        assert mqtt_client.state.dispatched_plate_id is None
        assert mqtt_client.state.dispatched_subtask is None


class TestStartPrintNozzleMappingDispatch:
    """``nozzle_mapping`` forwards the slicer's physical nozzle choice (#1780).

    BambuStudio's ``project_file`` command for O1C2 carries a per-filament rack
    position array. Without forwarding it the H2C firmware falls back to "last
    matching nozzle type" auto-pick and silently ignores the operator's choice. It
    goes on the wire as a LIST, matching BambuStudio, and only on dual-nozzle
    hardware — a single-nozzle printer has no rack to address, and a queue item can
    carry a stale capture from before a model change.

    Anything unusable is dropped and the dispatch PROCEEDS: the fallback is the
    firmware's auto-pick, which is merely the pre-fix behaviour, whereas refusing
    would brick every dispatch on one bad row.
    """

    client_kwargs = {"serial": "TEST_O1C2", "connected": True}

    @pytest.mark.parametrize(
        "is_dual, nozzle_mapping, injected, warns",
        [
            pytest.param(
                True,
                json.dumps([16, -1, -1, 1, -1, -1, -1, -1]),
                [16, -1, -1, 1, -1, -1, -1, -1],
                False,
                id="dual_nozzle_forwards_the_parsed_array",
            ),
            pytest.param(False, json.dumps([16, 0, 19]), None, False, id="single_nozzle_omits_it_even_if_asked"),
            pytest.param(True, None, None, False, id="no_slicer_pick_no_field"),
            # An empty-string column (legacy data, or a NOT NULL recovery shim) is absent.
            pytest.param(True, "", None, False, id="empty_string_is_absent_not_malformed"),
            pytest.param(True, "not valid json {", None, True, id="malformed_json_warns_and_dispatches_anyway"),
        ],
    )
    def test_the_rack_choice_only_rides_dual_nozzle_dispatches(
        self, mqtt_client, caplog, is_dual, nozzle_mapping, injected, warns
    ):
        mqtt_client._is_dual_nozzle = is_dual
        if not is_dual:
            mqtt_client.model = "P1S"

        with caplog.at_level(logging.WARNING):
            assert mqtt_client.start_print("test.3mf", nozzle_mapping=nozzle_mapping) is True

        cmd = _published_command(mqtt_client)
        if injected is None:
            assert "nozzle_mapping" not in cmd
        else:
            assert cmd["nozzle_mapping"] == injected
        assert any("Invalid nozzle_mapping" in rec.message for rec in caplog.records) is warns


class TestFilamentTrackSwitchDetection:
    """The Filament Track Switch is detected by PRESENCE of ``device.fila_switch``.

    The FTS sits between an AMS and the extruders and can route any slot to either
    nozzle, so while it is installed each AMS reports info bits 8-11 = 0xE
    (uninitialized) — slots are no longer tied to one extruder, and the frontend must
    stop applying its per-extruder filter. Presence alone is the signal: a firmware
    that omits or malforms the routing arrays still has an FTS fitted.
    """

    @pytest.mark.parametrize(
        "device, installed, in_slots, out_extruders",
        [
            pytest.param(None, False, [], [], id="no_push_at_all"),
            pytest.param({"extruder": {"state": 0}}, False, [], [], id="a_device_block_without_fila_switch"),
            pytest.param(
                {"fila_switch": {"in": [-1, 2], "info": 2, "out": [0, 1], "stat": 0}},
                True,
                [-1, 2],
                [0, 1],
                id="fila_switch_present_with_its_routing",
            ),
            pytest.param(
                {"fila_switch": {"stat": 0, "info": 0}}, True, [], [], id="fila_switch_present_without_arrays"
            ),
        ],
    )
    def test_presence_of_the_field_is_the_signal(self, mqtt_client, device, installed, in_slots, out_extruders):
        if device is not None:
            mqtt_client._update_state({"gcode_state": "IDLE", "device": device})

        fs = mqtt_client.state.fila_switch
        assert fs.installed is installed
        assert fs.in_slots == in_slots
        assert fs.out_extruders == out_extruders

    def test_the_routing_detail_is_captured(self, mqtt_client):
        """``stat`` and ``info`` ride along for triage."""
        mqtt_client._update_state({"device": {"fila_switch": {"in": [-1, 2], "info": 2, "out": [0, 1], "stat": 0}}})

        fs = mqtt_client.state.fila_switch
        assert fs.stat == 0
        assert fs.info == 2


class TestAmsLoadFilamentEncoding:
    """``ams_change_filament`` addresses a load target three different ways (#891).

    A regular tray splits into unit and local slot and carries no temperatures
    (-1 = "use whatever you have"). The external holders do not: the LEFT/legacy
    holder keeps the single-extruder capture's shape (``slot_id`` carrying 254),
    while the RIGHT one is addressed by extruder index with the live nozzle
    temperature — and a cold nozzle is replaced by a sane default, because the
    printer rejects a nonsensical temperature outright.
    """

    client_kwargs = {"connected": True}

    @pytest.mark.parametrize(
        "tray_id, nozzle_2_temp, ams_id, slot_id, target, temp",
        [
            pytest.param(5, None, 1, 1, 5, -1, id="regular_tray_5_is_unit1_slot1"),
            pytest.param(254, None, 255, 254, 254, -1, id="external_left_keeps_the_legacy_shape"),
            pytest.param(255, 215.0, 255, 0, 255, 215, id="external_right_carries_the_live_nozzle_temp"),
            pytest.param(255, 25.0, 255, 0, 255, 215, id="external_right_falls_back_when_the_nozzle_is_cold"),
        ],
    )
    def test_each_target_gets_its_own_encoding(
        self, mqtt_client, tray_id, nozzle_2_temp, ams_id, slot_id, target, temp
    ):
        if nozzle_2_temp is not None:
            mqtt_client.state.temperatures["nozzle_2"] = nozzle_2_temp

        assert mqtt_client.ams_load_filament(tray_id) is True

        cmd = _published_command(mqtt_client)
        assert cmd["command"] == "ams_change_filament"
        assert cmd["ams_id"] == ams_id
        assert cmd["slot_id"] == slot_id
        assert cmd["target"] == target
        assert cmd["curr_temp"] == temp
        assert cmd["tar_temp"] == temp

    def test_returns_false_when_disconnected(self, mqtt_client):
        mqtt_client.state.connected = False

        assert mqtt_client.ams_load_filament(0) is False

        mqtt_client._client.publish.assert_not_called()


class TestAmsFilamentSettingExternalSpoolEncoding:
    """``ams_filament_setting`` for the external spool, per #1279.

    The encoding comes from a captured BambuStudio → X1C exchange::

        REQ {"command":"ams_filament_setting","ams_id":255,"tray_id":254,"slot_id":0,…}
        REP {"result":"success",…}

    ``tray_id: 0`` for the single-external case is what the P1S in #1279 answered
    with ``result: "fail"``. The number of ``vt_tray`` entries is what tells a
    single-external printer from a dual-external one; the dual case was NOT in the
    capture, so it keeps its legacy shape until its own capture exists.
    """

    client_kwargs = {"connected": True}

    SETTING = {
        "tray_info_idx": "GFA01",
        "tray_type": "PLA",
        "tray_sub_brands": "PLA Matte",
        "tray_color": "FFFFFFFF",
        "nozzle_temp_min": 190,
        "nozzle_temp_max": 230,
    }

    @pytest.mark.parametrize(
        "vt_tray, ams_id, tray_id, wire_ams_id, wire_tray_id, wire_slot_id",
        [
            pytest.param([{"id": "255"}], 255, 0, 255, 254, 0, id="single_external_sends_tray_id_254"),
            pytest.param([], 0, 2, 0, 2, 2, id="a_regular_ams_tray_is_untouched"),
            pytest.param([], 128, 0, 128, 0, 0, id="ams_ht_keeps_one_tray_per_unit"),
            # Ext-L on an H2D: unit 254, and tray_id stays 0 — pinned so a future
            # capture-driven change shows up in the diff rather than silently.
            pytest.param(
                [{"id": "254"}, {"id": "255"}], 255, 0, 254, 0, 0, id="dual_external_left_keeps_the_legacy_encoding"
            ),
        ],
    )
    def test_the_slot_is_addressed_by_what_vt_tray_reports(
        self, mqtt_client, vt_tray, ams_id, tray_id, wire_ams_id, wire_tray_id, wire_slot_id
    ):
        mqtt_client.state.raw_data = {"vt_tray": vt_tray}

        assert mqtt_client.ams_set_filament_setting(ams_id=ams_id, tray_id=tray_id, **self.SETTING)

        cmd = _published_command(mqtt_client)
        assert cmd["command"] == "ams_filament_setting"
        assert cmd["ams_id"] == wire_ams_id
        assert cmd["tray_id"] == wire_tray_id
        assert cmd["slot_id"] == wire_slot_id

    def test_single_external_reset_uses_tray_id_254(self, mqtt_client):
        """A reset shares the convention, and clears the filament identity."""
        mqtt_client.state.raw_data = {"vt_tray": [{"id": "255"}]}

        assert mqtt_client.reset_ams_slot(ams_id=255, tray_id=0)

        cmd = _published_command(mqtt_client)
        assert cmd["ams_id"] == 255
        assert cmd["tray_id"] == 254
        assert cmd["slot_id"] == 0
        assert cmd["tray_info_idx"] == ""
        assert cmd["tray_type"] == ""

    def test_reset_publishes_the_blank_identity_field_for_field(self, mqtt_client):
        """A reset IS a filament setting — the BLANK one — so it publishes through
        ``ams_set_filament_setting`` rather than assembling a second copy of the id
        convention and the wire-safety refusal.

        Pinned WHOLE rather than by sample: the delegation is only safe while every
        field still matches, and a reset that quietly changes shape is a regression no
        partial assertion catches. ``setting_id`` must be ABSENT — the publisher omits
        the key for a blank value, exactly as the hand-rolled reset did by never
        including it.
        """
        mqtt_client.state.raw_data = {}

        assert mqtt_client.reset_ams_slot(ams_id=1, tray_id=2)

        assert _published_command(mqtt_client) == {
            "command": "ams_filament_setting",
            "ams_id": 1,
            "tray_id": 2,
            "slot_id": 2,
            "tray_info_idx": "",
            "tray_type": "",
            "tray_sub_brands": "",
            "tray_color": "00000000",
            "nozzle_temp_min": 0,
            "nozzle_temp_max": 0,
            "sequence_id": "0",
        }


class TestDryingCompleteCallback:
    """``on_drying_complete(ams_id)`` fires on a ``dry_time`` FALLING EDGE (#1349).

    The edge is per AMS id, and it must be a real edge: a push pair of 0 → 0 is a
    printer that was never drying (the seed-from-zero false positive at startup),
    and a tray-bearing partial that simply OMITS ``dry_time`` is silence, not zero.
    Reading that partial as zero fired "drying complete" seconds into a cycle, which
    armed the smart-plug auto-off and cut power to the printer mid-dry (#1462).
    """

    @pytest.fixture
    def drying_events(self):
        return []

    @pytest.fixture
    def mqtt_client(self, drying_events):
        return _make_client(serial="TEST-DRYING", on_drying_complete=drying_events.append)

    @staticmethod
    def _push(mqtt_client, *units):
        """Each unit is (ams_id, dry_time), or (ams_id, None) to omit the field."""
        mqtt_client._handle_ams_data(
            {
                "ams": [
                    {"id": str(ams_id), "tray": [], **({} if dry_time is None else {"dry_time": dry_time})}
                    for ams_id, dry_time in units
                ]
            }
        )

    def test_falling_edge_fires_callback(self, mqtt_client, drying_events):
        self._push(mqtt_client, (0, 60))
        assert drying_events == []

        self._push(mqtt_client, (0, 0))

        assert drying_events == [0]

    def test_no_fire_when_dry_time_never_started(self, mqtt_client, drying_events):
        self._push(mqtt_client, (0, 0))
        self._push(mqtt_client, (0, 0))

        assert drying_events == []

    def test_falling_edge_fires_once(self, mqtt_client, drying_events):
        self._push(mqtt_client, (0, 30))
        for _ in range(3):
            self._push(mqtt_client, (0, 0))

        assert drying_events == [0]

    def test_per_ams_tracking(self, mqtt_client, drying_events):
        """Two units drying finish independently."""
        self._push(mqtt_client, (0, 30), (1, 30))

        self._push(mqtt_client, (0, 0), (1, 15))
        assert drying_events == [0]

        self._push(mqtt_client, (0, 0), (1, 0))
        assert drying_events == [0, 1]

    def test_restart_drying_after_completion_refires_callback(self, mqtt_client, drying_events):
        """A second dry started from the UI has its own edge."""
        self._push(mqtt_client, (0, 30))
        self._push(mqtt_client, (0, 0))

        self._push(mqtt_client, (0, 45))
        self._push(mqtt_client, (0, 0))

        assert drying_events == [0, 0]

    def test_tray_only_partial_does_not_fake_completion(self, mqtt_client, drying_events):
        """The #1462 shape: a partial omitting ``dry_time`` must neither fire nor drop
        the field from the merged state."""
        self._push(mqtt_client, (0, 60))
        assert drying_events == []

        self._push(mqtt_client, (0, None))

        assert drying_events == []
        assert mqtt_client.state.raw_data["ams"][0]["dry_time"] == 60, "the partial must not drop it"

        self._push(mqtt_client, (0, 0))
        assert drying_events == [0], "and the real edge still fires exactly once"


class TestPrintRunningObservedCallback:
    """``on_print_running_observed`` is the restart-recovery twin of
    ``on_print_start`` (#1485).

    It fires the FIRST time a printer is seen RUNNING whose print began before the
    farm came up — the case where the #1304 guard deliberately suppresses
    ``on_print_start`` — so a consumer can still capture its timelapse baseline. It
    must never fire alongside ``on_print_start`` (that would capture twice), never
    more than once a session, and never without a file: the file is how the consumer
    finds the archive, so a RUNNING push carrying none is useless rather than urgent.
    """

    RUNNING = {
        "print": {
            "gcode_state": "RUNNING",
            "gcode_file": "/data/Metadata/test_print.gcode",
            "subtask_name": "Test_Print",
        }
    }

    @staticmethod
    def _recorders(mqtt_client):
        started, observed = [], []
        mqtt_client.on_print_start = started.append
        mqtt_client.on_print_running_observed = observed.append
        mqtt_client._was_running = False
        return started, observed

    def test_fires_on_first_running_push_after_startup(self, mqtt_client):
        started, observed = self._recorders(mqtt_client)
        mqtt_client._previous_gcode_state = None  # a freshly constructed client

        mqtt_client._process_message(self.RUNNING)

        assert started == [], "on_print_start must be suppressed by the #1304 guard"
        assert len(observed) == 1
        assert observed[0]["filename"] == "/data/Metadata/test_print.gcode"
        assert observed[0]["subtask_name"] == "Test_Print"

    def test_does_not_fire_when_print_start_fires(self, mqtt_client):
        started, observed = self._recorders(mqtt_client)
        mqtt_client._previous_gcode_state = "IDLE"  # a real transition, past the guard

        mqtt_client._process_message(self.RUNNING)

        assert len(started) == 1
        assert observed == [], "the two callbacks must not double up"

    def test_fires_only_once_per_session(self, mqtt_client):
        _, observed = self._recorders(mqtt_client)
        mqtt_client._previous_gcode_state = None

        for _ in range(3):
            mqtt_client._process_message(self.RUNNING)

        assert len(observed) == 1

    @pytest.mark.parametrize(
        "print_data",
        [
            pytest.param(
                {"gcode_state": "IDLE", "gcode_file": "", "subtask_name": ""}, id="not_running_nothing_to_baseline"
            ),
            pytest.param({"gcode_state": "RUNNING", "gcode_file": "", "subtask_name": ""}, id="running_without_a_file"),
        ],
    )
    def test_does_not_fire_without_a_print_to_recover(self, mqtt_client, print_data):
        _, observed = self._recorders(mqtt_client)
        mqtt_client._previous_gcode_state = None

        mqtt_client._process_message({"print": print_data})

        assert observed == []

    def test_safe_when_callback_not_set(self, mqtt_client):
        """No consumer wired → the firing branch must still not raise."""
        mqtt_client.on_print_running_observed = None
        mqtt_client._was_running = False
        mqtt_client._previous_gcode_state = None

        mqtt_client._process_message(self.RUNNING)

        assert mqtt_client._was_running is True

    def test_payload_shape_matches_print_start(self, mqtt_client):
        """The consumer reuses its ``on_print_start`` handler, so the keys must match
        exactly — an extra or missing key is a silent KeyError at recovery time."""
        _, observed = self._recorders(mqtt_client)
        mqtt_client._previous_gcode_state = None

        mqtt_client._process_message({"print": {**self.RUNNING["print"], "mc_remaining_time": 42}})

        assert set(observed[0]) == {"filename", "subtask_name", "remaining_time", "raw_data", "ams_mapping"}


class TestTotalLayersPreservation:
    """P1S firmware resets ``total_layer_num`` to 0 at print END (#1771).

    Read literally, the usage tracker's split path sees ``total_layers = 0`` at
    completion and dumps the whole print's filament onto the last spool. So a zero is
    not believed — and because it is not, a NEW print has to reset the value
    explicitly, or the previous print's total bleeds into it until its first real
    ``total_layer_num`` push arrives.
    """

    def test_nonzero_total_layer_num_sets_state(self, mqtt_client):
        mqtt_client._process_message({"print": {"total_layer_num": 260}})

        assert mqtt_client.state.total_layers == 260

    def test_zero_total_layer_num_does_not_clobber_cached_value(self, mqtt_client):
        mqtt_client._process_message({"print": {"total_layer_num": 260}})

        mqtt_client._process_message({"print": {"total_layer_num": 0}})

        assert mqtt_client.state.total_layers == 260

    def test_print_start_explicitly_resets_total_layers(self, mqtt_client):
        mqtt_client._process_message({"print": {"total_layer_num": 260}})
        assert mqtt_client.state.total_layers == 260

        # The is_new_print shape: RUNNING on a DIFFERENT file than before.
        mqtt_client._previous_gcode_state = "RUNNING"
        mqtt_client._previous_gcode_file = "/data/Metadata/old_print.gcode"
        mqtt_client._was_running = True
        mqtt_client._process_message(
            {
                "print": {
                    "gcode_state": "RUNNING",
                    "gcode_file": "/data/Metadata/new_print.gcode",
                    "subtask_name": "new_print",
                }
            }
        )

        assert mqtt_client.state.total_layers == 0


class TestAmsFilamentBackupHoldTimer:
    """A toggle the farm just sent outranks the printer's cfg for a 3 s hold.

    The optimistic state is set at publish time, and push_status keeps reporting the
    OLD cfg for a moment — believing it flickers the badge ON→OFF→ON. After the hold
    the printer's cfg is authoritative again, so a toggle made at the display or by
    the slicer still propagates. Same race guard the xcam settings use.
    """

    client_kwargs = {"connected": True}

    CFG_BIT18_CLEAR = "C0340BC219"
    CFG_BIT18_SET = "C0340FC219"

    def test_cfg_push_with_old_value_is_ignored_during_hold(self, mqtt_client):
        mqtt_client.set_ams_filament_backup(True)
        assert mqtt_client.state.ams_filament_backup is True

        mqtt_client._process_message({"print": {"cfg": self.CFG_BIT18_CLEAR}})

        assert mqtt_client.state.ams_filament_backup is True

    def test_cfg_push_after_hold_expires_overrides_state(self, mqtt_client):
        mqtt_client.set_ams_filament_backup(True)
        mqtt_client._xcam_hold_start["print_option_auto_switch_filament"] = time.time() - 10.0

        mqtt_client._process_message({"print": {"cfg": self.CFG_BIT18_CLEAR}})

        assert mqtt_client.state.ams_filament_backup is False

    def test_cfg_push_with_matching_value_during_hold_is_a_noop(self, mqtt_client):
        """A same-value push must not even re-arm the hold."""
        mqtt_client.set_ams_filament_backup(True)
        armed_at = mqtt_client._xcam_hold_start["print_option_auto_switch_filament"]

        mqtt_client._process_message({"print": {"cfg": self.CFG_BIT18_SET}})

        assert mqtt_client.state.ams_filament_backup is True
        assert mqtt_client._xcam_hold_start["print_option_auto_switch_filament"] == armed_at


class TestTrayNowH2SExternalSpoolOverride:
    """H2S firmware reports ``tray_now`` as the AMS's idle slot (usually 0) when the
    ACTIVE feed is the external spool (#1822).

    The only evidence that the print is external is the slicer-captured
    ``ams_mapping``, so the promotion to 254 fires exactly when every entry is -1 and
    the reported slot is one the firmware could be misreporting (0-3). A mixed or
    AMS-only mapping, an absent one (a screen-started print), or the unloaded
    sentinel all leave the firmware's value alone — an override without evidence
    would highlight the wrong feed on real AMS prints.
    """

    client_kwargs = {"serial": "TEST_H2S"}

    @pytest.mark.parametrize(
        "captured_mapping, reported, tray_now",
        [
            pytest.param([-1], 0, 254, id="all_external_single_filament_promotes"),
            pytest.param([-1, -1, -1], 0, 254, id="all_external_multi_filament_promotes"),
            pytest.param([5], 0, 0, id="ams_only_mapping_is_trusted_as_is"),
            # No evidence the firmware misreports a mid-print swap, so leave it.
            pytest.param([5, -1], 0, 0, id="mixed_mapping_is_trusted_as_is"),
            pytest.param(None, 0, 0, id="a_screen_started_print_has_no_mapping_to_read"),
            # all([]) is True, so an empty list must be excluded explicitly.
            pytest.param([], 0, 0, id="an_empty_mapping_is_no_signal_not_all_external"),
            pytest.param([-1], 255, 255, id="the_unloaded_sentinel_is_never_promoted"),
        ],
    )
    def test_only_an_all_external_mapping_promotes_the_reported_slot(
        self, mqtt_client, captured_mapping, reported, tray_now
    ):
        mqtt_client._captured_ams_mapping = captured_mapping

        mqtt_client._process_message(_ams_payload(reported))

        assert mqtt_client.state.tray_now == tray_now


class TestOperatorCancelEcho:
    """Cancel-echo capture (Phase 3.1) + native plate-occupancy retention (3.3).

    The firmware emits HMS cancel echoes (0300_400C / 0500_400E) during a normal
    user cancel. They stay OUT of ``state.hms_errors`` (they're not faults) but now
    ALSO stamp ``state.user_cancel_seen_at`` so the terminal handler can tell a
    screen-stop from a genuine failure. The native pre-print vision codes
    (0300_8017 / 0300_8006) are the opposite: real faults that MUST remain in
    hms_errors.
    """

    def test_hms_cancel_echo_sets_flag_and_stays_out_of_errors(self, mqtt_client):
        # 0300_400C: attr>>16 == 0x0300, code&0xFFFF == 0x400C.
        assert mqtt_client.state.user_cancel_seen_at is None
        mqtt_client._process_message({"print": {"hms": [{"attr": 0x03000000, "code": 0x400C}]}})
        assert mqtt_client.state.user_cancel_seen_at is not None
        # The echo is not a fault — it must not surface in hms_errors.
        codes = [f"{(e.attr >> 16) & 0xFFFF:04X}_{int(e.code, 16) & 0xFFFF:04X}" for e in mqtt_client.state.hms_errors]
        assert "0300_400C" not in codes

    def test_print_error_cancel_echo_sets_flag(self, mqtt_client):
        # print_error path carries 0500_400E as a single 32-bit int.
        mqtt_client._process_message({"print": {"print_error": 0x0500400E}})
        assert mqtt_client.state.user_cancel_seen_at is not None
        codes = [f"{(e.attr >> 16) & 0xFFFF:04X}_{int(e.code, 16) & 0xFFFF:04X}" for e in mqtt_client.state.hms_errors]
        assert "0500_400E" not in codes

    def test_plate_occupancy_codes_stay_in_hms_errors(self, mqtt_client):
        # 0300_8017 (foreign objects on heatbed) is an ACTIONABLE fault — kept.
        mqtt_client._process_message({"print": {"hms": [{"attr": 0x03000000, "code": 0x8017}]}})
        codes = [f"{(e.attr >> 16) & 0xFFFF:04X}_{int(e.code, 16) & 0xFFFF:04X}" for e in mqtt_client.state.hms_errors]
        assert "0300_8017" in codes
        # A native vision fault is NOT an operator cancel.
        assert mqtt_client.state.user_cancel_seen_at is None

    def test_flag_reset_on_new_print(self, mqtt_client):
        mqtt_client.state.user_cancel_seen_at = 12345.0
        mqtt_client._previous_gcode_state = "IDLE"
        mqtt_client.on_print_start = lambda data: None
        mqtt_client._process_message(
            {"print": {"gcode_state": "RUNNING", "gcode_file": "/data/Metadata/x.gcode", "subtask_name": "X"}}
        )
        # is_new_print reset clears the previous print's cancel observation.
        assert mqtt_client.state.user_cancel_seen_at is None

    def test_user_cancel_observed_surfaces_in_completion_payload(self, mqtt_client):
        complete_data = {}
        mqtt_client.on_print_start = lambda data: None
        mqtt_client.on_print_complete = lambda data: complete_data.update(data)
        mqtt_client._previous_gcode_state = "IDLE"

        # 1. Print starts.
        mqtt_client._process_message(
            {"print": {"gcode_state": "RUNNING", "gcode_file": "/data/Metadata/x.gcode", "subtask_name": "X"}}
        )
        # 2. Operator cancels on the screen → cancel echo arrives (state still RUNNING).
        mqtt_client._process_message({"print": {"hms": [{"attr": 0x03000000, "code": 0x400C}]}})
        # 3. Print goes FAILED (firmware reports failed/aborted for a cancel).
        mqtt_client._process_message(
            {"print": {"gcode_state": "FAILED", "gcode_file": "/data/Metadata/x.gcode", "subtask_name": "X"}}
        )
        assert complete_data.get("user_cancel_observed") is True

    def test_completion_without_cancel_has_false_flag(self, mqtt_client):
        complete_data = {}
        mqtt_client.on_print_start = lambda data: None
        mqtt_client.on_print_complete = lambda data: complete_data.update(data)
        mqtt_client._previous_gcode_state = "IDLE"
        mqtt_client._process_message(
            {"print": {"gcode_state": "RUNNING", "gcode_file": "/data/Metadata/x.gcode", "subtask_name": "X"}}
        )
        mqtt_client._process_message(
            {"print": {"gcode_state": "FINISH", "gcode_file": "/data/Metadata/x.gcode", "subtask_name": "X"}}
        )
        assert complete_data.get("user_cancel_observed") is False


class TestAmsChangeHashPresence:
    """The AMS change-hash carries a presence bit (state ∈ {10,11} → 'p',
    else 'a') off the MERGED tray state, so a tagless third-party spool physically
    inserted/removed fires on_ams_change even though no tray_type/tag/remain
    changed — while a mid-print 10↔11 tool-change flip does NOT storm the callback.
    """

    @staticmethod
    def _feed(mqtt_client, state, *, tray_type="", tag="0000000000000000", remain=0):
        mqtt_client._handle_ams_data(
            {
                "ams": [
                    {
                        "id": 0,
                        "tray": [{"id": 0, "state": state, "tray_type": tray_type, "tag_uid": tag, "remain": remain}],
                    }
                ]
            }
        )

    def test_presence_gain_9_to_11_fires(self, mqtt_client):
        mqtt_client.on_ams_change = Mock()
        self._feed(mqtt_client, 9)  # empty (absent)
        mqtt_client.on_ams_change.reset_mock()
        self._feed(mqtt_client, 11)  # tagless spool inserted — only presence flips a→p
        assert mqtt_client.on_ams_change.call_count == 1

    def test_presence_loss_11_to_9_fires(self, mqtt_client):
        mqtt_client.on_ams_change = Mock()
        self._feed(mqtt_client, 11)
        mqtt_client.on_ams_change.reset_mock()
        self._feed(mqtt_client, 9)  # spool removed — presence flips p→a
        assert mqtt_client.on_ams_change.call_count == 1

    def test_tool_change_10_to_11_does_not_fire(self, mqtt_client):
        # A loaded (tagged) spool flips 10↔11 on every load/unload mid-print; its
        # identity fields are constant so the presence bit ('p') never changes.
        mqtt_client.on_ams_change = Mock()
        self._feed(mqtt_client, 10, tray_type="PETG", tag="1234567890ABCDEF", remain=50)
        mqtt_client.on_ams_change.reset_mock()
        self._feed(mqtt_client, 11, tray_type="PETG", tag="1234567890ABCDEF", remain=50)
        assert mqtt_client.on_ams_change.call_count == 0

    def test_partial_update_omitting_state_does_not_flap(self, mqtt_client):
        # Merged basis: a partial update that omits `state` (and other fields)
        # must NOT flap the presence token — the merged tray retains state=11. If
        # the hash iterated the raw partial, the missing state → 'a' would fire.
        mqtt_client.on_ams_change = Mock()
        mqtt_client._handle_ams_data(
            {
                "ams": [
                    {
                        "id": 0,
                        "tray": [
                            {"id": 0, "state": 11, "tray_type": "PETG", "tag_uid": "1234567890ABCDEF", "remain": 80}
                        ],
                    }
                ]
            }
        )
        mqtt_client.on_ams_change.reset_mock()
        # Partial: only remain re-stated (unchanged), no `state`, no tray_type, no tag.
        mqtt_client._handle_ams_data({"ams": [{"id": 0, "tray": [{"id": 0, "remain": 80}]}]})
        assert mqtt_client.on_ams_change.call_count == 0


_AMS_WRITE_ARGS = {
    "ams_get_rfid": lambda c, ams, slot: c.ams_refresh_tray(ams, slot)[0],
    "ams_filament_setting": lambda c, ams, slot: c.ams_set_filament_setting(
        ams, slot, "GFL05", "PLA", "PLA Basic", "FFFF00FF", 190, 230
    ),
    "reset_ams_slot": lambda c, ams, slot: c.reset_ams_slot(ams, slot),
    "extrusion_cali_sel": lambda c, ams, slot: c.extrusion_cali_sel(ams, slot, cali_idx=-1, filament_id="GFL05"),
}
"""THE four AMS write choke points, by the op name their refusal logs (invariant 2:
wire safety belongs to the client, and every write re-evaluates it at publish time)."""

_AMS_WRITE_OPS = list(_AMS_WRITE_ARGS)


def _arm_ams_hazard(client, reason):
    """Put the client into one of the three states that refuse an AMS write."""
    if reason == "drying":
        client.state.raw_data["ams"] = [{"id": 0, "dry_time": 30, "tray": [{"id": 0, "state": 10}]}]
    elif reason == "identifying":
        client.state.ams_status_main = 2
    elif reason == "identify_in_flight":
        client._identify_gate_until = time.monotonic() + 30
    else:  # pragma: no cover - a typo in a parametrize id would otherwise pass silently
        raise AssertionError(f"unknown hazard {reason!r}")


class TestAmsWriteRefusalHelper:
    """The wire-safety MATRIX: every AMS write choke point refuses for every reason.

    All four writes share one evaluator (``_ams_write_refusal``) behind one
    evaluate-and-log helper, so the parity is the contract — a write that grew its own
    opinion is the bug this pins. The three hazards:

    * **drying** — poking a drying tray raises HMS 0700_C069, and drying is detected
      from per-unit ``dry_time`` because ``ams_status_main`` has no drying value;
    * **identifying** — the AMS is mid RFID read (``ams_status_main == 2``), and a
      concurrent write fails that read (0700_2x00_0001_0081);
    * **identify_in_flight** — WE published an ``ams_get_rfid`` and the per-printer
      gate is still armed; a config write to any slot would clobber the answer.

    The evaluator's ORDER (drying → identifying → gate) is itself the contract: the
    callers report the reason it returns.
    """

    client_kwargs = {"serial": "REFUSE1", "connected": True, "tray_now": 255}

    @pytest.mark.parametrize("op", _AMS_WRITE_OPS)
    @pytest.mark.parametrize("reason", ["drying", "identifying", "identify_in_flight"])
    def test_every_choke_point_refuses_every_reason(self, mqtt_client, caplog, reason, op):
        _arm_ams_hazard(mqtt_client, reason)

        with caplog.at_level(logging.WARNING):
            assert _AMS_WRITE_ARGS[op](mqtt_client, 0, 0) is False

        mqtt_client._client.publish.assert_not_called()
        # One standardized WARNING, naming the op and the reason.
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert f"Refusing {op} on AMS 0" in warnings[0]
        assert _AMS_REFUSAL_LOG_TEXT[reason] in warnings[0]

    @pytest.mark.parametrize("op", _AMS_WRITE_OPS)
    def test_all_proceed_when_the_wire_is_safe(self, mqtt_client, op):
        assert _AMS_WRITE_ARGS[op](mqtt_client, 0, 0) is True

        mqtt_client._client.publish.assert_called_once()

    def test_refusal_order_is_drying_then_identifying_then_gate(self, mqtt_client):
        """All three hazards at once → drying wins."""
        for reason in ("drying", "identifying", "identify_in_flight"):
            _arm_ams_hazard(mqtt_client, reason)

        assert mqtt_client.ams_write_refusal(0) == "drying"
        assert mqtt_client._refuse_ams_write("ams_get_rfid", 0) == "drying"

    @pytest.mark.parametrize("reason", list(_AMS_REFRESH_REFUSAL_MESSAGE))
    def test_refresh_tray_messages_are_reason_specific(self, reason):
        """The manual-refresh route 400s with this text, so the operator is told WHICH
        hazard — one reason per fresh client, or the order rule masks the others."""
        client = _make_client(serial="REFUSE1", connected=True, tray_now=255)
        _arm_ams_hazard(client, reason)

        ok, msg = client.ams_refresh_tray(0, 0)

        assert ok is False
        assert msg == _AMS_REFRESH_REFUSAL_MESSAGE[reason]

    def test_public_accessor_is_read_only(self, mqtt_client, caplog):
        """``ams_write_refusal`` is the advisory form callers may poll: it neither logs
        nor publishes, so a UI asking every second cannot flood the log."""
        assert mqtt_client.ams_write_refusal(0) is None
        _arm_ams_hazard(mqtt_client, "identifying")

        with caplog.at_level(logging.WARNING):
            assert mqtt_client.ams_write_refusal(0) == "identifying"

        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []
        mqtt_client._client.publish.assert_not_called()


class TestAmsDryingGuards:
    """How DRYING is detected and latched — what it refuses is pinned by the matrix
    above.

    ``ams_status_main`` has no drying value, so the per-unit ``dry_time`` is the only
    wire evidence. A cycle started at the touchscreen has no ``dry_time`` yet either,
    so the command echo latches it for the duration plus slack, and the latch also
    seeds the badge fields the UI shows.
    """

    client_kwargs = {"serial": "DRY1", "connected": True, "tray_now": 255}

    @staticmethod
    def _set_dry_time(mqtt_client, ams_id, minutes):
        mqtt_client.state.raw_data["ams"] = [{"id": ams_id, "dry_time": minutes, "tray": [{"id": 0, "state": 10}]}]

    def test_unit_drying_true_from_merged_dry_time(self, mqtt_client):
        self._set_dry_time(mqtt_client, 0, 45)

        assert mqtt_client.ams_unit_drying(0) is True
        assert mqtt_client.ams_unit_drying(1) is False, "the hazard is per unit"

    def test_unit_drying_false_when_dry_time_zero(self, mqtt_client):
        self._set_dry_time(mqtt_client, 0, 0)

        assert mqtt_client.ams_unit_drying(0) is False

    @pytest.mark.parametrize(
        "raw_data", [pytest.param({}, id="no_ams_key"), pytest.param({"ams": "garbage"}, id="ams_not_a_list")]
    )
    def test_unit_drying_handles_missing_or_malformed_raw_data(self, mqtt_client, raw_data):
        """Unknown is not drying — a malformed push must not lock every write out."""
        mqtt_client.state.raw_data = raw_data

        assert mqtt_client.ams_unit_drying(0) is False

    def test_mode1_echo_latches_and_seeds_badge(self, mqtt_client):
        """A touchscreen-started cycle is only visible through its echo."""
        mqtt_client._handle_drying_echo(
            {
                "command": "ams_filament_drying",
                "result": "success",
                "ams_id": 0,
                "mode": 1,
                "filament": "PETG",
                "temp": 65,
                "duration": 8,
            }
        )

        assert mqtt_client.ams_unit_drying(0) is True
        entry = mqtt_client._drying_targets[0]
        assert entry["filament"] == "PETG"
        assert entry["temp"] == 65
        assert isinstance(entry["latched_until"], float)

    def test_mode0_echo_clears_latch(self, mqtt_client):
        mqtt_client._handle_drying_echo(
            {"result": "success", "ams_id": 0, "mode": 1, "filament": "PLA", "temp": 55, "duration": 4}
        )
        assert 0 in mqtt_client._drying_targets

        mqtt_client._handle_drying_echo({"result": "success", "ams_id": 0, "mode": 0})

        assert 0 not in mqtt_client._drying_targets
        assert mqtt_client.ams_unit_drying(0) is False

    @pytest.mark.parametrize(
        "echo",
        [
            pytest.param({"result": "success", "mode": 1, "filament": "PLA", "temp": 55}, id="no_ams_id_to_latch"),
            pytest.param(
                {"result": "fail", "ams_id": 0, "mode": 1, "temp": 55, "duration": 4}, id="result_not_success"
            ),
        ],
    )
    def test_an_unusable_echo_latches_nothing(self, mqtt_client, echo):
        mqtt_client._handle_drying_echo(echo)

        assert mqtt_client._drying_targets == {}
        assert mqtt_client.ams_unit_drying(0) is False

    def test_falling_edge_pops_latched_target(self, mqtt_client):
        """Once real ``dry_time`` has been seen, its falling edge retires the latch."""
        mqtt_client._handle_drying_echo(
            {"result": "success", "ams_id": 0, "mode": 1, "filament": "PLA", "temp": 55, "duration": 4}
        )

        mqtt_client._handle_ams_data({"ams": [{"id": 0, "dry_time": 5, "tray": [{"id": 0, "state": 10}]}]})
        mqtt_client._handle_ams_data({"ams": [{"id": 0, "dry_time": 0, "tray": [{"id": 0, "state": 10}]}]})

        assert 0 not in mqtt_client._drying_targets

    def test_latch_expiry_via_monotonic(self, mqtt_client, monkeypatch):
        """The latch is bounded: duration + 30 min slack, then it stops claiming a
        hazard it can no longer see — a printer that never reports ``dry_time`` must
        not be locked out forever."""
        now = [1000.0]
        monkeypatch.setattr(mqtt_mod.time, "monotonic", lambda: now[0])
        mqtt_client._handle_drying_echo(
            {"result": "success", "ams_id": 0, "mode": 1, "filament": "PLA", "temp": 55, "duration": 1}
        )
        assert mqtt_client.ams_unit_drying(0) is True  # latched to 1000 + 3600 + 1800

        now[0] = 6399.0
        assert mqtt_client.ams_unit_drying(0) is True

        now[0] = 6401.0
        assert mqtt_client.ams_unit_drying(0) is False
        assert 0 not in mqtt_client._drying_targets

    @pytest.mark.parametrize("op", _AMS_WRITE_OPS)
    def test_other_unit_unaffected_by_drying(self, mqtt_client, op):
        """The lockout is unit-scoped: unit 0 drying must not stand unit 1 down."""
        self._set_dry_time(mqtt_client, 0, 30)

        assert _AMS_WRITE_ARGS[op](mqtt_client, 1, 0) is True

    def test_drying_echo_wired_through_process_message(self, mqtt_client):
        """The echo arrives as an ordinary command response on the report topic."""
        mqtt_client._process_message(
            {
                "print": {
                    "command": "ams_filament_drying",
                    "result": "success",
                    "ams_id": 2,
                    "mode": 1,
                    "filament": "PETG",
                    "temp": 65,
                    "duration": 6,
                }
            }
        )

        assert mqtt_client.ams_unit_drying(2) is True


class TestIdentifyGate:
    """The per-printer identify gate's LIFECYCLE: armed by publishing an
    ``ams_get_rfid``, released by the AMS leaving the identifying state, and expiring
    on its own after ``_IDENTIFY_GATE_S`` (30 s).

    A read the farm commanded is the one thing that cannot be re-asked for free: a
    second overlapping identify fails the in-flight one. The expiry is the liveness
    half — a printer that never reports the identifying state must not gate its own
    AMS forever.
    """

    client_kwargs = {"serial": "GATE1", "connected": True, "tray_now": 255}

    @staticmethod
    def _release_by_ams_going_idle(mqtt_client):
        mqtt_client.state.ams_status_main = 2  # the AMS entered identifying
        mqtt_client._handle_ams_data({"ams_status": 0})  # 2 → 0 releases the gate

    def test_second_refresh_inside_gate_refused(self, mqtt_client):
        """Including a DIFFERENT slot — the gate is per printer, not per tray."""
        assert mqtt_client.ams_refresh_tray(0, 0)[0] is True
        assert mqtt_client._identify_gate_until > 0

        ok, msg = mqtt_client.ams_refresh_tray(0, 1)

        assert ok is False
        assert "identifying another tray" in msg.lower()
        assert mqtt_client._client.publish.call_count == 1, "only the first identify went out"

    @pytest.mark.parametrize("op", _AMS_WRITE_OPS)
    def test_a_real_identify_gates_every_other_write(self, mqtt_client, op):
        assert mqtt_client.ams_refresh_tray(0, 0)[0] is True, "arms the gate"

        assert _AMS_WRITE_ARGS[op](mqtt_client, 0, 1) is False

        assert mqtt_client._client.publish.call_count == 1

    @pytest.mark.parametrize("op", _AMS_WRITE_OPS)
    def test_the_release_frees_every_write(self, mqtt_client, op):
        mqtt_client.ams_refresh_tray(0, 0)

        self._release_by_ams_going_idle(mqtt_client)

        assert mqtt_client._identify_gate_until == 0.0
        assert _AMS_WRITE_ARGS[op](mqtt_client, 0, 1) is True

    @pytest.mark.parametrize("op", _AMS_WRITE_OPS)
    def test_the_expiry_frees_every_write(self, mqtt_client, monkeypatch, op):
        now = [1000.0]
        monkeypatch.setattr(mqtt_mod.time, "monotonic", lambda: now[0])
        assert mqtt_client.ams_refresh_tray(0, 0)[0] is True, "arms the gate to 1030"

        now[0] = 1029.0
        assert _AMS_WRITE_ARGS[op](mqtt_client, 0, 1) is False

        now[0] = 1031.0
        assert _AMS_WRITE_ARGS[op](mqtt_client, 0, 1) is True


class TestAmsChangeFilamentGuards:
    """``ams_change_filament`` (load and unload) rides the same authority as the
    other AMS writes, with two things it must NEVER do.

    It must not refuse while a feed-fault HMS is standing — that is precisely the
    state jam recovery has to unload and reload in (invariant 8: the unload is
    unconditional before a load), and refusing there deadlocks the recovery. And it
    must not arm the identify gate: motion is not an identify.

    The unload's hazard is scoped to the unit currently FEEDING, so with nothing
    feeding (``tray_now`` 255 → unit 255, which has no row) there is no unit that
    could be drying and the recovery unload goes out.
    """

    client_kwargs = {"serial": "SWAP1", "connected": True, "tray_now": 0}

    @staticmethod
    def _dry(mqtt_client, ams_id):
        mqtt_client.state.raw_data["ams"] = [{"id": ams_id, "dry_time": 30, "tray": [{"id": 0, "state": 10}]}]

    @pytest.mark.parametrize("reason", ["drying", "identifying", "identify_in_flight"])
    def test_a_load_is_refused_for_every_reason(self, mqtt_client, caplog, reason):
        if reason == "identify_in_flight":
            mqtt_client.state.tray_now = 255  # ams_refresh_tray's own "nothing loaded" check
            assert mqtt_client.ams_refresh_tray(0, 0)[0] is True, "arms the gate for real"
        else:
            _arm_ams_hazard(mqtt_client, reason)
        published_before = mqtt_client._client.publish.call_count

        with caplog.at_level(logging.WARNING, logger="backend.app.services.bambu_mqtt"):
            assert mqtt_client.ams_load_filament(0) is False

        assert mqtt_client._client.publish.call_count == published_before
        if reason != "identify_in_flight":
            warned = "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
            assert "ams_change_filament (load)" in warned

    def test_unload_refused_while_the_source_unit_dries(self, mqtt_client, caplog):
        mqtt_client.state.tray_now = 4  # AMS 1 slot 0 is feeding
        self._dry(mqtt_client, 1)

        with caplog.at_level(logging.WARNING, logger="backend.app.services.bambu_mqtt"):
            assert mqtt_client.ams_unload_filament() is False

        mqtt_client._client.publish.assert_not_called()
        warned = "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
        assert "ams_change_filament (unload)" in warned

    def test_unload_refused_while_identifying(self, mqtt_client):
        _arm_ams_hazard(mqtt_client, "identifying")

        assert mqtt_client.ams_unload_filament() is False

        mqtt_client._client.publish.assert_not_called()

    def test_unload_with_nothing_feeding_has_no_unit_to_be_drying(self, mqtt_client):
        mqtt_client.state.tray_now = 255
        self._dry(mqtt_client, 0)

        assert mqtt_client.ams_unload_filament() is True

        mqtt_client._client.publish.assert_called_once()

    def test_load_and_unload_still_sent_under_a_standing_feed_fault(self, mqtt_client):
        """THE NO-DEADLOCK PIN: a live jam HMS must never gate the swap commands."""
        mqtt_client.state.hms_errors = [
            HMSError(code="8010", attr=0x07008210, module=7, severity=2),  # 0700_8010 tangle
            HMSError(code="8011", attr=0x07000000, module=7, severity=2),  # 0700_8011 runout
        ]
        mqtt_client.state.tray_now = 255  # nothing feeding — the live incident's telemetry

        assert mqtt_client.ams_unload_filament() is True
        assert mqtt_client.ams_load_filament(2) is True

        assert mqtt_client._client.publish.call_count == 2

    def test_neither_arms_the_identify_gate(self, mqtt_client):
        assert mqtt_client.ams_unload_filament() is True
        assert mqtt_client.ams_load_filament(1) is True

        assert mqtt_client._identify_gate_until == 0.0, "motion is not an identify"
        mqtt_client.state.tray_now = 255
        assert mqtt_client.ams_refresh_tray(0, 0)[0] is True, "so a later identify is free"

    def test_load_does_not_burn_a_sequence_id_when_refused(self, mqtt_client):
        """A refused command must cost nothing — a burnt sequence id desynchronises
        the response matching for every later command."""
        _arm_ams_hazard(mqtt_client, "identifying")
        before = mqtt_client._sequence_id

        assert mqtt_client.ams_load_filament(0) is False

        assert mqtt_client._sequence_id == before


class TestWaitAmsSettle:
    """``wait_ams_settle`` is how the terminal RFID sweep avoids overlapping reads:
    it blocks until the AMS is not identifying AND the gate has cleared, capped at
    ``_IDENTIFY_GATE_S`` so a printer that never settles cannot hang the sweep."""

    client_kwargs = {"serial": "SETTLE1", "connected": True}

    async def test_immediate_true_when_idle_and_gate_clear(self, mqtt_client, monkeypatch):
        sleep = AsyncMock()
        monkeypatch.setattr(mqtt_mod.asyncio, "sleep", sleep)
        mqtt_client.state.ams_status_main = 0
        mqtt_client._identify_gate_until = 0.0

        assert await mqtt_client.wait_ams_settle() is True

        sleep.assert_not_awaited(), "returned on entry, never polled"

    async def test_waits_while_identifying_then_true_on_settle(self, mqtt_client, monkeypatch):
        mqtt_client.state.ams_status_main = 2
        mqtt_client._identify_gate_until = 0.0
        polls = [0]

        async def fake_sleep(_):
            polls[0] += 1
            if polls[0] >= 2:
                mqtt_client.state.ams_status_main = 0  # the AMS settles after two polls

        monkeypatch.setattr(mqtt_mod.asyncio, "sleep", fake_sleep)

        assert await mqtt_client.wait_ams_settle() is True
        assert polls[0] == 2

    async def test_false_at_identify_gate_cap(self, mqtt_client, monkeypatch):
        now = [1000.0]
        monkeypatch.setattr(mqtt_mod.time, "monotonic", lambda: now[0])
        mqtt_client.state.ams_status_main = 2  # never settles

        async def fake_sleep(_):
            now[0] += 10.0

        monkeypatch.setattr(mqtt_mod.asyncio, "sleep", fake_sleep)

        # The deadline is 1000 + _IDENTIFY_GATE_S (30); three 10 s polls reach it.
        assert await mqtt_client.wait_ams_settle() is False


class TestTrayClearPresenceConsistency:
    """The stale-clear in ``_handle_ams_data`` keys off ``TRAY_PRESENT_STATES``.

    A partial ``{id, state}`` push may wipe stale identity ONLY when the spool is
    genuinely absent or unknown. 10 and 11 are presence and PRESERVE it — wiping a
    state-10 tray is what drove the AMS-drying incident (drying disengages trays to
    10, HMS 0700_C069) and it wipes routine load/unload transit too.
    """

    client_kwargs = {"serial": "TRAY1", "connected": True}

    @staticmethod
    def _seed_loaded(mqtt_client):
        mqtt_client._handle_ams_data(
            {
                "ams": [
                    {
                        "id": 0,
                        "tray": [{"id": 0, "state": 11, "tray_type": "PETG", "tray_color": "008000FF", "remain": 80}],
                    }
                ]
            }
        )

    @staticmethod
    def _tray_type(mqtt_client):
        return mqtt_client.state.raw_data["ams"][0]["tray"][0].get("tray_type")

    @pytest.mark.parametrize(
        "state, tray_type",
        [
            pytest.param(11, "PETG", id="state11_loaded_preserves"),
            pytest.param(10, "PETG", id="state10_present_not_fed_preserves"),
            pytest.param(9, "", id="state9_empty_clears"),
            # 0 is the H2C long-idle "AMS detail not reported" dialect.
            pytest.param(0, "", id="state0_dialect_clears"),
            pytest.param(8, "", id="state8_clears"),
        ],
    )
    def test_only_a_non_presence_state_wipes_stale_identity(self, mqtt_client, state, tray_type):
        self._seed_loaded(mqtt_client)

        mqtt_client._handle_ams_data({"ams": [{"id": 0, "tray": [{"id": 0, "state": state}]}]})

        assert self._tray_type(mqtt_client) == tray_type

    def test_full_push_empty_tray_type_still_clears(self, mqtt_client):
        """An EXPLICIT empty ``tray_type`` is the printer stating the slot is bare, so
        it clears whatever the state says."""
        self._seed_loaded(mqtt_client)

        mqtt_client._handle_ams_data({"ams": [{"id": 0, "tray": [{"id": 0, "state": 11, "tray_type": ""}]}]})

        assert self._tray_type(mqtt_client) == ""

    def test_state_10_partial_preserves_identity_during_drying(self, mqtt_client):
        """The incident itself: a drying unit disengages its trays to state 10."""
        self._seed_loaded(mqtt_client)
        mqtt_client.state.raw_data["ams"][0]["dry_time"] = 60

        mqtt_client._handle_ams_data({"ams": [{"id": 0, "tray": [{"id": 0, "state": 10}]}]})

        assert self._tray_type(mqtt_client) == "PETG"


class TestTrayExistBitsStatePromotionMerge:
    """The same promotion at the merge level, against the CACHED mask (003-H2S).

    A full tagless spool inserted mid-print gets no auto-read, so its tray sits at
    state 9 while ``tray_exist_bits`` already marks the slot occupied. The merge must
    promote it 9→10 — otherwise the presence/identify/auto-config pipeline, which
    keys on state ∈ {10, 11}, never sees it — and the stale-clear must not wipe a slot
    the last-seen mask still marks occupied. Where no mask has ever been carried,
    there is nothing to protect the slot and the wipe stands.
    """

    client_kwargs = {"serial": "PROMO1", "connected": True}

    LOADED_SLOT_0 = {
        "ams": [
            {"id": 0, "tray": [{"id": 0, "state": 11, "tray_type": "PETG", "tray_color": "008000FF", "remain": 80}]}
        ]
    }
    STUCK_NINE_PARTIAL = {"ams": [{"id": 0, "tray": [{"id": 0, "state": 9}]}]}

    @staticmethod
    def _tray(mqtt_client, ams_idx=0, tray_idx=0):
        return mqtt_client.state.raw_data["ams"][ams_idx]["tray"][tray_idx]

    @pytest.mark.parametrize("state", [pytest.param(9, id="int_nine"), pytest.param("9", id="string_nine")])
    def test_a_set_bit_in_the_same_push_promotes_a_stuck_nine(self, mqtt_client, state):
        mqtt_client._handle_ams_data(
            {"ams": [{"id": 0, "tray": [{"id": 2, "state": state}]}], "tray_exist_bits": "4"}  # bit 2
        )

        tray = self._tray(mqtt_client)
        assert tray["state"] == 10
        assert isinstance(tray["state"], int)
        assert not tray.get("tray_type"), "a promotion never fabricates identity"

    def test_state9_partial_with_cached_bit_set_preserves_and_promotes(self, mqtt_client):
        """The H2D-style minimal partial carries no mask at all, so the CACHED bit
        both blocks the wipe and drives the promotion."""
        mqtt_client._handle_ams_data({**self.LOADED_SLOT_0, "tray_exist_bits": "1"})

        mqtt_client._handle_ams_data(self.STUCK_NINE_PARTIAL)

        tray = self._tray(mqtt_client)
        assert tray["tray_type"] == "PETG", "identity preserved"
        assert tray["state"] == 10, "and the stuck 9 promoted"

    def test_state9_partial_with_cached_bit_clear_still_wipes(self, mqtt_client):
        mqtt_client._handle_ams_data({**self.LOADED_SLOT_0, "tray_exist_bits": "1"})

        # Tray-less pushes carrying an all-zero mask return BEFORE the merge, so they
        # touch no slot data — but they do feed the trust streak, and only a matured
        # streak may replace the cache.
        for _ in range(_ZERO_EXIST_BITS_TRUST_PUSHES - 1):
            mqtt_client._handle_ams_data({"tray_exist_bits": "0"})
        assert mqtt_client._last_tray_exist_bits == 1, "an untrusted zero must never become the cache"

        mqtt_client._handle_ams_data({"tray_exist_bits": "0"})
        assert mqtt_client._last_tray_exist_bits == 0
        assert self._tray(mqtt_client)["tray_type"] == "PETG", "tray-less pushes touch nothing"

        mqtt_client._handle_ams_data(self.STUCK_NINE_PARTIAL)

        assert self._tray(mqtt_client)["tray_type"] == ""

    def test_state9_partial_no_cache_still_wipes(self, mqtt_client):
        """No push ever carried a mask, so nothing contradicts the tray's own claim."""
        mqtt_client._handle_ams_data(self.LOADED_SLOT_0)
        assert mqtt_client._last_tray_exist_bits is None

        mqtt_client._handle_ams_data(self.STUCK_NINE_PARTIAL)

        assert self._tray(mqtt_client)["tray_type"] == ""


# Real captured wire shapes — prod H2S MQTT capture, 2026-08-07 (raw6/raw7).
# Trimmed only by omission of unrelated pushes: the tray dicts themselves are
# verbatim. Two facts these pin that hand-written fixtures kept getting wrong:
# tray ids are STRINGS, and the payload reaching _handle_ams_data is a BARE LIST
# with NO tray_exist_bits anywhere (so the 003-H2S veto is inert on this fleet).
CAPTURED_LOADED_TRAY = {
    "bed_temp": "0",
    "bed_temp_type": "0",
    "cali_idx": -1,
    "cols": ["000000FF"],
    "ctype": 0,
    "drying_temp": "65",
    "drying_time": "8",
    "id": "0",
    "nozzle_temp_max": "260",
    "nozzle_temp_min": "230",
    "remain": 100,
    "state": 11,
    "tag_uid": "BC5F48E000000100",
    "total_len": 330000,
    "tray_color": "000000FF",
    "tray_diameter": "1.75",
    "tray_id_name": "G02-K0",
    "tray_info_idx": "GFG02",
    "tray_sub_brands": "PETG HF",
    "tray_type": "PETG",
    "tray_uuid": "4AA6142F7F724215A955139CDD759829",
    "tray_weight": "1000",
    "xcam_info": "000000000000000000000000",
}

CAPTURED_CLEARED_TRAY = {
    "bed_temp": "0",
    "bed_temp_type": "0",
    "cali_idx": -1,
    "cols": ["000000FF"],
    "ctype": 0,
    "drying_temp": "0",
    "drying_time": "0",
    "id": "1",
    "nozzle_temp_max": "270",
    "nozzle_temp_min": "230",
    "remain": 0,
    "state": 9,
    "tag_uid": "0000000000000000",
    "total_len": 330000,
    "tray_color": "",
    "tray_diameter": "1.75",
    "tray_id_name": "",
    "tray_info_idx": "",
    "tray_sub_brands": "",
    "tray_type": "",
    "tray_uuid": "00000000000000000000000000000000",
    "tray_weight": "0",
    "xcam_info": "000000000000000000000000",
}

# The boot-forgotten slot: {id, state} and NOTHING else, in every push including
# the 97-key pushall. Seen on four slots across the fleet, at ~1 Hz for the whole
# capture window.
CAPTURED_MINIMAL_TRAY = {"id": "2", "state": 9}

CLEAR_LOG_TEXT = "clearing stale tray data"


def _captured_tray(template, tray_id, **overrides):
    """Captured tray template under a different slot id (wire ids are strings)."""
    out = dict(template)
    out["id"] = str(tray_id)
    out.update(overrides)
    return out


class _RawCapture:
    """Stands in for the spool pipeline on ``on_ams_push_raw``.

    Consumes the hand-off the way production does — SYNCHRONOUSLY, building
    observations inline — and additionally deep-copies the payload so a test can
    assert on what the wire said at hand-off time, before the merge touches it.
    """

    def __init__(self, printer_id=1):
        self.printer_id = printer_id
        self.payloads = []
        self.pushes = []

    def __call__(self, payload):
        self.payloads.append(copy.deepcopy(payload))
        self.pushes.append(observe_ams_push(self.printer_id, payload))

    def last(self, ams_id, tray_id):
        """The last push's observation for one slot (ids as ints, post-parse)."""
        for obs in self.pushes[-1]:
            if obs.ams_id == ams_id and obs.tray_id == tray_id:
                return obs
        raise AssertionError(f"no observation for A{ams_id}-T{tray_id}")

    def last_wire_tray(self, ams_id, tray_id):
        """The last push's RAW tray dict for one slot, frozen at hand-off."""
        units = self.payloads[-1]
        units = units.get("ams") if isinstance(units, dict) else units
        for unit in units:
            if str(unit.get("id")) != str(ams_id):
                continue
            for tray in unit.get("tray", []):
                if str(tray.get("id")) == str(tray_id):
                    return tray
        raise AssertionError(f"no wire tray for A{ams_id}-T{tray_id}")


class TestClearedTrayNormalization:
    """Split-authority normalization of minimal state-9 partials (raw side).

    ``_normalize_cleared_trays`` injects the asserted-cleared shape BEFORE the raw
    pre-merge hand-off, so the spool pipeline sees the same clear the merge applies
    to the display copy. Without it a boot-forgotten tray asserts nothing forever,
    ``tray_presence`` answers None, and release-on-empty (doctrine rule 9) can never
    fire for that slot. The merge's own block keeps only its non-9 display duty.
    """

    client_kwargs = {"serial": "TEST_H2S"}

    @pytest.fixture
    def mqtt_client(self, mqtt_client):
        """``on_ams_push_raw`` is consumed the way production consumes it."""
        mqtt_client.on_ams_push_raw = _RawCapture()
        return mqtt_client

    @staticmethod
    def _push(mqtt_client, trays, ams_id="0", **extra):
        """One push in the captured shape: a bare unit list unless `extra` is given."""
        units = [{"id": ams_id, "tray": trays}]
        mqtt_client._handle_ams_data({"ams": units, **extra} if extra else units)

    @staticmethod
    def _merged(mqtt_client, ams_id, tray_id):
        for unit in mqtt_client.state.raw_data.get("ams", []):
            if str(unit.get("id")) != str(ams_id):
                continue
            for tray in unit.get("tray", []):
                if str(tray.get("id")) == str(tray_id):
                    return tray
        raise AssertionError(f"no merged tray for A{ams_id}-T{tray_id}")

    @staticmethod
    def _clear_logs(caplog):
        return [r for r in caplog.records if CLEAR_LOG_TEXT in r.getMessage()]

    def test_steady_state_arm_injects_silently_on_every_push(self, mqtt_client, caplog):
        """Merged copy already cleared -> inject on EVERY push, no log.

        Recurrence is the point: a release deferred once (drying / identify /
        settling / restart) must retry on the next partial instead of never.
        """
        self._push(mqtt_client, [_captured_tray(CAPTURED_CLEARED_TRAY, 2)])
        assert self._merged(mqtt_client, 0, 2)["tray_type"] == ""

        caplog.set_level(logging.INFO, logger="backend.app.services.bambu_mqtt")
        for _ in range(3):
            self._push(mqtt_client, [dict(CAPTURED_MINIMAL_TRAY)])
            wire = mqtt_client.on_ams_push_raw.last_wire_tray(0, 2)
            assert wire["tray_type"] == "", "injection must repeat on every partial"
            assert wire["tag_uid"] == "0000000000000000"
            assert wire["remain"] == 0
            assert mqtt_client.on_ams_push_raw.last(0, 2).present is False

        assert self._clear_logs(caplog) == [], "steady-state arm is silent"

    def test_edge_arm_injects_and_logs_once(self, mqtt_client, caplog):
        """Merged copy still holds content, no exist bits -> inject + ONE INFO line."""
        self._push(mqtt_client, [dict(CAPTURED_LOADED_TRAY)])
        assert self._merged(mqtt_client, 0, 0)["tray_type"] == "PETG"

        caplog.set_level(logging.INFO, logger="backend.app.services.bambu_mqtt")
        self._push(mqtt_client, [{"id": "0", "state": 9}])

        wire = mqtt_client.on_ams_push_raw.last_wire_tray(0, 0)
        assert wire["tray_type"] == ""
        assert mqtt_client.on_ams_push_raw.last(0, 0).present is False
        assert len(self._clear_logs(caplog)) == 1

        # Merge result unchanged from pre-split behaviour: display copy cleared,
        # identity wiped through the same slot_clearing path.
        merged = self._merged(mqtt_client, 0, 0)
        assert merged["tray_type"] == ""
        assert merged["tray_color"] == ""
        assert merged["tag_uid"] == "0000000000000000"
        assert merged["tray_uuid"] == "00000000000000000000000000000000"
        assert merged["remain"] == 0
        assert merged["state"] == 9

    def test_exist_bit_veto_blocks_injection(self, mqtt_client, caplog):
        """003-H2S: a mid-print insert sits at state 9 with its bit set — never clear."""
        # Seed WITH a bitmask so the client caches it; bit 0 = AMS0 slot 0 occupied.
        self._push(mqtt_client, [dict(CAPTURED_LOADED_TRAY)], tray_exist_bits="1")
        assert mqtt_client._last_tray_exist_bits == 1

        caplog.set_level(logging.INFO, logger="backend.app.services.bambu_mqtt")
        self._push(mqtt_client, [{"id": "0", "state": 9}])

        wire = mqtt_client.on_ams_push_raw.last_wire_tray(0, 0)
        assert set(wire) == {"id", "state"}, "vetoed slot must reach the pipeline untouched"
        assert mqtt_client.on_ams_push_raw.last(0, 0).present is None, "presence stays UNKNOWN under the veto"
        assert self._merged(mqtt_client, 0, 0)["tray_type"] == "PETG", "seated spool keeps its identity"
        assert self._clear_logs(caplog) == []

    @pytest.mark.parametrize("state", [0, 3, 8, 25, 26, 27])
    def test_non_nine_states_never_inject(self, mqtt_client, state):
        """STRICTLY 9. 0 = H2C long-idle, 3 = A1/P1S constant, 25/27 = H2C dialect
        on visibly-loaded trays, 8/26 = transitional — asserting empty for any of
        them would authorize a release on a possibly-loaded tray."""
        self._push(mqtt_client, [_captured_tray(CAPTURED_CLEARED_TRAY, 2)])  # merged copy long-cleared
        self._push(mqtt_client, [{"id": "2", "state": state}])

        assert set(mqtt_client.on_ams_push_raw.last_wire_tray(0, 2)) == {"id", "state"}
        assert mqtt_client.on_ams_push_raw.last(0, 2).present is None

    def test_state_8_still_clears_merged_display_copy(self, mqtt_client, caplog):
        """Upstream behaviour pinned: the merge-side block keeps its non-9 duty."""
        self._push(mqtt_client, [dict(CAPTURED_LOADED_TRAY)])
        caplog.set_level(logging.INFO, logger="backend.app.services.bambu_mqtt")
        self._push(mqtt_client, [{"id": "0", "state": 8}])

        # Raw side untouched (not state 9) — the merge alone clears the display copy.
        assert set(mqtt_client.on_ams_push_raw.last_wire_tray(0, 0)) == {"id", "state"}
        assert mqtt_client.on_ams_push_raw.last(0, 0).present is None
        merged = self._merged(mqtt_client, 0, 0)
        assert merged["tray_type"] == ""
        assert merged["tag_uid"] == "0000000000000000"
        assert len(self._clear_logs(caplog)) == 1, "merge-side block logs the non-9 clear"

    def test_feeding_dialect_tray_untouched(self, mqtt_client):
        """004-H2S state-9-while-feeding: the push asserts a type, so it is never
        overwritten — and presence stays UNKNOWN, not empty."""
        self._push(mqtt_client, [{"id": "0", "state": 9, "tray_type": "PETG", "remain": -1}])

        wire = mqtt_client.on_ams_push_raw.last_wire_tray(0, 0)
        assert wire == {"id": "0", "state": 9, "tray_type": "PETG", "remain": -1}
        assert mqtt_client.on_ams_push_raw.last(0, 0).present is None

    def test_full_cleared_tray_passes_through_unmodified(self, mqtt_client, caplog):
        """A tray that already carries the cleared shape is not rewritten."""
        caplog.set_level(logging.INFO, logger="backend.app.services.bambu_mqtt")
        original = dict(CAPTURED_CLEARED_TRAY)
        self._push(mqtt_client, [dict(CAPTURED_CLEARED_TRAY)])

        assert mqtt_client.on_ams_push_raw.last_wire_tray(0, 1) == original
        assert mqtt_client.on_ams_push_raw.last(0, 1).present is False
        assert self._clear_logs(caplog) == []

    def test_state9_edge_logs_exactly_once_across_both_authorities(self, mqtt_client, caplog):
        """No double-processing: the raw-side injection makes the merge block's
        ``"tray_type" not in new_tray`` guard False, so it cannot fire again."""
        self._push(mqtt_client, [dict(CAPTURED_LOADED_TRAY)])
        caplog.set_level(logging.INFO, logger="backend.app.services.bambu_mqtt")
        self._push(mqtt_client, [{"id": "0", "state": 9}])

        assert len(self._clear_logs(caplog)) == 1, "one clear, one log — not two"

    def test_captured_boot_forgotten_slot_becomes_releasable(self, mqtt_client, caplog):
        """The measured prod push, verbatim: the minimal slot carries no tray_type
        in ANY push, so its merged copy never holds one either. Presence must still
        resolve to False — on the FIRST push and every push after it."""
        caplog.set_level(logging.INFO, logger="backend.app.services.bambu_mqtt")
        push = [
            dict(CAPTURED_LOADED_TRAY),
            _captured_tray(CAPTURED_CLEARED_TRAY, 1),
            dict(CAPTURED_MINIMAL_TRAY),
            _captured_tray(CAPTURED_CLEARED_TRAY, 3),
        ]
        for _ in range(2):
            self._push(mqtt_client, [dict(t) for t in push])
            capture = mqtt_client.on_ams_push_raw
            assert capture.last(0, 0).present is True
            assert capture.last(0, 1).present is False
            assert capture.last(0, 2).present is False, "boot-forgotten slot must read EMPTY"
            assert capture.last(0, 3).present is False

        assert self._merged(mqtt_client, 0, 2)["tray_type"] == ""
        assert self._clear_logs(caplog) == [], "never-configured slot is not a clearing edge"


class TestEvidencePushall:
    """A veto standing on a CACHED exist bit owes the wire one pushall.

    H2S sends ``tray_exist_bits`` in PUSHALLS ONLY, so the cached mask goes stale the
    moment a roll leaves between two of them. The veto in ``_normalize_cleared_trays``
    then blocks the asserted-cleared shape on every ~1 Hz partial forever: presence
    stays UNKNOWN and release-on-empty (doctrine rule 9) can never fire — measured in
    prod as 13 bound-but-empty slots and ZERO releases ever. The farm now asks the
    printer to re-report instead of guessing. The veto itself is untouched: a bit
    carried by THIS push is the 003-H2S mid-print insert and settles the question.
    """

    client_kwargs = {"serial": "TEST_H2S", "connected": True}

    @pytest.fixture
    def mqtt_client(self, mqtt_client):
        """``on_ams_push_raw`` is consumed the way production consumes it."""
        mqtt_client.on_ams_push_raw = _RawCapture()
        return mqtt_client

    @staticmethod
    def _pushalls(mqtt_client):
        """Every pushall REQUEST published — the message a full report answers."""
        return [
            json.loads(call[0][1])
            for call in mqtt_client._client.publish.call_args_list
            if json.loads(call[0][1]).get("pushing", {}).get("command") == "pushall"
        ]

    @staticmethod
    def _evidence_logs(caplog, needle):
        return [r for r in caplog.records if "[EVIDENCE]" in r.getMessage() and needle in r.getMessage()]

    @staticmethod
    def _seat_with_bits(mqtt_client, bits="1"):
        """A pushall from while the roll was seated: the slot's bit is SET and cached."""
        mqtt_client._handle_ams_data(
            {
                "ams": [
                    {
                        "id": 0,
                        "tray": [{"id": 0, "tray_type": "PETG", "tray_color": "00FF00FF", "remain": 42, "state": 11}],
                    }
                ],
                "tray_exist_bits": bits,
                "power_on_flag": True,
            }
        )

    @staticmethod
    def _minimal_partial(mqtt_client, times=1):
        """The bitless ~1 Hz partial the H2S sends for a slot it reports nothing about."""
        for _ in range(times):
            mqtt_client._handle_ams_data({"ams": [{"id": 0, "tray": [{"id": 0, "state": 9}]}]})

    def test_stale_bit_partials_request_exactly_one_pushall(self, mqtt_client, caplog):
        """The incident's own cadence: five contradicting partials, ONE request."""
        caplog.set_level(logging.INFO, logger="backend.app.services.bambu_mqtt")
        self._seat_with_bits(mqtt_client)
        assert mqtt_client._last_tray_exist_bits == 1
        assert self._pushalls(mqtt_client) == [], "a healthy report asks for nothing"

        self._minimal_partial(mqtt_client, times=5)

        assert len(self._pushalls(mqtt_client)) == 1, "paced: one request, never one per partial"
        assert len(self._evidence_logs(caplog, "pushall owed")) == 1, "one owed INFO per epoch, not per push"
        assert len(self._evidence_logs(caplog, "slot(s) owed")) == 1
        assert mqtt_client.on_ams_push_raw.last(0, 0).present is None, "the veto still holds — presence stays UNKNOWN"

    def test_a_bits_carrying_push_answers_every_owed_slot(self, mqtt_client):
        """The report the request asked for closes the epoch: nothing stays owed, and
        the un-vetoed partials that follow assert EMPTY instead of owing again.

        The answer is an all-zero mask, so it must repeat before the farm may act on
        it — until then it is treated exactly as a bits-less push, owed slots included.
        """
        self._seat_with_bits(mqtt_client)
        self._minimal_partial(mqtt_client, times=2)
        assert mqtt_client._evidence_owed, "precondition: the slot is owed a report"

        cleared_report = {
            "ams": [{"id": 0, "tray": [{"id": 0, "state": 9}]}],
            "tray_exist_bits": "0",
            "power_on_flag": True,
        }
        for _ in range(_ZERO_EXIST_BITS_TRUST_PUSHES - 1):
            mqtt_client._handle_ams_data(cleared_report)
            assert mqtt_client._evidence_owed, "an untrusted zero answers nothing"
        mqtt_client._handle_ams_data(cleared_report)
        assert mqtt_client._evidence_owed == {}, "the answering report clears the whole epoch"
        assert mqtt_client.on_ams_push_raw.last(0, 0).present is False, "bit clear → the slot reads EMPTY"

        self._minimal_partial(mqtt_client, times=3)
        assert mqtt_client._evidence_owed == {}, "no veto left to contradict — nothing re-owed"
        assert mqtt_client.on_ams_push_raw.last(0, 0).present is False

    def test_bits_carried_by_this_push_owe_nothing(self, mqtt_client, caplog):
        """003-H2S PROTECTION: a mid-print insert sits at state 9 with its bit SET in
        the SAME push. That is the firmware's current answer — the slot reads SEATED,
        it is not owed a report and no pushall goes out."""
        caplog.set_level(logging.INFO, logger="backend.app.services.bambu_mqtt")
        mqtt_client._handle_ams_data(
            {
                "ams": [{"id": 0, "tray": [{"id": 0, "state": 9}]}],
                "tray_exist_bits": "1",
                "power_on_flag": True,
            }
        )

        assert mqtt_client._evidence_owed == {}
        assert self._pushalls(mqtt_client) == []
        assert self._evidence_logs(caplog, "[EVIDENCE]") == []
        assert mqtt_client.on_ams_push_raw.last(0, 0).present is True, "the set bit IS the seating"

    def test_request_evidence_pushall_is_paced(self, mqtt_client, caplog):
        """The service-side lane: one request, then defer — never a loop."""
        caplog.set_level(logging.INFO, logger="backend.app.services.bambu_mqtt")
        assert mqtt_client.request_evidence_pushall("bound_presence_unknown") is True
        assert len(self._pushalls(mqtt_client)) == 1
        assert len(self._evidence_logs(caplog, "bound_presence_unknown")) == 1

        assert mqtt_client.request_evidence_pushall("bound_presence_unknown") is False
        assert len(self._pushalls(mqtt_client)) == 1, "inside the floor: nothing published"

    def test_request_evidence_pushall_needs_a_connection(self, mqtt_client):
        """A disconnected printer answers nothing; the caller defers."""
        mqtt_client.state.connected = False
        assert mqtt_client.request_evidence_pushall("bound_presence_unknown") is False
        assert self._pushalls(mqtt_client) == []

    def test_both_evidence_lanes_share_one_pacing_floor(self, mqtt_client):
        """One origin: the wire-side drain and the service-side request cannot add up
        to two reports inside the window."""
        self._seat_with_bits(mqtt_client)
        self._minimal_partial(mqtt_client, times=2)
        assert len(self._pushalls(mqtt_client)) == 1

        assert mqtt_client.request_evidence_pushall("bound_presence_unknown") is False
        assert len(self._pushalls(mqtt_client)) == 1


class TestStartPrintAllNegativeMappingRefused:
    """Translation honesty in the transport's own layer.

    ``-1`` means "no tray feeds this filament", so a mapping made ENTIRELY of them
    describes a print with no filament source at all. That is NOT the statement
    "this print uses the external spool" — yet it used to be translated into exactly
    that (``use_ams=False``), and a printer whose external holder was unconfigured
    then demanded external filament, had the fault classified as an AMS jam, and
    quarantined itself.

    This is not a second copy of the scheduler's completeness gate: that one decides
    whether a FARM item is ready, this one keeps the translation truthful for every
    caller of ``start_print`` (the direct API, foreign callers, the virtual printer).
    The bool return is the established fail-loud seam.
    """

    client_kwargs = {"connected": True}

    @pytest.mark.parametrize("mapping", [[-1], [-1, -1], [-1, -1, -1, -1], [None, -1], [None]])
    def test_all_negative_refuses_and_never_publishes(self, mqtt_client, mapping, caplog):
        with caplog.at_level(logging.ERROR):
            assert mqtt_client.start_print("test.3mf", ams_mapping=mapping) is False

        mqtt_client._client.publish.assert_not_called()
        assert "Refusing print start" in caplog.text
        assert "maps no filament source" in caplog.text

    def test_refusal_precedes_the_submission_id_mint(self):
        """Nothing may be mutated on the way to a refusal — a minted dispatch subtask id
        would let correlation bind a job that was never sent."""
        client = _make_client(serial="T2", connected=True)
        client.last_dispatch_subtask_id = "previous"

        assert client.start_print("test.3mf", ams_mapping=[-1, -1]) is False

        assert client.last_dispatch_subtask_id == "previous"
        assert client.state.dispatched_plate_id is None

    @pytest.mark.parametrize("use_ams", [True, False])
    def test_refusal_is_unconditional_on_use_ams(self, mqtt_client, use_ams):
        """The mapping is untranslatable whatever the caller asked for."""
        assert mqtt_client.start_print("t.3mf", ams_mapping=[-1], use_ams=use_ams) is False

        mqtt_client._client.publish.assert_not_called()

    def test_refusal_applies_on_dual_nozzle_hardware_too(self, mqtt_client):
        """Dual-nozzle models bypass the use_ams coercion, not the honesty check."""
        mqtt_client._is_dual_nozzle = True

        assert mqtt_client.start_print("t.3mf", ams_mapping=[-1, -1]) is False

        mqtt_client._client.publish.assert_not_called()

    def test_one_external_entry_makes_it_a_real_external_print(self, mqtt_client):
        """``[-1, 254]`` is a genuine external print with one unused filament slot."""
        assert mqtt_client.start_print("test.3mf", ams_mapping=[-1, 254], use_ams=True) is True

        cmd = _published_command(mqtt_client)
        assert cmd["ams_mapping"] == [-1, -1]
        assert cmd["ams_mapping2"] == [{"ams_id": 255, "slot_id": 255}, {"ams_id": 255, "slot_id": 0}]
        assert cmd["use_ams"] is False, "every MAPPED slot is external — the honest reading"

    def test_a_plain_ams_mapping_is_unchanged(self, mqtt_client):
        """The regression fence: an ordinary dispatch translates exactly as before."""
        assert mqtt_client.start_print("test.3mf", ams_mapping=[4], use_ams=True) is True

        cmd = _published_command(mqtt_client)
        assert cmd["ams_mapping"] == [4]
        assert cmd["ams_mapping2"] == [{"ams_id": 1, "slot_id": 0}]
        assert cmd["use_ams"] is True

    def test_no_mapping_at_all_is_not_a_refusal(self, mqtt_client):
        """``None`` means "this print needs no mapping" — a legitimate mapping-free
        dispatch (an eject file, a single-filament print with no AMS). Only a
        NON-EMPTY all-negative mapping is a lie."""
        assert mqtt_client.start_print("test.3mf", ams_mapping=None) is True

        assert "ams_mapping" not in _published_command(mqtt_client)

    def test_empty_mapping_is_not_a_refusal(self, mqtt_client):
        assert mqtt_client.start_print("test.3mf", ams_mapping=[]) is True

        mqtt_client._client.publish.assert_called_once()


class TestPrintLineNumberParse:
    """``mc_print_line_number`` retention, for localizing an eject stall.

    Percent alone cannot say whether a slow eject is stuck in the bed-drop or the
    sweep; the executing G-code line lands inside one phase. The field's presence on
    the H2S wire is UNVERIFIED, so the parse is defensive by design and every reader
    treats None as "not published OR not parsed" — and because it is only a
    breadcrumb, a junk value must never cost the rest of the status report.
    """

    @pytest.mark.parametrize(
        "value, parsed",
        [
            # Firmware spells it as a decimal string on the models where it has been seen.
            pytest.param("41207", 41207, id="a_decimal_string_is_coerced"),
            pytest.param(512, 512, id="an_int_is_kept"),
            pytest.param("n/a", None, id="junk_falls_back_to_none"),
        ],
    )
    def test_the_line_number_is_parsed_defensively(self, mqtt_client, value, parsed):
        mqtt_client._process_message(
            {"print": {"mc_print_line_number": value, "mc_percent": 77, "mc_remaining_time": 12}}
        )

        assert mqtt_client.state.mc_print_line_number == parsed
        # The sibling fields in the same block land regardless.
        assert mqtt_client.state.progress == 77.0
        assert mqtt_client.state.remaining_time == 12

    def test_absent_field_leaves_it_none(self, mqtt_client):
        mqtt_client._process_message({"print": {"mc_percent": 42, "gcode_state": "RUNNING"}})

        assert mqtt_client.state.progress == 42.0
        assert mqtt_client.state.mc_print_line_number is None

    def test_incremental_push_without_the_field_keeps_the_last_value(self, mqtt_client):
        """A field absent from an incremental push is unchanged, not cleared — the same
        convention as ``mc_percent``."""
        mqtt_client._process_message({"print": {"mc_print_line_number": 900}})

        mqtt_client._process_message({"print": {"mc_percent": 50}})

        assert mqtt_client.state.mc_print_line_number == 900


class TestProgressWireRecency:
    """``progress_wire_at`` — the recency stamp on ``mc_percent``.

    The percent field always holds SOME value, so its value cannot say whether it
    describes the job running now. The eject runtime watchdog decides on M73 phase
    edges, so it needs to tell a percent THIS push carried from one held over — which
    is what this stamp, and only this stamp, answers. Same rule as ``hms_wire_at``:
    only a frame BEARING the field stamps it.
    """

    def test_starts_unstamped(self, mqtt_client):
        assert mqtt_client.state.progress_wire_at == 0.0

    def test_a_percent_bearing_push_stamps_it(self, mqtt_client):
        before = time.monotonic()

        mqtt_client._process_message({"print": {"mc_percent": 5, "gcode_state": "RUNNING"}})

        assert mqtt_client.state.progress == 5.0
        assert mqtt_client.state.progress_wire_at >= before

    def test_a_push_without_the_field_does_not_stamp_it(self, mqtt_client):
        """Otherwise a silent link would look freshly reported."""
        mqtt_client._process_message({"print": {"mc_percent": 50}})
        stamped = mqtt_client.state.progress_wire_at
        assert stamped > 0.0

        mqtt_client._process_message({"print": {"gcode_state": "RUNNING", "mc_remaining_time": 30}})

        assert mqtt_client.state.progress == 50.0, "the value is retained…"
        assert mqtt_client.state.progress_wire_at == stamped, "…but not re-dated"

    def test_every_percent_bearing_push_advances_it(self, mqtt_client):
        """The same VALUE in a new push is still a new report."""
        mqtt_client._process_message({"print": {"mc_percent": 5}})
        first = mqtt_client.state.progress_wire_at

        mqtt_client._process_message({"print": {"mc_percent": 5}})

        assert mqtt_client.state.progress_wire_at >= first

    def test_a_zero_percent_push_is_still_a_report(self, mqtt_client):
        """The eject job resets ``mc_percent`` to 0 at start: that zero is evidence the
        job began, not an absence of evidence."""
        mqtt_client._process_message({"print": {"mc_percent": 0, "gcode_state": "RUNNING"}})

        assert mqtt_client.state.progress == 0.0
        assert mqtt_client.state.progress_wire_at > 0.0


class TestPredecessorReadingGate:
    """The stale-predecessor gate on ``layer_num`` / ``mc_percent`` (incident 2026-08-22).

    Wire behaviour: after a print starts, the firmware keeps republishing the PREVIOUS
    job's layer/percent for the seconds the new one spends heating and levelling. The
    old "last non-zero value" capture could not tell that republish apart from the
    cancel-reset it was written for, so a print an operator stopped at layer 0 reported
    ``last_layer_num=167`` — the plate total — and was charged the whole plate (417.9 g),
    while the deposit judgement read the same stale pair and raised the plate gate over a
    clean bed. That judgement is ``DepositEvidence.deposited`` on the occupancy
    authority, the ONE origin for it.

    Everything here drives the real ``_process_message`` entry point and asserts on the
    real ``on_print_complete`` payload, because that payload IS what the two victims
    (usage tracking and the plate gate) consume.
    """

    @staticmethod
    def _capture_terminals(client) -> list:
        """Collect every on_print_complete payload the client emits."""
        seen: list = []
        client.on_print_complete = seen.append
        return seen

    @staticmethod
    def _push(client, **fields) -> None:
        client._process_message({"print": fields})

    @classmethod
    def _run_predecessor(cls, client, *, layers=(), percents=(), file="prev_job.gcode.3mf") -> None:
        """Drive a complete predecessor job, leaving its final readings on the wire state.

        The first push is the client's first ever, so the #1304 guard suppresses
        ``is_new_print`` for it — which is exactly how a real predecessor is observed.
        """
        cls._push(client, gcode_state="RUNNING", gcode_file=file)
        for layer in layers:
            cls._push(client, layer_num=layer)
        for percent in percents:
            cls._push(client, mc_percent=percent)
        cls._push(client, gcode_state="FINISH")

    @classmethod
    def _start_new_print(cls, client, file="new_job.gcode.3mf") -> None:
        cls._push(client, gcode_state="RUNNING", gcode_file=file)

    # ---- (a) the incident ------------------------------------------------------

    def test_stale_predecessor_layer_is_not_charged_to_the_new_print(self, mqtt_client):
        """A print stopped at layer 0 reports layer 0 — not the predecessor's plate total."""
        terminals = self._capture_terminals(mqtt_client)
        self._run_predecessor(mqtt_client, layers=(100, 167))
        assert mqtt_client.state.layer_num == 167  # predecessor's final layer is still on the wire

        self._start_new_print(mqtt_client)
        assert mqtt_client._prev_job_layer == 167
        assert mqtt_client._job_layer_baseline_seen is False

        self._push(mqtt_client, layer_num=167)  # the firmware's stale republish
        assert mqtt_client.state.layer_num == 0, "a predecessor reading must not become this job's layer"
        assert mqtt_client._last_valid_layer_num == 0

        self._push(mqtt_client, layer_num=0)  # this job's own first reading
        assert mqtt_client._job_layer_baseline_seen is True
        self._push(mqtt_client, layer_num=1)

        self._push(mqtt_client, gcode_state="FAILED")  # operator stop, nothing deposited
        assert len(terminals) == 2
        assert terminals[-1]["last_layer_num"] == 0

    def test_the_incident_pair_now_reads_as_no_deposit(self, mqtt_client):
        """End to end: the terminal payload makes the plate gate see an empty bed again.

        Re-pinned onto ``DepositEvidence`` (2026-08-30), which replaced
        ``eject.monitor.deposited_nothing`` as the one origin for this judgement. The
        payload is fed to the classmethod WHOLE — exactly as ``main.on_print_complete``
        does — so this test also pins that the client emits every key the evidence
        reads, ``peaks_reliable`` included.
        """
        terminals = self._capture_terminals(mqtt_client)
        self._run_predecessor(mqtt_client, layers=(100, 167), percents=(60, 100))
        self._start_new_print(mqtt_client)
        self._push(mqtt_client, layer_num=167, mc_percent=100)  # stale republish of both
        self._push(mqtt_client, layer_num=0, mc_percent=0)
        self._push(mqtt_client, gcode_state="FAILED")

        payload = terminals[-1]
        assert payload["last_layer_num"] == 0
        assert payload["last_progress"] == 0.0
        # This client watched the print START, so its peaks ARE a measurement of this
        # job — which is what lets the measured zero speak at all.
        assert payload["peaks_reliable"] is True

        evidence = DepositEvidence.from_terminal_payload(payload, is_dry_run=False)

        assert evidence.deposited is False, "a layer-0 operator stop must not gate a clean bed"

    # ---- (b) a genuine partial print still reports its layer --------------------

    def test_a_genuine_partial_print_still_reports_its_last_layer(self, mqtt_client):
        terminals = self._capture_terminals(mqtt_client)
        self._run_predecessor(mqtt_client, layers=(100, 167))
        self._start_new_print(mqtt_client)

        for layer in range(1, 41):  # real layers 1..40
            self._push(mqtt_client, layer_num=layer)
        assert mqtt_client.state.layer_num == 40

        self._push(mqtt_client, layer_num=0)  # firmware zeroes the layer on cancel
        self._push(mqtt_client, gcode_state="FAILED")

        assert terminals[-1]["last_layer_num"] == 40

    # ---- (c) anti-starvation ----------------------------------------------------

    def test_gate_releases_even_when_the_zero_and_one_frames_are_never_seen(self, mqtt_client):
        """The gate compares against the predecessor's FINAL layer, never a magnitude.

        A "<= 1" test would still be closed here and would then track NO layers at all for
        the whole job — charging a real print 0 g. That silent absence is strictly worse
        than the over-charge this guard exists to stop.
        """
        terminals = self._capture_terminals(mqtt_client)
        self._run_predecessor(mqtt_client, layers=(100, 167))
        self._start_new_print(mqtt_client)

        self._push(mqtt_client, layer_num=2)  # first reading ever observed for this job
        assert mqtt_client._job_layer_baseline_seen is True
        assert mqtt_client.state.layer_num == 2

        for layer in range(3, 41):
            self._push(mqtt_client, layer_num=layer)
        self._push(mqtt_client, layer_num=0)
        self._push(mqtt_client, gcode_state="FAILED")

        assert terminals[-1]["last_layer_num"] == 40

    # ---- (d) clean predecessor --------------------------------------------------

    def test_a_predecessor_that_ended_at_layer_zero_leaves_the_gate_open(self, mqtt_client):
        """The eject control case: an eject file prints no layers, so there is nothing to confuse.

        Printer 001 came through the incident clean for exactly this reason.
        """
        terminals = self._capture_terminals(mqtt_client)
        self._run_predecessor(mqtt_client, layers=(0,), file="eject_p1.gcode.3mf")
        assert mqtt_client.state.layer_num == 0

        self._start_new_print(mqtt_client)
        assert mqtt_client._prev_job_layer == 0

        # With no predecessor value to shadow it, even a high first reading is this job's.
        self._push(mqtt_client, layer_num=167)
        assert mqtt_client.state.layer_num == 167

        self._push(mqtt_client, layer_num=0)
        self._push(mqtt_client, gcode_state="FAILED")
        assert terminals[-1]["last_layer_num"] == 167

    def test_a_predecessor_stopped_at_layer_zero_leaves_the_gate_open(self, mqtt_client):
        """The second control case: the follow-up cancel on 002/003 was clean because its own
        predecessor was a layer-0 stop."""
        self._run_predecessor(mqtt_client, layers=(0,))
        self._start_new_print(mqtt_client)
        assert mqtt_client._prev_job_layer == 0
        self._push(mqtt_client, layer_num=5)
        assert mqtt_client.state.layer_num == 5

    # ---- (e) mid-print adoption -------------------------------------------------

    def test_mid_print_adoption_tracks_layers_exactly_as_before(self, mqtt_client):
        """A restart that adopts a print never fires is_new_print (#1304), so the gate must
        default OPEN — otherwise the adopted job would track no layers at all."""
        terminals = self._capture_terminals(mqtt_client)
        assert mqtt_client._job_layer_baseline_seen is True
        assert mqtt_client._job_progress_baseline_seen is True

        # First push after startup: RUNNING mid-print, already at layer 120.
        self._push(mqtt_client, gcode_state="RUNNING", gcode_file="adopted.gcode.3mf", layer_num=120, mc_percent=70)
        assert mqtt_client.state.layer_num == 120, "the adopted job's layer must be tracked immediately"
        assert mqtt_client._prev_job_layer == 0, "is_new_print never fired, so nothing was captured"

        self._push(mqtt_client, layer_num=121, mc_percent=71)
        self._push(mqtt_client, layer_num=122, mc_percent=72)
        self._push(mqtt_client, layer_num=0, mc_percent=0)  # cancel reset
        self._push(mqtt_client, gcode_state="FAILED")

        assert terminals[-1]["last_layer_num"] == 122
        assert terminals[-1]["last_progress"] == 72.0

    # ---- (f) the same guard on the progress lane --------------------------------

    def test_stale_predecessor_progress_is_not_charged_to_the_new_print(self, mqtt_client):
        """Mirror of (a). This lane matters on its own: spoolman_tracking reconstructs a
        layer from ``last_progress`` when ``last_layer_num`` is 0, so a leaked 100 % would
        re-charge the whole plate through the other door."""
        terminals = self._capture_terminals(mqtt_client)
        self._run_predecessor(mqtt_client, percents=(60, 100))
        assert mqtt_client.state.progress == 100.0

        self._start_new_print(mqtt_client)
        assert mqtt_client._prev_job_progress == 100.0
        assert mqtt_client._job_progress_baseline_seen is False

        self._push(mqtt_client, mc_percent=100)  # the firmware's stale republish
        assert mqtt_client._last_valid_progress == 0.0

        self._push(mqtt_client, mc_percent=0)  # this job's own first reading
        assert mqtt_client._job_progress_baseline_seen is True
        assert mqtt_client.state.progress == 0.0
        assert mqtt_client._last_valid_progress == 0.0, "the releasing reading must not save the predecessor's percent"

        self._push(mqtt_client, mc_percent=1)
        self._push(mqtt_client, gcode_state="FAILED")
        assert terminals[-1]["last_progress"] == 0.0

    def test_a_discarded_percent_does_not_stamp_the_recency_clock(self, mqtt_client):
        """progress_wire_at means "this push reported on the job running NOW" — the eject
        runtime watchdog reads it as fresh evidence, so a refused reading must not date it."""
        self._run_predecessor(mqtt_client, percents=(60, 100))
        self._start_new_print(mqtt_client)
        stamped = mqtt_client.state.progress_wire_at

        self._push(mqtt_client, mc_percent=100)  # discarded
        assert mqtt_client.state.progress_wire_at == stamped

        self._push(mqtt_client, mc_percent=0)  # accepted
        assert mqtt_client.state.progress_wire_at > stamped

    def test_progress_gate_releases_without_a_zero_frame(self, mqtt_client):
        """Mirror of (c) for the progress lane."""
        terminals = self._capture_terminals(mqtt_client)
        self._run_predecessor(mqtt_client, percents=(60, 100))
        self._start_new_print(mqtt_client)

        self._push(mqtt_client, mc_percent=3)  # first reading ever observed for this job
        assert mqtt_client._job_progress_baseline_seen is True
        assert mqtt_client.state.progress == 3.0

        for percent in (10, 25, 40):
            self._push(mqtt_client, mc_percent=percent)
        self._push(mqtt_client, mc_percent=0)  # cancel reset
        self._push(mqtt_client, gcode_state="FAILED")

        assert terminals[-1]["last_progress"] == 40.0

    def test_a_genuine_partial_print_still_reports_its_last_percent(self, mqtt_client):
        """Mirror of (b) for the progress lane."""
        terminals = self._capture_terminals(mqtt_client)
        self._run_predecessor(mqtt_client, percents=(60, 100))
        self._start_new_print(mqtt_client)

        for percent in (1, 12, 33, 40):
            self._push(mqtt_client, mc_percent=percent)
        self._push(mqtt_client, mc_percent=0)
        self._push(mqtt_client, gcode_state="FAILED")

        assert terminals[-1]["last_progress"] == 40.0

    # ---- the gate never suppresses the callback it guards ------------------------

    def test_a_discarded_layer_does_not_fire_the_layer_change_callback(self, mqtt_client):
        """The layer-change hook drives layer-based timelapse capture — a predecessor's
        republished layer must not trigger a frame for a job that has not reached it."""
        self._run_predecessor(mqtt_client, layers=(100, 167))
        self._start_new_print(mqtt_client)

        fired: list = []
        mqtt_client.on_layer_change = fired.append

        self._push(mqtt_client, layer_num=167)  # discarded
        assert fired == []

        self._push(mqtt_client, layer_num=1)  # accepted
        assert fired == [1]

    # ---- the capture itself must not be poisoned by the start push ---------------

    def test_a_start_push_carrying_a_genuine_zero_still_arms_the_gate(self, mqtt_client):
        """The capture reads ``max(live field, _last_valid_*)`` for a reason.

        The field handlers run BEFORE the print-start block inside ``_update_state``, so a
        start push that itself carries a genuine ``layer_num: 0`` has already zeroed the
        live field and moved the predecessor's real final layer into ``_last_valid_layer_num``.
        Capturing the live field alone would arm the gate at 0 — and a gate armed at 0 is
        disabled for the WHOLE job, which is the original incident by another door.
        """
        self._run_predecessor(mqtt_client, layers=(100, 167), percents=(60, 100))

        # The start push carries this job's own honest zeros.
        self._push(mqtt_client, gcode_state="RUNNING", gcode_file="new_job.gcode.3mf", layer_num=0, mc_percent=0)
        assert mqtt_client._prev_job_layer == 167, "the gate must arm on the predecessor's real final layer"
        assert mqtt_client._prev_job_progress == 100.0

        # ...so the republish that follows is still recognised as the predecessor's.
        self._push(mqtt_client, layer_num=167, mc_percent=100)
        assert mqtt_client.state.layer_num == 0
        assert mqtt_client.state.progress == 0.0

    def test_zero_bearing_start_push_then_layer_zero_stop_charges_nothing(self, mqtt_client):
        """Full incident replay through the zero-bearing-start-push door, both lanes.

        Sequence matters: the operator stopped at layer 0, so the genuine readings are
        0 then 1. A 1-then-2 sequence would report 1 either way and prove nothing.
        """
        terminals = self._capture_terminals(mqtt_client)
        self._run_predecessor(mqtt_client, layers=(100, 167), percents=(60, 100))
        self._push(mqtt_client, gcode_state="RUNNING", gcode_file="new_job.gcode.3mf", layer_num=0, mc_percent=0)
        self._push(mqtt_client, layer_num=167, mc_percent=100)  # stale republish
        self._push(mqtt_client, layer_num=0, mc_percent=0)  # this job's own readings
        self._push(mqtt_client, layer_num=1, mc_percent=1)
        self._push(mqtt_client, gcode_state="FAILED")  # operator stop at layer 0

        payload = terminals[-1]
        assert payload["last_layer_num"] == 0
        assert payload["last_progress"] == 0.0
        assert payload["peaks_reliable"] is True

        evidence = DepositEvidence.from_terminal_payload(payload, is_dry_run=False)

        assert evidence.deposited is False, "a layer-0 operator stop must not gate a clean bed"

    def test_a_zero_bearing_start_push_does_not_over_arm_a_genuine_print(self, mqtt_client):
        """The max-capture must not suppress a real print's layers: the gate still releases
        on the first genuine reading, and a partial print reports its true last layer."""
        terminals = self._capture_terminals(mqtt_client)
        self._run_predecessor(mqtt_client, layers=(100, 167), percents=(60, 100))
        self._push(mqtt_client, gcode_state="RUNNING", gcode_file="new_job.gcode.3mf", layer_num=0, mc_percent=0)

        for layer in range(1, 41):
            self._push(mqtt_client, layer_num=layer, mc_percent=layer)
        assert mqtt_client.state.layer_num == 40

        self._push(mqtt_client, layer_num=0, mc_percent=0)  # cancel reset
        self._push(mqtt_client, gcode_state="FAILED")

        assert terminals[-1]["last_layer_num"] == 40
        assert terminals[-1]["last_progress"] == 40.0


class TestDepositEvidenceOverRealTerminalPayloads:
    """The fail-closed deposit rules, read off payloads this client actually emits.

    A measured zero is only ONE of four answers, and the other three all fail CLOSED,
    because absence of measurement is not measurement of absence. The cascade that
    proves it: six restart-adopted prints finished ``completed`` and reported zeroed
    peaks — the peaks live in process memory and the client was born mid-print — were
    read as "nothing on the plate", and the next unit dispatched onto the finished part
    1-5 s later.

    Driven through the real ``_process_message`` so the payload's KEYS are pinned too:
    a client that stopped emitting ``peaks_reliable`` would silently land every
    terminal on the fail-closed side, which the classmethod's default is designed to
    survive but which is a regression worth catching here.
    """

    @staticmethod
    def _terminal_payload(client, *, final_state: str, observe_start: bool) -> dict:
        """Run a job to *final_state* and return the emitted terminal payload.

        ``observe_start`` False models the restart-recovery attach: the #1304 guard
        suppresses ``is_new_print`` on a client's first ever push, so the peaks never
        become a measurement of this job.
        """
        seen: list = []
        client.on_print_complete = seen.append
        if observe_start:
            client._process_message({"print": {"gcode_state": "IDLE"}})
        client._process_message({"print": {"gcode_state": "RUNNING", "gcode_file": "job.gcode.3mf"}})
        client._process_message({"print": {"gcode_state": final_state}})
        return seen[-1]

    def test_a_dry_run_never_deposits(self, mqtt_client):
        """The eject dry-run file is motion-only by design — there is nothing to leave
        behind, whatever the printer reports."""
        payload = self._terminal_payload(mqtt_client, final_state="FINISH", observe_start=True)

        assert DepositEvidence.from_terminal_payload(payload, is_dry_run=True).deposited is False

    def test_a_completed_print_always_deposits(self, mqtt_client):
        """Peaks are irrelevant to a job the printer itself says it finished — this is
        the limb that would have gated all six of the 2026-08-29 plates."""
        payload = self._terminal_payload(mqtt_client, final_state="FINISH", observe_start=True)

        assert payload["status"] == "completed"
        assert payload["last_layer_num"] == 0  # zero layers observed...
        assert DepositEvidence.from_terminal_payload(payload, is_dry_run=False).deposited is True

    def test_unknown_peaks_deposit(self, mqtt_client):
        """A client born mid-print re-tracks from an unknown baseline and can honestly
        report zeros for a print that is physically three-quarters done."""
        payload = self._terminal_payload(mqtt_client, final_state="FAILED", observe_start=False)

        assert payload["peaks_reliable"] is False
        assert payload["status"] != "completed"
        assert DepositEvidence.from_terminal_payload(payload, is_dry_run=False).deposited is True

    def test_a_payload_with_no_peaks_reliable_key_fails_closed(self):
        """An older client, a virtual printer that has not caught up, or any payload
        shaped before the key existed must all land on the fail-closed side."""
        evidence = DepositEvidence.from_terminal_payload(
            {"status": "failed", "last_layer_num": 0, "last_progress": 0.0},
            is_dry_run=False,
        )

        assert evidence.peaks_reliable is False
        assert evidence.deposited is True


class TestIdleFromPauseCompletion:
    """A ``print.stop`` sent while the job is PAUSEd can land straight in IDLE.

    The detector only ever accepted IDLE from RUNNING, so such a stop produced NO
    terminal at all and the queue row sat ``printing`` forever. It is the shape the
    2026-09-04 plate-check lane makes on every trip — the firmware pauses at layer 0
    and the farm stops it there — and it is also how an operator screen-stop of a
    PAUSEd print has silently gone unrecorded.
    """

    def test_pause_to_idle_after_a_job_fires_the_terminal(self, mqtt_client):
        complete_data = {}
        mqtt_client.on_print_start = lambda data: None
        mqtt_client.on_print_complete = lambda data: complete_data.update(data)

        mqtt_client._previous_gcode_state = "PAUSE"
        mqtt_client._was_running = True  # there WAS a job
        mqtt_client._completion_triggered = False

        mqtt_client._process_message(
            {
                "print": {
                    "gcode_state": "IDLE",
                    "gcode_file": "/data/Metadata/plate_1.gcode",
                    "subtask_name": "PlateCheckTrip",
                }
            }
        )

        assert complete_data.get("status") == "aborted"

    def test_pause_to_idle_with_no_job_fires_nothing(self, mqtt_client):
        """Without ``_was_running`` there was no print, so there is no terminal."""
        calls = []
        mqtt_client.on_print_start = lambda data: None
        mqtt_client.on_print_complete = lambda data: calls.append(data)

        mqtt_client._previous_gcode_state = "PAUSE"
        mqtt_client._was_running = False
        mqtt_client._completion_triggered = False

        mqtt_client._process_message(
            {
                "print": {
                    "gcode_state": "IDLE",
                    "gcode_file": "/data/Metadata/plate_1.gcode",
                    "subtask_name": "NothingRan",
                }
            }
        )

        assert calls == []

    def test_running_to_idle_still_fires_without_the_was_running_flag(self, mqtt_client):
        """The RUNNING arm is deliberately left unguarded — today's behaviour.

        ``_was_running`` is only set on a RUNNING push that carried a filename, so
        requiring it here would DROP terminals rather than add them.
        """
        complete_data = {}
        mqtt_client.on_print_start = lambda data: None
        mqtt_client.on_print_complete = lambda data: complete_data.update(data)

        mqtt_client._previous_gcode_state = "RUNNING"
        mqtt_client._was_running = False
        mqtt_client._completion_triggered = False

        mqtt_client._process_message(
            {
                "print": {
                    "gcode_state": "IDLE",
                    "gcode_file": "/data/Metadata/plate_1.gcode",
                    "subtask_name": "Aborted",
                }
            }
        )

        assert complete_data.get("status") == "aborted"


class TestSetFanPercent:
    """``set_fan_percent`` is the ONE origin of the percent→PWM conversion.

    The wire takes ``M106 P<fan> S<0-255>``; every surface above it (the ``/fan-speed``
    route, the eject cooldown's aux-fan prep) speaks percent. These pin the mapping at
    the boundaries and in the middle, so a second inline ``* 255 / 100`` cannot be
    reintroduced somewhere else and quietly disagree.
    """

    client_kwargs = {"serial": "FAN123", "connected": True}

    @staticmethod
    def _published_gcode(client) -> list[str]:
        """Every G-code param this client published, in order."""
        return [json.loads(call.args[1])["print"]["param"] for call in client._client.publish.call_args_list]

    @pytest.mark.parametrize(
        ("percent", "expected"),
        [
            (100, "M106 P2 S255"),  # full
            (50, "M106 P2 S128"),  # 127.5 rounds to 128
            (0, "M106 P2 S0"),  # off
        ],
    )
    def test_percent_maps_to_pwm(self, mqtt_client, percent, expected):
        assert mqtt_client.set_fan_percent(2, percent) is True
        assert self._published_gcode(mqtt_client) == [expected]

    @pytest.mark.parametrize(
        ("percent", "expected"),
        [
            (101, "M106 P2 S255"),
            (5000, "M106 P2 S255"),
            (-1, "M106 P2 S0"),
            (-5000, "M106 P2 S0"),
        ],
    )
    def test_out_of_range_percent_clamps(self, mqtt_client, percent, expected):
        """Clamped, never scaled past the wire's 0-255 — an out-of-range percent must not
        become an out-of-range PWM the firmware would reject or misread."""
        assert mqtt_client.set_fan_percent(2, percent) is True
        assert self._published_gcode(mqtt_client) == [expected]

    def test_fan_index_rides_through_to_the_command(self, mqtt_client):
        # 1 = part cooling, 2 = auxiliary, 3 = chamber.
        mqtt_client.set_fan_percent(1, 100)
        mqtt_client.set_fan_percent(3, 100)
        assert self._published_gcode(mqtt_client) == ["M106 P1 S255", "M106 P3 S255"]

    def test_invalid_fan_index_is_refused_without_publishing(self, mqtt_client):
        """The validity gate lives in set_fan_speed; set_fan_percent must not bypass it."""
        assert mqtt_client.set_fan_percent(4, 100) is False
        assert self._published_gcode(mqtt_client) == []

    def test_disconnected_client_reports_failure(self, mqtt_client):
        """send_gcode is fail-loud (2026-07-21): a disconnected printer cannot eat a
        command silently, so the percent wrapper must propagate the False."""
        mqtt_client.state.connected = False
        assert mqtt_client.set_fan_percent(2, 100) is False
        assert self._published_gcode(mqtt_client) == []


class TestAmsMidFilamentChange:
    """The ONE predicate for "the AMS is mid filament-change and drops every write".

    A layer-0 jam leaves the AMS at ``ams_status_main == 1`` behind a PAUSE, where a
    Load click returns 200 and moves nothing and the scheduler will happily dispatch
    onto it. Value 1 is what makes that decidable — and value 1 ONLY: a fleet sample
    read ``ams_status_main = 3`` on every RUNNING H2S (assist) and 0 on an idle H2C,
    so a "non-idle" reading of this question would refuse dispatch on every healthy
    print in the farm. Unknown is not mid-change either: a gate that cannot read the
    wire must never refuse an operator.
    """

    @pytest.mark.parametrize(
        "state, mid_change",
        [
            pytest.param(SimpleNamespace(ams_status_main=1), True, id="value1_is_mid_change"),
            pytest.param(SimpleNamespace(ams_status_main=0), False, id="value0_idle"),
            pytest.param(SimpleNamespace(ams_status_main=2), False, id="value2_identifying"),
            # 3 is ASSIST — the steady state of every RUNNING H2S in the fleet.
            pytest.param(SimpleNamespace(ams_status_main=3), False, id="value3_assist_is_healthy"),
            pytest.param(SimpleNamespace(ams_status_main=4), False, id="value4"),
            pytest.param(SimpleNamespace(ams_status_main=9), False, id="value9"),
            pytest.param(None, False, id="no_state_at_all_startup_race"),
            pytest.param(SimpleNamespace(), False, id="a_state_without_the_field"),
        ],
    )
    def test_only_value_one_is_mid_filament_change(self, state, mid_change):
        assert ams_mid_filament_change(state) is mid_change

    def test_it_is_the_only_place_the_magic_value_is_compared(self, app_sources):
        """One origin (cross-cutting invariant 1). Every consumer asks the predicate; a
        direct comparison anywhere else is a second, drift-prone copy of a wire fact
        that took an incident to establish.

        Scans COMPARISON nodes, not source text: the prose that records the evidence
        (module docstrings, the constant's own comment) names the value on purpose and
        must stay readable.
        """
        offenders = []
        for module in app_sources.modules(exclude_names={"bambu_mqtt.py"}):
            for node in ast.walk(module.tree):
                if not isinstance(node, ast.Compare):
                    continue
                expr = ast.unparse(node)
                names_the_constant = "AMS_STATUS_FILAMENT_CHANGE" in expr
                against_literal_one = "ams_status_main" in expr and any(
                    isinstance(c, ast.Constant) and c.value == 1 for c in node.comparators
                )
                if names_the_constant or against_literal_one:
                    offenders.append(f"{module.rel_to_root}:{node.lineno}: {expr}")

        assert offenders == []


class TestAmsControlPublisher:
    """ONE publisher for ``ams_control``.

    The HMS modal used to carry its own inline copy of the frame, so the action
    vocabulary the printer's own error dialog needs (``done``, ``abort``) was
    reachable from there and NOT from the method that is supposed to own the command.
    """

    client_kwargs = {"serial": "03W-TEST", "connected": True}

    @pytest.mark.parametrize("action", ["resume", "reset", "pause", "done", "abort"])
    def test_the_whitelist_covers_every_action_the_hms_modal_dispatches(self, mqtt_client, action):
        assert mqtt_client.ams_control(action) is True

        assert _published_payloads(mqtt_client)[0] == {
            "print": {"command": "ams_control", "param": action, "sequence_id": "0"}
        }

    def test_an_unknown_action_is_refused(self, mqtt_client):
        assert mqtt_client.ams_control("detonate") is False

        assert _published_payloads(mqtt_client) == []

    def test_a_bare_call_carries_no_pushall(self, mqtt_client):
        """The recovery driver reads the AMS state machine off the next ~1 Hz push
        anyway; a full report per command would be pure wire cost."""
        mqtt_client.ams_control("resume")

        assert len(_published_payloads(mqtt_client)) == 1

    def test_request_pushall_appends_the_full_report_ask(self, mqtt_client):
        mqtt_client.ams_control("resume", request_pushall=True)

        assert _published_payloads(mqtt_client)[1] == {"pushing": {"command": "pushall", "sequence_id": "0"}}

    def test_disconnected_is_fail_loud(self, mqtt_client):
        mqtt_client.state.connected = False

        assert mqtt_client.ams_control("resume") is False
        assert _published_payloads(mqtt_client) == []


class TestTrayTarParse:
    """``tray_tar`` — the AMS's own target tray, part of the wire's answer to a motion
    command (``services/ams_command`` reads it as movement evidence). Recorded exactly as
    the wire spelled it, beside ``tray_now``, and never disambiguated."""

    client_kwargs = {"serial": "TEST_TRAY_TAR"}

    def test_unseen_is_none(self, mqtt_client):
        assert mqtt_client.state.tray_tar is None

    def test_parsed_from_the_ams_dict(self, mqtt_client):
        mqtt_client._process_message({"print": {"ams": {"tray_now": "3", "tray_tar": "3"}}})

        assert mqtt_client.state.tray_tar == 3

    def test_the_p1s_partial_update_carries_it_too(self, mqtt_client):
        mqtt_client._handle_ams_data({"tray_now": 255, "tray_tar": 6})

        assert mqtt_client.state.tray_tar == 6

    def test_an_absent_field_keeps_the_last_value(self, mqtt_client):
        mqtt_client._process_message({"print": {"ams": {"tray_now": "3", "tray_tar": "3"}}})
        mqtt_client._process_message({"print": {"ams": {"tray_now": "255"}}})

        assert mqtt_client.state.tray_tar == 3

    def test_an_unparseable_value_asserts_nothing(self, mqtt_client):
        mqtt_client._process_message({"print": {"ams": {"tray_now": "3", "tray_tar": "garbage"}}})

        assert mqtt_client.state.tray_tar is None

    def test_tray_now_is_untouched_by_it(self, mqtt_client):
        """No tray_now disambiguation reads tray_tar: a target that differs from the fed
        tray changes nothing about tray_now."""
        mqtt_client._process_message({"print": {"ams": {"tray_now": "2", "tray_tar": "7"}}})

        assert (mqtt_client.state.tray_now, mqtt_client.state.tray_tar) == (2, 7)

    def test_serialized_beside_tray_now(self, mqtt_client):
        from backend.app.services.printer_manager import printer_state_to_dict

        mqtt_client._process_message({"print": {"ams": {"tray_now": "3", "tray_tar": "3"}}})

        frame = printer_state_to_dict(mqtt_client.state)
        assert (frame["tray_now"], frame["tray_tar"]) == (3, 3)


class TestAmsMotionEcho:
    """The firmware's echo of a motion command is its own answer to it — invariant 14
    (every command ACK is consumed). It used to be dropped at DEBUG."""

    client_kwargs = {"serial": "TEST_ECHO"}

    @pytest.mark.parametrize("command", ["ams_change_filament", "ams_control"])
    def test_a_motion_echo_is_logged_at_info(self, mqtt_client, caplog, command):
        caplog.set_level(logging.DEBUG, logger="backend.app.services.bambu_mqtt")

        mqtt_client._process_message(
            {"print": {"command": command, "sequence_id": "42", "result": "fail", "reason": "busy"}}
        )

        echoes = [r for r in caplog.records if "echo:" in r.getMessage()]
        assert [(r.levelno, r.getMessage()) for r in echoes] == [
            (logging.INFO, f"[TEST_ECHO] {command} echo: sequence_id=42 result=fail reason=busy")
        ]

    def test_missing_fields_are_none(self, mqtt_client, caplog):
        caplog.set_level(logging.INFO, logger="backend.app.services.bambu_mqtt")

        mqtt_client._process_message({"print": {"command": "ams_change_filament"}})

        assert "[TEST_ECHO] ams_change_filament echo: sequence_id=None result=None reason=None" in [
            r.getMessage() for r in caplog.records
        ]

    def test_other_command_responses_stay_at_debug(self, mqtt_client, caplog):
        caplog.set_level(logging.DEBUG, logger="backend.app.services.bambu_mqtt")

        mqtt_client._process_message({"print": {"command": "push_status", "sequence_id": "1"}})

        responses = [r for r in caplog.records if "Received command response: push_status" in r.getMessage()]
        assert [r.levelno for r in responses] == [logging.DEBUG]
        assert not any("echo:" in r.getMessage() for r in caplog.records)
