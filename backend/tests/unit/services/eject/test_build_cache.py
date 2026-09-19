"""Tests for the eject lane's FACADE onto the generic derived-3MF cache.

The mechanism (keys, eviction, locking, atomic install, caller-owned copies) is tested
once, in ``unit/services/test_derived_3mf_cache.py``. What is tested HERE is only what
the facade itself owns: its unchanged signature and return type, the namespace / cap /
builder it binds, the ``cache_dir`` passthrough, and the mapping of the core's
``BuildFailed`` result onto the ``EjectBuildError`` its callers already catch.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from backend.app.services.derived_3mf_cache import BuildFailed, Derived
from backend.app.services.eject import build_cache
from backend.app.services.eject.build_cache import EjectBuildError, get_or_build_eject_file

pytestmark = pytest.mark.asyncio

_PLATE_GCODE = "; HEADER_BLOCK_START\n; max_z_height: 18.00\n; HEADER_BLOCK_END\nG1 X1 Y1\n"
_EJECT_GCODE = "; EXECUTABLE_BLOCK_START\nM17\nG28 X Y\n; EXECUTABLE_BLOCK_END\n"


def _make_source(tmp_path: Path, name: str = "src.gcode.3mf") -> Path:
    path = tmp_path / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Metadata/plate_1.gcode", _PLATE_GCODE)
        zf.writestr("Metadata/plate_2.gcode", _PLATE_GCODE)
        zf.writestr("3D/3dmodel.model", "<model/>")
    return path


def _read_plate(path: Path, plate_id: int = 1) -> bytes:
    with zipfile.ZipFile(path, "r") as zf:
        return zf.read(f"Metadata/plate_{plate_id}.gcode")


async def test_returns_a_caller_owned_path_to_the_built_eject_file(tmp_path):
    """The signature and contract callers depend on: a bare ``Path``, freshly copied,
    safe to unlink, carrying the eject G-code on the requested plate."""
    cache = tmp_path / "cache"
    src = _make_source(tmp_path)

    out = await get_or_build_eject_file(src, 2, _EJECT_GCODE, cache_dir=cache)

    assert isinstance(out, Path)
    assert _read_plate(out, 2) == _EJECT_GCODE.encode("utf-8")
    cached = list(cache.glob("*.3mf"))
    assert len(cached) == 1
    out.unlink()
    assert cached[0].exists()


async def test_build_failure_becomes_an_eject_build_error(tmp_path):
    """A donor with NO gcode member at all: the core answers ``BuildFailed`` and the
    facade must raise the error its two callers catch, naming plate and donor."""
    cache = tmp_path / "cache"
    src = tmp_path / "no_gcode.3mf"
    with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")

    with pytest.raises(EjectBuildError) as excinfo:
        await get_or_build_eject_file(src, 1, _EJECT_GCODE, cache_dir=cache)

    message = str(excinfo.value)
    assert "plate 1" in message
    assert "no_gcode.3mf" in message


async def test_binds_the_eject_namespace_cap_builder_and_cache_dir(tmp_path, monkeypatch):
    """Everything the facade exists to decide, in one place."""
    cache = tmp_path / "cache"
    src = _make_source(tmp_path)
    seen: dict[str, object] = {}

    async def _spy(source_path, plate_id, fingerprint, builder, **kwargs):
        seen.update(source_path=source_path, plate_id=plate_id, fingerprint=fingerprint, builder=builder, **kwargs)
        built = builder()
        assert built is not None
        return Derived(built)

    monkeypatch.setattr(build_cache, "get_or_build", _spy)

    out = await get_or_build_eject_file(src, 2, _EJECT_GCODE, cache_dir=cache)

    assert seen["source_path"] == src
    assert seen["plate_id"] == 2
    # The eject lane's fingerprint IS the eject G-code text.
    assert seen["fingerprint"] == _EJECT_GCODE
    assert seen["namespace"] == "eject_cache"
    assert seen["max_bytes"] == 64 * 1024 * 1024
    assert seen["cache_dir"] == cache
    # ...and the bound builder is the one-pass eject repack of THAT plate.
    assert _read_plate(out, 2) == _EJECT_GCODE.encode("utf-8")
    out.unlink(missing_ok=True)


async def test_cache_dir_defaults_to_the_namespace_directory(tmp_path, monkeypatch):
    """Omitting ``cache_dir`` passes ``None`` through, so the core resolves
    ``<data dir>/eject_cache`` — the facade never computes a path of its own."""
    src = _make_source(tmp_path)
    seen: dict[str, object] = {}

    async def _spy(source_path, plate_id, fingerprint, builder, **kwargs):
        seen.update(kwargs)
        return BuildFailed("stubbed")

    monkeypatch.setattr(build_cache, "get_or_build", _spy)

    with pytest.raises(EjectBuildError):
        await get_or_build_eject_file(src, 1, _EJECT_GCODE)

    assert seen["cache_dir"] is None
    assert seen["namespace"] == "eject_cache"
