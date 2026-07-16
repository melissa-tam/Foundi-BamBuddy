"""Farm capability-matching dispatch gate (Phase 4).

Before the scheduler dispatches a FARM queue item (an item whose batch carries a
``sku_file_id``), the target printer's LIVE state must be able to actually run
the sliced file. This module compares three capability facts and returns a
verdict the scheduler acts on:

1. **Printer model / bed geometry** (HARD): the printer's model must have a
   VALIDATED eject geometry row in the ``printer_model_geometry`` registry AND,
   when the file records the model it was sliced for, that model must match the
   printer. Both sides are normalised through ``utils.printer_models`` so a slice
   ``printer_model_id`` (e.g. ``O1S``), a g-code-header display name (e.g.
   ``Bambu Lab H2S``) and the stored ``Printer.model`` (e.g. ``H2S``) all compare
   equal.
2. **Nozzle diameter** — dual-nozzle aware. The file's requirements come from the
   parsed ``plate_capabilities`` (one entry per filament actually used on the
   plate, each optionally pinned to an MQTT extruder id — 0 = right/main, 1 =
   left/deputy) with the legacy scalar ``nozzle_diameter`` folded in as a single
   un-pinned requirement. The printer's LIVE state exposes the diameters mounted
   on each hotend PLUS the diameters sitting in the Vortek rack. Decision table:

   * **Requirement pinned to an extruder** (dual file): the mounted hotend on
     that side must match — OR the needed diameter is present in the rack, which
     H2C firmware auto-picks from, so a rack match is OK (no warn). Mounted known
     and mismatched with no rack match → BLOCK. Mounted unknown with no rack
     match → WARN (proceed).
   * **Requirement un-pinned on a single-nozzle printer**: exact legacy
     semantics — compare against the one mounted hotend (index 0); mismatch
     BLOCKS, unknown WARNs. (Reason strings kept byte-identical.)
   * **Requirement un-pinned on a dual printer**: any mounted-or-rack diameter
     matches → OK (with a WARN if the two hotends carry different diameters and
     the file names no extruder); nothing matches (all known) → BLOCK; nothing
     known → WARN.

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

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from backend.app.models.library import LibraryFile
from backend.app.models.print_batch import PrintBatch
from backend.app.services.eject.geometry import list_geometries
from backend.app.services.printer_manager import printer_manager
from backend.app.utils.filament_types import canonical_filament_type
from backend.app.utils.printer_models import RACK_ID_MIN, canon_model, is_dual_nozzle_model, side_label

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.printer import Printer

# Prefix on every block reason so it is unambiguous in the queue UI / logs.
REASON_PREFIX = "Capability: "

# Nozzle diameters compare as floats; allow a small tolerance for "0.4" vs 0.40.
_NOZZLE_TOLERANCE_MM = 0.01


@dataclass(frozen=True)
class NozzleRequirement:
    """One nozzle-diameter requirement drawn from a plate's used filaments.

    ``extruder_id`` is the MQTT hotend id the slice pins the filament to (0 =
    right/main, 1 = left/deputy) or ``None`` when the file names no extruder
    (single-nozzle files, or the legacy scalar). ``slot_id`` is the AMS slot the
    requirement came from (informational, used only in warn text).
    """

    slot_id: int | None
    diameter: float | None
    extruder_id: int | None


@dataclass(frozen=True)
class FileCapabilities:
    """Capability facts read from a sliced file's metadata."""

    model: str | None  # printer model the file was sliced for (raw, un-normalised)
    nozzle_diameter: float | None  # legacy scalar diameter (mm), None when absent
    filament_types: tuple[str, ...]  # required filament type(s)
    nozzle_requirements: tuple[NozzleRequirement, ...] = ()  # per-filament, plate-scoped


@dataclass(frozen=True)
class LiveNozzles:
    """The printer's live nozzle inventory: mounted hotends + Vortek rack slots.

    ``mounted`` maps MQTT extruder id → diameter string (only non-empty entries).
    ``rack`` maps a Vortek rack slot id (``>= RACK_ID_MIN``) → diameter string.
    Frozen for hygiene; never hashed (dict fields are unhashable) but compared by
    value in tests.
    """

    mounted: dict[int, str] = field(default_factory=dict)
    rack: dict[int, str] = field(default_factory=dict)


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


def _coerce_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _diameters_match(a: float, b: float) -> bool:
    return abs(a - b) <= _NOZZLE_TOLERANCE_MM


def _fmt_diameters(values: list[float]) -> str:
    return ", ".join(f"{v:g}" for v in values)


def _distinct(values: list[float]) -> list[float]:
    """Sorted, tolerance-deduped diameter list for reason/warn text."""
    out: list[float] = []
    for v in sorted(values):
        if not any(_diameters_match(v, kept) for kept in out):
            out.append(v)
    return out


def _extract_nozzle_requirements(meta: dict, plate_id: int | None) -> tuple[NozzleRequirement, ...]:
    """Build the per-filament requirements from the plate_capabilities blob.

    Resolves the plate INSIDE: the ``plate_id`` entry, else the sole entry when
    there is exactly one, else nothing. Tolerant of malformed input — a non-dict
    ``plate_capabilities`` / entry / ``filament_nozzles`` yields no requirements
    (the caller falls back to the legacy scalar).
    """
    pc = meta.get("plate_capabilities")
    if not isinstance(pc, dict) or not pc:
        return ()
    entry = pc.get(str(plate_id or 1))
    if entry is None and len(pc) == 1:
        entry = next(iter(pc.values()))
    if not isinstance(entry, dict):
        return ()
    fila_nozzles = entry.get("filament_nozzles")
    if not isinstance(fila_nozzles, list):
        return ()
    reqs: list[NozzleRequirement] = []
    for f in fila_nozzles:
        if not isinstance(f, dict):
            continue
        reqs.append(
            NozzleRequirement(
                slot_id=_coerce_int(f.get("slot_id")),
                diameter=_parse_float(f.get("nozzle_diameter")),
                extruder_id=_coerce_int(f.get("extruder_id")),
            )
        )
    return tuple(reqs)


def extract_file_capabilities(file_metadata: dict | None, plate_id: int | None = None) -> FileCapabilities:
    """Pull the capability facts out of a ``LibraryFile.file_metadata`` blob.

    ``plate_id`` scopes the per-filament nozzle requirements to the plate being
    dispatched (a ``.gcode.3mf`` can carry several plates with different nozzle
    layouts). The legacy scalar ``nozzle_diameter`` is still read for files that
    predate the ``plate_capabilities`` parser.
    """
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
    requirements = _extract_nozzle_requirements(meta, plate_id)
    return FileCapabilities(
        model=model if model else None,
        nozzle_diameter=nozzle,
        filament_types=ftypes,
        nozzle_requirements=requirements,
    )


def _check_one_nozzle_requirement(
    req: NozzleRequirement,
    mounted: dict[int, float],
    rack: list[float],
    printer_is_dual: bool,
    warnings: list[str],
) -> CapabilityDecision | None:
    """Evaluate a single nozzle requirement. Returns a BLOCK decision or None
    (append warnings for the non-fatal arms). ``mounted``/``rack`` carry parsed
    floats; only the mounted hotends actually reported are present."""
    req_d = req.diameter
    if req_d is None:
        slot = req.slot_id if req.slot_id is not None else "?"
        warnings.append(f"file filament {slot} records no nozzle diameter")
        return None

    rack_has_match = any(_diameters_match(r, req_d) for r in rack)

    if req.extruder_id is not None:
        # Dual-pinned requirement: the slice names the hotend it must run on.
        side = side_label(req.extruder_id)
        mounted_d = mounted.get(req.extruder_id)
        if mounted_d is not None:
            if _diameters_match(mounted_d, req_d):
                return None
            if rack_has_match:
                return None  # firmware auto-picks the matching rack nozzle — trusted, no warn
            return CapabilityDecision(
                ok=False,
                reason=(
                    f"{REASON_PREFIX}nozzle mismatch — {side} hotend has {mounted_d:g}mm, "
                    f"file needs {req_d:g}mm (not in rack)"
                ),
            )
        # Mounted diameter for that side is unknown.
        if rack_has_match:
            return None
        warnings.append(f"printer does not report {side} hotend diameter (file needs {req_d:g}mm)")
        return None

    # Un-pinned requirement (single-nozzle file, or the legacy scalar).
    if not printer_is_dual:
        # EXACT legacy single-nozzle semantics + reason strings.
        live_d = mounted.get(0)
        if live_d is None:
            warnings.append(f"printer does not report nozzle diameter (file needs {req_d:g}mm)")
            return None
        if _diameters_match(live_d, req_d):
            return None
        return CapabilityDecision(
            ok=False,
            reason=f"{REASON_PREFIX}nozzle mismatch — printer has {live_d:g}mm, file needs {req_d:g}mm",
        )

    # Un-pinned requirement on a dual printer: any hotend or rack slot may serve it.
    known = list(mounted.values()) + rack
    if not known:
        warnings.append(f"printer does not report nozzle diameter (file needs {req_d:g}mm)")
        return None
    if any(_diameters_match(k, req_d) for k in known):
        distinct_mounted = _distinct(list(mounted.values()))
        if len(distinct_mounted) >= 2:
            warnings.append(
                f"mounted nozzle diameters differ ({_fmt_diameters(distinct_mounted)}); file does not pin an extruder"
            )
        return None
    return CapabilityDecision(
        ok=False,
        reason=(
            f"{REASON_PREFIX}nozzle mismatch — printer has {_fmt_diameters(_distinct(known))}mm, file needs {req_d:g}mm"
        ),
    )


def evaluate_capability(
    *,
    file_caps: FileCapabilities,
    printer_model: str | None,
    live_nozzles: LiveNozzles | None,
    loaded_filament_types: list[str] | None,
    bed_dims_models: set[str] | None = None,
    printer_is_dual: bool = False,
) -> CapabilityDecision:
    """Decide whether a farm file may dispatch to a printer. Pure function.

    ``live_nozzles`` is the printer's mounted + rack nozzle inventory (``None``
    when there is no live status — treated as everything unknown).
    ``printer_is_dual`` is passed in by the caller (kept pure — no model lookups
    here). ``loaded_filament_types`` is ``None`` when the AMS state is unknown and
    a (possibly empty) list when it is known; only a known, non-empty list with
    zero overlap against the file's requirement blocks.

    ``bed_dims_models`` is the set of canonical model keys that have a VALIDATED
    eject geometry row (the caller derives it from the registry via
    ``list_geometries``). It defaults to the empty set — fail-closed: with no
    allowed models supplied, every printer blocks on the geometry check.
    """
    bed_models = bed_dims_models if bed_dims_models is not None else set()
    warnings: list[str] = []

    # 1. Printer model / bed geometry (HARD).
    printer_canon = canon_model(printer_model)
    allowed = {canon_model(m) for m in bed_models}
    allowed.discard(None)
    if printer_canon is None or printer_canon not in allowed:
        return CapabilityDecision(
            ok=False,
            reason=f"{REASON_PREFIX}printer model {printer_model!r} has no eject bed geometry",
        )
    file_model_canon = canon_model(file_caps.model)
    if file_model_canon is None:
        warnings.append("file records no sliced printer model")
    elif file_model_canon != printer_canon:
        return CapabilityDecision(
            ok=False,
            reason=f"{REASON_PREFIX}file sliced for {file_caps.model}, printer is {printer_model}",
        )

    # 2. Nozzle diameter (dual-nozzle aware). Requirements come from the parsed
    # plate_capabilities; the legacy scalar folds in as a single un-pinned one so
    # there is a SINGLE decision path (no per-shape duplication).
    mounted: dict[int, float] = {}
    for eid, raw in (live_nozzles.mounted if live_nozzles else {}).items():
        parsed = _parse_float(raw)
        if parsed is not None:
            mounted[eid] = parsed
    rack: list[float] = [
        p for p in (_parse_float(v) for v in (live_nozzles.rack if live_nozzles else {}).values()) if p is not None
    ]

    requirements = list(file_caps.nozzle_requirements)
    if not requirements and file_caps.nozzle_diameter is not None:
        requirements = [NozzleRequirement(slot_id=None, diameter=file_caps.nozzle_diameter, extruder_id=None)]

    if not requirements:
        warnings.append("file records no nozzle diameter")
    else:
        for req in requirements:
            block = _check_one_nozzle_requirement(req, mounted, rack, printer_is_dual, warnings)
            if block is not None:
                return block

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


def read_live_nozzles(status: Any) -> LiveNozzles | None:
    """Read the printer's mounted + Vortek-rack nozzle inventory from live status.

    ``None`` when there is no live status at all. ``mounted`` maps MQTT extruder
    id → diameter string for every hotend that reports a non-empty diameter
    (``state.nozzles`` is a fixed 2-slot list; an empty string means unknown and
    is dropped). ``rack`` is built from ``state.nozzle_rack`` — the raw entries
    carry ``id`` + ``diameter`` (mapped to ``nozzle_diameter`` only in the API
    payload, so accept either key here); only ids ``>= RACK_ID_MIN`` are true
    Vortek rack slots (ids 0/1 are hotend echoes and belong to ``mounted``).
    Shape-tolerant: short/absent lists and non-dict rack entries are skipped.
    """
    if status is None:
        return None
    mounted: dict[int, str] = {}
    for i, nozzle in enumerate(getattr(status, "nozzles", None) or []):
        raw = getattr(nozzle, "nozzle_diameter", "") or ""
        diameter = raw.strip() if isinstance(raw, str) else str(raw).strip()
        if diameter:
            mounted[i] = diameter
    rack: dict[int, str] = {}
    for entry in getattr(status, "nozzle_rack", None) or []:
        if not isinstance(entry, dict):
            continue
        rid = _coerce_int(entry.get("id"))
        if rid is None or rid < RACK_ID_MIN:
            continue
        raw = entry.get("diameter") or entry.get("nozzle_diameter") or ""
        diameter = raw.strip() if isinstance(raw, str) else str(raw).strip()
        if diameter:
            rack[rid] = diameter
    return LiveNozzles(mounted=mounted, rack=rack)


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


async def check_dispatch_capability(db: AsyncSession, item: PrintQueueItem, printer: Printer) -> CapabilityDecision:
    """Gate a single dispatch. Non-farm items bypass the gate (always ``ok``).

    Loads the item's batch to confirm it is a farm run (``sku_file_id`` set), the
    file capability facts from the library file's metadata (scoped to the item's
    plate), and the live printer state, then delegates to the pure
    ``evaluate_capability``.
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
    file_caps = extract_file_capabilities(file_meta, getattr(item, "plate_id", None))

    # Allowed models = canonical keys of the VALIDATED geometry rows. Pulled from
    # the registry here (the DB-touching wrapper) and handed to the pure decision
    # function, so an unvalidated model (envelope not through the ladder) is never
    # eligible for farm dispatch.
    validated_models = {g.model_key for g in await list_geometries(db) if g.validated}

    status = printer_manager.get_status(printer.id)
    return evaluate_capability(
        file_caps=file_caps,
        printer_model=printer.model,
        live_nozzles=read_live_nozzles(status),
        loaded_filament_types=loaded_filament_types(status),
        bed_dims_models=validated_models,
        printer_is_dual=is_dual_nozzle_model(printer.model),
    )
