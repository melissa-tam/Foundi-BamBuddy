"""Unit tests for the farm first-article + failure/quarantine policy (Phase 3)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from backend.app.models.eject_profile import EjectProfile
from backend.app.models.library import LibraryFile
from backend.app.models.print_batch import PrintBatch
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.sku import Sku, SkuFile
from backend.app.services import farm_policy
from backend.app.services.notification_service import notification_service
from backend.app.services.printer_manager import printer_manager

pytestmark = pytest.mark.asyncio


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
        # retry_count already at the max → on-failure must not create another.
        item = PrintQueueItem(
            batch_id=batch.id,
            status="failed",
            first_article=False,
            printer_id=3,
            eject_profile_id=prof.id,
            plate_id=1,
            retry_count=1,
            position=99,
            completed_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        await db_session.commit()
        await farm_policy._on_item_failed(db_session, batch, item)
        retries = [i for i in await _items(db_session, batch.id) if i.retry_of_id == item.id]
        assert retries == []


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
        # cleanup shared in-memory singleton state
        printer_manager.set_quarantined(printer.id, False)

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
        assert printer_manager.is_quarantined(printer.id) is False


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
        printer_manager.set_awaiting_plate_clear(printer.id, True)

        try:
            summary = await farm_policy.recover_printer(db_session, printer.id)

            assert summary["plate_cleared"] is True
            assert summary["quarantine_cleared"] is True
            assert summary["runs_resumed"] == [batch.id]

            await db_session.refresh(printer)
            await db_session.refresh(batch)
            await db_session.refresh(item)
            assert printer.quarantined is False
            assert printer.quarantine_reason is None
            assert printer_manager.is_awaiting_plate_clear(printer.id) is False
            assert printer_manager.is_quarantined(printer.id) is False
            assert batch.status == "active"
            assert item.manual_start is False  # resume un-staged the pending item

            # Idempotent: a second call is a no-op with no error.
            summary2 = await farm_policy.recover_printer(db_session, printer.id)
            assert summary2["plate_cleared"] is False
            assert summary2["quarantine_cleared"] is False
            assert summary2["runs_resumed"] == []
        finally:
            # Clean up shared in-memory singleton state.
            printer_manager.set_quarantined(printer.id, False)
            printer_manager.set_awaiting_plate_clear(printer.id, False)

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


async def _mk_failed_item(db, batch, prof, *, printer_id=3, retry_count=0, target_model=None, pos=99):
    item = PrintQueueItem(
        batch_id=batch.id,
        status="failed",
        first_article=False,
        printer_id=printer_id,
        target_model=target_model,
        eject_profile_id=prof.id,
        plate_id=1,
        retry_count=retry_count,
        position=pos,
        completed_at=datetime.now(timezone.utc),
    )
    db.add(item)
    await db.commit()
    return item


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
    """Retry rebalance (Phase 1, F7): model-targeted retries return to the pool."""

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
        # retry_count already at max → no retry minted; no other live items.
        failed = await _mk_failed_item(db_session, batch, prof, printer_id=3, retry_count=1, pos=1)

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
        fa.retry_count = 1  # at max → no retry, so zero live items after this event
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
        fa.retry_count = 1  # dead chain (state never left pending_print)
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

    async def test_finalize_remote_eject_fires_first_article_approved_once(self, db_session):
        batch, _ = await _mk_run(db_session, quantity=3, printer_ids=[7])
        fa = (await _items(db_session, batch.id))[0]
        fa.status = "completed"
        batch.first_article_state = "awaiting_approval"
        await db_session.commit()

        with patch.object(
            farm_policy.notification_service, "on_first_article_approved", new_callable=AsyncMock
        ) as mock_n:
            await farm_policy._finalize_remote_eject(db_session, batch.id, 7)

        await db_session.refresh(batch)
        assert batch.first_article_state == "approved"
        # Both approval paths (physical + remote eject) close the FA-pending loop.
        mock_n.assert_awaited_once()

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


class TestPendingEjectRegistry:
    """The typed pending-eject registry (moved to eject.remote) round-trips."""

    async def test_register_peek_pop(self):
        from backend.app.services.eject import remote

        pe = remote.PendingEject(purpose="production", run_id=5, queue_item_id=7)
        remote.register_pending_eject(42, pe)
        try:
            assert remote.peek_pending_eject(42) == pe
            assert remote.peek_pending_eject(42).purpose == "production"
            assert remote.pop_pending_eject(42) == pe
            assert remote.peek_pending_eject(42) is None
            assert remote.pop_pending_eject(42) is None  # idempotent pop
        finally:
            remote.pop_pending_eject(42)


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

    async def test_production_completed_clears_gate(self, db_session):
        from backend.app.services.eject import remote

        printer = await self._mk_printer(db_session, "PEok")
        batch, _ = await _mk_run(db_session, quantity=2, printer_ids=[printer.id], require_fa=False)
        remote.register_pending_eject(printer.id, remote.PendingEject("production", batch.id, 111))
        cleared = []
        try:
            with (
                patch.object(farm_policy.printer_manager, "get_client", return_value=self._fake_client("SUB-E")),
                patch.object(
                    farm_policy.printer_manager,
                    "set_awaiting_plate_clear",
                    side_effect=lambda pid, v, **kw: cleared.append((pid, v)),
                ),
            ):
                await farm_policy.on_terminal(db_session, printer.id, None, "completed", completed_subtask_id="SUB-E")
            assert remote.peek_pending_eject(printer.id) is None  # popped
            assert cleared == [(printer.id, False)]  # gate released
        finally:
            remote.pop_pending_eject(printer.id)

    async def test_production_failed_keeps_gate_and_quarantines(self, db_session):
        from backend.app.services.eject import remote

        printer = await self._mk_printer(db_session, "PEfail")
        batch, _ = await _mk_run(db_session, quantity=2, printer_ids=[printer.id], require_fa=False)
        remote.register_pending_eject(printer.id, remote.PendingEject("production", batch.id, 222))
        cleared = []
        try:
            with (
                patch.object(farm_policy.printer_manager, "get_client", return_value=self._fake_client("SUB-E")),
                patch.object(
                    farm_policy.printer_manager,
                    "set_awaiting_plate_clear",
                    side_effect=lambda pid, v, **kw: cleared.append((pid, v)),
                ),
                patch.object(farm_policy.printer_manager, "set_quarantined"),
                patch.object(farm_policy.notification_service, "on_printer_quarantined", new_callable=AsyncMock),
            ):
                await farm_policy.on_terminal(db_session, printer.id, None, "failed", completed_subtask_id="SUB-E")
            assert remote.peek_pending_eject(printer.id) is None  # job ended → popped
            assert cleared == []  # gate KEPT — sweep unverified
            await db_session.refresh(printer)
            assert printer.quarantined is True
            assert "sweep unverified" in (printer.quarantine_reason or "")
        finally:
            remote.pop_pending_eject(printer.id)

    async def test_manual_completed_clears_gate(self, db_session):
        # A foreign-plate manual eject owns no run/queue item: completed clears the
        # gate exactly like production, matched by the printer-keyed stem.
        from backend.app.services.eject import remote

        printer = await self._mk_printer(db_session, "MANok")
        remote.register_pending_eject(printer.id, remote.PendingEject("manual", None, None))
        cleared = []
        try:
            with (
                patch.object(farm_policy.printer_manager, "get_client", return_value=self._fake_client(None)),
                patch.object(
                    farm_policy.printer_manager,
                    "set_awaiting_plate_clear",
                    side_effect=lambda pid, v, **kw: cleared.append((pid, v)),
                ),
            ):
                await farm_policy.on_terminal(
                    db_session,
                    printer.id,
                    None,
                    "completed",
                    completed_subtask_id=None,
                    completed_subtask_name=f"eject_manual_p{printer.id}",
                )
            assert remote.peek_pending_eject(printer.id) is None  # popped
            assert cleared == [(printer.id, False)]  # gate released
        finally:
            remote.pop_pending_eject(printer.id)

    async def test_manual_failed_keeps_gate_no_quarantine(self, db_session):
        # A manual eject that ends non-completed keeps the gate raised (fail-closed)
        # and — unlike production/FA — NEVER quarantines (it owns no run to protect).
        from backend.app.services.eject import remote

        printer = await self._mk_printer(db_session, "MANfail")
        remote.register_pending_eject(printer.id, remote.PendingEject("manual", None, None))
        cleared = []
        try:
            with (
                patch.object(farm_policy.printer_manager, "get_client", return_value=self._fake_client(None)),
                patch.object(
                    farm_policy.printer_manager,
                    "set_awaiting_plate_clear",
                    side_effect=lambda pid, v, **kw: cleared.append((pid, v)),
                ),
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
            assert remote.peek_pending_eject(printer.id) is None  # job ended → popped
            assert cleared == []  # gate KEPT — sweep unverified
            set_q.assert_not_called()  # NO quarantine
            await db_session.refresh(printer)
            assert printer.quarantined is False
        finally:
            remote.pop_pending_eject(printer.id)

    async def test_echo_mismatch_keeps_pending(self, db_session):
        from backend.app.services.eject import remote

        printer = await self._mk_printer(db_session, "PEmis")
        remote.register_pending_eject(printer.id, remote.PendingEject("production", 1, 333))
        try:
            with patch.object(farm_policy.printer_manager, "get_client", return_value=self._fake_client("SUB-E")):
                await farm_policy.on_terminal(db_session, printer.id, None, "completed", completed_subtask_id="OTHER")
            assert remote.peek_pending_eject(printer.id) is not None  # foreign — pending kept
        finally:
            remote.pop_pending_eject(printer.id)

    async def test_fa_eject_terminal_finalizes(self, db_session):
        from backend.app.services.eject import remote

        printer = await self._mk_printer(db_session, "FAej")
        batch, _ = await _mk_run(db_session, quantity=3, printer_ids=[printer.id], require_fa=True)
        fa = (await _items(db_session, batch.id))[0]
        fa.status = "completed"
        batch.first_article_state = "awaiting_approval"
        await db_session.commit()
        remote.register_pending_eject(printer.id, remote.PendingEject("fa", batch.id, fa.id))
        try:
            with (
                patch.object(farm_policy.printer_manager, "get_client", return_value=self._fake_client("SUB-E")),
                patch.object(farm_policy.printer_manager, "set_awaiting_plate_clear"),
                patch.object(farm_policy.notification_service, "on_first_article_approved", new_callable=AsyncMock),
            ):
                await farm_policy.on_terminal(db_session, printer.id, None, "completed", completed_subtask_id="SUB-E")
            assert remote.peek_pending_eject(printer.id) is None
            await db_session.refresh(batch)
            assert batch.first_article_state == "approved"
        finally:
            remote.pop_pending_eject(printer.id)

    async def test_fa_eject_failed_terminal_keeps_awaiting_and_quarantines(self, db_session):
        """A FAILED FA eject must NOT approve/materialise: the run stays
        awaiting_approval (re-approvable after recovery), the gate is untouched,
        and the printer is quarantined like the production branch."""
        from backend.app.services.eject import remote

        printer = await self._mk_printer(db_session, "FAfail")
        batch, _ = await _mk_run(db_session, quantity=3, printer_ids=[printer.id], require_fa=True)
        fa = (await _items(db_session, batch.id))[0]
        fa.status = "completed"
        batch.first_article_state = "awaiting_approval"
        await db_session.commit()
        remote.register_pending_eject(printer.id, remote.PendingEject("fa", batch.id, fa.id))
        cleared = []
        try:
            with (
                patch.object(farm_policy.printer_manager, "get_client", return_value=self._fake_client("SUB-E")),
                patch.object(
                    farm_policy.printer_manager,
                    "set_awaiting_plate_clear",
                    side_effect=lambda pid, v, **kw: cleared.append((pid, v)),
                ),
                patch.object(farm_policy.printer_manager, "set_quarantined"),
                patch.object(farm_policy.notification_service, "on_printer_quarantined", new_callable=AsyncMock),
                patch.object(
                    farm_policy.notification_service, "on_first_article_approved", new_callable=AsyncMock
                ) as approved_note,
            ):
                await farm_policy.on_terminal(db_session, printer.id, None, "failed", completed_subtask_id="SUB-E")
            assert remote.peek_pending_eject(printer.id) is None  # job ended → popped
            assert cleared == []  # gate untouched
            approved_note.assert_not_awaited()
            await db_session.refresh(batch)
            assert batch.first_article_state == "awaiting_approval"  # NOT finalised
            await db_session.refresh(printer)
            assert printer.quarantined is True
            assert "First-article eject job ended 'failed'" in (printer.quarantine_reason or "")
        finally:
            remote.pop_pending_eject(printer.id)

    async def test_name_mismatch_during_pending_keeps_pending(self, db_session):
        # W1/R2: a foreign terminal whose echoed NAME is not our eject stem is a
        # positive mismatch even when the id path is lenient (no client) — the
        # pending is kept for the real eject terminal.
        from backend.app.services.eject import remote

        printer = await self._mk_printer(db_session, "PEname")
        remote.register_pending_eject(printer.id, remote.PendingEject("production", 1, 444))
        try:
            with patch.object(farm_policy.printer_manager, "get_client", return_value=None):
                await farm_policy.on_terminal(
                    db_session,
                    printer.id,
                    None,
                    "completed",
                    completed_subtask_id=None,
                    completed_subtask_name="OperatorLocalPrint",
                )
            assert remote.peek_pending_eject(printer.id) is not None  # foreign name — kept
        finally:
            remote.pop_pending_eject(printer.id)

    async def test_completed_clears_gate_and_nulls_stamp(self, db_session):
        # A name-matched production eject completion clears the gate AND NULLs the
        # durable eject_dispatched_at stamp (atomic resolution, W1).
        from datetime import datetime, timezone

        from backend.app.services.eject import remote

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
        remote.register_pending_eject(printer.id, remote.PendingEject("production", None, item.id))
        cleared = []
        try:
            with (
                patch.object(farm_policy.printer_manager, "get_client", return_value=None),
                patch.object(
                    farm_policy.printer_manager,
                    "set_awaiting_plate_clear",
                    side_effect=lambda pid, v, **kw: cleared.append((pid, v)),
                ),
            ):
                await farm_policy.on_terminal(
                    db_session,
                    printer.id,
                    None,
                    "completed",
                    completed_subtask_id=None,
                    completed_subtask_name=f"eject_production_item{item.id}",
                )
            assert remote.peek_pending_eject(printer.id) is None  # popped
            assert cleared == [(printer.id, False)]  # gate released
            await db_session.refresh(item)
            assert item.eject_dispatched_at is None  # durable stamp NULLed
        finally:
            remote.pop_pending_eject(printer.id)


class TestFaEjectCooldownGate:
    """approve-with-remote-eject honours the release threshold: hot bed defers to
    the FA cooldown watch (motion-only file must not sweep a hot plate); cold bed
    dispatches immediately (old UX, incl. 409s); disconnected printer is a 409."""

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

    async def test_hot_bed_arms_fa_watch_instead_of_dispatching(self, db_session):
        import backend.app.services.eject.monitor as monitor_mod
        from backend.app.services.eject import remote

        printer, batch, fa = await self._fa_fixture(db_session)
        armed = []
        with (
            patch.object(farm_policy.printer_manager, "is_connected", return_value=True),
            patch.object(farm_policy.printer_manager, "get_status", return_value=self._state(80.0)),
            patch.object(monitor_mod, "_resolve_eject_threshold", new=AsyncMock(return_value=33.0)),
            patch.object(
                monitor_mod.eject_cooldown_monitor,
                "start_fa_eject_watch",
                side_effect=lambda pid, qid, rid: armed.append((pid, qid, rid)) or True,
            ),
            patch.object(remote, "dispatch_part_present_eject", new_callable=AsyncMock) as direct,
        ):
            await farm_policy._dispatch_remote_eject(db_session, batch, fa)
        assert armed == [(printer.id, fa.id, batch.id)]
        direct.assert_not_awaited()

    async def test_cold_bed_dispatches_immediately(self, db_session):
        import backend.app.services.eject.monitor as monitor_mod

        printer, batch, fa = await self._fa_fixture(db_session)
        with (
            patch.object(farm_policy.printer_manager, "is_connected", return_value=True),
            patch.object(farm_policy.printer_manager, "get_status", return_value=self._state(30.5)),
            patch.object(monitor_mod, "_resolve_eject_threshold", new=AsyncMock(return_value=33.0)),
            patch.object(
                monitor_mod.eject_cooldown_monitor, "start_fa_eject_watch", side_effect=AssertionError
            ) as watch,
            patch.object(farm_policy.eject_remote, "dispatch_part_present_eject", new_callable=AsyncMock) as direct,
        ):
            await farm_policy._dispatch_remote_eject(db_session, batch, fa)
        direct.assert_awaited_once()
        assert direct.await_args.kwargs["purpose"] == "fa"
        assert not watch.called

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
