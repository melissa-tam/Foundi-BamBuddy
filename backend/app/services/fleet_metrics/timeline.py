"""THE classified timeline — one sweep, read by every figure the Fleet tab shows.

A window's evidence arrives as three row sets (observation spans, incident rows, the
printer roster) and leaves as one value object: for each printer, an ordered,
gapless, non-overlapping list of intervals, each carrying the class the evidence
folds to. Every series downstream is a projection of THIS — the matrix cell, the
fleet average, the peak, the drill-down list — so a figure can never be computed from
a second reading of the same rows, and "why was 009 down 4 h" drills into exactly the
intervals its cell was summed from.

**Pure.** No session, no clock, no settings: ``now`` and the window are arguments, so
the whole sweep runs under ``asyncio.to_thread`` without touching the event loop and
a test can place a printer's whole history on a timeline it chose. The loader
(:mod:`backend.app.services.fleet_metrics.loader`) is the only module that reads rows.

**The grid is the site's, and it is cut twice.** Calendar cuts come only from
``utils.site_time.bucket_edges``: a transition day is 23 h or 25 h, an ISO week is
clipped to the window, and nothing here constructs a midnight. The sweep cuts on the
BASE grid — hours for an hour request, days otherwise — and a week cell is
re-aggregated from its day cells, which is what makes the day→week identity exact
rather than approximately equal.

**Uncovered time is a class, not a discard.** A printer's intervals tile the whole
elapsed window; a stretch nothing observed reads as ``not_recorded`` or ``unobserved``
(or, over a durable hold, still as the fault the ledger proves). That is what keeps
"the classes of a bucket sum to the printers known × the bucket's elapsed seconds"
exactly true, which in turn is what lets a reader check any figure on the page by
hand.
"""

from __future__ import annotations

import heapq
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from typing import TYPE_CHECKING, TypeVar

from backend.app.services.fleet_activity import STALE_AFTER_S, Observation
from backend.app.services.fleet_metrics.classifier import (
    NO_SPAN_YET,
    OBSERVATION_GAP,
    OUT_OF_FLEET,
    AvailabilityClass,
    Seen,
    availability_class,
)
from backend.app.utils.site_time import Bucket, BucketEdge, bucket_edges, site_zone_name

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

# What a bucket's state figures REST ON. ``observed`` means the recorder covered some
# of it, so a state reading is a measurement; ``incidents_only`` means the only
# evidence in that bucket is the incident ledger, and every ratio derived from state
# is withheld rather than computed from a denominator nobody measured.
BASIS_OBSERVED = "observed"
BASIS_INCIDENTS_ONLY = "incidents_only"

# The two row shapes that carry a ``printer_id``; constrained rather than open so the
# one grouping helper below stays honest about what it can bucket.
_RowT = TypeVar("_RowT", "SpanRow", "IncidentRow")


# ── the rows the loader hands in ────────────────────────────────────────────────────
#
# Plain frozen values, not ORM entities: a year of a twelve-printer fleet is on the
# order of half a million spans, and hydrating those as instances would cost more than
# the sweep. Each mirrors the columns its Core select names.


@dataclass(frozen=True, slots=True)
class SpanRow:
    """One observation span, as selected."""

    printer_id: int
    started_at: datetime
    last_observed_at: datetime
    ended_at: datetime | None
    is_active: bool
    connected: bool
    gcode_state: str | None
    plate_phase: str
    quarantined: bool
    usb_present: bool | None
    model_mismatch: bool

    def observation(self) -> Observation:
        """The recorder's own tuple, rebuilt for the classifier it is fed to."""
        return Observation(
            is_active=self.is_active,
            connected=self.connected,
            gcode_state=self.gcode_state,
            plate_phase=self.plate_phase,
            quarantined=self.quarantined,
            usb_present=self.usb_present,
            model_mismatch=self.model_mismatch,
        )


@dataclass(frozen=True, slots=True)
class IncidentRow:
    """One incident's hold interval, as selected."""

    id: int
    printer_id: int
    kind: str
    created_at: datetime
    resolved_at: datetime | None


@dataclass(frozen=True, slots=True)
class RosterPrinter:
    """A printer that EXISTS right now."""

    printer_id: int
    name: str
    model: str | None
    is_active: bool


@dataclass(frozen=True, slots=True)
class PrinterEvidence:
    """Whole-table facts about one printer, independent of the window.

    Aggregates rather than rows: the sweep needs to know when this printer's record
    BEGAN (before it, silence is "not recorded", after it, a hole) and when its last
    evidence of existing was, which is what places a deleted printer out of the fleet.
    Both are computed over the whole table, because a window is not allowed to change
    what "before recording" means.
    """

    printer_id: int
    first_span_start: datetime | None
    last_span_end: datetime | None
    last_incident_at: datetime | None


# ── the window and its grid ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Grid:
    """The requested bucket grid, and the finer one the sweep actually cuts on.

    ``base`` is the hour grid for an hour request and the DAY grid otherwise, and
    ``base_to_bucket`` maps each base cell into the requested cell that contains it.
    A week is therefore the exact sum of its days rather than a second aggregation of
    the same spans, and a "peak concurrent" figure over a week is the max over its
    days — correct, because no interval crosses a base cut.
    """

    bucket: Bucket
    buckets: tuple[BucketEdge, ...]
    base: tuple[BucketEdge, ...]
    base_to_bucket: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class Window:
    """An inclusive SITE-date window, resolved to a half-open naive-UTC interval."""

    date_from: date
    date_to: date
    tz_name: str
    start: datetime
    end: datetime
    grid: Grid


def build_window(date_from: date, date_to: date, bucket: Bucket, tz: tzinfo | None = None) -> Window:
    """Resolve an inclusive site-date window and both grids. The ONE constructor.

    Raises ``ValueError`` (from ``bucket_edges``) when the window ends before it
    starts; every other validation belongs to the loader, which knows the limits the
    route publishes.
    """
    buckets = bucket_edges(date_from, date_to, bucket, tz)
    base = buckets if bucket != "week" else bucket_edges(date_from, date_to, "day", tz)
    grid = Grid(bucket=bucket, buckets=buckets, base=base, base_to_bucket=_base_to_bucket(base, buckets))
    return Window(
        date_from=date_from,
        date_to=date_to,
        tz_name=site_zone_name(buckets[0].start, tz),
        start=buckets[0].start,
        end=buckets[-1].end,
        grid=grid,
    )


def _base_to_bucket(base: tuple[BucketEdge, ...], buckets: tuple[BucketEdge, ...]) -> tuple[int, ...]:
    """Which requested bucket each base cell falls in. Both grids tile one range."""
    mapping: list[int] = []
    index = 0
    for edge in base:
        while index < len(buckets) - 1 and edge.start >= buckets[index].end:
            index += 1
        mapping.append(index)
    return tuple(mapping)


# ── the timeline ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BucketHeader:
    """The arithmetic every series divides by, computed once and shared.

    ``seconds`` is the bucket's full wall-clock width; ``elapsed_seconds`` is that
    width clamped to ``now`` (0 for a bucket entirely in the future). **Averages and
    rates divide by ELAPSED**, never by the width and never by ``observed_seconds``,
    so a half-finished current day compares fairly with a complete previous one and a
    recorder outage shows up as unobserved time rather than silently inflating every
    other class.
    """

    start: datetime
    end: datetime
    seconds: float
    elapsed_seconds: float
    observed_seconds: float
    utc_offset_minutes: int

    @property
    def basis(self) -> str:
        """Whether this bucket's STATE figures rest on observation or on the ledger."""
        return BASIS_OBSERVED if self.observed_seconds > 0 else BASIS_INCIDENTS_ONLY


@dataclass(frozen=True, slots=True)
class Interval:
    """One stretch over which one printer held one class. Half-open ``[start, end)``."""

    start: datetime
    end: datetime
    klass: AvailabilityClass

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


@dataclass(frozen=True, slots=True)
class PrinterTimeline:
    """One printer's classified window, plus who it is.

    ``name`` falls back to the printer's own id for a row the roster no longer has:
    the history is about a machine that existed, and inventing a label here would put
    display copy in a service. ``deleted`` is what the UI branches on.
    """

    printer_id: int
    name: str
    model: str | None
    is_active: bool
    deleted: bool
    intervals: tuple[Interval, ...]
    observed_seconds: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class FleetTimeline:
    """Everything the projections read: one window, one grid, one class per instant."""

    window: Window
    now: datetime
    printers: tuple[PrinterTimeline, ...]
    #: Per BASE cell — the grid the sweep cut on, and where a peak is exact.
    base_headers: tuple[BucketHeader, ...]
    #: Per REQUESTED cell, re-aggregated from ``base_headers`` (sum, never average).
    headers: tuple[BucketHeader, ...]
    #: The whole window as one cell, by the same re-aggregation.
    total: BucketHeader

    @property
    def printers_known(self) -> int:
        """Every printer the window knows of: the roster plus any row that names one."""
        return len(self.printers)


def build_timeline(
    *,
    window: Window,
    now: datetime,
    roster: Sequence[RosterPrinter],
    spans: Sequence[SpanRow],
    incidents: Sequence[IncidentRow],
    evidence: Sequence[PrinterEvidence],
) -> FleetTimeline:
    """Fold the window's rows into one classified timeline. Pure; O(n log n).

    ``spans`` must be ordered by ``started_at`` WITHIN each printer (the grouping below
    preserves the caller's order and nothing re-sorts it); the printers themselves may
    arrive interleaved, which is what lets the loader read the window in index order
    instead of paying for a sort. Spans may overlap only pathologically (a clock step):
    a span's start is clipped to the furthest end already covered, so the result is
    strictly non-overlapping whatever the table holds.
    """
    horizon = min(window.end, now)
    by_span = _group(spans)
    by_incident = _group(incidents)
    roster_by_id = {printer.printer_id: printer for printer in roster}
    evidence_by_id = {fact.printer_id: fact for fact in evidence}

    base = window.grid.base
    base_starts = [edge.start for edge in base]
    base_ends = [edge.end for edge in base]
    # Membership test for the merge rule: two neighbouring intervals of equal class are
    # one interval UNLESS a bucket boundary separates them, so every interval lies
    # wholly inside one base cell and a projection needs one lookup per interval.
    grid_cuts = frozenset(base_starts)

    timelines: list[PrinterTimeline] = []
    union_input: list[tuple[datetime, datetime]] = []
    for printer_id in sorted(set(roster_by_id) | set(by_span) | set(by_incident)):
        coverage = _coverage(by_span.get(printer_id, ()), now, window.start, horizon)
        holds = _holds(by_incident.get(printer_id, ()), now, window.start, horizon)
        fact = evidence_by_id.get(printer_id)
        entry = roster_by_id.get(printer_id)
        own_observed = [0.0] * len(base)
        _add_seconds([(piece.start, piece.end) for piece in coverage], base_starts, base_ends, own_observed)
        union_input.extend((piece.start, piece.end) for piece in coverage)
        timelines.append(
            PrinterTimeline(
                printer_id=printer_id,
                name=entry.name if entry is not None else str(printer_id),
                model=entry.model if entry is not None else None,
                is_active=entry.is_active if entry is not None else False,
                deleted=entry is None,
                intervals=_intervals(
                    coverage=coverage,
                    holds=holds,
                    start=window.start,
                    horizon=horizon,
                    base_starts=base_starts,
                    grid_cuts=grid_cuts,
                    first_span_start=fact.first_span_start if fact is not None else None,
                    evidence_end=_evidence_end(fact, coverage) if entry is None else None,
                ),
                observed_seconds=tuple(own_observed),
            )
        )

    union = [0.0] * len(base)
    _add_seconds(_union(union_input), base_starts, base_ends, union)
    base_headers = tuple(
        BucketHeader(
            start=edge.start,
            end=edge.end,
            seconds=edge.seconds,
            elapsed_seconds=max(0.0, (min(edge.end, now) - edge.start).total_seconds()),
            observed_seconds=union[index],
            utc_offset_minutes=edge.utc_offset_minutes,
        )
        for index, edge in enumerate(base)
    )
    headers = _fold_headers(base_headers, window.grid)
    return FleetTimeline(
        window=window,
        now=now,
        printers=tuple(timelines),
        base_headers=base_headers,
        headers=headers,
        total=_sum_headers(headers, window.start, window.end, window.grid.buckets[0].utc_offset_minutes),
    )


def printer_slice(timeline: FleetTimeline, printer_id: int) -> PrinterTimeline | None:
    """That printer's part of the timeline, or ``None`` when the window knows none."""
    return next((entry for entry in timeline.printers if entry.printer_id == printer_id), None)


# ── the sweep ───────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Coverage:
    """One stretch the recorder actually covered, and what it read there."""

    start: datetime
    end: datetime
    observation: Observation


def _coverage(spans: Iterable[SpanRow], now: datetime, start: datetime, horizon: datetime) -> list[_Coverage]:
    """What each span is evidence FOR, clipped to the window and to ``now``.

    An OPEN span is read by the recorder's own freshness rule (:data:`STALE_AFTER_S`),
    the one definition both sides share: fresh means the printer still reads this way
    and the span covers up to now; stale means its evidence ended at its last sample
    and the rest of it is a hole. Extending a stale span to now would claim a reading
    through a stretch nothing observed — which is exactly the restart gap the recorder
    goes to trouble to leave visible.
    """
    pieces: list[_Coverage] = []
    covered_to: datetime | None = None
    for span in spans:
        span_start = span.started_at if covered_to is None else max(span.started_at, covered_to)
        end = span_end(span, now)
        covered_to = end if covered_to is None else max(covered_to, end)
        low = max(span_start, start)
        high = min(end, horizon)
        if high > low:
            pieces.append(_Coverage(start=low, end=high, observation=span.observation()))
    return pieces


def span_end(span: SpanRow, now: datetime) -> datetime:
    """The instant a span stops being evidence. THE definition, shared by both readers.

    Public because the loader asks it too: deciding whether the one span that starts
    before a window still reaches into it is the same question as deciding how far
    that span covers, and answering it twice would let a straddler be admitted by one
    rule and then read by another.
    """
    if span.ended_at is not None:
        return span.ended_at
    if (now - span.last_observed_at).total_seconds() <= STALE_AFTER_S:
        return now
    return span.last_observed_at


def _holds(
    rows: Iterable[IncidentRow], now: datetime, start: datetime, horizon: datetime
) -> list[tuple[datetime, datetime, str]]:
    """Each incident as ``[created_at, resolved_at or now)``, clipped to the window."""
    holds: list[tuple[datetime, datetime, str]] = []
    for row in rows:
        low = max(row.created_at, start)
        high = min(row.resolved_at if row.resolved_at is not None else now, horizon)
        if high > low:
            holds.append((low, high, row.kind))
    holds.sort()
    return holds


def _evidence_end(fact: PrinterEvidence | None, coverage: list[_Coverage]) -> datetime | None:
    """When a DELETED printer was last evidenced to exist, or ``None`` if never.

    The later of its last span coverage and its latest incident, because deleting a
    printer does not close its incident rows: an orphaned open incident would
    otherwise hold a machine that no longer exists in downtime forever, and a fleet
    average would carry it for as long as the farm runs.
    """
    candidates = [value for value in (_last_span_end(fact), _last_incident_at(fact)) if value is not None]
    if coverage:
        candidates.append(coverage[-1].end)
    return max(candidates) if candidates else None


def _last_span_end(fact: PrinterEvidence | None) -> datetime | None:
    return fact.last_span_end if fact is not None else None


def _last_incident_at(fact: PrinterEvidence | None) -> datetime | None:
    return fact.last_incident_at if fact is not None else None


def _intervals(
    *,
    coverage: list[_Coverage],
    holds: list[tuple[datetime, datetime, str]],
    start: datetime,
    horizon: datetime,
    base_starts: list[datetime],
    grid_cuts: frozenset[datetime],
    first_span_start: datetime | None,
    evidence_end: datetime | None,
) -> tuple[Interval, ...]:
    """Classify one printer's whole window, then merge what the class did not change.

    Cut points are every instant at which the ANSWER could change — a span edge, an
    incident edge, a bucket boundary, the window's own bounds and (for a deleted
    printer) the moment it stopped being evidenced — so each elementary interval is
    classified exactly once. Merging afterwards is what keeps a drill-down readable:
    six consecutive spans that all read *offline* are one four-hour outage, not six
    rows, and a span change that does not change the class must not split it.
    """
    if horizon <= start:
        return ()
    cuts = {start, horizon}
    cuts.update(instant for instant in base_starts if start < instant < horizon)
    for piece in coverage:
        cuts.add(piece.start)
        cuts.add(piece.end)
    for low, high, _ in holds:
        cuts.add(low)
        cuts.add(high)
    if evidence_end is not None and start < evidence_end < horizon:
        cuts.add(evidence_end)
    points = sorted(instant for instant in cuts if start <= instant <= horizon)

    active = _ActiveKinds(holds)
    index = 0
    merged: list[tuple[datetime, datetime, AvailabilityClass]] = []
    for position in range(len(points) - 1):
        left = points[position]
        right = points[position + 1]
        while index < len(coverage) and coverage[index].end <= left:
            index += 1
        seen = _seen_at(coverage, index, left, first_span_start)
        if evidence_end is not None and left >= evidence_end:
            klass = OUT_OF_FLEET
        else:
            klass = availability_class(seen, active.at(left))
        # ``is`` and not ``==``: classes are interned, so identity IS equality here and
        # the merge cannot accidentally join two verdicts that merely look alike.
        if merged and merged[-1][1] == left and merged[-1][2] is klass and left not in grid_cuts:
            merged[-1] = (merged[-1][0], right, klass)
        else:
            merged.append((left, right, klass))
    return tuple(Interval(start=low, end=high, klass=klass) for low, high, klass in merged)


def _seen_at(coverage: list[_Coverage], index: int, instant: datetime, first_span_start: datetime | None) -> Seen:
    """The evidence at ``instant``, or the REASON there is none."""
    if index < len(coverage) and coverage[index].start <= instant < coverage[index].end:
        return coverage[index].observation
    if first_span_start is None or instant < first_span_start:
        return NO_SPAN_YET
    return OBSERVATION_GAP


class _ActiveKinds:
    """Which incident kinds hold a printer at an instant, as a forward-only sweep.

    A printer can carry several concurrent holds (one row per kind), so this keeps a
    count per kind rather than a flag, and rebuilds the frozenset only when membership
    actually changes — the classifier is memoised on that set, and minting an equal
    one per interval would defeat the cache it depends on.
    """

    __slots__ = ("_counts", "_current", "_index", "_open", "_starts")

    def __init__(self, holds: list[tuple[datetime, datetime, str]]) -> None:
        self._starts = holds
        self._index = 0
        # A heap keyed on END: the sweep only ever asks "what has expired by now", and
        # a rescan of the hold list per cut point would make a year-long window
        # quadratic in a printer's incident count.
        self._open: list[tuple[datetime, str]] = []
        self._counts: dict[str, int] = {}
        self._current: frozenset[str] = frozenset()

    def at(self, instant: datetime) -> frozenset[str]:
        changed = False
        while self._index < len(self._starts) and self._starts[self._index][0] <= instant:
            _, high, kind = self._starts[self._index]
            heapq.heappush(self._open, (high, kind))
            self._counts[kind] = self._counts.get(kind, 0) + 1
            self._index += 1
            changed = True
        while self._open and self._open[0][0] <= instant:
            _, kind = heapq.heappop(self._open)
            remaining = self._counts.get(kind, 0) - 1
            if remaining > 0:
                self._counts[kind] = remaining
            else:
                self._counts.pop(kind, None)
            changed = True
        if changed:
            self._current = frozenset(self._counts)
        return self._current


# ── bucket arithmetic ───────────────────────────────────────────────────────────────


def _union(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """Merge overlapping intervals — the recorder COVERAGE of the whole fleet.

    A bucket's ``observed_seconds`` is a union and not a sum: it answers "was the farm
    being watched", so two printers observed over the same minute are one observed
    minute, and a sum would report more coverage than the bucket is wide.
    """
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [ordered[0]]
    for low, high in ordered[1:]:
        last_low, last_high = merged[-1]
        if low <= last_high:
            if high > last_high:
                merged[-1] = (last_low, high)
        else:
            merged.append((low, high))
    return merged


def _add_seconds(
    intervals: Iterable[tuple[datetime, datetime]],
    base_starts: list[datetime],
    base_ends: list[datetime],
    out: list[float],
) -> None:
    """Add each interval's seconds into the base cells it spans, splitting at edges."""
    for low, high in intervals:
        cursor = low
        index = max(0, bisect_right(base_starts, cursor) - 1)
        while index < len(base_starts) and cursor < high:
            piece_end = min(high, base_ends[index])
            if piece_end > cursor:
                out[index] += (piece_end - cursor).total_seconds()
            cursor = piece_end
            index += 1


def bucket_index(base_starts: Sequence[datetime], instant: datetime) -> int:
    """Which base cell ``instant`` falls in. Clamped, so an edge case lands in range."""
    return min(max(0, bisect_right(base_starts, instant) - 1), len(base_starts) - 1)


def _fold_headers(base_headers: tuple[BucketHeader, ...], grid: Grid) -> tuple[BucketHeader, ...]:
    """Re-aggregate base cells into the requested grid: sum seconds, never average.

    Identity (e) in one function: a week's width, elapsed and observed seconds are the
    SUMS of its days', so a rate over a week and the same rate re-derived from the day
    series agree exactly instead of to within a rounding of averaged averages.
    """
    if grid.base is grid.buckets:
        return base_headers
    totals = [[0.0, 0.0, 0.0] for _ in grid.buckets]
    for position, header in enumerate(base_headers):
        row = totals[grid.base_to_bucket[position]]
        row[0] += header.seconds
        row[1] += header.elapsed_seconds
        row[2] += header.observed_seconds
    return tuple(
        BucketHeader(
            start=edge.start,
            end=edge.end,
            seconds=totals[index][0],
            elapsed_seconds=totals[index][1],
            observed_seconds=totals[index][2],
            utc_offset_minutes=edge.utc_offset_minutes,
        )
        for index, edge in enumerate(grid.buckets)
    )


def _sum_headers(
    headers: tuple[BucketHeader, ...], start: datetime, end: datetime, utc_offset_minutes: int
) -> BucketHeader:
    """The window as one cell — the ``totals`` every envelope carries."""
    return BucketHeader(
        start=start,
        end=end,
        seconds=sum(header.seconds for header in headers),
        elapsed_seconds=sum(header.elapsed_seconds for header in headers),
        observed_seconds=sum(header.observed_seconds for header in headers),
        utc_offset_minutes=utc_offset_minutes,
    )


def _group(rows: Iterable[_RowT]) -> dict[int, list[_RowT]]:
    """Bucket rows by printer id, preserving the select's order within each printer."""
    grouped: dict[int, list[_RowT]] = {}
    for row in rows:
        grouped.setdefault(row.printer_id, []).append(row)
    return grouped
