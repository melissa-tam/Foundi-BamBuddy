"""Drive ``main.on_print_start`` / ``main.on_print_complete`` over the REAL test engine.

Which archive records a print is decided by ``services/print_binding`` in conditional SQL — an
``UPDATE … RETURNING`` taken under the write lock — which a string-routed mock session cannot
model. So every test of what a print start or a terminal does to ARCHIVES runs the callbacks
against the test database and mocks only the transports: notifications, the websocket, the MQTT
relay, smart plugs, the printer's live state, the FTP lanes and the timelapse listing. ONE harness,
so the transport list cannot drift between the files that use it.

Background tasks the terminal spawns (photo, energy, farm policy, notifications) are the caller's
to settle — :func:`drain_new_tasks` cancels whatever a callback left running.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# A farm archive's name: the library STORAGE hash, never what the printer echoes (RC1).
STORAGE_HASH_FILENAME = "1d1054d9c0ffee5eed.3mf"


@dataclass(frozen=True)
class CallbackMocks:
    """The transports a test may assert on."""

    printer_manager: MagicMock
    send_start_notification: AsyncMock
    store_spoolman: AsyncMock
    record_energy: AsyncMock
    locate: AsyncMock
    ws: MagicMock
    notif: MagicMock


def live_state(
    *,
    subtask_id: str | None = None,
    progress: float | None = 40.0,
    state: str = "RUNNING",
    fresh: bool = True,
    subtask_name: str = "Fast_Half_Shell_spliced",
):
    """What ``printer_manager.get_status`` answers: a live print, no AMS payload (so the usage
    tracker's start hook leaves any session a test seeded alone).

    ``fresh`` is whether the state describes THIS MQTT session — its first report applied
    (``report_epoch == connection_epoch``); False models the previous session's cache that
    ``_on_connect`` re-broadcasts before its pushall answers."""
    return SimpleNamespace(
        connected=True,
        connection_epoch=1,
        report_epoch=1 if fresh else None,
        state=state,
        subtask_id=subtask_id,
        subtask_name=subtask_name,
        progress=progress,
        layer_num=5,
        raw_data=None,
    )


@contextmanager
def print_callbacks(
    maker: async_sessionmaker[AsyncSession],
    *,
    status=None,
    videos: Sequence[dict] = (),
) -> Iterator[CallbackMocks]:
    """Patch every transport of the two callbacks; their DB work runs on ``maker``.

    The 3MF locate MISSES by default (echoing the payload's subtask name, as the real lookup
    does), so a create lands on the no-3MF fallback archive — the create branch without a file.
    """
    from backend.app.services.bambu_ftp import DeleteResult
    from backend.app.services.foreign_archive import ThreeMFLookup

    async def _miss(_printer, subtask_name, _filename, known_donor=None):
        return ThreeMFLookup(local_path=None, filename=None, subtask_name=subtask_name, expected_plate=None)

    with ExitStack() as stack:
        stack.enter_context(patch("backend.app.main.async_session", maker))
        stack.enter_context(patch("backend.app.core.database.async_session", maker))

        notif = stack.enter_context(patch("backend.app.main.notification_service"))
        for name in ("on_print_complete", "on_queue_completed", "on_foreign_job_detected", "on_plate_not_empty"):
            setattr(notif, name, AsyncMock())
        notif._get_providers_for_event = AsyncMock(return_value=[])

        plugs = stack.enter_context(patch("backend.app.main.smart_plug_manager"))
        plugs.on_print_start = AsyncMock()
        plugs.on_print_complete = AsyncMock()

        ws = stack.enter_context(patch("backend.app.main.ws_manager"))
        for name in (
            "send_print_start",
            "send_print_complete",
            "send_archive_created",
            "send_archive_updated",
            "broadcast",
        ):
            setattr(ws, name, AsyncMock())

        relay = stack.enter_context(patch("backend.app.main.mqtt_relay"))
        for name in ("on_print_start", "on_print_complete", "on_archive_created", "on_archive_updated"):
            setattr(relay, name, AsyncMock())
        # The terminal's JOB phase (services/job_terminal) announces a closed record itself — the
        # same two transports, patched where that module reads them.
        stack.enter_context(patch("backend.app.services.job_terminal.ws_manager", ws))
        stack.enter_context(patch("backend.app.services.job_terminal.mqtt_relay", relay))

        pm = stack.enter_context(patch("backend.app.main.printer_manager"))
        pm.get_printer.return_value = None
        pm.get_current_print_user.return_value = None
        pm.clear_current_print_user = MagicMock()
        pm.get_status.return_value = status
        pm.get_client.return_value = None

        send_start = stack.enter_context(patch("backend.app.main._send_print_start_notification", new=AsyncMock()))
        stack.enter_context(patch("backend.app.main.notify_missing_spool_assignments_on_print_start", new=AsyncMock()))
        energy = stack.enter_context(patch("backend.app.main._record_energy_start", new=AsyncMock(return_value=False)))
        stack.enter_context(patch("backend.app.main._load_objects_from_archive"))
        stack.enter_context(patch("backend.app.main._maybe_start_layer_timelapse"))
        spoolman = stack.enter_context(patch("backend.app.main._store_spoolman_print_data", new=AsyncMock()))
        stack.enter_context(
            patch("backend.app.main._list_timelapse_videos", new=AsyncMock(return_value=(list(videos), "/timelapse")))
        )
        locate = stack.enter_context(patch("backend.app.main.locate_3mf_for_print", new=AsyncMock(side_effect=_miss)))
        stack.enter_context(
            patch("backend.app.main.maybe_schedule_foreign_3mf_retry", new=AsyncMock(return_value=False))
        )
        stack.enter_context(
            patch(
                "backend.app.services.bambu_ftp.delete_file_async",
                new=AsyncMock(return_value=DeleteResult.DELETED),
            )
        )
        # The terminal's finish-photo task holds a DB session across its camera capture; a real
        # capture attempt runs for seconds, and a task cancelled inside that session can strand its
        # connection's lock into the next test. Stubbed, the task finishes at once.
        stack.enter_context(patch("backend.app.services.camera.capture_finish_photo", new=AsyncMock(return_value=None)))
        stack.enter_context(
            patch("backend.app.services.external_camera.capture_frame", new=AsyncMock(return_value=None))
        )
        yield CallbackMocks(
            printer_manager=pm,
            send_start_notification=send_start,
            store_spoolman=spoolman,
            record_energy=energy,
            locate=locate,
            ws=ws,
            notif=notif,
        )


async def drain_new_tasks(tasks_before: set[asyncio.Task], *, timeout: float = 10.0) -> None:
    """Let every task a callback spawned (photo, energy, policy, notifications) FINISH, then reap.

    Awaited to completion rather than cancelled: with the transports mocked they end promptly, and a
    task cancelled while it holds a session can leave its SQLite connection's lock behind for the
    next test's table reset. Anything still running after ``timeout`` is cancelled as a last resort.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        pending = {t for t in asyncio.all_tasks() - tasks_before if t is not asyncio.current_task() and not t.done()}
        remaining = deadline - loop.time()
        if not pending or remaining <= 0:
            break
        await asyncio.wait(pending, timeout=remaining)
    for task in asyncio.all_tasks() - tasks_before:
        if task is asyncio.current_task() or task.done():
            continue
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 — reaping, not asserting
            pass


async def seed_printer(
    maker: async_sessionmaker[AsyncSession], *, serial: str = "H2S-1", auto_archive: bool = True
) -> int:
    from backend.app.models.printer import Printer

    async with maker() as s:
        printer = Printer(
            name=f"P-{serial}",
            serial_number=serial,
            ip_address="10.0.0.9",
            access_code="0000",
            model="H2S",
            auto_archive=auto_archive,
        )
        s.add(printer)
        await s.commit()
        return printer.id


async def seed_archive(
    maker: async_sessionmaker[AsyncSession],
    *,
    printer_id: int | None,
    status: str = "archived",
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    subtask_id: str | None = None,
    filename: str = STORAGE_HASH_FILENAME,
    print_name: str | None = "Fast_Half_Shell",
    file_path: str = "",
    timelapse_path: str | None = None,
    failure_reason: str | None = None,
    created_at: datetime | None = None,
    created_by_id: int | None = None,
) -> int:
    """An archive row written straight to the table — how the dispatcher or an earlier build left it."""
    from backend.app.models.archive import PrintArchive

    fields: dict[str, object] = {
        "printer_id": printer_id,
        "filename": filename,
        "file_path": file_path,
        "file_size": 0,
        "print_name": print_name,
        "status": status,
        "started_at": started_at,
        "completed_at": completed_at,
        "subtask_id": subtask_id,
        "timelapse_path": timelapse_path,
        "failure_reason": failure_reason,
        "created_by_id": created_by_id,
    }
    if created_at is not None:
        fields["created_at"] = created_at
    async with maker() as s:
        archive = PrintArchive(**fields)
        s.add(archive)
        await s.commit()
        return archive.id


async def seed_unit(
    maker: async_sessionmaker[AsyncSession],
    *,
    printer_id: int,
    archive_id: int | None,
    dispatch_subtask_id: str | None,
    status: str = "printing",
    ams_mapping: str | None = None,
    plate_id: int | None = None,
    created_by_id: int | None = None,
) -> int:
    """A queue unit the scheduler has claimed and dispatched onto ``printer_id``."""
    from backend.app.models.print_queue import PrintQueueItem

    async with maker() as s:
        unit = PrintQueueItem(
            printer_id=printer_id,
            archive_id=archive_id,
            status=status,
            dispatch_subtask_id=dispatch_subtask_id,
            ams_mapping=ams_mapping,
            plate_id=plate_id,
            created_by_id=created_by_id,
            started_at=datetime.now(timezone.utc),
        )
        s.add(unit)
        await s.commit()
        return unit.id


async def archive_row(maker: async_sessionmaker[AsyncSession], archive_id: int):
    """The archive as the database holds it NOW (a fresh session — never an identity-map copy)."""
    from backend.app.models.archive import PrintArchive

    async with maker() as s:
        return await s.get(PrintArchive, archive_id)


async def printer_archives(maker: async_sessionmaker[AsyncSession], printer_id: int) -> list:
    """Every archive on ``printer_id``, oldest first."""
    from sqlalchemy import select

    from backend.app.models.archive import PrintArchive

    async with maker() as s:
        rows = await s.execute(
            select(PrintArchive).where(PrintArchive.printer_id == printer_id).order_by(PrintArchive.id)
        )
        return list(rows.scalars().all())
