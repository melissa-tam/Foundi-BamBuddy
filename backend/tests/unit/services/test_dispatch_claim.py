"""``dispatch_claim`` — what a dispatch claim's evidence means (owner 9, 2026-10-11; R17/R18 2026-10-12).

The claim of a unit marked ``printing`` is decided by its START's answer on the record first; production's
start watchdog survives as the record's two self-heal bounds (``dispatch_start``), whose causes and the
late-answer rule live here. The tables pinned:

* :func:`verdict_of` — every record answer maps to one verdict, an outstanding start WAITS, a LATE answer
  touches nothing, and a new answer raises instead of falling through to a silent requeue;
* :func:`start_late` / :func:`fallback_cause` — the late-answer guard [AR-H5] and the bound's causes;
* :func:`judge` — the ONE reading for a claim the record cannot answer (no start recorded), at the
  printer's first fresh report. Its three double-dispatch guards must NEVER yield ``dead``: a wrong
  release is a print onto an occupied plate.
"""

from __future__ import annotations

import pytest

from backend.app.services import dispatch_claim
from backend.app.services.command_outcome import Answer
from backend.app.services.dispatch_claim import (
    RETURN_CAUSES,
    ClaimEvidence,
    fallback_cause,
    judge,
    start_late,
    verdict_of,
)


def _evidence(**overrides) -> ClaimEvidence:
    """An unrecorded claim that is DEAD on every axis, so each case names only its one difference."""
    base = {
        "live_state": "IDLE",
        "archive_printing": False,
        "dispatch_subtask": "dispatch-1",
        "live_subtask": "some-other-job",
    }
    base.update(overrides)
    return ClaimEvidence(**base)


class TestTheStartsAnswerDecides:
    def test_an_outstanding_start_waits(self):
        """Until its event or its self-heal bound (``dispatch_start``) — never a lane's clock."""
        assert verdict_of(None, late=False) == "waiting"
        assert verdict_of(None, late=True) == "waiting"

    @pytest.mark.parametrize(
        ("answer", "verdict"),
        [
            (Answer.REACHED, "started"),
            (Answer.REFUSED, "refused"),
            (Answer.VOID, "void"),
            (Answer.TAKEN_OVER, "taken_over"),
        ],
    )
    def test_every_answer_has_one_verdict(self, answer, verdict):
        assert verdict_of(answer, late=False) == verdict

    def test_the_table_is_total_over_the_answers(self):
        assert {verdict_of(answer, late=False) for answer in Answer} == {"started", "refused", "void", "taken_over"}

    @pytest.mark.parametrize("answer", list(Answer))
    def test_a_late_answer_of_any_kind_is_late(self, answer):
        """A start answered after its bound returned the unit touches no claim and no lease, whatever it is."""
        assert verdict_of(answer, late=True) == "late"

    @pytest.mark.parametrize(
        ("verdict", "cause"),
        [("refused", "start_refused"), ("void", "start_void"), ("taken_over", "start_taken_over")],
    )
    def test_every_unstarted_verdict_names_its_return_cause(self, verdict, cause):
        assert RETURN_CAUSES[verdict] == cause

    def test_the_causes_are_the_requeue_owners(self):
        from typing import get_args

        from backend.app.services.requeue import ReturnCause

        assert set(RETURN_CAUSES.values()) | {
            dispatch_claim.UNRECORDED_CAUSE,
            dispatch_claim.UNANSWERED_CAUSE,
            dispatch_claim.NOT_ACTIVE_CAUSE,
        } <= set(get_args(ReturnCause))


class TestTheBoundsFallback:
    """Production ``_watchdog_print_start`` at 337f4954: Phase A (no answer, no landing) and Phase B (landed,
    never active, #1678) each return the unit under their own cause."""

    def test_the_causes(self):
        assert fallback_cause(landed=False) == "start_unanswered"
        assert fallback_cause(landed=True) == "start_not_active"

    @pytest.mark.parametrize(
        ("bound_passed", "status", "current", "late"),
        [
            # the bound returned the unit: pending again, or re-dispatched under a NEWER start
            (True, "pending", 5, True),
            (True, "printing", 9, True),
            (True, "cancelled", 5, True),
            # the bound passed but the fallback has not moved the unit: whichever comes first wins
            (True, "printing", 5, False),
            # no bound passed: never late (an answer applied as ever)
            (False, "pending", 5, False),
            (False, "printing", 9, False),
        ],
    )
    def test_the_late_answer_guard(self, bound_passed, status, current, late):
        assert start_late(bound_passed=bound_passed, unit_status=status, current_start=current, action_id=5) is late


class TestTheUnrecordedClaimJudge:
    def test_the_baseline_is_dead(self):
        assert judge(_evidence()) == "dead"

    @pytest.mark.parametrize("live", ["PREPARE", "SLICING", "RUNNING", "PAUSE"])
    def test_an_active_state_is_started(self, live):
        """PAUSE included: a native-vision trip pauses at print start with the plate occupied."""
        assert judge(_evidence(live_state=live)) == "started"

    def test_no_state_at_all_is_started(self):
        """No state is not evidence."""
        assert judge(_evidence(live_state="")) == "started"

    def test_a_printing_archive_is_started(self):
        assert judge(_evidence(archive_printing=True)) == "started"

    @pytest.mark.parametrize("live", ["FINISH", "FAILED", "IDLE"])
    def test_our_own_job_in_any_job_word_is_started(self, live):
        assert judge(_evidence(live_state=live, live_subtask="dispatch-1")) == "started"

    @pytest.mark.parametrize(("dispatch_subtask", "live_subtask"), [("", ""), ("", "x"), ("0", "0")])
    def test_an_id_that_names_no_job_is_no_corroboration(self, dispatch_subtask, live_subtask):
        assert judge(_evidence(dispatch_subtask=dispatch_subtask, live_subtask=live_subtask)) == "dead"


class TestTheModuleIsALeaf:
    def test_it_imports_no_farm_service_but_the_owners_of_its_vocabulary(self):
        """The applying is ``services/dispatch_start``'s. The imports are owners of a definition a second
        spelling would fork: the active-job set, the one job-id comparison and the record's answers."""
        import inspect

        service_imports = sorted(
            line.strip()
            for line in inspect.getsource(dispatch_claim).splitlines()
            if line.strip().startswith(("import ", "from ")) and "backend.app.services" in line
        )
        assert service_imports == [
            "from backend.app.services.command_outcome import Answer",
            "from backend.app.services.job_identity import same_job",
            "from backend.app.services.plate_occupancy import ACTIVE_PRINT_STATES",
        ]

    def test_it_holds_no_process_state(self):
        """The start-watchdog registry is gone: the record is the only memory of a send."""
        assert not hasattr(dispatch_claim, "_start_watchdogs")
        assert not hasattr(dispatch_claim, "DISPATCH_START_BUDGET_S")
