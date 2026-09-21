"""Tests for the fleet-metrics projections (services.fleet_metrics.projections).

Nine identities hold this page together — they are listed in the module's own
docstring, and they are what lets an operator check a headline against the matrix
under it. This suite pins every one of them twice: once on a hand-built scenario
whose numbers a reader can work out on paper, and once on a seeded pseudo-random fleet
where nobody chose the arrangement. A projection can be wrong in a way that still
looks plausible; it cannot be wrong and keep the identities.

The rest of the file drives the cases where a figure must be WITHHELD rather than
computed — a bucket nothing observed, a denominator that is zero, an episode that
never finished — because a dashboard's dangerous failure is a confident number, not a
missing one.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from backend.app.models.farm_cycle_episode import (
    COOLDOWN_VARIANT_FAN_ONLY,
    COOLDOWN_VARIANT_HOLD,
    KIND_COOLDOWN,
    KIND_EJECT,
)
from backend.app.models.printer_incident import KIND_JAM, KIND_RUNOUT, KIND_SERVICE_HOLD
from backend.app.models.printer_observation_span import PLATE_PHASE_CLEAR, PLATE_PHASE_COOLING, PLATE_PHASE_HELD
from backend.app.services.fleet_metrics import projections
from backend.app.services.fleet_metrics.classifier import (
    GROUP_DOWN,
    GROUP_NOT_RECORDED,
    GROUP_OUT_OF_FLEET,
    GROUP_PLANNED,
    GROUP_UNOBSERVED,
    fault_kind_of,
)
from backend.app.services.fleet_metrics.timeline import (
    IncidentRow,
    PrinterEvidence,
    RosterPrinter,
    SpanRow,
    build_timeline,
    build_window,
)
from backend.app.utils.site_time import previous_window

NY = ZoneInfo("America/New_York")
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)

# 2026-09-01 00:00 in New York, as the naive UTC the tables store.
SEP1 = datetime(2026, 9, 1, 4, 0, 0)

_STATE_ROW_KEYS = (
    projections.ROW_AVG_PRINTING,
    projections.ROW_AVG_CYCLE_OVERHEAD,
    projections.ROW_AVG_IDLE,
    projections.ROW_AVG_DOWN,
    projections.ROW_AVG_PLANNED,
    projections.ROW_PEAK_DOWN,
    projections.ROW_PRINTERS_IN_FLEET,
    projections.ROW_UPTIME,
    projections.ROW_TIME_PRINTING,
)


def _span(printer_id: int, start: datetime, end: datetime | None, *, last: datetime | None = None, **overrides):
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


def _roster(*printer_ids: int):
    return [
        RosterPrinter(printer_id=identifier, name=f"{identifier:03d}-H2S", model="H2S", is_active=True)
        for identifier in printer_ids
    ]


def _timeline(window, now, *, roster=(), spans=(), incidents=(), evidence=None, history_since=None):
    """Build a timeline, deriving the whole-table evidence from the spans given."""
    if evidence is None:
        facts: dict[int, list[datetime]] = {}
        for span in spans:
            facts.setdefault(span.printer_id, []).append(span.started_at)
        holds: dict[int, list[datetime]] = {}
        for row in incidents:
            holds.setdefault(row.printer_id, []).append(row.created_at)
        evidence = [
            PrinterEvidence(
                printer_id=identifier,
                first_span_start=min(facts[identifier]) if identifier in facts else None,
                last_span_end=max(
                    (span.ended_at or span.last_observed_at for span in spans if span.printer_id == identifier),
                    default=None,
                ),
                last_incident_at=max(holds[identifier]) if identifier in holds else None,
            )
            for identifier in sorted(set(facts) | set(holds) | {entry.printer_id for entry in roster})
        ]
    return build_timeline(
        window=window,
        now=now,
        roster=list(roster),
        spans=list(spans),
        incidents=list(incidents),
        evidence=list(evidence),
        history_since=history_since,
    )


def _assert_identities(timeline, totals, fleet, matrix, recovery):
    """Every identity in the module docstring, checked against one built window.

    The state identities are asserted INSIDE THE OBSERVED MASK, which is where the
    figures are measured: the mask's length per bucket is ``observed_seconds``, and the
    clipped class seconds are what the averages divide by it. An ``incidents_only``
    bucket measures nothing, so it is checked for what it must WITHHOLD instead.
    """
    known = timeline.printers_known
    for index, bucket in enumerate(fleet.buckets):
        masked = totals.fleet_masked[index]
        observed = bucket.observed_seconds
        values = bucket.values

        if observed > 0:
            # (a) inside the mask every known printer accounts for exactly |M| seconds
            # across all classes — including printers that had no span of their own
            # there, which read as unobserved.
            assert sum(masked.values()) == pytest.approx(known * observed)
            assert sum(values.avg_by_group.values()) == pytest.approx(float(known))
            for printer in timeline.printers:
                own = sum(totals.per_printer_masked[printer.printer_id][index].values())
                assert own == pytest.approx(observed), printer.printer_id
            # (b) in-fleet is the roster less the two "not counted" groups.
            assert values.printers_in_fleet == pytest.approx(
                known
                - values.avg_by_group.get(GROUP_OUT_OF_FLEET, 0.0)
                - values.avg_by_group.get(GROUP_NOT_RECORDED, 0.0)
            )
            # (c) the causes account for all of down.
            assert sum(values.avg_down_by_cause.values()) == pytest.approx(values.avg_down)
            # (d) ...and so do the printers, one by one, over the same mask.
            per_printer = sum(
                seconds
                for printer in timeline.printers
                for klass, seconds in totals.per_printer_masked[printer.printer_id][index].items()
                if klass.is_down
            )
            assert per_printer / observed == pytest.approx(values.avg_down)
            # (f) an average can never exceed the peak, nor the peak the fleet.
            assert values.avg_down <= values.peak_down + 1e-9
        elif bucket.elapsed_seconds > 0:
            # An unwatched bucket reports the ledger and nothing else.
            assert values.uptime is None
            assert values.time_printing is None
            assert set(values.avg_by_group) <= {GROUP_DOWN, GROUP_PLANNED}
        assert values.peak_down <= known

    # (h) one printer's intervals sum, per class, to its own MATRIX cell — the full
    # elapsed fold, ledger included, which is the matrix's own coverage.
    for printer in timeline.printers:
        by_key: dict[str, float] = {}
        for interval in printer.intervals:
            by_key[interval.klass.key] = by_key.get(interval.klass.key, 0.0) + interval.seconds
        assert matrix.series.totals.printers[printer.printer_id].class_seconds == {
            key: value for key, value in by_key.items() if value
        }

    # (g) observed fault downtime can never exceed the ledger's own held time — a
    # printer that went on printing under an open hold contributes to the second and
    # not to the first.
    fault_seconds = sum(
        seconds for cell in totals.fleet for klass, seconds in cell.items() if fault_kind_of(klass) is not None
    )
    assert fault_seconds <= recovery.fault_open_seconds + 1e-6

    # (e) the window totals are the SUMS re-divided, never an average of averages.
    if timeline.total.observed_seconds > 0:
        assert sum(totals.fleet_masked_window.values()) == pytest.approx(known * timeline.total.observed_seconds)
        assert sum(fleet.totals.avg_by_group.values()) == pytest.approx(float(known))
        for group, average in fleet.totals.avg_by_group.items():
            summed = sum(
                seconds for cell in totals.fleet_masked for klass, seconds in cell.items() if klass.group == group
            )
            assert average == pytest.approx(summed / timeline.total.observed_seconds)


class TestIdentitiesOnAHandBuiltFleet:
    """Three printers, one of everything, over two days a reader can add up."""

    @pytest.fixture
    def built(self):
        window = build_window(date(2026, 9, 1), date(2026, 9, 2), "day", NY)
        now = window.end
        spans = [
            # 001 prints all day one, then cools, then is offline through day two.
            _span(1, SEP1, SEP1 + 20 * HOUR),
            _span(1, SEP1 + 20 * HOUR, SEP1 + 24 * HOUR, gcode_state="FINISH", plate_phase=PLATE_PHASE_COOLING),
            _span(1, SEP1 + 24 * HOUR, now, connected=False, gcode_state=None),
            # 002 is idle, then waits on a person with a part on the plate.
            _span(2, SEP1, SEP1 + 30 * HOUR, gcode_state="IDLE"),
            _span(2, SEP1 + 30 * HOUR, now, gcode_state="IDLE", plate_phase=PLATE_PHASE_HELD),
            # 003 starts recording six hours late and is then deactivated.
            _span(3, SEP1 + 6 * HOUR, SEP1 + 36 * HOUR, gcode_state="IDLE"),
            _span(3, SEP1 + 36 * HOUR, now, is_active=False, connected=False, gcode_state=None),
        ]
        incidents = [
            IncidentRow(id=1, printer_id=1, kind=KIND_JAM, created_at=SEP1 + 26 * HOUR, resolved_at=SEP1 + 34 * HOUR),
            IncidentRow(
                id=2, printer_id=2, kind=KIND_SERVICE_HOLD, created_at=SEP1 + 2 * HOUR, resolved_at=SEP1 + 5 * HOUR
            ),
        ]
        timeline = _timeline(window, now, roster=_roster(1, 2, 3), spans=spans, incidents=incidents)
        totals = projections.class_totals(timeline)
        tally = projections.print_tally(timeline, [])
        return {
            "timeline": timeline,
            "totals": totals,
            "fleet": projections.fleet_series(totals),
            "matrix": projections.matrix(totals, tally),
            "recovery": projections.recovery(timeline, [], incidents),
        }

    def test_every_identity_holds(self, built):
        _assert_identities(**built)

    def test_the_day_one_numbers_are_the_ones_on_paper(self, built):
        values = built["fleet"].buckets[0].values
        # Day one: 001 prints 20 h and cools 4 h; 002 is idle 24 h but held for three of
        # them by a maintenance hold; 003 has no record for its first six hours.
        assert values.avg_by_group["printing"] == pytest.approx(20 / 24)
        assert values.avg_by_group["cycle_overhead"] == pytest.approx(4 / 24)
        assert values.avg_by_group["planned"] == pytest.approx(3 / 24)
        assert values.avg_by_group["not_recorded"] == pytest.approx(6 / 24)
        assert values.avg_down == pytest.approx(0.0)
        assert values.printers_in_fleet == pytest.approx(3 - 6 / 24)

    def test_down_is_split_by_cause_and_a_fault_outranks_the_condition(self, built):
        values = built["fleet"].buckets[1].values
        # Day two: 001 is offline all 24 h, but a jam stands open for 8 of them — and a
        # fault names the downtime the operator has to act on.
        assert values.avg_down_by_cause["fault:jam"] == pytest.approx(8 / 24)
        assert values.avg_down_by_cause["offline"] == pytest.approx(16 / 24)
        assert values.avg_down_by_cause["plate_held"] == pytest.approx(18 / 24)

    def test_sparse_maps_omit_a_class_with_no_seconds(self, built):
        values = built["fleet"].buckets[0].values
        assert GROUP_UNOBSERVED not in values.avg_by_group
        assert GROUP_DOWN not in values.avg_by_group
        assert "fault:jam" not in values.avg_down_by_cause
        assert "out_of_fleet" not in built["matrix"].series.buckets[0].values.printers[1].class_seconds

    def test_the_ratios_share_one_denominator(self, built):
        bucket = built["fleet"].buckets[1]
        cell = built["totals"].fleet[1]
        known = built["timeline"].printers_known
        groups: dict[str, float] = {}
        for klass, seconds in cell.items():
            groups[klass.group] = groups.get(klass.group, 0.0) + seconds
        scheduled = (
            known * bucket.elapsed_seconds
            - groups.get(GROUP_OUT_OF_FLEET, 0.0)
            - groups.get(GROUP_NOT_RECORDED, 0.0)
            - groups.get(GROUP_UNOBSERVED, 0.0)
            - groups.get(GROUP_PLANNED, 0.0)
        )
        assert bucket.values.uptime == pytest.approx((scheduled - groups.get(GROUP_DOWN, 0.0)) / scheduled)
        assert bucket.values.time_printing == pytest.approx(groups.get("printing", 0.0) / scheduled)


class TestIdentitiesOnASeededFleet:
    """Nobody chose this arrangement, and the identities still hold."""

    @pytest.mark.parametrize("seed", [1, 7, 20260921])
    def test_a_random_week_keeps_every_identity(self, seed: int):
        rng = random.Random(seed)
        window = build_window(date(2026, 9, 1), date(2026, 9, 7), "day", NY)
        now = window.end
        spans: list[SpanRow] = []
        incidents: list[IncidentRow] = []
        for printer_id in range(1, 6):
            cursor = SEP1 + timedelta(hours=rng.randrange(0, 12))
            while cursor < now:
                width = timedelta(minutes=rng.randrange(20, 600))
                end = min(cursor + width, now)
                shape = rng.choice(["printing", "idle", "offline", "cooling", "held", "deactivated", "gap"])
                if shape == "gap":
                    cursor = end
                    continue
                spans.append(
                    _span(
                        printer_id,
                        cursor,
                        end,
                        gcode_state={"printing": "RUNNING", "offline": None, "deactivated": None}.get(shape, "IDLE"),
                        connected=shape not in ("offline", "deactivated"),
                        is_active=shape != "deactivated",
                        plate_phase={"cooling": PLATE_PHASE_COOLING, "held": PLATE_PHASE_HELD}.get(
                            shape, PLATE_PHASE_CLEAR
                        ),
                    )
                )
                cursor = end
            if rng.random() < 0.7:
                opened = SEP1 + timedelta(hours=rng.randrange(0, 120))
                resolved = opened + timedelta(hours=rng.randrange(1, 40))
                incidents.append(
                    IncidentRow(
                        id=len(incidents) + 1,
                        printer_id=printer_id,
                        kind=rng.choice([KIND_JAM, KIND_RUNOUT, KIND_SERVICE_HOLD]),
                        created_at=opened,
                        resolved_at=None if rng.random() < 0.25 else min(resolved, now),
                    )
                )
        timeline = _timeline(window, now, roster=_roster(1, 2, 3, 4, 5), spans=spans, incidents=incidents)
        totals = projections.class_totals(timeline)
        tally = projections.print_tally(timeline, [])
        # The scenario is only worth running if it actually produces partly-observed
        # buckets — otherwise the mask rule is never exercised and the identities pass
        # for the wrong reason.
        partial = [h for h in timeline.headers if 0 < h.observed_seconds < h.elapsed_seconds]
        assert partial, "seed produced no partly-observed bucket"
        _assert_identities(
            timeline,
            totals,
            projections.fleet_series(totals),
            projections.matrix(totals, tally),
            projections.recovery(timeline, [], incidents),
        )


class TestReAggregation:
    """Identity (e): a week is the sum of its days, and the window is the sum of both."""

    def _rows(self):
        spans = [
            _span(1, SEP1, SEP1 + 100 * HOUR),
            _span(1, SEP1 + 100 * HOUR, SEP1 + 14 * 24 * HOUR, connected=False, gcode_state=None),
            _span(2, SEP1 + 10 * HOUR, SEP1 + 14 * 24 * HOUR, gcode_state="IDLE"),
        ]
        incidents = [
            IncidentRow(id=1, printer_id=1, kind=KIND_JAM, created_at=SEP1 + 120 * HOUR, resolved_at=SEP1 + 160 * HOUR)
        ]
        return spans, incidents

    @pytest.mark.parametrize(
        ("date_from", "date_to"),
        [
            (date(2026, 9, 1), date(2026, 9, 14)),
            # Across the fall-back transition, where a day is 25 h.
            (date(2026, 10, 26), date(2026, 11, 8)),
            # Across an ISO-week year boundary.
            (date(2026, 12, 21), date(2027, 1, 3)),
        ],
    )
    def test_day_cells_fold_exactly_into_week_cells(self, date_from: date, date_to: date):
        spans, incidents = self._rows()
        by_day = build_window(date_from, date_to, "day", NY)
        by_week = build_window(date_from, date_to, "week", NY)
        now = by_day.end
        days = projections.class_totals(_timeline(by_day, now, roster=_roster(1, 2), spans=spans, incidents=incidents))
        weeks = projections.class_totals(
            _timeline(by_week, now, roster=_roster(1, 2), spans=spans, incidents=incidents)
        )
        assert days.fleet_window == weeks.fleet_window
        assert sum(header.seconds for header in days.timeline.headers) == pytest.approx(
            sum(header.seconds for header in weeks.timeline.headers)
        )
        assert days.timeline.total.observed_seconds == pytest.approx(weeks.timeline.total.observed_seconds)
        # ...and the window figure is the same however it was bucketed.
        assert projections.fleet_series(days).totals.avg_down == pytest.approx(
            projections.fleet_series(weeks).totals.avg_down
        )

    def test_a_peak_over_a_week_is_the_worst_of_its_days(self):
        spans, incidents = self._rows()
        by_day = build_window(date(2026, 9, 1), date(2026, 9, 14), "day", NY)
        by_week = build_window(date(2026, 9, 1), date(2026, 9, 14), "week", NY)
        now = by_day.end
        days = projections.fleet_series(
            projections.class_totals(_timeline(by_day, now, roster=_roster(1, 2), spans=spans, incidents=incidents))
        )
        weeks = projections.fleet_series(
            projections.class_totals(_timeline(by_week, now, roster=_roster(1, 2), spans=spans, incidents=incidents))
        )
        assert weeks.totals.peak_down == max(bucket.values.peak_down for bucket in days.buckets)

    def test_an_hour_grid_over_a_dst_day_keeps_the_window_total(self):
        window = build_window(date(2026, 11, 1), date(2026, 11, 1), "hour", NY)
        now = window.end
        timeline = _timeline(window, now, roster=_roster(1), spans=[_span(1, SEP1 - DAY, now)])
        totals = projections.class_totals(timeline)
        assert len(timeline.headers) == 25
        assert sum(sum(cell.values()) for cell in totals.fleet) == 25 * 3600


class TestPeakConcurrency:
    """The peak is a joint sweep, and touching is not overlapping."""

    def _peak_for(self, outages: list[tuple[int, int, int]]) -> int:
        window = build_window(date(2026, 9, 1), date(2026, 9, 1), "day", NY)
        now = window.end
        spans = []
        for printer_id, low, high in outages:
            spans.append(_span(printer_id, SEP1, SEP1 + low * HOUR, gcode_state="IDLE"))
            spans.append(_span(printer_id, SEP1 + low * HOUR, SEP1 + high * HOUR, connected=False, gcode_state=None))
            spans.append(_span(printer_id, SEP1 + high * HOUR, now, gcode_state="IDLE"))
        ids = sorted({printer_id for printer_id, _, _ in outages})
        timeline = _timeline(window, now, roster=_roster(*ids), spans=spans)
        return projections.fleet_series(projections.class_totals(timeline)).totals.peak_down

    def test_overlapping_outages_count_together(self):
        assert self._peak_for([(1, 2, 8), (2, 5, 11), (3, 6, 7)]) == 3

    def test_touching_outages_on_different_printers_do_not_overlap(self):
        # [2, 6) and [6, 10) share no instant; reporting two concurrent would be a peak
        # the farm never had.
        assert self._peak_for([(1, 2, 6), (2, 6, 10)]) == 1

    def test_a_nested_outage_does_not_double_count_its_host(self):
        assert self._peak_for([(1, 1, 20), (2, 5, 9)]) == 2


class TestWhatIsWithheld:
    """A bucket nothing observed, and a denominator that is not there."""

    @pytest.fixture
    def ledger_only(self):
        window = build_window(date(2026, 8, 1), date(2026, 8, 2), "day", NY)
        now = datetime(2026, 8, 3, 4, 0, 0)
        opened = datetime(2026, 8, 1, 10, 0, 0)
        incidents = [
            IncidentRow(
                id=1, printer_id=1, kind=KIND_JAM, created_at=opened, resolved_at=datetime(2026, 8, 1, 18, 0, 0)
            )
        ]
        timeline = _timeline(window, now, roster=_roster(1), incidents=incidents)
        totals = projections.class_totals(timeline)
        fleet = projections.fleet_series(totals)
        return {
            "timeline": timeline,
            "fleet": fleet,
            "prints": projections.throughput(totals, projections.print_tally(timeline, [])),
            "recovery": projections.recovery(timeline, [], incidents),
        }

    def test_an_incidents_only_bucket_nulls_every_ratio(self, ledger_only):
        for bucket in ledger_only["fleet"].buckets:
            assert bucket.basis == "incidents_only"
            assert bucket.values.uptime is None
            assert bucket.values.time_printing is None
        assert ledger_only["fleet"].totals.uptime is None

    def test_but_the_fault_hours_are_still_reported(self, ledger_only):
        assert ledger_only["recovery"].fault_open_seconds == 8 * 3600
        assert ledger_only["fleet"].totals.avg_down_by_cause["fault:jam"] > 0

    def test_every_state_summary_row_is_null_while_the_print_rows_are_not(self, ledger_only):
        summary = projections.compose_summary(
            projections.SummaryInputs(fleet=ledger_only["fleet"], prints=ledger_only["prints"]), None
        )
        rows = {row.key: row for row in summary.rows}
        for key in _STATE_ROW_KEYS:
            assert rows[key].figure is None, key
            assert all(value is None for value in rows[key].series), key
        assert rows[projections.ROW_PRINTS_PER_DAY].figure == 0.0

    def test_a_recorder_gap_reads_unobserved_and_the_averages_still_sum(self):
        window = build_window(date(2026, 9, 1), date(2026, 9, 1), "day", NY)
        now = window.end
        timeline = _timeline(
            window,
            now,
            roster=_roster(1, 2),
            spans=[_span(1, SEP1, SEP1 + 8 * HOUR), _span(1, SEP1 + 16 * HOUR, now), _span(2, SEP1, now)],
        )
        fleet = projections.fleet_series(projections.class_totals(timeline))
        values = fleet.buckets[0].values
        assert values.avg_by_group[GROUP_UNOBSERVED] == pytest.approx(8 / 24)
        assert sum(values.avg_by_group.values()) == pytest.approx(2.0)
        # Unobserved time is outside the scheduled denominator, so it neither counts as
        # uptime nor as downtime.
        assert values.uptime == pytest.approx(1.0)

    def test_a_partial_current_bucket_divides_by_elapsed_not_by_width(self):
        window = build_window(date(2026, 9, 1), date(2026, 9, 2), "day", NY)
        now = SEP1 + 30 * HOUR
        timeline = _timeline(window, now, roster=_roster(1), spans=[_span(1, SEP1, None, last=now)])
        fleet = projections.fleet_series(projections.class_totals(timeline))
        today = fleet.buckets[1]
        assert today.seconds == 86400.0
        assert today.elapsed_seconds == 6 * 3600.0
        # Six of six elapsed hours printing is one printer printing, not a quarter of one.
        assert today.values.avg_by_group["printing"] == pytest.approx(1.0)

    def test_a_future_bucket_has_no_elapsed_time_and_no_averages(self):
        window = build_window(date(2026, 9, 1), date(2026, 9, 3), "day", NY)
        now = SEP1 + 6 * HOUR
        timeline = _timeline(window, now, roster=_roster(1), spans=[_span(1, SEP1, None, last=now)])
        fleet = projections.fleet_series(projections.class_totals(timeline))
        assert fleet.buckets[2].elapsed_seconds == 0.0
        assert fleet.buckets[2].values.avg_by_group == {}
        assert fleet.buckets[2].values.uptime is None


class TestThroughputAndUnits:
    """Print and unit data is complete for its own history — and must reconcile."""

    def _window(self):
        window = build_window(date(2026, 9, 1), date(2026, 9, 2), "day", NY)
        timeline = _timeline(window, window.end, roster=_roster(1, 2), spans=[_span(1, SEP1, window.end)])
        return window, timeline, projections.class_totals(timeline)

    def test_totals_reconcile_with_a_direct_count_including_unknown_statuses(self):
        window, timeline, totals = self._window()
        rows = [
            projections.PrintLogRow(created_at=SEP1 + HOUR, status="completed", printer_id=1),
            projections.PrintLogRow(created_at=SEP1 + 2 * HOUR, status="failed", printer_id=1),
            projections.PrintLogRow(created_at=SEP1 + 3 * HOUR, status="cancelled", printer_id=2),
            projections.PrintLogRow(created_at=SEP1 + 26 * HOUR, status="completed", printer_id=2),
            # A status in no bucket at all is still counted, under its own name.
            projections.PrintLogRow(created_at=SEP1 + 27 * HOUR, status="teleported", printer_id=None),
        ]
        series = projections.throughput(totals, projections.print_tally(timeline, rows))
        assert series.totals.total == len(rows)
        assert series.totals.by_outcome == {"completed": 2, "failed": 1, "cancelled": 1, "other": 1}
        assert sum(bucket.values.total for bucket in series.buckets) == len(rows)
        assert series.totals.by_printer == {1: {"completed": 1, "failed": 1}, 2: {"cancelled": 1, "completed": 1}}

    def test_success_excludes_cancelled_and_the_series_carries_no_basis(self):
        window, timeline, totals = self._window()
        rows = [
            projections.PrintLogRow(created_at=SEP1 + HOUR, status="completed", printer_id=1),
            projections.PrintLogRow(created_at=SEP1 + 2 * HOUR, status="completed", printer_id=1),
            projections.PrintLogRow(created_at=SEP1 + 3 * HOUR, status="failed", printer_id=1),
            projections.PrintLogRow(created_at=SEP1 + 4 * HOUR, status="cancelled", printer_id=1),
        ]
        series = projections.throughput(totals, projections.print_tally(timeline, rows))
        assert series.totals.success_pct == pytest.approx(2 / 3)
        assert all(bucket.basis is None for bucket in series.buckets)

    def test_prints_per_day_divides_by_elapsed_days_whatever_the_recorder_saw(self):
        # The print log is complete for its own history, so this rate never asks the
        # recorder anything: two days elapsed, one completed print, half a print a day.
        window, timeline, totals = self._window()
        rows = [projections.PrintLogRow(created_at=SEP1 + HOUR, status="completed", printer_id=1)]
        series = projections.throughput(totals, projections.print_tally(timeline, rows))
        assert series.totals.prints_per_day == pytest.approx(0.5)

    def test_the_per_printer_rate_divides_by_measured_printers_times_elapsed_days(self):
        # Printer 1 is observed for the whole window and printer 2 never, so inside the
        # mask the fleet measures ONE printer: 1 print / (1 printer x 2 days).
        window, timeline, totals = self._window()
        rows = [projections.PrintLogRow(created_at=SEP1 + HOUR, status="completed", printer_id=1)]
        series = projections.throughput(totals, projections.print_tally(timeline, rows))
        assert totals.printers_in_fleet_window == pytest.approx(1.0)
        assert series.totals.prints_per_printer_per_day == pytest.approx(0.5)

    def test_the_per_printer_rate_is_null_where_no_fleet_size_was_measured(self):
        # Prints happened; nothing measured how many printers there were to divide them
        # among. "We cannot say" is not "none", and it is certainly not the roster.
        window = build_window(date(2026, 9, 1), date(2026, 9, 2), "day", NY)
        bare = _timeline(window, window.end, roster=_roster(1, 2))
        rows = [projections.PrintLogRow(created_at=SEP1 + HOUR, status="completed", printer_id=1)]
        series = projections.throughput(projections.class_totals(bare), projections.print_tally(bare, rows))
        assert series.totals.prints_per_printer_per_day is None
        assert all(bucket.values.prints_per_printer_per_day is None for bucket in series.buckets)
        # ...while the complete-data rate still answers.
        assert series.totals.prints_per_day == pytest.approx(0.5)

    def test_units_are_plates_times_the_skus_units_per_plate(self):
        window, timeline, totals = self._window()
        rows = [
            projections.UnitRow(completed_at=SEP1 + HOUR, units_per_plate=4, sku_code="SKU007.01"),
            projections.UnitRow(completed_at=SEP1 + 2 * HOUR, units_per_plate=4, sku_code="SKU007.01"),
            projections.UnitRow(completed_at=SEP1 + 26 * HOUR, units_per_plate=2, sku_code="SKU009.02"),
            # A stored zero or a missing value means one unit — ``plate_units``'s rule.
            projections.UnitRow(completed_at=SEP1 + 27 * HOUR, units_per_plate=0, sku_code="SKU009.02"),
        ]
        series = projections.units(totals, rows)
        assert series.buckets[0].values.by_sku == {"SKU007.01": 8}
        assert series.buckets[1].values.by_sku == {"SKU009.02": 3}
        assert series.totals.units == 11
        assert series.totals.plates == 4


class TestCycleEpisodes:
    """Only a finished episode is a duration of the thing being measured."""

    def _episodes(self):
        window = build_window(date(2026, 9, 1), date(2026, 9, 2), "day", NY)
        timeline = _timeline(window, window.end, roster=_roster(1), spans=[_span(1, SEP1, window.end)])
        return timeline

    def _episode(self, kind, seconds, outcome, *, variant=None, expected=None, printer_id=1):
        start = SEP1 + HOUR
        return projections.EpisodeRow(
            printer_id=printer_id,
            kind=kind,
            started_at=start,
            ended_at=start + timedelta(seconds=seconds),
            expected_s=expected,
            outcome=outcome,
            variant=variant,
        )

    def test_only_released_cooldowns_and_completed_ejects_reach_the_percentiles(self):
        timeline = self._episodes()
        rows = [
            self._episode(KIND_COOLDOWN, 600, "released", variant=COOLDOWN_VARIANT_HOLD),
            self._episode(KIND_COOLDOWN, 900, "released", variant=COOLDOWN_VARIANT_HOLD),
            self._episode(KIND_COOLDOWN, 5, "cleared", variant=COOLDOWN_VARIANT_HOLD),
            self._episode(KIND_COOLDOWN, 9999, "stalled", variant=COOLDOWN_VARIANT_HOLD),
        ]
        group = next(row for row in projections.cycle(timeline, rows).by_model if row.kind == KIND_COOLDOWN)
        assert group.stats.count == 2
        assert group.stats.not_completed == 2
        assert group.stats.median_s == pytest.approx(750.0)
        # A cooldown ends at a temperature, so it has nothing to be late against.
        assert group.stats.over_expected is None
        assert group.stats.over_expected_share is None

    def test_ejects_carry_the_over_expected_share_against_their_own_prediction(self):
        timeline = self._episodes()
        rows = [
            self._episode(KIND_EJECT, 80, "completed", variant="cycle", expected=85.0),
            self._episode(KIND_EJECT, 120, "completed", variant="cycle", expected=85.0),
            self._episode(KIND_EJECT, 300, "cancelled", variant="cycle", expected=85.0),
        ]
        group = next(row for row in projections.cycle(timeline, rows).by_model if row.kind == KIND_EJECT)
        assert group.stats.count == 2
        assert group.stats.not_completed == 1
        assert group.stats.over_expected == 1
        assert group.stats.over_expected_share == pytest.approx(0.5)

    def test_groups_split_by_printer_model_and_variant_and_are_returned_per_printer_too(self):
        timeline = self._episodes()
        rows = [
            self._episode(KIND_COOLDOWN, 600, "released", variant=COOLDOWN_VARIANT_HOLD),
            self._episode(KIND_COOLDOWN, 1800, "released", variant=COOLDOWN_VARIANT_FAN_ONLY),
        ]
        result = projections.cycle(timeline, rows)
        variants = {group.variant: group.stats.median_s for group in result.by_model}
        assert variants == {COOLDOWN_VARIANT_HOLD: 600.0, COOLDOWN_VARIANT_FAN_ONLY: 1800.0}
        assert {group.model for group in result.by_model} == {"H2S"}
        assert {group.printer_id for group in result.by_printer} == {1}


class TestTheSummaryCard:
    """The card reads the series' own numbers, and compares like with like."""

    def _inputs(self, date_from: date, date_to: date, *, spans, now, prints=(), prints_since=None):
        window = build_window(date_from, date_to, "day", NY)
        timeline = _timeline(window, now, roster=_roster(1, 2), spans=spans)
        totals = projections.class_totals(timeline)
        tally = projections.print_tally(timeline, list(prints))
        return projections.SummaryInputs(
            fleet=projections.fleet_series(totals),
            prints=projections.throughput(totals, tally),
            window_end=window.end,
            prints_since=prints_since,
        )

    def test_the_figure_and_the_sparkline_are_the_series_own_numbers(self):
        spans = [_span(1, SEP1, SEP1 + 48 * HOUR), _span(2, SEP1, SEP1 + 48 * HOUR, gcode_state="IDLE")]
        current = self._inputs(date(2026, 9, 1), date(2026, 9, 2), spans=spans, now=SEP1 + 48 * HOUR)
        summary = projections.compose_summary(current, None)
        rows = {row.key: row for row in summary.rows}
        assert rows[projections.ROW_AVG_PRINTING].figure == current.fleet.totals.avg_by_group["printing"]
        assert rows[projections.ROW_AVG_PRINTING].series == [
            bucket.values.avg_by_group["printing"] for bucket in current.fleet.buckets
        ]
        assert rows[projections.ROW_UPTIME].figure == current.fleet.totals.uptime

    def test_the_previous_window_is_the_same_number_of_site_days(self):
        previous_from, previous_to = previous_window(date(2026, 9, 8), date(2026, 9, 14))
        assert (previous_from, previous_to) == (date(2026, 9, 1), date(2026, 9, 7))
        current = build_window(date(2026, 9, 8), date(2026, 9, 14), "day", NY)
        earlier = build_window(previous_from, previous_to, "day", NY)
        assert len(current.grid.buckets) == len(earlier.grid.buckets)

    def test_previous_is_null_for_a_state_row_when_nothing_was_observed_before(self):
        spans = [_span(1, SEP1, SEP1 + 48 * HOUR), _span(2, SEP1, SEP1 + 48 * HOUR, gcode_state="IDLE")]
        current = self._inputs(date(2026, 9, 1), date(2026, 9, 2), spans=spans, now=SEP1 + 48 * HOUR)
        earlier = self._inputs(date(2026, 8, 30), date(2026, 8, 31), spans=[], now=SEP1 + 48 * HOUR)
        rows = {row.key: row for row in projections.compose_summary(current, earlier).rows}
        assert earlier.observed is False
        for key in _STATE_ROW_KEYS:
            assert rows[key].previous is None, key
        # ...and with an EMPTY print log there is nothing on the print side either, so
        # that row is withheld too rather than compared against a period with no record.
        assert rows[projections.ROW_PRINTS_PER_DAY].previous is None

    def test_a_previous_window_before_the_first_print_is_not_compared(self):
        """The reported defect: "vs previous +129" against a period with no record.

        The previous window ends before the print log's first row, so there is no print
        data there to be zero. Reporting 0 made the page render a rise against a period
        that does not exist — every state row correctly showed a dash beside it.
        """
        spans = [_span(1, SEP1, SEP1 + 48 * HOUR)]
        prints = [projections.PrintLogRow(created_at=SEP1 + HOUR, status="completed", printer_id=1)]
        first_print = SEP1 + HOUR
        current = self._inputs(
            date(2026, 9, 1),
            date(2026, 9, 2),
            spans=spans,
            now=SEP1 + 48 * HOUR,
            prints=prints,
            prints_since=first_print,
        )
        earlier = self._inputs(
            date(2026, 8, 30),
            date(2026, 8, 31),
            spans=[],
            now=SEP1 + 48 * HOUR,
            prints_since=first_print,
        )
        assert earlier.prints_comparable is False
        rows = {row.key: row for row in projections.compose_summary(current, earlier).rows}
        assert rows[projections.ROW_PRINTS_PER_DAY].previous is None
        assert rows[projections.ROW_PRINTS_PER_PRINTER_PER_DAY].previous is None
        # The current figure is untouched — this rule is about the comparison only.
        assert rows[projections.ROW_PRINTS_PER_DAY].figure == pytest.approx(0.5)

    def test_a_previous_window_that_straddles_the_first_print_is_compared(self):
        # The log is complete from its first row on, so a window reaching that instant
        # compares honestly — and a zero there IS a measurement.
        spans = [_span(1, SEP1, SEP1 + 48 * HOUR)]
        # The first print lands inside the PREVIOUS window (2026-08-30..08-31).
        first_print = datetime(2026, 8, 31, 12, 0, 0)
        current = self._inputs(
            date(2026, 9, 1), date(2026, 9, 2), spans=spans, now=SEP1 + 48 * HOUR, prints_since=first_print
        )
        earlier = self._inputs(
            date(2026, 8, 30),
            date(2026, 8, 31),
            spans=[],
            now=SEP1 + 48 * HOUR,
            prints=[projections.PrintLogRow(created_at=first_print, status="completed", printer_id=1)],
            prints_since=first_print,
        )
        assert earlier.prints_comparable is True
        rows = {row.key: row for row in projections.compose_summary(current, earlier).rows}
        assert rows[projections.ROW_PRINTS_PER_DAY].previous == pytest.approx(0.5)

    def test_an_empty_print_log_is_never_compared(self):
        spans = [_span(1, SEP1, SEP1 + 48 * HOUR)]
        current = self._inputs(date(2026, 9, 1), date(2026, 9, 2), spans=spans, now=SEP1 + 48 * HOUR)
        earlier = self._inputs(date(2026, 8, 30), date(2026, 8, 31), spans=[], now=SEP1 + 48 * HOUR)
        assert earlier.prints_comparable is False
        rows = {row.key: row for row in projections.compose_summary(current, earlier).rows}
        assert rows[projections.ROW_PRINTS_PER_DAY].previous is None

    def test_the_state_rows_keep_their_own_rule(self):
        # A window the RECORDER never covered withholds its state rows even when the
        # print log reaches right through it: the two records are bounded separately.
        spans = [_span(1, SEP1, SEP1 + 48 * HOUR)]
        first_print = datetime(2026, 8, 1, 0, 0, 0)
        current = self._inputs(
            date(2026, 9, 1), date(2026, 9, 2), spans=spans, now=SEP1 + 48 * HOUR, prints_since=first_print
        )
        earlier = self._inputs(
            date(2026, 8, 30),
            date(2026, 8, 31),
            spans=[],
            now=SEP1 + 48 * HOUR,
            prints=[
                projections.PrintLogRow(created_at=datetime(2026, 8, 30, 6, 0, 0), status="completed", printer_id=1)
            ],
            prints_since=first_print,
        )
        rows = {row.key: row for row in projections.compose_summary(current, earlier).rows}
        assert earlier.observed is False
        assert earlier.prints_comparable is True
        for key in _STATE_ROW_KEYS:
            assert rows[key].previous is None, key
        assert rows[projections.ROW_PRINTS_PER_DAY].previous == pytest.approx(0.5)

    def test_every_row_appears_once_in_the_cards_reading_order(self):
        spans = [_span(1, SEP1, SEP1 + 48 * HOUR)]
        summary = projections.compose_summary(
            self._inputs(date(2026, 9, 1), date(2026, 9, 2), spans=spans, now=SEP1 + 48 * HOUR), None
        )
        keys = [row.key for row in summary.rows]
        assert keys == [
            projections.ROW_AVG_PRINTING,
            projections.ROW_AVG_CYCLE_OVERHEAD,
            projections.ROW_AVG_IDLE,
            projections.ROW_AVG_DOWN,
            projections.ROW_AVG_PLANNED,
            projections.ROW_PEAK_DOWN,
            projections.ROW_PRINTERS_IN_FLEET,
            projections.ROW_PRINTS_PER_DAY,
            projections.ROW_PRINTS_PER_PRINTER_PER_DAY,
            projections.ROW_UPTIME,
            projections.ROW_TIME_PRINTING,
        ]


class TestTheProductionShape:
    """The farm on the day the recorder shipped: 44 days of ledger, 45 minutes of spans.

    This is the arrangement that exposed both arithmetic rules in production, and it is
    the shape every farm passes through exactly once — so it is pinned rather than
    remembered. Twelve printers have been printing and failing for 44 days, all of it
    in the print log and the incident ledger; the observation recorder started 45
    minutes ago and has seen 8 printing, 1 cooling and 3 offline.

    Dividing state figures by elapsed time reported that farm as ``printers_in_fleet
    = 1.29`` and ``avg_printing = 0.04``. Every identity held. The headline lied.
    """

    DAYS = 44
    PRINTERS = 12
    PRINTING = 8
    COOLING = 1
    DOWN = 3
    MASK = timedelta(minutes=45)

    @pytest.fixture
    def built(self):
        date_to = date(2026, 9, 13)
        date_from = date_to - timedelta(days=self.DAYS - 1)
        window = build_window(date_from, date_to, "day", NY)
        # ``now`` is the end of the last site day, so its elapsed time is a whole day
        # and the window is exactly 44 days — the recorder is still 45 minutes old.
        now = window.end
        mask_start = now - self.MASK

        spans = []
        for printer_id in range(1, self.PRINTERS + 1):
            if printer_id <= self.PRINTING:
                shape = {"gcode_state": "RUNNING"}
            elif printer_id <= self.PRINTING + self.COOLING:
                shape = {"gcode_state": "FINISH", "plate_phase": PLATE_PHASE_COOLING}
            else:
                shape = {"connected": False, "gcode_state": None}
            spans.append(_span(printer_id, mask_start, None, last=now, **shape))

        # The ledger: two full-day outages on days 5 and 6, deliberately NOT overlapping,
        # so the window's peak is today's three offline printers and every summary row's
        # figure can be compared with its own last series point.
        day_five = window.grid.buckets[5].start
        day_six = window.grid.buckets[6].start
        incidents = [
            IncidentRow(id=1, printer_id=1, kind=KIND_JAM, created_at=day_five, resolved_at=day_five + DAY),
            IncidentRow(id=2, printer_id=2, kind=KIND_JAM, created_at=day_six, resolved_at=day_six + DAY),
        ]
        # One completed print per printer per day, for all 44 days.
        prints = [
            projections.PrintLogRow(created_at=edge.start + 6 * HOUR, status="completed", printer_id=printer_id)
            for edge in window.grid.buckets
            for printer_id in range(1, self.PRINTERS + 1)
        ]
        timeline = _timeline(
            window,
            now,
            roster=_roster(*range(1, self.PRINTERS + 1)),
            spans=spans,
            incidents=incidents,
            history_since=day_five,
        )
        totals = projections.class_totals(timeline)
        tally = projections.print_tally(timeline, prints)
        fleet = projections.fleet_series(totals)
        prints_series = projections.throughput(totals, tally)
        return {
            "window": window,
            "timeline": timeline,
            "totals": totals,
            "fleet": fleet,
            "prints": prints_series,
            "matrix": projections.matrix(totals, tally),
            "summary": projections.compose_summary(projections.SummaryInputs(fleet=fleet, prints=prints_series), None),
        }

    def test_today_reads_the_fleet_that_is_actually_there(self, built):
        today = built["fleet"].buckets[-1]
        assert today.basis == "observed"
        assert today.observed_seconds == self.MASK.total_seconds()
        assert today.elapsed_seconds == 86400.0
        # THE headline: eight printers are printing, and the page says eight.
        assert today.values.avg_by_group["printing"] == pytest.approx(float(self.PRINTING))
        assert today.values.avg_by_group["cycle_overhead"] == pytest.approx(float(self.COOLING))
        assert today.values.avg_down == pytest.approx(float(self.DOWN))
        assert sum(today.values.avg_by_group.values()) == pytest.approx(float(self.PRINTERS))
        assert today.values.printers_in_fleet == pytest.approx(float(self.PRINTERS))

    def test_the_window_reports_twelve_printers_not_one(self, built):
        # The production bug, exactly: 44 days of unwatched time diluted the fleet to
        # 1.29 printers. Measured inside the mask, the window IS the 45 minutes that
        # were measured, and it says twelve.
        assert built["fleet"].totals.printers_in_fleet == pytest.approx(float(self.PRINTERS))
        assert built["fleet"].totals.avg_by_group["printing"] == pytest.approx(float(self.PRINTING))
        assert built["timeline"].total.observed_seconds == self.MASK.total_seconds()

    def test_the_ratios_come_from_the_observed_45_minutes_only(self, built):
        today = built["fleet"].buckets[-1].values
        # scheduled = 12 printers x the mask; nothing is out of fleet, unobserved or
        # held inside it, so uptime is the three offline machines and nothing else.
        assert today.time_printing == pytest.approx(self.PRINTING / self.PRINTERS)
        assert today.uptime == pytest.approx((self.PRINTERS - self.DOWN) / self.PRINTERS)
        assert built["fleet"].totals.uptime == pytest.approx(today.uptime)

    def test_the_earlier_days_report_the_ledger_and_withhold_the_rest(self, built):
        buckets = built["fleet"].buckets
        for bucket in buckets[:-1]:
            assert bucket.basis == "incidents_only"
            assert bucket.values.uptime is None
            assert bucket.values.time_printing is None
            assert set(bucket.values.avg_by_group) <= {"down", "planned"}
        # ...and the two full-day outages are intact, at one whole printer each.
        assert buckets[5].values.avg_down_by_cause == {f"fault:{KIND_JAM}": pytest.approx(1.0)}
        assert buckets[6].values.avg_down_by_cause == {f"fault:{KIND_JAM}": pytest.approx(1.0)}
        assert buckets[4].values.avg_by_group == {}

    def test_per_printer_prints_per_day_is_completed_over_the_elapsed_days(self, built):
        # 44 completed prints over 44 elapsed days. The old rule divided by "printer-days
        # in service", which only the 45-minute-old recorder could see, and answered 48.
        row = next(row for row in built["matrix"].printers if row.printer_id == 1)
        assert row.prints_per_day == pytest.approx(1.0)
        assert built["timeline"].total.elapsed_seconds == self.DAYS * 86400.0

    def test_per_printer_hours_down_per_day_ignores_days_with_no_record(self, built):
        # The farm's downtime record begins with the first incident, on day 5 — the
        # print log reaches further back, but no span and no incident does, so nothing
        # could have been known about days 0-4. They are excluded from the denominator
        # rather than counted as days the printer was fine: a rate that divided 24 h of
        # outage by 44 days would report a machine that lost a whole day as healthier
        # than one that lost the same day inside the recorded period.
        row = next(row for row in built["matrix"].printers if row.printer_id == 1)
        with_history = self.DAYS - 5
        assert built["totals"].history_elapsed_seconds == with_history * 86400.0
        assert row.hours_down_per_day == pytest.approx(24.0 / with_history)
        # The complete-data rate is unaffected: it divides by every elapsed day.
        assert row.prints_per_day == pytest.approx(1.0)

    def test_the_per_printer_rate_is_a_believable_number(self, built):
        # 12 prints completed in the one observed bucket, 12 printers measured in it.
        totals = built["prints"].totals
        assert totals.prints_per_day == pytest.approx(float(self.PRINTERS))
        assert totals.prints_per_printer_per_day == pytest.approx(1.0)
        # The order check the production number failed: 22.8 per printer per day beside
        # a fleet rate of 12 a day is arithmetically impossible on twelve machines.
        assert totals.prints_per_printer_per_day == pytest.approx(totals.prints_per_day / self.PRINTERS)
        assert all(bucket.values.prints_per_printer_per_day is None for bucket in built["prints"].buckets[:-1])

    def test_the_summary_shows_today_and_nothing_before_it(self, built):
        rows = {row.key: row for row in built["summary"].rows}
        for key in _STATE_ROW_KEYS:
            row = rows[key]
            assert all(value is None for value in row.series[:-1]), key
            assert row.series[-1] is not None, key
            # Only one bucket was observed, so the window figure IS that bucket's.
            assert row.figure == pytest.approx(row.series[-1]), key
        assert rows[projections.ROW_AVG_PRINTING].figure == pytest.approx(float(self.PRINTING))
        assert rows[projections.ROW_PRINTERS_IN_FLEET].figure == pytest.approx(float(self.PRINTERS))
        # The print rows are never withheld — the log is complete for its own history.
        assert rows[projections.ROW_PRINTS_PER_DAY].figure == pytest.approx(float(self.PRINTERS))
        assert all(value is not None for value in rows[projections.ROW_PRINTS_PER_DAY].series)

    def test_the_matrix_still_sees_the_whole_ledger(self, built):
        # Rule C: the matrix keeps the FULL elapsed bucket with ledger evidence, so the
        # outage on day 5 is visible in printer 1's cell even though no recorder existed
        # then — the opposite coverage from the averages above, deliberately.
        cell = built["matrix"].series.buckets[5].values.printers[1]
        assert cell.basis == "incidents_only"
        assert cell.class_seconds[f"down:fault:{KIND_JAM}"] == pytest.approx(86400.0)
        assert cell.down_seconds == pytest.approx(86400.0)
