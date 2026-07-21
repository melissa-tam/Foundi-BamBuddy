"""REST API for eject profiles + the hardware-ladder preview / dry-run endpoints.

- CRUD over :class:`EjectProfile`.
- ``/{id}/preview`` — generate + validate an eject block against a real library
  3MF, returning the G-code and validation report (no file produced).
- ``/{id}/dry-run`` — download a copy of the source 3MF whose plate G-code is
  replaced by the motion-only eject block (hardware-ladder step 1: run on an
  EMPTY bed). The eject block is motion-only now (the bed-cooldown wait lives in
  the eject monitor, not the G-code), so the dry run validates sweep GEOMETRY.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from backend.app.api.routes.library import _resolve_source_disk_path, save_3mf_bytes_to_library
from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.eject_profile import EjectProfile
from backend.app.models.library import LibraryFile
from backend.app.models.printer import Printer
from backend.app.models.user import User
from backend.app.schemas.eject_profile import (
    EjectDryRunDispatchRequest,
    EjectDryRunDispatchResponse,
    EjectPreviewRequest,
    EjectPreviewResponse,
    EjectProfileCreate,
    EjectProfileResponse,
    EjectProfileUpdate,
    EjectValidationResponse,
)
from backend.app.services.eject.build_cache import EjectBuildError, get_or_build_eject_file
from backend.app.services.eject.generator import (
    BLOCK_END_MARKER,
    EjectGenerationError,
    generate_eject_gcode,
)
from backend.app.services.eject.geometry import ModelGeometry, get_geometry
from backend.app.services.eject.validator import validate_eject_gcode
from backend.app.services.printer_manager import printer_manager
from backend.app.services.queue_builder import create_queue_items
from backend.app.utils.printer_models import canon_model, full_home_lines
from backend.app.utils.threemf_tools import read_plate_gcode_header

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/eject-profiles", tags=["eject-profiles"])

_EXEC_BLOCK_START = "; EXECUTABLE_BLOCK_START"
_DUPLICATE_NAME = "An eject profile with that name already exists"


async def _get_profile_or_404(db: AsyncSession, profile_id: int) -> EjectProfile:
    result = await db.execute(select(EjectProfile).where(EjectProfile.id == profile_id))
    profile = result.scalar_one_or_none()
    if profile is None:
        raise HTTPException(status_code=404, detail="Eject profile not found")
    return profile


async def _resolve_source_3mf(db: AsyncSession, library_file_id: int) -> Path:
    result = await db.execute(select(LibraryFile).where(LibraryFile.id == library_file_id))
    lib_file = result.scalar_one_or_none()
    if lib_file is None:
        raise HTTPException(status_code=404, detail="Library file not found")
    path = _resolve_source_disk_path(lib_file)
    if not path or not path.exists():
        raise HTTPException(status_code=404, detail="Library file not found on disk")
    return path


def _read_max_z_or_422(source_path: Path, plate_index: int) -> float:
    header = read_plate_gcode_header(source_path, plate_index)
    raw = header.get("max_z_height")
    if raw is None:
        raise HTTPException(status_code=422, detail=f"Plate {plate_index} has no G-code / max_z_height in the 3MF")
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"Unparseable max_z_height {raw!r} in the 3MF") from exc


async def _resolve_preview_geometry(db: AsyncSession, body: EjectPreviewRequest) -> tuple[ModelGeometry, list[str]]:
    """Resolve geometry for a preview / dry-run (download) request from EXACTLY ONE
    of ``body.model`` or ``body.printer_id`` (the schema enforces exactly-one).

    These are hardware-ladder tools, so an UNVALIDATED geometry row is ALLOWED;
    the returned warnings name a model whose row is not hardware-validated yet.
    Raises 404 (unknown printer) or 422 (the model has no geometry row at all).
    """
    if body.model is not None:
        model_str: str | None = body.model
    else:
        result = await db.execute(select(Printer.model).where(Printer.id == body.printer_id))
        row = result.first()
        if row is None:
            raise HTTPException(status_code=404, detail="Printer not found")
        model_str = row[0]
    geometry = await get_geometry(db, model_str)
    if geometry is None:
        label = canon_model(model_str) or (model_str or "<unknown>")
        raise HTTPException(status_code=422, detail=f"No eject geometry for model {label!r}")
    warnings: list[str] = []
    if not geometry.validated:
        warnings.append(f"geometry for {geometry.model_key} not hardware-validated")
    return geometry, warnings


def _build_dryrun_gcode(source_path: Path, plate_index: int, eject_block: str, geometry: ModelGeometry) -> str:
    """Wrap the motion-only eject block into an executable EMPTY-BED dry-run body.

    Layout of the produced body::

        [source header/config comment block .. ; EXECUTABLE_BLOCK_START]
        ; DRY-RUN full home (incl. Z — safe on the empty bed)
        [eject block, including its own completion epilogue]

    The eject block now carries the stock machine-end FINISH tail itself (the
    generator's ``COMPLETION_EPILOGUE``), so — unlike the earlier implementation —
    the dry run does NOT splice the source's machine-end block: the block
    self-completes (the job ends FINISH, not FAILED-at-EOF). ``geometry`` selects
    the prepended homing dialect via ``full_home_lines``: dual-nozzle models home
    with the parameterized stock forms (``G28 X T300`` / ``G28 Y T300`` /
    ``G28 Z P0 T250``) because an unparameterized ``G28`` stall-loops on that
    firmware (007-H2C incident, 2026-07-12); single-nozzle models keep the bare ``G28``.

    The caller must validate ``eject_block`` BEFORE this wrapping: the body here
    prepends a full ``G28`` homing line that the eject-block validator rightly
    forbids inside the block itself.
    """
    import zipfile

    from backend.app.utils.threemf_tools import _find_target_gcode_name

    head = ""
    with zipfile.ZipFile(source_path, "r") as zf:
        target = _find_target_gcode_name(zf.namelist(), plate_index)
        if target is not None:
            with zf.open(target, "r") as fh:
                # The header + config comment block sits at the very top of the
                # file; 2 MB comfortably covers it before EXECUTABLE_BLOCK_START.
                prefix = fh.read(2 * 1024 * 1024).decode("utf-8", errors="ignore")
            idx = prefix.find(_EXEC_BLOCK_START)
            if idx != -1:
                line_end = prefix.find("\n", idx)
                head = prefix[: len(prefix) if line_end == -1 else line_end + 1]

    # Unlike a real print (whose start G-code homed Z long before the eject block
    # runs), the dry-run body starts cold — Z is unhomed, so the block's `G1 Z...`
    # would move against an unknown datum. A full home (including Z) is safe HERE
    # ONLY because the dry run is by definition run on an EMPTY bed (hardware-ladder
    # step 1): there is no part for the Z-probe to hit. Never emit a bare /
    # unparameterized home inside a production eject block.
    #
    # Dual-nozzle firmware stall-loops on the unparameterized `G28` (007-H2C
    # incident, 2026-07-12), so dual models get the parameterized stock forms
    # (X/Y torque homes + `G28 Z P0 T250`); single-nozzle models keep the bare `G28`.
    homing_comment = "; DRY-RUN ONLY: full home incl. Z - safe because the bed is EMPTY by definition of the dry run\n"
    homing = homing_comment + "\n".join(full_home_lines(geometry.model_key)) + "\n"
    # [G28 prologue] + [motion-only eject block with its own FINISH epilogue].
    body = homing + eject_block.rstrip("\n") + "\n"
    if head:
        return head + body
    # No executable-block markers found up top — ship a minimal standalone file
    # (the eject block's epilogue still ends the job FINISH).
    return _EXEC_BLOCK_START + "\n" + body


@router.get("", response_model=list[EjectProfileResponse])
@router.get("/", response_model=list[EjectProfileResponse])
async def list_eject_profiles(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.EJECT_PROFILES_READ),
):
    result = await db.execute(select(EjectProfile).order_by(EjectProfile.name))
    return list(result.scalars().all())


@router.post("", response_model=EjectProfileResponse, status_code=status.HTTP_201_CREATED)
@router.post("/", response_model=EjectProfileResponse, status_code=status.HTTP_201_CREATED)
async def create_eject_profile(
    data: EjectProfileCreate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.EJECT_PROFILES_CREATE),
):
    existing = await db.execute(select(EjectProfile).where(EjectProfile.name == data.name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=_DUPLICATE_NAME)
    profile = EjectProfile(**data.model_dump())
    db.add(profile)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=_DUPLICATE_NAME) from exc
    await db.refresh(profile)
    return profile


@router.get("/{profile_id}", response_model=EjectProfileResponse)
async def get_eject_profile(
    profile_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.EJECT_PROFILES_READ),
):
    return await _get_profile_or_404(db, profile_id)


@router.put("/{profile_id}", response_model=EjectProfileResponse)
async def update_eject_profile(
    profile_id: int,
    data: EjectProfileUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.EJECT_PROFILES_UPDATE),
):
    profile = await _get_profile_or_404(db, profile_id)
    updates = data.model_dump(exclude_unset=True)
    for key, value in updates.items():
        setattr(profile, key, value)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=_DUPLICATE_NAME) from exc
    await db.refresh(profile)
    return profile


@router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_eject_profile(
    profile_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.EJECT_PROFILES_DELETE),
):
    profile = await _get_profile_or_404(db, profile_id)
    await db.delete(profile)
    await db.commit()


@router.post("/{profile_id}/preview", response_model=EjectPreviewResponse)
async def preview_eject_profile(
    profile_id: int,
    body: EjectPreviewRequest,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.EJECT_PROFILES_READ),
):
    profile = await _get_profile_or_404(db, profile_id)
    geometry, geo_warnings = await _resolve_preview_geometry(db, body)
    source_path = await _resolve_source_3mf(db, body.library_file_id)
    max_z = _read_max_z_or_422(source_path, body.plate_index)

    try:
        gcode = generate_eject_gcode(profile, max_z, geometry)
    except EjectGenerationError as exc:
        # A refused generation (e.g. part too tall) is a validation failure, not
        # an HTTP error — surface it in the body so the UI can show the reason.
        return EjectPreviewResponse(
            gcode="",
            validation=EjectValidationResponse(ok=False, errors=[str(exc)], warnings=[]),
            max_z_height=max_z,
            warnings=geo_warnings,
        )

    result = validate_eject_gcode(gcode, profile, max_z, geometry)
    return EjectPreviewResponse(
        gcode=gcode,
        validation=EjectValidationResponse(ok=result.ok, errors=result.errors, warnings=result.warnings),
        max_z_height=max_z,
        warnings=geo_warnings,
    )


async def _read_eject_slim_setting(db: AsyncSession) -> bool:
    """The ``eject_slim_3mf`` toggle (Phase D3), default OFF — read by the eject/dry-run
    builders so ``build_cache`` stays DB-free."""
    from backend.app.api.routes.settings import get_setting

    raw = await get_setting(db, "eject_slim_3mf")
    return raw is not None and str(raw).strip().lower() == "true"


async def _build_dryrun_3mf(
    source_path: Path,
    plate_index: int,
    profile: EjectProfile,
    max_z: float,
    geometry: ModelGeometry,
    *,
    slim: bool = False,
) -> Path:
    """Generate + validate the motion-only eject block and repack into a temp
    dry-run ``.gcode.3mf``.

    The single canonical build path shared by the download dry-run and the
    one-click dispatch so the two never drift. Returns the temp file :class:`Path`
    (caller owns cleanup). Raises ``HTTPException(422)`` on any generation,
    validation or repack failure.

    Dry run = sweep GEOMETRY validation on an EMPTY bed (hardware-ladder step 1).
    The eject block is motion-only (no thermal gate at all now — the cooldown wait
    lives in the eject monitor), so nothing here could hang an ambient empty bed;
    the block self-completes via its FINISH epilogue.

    The one-pass build (``repack_3mf_eject`` via :func:`get_or_build_eject_file`)
    replaces the plate G-code AND zeroes the donor's ``slice_info.config`` usage in a
    single ZIP rewrite, OFF the event loop + cached, so this motion-only file reports
    ZERO filament / print-time usage — no consumer books the donor's plate weight or
    prediction against a sweep that extrudes nothing.
    """
    try:
        eject_block = generate_eject_gcode(profile, max_z, geometry)
    except EjectGenerationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    result = validate_eject_gcode(eject_block, profile, max_z, geometry)
    if not result.ok:
        raise HTTPException(status_code=422, detail="Eject validation failed: " + "; ".join(result.errors))
    if BLOCK_END_MARKER not in eject_block:  # defensive — generator always emits it
        raise HTTPException(status_code=422, detail="Generated eject block is malformed")

    dryrun_gcode = _build_dryrun_gcode(source_path, plate_index, eject_block, geometry)
    try:
        out_path, _build_key = await get_or_build_eject_file(source_path, plate_index, dryrun_gcode, slim=slim)
        return out_path
    except EjectBuildError as exc:
        raise HTTPException(status_code=422, detail=f"Plate {plate_index} has no G-code to replace") from exc


@router.post("/{profile_id}/dry-run")
async def dry_run_eject_profile(
    profile_id: int,
    body: EjectPreviewRequest,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.EJECT_PROFILES_READ),
):
    profile = await _get_profile_or_404(db, profile_id)
    geometry, geo_warnings = await _resolve_preview_geometry(db, body)
    source_path = await _resolve_source_3mf(db, body.library_file_id)
    max_z = _read_max_z_or_422(source_path, body.plate_index)

    slim = await _read_eject_slim_setting(db)
    out_path = await _build_dryrun_3mf(source_path, body.plate_index, profile, max_z, geometry, slim=slim)

    safe_profile = re.sub(r"[^A-Za-z0-9_.-]+", "_", profile.name).strip("_") or "profile"
    safe_file = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(source_path).stem).strip("_") or "file"
    download_name = f"dryrun_{safe_profile}_{safe_file}.gcode.3mf"

    # Unvalidated-geometry warning rides in a header (the body is the file). The
    # ladder tooling reads it; a validated model sends no header.
    headers = {"X-Geometry-Warnings": "; ".join(geo_warnings)} if geo_warnings else None

    return FileResponse(
        path=str(out_path),
        media_type="application/octet-stream",
        filename=download_name,
        headers=headers,
        background=BackgroundTask(lambda: out_path.unlink(missing_ok=True)),
    )


@router.post("/{profile_id}/dry-run/dispatch", response_model=EjectDryRunDispatchResponse)
async def dispatch_dry_run_eject_profile(
    profile_id: int,
    body: EjectDryRunDispatchRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermissionIfAuthEnabled(Permission.QUEUE_CREATE),
):
    """One-click dry-run: build the motion-only eject 3MF and queue it on a
    printer for an EMPTY-BED sweep test (hardware-ladder step 1).

    Saves the built file as the library file ``DRY-RUN {profile.name}.gcode.3mf``
    (replacing any prior copy so dry-run artifacts don't accumulate) and creates
    an ASAP print-queue item for it with bed levelling / vibration-cali / AMS off
    and the filament check skipped.

    No-deposit handling: a dry run deposits nothing, so its own terminal status
    never raises the plate-clear gate (see ``main.on_print_complete``) — a chain
    of dry runs runs without wedging the queue. Dispatch still does NOT auto-clear
    a gate raised by a PRIOR real print; that pre-existing hold (if any) must be
    cleared by the operator before this bed-homing test can start.

    Guards: 404 for an unknown profile / library file / printer; 422 if the
    target printer's model has no eject geometry row; 409 if that geometry is not
    hardware-validated (unless ``allow_unvalidated=true``, ladder step 4); 409 if
    the target printer is live-RUNNING or PAUSEd (dispatching a bed-homing test
    into an active print is the one unrecoverable mistake); 422 if the plate has
    no sliced G-code or the eject block can't be generated/validated.
    """
    profile = await _get_profile_or_404(db, profile_id)
    source_path = await _resolve_source_3mf(db, body.library_file_id)

    printer_result = await db.execute(select(Printer).where(Printer.id == body.printer_id))
    printer = printer_result.scalar_one_or_none()
    if printer is None:
        raise HTTPException(status_code=404, detail="Printer not found")

    # Resolve geometry from the TARGET printer's registered model: 422 when the
    # model has no geometry row, 409 when the row is not hardware-validated unless
    # the caller explicitly opts in with allow_unvalidated (ladder step 4).
    geometry = await get_geometry(db, printer.model)
    if geometry is None:
        label = canon_model(printer.model) or (printer.model or "<unknown>")
        raise HTTPException(status_code=422, detail=f"No eject geometry for model {label!r}")
    if not geometry.validated and not body.allow_unvalidated:
        raise HTTPException(
            status_code=409,
            detail=(
                f"geometry for {geometry.model_key} is not hardware-validated — "
                "set allow_unvalidated=true to dispatch (hardware-ladder step 4 only)"
            ),
        )

    # 409: never dispatch a bed-homing sweep test into an active print. Read the
    # live state the same way the scheduler/other routes do (printer_manager).
    state = printer_manager.get_status(body.printer_id)
    if state is not None and getattr(state, "state", None) in ("RUNNING", "PAUSE"):
        raise HTTPException(
            status_code=409,
            detail=f"Printer {printer.name!r} is currently {state.state} — refusing to dispatch a dry run onto an active print",
        )

    max_z = _read_max_z_or_422(source_path, body.plate_index)
    slim = await _read_eject_slim_setting(db)
    out_path = await _build_dryrun_3mf(source_path, body.plate_index, profile, max_z, geometry, slim=slim)

    try:
        file_bytes = out_path.read_bytes()
    finally:
        out_path.unlink(missing_ok=True)

    # Persist as a library file, replacing any prior DRY-RUN copy for this profile
    # so artifacts don't accumulate. Soft-delete (the canonical library delete)
    # keeps historical queue-item FKs intact; the new row is created via the same
    # service the upload route uses (save_3mf_bytes_to_library).
    dryrun_filename = f"DRY-RUN {profile.name}.gcode.3mf"
    existing = await db.execute(LibraryFile.active().where(LibraryFile.filename == dryrun_filename))
    now = datetime.now(timezone.utc)
    for stale in existing.scalars().all():
        stale.deleted_at = now

    library_file, _existed = await save_3mf_bytes_to_library(
        db,
        file_bytes=file_bytes,
        filename=dryrun_filename,
        owner_id=current_user.id if current_user else None,
    )

    # Create the ASAP queue item on the requested printer via the shared
    # queue-item builder (same canonical path as POST /queue). Safe test flags:
    # no bed levelling / vibration cali / AMS, and skip the filament check.
    items = await create_queue_items(
        db,
        count=1,
        printer_id=body.printer_id,
        fields={
            "printer_id": body.printer_id,
            "library_file_id": library_file.id,
            "plate_id": body.plate_index,
            "bed_levelling": False,
            "vibration_cali": False,
            "use_ams": False,
            "skip_filament_check": True,
            "is_dry_run": True,
            "status": "pending",
            "created_by_id": current_user.id if current_user else None,
        },
    )
    await db.commit()
    queue_item = items[0]
    await db.refresh(queue_item)

    # Wake the scheduler so the dry-run item dispatches immediately (latency Phase A).
    try:
        from backend.app.services.dispatch_kick import dispatch_kick

        dispatch_kick.kick("dry_run_enqueue", body.printer_id)
    except Exception:
        logger.debug("dispatch kick failed after dry-run enqueue (non-fatal)", exc_info=True)

    return EjectDryRunDispatchResponse(
        queue_item_id=queue_item.id,
        library_file_id=library_file.id,
        message=(
            f"Dry run queued on {printer.name!r} (plate {body.plate_index}). Confirm the bed is EMPTY before it starts."
        ),
    )
