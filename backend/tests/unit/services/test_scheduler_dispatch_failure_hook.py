"""Dispatch-time failures must enter farm policy (Phase 1, R5).

`_start_print`'s pre-print failure sites route through
`PrintScheduler._fail_queue_item`, which marks the item terminally failed AND
calls `farm_policy.on_terminal(..., "failed")` so a farm unit that never got to
print still gets a retry / quarantine contribution instead of silently dying.
Non-farm items early-return inside `on_terminal`, so the standard queue is
unaffected. FK enforcement is off in the test engine, so rows may reference
arbitrary ids without seeding parents.
"""

import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from backend.app.models.eject_profile import EjectProfile
from backend.app.models.library import LibraryFile
from backend.app.models.print_batch import PrintBatch
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.sku import Sku, SkuFile
from backend.app.services import print_scheduler as ps_module
from backend.app.services.print_scheduler import scheduler
from backend.app.services.printer_manager import printer_manager

pytestmark = pytest.mark.asyncio


async def _mk_printer(db, name="DF"):
    p = Printer(name=name, serial_number=f"S{name}", ip_address="1.2.3.4", access_code="x", model="H2S")
    db.add(p)
    await db.flush()
    return p


async def _mk_farm_batch(db, *, retry_max=1):
    """A farm run (batch with sku_file_id) + its eject profile."""
    lib = LibraryFile(
        filename="f.gcode.3mf", file_path="/tmp/f.gcode.3mf", file_type="gcode.3mf", file_size=1, is_external=True
    )
    db.add(lib)
    await db.flush()
    sku = Sku(code=f"SKU{lib.id:03d}", name="Widget")
    db.add(sku)
    await db.flush()
    sf = SkuFile(sku_id=sku.id, library_file_id=lib.id, plate_index=1, units_per_plate=1)
    db.add(sf)
    await db.flush()
    prof = EjectProfile(name=f"ep{lib.id}")
    db.add(prof)
    await db.flush()
    batch = PrintBatch(
        name="run",
        quantity=1,
        status="active",
        sku_file_id=sf.id,
        target_units=1,
        require_first_article=False,
        retry_max_per_unit=retry_max,
        escalate_consecutive_failures=2,
    )
    db.add(batch)
    await db.flush()
    return batch, prof


class TestFailQueueItemHook:
    async def test_sets_terminal_fields_and_calls_on_terminal_once(self, db_session):
        item = PrintQueueItem(printer_id=5, status="printing", plate_id=1, position=1)
        db_session.add(item)
        await db_session.commit()

        with patch("backend.app.services.farm_policy.on_terminal", new_callable=AsyncMock) as mock_ot:
            await scheduler._fail_queue_item(db_session, item, "boom")
            mock_ot.assert_awaited_once_with(db_session, 5, item.id, "failed")

        await db_session.refresh(item)
        assert item.status == "failed"
        assert item.error_message == "boom"
        assert item.completed_at is not None

    async def test_dispatch_failure_on_farm_item_mints_retry(self, db_session):
        printer = await _mk_printer(db_session, "DF1")
        batch, prof = await _mk_farm_batch(db_session, retry_max=1)
        item = PrintQueueItem(
            batch_id=batch.id,
            printer_id=printer.id,
            status="printing",
            eject_profile_id=prof.id,
            plate_id=1,
            position=1,
            retry_count=0,
        )
        db_session.add(item)
        await db_session.commit()

        # Real farm_policy.on_terminal — end-to-end retry minting.
        await scheduler._fail_queue_item(db_session, item, "Failed to upload file to printer")

        r = await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.retry_of_id == item.id))
        retries = list(r.scalars().all())
        assert len(retries) == 1
        assert retries[0].status == "pending"
        assert retries[0].retry_count == 1
        assert retries[0].printer_id == printer.id  # printer-pinned run keeps the pin

    async def test_clears_stale_waiting_reason_on_failure(self, db_session):
        """W4b: a dispatch-time failure NULLs a stale hold token in the SAME update
        that sets the terminal status (on_terminal mocked to isolate this clear)."""
        item = PrintQueueItem(printer_id=5, status="printing", plate_id=1, position=1, waiting_reason="no_usb_drive")
        db_session.add(item)
        await db_session.commit()

        with patch("backend.app.services.farm_policy.on_terminal", new_callable=AsyncMock):
            await scheduler._fail_queue_item(db_session, item, "boom")

        await db_session.refresh(item)
        assert item.status == "failed"
        assert item.waiting_reason is None

    async def test_dispatch_failure_on_non_farm_item_is_noop(self, db_session):
        item = PrintQueueItem(batch_id=None, printer_id=5, status="printing", plate_id=1, position=1)
        db_session.add(item)
        await db_session.commit()

        # Must not raise, and must not mint a retry (non-farm item).
        await scheduler._fail_queue_item(db_session, item, "boom")

        await db_session.refresh(item)
        assert item.status == "failed"
        r = await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.retry_of_id == item.id))
        assert r.scalars().first() is None


@contextlib.contextmanager
def _usb_preflight_env(*, status):
    """Patch the printer_manager surface + FTP / notification / power sinks the
    USB pre-flight and its downstream dispatch touch.

    ``status`` is the object ``printer_manager.get_status`` returns (or ``None``
    to simulate 'no live status at all'). ``get_client`` is stubbed to ``None`` so
    the smart pre-flight takes the no-client path (request, no event wait) and the
    test doesn't sleep. Yields the mocks for assertions.
    """
    req = MagicMock(return_value=True)
    notif = AsyncMock()
    upload = AsyncMock(return_value=True)
    ftp_retry = AsyncMock(return_value=True)
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(printer_manager, "is_connected", return_value=True))
        stack.enter_context(patch.object(printer_manager, "request_status_update", req))
        stack.enter_context(patch.object(printer_manager, "get_status", MagicMock(return_value=status)))
        stack.enter_context(patch.object(printer_manager, "get_client", MagicMock(return_value=None)))
        stack.enter_context(patch.object(ps_module.notification_service, "on_queue_job_waiting", notif))
        stack.enter_context(patch.object(ps_module, "upload_file_async", upload))
        stack.enter_context(patch.object(ps_module, "with_ftp_retry", ftp_retry))
        stack.enter_context(patch.object(scheduler, "_power_off_if_needed", AsyncMock()))
        yield SimpleNamespace(request_status_update=req, notif=notif, upload=upload, ftp_retry=ftp_retry)


class TestUsbPreflight:
    """Dispatch-time USB pre-flight (fail-open, self-clearing).

    The H2 fleet needs a USB stick for LAN dispatch (no usable internal
    storage); the firmware reports USB presence (``state.sdcard``) only in a
    full status report. Before dispatching, ``_start_print`` requests a fresh
    report and — ONLY if the drive is confirmed absent (``sdcard`` is
    explicitly False) — holds the item pending with ``waiting_reason =
    "no_usb_drive"`` instead of letting the upload fail with an opaque FTPS 553.
    """

    async def test_no_usb_holds_dispatch_without_uploading(self, db_session):
        printer = await _mk_printer(db_session, "USB1")
        item = PrintQueueItem(printer_id=printer.id, status="pending", plate_id=1, position=1)
        db_session.add(item)
        await db_session.commit()

        with _usb_preflight_env(status=SimpleNamespace(sdcard=False)) as m:
            await scheduler._start_print(db_session, item)

        await db_session.refresh(item)
        assert item.waiting_reason == "no_usb_drive"
        assert item.status == "pending"  # a WAIT, not a failure — no retry burn
        m.request_status_update.assert_called_once_with(printer.id)
        m.notif.assert_awaited_once()
        assert m.notif.await_args.kwargs["waiting_reason"] == "no_usb_drive"
        m.upload.assert_not_awaited()  # no dispatch attempted
        m.ftp_retry.assert_not_awaited()

    async def test_notifies_once_across_repeated_ticks(self, db_session):
        printer = await _mk_printer(db_session, "USB2")
        item = PrintQueueItem(printer_id=printer.id, status="pending", plate_id=1, position=1)
        db_session.add(item)
        await db_session.commit()

        with _usb_preflight_env(status=SimpleNamespace(sdcard=False)) as m:
            await scheduler._start_print(db_session, item)
            await scheduler._start_print(db_session, item)

        m.notif.assert_awaited_once()  # deduped on the 2nd tick (already waiting)
        assert m.request_status_update.call_count == 2  # a fresh report every pass
        await db_session.refresh(item)
        assert item.waiting_reason == "no_usb_drive"
        assert item.status == "pending"

    async def test_usb_present_proceeds_and_clears_stale_reason(self, db_session):
        printer = await _mk_printer(db_session, "USB3")
        # Stale hold from a prior tick — the capability gate's existing
        # waiting_reason reset must clear it once dispatch proceeds.
        item = PrintQueueItem(
            printer_id=printer.id, status="pending", plate_id=1, position=1, waiting_reason="no_usb_drive"
        )
        db_session.add(item)
        await db_session.commit()

        with _usb_preflight_env(status=SimpleNamespace(sdcard=True)) as m:
            await scheduler._start_print(db_session, item)

        m.request_status_update.assert_called_once_with(printer.id)
        m.notif.assert_not_awaited()  # not held → no waiting notification
        await db_session.refresh(item)
        assert item.waiting_reason is None  # self-cleared via the existing pattern
        # Proceeded past the USB gate: with no source file the item reaches the
        # downstream "no source" failure — proof it did NOT hold on USB.
        assert item.status == "failed"
        assert item.error_message == "No source file specified"

    async def test_status_missing_fails_open(self, db_session):
        printer = await _mk_printer(db_session, "USB4")
        item = PrintQueueItem(printer_id=printer.id, status="pending", plate_id=1, position=1)
        db_session.add(item)
        await db_session.commit()

        # No live status object at all → fail-open (proceed).
        with _usb_preflight_env(status=None) as m:
            await scheduler._start_print(db_session, item)

        m.notif.assert_not_awaited()
        await db_session.refresh(item)
        assert item.waiting_reason != "no_usb_drive"
        assert item.status == "failed"  # proceeded to downstream no-source failure

    async def test_sdcard_none_fails_open(self, db_session):
        printer = await _mk_printer(db_session, "USB5")
        item = PrintQueueItem(printer_id=printer.id, status="pending", plate_id=1, position=1)
        db_session.add(item)
        await db_session.commit()

        # Live status present but sdcard flag not reported (None) → fail-open.
        with _usb_preflight_env(status=SimpleNamespace(sdcard=None)) as m:
            await scheduler._start_print(db_session, item)

        m.notif.assert_not_awaited()
        await db_session.refresh(item)
        assert item.waiting_reason != "no_usb_drive"
        assert item.status == "failed"

    async def test_self_clears_false_then_true(self, db_session):
        printer = await _mk_printer(db_session, "USB6")
        item = PrintQueueItem(printer_id=printer.id, status="pending", plate_id=1, position=1)
        db_session.add(item)
        await db_session.commit()

        # Tick 1 — USB absent → held.
        with _usb_preflight_env(status=SimpleNamespace(sdcard=False)) as m1:
            await scheduler._start_print(db_session, item)
        await db_session.refresh(item)
        assert item.waiting_reason == "no_usb_drive"
        assert item.status == "pending"
        m1.notif.assert_awaited_once()

        # Tick 2 — USB restored → dispatch proceeds, reason self-clears.
        with _usb_preflight_env(status=SimpleNamespace(sdcard=True)) as m2:
            await scheduler._start_print(db_session, item)
        await db_session.refresh(item)
        assert item.waiting_reason is None
        assert item.status == "failed"  # proceeded past the USB gate
        m2.notif.assert_not_awaited()
        m2.request_status_update.assert_called_once_with(printer.id)
