"""Tests for the part-present eject-only file builder (remote first-article eject)."""

import os
import re
import tempfile
import zipfile
from pathlib import Path

import pytest

from backend.app.models.eject_profile import EjectProfile
from backend.app.services.eject.dispatch import BuiltEject, build_part_present_eject_file
from backend.app.services.eject.generator import (
    EJECT_RUNTIME_OVERHEAD_S,
    EjectGenerationError,
    estimate_runtime_s,
    estimate_runtime_segments,
)
from backend.app.utils.printer_models import DUAL_NOZZLE_HOME
from backend.app.utils.threemf_tools import (
    extract_filament_usage_from_3mf,
    extract_print_time_from_3mf,
)
from backend.tests.unit.services.eject.geometry_fixtures import H2C_GEOMETRY, H2S_GEOMETRY

_PLATE_GCODE = (
    "; HEADER_BLOCK_START\n"
    "; max_z_height: 18.00\n"
    "; HEADER_BLOCK_END\n"
    "; EXECUTABLE_BLOCK_START\n"
    "G28\n"  # the ORIGINAL print homes all axes — must NOT survive into the eject file
    "G1 X10 Y10 E1\n"
    "; EXECUTABLE_BLOCK_END\n"
)

# A donor slice_info carrying real usage (~407 g / 16735 s) that repack copies
# verbatim — the motion-only eject build must zero it so no consumer books it.
_SLICE_INFO_NONZERO = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    "<config>\n"
    "  <plate>\n"
    '    <metadata key="index" value="1"/>\n'
    '    <metadata key="prediction" value="16735"/>\n'
    '    <metadata key="weight" value="406.85"/>\n'
    '    <filament id="1" type="PETG" color="#FF8000" used_g="406.9" used_m="132.15"/>\n'
    "  </plate>\n"
    "</config>\n"
)


def _make_3mf(gcode=_PLATE_GCODE, plate_id=1, slice_info=None):
    fd, name = tempfile.mkstemp(suffix=".3mf")
    os.close(fd)
    path = Path(name)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"Metadata/plate_{plate_id}.gcode", gcode)
        zf.writestr(f"Metadata/plate_{plate_id}.gcode.md5", "STALE")
        if slice_info is not None:
            zf.writestr("Metadata/slice_info.config", slice_info)
        zf.writestr("3D/3dmodel.model", "<model/>")
    return path


def _profile(**overrides):
    defaults = {
        "name": "pp",
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
        "max_part_height_mm": 42.0,
    }
    defaults.update(overrides)
    return EjectProfile(**defaults)


def _read_plate_gcode(path, plate_id=1):
    with zipfile.ZipFile(path, "r") as zf:
        return zf.read(f"Metadata/plate_{plate_id}.gcode").decode("utf-8")


class TestBuildPartPresentEjectFile:
    @pytest.mark.asyncio
    async def test_content_is_part_present_motion_only(self):
        src = _make_3mf()
        out = None
        try:
            out = (await build_part_present_eject_file(src, 1, _profile(), H2S_GEOMETRY)).path
            gcode = _read_plate_gcode(out)
        finally:
            src.unlink(missing_ok=True)
            if out:
                out.unlink(missing_ok=True)

        # Re-engage motors + home X/Y ONLY — relies on the retained Z datum.
        assert "M17" in gcode
        assert re.search(r"^G28 X Y\b", gcode, re.MULTILINE)
        # NEVER a bare G28 (would home Z into the part) — the original print's
        # G28 must have been replaced entirely by the eject block.
        assert not re.search(r"^G28\s*$", gcode, re.MULTILINE)
        assert not re.search(r"^G28 .*Z", gcode, re.MULTILINE)
        # Motion-only: bed heater off, NO in-file cooldown wait (that moved to the
        # eject monitor, which gates this dispatch on the live bed already).
        assert "M140 S0" in gcode
        assert "M190" not in gcode
        # Self-completing: the stock machine-end FINISH epilogue is appended.
        assert "M18" in gcode
        assert "M73 P100 R0" in gcode
        # Sweep + park markers present.
        assert "FARM EJECT BLOCK" in gcode
        # The original print body is gone (fully replaced).
        assert "E1" not in gcode

    @pytest.mark.asyncio
    async def test_md5_sidecar_recomputed(self):
        src = _make_3mf()
        out = None
        try:
            out = (await build_part_present_eject_file(src, 1, _profile(), H2S_GEOMETRY)).path
            with zipfile.ZipFile(out, "r") as zf:
                gcode_bytes = zf.read("Metadata/plate_1.gcode")
                md5 = zf.read("Metadata/plate_1.gcode.md5").decode("ascii")
        finally:
            src.unlink(missing_ok=True)
            if out:
                out.unlink(missing_ok=True)
        import hashlib

        assert md5 == hashlib.md5(gcode_bytes, usedforsecurity=False).hexdigest().upper()
        assert md5 != "STALE"

    @pytest.mark.asyncio
    async def test_missing_header_raises(self):
        src = _make_3mf("; EXECUTABLE_BLOCK_START\nG1 X1\n; EXECUTABLE_BLOCK_END\n")
        try:
            with pytest.raises(EjectGenerationError):
                await build_part_present_eject_file(src, 1, _profile(), H2S_GEOMETRY)
        finally:
            src.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_built_artifact_reports_zero_slice_usage(self):
        # The motion-only eject file extrudes nothing. The one-pass build copies the
        # donor's slice_info.config verbatim EXCEPT for its usage figures, which it
        # zeroes — otherwise the archive parser / usage tracker / queue card book
        # the donor's ~407 g and 16735 s against a sweep that used none.
        src = _make_3mf(slice_info=_SLICE_INFO_NONZERO)
        out = None
        try:
            # Sanity: the donor really does advertise the usage that would ride along.
            donor_slots = extract_filament_usage_from_3mf(src, plate_id=1)
            assert any(s["used_g"] > 0 for s in donor_slots)
            assert extract_print_time_from_3mf(src, plate_id=1) == 16735

            out = (await build_part_present_eject_file(src, 1, _profile(), H2S_GEOMETRY)).path
            slots = extract_filament_usage_from_3mf(out, plate_id=1)
            prediction = extract_print_time_from_3mf(out, plate_id=1)
        finally:
            src.unlink(missing_ok=True)
            if out:
                out.unlink(missing_ok=True)

        # filament_used_grams == 0 and print_time_seconds == 0 in the built artifact.
        assert all(s["used_g"] == 0 for s in slots)
        assert prediction == 0

    @pytest.mark.asyncio
    async def test_returns_path_plus_runtime_estimate(self):
        # The build is the ONLY place the expected runtime is derivable (it needs the
        # exact block generated for this part height / profile / geometry), and it is
        # consumed much later at the eject's terminal — so it travels with the
        # artifact rather than being re-derived from a file that is deleted by then.
        src = _make_3mf()
        built = None
        try:
            built = await build_part_present_eject_file(src, 1, _profile(), H2S_GEOMETRY)
            assert isinstance(built, BuiltEject)
            assert built.path.exists()
            # The estimate is of the EJECT BLOCK — what actually replaced the plate
            # G-code — so it must exceed the fixed overhead by the sweep's motion.
            assert built.expected_runtime_s > EJECT_RUNTIME_OVERHEAD_S
            assert built.expected_runtime_s == estimate_runtime_s(_read_plate_gcode(built.path))
        finally:
            src.unlink(missing_ok=True)
            if built:
                built.path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_h2c_dual_nozzle_home_flows_through_shared_path(self):
        # The part-present builder flows through the SAME generator + validator as
        # production injection: an H2C build must carry the dual-nozzle
        # parameterized homes (007-H2C stall-loop incident, 2026-07-12) and pass
        # the dual-aware validation the builder runs internally (a validation
        # failure would have raised EjectGenerationError).
        src = _make_3mf()
        out = None
        try:
            out = (await build_part_present_eject_file(src, 1, _profile(), H2C_GEOMETRY)).path
            gcode = _read_plate_gcode(out)
        finally:
            src.unlink(missing_ok=True)
            if out:
                out.unlink(missing_ok=True)

        lines = [ln.strip() for ln in gcode.splitlines()]
        for home in DUAL_NOZZLE_HOME:
            assert home in lines
        # The single-nozzle form is gone, along with any bare / Z home.
        assert "G28 X Y" not in lines
        assert not re.search(r"^G28\s*$", gcode, re.MULTILINE)
        assert not any(ln.startswith("G28") and " Z" in ln for ln in lines)
        # Standard block content still present (motion-only, markers, epilogue).
        assert "M140 S0" in gcode
        assert "M190" not in gcode
        assert "FARM EJECT BLOCK" in gcode


class TestBuiltEjectDropSpan:
    """The bed-drop phase budget the build hands to the runtime watchdog.

    It arms the watchdog's edge lane, which stops a drop that overruns BEFORE the sweep
    can touch the plate — the class of stall too short to overrun the whole job (~59 s
    on the production profile) and therefore invisible to the total deadline alone."""

    @pytest.mark.asyncio
    async def test_populated_only_when_the_profile_drops_the_bed(self):
        src = _make_3mf()
        built = dropless = None
        try:
            built = await build_part_present_eject_file(src, 1, _profile(bed_drop_clearance_mm=50.0), H2S_GEOMETRY)
            dropless = await build_part_present_eject_file(src, 1, _profile(), H2S_GEOMETRY)

            # With the assist on: the block's own P5→P50 span. The donor's part is
            # 18 mm, so the lift is 28 and the drop floor 340-50=290 → 524 mm at F900.
            assert built.drop_span_s == pytest.approx(2 * (290.0 - 28.0) / 900.0 * 60.0, abs=0.01)
            assert built.drop_span_s == pytest.approx(
                estimate_runtime_segments(_read_plate_gcode(built.path)).drop_span_s
            )
            # Without it, the pre-sweep motion is just the prologue lift, which cannot
            # stall against an under-bed obstruction — so the edge lane stays disarmed
            # rather than timing a ~0 s phase.
            assert dropless.drop_span_s is None
        finally:
            src.unlink(missing_ok=True)
            for artifact in (built, dropless):
                if artifact:
                    artifact.path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_span_is_a_strict_part_of_the_expected_runtime(self):
        src = _make_3mf()
        built = None
        try:
            built = await build_part_present_eject_file(src, 1, _profile(bed_drop_clearance_mm=50.0), H2S_GEOMETRY)
            assert 0 < built.drop_span_s < built.expected_runtime_s
            assert built.expected_runtime_s == estimate_runtime_s(_read_plate_gcode(built.path))
        finally:
            src.unlink(missing_ok=True)
            if built:
                built.path.unlink(missing_ok=True)
