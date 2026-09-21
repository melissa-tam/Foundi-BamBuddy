"""Wire shapes for the fleet-metrics read API.

One envelope carries every time series (:class:`SeriesEnvelope`), so the client has
ONE adapter from a response to a chart instead of one per widget, and the bucket
header a reader needs in order to trust a figure — how wide the bucket is, how much
of it has ELAPSED, how much of it was actually observed — travels with the numbers
rather than being reconstructed beside them.

**Every figure arrives finished.** These models carry averages, rates and shares, not
raw totals a client would have to divide: the denominator rules (divide by elapsed,
never by width and never by observed) are arithmetic the backend owns, and a second
division on the client is a second definition of *uptime*.

**Sparse maps mean zero, never unknown.** A class or an outcome with no seconds is
omitted from its map. Absence of DATA is carried by ``observed_seconds`` and
``basis`` instead, which is the distinction the matrix renders as four different
cells — a dim zero, a dash before recording, a hatch for a partly observed bucket and
a flat ground for a printer out of the fleet.

``basis`` rides STATE-derived series only (the matrix and the fleet series). Print,
incident and episode data is complete for its own history, so marking it partial
because the state recorder had a gap would be a lie in the safe direction.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Generic, TypeVar

from pydantic import BaseModel

from backend.app.utils.site_time import Bucket

ValuesT = TypeVar("ValuesT")


class SeriesBucket(BaseModel, Generic[ValuesT]):
    """One cell of a series: its header, then whatever that series measures."""

    # Naive UTC, the fork's convention. ``utc_offset_minutes`` is the site's offset AT
    # this instant, so a client renders the site-local label with no tz database and a
    # transition day still labels correctly on both sides of the change.
    start: datetime
    seconds: float
    # The width clamped to "now": 0 for a bucket entirely in the future, and the whole
    # width for a closed one. EVERY average and rate in ``values`` divides by this.
    elapsed_seconds: float
    # How much of the bucket the state recorder covered (a union over printers, so it
    # never exceeds the width). Less than ``elapsed_seconds`` is what the UI hatches.
    observed_seconds: float
    utc_offset_minutes: int
    # ``observed`` | ``incidents_only``. Null (omitted) on a series whose source is not
    # the state recorder — see the module docstring.
    basis: str | None = None
    values: ValuesT


class SeriesEnvelope(BaseModel, Generic[ValuesT]):
    """A whole series: its buckets, and the same shape re-aggregated over the window.

    ``totals`` is computed by summing SECONDS across the buckets and dividing once —
    never by averaging the buckets' own averages, which would weight a 23 h transition
    day exactly like a 25 h one.
    """

    buckets: list[SeriesBucket[ValuesT]]
    totals: ValuesT


class PrinterRef(BaseModel):
    """Who a row is about. ``name`` falls back to the printer's id once it is deleted."""

    printer_id: int
    name: str
    model: str | None
    is_active: bool
    # The printer row is gone; the history about it is not. The UI renders its own
    # label for this case rather than being handed display copy from a service.
    deleted: bool


# ── fleet series ────────────────────────────────────────────────────────────────────


class FleetSeriesValues(BaseModel):
    """The fleet as AVERAGE CONCURRENT PRINTERS — the same unit as the live counts.

    Stating an average in printers rather than in hours is what lets "2 down now" sit
    beside "2.1 down on average" in one column: the two are comparable numbers of the
    same thing.
    """

    printers_known: int
    # printers_known − out of fleet − not recorded, averaged over the bucket.
    printers_in_fleet: float
    # class GROUP -> average concurrent printers. Sparse. Sums to ``printers_known``
    # for any bucket with elapsed time.
    avg_by_group: dict[str, float]
    # down CAUSE -> average concurrent printers. Sparse. Sums to ``avg_down``.
    avg_down_by_cause: dict[str, float]
    avg_down: float
    # The most printers down AT ONCE inside the bucket, from a joint sweep across
    # printers — a different question from the average, and the one that says whether
    # a quiet average hides a bad hour.
    peak_down: int
    # Null when nothing was scheduled, and on an ``incidents_only`` bucket: a ratio
    # whose denominator nobody measured is not a low number, it is no number.
    uptime: float | None
    time_printing: float | None


# ── matrix ──────────────────────────────────────────────────────────────────────────


class MatrixCell(BaseModel):
    """One printer's (or the fleet's) bucket: where its time went, and what it made."""

    # class KEY -> seconds. Sparse.
    class_seconds: dict[str, float]
    down_seconds: float
    # outcome bucket -> print count. Sparse.
    prints: dict[str, int]
    basis: str


class MatrixValues(BaseModel):
    """Every printer's cell for one bucket, plus the fleet row under them."""

    printers: dict[int, MatrixCell]
    fleet: MatrixCell


class MatrixPrinter(PrinterRef):
    """A matrix row's identity and the two per-day figures shown beside the name."""

    # Null before this printer had any time in service — a rate with no denominator.
    hours_down_per_day: float | None
    prints_per_day: float | None


class MatrixProjection(BaseModel):
    """The matrix: a roster dimension, and the series indexed by it.

    The roster is lifted out of the buckets deliberately — a name and a model repeated
    in every one of 366 cells is the same fact 366 times, and the matrix is the one
    projection whose values are indexed by something other than time.
    """

    printers: list[MatrixPrinter]
    series: SeriesEnvelope[MatrixValues]


# ── throughput and units ────────────────────────────────────────────────────────────


class ThroughputValues(BaseModel):
    """Prints, from the print log — complete for its own history, so no ``basis``."""

    # outcome bucket -> count. Sparse. ``other`` holds rows whose status is in no
    # bucket, so this map always reconciles with a plain COUNT of the same window.
    by_outcome: dict[str, int]
    total: int
    # printer id -> outcome bucket -> count. Sparse both levels.
    by_printer: dict[int, dict[str, int]]
    # COMPLETED prints per day — the same numerator as the per-printer rate below, so
    # the two summary rows are consistent with each other.
    prints_per_day: float | None
    # Completed ÷ printer-days IN SERVICE (elapsed less out-of-fleet and not-recorded
    # time). Null where that denominator is zero — before recording, there is no
    # honest per-printer rate.
    prints_per_printer_per_day: float | None
    # completed ÷ (completed + failed). Cancelled prints are excluded: somebody
    # stopping a print is not the printer failing.
    success_pct: float | None


class UnitsValues(BaseModel):
    """Sellable units, from completed queue plates × the SKU file's units per plate."""

    units: int
    plates: int
    # SKU code -> units. Sparse.
    by_sku: dict[str, int]


# ── cycle episodes ──────────────────────────────────────────────────────────────────


class CycleStats(BaseModel):
    """One group's measured durations. Percentiles describe the FINISHED episodes."""

    count: int
    median_s: float | None
    p90_s: float | None
    # Episodes of this group that did not reach their kind's finished outcome (a
    # cooldown an operator cut short, a sweep the watchdog stopped). Counted, never
    # mixed into the percentiles: they are not durations of the thing being measured.
    not_completed: int
    # Ejects only — a cooldown ends at a temperature and has nothing to be late
    # against. Null for a cooldown group, and null for ejects with no prediction.
    over_expected: int | None
    over_expected_share: float | None


class CycleGroup(BaseModel):
    """Durations for one ``kind`` × printer model × variant."""

    kind: str
    model: str | None
    variant: str | None
    stats: CycleStats


class CyclePrinterGroup(CycleGroup):
    """The same, for one printer — the toggle's data, returned with the group view."""

    printer_id: int


class CycleProjection(BaseModel):
    """Cooling and eject durations, grouped both ways so the UI toggle needs no request."""

    by_model: list[CycleGroup]
    by_printer: list[CyclePrinterGroup]


# ── recovery ────────────────────────────────────────────────────────────────────────


class RecoveryValues(BaseModel):
    """Incidents OPENED in the bucket — the arrival rate, not the standing count."""

    opened: int
    # incident kind -> count. Sparse.
    opened_by_kind: dict[str, int]


class IncidentSummary(BaseModel):
    """``printer_incidents.summary`` as a model — the equipment-fault ledger's tally."""

    total: int
    zero_human: int
    declared: int
    by_outcome: dict[str, int]
    by_kind: dict[str, dict[str, int]]


class TimeToRecover(BaseModel):
    """How long one kind's holds stood, over the FAULT rows opened in the window."""

    kind: str
    count: int
    open_count: int
    total_held_s: float
    median_recover_s: float | None
    p90_recover_s: float | None


class RecoveryProjection(BaseModel):
    """Faults and holds: when they arrived, how long they took, how many held hours."""

    series: SeriesEnvelope[RecoveryValues]
    summary: IncidentSummary
    time_to_recover: list[TimeToRecover]
    # Incident-held seconds CLIPPED to the window. Deliberately a different figure
    # from hours DOWN (which counts observed printer condition): an incident that held
    # a printer while it went on printing contributes here and not there.
    fault_open_seconds: float


# ── summary ─────────────────────────────────────────────────────────────────────────


class SummaryRow(BaseModel):
    """One headline: this window's figure, the previous window's, and the sparkline.

    Every row is an average, a rate or a max — never a raw total — so the period column
    is comparable with the live column beside it and with the previous period.
    """

    key: str
    # Null when the window has no observed bucket and the row is state-derived.
    figure: float | None
    previous: float | None
    # One value per bucket of the SAME grid the other series use; null where the row
    # has no answer for that bucket.
    series: list[float | None]


class SummaryProjection(BaseModel):
    rows: list[SummaryRow]


# ── drill-down and live status ──────────────────────────────────────────────────────


class ClassifiedInterval(BaseModel):
    """One stretch of one printer's window, as the drill-down lists it."""

    start: datetime
    end: datetime
    class_key: str
    group: str
    cause: str | None
    seconds: float


class IncidentInterval(BaseModel):
    """An incident overlapping the drill-down's range, for the same list."""

    incident_id: int
    kind: str
    created_at: datetime
    resolved_at: datetime | None


class PrinterIntervalsResponse(BaseModel):
    """One printer's classified intervals — what a matrix cell was summed from."""

    printer: PrinterRef
    date_from: date
    date_to: date
    tz_name: str
    generated_at: datetime
    intervals: list[ClassifiedInterval]
    incidents: list[IncidentInterval]


class PrinterStatus(PrinterRef):
    """One printer RIGHT NOW, through the same classifier the history is read with."""

    class_key: str
    group: str
    cause: str | None
    # When the current class began: an incident's own ``created_at`` for a fault or a
    # hold (exact at any age), otherwise the start of the current run of adjacent
    # spans reading the same way.
    since: datetime | None
    # The walk back hit its bound before finding the run's start, so ``since`` is the
    # earliest instant proven, not the first.
    since_open_ended: bool


class FleetStatus(BaseModel):
    """The live tile: every printer's class now, and what "all time" resolves to."""

    generated_at: datetime
    site_today: date
    tz_name: str
    # The first observation ever recorded. State history cannot predate it.
    recording_since: datetime | None
    # The earlier of that and the first incident — fault and hold history reaches
    # further back than the recorder does, and this is what an "all time" range means.
    history_since: datetime | None
    printers: list[PrinterStatus]
    counts_by_group: dict[str, int]
    counts_by_class: dict[str, int]


class FleetOverview(BaseModel):
    """ONE history response: every projection over one window, from one timeline."""

    date_from: date
    date_to: date
    # Echoed, because the client omits it and the server chooses from the window
    # length — and the column layout follows what the server actually used.
    bucket: Bucket
    tz_name: str
    generated_at: datetime
    window_start: datetime
    window_end: datetime
    summary: SummaryProjection
    matrix: MatrixProjection
    fleet_series: SeriesEnvelope[FleetSeriesValues]
    throughput: SeriesEnvelope[ThroughputValues]
    units: SeriesEnvelope[UnitsValues]
    cycle: CycleProjection
    recovery: RecoveryProjection
