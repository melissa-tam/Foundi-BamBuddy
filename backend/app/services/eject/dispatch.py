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

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select

from backend.app.models.print_batch import PrintBatch
from backend.app.services.eject.build_cache import EjectBuildError, get_or_build_eject_file
from backend.app.services.eject.generator import (
    EjectGenerationError,
    estimate_runtime_segments,
    generate_eject_gcode,
)
from backend.app.services.eject.validator import validate_eject_gcode
from backend.app.utils.threemf_tools import read_plate_gcode_header

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.app.models.eject_profile import EjectProfile
    from backend.app.services.eject.geometry import ModelGeometry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BuiltEject:
    """A built eject artifact plus the runtime the machine is expected to need for it.

    The estimate travels WITH the file because it is only derivable at build time
    (from the exact block that was generated for this part height / profile /
    geometry) and is consumed much later, at the eject job's terminal, to decide
    whether the sweep can be trusted. Dispatch threads it onto the
    :class:`~backend.app.services.eject.remote.PendingEject` that survives until then.

    ``drop_span_s`` is the bed-drop phase's own budget (the commanded time between the
    M73 phase beacons), and is set ONLY for a profile that actually drops the bed. A
    drop-less block's pre-sweep motion is just the prologue lift, which cannot stall
    against an under-bed obstruction, so it carries None and the watchdog's edge lane
    stays disarmed rather than timing a phase that does not exist.

    ``sweep_span_s`` and ``tail_s`` are the two post-drop budgets, and unlike the drop
    they are unconditional: every generated block sweeps and every generated block ends
    in the park + completion epilogue, so both are always figures rather than an
    optional tuning's by-product.
    """

    path: Path
    expected_runtime_s: float
    drop_span_s: float | None
    sweep_span_s: float
    tail_s: float


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
    max_z_override: float | None = None,
) -> BuiltEject:
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

    ``max_z_override`` is the operator's confirmed part height (the foreign "Eject now"
    confirm dialog): when given it supersedes the donor header, because that donor may
    be an ASSUMED fallback rather than the print actually on the plate. It feeds the
    generator AND the validator exactly as a parsed height does — the profile's
    ``max_part_height_mm`` guard stays the one authority on a refusable height, so no
    validation is duplicated here.

    Returns a :class:`BuiltEject` — the temp ``.gcode.3mf`` path (caller cleans it
    up) plus the runtime the block is expected to take. The estimate is taken from
    the EJECT BLOCK text, which is exactly what replaces the plate G-code, i.e.
    exactly what the printer executes; anything else in the archive is inert. Raises
    :class:`EjectGenerationError` on any failure.
    """
    max_z = max_z_override if max_z_override is not None else _parse_max_z_height(Path(source_path), plate_id)
    if max_z is None:
        raise EjectGenerationError("Could not parse max_z_height from the 3MF gcode header")

    block = generate_eject_gcode(profile, max_z, geometry)
    validation = validate_eject_gcode(block, profile, max_z, geometry)
    if not validation.ok:
        raise EjectGenerationError("Part-present eject validation failed: " + "; ".join(validation.errors))
    segments = estimate_runtime_segments(block)
    # The drop span is only meaningful when the block actually drops the bed — see
    # BuiltEject.drop_span_s. Between the beacons a drop-less block holds nothing but
    # `M140 S0`, so publishing its ~0 s span would arm the edge lane on a phase that
    # cannot stall.
    drop_span_s = segments.drop_span_s if profile.bed_drop_clearance_mm is not None else None

    try:
        path = await get_or_build_eject_file(Path(source_path), plate_id, block)
    except EjectBuildError as exc:
        raise EjectGenerationError(f"Failed to repack the part-present eject 3mf: {exc}") from exc
    logger.info(
        "eject.dispatch: built part-present eject from %s plate %s (max_z %.2fmm, profile %r) — expected runtime "
        "%.0fs (pre %.0fs, bed-drop span %s, sweep span %.0fs, tail %.0fs)",
        Path(source_path).name,
        plate_id,
        max_z,
        profile.name,
        segments.total_s,
        segments.pre_s,
        f"{drop_span_s:.0f}s" if drop_span_s is not None else "off",
        segments.sweep_span_s,
        segments.tail_s,
    )
    return BuiltEject(
        path=path,
        expected_runtime_s=segments.total_s,
        drop_span_s=drop_span_s,
        sweep_span_s=segments.sweep_span_s,
        tail_s=segments.tail_s,
    )
