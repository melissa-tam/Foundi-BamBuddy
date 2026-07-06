"""Tests for the scheduler dispatch helper (eject end-snippet supersede logic)."""

import os
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.models.eject_profile import EjectProfile
from backend.app.services.eject.dispatch import build_eject_snippet

_PLATE_GCODE = (
    "; HEADER_BLOCK_START\n"
    "; BambuStudio 02.07.01.57\n"
    "; max_z_height: 20.00\n"
    "; HEADER_BLOCK_END\n"
    "; EXECUTABLE_BLOCK_START\n"
    "G1 X1 Y1\n"
    "; EXECUTABLE_BLOCK_END\n"
)

_PLATE_GCODE_NO_HEADER = "; EXECUTABLE_BLOCK_START\nG1 X1 Y1\n; EXECUTABLE_BLOCK_END\n"


def _make_3mf(gcode: str = _PLATE_GCODE, plate_id: int = 1) -> Path:
    fd, name = tempfile.mkstemp(suffix=".3mf")
    os.close(fd)
    path = Path(name)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"Metadata/plate_{plate_id}.gcode", gcode)
        zf.writestr("3D/3dmodel.model", "<model/>")
    return path


async def _add_profile(db_session, **overrides) -> EjectProfile:
    defaults = {
        "name": "disp",
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
    profile = EjectProfile(**defaults)
    db_session.add(profile)
    await db_session.commit()
    await db_session.refresh(profile)
    return profile


@pytest.mark.asyncio
class TestBuildEjectSnippet:
    async def test_success_returns_validated_block(self, db_session):
        profile = await _add_profile(db_session)
        item = SimpleNamespace(eject_profile_id=profile.id, plate_id=1, batch_id=None)
        printer = SimpleNamespace(model="H2S")
        source = _make_3mf()
        try:
            snippet, error = await build_eject_snippet(db_session, item, printer, source)
        finally:
            source.unlink(missing_ok=True)
        assert error is None
        # The returned snippet is what SUPERSEDES the global end snippet.
        assert snippet is not None
        assert snippet.startswith("; ===== FARM EJECT BLOCK profile=disp =====")
        assert snippet.count("M190 R28") == 5

    async def test_first_article_skips_injection(self, db_session):
        # First-article items must NOT get an eject block — the part stays on the
        # plate for inspection. build_eject_snippet returns (None, None): a skip,
        # not an error.
        profile = await _add_profile(db_session, name="disp-fa")
        item = SimpleNamespace(
            eject_profile_id=profile.id, plate_id=1, batch_id=None, first_article=True
        )
        printer = SimpleNamespace(model="H2S")
        source = _make_3mf()
        try:
            snippet, error = await build_eject_snippet(db_session, item, printer, source)
        finally:
            source.unlink(missing_ok=True)
        assert snippet is None
        assert error is None

    async def test_profile_not_found(self, db_session):
        item = SimpleNamespace(eject_profile_id=999999, plate_id=1, batch_id=None)
        printer = SimpleNamespace(model="H2S")
        source = _make_3mf()
        try:
            snippet, error = await build_eject_snippet(db_session, item, printer, source)
        finally:
            source.unlink(missing_ok=True)
        assert snippet is None
        assert error and "not found" in error

    async def test_unknown_model(self, db_session):
        profile = await _add_profile(db_session, name="disp-unknown")
        item = SimpleNamespace(eject_profile_id=profile.id, plate_id=1, batch_id=None)
        printer = SimpleNamespace(model="X1C")
        source = _make_3mf()
        try:
            snippet, error = await build_eject_snippet(db_session, item, printer, source)
        finally:
            source.unlink(missing_ok=True)
        assert snippet is None
        assert error and "bed geometry" in error

    async def test_missing_max_z(self, db_session):
        profile = await _add_profile(db_session, name="disp-nohdr")
        item = SimpleNamespace(eject_profile_id=profile.id, plate_id=1, batch_id=None)
        printer = SimpleNamespace(model="H2S")
        source = _make_3mf(_PLATE_GCODE_NO_HEADER)
        try:
            snippet, error = await build_eject_snippet(db_session, item, printer, source)
        finally:
            source.unlink(missing_ok=True)
        assert snippet is None
        assert error and "max_z_height" in error

    async def test_tall_part_refused(self, db_session):
        # Part height (20) fits, but shrink the guard so generation refuses.
        profile = await _add_profile(db_session, name="disp-tall", max_part_height_mm=10.0)
        item = SimpleNamespace(eject_profile_id=profile.id, plate_id=1, batch_id=None)
        printer = SimpleNamespace(model="H2S")
        source = _make_3mf()  # header max_z_height: 20.00
        try:
            snippet, error = await build_eject_snippet(db_session, item, printer, source)
        finally:
            source.unlink(missing_ok=True)
        assert snippet is None
        assert error and "generation refused" in error.lower()

    async def test_batch_cooldown_override_supersedes_profile(self, db_session):
        # A production run's cooldown_temp_c_override flows through dispatch:
        # the emitted M190 R waits use the override, not the profile's 28.
        from backend.app.models.print_batch import PrintBatch

        profile = await _add_profile(db_session, name="disp-override")
        batch = PrintBatch(name="run", quantity=1, status="active", cooldown_temp_c_override=22.0)
        db_session.add(batch)
        await db_session.commit()
        await db_session.refresh(batch)

        item = SimpleNamespace(eject_profile_id=profile.id, plate_id=1, batch_id=batch.id)
        printer = SimpleNamespace(model="H2S")
        source = _make_3mf()
        try:
            snippet, error = await build_eject_snippet(db_session, item, printer, source)
        finally:
            source.unlink(missing_ok=True)
        assert error is None
        assert snippet is not None
        assert snippet.count("M190 R22") == 5
        assert "M190 R28" not in snippet
