"""Negative tests for the eject G-code validator — one guard per test."""

import pytest

from backend.app.models.eject_profile import EjectProfile
from backend.app.services.eject.generator import DUAL_NOZZLE_HOME, generate_eject_gcode
from backend.app.services.eject.validator import validate_eject_gcode
from backend.tests.unit.services.eject.geometry_fixtures import H2C_GEOMETRY, H2S_GEOMETRY


def _profile(**overrides) -> EjectProfile:
    defaults = {
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
        "max_part_height_mm": 42.0,
        "sweep_x_min_mm": None,
        "sweep_x_max_mm": None,
        "sweep_start_frac": 1.0,
    }
    defaults.update(overrides)
    profile = EjectProfile()
    for key, value in defaults.items():
        setattr(profile, key, value)
    return profile


def _valid(profile=None, max_z=30.0):
    profile = profile or _profile()
    return generate_eject_gcode(profile, max_z, H2S_GEOMETRY), profile


class TestValidatorGuards:
    def test_generator_output_is_valid(self):
        gcode, profile = _valid()
        result = validate_eject_gcode(gcode, profile, 30.0, H2S_GEOMETRY)
        assert result.ok and result.errors == []

    def test_part_height_over_ceiling(self):
        gcode, profile = _valid()
        # Re-validate the same block against a taller declared part height.
        result = validate_eject_gcode(gcode, profile, 50.0, H2S_GEOMETRY)
        assert not result.ok
        assert any("max_part_height_mm" in e for e in result.errors)

    def test_move_below_z_offset(self):
        gcode, profile = _valid()
        bad = gcode + "G1 Z0.1 F600\n"
        result = validate_eject_gcode(bad, profile, 30.0, H2S_GEOMETRY)
        assert not result.ok
        assert any("z_offset floor" in e for e in result.errors)

    def test_x_outside_envelope(self):
        gcode, profile = _valid()
        bad = gcode + "G1 X999 F9000\n"
        result = validate_eject_gcode(bad, profile, 30.0, H2S_GEOMETRY)
        assert not result.ok
        assert any("envelope" in e and "X" in e for e in result.errors)

    def test_y_outside_envelope(self):
        gcode, profile = _valid()
        bad = gcode + "G1 Y-50 F3000\n"
        result = validate_eject_gcode(bad, profile, 30.0, H2S_GEOMETRY)
        assert not result.ok
        assert any("envelope" in e and "Y" in e for e in result.errors)

    def test_full_span_sweep_coordinates_accepted(self):
        # The operator-witnessed working sweep geometry (outer lane X=3, back
        # lane Y=322) is inside the permissive envelope and must validate.
        gcode, profile = _valid()
        full_span = gcode + "G1 X3 Y322 F9000\n"
        result = validate_eject_gcode(full_span, profile, 30.0, H2S_GEOMETRY)
        assert result.ok, result.errors

    def test_freshly_generated_block_accepted(self):
        # The block the generator produces validates cleanly.
        gcode, profile = _valid()
        result = validate_eject_gcode(gcode, profile, 30.0, H2S_GEOMETRY)
        assert result.ok and result.errors == []

    def test_wrong_m190_count(self):
        gcode, profile = _valid()
        bad = gcode + "M190 R28\n"  # one extra retry
        result = validate_eject_gcode(bad, profile, 30.0, H2S_GEOMETRY)
        assert not result.ok
        assert any("M190" in e for e in result.errors)

    def test_m190_wrong_threshold(self):
        # Build with 5 retries at 28, then hand-edit one to a different temp.
        gcode, profile = _valid()
        bad = gcode.replace("M190 R28", "M190 R40", 1)
        result = validate_eject_gcode(bad, profile, 30.0, H2S_GEOMETRY)
        assert not result.ok
        assert any("threshold" in e for e in result.errors)

    def test_bare_g28_forbidden(self):
        gcode, profile = _valid()
        bad = gcode + "G28\n"
        result = validate_eject_gcode(bad, profile, 30.0, H2S_GEOMETRY)
        assert not result.ok
        assert any("all axes" in e for e in result.errors)

    def test_g28_z_forbidden(self):
        gcode, profile = _valid()
        bad = gcode + "G28 Z\n"
        result = validate_eject_gcode(bad, profile, 30.0, H2S_GEOMETRY)
        assert not result.ok
        assert any("bed centre" in e for e in result.errors)

    def test_missing_prologue(self):
        # A block with no prologue commands at all.
        stub = (
            "; ===== FARM EJECT BLOCK profile=default =====\n"
            "M140 S0\n" + "M190 R28\n" * 5 + "; ===== FARM EJECT BLOCK END =====\n"
        )
        profile = _profile()
        result = validate_eject_gcode(stub, profile, 30.0, H2S_GEOMETRY)
        assert not result.ok
        assert any("M17" in e for e in result.errors)
        assert any("G28 X Y" in e for e in result.errors)
        assert any("G90" in e for e in result.errors)

    def test_validator_uses_passed_geometry_not_hardcoded(self):
        # A full-width H2S block reaches X=3 / X=337 — inside the H2S envelope but
        # OUTSIDE the tighter H2C envelope (x 25..325). Validating the SAME block
        # against H2C geometry must flag the out-of-envelope moves, proving the
        # validator keys on the geometry it is handed (not a hardcoded model). The
        # "unknown model" rejection moved to the geometry accessor (test_geometry).
        gcode, profile = _valid()
        result = validate_eject_gcode(gcode, profile, 30.0, H2C_GEOMETRY)
        assert not result.ok
        assert any("envelope" in e and "X" in e for e in result.errors)


class TestSweepBandAndFracGuards:
    def test_valid_band_and_frac_pass(self):
        profile = _profile(sweep_x_min_mm=50.0, sweep_x_max_mm=200.0, sweep_start_frac=0.5)
        gcode = generate_eject_gcode(profile, 30.0, H2S_GEOMETRY)
        result = validate_eject_gcode(gcode, profile, 30.0, H2S_GEOMETRY)
        assert result.ok, result.errors

    def test_one_sided_band(self):
        # Validate a valid full-width block against a one-sided profile.
        gcode, _ = _valid()
        profile = _profile(sweep_x_min_mm=50.0, sweep_x_max_mm=None)
        result = validate_eject_gcode(gcode, profile, 30.0, H2S_GEOMETRY)
        assert not result.ok
        assert any("both sweep_x_min_mm and sweep_x_max_mm" in e for e in result.errors)

    def test_band_width_too_narrow(self):
        gcode, _ = _valid()
        profile = _profile(sweep_x_min_mm=50.0, sweep_x_max_mm=55.0)
        result = validate_eject_gcode(gcode, profile, 30.0, H2S_GEOMETRY)
        assert not result.ok
        assert any("below the 10" in e for e in result.errors)

    def test_band_exceeds_bed(self):
        gcode, _ = _valid()
        profile = _profile(sweep_x_min_mm=50.0, sweep_x_max_mm=400.0)
        result = validate_eject_gcode(gcode, profile, 30.0, H2S_GEOMETRY)
        assert not result.ok
        assert any("exceeds bed width" in e for e in result.errors)

    def test_inverted_band(self):
        gcode, _ = _valid()
        profile = _profile(sweep_x_min_mm=200.0, sweep_x_max_mm=50.0)
        result = validate_eject_gcode(gcode, profile, 30.0, H2S_GEOMETRY)
        assert not result.ok
        assert any("need 0 <= min < max" in e for e in result.errors)

    def test_frac_zero_rejected(self):
        gcode, _ = _valid()
        profile = _profile(sweep_start_frac=0.0)
        result = validate_eject_gcode(gcode, profile, 30.0, H2S_GEOMETRY)
        assert not result.ok
        assert any("sweep_start_frac" in e for e in result.errors)

    def test_frac_above_one_rejected(self):
        gcode, _ = _valid()
        profile = _profile(sweep_start_frac=1.5)
        result = validate_eject_gcode(gcode, profile, 30.0, H2S_GEOMETRY)
        assert not result.ok
        assert any("sweep_start_frac" in e for e in result.errors)


class TestCooldownRequirement:
    """The M190 retry-count guard is skipped ONLY via require_cooldown=False."""

    def test_thermal_less_block_passes_when_cooldown_not_required(self):
        # A dry-run block (no M190 waits) validates when the caller opts out of
        # the retry-count guard — but every geometry guard still ran.
        profile = _profile()
        gcode = generate_eject_gcode(profile, 30.0, H2S_GEOMETRY, include_cooldown=False)
        result = validate_eject_gcode(gcode, profile, 30.0, H2S_GEOMETRY, require_cooldown=False)
        assert result.ok, result.errors

    def test_thermal_less_block_rejected_as_production(self):
        # The SAME thermal-less block fails under production validation
        # (default require_cooldown=True): a production eject snippet missing its
        # M190 release waits must never pass.
        profile = _profile()
        gcode = generate_eject_gcode(profile, 30.0, H2S_GEOMETRY, include_cooldown=False)
        result = validate_eject_gcode(gcode, profile, 30.0, H2S_GEOMETRY)
        assert not result.ok
        assert any("M190" in e for e in result.errors)

    def test_production_block_with_zero_retries_still_fails(self):
        # Strip the M190 waits out of a production block by hand — it must fail
        # even though require_cooldown defaults True (no global loosening).
        gcode, profile = _valid()
        stripped = "\n".join(ln for ln in gcode.splitlines() if not ln.startswith("M190")) + "\n"
        result = validate_eject_gcode(stripped, profile, 30.0, H2S_GEOMETRY)
        assert not result.ok
        assert any("expected cooldown_retries" in e for e in result.errors)

    def test_dry_run_opt_out_does_not_disable_geometry_guards(self):
        # require_cooldown=False must NOT loosen envelope/z-floor/G28 checks.
        gcode, profile = _valid()
        thermal_less = "\n".join(ln for ln in gcode.splitlines() if not ln.startswith("M190"))
        bad = thermal_less + "\nG1 X999 F9000\n"
        result = validate_eject_gcode(bad, profile, 30.0, H2S_GEOMETRY, require_cooldown=False)
        assert not result.ok
        assert any("envelope" in e and "X" in e for e in result.errors)


def _valid_dual(profile=None, max_z=30.0):
    """A freshly generated dual-nozzle (H2C) block + its profile."""
    profile = profile or _profile()
    return generate_eject_gcode(profile, max_z, H2C_GEOMETRY), profile


class TestDualNozzleHomeRequirement:
    """Dual-nozzle models must home with BOTH parameterized stock forms
    (DUAL_NOZZLE_HOME) — 007-H2C stall-loop incident, 2026-07-12. The bare
    `G28 X Y` requirement applies only to non-dual models."""

    def test_dual_generated_block_validates(self):
        gcode, profile = _valid_dual()
        result = validate_eject_gcode(gcode, profile, 30.0, H2C_GEOMETRY)
        assert result.ok, result.errors

    @pytest.mark.parametrize("missing", DUAL_NOZZLE_HOME)
    def test_dual_block_missing_a_home_line_rejected(self, missing):
        gcode, profile = _valid_dual()
        stripped = "\n".join(ln for ln in gcode.splitlines() if ln.strip() != missing) + "\n"
        result = validate_eject_gcode(stripped, profile, 30.0, H2C_GEOMETRY)
        assert not result.ok
        assert any(f"missing dual-nozzle home '{missing}'" in e for e in result.errors), result.errors

    def test_dual_block_missing_both_home_lines_names_both(self):
        gcode, profile = _valid_dual()
        stripped = "\n".join(ln for ln in gcode.splitlines() if not ln.strip().startswith("G28")) + "\n"
        result = validate_eject_gcode(stripped, profile, 30.0, H2C_GEOMETRY)
        assert not result.ok
        for home in DUAL_NOZZLE_HOME:
            assert any(f"missing dual-nozzle home '{home}'" in e for e in result.errors), result.errors

    def test_dual_block_with_g28_x_y_instead_rejected(self):
        # Swapping the parameterized homes for the single-nozzle `G28 X Y` (the
        # exact line that stall-loops on this firmware) must fail.
        gcode, profile = _valid_dual()
        lines = gcode.splitlines()
        lines = [ln for ln in lines if ln.strip() not in DUAL_NOZZLE_HOME]
        m17 = lines.index("M17")
        lines.insert(m17 + 1, "G28 X Y")
        result = validate_eject_gcode("\n".join(lines) + "\n", profile, 30.0, H2C_GEOMETRY)
        assert not result.ok
        assert any("missing dual-nozzle home" in e for e in result.errors)

    def test_g28_x_t300_not_misparsed_as_bare_g28(self):
        # `G28 X T300` homes exactly one axis (X); the T300 is a stall-torque
        # PARAMETER. It must trip neither the bare-G28 nor the G28-Z rejection —
        # on ANY geometry (the parse is model-independent).
        gcode, profile = _valid_dual()
        for geometry in (H2C_GEOMETRY, H2S_GEOMETRY):
            result = validate_eject_gcode(gcode, profile, 30.0, geometry)
            assert not any("all axes" in e for e in result.errors), result.errors
            assert not any("bed centre" in e for e in result.errors), result.errors

    def test_h2s_block_not_required_to_carry_dual_homes(self):
        # Non-dual geometry: the existing `G28 X Y` requirement, no dual demand.
        gcode, profile = _valid()
        result = validate_eject_gcode(gcode, profile, 30.0, H2S_GEOMETRY)
        assert result.ok, result.errors
        assert not any("dual-nozzle home" in e for e in result.errors)


class TestStandaloneToolChangeGuard:
    """A standalone tool-change (first token `T<digits>`) drives the AMS/Vortek
    tool state machine and is forbidden in eject blocks on ALL models."""

    @pytest.mark.parametrize("tool_line", ["T0", "T65535"])
    @pytest.mark.parametrize(
        "geometry", [H2S_GEOMETRY, H2C_GEOMETRY], ids=[g.model_key for g in (H2S_GEOMETRY, H2C_GEOMETRY)]
    )
    def test_standalone_tool_change_rejected(self, tool_line, geometry):
        profile = _profile()
        gcode = generate_eject_gcode(profile, 30.0, geometry)
        bad = gcode + tool_line + "\n"
        result = validate_eject_gcode(bad, profile, 30.0, geometry)
        assert not result.ok
        assert any("tool-change commands are forbidden" in e for e in result.errors), result.errors

    def test_t_parameter_inside_g28_does_not_trip_guard(self):
        # The dual block's own `G28 X T300` lines: T300 is a parameter, not a
        # first-token tool select — the guard must stay silent.
        gcode, profile = _valid_dual()
        result = validate_eject_gcode(gcode, profile, 30.0, H2C_GEOMETRY)
        assert result.ok, result.errors
        assert not any("tool-change" in e for e in result.errors)
