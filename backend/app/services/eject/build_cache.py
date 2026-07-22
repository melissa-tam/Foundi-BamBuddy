"""Off-loop, content-addressed cache for built eject / dry-run ``.gcode.3mf`` files.

The motion-only eject artifact is deterministic in ``(eject gcode text, donor file
bytes, plate id)``: the same finished unit ejected twice — or every unit of a run of
the same SKU — rebuilds a byte-identical archive. Rebuilding it means a full ZIP
read + re-deflate (:func:`repack_3mf_eject`), which is CPU-heavy and, run on the
event loop, stalls every other printer's dispatch (latency Phase C2).

:func:`get_or_build_eject_file` fixes both:

- **Off the loop**: the build runs in a worker thread (``asyncio.to_thread``), never
  on the event loop.
- **Cached**: artifacts are keyed by a sha256 of ``(eject gcode, source size, source
  mtime_ns, plate id)`` and stored under ``<data dir>/eject_cache``; a hit copies the
  cached artifact out instead of rebuilding.

Every caller UNLINKS the path it is returned (the existing eject-dispatch contract),
so both the hit and miss paths hand back a FRESH temp copy that is safe to delete
without harming the cache.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import tempfile
from pathlib import Path

from backend.app.utils.threemf_tools import repack_3mf_eject

logger = logging.getLogger(__name__)

# Keep at most this many built artifacts; the oldest (by mtime) are evicted on
# insert and swept on first use. Eject files are small and highly reusable, so a
# shallow cap bounds disk with no meaningful hit-rate cost.
_LRU_MAX = 20

_CACHE_SUBDIR = "eject_cache"

# Per-key build locks so two concurrent builds of the SAME key don't both burn CPU:
# the second awaits the first, then takes the freshly-populated cache hit.
_build_locks: dict[str, asyncio.Lock] = {}
_swept = False


class EjectBuildError(RuntimeError):
    """The one-pass eject repack produced no file (e.g. the plate has no G-code)."""


def _resolve_cache_dir(cache_dir: Path | None) -> Path:
    """The cache directory, created if needed. Defaults to ``<data dir>/eject_cache``
    off the same ``settings.base_dir`` data root the archive/library storage uses."""
    if cache_dir is None:
        from backend.app.core.config import settings as app_settings

        cache_dir = app_settings.base_dir / _CACHE_SUBDIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _cache_key(source_path: Path, plate_id: int, eject_gcode: str) -> str:
    """sha256 hex over ``(eject gcode, source size, source mtime_ns, plate id)``.

    Source SIZE + MTIME (not a full content hash) key the donor: an edited /
    re-sliced donor changes at least one, invalidating the entry, without paying to
    re-hash a potentially hundreds-of-MB 3MF on every dispatch."""
    st = source_path.stat()
    h = hashlib.sha256()
    for part in (
        eject_gcode.encode("utf-8"),
        str(st.st_size).encode(),
        str(st.st_mtime_ns).encode(),
        str(plate_id).encode(),
    ):
        h.update(part)
        h.update(b"\x00")
    return h.hexdigest()


def _evict_lru(cache_dir: Path) -> None:
    """Drop the oldest ``*.3mf`` artifacts beyond ``_LRU_MAX`` (by mtime)."""
    try:
        files = sorted(cache_dir.glob("*.3mf"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for stale in files[: max(0, len(files) - _LRU_MAX)]:
        try:
            stale.unlink()
        except OSError:
            pass


def _fresh_copy_of(cache_file: Path) -> Path:
    """Copy a cached artifact to a fresh NamedTemporaryFile-style path (caller owns +
    unlinks it — never the cache file itself)."""
    fd, name = tempfile.mkstemp(suffix=".3mf")
    os.close(fd)
    dest = Path(name)
    shutil.copyfile(cache_file, dest)
    return dest


def _build_and_store(
    source_path: Path, plate_id: int, eject_gcode: str, cache_dir: Path, cache_file: Path
) -> Path:
    """Synchronous (worker-thread) miss path: one-pass build, atomically install into
    the cache, evict, and return the freshly-built temp file for the caller.

    The built temp is returned directly as the caller-owned copy (it is a distinct
    file from the cached one), so a miss copies bytes exactly once (built → cache)."""
    built = repack_3mf_eject(source_path, plate_id, eject_gcode, zero_usage=True)
    if built is None:
        raise EjectBuildError(f"Eject repack produced no file for plate {plate_id} of {source_path}")
    # Install atomically: copy into a sibling temp within the cache dir, then
    # os.replace onto the final name (atomic on one filesystem). A concurrent
    # rebuild of the same key overwrites identical bytes — harmless.
    tmp_in_cache = cache_dir / f".{cache_file.stem}.{os.getpid()}.tmp"
    try:
        shutil.copyfile(built, tmp_in_cache)
        os.replace(tmp_in_cache, cache_file)
    except OSError as exc:
        try:
            tmp_in_cache.unlink(missing_ok=True)
        except OSError:
            pass
        logger.warning("eject build_cache: could not store artifact %s: %s", cache_file.name, exc)
    _evict_lru(cache_dir)
    return built


async def get_or_build_eject_file(
    source_path: Path,
    plate_id: int,
    eject_gcode: str,
    *,
    cache_dir: Path | None = None,
) -> Path:
    """Return the built eject ``.gcode.3mf`` :class:`Path` for ``(source, plate, gcode)``.

    The path is a freshly-copied, caller-owned temp file (safe to unlink without
    harming the cache). The content-addressed sha256 key names the cache entry and is
    kept internal to this module.

    HIT: copy the cached artifact to a fresh temp path (off the loop) and return it.
    MISS: run the one-pass build in a worker thread, store it in the cache, return the
    freshly-built temp file. Raises :class:`EjectBuildError` when the build produces
    no file.
    """
    global _swept
    cdir = _resolve_cache_dir(cache_dir)
    if not _swept:
        # One-time stale sweep: bound a cache that grew past the cap while the
        # process was down (or under a smaller prior cap).
        _swept = True
        await asyncio.to_thread(_evict_lru, cdir)

    key = await asyncio.to_thread(_cache_key, source_path, plate_id, eject_gcode)
    cache_file = cdir / f"{key}.3mf"

    if cache_file.exists():
        return await _hit(cache_file)

    lock = _build_locks.setdefault(key, asyncio.Lock())
    async with lock:
        # Re-check under the lock: a concurrent builder for this key may have just
        # populated the cache while we waited.
        if cache_file.exists():
            return await _hit(cache_file)
        return await asyncio.to_thread(_build_and_store, source_path, plate_id, eject_gcode, cdir, cache_file)


async def _hit(cache_file: Path) -> Path:
    """Serve a cache hit: refresh mtime (LRU recency) + copy out, both off the loop."""

    def _serve() -> Path:
        try:
            os.utime(cache_file, None)
        except OSError:
            pass
        return _fresh_copy_of(cache_file)

    return await asyncio.to_thread(_serve)
