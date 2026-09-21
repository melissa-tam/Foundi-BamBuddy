"""Unit tests for the site calendar (``backend/app/utils/site_time.py``).

Every assertion names its zone explicitly through the injectable ``tz`` so the
suite says the same thing on a UTC CI box and on the farm PC. The two host-zone
lanes that cannot be injected (``TZ`` unset) are cross-derived from the stdlib
instead of from a zone name.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from backend.app.utils import site_time

UTC = timezone.utc
NEW_YORK = ZoneInfo("America/New_York")  # 2026-03-08 is 23 h, 2026-11-01 is 25 h
SYDNEY = ZoneInfo("Australia/Sydney")  # southern hemisphere: back in April, forward in October
KOLKATA = ZoneInfo("Asia/Kolkata")  # +05:30, no DST at all

SPRING_FORWARD = date(2026, 3, 8)  # 23 h in New York
FALL_BACK = date(2026, 11, 1)  # 25 h in New York

# The SAME file a frontend test reads to pin ``utils/timeframe.ts`` against this
# grid — the week rule has one statement, not one per language.
WEEK_VECTORS: list[dict[str, str]] = json.loads(
    (Path(__file__).resolve().parents[2] / "_fixtures" / "site_week_vectors.json").read_text(encoding="utf-8")
)


def _stdlib_os_instant(d: date, at: time = time.min) -> datetime:
    """The naive-UTC instant of an OS-local wall clock, derived without site_time."""
    return datetime.combine(d, at).astimezone(UTC).replace(tzinfo=None)


class TestSiteZone:
    """TZ env → zone, and what "no zone" means."""

    def test_valid_tz_env_resolves_to_that_zone(self, monkeypatch):
        monkeypatch.setenv("TZ", "America/New_York")
        zone = site_time.site_zone()
        assert isinstance(zone, ZoneInfo)
        assert zone.key == "America/New_York"

    def test_unset_tz_env_means_the_os_zone(self, monkeypatch):
        monkeypatch.delenv("TZ", raising=False)
        assert site_time.site_zone() is None

    def test_blank_tz_env_means_the_os_zone(self, monkeypatch):
        monkeypatch.setenv("TZ", "   ")
        assert site_time.site_zone() is None

    def test_unrecognised_tz_env_means_the_os_zone_not_utc(self, monkeypatch):
        monkeypatch.setenv("TZ", "Not/A_Real_Zone")
        assert site_time.site_zone() is None

    def test_malformed_tz_env_means_the_os_zone(self, monkeypatch):
        # ZoneInfo raises ValueError (not ZoneInfoNotFoundError) for a key that
        # is not even shaped like one.
        monkeypatch.setenv("TZ", "/etc/localtime")
        assert site_time.site_zone() is None

    def test_missing_tz_database_means_the_os_zone(self, monkeypatch):
        """The Windows installer ships an embedded Python with no IANA tz DB.

        Every ``ZoneInfo(...)`` raises there unless the ``tzdata`` wheel is
        installed, and the site must still have a calendar.
        """

        def _always_missing(_key):
            raise ZoneInfoNotFoundError("no tz database on this platform")

        monkeypatch.setattr(site_time, "ZoneInfo", _always_missing)
        monkeypatch.setenv("TZ", "Europe/Berlin")
        assert site_time.site_zone() is None

    def test_unusable_tz_warns_once_per_value(self, monkeypatch, caplog):
        monkeypatch.setattr(site_time, "_UNRESOLVABLE_TZ_SEEN", set())
        monkeypatch.setenv("TZ", "Not/A_Real_Zone")
        with caplog.at_level(logging.WARNING, logger=site_time.logger.name):
            site_time.site_zone()
            site_time.site_zone()
        warnings = [r for r in caplog.records if r.name == site_time.logger.name]
        assert len(warnings) == 1


class TestSiteZoneName:
    """The display string a response carries."""

    def test_iana_zone_answers_with_its_key(self):
        assert site_time.site_zone_name(tz=NEW_YORK) == "America/New_York"

    def test_utc_answers_utc(self):
        assert site_time.site_zone_name(tz=UTC) == "UTC"

    def test_os_zone_answers_with_its_name_at_that_instant(self, monkeypatch):
        monkeypatch.delenv("TZ", raising=False)
        for instant in (datetime(2026, 1, 15, 12, 0), datetime(2026, 7, 15, 12, 0)):
            expected = instant.replace(tzinfo=UTC).astimezone().tzname()
            assert site_time.site_zone_name(instant) == expected

    def test_env_zone_is_used_when_no_tz_is_injected(self, monkeypatch):
        monkeypatch.setenv("TZ", "Asia/Kolkata")
        assert site_time.site_zone_name() == "Asia/Kolkata"


class TestToSite:
    """Naive-UTC storage in, aware site-local out."""

    def test_naive_input_is_read_as_utc(self):
        assert site_time.to_site(datetime(2026, 6, 15, 12, 0), NEW_YORK) == datetime(2026, 6, 15, 8, 0, tzinfo=NEW_YORK)

    def test_aware_input_is_converted_not_relabelled(self):
        aware = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
        assert site_time.to_site(aware, KOLKATA).utcoffset() == timedelta(hours=5, minutes=30)
        assert site_time.to_site(aware, KOLKATA).hour == 17

    def test_os_zone_conversion_is_per_instant(self, monkeypatch):
        monkeypatch.delenv("TZ", raising=False)
        for instant in (datetime(2026, 1, 15, 12, 0), datetime(2026, 7, 15, 12, 0)):
            assert site_time.to_site(instant) == instant.replace(tzinfo=UTC).astimezone()


class TestSiteToday:
    def test_site_date_is_not_the_utc_date_before_local_midnight(self):
        assert site_time.site_today(datetime(2026, 6, 15, 3, 0), NEW_YORK) == date(2026, 6, 14)

    def test_site_date_runs_ahead_of_utc_east_of_greenwich(self):
        assert site_time.site_today(datetime(2026, 6, 15, 22, 0), SYDNEY) == date(2026, 6, 16)

    def test_defaults_to_now(self, monkeypatch):
        monkeypatch.delenv("TZ", raising=False)
        before = datetime.now(UTC).astimezone().date()
        today = site_time.site_today()
        after = datetime.now(UTC).astimezone().date()
        assert today in {before, after}


class TestSiteInstant:
    """Site wall clock → the UTC instant it names."""

    def test_plain_wall_clock(self):
        assert site_time.site_instant(date(2026, 6, 15), time(21, 0), NEW_YORK) == datetime(2026, 6, 16, 1, 0)

    def test_nonexistent_wall_clock_normalises_forward(self):
        # 02:30 does not exist on the spring-forward day; fold=0 uses the
        # pre-gap offset, which lands inside the following hour.
        assert site_time.site_instant(SPRING_FORWARD, time(2, 30), NEW_YORK) == datetime(2026, 3, 8, 7, 30)

    def test_ambiguous_wall_clock_takes_the_earlier_instance(self):
        # 01:30 happens twice on the fall-back day; fold=0 is the first one.
        assert site_time.site_instant(FALL_BACK, time(1, 30), NEW_YORK) == datetime(2026, 11, 1, 5, 30)

    def test_os_zone_matches_the_stdlib_derivation(self, monkeypatch):
        monkeypatch.delenv("TZ", raising=False)
        assert site_time.site_instant(date(2026, 6, 15), time(21, 0)) == _stdlib_os_instant(
            date(2026, 6, 15), time(21, 0)
        )


class TestDayBounds:
    def test_ordinary_day_is_24h(self):
        start, end = site_time.day_bounds(date(2026, 6, 15), NEW_YORK)
        assert (start, end) == (datetime(2026, 6, 15, 4, 0), datetime(2026, 6, 16, 4, 0))

    def test_spring_forward_day_is_23h(self):
        start, end = site_time.day_bounds(SPRING_FORWARD, NEW_YORK)
        assert (start, end) == (datetime(2026, 3, 8, 5, 0), datetime(2026, 3, 9, 4, 0))
        assert (end - start) == timedelta(hours=23)

    def test_fall_back_day_is_25h(self):
        start, end = site_time.day_bounds(FALL_BACK, NEW_YORK)
        assert (start, end) == (datetime(2026, 11, 1, 4, 0), datetime(2026, 11, 2, 5, 0))
        assert (end - start) == timedelta(hours=25)

    def test_southern_hemisphere_transitions_the_other_way_round(self):
        april_start, april_end = site_time.day_bounds(date(2026, 4, 5), SYDNEY)
        october_start, october_end = site_time.day_bounds(date(2026, 10, 4), SYDNEY)
        assert (april_end - april_start) == timedelta(hours=25)
        assert (october_end - october_start) == timedelta(hours=23)

    def test_half_hour_offset_zone(self):
        start, end = site_time.day_bounds(date(2026, 6, 15), KOLKATA)
        assert (start, end) == (datetime(2026, 6, 14, 18, 30), datetime(2026, 6, 15, 18, 30))

    def test_utc_day_is_midnight_to_midnight(self):
        assert site_time.day_bounds(date(2026, 6, 15), UTC) == (
            datetime(2026, 6, 15, 0, 0),
            datetime(2026, 6, 16, 0, 0),
        )

    def test_os_zone_matches_the_stdlib_derivation(self, monkeypatch):
        monkeypatch.delenv("TZ", raising=False)
        d = date(2026, 6, 15)
        assert site_time.day_bounds(d) == (_stdlib_os_instant(d), _stdlib_os_instant(d + timedelta(days=1)))


@pytest.mark.parametrize("bucket", ["hour", "day", "week"])
@pytest.mark.parametrize(
    ("date_from", "date_to", "tz"),
    [
        (date(2026, 3, 4), date(2026, 3, 12), NEW_YORK),  # spans the 23 h day
        (date(2026, 10, 28), date(2026, 11, 5), NEW_YORK),  # spans the 25 h day
        (date(2026, 12, 28), date(2027, 1, 10), NEW_YORK),  # ISO week-year boundary
        (date(2026, 6, 15), date(2026, 6, 15), KOLKATA),  # single day, half-hour offset
        (date(2026, 4, 1), date(2026, 4, 9), SYDNEY),
        (date(2026, 6, 1), date(2026, 6, 30), UTC),
    ],
)
class TestGridInvariants:
    """Contiguous, non-overlapping, and exactly covering the window."""

    def test_buckets_are_contiguous_and_ordered(self, date_from, date_to, tz, bucket):
        edges = site_time.bucket_edges(date_from, date_to, bucket, tz)
        assert edges
        for edge in edges:
            assert edge.start < edge.end
        for earlier, later in zip(edges, edges[1:], strict=False):
            assert earlier.end == later.start

    def test_union_is_exactly_the_window(self, date_from, date_to, tz, bucket):
        edges = site_time.bucket_edges(date_from, date_to, bucket, tz)
        assert edges[0].start == site_time.day_bounds(date_from, tz)[0]
        assert edges[-1].end == site_time.day_bounds(date_to, tz)[1]

    def test_seconds_sum_to_the_window(self, date_from, date_to, tz, bucket):
        edges = site_time.bucket_edges(date_from, date_to, bucket, tz)
        window_start, window_end = (
            site_time.day_bounds(date_from, tz)[0],
            site_time.day_bounds(date_to, tz)[1],
        )
        assert sum(e.seconds for e in edges) == (window_end - window_start).total_seconds()

    def test_offset_is_the_site_offset_at_the_bucket_start(self, date_from, date_to, tz, bucket):
        for edge in site_time.bucket_edges(date_from, date_to, bucket, tz):
            expected = site_time.to_site(edge.start, tz).utcoffset()
            assert edge.utc_offset_minutes == round(expected.total_seconds() / 60)


class TestReaggregation:
    """Days re-aggregate exactly into weeks and into the window total."""

    @pytest.mark.parametrize(
        ("date_from", "date_to", "tz"),
        [
            (date(2026, 2, 25), date(2026, 4, 7), NEW_YORK),
            (date(2026, 10, 14), date(2026, 11, 24), NEW_YORK),
            (date(2026, 9, 23), date(2026, 10, 20), SYDNEY),
            (date(2026, 12, 28), date(2027, 1, 10), KOLKATA),
        ],
    )
    def test_hour_day_and_week_totals_agree(self, date_from, date_to, tz):
        hours = site_time.bucket_edges(date_from, date_to, "hour", tz)
        days = site_time.bucket_edges(date_from, date_to, "day", tz)
        weeks = site_time.bucket_edges(date_from, date_to, "week", tz)
        assert sum(e.seconds for e in hours) == sum(e.seconds for e in days) == sum(e.seconds for e in weeks)

    def test_each_week_is_the_exact_sum_of_its_days(self):
        date_from, date_to = date(2026, 10, 28), date(2026, 11, 10)
        days = site_time.bucket_edges(date_from, date_to, "day", NEW_YORK)
        for week in site_time.bucket_edges(date_from, date_to, "week", NEW_YORK):
            covered = [d for d in days if week.start <= d.start < week.end]
            assert sum(d.seconds for d in covered) == week.seconds
            assert covered[0].start == week.start
            assert covered[-1].end == week.end

    def test_each_day_is_the_exact_sum_of_its_hours(self):
        date_from, date_to = date(2026, 10, 31), date(2026, 11, 2)
        hours = site_time.bucket_edges(date_from, date_to, "hour", NEW_YORK)
        for day in site_time.bucket_edges(date_from, date_to, "day", NEW_YORK):
            covered = [h for h in hours if day.start <= h.start < day.end]
            assert sum(h.seconds for h in covered) == day.seconds


class TestDayBuckets:
    def test_one_bucket_per_site_day(self):
        edges = site_time.bucket_edges(date(2026, 6, 1), date(2026, 6, 30), "day", NEW_YORK)
        assert len(edges) == 30

    def test_transition_days_keep_their_own_length(self):
        edges = site_time.bucket_edges(date(2026, 10, 31), date(2026, 11, 3), "day", NEW_YORK)
        assert [e.seconds / 3600 for e in edges] == [24, 25, 24, 24]

    def test_offsets_change_only_after_the_transition(self):
        edges = site_time.bucket_edges(date(2026, 10, 31), date(2026, 11, 3), "day", NEW_YORK)
        # The fall-back day STARTS on daylight time — the switch lands mid-bucket.
        assert [e.utc_offset_minutes for e in edges] == [-240, -240, -300, -300]


class TestHourBuckets:
    def test_spring_forward_day_yields_23_buckets(self):
        edges = site_time.bucket_edges(SPRING_FORWARD, SPRING_FORWARD, "hour", NEW_YORK)
        assert len(edges) == 23
        assert all(e.seconds == 3600 for e in edges)

    def test_fall_back_day_yields_25_buckets(self):
        edges = site_time.bucket_edges(FALL_BACK, FALL_BACK, "hour", NEW_YORK)
        assert len(edges) == 25
        assert all(e.seconds == 3600 for e in edges)

    def test_offset_flips_at_the_transition_hour(self):
        edges = site_time.bucket_edges(FALL_BACK, FALL_BACK, "hour", NEW_YORK)
        assert edges[0].start == datetime(2026, 11, 1, 4, 0)
        assert [e.utc_offset_minutes for e in edges[:4]] == [-240, -240, -300, -300]

    def test_steps_are_utc_hours_not_local_wall_clocks(self):
        # 01:00 local occurs twice on the fall-back day; both instances are
        # present exactly once each because the grid steps in UTC.
        edges = site_time.bucket_edges(FALL_BACK, FALL_BACK, "hour", NEW_YORK)
        local_hours = [site_time.to_site(e.start, NEW_YORK).hour for e in edges]
        assert local_hours.count(1) == 2

    def test_half_hour_zone_still_tiles_the_day(self):
        edges = site_time.bucket_edges(date(2026, 6, 15), date(2026, 6, 15), "hour", KOLKATA)
        assert len(edges) == 24
        assert edges[0].start == datetime(2026, 6, 14, 18, 30)


class TestWeekBuckets:
    def test_iso_weeks_start_on_monday_site_local(self):
        edges = site_time.bucket_edges(date(2026, 9, 21), date(2026, 10, 4), "week", NEW_YORK)
        assert [e.start for e in edges] == [
            site_time.day_bounds(date(2026, 9, 21), NEW_YORK)[0],
            site_time.day_bounds(date(2026, 9, 28), NEW_YORK)[0],
        ]

    def test_first_and_last_weeks_are_clipped_to_the_window(self):
        # Wednesday 2026-12-30 .. Tuesday 2027-01-05.
        edges = site_time.bucket_edges(date(2026, 12, 30), date(2027, 1, 5), "week", NEW_YORK)
        assert [e.seconds / 86400 for e in edges] == [5, 2]

    def test_iso_week_year_boundary(self):
        # 2027-01-01 belongs to ISO week 53 of 2026, which starts 2026-12-28.
        edges = site_time.bucket_edges(date(2026, 12, 28), date(2027, 1, 10), "week", NEW_YORK)
        assert [e.start for e in edges] == [
            site_time.day_bounds(date(2026, 12, 28), NEW_YORK)[0],
            site_time.day_bounds(date(2027, 1, 4), NEW_YORK)[0],
        ]
        assert [e.seconds / 86400 for e in edges] == [7, 7]

    def test_single_day_window_is_one_clipped_week(self):
        edges = site_time.bucket_edges(date(2026, 1, 1), date(2026, 1, 1), "week", NEW_YORK)
        assert len(edges) == 1
        assert edges[0].seconds == 86400

    def test_week_containing_a_transition_is_an_hour_off_seven_days(self):
        edges = site_time.bucket_edges(date(2026, 10, 26), date(2026, 11, 1), "week", NEW_YORK)
        assert len(edges) == 1
        assert edges[0].seconds == 7 * 86400 + 3600

    @pytest.mark.parametrize("vector", WEEK_VECTORS, ids=[v["date"] for v in WEEK_VECTORS])
    def test_shared_week_start_vectors(self, vector):
        """The grid agrees with the vectors a frontend test reads from the same file."""
        day = date.fromisoformat(vector["date"])
        week_start = date.fromisoformat(vector["week_start"])
        # Start the window on a Monday two weeks back so the bucket holding
        # ``day`` is never the CLIPPED first one.
        edges = site_time.bucket_edges(week_start - timedelta(days=14), day, "week", NEW_YORK)
        assert edges[-1].start == site_time.day_bounds(week_start, NEW_YORK)[0]

    def test_vectors_cover_the_cases_the_grid_can_get_wrong(self):
        dates = {v["date"] for v in WEEK_VECTORS}
        assert {"2026-01-01", "2027-01-01"} <= dates  # year boundaries
        assert {"2026-03-08", "2026-11-01"} <= dates  # both DST weeks
        assert {"2026-09-21", "2026-01-04"} <= dates  # a Monday and a Sunday


class TestBucketEdgeGuards:
    def test_reversed_window_is_refused(self):
        with pytest.raises(ValueError):
            site_time.bucket_edges(date(2026, 6, 2), date(2026, 6, 1), "day", UTC)

    def test_unknown_bucket_is_refused(self):
        with pytest.raises(ValueError):
            site_time.bucket_edges(date(2026, 6, 1), date(2026, 6, 2), "fortnight", UTC)


class TestDefaultBucket:
    @pytest.mark.parametrize(
        ("days", "expected"),
        [(1, "hour"), (3, "hour"), (4, "day"), (30, "day"), (92, "day"), (93, "week"), (366, "week")],
    )
    def test_boundaries(self, days, expected):
        assert site_time.default_bucket(days) == expected


class TestPreviousWindow:
    def test_same_day_count_ending_the_day_before(self):
        assert site_time.previous_window(date(2026, 9, 1), date(2026, 9, 30)) == (date(2026, 8, 2), date(2026, 8, 31))

    def test_single_day_window(self):
        assert site_time.previous_window(FALL_BACK, FALL_BACK) == (date(2026, 10, 31), date(2026, 10, 31))

    def test_across_a_dst_change_the_day_count_is_preserved(self):
        date_from, date_to = date(2026, 11, 1), date(2026, 11, 30)
        previous_from, previous_to = site_time.previous_window(date_from, date_to)
        assert (previous_from, previous_to) == (date(2026, 10, 2), date(2026, 10, 31))
        assert (date_to - date_from).days == (previous_to - previous_from).days

    def test_across_a_month_boundary_of_unequal_length(self):
        assert site_time.previous_window(date(2026, 3, 1), date(2026, 3, 31)) == (date(2026, 1, 29), date(2026, 2, 28))

    def test_across_a_year_boundary(self):
        assert site_time.previous_window(date(2027, 1, 1), date(2027, 1, 7)) == (date(2026, 12, 25), date(2026, 12, 31))

    def test_bucket_counts_match_even_when_one_window_holds_a_transition(self):
        date_from, date_to = date(2026, 11, 1), date(2026, 11, 30)
        previous_from, previous_to = site_time.previous_window(date_from, date_to)
        current = site_time.bucket_edges(date_from, date_to, "day", NEW_YORK)
        previous = site_time.bucket_edges(previous_from, previous_to, "day", NEW_YORK)
        assert len(current) == len(previous)
        # Same number of DAYS, one hour more of seconds: the window that holds
        # the fall-back is genuinely longer, and a rate must divide by that.
        assert sum(e.seconds for e in current) == sum(e.seconds for e in previous) + 3600

    def test_reversed_window_is_refused(self):
        with pytest.raises(ValueError):
            site_time.previous_window(date(2026, 6, 2), date(2026, 6, 1))
