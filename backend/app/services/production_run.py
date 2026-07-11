"""Farm production-run orchestration (Phase 2).

A production run is a :class:`PrintBatch` tied to a SKU file. This service owns:
- run creation (batch + N queue items via the shared queue-builder);
- derived stats (units/plates counts, ETA) — never stored counters;
- pause/resume/abort, which REUSE existing queue-item primitives
  (``manual_start`` staging and the ``cancelled`` status) rather than a parallel
  hold mechanism.
"""

from __future__ import annotations

import json
import logging
import math
from collections import Counter
from typing import TYPE_CHECKING

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.core.websocket import broadcast_production_run_changed
from backend.app.models.eject_profile import EjectProfile
from backend.app.models.print_batch import PrintBatch
from backend.app.models.printer import Printer
from backend.app.models.sku import SkuFile
from backend.app.services.farm_policy import (
    build_first_article_plan,
    create_new_first_article,
    farm_policy_defaults,
)
from backend.app.services.farm_staging import release_filament_staged
from backend.app.services.printer_manager import printer_manager
from backend.app.services.queue_builder import create_queue_items
from backend.app.services.sku_catalog import median_cycle_seconds
from backend.app.utils.printer_models import normalize_printer_model, normalize_printer_model_id

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.user import User
    from backend.app.schemas.production_run import RunCreate

logger = logging.getLogger(__name__)

# A run's status is the batch status; farm runs add "paused" alongside the
# existing active/completed/cancelled. These are the valid transitions.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "pause": {"active"},
    "resume": {"paused"},
    "abort": {"active", "paused"},
}


def plates_needed(target_units: int, units_per_plate: int) -> int:
    """Plates required to reach ``target_units`` at ``units_per_plate`` each.

    ``ceil(target_units / units_per_plate)`` — may over-produce (e.g. target 10
    at 3/plate → 4 plates = 12 units). ``units_per_plate`` is guaranteed ≥1.
    """
    if units_per_plate < 1:
        units_per_plate = 1
    return max(1, math.ceil(target_units / units_per_plate))


def can_transition(current_status: str, action: str) -> bool:
    """Whether ``action`` (pause/resume/abort) is valid from ``current_status``."""
    return current_status in ALLOWED_TRANSITIONS.get(action, set())


async def _load_run(db: AsyncSession, run_id: int) -> PrintBatch:
    """Load a farm production run (batch with sku_file_id set) or raise 404."""
    result = await db.execute(
        select(PrintBatch)
        .where(PrintBatch.id == run_id)
        .options(
            selectinload(PrintBatch.queue_items),
            selectinload(PrintBatch.sku_file).selectinload(SkuFile.sku),
        )
    )
    run = result.scalar_one_or_none()
    if run is None or run.sku_file_id is None:
        raise HTTPException(status_code=404, detail="Production run not found")
    return run


async def create_production_run(db: AsyncSession, data: RunCreate, current_user: User | None) -> PrintBatch:
    """Create a run: resolve targets/eject, create the batch + plate queue items.

    Raises 404 (sku_file / printer / eject profile missing) and 422 (no eject
    profile resolvable) as HTTPExceptions. Commits and returns the run.
    """
    # SKU file
    sku_file_result = await db.execute(
        select(SkuFile)
        .where(SkuFile.id == data.sku_file_id)
        .options(selectinload(SkuFile.sku), selectinload(SkuFile.library_file))
    )
    sku_file = sku_file_result.scalar_one_or_none()
    if sku_file is None:
        raise HTTPException(status_code=404, detail="SKU file not found")

    # Eject profile: explicit → SKU default. A farm run must eject.
    if data.eject_profile_id is not None:
        prof = await db.execute(select(EjectProfile.id).where(EjectProfile.id == data.eject_profile_id))
        if prof.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Eject profile not found")
        effective_eject_id = data.eject_profile_id
    else:
        effective_eject_id = sku_file.sku.default_eject_profile_id if sku_file.sku else None
    if effective_eject_id is None:
        raise HTTPException(
            status_code=422,
            detail="A farm production run must eject: provide eject_profile_id or set the SKU's default",
        )

    # Targets: printer_ids XOR target_model (schema-validated). Verify printers.
    printer_ids: list[int] = list(data.printer_ids or [])
    target_model_norm: str | None = None
    if printer_ids:
        found = await db.execute(select(Printer.id).where(Printer.id.in_(printer_ids)))
        existing_ids = {row[0] for row in found.all()}
        missing = [pid for pid in printer_ids if pid not in existing_ids]
        if missing:
            raise HTTPException(status_code=422, detail=f"Printer(s) not found: {missing}")
    else:
        raw = data.target_model or ""
        target_model_norm = normalize_printer_model(raw) or normalize_printer_model_id(raw) or raw

    upp = sku_file.units_per_plate or 1
    n_plates = plates_needed(data.target_units, upp)

    # Cache SJF print time + capability filament hint from the file metadata.
    cached_print_time = None
    required_filament_types = None
    meta = sku_file.library_file.file_metadata if sku_file.library_file else None
    if meta:
        cached_print_time = meta.get("print_time_seconds")
        ftype = meta.get("filament_type")
        if isinstance(ftype, str) and ftype.strip():
            required_filament_types = json.dumps([t.strip() for t in ftype.split(",") if t.strip()])

    run_name = f"{sku_file.sku.code} — {sku_file.sku.name} ×{n_plates}"[:255] if sku_file.sku else f"Run ×{n_plates}"

    # Failure-policy knobs: explicit override, else the global farm settings.
    default_retry_max, default_escalate = await farm_policy_defaults(db)
    retry_max = data.retry_max_per_unit if data.retry_max_per_unit is not None else default_retry_max
    escalate = (
        data.escalate_consecutive_failures if data.escalate_consecutive_failures is not None else default_escalate
    )
    require_fa = data.require_first_article if data.require_first_article is not None else True

    batch = PrintBatch(
        name=run_name,
        library_file_id=sku_file.library_file_id,
        quantity=n_plates,
        status="active",
        sku_file_id=sku_file.id,
        target_units=data.target_units,
        cooldown_temp_c_override=data.cooldown_temp_c_override,
        require_first_article=require_fa,
        first_article_state="pending_print" if require_fa else None,
        retry_max_per_unit=retry_max,
        escalate_consecutive_failures=escalate,
        created_by_id=current_user.id if current_user else None,
    )
    db.add(batch)
    await db.flush()  # need batch.id for the items

    # Fields shared by every plate item (the target — printer_id/target_model —
    # and first_article/status/batch_id are set per branch below).
    plate_fields = {
        "library_file_id": sku_file.library_file_id,
        "plate_id": sku_file.plate_index,
        "eject_profile_id": effective_eject_id,
        "print_time_seconds": cached_print_time,
        "required_filament_types": required_filament_types,
        "created_by_id": current_user.id if current_user else None,
    }
    base_fields = {**plate_fields, "batch_id": batch.id, "status": "pending"}

    if require_fa:
        # Gated run: create ONLY the first-article plate now; the remaining
        # plates are stored as a plan and materialised on approval (so the
        # scheduler can't dispatch plate 2 before plate 1 is approved).
        fa_fields = {**base_fields, "first_article": True}
        if printer_ids:
            fa_fields["printer_id"] = printer_ids[0]
            await create_queue_items(db, count=1, printer_id=printer_ids[0], fields=fa_fields)
        else:
            fa_fields["printer_id"] = None
            fa_fields["target_model"] = target_model_norm
            await create_queue_items(db, count=1, printer_id=None, fields=fa_fields)
        batch.first_article_plan = build_first_article_plan(
            remaining=n_plates - 1,
            printer_ids=printer_ids or None,
            target_model=target_model_norm,
            base_fields=plate_fields,
        )
    elif printer_ids:
        # Round-robin the plates across the requested printers; each printer gets
        # a contiguous position block (allocated per-printer scope).
        assignments = [printer_ids[i % len(printer_ids)] for i in range(n_plates)]
        for pid, count in Counter(assignments).items():
            await create_queue_items(
                db,
                count=count,
                printer_id=pid,
                fields={**base_fields, "printer_id": pid, "first_article": False},
            )
    else:
        await create_queue_items(
            db,
            count=n_plates,
            printer_id=None,
            fields={**base_fields, "printer_id": None, "target_model": target_model_norm, "first_article": False},
        )

    await db.commit()
    return await _load_run(db, batch.id)


def _build_printer_states(printer_rows: list[Printer], items: list[PrintQueueItem]) -> tuple[list[dict], bool]:
    """Derive the per-printer blocked-state entries for a run (Phase 4.1).

    Everything here is DERIVED per 3NF — DB flags (quarantine) plus the live
    ``printer_manager`` flags the scheduler actually gates on (plate-clear gate,
    model mismatch, connectivity; model_mismatch has no DB column at all) plus
    per-item waiting_reason machine codes (offline stall, printer-vision hold).
    Returns ``(states, any_blocked)``. Mirrors ``farm_policy._printer_unavailable``
    for the never-connected case: a printer with no live status yet is *unknown*,
    not offline, so tests/startup don't spuriously report every printer blocked.
    """
    stalled_pids = {
        it.printer_id for it in items if it.printer_id is not None and it.waiting_reason == "printer_offline_stalled"
    }
    vision_pids = {
        it.printer_id
        for it in items
        if it.printer_id is not None and it.waiting_reason == "plate_not_empty_printer_detected"
    }
    states: list[dict] = []
    any_blocked = False
    for p in printer_rows:
        connected = printer_manager.is_connected(p.id)
        state = {
            "printer_id": p.id,
            "name": p.name,
            "connected": connected,
            "quarantined": bool(p.quarantined),
            "awaiting_plate_clear": printer_manager.is_awaiting_plate_clear(p.id),
            "model_mismatch": printer_manager.is_model_mismatch(p.id),
            "model_mismatch_reason": printer_manager.model_mismatch_reason(p.id),
            "stalled": p.id in stalled_pids,
            "vision_hold": p.id in vision_pids,
        }
        disconnected = printer_manager.get_status(p.id) is not None and not connected
        if (
            state["quarantined"]
            or state["awaiting_plate_clear"]
            or state["model_mismatch"]
            or state["stalled"]
            or state["vision_hold"]
            or disconnected
        ):
            any_blocked = True
        states.append(state)
    return states, any_blocked


async def build_run_response(db: AsyncSession, run: PrintBatch, *, detail: bool = False) -> dict:
    """Derive the RunResponse payload for a loaded run (queue_items eager-loaded).

    ``detail=False`` (the list endpoint) stays lean: progress counts plus the
    Phase-4 hold summary (``pause_reason``, the two staged counts and the
    ``has_blocked_printers`` boolean). ``detail=True`` (GET /production-runs/{id})
    additionally returns the full ``printer_states`` entries and the per-unit
    ``units`` list. Everything beyond ``pause_reason`` is DERIVED — no stored
    counters (3NF).
    """
    items = list(run.queue_items)
    sku_file = run.sku_file
    upp = sku_file.units_per_plate if sku_file else 1
    sku_code = sku_file.sku.code if (sku_file and sku_file.sku) else None

    counts = Counter(item.status for item in items)
    # Plate accounting must include plates NOT yet created (a gated run defers the
    # rest of its plates until the first article is approved). ``quantity`` is the
    # planned plate count; ``non_retry_created`` is the primary plates already
    # materialised (retries don't consume a plan slot).
    non_retry_created = sum(1 for it in items if (it.retry_count or 0) == 0)
    planned_plates = max(run.quantity or 0, non_retry_created)
    uncreated = max(0, planned_plates - non_retry_created)
    plates_total = planned_plates
    plates_completed = counts.get("completed", 0)
    plates_failed = counts.get("failed", 0)
    plates_pending = counts.get("pending", 0) + uncreated
    plates_printing = counts.get("printing", 0)

    # System-staged split (Phase 4.1): manual_start marks a held pending item;
    # filament_short distinguishes the scheduler's low-spool staging (swap the
    # spool, then Resume/re-check) from any other hold (pause, operator staging).
    pending_items = [it for it in items if it.status == "pending"]
    staged_filament_short = sum(1 for it in pending_items if it.manual_start and it.filament_short)
    staged_other = sum(1 for it in pending_items if it.manual_start and not it.filament_short)

    # Distinct assigned printers (resolved live from the items). Full rows: the
    # per-printer blocked-state derivation needs the persisted quarantine flag.
    printer_ids = sorted({item.printer_id for item in items if item.printer_id is not None})
    printer_rows: list[Printer] = []
    if printer_ids:
        result = await db.execute(select(Printer).where(Printer.id.in_(printer_ids)))
        printer_rows = sorted(result.scalars().all(), key=lambda p: p.id)
    printers = [{"id": p.id, "name": p.name} for p in printer_rows]

    printer_states, has_blocked_printers = _build_printer_states(printer_rows, items)

    # ETA: median cycle × remaining plates ÷ distinct printers. Null when we
    # lack a cycle estimate or have no printer to run the remainder.
    rows = [{"printer_id": it.printer_id, "started_at": it.started_at} for it in items]
    median_cycle = median_cycle_seconds(rows)
    remaining_plates = plates_pending + plates_printing
    distinct_printers = len(printers)
    eta_seconds: float | None = None
    if median_cycle is not None and distinct_printers > 0 and remaining_plates > 0:
        eta_seconds = median_cycle * remaining_plates / distinct_printers

    response = {
        "id": run.id,
        "name": run.name,
        "sku_code": sku_code,
        "sku_file_id": run.sku_file_id,
        "target_units": run.target_units,
        "units_planned": plates_total * upp,
        "units_completed": plates_completed * upp,
        "units_failed": plates_failed * upp,
        "plates_total": plates_total,
        "plates_completed": plates_completed,
        "plates_failed": plates_failed,
        "plates_pending": plates_pending,
        "status": run.status,
        "pause_reason": run.pause_reason,
        "staged_filament_short": staged_filament_short,
        "staged_other": staged_other,
        "has_blocked_printers": has_blocked_printers,
        "require_first_article": run.require_first_article,
        "first_article_state": run.first_article_state,
        "first_article_reject_reason": run.first_article_reject_reason,
        "retry_max_per_unit": run.retry_max_per_unit,
        "escalate_consecutive_failures": run.escalate_consecutive_failures,
        "eta_seconds": eta_seconds,
        "printers": printers,
        "created_at": run.created_at,
    }
    if detail:
        name_by_id = {p.id: p.name for p in printer_rows}
        response["printer_states"] = printer_states
        response["units"] = [
            {
                "id": it.id,
                "status": it.status,
                "stop_source": it.stop_source,
                "waiting_reason": it.waiting_reason,
                "printer_id": it.printer_id,
                "printer_name": name_by_id.get(it.printer_id) if it.printer_id is not None else None,
                "started_at": it.started_at,
                "completed_at": it.completed_at,
                "retry_of_id": it.retry_of_id,
                "retry_count": it.retry_count or 0,
                "filament_short": bool(it.filament_short),
                "manual_start": bool(it.manual_start),
                "first_article": bool(it.first_article),
                "error_message": it.error_message,
            }
            for it in sorted(items, key=lambda i: i.id)
        ]
    return response


async def transition_run(db: AsyncSession, run_id: int, action: str) -> PrintBatch:
    """Apply pause/resume/abort to a run, reusing queue-item primitives.

    - pause  → set ``manual_start=True`` on still-pending items, status "paused"
    - resume → clear ``manual_start`` on pending items, status "active".
      **Resuming a run that was paused by a first-article REJECT re-dispatches a
      NEW first article**: the state returns to ``pending_print`` and a fresh
      first-article queue item is created from the run's stored plan (the
      previous rejected article stays as history). This is the only supported way
      to continue a rejected run — there is no "approve the rejected one".
    - abort  → cancel pending items (status "cancelled"), status "cancelled"

    Raises 409 on an invalid transition for the current status.
    """
    run = await _load_run(db, run_id)
    if not can_transition(run.status, action):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot {action} a run in status '{run.status}'",
        )

    # Resume of a rejected run: re-dispatch a fresh first article instead of just
    # un-staging pending items (there are none — the plates are still deferred).
    if action == "resume" and run.first_article_state == "rejected":
        run.first_article_state = "pending_print"
        run.status = "active"
        run.pause_reason = None
        await create_new_first_article(db, run)
        await db.commit()
        broadcast_production_run_changed(run_id)
        return await _load_run(db, run_id)

    # Low-spool staging re-check (Phase 4.2): BEFORE force-clearing manual_start
    # below, re-run the deficit check for the run's printers so items whose spool
    # was swapped also drop their filament_short flag (the loop alone would clear
    # manual_start but leave the stale low-spool badge until the next tick).
    if action == "resume":
        for pid in sorted({it.printer_id for it in run.queue_items if it.printer_id is not None}):
            await release_filament_staged(db, pid)

    for item in run.queue_items:
        if item.status != "pending":
            continue
        if action == "pause":
            item.manual_start = True
        elif action == "resume":
            item.manual_start = False
        elif action == "abort":
            item.status = "cancelled"

    run.status = {"pause": "paused", "resume": "active", "abort": "cancelled"}[action]
    if action == "resume":
        # Resuming clears the hold reason and tops the run back up to plan (Phase
        # 3.1): replacements for units consumed without output (operator stops,
        # exhausted failures). Idempotent — recomputed from live counts.
        run.pause_reason = None
        await db.commit()
        broadcast_production_run_changed(run_id)
        run = await _load_run(db, run_id)
        await top_up_run(db, run)
    else:
        if action == "pause":
            # Machine-readable hold reason (Phase 4.1): a manual pause is
            # distinguishable from the auto-pauses. Cleared on resume above.
            run.pause_reason = "operator"
        await db.commit()
        broadcast_production_run_changed(run_id)
    return await _load_run(db, run_id)


async def top_up_run(db: AsyncSession, run: PrintBatch) -> int:
    """Create replacement queue items for units consumed WITHOUT output (Phase 3.1).

    A unit that was cancelled/stopped — or failed with its retry chain exhausted —
    leaves the run short of its planned plate count. This recomputes the shortfall
    from LIVE queue state (never a stored counter — 3NF) and materialises exactly
    that many fresh plate items, reusing the same per-plate field template the run
    was built with (via ``create_queue_items``, the shared builder — no hand-rolled
    fields). Idempotent: called on every RESUME, a zero-deficit resume is a no-op.
    Returns the number of replacement items created.

    Deficit = (primary plate chains) − (chains still productive), where a plate
    *chain* is the primary item (``retry_count`` 0) plus its retry lineage, and a
    chain is "productive" if any member is completed/pending/printing (a live retry
    keeps its chain productive). Cancelled/stopped and exhausted-failed chains ARE
    the deficit. A gated run that hasn't cleared its first-article gate defers its
    plates to the FA flow, so top-up is a deliberate no-op there.
    """
    # Never top up around an un-cleared first-article gate — the FA flow owns
    # plate creation while a run is gated (mirrors ``_maybe_complete_run``).
    if run.first_article_plan or run.first_article_state in ("pending_print", "awaiting_approval", "rejected"):
        return 0

    items = list(run.queue_items)
    if not items:
        return 0
    by_id = {it.id: it for it in items}

    def _root_id(it: PrintQueueItem) -> int:
        cur = it
        guard = 0
        while cur.retry_of_id is not None and cur.retry_of_id in by_id and guard < 1000:
            cur = by_id[cur.retry_of_id]
            guard += 1
        return cur.id

    chains: dict[int, list[PrintQueueItem]] = {}
    for it in items:
        chains.setdefault(_root_id(it), []).append(it)

    live_statuses = {"completed", "pending", "printing"}
    productive = sum(1 for members in chains.values() if any(m.status in live_statuses for m in members))
    # Anchor the deficit to the PLANNED plate count (run.quantity), never the live
    # chain count — a replacement adds a productive chain, so anchoring to the chain
    # count would top up forever. run.quantity is stable across top-ups, so a second
    # resume with the same live productive count computes deficit 0 (idempotent).
    planned = run.quantity or 0
    deficit = max(0, planned - productive)
    if deficit <= 0:
        return 0

    # Reuse a materialised primary plate as the field template (all primaries share
    # the plate-defining fields). Prefer a non-first-article primary.
    primaries = [it for it in items if (it.retry_count or 0) == 0]
    template = next((it for it in primaries if not it.first_article), None) or (primaries[0] if primaries else None)
    if template is None:
        return 0
    fields = {
        "library_file_id": template.library_file_id,
        "archive_id": template.archive_id,
        "plate_id": template.plate_id,
        "eject_profile_id": template.eject_profile_id,
        "print_time_seconds": template.print_time_seconds,
        "required_filament_types": template.required_filament_types,
        "target_location": template.target_location,
        "created_by_id": template.created_by_id,
        "batch_id": run.id,
        "status": "pending",
        "first_article": False,
    }

    # Distribute across the same printers the run used (round-robin), else model-based.
    printer_ids = sorted({it.printer_id for it in primaries if it.printer_id is not None})
    if printer_ids:
        assignments = [printer_ids[i % len(printer_ids)] for i in range(deficit)]
        for pid, count in Counter(assignments).items():
            await create_queue_items(db, count=count, printer_id=pid, fields={**fields, "printer_id": pid})
    else:
        target_model = next((it.target_model for it in primaries if it.target_model), None)
        await create_queue_items(
            db, count=deficit, printer_id=None, fields={**fields, "printer_id": None, "target_model": target_model}
        )
    await db.commit()
    broadcast_production_run_changed(run.id)
    logger.info("production_run: topped up run %s with %d replacement plate(s)", run.id, deficit)
    return deficit


async def delete_production_run(db: AsyncSession, run_id: int) -> None:
    """Hard-delete a finished production run and every queue item it owns.

    Only a run in a terminal state ('cancelled' or 'completed') may be deleted;
    an active or paused run must be aborted first (409). Removes the
    ``PrintBatch`` row and all its ``PrintQueueItem`` rows.

    There is no DB-level cascade from the batch to its items: ``batch_id`` is
    ``ON DELETE SET NULL`` (and SQLite FK enforcement is off), so the children
    are deleted explicitly here rather than relying on the FK action. Print
    archives are left untouched — they are referenced BY queue items
    (``print_queue.archive_id``), never the reverse, so removing the items and
    batch cannot reach into archive history.

    Raises 404 (unknown run) and 409 (non-terminal run) as HTTPExceptions.
    """
    run = await _load_run(db, run_id)
    if run.status not in ("cancelled", "completed"):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete a run in status '{run.status}'; abort it first",
        )
    # Explicit child delete (no batch→item cascade); archives stay intact.
    for item in list(run.queue_items):
        await db.delete(item)
    await db.delete(run)
    await db.commit()
