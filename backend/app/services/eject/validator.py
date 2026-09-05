"""Eject G-code validator.

Re-checks a generated (or hand-edited) eject block against the profile's safety
guards. The generator's output must always pass this; the scheduler refuses to
dispatch any eject job whose block fails validation, so a bad block fails the
queue item instead of driving the toolhead into the plate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from backend.app.services.eject.generator import (
    PARK_Z_MM,
    SWEEP_BAND_MIN_WIDTH_MM,
    block_start_marker,
)
from backend.app.utils.printer_models import DUAL_NOZZLE_HOME, is_bedslinger_model, is_dual_nozzle_model

if TYPE_CHECKING:
    from backend.app.models.eject_profile import EjectProfile
    from backend.app.services.eject.geometry import ModelGeometry

_EPS = 1e-6
# One word/param token, e.g. "X170", "Z0.4", "R28", "F9000".
_TOKEN_RE = re.compile(r"^([A-Za-z])(-?\d+(?:\.\d+)?)$")
# A standalone tool-change command whose FIRST token selects a tool (T0, T65535).
# `T300` inside `G28 X T300` is a PARAMETER (never the first token) — not matched.
_TOOLCHANGE_RE = re.compile(r"^T\d+$")
# G28 axis letters only — X/Y/Z. Any other trailing address word (e.g. the
# stall-torque `T300`, the `P0` on the Z-home form) is a PARAMETER, not an axis,
# and must not be mistaken for one when deciding "bare G28" / "G28 Z".
_G28_AXES = ("X", "Y", "Z")


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _tokens(line: str) -> list[str]:
    """Strip the inline/full-line comment and split a G-code line into tokens."""
    code = line.split(";", 1)[0].strip()
    return code.split()


def _params(tokens: list[str]) -> dict[str, float]:
    """Parse trailing address-word params (X/Y/Z/F/R/...) into a dict."""
    out: dict[str, float] = {}
    for tok in tokens[1:]:
        m = _TOKEN_RE.match(tok)
        if m:
            out[m.group(1).upper()] = float(m.group(2))
    return out


def validate_eject_gcode(
    gcode: str,
    profile: EjectProfile,
    max_z_height: float,
    geometry: ModelGeometry,
) -> ValidationResult:
    """Validate a MOTION-ONLY eject block against `profile`'s guards for `geometry`.

    Returns a :class:`ValidationResult`; ``ok`` is True only when there are no
    errors. Checks: part not taller than the guard; no move below the z_offset
    floor; no move above the eject Z ceiling (the lift height, or the bed-drop
    target when the release assist is on — floored at ``PARK_Z_MM``); the block's
    LAST Z-bearing move ends the bed at least the park Z (``max(lift_z,
    PARK_Z_MM)``) clear of the nozzle, so a surviving part is not dragged into;
    all X/Y inside the model's machine travel envelope (``geometry.envelope``);
    prologue present with no bare ``G28`` and no ``G28 Z`` (dual-nozzle models
    require both parameterized home forms); no standalone tool-change. ``geometry`` is the
    resolved :class:`~backend.app.services.eject.geometry.ModelGeometry` — the
    caller resolves it from the registry, so the validator does no model lookup.

    The Z RE-REFERENCE prologue (2026-09-04) is checked as MODAL RULES over the emitted
    text, never as a comparison against an expected string, so a hand-edited or
    reordered block is caught by the same machinery as a generator regression:
    ``G91`` must be closed by ``G90`` and ``M211 Z0`` matched by ``M211 Z1`` before the
    first ``G0``/``G1``; ``G380`` may run only in relative mode and only ``+Z`` (the
    direction that moves the bed AWAY from the part); at most one ``G92``, Z-only,
    declaring exactly ``z_travel_mm``; and all three commands are errors anywhere after
    the first move. All three are newly admissible — none appeared in a generated block
    before this recipe — so outside the prologue they remain forbidden outright.

    There is NO thermal check: the eject block is motion-only (the cooldown wait
    moved into the eject monitor), so a stray ``M190`` is neither required nor
    rejected — only the geometry/homing/tool-state guards apply.
    """
    errors: list[str] = []
    warnings: list[str] = []

    bed_x, bed_y = geometry.bed
    env_x_min, env_x_max, env_y_min, env_y_max = geometry.envelope

    # Guard 1: part-height ceiling.
    if max_z_height > profile.max_part_height_mm:
        errors.append(f"Part height {max_z_height} mm exceeds max_part_height_mm {profile.max_part_height_mm} mm")

    # Guard 1b: X sweep sub-band consistency (mirrors the generator's resolution).
    band_lo = getattr(profile, "sweep_x_min_mm", None)
    band_hi = getattr(profile, "sweep_x_max_mm", None)
    if (band_lo is None) != (band_hi is None):
        errors.append("Sweep band requires both sweep_x_min_mm and sweep_x_max_mm (or neither)")
    elif band_lo is not None:
        if not (0 <= band_lo < band_hi):
            errors.append(f"Sweep band [{band_lo:g}, {band_hi:g}] mm invalid: need 0 <= min < max")
        elif band_hi - band_lo < SWEEP_BAND_MIN_WIDTH_MM:
            errors.append(
                f"Sweep band width {band_hi - band_lo:g} mm is below the {SWEEP_BAND_MIN_WIDTH_MM:g} mm minimum"
            )
        if band_hi > bed_x + _EPS:
            errors.append(f"sweep_x_max_mm {band_hi:g} mm exceeds bed width {bed_x:g} mm")

    # Guard 1c: sweep start fraction must be in (0, 1].
    start_frac = getattr(profile, "sweep_start_frac", 1.0)
    if start_frac is not None and not (0 < start_frac <= 1 + _EPS):
        errors.append(f"sweep_start_frac {start_frac:g} must be in (0, 1]")

    # Machine travel envelope — the hard XY limit every move must respect,
    # independent of the profile. A move outside it (hand-edited or legacy block)
    # would trip the firmware soft limits and fault the toolhead.
    x_lo = env_x_min - _EPS
    x_hi = env_x_max + _EPS
    y_lo = env_y_min - _EPS
    y_hi = env_y_max + _EPS
    z_floor = profile.z_offset_mm - _EPS

    # Guard 1d: upper-Z ceiling. Bounded by the profile's OWN expectation, not the
    # machine ceiling — a hand-edited ``G1 Z300`` in a 42 mm-part non-drop block must
    # fail even though the machine could reach it. Base = the lift height (clearance
    # above the part), floored at PARK_Z_MM so a legal ``clearance_mm=0`` + tiny part
    # still admits the park Z10. When the bed-drop release assist is on, the ceiling
    # opens up to the (deeper) drop target; a missing z_travel_mm or a drop that is
    # not below the lift is itself an error (mirrors the generator's fail-closed).
    lift_z = max_z_height + profile.clearance_mm
    z_ceiling = max(lift_z, PARK_Z_MM)
    bed_drop = getattr(profile, "bed_drop_clearance_mm", None)
    if bed_drop is not None:
        if is_bedslinger_model(geometry.model_key):
            # Mirrors the generator's fail-closed: a bed-slinger's bed is fixed in Z
            # (the gantry carries Z), so a bed-drop release assist is meaningless.
            errors.append(
                f"bed-drop release assist is enabled but {geometry.model_key!r} is a bedslinger "
                "(bed does not move in Z) — disable bed_drop_clearance_mm in this profile or pick a bed-on-Z model"
            )
        if geometry.z_travel_mm is None:
            errors.append(f"bed-drop release assist is enabled but model {geometry.model_key!r} has no z_travel_mm")
        else:
            drop_z = geometry.z_travel_mm - bed_drop
            if drop_z <= lift_z:
                errors.append(f"bed-drop target Z{drop_z:g} is not below the lift height Z{lift_z:g} — degenerate drop")
            else:
                z_ceiling = max(drop_z, z_ceiling)
    z_hi = z_ceiling + _EPS

    # Guard 1e: the Z re-reference gate's own preconditions, mirroring the generator's
    # fail-closed refusals so a block cannot reach a printer through a hand-edited path
    # the generator would have refused.
    if geometry.z_reference_validated:
        if is_bedslinger_model(geometry.model_key):
            errors.append(
                f"Z re-reference is enabled but {geometry.model_key!r} is a bedslinger (the gantry carries Z, "
                "not the bed) — a gantry-Z reference is a different recipe and needs its own hardware ladder"
            )
        if geometry.z_travel_mm is None:
            errors.append(f"Z re-reference is enabled but model {geometry.model_key!r} has no z_travel_mm to declare")

    # Bed-slinger kinematics warning (independent of the bed-drop assist): on these
    # machines the bed moves in Y, so a sweep pushes the part by translating the BED,
    # not the toolhead — the sweep speeds are unproven until the hardware ladder.
    if is_bedslinger_model(geometry.model_key):
        warnings.append(
            "bedslinger kinematics: Y moves drive the bed — sweep speeds unproven until the hardware ladder"
        )

    dual = is_dual_nozzle_model(geometry.model_key)
    # Token-tuple -> original line for each required dual-nozzle home command, so
    # presence is a token-level match (whitespace/case independent), mirroring the
    # G28-form parsing below. A dual block homes X and Y in two SEPARATE
    # parameterized commands, so both must be present.
    required_home = {tuple(tok.upper() for tok in line.split()): line for line in DUAL_NOZZLE_HOME}
    found_home: set[tuple[str, ...]] = set()

    has_m17 = False
    has_g90 = False
    has_g28_xy = False
    # Z RE-REFERENCE modal state (2026-09-04). Checked INDEPENDENTLY of the generator —
    # these are modal-dialect rules about the emitted text, not a comparison against an
    # expected string, so a hand-edited block that reorders the prologue is caught by the
    # same machinery that catches a generator regression.
    relative_mode = False  # G91 open?
    soft_limits_off = False  # M211 Z0 seen with no matching M211 Z1 yet?
    first_move_seen = False
    g92_count = 0
    # The Z of the most recent Z-bearing G0/G1 move — the block's END STATE. The
    # generator now parks the bed proportional to part height so a surviving part
    # clears the nozzle; the guard after the loop rejects any block whose last Z
    # move ends bed-high (e.g. a legacy/hand-edited fixed ``Z10`` park).
    last_z: float | None = None

    for raw in gcode.splitlines():
        tokens = _tokens(raw)
        if not tokens:
            continue
        cmd = tokens[0].upper()

        # Guard (ALL models): a standalone tool-change command — first token
        # T<digits> (e.g. T0, T65535) — drives the AMS / Vortek tool state
        # machine; an eject block must stay tool-state-neutral. `T300` inside
        # `G28 X T300` is a parameter (not the first token), so it never trips.
        if _TOOLCHANGE_RE.match(cmd):
            errors.append("tool-change commands are forbidden in eject blocks")

        if cmd == "M17":
            has_m17 = True
        elif cmd == "G90":
            has_g90 = True
            relative_mode = False
        elif cmd == "G91":
            # Relative mode is admissible ONLY inside the pre-move prologue, and only
            # because the guarded Z drive is expressed as a displacement. Once any
            # G0/G1 has run, every later coordinate check in this validator (the Z
            # floor, the Z ceiling, the XY envelope) reads its parameters as ABSOLUTE
            # positions — so an open G91 past that point would make every one of those
            # guards silently meaningless rather than merely wrong.
            relative_mode = True
            if first_move_seen:
                errors.append("G91 (relative positioning) after the first move — the eject block is absolute")
        elif cmd == "G92":
            g92_count += 1
            params = _params(tokens)
            axes = {t[0].upper() for t in tokens[1:] if t and t[0].upper() in _G28_AXES}
            if first_move_seen:
                errors.append("G92 after the first move — the Z datum may only be declared in the prologue")
            if g92_count > 1:
                errors.append("more than one G92: the Z datum is declared exactly once")
            if axes - {"Z"}:
                errors.append(f"G92 may declare Z only, not {sorted(axes - {'Z'})} — X/Y come from the homing")
            if "Z" not in params:
                errors.append("G92 with no Z declares nothing")
            elif geometry.z_travel_mm is None:
                errors.append(
                    f"G92 declares a Z datum but model {geometry.model_key!r} has no z_travel_mm to declare it as"
                )
            elif abs(params["Z"] - geometry.z_travel_mm) > _EPS:
                # The declaration is safe only because the physical stop sits AT or
                # BEYOND z_travel_mm (every H2S reaches a commanded Z340 today), which
                # makes the frame error <= 0 — MORE clearance. Any other value throws
                # that argument away and declares a frame nothing supports.
                errors.append(
                    f"G92 Z{params['Z']:g} does not declare the machine bottom "
                    f"z_travel_mm {geometry.z_travel_mm:g} for {geometry.model_key}"
                )
        elif cmd == "M211":
            params = _params(tokens)
            if first_move_seen:
                errors.append("M211 after the first move — soft end stops are only toggled in the prologue")
            if params.get("Z") == 0:
                soft_limits_off = True
            elif params.get("Z") == 1:
                soft_limits_off = False
        elif cmd == "G380":
            params = _params(tokens)
            if first_move_seen:
                errors.append("G380 after the first move — the guarded drive belongs in the prologue")
            if not relative_mode:
                # An absolute G380 would drive to a coordinate in the very frame the
                # move exists to replace.
                errors.append("G380 outside relative mode (G91) — the guarded drive is a displacement")
            if "Z" not in params:
                errors.append("G380 with no Z: the only guarded drive this block may make is on Z")
            elif params["Z"] <= 0:
                # +Z moves the bed AWAY from the nozzle. A negative drive moves it
                # TOWARD the part, which is the one direction that can touch it.
                errors.append(
                    f"G380 Z{params['Z']:g} drives the bed TOWARD the nozzle — the re-reference drive is +Z only"
                )
        elif cmd == "G28":
            # Only X/Y/Z are axes; trailing T/P address words are PARAMETERS (the
            # dual-nozzle torque forms `G28 X T300` / `G28 Y T300` each home one
            # axis). Never home Z (probes the bed centre under the part) and never
            # bare G28 (homes all axes, including Z).
            axes = {t[0].upper() for t in tokens[1:] if t and t[0].upper() in _G28_AXES}
            norm = tuple(t.upper() for t in tokens)
            if norm in required_home:
                found_home.add(norm)
            if not axes:
                errors.append("G28 with no axis letters homes all axes (incl. Z) — forbidden with a part on the plate")
            elif "Z" in axes:
                errors.append("G28 Z probes the bed centre under the part — forbidden")
            elif {"X", "Y"} <= axes:
                has_g28_xy = True
        elif cmd in ("G0", "G1"):
            params = _params(tokens)
            if not first_move_seen:
                first_move_seen = True
                # The prologue's two modal escapes must BOTH be closed before any
                # ordinary move runs: an open G91 makes every coordinate guard below
                # read a displacement as a position, and soft end stops left off remove
                # the firmware's own last backstop for the rest of the block.
                if relative_mode:
                    errors.append("relative mode (G91) is still open at the first move — close it with G90")
                if soft_limits_off:
                    errors.append("soft end stops left off (M211 Z0 with no M211 Z1) at the first move")
            if "Z" in params:
                last_z = params["Z"]
            if "Z" in params and params["Z"] < z_floor:
                errors.append(f"Move Z{params['Z']:g} is below the z_offset floor {profile.z_offset_mm:g}")
            if "Z" in params and params["Z"] > z_hi:
                errors.append(f"Move Z{params['Z']:g} exceeds the eject Z ceiling {z_ceiling:g} mm")
            if "X" in params and not (x_lo <= params["X"] <= x_hi):
                errors.append(
                    f"Move X{params['X']:g} is outside the {geometry.model_key} travel envelope [{env_x_min:g}, {env_x_max:g}]"
                )
            if "Y" in params and not (y_lo <= params["Y"] <= y_hi):
                errors.append(
                    f"Move Y{params['Y']:g} is outside the {geometry.model_key} travel envelope [{env_y_min:g}, {env_y_max:g}]"
                )

    # Guard 5: prologue integrity.
    if block_start_marker(profile) not in gcode:
        errors.append("Missing FARM EJECT BLOCK start marker")
    if not has_m17:
        errors.append("Prologue missing 'M17' (motors not re-engaged)")
    if not has_g90:
        errors.append("Prologue missing 'G90' (absolute positioning)")
    if dual:
        # Dual-nozzle firmware stall-loops on a bare `G28 X Y`; the block homes X
        # and Y with the two parameterized forms instead — require both present.
        for norm, line in required_home.items():
            if norm not in found_home:
                errors.append(f"Prologue missing dual-nozzle home {line!r}")
    elif not has_g28_xy:
        errors.append("Prologue missing 'G28 X Y' home")

    # Guard 6: end-state clearance. Independent of the ceiling check: the block's
    # LAST Z-bearing move must leave the bed at least the park Z (part top +
    # clearance, floored at PARK_Z_MM) below the nozzle, so a part that survived
    # the sweep clears the toolhead. A block ending bed-high (e.g. a legacy fixed
    # ``Z10`` park under a taller part) is rejected here even though every
    # individual move sits under the ceiling.
    if last_z is not None and last_z < max(lift_z, PARK_Z_MM) - _EPS:
        errors.append(
            f"block ends with the bed at Z{last_z:g} while the part top is {max_z_height:g} mm "
            "— end state must clear the part"
        )

    return ValidationResult(ok=not errors, errors=errors, warnings=warnings)
