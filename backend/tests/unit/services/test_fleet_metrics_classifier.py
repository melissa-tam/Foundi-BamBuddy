"""Tests for the read-time availability classifier (services.fleet_metrics.classifier).

The classifier is a fourteen-row ordered table, and what it is FOR is the order: any
one rule is obvious, and every bug this module can have is two rules disagreeing about
which one wins. So the suite walks the table row by row, and then drives the pairs
that actually collide in production — a fault beside a maintenance hold, a print
running under one, a cooldown beside an open fault, PAUSE against the printing states,
and a deactivated printer against every other reason it might look down.

The two ABSENCE inputs get the same treatment as an observation, because the whole
point of making the reason-for-absence a value is that a durable hold reads the same
on either side of the recorder's own history.
"""

from __future__ import annotations

import pytest

from backend.app.models.printer_incident import (
    KIND_JAM,
    KIND_PHYSICAL,
    KIND_RUNOUT,
    KIND_SERVICE_HOLD,
    KIND_Z_REFERENCE_LOST,
)
from backend.app.models.printer_observation_span import (
    PLATE_PHASE_CLEAR,
    PLATE_PHASE_COOLING,
    PLATE_PHASE_EJECTING,
    PLATE_PHASE_HELD,
)
from backend.app.services.fleet_activity import Observation
from backend.app.services.fleet_metrics.classifier import (
    CYCLE_COOLING,
    CYCLE_EJECTING,
    DOWN_MODEL_MISMATCH,
    DOWN_NO_USB,
    DOWN_OFFLINE,
    DOWN_PAUSED,
    DOWN_PLATE_HELD,
    DOWN_QUARANTINED,
    GROUP_DOWN,
    IDLE,
    NO_SPAN_YET,
    NOT_RECORDED,
    OBSERVATION_GAP,
    OUT_OF_FLEET,
    PLANNED,
    PRINTING,
    PRINTING_STATES,
    UNOBSERVED,
    availability_class,
    down_fault,
    fault_kind_of,
)
from backend.app.services.plate_occupancy import ACTIVE_PRINT_STATES

NONE: frozenset[str] = frozenset()


def _obs(**overrides: object) -> Observation:
    """A connected, printing, healthy printer — the baseline each test varies once."""
    fields: dict[str, object] = {
        "is_active": True,
        "connected": True,
        "gcode_state": "RUNNING",
        "plate_phase": PLATE_PHASE_CLEAR,
        "quarantined": False,
        "usb_present": True,
        "model_mismatch": False,
    }
    fields.update(overrides)
    return Observation(**fields)  # type: ignore[arg-type]


class TestTheOrderedTable:
    """One case per row of the table, in the order the rows are tried."""

    def test_1_a_deactivated_printer_is_out_of_fleet_whatever_else_it_reads(self):
        # Every other reason to be down at once: the operator withdrew the machine, so
        # none of them is a fact about the FLEET any more.
        observation = _obs(
            is_active=False,
            connected=False,
            gcode_state="PAUSE",
            plate_phase=PLATE_PHASE_HELD,
            quarantined=True,
            usb_present=False,
            model_mismatch=True,
        )
        assert availability_class(observation, frozenset({KIND_JAM, KIND_SERVICE_HOLD})) is OUT_OF_FLEET

    @pytest.mark.parametrize("state", sorted(PRINTING_STATES))
    def test_2_every_printing_state_is_printing(self, state: str):
        assert availability_class(_obs(gcode_state=state), NONE) is PRINTING

    def test_2_printing_loses_to_an_ejecting_platen(self):
        # The sweep IS a job; counting it as output would credit the farm with a print
        # for every part it pushed off the plate.
        observation = _obs(gcode_state="RUNNING", plate_phase=PLATE_PHASE_EJECTING)
        assert availability_class(observation, NONE) is CYCLE_EJECTING

    def test_3_cooling_and_ejecting_are_the_between_prints_work(self):
        assert availability_class(_obs(gcode_state="IDLE", plate_phase=PLATE_PHASE_COOLING), NONE) is CYCLE_COOLING
        assert availability_class(_obs(gcode_state="IDLE", plate_phase=PLATE_PHASE_EJECTING), NONE) is CYCLE_EJECTING

    def test_3_a_cooling_plate_still_reads_cooling_while_the_wire_says_running(self):
        # The cooldown begins before the firmware's own FINISH lands; the plate axis has
        # one authority and it is not the gcode word.
        assert availability_class(_obs(plate_phase=PLATE_PHASE_COOLING), NONE) is PRINTING

    def test_4_an_open_fault_is_down_by_its_kind(self):
        klass = availability_class(_obs(gcode_state="IDLE"), frozenset({KIND_JAM}))
        assert klass.group == GROUP_DOWN
        assert klass.key == f"down:fault:{KIND_JAM}"
        assert fault_kind_of(klass) == KIND_JAM

    def test_4_several_open_faults_are_named_by_the_stores_precedence(self):
        # ``physical`` outranks ``runout`` outranks ``jam`` — the printer card's chip
        # order, so the metric and the chip never name different faults.
        kinds = frozenset({KIND_JAM, KIND_RUNOUT, KIND_PHYSICAL})
        assert availability_class(_obs(gcode_state="IDLE"), kinds) is down_fault(KIND_PHYSICAL)

    def test_5_a_declared_hold_is_planned_not_down(self):
        assert availability_class(_obs(gcode_state="IDLE"), frozenset({KIND_SERVICE_HOLD})) is PLANNED

    def test_6_no_span_yet_is_not_recorded(self):
        assert availability_class(NO_SPAN_YET, NONE) is NOT_RECORDED

    def test_7_a_gap_after_recording_began_is_unobserved(self):
        assert availability_class(OBSERVATION_GAP, NONE) is UNOBSERVED

    def test_8_a_disconnected_printer_is_down_offline(self):
        assert availability_class(_obs(connected=False, gcode_state=None), NONE) is DOWN_OFFLINE

    def test_9_pause_is_down_and_never_printing(self):
        # PAUSE is in ACTIVE_PRINT_STATES — a paused print still owns the platen — but
        # it is a printer waiting on somebody, which is the whole down/printing split.
        assert "PAUSE" in ACTIVE_PRINT_STATES
        assert "PAUSE" not in PRINTING_STATES
        assert availability_class(_obs(gcode_state="PAUSE"), NONE) is DOWN_PAUSED

    def test_10_quarantined_is_down(self):
        assert availability_class(_obs(gcode_state="IDLE", quarantined=True), NONE) is DOWN_QUARANTINED

    def test_11_an_occupied_plate_is_down_waiting_on_a_person(self):
        assert availability_class(_obs(gcode_state="IDLE", plate_phase=PLATE_PHASE_HELD), NONE) is DOWN_PLATE_HELD

    def test_12_a_model_mismatch_is_down(self):
        assert availability_class(_obs(gcode_state="IDLE", model_mismatch=True), NONE) is DOWN_MODEL_MISMATCH

    def test_13_a_genuinely_absent_usb_drive_is_down(self):
        assert availability_class(_obs(gcode_state="IDLE", usb_present=False), NONE) is DOWN_NO_USB

    def test_13_an_unknown_usb_reading_is_not_down(self):
        # NULL means "we could not tell", which must never be charged to the printer:
        # ``usb_present is False`` and not a truth test.
        assert availability_class(_obs(gcode_state="IDLE", usb_present=None), NONE) is IDLE

    def test_14_an_available_printer_doing_nothing_is_idle(self):
        assert availability_class(_obs(gcode_state="IDLE"), NONE) is IDLE


class TestThePairsThatCollide:
    """The precedences that decide real figures, each stated as the pair it settles."""

    def test_a_fault_beside_a_maintenance_hold_is_a_fault(self):
        # The repair window of a broken printer is unplanned downtime even while the
        # operator has also declared the machine out of the automatic lanes.
        kinds = frozenset({KIND_JAM, KIND_SERVICE_HOLD})
        assert availability_class(_obs(gcode_state="IDLE"), kinds) is down_fault(KIND_JAM)

    def test_printing_under_a_maintenance_hold_is_real_output(self):
        assert availability_class(_obs(), frozenset({KIND_SERVICE_HOLD})) is PRINTING

    def test_printing_beside_an_open_fault_is_real_output(self):
        # An AMS fault can stand open on a printer that is running the next job; an
        # observed physical process is never overlaid by the ledger.
        assert availability_class(_obs(), frozenset({KIND_PHYSICAL})) is PRINTING

    def test_cooling_beside_an_open_fault_is_still_between_prints(self):
        observation = _obs(gcode_state="FINISH", plate_phase=PLATE_PHASE_COOLING)
        assert availability_class(observation, frozenset({KIND_JAM})) is CYCLE_COOLING

    def test_a_paused_print_under_a_fault_is_named_by_the_fault(self):
        # The pause is the SYMPTOM; the ledger knows the cause, and an operator needs
        # the cause. Rule 4 sits above the PAUSE row for exactly this.
        observation = _obs(gcode_state="PAUSE")
        assert availability_class(observation, frozenset({KIND_RUNOUT})) is down_fault(KIND_RUNOUT)

    def test_a_disconnected_printer_under_a_hold_is_planned(self):
        # A printer an operator powered down to work on is not an outage.
        observation = _obs(connected=False, gcode_state=None)
        assert availability_class(observation, frozenset({KIND_SERVICE_HOLD})) is PLANNED

    def test_deactivated_beats_even_an_open_fault(self):
        observation = _obs(is_active=False, connected=False, gcode_state=None)
        assert availability_class(observation, frozenset({KIND_PHYSICAL})) is OUT_OF_FLEET


class TestTheLedgerSurvivesTheRecordersAbsence:
    """Rules 4 and 5 fire under both absences — the reason the absence is an input."""

    @pytest.mark.parametrize("seen", [NO_SPAN_YET, OBSERVATION_GAP])
    def test_a_fault_reads_the_same_with_no_observation_at_all(self, seen: object):
        assert availability_class(seen, frozenset({KIND_Z_REFERENCE_LOST})) is down_fault(KIND_Z_REFERENCE_LOST)

    @pytest.mark.parametrize("seen", [NO_SPAN_YET, OBSERVATION_GAP])
    def test_a_declared_hold_reads_the_same_with_no_observation_at_all(self, seen: object):
        assert availability_class(seen, frozenset({KIND_SERVICE_HOLD})) is PLANNED

    def test_the_two_absences_agree_with_each_other_whenever_the_ledger_decides(self):
        # The pin behind "recording_since never enters the classifier": while a durable
        # row is open, which side of the recorder's history an instant falls on makes
        # no difference at all.
        for kinds in (frozenset({KIND_JAM}), frozenset({KIND_SERVICE_HOLD}), frozenset({KIND_PHYSICAL})):
            assert availability_class(NO_SPAN_YET, kinds) is availability_class(OBSERVATION_GAP, kinds)

    def test_without_a_durable_row_the_two_absences_are_different_answers(self):
        assert availability_class(NO_SPAN_YET, NONE) is not availability_class(OBSERVATION_GAP, NONE)


class TestTheClassValueObject:
    """Keys are the wire contract, and identity is what the sweep merges on."""

    def test_instances_are_interned_so_identity_is_equality(self):
        assert availability_class(_obs(), NONE) is availability_class(_obs(), NONE)
        assert down_fault(KIND_JAM) is down_fault(KIND_JAM)

    def test_keys_are_stable_and_carry_the_cause(self):
        assert PRINTING.key == "printing"
        assert IDLE.key == "idle"
        assert PLANNED.key == "planned"
        assert OUT_OF_FLEET.key == "out_of_fleet"
        assert NOT_RECORDED.key == "not_recorded"
        assert UNOBSERVED.key == "unobserved"
        assert CYCLE_COOLING.key == "cycle_overhead:cooling"
        assert CYCLE_EJECTING.key == "cycle_overhead:ejecting"
        assert DOWN_OFFLINE.key == "down:offline"
        assert DOWN_PAUSED.key == "down:paused"
        assert DOWN_QUARANTINED.key == "down:quarantined"
        assert DOWN_PLATE_HELD.key == "down:plate_held"
        assert DOWN_MODEL_MISMATCH.key == "down:model_mismatch"
        assert DOWN_NO_USB.key == "down:no_usb"
        assert down_fault(KIND_JAM).key == "down:fault:jam"

    def test_only_a_fault_class_names_a_kind(self):
        assert fault_kind_of(DOWN_OFFLINE) is None
        assert fault_kind_of(PLANNED) is None
        assert fault_kind_of(down_fault(KIND_SERVICE_HOLD)) == KIND_SERVICE_HOLD

    def test_every_down_class_answers_is_down_and_no_other_does(self):
        down = (DOWN_OFFLINE, DOWN_PAUSED, DOWN_QUARANTINED, DOWN_PLATE_HELD, DOWN_MODEL_MISMATCH, DOWN_NO_USB)
        assert all(klass.is_down for klass in (*down, down_fault(KIND_JAM)))
        assert not any(klass.is_down for klass in (PRINTING, IDLE, PLANNED, OUT_OF_FLEET, NOT_RECORDED, UNOBSERVED))
