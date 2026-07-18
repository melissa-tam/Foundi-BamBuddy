"""Tests for the part-present eject-only file builder (remote first-article eject)."""

import os
import re
import tempfile
import zipfile
from pathlib import Path

import pytest

from backend.app.models.eject_profile import EjectProfile
from backend.app.services.eject.dispatch import build_part_present_eject_file
from backend.app.services.eject.generator import EjectGenerationError
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
    def test_content_is_part_present_motion_only(self):
        src = _make_3mf()
        out = None
        try:
            out = build_part_present_eject_file(src, 1, _profile(), H2S_GEOMETRY)
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

    def test_md5_sidecar_recomputed(self):
        src = _make_3mf()
        out = None
        try:
            out = build_part_present_eject_file(src, 1, _profile(), H2S_GEOMETRY)
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

    def test_missing_header_raises(self):
        src = _make_3mf("; EXECUTABLE_BLOCK_START\nG1 X1\n; EXECUTABLE_BLOCK_END\n")
        try:
            with pytest.raises(EjectGenerationError):
                build_part_present_eject_file(src, 1, _profile(), H2S_GEOMETRY)
        finally:
            src.unlink(missing_ok=True)

    def test_built_artifact_reports_zero_slice_usage(self):
        # The motion-only eject file extrudes nothing. repack_3mf_with_gcode copies
        # the donor's slice_info.config verbatim, so the builder must additionally
        # zero it — otherwise the archive parser / usage tracker / queue card book
        # the donor's ~407 g and 16735 s against a sweep that used none.
        src = _make_3mf(slice_info=_SLICE_INFO_NONZERO)
        out = None
        try:
            # Sanity: the donor really does advertise the usage that would ride along.
            donor_slots = extract_filament_usage_from_3mf(src, plate_id=1)
            assert any(s["used_g"] > 0 for s in donor_slots)
            assert extract_print_time_from_3mf(src, plate_id=1) == 16735

            out = build_part_present_eject_file(src, 1, _profile(), H2S_GEOMETRY)
            slots = extract_filament_usage_from_3mf(out, plate_id=1)
            prediction = extract_print_time_from_3mf(out, plate_id=1)
        finally:
            src.unlink(missing_ok=True)
            if out:
                out.unlink(missing_ok=True)

        # filament_used_grams == 0 and print_time_seconds == 0 in the built artifact.
        assert all(s["used_g"] == 0 for s in slots)
        assert prediction == 0

    def test_h2c_dual_nozzle_home_flows_through_shared_path(self):
        # The part-present builder flows through the SAME generator + validator as
        # production injection: an H2C build must carry the dual-nozzle
        # parameterized homes (007-H2C stall-loop incident, 2026-07-12) and pass
        # the dual-aware validation the builder runs internally (a validation
        # failure would have raised EjectGenerationError).
        src = _make_3mf()
        out = None
        try:
            out = build_part_present_eject_file(src, 1, _profile(), H2C_GEOMETRY)
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
