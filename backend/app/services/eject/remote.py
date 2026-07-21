"""Shared part-present eject dispatcher + the pending-eject registry.

The eject sweep is a SEPARATE, server-dispatched, motion-only job — used by two
callers that share this ONE path:

- **Production loop**: the eject monitor, once the live bed reaches the release
  threshold, dispatches the eject for the finished unit (``purpose="production"``).
- **First article**: an operator approval with ``eject_remotely`` dispatches the
  eject for the FA plate (``purpose="fa"``).

Both build a standalone motion-only ``.gcode.3mf`` (``build_part_present_eject_file``),
FTPS-upload it and ``project_file``-dispatch it via ``printer_manager.start_print``
with EVERY pre-print calibration OFF (never bed-probe / shake with a part on the
plate), then register a :class:`PendingEject` so the terminal handler can match the
job's echoed ``subtask_id`` and act on completion. The plate-clear gate is NOT
cleared here — it drops only when the eject job's terminal arrives.

Failures raise :class:`EjectDispatchError` (a plain domain error carrying an HTTP
status hint); the FA route wraps it in an ``HTTPException`` while the monitor lets
it propagate as a dispatch failure it retries.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.models.eject_profile import EjectProfile
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services.eject import progress as eject_progress
from backend.app.services.eject.dispatch import build_part_present_eject_file
from backend.app.services.eject.geometry import GeometryUnavailable, get_geometry_required
from backend.app.services.printer_manager import printer_manager
from backend.app.services.usb_storage import upload_in_flight

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

EjectPurpose = Literal["fa", "production", "manual"]


@dataclass(frozen=True)
class PendingEject:
    """An in-flight server-dispatched eject awaiting its terminal status.

    Held in-memory (``_pending_eject``) for the live-callback fast path AND mirrored
    durably onto the owning queue unit's ``eject_dispatched_at`` stamp so a restart
    between dispatch and terminal can rehydrate the entry (W1). The plate gate is
    persisted independently and only auto-clears once the eject's terminal (live or
    reconciled) is positively matched.
    """

    purpose: EjectPurpose
    run_id: int | None
    queue_item_id: int | None


# printer_id -> the one in-flight eject on that printer.
_pending_eject: dict[int, PendingEject] = {}

# Rows whose durable eject stamp is older than this at startup are treated as a
# crash that never cleared and are dropped (NULLed) with a WARNING rather than
# rehydrated — no eject stays "in flight" across a day-long outage.
_PENDING_EJECT_STALE_TTL_H = 24

# The canonical eject-job-name convention, minted at dispatch. Two shapes:
#   * queue-item-bound (production / first-article): ``eject_{purpose}_item{queue_item_id}``
#   * foreign-plate manual eject (no queue item): ``eject_manual_p{printer_id}``
# Case-insensitive; the printer echoes the stem verbatim as ``subtask_name``. For the
# manual form the trailing integer is the PRINTER id (there is no queue item).
_EJECT_NAME_RE = re.compile(r"^eject_(?:(fa|production)_item|manual_p)(\d+)$", re.IGNORECASE)


def _eject_name_stem(name: str) -> str:
    """Strip any leading path + repeated ``.gcode.3mf`` / ``.3mf`` / ``.gcode``
    suffixes, leaving the bare job stem for name matching."""
    base = str(name).strip().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    while True:
        low = base.lower()
        if low.endswith(".gcode.3mf"):
            base = base[:-10]
        elif low.endswith(".3mf"):
            base = base[:-4]
        elif low.endswith(".gcode"):
            base = base[:-6]
        else:
            break
    return base


def parse_eject_job_name(name: str | None) -> tuple[EjectPurpose, int] | None:
    """``(purpose, id)`` parsed from an eject job's echoed name/filename, or None
    when ``name`` is not one of our eject jobs.

    The trailing int is the QUEUE-ITEM id for ``fa``/``production`` jobs and the
    PRINTER id for a ``manual`` (foreign-plate) job. Every consumer uses only the
    truthiness of this result (``is_eject_job_name``), so the id's meaning per
    purpose does not leak."""
    if not name:
        return None
    m = _EJECT_NAME_RE.match(_eject_name_stem(name))
    if not m:
        return None
    purpose = m.group(1).lower() if m.group(1) else "manual"
    return (purpose, int(m.group(2)))  # type: ignore[return-value]


def is_eject_job_name(name: str | None) -> bool:
    """True when ``name`` (a subtask_name or filename) is one of our eject jobs."""
    return parse_eject_job_name(name) is not None


def expected_eject_stem(pending: PendingEject) -> str:
    """The eject job stem THIS pending eject was dispatched under."""
    return f"eject_{pending.purpose}_item{pending.queue_item_id}"


def register_pending_eject(printer_id: int, pending: PendingEject) -> None:
    _pending_eject[printer_id] = pending


def pop_pending_eject(printer_id: int) -> PendingEject | None:
    return _pending_eject.pop(printer_id, None)


def peek_pending_eject(printer_id: int) -> PendingEject | None:
    return _pending_eject.get(printer_id)


def pending_eject_printer_ids() -> list[int]:
    """Printer ids that currently have a pending eject (live or hydrated)."""
    return list(_pending_eject.keys())


def matches_pending_eject(
    printer_id: int, completed_subtask_id: str | None, *, subtask_name: str | None = None
) -> bool:
    """True when a :class:`PendingEject` is registered for ``printer_id`` AND the
    terminal/echoed identity does not POSITIVELY mismatch the dispatched eject.

    The single origin of the "is this terminal (or start) our server-dispatched
    eject?" decision, shared by ``farm_policy.on_terminal`` (which still pops the
    registry itself) and the ``main.py`` start/complete callbacks. A positive
    mismatch exists when EITHER:

    * BOTH ``completed_subtask_id`` and the client's ``last_dispatch_subtask_id`` are
      truthy AND unequal (the historical id check — a missing id on either side is a
      lenient match, since a standalone eject file can echo nothing / "0"); OR
    * ``subtask_name`` is truthy AND its stem does not equal ``expected_eject_stem``
      of the pending. This closes the post-restart hole (W1/R2): after a restart the
      client's ``last_dispatch_subtask_id`` is gone, so id-matching turns lenient and
      ANY terminal would otherwise consume a HYDRATED pending and clear our gate — the
      name check re-establishes positive identity from the echoed job name.

    Name evidence alone (empty registry) NEVER makes this return True — see
    :func:`is_eject_job_name` for the suppress-only name signal. This function NEVER
    pops the registry; callers own the pop.
    """
    pending = peek_pending_eject(printer_id)
    if pending is None:
        return False
    client = printer_manager.get_client(printer_id)
    expected_subtask = getattr(client, "last_dispatch_subtask_id", None) if client else None
    id_mismatch = bool(completed_subtask_id and expected_subtask and completed_subtask_id != expected_subtask)
    name_mismatch = False
    if subtask_name:
        # A manual (foreign-plate) eject carries no queue item, so its stem is keyed
        # by PRINTER id — close the queue_item_id-None leniency for that purpose by
        # name-checking the printer-keyed stem instead.
        expected_stem: str | None = None
        if pending.purpose == "manual":
            expected_stem = f"eject_manual_p{printer_id}"
        elif pending.queue_item_id is not None:
            expected_stem = expected_eject_stem(pending)
        if expected_stem is not None:
            name_mismatch = _eject_name_stem(subtask_name).lower() != expected_stem.lower()
    return not (id_mismatch or name_mismatch)


async def persist_pending_eject(db: AsyncSession, printer_id: int, pending: PendingEject) -> None:
    """Stamp ``eject_dispatched_at`` on the eject's owning queue unit (durable mirror).

    Same-session write on the caller's ``db`` (NOT a new/fire-and-forget session),
    committed here so the mirror is durable the instant dispatch is accepted. A
    missing / queue-item-less pending is a no-op (nothing to mirror)."""
    if pending.queue_item_id is None:
        return
    item = await db.get(PrintQueueItem, pending.queue_item_id)
    if item is None:
        return
    item.eject_dispatched_at = datetime.now(timezone.utc)
    await db.commit()


async def clear_pending_eject(db: AsyncSession, printer_id: int) -> PendingEject | None:
    """Resolve the pending eject on ``printer_id``: pop the in-memory registry AND
    NULL every in-flight eject stamp on that printer (atomic with resolution).

    Printer-scoped NULL (not just the popped entry's item) so a crash that stamped
    more than one row for a printer can't leave an orphan stamp behind. Returns the
    popped :class:`PendingEject` (or None). Commits only when a stamp was cleared."""
    pending = pop_pending_eject(printer_id)
    result = await db.execute(
        select(PrintQueueItem).where(
            PrintQueueItem.printer_id == printer_id,
            PrintQueueItem.eject_dispatched_at.is_not(None),
        )
    )
    changed = False
    for item in result.scalars().all():
        item.eject_dispatched_at = None
        changed = True
    if changed:
        await db.commit()
    return pending


async def hydrate_pending_ejects_from_db() -> int:
    """Rebuild the in-memory pending-eject registry from durable stamps at startup.

    Selects every ``eject_dispatched_at IS NOT NULL`` unit (newest first) and rebuilds
    :class:`PendingEject` keyed by ``printer_id`` (``purpose`` from ``first_article``,
    ``run_id`` from ``batch_id``). Stamps older than ``_PENDING_EJECT_STALE_TTL_H`` are
    dropped (NULLed) with a WARNING; if two unresolved rows resolve to one printer
    (only possible via a crash between cycles), the newest stamp is kept and the rest
    NULLed with a WARNING — the registry is one-per-printer by construction. Returns
    the number of pending ejects rehydrated."""
    from backend.app.core.database import async_session

    cutoff = datetime.now(timezone.utc) - timedelta(hours=_PENDING_EJECT_STALE_TTL_H)
    hydrated = 0
    async with async_session() as db:
        result = await db.execute(
            select(PrintQueueItem)
            .where(PrintQueueItem.eject_dispatched_at.is_not(None))
            .order_by(PrintQueueItem.eject_dispatched_at.desc())
        )
        rows = list(result.scalars().all())
        seen_printers: set[int] = set()
        changed = False
        for item in rows:
            stamp = item.eject_dispatched_at
            if stamp is not None and stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            if stamp is None or stamp < cutoff:
                logger.warning(
                    "eject.remote: dropping stale pending-eject stamp on item %s (dispatched %s, TTL %sh)",
                    item.id,
                    item.eject_dispatched_at,
                    _PENDING_EJECT_STALE_TTL_H,
                )
                item.eject_dispatched_at = None
                changed = True
                continue
            if item.printer_id is None:
                logger.warning("eject.remote: dropping pending-eject stamp on item %s — no printer_id", item.id)
                item.eject_dispatched_at = None
                changed = True
                continue
            if item.printer_id in seen_printers:
                logger.warning(
                    "eject.remote: multiple pending ejects for printer %s — NULLing older stamp on item %s",
                    item.printer_id,
                    item.id,
                )
                item.eject_dispatched_at = None
                changed = True
                continue
            seen_printers.add(item.printer_id)
            register_pending_eject(
                item.printer_id,
                PendingEject(
                    purpose="fa" if item.first_article else "production",
                    run_id=item.batch_id,
                    queue_item_id=item.id,
                ),
            )
            hydrated += 1
        if changed:
            await db.commit()
    if hydrated:
        logger.info("eject.remote: hydrated %d pending eject(s) from durable stamps", hydrated)
    return hydrated


class EjectDispatchError(RuntimeError):
    """A part-present eject could not be dispatched.

    Carries an HTTP ``status_code`` hint (409 precondition / 502 transport) so the
    FA route can translate it to an ``HTTPException`` without this module importing
    FastAPI. The monitor ignores the hint and treats any raise as a dispatch failure.
    """

    def __init__(self, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


async def _resolve_source_path(db: AsyncSession, item: PrintQueueItem) -> Path:
    """Resolve the on-disk source ``.gcode.3mf`` for ``item`` (the file it printed).

    Prefers the library file (present for the whole run's lifetime), falling back
    to the per-dispatch archive copy when the library file was cleaned up after
    dispatch. Raises :class:`EjectDispatchError` (409) when neither resolves to an
    existing file.
    """
    from backend.app.core.config import settings as app_settings

    if item.library_file_id:
        lib = await db.get(LibraryFile, item.library_file_id)
        if lib is not None:
            lib_path = Path(lib.file_path)
            path = lib_path if lib_path.is_absolute() else app_settings.base_dir / lib.file_path
            if path.exists():
                return path
    if item.archive_id:
        archive = await db.get(PrintArchive, item.archive_id)
        if archive is not None:
            path = app_settings.base_dir / archive.file_path
            if path.exists():
                return path
    raise EjectDispatchError("Eject source file not found on disk for the finished unit", status_code=409)


async def dispatch_part_present_eject(
    db: AsyncSession,
    *,
    printer_id: int,
    queue_item_id: int,
    purpose: EjectPurpose,
    run_id: int | None,
) -> None:
    """Build + FTPS-upload + dispatch a part-present motion-only eject for one unit.

    Resolves the profile / geometry / source file from ``queue_item_id`` and the
    target printer, builds the standalone eject-only file, uploads it (honouring the
    FTP retry settings) and starts it with EVERY pre-print calibration OFF, then
    registers a :class:`PendingEject`. Does NOT touch the plate-clear gate — that
    clears only when the eject job's terminal arrives.

    Raises :class:`EjectDispatchError` on any precondition (409) or transport (502)
    failure, leaving no half state (nothing is registered unless ``start_print``
    was accepted).
    """
    item = await db.get(PrintQueueItem, queue_item_id)
    if item is None:
        raise EjectDispatchError(f"Queue item {queue_item_id} not found; cannot eject", status_code=409)
    if item.eject_profile_id is None:
        raise EjectDispatchError("Unit has no eject profile; cannot eject remotely", status_code=409)

    printer = await db.get(Printer, printer_id)
    if printer is None:
        raise EjectDispatchError("Eject printer not found", status_code=409)
    if not printer_manager.is_connected(printer.id):
        raise EjectDispatchError("Printer is not connected; cannot eject remotely", status_code=409)

    # Fail-closed on a model with no geometry row or a row not hardware-validated —
    # a production eject must never drive an unvalidated envelope.
    try:
        geometry = await get_geometry_required(db, printer.model, require_validated=True)
    except GeometryUnavailable as exc:
        raise EjectDispatchError(exc.reason, status_code=409) from exc

    profile = await db.get(EjectProfile, item.eject_profile_id)
    if profile is None:
        raise EjectDispatchError("Eject profile not found", status_code=409)

    source_path = await _resolve_source_path(db, item)
    plate_id = item.plate_id or 1
    eject_progress.emit_eject_progress(printer_id=printer.id, queue_item_id=queue_item_id, phase="building")
    try:
        eject_path = await build_part_present_eject_file(source_path, plate_id, profile, geometry)
    except Exception as exc:  # noqa: BLE001 — generation/validation/repack → actionable 409
        eject_progress.emit_eject_progress(printer_id=printer.id, queue_item_id=queue_item_id, phase="failed")
        raise EjectDispatchError(f"Failed to build part-present eject file: {exc}", status_code=409) from exc

    pending = PendingEject(purpose=purpose, run_id=run_id, queue_item_id=queue_item_id)
    # The eject file's FTPS upload transiently drops the H2S sdcard flag; mark the
    # printer upload-in-flight so the USB-drop verifier ignores that dispatch blip.
    async with upload_in_flight(printer.id):
        await _upload_start_register_eject(
            db,
            printer=printer,
            eject_path=eject_path,
            job_stem=f"eject_{purpose}_item{queue_item_id}",
            plate_id=plate_id,
            pending=pending,
        )
    logger.info(
        "eject.remote: dispatched %s eject for item %s (run %s) on printer %s",
        purpose,
        queue_item_id,
        run_id,
        printer.id,
    )


async def dispatch_foreign_eject(
    db: AsyncSession,
    *,
    printer_id: int,
    profile_id: int,
    source_path: Path,
    plate_id: int,
) -> None:
    """Build + FTPS-upload + dispatch a part-present eject for a FOREIGN plate.

    The two-step "Eject now" confirm for a plate the farm did not dispatch (started
    from Bambu Studio): the manual-eject service resolved the donor ``source_path`` +
    ``plate_id`` from the foreign print's archive and picked an ``eject_profile_id``,
    and this shares ``dispatch_part_present_eject``'s upload→start→register tail. It is
    NOT queue-item-bound — it registers a ``purpose="manual"`` :class:`PendingEject`
    with ``queue_item_id=None``. That no-op mirror is DELIBERATE: a manual eject is not
    restart-durable, so a mid-eject restart leaves the plate gate raised (fail-closed).

    Geometry is fail-closed (``require_validated=True``); the caller owns cleanup of
    ``source_path`` (it may be a temp FTPS re-fetch). Raises :class:`EjectDispatchError`
    on any precondition (409) or transport (502) failure, leaving nothing registered
    unless ``start_print`` was accepted.
    """
    printer = await db.get(Printer, printer_id)
    if printer is None:
        raise EjectDispatchError("Eject printer not found", status_code=409)
    if not printer_manager.is_connected(printer.id):
        raise EjectDispatchError("Printer is not connected; cannot eject remotely", status_code=409)

    try:
        geometry = await get_geometry_required(db, printer.model, require_validated=True)
    except GeometryUnavailable as exc:
        raise EjectDispatchError(exc.reason, status_code=409) from exc

    profile = await db.get(EjectProfile, profile_id)
    if profile is None:
        raise EjectDispatchError("Eject profile not found", status_code=409)

    eject_progress.emit_eject_progress(printer_id=printer.id, queue_item_id=None, phase="building")
    try:
        eject_path = await build_part_present_eject_file(Path(source_path), plate_id, profile, geometry)
    except Exception as exc:  # noqa: BLE001 — generation/validation/repack → actionable 409
        eject_progress.emit_eject_progress(printer_id=printer.id, queue_item_id=None, phase="failed")
        raise EjectDispatchError(f"Failed to build part-present eject file: {exc}", status_code=409) from exc

    pending = PendingEject(purpose="manual", run_id=None, queue_item_id=None)
    # Same as the production path: the FTPS upload transiently drops the H2S sdcard
    # flag; mark the printer upload-in-flight so the USB-drop verifier ignores the blip.
    async with upload_in_flight(printer.id):
        await _upload_start_register_eject(
            db,
            printer=printer,
            eject_path=eject_path,
            job_stem=f"eject_manual_p{printer_id}",
            plate_id=plate_id,
            pending=pending,
        )
    logger.info(
        "eject.remote: dispatched manual (foreign-plate) eject on printer %s (plate %s, profile %s)",
        printer_id,
        plate_id,
        profile_id,
    )


async def _upload_start_register_eject(
    db: AsyncSession,
    *,
    printer: Printer,
    eject_path: Path,
    job_stem: str,
    plate_id: int,
    pending: PendingEject,
) -> None:
    """Shared eject tail: FTPS-upload the built eject file (honouring the FTP retry
    settings), start it with EVERY pre-print calibration OFF, then register + durably
    mirror the pending eject. The built ``eject_path`` is always cleaned up; nothing is
    registered unless ``start_print`` was accepted. Raises :class:`EjectDispatchError`
    (502) on upload / start failure.
    """
    import asyncio

    from backend.app.services.bambu_ftp import (
        get_ftp_retry_settings,
        upload_file_async,
        with_ftp_retry,
    )
    from backend.app.utils.filename import derive_remote_filename

    remote_filename = derive_remote_filename(f"{job_stem}.gcode.3mf")
    remote_path = f"/{remote_filename}"
    ftp_retry_enabled, ftp_retry_count, ftp_retry_delay, ftp_timeout = await get_ftp_retry_settings()

    # Upload progress rides the FTP callback, which fires on the executor thread —
    # marshal each tick back onto the loop before emitting (never touch the socket
    # from another thread). The callback must never raise (it would abort the upload).
    loop = asyncio.get_running_loop()

    def _on_upload_progress(uploaded_bytes: int, total_bytes: int) -> None:
        pct = round(uploaded_bytes / total_bytes * 100.0, 1) if total_bytes else None
        loop.call_soon_threadsafe(
            lambda: eject_progress.emit_eject_progress(
                printer_id=printer.id, queue_item_id=pending.queue_item_id, phase="uploading", progress_pct=pct
            )
        )

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
                progress_callback=_on_upload_progress,
                max_retries=ftp_retry_count,
                retry_delay=ftp_retry_delay,
                operation_name=f"Upload {pending.purpose} eject to {printer.name}",
            )
        else:
            uploaded = await upload_file_async(
                printer.ip_address,
                printer.access_code,
                eject_path,
                remote_path,
                socket_timeout=ftp_timeout,
                printer_model=printer.model,
                progress_callback=_on_upload_progress,
            )
    except Exception as exc:  # noqa: BLE001
        uploaded = False
        logger.error("eject.remote: %s eject upload error: %s", pending.purpose, exc)
    finally:
        try:
            eject_path.unlink(missing_ok=True)
        except OSError:
            pass

    if not uploaded:
        eject_progress.emit_eject_progress(printer_id=printer.id, queue_item_id=pending.queue_item_id, phase="failed")
        raise EjectDispatchError("Failed to upload the eject file to the printer", status_code=502)

    # EVERY pre-print calibration OFF — never bed-probe / shake / re-level with a
    # part on the plate (the old FA call omitted bed_levelling/vibration_cali and
    # defaulted them True — a hazard this closes).
    started = printer_manager.start_print(
        printer.id,
        remote_filename,
        plate_id=plate_id,
        bed_levelling=False,
        flow_cali=False,
        vibration_cali=False,
        layer_inspect=False,
        timelapse=False,
        use_ams=False,
    )
    if not started:
        eject_progress.emit_eject_progress(printer_id=printer.id, queue_item_id=pending.queue_item_id, phase="failed")
        raise EjectDispatchError("Failed to send the eject command to the printer", status_code=502)

    eject_progress.emit_eject_progress(printer_id=printer.id, queue_item_id=pending.queue_item_id, phase="sent")

    register_pending_eject(printer.id, pending)
    # Durable mirror: stamp the owning unit so the eject survives a restart between
    # here and its terminal (W1). A manual/foreign eject has no queue item, so this is
    # a deliberate no-op — the gate stays raised across a restart (fail-closed).
    await persist_pending_eject(db, printer.id, pending)
