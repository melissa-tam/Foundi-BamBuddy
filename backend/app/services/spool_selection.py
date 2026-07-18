"""Spool-selection policy owner for farm dispatch.

Single source of truth for *which loaded AMS spool starts a print* when more
than one tray satisfies a filament requirement. Extracted from
``print_scheduler`` so the policy lives in one testable place and the scheduler
keeps thin delegating methods.

Three policies (``SELECTION_POLICIES``), chosen by the ``spool_selection_policy``
setting:

* ``slot_order`` — legacy AMS-slot emission order (no sort).
* ``lowest_remaining`` — prefer the most-spent matching spool. Gated on the
  printer's AMS Filament Backup so we never sort toward a near-empty spool the
  printer can't switch away from (#1766 — see :func:`effective_policy`).
* ``first_loaded`` (farm default) — FIFO by first-in-service time
  (``Spool.first_loaded_at`` / Spoolman ``first_used``), so the oldest roll is
  drained first. When AMS Backup can't cover a mid-print switch, a *smart-cover*
  partition keeps a candidate that can finish the job on its own ahead of an
  older one that would run dry.

Plus a minimum-start-weight rule (``min_start_spool_g``): a spool whose *known*
remaining grams fall below the floor can never be the STARTING spool of a print
(it stays as a firmware backup donor). When the only otherwise-matching spool is
below the floor the requirement's slot is reported in
:attr:`MatchOutcome.start_blocked_slots` so the caller can stage the job with a
distinct reason instead of silently dispatching or falling back to a mismatch.

Above every policy sits a hard exclude of unusable spools: a spool flagged with a
mid-print feed fault (``Spool.feed_fault_at`` → :attr:`SlotInventory.out_of_rotation`)
OR a spent spool (``Spool.spent_at`` → :attr:`SlotInventory.spent`) is removed from
the candidate set before any eligibility split, so it can never start a print, be
staged, or surface in ``start_blocked_slots`` — it is simply invisible to selection
until the condition clears. This exclusion is unconditional: a jammed or spent spool
never starts a print regardless of the selection policy or the minimum-start floor.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment
from backend.app.utils.filament_types import canonical_filament_type

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.app.models.print_queue import PrintQueueItem

logger = logging.getLogger(__name__)

# Public policy surface. Keep in lock-step with AppSettings.spool_selection_policy
# / min_start_spool_g (guarded by test_spool_selection.py's defaults-drift test).
SELECTION_POLICIES: tuple[str, ...] = ("slot_order", "lowest_remaining", "first_loaded")
DEFAULT_SELECTION_POLICY = "first_loaded"
DEFAULT_MIN_START_SPOOL_G = 120
# Machine waiting_reason token for a job held because its starting spool is below
# the minimum-start floor. Rendered by QueuePage; released by farm_staging.
WAITING_REASON_START_MIN = "start_spool_below_minimum"


@dataclass
class SlotInventory:
    """Per-slot inventory facts used by the selection policies.

    ``remaining_g`` is the operator's authoritative remaining weight (Bambuddy
    ``label_weight - weight_used`` or Spoolman ``remaining_weight``); ``None``
    when the slot has no inventory binding (the sort then falls back to the MQTT
    ``remain`` percentage). ``first_loaded_ord`` is epoch seconds of
    ``COALESCE(first_loaded_at, created_at)`` (Spoolman: ``first_used``) — the
    FIFO ordinal; ``None`` when unknown/unbound.

    ``out_of_rotation`` is the feed-fault hard-exclude flag: a spool flagged with a
    mid-print feed fault (jam / tangle) is out of service and must never be selected.
    ``spent`` is the run-dry hard-exclude flag: a spool marked spent has no filament
    left to start with. Both are kept SEPARATE (their log/operator semantics differ)
    and both hard-exclude the slot. Only the internal inventory mode can set them
    (``Spool.feed_fault_at`` / ``Spool.spent_at``); Spoolman has no such concept, so
    they stay ``False`` there.
    """

    remaining_g: float | None
    first_loaded_ord: float | None
    out_of_rotation: bool = False
    spent: bool = False


@dataclass
class MatchOutcome:
    """Result of matching required filaments to loaded slots.

    ``mapping`` is the AMS mapping array (position = slot_id - 1, value =
    global_tray_id or -1), or ``None`` when nothing to map. ``start_blocked_slots``
    lists slot_ids that had NO eligible spool solely because every otherwise-
    matching candidate was below the minimum-start floor (a dropped candidate
    WOULD have matched) — the distinct "start spool below minimum" signal.
    """

    mapping: list[int] | None
    start_blocked_slots: list[int] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Colour helpers (canonical home; PrintScheduler delegates here). Kept module
# level so both the matcher and the scheduler's force-colour paths share one
# implementation.
# ---------------------------------------------------------------------------
def normalize_color_for_compare(color: str | None) -> str:
    """Normalize a colour for comparison (lowercase, no hash, RGB only)."""
    if not color:
        return ""
    return color.replace("#", "").lower()[:6]


def colors_are_similar(color1: str | None, color2: str | None, threshold: int = 40) -> bool:
    """True if two colours are within ``threshold`` on every RGB channel."""
    hex1 = normalize_color_for_compare(color1)
    hex2 = normalize_color_for_compare(color2)
    if not hex1 or not hex2 or len(hex1) < 6 or len(hex2) < 6:
        return False
    try:
        r1, g1, b1 = int(hex1[0:2], 16), int(hex1[2:4], 16), int(hex1[4:6], 16)
        r2, g2, b2 = int(hex2[0:2], 16), int(hex2[2:4], 16), int(hex2[4:6], 16)
    except ValueError:
        return False
    return abs(r1 - r2) <= threshold and abs(g1 - g2) <= threshold and abs(b1 - b2) <= threshold


# ---------------------------------------------------------------------------
# Slot ordering
# ---------------------------------------------------------------------------
def slot_priority(ams_id: int | None, tray_id: int | None) -> int:
    """Deterministic slot-position tie-breaker.

    Three bands so regular AMS < AMS-HT < external on ties, regardless of the
    raw ``ams_id`` (in particular external/VT ``ams_id = -1`` must NOT sort to a
    negative number and beat AMS slot 0):

    - Regular AMS (``ams_id`` 0..7): ``ams_id * 4 + tray_id`` → 0..31
    - AMS-HT (``ams_id`` >= 128, single tray): ``1000 + (ams_id - 128) * 4``
    - External / VT (``ams_id`` < 0 or ``None``): ``10_000``
    """
    if ams_id is None or ams_id < 0:
        return 10_000
    if ams_id >= 128:
        return 1_000 + (ams_id - 128) * 4 + (tray_id or 0)
    return ams_id * 4 + (tray_id or 0)


def effective_policy(policy: str | None, ams_filament_backup: bool | None) -> str:
    """Resolve the runtime policy, applying the #1766 AMS-Backup gate.

    ``lowest_remaining`` requires the printer to be able to switch to a backup
    spool when the picked one runs out; with backup explicitly OFF, sorting
    toward the lowest would strand the print, so it degrades to ``slot_order``.
    ``None`` (unknown / A1 family) preserves the requested policy. ``first_loaded``
    and ``slot_order`` pass through (first_loaded's backup handling is the
    smart-cover partition in :func:`match_filaments_to_slots`). An unknown policy
    string falls back to the default.
    """
    if policy not in SELECTION_POLICIES:
        return DEFAULT_SELECTION_POLICY
    if policy == "lowest_remaining" and ams_filament_backup is False:
        return "slot_order"
    return policy


def _lowest_remaining_key(f: dict, inv: dict[int, SlotInventory] | None) -> tuple[int, float, int]:
    """Two-tier sort key: inventory-tracked spools before MQTT-only ones, then
    ascending by remaining, then slot position. Grams vs percent never compare
    because the tier flag dominates; unknown MQTT ``remain`` maps to 101."""
    gtid = f.get("global_tray_id")
    prio = slot_priority(f.get("ams_id"), f.get("tray_id"))
    si = inv.get(gtid) if inv else None
    if si is not None and si.remaining_g is not None:
        return (0, si.remaining_g, prio)
    remain = f.get("remain", -1)
    return (1, float(remain) if remain is not None and remain >= 0 else 101.0, prio)


def _first_loaded_key(f: dict, inv: dict[int, SlotInventory] | None) -> tuple[int, float, int]:
    """FIFO sort key: spools with a known first-loaded ordinal first (ascending,
    oldest first), unbound trays last, slot position as the final tie-break."""
    gtid = f.get("global_tray_id")
    prio = slot_priority(f.get("ams_id"), f.get("tray_id"))
    si = inv.get(gtid) if inv else None
    if si is not None and si.first_loaded_ord is not None:
        return (0, si.first_loaded_ord, prio)
    return (1, 0.0, prio)


def _sort_candidates(candidates: list[dict], policy: str, inv: dict[int, SlotInventory] | None) -> None:
    """Sort ``candidates`` in place by the policy key. ``slot_order`` is a no-op
    (preserves emission order)."""
    if policy == "lowest_remaining":
        candidates.sort(key=lambda f: _lowest_remaining_key(f, inv))
    elif policy == "first_loaded":
        candidates.sort(key=lambda f: _first_loaded_key(f, inv))


def _known_remaining(f: dict, inv: dict[int, SlotInventory] | None) -> float | None:
    """Inventory-known remaining grams for a slot, else ``None`` (unknown/unbound
    — the MQTT ``remain`` percentage is NOT used here because it isn't grams)."""
    si = inv.get(f.get("global_tray_id")) if inv else None
    if si is not None and si.remaining_g is not None:
        return si.remaining_g
    return None


def _covers(f: dict, inv: dict[int, SlotInventory] | None, used_grams: float | None) -> bool:
    """True if ``f`` can supply the whole requirement on its own. Unknown
    remaining (or unknown requirement) ⇒ assumed covering."""
    if used_grams is None:
        return True
    rem = _known_remaining(f, inv)
    if rem is None:
        return True
    return rem >= used_grams


def _scan_candidates(req: dict, candidates: list[dict]) -> dict | None:
    """Bucket-precedence match over a pre-sorted, nozzle-filtered, not-yet-used
    candidate list: unique tray_info_idx > exact colour > similar colour >
    type-only. Returns the chosen loaded-filament dict, or ``None``."""
    req_type = (req.get("type") or "").upper()
    req_color = req.get("color", "")
    req_tray_info_idx = req.get("tray_info_idx", "")

    idx_match = exact_match = similar_match = type_only_match = None

    if req_tray_info_idx:
        idx_matches = [f for f in candidates if f.get("tray_info_idx") == req_tray_info_idx]
        if len(idx_matches) == 1:
            idx_match = idx_matches[0]
        elif len(idx_matches) > 1:
            # Multiple trays share the preset id — colour-match within the subset
            # (already policy-sorted, so filtering keeps the intended order).
            for f in idx_matches:
                f_color = f.get("color", "")
                if normalize_color_for_compare(f_color) == normalize_color_for_compare(req_color):
                    if not exact_match:
                        exact_match = f
                elif colors_are_similar(f_color, req_color):
                    if not similar_match:
                        similar_match = f
                elif not type_only_match:
                    type_only_match = f

    if not idx_match and not exact_match and not similar_match and not type_only_match:
        for f in candidates:
            f_type = (f.get("type") or "").upper()
            if canonical_filament_type(f_type) != canonical_filament_type(req_type):
                continue
            f_color = f.get("color", "")
            if normalize_color_for_compare(f_color) == normalize_color_for_compare(req_color):
                if not exact_match:
                    exact_match = f
            elif colors_are_similar(f_color, req_color):
                if not similar_match:
                    similar_match = f
            elif not type_only_match:
                type_only_match = f

    return idx_match or exact_match or similar_match or type_only_match


def match_filaments_to_slots(
    required: list[dict],
    loaded: list[dict],
    *,
    policy: str,
    inv: dict[int, SlotInventory] | None,
    backup_on: bool | None,
    min_start_g: int,
) -> MatchOutcome:
    """Match required filaments to loaded slots under the given policy.

    Bucket precedence (unique tray_info_idx > exact colour > similar colour >
    type-only) and the nozzle filter are UNCHANGED from the legacy matcher. On
    top, per requirement over the not-yet-used candidates:

    0. Unusable candidates are hard-excluded up front, so a jammed or spent spool
       never starts a print regardless of the policy or the floor: out-of-rotation
       (``SlotInventory.out_of_rotation`` — a jammed / feed-fault spool) and spent
       (``SlotInventory.spent`` — a run-dry spool). They enter neither ``eligible``
       nor ``dropped``, so they can never match nor appear in ``start_blocked_slots``.
    1. ``min_start_g > 0`` drops candidates whose *known* remaining is below the
       floor into a ``dropped`` reserve (unknown/unbound stay eligible).
    2. Eligible candidates are sorted by the policy key (``slot_order`` = none).
    3. ``first_loaded`` with backup not ON stable-partitions eligible into
       covering-first (a candidate covers when its remaining is unknown or
       >= the requirement's ``used_grams``), FIFO within each half.
    4. The bucket scan runs on eligible; on a miss, if a dropped candidate WOULD
       have matched, the slot is recorded start-blocked.
    """
    if not required:
        return MatchOutcome(mapping=None)

    trace = policy != "slot_order" or min_start_g > 0
    used_tray_ids: set[int] = set()
    comparisons: list[dict] = []
    start_blocked_slots: list[int] = []

    for req in required:
        slot_id = req.get("slot_id", 0)
        available = [f for f in loaded if f["global_tray_id"] not in used_tray_ids]

        # (0) Unusable-spool hard exclude: a jammed / feed-fault spool
        # (out_of_rotation) OR a spent spool leaves the candidate set entirely
        # BEFORE any eligibility split, so neither can ever start a print, be
        # staged, or surface in start_blocked_slots — regardless of the policy or
        # the minimum-start floor. The two reasons are kept SEPARATE so the trace
        # names why a slot vanished (jam vs run-dry differ operationally).
        excluded_oor: list[int] = []
        excluded_spent: list[int] = []
        if inv:
            kept: list[dict] = []
            for f in available:
                si = inv.get(f["global_tray_id"])
                if si is not None and si.out_of_rotation:
                    excluded_oor.append(f["global_tray_id"])
                elif si is not None and si.spent:
                    excluded_spent.append(f["global_tray_id"])
                else:
                    kept.append(f)
            available = kept

        # Nozzle-aware hard filter (cross-nozzle assignment fails the print).
        req_nozzle_id = req.get("nozzle_id")
        if req_nozzle_id is not None:
            available = [f for f in available if f.get("extruder_id") == req_nozzle_id]

        # (1) minimum-start floor: reserve known-low candidates as backup donors.
        if min_start_g > 0:
            eligible: list[dict] = []
            dropped: list[dict] = []
            for f in available:
                rem = _known_remaining(f, inv)
                if rem is not None and rem < min_start_g:
                    dropped.append(f)
                else:
                    eligible.append(f)
        else:
            eligible = list(available)
            dropped = []

        # (2) policy sort.
        _sort_candidates(eligible, policy, inv)

        # (3) first_loaded smart-cover partition when backup can't rescue a switch.
        if policy == "first_loaded" and backup_on is not True:
            used_grams = req.get("used_grams")
            covering = [f for f in eligible if _covers(f, inv, used_grams)]
            non_covering = [f for f in eligible if not _covers(f, inv, used_grams)]
            eligible = covering + non_covering

        if trace:
            logger.info(
                "[spool-select] slot=%s type=%r color=%r tii=%r nozzle=%s policy=%s min_start=%s; "
                "eligible=%s dropped=%s excluded_oor=%s excluded_spent=%s",
                slot_id,
                req_type_repr(req),
                req.get("color", ""),
                req.get("tray_info_idx", ""),
                req_nozzle_id,
                policy,
                min_start_g,
                _trace_rows(eligible, inv),
                _trace_rows(dropped, inv),
                excluded_oor,
                excluded_spent,
            )

        # (4) bucket scan on eligible; dropped-only match ⇒ start-blocked.
        match = _scan_candidates(req, eligible)
        if match is None and dropped:
            _sort_candidates(dropped, policy, inv)
            if _scan_candidates(req, dropped) is not None:
                start_blocked_slots.append(slot_id)
                if trace:
                    logger.info(
                        "[spool-select] slot=%s START-BLOCKED — only match(es) below %s g floor (kept as backup)",
                        slot_id,
                        min_start_g,
                    )

        if match:
            used_tray_ids.add(match["global_tray_id"])
            comparisons.append({"slot_id": slot_id, "global_tray_id": match["global_tray_id"]})
            if trace:
                logger.info("[spool-select] slot=%s -> picked gtid=%s", slot_id, match["global_tray_id"])
        else:
            comparisons.append({"slot_id": slot_id, "global_tray_id": -1})
            if trace and slot_id not in start_blocked_slots:
                logger.info("[spool-select] slot=%s -> NO MATCH", slot_id)

    if not comparisons:
        return MatchOutcome(mapping=None, start_blocked_slots=start_blocked_slots)

    max_slot_id = max(c["slot_id"] for c in comparisons)
    if max_slot_id <= 0:
        return MatchOutcome(mapping=None, start_blocked_slots=start_blocked_slots)

    mapping = [-1] * max_slot_id
    for c in comparisons:
        sid = c["slot_id"]
        if sid and sid > 0:
            mapping[sid - 1] = c["global_tray_id"]
    return MatchOutcome(mapping=mapping, start_blocked_slots=start_blocked_slots)


def req_type_repr(req: dict) -> str:
    """The upper-cased requirement type (small helper so the trace log matches
    the matcher's comparison casing)."""
    return (req.get("type") or "").upper()


def _trace_rows(rows: list[dict], inv: dict[int, SlotInventory] | None) -> list[dict]:
    """Compact per-candidate view for the decision-trace INFO log."""
    out = []
    for f in rows:
        gtid = f.get("global_tray_id")
        si = inv.get(gtid) if inv else None
        out.append(
            {
                "gtid": gtid,
                "type": f.get("type"),
                "color": f.get("color"),
                "tii": f.get("tray_info_idx"),
                "remain": f.get("remain"),
                "inv_g": si.remaining_g if si else None,
                "first_ord": si.first_loaded_ord if si else None,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Slot-inventory construction (extends the legacy _build_inventory_remain_overrides
# with the first-loaded ordinal, in one query per mode).
# ---------------------------------------------------------------------------
def _dt_to_epoch(dt: datetime | None) -> float | None:
    """Epoch seconds for a datetime (naive treated as its stored wall clock —
    consistent across all spools, which is all FIFO ordering needs)."""
    if dt is None:
        return None
    try:
        return dt.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def _iso_to_epoch(value: str | None) -> float | None:
    """Epoch seconds for an ISO-8601 string (Spoolman ``first_used``)."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except (ValueError, TypeError):
        return None


def epoch_to_iso(ord_: float | None) -> str | None:
    """UTC ISO-8601 rendering of a first-loaded ordinal for API responses."""
    if ord_ is None:
        return None
    try:
        return datetime.fromtimestamp(ord_, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


async def _is_spoolman_mode(db: AsyncSession) -> bool:
    """Single source: reuse the deficit service's mode check."""
    from backend.app.services.filament_deficit import _is_spoolman_mode as _mode

    return await _mode(db)


async def build_slot_inventory(db: AsyncSession, printer_id: int, loaded: list[dict]) -> dict[int, SlotInventory]:
    """Return ``{global_tray_id: SlotInventory}`` for AMS slots bound to an
    inventory spool (Bambuddy-side or Spoolman-side).

    Extends the legacy inventory-remain lookup with the first-loaded ordinal so
    the FIFO policy has a durable ordering signal. External / virtual-tray slots
    are skipped (tracked separately). Slots without a binding are absent from the
    map — the caller falls back to MQTT ``remain`` for those. Best-effort: an
    empty map on any failure.
    """
    if not loaded:
        return {}
    tracked_slots = [(f["ams_id"], f["tray_id"], f["global_tray_id"]) for f in loaded if not f.get("is_external")]
    if not tracked_slots:
        return {}

    out: dict[int, SlotInventory] = {}

    if await _is_spoolman_mode(db):
        result = await db.execute(select(SpoolmanSlotAssignment).where(SpoolmanSlotAssignment.printer_id == printer_id))
        by_slot = {(a.ams_id, a.tray_id): a.spoolman_spool_id for a in result.scalars().all()}
        for ams_id, tray_id, gtid in tracked_slots:
            spoolman_id = by_slot.get((ams_id, tray_id))
            if spoolman_id is None:
                continue
            remaining_g, first_ord = await _fetch_spoolman_slot(spoolman_id)
            if remaining_g is None and first_ord is None:
                continue
            out[gtid] = SlotInventory(remaining_g=remaining_g, first_loaded_ord=first_ord)
        return out

    # Internal inventory mode (default).
    result = await db.execute(
        select(SpoolAssignment)
        .options(selectinload(SpoolAssignment.spool))
        .where(SpoolAssignment.printer_id == printer_id)
    )
    by_slot = {(a.ams_id, a.tray_id): a.spool for a in result.scalars().all()}
    for ams_id, tray_id, gtid in tracked_slots:
        spool = by_slot.get((ams_id, tray_id))
        if spool is None:
            continue
        label = float(spool.label_weight or 0)
        used = float(spool.weight_used or 0)
        remaining_g = max(0.0, label - used)
        first_ord = _dt_to_epoch(spool.first_loaded_at or spool.created_at)
        out[gtid] = SlotInventory(
            remaining_g=remaining_g,
            first_loaded_ord=first_ord,
            out_of_rotation=spool.feed_fault_at is not None,
            spent=spool.spent_at is not None,
        )
    return out


async def _fetch_spoolman_slot(spoolman_spool_id: int) -> tuple[float | None, float | None]:
    """One Spoolman fetch → (remaining_g, first_loaded_ord). Both ``None`` on any
    failure. Reads ``remaining_weight`` and ``first_used`` from the SAME dict."""
    try:
        from backend.app.services.spoolman import (
            SpoolmanClientError,
            SpoolmanNotFoundError,
            get_spoolman_client,
        )
    except ImportError:
        return None, None
    try:
        client = await get_spoolman_client()
        if client is None:
            return None, None
        spool = await client.get_spool(spoolman_spool_id)
    except (SpoolmanNotFoundError, SpoolmanClientError):
        return None, None
    except Exception as e:  # noqa: BLE001 — best-effort; a preference, not a guarantee
        logger.debug("Spoolman fetch failed for spool %s: %s", spoolman_spool_id, e)
        return None, None

    from backend.app.services.filament_deficit import _spoolman_grams_from_dict

    remaining_g = _spoolman_grams_from_dict(spool)
    first_ord = _iso_to_epoch(spool.get("first_used")) if isinstance(spool, dict) else None
    return remaining_g, first_ord


# ---------------------------------------------------------------------------
# Release-path guard
# ---------------------------------------------------------------------------
async def _read_min_start_g(db: AsyncSession) -> int:
    """Read the ``min_start_spool_g`` setting (default when unset/invalid)."""
    from backend.app.api.routes.settings import get_setting

    raw = await get_setting(db, "min_start_spool_g")
    if raw is None:
        return DEFAULT_MIN_START_SPOOL_G
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MIN_START_SPOOL_G


async def start_rule_blocked_slots(db: AsyncSession, item: PrintQueueItem) -> list[int]:
    """Recompute the pinned printer's selection outcome and return the slot_ids
    blocked purely by the minimum-start floor. Empty when the rule can't apply
    (no pinned printer, Print-Anyway acknowledged, or the floor is disabled)."""
    if item.printer_id is None or item.skip_filament_check:
        return []
    if await _read_min_start_g(db) == 0:
        return []
    # Function-local import: print_scheduler imports this module at load time.
    from backend.app.services.print_scheduler import scheduler

    outcome = await scheduler._compute_ams_mapping_for_printer(db, item.printer_id, item)
    return list(outcome.start_blocked_slots)


async def start_rule_blocks_item(db: AsyncSession, item: PrintQueueItem) -> bool:
    """True when the minimum-start floor blocks this pinned item from starting."""
    return bool(await start_rule_blocked_slots(db, item))
