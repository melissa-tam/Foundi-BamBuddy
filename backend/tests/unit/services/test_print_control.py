"""``print_control.stop_as_operator`` — the operator's stop REQUEST, durable, then the stop.

The request (``print_queue.operator_stop_requested_at``) replaced the process-memory
``main._user_stopped_printers`` set on 2026-09-25: a restart emptied the set, and the
terminal of a job an operator had stopped read as no stop at all — the farm's own job
resolved FOREIGN. Pinned here: which unit carries the request, that it is COMMITTED before
``print.stop`` is published, and that neither half can take the other down.

FK enforcement is off in the test engine, so a queue item may reference an arbitrary
printer id without seeding the printer.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.app.models.print_queue import PrintQueueItem
from backend.app.services import print_control

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _verb_session_on_the_test_engine(monkeypatch, own_session_factory):
    """The verb opens its OWN session (its commit must precede the publish); point it here."""
    from backend.app.core import database as core_db

    monkeypatch.setattr(core_db, "async_session", own_session_factory)


@pytest.fixture
def wire(monkeypatch):
    """The printer's live state and the raw publisher, recorded."""
    sent: list[int] = []
    live = SimpleNamespace(subtask_id=None)
    monkeypatch.setattr(print_control.printer_manager, "get_status", lambda _pid: live)
    monkeypatch.setattr(print_control.printer_manager, "stop_print", lambda pid: sent.append(pid) or True)
    return SimpleNamespace(sent=sent, live=live)


async def _unit(db, *, printer_id, subtask, status="printing"):
    item = PrintQueueItem(printer_id=printer_id, status=status, dispatch_subtask_id=subtask, position=1)
    db.add(item)
    await db.commit()
    return item


async def _request(db, item: PrintQueueItem):
    await db.refresh(item)
    return item.operator_stop_requested_at


async def test_the_printers_running_unit_carries_the_request(db_session, wire):
    item = await _unit(db_session, printer_id=1, subtask="SUB-1")
    wire.live.subtask_id = "SUB-1"

    assert await print_control.stop_as_operator(1) is True

    assert wire.sent == [1]
    assert await _request(db_session, item) is not None


async def test_the_live_job_id_picks_the_unit_among_several(db_session, wire):
    """``resolve_printing_item``: the printer's echoed job first — never the other row."""
    running = await _unit(db_session, printer_id=2, subtask="SUB-A")
    other = await _unit(db_session, printer_id=2, subtask="SUB-B")
    wire.live.subtask_id = "SUB-A"

    await print_control.stop_as_operator(2)

    assert await _request(db_session, running) is not None
    assert await _request(db_session, other) is None


async def test_the_named_unit_is_the_one_stamped(db_session, wire):
    """The queue page names its row — exact, whatever the live state says."""
    named = await _unit(db_session, printer_id=3, subtask="SUB-N")
    await _unit(db_session, printer_id=3, subtask="SUB-M")
    wire.live.subtask_id = "SUB-M"

    await print_control.stop_as_operator(3, unit_id=named.id)

    assert await _request(db_session, named) is not None


async def test_a_foreign_print_has_no_unit_and_the_stop_still_goes_out(db_session, wire):
    """Nothing is printing for the farm: no request is written anywhere, the stop is sent,
    and the terminal will be classified from the printer's own echo."""
    ended = await _unit(db_session, printer_id=4, subtask="SUB-OLD", status="completed")
    wire.live.subtask_id = "SCREEN-1"

    assert await print_control.stop_as_operator(4) is True

    assert wire.sent == [4]
    assert await _request(db_session, ended) is None


async def test_the_request_is_committed_before_the_stop_is_published(db_session, wire, monkeypatch):
    """THE ordering. A terminal can only follow the publish, so the request's commit must
    precede it: ``run_with_retry`` returns only after the write committed."""
    await _unit(db_session, printer_id=5, subtask="SUB-5")
    wire.live.subtask_id = "SUB-5"
    order: list[str] = []
    real = print_control.run_with_retry

    async def _committing(fn, **kw):
        result = await real(fn, **kw)
        order.append("request committed")
        return result

    monkeypatch.setattr(print_control, "run_with_retry", _committing)
    monkeypatch.setattr(print_control.printer_manager, "stop_print", lambda _pid: order.append("published") or True)

    await print_control.stop_as_operator(5)

    assert order == ["request committed", "published"]


async def test_an_undelivered_stop_keeps_the_request_and_reports_it(db_session, wire, monkeypatch):
    """The operator asked; the printer-card route reports the 502. The request stands until
    the unit ends or is re-armed."""
    item = await _unit(db_session, printer_id=6, subtask="SUB-6")
    monkeypatch.setattr(print_control.printer_manager, "stop_print", MagicMock(return_value=False))

    assert await print_control.stop_as_operator(6) is False

    assert await _request(db_session, item) is not None


async def test_a_request_that_cannot_be_written_never_blocks_the_stop(db_session, wire, monkeypatch, caplog):
    """Losing the request mislabels a cancel as a failure — worse than a log line, but not a
    reason to leave the print running."""
    await _unit(db_session, printer_id=7, subtask="SUB-7")

    async def _broken(_fn, **_kw):
        raise RuntimeError("database is gone")

    monkeypatch.setattr(print_control, "run_with_retry", _broken)

    assert await print_control.stop_as_operator(7) is True

    assert wire.sent == [7]
    assert any("could not record the stop request" in r.getMessage() for r in caplog.records)


async def test_a_raising_publisher_is_an_undelivered_stop(db_session, wire, monkeypatch):
    item = await _unit(db_session, printer_id=8, subtask="SUB-8")
    monkeypatch.setattr(print_control.printer_manager, "stop_print", MagicMock(side_effect=OSError("socket")))

    assert await print_control.stop_as_operator(8) is False

    assert await _request(db_session, item) is not None
