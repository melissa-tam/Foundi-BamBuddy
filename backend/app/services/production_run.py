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
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from backend.app.core.websocket import broadcast_production_run_changed
from backend.app.models.archive import PrintArchive
from backend.app.models.eject_profile import EjectProfile
from backend.app.models.library import LibraryFile
from backend.app.models.print_batch import PrintBatch
from backend.app.models.printer import Printer
from backend.app.models.sku import SkuFile
from backend.app.services.capability_gate import (
    evaluate_capability,
    extract_file_capabilities,
    loaded_filament_types,
    read_live_nozzles,
)
from backend.app.services.eject.geometry import list_geometries
from backend.app.services.farm_correlation import WAITING_REASON_PLATE_VISION
from backend.app.services.farm_policy import (
    _sku_code,
    build_first_article_plan,
    create_new_first_article,
    farm_policy_defaults,
)
from backend.app.services.farm_staging import release_filament_staged
from backend.app.services.filament_deficit import compute_deficit_for_queue_item
from backend.app.services.notification_service import notification_service
from backend.app.services.printer_manager import printer_manager
from backend.app.services.queue_builder import create_queue_items
from backend.app.services.sku_catalog import median_cycle_seconds
from backend.app.utils.printer_models import (
    is_dual_nozzle_model,
    normalize_printer_model,
    normalize_printer_model_id,
)

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


def _now_naive() -> datetime:
    """Current UTC time as a naive datetime, matching the ``scheduled_time`` column
    convention (stored naive-UTC; the scheduler coerces a naive value to UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_utc(dt: datetime | None) -> datetime | None:
    """Normalise an inbound datetime to naive-UTC for storage/comparison.

    Pydantic parses an ISO string with a ``Z``/offset into an *aware* datetime,
    while ``PrintQueueItem.scheduled_time`` stores naive-UTC (the scheduler treats
    a naive value as UTC — ``print_scheduler`` coerces it). Coerce aware→UTC-naive;
    assume a naive value is already UTC. None passes through unchanged (= ASAP).
    """
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def resolve_scheduled_start(dt: datetime | None) -> datetime | None:
    """Normalise an operator-supplied start time to a stored value.

    A future time (naive-UTC) is kept; ``None`` or a time at/​before now collapses
    to ``None`` = start ASAP (no confusing 422 for a near-now race). Shared by run
    creation and rescheduling so the future-vs-immediate rule has one home.
    """
    value = _as_utc(dt)
    if value is not None and value > _now_naive():
        return value
    return None


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


def _find_fa_item(run: PrintBatch) -> PrintQueueItem | None:
    """The most-recently-completed first-article item for the run, if any.

    Single implementation shared by ``build_run_response`` (below) and the
    first-article approve/reject policy in ``farm_policy`` (which imports it
    function-locally to avoid the production_run↔farm_policy import cycle).
    """
    candidates = [i for i in run.queue_items if i.first_article and i.status == "completed"]
    if not candidates:
        candidates = [i for i in run.queue_items if i.first_article]
    if not candidates:
        return None
    return sorted(candidates, key=lambda i: i.completed_at or i.created_at or 0)[-1]


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
    require_fa = data.require_first_article if data.require_first_article is not None else False

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
    # One-time deferred start (Phase 5): stamp the operator's chosen start onto
    # every plate item's scheduled_time so the existing scheduler gate holds
    # dispatch until then (non-blocking; None => ASAP). ``plate_fields`` — the
    # FA-plan template passed to build_first_article_plan below — deliberately
    # OMITS it, so plates materialised AFTER first-article approval (necessarily
    # after the start time) dispatch ASAP.
    scheduled_start = resolve_scheduled_start(data.scheduled_start_at)
    base_fields = {
        **plate_fields,
        "batch_id": batch.id,
        "status": "pending",
        "scheduled_time": scheduled_start,
    }

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

    # Wake the scheduler so the run's fresh queue items dispatch immediately
    # (latency Phase A). Run spans printers → no single printer_id. Guarded.
    try:
        from backend.app.services.dispatch_kick import dispatch_kick

        dispatch_kick.kick("run_create")
    except Exception:
        logger.debug("dispatch kick failed after run create (non-fatal)", exc_info=True)

    return await _load_run(db, batch.id)


# Farm waiting-reason machine codes surfaced as per-printer flags. This is the
# ONE place the vocabulary is mapped — both the run-detail printer states and the
# fleet-scoped printer contexts derive from ``_derive_printer_unit_context`` so no
# parallel mapping (and no new reason codes) can drift in. The vision token is
# imported from its single origin in farm_correlation (no local duplicate).
_WAIT_STALLED = "printer_offline_stalled"


def _derive_printer_unit_context(printer_id: int, items: list[PrintQueueItem]) -> dict:
    """Resolve a printer's representative farm unit from a run's items (Phase 3).

    Shared by ``_build_printer_states`` (run detail) and
    ``build_farm_printer_contexts`` (Printers page) so the unit/waiting-reason
    vocabulary has a single home — no parallel mapping, no invented reason codes.

    Picks the printer's representative unit — a live PRINTING unit, else a live
    PENDING one — and surfaces its ``waiting_reason`` machine code plus the
    ``staged`` / ``filament_short`` / ``first_article`` flags. When the printer
    has no live unit, the most recent FAILED unit's ``error_message`` explains the
    last failure so an idle printer can still say what went wrong.

    ``rank`` orders which run "owns" a printer when it appears in several
    (printing 3 > pending 2 > failed 1 > none 0): a live unit wins; the caller's
    most-recent-first iteration breaks ties toward the newest run.
    """
    on_printer = [it for it in items if it.printer_id == printer_id]
    printing = [it for it in on_printer if it.status == "printing"]
    pending = [it for it in on_printer if it.status == "pending"]
    live = None
    if printing:
        live = min(printing, key=lambda i: (i.position, i.id))
    elif pending:
        live = min(pending, key=lambda i: (i.position, i.id))

    if live is not None:
        is_pending = live.status == "pending"
        return {
            "unit_id": live.id,
            "unit_status": live.status,  # 'printing' or 'pending'
            "waiting_reason": live.waiting_reason,
            "error_message": None,
            "staged": bool(live.manual_start) and is_pending,
            "filament_short": bool(live.filament_short) and is_pending,
            "first_article": bool(live.first_article),
            "rank": 2 if is_pending else 3,
        }

    failed = [it for it in on_printer if it.status == "failed"]
    if failed:
        # Highest id ≈ most recent failure (retries are minted after the original
        # and carry a higher id) — avoids naive/aware datetime comparison on
        # ``completed_at``.
        last = max(failed, key=lambda i: i.id)
        return {
            "unit_id": last.id,
            "unit_status": "failed",
            "waiting_reason": None,
            "error_message": last.error_message,
            "staged": False,
            "filament_short": False,
            "first_article": bool(last.first_article),
            "rank": 1,
        }

    return {
        "unit_id": None,
        "unit_status": None,
        "waiting_reason": None,
        "error_message": None,
        "staged": False,
        "filament_short": False,
        "first_article": False,
        "rank": 0,
    }


def _build_printer_states(printer_rows: list[Printer], items: list[PrintQueueItem]) -> tuple[list[dict], bool]:
    """Derive the per-printer blocked-state entries for a run (Phase 4.1).

    Everything here is DERIVED per 3NF — DB flags (quarantine) plus the live
    ``printer_manager`` flags the scheduler actually gates on (plate-clear gate,
    model mismatch, connectivity; model_mismatch has no DB column at all) plus
    the representative unit's waiting_reason machine codes (offline stall,
    printer-vision hold) resolved by the shared ``_derive_printer_unit_context``.
    Returns ``(states, any_blocked)``. Mirrors ``farm_policy._printer_unavailable``
    for the never-connected case: a printer with no live status yet is *unknown*,
    not offline, so tests/startup don't spuriously report every printer blocked.
    """
    states: list[dict] = []
    any_blocked = False
    for p in printer_rows:
        connected = printer_manager.is_connected(p.id)
        waiting_reason = _derive_printer_unit_context(p.id, items)["waiting_reason"]
        state = {
            "printer_id": p.id,
            "name": p.name,
            "connected": connected,
            "quarantined": bool(p.quarantined),
            "awaiting_plate_clear": printer_manager.is_awaiting_plate_clear(p.id),
            "model_mismatch": printer_manager.is_model_mismatch(p.id),
            "model_mismatch_reason": printer_manager.model_mismatch_reason(p.id),
            "stalled": waiting_reason == _WAIT_STALLED,
            "vision_hold": waiting_reason == WAITING_REASON_PLATE_VISION,
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


async def _build_printer_eligibility(
    db: AsyncSession,
    states: list[dict],
    printer_rows: list[Printer],
    items: list[PrintQueueItem],
) -> None:
    """Merge the three live dispatch-eligibility dimensions into each state dict.

    Detail-only companion to :func:`_build_printer_states` (kept sync/live-flag-
    only because it is reused by the Printers page): mutates each entry in
    ``states`` IN PLACE, adding ``filament_short_live`` (+ ``filament_short_detail``
    grams), ``no_usb_drive`` and ``capability_reason`` so the run-detail
    eligibility panel can say WHY a printer won't take the next plate.

    Computed only against the run's PENDING work — a representative pending item
    (lowest position/id) drives a per-candidate deficit check
    (``compute_deficit_for_queue_item`` with the ``printer_id_override`` /
    ``ams_mapping_override`` params, so the item is never mutated) and the pure
    capability gate. With no pending item there is nothing left to dispatch, so
    every new flag stays at its default. File capabilities + the validated-model
    set are parsed/queried ONCE and reused across the (≤10) printers.

    Each per-printer computation is wrapped fail-safe (mirrors
    ``farm_staging.release_filament_staged``): an exception on one dimension
    leaves that printer's flag at its default and is logged — the detail endpoint
    must never 500 on a malformed 3MF or a missing live status.
    """
    # Seed defaults so every state carries the full shape even on early return.
    for st in states:
        st.setdefault("filament_short_live", False)
        st.setdefault("filament_short_detail", None)
        st.setdefault("no_usb_drive", False)
        st.setdefault("capability_reason", None)

    pending = [it for it in items if it.status == "pending"]
    if not pending:
        return  # nothing left to dispatch — all flags stay at defaults
    rep = min(pending, key=lambda i: (i.position, i.id))

    # File capability facts + the validated-geometry model set: parsed/queried
    # once, reused across all candidate printers.
    file_meta = None
    lib_id = getattr(rep, "library_file_id", None)
    if lib_id is not None:
        lib = await db.get(LibraryFile, lib_id)
        if lib is not None:
            file_meta = lib.file_metadata
    file_caps = extract_file_capabilities(file_meta, getattr(rep, "plate_id", None))
    validated_models = {g.model_key for g in await list_geometries(db) if g.validated}

    # Function-local singleton import (matches settings.py): the scheduler is a
    # heavy module and this keeps production_run import-cheap / cycle-free.
    from backend.app.services.print_scheduler import scheduler as print_scheduler

    by_id = {p.id: p for p in printer_rows}
    for st in states:
        printer = by_id.get(st["printer_id"])
        if printer is None:
            continue
        status = printer_manager.get_status(printer.id)

        # 1. Filament deficit against THIS candidate printer (no item mutation).
        try:
            outcome = await print_scheduler._compute_ams_mapping_for_printer(db, printer.id, rep)
            mapping = outcome.mapping
            mapping_override = json.dumps(mapping) if mapping is not None else None
            deficit = await compute_deficit_for_queue_item(
                db, rep, printer_id_override=printer.id, ams_mapping_override=mapping_override
            )
            if deficit:
                needs = sum(d.required_grams for d in deficit)
                available = sum(d.remaining_grams for d in deficit if d.remaining_grams is not None)
                st["filament_short_live"] = True
                st["filament_short_detail"] = f"needs {needs:.0f} g, {available:.0f} g available"
        except Exception as e:  # noqa: BLE001 — malformed 3MF / stale spool data: default, don't 500
            logger.warning("eligibility: filament deficit check failed for printer %s: %s", printer.id, e)

        # 2. USB drive: ONLY an explicit live "absent" holds (fail-open on
        # unknown/offline, mirroring the dispatch pre-flight, scheduler :2384).
        try:
            if status is not None and getattr(status, "sdcard", None) is False:
                st["no_usb_drive"] = True
        except Exception as e:  # noqa: BLE001
            logger.warning("eligibility: usb check failed for printer %s: %s", printer.id, e)

        # 3. Capability gate (pure): geometry / sliced-model / nozzle / filament.
        # Only a BLOCK (ok False) surfaces a reason; a non-fatal warn is not a
        # block, so it stays None.
        try:
            decision = evaluate_capability(
                file_caps=file_caps,
                printer_model=printer.model,
                live_nozzles=read_live_nozzles(status),
                loaded_filament_types=loaded_filament_types(status),
                bed_dims_models=validated_models,
                printer_is_dual=is_dual_nozzle_model(printer.model),
            )
            if not decision.ok:
                st["capability_reason"] = decision.reason
        except Exception as e:  # noqa: BLE001
            logger.warning("eligibility: capability check failed for printer %s: %s", printer.id, e)


async def build_farm_printer_contexts(db: AsyncSession) -> list[dict]:
    """Fleet-scoped "why is this printer doing (or not doing) farm work" contexts.

    Answers finding F2 (Phase 3): the Printers page can explain a printer sitting
    idle on a blocked farm unit without opening the run detail. ONE query over the
    active/paused farm runs (a run is a ``PrintBatch`` with ``sku_file_id`` set),
    with queue items and the SKU eager-loaded, resolves per assigned printer: the
    owning run + SKU, and the printer's live/last unit via the shared
    ``_derive_printer_unit_context`` (same reason vocabulary as the run-detail
    printer states — no parallel mapping). A printer appearing in several active
    runs is attributed to the run where it has a live unit (printing before
    pending), else its most recent run. Returns one dict per assigned printer,
    sorted by printer id — the ``FarmPrinterContext`` shape.
    """
    result = await db.execute(
        select(PrintBatch)
        .where(PrintBatch.sku_file_id.is_not(None), PrintBatch.status.in_(("active", "paused")))
        .options(
            selectinload(PrintBatch.queue_items),
            selectinload(PrintBatch.sku_file).selectinload(SkuFile.sku),
        )
        .order_by(PrintBatch.created_at.desc())
    )
    runs = result.scalars().all()

    best: dict[int, dict] = {}
    for run in runs:  # most recent first (created_at desc)
        items = list(run.queue_items)
        sku_code = run.sku_file.sku.code if (run.sku_file and run.sku_file.sku) else None
        for pid in {it.printer_id for it in items if it.printer_id is not None}:
            unit_ctx = _derive_printer_unit_context(pid, items)
            existing = best.get(pid)
            # Most-recent-first: keep the newest run on a tie; a strictly higher
            # rank (a live unit) from an older run takes the printer.
            if existing is not None and unit_ctx["rank"] <= existing["_rank"]:
                continue
            best[pid] = {
                "printer_id": pid,
                "run_id": run.id,
                "run_name": run.name,
                "sku_code": sku_code,
                "run_status": run.status,
                "pause_reason": run.pause_reason,
                "unit_id": unit_ctx["unit_id"],
                "unit_status": unit_ctx["unit_status"],
                "waiting_reason": unit_ctx["waiting_reason"],
                "error_message": unit_ctx["error_message"],
                "staged": unit_ctx["staged"],
                "filament_short": unit_ctx["filament_short"],
                "first_article": unit_ctx["first_article"],
                "first_article_state": run.first_article_state,
                "_rank": unit_ctx["rank"],
            }

    contexts = sorted(best.values(), key=lambda c: c["printer_id"])
    for c in contexts:
        del c["_rank"]
    return contexts


def planned_plate_count(quantity: int | None, items: Iterable[PrintQueueItem]) -> int:
    """Planned plate count for a run — the ``plates_total`` figure the run-detail
    API reports.

    SINGLE SOURCE OF TRUTH for "planned" (reused by ``build_run_response`` and by
    ``farm_policy._maybe_complete_run``). ``quantity`` is the plan the run was
    created with; a run whose primary plates (``retry_count`` 0, retries don't
    consume a plan slot) outran the plan — e.g. plates added after creation —
    counts those instead. Never counts retries toward the plan.
    """
    non_retry_created = sum(1 for it in items if (it.retry_count or 0) == 0)
    return max(quantity or 0, non_retry_created)


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
    planned_plates = planned_plate_count(run.quantity, items)
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
    name_by_id = {p.id: p.name for p in printer_rows}

    # Candidate printers for the per-printer blocked-state / eligibility panel.
    # The pinned printers always count; for a model-targeted run whose staged
    # items are UNPINNED (printer_id NULL, target_model set) there are NO pinned
    # printers, so a run-detail panel built from ``printer_rows`` alone would be
    # empty exactly when the operator needs the feedback. Union in every ACTIVE
    # printer of the run's target model(s) — quarantined ones kept (they surface
    # AS blocked). Explicit-printer runs carry no item ``target_model`` and are
    # unchanged. The assigned-printer list (``printers``) + ETA stay pinned-only.
    state_rows: list[Printer] = printer_rows
    target_models = {normalize_printer_model(it.target_model) or it.target_model for it in items if it.target_model}
    if target_models:
        lowered = {m.lower() for m in target_models if m}
        model_result = await db.execute(
            select(Printer).where(func.lower(Printer.model).in_(lowered), Printer.is_active == True)  # noqa: E712
        )
        merged = {p.id: p for p in printer_rows}
        merged.update({p.id: p for p in model_result.scalars().all()})
        state_rows = sorted(merged.values(), key=lambda p: p.id)

    printer_states, has_blocked_printers = _build_printer_states(state_rows, items)

    # Live dispatch-eligibility (deficit / USB / capability) is detail-only — it
    # parses the 3MF and runs per-printer DB deficit queries, so the list
    # endpoint stays lean. Merge it into the states and fold the three new
    # dimensions into the single ``has_blocked_printers`` summary.
    if detail:
        await _build_printer_eligibility(db, printer_states, state_rows, items)
        has_blocked_printers = has_blocked_printers or any(
            st["filament_short_live"] or st["no_usb_drive"] or st["capability_reason"] is not None
            for st in printer_states
        )

    # First-article inspection payload (Phase 4, F1): only while the run is
    # awaiting approval or after a reject does the operator need the finished
    # part's photo + the printer to view its camera. Fetch the FA item's archive
    # ONLY in those states (cheap on the common path). The photo URL is relative
    # on purpose — same-origin session/stream-token auth; the frontend degrades
    # gracefully (photoUnavailable) if the photo was pruned or is forbidden.
    first_article_photo_url: str | None = None
    first_article_printer_id: int | None = None
    first_article_printer_name: str | None = None
    if run.first_article_state in ("awaiting_approval", "rejected"):
        fa_item = _find_fa_item(run)
        if fa_item is not None:
            first_article_printer_id = fa_item.printer_id
            if fa_item.printer_id is not None:
                first_article_printer_name = name_by_id.get(fa_item.printer_id)
            if fa_item.archive_id is not None:
                archive = await db.get(PrintArchive, fa_item.archive_id)
                if archive is not None and archive.photos:
                    # Mirror main.py's finish-photo selection: the capture path
                    # appends the freshly-taken ``finish_*`` photo last, so the
                    # newest finish photo is the last such entry.
                    finish_photos = [p for p in archive.photos if isinstance(p, str) and p.startswith("finish_")]
                    if finish_photos:
                        first_article_photo_url = f"/api/v1/archives/{fa_item.archive_id}/photos/{finish_photos[-1]}"

    # Prefill values for "Run again" (Phase 5, F9): the eject profile and target
    # model are uniform across a run's items — take the first non-null; the
    # cooldown override is a batch-level column. Surfaced on BOTH the list and
    # detail responses so a terminal run card can reopen the dialog pre-filled.
    prefill_eject_profile_id = next((it.eject_profile_id for it in items if it.eject_profile_id is not None), None)
    prefill_target_model = next((it.target_model for it in items if it.target_model), None)

    # ETA: median cycle × remaining plates ÷ distinct printers. Null when we
    # lack a cycle estimate or have no printer to run the remainder.
    rows = [{"printer_id": it.printer_id, "started_at": it.started_at} for it in items]
    median_cycle = median_cycle_seconds(rows)
    remaining_plates = plates_pending + plates_printing
    distinct_printers = len(printers)
    eta_seconds: float | None = None
    if median_cycle is not None and distinct_printers > 0 and remaining_plates > 0:
        eta_seconds = median_cycle * remaining_plates / distinct_printers

    # Derived run-level scheduled start (Phase 5): the earliest not-yet-started
    # plate's scheduled_time. STORED on the items (scheduled_time), never on the
    # batch — the run-level view is derived like every other count here. Null once
    # the run has started (its remaining pending plates carry a past time or none).
    scheduled_start_at = min(
        (it.scheduled_time for it in items if it.status == "pending" and it.scheduled_time is not None),
        default=None,
    )

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
        "first_article_photo_url": first_article_photo_url,
        "first_article_printer_id": first_article_printer_id,
        "first_article_printer_name": first_article_printer_name,
        "retry_max_per_unit": run.retry_max_per_unit,
        "escalate_consecutive_failures": run.escalate_consecutive_failures,
        "eject_profile_id": prefill_eject_profile_id,
        "cooldown_temp_c_override": run.cooldown_temp_c_override,
        "target_model": prefill_target_model,
        "eta_seconds": eta_seconds,
        "printers": printers,
        "scheduled_start_at": scheduled_start_at,
        "created_at": run.created_at,
    }
    if detail:
        response["printer_states"] = printer_states
        response["units"] = [
            {
                "id": it.id,
                "status": it.status,
                "stop_source": it.stop_source,
                "waiting_reason": it.waiting_reason,
                "scheduled_time": it.scheduled_time,
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
            # Terminal-transition hygiene (W4b): a run-abort cancels PENDING items
            # directly (they never flow through farm_policy.on_terminal), so any
            # scheduler hold token (filament_short, capability block, stagger_hold…)
            # must be cleared here or it survives on a cancelled row forever.
            item.waiting_reason = None

    run.status = {"pause": "paused", "resume": "active", "abort": "cancelled"}[action]
    if action == "resume":
        # Resuming clears the hold reason and tops the run back up to plan (Phase
        # 3.1): replacements for units consumed without output (operator stops,
        # exhausted failures). Idempotent — recomputed from live counts.
        run.pause_reason = None
        await db.commit()
        broadcast_production_run_changed(run_id)
        run = await _load_run(db, run_id)
        # FA-zombie guard (Phase 1): a gated run whose entire first-article chain
        # died at max retries paused with its remaining plates still deferred to the
        # plan. Resuming must re-dispatch a fresh first article from that plan, else
        # the run goes 'active' with zero live items and never progresses (top_up is
        # a deliberate no-op while gated). The plan is left intact for approval.
        if (
            run.first_article_state == "pending_print"
            and run.first_article_plan
            and not any(it.first_article and it.status in ("pending", "printing") for it in run.queue_items)
        ):
            await create_new_first_article(db, run)
            await db.commit()
            run = await _load_run(db, run_id)
        topped_up = await top_up_run(db, run)
        # Lifecycle notification (Phase 6): tell the other operator the run is
        # progressing again. Reload first so sku_file/sku are fresh, then read the
        # identity args BEFORE on_run_resumed's internal commit expires the row
        # (same ordering the farm_policy notify sites rely on).
        run = await _load_run(db, run_id)
        await notification_service.on_run_resumed(run.name, _sku_code(run), topped_up, db)

        # Wake the scheduler so resumed / topped-up items dispatch immediately
        # (latency Phase A). Guarded — never let a kick failure break resume.
        try:
            from backend.app.services.dispatch_kick import dispatch_kick

            dispatch_kick.kick("run_resume")
        except Exception:
            logger.debug("dispatch kick failed after run resume (non-fatal)", exc_info=True)

        return await _load_run(db, run_id)
    else:
        if action == "pause":
            # Machine-readable hold reason (Phase 4.1): a manual pause is
            # distinguishable from the auto-pauses. Cleared on resume above.
            run.pause_reason = "operator"
        await db.commit()
        broadcast_production_run_changed(run_id)
        # Lifecycle notifications (Phase 6): a manual pause reuses the existing
        # on_run_paused event; an abort fires the destructive on_run_aborted so the
        # other operator knows the run is over. Reload for fresh sku_file/sku.
        run = await _load_run(db, run_id)
        if action == "pause":
            await notification_service.on_run_paused(run.name, _sku_code(run), "Paused by operator", db)
        elif action == "abort":
            await notification_service.on_run_aborted(run.name, _sku_code(run), db)
        return await _load_run(db, run_id)


async def reschedule_run(db: AsyncSession, run_id: int, scheduled_start_at: datetime | None) -> PrintBatch:
    """Change (or clear) a not-yet-started run's deferred start time (Phase 5).

    Unifies **Start now** (``scheduled_start_at`` None/past → clear the gate) and
    **Reschedule** (a future time → re-stamp). The start time is STORED on the
    run's pending plate items (``scheduled_time``), not the batch, so this is a
    single pass over those items; the run-level ``scheduled_start_at`` in the
    response is derived from them.

    Guard: a run is reschedulable only while it is ``active`` and NO item has
    started (``started_at`` None on every item) — reschedulable right up until it
    actually begins. This is race-free against a just-passed clock and unambiguous
    versus a paused run (409 → resume/abort instead). Raises 404 (unknown run) and
    409 (already started / not active) as HTTPExceptions.
    """
    run = await _load_run(db, run_id)
    if run.status != "active" or any(it.started_at is not None for it in run.queue_items):
        raise HTTPException(
            status_code=409,
            detail="Only a run that hasn't started yet can be rescheduled; resume it first if paused, or abort it.",
        )

    value = resolve_scheduled_start(scheduled_start_at)
    for item in run.queue_items:
        if item.status == "pending":
            item.scheduled_time = value
    await db.commit()
    broadcast_production_run_changed(run_id)
    logger.info(
        "production_run: rescheduled run %s to %s",
        run_id,
        value.isoformat() if value else "ASAP (start now)",
    )
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
