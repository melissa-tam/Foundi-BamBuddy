"""Golden-structure tests for the eject G-code generator."""

import pytest

from backend.app.models.eject_profile import EjectProfile
from backend.app.services.eject.generator import (
    DUAL_NOZZLE_HOME,
    EjectGenerationError,
    generate_eject_gcode,
)
from backend.app.services.eject.validator import validate_eject_gcode
from backend.tests.unit.services.eject.geometry_fixtures import H2C_GEOMETRY, H2S_GEOMETRY


def _profile(**overrides) -> EjectProfile:
    """Build an in-memory EjectProfile with the documented defaults."""
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
        "final_skim": True,
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


def _sweep_x_values(gcode: str) -> list[float]:
    """X targets of the pure-X lane moves (exclude Y-parks and the Z park move)."""
    xs: list[float] = []
    for line in gcode.splitlines():
        code = line.split(";", 1)[0].strip()
        if not code.startswith("G1 "):
            continue
        params = {tok[0]: tok[1:] for tok in code.split()[1:] if tok and tok[0].isalpha()}
        if "X" in params and "Y" not in params and "Z" not in params:
            xs.append(float(params["X"]))
    return xs


def _sweep_z_values(gcode: str) -> list[float]:
    """Every Z target emitted by a G1 move in the block."""
    zs: list[float] = []
    for line in gcode.splitlines():
        code = line.split(";", 1)[0].strip()
        toks = code.split()
        if not toks or toks[0] != "G1":
            continue
        for tok in toks[1:]:
            if tok.startswith("Z"):
                zs.append(float(tok[1:]))
    return zs


def _all_xy(gcode: str) -> tuple[list[float], list[float]]:
    """Every X and every Y target emitted by any G0/G1 move in the block."""
    xs: list[float] = []
    ys: list[float] = []
    for line in gcode.splitlines():
        code = line.split(";", 1)[0].strip()
        toks = code.split()
        if not toks or toks[0] not in ("G0", "G1"):
            continue
        for tok in toks[1:]:
            if tok.startswith("X"):
                xs.append(float(tok[1:]))
            elif tok.startswith("Y"):
                ys.append(float(tok[1:]))
    return xs, ys


class TestDefaultsProfile:
    def test_generates_and_self_validates(self):
        profile = _profile()
        gcode = generate_eject_gcode(profile, 30.0, H2S_GEOMETRY)
        result = validate_eject_gcode(gcode, profile, 30.0, H2S_GEOMETRY)
        assert result.ok, result.errors
        assert result.warnings == []

    def test_block_markers_and_profile_name(self):
        gcode = generate_eject_gcode(_profile(name="widget"), 30.0, H2S_GEOMETRY)
        assert gcode.startswith("; ===== FARM EJECT BLOCK profile=widget =====")
        assert gcode.rstrip().endswith("; ===== FARM EJECT BLOCK END =====")

    def test_prologue_reengages_without_z_home(self):
        gcode = generate_eject_gcode(_profile(), 30.0, H2S_GEOMETRY)
        lines = [ln.strip() for ln in gcode.splitlines()]
        assert "M17" in lines
        assert "G28 X Y" in lines
        assert "G90" in lines
        # Never home Z (would probe the bed centre under the part).
        assert not any(ln == "G28" for ln in lines)
        assert not any(ln.startswith("G28") and "Z" in ln for ln in lines)

    def test_clearance_z_is_max_z_plus_clearance(self):
        gcode = generate_eject_gcode(_profile(clearance_mm=10.0), 30.0, H2S_GEOMETRY)
        assert "G1 Z40 F900" in gcode  # 30 + 10

    def test_cooldown_retries_and_threshold(self):
        gcode = generate_eject_gcode(_profile(), 30.0, H2S_GEOMETRY)
        assert gcode.count("M190 R28") == 5
        assert "M140 S0" in gcode

    def test_fan_assist_toggles_m106(self):
        with_fan = generate_eject_gcode(_profile(cooling_fan_assist=True), 30.0, H2S_GEOMETRY)
        without_fan = generate_eject_gcode(_profile(cooling_fan_assist=False), 30.0, H2S_GEOMETRY)
        assert "M106 S255" in with_fan
        assert "M106 S255" not in without_fan

    def test_parks_centre_at_safe_z(self):
        gcode = generate_eject_gcode(_profile(), 30.0, H2S_GEOMETRY)
        # H2S bed 340x320 -> centre 170,160
        assert "G1 X170 Y160 Z10 F9000" in gcode

    def test_no_move_below_z_offset(self):
        gcode = generate_eject_gcode(_profile(z_offset_mm=0.4), 30.0, H2S_GEOMETRY)
        for line in gcode.splitlines():
            code = line.split(";", 1)[0].strip()
            for tok in code.split():
                if tok.startswith("Z"):
                    assert float(tok[1:]) >= 0.4 - 1e-9


class TestThermalLessDryRunMode:
    """include_cooldown=False strips the thermal gate for the empty-bed dry run.

    The dry run validates sweep GEOMETRY on an ambient empty bed, where an
    `M190 R` release wait can never complete (heater off, bed below target) and
    would hang the job forever. The block keeps its M140 S0 safety-off but emits
    no cooling fan and NO M190 waits.
    """

    def test_omits_all_m190_and_cooldown_fan(self):
        gcode = generate_eject_gcode(_profile(), 30.0, H2S_GEOMETRY, include_cooldown=False)
        assert "M190" not in gcode  # zero release waits of any kind
        assert "M106 S255" not in gcode  # no cooldown fan wait paired with M190
        assert "M140 S0" in gcode  # heater safety-off is still emitted

    def test_still_emits_prologue_and_full_sweep(self):
        gcode = generate_eject_gcode(_profile(), 30.0, H2S_GEOMETRY, include_cooldown=False)
        lines = [ln.strip() for ln in gcode.splitlines()]
        # Prologue re-engage (still never a bare G28 / G28 Z inside the block).
        assert "M17" in lines
        assert "G28 X Y" in lines
        assert "G90" in lines
        assert not any(ln == "G28" for ln in lines)
        # Sweep + park geometry is unchanged from production.
        assert any(ln.startswith("G1 Y") for ln in lines)
        assert "G1 X170 Y160 Z10 F9000" in gcode  # centre park (bed 340x320)

    def test_self_validates_as_dry_run(self):
        profile = _profile()
        gcode = generate_eject_gcode(profile, 30.0, H2S_GEOMETRY, include_cooldown=False)
        result = validate_eject_gcode(gcode, profile, 30.0, H2S_GEOMETRY, require_cooldown=False)
        assert result.ok, result.errors

    def test_geometry_identical_to_production_minus_thermal(self):
        # Same sweep lanes/levels; only the thermal section differs.
        profile = _profile(x_passes=3, descent_steps=2)
        prod = generate_eject_gcode(profile, 25.0, H2S_GEOMETRY)
        dry = generate_eject_gcode(profile, 25.0, H2S_GEOMETRY, include_cooldown=False)
        sweep_prefixes = ("G1 X", "G1 Y", "G1 Z")
        prod_moves = [ln for ln in prod.splitlines() if ln.startswith(sweep_prefixes)]
        dry_moves = [ln for ln in dry.splitlines() if ln.startswith(sweep_prefixes)]
        assert prod_moves == dry_moves

    def test_production_default_keeps_full_thermal_gate(self):
        # Explicit contrast: the default (production) path is unchanged.
        gcode = generate_eject_gcode(_profile(), 30.0, H2S_GEOMETRY)
        assert "M140 S0" in gcode
        assert "M106 S255" in gcode
        assert gcode.count("M190 R28") == 5


class TestCustomThresholdRetries:
    def test_cold_release_eight_retries(self):
        profile = _profile(name="cryonix", cooldown_temp_c=35.0, cooldown_retries=8)
        gcode = generate_eject_gcode(profile, 20.0, H2S_GEOMETRY)
        assert gcode.count("M190 R35") == 8
        assert validate_eject_gcode(gcode, profile, 20.0, H2S_GEOMETRY).ok

    def test_single_retry_and_lane(self):
        profile = _profile(name="fast", cooldown_retries=1, x_passes=1, descent_steps=1)
        gcode = generate_eject_gcode(profile, 15.0, H2S_GEOMETRY)
        result = validate_eject_gcode(gcode, profile, 15.0, H2S_GEOMETRY)
        assert result.ok, result.errors
        assert gcode.count("M190 R28") == 1


class TestRejections:
    def test_tall_part_rejected(self):
        with pytest.raises(EjectGenerationError, match="exceeds"):
            generate_eject_gcode(_profile(max_part_height_mm=42.0), 50.1, H2S_GEOMETRY)

    def test_part_at_exactly_limit_is_allowed(self):
        gcode = generate_eject_gcode(_profile(max_part_height_mm=42.0), 42.0, H2S_GEOMETRY)
        assert "FARM EJECT BLOCK" in gcode

    def test_generation_uses_geometry_bed_centre(self):
        # The generator keys coordinates on the PASSED geometry: an H2C block
        # (bed 330x320) parks at the H2C centre (165,160), not the H2S centre.
        # Unknown-model rejection now lives in the geometry accessor (test_geometry).
        gcode = generate_eject_gcode(_profile(), 20.0, H2C_GEOMETRY)
        assert "G1 X165 Y160 Z10 F9000" in gcode

    def test_h2s_geometry_present(self):
        assert H2S_GEOMETRY.bed == (340.0, 320.0)


class TestDualNozzleHoming:
    """Dual-nozzle (Vortek) prologue homing — 007-H2C incident, 2026-07-12.

    An unparameterized `G28` / `G28 X Y` stall-loops on dual-nozzle H2 firmware
    (failed sensorless X-homing: the carriage rams the X-homing wall nonstop).
    Dual models must home with the stock torque-parameterized forms
    (DUAL_NOZZLE_HOME); single-nozzle models keep `G28 X Y` byte-identical.
    """

    def test_dual_geometry_emits_parameterized_home_lines(self):
        gcode = generate_eject_gcode(_profile(), 30.0, H2C_GEOMETRY)
        lines = [ln.strip() for ln in gcode.splitlines()]
        for home in DUAL_NOZZLE_HOME:
            assert home in lines
        # In order, directly after M17, before G90.
        m17_idx = lines.index("M17")
        x_idx = lines.index("G28 X T300")
        y_idx = lines.index("G28 Y T300")
        g90_idx = lines.index("G90")
        assert m17_idx < x_idx < y_idx < g90_idx

    def test_dual_geometry_never_emits_g28_x_y(self):
        gcode = generate_eject_gcode(_profile(), 30.0, H2C_GEOMETRY)
        lines = [ln.strip() for ln in gcode.splitlines()]
        assert "G28 X Y" not in lines
        # And never a bare / Z-touching home either.
        assert not any(ln == "G28" for ln in lines)
        assert not any(ln.startswith("G28") and "Z" in ln for ln in lines)

    def test_h2s_geometry_keeps_g28_x_y_and_no_parameterized_forms(self):
        gcode = generate_eject_gcode(_profile(), 30.0, H2S_GEOMETRY)
        lines = [ln.strip() for ln in gcode.splitlines()]
        assert "G28 X Y" in lines
        for home in DUAL_NOZZLE_HOME:
            assert home not in lines
        assert "T300" not in gcode

    def test_dual_block_self_validates(self):
        profile = _profile()
        gcode = generate_eject_gcode(profile, 30.0, H2C_GEOMETRY)
        result = validate_eject_gcode(gcode, profile, 30.0, H2C_GEOMETRY)
        assert result.ok, result.errors

    def test_dual_dryrun_block_self_validates(self):
        profile = _profile()
        gcode = generate_eject_gcode(profile, 30.0, H2C_GEOMETRY, include_cooldown=False)
        result = validate_eject_gcode(gcode, profile, 30.0, H2C_GEOMETRY, require_cooldown=False)
        assert result.ok, result.errors


class TestSweepBand:
    def test_band_bounds_the_lanes(self):
        # Both bounds set -> sweep lanes span exactly [min, max].
        profile = _profile(sweep_x_min_mm=50.0, sweep_x_max_mm=200.0, x_passes=11)
        gcode = generate_eject_gcode(profile, 30.0, H2S_GEOMETRY)
        xs = _sweep_x_values(gcode)
        assert min(xs) == pytest.approx(50.0)
        assert max(xs) == pytest.approx(200.0)
        assert all(50.0 - 1e-9 <= x <= 200.0 + 1e-9 for x in xs)
        assert validate_eject_gcode(gcode, profile, 30.0, H2S_GEOMETRY).ok

    def test_default_full_width_spans_margin_inset_bed(self):
        # No band -> margin-inset full width span (3 .. bed_x-3 = 337). The
        # permissive envelope must not narrow it: this exact span was operator-
        # witnessed sweeping a full plate on a real H2S (2026-07-04, dry-run v1).
        gcode = generate_eject_gcode(_profile(x_margin_mm=3.0, x_passes=11), 30.0, H2S_GEOMETRY)
        xs = _sweep_x_values(gcode)
        assert min(xs) == pytest.approx(3.0)
        assert max(xs) == pytest.approx(337.0)

    def test_one_sided_band_min_only_rejected(self):
        with pytest.raises(EjectGenerationError, match="both be set or both be null"):
            generate_eject_gcode(_profile(sweep_x_min_mm=50.0, sweep_x_max_mm=None), 30.0, H2S_GEOMETRY)

    def test_one_sided_band_max_only_rejected(self):
        with pytest.raises(EjectGenerationError, match="both be set or both be null"):
            generate_eject_gcode(_profile(sweep_x_min_mm=None, sweep_x_max_mm=200.0), 30.0, H2S_GEOMETRY)

    def test_band_width_below_minimum_rejected(self):
        with pytest.raises(EjectGenerationError, match="below the 10"):
            generate_eject_gcode(_profile(sweep_x_min_mm=50.0, sweep_x_max_mm=55.0), 30.0, H2S_GEOMETRY)

    def test_inverted_band_rejected(self):
        with pytest.raises(EjectGenerationError, match="0 <= sweep_x_min_mm"):
            generate_eject_gcode(_profile(sweep_x_min_mm=200.0, sweep_x_max_mm=50.0), 30.0, H2S_GEOMETRY)

    def test_band_past_bed_edge_rejected(self):
        with pytest.raises(EjectGenerationError, match="exceeds bed width"):
            generate_eject_gcode(_profile(sweep_x_min_mm=50.0, sweep_x_max_mm=400.0), 30.0, H2S_GEOMETRY)


class TestSweepStartFrac:
    def test_top_level_is_fraction_of_part_height(self):
        # max_z 50.1, frac 0.5 -> top sweep level 25.05.
        profile = _profile(sweep_start_frac=0.5, max_part_height_mm=60.0)
        gcode = generate_eject_gcode(profile, 50.1, H2S_GEOMETRY)
        assert "G1 Z25.05 F600" in gcode
        # Prologue clearance STILL clears the full part top (50.1 + 10 = 60.1).
        assert "G1 Z60.1 F900" in gcode

    def test_default_frac_starts_at_part_top(self):
        gcode = generate_eject_gcode(_profile(sweep_start_frac=1.0), 30.0, H2S_GEOMETRY)
        # Descent top level equals the part top (30).
        assert "G1 Z30 F600" in gcode

    def test_frac_clamped_at_z_offset_floor(self):
        # A tiny fraction would put the top below z_offset -> clamp to the floor.
        # max_part_height_mm must clear the 50 mm part or generation is refused
        # by the height guard before the frac logic runs (cf. the sibling test).
        profile = _profile(sweep_start_frac=0.001, z_offset_mm=0.4, max_part_height_mm=60.0)
        gcode = generate_eject_gcode(profile, 50.0, H2S_GEOMETRY)
        for z in _sweep_z_values(gcode):
            assert z >= 0.4 - 1e-9

    def test_none_frac_treated_as_full_top(self):
        # A transient profile with the attribute unset behaves like frac=1.0.
        profile = _profile(sweep_start_frac=None)
        gcode = generate_eject_gcode(profile, 30.0, H2S_GEOMETRY)
        assert "G1 Z30 F600" in gcode


class TestFinalSkim:
    """The final_skim toggle gates the trailing slow skim pass.

    True (default) keeps the prior behaviour: after the descent sweeps, one more
    slow pass at the z_offset floor clears thin remnants. False pushes exactly
    once (e.g. a single mid-height lane for a tall part) — no skim pass at all.
    """

    def test_default_keeps_final_skim(self):
        # Default profile (final_skim True) keeps the skim marker + a skim-speed pass.
        gcode = generate_eject_gcode(_profile(), 30.0, H2S_GEOMETRY)
        assert "; --- final skim ---" in gcode
        assert "F1500" in gcode  # skim_speed_mm_min default -> a skim-speed push

    def test_final_skim_false_single_push(self):
        # One lane, one descent level, half-height start, skim OFF -> EXACTLY one
        # push toward the front at the eject speed; zero skim-speed passes; no
        # skim marker. Still a valid production block.
        profile = _profile(
            name="single",
            final_skim=False,
            x_passes=1,
            descent_steps=1,
            sweep_start_frac=0.5,
        )
        gcode = generate_eject_gcode(profile, 30.0, H2S_GEOMETRY)
        assert "; --- final skim ---" not in gcode
        assert "F1500" not in gcode  # no skim-speed pass emitted at all
        push_lines = [ln for ln in gcode.splitlines() if ln.startswith("G1 Y-2 ")]
        assert push_lines == ["G1 Y-2 F3000"]  # one Y-to-front push at eject speed
        assert validate_eject_gcode(gcode, profile, 30.0, H2S_GEOMETRY).ok

    def test_none_final_skim_treated_as_true(self):
        # A transient profile with the attribute unset behaves like final_skim=True.
        profile = _profile(final_skim=None)
        gcode = generate_eject_gcode(profile, 30.0, H2S_GEOMETRY)
        assert "; --- final skim ---" in gcode
        assert "F1500" in gcode

    def test_final_skim_false_omits_skim_but_keeps_descent(self):
        # With multiple lanes/levels, skim OFF removes ONLY the trailing skim
        # marker + pass; the descent sweeps at eject speed are unchanged.
        on_profile = _profile(x_passes=3, descent_steps=2)
        off_profile = _profile(name="noskim", final_skim=False, x_passes=3, descent_steps=2)
        on = generate_eject_gcode(on_profile, 30.0, H2S_GEOMETRY)
        off = generate_eject_gcode(off_profile, 30.0, H2S_GEOMETRY)
        assert on.count("; --- final skim ---") == 1
        assert off.count("; --- final skim ---") == 0
        # Descent (eject-speed) pushes identical; only the extra skim pass differs.
        assert off.count("G1 Y-2 F3000") == on.count("G1 Y-2 F3000")
        assert off.count("F1500") == 0
        assert validate_eject_gcode(off, off_profile, 30.0, H2S_GEOMETRY).ok


class TestTravelEnvelopeClamp:
    """Generated XY moves stay inside the machine travel envelope.

    The envelope is PERMISSIVE (gross-configuration guard only): the full
    default sweep span (X 3..337, Y -2..322) was operator-witnessed executing a
    complete plate sweep on a real H2S (2026-07-04, dry-run v1), so the envelope
    must never narrow the default geometry. Real measured limits replace these
    values after the live soft-limit probe session.
    """

    def test_h2s_envelope_constant(self):
        # Permissive gross-guard bounds (x_min, x_max, y_min, y_max).
        assert H2S_GEOMETRY.envelope == (0.0, 340.0, -16.0, 325.0)

    def test_default_profile_emits_nothing_outside_envelope(self):
        x_min, x_max, y_min, y_max = H2S_GEOMETRY.envelope
        gcode = generate_eject_gcode(_profile(), 30.0, H2S_GEOMETRY)
        xs, ys = _all_xy(gcode)
        assert xs and ys
        assert min(xs) >= x_min - 1e-9
        assert max(xs) <= x_max + 1e-9
        assert min(ys) >= y_min - 1e-9
        assert max(ys) <= y_max + 1e-9

    def test_full_sweep_span_present(self):
        # The operator-witnessed working geometry: outer lanes at the margin
        # (X 3 / 337) and lanes spanning front push-off to back overhang
        # (Y -2 / 322). The permissive envelope must pass all of it through.
        gcode = generate_eject_gcode(_profile(), 30.0, H2S_GEOMETRY)
        xs, ys = _all_xy(gcode)
        assert 3.0 in xs
        assert 337.0 in xs
        assert 322.0 in ys
        assert min(ys) == pytest.approx(-2.0)
        assert max(ys) == pytest.approx(322.0)

    def test_band_span_inside_bed_not_narrowed(self):
        # Band [50, 335] is legal vs the 340 bed and inside the permissive
        # envelope -> passes through unchanged.
        profile = _profile(sweep_x_min_mm=50.0, sweep_x_max_mm=335.0, x_passes=11)
        gcode = generate_eject_gcode(profile, 30.0, H2S_GEOMETRY)
        xs = _sweep_x_values(gcode)
        assert min(xs) == pytest.approx(50.0)
        assert max(xs) == pytest.approx(335.0)
        assert validate_eject_gcode(gcode, profile, 30.0, H2S_GEOMETRY).ok

    def test_gross_config_front_overhang_clamped(self):
        # Gross-guard engagement: an absurd front overhang (Y -30) is clamped to
        # the envelope floor (-16) instead of reaching the firmware.
        gcode = generate_eject_gcode(_profile(front_overhang_mm=30.0), 30.0, H2S_GEOMETRY)
        _xs, ys = _all_xy(gcode)
        assert min(ys) == pytest.approx(-16.0)

    def test_generated_block_passes_validator(self):
        # Independent-defense round trip: what the generator emits must validate.
        profile = _profile()
        gcode = generate_eject_gcode(profile, 30.0, H2S_GEOMETRY)
        assert validate_eject_gcode(gcode, profile, 30.0, H2S_GEOMETRY).ok
