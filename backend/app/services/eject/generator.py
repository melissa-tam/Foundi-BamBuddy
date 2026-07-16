"""Eject G-code generator.

Produces the machine-end EJECT BLOCK for a given :class:`EjectProfile`, part
height and printer model. The block runs *after* the printer's stock shutdown
(bed dropped ~Z123, motors M18-disabled), so it re-engages the motors and homes
only X/Y before cooling the bed and sweeping the part off the front (door side).

Every coordinate is derived from the profile plus the model's bed dimensions,
then clamped into the model's proven-safe machine travel envelope so no
generated move can trip the firmware soft limits — nothing is hardcoded. The bed
rectangle and the envelope both arrive as a :class:`ModelGeometry` resolved from
the ``printer_model_geometry`` registry (``services.eject.geometry``), so adding
a printer model is a DB row, not a code change.

Two optional tunings narrow the sweep: an X sub-band (``sweep_x_min_mm`` /
``sweep_x_max_mm``) confines the lanes to part of the bed width instead of the
full width, and ``sweep_start_frac`` starts the descending sweep at a fraction
of the part height instead of at the part top. The prologue clearance move
still clears the full part top regardless of either tuning. A third tuning,
``final_skim`` (default True), gates the trailing slow skim pass at the
z_offset floor — set it False to push exactly once (e.g. one mid-height lane
for a tall part).

An optional ``bed_drop_clearance_mm`` (NULL = off) adds a mechanical release
assist after the bed heater is commanded off: the bed drives all the way DOWN
to the machine bottom minus that clearance (bigger Z = bed farther from the
nozzle), then returns to the lift height before the sweep runs — jolting a
stuck part loose without changing the sweep itself. The machine bottom is the
target model's ``z_travel_mm`` (from the geometry registry, never hardcoded);
a profile that enables the assist against a model with no ``z_travel_mm`` fails
closed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from backend.app.utils.printer_models import DUAL_NOZZLE_HOME, is_bedslinger_model, is_dual_nozzle_model

if TYPE_CHECKING:
    from backend.app.models.eject_profile import EjectProfile
    from backend.app.services.eject.geometry import ModelGeometry

# Minimum width (mm) of an explicit X sweep sub-band. Narrower than this the
# toolhead cannot reliably clear a part across the band, so a tighter band is a
# safety error (schema-validated and re-checked here + in the validator).
SWEEP_BAND_MIN_WIDTH_MM = 10.0

# Safe Z (mm) the toolhead parks at after the sweep, above the plate — a module
# constant (not an inline literal) so the same value is the validator's park-Z
# floor for the upper-Z ceiling guard (``_fmt(10.0)`` == "10", byte-identical).
PARK_Z_MM = 10.0

# The dual-nozzle homing forms (``DUAL_NOZZLE_HOME`` / ``DUAL_NOZZLE_FULL_HOME``)
# live in ``utils.printer_models`` — the single canonical source of truth shared
# with the dry-run wrapper and ``BambuMQTTClient.home_axes``. The generator's
# eject prologue homes X/Y only (never Z — a part sits on the plate), so it uses
# ``DUAL_NOZZLE_HOME`` (the X/Y torque pair) directly.

# Marker comments wrapping the generated block so it is unambiguously locatable
# in an injected file (and greppable in dry-run downloads).
BLOCK_START_PREFIX = "; ===== FARM EJECT BLOCK profile="
BLOCK_END_MARKER = "; ===== FARM EJECT BLOCK END ====="

# Completion epilogue — the stock machine-end FINISH TAIL, copied verbatim from a
# production H2S plate (foundi-FarmManager/Print Files/
# _6_Half_Shell_PCO-M18-2656_top_surface_gcode.3mf → Metadata/plate_3.gcode), the
# segment from the feedrate/acc/time resets through the final `M73 P100 R0`.
#
# The eject sweep is now a STANDALONE, server-dispatched motion-only job whose file
# REPLACES the plate G-code entirely — it no longer splices after a real print's
# stock machine-end block. A standalone file WITHOUT that block ends FAILED at EOF
# even after clean motion (cosmetic, live-observed on a real H2S 2026-07-04). This
# tail is the firmware's job-completion handshake — progress/feedrate/accel resets,
# the air-filtration `M1002 judge_flag` conditional (J1/J2 fire only when the
# firmware set the flag; otherwise skipped), the finish chime, then `M400`/`M18` —
# so appending it makes the eject job register FINISH instead of FAILED-at-EOF.
# Verbatim: no commands the stock file lacks are invented; only insignificant
# trailing whitespace on the melody lines is normalised.
COMPLETION_EPILOGUE = """\
M220 S100  ; Reset feedrate magnitude
M201.2 K1.0 ; Reset acc magnitude
M73.2   R1.0 ;Reset left time magnitude

M1015.4 S0 K0 ;disable air printing detect

;=====printer finish air purification=========
M622.1 S0
M1002 judge_flag print_finish_air_filt_flag

M622 J1
M1002 gcode_claim_action : 66
M145 P1
M106 P6 S255
M400 S180
M106 P6 S0
M623

M622 J2
M1002 gcode_claim_action : 66
M145 P0
M106 P3 S127
M400 S180
M106 P3 S0
M623
;=====printer finish air purification=========

;=====printer finish  sound=========
M17
M400 S1
M1006 S1
M1006 A53 B10 L99 C53 D10 M99 E53 F10 N99
M1006 A57 B10 L99 C57 D10 M99 E57 F10 N99
M1006 A0 B15 L0 C0 D15 M0 E0 F15 N0
M1006 A53 B10 L99 C53 D10 M99 E53 F10 N99
M1006 A57 B10 L99 C57 D10 M99 E57 F10 N99
M1006 A0 B15 L0 C0 D15 M0 E0 F15 N0
M1006 A48 B10 L99 C48 D10 M99 E48 F10 N99
M1006 A0 B15 L0 C0 D15 M0 E0 F15 N0
M1006 A60 B10 L99 C60 D10 M99 E60 F10 N99
M1006 W
;=====printer finish  sound=========
M400
M18

M73 P100 R0"""


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
    geometry: ModelGeometry,
) -> str:
    """Build the MOTION-ONLY eject G-code block for `profile` at part height `max_z_height`.

    The block is a self-contained, self-completing eject-only job: prologue
    (re-engage + home X/Y), bed-heater off, the descending sweep + park, then the
    :data:`COMPLETION_EPILOGUE` (stock machine-end finish tail) so the standalone
    file ends FINISH rather than FAILED-at-EOF.

    There is NO in-file cooldown wait: the bed-cooldown gate moved OUT of the
    G-code into the eject monitor, which holds the plate-clear gate until the live
    ``bed_temper`` reaches the profile's ``cooldown_temp_c`` and only THEN dispatches
    this motion-only job. ``M140 S0`` (heater off) is still emitted defensively; the
    old ``M106``/``M190 R`` thermal block is gone.

    Args:
        profile: the eject profile (all tunable parameters).
        max_z_height: parsed part top Z from the 3MF gcode header (mm).
        geometry: the target model's :class:`~backend.app.services.eject.geometry.ModelGeometry`
            (bed rectangle + travel envelope), resolved from the registry by the
            caller. Pure input — the generator does no DB / model-string lookup.

    Returns:
        The complete eject block as a newline-terminated string.

    Raises:
        EjectGenerationError: part taller than the profile's ``max_part_height_mm``
            guard; a degenerate sweep after the travel-envelope clamp; or the
            bed-drop release assist is enabled but the model has no
            ``z_travel_mm`` in its geometry row, or the drop target is not below
            the lift height (degenerate drop).
    """
    bed_x, bed_y = geometry.bed
    x_min, x_max, y_min, y_max = geometry.envelope

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
        raise EjectGenerationError("sweep_x_min_mm and sweep_x_max_mm must both be set or both be null")
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
                f"sweep_x_max_mm {band_hi} mm exceeds bed width {bed_x} mm for {geometry.model_key}"
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
    if is_dual_nozzle_model(geometry.model_key):
        # Dual-nozzle firmware stall-loops on unparameterized homing (see
        # DUAL_NOZZLE_HOME) — home X then Y with the stock parameterized forms.
        lines.extend(DUAL_NOZZLE_HOME)
    else:
        lines.append("G28 X Y")
    lines.append("G90")
    # Lift the bed clear of the part; reused as the return height of the optional
    # bed-drop assist and as the validator's expected Z ceiling for a non-drop block.
    lift_z = max_z_height + profile.clearance_mm
    lines.append(f"G1 Z{_fmt(lift_z)} F900")

    # --- bed heater off ---------------------------------------------------
    # Command the bed heater off defensively. The cooldown WAIT is no longer in
    # the G-code — the eject monitor already held the plate gate until the live
    # bed reached cooldown_temp_c before dispatching this motion-only job — so no
    # fan / M190 R loop is emitted here.
    lines.append("; --- bed heater off ---")
    lines.append("M140 S0")

    # --- bed-drop release assist (optional) -------------------------------
    # Drive the bed all the way DOWN to the machine bottom minus the profile's
    # clearance (bigger Z = bed farther from the nozzle), then return to the lift
    # height — a mechanical jolt to release a part the sweep alone can't shift.
    # NULL clearance = assist off (the 5 golden fixtures stay byte-identical).
    bed_drop = profile.bed_drop_clearance_mm
    if bed_drop is not None:
        if is_bedslinger_model(geometry.model_key):
            # A bed-slinger's bed is fixed in Z (the gantry carries Z), so there is
            # no bed-on-Z travel to open a part↔nozzle gap — the drop is physically
            # meaningless and driving Z would move the TOOLHEAD toward the part.
            raise EjectGenerationError(
                f"bed-drop release assist is enabled but {geometry.model_key!r} is a bedslinger "
                "(bed does not move in Z) — disable bed_drop_clearance_mm in this profile or pick a bed-on-Z model"
            )
        if geometry.z_travel_mm is None:
            raise EjectGenerationError(
                f"bed-drop release assist is enabled but model {geometry.model_key!r} has no "
                "z_travel_mm — set it via PUT /model-geometry before ejecting with this profile"
            )
        drop_z = geometry.z_travel_mm - bed_drop
        if drop_z <= lift_z:
            raise EjectGenerationError(
                f"bed-drop target Z{drop_z:g} (z_travel {geometry.z_travel_mm:g} - clearance "
                f"{bed_drop:g}) is not below the lift height Z{lift_z:g} — degenerate drop"
            )
        lines.append("; --- bed-drop release assist: full down + return ---")
        lines.append(f"G1 Z{_fmt(drop_z)} F900")
        lines.append(f"G1 Z{_fmt(lift_z)} F900")

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
    lines.append(f"G1 X{_fmt(park_x)} Y{_fmt(park_y)} Z{_fmt(PARK_Z_MM)} F9000")

    # --- completion epilogue ----------------------------------------------
    # Stock machine-end finish tail so this standalone motion-only file ends
    # FINISH, not FAILED-at-EOF (see COMPLETION_EPILOGUE). Emitted verbatim.
    lines.append("; --- completion epilogue: stock machine-end finish tail (job ends FINISH) ---")
    lines.append(COMPLETION_EPILOGUE)

    lines.append(BLOCK_END_MARKER)
    return "\n".join(lines) + "\n"
