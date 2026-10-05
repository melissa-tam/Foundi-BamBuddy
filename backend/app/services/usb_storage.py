"""The USB sweep: keep each printer's USB drive down to what the farm still needs.

H2S printers report HMS ``0500010000030004`` ("Not enough space on MicroSD Card") when the USB-A
drive runs low — on the H2S that "MicroSD" IS the USB drive the LAN dispatch path requires (there
is no card slot), and a full drive breaks every FTPS upload (553). The firmware raises the code
while free space is under its reserve (3 GB on drives up to 16 GB, Bambu wiki HMS
0500_0100_0003_0004), and camera video recording (Setting > General > Video) may take up to 65 %
of the drive, so on the fleet's 4 GB sticks the code stands whenever recording is on: the farm
can trim the drive, it cannot make room for both. H2S also refuses FTP ``AVBL``, so free space is
unknowable here — the sweep never reads it.

ONE sweep (:func:`sweep`), two triggers:

* :func:`on_storage_low` — the ARRIVAL of that HMS code (``main.on_printer_status_change``'s
  new-HMS hook) and its deferred retry;
* :func:`sweep_after_terminal` — every print terminal (``main.on_print_complete``), so the drive is
  trimmed before it ever reaches the wall.

What may leave the drive (the sweep's SCOPE) — only files older than :data:`MIN_FILE_AGE_S`:

1. recordings in :data:`RECORDING_DIRS` (camera video, its stills, timelapses);
2. root files whose name the FARM wrote: the remote name (``derive_remote_filename``, the
   dispatcher's own rule) of any archive or library file, an eject job
   (``eject.remote.is_eject_job_name``) or a dry run (``DRY-RUN_*``). A file an operator sent
   from Studio under any other name is never a candidate.

…minus the KEEP-SET, whose one owner is ``job_terminal.live_upload_paths`` (the live job's paths
and those of the pending units pinned to the printer).

The gates, all in memory and checked before any I/O: the printer is not printing
(RUNNING/PREPARE), no sweep ran in the last :data:`CLEANUP_COOLDOWN_S`, and no dispatch lease,
eject or upload is in flight on it. Then the ``farm_usb_auto_cleanup`` setting. Every HMS trigger
a gate swallows re-arms a deferral that ``main``'s status hook retries (:func:`should_retry_deferred`
/ :func:`take_deferred`); a swallowed terminal trigger waits for the next terminal.

ONE FTPS session per sweep (``bambu_ftp.run_in_session``): five listings and every delete ride
one connection. The claim gate is re-read on the event loop before EACH delete, so a dispatch that
claims the printer mid-sweep stops the sweep before it can delete the file that dispatch is
uploading under the same name.

Alerting is the HMS trigger's alone, failures only: a sweep that freed space and saw the code
drop within :data:`STORAGE_SETTLE_S` is an INFO line; one whose code still stands pages with the
operator's exit (:data:`STORAGE_STILL_LOW_REASON`); an unreachable drive, undeletable files, or
nothing cleanable while the code stands page too. At most ONE such page per printer per
:data:`STORAGE_LOW_PAGE_COOLDOWN_S`: a 4 GB drive with camera recording still on raises the code
at every print, and the 10 min sweep cooldown alone would page every cycle. The terminal sweep
never notifies.

A reported ``sdcard`` True→False edge is a separate lane (:func:`verify_and_alert_usb_drop`): a
firmware unmount of a full or failed drive mid-print, surfaced once probe-confirmed.

Which printers have an upload in flight is ``usb_uploads``' registry (a leaf module every upload
lane writes); this module only reads it.

State (cooldowns, deferral, single-flight, the drop edge) is module-level and in-memory by
design. Its rehydrate story: a restart forgets a sweep cooldown (one sweep may run early), a
page cooldown (worst cost: one extra page), a drop-alert cooldown (one extra drop alert) and a
deferral (the next print terminal sweeps the drive anyway, and a code still standing pages from
that sweep's HMS retry once it re-appears); the drop edge is rebuilt by the next full status
report. Injectable ``manager`` / ``now`` / ``sleep`` for tests.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, TypeVar

from sqlalchemy import select

from backend.app.services.bambu_ftp import DeleteResult, list_files_async, run_in_session
from backend.app.services.eject.remote import is_eject_job_name
from backend.app.services.job_terminal import live_upload_paths
from backend.app.services.plate_occupancy import plate_occupancy
from backend.app.services.printer_manager import printer_manager
from backend.app.services.usb_uploads import upload_active
from backend.app.utils.filename import derive_remote_filename

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.app.services.bambu_ftp import BambuFTPClient

logger = logging.getLogger(__name__)

T = TypeVar("T")

# HMS full-codes (uppercase-hex ``f"{attr:08X}{code:08X}"``) that mean the USB drive
# is (nearly) full. Frozen so it can be intersected against new HMS codes in the
# main.py hook and used for the generic-notification suppression there.
HMS_STORAGE_LOW_FULL_CODES: frozenset[str] = frozenset({"0500010000030004"})

# A file must be at least this old before the sweep deletes it — never a recording still
# being written, never a print file that may have just been uploaded.
MIN_FILE_AGE_S: float = 3600.0

# After a sweep RAN on a printer (success or failure), no other runs there for this long, so
# a terminal burst or a re-arriving HMS code cannot thrash the drive. Short enough that the
# deferral a swallowed HMS trigger leaves behind is retried within minutes.
CLEANUP_COOLDOWN_S: float = 10 * 60.0

# One confirmed USB-drop alert per printer per this window, so a physically flapping stick
# cannot spam the notification channel.
USB_DROP_ALERT_COOLDOWN_S: float = 6 * 3600.0

# One storage-low PAGE per printer per this window (every sweep-outcome page: still low,
# undeletable files, nothing cleanable, unreachable, an unexpected error). The sweep cooldown is
# 10 min, and a 4 GB drive with camera recording on raises the code at every print — without
# this the farm would page the same standing condition every cycle. Separate from the drop alert.
STORAGE_LOW_PAGE_COOLDOWN_S: float = 6 * 3600.0

# How long the HMS trigger waits, after a sweep that freed space, for the printer to drop the
# storage-low code before it pages that space is still low. MEASURED: the firmware dropped the
# code 19 s and 32 s after the deletes on 014-H2S / 013-H2S, 2026-10-05; 2x the longer one.
STORAGE_SETTLE_S: float = 60.0

# The live HMS list is re-read this often during the settle wait (in memory, no I/O).
STORAGE_SETTLE_POLL_S: float = 5.0

# Verify-then-alert delay for a reported ``sdcard`` True→False edge: on the first
# FTPS probe seeing an empty/unreachable root, wait this long and re-probe before
# treating the drop as genuine. H2S firmware transiently reports ``sdcard=false``
# for ~1 s during job dispatch / FTPS upload — a single confirming re-probe filters
# those dispatch blips out without a fixed idle debounce.
USB_DROP_PROBE_RETRY_S: float = 15.0

# Human reason carried on the confirmed-drop notification (attempted=False → no
# "Auto-cleanup could not free space:" prefix). Lives here so main.py stays a thin
# edge-detect + spawn.
USB_DROP_REASON: str = "USB drive dropped/unmounted — power-cycle the printer to remount it"

# The operator's exit when a sweep freed space and the code still stands: on a 4 GB drive the
# firmware's video quota and its free-space reserve cannot both hold (module docstring).
STORAGE_STILL_LOW_REASON: str = (
    "the printer still reports low space; a 4 GB drive cannot hold camera video recording "
    "(Setting > General > Video) — turn recording off or fit a 16 GB+ drive"
)

# Recording directories the sweep cleans: camera video and its stills, timelapses and their
# orphan preview stills. A directory a model lacks lists empty.
RECORDING_DIRS: tuple[str, ...] = ("/ipcam", "/ipcam/thumbnail", "/timelapse", "/timelapse/thumbnail")

# The storage root: where the dispatcher, the eject lane and the dry-run lane upload.
_ROOT_DIR = "/"

# The dry-run lane's library files are ``DRY-RUN {profile}.gcode.3mf`` — remote
# ``DRY-RUN_{profile}.3mf``; the prefix keeps a replaced or deleted dry run's leftover in scope.
_DRY_RUN_PREFIX = "dry-run_"

# Live gcode_state values that mean the printer is actively printing — never sweep then.
_ACTIVE_PRINT_STATES: frozenset[str] = frozenset({"RUNNING", "PREPARE"})

# The longest the FTPS worker waits for the event loop to answer a gate read before it treats
# the gate as closed (fail closed: stop deleting).
_GATE_READ_TIMEOUT_S: float = 10.0

SweepTrigger = Literal["hms", "terminal"]

# printer_id -> when the last sweep RAN there (success or failure). Guards the cooldown,
# shared by both triggers.
_last_cleanup_at: dict[int, float] = {}

# Printers whose last storage-low trigger a gate swallowed. Without it the deferral would be
# permanent: a storage-low HMS that arrives mid-print may never re-fire the main.py NEW-code
# edge, and the firmware can even DROP the code from hms[] when it unmounts a full drive
# (live-observed on printer 7, 2026-07-14) — so a presence-gated retry would wait forever.
# main.py's status hook asks ``should_retry_deferred`` (cheap, DB-free, NO code-presence
# requirement) every tick and consumes the flag through ``take_deferred`` — the one remover —
# before it spawns the retry, so consecutive ticks cannot multi-spawn.
_deferred_printers: set[int] = set()

# Printers with a sweep currently running. Checked-and-added SYNCHRONOUSLY at the very top of
# ``sweep`` (before any await) and discarded in a finally, so concurrent triggers — consecutive
# status ticks, or an HMS trigger racing a terminal — collapse to one running sweep per printer.
_inflight: set[int] = set()

# Last REPORTED USB (`sdcard`) presence per printer, for the mid-print drop alert.
# Absent = never observed present: tracking only begins once the drive is seen True,
# so the startup default (False) never spuriously fires a drop. See
# ``record_sdcard_and_detect_drop``.
_last_sdcard: dict[int, bool] = {}

# printer_id -> timestamp of the last CONFIRMED USB-drop alert (``USB_DROP_ALERT_COOLDOWN_S``).
# Re-arms naturally after the cooldown / a remount.
_last_drop_alert_at: dict[int, float] = {}

# printer_id -> timestamp of the last storage-low PAGE (``STORAGE_LOW_PAGE_COOLDOWN_S``).
_last_storage_page_at: dict[int, float] = {}


def _reset_state() -> None:
    """Test hook: clear the module-level cooldowns + deferral + in-flight + USB-drop state."""
    _last_cleanup_at.clear()
    _deferred_printers.clear()
    _inflight.clear()
    _last_sdcard.clear()
    _last_drop_alert_at.clear()
    _last_storage_page_at.clear()


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


def _cooling_down(printer_id: int, now: float) -> bool:
    """Whether a sweep ran on this printer within ``CLEANUP_COOLDOWN_S`` of ``now``."""
    last = _last_cleanup_at.get(printer_id)
    return last is not None and now - last < CLEANUP_COOLDOWN_S


def should_retry_deferred(printer_id: int, live_state: str | None, *, now: float | None = None) -> bool:
    """DB-free trigger predicate for the main.py status hook (deferred-retry path). O(1), pure.

    True when the printer has a pending deferral, its live ``gcode_state`` is no longer
    RUNNING/PREPARE, and the sweep cooldown has lapsed — so a deferral left by a cooldown is
    retried once, when it can run, instead of spawning a task on every status push meanwhile.
    Deliberately does NOT require the storage-low HMS code to still be present — the firmware
    DROPS that code from hms[] when it unmounts a full drive, so a presence-gated retry would
    never fire for the exact failure it exists to recover from (silent permanent deferral,
    printer 7, 2026-07-14). A retry that finds the drive unmounted surfaces as the FTPS
    unreachable FAILURE notification instead. Set-membership check first so the common
    no-deferral tick short-circuits before any state work.
    """
    if printer_id not in _deferred_printers:
        return False
    if str(live_state or "").upper() in _ACTIVE_PRINT_STATES:
        return False
    return not _cooling_down(printer_id, time.time() if now is None else now)


def take_deferred(printer_id: int) -> bool:
    """Consume this printer's storage-low deferral; True iff there was one.

    The ONE remover of a deferral: main.py's retry hook calls it before it spawns the retry,
    so consecutive status ticks cannot spawn two. The retry's sweep re-arms it if a gate
    swallows the retry too.
    """
    if printer_id not in _deferred_printers:
        return False
    _deferred_printers.discard(printer_id)
    return True


def _defer(printer_id: int, why: str) -> None:
    """Re-arm the storage-low deferral. Logs only a NEW deferral: a mid-print storage-low HMS
    re-triggers on every status tick, and logging each one flooded the log (776 lines/day in
    prod from one flapping printer)."""
    if printer_id not in _deferred_printers:
        logger.info("[USB-STORAGE] printer %s storage-low sweep deferred (%s)", printer_id, why)
    _deferred_printers.add(printer_id)


def _live_sdcard(manager, printer_id: int) -> bool | None:
    """THE reader of the live ``sdcard`` field — the reported value, or None.

    None means there is no reading to interpret: no live status at all, a
    dropped session (a disconnected printer's last flag is a memory, not an
    observation), or a status that never carried the field — the firmware
    reports ``sdcard`` only inside a FULL status report. Only an explicit
    boolean is an answer, matching the fail-open grammar every consumer uses.
    """
    try:
        status = manager.get_status(printer_id)
    except Exception:  # noqa: BLE001 — manager access must never crash a caller
        return None
    if status is None or getattr(status, "connected", None) is False:
        return None
    value = getattr(status, "sdcard", None)
    return value if isinstance(value, bool) else None


def usb_present(printer_id: int) -> bool | None:
    """Is the USB drive genuinely present on ``printer_id``? Sync, DB-free.

    THE decider behind every "no USB drive" verdict — the dispatch pre-flight's
    hold, the run-detail eligibility panel, the fleet observation recorder — so
    one question has one answer instead of three readings of a flag that lies
    for ~1 s per upload.

      * ``True``  — the live status reports the drive present.
      * ``False`` — GENUINELY absent: a live status reads ``sdcard`` False and
        no FTPS upload is in flight to this printer.
      * ``None``  — unknown: no live status, a dropped session, the field never
        reported, or an in-flight upload masking a False reading.

    The blip suppression covers every lane that registers an upload in
    ``usb_uploads`` (dispatch, remote eject, firmware): H2S firmware
    transiently reports ``sdcard=false`` while an upload runs, which is a
    dispatch blip, not a missing stick. A genuinely absent drive instead makes
    the upload itself fail, and that path owns its own failure handling.
    """
    present = _live_sdcard(printer_manager, printer_id)
    if present is False and upload_active(printer_id):
        return None
    return present


async def _usb_probe_entry_count(ip: str, code: str, model: str | None) -> int | None:
    """Number of root FTPS entries, or None when the probe itself errors.

    A present USB drive lists a non-empty root; a missing/unmounted drive (or a down
    port / rejected login) collapses to an empty listing or an exception. Both the
    empty and exception cases are treated by the caller as "not present".
    """
    try:
        entries = await list_files_async(ip, code, path="/", printer_model=model)
    except Exception:  # noqa: BLE001 — a failed probe reads as "not present"
        return None
    return len(entries or [])


async def verify_and_alert_usb_drop(
    printer_id: int,
    *,
    manager=None,
    now: float | None = None,
    sleep=None,
) -> None:
    """Verify a reported ``sdcard`` True→False edge before alerting on a USB drop.

    Thin service-owned entry point for the ``main`` status hook (which owns only the
    edge detection via ``record_sdcard_and_detect_drop``). A reported drop is NOT
    trusted blindly — the H2S firmware transiently reports ``sdcard=false`` during
    job dispatch / FTPS upload:

      * If an FTPS upload is in flight for this printer → suppress (dispatch blip).
      * Else actively probe FTPS: a non-empty root means the USB is present →
        suppress. Empty/unreachable → wait ``USB_DROP_PROBE_RETRY_S`` and re-probe;
        still empty/unreachable → confirmed drop.
      * If the printer reports the USB back (live ``sdcard`` True) before the alert
        fires → no alert.
      * A per-printer ``USB_DROP_ALERT_COOLDOWN_S`` keeps a flapping stick from
        spamming the notification.

    Fires the ``on_storage_low`` notification with ``attempted=False`` (raw reason,
    no "Auto-cleanup could not free space:" prefix) only on a confirmed drop. Never
    raises — the whole body is guarded so it can't crash the status flow.
    """
    if manager is None:
        manager = printer_manager
    now = time.time() if now is None else now
    sleep = asyncio.sleep if sleep is None else sleep

    try:
        # (a) Upload-in-flight suppression — a dispatch/upload blip, not a drop.
        if upload_active(printer_id):
            logger.info(
                "[USB-STORAGE] printer %s sdcard=false during active upload; ignoring (dispatch blip)",
                printer_id,
            )
            return

        from backend.app.core.database import async_session
        from backend.app.models.printer import Printer

        async with async_session() as db:
            printer = await db.get(Printer, printer_id)
            if printer is None:
                logger.warning("[USB-STORAGE] printer %s not found; cannot verify USB drop", printer_id)
                return
            printer_name = printer.name or f"printer {printer_id}"
            ip = printer.ip_address
            code = printer.access_code
            model = printer.model

        # (b) Active FTPS probe — a present USB lists a non-empty root.
        count = await _usb_probe_entry_count(ip, code, model)
        if count:
            logger.info(
                "[USB-STORAGE] printer %s transient sdcard=false ignored (FTPS probe saw %d entries)",
                printer_id,
                count,
            )
            return

        # Empty/unreachable → confirm with one delayed re-probe (filters dispatch blips).
        await sleep(USB_DROP_PROBE_RETRY_S)
        count = await _usb_probe_entry_count(ip, code, model)
        if count:
            logger.info(
                "[USB-STORAGE] printer %s transient sdcard=false ignored (FTPS probe saw %d entries)",
                printer_id,
                count,
            )
            return

        # (a′) Re-check upload-in-flight AFTER the re-probe. An edge can fire just
        # BEFORE a dispatch upload begins (blips also occur at the project_file /
        # print-start moment); once that upload is running, the printer caps
        # concurrent FTPS sessions, so BOTH probes can fail on session contention and
        # fabricate a confirmed drop. If an upload is now in flight, it's that blip.
        if upload_active(printer_id):
            logger.info(
                "[USB-STORAGE] printer %s sdcard=false during active upload; ignoring (dispatch blip)",
                printer_id,
            )
            return

        # (c) Cancellation — the printer reported the USB back before we alerted.
        if _live_sdcard(manager, printer_id) is True:
            logger.info("[USB-STORAGE] printer %s sdcard restored during verification; no alert", printer_id)
            return

        # (d) Repeat suppression — one alert per printer per cooldown window.
        last = _last_drop_alert_at.get(printer_id)
        if last is not None and now - last < USB_DROP_ALERT_COOLDOWN_S:
            logger.info("[USB-STORAGE] printer %s USB drop within alert cooldown; suppressing", printer_id)
            return

        _last_drop_alert_at[printer_id] = now
        logger.warning("[USB-STORAGE] printer %s USB drop confirmed by FTPS probe; alerting", printer_id)
        await _fire_usb_drop_notification(printer_id, printer_name)
    except Exception:  # noqa: BLE001 — verifier must NEVER crash the status flow
        logger.exception("[USB-STORAGE] USB-drop verification failed unexpectedly for printer %s", printer_id)


# --------------------------------------------------------------------------- #
# The sweep
# --------------------------------------------------------------------------- #
@dataclass
class _SweepOutcome:
    """What one sweep did to the drive, shaped for its log line and the HMS trigger's page."""

    freed_bytes: int = 0
    files_deleted: int = 0
    failed_paths: list[str] = field(default_factory=list)
    # The claim gate that closed mid-sweep (a dispatch, eject or upload arrived), or None.
    stood_down: str | None = None

    @property
    def success(self) -> bool:
        """Every candidate the sweep reached left the drive, and nothing stopped it short."""
        return not self.failed_paths and self.stood_down is None


@dataclass(frozen=True)
class _SweepScope:
    """What may leave this printer's drive, decided BEFORE the FTPS session opens.

    ``keep`` and ``farm_names`` are lower-cased: the H2S drive is FAT/exFAT, case-insensitive,
    so a name differing only in case is the same file.
    """

    now: float
    keep: frozenset[str]
    farm_names: frozenset[str]

    def _old_enough(self, entry: dict) -> bool:
        if entry.get("is_directory") or not entry.get("path"):
            return False
        mtime = entry.get("mtime")
        if mtime is None:
            return False  # no timestamp → cannot prove it is old enough; leave it alone
        return self.now - mtime.timestamp() >= MIN_FILE_AGE_S

    def recording(self, entry: dict) -> bool:
        """A recording-directory entry that may go."""
        return self._old_enough(entry)

    def print_file(self, entry: dict) -> bool:
        """A root entry that may go: old enough, written by the farm, not in the keep-set."""
        if not self._old_enough(entry) or str(entry["path"]).lower() in self.keep:
            return False
        return _farm_written(str(entry.get("name") or ""), self.farm_names)


def _farm_written(name: str, farm_names: frozenset[str]) -> bool:
    """Did the farm write a root file of this name? An archive or library file's remote name,
    an eject job, or a dry run. Anything else is an operator's file and never a candidate."""
    low = name.lower()
    return bool(low) and (low in farm_names or low.startswith(_DRY_RUN_PREFIX) or is_eject_job_name(name))


async def _farm_written_names(db: AsyncSession) -> frozenset[str]:
    """The remote name of every archive and library file — ONE query per table, never per file.

    ``derive_remote_filename`` is the dispatcher's own upload rule, so this is the set of names a
    farm dispatch can have written to a storage root. Lower-cased (see :class:`_SweepScope`).
    """
    from backend.app.models.archive import PrintArchive
    from backend.app.models.library import LibraryFile

    names: set[str] = set()
    for column in (PrintArchive.filename, LibraryFile.filename):
        for filename in (await db.execute(select(column).distinct())).scalars():
            if isinstance(filename, str) and filename:
                names.add(derive_remote_filename(filename).lower())
    return frozenset(names)


def _is_printing(manager, printer_id: int) -> bool:
    """Whether the printer is actively printing (RUNNING/PREPARE) right now."""
    try:
        client = manager.get_client(printer_id)
    except Exception:  # noqa: BLE001 — manager access must never crash the guard
        return False
    if client is None or getattr(client, "state", None) is None:
        return False
    return str(getattr(client.state, "state", "") or "").upper() in _ACTIVE_PRINT_STATES


def _claimed(printer_id: int, manager) -> str | None:
    """Why the farm may not touch this printer's drive right now, or None. In memory, no I/O.

    Printing, an FTPS upload, a dispatch lease or an eject: each is a job the drive is about to
    serve, and a farm job's file shares its name with every earlier upload of the same donor.
    """
    if _is_printing(manager, printer_id):
        return "printing"
    if upload_active(printer_id):
        return "claimed"
    if printer_id in plate_occupancy.printers_with_lease_or_eject():
        return "claimed"
    return None


def _read_on_loop(loop: asyncio.AbstractEventLoop, read: Callable[[], T]) -> T:
    """Evaluate ``read`` on the event-loop thread and hand its result to the calling worker thread.

    The claim gate's state belongs to the loop: the occupancy authority prunes a spent lease
    as it is read, and doing that from the FTPS worker would race the dispatcher's own writes.
    """
    answer: concurrent.futures.Future[T] = concurrent.futures.Future()

    def _run() -> None:
        try:
            answer.set_result(read())
        except BaseException as exc:  # noqa: BLE001 — handed to the waiting worker, never swallowed
            answer.set_exception(exc)

    loop.call_soon_threadsafe(_run)
    return answer.result(timeout=_GATE_READ_TIMEOUT_S)


def _sweep_drive(client: BambuFTPClient, scope: _SweepScope, claimed: Callable[[], str | None]) -> _SweepOutcome:
    """The sweep's FTPS body — runs in the worker thread on ONE connected session.

    Lists the recording directories and the root, then deletes every candidate in that order
    (recordings are the bulk). ``claimed`` is re-read before each delete; the first answer that
    is not None stops the sweep, and what is left waits for the next one.
    """
    candidates: list[tuple[str, int, str]] = []
    for directory in RECORDING_DIRS:
        candidates += [
            (entry["path"], int(entry.get("size") or 0), "recording")
            for entry in client.list_files(directory)
            if scope.recording(entry)
        ]
    candidates += [
        (entry["path"], int(entry.get("size") or 0), "print file")
        for entry in client.list_files(_ROOT_DIR)
        if scope.print_file(entry)
    ]

    outcome = _SweepOutcome()
    for path, size, kind in candidates:
        try:
            outcome.stood_down = claimed()
        except Exception:  # noqa: BLE001 — an unreadable gate is a closed gate
            outcome.stood_down = "gate unreadable"
        if outcome.stood_down is not None:
            break
        result = client.delete_file(path)
        if result is DeleteResult.DELETED:
            outcome.freed_bytes += size
            outcome.files_deleted += 1
            logger.warning(
                "[USB-STORAGE] deleted %s %s (%d bytes)",
                "recording" if kind == "recording" else "stale print file",
                path,
                size,
            )
        elif result is DeleteResult.FAILED:
            outcome.failed_paths.append(path)
            logger.warning("[USB-STORAGE] failed to delete %s %s", kind, path)
    return outcome


def _storage_low_standing(manager, printer_id: int) -> bool:
    """Does the printer's LIVE HMS list carry the storage-low code? In memory, no I/O.

    No live session reads as "not standing": the farm cannot claim space is still low from a
    memory of the wire.
    """
    try:
        status = manager.get_status(printer_id)
    except Exception:  # noqa: BLE001 — manager access must never crash a caller
        return False
    if status is None or getattr(status, "connected", None) is False:
        return False
    return any(
        getattr(error, "full_code", "") in HMS_STORAGE_LOW_FULL_CODES
        for error in (getattr(status, "hms_errors", None) or [])
    )


async def _storage_low_settles(printer_id: int, manager, sleep: Callable[[float], Awaitable[None]]) -> bool:
    """Wait up to ``STORAGE_SETTLE_S`` for the printer to drop the storage-low code. True iff it did."""
    waited = 0.0
    while _storage_low_standing(manager, printer_id):
        if waited >= STORAGE_SETTLE_S:
            return False
        await sleep(STORAGE_SETTLE_POLL_S)
        waited += STORAGE_SETTLE_POLL_S
    return True


async def _page(printer_id: int, printer_name: str, outcome: _SweepOutcome, reason: str, *, now: float) -> None:
    """Send a storage-low page — at most one per printer per ``STORAGE_LOW_PAGE_COOLDOWN_S``.

    Every sweep-outcome page goes through here. The window is stamped before the send, like the
    drop alert's, so a slow or failing provider cannot let a second page through behind it.
    """
    last = _last_storage_page_at.get(printer_id)
    if last is not None and now - last < STORAGE_LOW_PAGE_COOLDOWN_S:
        logger.info(
            "[USB-STORAGE] printer %s storage-low page within its cooldown; suppressed (%s)", printer_id, reason
        )
        return
    _last_storage_page_at[printer_id] = now
    await _fire_notification(printer_id, printer_name, outcome, reason)


async def _answer_storage_low(
    printer_id: int,
    printer_name: str,
    outcome: _SweepOutcome,
    manager,
    sleep: Callable[[float], Awaitable[None]],
    *,
    now: float,
) -> None:
    """The HMS trigger's verdict on a sweep that finished the drive: a page only when the operator
    has something to do. Success is the sweep's INFO line."""
    if outcome.freed_bytes > 0:
        if await _storage_low_settles(printer_id, manager, sleep):
            logger.info("[USB-STORAGE] printer %s storage-low cleared after the sweep", printer_id)
            return
        logger.warning(
            "[USB-STORAGE] printer %s still reports storage-low %.0fs after the sweep freed %d bytes",
            printer_id,
            STORAGE_SETTLE_S,
            outcome.freed_bytes,
        )
        await _page(printer_id, printer_name, outcome, STORAGE_STILL_LOW_REASON, now=now)
        return
    if outcome.failed_paths:
        await _page(
            printer_id, printer_name, outcome, f"{len(outcome.failed_paths)} file(s) could not be deleted", now=now
        )
        return
    if _storage_low_standing(manager, printer_id):
        # Nothing the farm wrote is left to remove and the code stands: the drive is full of what
        # the printer itself keeps writing — the 4 GB + video-recording case — so the page carries
        # the operator's exit.
        await _page(printer_id, printer_name, outcome, f"nothing left to clean; {STORAGE_STILL_LOW_REASON}", now=now)


async def _run_sweep(
    printer_id: int, trigger: SweepTrigger, manager, now: float, sleep: Callable[[float], Awaitable[None]]
) -> str:
    """The sweep behind the single-flight guard. Returns its verdict token."""
    gate = _claimed(printer_id, manager)
    if gate is None and _cooling_down(printer_id, now):
        gate = "cooldown"
    if gate is not None:
        if trigger == "hms":
            _defer(printer_id, gate)
        else:
            logger.debug("[USB-STORAGE] printer %s terminal sweep skipped (%s)", printer_id, gate)
        return gate

    from backend.app.api.routes.settings import get_setting
    from backend.app.core.database import async_session
    from backend.app.models.printer import Printer

    async with async_session() as db:
        raw = await get_setting(db, "farm_usb_auto_cleanup")
        if raw is not None and str(raw).strip().lower() == "false":
            logger.info("[USB-STORAGE] auto-cleanup disabled; printer %s ignored", printer_id)
            return "disabled"
        printer = await db.get(Printer, printer_id)
        if printer is None:
            logger.warning("[USB-STORAGE] printer %s not found; cannot clean", printer_id)
            return "not_found"
        # Stamped before the slow work, so a trigger arriving meanwhile does not queue a second run.
        _last_cleanup_at[printer_id] = now
        live = manager.get_status(printer_id)
        keep = await live_upload_paths(
            db, printer_id, getattr(live, "subtask_id", None), getattr(live, "subtask_name", None)
        )
        scope = _SweepScope(
            now=now, keep=frozenset(path.lower() for path in keep), farm_names=await _farm_written_names(db)
        )
        printer_name = printer.name or f"printer {printer_id}"
        ip, code, model = printer.ip_address, printer.access_code, printer.model

    loop = asyncio.get_running_loop()

    def _claimed_now() -> str | None:
        return _read_on_loop(loop, lambda: _claimed(printer_id, manager))

    try:
        outcome = await run_in_session(
            ip, code, lambda client: _sweep_drive(client, scope, _claimed_now), printer_model=model
        )
    except ConnectionError:
        logger.warning("[USB-STORAGE] printer %s FTPS unreachable; cannot clean", printer_id)
        if trigger == "hms":
            await _page(
                printer_id,
                printer_name,
                _SweepOutcome(),
                "printer FTPS unreachable (USB drive missing or port 990 down)",
                now=now,
            )
        return "unreachable"

    logger.info(
        "[USB-STORAGE] printer %s cleanup done: freed %d bytes across %d file(s), success=%s",
        printer_id,
        outcome.freed_bytes,
        outcome.files_deleted,
        outcome.success,
    )
    if outcome.stood_down is not None:
        # The rest waits for the next sweep; an HMS trigger is owed that retry, and its verdict
        # belongs to the sweep that finishes the drive.
        logger.info("[USB-STORAGE] printer %s sweep stopped short (%s)", printer_id, outcome.stood_down)
        if trigger == "hms":
            _defer(printer_id, outcome.stood_down)
    elif trigger == "hms":
        await _answer_storage_low(printer_id, printer_name, outcome, manager, sleep, now=now)
    return "done"


async def sweep(
    printer_id: int,
    *,
    trigger: SweepTrigger,
    manager=None,
    now: float | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> str:
    """THE USB sweep, shared by both triggers. Never raises; returns its verdict token.

    ``"inflight"`` / ``"printing"`` / ``"cooldown"`` / ``"claimed"`` — a gate swallowed it (an
    HMS trigger re-arms its deferral); ``"disabled"`` / ``"not_found"`` — nothing to do;
    ``"unreachable"`` — no FTPS session; ``"done"`` — the drive was swept; ``"error"`` — an
    unexpected failure (logged; the HMS trigger pages it).
    """
    if printer_id in _inflight:
        logger.debug("[USB-STORAGE] sweep already in flight for printer %s (%s trigger)", printer_id, trigger)
        if trigger == "hms":
            _defer(printer_id, "inflight")
        return "inflight"
    _inflight.add(printer_id)
    now = time.time() if now is None else now
    try:
        return await _run_sweep(
            printer_id,
            trigger,
            printer_manager if manager is None else manager,
            now,
            asyncio.sleep if sleep is None else sleep,
        )
    except Exception:  # noqa: BLE001 — the sweep must NEVER crash the status or completion flow
        logger.exception("[USB-STORAGE] sweep failed unexpectedly for printer %s", printer_id)
        if trigger == "hms":
            try:
                await _page(
                    printer_id, f"printer {printer_id}", _SweepOutcome(), "unexpected error during cleanup", now=now
                )
            except Exception:  # noqa: BLE001
                logger.exception("[USB-STORAGE] failure notification also failed for printer %s", printer_id)
        return "error"
    finally:
        _inflight.discard(printer_id)


async def on_storage_low(
    printer_id: int,
    full_codes: set[str],
    *,
    manager=None,
    now: float | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> None:
    """The HMS trigger: a storage-low code APPEARED on ``printer_id`` (or its deferral is retried).

    Runs :func:`sweep`; a gate that swallows it re-arms the deferral for main.py's retry hook.
    Pages only when the operator has something to do (module docstring). Never raises.
    """
    if not (set(full_codes) & HMS_STORAGE_LOW_FULL_CODES):
        return
    await sweep(printer_id, trigger="hms", manager=manager, now=now, sleep=sleep)


async def sweep_after_terminal(printer_id: int, *, manager=None, now: float | None = None) -> None:
    """The terminal trigger: fired from ``main.on_print_complete`` after every print terminal, so
    the recordings a lights-out run accumulates and the uploads of finished jobs leave the drive
    BEFORE it reaches the storage-low wall. Never notifies; never raises."""
    await sweep(printer_id, trigger="terminal", manager=manager, now=now)


async def _fire_notification(printer_id: int, printer_name: str, outcome: _SweepOutcome, reason: str) -> None:
    """Send the dedicated ``on_storage_low`` failure notification for a sweep's outcome. Called
    only through :func:`_page`, which owns the page cooldown."""
    from backend.app.core.database import async_session
    from backend.app.services.notification_service import notification_service

    async with async_session() as db:
        await notification_service.on_storage_low(
            printer_id,
            printer_name,
            success=False,
            freed_bytes=outcome.freed_bytes,
            files_deleted=outcome.files_deleted,
            reason=reason,
            db=db,
        )


async def _fire_usb_drop_notification(printer_id: int, printer_name: str) -> None:
    """Send the ``on_storage_low`` notification for a CONFIRMED USB drop.

    Passes ``attempted=False`` so the body carries the raw drop reason with no
    "Auto-cleanup could not free space:" prefix (no cleanup was attempted).
    """
    from backend.app.core.database import async_session
    from backend.app.services.notification_service import notification_service

    async with async_session() as db:
        await notification_service.on_storage_low(
            printer_id,
            printer_name,
            success=False,
            freed_bytes=0,
            files_deleted=0,
            reason=USB_DROP_REASON,
            attempted=False,
            db=db,
        )
