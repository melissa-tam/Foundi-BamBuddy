"""``GET /api/v1/archives/stats`` — the EXTERNAL contract, pinned to literals.

``foundi-accounting`` polls this endpoint with an ``X-API-Key`` and reads exactly
six fields — ``total_prints``, ``total_cost``, ``total_filament_grams``,
``total_energy_kwh``, ``total_energy_cost``, ``energy_data_warming_up`` — passing
``date_from`` / ``date_to`` as UTC days. Those six are a published interface: a
refactor of how the route buckets print outcomes may not move any of them by a
cent or a gram, and the only way to know is to have written the numbers down
first. The three outcome counters ride along because they are exactly what a
bucket refactor CAN move, so they are pinned beside the six rather than left to
be noticed by the accounting service.

Every literal below is arithmetic over :data:`_ROWS`, which covers all six
statuses the column carries, two printers and two UTC days, with a row on each
day's first and last second so the day bounds are pinned too.
"""

from datetime import datetime

import pytest
from httpx import AsyncClient

from backend.app.models.print_log import PrintLogEntry
from backend.app.models.settings import Settings

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

_DAY_A = "2026-03-10"
_DAY_B = "2026-03-11"

# (created_at, printer slot, status, cost, grams, kWh, energy cost, duration_s).
# The printer slot indexes the two printers the fixture creates.
_ROWS: tuple[tuple[datetime, int, str, float, float, float, float, int], ...] = (
    (datetime(2026, 3, 10, 0, 0, 0), 0, "completed", 1.50, 10.0, 0.50, 0.10, 3600),
    (datetime(2026, 3, 10, 12, 0, 0), 0, "failed", 2.00, 20.0, 0.25, 0.05, 1800),
    (datetime(2026, 3, 10, 18, 30, 0), 1, "aborted", 0.50, 5.0, 0.10, 0.02, 900),
    (datetime(2026, 3, 10, 23, 59, 59), 1, "stopped", 0.25, 2.5, 0.05, 0.01, 600),
    (datetime(2026, 3, 11, 0, 0, 0), 0, "completed", 3.00, 30.0, 1.00, 0.20, 7200),
    (datetime(2026, 3, 11, 9, 15, 0), 0, "cancelled", 0.75, 7.5, 0.15, 0.03, 300),
    (datetime(2026, 3, 11, 16, 45, 0), 1, "skipped", 0.10, 1.0, 0.02, 0.004, 120),
    (datetime(2026, 3, 11, 23, 59, 59), 1, "completed", 4.00, 40.0, 1.20, 0.24, 5400),
)

# The six external fields, per window. Hand-computed from _ROWS, then confirmed
# against the unrefactored route before the outcome buckets were extracted.
_ALL_TIME = {
    "total_prints": 8,
    "total_cost": 12.1,
    "total_filament_grams": 116.0,
    "total_energy_kwh": 3.27,
    "total_energy_cost": 0.654,
    "energy_data_warming_up": False,
}
_DAY_A_ONLY = {
    "total_prints": 4,
    "total_cost": 4.25,
    "total_filament_grams": 37.5,
    "total_energy_kwh": 0.9,
    "total_energy_cost": 0.18,
    "energy_data_warming_up": False,
}
_DAY_B_ONLY = {
    "total_prints": 4,
    "total_cost": 7.85,
    "total_filament_grams": 78.5,
    "total_energy_kwh": 2.37,
    "total_energy_cost": 0.474,
    "energy_data_warming_up": False,
}

# The response's own outcome counters, per window: completed / failed+aborted /
# stopped+cancelled+skipped.
_COUNTERS_ALL_TIME = {"successful_prints": 3, "failed_prints": 2, "cancelled_prints": 3}
_COUNTERS_DAY_A = {"successful_prints": 1, "failed_prints": 2, "cancelled_prints": 1}
_COUNTERS_DAY_B = {"successful_prints": 2, "failed_prints": 0, "cancelled_prints": 2}


@pytest.fixture
async def seeded_log(db_session, printer_factory) -> list[int]:
    """The eight print-log rows of :data:`_ROWS`; returns the two printer ids.

    Energy is pinned in PER-PRINT mode: the default "total" mode reads the smart
    plugs' lifetime counters, which no seeded print row can reach, so it would
    pin two of the six fields to a constant 0 whatever the route did with them.
    :class:`TestTotalEnergyMode` covers that branch separately.
    """
    printers = [await printer_factory(), await printer_factory()]
    db_session.add(Settings(key="energy_tracking_mode", value="print"))
    for created_at, slot, status, cost, grams, kwh, energy_cost, duration in _ROWS:
        db_session.add(
            PrintLogEntry(
                printer_id=printers[slot].id,
                printer_name=printers[slot].name,
                print_name=f"{status}-{created_at:%d%H%M}",
                status=status,
                started_at=created_at,
                completed_at=created_at,
                duration_seconds=duration,
                filament_used_grams=grams,
                cost=cost,
                energy_kwh=kwh,
                energy_cost=energy_cost,
                created_at=created_at,
            )
        )
    await db_session.commit()
    return [printer.id for printer in printers]


def _external(body: dict) -> dict:
    """Just the six fields foundi-accounting reads."""
    return {key: body[key] for key in _ALL_TIME}


def _counters(body: dict) -> dict:
    """The response's own outcome counters."""
    return {key: body[key] for key in _COUNTERS_ALL_TIME}


class TestExternalContract:
    async def test_unfiltered_totals(self, async_client: AsyncClient, seeded_log):
        body = (await async_client.get("/api/v1/archives/stats")).json()

        assert _external(body) == _ALL_TIME
        assert _counters(body) == _COUNTERS_ALL_TIME
        # Two printers, all eight events — the seed really is fleet-wide.
        assert body["prints_by_printer"] == {str(seeded_log[0]): 4, str(seeded_log[1]): 4}

    async def test_one_utc_day(self, async_client: AsyncClient, seeded_log):
        params = {"date_from": _DAY_A, "date_to": _DAY_A}
        body = (await async_client.get("/api/v1/archives/stats", params=params)).json()

        assert _external(body) == _DAY_A_ONLY
        assert _counters(body) == _COUNTERS_DAY_A

    async def test_the_other_utc_day(self, async_client: AsyncClient, seeded_log):
        params = {"date_from": _DAY_B, "date_to": _DAY_B}
        body = (await async_client.get("/api/v1/archives/stats", params=params)).json()

        assert _external(body) == _DAY_B_ONLY
        assert _counters(body) == _COUNTERS_DAY_B

    async def test_a_range_spanning_both_days_is_the_unfiltered_total(self, async_client: AsyncClient, seeded_log):
        """Inclusive on both ends: the 00:00:00 and 23:59:59 rows are inside."""
        params = {"date_from": _DAY_A, "date_to": _DAY_B}
        body = (await async_client.get("/api/v1/archives/stats", params=params)).json()

        assert _external(body) == _ALL_TIME
        assert _counters(body) == _COUNTERS_ALL_TIME

    async def test_a_window_before_the_data_is_all_zero(self, async_client: AsyncClient, seeded_log):
        params = {"date_from": "2026-03-01", "date_to": "2026-03-09"}
        body = (await async_client.get("/api/v1/archives/stats", params=params)).json()

        assert _external(body) == {
            "total_prints": 0,
            "total_cost": 0,
            "total_filament_grams": 0,
            "total_energy_kwh": 0.0,
            "total_energy_cost": 0.0,
            "energy_data_warming_up": False,
        }
        assert _counters(body) == {"successful_prints": 0, "failed_prints": 0, "cancelled_prints": 0}


class TestTotalEnergyMode:
    """The DEFAULT energy mode: the two energy fields come from the plugs, not the rows.

    With no smart plug configured both branches — live lifetime counters
    (unfiltered) and snapshot deltas (date-filtered) — answer zero and
    ``energy_data_warming_up`` false. The other four external fields are
    unaffected by the mode, which is what this pins.
    """

    @pytest.fixture
    async def seeded_log_default_mode(self, db_session, printer_factory) -> None:
        printers = [await printer_factory(), await printer_factory()]
        for created_at, slot, status, cost, grams, kwh, energy_cost, duration in _ROWS:
            db_session.add(
                PrintLogEntry(
                    printer_id=printers[slot].id,
                    status=status,
                    started_at=created_at,
                    completed_at=created_at,
                    duration_seconds=duration,
                    filament_used_grams=grams,
                    cost=cost,
                    energy_kwh=kwh,
                    energy_cost=energy_cost,
                    created_at=created_at,
                )
            )
        await db_session.commit()

    async def test_unfiltered(self, async_client: AsyncClient, seeded_log_default_mode):
        body = (await async_client.get("/api/v1/archives/stats")).json()

        assert _external(body) == {**_ALL_TIME, "total_energy_kwh": 0.0, "total_energy_cost": 0.0}
        assert _counters(body) == _COUNTERS_ALL_TIME

    async def test_date_filtered(self, async_client: AsyncClient, seeded_log_default_mode):
        params = {"date_from": _DAY_A, "date_to": _DAY_A}
        body = (await async_client.get("/api/v1/archives/stats", params=params)).json()

        assert _external(body) == {**_DAY_A_ONLY, "total_energy_kwh": 0.0, "total_energy_cost": 0.0}
        assert _counters(body) == _COUNTERS_DAY_A
