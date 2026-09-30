"""THE job-identity comparison and its held-job reading (``services/job_identity``).

``same_job`` has three answers because an absent id is not evidence of a different job;
``is_held_job`` is the ONE caller-independent reading of ``unknown`` — "is this the job a
hold recorded" — shared by the rule table's job-pause cells and both terminal classifiers
(2026-09-29). Pure functions, so the tables below are the whole contract.
"""

import pytest

from backend.app.services.job_identity import is_held_job, same_job


class TestSameJob:
    @pytest.mark.parametrize(
        ("live", "record", "expected"),
        [
            ("JOB-7", "JOB-7", "same"),
            (" JOB-7 ", "JOB-7", "same"),
            ("JOB-7", "JOB-8", "other"),
            ("JOB-7", "", "unknown"),
            ("0", "JOB-7", "unknown"),
            (None, None, "unknown"),
            ("", "0", "unknown"),
        ],
    )
    def test_three_answers(self, live, record, expected):
        assert same_job(live, record) == expected


class TestIsHeldJob:
    """``same`` holds; ``other`` never does; ``unknown`` holds only when BOTH sides name no job."""

    @pytest.mark.parametrize(
        ("live", "recorded", "held"),
        [
            ("JOB-7", "JOB-7", True),
            (" JOB-7 ", "JOB-7", True),
            ("JOB-7", "JOB-8", False),
            # An id on ONE side is a different job, never a match.
            ("JOB-7", "", False),
            ("JOB-7", "0", False),
            ("", "JOB-7", False),
            ("0", "JOB-7", False),
            (None, "JOB-7", False),
            # Both id-less: the same id-less job, whichever spelling each side used.
            ("", "", True),
            ("0", "0", True),
            ("0", "", True),
            ("", "0", True),
            (None, "", True),
            (None, None, True),
            (" 0 ", None, True),
        ],
        ids=[
            "same",
            "same-padded",
            "other",
            "record-none",
            "record-zero",
            "live-none",
            "live-zero",
            "live-None",
            "both-empty",
            "both-zero",
            "zero-vs-empty",
            "empty-vs-zero",
            "None-vs-empty",
            "both-None",
            "padded-zero-vs-None",
        ],
    )
    def test_the_table(self, live, recorded, held):
        assert is_held_job(live, recorded) is held

    @pytest.mark.parametrize(("a", "b"), [("JOB-7", "JOB-7"), ("JOB-7", ""), ("0", ""), ("JOB-7", "JOB-8")])
    def test_it_is_symmetric(self, a, b):
        assert is_held_job(a, b) is is_held_job(b, a)
