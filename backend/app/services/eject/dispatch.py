"""Helpers for the (server-dispatched) auto-eject pipeline.

The eject sweep is a SEPARATE motion-only job now — print files dispatch
unmodified and never carry an injected eject block. This module keeps the
pure, reusable piece that survived that move:

- :func:`build_part_present_eject_file` — build a standalone, motion-only
  eject-only ``.gcode.3mf`` (the file the shared remote dispatcher uploads).

The run-level cooldown override that used to live here is gone with the per-profile
threshold (2026-09-25): the eject line is measured shop air plus one margin
(``services/eject/shop_air``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from backend.app.services.eject.build_cache import EjectBuildError, get_or_build_eject_file
from backend.app.services.eject.donor import read_max_z
from backend.app.services.eject.generator import (
    EjectGenerationError,
    estimate_runtime_segments,
    generate_eject_gcode,
)
from backend.app.services.eject.validator import validate_eject_gcode
from backend.app.utils.threemf_tools import list_gcode_plate_ids

if TYPE_CHECKING:
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

    ``drop_span_s`` is the P5→P50 phase's own budget (the commanded time between the
    M73 phase beacons), and is set ONLY for a profile that actually drops the bed.

    That is now a DECISION rather than a description. Since the block became one Z flow,
    an assist-OFF block's P5→P50 span is no longer empty: it holds the 63 mm open-loop
    lift to the sweep height plus the X/Y home — real motion this lane could bound. It
    is left DISARMED for such profiles this wave anyway: no SKU runs an assist-off
    profile, so the lane would gain no coverage today, and arming it cannot make an
    existing eject safer while it can make a healthy one killable. The alternative,
    for the wave that wants it, is to arm on the SPAN rather than on the profile flag —
    the ``_EDGE_LANE_MIN_LEAD_S`` test in ``remote.py`` already refuses a span too close
    to the whole-job deadline to be worth timing, which is the same question asked
    honestly.

    ``sweep_span_s`` and ``tail_s`` are the two post-drop budgets, and unlike the drop
    they are unconditional: every generated block sweeps and every generated block ends
    in the park + completion epilogue, so both are always figures rather than an
    optional tuning's by-product.

    ``reference_s`` is the guarded Z re-reference drive's budget, and like ``drop_span_s``
    it is conditional — ``None`` for every model whose ladder has not opened
    ``z_reference_validated``, which is all of them until one does. A block without that
    phase must leave the watchdog's lane DISARMED rather than arm a zero-length deadline.

    ``start_z`` is the plate height the estimate was SEEDED with, or None when the build
    could not know where the plate was. It rides along because ``expected_runtime_s`` is
    a function of it: a seeded estimate MEASURES the block's first Z move, an unseeded
    one BOUNDS it. "Seeded" and "unseeded" are therefore two populations of one
    instrument, and a runtime series that mixes them without saying which is which
    cannot be read.

    ``plate_id`` is the member the build actually PACKED the sweep into, and it is the
    plate the dispatcher then commands in ``project_file`` — ONE value for both halves.
    They used to be two reads of ``item.plate_id`` with a plate-reader fallback between
    them, and on 2026-09-17 they disagreed: the sweep was packed into ``plate_1.gcode``
    while ``start_print`` told the firmware ``Metadata/plate_3.gcode``, which is a file
    the printer cannot parse (005-H2S, HMS ``0500_0003`` / ``0500_4003``).
    """

    path: Path
    plate_id: int
    expected_runtime_s: float
    drop_span_s: float | None
    sweep_span_s: float
    tail_s: float
    reference_s: float | None = None
    start_z: float | None = None


_NO_MAX_Z = "Could not parse max_z_height from the 3MF gcode header"


def _absent_plate_reason(source_path: Path, plate_id: int) -> str | None:
    """The "donor X has no plate N" sentence when the container genuinely lacks it, else None.

    The build fails on an absent plate in two places — the header read (no height) and
    the repack (no member to replace) — and both used to report a symptom ("could not
    parse max_z_height", "failed to repack") for what is really ONE fact: this donor is
    not the file that printed this plate. Naming the fact is what the operator needs,
    because the fix is never in the eject profile; it is the wrong donor (2026-09-17,
    005-H2S). Returns None when the plate IS present, so a genuinely headerless plate
    keeps its own diagnosis instead of being mislabelled.
    """
    plates = list_gcode_plate_ids(source_path)
    if plate_id in plates:
        return None
    return f"donor {source_path.name} has no plate {plate_id} (G-code plates: {plates or 'none'})"


async def build_part_present_eject_file(
    source_path: Path,
    plate_id: int,
    profile: EjectProfile,
    geometry: ModelGeometry,
    max_z_override: float | None = None,
    plate_z: float | None = None,
) -> BuiltEject:
    """Build a standalone PART-PRESENT, MOTION-ONLY eject-only ``.gcode.3mf`` for ``plate_id``.

    The plate's G-code is REPLACED ENTIRELY (via ``repack_3mf_with_gcode``, MD5
    recomputed) by the generated eject block: ``M17`` → ``M140 S0`` / ``M106 P2 S0`` /
    ``M106 P3 S0`` → ONE Z move to the lift height → home X/Y only → the bed-drop round
    trip when the release assist is on → the sweep, the park, then the completion
    epilogue (the canonical order statement is :func:`~backend.app.services.eject.
    generator.generate_eject_gcode`'s own diagram). The home is
    single-nozzle ``G28 X Y`` or the dual-nozzle (H2C/H2D/X2D) torque-parameterized
    ``G28 X T300`` / ``G28 Y T300`` pair (a bare ``G28 X Y`` stall-loops that firmware),
    and NEVER a bare ``G28`` / ``G28 Z`` — the part sits on the plate, so the block
    relies on the retained Z datum. There is NO in-file cooldown wait: the eject monitor
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

    ``plate_z`` is where the bed IS when the block starts, when the caller knows: the
    cooldown hold parks the plate at a height the server commanded, so a seeded estimate
    measures the block's first Z move instead of bounding it. It changes no G-code — only
    what the watchdog's deadlines are computed from — and its absence is honest rather
    than fatal (the estimate is then the longest the move could take).

    Returns a :class:`BuiltEject` — the temp ``.gcode.3mf`` path (caller cleans it
    up), the plate the sweep was PACKED into (the dispatcher commands that same value,
    never a second read of the queue row) plus the runtime the block is expected to
    take. The estimate is taken from the EJECT BLOCK text, which is exactly what
    replaces the plate G-code, i.e. exactly what the printer executes; anything else in
    the archive is inert. Raises :class:`EjectGenerationError` on any failure —
    including a donor that does not carry ``plate_id`` at all, which is named as such
    ("donor <name> has no plate N") and surfaces to the operator through the eject
    route's existing 409: the remedy is a different donor, never a different profile.
    """
    max_z = max_z_override if max_z_override is not None else read_max_z(Path(source_path), plate_id)
    if max_z is None:
        raise EjectGenerationError(_absent_plate_reason(Path(source_path), plate_id) or _NO_MAX_Z)

    block = generate_eject_gcode(profile, max_z, geometry)
    validation = validate_eject_gcode(block, profile, max_z, geometry)
    if not validation.ok:
        raise EjectGenerationError("Part-present eject validation failed: " + "; ".join(validation.errors))
    segments = estimate_runtime_segments(block, start_z=plate_z, z_travel_mm=geometry.z_travel_mm)
    # The edge lane is armed only for a profile that actually drops the bed — see
    # BuiltEject.drop_span_s for why that is now a decision rather than a description
    # (an assist-off block's P5→P50 span holds its lift and its home, and could be
    # bounded; it is deliberately not, this wave).
    drop_span_s = segments.drop_span_s if profile.bed_drop_clearance_mm is not None else None

    try:
        path = await get_or_build_eject_file(Path(source_path), plate_id, block)
    except EjectBuildError as exc:
        reason = _absent_plate_reason(Path(source_path), plate_id)
        if reason is not None:
            raise EjectGenerationError(reason) from exc
        raise EjectGenerationError(f"Failed to repack the part-present eject 3mf: {exc}") from exc
    logger.info(
        "eject.dispatch: built part-present eject from %s plate %s (max_z %.2fmm, profile %r, z_ref=%s, "
        "start_z=%s) — expected runtime %.0fs (z-reference %s, pre %.0fs, bed-drop span %s, sweep span %.0fs, "
        "tail %.0fs)",
        Path(source_path).name,
        plate_id,
        max_z,
        profile.name,
        "on" if geometry.z_reference_validated else "off",
        f"{plate_z:g}" if plate_z is not None else "unseeded",
        segments.total_s,
        f"{segments.reference_s:.0f}s" if segments.reference_s is not None else "off",
        segments.pre_s,
        f"{drop_span_s:.0f}s" if drop_span_s is not None else "off",
        segments.sweep_span_s,
        segments.tail_s,
    )
    return BuiltEject(
        path=path,
        plate_id=plate_id,
        expected_runtime_s=segments.total_s,
        drop_span_s=drop_span_s,
        sweep_span_s=segments.sweep_span_s,
        tail_s=segments.tail_s,
        reference_s=segments.reference_s,
        start_z=plate_z,
    )
