"""Unit tests for the per-run filament helper (#1378, #1390).

The helper computes what value to write into PrintLogEntry.filament_used_grams
for a given print event — partial-aware so failed / cancelled / stopped prints
don't inflate stats with the full slicer estimate, and tracker-aware so
completed prints agree with the per-spool counter on the Inventory page.

Keyed on the run's CHARGE BASIS (2026-09-25), the same one the spool ledger is
charged on, so Stats and the ledger tell one story: ``full`` for a print the
printer finished, ``partial`` for one it ended early (scaled by the job's own
last progress, off its terminal payload), ``none`` for a run nobody measured.
"""

from backend.app.services.job_terminal import compute_run_filament_grams


class TestComputeRunFilamentGrams:
    def test_completed_no_tracker_returns_archive_estimate(self):
        # Completed print without inventory tracking: the slicer estimate is
        # the canonical "this print used X" value.
        assert compute_run_filament_grams("full", 100.0, 100, []) == 100.0

    def test_completed_prefers_tracked_over_estimate(self):
        # #1390: when inventory tracked the AMS weight delta, Stats should
        # reflect that — same source that drives "Total Consumed" on the
        # Inventory page. Two halves of the app must show the same number.
        assert compute_run_filament_grams("full", 100.0, 100, [{"weight_used": 96.5}]) == 96.5

    def test_failed_uses_tracked_spool_delta(self):
        # Failed reprint at 10g actual: inventory tracked the spool delta.
        # The estimate was 100g; we want 10g recorded for stats.
        assert compute_run_filament_grams("partial", 100.0, 10, [{"weight_used": 10.0}]) == 10.0

    def test_cancelled_uses_tracked_spool_delta(self):
        # Same logic for cancelled.
        assert compute_run_filament_grams("partial", 100.0, 12, [{"weight_used": 8.5}]) == 8.5

    def test_stopped_uses_tracked_spool_delta(self):
        assert compute_run_filament_grams("partial", 100.0, 15, [{"weight_used": 12.0}]) == 12.0

    def test_failed_with_no_tracked_falls_back_to_progress_scale(self):
        # No inventory tracking: scale estimate by progress% (10% of 100g = 10g).
        assert compute_run_filament_grams("partial", 100.0, 10, []) == 10.0

    def test_failed_with_no_tracked_and_no_progress_returns_none(self):
        # Nothing to infer from — return None rather than guess the estimate.
        assert compute_run_filament_grams("partial", 100.0, 0, []) is None

    def test_failed_with_partial_progress_rounds_correctly(self):
        # 100g × 33% = 33.0g (rounded to 1 decimal)
        assert compute_run_filament_grams("partial", 100.0, 33, []) == 33.0

    def test_failed_with_no_estimate_returns_none(self):
        # No estimate, no tracked usage → can't compute anything.
        assert compute_run_filament_grams("partial", None, 50, []) is None

    def test_failed_with_no_estimate_but_tracked_uses_tracked(self):
        # Tracked spool delta is authoritative even without an estimate.
        assert compute_run_filament_grams("partial", None, 50, [{"weight_used": 5.0}]) == 5.0

    def test_tracked_overrides_progress_scale_when_both_available(self):
        # If inventory says 8g but progress says 15g, trust inventory (it's measured).
        assert compute_run_filament_grams("partial", 100.0, 15, [{"weight_used": 8.0}]) == 8.0

    def test_progress_above_100_clamps_to_full_estimate(self):
        # Defensive: progress overshoot doesn't multiply past the estimate.
        assert compute_run_filament_grams("partial", 100.0, 150, []) == 100.0

    def test_multiple_tracked_slots_summed(self):
        # Multi-filament print, two slots tracked.
        usage = [{"weight_used": 5.0}, {"weight_used": 3.5}, {"weight_used": 1.0}]
        assert compute_run_filament_grams("partial", 100.0, 20, usage) == 9.5

    def test_completed_with_none_estimate_returns_none(self):
        # Archive somehow has no estimate (rare; archive_print parsed nothing).
        assert compute_run_filament_grams("full", None, 100, []) is None

    def test_none_records_no_grams_whatever_the_estimate_and_progress_say(self):
        """A run nobody measured — the reconcile's unknown outcome, a job joined mid-way with no peak
        read — states no grams, exactly as it is charged nothing."""
        assert compute_run_filament_grams("none", 100.0, 60, []) is None
        assert compute_run_filament_grams("none", 100.0, None, None) is None

    def test_none_still_reports_what_a_tracker_measured(self):
        """The tracked delta is a measurement whatever the basis (a none basis charges nothing, so in
        practice there is none to report)."""
        assert compute_run_filament_grams("none", 100.0, None, [{"weight_used": 4.0}]) == 4.0

    def test_partial_with_no_progress_reading_states_no_grams(self):
        """``last_progress`` None — no terminal peak reached this lane."""
        assert compute_run_filament_grams("partial", 100.0, None, []) is None
