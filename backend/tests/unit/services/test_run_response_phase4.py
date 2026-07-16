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
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.models.print_batch import PrintBatch
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.sku import Sku, SkuFile
from backend.app.services import farm_policy, farm_stall, production_run
from backend.app.services.capability_gate import CapabilityDecision, evaluate_capability as _real_evaluate_capability
from backend.app.services.filament_deficit import FilamentDeficit
from backend.app.services.notification_service import notification_service
from backend.app.services.print_scheduler import scheduler as _scheduler
from backend.app.services.printer_manager import printer_manager
from backend.app.services.production_run import (
    _derive_printer_unit_context,
    _load_run,
    build_farm_printer_contexts,
    build_run_response,
    transition_run,
)
from backend.app.services.spool_selection import MatchOutcome

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


def _fake_item(**kw):
    """A lightweight stand-in with the fields ``_derive_printer_unit_context``
    reads — no DB session needed for the pure derivation logic."""
    base = {
        "id": 1,
        "printer_id": 1,
        "status": "pending",
        "position": 1,
        "waiting_reason": None,
        "manual_start": False,
        "filament_short": False,
        "first_article": False,
        "error_message": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


class TestDerivePrinterUnitContext:
    """The shared per-printer unit derivation used by BOTH the run-detail printer
    states and the fleet-scoped printer contexts (single reason vocabulary)."""

    async def test_printing_unit_wins_over_pending(self):
        items = [
            _fake_item(id=1, status="pending", position=1),
            _fake_item(id=2, status="printing", position=2),
        ]
        ctx = _derive_printer_unit_context(1, items)
        assert ctx["unit_id"] == 2
        assert ctx["unit_status"] == "printing"
        assert ctx["rank"] == 3
        assert ctx["staged"] is False

    async def test_pending_manual_start_is_staged_low_spool(self):
        items = [_fake_item(id=5, status="pending", manual_start=True, filament_short=True)]
        ctx = _derive_printer_unit_context(1, items)
        assert ctx["unit_status"] == "pending"
        assert ctx["staged"] is True
        assert ctx["filament_short"] is True
        assert ctx["rank"] == 2

    async def test_waiting_reason_from_live_unit(self):
        items = [_fake_item(id=1, status="printing", waiting_reason="printer_offline_stalled")]
        assert _derive_printer_unit_context(1, items)["waiting_reason"] == "printer_offline_stalled"

    async def test_failed_error_only_without_live_unit(self):
        failed = _fake_item(id=1, status="failed", error_message="boom")
        # A live printing unit suppresses the failed error_message.
        ctx = _derive_printer_unit_context(1, [failed, _fake_item(id=2, status="printing")])
        assert ctx["unit_status"] == "printing"
        assert ctx["error_message"] is None
        # No live unit → the last failure explains the idle printer.
        ctx2 = _derive_printer_unit_context(1, [failed, _fake_item(id=2, status="completed")])
        assert ctx2["unit_status"] == "failed"
        assert ctx2["error_message"] == "boom"
        assert ctx2["rank"] == 1

    async def test_last_failed_picks_highest_id(self):
        items = [
            _fake_item(id=1, status="failed", error_message="first"),
            _fake_item(id=9, status="failed", error_message="latest"),
        ]
        assert _derive_printer_unit_context(1, items)["error_message"] == "latest"

    async def test_no_unit_for_printer(self):
        # Only another printer's unit present → this printer holds nothing.
        ctx = _derive_printer_unit_context(1, [_fake_item(id=1, printer_id=99, status="printing")])
        assert ctx == {
            "unit_id": None,
            "unit_status": None,
            "waiting_reason": None,
            "error_message": None,
            "staged": False,
            "filament_short": False,
            "first_article": False,
            "rank": 0,
        }


class TestBuildFarmPrinterContexts:
    async def test_only_active_paused_farm_runs_and_live_unit_wins(self, db_session):
        # Older run: printer P holds a live printing unit. Newer run: same printer
        # only has a completed unit. Live unit must win the printer.
        older = await _mk_run(db_session, quantity=1)
        p = await _mk_printer(db_session, name="Shared")
        await _add(db_session, older, printer_id=p.id, status="printing", pos=1)
        newer = await _mk_run(db_session, quantity=1)
        await _add(db_session, newer, printer_id=p.id, status="completed", pos=1)
        # A completed run must be excluded entirely.
        done = await _mk_run(db_session, quantity=1, status="completed")
        p2 = await _mk_printer(db_session, name="Done")
        await _add(db_session, done, printer_id=p2.id, status="printing", pos=1)
        await db_session.commit()

        contexts = await build_farm_printer_contexts(db_session)
        by_pid = {c["printer_id"]: c for c in contexts}
        assert p.id in by_pid
        assert by_pid[p.id]["run_id"] == older.id  # live unit wins over the newer run
        assert by_pid[p.id]["unit_status"] == "printing"
        assert p2.id not in by_pid  # completed run excluded


async def _mk_archive(db, photos):
    arch = PrintArchive(filename="fa.gcode.3mf", file_path="/tmp/fa.gcode.3mf", file_size=1, photos=photos)
    db.add(arch)
    await db.flush()
    return arch


class TestFirstArticlePhoto:
    """build_run_response first-article inspection payload (Phase 4, F1).

    Only ``awaiting_approval`` / ``rejected`` runs carry the finish-photo URL +
    the producing printer; the newest ``finish_*`` archive photo is selected,
    mirroring main.py (the capture path appends the fresh finish photo LAST).
    """

    async def test_awaiting_approval_picks_finish_photo_and_printer(self, db_session):
        batch = await _mk_run(db_session, quantity=2, fa_state="awaiting_approval")
        p = await _mk_printer(db_session)
        arch = await _mk_archive(db_session, ["foo.jpg", "finish_1.jpg"])
        await _add(db_session, batch, printer_id=p.id, status="completed", first_article=True, archive_id=arch.id)
        await db_session.commit()
        run = await _load_run(db_session, batch.id)

        resp = await build_run_response(db_session, run)
        assert resp["first_article_photo_url"] == f"/api/v1/archives/{arch.id}/photos/finish_1.jpg"
        assert resp["first_article_photo_url"].endswith("/photos/finish_1.jpg")
        assert resp["first_article_printer_id"] == p.id
        assert resp["first_article_printer_name"] == p.name

    async def test_multiple_finish_photos_pick_newest(self, db_session):
        batch = await _mk_run(db_session, quantity=2, fa_state="awaiting_approval")
        p = await _mk_printer(db_session)
        arch = await _mk_archive(
            db_session,
            [
                "thumb.png",
                "finish_20260706_100000_aaaa.jpg",
                "midprint.jpg",
                "finish_20260706_120000_bbbb.jpg",
            ],
        )
        await _add(db_session, batch, printer_id=p.id, status="completed", first_article=True, archive_id=arch.id)
        await db_session.commit()
        run = await _load_run(db_session, batch.id)

        resp = await build_run_response(db_session, run)
        # The last finish_* entry (newest captured) wins.
        assert resp["first_article_photo_url"].endswith("/photos/finish_20260706_120000_bbbb.jpg")

    async def test_no_finish_photo_yields_null_url_but_keeps_printer(self, db_session):
        batch = await _mk_run(db_session, quantity=2, fa_state="awaiting_approval")
        p = await _mk_printer(db_session)
        arch = await _mk_archive(db_session, ["foo.jpg", "thumb.png"])
        await _add(db_session, batch, printer_id=p.id, status="completed", first_article=True, archive_id=arch.id)
        await db_session.commit()
        run = await _load_run(db_session, batch.id)

        resp = await build_run_response(db_session, run)
        assert resp["first_article_photo_url"] is None
        assert resp["first_article_printer_id"] == p.id
        assert resp["first_article_printer_name"] == p.name

    async def test_rejected_state_populates_fields(self, db_session):
        batch = await _mk_run(db_session, quantity=2, fa_state="rejected")
        p = await _mk_printer(db_session)
        arch = await _mk_archive(db_session, ["finish_x.jpg"])
        await _add(db_session, batch, printer_id=p.id, status="completed", first_article=True, archive_id=arch.id)
        await db_session.commit()
        run = await _load_run(db_session, batch.id)

        resp = await build_run_response(db_session, run)
        assert resp["first_article_photo_url"] == f"/api/v1/archives/{arch.id}/photos/finish_x.jpg"
        assert resp["first_article_printer_id"] == p.id
        assert resp["first_article_printer_name"] == p.name

    async def test_approved_state_yields_all_null(self, db_session):
        # A completed FA item with a finish photo exists, but once approved the
        # inspection payload is suppressed (nothing left to approve).
        batch = await _mk_run(db_session, quantity=2, fa_state="approved")
        p = await _mk_printer(db_session)
        arch = await _mk_archive(db_session, ["finish_x.jpg"])
        await _add(db_session, batch, printer_id=p.id, status="completed", first_article=True, archive_id=arch.id)
        await db_session.commit()
        run = await _load_run(db_session, batch.id)

        resp = await build_run_response(db_session, run)
        assert resp["first_article_photo_url"] is None
        assert resp["first_article_printer_id"] is None
        assert resp["first_article_printer_name"] is None

    async def test_no_fa_gate_yields_all_null(self, db_session):
        batch = await _mk_run(db_session, quantity=2)  # first_article_state None
        p = await _mk_printer(db_session)
        await _add(db_session, batch, printer_id=p.id, status="completed")
        await db_session.commit()
        run = await _load_run(db_session, batch.id)

        resp = await build_run_response(db_session, run)
        assert resp["first_article_photo_url"] is None
        assert resp["first_article_printer_id"] is None
        assert resp["first_article_printer_name"] is None


@pytest.fixture
def elig_env(monkeypatch):
    """Patch the three eligibility collaborators to an "all-eligible" baseline.

    Every dimension defaults to OK (empty deficit, capable, no live status) so a
    test can raise ONE flag by reconfiguring its mock. ``status_map`` feeds the
    real ``printer_manager.get_status`` used by both ``_build_printer_states`` and
    the eligibility helper.
    """
    deficit_mock = AsyncMock(return_value=[])
    cap_mock = MagicMock(return_value=CapabilityDecision(ok=True))
    status_map: dict[int, object] = {}
    monkeypatch.setattr(production_run, "compute_deficit_for_queue_item", deficit_mock)
    monkeypatch.setattr(production_run, "evaluate_capability", cap_mock)
    monkeypatch.setattr(production_run, "list_geometries", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        _scheduler, "_compute_ams_mapping_for_printer", AsyncMock(return_value=MatchOutcome(mapping=None))
    )
    monkeypatch.setattr(printer_manager, "get_status", lambda pid: status_map.get(pid))
    return SimpleNamespace(deficit=deficit_mock, capability=cap_mock, status_map=status_map)


class TestPrinterEligibility:
    """Per-printer live dispatch-eligibility merged onto the run-detail printer
    states: filament deficit, USB presence and the capability gate. Detail-only;
    each per-printer computation is fail-safe (defaults on error)."""

    async def test_deficit_sets_filament_short_live_and_detail(self, db_session, elig_env):
        elig_env.deficit.return_value = [
            FilamentDeficit(
                slot_id=1, ams_id=0, tray_id=1, filament_type="PETG", required_grams=455.1, remaining_grams=260.0
            )
        ]
        batch = await _mk_run(db_session, quantity=1)
        p = await _mk_printer(db_session)
        await _add(db_session, batch, printer_id=p.id, status="pending")
        await db_session.commit()
        run = await _load_run(db_session, batch.id)

        resp = await build_run_response(db_session, run, detail=True)
        st = resp["printer_states"][0]
        assert st["filament_short_live"] is True
        assert st["filament_short_detail"] == "needs 455 g, 260 g available"
        # A single new flag alone raises the run-level blocked summary.
        assert resp["has_blocked_printers"] is True

    async def test_sdcard_false_sets_no_usb_drive(self, db_session, elig_env):
        batch = await _mk_run(db_session, quantity=1)
        p = await _mk_printer(db_session)
        elig_env.status_map[p.id] = SimpleNamespace(sdcard=False)
        await _add(db_session, batch, printer_id=p.id, status="pending")
        await db_session.commit()
        run = await _load_run(db_session, batch.id)

        resp = await build_run_response(db_session, run, detail=True)
        st = resp["printer_states"][0]
        assert st["no_usb_drive"] is True
        assert resp["has_blocked_printers"] is True

    async def test_unvalidated_geometry_sets_capability_reason(self, db_session, elig_env, monkeypatch):
        # Use the REAL capability gate with an empty validated-model set (no
        # seeded geometry) → every printer blocks on missing eject geometry.
        monkeypatch.setattr(production_run, "evaluate_capability", _real_evaluate_capability)
        batch = await _mk_run(db_session, quantity=1)
        p = await _mk_printer(db_session)  # model "H2S"
        await _add(db_session, batch, printer_id=p.id, status="pending")
        await db_session.commit()
        run = await _load_run(db_session, batch.id)

        resp = await build_run_response(db_session, run, detail=True)
        st = resp["printer_states"][0]
        assert st["capability_reason"] is not None
        assert "eject bed geometry" in st["capability_reason"]
        assert resp["has_blocked_printers"] is True

    async def test_nozzle_mismatch_sets_capability_reason(self, db_session, elig_env, monkeypatch):
        # Real gate, H2S geometry VALIDATED (geometry passes), but the file needs a
        # 0.6 nozzle and the printer reports 0.4 → the nozzle arm blocks. Pins the
        # eligibility wiring (plate-scoped file caps + live-nozzle reader) end to end.
        monkeypatch.setattr(production_run, "evaluate_capability", _real_evaluate_capability)
        monkeypatch.setattr(
            production_run,
            "list_geometries",
            AsyncMock(return_value=[SimpleNamespace(model_key="H2S", validated=True)]),
        )
        batch = await _mk_run(db_session, quantity=1)
        p = await _mk_printer(db_session)  # model "H2S"
        lib = LibraryFile(
            filename="n.gcode.3mf",
            file_path="/tmp/n.gcode.3mf",
            file_type="gcode.3mf",
            file_size=1,
            file_metadata={"nozzle_diameter": 0.6},
        )
        db_session.add(lib)
        await db_session.flush()
        elig_env.status_map[p.id] = SimpleNamespace(nozzles=[SimpleNamespace(nozzle_diameter="0.4")], raw_data={})
        await _add(db_session, batch, printer_id=p.id, status="pending", library_file_id=lib.id)
        await db_session.commit()
        run = await _load_run(db_session, batch.id)

        resp = await build_run_response(db_session, run, detail=True)
        st = resp["printer_states"][0]
        assert st["capability_reason"] is not None
        assert "nozzle mismatch" in st["capability_reason"]
        assert resp["has_blocked_printers"] is True

    async def test_fully_eligible_printer_all_defaults(self, db_session, elig_env):
        batch = await _mk_run(db_session, quantity=1)
        p = await _mk_printer(db_session)
        await _add(db_session, batch, printer_id=p.id, status="pending")
        await db_session.commit()
        run = await _load_run(db_session, batch.id)

        resp = await build_run_response(db_session, run, detail=True)
        st = resp["printer_states"][0]
        assert st["filament_short_live"] is False
        assert st["filament_short_detail"] is None
        assert st["no_usb_drive"] is False
        assert st["capability_reason"] is None
        assert resp["has_blocked_printers"] is False

    async def test_no_pending_units_defaults_and_skips_deficit(self, db_session, elig_env):
        batch = await _mk_run(db_session, quantity=1)
        p = await _mk_printer(db_session)
        await _add(db_session, batch, printer_id=p.id, status="completed", completed_at=datetime.now(timezone.utc))
        await db_session.commit()
        run = await _load_run(db_session, batch.id)

        resp = await build_run_response(db_session, run, detail=True)
        st = resp["printer_states"][0]
        assert st["filament_short_live"] is False
        assert st["no_usb_drive"] is False
        assert st["capability_reason"] is None
        # No pending work → the deficit path is never entered.
        assert elig_env.deficit.await_count == 0

    async def test_model_targeted_run_lists_active_model_printers(self, db_session, elig_env):
        # A model-targeted run whose staged items are UNPINNED (printer_id NULL):
        # the panel must list every ACTIVE printer of the target model.
        batch = await _mk_run(db_session, quantity=1)
        h2s_a = await _mk_printer(db_session, name="H2S-A")
        h2s_b = await _mk_printer(db_session, name="H2S-B")
        # An inactive H2S and an active non-H2S must NOT appear.
        inactive = Printer(
            name="H2S-off",
            serial_number="SN-off",
            ip_address="192.0.2.9",
            access_code="1",
            model="H2S",
            is_active=False,
        )
        other = Printer(
            name="P1S-A", serial_number="SN-p1s", ip_address="192.0.2.8", access_code="1", model="P1S", is_active=True
        )
        db_session.add_all([inactive, other])
        await db_session.flush()
        await _add(db_session, batch, printer_id=None, target_model="H2S", status="pending")
        await db_session.commit()
        run = await _load_run(db_session, batch.id)

        resp = await build_run_response(db_session, run, detail=True)
        ids = {s["printer_id"] for s in resp["printer_states"]}
        assert ids == {h2s_a.id, h2s_b.id}

    async def test_per_printer_exception_defaults_and_no_raise(self, db_session, elig_env):
        elig_env.deficit.side_effect = RuntimeError("malformed 3MF")
        batch = await _mk_run(db_session, quantity=1)
        p = await _mk_printer(db_session)
        await _add(db_session, batch, printer_id=p.id, status="pending")
        await db_session.commit()
        run = await _load_run(db_session, batch.id)

        # Must not propagate; the deficit flag stays at its default.
        resp = await build_run_response(db_session, run, detail=True)
        st = resp["printer_states"][0]
        assert st["filament_short_live"] is False
        assert st["filament_short_detail"] is None

    async def test_busy_printer_not_flagged(self, db_session, elig_env):
        # A printing (busy) printer is not ineligibility: with filament/USB/
        # capability all OK it carries no eligibility flag.
        batch = await _mk_run(db_session, quantity=2)
        busy = await _mk_printer(db_session, name="Busy")
        await _add(db_session, batch, printer_id=busy.id, status="printing", pos=1)
        await _add(db_session, batch, printer_id=busy.id, status="pending", pos=2)
        await db_session.commit()
        run = await _load_run(db_session, batch.id)

        resp = await build_run_response(db_session, run, detail=True)
        st = next(s for s in resp["printer_states"] if s["printer_id"] == busy.id)
        assert st["filament_short_live"] is False
        assert st["no_usb_drive"] is False
        assert st["capability_reason"] is None

    async def test_list_endpoint_skips_eligibility(self, db_session, elig_env):
        # detail=False must not compute eligibility (lean list path).
        elig_env.deficit.return_value = [
            FilamentDeficit(
                slot_id=1, ams_id=0, tray_id=1, filament_type="PETG", required_grams=455.0, remaining_grams=10.0
            )
        ]
        batch = await _mk_run(db_session, quantity=1)
        p = await _mk_printer(db_session)
        await _add(db_session, batch, printer_id=p.id, status="pending")
        await db_session.commit()
        run = await _load_run(db_session, batch.id)

        lean = await build_run_response(db_session, run)  # detail defaults False
        assert "printer_states" not in lean
        assert elig_env.deficit.await_count == 0


class TestRunAgainPrefillFields:
    """build_run_response surfaces the "Run again" prefill fields (Phase 5, F9):
    eject_profile_id + target_model derived from the items (first non-null,
    uniform per run) and cooldown_temp_c_override from the batch column. Present
    on BOTH the list and detail shapes so a terminal run card can reopen the
    dialog pre-filled. FK enforcement is off in the test engine, so a bare
    eject_profile_id needs no real profile row."""

    async def test_specific_printer_run_derives_eject_and_cooldown_no_model(self, db_session):
        batch = await _mk_run(db_session, quantity=2)
        batch.cooldown_temp_c_override = 34.5
        p = await _mk_printer(db_session)
        await _add(db_session, batch, printer_id=p.id, status="pending", eject_profile_id=7, pos=1)
        await _add(db_session, batch, printer_id=p.id, status="pending", eject_profile_id=7, pos=2)
        await db_session.commit()
        run = await _load_run(db_session, batch.id)

        resp = await build_run_response(db_session, run)
        assert resp["eject_profile_id"] == 7
        assert resp["cooldown_temp_c_override"] == 34.5
        assert resp["target_model"] is None  # specific-printer run

    async def test_model_run_derives_target_model(self, db_session):
        batch = await _mk_run(db_session, quantity=1)
        await _add(db_session, batch, status="pending", target_model="H2C", eject_profile_id=3)
        await db_session.commit()
        run = await _load_run(db_session, batch.id)

        resp = await build_run_response(db_session, run)
        assert resp["target_model"] == "H2C"
        assert resp["eject_profile_id"] == 3

    async def test_prefill_fields_null_when_absent(self, db_session):
        batch = await _mk_run(db_session, quantity=1)  # no cooldown override
        await _add(db_session, batch, status="pending")  # no eject profile / target_model
        await db_session.commit()
        run = await _load_run(db_session, batch.id)

        resp = await build_run_response(db_session, run)
        assert resp["eject_profile_id"] is None
        assert resp["cooldown_temp_c_override"] is None
        assert resp["target_model"] is None
