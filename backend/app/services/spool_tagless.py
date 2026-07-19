"""Tagless (non-RFID) spool lifecycle — single owner (fork farm feature).

Every AMS tray holding a non-RFID spool becomes silently tracked so the farm's
gram accounting and FIFO spool-selection see it like any other roll:

* **Configured tagless tray** — the firmware reports a ``tray_type`` the operator
  set in the slicer, but there is no RFID tag: auto-mint a
  ``data_origin="ams_auto"`` spool from the tray fields and bind it
  (:func:`handle_tagless_slot`, "Hook B" in ``main.on_ams_change``).
* **BARE tray** — a spool is physically present but nothing is configured
  (``tray_type`` empty, state 10/11): additionally push a configured default
  filament to the printer so the slot is usable, including mid-print where the
  configured slot joins the firmware backup pool
  (:func:`maybe_autoconfigure_bare_tray`, decision D3b).

Continuity rules:

* **Sticky rebind** — a tagless spool pulled for drying and returned re-binds to
  the SAME ledger row: :func:`should_keep_on_empty` keeps the assignment while
  the slot is empty; :func:`fingerprint_matches` re-binds on return. Operator
  edits to a minted spool are NEVER overwritten.
* **Ran-dry always mints new** — a spool marked ``spent_at`` re-loaded is
  archived (grams preserved) and a fresh row is minted (physically a new roll).
* **Provisional disposal** — an auto-minted tagless row is provisional; when a
  real RFID tag later claims the slot, :func:`dispose_provisional_on_tag`
  hard-deletes it (no usage ledger) or archives it (has one).

Module edge state (``_autoconfig_attempts``, ``_stale_config_markers``) mirrors
the fork's other event-edge bookkeeping (``spool_respool._last_tray_now``). It is
lost on restart — worst case a bare-tray config re-push waits one AMS push, and a
lost stale-config marker degrades to a tray-derived mint (benign; an operator can
correct the row).

``stamp_first_loaded`` lives in ``spool_tag_matcher`` (the lowest module every
assignment-creating caller already imports); this module re-exports it via import
so callers keep one implementation and there is no import cycle.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from time import monotonic
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from backend.app.core.websocket import ws_manager
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services import ams_presence
from backend.app.services.printer_manager import printer_manager
from backend.app.services.spool_tag_matcher import (
    auto_assign_spool,
    is_valid_tag,
    parse_tray_fields,
    stamp_first_loaded,
)
from backend.app.utils.color_utils import colors_similar
from backend.app.utils.filament_types import canonical_filament_type

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Marker written on every auto-minted tagless row — the single classification the
# attract-exclusion, provisional-disposal, and terminal-sweep relax all key on.
DATA_ORIGIN = "ams_auto"

# Re-push cadence for a BARE tray whose default-filament config has not yet
# landed on the printer (failed / slow MQTT). The trigger persists across AMS
# pushes until the firmware reports a non-empty tray_type; this gate stops it
# hammering the broker every push in the meantime.
_AUTOCONFIG_RETRY_S = 30.0

# (printer_id, ams_id, tray_id) -> monotonic timestamp of the last bare-tray
# config attempt. Cleared when the slot empties.
_autoconfig_attempts: dict[tuple[int, int, int], float] = {}

# (printer_id, ams_id, tray_id) -> (canonical_material, upper_rgba) fingerprint of
# a SPENT spool that just departed a now-empty slot. If the firmware then
# re-reports that same leftover config on the slot, it is stale firmware state
# (not a real new spool), so we apply the bare-tray default instead of minting
# from the leftover fingerprint.
_stale_config_markers: dict[tuple[int, int, int], tuple[str, str]] = {}


def _reset_state() -> None:
    """Test hook: clear module-level edge/dedup state between cases."""
    _autoconfig_attempts.clear()
    _stale_config_markers.clear()


# --- state / predicate helpers ---------------------------------------------


def _norm_state(raw: object) -> int | None:
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def tray_present(tray: dict) -> bool:
    """Positive-evidence presence: seated/loaded (state 10 or 11) only.

    Matches ``ams_presence._tray_present`` — state 9/None/unknown read as absent
    so an H2C idle-empty (state 0) never reads as a phantom spool.
    """
    return _norm_state(tray.get("state")) in (10, 11)


def _tray_loaded(tray: dict) -> bool:
    """Filament-loaded heuristic — mirrors ``spool_respool._tray_loaded``.

    state == 11 (fed to extruder) OR a non-empty tray_type when state is NOT one
    of the firmware's explicit empty signals (9, 10). A spent spool re-inserted
    without a refill reads present-but-not-loaded → False → no fresh-mint churn.
    """
    cur_state = tray.get("state")
    cur_type = (tray.get("tray_type") or "").strip()
    return cur_state == 11 or (cur_state not in (9, 10) and bool(cur_type))


def _tray_material(tray: dict) -> str:
    """Best-effort material string from a tray dict (no DB), for fingerprinting."""
    tray_type = (tray.get("tray_type") or "").strip()
    if tray_type:
        return tray_type
    sub = (tray.get("tray_sub_brands") or "").strip()
    return sub.split(" ", 1)[0] if sub else ""


def is_tagless_spool(spool: Spool | None) -> bool:
    """True when the spool carries no RFID identity (no tag_uid and no tray_uuid)."""
    if spool is None:
        return False
    return not (spool.tag_uid or spool.tray_uuid)


def fingerprint_matches(spool: Spool, tray: dict) -> bool:
    """Same physical filament: color within tolerance AND same canonical material."""
    if not colors_similar(tray.get("tray_color") or "", spool.rgba or "FFFFFFFF"):
        return False
    return canonical_filament_type(_tray_material(tray)) == canonical_filament_type(spool.material or "")


def effectively_empty(spool: Spool, threshold_g: int) -> bool:
    """Remaining grams at or below the 'effectively empty' threshold."""
    remaining = (spool.label_weight or 0) - (spool.weight_used or 0)
    return remaining <= threshold_g


def should_keep_on_empty(assignment: SpoolAssignment, threshold_g: int) -> bool:
    """Sticky-rebind decision for a slot that just went empty.

    Keep the assignment (do NOT unlink) only when the bound spool is a tagless
    roll that is neither spent nor effectively empty — i.e. pulled for drying and
    expected back. A spent or near-empty spool departing is a genuine removal;
    the caller should unlink it.
    """
    spool = assignment.spool
    if spool is None or not is_tagless_spool(spool):
        return False
    if spool.spent_at is not None:
        return False
    return not effectively_empty(spool, threshold_g)


def _marker_matches(marker: tuple[str, str], tray: dict) -> bool:
    mat, color = marker
    if canonical_filament_type(_tray_material(tray)) != canonical_filament_type(mat):
        return False
    return colors_similar(tray.get("tray_color") or "", color or "")


def _refresh_assignment_fingerprint(assignment: SpoolAssignment, tray: dict) -> None:
    cur_color = tray.get("tray_color", "") or ""
    cur_type = tray.get("tray_type", "") or ""
    if (assignment.fingerprint_color or "").upper() != cur_color.upper() or (
        assignment.fingerprint_type or ""
    ).upper() != cur_type.upper():
        assignment.fingerprint_color = cur_color
        assignment.fingerprint_type = cur_type


# --- setting helpers --------------------------------------------------------


async def _auto_add_untagged(db: AsyncSession) -> bool:
    from backend.app.api.routes.settings import get_setting

    raw = await get_setting(db, "auto_add_untagged")
    return raw is None or raw.lower() == "true"


async def _tagless_default(db: AsyncSession) -> dict | None:
    """Parse the ``tagless_default_filament`` setting; None when empty (feature off).

    Unset (no DB row) resolves to the schema default (Bambu Lab PETG HF) — the
    feature is on by default. A stored empty string is the operator's explicit
    "off" and returns None. A shape without material/rgba is treated as off.
    """
    from backend.app.api.routes.settings import get_setting
    from backend.app.schemas.settings import _DEFAULT_TAGLESS_FILAMENT_JSON

    raw = await get_setting(db, "tagless_default_filament")
    if raw is None:
        raw = _DEFAULT_TAGLESS_FILAMENT_JSON
    raw = raw.strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("tagless_default_filament is not valid JSON — treating as feature off")
        return None
    if not isinstance(parsed, dict) or not parsed.get("material") or not parsed.get("rgba"):
        return None
    return parsed


async def tagless_default_brand(db: AsyncSession) -> str:
    """The configured tagless-default filament brand, or "" when unset/off.

    Public accessor over :func:`_tagless_default` (the single JSON parser) so
    other services — e.g. the re-spool tier-2 auto-brand fallback — share ONE
    source of truth for the ``tagless_default_filament`` setting without copying
    the parse or reaching into a module-private helper. Returns "" when the
    feature is off (parser returns None) or the configured JSON carries no brand,
    so callers never invent a brand.
    """
    default = await _tagless_default(db)
    return ((default or {}).get("brand") or "").strip()


async def effective_threshold(db: AsyncSession) -> int:
    """The 'effectively empty' grams threshold (reuses ``respool_prompt_threshold_g``)."""
    from backend.app.api.routes.settings import get_setting

    raw = await get_setting(db, "respool_prompt_threshold_g")
    try:
        return int(raw) if raw is not None else 30
    except (TypeError, ValueError):
        return 30


# --- minting ----------------------------------------------------------------


async def mint_tagless_spool(
    db: AsyncSession, *, tray: dict | None = None, default_filament: dict | None = None
) -> Spool:
    """Mint a silently-tracked tagless spool from ONE of two sources.

    * ``tray`` — a configured AMS tray dict (identity via :func:`parse_tray_fields`).
    * ``default_filament`` — the ``tagless_default_filament`` setting dict
      (brand/material/subtype/rgba/slicer_filament), used for bare trays.

    Exactly one source must be given. Common shape: no tag fields,
    ``data_origin="ams_auto"``, ``tag_type=None``, ``weight_used=0``.
    ``label_weight`` uses the tray's reported net weight when the AMS gives a
    positive one, otherwise it is left to the ``Spool`` model's default (see
    ``models/spool.py`` — ``label_weight`` default = 1000 g); tagless trays
    commonly report ``tray_weight="0"``.
    """
    if (tray is None) == (default_filament is None):
        raise ValueError("mint_tagless_spool requires exactly one of tray / default_filament")

    label_weight: int | None
    if tray is not None:
        parsed = await parse_tray_fields(db, tray)
        material = parsed.material
        subtype = parsed.subtype
        color_name = parsed.color_name
        rgba = parsed.rgba
        brand = None  # tagless: brand unknown (third-party) — the operator can set it
        core_weight = parsed.core_weight
        slicer_filament = parsed.slicer_filament
        slicer_filament_name = parsed.slicer_filament_name
        nozzle_temp_min = parsed.nozzle_temp_min
        nozzle_temp_max = parsed.nozzle_temp_max
        # Only a POSITIVE reported net weight overrides the model default.
        label_weight = parsed.label_weight if parsed.label_weight > 0 else None
        source = "tray"
    else:
        material = default_filament.get("material") or "PLA"
        subtype = (default_filament.get("subtype") or "").strip() or None
        color_name = None
        rgba = default_filament.get("rgba")
        brand = default_filament.get("brand") or None
        core_weight = 250
        slicer_filament = default_filament.get("slicer_filament") or None
        slicer_filament_name = None
        nozzle_temp_min = None
        nozzle_temp_max = None
        label_weight = None  # use the Spool model default (1000 g)
        source = "default"

    kwargs: dict = {
        "material": material,
        "subtype": subtype,
        "color_name": color_name,
        "rgba": rgba,
        "brand": brand,
        "core_weight": core_weight,
        "weight_used": 0,
        "slicer_filament": slicer_filament,
        "slicer_filament_name": slicer_filament_name,
        "nozzle_temp_min": nozzle_temp_min,
        "nozzle_temp_max": nozzle_temp_max,
        "data_origin": DATA_ORIGIN,
        "tag_type": None,
    }
    if label_weight is not None:
        kwargs["label_weight"] = label_weight

    spool = Spool(**kwargs)
    # Initialize relationships BEFORE add() to avoid an async lazy load — the
    # SpoolAssignment back_populates resolution runs synchronously (see #612 and
    # ``create_spool_from_tray``).
    spool.k_profiles = []
    spool.assignments = []
    db.add(spool)
    await db.flush()
    logger.info(
        "Auto-minted tagless spool %d: %s %s %s (source=%s, origin=ams_auto)",
        spool.id,
        material,
        subtype or "",
        color_name or "",
        source,
    )
    return spool


# --- assignment helpers -----------------------------------------------------


async def _assign_from_setting(
    db: AsyncSession, spool: Spool, printer_id: int, ams_id: int, tray_id: int, default: dict
) -> None:
    """Bind a setting-minted spool with a fingerprint seeded from the SETTING.

    A bare tray reports an empty tray_type, so an auto_assign_spool-derived
    fingerprint would be empty and re-trip the SpoolBuddy empty-fingerprint
    replay. Seeding fingerprint_color/type from the default filament suppresses
    that and makes the later Hook B fingerprint-match a no-op.
    """
    existing = await db.execute(
        select(SpoolAssignment).where(
            SpoolAssignment.printer_id == printer_id,
            SpoolAssignment.ams_id == ams_id,
            SpoolAssignment.tray_id == tray_id,
        )
    )
    old = existing.scalar_one_or_none()
    if old is not None:
        await db.delete(old)
        await db.flush()
    assignment = SpoolAssignment(
        spool_id=spool.id,
        printer_id=printer_id,
        ams_id=ams_id,
        tray_id=tray_id,
        fingerprint_color=default.get("rgba") or "",
        fingerprint_type=default.get("material") or "",
    )
    db.add(assignment)
    await db.flush()
    stamp_first_loaded(spool)


async def _push_config(db: AsyncSession, spool: Spool, printer_id: int, ams_id: int, tray_id: int, tray: dict) -> bool:
    """Publish the default filament config to the slot (bare-tray path only)."""
    from backend.app.api.routes.inventory import apply_spool_to_slot_via_mqtt

    try:
        return await apply_spool_to_slot_via_mqtt(
            db=db,
            current_user=None,
            spool=spool,
            printer_id=printer_id,
            ams_id=ams_id,
            tray_id=tray_id,
            current_tray_info_idx=tray.get("tray_info_idx", "") or "",
            current_tray_type=tray.get("tray_type", "") or "",
        )
    except Exception:  # noqa: BLE001 — log a TRANSIENT push failure; a later AMS push retries it
        # NOT self-healing for a deterministic error: the callee's lazy-load crash
        # (walking spool.k_profiles on a pre-existing DB spool) used to fail EVERY
        # push here and was fixed at apply_spool_to_slot_via_mqtt. What remains is a
        # genuinely transient MQTT/config failure — while the slot stays bare the
        # bare-tray trigger re-fires on subsequent AMS pushes (gated by
        # _AUTOCONFIG_RETRY_S), so a transient miss is retried; a stuck-bare slot
        # eventually escalates via spool_recovery's forced sweep.
        logger.exception(
            "Bare-tray config push failed for spool %d on printer %d AMS%d-T%d",
            spool.id,
            printer_id,
            ams_id,
            tray_id,
        )
        return False


async def _broadcast_auto_assigned(
    printer_id: int, ams_id: int, tray_id: int, spool_id: int, origin: str | None = None
) -> None:
    """Broadcast a ``spool_auto_assigned`` slot event.

    ``origin`` distinguishes this module's tagless silent-mint (``"tagless"`` —
    a genuinely NEW untagged roll the frontend toasts about) from the RFID
    auto-assign broadcasts elsewhere (``main.on_ams_change`` / ``routes.inventory``),
    which omit the field. The key is only added when ``origin`` is given so RFID
    payloads stay byte-for-byte unchanged (absent field).
    """
    payload: dict = {
        "type": "spool_auto_assigned",
        "printer_id": printer_id,
        "ams_id": ams_id,
        "tray_id": tray_id,
        "spool_id": spool_id,
    }
    if origin is not None:
        payload["origin"] = origin
    await ws_manager.broadcast(payload)


# --- Hook B: tagless-slot policy -------------------------------------------


async def _maybe_move_tagless_assignment(
    db: AsyncSession,
    printer_id: int,
    ams_id: int,
    tray_id: int,
    tray: dict,
    ams_data: list,
) -> bool:
    """Re-bind a MOVED tagless roll to its existing ledger row instead of minting.

    A tagless spool physically relocated to another slot on the SAME printer
    leaves its old :class:`SpoolAssignment` sticky-kept over the now-empty source
    slot (phase 1's :func:`should_keep_on_empty`). Without this, branch (1) of
    :func:`handle_tagless_slot` would mint a SECOND spool for the same physical
    roll — the ledger duplicate this closes.

    A candidate is one of THIS printer's OTHER assignments whose spool is tagless,
    not spent, and fingerprint-matches ``tray``, AND whose own slot is verifiably
    EMPTY in the live ``ams_data`` (its tray dict is present with a blank
    ``tray_type``). A slot ABSENT from the payload is unknowable, so it is NOT a
    candidate. Cross-printer moves are never considered — we only query THIS
    printer.

    Returns True (caller should ``continue``) ONLY when exactly one candidate is
    found and its assignment was moved to this slot. Zero candidates → False
    (mint as usual). Two or more → False plus a WARNING naming the ambiguous
    spool ids (mint rather than guess which roll moved).
    """
    from backend.app.api.routes.inventory import _find_tray_in_ams_data

    res = await db.execute(
        select(SpoolAssignment)
        .options(selectinload(SpoolAssignment.spool))
        .where(SpoolAssignment.printer_id == printer_id)
    )
    candidates: list[SpoolAssignment] = []
    for asg in res.scalars().all():
        if asg.ams_id == ams_id and asg.tray_id == tray_id:
            continue  # the target slot itself (defensive — branch (1) has no row here)
        spool = asg.spool
        if spool is None or not is_tagless_spool(spool) or spool.spent_at is not None:
            continue
        if not fingerprint_matches(spool, tray):
            continue
        src_tray = _find_tray_in_ams_data(ams_data, asg.ams_id, asg.tray_id)
        if src_tray is None:
            continue  # source slot/unit absent from the payload → unknowable, not a candidate
        if (src_tray.get("tray_type") or "").strip():
            continue  # source slot still holds filament → not an empty source
        candidates.append(asg)

    if len(candidates) != 1:
        if len(candidates) >= 2:
            logger.warning(
                "Tagless slot-move ambiguous on printer %d AMS%d-T%d: %d empty-source candidates "
                "(spool ids %s) fingerprint-match — minting a fresh row instead of moving",
                printer_id,
                ams_id,
                tray_id,
                len(candidates),
                [c.spool_id for c in candidates],
            )
        return False

    asg = candidates[0]
    old_ams_id, old_tray_id = asg.ams_id, asg.tray_id
    asg.ams_id = ams_id
    asg.tray_id = tray_id
    _refresh_assignment_fingerprint(asg, tray)
    await db.commit()
    await _broadcast_auto_assigned(printer_id, ams_id, tray_id, asg.spool_id, origin="tagless")
    logger.info(
        "Tagless moved assignment (slot-move) spool %d from AMS%d-T%d to AMS%d-T%d",
        asg.spool_id,
        old_ams_id,
        old_tray_id,
        ams_id,
        tray_id,
    )
    return True


async def handle_tagless_slot(
    db: AsyncSession,
    printer_id: int,
    ams_id: int,
    tray_id: int,
    tray: dict,
    existing_assignment: SpoolAssignment | None,
    ams_data: list,
) -> bool:
    """Policy for a NON-empty tray with no valid RFID tag (native-loop Hook B).

    Returns True when this module took ownership of the slot (the caller should
    ``continue``), or False to let the existing tagged-spool paths run
    (weight-sync, respool gate, K-profile re-apply). ``tray`` is guaranteed to
    have a non-empty ``tray_type`` here — the empty-tray branch handles bare/empty.

    ``ams_data`` is the full live AMS payload the caller is iterating; the
    no-assignment branch uses it to detect a roll that physically MOVED from
    another (now-empty) slot on THIS printer and re-bind its existing ledger row
    instead of minting a duplicate (see :func:`_maybe_move_tagless_assignment`).
    """
    key = (printer_id, ams_id, tray_id)

    # Defer while an RFID identify is in flight on this slot: minting/pushing now
    # collides with the firmware's read and fails it (HMS 0700_2x00_0001_0081).
    # Return True (NOT False): a falsy return falls through main.on_ams_change to
    # the MUTATING spent→respool gate + AMS weight-sync (main.py ~:2077-2132);
    # True makes the caller ``continue`` — do nothing else with this tray this pass.
    # A later AMS push (after the identify settles) handles the slot normally.
    if ams_presence.identify_in_flight(printer_id, ams_id, tray_id):
        logger.debug(
            "Deferring tagless handling for printer %d AMS%d-T%d: identify in flight",
            printer_id,
            ams_id,
            tray_id,
        )
        return True

    # Defer while the AMS unit is drying: minting/pushing config now disengages the
    # drying tray and fails the cycle (HMS 0700_C069). Same True-return rationale as
    # the identify defer above — a later push (after drying) handles the slot.
    if ams_presence.unit_drying(printer_id, ams_id):
        logger.debug(
            "Deferring tagless handling for printer %d AMS%d-T%d: AMS unit is drying",
            printer_id,
            ams_id,
            tray_id,
        )
        return True

    # An assignment whose spool row was deleted is an orphan — drop it and treat
    # the slot as unassigned.
    if existing_assignment is not None and existing_assignment.spool is None:
        await db.delete(existing_assignment)
        await db.flush()
        existing_assignment = None

    if existing_assignment is not None:
        # A live assignment means the slot is tracked — the stale-config marker
        # is moot now (pre-assignment clear).
        _stale_config_markers.pop(key, None)
        spool = existing_assignment.spool

        # (2) Bound to a TAGGED spool → RFID/respool flows own it (this also lets
        # a spent TAGGED spool reach the respool gate). Not ours.
        if not is_tagless_spool(spool):
            return False

        # (3) Bound spool is spent → a fresh roll physically cannot be it.
        if spool.spent_at is not None:
            if not _tray_loaded(tray):
                return True  # dead roll re-seated, filament not fed — no churn
            spool.archived_at = datetime.utcnow()  # keep the ledger row + its grams
            await db.delete(existing_assignment)
            await db.flush()
            new_spool = await mint_tagless_spool(db, tray=tray)
            await auto_assign_spool(
                printer_id,
                ams_id,
                tray_id,
                new_spool,
                printer_manager,
                db,
                tray_info_idx=tray.get("tray_info_idx", "") or "",
            )
            await db.commit()
            await _broadcast_auto_assigned(printer_id, ams_id, tray_id, new_spool.id, origin="tagless")
            return True

        # (4) Same filament → rebind: refresh a drifted fingerprint, write NOTHING
        # to the spool (operator edits are sacred).
        if fingerprint_matches(spool, tray):
            _refresh_assignment_fingerprint(existing_assignment, tray)
            await db.commit()
            return True

        # (5) Different filament → unlink (old spool stays active, just unbound),
        # then mint the new one from the tray.
        await db.delete(existing_assignment)
        await db.flush()
        new_spool = await mint_tagless_spool(db, tray=tray)
        await auto_assign_spool(
            printer_id,
            ams_id,
            tray_id,
            new_spool,
            printer_manager,
            db,
            tray_info_idx=tray.get("tray_info_idx", "") or "",
        )
        await db.commit()
        await _broadcast_auto_assigned(printer_id, ams_id, tray_id, new_spool.id, origin="tagless")
        return True

    # (1) No assignment. Honour the feature switch.
    if not await _auto_add_untagged(db):
        return True  # feature off — leave the slot alone (handled = do nothing)

    # Slot-move: a tagless roll physically relocated to this slot from another
    # (now-empty) slot on THIS printer must re-bind its EXISTING ledger row, not
    # mint a duplicate. Phase 1's sticky-keep leaves the source assignment intact
    # over its empty slot, which is exactly the row we move here.
    if await _maybe_move_tagless_assignment(db, printer_id, ams_id, tray_id, tray, ams_data):
        return True

    # Stale-config override: a spent spool's leftover config re-reported by the
    # firmware on this now-refilled slot → apply the default instead of minting
    # from the leftover fingerprint. A DIFFERING config clears the marker and
    # falls through to the normal tray-derived mint.
    marker = _stale_config_markers.pop(key, None)
    if marker is not None and _marker_matches(marker, tray):
        default = await _tagless_default(db)
        if default is not None:
            new_spool = await mint_tagless_spool(db, default_filament=default)
            await _assign_from_setting(db, new_spool, printer_id, ams_id, tray_id, default)
            await db.commit()
            await _push_config(db, new_spool, printer_id, ams_id, tray_id, tray)
            await _broadcast_auto_assigned(printer_id, ams_id, tray_id, new_spool.id, origin="tagless")
            logger.info(
                "Stale-config override on printer %d AMS%d-T%d: applied tagless default over firmware leftover",
                printer_id,
                ams_id,
                tray_id,
            )
            return True
        # setting cleared → fall through to a normal tray-derived mint.

    new_spool = await mint_tagless_spool(db, tray=tray)
    await auto_assign_spool(
        printer_id,
        ams_id,
        tray_id,
        new_spool,
        printer_manager,
        db,
        tray_info_idx=tray.get("tray_info_idx", "") or "",
    )
    await db.commit()
    await _broadcast_auto_assigned(printer_id, ams_id, tray_id, new_spool.id, origin="tagless")
    return True


# --- D3b: bare-tray auto-config --------------------------------------------


async def maybe_autoconfigure_bare_tray(
    db: AsyncSession, printer_id: int, ams_id: int, tray_id: int, tray: dict, *, force: bool = False
) -> bool:
    """Push a default filament to a BARE tray (spool present, nothing configured).

    Makes an unconfigured third-party spool usable — including mid-print, where
    the newly-configured slot joins the firmware backup pool. Returns True when a
    config push was attempted this tick.

    Trigger: tray PRESENT (state 10/11) AND tray_type empty AND no valid tag AND
    ``auto_add_untagged`` AND a non-empty ``tagless_default_filament`` setting.
    The config push self-heals: while the slot stays bare, the trigger persists
    across AMS pushes (gated by :data:`_AUTOCONFIG_RETRY_S`) until the firmware
    reports a non-empty tray_type and the slot leaves this branch.

    ``force=True`` bypasses ONLY the :data:`_AUTOCONFIG_RETRY_S` window (every
    other guard — presence, tray_type-empty, RFID, settings, operator/RFID-bound
    slot — still applies). ``spool_recovery`` uses it for a one-shot bare-tray
    sweep when a mid-print jam has no configured replacement, so a present-but-bare
    backup spool can be enrolled without waiting out the retry cadence.
    """
    if not tray_present(tray):
        return False
    if (tray.get("tray_type") or "").strip():
        return False  # already configured — not bare
    if is_valid_tag(tray.get("tag_uid", "") or "", tray.get("tray_uuid", "") or ""):
        return False  # RFID tray — not tagless
    if not await _auto_add_untagged(db):
        return False
    default = await _tagless_default(db)
    if default is None:
        return False  # setting cleared → feature off

    # Defer a doomed config push while the AMS is mid-identify or drying: the write
    # would collide with the RFID read (HMS 0700_2x00_0001_0081) or disengage the
    # drying tray (HMS 0700_C069). Return BEFORE stamping _autoconfig_attempts so the
    # retry window is not burned on a push that never went out. force= bypasses only
    # the retry window — never these hardware-state guards.
    if ams_presence.identify_in_flight(printer_id, ams_id, tray_id) or ams_presence.unit_drying(printer_id, ams_id):
        logger.debug(
            "Deferring bare-tray auto-config for printer %d AMS%d-T%d: AMS identify/drying in progress",
            printer_id,
            ams_id,
            tray_id,
        )
        return False

    key = (printer_id, ams_id, tray_id)
    now = monotonic()
    last = _autoconfig_attempts.get(key)
    if not force and last is not None and (now - last) < _AUTOCONFIG_RETRY_S:
        return False  # config attempt still inside its retry window

    res = await db.execute(
        select(SpoolAssignment)
        .options(selectinload(SpoolAssignment.spool))
        .where(
            SpoolAssignment.printer_id == printer_id,
            SpoolAssignment.ams_id == ams_id,
            SpoolAssignment.tray_id == tray_id,
        )
    )
    assignment = res.scalar_one_or_none()
    if assignment is not None and (assignment.spool is None or assignment.spool.data_origin != DATA_ORIGIN):
        # Operator- or RFID-bound slot (or an orphan) — never overwrite. Only our
        # OWN auto-minted default is eligible for a self-healing re-push.
        return False

    _autoconfig_attempts[key] = now

    if assignment is None:
        spool = await mint_tagless_spool(db, default_filament=default)
        await _assign_from_setting(db, spool, printer_id, ams_id, tray_id, default)
        await db.commit()
    else:
        # Our own default already tracked but the firmware hasn't applied it yet
        # (failed / slow push) — re-push, don't re-mint.
        spool = assignment.spool

    await _push_config(db, spool, printer_id, ams_id, tray_id, tray)
    return True


# --- stale-config markers + dedup lifecycle --------------------------------


def clear_autoconfig_dedup(printer_id: int, ams_id: int, tray_id: int) -> None:
    """Drop the bare-tray retry timestamp for a slot (called when it empties)."""
    _autoconfig_attempts.pop((printer_id, ams_id, tray_id), None)


def clear_stale_marker(printer_id: int, ams_id: int, tray_id: int) -> None:
    _stale_config_markers.pop((printer_id, ams_id, tray_id), None)


def record_stale_marker_for_spool(printer_id: int, ams_id: int, tray_id: int, spool: Spool | None) -> None:
    """Record a departing SPENT spool's fingerprint so its firmware-leftover
    config on the next insertion is recognized as stale (not a new spool).

    No-op unless the spool exists and is spent — a live spool departing is a
    normal removal, not a firmware-leftover source.
    """
    if spool is None or spool.spent_at is None:
        return
    _stale_config_markers[(printer_id, ams_id, tray_id)] = (
        canonical_filament_type(spool.material or ""),
        (spool.rgba or "").upper(),
    )


async def record_stale_marker(db: AsyncSession, printer_id: int, ams_id: int, tray_id: int) -> None:
    """Fetch the slot's bound spool and record a stale-config marker iff it is
    spent. Called from the native-loop 'truly empty' branch."""
    res = await db.execute(
        select(SpoolAssignment)
        .options(selectinload(SpoolAssignment.spool))
        .where(
            SpoolAssignment.printer_id == printer_id,
            SpoolAssignment.ams_id == ams_id,
            SpoolAssignment.tray_id == tray_id,
        )
    )
    assignment = res.scalar_one_or_none()
    if assignment is not None:
        record_stale_marker_for_spool(printer_id, ams_id, tray_id, assignment.spool)


# --- provisional disposal on RFID takeover ---------------------------------


async def dispose_provisional_on_tag(db: AsyncSession, spool: Spool | None) -> str:
    """Dispose an auto-minted tagless row when a real RFID tag claims its slot.

    Hard-delete a pristine provisional row (no ``SpoolUsageHistory``) or archive a
    ledger-bearing one — mirrors ``spool_respool``'s donor disposition. Returns
    the disposition ("hard-deleted" / "archived" / "kept"). "kept" means the
    spool was not an auto-minted provisional row and must be left untouched.
    """
    from backend.app.models.spool_usage_history import SpoolUsageHistory

    if spool is None or spool.data_origin != DATA_ORIGIN:
        return "kept"
    history_count = await db.scalar(
        select(func.count(SpoolUsageHistory.id)).where(SpoolUsageHistory.spool_id == spool.id)
    )
    if not history_count:
        await db.delete(spool)
        return "hard-deleted"
    spool.archived_at = datetime.utcnow()
    return "archived"
