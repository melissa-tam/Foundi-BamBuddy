"""Stagger policy module (latency Phase E): bed-temperature dynamic release,
the in-flight set as the single admission source of truth, and the ramp-watch
bed-at-target kick.

Migrated from ``test_scheduler_stagger.py``'s ``TestStaggerBudget`` /
``TestStaggerDefaultInterval`` (which exercised the deleted
``PrintScheduler._stagger_budget``) — the pure-time-window / ceiling / restart
cases are preserved here against the module with dynamic release OFF, and the
new dynamic-release + in-flight + ramp-watch behaviour is added.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import backend.app.services.stagger as stagger_mod
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.settings import Settings
from backend.app.services.stagger import StaggerPolicy, stagger_policy

# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _reset_policy():
    """Every test starts from a clean in-flight set / ramp-watch / tunable cache."""
    stagger_policy.reset()
    yield
    stagger_policy.reset()


class FakeStatus:
    """Minimal stand-in for ``PrinterState`` — only what ``budget`` reads."""

    def __init__(self, *, connected=True, bed=None, bed_target=None):
        self.connected = connected
        self.temperatures: dict = {}
        if bed is not None:
            self.temperatures["bed"] = bed
        if bed_target is not None:
            self.temperatures["bed_target"] = bed_target


def _patch_status(monkeypatch, status_map: dict):
    """Route ``printer_manager.get_status(pid)`` to a fixed map (missing → None)."""
    monkeypatch.setattr(stagger_mod.printer_manager, "get_status", lambda pid: status_map.get(pid))


async def _set(db, key, value):
    db.add(Settings(key=key, value=str(value)))
    await db.commit()


async def _add_started_item(db, *, minutes_ago, printer_id=None) -> int:
    item = PrintQueueItem(
        status="printing",
        printer_id=printer_id,
        started_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item.id


# --------------------------------------------------------------------------- #
# Pure time-window / ceiling / restart (migrated — dynamic release OFF so the
# occupancy is the exact legacy inside-window count)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestPureWindowBudget:
    async def test_defaults_when_no_settings_no_starts(self, db_session):
        # Schema defaults: group_size 2, interval 3 → full budget of 2.
        assert await stagger_policy.budget(db_session) == 2

    async def test_only_starts_inside_window_count(self, db_session):
        await _set(db_session, "stagger_dynamic_release", "false")
        await _set(db_session, "stagger_group_size", 3)
        await _set(db_session, "stagger_interval_minutes", 5)
        await _add_started_item(db_session, minutes_ago=1)  # inside window
        await _add_started_item(db_session, minutes_ago=60)  # outside window
        assert await stagger_policy.budget(db_session) == 2

    async def test_budget_exhausted_when_window_full(self, db_session):
        await _set(db_session, "stagger_dynamic_release", "false")
        await _set(db_session, "stagger_group_size", 2)
        await _set(db_session, "stagger_interval_minutes", 5)
        await _add_started_item(db_session, minutes_ago=1)
        await _add_started_item(db_session, minutes_ago=2)
        assert await stagger_policy.budget(db_session) == 0

    async def test_never_negative(self, db_session):
        await _set(db_session, "stagger_dynamic_release", "false")
        await _set(db_session, "stagger_group_size", 1)
        await _set(db_session, "stagger_interval_minutes", 10)
        for m in (1, 2, 3):
            await _add_started_item(db_session, minutes_ago=m)
        assert await stagger_policy.budget(db_session) == 0

    async def test_high_group_size_effectively_disables(self, db_session):
        await _set(db_session, "stagger_dynamic_release", "false")
        await _set(db_session, "stagger_group_size", 50)
        await _set(db_session, "stagger_interval_minutes", 5)
        for _ in range(3):
            await _add_started_item(db_session, minutes_ago=1)
        assert await stagger_policy.budget(db_session) == 47

    async def test_restart_derives_budget_by_query_not_memory(self, db_session):
        await _set(db_session, "stagger_dynamic_release", "false")
        await _set(db_session, "stagger_group_size", 3)
        await _set(db_session, "stagger_interval_minutes", 5)
        await _add_started_item(db_session, minutes_ago=1)
        await _add_started_item(db_session, minutes_ago=2)
        # Two fresh instances (a "restart") see the SAME budget for the same DB.
        budget_a = await StaggerPolicy().budget(db_session)
        budget_b = await StaggerPolicy().budget(db_session)
        assert budget_a == budget_b == 1

    async def test_interval_widens_window_to_include_more_starts(self, db_session):
        await _set(db_session, "stagger_dynamic_release", "false")
        await _set(db_session, "stagger_group_size", 5)
        await _set(db_session, "stagger_interval_minutes", 30)
        await _add_started_item(db_session, minutes_ago=1)
        await _add_started_item(db_session, minutes_ago=20)  # inside the wider window
        assert await stagger_policy.budget(db_session) == 3

    async def test_default_interval_3_excludes_older_starts(self, db_session):
        # Default interval is 3 min: a start 4 min ago falls OUTSIDE the window.
        await _set(db_session, "stagger_dynamic_release", "false")
        await _set(db_session, "stagger_group_size", 2)
        await _add_started_item(db_session, minutes_ago=4)
        assert await stagger_policy.budget(db_session) == 2


# --------------------------------------------------------------------------- #
# staggering-off semantics (group_size<=0 / interval<=0) preserved
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestStaggeringOff:
    async def test_group_size_zero_returns_huge_budget(self, db_session):
        await _set(db_session, "stagger_group_size", 0)
        await _set(db_session, "stagger_interval_minutes", 5)
        assert await stagger_policy.budget(db_session) == 1_000_000

    async def test_interval_zero_returns_group_size(self, db_session):
        await _set(db_session, "stagger_group_size", 2)
        await _set(db_session, "stagger_interval_minutes", 0)
        assert await stagger_policy.budget(db_session) == 2


# --------------------------------------------------------------------------- #
# Dynamic release: occupancy judged from live bed temperature
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestDynamicRelease:
    async def _setup(self, db_session, *, group_size=2, interval=5):
        await _set(db_session, "stagger_group_size", group_size)
        await _set(db_session, "stagger_interval_minutes", interval)
        # dynamic release is ON by default — no setting needed.

    async def test_ramping_below_target_occupies(self, db_session, monkeypatch):
        await self._setup(db_session)
        pid = await _add_started_item(db_session, minutes_ago=1, printer_id=10)  # noqa: F841
        _patch_status(monkeypatch, {10: FakeStatus(bed=30.0, bed_target=60.0)})
        # 30 < 60 - 2 → still ramping → occupies → 2 - 1 = 1.
        assert await stagger_policy.budget(db_session) == 1

    async def test_at_target_releases(self, db_session, monkeypatch):
        await self._setup(db_session)
        await _add_started_item(db_session, minutes_ago=1, printer_id=10)
        _patch_status(monkeypatch, {10: FakeStatus(bed=59.0, bed_target=60.0)})
        # 59 >= 60 - 2 → released → full budget 2.
        assert await stagger_policy.budget(db_session) == 2

    async def test_bed_target_zero_inside_grace_occupies(self, db_session, monkeypatch):
        await self._setup(db_session, interval=10)
        await _add_started_item(db_session, minutes_ago=1, printer_id=10)  # 60 s < 120 grace
        _patch_status(monkeypatch, {10: FakeStatus(bed=25.0, bed_target=0.0)})
        assert await stagger_policy.budget(db_session) == 1

    async def test_bed_target_zero_past_grace_releases(self, db_session, monkeypatch):
        await self._setup(db_session, interval=10)
        await _add_started_item(db_session, minutes_ago=3, printer_id=10)  # 180 s > 120 grace
        _patch_status(monkeypatch, {10: FakeStatus(bed=25.0, bed_target=0.0)})
        assert await stagger_policy.budget(db_session) == 2

    async def test_no_status_occupies(self, db_session, monkeypatch):
        await self._setup(db_session)
        await _add_started_item(db_session, minutes_ago=1, printer_id=10)
        _patch_status(monkeypatch, {})  # get_status → None
        assert await stagger_policy.budget(db_session) == 1

    async def test_disconnected_occupies(self, db_session, monkeypatch):
        await self._setup(db_session)
        await _add_started_item(db_session, minutes_ago=1, printer_id=10)
        _patch_status(monkeypatch, {10: FakeStatus(connected=False, bed=59.0, bed_target=60.0)})
        # Disconnected → fail-safe occupied even though bed reads at target.
        assert await stagger_policy.budget(db_session) == 1

    async def test_outside_window_never_occupies_even_when_ramping(self, db_session, monkeypatch):
        await self._setup(db_session, interval=5)
        await _add_started_item(db_session, minutes_ago=60, printer_id=10)  # outside window
        _patch_status(monkeypatch, {10: FakeStatus(bed=10.0, bed_target=60.0)})  # cold/ramping
        # Ceiling: outside the window it never occupies, regardless of bed temp.
        assert await stagger_policy.budget(db_session) == 2

    async def test_dynamic_off_ignores_bed_temp(self, db_session, monkeypatch):
        await _set(db_session, "stagger_dynamic_release", "false")
        await self._setup(db_session)
        await _add_started_item(db_session, minutes_ago=1, printer_id=10)
        # Bed at target — under dynamic ON this would release; OFF → still occupies.
        _patch_status(monkeypatch, {10: FakeStatus(bed=60.0, bed_target=60.0)})
        assert await stagger_policy.budget(db_session) == 1


# --------------------------------------------------------------------------- #
# In-flight set: planned-but-not-settled dispatches occupy the budget
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestInFlight:
    async def test_planned_not_settled_occupies(self, db_session):
        await _set(db_session, "stagger_group_size", 2)
        stagger_policy.note_dispatch_planned(printer_id=1, item_id=100)
        # No started rows yet, but the planned dispatch charges the budget.
        assert await stagger_policy.budget(db_session) == 1

    async def test_settle_frees_the_slot(self, db_session):
        await _set(db_session, "stagger_group_size", 2)
        stagger_policy.note_dispatch_planned(printer_id=1, item_id=100)
        stagger_policy.note_dispatch_settled(item_id=100)  # failure/skip/hold or success
        assert await stagger_policy.budget(db_session) == 2

    async def test_kick_mid_gather_never_over_admits(self, db_session):
        # Two dispatches in flight against a group of 2 → budget 0. A kick landing
        # here (recomputing budget) must NOT admit a third heater.
        await _set(db_session, "stagger_group_size", 2)
        stagger_policy.note_dispatch_planned(printer_id=1, item_id=100)
        stagger_policy.note_dispatch_planned(printer_id=2, item_id=101)
        assert await stagger_policy.budget(db_session) == 0

    async def test_in_flight_item_not_double_charged_with_its_started_row(self, db_session, monkeypatch):
        # During the started_at handoff an item can momentarily have BOTH an
        # in-flight entry and a started row — it must count once, not twice.
        await _set(db_session, "stagger_group_size", 2)
        item_id = await _add_started_item(db_session, minutes_ago=0, printer_id=10)
        _patch_status(monkeypatch, {10: FakeStatus(bed=10.0, bed_target=60.0)})  # ramping
        stagger_policy.note_dispatch_planned(printer_id=10, item_id=item_id)
        # in-flight charges 1; the started row is excluded (same id) → budget 1, not 0.
        assert await stagger_policy.budget(db_session) == 1


# --------------------------------------------------------------------------- #
# Ramp-watch: on_status_push fires a one-shot bed-at-target kick
# --------------------------------------------------------------------------- #


class TestRampWatch:
    def test_fires_once_when_bed_crosses_target(self, monkeypatch):
        kick = Mock()
        monkeypatch.setattr(stagger_mod.dispatch_kick, "kick", kick)
        stagger_policy.note_dispatch_planned(printer_id=5, item_id=1)
        # Below target − epsilon: no kick yet.
        stagger_policy.on_status_push(5, FakeStatus(bed=30.0, bed_target=60.0))
        kick.assert_not_called()
        # Crosses target − epsilon: fires exactly one bed_at_target kick.
        stagger_policy.on_status_push(5, FakeStatus(bed=59.0, bed_target=60.0))
        kick.assert_called_once_with("bed_at_target", 5)

    def test_unwatched_printer_is_a_noop(self, monkeypatch):
        kick = Mock()
        monkeypatch.setattr(stagger_mod.dispatch_kick, "kick", kick)
        stagger_policy.on_status_push(999, FakeStatus(bed=59.0, bed_target=60.0))
        kick.assert_not_called()

    def test_second_crossing_without_rearm_does_not_refire(self, monkeypatch):
        kick = Mock()
        monkeypatch.setattr(stagger_mod.dispatch_kick, "kick", kick)
        stagger_policy.note_dispatch_planned(printer_id=5, item_id=1)
        stagger_policy.on_status_push(5, FakeStatus(bed=59.0, bed_target=60.0))
        stagger_policy.on_status_push(5, FakeStatus(bed=60.0, bed_target=60.0))
        kick.assert_called_once()

    def test_entry_expires_at_window_end(self, monkeypatch):
        kick = Mock()
        monkeypatch.setattr(stagger_mod.dispatch_kick, "kick", kick)
        stagger_policy.note_dispatch_planned(printer_id=5, item_id=1)
        # Age the watch past the cached window ceiling.
        stagger_policy._ramp_watch[5].armed_at -= stagger_policy._window_seconds + 1
        stagger_policy.on_status_push(5, FakeStatus(bed=59.0, bed_target=60.0))
        kick.assert_not_called()
        assert 5 not in stagger_policy._ramp_watch  # lazily pruned

    def test_dynamic_off_suppresses_kick(self, monkeypatch):
        kick = Mock()
        monkeypatch.setattr(stagger_mod.dispatch_kick, "kick", kick)
        stagger_policy._dynamic = False  # cache reflects dynamic release OFF
        stagger_policy.note_dispatch_planned(printer_id=5, item_id=1)
        stagger_policy.on_status_push(5, FakeStatus(bed=59.0, bed_target=60.0))
        kick.assert_not_called()


# --------------------------------------------------------------------------- #
# Settle is guaranteed by _start_print_by_id's finally (integration)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_settle_guaranteed_when_start_print_raises(test_engine, monkeypatch):
    import backend.app.services.print_scheduler as sched_mod
    from backend.app.services.print_scheduler import PrintScheduler

    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(sched_mod, "async_session", maker)

    async with maker() as session:
        item = PrintQueueItem(status="pending", printer_id=7)
        session.add(item)
        await session.commit()
        await session.refresh(item)
        item_id = item.id

    sched = PrintScheduler()
    monkeypatch.setattr(sched, "_start_print", AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(sched, "_fail_queue_item", AsyncMock())

    # Simulate the plan step entering the in-flight set.
    stagger_policy.note_dispatch_planned(printer_id=7, item_id=item_id)
    assert stagger_policy.in_flight_count == 1

    import asyncio

    await sched._start_print_by_id(item_id, 7, asyncio.Semaphore(1))

    # Despite the crash, the finally settled the slot.
    assert stagger_policy.in_flight_count == 0
