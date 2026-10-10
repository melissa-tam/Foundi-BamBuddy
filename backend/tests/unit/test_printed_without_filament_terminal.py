"""A FINISH that printed on air, through the REAL ``main.on_print_complete`` (011-H2S, 2026-10-09).

011-H2S was resumed onto an empty toolhead at layer 93 of 167 and ran to the end on air; the printer
said FINISH. Every figure the terminal writes or sends about filament reads the ONE charged extent
(``job_extent``): the archive records the failure, the print-log row — the row ``GET
/archives/stats`` sums, which foundi-accounting posts as COGS — states the 93 layers' grams, and so
does the completion notification. Only the transports are mocked (``print_callbacks``).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from backend.tests._fixtures.print_callbacks import (
    archive_row,
    drain_new_tasks,
    live_state,
    print_callbacks,
    seed_archive,
    seed_printer,
)

_JOB = "011-H2S-538"
_PLATE_GRAMS = 167.0  # one gram per layer of the 167-layer plate, so the fed share reads as layers
_FED_GRAMS = 93.0


@pytest.mark.asyncio
async def test_the_archive_the_print_log_the_stats_and_the_notification_state_what_fed(own_session_factory):
    from backend.app.api.routes.archives import get_archive_stats
    from backend.app.main import on_print_complete
    from backend.app.models.archive import PrintArchive
    from backend.app.models.print_log import PrintLogEntry

    maker = own_session_factory
    pid = await seed_printer(maker)
    archive_id = await seed_archive(
        maker,
        printer_id=pid,
        status="printing",
        started_at=datetime.now(timezone.utc) - timedelta(hours=9),
        subtask_id=_JOB,
    )
    async with maker() as s:
        # The slicer estimate the archive was created with (``ArchiveService._ingest_3mf``).
        await s.execute(
            update(PrintArchive).where(PrintArchive.id == archive_id).values(filament_used_grams=_PLATE_GRAMS)
        )
        await s.commit()

    tasks_before = set(asyncio.all_tasks())
    with print_callbacks(maker, status=live_state(subtask_id=_JOB, state="FINISH", progress=100.0)) as mocks:
        await on_print_complete(
            pid,
            {
                "status": "completed",
                "subtask_id": _JOB,
                "subtask_name": "Fast_Half_Shell",
                "filename": "/data/Metadata/plate_1.gcode",
                "timelapse_was_active": False,
                "peaks_reliable": True,
                "last_layer_num": 167,
                "last_progress": 100.0,
                "total_layers": 167,
                "first_unfed_layer": 93,
            },
            archive_id=archive_id,
        )
        await drain_new_tasks(tasks_before)

    archive = await archive_row(maker, archive_id)
    assert (archive.status, archive.failure_reason) == ("failed", "printed_without_filament")

    async with maker() as s:
        logs = (await s.execute(select(PrintLogEntry).where(PrintLogEntry.archive_id == archive_id))).scalars().all()
        assert [(log.status, log.filament_used_grams) for log in logs] == [("failed", _FED_GRAMS)]
        stats = await get_archive_stats(date_from=None, date_to=None, created_by_id=None, db=s, current_user=None)
    assert stats.total_filament_grams == _FED_GRAMS

    mocks.notif.on_print_complete.assert_awaited_once()
    sent = mocks.notif.on_print_complete.await_args
    assert sent.args[2] == "failed"
    assert sent.kwargs["archive_data"]["actual_filament_grams"] == _FED_GRAMS
    assert sent.kwargs["archive_data"]["printer_message"] == "Printed without filament from layer 93 of 167"
