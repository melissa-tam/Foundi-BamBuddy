"""Eject G-code generator.

Produces the machine-end EJECT BLOCK for a given :class:`EjectProfile`, part
height and printer model. The block runs *after* the printer's stock shutdown
(bed dropped ~Z123, motors M18-disabled), so it re-engages the motors and homes
only X/Y before cooling the bed and sweeping the part off the front (door side).

Every coordinate is derived from the profile plus the model's bed dimensions,
then clamped into the model's proven-safe machine travel envelope
(``PRINTER_TRAVEL_ENVELOPE``) so no generated move can trip the firmware soft
limits — nothing is hardcoded. Adding a printer model is a one-line entry in
``PRINTER_BED_DIMS`` and ``PRINTER_TRAVEL_ENVELOPE``.

Two optional tunings narrow the sweep: an X sub-band (``sweep_x_min_mm`` /
``sweep_x_max_mm``) confines the lanes to part of the bed width instead of the
full width, and ``sweep_start_frac`` starts the descending sweep at a fraction
of the part height instead of at the part top. The prologue clearance move
still clears the full part top regardless of either tuning. A third tuning,
``final_skim`` (default True), gates the trailing slow skim pass at the
z_offset floor — set it False to push exactly once (e.g. one mid-height lane
for a tall part).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.app.models.eject_profile import EjectProfile

# Bed dimensions (X, Y) in mm, keyed by printer model string. Fail-fast on any
# model not listed — geometry must be verified per machine before ejecting.
PRINTER_BED_DIMS: dict[str, tuple[float, float]] = {
    "H2S": (340.0, 320.0),
}

# Machine XY travel envelope (x_min, x_max, y_min, y_max) in mm, keyed by printer
# model. This is the box the toolhead may reach WITHOUT tripping the firmware
# soft limits — a MACHINE property, distinct from the (larger) bed rectangle and
# from any profile overhang/margin. Every generated XY move is clamped into it,
# and the validator independently rejects any move outside it.
#
# PERMISSIVE bounds: the full sweep span (X 3..337 from a 3 mm margin, Y -2..322
# from 2 mm overhangs) was operator-witnessed executing a complete plate sweep on
# a real H2S (2026-07-04, dry-run v1: ~7 min of lanes, no motion fault), so the
# envelope must not narrow it. One dispatch (queue 10) faulted at the same
# coordinates (HMS 0x30001, motion controller) — a single unexplained occurrence,
# not reproducible evidence of soft limits. These bounds therefore act only as a
# gross-configuration guard; replace them with MEASURED limits after the live
# soft-limit probe session.
PRINTER_TRAVEL_ENVELOPE: dict[str, tuple[float, float, float, float]] = {
    "H2S": (0.0, 340.0, -16.0, 325.0),
}

# Minimum width (mm) of an explicit X sweep sub-band. Narrower than this the
# toolhead cannot reliably clear a part across the band, so a tighter band is a
# safety error (schema-validated and re-checked here + in the validator).
SWEEP_BAND_MIN_WIDTH_MM = 10.0

# Marker comments wrapping the generated block so it is unambiguously locatable
# in an injected file (and greppable in dry-run downloads).
BLOCK_START_PREFIX = "; ===== FARM EJECT BLOCK profile="
BLOCK_END_MARKER = "; ===== FARM EJECT BLOCK END ====="


class EjectGenerationError(ValueError):
    """Raised when an eject block cannot be safely generated for the inputs."""


def _fmt(value: float) -> str:
    """Format a coordinate/temperature for G-code: trim trailing zeros, no exponent."""
    return f"{value:g}"


def _clamp(value: float, lo: float, hi: float) -> float:
    """Constrain `value` to the closed interval [lo, hi]."""
    return max(lo, min(value, hi))


def _linspace(start: float, end: float, count: int) -> list[float]:
    """`count` values evenly spaced from `start` to `end` (both inclusive)."""
    if count <= 1:
        return [start]
    step = (end - start) / (count - 1)
    return [start + step * i for i in range(count)]


def block_start_marker(profile: EjectProfile) -> str:
    """The exact block-start marker comment for `profile` (also used by validator)."""
    return f"{BLOCK_START_PREFIX}{profile.name} ====="


def generate_eject_gcode(
    profile: EjectProfile,
    max_z_height: float,
    printer_model: str,
    cooldown_temp_c: float | None = None,
    *,
    include_cooldown: bool = True,
) -> str:
    """Build the eject G-code block for `profile` at a part height of `max_z_height`.

    Args:
        profile: the eject profile (all tunable parameters).
        max_z_height: parsed part top Z from the 3MF gcode header (mm).
        printer_model: printer model string; must be a key of ``PRINTER_BED_DIMS``.
        cooldown_temp_c: optional per-run override for the bed cooldown gate
            (°C). When None, the profile's ``cooldown_temp_c`` is used. Lets a
            production run tighten/loosen the release temperature without a
            dedicated profile — the ``M190 R`` waits are emitted at this value.
        include_cooldown: emit the thermal gate (fan + ``M190 R`` release waits).
            True for every PRODUCTION path — the bed always approaches the
            threshold from ABOVE (cooling from a finished print), so the wait
            completes. Pass False ONLY for the empty-bed dry run: there the bed
            sits at ambient with the heater off, so an ``M190 R`` toward the
            release threshold can never be reached from below (``M190 R`` waits
            from EITHER direction) — it would hang the job forever and the sweep
            the dry run exists to validate would never run. The ``M140 S0``
            heater-off is emitted either way; only the fan + ``M190 R`` waits are
            dropped, so the dry-run body validates GEOMETRY, not thermals.

    Returns:
        The complete eject block as a newline-terminated string.

    Raises:
        EjectGenerationError: unknown printer model, or part taller than the
            profile's ``max_part_height_mm`` guard.
    """
    effective_cooldown = cooldown_temp_c if cooldown_temp_c is not None else profile.cooldown_temp_c
    dims = PRINTER_BED_DIMS.get(printer_model)
    if dims is None:
        raise EjectGenerationError(
            f"No eject bed geometry for printer model {printer_model!r}; "
            f"add it to PRINTER_BED_DIMS (known: {sorted(PRINTER_BED_DIMS)})"
        )
    bed_x, bed_y = dims

    env = PRINTER_TRAVEL_ENVELOPE.get(printer_model)
    if env is None:
        raise EjectGenerationError(
            f"No travel envelope for printer model {printer_model!r}; "
            f"add it to PRINTER_TRAVEL_ENVELOPE (known: {sorted(PRINTER_TRAVEL_ENVELOPE)})"
        )
    x_min, x_max, y_min, y_max = env

    if max_z_height > profile.max_part_height_mm:
        raise EjectGenerationError(
            f"Part height {max_z_height} mm exceeds profile max_part_height_mm "
            f"{profile.max_part_height_mm} mm — refusing to generate eject block"
        )

    # Lane Y endpoints: profile intent is front = -front_overhang, back =
    # bed_y + back_overhang, but the machine cannot travel past its soft limits,
    # so both are clamped into the travel envelope. Clamping is silent (intent is
    # preserved as closely as the machine allows); only a collapse is fatal.
    front_y = _clamp(-profile.front_overhang_mm, y_min, y_max)
    back_y = _clamp(bed_y + profile.back_overhang_mm, y_min, y_max)
    if front_y >= back_y:
        raise EjectGenerationError(
            f"Eject sweep degenerate after travel-envelope clamp: front Y {front_y:g} "
            f">= back Y {back_y:g} (envelope Y [{y_min:g}, {y_max:g}])"
        )

    # X lanes: an explicit sub-band [min, max] when BOTH are set, else the full
    # margin-inset bed width (unchanged default). Exactly one bound set, an
    # inverted/too-narrow band, or a band past the bed edge is a safety error.
    band_lo = profile.sweep_x_min_mm
    band_hi = profile.sweep_x_max_mm
    if (band_lo is None) != (band_hi is None):
        raise EjectGenerationError(
            "sweep_x_min_mm and sweep_x_max_mm must both be set or both be null"
        )
    if band_lo is not None:
        if not (0 <= band_lo < band_hi):
            raise EjectGenerationError(
                f"Invalid sweep band [{band_lo}, {band_hi}] mm: need 0 <= sweep_x_min_mm < sweep_x_max_mm"
            )
        if band_hi - band_lo < SWEEP_BAND_MIN_WIDTH_MM:
            raise EjectGenerationError(
                f"Sweep band width {band_hi - band_lo} mm is below the {SWEEP_BAND_MIN_WIDTH_MM} mm minimum"
            )
        if band_hi > bed_x:
            raise EjectGenerationError(
                f"sweep_x_max_mm {band_hi} mm exceeds bed width {bed_x} mm for {printer_model}"
            )
        lane_lo, lane_hi = band_lo, band_hi
    else:
        lane_lo, lane_hi = profile.x_margin_mm, bed_x - profile.x_margin_mm
    # Clamp the lane span into the travel envelope (silently — see the Y note
    # above). Fail-fast only if the clamp collapses the span to zero width.
    lane_lo = _clamp(lane_lo, x_min, x_max)
    lane_hi = _clamp(lane_hi, x_min, x_max)
    if lane_lo >= lane_hi:
        raise EjectGenerationError(
            f"Eject sweep degenerate after travel-envelope clamp: X span "
            f"[{lane_lo:g}, {lane_hi:g}] collapsed (envelope X [{x_min:g}, {x_max:g}])"
        )
    x_lanes = _linspace(lane_lo, lane_hi, profile.x_passes)

    # Top sweep level: begin the descent at a fraction of the part height, never
    # below the z_offset floor. sweep_start_frac defaults to 1.0 (part top); a
    # transient profile with the attribute unset is treated as 1.0.
    start_frac = profile.sweep_start_frac if profile.sweep_start_frac is not None else 1.0
    sweep_top = max(max_z_height * start_frac, profile.z_offset_mm)
    z_levels = _linspace(sweep_top, profile.z_offset_mm, profile.descent_steps)

    lines: list[str] = [block_start_marker(profile)]

    # --- prologue: re-engage after stock shutdown -------------------------
    # NEVER G28 (all axes) or G28 Z: Z-homing probes the bed centre where the
    # part still sits. Home X/Y only, then lift the bed clear of the part.
    lines.append("; --- prologue: re-engage motors, home X/Y (never Z) ---")
    lines.append("M17")
    lines.append("G28 X Y")
    lines.append("G90")
    lines.append(f"G1 Z{_fmt(max_z_height + profile.clearance_mm)} F900")

    # --- bed heater off ---------------------------------------------------
    # Always command the bed heater off. In the full (production) mode this is
    # the front of a thermal gate that HOLDS until the bed reaches the release
    # threshold; in the thermal-less dry-run mode it is the whole thermal
    # handling — no fan, no M190 wait (see the include_cooldown arg docstring).
    lines.append("; --- bed heater off ---")
    lines.append("M140 S0")
    if include_cooldown:
        lines.append("; --- cooldown: hold until the bed reaches the release threshold ---")
        if profile.cooling_fan_assist:
            lines.append("M106 S255")
        # One M190 R stalls early on the cooling slope, so re-arm it N times.
        for _ in range(profile.cooldown_retries):
            lines.append(f"M190 R{_fmt(effective_cooldown)}")
        lines.append("M106 S0")

    # --- sweep: push the part off the FRONT (door side) -------------------
    lines.append("; --- sweep: push part off the front edge ---")
    # Park behind the part (rear service area) at the first lane.
    lines.append(f"G1 X{_fmt(x_lanes[0])} Y{_fmt(back_y)} F9000")

    def sweep_level(z: float, feed: int) -> None:
        lines.append(f"G1 Z{_fmt(z)} F600")
        # Reset X to the first lane (moving along the rear, clear of the part).
        lines.append(f"G1 X{_fmt(x_lanes[0])} F9000")
        for i, _x in enumerate(x_lanes):
            lines.append(f"G1 Y{_fmt(front_y)} F{feed}")  # push through, off the front
            lines.append(f"G1 Y{_fmt(back_y)} F9000")  # return to the rear
            if i < len(x_lanes) - 1:
                lines.append(f"G1 X{_fmt(x_lanes[i + 1])} F9000")  # advance to next lane

    for z in z_levels:
        sweep_level(z, profile.eject_speed_mm_min)
    # Final slow skim right above the plate to clear thin remnants. Gated by the
    # profile's final_skim toggle: True (default, prior behaviour) appends the
    # skim; False pushes exactly once. A transient profile with the attribute
    # unset is treated as True (mirrors the sweep_start_frac None handling).
    final_skim = profile.final_skim if profile.final_skim is not None else True
    if final_skim:
        lines.append("; --- final skim ---")
        sweep_level(profile.z_offset_mm, profile.skim_speed_mm_min)

    # --- park centre at a safe Z ------------------------------------------
    # Bed centre, clamped into the travel envelope (the centre is well inside it
    # for every real bed, but clamp for the same single-source guarantee).
    park_x = _clamp(bed_x / 2, x_min, x_max)
    park_y = _clamp(bed_y / 2, y_min, y_max)
    lines.append(f"G1 X{_fmt(park_x)} Y{_fmt(park_y)} Z10 F9000")

    lines.append(BLOCK_END_MARKER)
    return "\n".join(lines) + "\n"
