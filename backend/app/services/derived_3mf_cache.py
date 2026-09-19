"""Off-loop, content-addressed cache for DERIVED ``.gcode.3mf`` artifacts.

More than one farm lane builds a new 3MF out of a donor the farm already holds: the
motion-only eject / dry-run build, and the per-dispatch print-file derivation. Every
such artifact is deterministic in ``(what the transform is, which donor bytes, which
plate)`` — the same finished unit ejected twice, or every unit of a run of the same
SKU, rebuilds a byte-identical archive. Building one means a full ZIP read +
re-deflate, which is CPU-heavy and, run on the event loop, stalls every other
printer's dispatch (latency Phase C2). This module is the ONE mechanism both lanes
use; each binds it to its own namespace and cap and keeps its own error vocabulary.

:func:`get_or_build` is the whole surface:

- **Off the loop**: ``builder`` runs in a worker thread (``asyncio.to_thread``), never
  on the event loop.
- **Content-addressed**: artifacts are keyed by a sha256 of ``(fingerprint, source
  size, source mtime_ns, plate id)`` and stored under ``<data dir>/<namespace>``.
  Source SIZE + MTIME (not a content hash) key the donor: an edited / re-sliced donor
  changes at least one, invalidating the entry, without paying to re-hash a
  potentially hundreds-of-MB 3MF on every dispatch. ``fingerprint`` is the caller's
  word for "which transform produced this" — the eject lane passes the eject G-code
  itself, a transform-stack lane passes its stack's joined fingerprints.
- **Caller-owned copies**: both the hit and the miss path hand back a FRESH temp file
  in the SYSTEM temp dir, so a caller that UNLINKS what it is handed (the dispatch
  contract) never harms the cache. System temp matters beyond tidiness:
  :func:`bambu_ftp.cleanup_downloaded_3mf` only deletes under the system temp dir or
  ``archive_dir/temp``, so an artifact handed out from anywhere else could not be
  reaped by the lane that consumes it.
- **Byte-bounded**: each namespace is held under its own ``max_bytes`` by evicting
  oldest-by-mtime, so one lane's large artifacts cannot be bounded by the other's
  count-based intuition.

Failure is a RESULT, not an exception: a lane that must not die on a bad donor gets
:class:`BuildFailed` and maps it into its own vocabulary (the eject facade raises
``EjectBuildError``). Only an unreadable ``source_path`` still raises — that is a
broken precondition, not a build outcome.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

logger = logging.getLogger(__name__)

# Per-(namespace, key) build locks so two concurrent builds of the SAME artifact
# don't both burn CPU: the second awaits the first, then takes the freshly-populated
# cache hit. Entries are PRUNED when their build section finishes — a 24/7 per-dispatch
# lane would otherwise leak one lock per key for the life of the process.
_build_locks: dict[tuple[str, str], asyncio.Lock] = {}

# Namespaces whose one-time stale sweep has run in this process.
_swept_namespaces: set[str] = set()


@dataclass(frozen=True)
class Derived:
    """A built artifact, as a FRESH caller-owned temp copy (safe to unlink)."""

    path: Path


@dataclass(frozen=True)
class BuildFailed:
    """The builder produced no artifact; ``detail`` says what went wrong."""

    detail: str


DerivedResult: TypeAlias = Derived | BuildFailed


def _resolve_cache_dir(namespace: str, cache_dir: Path | None) -> Path:
    """The namespace's cache directory, created if needed. Defaults to
    ``<data dir>/<namespace>`` off the same ``settings.base_dir`` data root the
    archive/library storage uses; ``cache_dir`` overrides it (tests)."""
    if cache_dir is None:
        from backend.app.core.config import settings as app_settings

        cache_dir = app_settings.base_dir / namespace
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _cache_key(source_path: Path, plate_id: int, fingerprint: str) -> str:
    """sha256 hex over ``(fingerprint, source size, source mtime_ns, plate id)``.

    Raises whatever ``stat()`` raises: an unreadable donor is a broken precondition
    of the whole call, not a build outcome the caller can serve around."""
    st = source_path.stat()
    h = hashlib.sha256()
    for part in (
        fingerprint.encode("utf-8"),
        str(st.st_size).encode(),
        str(st.st_mtime_ns).encode(),
        str(plate_id).encode(),
    ):
        h.update(part)
        h.update(b"\x00")
    return h.hexdigest()


def _evict_to_cap(cache_dir: Path, max_bytes: int) -> None:
    """Drop oldest-by-mtime ``*.3mf`` artifacts until the namespace totals
    ``max_bytes`` or less.

    BYTE-bounded rather than count-bounded: artifact sizes differ by an order of
    magnitude between lanes (a slim motion-only eject file vs a full print 3MF), and
    a count cap sized for one is either useless or ruinous for the other."""
    try:
        entries = sorted(((p, p.stat()) for p in cache_dir.glob("*.3mf")), key=lambda e: e[1].st_mtime)
    except OSError:
        return
    total = sum(st.st_size for _, st in entries)
    for path, st in entries:
        if total <= max_bytes:
            return
        try:
            path.unlink()
        except OSError:
            continue
        total -= st.st_size


def _fresh_copy_of(cache_file: Path) -> Path:
    """Copy a cached artifact to a fresh temp path in the SYSTEM temp dir (caller owns
    + unlinks it — never the cache file itself)."""
    fd, name = tempfile.mkstemp(suffix=".3mf")
    os.close(fd)
    dest = Path(name)
    shutil.copyfile(cache_file, dest)
    return dest


def _build_and_store(
    builder: Callable[[], Path | None],
    namespace: str,
    cache_dir: Path,
    cache_file: Path,
    max_bytes: int,
) -> Path | None:
    """Synchronous (worker-thread) miss path: build, atomically install into the
    cache, evict to the cap, and return the freshly-built temp file for the caller.

    The built temp is returned directly as the caller-owned copy (it is a distinct
    file from the cached one), so a miss copies bytes exactly once (built → cache).
    ``None`` from the builder is passed straight through — the caller turns it into a
    :class:`BuildFailed`. An artifact that ALONE exceeds ``max_bytes`` is served but
    not installed: caching it would evict the entire namespace on every insert."""
    built = builder()
    if built is None:
        return None
    try:
        size = built.stat().st_size
    except OSError:
        size = 0
    if size > max_bytes:
        logger.info(
            "derived_3mf_cache[%s]: artifact %s bytes exceeds the %s byte cap — served, not cached",
            namespace,
            size,
            max_bytes,
        )
        return built
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
        logger.warning("derived_3mf_cache[%s]: could not store artifact %s: %s", namespace, cache_file.name, exc)
    _evict_to_cap(cache_dir, max_bytes)
    return built


async def _hit(cache_file: Path) -> Derived | None:
    """Serve a cache hit: refresh mtime (LRU recency) + copy out, both off the loop.

    ``None`` when the entry LOST A RACE with an eviction between the caller's
    ``exists()`` and this copy — the hit is simply not there any more, and the caller
    falls through to the build path. That race is not theoretical on the dispatch
    namespace: every insert evicts oldest-first to hold the byte cap, so a concurrent
    dispatch of a different SKU can delete this very file while it is being served.
    Raising here would fail a dispatch over a cache detail."""

    def _serve() -> Path | None:
        try:
            os.utime(cache_file, None)
        except OSError:
            pass
        try:
            return _fresh_copy_of(cache_file)
        except OSError as exc:
            logger.info("derived_3mf_cache: hit %s vanished mid-copy (%s) — rebuilding", cache_file.name, exc)
            return None

    served = await asyncio.to_thread(_serve)
    return None if served is None else Derived(served)


async def get_or_build(
    source_path: Path,
    plate_id: int,
    fingerprint: str,
    builder: Callable[[], Path | None],
    *,
    namespace: str,
    max_bytes: int,
    cache_dir: Path | None = None,
) -> DerivedResult:
    """Return the derived ``.gcode.3mf`` for ``(source, plate, fingerprint)``.

    ``builder`` is SYNCHRONOUS and runs in a worker thread; it returns a caller-owned
    temp 3MF or ``None``. The content-addressed sha256 key names the cache entry and
    is kept internal to this module.

    HIT: copy the cached artifact to a fresh temp path (off the loop) and return it.
    MISS: run ``builder`` in a worker thread, install the result in the cache and
    return the freshly-built temp file. Either way the returned
    :class:`Derived` ``path`` is caller-owned and safe to unlink. A hit whose copy
    LOSES A RACE with the cap's eviction falls through to the build path rather than
    raising — at BOTH the unlocked check and the re-check under the lock.

    :class:`BuildFailed` when ``builder`` returns ``None`` or raises (the traceback is
    logged). A store failure is a WARNING only — the artifact was built, so it is
    still served.

    Concurrency: the per-key lock is released AND pruned when the build section
    finishes. Waiters already hold the lock object, and a caller arriving after the
    prune finds the installed cache file without needing the lock at all. Two
    concurrent builds of one key — possible only after a FAILED build left no cache
    file — are harmless: the install is atomic and the bytes are identical.
    """
    cdir = _resolve_cache_dir(namespace, cache_dir)
    if namespace not in _swept_namespaces:
        # One-time stale sweep: bound a namespace that grew past the cap while the
        # process was down (or under a larger prior cap — including a cap belonging
        # to a build this one rolled back to).
        _swept_namespaces.add(namespace)
        await asyncio.to_thread(_evict_to_cap, cdir, max_bytes)

    key = await asyncio.to_thread(_cache_key, source_path, plate_id, fingerprint)
    cache_file = cdir / f"{key}.3mf"

    if cache_file.exists():
        hit = await _hit(cache_file)
        if hit is not None:
            return hit

    lock_key = (namespace, key)
    lock = _build_locks.setdefault(lock_key, asyncio.Lock())
    try:
        async with lock:
            # Re-check under the lock: a concurrent builder for this key may have just
            # populated the cache while we waited.
            if cache_file.exists():
                hit = await _hit(cache_file)
                if hit is not None:
                    return hit
            try:
                built = await asyncio.to_thread(_build_and_store, builder, namespace, cdir, cache_file, max_bytes)
            except Exception as exc:
                logger.exception(
                    "derived_3mf_cache[%s]: builder raised for %s plate %s", namespace, source_path, plate_id
                )
                return BuildFailed(f"{type(exc).__name__}: {exc}")
            if built is None:
                return BuildFailed("builder produced no file")
            return Derived(built)
    finally:
        _build_locks.pop(lock_key, None)
