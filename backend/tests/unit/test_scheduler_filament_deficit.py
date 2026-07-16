"""Scheduler pre-dispatch filament-deficit guard tests (#1496).

``PrintScheduler._block_on_filament_deficit`` is the gate that keeps an
auto_dispatch=True VP intake (or any other scheduler-driven dispatch) from
sending a print onto a spool that can't satisfy it. On a deficit it
promotes the item to manual_start; when a previously-flagged item's spool
is now adequate it clears the flag so the next tick dispatches.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

import backend.app.services.print_scheduler as sched_mod
from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services.filament_deficit import FilamentDeficit, compute_deficit_for_queue_item
from backend.app.services.print_scheduler import PrintScheduler
from backend.app.services.spool_selection import WAITING_REASON_START_MIN, MatchOutcome


def _deficit(printer_id_override=None, ams_mapping_override=None, slots=1):
    """Build a throwaway deficit list (contents don't matter to the scheduler)."""
    return [
        FilamentDeficit(
            slot_id=i + 1,
            ams_id=0,
            tray_id=0,
            filament_type="PETG",
            required_grams=455.0,
            remaining_grams=200.0,
        )
        for i in range(slots)
    ]


@pytest.fixture
def scheduler():
    """A fresh scheduler instance — internal state is not exercised."""
    return PrintScheduler()


@pytest.fixture
def queue_item(db_session, printer_factory):
    """Helper to drop a queue item the helper can mutate."""

    async def _make(**overrides):
        printer = await printer_factory()
        defaults = {
            "printer_id": printer.id,
            "status": "pending",
            "manual_start": False,
            "filament_short": False,
        }
        defaults.update(overrides)
        item = PrintQueueItem(**defaults)
        db_session.add(item)
        await db_session.commit()
        await db_session.refresh(item)
        return item

    return _make


@pytest.mark.asyncio
async def test_blocks_on_deficit_promotes_to_manual_start(scheduler, db_session, queue_item):
    item = await queue_item()
    with patch(
        "backend.app.services.print_scheduler.compute_deficit_for_queue_item",
        AsyncMock(
            return_value=[
                FilamentDeficit(
                    slot_id=1,
                    ams_id=0,
                    tray_id=0,
                    filament_type="PLA",
                    required_grams=270.0,
                    remaining_grams=200.0,
                ),
            ]
        ),
    ):
        blocked = await scheduler._block_on_filament_deficit(db_session, item)

    assert blocked is True
    await db_session.refresh(item)
    assert item.manual_start is True
    assert item.filament_short is True


@pytest.mark.asyncio
async def test_clears_stale_flag_when_deficit_resolves(scheduler, db_session, queue_item):
    """Previously-flagged item whose spool was swapped is unblocked."""
    item = await queue_item(filament_short=True, manual_start=False)
    with patch(
        "backend.app.services.print_scheduler.compute_deficit_for_queue_item",
        AsyncMock(return_value=[]),
    ):
        blocked = await scheduler._block_on_filament_deficit(db_session, item)

    assert blocked is False
    await db_session.refresh(item)
    assert item.filament_short is False
    assert item.manual_start is False


@pytest.mark.asyncio
async def test_no_deficit_no_op(scheduler, db_session, queue_item):
    """Happy path — no deficit, no flag changes, dispatch proceeds."""
    item = await queue_item()
    with patch(
        "backend.app.services.print_scheduler.compute_deficit_for_queue_item",
        AsyncMock(return_value=[]),
    ):
        blocked = await scheduler._block_on_filament_deficit(db_session, item)

    assert blocked is False
    await db_session.refresh(item)
    assert item.filament_short is False
    assert item.manual_start is False


@pytest.mark.asyncio
async def test_helper_exception_does_not_wedge_dispatch(scheduler, db_session, queue_item):
    """A flaky deficit check (e.g. Spoolman timeout) must not block dispatch."""
    item = await queue_item()
    with patch(
        "backend.app.services.print_scheduler.compute_deficit_for_queue_item",
        AsyncMock(side_effect=RuntimeError("network down")),
    ):
        blocked = await scheduler._block_on_filament_deficit(db_session, item)

    assert blocked is False
    await db_session.refresh(item)
    assert item.filament_short is False


@pytest.mark.asyncio
async def test_skip_filament_check_short_circuits_without_compute(scheduler, db_session, queue_item):
    """User clicked Print Anyway (skip_filament_check=True): no compute, no flag (#1698-followup).

    Pre-fix the scheduler re-ran the deficit check on every tick, re-set
    manual_start/filament_short to True, and the item bounced between
    "user said anyway" (route clears flags) and "scheduler re-blocked"
    forever. With the persistent acknowledgement flag the scheduler bails
    early without even touching the deficit helper.
    """
    item = await queue_item(skip_filament_check=True)
    compute_mock = AsyncMock(
        return_value=[
            FilamentDeficit(
                slot_id=1,
                ams_id=0,
                tray_id=0,
                filament_type="PLA",
                required_grams=270.0,
                remaining_grams=200.0,
            ),
        ]
    )
    with patch(
        "backend.app.services.print_scheduler.compute_deficit_for_queue_item",
        compute_mock,
    ):
        blocked = await scheduler._block_on_filament_deficit(db_session, item)

    assert blocked is False
    compute_mock.assert_not_awaited()
    await db_session.refresh(item)
    # Flags must not get re-set by the scheduler now that the user has
    # acknowledged the deficit.
    assert item.filament_short is False
    assert item.manual_start is False


# ---------------------------------------------------------------------------
# Model-based candidate loop (head-of-line fix, 2026-07-12)
#
# The model-based branch of ``check_queue`` now walks candidate printers one at
# a time, excluding any candidate found short on filament THIS tick, so a short
# low-id printer no longer swallows the whole run onto itself. These drive the
# real ``check_queue`` with its printer-facing collaborators pinned.
# ---------------------------------------------------------------------------


@pytest.fixture
def cq_scheduler(monkeypatch, test_engine):
    """A PrintScheduler whose environment is pinned for a model-based dispatch tick."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(sched_mod, "async_session", maker)

    s = PrintScheduler()
    monkeypatch.setattr(s, "_stagger_budget", AsyncMock(return_value=99))
    monkeypatch.setattr(s, "_check_auto_drying", AsyncMock())
    monkeypatch.setattr(s, "_get_job_name", AsyncMock(return_value="job"))
    monkeypatch.setattr(s, "_compute_ams_mapping_for_printer", AsyncMock(return_value=MatchOutcome(mapping=[0])))
    monkeypatch.setattr(sched_mod.notification_service, "on_queue_job_assigned", AsyncMock())
    monkeypatch.setattr(sched_mod.notification_service, "on_queue_job_waiting", AsyncMock())
    return s


async def _model_item(db_session, *, batch_id=None, target_model="H2S", pos=1) -> PrintQueueItem:
    item = PrintQueueItem(
        batch_id=batch_id,
        printer_id=None,
        target_model=target_model,
        status="pending",
        manual_start=False,
        filament_short=False,
        position=pos,
    )
    db_session.add(item)
    await db_session.commit()
    await db_session.refresh(item)
    return item


@pytest.mark.asyncio
async def test_head_of_line_dispatches_to_second_printer_same_tick(
    cq_scheduler, db_session, printer_factory, monkeypatch
):
    """Printer A short + B fine → the item dispatches to B in the SAME tick."""
    a = await printer_factory(model="H2S")
    b = await printer_factory(model="H2S")

    def _find(db, model, exclude, *a_, **k_):
        if a.id not in exclude:
            return (a.id, None)
        if b.id not in exclude:
            return (b.id, None)
        return (None, "all printers busy")

    monkeypatch.setattr(cq_scheduler, "_find_idle_printer_for_model", AsyncMock(side_effect=_find))

    async def _deficit_for(db, item, *, printer_id_override=None, ams_mapping_override=None):
        return _deficit() if printer_id_override == a.id else []

    monkeypatch.setattr(cq_scheduler, "_compute_deficit_safe", AsyncMock(side_effect=_deficit_for))

    started: dict = {}

    async def _start(db, item):
        item.status = "printing"
        started["printer_id"] = item.printer_id
        await db.commit()

    monkeypatch.setattr(cq_scheduler, "_start_print", AsyncMock(side_effect=_start))

    item = await _model_item(db_session)
    item_id, b_id = item.id, b.id
    await cq_scheduler.check_queue()

    # Dispatched — to B (the printer with adequate filament), not the short A.
    assert started.get("printer_id") == b_id
    db_session.expire_all()
    row = (
        await db_session.execute(
            select(PrintQueueItem.printer_id, PrintQueueItem.manual_start).where(PrintQueueItem.id == item_id)
        )
    ).one()
    assert row.printer_id == b_id
    assert row.manual_start is False  # never staged


@pytest.mark.asyncio
async def test_short_printer_excluded_from_later_candidate_search(
    cq_scheduler, db_session, printer_factory, monkeypatch
):
    """Once A is found short, the next candidate search excludes it (tick-local)."""
    a = await printer_factory(model="H2S")
    b = await printer_factory(model="H2S")

    excludes: list[set] = []

    def _find(db, model, exclude, *a_, **k_):
        excludes.append(set(exclude))
        if a.id not in exclude:
            return (a.id, None)
        if b.id not in exclude:
            return (b.id, None)
        return (None, "all printers busy")

    monkeypatch.setattr(cq_scheduler, "_find_idle_printer_for_model", AsyncMock(side_effect=_find))

    async def _deficit_for(db, item, *, printer_id_override=None, ams_mapping_override=None):
        return _deficit() if printer_id_override == a.id else []

    monkeypatch.setattr(cq_scheduler, "_compute_deficit_safe", AsyncMock(side_effect=_deficit_for))
    monkeypatch.setattr(cq_scheduler, "_start_print", AsyncMock(side_effect=_mk_start()))

    await _model_item(db_session)
    await cq_scheduler.check_queue()

    # First search saw neither excluded; the SECOND search excluded the short A.
    assert len(excludes) >= 2
    assert a.id not in excludes[0]
    assert a.id in excludes[1]


@pytest.mark.asyncio
async def test_all_candidates_short_stages_unpinned_with_one_notification(
    cq_scheduler, db_session, printer_factory, monkeypatch
):
    """Every eligible printer short → item staged UNPINNED + exactly one notification."""
    a = await printer_factory(model="H2S")
    b = await printer_factory(model="H2S")

    def _find(db, model, exclude, *a_, **k_):
        if a.id not in exclude:
            return (a.id, None)
        if b.id not in exclude:
            return (b.id, None)
        return (None, "No idle H2S printers")

    monkeypatch.setattr(cq_scheduler, "_find_idle_printer_for_model", AsyncMock(side_effect=_find))
    # Every candidate is short.
    monkeypatch.setattr(cq_scheduler, "_compute_deficit_safe", AsyncMock(return_value=_deficit()))
    start_mock = AsyncMock()
    monkeypatch.setattr(cq_scheduler, "_start_print", start_mock)

    item = await _model_item(db_session, batch_id=None)
    item_id = item.id
    await cq_scheduler.check_queue()

    start_mock.assert_not_awaited()  # nothing dispatched
    db_session.expire_all()
    row = (
        await db_session.execute(
            select(
                PrintQueueItem.printer_id,
                PrintQueueItem.ams_mapping,
                PrintQueueItem.manual_start,
                PrintQueueItem.filament_short,
                PrintQueueItem.waiting_reason,
            ).where(PrintQueueItem.id == item_id)
        )
    ).one()
    assert row.printer_id is None  # UNPINNED
    assert row.ams_mapping is None
    assert row.manual_start is True
    assert row.filament_short is True
    assert row.waiting_reason == "filament_short"
    # Exactly one waiting notification for the group.
    assert sched_mod.notification_service.on_queue_job_waiting.await_count == 1


@pytest.mark.asyncio
async def test_stage_model_item_notifies_once_per_group(cq_scheduler, db_session, printer_factory):
    """The staging primitive dedups notifications by (batch_id, target_model) per tick."""
    item1 = await _model_item(db_session, batch_id=7, target_model="H2S", pos=1)
    item2 = await _model_item(db_session, batch_id=7, target_model="H2S", pos=2)
    groups: set = set()

    await cq_scheduler._stage_model_item_filament_short(db_session, item1, groups)
    await cq_scheduler._stage_model_item_filament_short(db_session, item2, groups)

    await db_session.refresh(item1)
    await db_session.refresh(item2)
    # Both staged unpinned...
    for it in (item1, item2):
        assert it.printer_id is None
        assert it.manual_start is True
        assert it.filament_short is True
        assert it.waiting_reason == "filament_short"
    # ...but only ONE notification for the shared group.
    assert sched_mod.notification_service.on_queue_job_waiting.await_count == 1


# ---------------------------------------------------------------------------
# Minimum-start-weight floor: start-blocked candidates (spool-selection WI)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pinned_start_block_stages_with_reason(cq_scheduler, db_session, printer_factory, monkeypatch):
    """A pinned item whose only matching spool is below the start floor is staged
    with the distinct reason and NO mapping persisted (the spool stays as a
    backup donor)."""
    printer = await printer_factory(model="H2S")
    item = PrintQueueItem(
        printer_id=printer.id,
        status="pending",
        manual_start=False,
        filament_short=False,
        position=1,
        plate_id=1,
    )
    db_session.add(item)
    await db_session.commit()
    await db_session.refresh(item)
    item_id = item.id

    monkeypatch.setattr(
        cq_scheduler,
        "_compute_ams_mapping_for_printer",
        AsyncMock(return_value=MatchOutcome(mapping=None, start_blocked_slots=[1])),
    )
    monkeypatch.setattr(cq_scheduler, "_is_printer_idle", lambda pid: True)
    monkeypatch.setattr(cq_scheduler, "_get_printer", AsyncMock(return_value=printer))
    monkeypatch.setattr(sched_mod.printer_manager, "is_connected", lambda pid: True)
    start_mock = AsyncMock()
    monkeypatch.setattr(cq_scheduler, "_start_print", start_mock)

    await cq_scheduler.check_queue()

    start_mock.assert_not_awaited()  # never dispatched
    db_session.expire_all()
    row = (
        await db_session.execute(
            select(
                PrintQueueItem.ams_mapping,
                PrintQueueItem.manual_start,
                PrintQueueItem.filament_short,
                PrintQueueItem.waiting_reason,
            ).where(PrintQueueItem.id == item_id)
        )
    ).one()
    assert row.ams_mapping is None  # no mapping persisted
    assert row.manual_start is True
    assert row.filament_short is True
    assert row.waiting_reason == WAITING_REASON_START_MIN
    assert sched_mod.notification_service.on_queue_job_waiting.await_count == 1


@pytest.mark.asyncio
async def test_model_loop_skips_start_blocked_candidate(cq_scheduler, db_session, printer_factory, monkeypatch):
    """A start-blocked candidate is skipped like a deficit — the item dispatches
    to the next printer whose starting spool clears the floor."""
    a = await printer_factory(model="H2S")
    b = await printer_factory(model="H2S")

    def _find(db, model, exclude, *a_, **k_):
        if a.id not in exclude:
            return (a.id, None)
        if b.id not in exclude:
            return (b.id, None)
        return (None, "all printers busy")

    monkeypatch.setattr(cq_scheduler, "_find_idle_printer_for_model", AsyncMock(side_effect=_find))

    async def _outcome(db, printer_id, item):
        if printer_id == a.id:
            return MatchOutcome(mapping=None, start_blocked_slots=[1])  # A below floor
        return MatchOutcome(mapping=[0])

    monkeypatch.setattr(cq_scheduler, "_compute_ams_mapping_for_printer", AsyncMock(side_effect=_outcome))
    monkeypatch.setattr(cq_scheduler, "_compute_deficit_safe", AsyncMock(return_value=[]))

    started: dict = {}

    async def _start(db, item):
        item.status = "printing"
        started["printer_id"] = item.printer_id
        await db.commit()

    monkeypatch.setattr(cq_scheduler, "_start_print", AsyncMock(side_effect=_start))

    await _model_item(db_session)
    await cq_scheduler.check_queue()

    assert started.get("printer_id") == b.id  # skipped A, ran on B


@pytest.mark.asyncio
async def test_all_candidates_start_blocked_stages_with_start_min_reason(
    cq_scheduler, db_session, printer_factory, monkeypatch
):
    """Every candidate blocked PURELY by the start floor → staged UNPINNED with
    the distinct start-min reason (not the generic filament_short)."""
    a = await printer_factory(model="H2S")
    b = await printer_factory(model="H2S")

    def _find(db, model, exclude, *a_, **k_):
        if a.id not in exclude:
            return (a.id, None)
        if b.id not in exclude:
            return (b.id, None)
        return (None, "No idle H2S printers")

    monkeypatch.setattr(cq_scheduler, "_find_idle_printer_for_model", AsyncMock(side_effect=_find))
    monkeypatch.setattr(
        cq_scheduler,
        "_compute_ams_mapping_for_printer",
        AsyncMock(return_value=MatchOutcome(mapping=None, start_blocked_slots=[1])),
    )
    monkeypatch.setattr(cq_scheduler, "_compute_deficit_safe", AsyncMock(return_value=[]))  # no true deficit
    start_mock = AsyncMock()
    monkeypatch.setattr(cq_scheduler, "_start_print", start_mock)

    item = await _model_item(db_session)
    item_id = item.id
    await cq_scheduler.check_queue()

    start_mock.assert_not_awaited()
    db_session.expire_all()
    row = (
        await db_session.execute(
            select(
                PrintQueueItem.printer_id,
                PrintQueueItem.manual_start,
                PrintQueueItem.waiting_reason,
            ).where(PrintQueueItem.id == item_id)
        )
    ).one()
    assert row.printer_id is None  # UNPINNED
    assert row.manual_start is True
    assert row.waiting_reason == WAITING_REASON_START_MIN


@pytest.mark.asyncio
async def test_mixed_block_stages_generic_filament_short(cq_scheduler, db_session, printer_factory, monkeypatch):
    """A mix of a true deficit AND a start-floor block stages with the GENERIC
    filament_short reason (the deficit is the more urgent story)."""
    a = await printer_factory(model="H2S")
    b = await printer_factory(model="H2S")

    def _find(db, model, exclude, *a_, **k_):
        if a.id not in exclude:
            return (a.id, None)
        if b.id not in exclude:
            return (b.id, None)
        return (None, "No idle H2S printers")

    monkeypatch.setattr(cq_scheduler, "_find_idle_printer_for_model", AsyncMock(side_effect=_find))

    async def _outcome(db, printer_id, item):
        # A is start-blocked; B has a mapping (its block will be a true deficit).
        if printer_id == a.id:
            return MatchOutcome(mapping=None, start_blocked_slots=[1])
        return MatchOutcome(mapping=[0])

    async def _deficit_for(db, item, *, printer_id_override=None, ams_mapping_override=None):
        return _deficit() if printer_id_override == b.id else []

    monkeypatch.setattr(cq_scheduler, "_compute_ams_mapping_for_printer", AsyncMock(side_effect=_outcome))
    monkeypatch.setattr(cq_scheduler, "_compute_deficit_safe", AsyncMock(side_effect=_deficit_for))
    start_mock = AsyncMock()
    monkeypatch.setattr(cq_scheduler, "_start_print", start_mock)

    item = await _model_item(db_session)
    item_id = item.id
    await cq_scheduler.check_queue()

    start_mock.assert_not_awaited()
    db_session.expire_all()
    row = (
        await db_session.execute(
            select(PrintQueueItem.printer_id, PrintQueueItem.waiting_reason).where(PrintQueueItem.id == item_id)
        )
    ).one()
    assert row.printer_id is None
    assert row.waiting_reason == "filament_short"  # mixed → generic


def _mk_start():
    async def _start(db, item):
        item.status = "printing"
        await db.commit()

    return _start


# ---------------------------------------------------------------------------
# compute_deficit_for_queue_item override params (candidate-aware, item unmutated)
# ---------------------------------------------------------------------------


def _write_3mf(file_path: Path, filaments: list[dict]) -> None:
    body = "".join(
        f'<filament id="{f["id"]}" type="{f["type"]}" color="{f["color"]}" used_g="{f["used_g"]}"/>' for f in filaments
    )
    config = f'<?xml version="1.0" encoding="utf-8"?><config>{body}</config>'
    with zipfile.ZipFile(file_path, "w") as zf:
        zf.writestr("Metadata/slice_info.config", config)


@pytest.mark.asyncio
async def test_override_params_check_candidate_printer(db_session, printer_factory, tmp_path):
    """``printer_id_override`` / ``ams_mapping_override`` deficit-check a candidate
    the item is NOT pinned to, without mutating the item — and existing
    no-override callers are unaffected."""
    candidate = await printer_factory(model="H2S")
    file_path = tmp_path / "m.3mf"
    _write_3mf(file_path, [{"id": "1", "type": "PLA", "color": "#FFFFFF", "used_g": "100.0"}])
    archive = PrintArchive(
        filename="m.3mf",
        print_name="T",
        file_path=str(file_path),
        file_size=file_path.stat().st_size,
        status="completed",
    )
    db_session.add(archive)
    await db_session.commit()
    await db_session.refresh(archive)

    # Short spool assigned to the CANDIDATE printer (30 g for a 100 g print).
    spool = Spool(material="PLA", label_weight=1000, weight_used=970.0, rgba="#FFFFFF")
    db_session.add(spool)
    await db_session.commit()
    await db_session.refresh(spool)
    db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=candidate.id, ams_id=0, tray_id=0))
    await db_session.commit()

    # Item is UNPINNED (model-based) with no mapping of its own.
    item = PrintQueueItem(
        printer_id=None, archive_id=archive.id, ams_mapping=None, status="pending", manual_start=False
    )
    db_session.add(item)
    await db_session.commit()
    await db_session.refresh(item, ["archive", "library_file"])

    with patch("backend.app.services.filament_deficit.app_settings.base_dir", Path("/")):
        # No override: unpinned item → early [] (existing callers unaffected).
        assert await compute_deficit_for_queue_item(db_session, item) == []
        # With overrides: resolves the CANDIDATE's short spool → deficit fires.
        deficit = await compute_deficit_for_queue_item(
            db_session,
            item,
            printer_id_override=candidate.id,
            ams_mapping_override=json.dumps([0]),
        )

    assert len(deficit) == 1
    assert deficit[0].remaining_grams == 30.0
    # Item itself was never mutated by the candidate check.
    await db_session.refresh(item)
    assert item.printer_id is None
    assert item.ams_mapping is None
