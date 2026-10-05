"""``main.on_print_complete`` spawns the USB sweep's terminal trigger for a PRINT's terminal only.

An eject's terminal clears the plate gate and the next unit uploads within seconds — the
dispatcher's moment — and the printer caps concurrent FTPS sessions, so a sweep spawned there would
open its session exactly when the dispatcher needs one (``usb_storage``'s per-delete gate protects
the files, not the session). A print's terminal is followed by minutes of cooldown. The eject's own
file still leaves at its terminal, through ``job_terminal.delete_uploaded_file``.

The callback runs over the test engine through the shared harness (``_fixtures/print_callbacks``);
the sweep itself is mocked at ``main``'s import of it — what it does is ``test_usb_storage``'s.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.services.plate_occupancy import CooldownEject, Evidence, PendingEject, plate_occupancy
from backend.tests._fixtures.print_callbacks import drain_new_tasks, print_callbacks, seed_printer

pytestmark = pytest.mark.asyncio

_EJECT_TERMINAL = {
    "status": "completed",
    "filename": "eject_production_item2.gcode.3mf",
    "subtask_name": "eject_production_item2",
    "subtask_id": "SUB-E",
    "timelapse_was_active": False,
    "peaks_reliable": True,
    "last_layer_num": 0,
    "last_progress": 0,
}

_PRINT_TERMINAL = {
    "status": "completed",
    "filename": "Bracket.gcode.3mf",
    "subtask_name": "Bracket",
    "subtask_id": "SUB-P",
    "timelapse_was_active": False,
    "peaks_reliable": True,
    "last_layer_num": 40,
    "last_progress": 100,
}


async def _terminal(maker, printer_id: int, payload: dict) -> tuple[AsyncMock, AsyncMock]:
    """Run one terminal; return the sweep spy and the uploaded-file delete spy."""
    from backend.app.main import on_print_complete

    tasks_before = set(asyncio.all_tasks())
    sweep = AsyncMock()
    with (
        print_callbacks(maker),
        patch("backend.app.main.sweep_after_terminal", sweep),
        patch("backend.app.services.farm_policy.on_terminal", new_callable=AsyncMock),
        patch("backend.app.services.job_terminal.delete_uploaded_file", new_callable=AsyncMock) as deletes,
    ):
        await on_print_complete(printer_id, dict(payload))
        await drain_new_tasks(tasks_before)
    return sweep, deletes


def _claim_eject(printer_id: int) -> None:
    """A LIVE server-dispatched eject on the printer, the way a dispatch leaves one."""
    plate_occupancy.hydrate_plate(printer_id, "SUB-P", CooldownEject(unit_id=2, run_id=1))
    assert plate_occupancy.claim_for_eject(printer_id, PendingEject("production", 1, 2), Evidence()) is None


async def test_a_print_terminal_spawns_the_sweep(own_session_factory):
    printer_id = await seed_printer(own_session_factory, serial="USB-SWEEP-P")

    sweep, _ = await _terminal(own_session_factory, printer_id, _PRINT_TERMINAL)

    sweep.assert_called_once_with(printer_id)


async def test_an_eject_terminal_spawns_no_sweep(own_session_factory):
    printer_id = await seed_printer(own_session_factory, serial="USB-SWEEP-E")
    _claim_eject(printer_id)

    sweep, deletes = await _terminal(own_session_factory, printer_id, _EJECT_TERMINAL)

    sweep.assert_not_called()
    # Nothing is lost: the eject's own upload still leaves at its terminal.
    deletes.assert_awaited_once()
    assert deletes.await_args.kwargs["subtask_name"] == "eject_production_item2"


async def test_an_eject_named_terminal_with_no_claim_spawns_no_sweep(own_session_factory):
    """After a restart the claim is gone; the eject name alone identifies the farm's sweep."""
    printer_id = await seed_printer(own_session_factory, serial="USB-SWEEP-N")

    sweep, _ = await _terminal(own_session_factory, printer_id, _EJECT_TERMINAL)

    sweep.assert_not_called()
