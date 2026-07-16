"""REST API for farm production runs (Phase 2).

A production run is a :class:`PrintBatch` with ``sku_file_id`` set. Creation,
derived counts, and pause/resume/abort transitions live in
``services.production_run``; this module is the thin HTTP layer.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.print_batch import PrintBatch
from backend.app.models.sku import SkuFile
from backend.app.models.user import User
from backend.app.schemas.production_run import (
    FarmPrinterContext,
    FirstArticleApprove,
    FirstArticleReject,
    RunCreate,
    RunReschedule,
    RunResponse,
)
from backend.app.services.farm_policy import approve_first_article, reject_first_article
from backend.app.services.production_run import (
    build_farm_printer_contexts,
    build_run_response,
    create_production_run,
    delete_production_run,
    reschedule_run,
    transition_run,
)

router = APIRouter(prefix="/production-runs", tags=["production-runs"])


async def _load_run_or_404(db: AsyncSession, run_id: int) -> PrintBatch:
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


@router.post("", response_model=RunResponse, status_code=status.HTTP_201_CREATED)
@router.post("/", response_model=RunResponse, status_code=status.HTTP_201_CREATED)
async def create_run(
    data: RunCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermissionIfAuthEnabled(Permission.PRODUCTION_RUNS_CREATE),
):
    run = await create_production_run(db, data, current_user)
    return await build_run_response(db, run)


@router.get("", response_model=list[RunResponse])
@router.get("/", response_model=list[RunResponse])
async def list_runs(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.PRODUCTION_RUNS_READ),
):
    result = await db.execute(
        select(PrintBatch)
        .where(PrintBatch.sku_file_id.is_not(None))
        .options(
            selectinload(PrintBatch.queue_items),
            selectinload(PrintBatch.sku_file).selectinload(SkuFile.sku),
        )
        .order_by(PrintBatch.created_at.desc())
    )
    runs = result.scalars().all()
    return [await build_run_response(db, run) for run in runs]


@router.get("/printer-states", response_model=list[FarmPrinterContext])
async def get_farm_printer_states(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.PRODUCTION_RUNS_READ),
):
    """Fleet-scoped per-printer farm context for the Printers page (F2).

    One entry per printer assigned to an active/paused run explaining why it is
    doing (or blocked on) farm work — the owning run/SKU plus the printer's
    live/last unit. Registered BEFORE ``/{run_id}`` so the literal path is not
    captured by the int path param.
    """
    return await build_farm_printer_contexts(db)


@router.get("/{run_id}", response_model=RunResponse)
async def get_run(
    run_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.PRODUCTION_RUNS_READ),
):
    """Full run detail: the list payload plus per-printer blocked states and the
    per-unit list (status, stop_source, waiting_reason, retry lineage)."""
    run = await _load_run_or_404(db, run_id)
    return await build_run_response(db, run, detail=True)


@router.delete("/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_run(
    run_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.PRODUCTION_RUNS_DELETE),
):
    """Hard-delete a cancelled/completed run and all its queue items (204).

    Returns 404 for an unknown run and 409 when the run is still active or
    paused (abort it first). Print archives are preserved — see
    ``delete_production_run``.
    """
    await delete_production_run(db, run_id)


@router.post("/{run_id}/pause", response_model=RunResponse)
async def pause_run(
    run_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.PRODUCTION_RUNS_UPDATE),
):
    run = await transition_run(db, run_id, "pause")
    return await build_run_response(db, run)


@router.post("/{run_id}/resume", response_model=RunResponse)
async def resume_run(
    run_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.PRODUCTION_RUNS_UPDATE),
):
    run = await transition_run(db, run_id, "resume")
    return await build_run_response(db, run)


@router.post("/{run_id}/abort", response_model=RunResponse)
async def abort_run(
    run_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.PRODUCTION_RUNS_UPDATE),
):
    run = await transition_run(db, run_id, "abort")
    return await build_run_response(db, run)


@router.post("/{run_id}/reschedule", response_model=RunResponse)
async def reschedule_production_run(
    run_id: int,
    body: RunReschedule,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.PRODUCTION_RUNS_UPDATE),
):
    """Change or clear a not-yet-started run's deferred start time.

    A future ``scheduled_start_at`` re-stamps the run's pending plates; a value
    at/before now, or null, clears the gate (start now). Returns 404 (unknown
    run) and 409 when the run has already started or is not active (resume it
    first if paused, or abort it).
    """
    run = await reschedule_run(db, run_id, body.scheduled_start_at)
    return await build_run_response(db, run)


@router.post("/{run_id}/first-article/approve", response_model=RunResponse)
async def approve_run_first_article(
    run_id: int,
    body: FirstArticleApprove,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.PRODUCTION_RUNS_UPDATE),
):
    """Approve the run's first article.

    ``eject_remotely=false``: the operator physically removed the part — the plate
    gate is cleared, the run is marked ``approved`` and the remaining plates are
    created. ``eject_remotely=true``: a part-present eject job is dispatched and
    the run stays ``awaiting_approval`` until it completes (then finalised
    automatically). Returns 409 when the run is not ``awaiting_approval``; 409/502
    when a remote-eject dispatch cannot be performed.
    """
    run = await approve_first_article(db, run_id, body.eject_remotely)
    return await build_run_response(db, run)


@router.post("/{run_id}/first-article/reject", response_model=RunResponse)
async def reject_run_first_article(
    run_id: int,
    body: FirstArticleReject,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.PRODUCTION_RUNS_UPDATE),
):
    """Reject the run's first article: the run is paused with the given reason.

    The plate gate stays set (the rejected part is still on the plate). To
    continue, resume the run — that re-dispatches a NEW first article. Returns 409
    when the run is not ``awaiting_approval``.
    """
    run = await reject_first_article(db, run_id, body.reason)
    return await build_run_response(db, run)
