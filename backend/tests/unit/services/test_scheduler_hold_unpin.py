"""A sick-but-idle printer must not swallow a model-based run (funnel bug).

The model-based path of ``check_queue`` pins a unit to a chosen printer
(``item.printer_id = assigned_printer_id``) BEFORE calling ``_start_print``.
``_start_print`` then hits two self-clearing WAIT gates — the USB pre-flight
(``sdcard`` explicitly False) and the capability gate (nozzle/model/filament
mismatch). Historically those gates set ``waiting_reason`` and returned while
LEAVING the scheduler-made pin in place, so the unit permanently became a
"specific printer" item the model path never rebalanced. With NULL-first row
ordering, every still-unassigned unit was processed first, saw the same
sick-but-idle printer, got pinned, and held — funnelling the whole run onto one
broken printer, one unit per tick.

The fix: at BOTH holds, a model-targeted unit (``target_model`` truthy) releases
its scheduler-made assignment (``printer_id = None``, ``ams_mapping = None``)
before returning, so the next tick re-runs the full candidate search across the
fleet. User-pinned units (no ``target_model``) keep their printer unchanged.

These tests mirror the fixture/mocking style of
``test_scheduler_dispatch_failure_hook.py`` (direct ``_start_print`` drives) and
``test_scheduler_filament_deficit.py`` (real ``check_queue`` tick with printer
collaborators pinned).
"""

import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

import backend.app.services.print_scheduler as ps_module
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services import print_scheduler as sched_mod
from backend.app.services.capability_gate import CapabilityDecision
from backend.app.services.print_scheduler import PrintScheduler, scheduler
from backend.app.services.printer_manager import printer_manager

pytestmark = pytest.mark.asyncio


async def _mk_printer(db, name="HU"):
    p = Printer(name=name, serial_number=f"S{name}", ip_address="1.2.3.4", access_code="x", model="H2S")
    db.add(p)
    await db.flush()
    return p


@contextlib.contextmanager
def _usb_env(*, status, capability=None):
    """Patch the printer_manager surface + sinks the USB/capability holds touch.

    ``status`` is the object ``printer_manager.get_status`` returns. ``capability``
    (a ``CapabilityDecision`` or None) overrides the capability gate; None leaves
    the real gate in place. The pre-flight settle wait is zeroed.
    """
    notif = AsyncMock()
    upload = AsyncMock(return_value=True)
    ftp_retry = AsyncMock(return_value=True)
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(printer_manager, "is_connected", return_value=True))
        stack.enter_context(patch.object(printer_manager, "request_status_update", MagicMock(return_value=True)))
        stack.enter_context(patch.object(printer_manager, "get_status", MagicMock(return_value=status)))
        stack.enter_context(patch.object(ps_module, "_USB_PREFLIGHT_WAIT_S", 0))
        stack.enter_context(patch.object(ps_module.notification_service, "on_queue_job_waiting", notif))
        stack.enter_context(patch.object(ps_module, "upload_file_async", upload))
        stack.enter_context(patch.object(ps_module, "with_ftp_retry", ftp_retry))
        stack.enter_context(patch.object(scheduler, "_power_off_if_needed", AsyncMock()))
        if capability is not None:
            stack.enter_context(
                patch(
                    "backend.app.services.capability_gate.check_dispatch_capability",
                    AsyncMock(return_value=capability),
                )
            )
        yield SimpleNamespace(notif=notif, upload=upload, ftp_retry=ftp_retry)


# --------------------------------------------------------------------------- #
# Direct _start_print drives — the un-pin logic itself
# --------------------------------------------------------------------------- #
class TestUnpinOnHold:
    async def test_model_item_unpins_on_usb_hold(self, db_session):
        """#1 — model unit assigned to a USB-less printer: held pending, un-pinned."""
        printer = await _mk_printer(db_session, "M1")
        item = PrintQueueItem(
            printer_id=printer.id,
            target_model="H2S",
            ams_mapping="[0]",
            status="pending",
            plate_id=1,
            position=1,
        )
        db_session.add(item)
        await db_session.commit()

        with _usb_env(status=SimpleNamespace(sdcard=False)) as m:
            await scheduler._start_print(db_session, item)

        await db_session.refresh(item)
        assert item.status == "pending"  # a WAIT, not a failure
        assert item.waiting_reason == "no_usb_drive"
        assert item.printer_id is None  # scheduler-made pin released
        assert item.ams_mapping is None  # per-printer mapping cleared
        m.upload.assert_not_awaited()
        m.notif.assert_awaited_once()

    async def test_capability_hold_unpins_model_item(self, db_session):
        """#3 — capability BLOCK on a model unit: same un-pin outcome (USB present)."""
        printer = await _mk_printer(db_session, "M2")
        item = PrintQueueItem(
            printer_id=printer.id,
            target_model="H2S",
            ams_mapping="[0]",
            status="pending",
            plate_id=1,
            position=1,
        )
        db_session.add(item)
        await db_session.commit()

        block = CapabilityDecision(ok=False, reason="nozzle mismatch: file 0.6 vs printer 0.4")
        with _usb_env(status=SimpleNamespace(sdcard=True), capability=block) as m:
            await scheduler._start_print(db_session, item)

        await db_session.refresh(item)
        assert item.status == "pending"
        assert item.waiting_reason == "nozzle mismatch: file 0.6 vs printer 0.4"
        assert item.printer_id is None
        assert item.ams_mapping is None
        m.upload.assert_not_awaited()

    async def test_user_pinned_item_held_not_unpinned_on_usb(self, db_session):
        """#4 — user-pinned unit (no target_model): held, printer_id UNCHANGED."""
        printer = await _mk_printer(db_session, "M3")
        item = PrintQueueItem(
            printer_id=printer.id,
            target_model=None,
            ams_mapping="[0]",
            status="pending",
            plate_id=1,
            position=1,
        )
        db_session.add(item)
        await db_session.commit()

        with _usb_env(status=SimpleNamespace(sdcard=False)):
            await scheduler._start_print(db_session, item)

        await db_session.refresh(item)
        assert item.status == "pending"
        assert item.waiting_reason == "no_usb_drive"
        assert item.printer_id == printer.id  # pin preserved (byte-for-byte behavior)
        assert item.ams_mapping == "[0]"  # mapping untouched

    async def test_notifies_once_across_two_direct_holds(self, db_session):
        """#5 (hold-site level) — the notification dedup survives re-pinning.

        The model path re-pins a held unit every tick; here we simulate two ticks
        by re-assigning ``printer_id`` before the second ``_start_print`` while the
        persisted ``waiting_reason`` carries "no_usb_drive" across the calls — the
        dedup must fire the waiting notification exactly once.
        """
        printer = await _mk_printer(db_session, "M4")
        item = PrintQueueItem(printer_id=printer.id, target_model="H2S", status="pending", plate_id=1, position=1)
        db_session.add(item)
        await db_session.commit()

        with _usb_env(status=SimpleNamespace(sdcard=False)) as m:
            await scheduler._start_print(db_session, item)  # tick 1: held + un-pinned + notify
            assert item.printer_id is None
            # Tick 2: the scheduler re-pins the same unit (waiting_reason persists).
            item.printer_id = printer.id
            await db_session.commit()
            await scheduler._start_print(db_session, item)  # re-held; still un-pinned

        await db_session.refresh(item)
        assert item.printer_id is None  # re-held unit still ends un-pinned
        assert item.waiting_reason == "no_usb_drive"
        m.notif.assert_awaited_once()  # deduped on the 2nd hold


# --------------------------------------------------------------------------- #
# Hazard #3 — model retries never inherit a stale per-printer AMS mapping
# --------------------------------------------------------------------------- #
class TestModelRetryUnpinned:
    async def test_model_retry_starts_unpinned_and_unmapped(self, db_session):
        """farm_policy.create_retry_if_absent never copies ams_mapping, and returns a
        model unit's retry to the unassigned pool — so no stale-mapping leak."""
        from backend.app.services.farm_policy import create_retry_if_absent

        printer = await _mk_printer(db_session, "RT")
        item = PrintQueueItem(
            printer_id=printer.id,
            target_model="H2S",
            ams_mapping="[0]",
            status="failed",
            plate_id=1,
            position=1,
            retry_count=0,
        )
        db_session.add(item)
        await db_session.commit()

        retry = await create_retry_if_absent(db_session, item)
        assert retry is not None
        assert retry.printer_id is None  # model unit returns to the pool
        assert retry.ams_mapping is None  # never inherits the donor's per-printer mapping


# --------------------------------------------------------------------------- #
# Real check_queue tick — funnel break, redistribution, busy_printers hazard
# --------------------------------------------------------------------------- #
@pytest.fixture
def cq_scheduler(monkeypatch, test_engine):
    """A PrintScheduler wired for a model-based dispatch tick with REAL _start_print."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(sched_mod, "async_session", maker)

    s = PrintScheduler()
    monkeypatch.setattr(s, "_stagger_budget", AsyncMock(return_value=99))
    monkeypatch.setattr(s, "_check_auto_drying", AsyncMock())
    monkeypatch.setattr(s, "_get_job_name", AsyncMock(return_value="job"))
    monkeypatch.setattr(s, "_compute_ams_mapping_for_printer", AsyncMock(return_value=ps_module.MatchOutcome(mapping=[0])))
    monkeypatch.setattr(s, "_compute_deficit_safe", AsyncMock(return_value=[]))
    monkeypatch.setattr(s, "_block_on_filament_deficit", AsyncMock(return_value=False))
    monkeypatch.setattr(s, "_is_printer_idle", MagicMock(return_value=True))
    monkeypatch.setattr(s, "_power_off_if_needed", AsyncMock())
    monkeypatch.setattr(sched_mod.notification_service, "on_queue_job_assigned", AsyncMock())
    monkeypatch.setattr(sched_mod.notification_service, "on_queue_job_waiting", AsyncMock())
    # Real _start_print reaches the USB/capability gates; zero the settle wait.
    monkeypatch.setattr(ps_module, "_USB_PREFLIGHT_WAIT_S", 0)
    monkeypatch.setattr(ps_module.printer_manager, "is_connected", MagicMock(return_value=True))
    monkeypatch.setattr(ps_module.printer_manager, "request_status_update", MagicMock(return_value=True))
    monkeypatch.setattr(
        "backend.app.services.capability_gate.check_dispatch_capability",
        AsyncMock(return_value=CapabilityDecision(ok=True)),
    )
    # The healthy-path unit advances past both gates and then fails on "no source
    # file" (these units carry no archive/library) — that terminal is routed
    # through farm_policy.on_terminal, whose real relationship IO is irrelevant to
    # the funnel behaviour under test. Stub it so the tick stays focused.
    monkeypatch.setattr("backend.app.services.farm_policy.on_terminal", AsyncMock())
    return s


async def _model_item(db, *, printer_id=None, ams_mapping=None, batch_id=None, target_model="H2S", pos=1):
    item = PrintQueueItem(
        batch_id=batch_id,
        printer_id=printer_id,
        ams_mapping=ams_mapping,
        target_model=target_model,
        status="pending",
        manual_start=False,
        filament_short=False,
        position=pos,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


async def test_redistributes_to_healthy_printer_next_tick(cq_scheduler, db_session, printer_factory, monkeypatch):
    """#2 — sick printer holds the unit (un-pinned); a later tick with a healthy
    idle printer available assigns and dispatches there."""
    sick = await printer_factory(model="H2S")
    healthy = await printer_factory(model="H2S")
    sick_id, healthy_id = sick.id, healthy.id  # snapshot: avoid ORM lazy-load in the mock

    statuses = {
        sick_id: SimpleNamespace(sdcard=False, state="IDLE"),
        healthy_id: SimpleNamespace(sdcard=True, state="IDLE"),
    }
    monkeypatch.setattr(ps_module.printer_manager, "get_status", MagicMock(side_effect=lambda pid: statuses.get(pid)))

    tick = {"n": 1}

    def _find(db, model, exclude, *a_, **k_):
        # Tick 1 offers only the sick printer; tick 2 offers the healthy one.
        pick = sick_id if tick["n"] == 1 else healthy_id
        return (pick, None) if pick not in exclude else (None, "no idle printer")

    monkeypatch.setattr(cq_scheduler, "_find_idle_printer_for_model", AsyncMock(side_effect=_find))

    item = await _model_item(db_session)
    item_id = item.id

    await cq_scheduler.check_queue()  # tick 1 — held on the sick printer
    db_session.expire_all()
    row = (
        await db_session.execute(
            select(
                PrintQueueItem.printer_id,
                PrintQueueItem.ams_mapping,
                PrintQueueItem.waiting_reason,
                PrintQueueItem.status,
            ).where(PrintQueueItem.id == item_id)
        )
    ).one()
    assert row.printer_id is None  # un-pinned (funnel broken)
    assert row.ams_mapping is None
    assert row.waiting_reason == "no_usb_drive"
    assert row.status == "pending"

    tick["n"] = 2
    await cq_scheduler.check_queue()  # tick 2 — redistributes to the healthy printer
    db_session.expire_all()
    row2 = (
        await db_session.execute(
            select(PrintQueueItem.printer_id, PrintQueueItem.status, PrintQueueItem.waiting_reason).where(
                PrintQueueItem.id == item_id
            )
        )
    ).one()
    # Advanced onto the healthy printer past the USB gate (then fails on no source
    # file — proof it left the USB hold and dispatched, not that it held again).
    assert row2.printer_id == healthy_id
    assert row2.status == "failed"
    assert row2.waiting_reason is None


async def test_pinned_model_item_unpins_without_poisoning_busy_set(
    cq_scheduler, db_session, printer_factory, monkeypatch
):
    """#6 — a previously-funneled model unit (pinned at tick start) traverses the
    PINNED path, holds, un-pins; None must never enter busy_printers, and a second
    model unit in the SAME tick must still be evaluated and dispatched."""
    sick = await printer_factory(model="H2S")
    healthy = await printer_factory(model="H2S")
    sick_id, healthy_id = sick.id, healthy.id  # snapshot: avoid ORM lazy-load in the mock

    statuses = {
        sick_id: SimpleNamespace(sdcard=False, state="IDLE"),
        healthy_id: SimpleNamespace(sdcard=True, state="IDLE"),
    }
    monkeypatch.setattr(ps_module.printer_manager, "get_status", MagicMock(side_effect=lambda pid: statuses.get(pid)))

    def _find(db, model, exclude, *a_, **k_):
        # The unpinned unit's model search: the healthy printer (sick excluded once
        # it enters busy_printers after the pinned unit holds).
        if healthy_id not in exclude:
            return (healthy_id, None)
        return (None, "no idle printer")

    monkeypatch.setattr(cq_scheduler, "_find_idle_printer_for_model", AsyncMock(side_effect=_find))

    # Item A: model unit already PINNED to the sick printer (a prior funnel).
    a = await _model_item(db_session, printer_id=sick_id, ams_mapping="[0]", pos=1)
    # Item B: unpinned model unit behind it.
    b = await _model_item(db_session, pos=2)
    a_id, b_id = a.id, b.id

    await cq_scheduler.check_queue()

    db_session.expire_all()
    row_a = (
        await db_session.execute(
            select(PrintQueueItem.printer_id, PrintQueueItem.waiting_reason, PrintQueueItem.status).where(
                PrintQueueItem.id == a_id
            )
        )
    ).one()
    row_b = (
        await db_session.execute(
            select(PrintQueueItem.printer_id, PrintQueueItem.status).where(PrintQueueItem.id == b_id)
        )
    ).one()

    # A un-pinned + held (funnel released); no crash from a None in busy_printers.
    assert row_a.printer_id is None
    assert row_a.waiting_reason == "no_usb_drive"
    assert row_a.status == "pending"
    # B was still evaluated in the same tick and dispatched onto the healthy printer.
    assert row_b.printer_id == healthy_id
    assert row_b.status == "failed"  # advanced past USB, then no source file


async def test_usb_waiting_notification_deduped_across_ticks(cq_scheduler, db_session, printer_factory, monkeypatch):
    """#5 (tick level) — a unit that keeps landing on the same USB-less printer
    notifies once, not per tick (line-517 preserves the hold token)."""
    sick = await printer_factory(model="H2S")
    monkeypatch.setattr(
        ps_module.printer_manager,
        "get_status",
        MagicMock(return_value=SimpleNamespace(sdcard=False, state="IDLE")),
    )
    monkeypatch.setattr(cq_scheduler, "_find_idle_printer_for_model", AsyncMock(return_value=(sick.id, None)))

    await _model_item(db_session)

    await cq_scheduler.check_queue()  # tick 1 — transition into the hold → notify
    await cq_scheduler.check_queue()  # tick 2 — re-held on the same printer → no re-notify
    await cq_scheduler.check_queue()  # tick 3 — still held → no re-notify

    assert sched_mod.notification_service.on_queue_job_waiting.await_count == 1


# --------------------------------------------------------------------------- #
# Assigned-notification once-guard (_hold_unpinned_items)
# --------------------------------------------------------------------------- #
async def test_assigned_notification_deduped_while_hold_unpinned(
    cq_scheduler, db_session, printer_factory, monkeypatch
):
    """A — sole-idle sick printer re-selected every tick: on_queue_job_assigned has
    no dedupe of its own, so without the once-guard a lights-out farm gets an
    "assigned" notification every 30 s for hours. First assignment notifies;
    hold-release re-assignments stay silent. Waiting dedup unchanged."""
    sick = await printer_factory(model="H2S")
    monkeypatch.setattr(
        ps_module.printer_manager,
        "get_status",
        MagicMock(return_value=SimpleNamespace(sdcard=False, state="IDLE")),
    )
    monkeypatch.setattr(cq_scheduler, "_find_idle_printer_for_model", AsyncMock(return_value=(sick.id, None)))

    await _model_item(db_session)

    await cq_scheduler.check_queue()  # tick 1 — assigned (notify) → USB hold → un-pin
    await cq_scheduler.check_queue()  # tick 2 — re-assigned (silent) → re-held
    await cq_scheduler.check_queue()  # tick 3 — re-assigned (silent) → re-held

    assert sched_mod.notification_service.on_queue_job_assigned.await_count == 1
    assert sched_mod.notification_service.on_queue_job_waiting.await_count == 1


async def test_hold_unpin_guard_discarded_on_real_dispatch(cq_scheduler, db_session, printer_factory, monkeypatch):
    """B — recovery: tick 1 holds on the sick printer (id enters the guard set);
    tick 2 the same unit lands on a healthy printer and DISPATCHES — the guard id
    is discarded so a future hold on a new assignment is a fresh transition."""
    sick = await printer_factory(model="H2S")
    healthy = await printer_factory(model="H2S")
    sick_id, healthy_id = sick.id, healthy.id  # snapshot: avoid ORM lazy-load in the mock

    statuses = {
        sick_id: SimpleNamespace(sdcard=False, state="IDLE"),
        healthy_id: SimpleNamespace(sdcard=True, state="IDLE"),
    }
    monkeypatch.setattr(ps_module.printer_manager, "get_status", MagicMock(side_effect=lambda pid: statuses.get(pid)))

    tick = {"n": 1}

    def _find(db, model, exclude, *a_, **k_):
        pick = sick_id if tick["n"] == 1 else healthy_id
        return (pick, None) if pick not in exclude else (None, "no idle printer")

    monkeypatch.setattr(cq_scheduler, "_find_idle_printer_for_model", AsyncMock(side_effect=_find))

    item = await _model_item(db_session)
    item_id = item.id

    await cq_scheduler.check_queue()  # tick 1 — held + un-pinned + registered in the guard
    assert item_id in cq_scheduler._hold_unpinned_items

    # Tick 2 — the healthy printer is offered; stub _start_print with the
    # dispatch-success shape (status → "printing") so the model path's
    # discard-on-dispatch is exercised (the real _start_print would fail these
    # sourceless test units downstream of the gates).
    started: dict = {}

    async def _start(db, it):
        it.status = "printing"
        started["printer_id"] = it.printer_id
        await db.commit()

    tick["n"] = 2
    monkeypatch.setattr(cq_scheduler, "_start_print", AsyncMock(side_effect=_start))
    await cq_scheduler.check_queue()

    assert started.get("printer_id") == healthy_id  # dispatch happened, on the healthy printer
    assert item_id not in cq_scheduler._hold_unpinned_items  # guard discarded on real dispatch


async def test_normal_assignment_still_notifies(cq_scheduler, db_session, printer_factory, monkeypatch):
    """C — legit-path regression: a unit that waited (no candidate) on tick 1 and
    assigns on tick 2 still notifies exactly once — the guard only suppresses
    re-assignments born from a hold-release, never normal assignments."""
    healthy = await printer_factory(model="H2S")
    healthy_id = healthy.id  # snapshot: avoid ORM lazy-load in the mock
    monkeypatch.setattr(
        ps_module.printer_manager,
        "get_status",
        MagicMock(return_value=SimpleNamespace(sdcard=True, state="IDLE")),
    )

    tick = {"n": 1}

    def _find(db, model, exclude, *a_, **k_):
        if tick["n"] == 1:
            return (None, "Busy: all printers busy")
        return (healthy_id, None) if healthy_id not in exclude else (None, "no idle printer")

    monkeypatch.setattr(cq_scheduler, "_find_idle_printer_for_model", AsyncMock(side_effect=_find))

    await _model_item(db_session)

    await cq_scheduler.check_queue()  # tick 1 — no candidate → waits
    assert sched_mod.notification_service.on_queue_job_assigned.await_count == 0

    tick["n"] = 2
    await cq_scheduler.check_queue()  # tick 2 — assigned → notify fires

    assert sched_mod.notification_service.on_queue_job_assigned.await_count == 1
