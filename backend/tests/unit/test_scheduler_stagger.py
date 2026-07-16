"""Power-stagger consumer: window-budget math + restart-safe derivation (Phase 4).

The scheduler's ``_stagger_budget`` derives how many more prints may BEGIN
heating this tick from the persisted ``stagger_group_size`` /
``stagger_interval_minutes`` settings and a BY-QUERY count of recent
``print_queue.started_at`` — so a backend restart can't unleash a thundering
herd (no reliance on in-memory counters).
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import backend.app.services.print_scheduler as sched_mod
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.settings import Settings
from backend.app.services.filament_deficit import FilamentDeficit
from backend.app.services.print_scheduler import PrintScheduler


async def _set(db, key, value):
    db.add(Settings(key=key, value=str(value)))
    await db.commit()


async def _add_started_item(db, minutes_ago):
    item = PrintQueueItem(
        status="printing",
        started_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )
    db.add(item)
    await db.commit()


@pytest.mark.asyncio
class TestStaggerBudget:
    async def test_defaults_when_no_settings_no_starts(self, db_session):
        # Schema defaults: group_size 2, interval 3 → full budget of 2.
        sched = PrintScheduler()
        assert await sched._stagger_budget(db_session) == 2

    async def test_only_starts_inside_window_count(self, db_session):
        await _set(db_session, "stagger_group_size", 3)
        await _set(db_session, "stagger_interval_minutes", 5)
        await _add_started_item(db_session, minutes_ago=1)  # inside window
        await _add_started_item(db_session, minutes_ago=60)  # outside window
        sched = PrintScheduler()
        # Only the 1-min-ago start counts → 3 - 1 = 2 remaining.
        assert await sched._stagger_budget(db_session) == 2

    async def test_budget_exhausted_when_window_full(self, db_session):
        await _set(db_session, "stagger_group_size", 2)
        await _set(db_session, "stagger_interval_minutes", 5)
        await _add_started_item(db_session, minutes_ago=1)
        await _add_started_item(db_session, minutes_ago=2)
        sched = PrintScheduler()
        assert await sched._stagger_budget(db_session) == 0

    async def test_never_negative(self, db_session):
        await _set(db_session, "stagger_group_size", 1)
        await _set(db_session, "stagger_interval_minutes", 10)
        await _add_started_item(db_session, minutes_ago=1)
        await _add_started_item(db_session, minutes_ago=2)
        await _add_started_item(db_session, minutes_ago=3)
        sched = PrintScheduler()
        assert await sched._stagger_budget(db_session) == 0

    async def test_high_group_size_effectively_disables(self, db_session):
        await _set(db_session, "stagger_group_size", 50)
        await _set(db_session, "stagger_interval_minutes", 5)
        for _ in range(3):
            await _add_started_item(db_session, minutes_ago=1)
        sched = PrintScheduler()
        # 50 - 3 = 47 remaining — staggering never bites in practice.
        assert await sched._stagger_budget(db_session) == 47

    async def test_restart_derives_budget_by_query_not_memory(self, db_session):
        # Two separate scheduler instances (a "restart") see the SAME budget for
        # the same DB state — the window is reconstructed from started_at, not any
        # in-memory counter.
        await _set(db_session, "stagger_group_size", 3)
        await _set(db_session, "stagger_interval_minutes", 5)
        await _add_started_item(db_session, minutes_ago=1)
        await _add_started_item(db_session, minutes_ago=2)

        before_restart = PrintScheduler()
        budget_a = await before_restart._stagger_budget(db_session)
        # Simulate a process restart: brand-new instance, no shared state.
        after_restart = PrintScheduler()
        budget_b = await after_restart._stagger_budget(db_session)
        assert budget_a == budget_b == 1

    async def test_interval_widens_window_to_include_more_starts(self, db_session):
        await _set(db_session, "stagger_group_size", 5)
        await _set(db_session, "stagger_interval_minutes", 30)
        await _add_started_item(db_session, minutes_ago=1)
        await _add_started_item(db_session, minutes_ago=20)  # now inside the wider window
        sched = PrintScheduler()
        assert await sched._stagger_budget(db_session) == 3


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


@pytest.mark.asyncio
class TestStaggerDefaultInterval:
    async def test_default_interval_3_excludes_older_starts(self, db_session):
        """New default interval is 3 min: a start 4 min ago falls OUTSIDE the
        window (it would still count under the old 5-min default)."""
        await _set(db_session, "stagger_group_size", 2)  # only group size configured
        await _add_started_item(db_session, minutes_ago=4)  # outside a 3-min window
        sched = PrintScheduler()
        # 4-min-ago start excluded → full budget of 2 remains (was 1 under old default 5).
        assert await sched._stagger_budget(db_session) == 2


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

    s = PrintScheduler()
    monkeypatch.setattr(s, "_check_auto_drying", AsyncMock())
    monkeypatch.setattr(s, "_get_job_name", AsyncMock(return_value="job"))
    monkeypatch.setattr(s, "_compute_ams_mapping_for_printer", AsyncMock(return_value=sched_mod.MatchOutcome(mapping=[0])))
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
    monkeypatch.setattr(cq_scheduler, "_stagger_budget", AsyncMock(return_value=0))
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
    monkeypatch.setattr(cq_scheduler, "_stagger_budget", AsyncMock(return_value=5))  # budget available
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
    monkeypatch.setattr(cq_scheduler, "_stagger_budget", AsyncMock(return_value=0))  # window full

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
    assert row.waiting_reason == "filament_short"
    start_mock.assert_not_awaited()
