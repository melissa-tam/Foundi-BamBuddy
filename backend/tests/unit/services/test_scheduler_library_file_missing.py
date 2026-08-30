"""A queued item whose source file is gone from disk WAITS — it never fails.

2026-08-14: a farm bug deleted a library file out from under a production run. Every
one of its 22 pending units then died server-side in ``_start_print`` — the archive
copy raised FileNotFoundError, which was handled as a genuine print failure — and
each insta-fail counted toward the printer's consecutive-failure streak, so two in a
row quarantined a perfectly healthy printer. "Recover & resume" re-quarantined within
seconds. The whole fleet had shut itself down by morning.

A missing source file says nothing about the printer, and failing the item neither
restores the file nor stops the next unit trying. It is a dispatch PRECONDITION, held
exactly like the USB pre-flight: the item stays pending, nothing terminal is written,
and restoring the file self-clears the hold on the next pass.
"""

from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.models  # noqa: F401 - populate Base.metadata
import backend.app.services.print_scheduler as scheduler_module
from backend.app.core.database import Base
from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services.plate_occupancy import plate_occupancy
from backend.app.services.print_scheduler import WAITING_REASON_LIBRARY_FILE_MISSING, PrintScheduler


@pytest.fixture(autouse=True)
def _clean_authority():
    """A dispatch that reaches the print command claims a printer LEASE on the
    process-wide occupancy authority; a HELD one never gets that far. Start clean so
    neither leaks into the next case."""
    plate_occupancy.reset_for_tests()
    yield
    plate_occupancy.reset_for_tests()


@pytest.fixture
async def case_factory(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    counter = 0

    async def make_case(*, file_on_disk: bool, target_model: str | None = None):
        nonlocal counter
        counter += 1

        base_dir = tmp_path / f"case-{counter}"
        base_dir.mkdir()
        source_path = base_dir / "library" / f"source-{counter}.3mf"
        source_path.parent.mkdir()
        if file_on_disk:
            source_path.write_bytes(b"library source")

        async with session_maker() as db:
            printer = Printer(
                name=f"Printer {counter}",
                serial_number=f"SERIAL-{counter}",
                ip_address="127.0.0.1",
                access_code="access-code",
                model="H2S",
            )
            library_file = LibraryFile(
                filename=f"source-{counter}.3mf",
                file_path=str(source_path),
                file_type="3mf",
                file_size=14,
                file_hash=None,
                thumbnail_path=None,
                file_metadata=None,
            )
            db.add_all([printer, library_file])
            await db.flush()

            item = PrintQueueItem(
                printer_id=printer.id,
                library_file_id=library_file.id,
                status="pending",
                target_model=target_model,
                bed_levelling=True,
                flow_cali=False,
                vibration_cali=True,
                layer_inspect=False,
                timelapse=False,
                use_ams=True,
                nozzle_offset_cali=True,
            )
            db.add(item)
            await db.commit()

            return SimpleNamespace(
                session_maker=session_maker,
                base_dir=base_dir,
                source_path=source_path,
                printer_id=printer.id,
                library_file_id=library_file.id,
                queue_item_id=item.id,
                upload=AsyncMock(return_value=True),
                start_print=MagicMock(return_value=True),
                waiting_notify=AsyncMock(),
                on_terminal=AsyncMock(),
            )

    try:
        yield make_case
    finally:
        await engine.dispose()


async def _dispatch(ctx):
    """Run one ``_start_print`` pass against a fresh session, as the tick does."""
    scheduler = PrintScheduler()

    async def archive_print(
        self, *, printer_id, source_file, original_filename, created_by_id=None, project_id=None, plate_id=None
    ):
        archive_rel_path = Path("archives") / f"archive-{ctx.queue_item_id}.3mf"
        archive_path = ctx.base_dir / archive_rel_path
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        archive_path.write_bytes(Path(source_file).read_bytes())
        archive = PrintArchive(
            printer_id=printer_id,
            filename=original_filename,
            file_path=str(archive_rel_path),
            file_size=archive_path.stat().st_size,
            content_hash=None,
            thumbnail_path=None,
            timelapse_path=None,
            print_time_seconds=120,
            status="completed",
            project_id=project_id,
            created_by_id=created_by_id,
        )
        self.db.add(archive)
        await self.db.flush()
        return archive

    patches = [
        patch.object(scheduler_module.settings, "base_dir", ctx.base_dir),
        patch("backend.app.services.archive.ArchiveService.archive_print", new=archive_print),
        patch("backend.app.services.print_scheduler.printer_manager.is_connected", MagicMock(return_value=True)),
        patch("backend.app.services.print_scheduler.printer_manager.get_status", MagicMock(return_value=None)),
        patch("backend.app.services.print_scheduler.printer_manager.start_print", ctx.start_print),
        # The dispatch path's unconditional plate write is gone (2026-08-30): there is
        # nothing to stub, and a HOLD writes nothing to the authority either.
        patch(
            "backend.app.services.print_scheduler.get_ftp_retry_settings", AsyncMock(return_value=(False, 0, 0, 1.0))
        ),
        patch("backend.app.services.print_scheduler.delete_file_async", AsyncMock(return_value=True)),
        patch("backend.app.services.print_scheduler.upload_file_async", ctx.upload),
        patch("backend.app.services.print_scheduler.cache_3mf_download", MagicMock()),
        patch("backend.app.services.print_scheduler.spawn_background_task", MagicMock()),
        patch(
            "backend.app.services.notification_service.notification_service.on_queue_job_waiting", ctx.waiting_notify
        ),
        patch("backend.app.services.notification_service.notification_service.on_queue_job_started", AsyncMock()),
        patch("backend.app.services.notification_service.notification_service.on_queue_job_failed", AsyncMock()),
        patch("backend.app.services.mqtt_relay.mqtt_relay.on_queue_job_started", AsyncMock()),
        # The one hook that both retries a unit and counts it toward quarantine.
        patch("backend.app.services.farm_policy.on_terminal", ctx.on_terminal),
        patch.object(scheduler, "_propagate_owner_to_printer_manager", AsyncMock()),
        patch.object(scheduler, "_power_off_if_needed", AsyncMock()),
    ]

    with ExitStack() as stack:
        for patcher in patches:
            stack.enter_context(patcher)
        async with ctx.session_maker() as db:
            item = await db.get(PrintQueueItem, ctx.queue_item_id)
            await scheduler._start_print(db, item)
    return scheduler


async def _snapshot(ctx):
    async with ctx.session_maker() as db:
        item = await db.get(PrintQueueItem, ctx.queue_item_id)
        rows = (await db.execute(select(PrintQueueItem))).scalars().all()
        printer = await db.get(Printer, ctx.printer_id)
        return item, rows, printer


@pytest.mark.asyncio
async def test_missing_library_file_holds_the_item_pending(case_factory):
    ctx = await case_factory(file_on_disk=False)

    await _dispatch(ctx)

    item, rows, printer = await _snapshot(ctx)
    assert item.status == "pending"
    assert item.waiting_reason == WAITING_REASON_LIBRARY_FILE_MISSING
    # Nothing terminal was written — this is the whole point.
    assert item.completed_at is None
    assert item.error_message is None
    # No retry row minted, and the failure-policy hook (retry + quarantine counter)
    # never ran, so the streak cannot have advanced.
    assert len(rows) == 1
    assert item.retry_count == 0
    ctx.on_terminal.assert_not_awaited()
    assert printer.quarantined is False
    # Held BEFORE any I/O: nothing was uploaded and no print was commanded.
    ctx.upload.assert_not_awaited()
    ctx.start_print.assert_not_called()
    ctx.waiting_notify.assert_awaited_once()
    # And nothing was claimed: the hold sits far above the point of no return, so
    # the printer keeps neither a dispatch lease nor a plate write from this pass.
    assert plate_occupancy.snapshot(ctx.printer_id).lease_unit_id is None
    assert plate_occupancy.is_plate_occupied(ctx.printer_id) is False


@pytest.mark.asyncio
async def test_hold_notifies_once_across_repeated_ticks(case_factory):
    """A file missing across many ticks is one hold, not one alert per tick."""
    ctx = await case_factory(file_on_disk=False)

    await _dispatch(ctx)
    await _dispatch(ctx)
    await _dispatch(ctx)

    item, _rows, _printer = await _snapshot(ctx)
    assert item.status == "pending"
    assert item.waiting_reason == WAITING_REASON_LIBRARY_FILE_MISSING
    ctx.waiting_notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_restoring_the_file_dispatches_on_the_next_pass_and_clears_the_reason(case_factory):
    """Self-clearing: the hold releases with no operator action beyond the fix."""
    ctx = await case_factory(file_on_disk=False)

    await _dispatch(ctx)
    item, _rows, _printer = await _snapshot(ctx)
    assert item.waiting_reason == WAITING_REASON_LIBRARY_FILE_MISSING

    ctx.source_path.write_bytes(b"library source")
    await _dispatch(ctx)

    item, rows, _printer = await _snapshot(ctx)
    assert item.status == "printing"
    assert item.waiting_reason is None
    assert item.archive_id is not None
    assert len(rows) == 1
    ctx.upload.assert_awaited_once()
    ctx.start_print.assert_called_once()


@pytest.mark.asyncio
async def test_model_targeted_item_unpins_so_the_fleet_is_re_searched(case_factory):
    """A model-targeted unit releases the pin the scheduler made this tick.

    Same rule as the USB hold: the pin was the model path's choice, not a human's,
    so a printer that cannot serve this unit must not become its permanent home.
    """
    ctx = await case_factory(file_on_disk=False, target_model="H2S")

    await _dispatch(ctx)

    item, _rows, _printer = await _snapshot(ctx)
    assert item.status == "pending"
    assert item.waiting_reason == WAITING_REASON_LIBRARY_FILE_MISSING
    assert item.printer_id is None


@pytest.mark.asyncio
async def test_user_pinned_item_keeps_its_printer(case_factory):
    """No ``target_model`` means a human chose the printer — the pin survives."""
    ctx = await case_factory(file_on_disk=False)

    await _dispatch(ctx)

    item, _rows, _printer = await _snapshot(ctx)
    assert item.printer_id == ctx.printer_id
