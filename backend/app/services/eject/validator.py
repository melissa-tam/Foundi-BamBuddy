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
    DUAL_NOZZLE_HOME,
    SWEEP_BAND_MIN_WIDTH_MM,
    block_start_marker,
)
from backend.app.utils.printer_models import is_dual_nozzle_model

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
    cooldown_temp_c: float | None = None,
    *,
    require_cooldown: bool = True,
) -> ValidationResult:
    """Validate `gcode` against `profile`'s guards for `geometry`.

    Returns a :class:`ValidationResult`; ``ok`` is True only when there are no
    errors. Checks: part not taller than the guard; no move below the z_offset
    floor; all X/Y inside the model's machine travel envelope (``geometry.envelope``);
    exactly ``cooldown_retries`` ``M190 R`` waits at the effective threshold;
    prologue present with no bare ``G28`` and no ``G28 Z``. ``geometry`` is the
    resolved :class:`~backend.app.services.eject.geometry.ModelGeometry` — the
    caller resolves it from the registry, so the validator does no model lookup.

    ``cooldown_temp_c`` is the effective release temperature — pass the same
    per-run override handed to :func:`generate_eject_gcode` so the ``M190 R``
    threshold check validates against the value actually emitted, not the
    profile default. When None, the profile's ``cooldown_temp_c`` is used.

    ``require_cooldown`` gates ONLY the ``M190 R`` retry-count check. Keep it
    True for every PRODUCTION block (a snippet missing its release waits must
    still fail). Pass False to validate a thermal-less dry-run block generated
    with ``include_cooldown=False`` — that body deliberately omits the ``M190 R``
    waits (they cannot complete on an empty ambient bed), so the count check is
    skipped while EVERY geometry guard above (envelope, z-floor, forbidden bare
    ``G28`` / ``G28 Z``, prologue integrity) is still enforced. This is an
    explicit per-call opt-out, never a global loosening.
    """
    errors: list[str] = []
    warnings: list[str] = []
    effective_cooldown = cooldown_temp_c if cooldown_temp_c is not None else profile.cooldown_temp_c

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

    dual = is_dual_nozzle_model(geometry.model_key)
    # Token-tuple -> original line for each required dual-nozzle home command, so
    # presence is a token-level match (whitespace/case independent), mirroring the
    # G28-form parsing below. A dual block homes X and Y in two SEPARATE
    # parameterized commands, so both must be present.
    required_home = {tuple(tok.upper() for tok in line.split()): line for line in DUAL_NOZZLE_HOME}
    found_home: set[tuple[str, ...]] = set()

    m190_count = 0
    has_m17 = False
    has_g90 = False
    has_g28_xy = False

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
        elif cmd == "M190":
            params = _params(tokens)
            if "R" in params:
                m190_count += 1
                if abs(params["R"] - effective_cooldown) > _EPS:
                    errors.append(
                        f"M190 R{params['R']:g} threshold != effective cooldown_temp_c {effective_cooldown:g}"
                    )
        elif cmd in ("G0", "G1"):
            params = _params(tokens)
            if "Z" in params and params["Z"] < z_floor:
                errors.append(f"Move Z{params['Z']:g} is below the z_offset floor {profile.z_offset_mm:g}")
            if "X" in params and not (x_lo <= params["X"] <= x_hi):
                errors.append(
                    f"Move X{params['X']:g} is outside the {geometry.model_key} travel envelope [{env_x_min:g}, {env_x_max:g}]"
                )
            if "Y" in params and not (y_lo <= params["Y"] <= y_hi):
                errors.append(
                    f"Move Y{params['Y']:g} is outside the {geometry.model_key} travel envelope [{env_y_min:g}, {env_y_max:g}]"
                )

    # Guard 3: cooldown retry count must match the profile exactly — enforced
    # for production blocks only. A thermal-less dry-run block (validated with
    # require_cooldown=False) legitimately omits the M190 R waits; the geometry
    # guards above still apply. The M190 R *threshold* check inside the loop
    # always runs, so any stray wait at the wrong temperature is still rejected.
    if require_cooldown and m190_count != profile.cooldown_retries:
        errors.append(f"Found {m190_count} 'M190 R' waits, expected cooldown_retries={profile.cooldown_retries}")

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

    return ValidationResult(ok=not errors, errors=errors, warnings=warnings)
