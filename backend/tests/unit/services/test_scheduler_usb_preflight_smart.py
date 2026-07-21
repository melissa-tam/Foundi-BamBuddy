"""Smart USB pre-flight (latency Phase A): fresh-report short-circuit + event wait.

The fixed 2.5 s settle sleep is gone. If a full report landed within the fresh
window the cached ``sdcard`` flag is trusted (no request, no wait); otherwise the
scheduler requests a fresh report and waits on the client's full-report Event up
to the max-wait cap, then reads the flag. Fail-open semantics are unchanged: only
an explicit ``sdcard is False`` holds the item.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.settings import Settings
from backend.app.services import print_scheduler as ps_module
from backend.app.services.print_scheduler import scheduler
from backend.app.services.printer_manager import printer_manager

pytestmark = pytest.mark.asyncio


async def _mk_printer(db, name="PF"):
    p = Printer(name=name, serial_number=f"S{name}", ip_address="1.2.3.4", access_code="x", model="H2S")
    db.add(p)
    await db.flush()
    return p


class _FakeClient:
    """Minimal stand-in exposing the full-report event accessor the pre-flight uses."""

    def __init__(self):
        self.event = asyncio.Event()
        self.armed = 0

    def arm_full_report_wait(self):
        self.armed += 1
        self.event.clear()
        return self.event


@contextlib.contextmanager
def _env(*, status, client=None, on_request=None):
    """Patch the printer_manager surface + downstream sinks the pre-flight/dispatch touch.

    ``on_request`` (optional) is invoked by the patched request_status_update so a
    test can simulate a report landing (e.g. set the client event) synchronously.
    """
    req = MagicMock(return_value=True)
    if on_request is not None:
        req.side_effect = lambda *_a, **_k: (on_request(), True)[1]
    notif = AsyncMock()
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(printer_manager, "is_connected", return_value=True))
        stack.enter_context(patch.object(printer_manager, "request_status_update", req))
        stack.enter_context(patch.object(printer_manager, "get_status", MagicMock(return_value=status)))
        stack.enter_context(patch.object(printer_manager, "get_client", MagicMock(return_value=client)))
        stack.enter_context(patch.object(ps_module.notification_service, "on_queue_job_waiting", notif))
        stack.enter_context(patch.object(scheduler, "_power_off_if_needed", AsyncMock()))
        yield SimpleNamespace(request=req, notif=notif, client=client)


class TestFreshShortCircuit:
    async def test_fresh_report_skips_request_and_wait(self, db_session):
        printer = await _mk_printer(db_session, "FRESH")
        item = PrintQueueItem(printer_id=printer.id, status="pending", plate_id=1, position=1)
        db_session.add(item)
        await db_session.commit()

        # A full report landed just now → within the default 10 s window.
        status = SimpleNamespace(sdcard=True, last_full_report_at=time.monotonic())
        client = _FakeClient()
        with _env(status=status, client=client) as m:
            await scheduler._start_print(db_session, item)

        m.request.assert_not_called()  # cached flag trusted — no pushall
        assert client.armed == 0  # event never armed
        await db_session.refresh(item)
        # Proceeded past the USB gate to the downstream no-source failure.
        assert item.status == "failed"
        assert item.error_message == "No source file specified"


class TestStalePath:
    async def test_proceeds_when_event_fires_early(self, db_session):
        printer = await _mk_printer(db_session, "EARLY")
        item = PrintQueueItem(printer_id=printer.id, status="pending", plate_id=1, position=1)
        db_session.add(item)
        await db_session.commit()

        client = _FakeClient()
        # Stale status (no last_full_report_at) → request path; the request "lands"
        # a report by setting the event immediately, so the wait returns at once.
        status = SimpleNamespace(sdcard=True)
        with _env(status=status, client=client, on_request=client.event.set) as m:
            await scheduler._start_print(db_session, item)

        m.request.assert_called_once_with(printer.id)
        assert client.armed == 1
        await db_session.refresh(item)
        assert item.status == "failed"  # proceeded (sdcard True)
        assert item.error_message == "No source file specified"

    async def test_timeout_cap_proceeds_fail_open(self, db_session):
        printer = await _mk_printer(db_session, "TIMEOUT")
        item = PrintQueueItem(printer_id=printer.id, status="pending", plate_id=1, position=1)
        db_session.add(item)
        # Tiny max-wait so the never-firing event times out fast.
        db_session.add(Settings(key="usb_preflight_max_wait_seconds", value="0.05"))
        await db_session.commit()

        client = _FakeClient()  # event never set
        status = SimpleNamespace(sdcard=None)  # unknown → fail-open
        with _env(status=status, client=client) as m:
            await scheduler._start_print(db_session, item)

        m.request.assert_called_once_with(printer.id)
        await db_session.refresh(item)
        assert item.waiting_reason != "no_usb_drive"  # fail-open, not held
        assert item.status == "failed"

    async def test_explicit_false_still_holds(self, db_session):
        printer = await _mk_printer(db_session, "HOLD")
        item = PrintQueueItem(printer_id=printer.id, status="pending", plate_id=1, position=1)
        db_session.add(item)
        db_session.add(Settings(key="usb_preflight_max_wait_seconds", value="0.05"))
        await db_session.commit()

        client = _FakeClient()
        # Report lands (event set) but confirms the drive is ABSENT.
        status = SimpleNamespace(sdcard=False)
        with _env(status=status, client=client, on_request=client.event.set) as m:
            await scheduler._start_print(db_session, item)

        m.request.assert_called_once_with(printer.id)
        m.notif.assert_awaited_once()
        await db_session.refresh(item)
        assert item.waiting_reason == "no_usb_drive"
        assert item.status == "pending"  # a WAIT, not a failure
