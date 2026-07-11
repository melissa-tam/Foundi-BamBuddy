"""The require_previous_success skip sites stamp a machine code (Phase 4.3f).

Both skip sites (assigned-printer and model-based) must set
``waiting_reason="previous_print_failed"`` alongside the human-readable
``error_message`` — the queue UI's resume-after-failure banner matches the
machine code, never the English literal. ``check_queue`` is driven with its
collaborators patched down to the skip branch.
"""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import backend.app.services.print_scheduler as sched_mod
from backend.app.models.print_queue import PrintQueueItem
from backend.app.services.print_scheduler import PrintScheduler

pytestmark = pytest.mark.asyncio


@pytest.fixture
def scheduler(monkeypatch, test_engine):
    """A PrintScheduler whose environment is pinned to the skip branch."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(sched_mod, "async_session", maker)

    s = PrintScheduler()
    monkeypatch.setattr(s, "_is_printer_idle", lambda pid: True)
    monkeypatch.setattr(s, "_check_previous_success", AsyncMock(return_value=False))
    monkeypatch.setattr(s, "_get_job_name", AsyncMock(return_value="job"))
    monkeypatch.setattr(s, "_stagger_budget", AsyncMock(return_value=99))
    monkeypatch.setattr(s, "_check_auto_drying", AsyncMock())
    monkeypatch.setattr(sched_mod.printer_manager, "is_connected", lambda pid: True)
    monkeypatch.setattr(sched_mod.notification_service, "on_queue_job_skipped", AsyncMock())
    return s


async def test_assigned_printer_skip_sets_machine_code(scheduler, db_session, printer_factory):
    printer = await printer_factory()
    item = PrintQueueItem(
        printer_id=printer.id,
        status="pending",
        require_previous_success=True,
        position=1,
    )
    db_session.add(item)
    await db_session.commit()
    item_id = item.id

    await scheduler.check_queue()

    db_session.expire_all()
    row = (await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one()
    assert row.status == "skipped"
    assert row.error_message == "Previous print failed or was aborted"  # human copy kept
    assert row.waiting_reason == "previous_print_failed"  # machine code for the UI


async def test_model_based_skip_sets_machine_code(scheduler, db_session, printer_factory, monkeypatch):
    printer = await printer_factory()
    monkeypatch.setattr(scheduler, "_find_idle_printer_for_model", AsyncMock(return_value=(printer.id, None)))
    item = PrintQueueItem(
        printer_id=None,
        target_model="H2S",
        status="pending",
        require_previous_success=True,
        position=1,
    )
    db_session.add(item)
    await db_session.commit()
    item_id = item.id

    await scheduler.check_queue()

    db_session.expire_all()
    row = (await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one()
    assert row.status == "skipped"
    assert row.waiting_reason == "previous_print_failed"
