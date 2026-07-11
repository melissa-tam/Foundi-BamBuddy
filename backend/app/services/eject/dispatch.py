"""Scheduler dispatch helper for the auto-eject pipeline.

Keeps the eject-specific logic out of the print scheduler: given a queue item
with an ``eject_profile_id``, resolve the profile, parse the part height from the
3MF header, generate the eject block and validate it. The scheduler injects the
returned snippet as the machine-end block (superseding any global per-model end
snippet). On any failure this returns an error string so the scheduler fails the
item instead of dispatching an unprotected (or unsafe) eject.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select

from backend.app.models.eject_profile import EjectProfile
from backend.app.models.print_batch import PrintBatch
from backend.app.services.eject.generator import (
    EjectGenerationError,
    generate_eject_gcode,
)
from backend.app.services.eject.geometry import GeometryUnavailable, get_geometry_required
from backend.app.services.eject.validator import validate_eject_gcode
from backend.app.utils.threemf_tools import read_plate_gcode_header, repack_3mf_with_gcode

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.printer import Printer
    from backend.app.services.eject.geometry import ModelGeometry


def _parse_max_z_height(source_path: Path, plate_id: int) -> float | None:
    """Read `max_z_height` (mm) from the plate's 3MF gcode header, or None."""
    header = read_plate_gcode_header(source_path, plate_id)
    raw = header.get("max_z_height")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


async def resolve_cooldown_override(db: AsyncSession, batch_id: int | None) -> float | None:
    """Return the run-level cooldown override for ``batch_id``, or ``None``.

    Farm production runs may override the eject cooldown gate per-run; the value
    lives on the run's :class:`PrintBatch` (``cooldown_temp_c_override``). When
    set it supersedes the profile's ``cooldown_temp_c`` for the eject block's
    ``M190 R`` threshold — the single source of truth shared by dispatch (block
    generation + validation) and the cooldown monitor's release threshold, so the
    in-file wait and the server-side gate never disagree. Returns ``None`` when
    the item has no batch or the run set no override (caller falls back to the
    profile value).
    """
    if batch_id is None:
        return None
    result = await db.execute(select(PrintBatch.cooldown_temp_c_override).where(PrintBatch.id == batch_id))
    return result.scalar_one_or_none()


async def build_eject_snippet(
    db: AsyncSession,
    item: PrintQueueItem,
    printer: Printer,
    source_path: Path,
) -> tuple[str | None, str | None]:
    """Build the validated eject end-snippet for `item`.

    Assumes ``item.eject_profile_id`` is set (caller checks). Returns
    ``(snippet, None)`` on success or ``(None, error_message)`` on failure — the
    scheduler must fail the item on an error rather than dispatch it.

    First-article items get ``(None, None)`` — a deliberate *skip*, not an error:
    the eject block must NOT be injected so the printed part stays on the plate
    for operator inspection. The scheduler treats a ``(None, None)`` result as
    "dispatch with no eject supersede".
    """
    if getattr(item, "first_article", False):
        return None, None

    result = await db.execute(select(EjectProfile).where(EjectProfile.id == item.eject_profile_id))
    profile = result.scalar_one_or_none()
    if profile is None:
        return None, f"Eject profile {item.eject_profile_id} not found"

    # Resolve the target model's bed/envelope from the registry — fail-closed on a
    # model with no geometry row OR a row not yet hardware-validated (production
    # dispatch requires validation; the reason distinguishes the two causes). This
    # is the canonical-match replacement for the old raw ``model in PRINTER_BED_DIMS``
    # membership test, so ``O1S`` and ``H2S`` no longer diverge.
    try:
        geometry = await get_geometry_required(db, printer.model, require_validated=True)
    except GeometryUnavailable as exc:
        return None, exc.reason

    plate_id = item.plate_id or 1
    max_z = _parse_max_z_height(Path(source_path), plate_id)
    if max_z is None:
        return None, "Could not parse max_z_height from the 3MF gcode header"

    # Farm production runs may override the cooldown gate per-run; the override
    # (when set) supersedes the profile's cooldown_temp_c for THIS item's eject
    # block so generation + validation share the effective M190 R threshold.
    cooldown_override = await resolve_cooldown_override(db, item.batch_id)

    try:
        gcode = generate_eject_gcode(profile, max_z, geometry, cooldown_temp_c=cooldown_override)
    except EjectGenerationError as exc:
        return None, f"Eject generation refused: {exc}"

    validation = validate_eject_gcode(gcode, profile, max_z, geometry, cooldown_temp_c=cooldown_override)
    if not validation.ok:
        return None, "Eject validation failed: " + "; ".join(validation.errors)

    return gcode, None


def build_part_present_eject_file(
    source_path: Path,
    plate_id: int,
    profile: EjectProfile,
    geometry: ModelGeometry,
    cooldown_temp_c: float | None = None,
) -> Path:
    """Build a standalone PART-PRESENT eject-only ``.gcode.3mf`` for ``plate_id``.

    The plate's G-code is REPLACED ENTIRELY (via ``repack_3mf_with_gcode``, MD5
    recomputed) by the generated eject block: prologue ``M17`` → ``G28 X Y`` only
    (NEVER a bare ``G28`` / ``G28 Z`` — the part sits on the plate, so the block
    relies on the retained Z datum), then the cooldown loop (``M190 R`` retries at
    the effective threshold — the bed is already cooling from the finished
    print), the sweep and the park. The generator already emits exactly that
    shape; the validator's existing rules (forbid bare ``G28``, enforce the
    ``M190 R`` count/threshold and the sweep envelope) re-check it.

    HARDWARE LADDER: the retained-Z assumption MUST be validated on an empty-bed
    dry run before this is used unattended in production.

    Returns the temp ``.gcode.3mf`` :class:`Path` (caller cleans it up). Raises
    :class:`EjectGenerationError` on any failure.
    """
    max_z = _parse_max_z_height(Path(source_path), plate_id)
    if max_z is None:
        raise EjectGenerationError("Could not parse max_z_height from the 3MF gcode header")

    block = generate_eject_gcode(profile, max_z, geometry, cooldown_temp_c=cooldown_temp_c)
    validation = validate_eject_gcode(block, profile, max_z, geometry, cooldown_temp_c=cooldown_temp_c)
    if not validation.ok:
        raise EjectGenerationError("Part-present eject validation failed: " + "; ".join(validation.errors))

    out = repack_3mf_with_gcode(Path(source_path), plate_id, block)
    if out is None:
        raise EjectGenerationError("Failed to repack the part-present eject 3mf")
    return out
