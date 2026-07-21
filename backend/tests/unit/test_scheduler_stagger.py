"""Power-stagger scheduler integration: the ``stagger_hold`` visibility token.

The budget MATH itself now lives in ``services/stagger.py`` (latency Phase E) and
is covered by ``tests/unit/services/test_stagger.py`` — this file keeps the
scheduler-side behaviour: a held item surfaces a self-clearing ``waiting_reason``
(never notified), clears on dispatch, and filament shortages still surface during
held windows (deficit-before-stagger). ``PrintScheduler._get_int_setting`` (still
used throughout the scheduler) is exercised here too.
"""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import backend.app.services.print_scheduler as sched_mod
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.settings import Settings
from backend.app.services.filament_deficit import FilamentDeficit
from backend.app.services.print_scheduler import PrintScheduler
from backend.app.services.stagger import stagger_policy


async def _set(db, key, value):
    db.add(Settings(key=key, value=str(value)))
    await db.commit()


@pytest.mark.asyncio
class TestGetIntSetting:
    async def test_reads_value(self, db_session):
        await _set(db_session, "stagger_group_size", 4)
        sched = PrintScheduler()
        assert await sched._get_int_setting(db_session, "stagger_group_size", default=2) == 4

    async def test_default_when_absent(self, db_session):
        sched = PrintScheduler()
        assert await sched._get_int_setting(db_session, "nope", default=7) == 7

    async def test_default_when_malformed(self, db_session):
        await _set(db_session, "stagger_group_size", "not-an-int")
        sched = PrintScheduler()
        assert await sched._get_int_setting(db_session, "stagger_group_size", default=2) == 2


# ---------------------------------------------------------------------------
# stagger_hold visibility token (2026-07-12): a held item surfaces a
# self-clearing waiting_reason (never notified), clears on dispatch, and
# filament shortages still surface during held windows (deficit-before-stagger).
# ---------------------------------------------------------------------------


@pytest.fixture
def cq_scheduler(monkeypatch, test_engine):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(sched_mod, "async_session", maker)
    # Phase E: the budget is owned by the module singleton — start each test from
    # a clean in-flight set / ramp-watch so a prior test's dispatch can't leak.
    stagger_policy.reset()

    s = PrintScheduler()
    monkeypatch.setattr(s, "_check_auto_drying", AsyncMock())
    monkeypatch.setattr(s, "_get_job_name", AsyncMock(return_value="job"))
    monkeypatch.setattr(
        s, "_compute_ams_mapping_for_printer", AsyncMock(return_value=sched_mod.MatchOutcome(mapping=[0]))
    )
    monkeypatch.setattr(sched_mod.notification_service, "on_queue_job_assigned", AsyncMock())
    monkeypatch.setattr(sched_mod.notification_service, "on_queue_job_waiting", AsyncMock())
    return s


async def _model_item(db_session, *, batch_id=None, target_model="H2S", pos=1) -> PrintQueueItem:
    item = PrintQueueItem(
        batch_id=batch_id,
        printer_id=None,
        target_model=target_model,
        status="pending",
        manual_start=False,
        filament_short=False,
        position=pos,
    )
    db_session.add(item)
    await db_session.commit()
    await db_session.refresh(item)
    return item


async def _row(db_session, item_id):
    """Fetch scalar columns (no ORM object → no lazy-load across sessions)."""
    db_session.expire_all()
    res = await db_session.execute(
        select(
            PrintQueueItem.printer_id,
            PrintQueueItem.ams_mapping,
            PrintQueueItem.manual_start,
            PrintQueueItem.filament_short,
            PrintQueueItem.waiting_reason,
        ).where(PrintQueueItem.id == item_id)
    )
    return res.one()


@pytest.mark.asyncio
async def test_stagger_held_model_item_gets_stagger_hold_token(cq_scheduler, db_session, printer_factory, monkeypatch):
    """Budget exhausted → held item marked stagger_hold, not dispatched, not notified."""
    p = await printer_factory(model="H2S")
    monkeypatch.setattr(sched_mod.stagger_policy, "budget", AsyncMock(return_value=0))
    monkeypatch.setattr(cq_scheduler, "_find_idle_printer_for_model", AsyncMock(return_value=(p.id, None)))
    monkeypatch.setattr(cq_scheduler, "_compute_deficit_safe", AsyncMock(return_value=[]))  # not short
    start_mock = AsyncMock()
    monkeypatch.setattr(cq_scheduler, "_start_print", start_mock)

    item = await _model_item(db_session)
    await cq_scheduler.check_queue()

    row = await _row(db_session, item.id)
    assert row.waiting_reason == "stagger_hold"
    assert row.printer_id is None  # not claimed
    start_mock.assert_not_awaited()
    # stagger_hold is self-clearing — NEVER notified.
    sched_mod.notification_service.on_queue_job_waiting.assert_not_awaited()


@pytest.mark.asyncio
async def test_stagger_hold_cleared_on_dispatch(cq_scheduler, db_session, printer_factory, monkeypatch):
    """A previously-held item clears the token when the window re-opens and it dispatches."""
    p = await printer_factory(model="H2S")
    monkeypatch.setattr(sched_mod.stagger_policy, "budget", AsyncMock(return_value=5))  # budget available
    monkeypatch.setattr(cq_scheduler, "_find_idle_printer_for_model", AsyncMock(return_value=(p.id, None)))
    monkeypatch.setattr(cq_scheduler, "_compute_deficit_safe", AsyncMock(return_value=[]))

    async def _start(db, item):
        item.status = "printing"
        await db.commit()

    monkeypatch.setattr(cq_scheduler, "_start_print", AsyncMock(side_effect=_start))

    item = await _model_item(db_session)
    item.waiting_reason = "stagger_hold"  # left over from a prior held tick
    await db_session.commit()
    item_id, p_id = item.id, p.id

    await cq_scheduler.check_queue()

    row = await _row(db_session, item_id)
    assert row.printer_id == p_id
    assert row.waiting_reason is None  # stale token cleared on assignment


@pytest.mark.asyncio
async def test_deficit_staging_happens_even_while_budget_exhausted(
    cq_scheduler, db_session, printer_factory, monkeypatch
):
    """Deficit check runs BEFORE the stagger gate: an all-short item is staged
    filament_short even when the stagger budget is spent (shortages surface)."""
    a = await printer_factory(model="H2S")
    b = await printer_factory(model="H2S")
    monkeypatch.setattr(sched_mod.stagger_policy, "budget", AsyncMock(return_value=0))  # window full

    def _find(db, model, exclude, *a_, **k_):
        if a.id not in exclude:
            return (a.id, None)
        if b.id not in exclude:
            return (b.id, None)
        return (None, "No idle H2S printers")

    monkeypatch.setattr(cq_scheduler, "_find_idle_printer_for_model", AsyncMock(side_effect=_find))
    monkeypatch.setattr(
        cq_scheduler,
        "_compute_deficit_safe",
        AsyncMock(
            return_value=[
                FilamentDeficit(
                    slot_id=1, ams_id=0, tray_id=0, filament_type="PETG", required_grams=455.0, remaining_grams=100.0
                )
            ]
        ),
    )
    start_mock = AsyncMock()
    monkeypatch.setattr(cq_scheduler, "_start_print", start_mock)

    item = await _model_item(db_session)
    await cq_scheduler.check_queue()

    row = await _row(db_session, item.id)
    # Staged for filament — NOT parked on the stagger gate.
    assert row.filament_short is True
    assert row.manual_start is True
    assert row.waiting_reason.startswith("Low filament")  # D9: rich reason names the short machines
    start_mock.assert_not_awaited()
