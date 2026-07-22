"""Helpers for the (server-dispatched) auto-eject pipeline.

The eject sweep is a SEPARATE motion-only job now — print files dispatch
unmodified and never carry an injected eject block. This module keeps the two
pure, reusable pieces that survived that move:

- :func:`build_part_present_eject_file` — build a standalone, motion-only
  eject-only ``.gcode.3mf`` (the file the shared remote dispatcher uploads).
- :func:`resolve_cooldown_override` — the run-level cooldown-release override the
  eject MONITOR reads for its server-side release threshold.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select

from backend.app.models.print_batch import PrintBatch
from backend.app.services.eject.build_cache import EjectBuildError, get_or_build_eject_file
from backend.app.services.eject.generator import (
    EjectGenerationError,
    generate_eject_gcode,
)
from backend.app.services.eject.validator import validate_eject_gcode
from backend.app.utils.threemf_tools import read_plate_gcode_header

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.app.models.eject_profile import EjectProfile
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


async def build_part_present_eject_file(
    source_path: Path,
    plate_id: int,
    profile: EjectProfile,
    geometry: ModelGeometry,
) -> Path:
    """Build a standalone PART-PRESENT, MOTION-ONLY eject-only ``.gcode.3mf`` for ``plate_id``.

    The plate's G-code is REPLACED ENTIRELY (via ``repack_3mf_with_gcode``, MD5
    recomputed) by the generated eject block: prologue ``M17`` → home X/Y only —
    single-nozzle models use ``G28 X Y``, dual-nozzle (H2C/H2D/X2D) models use the
    torque-parameterized ``G28 X T300`` / ``G28 Y T300`` forms (a bare ``G28 X Y``
    stall-loops that firmware). NEVER a bare ``G28`` / ``G28 Z`` — the part sits on
    the plate, so the block relies on the retained Z datum. Then ``M140 S0``, the
    optional bed-drop release assist, the sweep, the park, then the completion
    epilogue. There is NO in-file cooldown wait: the eject monitor
    already held the plate gate until the live bed reached the release threshold
    before this motion-only job is dispatched. The generator emits exactly that
    shape; the validator re-checks geometry / homing / tool-state.

    HARDWARE LADDER: the retained-Z assumption MUST be validated on an empty-bed
    dry run before this is used unattended in production.

    The artifact is built ONE-PASS (``repack_3mf_eject``): the plate G-code+MD5
    replacement, the ``slice_info.config`` usage-zeroing and the slim member drop
    (object meshes + plate thumbnails) happen in a single ZIP rewrite, so this
    motion-only file reports ZERO filament / print-time usage — it extrudes nothing,
    and must not inherit the donor's plate weight / prediction. The build runs OFF the
    event loop and is cached by ``(gcode, donor, plate)`` via
    :func:`get_or_build_eject_file` (latency Phase C2); the cheap gcode
    generation+validation stays here (the cache key needs the final gcode text).

    Returns the temp ``.gcode.3mf`` :class:`Path` (caller cleans it up). Raises
    :class:`EjectGenerationError` on any failure.
    """
    max_z = _parse_max_z_height(Path(source_path), plate_id)
    if max_z is None:
        raise EjectGenerationError("Could not parse max_z_height from the 3MF gcode header")

    block = generate_eject_gcode(profile, max_z, geometry)
    validation = validate_eject_gcode(block, profile, max_z, geometry)
    if not validation.ok:
        raise EjectGenerationError("Part-present eject validation failed: " + "; ".join(validation.errors))

    try:
        return await get_or_build_eject_file(Path(source_path), plate_id, block)
    except EjectBuildError as exc:
        raise EjectGenerationError(f"Failed to repack the part-present eject 3mf: {exc}") from exc
