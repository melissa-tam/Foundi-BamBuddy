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


def _push(client, **fields) -> None:
    client._process_message({"print": fields})


class TestThePlateCheckLayerReading:
    """The per-job layer the plate-check episode reads for its pass (RUNNING at layer >= 1 of
    THIS job) — ``pause_recovery._job_layer``, read off the client's ONE peaks reader
    (``job_peaks``), so it has the terminal payload's source and reliability flag: a
    predecessor's republish or a restart baseline is never a reading of this job."""

    @staticmethod
    def _layer(client) -> int | None:
        from backend.app.services.pause_recovery import _job_layer

        return _job_layer(client)

    def test_a_restart_attach_reads_no_measurement(self, mqtt_client):
        """Born mid-job: whatever layer the printer reports is a baseline, not a measurement."""
        mqtt_client.on_print_running_observed = lambda data: None

        _push(mqtt_client, gcode_state="PAUSE", gcode_file=RUNNING_FILE)
        _push(mqtt_client, gcode_state="RUNNING", layer_num=5)

        assert mqtt_client.state.layer_num == 5
        assert mqtt_client.job_peaks().reliable is False
        assert self._layer(mqtt_client) is None

    def test_a_predecessors_republished_layer_is_not_this_jobs(self, mqtt_client):
        """The firmware keeps republishing the previous job's final layer while this one heats,
        levels and pauses at its plate check: the reading stays 0 until this job's own layer."""
        mqtt_client.on_print_start = lambda data: None
        mqtt_client.on_print_running_observed = lambda data: None
        mqtt_client.on_print_complete = lambda data: None
        # The predecessor, first met RUNNING (the #1304 guard: an attach), ends at layer 167.
        _push(mqtt_client, gcode_state="RUNNING", gcode_file="/data/Metadata/prev.gcode")
        for layer in (100, 167):
            _push(mqtt_client, layer_num=layer)
        _push(mqtt_client, gcode_state="FINISH")

        _push(mqtt_client, gcode_state="RUNNING", gcode_file=RUNNING_FILE)  # this job starts
        _push(mqtt_client, layer_num=167)  # the stale republish
        _push(mqtt_client, gcode_state="PAUSE", layer_num=167)  # its plate check

        assert self._layer(mqtt_client) == 0

        _push(mqtt_client, gcode_state="RUNNING", layer_num=0)  # this job's own first reading
        assert self._layer(mqtt_client) == 0
        _push(mqtt_client, layer_num=1)

        assert self._layer(mqtt_client) == 1


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

    def test_the_terminal_payload_carries_the_readers_keys(self, mqtt_client):
        """One reader: the payload's ``last_progress`` / ``last_layer_num`` / ``peaks_reliable``
        / ``first_unfed_layer`` are exactly ``job_peaks().terminal_fields()`` at the terminal."""
        payload: dict = {}
        self._start(mqtt_client)
        mqtt_client.on_print_complete = lambda data: payload.update(data)
        for layer in (1, 2, 3):
            mqtt_client._process_message({"print": {"layer_num": layer}})
        expected = {"last_progress": 0.0, "last_layer_num": 2, "peaks_reliable": True, "first_unfed_layer": None}
        assert mqtt_client.job_peaks().terminal_fields() == expected

        mqtt_client._process_message({"print": {"gcode_state": "FAILED"}})

        assert {key: payload[key] for key in expected} == expected


# --- K2: the first layer printed with nothing fed -------------------------------------
#
# ``JobPeaks.first_unfed_layer`` — the layer L at which a run of RUNNING pushes began with
# the ACTIVE extruder empty (``tray_fields.toolhead_feed``) and no filament change in flight
# (``ams_mid_filament_change``), stamped only once ``layer_num`` ADVANCES past L while still
# empty: a layer printed with nothing fed, an event and never a time. 011-H2S 2026-10-09: an
# accepted pull-back ran on its own while the print sat PAUSED, a resume then printed from
# layer 93 to the end with ``tray_now=255``, and the FINISH was recorded completed.

_TOTAL = 150
_FED = "2"
_EMPTY = "255"


def _started(client, *, total: int = _TOTAL) -> None:
    """A job this client watched START (past the #1304 first-push guard), fed from slot 2.

    The slicer total lands on the push AFTER the start: the start block zeroes
    ``total_layers`` once the field handlers have run (#1771), and the firmware repeats the
    field on every report."""
    client.on_print_start = lambda data: None
    client.on_print_running_observed = lambda data: None
    client.on_print_complete = lambda data: None
    client._previous_gcode_state = "IDLE"
    client._was_running = False
    _push(client, gcode_state="RUNNING", gcode_file=RUNNING_FILE, ams={"tray_now": _FED})
    _push(client, total_layer_num=total)
    assert client.state.total_layers == total


def _layer(client, layer: int, tray_now: str, **fields) -> None:
    _push(client, layer_num=layer, ams={"tray_now": tray_now}, **fields)


def _unfed(client) -> int | None:
    return client.job_peaks().first_unfed_layer


class TestFirstUnfedLayer:
    def test_the_011_shape_stamps_the_layer_the_air_began_at(self, mqtt_client):
        """Fed to layer 92, empty from 93 to the end: layer 93 is the first printed with
        nothing fed, and the FINISH payload says so."""
        payload: dict = {}
        _started(mqtt_client)
        mqtt_client.on_print_complete = lambda data: payload.update(data)
        for layer in range(1, 93):
            _layer(mqtt_client, layer, _FED)
        assert mqtt_client.state.tray_now == 2
        assert _unfed(mqtt_client) is None

        _layer(mqtt_client, 93, _EMPTY)
        assert mqtt_client.state.tray_now == 255
        assert _unfed(mqtt_client) is None  # nothing printed empty YET: the layer has not advanced

        for layer in range(94, _TOTAL + 1):
            _layer(mqtt_client, layer, _EMPTY)
        assert _unfed(mqtt_client) == 93

        _push(mqtt_client, gcode_state="FINISH")
        assert payload["status"] == "completed"
        assert payload["first_unfed_layer"] == 93

    def test_a_sub_layer_empty_blip_refilled_before_the_layer_advances_never_stamps(self, mqtt_client):
        _started(mqtt_client)
        _layer(mqtt_client, 50, _FED)
        _layer(mqtt_client, 50, _EMPTY)
        _layer(mqtt_client, 50, _FED)
        _layer(mqtt_client, 51, _FED)

        assert _unfed(mqtt_client) is None

    def test_a_pause_refill_and_resume_inside_one_layer_never_stamps(self, mqtt_client):
        """T4's shape: the farm pauses within a push, refills and resumes. The layer that
        was entered empty was finished fed."""
        _started(mqtt_client)
        _layer(mqtt_client, 50, _EMPTY)
        _push(mqtt_client, gcode_state="PAUSE")
        _push(mqtt_client, ams={"tray_now": _FED})
        _push(mqtt_client, gcode_state="RUNNING")
        _layer(mqtt_client, 51, _FED)

        assert _unfed(mqtt_client) is None

    def test_empty_during_a_filament_change_never_stamps(self, mqtt_client):
        """Mid-change (``ams_status_main == 1``) the AMS owns the path: 255 there is the change
        in flight, not air."""
        _started(mqtt_client)
        mid_change = 0x0105  # main 1, sub 5
        _layer(mqtt_client, 50, _EMPTY, ams_status=mid_change)
        assert mqtt_client.state.ams_status_main == 1
        _layer(mqtt_client, 51, _EMPTY, ams_status=mid_change)
        _layer(mqtt_client, 51, _FED, ams_status=0x0300)

        assert _unfed(mqtt_client) is None

    def test_the_end_of_print_retract_at_the_last_layer_never_stamps(self, mqtt_client):
        payload: dict = {}
        _started(mqtt_client)
        mqtt_client.on_print_complete = lambda data: payload.update(data)
        _layer(mqtt_client, _TOTAL - 1, _FED)
        _layer(mqtt_client, _TOTAL, _FED)
        _layer(mqtt_client, _TOTAL, _EMPTY)
        _push(mqtt_client, gcode_state="FINISH")

        assert payload["first_unfed_layer"] is None

    def test_an_unknown_total_stamps_nothing(self, mqtt_client):
        """L must be below ``total_layers``: with no slicer total read yet nothing can show
        the layer was not the last one, and a false stamp fails a finished print."""
        _started(mqtt_client, total=0)
        _layer(mqtt_client, 40, _EMPTY)
        _layer(mqtt_client, 41, _EMPTY)

        assert _unfed(mqtt_client) is None

    def test_an_empty_layer_0_does_not_stamp_the_print_start(self, mqtt_client):
        """Before the first layer the start block loads the filament: a 255 there is the
        load still to come."""
        _started(mqtt_client)
        _layer(mqtt_client, 0, _EMPTY)
        _layer(mqtt_client, 0, _FED)
        _layer(mqtt_client, 1, _FED)
        _layer(mqtt_client, 2, _FED)

        assert _unfed(mqtt_client) is None

    def test_a_first_layer_printed_with_nothing_fed_stamps_layer_1(self, mqtt_client):
        """Empty from the start block on: layer 0 is no printed layer, so the run is read
        from the first one, layer 1, finished with nothing fed."""
        _started(mqtt_client)
        _layer(mqtt_client, 0, _EMPTY)
        _layer(mqtt_client, 1, _EMPTY)
        assert _unfed(mqtt_client) is None
        _layer(mqtt_client, 2, _EMPTY)

        assert _unfed(mqtt_client) == 1

    def test_the_lowest_layer_is_kept(self, mqtt_client):
        _started(mqtt_client)
        _layer(mqtt_client, 40, _EMPTY)
        _layer(mqtt_client, 41, _EMPTY)
        _layer(mqtt_client, 42, _FED)
        _layer(mqtt_client, 120, _EMPTY)
        _layer(mqtt_client, 121, _EMPTY)

        assert _unfed(mqtt_client) == 40

    def test_an_attach_mid_air_stamps_the_first_observed_advancing_empty_layer(self, mqtt_client):
        """NOT gated on ``reliable``: layer numbers are absolute, so a layer printed empty
        after an attach is a real measurement of THIS job."""
        mqtt_client.on_print_running_observed = lambda data: None
        mqtt_client.on_print_complete = lambda data: None
        _push(
            mqtt_client,
            gcode_state="RUNNING",
            gcode_file=RUNNING_FILE,
            total_layer_num=_TOTAL,
            layer_num=80,
            ams={"tray_now": _EMPTY},
        )
        _layer(mqtt_client, 81, _EMPTY)

        assert mqtt_client.job_peaks().reliable is False
        assert _unfed(mqtt_client) == 80

    def test_a_new_job_starts_unstamped(self, mqtt_client):
        _started(mqtt_client)
        _layer(mqtt_client, 9, _EMPTY)
        _layer(mqtt_client, 10, _EMPTY)
        _push(mqtt_client, gcode_state="FINISH")
        assert _unfed(mqtt_client) == 9

        _push(mqtt_client, gcode_state="RUNNING", gcode_file="/data/Metadata/plate_2.gcode", ams={"tray_now": _FED})

        assert _unfed(mqtt_client) is None

    def test_an_attach_after_a_watched_job_clears_the_predecessors_stamp(self, mqtt_client):
        """The reconnect-gap attach clears the predecessor's peaks, its unfed layer too."""
        _started(mqtt_client)
        _layer(mqtt_client, 9, _EMPTY)
        _layer(mqtt_client, 10, _EMPTY)
        _push(mqtt_client, gcode_state="FINISH")
        assert _unfed(mqtt_client) == 9

        _push(mqtt_client, gcode_state="PAUSE", gcode_file="/data/Metadata/plate_2.gcode")

        assert mqtt_client.job_peaks().reliable is False
        assert _unfed(mqtt_client) is None

    def test_a_run_open_at_the_job_boundary_does_not_carry_into_the_next_job(self, mqtt_client):
        """An empty run still open when the next job starts (no terminal in between) must not
        stamp that job's first layer advance."""
        _started(mqtt_client)
        _layer(mqtt_client, 30, _EMPTY)  # a run open at layer 30, never advanced
        _push(
            mqtt_client,
            gcode_state="RUNNING",
            gcode_file="/data/Metadata/plate_2.gcode",
            layer_num=0,
            ams={"tray_now": _EMPTY},
        )
        _push(mqtt_client, total_layer_num=_TOTAL)
        _layer(mqtt_client, 1, _EMPTY)
        _layer(mqtt_client, 2, _EMPTY)

        assert _unfed(mqtt_client) == 1  # the new job's own first empty layer, never the old run's

    def test_the_active_nozzle_decides_on_a_dual_nozzle_machine(self, mqtt_client):
        """The H2C hazard: the RIGHT nozzle (0) still holds slot 5 while the LEFT (1), the
        active one, is empty. Read through ``toolhead_feed``, that is air."""
        _started(mqtt_client)
        dual = {
            "extruder": {
                "state": 0x100,  # bit 8 = 1: the LEFT extruder is active
                "info": [{"id": 0, "snow": (1 << 8) | 1}, {"id": 1, "snow": 0xFFFF}],
            }
        }
        _push(mqtt_client, layer_num=60, device=dual)
        assert mqtt_client.state.active_extruder == 1
        assert mqtt_client.state.h2d_extruder_snow == {0: 5, 1: 255}
        _push(mqtt_client, layer_num=61, device=dual)

        assert _unfed(mqtt_client) == 60

    def test_the_terminal_fields_carry_the_key(self, mqtt_client):
        """Capsule C reads exactly ``first_unfed_layer`` off the terminal payload."""
        _started(mqtt_client)
        assert mqtt_client.job_peaks().terminal_fields()["first_unfed_layer"] is None
        _layer(mqtt_client, 12, _EMPTY)
        _layer(mqtt_client, 13, _EMPTY)

        assert mqtt_client.job_peaks().terminal_fields()["first_unfed_layer"] == 12
