"""Run-visibility derived fields + hold lifecycle (Phase 4.1).

``build_run_response`` must DERIVE (never store) the hold summary — pause_reason
passthrough, the staged split, per-printer blocked states, has_blocked_printers —
with the full ``printer_states``/``units`` payloads only on ``detail=True``.
The four pause_reason setters (operator pause, operator stop, first-article
reject, no-available-printers auto-pause) each stamp their machine code and
resume clears it; every mutation site fires ONE ``production_run_changed``
broadcast (spied per call site). FK enforcement is off in the test engine.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from backend.app.models.library import LibraryFile
from backend.app.models.print_batch import PrintBatch
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.sku import Sku, SkuFile
from backend.app.services import farm_policy, farm_stall, production_run
from backend.app.services.notification_service import notification_service
from backend.app.services.production_run import _load_run, build_run_response, transition_run

pytestmark = pytest.mark.asyncio


async def _mk_run(db, *, quantity=2, status="active", fa_state=None):
    lib = LibraryFile(filename="f.gcode.3mf", file_path="/tmp/f.gcode.3mf", file_type="gcode.3mf", file_size=1)
    db.add(lib)
    await db.flush()
    sku = Sku(code=f"SKU{lib.id:03d}", name="Widget")
    db.add(sku)
    await db.flush()
    sf = SkuFile(sku_id=sku.id, library_file_id=lib.id, plate_index=1, units_per_plate=1)
    db.add(sf)
    await db.flush()
    batch = PrintBatch(
        name="run",
        quantity=quantity,
        status=status,
        sku_file_id=sf.id,
        target_units=quantity,
        require_first_article=fa_state is not None,
        first_article_state=fa_state,
    )
    db.add(batch)
    await db.flush()
    return batch


async def _mk_printer(db, *, name="P1", quarantined=False):
    p = Printer(
        name=name,
        serial_number=f"SN-{name}",
        ip_address="192.0.2.1",
        access_code="123",
        model="H2S",
        quarantined=quarantined,
    )
    db.add(p)
    await db.flush()
    return p


async def _add(db, batch, **kw):
    fields = {
        "batch_id": batch.id,
        "printer_id": None,
        "status": "pending",
        "first_article": False,
        "plate_id": 1,
        "position": kw.pop("pos", 1),
    }
    fields.update(kw)
    it = PrintQueueItem(**fields)
    db.add(it)
    await db.flush()
    return it


@pytest.fixture(autouse=True)
def _reset_stall_state():
    farm_stall._reset_state()
    yield
    farm_stall._reset_state()


@pytest.fixture
def run_changed_spy(monkeypatch):
    """Record every production_run_changed broadcast from every call site."""
    calls: list[int] = []

    def spy(run_id: int) -> None:
        calls.append(run_id)

    monkeypatch.setattr(production_run, "broadcast_production_run_changed", spy)
    monkeypatch.setattr(farm_policy, "broadcast_production_run_changed", spy)
    monkeypatch.setattr(farm_stall, "broadcast_production_run_changed", spy)
    return calls


class TestDerivedFields:
    async def test_list_shape_lean_and_detail_shape_full(self, db_session):
        batch = await _mk_run(db_session, quantity=2)
        p = await _mk_printer(db_session)
        await _add(db_session, batch, printer_id=p.id, status="completed", pos=1)
        await _add(db_session, batch, printer_id=p.id, status="pending", pos=2)
        await db_session.commit()
        run = await _load_run(db_session, batch.id)

        lean = await build_run_response(db_session, run)
        assert lean["pause_reason"] is None
        assert lean["staged_filament_short"] == 0
        assert lean["staged_other"] == 0
        assert lean["has_blocked_printers"] is False
        assert "printer_states" not in lean
        assert "units" not in lean

        full = await build_run_response(db_session, run, detail=True)
        assert [u["status"] for u in full["units"]] == ["completed", "pending"]
        assert full["printer_states"][0]["printer_id"] == p.id
        assert full["printer_states"][0]["name"] == p.name

    async def test_staged_counts_split_by_filament_short(self, db_session):
        batch = await _mk_run(db_session, quantity=4)
        await _add(db_session, batch, status="pending", manual_start=True, filament_short=True, pos=1)
        await _add(db_session, batch, status="pending", manual_start=True, filament_short=True, pos=2)
        await _add(db_session, batch, status="pending", manual_start=True, filament_short=False, pos=3)
        # Non-pending staged rows never count.
        await _add(db_session, batch, status="printing", manual_start=True, filament_short=True, pos=4)
        await db_session.commit()
        run = await _load_run(db_session, batch.id)
        resp = await build_run_response(db_session, run)
        assert resp["staged_filament_short"] == 2
        assert resp["staged_other"] == 1

    async def test_quarantined_printer_blocks_and_surfaces(self, db_session):
        batch = await _mk_run(db_session)
        p = await _mk_printer(db_session, quarantined=True)
        await _add(db_session, batch, printer_id=p.id, status="pending")
        await db_session.commit()
        run = await _load_run(db_session, batch.id)
        resp = await build_run_response(db_session, run, detail=True)
        assert resp["has_blocked_printers"] is True
        state = resp["printer_states"][0]
        assert state["quarantined"] is True

    async def test_stalled_and_vision_hold_flags_from_waiting_reason(self, db_session):
        batch = await _mk_run(db_session, quantity=2)
        p1 = await _mk_printer(db_session, name="P1")
        p2 = await _mk_printer(db_session, name="P2")
        await _add(db_session, batch, printer_id=p1.id, status="printing", waiting_reason="printer_offline_stalled")
        await _add(
            db_session,
            batch,
            printer_id=p2.id,
            status="printing",
            waiting_reason="plate_not_empty_printer_detected",
            pos=2,
        )
        await db_session.commit()
        run = await _load_run(db_session, batch.id)
        resp = await build_run_response(db_session, run, detail=True)
        by_id = {s["printer_id"]: s for s in resp["printer_states"]}
        assert by_id[p1.id]["stalled"] is True and by_id[p1.id]["vision_hold"] is False
        assert by_id[p2.id]["vision_hold"] is True and by_id[p2.id]["stalled"] is False
        assert resp["has_blocked_printers"] is True

    async def test_units_carry_retry_lineage_and_stop_source(self, db_session):
        batch = await _mk_run(db_session, quantity=2)
        p = await _mk_printer(db_session)
        failed = await _add(db_session, batch, printer_id=p.id, status="failed", pos=1)
        retry = await _add(
            db_session,
            batch,
            printer_id=p.id,
            status="pending",
            retry_of_id=failed.id,
            retry_count=1,
            pos=2,
        )
        stopped = await _add(
            db_session,
            batch,
            printer_id=p.id,
            status="cancelled",
            stop_source="operator_screen",
            error_message="boom",
            pos=3,
            completed_at=datetime.now(timezone.utc),
        )
        await db_session.commit()
        run = await _load_run(db_session, batch.id)
        resp = await build_run_response(db_session, run, detail=True)
        units = {u["id"]: u for u in resp["units"]}
        assert units[retry.id]["retry_of_id"] == failed.id
        assert units[retry.id]["retry_count"] == 1
        assert units[stopped.id]["stop_source"] == "operator_screen"
        assert units[stopped.id]["error_message"] == "boom"
        assert units[stopped.id]["printer_name"] == p.name


class TestPauseReasonLifecycle:
    async def test_operator_pause_sets_and_resume_clears(self, db_session, run_changed_spy):
        batch = await _mk_run(db_session, quantity=1)
        await _add(db_session, batch, status="pending")
        await db_session.commit()

        run = await transition_run(db_session, batch.id, "pause")
        assert run.pause_reason == "operator"
        assert run.status == "paused"

        run = await transition_run(db_session, batch.id, "resume")
        assert run.pause_reason is None
        assert run.status == "active"
        # pause fired once, resume fired once (+ possible top-up fire, none here).
        assert run_changed_spy.count(batch.id) == 2

    async def test_operator_stop_sets_reason_and_broadcasts(self, db_session, run_changed_spy):
        batch = await _mk_run(db_session, quantity=1)
        p = await _mk_printer(db_session)
        item = await _add(
            db_session,
            batch,
            printer_id=p.id,
            status="cancelled",
            stop_source="operator_ui",
            completed_at=datetime.now(timezone.utc),
        )
        await db_session.commit()
        with patch.object(notification_service, "on_run_unit_stopped", new_callable=AsyncMock):
            await farm_policy.on_operator_stop(db_session, batch, item)
        await db_session.refresh(batch)
        assert batch.pause_reason == "operator_stop"
        assert batch.status == "active"  # a visible hold, not a pause
        assert run_changed_spy == [batch.id]

    async def test_first_article_reject_sets_reason_and_broadcasts(self, db_session, run_changed_spy):
        batch = await _mk_run(db_session, quantity=2, fa_state="awaiting_approval")
        await _add(db_session, batch, status="completed", first_article=True)
        await db_session.commit()
        with patch.object(notification_service, "on_run_paused", new_callable=AsyncMock):
            run = await farm_policy.reject_first_article(db_session, batch.id, "warped")
        assert run.status == "paused"
        assert run.pause_reason == "first_article_rejected"
        assert run_changed_spy == [batch.id]

    async def test_auto_pause_no_printers_sets_reason_and_broadcasts(self, db_session, run_changed_spy):
        batch = await _mk_run(db_session, quantity=1)
        p = await _mk_printer(db_session, quarantined=True)
        await _add(db_session, batch, printer_id=p.id, status="pending")
        await db_session.commit()
        with patch.object(notification_service, "on_run_paused", new_callable=AsyncMock):
            await farm_policy._maybe_pause_run_no_printers(db_session, batch)
        await db_session.refresh(batch)
        assert batch.status == "paused"
        assert batch.pause_reason == "no_available_printers"
        assert run_changed_spy == [batch.id]


class TestRunChangedBroadcastSites:
    async def test_run_completion_broadcasts(self, db_session, run_changed_spy):
        batch = await _mk_run(db_session, quantity=1)
        await _add(db_session, batch, status="completed", completed_at=datetime.now(timezone.utc))
        await db_session.commit()
        with patch.object(notification_service, "on_run_completed", new_callable=AsyncMock):
            await farm_policy._maybe_complete_run(db_session, batch)
        await db_session.refresh(batch)
        assert batch.status == "completed"
        assert run_changed_spy == [batch.id]

    async def test_resume_with_deficit_also_fires_top_up_broadcast(self, db_session, run_changed_spy):
        batch = await _mk_run(db_session, quantity=2)
        p = await _mk_printer(db_session)
        await _add(db_session, batch, printer_id=p.id, status="completed", pos=1)
        await _add(
            db_session,
            batch,
            printer_id=p.id,
            status="cancelled",
            pos=2,
            completed_at=datetime.now(timezone.utc),
        )
        batch.status = "paused"
        await db_session.commit()

        run = await transition_run(db_session, batch.id, "resume")
        # transition fired once + top_up_run fired once for its replacement.
        assert run_changed_spy.count(batch.id) == 2
        pend = await db_session.execute(
            select(PrintQueueItem).where(PrintQueueItem.batch_id == batch.id, PrintQueueItem.status == "pending")
        )
        assert len(list(pend.scalars().all())) == 1
        assert run.status == "active"

    async def test_farm_stall_set_and_clear_broadcast(self, db_session, run_changed_spy):
        batch = await _mk_run(db_session, quantity=1)
        item = PrintQueueItem(
            batch_id=batch.id,
            printer_id=77,
            status="printing",
            plate_id=1,
            position=1,
            started_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        await db_session.commit()

        class _Mgr:
            def __init__(self, up):
                self.up = up

            def is_connected(self, pid):
                return self.up

        grace = 30 * 60
        with patch.object(notification_service, "on_print_stalled", new_callable=AsyncMock):
            await farm_stall.check_stalled_prints(db_session, manager=_Mgr(False), now=0.0)
            await farm_stall.check_stalled_prints(db_session, manager=_Mgr(False), now=grace + 1)
        assert run_changed_spy == [batch.id]  # flag SET
        await farm_stall.check_stalled_prints(db_session, manager=_Mgr(True), now=grace + 2)
        assert run_changed_spy == [batch.id, batch.id]  # flag CLEARED

    async def test_farm_stall_non_farm_batch_does_not_broadcast(self, db_session, run_changed_spy):
        # A plain batch (no sku_file_id) stalling must not emit a run event.
        batch = PrintBatch(name="plain", quantity=1, status="active")
        db_session.add(batch)
        await db_session.flush()
        item = PrintQueueItem(
            batch_id=batch.id,
            printer_id=78,
            status="printing",
            plate_id=1,
            position=1,
            started_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        await db_session.commit()

        class _Down:
            def is_connected(self, pid):
                return False

        with patch.object(notification_service, "on_print_stalled", new_callable=AsyncMock):
            await farm_stall.check_stalled_prints(db_session, manager=_Down(), now=0.0)
            await farm_stall.check_stalled_prints(db_session, manager=_Down(), now=31 * 60)
        assert run_changed_spy == []
