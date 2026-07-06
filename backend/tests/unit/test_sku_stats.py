"""Unit tests for derived SKU stats (Phase 2)."""

from datetime import datetime, timedelta

from backend.app.services.sku_catalog import compute_stats_from_rows, median_cycle_seconds

_BASE = datetime(2026, 7, 4, 12, 0, 0)


def _row(status="completed", upp=1, printer_id=1, started_offset=None):
    started = _BASE + timedelta(seconds=started_offset) if started_offset is not None else None
    return {"status": status, "units_per_plate": upp, "printer_id": printer_id, "started_at": started}


class TestComputeStats:
    def test_completed_and_failed_counts_and_units(self):
        rows = [
            _row("completed", upp=3),
            _row("completed", upp=3),
            _row("failed", upp=3),
            _row("pending", upp=3),
        ]
        stats = compute_stats_from_rows(rows)
        assert stats["plates_completed"] == 2
        assert stats["plates_failed"] == 1
        assert stats["units_completed"] == 6  # 2 plates × 3
        assert stats["units_failed"] == 3
        # success_rate = 2 / (2 + 1)
        assert abs(stats["success_rate"] - (2 / 3)) < 1e-9

    def test_zero_terminal_plates_success_rate_zero(self):
        rows = [_row("pending"), _row("printing")]
        stats = compute_stats_from_rows(rows)
        assert stats["plates_completed"] == 0
        assert stats["plates_failed"] == 0
        assert stats["units_completed"] == 0
        assert stats["success_rate"] == 0.0
        assert stats["median_cycle_seconds"] is None

    def test_empty_rows_all_zero(self):
        stats = compute_stats_from_rows([])
        assert stats["units_completed"] == 0
        assert stats["units_failed"] == 0
        assert stats["plates_completed"] == 0
        assert stats["plates_failed"] == 0
        assert stats["success_rate"] == 0.0
        assert stats["median_cycle_seconds"] is None

    def test_units_per_plate_defaults_to_one(self):
        rows = [_row("completed", upp=None)]
        stats = compute_stats_from_rows(rows)
        assert stats["units_completed"] == 1


class TestMedianCycle:
    def test_single_start_no_gap(self):
        assert median_cycle_seconds([_row(started_offset=0)]) is None

    def test_two_starts_one_gap(self):
        rows = [_row(started_offset=0), _row(started_offset=100)]
        assert median_cycle_seconds(rows) == 100

    def test_median_of_gaps_same_printer(self):
        # starts at 0, 100, 400 -> gaps 100, 300 -> median 200
        rows = [_row(started_offset=0), _row(started_offset=100), _row(started_offset=400)]
        assert median_cycle_seconds(rows) == 200

    def test_gaps_are_per_printer_then_pooled(self):
        # printer 1: 0,100 -> gap 100 ; printer 2: 0,300 -> gap 300 ; median 200
        rows = [
            _row(printer_id=1, started_offset=0),
            _row(printer_id=1, started_offset=100),
            _row(printer_id=2, started_offset=0),
            _row(printer_id=2, started_offset=300),
        ]
        assert median_cycle_seconds(rows) == 200

    def test_rows_without_started_at_ignored(self):
        rows = [_row(started_offset=None), _row(started_offset=0), _row(started_offset=50)]
        assert median_cycle_seconds(rows) == 50
