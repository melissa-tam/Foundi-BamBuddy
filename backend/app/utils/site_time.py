"""The site's calendar — one resolver for "which day, hour and week is this".

Timestamps are stored as NAIVE UTC datetimes throughout the fork; an operator
reads the farm in the site's own calendar. This module is the only place that
converts between the two, so a day boundary, a week start and a bucket grid
cannot drift apart between the backup scheduler, a metrics sweep and a route.

Zone resolution has exactly two answers: the ``TZ`` env var when it names a
loadable IANA zone, otherwise the OS zone resolved PER INSTANT through
``datetime.astimezone()``. The per-instant form is what makes a Windows host
with no ``TZ`` DST-correct — a zone captured once as a fixed offset is wrong
for half the year, and a fixed UTC fallback silently moves every site day.

Every function takes an injectable ``tz`` so a caller (and a test) can ask
about a zone other than the host's.

Leaf module: stdlib only, no service or model imports.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

Bucket = Literal["hour", "day", "week"]

_HOUR = timedelta(hours=1)
_DAY = timedelta(days=1)

# One warning per distinct unusable TZ value: this resolver sits under a 30 s
# sampler, so a per-call warning would be the loudest line in the log.
_UNRESOLVABLE_TZ_SEEN: set[str] = set()


@dataclass(frozen=True)
class BucketEdge:
    """One half-open ``[start, end)`` bucket of the grid, in naive UTC.

    ``utc_offset_minutes`` is the site's offset AT ``start``, carried so a
    client can render site-local labels without a tz database of its own.
    """

    start: datetime
    end: datetime
    utc_offset_minutes: int

    @property
    def seconds(self) -> float:
        """Wall-clock width of the bucket — 23 h or 25 h on a transition day."""
        return (self.end - self.start).total_seconds()


def site_zone() -> ZoneInfo | None:
    """The configured site zone, or None meaning "the OS zone, per instant"."""
    name = os.environ.get("TZ", "").strip()
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        if name not in _UNRESOLVABLE_TZ_SEEN:
            _UNRESOLVABLE_TZ_SEEN.add(name)
            logger.warning("TZ=%r names no loadable zone; site days follow the OS zone", name)
        return None


def site_zone_name(now: datetime | None = None, tz: tzinfo | None = None) -> str:
    """A display name for the site zone, for responses and the UI.

    An IANA zone answers with its key; the OS zone has no key, so it answers
    with its name at ``now`` (which is why the instant is a parameter — the
    OS zone renders "…Daylight Time" for half the year).
    """
    zone = _resolved(tz)
    key = getattr(zone, "key", None)
    if key:
        return str(key)
    site = _to_site(now if now is not None else datetime.now(timezone.utc), zone)
    return site.tzname() or str(site.tzinfo)


def to_site(dt: datetime, tz: tzinfo | None = None) -> datetime:
    """Convert a naive-UTC (or aware) instant to an aware site-local datetime."""
    return _to_site(dt, _resolved(tz))


def site_today(now: datetime | None = None, tz: tzinfo | None = None) -> date:
    """The site's current calendar date — not the UTC one."""
    return _to_site(now if now is not None else datetime.now(timezone.utc), _resolved(tz)).date()


def site_instant(d: date, at: time = time.min, tz: tzinfo | None = None) -> datetime:
    """The naive-UTC instant of a site-local wall clock (``d`` at ``at``).

    The one direction that cannot be done by arithmetic on an already-converted
    datetime: an offset read at some other instant is the wrong offset across a
    transition. Callers that schedule a local HH:MM re-resolve through here.
    """
    return _instant(d, at, _resolved(tz))


def day_bounds(d: date, tz: tzinfo | None = None) -> tuple[datetime, datetime]:
    """The half-open ``[start, end)`` of a SITE day, as naive UTC.

    A transition day is 23 h or 25 h long; both come from the two midnights,
    never from adding 24 h to the start.
    """
    return _day_bounds(d, _resolved(tz))


def bucket_edges(
    date_from: date,
    date_to: date,
    bucket: Bucket,
    tz: tzinfo | None = None,
) -> tuple[BucketEdge, ...]:
    """The ordered, contiguous, half-open buckets of an INCLUSIVE site-date window.

    The union is exactly ``[day_bounds(date_from).start, day_bounds(date_to).end)``
    for every bucket size, so day buckets re-aggregate exactly into week buckets
    and into the window total. Weeks are ISO-8601 (Monday 00:00 site-local) and
    the first and last are CLIPPED to the window.
    """
    if date_to < date_from:
        raise ValueError(f"window ends before it starts: {date_from}..{date_to}")
    zone = _resolved(tz)
    if bucket == "day":
        spans = _day_spans(date_from, date_to, zone)
    elif bucket == "week":
        spans = _week_spans(date_from, date_to, zone)
    elif bucket == "hour":
        spans = _hour_spans(date_from, date_to, zone)
    else:
        raise ValueError(f"unknown bucket {bucket!r}")
    return tuple(
        BucketEdge(start=start, end=end, utc_offset_minutes=_offset_minutes(start, zone)) for start, end in spans
    )


def default_bucket(days: int) -> Bucket:
    """The bucket a window of ``days`` site days is read at by default."""
    if days <= 3:
        return "hour"
    if days <= 92:
        return "day"
    return "week"


def previous_window(date_from: date, date_to: date) -> tuple[date, date]:
    """The same NUMBER OF SITE DAYS, ending the day before ``date_from``.

    Calendar-date arithmetic only: a window that crosses a transition has the
    same day count as its predecessor even though it has a different number of
    seconds, and it is days the two windows are compared over.
    """
    if date_to < date_from:
        raise ValueError(f"window ends before it starts: {date_from}..{date_to}")
    days = (date_to - date_from).days + 1
    previous_to = date_from - _DAY
    return previous_to - timedelta(days=days - 1), previous_to


def _resolved(tz: tzinfo | None) -> tzinfo | None:
    """Resolve once per public call — the site zone unless a caller named one."""
    return tz if tz is not None else site_zone()


def _to_site(dt: datetime, zone: tzinfo | None) -> datetime:
    aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    # No argument = the OS zone for THAT instant, which is the whole point of
    # carrying None rather than a captured offset.
    return aware.astimezone() if zone is None else aware.astimezone(zone)


def _instant(d: date, at: time, zone: tzinfo | None) -> datetime:
    local = datetime.combine(d, at)
    if zone is None:
        # A naive datetime is presumed to be OS local time, so the platform
        # resolves the offset for this wall clock — gaps and folds included.
        aware = local.astimezone(timezone.utc)
    else:
        # fold=0: the earlier of an ambiguous pair, and the pre-gap offset for a
        # wall clock that does not exist — both land on the first instant of the
        # day, which is what a day boundary means.
        aware = local.replace(tzinfo=zone).astimezone(timezone.utc)
    return aware.replace(tzinfo=None)


def _day_bounds(d: date, zone: tzinfo | None) -> tuple[datetime, datetime]:
    return _instant(d, time.min, zone), _instant(d + _DAY, time.min, zone)


def _offset_minutes(dt: datetime, zone: tzinfo | None) -> int:
    offset = _to_site(dt, zone).utcoffset() or timedelta()
    return round(offset.total_seconds() / 60)


def _day_spans(date_from: date, date_to: date, zone: tzinfo | None) -> list[tuple[datetime, datetime]]:
    spans = []
    d = date_from
    while d <= date_to:
        spans.append(_day_bounds(d, zone))
        d += _DAY
    return spans


def _week_spans(date_from: date, date_to: date, zone: tzinfo | None) -> list[tuple[datetime, datetime]]:
    spans = []
    d = date_from
    while d <= date_to:
        # isoweekday(): Monday is 1, so this is always the NEXT Monday.
        next_monday = d + timedelta(days=8 - d.isoweekday())
        last = min(next_monday - _DAY, date_to)
        spans.append((_instant(d, time.min, zone), _instant(last + _DAY, time.min, zone)))
        d = last + _DAY
    return spans


def _hour_spans(date_from: date, date_to: date, zone: tzinfo | None) -> list[tuple[datetime, datetime]]:
    spans = []
    d = date_from
    while d <= date_to:
        # Stepped in UTC, never as local wall clocks: 01:30 occurs twice on a
        # fall-back day, so a 25 h day is 25 steps and a 23 h day is 23.
        start, day_end = _day_bounds(d, zone)
        while start < day_end:
            end = min(start + _HOUR, day_end)
            spans.append((start, end))
            start = end
        d += _DAY
    return spans
