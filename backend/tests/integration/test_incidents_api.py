"""``GET /api/v1/incidents`` — the equipment-fault ledger's query surface (2026-09-11).

The 2026-09-11 audit answered "how many holds did the farm end by itself" (9 of 122)
with a read-only SELECT over the farm's SSH lane and a day of log reading. The
derivation is now ONE table (``printer_incidents.outcome_of``) and this route is how
the number is read back. These pins seed rows through the store's own writers, so
the shapes are the ones production produces.
"""

from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient

from backend.app.models.printer_incident import (
    KIND_JAM,
    KIND_PHYSICAL,
    KIND_RUNOUT,
    RESOLVE_DRIVER_SWAP,
    RESOLVE_OBSERVED_RUNNING,
    STATUS_ESCALATED,
    STATUS_RESOLVED,
)
from backend.app.services import printer_incidents

pytestmark = pytest.mark.asyncio


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


class TestListIncidents:
    async def test_returns_the_rows_newest_first_with_derived_fields(
        self, async_client: AsyncClient, printer_factory, db_session
    ):
        printer = await printer_factory()
        swapped = await _seed(db_session, printer.id, close=RESOLVE_DRIVER_SWAP)
        held = await _seed(db_session, printer.id, kind=KIND_PHYSICAL, code="0700_8004", status=STATUS_ESCALATED)

        response = await async_client.get("/api/v1/incidents")

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

        body = (await async_client.get("/api/v1/incidents")).json()

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

        by_kind = (await async_client.get("/api/v1/incidents", params={"kind": KIND_JAM})).json()
        assert [row["printer_id"] for row in by_kind["items"]] == [a.id]

        by_printer = (
            await async_client.get(
                "/api/v1/incidents",
                params={"printer_id": b.id, "since": (datetime.utcnow() - timedelta(days=60)).isoformat()},
            )
        ).json()
        assert [row["id"] for row in by_printer["items"]] == [old.id]

        default_window = (await async_client.get("/api/v1/incidents")).json()
        assert old.id not in [row["id"] for row in default_window["items"]]  # 30-day default

    async def test_an_unknown_kind_is_refused(self, async_client: AsyncClient):
        response = await async_client.get("/api/v1/incidents", params={"kind": "gremlin"})
        assert response.status_code == 422

    async def test_an_empty_window_still_answers(self, async_client: AsyncClient):
        body = (await async_client.get("/api/v1/incidents")).json()
        assert body["items"] == []
        assert body["summary"]["total"] == 0
        assert body["summary"]["zero_human"] == 0
