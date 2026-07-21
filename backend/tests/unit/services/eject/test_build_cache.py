"""Tests for the off-loop, content-addressed eject build cache (latency Phase C2)."""

from __future__ import annotations

import asyncio
import os
import zipfile
from pathlib import Path

import pytest

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


async def test_miss_then_hit_same_inputs(tmp_path):
    cache = tmp_path / "cache"
    src = _make_source(tmp_path)

    out1 = await get_or_build_eject_file(src, 1, _EJECT_GCODE, cache_dir=cache)
    # Exactly one cached artifact after the miss.
    cached = list(cache.glob("*.3mf"))
    assert len(cached) == 1
    assert _read_plate(out1) == _EJECT_GCODE.encode("utf-8")

    out2 = await get_or_build_eject_file(src, 1, _EJECT_GCODE, cache_dir=cache)
    # A HIT reuses the same single cache entry (no new artifact) and returns a
    # DISTINCT caller-owned copy.
    assert len(list(cache.glob("*.3mf"))) == 1
    assert out1 != out2
    assert out2 != cached[0]
    assert _read_plate(out2) == _EJECT_GCODE.encode("utf-8")

    out1.unlink(missing_ok=True)
    out2.unlink(missing_ok=True)


async def test_returned_path_is_copy_not_cache_file(tmp_path):
    cache = tmp_path / "cache"
    src = _make_source(tmp_path)

    out = await get_or_build_eject_file(src, 1, _EJECT_GCODE, cache_dir=cache)
    cached = list(cache.glob("*.3mf"))
    assert len(cached) == 1
    # Unlinking the returned path must NOT remove the cache entry (contract: callers
    # unlink what they are handed).
    out.unlink()
    assert cached[0].exists()

    # The next call is still a hit off the surviving cache file.
    out2 = await get_or_build_eject_file(src, 1, _EJECT_GCODE, cache_dir=cache)
    assert len(list(cache.glob("*.3mf"))) == 1
    out2.unlink(missing_ok=True)


async def test_gcode_change_is_a_new_key(tmp_path):
    cache = tmp_path / "cache"
    src = _make_source(tmp_path)

    o1 = await get_or_build_eject_file(src, 1, _EJECT_GCODE, cache_dir=cache)
    o2 = await get_or_build_eject_file(src, 1, _EJECT_GCODE + "M18\n", cache_dir=cache)
    # Distinct gcode → distinct cache entries.
    assert len(list(cache.glob("*.3mf"))) == 2
    o1.unlink(missing_ok=True)
    o2.unlink(missing_ok=True)


async def test_plate_change_is_a_new_key(tmp_path):
    cache = tmp_path / "cache"
    src = _make_source(tmp_path)

    o1 = await get_or_build_eject_file(src, 1, _EJECT_GCODE, cache_dir=cache)
    o2 = await get_or_build_eject_file(src, 2, _EJECT_GCODE, cache_dir=cache)
    assert len(list(cache.glob("*.3mf"))) == 2
    o1.unlink(missing_ok=True)
    o2.unlink(missing_ok=True)


async def test_source_mtime_change_is_a_new_key(tmp_path):
    cache = tmp_path / "cache"
    src = _make_source(tmp_path)

    o1 = await get_or_build_eject_file(src, 1, _EJECT_GCODE, cache_dir=cache)
    # Bump the donor's mtime (an edit / re-slice) — same size, new key.
    st = src.stat()
    os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    o2 = await get_or_build_eject_file(src, 1, _EJECT_GCODE, cache_dir=cache)
    assert len(list(cache.glob("*.3mf"))) == 2
    o1.unlink(missing_ok=True)
    o2.unlink(missing_ok=True)


async def test_lru_evicts_oldest_beyond_cap(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    monkeypatch.setattr(build_cache, "_LRU_MAX", 3)
    src = _make_source(tmp_path)

    outs = []
    for i in range(5):
        # Distinct gcode text → distinct keys → 5 inserts against a cap of 3.
        out = await get_or_build_eject_file(src, 1, _EJECT_GCODE + f"; n{i}\n", cache_dir=cache)
        outs.append(out)
        # Space mtimes so eviction order is deterministic.
        await asyncio.sleep(0.01)

    remaining = list(cache.glob("*.3mf"))
    assert len(remaining) == 3
    for out in outs:
        out.unlink(missing_ok=True)


async def test_no_gcode_member_raises(tmp_path):
    # A donor with NO gcode member at all → EjectBuildError (a specific-plate miss
    # falls back to the first gcode, so this needs a gcode-less donor).
    cache = tmp_path / "cache"
    src = tmp_path / "no_gcode.3mf"
    with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")
    with pytest.raises(EjectBuildError):
        await get_or_build_eject_file(src, 1, _EJECT_GCODE, cache_dir=cache)


async def test_concurrent_same_key_both_succeed(tmp_path):
    cache = tmp_path / "cache"
    src = _make_source(tmp_path)

    results = await asyncio.gather(*[get_or_build_eject_file(src, 1, _EJECT_GCODE, cache_dir=cache) for _ in range(6)])
    # Every concurrent caller got a valid, distinct, correctly-built artifact...
    assert len({str(p) for p in results}) == 6
    for out in results:
        assert _read_plate(out) == _EJECT_GCODE.encode("utf-8")
    # ...and the cache converged to a single entry for the shared key.
    assert len(list(cache.glob("*.3mf"))) == 1
    for out in results:
        out.unlink(missing_ok=True)
