"""Tests for HMS-action lookup and the MQTT dispatcher in execute_hms_action.

The lookup tests confirm the bundled catalog round-trips correctly. The
dispatcher tests are payload-shape contracts — wrong shape sends a bogus
command to the printer, which is the failure mode this PR is most exposed to,
so each HMSAction case publishes the expected JSON.

The dialog-button frames are BambuStudio's own (``DeviceErrorDialog.cpp`` picks the
command, ``DeviceManager.cpp`` builds it): "Problem solved, resume" is
``command_hms_resume`` (``resume`` + decimal ``err`` + ``param: "reserve"`` + ``job_id``),
"Ignore" is ``command_hms_ignore`` (the same with ``ignore``), "No reminder next time" is
``command_hms_idle_ignore(err, 0)``, and the plain resume is ``resume_print``'s frame.
"""

import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient, SentCommand
from backend.app.services.hms_actions import (
    HMSAction,
    get_actions_for_error_code,
)

# 0500_808C, the dominant production plate-check trip, as the 8-hex ``print_error`` the
# frontend echoes (``HMSError.full_code``) and as the DECIMAL string BambuStudio sends
# (``std::to_string(m_error_code)``): 0x0500808C == 83918988.
_PLATE_ERR_HEX = "0500808C"
_PLATE_ERR_DECIMAL = "83918988"


class TestActionLookup:
    def test_known_a1_error_returns_actions(self):
        # 03W is the A1 model code; 03008070 is "Heat the nozzle…" and Bambu's
        # catalog lists CHECK_ASSISTANT for it.
        actions = get_actions_for_error_code("03W", "03008070")
        assert isinstance(actions, list)
        assert len(actions) > 0
        for a in actions:
            assert isinstance(a, str)

    def test_unknown_device_returns_empty_list(self):
        assert get_actions_for_error_code("ZZZ", "03008070") == []

    def test_unknown_error_returns_empty_list(self):
        # Real model code, made-up error.
        assert get_actions_for_error_code("03W", "DEADBEEF") == []

    def test_underscore_form_does_not_match(self):
        # Caller is responsible for stripping the `_` before lookup. Guards
        # against accidental rewires that pass the underscore form.
        assert get_actions_for_error_code("03W", "0300_8070") == []

    def test_action_enum_values_are_uppercase_strings(self):
        # The catalog stores actions verbatim from BambuStudio. Drift here
        # silently breaks the dispatcher's `match` because StrEnum compares
        # by value.
        assert HMSAction.RESUME_PRINTING == "RESUME_PRINTING"
        assert HMSAction.CANCLE == "CANCLE"  # sic — kept from BambuStudio

    def test_the_plate_check_offers_problem_solved_and_ignore(self):
        """VENDOR EVIDENCE: the H2S catalog's buttons for the dominant plate-check trip
        are exactly the two the farm and the modal choose between."""
        assert get_actions_for_error_code("093", _PLATE_ERR_HEX) == ["IGNORE_RESUME", "PROBLEM_SOLVED_RESUME"]


class TestExecuteHmsActionDispatch:
    """Each case in the `match` publishes a specific JSON shape. These tests
    pin those shapes so silent regressions surface as test failures, not as
    a printer receiving a malformed command on a live print.
    """

    @pytest.fixture
    def client(self):
        c = BambuMQTTClient(
            ip_address="192.168.1.100",
            serial_number="03W-TEST",
            access_code="12345678",
        )
        c._client = MagicMock()
        c.state.connected = True
        return c

    def _published_commands(self, client):
        """Return the list of `print`/`system` command dicts from publish calls,
        skipping the `pushing.pushall` echoes that follow every action."""
        out = []
        for call in client._client.publish.call_args_list:
            _topic, payload = call.args[0], call.args[1]
            data = json.loads(payload)
            if "pushing" in data:
                continue
            out.append(data)
        return out

    def _all_payloads(self, client):
        return [json.loads(call.args[1]) for call in client._client.publish.call_args_list]

    def test_returns_none_when_disconnected(self, client):
        client.state.connected = False
        assert client.execute_hms_action(_PLATE_ERR_HEX, HMSAction.PROBLEM_SOLVED_RESUME) is None
        client._client.publish.assert_not_called()

    def test_returns_none_on_unknown_action(self, client):
        assert client.execute_hms_action("03008070", "DOES_NOT_EXIST") is None
        client._client.publish.assert_not_called()

    # -- "Problem solved, resume": the vendor resume+err frame -------------------------

    @pytest.mark.parametrize("action", [HMSAction.PROBLEM_SOLVED_RESUME, HMSAction.RESUME_PRINTING_PROBELM_SOLVED])
    def test_problem_solved_sends_the_vendor_resume_frame(self, client, action):
        """``command_hms_resume``: decimal ``err``, ``param: "reserve"``, the push's own
        ``job_id``, and a REAL sequence id recorded for the ACK read."""
        client.state.job_id = "771234"

        sent = client.execute_hms_action(_PLATE_ERR_HEX, action)

        assert self._published_commands(client) == [
            {
                "print": {
                    "command": "resume",
                    "err": _PLATE_ERR_DECIMAL,
                    "param": "reserve",
                    "job_id": "771234",
                    "sequence_id": "1",
                }
            }
        ]
        assert sent == SentCommand(command="resume", sequence_id="1")
        assert client.last_sent_sequence_id("resume") == "1"

    def test_the_callers_job_id_wins_over_the_push(self, client):
        client.state.job_id = "771234"

        client.execute_hms_action(_PLATE_ERR_HEX, HMSAction.PROBLEM_SOLVED_RESUME, job_id="task-42")

        assert self._published_commands(client)[0]["print"]["job_id"] == "task-42"

    def test_job_id_is_empty_when_the_printer_reports_none(self, client):
        """Studio's ``std::string`` default: an absent job id is ``""``, never omitted."""
        client.execute_hms_action(_PLATE_ERR_HEX, HMSAction.PROBLEM_SOLVED_RESUME)

        assert self._published_commands(client)[0]["print"]["job_id"] == ""

    def test_lowercase_hex_converts_to_the_same_decimal(self, client):
        client.execute_hms_action(_PLATE_ERR_HEX.lower(), HMSAction.PROBLEM_SOLVED_RESUME)

        assert self._published_commands(client)[0]["print"]["err"] == _PLATE_ERR_DECIMAL

    def test_the_foreign_objects_code_converts_too(self, client):
        client.execute_hms_action("0500806E", HMSAction.PROBLEM_SOLVED_RESUME)

        assert self._published_commands(client)[0]["print"]["err"] == "83918958"

    # -- "Ignore": the vendor ignore frame, never a plain resume ------------------------

    @pytest.mark.parametrize(
        "action",
        [HMSAction.IGNORE_RESUME, HMSAction.IGNORE_NO_REMINDER_NEXT_TIME, HMSAction.DONT_REMIND_NEXT_TIME],
    )
    def test_ignore_sends_the_vendor_ignore_frame(self, client, action):
        """``command_hms_ignore`` for all three buttons. The pre-2026-09-29 dispatcher sent
        a plain resume for "Ignore" on a paused print — which RE-CHECKS (upstream #1869),
        the opposite of what the button says."""
        client.state.state = "PAUSE"
        client.state.job_id = "771234"

        sent = client.execute_hms_action(_PLATE_ERR_HEX, action)

        assert self._published_commands(client) == [
            {
                "print": {
                    "command": "ignore",
                    "err": _PLATE_ERR_DECIMAL,
                    "param": "reserve",
                    "job_id": "771234",
                    "sequence_id": "1",
                }
            }
        ]
        assert sent == SentCommand(command="ignore", sequence_id="1")
        assert client.last_sent_sequence_id("ignore") == "1"

    @pytest.mark.parametrize("gcode_state", ["PAUSE", "RUNNING", "IDLE"])
    def test_ignore_never_publishes_a_resume(self, client, gcode_state):
        client.state.state = gcode_state

        client.execute_hms_action(_PLATE_ERR_HEX, HMSAction.IGNORE_RESUME)

        commands = [p["print"]["command"] for p in self._published_commands(client)]
        assert commands == ["ignore"]
        assert client.last_sent_sequence_id("resume") is None

    def test_no_reminder_sends_idle_ignore_type_zero(self, client):
        """``command_hms_idle_ignore(err, 0)`` — dismiss without resuming; no param, no job id."""
        sent = client.execute_hms_action(_PLATE_ERR_HEX, HMSAction.NO_REMINDER_NEXT_TIME)

        assert self._published_commands(client) == [
            {"print": {"command": "idle_ignore", "err": _PLATE_ERR_DECIMAL, "type": 0, "sequence_id": "1"}}
        ]
        assert sent == SentCommand(command="idle_ignore", sequence_id="1")

    # -- The err form: only the 8-hex print_error is accepted ---------------------------

    @pytest.mark.parametrize(
        "action",
        [
            HMSAction.PROBLEM_SOLVED_RESUME,
            HMSAction.RESUME_PRINTING_PROBELM_SOLVED,
            HMSAction.IGNORE_RESUME,
            HMSAction.IGNORE_NO_REMINDER_NEXT_TIME,
            HMSAction.DONT_REMIND_NEXT_TIME,
            HMSAction.NO_REMINDER_NEXT_TIME,
        ],
    )
    @pytest.mark.parametrize(
        "bad_err",
        [
            pytest.param("0C00030000020010", id="16_char_hms_code"),
            pytest.param("0500_808", id="underscore"),
            pytest.param("0x05008C", id="hex_prefix"),
            pytest.param("GGGGGGGG", id="non_hex"),
            pytest.param("", id="empty"),
        ],
    )
    def test_an_err_frame_refuses_anything_but_the_8_hex_code(self, client, caplog, action, bad_err):
        caplog.set_level(logging.WARNING, logger="backend.app.services.bambu_mqtt")

        assert client.execute_hms_action(bad_err, action) is None

        client._client.publish.assert_not_called()
        assert client.last_sent_sequence_id("resume") is None
        assert client.last_sent_sequence_id("ignore") is None
        assert client.last_sent_sequence_id("idle_ignore") is None
        assert any("refused" in r.getMessage() and "8-hex" in r.getMessage() for r in caplog.records)

    # -- The plain resume: resume_print, the one publisher of that frame ---------------

    @pytest.mark.parametrize(
        "action",
        [
            HMSAction.RESUME_PRINTING,
            HMSAction.RESUME_PRINTING_DEFECTS,
            HMSAction.FILAMENT_LOAD_RESUME,
            HMSAction.PROCEED,
            HMSAction.DBL_CHECK_RESUME,
        ],
    )
    def test_the_plain_resume_is_resume_prints_frame(self, client, action):
        """No err, no job id — and the 16-char code is harmless here, because this frame
        carries no ``err`` at all."""
        sent = client.execute_hms_action("0C00030000020010", action, job_id="task-42")

        assert self._all_payloads(client) == [
            {"print": {"command": "resume", "sequence_id": "1"}},
            {"pushing": {"command": "pushall", "sequence_id": "0"}},
        ]
        assert sent == SentCommand(command="resume", sequence_id="1")

    def test_the_plain_resume_goes_through_resume_print(self, client):
        with patch.object(BambuMQTTClient, "resume_print", return_value=True) as publisher:
            client.execute_hms_action("03008007", HMSAction.RESUME_PRINTING)

        publisher.assert_called_once_with()

    # -- Stop is not this dispatcher's -----------------------------------------------

    def test_stop_is_not_the_dialog_dispatchers_to_send(self, client):
        """CHANGED 2026-09-24: an operator choosing "Stop printing" in the HMS dialog is
        the operator's Stop, whose one owner is ``print_control.stop_as_operator`` (the
        durable stop request AND the stop). The route sends it there; this dispatcher owns
        no stop and sends nothing for it — sending one here went out WITHOUT the request,
        so the terminal read as a failure."""
        assert client.execute_hms_action("03008070", HMSAction.STOP_PRINTING, job_id="task-1") is None
        assert self._published_commands(client) == []

    def test_the_one_stop_publisher_is_plain_no_err_no_job_id(self, client):
        # The stop the operator verb sends. Same firmware silent-rejection class as
        # resume — the `err` variant was confirmed broken on H2D-1 (PAUSE → PAUSE); the
        # plain shape transitions to FAILED within ~2s.
        assert client.stop_print() is True
        cmds = self._published_commands(client)
        assert cmds == [{"print": {"command": "stop", "sequence_id": "0"}}]
        assert "err" not in cmds[0]["print"]
        assert "job_id" not in cmds[0]["print"]

    # -- The other buttons ------------------------------------------------------------

    def test_filament_extruded_sends_ams_done(self, client):
        sent = client.execute_hms_action("07008029", HMSAction.FILAMENT_EXTRUDED)
        cmds = self._published_commands(client)
        assert cmds[0] == {"print": {"command": "ams_control", "param": "done", "sequence_id": "1"}}
        assert sent == SentCommand(command="ams_control", sequence_id="1")

    def test_retry_sends_ams_resume(self, client):
        client.execute_hms_action("07008029", HMSAction.RETRY_FILAMENT_EXTRUDED)
        cmds = self._published_commands(client)
        assert cmds[0]["print"]["param"] == "resume"
        assert cmds[0]["print"]["command"] == "ams_control"

    def test_abort_sends_ams_abort(self, client):
        client.execute_hms_action("07008029", HMSAction.ABORT)
        cmds = self._published_commands(client)
        assert cmds[0]["print"]["param"] == "abort"

    def test_ok_button_sends_bare_clean_print_error(self, client):
        # Matches the existing `clear_hms_errors` shape — no `print_error` body
        # field, which the original PR mistakenly added.
        sent = client.execute_hms_action("03008070", HMSAction.OK_BUTTON)
        cmds = self._published_commands(client)
        assert cmds[0] == {"print": {"command": "clean_print_error", "sequence_id": "1"}}
        assert sent == SentCommand(command="clean_print_error", sequence_id="1")

    def test_dbl_check_ok_sends_clean_then_uiop_close_and_answers_sent(self, client):
        sent = client.execute_hms_action("03008070", HMSAction.DBL_CHECK_OK)
        cmds = self._published_commands(client)
        assert len(cmds) == 2
        assert cmds[0]["print"]["command"] == "clean_print_error"
        assert cmds[1]["system"]["command"] == "uiop"
        # `err` is the already-string short code, NOT `f"{x:08X}"` against a
        # str (which would TypeError on the old code path).
        assert cmds[1]["system"]["err"] == "03008070"
        # The close rides the SYSTEM topic, whose echo is never recorded as an ACK.
        assert sent == SentCommand(command="uiop", sequence_id=None)

    def test_uiop_close_uppercases_lowercase_input(self, client):
        # Frontend may send the short code in either case; we normalise.
        client.execute_hms_action("0300abcd", HMSAction.DBL_CHECK_OK)
        cmds = self._published_commands(client)
        assert cmds[1]["system"]["err"] == "0300ABCD"

    @pytest.mark.parametrize(
        ("action", "frame"),
        [
            (HMSAction.REFRESH_NOZZLE, {"command": "refresh_nozzle"}),
            (HMSAction.TURN_OFF_FIRE_ALARM, {"command": "buzzer_ctrl", "mode": 0}),
            (HMSAction.STOP_DRYING, {"command": "auto_stop_ams_dry"}),
            (HMSAction.DISABLE_PURIFICATION, {"command": "close_air_filt"}),
        ],
    )
    def test_the_device_buttons_send_a_sequenced_frame(self, client, action, frame):
        """A real id on every print-topic frame, so the route can read THIS send's ACK."""
        sent = client.execute_hms_action("03008070", action)

        assert self._published_commands(client) == [{"print": {**frame, "sequence_id": "1"}}]
        assert sent == SentCommand(command=frame["command"], sequence_id="1")
        assert client.last_sent_sequence_id(frame["command"]) == "1"

    @pytest.mark.parametrize(
        "action",
        [
            HMSAction.CHECK_ASSISTANT,
            HMSAction.JUMP_TO_LIVEVIEW,
            HMSAction.OK_JUMP_RACK,
            HMSAction.REMOVE_CLOSE_BTN,
            HMSAction.LOAD_VIRTUAL_TRAY,
            HMSAction.CANCLE,
            HMSAction.DBL_CHECK_CANCEL,
        ],
    )
    def test_ui_only_actions_publish_nothing(self, client, action):
        # These actions exist for parity with BambuStudio's modal but have no
        # MQTT counterpart — the printer's own screen drives them. Nothing went out,
        # so there is nothing to answer.
        assert client.execute_hms_action("03008070", action) is None
        client._client.publish.assert_not_called()

    @pytest.mark.parametrize(
        "action",
        [
            HMSAction.PROBLEM_SOLVED_RESUME,
            HMSAction.IGNORE_RESUME,
            HMSAction.NO_REMINDER_NEXT_TIME,
            HMSAction.RESUME_PRINTING,
            HMSAction.OK_BUTTON,
            HMSAction.FILAMENT_EXTRUDED,
            HMSAction.REFRESH_NOZZLE,
        ],
    )
    def test_every_command_is_followed_by_a_pushall(self, client, action):
        # The dispatcher pairs every command with a `pushing.pushall` echo so
        # the state stream refreshes on the next tick. Regression guard.
        client.execute_hms_action(_PLATE_ERR_HEX, action)
        payloads = self._all_payloads(client)
        assert "print" in payloads[0]
        assert payloads[-1] == {"pushing": {"command": "pushall", "sequence_id": "0"}}

    def test_no_print_frame_is_numbered_zero(self, client):
        """Invariant 14: with every send numbered ``"0"`` no ACK could name the send it
        answers."""
        for action in (
            HMSAction.PROBLEM_SOLVED_RESUME,
            HMSAction.IGNORE_RESUME,
            HMSAction.NO_REMINDER_NEXT_TIME,
            HMSAction.RESUME_PRINTING,
            HMSAction.OK_BUTTON,
            HMSAction.ABORT,
            HMSAction.REFRESH_NOZZLE,
        ):
            client.execute_hms_action(_PLATE_ERR_HEX, action)

        ids = [p["print"]["sequence_id"] for p in self._published_commands(client) if "print" in p]
        assert ids == ["1", "2", "3", "4", "5", "6", "7"]


class TestAmsControlBranchesDelegateToTheOnePublisher:
    """The modal's three ``ams_control`` branches used to publish through a closure
    that duplicated ``BambuMQTTClient.ams_control``'s frame. They now delegate, and
    the wire must not have moved a byte."""

    @pytest.fixture
    def client(self):
        c = BambuMQTTClient(ip_address="192.168.1.100", serial_number="03W-TEST", access_code="12345678")
        c._client = MagicMock()
        c.state.connected = True
        return c

    def _raw(self, client):
        return [call.args[1] for call in client._client.publish.call_args_list]

    @pytest.mark.parametrize(
        "action,param",
        [
            (HMSAction.FILAMENT_EXTRUDED, "done"),
            (HMSAction.RETRY_FILAMENT_EXTRUDED, "resume"),
            (HMSAction.ABORT, "abort"),
        ],
    )
    def test_the_published_bytes_are_unchanged(self, client, action, param):
        client.execute_hms_action("07008029", action)
        expected = json.dumps({"print": {"command": "ams_control", "param": param, "sequence_id": "1"}})
        assert self._raw(client)[0] == expected

    @pytest.mark.parametrize(
        "action,param",
        [
            (HMSAction.FILAMENT_EXTRUDED, "done"),
            (HMSAction.RETRY_FILAMENT_EXTRUDED, "resume"),
            (HMSAction.ABORT, "abort"),
        ],
    )
    def test_it_goes_through_the_method_and_asks_for_a_pushall(self, client, action, param):
        """The modal's underlying status query refreshes off that full report — which
        is why the branch asks for one where a bare ``ams_control`` does not."""
        with patch.object(BambuMQTTClient, "ams_control", return_value=True) as pub:
            client.execute_hms_action("07008029", action)
        pub.assert_called_once_with(param, request_pushall=True)
