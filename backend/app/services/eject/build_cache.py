"""The eject / dry-run lane of the derived-3MF cache.

A FACADE, deliberately thin: the mechanism (content-addressed key, off-loop build,
atomic install, caller-owned temp copies, byte-bounded LRU) lives ONCE in
:mod:`backend.app.services.derived_3mf_cache`, because a second lane — the
per-dispatch print file — needs exactly the same thing and a second copy of a cache
is a second set of eviction and locking bugs. What is genuinely the eject lane's own
stays here: WHAT is built (:func:`repack_3mf_eject` over the eject G-code), WHICH
namespace and cap it is built into, and the error vocabulary its callers already
catch (:class:`EjectBuildError`).

The motion-only eject artifact is deterministic in ``(eject gcode text, donor file
bytes, plate id)``: the same finished unit ejected twice — or every unit of a run of
the same SKU — rebuilds a byte-identical archive, so it caches perfectly.
"""

from __future__ import annotations

from pathlib import Path

from backend.app.services.derived_3mf_cache import BuildFailed, get_or_build
from backend.app.utils.threemf_tools import repack_3mf_eject

# The namespace directory is unchanged from the count-capped implementation this
# facade replaced, so entries built by an earlier build are still hits — and any
# excess simply ages out through the byte cap on first use.
_NAMESPACE = "eject_cache"

# Eject builds are SLIM (motion only: no meshes, no thumbnails), a few hundred KB
# each, so 64 MiB holds hundreds of them — deep enough that a whole run of one SKU
# stays resident, shallow enough to bound the data dir.
_MAX_BYTES = 64 * 1024 * 1024


class EjectBuildError(RuntimeError):
    """The one-pass eject repack produced no file (e.g. the plate has no G-code)."""


async def get_or_build_eject_file(
    source_path: Path,
    plate_id: int,
    eject_gcode: str,
    *,
    cache_dir: Path | None = None,
) -> Path:
    """Return the built eject ``.gcode.3mf`` :class:`Path` for ``(source, plate, gcode)``.

    The path is a freshly-copied, caller-owned temp file (safe to unlink without
    harming the cache). Raises :class:`EjectBuildError` when the build produces no
    file.
    """
    result = await get_or_build(
        source_path,
        plate_id,
        eject_gcode,
        lambda: repack_3mf_eject(source_path, plate_id, eject_gcode, zero_usage=True),
        namespace=_NAMESPACE,
        max_bytes=_MAX_BYTES,
        cache_dir=cache_dir,
    )
    if isinstance(result, BuildFailed):
        raise EjectBuildError(f"Eject repack produced no file for plate {plate_id} of {source_path} ({result.detail})")
    return result.path
