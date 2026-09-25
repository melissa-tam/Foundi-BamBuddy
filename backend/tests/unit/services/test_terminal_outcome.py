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


def _charge(raw, verdict=None, *, reliable=True, layer=10, progress=40.0, dry_run=False):
    return build_terminal_outcome(
        raw_status=raw,
        verdict=verdict,
        open_incidents=(),
        job_id=_JOB,
        evidence=DepositEvidence(
            final_status=raw,
            is_dry_run=dry_run,
            peaks_reliable=reliable,
            last_layer_num=layer,
            last_progress=progress,
        ),
        first_article=False,
        is_eject=False,
        hms_errors=None,
    ).charge


class TestChargeBasis:
    """What the job that ended is charged for — decided ONCE, from THIS terminal's evidence.

    Never from the live printer: by the time a terminal is processed the printer can be running
    another job (2026-09-16 → 24: ~7.2 kg over 32 phantom charges scaled by the next job's live
    progress). The ledger heals in one direction only (doctrine rule 8) — the increase-only wire
    sync restores an under-charge on a tagged roll, nothing restores an over-charge — so a job
    nobody measured is charged nothing."""

    @pytest.mark.parametrize("verdict", [None, "operator_ui", "operator_screen"])
    def test_a_finish_charges_the_whole_plate(self, verdict):
        assert _charge("completed", verdict) == "full"

    def test_a_finish_charges_the_whole_plate_whatever_its_peaks_say(self):
        """A restart-attached client reads no peaks — the printer still said the job ran to its end."""
        assert _charge("completed", reliable=False, layer=0, progress=0) == "full"

    def test_a_dry_runs_finish_is_still_the_printers_word(self):
        """Recorded ``cancelled`` (it deposited nothing), yet nothing is un-extruded by a rewrite: the
        motion-only file declares zero grams, so ``full`` charges zero."""
        assert _charge("completed", dry_run=True) == "full"

    def test_the_reconciles_unknown_outcome_charges_nothing(self):
        assert _charge("aborted", "reconcile_unknown", reliable=False, layer=0, progress=0) == "none"

    def test_the_unknown_outcome_charges_nothing_even_with_a_peak_in_hand(self):
        """The ``ended`` synthesis carries the live progress of the SAME job; the IDLE branch does not,
        and whatever it carried, nobody observed how the job ended."""
        assert _charge("aborted", "reconcile_unknown", reliable=False, progress=60.0) == "none"

    @pytest.mark.parametrize(
        ("raw", "verdict"),
        [("failed", None), ("failed", "operator_ui"), ("aborted", "operator_screen"), ("failed", "plate_refused")],
    )
    def test_any_other_end_with_measured_peaks_is_partial(self, raw, verdict):
        assert _charge(raw, verdict) == "partial"

    def test_a_measured_zero_is_a_partial_charge_of_nothing(self):
        """A job stopped before its first layer: the peaks WERE measured, and they say zero."""
        assert _charge("failed", layer=0, progress=0) == "partial"

    def test_unmeasured_peaks_with_nothing_read_charge_nothing(self):
        """A client that joined mid-job and read no peak before the end has nothing to scale by —
        and a guess could only ever over-charge a ledger that never heals downward."""
        assert _charge("failed", reliable=False, layer=0, progress=0) == "none"
        assert _charge("aborted", reliable=False, layer=None, progress=None) == "none"

    @pytest.mark.parametrize(("layer", "progress"), [(0, 35.0), (80, 0)])
    def test_a_peak_read_after_an_attach_still_measures_this_job(self, layer, progress):
        """The firmware's percent and layer are ABSOLUTE: a non-zero reading taken after the attach is
        this job's (the attach cleared the predecessor's) — only its zero means nothing."""
        assert _charge("failed", reliable=False, layer=layer, progress=progress) == "partial"

    def test_the_unobserved_job_phase_outcome_charges_nothing_by_construction(self):
        """The reconcile's ``superseded`` job phase builds its outcome through the one builder under
        ``reconcile_unknown`` — so the job phase has no charge to make, and makes none."""
        from types import SimpleNamespace

        from backend.app.services.job_terminal import unobserved_outcome

        outcome = unobserved_outcome(SimpleNamespace(subtask_id="J1"), SimpleNamespace(first_article=False))
        assert outcome.charge == "none"
