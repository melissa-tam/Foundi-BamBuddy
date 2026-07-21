"""Per-printer dispatch parallelism inside one scheduler tick (latency Phase B).

Selection/gating stays sequential on the tick session; the slow per-printer work
(_start_print: FTPS upload + start command) is collected into a tick-local plan
and dispatched concurrently after both selection loops finish, bounded by the
``dispatch_parallel_limit`` semaphore. A slow upload to printer A must no longer
push printer B's dispatch to a later tick.

These tests mirror the fixture style of ``test_scheduler_hold_unpin.py`` (fresh
PrintScheduler + real ``async_session`` on the test engine, collaborators stubbed)
but stub ``_start_print`` itself so the concurrency of the gather — not the FTPS
internals — is what's under test.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

import backend.app.services.print_scheduler as ps_module
from backend.app.models.print_queue import PrintQueueItem
from backend.app.services import print_scheduler as sched_mod
from backend.app.services.print_scheduler import PrintScheduler

pytestmark = pytest.mark.asyncio


@pytest.fixture
def pd_scheduler(monkeypatch, test_engine):
    """A PrintScheduler wired for a direct-assignment dispatch tick with a stubbed _start_print."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(sched_mod, "async_session", maker)

    s = PrintScheduler()
    # Phase E: budget is owned by the module singleton — stub it there + reset the
    # in-flight set so a prior test's dispatch can't leak into this tick.
    sched_mod.stagger_policy.reset()
    monkeypatch.setattr(sched_mod.stagger_policy, "budget", AsyncMock(return_value=99))
    monkeypatch.setattr(s, "_check_auto_drying", AsyncMock())
    monkeypatch.setattr(s, "_block_on_filament_deficit", AsyncMock(return_value=False))
    monkeypatch.setattr(s, "_is_printer_idle", MagicMock(return_value=True))
    monkeypatch.setattr(s, "_read_dispatch_parallel_limit", AsyncMock(return_value=3))
    monkeypatch.setattr(ps_module.printer_manager, "is_connected", MagicMock(return_value=True))
    return s


async def _pinned_item(db, *, printer_id, pos=1, ams_mapping="[0]"):
    item = PrintQueueItem(
        printer_id=printer_id,
        ams_mapping=ams_mapping,
        target_model=None,
        status="pending",
        manual_start=False,
        filament_short=False,
        position=pos,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


def _make_fake_start(*, order, sessions, delays):
    """Return a stub for ``_start_print`` recording session identity + finish order.

    ``delays`` maps printer_id -> seconds to sleep before flipping to 'printing',
    simulating a slow FTPS upload.
    """

    async def _fake_start(session, item):
        sessions.append(id(session))
        delay = delays.get(item.printer_id, 0.0)
        if delay:
            await asyncio.sleep(delay)
        item.status = "printing"
        item.started_at = datetime.now(timezone.utc)
        await session.commit()
        order.append(item.printer_id)

    return _fake_start


async def test_slow_printer_does_not_delay_fast_printer(pd_scheduler, db_session, printer_factory, monkeypatch):
    """A slow upload on printer A and a fast one on printer B both dispatch in ONE
    tick, and B finishes first (it did not wait for A) — proof of parallelism."""
    a = await printer_factory(model="H2S")
    b = await printer_factory(model="H2S")
    a_id, b_id = a.id, b.id

    statuses = {a_id: SimpleNamespace(state="IDLE"), b_id: SimpleNamespace(state="IDLE")}
    monkeypatch.setattr(ps_module.printer_manager, "get_status", MagicMock(side_effect=lambda pid: statuses.get(pid)))

    order: list[int] = []
    sessions: list[int] = []
    monkeypatch.setattr(
        pd_scheduler,
        "_start_print",
        AsyncMock(side_effect=_make_fake_start(order=order, sessions=sessions, delays={a_id: 0.25})),
    )

    # A is planned first (lower printer id), so a serial dispatch would finish [A, B].
    item_a = await _pinned_item(db_session, printer_id=a_id, pos=1)
    item_b = await _pinned_item(db_session, printer_id=b_id, pos=2)

    started = time.monotonic()
    await pd_scheduler.check_queue()
    elapsed = time.monotonic() - started

    # Both dispatched in this single pass.
    db_session.expire_all()
    rows = {
        r.printer_id: r.status
        for r in (await db_session.execute(select(PrintQueueItem.printer_id, PrintQueueItem.status))).all()
    }
    assert rows[a_id] == "printing"
    assert rows[b_id] == "printing"

    # B (fast) completed before A (slow) — it did NOT wait for A's slow upload.
    assert order == [b_id, a_id]
    # And the whole tick took ~one slow upload, not two serialized ones.
    assert elapsed < 0.45
    # Two concurrent dispatches ran on two DISTINCT sessions (isolation).
    assert len(set(sessions)) == 2

    _ = (item_a, item_b)


async def test_semaphore_limit_one_preserves_serial_order(pd_scheduler, db_session, printer_factory, monkeypatch):
    """limit=1 collapses the gather back to serial: the slow A is planned first, so
    it acquires the sole permit and B waits — finish order is [A, B]."""
    a = await printer_factory(model="H2S")
    b = await printer_factory(model="H2S")
    a_id, b_id = a.id, b.id

    statuses = {a_id: SimpleNamespace(state="IDLE"), b_id: SimpleNamespace(state="IDLE")}
    monkeypatch.setattr(ps_module.printer_manager, "get_status", MagicMock(side_effect=lambda pid: statuses.get(pid)))
    monkeypatch.setattr(pd_scheduler, "_read_dispatch_parallel_limit", AsyncMock(return_value=1))

    order: list[int] = []
    sessions: list[int] = []
    monkeypatch.setattr(
        pd_scheduler,
        "_start_print",
        AsyncMock(side_effect=_make_fake_start(order=order, sessions=sessions, delays={a_id: 0.2})),
    )

    await _pinned_item(db_session, printer_id=a_id, pos=1)
    await _pinned_item(db_session, printer_id=b_id, pos=2)

    await pd_scheduler.check_queue()

    # Serialized by the single permit: A (planned first, slow) finishes before B.
    assert order == [a_id, b_id]


async def test_task_exception_does_not_kill_gather_or_sibling(pd_scheduler, db_session, printer_factory, monkeypatch):
    """A crashing dispatch for printer A is routed to _fail_queue_item and does not
    stop printer B's dispatch, nor propagate out of check_queue."""
    a = await printer_factory(model="H2S")
    b = await printer_factory(model="H2S")
    a_id, b_id = a.id, b.id

    statuses = {a_id: SimpleNamespace(state="IDLE"), b_id: SimpleNamespace(state="IDLE")}
    monkeypatch.setattr(ps_module.printer_manager, "get_status", MagicMock(side_effect=lambda pid: statuses.get(pid)))
    # Neutralise the farm-policy hook the failure path funnels through.
    monkeypatch.setattr("backend.app.services.farm_policy.on_terminal", AsyncMock())

    async def _start(session, item):
        if item.printer_id == a_id:
            raise RuntimeError("boom during upload")
        item.status = "printing"
        await session.commit()

    monkeypatch.setattr(pd_scheduler, "_start_print", AsyncMock(side_effect=_start))

    item_a = await _pinned_item(db_session, printer_id=a_id, pos=1)
    item_b = await _pinned_item(db_session, printer_id=b_id, pos=2)
    a_iid, b_iid = item_a.id, item_b.id

    # Must not raise despite the task exception.
    await pd_scheduler.check_queue()

    db_session.expire_all()
    row_a = (await db_session.execute(select(PrintQueueItem.status).where(PrintQueueItem.id == a_iid))).scalar_one()
    row_b = (await db_session.execute(select(PrintQueueItem.status).where(PrintQueueItem.id == b_iid))).scalar_one()
    assert row_a == "failed"  # routed to the terminal-failure path, not left 'pending'
    assert row_b == "printing"  # sibling dispatch unaffected


async def test_idempotency_guard_skips_non_pending_item(pd_scheduler, db_session, printer_factory, monkeypatch):
    """_start_print_by_id re-fetches on its own session and only proceeds if the item
    is still 'pending' — a concurrently-terminated item is skipped, _start_print
    never runs for it."""
    p = await printer_factory(model="H2S")
    p_id = p.id  # snapshot before the commit below expires the ORM object
    item = PrintQueueItem(printer_id=p_id, status="printing", plate_id=1, position=1)
    db_session.add(item)
    await db_session.commit()
    await db_session.refresh(item)
    item_id = item.id

    start_stub = AsyncMock()
    monkeypatch.setattr(pd_scheduler, "_start_print", start_stub)

    await pd_scheduler._start_print_by_id(item_id, p_id, asyncio.Semaphore(1))

    start_stub.assert_not_awaited()  # not pending → skipped
    db_session.expire_all()
    row = (await db_session.execute(select(PrintQueueItem.status).where(PrintQueueItem.id == item_id))).scalar_one()
    assert row == "printing"  # untouched


async def test_plan_dispatch_one_printer_once_guard(pd_scheduler):
    """_plan_dispatch drops a duplicate entry for a printer already in the plan."""
    plan: list[tuple[int, int]] = []
    planned: set[int] = set()
    pd_scheduler._plan_dispatch(plan, planned, item_id=10, printer_id=5)
    pd_scheduler._plan_dispatch(plan, planned, item_id=11, printer_id=5)  # duplicate printer
    pd_scheduler._plan_dispatch(plan, planned, item_id=12, printer_id=6)
    assert plan == [(10, 5), (12, 6)]
    assert planned == {5, 6}
