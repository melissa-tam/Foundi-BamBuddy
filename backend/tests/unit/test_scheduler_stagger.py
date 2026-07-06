"""Power-stagger consumer: window-budget math + restart-safe derivation (Phase 4).

The scheduler's ``_stagger_budget`` derives how many more prints may BEGIN
heating this tick from the persisted ``stagger_group_size`` /
``stagger_interval_minutes`` settings and a BY-QUERY count of recent
``print_queue.started_at`` — so a backend restart can't unleash a thundering
herd (no reliance on in-memory counters).
"""

from datetime import datetime, timedelta, timezone

import pytest

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.settings import Settings
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
        # Schema defaults: group_size 2, interval 5 → full budget of 2.
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
