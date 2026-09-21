"""Every fleet figure, as a pure projection of ONE timeline.

Nothing here re-reads a row set or re-derives a number that another function already
owns: the class seconds are summed once (:func:`class_totals`), each figure divides
them by the ONE coverage its own question is answerable over, the window totals
re-aggregate the same seconds rather than averaging the buckets' averages, and the
summary card reads the series' own numbers instead of computing a second version of
them.

**Two coverages, on purpose, and which one a figure uses is the whole design.**

A fleet STATE figure is a measurement, and it is measured INSIDE THE OBSERVED MASK:
per bucket, every printer's class seconds are clipped to the mask ``M`` (the union of
all span coverage in that bucket) and divided by ``|M|``. Production made the case:
with the recorder 45 minutes old and 44 days of ledger behind it, dividing by elapsed
time reported a twelve-printer farm as ``printers_in_fleet = 1.29`` and
``avg_printing = 0.04`` while eight machines were visibly printing. Every one of those
figures satisfied the old identity exactly — the identity held and the headline lied,
because the denominator counted time nobody was watching.

The MATRIX keeps the other coverage: per printer × bucket seconds over the FULL
elapsed bucket, ledger evidence included, because a fault that stood through a
recorder gap really was that printer's downtime and an operator asking "why was 009
down on the 3rd" must be shown it. So the matrix and the fleet averages deliberately
disagree about a recorder gap, and each is right about its own question.

**The identities, under that rule** — each stated again beside the code that produces
it, and pinned in ``test_fleet_metrics_series.py``:

a. in an OBSERVED bucket, every known printer contributes exactly ``|M|`` clipped
   seconds across all classes (a printer with no span of its own reads *unobserved*
   there), so the per-group average concurrent printers sum to ``printers_known``;
b. ``printers_in_fleet = printers_known − avg(out_of_fleet) − avg(not_recorded)``,
   over the same mask;
c. the down causes sum to down;
d. the printers' clipped down seconds ÷ ``|M|`` is the fleet's ``avg_down``;
e. day cells re-aggregate EXACTLY into week cells and into the window totals — sums
   of clipped seconds over sums of ``|M|``, never an average of averages;
f. ``avg_down ≤ peak_down ≤ printers_known`` (the peak stays a maximum over
   everything KNOWN in the bucket, ledger included, so it can only be larger);
g. the ``down:fault`` seconds are at most the incident-held seconds clipped to the
   window (equal only when nothing was observed running through the hold);
h. one printer's intervals sum, per class, to its MATRIX cell — the unclipped one;
i. the ratios share one denominator, ``scheduled``, itself inside the mask, and are
   null without it.

An ``incidents_only`` bucket measures nothing, so it reports only what the ledger
knows — fault and planned averages over elapsed time — and withholds the rest.

They are not decoration. They are what lets an operator check any number on the page
against the matrix beside it, which is the difference between a dashboard that is
trusted and one that is argued with.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from backend.app.models.farm_cycle_episode import KIND_COOLDOWN, KIND_EJECT
from backend.app.models.printer_incident import FAULT_KINDS
from backend.app.schemas.fleet_metrics import (
    ClassifiedInterval,
    CycleGroup,
    CyclePrinterGroup,
    CycleProjection,
    CycleStats,
    FleetSeriesValues,
    IncidentInterval,
    IncidentSummary,
    MatrixCell,
    MatrixPrinter,
    MatrixProjection,
    MatrixValues,
    PrinterIntervalsResponse,
    PrinterRef,
    RecoveryProjection,
    RecoveryValues,
    SeriesBucket,
    SeriesEnvelope,
    SummaryProjection,
    SummaryRow,
    ThroughputValues,
    TimeToRecover,
    UnitsValues,
)
from backend.app.services.fleet_metrics.classifier import (
    GROUP_CYCLE_OVERHEAD,
    GROUP_DOWN,
    GROUP_IDLE,
    GROUP_NOT_RECORDED,
    GROUP_OUT_OF_FLEET,
    GROUP_PLANNED,
    GROUP_PRINTING,
    GROUP_UNOBSERVED,
    AvailabilityClass,
)
from backend.app.services.fleet_metrics.timeline import (
    BASIS_INCIDENTS_ONLY,
    BASIS_OBSERVED,
    BucketHeader,
    FleetTimeline,
    PrinterTimeline,
    bucket_index,
)
from backend.app.services.print_log import outcome_bucket

# ``nearest_rank_p90`` is the fork's ONE p90 — the incident ledger defines it, with its
# rationale, and a second percentile method would make "p90" mean two things on one
# page. Importing it is reuse; copying it would be the drift this package exists to
# avoid.
from backend.app.services.printer_incidents import (
    held_stats,
    nearest_rank_p90,
    summary as incident_summary,
)
from backend.app.services.sku_catalog import plate_units

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from backend.app.models.printer_incident import PrinterIncident
    from backend.app.services.fleet_metrics.timeline import IncidentRow

_SECONDS_PER_DAY = 86400.0
_SECONDS_PER_HOUR = 3600.0

# The outcome bucket a print status falls in when it falls in none. It is counted and
# named rather than dropped, so a throughput total always reconciles with a plain
# COUNT over the same window — a figure that silently omits rows is a figure nobody
# can check.
OUTCOME_OTHER = "other"
OUTCOME_COMPLETED = "completed"
OUTCOME_FAILED = "failed"

# The one outcome per episode kind that IS a measured duration of the thing being
# measured. A cooldown cut short by an operator clearing the gate never reached its
# temperature, and a sweep the runtime watchdog stopped never finished its motion;
# both are real events and neither is a cooling time or an eject time. The vocabulary
# belongs to the measuring owners (``eject.cooldown_prep.WatchVerdict`` for the
# cooldown, the printer's own terminal word for the eject) and neither publishes it as
# an importable constant, so it is named once here.
_FINISHED_OUTCOME: dict[str, str] = {KIND_COOLDOWN: "released", KIND_EJECT: "completed"}

# Summary row keys. The UI maps them to copy; nothing here renders a label.
ROW_AVG_PRINTING = "avg_printing"
ROW_AVG_CYCLE_OVERHEAD = "avg_cycle_overhead"
ROW_AVG_IDLE = "avg_idle"
ROW_AVG_DOWN = "avg_down"
ROW_AVG_PLANNED = "avg_planned"
ROW_PEAK_DOWN = "peak_down"
ROW_PRINTERS_IN_FLEET = "printers_in_fleet"
ROW_PRINTS_PER_DAY = "prints_per_day"
ROW_PRINTS_PER_PRINTER_PER_DAY = "prints_per_printer_per_day"
ROW_UPTIME = "uptime"
ROW_TIME_PRINTING = "time_printing"


# ── the rows the loader hands in (non-state sources) ────────────────────────────────


@dataclass(frozen=True, slots=True)
class PrintLogRow:
    """One print EVENT, as selected from the print log."""

    created_at: datetime
    status: str | None
    printer_id: int | None


@dataclass(frozen=True, slots=True)
class UnitRow:
    """One completed queue plate, joined to the SKU file that says what it yields."""

    completed_at: datetime
    units_per_plate: int | None
    sku_code: str | None


@dataclass(frozen=True, slots=True)
class EpisodeRow:
    """One measured cooldown or eject, as selected."""

    printer_id: int
    kind: str
    started_at: datetime
    ended_at: datetime
    expected_s: float | None
    outcome: str | None
    variant: str | None

    @property
    def seconds(self) -> float:
        return (self.ended_at - self.started_at).total_seconds()


# ── the one accumulation ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ClassTotals:
    """Class seconds per printer × bucket, summed ONCE for every projection.

    Built by :func:`class_totals` and threaded through the projections by the loader,
    rather than each of them folding the same intervals again: five projections
    re-summing half a million intervals is five times the cost and five places a
    definition of *down* could drift.
    """

    timeline: FleetTimeline
    #: printer id -> per requested bucket -> class -> seconds. FULL elapsed bucket,
    #: ledger evidence included — the MATRIX's coverage (rule C).
    per_printer: dict[int, list[dict[AvailabilityClass, float]]]
    #: per requested bucket -> class -> seconds, over the whole fleet. Same coverage.
    fleet: list[dict[AvailabilityClass, float]]
    #: The same two CLIPPED TO THE OBSERVED MASK — the fleet STATE figures' coverage
    #: (rule A). Kept beside the unclipped pair rather than replacing it, because the
    #: matrix and the averages answer different questions about the same intervals.
    per_printer_masked: dict[int, list[dict[AvailabilityClass, float]]]
    fleet_masked: list[dict[AvailabilityClass, float]]
    #: printer id -> per requested bucket -> that printer's own observed seconds.
    per_printer_observed: dict[int, list[float]]
    #: per requested bucket -> the most printers down at once inside it.
    peak_down: list[int]
    #: per requested bucket -> ``printers_known − avg(out_of_fleet) − avg(not_recorded)``
    #: inside the mask. Computed HERE and read by both the fleet series and the
    #: per-printer print rate, so the two cannot disagree about how many printers the
    #: farm had (rule B's denominator is rule A's figure).
    printers_in_fleet: list[float]
    #: Elapsed seconds of the buckets that COULD have been known about: observed, or
    #: ending after the fleet's ``history_since``. The denominator of hours-down-per-day
    #: — days before any record at all must not dilute a downtime rate.
    history_elapsed_seconds: float
    #: The same, re-aggregated over the whole window.
    per_printer_window: dict[int, dict[AvailabilityClass, float]]
    fleet_window: dict[AvailabilityClass, float]
    fleet_masked_window: dict[AvailabilityClass, float]
    per_printer_observed_window: dict[int, float]
    peak_down_window: int
    printers_in_fleet_window: float


def class_totals(timeline: FleetTimeline) -> ClassTotals:
    """Fold the timeline's intervals into per-printer, per-bucket class seconds.

    Each interval lies wholly inside one base cell (the timeline's merge stops at grid
    cuts), so this is one bucket lookup per interval and no interval is ever split
    across two cells — which is what makes identity (h) exact rather than nearly so.

    ``peak_down`` is computed per BASE cell and taken as a MAX upward: concurrency
    cannot span a cut, so the largest overlap inside a week really is the largest
    overlap inside one of its days.
    """
    grid = timeline.window.grid
    base_starts = [header.start for header in timeline.base_headers]
    count = len(timeline.headers)
    mask = timeline.observed_mask

    per_printer: dict[int, list[dict[AvailabilityClass, float]]] = {}
    per_printer_masked: dict[int, list[dict[AvailabilityClass, float]]] = {}
    per_printer_observed: dict[int, list[float]] = {}
    fleet: list[dict[AvailabilityClass, float]] = [{} for _ in range(count)]
    fleet_masked: list[dict[AvailabilityClass, float]] = [{} for _ in range(count)]
    down_by_base: list[list[tuple[datetime, datetime]]] = [[] for _ in timeline.base_headers]

    for printer in timeline.printers:
        cells: list[dict[AvailabilityClass, float]] = [{} for _ in range(count)]
        masked_cells: list[dict[AvailabilityClass, float]] = [{} for _ in range(count)]
        observed = [0.0] * count
        for position, seconds in enumerate(printer.observed_seconds):
            observed[grid.base_to_bucket[position]] += seconds
        # One forward pointer per printer: this printer's intervals ascend and the mask
        # ascends, so the clip never rescans.
        cursor = 0
        for interval in printer.intervals:
            base_position = bucket_index(base_starts, interval.start)
            index = grid.base_to_bucket[base_position]
            seconds = interval.seconds
            cells[index][interval.klass] = cells[index].get(interval.klass, 0.0) + seconds
            fleet[index][interval.klass] = fleet[index].get(interval.klass, 0.0) + seconds
            # ...and the same interval again, inside the mask. An interval lies wholly
            # within one base cell (the sweep's merge stops at grid cuts), so whatever
            # survives the clip belongs to that same cell.
            cursor, clipped = _clip_seconds(interval.start, interval.end, mask, cursor)
            if clipped:
                masked_cells[index][interval.klass] = masked_cells[index].get(interval.klass, 0.0) + clipped
                fleet_masked[index][interval.klass] = fleet_masked[index].get(interval.klass, 0.0) + clipped
            if interval.klass.is_down:
                down_by_base[base_position].append((interval.start, interval.end))
        per_printer[printer.printer_id] = cells
        per_printer_masked[printer.printer_id] = masked_cells
        per_printer_observed[printer.printer_id] = observed

    peak_down = [0] * count
    for base_position, intervals in enumerate(down_by_base):
        index = grid.base_to_bucket[base_position]
        peak_down[index] = max(peak_down[index], _peak(intervals))

    known = timeline.printers_known
    fleet_masked_window = _merge(fleet_masked)
    return ClassTotals(
        timeline=timeline,
        per_printer=per_printer,
        fleet=fleet,
        per_printer_masked=per_printer_masked,
        fleet_masked=fleet_masked,
        per_printer_observed=per_printer_observed,
        peak_down=peak_down,
        printers_in_fleet=[
            _printers_in_fleet(fleet_masked[index], header.observed_seconds, known)
            for index, header in enumerate(timeline.headers)
        ],
        history_elapsed_seconds=_history_elapsed(timeline),
        per_printer_window={printer_id: _merge(cells) for printer_id, cells in per_printer.items()},
        fleet_window=_merge(fleet),
        fleet_masked_window=fleet_masked_window,
        per_printer_observed_window={printer_id: sum(values) for printer_id, values in per_printer_observed.items()},
        peak_down_window=max(peak_down, default=0),
        printers_in_fleet_window=_printers_in_fleet(fleet_masked_window, timeline.total.observed_seconds, known),
    )


def _clip_seconds(
    start: datetime, end: datetime, mask: tuple[tuple[datetime, datetime], ...], cursor: int
) -> tuple[int, float]:
    """Seconds of ``[start, end)`` that fall inside the observed mask, and the cursor.

    The mask is disjoint and ascending, so this is a merge step rather than a search:
    the cursor is only ever advanced past stretches that end before ``start``, which is
    safe because the caller feeds intervals in ascending order.
    """
    while cursor < len(mask) and mask[cursor][1] <= start:
        cursor += 1
    total = 0.0
    index = cursor
    while index < len(mask) and mask[index][0] < end:
        low = max(start, mask[index][0])
        high = min(end, mask[index][1])
        if high > low:
            total += (high - low).total_seconds()
        if mask[index][1] >= end:
            break
        index += 1
    return cursor, total


def _printers_in_fleet(masked: dict[AvailabilityClass, float], observed: float, known: int) -> float:
    """Identity (b), inside the mask: the roster less what was measured as not in it.

    0.0 where nothing was observed. The schema's field is not nullable, so a bucket
    that measured nothing reports the one value that cannot be mistaken for a
    measurement — and the summary row that an operator actually reads is withheld
    outright there (see :func:`compose_summary`).
    """
    if observed <= 0:
        return 0.0
    groups = _by_group(masked)
    return known - groups.get(GROUP_OUT_OF_FLEET, 0.0) / observed - groups.get(GROUP_NOT_RECORDED, 0.0) / observed


def _history_elapsed(timeline: FleetTimeline) -> float:
    """Elapsed seconds of the buckets the farm could have known anything about.

    A bucket counts when it was observed, or when it ends after the first evidence of
    any kind exists — the ledger could have carried a fault there. Buckets that predate
    the record entirely are excluded, because dividing a printer's downtime by days on
    which nothing could have been recorded reports a broken machine as a healthy one.
    """
    since = timeline.history_since
    return sum(
        header.elapsed_seconds
        for header in timeline.headers
        if header.observed_seconds > 0 or (since is not None and header.end > since)
    )


def _peak(intervals: list[tuple[datetime, datetime]]) -> int:
    """The largest number of intervals overlapping at any instant. Joint +1/−1 sweep.

    Ends are applied BEFORE starts at an equal instant, because two half-open
    intervals ``[a, b)`` and ``[b, c)`` on different printers do not overlap — counting
    them as two concurrent outages would report a peak the farm never had.
    """
    if not intervals:
        return 0
    events: list[tuple[datetime, int]] = []
    for low, high in intervals:
        events.append((low, 1))
        events.append((high, -1))
    events.sort()
    best = 0
    current = 0
    for _, delta in events:
        current += delta
        best = max(best, current)
    return best


def _merge(cells: Iterable[dict[AvailabilityClass, float]]) -> dict[AvailabilityClass, float]:
    """Sum per-bucket class seconds into one window total — identity (e), per class."""
    total: dict[AvailabilityClass, float] = {}
    for cell in cells:
        for klass, seconds in cell.items():
            total[klass] = total.get(klass, 0.0) + seconds
    return total


# ── shared arithmetic ───────────────────────────────────────────────────────────────


def _by_group(cell: dict[AvailabilityClass, float]) -> dict[str, float]:
    """Class seconds folded onto their groups. Sparse — a zero group is absent."""
    groups: dict[str, float] = {}
    for klass, seconds in cell.items():
        if seconds:
            groups[klass.group] = groups.get(klass.group, 0.0) + seconds
    return groups


def _down_by_cause(cell: dict[AvailabilityClass, float]) -> dict[str, float]:
    """Down seconds split by cause — identity (c): these sum to the down group."""
    causes: dict[str, float] = {}
    for klass, seconds in cell.items():
        if klass.is_down and seconds and klass.cause is not None:
            causes[klass.cause] = causes.get(klass.cause, 0.0) + seconds
    return causes


def _down_seconds(cell: dict[AvailabilityClass, float]) -> float:
    return sum(seconds for klass, seconds in cell.items() if klass.is_down)


def _scheduled_seconds(masked: dict[AvailabilityClass, float], observed: float, printers_known: int) -> float:
    """Printer-seconds the farm was actually expected to be producing in.

    Identity (i)'s single denominator, INSIDE THE MASK: observed printer-seconds less
    the time nobody could have printed in anyway — out of fleet, before recording,
    unobserved, and planned maintenance. Uptime and time-printing share it, so the two
    ratios are always comparable and neither can be improved by changing what it
    divides by.

    Measuring it inside the mask is what stops the two halves of the ratio coming from
    different coverages: the ledger knows a printer's fault for the whole elapsed day
    while the recorder knows its printing for one observed hour, and a ratio built from
    both reads as an availability collapse that never happened.
    """
    groups = _by_group(masked)
    return max(
        0.0,
        printers_known * observed
        - groups.get(GROUP_OUT_OF_FLEET, 0.0)
        - groups.get(GROUP_NOT_RECORDED, 0.0)
        - groups.get(GROUP_UNOBSERVED, 0.0)
        - groups.get(GROUP_PLANNED, 0.0),
    )


def _ratios(
    masked: dict[AvailabilityClass, float], header: BucketHeader, printers_known: int
) -> tuple[float | None, float | None]:
    """``(uptime, time_printing)``, or ``(None, None)`` where neither has a meaning.

    Both are withheld on a bucket nothing observed: an availability computed from the
    incident ledger alone would read 100 % for every hour the recorder was down, which
    is the most dangerous number this page could show.
    """
    if header.elapsed_seconds <= 0 or header.observed_seconds <= 0:
        return None, None
    scheduled = _scheduled_seconds(masked, header.observed_seconds, printers_known)
    if scheduled <= 0:
        return None, None
    groups = _by_group(masked)
    uptime = (scheduled - groups.get(GROUP_DOWN, 0.0)) / scheduled
    return uptime, groups.get(GROUP_PRINTING, 0.0) / scheduled


def _rate(count: float, seconds: float, per: float = _SECONDS_PER_DAY) -> float | None:
    """``count`` per ``per`` seconds of ``seconds``; ``None`` when there are none."""
    if seconds <= 0:
        return None
    return count * per / seconds


def _sparse(values: dict[str, float]) -> dict[str, float]:
    return {key: value for key, value in values.items() if value}


def _sparse_counts(values: dict[str, int]) -> dict[str, int]:
    return {key: value for key, value in values.items() if value}


# ── fleet series ────────────────────────────────────────────────────────────────────


def fleet_series(totals: ClassTotals) -> SeriesEnvelope[FleetSeriesValues]:
    """The fleet over time, in AVERAGE CONCURRENT PRINTERS per class.

    Identity (a) lives here: inside the observed mask every known printer accounts for
    exactly ``|M|`` seconds across all classes, so dividing the clipped class seconds
    by ``|M|`` makes the per-group averages sum to ``printers_known``. Identity (b)
    then follows by subtraction rather than by a second count of the roster.

    Window totals re-aggregate the same way — Σ clipped seconds ÷ Σ ``|M|``, both taken
    over the observed buckets, since an unobserved bucket contributes nothing to
    either. Identity (e) is that sum, never an average of averages.
    """
    timeline = totals.timeline
    known = timeline.printers_known
    buckets = [
        SeriesBucket[FleetSeriesValues](
            start=header.start,
            seconds=header.seconds,
            elapsed_seconds=header.elapsed_seconds,
            observed_seconds=header.observed_seconds,
            utc_offset_minutes=header.utc_offset_minutes,
            basis=header.basis,
            values=_fleet_values(
                totals.fleet_masked[index],
                totals.fleet[index],
                header,
                known,
                totals.peak_down[index],
                totals.printers_in_fleet[index],
            ),
        )
        for index, header in enumerate(timeline.headers)
    ]
    return SeriesEnvelope[FleetSeriesValues](
        buckets=buckets,
        totals=_fleet_values(
            totals.fleet_masked_window,
            totals.fleet_window,
            timeline.total,
            known,
            totals.peak_down_window,
            totals.printers_in_fleet_window,
        ),
    )


def _fleet_values(
    masked: dict[AvailabilityClass, float],
    ledger: dict[AvailabilityClass, float],
    header: BucketHeader,
    known: int,
    peak: int,
    in_fleet: float,
) -> FleetSeriesValues:
    """One bucket's state figures, from whichever evidence the bucket actually has.

    Three cases, and they are three different statements:

    * OBSERVED — the mask has length, so every figure is a measurement inside it;
    * ``incidents_only`` — nothing was watched, so only what the LEDGER knows is
      reported (fault and planned averages, over elapsed time) and the rest is
      withheld. Reporting a printing average of zero there would be indistinguishable
      from a farm that genuinely printed nothing;
    * not yet elapsed — a future bucket answers with empty maps rather than zeros:
      nothing happened in it because it has not happened yet.
    """
    averages: dict[str, float] = {}
    causes: dict[str, float] = {}
    if header.observed_seconds > 0:
        observed = header.observed_seconds
        averages = {group: seconds / observed for group, seconds in _by_group(masked).items()}
        causes = {cause: seconds / observed for cause, seconds in _down_by_cause(masked).items()}
    elif header.elapsed_seconds > 0:
        elapsed = header.elapsed_seconds
        # The ledger's own two groups and nothing else. A fault or a declared hold is
        # durable evidence the recorder's absence cannot erase, and it is the only
        # thing an unwatched bucket can honestly report.
        averages = {
            group: seconds / elapsed
            for group, seconds in _by_group(ledger).items()
            if group in (GROUP_DOWN, GROUP_PLANNED)
        }
        causes = {cause: seconds / elapsed for cause, seconds in _down_by_cause(ledger).items()}
    uptime, time_printing = _ratios(masked, header, known)
    return FleetSeriesValues(
        printers_known=known,
        printers_in_fleet=in_fleet,
        avg_by_group=_sparse(averages),
        avg_down_by_cause=_sparse(causes),
        avg_down=averages.get(GROUP_DOWN, 0.0),
        # Deliberately NOT clipped: the peak answers "how bad did it get", and a fault
        # the ledger proves stood through a recorder gap was as bad as it looked. It is
        # therefore an upper bound on ``avg_down`` from a strictly larger evidence set,
        # which is what keeps identity (f) true rather than accidental.
        peak_down=peak,
        uptime=uptime,
        time_printing=time_printing,
    )


# ── matrix ──────────────────────────────────────────────────────────────────────────


def matrix(totals: ClassTotals, prints: PrintTally) -> MatrixProjection:
    """Printer × bucket: where the time went, and what came out.

    **The matrix keeps the FULL elapsed bucket, ledger evidence included** — the other
    coverage from the fleet averages above, deliberately. The two answer different
    questions: an average asks "how many printers were printing", which can only be
    measured where something was measuring, while a cell asks "what was THIS printer
    doing on the 3rd", and a fault the incident ledger proves stood through a recorder
    gap is that printer's downtime whether or not anyone was watching. Clipping the
    matrix to the observed mask would erase exactly the evidence an operator opens it
    to find.

    Identity (h): a cell's ``class_seconds`` are exactly the intervals
    :func:`printer_intervals` lists for the same printer and bucket, because both read
    the one timeline and neither re-classifies anything — which is only true because
    the cell is the unclipped fold.
    """
    timeline = totals.timeline
    per_printer_prints, fleet_prints = prints.per_printer, prints.fleet
    buckets: list[SeriesBucket[MatrixValues]] = []
    for index, header in enumerate(timeline.headers):
        buckets.append(
            SeriesBucket[MatrixValues](
                start=header.start,
                seconds=header.seconds,
                elapsed_seconds=header.elapsed_seconds,
                observed_seconds=header.observed_seconds,
                utc_offset_minutes=header.utc_offset_minutes,
                basis=header.basis,
                values=MatrixValues(
                    printers={
                        printer.printer_id: _cell(
                            totals.per_printer[printer.printer_id][index],
                            per_printer_prints.get(printer.printer_id, {}).get(index, {}),
                            totals.per_printer_observed[printer.printer_id][index],
                        )
                        for printer in timeline.printers
                    },
                    fleet=_cell(totals.fleet[index], fleet_prints.get(index, {}), header.observed_seconds),
                ),
            )
        )
    window_prints = _fold_counts(fleet_prints.values())
    return MatrixProjection(
        printers=[_matrix_printer(totals, printer, per_printer_prints) for printer in timeline.printers],
        series=SeriesEnvelope[MatrixValues](
            buckets=buckets,
            totals=MatrixValues(
                printers={
                    printer.printer_id: _cell(
                        totals.per_printer_window[printer.printer_id],
                        _fold_counts(per_printer_prints.get(printer.printer_id, {}).values()),
                        totals.per_printer_observed_window[printer.printer_id],
                    )
                    for printer in timeline.printers
                },
                fleet=_cell(totals.fleet_window, window_prints, timeline.total.observed_seconds),
            ),
        ),
    )


def _cell(cell: dict[AvailabilityClass, float], prints: dict[str, int], observed: float) -> MatrixCell:
    return MatrixCell(
        class_seconds=_sparse({klass.key: seconds for klass, seconds in cell.items()}),
        down_seconds=_down_seconds(cell),
        prints=_sparse_counts(prints),
        # This PRINTER's own coverage, not the fleet's: a machine nobody watched in a
        # bucket its neighbours were watched in still has no state measurement.
        basis=BASIS_OBSERVED if observed > 0 else BASIS_INCIDENTS_ONLY,
    )


def _matrix_printer(
    totals: ClassTotals, printer: PrinterTimeline, per_printer_prints: dict[int, dict[int, dict[str, int]]]
) -> MatrixPrinter:
    """One row's identity plus its two per-day figures — ONE COVERAGE PER RATE.

    The two rates deliberately divide by different denominators, because their
    numerators come from different records and mixing them is what produced "48 prints
    a day" for a printer that made twenty in a week:

    * ``prints_per_day`` counts COMPLETED prints, and the print log is complete for the
      whole window, so it divides by the window's elapsed days. The recorder has no say
      in this number at all;
    * ``hours_down_per_day`` counts observed and ledger-known downtime, so it divides
      by the elapsed days of the buckets that could have known anything — days before
      the farm kept any record must not dilute a downtime rate toward zero.

    Null, not zero, where a denominator does not exist: "we cannot say" and "none" are
    different answers.
    """
    cell = totals.per_printer_window[printer.printer_id]
    completed = _fold_counts(per_printer_prints.get(printer.printer_id, {}).values()).get(OUTCOME_COMPLETED, 0)
    down_hours = _down_seconds(cell) / _SECONDS_PER_HOUR
    return MatrixPrinter(
        printer_id=printer.printer_id,
        name=printer.name,
        model=printer.model,
        is_active=printer.is_active,
        deleted=printer.deleted,
        hours_down_per_day=_rate(down_hours, totals.history_elapsed_seconds),
        prints_per_day=_rate(completed, totals.timeline.total.elapsed_seconds),
    )


@dataclass(frozen=True, slots=True)
class PrintTally:
    """Print events folded onto the grid ONCE — read by the matrix and by throughput.

    Both projections count the same rows into the same buckets, so the fold happens
    here and the two read it: two folds would be two chances for a print to land in
    different buckets on the two surfaces that show it.
    """

    #: printer id -> bucket index -> outcome -> count.
    per_printer: dict[int, dict[int, dict[str, int]]]
    #: bucket index -> outcome -> count, fleet-wide (a row with no printer included).
    fleet: dict[int, dict[str, int]]


def print_tally(timeline: FleetTimeline, prints: Sequence[PrintLogRow]) -> PrintTally:
    """Fold print events onto the requested grid, per printer and fleet-wide."""
    grid = timeline.window.grid
    base_starts = [header.start for header in timeline.base_headers]
    per_printer: dict[int, dict[int, dict[str, int]]] = {}
    fleet: dict[int, dict[str, int]] = {}
    for row in prints:
        index = grid.base_to_bucket[bucket_index(base_starts, row.created_at)]
        # A status in none of the three buckets is counted under ``other`` rather than
        # dropped, so the totals here always reconcile with a plain COUNT.
        outcome = outcome_bucket(row.status) or OUTCOME_OTHER
        bucket = fleet.setdefault(index, {})
        bucket[outcome] = bucket.get(outcome, 0) + 1
        if row.printer_id is not None:
            cell = per_printer.setdefault(row.printer_id, {}).setdefault(index, {})
            cell[outcome] = cell.get(outcome, 0) + 1
    return PrintTally(per_printer=per_printer, fleet=fleet)


def _fold_counts(cells: Iterable[dict[str, int]]) -> dict[str, int]:
    total: dict[str, int] = {}
    for cell in cells:
        for key, value in cell.items():
            total[key] = total.get(key, 0) + value
    return total


# ── throughput ──────────────────────────────────────────────────────────────────────


def throughput(totals: ClassTotals, prints: PrintTally) -> SeriesEnvelope[ThroughputValues]:
    """Prints per bucket and per printer, plus the two per-day rates.

    No ``basis``: the print log is complete for its own history, and a recorder gap
    says nothing about whether a print happened. The per-PRINTER rate still goes null
    where the state record cannot supply an in-service denominator — that is the
    denominator being honest, not the print data being doubted.
    """
    timeline = totals.timeline
    per_printer, fleet = prints.per_printer, prints.fleet
    # The per-printer rate's denominator, accumulated as PRINTER-DAYS over the observed
    # buckets only, so the window figure is the sum of the same products the buckets
    # divided by — never an average of per-bucket rates.
    printer_days = 0.0
    completed_observed = 0
    buckets: list[SeriesBucket[ThroughputValues]] = []
    for index, header in enumerate(timeline.headers):
        bucket_counts = fleet.get(index, {})
        bucket_days = _bucket_printer_days(header, totals.printers_in_fleet[index])
        if bucket_days is not None:
            printer_days += bucket_days
            completed_observed += bucket_counts.get(OUTCOME_COMPLETED, 0)
        buckets.append(
            SeriesBucket[ThroughputValues](
                start=header.start,
                seconds=header.seconds,
                elapsed_seconds=header.elapsed_seconds,
                observed_seconds=header.observed_seconds,
                utc_offset_minutes=header.utc_offset_minutes,
                values=_throughput_values(
                    bucket_counts,
                    {printer_id: cells[index] for printer_id, cells in per_printer.items() if index in cells},
                    header.elapsed_seconds,
                    bucket_days,
                    bucket_counts.get(OUTCOME_COMPLETED, 0),
                ),
            )
        )
    return SeriesEnvelope[ThroughputValues](
        buckets=buckets,
        totals=_throughput_values(
            _fold_counts(fleet.values()),
            {printer_id: _fold_counts(cells.values()) for printer_id, cells in per_printer.items()},
            timeline.total.elapsed_seconds,
            printer_days if printer_days > 0 else None,
            completed_observed,
        ),
    )


def _bucket_printer_days(header: BucketHeader, printers_in_fleet: float) -> float | None:
    """Printer-days this bucket can charge a per-printer rate to, or ``None``.

    ``printers_in_fleet`` × the bucket's ELAPSED days: the count of machines is a
    measurement (rule A, inside the mask) while the days are the whole bucket, because
    the numerator — completed prints — is known for the whole bucket too. One coverage
    per ratio, and this ratio's is the bucket.

    ``None`` on a bucket that measured nothing: the print count is still real, but
    there is no measured fleet size to divide it among, and dividing by the roster
    instead would quietly turn an unmeasured period into a per-printer figure.
    """
    if header.observed_seconds <= 0 or header.elapsed_seconds <= 0 or printers_in_fleet <= 0:
        return None
    return printers_in_fleet * header.elapsed_seconds / _SECONDS_PER_DAY


def _throughput_values(
    by_outcome: dict[str, int],
    by_printer: dict[int, dict[str, int]],
    elapsed_seconds: float,
    printer_days: float | None,
    completed_for_rate: int,
) -> ThroughputValues:
    completed = by_outcome.get(OUTCOME_COMPLETED, 0)
    failed = by_outcome.get(OUTCOME_FAILED, 0)
    judged = completed + failed
    return ThroughputValues(
        by_outcome=_sparse_counts(by_outcome),
        total=sum(by_outcome.values()),
        by_printer={printer_id: _sparse_counts(counts) for printer_id, counts in by_printer.items() if counts},
        # The print log is complete for its own history, so this divides by elapsed time
        # and answers for every bucket, observed or not.
        prints_per_day=_rate(completed, elapsed_seconds),
        # ...whereas the per-printer rate needs a measured fleet size, so it answers
        # only where one exists. ``completed_for_rate`` is the count over the same
        # buckets the denominator was accumulated from.
        prints_per_printer_per_day=(
            completed_for_rate / printer_days if printer_days is not None and printer_days > 0 else None
        ),
        # Cancelled prints are outside the ratio on purpose: an operator stopping a
        # print is not the printer failing to deliver one.
        success_pct=completed / judged if judged else None,
    )


# ── units ───────────────────────────────────────────────────────────────────────────


def units(totals: ClassTotals, rows: Sequence[UnitRow]) -> SeriesEnvelope[UnitsValues]:
    """Sellable units per bucket, per SKU — plates × the SKU file's units per plate."""
    timeline = totals.timeline
    grid = timeline.window.grid
    base_starts = [header.start for header in timeline.base_headers]
    per_bucket: list[dict[str, int]] = [{} for _ in timeline.headers]
    plates: list[int] = [0] * len(timeline.headers)
    for row in rows:
        index = grid.base_to_bucket[bucket_index(base_starts, row.completed_at)]
        # ``plate_units`` is THE rule for "missing / absent / below 1 means one unit";
        # a consumer that skipped it would multiply a whole run's arithmetic by a bad
        # stored value.
        yielded = plate_units(row.units_per_plate)
        code = row.sku_code or ""
        if code:
            per_bucket[index][code] = per_bucket[index].get(code, 0) + yielded
        plates[index] += 1
    buckets = [
        SeriesBucket[UnitsValues](
            start=header.start,
            seconds=header.seconds,
            elapsed_seconds=header.elapsed_seconds,
            observed_seconds=header.observed_seconds,
            utc_offset_minutes=header.utc_offset_minutes,
            values=UnitsValues(
                units=sum(per_bucket[index].values()),
                plates=plates[index],
                by_sku=_sparse_counts(per_bucket[index]),
            ),
        )
        for index, header in enumerate(timeline.headers)
    ]
    window = _fold_counts(per_bucket)
    return SeriesEnvelope[UnitsValues](
        buckets=buckets,
        totals=UnitsValues(units=sum(window.values()), plates=sum(plates), by_sku=_sparse_counts(window)),
    )


# ── cycle episodes ──────────────────────────────────────────────────────────────────


def cycle(timeline: FleetTimeline, rows: Sequence[EpisodeRow]) -> CycleProjection:
    """Cooling and eject durations, grouped by printer model × variant AND by printer.

    Both groupings are returned together because the UI's per-printer toggle is a view
    of the same rows: a second request would re-read the same table to answer the same
    question at a different granularity.
    """
    model_by_printer = {printer.printer_id: printer.model for printer in timeline.printers}
    by_model: dict[tuple[str, str | None, str | None], list[EpisodeRow]] = {}
    by_printer: dict[tuple[str, int, str | None, str | None], list[EpisodeRow]] = {}
    for row in rows:
        model = model_by_printer.get(row.printer_id)
        by_model.setdefault((row.kind, model, row.variant), []).append(row)
        by_printer.setdefault((row.kind, row.printer_id, model, row.variant), []).append(row)
    return CycleProjection(
        by_model=[
            CycleGroup(kind=kind, model=model, variant=variant, stats=_cycle_stats(kind, group))
            for (kind, model, variant), group in sorted(by_model.items(), key=_model_sort_key)
        ],
        by_printer=[
            CyclePrinterGroup(
                kind=kind, model=model, variant=variant, printer_id=printer_id, stats=_cycle_stats(kind, group)
            )
            for (kind, printer_id, model, variant), group in sorted(by_printer.items(), key=_printer_sort_key)
        ],
    )


def _model_sort_key(item: tuple[tuple[str, str | None, str | None], list[EpisodeRow]]) -> tuple[str, str, str]:
    kind, model, variant = item[0]
    return kind, model or "", variant or ""


def _printer_sort_key(
    item: tuple[tuple[str, int, str | None, str | None], list[EpisodeRow]],
) -> tuple[str, int, str, str]:
    kind, printer_id, model, variant = item[0]
    return kind, printer_id, model or "", variant or ""


def _cycle_stats(kind: str, rows: list[EpisodeRow]) -> CycleStats:
    """Percentiles over the FINISHED episodes; everything else counted beside them.

    ``median`` and the nearest-rank p90 are the fork's one percentile pair (they are
    ``printer_incidents``'s), so a duration figure reads the same way here and on the
    incident ledger.
    """
    finished = [row for row in rows if row.outcome == _FINISHED_OUTCOME.get(kind)]
    durations = sorted(row.seconds for row in finished)
    # An eject carries the generator's own prediction; a cooldown ends at a temperature
    # and has nothing to be late against, so the share is null there rather than zero.
    predicted = [row for row in finished if row.expected_s is not None] if kind == KIND_EJECT else []
    over = sum(1 for row in predicted if row.seconds > (row.expected_s or 0.0))
    return CycleStats(
        count=len(finished),
        median_s=float(statistics.median(durations)) if durations else None,
        p90_s=nearest_rank_p90(durations) if durations else None,
        not_completed=len(rows) - len(finished),
        over_expected=over if predicted else None,
        over_expected_share=over / len(predicted) if predicted else None,
    )


# ── recovery ────────────────────────────────────────────────────────────────────────


def recovery(
    timeline: FleetTimeline,
    opened: Sequence[PrinterIncident],
    overlapping: Sequence[IncidentRow],
) -> RecoveryProjection:
    """Faults and holds: arrivals per bucket, the outcome ledger, and held hours.

    Two row sets, deliberately. ``opened`` is what ARRIVED in the window — the ledger
    tally and the time-to-recover both belong to the period a fault started in.
    ``overlapping`` is everything that HELD a printer during it, clipped, because an
    outage that began last month still costs this month its hours.

    Identity (g) relates the second figure to the matrix's: held hours are at least the
    ``down:fault`` hours, and more whenever a printer went on printing under a hold.
    """
    grid = timeline.window.grid
    base_starts = [header.start for header in timeline.base_headers]
    per_bucket: list[dict[str, int]] = [{} for _ in timeline.headers]
    for row in opened:
        index = grid.base_to_bucket[bucket_index(base_starts, row.created_at)]
        per_bucket[index][row.kind] = per_bucket[index].get(row.kind, 0) + 1
    buckets = [
        SeriesBucket[RecoveryValues](
            start=header.start,
            seconds=header.seconds,
            elapsed_seconds=header.elapsed_seconds,
            observed_seconds=header.observed_seconds,
            utc_offset_minutes=header.utc_offset_minutes,
            values=RecoveryValues(
                opened=sum(per_bucket[index].values()), opened_by_kind=_sparse_counts(per_bucket[index])
            ),
        )
        for index, header in enumerate(timeline.headers)
    ]
    window = _fold_counts(per_bucket)
    # Faults only: how long a DECLARED maintenance hold stood is not a recovery time,
    # and averaging it in would report the shop's maintenance habits as repair speed.
    stats = held_stats([row for row in opened if row.kind in FAULT_KINDS], timeline.now)
    return RecoveryProjection(
        series=SeriesEnvelope[RecoveryValues](
            buckets=buckets,
            totals=RecoveryValues(opened=sum(window.values()), opened_by_kind=_sparse_counts(window)),
        ),
        summary=IncidentSummary(**incident_summary(list(opened))),
        time_to_recover=[
            TimeToRecover(
                kind=kind,
                count=value.count,
                open_count=value.open_count,
                total_held_s=value.total_held_s,
                median_recover_s=value.median_recover_s,
                p90_recover_s=value.p90_recover_s,
            )
            for kind, value in sorted(stats.items())
        ],
        fault_open_seconds=_clipped_hold_seconds(overlapping, timeline),
    )


def _clipped_hold_seconds(rows: Sequence[IncidentRow], timeline: FleetTimeline) -> float:
    """Incident-held seconds inside the window, over FAULT rows only.

    Clipped to ``[window.start, min(window.end, now))``, which is what makes it
    comparable with the matrix's down hours at all: ``held_seconds`` measures a row's
    whole life, and a month-long outage would otherwise report a month of hours inside
    a one-day window.
    """
    horizon = min(timeline.window.end, timeline.now)
    total = 0.0
    for row in rows:
        if row.kind not in FAULT_KINDS:
            continue
        low = max(row.created_at, timeline.window.start)
        high = min(row.resolved_at if row.resolved_at is not None else timeline.now, horizon)
        if high > low:
            total += (high - low).total_seconds()
    return total


# ── drill-down ──────────────────────────────────────────────────────────────────────


def printer_intervals(
    timeline: FleetTimeline,
    printer: PrinterTimeline,
    incidents: Sequence[IncidentRow],
    *,
    generated_at: datetime,
    tz_name: str,
) -> PrinterIntervalsResponse:
    """One printer's classified intervals and the incidents overlapping its range.

    The drill-down behind a matrix cell: these intervals are the very ones the cell was
    summed from (identity h), so "why was 009 down 4 h" is answered by the same objects
    that produced the 4, never by a re-derivation that could disagree with it.

    ``tz_name`` is passed in rather than read off the window: the zone's display name
    is a fact about now, resolved once by the loader for every response, so this
    drill-down cannot name a different zone from the page it opened out of.
    """
    horizon = min(timeline.window.end, timeline.now)
    return PrinterIntervalsResponse(
        printer=PrinterRef(
            printer_id=printer.printer_id,
            name=printer.name,
            model=printer.model,
            is_active=printer.is_active,
            deleted=printer.deleted,
        ),
        date_from=timeline.window.date_from,
        date_to=timeline.window.date_to,
        tz_name=tz_name,
        generated_at=generated_at,
        intervals=[
            ClassifiedInterval(
                start=interval.start,
                end=interval.end,
                class_key=interval.klass.key,
                group=interval.klass.group,
                cause=interval.klass.cause,
                seconds=interval.seconds,
            )
            for interval in printer.intervals
        ],
        incidents=[
            IncidentInterval(incident_id=row.id, kind=row.kind, created_at=row.created_at, resolved_at=row.resolved_at)
            for row in incidents
            if row.printer_id == printer.printer_id
            and row.created_at < horizon
            and (row.resolved_at is None or row.resolved_at > timeline.window.start)
        ],
    )


# ── summary ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SummaryInputs:
    """One window's finished series — the summary derives nothing of its own.

    It reads the series the page already shows, so a headline and the chart under it
    can never be two different numbers: the row IS the series' totals, and the
    sparkline IS its buckets.
    """

    fleet: SeriesEnvelope[FleetSeriesValues]
    prints: SeriesEnvelope[ThroughputValues]
    #: The window's own end, and where the PRINT log's history begins. Both default to
    #: None so a caller that asks for no comparison need not supply them — and the
    #: conservative direction is the safe one: with either missing, a print row is not
    #: compared rather than compared against nothing.
    window_end: datetime | None = None
    prints_since: datetime | None = None

    @property
    def observed(self) -> bool:
        """Did the state recorder cover ANY of this window?"""
        return any(bucket.basis == BASIS_OBSERVED for bucket in self.fleet.buckets)

    @property
    def prints_comparable(self) -> bool:
        """Does this window overlap the print log's own history at all?

        A comparison needs evidence on BOTH sides. The print log is complete from its
        first row onward, so a window that reaches that instant compares honestly —
        zero prints included, because there a zero is a measurement. A window lying
        entirely before it has no print data to be zero, and reporting 0 made the page
        render "vs previous +129" against a period in which the farm had no record at
        all. Withholding is the only honest answer, and it is the same answer the state
        rows already give for their own record.

        The bound is the print log's start and NOT the state recorder's: the two
        records begin at different instants, and a print row compared on the recorder's
        history would be withheld for weeks of perfectly good print data.
        """
        return self.prints_since is not None and self.window_end is not None and self.window_end > self.prints_since


def compose_summary(current: SummaryInputs, previous: SummaryInputs | None) -> SummaryProjection:
    """The headline rows: this window's figure, the previous window's, and the shape.

    Every row is an average, a rate or a max — never a raw total — so the period column
    is in the same unit as the live count beside it ("2 down now" against "2.1 down on
    average"). A STATE row answers null for a window nothing observed, and its
    sparkline points answer null on the individual buckets nothing observed, so a
    trend line never joins a measurement to a ledger-only estimate. A PRINT row is
    never withheld that way — the print log is complete for its own history.
    """
    rows: list[SummaryRow] = []
    for key, source, read in _ROWS:
        if source == _FROM_STATE:
            state_read: Callable[[FleetSeriesValues], float | None] = read  # type: ignore[assignment]
            rows.append(
                SummaryRow(
                    key=key,
                    figure=state_read(current.fleet.totals) if current.observed else None,
                    previous=(
                        state_read(previous.fleet.totals) if previous is not None and previous.observed else None
                    ),
                    series=[
                        state_read(bucket.values) if bucket.basis == BASIS_OBSERVED else None
                        for bucket in current.fleet.buckets
                    ],
                )
            )
            continue
        print_read: Callable[[ThroughputValues], float | None] = read  # type: ignore[assignment]
        rows.append(
            SummaryRow(
                key=key,
                figure=print_read(current.prints.totals),
                # ...but a print row IS withheld when the previous window predates the
                # print log entirely: see ``SummaryInputs.prints_comparable``.
                previous=(
                    print_read(previous.prints.totals) if previous is not None and previous.prints_comparable else None
                ),
                series=[print_read(bucket.values) for bucket in current.prints.buckets],
            )
        )
    return SummaryProjection(rows=rows)


def _group_average(group: str) -> Callable[[FleetSeriesValues], float | None]:
    """A reader for one group's average — the state rows differ only in which group."""

    def read(values: FleetSeriesValues) -> float | None:
        return values.avg_by_group.get(group, 0.0)

    return read


_FROM_STATE: Literal["state"] = "state"
_FROM_PRINTS: Literal["prints"] = "prints"

# The card, top to bottom. An ordered tuple and not two dicts: the reading order is
# part of the design (the five state rows in one unit, then the two rates, then the
# two ratios), and a dict per source would interleave them by accident.
_ROWS: tuple[tuple[str, str, Callable], ...] = (
    (ROW_AVG_PRINTING, _FROM_STATE, _group_average(GROUP_PRINTING)),
    (ROW_AVG_CYCLE_OVERHEAD, _FROM_STATE, _group_average(GROUP_CYCLE_OVERHEAD)),
    (ROW_AVG_IDLE, _FROM_STATE, _group_average(GROUP_IDLE)),
    (ROW_AVG_DOWN, _FROM_STATE, lambda values: values.avg_down),
    (ROW_AVG_PLANNED, _FROM_STATE, _group_average(GROUP_PLANNED)),
    (ROW_PEAK_DOWN, _FROM_STATE, lambda values: float(values.peak_down)),
    (ROW_PRINTERS_IN_FLEET, _FROM_STATE, lambda values: values.printers_in_fleet),
    (ROW_PRINTS_PER_DAY, _FROM_PRINTS, lambda values: values.prints_per_day),
    (ROW_PRINTS_PER_PRINTER_PER_DAY, _FROM_PRINTS, lambda values: values.prints_per_printer_per_day),
    (ROW_UPTIME, _FROM_STATE, lambda values: values.uptime),
    (ROW_TIME_PRINTING, _FROM_STATE, lambda values: values.time_printing),
)
