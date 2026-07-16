"""Golden byte-equality guard for the eject generator.

The goldens in ``golden/`` lock the MOTION-ONLY, server-dispatched eject recipe
(the eject sweep moved OUT of the print file into a separate job; the block is now
motion-only + a stock machine-end FINISH epilogue, no in-file ``M190 R`` cooldown
loop). RED LINE: any drift here means the generator changed the eject G-code — the
hardware ladder MUST be re-run before any unattended dispatch. NEVER regenerate a
golden just to make this pass; regenerate ONLY as part of a deliberate,
ladder-gated recipe change, and fix the generator to reproduce the locked bytes
otherwise.

* **H2S goldens** (``*_h2s_*``) lock the single-nozzle recipe (bare ``G28 X Y``).
* **H2C goldens** (``*_h2c_*``) lock the dual-nozzle recipe: the prologue homes
  with the parameterized stock forms (``M17`` → ``G28 X T300`` → ``G28 Y T300`` →
  ``G90``) instead of the bare ``G28 X Y`` that stall-loops on that firmware
  (007-H2C incident).

``capture_golden.py`` regenerates every fixture below (it imports this ``MATRIX``
and ``_profile``), so the two never drift.
"""

from __future__ import annotations

import pathlib

import pytest

from backend.app.models.eject_profile import EjectProfile
from backend.app.services.eject.generator import generate_eject_gcode
from backend.app.services.eject.geometry import ModelGeometry
from backend.tests.unit.services.eject.geometry_fixtures import H2C_GEOMETRY

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
    z_travel_mm=340.0,
)

_DEFAULTS = {
    "name": "default",
    "cooldown_temp_c": 28.0,
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
    "bed_drop_clearance_mm": None,
}


def _profile(**overrides) -> EjectProfile:
    fields = {**_DEFAULTS, **overrides}
    profile = EjectProfile()
    for key, value in fields.items():
        setattr(profile, key, value)
    return profile


# (golden_name, geometry, profile_overrides, max_z)
# Must match capture_golden.py exactly — the goldens were captured from these.
# H2S rows keep the module-local H2S_GEOMETRY; the H2C rows use the shared
# H2C_GEOMETRY fixture (ladder-validated dual-nozzle homing lock).
MATRIX = [
    ("default_h2s_z30", H2S_GEOMETRY, {}, 30.0),
    ("default_h2s_z42", H2S_GEOMETRY, {}, 42.0),
    (
        "band_h2s_z30",
        H2S_GEOMETRY,
        {"name": "band", "sweep_x_min_mm": 50.0, "sweep_x_max_mm": 200.0, "x_passes": 11},
        30.0,
    ),
    (
        "startfrac_skimoff_h2s_z50",
        H2S_GEOMETRY,
        {
            "name": "tall",
            "sweep_start_frac": 0.5,
            "final_skim": False,
            "x_passes": 3,
            "descent_steps": 2,
            "max_part_height_mm": 60.0,
        },
        50.0,
    ),
    # H2C dual-nozzle golden: mirrors its H2S namesake's parameters; the prologue
    # locks the ladder-validated parameterized homing recipe.
    ("default_h2c_z30", H2C_GEOMETRY, {}, 30.0),
    # Bed-drop release assist (farm eject v2): default 50 mm clearance. H2S drops to
    # z_travel 340 - 50 = 290 then returns to lift 40; H2C drops to 325 - 50 = 275.
    # These 2 goldens ARE the deliberate, ladder-gated recipe addition.
    ("drop_h2s_z30", H2S_GEOMETRY, {"name": "drop", "bed_drop_clearance_mm": 50.0}, 30.0),
    ("drop_h2c_z30", H2C_GEOMETRY, {"name": "drop", "bed_drop_clearance_mm": 50.0}, 30.0),
]


@pytest.mark.parametrize("name,geometry,overrides,max_z", MATRIX, ids=[m[0] for m in MATRIX])
def test_eject_gcode_is_byte_identical(name, geometry, overrides, max_z):
    golden = (GOLDEN_DIR / f"{name}.gcode").read_bytes()
    produced = generate_eject_gcode(_profile(**overrides), max_z, geometry).encode("utf-8")
    assert produced == golden, (
        f"Eject G-code DRIFTED for {name!r} ({geometry.model_key}). Generator output changed — "
        "re-run the hardware ladder; regenerate the goldens only as a deliberate ladder-gated recipe change."
    )


def test_all_golden_fixtures_are_covered():
    """Every golden file has a matrix case (no orphan/stale fixtures)."""
    covered = {f"{m[0]}.gcode" for m in MATRIX}
    on_disk = {p.name for p in GOLDEN_DIR.glob("*.gcode")}
    assert on_disk == covered, f"golden dir {on_disk} != matrix {covered}"
