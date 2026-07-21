"""Kick hooks fire on the dispatch-producing routes/services (latency Phase A).

Route-level: enqueue (POST /queue) and manual start (POST /queue/{id}/start).
Service-level: farm_staging.release_filament_staged kicks once at released>0.
Settings whitelist: the 4 new tunables round-trip with correct types.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def _mk_printer(db, name="KH"):
    from backend.app.models.printer import Printer

    p = Printer(name=name, serial_number=f"S{name}", ip_address="192.168.9.9", access_code="12345678", model="X1C")
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


async def _mk_archive(db, name="kh"):
    from backend.app.models.archive import PrintArchive

    a = PrintArchive(
        filename=f"{name}.3mf",
        print_name=name,
        file_path=f"/tmp/{name}.3mf",
        file_size=1024,
        content_hash=f"hash_{name}",
        status="completed",
    )
    db.add(a)
    await db.commit()
    await db.refresh(a)
    return a


class TestRouteHooks:
    async def test_enqueue_fires_kick(self, async_client: AsyncClient, db_session):
        printer = await _mk_printer(db_session, "ENQ")
        archive = await _mk_archive(db_session, "enq")
        with patch("backend.app.services.dispatch_kick.dispatch_kick") as mock_dk:
            resp = await async_client.post("/api/v1/queue/", json={"archive_id": archive.id, "printer_id": printer.id})
        assert resp.status_code == 200
        mock_dk.kick.assert_called_once_with("enqueue", printer.id)

    async def test_manual_start_fires_kick(self, async_client: AsyncClient, db_session):
        from backend.app.models.print_queue import PrintQueueItem

        printer = await _mk_printer(db_session, "MAN")
        archive = await _mk_archive(db_session, "man")
        item = PrintQueueItem(
            printer_id=printer.id, archive_id=archive.id, status="pending", manual_start=True, plate_id=1, position=1
        )
        db_session.add(item)
        await db_session.commit()
        await db_session.refresh(item)

        with patch("backend.app.services.dispatch_kick.dispatch_kick") as mock_dk:
            resp = await async_client.post(f"/api/v1/queue/{item.id}/start?skip_filament_check=true")
        assert resp.status_code == 200
        mock_dk.kick.assert_called_once_with("manual_start", printer.id)


class TestServiceHook:
    async def test_release_staged_kicks_once_at_release(self, db_session):
        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.services import farm_staging

        printer = await _mk_printer(db_session, "REL")
        item = PrintQueueItem(
            printer_id=printer.id,
            status="pending",
            manual_start=True,
            filament_short=True,
            waiting_reason="filament_short",
            plate_id=1,
            position=1,
        )
        db_session.add(item)
        await db_session.commit()

        with (
            patch.object(farm_staging, "compute_deficit_for_queue_item", new=AsyncMock(return_value=[])),
            patch.object(farm_staging.spool_selection, "start_rule_blocks_item", new=AsyncMock(return_value=False)),
            patch("backend.app.services.dispatch_kick.dispatch_kick") as mock_dk,
        ):
            released = await farm_staging.release_filament_staged(db_session, printer.id)

        assert released == 1
        mock_dk.kick.assert_called_once_with("release_staged", printer.id)


class TestSettingsWhitelistRoundTrip:
    async def test_new_tunables_persist_with_correct_types(self, async_client: AsyncClient):
        payload = {
            "queue_check_interval_seconds": 45,
            "dispatch_kick_debounce_seconds": 2.5,
            "usb_preflight_fresh_window_seconds": 20,
            "usb_preflight_max_wait_seconds": 3.5,
        }
        put = await async_client.put("/api/v1/settings/", json=payload)
        assert put.status_code == 200

        got = await async_client.get("/api/v1/settings/")
        assert got.status_code == 200
        body = got.json()
        assert body["queue_check_interval_seconds"] == 45
        assert body["dispatch_kick_debounce_seconds"] == 2.5
        assert body["usb_preflight_fresh_window_seconds"] == 20
        assert body["usb_preflight_max_wait_seconds"] == 3.5
        # Correct coercion, not stringified.
        assert isinstance(body["queue_check_interval_seconds"], int)
        assert isinstance(body["usb_preflight_max_wait_seconds"], float)
