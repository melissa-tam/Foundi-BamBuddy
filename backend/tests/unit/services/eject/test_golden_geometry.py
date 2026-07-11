"""Golden byte-equality guard for the ModelGeometry refactor (Phase 2).

The eject generator moved from two in-code dicts (``PRINTER_BED_DIMS`` /
``PRINTER_TRAVEL_ENVELOPE``) to a ``ModelGeometry`` resolved from the registry.
The golden ``.gcode`` fixtures in ``golden/`` were captured from the PRE-refactor
generator (H2S). This test regenerates the same matrix through the NEW geometry
path and asserts BYTE-IDENTICAL output.

RED LINE: any drift here means the refactor changed H2S eject G-code — the
hardware ladder must be re-run before any dispatch. NEVER regenerate the goldens
to make this pass; fix the generator so it reproduces them exactly.
"""

from __future__ import annotations

import pathlib

import pytest

from backend.app.models.eject_profile import EjectProfile
from backend.app.services.eject.generator import generate_eject_gcode
from backend.app.services.eject.geometry import ModelGeometry

GOLDEN_DIR = pathlib.Path(__file__).parent / "golden"

# The H2S geometry EXACTLY as the deleted dicts encoded it: bed (340, 320),
# permissive travel envelope (0, 340, -16, 325). max_part_height_mm mirrors the
# registry seed; validated True (production).
H2S_GEOMETRY = ModelGeometry(
    model_key="H2S",
    bed=(340.0, 320.0),
    envelope=(0.0, 340.0, -16.0, 325.0),
    max_part_height_mm=42.0,
    validated=True,
)

_DEFAULTS = {
    "name": "default",
    "cooldown_temp_c": 28.0,
    "cooldown_retries": 5,
    "clearance_mm": 10.0,
    "z_offset_mm": 0.4,
    "descent_steps": 4,
    "x_passes": 11,
    "x_margin_mm": 3.0,
    "front_overhang_mm": 2.0,
    "back_overhang_mm": 2.0,
    "eject_speed_mm_min": 3000,
    "skim_speed_mm_min": 1500,
    "cooling_fan_assist": True,
    "final_skim": True,
    "max_part_height_mm": 42.0,
    "sweep_x_min_mm": None,
    "sweep_x_max_mm": None,
    "sweep_start_frac": 1.0,
}


def _profile(**overrides) -> EjectProfile:
    fields = {**_DEFAULTS, **overrides}
    profile = EjectProfile()
    for key, value in fields.items():
        setattr(profile, key, value)
    return profile


# (golden_name, profile_overrides, max_z, cooldown_override, include_cooldown)
# Must match capture_golden.py exactly — the goldens were captured from these.
MATRIX = [
    ("default_h2s_z30", {}, 30.0, None, True),
    ("default_h2s_z42", {}, 42.0, None, True),
    (
        "band_h2s_z30",
        {"name": "band", "sweep_x_min_mm": 50.0, "sweep_x_max_mm": 200.0, "x_passes": 11},
        30.0,
        None,
        True,
    ),
    ("override_h2s_z25", {}, 25.0, 33.0, True),
    ("dryrun_h2s_z30", {}, 30.0, None, False),
    (
        "startfrac_skimoff_h2s_z50",
        {
            "name": "tall",
            "sweep_start_frac": 0.5,
            "final_skim": False,
            "x_passes": 3,
            "descent_steps": 2,
            "max_part_height_mm": 60.0,
        },
        50.0,
        None,
        True,
    ),
]


@pytest.mark.parametrize("name,overrides,max_z,override,include_cooldown", MATRIX, ids=[m[0] for m in MATRIX])
def test_h2s_geometry_path_is_byte_identical(name, overrides, max_z, override, include_cooldown):
    golden = (GOLDEN_DIR / f"{name}.gcode").read_bytes()
    produced = generate_eject_gcode(
        _profile(**overrides),
        max_z,
        H2S_GEOMETRY,
        cooldown_temp_c=override,
        include_cooldown=include_cooldown,
    ).encode("utf-8")
    assert produced == golden, (
        f"H2S eject G-code DRIFTED for {name!r} through the ModelGeometry path. "
        "The geometry refactor changed output — re-run the hardware ladder; do NOT regenerate goldens."
    )


def test_all_golden_fixtures_are_covered():
    """Every golden file has a matrix case (no orphan/stale fixtures)."""
    covered = {f"{m[0]}.gcode" for m in MATRIX}
    on_disk = {p.name for p in GOLDEN_DIR.glob("*.gcode")}
    assert on_disk == covered, f"golden dir {on_disk} != matrix {covered}"
