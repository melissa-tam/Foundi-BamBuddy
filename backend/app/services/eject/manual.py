"""Manual "Eject now" — farm-known finished unit (W2) OR a confirmed foreign plate.

An operator affordance to trigger the part-present eject sweep by hand — either to
skip the cooldown wait (with an explicit hot-bed confirm) or to clear a plate the
automatic watch could not. For a FARM-KNOWN part the target unit is resolved from the
armed cooldown watch's identity or, failing that, from the last completed
eject-profiled unit whose dispatch subtask raised the current plate gate; an unapproved
first article still goes through the approval flow (never weakened into a blind sweep).

When NO farm-known unit resolves, the gate may instead have been raised by a FOREIGN
print (started in Bambu Studio, not farm-dispatched). That is a deliberate two-step
confirm: the first call resolves the foreign print's donor file from the print archive
and raises :class:`ForeignPlateEject` (carrying the print name, parsed max Z height,
and a suggested profile) so the UI can render an eject-profile picker; the second call
supplies the chosen ``eject_profile_id`` and dispatches the sweep. A farm-known-but-
ineligible gate (e.g. an unapproved first article) is NEVER treated as foreign.

The MANUAL foreign path carries one extra, purely-local fallback the fail-closed AUTO
path does NOT: a screen-RESTART of the farm's OWN USB file echoes a degenerate identity
(empty ``subtask_id``, ``subtask_name="project_file"``), so the gate id is blank AND the
auto-archive is the download-failed fallback row (``file_path=""``) — the strict archive
resolver finds nothing usable. But the farm still KNOWS what is on the plate: the most-
recently-started queue item on the printer carries the library/archive it was dispatched
from. When the strict resolver 409s, :func:`_manual_eject_foreign` falls back to
:func:`_resolve_foreign_source_from_last_farm_item` (on-disk donor only) before giving up;
if THAT can't resolve either, the original 409 is re-raised unchanged.

The service composes the existing eject primitives — it NEVER hand-rolls a dispatch.
When a cooldown watch is armed it merely signals that watch's single ``_do_release``
path (``request_release_now``) so there is no parallel dispatch race; otherwise it
calls the shared ``eject_remote.dispatch_part_present_eject`` (farm-known) or
``eject_remote.dispatch_foreign_eject`` (foreign plate) directly.

Ordered precondition checks each raise :class:`ManualEjectError` (a plain domain
error carrying a stable machine code + HTTP status hint); the route translates them
to ``HTTPException``. :class:`BedTooHot` fires only with a REAL live bed reading
above the threshold and carries bed + threshold so the UI can show the confirm
dialog with true numbers; an unreadable bed is its own ``bed_unreadable`` 409 (a
retry-in-a-moment condition, never a confirm prompt built on a missing reading).
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.models.eject_profile import EjectProfile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services.eject import remote as eject_remote
from backend.app.services.eject.geometry import GeometryUnavailable, get_geometry_required
from backend.app.services.eject.monitor import _latest_started_item, _resolve_eject_threshold, eject_cooldown_monitor
from backend.app.services.printer_manager import printer_manager
from backend.app.utils.filename import derive_remote_filename
from backend.app.utils.threemf_tools import list_gcode_plate_ids, read_plate_gcode_header

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# The single no_eligible_unit message (a farm-known-but-ineligible gate — e.g. an
# unapproved first article — is NEVER weakened into a foreign sweep).
_NO_ELIGIBLE_MSG = "No farm-known finished unit to eject on this printer (first articles use the approval flow)"
# Shown when a foreign plate is the gate source but its donor file / plate / height
# can't be resolved — the operator's only safe move is the by-hand clear.
_FOREIGN_UNRESOLVABLE_MSG = (
    "Could not resolve the file for the plate on this printer — use Mark plate as cleared and remove the part by hand"
)


class ManualEjectError(RuntimeError):
    """A manual eject could not be started.

    ``code`` is a stable machine-readable reason (``not_found`` / ``not_connected``
    / ``printer_busy`` / ``no_plate_gate`` / ``eject_in_flight`` / ``no_eligible_unit``
    / ``bed_unreadable`` / ``profile_not_found`` / ``bed_hot`` / ``foreign_plate``) and
    ``status_code`` the HTTP hint the route applies without this module importing
    FastAPI.
    """

    def __init__(self, code: str, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class BedTooHot(ManualEjectError):
    """A REAL live bed reading is above the release threshold and the caller did not
    pass ``allow_hot`` — carries the live bed + threshold (both always finite floats)
    so the UI can render the explicit hot-bed confirm dialog with true numbers. An
    unreadable bed never raises this (it raises ``bed_unreadable`` instead)."""

    def __init__(self, bed_c: float, threshold_c: float) -> None:
        super().__init__(
            "bed_hot",
            f"Bed is {bed_c:.1f}°C, above the {threshold_c:.1f}°C eject threshold — confirm to eject hot",
            status_code=409,
        )
        self.bed_c = bed_c
        self.threshold_c = threshold_c


class ForeignPlateEject(ManualEjectError):
    """The raised plate gate came from a FOREIGN print (started in Bambu Studio, not
    farm-dispatched) and the caller supplied no ``eject_profile_id`` — the FIRST call
    of the two-step confirm. Carries the resolved foreign source so the route/UI can
    render an eject-profile picker with the print name, the plate's parsed max Z
    height, and a suggested profile (the printer's last eject-profiled unit). The
    caller re-calls with a chosen ``eject_profile_id`` to actually dispatch."""

    def __init__(
        self,
        *,
        print_name: str | None,
        max_z_height_mm: float | None,
        suggested_eject_profile_id: int | None,
    ) -> None:
        super().__init__(
            "foreign_plate",
            "This plate was started outside the farm — confirm an eject profile to sweep it, "
            "or use Mark plate as cleared and remove the part by hand",
            status_code=409,
        )
        self.print_name = print_name
        self.max_z_height_mm = max_z_height_mm
        self.suggested_eject_profile_id = suggested_eject_profile_id


@dataclass(frozen=True)
class _ForeignSource:
    """A foreign plate's resolved eject donor: the on-disk (or FTPS re-fetched) source
    ``.gcode.3mf``, its ejectable plate id, the plate's parsed ``max_z`` height, the
    print name for the confirm dialog, and — when the donor was re-fetched — the temp
    path the caller must clean up (``None`` when the donor was the on-disk archive)."""

    donor_path: Path
    plate_id: int
    max_z: float
    print_name: str | None
    tmp_path: Path | None


def _safe_unlink(path: Path | None) -> None:
    """Best-effort delete of a temp donor file (no-op for None / on-disk archives)."""
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Foreign-donor FTPS re-fetch cache (latency Phase D1)
# --------------------------------------------------------------------------- #
# The foreign "Eject now" flow resolves a donor 3MF TWICE: once on the confirm-409
# (which may FTPS-download the donor) and again on the confirmed POST; the AUTO
# foreign path (identify → cooldown watch → dispatch) fetches twice for the same
# reason. This module-level TTL cache lets the FIRST resolve DEPOSIT the fetched temp
# file and the SECOND resolve CONSUME it — turning two FTPS downloads into one.
#
# Key = ``(printer_id, gate_subtask)`` where ``gate_subtask`` is the printer's
# ``plate_gate_subtask_id`` — the SAME gate both the 409 and the confirm operate on
# (one gate per printer). A gate RE-RAISE for a DIFFERENT subtask yields a different
# key, so a stale donor is never served for a new foreign print. Only the expensive
# re-fetch (``tmp_path is not None``) is ever cached; an on-disk archive donor (nothing
# to clean up) is a no-op deposit. Entries expire after ``_FOREIGN_DONOR_TTL_S`` and are
# swept (files unlinked) lazily on every access. A CONSUMED entry is removed — the
# consumer owns the file's lifecycle exactly as a fresh fetch does today.
_FOREIGN_DONOR_TTL_S = 600.0  # ~10 min
_foreign_donor_cache: dict[tuple[int, str], tuple[Path, float]] = {}


def _foreign_cache_key(printer: Printer) -> tuple[int, str]:
    """The cache key for ``printer``'s current foreign-plate gate."""
    return (printer.id, printer.plate_gate_subtask_id or "")


def _sweep_expired_donors(now: float) -> None:
    """Unlink + drop every cache entry past its TTL (lazy sweep, on each access)."""
    for key in [k for k, (_p, exp) in _foreign_donor_cache.items() if exp <= now]:
        path, _exp = _foreign_donor_cache.pop(key)
        _safe_unlink(path)


def _foreign_cache_put(key: tuple[int, str], path: Path | None) -> None:
    """DEPOSIT a re-fetched donor temp file under ``key`` (no-op for ``None``).

    An existing entry for the key is unlinked first (never leak a superseded temp)."""
    now = time.monotonic()
    _sweep_expired_donors(now)
    if path is None:
        return
    existing = _foreign_donor_cache.pop(key, None)
    if existing is not None and existing[0] != path:
        _safe_unlink(existing[0])
    _foreign_donor_cache[key] = (path, now + _FOREIGN_DONOR_TTL_S)


def _foreign_cache_take(key: tuple[int, str]) -> Path | None:
    """CONSUME (pop) the cached donor for ``key`` — the caller now owns the file.

    Returns the path when a live, on-disk entry exists; ``None`` on miss / expiry /
    a vanished file (a stale entry is unlinked and dropped)."""
    _sweep_expired_donors(time.monotonic())
    entry = _foreign_donor_cache.pop(key, None)
    if entry is None:
        return None
    path, _exp = entry
    if not path.is_file():
        _safe_unlink(path)
        return None
    return path


def _thermal_gate(state, threshold: float, *, allow_hot: bool) -> None:
    """The shared hot-bed precondition, reused by the farm-known and foreign paths.

    ``allow_hot`` skips it entirely. An unreadable live bed is a retryable
    ``bed_unreadable`` 409 (never a confirm dialog built on a missing reading); a real
    reading above ``threshold`` raises :class:`BedTooHot` carrying live bed + threshold.
    """
    if allow_hot:
        return
    bed = state.temperatures.get("bed") if state is not None and getattr(state, "connected", False) else None
    if bed is None:
        raise ManualEjectError(
            "bed_unreadable",
            "Live bed temperature is unavailable; wait a few seconds for printer telemetry and retry",
            status_code=409,
        )
    if bed > threshold:
        raise BedTooHot(bed, threshold)


async def _resolve_manual_eject_item(db: AsyncSession, printer_id: int) -> int | None:
    """Resolve the farm-known unit to eject on ``printer_id``, or None if none eligible.

    Prefers the armed PRODUCTION cooldown watch's ``queue_item_id`` (the unit the
    watch is already cooling for). Falls back to the ``should_rearm``-style DB lookup:
    the most-recently started unit, which must be a COMPLETED, eject-profiled,
    NON-first-article unit whose ``dispatch_subtask_id`` matches the printer's
    ``plate_gate_subtask_id`` (the gate this eject would clear). An unapproved first
    article is deliberately excluded — it must use the approval flow."""
    identity = eject_cooldown_monitor.active_watch_identity(printer_id)
    if identity is not None and identity.purpose == "production" and identity.queue_item_id is not None:
        return identity.queue_item_id

    printer = await db.get(Printer, printer_id)
    item = await _latest_started_item(db, printer_id)
    if printer is None or item is None:
        return None
    if item.status != "completed" or item.eject_profile_id is None or item.first_article:
        return None
    if not printer.plate_gate_subtask_id or item.dispatch_subtask_id != printer.plate_gate_subtask_id:
        return None
    return item.id


async def manual_eject(
    db: AsyncSession, printer_id: int, *, allow_hot: bool = False, eject_profile_id: int | None = None
) -> dict:
    """Trigger a part-present eject on ``printer_id`` — farm-known unit OR foreign plate.

    Ordered 409 preconditions: printer known → connected → not RUNNING/PAUSE → plate
    gate raised → no eject already in flight. Then a farm-known eligible unit is
    resolved:

    * **Farm-known unit** → thermal check (skipped by ``allow_hot``) then either signal
      an armed cooldown watch's single release path or dispatch directly. Returns
      ``{"mode": "released_watch"|"dispatched", "queue_item_id": int}``.
    * **No farm-known unit** → the two-step FOREIGN-plate flow (``_manual_eject_foreign``):
      a farm-known-but-ineligible gate (e.g. an unapproved first article) still 409s
      ``no_eligible_unit``; a genuine foreign plate with no ``eject_profile_id`` raises
      :class:`ForeignPlateEject` (the confirm prompt), and with one dispatches the eject
      returning ``{"mode": "dispatched", "queue_item_id": None}``.
    """
    printer = await db.get(Printer, printer_id)
    if printer is None:
        raise ManualEjectError("not_found", "Printer not found", status_code=404)
    if not printer_manager.is_connected(printer_id):
        raise ManualEjectError("not_connected", "Printer is not connected; cannot eject", status_code=409)

    state = printer_manager.get_status(printer_id)
    if state is not None and getattr(state, "state", None) in ("RUNNING", "PAUSE"):
        raise ManualEjectError("printer_busy", "Printer is printing or paused; cannot eject now", status_code=409)

    if not printer_manager.is_awaiting_plate_clear(printer_id):
        raise ManualEjectError(
            "no_plate_gate", "Printer is not awaiting plate clear; nothing to eject", status_code=409
        )

    if eject_remote.peek_pending_eject(printer_id) is not None:
        raise ManualEjectError("eject_in_flight", "An eject is already in flight on this printer", status_code=409)

    queue_item_id = await _resolve_manual_eject_item(db, printer_id)
    if queue_item_id is None:
        # No farm-known finished unit → the foreign-plate two-step flow (or a firm 409
        # for a farm-known-but-ineligible gate like an unapproved first article).
        return await _manual_eject_foreign(db, printer, state, allow_hot=allow_hot, eject_profile_id=eject_profile_id)

    threshold = await _resolve_eject_threshold(queue_item_id)
    if threshold is None:
        raise ManualEjectError("no_eligible_unit", "Unit has no eject profile; cannot eject", status_code=409)

    _thermal_gate(state, threshold, allow_hot=allow_hot)

    # Armed PRODUCTION watch → drive its single _do_release path (no parallel race).
    identity = eject_cooldown_monitor.active_watch_identity(printer_id)
    if identity is not None and identity.purpose == "production" and identity.queue_item_id == queue_item_id:
        if eject_cooldown_monitor.request_release_now(printer_id):
            logger.info(
                "manual_eject: signalled immediate release on printer %s (watch armed, item %s)",
                printer_id,
                queue_item_id,
            )
            return {"mode": "released_watch", "queue_item_id": queue_item_id}

    # No armed watch (the DB-fallback path) → dispatch directly. EjectDispatchError
    # propagates for the route to translate to its status hint.
    item = await db.get(PrintQueueItem, queue_item_id)
    run_id = item.batch_id if item is not None else None
    await eject_remote.dispatch_part_present_eject(
        db, printer_id=printer_id, queue_item_id=queue_item_id, purpose="production", run_id=run_id
    )
    logger.info("manual_eject: dispatched part-present eject on printer %s for item %s", printer_id, queue_item_id)
    return {"mode": "dispatched", "queue_item_id": queue_item_id}


# --------------------------------------------------------------------------- #
# Foreign-plate "Eject now" (warn + confirm with a selectable profile)
# --------------------------------------------------------------------------- #
async def _suggest_eject_profile_id(db: AsyncSession, printer_id: int) -> int | None:
    """The eject profile to pre-select in the foreign-plate confirm dialog: the most
    recently started eject-profiled unit on this printer (best guess of the operator's
    usual profile), or None when the printer has never run an eject-profiled unit."""
    result = await db.execute(
        select(PrintQueueItem.eject_profile_id)
        .where(
            PrintQueueItem.printer_id == printer_id,
            PrintQueueItem.eject_profile_id.is_not(None),
            PrintQueueItem.started_at.is_not(None),
        )
        .order_by(PrintQueueItem.started_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _fetch_foreign_donor(printer: Printer, filename: str | None) -> Path | None:
    """FTPS re-fetch the foreign print's donor file by ``filename`` into a temp file.

    Walks the standard FTPS root/cache/model/data fan-out over one connection (the
    printer's FTPS root IS the USB drive). Returns the temp :class:`Path` on success
    (caller owns cleanup) or None when the name is missing / the file is unfetchable.
    """
    from backend.app.core.config import settings as app_settings
    from backend.app.services.bambu_ftp import download_file_try_paths_async

    if not filename:
        return None
    remote_paths = [f"/{filename}", f"/cache/{filename}", f"/model/{filename}", f"/data/{filename}"]
    temp_dir = app_settings.archive_dir / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = temp_dir / f"foreign_eject_{printer.id}_{Path(filename).name}"
    try:
        ok = await download_file_try_paths_async(
            printer.ip_address,
            printer.access_code,
            remote_paths,
            temp_path,
            printer_model=printer.model,
        )
    except Exception as exc:  # noqa: BLE001 — any transport failure → unfetchable
        logger.warning("manual_eject: FTPS re-fetch of foreign donor %r failed: %s", filename, exc)
        _safe_unlink(temp_path)
        return None
    if not ok:
        _safe_unlink(temp_path)
        return None
    return temp_path


def _resolve_foreign_plate_id(donor_path: Path, filename: str | None) -> int | None:
    """Pick the ejectable plate id for a foreign donor, or None if unresolvable.

    Prefers a ``plate_(\\d+)`` hint in the filename WHEN that plate actually carries
    G-code; otherwise falls back to the single G-code-bearing plate. Returns None when
    the file has no G-code plate or the hint is absent and the choice is ambiguous
    (multiple G-code plates) — a blind sweep is never guessed."""
    plates = list_gcode_plate_ids(donor_path)
    if not plates:
        return None
    m = re.search(r"plate_(\d+)", str(filename or ""))
    if m:
        hinted = int(m.group(1))
        if hinted in plates:
            return hinted
    if len(plates) == 1:
        return plates[0]
    return None


async def _resolve_foreign_source(db: AsyncSession, printer: Printer) -> _ForeignSource:
    """Resolve a foreign plate's eject donor from the print archive that raised the gate.

    (a) newest ``PrintArchive`` whose ``subtask_id`` == the printer's gate subtask AND
    ``printer_id`` == this printer; (b) donor = the on-disk archive file if present,
    else an FTPS re-fetch by ``filename``; (c) plate id from the filename hint / single
    G-code plate; (d) ``max_z`` from the plate's G-code header. Any unresolved step
    raises ``no_eligible_unit`` with the actionable by-hand-clear message (and cleans up
    a temp re-fetch)."""
    from backend.app.core.config import settings as app_settings

    gate = printer.plate_gate_subtask_id
    if not gate:
        raise ManualEjectError("no_eligible_unit", _FOREIGN_UNRESOLVABLE_MSG, status_code=409)

    result = await db.execute(
        select(PrintArchive)
        .where(PrintArchive.subtask_id == gate, PrintArchive.printer_id == printer.id)
        .order_by(PrintArchive.id.desc())
        .limit(1)
    )
    archive = result.scalar_one_or_none()
    if archive is None:
        raise ManualEjectError("no_eligible_unit", _FOREIGN_UNRESOLVABLE_MSG, status_code=409)

    # (b) donor file — on disk if the archive copy exists, else a Phase-D1 cached
    # re-fetch, else a fresh FTPS re-fetch. A download-failed archive carries
    # file_path="" (the fallback row), so guard on is_file(), never bare exists()
    # (base_dir/"" is a directory).
    donor_path: Path | None = None
    tmp_path: Path | None = None
    if archive.file_path:
        disk = app_settings.base_dir / archive.file_path
        if disk.is_file():
            donor_path = disk
    if donor_path is None:
        # A prior resolve (the confirm-409, or the auto path's identify) may have
        # DEPOSITED the fetched donor for this exact gate — consume it and skip the
        # second download entirely (caller owns the file, same as a fresh fetch).
        tmp_path = _foreign_cache_take(_foreign_cache_key(printer))
        if tmp_path is None:
            tmp_path = await _fetch_foreign_donor(printer, archive.filename)
        if tmp_path is None:
            raise ManualEjectError("no_eligible_unit", _FOREIGN_UNRESOLVABLE_MSG, status_code=409)
        donor_path = tmp_path

    # (c) plate id, then (d) max_z — clean up a temp re-fetch on any failure.
    plate_id = _resolve_foreign_plate_id(donor_path, archive.filename)
    if plate_id is None:
        _safe_unlink(tmp_path)
        raise ManualEjectError("no_eligible_unit", _FOREIGN_UNRESOLVABLE_MSG, status_code=409)

    header = read_plate_gcode_header(donor_path, plate_id)
    raw = header.get("max_z_height")
    try:
        max_z = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        max_z = None
    if max_z is None:
        _safe_unlink(tmp_path)
        raise ManualEjectError("no_eligible_unit", _FOREIGN_UNRESOLVABLE_MSG, status_code=409)

    return _ForeignSource(
        donor_path=donor_path,
        plate_id=plate_id,
        max_z=max_z,
        print_name=archive.print_name,
        tmp_path=tmp_path,
    )


async def _resolve_foreign_source_from_last_farm_item(db: AsyncSession, printer: Printer) -> _ForeignSource | None:
    """MANUAL-only donor fallback: resolve the plate from the printer's last farm item.

    The strict :func:`_resolve_foreign_source` ties the donor to the *archive that
    raised the gate*. That fails for the screen-RESTART incident shape — a blank gate
    id + a download-failed fallback archive (``file_path=""``) carry no usable donor.
    The farm still knows the plate's contents though: the most-recently-started queue
    item on this printer records the library file / archive it was dispatched from.

    Donor resolution is ON DISK ONLY (never an FTPS re-fetch — the strict path already
    tried the wire; this is the last, purely-local fallback), in priority order:

    * (a) ``item.archive_id`` → :class:`PrintArchive` whose ``file_path`` is non-empty
      and exists on disk (``base_dir / file_path``);
    * (b) else ``item.library_file_id`` → :class:`LibraryFile` resolved with the
      established absolute-or-``base_dir`` pattern (``print_scheduler`` line ~1249);
    * (c) neither on disk → ``None``.

    Plate id prefers the item's own ``plate_id`` when the donor actually carries it,
    else the filename-hint / single-G-code-plate resolution (never a blind guess); the
    plate's ``max_z`` is parsed from its G-code header exactly like the strict resolver.
    Any unresolved step returns ``None`` so the caller re-raises the ORIGINAL strict 409
    (the by-hand-clear behaviour is unchanged when the plate is genuinely unresolvable).

    The returned donor is always on disk, so ``tmp_path`` is ``None`` — there is nothing
    for the caller to clean up. Used ONLY by :func:`_manual_eject_foreign`; the strict
    resolver stays fail-closed for the AUTO foreign-eject path (a farm red line)."""
    from backend.app.core.config import settings as app_settings
    from backend.app.models.library import LibraryFile

    item = await _latest_started_item(db, printer.id)
    if item is None:
        return None

    # (a)/(b) donor on disk — the archived copy is preferred, else the library source.
    donor_path: Path | None = None
    display_name: str | None = None  # names the donor for the plate-id filename hint
    print_name: str | None = None  # the operator-facing name for the confirm dialog
    if item.archive_id is not None:
        archive = await db.get(PrintArchive, item.archive_id)
        if archive is not None and archive.file_path:
            disk = app_settings.base_dir / archive.file_path
            if disk.is_file():
                donor_path = disk
                display_name = archive.filename
                print_name = archive.print_name or archive.filename
    if donor_path is None and item.library_file_id is not None:
        library_file = await db.get(LibraryFile, item.library_file_id)
        if library_file is not None:
            lib_path = Path(library_file.file_path)
            resolved = lib_path if lib_path.is_absolute() else app_settings.base_dir / library_file.file_path
            if resolved.is_file():
                donor_path = resolved
                display_name = library_file.filename
                print_name = library_file.filename
    if donor_path is None:
        return None

    # Plate id: the item's own plate when the donor actually carries it, else the
    # filename-hint / single-G-code-plate resolution — never a blind guess.
    plates = list_gcode_plate_ids(donor_path)
    if not plates:
        return None
    if item.plate_id is not None and item.plate_id in plates:
        plate_id = item.plate_id
    else:
        plate_id = _resolve_foreign_plate_id(donor_path, display_name)
        if plate_id is None:
            return None

    # max_z from the plate's G-code header, parsed exactly as _resolve_foreign_source.
    header = read_plate_gcode_header(donor_path, plate_id)
    raw = header.get("max_z_height")
    try:
        max_z = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        max_z = None
    if max_z is None:
        return None

    return _ForeignSource(donor_path=donor_path, plate_id=plate_id, max_z=max_z, print_name=print_name, tmp_path=None)


async def _manual_eject_foreign(
    db: AsyncSession,
    printer: Printer,
    state,
    *,
    allow_hot: bool,
    eject_profile_id: int | None,
) -> dict:
    """The foreign-plate branch of ``manual_eject`` (called when no farm-known unit
    resolves). A farm-known-but-ineligible gate (a queue item stamped with the gate's
    subtask — e.g. an unapproved first article) is NEVER weakened into a foreign sweep:
    it keeps today's ``no_eligible_unit`` 409. Otherwise the foreign donor is resolved
    — the strict archive resolver first, then the on-disk last-farm-item fallback for the
    screen-RESTART shape the strict resolver can't tie (:func:`_resolve_foreign_source_from_last_farm_item`)
    — and with no ``eject_profile_id`` the confirm prompt (:class:`ForeignPlateEject`) is
    raised; with one the eject is dispatched via ``dispatch_foreign_eject``."""
    gate = printer.plate_gate_subtask_id
    if gate:
        known = await db.execute(select(PrintQueueItem.id).where(PrintQueueItem.dispatch_subtask_id == gate).limit(1))
        if known.scalar_one_or_none() is not None:
            raise ManualEjectError("no_eligible_unit", _NO_ELIGIBLE_MSG, status_code=409)

    try:
        source = await _resolve_foreign_source(db, printer)
    except ManualEjectError as strict_err:
        # The strict resolver (shared, fail-closed, with the AUTO path) could not tie the
        # gate to a donor — the screen-RESTART incident: a blank gate id + a download-
        # failed fallback archive. Try the on-disk last-farm-item fallback; if THAT can't
        # resolve either, re-raise the ORIGINAL error so the 409 message/behaviour is
        # unchanged when the plate is genuinely unresolvable.
        source = await _resolve_foreign_source_from_last_farm_item(db, printer)
        if source is None:
            raise strict_err

    # First call (no profile chosen) → the confirm prompt. DEPOSIT the temp re-fetch
    # (Phase D1) keyed by this gate so the confirm call consumes it instead of
    # re-downloading; an on-disk archive donor (tmp_path=None) deposits nothing.
    if eject_profile_id is None:
        _foreign_cache_put(_foreign_cache_key(printer), source.tmp_path)
        suggested = await _suggest_eject_profile_id(db, printer.id)
        raise ForeignPlateEject(
            print_name=source.print_name,
            max_z_height_mm=source.max_z,
            suggested_eject_profile_id=suggested,
        )

    profile = await db.get(EjectProfile, eject_profile_id)
    if profile is None:
        _safe_unlink(source.tmp_path)
        raise ManualEjectError("profile_not_found", f"Eject profile {eject_profile_id} not found", status_code=404)

    try:
        _thermal_gate(state, profile.cooldown_temp_c, allow_hot=allow_hot)
        await eject_remote.dispatch_foreign_eject(
            db,
            printer_id=printer.id,
            profile_id=eject_profile_id,
            source_path=source.donor_path,
            plate_id=source.plate_id,
        )
    finally:
        _safe_unlink(source.tmp_path)

    logger.info(
        "manual_eject: dispatched foreign-plate eject on printer %s (plate %s, profile %s)",
        printer.id,
        source.plate_id,
        eject_profile_id,
    )
    return {"mode": "dispatched", "queue_item_id": None}


# --------------------------------------------------------------------------- #
# Auto foreign-eject: a foreign completion that is positively the farm's OWN file
# (2026-07-18 decision) is auto-ejected after cooldown, no operator confirm.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ForeignFarmFile:
    """A foreign plate positively identified as the farm's OWN file — safe to
    auto-eject after cooldown. Carries the chosen eject ``profile_id``, the release
    ``threshold_c`` (the profile's ``cooldown_temp_c``) and the print name for logs."""

    profile_id: int
    threshold_c: float
    print_name: str | None


def _canonical_names(*names: str | None) -> set[str]:
    """The set of canonical (``derive_remote_filename``) forms of ``names``, blanks
    skipped. Folds spaces→underscores and normalises the 3MF/gcode suffix so a
    screen-started print's UNDERSCORED USB echo and the farm's SPACED library/archive
    filename compare equal — the extension/underscore canonicalisation
    ``farm_correlation._normalize_name`` deliberately does NOT do."""
    out: set[str] = set()
    for name in names:
        if not name:
            continue
        base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        try:
            out.add(derive_remote_filename(base))
        except TypeError:  # non-str duck type — skip rather than crash the identity check
            continue
    return out


async def _farm_dispatched_names(db: AsyncSession, printer_id: int) -> set[str]:
    """Canonical names of every FARM file dispatched to ``printer_id``: the library
    filenames and archive filenames of the printer's farm queue items (a batch with a
    ``sku_file_id``, or an item carrying an ``eject_profile_id``). The identity corpus
    a foreign completion's echoed name is checked against before auto-ejecting it."""
    from backend.app.models.library import LibraryFile
    from backend.app.models.print_batch import PrintBatch

    result = await db.execute(
        select(PrintQueueItem)
        .outerjoin(PrintBatch, PrintQueueItem.batch_id == PrintBatch.id)
        .where(PrintQueueItem.printer_id == printer_id)
        .where((PrintQueueItem.eject_profile_id.is_not(None)) | (PrintBatch.sku_file_id.is_not(None)))
    )
    names: set[str] = set()
    for item in result.scalars().all():
        if item.archive_id is not None:
            archive = await db.get(PrintArchive, item.archive_id)
            if archive is not None:
                names |= _canonical_names(archive.filename)
        if item.library_file_id is not None:
            library_file = await db.get(LibraryFile, item.library_file_id)
            if library_file is not None:
                names |= _canonical_names(library_file.filename)
    return names


async def identify_farm_file_foreign(
    db: AsyncSession, printer_id: int, *, subtask_name: str | None, filename: str | None
) -> ForeignFarmFile | None:
    """Decide whether a FOREIGN completion is positively the farm's OWN file, so the
    farm may auto-eject it after cooldown instead of only escalating (2026-07-18).

    Returns a :class:`ForeignFarmFile` (profile + release threshold) ONLY when ALL of:

      (a) the echoed ``subtask_name``/``filename`` matches a file the farm has
          dispatched to THIS printer, both sides canonicalised through
          ``derive_remote_filename`` (a screen-started print echoes the UNDERSCORED
          USB name; the farm library stores the SPACED display name — only this
          canonicalisation makes them compare equal);
      (b) the printer model's geometry row is hardware-``validated`` (production eject
          never runs on an unvalidated model);
      (c) a suggested eject profile exists for this printer;
      (d) the foreign donor resolves and its parsed max Z height is within that
          profile's ``max_part_height_mm`` guard.

    Any miss → None (the caller falls back to the escalation-only hold). The cheap
    checks run BEFORE the donor resolution (which may FTPS re-fetch) so the common
    negative — a genuinely foreign print — exits fast without touching the wire. The
    helper opens no session of its own (the caller owns ``db``, per convention) and
    cleans up any temp re-fetch it makes."""
    # (a) name match against farm-dispatched files on this printer — the strongest,
    # cheapest signal, so it gates everything else.
    echoed = _canonical_names(subtask_name, filename)
    if not echoed:
        return None
    if echoed.isdisjoint(await _farm_dispatched_names(db, printer_id)):
        return None

    printer = await db.get(Printer, printer_id)
    if printer is None:
        return None

    # (b) model geometry must be hardware-validated (fail-closed, never auto-eject an
    # unvalidated model's envelope).
    try:
        await get_geometry_required(db, printer.model, require_validated=True)
    except GeometryUnavailable:
        return None

    # (c) a profile to sweep with — the printer's usual eject profile.
    profile_id = await _suggest_eject_profile_id(db, printer_id)
    if profile_id is None:
        return None
    profile = await db.get(EjectProfile, profile_id)
    if profile is None:
        return None

    # (d) donor resolves + part height within the profile's guard. _resolve_foreign_source
    # raises ManualEjectError when the donor/plate/height cannot be resolved → not
    # identified. Clean up any temp re-fetch either way (the auto-eject dispatch
    # re-resolves the donor fresh at release time, exactly like the manual confirm).
    try:
        source = await _resolve_foreign_source(db, printer)
    except ManualEjectError:
        return None
    try:
        if source.max_z > profile.max_part_height_mm:
            return None
    finally:
        # DEPOSIT the re-fetched donor (Phase D1) so the LATER auto-eject dispatch
        # (dispatch_identified_foreign_eject, after the cooldown watch) consumes it
        # instead of downloading again. Keyed by the gate; expires with the TTL if the
        # cooldown outlives it (dispatch then re-fetches — fail-open). An on-disk donor
        # (tmp_path=None) deposits nothing.
        _foreign_cache_put(_foreign_cache_key(printer), source.tmp_path)

    logger.info(
        "identify_farm_file_foreign: printer %s foreign plate IS the farm's own file "
        "(profile %s, cooldown %.1f°C, max_z %.1fmm) — auto-eject eligible",
        printer_id,
        profile_id,
        profile.cooldown_temp_c,
        source.max_z,
    )
    return ForeignFarmFile(profile_id=profile_id, threshold_c=profile.cooldown_temp_c, print_name=source.print_name)


async def dispatch_identified_foreign_eject(*, printer_id: int, profile_id: int) -> None:
    """``on_release`` for the auto foreign-eject watch: resolve the foreign donor FRESH
    and dispatch the sweep exactly as ``_manual_eject_foreign``'s confirm call does
    (minus the thermal gate — the cooldown watch already waited for the bed to reach
    the threshold). Opens its own session (module convention); RAISES on any failure so
    :func:`watch_bed_and_clear` counts a dispatch failure (retry, then stall after
    three) rather than silently dropping the sweep."""
    from backend.app.core.database import async_session

    async with async_session() as db:
        printer = await db.get(Printer, printer_id)
        if printer is None:
            raise ManualEjectError("not_found", "Printer not found", status_code=404)
        source = await _resolve_foreign_source(db, printer)
        try:
            await eject_remote.dispatch_foreign_eject(
                db,
                printer_id=printer_id,
                profile_id=profile_id,
                source_path=source.donor_path,
                plate_id=source.plate_id,
            )
            logger.info(
                "dispatch_identified_foreign_eject: printer %s auto foreign-plate eject dispatched "
                "(plate %s, profile %s)",
                printer_id,
                source.plate_id,
                profile_id,
            )
        finally:
            _safe_unlink(source.tmp_path)
