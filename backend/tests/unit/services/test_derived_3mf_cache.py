"""Tests for the generic off-loop, content-addressed derived-3MF cache.

The mechanism tests live HERE (they moved off the eject facade with the mechanism):
key sensitivity, caller-owned copies, atomic install, byte-bounded eviction, the
per-namespace one-time sweep, lock pruning and the failure results. The builder is a
trivial file-writer — this module knows nothing about 3MF internals, and neither
should its tests.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

from backend.app.services import derived_3mf_cache
from backend.app.services.derived_3mf_cache import BuildFailed, Derived, get_or_build

pytestmark = pytest.mark.asyncio

_BIG_CAP = 64 * 1024 * 1024


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Both process-wide registries are per-namespace, so a namespace reused across
    tests would otherwise inherit the previous test's 'already swept' verdict."""
    derived_3mf_cache._build_locks.clear()
    derived_3mf_cache._swept_namespaces.clear()
    yield
    derived_3mf_cache._build_locks.clear()
    derived_3mf_cache._swept_namespaces.clear()


def _make_source(tmp_path: Path, name: str = "donor.gcode.3mf", payload: bytes = b"donor") -> Path:
    """The donor is only ever stat()ed by the cache — its bytes are the builder's business."""
    path = tmp_path / name
    path.write_bytes(payload)
    return path


def _builder(payload: bytes, calls: list[bytes] | None = None) -> Callable[[], Path | None]:
    """A builder that writes ``payload`` to a fresh system-temp file, as the contract
    requires of every real builder, and records the call when ``calls`` is given."""

    def _build() -> Path:
        if calls is not None:
            calls.append(payload)
        fd, name = tempfile.mkstemp(suffix=".3mf")
        os.close(fd)
        built = Path(name)
        built.write_bytes(payload)
        return built

    return _build


def _seed_junk(cache_dir: Path, name: str, size: int, mtime: int) -> Path:
    path = cache_dir / name
    path.write_bytes(b"x" * size)
    os.utime(path, (mtime, mtime))
    return path


async def test_miss_then_hit_same_inputs(tmp_path):
    cache = tmp_path / "cache"
    src = _make_source(tmp_path)
    calls: list[bytes] = []

    first = await get_or_build(
        src, 1, "fp", _builder(b"ARTIFACT", calls), namespace="ns", max_bytes=_BIG_CAP, cache_dir=cache
    )
    assert isinstance(first, Derived)
    assert len(list(cache.glob("*.3mf"))) == 1
    assert first.path.read_bytes() == b"ARTIFACT"

    second = await get_or_build(
        src, 1, "fp", _builder(b"ARTIFACT", calls), namespace="ns", max_bytes=_BIG_CAP, cache_dir=cache
    )
    assert isinstance(second, Derived)
    # A HIT reuses the single cache entry, never re-runs the builder, and hands back a
    # DISTINCT caller-owned copy.
    assert calls == [b"ARTIFACT"]
    assert len(list(cache.glob("*.3mf"))) == 1
    assert second.path != first.path
    assert second.path.read_bytes() == b"ARTIFACT"

    first.path.unlink(missing_ok=True)
    second.path.unlink(missing_ok=True)


async def test_returned_path_is_a_copy_not_the_cache_file(tmp_path):
    cache = tmp_path / "cache"
    src = _make_source(tmp_path)

    out = await get_or_build(src, 1, "fp", _builder(b"A"), namespace="ns", max_bytes=_BIG_CAP, cache_dir=cache)
    assert isinstance(out, Derived)
    cached = list(cache.glob("*.3mf"))
    assert len(cached) == 1
    assert out.path != cached[0]
    # Unlinking the returned path must NOT remove the cache entry (contract: callers
    # unlink what they are handed).
    out.path.unlink()
    assert cached[0].exists()

    again = await get_or_build(src, 1, "fp", _builder(b"A"), namespace="ns", max_bytes=_BIG_CAP, cache_dir=cache)
    assert isinstance(again, Derived)
    assert len(list(cache.glob("*.3mf"))) == 1
    again.path.unlink(missing_ok=True)


async def test_caller_owned_copy_lands_in_the_system_temp_dir(tmp_path):
    """``bambu_ftp.cleanup_downloaded_3mf`` only deletes under the system temp dir, so
    a hit served from anywhere else could not be reaped by the lane consuming it."""
    cache = tmp_path / "cache"
    src = _make_source(tmp_path)
    system_temp = Path(tempfile.gettempdir()).resolve()

    miss = await get_or_build(src, 1, "fp", _builder(b"A"), namespace="ns", max_bytes=_BIG_CAP, cache_dir=cache)
    hit = await get_or_build(src, 1, "fp", _builder(b"A"), namespace="ns", max_bytes=_BIG_CAP, cache_dir=cache)
    assert isinstance(miss, Derived) and isinstance(hit, Derived)
    assert hit.path.resolve().parent == system_temp

    miss.path.unlink(missing_ok=True)
    hit.path.unlink(missing_ok=True)


async def test_hit_that_loses_a_race_with_eviction_rebuilds(tmp_path, monkeypatch):
    """``exists()`` said yes, the copy said no. A hit is a fast path, never a promise:
    every insert evicts oldest-first to hold the byte cap, so a concurrent dispatch of
    another SKU can delete the very entry being served. Falling through to the build is
    the only answer that still hands the caller a file."""
    cache = tmp_path / "cache"
    src = _make_source(tmp_path)
    calls: list[bytes] = []

    first = await get_or_build(
        src, 1, "fp", _builder(b"ARTIFACT", calls), namespace="ns", max_bytes=_BIG_CAP, cache_dir=cache
    )
    assert isinstance(first, Derived)
    first.path.unlink(missing_ok=True)

    real_copyfile = derived_3mf_cache.shutil.copyfile
    vanished: list[Path] = []

    def _evict_then_copy(source, dest, *args, **kwargs):
        # The eviction lands in the window between the caller's exists() and this copy,
        # and ONLY for the cache file (the install's own copy must still work).
        cached = Path(source)
        if cached.parent == cache and not vanished:
            vanished.append(cached)
            cached.unlink()
        return real_copyfile(source, dest, *args, **kwargs)

    monkeypatch.setattr(derived_3mf_cache.shutil, "copyfile", _evict_then_copy)

    second = await get_or_build(
        src, 1, "fp", _builder(b"ARTIFACT", calls), namespace="ns", max_bytes=_BIG_CAP, cache_dir=cache
    )

    # The caller got a real artifact — from a REBUILD, not from the vanished entry.
    assert vanished, "the eviction never fired — this test is not exercising the race"
    assert isinstance(second, Derived)
    assert second.path.read_bytes() == b"ARTIFACT"
    assert calls == [b"ARTIFACT", b"ARTIFACT"]
    second.path.unlink(missing_ok=True)


async def test_fingerprint_change_is_a_new_key(tmp_path):
    cache = tmp_path / "cache"
    src = _make_source(tmp_path)

    a = await get_or_build(src, 1, "fp-a", _builder(b"A"), namespace="ns", max_bytes=_BIG_CAP, cache_dir=cache)
    b = await get_or_build(src, 1, "fp-b", _builder(b"B"), namespace="ns", max_bytes=_BIG_CAP, cache_dir=cache)
    assert len(list(cache.glob("*.3mf"))) == 2
    assert isinstance(a, Derived) and isinstance(b, Derived)
    assert b.path.read_bytes() == b"B"
    a.path.unlink(missing_ok=True)
    b.path.unlink(missing_ok=True)


async def test_plate_change_is_a_new_key(tmp_path):
    cache = tmp_path / "cache"
    src = _make_source(tmp_path)

    a = await get_or_build(src, 1, "fp", _builder(b"A"), namespace="ns", max_bytes=_BIG_CAP, cache_dir=cache)
    b = await get_or_build(src, 2, "fp", _builder(b"B"), namespace="ns", max_bytes=_BIG_CAP, cache_dir=cache)
    assert len(list(cache.glob("*.3mf"))) == 2
    assert isinstance(a, Derived) and isinstance(b, Derived)
    a.path.unlink(missing_ok=True)
    b.path.unlink(missing_ok=True)


async def test_source_mtime_change_is_a_new_key(tmp_path):
    cache = tmp_path / "cache"
    src = _make_source(tmp_path)

    a = await get_or_build(src, 1, "fp", _builder(b"A"), namespace="ns", max_bytes=_BIG_CAP, cache_dir=cache)
    # Bump the donor's mtime (an edit / re-slice) — same size, new key.
    st = src.stat()
    os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    b = await get_or_build(src, 1, "fp", _builder(b"B"), namespace="ns", max_bytes=_BIG_CAP, cache_dir=cache)
    assert len(list(cache.glob("*.3mf"))) == 2
    assert isinstance(a, Derived) and isinstance(b, Derived)
    a.path.unlink(missing_ok=True)
    b.path.unlink(missing_ok=True)


async def test_source_size_change_is_a_new_key(tmp_path):
    cache = tmp_path / "cache"
    src = _make_source(tmp_path)

    a = await get_or_build(src, 1, "fp", _builder(b"A"), namespace="ns", max_bytes=_BIG_CAP, cache_dir=cache)
    # Rewrite the donor LONGER and restore its mtime: size alone must move the key.
    st = src.stat()
    src.write_bytes(b"donor-but-longer")
    os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns))
    b = await get_or_build(src, 1, "fp", _builder(b"B"), namespace="ns", max_bytes=_BIG_CAP, cache_dir=cache)
    assert len(list(cache.glob("*.3mf"))) == 2
    assert isinstance(a, Derived) and isinstance(b, Derived)
    a.path.unlink(missing_ok=True)
    b.path.unlink(missing_ok=True)


async def test_install_leaves_no_partial_file_in_the_cache(tmp_path):
    """The install is atomic (sibling temp + ``os.replace``) — no residue, and never a
    half-written entry under the final key."""
    cache = tmp_path / "cache"
    src = _make_source(tmp_path)

    out = await get_or_build(src, 1, "fp", _builder(b"A"), namespace="ns", max_bytes=_BIG_CAP, cache_dir=cache)
    assert isinstance(out, Derived)
    assert [p.name for p in cache.iterdir() if p.name.startswith(".")] == []
    cached = list(cache.glob("*.3mf"))
    assert len(cached) == 1
    assert cached[0].read_bytes() == b"A"
    out.path.unlink(missing_ok=True)


async def test_concurrent_same_key_builds_coalesce(tmp_path):
    cache = tmp_path / "cache"
    src = _make_source(tmp_path)
    calls: list[bytes] = []

    results = await asyncio.gather(
        *[
            get_or_build(src, 1, "fp", _builder(b"A", calls), namespace="ns", max_bytes=_BIG_CAP, cache_dir=cache)
            for _ in range(6)
        ]
    )
    # One build served six callers; each got a distinct, valid, caller-owned copy...
    assert len(calls) == 1
    assert all(isinstance(r, Derived) for r in results)
    paths = [r.path for r in results]
    assert len({str(p) for p in paths}) == 6
    for path in paths:
        assert path.read_bytes() == b"A"
    # ...and the cache converged to a single shared entry.
    assert len(list(cache.glob("*.3mf"))) == 1
    for path in paths:
        path.unlink(missing_ok=True)


async def test_byte_cap_evicts_oldest_first(tmp_path):
    """The cap is BYTES, and the victim is the oldest mtime — not the whole cache."""
    cache = tmp_path / "cache"
    cache.mkdir()
    src = _make_source(tmp_path)
    oldest = _seed_junk(cache, "old.3mf", 100_000, 1_700_000_000)
    middle = _seed_junk(cache, "mid.3mf", 100_000, 1_700_000_100)
    newest = _seed_junk(cache, "new.3mf", 100_000, 1_700_000_200)

    out = await get_or_build(src, 1, "fp", _builder(b"A"), namespace="ns", max_bytes=250_000, cache_dir=cache)

    assert isinstance(out, Derived)
    assert not oldest.exists()
    assert middle.exists()
    assert newest.exists()
    # The freshly built artifact is installed alongside the survivors.
    assert len(list(cache.glob("*.3mf"))) == 3
    out.path.unlink(missing_ok=True)


async def test_oversized_artifact_is_served_but_not_installed(tmp_path):
    """An artifact that alone exceeds the cap must not evict the whole namespace on
    every insert — it is handed to the caller and never cached."""
    cache = tmp_path / "cache"
    src = _make_source(tmp_path)

    out = await get_or_build(src, 1, "fp", _builder(b"x" * 5_000), namespace="ns", max_bytes=1_000, cache_dir=cache)

    assert isinstance(out, Derived)
    assert out.path.read_bytes() == b"x" * 5_000
    assert list(cache.glob("*.3mf")) == []
    out.path.unlink(missing_ok=True)


async def test_sweep_runs_once_per_namespace(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    src = _make_source(tmp_path)

    first = await get_or_build(src, 1, "fp", _builder(b"A"), namespace="ns", max_bytes=250_000, cache_dir=cache)
    assert isinstance(first, Derived)
    assert derived_3mf_cache._swept_namespaces == {"ns"}

    # Junk that a sweep WOULD delete (it alone busts the cap), dropped in after the
    # namespace's one-time sweep has run: the next call is a hit and must not sweep.
    late = _seed_junk(cache, "late.3mf", 300_000, 1_700_000_000)
    second = await get_or_build(src, 1, "fp", _builder(b"A"), namespace="ns", max_bytes=250_000, cache_dir=cache)
    assert isinstance(second, Derived)
    assert late.exists()

    # A DIFFERENT namespace sweeps on ITS first use, in its own directory.
    other_cache = tmp_path / "other"
    other_cache.mkdir()
    other_junk = _seed_junk(other_cache, "stale.3mf", 300_000, 1_700_000_000)
    third = await get_or_build(
        src, 1, "fp", _builder(b"A"), namespace="other", max_bytes=250_000, cache_dir=other_cache
    )
    assert isinstance(third, Derived)
    assert not other_junk.exists()

    for out in (first, second, third):
        out.path.unlink(missing_ok=True)


async def test_namespaces_are_isolated_directories(tmp_path, monkeypatch):
    """Identical key inputs in two namespaces are two artifacts in two directories —
    the namespace is the lane's own storage, not a label."""
    from backend.app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    src = _make_source(tmp_path)

    a = await get_or_build(src, 1, "fp", _builder(b"EJECT"), namespace="ns_a", max_bytes=_BIG_CAP)
    b = await get_or_build(src, 1, "fp", _builder(b"DISPATCH"), namespace="ns_b", max_bytes=_BIG_CAP)

    assert isinstance(a, Derived) and isinstance(b, Derived)
    assert len(list((tmp_path / "ns_a").glob("*.3mf"))) == 1
    assert len(list((tmp_path / "ns_b").glob("*.3mf"))) == 1
    # No cross-namespace hit: the second lane got ITS artifact, not the first's.
    assert a.path.read_bytes() == b"EJECT"
    assert b.path.read_bytes() == b"DISPATCH"
    a.path.unlink(missing_ok=True)
    b.path.unlink(missing_ok=True)


async def test_builder_returning_none_is_a_build_failure(tmp_path):
    cache = tmp_path / "cache"
    src = _make_source(tmp_path)

    out = await get_or_build(src, 1, "fp", lambda: None, namespace="ns", max_bytes=_BIG_CAP, cache_dir=cache)

    assert isinstance(out, BuildFailed)
    assert out.detail == "builder produced no file"
    assert list(cache.glob("*.3mf")) == []


async def test_builder_raising_is_a_build_failure_not_an_exception(tmp_path):
    cache = tmp_path / "cache"
    src = _make_source(tmp_path)

    def _boom() -> Path | None:
        raise RuntimeError("repack exploded")

    out = await get_or_build(src, 1, "fp", _boom, namespace="ns", max_bytes=_BIG_CAP, cache_dir=cache)

    assert isinstance(out, BuildFailed)
    assert out.detail == "RuntimeError: repack exploded"
    assert list(cache.glob("*.3mf")) == []


async def test_lock_dict_is_empty_after_a_completed_build(tmp_path):
    """A 24/7 per-dispatch lane would otherwise leak one lock per key forever."""
    cache = tmp_path / "cache"
    src = _make_source(tmp_path)

    out = await get_or_build(src, 1, "fp", _builder(b"A"), namespace="ns", max_bytes=_BIG_CAP, cache_dir=cache)

    assert isinstance(out, Derived)
    assert derived_3mf_cache._build_locks == {}
    out.path.unlink(missing_ok=True)


async def test_lock_dict_is_empty_after_a_failed_build(tmp_path):
    cache = tmp_path / "cache"
    src = _make_source(tmp_path)

    def _boom() -> Path | None:
        raise RuntimeError("repack exploded")

    assert isinstance(
        await get_or_build(src, 1, "fp", _boom, namespace="ns", max_bytes=_BIG_CAP, cache_dir=cache), BuildFailed
    )
    assert isinstance(
        await get_or_build(src, 2, "fp", lambda: None, namespace="ns", max_bytes=_BIG_CAP, cache_dir=cache),
        BuildFailed,
    )
    assert derived_3mf_cache._build_locks == {}
