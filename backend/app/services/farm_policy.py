"""Farm first-article + failure/quarantine policy (Phase 3).

Every run-lifecycle reaction to a terminal print status lives here so main.py
keeps a *single* hook call (``on_terminal``) and no farm logic leaks into the
345 KB monolith. Responsibilities:

- **First article**: a gated run prints one plate (``first_article=True``),
  holds (``first_article_state='awaiting_approval'``), and only materialises the
  rest of its plates once an operator approves. Approval can eject the part
  remotely (dispatch a part-present eject-only file) or trust that the operator
  physically removed it.
- **Retry**: a failed farm unit is re-queued up to ``retry_max_per_unit`` times,
  idempotently (exactly one retry per failure event, keyed by ``retry_of_id``).
- **Quarantine**: N consecutive terminal failures on one printer trip a
  DB-backed quarantine that excludes the printer from ALL dispatch.
- **Run pause**: when every printer a run can use is quarantined/offline the run
  is paused; a first-article reject also pauses it.
- **Run completion**: the last plate completing marks the batch ``completed``.

The eject GENERATION for a remote eject is a pure, tested helper
(``build_part_present_eject_file`` in ``services.eject.dispatch``); the FTPS
upload + MQTT ``project_file`` dispatch reuse the existing
``upload_file_async`` + ``printer_manager.start_print`` primitives — no
hand-rolled MQTT.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import TYPE_CHECKING

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.models.eject_profile import EjectProfile
from backend.app.models.library import LibraryFile
from backend.app.models.print_batch import PrintBatch
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.sku import SkuFile
from backend.app.services.notification_service import notification_service
from backend.app.services.printer_manager import printer_manager
from backend.app.services.queue_builder import create_queue_items

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_TERMINAL_RUN_OUTCOMES = ("completed", "failed")

# printer_id -> run_id for an in-flight remote first-article eject. In-memory:
# a restart between dispatch and completion drops the finalisation (the run
# stays 'awaiting_approval' with its plate gate set, so the operator simply
# re-approves — no bad state, just a repeat click).
_pending_remote_eject: dict[int, int] = {}


def _register_pending_remote_eject(printer_id: int, run_id: int) -> None:
    _pending_remote_eject[printer_id] = run_id


def _pop_pending_remote_eject(printer_id: int) -> int | None:
    return _pending_remote_eject.pop(printer_id, None)


# --------------------------------------------------------------------------- #
# Settings-backed policy defaults
# --------------------------------------------------------------------------- #
async def farm_policy_defaults(db: AsyncSession) -> tuple[int, int]:
    """Return ``(retry_max_per_unit, escalate_consecutive_failures)`` defaults.

    Read from the global farm settings, falling back to the schema defaults so a
    fresh install with no rows still resolves sane values.
    """
    from backend.app.api.routes.settings import get_setting

    retry_raw = await get_setting(db, "farm_retry_max_per_unit")
    escalate_raw = await get_setting(db, "farm_escalate_consecutive_failures")
    try:
        retry_max = int(retry_raw) if retry_raw is not None else 1
    except (TypeError, ValueError):
        retry_max = 1
    try:
        escalate = int(escalate_raw) if escalate_raw is not None else 2
    except (TypeError, ValueError):
        escalate = 2
    return retry_max, max(1, escalate)


# --------------------------------------------------------------------------- #
# Loading helpers
# --------------------------------------------------------------------------- #
async def _load_run(db: AsyncSession, run_id: int) -> PrintBatch:
    """Load a farm run (batch with sku_file_id) with items + sku, or 404."""
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


def _sku_code(run: PrintBatch) -> str | None:
    return run.sku_file.sku.code if (run.sku_file and run.sku_file.sku) else None


def _units_per_plate(run: PrintBatch) -> int:
    return (run.sku_file.units_per_plate if run.sku_file else 1) or 1


def _find_fa_item(run: PrintBatch) -> PrintQueueItem | None:
    """The most-recently-completed first-article item for the run, if any."""
    candidates = [i for i in run.queue_items if i.first_article and i.status == "completed"]
    if not candidates:
        candidates = [i for i in run.queue_items if i.first_article]
    if not candidates:
        return None
    return sorted(candidates, key=lambda i: (i.completed_at or i.created_at or 0))[-1]


# --------------------------------------------------------------------------- #
# Plan (deferred remaining plates) serialisation
# --------------------------------------------------------------------------- #
def build_first_article_plan(
    *,
    remaining: int,
    printer_ids: list[int] | None,
    target_model: str | None,
    base_fields: dict,
) -> str:
    """Serialise the not-yet-created plates so approval can materialise them."""
    return json.dumps(
        {
            "remaining": remaining,
            "printer_ids": printer_ids or None,
            "target_model": target_model,
            "base_fields": base_fields,
        }
    )


async def create_remaining_plates(db: AsyncSession, run: PrintBatch) -> int:
    """Materialise the deferred plates recorded in ``run.first_article_plan``.

    Returns the number of plate items created. Idempotent: consumes (clears) the
    plan so a duplicate approval can't double-create.
    """
    if not run.first_article_plan:
        return 0
    try:
        plan = json.loads(run.first_article_plan)
    except (TypeError, ValueError):
        run.first_article_plan = None
        await db.commit()
        return 0

    remaining = int(plan.get("remaining") or 0)
    base = dict(plan.get("base_fields") or {})
    printer_ids = plan.get("printer_ids")
    target_model = plan.get("target_model")

    if remaining <= 0:
        run.first_article_plan = None
        await db.commit()
        return 0

    base.pop("printer_id", None)
    base.pop("target_model", None)
    base.pop("first_article", None)
    fields_common = {**base, "batch_id": run.id, "status": "pending", "first_article": False}

    if printer_ids:
        # Continue the round-robin from index 1 — index 0 seeded the FA plate.
        assignments = [printer_ids[(i + 1) % len(printer_ids)] for i in range(remaining)]
        for pid, count in Counter(assignments).items():
            await create_queue_items(
                db, count=count, printer_id=pid, fields={**fields_common, "printer_id": pid}
            )
    else:
        await create_queue_items(
            db,
            count=remaining,
            printer_id=None,
            fields={**fields_common, "printer_id": None, "target_model": target_model},
        )

    run.first_article_plan = None
    await db.commit()
    return remaining


async def create_new_first_article(db: AsyncSession, run: PrintBatch) -> PrintQueueItem | None:
    """Create a fresh first-article item from the run's stored plan.

    Used by resume-after-reject to re-dispatch a new first article. The plan is
    left intact (still describes the remaining plates for the eventual approval).
    """
    if not run.first_article_plan:
        return None
    try:
        plan = json.loads(run.first_article_plan)
    except (TypeError, ValueError):
        return None
    base = dict(plan.get("base_fields") or {})
    printer_ids = plan.get("printer_ids")
    target_model = plan.get("target_model")
    base.pop("printer_id", None)
    base.pop("target_model", None)
    base.pop("first_article", None)
    fields = {
        **base,
        "batch_id": run.id,
        "status": "pending",
        "first_article": True,
    }
    if printer_ids:
        fields["printer_id"] = printer_ids[0]
        items = await create_queue_items(db, count=1, printer_id=printer_ids[0], fields=fields)
    else:
        fields["printer_id"] = None
        fields["target_model"] = target_model
        items = await create_queue_items(db, count=1, printer_id=None, fields=fields)
    # A fresh first article supersedes the previous rejection.
    run.first_article_reject_reason = None
    return items[0] if items else None


# --------------------------------------------------------------------------- #
# Terminal-status hook (the single entry point called from main.py)
# --------------------------------------------------------------------------- #
async def on_terminal(
    db: AsyncSession,
    printer_id: int | None,
    queue_item_id: int | None,
    final_status: str,
    archive_data: dict | None = None,
) -> None:
    """React to a terminal print status. Non-farm prints are a no-op.

    Called once from ``main.on_print_complete`` (the notification flow, where the
    finish photo is available). Wraps each sub-action so a notification failure
    can never abort a committed state change.
    """
    try:
        # 1. Remote first-article eject completion (no queue item involved).
        if final_status == "completed" and printer_id is not None:
            run_id = _pop_pending_remote_eject(printer_id)
            if run_id is not None:
                await _finalize_remote_eject(db, run_id, printer_id)
                return

        # 2. Item-based policy.
        if queue_item_id is None:
            return
        item = await db.get(PrintQueueItem, queue_item_id)
        if item is None or item.batch_id is None:
            return
        batch = await db.get(PrintBatch, item.batch_id)
        if batch is None or batch.sku_file_id is None:
            return  # non-farm batch — leave it alone

        if final_status == "completed":
            await _on_item_completed(db, batch, item, archive_data)
        elif final_status == "failed":
            await _on_item_failed(db, batch, item)
    except Exception:  # noqa: BLE001 — policy must never crash the callback chain
        logger.exception("farm_policy.on_terminal failed for item=%s status=%s", queue_item_id, final_status)


async def _on_item_completed(
    db: AsyncSession, batch: PrintBatch, item: PrintQueueItem, archive_data: dict | None
) -> None:
    if item.first_article and batch.first_article_state == "pending_print":
        batch.first_article_state = "awaiting_approval"
        await db.commit()
        await _notify_first_article_pending(db, batch, item, archive_data)
        return
    await _maybe_complete_run(db, batch)


async def _on_item_failed(db: AsyncSession, batch: PrintBatch, item: PrintQueueItem) -> None:
    retry_max = batch.retry_max_per_unit if batch.retry_max_per_unit is not None else 1
    if (item.retry_count or 0) < retry_max:
        await create_retry_if_absent(db, item)
    await maybe_quarantine_printer(db, batch, item)
    await _maybe_pause_run_no_printers(db, batch)


# --------------------------------------------------------------------------- #
# Retry
# --------------------------------------------------------------------------- #
async def create_retry_if_absent(db: AsyncSession, item: PrintQueueItem) -> PrintQueueItem | None:
    """Create exactly one retry for a failed ``item`` (idempotent via retry_of_id).

    The retry preserves the ``first_article`` flag (a failed first article is
    re-attempted as a first article) and the plate/profile/printer/model target.
    """
    existing = await db.execute(select(PrintQueueItem.id).where(PrintQueueItem.retry_of_id == item.id))
    if existing.first() is not None:
        return None  # already retried this failure event

    fields = {
        "printer_id": item.printer_id,
        "target_model": item.target_model,
        "target_location": item.target_location,
        "required_filament_types": item.required_filament_types,
        "library_file_id": item.library_file_id,
        "archive_id": item.archive_id,
        "batch_id": item.batch_id,
        "eject_profile_id": item.eject_profile_id,
        "plate_id": item.plate_id,
        "print_time_seconds": item.print_time_seconds,
        "created_by_id": item.created_by_id,
        "status": "pending",
        "first_article": item.first_article,
        "retry_count": (item.retry_count or 0) + 1,
        "retry_of_id": item.id,
    }
    created = await create_queue_items(db, count=1, printer_id=item.printer_id, fields=fields)
    await db.commit()
    logger.info("farm_policy: created retry #%d for failed item %s", fields["retry_count"], item.id)
    return created[0] if created else None


# --------------------------------------------------------------------------- #
# Quarantine
# --------------------------------------------------------------------------- #
async def recent_terminal_farm_items(db: AsyncSession, printer_id: int, limit: int) -> list[PrintQueueItem]:
    """The last ``limit`` terminal (completed/failed) farm items on ``printer_id``.

    "Farm" = the item belongs to a batch with a ``sku_file_id``. Ordered
    most-recent-first by ``completed_at``.
    """
    result = await db.execute(
        select(PrintQueueItem)
        .join(PrintBatch, PrintQueueItem.batch_id == PrintBatch.id)
        .where(PrintQueueItem.printer_id == printer_id)
        .where(PrintBatch.sku_file_id.is_not(None))
        .where(PrintQueueItem.status.in_(_TERMINAL_RUN_OUTCOMES))
        .order_by(PrintQueueItem.completed_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def maybe_quarantine_printer(db: AsyncSession, batch: PrintBatch, item: PrintQueueItem) -> bool:
    """Quarantine ``item.printer_id`` when the last N farm outcomes all failed.

    N = the run's ``escalate_consecutive_failures``. Returns True if it tripped.
    """
    printer_id = item.printer_id
    if printer_id is None:
        return False
    threshold = batch.escalate_consecutive_failures if batch.escalate_consecutive_failures else 2
    recent = await recent_terminal_farm_items(db, printer_id, threshold)
    if len(recent) < threshold or not all(r.status == "failed" for r in recent):
        return False

    printer = await db.get(Printer, printer_id)
    if printer is None or printer.quarantined:
        return False

    reason = f"{threshold} consecutive farm print failures"
    printer.quarantined = True
    printer.quarantine_reason = reason
    await db.commit()
    printer_manager.set_quarantined(printer_id, True)
    logger.warning("farm_policy: quarantined printer %s — %s", printer_id, reason)
    await notification_service.on_printer_quarantined(printer_id, printer.name, threshold, reason, db)
    return True


async def clear_quarantine(db: AsyncSession, printer_id: int) -> Printer:
    """Clear a printer's quarantine flag + reason (route helper). 404 if missing."""
    printer = await db.get(Printer, printer_id)
    if printer is None:
        raise HTTPException(status_code=404, detail="Printer not found")
    printer.quarantined = False
    printer.quarantine_reason = None
    await db.commit()
    printer_manager.set_quarantined(printer_id, False)
    logger.info("farm_policy: quarantine cleared for printer %s", printer_id)
    return printer


# --------------------------------------------------------------------------- #
# Run pause / completion
# --------------------------------------------------------------------------- #
def _printer_unavailable(printer: Printer | None) -> bool:
    """A printer is unavailable for a run when quarantined, or connected-then-lost.

    A printer that was never connected in this process (no live status) is
    treated as *unknown*, not offline, so a run isn't spuriously paused at
    startup or in tests where no MQTT session exists.
    """
    if printer is None:
        return True
    if printer.quarantined:
        return True
    status = printer_manager.get_status(printer.id)
    return status is not None and not printer_manager.is_connected(printer.id)


async def _maybe_pause_run_no_printers(db: AsyncSession, batch: PrintBatch) -> None:
    if batch.status != "active":
        return
    result = await db.execute(
        select(PrintQueueItem).where(
            PrintQueueItem.batch_id == batch.id,
            PrintQueueItem.status.in_(("pending", "printing")),
        )
    )
    active_items = list(result.scalars().all())
    printer_ids = {i.printer_id for i in active_items if i.printer_id is not None}
    if not printer_ids:
        return  # model-based / unassigned — scheduler's waiting_reason owns this

    printers = {
        p.id: p
        for p in (await db.execute(select(Printer).where(Printer.id.in_(printer_ids)))).scalars().all()
    }
    if not all(_printer_unavailable(printers.get(pid)) for pid in printer_ids):
        return

    batch.status = "paused"
    await db.commit()
    run = await _load_run(db, batch.id)
    await notification_service.on_run_paused(
        run.name, _sku_code(run), "All selected printers are quarantined or offline", db
    )
    logger.warning("farm_policy: paused run %s — no available printers", batch.id)


async def _maybe_complete_run(db: AsyncSession, batch: PrintBatch) -> None:
    if batch.status != "active":
        return
    # A gated run whose FA isn't approved still has uncreated plates.
    if batch.first_article_state in ("pending_print", "awaiting_approval", "rejected"):
        return
    if batch.first_article_plan:
        return  # deferred plates not yet materialised

    result = await db.execute(select(PrintQueueItem).where(PrintQueueItem.batch_id == batch.id))
    items = list(result.scalars().all())
    if any(i.status in ("pending", "printing") for i in items):
        return
    completed = sum(1 for i in items if i.status == "completed")
    if completed == 0:
        return

    batch.status = "completed"
    await db.commit()
    run = await _load_run(db, batch.id)
    upp = _units_per_plate(run)
    await notification_service.on_run_completed(run.name, _sku_code(run), completed * upp, completed, db)
    logger.info("farm_policy: run %s completed (%d plates)", batch.id, completed)


# --------------------------------------------------------------------------- #
# First-article approve / reject / finalize
# --------------------------------------------------------------------------- #
async def _notify_first_article_pending(
    db: AsyncSession, batch: PrintBatch, item: PrintQueueItem, archive_data: dict | None
) -> None:
    run = await _load_run(db, batch.id)
    printer_name = "Unknown"
    if item.printer_id is not None:
        printer = await db.get(Printer, item.printer_id)
        if printer is not None:
            printer_name = printer.name
    finish_photo_url = (archive_data or {}).get("finish_photo_url")
    image_data = (archive_data or {}).get("image_data")
    await notification_service.on_first_article_pending(
        item.printer_id,
        printer_name,
        run.name,
        _sku_code(run),
        db,
        finish_photo_url=finish_photo_url,
        image_data=image_data,
    )


async def approve_first_article(db: AsyncSession, run_id: int, eject_remotely: bool) -> PrintBatch:
    """Approve a run's first article.

    ``eject_remotely=False``: the operator physically removed the part — clear
    the plate gate (same mechanism as the manual plate-clear confirm), mark
    ``approved``, materialise the remaining plates.

    ``eject_remotely=True``: dispatch a part-present eject-only job; the run stays
    ``awaiting_approval`` until that eject completes, at which point
    ``_finalize_remote_eject`` clears the gate, marks ``approved`` and creates the
    remaining plates. A dispatch failure raises 409/502 and leaves the state as
    ``awaiting_approval``.
    """
    run = await _load_run(db, run_id)
    if run.first_article_state != "awaiting_approval":
        raise HTTPException(
            status_code=409,
            detail=f"First article is not awaiting approval (state={run.first_article_state})",
        )
    fa_item = _find_fa_item(run)

    if eject_remotely:
        await _dispatch_remote_eject(db, run, fa_item)
        return await _load_run(db, run_id)

    run.first_article_state = "approved"
    await db.commit()
    if fa_item is not None and fa_item.printer_id is not None:
        printer_manager.set_awaiting_plate_clear(fa_item.printer_id, False)
    run = await _load_run(db, run_id)
    await create_remaining_plates(db, run)
    return await _load_run(db, run_id)


async def reject_first_article(db: AsyncSession, run_id: int, reason: str) -> PrintBatch:
    """Reject a run's first article: mark ``rejected``, pause the run, notify.

    The plate gate is left SET — the rejected part is still on the plate for the
    operator to inspect/remove. Resuming a rejected run re-dispatches a new first
    article (see ``services.production_run.transition_run``).
    """
    run = await _load_run(db, run_id)
    if run.first_article_state != "awaiting_approval":
        raise HTTPException(
            status_code=409,
            detail=f"First article is not awaiting approval (state={run.first_article_state})",
        )
    run.first_article_state = "rejected"
    run.first_article_reject_reason = reason
    run.status = "paused"
    await db.commit()
    run = await _load_run(db, run_id)
    await notification_service.on_run_paused(run.name, _sku_code(run), f"First article rejected: {reason}", db)
    return run


async def _finalize_remote_eject(db: AsyncSession, run_id: int, printer_id: int) -> None:
    run = await db.get(PrintBatch, run_id)
    if run is None or run.first_article_state != "awaiting_approval":
        return
    run.first_article_state = "approved"
    await db.commit()
    printer_manager.set_awaiting_plate_clear(printer_id, False)
    run = await _load_run(db, run_id)
    await create_remaining_plates(db, run)
    logger.info("farm_policy: remote eject finalised run %s on printer %s", run_id, printer_id)


async def _dispatch_remote_eject(db: AsyncSession, run: PrintBatch, fa_item: PrintQueueItem | None) -> None:
    """FTPS-upload + project_file dispatch a part-present eject for the FA plate.

    Reuses ``build_part_present_eject_file`` (generator + validator + repack) and
    the existing FTP/MQTT primitives. Raises 409 on a bad precondition, 502 on a
    dispatch failure — never leaves the run in a half state (state stays
    ``awaiting_approval`` and the caller reports the error).
    """
    from pathlib import Path

    from backend.app.core.config import settings
    from backend.app.services.bambu_ftp import (
        get_ftp_retry_settings,
        upload_file_async,
        with_ftp_retry,
    )
    from backend.app.services.eject.dispatch import build_part_present_eject_file
    from backend.app.services.eject.generator import PRINTER_BED_DIMS
    from backend.app.utils.filename import derive_remote_filename

    if fa_item is None or fa_item.printer_id is None:
        raise HTTPException(status_code=409, detail="First-article printer is unknown; cannot eject remotely")
    if fa_item.eject_profile_id is None:
        raise HTTPException(status_code=409, detail="First article has no eject profile; cannot eject remotely")

    printer = await db.get(Printer, fa_item.printer_id)
    if printer is None:
        raise HTTPException(status_code=409, detail="First-article printer not found")
    if not printer_manager.is_connected(printer.id):
        raise HTTPException(status_code=409, detail="Printer is not connected; cannot eject remotely")
    model = printer.model or ""
    if model not in PRINTER_BED_DIMS:
        raise HTTPException(status_code=409, detail=f"No eject bed geometry for printer model {model!r}")

    profile = await db.get(EjectProfile, fa_item.eject_profile_id)
    if profile is None:
        raise HTTPException(status_code=409, detail="Eject profile not found")

    lib = await db.get(LibraryFile, fa_item.library_file_id) if fa_item.library_file_id else None
    if lib is None:
        raise HTTPException(status_code=409, detail="First-article source file not found")
    lib_path = Path(lib.file_path)
    source_path = lib_path if lib_path.is_absolute() else settings.base_dir / lib.file_path
    if not source_path.exists():
        raise HTTPException(status_code=409, detail="First-article source file is missing on disk")

    plate_id = fa_item.plate_id or 1
    try:
        eject_path = build_part_present_eject_file(
            source_path, plate_id, profile, model, cooldown_temp_c=run.cooldown_temp_c_override
        )
    except Exception as exc:  # noqa: BLE001 — surface as an actionable 409
        raise HTTPException(status_code=409, detail=f"Failed to build part-present eject file: {exc}") from exc

    remote_filename = derive_remote_filename(f"fa_eject_{run.id}.gcode.3mf")
    remote_path = f"/{remote_filename}"
    ftp_retry_enabled, ftp_retry_count, ftp_retry_delay, ftp_timeout = await get_ftp_retry_settings()
    try:
        if ftp_retry_enabled:
            uploaded = await with_ftp_retry(
                upload_file_async,
                printer.ip_address,
                printer.access_code,
                eject_path,
                remote_path,
                socket_timeout=ftp_timeout,
                printer_model=printer.model,
                max_retries=ftp_retry_count,
                retry_delay=ftp_retry_delay,
                operation_name=f"Upload FA eject to {printer.name}",
            )
        else:
            uploaded = await upload_file_async(
                printer.ip_address,
                printer.access_code,
                eject_path,
                remote_path,
                socket_timeout=ftp_timeout,
                printer_model=printer.model,
            )
    except Exception as exc:  # noqa: BLE001
        uploaded = False
        logger.error("farm_policy: FA eject upload error: %s", exc)
    finally:
        try:
            eject_path.unlink(missing_ok=True)
        except OSError:
            pass

    if not uploaded:
        raise HTTPException(status_code=502, detail="Failed to upload the eject file to the printer")

    started = printer_manager.start_print(printer.id, remote_filename, plate_id=plate_id, use_ams=False)
    if not started:
        raise HTTPException(status_code=502, detail="Failed to send the eject command to the printer")

    _register_pending_remote_eject(printer.id, run.id)
    printer_manager.set_awaiting_plate_clear(printer.id, False)
    logger.info("farm_policy: dispatched remote FA eject for run %s on printer %s", run.id, printer.id)
