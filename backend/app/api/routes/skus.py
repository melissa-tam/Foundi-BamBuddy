"""REST API for the SKU catalog (Phase 2).

CRUD over SKUs, file links (SKU ↔ library_file+plate), an auto-suggest endpoint
that parses a code/part-number/name from a file, and a derived lifetime-stats
endpoint. Capability facts on file responses are read live from the library
file's metadata — never stored on ``sku_files``.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.api.routes.library import _resolve_source_disk_path
from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.library import LibraryFile
from backend.app.models.print_batch import PrintBatch
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.sku import Sku, SkuFile
from backend.app.models.user import User
from backend.app.schemas.sku import (
    SkuCreate,
    SkuFileCreate,
    SkuFileResponse,
    SkuResponse,
    SkuStatsResponse,
    SkuSuggestResponse,
    SkuUpdate,
)
from backend.app.services.sku_catalog import (
    compute_stats_from_rows,
    parse_sku_suggestion,
    resolve_file_capabilities,
)

router = APIRouter(prefix="/skus", tags=["skus"])

_DUPLICATE_CODE = "A SKU with that code already exists"


def _build_sku_file_response(sku_file: SkuFile) -> SkuFileResponse:
    """Serialize a SkuFile, resolving capability facts live from its library file."""
    lib = sku_file.library_file
    source_path = _resolve_source_disk_path(lib) if lib else None
    caps = resolve_file_capabilities(
        lib.file_metadata if lib else None,
        source_path,
        sku_file.plate_index,
    )
    return SkuFileResponse(
        id=sku_file.id,
        sku_id=sku_file.sku_id,
        library_file_id=sku_file.library_file_id,
        library_file_name=lib.filename if lib else None,
        plate_index=sku_file.plate_index,
        units_per_plate=sku_file.units_per_plate,
        **caps,
    )


async def _compute_sku_stats(db: AsyncSession, sku_id: int) -> SkuStatsResponse:
    """Derive lifetime stats from queue items of runs linked to this SKU's files."""
    result = await db.execute(
        select(
            PrintQueueItem.status,
            SkuFile.units_per_plate,
            PrintQueueItem.printer_id,
            PrintQueueItem.started_at,
        )
        .join(PrintBatch, PrintQueueItem.batch_id == PrintBatch.id)
        .join(SkuFile, PrintBatch.sku_file_id == SkuFile.id)
        .where(SkuFile.sku_id == sku_id)
    )
    rows = [{"status": r[0], "units_per_plate": r[1], "printer_id": r[2], "started_at": r[3]} for r in result.all()]
    return SkuStatsResponse(**compute_stats_from_rows(rows))


async def _serialize_sku(db: AsyncSession, sku: Sku) -> SkuResponse:
    files = [_build_sku_file_response(f) for f in sku.files]
    stats = await _compute_sku_stats(db, sku.id)
    return SkuResponse(
        id=sku.id,
        code=sku.code,
        name=sku.name,
        part_number=sku.part_number,
        notes=sku.notes,
        default_eject_profile_id=sku.default_eject_profile_id,
        files=files,
        stats=stats,
        created_at=sku.created_at,
        updated_at=sku.updated_at,
    )


async def _get_sku_or_404(db: AsyncSession, sku_id: int) -> Sku:
    result = await db.execute(
        select(Sku).where(Sku.id == sku_id).options(selectinload(Sku.files).selectinload(SkuFile.library_file))
    )
    sku = result.scalar_one_or_none()
    if sku is None:
        raise HTTPException(status_code=404, detail="SKU not found")
    return sku


async def _null_referencing_runs(db: AsyncSession, sku_file_ids: list[int]) -> None:
    """Detach any production run referencing these SKU files (FK SET NULL).

    SQLite runs with foreign-key enforcement off, so the declared ON DELETE SET
    NULL never fires — do it explicitly to keep runs/history intact.
    """
    if not sku_file_ids:
        return
    await db.execute(update(PrintBatch).where(PrintBatch.sku_file_id.in_(sku_file_ids)).values(sku_file_id=None))


@router.get("", response_model=list[SkuResponse])
@router.get("/", response_model=list[SkuResponse])
async def list_skus(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SKUS_READ),
):
    result = await db.execute(
        select(Sku).options(selectinload(Sku.files).selectinload(SkuFile.library_file)).order_by(Sku.code)
    )
    skus = result.scalars().all()
    return [await _serialize_sku(db, sku) for sku in skus]


@router.post("", response_model=SkuResponse, status_code=status.HTTP_201_CREATED)
@router.post("/", response_model=SkuResponse, status_code=status.HTTP_201_CREATED)
async def create_sku(
    data: SkuCreate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SKUS_CREATE),
):
    existing = await db.execute(select(Sku.id).where(Sku.code == data.code))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail=_DUPLICATE_CODE)
    sku = Sku(**data.model_dump())
    db.add(sku)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=_DUPLICATE_CODE) from exc
    return await _serialize_sku(db, await _get_sku_or_404(db, sku.id))


@router.get("/suggest", response_model=SkuSuggestResponse)
async def suggest_sku(
    library_file_id: int = Query(...),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SKUS_READ),
):
    result = await db.execute(LibraryFile.active().where(LibraryFile.id == library_file_id))
    lib = result.scalar_one_or_none()
    if lib is None:
        raise HTTPException(status_code=404, detail="Library file not found")

    object_names: list[str] = []
    meta = lib.file_metadata or {}
    printable = meta.get("printable_objects")
    if isinstance(printable, dict):
        object_names = [v for v in printable.values() if isinstance(v, str)]

    # Fall back to parsing slice_info for object names if metadata lacked them.
    if not object_names:
        source_path = _resolve_source_disk_path(lib)
        if source_path and Path(source_path).exists():
            from backend.app.services.archive import ThreeMFParser

            try:
                parsed = ThreeMFParser(Path(source_path)).parse()
            except Exception:
                parsed = {}
            parsed_objs = parsed.get("printable_objects")
            if isinstance(parsed_objs, dict):
                object_names = [v for v in parsed_objs.values() if isinstance(v, str)]

    suggestion = parse_sku_suggestion(object_names, lib.filename)
    return SkuSuggestResponse(**suggestion)


@router.get("/{sku_id}", response_model=SkuResponse)
async def get_sku(
    sku_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SKUS_READ),
):
    return await _serialize_sku(db, await _get_sku_or_404(db, sku_id))


@router.put("/{sku_id}", response_model=SkuResponse)
async def update_sku(
    sku_id: int,
    data: SkuUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SKUS_UPDATE),
):
    sku = await _get_sku_or_404(db, sku_id)
    updates = data.model_dump(exclude_unset=True)
    if "code" in updates and updates["code"] != sku.code:
        clash = await db.execute(select(Sku.id).where(Sku.code == updates["code"]))
        if clash.scalar_one_or_none() is not None:
            raise HTTPException(status_code=409, detail=_DUPLICATE_CODE)
    for key, value in updates.items():
        setattr(sku, key, value)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=_DUPLICATE_CODE) from exc
    return await _serialize_sku(db, await _get_sku_or_404(db, sku_id))


@router.delete("/{sku_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sku(
    sku_id: int,
    force: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SKUS_DELETE),
):
    sku = await _get_sku_or_404(db, sku_id)
    file_ids = [f.id for f in sku.files]

    # Refuse if any production run references this SKU's files, unless forced.
    if file_ids:
        refs = await db.execute(select(PrintBatch.id).where(PrintBatch.sku_file_id.in_(file_ids)).limit(1))
        if refs.scalar_one_or_none() is not None:
            if not force:
                raise HTTPException(
                    status_code=409,
                    detail="SKU has production runs referencing its files; pass ?force=true to detach and delete",
                )
            await _null_referencing_runs(db, file_ids)

    await db.delete(sku)  # cascade deletes the SkuFile links (ORM delete-orphan)
    await db.commit()


@router.post("/{sku_id}/files", response_model=SkuFileResponse, status_code=status.HTTP_201_CREATED)
async def add_sku_file(
    sku_id: int,
    data: SkuFileCreate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SKUS_UPDATE),
):
    sku = await _get_sku_or_404(db, sku_id)

    lib_result = await db.execute(LibraryFile.active().where(LibraryFile.id == data.library_file_id))
    lib = lib_result.scalar_one_or_none()
    if lib is None:
        raise HTTPException(status_code=404, detail="Library file not found")

    # 422 if the requested plate has no sliced G-code in the 3MF. STRICT match
    # on `plate_{index}.gcode` — do NOT use _find_target_gcode_name here, whose
    # any-gcode fallback is dispatch-time leniency; a catalog link must pin an
    # actually-sliced plate (a multi-plate project may have only one sliced).
    source_path = _resolve_source_disk_path(lib)
    if not source_path or not Path(source_path).exists():
        raise HTTPException(status_code=422, detail="Library file not found on disk")
    import zipfile

    plate_member_suffix = f"plate_{data.plate_index}.gcode"
    try:
        with zipfile.ZipFile(source_path, "r") as zf:
            has_gcode = any(name.endswith(plate_member_suffix) for name in zf.namelist())
    except (OSError, zipfile.BadZipFile) as exc:
        raise HTTPException(status_code=422, detail="Library file is not a readable 3MF") from exc
    if not has_gcode:
        raise HTTPException(status_code=422, detail=f"Plate {data.plate_index} has no G-code in the 3MF")

    link = SkuFile(
        sku_id=sku.id,
        library_file_id=data.library_file_id,
        plate_index=data.plate_index,
        units_per_plate=data.units_per_plate,
    )
    db.add(link)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="That library file + plate is already linked to this SKU",
        ) from exc

    result = await db.execute(select(SkuFile).where(SkuFile.id == link.id).options(selectinload(SkuFile.library_file)))
    return _build_sku_file_response(result.scalar_one())


@router.delete("/{sku_id}/files/{file_link_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sku_file(
    sku_id: int,
    file_link_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SKUS_UPDATE),
):
    result = await db.execute(select(SkuFile).where(SkuFile.id == file_link_id, SkuFile.sku_id == sku_id))
    link = result.scalar_one_or_none()
    if link is None:
        raise HTTPException(status_code=404, detail="SKU file link not found")
    # Detach any run pointing at this link (FK SET NULL — explicit for SQLite).
    await _null_referencing_runs(db, [link.id])
    await db.delete(link)
    await db.commit()


@router.get("/{sku_id}/stats", response_model=SkuStatsResponse)
async def get_sku_stats(
    sku_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SKUS_READ),
):
    await _get_sku_or_404(db, sku_id)
    return await _compute_sku_stats(db, sku_id)
