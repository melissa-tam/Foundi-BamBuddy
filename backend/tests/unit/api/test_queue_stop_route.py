"""Queue-UI stop route root-cause fix (W4b).

``POST /queue/{id}/stop`` force-cancels the item synchronously, so by the time the
MQTT terminal callback arrives the item is already ``cancelled`` and main.py's
``stop_source`` stamp (guarded on ``status == "printing"``) is skipped — the unit
landed ``cancelled`` with ``stop_source`` NULL (prod item 219). The route now stamps
``stop_source='operator_ui'`` (this IS classify_stop's queue-UI verdict) and NULLs
any stale hold token in the SAME transition that sets the terminal status.

FK enforcement is off in the test engine, so a queue item may reference an
arbitrary printer id without seeding the printer.
"""

from unittest.mock import MagicMock, patch

import pytest

import backend.app.main as main_module
from backend.app.api.routes.print_queue import stop_queue_item
from backend.app.models.print_queue import PrintQueueItem
from backend.app.services.printer_manager import printer_manager

pytestmark = pytest.mark.asyncio


async def _add_printing(db, *, printer_id=7, reason=None):
    item = PrintQueueItem(
        printer_id=printer_id,
        status="printing",
        plate_id=1,
        position=1,
        auto_off_after=False,
        waiting_reason=reason,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


async def test_stop_stamps_operator_ui_and_clears_waiting_reason(db_session):
    item = await _add_printing(db_session, printer_id=7, reason="spool_jam_recovery_failed")
    with (
        patch.object(printer_manager, "stop_print", MagicMock(return_value=True)),
        patch.object(main_module, "mark_printer_stopped_by_user", MagicMock()),
    ):
        await stop_queue_item(item.id, db=db_session, auth_result=(None, True))

    await db_session.refresh(item)
    assert item.status == "cancelled"
    assert item.stop_source == "operator_ui"  # root-cause fix — was None (prod item 219)
    assert item.waiting_reason is None  # stale hold token cleared in the same transition
    assert item.error_message == "Stopped by user"


async def test_stop_when_printer_offline_still_stamps(db_session):
    # Even when the stop command couldn't be sent, the route force-cancels — so it
    # must still stamp operator_ui + clear the reason.
    item = await _add_printing(db_session, printer_id=8, reason="print_paused_stalled")
    with (
        patch.object(printer_manager, "stop_print", MagicMock(return_value=False)),
        patch.object(main_module, "mark_printer_stopped_by_user", MagicMock()),
    ):
        await stop_queue_item(item.id, db=db_session, auth_result=(None, True))

    await db_session.refresh(item)
    assert item.status == "cancelled"
    assert item.stop_source == "operator_ui"
    assert item.waiting_reason is None
    assert item.error_message == "Stopped by user (printer was offline)"


async def test_stop_rejects_non_printing_item(db_session):
    # A non-printing (already terminal / pending) item is a 400 — no state change,
    # so a non-terminal→terminal transition never happens here.
    from fastapi import HTTPException

    item = PrintQueueItem(printer_id=7, status="pending", plate_id=1, position=1, waiting_reason="stagger_hold")
    db_session.add(item)
    await db_session.commit()
    with pytest.raises(HTTPException) as exc:
        await stop_queue_item(item.id, db=db_session, auth_result=(None, True))
    assert exc.value.status_code == 400
    await db_session.refresh(item)
    assert item.status == "pending"
    assert item.stop_source is None
    assert item.waiting_reason == "stagger_hold"  # untouched — no terminal transition
