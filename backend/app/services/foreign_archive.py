"""Capture of the source ``.gcode.3mf`` of a print that is running right now.

The printer keeps a print's source 3MF only while that print runs. A job Bambuddy
did not dispatch (Bambu Studio / hand-spliced LAN print) echoes a degenerate
``Metadata/plate_N.gcode`` name, so the print-start locate can miss the file, and
by the terminal event the printer's copy is usually already deleted: the archive
row is created without a file, ``usage_tracker`` finds no 3MF, and the run charges
zero grams. Thirteen terminals between 2026-08-01 and 2026-08-09 charged nothing
for exactly this reason.

This module owns:

* :func:`locate_3mf_for_print` — the ONE candidate-derivation + FTPS download +
  stale-plate correction implementation. ``main.on_print_start`` and the retry
  below are both callers; there is no second copy. Its ``known_donor`` argument is
  how a caller that already KNOWS the source file says so (a farm dispatch — see
  ``farm_correlation.resolve_dispatch_donor``); everything else in here is the
  answer for a print whose source the farm has to guess. Supplied versus derived is
  an argument, deliberately not a second lane: there is one archive path and this
  module stays the module for the guessing half of it.
* :func:`maybe_schedule_foreign_3mf_retry` — the bounded in-flight retry armed
  when the start-time capture missed AND no farm queue item claims the print.
  Farm prints need no retry: their 3MF is already in the library / a previous
  archive, which is where ``usage_tracker``'s fallback lookup finds it.
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import settings as app_settings
from backend.app.core.database import async_session
from backend.app.core.tasks import spawn_background_task
from backend.app.models.archive import PrintArchive
from backend.app.models.printer import Printer
from backend.app.services.archive import ArchiveService, plate_indices_in_3mf, swap_plate_suffix
from backend.app.services.bambu_ftp import (
    FileNotOnPrinterError,
    cache_3mf_download,
    cleanup_downloaded_3mf,
    download_file_async,
    get_cached_3mf,
    get_ftp_retry_settings,
    list_files_async,
    with_ftp_retry,
)
from backend.app.services.farm_correlation import DispatchDonor, resolve_terminal_item
from backend.app.services.printer_manager import parse_plate_id, printer_manager
from backend.app.utils.filename import print_identity_key

logger = logging.getLogger(__name__)

# Two further attempts while the job is still running. The first is soon enough to
# beat a short print's cleanup, the second covers a printer that only publishes its
# enriched subtask_name / gcode_file minutes in (the degenerate-echo case).
RETRY_DELAYS_S: tuple[float, ...] = (60.0, 300.0)

# Remote directories a Bambu printer may hold an uploaded 3MF in. Root first:
# BambuStudio/OrcaSlicer uploads land there on A1/P1-series, and deferring it cost
# #972's reporter ~48 minutes of retries before landing on the right path.
_REMOTE_DIRS: tuple[str, ...] = ("/", "/cache", "/model", "/data", "/data/Metadata")


@dataclass(frozen=True)
class ThreeMFLookup:
    """Outcome of locating a running print's source 3MF on the printer.

    ``subtask_name`` is echoed back because a stale-plate correction rewrites it:
    the caller names its fallback archive from the corrected value (#1204).

    ``local_path`` is borrowed (a library file or archive copy served from the
    download cache) or cache-owned (a temp published to it) — never the caller's
    to delete.
    """

    local_path: Path | None
    filename: str | None
    subtask_name: str
    expected_plate: int | None

    @property
    def found(self) -> bool:
        return self.local_path is not None and self.filename is not None


def _candidate_names(subtask_name: str, filename: str) -> list[str]:
    """3MF filenames the printer may hold this print under, best guess first."""
    possible_names: list[str] = []

    # Bambu printers typically store files as "Name.gcode.3mf"
    # The subtask_name is usually the best source for the filename
    if subtask_name:
        possible_names.append(f"{subtask_name}.gcode.3mf")
        possible_names.append(f"{subtask_name}.3mf")

    # Try original filename with .3mf extension
    if filename:
        # Extract just the filename part, not the full path
        fname = filename.split("/")[-1] if "/" in filename else filename
        if fname.endswith(".3mf"):
            possible_names.append(fname)
        elif fname.endswith(".gcode"):
            base = fname.rsplit(".", 1)[0]
            possible_names.append(f"{base}.gcode.3mf")
            possible_names.append(f"{base}.3mf")
        else:
            possible_names.append(f"{fname}.gcode.3mf")
            possible_names.append(f"{fname}.3mf")

    # Also try with spaces converted to underscores (Bambu Studio may normalize filenames)
    space_variants = [name.replace(" ", "_") for name in possible_names if " " in name]
    possible_names.extend(space_variants)

    # Remove duplicates while preserving order
    seen: set[str] = set()
    return [x for x in possible_names if x.endswith(".3mf") and not (x in seen or seen.add(x))]


async def _download(
    printer: Printer,
    remote_path: str,
    dest: Path,
    retry_settings: tuple[bool, int, float, float],
    operation_name: str,
    *,
    non_retry_exceptions: tuple[type[BaseException], ...] = (),
) -> bool:
    """One FTPS fetch through the configured retry lane. Raises what the lane raises."""
    ftp_retry_enabled, ftp_retry_count, ftp_retry_delay, ftp_timeout = retry_settings
    if ftp_retry_enabled:
        return await with_ftp_retry(
            download_file_async,
            printer.ip_address,
            printer.access_code,
            remote_path,
            dest,
            timeout=ftp_timeout,
            socket_timeout=ftp_timeout,
            printer_model=printer.model,
            max_retries=ftp_retry_count,
            retry_delay=ftp_retry_delay,
            operation_name=operation_name,
            non_retry_exceptions=non_retry_exceptions,
        )
    return await download_file_async(
        printer.ip_address,
        printer.access_code,
        remote_path,
        dest,
        timeout=ftp_timeout,
        socket_timeout=ftp_timeout,
        printer_model=printer.model,
    )


async def locate_3mf_for_print(
    printer: Printer,
    subtask_name: str,
    filename: str,
    *,
    known_donor: DispatchDonor | None = None,
) -> ThreeMFLookup:
    """Find and download the 3MF the printer is running, into the archive temp dir.

    Order: shared download cache → direct FTPS fetch of each candidate name from
    every known remote directory → directory listing search by name → validation
    that the file's plate matches the plate the printer is actually running, with
    one corrected re-download when it does not (#1204).

    A miss returns a lookup whose ``found`` is False, carrying the corrected
    ``subtask_name`` so the caller's fallback archive is at least named for the
    right plate.

    ``known_donor`` is the SUPPLIED answer to everything above, and the only thing
    that distinguishes a farm dispatch from a foreign print here. The derivation
    lane exists because a foreign print's source is unknown — its name must be
    guessed from the MQTT echo and the guess must then be cross-checked, because a
    stale ``subtask_name`` across consecutive plates lands on the PREVIOUS plate's
    still-resident upload (#1204). A print the farm dispatched has no such problem:
    the queue item records the file and the plate, so
    ``farm_correlation.resolve_dispatch_donor`` hands them over and the guess-plus-
    check is skipped whole — not weakened, skipped, because there is nothing left to
    guess. Foreign prints keep the guard exactly as it was.

    Skipping it is not an optimisation, it is the fix for a production bleed: the
    2026-08-18 run printed a hand-spliced ladder file whose ``slice_info`` declares
    plate 1 while dispatch ran its ``Metadata/plate_3.gcode``. The cross-check read
    the internal 1, compared it to the 3 parsed from the printer's ``gcode_file``,
    called the farm's OWN file a stale-name mismatch and discarded it. The fallback
    archive that followed carried no 3MF, so ``usage_tracker`` had no slicer data —
    and on a tagless tray (``remain: -1``, always) the 3MF is the ONLY gram source,
    so 15 consecutive prints charged ZERO grams and two 1 kg rolls read "0 g used"
    after 5.8 h each.

    A donor whose bytes are no longer on disk falls through to the derivation lane
    rather than failing: the guessing path is a worse answer, never a wrong one.
    """
    if known_donor is not None:
        if known_donor.local_path.is_file():
            # The item's own plate outranks the gcode_file echo; fall back to the
            # echo only for a unit that carries no plate (single-plate / non-farm).
            expected = known_donor.plate_id if known_donor.plate_id is not None else parse_plate_id(filename)
            logger.info(
                "[CALLBACK] printer %s: queue item %s dispatched this print — using its known donor %s "
                "(plate %s), skipping the name-derived lookup and the #1204 plate cross-check",
                printer.id,
                known_donor.item_id,
                known_donor.filename,
                expected,
            )
            return ThreeMFLookup(
                local_path=known_donor.local_path,
                filename=known_donor.filename,
                subtask_name=subtask_name,
                expected_plate=expected,
            )
        logger.warning(
            "[CALLBACK] printer %s: queue item %s names donor %s but it is not on disk — "
            "falling back to the name-derived 3MF lookup",
            printer.id,
            known_donor.item_id,
            known_donor.local_path,
        )

    possible_names = _candidate_names(subtask_name, filename)
    logger.info("Trying filenames: %s", possible_names)

    temp_path: Path | None = None
    downloaded_filename: str | None = None

    # Cache check: cover endpoint may have already pulled this 3MF during
    # the print (frontend opens the card and shows the thumbnail) — reuse
    # that file instead of re-downloading 36MB over the same FTP link that
    # just served it (#972). The cache keys on a normalized filename so
    # variants like "X", "X.3mf", "X.gcode.3mf" all collapse to one entry.
    for try_filename in possible_names:
        cached = get_cached_3mf(printer.id, try_filename)
        if cached:
            logger.info("Reusing cached 3MF from %s (avoided duplicate FTP)", cached)
            temp_path = cached
            downloaded_filename = try_filename
            break

    retry_settings = await get_ftp_retry_settings()

    for try_filename in possible_names if not downloaded_filename else []:
        temp_path = app_settings.archive_dir / "temp" / try_filename
        temp_path.parent.mkdir(parents=True, exist_ok=True)

        for remote_path in (posixpath.join(directory, try_filename) for directory in _REMOTE_DIRS):
            logger.debug("Trying FTP download: %s", remote_path)
            try:
                downloaded = await _download(
                    printer,
                    remote_path,
                    temp_path,
                    retry_settings,
                    f"Download 3MF from {remote_path}",
                    non_retry_exceptions=(FileNotOnPrinterError,),
                )
                if downloaded:
                    downloaded_filename = try_filename
                    logger.info("Downloaded: %s", remote_path)
                    # Populate shared cache so the cover endpoint (if it
                    # runs next) doesn't refetch the same 36MB over FTP.
                    cache_3mf_download(printer.id, try_filename, temp_path)
                    break
            except FileNotOnPrinterError:
                # 550 — file isn't at this path. Advance to next candidate
                # without burning the retry budget.
                logger.debug("3MF not at %s (550), trying next path", remote_path)
            except Exception as e:  # noqa: BLE001 — any transport failure just means "try the next path"
                logger.debug("FTP download failed for %s: %s", remote_path, e)

        if downloaded_filename:
            break

    # If still not found, try listing directories to find matching file
    # Different printer models use different directory structures
    if not downloaded_filename and (filename or subtask_name):
        # ONE normaliser on BOTH sides — ``print_identity_key``, the fork's single
        # "is this the same print?" key. The private construction that stood here
        # could not match this farm's corpus at all: it dropped ``.gcode`` with an
        # unanchored replace on the SEARCH side but normalised the CANDIDATE with
        # spaces→underscores and lower-case only, leaving that side's mid-stem token
        # in place. Library files are named
        # ``Rotary_tool_top_surfaces_PCO-M12-2525.gcode_L1-90_spliced.3mf`` and the
        # printer echoes ``Rotary_tool_top_surfaces_PCO-M12-2525_L1-90_spliced``, so
        # the surviving ``.gcode`` sat INSIDE the candidate and broke contiguity —
        # the substring test could never be true for a spliced file, which is most
        # of what this farm prints. Same defect ``print_identity_key`` was created
        # to fix in terminal correlation and foreign-eject identity on 2026-08-22;
        # this module simply did not import it.
        search_key = print_identity_key(subtask_name or filename)
        logger.info("Direct FTP download failed, searching directories for '%s'", search_key)
        for search_dir in ("/cache", "/model", "/data", "/data/Metadata", "/"):
            if downloaded_filename:
                break
            try:
                dir_files = await list_files_async(
                    printer.ip_address, printer.access_code, search_dir, printer_model=printer.model
                )
                threemf_files = [f.get("name") for f in dir_files if f.get("name", "").endswith(".3mf")]
                if threemf_files:
                    logger.info(
                        "Found %d 3MF files in %s: %s%s",
                        len(threemf_files),
                        search_dir,
                        threemf_files[:5],
                        "..." if len(threemf_files) > 5 else "",
                    )
                # Exact key first, containment second — both on the SAME normalised
                # form. Equality is the answer whenever the printer's echo and the
                # stored file are the same print, and testing it first stops a
                # longer neighbour (``…_L1-88_spliced_v2.3mf``) from winning the
                # listing order over the file that actually matches. Containment
                # stays as the loose tier for an echo that carries only part of the
                # stored name; it is strictly WIDER than what it replaces, whose
                # loose tier was broken on one side and matched nothing here.
                names = [
                    f.get("name", "")
                    for f in dir_files
                    if not f.get("is_directory") and f.get("name", "").endswith(".3mf")
                ]
                exact = [n for n in names if print_identity_key(n) == search_key]
                loose = [n for n in names if search_key and n not in exact and search_key in print_identity_key(n)]
                for fname in exact + loose:
                    logger.info("Found matching file in %s: %s", search_dir, fname)
                    temp_path = app_settings.archive_dir / "temp" / fname
                    temp_path.parent.mkdir(parents=True, exist_ok=True)
                    remote_full_path = posixpath.join(search_dir, fname)
                    downloaded = await _download(
                        printer,
                        remote_full_path,
                        temp_path,
                        retry_settings,
                        f"Download 3MF from {remote_full_path}",
                    )
                    if downloaded:
                        downloaded_filename = fname
                        logger.info("Found and downloaded from %s: %s", search_dir, fname)
                        cache_3mf_download(printer.id, fname, temp_path)
                        break
            except Exception as e:  # noqa: BLE001 — an unlistable directory is just not the one
                logger.debug("Failed to list %s: %s", search_dir, e)

    expected_plate = parse_plate_id(filename)

    # Validate the downloaded 3MF actually CONTAINS the plate that's running
    # (#1204): subtask_name lags across consecutive plates of the same model,
    # so the first FTP candidate (built from subtask_name) can land on the
    # previous plate's still-resident upload. Cross-check the plate parsed from
    # gcode_file (always fresh — it's the field whose change triggered this
    # callback) against every plate index the slice_info declares.
    #
    # CONTAINMENT, not equality with the first block: a multi-plate upload
    # declares plate 1 first whichever plate is running, so "is this file's first
    # plate the running plate?" is false for every multi-plate 3MF this farm
    # prints. It threw away 57 correct files in 30 days — and a discarded 3MF is a
    # print charged ZERO grams on a tagless tray, whose ledger then reads fuller
    # than the roll is. A stale name still fails this test the same way it always
    # did: it fetches a DIFFERENT upload, whose index set does not contain the
    # running plate. An empty set is "unreadable", not "absent" — accept it.
    if downloaded_filename and temp_path:
        declared_plates = plate_indices_in_3mf(temp_path) if expected_plate is not None else set()
        if expected_plate is not None and declared_plates and expected_plate not in declared_plates:
            logger.warning(
                "[CALLBACK] 3MF plate mismatch: downloaded %s declares plates %s but printer is "
                "running plate %s — subtask_name=%r appears stale, retrying with corrected name",
                downloaded_filename,
                sorted(declared_plates),
                expected_plate,
                subtask_name,
            )
            corrected_subtask = swap_plate_suffix(subtask_name, expected_plate)
            retry_succeeded = False
            if corrected_subtask and corrected_subtask != subtask_name:
                for try_filename in (f"{corrected_subtask}.gcode.3mf", f"{corrected_subtask}.3mf"):
                    retry_temp_path = app_settings.archive_dir / "temp" / try_filename
                    retry_temp_path.parent.mkdir(parents=True, exist_ok=True)
                    for remote_path in (posixpath.join(directory, try_filename) for directory in _REMOTE_DIRS):
                        try:
                            downloaded = await _download(
                                printer,
                                remote_path,
                                retry_temp_path,
                                retry_settings,
                                f"Re-download 3MF from {remote_path}",
                                non_retry_exceptions=(FileNotOnPrinterError,),
                            )
                            # DELIBERATE EXCEPTION to plate_indices_in_3mf's "an empty
                            # set means UNKNOWN, so accept" contract: here an
                            # unreadable retry file yields an empty set and is
                            # REJECTED. Do not "correct" this to match the helper.
                            #
                            # The contract's rule protects a file already in hand —
                            # discarding it on an unreadable answer throws away the
                            # only gram source a tagless roll has. This branch is the
                            # opposite situation: it must positively confirm a
                            # REPLACEMENT before swapping it in. Accepting an
                            # unreadable one would attach a 3MF to the archive, and
                            # attaching pops ``no_3mf_available``
                            # (``ArchiveService.attach_3mf_to_archive``), which
                            # SUPPRESSES the zero-gram page — the farm would go quiet
                            # about a print it still cannot price. Rejecting keeps the
                            # loss loud and costs nothing: an unreadable slice_info
                            # yields no filament data either way.
                            if downloaded and expected_plate in plate_indices_in_3mf(retry_temp_path):
                                logger.info(
                                    "[CALLBACK] Re-download succeeded with corrected name %s "
                                    "(plate %s) — replacing wrong file",
                                    try_filename,
                                    expected_plate,
                                )
                                temp_path = retry_temp_path
                                downloaded_filename = try_filename
                                subtask_name = corrected_subtask
                                cache_3mf_download(printer.id, try_filename, temp_path)
                                retry_succeeded = True
                                break
                            elif downloaded:
                                # Wrong plate again — never published, so discard
                                # it here and keep trying.
                                cleanup_downloaded_3mf(retry_temp_path)
                        except FileNotOnPrinterError:
                            continue
                        except Exception as e:  # noqa: BLE001 — next path
                            logger.debug("Re-download failed for %s: %s", remote_path, e)
                    if retry_succeeded:
                        break
            # If the retry didn't find a matching file, drop the wrong 3MF
            # so the no-3MF fallback below creates an archive whose name
            # at least reflects the right plate.
            if not retry_succeeded:
                logger.warning(
                    "[CALLBACK] Could not re-download correct plate %s — falling back to no-3MF archive",
                    expected_plate,
                )
                temp_path = None
                downloaded_filename = None
                # Override the stale subtask_name so the fallback archive's
                # print_name reflects the correct plate. Prefer the swapped
                # name when we have one; otherwise let filename win.
                subtask_name = corrected_subtask or ""

    return ThreeMFLookup(
        # A miss leaves the last candidate's (never-written) temp path behind —
        # report no path rather than one nothing landed at.
        local_path=temp_path if downloaded_filename else None,
        filename=downloaded_filename,
        subtask_name=subtask_name,
        expected_plate=expected_plate,
    )


async def maybe_schedule_foreign_3mf_retry(
    db: AsyncSession,
    *,
    printer_id: int,
    archive_id: int,
    payload: dict,
    subtask_name: str,
    filename: str,
    delays: tuple[float, ...] = RETRY_DELAYS_S,
) -> bool:
    """Arm the in-flight 3MF capture retry for a print no farm queue item claims.

    Called from the print-start handler's no-3MF fallback branch only — a
    start-time capture that succeeded has nothing to retry. Attribution comes from
    the one correlation authority (:func:`resolve_terminal_item`): an attributed
    print is a farm dispatch whose 3MF already exists locally, so it is skipped.

    Returns True when a retry task was spawned.
    """
    try:
        resolution = await resolve_terminal_item(db, printer_id, payload)
    except Exception as e:  # noqa: BLE001 — a correlation failure must never break print start
        logger.warning("[FOREIGN-3MF] printer %s: correlation failed (%s) — no capture retry armed", printer_id, e)
        return False

    if resolution.item is not None:
        logger.debug(
            "[FOREIGN-3MF] printer %s archive %s: farm queue item %s claims this print (%s) — "
            "no capture retry needed, its 3MF is already local",
            printer_id,
            archive_id,
            resolution.item.id,
            resolution.verdict,
        )
        return False

    logger.info(
        "[FOREIGN-3MF] printer %s archive %s: no farm queue item claims this print (%s) and the start-time "
        "3MF capture missed — retrying in-flight at %s to keep the run chargeable",
        printer_id,
        archive_id,
        resolution.verdict,
        ", ".join(f"+{int(d)}s" for d in delays),
    )
    spawn_background_task(
        _retry_capture(printer_id, archive_id, subtask_name, filename, delays),
        name=f"foreign-3mf-capture-{printer_id}-{archive_id}",
    )
    return True


async def _retry_capture(
    printer_id: int,
    archive_id: int,
    subtask_name: str,
    filename: str,
    delays: tuple[float, ...],
) -> None:
    """Re-attempt the capture while the print runs; attach the first hit."""
    for attempt, delay in enumerate(delays, start=1):
        try:
            await asyncio.sleep(delay)
            if await _attempt_capture(printer_id, archive_id, subtask_name, filename, attempt):
                return
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — one failed attempt must not cancel the next
            logger.warning(
                "[FOREIGN-3MF] printer %s archive %s: capture attempt %d failed: %s",
                printer_id,
                archive_id,
                attempt,
                e,
            )

    logger.warning(
        "[FOREIGN-3MF] printer %s archive %s: no source 3MF captured after %d in-flight attempts — "
        "this print has no slicer data and will charge no filament",
        printer_id,
        archive_id,
        len(delays),
    )


async def _attempt_capture(
    printer_id: int,
    archive_id: int,
    subtask_name: str,
    filename: str,
    attempt: int,
) -> bool:
    """One capture attempt. Returns True when the retry is over (attached or moot)."""
    async with async_session() as db:
        archive = await db.get(PrintArchive, archive_id)
        if archive is None:
            logger.info("[FOREIGN-3MF] archive %s is gone — abandoning capture", archive_id)
            return True
        if archive.file_path:
            # The terminal path (or an earlier attempt) already attached one.
            logger.info("[FOREIGN-3MF] archive %s already has a 3MF — capture retry stands down", archive_id)
            return True

        printer = await db.get(Printer, printer_id)
        if printer is None:
            logger.info("[FOREIGN-3MF] printer %s is gone — abandoning capture for archive %s", printer_id, archive_id)
            return True

        # Re-derive from the live printer state: the enriched subtask_name /
        # gcode_file often only arrive after the start callback, and they are what
        # names the file on disk. Only while this print still runs — once the row
        # is terminal the live state may describe the NEXT job, whose 3MF must
        # never be charged to this one.
        live_subtask, live_filename = subtask_name, filename
        if archive.status == "printing":
            state = printer_manager.get_status(printer_id)
            if state is not None:
                live_subtask = getattr(state, "subtask_name", None) or subtask_name
                live_filename = getattr(state, "gcode_file", None) or filename

        logger.info(
            "[FOREIGN-3MF] printer %s archive %s: capture attempt %d (subtask=%r, file=%r)",
            printer_id,
            archive_id,
            attempt,
            live_subtask,
            live_filename,
        )
        lookup = await locate_3mf_for_print(printer, live_subtask, live_filename)
        if not lookup.found or lookup.local_path is None:
            return False

        attached = await ArchiveService(db).attach_3mf_to_archive(archive, lookup.local_path, lookup.expected_plate)
        if attached:
            logger.info(
                "[FOREIGN-3MF] printer %s archive %s: captured %s on attempt %d — the run is chargeable again",
                printer_id,
                archive_id,
                lookup.filename,
                attempt,
            )
        return attached
