"""Shared ModelGeometry fixtures for the eject unit tests.

The generator/validator/dispatch used to key on a model string against two
in-code dicts; they now take a resolved :class:`ModelGeometry`. These constants
encode the SAME H2S/H2C geometry the registry seeds, so the eject suites exercise
the exact production geometry without a DB.
"""

from dataclasses import replace

from backend.app.services.eject.geometry import ModelGeometry

# H2S — validated production geometry (matches the run_migrations seed + the
# values the deleted PRINTER_BED_DIMS / PRINTER_TRAVEL_ENVELOPE dicts encoded).
H2S_GEOMETRY = ModelGeometry(
    model_key="H2S",
    bed=(340.0, 320.0),
    envelope=(0.0, 340.0, -16.0, 325.0),
    max_part_height_mm=42.0,
    validated=True,
    z_travel_mm=340.0,
    # The SECOND ladder gate is CLOSED here, matching the registry seed for every model:
    # the production fixtures must keep producing today's block byte-for-byte. The
    # flag-on variants live in their own fixtures below.
    z_reference_validated=False,
    hold_lift_mm=12.0,
)

# The same H2S geometry with the Z re-reference gate OPEN — what a model looks like once
# its own hardware ladder (rungs 1-5) has been operator-witnessed and the flag flipped
# through PUT /model-geometry. Kept as a separate constant so no test can accidentally
# enable a motion recipe that no machine has run.
H2S_GEOMETRY_Z_REFERENCED = replace(H2S_GEOMETRY, z_reference_validated=True)

# H2C — provisional (unvalidated) geometry. Envelope measured live on 007-H2C
# (hardware ladder 2026-07-12): X-min step-probed 25→20→15 clean at mid-bed; the
# no-tool carriage's reachable left limit sits INSIDE the left-extruder printable
# 0-325 (a commanded X3 sweep lane contacted the left wall — incident 2), so the
# left bound is the PROBED 15, operator-ruled. X-max 325 / Y 0-320 walked clean
# at both edges.
H2C_GEOMETRY = ModelGeometry(
    model_key="H2C",
    bed=(330.0, 320.0),
    envelope=(15.0, 325.0, 0.0, 320.0),
    max_part_height_mm=42.0,
    validated=False,
    z_travel_mm=325.0,
    z_reference_validated=False,
    hold_lift_mm=12.0,
)

# Dual-nozzle sibling of ``H2S_GEOMETRY_Z_REFERENCED``: the re-reference prologue is
# model-level, so it must compose with the torque-parameterized homing dialect rather
# than replacing it.
H2C_GEOMETRY_Z_REFERENCED = replace(H2C_GEOMETRY, z_reference_validated=True)
