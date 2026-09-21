"""``GET /api/v1/fleet-metrics/*`` — the Fleet tab's three read surfaces.

The observation log records raw tuples and the incident ledger records durable holds;
everything an operator actually reads is derived from them at request time. These pins
seed the two records through the ORM and assert on the WIRE, because the properties
that matter to the client are wire properties: which window a bare request resolves
to, which bucket the server chose, which series may claim ``basis``, and whether a
drill-down can disagree with the cell it was opened from.

The window bounds get their own class. A refused window is a contract, not an
accident: a client that asked for two years at hour resolution has to be told so
rather than handed a sweep that takes the event loop with it.
"""

from datetime import date, datetime, timedelta

import pytest
from httpx import AsyncClient

from backend.app.models.farm_cycle_episode import (
    COOLDOWN_VARIANT_HOLD,
    KIND_COOLDOWN,
    KIND_EJECT,
    FarmCycleEpisode,
)
from backend.app.models.print_batch import PrintBatch
from backend.app.models.print_log import PrintLogEntry
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_incident import (
    KIND_PHYSICAL,
    RESOLVE_REPAIR_COMPLETED,
    STATUS_ESCALATED,
    STATUS_RESOLVED,
    PrinterIncident,
)
from backend.app.models.printer_observation_span import (
    PLATE_PHASE_CLEAR,
    PLATE_PHASE_COOLING,
    PrinterObservationSpan,
)
from backend.app.models.sku import Sku, SkuFile
from backend.app.services import printer_incidents
from backend.app.utils import site_time

pytestmark = pytest.mark.asyncio

_OVERVIEW = "/api/v1/fleet-metrics/overview"
_STATUS = "/api/v1/fleet-metrics/status"

#: The route's own default, restated here so a silent change to it fails a test
#: rather than a dashboard.
_DEFAULT_WINDOW_DAYS = 30

#: The service's published ceilings, restated for the same reason.
_MAX_WINDOW_DAYS = 366
_MAX_HOUR_WINDOW_DAYS = 3
_MAX_INTERVAL_DAYS = 7


@pytest.fixture(autouse=True)
def _reset_incident_cache():
    """The incident store answers the live tile from a process cache."""
    printer_incidents._reset_state()
    yield
    printer_incidents._reset_state()


def _intervals_url(printer_id: int) -> str:
    return f"/api/v1/fleet-metrics/printers/{printer_id}/intervals"


def _class_at(intervals: list[dict], instant: datetime) -> str | None:
    """The class the drill-down lists for one instant. The list is gapless and ordered."""
    stamp = instant.isoformat()
    for interval in intervals:
        if interval["start"] <= stamp < interval["end"]:
            return interval["class_key"]
    return None


async def _span(
    db,
    printer_id: int,
    started_at: datetime,
    ended_at: datetime,
    *,
    connected: bool = True,
    gcode_state: str | None = "IDLE",
    plate_phase: str = PLATE_PHASE_CLEAR,
    is_active: bool = True,
    quarantined: bool = False,
    usb_present: bool | None = True,
    model_mismatch: bool = False,
) -> PrinterObservationSpan:
    """One CLOSED observation span. Always closed: one open row per printer exists."""
    row = PrinterObservationSpan(
        printer_id=printer_id,
        started_at=started_at,
        last_observed_at=ended_at,
        ended_at=ended_at,
        is_active=is_active,
        connected=connected,
        gcode_state=gcode_state,
        plate_phase=plate_phase,
        quarantined=quarantined,
        usb_present=usb_present,
        model_mismatch=model_mismatch,
    )
    db.add(row)
    await db.commit()
    return row


async def _incident(
    db,
    printer_id: int,
    *,
    created_at: datetime,
    resolved_at: datetime | None = None,
    kind: str = KIND_PHYSICAL,
) -> PrinterIncident:
    """One equipment-fault row, written directly so its instants are in the past."""
    row = PrinterIncident(
        printer_id=printer_id,
        job_id="task-1",
        item_id=None,
        kind=kind,
        code="0700_8004",
        codes=f"{kind}:0700_8004",
        slot_global_tray=None,
        status=STATUS_RESOLVED if resolved_at is not None else STATUS_ESCALATED,
        created_at=created_at,
        resolved_at=resolved_at,
        resolve_source=RESOLVE_REPAIR_COMPLETED if resolved_at is not None else None,
    )
    db.add(row)
    await db.commit()
    return row


async def _print_log(db, printer_id: int, created_at: datetime, *, status: str = "completed") -> PrintLogEntry:
    row = PrintLogEntry(
        printer_id=printer_id,
        printer_name="Test Printer",
        print_name="SKU007.01",
        status=status,
        created_at=created_at,
        completed_at=created_at,
    )
    db.add(row)
    await db.commit()
    return row


async def _completed_unit(db, printer_id: int, completed_at: datetime, *, units_per_plate: int = 4) -> str:
    """A delivered plate joined to the SKU file that says what a plate yields."""
    sku = Sku(code=f"SKU{printer_id:03d}.01", name="Bracket")
    db.add(sku)
    await db.commit()
    sku_file = SkuFile(sku_id=sku.id, library_file_id=1000 + printer_id, plate_index=1, units_per_plate=units_per_plate)
    db.add(sku_file)
    await db.commit()
    batch = PrintBatch(name="batch", quantity=1, sku_file_id=sku_file.id)
    db.add(batch)
    await db.commit()
    db.add(
        PrintQueueItem(
            printer_id=printer_id,
            batch_id=batch.id,
            status="completed",
            completed_at=completed_at,
        )
    )
    await db.commit()
    return sku.code


async def _episodes(db, printer_id: int, ended_at: datetime) -> None:
    db.add(
        FarmCycleEpisode(
            printer_id=printer_id,
            kind=KIND_COOLDOWN,
            started_at=ended_at - timedelta(minutes=9),
            ended_at=ended_at,
            expected_s=None,
            outcome="completed",
            variant=COOLDOWN_VARIANT_HOLD,
        )
    )
    db.add(
        FarmCycleEpisode(
            printer_id=printer_id,
            kind=KIND_EJECT,
            started_at=ended_at,
            ended_at=ended_at + timedelta(seconds=85),
            expected_s=80.0,
            outcome="completed",
            variant="production",
        )
    )
    await db.commit()


async def _seed_one_printer_day(db, printer_id: int, target: date) -> dict[str, float]:
    """Cover a whole SITE day with three classifiable stretches.

    Returns the class-second map the day should add up to, built from the same
    ``day_bounds`` the server buckets on rather than from 24 h of assumed wall clock —
    a transition day is 23 h or 25 h long and the identity has to hold on those too.
    """
    start, end = site_time.day_bounds(target)
    printing_end = start + timedelta(hours=6)
    cooling_end = printing_end + timedelta(hours=1)

    await _span(db, printer_id, start, printing_end, gcode_state="RUNNING")
    await _span(db, printer_id, printing_end, cooling_end, gcode_state="FINISH", plate_phase=PLATE_PHASE_COOLING)
    await _span(db, printer_id, cooling_end, end, connected=False, gcode_state=None)
    return {
        "printing": (printing_end - start).total_seconds(),
        "cycle_overhead:cooling": (cooling_end - printing_end).total_seconds(),
        "down:offline": (end - cooling_end).total_seconds(),
    }


class TestFleetStatus:
    async def test_lists_every_roster_printer_and_the_counts_add_up(
        self, async_client: AsyncClient, printer_factory, db_session
    ):
        active = await printer_factory()
        retired = await printer_factory(is_active=False)

        response = await async_client.get(_STATUS)

        assert response.status_code == 200
        body = response.json()
        assert set(body) >= {
            "generated_at",
            "site_today",
            "tz_name",
            "recording_since",
            "history_since",
            "printers",
            "counts_by_group",
            "counts_by_class",
        }
        by_id = {row["printer_id"]: row for row in body["printers"]}
        assert set(by_id) == {active.id, retired.id}
        for row in body["printers"]:
            assert row["class_key"]
            assert row["group"]
        # A deactivated printer is the one reading that needs no session: the operator
        # withdrew it, so it is a known fact from the first tick.
        assert by_id[retired.id]["group"] == "out_of_fleet"
        assert sum(body["counts_by_group"].values()) == len(by_id)
        assert sum(body["counts_by_class"].values()) == len(by_id)
        assert body["site_today"] == site_time.site_today().isoformat()

    async def test_recording_since_reports_the_first_span(self, async_client: AsyncClient, printer_factory, db_session):
        printer = await printer_factory()
        target = site_time.site_today() - timedelta(days=2)
        start, end = site_time.day_bounds(target)
        await _span(db_session, printer.id, start, end)

        body = (await async_client.get(_STATUS)).json()

        assert body["recording_since"] == start.isoformat()
        assert body["history_since"] == start.isoformat()


class TestFleetOverview:
    async def test_returns_every_projection_with_the_documented_keys(
        self, async_client: AsyncClient, printer_factory, db_session
    ):
        printer = await printer_factory()
        target = site_time.site_today() - timedelta(days=2)
        await _seed_one_printer_day(db_session, printer.id, target)
        noon = site_time.day_bounds(target)[0] + timedelta(hours=5)
        await _print_log(db_session, printer.id, noon)
        await _completed_unit(db_session, printer.id, noon)
        await _episodes(db_session, printer.id, noon)

        response = await async_client.get(
            _OVERVIEW, params={"date_from": target.isoformat(), "date_to": target.isoformat(), "bucket": "day"}
        )

        assert response.status_code == 200
        body = response.json()
        assert set(body) == {
            "date_from",
            "date_to",
            "bucket",
            "tz_name",
            "generated_at",
            "window_start",
            "window_end",
            "summary",
            "matrix",
            "fleet_series",
            "throughput",
            "units",
            "cycle",
            "recovery",
        }
        assert body["matrix"]["printers"][0]["printer_id"] == printer.id
        assert set(body["matrix"]["series"]) == {"buckets", "totals"}
        assert body["throughput"]["totals"]["by_outcome"]["completed"] == 1
        assert body["units"]["totals"]["units"] == 4
        assert body["units"]["totals"]["plates"] == 1
        assert {group["kind"] for group in body["cycle"]["by_model"]} == {KIND_COOLDOWN, KIND_EJECT}
        assert set(body["recovery"]) == {"series", "summary", "time_to_recover", "fault_open_seconds"}
        assert [row["key"] for row in body["summary"]["rows"]]

    async def test_a_bare_request_resolves_thirty_site_days_and_echoes_its_bucket(
        self, async_client: AsyncClient, printer_factory
    ):
        await printer_factory()

        body = (await async_client.get(_OVERVIEW)).json()

        today = site_time.site_today()
        assert body["date_to"] == today.isoformat()
        assert body["date_from"] == (today - timedelta(days=_DEFAULT_WINDOW_DAYS - 1)).isoformat()
        # The client omits the bucket precisely so the server's choice is the one the
        # columns are laid out from.
        assert body["bucket"] == site_time.default_bucket(_DEFAULT_WINDOW_DAYS) == "day"
        assert len(body["fleet_series"]["buckets"]) == _DEFAULT_WINDOW_DAYS

    async def test_supplying_one_bound_leaves_the_other_rule_alone(self, async_client: AsyncClient, printer_factory):
        await printer_factory()
        today = site_time.site_today()
        given_from = today - timedelta(days=4)

        from_only = (await async_client.get(_OVERVIEW, params={"date_from": given_from.isoformat()})).json()
        assert from_only["date_from"] == given_from.isoformat()
        assert from_only["date_to"] == today.isoformat()

        given_to = today - timedelta(days=10)
        to_only = (await async_client.get(_OVERVIEW, params={"date_to": given_to.isoformat()})).json()
        assert to_only["date_to"] == given_to.isoformat()
        assert to_only["date_from"] == (given_to - timedelta(days=_DEFAULT_WINDOW_DAYS - 1)).isoformat()

    async def test_an_explicit_bucket_is_honoured(self, async_client: AsyncClient, printer_factory):
        await printer_factory()
        today = site_time.site_today()
        date_from = today - timedelta(days=29)

        body = (
            await async_client.get(
                _OVERVIEW,
                params={"date_from": date_from.isoformat(), "date_to": today.isoformat(), "bucket": "week"},
            )
        ).json()

        assert body["bucket"] == "week"
        expected = len(site_time.bucket_edges(date_from, today, "week"))
        assert len(body["fleet_series"]["buckets"]) == expected
        assert expected < _DEFAULT_WINDOW_DAYS  # weeks, not days

    async def test_basis_rides_only_the_state_derived_series(
        self, async_client: AsyncClient, printer_factory, db_session
    ):
        printer = await printer_factory()
        target = site_time.site_today() - timedelta(days=2)
        await _seed_one_printer_day(db_session, printer.id, target)
        noon = site_time.day_bounds(target)[0] + timedelta(hours=5)
        await _print_log(db_session, printer.id, noon)
        await _completed_unit(db_session, printer.id, noon)

        body = (
            await async_client.get(
                _OVERVIEW, params={"date_from": target.isoformat(), "date_to": target.isoformat(), "bucket": "day"}
            )
        ).json()

        for bucket in body["matrix"]["series"]["buckets"]:
            assert bucket["basis"] == "observed"
        for bucket in body["fleet_series"]["buckets"]:
            assert bucket["basis"] == "observed"
        # Print, unit and incident data is complete for its OWN history, so marking it
        # partial because the state recorder had a gap would be a lie.
        for series in (body["throughput"], body["units"], body["recovery"]["series"]):
            assert series["buckets"], "a series with no buckets proves nothing here"
            for bucket in series["buckets"]:
                assert bucket["basis"] is None

    async def test_a_pre_recording_window_is_incidents_only_with_null_ratios(
        self, async_client: AsyncClient, printer_factory, db_session
    ):
        printer = await printer_factory()
        target = site_time.site_today() - timedelta(days=5)
        start, end = site_time.day_bounds(target)
        # No spans at all: the recorder did not exist yet. The ledger still does.
        await _incident(
            db_session, printer.id, created_at=start + timedelta(hours=1), resolved_at=start + timedelta(hours=4)
        )

        body = (
            await async_client.get(
                _OVERVIEW, params={"date_from": target.isoformat(), "date_to": target.isoformat(), "bucket": "day"}
            )
        ).json()

        bucket = body["fleet_series"]["buckets"][0]
        assert bucket["basis"] == "incidents_only"
        assert bucket["observed_seconds"] == 0
        # A ratio whose denominator nobody measured is not a low number, it is no number.
        assert bucket["values"]["uptime"] is None
        assert bucket["values"]["time_printing"] is None
        # The hold itself is durable evidence and is reported in full.
        assert body["recovery"]["fault_open_seconds"] == pytest.approx(3 * 3600, rel=1e-6)
        assert body["matrix"]["series"]["buckets"][0]["values"]["fleet"]["basis"] == "incidents_only"
        assert end > start  # the window really was a whole site day


class TestOverviewWindowBounds:
    async def test_a_backwards_window_is_refused(self, async_client: AsyncClient):
        today = site_time.site_today()
        response = await async_client.get(
            _OVERVIEW,
            params={"date_from": today.isoformat(), "date_to": (today - timedelta(days=1)).isoformat()},
        )
        assert response.status_code == 422

    async def test_a_window_past_the_sweep_ceiling_is_refused(self, async_client: AsyncClient):
        today = site_time.site_today()
        response = await async_client.get(
            _OVERVIEW,
            params={
                "date_from": (today - timedelta(days=_MAX_WINDOW_DAYS)).isoformat(),
                "date_to": today.isoformat(),
            },
        )
        assert response.status_code == 422

    async def test_the_ceiling_itself_is_allowed(self, async_client: AsyncClient, printer_factory):
        await printer_factory()
        today = site_time.site_today()
        response = await async_client.get(
            _OVERVIEW,
            params={
                "date_from": (today - timedelta(days=_MAX_WINDOW_DAYS - 1)).isoformat(),
                "date_to": today.isoformat(),
            },
        )
        assert response.status_code == 200
        assert response.json()["bucket"] == "week"

    async def test_hour_buckets_past_three_days_are_refused(self, async_client: AsyncClient):
        today = site_time.site_today()
        response = await async_client.get(
            _OVERVIEW,
            params={
                "date_from": (today - timedelta(days=_MAX_HOUR_WINDOW_DAYS)).isoformat(),
                "date_to": today.isoformat(),
                "bucket": "hour",
            },
        )
        assert response.status_code == 422

    async def test_three_days_of_hour_buckets_is_allowed(self, async_client: AsyncClient, printer_factory):
        """The allowed side of the same ceiling — an off-by-one here refuses a valid day."""
        await printer_factory()
        today = site_time.site_today()
        date_from = today - timedelta(days=_MAX_HOUR_WINDOW_DAYS - 1)
        response = await async_client.get(
            _OVERVIEW,
            params={"date_from": date_from.isoformat(), "date_to": today.isoformat(), "bucket": "hour"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["bucket"] == "hour"
        assert len(body["fleet_series"]["buckets"]) == len(site_time.bucket_edges(date_from, today, "hour"))

    async def test_an_unknown_bucket_is_refused_by_the_type(self, async_client: AsyncClient):
        today = site_time.site_today()
        response = await async_client.get(
            _OVERVIEW,
            params={"date_from": today.isoformat(), "date_to": today.isoformat(), "bucket": "fortnight"},
        )
        assert response.status_code == 422


class TestPrinterIntervals:
    async def test_one_printer_day_sums_to_its_matrix_cell(
        self, async_client: AsyncClient, printer_factory, db_session
    ):
        printer = await printer_factory()
        await printer_factory()  # a second roster row, so the cell has to be selected
        target = site_time.site_today() - timedelta(days=2)
        expected = await _seed_one_printer_day(db_session, printer.id, target)

        drill = await async_client.get(
            _intervals_url(printer.id), params={"date_from": target.isoformat(), "date_to": target.isoformat()}
        )
        assert drill.status_code == 200
        body = drill.json()
        assert set(body) == {
            "printer",
            "date_from",
            "date_to",
            "tz_name",
            "generated_at",
            "intervals",
            "incidents",
        }
        assert body["printer"]["printer_id"] == printer.id

        summed: dict[str, float] = {}
        for interval in body["intervals"]:
            summed[interval["class_key"]] = summed.get(interval["class_key"], 0.0) + interval["seconds"]
        assert summed == pytest.approx(expected)

        overview = (
            await async_client.get(
                _OVERVIEW, params={"date_from": target.isoformat(), "date_to": target.isoformat(), "bucket": "day"}
            )
        ).json()
        # Keys arrive as strings: a JSON object cannot have integer keys.
        cell = overview["matrix"]["series"]["buckets"][0]["values"]["printers"][str(printer.id)]
        assert cell["class_seconds"] == pytest.approx(expected)
        assert cell["down_seconds"] == pytest.approx(expected["down:offline"])

    async def test_a_fault_reads_as_down_but_never_overlays_an_observed_print(
        self, async_client: AsyncClient, printer_factory, db_session
    ):
        """Both halves of the precedence, on one printer-day and one response.

        A hold the ledger proves outranks a missing sample and an idle printer, which
        is what makes fault history readable from before the recorder shipped. What it
        must never do is overlay an OBSERVED physical process: a print running under an
        open hold is real output, and counting that hour as downtime would take a
        finished part off the tally.
        """
        printer = await printer_factory()
        target = site_time.site_today() - timedelta(days=2)
        start, _end = site_time.day_bounds(target)
        await _seed_one_printer_day(db_session, printer.id, target)
        while_printing = await _incident(
            db_session, printer.id, created_at=start + timedelta(hours=1), resolved_at=start + timedelta(hours=2)
        )
        while_offline = await _incident(
            db_session, printer.id, created_at=start + timedelta(hours=8), resolved_at=start + timedelta(hours=9)
        )

        body = (
            await async_client.get(
                _intervals_url(printer.id),
                params={"date_from": target.isoformat(), "date_to": target.isoformat()},
            )
        ).json()

        assert sorted(incident["incident_id"] for incident in body["incidents"]) == sorted(
            [while_printing.id, while_offline.id]
        )
        at_one = _class_at(body["intervals"], start + timedelta(minutes=90))
        at_eight = _class_at(body["intervals"], start + timedelta(hours=8, minutes=30))
        assert at_one == "printing"
        assert at_eight == f"down:fault:{KIND_PHYSICAL}"

    async def test_a_range_past_seven_days_is_refused(self, async_client: AsyncClient, printer_factory):
        printer = await printer_factory()
        today = site_time.site_today()
        response = await async_client.get(
            _intervals_url(printer.id),
            params={
                "date_from": (today - timedelta(days=_MAX_INTERVAL_DAYS)).isoformat(),
                "date_to": today.isoformat(),
            },
        )
        assert response.status_code == 422

    async def test_an_unknown_printer_is_not_found(self, async_client: AsyncClient):
        response = await async_client.get(_intervals_url(987654))
        assert response.status_code == 404

    async def test_both_bounds_default_to_the_sites_today(self, async_client: AsyncClient, printer_factory, db_session):
        printer = await printer_factory()
        today = site_time.site_today()
        start, _end = site_time.day_bounds(today)
        await _span(db_session, printer.id, start, start + timedelta(minutes=30))

        body = (await async_client.get(_intervals_url(printer.id))).json()

        assert body["date_from"] == today.isoformat()
        assert body["date_to"] == today.isoformat()


class TestFleetMetricsPermission:
    """With auth on, every route sits behind ``STATS_READ`` (``can_read_status``)."""

    async def _enable_auth(self, db_session, *, can_read_status: bool) -> str:
        from backend.app.core.auth import generate_api_key
        from backend.app.models.api_key import APIKey
        from backend.app.models.settings import Settings

        db_session.add(Settings(key="auth_enabled", value="true"))
        full_key, key_hash, key_prefix = generate_api_key()
        db_session.add(
            APIKey(
                name="stats-key",
                key_hash=key_hash,
                key_prefix=key_prefix,
                can_read_status=can_read_status,
                enabled=True,
            )
        )
        await db_session.commit()
        return full_key

    async def test_no_credentials_at_all_is_unauthorized(self, async_client: AsyncClient, db_session):
        await self._enable_auth(db_session, can_read_status=True)
        response = await async_client.get(_STATUS)
        assert response.status_code == 401

    async def test_a_key_without_stats_read_is_forbidden(self, async_client: AsyncClient, db_session):
        key = await self._enable_auth(db_session, can_read_status=False)
        response = await async_client.get(_STATUS, headers={"X-API-Key": key})
        assert response.status_code == 403

    async def test_a_key_with_stats_read_is_served(self, async_client: AsyncClient, printer_factory, db_session):
        await printer_factory()
        key = await self._enable_auth(db_session, can_read_status=True)
        response = await async_client.get(_STATUS, headers={"X-API-Key": key})
        assert response.status_code == 200

    async def test_the_history_routes_carry_the_same_gate(self, async_client: AsyncClient, printer_factory, db_session):
        printer = await printer_factory()
        key = await self._enable_auth(db_session, can_read_status=False)
        today = date.today().isoformat()
        assert (await async_client.get(_OVERVIEW, headers={"X-API-Key": key})).status_code == 403
        forbidden = await async_client.get(
            _intervals_url(printer.id), params={"date_from": today, "date_to": today}, headers={"X-API-Key": key}
        )
        assert forbidden.status_code == 403
