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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from backend.app.core.websocket import broadcast_production_run_changed
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
    # Approve-while-paused stages the materialised plates (manual_start=True) so
    # resume releases them together (R6, same class as R1); an active-run approval
    # dispatches immediately. Covers the _finalize_remote_eject path too.
    fields_common = {
        **base,
        "batch_id": run.id,
        "status": "pending",
        "first_article": False,
        "manual_start": run.status == "paused",
    }

    if printer_ids:
        # Continue the round-robin from index 1 — index 0 seeded the FA plate.
        assignments = [printer_ids[(i + 1) % len(printer_ids)] for i in range(remaining)]
        for pid, count in Counter(assignments).items():
            await create_queue_items(db, count=count, printer_id=pid, fields={**fields_common, "printer_id": pid})
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
    completed_subtask_id: str | None = None,
) -> None:
    """React to a terminal print status. Non-farm prints are a no-op.

    Called once from ``main.on_print_complete`` (the notification flow, where the
    finish photo is available). Wraps each sub-action so a notification failure
    can never abort a committed state change.

    ``completed_subtask_id`` is the terminal payload's subtask_id, used to confirm
    that a "completed" is really the remote first-article eject Bambuddy dispatched
    (which echoes ``last_dispatch_subtask_id``) before consuming the pending eject —
    a foreign completion must not finalise someone else's approval (Phase 1).
    """
    try:
        # 1. Remote first-article eject completion (no queue item involved). The FA
        #    eject is dispatched via start_print, so the printer echoes its
        #    submission id back. Only finalise when the completed job IS that eject:
        #    on a positive mismatch (both ids known and different) treat the
        #    completion as foreign and leave the pending eject for the real finish.
        if final_status == "completed" and printer_id is not None:
            client = printer_manager.get_client(printer_id)
            expected_subtask = getattr(client, "last_dispatch_subtask_id", None) if client else None
            if completed_subtask_id and expected_subtask and completed_subtask_id != expected_subtask:
                logger.info(
                    "farm_policy: printer %s completion subtask %r != dispatched eject subtask %r — "
                    "foreign, not popping pending remote eject",
                    printer_id,
                    completed_subtask_id,
                    expected_subtask,
                )
            else:
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
        elif final_status == "cancelled" and item.stop_source:
            # Operator stop (UI or printer screen), attributed by the terminal
            # handler. NOT a failure: no retry, no quarantine contribution — just
            # a visible hold + notification (Phase 3.1). A 'cancelled' with NO
            # stop_source (e.g. a run-abort or an unattributed interruption) is a
            # deliberate no-op here.
            await on_operator_stop(db, batch, item)
    except Exception:  # noqa: BLE001 — policy must never crash the callback chain
        logger.exception("farm_policy.on_terminal failed for item=%s status=%s", queue_item_id, final_status)


async def _on_item_completed(
    db: AsyncSession, batch: PrintBatch, item: PrintQueueItem, archive_data: dict | None
) -> None:
    if item.first_article and batch.first_article_state == "pending_print":
        batch.first_article_state = "awaiting_approval"
        await db.commit()
        broadcast_production_run_changed(batch.id)
        await _notify_first_article_pending(db, batch, item, archive_data)
        return
    await _maybe_complete_run(db, batch)


async def _on_item_failed(db: AsyncSession, batch: PrintBatch, item: PrintQueueItem) -> None:
    # A terminal run (aborted/completed) still counts this failure toward printer
    # health — quarantine is independent of run intent — but must NOT mint a
    # dispatchable retry (R1/R2: a cancelled run would silently print an extra
    # plate) nor evaluate the run-pause helpers.
    if batch.status in ("cancelled", "completed"):
        await maybe_quarantine_printer(db, batch, item)
        return

    retry_max = batch.retry_max_per_unit if batch.retry_max_per_unit is not None else 1
    if (item.retry_count or 0) < retry_max:
        # A paused run keeps re-queuing failed units, but STAGED (manual_start=True)
        # so the retry can't dispatch while paused; resume's manual_start sweep
        # releases it (R1). An active run's retry dispatches as today.
        await create_retry_if_absent(db, item, stage_manual=batch.status == "paused")
    await maybe_quarantine_printer(db, batch, item)
    await _maybe_pause_run_no_printers(db, batch)
    await _maybe_pause_run_exhausted(db, batch)


async def on_operator_stop(db: AsyncSession, batch: PrintBatch, item: PrintQueueItem) -> None:
    """A farm unit was deliberately stopped by the operator (Phase 3.1).

    Called from :func:`on_terminal` when a farm item lands terminal ``cancelled``
    WITH ``stop_source`` set. Deliberately does the OPPOSITE of a failure:

    - NO auto-retry (the operator chose to stop this unit);
    - NOT counted toward quarantine — ``cancelled`` is already outside
      ``_TERMINAL_RUN_OUTCOMES`` (kept that way), so ``recent_terminal_farm_items``
      never sees it;
    - the plate-clear gate is left exactly as the deposit path set it (the part is
      on the plate for the operator to clear);
    - the run STAYS ``active`` but records ``pause_reason='operator_stop'`` as a
      visible hold — RESUME clears it and tops the run back up (``top_up_run``);
    - fires the run-scoped ``on_run_unit_stopped`` notification. The generic
      upstream ``on_print_stopped`` may also fire independently — their templates
      don't duplicate content.
    """
    batch.pause_reason = "operator_stop"
    await db.commit()
    broadcast_production_run_changed(batch.id)

    printer_name = "Unknown"
    if item.printer_id is not None:
        printer = await db.get(Printer, item.printer_id)
        if printer is not None:
            printer_name = printer.name
    run = await _load_run(db, batch.id)
    await notification_service.on_run_unit_stopped(item.printer_id, printer_name, run.name, db)
    logger.info(
        "farm_policy: unit %s stopped by operator (%s) on run %s — no retry, no quarantine, run held (active)",
        item.id,
        item.stop_source,
        batch.id,
    )


# --------------------------------------------------------------------------- #
# Retry
# --------------------------------------------------------------------------- #
async def create_retry_if_absent(
    db: AsyncSession, item: PrintQueueItem, *, stage_manual: bool = False
) -> PrintQueueItem | None:
    """Create exactly one retry for a failed ``item`` (idempotent via retry_of_id).

    The retry preserves the ``first_article`` flag (a failed first article is
    re-attempted as a first article) and the plate/profile target. ``stage_manual``
    stages the retry (``manual_start=True``) so it can't dispatch onto a paused run;
    the resume sweep releases it.

    Rebalance (F7): a model-targeted unit's retry returns to the unassigned pool
    (``printer_id=None``) so the scheduler can pick a healthy sibling of the same
    model and recompute its AMS mapping — a printer-pinned unit keeps its pin
    (operator intent; the failing printer's own quarantine→pause→recover path owns
    that). The per-printer plate-clear gate is unaffected either way.
    """
    existing = await db.execute(select(PrintQueueItem.id).where(PrintQueueItem.retry_of_id == item.id))
    if existing.first() is not None:
        return None  # already retried this failure event

    retry_printer_id = None if item.target_model else item.printer_id
    fields = {
        "printer_id": retry_printer_id,
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
        "manual_start": stage_manual,
        "first_article": item.first_article,
        "retry_count": (item.retry_count or 0) + 1,
        "retry_of_id": item.id,
    }
    try:
        # SAVEPOINT (not a bare rollback): the unique-``retry_of_id`` violation of a
        # race loser (R4) is contained to the savepoint, so the outer transaction and
        # the loaded ``batch``/``item`` ORM state survive — the caller then still
        # evaluates quarantine/pause. A full ``db.rollback()`` here would expire those
        # objects and the caller's next attribute access would raise MissingGreenlet.
        # Precedent: services/location_service.py:74-106.
        async with db.begin_nested():
            created = await create_queue_items(db, count=1, printer_id=retry_printer_id, fields=fields)
            await db.flush()
        await db.commit()
    except IntegrityError:
        logger.info("farm_policy: retry for item %s lost the idempotency race (unique retry_of_id)", item.id)
        return None
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


async def recover_printer(db: AsyncSession, printer_id: int) -> dict:
    """One-click operator recovery for a wedged farm printer.

    Collapses the genuine-failure cascade's three manual actions (clear plate →
    clear quarantine → resume run) into one explicit operator override, composing
    the canonical service mutators — no new recovery logic, no dual path. Every
    step is idempotent, so a repeat call is a no-op returning the same shape.

    1. Force-clear the plate-clear gate via the canonical setter, deliberately
       WITHOUT the routine clear-plate route's live-connection / FINISH-FAILED
       guard: recover is an explicit "I've handled the printer" override, gated by
       its own UI confirm, distinct from the everyday empty-bed ack.
    2. Clear any farm quarantine on the printer.
    3. Resume every ``paused`` run that has a queue item on this printer (only
       paused runs — ``transition_run`` 409s otherwise, so filter first).

    Returns ``{"plate_cleared": bool, "quarantine_cleared": bool,
    "runs_resumed": [ids]}`` — the booleans report whether that state was actually
    changed (was set/quarantined before). 404 if the printer is unknown. Each
    per-run resume is wrapped so one failure can't abort the whole recovery.
    """
    # Function-level import avoids a circular import (production_run imports
    # farm_policy helpers), matching the fork's style.
    from backend.app.services.production_run import transition_run

    printer = await db.get(Printer, printer_id)
    if printer is None:
        raise HTTPException(status_code=404, detail="Printer not found")

    # 1. Plate-clear gate — explicit override, no connection/state guard.
    plate_cleared = printer_manager.is_awaiting_plate_clear(printer_id)
    printer_manager.set_awaiting_plate_clear(printer_id, False)

    # 2. Quarantine — idempotent; report whether it was actually set.
    quarantine_cleared = bool(printer.quarantined)
    if quarantine_cleared:
        await clear_quarantine(db, printer_id)

    # 3. Resume runs paused because THIS printer became unavailable (quarantine /
    #    offline). A run paused by a first-article REJECT is deliberately excluded:
    #    reject leaves the rejected part on the plate and resuming re-dispatches a
    #    brand-new first article (transition_run), which would silently undo the
    #    operator's rejection — that run has its own resume affordance on the run
    #    page. Exclude in Python (dialect-safe: first_article_state is NULL for
    #    non-FA runs, which a SQL ``!= 'rejected'`` would wrongly drop).
    result = await db.execute(
        select(PrintBatch.id, PrintBatch.first_article_state)
        .join(PrintQueueItem, PrintQueueItem.batch_id == PrintBatch.id)
        .where(PrintQueueItem.printer_id == printer_id)
        .where(PrintBatch.status == "paused")
        .distinct()
    )
    paused_batch_ids = [bid for bid, fa_state in result.all() if fa_state != "rejected"]
    runs_resumed: list[int] = []
    for batch_id in paused_batch_ids:
        try:
            await transition_run(db, batch_id, "resume")
            runs_resumed.append(batch_id)
        except Exception:  # noqa: BLE001 — one run's failure must not abort recovery
            logger.exception("farm_policy: failed to resume run %s while recovering printer %s", batch_id, printer_id)

    logger.info(
        "farm_policy: recovered printer %s (plate_cleared=%s, quarantine_cleared=%s, runs_resumed=%s)",
        printer_id,
        plate_cleared,
        quarantine_cleared,
        runs_resumed,
    )
    return {
        "plate_cleared": plate_cleared,
        "quarantine_cleared": quarantine_cleared,
        "runs_resumed": runs_resumed,
    }


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

    printers = {p.id: p for p in (await db.execute(select(Printer).where(Printer.id.in_(printer_ids)))).scalars().all()}
    if not all(_printer_unavailable(printers.get(pid)) for pid in printer_ids):
        return

    batch.status = "paused"
    # Machine-readable hold reason (Phase 4.1): the run card must distinguish this
    # auto-pause from a manual one. Cleared on resume (transition_run).
    batch.pause_reason = "no_available_printers"
    await db.commit()
    broadcast_production_run_changed(batch.id)
    run = await _load_run(db, batch.id)
    await notification_service.on_run_paused(
        run.name, _sku_code(run), "All selected printers are quarantined or offline", db
    )
    logger.warning("farm_policy: paused run %s — no available printers", batch.id)


async def _maybe_pause_run_exhausted(db: AsyncSession, batch: PrintBatch) -> None:
    """Pause an active run whose last unit exhausted its retries with no work left (R3).

    Without this, a run whose final plate fails past ``retry_max`` sits ``active``
    forever with nothing pending/printing and no notification. Deliberately NOT
    ``_maybe_complete_run`` — completing would hide the shortfall; pausing surfaces a
    Resume affordance whose ``top_up_run`` mints the replacement plates.

    Guards: only an active run; a run still awaiting first-article approval is
    NORMAL with zero live items (operator-gated), so it's exempt; and if ANY item is
    still pending/printing (a just-created retry, or a duplicate failure event racing
    a live retry) there's work in flight — no pause.
    """
    if batch.status != "active":
        return
    if batch.first_article_state == "awaiting_approval":
        return
    result = await db.execute(
        select(PrintQueueItem.id)
        .where(
            PrintQueueItem.batch_id == batch.id,
            PrintQueueItem.status.in_(("pending", "printing")),
        )
        .limit(1)
    )
    if result.first() is not None:
        return

    batch.status = "paused"
    # Machine-readable hold reason (Phase 1): distinguishes retry-exhaustion from the
    # other auto-pauses on the run card. Cleared on resume (transition_run).
    batch.pause_reason = "retries_exhausted"
    await db.commit()
    broadcast_production_run_changed(batch.id)
    run = await _load_run(db, batch.id)
    await notification_service.on_run_paused(
        run.name,
        _sku_code(run),
        "A unit failed with no retries left and the run has no work in flight — Resume creates replacement plates",
        db,
    )
    logger.warning("farm_policy: paused run %s — retries exhausted, no work in flight", batch.id)


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
    broadcast_production_run_changed(batch.id)
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
    if run.status == "cancelled":
        # An aborted run must never materialise dispatchable plates from an approval
        # (R6: the abort cancelled its pending items; approval would resurrect it).
        raise HTTPException(status_code=409, detail="Cannot approve a first article on a cancelled run")
    # Function-level import avoids a circular import (production_run imports
    # farm_policy helpers), matching the fork's style; _find_fa_item now lives in
    # production_run as the single shared home.
    from backend.app.services.production_run import _find_fa_item

    fa_item = _find_fa_item(run)

    if eject_remotely:
        await _dispatch_remote_eject(db, run, fa_item)
        return await _load_run(db, run_id)

    run.first_article_state = "approved"
    await db.commit()
    broadcast_production_run_changed(run_id)
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
    # Machine-readable hold reason (Phase 4.1); cleared on resume (which
    # re-dispatches a fresh first article).
    run.pause_reason = "first_article_rejected"
    await db.commit()
    broadcast_production_run_changed(run_id)
    run = await _load_run(db, run_id)
    await notification_service.on_run_paused(run.name, _sku_code(run), f"First article rejected: {reason}", db)
    return run


async def _finalize_remote_eject(db: AsyncSession, run_id: int, printer_id: int) -> None:
    run = await db.get(PrintBatch, run_id)
    if run is None or run.first_article_state != "awaiting_approval":
        return
    run.first_article_state = "approved"
    await db.commit()
    broadcast_production_run_changed(run_id)
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
    from backend.app.services.eject.geometry import GeometryUnavailable, get_geometry_required
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
    # Resolve validated eject geometry for the target model (canonical match, via
    # the registry) — fail-closed on a missing/unvalidated row with the accessor's
    # actionable reason.
    try:
        geometry = await get_geometry_required(db, printer.model, require_validated=True)
    except GeometryUnavailable as exc:
        raise HTTPException(status_code=409, detail=exc.reason) from exc

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
            source_path, plate_id, profile, geometry, cooldown_temp_c=run.cooldown_temp_c_override
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
