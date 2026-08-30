"""Eject-donor resolution — a Chain of Responsibility over three tiers.

An eject sweep is built by REPACKING a donor ``.gcode.3mf``: the build replaces the
chosen plate's G-code with motion-only sweep G-code and recomputes the MD5 sidecar
(``eject/dispatch.py``). So "which file do we build the eject from?" is a question
with several possible answers of decreasing certainty, and until this module existed
those answers were two hand-rolled resolvers inside ``eject/manual.py`` plus an
``except`` that chained one into the other.

The three tiers, strongest first:

* :class:`GateSubtaskArchive` — the archive of the print that RAISED this plate gate.
  The only tier that positively identifies what is on the bed, so it is the only one
  the fail-closed AUTO lane may use.
* :class:`LastFarmItemFile` — the printer's last-started farm unit's own on-disk file
  (its archive copy, else its library source). An ASSUMED identity: the farm knows
  what it last put on that printer, not what a human restarted from the screen. This
  is the 2026-07-21 degenerate-echo rescue.
* :class:`ContainerLibraryFile` — any on-disk library file sliced for this printer's
  model. It identifies NOTHING; it is a valid ZIP skeleton and nothing more.

The tiers differ in what they can tell the operator, not in what they can build:

* the build replaces the plate's G-code ENTIRELY (``dispatch.py`` — the donor's own
  motion never reaches the printer), and
* ``max_z_override`` supersedes the donor's parsed header height (``dispatch.py``),

so a donor is only a CONTAINER once the operator supplies the part height — which is
exactly the shape the dry-run lane has always had. That is why the container tier is
architecturally safe and why it is fail-closed on height: it returns ``max_z=None``,
and the manual lane refuses to confirm a container sweep without an operator figure.

**Composition is declared here, once.** :data:`AUTO_DONOR_CHAIN` is the strict tier
alone — the automatic foreign eject runs unattended, so an assumed or anonymous donor
must never reach it. :data:`MANUAL_DONOR_CHAIN` is all three, because every tier below
the first is answered by an operator looking at the plate before confirming.
"""

from __future__ import annotations

import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.utils.printer_models import canon_model
from backend.app.utils.threemf_tools import list_gcode_plate_ids, read_plate_gcode_header

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DonorSource:
    """A resolved eject donor: the file the sweep is repacked from.

    ``max_z`` is the part height parsed from the donor plate's G-code header, and it
    is ``None`` when the donor cannot speak to what is on the plate (the container
    tier). ``None`` is NOT "zero" and never a build input on its own — the manual
    lane turns it into an operator prompt.

    ``print_name`` names the print for the confirm dialog, ``None`` when the donor
    does not know it. ``tmp_path`` is set only when the donor was FTPS re-fetched
    into a temp file; the caller owns that file's lifecycle (see
    :func:`release_donor`).
    """

    path: Path
    plate_id: int
    max_z: float | None
    print_name: str | None
    tmp_path: Path | None = None


@dataclass(frozen=True)
class DonorContext:
    """Everything a tier may consult. No tier reads anything else.

    ``plate_source`` is the plate gate's source ``subtask_id`` as the OCCUPANCY
    AUTHORITY reports it (``plate_occupancy.plate_source``) — never
    ``Printer.plate_gate_subtask_id``. That column is write-only persistence since
    the 2026-08-30 cut-over: a failed persist leaves it holding a dead key, and a
    resolver steering off that key would build a sweep for a print that is not on
    this plate.

    ``item`` is the farm queue unit the CALLER already knows this plate belongs to,
    when it knows one. Supplying it lets :class:`LastFarmItemFile` answer for that
    exact unit instead of re-deriving "the printer's last-started unit"; leaving it
    ``None`` is the foreign lane, where the printer's last start is the best the farm
    can offer.
    """

    db: AsyncSession
    printer: Printer
    plate_source: str | None
    item: PrintQueueItem | None = None


# --------------------------------------------------------------------------- #
# Temp-donor lifecycle + the FTPS re-fetch cache (latency Phase D1)
# --------------------------------------------------------------------------- #
# The foreign "Eject plate" flow resolves a donor TWICE: once for the needs-input
# prompt (which may FTPS-download the donor) and again on the confirmed call; the
# AUTO foreign path (identify -> cooldown watch -> dispatch) fetches twice for the
# same reason. This module-level TTL cache lets the FIRST resolve DEPOSIT the fetched
# temp file and the SECOND resolve CONSUME it — two FTPS downloads become one.
#
# Key = ``(printer_id, plate_source)`` — the SAME gate both resolves operate on (one
# gate per printer). A gate RE-RAISE for a DIFFERENT subtask yields a different key,
# so a stale donor is never served for a new print. Only the expensive re-fetch
# (``tmp_path is not None``) is ever cached; an on-disk donor is a no-op deposit.
# Entries expire after ``_DONOR_TTL_S`` and are swept (files unlinked) lazily on every
# access. A CONSUMED entry is removed — the consumer owns the file's lifecycle exactly
# as a fresh fetch does.
_DONOR_TTL_S = 600.0  # ~10 min
_donor_cache: dict[tuple[int, str], tuple[Path, float]] = {}


def release_donor_temp(path: Path | None) -> None:
    """Best-effort delete of a temp donor file (no-op for None / on-disk donors)."""
    if path is None:
        return
    from backend.app.services.bambu_ftp import cleanup_downloaded_3mf

    cleanup_downloaded_3mf(path)


def release_donor(source: DonorSource | None) -> None:
    """Release a resolved donor's temp file, if it had one. Safe on ``None``."""
    if source is not None:
        release_donor_temp(source.tmp_path)


def donor_cache_key(printer_id: int, plate_source: str | None) -> tuple[int, str]:
    """The cache key for one printer's current plate gate."""
    return (printer_id, plate_source or "")


def _sweep_expired(now: float) -> None:
    """Unlink + drop every cache entry past its TTL (lazy sweep, on each access)."""
    for key in [k for k, (_p, exp) in _donor_cache.items() if exp <= now]:
        path, _exp = _donor_cache.pop(key)
        release_donor_temp(path)


def deposit_donor(printer_id: int, plate_source: str | None, tmp_path: Path | None) -> None:
    """DEPOSIT a re-fetched donor temp file for this gate (no-op for ``None``).

    An existing entry for the key is unlinked first (never leak a superseded temp).
    """
    now = time.monotonic()
    _sweep_expired(now)
    if tmp_path is None:
        return
    key = donor_cache_key(printer_id, plate_source)
    existing = _donor_cache.pop(key, None)
    if existing is not None and existing[0] != tmp_path:
        release_donor_temp(existing[0])
    _donor_cache[key] = (tmp_path, now + _DONOR_TTL_S)


def _take_donor(printer_id: int, plate_source: str | None) -> Path | None:
    """CONSUME (pop) the cached donor for this gate — the caller now owns the file.

    Returns the path when a live, on-disk entry exists; ``None`` on miss / expiry / a
    vanished file (a stale entry is unlinked and dropped)."""
    _sweep_expired(time.monotonic())
    entry = _donor_cache.pop(donor_cache_key(printer_id, plate_source), None)
    if entry is None:
        return None
    path, _exp = entry
    if not path.is_file():
        release_donor_temp(path)
        return None
    return path


# --------------------------------------------------------------------------- #
# Shared plate / height helpers
# --------------------------------------------------------------------------- #
def resolve_plate_id(donor_path: Path, filename: str | None) -> int | None:
    """Pick the ejectable plate id for a donor, or None if unresolvable.

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


def read_max_z(donor_path: Path, plate_id: int) -> float | None:
    """The plate's parsed ``max_z_height``, or None when the header does not carry one."""
    header = read_plate_gcode_header(donor_path, plate_id)
    raw = header.get("max_z_height")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


async def _fetch_donor(printer: Printer, filename: str | None) -> Path | None:
    """FTPS re-fetch a print's donor file by ``filename`` into a temp file.

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
        logger.warning("[donor] FTPS re-fetch of donor %r failed: %s", filename, exc)
        release_donor_temp(temp_path)
        return None
    if not ok:
        release_donor_temp(temp_path)
        return None
    return temp_path


async def source_from_item(db: AsyncSession, printer: Printer, item: PrintQueueItem) -> DonorSource | None:
    """The ON-DISK donor for one farm queue unit, or None.

    The one item→donor body, shared by :class:`LastFarmItemFile` and every caller that
    already knows which unit a plate belongs to. Deliberately DISK-ONLY (never an FTPS
    re-fetch — the strict tier already asked the wire; this is the local fallback), in
    priority order:

    * (a) ``item.archive_id`` → :class:`PrintArchive` whose ``file_path`` is non-empty
      and exists on disk (``base_dir / file_path``);
    * (b) else ``item.library_file_id`` → :class:`LibraryFile` resolved with the
      established absolute-or-``base_dir`` pattern;
    * (c) neither on disk → ``None``.

    Plate id prefers the item's own ``plate_id`` when the donor actually carries it,
    else the filename-hint / single-G-code-plate resolution (never a blind guess); the
    height is parsed from that plate's G-code header. Any unresolved step returns None.
    """
    from backend.app.core.config import settings as app_settings

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
        logger.info("[donor] p%s item %s has no on-disk donor (archive/library both absent)", printer.id, item.id)
        return None

    plates = list_gcode_plate_ids(donor_path)
    if not plates:
        logger.info("[donor] p%s item %s donor %s carries no G-code plate", printer.id, item.id, donor_path.name)
        return None
    if item.plate_id is not None and item.plate_id in plates:
        plate_id = item.plate_id
    else:
        resolved_plate = resolve_plate_id(donor_path, display_name)
        if resolved_plate is None:
            logger.info("[donor] p%s item %s donor plate ambiguous (plates=%s)", printer.id, item.id, plates)
            return None
        plate_id = resolved_plate

    max_z = read_max_z(donor_path, plate_id)
    if max_z is None:
        logger.info("[donor] p%s item %s donor plate %s has no max_z header", printer.id, item.id, plate_id)
        return None

    return DonorSource(path=donor_path, plate_id=plate_id, max_z=max_z, print_name=print_name, tmp_path=None)


# --------------------------------------------------------------------------- #
# The tiers
# --------------------------------------------------------------------------- #
class DonorHandler(ABC):
    """One tier of the donor chain.

    Stateless by construction — a tier holds no per-request state, so the chains below
    are tuples of the CLASSES and :func:`resolve_donor` calls ``Tier.resolve(ctx)``
    directly. Every tier answers ``None`` for "not my case" and logs WHY, so a plate
    that ends up unresolvable leaves a readable trail of which tiers declined and on
    what evidence (the 2026-08-22 lesson: a silent ``None`` hid a gate that was
    refusing the entire production corpus).
    """

    @staticmethod
    @abstractmethod
    async def resolve(ctx: DonorContext) -> DonorSource | None:
        """The donor this tier can vouch for, or None."""


class GateSubtaskArchive(DonorHandler):
    """Tier 1 — the archive of the print that RAISED this plate gate.

    The only tier that positively identifies what is on the bed: the gate's source
    ``subtask_id`` names one job, and this printer's archive row for that job records
    the file it ran. Donor = the on-disk archive copy if present, else a Phase-D1
    cached re-fetch, else a fresh FTPS re-fetch from the printer's USB.

    Fail-closed by construction, which is why it is the AUTO chain's only member.
    """

    @staticmethod
    async def resolve(ctx: DonorContext) -> DonorSource | None:
        from backend.app.core.config import settings as app_settings

        printer = ctx.printer
        gate = ctx.plate_source
        if not gate:
            logger.info("[donor] p%s tier=gate_archive declined — the plate gate carries no source id", printer.id)
            return None

        result = await ctx.db.execute(
            select(PrintArchive)
            .where(PrintArchive.subtask_id == gate, PrintArchive.printer_id == printer.id)
            .order_by(PrintArchive.id.desc())
            .limit(1)
        )
        archive = result.scalar_one_or_none()
        if archive is None:
            logger.info("[donor] p%s tier=gate_archive declined — no archive row for gate %r", printer.id, gate)
            return None

        # Donor file — on disk if the archive copy exists, else a Phase-D1 cached
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
            # A prior resolve (the needs-input prompt, or the auto path's identify) may
            # have DEPOSITED the fetched donor for this exact gate — consume it and skip
            # the second download entirely (caller owns the file, same as a fresh fetch).
            tmp_path = _take_donor(printer.id, gate)
            if tmp_path is None:
                tmp_path = await _fetch_donor(printer, archive.filename)
            if tmp_path is None:
                logger.info(
                    "[donor] p%s tier=gate_archive declined — archive %s has no on-disk copy and %r is unfetchable",
                    printer.id,
                    archive.id,
                    archive.filename,
                )
                return None
            donor_path = tmp_path

        plate_id = resolve_plate_id(donor_path, archive.filename)
        if plate_id is None:
            release_donor_temp(tmp_path)
            logger.info("[donor] p%s tier=gate_archive declined — no unambiguous G-code plate", printer.id)
            return None

        max_z = read_max_z(donor_path, plate_id)
        if max_z is None:
            release_donor_temp(tmp_path)
            logger.info("[donor] p%s tier=gate_archive declined — plate %s has no max_z header", printer.id, plate_id)
            return None

        return DonorSource(
            path=donor_path,
            plate_id=plate_id,
            max_z=max_z,
            print_name=archive.print_name,
            tmp_path=tmp_path,
        )


class LastFarmItemFile(DonorHandler):
    """Tier 2 — the caller's known unit, else the printer's last-started farm unit.

    The 2026-07-21 degenerate-echo rescue: a screen RESTART of the farm's own USB file
    echoes an empty ``subtask_id`` and leaves the auto-archive as a download-failed
    fallback row, so tier 1 has nothing to tie. The farm still knows what it last put
    on that printer — the most-recently-started queue unit records the library file /
    archive it was dispatched from.

    The identity is ASSUMED, not proven, which is why this tier is MANUAL-only: the
    operator confirms the file name and the part height in the dialog before anything
    sweeps.
    """

    @staticmethod
    async def resolve(ctx: DonorContext) -> DonorSource | None:
        from backend.app.services.plate_occupancy_store import latest_started_item

        item = ctx.item
        if item is None:
            item = await latest_started_item(ctx.db, ctx.printer.id)
        if item is None:
            logger.info(
                "[donor] p%s tier=last_farm_item declined — the printer has never started a unit", ctx.printer.id
            )
            return None
        return await source_from_item(ctx.db, ctx.printer, item)


class ContainerLibraryFile(DonorHandler):
    """Tier 3 — the newest on-disk library file sliced for this printer's model.

    It identifies NOTHING. It is a ZIP skeleton with a G-code-bearing plate, and that
    is all an eject build needs from a donor: the plate's G-code is replaced ENTIRELY
    with the generated sweep and ``max_z_override`` supersedes the header height, so
    once the operator supplies the part height the donor's own contents never reach
    the printer. That is precisely the dry-run lane's shape, and the reason this tier
    is architecturally safe rather than a guess.

    It therefore returns ``max_z=None`` and ``print_name=None`` — the two facts a
    container genuinely cannot supply — and the manual lane refuses to confirm a
    container sweep until the operator has supplied the height.

    Model match is required (``file_metadata["sliced_for_model"]`` canonicalised
    against the printer's model), because a plate sliced for another machine can carry
    a bed size the eject generator would validate against the wrong envelope.
    """

    @staticmethod
    async def resolve(ctx: DonorContext) -> DonorSource | None:
        from backend.app.core.config import settings as app_settings

        printer = ctx.printer
        want_model = canon_model(printer.model)
        if want_model is None:
            logger.info("[donor] p%s tier=container declined — the printer row carries no model", printer.id)
            return None

        result = await ctx.db.execute(
            select(LibraryFile)
            .where(LibraryFile.deleted_at.is_(None))
            .where(LibraryFile.filename.ilike("%.gcode.3mf"))
            .order_by(LibraryFile.id.desc())
        )
        scanned = 0
        for row in result.scalars():
            scanned += 1
            sliced_for = (row.file_metadata or {}).get("sliced_for_model")
            if canon_model(sliced_for) != want_model:
                continue
            raw = Path(row.file_path)
            path = raw if raw.is_absolute() else app_settings.base_dir / row.file_path
            if not path.is_file():
                continue
            plates = list_gcode_plate_ids(path)
            if not plates:
                continue
            # Any G-code-bearing plate is a valid container: the plate this names is the
            # one whose G-code the build overwrites, so the choice decides which member
            # is replaced, not what the printer moves. Tier 1's hint/ambiguity rules
            # exist to identify WHICH plate produced the part on the bed — a question a
            # container is never asked.
            plate_id = min(plates)
            logger.warning(
                "[donor] p%s container-only donor: library file %s (%r, plate %s) supplies the eject skeleton — "
                "it identifies nothing on the plate, so the operator's part height is required to build the sweep",
                printer.id,
                row.id,
                row.filename,
                plate_id,
            )
            return DonorSource(path=path, plate_id=plate_id, max_z=None, print_name=None, tmp_path=None)

        logger.info(
            "[donor] p%s tier=container declined — none of %d library .gcode.3mf rows is an on-disk %s slice",
            printer.id,
            scanned,
            want_model,
        )
        return None


# --------------------------------------------------------------------------- #
# Compositions — declared once
# --------------------------------------------------------------------------- #
#: The UNATTENDED lane's chain: the strict tier alone. The automatic foreign eject
#: dispatches with no operator present, so a donor whose identity is assumed
#: (:class:`LastFarmItemFile`) or absent (:class:`ContainerLibraryFile`) must never
#: reach it — a farm red line, not a tuning choice.
AUTO_DONOR_CHAIN: tuple[type[DonorHandler], ...] = (GateSubtaskArchive,)

#: The OPERATOR lane's chain: every tier, because each one below the first is
#: answered by a human looking at the plate before confirming the sweep.
MANUAL_DONOR_CHAIN: tuple[type[DonorHandler], ...] = (
    GateSubtaskArchive,
    LastFarmItemFile,
    ContainerLibraryFile,
)


async def resolve_donor(chain: tuple[type[DonorHandler], ...], ctx: DonorContext) -> DonorSource | None:
    """Walk ``chain`` in order and return the first donor a tier vouches for.

    ``None`` means every tier declined — for the manual chain that is a genuinely
    empty install (nothing on disk at all), and the operator's only move is to clear
    the plate by hand.
    """
    for tier in chain:
        source = await tier.resolve(ctx)
        if source is not None:
            logger.info(
                "[donor] p%s resolved by tier=%s (plate %s, max_z=%s)",
                ctx.printer.id,
                tier.__name__,
                source.plate_id,
                source.max_z,
            )
            return source
    return None
