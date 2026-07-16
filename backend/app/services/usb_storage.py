"""USB storage-low notification + automatic cleanup for the print farm.

H2S printers report HMS ``0500010000030004`` ("Not enough space on MicroSD Card")
when the USB-A drive fills up — on the H2S that "MicroSD" IS the USB drive the LAN
dispatch path requires (there is no card slot). A full USB breaks every FTPS upload
(553) and silently drops the printer out of lights-out production. The bulk of the
fill is ``/ipcam`` camera recordings, eventually stale print files.

This module reacts to the ARRIVAL of that HMS code (driven from
``main.on_printer_status_change``'s new-HMS hook, beside the plate-occupancy hook)
by:

  1. Stage 1 — deleting old camera/timelapse recordings (``/ipcam``,
     ``/ipcam/thumbnail``, ``/timelapse``).
  2. Stage 2 — only if free space is still known-low, deleting the oldest UNUSED
     root print files (``*.gcode.3mf`` / ``*.3mf``), never the file currently
     loaded/printing and never a file backing a non-terminal queue item.
  3. Firing the dedicated ``on_storage_low`` notification ONLY on FAILURE — a
     cleanup that freed space (or found space already OK) is a routine success
     the operator does not need pinged about; it leaves an INFO audit line only.
     Failures (FTPS unreachable / USB unmounted, nothing cleanable, undeletable
     files) still notify.

Two proactive companions keep the drive from ever reaching the full wall:

  * ``drain_recordings_if_idle`` — a recordings-only (stage 1) pass fired
    fire-and-forget from ``main.on_print_complete`` after every terminal print, so
    the ~3 GB/h of ``/ipcam`` chunks a long lights-out run generates get trimmed
    BEFORE the USB fills. Opportunistic: it never touches print files and never
    notifies (success logs, failure is silent — the HMS/drop paths own alerting).
  * a ``sdcard`` True→False transition detected in ``main``'s status hook fires the
    failure notification directly (``reason="USB drive dropped/unmounted…"``) so a
    mid-print firmware unmount is surfaced instead of silently stalling.

It NEVER touches ``verify_job``, ``/model``, directories, or anything outside the
three recording dirs + root 3mf files. An unreachable FTPS printer surfaces as the
failure notification, never an unhandled exception.

State (per-printer cleanup cooldown + deferral + single-flight) is module-level,
matching the other event-edge bookkeeping in the fork. A module ``_inflight`` set
collapses concurrent triggers (consecutive status ticks, HMS + drain) into one
running pass per printer. Injectable ``manager`` / ``now`` for tests.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from backend.app.services.bambu_ftp import (
    DeleteResult,
    delete_file_async,
    get_storage_info_async,
    list_files_async,
)
from backend.app.services.printer_manager import printer_manager
from backend.app.utils.filename import derive_remote_filename

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# HMS full-codes (uppercase-hex ``f"{attr:08X}{code:08X}"``) that mean the USB drive
# is (nearly) full. Frozen so it can be intersected against new HMS codes in the
# main.py hook and used for the generic-notification suppression there.
HMS_STORAGE_LOW_FULL_CODES: frozenset[str] = frozenset({"0500010000030004"})

# A file must be at least this old before cleanup will delete it — never remove a
# recording still being written or a print file that may have just been uploaded.
MIN_FILE_AGE_S: float = 3600.0

# After an attempt (success OR failure) on a printer, suppress re-runs for this long
# so a flapping / re-arriving HMS code doesn't thrash the USB or the notification
# channel.
CLEANUP_COOLDOWN_S: float = 6 * 3600.0

# Stage-2 stop target AND the stage-2 entry guard: don't touch print files at all
# once at least this much space is free; in stage 2, stop deleting as soon as the
# projected free space reaches it.
TARGET_FREE_BYTES: int = 2 * 1024**3

# Recording directories cleaned in stage 1 (camera stills, thumbnails, timelapses).
RECORDING_DIRS: tuple[str, ...] = ("/ipcam", "/ipcam/thumbnail", "/timelapse")

# Root print-file extensions considered for stage-2 deletion (longest first).
_PRINT_FILE_SUFFIXES: tuple[str, ...] = (".gcode.3mf", ".3mf")

# Live gcode_state values that mean the printer is actively printing — cleanup is
# skipped so we never interfere with an in-progress job.
_ACTIVE_PRINT_STATES: frozenset[str] = frozenset({"RUNNING", "PREPARE"})

# Queue-item statuses that keep a print file "in use" (must not be deleted). The
# terminal statuses (completed / failed / skipped / cancelled) release the file.
_NON_TERMINAL_QUEUE_STATUSES: frozenset[str] = frozenset({"pending", "printing"})

# printer_id -> timestamp of the last cleanup ATTEMPT (success or failure). Guards
# the per-printer cooldown, SHARED by the HMS-triggered cleanup and the post-print
# recordings drain. Module-level, matching farm_stall's edge bookkeeping.
_last_cleanup_at: dict[int, float] = {}

# Printers whose LAST storage-low trigger was swallowed by the actively-printing
# guard. Without this the deferral would be permanent: a storage-low HMS that
# arrives mid-print may never re-fire the main.py NEW-code edge, and the firmware
# can even DROP the code from hms[] when it unmounts a full drive (live-observed on
# printer 7, 2026-07-14) — so a presence-gated retry would wait forever. The
# main.py status hook therefore consults ``should_retry_deferred`` (cheap, DB-free,
# with NO code-presence requirement) every tick and re-triggers once the printer is
# no longer printing; a retry against an unmounted drive then surfaces as the normal
# FTPS-unreachable FAILURE notification instead of silence. The spawn site clears
# the flag before create_task (and every attempt re-clears at its start, re-adding
# only if still printing) so a disabled setting or an active cooldown can't cause
# endless per-tick retries.
_deferred_printers: set[int] = set()

# Printers with a cleanup pass currently running. Checked-and-added SYNCHRONOUSLY at
# the very top of every entry point (before any await) and discarded in a finally,
# so concurrent triggers — consecutive status ticks, or an HMS trigger racing the
# post-print drain — collapse to a single running pass per printer.
_inflight: set[int] = set()

# Last REPORTED USB (`sdcard`) presence per printer, for the mid-print drop alert.
# Absent = never observed present: tracking only begins once the drive is seen True,
# so the startup default (False) never spuriously fires a drop. See
# ``record_sdcard_and_detect_drop``.
_last_sdcard: dict[int, bool] = {}


def _reset_state() -> None:
    """Test hook: clear the module-level cooldown + deferral + in-flight + USB state."""
    _last_cleanup_at.clear()
    _deferred_printers.clear()
    _inflight.clear()
    _last_sdcard.clear()


def record_sdcard_and_detect_drop(printer_id: int, cur_sdcard: bool) -> bool:
    """Track USB (`sdcard`) presence and report a genuine reported True→False drop.

    Returns True exactly once when a printer transitions from a REPORTED-present USB
    drive to absent — a firmware unmount of a full/failed drive, typically mid-print
    (printer 7, 2026-07-14), which clears hms[] too and would otherwise silently
    strand the printer. NEVER fires from the startup default / never-reported state:
    tracking begins only once the drive has been observed present, so an initial
    False (the ``PrinterState.sdcard`` default before any report) is ignored. After
    a drop the printer re-arms — a remount (True) followed by another drop fires
    again. Pure apart from the module ``_last_sdcard`` edge state.
    """
    prev = _last_sdcard.get(printer_id)
    dropped = prev is True and cur_sdcard is False
    # Record only once the drive has actually been observed present (or we are
    # already tracking it), so the never-reported default can't fire a drop.
    if cur_sdcard or printer_id in _last_sdcard:
        _last_sdcard[printer_id] = cur_sdcard
    return dropped


def has_deferred(printer_id: int) -> bool:
    """Whether this printer's last storage-low trigger was deferred (was printing)."""
    return printer_id in _deferred_printers


def should_retry_deferred(printer_id: int, live_state: str | None) -> bool:
    """DB-free trigger predicate for the main.py status hook (deferred-retry path).

    True when BOTH hold: the printer has a pending deferral AND its live
    ``gcode_state`` is no longer RUNNING/PREPARE. Deliberately does NOT require the
    storage-low HMS code to still be present — the firmware DROPS that code from
    hms[] when it unmounts a full drive, so a presence-gated retry would never fire
    for the exact failure it exists to recover from (silent permanent deferral,
    printer 7, 2026-07-14). A retry that finds the drive unmounted surfaces as the
    normal FTPS-unreachable FAILURE notification instead. Set-membership check first
    so the common no-deferral tick short-circuits before any state work.
    """
    if printer_id not in _deferred_printers:
        return False
    return str(live_state or "").upper() not in _ACTIVE_PRINT_STATES


@dataclass
class _CleanupOutcome:
    """Result of a cleanup pass, shaped for the notification."""

    success: bool = False
    freed_bytes: int = 0
    files_deleted: int = 0
    free_bytes: int | None = None
    reason: str | None = None
    failed_paths: list[str] = field(default_factory=list)


async def _ftp_reachable(ip_address: str, access_code: str, model: str | None) -> bool:
    """Best-effort FTPS reachability probe (port 990 up + auth OK).

    ``list_files_async`` / ``get_storage_info_async`` both collapse a dead port or
    a rejected login to an empty/None result, so an explicit connect probe is the
    only way to tell "USB empty" from "printer FTPS unreachable" and surface the
    latter as the failure notification. Module-level so tests can patch it.
    """
    from backend.app.services.bambu_ftp import BambuFTPClient

    loop = asyncio.get_event_loop()

    def _probe() -> bool:
        client = BambuFTPClient(ip_address, access_code, printer_model=model)
        try:
            if client.connect():
                client.disconnect()
                return True
            return False
        except Exception:  # noqa: BLE001 — any probe failure means "not reachable"
            return False

    try:
        return await loop.run_in_executor(None, _probe)
    except Exception:  # noqa: BLE001 — executor/scheduling failure is still "unreachable"
        return False


def _is_printing(manager, printer_id: int) -> bool:
    """Whether the printer is actively printing (RUNNING/PREPARE) right now."""
    try:
        client = manager.get_client(printer_id)
    except Exception:  # noqa: BLE001 — manager access must never crash the guard
        return False
    if client is None or getattr(client, "state", None) is None:
        return False
    return str(getattr(client.state, "state", "") or "").upper() in _ACTIVE_PRINT_STATES


def _name_variants(name: str | None) -> set[str]:
    """Normalized name variants for in-use comparison (raw + derived remote name)."""
    if not name or not isinstance(name, str):
        return set()
    variants = {name.strip().lower()}
    try:
        variants.add(derive_remote_filename(name).lower())
    except (TypeError, ValueError):
        pass
    return {v for v in variants if v}


async def _in_use_remote_names(db: AsyncSession, manager, printer_id: int) -> set[str]:
    """Remote filenames that must NOT be deleted for this printer.

    Union of (a) the file currently loaded/printing on the printer
    (``state.gcode_file`` / ``state.subtask_name``) and (b) every file backing a
    non-terminal (pending/printing) queue item on this printer, resolved to its
    remote name exactly as ``print_scheduler`` derives it at dispatch.
    """
    from sqlalchemy import select

    from backend.app.models.archive import PrintArchive
    from backend.app.models.library import LibraryFile
    from backend.app.models.print_queue import PrintQueueItem

    in_use: set[str] = set()

    # (a) live loaded / printing file
    try:
        client = manager.get_client(printer_id)
    except Exception:  # noqa: BLE001
        client = None
    if client is not None and getattr(client, "state", None) is not None:
        in_use |= _name_variants(getattr(client.state, "gcode_file", None))
        in_use |= _name_variants(getattr(client.state, "subtask_name", None))

    # (b) files backing non-terminal queue items on this printer
    result = await db.execute(
        select(PrintQueueItem).where(
            PrintQueueItem.printer_id == printer_id,
            PrintQueueItem.status.in_(_NON_TERMINAL_QUEUE_STATUSES),
        )
    )
    for item in result.scalars().all():
        filename: str | None = None
        if item.archive_id is not None:
            archive = await db.get(PrintArchive, item.archive_id)
            if archive is not None:
                filename = archive.filename
        if filename is None and item.library_file_id is not None:
            lib = await db.get(LibraryFile, item.library_file_id)
            if lib is not None:
                filename = lib.filename
        in_use |= _name_variants(filename)

    return in_use


def _free_bytes_from(info: dict | None) -> int | None:
    """Extract ``free_bytes`` from a storage-info dict, or None when unknown."""
    if not info:
        return None
    value = info.get("free_bytes")
    return int(value) if isinstance(value, (int, float)) else None


async def _delete_old_recordings(ip: str, code: str, model: str | None, now: float, outcome: _CleanupOutcome) -> None:
    """Stage 1: delete recordings older than ``MIN_FILE_AGE_S`` from the camera dirs."""
    for directory in RECORDING_DIRS:
        entries = await list_files_async(ip, code, path=directory, printer_model=model)
        for entry in entries:
            if entry.get("is_directory"):
                continue
            mtime = entry.get("mtime")
            if mtime is None:
                # No timestamp → can't prove it's old enough; leave it alone.
                continue
            if now - mtime.timestamp() < MIN_FILE_AGE_S:
                continue
            path = entry.get("path")
            if not path:
                continue
            result = await delete_file_async(ip, code, path, printer_model=model)
            if result == DeleteResult.DELETED:
                size = int(entry.get("size") or 0)
                outcome.freed_bytes += size
                outcome.files_deleted += 1
                logger.warning("[USB-STORAGE] deleted recording %s (%d bytes)", path, size)
            elif result == DeleteResult.FAILED:
                outcome.failed_paths.append(path)
                logger.warning("[USB-STORAGE] failed to delete recording %s", path)


async def _delete_oldest_print_files(
    ip: str,
    code: str,
    model: str | None,
    now: float,
    free_bytes: int,
    in_use: set[str],
    outcome: _CleanupOutcome,
) -> int:
    """Stage 2: delete oldest UNUSED root print files until free >= TARGET_FREE_BYTES.

    Returns the projected free bytes after deletions (computed incrementally from
    the deleted files' sizes). Only ever called when ``free_bytes`` is known and
    below the target.
    """
    entries = await list_files_async(ip, code, path="/", printer_model=model)

    candidates = []
    for entry in entries:
        if entry.get("is_directory"):
            continue
        name = entry.get("name") or ""
        if not name.lower().endswith(_PRINT_FILE_SUFFIXES):
            continue
        mtime = entry.get("mtime")
        if mtime is None:
            continue  # can't age-check or oldest-sort — skip
        if now - mtime.timestamp() < MIN_FILE_AGE_S:
            continue
        # Exclude anything in use (current print + non-terminal queue items).
        name_keys = _name_variants(name)
        if name_keys & in_use:
            continue
        candidates.append(entry)

    # Oldest first.
    candidates.sort(key=lambda e: e["mtime"].timestamp())

    projected_free = free_bytes
    for entry in candidates:
        if projected_free >= TARGET_FREE_BYTES:
            break
        path = entry.get("path")
        if not path:
            continue
        result = await delete_file_async(ip, code, path, printer_model=model)
        if result == DeleteResult.DELETED:
            size = int(entry.get("size") or 0)
            outcome.freed_bytes += size
            outcome.files_deleted += 1
            projected_free += size
            logger.warning("[USB-STORAGE] deleted stale print file %s (%d bytes)", path, size)
        elif result == DeleteResult.FAILED:
            outcome.failed_paths.append(path)
            logger.warning("[USB-STORAGE] failed to delete print file %s", path)

    return projected_free


async def _perform_gated_cleanup(
    printer_id: int,
    manager,
    now: float,
    *,
    include_print_files: bool,
    notify: bool,
) -> str:
    """Shared gated cleanup core for the HMS trigger and the post-print drain.

    Runs the common gates (``farm_usb_auto_cleanup`` setting, actively-printing,
    per-printer cooldown, printer lookup), stamps the cooldown, then stage 1
    (recordings) and — only when ``include_print_files`` — stage 2 (oldest unused
    print files). Fires the failure-only notification when ``notify`` is True.
    Returns a status sentinel so the caller can react to the printing case
    (``"disabled"`` / ``"printing"`` / ``"cooldown"`` / ``"not_found"`` /
    ``"unreachable"`` / ``"done"``). Never fires a success notification — a
    successful cleanup leaves an INFO audit line only.
    """
    from backend.app.api.routes.settings import get_setting
    from backend.app.core.database import async_session
    from backend.app.models.printer import Printer

    async with async_session() as db:
        # Setting gate (default ON). A false-y "false" disables the whole feature.
        raw = await get_setting(db, "farm_usb_auto_cleanup")
        if raw is not None and str(raw).strip().lower() == "false":
            logger.info("[USB-STORAGE] auto-cleanup disabled; printer %s ignored", printer_id)
            return "disabled"

        # Actively-printing guard — never clean mid-print.
        if _is_printing(manager, printer_id):
            return "printing"

        # Per-printer cooldown (shared across HMS trigger + drain).
        last = _last_cleanup_at.get(printer_id)
        if last is not None and now - last < CLEANUP_COOLDOWN_S:
            logger.info("[USB-STORAGE] printer %s within cleanup cooldown; skipping", printer_id)
            return "cooldown"

        printer = await db.get(Printer, printer_id)
        if printer is None:
            logger.warning("[USB-STORAGE] printer %s not found; cannot clean", printer_id)
            return "not_found"

        printer_name = printer.name or f"printer {printer_id}"
        ip = printer.ip_address
        code = printer.access_code
        model = printer.model

        # Mark the attempt now so a re-arriving trigger within the window doesn't
        # re-run even if the work below is slow.
        _last_cleanup_at[printer_id] = now

        # Only stage 2 needs the in-use set; skip the queries for a recordings drain.
        in_use = await _in_use_remote_names(db, manager, printer_id) if include_print_files else set()

    # --- FTP work happens outside the DB session -----------------------------
    outcome = _CleanupOutcome()

    if not await _ftp_reachable(ip, code, model):
        outcome.success = False
        outcome.reason = "printer FTPS unreachable (USB drive missing or port 990 down)"
        logger.warning("[USB-STORAGE] printer %s FTPS unreachable; cannot clean", printer_id)
        if notify:
            await _fire_notification(printer_id, printer_name, outcome)
        return "unreachable"

    # Stage 1: recordings.
    await _delete_old_recordings(ip, code, model, now, outcome)

    # Stage 2 only when print files are in scope AND free space is known-low.
    if include_print_files:
        outcome.free_bytes = _free_bytes_from(await get_storage_info_async(ip, code, printer_model=model))
        if outcome.free_bytes is not None and outcome.free_bytes < TARGET_FREE_BYTES:
            outcome.free_bytes = await _delete_oldest_print_files(
                ip, code, model, now, outcome.free_bytes, in_use, outcome
            )

    # Success = we made progress (freed something) OR space is now at/above target.
    space_ok = outcome.free_bytes is not None and outcome.free_bytes >= TARGET_FREE_BYTES
    if outcome.freed_bytes > 0 or space_ok:
        outcome.success = True
    else:
        outcome.success = False
        if outcome.failed_paths:
            outcome.reason = f"{len(outcome.failed_paths)} file(s) could not be deleted"
        else:
            outcome.reason = "no cleanable recordings or unused print files found"

    logger.info(
        "[USB-STORAGE] printer %s cleanup done: freed %d bytes across %d file(s), free=%s, success=%s",
        printer_id,
        outcome.freed_bytes,
        outcome.files_deleted,
        outcome.free_bytes,
        outcome.success,
    )
    # Failures-only: a routine success is the INFO line above, no notification.
    if notify and not outcome.success:
        await _fire_notification(printer_id, printer_name, outcome)
    return "done"


async def on_storage_low(
    printer_id: int,
    full_codes: set[str],
    *,
    manager=None,
    now: float | None = None,
) -> None:
    """React to a new USB-storage-low HMS code on ``printer_id``.

    Respects the ``farm_usb_auto_cleanup`` setting, a per-printer cooldown, and an
    actively-printing guard (all no-ops; printing records a deferral so the main.py
    hook retries post-print). Otherwise runs stage 1 (recordings) then, only if free
    space is still known-low, stage 2 (oldest unused print files), and fires the
    dedicated ``on_storage_low`` notification ONLY on FAILURE. Single-flight per
    printer. Never raises — an unreachable FTPS printer becomes the failure
    notification.
    """
    if not (set(full_codes) & HMS_STORAGE_LOW_FULL_CODES):
        return
    # Single-flight: check-and-add synchronously, before any await.
    if printer_id in _inflight:
        logger.debug("[USB-STORAGE] cleanup already in-flight for printer %s; skipping trigger", printer_id)
        return
    _inflight.add(printer_id)
    if manager is None:
        manager = printer_manager
    now = time.time() if now is None else now

    try:
        # Consume any pending deferral for this attempt: only the printing outcome
        # below re-arms it. Guards that intentionally swallow the attempt (disabled
        # setting, cooldown) leave it cleared, or the main.py retry hook would
        # re-fire a DB-opening task on every status tick forever.
        _deferred_printers.discard(printer_id)

        status = await _perform_gated_cleanup(printer_id, manager, now, include_print_files=True, notify=True)
        if status == "printing":
            # Never clean mid-print — record the deferral so the main.py hook can
            # retry once the print ends.
            _deferred_printers.add(printer_id)
            logger.info("[USB-STORAGE] printer %s is printing; deferring cleanup", printer_id)
    except Exception:  # noqa: BLE001 — the storage hook must NEVER crash the status flow
        logger.exception("[USB-STORAGE] cleanup failed unexpectedly for printer %s", printer_id)
        try:
            await _fire_notification(
                printer_id,
                f"printer {printer_id}",
                _CleanupOutcome(success=False, reason="unexpected error during cleanup"),
            )
        except Exception:  # noqa: BLE001
            logger.exception("[USB-STORAGE] failure notification also failed for printer %s", printer_id)
    finally:
        _inflight.discard(printer_id)


async def drain_recordings_if_idle(
    printer_id: int,
    *,
    manager=None,
    now: float | None = None,
) -> None:
    """Opportunistic post-print recordings drain (stage 1 only).

    Fired fire-and-forget from ``main.on_print_complete`` for terminal prints to
    trim the ``/ipcam`` recordings a long lights-out run accumulates BEFORE the USB
    hits the HMS-full wall. Respects the SAME gates as the HMS-triggered cleanup
    (``farm_usb_auto_cleanup`` setting, not-printing, the 6h cooldown, and the
    single-flight) but never touches print files and NEVER notifies — success is an
    INFO line, a failure is silent (the HMS/drop-triggered paths own alerting).
    Never raises.
    """
    if manager is None:
        manager = printer_manager
    now = time.time() if now is None else now
    # Single-flight: check-and-add synchronously, before any await.
    if printer_id in _inflight:
        logger.debug("[USB-STORAGE] cleanup already in-flight for printer %s; skipping drain", printer_id)
        return
    _inflight.add(printer_id)
    try:
        await _perform_gated_cleanup(printer_id, manager, now, include_print_files=False, notify=False)
    except Exception:  # noqa: BLE001 — the drain is opportunistic; never crash the completion flow
        logger.exception("[USB-STORAGE] recordings drain failed unexpectedly for printer %s", printer_id)
    finally:
        _inflight.discard(printer_id)


async def _fire_notification(printer_id: int, printer_name: str, outcome: _CleanupOutcome) -> None:
    """Send the dedicated ``on_storage_low`` notification for a cleanup outcome."""
    from backend.app.core.database import async_session
    from backend.app.services.notification_service import notification_service

    async with async_session() as db:
        await notification_service.on_storage_low(
            printer_id,
            printer_name,
            success=outcome.success,
            freed_bytes=outcome.freed_bytes,
            files_deleted=outcome.files_deleted,
            free_bytes=outcome.free_bytes,
            reason=outcome.reason,
            db=db,
        )
