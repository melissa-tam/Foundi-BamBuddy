"""The ONLY module here that reads a database or a live accessor.

Everything else in the package is pure: the classifier, the timeline sweep and every
projection take rows and return values. This module is where rows come from — one
window, one set of Core column selects, then the whole fold handed to a worker thread
so a year-long sweep never blocks the event loop while the farm is dispatching.

**The selects are shaped for the indexes that exist.** Spans come out of
``(printer_id, started_at)`` by the standard overlap predicate; incidents through the
store's own ``list_overlapping`` (a window reader must see the hold that OPENED
before the window and was still standing through it — the longest outages, which a
``created_at >= since`` filter is blind to); print events off
``print_log_entries.created_at``; episodes off ``(kind, ended_at)``.

**Aggregates answer the questions a window cannot.** When a printer's record began
and when it was last evidenced to exist are facts about the whole table, not about the
window, so they are two small GROUP BY queries rather than a scan: a window must not
be able to change what *before recording* means.

**Read-only, and deliberately so.** Nothing here inserts, updates or deletes. The
recorder and the episode writer own those tables; classification happens at read time
precisely so the definition of *down* can change without a migration.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import func, or_, select

from backend.app.models.farm_cycle_episode import FarmCycleEpisode
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
)
from backend.app.utils.site_time import Bucket, default_bucket, previous_window, site_today, site_zone_name

if TYPE_CHECKING:
    from datetime import tzinfo

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
    evidence = await _load_evidence(db)
    facts = await _load_window(db, window, roster=roster, evidence=evidence)
    previous_from, previous_to = previous_window(date_from, date_to)
    previous = await _load_window(
        db,
        build_window(previous_from, previous_to, resolved_bucket, tz),
        roster=roster,
        evidence=evidence,
        include_output=False,
    )

    return await asyncio.to_thread(_compose_overview, facts, previous, moment)


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
        evidence=await _load_evidence(db, printer_id=printer_id),
        printer_id=printer_id,
        include_output=False,
    )
    timeline = await asyncio.to_thread(_build, facts, moment)
    entry = printer_slice(timeline, printer_id)
    if entry is None:
        raise PrinterUnknown(f"no printer, span or incident in this window names printer {printer_id}")
    return projections.printer_intervals(timeline, entry, facts.incident_rows, generated_at=moment)


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
    first_incident = await db.scalar(select(func.min(PrinterIncident.created_at)))
    return FleetStatus(
        generated_at=moment,
        site_today=site_today(moment, tz),
        tz_name=site_zone_name(moment, tz),
        recording_since=recording_since,
        # What "all time" resolves to: the ledger reaches further back than the
        # recorder, and a range that stopped at the first span would silently drop
        # every fault the farm has a durable record of.
        history_since=min([value for value in (recording_since, first_incident) if value is not None], default=None),
        printers=printers,
        counts_by_group=by_group,
        counts_by_class=by_class,
    )


def utcnow() -> datetime:
    """The reader's clock: naive UTC at whole seconds, the tables' own convention."""
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


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


async def _load_window(
    db: AsyncSession,
    window: Window,
    *,
    roster: list[RosterPrinter],
    evidence: list[PrinterEvidence],
    printer_id: int | None = None,
    include_output: bool = True,
) -> _WindowFacts:
    """Every row one window needs. Read-only; the session is left exactly as found.

    ``roster`` and ``evidence`` are passed in because they are facts about the whole
    table, identical for a window and its predecessor — loading them twice would be
    two identical aggregates per request. ``include_output`` is off for the comparison
    window: the summary reads only its state and print series, so a year of units and
    episodes nobody will look at is a query not worth making.
    """
    incidents = await printer_incidents.list_overlapping(db, start=window.start, end=window.end)
    if printer_id is not None:
        incidents = [row for row in incidents if row.printer_id == printer_id]
    return _WindowFacts(
        window=window,
        roster=roster,
        spans=await _load_spans(db, window, printer_id=printer_id),
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


async def _load_spans(db: AsyncSession, window: Window, *, printer_id: int | None = None) -> list[SpanRow]:
    """Spans OVERLAPPING the window: started before it ended, and had not ended when it began."""
    stmt = (
        select(
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
        .where(PrinterObservationSpan.started_at < window.end)
        .where(
            or_(
                PrinterObservationSpan.ended_at.is_(None),
                PrinterObservationSpan.ended_at > window.start,
            )
        )
        .order_by(PrinterObservationSpan.printer_id, PrinterObservationSpan.started_at, PrinterObservationSpan.id)
    )
    if printer_id is not None:
        stmt = stmt.where(PrinterObservationSpan.printer_id == printer_id)
    return [
        SpanRow(
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
        for row in (await db.execute(stmt)).all()
    ]


async def _load_evidence(db: AsyncSession, *, printer_id: int | None = None) -> list[PrinterEvidence]:
    """Per-printer facts over the WHOLE table: when its record began, and last existed.

    Two aggregates, not a scan. ``first_span_start`` is what makes "before recording"
    a fixed instant rather than a property of whichever window is being asked about,
    and the two last-evidence columns are what place a DELETED printer out of the
    fleet instead of leaving its orphaned open incident reading as downtime forever.
    """
    spans = select(
        PrinterObservationSpan.printer_id,
        func.min(PrinterObservationSpan.started_at),
        func.max(func.coalesce(PrinterObservationSpan.ended_at, PrinterObservationSpan.last_observed_at)),
    ).group_by(PrinterObservationSpan.printer_id)
    holds = select(PrinterIncident.printer_id, func.max(PrinterIncident.created_at)).group_by(
        PrinterIncident.printer_id
    )
    if printer_id is not None:
        spans = spans.where(PrinterObservationSpan.printer_id == printer_id)
        holds = holds.where(PrinterIncident.printer_id == printer_id)

    first: dict[int, tuple[datetime | None, datetime | None]] = {
        row[0]: (row[1], row[2]) for row in (await db.execute(spans)).all()
    }
    last_hold: dict[int, datetime | None] = {row[0]: row[1] for row in (await db.execute(holds)).all()}
    return [
        PrinterEvidence(
            printer_id=identifier,
            first_span_start=first.get(identifier, (None, None))[0],
            last_span_end=first.get(identifier, (None, None))[1],
            last_incident_at=last_hold.get(identifier),
        )
        for identifier in sorted(set(first) | set(last_hold))
    ]


async def _load_prints(db: AsyncSession, window: Window) -> list[projections.PrintLogRow]:
    stmt = (
        select(PrintLogEntry.created_at, PrintLogEntry.status, PrintLogEntry.printer_id)
        .where(PrintLogEntry.created_at >= window.start)
        .where(PrintLogEntry.created_at < window.end)
        .order_by(PrintLogEntry.created_at)
    )
    return [
        projections.PrintLogRow(created_at=row[0], status=row[1], printer_id=row[2])
        for row in (await db.execute(stmt)).all()
        if row[0] is not None
    ]


async def _load_units(db: AsyncSession, window: Window) -> list[projections.UnitRow]:
    """Completed queue plates joined to the SKU file that says what a plate yields."""
    stmt = (
        select(PrintQueueItem.completed_at, SkuFile.units_per_plate, Sku.code)
        .join(PrintBatch, PrintQueueItem.batch_id == PrintBatch.id)
        .join(SkuFile, PrintBatch.sku_file_id == SkuFile.id)
        .join(Sku, SkuFile.sku_id == Sku.id)
        .where(PrintQueueItem.status == _QUEUE_STATUS_COMPLETED)
        .where(PrintQueueItem.completed_at >= window.start)
        .where(PrintQueueItem.completed_at < window.end)
        .order_by(PrintQueueItem.completed_at)
    )
    return [
        projections.UnitRow(completed_at=row[0], units_per_plate=row[1], sku_code=row[2])
        for row in (await db.execute(stmt)).all()
    ]


async def _load_episodes(db: AsyncSession, window: Window) -> list[projections.EpisodeRow]:
    """Episodes that ENDED in the window — an episode belongs to the bucket it finished in."""
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
        .where(FarmCycleEpisode.ended_at >= window.start)
        .where(FarmCycleEpisode.ended_at < window.end)
        .order_by(FarmCycleEpisode.ended_at)
    )
    return [
        projections.EpisodeRow(
            printer_id=row[0],
            kind=row[1],
            started_at=row[2],
            ended_at=row[3],
            expected_s=row[4],
            outcome=row[5],
            variant=row[6],
        )
        for row in (await db.execute(stmt)).all()
    ]


# ── the pure fold (runs in a worker thread) ─────────────────────────────────────────


def _build(facts: _WindowFacts, now: datetime) -> FleetTimeline:
    return build_timeline(
        window=facts.window,
        now=now,
        roster=facts.roster,
        spans=facts.spans,
        incidents=facts.incident_rows,
        evidence=facts.evidence,
    )


def _compose_overview(facts: _WindowFacts, previous: _WindowFacts, now: datetime) -> FleetOverview:
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
        tz_name=window.tz_name,
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

    stmt = (
        select(
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
        .where(PrinterObservationSpan.printer_id == printer_id)
        .order_by(PrinterObservationSpan.started_at.desc(), PrinterObservationSpan.id.desc())
        .limit(_SINCE_SPAN_LIMIT)
    )
    rows = (await db.execute(stmt)).all()
    since: datetime | None = None
    newer_start: datetime | None = None
    exhausted = True
    for row in rows:
        span = SpanRow(
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
