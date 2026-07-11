"""Dispatch-time failures must enter farm policy (Phase 1, R5).

`_start_print`'s pre-print failure sites route through
`PrintScheduler._fail_queue_item`, which marks the item terminally failed AND
calls `farm_policy.on_terminal(..., "failed")` so a farm unit that never got to
print still gets a retry / quarantine contribution instead of silently dying.
Non-farm items early-return inside `on_terminal`, so the standard queue is
unaffected. FK enforcement is off in the test engine, so rows may reference
arbitrary ids without seeding parents.
"""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from backend.app.models.eject_profile import EjectProfile
from backend.app.models.library import LibraryFile
from backend.app.models.print_batch import PrintBatch
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.sku import Sku, SkuFile
from backend.app.services.print_scheduler import scheduler

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
