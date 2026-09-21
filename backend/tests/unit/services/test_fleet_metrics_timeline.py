"""Tests for the pure fleet timeline sweep (services.fleet_metrics.timeline).

The sweep's contract is a tiling: for every printer the window knows of, the intervals
cover ``[window start, min(window end, now))`` exactly once, with no gap and no
overlap, whatever the rows underneath look like. Every identity the projections rest
on is a consequence of that, so most of this file is a property check applied to
awkward inputs — a clock that stepped backwards into an overlap, an open span that has
gone stale, a printer deleted while an incident was still open, a window that runs
past *now*.

The calendar cases are driven in ``America/New_York`` across both 2026 transitions,
because a 23 h and a 25 h day are the only way to tell a real grid from one that adds
24 h to a start.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from backend.app.models.printer_incident import KIND_JAM, KIND_SERVICE_HOLD
from backend.app.models.printer_observation_span import PLATE_PHASE_CLEAR
from backend.app.services import fleet_activity
from backend.app.services.fleet_metrics.classifier import (
    DOWN_OFFLINE,
    IDLE,
    NOT_RECORDED,
    OUT_OF_FLEET,
    PLANNED,
    PRINTING,
    UNOBSERVED,
    down_fault,
)
from backend.app.services.fleet_metrics.timeline import (
    IncidentRow,
    PrinterEvidence,
    RosterPrinter,
    SpanRow,
    build_timeline,
    build_window,
    printer_slice,
)

NY = ZoneInfo("America/New_York")
HOUR = timedelta(hours=1)
STALE_AFTER = timedelta(seconds=fleet_activity.STALE_AFTER_S)

# 2026-09-01 00:00 in New York (EDT, UTC−4), as the naive UTC the tables store.
SEP1 = datetime(2026, 9, 1, 4, 0, 0)


def _span(printer_id: int, start: datetime, end: datetime | None, *, last: datetime | None = None, **overrides):
    """A closed (or open) span reading as a printing, healthy printer."""
    fields = {
        "is_active": True,
        "connected": True,
        "gcode_state": "RUNNING",
        "plate_phase": PLATE_PHASE_CLEAR,
        "quarantined": False,
        "usb_present": True,
        "model_mismatch": False,
    }
    fields.update(overrides)
    return SpanRow(
        printer_id=printer_id,
        started_at=start,
        last_observed_at=last if last is not None else (end if end is not None else start),
        ended_at=end,
        **fields,  # type: ignore[arg-type]
    )


def _roster(*printer_ids: int, is_active: bool = True):
    return [
        RosterPrinter(printer_id=identifier, name=f"{identifier:03d}-H2S", model="H2S", is_active=is_active)
        for identifier in printer_ids
    ]


def _evidence(printer_id: int, *, first=None, last_span=None, last_incident=None):
    return PrinterEvidence(
        printer_id=printer_id,
        first_span_start=first,
        last_span_end=last_span,
        last_incident_at=last_incident,
    )


def _build(*, window, now, roster=(), spans=(), incidents=(), evidence=()):
    return build_timeline(
        window=window,
        now=now,
        roster=list(roster),
        spans=list(spans),
        incidents=list(incidents),
        evidence=list(evidence),
    )


def _keys(entry) -> list[tuple[datetime, datetime, str]]:
    return [(interval.start, interval.end, interval.klass.key) for interval in entry.intervals]


def _assert_tiles(entry, start: datetime, end: datetime) -> None:
    """The contract: gapless, non-overlapping, and covering exactly ``[start, end)``."""
    if start >= end:
        assert entry.intervals == ()
        return
    assert entry.intervals[0].start == start
    assert entry.intervals[-1].end == end
    # Pairwise, so the second sequence is deliberately one shorter.
    for left, right in zip(entry.intervals, entry.intervals[1:], strict=False):
        assert left.end == right.start
    assert all(interval.end > interval.start for interval in entry.intervals)


class TestTheSiteCalendar:
    """The grid is the site's, and a transition day is not 24 h."""

    def test_a_spring_forward_day_is_23_hours_and_a_fall_back_day_is_25(self):
        spring = build_window(date(2026, 3, 8), date(2026, 3, 8), "day", NY)
        autumn = build_window(date(2026, 11, 1), date(2026, 11, 1), "day", NY)
        timelines = [
            _build(window=window, now=window.end, roster=_roster(1), evidence=[_evidence(1)])
            for window in (spring, autumn)
        ]
        assert timelines[0].headers[0].seconds == 23 * 3600
        assert timelines[1].headers[0].seconds == 25 * 3600
        # ...and the whole window is still classified, at its real width.
        for timeline, width in zip(timelines, (23 * 3600, 25 * 3600), strict=True):
            entry = timeline.printers[0]
            assert sum(interval.seconds for interval in entry.intervals) == width

    def test_an_hour_grid_has_23_and_25_cells_on_those_days(self):
        spring = build_window(date(2026, 3, 8), date(2026, 3, 8), "hour", NY)
        autumn = build_window(date(2026, 11, 1), date(2026, 11, 1), "hour", NY)
        assert len(spring.grid.buckets) == 23
        assert len(autumn.grid.buckets) == 25

    def test_a_week_window_cuts_on_days_so_the_day_to_week_fold_is_exact(self):
        window = build_window(date(2026, 3, 2), date(2026, 3, 15), "week", NY)
        assert len(window.grid.base) == 14
        assert len(window.grid.buckets) == 2
        assert sum(edge.seconds for edge in window.grid.base) == sum(edge.seconds for edge in window.grid.buckets)

    def test_a_span_is_split_at_site_midnight_even_when_nothing_changed(self):
        window = build_window(date(2026, 9, 1), date(2026, 9, 2), "day", NY)
        timeline = _build(
            window=window,
            now=window.end,
            roster=_roster(1),
            spans=[_span(1, SEP1, SEP1 + timedelta(days=2))],
            evidence=[_evidence(1, first=SEP1, last_span=SEP1 + timedelta(days=2))],
        )
        assert _keys(timeline.printers[0]) == [
            (SEP1, SEP1 + timedelta(days=1), PRINTING.key),
            (SEP1 + timedelta(days=1), SEP1 + timedelta(days=2), PRINTING.key),
        ]


class TestCoverage:
    """What a span is evidence for, and what the absence of one is evidence for."""

    def test_neighbouring_spans_of_equal_class_merge_into_one_interval(self):
        # Six consecutive readings that all classify the same are ONE outage in the
        # drill-down, not six rows — a span change that changes no class must not split.
        window = build_window(date(2026, 9, 1), date(2026, 9, 1), "day", NY)
        spans = [
            _span(1, SEP1 + index * HOUR, SEP1 + (index + 1) * HOUR, connected=False, gcode_state=None)
            for index in range(6)
        ]
        timeline = _build(
            window=window,
            now=window.end,
            roster=_roster(1),
            spans=spans,
            evidence=[_evidence(1, first=SEP1, last_span=SEP1 + 6 * HOUR)],
        )
        assert _keys(timeline.printers[0])[0] == (SEP1, SEP1 + 6 * HOUR, DOWN_OFFLINE.key)

    def test_a_fresh_open_span_covers_up_to_now_and_a_stale_one_stops_at_its_last_sample(self):
        window = build_window(date(2026, 9, 1), date(2026, 9, 1), "day", NY)
        now = SEP1 + 12 * HOUR
        fresh = _span(1, SEP1, None, last=now - timedelta(seconds=30))
        stale = _span(2, SEP1, None, last=SEP1 + 2 * HOUR)
        timeline = _build(
            window=window,
            now=now,
            roster=_roster(1, 2),
            spans=[fresh, stale],
            evidence=[
                _evidence(1, first=SEP1, last_span=now - timedelta(seconds=30)),
                _evidence(2, first=SEP1, last_span=SEP1 + 2 * HOUR),
            ],
        )
        assert _keys(printer_slice(timeline, 1)) == [(SEP1, now, PRINTING.key)]
        # The hole the stale span leaves is the honest record of a restart.
        assert _keys(printer_slice(timeline, 2)) == [
            (SEP1, SEP1 + 2 * HOUR, PRINTING.key),
            (SEP1 + 2 * HOUR, now, UNOBSERVED.key),
        ]

    def test_the_staleness_bound_is_the_recorders_own(self):
        window = build_window(date(2026, 9, 1), date(2026, 9, 1), "day", NY)
        now = SEP1 + 12 * HOUR
        timeline = _build(
            window=window,
            now=now,
            roster=_roster(1),
            spans=[_span(1, SEP1, None, last=now - STALE_AFTER)],
            evidence=[_evidence(1, first=SEP1)],
        )
        assert _keys(timeline.printers[0]) == [(SEP1, now, PRINTING.key)]

    def test_overlapping_spans_are_clipped_to_the_earlier_ones_end(self):
        # A clock stepped backwards can leave two spans overlapping; the result must
        # still be a tiling, and the earlier reading owns the contested stretch.
        window = build_window(date(2026, 9, 1), date(2026, 9, 1), "day", NY)
        spans = [
            _span(1, SEP1, SEP1 + 4 * HOUR),
            _span(1, SEP1 + 2 * HOUR, SEP1 + 6 * HOUR, connected=False, gcode_state=None),
        ]
        timeline = _build(
            window=window,
            now=window.end,
            roster=_roster(1),
            spans=spans,
            evidence=[_evidence(1, first=SEP1, last_span=SEP1 + 6 * HOUR)],
        )
        entry = timeline.printers[0]
        assert _keys(entry)[:2] == [
            (SEP1, SEP1 + 4 * HOUR, PRINTING.key),
            (SEP1 + 4 * HOUR, SEP1 + 6 * HOUR, DOWN_OFFLINE.key),
        ]
        _assert_tiles(entry, window.start, window.end)

    def test_before_a_printers_first_span_the_window_reads_not_recorded(self):
        window = build_window(date(2026, 9, 1), date(2026, 9, 1), "day", NY)
        first = SEP1 + 6 * HOUR
        timeline = _build(
            window=window,
            now=window.end,
            roster=_roster(1),
            spans=[_span(1, first, window.end)],
            evidence=[_evidence(1, first=first, last_span=window.end)],
        )
        assert _keys(timeline.printers[0]) == [
            (SEP1, first, NOT_RECORDED.key),
            (first, window.end, PRINTING.key),
        ]

    def test_a_roster_printer_with_no_rows_at_all_is_not_recorded_throughout(self):
        window = build_window(date(2026, 9, 1), date(2026, 9, 1), "day", NY)
        timeline = _build(window=window, now=window.end, roster=_roster(1), evidence=[_evidence(1)])
        assert _keys(timeline.printers[0]) == [(SEP1, window.end, NOT_RECORDED.key)]

    def test_a_gap_after_recording_began_is_unobserved_not_not_recorded(self):
        window = build_window(date(2026, 9, 1), date(2026, 9, 1), "day", NY)
        timeline = _build(
            window=window,
            now=window.end,
            roster=_roster(1),
            spans=[_span(1, SEP1, SEP1 + 2 * HOUR), _span(1, SEP1 + 5 * HOUR, window.end)],
            evidence=[_evidence(1, first=SEP1, last_span=window.end)],
        )
        assert _keys(timeline.printers[0])[1] == (SEP1 + 2 * HOUR, SEP1 + 5 * HOUR, UNOBSERVED.key)


class TestIncidents:
    """Durable holds, clamped to their own life and to the window."""

    def test_an_open_incident_is_clamped_at_now_and_nothing_is_classified_after_it(self):
        window = build_window(date(2026, 9, 1), date(2026, 9, 2), "day", NY)
        now = SEP1 + 30 * HOUR
        timeline = _build(
            window=window,
            now=now,
            roster=_roster(1),
            incidents=[IncidentRow(id=1, printer_id=1, kind=KIND_JAM, created_at=SEP1 + 6 * HOUR, resolved_at=None)],
            evidence=[_evidence(1, last_incident=SEP1 + 6 * HOUR)],
        )
        entry = timeline.printers[0]
        assert entry.intervals[-1].end == now
        _assert_tiles(entry, window.start, now)
        assert _keys(entry)[1] == (SEP1 + 6 * HOUR, SEP1 + timedelta(days=1), down_fault(KIND_JAM).key)

    def test_a_hold_with_no_observation_still_reads_from_the_ledger(self):
        window = build_window(date(2026, 9, 1), date(2026, 9, 1), "day", NY)
        timeline = _build(
            window=window,
            now=window.end,
            roster=_roster(1),
            incidents=[
                IncidentRow(
                    id=1,
                    printer_id=1,
                    kind=KIND_SERVICE_HOLD,
                    created_at=SEP1 + 2 * HOUR,
                    resolved_at=SEP1 + 4 * HOUR,
                )
            ],
            evidence=[_evidence(1, last_incident=SEP1 + 2 * HOUR)],
        )
        assert _keys(timeline.printers[0]) == [
            (SEP1, SEP1 + 2 * HOUR, NOT_RECORDED.key),
            (SEP1 + 2 * HOUR, SEP1 + 4 * HOUR, PLANNED.key),
            (SEP1 + 4 * HOUR, window.end, NOT_RECORDED.key),
        ]

    def test_a_printer_known_only_from_an_incident_is_still_counted(self):
        window = build_window(date(2026, 9, 1), date(2026, 9, 1), "day", NY)
        timeline = _build(
            window=window,
            now=window.end,
            incidents=[IncidentRow(id=1, printer_id=7, kind=KIND_JAM, created_at=SEP1, resolved_at=None)],
            evidence=[_evidence(7, last_incident=SEP1)],
        )
        assert timeline.printers_known == 1
        assert timeline.printers[0].deleted is True
        assert timeline.printers[0].name == "7"


class TestLifecycle:
    """Printers arrive, are deactivated, and are deleted — each reads differently."""

    def test_a_printer_deactivated_mid_bucket_splits_at_the_span_edge(self):
        window = build_window(date(2026, 9, 1), date(2026, 9, 1), "day", NY)
        spans = [
            _span(1, SEP1, SEP1 + 8 * HOUR, gcode_state="IDLE"),
            _span(1, SEP1 + 8 * HOUR, window.end, is_active=False, connected=False, gcode_state=None),
        ]
        timeline = _build(
            window=window,
            now=window.end,
            roster=_roster(1, is_active=False),
            spans=spans,
            evidence=[_evidence(1, first=SEP1, last_span=window.end)],
        )
        assert _keys(timeline.printers[0]) == [
            (SEP1, SEP1 + 8 * HOUR, IDLE.key),
            (SEP1 + 8 * HOUR, window.end, OUT_OF_FLEET.key),
        ]

    def test_a_deleted_printer_leaves_the_fleet_after_its_last_evidence(self):
        window = build_window(date(2026, 9, 1), date(2026, 9, 2), "day", NY)
        last = SEP1 + 10 * HOUR
        timeline = _build(
            window=window,
            now=window.end,
            roster=_roster(2),
            spans=[_span(1, SEP1, last, gcode_state="IDLE")],
            evidence=[_evidence(1, first=SEP1, last_span=last), _evidence(2)],
        )
        entry = printer_slice(timeline, 1)
        assert entry.deleted is True
        assert entry.name == "1"
        assert _keys(entry) == [
            (SEP1, last, IDLE.key),
            (last, SEP1 + timedelta(days=1), OUT_OF_FLEET.key),
            (SEP1 + timedelta(days=1), window.end, OUT_OF_FLEET.key),
        ]

    def test_an_orphaned_open_incident_does_not_hold_a_deleted_printer_down_forever(self):
        # Deleting a printer does not close its incident rows. Without the last-evidence
        # rule that open row would read as downtime in every window from now on, and a
        # fleet average would carry a machine that no longer exists.
        window = build_window(date(2026, 9, 1), date(2026, 9, 2), "day", NY)
        opened = SEP1 + 3 * HOUR
        last_span = SEP1 + 5 * HOUR
        timeline = _build(
            window=window,
            now=window.end,
            roster=_roster(2),
            spans=[_span(1, SEP1, last_span, gcode_state="IDLE")],
            incidents=[IncidentRow(id=1, printer_id=1, kind=KIND_JAM, created_at=opened, resolved_at=None)],
            evidence=[_evidence(1, first=SEP1, last_span=last_span, last_incident=opened), _evidence(2)],
        )
        entry = printer_slice(timeline, 1)
        assert _keys(entry)[:3] == [
            (SEP1, opened, IDLE.key),
            (opened, last_span, down_fault(KIND_JAM).key),
            (last_span, SEP1 + timedelta(days=1), OUT_OF_FLEET.key),
        ]
        assert entry.intervals[-1].klass is OUT_OF_FLEET


class TestTheTilingContract:
    """The property every identity downstream rests on, over awkward inputs."""

    @pytest.mark.parametrize("bucket", ["hour", "day"])
    def test_every_printer_tiles_the_elapsed_window_exactly(self, bucket: str):
        window = build_window(date(2026, 9, 1), date(2026, 9, 2), bucket, NY)
        now = SEP1 + 30 * HOUR
        timeline = _build(
            window=window,
            now=now,
            roster=_roster(1, 2, 3),
            spans=[
                _span(1, SEP1 - 2 * HOUR, SEP1 + 9 * HOUR),
                _span(1, SEP1 + 9 * HOUR, None, last=now - timedelta(seconds=15)),
                _span(2, SEP1 + 6 * HOUR, SEP1 + 7 * HOUR, gcode_state="PAUSE"),
            ],
            incidents=[
                IncidentRow(id=1, printer_id=2, kind=KIND_JAM, created_at=SEP1 + 6 * HOUR, resolved_at=None),
                IncidentRow(
                    id=2,
                    printer_id=3,
                    kind=KIND_SERVICE_HOLD,
                    created_at=SEP1 - HOUR,
                    resolved_at=SEP1 + 20 * HOUR,
                ),
            ],
            evidence=[_evidence(1, first=SEP1 - 2 * HOUR), _evidence(2, first=SEP1 + 6 * HOUR), _evidence(3)],
        )
        for entry in timeline.printers:
            _assert_tiles(entry, window.start, now)
            assert sum(interval.seconds for interval in entry.intervals) == (now - window.start).total_seconds()

    def test_nothing_is_classified_after_now_even_for_a_future_window(self):
        window = build_window(date(2026, 9, 1), date(2026, 9, 3), "day", NY)
        now = SEP1 + 6 * HOUR
        timeline = _build(
            window=window,
            now=now,
            roster=_roster(1),
            spans=[_span(1, SEP1, None, last=now)],
            evidence=[_evidence(1, first=SEP1)],
        )
        assert timeline.printers[0].intervals[-1].end == now
        # The future buckets still exist, with a width and no elapsed time.
        assert [header.elapsed_seconds for header in timeline.headers] == [6 * 3600.0, 0.0, 0.0]
        assert [header.seconds for header in timeline.headers] == [86400.0, 86400.0, 86400.0]

    def test_observed_seconds_are_a_union_across_printers_never_a_sum(self):
        window = build_window(date(2026, 9, 1), date(2026, 9, 1), "day", NY)
        timeline = _build(
            window=window,
            now=window.end,
            roster=_roster(1, 2),
            spans=[_span(1, SEP1, SEP1 + 4 * HOUR), _span(2, SEP1 + 2 * HOUR, SEP1 + 6 * HOUR)],
            evidence=[_evidence(1, first=SEP1), _evidence(2, first=SEP1 + 2 * HOUR)],
        )
        # Two printers, four observed hours each, two of them the same hours: the farm
        # was watched for six hours, not eight.
        assert timeline.headers[0].observed_seconds == 6 * 3600
        assert timeline.headers[0].basis == "observed"

    def test_a_window_nothing_observed_reads_incidents_only(self):
        window = build_window(date(2026, 9, 1), date(2026, 9, 1), "day", NY)
        timeline = _build(
            window=window,
            now=window.end,
            roster=_roster(1),
            incidents=[IncidentRow(id=1, printer_id=1, kind=KIND_JAM, created_at=SEP1, resolved_at=None)],
            evidence=[_evidence(1, last_incident=SEP1)],
        )
        assert timeline.headers[0].observed_seconds == 0.0
        assert timeline.headers[0].basis == "incidents_only"
        assert timeline.total.basis == "incidents_only"
