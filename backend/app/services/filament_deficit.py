"""Filament-deficit check used by every queue dispatch path.

The PrintModal warns when an assigned spool can't satisfy a print's per-slot
filament weight (``Pre-print checks now also warn when the spool has
insufficient material`` — #720). That check only runs when the user clicks
"Print" inside PrintModal; ``QueuePage`` Play button, ``start_queue_item``
route, and the VP intake + scheduler auto-dispatch path all skip it (#1496).

This module is the single source of truth for the check. Both the route
handler (``POST /print-queue/{id}/start``) and the dispatch scheduler call
``compute_deficit_for_queue_item`` against live spool state.

Design notes:
* The 3MF parser is the same one used by PrintModal: per-slot ``used_grams``
  comes from ``extract_filament_requirements`` (#1188's filament-overrides
  pipeline) or — when the item points at an unsliced library file — falls
  through to the file's archive copy. Anything that yields no requirements
  is treated as "no deficit" so a malformed or stripped 3MF never blocks.
* Both internal-inventory and Spoolman modes are covered. Internal mode
  resolves via ``SpoolAssignment`` joined to ``Spool`` (``label_weight``
  minus ``weight_used``). Spoolman mode resolves via
  ``SpoolmanSlotAssignment`` then ``SpoolmanClient.get_spool`` for the live
  remaining weight; if Spoolman is unreachable we return no deficit rather
  than wedge the queue on a flaky network call.
* The ``disable_filament_warnings`` user setting is respected at the
  service boundary — callers do not have to know about it.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.core.config import settings as app_settings
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment
from backend.app.services.filament_requirements import extract_filament_requirements

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FilamentDeficit:
    """One slot's filament shortfall."""

    slot_id: int
    ams_id: int | None
    tray_id: int | None
    filament_type: str
    required_grams: float
    remaining_grams: float | None  # None = could not determine

    def to_dict(self) -> dict:
        return {
            "slot_id": self.slot_id,
            "ams_id": self.ams_id,
            "tray_id": self.tray_id,
            "filament_type": self.filament_type,
            "required_grams": self.required_grams,
            "remaining_grams": self.remaining_grams,
        }


def _global_to_ams_key(global_tray_id: int) -> tuple[int, int]:
    """Inverse of ``ams_id * 4 + tray_id`` — matches ``usage_tracker``."""
    if global_tray_id >= 254:
        return (255, global_tray_id - 254)
    if global_tray_id >= 128:
        return (global_tray_id, 0)
    return (global_tray_id // 4, global_tray_id % 4)


def _resolve_source_3mf(item: PrintQueueItem) -> Path | None:
    """Locate the 3MF file backing this queue item (archive or library)."""
    if item.archive is not None and item.archive.file_path:
        return app_settings.base_dir / item.archive.file_path
    if item.library_file is not None and item.library_file.file_path:
        return Path(item.library_file.file_path)
    return None


def _spoolman_grams_from_dict(spool: dict | None) -> float | None:
    """Remaining grams from a Spoolman spool dict, or ``None`` when indeterminable.

    Spoolman exposes either an absolute ``remaining_weight``, or ``used_weight``
    + ``filament.weight``. Either is sufficient — prefer ``remaining_weight``
    when present (the user may have overridden it).
    """
    if not spool:
        return None
    remaining = spool.get("remaining_weight")
    if isinstance(remaining, (int, float)) and remaining >= 0:
        return float(remaining)
    used = spool.get("used_weight")
    total = (spool.get("filament") or {}).get("weight")
    if isinstance(used, (int, float)) and isinstance(total, (int, float)) and total > 0:
        return max(0.0, float(total) - float(used))
    return None


async def _spoolman_remaining_grams(spoolman_spool_id: int) -> float | None:
    """Live remaining grams for a Spoolman spool, or None if unavailable."""
    try:
        from backend.app.services.spoolman import (
            SpoolmanClientError,
            SpoolmanNotFoundError,
            get_spoolman_client,
        )
    except ImportError:
        return None
    try:
        client = await get_spoolman_client()
        if client is None:
            return None
        spool = await client.get_spool(spoolman_spool_id)
    except (SpoolmanNotFoundError, SpoolmanClientError):
        return None
    except Exception as e:
        logger.debug("Spoolman fetch failed for spool %s: %s", spoolman_spool_id, e)
        return None

    return _spoolman_grams_from_dict(spool)


async def _is_spoolman_mode(db: AsyncSession) -> bool:
    """Check whether the user has opted in to Spoolman inventory mode."""
    try:
        from backend.app.api.routes.settings import get_setting

        spoolman_enabled = await get_setting(db, "spoolman_enabled")
        return bool(spoolman_enabled) and spoolman_enabled.lower() == "true"
    except Exception:
        return False


async def _warnings_disabled(db: AsyncSession) -> bool:
    """Honour the ``disable_filament_warnings`` setting (#720)."""
    try:
        from backend.app.api.routes.settings import get_setting

        disabled = await get_setting(db, "disable_filament_warnings")
        return bool(disabled) and disabled.lower() == "true"
    except Exception:
        return False


def _normalize_color_for_id(raw: str | None) -> str:
    """Canonicalise a hex colour for identity comparison.

    Strips the leading ``#``, uppercases, and drops the alpha channel when
    the hex is 8 chars long (``RRGGBBAA``) so a fully-opaque 8-char hex
    matches a 6-char hex of the same RGB. Empty / None → empty string.
    """
    s = (raw or "").strip().lstrip("#").upper()
    if len(s) == 8:  # RRGGBBAA → strip alpha
        s = s[:6]
    return s


def _live_tray_identities(printer_id: int) -> dict[tuple[int, int], str]:
    """Map ``(ams_id, tray_id) → material identity`` from the printer's LIVE trays.

    Mirrors how AMS Filament Backup actually works: the firmware pools by each
    tray's *configured* filament (its ``tray_info_idx``/``tray_type`` + colour)
    and switches on physical runout — regardless of whether the software holds
    an inventory binding for the spool (bindings auto-create for RFID spools
    only). So the pooling identity must come from the live tray, not from a
    ``Spool`` row.

    Reads ``printer_manager.get_status(printer_id).raw_data["ams"]`` — the same
    live-status surface ``capability_gate.loaded_filament_types`` reads. The
    external ``vt_tray`` holder is intentionally excluded (AMS backup never
    spans it). Empty/blank trays (no ``tray_type``/``filament_type``) produce no
    identity. Every access is guarded; returns ``{}`` when the state or its AMS
    structure is missing.
    """
    try:
        from backend.app.services.printer_manager import printer_manager
    except ImportError:
        return {}

    state = printer_manager.get_status(printer_id)
    raw = getattr(state, "raw_data", None)
    if not isinstance(raw, dict):
        return {}

    identities: dict[tuple[int, int], str] = {}
    for unit in raw.get("ams") or []:
        if not isinstance(unit, dict):
            continue
        try:
            ams_id = int(unit["id"])
        except (KeyError, TypeError, ValueError):
            continue
        for tray in unit.get("tray") or []:
            if not isinstance(tray, dict):
                continue
            tray_type = (tray.get("tray_type") or tray.get("filament_type") or "").strip()
            if not tray_type:
                continue  # empty/blank slot — nothing loaded
            try:
                tray_id = int(tray["id"])
            except (KeyError, TypeError, ValueError):
                continue
            info_idx = (tray.get("tray_info_idx") or "").strip()
            color = _normalize_color_for_id(tray.get("tray_color"))
            identities[(ams_id, tray_id)] = f"tray:{info_idx or tray_type}|color:{color}"
    return identities


def _ams_id_from_global(global_tray_id: int) -> int:
    """Inverse of ``_global_to_ams_key`` returning ams_id only."""
    return _global_to_ams_key(global_tray_id)[0]


def _extruder_side_for_ams(
    ams_id: int,
    ams_extruder_map: dict[str, int],
    is_dual_extruder: bool,
) -> int:
    """Resolve the extruder index (0=right, 1=left) for a given AMS unit.

    Single-extruder printers collapse everything to 0. On dual-extruder
    printers (H2D / H2C / X2D), the firmware can't cross extruders even with
    AMS Filament Backup ON, so the pool must be scoped per-side.
    """
    if not is_dual_extruder:
        return 0
    return int(ams_extruder_map.get(str(ams_id), 0))


def _parse_ams_mapping(raw: str | None) -> list[int] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, list):
        return None
    return [v for v in parsed if isinstance(v, int)]


async def _get_printer_backup_context(
    printer_id: int,
) -> tuple[bool, dict[str, int], bool]:
    """Return ``(backup_on, ams_extruder_map, is_dual_extruder)`` for the printer.

    Read from the live MQTT state via ``printer_manager`` (no DB round-trip).
    Defaults conservatively to ``backup_on=False`` when the state is missing
    or the printer is offline — same fallback as today (per-slot deficit
    accounting), so an offline printer is never treated as backup-capable.
    """
    try:
        from backend.app.services.printer_manager import printer_manager
        from backend.app.utils.printer_models import is_dual_nozzle_model
    except ImportError:
        return False, {}, False

    state = printer_manager.get_status(printer_id)
    if state is None:
        return False, {}, False

    backup_on = state.ams_filament_backup is True
    ams_extruder_map = dict(state.ams_extruder_map or {})
    model = printer_manager.get_model(printer_id)
    is_dual = bool(model and is_dual_nozzle_model(model))
    return backup_on, ams_extruder_map, is_dual


async def compute_deficit_for_queue_item(
    db: AsyncSession,
    item: PrintQueueItem,
    *,
    printer_id_override: int | None = None,
    ams_mapping_override: str | None = None,
) -> list[FilamentDeficit]:
    """Return per-slot filament shortfalls for ``item``, or [] when it's safe to dispatch.

    ``printer_id_override`` / ``ams_mapping_override`` let the scheduler
    deficit-check a candidate printer WITHOUT mutating the item — the model-based
    candidate loop tries each idle printer's own spool state in turn before
    claiming one. When either is ``None`` the corresponding ``item`` field is used,
    so every existing caller (route, PrintModal, farm_staging) is unaffected.

    Returns an empty list whenever any of the following hold:

    * The ``disable_filament_warnings`` setting is on.
    * There is no resolved printer (override or ``item.printer_id`` — model-based
      assignment not yet picked a printer; the scheduler re-runs after it does).
    * No source 3MF is available, or the 3MF carries no per-slot
      requirements (treated as "nothing to verify" rather than an error,
      matching the PrintModal behaviour).
    * No AMS mapping is set yet — the scheduler computes the mapping just
      before dispatch; until it does we cannot map slot → tray.
    * Spoolman mode is on but the Spoolman server is unreachable. We do not
      wedge the queue on a network blip.

    #1762: when the printer reports ``ams_filament_backup=True`` in MQTT
    status, available material is pooled by LIVE TRAY identity — each tray's
    configured filament (``tray_info_idx``/``tray_type`` + colour), the same
    key the firmware itself switches on — across ALL loaded trays on the
    printer (within the same extruder side for dual-nozzle models, since
    firmware can't cross extruders even with the backup bit set). Pool grams
    come from whatever inventory binding backs each tray; a loaded tray with
    NO determinable grams (an unbound / no-RFID spool, e.g. a manually-set
    1 kg roll) makes its identity's pool *open-ended*, so requirements for
    that identity are never blocked — mirroring the firmware, which switches
    to that spool on runout. Per-slot shortfalls are otherwise only emitted
    when the POOL is too small for the print's total required of that
    identity. A mapped slot whose tray reports no live filament falls back to
    today's strict per-slot check.
    """
    if await _warnings_disabled(db):
        return []
    printer_id = printer_id_override if printer_id_override is not None else item.printer_id
    if printer_id is None:
        return []

    # Refresh the relationships we need without assuming the caller eagerly
    # loaded them — both the route and the scheduler call this from contexts
    # with different loading strategies.
    refreshed = await db.execute(
        select(PrintQueueItem)
        .options(
            selectinload(PrintQueueItem.archive),
            selectinload(PrintQueueItem.library_file),
        )
        .where(PrintQueueItem.id == item.id)
    )
    item = refreshed.scalar_one_or_none() or item

    source_path = _resolve_source_3mf(item)
    if source_path is None or not source_path.exists():
        return []

    requirements = extract_filament_requirements(source_path, item.plate_id)
    if not requirements:
        return []

    mapping_source = ams_mapping_override if ams_mapping_override is not None else item.ams_mapping
    mapping = _parse_ams_mapping(mapping_source)
    if not mapping:
        return []

    spoolman_mode = await _is_spoolman_mode(db)
    backup_on, ams_extruder_map, is_dual = await _get_printer_backup_context(printer_id)
    # Live tray identities drive backup-ON pooling only; skip the read entirely
    # when backup is OFF so the per-slot path (phase 2) is untouched.
    live_ids = _live_tray_identities(printer_id) if backup_on else {}

    # ------------------------------------------------------------------ phase 1
    # Resolve each requirement to (ams_id, tray_id, identity, remaining_grams).
    # ``identity`` is the LIVE TRAY identity at the mapped slot (``None`` when the
    # tray reports no loaded filament — spool pulled). A ``None`` remaining means
    # "couldn't determine" — treated as "no deficit" below (preserved from
    # pre-#1762 behaviour for non-backup paths too).
    @dataclass
    class _ReqRow:
        slot_id: int
        ams_id: int
        tray_id: int
        global_tray_id: int
        required: float
        identity: str | None
        remaining: float | None
        filament_type: str
        extruder: int

    resolved: list[_ReqRow] = []

    for req in requirements:
        slot_id = req.get("slot_id")
        used_grams = req.get("used_grams")
        if not isinstance(slot_id, int) or slot_id <= 0:
            continue
        if not isinstance(used_grams, (int, float)) or used_grams <= 0:
            continue
        idx = slot_id - 1
        if idx >= len(mapping):
            continue
        global_tray_id = mapping[idx]
        if global_tray_id is None or global_tray_id < 0:
            continue
        ams_id, tray_id = _global_to_ams_key(global_tray_id)

        identity = live_ids.get((ams_id, tray_id))
        remaining: float | None = None
        if spoolman_mode:
            sm_result = await db.execute(
                select(SpoolmanSlotAssignment).where(
                    SpoolmanSlotAssignment.printer_id == printer_id,
                    SpoolmanSlotAssignment.ams_id == ams_id,
                    SpoolmanSlotAssignment.tray_id == tray_id,
                )
            )
            sm_assignment = sm_result.scalar_one_or_none()
            if sm_assignment is None:
                continue
            # Live remaining_weight from Spoolman for this slot's binding. The
            # pooling identity comes from the live tray, not this fetch.
            from backend.app.services.spoolman import (
                SpoolmanClientError,
                SpoolmanNotFoundError,
                get_spoolman_client,
            )

            try:
                client = await get_spoolman_client()
                spool_dict = await client.get_spool(sm_assignment.spoolman_spool_id) if client else None
            except (SpoolmanNotFoundError, SpoolmanClientError):
                spool_dict = None
            except Exception as e:
                logger.debug("Spoolman fetch failed for spool %s: %s", sm_assignment.spoolman_spool_id, e)
                spool_dict = None
            remaining = _spoolman_grams_from_dict(spool_dict)
        else:
            internal_result = await db.execute(
                select(SpoolAssignment)
                .options(selectinload(SpoolAssignment.spool))
                .where(
                    SpoolAssignment.printer_id == printer_id,
                    SpoolAssignment.ams_id == ams_id,
                    SpoolAssignment.tray_id == tray_id,
                )
            )
            assignment = internal_result.scalar_one_or_none()
            if assignment is None or assignment.spool is None:
                continue
            spool = assignment.spool
            if spool.spent_at is not None:
                # A spent spool is KNOWN empty (remaining 0.0, not undetermined) — the
                # spent stamp no longer floors weight_used, so an under-counted ledger
                # must not read positive and let dispatch start a print on an empty roll.
                remaining = 0.0
            else:
                label_weight = float(spool.label_weight or 0)
                weight_used = float(spool.weight_used or 0)
                if label_weight <= 0:
                    continue
                remaining = max(0.0, label_weight - weight_used)

        if remaining is None:
            # Unable to determine remaining grams — preserve pre-#1762 behaviour
            # (don't block on undetermined data).
            continue

        resolved.append(
            _ReqRow(
                slot_id=slot_id,
                ams_id=ams_id,
                tray_id=tray_id,
                global_tray_id=global_tray_id,
                required=float(used_grams),
                identity=identity,
                remaining=remaining,
                filament_type=str(req.get("type", "")),
                extruder=_extruder_side_for_ams(ams_id, ams_extruder_map, is_dual),
            )
        )

    # ------------------------------------------------------------------ phase 2
    # When backup is OFF, fall back to today's per-slot accounting (one-line
    # equivalence of the original loop), so this path is a strict no-op
    # behaviour-wise vs. the pre-#1762 code.
    if not backup_on:
        return [
            FilamentDeficit(
                slot_id=row.slot_id,
                ams_id=row.ams_id,
                tray_id=row.tray_id,
                filament_type=row.filament_type,
                required_grams=row.required,
                remaining_grams=row.remaining,
            )
            for row in resolved
            if row.remaining is not None and row.remaining < row.required
        ]

    # ------------------------------------------------------------------ phase 3
    # Backup ON: pool remaining grams by LIVE TRAY identity — the same key the
    # firmware switches on (tray_info_idx/tray_type + colour), scoped per
    # extruder side. Grams for each live tray come from whatever inventory
    # binding backs it; a loaded tray with no determinable grams (unbound /
    # no-RFID spool) makes its identity's pool OPEN-ENDED (undetermined) so we
    # never block on it — mirroring the firmware, which switches to that spool
    # on runout. A requirement whose mapped tray has no live identity falls back
    # to the strict per-slot check (same as phase 2 for that slot).
    pool_by_key: dict[tuple[str, int], float] = defaultdict(float)
    required_by_key: dict[tuple[str, int], float] = defaultdict(float)
    undetermined_keys: set[tuple[str, int]] = set()

    # Determinable remaining grams for every bound slot on the printer, keyed by
    # (ams_id, tray_id). ``None`` marks a binding we can't price (missing spool,
    # non-positive label weight, or a Spoolman fetch miss).
    grams_by_slot: dict[tuple[int, int], float | None] = {}
    if spoolman_mode:
        sm_all = await db.execute(select(SpoolmanSlotAssignment).where(SpoolmanSlotAssignment.printer_id == printer_id))
        from backend.app.services.spoolman import (
            SpoolmanClientError,
            SpoolmanNotFoundError,
            get_spoolman_client,
        )

        try:
            client = await get_spoolman_client()
        except Exception:
            client = None
        for sa in sm_all.scalars().all():
            slot_key = (sa.ams_id, sa.tray_id)
            if client is None:
                grams_by_slot[slot_key] = None
                continue
            try:
                spool_dict = await client.get_spool(sa.spoolman_spool_id)
            except (SpoolmanNotFoundError, SpoolmanClientError):
                grams_by_slot[slot_key] = None
                continue
            except Exception as e:
                logger.debug("Spoolman pool fetch failed for spool %s: %s", sa.spoolman_spool_id, e)
                grams_by_slot[slot_key] = None
                continue
            grams_by_slot[slot_key] = _spoolman_grams_from_dict(spool_dict)
    else:
        internal_all = await db.execute(
            select(SpoolAssignment)
            .options(selectinload(SpoolAssignment.spool))
            .where(SpoolAssignment.printer_id == printer_id)
        )
        for assignment in internal_all.scalars().all():
            slot_key = (assignment.ams_id, assignment.tray_id)
            spool = assignment.spool
            if spool is None:
                grams_by_slot[slot_key] = None
                continue
            if spool.spent_at is not None:
                # Spent → KNOWN empty: contributes 0.0 to the pool (NOT undetermined,
                # which would make the identity open-ended and never block). The spent
                # stamp no longer floors weight_used, so the ledger can't read positive.
                grams_by_slot[slot_key] = 0.0
                continue
            label_weight = float(spool.label_weight or 0)
            weight_used = float(spool.weight_used or 0)
            if label_weight <= 0:
                grams_by_slot[slot_key] = None
                continue
            grams_by_slot[slot_key] = max(0.0, label_weight - weight_used)

    # Iterate LIVE TRAYS: each contributes its grams to the pool, or marks its
    # identity open-ended when the grams can't be determined.
    for (ams_id, tray_id), identity in live_ids.items():
        key = (identity, _extruder_side_for_ams(ams_id, ams_extruder_map, is_dual))
        grams = grams_by_slot.get((ams_id, tray_id))
        if grams is None:
            undetermined_keys.add(key)
        else:
            pool_by_key[key] += grams

    for row in resolved:
        if row.identity is not None:
            required_by_key[(row.identity, row.extruder)] += row.required

    deficits: list[FilamentDeficit] = []
    for row in resolved:
        if row.identity is None:
            # Mapped tray reports no live filament (spool pulled) → strict
            # per-slot check, exactly as the backup-OFF path would.
            if row.remaining is not None and row.remaining < row.required:
                deficits.append(
                    FilamentDeficit(
                        slot_id=row.slot_id,
                        ams_id=row.ams_id,
                        tray_id=row.tray_id,
                        filament_type=row.filament_type,
                        required_grams=row.required,
                        remaining_grams=row.remaining,
                    )
                )
            continue
        key = (row.identity, row.extruder)
        if key in undetermined_keys:
            continue  # open-ended pool (unbound peer tray) → never block
        # Pool insufficient for the print's TOTAL required of this identity on
        # this extruder side → real deficit. The per-slot remaining still gets
        # surfaced so the UI can point at the slot the user assigned.
        if pool_by_key[key] < required_by_key[key]:
            deficits.append(
                FilamentDeficit(
                    slot_id=row.slot_id,
                    ams_id=row.ams_id,
                    tray_id=row.tray_id,
                    filament_type=row.filament_type,
                    required_grams=row.required,
                    remaining_grams=row.remaining,
                )
            )

    return deficits


# Re-export the most useful pieces for callers that just want the data.
__all__ = [
    "FilamentDeficit",
    "compute_deficit_for_queue_item",
]
