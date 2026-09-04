"""A pending row's ``printer_id`` is an operator PIN — the scheduler never writes one.

This file used to be about UN-PINNING. The model path wrote its pick onto the pending
row (``item.printer_id = assigned_printer_id``) and three ad-hoc guards released it
again when a hold gate stopped the dispatch, so that a sick-but-idle printer could not
become a unit's permanent home. That worked only where a guard was reached: a refused
dispatch-lease claim left the row pinned to a busy printer with nothing to undo it, and
the row then entered the PINNED branch on every later tick and waited forever.

The 2026-09-04 pool-target wave removed the write instead of adding a fourth guard. The
decided printer rides the dispatch plan — ``_PlannedDispatch`` → ``_start_print_by_id``
→ ``_start_print(printer_id=…)`` — exactly as the decided ``ams_mapping`` already did,
and lands on the row only at the ``pending → printing`` claim
(``queue_transitions.claim_pending_for_dispatch``) or, on a failure, as the attribution
``_fail_queue_item`` records. So there is nothing to un-pin, and every scenario below is
re-expressed against that invariant: a POOL unit's row carries no printer because none
was ever written, and a PINNED unit's row keeps its because a human put it there.

The four target kinds and the invariant itself live in
``backend/app/services/dispatch_target.py``.

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
from backend.app.services.dispatch_target import DispatchTarget, encode_printer_ids
from backend.app.services.plate_occupancy import plate_occupancy
from backend.app.services.print_scheduler import PrintScheduler, scheduler
from backend.app.services.printer_manager import printer_manager

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clean_authority():
    """Every tick that plans a dispatch claims a printer LEASE on the process-wide
    occupancy authority. A HELD unit never commits one (and ``_start_print_by_id``'s
    finally releases it), but a leftover claim from a previous case would make the
    printer undispatchable and turn a funnel test into a false pass.

    The module singleton's held-pool once-guard is process-wide for the same reason and
    is cleared alongside it — an id left behind by one case would silently suppress the
    next case's "assigned" notification."""
    plate_occupancy.reset_for_tests()
    scheduler._held_pool_items.clear()
    yield
    plate_occupancy.reset_for_tests()
    scheduler._held_pool_items.clear()


async def _mk_printer(db, name="HU"):
    p = Printer(name=name, serial_number=f"S{name}", ip_address="1.2.3.4", access_code="x", model="H2S")
    db.add(p)
    await db.flush()
    return p


def _pool_columns(kind: str, printer_id: int) -> dict:
    """The target columns for a POOL unit of *kind*, over a pool containing *printer_id*.

    Both kinds are POOL and take the identical lane, which is the point of the
    parametrisation: MODEL says "any H2S", PRINTERS says "any of this id set", and
    neither carries a ``printer_id`` while pending.
    """
    if kind == "model":
        return {"printer_id": None, "target_model": "H2S", "target_printer_ids": None}
    return {"printer_id": None, "target_model": None, "target_printer_ids": encode_printer_ids([printer_id])}


@contextlib.contextmanager
def _usb_env(*, status, capability=None):
    """Patch the printer_manager surface + sinks the USB/capability holds touch.

    ``status`` is the object ``printer_manager.get_status`` returns. ``capability``
    (a ``CapabilityDecision`` or None) overrides the capability gate; None leaves
    the real gate in place. ``get_client`` is stubbed to ``None`` so the smart
    pre-flight takes the no-client path (request, no event wait) — no sleep.
    """
    notif = AsyncMock()
    upload = AsyncMock(return_value=True)
    ftp_retry = AsyncMock(return_value=True)
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(printer_manager, "is_connected", return_value=True))
        stack.enter_context(patch.object(printer_manager, "request_status_update", MagicMock(return_value=True)))
        stack.enter_context(patch.object(printer_manager, "get_status", MagicMock(return_value=status)))
        stack.enter_context(patch.object(printer_manager, "get_client", MagicMock(return_value=None)))
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
# Direct _start_print drives — the invariant itself
# --------------------------------------------------------------------------- #
class TestPoolUnitNeverCarriesThePick:
    @pytest.mark.parametrize("kind", ["model", "printers"])
    async def test_pool_unit_leaves_usb_hold_with_no_printer_on_the_row(self, db_session, kind):
        """#1 — a POOL unit dispatched onto a USB-less printer: held pending, and the
        row carries no printer because none was ever written to it."""
        printer = await _mk_printer(db_session, f"M1{kind}")
        item = PrintQueueItem(
            **_pool_columns(kind, printer.id),
            ams_mapping="[0]",
            status="pending",
            plate_id=1,
            position=1,
        )
        db_session.add(item)
        await db_session.commit()

        with _usb_env(status=SimpleNamespace(sdcard=False)) as m:
            await scheduler._start_print(db_session, item, printer_id=printer.id)

        await db_session.refresh(item)
        assert item.status == "pending"  # a WAIT, not a failure
        assert item.waiting_reason == "no_usb_drive"
        assert item.printer_id is None  # never written — the pick rode the argument
        # ams_mapping is PRESERVED (2026-08-12 contract). It used to be cleared here as
        # a per-printer derivation to invalidate; it is now the operator's slot
        # instruction, and a missing USB stick is no reason to discard what a human
        # asked for. The matcher re-decides per candidate on the next tick anyway.
        assert item.ams_mapping == "[0]"
        # The once-guard holds the id so the next tick's identical re-pick stays silent.
        assert item.id in scheduler._held_pool_items
        # Nothing was claimed: the hold sits far above the point of no return.
        assert plate_occupancy.snapshot(printer.id).lease_unit_id is None
        m.upload.assert_not_awaited()
        m.notif.assert_awaited_once()

    @pytest.mark.parametrize("kind", ["model", "printers"])
    async def test_pool_unit_leaves_capability_hold_with_no_printer_on_the_row(self, db_session, kind):
        """#3 — capability BLOCK on a POOL unit: same outcome, USB present."""
        printer = await _mk_printer(db_session, f"M2{kind}")
        item = PrintQueueItem(
            **_pool_columns(kind, printer.id),
            ams_mapping="[0]",
            status="pending",
            plate_id=1,
            position=1,
        )
        db_session.add(item)
        await db_session.commit()

        block = CapabilityDecision(ok=False, reason="nozzle mismatch: file 0.6 vs printer 0.4")
        with _usb_env(status=SimpleNamespace(sdcard=True), capability=block) as m:
            await scheduler._start_print(db_session, item, printer_id=printer.id)

        await db_session.refresh(item)
        assert item.status == "pending"
        assert item.waiting_reason == "nozzle mismatch: file 0.6 vs printer 0.4"
        assert item.printer_id is None
        # Preserved for the same reason as the USB hold above — see that comment.
        assert item.ams_mapping == "[0]"
        assert item.id in scheduler._held_pool_items
        assert plate_occupancy.snapshot(printer.id).lease_unit_id is None
        m.upload.assert_not_awaited()

    async def test_user_pinned_item_holds_in_place_with_its_pin(self, db_session):
        """#4 — user-pinned unit (no pool columns): held, printer_id UNCHANGED.

        The pin predates the dispatch and is a human's instruction, so an unmet
        precondition must not silently move the unit to another machine. It also stays
        OUT of the once-guard: nothing re-picks a pinned unit, so there is no repeated
        "assigned" notification to suppress.
        """
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
            await scheduler._start_print(db_session, item, printer_id=printer.id)

        await db_session.refresh(item)
        assert item.status == "pending"
        assert item.waiting_reason == "no_usb_drive"
        assert item.printer_id == printer.id  # pin preserved (byte-for-byte behavior)
        assert item.ams_mapping == "[0]"  # mapping untouched
        assert item.id not in scheduler._held_pool_items

    async def test_notifies_once_across_two_direct_holds(self, db_session):
        """#5 (hold-site level) — the notification dedup survives a repeated pick.

        The pool path re-picks a held unit every tick; here we simulate two ticks by
        calling ``_start_print`` twice with the same printer argument while the
        persisted ``waiting_reason`` carries "no_usb_drive" across the calls — the
        dedup must fire the waiting notification exactly once, and the row must carry
        no printer either time.
        """
        printer = await _mk_printer(db_session, "M4")
        item = PrintQueueItem(target_model="H2S", status="pending", plate_id=1, position=1)
        db_session.add(item)
        await db_session.commit()

        with _usb_env(status=SimpleNamespace(sdcard=False)) as m:
            await scheduler._start_print(db_session, item, printer_id=printer.id)  # tick 1: held + notify
            assert item.printer_id is None
            await scheduler._start_print(db_session, item, printer_id=printer.id)  # tick 2: re-held, silent

        await db_session.refresh(item)
        assert item.printer_id is None  # re-held unit still carries no printer
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
# The pool finder — membership is the target's question
# --------------------------------------------------------------------------- #
class TestFindIdlePrinterForTarget:
    @staticmethod
    def _sched(monkeypatch, *, idle: dict[int, bool]):
        s = PrintScheduler()
        monkeypatch.setattr(ps_module.printer_manager, "is_connected", MagicMock(return_value=True))
        monkeypatch.setattr(s, "_is_printer_idle", MagicMock(side_effect=lambda pid: idle.get(pid, True)))
        return s

    async def test_returns_the_lowest_id_idle_member(self, db_session, printer_factory, monkeypatch):
        """A pool is walked in one stable order (``ORDER BY Printer.id``), so the same
        set of equally eligible printers always answers the same way."""
        a = await printer_factory(model="H2S")
        b = await printer_factory(model="H2S")
        s = self._sched(monkeypatch, idle={})

        found, reason = await s._find_idle_printer_for_target(db_session, DispatchTarget.printers([b.id, a.id]), set())
        assert found == min(a.id, b.id)
        assert reason is None

    async def test_skips_a_busy_member_and_returns_the_next(self, db_session, printer_factory, monkeypatch):
        a = await printer_factory(model="H2S")
        b = await printer_factory(model="H2S")
        s = self._sched(monkeypatch, idle={a.id: False})

        found, reason = await s._find_idle_printer_for_target(db_session, DispatchTarget.printers([a.id, b.id]), set())
        assert found == b.id
        assert reason is None

    async def test_never_returns_a_same_model_non_member(self, db_session, printer_factory, monkeypatch):
        """The whole point of a PRINTERS pool: an idle printer of the right MODEL that
        the operator did not choose is not a candidate."""
        a = await printer_factory(model="H2S")
        outsider = await printer_factory(model="H2S")
        c = await printer_factory(model="H2S")
        s = self._sched(monkeypatch, idle={a.id: False})

        found, _ = await s._find_idle_printer_for_target(db_session, DispatchTarget.printers([a.id, c.id]), set())
        assert found == c.id
        assert found != outsider.id

    async def test_all_busy_pool_reports_the_busy_names(self, db_session, printer_factory, monkeypatch):
        a = await printer_factory(model="H2S", name="001-H2S")
        b = await printer_factory(model="H2S", name="003-H2S")
        s = self._sched(monkeypatch, idle={a.id: False, b.id: False})

        found, reason = await s._find_idle_printer_for_target(db_session, DispatchTarget.printers([a.id, b.id]), set())
        assert found is None
        assert reason == "Busy: 001-H2S, 003-H2S"

    async def test_inactive_pool_names_its_members(self, db_session, printer_factory, monkeypatch):
        """Nothing survives the membership+active filter, so the sentence has to name
        the pool itself — a model string could not describe one."""
        a = await printer_factory(model="H2S", name="001-H2S")
        b = await printer_factory(model="H2S", name="003-H2S")
        a.is_active = False
        b.is_active = False
        await db_session.commit()
        s = self._sched(monkeypatch, idle={})

        found, reason = await s._find_idle_printer_for_target(db_session, DispatchTarget.printers([a.id, b.id]), set())
        assert found is None
        assert reason == "No active printers among 001-H2S, 003-H2S"


# --------------------------------------------------------------------------- #
# Real check_queue tick — funnel break, redistribution, busy_printers hazard
# --------------------------------------------------------------------------- #
@pytest.fixture
def cq_scheduler(monkeypatch, test_engine):
    """A PrintScheduler wired for a pool dispatch tick with REAL _start_print."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(sched_mod, "async_session", maker)

    s = PrintScheduler()
    # Phase E: budget moved to the module singleton — stub it there + reset state.
    sched_mod.stagger_policy.reset()
    monkeypatch.setattr(sched_mod.stagger_policy, "budget", AsyncMock(return_value=99))
    monkeypatch.setattr(s, "_check_auto_drying", AsyncMock())
    monkeypatch.setattr(s, "_get_job_name", AsyncMock(return_value="job"))
    monkeypatch.setattr(
        s, "_compute_ams_mapping_for_printer", AsyncMock(return_value=ps_module.MatchOutcome(mapping=[0]))
    )
    monkeypatch.setattr(s, "_compute_deficit_safe", AsyncMock(return_value=[]))
    monkeypatch.setattr(s, "_block_on_filament_deficit", AsyncMock(return_value=False))
    monkeypatch.setattr(s, "_is_printer_idle", MagicMock(return_value=True))
    monkeypatch.setattr(s, "_power_off_if_needed", AsyncMock())
    monkeypatch.setattr(sched_mod.notification_service, "on_queue_job_assigned", AsyncMock())
    monkeypatch.setattr(sched_mod.notification_service, "on_queue_job_waiting", AsyncMock())
    # Real _start_print reaches the USB/capability gates; stub get_client to None so
    # the smart pre-flight takes the no-client path (request, no event wait) — no sleep.
    monkeypatch.setattr(ps_module.printer_manager, "is_connected", MagicMock(return_value=True))
    monkeypatch.setattr(ps_module.printer_manager, "request_status_update", MagicMock(return_value=True))
    monkeypatch.setattr(ps_module.printer_manager, "get_client", MagicMock(return_value=None))
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


async def _model_item(
    db,
    *,
    printer_id=None,
    ams_mapping=None,
    batch_id=None,
    target_model="H2S",
    target_printer_ids=None,
    pos=1,
):
    item = PrintQueueItem(
        batch_id=batch_id,
        printer_id=printer_id,
        ams_mapping=ams_mapping,
        target_model=target_model,
        target_printer_ids=target_printer_ids,
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
    """#2 — a sick printer holds the unit; a later tick with a healthy idle printer
    available picks it and dispatches there."""
    sick = await printer_factory(model="H2S")
    healthy = await printer_factory(model="H2S")
    sick_id, healthy_id = sick.id, healthy.id  # snapshot: avoid ORM lazy-load in the mock

    statuses = {
        sick_id: SimpleNamespace(sdcard=False, state="IDLE"),
        healthy_id: SimpleNamespace(sdcard=True, state="IDLE"),
    }
    monkeypatch.setattr(ps_module.printer_manager, "get_status", MagicMock(side_effect=lambda pid: statuses.get(pid)))

    tick = {"n": 1}

    def _find(db, target, exclude, *a_, **k_):
        # Tick 1 offers only the sick printer; tick 2 offers the healthy one.
        pick = sick_id if tick["n"] == 1 else healthy_id
        return (pick, None) if pick not in exclude else (None, "no idle printer")

    monkeypatch.setattr(cq_scheduler, "_find_idle_printer_for_target", AsyncMock(side_effect=_find))

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
    # No printer on the row — not because a hold gate released one, but because the
    # tick's pick was NEVER written there (it rode the plan into _start_print).
    assert row.printer_id is None
    assert row.ams_mapping is None
    assert row.waiting_reason == "no_usb_drive"
    assert row.status == "pending"
    # The PRINTER claim released with the dispatch: a dispatch that stopped at a hold
    # never reached the print command, so the printer must go straight back to the
    # queue rather than sit claimed for the full hold ceiling.
    assert plate_occupancy.snapshot(sick_id).lease_unit_id is None

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
    # Advanced onto the healthy printer past the USB gate, then failed on no source
    # file. The row names the printer ONLY because it left 'pending': _fail_queue_item
    # records the printer a dispatch failed on (farm_policy's consecutive-failure count
    # reads it). A still-pending row would carry None here.
    assert row2.status == "failed"
    assert row2.printer_id == healthy_id
    assert row2.waiting_reason is None


async def test_stale_pinned_pool_row_takes_the_pool_path_without_poisoning_busy_set(
    cq_scheduler, db_session, printer_factory, monkeypatch
):
    """#6 — a pre-cutover row carrying BOTH a pool target and a leftover printer_id is
    still the POOL kind (``target_of`` precedence), so it is re-searched rather than
    frozen onto the machine some earlier tick happened to pick. None must never enter
    busy_printers, and a second pool unit in the SAME tick must still be dispatched.

    The stale value is deliberately NOT scrubbed. This capsule removed the WRITE, not
    added a cleaner: nothing on the dispatch path reads a pool row's ``printer_id``, so
    a leftover is inert, and a pass that went around NULLing pool rows would be the
    same "guard that must remember" the write's removal exists to eliminate. What the
    tick must never do is OBEY it — which is what the sick-printer hold below proves,
    because the stale home is idle and USB-healthy and would have dispatched at once
    had the row been read as PINNED.
    """
    stale_home = await printer_factory(model="H2S")
    sick = await printer_factory(model="H2S")
    healthy = await printer_factory(model="H2S")
    stale_id, sick_id, healthy_id = stale_home.id, sick.id, healthy.id  # snapshot: no ORM lazy-load in the mock

    statuses = {
        stale_id: SimpleNamespace(sdcard=True, state="IDLE"),  # would happily take the print
        sick_id: SimpleNamespace(sdcard=False, state="IDLE"),
        healthy_id: SimpleNamespace(sdcard=True, state="IDLE"),
    }
    monkeypatch.setattr(ps_module.printer_manager, "get_status", MagicMock(side_effect=lambda pid: statuses.get(pid)))

    def _find(db, target, exclude, *a_, **k_):
        # Offer the healthy printer first, the sick one as the fallback, and NEVER the
        # stale home — so a dispatch onto it could only come from obeying the row.
        # Order-free on purpose: the pending query sorts by ``printer_id`` (NULLs
        # first), so the clean row is always evaluated before the stale-pinned one.
        if healthy_id not in exclude:
            return (healthy_id, None)
        if sick_id not in exclude:
            return (sick_id, None)
        return (None, "no idle printer")

    monkeypatch.setattr(cq_scheduler, "_find_idle_printer_for_target", AsyncMock(side_effect=_find))

    # Item A: pool unit with a STALE printer_id from before the cutover.
    a = await _model_item(db_session, printer_id=stale_id, ams_mapping="[0]", pos=1)
    # Item B: an ordinary pool unit, evaluated first (NULL printer_id sorts first).
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

    # A went down the POOL lane: the finder placed it on the SICK printer and it held
    # there, rather than dispatching onto the idle, USB-healthy machine its stale
    # printer_id names. No crash came from a None in busy_printers.
    assert row_a.status == "pending"
    assert row_a.waiting_reason == "no_usb_drive"
    assert row_a.printer_id == stale_id  # inert, untouched — never obeyed, never scrubbed
    # B was still evaluated in the same tick and dispatched onto the healthy printer.
    assert row_b.printer_id == healthy_id
    assert row_b.status == "failed"  # advanced past USB, then no source file


async def test_usb_waiting_notification_deduped_across_ticks(cq_scheduler, db_session, printer_factory, monkeypatch):
    """#5 (tick level) — a unit that keeps landing on the same USB-less printer
    notifies once, not per tick (the assignment path preserves the hold token)."""
    sick = await printer_factory(model="H2S")
    monkeypatch.setattr(
        ps_module.printer_manager,
        "get_status",
        MagicMock(return_value=SimpleNamespace(sdcard=False, state="IDLE")),
    )
    monkeypatch.setattr(cq_scheduler, "_find_idle_printer_for_target", AsyncMock(return_value=(sick.id, None)))

    await _model_item(db_session)

    await cq_scheduler.check_queue()  # tick 1 — transition into the hold → notify
    await cq_scheduler.check_queue()  # tick 2 — re-held on the same printer → no re-notify
    await cq_scheduler.check_queue()  # tick 3 — still held → no re-notify

    assert sched_mod.notification_service.on_queue_job_waiting.await_count == 1


# --------------------------------------------------------------------------- #
# The pool lane end to end — a printer subset placed by the REAL finder
# --------------------------------------------------------------------------- #
async def test_printer_pool_dispatches_to_the_next_idle_member(cq_scheduler, db_session, printer_factory, monkeypatch):
    """A PRINTERS run is a POOL, not a set of pre-assigned pins: each unit goes to
    whichever MEMBER is free at dispatch time, and a same-model printer outside the
    subset is never used. The finder is REAL here — this is the lane under test."""
    a = await printer_factory(model="H2S")
    b = await printer_factory(model="H2S")
    c = await printer_factory(model="H2S")
    a_id, b_id, c_id = a.id, b.id, c.id

    statuses = {
        a_id: SimpleNamespace(sdcard=True, state="IDLE"),
        b_id: SimpleNamespace(sdcard=True, state="IDLE"),
        c_id: SimpleNamespace(sdcard=True, state="IDLE"),
    }
    monkeypatch.setattr(ps_module.printer_manager, "get_status", MagicMock(side_effect=lambda pid: statuses.get(pid)))

    # Per-printer idleness, flipped between ticks. C stays idle throughout.
    idle = {a_id: False, b_id: True, c_id: True}
    monkeypatch.setattr(cq_scheduler, "_is_printer_idle", MagicMock(side_effect=lambda pid: idle[pid]))

    pool = encode_printer_ids([a_id, b_id])
    u1 = await _model_item(db_session, target_model=None, target_printer_ids=pool, pos=1)
    u2 = await _model_item(db_session, target_model=None, target_printer_ids=pool, pos=2)
    u1_id, u2_id = u1.id, u2.id

    async def _row(item_id):
        db_session.expire_all()
        return (
            await db_session.execute(
                select(PrintQueueItem.printer_id, PrintQueueItem.status).where(PrintQueueItem.id == item_id)
            )
        ).one()

    # Tick 1: A busy, B idle → unit 1 runs on B. (Both units are eligible, but one
    # dispatch per printer per tick, and A is not idle — so only unit 1 goes out.)
    await cq_scheduler.check_queue()
    assert (await _row(u1_id)) == (b_id, "failed")  # dispatched on B, then no source file
    # The unit still waiting carries NO printer between ticks — it is in the pool, not
    # parked on a machine.
    assert (await _row(u2_id)) == (None, "pending")

    # Tick 2: A idle, B busy → unit 2 runs on A.
    idle[a_id], idle[b_id] = True, False
    await cq_scheduler.check_queue()
    assert (await _row(u2_id)) == (a_id, "failed")

    # C was idle the whole time and is NOT a member: it never took either unit.
    assert c_id not in {(await _row(u1_id)).printer_id, (await _row(u2_id)).printer_id}


async def test_lease_refusal_leaves_pool_row_unpinned(cq_scheduler, db_session, printer_factory, monkeypatch):
    """THE leak the design review found. A refused dispatch-lease claim is the one
    unwind path no "un-pin" guard ever covered: the tick used to have already committed
    the pick to the row, so the unit entered the PINNED branch on every later tick and
    waited for that one printer forever. Nothing is written now, so a refusal costs the
    unit nothing but this tick."""
    p = await printer_factory(model="H2S")
    monkeypatch.setattr(
        ps_module.printer_manager,
        "get_status",
        MagicMock(return_value=SimpleNamespace(sdcard=True, state="IDLE")),
    )
    monkeypatch.setattr(cq_scheduler, "_find_idle_printer_for_target", AsyncMock(return_value=(p.id, None)))
    # The authority refuses the claim (an operator declared the plate, an eject took
    # the printer). The refusal token is returned verbatim, not a bare None.
    monkeypatch.setattr(cq_scheduler, "_claim_dispatch_lease", MagicMock(return_value="plate_occupied"))
    start_mock = AsyncMock()
    monkeypatch.setattr(cq_scheduler, "_start_print", start_mock)

    item = await _model_item(db_session)
    item_id = item.id

    await cq_scheduler.check_queue()

    start_mock.assert_not_awaited()  # nothing dispatched
    db_session.expire_all()
    row = (
        await db_session.execute(
            select(PrintQueueItem.printer_id, PrintQueueItem.status).where(PrintQueueItem.id == item_id)
        )
    ).one()
    assert row.printer_id is None  # the leak: this used to be p.id, permanently
    assert row.status == "pending"


# --------------------------------------------------------------------------- #
# Assigned-notification once-guard (_held_pool_items)
# --------------------------------------------------------------------------- #
async def test_assigned_notification_deduped_while_held_in_the_pool_guard(
    cq_scheduler, db_session, printer_factory, monkeypatch
):
    """A — sole-idle sick printer re-picked every tick: on_queue_job_assigned has no
    dedupe of its own, so without the once-guard a lights-out farm gets an "assigned"
    notification every 30 s for hours. First pick notifies; re-picks after a hold stay
    silent. Waiting dedup unchanged."""
    sick = await printer_factory(model="H2S")
    monkeypatch.setattr(
        ps_module.printer_manager,
        "get_status",
        MagicMock(return_value=SimpleNamespace(sdcard=False, state="IDLE")),
    )
    monkeypatch.setattr(cq_scheduler, "_find_idle_printer_for_target", AsyncMock(return_value=(sick.id, None)))

    await _model_item(db_session)

    await cq_scheduler.check_queue()  # tick 1 — picked (notify) → USB hold
    await cq_scheduler.check_queue()  # tick 2 — re-picked (silent) → re-held
    await cq_scheduler.check_queue()  # tick 3 — re-picked (silent) → re-held

    assert sched_mod.notification_service.on_queue_job_assigned.await_count == 1
    assert sched_mod.notification_service.on_queue_job_waiting.await_count == 1


async def test_held_pool_guard_discarded_on_real_dispatch(cq_scheduler, db_session, printer_factory, monkeypatch):
    """B — recovery: tick 1 holds on the sick printer (id enters the guard set);
    tick 2 the same unit lands on a healthy printer and DISPATCHES — the guard id
    is discarded so a future hold on a new pick is a fresh transition."""
    sick = await printer_factory(model="H2S")
    healthy = await printer_factory(model="H2S")
    sick_id, healthy_id = sick.id, healthy.id  # snapshot: avoid ORM lazy-load in the mock

    statuses = {
        sick_id: SimpleNamespace(sdcard=False, state="IDLE"),
        healthy_id: SimpleNamespace(sdcard=True, state="IDLE"),
    }
    monkeypatch.setattr(ps_module.printer_manager, "get_status", MagicMock(side_effect=lambda pid: statuses.get(pid)))

    tick = {"n": 1}

    def _find(db, target, exclude, *a_, **k_):
        pick = sick_id if tick["n"] == 1 else healthy_id
        return (pick, None) if pick not in exclude else (None, "no idle printer")

    monkeypatch.setattr(cq_scheduler, "_find_idle_printer_for_target", AsyncMock(side_effect=_find))

    item = await _model_item(db_session)
    item_id = item.id

    await cq_scheduler.check_queue()  # tick 1 — held + registered in the guard
    assert item_id in cq_scheduler._held_pool_items

    # Tick 2 — the healthy printer is offered; stub _start_print with the
    # dispatch-success shape (status → "printing") so the pool path's
    # discard-on-dispatch is exercised (the real _start_print would fail these
    # sourceless test units downstream of the gates).
    started: dict = {}

    async def _start(db, it, *, printer_id=None, ams_mapping=None, lease=None):
        it.status = "printing"
        started["printer_id"] = printer_id
        await db.commit()

    tick["n"] = 2
    monkeypatch.setattr(cq_scheduler, "_start_print", AsyncMock(side_effect=_start))
    await cq_scheduler.check_queue()

    assert started.get("printer_id") == healthy_id  # dispatch happened, on the healthy printer
    assert item_id not in cq_scheduler._held_pool_items  # guard discarded on real dispatch


async def test_normal_assignment_still_notifies(cq_scheduler, db_session, printer_factory, monkeypatch):
    """C — legit-path regression: a unit that waited (no candidate) on tick 1 and is
    picked on tick 2 still notifies exactly once — the guard only suppresses re-picks
    born from a hold-release, never first picks."""
    healthy = await printer_factory(model="H2S")
    healthy_id = healthy.id  # snapshot: avoid ORM lazy-load in the mock
    monkeypatch.setattr(
        ps_module.printer_manager,
        "get_status",
        MagicMock(return_value=SimpleNamespace(sdcard=True, state="IDLE")),
    )

    tick = {"n": 1}

    def _find(db, target, exclude, *a_, **k_):
        if tick["n"] == 1:
            return (None, "Busy: all printers busy")
        return (healthy_id, None) if healthy_id not in exclude else (None, "no idle printer")

    monkeypatch.setattr(cq_scheduler, "_find_idle_printer_for_target", AsyncMock(side_effect=_find))

    await _model_item(db_session)

    await cq_scheduler.check_queue()  # tick 1 — no candidate → waits
    assert sched_mod.notification_service.on_queue_job_assigned.await_count == 0

    tick["n"] = 2
    await cq_scheduler.check_queue()  # tick 2 — picked → notify fires

    assert sched_mod.notification_service.on_queue_job_assigned.await_count == 1
