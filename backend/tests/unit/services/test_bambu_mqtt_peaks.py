"""``_peaks_reliable`` — may this client's layer/progress peaks be read as measurement?

The peaks live in PROCESS memory, so they only measure a job whose PRINT START this
client observed. A client born mid-print re-tracks from an unknown baseline and can
report zeros for a print that is physically three-quarters done — on 2026-08-29 six
such prints finished ``completed`` and were each classified "produced zero layers →
nothing on the plate": no plate gate, no eject, and the next unit dispatched onto the
finished part. These tests pin the flag's whole lifecycle: False at birth, True only on
an observed start edge, and False through the restart-recovery attach that the #1304
first-push guard routes to ``on_print_running_observed``.
"""

from __future__ import annotations

import pytest

RUNNING_FILE = "/data/Metadata/plate_1.gcode"


@pytest.fixture
def mqtt_client():
    from backend.app.services.bambu_mqtt import BambuMQTTClient

    return BambuMQTTClient(ip_address="192.168.1.100", serial_number="TEST123", access_code="12345678")


def _print_push(state: str, gcode_file: str = RUNNING_FILE, subtask_name: str = "Unit_1") -> dict:
    return {"print": {"gcode_state": state, "gcode_file": gcode_file, "subtask_name": subtask_name}}


class TestPeaksReliableLifecycle:
    def test_false_at_client_birth(self, mqtt_client):
        """Fail closed: a client that has observed nothing has measured nothing."""
        assert mqtt_client._peaks_reliable is False

    def test_observed_print_start_arms_it(self, mqtt_client):
        mqtt_client.on_print_start = lambda data: None
        mqtt_client._previous_gcode_state = "IDLE"  # past the #1304 first-push guard
        mqtt_client._was_running = False

        mqtt_client._process_message(_print_push("RUNNING"))

        assert mqtt_client._peaks_reliable is True

    def test_file_change_while_running_arms_it(self, mqtt_client):
        """A new file under a still-RUNNING state is a print start by another door — the
        peaks are reset there too, so they become measurement there too."""
        mqtt_client.on_print_start = lambda data: None
        mqtt_client._previous_gcode_state = "RUNNING"
        mqtt_client._previous_gcode_file = "/data/Metadata/plate_9.gcode"
        mqtt_client._was_running = True

        mqtt_client._process_message(_print_push("RUNNING"))

        assert mqtt_client._peaks_reliable is True

    def test_restart_recovery_attach_leaves_it_false(self, mqtt_client):
        """Bambuddy started mid-print: the #1304 guard suppresses ``on_print_start`` and
        the client adopts the job through ``on_print_running_observed``. It never saw the
        start, so its peaks are not evidence — the 2026-08-29 shape."""
        start_calls: list[dict] = []
        observed_calls: list[dict] = []
        mqtt_client.on_print_start = lambda data: start_calls.append(data)
        mqtt_client.on_print_running_observed = lambda data: observed_calls.append(data)
        mqtt_client._previous_gcode_state = None
        mqtt_client._was_running = False

        mqtt_client._process_message(_print_push("RUNNING"))

        assert start_calls == []
        assert len(observed_calls) == 1
        assert mqtt_client._peaks_reliable is False

    def test_non_running_pushes_do_not_arm_it(self, mqtt_client):
        mqtt_client._previous_gcode_state = "IDLE"
        mqtt_client._process_message(_print_push("PREPARE"))
        assert mqtt_client._peaks_reliable is False


class TestPeaksReliableInCompletionPayload:
    def test_payload_carries_true_after_an_observed_start(self, mqtt_client):
        payload: dict = {}
        mqtt_client.on_print_start = lambda data: None
        mqtt_client.on_print_complete = lambda data: payload.update(data)
        mqtt_client._previous_gcode_state = "IDLE"
        mqtt_client._was_running = False

        mqtt_client._process_message(_print_push("RUNNING"))
        mqtt_client._process_message(_print_push("FINISH"))

        assert payload["peaks_reliable"] is True
        assert "last_layer_num" in payload and "last_progress" in payload

    def test_payload_carries_false_after_a_restart_recovery_attach(self, mqtt_client):
        """The terminal that cost the farm six plates: a genuine ``completed`` whose
        peaks are an artefact of the restart. The payload must say so."""
        payload: dict = {}
        mqtt_client.on_print_start = lambda data: None
        mqtt_client.on_print_running_observed = lambda data: None
        mqtt_client.on_print_complete = lambda data: payload.update(data)
        mqtt_client._previous_gcode_state = None
        mqtt_client._was_running = False

        mqtt_client._process_message(_print_push("RUNNING"))
        mqtt_client._process_message(_print_push("FINISH"))

        assert payload["status"] == "completed"
        assert payload["peaks_reliable"] is False

    def test_deposit_evidence_reads_the_payload_key(self, mqtt_client):
        """The consumer contract: an unreliable-peaks terminal is deposit-bearing even
        with zero layers and zero progress."""
        from backend.app.services.plate_occupancy import DepositEvidence

        payload: dict = {}
        mqtt_client.on_print_start = lambda data: None
        mqtt_client.on_print_running_observed = lambda data: None
        mqtt_client.on_print_complete = lambda data: payload.update(data)
        mqtt_client._previous_gcode_state = None
        mqtt_client._was_running = False

        mqtt_client._process_message(_print_push("RUNNING"))
        mqtt_client._process_message(_print_push("FAILED"))

        evidence = DepositEvidence.from_terminal_payload(payload, is_dry_run=False)
        assert evidence.peaks_reliable is False
        assert evidence.last_layer_num == 0
        assert evidence.deposited is True
