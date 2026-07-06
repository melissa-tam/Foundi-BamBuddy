"""Unit tests for production-run math + transition rules (Phase 2)."""

import pytest

from backend.app.services.production_run import ALLOWED_TRANSITIONS, can_transition, plates_needed


class TestPlatesNeeded:
    def test_exact_division(self):
        assert plates_needed(12, 3) == 4

    def test_remainder_rounds_up_overproducing(self):
        # target 10 at 3/plate → 4 plates = 12 units (over-production).
        assert plates_needed(10, 3) == 4

    def test_one_per_plate(self):
        assert plates_needed(5, 1) == 5

    def test_target_one(self):
        assert plates_needed(1, 3) == 1

    def test_units_per_plate_larger_than_target(self):
        assert plates_needed(2, 5) == 1

    def test_units_per_plate_floor_of_one(self):
        # Defensive: a 0/negative units_per_plate is treated as 1.
        assert plates_needed(4, 0) == 4

    @pytest.mark.parametrize(
        "target,upp,expected",
        [(7, 2, 4), (100, 7, 15), (9, 4, 3), (3, 3, 1), (1, 1, 1)],
    )
    def test_table(self, target, upp, expected):
        assert plates_needed(target, upp) == expected


class TestTransitions:
    def test_pause_only_from_active(self):
        assert can_transition("active", "pause") is True
        assert can_transition("paused", "pause") is False
        assert can_transition("completed", "pause") is False
        assert can_transition("cancelled", "pause") is False

    def test_resume_only_from_paused(self):
        assert can_transition("paused", "resume") is True
        assert can_transition("active", "resume") is False
        assert can_transition("cancelled", "resume") is False

    def test_abort_from_active_or_paused(self):
        assert can_transition("active", "abort") is True
        assert can_transition("paused", "abort") is True
        assert can_transition("completed", "abort") is False
        assert can_transition("cancelled", "abort") is False

    def test_unknown_action_never_allowed(self):
        assert can_transition("active", "explode") is False

    def test_allowed_transitions_shape(self):
        assert {
            "pause": {"active"},
            "resume": {"paused"},
            "abort": {"active", "paused"},
        } == ALLOWED_TRANSITIONS
