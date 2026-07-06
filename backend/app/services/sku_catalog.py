"""SKU catalog domain logic (Phase 2).

Pure, unit-testable helpers for:
- auto-suggesting a SKU code/part-number/name from a file's object names and
  filename (the ``SKU\\d+\\.\\d+`` / ``#?\\d{4}(?:-\\d{2})?`` conventions);
- resolving a SKU file's live capability facts (nozzle/filament/model/max-Z)
  from ``LibraryFile.file_metadata`` with a 3MF-parse fallback;
- deriving lifetime production stats from queue-item rows.

Nothing here stores capability facts or counters — they are read/derived on
demand so the catalog never drifts from the actual sliced files.
"""

from __future__ import annotations

import re
import statistics
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from backend.app.utils.threemf_tools import read_plate_gcode_header

if TYPE_CHECKING:
    pass

# SKU code, e.g. "SKU007.01". Part number, e.g. "2656-20" or "#2656-20".
_SKU_CODE_RE = re.compile(r"SKU\d+\.\d+")
_PART_NUMBER_RE = re.compile(r"#?\d{4}(?:-\d{2})?")
# Strip a trailing model-file extension from an object name for the display name.
_EXT_RE = re.compile(r"\.(stl|3mf|obj|step|stp|gcode)$", re.IGNORECASE)


def _clean_name(object_name: str, code: str | None) -> str | None:
    """Derive a human display name from an object name.

    Removes the file extension, the SKU-code token, and any ``(#part)``
    parenthetical, then collapses whitespace. Returns None if nothing is left.
    """
    text = _EXT_RE.sub("", object_name)
    if code:
        text = text.replace(code, " ")
    # Drop a trailing parenthetical part-number group, e.g. " (#2656-20)".
    text = re.sub(r"\(\s*#?\d{4}(?:-\d{2})?\s*\)", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -_")
    return text or None


def parse_sku_suggestion(object_names: list[str], filename: str | None) -> dict:
    """Best-effort parse of a SKU code / part number / name.

    Searches ``object_names`` first (most authoritative), then the ``filename``.
    All fields are nullable. ``matched_from`` records where the primary signal
    came from ("object_name" or "filename"), or None if nothing matched.
    """
    code: str | None = None
    part_number: str | None = None
    name: str | None = None
    matched_from: str | None = None

    def _find(source: str) -> tuple[str | None, str | None]:
        code_m = _SKU_CODE_RE.search(source)
        found_code = code_m.group(0) if code_m else None
        # Search for the part number AFTER excising the SKU-code span so digits
        # inside the code can't be mistaken for a part number.
        residual = source.replace(found_code, " ") if found_code else source
        part_m = _PART_NUMBER_RE.search(residual)
        found_part = part_m.group(0).lstrip("#") if part_m else None
        return found_code, found_part

    for obj in object_names:
        if not obj:
            continue
        c, p = _find(obj)
        if c and code is None:
            code = c
            matched_from = "object_name"
        if p and part_number is None:
            part_number = p
            if matched_from is None:
                matched_from = "object_name"
        if name is None:
            derived = _clean_name(obj, c or code)
            if derived:
                name = derived
        if code and part_number and name:
            break

    if (code is None or part_number is None) and filename:
        c, p = _find(filename)
        if c and code is None:
            code = c
            if matched_from is None:
                matched_from = "filename"
        if p and part_number is None:
            part_number = p
            if matched_from is None:
                matched_from = "filename"

    return {"code": code, "part_number": part_number, "name": name, "matched_from": matched_from}


def resolve_file_capabilities(
    file_metadata: dict | None,
    source_path: Path | None,
    plate_index: int,
) -> dict:
    """Resolve nozzle/filament/model/max-Z for a SKU file.

    Prefers ``file_metadata`` (populated by the archive parser at import time);
    falls back to parsing the 3MF on disk for any missing capability field.
    ``max_z_height`` is always read from the plate's G-code header (it is a
    per-plate value that is not stored in ``file_metadata``).
    """
    meta = dict(file_metadata or {})
    nozzle_diameter = meta.get("nozzle_diameter")
    filament_type = meta.get("filament_type")
    printer_model = meta.get("sliced_for_model")
    max_z_height: float | None = None

    if source_path is not None and Path(source_path).exists():
        header = read_plate_gcode_header(Path(source_path), plate_index)
        raw = header.get("max_z_height")
        if raw is not None:
            try:
                max_z_height = float(raw)
            except (TypeError, ValueError):
                max_z_height = None

        if nozzle_diameter is None or filament_type is None or printer_model is None:
            from backend.app.services.archive import ThreeMFParser

            try:
                parsed = ThreeMFParser(Path(source_path), plate_index).parse()
            except Exception:
                parsed = {}
            if nozzle_diameter is None:
                nozzle_diameter = parsed.get("nozzle_diameter")
            if filament_type is None:
                filament_type = parsed.get("filament_type")
            if printer_model is None:
                printer_model = parsed.get("sliced_for_model")

    if nozzle_diameter is not None:
        try:
            nozzle_diameter = float(nozzle_diameter)
        except (TypeError, ValueError):
            nozzle_diameter = None

    return {
        "nozzle_diameter": nozzle_diameter,
        "filament_type": filament_type,
        "printer_model": printer_model,
        "max_z_height": max_z_height,
    }


def _median(values: list[float]) -> float | None:
    """Median of ``values``, or None if empty."""
    if not values:
        return None
    return float(statistics.median(values))


def median_cycle_seconds(rows: list[dict]) -> float | None:
    """Median gap (seconds) between consecutive ``started_at`` per printer.

    ``rows`` is a list of dicts with keys ``printer_id`` and ``started_at``.
    Gaps are computed within each printer (sorted by start time) then pooled;
    the median of the pool is returned. None when there are fewer than 2 start
    samples (i.e. no gap can be formed).
    """
    by_printer: dict[object, list[datetime]] = {}
    for row in rows:
        started = row.get("started_at")
        if started is None:
            continue
        by_printer.setdefault(row.get("printer_id"), []).append(started)

    gaps: list[float] = []
    for starts in by_printer.values():
        starts.sort()
        for prev, cur in zip(starts, starts[1:], strict=False):
            gaps.append((cur - prev).total_seconds())

    return _median(gaps)


def compute_stats_from_rows(rows: list[dict]) -> dict:
    """Derive lifetime production stats from queue-item rows.

    Each row is a dict with keys: ``status``, ``units_per_plate``,
    ``printer_id``, ``started_at``. Units are ``plates × units_per_plate``.
    ``success_rate`` is completed / (completed + failed) plates, or 0.0 when no
    terminal plates exist. ``median_cycle_seconds`` is per :func:`median_cycle_seconds`.
    """
    units_completed = 0
    units_failed = 0
    plates_completed = 0
    plates_failed = 0

    for row in rows:
        upp = row.get("units_per_plate") or 1
        status = row.get("status")
        if status == "completed":
            plates_completed += 1
            units_completed += upp
        elif status == "failed":
            plates_failed += 1
            units_failed += upp

    terminal = plates_completed + plates_failed
    success_rate = (plates_completed / terminal) if terminal > 0 else 0.0

    return {
        "units_completed": units_completed,
        "units_failed": units_failed,
        "plates_completed": plates_completed,
        "plates_failed": plates_failed,
        "success_rate": success_rate,
        "median_cycle_seconds": median_cycle_seconds(rows),
    }
