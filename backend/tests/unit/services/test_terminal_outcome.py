"""THE classification of one print terminal (``terminal_outcome.build_terminal_outcome``).

Every status rewrite ``main.on_print_complete`` used to do in place now lives in the
builder, each with its reason, and the outcome carries the facts the sinks used to
re-derive for themselves: the verdict, the equipment faults open AT the terminal (captured
before any closer ran), the printer's words and category, and a refused plate's cause.
The end-to-end journeys through ``main.on_print_complete`` are in
``integration/test_print_lifecycle.py``; this pins the pure table.
"""

import pytest

from backend.app.services.hms_errors import PrinterMessage
from backend.app.services.plate_occupancy import DepositEvidence, PlateRefusal
from backend.app.services.terminal_outcome import build_terminal_outcome, open_holds_at_terminal

_JOB = "JOB-1"
_VISION_WORDS = {"short_code": "0500_808C", "description": "Detected build plate offset."}


def _evidence(*, status="failed", deposited=True, dry_run=False) -> DepositEvidence:
    return DepositEvidence(
        final_status=status,
        is_dry_run=dry_run,
        peaks_reliable=True,
        last_layer_num=10 if deposited else 0,
        last_progress=40.0 if deposited else 0,
    )


def _build(raw="failed", verdict=None, *, holds=(), deposited=True, first_article=False, eject=False, hms=None):
    return build_terminal_outcome(
        raw_status=raw,
        verdict=verdict,
        open_incidents=holds,
        job_id=_JOB,
        evidence=_evidence(status=raw, deposited=deposited),
        first_article=first_article,
        is_eject=eject,
        hms_errors=hms,
    )


class TestRecordedStatus:
    """The farm's word for a terminal — every rewrite, with its reason, in one place."""

    @pytest.mark.parametrize("first_article", [False, True])
    @pytest.mark.parametrize("raw", ["failed", "aborted", "cancelled"])
    def test_a_refused_plate_is_a_stop_first_article_included(self, raw, first_article):
        """Recorded ``failed``, a first article would spend its one retry and feed
        quarantine for a plate the printer itself turned away."""
        outcome = _build(raw, "plate_refused", deposited=False, first_article=first_article)
        assert outcome.recorded_status == "cancelled"

    @pytest.mark.parametrize("raw", ["failed", "aborted"])
    def test_an_operator_ui_stop_is_a_cancel(self, raw):
        assert _build(raw, "operator_ui").recorded_status == "cancelled"

    def test_a_no_deposit_stop_is_a_cancel_but_a_first_article_keeps_failed(self):
        assert _build("failed", deposited=False).recorded_status == "cancelled"
        assert _build("failed", deposited=False, first_article=True).recorded_status == "failed"

    def test_an_eject_sweeps_word_is_its_own(self):
        assert _build("failed", deposited=False, eject=True).recorded_status == "failed"

    def test_a_screen_stop_that_deposited_is_a_cancel(self):
        assert _build("failed", "operator_screen").recorded_status == "cancelled"

    def test_the_reconciles_unknown_outcome_is_a_cancel(self):
        assert _build("aborted", "reconcile_unknown").recorded_status == "cancelled"

    def test_a_genuine_failure_and_a_finish_keep_their_words(self):
        assert _build("failed").recorded_status == "failed"
        assert _build("completed").recorded_status == "completed"

    def test_an_unattributed_abort_is_a_cancel_never_aborted(self):
        """CHANGED 2026-09-24: an outcome nobody can attribute is not a completed one.
        Left ``aborted``, the farm policy matched no branch and the run ended one plate
        short in silence; ``cancelled`` takes the operator-stop disposition."""
        assert _build("aborted").recorded_status == "cancelled"
        assert _build("aborted", first_article=True).recorded_status == "cancelled"
        assert _build("aborted", eject=True).recorded_status == "cancelled"
        assert _build("aborted").verdict is None  # no verdict is invented for it


class TestFaultsOpenAtTheTerminal:
    def test_only_equipment_faults_are_captured(self):
        """A HOLD IS NOT A FAULT: a declared maintenance hold is not something an operator
        stop "finishes"."""
        holds = [{"kind": "runout", "job_id": "x"}, {"kind": "service_hold", "job_id": ""}]
        assert _build(holds=holds).faults_open == frozenset({"runout"})
        assert open_holds_at_terminal(holds) == frozenset({"runout"})

    def test_faults_of_any_job_count(self):
        """ "The printer was holding" is a fact about the PRINTER (a physical fault can
        outlive the job that met it)."""
        assert _build(holds=[{"kind": "physical", "job_id": "older"}]).faults_open == frozenset({"physical"})

    def test_operator_stopped_reads_the_verdict(self):
        assert _build("failed", "operator_ui").operator_stopped is True
        assert _build("failed", "operator_screen").operator_stopped is True
        assert _build("failed", "plate_refused").operator_stopped is False
        assert _build("failed").operator_stopped is False


class TestThePrintersEvidence:
    def test_a_refused_plates_words_come_from_its_hold_because_the_stop_wiped_the_wire(self):
        hold = {"kind": "plate_vision", "job_id": _JOB, "printer_messages": [_VISION_WORDS]}

        outcome = _build("failed", "plate_refused", holds=[hold], deposited=False, hms=[])

        assert outcome.printer_message == "[0500_808C] Detected build plate offset."
        assert outcome.failure_category == "Plate not empty (printer vision)"
        assert outcome.plate_refusal == PlateRefusal(
            messages=(PrinterMessage(short_code="0500_808C", description="Detected build plate offset."),)
        )

    def test_another_jobs_hold_explains_nothing_about_this_one(self):
        hold = {"kind": "plate_vision", "job_id": "OTHER", "printer_messages": [_VISION_WORDS]}
        outcome = _build("failed", holds=[hold])
        assert outcome.printer_message is None
        assert outcome.failure_category is None

    def test_the_holds_words_lead_and_the_live_list_follows_deduplicated(self):
        runout = {"code": "0x8011", "attr": 0x07FF_0000, "module": 7, "severity": 2}
        vision_again = {"code": "0x808c", "attr": 0x0500_0000, "module": 5, "severity": 2}
        hold = {"kind": "plate_vision", "job_id": _JOB, "printer_messages": [_VISION_WORDS]}

        outcome = _build("failed", "plate_refused", holds=[hold], hms=[vision_again, runout])

        assert outcome.printer_message.startswith("[0500_808C] Detected build plate offset.; [07FF_8011]")
        assert outcome.printer_message.count("0500_808C") == 1

    def test_an_operator_stop_the_printer_explained_nothing_about_is_user_cancelled(self):
        outcome = _build("failed", "operator_ui", hms=[])
        assert outcome.failure_category == "User cancelled"
        assert outcome.printer_message is None

    def test_a_completed_print_carries_no_failure_facts(self):
        runout = {"code": "0x8011", "attr": 0x07FF_0000, "module": 7, "severity": 2}
        outcome = _build("completed", hms=[runout])
        assert outcome.failure_category is None
        assert outcome.printer_message is None

    def test_no_refusal_without_the_verdict(self):
        hold = {"kind": "plate_vision", "job_id": _JOB, "printer_messages": [_VISION_WORDS]}
        assert _build("completed", holds=[hold]).plate_refusal is None
        assert _build("failed", "operator_ui", holds=[hold]).plate_refusal is None
