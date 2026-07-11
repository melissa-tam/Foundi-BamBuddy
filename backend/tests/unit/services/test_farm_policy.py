"""Unit tests for the farm first-article + failure/quarantine policy (Phase 3)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from backend.app.models.eject_profile import EjectProfile
from backend.app.models.library import LibraryFile
from backend.app.models.print_batch import PrintBatch
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.sku import Sku, SkuFile
from backend.app.services import farm_policy
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
