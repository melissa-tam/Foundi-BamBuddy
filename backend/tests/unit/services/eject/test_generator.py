"""Golden-structure tests for the eject G-code generator."""

import logging
import math
import pathlib
from dataclasses import replace

import pytest

from backend.app.models.eject_profile import EjectProfile
from backend.app.services.eject.generator import (
    EJECT_RUNTIME_OVERHEAD_S,
    PHASE_BEACON_LIFTED,
    PHASE_BEACON_PARK,
    PHASE_BEACON_SWEEP,
    SWEEP_PHASE_MARKER,
    UNMODELLED_EPILOGUE_ALLOWANCE_S,
    EjectGenerationError,
    estimate_runtime_s,
    estimate_runtime_segments,
    generate_eject_gcode,
)
from backend.app.services.eject.remote import eject_abort_deadline_s
from backend.app.services.eject.validator import validate_eject_gcode
from backend.app.utils.printer_models import DUAL_NOZZLE_HOME
from backend.tests.unit.services.eject.geometry_fixtures import H2C_GEOMETRY, H2S_GEOMETRY


def _profile(**overrides) -> EjectProfile:
    """Build an in-memory EjectProfile with the documented defaults."""
    defaults = {
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
        "bed_drop_dwell_s": None,
        "bed_drop_jitter_cycles": None,
        "bed_drop_jitter_mm": None,
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

    def test_motion_only_no_cooldown_wait(self):
        # The eject block is motion-only now: the bed heater is commanded off but
        # there is NO in-file cooldown wait (that moved into the eject monitor).
        gcode = generate_eject_gcode(_profile(), 30.0, H2S_GEOMETRY)
        assert "M140 S0" in gcode
        assert "M190" not in gcode  # no cooldown release wait of any kind
        assert "M106 S255" not in gcode  # no cooldown fan

    def test_completion_epilogue_makes_block_self_completing(self):
        # A standalone eject file must end FINISH, so the block carries the stock
        # machine-end finish tail: progress reset + the judge-flag finish sequence
        # + M400 + M18.
        gcode = generate_eject_gcode(_profile(), 30.0, H2S_GEOMETRY)
        assert "M1002 judge_flag print_finish_air_filt_flag" in gcode
        assert "M73 P100 R0" in gcode
        assert "M400" in gcode
        assert "M18" in gcode
        # The epilogue sits after the sweep/park, before the block-end marker.
        lines = gcode.splitlines()
        assert lines.index("M18") < lines.index("; ===== FARM EJECT BLOCK END =====")

    def test_parks_centre_at_safe_z(self):
        gcode = generate_eject_gcode(_profile(), 30.0, H2S_GEOMETRY)
        # H2S bed 340x320 -> centre 170,160. End state parks proportional to part
        # height: bed drops clear FIRST (30 + clearance 10 = 40), THEN a straight
        # traverse to centre — no low-Z diagonal through a surviving part.
        lines = gcode.splitlines()
        assert "G1 Z40 F900" in lines
        assert "G1 X170 Y160 F9000" in lines
        park_idx = lines.index("G1 X170 Y160 F9000")
        assert lines[park_idx - 1] == "G1 Z40 F900"
        # The old fixed bed-high combined park must be gone.
        assert "G1 X170 Y160 Z10 F9000" not in gcode

    def test_park_z_scales_with_part_height(self):
        # The park (end-state) Z tracks part height + clearance, not a fixed value.
        short = generate_eject_gcode(_profile(clearance_mm=10.0), 20.0, H2S_GEOMETRY)
        tall = generate_eject_gcode(_profile(clearance_mm=10.0, max_part_height_mm=60.0), 40.0, H2S_GEOMETRY)

        def _park_z(gcode: str) -> float:
            lines = gcode.splitlines()
            idx = lines.index("G1 X170 Y160 F9000")
            z_line = lines[idx - 1]
            return float(z_line.split("Z", 1)[1].split()[0])

        assert _park_z(short) == pytest.approx(30.0)  # 20 + 10
        assert _park_z(tall) == pytest.approx(50.0)  # 40 + 10
        assert _park_z(tall) > _park_z(short)

    def test_park_z_floored_at_park_z_mm(self):
        # Tiny part + clearance 0 -> lift 5, but the park floors at PARK_Z_MM (10).
        gcode = generate_eject_gcode(_profile(clearance_mm=0.0), 5.0, H2S_GEOMETRY)
        lines = gcode.splitlines()
        assert "G1 Z10 F900" in lines
        park_idx = lines.index("G1 X170 Y160 F9000")
        assert lines[park_idx - 1] == "G1 Z10 F900"

    def test_no_move_below_z_offset(self):
        gcode = generate_eject_gcode(_profile(z_offset_mm=0.4), 30.0, H2S_GEOMETRY)
        for line in gcode.splitlines():
            code = line.split(";", 1)[0].strip()
            for tok in code.split():
                if tok.startswith("Z"):
                    assert float(tok[1:]) >= 0.4 - 1e-9


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
        lines = gcode.splitlines()
        # Part-clear park: Z (20 + clearance 10 = 30) first, then the H2C centre.
        assert "G1 Z30 F900" in lines
        assert "G1 X165 Y160 F9000" in lines
        park_idx = lines.index("G1 X165 Y160 F9000")
        assert lines[park_idx - 1] == "G1 Z30 F900"

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


class TestBedDropReleaseAssist:
    """Farm eject v2: the optional bed-drop release assist drives the bed all the
    way DOWN (bigger Z) then back to the lift height, between the heater-off and the
    sweep. NULL clearance = off (the v1 goldens stay byte-identical)."""

    def test_drop_emits_down_then_return_between_heater_off_and_sweep(self):
        # H2S z_travel 340, clearance 50 -> drop to 290; max_z 30 + clearance 10 ->
        # return to lift 40. The pair sits after M140 S0, before the sweep comment.
        profile = _profile(bed_drop_clearance_mm=50.0)
        gcode = generate_eject_gcode(profile, 30.0, H2S_GEOMETRY)
        lines = [ln.strip() for ln in gcode.splitlines()]
        # The drop (Z290) + return (Z40) are the two lines right after the marker,
        # which itself sits between M140 S0 and the sweep. (Z40 also appears in the
        # prologue clearance lift, so anchor positionally on the unique marker.)
        heater_idx = lines.index("M140 S0")
        marker_idx = lines.index("; --- bed-drop release assist: full down + return ---")
        sweep_idx = lines.index("; --- sweep: push part off the front edge ---")
        assert heater_idx < marker_idx < sweep_idx
        assert lines[marker_idx + 1] == "G1 Z290 F900"
        assert lines[marker_idx + 2] == "G1 Z40 F900"

    def test_drop_zero_clearance_goes_to_full_travel(self):
        # clearance 0 (still "set", not None) -> drop to the machine bottom z_travel.
        profile = _profile(bed_drop_clearance_mm=0.0)
        gcode = generate_eject_gcode(profile, 30.0, H2S_GEOMETRY)
        assert "G1 Z340 F900" in gcode  # 340 - 0
        assert "G1 Z40 F900" in gcode  # return to lift

    def test_disabled_emits_no_drop_marker(self):
        gcode = generate_eject_gcode(_profile(bed_drop_clearance_mm=None), 30.0, H2S_GEOMETRY)
        assert "bed-drop release assist" not in gcode

    def test_missing_z_travel_fails_closed(self):
        geom = replace(H2S_GEOMETRY, z_travel_mm=None)
        with pytest.raises(EjectGenerationError, match="z_travel_mm"):
            generate_eject_gcode(_profile(bed_drop_clearance_mm=50.0), 30.0, geom)

    def test_degenerate_drop_rejected(self):
        # clearance so large the drop target is not below the lift height -> refuse.
        # H2S z_travel 340, lift 40 -> clearance 305 gives drop 35 <= 40.
        with pytest.raises(EjectGenerationError, match="degenerate drop"):
            generate_eject_gcode(_profile(bed_drop_clearance_mm=305.0), 30.0, H2S_GEOMETRY)

    def test_drop_block_self_validates_on_both_geometries(self):
        for geometry, drop_z in ((H2S_GEOMETRY, "G1 Z290 F900"), (H2C_GEOMETRY, "G1 Z275 F900")):
            profile = _profile(bed_drop_clearance_mm=50.0)
            gcode = generate_eject_gcode(profile, 30.0, geometry)
            assert drop_z in gcode
            result = validate_eject_gcode(gcode, profile, 30.0, geometry)
            assert result.ok, result.errors


class TestBedDropDwellAndJitter:
    """The two drop-FLOOR behaviours. Emission order is drop → jitter → dwell →
    return; jitter strokes rise AWAY from the machine bottom first so no move
    passes the drop target, and the dwell is `M400 S<n>` (the only dwell form the
    runtime estimator counts, and the abort watchdog consumes that estimate)."""

    def test_emission_order_is_drop_jitter_dwell_return(self):
        # H2S z_travel 340 - clearance 50 -> drop 290; lift 40; 3 x 10 mm strokes
        # oscillate 290 -> 280 -> 290, then a 5 s hold, then the return.
        profile = _profile(
            bed_drop_clearance_mm=50.0,
            bed_drop_dwell_s=5,
            bed_drop_jitter_cycles=3,
            bed_drop_jitter_mm=10.0,
        )
        gcode = generate_eject_gcode(profile, 30.0, H2S_GEOMETRY)
        lines = [ln.strip() for ln in gcode.splitlines()]
        marker_idx = lines.index("; --- bed-drop release assist: full down + return ---")
        block = [ln for ln in lines[marker_idx + 1 :] if ln and not ln.startswith(";")]
        assert block[:9] == [
            "G1 Z290 F900",  # drop to the floor
            "G1 Z280 F900",  # jitter 1: up (away from the machine bottom) ...
            "G1 Z290 F900",  # ... and back to the floor
            "G1 Z280 F900",  # jitter 2
            "G1 Z290 F900",
            "G1 Z280 F900",  # jitter 3
            "G1 Z290 F900",
            "M400 S5",  # dwell at the floor, AFTER the strokes
            "G1 Z40 F900",  # return to the lift height, LAST
        ]

    def test_dwell_alone_sits_between_drop_and_return(self):
        profile = _profile(bed_drop_clearance_mm=50.0, bed_drop_dwell_s=7)
        gcode = generate_eject_gcode(profile, 30.0, H2S_GEOMETRY)
        lines = [ln.strip() for ln in gcode.splitlines()]
        marker_idx = lines.index("; --- bed-drop release assist: full down + return ---")
        assert lines[marker_idx + 1] == "G1 Z290 F900"
        assert lines[marker_idx + 3] == "M400 S7"  # +2 is the dwell comment
        assert lines[marker_idx + 4] == "G1 Z40 F900"

    def test_dwell_is_m400_never_g4(self):
        # G4 is invisible to estimate_runtime_s, which the abort watchdog consumes.
        profile = _profile(bed_drop_clearance_mm=50.0, bed_drop_dwell_s=5)
        gcode = generate_eject_gcode(profile, 30.0, H2S_GEOMETRY)
        assert "M400 S5" in gcode
        assert not any(ln.strip().startswith("G4") for ln in gcode.splitlines())

    def test_jitter_never_passes_the_drop_target(self):
        profile = _profile(bed_drop_clearance_mm=50.0, bed_drop_jitter_cycles=2, bed_drop_jitter_mm=10.0)
        gcode = generate_eject_gcode(profile, 30.0, H2S_GEOMETRY)
        assert max(_sweep_z_values(gcode)) == pytest.approx(290.0)

    def test_disabled_emits_neither(self):
        gcode = generate_eject_gcode(_profile(bed_drop_clearance_mm=50.0), 30.0, H2S_GEOMETRY)
        assert "bed-drop jitter" not in gcode
        assert "bed-drop dwell" not in gcode

    def test_one_sided_jitter_rejected(self):
        for overrides in ({"bed_drop_jitter_cycles": 3}, {"bed_drop_jitter_mm": 10.0}):
            with pytest.raises(EjectGenerationError, match="both set or both null"):
                generate_eject_gcode(_profile(bed_drop_clearance_mm=50.0, **overrides), 30.0, H2S_GEOMETRY)

    def test_jitter_reaching_the_lift_height_rejected(self):
        # drop 290 - lift 40 = 250 mm of span; a 250 mm stroke lands ON the lift.
        profile = _profile(bed_drop_clearance_mm=50.0, bed_drop_jitter_cycles=1, bed_drop_jitter_mm=250.0)
        with pytest.raises(EjectGenerationError, match="cross the lift height"):
            generate_eject_gcode(profile, 30.0, H2S_GEOMETRY)

    def test_dwell_without_bed_drop_rejected(self):
        with pytest.raises(EjectGenerationError, match="require the bed-drop release assist"):
            generate_eject_gcode(_profile(bed_drop_dwell_s=5), 30.0, H2S_GEOMETRY)

    def test_jitter_without_bed_drop_rejected(self):
        profile = _profile(bed_drop_jitter_cycles=3, bed_drop_jitter_mm=10.0)
        with pytest.raises(EjectGenerationError, match="require the bed-drop release assist"):
            generate_eject_gcode(profile, 30.0, H2S_GEOMETRY)

    def test_dwell_adds_exactly_its_seconds_to_the_estimate(self):
        base = _profile(bed_drop_clearance_mm=50.0)
        held = _profile(bed_drop_clearance_mm=50.0, bed_drop_dwell_s=5)
        plain_s = estimate_runtime_s(generate_eject_gcode(base, 30.0, H2S_GEOMETRY))
        dwell_s = estimate_runtime_s(generate_eject_gcode(held, 30.0, H2S_GEOMETRY))
        assert dwell_s - plain_s == pytest.approx(5.0, abs=0.01)

    def test_jitter_adds_its_stroke_time_to_the_estimate(self):
        # 3 cycles x 2 moves x 10 mm at F900 = 60 mm of travel.
        base = _profile(bed_drop_clearance_mm=50.0)
        shaken = _profile(bed_drop_clearance_mm=50.0, bed_drop_jitter_cycles=3, bed_drop_jitter_mm=10.0)
        plain_s = estimate_runtime_s(generate_eject_gcode(base, 30.0, H2S_GEOMETRY))
        jitter_s = estimate_runtime_s(generate_eject_gcode(shaken, 30.0, H2S_GEOMETRY))
        assert jitter_s - plain_s == pytest.approx(2 * 3 * 10.0 / 900.0 * 60.0, abs=0.01)


class TestBedslingerBedDropGuard:
    """A bed-slinger's bed is fixed in Z (the gantry carries Z), so the bed-drop
    release assist is physically meaningless and the generator must fail closed —
    BEFORE the z_travel-None check, so the operator sees the bedslinger reason even
    when z_travel is also absent."""

    def test_bedslinger_with_bed_drop_raises(self):
        # z_travel None (as seeded for A2L): the bedslinger guard fires first, so the
        # message names the kinematics, not the missing z_travel.
        geom = replace(H2S_GEOMETRY, model_key="A2L", z_travel_mm=None)
        with pytest.raises(EjectGenerationError, match="bedslinger"):
            generate_eject_gcode(_profile(bed_drop_clearance_mm=50.0), 30.0, geom)

    def test_bedslinger_guard_wins_even_when_z_travel_present(self):
        # Even with a (nonsensical) z_travel set, a bedslinger + bed-drop is refused.
        geom = replace(H2S_GEOMETRY, model_key="A2L", z_travel_mm=325.0)
        with pytest.raises(EjectGenerationError, match="bedslinger"):
            generate_eject_gcode(_profile(bed_drop_clearance_mm=50.0), 30.0, geom)

    def test_bedslinger_without_bed_drop_generates_fine(self):
        # No bed-drop → the whole drop branch is skipped; a plain eject block for a
        # bed-slinger generates and self-validates (the bedslinger warning does not
        # make it invalid).
        geom = replace(H2S_GEOMETRY, model_key="A2L", z_travel_mm=None)
        profile = _profile(bed_drop_clearance_mm=None)
        gcode = generate_eject_gcode(profile, 30.0, geom)
        assert "bed-drop release assist" not in gcode
        result = validate_eject_gcode(gcode, profile, 30.0, geom)
        assert result.ok, result.errors


# The single-pass shape that was running in production on 2026-07-31: bed-drop from
# the lift height to the machine bottom and back, then ONE sweep lane at F3000 across
# the 324 mm Y span, then park. Hand-built (not generated) so the calibration figures
# it pins — 80-83 s observed nominal, 179 s during the gouged-plate incident — stay
# readable next to the assertion instead of hiding behind a profile fixture.
_INCIDENT_SHAPE_BLOCK = """\
; ===== FARM EJECT BLOCK profile=singlepass =====
M17
G28 X Y
G90
G1 Z60.1 F900
M140 S0
; --- bed-drop release assist: full down + return ---
G1 Z340 F900
G1 Z60.1 F900
; --- sweep ---
G1 X3 Y322 F9000
G1 Z50 F600
G1 X3 F9000
G1 Y-2 F3000
G1 Y322 F9000
G1 Z60.1 F900
G1 X170 Y160 F9000
M400 S1
M18
"""


class TestEstimateRuntime:
    """The runtime estimator behind the eject runtime guard (2026-07-31 incident).

    It exists to answer ONE question — "did this sweep take far longer than the
    motion it was told to perform?" — so these pin the rules that keep the estimate
    honest, not a precise duration."""

    def test_every_golden_lands_in_a_sane_band(self):
        # Finite, above the fixed overhead, and nowhere near a runaway. The committed
        # goldens lock the 4-descent × 11-lane recipe (55 full-width sweeps), so they
        # score ~590-640 s — an order of magnitude above the single-pass production
        # profile. The band is deliberately wide: this asserts the estimator never
        # returns garbage for a real block, while the shape-specific calibration is
        # pinned by test_incident_shape_matches_observed_nominal below.
        golden_dir = pathlib.Path(__file__).parent / "golden"
        goldens = sorted(golden_dir.glob("*.gcode"))
        assert goldens, "golden fixtures missing"
        for path in goldens:
            seconds = estimate_runtime_s(path.read_text())
            assert math.isfinite(seconds)
            assert EJECT_RUNTIME_OVERHEAD_S < seconds < 900, f"{path.name} estimated {seconds:.1f}s"

    def test_bed_drop_pair_is_counted(self):
        # The drop goldens differ from their namesakes ONLY by the bed-drop pair
        # (lift 40 → 290 → 40 at F900 = 500 mm ≈ 33.3 s). That move is the one that
        # stalled in the incident, so it must be fully inside the estimate.
        golden_dir = pathlib.Path(__file__).parent / "golden"
        plain = estimate_runtime_s((golden_dir / "default_h2s_z30.gcode").read_text())
        with_drop = estimate_runtime_s((golden_dir / "drop_h2s_z30.gcode").read_text())
        assert with_drop - plain == pytest.approx(500.0 / 900.0 * 60.0, abs=0.01)

    def test_dwell_inside_a_conditional_block_is_not_counted(self):
        # The stock finish tail's air-purification blocks each hold an M400 S180 that
        # fires only when the firmware set print_finish_air_filt_flag — no farm printer
        # does. Counting them would add 360 s and make the guard unable to ever fire.
        skipped = "M622 J1\nM400 S180\nM623\n"
        counted = "M400 S180\n"
        assert estimate_runtime_s(skipped) == pytest.approx(EJECT_RUNTIME_OVERHEAD_S)
        assert estimate_runtime_s(counted) == pytest.approx(EJECT_RUNTIME_OVERHEAD_S + 180.0)

    def test_conditional_prepare_line_also_opens_the_skip(self):
        # The stock tail opens with `M622.1 S0` before the J1/J2 arms; everything from
        # it to the next M623 is conditional either way.
        gcode = "M622.1 S0\nM1002 judge_flag print_finish_air_filt_flag\nM400 S180\nM623\nM400 S1\n"
        assert estimate_runtime_s(gcode) == pytest.approx(EJECT_RUNTIME_OVERHEAD_S + 1.0)

    def test_bare_m400_dwells_zero(self):
        assert estimate_runtime_s("M400\n") == pytest.approx(EJECT_RUNTIME_OVERHEAD_S)

    def test_modal_feedrate_persists_across_lines(self):
        # F is modal: the second move carries no F and must inherit F600, not be
        # dropped as feedless. 60 mm at F600 = 6 s per move.
        gcode = "G28 X Y\nG1 X60 F600\nG1 X120\n"
        assert estimate_runtime_s(gcode) == pytest.approx(EJECT_RUNTIME_OVERHEAD_S + 12.0)

    def test_move_before_any_feedrate_contributes_nothing(self):
        # Defensive: the generator always emits an explicit F, so a feedless leading
        # move means the input is not one of ours — score it 0 rather than guess.
        assert estimate_runtime_s("G28 X Y\nG1 X100\n") == pytest.approx(EJECT_RUNTIME_OVERHEAD_S)

    def test_unknown_axis_first_move_contributes_zero(self):
        # G28 zeroes X/Y but NEVER Z (the eject prologue must not home Z with a part
        # on the plate), so the prologue's first Z lift has no known origin and is
        # unmeasurable. It still makes Z known — the bed-drop that follows IS counted.
        prologue_only = "M17\nG28 X Y\nG90\nG1 Z60 F900\n"
        assert estimate_runtime_s(prologue_only) == pytest.approx(EJECT_RUNTIME_OVERHEAD_S)
        then_dropped = prologue_only + "G1 Z340 F900\n"
        assert estimate_runtime_s(then_dropped) == pytest.approx(EJECT_RUNTIME_OVERHEAD_S + 280.0 / 900.0 * 60.0)

    def test_torque_home_dialect_zeroes_xy_like_the_bare_form(self):
        # Dual-nozzle models home with G28 X T300 / G28 Y T300 (a bare G28 X Y
        # stall-loops that firmware). Both dialects must establish the same datum,
        # or every H2C estimate would silently lose its first sweep move.
        bare = "G28 X Y\nG1 X60 Y0 F600\n"
        torque = "G28 X T300\nG28 Y T300\nG1 X60 Y0 F600\n"
        assert estimate_runtime_s(torque) == pytest.approx(estimate_runtime_s(bare))

    def test_comments_are_stripped(self):
        assert estimate_runtime_s("; G1 X999 F60\n") == pytest.approx(EJECT_RUNTIME_OVERHEAD_S)

    def test_incident_shape_matches_observed_nominal(self):
        # THE calibration: the single-pass profile running on 2026-07-31 executed in
        # 80-83 s across 11 nominal ejects. The estimate must land in that
        # neighbourhood — too low and the watchdog aborts healthy sweeps, too high and
        # the deadline drifts out past the incident's stall.
        seconds = estimate_runtime_s(_INCIDENT_SHAPE_BLOCK)
        assert 67.0 <= seconds <= 97.0, f"estimated {seconds:.1f}s, expected 82±15s"
        # The watchdog must fire well before the incident's 179 s while leaving every
        # nominal 80-83 s sweep untouched.
        assert eject_abort_deadline_s(seconds) < 179.0
        assert eject_abort_deadline_s(seconds) >= 83.0


class TestBuildSummaryLog:
    """The build emits exactly one INFO line carrying the geometry it just baked in.

    The emitted G-code is never persisted (it lives inside a temp artifact), so
    before this line the 2026-07-31 incident could only be reconstructed by
    re-deriving the block through the preview endpoint."""

    def test_summary_names_the_drop_target(self, caplog):
        with caplog.at_level(logging.INFO, logger="backend.app.services.eject.generator"):
            generate_eject_gcode(_profile(bed_drop_clearance_mm=50.0), 30.0, H2S_GEOMETRY)
        lines = [r.getMessage() for r in caplog.records]
        assert len(lines) == 1
        assert "model=H2S" in lines[0]
        assert "max_z=30" in lines[0]
        assert "lift_z=40" in lines[0]
        assert "drop_z=290" in lines[0]  # z_travel 340 - clearance 50
        assert "lanes=11" in lines[0]

    def test_summary_says_off_when_bed_drop_disabled(self, caplog):
        with caplog.at_level(logging.INFO, logger="backend.app.services.eject.generator"):
            generate_eject_gcode(_profile(), 30.0, H2S_GEOMETRY)
        assert "drop_z=off" in caplog.records[0].getMessage()


class TestPhaseBeacons:
    """The three M73 beacons the eject runtime watchdog times.

    mc_percent is M73-driven end to end and resets to 0 at job start, and the H2S
    publishes no mc_print_line_number — so these commands are the ONLY in-band signal
    that says which phase the machine is in. Their POSITIONS are the contract: the
    watchdog's pre-sweep guarantee rests on "percent below the sweep beacon ⇒ the sweep
    has not started", which only holds while P50 sits directly above the sweep marker."""

    @staticmethod
    def _lines(gcode: str) -> list[str]:
        return [ln.strip() for ln in gcode.splitlines()]

    def test_lifted_beacon_follows_the_prologue_lift(self):
        lines = self._lines(generate_eject_gcode(_profile(), 30.0, H2S_GEOMETRY))
        lift_idx = lines.index("G1 Z40 F900")
        assert lines[lift_idx + 1].startswith(PHASE_BEACON_LIFTED + " ;")
        # ...and it precedes the heater-off section, so the drop span it opens covers the
        # whole pre-sweep block rather than starting mid-way through it.
        assert lift_idx + 1 < lines.index("; --- bed heater off ---")

    @pytest.mark.parametrize("bed_drop", [None, 50.0], ids=["drop-off", "drop-on"])
    def test_sweep_beacon_sits_immediately_above_the_sweep_marker(self, bed_drop):
        # ONE emission site: with the bed-drop assist on the beacon must land after the
        # whole assist, with it off directly after M140 S0 — in both cases adjacent to
        # the marker, which is what makes the boundary unambiguous.
        lines = self._lines(generate_eject_gcode(_profile(bed_drop_clearance_mm=bed_drop), 30.0, H2S_GEOMETRY))
        marker_idx = lines.index(SWEEP_PHASE_MARKER)
        assert lines[marker_idx - 1].startswith(PHASE_BEACON_SWEEP + " ;")
        assert sum(1 for ln in lines if ln.startswith(PHASE_BEACON_SWEEP + " ")) == 1

    def test_park_beacon_closes_the_sweep_before_the_park_move(self):
        profile = _profile()
        lines = self._lines(generate_eject_gcode(profile, 30.0, H2S_GEOMETRY))
        park_idx = lines.index(PHASE_BEACON_PARK + " ; phase beacon: sweep done - eject runtime watchdog")
        # The park's Z move (bed clear FIRST, then the traverse to centre) follows it.
        assert lines[park_idx + 1] == "G1 Z40 F900"
        assert lines[park_idx + 2].startswith("G1 X170 Y160")
        # The last skim lane precedes it — the beacon closes the sweep, never splits it.
        assert lines.index("; --- final skim ---") < park_idx

    def test_beacons_are_ordered_and_the_epilogue_hundred_still_closes(self):
        gcode = generate_eject_gcode(_profile(bed_drop_clearance_mm=50.0), 30.0, H2S_GEOMETRY)
        codes = [ln.split(";", 1)[0].strip() for ln in gcode.splitlines()]
        # First-token match: the stock epilogue also carries `M73.2 R1.0` (a different
        # command), which is exactly why the estimator splits on the exact literals.
        assert [c for c in codes if c.split()[:1] == ["M73"]] == [
            PHASE_BEACON_LIFTED,
            PHASE_BEACON_SWEEP,
            PHASE_BEACON_PARK,
            "M73 P100 R0",
        ]

    def test_beacon_lines_are_pure_ascii(self):
        # The printer executes these bytes. Every eject block shipped to date has been
        # ASCII-only; a beacon comment must not be the first exception.
        gcode = generate_eject_gcode(_profile(bed_drop_clearance_mm=50.0), 30.0, H2S_GEOMETRY)
        gcode.encode("ascii")

    def test_a_beacon_bearing_block_passes_the_validator_unchanged(self):
        # M73 is not a motion / homing / tool-change command, so the validator has no
        # case for it — this locks that the beacons stay invisible to every guard.
        for overrides in ({}, {"bed_drop_clearance_mm": 50.0}):
            profile = _profile(**overrides)
            gcode = generate_eject_gcode(profile, 30.0, H2S_GEOMETRY)
            assert PHASE_BEACON_LIFTED in gcode
            result = validate_eject_gcode(gcode, profile, 30.0, H2S_GEOMETRY)
            assert result.ok, result.errors


class TestEstimateRuntimeSegments:
    """The per-phase split behind the pre-sweep guarantee and the post-sweep patience.
    The whole-job estimate can only catch a stall that overruns the ENTIRE eject;
    drop_span_s bounds the phase that actually stalls, sweep_span_s the phase that
    touches the plate, and tail_s what remains once neither can happen again."""

    def test_segments_sum_to_the_total_for_every_golden(self):
        # Exact equality, not approx: the total IS the sum, computed by one walk. Any
        # drift here means a second code path started scoring the block.
        golden_dir = pathlib.Path(__file__).parent / "golden"
        goldens = sorted(golden_dir.glob("*.gcode"))
        assert len(goldens) == 9, f"expected 9 golden fixtures, found {len(goldens)}"
        for path in goldens:
            gcode = path.read_text()
            seg = estimate_runtime_segments(gcode)
            assert (
                seg.pre_s + seg.drop_span_s + seg.sweep_span_s + seg.tail_s + EJECT_RUNTIME_OVERHEAD_S
                == estimate_runtime_s(gcode)
            )
            assert seg.total_s == estimate_runtime_s(gcode)

    def test_the_park_split_partitions_the_old_sweep_and_leaves_the_total_alone(self):
        # Splitting at PHASE_BEACON_PARK is an ANALYSIS change only: it must move no
        # commanded second into or out of the estimate the abort deadline is derived
        # from. Removing the boundary line reproduces the pre-split walk exactly, so the
        # two post-drop spans must merge back into one with the same total.
        golden_dir = pathlib.Path(__file__).parent / "golden"
        goldens = sorted(golden_dir.glob("*.gcode"))
        assert goldens, "golden fixtures missing"
        for path in goldens:
            gcode = path.read_text()
            assert PHASE_BEACON_PARK in gcode, f"{path.name} carries no park beacon"
            unsplit = "\n".join(ln for ln in gcode.splitlines() if not ln.startswith(PHASE_BEACON_PARK))
            seg, merged = estimate_runtime_segments(gcode), estimate_runtime_segments(unsplit)
            assert seg.sweep_span_s + seg.tail_s == pytest.approx(merged.sweep_span_s), path.name
            assert seg.total_s == pytest.approx(merged.total_s), path.name
            assert seg.tail_s > 0.0, f"{path.name} scored an empty tail"

    def test_drop_span_holds_exactly_the_bed_drop_motion(self):
        # lift 40 → 290 → 40 at F900 = 500 mm of commanded travel, and nothing else in
        # the drop-less block's span (M140 S0 costs no time).
        drop = estimate_runtime_segments(generate_eject_gcode(_profile(bed_drop_clearance_mm=50.0), 30.0, H2S_GEOMETRY))
        plain = estimate_runtime_segments(generate_eject_gcode(_profile(), 30.0, H2S_GEOMETRY))
        assert plain.drop_span_s == 0.0
        assert drop.drop_span_s == pytest.approx(500.0 / 900.0 * 60.0, abs=0.01)

    def test_drop_span_includes_the_floor_dwell_and_jitter(self):
        # Both act AT the drop floor, so both are inside the phase the watchdog bounds —
        # a 5 s hold omitted from the span would make every such eject look overrun.
        profile = _profile(
            bed_drop_clearance_mm=50.0, bed_drop_dwell_s=5, bed_drop_jitter_cycles=3, bed_drop_jitter_mm=10.0
        )
        seg = estimate_runtime_segments(generate_eject_gcode(profile, 30.0, H2S_GEOMETRY))
        bare = estimate_runtime_segments(generate_eject_gcode(_profile(bed_drop_clearance_mm=50.0), 30.0, H2S_GEOMETRY))
        assert seg.drop_span_s - bare.drop_span_s == pytest.approx(5.0 + 2 * 3 * 10.0 / 900.0 * 60.0, abs=0.01)

    def test_pre_segment_carries_the_unmeasurable_lift_only(self):
        # The prologue's first Z move has no known origin (G28 never homes Z with a part
        # on the plate), so it scores 0 — and the overhead belongs to no phase.
        seg = estimate_runtime_segments(generate_eject_gcode(_profile(), 30.0, H2S_GEOMETRY))
        assert seg.pre_s == 0.0
        assert seg.sweep_span_s > 0.0

    def test_the_park_beacon_closes_the_sweep_and_the_epilogue_hundred_does_not(self):
        # Only the three exact beacon literals split. M73 P100 R0 must stay inside
        # tail_s: it is the stock epilogue's own progress line, not a phase boundary.
        gcode = "G28 X Y\nM73 P5\nG1 Z100 F900\nG1 Z50 F900\nM73 P50\nM400 S3\nM73 P75\nM73 P100 R0\nM400 S4\n"
        seg = estimate_runtime_segments(gcode)
        assert seg.drop_span_s == pytest.approx(50.0 / 900.0 * 60.0, abs=0.01)
        assert seg.sweep_span_s == pytest.approx(3.0)
        assert seg.tail_s == pytest.approx(4.0)

    def test_a_repeated_beacon_cannot_reopen_a_closed_segment(self):
        # Defensive: boundaries only advance, so a replayed P5 after the sweep opened
        # cannot pull sweep motion back into the drop span — and a replayed P50 after the
        # park beacon cannot pull the firmware tail back into the sweep the watchdog
        # treats as plate-contact time.
        gcode = "G28 X Y\nM73 P5\nG1 Z100 F900\nM73 P50\nM73 P5\nM400 S9\n"
        seg = estimate_runtime_segments(gcode)
        assert seg.drop_span_s == 0.0
        assert seg.sweep_span_s == pytest.approx(9.0)
        replayed = estimate_runtime_segments("M73 P5\nM73 P50\nM73 P75\nM73 P50\nM400 S9\n")
        assert replayed.sweep_span_s == 0.0
        assert replayed.tail_s == pytest.approx(9.0)

    def test_a_conditional_dwell_is_excluded_from_every_segment(self):
        gcode = "M73 P5\nM73 P50\nM622 J1\nM400 S180\nM623\nM400 S2\nM73 P75\nM622.1 S0\nM400 S180\nM623\nM400 S1\n"
        seg = estimate_runtime_segments(gcode)
        assert (seg.pre_s, seg.drop_span_s) == (0.0, 0.0)
        assert seg.sweep_span_s == pytest.approx(2.0)
        # The tail's own conditional purge is the very omission
        # UNMODELLED_EPILOGUE_ALLOWANCE_S compensates for — it must not appear here.
        assert seg.tail_s == pytest.approx(1.0)

    def test_the_epilogue_allowance_covers_the_purge_it_is_sized_against(self):
        # Sized as one firing of the conditional air purge (M400 S180) plus chime and
        # publish slop. The estimator deliberately counts neither, so an allowance below
        # the purge could not absorb a single firing of it.
        assert UNMODELLED_EPILOGUE_ALLOWANCE_S >= 180.0
