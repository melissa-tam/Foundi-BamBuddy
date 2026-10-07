"""``GET /api/v1/incidents`` — the equipment-fault ledger's query surface (2026-09-11).

The 2026-09-11 audit answered "how many holds did the farm end by itself" (9 of 122)
with a read-only SELECT over the farm's SSH lane and a day of log reading. The
derivation is now ONE table (``printer_incidents.outcome_of``) and this route is how
the number is read back — since 2026-10-07 by the Stats page's Faults tab, over the
page's site-date window with window-wide ``summary`` / ``total`` and offset paging.
Most pins seed rows through the store's own writers, so the shapes are the ones
production produces; the window-edge pins build rows directly because they must place
``created_at`` on an exact instant.
"""

from datetime import date, datetime, timedelta

import pytest
from httpx import AsyncClient

from backend.app.models.printer_incident import (
    KIND_JAM,
    KIND_PHYSICAL,
    KIND_RUNOUT,
    KIND_SERVICE_HOLD,
    RESOLVE_DRIVER_SWAP,
    RESOLVE_OBSERVED_RUNNING,
    STATUS_ESCALATED,
    STATUS_RESOLVED,
    PrinterIncident,
)
from backend.app.services import printer_incidents
from backend.app.services.hms_errors import printer_message_from_short_code
from backend.app.utils import site_time

pytestmark = pytest.mark.asyncio

_URL = "/api/v1/incidents"


@pytest.fixture(autouse=True)
def _reset_cache():
    printer_incidents._reset_state()
    yield
    printer_incidents._reset_state()


async def _seed(db, printer_id, *, kind=KIND_JAM, code="0700_8010", status=None, close=None):
    row = await printer_incidents.open_new(
        db,
        printer_id=printer_id,
        job_id="task-1",
        item_id=None,
        kind=kind,
        code=code,
        codes=f"{kind}:{code}",
        slot_global_tray=None,
        status=status or "recovering",
    )
    assert row is not None
    if close is not None:
        await printer_incidents.close(db, row.id, status=STATUS_RESOLVED, source=close)
    return row


async def _closed_at(db, printer_id, created_at, *, kind=KIND_JAM, code="0700_8010", source=RESOLVE_DRIVER_SWAP):
    """A CLOSED row opened at an exact instant (closed rows never collide on the open-row indexes)."""
    row = PrinterIncident(
        printer_id=printer_id,
        job_id="task-1",
        item_id=None,
        kind=kind,
        code=code,
        codes=f"{kind}:{code}",
        slot_global_tray=None,
        status=STATUS_RESOLVED,
        created_at=created_at,
        resolved_at=created_at + timedelta(minutes=2),
        resolve_source=source,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


def _site_noon(d: date) -> datetime:
    return site_time.day_bounds(d)[0] + timedelta(hours=12)


def _dates(date_from: date, date_to: date) -> dict[str, str]:
    return {"date_from": date_from.isoformat(), "date_to": date_to.isoformat()}


class TestListIncidents:
    async def test_returns_the_rows_newest_first_with_derived_fields(
        self, async_client: AsyncClient, printer_factory, db_session
    ):
        printer = await printer_factory()
        swapped = await _seed(db_session, printer.id, close=RESOLVE_DRIVER_SWAP)
        held = await _seed(db_session, printer.id, kind=KIND_PHYSICAL, code="0700_8004", status=STATUS_ESCALATED)

        response = await async_client.get(_URL)

        assert response.status_code == 200
        body = response.json()
        assert [row["id"] for row in body["items"]] == [held.id, swapped.id]
        physical, jam = body["items"]
        assert physical["kind"] == KIND_PHYSICAL
        assert physical["outcome"] == printer_incidents.OUTCOME_HELD
        assert physical["resolution_class"] == "repair"  # an AMS physical fault
        assert physical["external"] is False
        assert physical["printer_name"] == printer.name
        assert physical["resolved_at"] is None
        assert physical["held_s"] >= 0.0
        assert physical["recurring"] is False
        assert jam["outcome"] == printer_incidents.OUTCOME_AUTO_RECOVERED
        assert jam["resolve_source"] == RESOLVE_DRIVER_SWAP
        assert jam["resolution_class"] == "wire"

    async def test_the_summary_is_the_zero_human_tally(self, async_client: AsyncClient, printer_factory, db_session):
        a = await printer_factory()
        b = await printer_factory()
        await _seed(db_session, a.id, close=RESOLVE_DRIVER_SWAP)  # the farm did it
        await _seed(db_session, b.id, kind=KIND_RUNOUT, code="0700_8011", status=STATUS_ESCALATED)  # a human's
        # A runout that paged and was then resumed on the screen: human-resolved.
        row = await _seed(db_session, a.id, kind=KIND_RUNOUT, code="0700_8011", status=STATUS_ESCALATED)
        await printer_incidents.close(db_session, row.id, status=STATUS_RESOLVED, source=RESOLVE_OBSERVED_RUNNING)

        body = (await async_client.get(_URL)).json()

        summary = body["summary"]
        assert summary["total"] == 3
        assert summary["zero_human"] == 1
        assert summary["by_outcome"][printer_incidents.OUTCOME_AUTO_RECOVERED] == 1
        assert summary["by_outcome"][printer_incidents.OUTCOME_HELD] == 1
        assert summary["by_outcome"][printer_incidents.OUTCOME_HUMAN_RESOLVED] == 1
        assert summary["by_kind"][KIND_JAM][printer_incidents.OUTCOME_AUTO_RECOVERED] == 1
        assert summary["by_kind"][KIND_RUNOUT][printer_incidents.OUTCOME_HELD] == 1

    async def test_filters_by_kind_printer_and_window(self, async_client: AsyncClient, printer_factory, db_session):
        a = await printer_factory()
        b = await printer_factory()
        await _seed(db_session, a.id, close=RESOLVE_DRIVER_SWAP)
        old = await _seed(db_session, b.id, kind=KIND_RUNOUT, code="0700_8011", status=STATUS_ESCALATED)
        old.created_at = datetime.utcnow() - timedelta(days=40)
        await db_session.commit()
        today = site_time.site_today()

        by_kind = (await async_client.get(_URL, params={"kind": KIND_JAM})).json()
        assert [row["printer_id"] for row in by_kind["items"]] == [a.id]

        by_printer = (
            await async_client.get(_URL, params={"printer_id": b.id, **_dates(today - timedelta(days=60), today)})
        ).json()
        assert [row["id"] for row in by_printer["items"]] == [old.id]

        last_30 = (await async_client.get(_URL, params=_dates(today - timedelta(days=30), today))).json()
        assert old.id not in [row["id"] for row in last_30["items"]]
        assert last_30["total"] == 1

    async def test_an_unknown_kind_is_refused(self, async_client: AsyncClient):
        response = await async_client.get(_URL, params={"kind": "gremlin"})
        assert response.status_code == 422

    async def test_an_unknown_outcome_is_refused(self, async_client: AsyncClient):
        response = await async_client.get(_URL, params={"outcome": "gremlin"})
        assert response.status_code == 422

    async def test_a_backwards_window_is_refused(self, async_client: AsyncClient):
        today = site_time.site_today()
        response = await async_client.get(_URL, params=_dates(today, today - timedelta(days=1)))
        assert response.status_code == 422

    async def test_the_since_parameter_is_gone(self, async_client: AsyncClient, printer_factory, db_session):
        """``since`` was deleted (2026-10-07): an old client's bound is ignored, never honoured."""
        printer = await printer_factory()
        old = await _closed_at(db_session, printer.id, datetime.utcnow() - timedelta(days=40))

        body = (await async_client.get(_URL, params={"since": datetime.utcnow().isoformat()})).json()

        assert [row["id"] for row in body["items"]] == [old.id]

    async def test_an_empty_window_still_answers(self, async_client: AsyncClient):
        today = site_time.site_today()
        body = (await async_client.get(_URL, params=_dates(today - timedelta(days=6), today))).json()
        assert body["items"] == []
        assert body["total"] == 0
        assert body["recurring"] == []
        assert body["summary"]["total"] == 0
        assert body["summary"]["zero_human"] == 0
        assert body["date_from"] == (today - timedelta(days=6)).isoformat()
        assert body["date_to"] == today.isoformat()
        assert body["tz_name"] == site_time.site_zone_name()
        assert body["limit"] == 50
        assert body["offset"] == 0


class TestTheSiteDayWindow:
    async def test_the_window_is_the_site_days_half_open(self, async_client: AsyncClient, printer_factory, db_session):
        """A row AT ``day_bounds(date_from).start`` is in, one second before is out, and a
        row AT ``day_bounds(date_to).end`` is out — the edges ``/fleet-metrics`` buckets on."""
        printer = await printer_factory()
        date_to = site_time.site_today() - timedelta(days=3)
        date_from = date_to - timedelta(days=1)
        start = site_time.day_bounds(date_from)[0]
        end = site_time.day_bounds(date_to)[1]
        at_start = await _closed_at(db_session, printer.id, start)
        await _closed_at(db_session, printer.id, start - timedelta(seconds=1))
        last_second = await _closed_at(db_session, printer.id, end - timedelta(seconds=1))
        await _closed_at(db_session, printer.id, end)

        body = (await async_client.get(_URL, params=_dates(date_from, date_to))).json()

        assert [row["id"] for row in body["items"]] == [last_second.id, at_start.id]
        assert body["total"] == 2

    async def test_each_row_carries_the_site_offset_at_its_open(
        self, async_client: AsyncClient, printer_factory, db_session
    ):
        """``tz_name`` may be an OS display name no browser resolves; the offset is the
        render key, read AT each row's own ``created_at`` (expectation via ``site_time``)."""
        printer = await printer_factory()
        today = site_time.site_today()
        rows = [await _closed_at(db_session, printer.id, _site_noon(today - timedelta(days=days))) for days in (1, 200)]

        body = (await async_client.get(_URL)).json()

        by_id = {item["id"]: item for item in body["items"]}
        for row in rows:
            assert by_id[row.id]["utc_offset_minutes"] == site_time.offset_minutes(row.created_at)

    async def test_no_bounds_reads_every_row_and_one_sided_bounds_open_the_other_side(
        self, async_client: AsyncClient, printer_factory, db_session
    ):
        printer = await printer_factory()
        today = site_time.site_today()
        ancient = await _closed_at(db_session, printer.id, _site_noon(today - timedelta(days=40)))
        recent = await _closed_at(db_session, printer.id, _site_noon(today - timedelta(days=2)))

        unbounded = (await async_client.get(_URL)).json()
        assert [row["id"] for row in unbounded["items"]] == [recent.id, ancient.id]
        assert unbounded["date_from"] is None
        assert unbounded["date_to"] is None

        from_only = (
            await async_client.get(_URL, params={"date_from": (today - timedelta(days=10)).isoformat()})
        ).json()
        assert [row["id"] for row in from_only["items"]] == [recent.id]

        to_only = (await async_client.get(_URL, params={"date_to": (today - timedelta(days=10)).isoformat()})).json()
        assert [row["id"] for row in to_only["items"]] == [ancient.id]


class TestFiltersPagingAndTotals:
    async def test_the_outcome_filter_narrows_rows_and_total_but_not_the_summary(
        self, async_client: AsyncClient, printer_factory, db_session
    ):
        a = await printer_factory()
        b = await printer_factory()
        await _seed(db_session, a.id, close=RESOLVE_DRIVER_SWAP)
        swapped_b = await _seed(db_session, b.id, close=RESOLVE_DRIVER_SWAP)
        await _seed(db_session, b.id, kind=KIND_PHYSICAL, code="0700_8004", status=STATUS_ESCALATED)

        body = (await async_client.get(_URL, params={"outcome": printer_incidents.OUTCOME_AUTO_RECOVERED})).json()

        assert {row["outcome"] for row in body["items"]} == {printer_incidents.OUTCOME_AUTO_RECOVERED}
        assert body["total"] == 2
        # The strip's population: kind + printer filters only, so the share stays honest.
        assert body["summary"]["total"] == 3
        assert body["summary"]["zero_human"] == 2

        narrowed = (
            await async_client.get(
                _URL, params={"outcome": printer_incidents.OUTCOME_AUTO_RECOVERED, "printer_id": b.id}
            )
        ).json()
        assert [row["id"] for row in narrowed["items"]] == [swapped_b.id]
        assert narrowed["total"] == 1
        assert narrowed["summary"]["total"] == 2  # printer b's two faults, whatever their outcome

    async def test_offset_paging_keeps_total_and_summary_on_every_page(
        self, async_client: AsyncClient, printer_factory, db_session
    ):
        printer = await printer_factory()
        today = site_time.site_today()
        newest, second, _oldest = [
            await _closed_at(db_session, printer.id, _site_noon(today - timedelta(days=days))) for days in (1, 2, 3)
        ]

        first = (await async_client.get(_URL, params={"limit": 1, "offset": 0})).json()
        page_two = (await async_client.get(_URL, params={"limit": 1, "offset": 1})).json()
        past_end = (await async_client.get(_URL, params={"limit": 1, "offset": 5})).json()

        assert [row["id"] for row in first["items"]] == [newest.id]
        assert [row["id"] for row in page_two["items"]] == [second.id]
        assert past_end["items"] == []
        assert first["total"] == page_two["total"] == past_end["total"] == 3
        assert first["summary"] == page_two["summary"] == past_end["summary"]
        assert page_two["limit"] == 1
        assert page_two["offset"] == 1

    async def test_the_limit_is_bounded(self, async_client: AsyncClient):
        assert (await async_client.get(_URL, params={"limit": 1001})).status_code == 422
        assert (await async_client.get(_URL, params={"limit": 1000})).status_code == 200
        assert (await async_client.get(_URL, params={"offset": -1})).status_code == 422

    async def test_total_is_the_faults_plus_the_declared_holds(
        self, async_client: AsyncClient, printer_factory, db_session
    ):
        printer = await printer_factory()
        await _seed(db_session, printer.id, close=RESOLVE_DRIVER_SWAP)
        assert await printer_incidents.open_declared(db_session, printer.id, kind=KIND_SERVICE_HOLD) is not None

        body = (await async_client.get(_URL)).json()

        assert body["total"] == 2
        assert body["summary"]["declared"] == 1
        assert body["total"] == body["summary"]["total"] + body["summary"]["declared"]

    async def test_the_summary_equals_the_fleet_tabs_recovery_summary(
        self, async_client: AsyncClient, printer_factory, db_session
    ):
        """Parity: one window, one tally — the Faults strip and the Fleet tab cannot disagree."""
        a = await printer_factory()
        b = await printer_factory()
        today = site_time.site_today()
        date_from, date_to = today - timedelta(days=6), today - timedelta(days=1)
        await _closed_at(db_session, a.id, site_time.day_bounds(date_from)[0])  # AT the start: in
        await _closed_at(db_session, a.id, site_time.day_bounds(date_from)[0] - timedelta(seconds=1))  # out
        await _closed_at(db_session, b.id, _site_noon(date_to), kind=KIND_RUNOUT, code="0700_8011", source="terminal")
        # Zero-length at the window start: opened (and closed) exactly AT the edge.
        edge = await _closed_at(db_session, b.id, site_time.day_bounds(date_from)[0])
        edge.resolved_at = edge.created_at
        await db_session.commit()

        ledger = (await async_client.get(_URL, params=_dates(date_from, date_to))).json()
        overview = (
            await async_client.get(
                "/api/v1/fleet-metrics/overview", params={**_dates(date_from, date_to), "bucket": "day"}
            )
        ).json()

        assert ledger["summary"] == overview["recovery"]["summary"]
        assert ledger["summary"]["total"] == 3


class TestRecurringOnTheWire:
    async def _three_day_extruder_overload(self, db_session, printer_factory):
        printers = [await printer_factory(name=f"00{index}-H2S") for index in range(1, 5)]
        today = site_time.site_today()
        repeat = printers[0]
        for days in (1, 2, 3):
            await _closed_at(db_session, repeat.id, _site_noon(today - timedelta(days=days)), code="0300_801E")
        other = await _closed_at(db_session, printers[1].id, _site_noon(today - timedelta(days=1)))
        runout = await _closed_at(
            db_session, printers[2].id, _site_noon(today - timedelta(days=1)), kind=KIND_RUNOUT, code="0700_8011"
        )
        return repeat, other, runout, today

    async def test_a_printer_line_carries_the_taxonomy_words_and_flags_its_rows(
        self, async_client: AsyncClient, printer_factory, db_session
    ):
        repeat, other, _runout, today = await self._three_day_extruder_overload(db_session, printer_factory)
        words = printer_message_from_short_code("0300_801E")
        assert words is not None and words.description

        body = (await async_client.get(_URL, params=_dates(today - timedelta(days=29), today))).json()

        assert len(body["recurring"]) == 1
        line = body["recurring"][0]
        assert line["scope"] == "printer"
        assert line["printer_id"] == repeat.id
        assert line["printer_name"] == repeat.name
        assert (line["kind"], line["code"]) == (KIND_JAM, "0300_801E")
        assert line["printer_message"] == {"short_code": "0300_801E", "description": words.description}
        assert (line["holds"], line["days"], line["printers_affected"], line["roster_size"]) == (3, 3, 1, 4)
        assert line["fleet_median"] == 0
        assert line["worst"] == []
        flagged = {row["id"] for row in body["items"] if row["recurring"]}
        assert flagged == {row["id"] for row in body["items"] if row["printer_id"] == repeat.id}
        assert other.id not in flagged
        overload = next(row for row in body["items"] if row["printer_id"] == repeat.id)
        assert overload["printer_messages"] == [{"short_code": "0300_801E", "description": words.description}]

    async def test_filters_never_change_what_counts_as_recurring(
        self, async_client: AsyncClient, printer_factory, db_session
    ):
        repeat, _other, runout, today = await self._three_day_extruder_overload(db_session, printer_factory)

        body = (
            await async_client.get(_URL, params={"kind": KIND_RUNOUT, **_dates(today - timedelta(days=29), today)})
        ).json()

        assert [row["id"] for row in body["items"]] == [runout.id]
        assert [(line["printer_id"], line["code"]) for line in body["recurring"]] == [(repeat.id, "0300_801E")]


class TestPermission:
    """The ledger read sits behind ``STATS_READ`` — the Statistics page's gate."""

    async def _token_for(self, db_session, *, permissions: list[str], username: str) -> str:
        from backend.app.core.auth import create_access_token, get_password_hash
        from backend.app.models.group import Group
        from backend.app.models.settings import Settings
        from backend.app.models.user import User

        db_session.add(Settings(key="auth_enabled", value="true"))
        group = Group(name=f"{username}-group", permissions=permissions)
        # Membership set on the TRANSIENT user: appending to a loaded collection would lazy-load.
        db_session.add(
            User(username=username, password_hash=get_password_hash("testpass123"), role="user", groups=[group])
        )
        await db_session.commit()
        return create_access_token(data={"sub": username})

    async def test_printers_read_alone_is_forbidden(self, async_client: AsyncClient, db_session):
        token = await self._token_for(db_session, permissions=["printers:read"], username="printers-only")
        response = await async_client.get(_URL, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 403

    async def test_stats_read_is_served(self, async_client: AsyncClient, db_session):
        token = await self._token_for(db_session, permissions=["stats:read"], username="stats-reader")
        response = await async_client.get(_URL, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200

    async def test_no_credentials_is_unauthorized(self, async_client: AsyncClient, db_session):
        await self._token_for(db_session, permissions=["stats:read"], username="someone")
        assert (await async_client.get(_URL)).status_code == 401
