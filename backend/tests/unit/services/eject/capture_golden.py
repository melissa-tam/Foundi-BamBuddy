"""Regenerate every golden ``.gcode`` fixture in ``golden/`` from the CURRENT generator.

Deterministic: iterates the exact ``MATRIX`` (and ``_profile`` defaults) the golden
test parametrizes, so a fixture written here is byte-for-byte what
``test_golden_geometry.py`` asserts against.

USE WITH CARE — the goldens are a regression gate, not a convenience:

* **H2S fixtures** lock the PRE-refactor generator bytes. Regenerating them only
  "passes" if the generator still reproduces those bytes exactly; if an H2S golden
  fails, fix the generator, NEVER rewrite the fixture (test docstring red line).
  Running this script must leave the six H2S files unchanged (verify with git).
* **H2C fixtures** lock the ladder-validated dual-nozzle recipe (2026-07-12). They
  may only be regenerated after a NEW supervised hardware ladder validates a
  changed recipe.

Run from the repo root (as a module, so ``backend`` imports resolve)::

    .venv\\Scripts\\python.exe -m backend.tests.unit.services.eject.capture_golden
"""

from __future__ import annotations

from backend.app.services.eject.generator import generate_eject_gcode
from backend.tests.unit.services.eject.test_golden_geometry import GOLDEN_DIR, MATRIX, _profile


def capture_all() -> None:
    GOLDEN_DIR.mkdir(exist_ok=True)
    for name, geometry, overrides, max_z, override, include_cooldown in MATRIX:
        gcode = generate_eject_gcode(
            _profile(**overrides),
            max_z,
            geometry,
            cooldown_temp_c=override,
            include_cooldown=include_cooldown,
        )
        path = GOLDEN_DIR / f"{name}.gcode"
        path.write_bytes(gcode.encode("utf-8"))
        print(f"wrote {path} ({len(gcode.splitlines())} lines)")


if __name__ == "__main__":
    capture_all()
