"""Reused Bambu RFID tag → spent-certain auto re-spool (fork farm feature).

The farm refills spent Bambu 1 kg rolls by peeling the RFID tag onto a fresh
third-party spool. The AMS then auto-identifies the filament, but Bambuddy's
spool ledger would otherwise map the tag to the SPENT donor row (weight_used ≈
1000 g), silently stalling the lights-out queue on the #1496 filament-deficit
guard. This module is the single owner of the re-spool operation and its three
certainty tiers:

* **Tier 1 — spent-certain marking** (`mark_spent_on_runout`, `capture_backup_swap`):
  a hardware runout signal (runout HMS / seamless AMS backup-swap) stamps
  ``spool.spent_at`` — the certainty key. Never set by gram estimates.
* **Tier 2 — automatic re-spool** (`maybe_auto_or_prompt_respool`): a tag arrival
  resolving to a spent, LOADED tray physically cannot be the old (empty) spool,
  so it re-spools with no operator involvement.
* **Tier 3 — one-click prompt** (`maybe_auto_or_prompt_respool`): uncertain cases
  (spent_at NULL, remaining below threshold) broadcast a ``respool_prompt`` WS
  event mirroring the ``unknown_tag`` flow.

The core operation `respool_tag` disposes the donor, mints a fresh full
third-party spool (weight_locked, spent_at NULL), copies K-profiles, re-assigns
the slot and releases low-spool-staged farm items. All entry points no-op when
Spoolman owns the spool lifecycle (``spoolman_enabled == "true"``).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from backend.app.core.websocket import ws_manager
from backend.app.models.print_batch import PrintBatch
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spool_k_profile import SpoolKProfile
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.services.spool_tag_matcher import (
    ZERO_TAG_UID,
    ZERO_TRAY_UUID,
    auto_assign_spool,
    get_spool_by_tag,
    is_valid_tag,
    parse_tray_fields,
)
from backend.app.utils.tag_normalization import normalize_tag_uid, normalize_tray_uuid

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Tag vendor marker written on every re-spooled row. Single origin of truth so
# the sibling-tag guard and the observability hook agree on the classification.
RESPOOL_TAG_TYPE = "bambulab_reused"

# Hardware runout HMS short codes that mean "the AMS physically saw the filament
# end" — printer-side (0300_8004) and per-AMS-slot (07xx_8011). Curated to the
# same family already in main._HMS_FAILURE_REASONS; the slot-from-attr decode is
# hardware-probe-pinned, so we resolve WHICH tray ran out via the dispatched farm
# ams_mapping / live tray_now instead of the HMS attr.
RUNOUT_HMS_CODES: frozenset[str] = frozenset(
    {
        "0300_8004",
        "0700_8011",
        "0701_8011",
        "0702_8011",
        "0703_8011",
        "0704_8011",
        "0705_8011",
        "0706_8011",
        "0707_8011",
    }
)

# Per-printer last-seen loaded tray (global id) for the backup-swap detector.
# Module-level edge state matching the fork's other event-edge bookkeeping
# (farm_staging._tray_signatures). Lost on restart — worst case is one missed
# swap edge, which falls through to the Tier-3 prompt on reuse (fail-safe).
_last_tray_now: dict[int, int] = {}

# Per-printer dedup for `respool_prompt` WS broadcasts, keyed
# (ams_id, tray_id) -> (tag_uid, tray_uuid). Mirrors main._unknown_tag_last_broadcast:
# re-broadcast only when the tag tuple changes for the slot; cleared when the
# slot goes empty so remove + reinsert re-prompts.
_respool_prompt_dedup: dict[int, dict[tuple[int, int], tuple[str, str]]] = {}


def _reset_state() -> None:
    """Test hook: clear module-level edge/dedup state between cases."""
    _last_tray_now.clear()
    _respool_prompt_dedup.clear()


class RespoolError(Exception):
    """Re-spool failure carrying an HTTP status + operator-facing detail.

    The route maps this straight onto an HTTPException; the auto path catches it
    to fall back to the prompt tier instead of raising into the AMS callback.
    """

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class RespoolSiblingConflict(RespoolError):
    """The tray_uuid-matching active row is a DIFFERENT reused-tag spool.

    Bambu rolls carry two RFID tags sharing one tray_uuid. When the donor's
    sibling tag already lives on another third-party spool, proceeding would
    silently merge two physical spools (get_spool_by_tag prefers tray_uuid), so
    we refuse. Carries the conflicting spool id for an actionable message.
    """

    def __init__(self, conflicting_spool_id: int):
        self.conflicting_spool_id = conflicting_spool_id
        super().__init__(
            409,
            (
                f"Tray UUID already belongs to re-spooled spool #{conflicting_spool_id} via its sibling tag. "
                "Use ONE tag per donor roll — discard the second tag and re-spool with a different donor roll's tag."
            ),
        )


# --- setting helpers --------------------------------------------------------


async def _spoolman_enabled(db: AsyncSession) -> bool:
    from backend.app.api.routes.settings import get_setting

    value = await get_setting(db, "spoolman_enabled")
    return bool(value) and value.lower() == "true"


async def _respool_last_brand(db: AsyncSession) -> str:
    from backend.app.api.routes.settings import get_setting

    return (await get_setting(db, "respool_last_brand")) or ""


async def _respool_prompt_threshold_g(db: AsyncSession) -> int:
    from backend.app.api.routes.settings import get_setting

    raw = await get_setting(db, "respool_prompt_threshold_g")
    try:
        return int(raw) if raw is not None else 30
    except (TypeError, ValueError):
        return 30


# --- tray geometry helpers --------------------------------------------------


def _decode_global_tray(global_tray: int | None) -> tuple[int | None, int | None]:
    """Decode a global tray id to (ams_id, tray_id) for SpoolAssignment lookup.

    Mirrors the encoding used across bambu_mqtt / main: regular AMS
    ``global = ams_id*4 + slot``; AMS-HT (128-191) reports ``global == ams_id``
    (single tray); external vt_tray 254/255 maps to ams_id=255 slot 0/1 (the
    ``tray_id + 254`` convention from the auto-unlink path).
    """
    if global_tray is None or global_tray < 0:
        return (None, None)
    if global_tray in (254, 255):
        return (255, global_tray - 254)
    if 128 <= global_tray <= 191:
        return (global_tray, 0)
    if global_tray <= 127:
        return (global_tray // 4, global_tray % 4)
    return (None, None)


def _iter_ams_units(state) -> list:
    """Normalize the AMS payload in ``state.raw_data`` to a list of AMS units."""
    if not state or not getattr(state, "raw_data", None):
        return []
    ams_data = state.raw_data.get("ams")
    if isinstance(ams_data, list):
        return ams_data
    if isinstance(ams_data, dict):
        if isinstance(ams_data.get("ams"), list):
            return ams_data["ams"]
        if "tray" in ams_data:
            return [{"id": 0, "tray": ams_data.get("tray", [])}]
    return []


def _resolve_live_tray(state, ams_id: int, tray_id: int) -> dict | None:
    """Find the live tray dict for (ams_id, tray_id) from a printer state.

    Handles the external vt_tray slot (ams_id=255) via the ``tray_id + 254``
    global-id convention (main.on_ams_change) and regular AMS units via the same
    normalization ``create_spool_from_slot`` uses.
    """
    if not state or not getattr(state, "raw_data", None):
        return None
    if ams_id == 255:
        vt_tray = state.raw_data.get("vt_tray") or []
        ext_id = tray_id + 254  # 0→254, 1→255
        for vt in vt_tray:
            if isinstance(vt, dict) and int(vt.get("id", 254)) == ext_id:
                return vt
        return None
    for unit in _iter_ams_units(state):
        if not isinstance(unit, dict) or int(unit.get("id", -1)) != ams_id:
            continue
        for tray in unit.get("tray", []):
            if isinstance(tray, dict) and int(tray.get("id", -1)) == tray_id:
                return tray
    return None


def _tray_present(state, global_tray: int) -> bool:
    """Exist-bit proxy: does the tray at ``global_tray`` still hold a spool?

    The raw AMS exist-bit field is hardware-probe-pinned; until then a present
    spool is read as a non-empty ``tray_type`` in the live AMS data for the
    decoded slot. Used only by the backup-swap detector.
    """
    ams_id, tray_id = _decode_global_tray(global_tray)
    if ams_id is None:
        return False
    tray = _resolve_live_tray(state, ams_id, tray_id)
    return bool(tray and (tray.get("tray_type") or "").strip())


def _tray_loaded(tray: dict) -> bool:
    """Filament-loaded heuristic — mirrors main.on_ams_change (:1643 semantics).

    state == 11 (fed to extruder) OR a non-empty tray_type when state is NOT one
    of the firmware's explicit empty signals (9, 10). A spent spool re-inserted
    without refill reads present-but-not-loaded → False → no auto trigger.
    """
    cur_state = tray.get("state")
    cur_type = (tray.get("tray_type") or "").strip()
    return cur_state == 11 or (cur_state not in (9, 10) and bool(cur_type))


# --- Tier 1: spent-certain marking -----------------------------------------


async def _mark_tray_spent(db: AsyncSession, printer_id: int, global_tray: int) -> Spool | None:
    """Stamp spent_at on the spool assigned to the decoded slot. Idempotent."""
    ams_id, tray_id = _decode_global_tray(global_tray)
    if ams_id is None:
        return None
    result = await db.execute(
        select(SpoolAssignment)
        .options(selectinload(SpoolAssignment.spool))
        .where(
            SpoolAssignment.printer_id == printer_id,
            SpoolAssignment.ams_id == ams_id,
            SpoolAssignment.tray_id == tray_id,
        )
    )
    assignment = result.scalar_one_or_none()
    if assignment is None or assignment.spool is None:
        return None
    spool = assignment.spool
    if spool.spent_at is not None:
        return spool  # idempotent — already marked spent
    spool.spent_at = datetime.utcnow()
    # Floor weight_used to the label so the deficit guard treats it as empty even
    # if the gram ledger under-counted. This is a service-owned correction,
    # distinct from the AMS %-sync that weight_locked blocks.
    spool.weight_used = max(spool.weight_used or 0, spool.label_weight or 0)
    await db.commit()
    logger.info(
        "Marked spool %d spent (printer %d AMS%d-T%d, hardware runout)",
        spool.id,
        printer_id,
        ams_id,
        tray_id,
    )
    return spool


async def _resolve_exhausted_tray(db: AsyncSession, printer_id: int, state) -> int | None:
    """Which tray ran out: dispatched farm ams_mapping first, else live tray_now."""
    result = await db.execute(
        select(PrintQueueItem)
        .join(PrintBatch, PrintQueueItem.batch_id == PrintBatch.id)
        .where(
            PrintQueueItem.printer_id == printer_id,
            PrintQueueItem.status == "printing",
            PrintQueueItem.ams_mapping.is_not(None),
            PrintBatch.sku_file_id.is_not(None),
        )
        .order_by(PrintQueueItem.started_at.desc())
        .limit(1)
    )
    item = result.scalar_one_or_none()
    tray_now = getattr(state, "tray_now", None)
    if item and item.ams_mapping:
        try:
            mapping = json.loads(item.ams_mapping)
            feeders = [int(v) for v in mapping if isinstance(v, (int, float)) and int(v) >= 0]
        except (ValueError, TypeError):
            feeders = []
        if len(feeders) == 1:
            return feeders[0]  # single-feeder farm job — deterministic
        if feeders:
            # Multi-filament job: the mapping alone can't say WHICH feeder ran
            # out. Trust the live tray_now only when it is one of the job's
            # feeders; otherwise mark nothing (fail-safe — a wrong spent stamp
            # would auto-reset a half-full spool to fresh on its next arrival).
            if tray_now is not None and tray_now in feeders:
                return tray_now
            return None
    if tray_now is not None and 0 <= tray_now <= 254:
        return tray_now
    return None


async def mark_spent_on_runout(db: AsyncSession, printer_id: int, new_short_codes, state) -> Spool | None:
    """Tier 1: a NEW runout HMS code stamps spent_at on the exhausted tray's spool.

    Resolves the exhausted tray via the dispatched farm ``ams_mapping`` (the
    deterministic feeding tray) falling back to the live ``tray_now``. Idempotent:
    re-observing the code is a no-op once spent_at is set. No-op in Spoolman mode.
    """
    if await _spoolman_enabled(db):
        return None
    if not (set(new_short_codes) & RUNOUT_HMS_CODES):
        return None
    global_tray = await _resolve_exhausted_tray(db, printer_id, state)
    if global_tray is None:
        return None
    return await _mark_tray_spent(db, printer_id, global_tray)


async def capture_backup_swap(db: AsyncSession, printer_id: int, state) -> Spool | None:
    """Tier 1: seamless AMS backup-swap detector (runout with no HMS).

    During a RUNNING print, when ``tray_now`` leaves a tray whose spool is still
    physically present (exist-bit proxy), that tray ran out and the AMS switched
    to a backup — mark it spent. Tracks the per-printer last tray_now edge in a
    module dict (farm_staging precedent). No-op in Spoolman mode.
    """
    if await _spoolman_enabled(db):
        return None
    current = getattr(state, "tray_now", 255)
    if getattr(state, "state", None) != "RUNNING":
        # Only meaningful mid-print, but keep the edge tracker current so the
        # first RUNNING push after an idle period doesn't fire a false swap.
        _last_tray_now[printer_id] = current
        return None
    prev = _last_tray_now.get(printer_id)
    _last_tray_now[printer_id] = current
    if prev is None or prev == current:
        return None
    if prev < 0 or prev >= 254:
        return None  # unloaded / external sentinel — not a backup-swap edge
    if not _tray_present(state, prev):
        return None  # spool physically gone → an ordinary unload, not a runout
    return await _mark_tray_spent(db, printer_id, prev)


# --- Tier 2 / 3: automatic re-spool or prompt ------------------------------


def clear_respool_prompt_dedup(printer_id: int, ams_id: int, tray_id: int) -> None:
    """Drop the cached prompt tag for a slot (called when the slot reports empty)."""
    per_printer = _respool_prompt_dedup.get(printer_id)
    if per_printer is None:
        return
    per_printer.pop((ams_id, tray_id), None)


def _count_trays_in_ams(state, ams_id: int) -> int:
    for unit in _iter_ams_units(state):
        if isinstance(unit, dict) and int(unit.get("id", -1)) == ams_id:
            return len(unit.get("tray", []) or [])
    return 0


async def _broadcast_respool_prompt(
    db: AsyncSession,
    printer_id: int,
    ams_id: int,
    tray_id: int,
    tray: dict,
    donor: Spool,
) -> None:
    """Broadcast a deduped ``respool_prompt`` WS event (frozen contract)."""
    from backend.app.services.printer_manager import printer_manager

    slot_key = (ams_id, tray_id)
    tag_uid = tray.get("tag_uid") or ""
    tray_uuid = tray.get("tray_uuid") or ""
    tag_key = (tag_uid, tray_uuid)
    per_printer = _respool_prompt_dedup.setdefault(printer_id, {})
    if per_printer.get(slot_key) == tag_key:
        return

    state = printer_manager.get_status(printer_id)
    tray_count = _count_trays_in_ams(state, ams_id) if ams_id != 255 else 0

    tray_weight = tray.get("tray_weight")
    try:
        label_weight_prefill = int(tray_weight) if tray_weight else int(donor.label_weight or 1000)
    except (TypeError, ValueError):
        label_weight_prefill = int(donor.label_weight or 1000)

    brand_prefill = (await _respool_last_brand(db)) or None
    donor_remaining = float((donor.label_weight or 0) - (donor.weight_used or 0))

    # Broadcast first; only commit the dedup if the WS write succeeds (mirrors
    # main._broadcast_unknown_tag so a failed push retries on the next tick).
    await ws_manager.broadcast(
        {
            "type": "respool_prompt",
            "printer_id": printer_id,
            "ams_id": ams_id,
            "tray_id": tray_id,
            "tag_uid": tag_uid or None,
            "tray_uuid": tray_uuid or None,
            "tray_type": tray.get("tray_type") or None,
            "tray_color": tray.get("tray_color") or None,
            "tray_sub_brands": tray.get("tray_sub_brands") or None,
            "tray_count": tray_count,
            "donor_spool_id": donor.id,
            "donor_remaining_g": donor_remaining,
            "brand_prefill": brand_prefill,
            "label_weight_prefill": label_weight_prefill,
        }
    )
    per_printer[slot_key] = tag_key
    logger.info(
        "respool_prompt broadcast: printer=%d AMS=%d slot=%d donor=%d remaining=%.1fg",
        printer_id,
        ams_id,
        tray_id,
        donor.id,
        donor_remaining,
    )


async def maybe_auto_or_prompt_respool(
    db: AsyncSession,
    printer_id: int,
    ams_id: int,
    tray_id: int,
    tray: dict,
    spool: Spool,
) -> Spool | None:
    """Tier 2/3 gate for a tag arrival that resolved to inventory ``spool``.

    * Tier 2 (auto): ``spool.spent_at`` set AND the tray is LOADED → the physical
      spool cannot be the spent one, so re-spool with the server-held last brand
      and return the NEW spool (the caller must skip its own auto-assign — the
      re-spool already re-assigned the slot). Empty brand or a sibling conflict
      falls through to the prompt instead.
    * Tier 3 (prompt): ``spent_at`` NULL and remaining ≤ threshold → broadcast a
      deduped ``respool_prompt`` and return None (existing auto-assign proceeds).
    * Otherwise: None (no-op).

    No-op in Spoolman mode.
    """
    if await _spoolman_enabled(db):
        return None

    if spool.spent_at is not None:
        if not _tray_loaded(tray):
            return None  # spent but not loaded → dead spool re-inserted, no trigger
        brand = (await _respool_last_brand(db)).strip()
        if not brand:
            # No prefill brand → can't auto safely; surface the one-click prompt.
            await _broadcast_respool_prompt(db, printer_id, ams_id, tray_id, tray, spool)
            return None
        try:
            new_spool = await respool_tag(
                db,
                printer_id=printer_id,
                ams_id=ams_id,
                tray_id=tray_id,
                brand=brand,
            )
            logger.info(
                "Auto re-spooled tag on printer %d AMS%d-T%d: donor #%d → spool #%d",
                printer_id,
                ams_id,
                tray_id,
                spool.id,
                new_spool.id,
            )
            return new_spool
        except RespoolSiblingConflict as exc:
            logger.warning(
                "Auto re-spool skipped on printer %d AMS%d-T%d (sibling-tag conflict): %s",
                printer_id,
                ams_id,
                tray_id,
                exc.detail,
            )
            await _broadcast_respool_prompt(db, printer_id, ams_id, tray_id, tray, spool)
            return None
        except RespoolError as exc:
            logger.warning(
                "Auto re-spool failed on printer %d AMS%d-T%d: %s",
                printer_id,
                ams_id,
                tray_id,
                exc.detail,
            )
            return None

    # Tier 3: uncertain — spent_at NULL, remaining near-empty → one-click prompt.
    remaining = (spool.label_weight or 0) - (spool.weight_used or 0)
    threshold = await _respool_prompt_threshold_g(db)
    if remaining <= threshold:
        await _broadcast_respool_prompt(db, printer_id, ams_id, tray_id, tray, spool)
    return None


# --- Core operation ---------------------------------------------------------


async def respool_tag(
    db: AsyncSession,
    *,
    printer_id: int,
    ams_id: int,
    tray_id: int,
    brand: str,
    label_weight: int | None = None,
    cost_per_kg: float | None = None,
    note: str | None = None,
) -> Spool:
    """Re-spool a reused Bambu tag onto a fresh full third-party spool.

    Resolves the live tray, guards against a sibling-tag merge, disposes the
    donor (hard-delete a pristine drive-by auto-create, else archive), mints a
    fresh full spool (weight_used=0, weight_locked, spent_at NULL,
    tag_type=bambulab_reused), copies the donor's K-profiles, re-assigns the AMS
    slot, updates the last-brand prefill, commits, and releases low-spool-staged
    farm items. Broadcasts ``spool_respooled``.

    Raises :class:`RespoolError` (404 not connected / 400 empty-or-no-tag) or
    :class:`RespoolSiblingConflict` (409) — the caller maps these to HTTP or the
    prompt fallback.
    """
    from backend.app.services.printer_manager import printer_manager

    # 1. Resolve the live tray + tag identity.
    state = printer_manager.get_status(printer_id)
    if not state or not getattr(state, "raw_data", None):
        raise RespoolError(404, "Printer not connected or no live state available")
    tray = _resolve_live_tray(state, ams_id, tray_id)
    if not tray or not tray.get("tray_type"):
        raise RespoolError(400, "Slot is empty or has no readable tray data")
    scan_tag_uid = tray.get("tag_uid", "")
    scan_tray_uuid = tray.get("tray_uuid", "")
    if not is_valid_tag(scan_tag_uid, scan_tray_uuid):
        raise RespoolError(400, "Slot has no valid RFID tag")
    norm_uid = normalize_tag_uid(scan_tag_uid)
    norm_uuid = normalize_tray_uuid(scan_tray_uuid)

    # 2. Sibling-tag guard + donor resolution. get_spool_by_tag prefers tray_uuid,
    # so a tray_uuid match that is itself a reused-type row with a DIFFERENT
    # tag_uid is the donor's sibling already living on another spool → refuse.
    donor = await get_spool_by_tag(db, scan_tag_uid, scan_tray_uuid)
    if (
        donor is not None
        and donor.tag_type == RESPOOL_TAG_TYPE
        and norm_uuid
        and normalize_tray_uuid(donor.tray_uuid or "") == norm_uuid
    ):
        donor_uid = normalize_tag_uid(donor.tag_uid or "")
        if norm_uid and donor_uid and donor_uid != norm_uid:
            raise RespoolSiblingConflict(donor.id)

    # Idempotency: the resolved row is ALREADY the fresh re-spooled record for
    # this very tag (double-submit, or the auto path racing a manual confirm) —
    # disposing it and minting another would churn a duplicate. Return it
    # unchanged; a brand correction goes through the normal spool edit.
    if (
        donor is not None
        and donor.tag_type == RESPOOL_TAG_TYPE
        and donor.spent_at is None
        and not (donor.weight_used or 0)
        and norm_uid
        and normalize_tag_uid(donor.tag_uid or "") == norm_uid
    ):
        logger.info("Re-spool no-op: spool %d is already the fresh record for tag %s", donor.id, norm_uid)
        return donor

    # Capture everything needed from the donor BEFORE disposal (a pristine donor
    # is hard-deleted, which cascade-removes its K-profiles).
    donor_id: int | None = donor.id if donor else None
    donor_kprofiles: list[dict] = []
    donor_fields: dict = {}
    if donor is not None:
        donor_fields = {
            "material": donor.material,
            "subtype": donor.subtype,
            "color_name": donor.color_name,
            "rgba": donor.rgba,
            "extra_colors": donor.extra_colors,
            "effect_type": donor.effect_type,
            "core_weight": donor.core_weight,
            "core_weight_catalog_id": donor.core_weight_catalog_id,
            "slicer_filament": donor.slicer_filament,
            "slicer_filament_name": donor.slicer_filament_name,
            "nozzle_temp_min": donor.nozzle_temp_min,
            "nozzle_temp_max": donor.nozzle_temp_max,
            "label_weight": donor.label_weight,
        }
        for kp in donor.k_profiles:
            donor_kprofiles.append(
                {
                    "printer_id": kp.printer_id,
                    "extruder": kp.extruder,
                    "nozzle_diameter": kp.nozzle_diameter,
                    "nozzle_type": kp.nozzle_type,
                    "k_value": kp.k_value,
                    "name": kp.name,
                    "cali_idx": kp.cali_idx,
                    "setting_id": kp.setting_id,
                }
            )

    # 3. Dispose the donor: strip tags, drop its slot assignments, then
    # hard-delete a pristine drive-by auto-create or archive a ledger-bearing row.
    if donor is not None:
        donor.tag_uid = None
        donor.tray_uuid = None
        for assignment in list(donor.assignments):
            await db.delete(assignment)
        await db.flush()

        history_count = await db.scalar(
            select(func.count(SpoolUsageHistory.id)).where(SpoolUsageHistory.spool_id == donor.id)
        )
        if donor.data_origin == "rfid_auto" and not history_count:
            await db.delete(donor)  # pristine auto-create — no ledger to preserve
            disposition = "hard-deleted"
        else:
            donor.archived_at = datetime.utcnow()
            disposition = "archived"
        await db.flush()
    else:
        disposition = "none"

    # 4. Mint the fresh full third-party spool. Identity from the donor when we
    # have it, else parsed straight from the tray (shared helper).
    if donor_fields:
        source = donor_fields
    else:
        parsed = await parse_tray_fields(db, tray)
        source = {
            "material": parsed.material,
            "subtype": parsed.subtype,
            "color_name": parsed.color_name,
            "rgba": parsed.rgba,
            "extra_colors": None,
            "effect_type": None,
            "core_weight": parsed.core_weight,
            "core_weight_catalog_id": None,
            "slicer_filament": parsed.slicer_filament,
            "slicer_filament_name": parsed.slicer_filament_name,
            "nozzle_temp_min": parsed.nozzle_temp_min,
            "nozzle_temp_max": parsed.nozzle_temp_max,
            "label_weight": parsed.label_weight,
        }

    final_label_weight = int(label_weight) if label_weight else int(source["label_weight"] or 1000)

    new_spool = Spool(
        material=source["material"],
        subtype=source["subtype"],
        color_name=source["color_name"],
        rgba=source["rgba"],
        extra_colors=source["extra_colors"],
        effect_type=source["effect_type"],
        brand=brand,
        label_weight=final_label_weight,
        core_weight=source["core_weight"] or 250,
        core_weight_catalog_id=source["core_weight_catalog_id"],
        weight_used=0,  # fresh full spool by definition
        weight_locked=True,  # neutralize the donor tag's stale AMS remain%
        spent_at=None,
        slicer_filament=source["slicer_filament"],
        slicer_filament_name=source["slicer_filament_name"],
        nozzle_temp_min=source["nozzle_temp_min"],
        nozzle_temp_max=source["nozzle_temp_max"],
        tag_uid=norm_uid if norm_uid and norm_uid != ZERO_TAG_UID else None,
        tray_uuid=norm_uuid if norm_uuid and norm_uuid != ZERO_TRAY_UUID else None,
        data_origin="rfid_linked",
        tag_type=RESPOOL_TAG_TYPE,
        cost_per_kg=cost_per_kg,
        note=note,
    )
    # Initialize relationships before add() to avoid a lazy load in async context
    # (SpoolAssignment back_populates resolution runs synchronously — see #612).
    new_spool.k_profiles = []
    new_spool.assignments = []
    db.add(new_spool)
    await db.flush()

    # 5. Copy donor K-profiles (same-performance filament per the operator).
    for kp in donor_kprofiles:
        db.add(SpoolKProfile(spool_id=new_spool.id, **kp))
    await db.flush()

    # 6. Assign the slot + re-apply K-profile via MQTT.
    await auto_assign_spool(
        printer_id,
        ams_id,
        tray_id,
        new_spool,
        printer_manager,
        db,
        tray_info_idx=tray.get("tray_info_idx", ""),
    )

    # 7. Persist the last-brand prefill and commit the atomic unit (3-6).
    from backend.app.api.routes.settings import set_setting

    await set_setting(db, "respool_last_brand", brand)
    await db.commit()
    logger.info(
        "Re-spooled tag on printer %d AMS%d-T%d: donor %s (%s) → fresh spool %d (%s, %dg, locked)",
        printer_id,
        ams_id,
        tray_id,
        donor_id if donor_id is not None else "none",
        disposition,
        new_spool.id,
        brand,
        final_label_weight,
    )

    await ws_manager.broadcast(
        {
            "type": "spool_respooled",
            "printer_id": printer_id,
            "ams_id": ams_id,
            "tray_id": tray_id,
            "donor_spool_id": donor_id,
            "new_spool_id": new_spool.id,
            "brand": brand,
            "label_weight": final_label_weight,
        }
    )

    # 8. Release low-spool-staged farm units without waiting for an AMS push
    # (commits internally, per-item fail-safe).
    from backend.app.services.farm_staging import release_filament_staged

    await release_filament_staged(db, printer_id)

    # The dedup for this slot is stale now the tag maps to a fresh spool.
    clear_respool_prompt_dedup(printer_id, ams_id, tray_id)
    return new_spool
