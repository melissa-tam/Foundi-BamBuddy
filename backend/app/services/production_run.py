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
import math
from collections import Counter
from typing import TYPE_CHECKING

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.models.eject_profile import EjectProfile
from backend.app.models.print_batch import PrintBatch
from backend.app.models.printer import Printer
from backend.app.models.sku import SkuFile
from backend.app.services.farm_policy import (
    build_first_article_plan,
    create_new_first_article,
    farm_policy_defaults,
)
from backend.app.services.queue_builder import create_queue_items
from backend.app.services.sku_catalog import median_cycle_seconds
from backend.app.utils.printer_models import normalize_printer_model, normalize_printer_model_id

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.app.models.user import User
    from backend.app.schemas.production_run import RunCreate

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


async def build_run_response(db: AsyncSession, run: PrintBatch) -> dict:
    """Derive the RunResponse payload for a loaded run (queue_items eager-loaded)."""
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

    # Distinct assigned printers (resolved live from the items).
    printer_ids = sorted({item.printer_id for item in items if item.printer_id is not None})
    printers: list[dict] = []
    if printer_ids:
        result = await db.execute(select(Printer.id, Printer.name).where(Printer.id.in_(printer_ids)))
        printers = [{"id": pid, "name": name} for pid, name in result.all()]

    # ETA: median cycle × remaining plates ÷ distinct printers. Null when we
    # lack a cycle estimate or have no printer to run the remainder.
    rows = [{"printer_id": it.printer_id, "started_at": it.started_at} for it in items]
    median_cycle = median_cycle_seconds(rows)
    remaining_plates = plates_pending + plates_printing
    distinct_printers = len(printers)
    eta_seconds: float | None = None
    if median_cycle is not None and distinct_printers > 0 and remaining_plates > 0:
        eta_seconds = median_cycle * remaining_plates / distinct_printers

    return {
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
        "require_first_article": run.require_first_article,
        "first_article_state": run.first_article_state,
        "first_article_reject_reason": run.first_article_reject_reason,
        "retry_max_per_unit": run.retry_max_per_unit,
        "escalate_consecutive_failures": run.escalate_consecutive_failures,
        "eta_seconds": eta_seconds,
        "printers": printers,
        "created_at": run.created_at,
    }


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
        await create_new_first_article(db, run)
        await db.commit()
        return await _load_run(db, run_id)

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
    await db.commit()
    return await _load_run(db, run_id)


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
