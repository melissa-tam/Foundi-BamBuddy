"""Regression for #1807: the live record must carry the LIVE job's id.

Bambuddy mints a fresh subtask_id per dispatch. When a reprint reused the source archive row, the
stored ``archive.subtask_id`` could still be the FIRST run's — and on the next MQTT reconnect the
downtime reconcile compared the printer's live id against it, saw a mismatch and synthesised a
bogus "Print Stopped" while the print kept running.

Since 2026-09-25 the print's record is bound by ``print_binding.attach`` and stamped with the unit's
DURABLE dispatch id (never the lagging echo, never the process-memory ``last_dispatch_subtask_id``):

* a reprint of an archive that already recorded a run gets a NEW record carrying the new id — the
  first run's row keeps its own id and is not live, so the reconcile never reads it (the ruling
  "one archive per attempt" replaces the upstream rewrite of the reused row);
* a first run stamps the id on the adopted archive, even before the printer echoes one;
* a repeated start of the same job resumes its record and rewrites nothing.

Driven through the real ``main.on_print_start`` over the test database.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

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


async def _start(maker, printer_id: int, echoed: str | None):
    """A print-start payload carrying the printer-echoed id (None: not echoed yet)."""
    from backend.app.main import on_print_start

    tasks_before = set(asyncio.all_tasks())
    payload = {"filename": "Ikea-drybox_silicabox.3mf", "subtask_name": "Ikea-drybox_silicabox"}
    if echoed is not None:
        payload["subtask_id"] = echoed
    with print_callbacks(maker, status=live_state(subtask_id=echoed)):
        await on_print_start(printer_id, payload)
        await drain_new_tasks(tasks_before)


@pytest.mark.asyncio
async def test_a_reprint_records_the_new_dispatch_id_on_its_own_record(own_session_factory):
    """The #1807 case: the source archive holds the FIRST run's id; the reprint's live record must
    carry the new one so the reconciler does not flag the live print on the next reconnect."""
    maker = own_session_factory
    pid = await seed_printer(maker)
    first_run = await seed_archive(
        maker,
        printer_id=pid,
        status="completed",
        started_at=datetime(2026, 9, 1, 8),
        completed_at=datetime(2026, 9, 1, 14),
        subtask_id="1844213296",
    )
    await seed_unit(maker, printer_id=pid, archive_id=first_run, dispatch_subtask_id="2103771517")

    await _start(maker, pid, "2103771517")

    live = [a for a in await printer_archives(maker, pid) if a.status == "printing"]
    assert [a.subtask_id for a in live] == ["2103771517"], (
        "the live record must carry the new dispatch id; a stale one lets reconcile_stale_active_prints "
        "synthesise a bogus PRINT COMPLETE on the next MQTT reconnect (#1807)"
    )
    assert (await archive_row(maker, first_run)).subtask_id == "1844213296", "the first run keeps its own id"


@pytest.mark.asyncio
async def test_a_first_run_stamps_the_units_id_before_the_echo_arrives(own_session_factory):
    """The printer often has not echoed the id this soon after dispatch; the unit's durable
    dispatch id is the record's id regardless."""
    maker = own_session_factory
    pid = await seed_printer(maker)
    archive_id = await seed_archive(maker, printer_id=pid, subtask_id=None)
    await seed_unit(maker, printer_id=pid, archive_id=archive_id, dispatch_subtask_id="2103771517")

    await _start(maker, pid, None)

    assert (await archive_row(maker, archive_id)).subtask_id == "2103771517"


@pytest.mark.asyncio
async def test_a_repeated_start_of_the_same_job_rewrites_nothing(own_session_factory):
    maker = own_session_factory
    pid = await seed_printer(maker)
    started = datetime(2026, 9, 24, 10)
    archive_id = await seed_archive(
        maker, printer_id=pid, status="printing", started_at=started, subtask_id="2103771517"
    )
    await seed_unit(maker, printer_id=pid, archive_id=archive_id, dispatch_subtask_id="2103771517")

    await _start(maker, pid, "2103771517")

    row = await archive_row(maker, archive_id)
    assert (row.status, row.subtask_id, row.started_at) == ("printing", "2103771517", started)
    assert len(await printer_archives(maker, pid)) == 1
