"""Unit tests for the per-run cooldown override on the eject pipeline (Phase 2)."""

from backend.app.models.eject_profile import EjectProfile
from backend.app.services.eject.generator import generate_eject_gcode
from backend.app.services.eject.validator import validate_eject_gcode


def _profile(**overrides) -> EjectProfile:
    defaults = {
        "name": "override",
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
        "max_part_height_mm": 60.0,
    }
    defaults.update(overrides)
    profile = EjectProfile()
    for key, value in defaults.items():
        setattr(profile, key, value)
    return profile


class TestCooldownOverride:
    def test_none_uses_profile_value(self):
        gcode = generate_eject_gcode(_profile(), 30.0, "H2S", cooldown_temp_c=None)
        assert gcode.count("M190 R28") == 5

    def test_override_changes_emitted_threshold(self):
        gcode = generate_eject_gcode(_profile(), 30.0, "H2S", cooldown_temp_c=22.0)
        assert gcode.count("M190 R22") == 5
        assert "M190 R28" not in gcode

    def test_generate_and_validate_share_override(self):
        # The generated block validates only when the validator is told the same
        # effective temp — proving generation + validation stay consistent.
        gcode = generate_eject_gcode(_profile(), 30.0, "H2S", cooldown_temp_c=22.0)
        ok = validate_eject_gcode(gcode, _profile(), 30.0, "H2S", cooldown_temp_c=22.0)
        assert ok.ok, ok.errors

    def test_validator_flags_mismatched_threshold(self):
        # Block emitted at 22 but validated against the profile default (28) fails.
        gcode = generate_eject_gcode(_profile(), 30.0, "H2S", cooldown_temp_c=22.0)
        result = validate_eject_gcode(gcode, _profile(), 30.0, "H2S", cooldown_temp_c=None)
        assert not result.ok
        assert any("threshold" in e for e in result.errors)
