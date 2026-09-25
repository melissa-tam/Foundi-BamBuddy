"""Queue-UI stop route root-cause fix (W4b).

``POST /queue/{id}/stop`` force-cancels the item synchronously, so by the time the
MQTT terminal callback arrives the item is already ``cancelled`` and main.py's
``stop_source`` stamp (guarded on ``status == "printing"``) is skipped — the unit
landed ``cancelled`` with ``stop_source`` NULL (prod item 219). The route now stamps
``stop_source='operator_ui'`` (this IS classify_stop's queue-UI verdict) and NULLs
any stale hold token in the SAME transition that sets the terminal status.

Since 2026-09-25 the operator's stop REQUEST is durable too: the verb
(``print_control.stop_as_operator``) records it on the row the route names, in its own
committed session, before the ``print.stop`` goes out — so a terminal that lands after a
restart still reads the operator's stop.

FK enforcement is off in the test engine, so a queue item may reference an
arbitrary printer id without seeding the printer.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.api.routes.print_queue import stop_queue_item
from backend.app.models.print_queue import PrintQueueItem
from backend.app.services.printer_manager import printer_manager

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _verb_session_on_the_test_engine(monkeypatch, own_session_factory):
    """The verb opens its OWN session (its commit must precede the publish); point it here."""
    from backend.app.core import database as core_db

    monkeypatch.setattr(core_db, "async_session", own_session_factory)


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
    with patch.object(printer_manager, "stop_print", MagicMock(return_value=True)):
        await stop_queue_item(item.id, db=db_session, auth_result=(None, True))

    await db_session.refresh(item)
    assert item.status == "cancelled"
    assert item.stop_source == "operator_ui"  # root-cause fix — was None (prod item 219)
    assert item.waiting_reason is None  # stale hold token cleared in the same transition
    assert item.error_message == "Stopped by user"
    assert item.operator_stop_requested_at is not None  # the durable request, on THIS row


async def test_stop_when_printer_offline_still_stamps(db_session):
    # Even when the stop command couldn't be sent, the route force-cancels — so it
    # must still stamp operator_ui + clear the reason.
    item = await _add_printing(db_session, printer_id=8, reason="print_paused_stalled")
    with patch.object(printer_manager, "stop_print", MagicMock(return_value=False)):
        await stop_queue_item(item.id, db=db_session, auth_result=(None, True))

    await db_session.refresh(item)
    assert item.status == "cancelled"
    assert item.stop_source == "operator_ui"
    assert item.waiting_reason is None
    assert item.error_message == "Stopped by user (printer was offline)"
    assert item.operator_stop_requested_at is not None


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
    assert item.operator_stop_requested_at is None


async def test_stop_routes_through_the_one_operator_stop(db_session):
    """The stop request + stop pair has ONE owner (``print_control``, 2026-09-12).

    This route keeps its own row transitions and its smart-plug step; what it must not
    keep is a second copy of the wire act or of the request, because a third copy is how
    the request goes missing on the path that needs it — a cancel then reads as a print
    FAILURE. The route names its own unit, so the request lands on exactly this row.
    """
    from backend.app.services import print_control

    item = await _add_printing(db_session, printer_id=9)
    operator_stop = AsyncMock(return_value=True)

    with patch.object(print_control, "stop_as_operator", operator_stop):
        await stop_queue_item(item.id, db=db_session, auth_result=(None, True))

    operator_stop.assert_awaited_once_with(9, unit_id=item.id)
    await db_session.refresh(item)
    assert item.status == "cancelled"
    assert item.stop_source == "operator_ui"
    assert item.error_message == "Stopped by user"
