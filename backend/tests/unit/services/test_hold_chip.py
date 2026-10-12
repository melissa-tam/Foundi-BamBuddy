"""``services/hold_chip`` — THE printer card's hold chip: which hold it names, in which tone, with which
words (2026-10-11). The verdict moved here from the browser (``utils/incidentChip.ts``, deleted), whose
precedence pins are ported below; the new toolhead states are the 001-H2S incident 574 fix: a
``toolhead_refill`` hold over a toolhead the wire reports FED is never "Toolhead empty" — it reads
"Resuming" while the farm resumes it, "Resume refused" with the printer's words when the printer said
no, and "Toolhead fed" + "Press Resume." when the farm will not. R29 (2026-10-12) scopes "Resuming" /
"Resume refused" to the kinds the farm resumes outside a driver with none live (a live jam reads
"Recovering"), reads a runout's re-owed resume as "Resuming" until ``resume_spent`` gives up, and names
an air stop the printer did not take.

The precedence, first match wins:

1. the farm's refill state (``printer_incidents.refill_state``, read only over an empty / unread
   toolhead) → the toolhead variant, over ANY row;
2. the open row the store ranks first (own-surface kinds skipped);
3. a paused print on an empty toolhead with nothing open → only for a T3 verdict a person should see.
"""

from __future__ import annotations

import itertools
import pathlib
import re
from types import SimpleNamespace

import pytest

from backend.app.models.printer_incident import (
    KIND_JAM,
    KIND_PHYSICAL,
    KIND_PLATE_VISION,
    KIND_POWER_LOSS,
    KIND_RUNOUT,
    KIND_SERVICE_HOLD,
    KIND_TOOLHEAD_REFILL,
    KIND_Z_REFERENCE_LOST,
    STATUS_ESCALATED,
    STATUS_RECOVERING,
)
from backend.app.services import hold_chip, printer_incidents, refill_verdict
from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.feed_state import FeedPhase, FeedReading

_PID = 7
_JOB = "674664134"
_TOOLHEAD_EMPTY = "printers.incident.toolhead_refill"

# --- the scripted printer ---------------------------------------------------------------------------


def _state(tray_now: int, *, live: str = "PAUSE", layer: int = 85, fresh: bool = True) -> PrinterState:
    state = PrinterState()
    state.connected = True
    state.state = live
    state.tray_now = tray_now
    state.subtask_id = _JOB
    state.layer_num = layer
    state.total_layers = 167
    if fresh:
        state.report_epoch = state.connection_epoch
    return state


def _feed(phase: FeedPhase, feeder: int | None = None) -> FeedReading:
    """A feed state reading in ``phase`` (the chip reads the phase and the effective feeder)."""
    return FeedReading(
        phase=phase,
        active_extruder=0,
        feeder=feeder,
        cause=None,
        departed=None,
        tail=feeder if phase is FeedPhase.TAIL else None,
        held_slot=None,
        extruders=(),
        feeder_log=(),
        last_loaded=feeder if feeder is not None else -1,
        first_unfed_layer=None,
        roll_drained=None,
        load_landed=None,
        auto_switched=None,
        air_began=None,
    )


_FED = _feed(FeedPhase.FED, 1)
_EMPTY_PAUSED = _feed(FeedPhase.EMPTY_PAUSED)


def _action(
    command: str, answer: str | None, *, reason: str | None = None, unsent: bool = False, action_id: int = 41
) -> dict:
    return {
        "action_id": action_id,
        "command": command,
        "target": None,
        "answer": answer,
        "sent_at": "2026-10-11T03:43:00",
        "answered_at": None if answer is None else "2026-10-11T03:43:05",
        "step_reading": None,
        "reason": reason,
        "unsent": unsent,
    }


def _row(
    kind: str, status: str = STATUS_ESCALATED, *, row_id: int = 574, slot_desc: str | None = None, **extra
) -> dict:
    """An open row as the store's projection carries it."""
    return {
        "id": row_id,
        "kind": kind,
        "code": "",
        "status": status,
        "job_id": _JOB,
        "slot_desc": slot_desc,
        "farm_actions": [],
        "pending_command": None,
        "failed_command": None,
        **extra,
    }


def _held(*rows: dict) -> None:
    printer_incidents._open_cache[_PID] = {row["id"]: row for row in rows}  # noqa: SLF001


def _chip(state: PrinterState | None, feed: FeedReading | None) -> dict | None:
    return hold_chip.hold_chip_payload(_PID, state, feed)


def _keys(chip: dict | None) -> list[str]:
    assert chip is not None
    return [copy["key"] for copy in chip["tooltip"]]


@pytest.fixture(autouse=True)
def _clean_store(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    printer_incidents._reset_state()  # noqa: SLF001
    yield
    printer_incidents._reset_state()  # noqa: SLF001


def _verdict(monkeypatch, reason: str) -> None:
    """The T3 verdict (the refill verdict leaf, pinned in its own tests) as rule 3 reads it."""
    monkeypatch.setattr(hold_chip.refill_verdict, "refill_owed", lambda *_a, **_k: SimpleNamespace(reason=reason))


# --- rule 1: the farm's refill ------------------------------------------------------------------------


class TestTheRefillState:
    def test_a_failed_refill_is_named_over_the_jam_row_it_ran_under(self):
        _held(
            _row(
                KIND_JAM,
                slot_desc="AMS A slot 3",
                failed_command={"incident_id": 574, "seq": 3, "name": "load", "target": 0, "answer": "no_movement"},
            )
        )

        chip = _chip(_state(255), _EMPTY_PAUSED)

        assert (chip["variant"], chip["tone"], chip["label"]["key"]) == ("toolhead", "held", _TOOLHEAD_EMPTY)
        assert _keys(chip) == [
            "printers.toolhead.loadFailed",
            "printers.toolhead.answer.no_movement",
            "printers.incidentAction.toolhead_refill",
        ]
        assert chip["tooltip"][0]["values"] == {"slot": "AMS A slot 1"}
        # The jam row's slot belongs to the jam chip, not to this one.
        assert chip["qualifier"] is None

    @pytest.mark.parametrize("kind", [KIND_JAM, KIND_PHYSICAL, KIND_RUNOUT])
    def test_a_farm_load_in_flight_is_named_over_the_row(self, kind, monkeypatch):
        _held(
            _row(
                kind,
                STATUS_RECOVERING,
                pending_command={"incident_id": 574, "action_id": 9, "seq": 2, "name": "load", "target": 0},
            )
        )
        monkeypatch.setattr(printer_incidents, "driver_live", lambda _pid: True)

        chip = _chip(_state(255), _EMPTY_PAUSED)

        assert (chip["variant"], chip["tone"]) == ("toolhead", "acting")
        assert _keys(chip) == ["printers.toolhead.loading"]
        assert chip["tooltip"][0]["values"] == {"slot": "AMS A slot 1"}

    @pytest.mark.parametrize(
        ("failed", "keys"),
        [
            pytest.param(
                {"name": "load", "target": None, "answer": None},
                ["printers.toolhead.loadFailedAnySlot", "printers.incidentAction.toolhead_refill"],
                id="a_failed_load_naming_no_slot_and_no_answer",
            ),
            pytest.param(
                {"name": "load", "target": 5, "answer": "acted"},
                [
                    "printers.toolhead.loadFailed",
                    "printers.toolhead.answer.loadActed",
                    "printers.incidentAction.toolhead_refill",
                ],
                id="a_load_that_moved_without_finishing",
            ),
            pytest.param(
                {"name": "unload", "target": None, "answer": "acted"},
                [
                    "printers.toolhead.unloadFailed",
                    "printers.toolhead.answer.unloadActed",
                    "printers.incidentAction.toolhead_refill",
                ],
                id="an_unload_never_reads_as_a_load",
            ),
        ],
    )
    def test_the_failed_step_names_itself(self, failed, keys):
        _held(
            _row(
                KIND_TOOLHEAD_REFILL, slot_desc="AMS B slot 2", failed_command={"incident_id": 574, "seq": 1, **failed}
            )
        )

        assert _keys(_chip(_state(255), _EMPTY_PAUSED)) == keys

    def test_a_failed_refill_is_history_once_the_toolhead_reads_fed(self):
        _held(
            _row(
                KIND_JAM,
                failed_command={"incident_id": 574, "seq": 3, "name": "load", "target": 0, "answer": "no_movement"},
            )
        )

        chip = _chip(_state(1), _FED)

        assert chip["label"]["key"] == "printers.incident.jam"


# --- rule 2: the open row -------------------------------------------------------------------------


class TestTheOpenRow:
    @pytest.mark.parametrize(
        "kind", [KIND_PLATE_VISION, KIND_POWER_LOSS, KIND_Z_REFERENCE_LOST, KIND_JAM, KIND_RUNOUT, KIND_PHYSICAL]
    )
    def test_a_row_keeps_its_own_chip_on_an_empty_toolhead_even_when_a_resume_is_owed(self, kind, monkeypatch):
        _verdict(monkeypatch, "owed")
        _held(_row(kind))

        chip = _chip(_state(255), _EMPTY_PAUSED)

        assert (chip["variant"], chip["tone"], chip["label"]["key"]) == (
            "incident",
            "held",
            f"printers.incident.{kind}",
        )
        assert _keys(chip) == [f"printers.incidentAction.{kind}"]

    def test_the_plate_check_names_no_exit_while_the_farm_still_acts(self):
        _held(_row(KIND_PLATE_VISION, STATUS_RECOVERING))

        chip = _chip(_state(1), _FED)

        assert (chip["tone"], chip["label"]["key"]) == ("acting", "printers.incident.recovering")
        assert _keys(chip) == ["printers.incidentRecoveringAction.plate_vision"]

    @pytest.mark.parametrize("status", [STATUS_RECOVERING, STATUS_ESCALATED])
    def test_a_runout_keeps_its_slot_qualified_instruction_in_both_states(self, status):
        _held(_row(KIND_RUNOUT, status, slot_desc="AMS A slot 2"))

        chip = _chip(_state(255), _EMPTY_PAUSED)

        assert _keys(chip) == ["printers.incidentAction.runout"]
        assert chip["qualifier"] == "AMS A slot 2"

    def test_maintenance_mode_never_takes_the_chip(self):
        _held(_row(KIND_SERVICE_HOLD))

        assert _chip(_state(1), _FED) is None


# --- rule 2, the toolhead hold: never "Toolhead empty" over a fed toolhead -------------------------


class TestTheToolheadHold:
    def test_574_a_hold_over_a_fed_toolhead_reads_resuming_with_the_feeding_slot(self):
        """001-H2S incident 574 after the deploy: the escalated hold, the print PAUSEd on its job, the
        firmware's backup switch fed slot 2 — the farm resumes it (the spawner's own question)."""
        _held(_row(KIND_TOOLHEAD_REFILL))

        chip = _chip(_state(1), _FED)

        assert (chip["variant"], chip["tone"], chip["label"]["key"]) == (
            "toolhead",
            "acting",
            "printers.toolhead.label.resuming",
        )
        assert chip["tooltip"] == [{"key": "printers.toolhead.fedFrom", "values": {"slot": "AMS A slot 2"}}]

    def test_an_outstanding_farm_resume_names_when_it_went_out(self):
        _held(_row(KIND_TOOLHEAD_REFILL, farm_actions=[_action("resume", None)]))

        chip = _chip(_state(1), _FED)

        assert (chip["tone"], chip["label"]["key"]) == ("acting", "printers.toolhead.label.resuming")
        assert chip["tooltip"] == [
            {"key": "printers.toolhead.fedFrom", "values": {"slot": "AMS A slot 2"}},
            {"key": "printers.toolhead.resumeSent", "values": {"time": "03:43"}},
        ]

    def test_a_resume_the_printer_refused_reads_resume_refused_with_its_words(self):
        _held(
            _row(KIND_TOOLHEAD_REFILL, farm_actions=[_action("resume", "refused", reason="mqtt message verify failed")])
        )

        chip = _chip(_state(1), _FED)

        assert (chip["tone"], chip["label"]["key"]) == ("held", "printers.toolhead.label.resumeRefused")
        assert chip["tooltip"] == [
            {"key": "printers.toolhead.refusedBy", "values": {"reason": "mqtt message verify failed"}},
            {"key": "printers.toolhead.pressResume", "values": {}},
        ]

    def test_a_resume_that_never_left_the_farm_is_no_refusal(self):
        """A frame that never left (offline) is owed again — "Resuming", never "Resume refused"."""
        _held(_row(KIND_TOOLHEAD_REFILL, farm_actions=[_action("resume", "refused", reason="not sent", unsent=True)]))

        chip = _chip(_state(1), _FED)

        assert chip["label"]["key"] == "printers.toolhead.label.resuming"

    def test_a_fed_toolhead_the_farm_will_not_resume_asks_a_person(self):
        """Maintenance mode beside it: the farm resumes nothing — "Toolhead fed", "Press Resume."."""
        _held(_row(KIND_TOOLHEAD_REFILL), _row(KIND_SERVICE_HOLD, row_id=600))

        chip = _chip(_state(1), _FED)

        assert (chip["tone"], chip["label"]["key"]) == ("held", "printers.toolhead.label.fed")
        assert _keys(chip) == ["printers.toolhead.fedFrom", "printers.toolhead.pressResume"]

    def test_a_running_print_on_a_fed_toolhead_reads_resuming(self):
        _held(_row(KIND_TOOLHEAD_REFILL))

        chip = _chip(_state(1, live="RUNNING"), _FED)

        assert chip["label"]["key"] == "printers.toolhead.label.resuming"

    def test_the_farms_own_refill_episode_resumes_its_fed_toolhead(self):
        _held(_row(KIND_TOOLHEAD_REFILL, STATUS_RECOVERING, farm_actions=[_action("load", "reached")]))

        chip = _chip(_state(1), _FED)

        assert (chip["tone"], chip["label"]["key"]) == ("acting", "printers.toolhead.label.resuming")

    def test_the_external_holder_is_named(self):
        _held(_row(KIND_TOOLHEAD_REFILL))

        chip = _chip(_state(254), _feed(FeedPhase.FED, 254))

        assert _keys(chip) == ["printers.toolhead.fedFromExternal"]

    @pytest.mark.parametrize(
        ("status", "tone", "tooltip"),
        [
            (STATUS_RECOVERING, "acting", "printers.incidentRecoveringAction.toolhead_refill"),
            (STATUS_ESCALATED, "held", "printers.incidentAction.toolhead_refill"),
        ],
    )
    def test_an_empty_toolhead_reads_toolhead_empty(self, status, tone, tooltip):
        _held(_row(KIND_TOOLHEAD_REFILL, status))

        chip = _chip(_state(255), _EMPTY_PAUSED)

        assert (chip["tone"], chip["label"]["key"]) == (tone, _TOOLHEAD_EMPTY)
        assert _keys(chip) == [tooltip]

    @pytest.mark.parametrize(
        ("phase", "status", "resume", "live"),
        list(
            itertools.product(
                (FeedPhase.FED, FeedPhase.TAIL),
                (STATUS_RECOVERING, STATUS_ESCALATED),
                (None, "outstanding", "reached", "refused", "void", "taken_over", "unsent"),
                ("PAUSE", "RUNNING"),
            )
        ),
    )
    def test_toolhead_empty_is_never_shown_over_a_fed_toolhead(self, phase, status, resume, live):
        """THE property (the plan's owner 8): whatever the row, the record and the job word, a toolhead
        the feed state reads fed is never labelled "Toolhead empty"."""
        actions = []
        if resume == "outstanding":
            actions = [_action("resume", None)]
        elif resume == "unsent":
            actions = [_action("resume", "refused", unsent=True)]
        elif resume is not None:
            actions = [_action("resume", resume, reason="no")]
        _held(_row(KIND_TOOLHEAD_REFILL, status, farm_actions=actions))

        chip = _chip(_state(1, live=live), _feed(phase, 1))

        assert chip is not None
        assert chip["label"]["key"] != _TOOLHEAD_EMPTY


# --- rule 3: a paused print on an empty toolhead with nothing open ------------------------------------


class TestTheResumeVerdict:
    @pytest.mark.parametrize("reason", ["owed", "maintenance", "physical", "command_pending"])
    def test_a_verdict_a_person_should_see_reads_toolhead_empty(self, reason, monkeypatch):
        _verdict(monkeypatch, reason)

        chip = _chip(_state(255), _EMPTY_PAUSED)

        assert (chip["variant"], chip["tone"], chip["label"]["key"]) == ("toolhead", "held", _TOOLHEAD_EMPTY)
        assert _keys(chip) == [f"printers.toolhead.reason.{reason}"]

    @pytest.mark.parametrize(
        "reason",
        [
            "fed",
            "runout_demand",
            "power_loss_prompt",
            "before_first_layer",
            "last_layer",
            "change_in_flight",
            "eject_sweep",
            "unconfirmed",
            "unknown",
        ],
    )
    def test_every_other_verdict_is_no_chip(self, reason, monkeypatch):
        """012-H2S 2026-10-10: PAUSEd at layer 0 (``before_first_layer``) — the start block loads."""
        _verdict(monkeypatch, reason)

        assert _chip(_state(255), _EMPTY_PAUSED) is None

    def test_the_verdict_set_is_the_refill_verdicts_own(self):
        """Every reason rule 3 speaks for is K7's (``refill_verdict.RefillReason``)."""
        from typing import get_args

        assert set(hold_chip._PAUSED_EMPTY_TOOLTIP) <= set(get_args(refill_verdict.RefillReason))  # noqa: SLF001

    @pytest.mark.parametrize("live", ["IDLE", "RUNNING"])
    def test_an_empty_toolhead_that_is_not_paused_is_no_chip(self, live, monkeypatch):
        _verdict(monkeypatch, "owed")

        assert _chip(_state(255, live=live), _feed(FeedPhase.IDLE)) is None

    def test_a_fed_or_unread_toolhead_is_no_chip(self, monkeypatch):
        _verdict(monkeypatch, "owed")

        assert _chip(_state(1), _FED) is None
        assert _chip(None, None) is None

    def test_the_maintenance_verdict_speaks_past_the_maintenance_row(self, monkeypatch):
        _verdict(monkeypatch, "maintenance")
        _held(_row(KIND_SERVICE_HOLD))

        assert _keys(_chip(_state(255), _EMPTY_PAUSED)) == ["printers.toolhead.reason.maintenance"]


# --- the payload and its vocabulary -------------------------------------------------------------------


class TestThePayload:
    def test_it_is_the_dtos_shape_and_json(self):
        import json

        from backend.app.schemas.printer import HoldChipState

        _held(_row(KIND_TOOLHEAD_REFILL, farm_actions=[_action("resume", None)]))
        chip = _chip(_state(1), _FED)

        assert HoldChipState(**chip).model_dump() == chip
        json.dumps(chip)

    def test_the_kind_sets_are_the_ones_the_frontend_pins(self):
        """``__tests__/i18n/incidentKinds.test.ts`` spells these two sets to pin the locale leaves the
        chip names by kind; they must stay equal."""
        assert {"plate_vision", "toolhead_refill"} == hold_chip.RECOVERING_ACTION_KINDS
        assert {"service_hold"} == hold_chip.OWN_SURFACE_KINDS

    def test_every_static_key_resolves_in_the_english_locale(self):
        """A chip never prints a raw key: every key the builder names outright is a leaf of ``en``
        (the kind-keyed ones are pinned by the frontend's ``incidentKinds`` test)."""
        source = pathlib.Path(hold_chip.__file__).read_text(encoding="utf-8")
        keys = set(re.findall(r'"(printers\.[A-Za-z_.]+)"', source)) - {
            "printers.incident.",
            "printers.incidentAction.",
        }
        leaves = _english_leaves()
        missing = sorted(key for key in keys if not key.endswith(".") and key not in leaves)
        assert missing == []


def _english_leaves() -> set[str]:
    """The dotted leaf paths of ``frontend/src/i18n/locales/en.ts``, read line by line (one key per
    line, a nested object opened by ``name: {`` and closed by ``},``)."""
    root = pathlib.Path(__file__).resolve().parents[4]
    lines = (root / "frontend" / "src" / "i18n" / "locales" / "en.ts").read_text(encoding="utf-8").splitlines()
    stack: list[str] = []
    leaves: set[str] = set()
    opener = re.compile(r"^\s*'?([A-Za-z0-9_-]+)'?: \{$")
    leaf = re.compile(r"^\s*'?([A-Za-z0-9_-]+)'?: ")
    for line in lines[1:]:
        if opener.match(line):
            stack.append(opener.match(line).group(1))
        elif re.match(r"^\s*\},?$", line):
            if stack:
                stack.pop()
        elif leaf.match(line):
            leaves.add(".".join([*stack, leaf.match(line).group(1)]))
    return leaves


class TestTheResumeVerdictDrivesTheFedChip:
    """A FED toolhead under an escalated hold on a PAUSEd print reads the spawner's own closed verdict:
    ``owed`` / ``waiting`` (a guard the farm waits out) → amber "Resuming"; ``declined`` because another
    row owns the pause → that row's chip; ``declined`` otherwise → red "Toolhead fed" + "Press Resume."."""

    def test_a_farm_command_still_pending_is_waiting_never_press_resume(self):
        _held(
            _row(
                KIND_TOOLHEAD_REFILL,
                pending_command={"incident_id": 574, "action_id": 9, "seq": 2, "name": "unload", "target": None},
            )
        )

        chip = _chip(_state(1), _FED)

        assert (chip["tone"], chip["label"]["key"]) == ("acting", "printers.toolhead.label.resuming")

    def test_a_path_that_is_not_quiet_is_waiting(self, monkeypatch):
        monkeypatch.setattr(hold_chip.incident_resolution, "path_quiet", lambda _state: False)
        _held(_row(KIND_TOOLHEAD_REFILL))

        chip = _chip(_state(1), _FED)

        assert (chip["tone"], chip["label"]["key"]) == ("acting", "printers.toolhead.label.resuming")

    def test_a_plate_check_row_owning_the_pause_takes_the_chip(self):
        _held(_row(KIND_TOOLHEAD_REFILL), _row(KIND_PLATE_VISION, row_id=575))

        chip = _chip(_state(1), _FED)

        assert (chip["variant"], chip["label"]["key"]) == ("incident", "printers.incident.plate_vision")
        assert _keys(chip) == ["printers.incidentAction.plate_vision"]

    @pytest.mark.parametrize("answer", ["reached", "taken_over"])
    def test_a_spent_once_asks_a_person(self, answer):
        """The farm's resume reached (and the print paused again) or a person took it over: the farm
        will not resume this row again — "Toolhead fed", "Press Resume."."""
        _held(_row(KIND_TOOLHEAD_REFILL, farm_actions=[_action("resume", answer)]))

        chip = _chip(_state(1), _FED)

        assert (chip["tone"], chip["label"]["key"]) == ("held", "printers.toolhead.label.fed")
        assert _keys(chip)[-1] == "printers.toolhead.pressResume"


def test_a_rebooted_printer_at_the_power_loss_prompt_reads_resuming():
    from backend.app.services.bambu_mqtt import HMSError

    _held(_row(KIND_TOOLHEAD_REFILL))
    state = _state(1)
    state.hms_errors = [HMSError(code="8007", attr=0x03000000, module=3, severity=3, full_code="0300000000008007")]

    chip = _chip(state, _FED)

    assert (chip["tone"], chip["label"]["key"]) == ("acting", "printers.toolhead.label.resuming")


# --- R29 (2026-10-12): the resume states are kind- and driver-scoped; the air stop not taken -----------

_RESUMING = "printers.toolhead.label.resuming"
_RESUME_REFUSED = "printers.toolhead.label.resumeRefused"
_RECOVERING = "printers.incident.recovering"
_AIR_STOP_NOT_TAKEN = "printers.toolhead.label.airStopNotTaken"
_NON_RESUME_KINDS = (KIND_JAM, KIND_PLATE_VISION, KIND_POWER_LOSS, KIND_Z_REFERENCE_LOST)


def _resume_actions(resume: str) -> list[dict]:
    if resume == "outstanding":
        return [_action("resume", None)]
    return [_action("resume", "refused", reason="mqtt message verify failed")]


def _driver(monkeypatch, live: bool) -> None:
    monkeypatch.setattr(printer_incidents, "driver_live", lambda _pid: live)


class TestTheResumeStatesAreKindAndDriverScoped:
    """Production showed "Resuming" over a jam its driver was working: the resume states read ANY row's
    last farm resume. They now speak only for a kind the farm resumes outside a driver (the toolhead
    refill, the runout refill, the repair class) and only with no driver live."""

    def test_the_resume_kinds_are_the_refill_and_repair_kinds(self):
        assert {KIND_TOOLHEAD_REFILL, KIND_RUNOUT, KIND_PHYSICAL} == hold_chip.RESUME_KINDS
        assert printer_incidents.resolution_class(KIND_PHYSICAL) == "repair"
        assert printer_incidents.resolution_class(KIND_TOOLHEAD_REFILL) == "toolhead"

    def test_production_a_jam_whose_driver_resumes_reads_recovering(self, monkeypatch):
        """The production chip: the jam driver's resume rung outstanding while the driver lives."""
        _driver(monkeypatch, True)
        _held(_row(KIND_JAM, STATUS_RECOVERING, slot_desc="AMS A slot 3", farm_actions=[_action("resume", None)]))

        chip = _chip(_state(1), _FED)

        assert (chip["variant"], chip["tone"], chip["label"]["key"]) == ("incident", "acting", _RECOVERING)
        assert _keys(chip) == ["printers.incidentAction.jam"]
        assert chip["qualifier"] == "AMS A slot 3"

    @pytest.mark.parametrize(
        ("kind", "status", "driver", "resume"),
        list(
            itertools.product(
                _NON_RESUME_KINDS, (STATUS_RECOVERING, STATUS_ESCALATED), (True, False), ("outstanding", "refused")
            )
        ),
    )
    def test_a_kind_the_farm_does_not_resume_keeps_its_own_chip(self, kind, status, driver, resume, monkeypatch):
        _driver(monkeypatch, driver)
        _held(_row(kind, status, farm_actions=_resume_actions(resume)))

        chip = _chip(_state(1), _FED)

        escalated = status == STATUS_ESCALATED
        assert chip["variant"] == "incident"
        assert chip["tone"] == ("held" if escalated else "acting")
        assert chip["label"]["key"] == (f"printers.incident.{kind}" if escalated else _RECOVERING)

    @pytest.mark.parametrize(
        ("kind", "status", "resume"),
        list(
            itertools.product(
                (KIND_RUNOUT, KIND_PHYSICAL), (STATUS_RECOVERING, STATUS_ESCALATED), ("outstanding", "refused")
            )
        ),
    )
    def test_a_live_driver_takes_the_resume_states_away(self, kind, status, resume, monkeypatch):
        _driver(monkeypatch, True)
        _held(_row(kind, status, farm_actions=_resume_actions(resume)))

        chip = _chip(_state(1), _FED)

        escalated = status == STATUS_ESCALATED
        assert chip["label"]["key"] == (f"printers.incident.{kind}" if escalated else _RECOVERING)

    @pytest.mark.parametrize("kind", [KIND_RUNOUT, KIND_PHYSICAL, KIND_TOOLHEAD_REFILL])
    def test_an_outstanding_resume_with_no_driver_reads_resuming(self, kind, monkeypatch):
        _driver(monkeypatch, False)
        _held(_row(kind, slot_desc="AMS A slot 2", farm_actions=[_action("resume", None)]))

        chip = _chip(_state(1), _FED)

        assert (chip["tone"], chip["label"]["key"]) == ("acting", _RESUMING)
        assert _keys(chip)[-1] == "printers.toolhead.resumeSent"
        assert chip["qualifier"] == "AMS A slot 2"

    @pytest.mark.parametrize("kind", [KIND_PHYSICAL, KIND_TOOLHEAD_REFILL])
    def test_a_refused_resume_with_its_one_resume_spent_reads_resume_refused(self, kind, monkeypatch):
        _driver(monkeypatch, False)
        _held(_row(kind, farm_actions=_resume_actions("refused")))

        chip = _chip(_state(1), _FED)

        assert (chip["variant"], chip["tone"], chip["label"]["key"]) == ("incident", "held", _RESUME_REFUSED)
        assert chip["tooltip"] == [
            {"key": "printers.toolhead.refusedBy", "values": {"reason": "mqtt message verify failed"}},
            {"key": "printers.toolhead.pressResume", "values": {}},
        ]

    def test_a_refused_resume_over_a_running_print_is_no_resume_state(self, monkeypatch):
        _driver(monkeypatch, False)
        _held(_row(KIND_PHYSICAL, farm_actions=_resume_actions("refused")))

        chip = _chip(_state(1, live="RUNNING"), _FED)

        assert chip["label"]["key"] == "printers.incident.physical"


class TestTheRunoutResumeIsOwedUntilTheFarmGivesUp:
    """A runout row's refill resume is re-owed after a refusal, up to ``RUNOUT_RESUME_CAP`` resumes
    (``incident_resolution.resume_spent``): the chip reads "Resuming" while one is owed, and "Resume
    refused" only once the farm gave up — never red between a refusal and its resend."""

    @staticmethod
    def _refused(count: int, reason: str | None = "mqtt message verify failed") -> list[dict]:
        return [_action("resume", "refused", reason=reason, action_id=41 + n) for n in range(count)]

    def test_a_refused_resume_still_owed_reads_resuming(self, monkeypatch):
        _driver(monkeypatch, False)
        _held(_row(KIND_RUNOUT, slot_desc="AMS A slot 2", farm_actions=self._refused(1)))

        chip = _chip(_state(255), _EMPTY_PAUSED)

        assert (chip["variant"], chip["tone"], chip["label"]["key"]) == ("incident", "acting", _RESUMING)
        assert chip["tooltip"] == [
            {"key": "printers.toolhead.refusedBy", "values": {"reason": "mqtt message verify failed"}},
            {"key": "printers.toolhead.resumeResent", "values": {}},
        ]
        assert chip["qualifier"] == "AMS A slot 2"

    def test_the_owed_and_spent_reading_is_resume_spents_own(self):
        """Per refusal count up to the cap, the chip follows ``resume_spent`` — never a cap of its own."""
        from backend.app.services import incident_resolution

        seen = []
        for count in range(1, incident_resolution.RUNOUT_RESUME_CAP + 1):
            _held(_row(KIND_RUNOUT, farm_actions=self._refused(count)))
            spent = incident_resolution.resume_spent(_PID, 574)
            label = _chip(_state(255), _EMPTY_PAUSED)["label"]["key"]
            if spent is None:
                assert label == _RESUMING, count
            else:
                assert spent.gave_up, count
                assert label == _RESUME_REFUSED, count
            seen.append(label)
        assert seen[0] == _RESUMING
        assert seen[-1] == _RESUME_REFUSED

    def test_the_cap_spent_reads_resume_refused(self, monkeypatch):
        from backend.app.services import incident_resolution

        _driver(monkeypatch, False)
        _held(_row(KIND_RUNOUT, farm_actions=self._refused(incident_resolution.RUNOUT_RESUME_CAP)))

        chip = _chip(_state(255), _EMPTY_PAUSED)

        assert (chip["tone"], chip["label"]["key"]) == ("held", _RESUME_REFUSED)
        assert _keys(chip) == ["printers.toolhead.refusedBy", "printers.toolhead.pressResume"]

    def test_an_owed_resume_with_no_printer_words_names_only_the_resend(self, monkeypatch):
        _driver(monkeypatch, False)
        _held(_row(KIND_RUNOUT, farm_actions=self._refused(1, reason=None)))

        assert _keys(_chip(_state(255), _EMPTY_PAUSED)) == ["printers.toolhead.resumeResent"]


class TestTheAirStopNotTaken:
    """Capsule 6: the farm STOPS a print that printed a layer on air; a stop the printer did not take
    (refused, or unanswered past ``STOP_SELF_HEAL``) after one resend escalates the row
    ``air_stop_not_taken``. The chip names it: the stop is a person's now."""

    @staticmethod
    def _air_row(kind: str, stop: dict, *, status: str = STATUS_ESCALATED, **extra) -> dict:
        return _row(kind, status, farm_actions=[stop], **{printer_incidents.PAYLOAD_AIR_STOP: True, **extra})

    @pytest.mark.parametrize("kind", [KIND_TOOLHEAD_REFILL, KIND_JAM])
    def test_a_refused_air_stop_reads_air_stop_not_taken_with_the_printers_words(self, kind):
        _held(self._air_row(kind, _action("stop", "refused", reason="print is busy"), slot_desc="AMS A slot 2"))

        chip = _chip(_state(255, live="RUNNING"), _feed(FeedPhase.AIR))

        assert (chip["variant"], chip["tone"], chip["label"]["key"]) == ("incident", "held", _AIR_STOP_NOT_TAKEN)
        assert chip["tooltip"] == [
            {"key": "printers.toolhead.airStopNotTaken", "values": {}},
            {"key": "printers.toolhead.refusedBy", "values": {"reason": "print is busy"}},
            {"key": "printers.toolhead.stopOnPrinter", "values": {}},
        ]
        assert chip["qualifier"] == "AMS A slot 2"

    def test_an_air_stop_unanswered_past_its_bound_reads_air_stop_not_taken(self):
        _held(self._air_row(KIND_TOOLHEAD_REFILL, _action("stop", None)))

        chip = _chip(_state(255, live="RUNNING"), _feed(FeedPhase.AIR))

        assert chip["label"]["key"] == _AIR_STOP_NOT_TAKEN
        assert _keys(chip) == ["printers.toolhead.airStopNotTaken", "printers.toolhead.stopOnPrinter"]

    @pytest.mark.parametrize("answer", ["reached", "taken_over"])
    def test_a_taken_stop_is_no_air_stop_chip(self, answer):
        _held(self._air_row(KIND_JAM, _action("stop", answer)))

        chip = _chip(_state(255, live="RUNNING"), _feed(FeedPhase.AIR))

        assert chip["label"]["key"] == "printers.incident.jam"

    def test_the_driver_still_resending_is_no_air_stop_chip(self, monkeypatch):
        _driver(monkeypatch, True)
        _held(self._air_row(KIND_JAM, _action("stop", None), status=STATUS_RECOVERING))

        chip = _chip(_state(255, live="RUNNING"), _feed(FeedPhase.AIR))

        assert chip["label"]["key"] == _RECOVERING

    def test_a_row_without_the_air_stop_is_no_air_stop_chip(self):
        _held(_row(KIND_JAM, farm_actions=[_action("stop", "refused")]))

        assert _chip(_state(1), _FED)["label"]["key"] == "printers.incident.jam"
