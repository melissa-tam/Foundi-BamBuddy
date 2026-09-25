"""Regression for #1403 follow-up: an adopted VP-queue archive must be assigned its printer.

Reporter @pwostran and @enjoylifenow both saw "Scan for timelapse" greyed out on archives that came
from the VP print-queue flow even though the H.264 timelapse was sitting on the printer's SD card.
The frontend gates that button on ``!archive.printer_id`` (ArchivesPage.tsx:459). VP-queue archives
are created with ``printer_id=None`` at queue-add time because nobody knows which printer will run
the job; the print start must assign the printer that actually started it.

Since 2026-09-25 that start is ``print_binding.attach`` adopting the unit's never-printed archive in
one statement that also stamps ``printer_id`` — driven here through the real ``main.on_print_start``
over the test database, together with the timelapse baseline the adopt branch must capture.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.app.main import _timelapse_baselines
from backend.tests._fixtures.print_callbacks import (
    archive_row,
    drain_new_tasks,
    live_state,
    print_callbacks,
    seed_archive,
    seed_printer,
    seed_unit,
)


@pytest.fixture(autouse=True)
def _clear_baselines():
    _timelapse_baselines.clear()
    yield
    _timelapse_baselines.clear()


async def _start(maker, printer_id: int, *, subtask: str, videos=()):
    from backend.app.main import on_print_start

    tasks_before = set(asyncio.all_tasks())
    with print_callbacks(maker, status=live_state(subtask_id=subtask), videos=videos):
        await on_print_start(
            printer_id,
            {
                "filename": "bambu_lab_a1_tool_plate_3.gcode.3mf",
                "subtask_name": "bambu_lab_a1_tool_plate_3",
                "subtask_id": subtask,
            },
        )
        await drain_new_tasks(tasks_before)


@pytest.mark.asyncio
async def test_adopting_a_vp_queue_archive_assigns_the_running_printer(own_session_factory):
    """VP-queue archives arrive with printer_id=None; without the assignment the
    /archives/{id}/timelapse/scan endpoint refuses the request and the UI button stays disabled."""
    maker = own_session_factory
    pid = await seed_printer(maker)
    archive_id = await seed_archive(maker, printer_id=None, filename="bambu_lab_a1_tool_plate_3.gcode.3mf")
    await seed_unit(maker, printer_id=pid, archive_id=archive_id, dispatch_subtask_id="VP-1403")

    await _start(maker, pid, subtask="VP-1403")

    row = await archive_row(maker, archive_id)
    assert row.printer_id == pid, (
        "the adopted archive must carry the running printer_id so the post-print timelapse-scan path "
        "(gated on archive.printer_id) works"
    )
    assert row.status == "printing"


@pytest.mark.asyncio
async def test_adopting_an_archive_that_already_names_the_printer_keeps_it(own_session_factory):
    """Idempotent on correct data: a library-dispatch copy created with the printer already set."""
    maker = own_session_factory
    pid = await seed_printer(maker)
    archive_id = await seed_archive(maker, printer_id=pid, filename="MyModel.3mf", print_name="MyModel")
    await seed_unit(maker, printer_id=pid, archive_id=archive_id, dispatch_subtask_id="LIB-7")

    await _start(maker, pid, subtask="LIB-7")

    row = await archive_row(maker, archive_id)
    assert (row.printer_id, row.status) == (pid, "printing")


@pytest.mark.asyncio
async def test_adopting_captures_the_timelapse_baseline(own_session_factory):
    """Queue / VP-dispatched prints take the adopt branch, which once skipped the baseline capture
    the new-archive branch did. Without a baseline, _scan_for_timelapse_with_retries falls into its
    "take baseline now" fallback that snapshots the SD card AFTER the new MP4 has landed — the new
    file ends up in the baseline set and no diff ever matches."""
    maker = own_session_factory
    pid = await seed_printer(maker)
    archive_id = await seed_archive(maker, printer_id=None)
    await seed_unit(maker, printer_id=pid, archive_id=archive_id, dispatch_subtask_id="VP-TL")
    existing_videos = [
        {"name": "earlier_print_a.mp4", "is_directory": False, "path": "/timelapse/earlier_print_a.mp4"},
        {"name": "earlier_print_b.mp4", "is_directory": False, "path": "/timelapse/earlier_print_b.mp4"},
    ]

    await _start(maker, pid, subtask="VP-TL", videos=existing_videos)

    assert _timelapse_baselines.get(pid) == {"earlier_print_a.mp4", "earlier_print_b.mp4"}, (
        "the adopt branch must capture the printer's existing-videos baseline so the completion-time "
        "scan can set-diff to find the new file"
    )
