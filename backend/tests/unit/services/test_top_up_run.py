"""Deficit-math tests for ``production_run.top_up_run`` (Phase 3.1).

Top-up recomputes, from LIVE queue state, how many plate slots ended WITHOUT
output (cancelled/stopped, or failed with an exhausted retry chain) and creates
exactly that many replacement items — reusing the shared ``create_queue_items``
builder. It is idempotent (never stores a counter), so a zero-deficit resume is a
no-op and a double resume never double-creates. FK enforcement is off in the test
engine, so rows reference arbitrary ids without seeding parents.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from backend.app.models.eject_profile import EjectProfile
from backend.app.models.library import LibraryFile
from backend.app.models.print_batch import PrintBatch
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.sku import Sku, SkuFile
from backend.app.services.production_run import _load_run, top_up_run, transition_run

pytestmark = pytest.mark.asyncio


async def _mk_run(db, *, quantity, printer_id, gated=False):
    lib = LibraryFile(filename="f.gcode.3mf", file_path="/tmp/f.gcode.3mf", file_type="gcode.3mf", file_size=1)
    db.add(lib)
    await db.flush()
    sku = Sku(code=f"SKU{lib.id:03d}", name="Widget")
    db.add(sku)
    await db.flush()
    sf = SkuFile(sku_id=sku.id, library_file_id=lib.id, plate_index=1, units_per_plate=1)
    db.add(sf)
    await db.flush()
    prof = EjectProfile(name=f"ep{lib.id}")
    db.add(prof)
    await db.flush()
    batch = PrintBatch(
        name="run",
        quantity=quantity,
        status="active",
        sku_file_id=sf.id,
        target_units=quantity,
        require_first_article=gated,
        first_article_state="pending_print" if gated else None,
        first_article_plan='{"remaining": 2, "printer_ids": null, "target_model": "H2S", "base_fields": {}}'
        if gated
        else None,
    )
    db.add(batch)
    await db.flush()
    return batch, lib, prof


async def _add(db, batch, *, printer_id, status, retry_of_id=None, retry_count=0, first_article=False, pos):
    it = PrintQueueItem(
        batch_id=batch.id,
        printer_id=printer_id,
        status=status,
        first_article=first_article,
        library_file_id=None,
        plate_id=1,
        position=pos,
        retry_of_id=retry_of_id,
        retry_count=retry_count,
        completed_at=datetime.now(timezone.utc) if status in ("completed", "failed", "cancelled") else None,
    )
    db.add(it)
    await db.flush()
    return it


async def _pending_count(db, batch_id):
    r = await db.execute(
        select(PrintQueueItem).where(PrintQueueItem.batch_id == batch_id).where(PrintQueueItem.status == "pending")
    )
    return len(list(r.scalars().all()))


class TestTopUpRun:
    async def test_zero_deficit_no_op(self, db_session):
        batch, _lib, _prof = await _mk_run(db_session, quantity=2, printer_id=3)
        await _add(db_session, batch, printer_id=3, status="completed", pos=1)
        await _add(db_session, batch, printer_id=3, status="pending", pos=2)
        await db_session.commit()
        run = await _load_run(db_session, batch.id)
        created = await top_up_run(db_session, run)
        assert created == 0

    async def test_stopped_unit_makes_one_replacement(self, db_session):
        batch, _lib, _prof = await _mk_run(db_session, quantity=2, printer_id=3)
        await _add(db_session, batch, printer_id=3, status="completed", pos=1)
        await _add(db_session, batch, printer_id=3, status="cancelled", pos=2)  # stopped, no output
        await db_session.commit()
        run = await _load_run(db_session, batch.id)
        created = await top_up_run(db_session, run)
        assert created == 1
        # New pending replacement targets the same printer.
        r = await db_session.execute(
            select(PrintQueueItem).where(PrintQueueItem.batch_id == batch.id).where(PrintQueueItem.status == "pending")
        )
        pend = list(r.scalars().all())
        assert len(pend) == 1
        assert pend[0].printer_id == 3
        assert pend[0].first_article is False

    async def test_failed_with_live_retry_chain_counts_as_productive(self, db_session):
        # A failed primary + its pending retry = ONE productive chain → no deficit.
        batch, _lib, _prof = await _mk_run(db_session, quantity=2, printer_id=3)
        await _add(db_session, batch, printer_id=3, status="completed", pos=1)
        failed = await _add(db_session, batch, printer_id=3, status="failed", pos=2)
        await _add(db_session, batch, printer_id=3, status="pending", retry_of_id=failed.id, retry_count=1, pos=3)
        await db_session.commit()
        run = await _load_run(db_session, batch.id)
        created = await top_up_run(db_session, run)
        assert created == 0

    async def test_exhausted_failed_chain_is_deficit(self, db_session):
        # A failed primary whose retry ALSO failed (chain dead) → 1 replacement.
        batch, _lib, _prof = await _mk_run(db_session, quantity=1, printer_id=3)
        failed = await _add(db_session, batch, printer_id=3, status="failed", pos=1)
        await _add(db_session, batch, printer_id=3, status="failed", retry_of_id=failed.id, retry_count=1, pos=2)
        await db_session.commit()
        run = await _load_run(db_session, batch.id)
        created = await top_up_run(db_session, run)
        assert created == 1

    async def test_fa_plan_pending_run_is_noop(self, db_session):
        # A gated run still deferring its plates to the FA flow → top-up no-op.
        batch, _lib, _prof = await _mk_run(db_session, quantity=3, printer_id=3, gated=True)
        await _add(db_session, batch, printer_id=3, status="cancelled", first_article=True, pos=1)
        await db_session.commit()
        run = await _load_run(db_session, batch.id)
        created = await top_up_run(db_session, run)
        assert created == 0

    async def test_idempotent_double_resume(self, db_session):
        batch, _lib, _prof = await _mk_run(db_session, quantity=2, printer_id=3)
        await _add(db_session, batch, printer_id=3, status="completed", pos=1)
        await _add(db_session, batch, printer_id=3, status="cancelled", pos=2)
        await db_session.commit()

        run = await _load_run(db_session, batch.id)
        first = await top_up_run(db_session, run)
        assert first == 1
        # Re-run against FRESH state (each production resume is a new session; detach
        # so the reload rebuilds queue_items instead of reusing the stale identity-map
        # collection). The replacement is now a live pending chain → deficit 0.
        db_session.expunge_all()
        run = await _load_run(db_session, batch.id)
        second = await top_up_run(db_session, run)
        assert second == 0
        assert await _pending_count(db_session, batch.id) == 1  # not double-created


class TestTransitionResumeWiring:
    """The real transition_run(resume) path clears pause_reason AND tops up (3.1)."""

    async def test_resume_clears_pause_reason_and_tops_up(self, db_session):
        batch, _lib, _prof = await _mk_run(db_session, quantity=2, printer_id=3)
        batch.status = "paused"
        batch.pause_reason = "operator_stop"
        await _add(db_session, batch, printer_id=3, status="completed", pos=1)
        await _add(db_session, batch, printer_id=3, status="cancelled", pos=2)
        await db_session.commit()

        run = await transition_run(db_session, batch.id, "resume")
        assert run.status == "active"
        assert run.pause_reason is None
        # The stopped unit was topped up with one replacement.
        assert await _pending_count(db_session, batch.id) == 1

    async def test_resume_zero_deficit_creates_nothing(self, db_session):
        batch, _lib, _prof = await _mk_run(db_session, quantity=2, printer_id=3)
        batch.status = "paused"
        await _add(db_session, batch, printer_id=3, status="completed", pos=1)
        await _add(db_session, batch, printer_id=3, status="pending", pos=2)
        await db_session.commit()

        await transition_run(db_session, batch.id, "resume")
        assert await _pending_count(db_session, batch.id) == 1  # only the original pending
