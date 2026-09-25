"""Regression for #1707: a print's finish photo must never be another run's frame.

Telegram (and any image-bearing) notification on a reprint from archive showed the ORIGINAL print's
finish photo, because the reused archive row still pointed ``timelapse_path`` at the first run's
MP4: ``_scan_for_timelapse_with_retries`` early-returned ("already has timelapse") and
``_capture_finish_photo_from_timelapse`` extracted the original's last frame.

Two guarantees now carry it, both driven through the real ``main.on_print_start``:

* **One archive per attempt (2026-09-25 ruling).** A reprint of an archive that already recorded a
  print gets a NEW record (``print_binding.attach`` refuses to adopt it), so the original run's
  video, timelapse path and row stay with the original run — the reprint's record has no video to
  confuse. This REPLACES the reuse the upstream fix patched over.
* **An adopted archive starts clean.** A never-printed archive the farm adopts can still carry a
  video (an imported archive); the adopt branch clears the path and unlinks the file, as upstream
  did on reuse.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import patch

import pytest

from backend.app.core.config import settings as app_settings
from backend.app.main import _timelapse_baselines
from backend.tests._fixtures.print_callbacks import (
    archive_row,
    drain_new_tasks,
    live_state,
    print_callbacks,
    printer_archives,
    seed_archive,
    seed_printer,
    seed_unit,
)


@pytest.fixture(autouse=True)
def _clear_baselines():
    _timelapse_baselines.clear()
    yield
    _timelapse_baselines.clear()


async def _start(maker, printer_id: int, subtask: str, tmp_path):
    from backend.app.main import on_print_start

    tasks_before = set(asyncio.all_tasks())
    with (
        print_callbacks(maker, status=live_state(subtask_id=subtask)),
        patch.object(app_settings, "base_dir", tmp_path),
    ):
        await on_print_start(printer_id, {"filename": "MyModel.3mf", "subtask_name": "MyModel", "subtask_id": subtask})
        await drain_new_tasks(tasks_before)


@pytest.mark.asyncio
async def test_a_reprint_of_a_printed_archive_leaves_the_original_runs_video_alone(own_session_factory, tmp_path):
    maker = own_session_factory
    pid = await seed_printer(maker)
    relpath = "archives/42/timelapse/original.mp4"
    video = tmp_path / relpath
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"old timelapse bytes")
    original = await seed_archive(
        maker,
        printer_id=pid,
        status="completed",
        started_at=datetime(2026, 9, 1, 8),
        completed_at=datetime(2026, 9, 1, 14),
        subtask_id="FIRST-RUN",
        timelapse_path=relpath,
    )
    await seed_unit(maker, printer_id=pid, archive_id=original, dispatch_subtask_id="REPRINT-1707")

    await _start(maker, pid, "REPRINT-1707", tmp_path)

    kept = await archive_row(maker, original)
    assert (kept.status, kept.timelapse_path, kept.subtask_id) == ("completed", relpath, "FIRST-RUN")
    assert video.exists(), "the original run keeps its video"
    records = [a for a in await printer_archives(maker, pid) if a.id != original]
    assert len(records) == 1, "the reprint is recorded on a record of its own"
    assert (records[0].status, records[0].subtask_id, records[0].timelapse_path) == ("printing", "REPRINT-1707", None)


@pytest.mark.asyncio
async def test_adopting_an_archive_with_a_video_clears_and_unlinks_it(own_session_factory, tmp_path):
    maker = own_session_factory
    pid = await seed_printer(maker)
    relpath = "archives/43/timelapse/imported.mp4"
    stale_file = tmp_path / relpath
    stale_file.parent.mkdir(parents=True, exist_ok=True)
    stale_file.write_bytes(b"imported timelapse bytes")
    archive_id = await seed_archive(maker, printer_id=pid, timelapse_path=relpath)
    await seed_unit(maker, printer_id=pid, archive_id=archive_id, dispatch_subtask_id="ADOPT-1707")

    await _start(maker, pid, "ADOPT-1707", tmp_path)

    row = await archive_row(maker, archive_id)
    assert row.timelapse_path is None, (
        "the adopt branch must clear timelapse_path so _scan_for_timelapse_with_retries doesn't "
        "early-return and _capture_finish_photo_from_timelapse doesn't extract another run's frame (#1707)"
    )
    assert row.status == "printing"
    assert not stale_file.exists(), "the stale video is unlinked so the archive directory keeps no orphan"


@pytest.mark.asyncio
async def test_adopting_an_archive_with_no_video_is_a_noop(own_session_factory, tmp_path):
    maker = own_session_factory
    pid = await seed_printer(maker)
    archive_id = await seed_archive(maker, printer_id=pid, timelapse_path=None)
    await seed_unit(maker, printer_id=pid, archive_id=archive_id, dispatch_subtask_id="FRESH-1707")

    await _start(maker, pid, "FRESH-1707", tmp_path)

    row = await archive_row(maker, archive_id)
    assert (row.timelapse_path, row.status) == (None, "printing")


@pytest.mark.asyncio
async def test_adopting_with_the_video_already_gone_does_not_raise(own_session_factory, tmp_path):
    """The file behind ``timelapse_path`` was deleted (user delete, purge, bind-mount drift): the
    path is still cleared cleanly."""
    maker = own_session_factory
    pid = await seed_printer(maker)
    archive_id = await seed_archive(maker, printer_id=pid, timelapse_path="archives/7/timelapse/vanished.mp4")
    await seed_unit(maker, printer_id=pid, archive_id=archive_id, dispatch_subtask_id="GHOST-1707")

    await _start(maker, pid, "GHOST-1707", tmp_path)

    row = await archive_row(maker, archive_id)
    assert (row.timelapse_path, row.status) == (None, "printing")
