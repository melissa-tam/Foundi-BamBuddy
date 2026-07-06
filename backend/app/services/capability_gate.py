"""Farm capability-matching dispatch gate (Phase 4).

Before the scheduler dispatches a FARM queue item (an item whose batch carries a
``sku_file_id``), the target printer's LIVE state must be able to actually run
the sliced file. This module compares three capability facts and returns a
verdict the scheduler acts on:

1. **Printer model / bed geometry** (HARD): the printer's model must have eject
   bed geometry (be a key of the eject generator's ``PRINTER_BED_DIMS``) AND, when
   the file records the model it was sliced for, that model must match the
   printer. Both sides are normalised through ``utils.printer_models`` so a slice
   ``printer_model_id`` (e.g. ``O1S``), a g-code-header display name (e.g.
   ``Bambu Lab H2S``) and the stored ``Printer.model`` (e.g. ``H2S``) all compare
   equal.
2. **Nozzle diameter**: if the printer reports its nozzle diameter over MQTT it
   must equal the file's ``nozzle_diameter``; a mismatch BLOCKS. If the printer
   does not report a diameter (older firmware / no live status) the fact is
   UNKNOWN — the gate WARN-dispatches (logs and proceeds) rather than blocking.
3. **Filament type**: only when the AMS/external-spool state exposes at least one
   loaded filament type AND none of them matches the file's required type(s) does
   the gate BLOCK. If nothing is loaded/known, the gate proceeds (the scheduler's
   own model-based filament matching and the printer's own load prompt remain the
   backstops).

A BLOCK is NOT a failure: the scheduler records the reason on the item's
``waiting_reason`` (surfaced in the queue UI, no frontend change needed), leaves
the item ``pending``, and re-evaluates it on later ticks — a swapped spool or a
corrected assignment clears the block automatically.

``evaluate_capability`` and ``extract_file_capabilities`` are PURE functions
(no DB, no MQTT) so the decision matrix is unit-testable in isolation; the thin
``check_dispatch_capability`` async wrapper is the only part that reads the DB and
live printer state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from backend.app.models.library import LibraryFile
from backend.app.models.print_batch import PrintBatch
from backend.app.services.eject.generator import PRINTER_BED_DIMS
from backend.app.services.printer_manager import printer_manager
from backend.app.utils.filament_types import canonical_filament_type
from backend.app.utils.printer_models import normalize_printer_model, normalize_printer_model_id

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.printer import Printer

# Prefix on every block reason so it is unambiguous in the queue UI / logs.
REASON_PREFIX = "Capability: "

# Nozzle diameters compare as floats; allow a small tolerance for "0.4" vs 0.40.
_NOZZLE_TOLERANCE_MM = 0.01


@dataclass(frozen=True)
class FileCapabilities:
    """Capability facts read from a sliced file's metadata."""

    model: str | None  # printer model the file was sliced for (raw, un-normalised)
    nozzle_diameter: float | None  # mm
    filament_types: tuple[str, ...]  # required filament type(s)


@dataclass(frozen=True)
class CapabilityDecision:
    """Outcome of the capability gate.

    ``ok`` False → BLOCK dispatch (``reason`` set). ``ok`` True → dispatch may
    proceed; when ``warn`` is True the (non-fatal) ``reason`` explains an
    unverifiable fact the scheduler should log.
    """

    ok: bool
    reason: str | None = None
    warn: bool = False


_OK = CapabilityDecision(ok=True)


def _parse_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _canon_model(value: str | None) -> str | None:
    """Collapse any model spelling (code / display name / short name) to a key.

    Runs the value through the internal-code map then the display-name map so
    ``O1S`` → ``H2S``, ``Bambu Lab H2S`` → ``H2S`` and ``H2S`` → ``H2S`` all land
    on the same uppercase, space-stripped token used for comparison.
    """
    if not value or not str(value).strip():
        return None
    mapped = normalize_printer_model_id(str(value).strip())
    mapped = normalize_printer_model(mapped) or mapped
    return mapped.upper().replace(" ", "") if mapped else None


def extract_file_capabilities(file_metadata: dict | None) -> FileCapabilities:
    """Pull the capability facts out of a ``LibraryFile.file_metadata`` blob."""
    meta = file_metadata if isinstance(file_metadata, dict) else {}
    model = meta.get("sliced_for_model") or meta.get("printer_model_id") or meta.get("printer_model")
    nozzle = _parse_float(meta.get("nozzle_diameter"))
    ftypes_raw = meta.get("filament_type")
    if isinstance(ftypes_raw, str):
        ftypes = tuple(t.strip() for t in ftypes_raw.split(",") if t.strip())
    elif isinstance(ftypes_raw, (list, tuple)):
        ftypes = tuple(str(t).strip() for t in ftypes_raw if str(t).strip())
    else:
        ftypes = ()
    return FileCapabilities(model=model if model else None, nozzle_diameter=nozzle, filament_types=ftypes)


def evaluate_capability(
    *,
    file_caps: FileCapabilities,
    printer_model: str | None,
    live_nozzle_diameter: str | float | None,
    loaded_filament_types: list[str] | None,
    bed_dims_models: set[str] | None = None,
) -> CapabilityDecision:
    """Decide whether a farm file may dispatch to a printer. Pure function.

    ``loaded_filament_types`` is ``None`` when the AMS state is unknown (no live
    status) and a (possibly empty) list when it is known. Only a known,
    non-empty list with zero overlap against the file's requirement blocks.
    """
    bed_models = bed_dims_models if bed_dims_models is not None else set(PRINTER_BED_DIMS)
    warnings: list[str] = []

    # 1. Printer model / bed geometry (HARD).
    printer_canon = _canon_model(printer_model)
    allowed = {_canon_model(m) for m in bed_models}
    allowed.discard(None)
    if printer_canon is None or printer_canon not in allowed:
        return CapabilityDecision(
            ok=False,
            reason=f"{REASON_PREFIX}printer model {printer_model!r} has no eject bed geometry",
        )
    file_model_canon = _canon_model(file_caps.model)
    if file_model_canon is None:
        warnings.append("file records no sliced printer model")
    elif file_model_canon != printer_canon:
        return CapabilityDecision(
            ok=False,
            reason=f"{REASON_PREFIX}file sliced for {file_caps.model}, printer is {printer_model}",
        )

    # 2. Nozzle diameter (BLOCK on reported mismatch; WARN when unreported).
    file_nozzle = file_caps.nozzle_diameter
    live_nozzle = _parse_float(live_nozzle_diameter)
    if file_nozzle is None:
        warnings.append("file records no nozzle diameter")
    elif live_nozzle is None:
        warnings.append(f"printer does not report nozzle diameter (file needs {file_nozzle:g}mm)")
    elif abs(live_nozzle - file_nozzle) > _NOZZLE_TOLERANCE_MM:
        return CapabilityDecision(
            ok=False,
            reason=f"{REASON_PREFIX}nozzle mismatch — printer has {live_nozzle:g}mm, file needs {file_nozzle:g}mm",
        )

    # 3. Filament type (BLOCK only on a known, non-empty, non-overlapping set).
    required = file_caps.filament_types
    if required and loaded_filament_types:
        req_canon = {canonical_filament_type(t) for t in required}
        loaded_canon = {canonical_filament_type(t) for t in loaded_filament_types if t}
        if loaded_canon and req_canon.isdisjoint(loaded_canon):
            return CapabilityDecision(
                ok=False,
                reason=(
                    f"{REASON_PREFIX}filament mismatch — file needs {', '.join(required)}, "
                    f"printer has {', '.join(sorted(loaded_filament_types))}"
                ),
            )

    if warnings:
        return CapabilityDecision(ok=True, reason=REASON_PREFIX + "; ".join(warnings), warn=True)
    return _OK


def live_nozzle_diameter(status: Any) -> str | None:
    """Read the printer's reported main-nozzle diameter from a live status object.

    Returns ``None`` when the firmware doesn't report one (treated as UNKNOWN by
    the gate → warn-dispatch, not block). Index 0 is the main/left nozzle.
    """
    if status is None:
        return None
    nozzles = getattr(status, "nozzles", None)
    if not nozzles:
        return None
    value = getattr(nozzles[0], "nozzle_diameter", "") or ""
    return value.strip() or None


def loaded_filament_types(status: Any) -> list[str] | None:
    """Read loaded filament types (AMS trays + external spool) from live status.

    Returns ``None`` when the state carries no AMS/external-spool structure at all
    (UNKNOWN → the gate won't block on filament), else the list of loaded types
    (which may be empty when trays are present but unspooled).
    """
    if status is None:
        return None
    raw = getattr(status, "raw_data", None)
    if not isinstance(raw, dict):
        return None
    if "ams" not in raw and "vt_tray" not in raw:
        return None

    types: list[str] = []
    for unit in raw.get("ams") or []:
        if not isinstance(unit, dict):
            continue
        for tray in unit.get("tray") or []:
            if isinstance(tray, dict):
                ttype = (tray.get("tray_type") or tray.get("filament_type") or "").strip()
                if ttype:
                    types.append(ttype)

    vt = raw.get("vt_tray")
    vt_list = vt if isinstance(vt, list) else ([vt] if isinstance(vt, dict) else [])
    for tray in vt_list:
        if isinstance(tray, dict):
            ttype = (tray.get("tray_type") or tray.get("filament_type") or "").strip()
            if ttype:
                types.append(ttype)
    return types


async def check_dispatch_capability(
    db: AsyncSession, item: PrintQueueItem, printer: Printer
) -> CapabilityDecision:
    """Gate a single dispatch. Non-farm items bypass the gate (always ``ok``).

    Loads the item's batch to confirm it is a farm run (``sku_file_id`` set), the
    file capability facts from the library file's metadata, and the live printer
    state, then delegates to the pure ``evaluate_capability``.
    """
    batch_id = getattr(item, "batch_id", None)
    if batch_id is None:
        return _OK  # never batched → not a farm item
    batch = await db.get(PrintBatch, batch_id)
    if batch is None or batch.sku_file_id is None:
        return _OK  # plain batch, not a farm production run

    file_meta = None
    lib_id = getattr(item, "library_file_id", None)
    if lib_id is not None:
        lib = await db.get(LibraryFile, lib_id)
        if lib is not None:
            file_meta = lib.file_metadata
    file_caps = extract_file_capabilities(file_meta)

    status = printer_manager.get_status(printer.id)
    return evaluate_capability(
        file_caps=file_caps,
        printer_model=printer.model,
        live_nozzle_diameter=live_nozzle_diameter(status),
        loaded_filament_types=loaded_filament_types(status),
    )
