"""Shared ModelGeometry fixtures for the eject unit tests.

The generator/validator/dispatch used to key on a model string against two
in-code dicts; they now take a resolved :class:`ModelGeometry`. These constants
encode the SAME H2S/H2C geometry the registry seeds, so the eject suites exercise
the exact production geometry without a DB.
"""

from backend.app.services.eject.geometry import ModelGeometry

# H2S — validated production geometry (matches the run_migrations seed + the
# values the deleted PRINTER_BED_DIMS / PRINTER_TRAVEL_ENVELOPE dicts encoded).
H2S_GEOMETRY = ModelGeometry(
    model_key="H2S",
    bed=(340.0, 320.0),
    envelope=(0.0, 340.0, -16.0, 325.0),
    max_part_height_mm=42.0,
    validated=True,
)

# H2C — provisional (unvalidated) geometry. Envelope = the LEFT-extruder frame
# measured live on 007-H2C (2026-07-12): the post-print no-tool state maps as the
# left frame, printable X 0-325, Y 0-320 (matches Bambu's extruder_printable_area).
# Replaces the earlier conservative 25-325 per-extruder intersection guess.
H2C_GEOMETRY = ModelGeometry(
    model_key="H2C",
    bed=(330.0, 320.0),
    envelope=(0.0, 325.0, 0.0, 320.0),
    max_part_height_mm=42.0,
    validated=False,
)
