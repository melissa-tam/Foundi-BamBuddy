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

_PLATE_GCODE = (
    "; HEADER_BLOCK_START\n"
    "; max_z_height: 18.00\n"
    "; HEADER_BLOCK_END\n"
    "; EXECUTABLE_BLOCK_START\n"
    "G28\n"  # the ORIGINAL print homes all axes — must NOT survive into the eject file
    "G1 X10 Y10 E1\n"
    "; EXECUTABLE_BLOCK_END\n"
)


def _make_3mf(gcode=_PLATE_GCODE, plate_id=1):
    fd, name = tempfile.mkstemp(suffix=".3mf")
    os.close(fd)
    path = Path(name)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"Metadata/plate_{plate_id}.gcode", gcode)
        zf.writestr(f"Metadata/plate_{plate_id}.gcode.md5", "STALE")
        zf.writestr("3D/3dmodel.model", "<model/>")
    return path


def _profile(**overrides):
    defaults = {
        "name": "pp",
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
    }
    defaults.update(overrides)
    return EjectProfile(**defaults)


def _read_plate_gcode(path, plate_id=1):
    with zipfile.ZipFile(path, "r") as zf:
        return zf.read(f"Metadata/plate_{plate_id}.gcode").decode("utf-8")


class TestBuildPartPresentEjectFile:
    def test_content_has_part_present_prologue_and_cooldown(self):
        src = _make_3mf()
        out = None
        try:
            out = build_part_present_eject_file(src, 1, _profile(), "H2S")
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
        # Cooldown block: 5 M190 R waits at the threshold, bed heater off.
        assert gcode.count("M190 R28") == 5
        assert "M140 S0" in gcode
        # Sweep + park markers present.
        assert "FARM EJECT BLOCK" in gcode
        # The original print body is gone (fully replaced).
        assert "E1" not in gcode

    def test_md5_sidecar_recomputed(self):
        src = _make_3mf()
        out = None
        try:
            out = build_part_present_eject_file(src, 1, _profile(), "H2S")
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

    def test_cooldown_override_flows_through(self):
        src = _make_3mf()
        out = None
        try:
            out = build_part_present_eject_file(src, 1, _profile(), "H2S", cooldown_temp_c=22.0)
            gcode = _read_plate_gcode(out)
        finally:
            src.unlink(missing_ok=True)
            if out:
                out.unlink(missing_ok=True)
        assert gcode.count("M190 R22") == 5
        assert "M190 R28" not in gcode

    def test_missing_header_raises(self):
        src = _make_3mf("; EXECUTABLE_BLOCK_START\nG1 X1\n; EXECUTABLE_BLOCK_END\n")
        try:
            with pytest.raises(EjectGenerationError):
                build_part_present_eject_file(src, 1, _profile(), "H2S")
        finally:
            src.unlink(missing_ok=True)

    def test_unknown_model_raises(self):
        src = _make_3mf()
        try:
            with pytest.raises(EjectGenerationError):
                build_part_present_eject_file(src, 1, _profile(), "X1C")
        finally:
            src.unlink(missing_ok=True)
