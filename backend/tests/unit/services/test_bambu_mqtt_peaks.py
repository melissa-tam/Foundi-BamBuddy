"""``_peaks_reliable`` — may this client's layer/progress peaks be read as measurement?

The peaks live in PROCESS memory, so they only measure a job whose PRINT START this
client observed. A client born mid-print re-tracks from an unknown baseline and can
report zeros for a print that is physically three-quarters done — on 2026-08-29 six
such prints finished ``completed`` and were each classified "produced zero layers →
nothing on the plate": no plate gate, no eject, and the next unit dispatched onto the
finished part. These tests pin the flag's whole lifecycle: False at birth, True only on
an observed start edge, and False through every restart-recovery attach — RUNNING or
PAUSE — that the #1304 first-push guard routes to ``on_print_running_observed``.
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

    def test_a_pause_attach_and_its_resume_leave_it_false(self, mqtt_client):
        """Bambuddy started during a PAUSE (a plate-check hold, a runout): the attach
        happens on the PAUSE, and the resume after it is not a start — so nothing ever
        claims this client measured the job."""
        mqtt_client.on_print_start = lambda data: None
        mqtt_client.on_print_running_observed = lambda data: None

        mqtt_client._process_message(_print_push("PAUSE"))
        mqtt_client._process_message(_print_push("RUNNING"))

        assert mqtt_client._peaks_reliable is False

    def test_an_attach_after_an_observed_job_resets_it(self, mqtt_client):
        """The flag is otherwise never cleared, so a job first met PAUSEd after one this
        client DID watch start (a reconnect gap) would inherit that job's True."""
        mqtt_client.on_print_start = lambda data: None
        mqtt_client.on_print_running_observed = lambda data: None
        mqtt_client.on_print_complete = lambda data: None
        for state in ("IDLE", "PREPARE", "RUNNING", "FINISH"):
            mqtt_client._process_message(_print_push(state))
        assert mqtt_client._peaks_reliable is True

        mqtt_client._process_message(_print_push("PAUSE", gcode_file="/data/Metadata/plate_2.gcode"))

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

    def test_payload_carries_false_after_a_pause_attach_is_stopped(self, mqtt_client):
        """The stop of a job first met PAUSEd is a terminal now — and a fail-closed one."""
        payload: dict = {}
        mqtt_client.on_print_running_observed = lambda data: None
        mqtt_client.on_print_complete = lambda data: payload.update(data)

        mqtt_client._process_message(_print_push("PAUSE"))
        mqtt_client._process_message(_print_push("IDLE"))

        assert payload["status"] == "aborted"
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


class TestTheOnePeaksReader:
    """``job_peaks`` — THE read of how far the tracked job got, shared by the terminal
    payload and the recovery driver's LIVE deposit read (operator ruling 2026-09-29). It
    honours the stale-predecessor gate: for the seconds a new job heats and levels the
    firmware republishes the PREVIOUS job's percent (the live percent is never reset at a
    print start), and a paused layer-0 job must never read as having printed it — nor may a
    job that did print read as empty."""

    @staticmethod
    def _start(client, predecessor_percents=(60, 100), predecessor_layers=(100, 167)) -> None:
        client.on_print_start = lambda data: None
        client.on_print_running_observed = lambda data: None
        client.on_print_complete = lambda data: None
        client._process_message({"print": {"gcode_state": "RUNNING", "gcode_file": "prev.gcode.3mf"}})
        for layer in predecessor_layers:
            client._process_message({"print": {"layer_num": layer}})
        for percent in predecessor_percents:
            client._process_message({"print": {"mc_percent": percent}})
        client._process_message({"print": {"gcode_state": "FINISH"}})
        client._process_message({"print": {"gcode_state": "RUNNING", "gcode_file": RUNNING_FILE}})

    def _live_deposited(self, client) -> bool:
        from backend.app.services.plate_occupancy import DepositEvidence

        return DepositEvidence.live(client.job_peaks()).deposited

    def test_a_predecessors_republished_percent_never_makes_a_layer_0_job_deposited(self, mqtt_client):
        self._start(mqtt_client)
        mqtt_client._process_message({"print": {"mc_percent": 100, "layer_num": 167}})  # the stale republish
        mqtt_client._process_message({"print": {"gcode_state": "PAUSE"}})

        assert mqtt_client.state.progress == 100.0  # the live field still holds the predecessor's
        peaks = mqtt_client.job_peaks()
        assert (peaks.progress, peaks.layer_num, peaks.reliable) == (0.0, 0, True)
        assert self._live_deposited(mqtt_client) is False

    def test_a_job_mid_way_through_its_first_layer_reads_deposited(self, mqtt_client):
        self._start(mqtt_client)
        mqtt_client._process_message({"print": {"mc_percent": 0, "layer_num": 0}})  # this job's own
        mqtt_client._process_message({"print": {"layer_num": 1}})
        mqtt_client._process_message({"print": {"gcode_state": "PAUSE"}})

        peaks = mqtt_client.job_peaks()
        assert (peaks.last_layer_num, peaks.layer_num, peaks.peak_layer_num) == (0, 1, 1)
        assert self._live_deposited(mqtt_client) is True

    def test_its_own_percent_reads_deposited(self, mqtt_client):
        self._start(mqtt_client)
        mqtt_client._process_message({"print": {"mc_percent": 0}})
        mqtt_client._process_message({"print": {"mc_percent": 2}})

        assert mqtt_client.job_peaks().progress == 2.0
        assert self._live_deposited(mqtt_client) is True

    def test_the_terminal_payload_carries_the_readers_three_keys(self, mqtt_client):
        """One reader: the payload's ``last_progress`` / ``last_layer_num`` / ``peaks_reliable``
        are exactly ``job_peaks().terminal_fields()`` at the terminal."""
        payload: dict = {}
        self._start(mqtt_client)
        mqtt_client.on_print_complete = lambda data: payload.update(data)
        for layer in (1, 2, 3):
            mqtt_client._process_message({"print": {"layer_num": layer}})
        expected = {"last_progress": 0.0, "last_layer_num": 2, "peaks_reliable": True}
        assert mqtt_client.job_peaks().terminal_fields() == expected

        mqtt_client._process_message({"print": {"gcode_state": "FAILED"}})

        assert {key: payload[key] for key in expected} == expected
