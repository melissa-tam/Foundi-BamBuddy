"""Unit tests for the farm first-article + failure/quarantine policy (Phase 3)."""

from datetime import datetime, timedelta, timezone

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
