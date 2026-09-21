"""Tests for the fleet-metrics loader (services.fleet_metrics.loader).

The pure side is pinned elsewhere; what this file is for is everything the pure side
cannot see — that the selects find the rows, that the joins reach the SKU, that the
window bounds are refused where they should be, and above all that the figure a
reader drills into is the figure they clicked on. The cell-equals-drill-down check
runs through the DATABASE rather than over hand-built rows, because two code paths
agreeing on invented input proves nothing about two queries.

``status_now`` is driven against the house fake for ``printer_manager.get_status``,
and it ends with a PARITY pin: on every axis the live tile and the printer card both
read, the two must agree for the same fake status. A fleet page that calls a printer
idle while its own card says offline is worse than no page.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from backend.app.models.farm_cycle_episode import KIND_COOLDOWN, KIND_EJECT, FarmCycleEpisode
from backend.app.models.library import LibraryFile
from backend.app.models.print_batch import PrintBatch
from backend.app.models.print_log import PrintLogEntry
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_incident import (
    KIND_JAM,
    KIND_SERVICE_HOLD,
    STATUS_ESCALATED,
    PrinterIncident,
)
from backend.app.models.printer_observation_span import PLATE_PHASE_CLEAR, PLATE_PHASE_HELD, PrinterObservationSpan
from backend.app.models.sku import Sku, SkuFile
from backend.app.services import printer_incidents
from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.fleet_metrics import loader
from backend.app.services.fleet_metrics.classifier import down_fault
from backend.app.services.printer_manager import printer_manager, printer_state_to_dict

pytestmark = pytest.mark.asyncio

NY = ZoneInfo("America/New_York")
HOUR = timedelta(hours=1)

# 2026-09-01 00:00 in New York (EDT), as the naive UTC every table stores.
SEP1 = datetime(2026, 9, 1, 4, 0, 0)
NOW = SEP1 + timedelta(days=2)


@pytest.fixture(autouse=True)
def _clean_incident_cache():
    """The incident projection is process-wide; this suite rehydrates it per test."""
    printer_incidents._reset_state()
    yield
    printer_incidents._reset_state()


@pytest.fixture
def status_map(monkeypatch: pytest.MonkeyPatch) -> dict[int, object]:
    """The house fake: ``printer_manager.get_status`` answers from this map."""
    live: dict[int, object] = {}
    monkeypatch.setattr(printer_manager, "get_status", lambda pid: live.get(pid))
    return live


async def _printer(db, name: str, *, is_active: bool = True, model: str = "H2S") -> Printer:
    printer = Printer(
        name=name,
        serial_number=f"SN-{name}",
        ip_address="192.0.2.1",
        access_code="00000000",
        model=model,
        is_active=is_active,
    )
    db.add(printer)
    await db.flush()
    return printer


async def _span(db, printer_id: int, start: datetime, end: datetime | None, **overrides) -> None:
    fields = {
        "is_active": True,
        "connected": True,
        "gcode_state": "RUNNING",
        "plate_phase": PLATE_PHASE_CLEAR,
        "quarantined": False,
        "usb_present": True,
        "model_mismatch": False,
    }
    last = overrides.pop("last", None)
    fields.update(overrides)
    db.add(
        PrinterObservationSpan(
            printer_id=printer_id,
            started_at=start,
            last_observed_at=last if last is not None else (end if end is not None else start),
            ended_at=end,
            **fields,
        )
    )
    await db.flush()


async def _incident(db, printer_id: int, kind: str, created_at: datetime, resolved_at=None) -> PrinterIncident:
    incident = PrinterIncident(
        printer_id=printer_id,
        job_id="",
        item_id=None,
        kind=kind,
        code="",
        codes="",
        slot_global_tray=None,
        status=STATUS_ESCALATED,
        created_at=created_at,
        escalated_at=created_at,
        resolved_at=resolved_at,
    )
    db.add(incident)
    await db.flush()
    return incident


async def _print_log(db, printer_id: int | None, created_at: datetime, status: str) -> None:
    db.add(PrintLogEntry(printer_id=printer_id, status=status, created_at=created_at))
    await db.flush()


async def _sku_plate(db, *, completed_at: datetime, code: str, units_per_plate: int) -> None:
    """One completed queue plate, wired through the batch → sku_file → sku join."""
    library_file = LibraryFile(filename=f"{code}.3mf", file_path=f"/{code}.3mf", file_type="3mf", file_size=1)
    db.add(library_file)
    await db.flush()
    sku = Sku(code=code, name=code)
    db.add(sku)
    await db.flush()
    sku_file = SkuFile(sku_id=sku.id, library_file_id=library_file.id, plate_index=1, units_per_plate=units_per_plate)
    db.add(sku_file)
    await db.flush()
    batch = PrintBatch(name=f"run-{code}", sku_file_id=sku_file.id)
    db.add(batch)
    await db.flush()
    db.add(PrintQueueItem(batch_id=batch.id, status="completed", completed_at=completed_at))
    await db.flush()


async def _episode(db, printer_id: int, kind: str, start: datetime, seconds: int, outcome: str, **kw) -> None:
    db.add(
        FarmCycleEpisode(
            printer_id=printer_id,
            kind=kind,
            started_at=start,
            ended_at=start + timedelta(seconds=seconds),
            outcome=outcome,
            expected_s=kw.get("expected_s"),
            variant=kw.get("variant"),
        )
    )
    await db.flush()


async def _seed(db) -> tuple[Printer, Printer]:
    """Two printers, two days: one prints then breaks, one idles then holds a plate."""
    first = await _printer(db, "001-H2S")
    second = await _printer(db, "002-H2S")
    await _span(db, first.id, SEP1, SEP1 + 20 * HOUR)
    await _span(db, first.id, SEP1 + 20 * HOUR, NOW, connected=False, gcode_state=None)
    await _span(db, second.id, SEP1, SEP1 + 30 * HOUR, gcode_state="IDLE")
    await _span(db, second.id, SEP1 + 30 * HOUR, NOW, gcode_state="IDLE", plate_phase=PLATE_PHASE_HELD)
    await _incident(db, first.id, KIND_JAM, SEP1 + 26 * HOUR, SEP1 + 34 * HOUR)
    await _print_log(db, first.id, SEP1 + 2 * HOUR, "completed")
    await _print_log(db, first.id, SEP1 + 4 * HOUR, "failed")
    await _print_log(db, second.id, SEP1 + 28 * HOUR, "completed")
    await _sku_plate(db, completed_at=SEP1 + 3 * HOUR, code="SKU007.01", units_per_plate=4)
    await _episode(db, first.id, KIND_COOLDOWN, SEP1 + 20 * HOUR, 900, "released", variant="hold")
    await _episode(db, first.id, KIND_EJECT, SEP1 + 21 * HOUR, 120, "completed", variant="cycle", expected_s=85.0)
    await _episode(db, first.id, KIND_EJECT, SEP1 + 22 * HOUR, 300, "cancelled", variant="cycle", expected_s=85.0)
    await db.commit()
    return first, second


class TestOverview:
    async def test_it_reads_every_projection_off_one_window(self, db_session):
        first, second = await _seed(db_session)
        result = await loader.overview(
            db_session, date_from=date(2026, 9, 1), date_to=date(2026, 9, 2), bucket="day", now=NOW, tz=NY
        )

        assert result.bucket == "day"
        assert result.tz_name == "America/New_York"
        assert result.window_start == SEP1
        assert len(result.fleet_series.buckets) == 2
        assert result.fleet_series.totals.printers_known == 2

        # Throughput and units come from their own tables, not from the state record.
        assert result.throughput.totals.by_outcome == {"completed": 2, "failed": 1}
        assert result.throughput.totals.success_pct == pytest.approx(2 / 3)
        assert result.units.totals.by_sku == {"SKU007.01": 4}

        # The episode ledger is grouped by the printer's MODEL.
        ejects = next(group for group in result.cycle.by_model if group.kind == KIND_EJECT)
        assert (ejects.model, ejects.stats.count, ejects.stats.not_completed) == ("H2S", 1, 1)

        # ...and the incident ledger reports both the arrivals and the held hours.
        assert result.recovery.series.totals.opened_by_kind == {KIND_JAM: 1}
        assert result.recovery.fault_open_seconds == 8 * 3600
        assert {row.kind for row in result.recovery.time_to_recover} == {KIND_JAM}
        assert result.matrix.printers[0].printer_id == first.id
        assert result.matrix.series.totals.printers[second.id].down_seconds > 0

    async def test_the_default_bucket_follows_the_window_length_and_is_echoed(self, db_session):
        await _seed(db_session)
        # <= 3 site days is read at the hour; up to a quarter at the day; beyond that
        # by week. The client omits the parameter and the layout follows the echo.
        hourly = await loader.overview(db_session, date_from=date(2026, 9, 1), date_to=date(2026, 9, 1), now=NOW, tz=NY)
        assert hourly.bucket == "hour"
        assert len(hourly.fleet_series.buckets) == 24
        daily = await loader.overview(db_session, date_from=date(2026, 8, 29), date_to=date(2026, 9, 2), now=NOW, tz=NY)
        assert daily.bucket == "day"
        assert len(daily.fleet_series.buckets) == 5
        weekly = await loader.overview(db_session, date_from=date(2026, 1, 1), date_to=date(2026, 9, 2), now=NOW, tz=NY)
        assert weekly.bucket == "week"

    async def test_an_explicit_bucket_is_honoured(self, db_session):
        await _seed(db_session)
        result = await loader.overview(
            db_session, date_from=date(2026, 9, 1), date_to=date(2026, 9, 2), bucket="hour", now=NOW, tz=NY
        )
        assert result.bucket == "hour"
        assert len(result.fleet_series.buckets) == 48

    async def test_a_window_before_recording_reports_honestly_rather_than_perfectly(self, db_session):
        await _seed(db_session)
        result = await loader.overview(db_session, date_from=date(2026, 7, 1), date_to=date(2026, 7, 2), now=NOW, tz=NY)
        assert all(bucket.basis == "incidents_only" for bucket in result.fleet_series.buckets)
        assert result.fleet_series.totals.uptime is None
        state_row = next(row for row in result.summary.rows if row.key == loader.projections.ROW_AVG_DOWN)
        assert state_row.figure is None


class TestValidation:
    async def test_a_backwards_window_is_refused(self, db_session):
        with pytest.raises(loader.InvalidWindow):
            await loader.overview(db_session, date_from=date(2026, 9, 2), date_to=date(2026, 9, 1))

    async def test_a_window_beyond_a_year_is_refused(self, db_session):
        with pytest.raises(loader.WindowTooLong):
            await loader.overview(db_session, date_from=date(2025, 1, 1), date_to=date(2026, 9, 1))

    async def test_hour_buckets_are_refused_beyond_three_days(self, db_session):
        with pytest.raises(loader.WindowTooLong):
            await loader.overview(db_session, date_from=date(2026, 9, 1), date_to=date(2026, 9, 10), bucket="hour")

    async def test_interval_listings_are_refused_beyond_a_week(self, db_session):
        with pytest.raises(loader.WindowTooLong):
            await loader.printer_intervals(db_session, 1, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30))

    async def test_every_refusal_is_a_value_error_the_route_can_map(self, db_session):
        assert issubclass(loader.InvalidWindow, ValueError)
        assert issubclass(loader.WindowTooLong, ValueError)
        assert issubclass(loader.PrinterUnknown, loader.FleetMetricsError)


class TestPrinterIntervals:
    async def test_a_printers_intervals_sum_to_its_matrix_cell_through_the_db(self, db_session):
        # The drill-down contract, end to end: two endpoints, two queries, one answer.
        first, _ = await _seed(db_session)
        overview = await loader.overview(
            db_session, date_from=date(2026, 9, 1), date_to=date(2026, 9, 1), bucket="day", now=NOW, tz=NY
        )
        detail = await loader.printer_intervals(
            db_session, first.id, date_from=date(2026, 9, 1), date_to=date(2026, 9, 1), now=NOW, tz=NY
        )
        by_key: dict[str, float] = {}
        for interval in detail.intervals:
            by_key[interval.class_key] = by_key.get(interval.class_key, 0.0) + interval.seconds
        assert by_key == overview.matrix.series.buckets[0].values.printers[first.id].class_seconds

    async def test_it_carries_the_printer_and_the_incidents_overlapping_the_range(self, db_session):
        first, _ = await _seed(db_session)
        detail = await loader.printer_intervals(
            db_session, first.id, date_from=date(2026, 9, 2), date_to=date(2026, 9, 2), now=NOW, tz=NY
        )
        assert detail.printer.name == "001-H2S"
        assert detail.printer.deleted is False
        assert [row.kind for row in detail.incidents] == [KIND_JAM]
        assert any(interval.class_key == down_fault(KIND_JAM).key for interval in detail.intervals)

    async def test_an_unknown_printer_is_refused(self, db_session):
        await _seed(db_session)
        with pytest.raises(loader.PrinterUnknown):
            await loader.printer_intervals(
                db_session, 999, date_from=date(2026, 9, 1), date_to=date(2026, 9, 1), now=NOW, tz=NY
            )


class TestStatusNow:
    async def test_it_classifies_every_roster_printer_through_the_same_classifier(self, db_session, status_map):
        first, second = await _seed(db_session)
        status_map[first.id] = PrinterState(connected=True, connection_epoch=1, state="RUNNING", sdcard=True)
        status_map[second.id] = PrinterState(connected=False, connection_epoch=1, state="IDLE")

        result = await loader.status_now(db_session, now=NOW, tz=NY)

        by_id = {entry.printer_id: entry for entry in result.printers}
        assert by_id[first.id].class_key == "printing"
        assert by_id[second.id].class_key == "down:offline"
        assert result.counts_by_group == {"printing": 1, "down": 1}
        assert result.site_today == date(2026, 9, 3)
        assert result.tz_name == "America/New_York"
        assert result.recording_since == SEP1
        assert result.history_since == SEP1

    async def test_a_printer_the_reader_declines_to_answer_for_is_unobserved(self, db_session, status_map):
        first, _ = await _seed(db_session)
        # No live status at all and a process that has only just started: the recorder
        # itself would write nothing here, so the tile must not invent "offline".
        result = await loader.status_now(db_session, now=NOW, tz=NY)
        assert {entry.class_key for entry in result.printers} == {"unobserved"}
        assert first.id in {entry.printer_id for entry in result.printers}

    async def test_since_for_a_fault_is_the_incidents_own_created_at(self, db_session, status_map):
        first, _ = await _seed(db_session)
        opened = datetime(2026, 9, 2, 9, 0, 0)
        await _incident(db_session, first.id, KIND_JAM, opened)
        await db_session.commit()
        await printer_incidents.rehydrate(db_session)
        status_map[first.id] = PrinterState(connected=True, connection_epoch=1, state="IDLE", sdcard=True)

        result = await loader.status_now(db_session, now=NOW, tz=NY)

        entry = next(row for row in result.printers if row.printer_id == first.id)
        assert entry.class_key == down_fault(KIND_JAM).key
        assert entry.since == opened
        assert entry.since_open_ended is False

    async def test_since_for_a_declared_hold_is_that_holds_created_at(self, db_session, status_map):
        first, _ = await _seed(db_session)
        declared = datetime(2026, 9, 2, 11, 30, 0)
        await _incident(db_session, first.id, KIND_SERVICE_HOLD, declared)
        await db_session.commit()
        await printer_incidents.rehydrate(db_session)
        status_map[first.id] = PrinterState(connected=True, connection_epoch=1, state="IDLE", sdcard=True)

        result = await loader.status_now(db_session, now=NOW, tz=NY)

        entry = next(row for row in result.printers if row.printer_id == first.id)
        assert (entry.class_key, entry.since) == ("planned", declared)

    async def test_since_for_an_observed_class_is_walked_back_over_adjacent_spans(self, db_session, status_map):
        # Three consecutive spans that all classify idle are ONE run, and it began at
        # the first of them — not at the newest span's own start.
        printer = await _printer(db_session, "003-H2S")
        run_start = SEP1 + 10 * HOUR
        await _span(db_session, printer.id, SEP1, run_start, gcode_state="RUNNING")
        await _span(db_session, printer.id, run_start, run_start + HOUR, gcode_state="IDLE")
        await _span(
            db_session, printer.id, run_start + HOUR, run_start + 2 * HOUR, gcode_state="IDLE", usb_present=None
        )
        await _span(db_session, printer.id, run_start + 2 * HOUR, None, gcode_state="IDLE", last=NOW)
        await db_session.commit()
        status_map[printer.id] = PrinterState(connected=True, connection_epoch=1, state="IDLE", sdcard=True)

        result = await loader.status_now(db_session, now=NOW, tz=NY)

        entry = next(row for row in result.printers if row.printer_id == printer.id)
        assert entry.class_key == "idle"
        assert entry.since == run_start
        assert entry.since_open_ended is False

    async def test_the_walk_back_stops_at_a_hole_in_the_record(self, db_session, status_map):
        printer = await _printer(db_session, "004-H2S")
        after_gap = SEP1 + 12 * HOUR
        await _span(db_session, printer.id, SEP1, SEP1 + 4 * HOUR, gcode_state="IDLE")
        await _span(db_session, printer.id, after_gap, None, gcode_state="IDLE", last=NOW)
        await db_session.commit()
        status_map[printer.id] = PrinterState(connected=True, connection_epoch=1, state="IDLE", sdcard=True)

        result = await loader.status_now(db_session, now=NOW, tz=NY)

        entry = next(row for row in result.printers if row.printer_id == printer.id)
        assert entry.since == after_gap

    async def test_history_reaches_back_to_the_ledger_when_it_predates_the_recorder(self, db_session, status_map):
        first, _ = await _seed(db_session)
        older = datetime(2026, 8, 1, 0, 0, 0)
        await _incident(db_session, first.id, KIND_SERVICE_HOLD, older, older + HOUR)
        await db_session.commit()

        result = await loader.status_now(db_session, now=NOW, tz=NY)

        assert result.recording_since == SEP1
        assert result.history_since == older


class TestStatusCardParity:
    """The live tile and the printer card must read the same printer the same way."""

    @pytest.mark.parametrize(
        ("state", "expected_class"),
        [
            (PrinterState(connected=True, connection_epoch=1, state="RUNNING", sdcard=True), "printing"),
            (PrinterState(connected=True, connection_epoch=1, state="IDLE", sdcard=True), "idle"),
            (PrinterState(connected=True, connection_epoch=1, state="PAUSE", sdcard=True), "down:paused"),
            (PrinterState(connected=False, connection_epoch=1, state="IDLE"), "down:offline"),
            (PrinterState(connected=True, connection_epoch=1, state="IDLE", sdcard=False), "down:no_usb"),
        ],
    )
    async def test_the_shared_axes_agree_for_the_same_live_status(
        self, db_session, status_map, state: PrinterState, expected_class: str
    ):
        printer = await _printer(db_session, "005-H2S")
        await db_session.commit()
        status_map[printer.id] = state

        result = await loader.status_now(db_session, now=NOW, tz=NY)
        entry = next(row for row in result.printers if row.printer_id == printer.id)
        card = printer_state_to_dict(state, printer_id=printer.id, model="H2S")

        assert entry.class_key == expected_class
        # Connectivity: the class says offline exactly when the card says disconnected.
        assert (entry.class_key == "down:offline") is (card["connected"] is False)
        # The gcode word: the class is derived from the very state the card renders.
        assert (entry.class_key == "printing") is (card["state"] == "RUNNING" and card["connected"])
        assert (entry.class_key == "down:paused") is (card["state"] == "PAUSE" and card["connected"])
        # Quarantine, model mismatch and the plate gate are all clear on this printer,
        # and the class agrees with the card on each.
        assert card["quarantined"] is False
        assert card["model_mismatch"] is False
        assert card["occupancy"] is None or card["occupancy"]["plate"]["occupied"] is False
        assert entry.class_key not in ("down:quarantined", "down:model_mismatch", "down:plate_held")
        # ...and the identity axes the card also carries.
        assert (entry.name, entry.is_active, entry.deleted) == (printer.name, True, False)
