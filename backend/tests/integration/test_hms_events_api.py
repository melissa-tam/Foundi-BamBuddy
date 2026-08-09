"""Integration tests for the HMS vocabulary read API (WS6).

``GET /api/v1/printers/{id}/hms-events`` is the audit surface over ``hms_event``: the
codes a printer has actually emitted, newest sighting first, including the ones no
catalog describes (which is the whole reason the table exists).
"""

from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient

# Two production codes with NO catalog description, plus one the catalog knows.
_UNKNOWN = ("0500000000030051", 0x05000000, 0x00030051, "0500_0051")
_KNOWN = ("0700200000024025", 0x07002000, 0x00024025, "0700_4025")


@pytest.fixture
async def event_factory(db_session):
    """Insert hms_event rows with explicit stamps (ordering is under test)."""
    from backend.app.models.hms_event import HMSEvent

    base = datetime(2026, 8, 9, 12, 0, 0)

    async def _create(printer_id: int, full_code: str, attr: int, code: int, *, minutes_ago: int = 0, count: int = 1):
        row = HMSEvent(
            printer_id=printer_id,
            full_code=full_code,
            attr=attr,
            code=code,
            severity=3,
            first_seen=base - timedelta(days=1),
            last_seen=base - timedelta(minutes=minutes_ago),
            count=count,
        )
        db_session.add(row)
        await db_session.commit()
        return row

    return _create


@pytest.mark.asyncio
@pytest.mark.integration
class TestHmsEventsRead:
    async def test_empty_for_a_printer_that_has_said_nothing(self, async_client: AsyncClient, printer_factory):
        printer = await printer_factory()
        resp = await async_client.get(f"/api/v1/printers/{printer.id}/hms-events")
        assert resp.status_code == 200, resp.text
        assert resp.json() == []

    async def test_unknown_printer_404(self, async_client: AsyncClient):
        resp = await async_client.get("/api/v1/printers/424242/hms-events")
        assert resp.status_code == 404

    async def test_row_carries_the_decoded_view_and_the_lossless_code(
        self, async_client: AsyncClient, printer_factory, event_factory
    ):
        """short_code + description are DERIVED at read time; an undescribed code
        answers None there and is identifiable only by its full_code — exactly the
        forensic gap this endpoint closes."""
        printer = await printer_factory()
        await event_factory(printer.id, *_UNKNOWN[:3], count=4)

        resp = await async_client.get(f"/api/v1/printers/{printer.id}/hms-events")
        assert resp.status_code == 200, resp.text
        (row,) = resp.json()
        assert row["full_code"] == _UNKNOWN[0]
        assert row["short_code"] == _UNKNOWN[3]
        assert row["description"] is None
        assert row["attr"] == _UNKNOWN[1]
        assert row["code"] == _UNKNOWN[2]
        assert row["severity"] == 3
        assert row["count"] == 4

    async def test_a_catalogued_code_resolves_its_description(
        self, async_client: AsyncClient, printer_factory, event_factory
    ):
        printer = await printer_factory()
        await event_factory(printer.id, *_KNOWN[:3])

        resp = await async_client.get(f"/api/v1/printers/{printer.id}/hms-events")
        (row,) = resp.json()
        assert row["short_code"] == _KNOWN[3]
        assert row["description"]

    async def test_newest_last_seen_first(self, async_client: AsyncClient, printer_factory, event_factory):
        printer = await printer_factory()
        await event_factory(printer.id, *_UNKNOWN[:3], minutes_ago=60)
        await event_factory(printer.id, *_KNOWN[:3], minutes_ago=5)

        resp = await async_client.get(f"/api/v1/printers/{printer.id}/hms-events")
        assert [r["full_code"] for r in resp.json()] == [_KNOWN[0], _UNKNOWN[0]]

    async def test_paging_walks_the_list_without_overlap(
        self, async_client: AsyncClient, printer_factory, event_factory
    ):
        printer = await printer_factory()
        for minutes in range(5):
            await event_factory(
                printer.id, f"05000000000300{minutes:02d}", 0x05000000, 0x00030000 + minutes, minutes_ago=minutes
            )

        page_1 = await async_client.get(f"/api/v1/printers/{printer.id}/hms-events?limit=2")
        page_2 = await async_client.get(f"/api/v1/printers/{printer.id}/hms-events?limit=2&offset=2")
        assert page_1.status_code == 200 and page_2.status_code == 200
        first = [r["full_code"] for r in page_1.json()]
        second = [r["full_code"] for r in page_2.json()]
        assert len(first) == 2 and len(second) == 2
        assert not set(first) & set(second)
        # Both pages stay on the newest-first ordering.
        assert first + second == ["050000000003000" + str(i) for i in range(4)]

    async def test_only_this_printer_s_codes(self, async_client: AsyncClient, printer_factory, event_factory):
        one = await printer_factory()
        two = await printer_factory()
        await event_factory(one.id, *_UNKNOWN[:3])
        await event_factory(two.id, *_KNOWN[:3])

        resp = await async_client.get(f"/api/v1/printers/{one.id}/hms-events")
        assert [r["full_code"] for r in resp.json()] == [_UNKNOWN[0]]

    @pytest.mark.parametrize("query", ["limit=0", "limit=501", "offset=-1"])
    async def test_page_bounds_are_enforced(self, async_client: AsyncClient, printer_factory, query):
        printer = await printer_factory()
        resp = await async_client.get(f"/api/v1/printers/{printer.id}/hms-events?{query}")
        assert resp.status_code == 422


@pytest.mark.asyncio
@pytest.mark.integration
class TestHmsEventsPermissions:
    """PRINTERS_READ — the same gate the printer status read uses."""

    async def _api_key(self, db_session, *, can_read_status: bool) -> str:
        from backend.app.core.auth import generate_api_key
        from backend.app.models.api_key import APIKey
        from backend.app.models.settings import Settings

        db_session.add(Settings(key="auth_enabled", value="true"))
        full_key, key_hash, key_prefix = generate_api_key()
        db_session.add(
            APIKey(
                name="probe-key",
                key_hash=key_hash,
                key_prefix=key_prefix,
                can_read_status=can_read_status,
                enabled=True,
            )
        )
        await db_session.commit()
        return full_key

    async def test_read_key_allowed(self, async_client: AsyncClient, db_session, printer_factory):
        printer = await printer_factory()
        key = await self._api_key(db_session, can_read_status=True)
        resp = await async_client.get(f"/api/v1/printers/{printer.id}/hms-events", headers={"X-API-Key": key})
        assert resp.status_code == 200, resp.text

    async def test_key_without_status_scope_forbidden(self, async_client: AsyncClient, db_session, printer_factory):
        printer = await printer_factory()
        key = await self._api_key(db_session, can_read_status=False)
        resp = await async_client.get(f"/api/v1/printers/{printer.id}/hms-events", headers={"X-API-Key": key})
        assert resp.status_code == 403

    async def test_unauthenticated_rejected_when_auth_is_on(
        self, async_client: AsyncClient, db_session, printer_factory
    ):
        printer = await printer_factory()
        await self._api_key(db_session, can_read_status=True)  # only to flip auth_enabled
        resp = await async_client.get(f"/api/v1/printers/{printer.id}/hms-events")
        assert resp.status_code == 401
