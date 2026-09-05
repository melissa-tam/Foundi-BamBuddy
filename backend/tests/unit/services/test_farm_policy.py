"""Unit tests for the farm first-article + failure/quarantine policy (Phase 3)."""

import dataclasses
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from backend.app.models.eject_profile import EjectProfile
from backend.app.models.library import LibraryFile
from backend.app.models.print_batch import PrintBatch
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_incident import (
    KIND_PLATE_VISION,
    KIND_RUNOUT,
    STATUS_ESCALATED,
    STATUS_RECOVERING,
)
from backend.app.models.printer_model_geometry import PrinterModelGeometry
from backend.app.models.sku import Sku, SkuFile
from backend.app.services import farm_correlation, farm_policy, printer_incidents
from backend.app.services.dispatch_target import decode_printer_ids, encode_printer_ids
from backend.app.services.notification_service import notification_service
from backend.app.services.plate_occupancy import (
    CooldownEject,
    DispatchLease,
    EscalationOnly,
    Evidence,
    FirstArticleEject,
    PendingEject,
    plate_occupancy,
)
from backend.app.services.printer_manager import printer_manager

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clean_occupancy():
    """Every test starts with an empty fleet and UN-WIRED occupancy side effects.

    The authority is a process singleton shared with every other test module, and
    ``reset_for_tests`` drops both halves: the per-printer records AND the five
    injected callables. Un-wiring matters as much as clearing — a persist/broadcast/
    kick left wired by another module would fire real DB writes and websocket pushes
    off every transition these tests drive.
    """
    plate_occupancy.reset_for_tests()
    yield
    plate_occupancy.reset_for_tests()


async def _mk_profile(db, name="ep"):
    prof = EjectProfile(name=name)
    db.add(prof)
    await db.flush()
    return prof


async def _mk_run(
    db,
    *,
    quantity=3,
    printer_ids=None,
    target_model="H2S",
    require_fa=True,
    retry_max=1,
    escalate=2,
):
    """Create a farm run + its first-article item (or all plates if not gated)."""
    lib = LibraryFile(
        filename="f.gcode.3mf",
        file_path="/tmp/f.gcode.3mf",
        file_type="gcode.3mf",
        file_size=1,
        is_external=True,
        file_metadata={},
    )
    db.add(lib)
    await db.flush()
    sku = Sku(code=f"SKU{lib.id:03d}", name="Widget")
    db.add(sku)
    await db.flush()
    sf = SkuFile(sku_id=sku.id, library_file_id=lib.id, plate_index=1, units_per_plate=1)
    db.add(sf)
    await db.flush()
    prof = await _mk_profile(db, name=f"ep{lib.id}")

    plate_fields = {
        "library_file_id": lib.id,
        "plate_id": 1,
        "eject_profile_id": prof.id,
        "print_time_seconds": 1000,
        "required_filament_types": None,
        "created_by_id": None,
    }
    batch = PrintBatch(
        name="run",
        quantity=quantity,
        status="active",
        sku_file_id=sf.id,
        target_units=quantity,
        require_first_article=require_fa,
        first_article_state="pending_print" if require_fa else None,
        retry_max_per_unit=retry_max,
        escalate_consecutive_failures=escalate,
    )
    db.add(batch)
    await db.flush()

    if require_fa:
        batch.first_article_plan = farm_policy.build_first_article_plan(
            remaining=quantity - 1,
            printer_ids=printer_ids,
            target_model=None if printer_ids else target_model,
            base_fields=plate_fields,
        )
        fa = PrintQueueItem(
            batch_id=batch.id,
            status="pending",
            first_article=True,
            printer_id=printer_ids[0] if printer_ids else None,
            target_model=None if printer_ids else target_model,
            position=1,
            **plate_fields,
        )
        db.add(fa)
    await db.commit()
    await db.refresh(batch)
    return batch, prof


async def _items(db, batch_id):
    r = await db.execute(select(PrintQueueItem).where(PrintQueueItem.batch_id == batch_id))
    return list(r.scalars().all())


class TestFirstArticleStateMachine:
    async def test_fa_completion_moves_to_awaiting(self, db_session):
        batch, _ = await _mk_run(db_session, printer_ids=[1])
        fa = (await _items(db_session, batch.id))[0]
        fa.status = "completed"
        await db_session.commit()

        await farm_policy.on_terminal(db_session, 1, fa.id, "completed")

        await db_session.refresh(batch)
        assert batch.first_article_state == "awaiting_approval"

    async def test_approve_local_creates_remaining_and_approves(self, db_session):
        batch, _ = await _mk_run(db_session, quantity=3, printer_ids=[7])
        fa = (await _items(db_session, batch.id))[0]
        fa.status = "completed"
        batch.first_article_state = "awaiting_approval"
        await db_session.commit()

        run = await farm_policy.approve_first_article(db_session, batch.id, eject_remotely=False)
        assert run.first_article_state == "approved"
        items = await _items(db_session, batch.id)
        # 1 FA + 2 remaining = 3 total; the plan is consumed.
        assert len(items) == 3
        assert sum(1 for i in items if not i.first_article) == 2
        await db_session.refresh(batch)
        assert batch.first_article_plan is None

    async def test_approve_when_not_awaiting_409(self, db_session):
        from fastapi import HTTPException

        batch, _ = await _mk_run(db_session, printer_ids=[1])  # state pending_print
        with pytest.raises(HTTPException) as exc:
            await farm_policy.approve_first_article(db_session, batch.id, eject_remotely=False)
        assert exc.value.status_code == 409

    async def test_reject_pauses_run_and_stores_reason(self, db_session):
        batch, _ = await _mk_run(db_session, printer_ids=[1])
        batch.first_article_state = "awaiting_approval"
        await db_session.commit()

        run = await farm_policy.reject_first_article(db_session, batch.id, "warping on the front edge")
        assert run.first_article_state == "rejected"
        assert run.status == "paused"
        assert run.first_article_reject_reason == "warping on the front edge"

    async def test_reject_when_not_awaiting_409(self, db_session):
        from fastapi import HTTPException

        batch, _ = await _mk_run(db_session, printer_ids=[1])
        with pytest.raises(HTTPException) as exc:
            await farm_policy.reject_first_article(db_session, batch.id, "nope")
        assert exc.value.status_code == 409

    async def test_reject_then_resume_redispatches_new_first_article(self, db_session):
        from backend.app.services.production_run import transition_run

        batch, _ = await _mk_run(db_session, quantity=3, printer_ids=[5])
        fa = (await _items(db_session, batch.id))[0]
        fa.status = "completed"
        batch.first_article_state = "awaiting_approval"
        await db_session.commit()

        await farm_policy.reject_first_article(db_session, batch.id, "bad part")
        # Resume from the paused/rejected state → a NEW first article is created.
        run = await transition_run(db_session, batch.id, "resume")
        assert run.first_article_state == "pending_print"
        assert run.status == "active"
        assert run.first_article_reject_reason is None

        items = await _items(db_session, batch.id)
        fa_items = [i for i in items if i.first_article]
        # Old (completed) FA + a fresh pending FA.
        assert len(fa_items) == 2
        assert any(i.status == "pending" for i in fa_items)


class TestRetryPolicy:
    async def test_retry_created_on_failure(self, db_session):
        batch, prof = await _mk_run(db_session, quantity=2, printer_ids=[3], require_fa=False)
        item = PrintQueueItem(
            batch_id=batch.id,
            status="failed",
            first_article=False,
            printer_id=3,
            eject_profile_id=prof.id,
            library_file_id=None,
            plate_id=1,
            retry_count=0,
            position=99,
            completed_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        await db_session.commit()

        created = await farm_policy.create_retry_if_absent(db_session, item)
        assert created is not None
        assert created.retry_count == 1
        assert created.retry_of_id == item.id
        assert created.status == "pending"

    async def test_retry_is_idempotent(self, db_session):
        batch, prof = await _mk_run(db_session, quantity=2, printer_ids=[3], require_fa=False)
        item = PrintQueueItem(
            batch_id=batch.id,
            status="failed",
            first_article=False,
            printer_id=3,
            eject_profile_id=prof.id,
            plate_id=1,
            retry_count=0,
            position=99,
            completed_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        await db_session.commit()

        first = await farm_policy.create_retry_if_absent(db_session, item)
        second = await farm_policy.create_retry_if_absent(db_session, item)
        assert first is not None
        assert second is None  # exactly one retry per failure event
        retries = [i for i in await _items(db_session, batch.id) if i.retry_of_id == item.id]
        assert len(retries) == 1

    async def test_no_retry_past_max(self, db_session):
        batch, prof = await _mk_run(db_session, quantity=2, printer_ids=[3], require_fa=False, retry_max=1)
        # The chain already holds one GENUINE failure (the original) → on-failure of
        # its retry must not create another.
        item = await _mk_exhausted_chain(db_session, batch, prof, printer_id=3, pos=99)
        await farm_policy._on_item_failed(db_session, batch, item)
        retries = [i for i in await _items(db_session, batch.id) if i.retry_of_id == item.id]
        assert retries == []

    async def test_cancelled_ancestors_do_not_consume_the_cap(self, db_session):
        """A lineage-only requeue (a plate the farm refused) leaves the retry intact.

        The chain is two deep — a ``cancelled`` original and its requeue — and the
        requeue then fails GENUINELY. The cap counts failed ancestors, of which there
        are none, so this first real failure still gets its one retry.
        """
        batch, prof = await _mk_run(db_session, quantity=2, printer_ids=[3], require_fa=False, retry_max=1)
        refused = await _mk_failed_item(db_session, batch, prof, printer_id=3, retry_count=0, pos=10)
        refused.status = "cancelled"
        refused.stop_source = farm_correlation.STOP_SOURCE_FARM_VISION_ABORT
        await db_session.commit()
        requeued = await _mk_failed_item(
            db_session, batch, prof, printer_id=3, retry_count=1, pos=11, retry_of_id=refused.id
        )

        assert await farm_policy._genuine_failure_count(db_session, requeued) == 0
        await farm_policy._on_item_failed(db_session, batch, requeued)
        retries = [i for i in await _items(db_session, batch.id) if i.retry_of_id == requeued.id]
        assert len(retries) == 1
        # ...and the NEXT genuine failure in the same chain has spent it.
        retry = retries[0]
        retry.status = "failed"
        retry.completed_at = datetime.now(timezone.utc)
        await db_session.commit()
        assert await farm_policy._genuine_failure_count(db_session, retry) == 1


class TestQuarantine:
    async def _mk_printer(self, db, pid_name="Q"):
        p = Printer(name=pid_name, serial_number=f"S{pid_name}", ip_address="1.2.3.4", access_code="x", model="H2S")
        db.add(p)
        await db.flush()
        return p

    async def test_recent_terminal_query_orders_and_limits(self, db_session):
        batch, prof = await _mk_run(db_session, quantity=5, printer_ids=[0], require_fa=False)
        printer = await self._mk_printer(db_session, "A")
        base = datetime.now(timezone.utc)
        for i, status in enumerate(["completed", "failed", "failed"]):
            db_session.add(
                PrintQueueItem(
                    batch_id=batch.id,
                    printer_id=printer.id,
                    status=status,
                    eject_profile_id=prof.id,
                    plate_id=1,
                    position=100 + i,
                    completed_at=base + timedelta(minutes=i),
                )
            )
        await db_session.commit()
        recent = await farm_policy.recent_terminal_farm_items(db_session, printer.id, 2)
        assert [r.status for r in recent] == ["failed", "failed"]  # most-recent-first, limited to 2

    async def test_quarantine_trips_on_n_consecutive_failures(self, db_session):
        batch, prof = await _mk_run(db_session, quantity=5, printer_ids=[0], require_fa=False, escalate=2)
        printer = await self._mk_printer(db_session, "B")
        base = datetime.now(timezone.utc)
        items = []
        for i in range(2):
            it = PrintQueueItem(
                batch_id=batch.id,
                printer_id=printer.id,
                status="failed",
                eject_profile_id=prof.id,
                plate_id=1,
                position=200 + i,
                completed_at=base + timedelta(minutes=i),
            )
            db_session.add(it)
            items.append(it)
        await db_session.commit()

        tripped = await farm_policy.maybe_quarantine_printer(db_session, batch, items[-1])
        assert tripped is True
        await db_session.refresh(printer)
        assert printer.quarantined is True
        assert printer.quarantine_reason
        assert printer_manager.is_quarantined(printer.id) is True

    async def test_no_quarantine_when_a_recent_run_succeeded(self, db_session):
        batch, prof = await _mk_run(db_session, quantity=5, printer_ids=[0], require_fa=False, escalate=2)
        printer = await self._mk_printer(db_session, "C")
        base = datetime.now(timezone.utc)
        # newest is a failure, but the prior one COMPLETED → not N consecutive fails.
        for i, status in enumerate(["completed", "failed"]):
            db_session.add(
                PrintQueueItem(
                    batch_id=batch.id,
                    printer_id=printer.id,
                    status=status,
                    eject_profile_id=prof.id,
                    plate_id=1,
                    position=300 + i,
                    completed_at=base + timedelta(minutes=i),
                )
            )
        await db_session.commit()
        last = (await farm_policy.recent_terminal_farm_items(db_session, printer.id, 1))[0]
        tripped = await farm_policy.maybe_quarantine_printer(db_session, batch, last)
        assert tripped is False
        await db_session.refresh(printer)
        assert printer.quarantined is False

    async def test_clear_quarantine(self, db_session):
        printer = await self._mk_printer(db_session, "D")
        printer.quarantined = True
        printer.quarantine_reason = "boom"
        await db_session.commit()
        printer_manager.set_quarantined(printer.id, True)

        cleared = await farm_policy.clear_quarantine(db_session, printer.id)
        assert cleared.quarantined is False
        assert cleared.quarantine_reason is None
        assert cleared.quarantine_cleared_at is not None
        assert printer_manager.is_quarantined(printer.id) is False

    async def _fail_at(self, db, batch, prof, printer, when, position):
        it = PrintQueueItem(
            batch_id=batch.id,
            printer_id=printer.id,
            status="failed",
            eject_profile_id=prof.id,
            plate_id=1,
            position=position,
            completed_at=when,
        )
        db.add(it)
        await db.commit()
        return it

    async def test_recovery_resets_the_streak_so_one_failure_does_not_requarantine(self, db_session):
        """The 2026-08-14 fleet self-quarantine: recover must mean "count fresh".

        The streak is DERIVED from queue history, so clearing the quarantine flag
        alone left the two failures that tripped it sitting right there — the next
        single failure re-derived "2 consecutive" and re-quarantined the printer
        seconds after the operator clicked Recover, over and over.
        """
        batch, prof = await _mk_run(db_session, quantity=9, printer_ids=[0], require_fa=False, escalate=2)
        printer = await self._mk_printer(db_session, "REC")
        base = datetime.now(timezone.utc) - timedelta(minutes=30)

        await self._fail_at(db_session, batch, prof, printer, base, 800)
        second = await self._fail_at(db_session, batch, prof, printer, base + timedelta(minutes=1), 801)
        assert await farm_policy.maybe_quarantine_printer(db_session, batch, second) is True

        await farm_policy.clear_quarantine(db_session, printer.id)
        await db_session.refresh(printer)
        assert printer.quarantined is False

        # ONE post-recovery failure: the pre-recovery pair is outside the window now,
        # so a streak of 1 cannot reach the threshold of 2.
        after_1 = await self._fail_at(db_session, batch, prof, printer, datetime.now(timezone.utc), 802)
        assert await farm_policy.maybe_quarantine_printer(db_session, batch, after_1) is False
        await db_session.refresh(printer)
        assert printer.quarantined is False

        # TWO post-recovery failures still quarantine — the policy is intact, it just
        # counts from the recovery boundary.
        after_2 = await self._fail_at(
            db_session, batch, prof, printer, datetime.now(timezone.utc) + timedelta(seconds=1), 803
        )
        assert await farm_policy.maybe_quarantine_printer(db_session, batch, after_2) is True
        await db_session.refresh(printer)
        assert printer.quarantined is True
        printer_manager.set_quarantined(printer.id, False)

    async def test_streak_window_starts_at_the_recovery_stamp(self, db_session):
        """The window itself — one origin, so every consumer sees the same history."""
        batch, prof = await _mk_run(db_session, quantity=9, printer_ids=[0], require_fa=False, escalate=2)
        printer = await self._mk_printer(db_session, "WIN")
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        await self._fail_at(db_session, batch, prof, printer, old, 810)
        await self._fail_at(db_session, batch, prof, printer, old + timedelta(minutes=1), 811)
        assert len(await farm_policy.recent_terminal_farm_items(db_session, printer.id, 5)) == 2

        await farm_policy.clear_quarantine(db_session, printer.id)
        assert await farm_policy.recent_terminal_farm_items(db_session, printer.id, 5) == []

        await self._fail_at(db_session, batch, prof, printer, datetime.now(timezone.utc), 812)
        fresh = await farm_policy.recent_terminal_farm_items(db_session, printer.id, 5)
        assert [r.position for r in fresh] == [812]

    async def test_success_after_recovery_still_resets_the_streak(self, db_session):
        """Unchanged policy: a completed print breaks the run of failures."""
        batch, prof = await _mk_run(db_session, quantity=9, printer_ids=[0], require_fa=False, escalate=2)
        printer = await self._mk_printer(db_session, "SUC")
        await farm_policy.clear_quarantine(db_session, printer.id)

        base = datetime.now(timezone.utc)
        await self._fail_at(db_session, batch, prof, printer, base, 820)
        db_session.add(
            PrintQueueItem(
                batch_id=batch.id,
                printer_id=printer.id,
                status="completed",
                eject_profile_id=prof.id,
                plate_id=1,
                position=821,
                completed_at=base + timedelta(minutes=1),
            )
        )
        await db_session.commit()
        last = await self._fail_at(db_session, batch, prof, printer, base + timedelta(minutes=2), 822)

        assert await farm_policy.maybe_quarantine_printer(db_session, batch, last) is False
        await db_session.refresh(printer)
        assert printer.quarantined is False


class TestOperatorStop:
    """Operator-stop policy (Phase 3.1): a farm unit cancelled WITH a stop_source
    takes the no-retry / no-quarantine path, holds the run (active), notifies once."""

    async def _mk_printer(self, db, name="OS"):
        p = Printer(name=name, serial_number=f"S{name}", ip_address="1.2.3.4", access_code="x", model="H2S")
        db.add(p)
        await db.flush()
        return p

    async def test_operator_stop_no_retry_holds_run_notifies_once(self, db_session):
        batch, prof = await _mk_run(db_session, quantity=3, printer_ids=[3], require_fa=False)
        item = PrintQueueItem(
            batch_id=batch.id,
            status="cancelled",
            first_article=False,
            printer_id=3,
            eject_profile_id=prof.id,
            plate_id=1,
            retry_count=0,
            position=99,
            stop_source="operator_screen",
            completed_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        await db_session.commit()

        with patch.object(farm_policy.notification_service, "on_run_unit_stopped", new_callable=AsyncMock) as mock_n:
            await farm_policy.on_terminal(db_session, 3, item.id, "cancelled")
            mock_n.assert_awaited_once()

        # No retry row for an operator stop.
        retries = [i for i in await _items(db_session, batch.id) if i.retry_of_id == item.id]
        assert retries == []
        await db_session.refresh(batch)
        assert batch.pause_reason == "operator_stop"
        assert batch.status == "active"  # run STAYS active with a visible hold

    async def test_cancelled_without_stop_source_is_noop(self, db_session):
        # A run-abort cancel (no stop_source) must NOT trigger operator-stop handling.
        batch, prof = await _mk_run(db_session, quantity=2, printer_ids=[3], require_fa=False)
        item = PrintQueueItem(
            batch_id=batch.id,
            status="cancelled",
            first_article=False,
            printer_id=3,
            eject_profile_id=prof.id,
            plate_id=1,
            position=98,
            stop_source=None,
            completed_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        await db_session.commit()

        with patch.object(farm_policy.notification_service, "on_run_unit_stopped", new_callable=AsyncMock) as mock_n:
            await farm_policy.on_terminal(db_session, 3, item.id, "cancelled")
            mock_n.assert_not_awaited()
        await db_session.refresh(batch)
        assert batch.pause_reason is None

    async def test_operator_stop_not_counted_toward_quarantine(self, db_session):
        # A prior failure + an operator stop must NOT quarantine (cancelled is
        # outside the terminal-outcome window that quarantine counts).
        batch, prof = await _mk_run(db_session, quantity=5, printer_ids=[0], require_fa=False, escalate=2)
        printer = await self._mk_printer(db_session, "OSQ")
        base = datetime.now(timezone.utc)
        db_session.add(
            PrintQueueItem(
                batch_id=batch.id,
                printer_id=printer.id,
                status="failed",
                eject_profile_id=prof.id,
                plate_id=1,
                position=700,
                completed_at=base,
            )
        )
        stop_item = PrintQueueItem(
            batch_id=batch.id,
            printer_id=printer.id,
            status="cancelled",
            eject_profile_id=prof.id,
            plate_id=1,
            position=701,
            stop_source="operator_ui",
            completed_at=base + timedelta(minutes=1),
        )
        db_session.add(stop_item)
        await db_session.commit()

        with patch.object(farm_policy.notification_service, "on_run_unit_stopped", new_callable=AsyncMock):
            await farm_policy.on_terminal(db_session, printer.id, stop_item.id, "cancelled")

        # The quarantine window only sees completed/failed — the cancelled stop is
        # invisible to it, so the printer is NOT quarantined off one real failure.
        recent = await farm_policy.recent_terminal_farm_items(db_session, printer.id, 2)
        assert all(r.status == "failed" for r in recent)
        assert len(recent) == 1
        await db_session.refresh(printer)
        assert printer.quarantined is False

    async def test_true_failed_path_unchanged(self, db_session):
        # A genuine FAILED (no stop_source) still spawns a retry — regression guard.
        batch, prof = await _mk_run(db_session, quantity=2, printer_ids=[3], require_fa=False, retry_max=1)
        item = PrintQueueItem(
            batch_id=batch.id,
            status="failed",
            first_article=False,
            printer_id=3,
            eject_profile_id=prof.id,
            plate_id=1,
            retry_count=0,
            position=99,
            completed_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        await db_session.commit()
        await farm_policy.on_terminal(db_session, 3, item.id, "failed")
        retries = [i for i in await _items(db_session, batch.id) if i.retry_of_id == item.id]
        assert len(retries) == 1


class TestRunCompletion:
    async def test_last_plate_completes_run(self, db_session):
        batch, prof = await _mk_run(db_session, quantity=2, printer_ids=[9], require_fa=False)
        # Two completed plates, no pending/printing → run completes.
        for i in range(2):
            db_session.add(
                PrintQueueItem(
                    batch_id=batch.id,
                    printer_id=9,
                    status="completed",
                    eject_profile_id=prof.id,
                    plate_id=1,
                    position=400 + i,
                    completed_at=datetime.now(timezone.utc),
                )
            )
        await db_session.commit()
        await farm_policy._maybe_complete_run(db_session, batch)
        await db_session.refresh(batch)
        assert batch.status == "completed"
        # A clean completion (never held) must not leave a hold reason stamped.
        assert batch.pause_reason is None

    async def test_operator_stop_deficit_holds_run_paused(self, db_session):
        """F2: a run held by an operator stop must NOT complete while its plate
        plan is unmet — completing would strand the Resume/top-up affordance
        (resume of a completed run 409s). It pauses, KEEPING ``operator_stop``."""
        batch, prof = await _mk_run(db_session, quantity=3, printer_ids=[9], require_fa=False)
        # 2 completed + 1 operator-cancelled = 3 primaries, but only 2 plates done
        # against a plan of 3 → one plate short.
        for i in range(2):
            db_session.add(
                PrintQueueItem(
                    batch_id=batch.id,
                    printer_id=9,
                    status="completed",
                    eject_profile_id=prof.id,
                    plate_id=1,
                    position=700 + i,
                    completed_at=datetime.now(timezone.utc),
                )
            )
        db_session.add(
            PrintQueueItem(
                batch_id=batch.id,
                printer_id=9,
                status="cancelled",
                stop_source="operator_screen",
                eject_profile_id=prof.id,
                plate_id=1,
                position=799,
                completed_at=datetime.now(timezone.utc),
            )
        )
        batch.pause_reason = "operator_stop"
        await db_session.commit()

        with patch.object(notification_service, "on_run_paused", new_callable=AsyncMock) as paused_spy:
            await farm_policy._maybe_complete_run(db_session, batch)

        await db_session.refresh(batch)
        assert batch.status == "paused"  # NOT completed
        assert batch.pause_reason == "operator_stop"  # hold reason preserved
        paused_spy.assert_awaited_once()

    async def test_operator_stop_topped_up_completes_and_clears_reason(self, db_session):
        """Once the plate plan is met (the deficit was topped up), a lingering
        ``operator_stop`` hold must not block completion — the run completes and
        the stale hold reason is cleared (the prod-18 defect)."""
        batch, prof = await _mk_run(db_session, quantity=2, printer_ids=[9], require_fa=False)
        for i in range(2):
            db_session.add(
                PrintQueueItem(
                    batch_id=batch.id,
                    printer_id=9,
                    status="completed",
                    eject_profile_id=prof.id,
                    plate_id=1,
                    position=800 + i,
                    completed_at=datetime.now(timezone.utc),
                )
            )
        batch.pause_reason = "operator_stop"
        await db_session.commit()

        with patch.object(notification_service, "on_run_completed", new_callable=AsyncMock):
            await farm_policy._maybe_complete_run(db_session, batch)

        await db_session.refresh(batch)
        assert batch.status == "completed"
        assert batch.pause_reason is None  # stale hold reason cleared

    async def test_run_not_complete_while_pending_items(self, db_session):
        batch, prof = await _mk_run(db_session, quantity=2, printer_ids=[9], require_fa=False)
        db_session.add(
            PrintQueueItem(
                batch_id=batch.id,
                printer_id=9,
                status="completed",
                plate_id=1,
                position=500,
                completed_at=datetime.now(timezone.utc),
            )
        )
        db_session.add(PrintQueueItem(batch_id=batch.id, printer_id=9, status="pending", plate_id=1, position=501))
        await db_session.commit()
        await farm_policy._maybe_complete_run(db_session, batch)
        await db_session.refresh(batch)
        assert batch.status == "active"


class TestRecoverPrinter:
    """One-click recovery: clear plate + quarantine + resume paused runs, idempotently."""

    async def _mk_printer(self, db, pid_name="R"):
        p = Printer(name=pid_name, serial_number=f"S{pid_name}", ip_address="1.2.3.4", access_code="x", model="H2S")
        db.add(p)
        await db.flush()
        return p

    async def test_recover_clears_gate_quarantine_and_resumes_run(self, db_session):
        printer = await self._mk_printer(db_session, "REC1")
        # A paused farm run with a pending, manual_start item on this printer.
        batch, prof = await _mk_run(db_session, quantity=2, printer_ids=[printer.id], require_fa=False)
        batch.status = "paused"
        item = PrintQueueItem(
            batch_id=batch.id,
            printer_id=printer.id,
            status="pending",
            manual_start=True,
            plate_id=1,
            position=600,
        )
        db_session.add(item)
        # Raise the gate + quarantine the printer.
        printer.quarantined = True
        printer.quarantine_reason = "boom"
        await db_session.commit()
        printer_manager.set_quarantined(printer.id, True)
        plate_occupancy.hydrate_plate(printer.id, "SUB-REC1", EscalationOnly())

        summary = await farm_policy.recover_printer(db_session, printer.id)

        assert summary["plate_cleared"] is True
        assert summary["quarantine_cleared"] is True
        assert summary["runs_resumed"] == [batch.id]

        await db_session.refresh(printer)
        await db_session.refresh(batch)
        await db_session.refresh(item)
        assert printer.quarantined is False
        assert printer.quarantine_reason is None
        assert plate_occupancy.is_plate_occupied(printer.id) is False
        assert printer_manager.is_quarantined(printer.id) is False
        assert batch.status == "active"
        assert item.manual_start is False  # resume un-staged the pending item

        # Idempotent: a second call is a no-op with no error.
        summary2 = await farm_policy.recover_printer(db_session, printer.id)
        assert summary2["plate_cleared"] is False
        assert summary2["quarantine_cleared"] is False
        assert summary2["runs_resumed"] == []

    async def test_recover_drops_the_plate_the_eject_and_the_dispatch_lease(self, db_session):
        """Recover means "an operator inspected the machine", which outranks every
        stored belief — so it drops ALL THREE of the authority's records, not just the
        gate: the plate, any registered eject, and any dispatch lease.

        The eject and the lease matter as much as the plate here: a stale eject makes
        every later eject 409 ``eject_in_flight`` (the 2026-08-30 printer-4 dead end)
        and a stale lease keeps the printer in the scheduler's busy set, so a recovery
        that cleared only the gate would hand the operator a printer that still cannot
        take work.
        """
        printer = await self._mk_printer(db_session, "REC2")
        await db_session.commit()

        lease = plate_occupancy.claim_for_dispatch(
            printer.id,
            4242,
            pre_state="IDLE",
            pre_subtask=None,
            min_hold_s=60.0,
            max_hold_s=180.0,
            ev=Evidence(live_state="IDLE"),
        )
        assert isinstance(lease, DispatchLease)
        # An operator declaration revokes-but-KEEPS the lease and raises the plate...
        assert plate_occupancy.declare_occupied(printer.id, Evidence(live_state="IDLE")) is None
        # ...and an eject then claims that occupied plate.
        assert (
            plate_occupancy.claim_for_eject(
                printer.id, PendingEject("production", None, 4242), Evidence(live_state="IDLE")
            )
            is None
        )
        before = plate_occupancy.snapshot(printer.id)
        assert (before.plate_occupied, before.lease_unit_id, before.eject_purpose) == (True, 4242, "production")

        summary = await farm_policy.recover_printer(db_session, printer.id)

        assert summary["plate_cleared"] is True
        after = plate_occupancy.snapshot(printer.id)
        assert after.plate_occupied is False
        assert after.lease_unit_id is None
        assert after.eject_present is False

    async def test_recover_unknown_printer_404(self, db_session):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            await farm_policy.recover_printer(db_session, 987654)
        assert exc.value.status_code == 404

    async def test_recover_excludes_fa_rejected_run(self, db_session):
        """A run paused by a first-article REJECT is NOT resumed by recover: the
        rejected part is still on the plate and resuming re-dispatches a fresh
        first article (transition_run), which would silently undo the operator's
        rejection. That run keeps its own run-page resume affordance."""
        printer = await self._mk_printer(db_session, "REC3")
        batch, prof = await _mk_run(db_session, quantity=2, printer_ids=[printer.id], require_fa=True)
        batch.status = "paused"
        batch.first_article_state = "rejected"
        db_session.add(
            PrintQueueItem(
                batch_id=batch.id,
                printer_id=printer.id,
                status="pending",
                manual_start=True,
                plate_id=1,
                position=610,
            )
        )
        await db_session.commit()

        summary = await farm_policy.recover_printer(db_session, printer.id)

        # The FA-rejected run is left untouched — not swept into recovery.
        assert summary["runs_resumed"] == []
        await db_session.refresh(batch)
        assert batch.status == "paused"
        assert batch.first_article_state == "rejected"


async def _mk_failed_item(
    db,
    batch,
    prof,
    *,
    printer_id=3,
    retry_count=0,
    target_model=None,
    target_printer_ids=None,
    pos=99,
    retry_of_id=None,
):
    item = PrintQueueItem(
        batch_id=batch.id,
        status="failed",
        first_article=False,
        printer_id=printer_id,
        target_model=target_model,
        target_printer_ids=target_printer_ids,
        eject_profile_id=prof.id,
        plate_id=1,
        retry_count=retry_count,
        retry_of_id=retry_of_id,
        position=pos,
        completed_at=datetime.now(timezone.utc),
    )
    db.add(item)
    await db.commit()
    return item


async def _mk_exhausted_chain(db, batch, prof, *, printer_id=3, pos=99):
    """A plate whose ONE genuine retry is already spent: failed original -> failed retry.

    Built as a real lineage rather than by stamping ``retry_count``, because the cap is
    DERIVED from the failed ancestors in the ``retry_of_id`` chain (W10) — a bare
    ``retry_count`` on a parentless row describes no failure that ever happened, and a
    fixture that fakes one would stop testing the mechanism the farm actually runs.
    Returns the retry (the row whose failure event is being decided).
    """
    original = await _mk_failed_item(db, batch, prof, printer_id=printer_id, retry_count=0, pos=pos)
    return await _mk_failed_item(
        db,
        batch,
        prof,
        printer_id=printer_id,
        retry_count=1,
        pos=pos + 1,
        retry_of_id=original.id,
    )


class TestFailurePolicyBatchGate:
    """The batch-status gate on `_on_item_failed` (Phase 1, R1/R2/R3)."""

    async def _mk_printer(self, db, name="BG"):
        p = Printer(name=name, serial_number=f"S{name}", ip_address="1.2.3.4", access_code="x", model="H2S")
        db.add(p)
        await db.flush()
        return p

    async def test_paused_run_failure_stages_retry_and_resume_releases(self, db_session):
        """R1: a failure on a paused run mints a STAGED retry; resume releases it."""
        from backend.app.services.production_run import transition_run

        batch, prof = await _mk_run(db_session, quantity=2, printer_ids=[3], require_fa=False)
        batch.status = "paused"
        batch.pause_reason = "operator"
        db_session.add(
            PrintQueueItem(
                batch_id=batch.id,
                printer_id=3,
                status="completed",
                eject_profile_id=prof.id,
                plate_id=1,
                position=1,
                completed_at=datetime.now(timezone.utc),
            )
        )
        failed = await _mk_failed_item(db_session, batch, prof, printer_id=3, pos=2)

        await farm_policy.on_terminal(db_session, 3, failed.id, "failed")

        retries = [i for i in await _items(db_session, batch.id) if i.retry_of_id == failed.id]
        assert len(retries) == 1
        assert retries[0].status == "pending"
        assert retries[0].manual_start is True  # staged — can't dispatch while paused

        run = await transition_run(db_session, batch.id, "resume")
        assert run.status == "active"
        released = [i for i in await _items(db_session, batch.id) if i.retry_of_id == failed.id][0]
        assert released.manual_start is False  # resume swept it un-staged

    async def test_cancelled_run_failure_no_retry(self, db_session):
        """R2: a failure on an aborted run mints NO retry; batch stays cancelled."""
        batch, prof = await _mk_run(db_session, quantity=2, printer_ids=[3], require_fa=False)
        batch.status = "cancelled"
        failed = await _mk_failed_item(db_session, batch, prof, printer_id=3, pos=2)

        await farm_policy.on_terminal(db_session, 3, failed.id, "failed")

        retries = [i for i in await _items(db_session, batch.id) if i.retry_of_id == failed.id]
        assert retries == []
        await db_session.refresh(batch)
        assert batch.status == "cancelled"

    async def test_completed_run_failure_no_retry(self, db_session):
        """A late failure on a completed run mints NO retry."""
        batch, prof = await _mk_run(db_session, quantity=2, printer_ids=[3], require_fa=False)
        batch.status = "completed"
        failed = await _mk_failed_item(db_session, batch, prof, printer_id=3, pos=2)

        await farm_policy.on_terminal(db_session, 3, failed.id, "failed")

        retries = [i for i in await _items(db_session, batch.id) if i.retry_of_id == failed.id]
        assert retries == []
        await db_session.refresh(batch)
        assert batch.status == "completed"

    async def test_cancelled_run_failure_still_quarantines(self, db_session):
        """A cancelled run's failure still counts toward quarantine (printer health
        is independent of run intent) — the 2nd consecutive failure trips it."""
        printer = await self._mk_printer(db_session, "BGQ")
        batch, prof = await _mk_run(db_session, quantity=5, printer_ids=[0], require_fa=False, escalate=2)
        batch.status = "cancelled"
        base = datetime.now(timezone.utc)
        db_session.add(
            PrintQueueItem(
                batch_id=batch.id,
                printer_id=printer.id,
                status="failed",
                eject_profile_id=prof.id,
                plate_id=1,
                position=1,
                completed_at=base,
            )
        )
        second = PrintQueueItem(
            batch_id=batch.id,
            printer_id=printer.id,
            status="failed",
            eject_profile_id=prof.id,
            plate_id=1,
            position=2,
            completed_at=base + timedelta(minutes=1),
        )
        db_session.add(second)
        await db_session.commit()

        try:
            await farm_policy.on_terminal(db_session, printer.id, second.id, "failed")
            await db_session.refresh(printer)
            assert printer.quarantined is True
            # ...but no retry was minted for the cancelled run.
            retries = [i for i in await _items(db_session, batch.id) if i.retry_of_id == second.id]
            assert retries == []
        finally:
            printer_manager.set_quarantined(printer.id, False)


class TestRetryRebalance:
    """Retry rebalance (Phase 1, F7): POOL retries return to the pool."""

    async def test_model_targeted_retry_returns_to_unassigned_pool(self, db_session):
        batch, prof = await _mk_run(db_session, quantity=2, target_model="H2S", require_fa=False)
        item = await _mk_failed_item(db_session, batch, prof, printer_id=7, target_model="H2S", pos=1)

        created = await farm_policy.create_retry_if_absent(db_session, item)
        assert created is not None
        assert created.printer_id is None  # rebalanced off the failing printer
        assert created.target_model == "H2S"  # model target preserved

    async def test_printer_pinned_retry_keeps_pin(self, db_session):
        batch, prof = await _mk_run(db_session, quantity=2, printer_ids=[7], require_fa=False)
        item = await _mk_failed_item(db_session, batch, prof, printer_id=7, target_model=None, pos=1)

        created = await farm_policy.create_retry_if_absent(db_session, item)
        assert created is not None
        assert created.printer_id == 7  # operator-pinned run keeps its printer

    async def test_printer_pool_retry_returns_to_pool(self, db_session):
        """A printers-pool unit's retry goes back to the POOL, not to the printer
        that just failed it: on the failed row ``printer_id`` is the attribution
        RECORD of where this attempt ran, and copying it would pin the chain to the
        machine the scheduler should now avoid."""
        batch, prof = await _mk_run(db_session, quantity=2, printer_ids=[7, 9], require_fa=False)
        item = await _mk_failed_item(
            db_session,
            batch,
            prof,
            printer_id=7,
            target_model=None,
            target_printer_ids=encode_printer_ids([7, 9]),
            pos=1,
        )

        created = await farm_policy.create_retry_if_absent(db_session, item)
        assert created is not None
        assert created.printer_id is None
        assert decode_printer_ids(created.target_printer_ids) == {7, 9}
        assert created.target_model is None


class TestRetryRaceLoser:
    """R4: the unique-constraint loser returns None and the caller still runs."""

    async def _mk_printer(self, db, name="RACE"):
        p = Printer(name=name, serial_number=f"S{name}", ip_address="1.2.3.4", access_code="x", model="H2S")
        db.add(p)
        await db.flush()
        return p

    async def test_integrity_error_loser_returns_none_and_quarantine_still_evaluated(self, db_session):
        from sqlalchemy.exc import IntegrityError

        printer = await self._mk_printer(db_session, "RACE1")
        batch, prof = await _mk_run(db_session, quantity=5, printer_ids=[0], require_fa=False, escalate=2)
        base = datetime.now(timezone.utc)
        db_session.add(
            PrintQueueItem(
                batch_id=batch.id,
                printer_id=printer.id,
                status="failed",
                eject_profile_id=prof.id,
                plate_id=1,
                position=1,
                completed_at=base,
            )
        )
        failing = PrintQueueItem(
            batch_id=batch.id,
            printer_id=printer.id,
            status="failed",
            eject_profile_id=prof.id,
            plate_id=1,
            position=2,
            retry_count=0,
            completed_at=base + timedelta(minutes=1),
        )
        db_session.add(failing)
        await db_session.commit()

        raiser = AsyncMock(side_effect=IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed")))
        try:
            with patch.object(farm_policy, "create_queue_items", new=raiser):
                # Direct call: the loser returns None (no duplicate retry).
                created = await farm_policy.create_retry_if_absent(db_session, failing)
                assert created is None
                # Session still usable after the rollback — a fresh query works.
                still = await _items(db_session, batch.id)
                assert any(i.id == failing.id for i in still)
                # And the whole failure path still reaches quarantine evaluation.
                await farm_policy._on_item_failed(db_session, batch, failing)
            await db_session.refresh(printer)
            assert printer.quarantined is True
        finally:
            printer_manager.set_quarantined(printer.id, False)


class TestExhaustedRunPause:
    """R3: an active run whose last unit exhausts retries with no work left pauses."""

    async def _mk_printer(self, db, name="EX"):
        p = Printer(name=name, serial_number=f"S{name}", ip_address="1.2.3.4", access_code="x", model="H2S")
        db.add(p)
        await db.flush()
        return p

    async def test_exhausted_last_unit_pauses_and_notifies_once(self, db_session):
        batch, prof = await _mk_run(db_session, quantity=1, printer_ids=[3], require_fa=False, retry_max=1)
        # The chain's one genuine retry is already spent → no retry minted; no other
        # live items.
        failed = await _mk_exhausted_chain(db_session, batch, prof, printer_id=3, pos=1)

        with patch.object(farm_policy.notification_service, "on_run_paused", new_callable=AsyncMock) as mock_p:
            await farm_policy.on_terminal(db_session, 3, failed.id, "failed")
            mock_p.assert_awaited_once()

        await db_session.refresh(batch)
        assert batch.status == "paused"
        assert batch.pause_reason == "retries_exhausted"

    async def test_live_retry_suppresses_pause(self, db_session):
        # A real printer row (available: no live MQTT status) so the no-printers
        # auto-pause doesn't fire — isolating the exhausted-pause suppression.
        printer = await self._mk_printer(db_session, "EXLIVE")
        batch, prof = await _mk_run(db_session, quantity=2, printer_ids=[printer.id], require_fa=False, retry_max=1)
        failed = await _mk_failed_item(db_session, batch, prof, printer_id=printer.id, retry_count=0, pos=1)

        await farm_policy.on_terminal(db_session, printer.id, failed.id, "failed")

        # A retry was minted (pending) → work in flight → the run stays active.
        retries = [i for i in await _items(db_session, batch.id) if i.retry_of_id == failed.id]
        assert len(retries) == 1
        await db_session.refresh(batch)
        assert batch.status == "active"
        assert batch.pause_reason is None

    async def test_awaiting_approval_zero_live_items_stays_active(self, db_session):
        """A gated run awaiting approval is NORMAL with zero live items — not paused
        even when a stale FA failure event arrives with no retry left."""
        batch, prof = await _mk_run(db_session, quantity=3, printer_ids=[3], require_fa=True, retry_max=1)
        fa = (await _items(db_session, batch.id))[0]
        fa.status = "failed"
        # A spent chain: this FA IS the retry of an earlier failed FA, so the cap's
        # one genuine failure is gone and no further retry is minted → zero live items.
        first_fa = await _mk_failed_item(db_session, batch, prof, printer_id=3, retry_count=0, pos=50)
        fa.retry_of_id = first_fa.id
        fa.retry_count = 1
        fa.completed_at = datetime.now(timezone.utc)
        batch.first_article_state = "awaiting_approval"
        await db_session.commit()

        await farm_policy.on_terminal(db_session, 3, fa.id, "failed")

        await db_session.refresh(batch)
        assert batch.status == "active"
        assert batch.pause_reason is None

    async def test_dead_fa_chain_pauses_then_resume_redispatches_fa(self, db_session):
        """A gated run whose entire FA chain died at max retries pauses; resume
        re-dispatches a fresh first article and leaves the plan intact."""
        from backend.app.services.production_run import transition_run

        batch, prof = await _mk_run(db_session, quantity=3, printer_ids=[5], require_fa=True, retry_max=1)
        plan_before = batch.first_article_plan
        fa = (await _items(db_session, batch.id))[0]
        fa.status = "failed"
        # Dead chain (state never left pending_print): this FA is the retry of an
        # earlier failed FA, so the cap's one genuine failure is already spent.
        first_fa = await _mk_failed_item(db_session, batch, prof, printer_id=5, retry_count=0, pos=50)
        fa.retry_of_id = first_fa.id
        fa.retry_count = 1
        fa.completed_at = datetime.now(timezone.utc)
        await db_session.commit()

        await farm_policy.on_terminal(db_session, 5, fa.id, "failed")
        await db_session.refresh(batch)
        assert batch.status == "paused"
        assert batch.pause_reason == "retries_exhausted"

        run = await transition_run(db_session, batch.id, "resume")
        assert run.status == "active"
        items = await _items(db_session, batch.id)
        assert any(i.first_article and i.status == "pending" for i in items)  # fresh FA
        await db_session.refresh(batch)
        assert batch.first_article_plan == plan_before  # plan intact for approval


class TestApproveGuards:
    """R6: FA approval respects the run's paused/cancelled status."""

    async def test_approve_on_paused_run_stages_plates_resume_releases(self, db_session):
        from backend.app.services.production_run import transition_run

        batch, _ = await _mk_run(db_session, quantity=3, printer_ids=[7], require_fa=True)
        fa = (await _items(db_session, batch.id))[0]
        fa.status = "completed"
        fa.completed_at = datetime.now(timezone.utc)
        batch.first_article_state = "awaiting_approval"
        batch.status = "paused"
        batch.pause_reason = "operator"
        await db_session.commit()

        run = await farm_policy.approve_first_article(db_session, batch.id, eject_remotely=False)
        assert run.first_article_state == "approved"
        plates = [i for i in await _items(db_session, batch.id) if not i.first_article]
        assert len(plates) == 2
        assert all(i.manual_start is True for i in plates)  # staged while paused

        # Production runs resume in a fresh session; detach so the reload rebuilds
        # queue_items (approve created the plates on a now-stale identity-map row).
        db_session.expunge_all()
        run2 = await transition_run(db_session, batch.id, "resume")
        assert run2.status == "active"
        plates2 = [i for i in await _items(db_session, batch.id) if not i.first_article]
        assert all(i.manual_start is False for i in plates2)  # released on resume

    async def test_approve_on_cancelled_run_409(self, db_session):
        from fastapi import HTTPException

        batch, _ = await _mk_run(db_session, quantity=3, printer_ids=[7], require_fa=True)
        fa = (await _items(db_session, batch.id))[0]
        fa.status = "completed"
        batch.first_article_state = "awaiting_approval"
        batch.status = "cancelled"
        await db_session.commit()

        with pytest.raises(HTTPException) as exc:
            await farm_policy.approve_first_article(db_session, batch.id, eject_remotely=False)
        assert exc.value.status_code == 409

    async def test_approve_local_fires_first_article_approved_once(self, db_session):
        batch, _ = await _mk_run(db_session, quantity=3, printer_ids=[7])
        fa = (await _items(db_session, batch.id))[0]
        fa.status = "completed"
        batch.first_article_state = "awaiting_approval"
        await db_session.commit()

        with patch.object(
            farm_policy.notification_service, "on_first_article_approved", new_callable=AsyncMock
        ) as mock_n:
            run = await farm_policy.approve_first_article(db_session, batch.id, eject_remotely=False)

        assert run.first_article_state == "approved"
        mock_n.assert_awaited_once()
        assert mock_n.call_args.args[0] == batch.name  # run_name

    async def test_physical_approve_clears_the_plate(self, db_session):
        """``eject_remotely=False`` means the operator removed the part by hand, so the
        approval itself drops the gate — the same mechanism as the manual plate-clear
        confirm, now expressed as ``plate_occupancy.clear_plate``."""
        batch, _ = await _mk_run(db_session, quantity=3, printer_ids=[7])
        fa = (await _items(db_session, batch.id))[0]
        fa.status = "completed"
        batch.first_article_state = "awaiting_approval"
        await db_session.commit()
        plate_occupancy.hydrate_plate(7, "SUB-FA", EscalationOnly())

        with patch.object(farm_policy.notification_service, "on_first_article_approved", new_callable=AsyncMock):
            run = await farm_policy.approve_first_article(db_session, batch.id, eject_remotely=False)

        assert run.first_article_state == "approved"
        assert plate_occupancy.is_plate_occupied(7) is False

    async def test_finalize_remote_eject_fires_first_article_approved_once(self, db_session):
        batch, _ = await _mk_run(db_session, quantity=3, printer_ids=[7])
        fa = (await _items(db_session, batch.id))[0]
        fa.status = "completed"
        batch.first_article_state = "awaiting_approval"
        await db_session.commit()
        # The reconciled path's shape: the gate is still up (no live terminal dropped
        # it), so the finalise belt is what clears it.
        plate_occupancy.hydrate_plate(7, "SUB-FA", EscalationOnly())

        with patch.object(
            farm_policy.notification_service, "on_first_article_approved", new_callable=AsyncMock
        ) as mock_n:
            await farm_policy._finalize_remote_eject(db_session, batch.id, 7)

        await db_session.refresh(batch)
        assert batch.first_article_state == "approved"
        # Both approval paths (physical + remote eject) close the FA-pending loop.
        mock_n.assert_awaited_once()
        # Idempotent belt: clear_plate answers ``not_occupied`` on an already-clear
        # plate, so the reconciled replay is safe to run after a live terminal.
        assert plate_occupancy.is_plate_occupied(7) is False

    async def test_approve_on_cancelled_run_fires_no_notification(self, db_session):
        from fastapi import HTTPException

        batch, _ = await _mk_run(db_session, quantity=3, printer_ids=[7], require_fa=True)
        fa = (await _items(db_session, batch.id))[0]
        fa.status = "completed"
        batch.first_article_state = "awaiting_approval"
        batch.status = "cancelled"
        await db_session.commit()

        with (
            patch.object(
                farm_policy.notification_service, "on_first_article_approved", new_callable=AsyncMock
            ) as mock_n,
            pytest.raises(HTTPException),
        ):
            await farm_policy.approve_first_article(db_session, batch.id, eject_remotely=False)
        mock_n.assert_not_awaited()


class TestLifecycleNotifications:
    """Phase 6: transition_run pause/abort/resume fire the lifecycle events once."""

    async def test_pause_fires_on_run_paused_operator_reason(self, db_session):
        from backend.app.services.production_run import transition_run

        batch, _ = await _mk_run(db_session, quantity=3, printer_ids=[1], require_fa=False)
        with patch.object(farm_policy.notification_service, "on_run_paused", new_callable=AsyncMock) as mock_n:
            run = await transition_run(db_session, batch.id, "pause")

        assert run.status == "paused"
        mock_n.assert_awaited_once()
        # reason is the third positional arg (run_name, sku_code, reason, db).
        assert mock_n.call_args.args[2] == "Paused by operator"

    async def test_abort_fires_on_run_aborted_once(self, db_session):
        from backend.app.services.production_run import transition_run

        batch, _ = await _mk_run(db_session, quantity=3, printer_ids=[1], require_fa=False)
        with (
            patch.object(farm_policy.notification_service, "on_run_aborted", new_callable=AsyncMock) as mock_ab,
            patch.object(farm_policy.notification_service, "on_run_paused", new_callable=AsyncMock) as mock_pa,
        ):
            run = await transition_run(db_session, batch.id, "abort")

        assert run.status == "cancelled"
        mock_ab.assert_awaited_once()
        mock_pa.assert_not_awaited()

    async def test_resume_fires_on_run_resumed_once(self, db_session):
        from backend.app.services.production_run import transition_run

        batch, _ = await _mk_run(db_session, quantity=3, printer_ids=[1], require_fa=False)
        batch.status = "paused"
        batch.pause_reason = "operator"
        await db_session.commit()

        with patch.object(farm_policy.notification_service, "on_run_resumed", new_callable=AsyncMock) as mock_n:
            run = await transition_run(db_session, batch.id, "resume")

        assert run.status == "active"
        mock_n.assert_awaited_once()

    async def test_abort_of_cancelled_run_409_fires_nothing(self, db_session):
        from fastapi import HTTPException

        from backend.app.services.production_run import transition_run

        batch, _ = await _mk_run(db_session, quantity=3, printer_ids=[1], require_fa=False)
        batch.status = "cancelled"
        await db_session.commit()

        with (
            patch.object(farm_policy.notification_service, "on_run_aborted", new_callable=AsyncMock) as mock_n,
            pytest.raises(HTTPException) as exc,
        ):
            await transition_run(db_session, batch.id, "abort")
        assert exc.value.status_code == 409
        mock_n.assert_not_awaited()


class TestQuarantinePrinterHelper:
    """The extracted idempotent quarantine mutator shared by the consecutive-failure
    policy and the eject-verification / cooldown-stall paths."""

    async def _mk_printer(self, db, name="QP"):
        p = Printer(name=name, serial_number=f"S{name}", ip_address="1.2.3.4", access_code="x", model="H2S")
        db.add(p)
        await db.flush()
        return p

    async def test_idempotent_and_notifies_once(self, db_session):
        printer = await self._mk_printer(db_session, "QP1")
        with (
            patch.object(farm_policy.printer_manager, "set_quarantined") as sq,
            patch.object(farm_policy.notification_service, "on_printer_quarantined", new_callable=AsyncMock) as notif,
        ):
            first = await farm_policy.quarantine_printer(db_session, printer.id, "boom", failure_count=1)
            second = await farm_policy.quarantine_printer(db_session, printer.id, "boom again", failure_count=1)
        assert first is True
        assert second is False  # already quarantined → no-op
        await db_session.refresh(printer)
        assert printer.quarantined is True
        assert printer.quarantine_reason == "boom"  # first reason kept
        notif.assert_awaited_once()
        sq.assert_called_once_with(printer.id, True)

    async def test_missing_printer_returns_false(self, db_session):
        assert await farm_policy.quarantine_printer(db_session, 999999, "x", failure_count=1) is False


class TestPendingEjectRecord:
    """The typed pending-eject record round-trips — now through the occupancy authority.

    The per-module registry it used to round-trip through was one of the five stores
    the 2026-08-30 cut-over collapsed into one record per printer. The VALUE type is
    unchanged and still resolves through its old import site; what moved is who holds
    it, and the claim→peek→retire cycle is pinned here on the new owner."""

    async def test_claim_peek_resolve(self):
        from backend.app.services.eject import remote

        # One vocabulary: ``remote.PendingEject`` is a re-export of the authority's type.
        assert remote.PendingEject is PendingEject

        pe = PendingEject(purpose="production", run_id=5, queue_item_id=7)
        plate_occupancy.hydrate_plate(42, "SUB-REG", EscalationOnly())
        assert plate_occupancy.claim_for_eject(42, pe, Evidence()) is None

        stored = plate_occupancy.pending_eject_view(42)
        assert stored is not None
        assert (stored.purpose, stored.run_id, stored.queue_item_id) == ("production", 5, 7)
        identity = plate_occupancy.eject_identity(42)
        assert identity is not None
        assert identity.purpose == "production"
        assert identity.hydrated is False  # a claim is a LIVE dispatch by definition

        assert plate_occupancy.resolve_eject(42, "completed") is None
        assert plate_occupancy.pending_eject_view(42) is None
        assert plate_occupancy.eject_identity(42) is None
        # The old pop was idempotent-by-None; the transition refuses instead, which is
        # the same guarantee with a reason attached.
        assert plate_occupancy.resolve_eject(42, "completed") == "no_eject"

    async def test_the_module_level_registry_is_gone(self):
        """Absence pin: the eject lane must never grow a second pending-eject store.

        Every name below was a writer or reader of the registry the cut-over deleted;
        a re-appearance means the authority has a competing opinion again, which is
        precisely the seam the 2026-08-30 cascade cashed."""
        from backend.app.services.eject import remote

        for name in (
            "register_pending_eject",
            "pop_pending_eject",
            "peek_pending_eject",
            "pending_eject_printer_ids",
            "persist_pending_eject",
            "clear_pending_eject",
            "hydrate_pending_ejects_from_db",
            "mark_pending_eject_started",
            "cancel_runtime_watchdog",
        ):
            assert not hasattr(remote, name), f"eject.remote.{name} must stay deleted"


class TestOnTerminalEjectHandling:
    """on_terminal step 1: a server-dispatched eject job's terminal, matched by the
    printer's subtask echo, is consumed here (production clears/keeps the gate; FA
    finalises) and never falls through to item-based policy."""

    async def _mk_printer(self, db, name="EJ"):
        p = Printer(name=name, serial_number=f"S{name}", ip_address="1.2.3.4", access_code="x", model="H2S")
        db.add(p)
        await db.flush()
        return p

    @staticmethod
    def _fake_client(subtask):
        from types import SimpleNamespace

        return SimpleNamespace(last_dispatch_subtask_id=subtask)

    @staticmethod
    def _arm(printer_id, pending, *, gate="SUB-E"):
        """Gate the plate and CLAIM it for ``pending`` — the live-dispatch shape.

        Two transitions, in the order the real dispatcher makes them: the deposit
        raises the plate, then the eject claims the printer. ``claim_for_eject``
        refuses on a clear plate, so the order is not cosmetic."""
        plate_occupancy.hydrate_plate(printer_id, gate, EscalationOnly())
        assert plate_occupancy.claim_for_eject(printer_id, pending, Evidence()) is None

    async def test_production_completed_clears_plate(self, db_session):
        printer = await self._mk_printer(db_session, "PEok")
        batch, _ = await _mk_run(db_session, quantity=2, printer_ids=[printer.id], require_fa=False)
        self._arm(printer.id, PendingEject("production", batch.id, 111))

        with patch.object(farm_policy.printer_manager, "get_client", return_value=self._fake_client("SUB-E")):
            await farm_policy.on_terminal(db_session, printer.id, None, "completed", completed_subtask_id="SUB-E")

        assert plate_occupancy.pending_eject_view(printer.id) is None  # eject retired
        assert plate_occupancy.is_plate_occupied(printer.id) is False  # plate released

    async def test_production_failed_keeps_plate_and_quarantines(self, db_session):
        printer = await self._mk_printer(db_session, "PEfail")
        batch, _ = await _mk_run(db_session, quantity=2, printer_ids=[printer.id], require_fa=False)
        self._arm(printer.id, PendingEject("production", batch.id, 222))

        with (
            patch.object(farm_policy.printer_manager, "get_client", return_value=self._fake_client("SUB-E")),
            patch.object(farm_policy.printer_manager, "set_quarantined"),
            patch.object(farm_policy.notification_service, "on_printer_quarantined", new_callable=AsyncMock),
        ):
            await farm_policy.on_terminal(db_session, printer.id, None, "failed", completed_subtask_id="SUB-E")

        assert plate_occupancy.pending_eject_view(printer.id) is None  # job ended → retired
        view = plate_occupancy.snapshot(printer.id)
        assert view.plate_occupied is True  # plate KEPT — sweep unverified
        assert isinstance(view.plate_policy, EscalationOnly)  # only a human clears it
        assert view.plate_source_subtask_id == "SUB-E"  # the deposit's identity survives
        await db_session.refresh(printer)
        assert printer.quarantined is True
        assert "sweep unverified" in (printer.quarantine_reason or "")

    async def test_production_failed_pauses_a_run_with_no_printers_left(self, db_session):
        """The production/other branch's third action, beside the plate and the
        quarantine: the failed sweep's run is paused when it has no printer left."""
        printer = await self._mk_printer(db_session, "PEpause")
        batch, prof = await _mk_run(db_session, quantity=2, printer_ids=[printer.id], require_fa=False)
        db_session.add(
            PrintQueueItem(
                batch_id=batch.id,
                printer_id=printer.id,
                status="pending",
                eject_profile_id=prof.id,
                plate_id=1,
                position=900,
            )
        )
        await db_session.commit()
        self._arm(printer.id, PendingEject("production", batch.id, 223))

        with (
            patch.object(farm_policy.printer_manager, "get_client", return_value=self._fake_client("SUB-E")),
            patch.object(farm_policy.printer_manager, "set_quarantined"),
            patch.object(farm_policy.notification_service, "on_printer_quarantined", new_callable=AsyncMock),
            patch.object(farm_policy.notification_service, "on_run_paused", new_callable=AsyncMock) as paused,
        ):
            await farm_policy.on_terminal(db_session, printer.id, None, "failed", completed_subtask_id="SUB-E")

        await db_session.refresh(batch)
        assert batch.status == "paused"
        assert batch.pause_reason == "no_available_printers"
        paused.assert_awaited_once()

    async def test_manual_completed_clears_plate(self, db_session):
        # A foreign-plate manual eject owns no run/queue item: completed clears the
        # plate exactly like production, matched by the printer-keyed stem.
        printer = await self._mk_printer(db_session, "MANok")
        self._arm(printer.id, PendingEject("manual", None, None))

        with patch.object(farm_policy.printer_manager, "get_client", return_value=self._fake_client(None)):
            await farm_policy.on_terminal(
                db_session,
                printer.id,
                None,
                "completed",
                completed_subtask_id=None,
                completed_subtask_name=f"eject_manual_p{printer.id}",
            )

        assert plate_occupancy.pending_eject_view(printer.id) is None  # eject retired
        assert plate_occupancy.is_plate_occupied(printer.id) is False  # plate released

    async def test_manual_failed_keeps_plate_no_quarantine(self, db_session):
        # A manual eject that ends non-completed keeps the plate occupied (fail-closed)
        # and — unlike production/FA — NEVER quarantines (it owns no run to protect).
        printer = await self._mk_printer(db_session, "MANfail")
        self._arm(printer.id, PendingEject("manual", None, None))

        with (
            patch.object(farm_policy.printer_manager, "get_client", return_value=self._fake_client(None)),
            patch.object(farm_policy.printer_manager, "set_quarantined") as set_q,
            patch.object(farm_policy.notification_service, "on_printer_quarantined", new_callable=AsyncMock),
        ):
            await farm_policy.on_terminal(
                db_session,
                printer.id,
                None,
                "failed",
                completed_subtask_id=None,
                completed_subtask_name=f"eject_manual_p{printer.id}",
            )

        assert plate_occupancy.pending_eject_view(printer.id) is None  # job ended → retired
        view = plate_occupancy.snapshot(printer.id)
        assert view.plate_occupied is True  # plate KEPT — sweep unverified
        assert isinstance(view.plate_policy, EscalationOnly)
        set_q.assert_not_called()  # NO quarantine
        await db_session.refresh(printer)
        assert printer.quarantined is False

    async def test_echo_mismatch_keeps_the_eject(self, db_session):
        printer = await self._mk_printer(db_session, "PEmis")
        self._arm(printer.id, PendingEject("production", 1, 333))

        with patch.object(farm_policy.printer_manager, "get_client", return_value=self._fake_client("SUB-E")):
            await farm_policy.on_terminal(db_session, printer.id, None, "completed", completed_subtask_id="OTHER")

        # Foreign — the eject is left for its own terminal and the plate is untouched.
        assert plate_occupancy.pending_eject_view(printer.id) is not None
        assert plate_occupancy.is_plate_occupied(printer.id) is True

    async def test_fa_eject_terminal_finalizes(self, db_session):
        printer = await self._mk_printer(db_session, "FAej")
        batch, _ = await _mk_run(db_session, quantity=3, printer_ids=[printer.id], require_fa=True)
        fa = (await _items(db_session, batch.id))[0]
        fa.status = "completed"
        batch.first_article_state = "awaiting_approval"
        await db_session.commit()
        self._arm(printer.id, PendingEject("fa", batch.id, fa.id))

        with (
            patch.object(farm_policy.printer_manager, "get_client", return_value=self._fake_client("SUB-E")),
            patch.object(farm_policy.notification_service, "on_first_article_approved", new_callable=AsyncMock),
        ):
            await farm_policy.on_terminal(db_session, printer.id, None, "completed", completed_subtask_id="SUB-E")

        assert plate_occupancy.pending_eject_view(printer.id) is None
        assert plate_occupancy.is_plate_occupied(printer.id) is False  # swept
        await db_session.refresh(batch)
        assert batch.first_article_state == "approved"

    async def test_fa_eject_failed_terminal_keeps_awaiting_and_quarantines(self, db_session):
        """A FAILED FA eject must NOT approve/materialise: the run stays
        awaiting_approval (re-approvable after recovery), the plate stays occupied,
        and the printer is quarantined like the production branch."""
        printer = await self._mk_printer(db_session, "FAfail")
        batch, _ = await _mk_run(db_session, quantity=3, printer_ids=[printer.id], require_fa=True)
        fa = (await _items(db_session, batch.id))[0]
        fa.status = "completed"
        batch.first_article_state = "awaiting_approval"
        await db_session.commit()
        self._arm(printer.id, PendingEject("fa", batch.id, fa.id))

        with (
            patch.object(farm_policy.printer_manager, "get_client", return_value=self._fake_client("SUB-E")),
            patch.object(farm_policy.printer_manager, "set_quarantined"),
            patch.object(farm_policy.notification_service, "on_printer_quarantined", new_callable=AsyncMock),
            patch.object(
                farm_policy.notification_service, "on_first_article_approved", new_callable=AsyncMock
            ) as approved_note,
        ):
            await farm_policy.on_terminal(db_session, printer.id, None, "failed", completed_subtask_id="SUB-E")

        assert plate_occupancy.pending_eject_view(printer.id) is None  # job ended → retired
        view = plate_occupancy.snapshot(printer.id)
        assert view.plate_occupied is True  # plate untouched
        assert isinstance(view.plate_policy, EscalationOnly)
        approved_note.assert_not_awaited()
        await db_session.refresh(batch)
        assert batch.first_article_state == "awaiting_approval"  # NOT finalised
        await db_session.refresh(printer)
        assert printer.quarantined is True
        assert "First-article eject job ended 'failed'" in (printer.quarantine_reason or "")

    async def test_name_mismatch_during_pending_keeps_the_eject(self, db_session):
        # W1/R2: a foreign terminal whose echoed NAME is not our eject stem is a
        # positive mismatch even when the id path is lenient (no client) — the
        # eject is kept for the real eject terminal.
        printer = await self._mk_printer(db_session, "PEname")
        self._arm(printer.id, PendingEject("production", 1, 444))

        with patch.object(farm_policy.printer_manager, "get_client", return_value=None):
            await farm_policy.on_terminal(
                db_session,
                printer.id,
                None,
                "completed",
                completed_subtask_id=None,
                completed_subtask_name="OperatorLocalPrint",
            )

        assert plate_occupancy.pending_eject_view(printer.id) is not None  # foreign name — kept
        assert plate_occupancy.is_plate_occupied(printer.id) is True

    async def test_completed_clears_plate_and_retires_the_durable_mirror(self, db_session):
        """A name-matched production eject completion clears the plate AND retires the
        record whose durable mirror is ``print_queue.eject_dispatched_at`` (W1).

        The DB write itself moved one layer down at the 2026-08-30 cut-over — the
        authority's injected ``persist`` callable owns it, and ``test_plate_occupancy_store``
        pins the SQL — so what this pins is the fact that drives it: the terminal hands
        that callable a final view carrying no eject and no plate, which is exactly the
        shape ``_apply_eject_stamp`` NULLs every stamp on."""
        printer = await self._mk_printer(db_session, "PEstamp")
        item = PrintQueueItem(
            printer_id=printer.id,
            status="completed",
            plate_id=1,
            position=1,
            eject_dispatched_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        await db_session.commit()
        await db_session.refresh(item)

        persisted: list[tuple[int, object]] = []
        plate_occupancy.configure(persist=lambda pid, view: persisted.append((pid, view)))
        self._arm(printer.id, PendingEject("production", None, item.id))

        with patch.object(farm_policy.printer_manager, "get_client", return_value=None):
            await farm_policy.on_terminal(
                db_session,
                printer.id,
                None,
                "completed",
                completed_subtask_id=None,
                completed_subtask_name=f"eject_production_item{item.id}",
            )

        assert plate_occupancy.pending_eject_view(printer.id) is None  # eject retired
        assert plate_occupancy.is_plate_occupied(printer.id) is False  # plate released
        final_printer_id, final_view = persisted[-1]
        assert final_printer_id == printer.id
        assert final_view.eject_present is False  # → every stamp on this printer NULLed
        assert final_view.plate_occupied is False


class TestEjectRuntimeExceededMark:
    """The terminal handler HONORS the in-flight watchdog's mark; it never judges
    runtime itself.

    2026-07-31 incident: an ejected part lodged under the heatbed; the next eject's
    open-loop bed-drop stalled against it and lost Z steps, so the bed returned high
    and the sweep gouged the plate. The job still ended ``completed`` (179 s vs the
    80-83 s of every nominal eject) and auto-released the gate onto the damaged
    plate. The watchdog now stops that job mid-flight and stamps
    ``runtime_exceeded_at``; by the time a terminal arrives, the ONLY correct
    reaction is to keep the plate gated — whatever status the printer echoed."""

    async def _mk_printer(self, db, name):
        p = Printer(name=name, serial_number=f"S{name}", ip_address="1.2.3.4", access_code="x", model="H2S")
        db.add(p)
        await db.flush()
        return p

    @staticmethod
    def _fake_client(subtask):
        from types import SimpleNamespace

        return SimpleNamespace(last_dispatch_subtask_id=subtask)

    @staticmethod
    def _marked(purpose, run_id, item_id):
        return PendingEject(
            purpose,
            run_id,
            item_id,
            expected_runtime_s=83.0,
            started_at=datetime.now(timezone.utc) - timedelta(seconds=179),
            runtime_exceeded_at=datetime.now(timezone.utc) - timedelta(seconds=75),
        )

    async def _terminal(self, db, printer, pending, final_status):
        """Drive one eject terminal and report the resulting occupancy + the spies.

        The escalation-only HOLD is no longer a call the handler makes: it is the state
        ``resolve_eject("unverified")`` leaves behind (plate occupied, policy
        ``EscalationOnly``), off which the authority's policy driver arms the watch. So
        the re-arm is pinned on the returned view rather than on a mock."""
        from backend.app.services.eject import monitor as monitor_mod

        plate_occupancy.hydrate_plate(printer.id, "SUB-E", EscalationOnly())
        assert plate_occupancy.claim_for_eject(printer.id, pending, Evidence()) is None
        with (
            patch.object(farm_policy.printer_manager, "get_client", return_value=self._fake_client("SUB-E")),
            patch.object(farm_policy, "quarantine_printer", new_callable=AsyncMock) as quarantine,
            patch.object(farm_policy, "_finalize_remote_eject", new_callable=AsyncMock) as finalize,
            patch.object(monitor_mod, "notify_plate_not_empty", new_callable=AsyncMock) as notify,
        ):
            await farm_policy.on_terminal(db, printer.id, None, final_status, completed_subtask_id="SUB-E")
        return plate_occupancy.snapshot(printer.id), quarantine, finalize, notify

    async def test_gouged_plate_20260731_marked_completed_holds_the_plate_under_escalation_only(self, db_session):
        """THE 2026-07-31 GOUGED-PLATE PIN.

        The incident's own shape: the watchdog stopped the sweep and the printer STILL
        echoed ``completed`` (our stop lost the race, or an MQTT drop let the file run
        out). Honouring the mark rather than re-deriving a runtime judgement is what
        keeps the plate gated instead of auto-releasing onto a damaged one. Whatever the
        echo: the eject is RETIRED, the plate stays OCCUPIED under an escalation-only
        policy, and nothing is quarantined."""
        printer = await self._mk_printer(db_session, "RGslow")
        batch, _ = await _mk_run(db_session, quantity=2, printer_ids=[printer.id], require_fa=False)
        view, quarantine, _, notify = await self._terminal(
            db_session, printer, self._marked("production", batch.id, 111), "completed"
        )
        assert plate_occupancy.pending_eject_view(printer.id) is None  # job ended, eject retired
        assert view.eject_present is False
        assert view.plate_occupied is True  # plate KEPT — it waits for a human
        assert isinstance(view.plate_policy, EscalationOnly)  # the re-attached hold
        # The watchdog owns paging; a second alert here would double-notify.
        notify.assert_not_awaited()
        # A held plate already takes the printer out of rotation; an obstruction
        # suspicion is not a hardware fault, and quarantining would also cost the run
        # its rebalance onto this printer.
        quarantine.assert_not_awaited()
        await db_session.refresh(printer)
        assert printer.quarantined is False

    async def test_gouged_plate_20260731_marked_failed_never_masquerades_as_a_hardware_failure(self, db_session):
        # The expected shape: our own stop lands the job on 'failed'. It must NOT take
        # the genuine-failure branch and quarantine the printer.
        printer = await self._mk_printer(db_session, "RGfail")
        batch, _ = await _mk_run(db_session, quantity=2, printer_ids=[printer.id], require_fa=False)
        view, quarantine, _, notify = await self._terminal(
            db_session, printer, self._marked("production", batch.id, 114), "failed"
        )
        assert view.plate_occupied is True
        assert isinstance(view.plate_policy, EscalationOnly)
        quarantine.assert_not_awaited()
        notify.assert_not_awaited()
        await db_session.refresh(printer)
        assert printer.quarantined is False

    async def test_gouged_plate_20260731_marked_fa_eject_never_finalises_the_approval(self, db_session):
        # Purpose-independent: an unverified sweep must not materialise FA plates.
        printer = await self._mk_printer(db_session, "RGfa")
        batch, _ = await _mk_run(db_session, quantity=2, printer_ids=[printer.id], require_fa=True)
        view, quarantine, finalize, _ = await self._terminal(
            db_session, printer, self._marked("fa", batch.id, 115), "completed"
        )
        finalize.assert_not_awaited()
        assert view.plate_occupied is True
        assert isinstance(view.plate_policy, EscalationOnly)
        quarantine.assert_not_awaited()

    async def test_gouged_plate_20260731_marked_manual_eject_keeps_the_plate(self, db_session):
        printer = await self._mk_printer(db_session, "RGman")
        view, quarantine, _, _ = await self._terminal(
            db_session, printer, self._marked("manual", None, None), "completed"
        )
        assert view.plate_occupied is True
        assert isinstance(view.plate_policy, EscalationOnly)
        quarantine.assert_not_awaited()

    async def test_unmarked_completed_releases_the_plate(self, db_session):
        # No mark = the watchdog never fired: pure normal-terminal handling, exactly
        # as before the guard wave.
        printer = await self._mk_printer(db_session, "RGok")
        batch, _ = await _mk_run(db_session, quantity=2, printer_ids=[printer.id], require_fa=False)
        pending = dataclasses.replace(self._marked("production", batch.id, 112), runtime_exceeded_at=None)
        view, quarantine, _, notify = await self._terminal(db_session, printer, pending, "completed")
        assert view.plate_occupied is False
        assert view.plate_policy is None  # a clear plate carries no policy to arm
        notify.assert_not_awaited()
        quarantine.assert_not_awaited()

    async def test_unmarked_failed_still_quarantines(self, db_session):
        # The genuine-failure branch is untouched by this wave.
        printer = await self._mk_printer(db_session, "RGgenuine")
        batch, _ = await _mk_run(db_session, quantity=2, printer_ids=[printer.id], require_fa=False)
        pending = dataclasses.replace(self._marked("production", batch.id, 116), runtime_exceeded_at=None)
        view, quarantine, _, _ = await self._terminal(db_session, printer, pending, "failed")
        assert view.plate_occupied is True
        assert isinstance(view.plate_policy, EscalationOnly)
        quarantine.assert_awaited_once()


class _ParkClient:
    """Minimal MQTT-client stand-in: records G-code and reports send success.

    ``last_dispatch_subtask_id`` is what ``matches_pending_eject`` reads to confirm
    the terminal is our dispatched eject.
    """

    def __init__(self, *, subtask: str | None = "SUB-E", ok: bool = True) -> None:
        self.last_dispatch_subtask_id = subtask
        self.ok = ok
        self.sent: list[str] = []

    def send_gcode(self, gcode: str) -> bool:
        self.sent.append(gcode)
        return self.ok


class TestIdleDeepPark:
    """After a CLEAN PRODUCTION eject on a printer with nothing slated, the bed is
    lowered to a percentage of the model's Z travel over the ``gcode_line`` lane.

    The park is cosmetic, so every guard is a silent skip and every failure is a log
    line: the terminal chain (gate release included) must complete regardless. It is
    production-only — the manual branch has an operator standing at the machine —
    and never runs after a watchdog-killed or failed eject, where the machine's
    motion state is exactly what is in doubt.
    """

    async def _mk_printer(self, db, name, model="H2S"):
        p = Printer(name=name, serial_number=f"S{name}", ip_address="1.2.3.4", access_code="x", model=model)
        db.add(p)
        await db.commit()
        await db.refresh(p)
        return p

    async def _seed_geometry(self, db, *, model_key="H2S", z_travel=340.0):
        """The test DB is built with ``create_all`` only, so registry rows are absent."""
        db.add(
            PrinterModelGeometry(
                model_key=model_key,
                bed_x=340,
                bed_y=320,
                env_x_min=0,
                env_x_max=340,
                env_y_min=-16,
                env_y_max=325,
                max_part_height_mm=42,
                z_travel_mm=z_travel,
                validated=True,
                notes="test seed",
            )
        )
        await db.commit()

    async def _terminal(self, db, printer, client, *, final_status="completed", pending=None):
        """Drive one eject terminal; returns the printer's occupancy view afterwards."""
        from backend.app.services.eject import monitor as monitor_mod

        plate_occupancy.hydrate_plate(printer.id, "SUB-E", EscalationOnly())
        assert (
            plate_occupancy.claim_for_eject(printer.id, pending or PendingEject("production", None, None), Evidence())
            is None
        )
        with (
            patch.object(farm_policy.printer_manager, "get_client", return_value=client),
            patch.object(farm_policy, "quarantine_printer", new_callable=AsyncMock),
            patch.object(monitor_mod, "notify_plate_not_empty", new_callable=AsyncMock),
        ):
            await farm_policy.on_terminal(db, printer.id, None, final_status, completed_subtask_id="SUB-E")
        return plate_occupancy.snapshot(printer.id)

    async def test_parks_idle_printer_after_clean_production_eject(self, db_session):
        printer = await self._mk_printer(db_session, "PARKok")
        await self._seed_geometry(db_session)
        client = _ParkClient()
        view = await self._terminal(db_session, printer, client)
        assert view.plate_occupied is False  # plate released as before
        # 75 % of 340 mm. M400 drains the queue BEFORE M18 releases the steppers —
        # cutting them mid-descent would drop the bed the rest of the way.
        assert client.sent == ["M17\nG90\nG1 Z255.0 F900\nM400\nM18"]

    async def test_no_park_when_work_is_bound_to_the_printer(self, db_session):
        printer = await self._mk_printer(db_session, "PARKbound")
        await self._seed_geometry(db_session)
        await _mk_run(db_session, quantity=2, printer_ids=[printer.id], require_fa=True)
        client = _ParkClient()
        await self._terminal(db_session, printer, client)
        assert client.sent == []  # the next unit is about to use this bed

    async def test_no_park_when_model_targeted_work_is_pending(self, db_session):
        printer = await self._mk_printer(db_session, "PARKmodel")
        await self._seed_geometry(db_session)
        # Unassigned, model-targeted work the scheduler could land right here.
        await _mk_run(db_session, quantity=2, printer_ids=None, target_model="H2S", require_fa=True)
        client = _ParkClient()
        await self._terminal(db_session, printer, client)
        assert client.sent == []

    async def test_no_park_on_bedslinger_model(self, db_session):
        # The gantry carries Z and the bed is fixed in Z — there is no bed travel to
        # park into (the same physics that refuses the eject bed-drop).
        printer = await self._mk_printer(db_session, "PARKa1", model="A1")
        await self._seed_geometry(db_session, model_key="A1", z_travel=250.0)
        client = _ParkClient()
        await self._terminal(db_session, printer, client)
        assert client.sent == []

    async def test_no_park_without_a_geometry_row(self, db_session):
        printer = await self._mk_printer(db_session, "PARKnogeo")
        client = _ParkClient()
        view = await self._terminal(db_session, printer, client)
        assert view.plate_occupied is False  # terminal chain unaffected
        assert client.sent == []

    async def test_no_park_when_z_travel_is_null(self, db_session):
        # A2L-shaped row: no commandable Z travel recorded → nothing to take a
        # percentage of, so the park has no defensible target.
        printer = await self._mk_printer(db_session, "PARKnullz")
        await self._seed_geometry(db_session, z_travel=None)
        client = _ParkClient()
        await self._terminal(db_session, printer, client)
        assert client.sent == []

    async def test_no_park_when_setting_is_off(self, db_session):
        from backend.app.api.routes.settings import set_setting

        printer = await self._mk_printer(db_session, "PARKoff")
        await self._seed_geometry(db_session)
        await set_setting(db_session, "farm_idle_park_enabled", "false")
        await db_session.commit()
        client = _ParkClient()
        await self._terminal(db_session, printer, client)
        assert client.sent == []

    async def test_refused_command_is_warn_only(self, db_session):
        # send_gcode False (disconnected client) must not raise, retry, or escalate —
        # the terminal chain still completes.
        printer = await self._mk_printer(db_session, "PARKrefused")
        await self._seed_geometry(db_session)
        client = _ParkClient(ok=False)
        view = await self._terminal(db_session, printer, client)
        assert view.plate_occupied is False
        assert client.sent == ["M17\nG90\nG1 Z255.0 F900\nM400\nM18"]  # attempted once, not retried

    async def test_percent_is_clamped_and_bad_values_fall_back(self, db_session):
        from backend.app.api.routes.settings import set_setting

        printer = await self._mk_printer(db_session, "PARKpct")
        await self._seed_geometry(db_session)

        await set_setting(db_session, "farm_idle_park_percent", "200")  # above the 95 ceiling
        await db_session.commit()
        client = _ParkClient()
        await self._terminal(db_session, printer, client)
        assert client.sent == ["M17\nG90\nG1 Z323.0 F900\nM400\nM18"]  # 95 % of 340

        await set_setting(db_session, "farm_idle_park_percent", "5")  # below the 10 floor
        await db_session.commit()
        client = _ParkClient()
        await self._terminal(db_session, printer, client)
        assert client.sent == ["M17\nG90\nG1 Z34.0 F900\nM400\nM18"]  # 10 % of 340

        await set_setting(db_session, "farm_idle_park_percent", "not-a-number")
        await db_session.commit()
        client = _ParkClient()
        await self._terminal(db_session, printer, client)
        assert client.sent == ["M17\nG90\nG1 Z255.0 F900\nM400\nM18"]  # schema default 75 %

    async def test_never_parks_after_a_watchdog_killed_eject(self, db_session):
        # The stalled-sweep branch returns before the plate release: a machine whose
        # motion is under suspicion must never be commanded to move further.
        printer = await self._mk_printer(db_session, "PARKkilled")
        await self._seed_geometry(db_session)
        killed = PendingEject(
            "production",
            None,
            None,
            expected_runtime_s=83.0,
            started_at=datetime.now(timezone.utc) - timedelta(seconds=179),
            runtime_exceeded_at=datetime.now(timezone.utc) - timedelta(seconds=75),
        )
        client = _ParkClient()
        view = await self._terminal(db_session, printer, client, pending=killed)
        assert view.plate_occupied is True  # plate KEPT
        assert client.sent == []

    async def test_never_parks_on_a_failed_eject(self, db_session):
        printer = await self._mk_printer(db_session, "PARKfailed")
        await self._seed_geometry(db_session)
        client = _ParkClient()
        view = await self._terminal(db_session, printer, client, final_status="failed")
        assert view.plate_occupied is True  # sweep unverified — plate kept
        assert client.sent == []

    async def test_never_parks_on_the_manual_branch(self, db_session):
        # A manual (foreign-plate) eject means an operator is at the machine; their
        # plate stays at working height.
        printer = await self._mk_printer(db_session, "PARKmanual")
        await self._seed_geometry(db_session)
        client = _ParkClient()
        view = await self._terminal(db_session, printer, client, pending=PendingEject("manual", None, None))
        assert view.plate_occupied is False  # plate released by the manual branch
        assert client.sent == []


class TestFaEjectCooldownGate:
    """approve-with-remote-eject honours the release threshold: hot bed defers the
    sweep (the motion-only file must not sweep a hot plate); cold bed dispatches
    immediately (old UX, incl. 409s); disconnected printer is a 409.

    Since the 2026-08-30 cut-over the deferral is a POLICY on the plate rather than a
    spawned watch — the FA part sits on that plate, so the FA eject is a property of
    it and the authority's policy driver arms the watch off the record. Which is why
    the deferral now REQUIRES the plate to be gated (a policy with no deposit to act
    on is an armed watch over nothing) and 409s when it is not."""

    async def _fa_fixture(self, db):
        printer = Printer(name="FAgate", serial_number="SFAg", ip_address="1.2.3.4", access_code="x", model="H2S")
        db.add(printer)
        await db.flush()
        batch, _ = await _mk_run(db, quantity=3, printer_ids=[printer.id], require_fa=True)
        fa = (await _items(db, batch.id))[0]
        fa.status = "completed"
        batch.first_article_state = "awaiting_approval"
        await db.commit()
        return printer, batch, fa

    @staticmethod
    def _state(bed):
        from types import SimpleNamespace

        return SimpleNamespace(connected=True, temperatures={"bed": bed})

    async def test_hot_bed_sets_the_fa_eject_as_the_plate_policy(self, db_session):
        import backend.app.services.eject.monitor as monitor_mod

        printer, batch, fa = await self._fa_fixture(db_session)
        plate_occupancy.hydrate_plate(printer.id, "SUB-FA", EscalationOnly())
        with (
            patch.object(farm_policy.printer_manager, "is_connected", return_value=True),
            patch.object(farm_policy.printer_manager, "get_status", return_value=self._state(80.0)),
            patch.object(monitor_mod, "_resolve_eject_threshold", new=AsyncMock(return_value=33.0)),
            patch.object(farm_policy.eject_remote, "dispatch_part_present_eject", new_callable=AsyncMock) as direct,
        ):
            await farm_policy._dispatch_remote_eject(db_session, batch, fa)

        view = plate_occupancy.snapshot(printer.id)
        assert view.plate_policy == FirstArticleEject(unit_id=fa.id, run_id=batch.id)
        assert view.plate_occupied is True  # nothing is swept yet — the plate still holds
        direct.assert_not_awaited()

    async def test_hot_bed_409s_when_the_plate_is_not_gated(self, db_session):
        """Behaviour change to pin (2026-08-30): the deferred FA sweep now REQUIRES a
        gated plate. ``not_occupied`` means an operator already cleared it (or the gate
        never rose) — there is nothing to eject, so the approval says so instead of
        arming a watch over an empty bed."""
        import backend.app.services.eject.monitor as monitor_mod

        printer, batch, fa = await self._fa_fixture(db_session)
        assert plate_occupancy.is_plate_occupied(printer.id) is False
        with (
            patch.object(farm_policy.printer_manager, "is_connected", return_value=True),
            patch.object(farm_policy.printer_manager, "get_status", return_value=self._state(80.0)),
            patch.object(monitor_mod, "_resolve_eject_threshold", new=AsyncMock(return_value=33.0)),
            patch.object(farm_policy.eject_remote, "dispatch_part_present_eject", new_callable=AsyncMock) as direct,
            pytest.raises(HTTPException) as exc,
        ):
            await farm_policy._dispatch_remote_eject(db_session, batch, fa)
        assert exc.value.status_code == 409
        assert "not gated" in exc.value.detail
        direct.assert_not_awaited()
        assert plate_occupancy.is_plate_occupied(printer.id) is False  # no phantom gate raised

    async def test_cold_bed_dispatches_immediately(self, db_session):
        import backend.app.services.eject.monitor as monitor_mod

        printer, batch, fa = await self._fa_fixture(db_session)
        plate_occupancy.hydrate_plate(printer.id, "SUB-FA", EscalationOnly())
        with (
            patch.object(farm_policy.printer_manager, "is_connected", return_value=True),
            patch.object(farm_policy.printer_manager, "get_status", return_value=self._state(30.5)),
            patch.object(monitor_mod, "_resolve_eject_threshold", new=AsyncMock(return_value=33.0)),
            patch.object(farm_policy.eject_remote, "dispatch_part_present_eject", new_callable=AsyncMock) as direct,
        ):
            await farm_policy._dispatch_remote_eject(db_session, batch, fa)
        direct.assert_awaited_once()
        assert direct.await_args.kwargs["purpose"] == "fa"
        # The immediate path never swaps the policy — no deferred sweep was armed.
        assert isinstance(plate_occupancy.snapshot(printer.id).plate_policy, EscalationOnly)

    async def test_disconnected_printer_409s_up_front(self, db_session):
        printer, batch, fa = await self._fa_fixture(db_session)
        with (
            patch.object(farm_policy.printer_manager, "is_connected", return_value=False),
            pytest.raises(HTTPException) as exc,
        ):
            await farm_policy._dispatch_remote_eject(db_session, batch, fa)
        assert exc.value.status_code == 409
        assert "not connected" in exc.value.detail


class TestTerminalWaitingReasonHygiene:
    """W4b: a farm unit reaching a terminal status through ``on_terminal`` must not
    keep a stale hold token (the 2026-07-20 completed/cancelled rows still flagged
    spool_jam_recovery_failed / printer_offline_stalled / print_paused_stalled)."""

    async def _held_item(self, db, batch, prof, *, status, reason, printer_id=3, pos=1):
        item = PrintQueueItem(
            batch_id=batch.id,
            status=status,
            first_article=False,
            printer_id=printer_id,
            eject_profile_id=prof.id,
            plate_id=1,
            position=pos,
            waiting_reason=reason,
            completed_at=datetime.now(timezone.utc),
        )
        db.add(item)
        await db.commit()
        return item

    async def test_completed_clears_stale_waiting_reason(self, db_session):
        batch, prof = await _mk_run(db_session, quantity=1, printer_ids=[3], require_fa=False)
        item = await self._held_item(db_session, batch, prof, status="completed", reason="spool_jam_recovery_failed")
        await farm_policy.on_terminal(db_session, 3, item.id, "completed")
        await db_session.refresh(item)
        assert item.waiting_reason is None

    async def test_failed_clears_stale_waiting_reason(self, db_session):
        batch, prof = await _mk_run(db_session, quantity=2, printer_ids=[3], require_fa=False)
        item = await self._held_item(db_session, batch, prof, status="failed", reason="printer_offline_stalled")
        await farm_policy.on_terminal(db_session, 3, item.id, "failed")
        await db_session.refresh(item)
        assert item.waiting_reason is None

    async def test_cancelled_clears_stale_waiting_reason(self, db_session):
        batch, prof = await _mk_run(db_session, quantity=2, printer_ids=[3], require_fa=False)
        item = await self._held_item(db_session, batch, prof, status="cancelled", reason="print_paused_stalled", pos=2)
        await farm_policy.on_terminal(db_session, 3, item.id, "cancelled")
        await db_session.refresh(item)
        assert item.waiting_reason is None

    async def test_only_the_transitioning_unit_is_cleared(self, db_session):
        """The clear targets the exact unit that went terminal — a still-printing
        (non-terminal) sibling keeps its own waiting_reason."""
        batch, prof = await _mk_run(db_session, quantity=2, printer_ids=[3], require_fa=False)
        done = await self._held_item(
            db_session, batch, prof, status="completed", reason="print_paused_stalled", printer_id=3, pos=1
        )
        sibling = await self._held_item(
            db_session, batch, prof, status="printing", reason="spool_jam_recovery_failed", printer_id=4, pos=2
        )
        await farm_policy.on_terminal(db_session, 3, done.id, "completed")
        await db_session.refresh(done)
        await db_session.refresh(sibling)
        assert done.waiting_reason is None
        assert sibling.status == "printing"
        assert sibling.waiting_reason == "spool_jam_recovery_failed"  # untouched — not terminal

    async def test_non_farm_terminal_leaves_waiting_reason(self, db_session):
        """A non-farm batch (sku_file_id NULL) early-returns before the clear — the
        hygiene is scoped to farm terminal transitions only."""
        batch = PrintBatch(name="plain", quantity=1, status="active", target_units=1)
        db_session.add(batch)
        await db_session.flush()
        item = PrintQueueItem(
            batch_id=batch.id,
            status="cancelled",
            printer_id=3,
            plate_id=1,
            position=1,
            waiting_reason="print_paused_stalled",
            completed_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        await db_session.commit()
        await farm_policy.on_terminal(db_session, 3, item.id, "cancelled")
        await db_session.refresh(item)
        assert item.waiting_reason == "print_paused_stalled"  # untouched (non-farm)


class TestRecoverPrinterIgnoresNonFarmBatches:
    """006-H2S 2026-07-26: a plain upstream ``PrintBatch`` (no ``sku_file_id``) left
    ``paused`` with an item on the printer was swept into the resume loop.
    ``transition_run`` cannot drive a non-run batch and raised; the per-run guard only
    LOGS, so one zombie batch turned every Recover click into a silent no-op for the
    real runs behind it. The candidate query is now farm-scoped — the same
    ``sku_file_id IS NOT NULL`` predicate ``spool_recovery._resolve_farm_item`` uses."""

    async def _mk_printer(self, db, name):
        p = Printer(name=name, serial_number=f"S{name}", ip_address="1.2.3.4", access_code="x", model="H2S")
        db.add(p)
        await db.flush()
        return p

    async def test_non_farm_paused_batch_is_never_passed_to_transition_run(self, db_session):
        printer = await self._mk_printer(db_session, "REC4")
        zombie = PrintBatch(name="upstream batch", status="paused")  # sku_file_id is NULL
        db_session.add(zombie)
        await db_session.flush()
        db_session.add(
            PrintQueueItem(batch_id=zombie.id, printer_id=printer.id, status="pending", plate_id=1, position=700)
        )
        await db_session.commit()

        with patch("backend.app.services.production_run.transition_run", new_callable=AsyncMock) as transition:
            summary = await farm_policy.recover_printer(db_session, printer.id)

        transition.assert_not_awaited()  # the zombie never reached the run machinery
        assert summary["runs_resumed"] == []
        await db_session.refresh(zombie)
        assert zombie.status == "paused"  # untouched, not silently mutated

    async def test_a_real_run_still_resumes_alongside_a_zombie_batch(self, db_session):
        """The regression the fix targets: the farm run behind the zombie must resume."""
        printer = await self._mk_printer(db_session, "REC5")
        zombie = PrintBatch(name="upstream batch", status="paused")
        db_session.add(zombie)
        await db_session.flush()
        db_session.add(
            PrintQueueItem(batch_id=zombie.id, printer_id=printer.id, status="pending", plate_id=1, position=710)
        )
        batch, _prof = await _mk_run(db_session, quantity=2, printer_ids=[printer.id], require_fa=False)
        batch.status = "paused"
        db_session.add(
            PrintQueueItem(
                batch_id=batch.id,
                printer_id=printer.id,
                status="pending",
                manual_start=True,
                plate_id=1,
                position=711,
            )
        )
        await db_session.commit()

        summary = await farm_policy.recover_printer(db_session, printer.id)

        assert summary["runs_resumed"] == [batch.id]
        await db_session.refresh(batch)
        await db_session.refresh(zombie)
        assert batch.status == "active"
        assert zombie.status == "paused"


# --------------------------------------------------------------------------- #
# W10 — the third terminal disposition, and W9's post-terminal half
# --------------------------------------------------------------------------- #
async def _mk_printer_row(db, name, model="H2S"):
    p = Printer(name=name, serial_number=f"S{name}", ip_address="1.2.3.4", access_code="x", model=model)
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


async def _seed_hold_geometry(db, *, model_key="H2S", z_travel=340.0, hold_lift=12.0):
    """The test DB is built with ``create_all`` only, so registry rows are absent."""
    db.add(
        PrinterModelGeometry(
            model_key=model_key,
            bed_x=340,
            bed_y=320,
            env_x_min=0,
            env_x_max=340,
            env_y_min=-16,
            env_y_max=325,
            max_part_height_mm=42,
            z_travel_mm=z_travel,
            hold_lift_mm=hold_lift,
            validated=True,
            notes="test seed",
        )
    )
    await db.commit()


async def _mk_live_unit(db, batch, prof, *, printer_id=None, target_model=None, status="printing", pos=1, **kw):
    item = PrintQueueItem(
        batch_id=batch.id,
        status=status,
        first_article=False,
        printer_id=printer_id,
        target_model=target_model,
        eject_profile_id=prof.id,
        plate_id=1,
        position=pos,
        completed_at=datetime.now(timezone.utc),
        **kw,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


async def _open_vision_incident(db, printer_id, *, item_id=None, codes="0500_806E"):
    return await printer_incidents.open_new(
        db,
        printer_id=printer_id,
        job_id="task-vision",
        item_id=item_id,
        kind=KIND_PLATE_VISION,
        code=codes.split(",")[0],
        codes=codes,
        slot_global_tray=None,
        status=STATUS_RECOVERING,
    )


def _vision_state(*, detector: bool):
    """A live PrinterState stand-in carrying only what the vouch test reads."""
    return SimpleNamespace(print_options=SimpleNamespace(buildplate_marker_detector=detector))


class TestGracefulRequeue:
    """W10: a plate the farm REFUSED, or one an operator stopped on a held printer,
    is queued again with its settings — lineage only, no quarantine, no run pause."""

    @pytest.fixture(autouse=True)
    def _clean_incidents(self):
        printer_incidents._reset_state()
        yield
        printer_incidents._reset_state()

    async def test_farm_vision_abort_requeues_without_quarantine_or_pause(self, db_session):
        printer = await _mk_printer_row(db_session, "GRQ1")
        await _seed_hold_geometry(db_session)
        batch, prof = await _mk_run(db_session, quantity=2, printer_ids=[printer.id], require_fa=False)
        item = await _mk_live_unit(
            db_session,
            batch,
            prof,
            printer_id=printer.id,
            status="cancelled",
            skip_filament_check=True,
            stop_source=farm_correlation.STOP_SOURCE_FARM_VISION_ABORT,
        )
        await _open_vision_incident(db_session, printer.id, item_id=item.id)

        with (
            patch.object(farm_policy.printer_manager, "get_status", return_value=_vision_state(detector=True)),
            patch.object(farm_policy, "maybe_quarantine_printer", new_callable=AsyncMock) as quarantine,
        ):
            await farm_policy.on_terminal(db_session, printer.id, item.id, "cancelled")

        retries = [i for i in await _items(db_session, batch.id) if i.retry_of_id == item.id]
        assert len(retries) == 1
        # Full settings, and the lineage that makes the chain readable...
        assert retries[0].skip_filament_check is True
        assert retries[0].retry_count == 1
        # ...but nothing that treats this as a failure.
        quarantine.assert_not_awaited()
        await db_session.refresh(batch)
        assert batch.status == "active"
        assert batch.pause_reason is None
        # The cap is untouched: this chain has no FAILED ancestor.
        assert await farm_policy._genuine_failure_count(db_session, retries[0]) == 0

    async def test_first_article_farm_abort_reaches_the_requeue_not_the_failure_path(self, db_session):
        """The precedence case: a first-article no-deposit stop keeps ``failed``.

        The disposition is decided from the CLASSIFICATION, ahead of the status fork,
        so the farm's own abort never routes an FA plate into ``_on_item_failed`` and
        a quarantine count for a plate the farm itself refused.
        """
        printer = await _mk_printer_row(db_session, "GRQFA")
        await _seed_hold_geometry(db_session)
        batch, prof = await _mk_run(db_session, quantity=3, printer_ids=[printer.id], require_fa=True)
        fa = (await _items(db_session, batch.id))[0]
        fa.printer_id = printer.id
        fa.status = "failed"  # the FA no-deposit shape main.py deliberately keeps
        fa.stop_source = farm_correlation.STOP_SOURCE_FARM_VISION_ABORT
        fa.completed_at = datetime.now(timezone.utc)
        await db_session.commit()
        await _open_vision_incident(db_session, printer.id, item_id=fa.id)

        with (
            patch.object(farm_policy.printer_manager, "get_status", return_value=_vision_state(detector=True)),
            patch.object(farm_policy, "maybe_quarantine_printer", new_callable=AsyncMock) as quarantine,
            patch.object(farm_policy, "_on_item_failed", new_callable=AsyncMock) as failed_path,
        ):
            await farm_policy.on_terminal(db_session, printer.id, fa.id, "failed")

        failed_path.assert_not_awaited()
        quarantine.assert_not_awaited()
        retries = [i for i in await _items(db_session, batch.id) if i.retry_of_id == fa.id]
        assert len(retries) == 1
        assert retries[0].first_article is True  # re-attempted AS a first article

    async def test_operator_stop_on_a_held_printer_requeues(self, db_session):
        printer = await _mk_printer_row(db_session, "GRQ2")
        batch, prof = await _mk_run(db_session, quantity=2, printer_ids=[printer.id], require_fa=False)
        item = await _mk_live_unit(
            db_session, batch, prof, printer_id=printer.id, status="cancelled", stop_source="operator_ui"
        )
        # The printer was ALREADY holding when the operator pressed Stop.
        assert (
            await printer_incidents.open_new(
                db_session,
                printer_id=printer.id,
                job_id="task-runout",
                item_id=item.id,
                kind=KIND_RUNOUT,
                code="0700_8011",
                codes="0700_8011",
                slot_global_tray=None,
                status=STATUS_ESCALATED,
            )
            is not None
        )

        await farm_policy.on_terminal(db_session, printer.id, item.id, "cancelled")

        retries = [i for i in await _items(db_session, batch.id) if i.retry_of_id == item.id]
        assert len(retries) == 1
        await db_session.refresh(batch)
        assert batch.status == "active"
        assert batch.pause_reason is None  # NOT the operator-stop hold

    async def test_operator_stop_with_no_incident_keeps_the_cancel_and_holds_the_run(self, db_session):
        printer = await _mk_printer_row(db_session, "GRQ3")
        batch, prof = await _mk_run(db_session, quantity=2, printer_ids=[printer.id], require_fa=False)
        item = await _mk_live_unit(
            db_session, batch, prof, printer_id=printer.id, status="cancelled", stop_source="operator_ui"
        )

        with patch.object(farm_policy.notification_service, "on_run_unit_stopped", new_callable=AsyncMock):
            await farm_policy.on_terminal(db_session, printer.id, item.id, "cancelled")

        assert [i for i in await _items(db_session, batch.id) if i.retry_of_id == item.id] == []
        await db_session.refresh(batch)
        assert batch.status == "active"
        assert batch.pause_reason == "operator_stop"  # RESUME tops the deficit back up

    async def test_a_completed_terminal_is_never_requeued(self, db_session):
        """A stop that lost the race to a finishing print produced a part."""
        printer = await _mk_printer_row(db_session, "GRQ4")
        batch, prof = await _mk_run(db_session, quantity=2, printer_ids=[printer.id], require_fa=False)
        item = await _mk_live_unit(
            db_session,
            batch,
            prof,
            printer_id=printer.id,
            status="completed",
            stop_source=farm_correlation.STOP_SOURCE_FARM_VISION_ABORT,
        )
        await _open_vision_incident(db_session, printer.id, item_id=item.id)

        await farm_policy.on_terminal(db_session, printer.id, item.id, "completed")

        assert [i for i in await _items(db_session, batch.id) if i.retry_of_id == item.id] == []


class TestPlateVisionTerminal:
    """W9's post-terminal half: re-check once, then hold the printer for a human.

    ``pause_recovery`` owns the trip (incident, abort mark, stop). Everything from the
    terminal on is decided here, because only this lane can tell a FIRST trip from a
    CONFIRMED one — and only it owns the requeue.
    """

    @pytest.fixture(autouse=True)
    def _clean_incidents(self):
        printer_incidents._reset_state()
        yield
        printer_incidents._reset_state()

    async def _tripped_unit(self, db, printer, *, model_targeted=False):
        batch, prof = await _mk_run(
            db,
            quantity=2,
            printer_ids=None if model_targeted else [printer.id],
            target_model="H2S" if model_targeted else None,
            require_fa=False,
        )
        item = await _mk_live_unit(
            db,
            batch,
            prof,
            printer_id=printer.id,
            target_model="H2S" if model_targeted else None,
            status="cancelled",
            stop_source=farm_correlation.STOP_SOURCE_FARM_VISION_ABORT,
        )
        return batch, item

    async def test_first_trip_requeues_for_the_printers_own_recheck(self, db_session):
        """Vouched + no deposit: no gate, no page, incident resolved at the terminal."""
        printer = await _mk_printer_row(db_session, "PV1")
        await _seed_hold_geometry(db_session)
        batch, item = await self._tripped_unit(db_session, printer)
        incident = await _open_vision_incident(db_session, printer.id, item_id=item.id)
        client = _ParkClient()

        from backend.app.services.eject import monitor as monitor_mod

        with (
            patch.object(farm_policy.printer_manager, "get_status", return_value=_vision_state(detector=True)),
            patch.object(farm_policy.printer_manager, "get_client", return_value=client),
            patch.object(monitor_mod, "notify_plate_not_empty", new_callable=AsyncMock) as page,
        ):
            await farm_policy.on_terminal(db_session, printer.id, item.id, "cancelled")

        retries = [i for i in await _items(db_session, batch.id) if i.retry_of_id == item.id]
        assert len(retries) == 1
        assert plate_occupancy.snapshot(printer.id).plate_occupied is False  # no gate
        page.assert_not_awaited()
        assert client.sent == []  # a re-check lifts its own bed at the next start
        await db_session.refresh(incident)
        assert incident.resolved_at is not None
        assert incident.resolve_source == "terminal"

    async def test_first_trip_gates_when_the_farm_cannot_vouch_for_the_recheck(self, db_session):
        """Detector not reported ON: the requeued start may not check at all."""
        printer = await _mk_printer_row(db_session, "PV2")
        await _seed_hold_geometry(db_session)
        batch, item = await self._tripped_unit(db_session, printer)
        await _open_vision_incident(db_session, printer.id, item_id=item.id)

        with (
            patch.object(farm_policy.printer_manager, "get_status", return_value=_vision_state(detector=False)),
            patch.object(farm_policy.printer_manager, "get_client", return_value=_ParkClient()),
        ):
            await farm_policy.on_terminal(db_session, printer.id, item.id, "cancelled")

        view = plate_occupancy.snapshot(printer.id)
        assert view.plate_occupied is True
        assert isinstance(view.plate_policy, EscalationOnly)
        assert view.plate_source_subtask_id is None  # human-clear only
        assert len([i for i in await _items(db_session, batch.id) if i.retry_of_id == item.id]) == 1

    async def test_first_trip_gates_when_the_terminal_carried_a_deposit(self, db_session):
        """Unreliable peaks after a restart read as a deposit — the farm cannot say
        what is on that plate, so it fails closed even though the detector is on."""
        printer = await _mk_printer_row(db_session, "PV3")
        await _seed_hold_geometry(db_session)
        batch, item = await self._tripped_unit(db_session, printer)
        await _open_vision_incident(db_session, printer.id, item_id=item.id)
        # What note_terminal would have armed for a deposit-bearing farm terminal.
        plate_occupancy.hydrate_plate(printer.id, "SUB-D", CooldownEject(unit_id=item.id, run_id=batch.id))

        with (
            patch.object(farm_policy.printer_manager, "get_status", return_value=_vision_state(detector=True)),
            patch.object(farm_policy.printer_manager, "get_client", return_value=_ParkClient()),
        ):
            await farm_policy.on_terminal(db_session, printer.id, item.id, "cancelled")

        view = plate_occupancy.snapshot(printer.id)
        assert isinstance(view.plate_policy, EscalationOnly)  # never swept automatically
        assert view.plate_source_subtask_id is None
        assert len([i for i in await _items(db_session, batch.id) if i.retry_of_id == item.id]) == 1

    async def test_second_trip_confirms_holds_gates_pages_and_lifts(self, db_session):
        printer = await _mk_printer_row(db_session, "PV4")
        await _seed_hold_geometry(db_session, hold_lift=15.0)
        batch, item = await self._tripped_unit(db_session, printer, model_targeted=True)
        # The FIRST trip, already resolved at its own terminal (the re-check).
        first = await _open_vision_incident(db_session, printer.id, item_id=item.id)
        await printer_incidents.close(db_session, first.id, status="resolved", source="terminal")
        second = await _open_vision_incident(db_session, printer.id, item_id=item.id)
        client = _ParkClient()

        from backend.app.services.eject import monitor as monitor_mod

        with (
            patch.object(farm_policy.printer_manager, "get_status", return_value=_vision_state(detector=True)),
            patch.object(farm_policy.printer_manager, "get_client", return_value=client),
            patch.object(monitor_mod, "notify_plate_not_empty", new_callable=AsyncMock) as page,
        ):
            await farm_policy.on_terminal(db_session, printer.id, item.id, "cancelled")

        # The hold: escalated, chip lit, hourly nag armed — and NOT resolved.
        await db_session.refresh(second)
        assert second.status == "escalated"
        assert second.resolved_at is None
        # The gate, raised AFTER the terminal: human-clear only, never a CooldownEject
        # built for a part that is not there.
        view = plate_occupancy.snapshot(printer.id)
        assert view.plate_occupied is True
        assert isinstance(view.plate_policy, EscalationOnly)
        assert view.plate_source_subtask_id is None
        # One page, carrying the confirmed sentence.
        page.assert_awaited_once()
        assert page.await_args.kwargs["source_detail"] == farm_policy._VISION_CONFIRMED_DETAIL
        # The lift: guarded DOWN onto the stop, then up by the model's own figure.
        assert client.sent == ["M17\nG91\nG380 S2 Z32.0 F1200\nG380 S2 Z-15.0 F1200\nG90\nM400\nM18"]
        # A model-targeted unit returns to the POOL rather than to the held printer.
        retries = [i for i in await _items(db_session, batch.id) if i.retry_of_id == item.id]
        assert len(retries) == 1
        assert retries[0].printer_id is None
        assert retries[0].target_model == "H2S"

    async def test_a_foreign_trip_is_confirmed_on_its_first(self, db_session):
        """No unit to requeue means no second opinion to buy."""
        printer = await _mk_printer_row(db_session, "PV5")
        await _seed_hold_geometry(db_session)
        incident = await _open_vision_incident(db_session, printer.id, item_id=None)
        client = _ParkClient()

        from backend.app.services.eject import monitor as monitor_mod

        with (
            patch.object(farm_policy.printer_manager, "get_status", return_value=_vision_state(detector=True)),
            patch.object(farm_policy.printer_manager, "get_client", return_value=client),
            patch.object(monitor_mod, "notify_plate_not_empty", new_callable=AsyncMock) as page,
        ):
            await farm_policy.on_terminal(db_session, printer.id, None, "cancelled")

        await db_session.refresh(incident)
        assert incident.status == "escalated"
        view = plate_occupancy.snapshot(printer.id)
        assert view.plate_occupied is True and isinstance(view.plate_policy, EscalationOnly)
        page.assert_awaited_once()
        assert client.sent  # the bed is lifted for the human who has to reach the part

    async def test_the_lift_is_skipped_on_a_bedslinger(self, db_session):
        """The gantry carries Z there — the same physics that refuses the bed-drop."""
        printer = await _mk_printer_row(db_session, "PV6", model="A1")
        await _seed_hold_geometry(db_session, model_key="A1", z_travel=250.0)
        incident = await _open_vision_incident(db_session, printer.id, item_id=None)
        client = _ParkClient()

        from backend.app.services.eject import monitor as monitor_mod

        with (
            patch.object(farm_policy.printer_manager, "get_status", return_value=_vision_state(detector=True)),
            patch.object(farm_policy.printer_manager, "get_client", return_value=client),
            patch.object(monitor_mod, "notify_plate_not_empty", new_callable=AsyncMock),
        ):
            await farm_policy.on_terminal(db_session, printer.id, None, "cancelled")

        assert client.sent == []
        await db_session.refresh(incident)
        assert incident.status == "escalated"  # the hold still stands

    async def test_an_unrelated_open_incident_keeps_the_hold_it_owns(self, db_session):
        """One open incident per printer: a fault that took the printer between the
        stop and the terminal owns it, and this lane must not re-decide."""
        printer = await _mk_printer_row(db_session, "PV7")
        await _seed_hold_geometry(db_session)
        batch, item = await self._tripped_unit(db_session, printer)
        assert (
            await printer_incidents.open_new(
                db_session,
                printer_id=printer.id,
                job_id="task-jam",
                item_id=item.id,
                kind=KIND_RUNOUT,
                code="0700_8011",
                codes="0700_8011",
                slot_global_tray=None,
                status=STATUS_ESCALATED,
            )
            is not None
        )
        client = _ParkClient()

        with (
            patch.object(farm_policy.printer_manager, "get_status", return_value=_vision_state(detector=True)),
            patch.object(farm_policy.printer_manager, "get_client", return_value=client),
        ):
            await farm_policy.on_terminal(db_session, printer.id, item.id, "cancelled")

        assert client.sent == []
        assert plate_occupancy.snapshot(printer.id).plate_occupied is False
        assert [i for i in await _items(db_session, batch.id) if i.retry_of_id == item.id] == []

    async def test_a_terminal_on_an_already_held_printer_re_decides_nothing(self, db_session):
        """The hold stands until a human clears it — every later terminal is a no-op.

        Without this the page and the bed lift would re-fire on every terminal the
        printer produced while it waited.
        """
        printer = await _mk_printer_row(db_session, "PV8")
        await _seed_hold_geometry(db_session)
        incident = await _open_vision_incident(db_session, printer.id, item_id=None)
        await printer_incidents.mark_escalated(db_session, incident.id)
        client = _ParkClient()

        from backend.app.services.eject import monitor as monitor_mod

        with (
            patch.object(farm_policy.printer_manager, "get_status", return_value=_vision_state(detector=True)),
            patch.object(farm_policy.printer_manager, "get_client", return_value=client),
            patch.object(monitor_mod, "notify_plate_not_empty", new_callable=AsyncMock) as page,
        ):
            await farm_policy.on_terminal(db_session, printer.id, None, "cancelled")

        page.assert_not_awaited()
        assert client.sent == []


class TestRecoverClosesOperatorResolvedHolds:
    """One-click Recover is the stronger form of "I cleared the plate"."""

    @pytest.fixture(autouse=True)
    def _clean_incidents(self):
        printer_incidents._reset_state()
        yield
        printer_incidents._reset_state()

    @pytest.fixture(autouse=True)
    def _lane_session(self, test_engine, monkeypatch):
        """``pause_recovery.on_plate_cleared`` opens its OWN session — it is also a
        fire-and-forget entry point off the wire, so it cannot borrow a caller's.
        Point ``async_session`` at the test engine, the shape ``own_session_factory``
        documents."""
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from backend.app.core import database as core_db

        monkeypatch.setattr(
            core_db, "async_session", async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        )

    async def test_recover_closes_a_plate_vision_hold(self, db_session):
        printer = await _mk_printer_row(db_session, "REC1")
        incident = await _open_vision_incident(db_session, printer.id, item_id=None)
        await printer_incidents.mark_escalated(db_session, incident.id)

        await farm_policy.recover_printer(db_session, printer.id)

        await db_session.refresh(incident)
        assert incident.resolved_at is not None
        assert incident.resolve_source == "operator"
        assert printer_incidents.snapshot(printer.id) is None  # the chip goes dark

    async def test_recover_leaves_a_wire_resolved_hold_standing(self, db_session):
        """A runout is not answered by somebody clearing a plate."""
        printer = await _mk_printer_row(db_session, "REC2")
        incident = await printer_incidents.open_new(
            db_session,
            printer_id=printer.id,
            job_id="task-runout",
            item_id=None,
            kind=KIND_RUNOUT,
            code="0700_8011",
            codes="0700_8011",
            slot_global_tray=None,
            status=STATUS_ESCALATED,
        )

        await farm_policy.recover_printer(db_session, printer.id)

        await db_session.refresh(incident)
        assert incident.resolved_at is None


class TestEscalationNeverStops:
    """Operator decision 2026-09-04: an unrecoverable filament fault stays PAUSED and
    RESUMABLE. ``spool_recovery`` escalates and holds; it never stops the print — the
    graceful half of that decision is :func:`farm_policy.on_farm_requeue`, which turns
    the operator's OWN stop of a held print into a requeue.

    Pinned at MODULE scope by parsing the recovery lane: the invariant is "no stop is
    ever sent from this lane", not "not from this one branch", and an AST walk is the
    only assertion that covers every path including the ones a future wave adds.
    """

    async def test_the_recovery_lane_sends_no_stop_anywhere(self):
        import ast
        import inspect

        from backend.app.services import spool_recovery

        tree = ast.parse(inspect.getsource(spool_recovery))
        called = {
            node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        }
        forbidden = {"stop_print", "mark_printer_stopped_by_user"}
        assert called & forbidden == set(), f"spool_recovery must never stop a print: {called & forbidden}"
