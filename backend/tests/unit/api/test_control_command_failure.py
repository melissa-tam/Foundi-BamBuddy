"""A dropped printer command surfaces as HTTP 502, never a silent success (B2).

The printers.py control endpoints and the webhook stop endpoint convert a control
wrapper's ``False`` (MQTT session gone / publish rejected) into a 502 with a clear
detail. The webhook stop path additionally used to ``await`` a synchronous bool —
that live TypeError bug is fixed here too.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

import backend.app.api.routes.webhook as webhook_mod
from backend.app.api.routes.printers import pause_print, stop_print
from backend.app.models.printer import Printer
from backend.app.services.printer_manager import printer_manager

pytestmark = pytest.mark.asyncio


async def _mk_printer(db):
    p = Printer(name="CC", serial_number="SCC", ip_address="1.2.3.4", access_code="x", model="H2S")
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


class TestPrintersRoute502:
    async def test_stop_returns_502_when_command_not_delivered(self, db_session):
        p = await _mk_printer(db_session)
        fake_client = MagicMock()
        fake_client.stop_print.return_value = False
        with (
            patch.object(printer_manager, "get_client", MagicMock(return_value=fake_client)),
            pytest.raises(HTTPException) as ei,
        ):
            await stop_print(p.id, db=db_session)
        assert ei.value.status_code == 502
        assert "not delivered" in ei.value.detail

    async def test_pause_returns_502_when_command_not_delivered(self, db_session):
        p = await _mk_printer(db_session)
        fake_client = MagicMock()
        fake_client.pause_print.return_value = False
        with (
            patch.object(printer_manager, "get_client", MagicMock(return_value=fake_client)),
            pytest.raises(HTTPException) as ei,
        ):
            await pause_print(p.id, db=db_session)
        assert ei.value.status_code == 502
        assert "not delivered" in ei.value.detail


class TestWebhookStop502:
    async def test_webhook_stop_502_on_dropped_command(self):
        """stop_print is synchronous and returns a bool — it must NOT be awaited, and
        a False must fail loudly with 502 instead of returning 200 'Print stopped'."""
        connected_running = SimpleNamespace(connected=True, state="RUNNING")
        with (
            patch.object(webhook_mod, "check_permission", MagicMock()),
            patch.object(webhook_mod, "check_printer_access", MagicMock()),
            patch.object(printer_manager, "get_status", MagicMock(return_value=connected_running)),
            patch.object(printer_manager, "stop_print", MagicMock(return_value=False)),
            pytest.raises(HTTPException) as ei,
        ):
            await webhook_mod.webhook_stop_print(printer_id=1, api_key=MagicMock())
        assert ei.value.status_code == 502
        assert "not delivered" in ei.value.detail

    async def test_webhook_stop_ok_on_delivered_command(self):
        connected_running = SimpleNamespace(connected=True, state="RUNNING")
        with (
            patch.object(webhook_mod, "check_permission", MagicMock()),
            patch.object(webhook_mod, "check_printer_access", MagicMock()),
            patch.object(printer_manager, "get_status", MagicMock(return_value=connected_running)),
            patch.object(printer_manager, "stop_print", MagicMock(return_value=True)),
        ):
            result = await webhook_mod.webhook_stop_print(printer_id=1, api_key=MagicMock())
        assert result == {"message": "Print stopped"}
