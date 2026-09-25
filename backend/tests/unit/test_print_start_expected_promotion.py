"""A dispatched print records itself even with auto-archive off (#839) — through the binding owner.

With ``auto_archive=False`` a print Bambuddy dispatched (queue / reprint) must still bind the archive
the scheduler made for it, so the terminal finds it and usage tracking can charge it. Until
2026-09-25 that promotion was a name-keyed process-memory registry the scheduler filled; it missed
whenever the printer normalised the name and was empty after every restart. Now the dispatched unit
IS the registration: ``print_binding.attach`` finds it by its durable ``dispatch_subtask_id`` and
adopts its never-printed archive, and the unit's own ``ams_mapping`` / ``plate_id`` columns replace
the registry's copies. Driven through the real ``main.on_print_start`` over the test database.

What the old registry tests proved, re-expressed:

* promotion happens for a dispatched print and never for a foreign one (``TestAutoArchiveOff``);
* the completion finds the promoted archive (``test_the_terminal_finds_the_promoted_archive``);
* the name variations the old key-builder chased no longer matter — identity is the id
  (``test_a_printer_normalised_name_still_binds``);
* the start-time injection of the dispatch's AMS mapping and plate into the usage session
  (``TestUsageSessionInjection``), now read off the unit row.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from backend.app.services.usage_tracker import PrintSession, _active_sessions
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
def _clear_usage_sessions():
    _active_sessions.clear()
    yield
    _active_sessions.clear()


async def _start(maker, printer_id: int, payload: dict, *, subtask: str | None):
    from backend.app.main import on_print_start

    tasks_before = set(asyncio.all_tasks())
    with print_callbacks(maker, status=live_state(subtask_id=subtask)) as mocks:
        await on_print_start(printer_id, payload)
        await drain_new_tasks(tasks_before)
    return mocks


@pytest.mark.asyncio
class TestAutoArchiveOff:
    async def test_a_dispatched_print_binds_its_archive(self, own_session_factory):
        maker = own_session_factory
        pid = await seed_printer(maker, auto_archive=False)
        archive_id = await seed_archive(maker, printer_id=None)
        await seed_unit(maker, printer_id=pid, archive_id=archive_id, dispatch_subtask_id="D-839", created_by_id=5)

        mocks = await _start(
            maker, pid, {"filename": "Box.gcode", "subtask_name": "Box", "subtask_id": "D-839"}, subtask="D-839"
        )

        row = await archive_row(maker, archive_id)
        assert (row.status, row.subtask_id, row.printer_id) == ("printing", "D-839", pid)
        archive_data = mocks.send_start_notification.await_args.args[2]
        assert archive_data["created_by_id"] == 5, "the unit's creator reaches the start email"

    async def test_a_foreign_print_records_nothing(self, own_session_factory):
        maker = own_session_factory
        pid = await seed_printer(maker, auto_archive=False)

        mocks = await _start(
            maker, pid, {"filename": "Benchy.gcode", "subtask_name": "Benchy", "subtask_id": "LAN-1"}, subtask="LAN-1"
        )

        assert await printer_archives(maker, pid) == []
        mocks.locate.assert_not_awaited()
        assert mocks.send_start_notification.await_args.args[2] is None

    async def test_a_retry_with_auto_archive_off_records_no_second_row(self, own_session_factory):
        """The retry's donor already recorded its parent's print; with auto-archive off nothing
        creates a record for the attempt. The start email still reaches the unit's creator."""
        maker = own_session_factory
        pid = await seed_printer(maker, auto_archive=False)
        parent = await seed_archive(
            maker, printer_id=pid, status="completed", started_at=datetime(2026, 9, 1), subtask_id="OLD"
        )
        await seed_unit(maker, printer_id=pid, archive_id=parent, dispatch_subtask_id="RETRY", created_by_id=9)

        mocks = await _start(
            maker, pid, {"filename": "Box.gcode", "subtask_name": "Box", "subtask_id": "RETRY"}, subtask="RETRY"
        )

        assert [a.id for a in await printer_archives(maker, pid)] == [parent]
        assert (await archive_row(maker, parent)).status == "completed"
        assert mocks.send_start_notification.await_args.args[2] == {"created_by_id": 9}

    async def test_the_terminal_finds_the_promoted_archive(self, own_session_factory):
        from backend.app.services.print_binding import resolve_terminal

        maker = own_session_factory
        pid = await seed_printer(maker, auto_archive=False)
        archive_id = await seed_archive(maker, printer_id=None)
        await seed_unit(maker, printer_id=pid, archive_id=archive_id, dispatch_subtask_id="D-839")

        await _start(
            maker, pid, {"filename": "Box.gcode", "subtask_name": "Box", "subtask_id": "D-839"}, subtask="D-839"
        )

        async with maker() as s:
            assert await resolve_terminal(s, pid, "D-839") == archive_id


@pytest.mark.asyncio
async def test_a_printer_normalised_name_still_binds(own_session_factory):
    """The printer echoes ``/data/Metadata/plate_2.gcode`` and a human subtask name while the
    archive is named by the storage hash — no name agrees, and none has to."""
    maker = own_session_factory
    pid = await seed_printer(maker)
    archive_id = await seed_archive(maker, printer_id=None)
    await seed_unit(maker, printer_id=pid, archive_id=archive_id, dispatch_subtask_id="D-NORM")

    mocks = await _start(
        maker,
        pid,
        {"filename": "/data/Metadata/plate_2.gcode", "subtask_name": "Fast Half Shell (2)", "subtask_id": "D-NORM"},
        subtask="D-NORM",
    )

    assert (await archive_row(maker, archive_id)).status == "printing"
    assert len(await printer_archives(maker, pid)) == 1, "no second record for the same print"
    mocks.locate.assert_not_awaited()


def _session(*, ams_mapping=None, plate_id=None) -> PrintSession:
    return PrintSession(
        printer_id=0,
        print_name="Box",
        started_at=datetime.now(timezone.utc),
        ams_mapping=ams_mapping,
        plate_id=plate_id,
    )


@pytest.mark.asyncio
class TestUsageSessionInjection:
    """The session is opened before the binding; the unit's durable decision fills what the MQTT
    request-topic capture missed (P1S/A1), and never overwrites what it caught."""

    async def _adopt_with_session(self, maker, session: PrintSession):
        pid = await seed_printer(maker)
        archive_id = await seed_archive(maker, printer_id=None)
        await seed_unit(
            maker,
            printer_id=pid,
            archive_id=archive_id,
            dispatch_subtask_id="D-UT",
            ams_mapping=json.dumps([1, -1, 3]),
            plate_id=2,
        )
        session.printer_id = pid
        _active_sessions[pid] = session
        mocks = await _start(
            maker, pid, {"filename": "Box.gcode", "subtask_name": "Box", "subtask_id": "D-UT"}, subtask="D-UT"
        )
        return _active_sessions[pid], mocks

    async def test_the_units_mapping_and_plate_fill_an_empty_session(self, own_session_factory):
        session, mocks = await self._adopt_with_session(own_session_factory, _session())

        assert session.ams_mapping == [1, -1, 3]
        assert session.plate_id == 2
        spoolman = mocks.store_spoolman.await_args.kwargs
        assert (spoolman["ams_mapping"], spoolman["plate_id"]) == ([1, -1, 3], 2)

    async def test_a_captured_mapping_and_plate_are_kept(self, own_session_factory):
        session, _mocks = await self._adopt_with_session(own_session_factory, _session(ams_mapping=[5, 6], plate_id=3))

        assert session.ams_mapping == [5, 6]
        assert session.plate_id == 3

    async def test_a_retry_on_a_new_record_gets_the_units_decision_too(self, own_session_factory):
        """A retry's donor already recorded its parent's print, so the attempt gets a NEW record —
        and the same injection: every print the farm dispatched feeds from the unit's decision."""
        maker = own_session_factory
        pid = await seed_printer(maker)
        parent = await seed_archive(
            maker, printer_id=pid, status="completed", started_at=datetime(2026, 9, 1), subtask_id="OLD"
        )
        await seed_unit(
            maker,
            printer_id=pid,
            archive_id=parent,
            dispatch_subtask_id="RETRY-UT",
            ams_mapping=json.dumps([2]),
            plate_id=4,
        )
        _active_sessions[pid] = _session()
        _active_sessions[pid].printer_id = pid

        mocks = await _start(
            maker, pid, {"filename": "Box.gcode", "subtask_name": "Box", "subtask_id": "RETRY-UT"}, subtask="RETRY-UT"
        )

        session = _active_sessions[pid]
        assert (session.ams_mapping, session.plate_id) == ([2], 4)
        assert len(await printer_archives(maker, pid)) == 2, "the attempt is on a record of its own"
        spoolman = mocks.store_spoolman.await_args.kwargs
        assert (spoolman["ams_mapping"], spoolman["plate_id"]) == ([2], 4)
