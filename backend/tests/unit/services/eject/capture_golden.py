"""Regenerate every golden ``.gcode`` fixture in ``golden/`` from the CURRENT generator.

Deterministic: iterates the exact ``MATRIX`` (and ``_profile`` defaults) the golden
test parametrizes, so a fixture written here is byte-for-byte what
``test_golden_geometry.py`` asserts against.

USE WITH CARE — the goldens are a regression gate, not a convenience. They lock the
motion-only, server-dispatched eject recipe; regenerate them ONLY as part of a
deliberate, supervised hardware-ladder-gated recipe change. If a golden fails
unexpectedly, fix the generator, do NOT rewrite the fixture (test docstring red line).

Run from the repo root (as a module, so ``backend`` imports resolve)::

    .venv\\Scripts\\python.exe -m backend.tests.unit.services.eject.capture_golden
"""

from __future__ import annotations

from backend.app.services.eject.generator import generate_eject_gcode
from backend.tests.unit.services.eject.test_golden_geometry import GOLDEN_DIR, MATRIX, _profile


def capture_all() -> None:
    GOLDEN_DIR.mkdir(exist_ok=True)
    for name, geometry, overrides, max_z in MATRIX:
        gcode = generate_eject_gcode(_profile(**overrides), max_z, geometry)
        path = GOLDEN_DIR / f"{name}.gcode"
        path.write_bytes(gcode.encode("utf-8"))
        print(f"wrote {path} ({len(gcode.splitlines())} lines)")


if __name__ == "__main__":
    capture_all()
