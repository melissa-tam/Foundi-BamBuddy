"""The ONLY module here that reads a database or a live accessor.

Everything else in the package is pure: the classifier, the timeline sweep and every
projection take rows and return values. This module is where rows come from — one
window, one set of Core column selects, then the whole fold handed to a worker thread
so a year-long sweep never blocks the event loop while the farm is dispatching.

**A window read costs the window, not the history.** The obvious overlap predicate —
``started_at < :end AND (ended_at IS NULL OR ended_at > :start)`` — cannot be served
by any index, because the disjunction has no lower bound to seek on; it reads every
row in the table however narrow the window is. So the read is in two parts instead.
Spans that START inside the window come off ``(started_at)`` as a range scan. The
spans already running when the window opened are found one at a time: the log is
run-length compressed, so a printer's spans are contiguous and non-overlapping and at
most ONE of them can straddle the opening instant — the last one starting before it,
a seek into ``(printer_id, started_at)``. Incidents come through the store's own
``list_overlapping`` (a window reader must see the hold that OPENED before the window
and was still standing through it — the longest outages, which a ``created_at >=
since`` filter is blind to); print events off ``print_log_entries.created_at``;
episodes off ``(kind, ended_at)``, with ``kind`` constrained so the index's leading
column is usable; completed plates off ``print_queue.completed_at``.

**Whole-table facts are answered by seeks, not by aggregates over the table.** When a
printer's record began and when it was last evidenced to exist are facts about all of
history, not about the window — a window must not be able to change what *before
recording* means — but a GROUP BY over the span table reads every row to produce one
line per printer, and that cost grows for ever while the answer stays the same size.
Both come from per-printer index seeks, and the set of printers to ask is itself
walked out of the index by hops (``min(printer_id) WHERE printer_id > :last``) rather
than by a DISTINCT that would scan. That set deliberately includes printers the
roster no longer has: a machine that sat offline for weeks and was then deleted still
owns that downtime, and a reader that only asked the roster would report a fleet that
was never down.

**Read-only, and deliberately so.** Nothing here inserts, updates or deletes. The
recorder and the episode writer own those tables; classification happens at read time
precisely so the definition of *down* can change without a migration.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any, TypeVar

from sqlalchemy import func, select

from backend.app.models.farm_cycle_episode import EPISODE_KINDS, FarmCycleEpisode
from backend.app.models.print_batch import PrintBatch
from backend.app.models.print_log import PrintLogEntry
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_incident import DECLARED_KINDS, KIND_PRECEDENCE, PrinterIncident
from backend.app.models.printer_observation_span import PrinterObservationSpan
from backend.app.models.sku import Sku, SkuFile
from backend.app.schemas.fleet_metrics import (
    FleetOverview,
    FleetStatus,
    PrinterIntervalsResponse,
    PrinterStatus,
)
from backend.app.services import printer_incidents
from backend.app.services.fleet_activity import fleet_activity_recorder, gather_observation
from backend.app.services.fleet_metrics import projections
from backend.app.services.fleet_metrics.classifier import (
    GROUP_DOWN,
    GROUP_IDLE,
    GROUP_PLANNED,
    OBSERVATION_GAP,
    AvailabilityClass,
    Seen,
    availability_class,
    fault_kind_of,
)
from backend.app.services.fleet_metrics.timeline import (
    FleetTimeline,
    IncidentRow,
    PrinterEvidence,
    RosterPrinter,
    SpanRow,
    Window,
    build_timeline,
    build_window,
    printer_slice,
    span_end,
)
from backend.app.utils.site_time import Bucket, default_bucket, previous_window, site_today, site_zone_name

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import tzinfo

    from sqlalchemy import Row, Select
    from sqlalchemy.ext.asyncio import AsyncSession

# The window bounds the API publishes. They are limits on the SWEEP, not on the data:
# history is kept forever, and a longer range is a second request.
MAX_WINDOW_DAYS = 366
MAX_HOUR_WINDOW_DAYS = 3
MAX_INTERVAL_DAYS = 7

# How far back the live tile walks to find when the current class began. A bound and
# not a full scan: this runs on a polled endpoint, and a printer that has been idle
# through thousands of spans is answered "at least this long" rather than costing the
# poll a table scan. Six hundred spans is days of an unsettled printer.
_SINCE_SPAN_LIMIT = 600

# The queue vocabulary's word for a plate that was delivered. ``print_log`` owns the
# same word for the print LOG's status column, and the two are different vocabularies
# over different tables — importing that constant here would tie a queue filter to the
# outcome taxonomy of another table.
_QUEUE_STATUS_COMPLETED = "completed"

# The store's own precedence, as a rank lookup — the same order the printer card's
# chip reads, so "which hold decided this class" has one answer on both surfaces.
_RANK: dict[str, int] = {kind: rank for rank, kind in enumerate(KIND_PRECEDENCE)}

#: What ``_stream`` builds — the row type is the caller's, the partitioning is not.
_RowT = TypeVar("_RowT")


class FleetMetricsError(ValueError):
    """A request the metrics reader refuses. The route maps it to 422."""


class InvalidWindow(FleetMetricsError):
    """The window ends before it starts."""


class WindowTooLong(FleetMetricsError):
    """The window, or the window at that bucket, exceeds what one request may sweep."""


class PrinterUnknown(FleetMetricsError):
    """No printer, span or incident in the window names this id."""


async def overview(
    db: AsyncSession,
    *,
    date_from: date,
    date_to: date,
    bucket: Bucket | None = None,
    now: datetime | None = None,
    tz: tzinfo | None = None,
) -> FleetOverview:
    """Every projection over one window, from ONE timeline.

    ``bucket`` is chosen from the window length when the caller omits it and echoed on
    the response, because the column layout has to follow the grid the server actually
    used rather than the one the client guessed.

    The previous window (same NUMBER of site days, ending the day before) is built as
    a second timeline so the summary's comparison comes from the same formulas as its
    figure — never from a stored rollup or a scaled estimate.
    """
    resolved_bucket = _resolve_bucket(date_from, date_to, bucket)
    window = build_window(date_from, date_to, resolved_bucket, tz)
    moment = now if now is not None else utcnow()

    roster = await _load_roster(db)
    # From the SPAN table, not the roster: a printer that was down for weeks and then
    # deleted still owns that downtime, and asking only the roster would drop it.
    span_printers = await _span_printer_ids(db)
    evidence = await _load_evidence(db, printer_ids=span_printers)
    since = await _history_since(db)
    facts = await _load_window(
        db, window, roster=roster, evidence=evidence, printer_ids=span_printers, now=moment, history_since=since
    )
    previous_from, previous_to = previous_window(date_from, date_to)
    previous = await _load_window(
        db,
        build_window(previous_from, previous_to, resolved_bucket, tz),
        roster=roster,
        evidence=evidence,
        printer_ids=span_printers,
        now=moment,
        history_since=since,
        include_output=False,
    )

    return await asyncio.to_thread(_compose_overview, facts, previous, moment, zone_name(moment, tz))


async def printer_intervals(
    db: AsyncSession,
    printer_id: int,
    *,
    date_from: date,
    date_to: date,
    bucket: Bucket | None = None,
    now: datetime | None = None,
    tz: tzinfo | None = None,
) -> PrinterIntervalsResponse:
    """One printer's classified intervals — what a matrix cell was summed from.

    The same loader and the same sweep, restricted to one printer, so the drill-down
    cannot disagree with the cell it opened from. The range is short by design: this is
    a list a human reads, not a series.
    """
    days = _window_days(date_from, date_to)
    if days > MAX_INTERVAL_DAYS:
        raise WindowTooLong(f"interval listings cover at most {MAX_INTERVAL_DAYS} days; asked for {days}")
    window = build_window(date_from, date_to, bucket or default_bucket(days), tz)
    moment = now if now is not None else utcnow()

    facts = await _load_window(
        db,
        window,
        roster=await _load_roster(db, printer_id=printer_id),
        evidence=await _load_evidence(db, printer_ids=[printer_id], printer_id=printer_id),
        printer_ids=[printer_id],
        now=moment,
        # The drill-down lists intervals and computes no rate, so nothing reads it
        # here; it is passed for one timeline shape rather than two.
        history_since=None,
        printer_id=printer_id,
        include_output=False,
    )
    timeline = await asyncio.to_thread(_build, facts, moment)
    entry = printer_slice(timeline, printer_id)
    if entry is None:
        raise PrinterUnknown(f"no printer, span or incident in this window names printer {printer_id}")
    return projections.printer_intervals(
        timeline, entry, facts.incident_rows, generated_at=moment, tz_name=zone_name(moment, tz)
    )


async def status_now(db: AsyncSession, *, now: datetime | None = None, tz: tzinfo | None = None) -> FleetStatus:
    """Every printer's class RIGHT NOW, through the same classifier as the history.

    The live reading comes from ``fleet_activity.gather_observation`` — the recorder's
    own reader, with the recorder's own startup grace — so a printer still dialling one
    minute after a restart reads as not-yet-known on the tile exactly as it does in the
    log, instead of being offline on one surface and absent on the other. A reading it
    declines is an observation GAP, never an invented "offline".
    """
    moment = now if now is not None else utcnow()
    roster = await _load_roster(db)
    uptime = fleet_activity_recorder.uptime_s()

    printers: list[PrinterStatus] = []
    by_group: dict[str, int] = {}
    by_class: dict[str, int] = {}
    for entry in roster:
        seen: Seen = gather_observation(entry.printer_id, entry.is_active, uptime_s=uptime) or OBSERVATION_GAP
        open_kinds = printer_incidents.open_kinds(entry.printer_id)
        klass = availability_class(seen, open_kinds)
        since, open_ended = await _since(db, entry.printer_id, klass, open_kinds, moment)
        printers.append(
            PrinterStatus(
                printer_id=entry.printer_id,
                name=entry.name,
                model=entry.model,
                is_active=entry.is_active,
                deleted=False,
                class_key=klass.key,
                group=klass.group,
                cause=klass.cause,
                since=since,
                since_open_ended=open_ended,
            )
        )
        by_group[klass.group] = by_group.get(klass.group, 0) + 1
        by_class[klass.key] = by_class.get(klass.key, 0) + 1

    recording_since = await db.scalar(select(func.min(PrinterObservationSpan.started_at)))
    return FleetStatus(
        generated_at=moment,
        site_today=site_today(moment, tz),
        tz_name=zone_name(moment, tz),
        recording_since=recording_since,
        history_since=await _history_since(db, recording_since=recording_since),
        printers=printers,
        counts_by_group=by_group,
        counts_by_class=by_class,
    )


async def _history_since(db: AsyncSession, *, recording_since: datetime | None = None) -> datetime | None:
    """The earliest instant the farm has ANY evidence for. ``None`` before either exists.

    THE one origin of that instant, because two consumers would otherwise define it
    twice: it is what "all time" resolves to on the live tile, and it is what tells a
    downtime rate which buckets could have been known about. The ledger reaches
    further back than the recorder — fault history predates the first span by weeks on
    this farm — so a reader that stopped at the first span would silently drop every
    fault the farm has a durable record of.

    ``recording_since`` is passed in by a caller that already has it, so the live tile
    costs one scalar rather than two.
    """
    first_span = (
        recording_since
        if recording_since is not None
        else await db.scalar(select(func.min(PrinterObservationSpan.started_at)))
    )
    first_incident = await db.scalar(select(func.min(PrinterIncident.created_at)))
    return min([value for value in (first_span, first_incident) if value is not None], default=None)


def utcnow() -> datetime:
    """The reader's clock: naive UTC at whole seconds, the tables' own convention."""
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def zone_name(moment: datetime, tz: tzinfo | None = None) -> str:
    """The site zone's display name AT ``moment`` — THE resolver for every response.

    A name, not an offset, and therefore a fact about the clock rather than about the
    range being asked about. It has to be resolved from the REQUEST's instant and not
    from a window's first bucket: on a host with no ``TZ`` the OS zone renders
    "…Standard Time" for half the year, so a window opening in January would label
    itself with January's name while the live tile beside it and every other preset on
    the page named the current one. One function, one instant, three responses.
    """
    return site_zone_name(moment, tz)


# ── reading machinery ───────────────────────────────────────────────────────────────

# Rows per streamed partition. Chosen by measurement rather than by feel: reading a
# year of spans in one buffered fetch holds the event loop for ~1.9 s, which is a farm
# whose printer list stops answering while somebody looks at a chart. At 500 the worst
# observed gap is ~16 ms and the read is FASTER in wall-clock terms as well, because
# nothing has to materialise a single list of half a million rows.
_STREAM_PARTITION = 500

# The span columns, in the order ``_span_row`` reads them. One tuple, because three
# call sites select exactly this shape and a column added to one of them and not the
# others would be a silent mis-read rather than an error.
_SPAN_COLUMNS = (
    PrinterObservationSpan.printer_id,
    PrinterObservationSpan.started_at,
    PrinterObservationSpan.last_observed_at,
    PrinterObservationSpan.ended_at,
    PrinterObservationSpan.is_active,
    PrinterObservationSpan.connected,
    PrinterObservationSpan.gcode_state,
    PrinterObservationSpan.plate_phase,
    PrinterObservationSpan.quarantined,
    PrinterObservationSpan.usb_present,
    PrinterObservationSpan.model_mismatch,
)


def _span_row(row: Row[Any]) -> SpanRow:
    """One selected row as the sweep's own value object."""
    return SpanRow(
        printer_id=row[0],
        started_at=row[1],
        last_observed_at=row[2],
        ended_at=row[3],
        is_active=bool(row[4]),
        connected=bool(row[5]),
        gcode_state=row[6],
        plate_phase=row[7],
        quarantined=bool(row[8]),
        usb_present=None if row[9] is None else bool(row[9]),
        model_mismatch=bool(row[10]),
    )


async def _stream(db: AsyncSession, stmt: Select[Any], build: Callable[[Row[Any]], _RowT]) -> list[_RowT]:
    """Run ``stmt`` in bounded partitions, building each row as it arrives.

    Every partition boundary is an ``await``, so a long read hands the event loop back
    hundreds of times instead of once at the end. This is the difference between a
    concurrent request waiting a few milliseconds and waiting for the whole query: the
    pure fold already runs in a worker thread, but fetching and materialising rows is
    the other half of the work and it happens here, on the loop.
    """
    rows: list[_RowT] = []
    result = await db.stream(stmt)
    async for partition in result.partitions(_STREAM_PARTITION):
        rows.extend(build(row) for row in partition)
    return rows


# ── window loading ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _WindowFacts:
    """One window's rows, kept together so the pure side takes a single argument.

    ``incidents`` are ORM rows, because the incident store's own ``summary`` and
    ``held_stats`` are the one derivation of those figures and they take rows. Every
    attribute the pure side reads is loaded by the select that fetched them and the
    loader commits nothing in between, so the worker thread only ever touches resident
    state — a lazy refresh from off the event loop would be a MissingGreenlet.
    """

    window: Window
    roster: list[RosterPrinter]
    spans: list[SpanRow]
    incidents: list[PrinterIncident]
    incident_rows: list[IncidentRow]
    evidence: list[PrinterEvidence]
    prints: list[projections.PrintLogRow]
    units: list[projections.UnitRow]
    episodes: list[projections.EpisodeRow]
    #: Whole-table, window-independent: the earliest evidence of any kind. A rate that
    #: divides by "days the farm could have known about" reads it.
    history_since: datetime | None


async def _load_window(
    db: AsyncSession,
    window: Window,
    *,
    roster: list[RosterPrinter],
    evidence: list[PrinterEvidence],
    printer_ids: Sequence[int],
    now: datetime,
    history_since: datetime | None,
    printer_id: int | None = None,
    include_output: bool = True,
) -> _WindowFacts:
    """Every row one window needs. Read-only; the session is left exactly as found.

    ``roster``, ``evidence`` and ``printer_ids`` are passed in because they are facts
    about the whole table, identical for a window and its predecessor — resolving them
    twice would be the same seeks twice per request. ``include_output`` is off for the
    comparison window: the summary reads only its state and print series, so a year of
    units and episodes nobody will look at is a query not worth making.
    """
    incidents = await printer_incidents.list_overlapping(db, start=window.start, end=window.end)
    if printer_id is not None:
        incidents = [row for row in incidents if row.printer_id == printer_id]
    return _WindowFacts(
        window=window,
        history_since=history_since,
        roster=roster,
        spans=await _load_spans(db, window, printer_ids=printer_ids, now=now, printer_id=printer_id),
        incidents=incidents,
        incident_rows=[
            IncidentRow(
                id=row.id,
                printer_id=row.printer_id,
                kind=row.kind,
                created_at=row.created_at,
                resolved_at=row.resolved_at,
            )
            for row in incidents
        ],
        evidence=evidence,
        prints=await _load_prints(db, window),
        units=await _load_units(db, window) if include_output else [],
        episodes=await _load_episodes(db, window) if include_output else [],
    )


async def _load_roster(db: AsyncSession, *, printer_id: int | None = None) -> list[RosterPrinter]:
    stmt = select(Printer.id, Printer.name, Printer.model, Printer.is_active).order_by(Printer.id)
    if printer_id is not None:
        stmt = stmt.where(Printer.id == printer_id)
    return [
        RosterPrinter(printer_id=row[0], name=row[1], model=row[2], is_active=bool(row[3]))
        for row in (await db.execute(stmt)).all()
    ]


async def _span_printer_ids(db: AsyncSession) -> list[int]:
    """Every printer id the span table holds, walked out of the index by hops.

    ``SELECT min(printer_id) WHERE printer_id > :last``, repeated until it answers
    nothing: each call is a seek into ``(printer_id, started_at)``, so the whole set
    costs O(printers · log n) where ``SELECT DISTINCT`` would read every entry in the
    table. Portable — the min/max-with-a-lower-bound form is an index seek on SQLite
    and on PostgreSQL alike, unlike a ``DISTINCT ON`` or a recursive CTE.
    """
    ids: list[int] = []
    last: int | None = None
    while True:
        stmt = select(func.min(PrinterObservationSpan.printer_id))
        if last is not None:
            stmt = stmt.where(PrinterObservationSpan.printer_id > last)
        found = await db.scalar(stmt)
        if found is None:
            return ids
        ids.append(int(found))
        last = int(found)


async def _straddler(db: AsyncSession, printer_id: int, start: datetime, now: datetime) -> SpanRow | None:
    """The one span that can reach into the window from before it, for one printer.

    Spans are run-length compressed and therefore contiguous per printer, so at most
    one can start before ``start`` and still be covering the printer when the window
    opens: the LAST one starting before it. Whether it actually reaches is the
    timeline's own coverage rule (``span_end``) rather than a second predicate written
    in SQL — an open span's freshness has one definition, and a straddler admitted by
    one rule and then read by another would put a hole where the record has none.
    """
    stmt = (
        select(*_SPAN_COLUMNS)
        .where(PrinterObservationSpan.printer_id == printer_id)
        .where(PrinterObservationSpan.started_at < start)
        .order_by(PrinterObservationSpan.started_at.desc())
        .limit(1)
    )
    row = (await db.execute(stmt)).first()
    if row is None:
        return None
    span = _span_row(row)
    return span if span_end(span, now) > start else None


async def _load_spans(
    db: AsyncSession,
    window: Window,
    *,
    printer_ids: Sequence[int],
    now: datetime,
    printer_id: int | None = None,
) -> list[SpanRow]:
    """The window's spans: the ones that START in it, plus each printer's straddler.

    Returned straddlers first, then the rest in ``started_at`` order. That is already
    the order the timeline needs — every straddler starts before the window and every
    other row inside it, so within any one printer the sequence ascends — and it is
    the order the index yields, so nothing sorts a year of rows to get it.
    """
    inside = (
        select(*_SPAN_COLUMNS)
        .where(PrinterObservationSpan.started_at >= window.start)
        .where(PrinterObservationSpan.started_at < window.end)
        .order_by(PrinterObservationSpan.started_at)
    )
    if printer_id is not None:
        inside = inside.where(PrinterObservationSpan.printer_id == printer_id)

    spans: list[SpanRow] = []
    for identifier in printer_ids:
        straddler = await _straddler(db, identifier, window.start, now)
        if straddler is not None:
            spans.append(straddler)
    spans.extend(await _stream(db, inside, _span_row))
    return spans


async def _load_evidence(
    db: AsyncSession, *, printer_ids: Sequence[int], printer_id: int | None = None
) -> list[PrinterEvidence]:
    """Per-printer facts over the WHOLE table: when its record began, and last existed.

    ``first_span_start`` is what makes "before recording" a fixed instant rather than
    a property of whichever window is being asked about, and the two last-evidence
    columns are what place a DELETED printer out of the fleet instead of leaving its
    orphaned open incident reading as downtime for ever.

    Two index seeks per printer, not a GROUP BY over the span table. The answer is one
    line per printer either way, but the aggregate reads every row of history to build
    it — so a figure whose size never changes would get slower every day the farm runs.
    The incident side stays a grouped aggregate: that table holds a row per FAULT, not
    one per sample, and is smaller by orders of magnitude.
    """
    holds = select(PrinterIncident.printer_id, func.max(PrinterIncident.created_at)).group_by(
        PrinterIncident.printer_id
    )
    if printer_id is not None:
        holds = holds.where(PrinterIncident.printer_id == printer_id)
    last_hold: dict[int, datetime | None] = {row[0]: row[1] for row in (await db.execute(holds)).all()}

    evidence: list[PrinterEvidence] = []
    for identifier in sorted(set(printer_ids) | set(last_hold)):
        first_span = await db.scalar(
            select(func.min(PrinterObservationSpan.started_at)).where(PrinterObservationSpan.printer_id == identifier)
        )
        # The last span BY START is the one that ends last: the log is run-length
        # compressed, so one printer's spans are contiguous and a later start cannot
        # carry an earlier end. That invariant is what lets a seek replace a max() over
        # a COALESCE, which no index can serve.
        last_row = (
            await db.execute(
                select(PrinterObservationSpan.ended_at, PrinterObservationSpan.last_observed_at)
                .where(PrinterObservationSpan.printer_id == identifier)
                .order_by(PrinterObservationSpan.started_at.desc())
                .limit(1)
            )
        ).first()
        last_span_end: datetime | None = None
        if last_row is not None:
            last_span_end = last_row[0] if last_row[0] is not None else last_row[1]
        evidence.append(
            PrinterEvidence(
                printer_id=identifier,
                first_span_start=first_span,
                last_span_end=last_span_end,
                last_incident_at=last_hold.get(identifier),
            )
        )
    return evidence


async def _load_prints(db: AsyncSession, window: Window) -> list[projections.PrintLogRow]:
    """Print events inside the window, off ``created_at``'s own index.

    The ORDER BY is the index's order, so it costs nothing and the rows arrive ready
    to stream. A row with no ``created_at`` cannot be placed in a bucket and is left
    out rather than being charged to the window's first one.
    """
    stmt = (
        select(PrintLogEntry.created_at, PrintLogEntry.status, PrintLogEntry.printer_id)
        .where(PrintLogEntry.created_at >= window.start)
        .where(PrintLogEntry.created_at < window.end)
        .order_by(PrintLogEntry.created_at)
    )
    rows = await _stream(
        db,
        stmt,
        lambda row: projections.PrintLogRow(created_at=row[0], status=row[1], printer_id=row[2]),
    )
    return [row for row in rows if row.created_at is not None]


async def _load_units(db: AsyncSession, window: Window) -> list[projections.UnitRow]:
    """Completed queue plates joined to the SKU file that says what a plate yields.

    No ORDER BY: the units projection places every row by its own timestamp and
    accumulates into buckets, so the order rows arrive in cannot change the answer —
    and asking for one here would make the planner sort the join's output instead of
    driving straight off ``completed_at``'s index.
    """
    stmt = (
        select(PrintQueueItem.completed_at, SkuFile.units_per_plate, Sku.code)
        .join(PrintBatch, PrintQueueItem.batch_id == PrintBatch.id)
        .join(SkuFile, PrintBatch.sku_file_id == SkuFile.id)
        .join(Sku, SkuFile.sku_id == Sku.id)
        .where(PrintQueueItem.status == _QUEUE_STATUS_COMPLETED)
        .where(PrintQueueItem.completed_at >= window.start)
        .where(PrintQueueItem.completed_at < window.end)
    )
    return await _stream(
        db,
        stmt,
        lambda row: projections.UnitRow(completed_at=row[0], units_per_plate=row[1], sku_code=row[2]),
    )


async def _load_episodes(db: AsyncSession, window: Window) -> list[projections.EpisodeRow]:
    """Episodes that ENDED in the window — an episode belongs to the bucket it finished in.

    ``kind`` is constrained to the registered vocabulary so the ``(kind, ended_at)``
    index is usable at all: its leading column is ``kind``, and a query that named
    only ``ended_at`` could not seek into it and read the whole table instead. The
    cycle projection groups rows and sorts durations itself, so no ORDER BY is asked
    for — one across both kinds would be a sort the index cannot provide.
    """
    stmt = (
        select(
            FarmCycleEpisode.printer_id,
            FarmCycleEpisode.kind,
            FarmCycleEpisode.started_at,
            FarmCycleEpisode.ended_at,
            FarmCycleEpisode.expected_s,
            FarmCycleEpisode.outcome,
            FarmCycleEpisode.variant,
        )
        .where(FarmCycleEpisode.kind.in_(sorted(EPISODE_KINDS)))
        .where(FarmCycleEpisode.ended_at >= window.start)
        .where(FarmCycleEpisode.ended_at < window.end)
    )
    return await _stream(
        db,
        stmt,
        lambda row: projections.EpisodeRow(
            printer_id=row[0],
            kind=row[1],
            started_at=row[2],
            ended_at=row[3],
            expected_s=row[4],
            outcome=row[5],
            variant=row[6],
        ),
    )


# ── the pure fold (runs in a worker thread) ─────────────────────────────────────────


def _build(facts: _WindowFacts, now: datetime) -> FleetTimeline:
    return build_timeline(
        window=facts.window,
        now=now,
        roster=facts.roster,
        spans=facts.spans,
        incidents=facts.incident_rows,
        evidence=facts.evidence,
        history_since=facts.history_since,
    )


def _compose_overview(facts: _WindowFacts, previous: _WindowFacts, now: datetime, tz_name: str) -> FleetOverview:
    """Build both timelines and every projection. Pure; the whole cost of a request."""
    timeline = _build(facts, now)
    totals = projections.class_totals(timeline)
    tally = projections.print_tally(timeline, facts.prints)
    fleet = projections.fleet_series(totals)
    prints = projections.throughput(totals, tally)

    previous_timeline = _build(previous, now)
    previous_totals = projections.class_totals(previous_timeline)
    previous_inputs = projections.SummaryInputs(
        fleet=projections.fleet_series(previous_totals),
        prints=projections.throughput(previous_totals, projections.print_tally(previous_timeline, previous.prints)),
    )

    window = facts.window
    return FleetOverview(
        date_from=window.date_from,
        date_to=window.date_to,
        bucket=window.grid.bucket,
        tz_name=tz_name,
        generated_at=now,
        window_start=window.start,
        window_end=window.end,
        summary=projections.compose_summary(projections.SummaryInputs(fleet=fleet, prints=prints), previous_inputs),
        matrix=projections.matrix(totals, tally),
        fleet_series=fleet,
        throughput=prints,
        units=projections.units(totals, facts.units),
        cycle=projections.cycle(timeline, facts.episodes),
        recovery=projections.recovery(
            timeline,
            [row for row in facts.incidents if window.start <= row.created_at < window.end],
            facts.incident_rows,
        ),
    )


# ── validation and live helpers ─────────────────────────────────────────────────────


def _window_days(date_from: date, date_to: date) -> int:
    if date_to < date_from:
        raise InvalidWindow(f"window ends before it starts: {date_from}..{date_to}")
    return (date_to - date_from).days + 1


def _resolve_bucket(date_from: date, date_to: date, bucket: Bucket | None) -> Bucket:
    """Validate the window and settle the grid. The client may omit ``bucket``."""
    days = _window_days(date_from, date_to)
    if days > MAX_WINDOW_DAYS:
        raise WindowTooLong(f"windows cover at most {MAX_WINDOW_DAYS} days; asked for {days}")
    resolved = bucket if bucket is not None else default_bucket(days)
    if resolved == "hour" and days > MAX_HOUR_WINDOW_DAYS:
        raise WindowTooLong(f"hour buckets cover at most {MAX_HOUR_WINDOW_DAYS} days; asked for {days}")
    return resolved


async def _since(
    db: AsyncSession, printer_id: int, klass: AvailabilityClass, open_kinds: frozenset[str], now: datetime
) -> tuple[datetime | None, bool]:
    """When the printer's CURRENT class began, and whether the answer is a lower bound.

    A fault or a declared hold answers exactly and at any age, from the deciding
    incident's own ``created_at`` — the store's cache, so the poll costs no query. An
    observed class has no such stamp, so its start is walked back over ADJACENT spans
    that classify the same way, bounded: a span that does not meet its successor is a
    gap, and a run that reaches the bound is reported as "at least this long" rather
    than being guessed at or scanned for.
    """
    kind = fault_kind_of(klass) or _declared_kind(klass, open_kinds)
    if kind is not None:
        payload = printer_incidents.snapshot(printer_id, kind=kind)
        created = payload.get("created_at") if payload else None
        return (datetime.fromisoformat(created) if isinstance(created, str) else None), False
    if klass.group not in (GROUP_DOWN, GROUP_IDLE):
        return None, False

    # The composite index seeks to this printer and already yields its rows in
    # ``started_at`` order, so the walk-back reads the index backwards and stops after
    # at most ``_SINCE_SPAN_LIMIT`` entries — a seek, not a scan, however long this
    # printer's history is. The ``id`` tiebreak costs nothing (the plan is identical
    # with and without it) and makes the order total, so a clock step that produced two
    # spans with one start cannot make the walk-back's answer depend on row order.
    stmt = (
        select(*_SPAN_COLUMNS)
        .where(PrinterObservationSpan.printer_id == printer_id)
        .order_by(PrinterObservationSpan.started_at.desc(), PrinterObservationSpan.id.desc())
        .limit(_SINCE_SPAN_LIMIT)
    )
    rows = (await db.execute(stmt)).all()
    since: datetime | None = None
    newer_start: datetime | None = None
    exhausted = True
    for row in rows:
        span = _span_row(row)
        # A span that does not MEET the one after it is a hole in the record, and a run
        # cannot reach back across one: what happened in the gap is unknown, not more
        # of the same.
        if newer_start is not None and span.ended_at != newer_start:
            exhausted = False
            break
        if availability_class(span.observation(), open_kinds) is not klass:
            exhausted = False
            break
        since = span.started_at
        newer_start = span.started_at
    return since, bool(since is not None and exhausted and len(rows) >= _SINCE_SPAN_LIMIT)


def _declared_kind(klass: AvailabilityClass, open_kinds: frozenset[str]) -> str | None:
    """Which declared hold decided a ``planned`` class, by the store's own precedence."""
    if klass.group != GROUP_PLANNED:
        return None
    declared = open_kinds & DECLARED_KINDS
    if not declared:
        return None
    return min(declared, key=lambda value: _RANK.get(value, len(KIND_PRECEDENCE)))
