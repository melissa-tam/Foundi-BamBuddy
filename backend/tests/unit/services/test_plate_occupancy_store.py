"""Tests for ``plate_occupancy_store`` — the I/O half of the occupancy authority (WS2).

Two halves, two shapes of test:

* **hydration** rebuilds the durable facts into the core, so each test asserts on the
  authority's own snapshot rather than on a return value — the ladder that used to live
  in ``monitor.rearm_on_startup`` is now a POLICY on the plate, and the rungs that used
  to arm nothing at all (a quarantined printer, a printer the startup reconciler owns)
  must now land on ``EscalationOnly`` rather than on silence;
* **the side effects** are synchronous callables that schedule their own I/O, so the
  tests drive them exactly as the core does — including the no-loop case, which is the
  guard that keeps the core's synchronous transition surface callable from a sync test.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta, timezone

import pytest

from backend.app.models.eject_profile import EjectProfile
from backend.app.models.print_batch import PrintBatch
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services import plate_occupancy as po, plate_occupancy_store as store


@pytest.fixture(autouse=True)
def _clean_authority():
    """Every test starts and ends with an empty fleet and un-wired callables."""
    po.plate_occupancy.reset_for_tests()
    yield
    po.plate_occupancy.reset_for_tests()


@pytest.fixture
def scheduled(monkeypatch):
    """Capture what the callables schedule, so a test can await the write it caused."""
    tasks: list[asyncio.Task] = []

    def _spawn(coro, *, name=None):
        task = asyncio.ensure_future(coro)
        tasks.append(task)
        return task

    monkeypatch.setattr(store, "spawn_background_task", _spawn)
    return tasks


async def _drain(tasks: list[asyncio.Task]) -> None:
    if tasks:
        await asyncio.gather(*tasks)


def _patch_session(monkeypatch, db_session):
    @contextlib.asynccontextmanager
    async def _fake_session():
        yield db_session

    monkeypatch.setattr("backend.app.core.database.async_session", _fake_session, raising=False)


async def _mk_printer(db, name="OS", *, awaiting=False, gate=None, quarantined=False):
    printer = Printer(
        name=name,
        serial_number=f"S{name}",
        ip_address="1.2.3.4",
        access_code="x",
        model="H2S",
        awaiting_plate_clear=awaiting,
        plate_gate_subtask_id=gate,
        quarantined=quarantined,
    )
    db.add(printer)
    await db.flush()
    return printer


async def _mk_profile(db, name):
    profile = EjectProfile(name=name)
    db.add(profile)
    await db.flush()
    return profile


async def _mk_item(
    db,
    *,
    printer_id,
    status="completed",
    first_article=False,
    batch_id=None,
    eject_profile_id=None,
    dispatch_subtask_id=None,
    started_at=None,
    stamp=None,
):
    item = PrintQueueItem(
        printer_id=printer_id,
        status=status,
        first_article=first_article,
        batch_id=batch_id,
        eject_profile_id=eject_profile_id,
        dispatch_subtask_id=dispatch_subtask_id,
        started_at=started_at or datetime.now(timezone.utc),
        plate_id=1,
        position=1,
        eject_dispatched_at=stamp,
    )
    db.add(item)
    await db.flush()
    return item


# ---------------------------------------------------------------------------
# Plate hydration + the re-arm ladder
# ---------------------------------------------------------------------------


class TestHydratePlate:
    async def test_gate_with_no_history_hydrates_escalation_only(self, db_session, monkeypatch):
        """A raised gate the farm cannot tie to an eject job never auto-clears — but it
        is never left policy-less either (the 2026-07-18/07-21 never-armless floor)."""
        printer = await _mk_printer(db_session, "PLAIN", awaiting=True)
        await db_session.commit()
        _patch_session(monkeypatch, db_session)

        await store.hydrate()

        view = po.plate_occupancy.snapshot(printer.id)
        assert view.plate_occupied is True
        assert isinstance(view.plate_policy, po.EscalationOnly)

    async def test_completed_eject_unit_matching_gate_hydrates_cooldown(self, db_session, monkeypatch):
        """Every rung holds: completed, eject profile, not FA, and the gate's source id
        positively names THIS unit's dispatch → the production cooldown re-arms."""
        printer = await _mk_printer(db_session, "COOL", awaiting=True, gate="SUB-1")
        profile = await _mk_profile(db_session, "cool-ep")
        batch = PrintBatch(name="r", quantity=1, status="active")
        db_session.add(batch)
        await db_session.flush()
        item = await _mk_item(
            db_session,
            printer_id=printer.id,
            batch_id=batch.id,
            eject_profile_id=profile.id,
            dispatch_subtask_id="SUB-1",
        )
        await db_session.commit()
        _patch_session(monkeypatch, db_session)

        await store.hydrate()

        policy = po.plate_occupancy.snapshot(printer.id).plate_policy
        assert isinstance(policy, po.CooldownEject)
        assert policy.unit_id == item.id
        assert policy.run_id == batch.id

    async def test_first_article_gate_hydrates_escalation_only(self, db_session, monkeypatch):
        """An FA part holds on the plate for inspection: the approval flow arms its own
        eject, so a production cooldown must never sweep it away at startup."""
        printer = await _mk_printer(db_session, "FA", awaiting=True, gate="SUB-FA")
        profile = await _mk_profile(db_session, "fa-ep")
        await _mk_item(
            db_session,
            printer_id=printer.id,
            first_article=True,
            eject_profile_id=profile.id,
            dispatch_subtask_id="SUB-FA",
        )
        await db_session.commit()
        _patch_session(monkeypatch, db_session)

        await store.hydrate()

        assert isinstance(po.plate_occupancy.snapshot(printer.id).plate_policy, po.EscalationOnly)

    async def test_subtask_mismatch_hydrates_escalation_only(self, db_session, monkeypatch):
        """A gate whose source id names a different job than the last farm unit is a
        foreign deposit as far as identity goes — it may never auto-clear."""
        printer = await _mk_printer(db_session, "MISM", awaiting=True, gate="SUB-GATE")
        profile = await _mk_profile(db_session, "mism-ep")
        await _mk_item(
            db_session,
            printer_id=printer.id,
            eject_profile_id=profile.id,
            dispatch_subtask_id="SUB-OTHER",
        )
        await db_session.commit()
        _patch_session(monkeypatch, db_session)

        await store.hydrate()

        assert isinstance(po.plate_occupancy.snapshot(printer.id).plate_policy, po.EscalationOnly)

    async def test_unit_without_eject_profile_hydrates_escalation_only(self, db_session, monkeypatch):
        printer = await _mk_printer(db_session, "NOEP", awaiting=True, gate="SUB-2")
        await _mk_item(db_session, printer_id=printer.id, dispatch_subtask_id="SUB-2")
        await db_session.commit()
        _patch_session(monkeypatch, db_session)

        await store.hydrate()

        assert isinstance(po.plate_occupancy.snapshot(printer.id).plate_policy, po.EscalationOnly)

    async def test_quarantined_printer_hydrates_escalation_only(self, db_session, monkeypatch):
        """The legacy loader filtered quarantined printers out of the re-arm query, which
        left their gates watch-less. Under the authority the gate keeps a policy — the
        escalation hold — so a quarantined printer with a part on it still pages."""
        printer = await _mk_printer(db_session, "QUAR", awaiting=True, gate="SUB-Q", quarantined=True)
        profile = await _mk_profile(db_session, "quar-ep")
        await _mk_item(
            db_session,
            printer_id=printer.id,
            eject_profile_id=profile.id,
            dispatch_subtask_id="SUB-Q",
        )
        await db_session.commit()
        _patch_session(monkeypatch, db_session)

        await store.hydrate()

        view = po.plate_occupancy.snapshot(printer.id)
        assert view.plate_occupied is True
        assert isinstance(view.plate_policy, po.EscalationOnly)

    async def test_clear_plate_is_not_hydrated(self, db_session, monkeypatch):
        printer = await _mk_printer(db_session, "CLEAR", awaiting=False)
        await db_session.commit()
        _patch_session(monkeypatch, db_session)

        await store.hydrate()

        assert po.plate_occupancy.snapshot(printer.id).plate_occupied is False


# ---------------------------------------------------------------------------
# Eject hydration
# ---------------------------------------------------------------------------


class TestHydrateEject:
    async def test_fresh_stamp_hydrates_pending_eject(self, db_session, monkeypatch):
        """The record is rebuilt with its ORIGINAL dispatch time and marked hydrated —
        the provenance every downstream rule (no watchdog, supersedable) depends on."""
        printer = await _mk_printer(db_session, "EJF")
        batch = PrintBatch(name="r2", quantity=1, status="active")
        db_session.add(batch)
        await db_session.flush()
        stamp = datetime.now(timezone.utc) - timedelta(minutes=4)
        item = await _mk_item(db_session, printer_id=printer.id, batch_id=batch.id, stamp=stamp)
        await db_session.commit()
        _patch_session(monkeypatch, db_session)

        await store.hydrate()

        identity = po.plate_occupancy.eject_identity(printer.id)
        assert identity is not None
        assert identity.purpose == "production"
        assert identity.queue_item_id == item.id
        assert identity.hydrated is True
        assert identity.started_at is None
        assert store._as_utc(identity.dispatched_at) == store._as_utc(stamp)
        assert po.plate_occupancy.snapshot(printer.id).eject_hydrated is True

    async def test_first_article_stamp_hydrates_fa_purpose(self, db_session, monkeypatch):
        printer = await _mk_printer(db_session, "EJFA")
        await _mk_item(db_session, printer_id=printer.id, first_article=True, stamp=datetime.now(timezone.utc))
        await db_session.commit()
        _patch_session(monkeypatch, db_session)

        await store.hydrate()

        identity = po.plate_occupancy.eject_identity(printer.id)
        assert identity is not None and identity.purpose == "fa"

    async def test_stale_stamp_is_nulled_and_not_hydrated(self, db_session, monkeypatch, caplog):
        """Past the 24 h TTL the stamp is residue, not an eject in flight."""
        printer = await _mk_printer(db_session, "EJSTALE")
        stale = datetime.now(timezone.utc) - timedelta(hours=store._PENDING_EJECT_STALE_TTL_H + 1)
        item = await _mk_item(db_session, printer_id=printer.id, stamp=stale)
        await db_session.commit()
        _patch_session(monkeypatch, db_session)

        with caplog.at_level(logging.WARNING):
            await store.hydrate()

        assert po.plate_occupancy.eject_identity(printer.id) is None
        await db_session.refresh(item)
        assert item.eject_dispatched_at is None
        assert any("stale pending-eject" in r.message for r in caplog.records)

    async def test_second_stamp_on_one_printer_is_nulled(self, db_session, monkeypatch):
        printer = await _mk_printer(db_session, "EJDUP")
        now = datetime.now(timezone.utc)
        older = await _mk_item(db_session, printer_id=printer.id, stamp=now - timedelta(minutes=30))
        newer = await _mk_item(db_session, printer_id=printer.id, stamp=now)
        await db_session.commit()
        _patch_session(monkeypatch, db_session)

        await store.hydrate()

        identity = po.plate_occupancy.eject_identity(printer.id)
        assert identity is not None and identity.queue_item_id == newer.id
        await db_session.refresh(older)
        assert older.eject_dispatched_at is None

    async def test_hydrated_eject_forces_escalation_only_policy(self, db_session, monkeypatch):
        """Ordering rung: the eject is rebuilt BEFORE the plate's policy is chosen, so a
        printer the startup reconciler owns can never also get a cooldown watch — the
        double dispatch the legacy rearm avoided by skipping such printers entirely."""
        printer = await _mk_printer(db_session, "OWNED", awaiting=True, gate="SUB-3")
        profile = await _mk_profile(db_session, "owned-ep")
        await _mk_item(
            db_session,
            printer_id=printer.id,
            eject_profile_id=profile.id,
            dispatch_subtask_id="SUB-3",
            stamp=datetime.now(timezone.utc),
        )
        await db_session.commit()
        _patch_session(monkeypatch, db_session)

        await store.hydrate()

        view = po.plate_occupancy.snapshot(printer.id)
        assert view.eject_hydrated is True
        assert isinstance(view.plate_policy, po.EscalationOnly)


# ---------------------------------------------------------------------------
# The persist callable
# ---------------------------------------------------------------------------


class TestPersistCallable:
    def test_without_a_running_loop_it_is_a_silent_no_op(self, monkeypatch):
        """The loop guard: a synchronous caller (a unit test, a non-loop thread) must not
        raise and must not leave an unawaited coroutine behind."""
        spawned: list = []
        monkeypatch.setattr(store, "spawn_background_task", lambda coro, name=None: spawned.append(name))

        po.plate_occupancy.hydrate_plate(7, source_subtask_id="S", policy=po.EscalationOnly())
        store.persist_occupancy(7, po.plate_occupancy.snapshot(7))

        assert spawned == []

    async def test_writes_the_plate_columns(self, db_session, monkeypatch, scheduled):
        printer = await _mk_printer(db_session, "PWR")
        await db_session.commit()
        _patch_session(monkeypatch, db_session)
        po.plate_occupancy.hydrate_plate(printer.id, source_subtask_id="SUB-W", policy=po.EscalationOnly())

        store.persist_occupancy(printer.id, po.plate_occupancy.snapshot(printer.id))
        await _drain(scheduled)

        await db_session.refresh(printer)
        assert printer.awaiting_plate_clear is True
        assert printer.plate_gate_subtask_id == "SUB-W"

    async def test_release_nulls_the_gate_source(self, db_session, monkeypatch, scheduled):
        """A cleared gate NULLs its source id, so a later re-arm cannot mistake a stale
        id for a live gate."""
        printer = await _mk_printer(db_session, "PCLR", awaiting=True, gate="SUB-OLD")
        await db_session.commit()
        _patch_session(monkeypatch, db_session)

        store.persist_occupancy(printer.id, po.plate_occupancy.snapshot(printer.id))
        await _drain(scheduled)

        await db_session.refresh(printer)
        assert printer.awaiting_plate_clear is False
        assert printer.plate_gate_subtask_id is None

    async def test_stamps_the_owning_units_eject_dispatched_at(self, db_session, monkeypatch, scheduled):
        printer = await _mk_printer(db_session, "PSTAMP")
        item = await _mk_item(db_session, printer_id=printer.id)
        await db_session.commit()
        _patch_session(monkeypatch, db_session)
        dispatched = datetime.now(timezone.utc)
        po.plate_occupancy.hydrate_eject(
            printer.id,
            po.PendingEject(purpose="production", run_id=None, queue_item_id=item.id, dispatched_at=dispatched),
        )

        store.persist_occupancy(printer.id, po.plate_occupancy.snapshot(printer.id))
        await _drain(scheduled)

        await db_session.refresh(item)
        assert store._as_utc(item.eject_dispatched_at) == store._as_utc(dispatched)

    async def test_no_eject_nulls_every_stamp_on_the_printer(self, db_session, monkeypatch, scheduled):
        """Printer-scoped, not item-scoped: a crash that stamped two rows for one printer
        must not leave an orphan behind to hydrate as a phantom eject."""
        printer = await _mk_printer(db_session, "PNULL", awaiting=True)
        now = datetime.now(timezone.utc)
        a = await _mk_item(db_session, printer_id=printer.id, stamp=now)
        b = await _mk_item(db_session, printer_id=printer.id, stamp=now)
        await db_session.commit()
        _patch_session(monkeypatch, db_session)
        po.plate_occupancy.hydrate_plate(printer.id, source_subtask_id=None, policy=po.EscalationOnly())

        store.persist_occupancy(printer.id, po.plate_occupancy.snapshot(printer.id))
        await _drain(scheduled)

        await db_session.refresh(a)
        await db_session.refresh(b)
        assert a.eject_dispatched_at is None
        assert b.eject_dispatched_at is None

    async def test_manual_eject_leaves_no_durable_mirror(self, db_session, monkeypatch, scheduled):
        """A manual eject is memory-only by prior ruling, and it can SUPERSEDE a hydrated
        pending — so the superseded unit's stamp must go, or the next restart rebuilds a
        phantom eject from it."""
        printer = await _mk_printer(db_session, "PMAN", awaiting=True)
        item = await _mk_item(db_session, printer_id=printer.id, stamp=datetime.now(timezone.utc))
        await db_session.commit()
        _patch_session(monkeypatch, db_session)
        po.plate_occupancy.hydrate_plate(printer.id, source_subtask_id=None, policy=po.EscalationOnly())
        po.plate_occupancy.hydrate_eject(
            printer.id,
            po.PendingEject(
                purpose="manual", run_id=None, queue_item_id=None, dispatched_at=datetime.now(timezone.utc)
            ),
        )

        store.persist_occupancy(printer.id, po.plate_occupancy.snapshot(printer.id))
        await _drain(scheduled)

        await db_session.refresh(item)
        assert item.eject_dispatched_at is None

    async def test_re_persisting_the_same_stamp_is_idempotent(self, db_session, monkeypatch, scheduled):
        printer = await _mk_printer(db_session, "PIDEM")
        dispatched = datetime.now(timezone.utc) - timedelta(minutes=2)
        item = await _mk_item(db_session, printer_id=printer.id, stamp=dispatched)
        await db_session.commit()
        _patch_session(monkeypatch, db_session)
        po.plate_occupancy.hydrate_eject(
            printer.id,
            po.PendingEject(purpose="production", run_id=None, queue_item_id=item.id, dispatched_at=dispatched),
        )

        store.persist_occupancy(printer.id, po.plate_occupancy.snapshot(printer.id))
        store.persist_occupancy(printer.id, po.plate_occupancy.snapshot(printer.id))
        await _drain(scheduled)

        await db_session.refresh(item)
        assert store._as_utc(item.eject_dispatched_at) == store._as_utc(dispatched)


# ---------------------------------------------------------------------------
# The remaining callables
# ---------------------------------------------------------------------------


class TestKickAndWiring:
    def test_kick_forwards_the_plate_gate_release_reason(self, monkeypatch):
        """One spelling in the scheduler's wake-reason ring, whatever the core's cause."""
        from backend.app.services import dispatch_kick as dk

        calls: list[tuple[str, int | None]] = []
        monkeypatch.setattr(
            dk.dispatch_kick, "kick", lambda reason, printer_id=None: calls.append((reason, printer_id))
        )

        store.kick_scheduler(4, "eject_completed")

        assert calls == [("plate_gate_release", 4)]

    def test_kick_swallows_a_failing_scheduler(self, monkeypatch):
        """A kick is an optimisation; a broken one must never unwind a release."""
        from backend.app.services import dispatch_kick as dk

        def _boom(reason, printer_id=None):
            raise RuntimeError("no scheduler")

        monkeypatch.setattr(dk.dispatch_kick, "kick", _boom)

        store.kick_scheduler(4, "clear_plate")

    def test_wire_core_injects_the_four_callables(self):
        store.wire_core()

        assert po.plate_occupancy._persist is store.persist_occupancy
        assert po.plate_occupancy._broadcast is store.broadcast_occupancy
        assert po.plate_occupancy._kick is store.kick_scheduler
        assert po.plate_occupancy._escalate is store.escalate_occupancy
        # The policy driver belongs to the monitor and must be left for it to wire.
        assert po.plate_occupancy._policy_driver is po._noop_policy_driver
