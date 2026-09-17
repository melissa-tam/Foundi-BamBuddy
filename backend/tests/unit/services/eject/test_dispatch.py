"""Dispatch-side guards for the (now server-dispatched) eject pipeline.

The eject sweep is a SEPARATE motion-only job — the scheduler no longer injects
any eject block into a print file at dispatch, so an item carrying an
``eject_profile_id`` dispatches its source file UNMODIFIED (apart from the
upstream global per-model start/end snippet, which is unchanged). The eject
end-snippet builder ``build_eject_snippet`` and its scheduler branch were deleted.
"""

import inspect
import os
import tempfile
import zipfile
from pathlib import Path

import pytest

from backend.app.models.eject_profile import EjectProfile
from backend.app.services import print_scheduler as ps_module
from backend.app.services.eject import dispatch as dispatch_mod
from backend.app.services.eject.dispatch import build_part_present_eject_file
from backend.app.services.eject.generator import EjectGenerationError
from backend.tests.unit.services.eject.geometry_fixtures import H2S_GEOMETRY

_PLATE_GCODE = "; HEADER_BLOCK_START\n; max_z_height: 18.00\n; HEADER_BLOCK_END\nG28\nG1 X10 Y10 E1\n"


def _donor_3mf(plates: list[int]) -> Path:
    """A synthetic donor carrying a G-code member (+ MD5 sidecar) for ``plates``."""
    fd, name = tempfile.mkstemp(suffix=".gcode.3mf")
    os.close(fd)
    path = Path(name)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for plate in plates:
            zf.writestr(f"Metadata/plate_{plate}.gcode", _PLATE_GCODE)
            zf.writestr(f"Metadata/plate_{plate}.gcode.md5", "STALE")
        zf.writestr("3D/3dmodel.model", "<model/>")
    return path


def _profile(**overrides) -> EjectProfile:
    defaults = {
        "name": "pp",
        "cooldown_temp_c": 33.0,
        "clearance_mm": 10.0,
        "z_offset_mm": 0.4,
        "descent_steps": 4,
        "x_passes": 11,
        "x_margin_mm": 3.0,
        "front_overhang_mm": 2.0,
        "back_overhang_mm": 2.0,
        "eject_speed_mm_min": 3000,
        "skim_speed_mm_min": 1500,
        "max_part_height_mm": 42.0,
    }
    defaults.update(overrides)
    return EjectProfile(**defaults)


def test_build_eject_snippet_is_deleted():
    """The eject end-snippet builder is gone (its scheduler injection branch too)."""
    assert not hasattr(dispatch_mod, "build_eject_snippet")


def test_dispatch_module_surface_is_motion_only():
    """dispatch.py keeps only the motion-only builder + the cooldown-override
    resolver the eject MONITOR reads; nothing generates an injectable snippet."""
    assert hasattr(dispatch_mod, "build_part_present_eject_file")
    assert hasattr(dispatch_mod, "resolve_cooldown_override")
    # build_part_present_eject_file is motion-only now: no cooldown_temp_c param.
    params = inspect.signature(dispatch_mod.build_part_present_eject_file).parameters
    assert "cooldown_temp_c" not in params


def test_scheduler_has_no_eject_injection_branch():
    """The scheduler's dispatch body no longer builds/superseded an eject block.

    Regression guard for the deleted branch: an ``eject_profile_id`` item flows
    through the SAME global-snippet-only injection path as any other item, so its
    print file is dispatched unmodified (no eject-block supersede, no eject repack).
    Asserted at the source level because driving the full ``_start_print`` would
    exercise many orthogonal farm gates (capability/USB/archive/FTP/MQTT); this
    directly pins the one behaviour under test.
    """
    src = inspect.getsource(ps_module.PrintScheduler._start_print)
    assert "build_eject_snippet" not in src
    assert "eject_snippet" not in src
    # The eject-profile branch that superseded the machine-end snippet is gone;
    # the only injection is the upstream global per-model snippet flow.
    assert "supersede the global end snippet" not in src
    assert "auto-eject block generated from profile" not in src


class TestPackedPlateIsTheCommandedPlate:
    """The plate the build PACKS is the plate the dispatcher COMMANDS — one value.

    On 2026-09-17 they were two: the builder resolved a G-code member with a
    first-member fallback while ``eject/remote.py`` re-read ``item.plate_id or 1`` for
    the firmware's ``project_file``. A donor that did not carry the unit's plate
    therefore produced a container with the sweep in ``plate_1.gcode`` and a command
    naming ``Metadata/plate_3.gcode`` — unparseable (005-H2S, HMS ``0500_0003`` /
    ``0500_4003``). ``BuiltEject.plate_id`` is now the single value, and the build
    refuses outright when the donor cannot supply the plate.

    The end-to-end half — that ``start_print`` is called with ``built.plate_id`` — is
    asserted in ``test_remote.py``, which owns the dispatch harness.
    """

    @pytest.mark.asyncio
    async def test_a_donor_without_the_plate_refuses_and_names_it(self):
        """A single-plate donor asked for plate 3 raises, naming plate 3.

        The refusal text reaches the operator through the eject route's existing 409,
        and it names the DONOR — because the remedy is a different source file, never a
        different eject profile."""
        src = _donor_3mf([1])
        try:
            with pytest.raises(EjectGenerationError) as exc:
                await build_part_present_eject_file(src, 3, _profile(), H2S_GEOMETRY)
        finally:
            src.unlink(missing_ok=True)
        message = str(exc.value)
        assert "plate 3" in message, message
        assert src.name in message, message

    @pytest.mark.asyncio
    async def test_the_build_reports_the_plate_it_packed(self):
        """LIVENESS pair: the multi-plate donor still builds, on the asked-for plate.

        The built container carries the sweep in ``plate_3.gcode`` — and nowhere else —
        and ``BuiltEject.plate_id`` is that same 3."""
        src = _donor_3mf([1, 2, 3, 4])
        out = None
        try:
            built = await build_part_present_eject_file(src, 3, _profile(), H2S_GEOMETRY)
            out = built.path
            assert built.plate_id == 3
            with zipfile.ZipFile(out, "r") as zf:
                packed = zf.read("Metadata/plate_3.gcode").decode("utf-8")
                untouched = zf.read("Metadata/plate_1.gcode").decode("utf-8")
            assert "FARM EJECT BLOCK" in packed
            assert "FARM EJECT BLOCK" not in untouched, "only the commanded plate is rewritten"
        finally:
            src.unlink(missing_ok=True)
            if out is not None:
                out.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_an_override_height_does_not_rescue_an_absent_plate(self):
        """The foreign lane supplies ``max_z_override``, so the header read is skipped —
        the repack still has no member to replace, and the same refusal is raised."""
        src = _donor_3mf([1])
        try:
            with pytest.raises(EjectGenerationError) as exc:
                await build_part_present_eject_file(src, 2, _profile(), H2S_GEOMETRY, max_z_override=20.0)
        finally:
            src.unlink(missing_ok=True)
        assert "plate 2" in str(exc.value)
