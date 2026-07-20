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
* **Spent-binding latch (W1)** — a spool marked ``spent_at`` keeps its binding
  while the tray remains continuously present (:func:`should_keep_on_empty` keeps
  spent rows too); the spent binding IS the durable "this tray ran dry" latch.
  "Ran-dry mints new" fires ONLY after a qualified physical absence
  (:func:`note_physical_cycle` records the cycle; branch (3) /
  :func:`maybe_autoconfigure_bare_tray` consume it) — a runout-instant state flap
  can no longer phantom-mint a fresh row over a still-present spool.
* **Provisional disposal** — an auto-minted tagless row is provisional; when a
  real RFID tag later claims the slot, :func:`dispose_provisional_on_tag`
  hard-deletes it (no usage ledger) or archives it (has one).

Module edge state (``_autoconfig_window``, ``_pending_physical_cycles``,
``_fresh_prompt_unanswered``, ``_identity_reconciled``) mirrors the fork's other
event-edge bookkeeping (``spool_respool._last_tray_now``). It is lost on restart —
worst case a bare-tray config re-push waits one AMS push, a spent slot stays
latched+excluded until a pull/reseat (honest, not silent), an unanswered fresh-roll
prompt re-asks on the next physical cycle, and the one-shot identity reconcile is
re-armed (it re-checks live divergence first, so a converged slot stays silent).

``stamp_first_loaded`` lives in ``spool_tag_matcher`` (the lowest module every
assignment-creating caller already imports); this module re-exports it via import
so callers keep one implementation and there is no import cycle.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
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
from backend.app.utils.filament_ids import GENERIC_FILAMENT_IDS
from backend.app.utils.filament_types import canonical_filament_type
from backend.app.utils.retry_window import RetryWindow

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Marker written on every auto-minted tagless row — the single classification the
# attract-exclusion, provisional-disposal, and terminal-sweep relax all key on.
DATA_ORIGIN = "ams_auto"

# Generic (GFx99) slicer ids — the fallback a bare-tray auto-config writes when no
# specific id is configured. A tray re-reporting one of these is untrustworthy for
# minting a fresh identity (W4 generic-id override).
_GENERIC_ID_VALUES = frozenset(GENERIC_FILAMENT_IDS.values())

# Re-push cadence for a BARE tray whose default-filament config has not yet
# landed on the printer (failed / slow MQTT). The trigger persists across AMS
# pushes until the firmware reports a non-empty tray_type; this gate stops it
# hammering the broker every push in the meantime.
_AUTOCONFIG_RETRY_S = 30.0

# Per-slot gate for that cadence. Cleared when the slot empties
# (:func:`clear_autoconfig_dedup`).
_autoconfig_window = RetryWindow(_AUTOCONFIG_RETRY_S)

# Settle delay before a FRESH tagless row may be minted for a just-inserted spool.
# Inserting a roll makes the firmware publish the slot's config ~1 s BEFORE its own
# RFID read lands; minting on that first push auto-mints a tagless row that the tag
# read then destroys ("Provisional tagless spool N hard-deleted on RFID takeover" —
# three cases in one evening, 2026-07-19). Holding a fresh mint until the gain has
# settled lets the firmware's read win. Only FRESH mints wait: an existing binding
# (rebind / slot-move / spent-replace) is already the ledger's answer for the slot.
_MINT_SETTLE_S = 5.0

# (printer_id, ams_id, tray_id) of slots that saw a QUALIFIED physical roll swap
# (≥ _MIN_PHYSICAL_ABSENT_S absent → present, recorded by note_physical_cycle).
# This is the spent-binding latch's RELEASE signal: handle_tagless_slot branch (3)
# and maybe_autoconfigure_bare_tray consume it to mint the replacement over a spent
# row. A spent row with NO pending cycle stays latched (no phantom mint). Popped
# once processed on every branch; process-lifetime (a swap during downtime degrades
# to a latched+excluded slot, released by pull/reseat — honest, not silent).
_pending_physical_cycles: set[tuple[int, int, int]] = set()

# Fraction of a tagless row's label weight consumed past which a physical cycle
# raises the over-consumption / fresh-roll prompt (W5). 0.7 = the roll is ≥70 %
# consumed (≤300 g left on a 1000 g label) — operator setting 2026-07-20: a swap
# earlier in a roll's life is routine (drying, slot juggling) and asking then is
# noise.
_FRESH_ROLL_PROMPT_USED_FRAC = 0.7

# (printer_id, ams_id, tray_id) of tagless fresh-roll prompts awaiting an operator
# answer (W5). PER-CYCLE dedup (deliberately NOT the permanent respool_dismissed_at):
# cleared by either tagless-fresh answer and re-armed on the next qualified physical
# cycle, so each new roll swap asks again. Lost on restart (re-asks next cycle).
_fresh_prompt_unanswered: set[tuple[int, int, int]] = set()

# (printer_id, ams_id, tray_id) of slots whose live identity has been EVALUATED
# against the resolver this process (E3/W6) — either found converged or re-pushed once.
# A stale spool row can keep re-publishing an identity (a GENERIC id, or divergent
# nozzle temps) that splits the firmware's auto-refill backup group; the reconcile
# corrects it ONCE per slot per process, so a divergence can never loop and a fully
# converged slot costs nothing on later pushes. Convergence is the WHOLE wire identity
# the firmware groups on — tray_info_idx AND both nozzle temps — not tray_info_idx alone.
_identity_reconciled: set[tuple[int, int, int]] = set()


def _reset_state() -> None:
    """Test hook: clear module-level edge/dedup state between cases."""
    _autoconfig_window.reset()
    _pending_physical_cycles.clear()
    _fresh_prompt_unanswered.clear()
    _identity_reconciled.clear()


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


def _fingerprint_matches_default(material: str | None, rgba: str | None, default: dict) -> bool:
    """True when a (material, rgba) pair fingerprint-matches the tagless default —
    same canonical material AND color within tolerance. The material+rgba twin of
    :func:`fingerprint_matches` (a dict default has no tray shape), shared by the
    generic-id mint override (W4.4) and :func:`default_temps_for_fingerprint`."""
    if canonical_filament_type(material or "") != canonical_filament_type(default.get("material") or ""):
        return False
    return colors_similar(rgba or "", default.get("rgba") or "")


async def default_temps_for_fingerprint(
    db: AsyncSession, material: str | None, rgba: str | None
) -> tuple[int, int] | None:
    """The tagless default's ``(nozzle_temp_min, nozzle_temp_max)`` IFF a
    (material, rgba) pair fingerprint-matches the configured default AND the default
    carries both temps; else ``None``.

    Public accessor over the single tagless-default JSON parser (:func:`_tagless_default`)
    — the slicer resolver's middle nozzle-temp tier (row temps → THIS →
    ``MATERIAL_TEMPS``) so a fingerprint-matched tagless slot inherits the default's
    canonical range and stays a byte-identical firmware backup-group peer (W4)."""
    default = await _tagless_default(db)
    if default is None or not _fingerprint_matches_default(material, rgba, default):
        return None
    tmin = default.get("nozzle_temp_min")
    tmax = default.get("nozzle_temp_max")
    if tmin is None or tmax is None:
        return None
    try:
        return (int(tmin), int(tmax))
    except (TypeError, ValueError):
        return None


async def override_generic_identity(
    db: AsyncSession, slicer_filament: str | None, material: str | None, rgba: str | None
) -> dict | None:
    """The tagless default's SPECIFIC identity to write instead of a GENERIC one.

    Returns ``{"slicer_filament", "nozzle_temp_min", "nozzle_temp_max"}`` when
    ``slicer_filament`` is a generic id (``GFG99`` …) AND the configured tagless
    default carries a specific id AND ``(material, rgba)`` fingerprint-matches that
    default; ``None`` otherwise (nothing to override).

    Generic-id self-perpetuation is the 011-H2S no-auto-refill cause (2026-07-19):
    the firmware's auto-refill backup group only pairs slots whose brand-class /
    type / colour / nozzle temps match EXACTLY, so one slot configured ``GFG99``
    beside a ``GFG02`` peer splits the group. A generic id enters the ledger from a
    bare-tray auto-config or a legacy row and is then re-read and re-published
    forever. This is the single override, consumed at BOTH ends of that loop: the
    tagless mint (:func:`mint_tagless_spool`, so a re-read row stops carrying it)
    and the wire resolver (``slicer_filament_resolver.resolve_slicer_filament``, so
    a stale row already in the DB can no longer re-publish it). The temps ride along
    deliberately — substituting the id while keeping a stale row's temps would still
    split the group on the temperature dimension.
    """
    if not slicer_filament or slicer_filament not in _GENERIC_ID_VALUES:
        return None
    default = await _tagless_default(db)
    if default is None:
        return None
    specific = default.get("slicer_filament") or ""
    if not specific or not _fingerprint_matches_default(material, rgba, default):
        return None
    return {
        "slicer_filament": specific,
        "nozzle_temp_min": default.get("nozzle_temp_min"),
        "nozzle_temp_max": default.get("nozzle_temp_max"),
    }


def effectively_empty(spool: Spool, threshold_g: int) -> bool:
    """Remaining grams at or below the 'effectively empty' threshold."""
    remaining = (spool.label_weight or 0) - (spool.weight_used or 0)
    return remaining <= threshold_g


def should_keep_on_empty(assignment: SpoolAssignment, threshold_g: int) -> bool:
    """Sticky-rebind decision for a slot that just went empty.

    Keep the assignment (do NOT unlink) when the bound spool is a tagless roll that
    is either SPENT (W1: the spent binding is the durable "this tray ran dry" latch
    — kept until a physical roll swap releases it via :func:`note_physical_cycle`,
    so a runout-instant flap can't phantom-mint a fresh row) OR live-but-not-
    effectively-empty (pulled for drying and expected back). A non-spent near-empty
    spool departing is a genuine removal; the caller should unlink it.
    """
    spool = assignment.spool
    if spool is None or not is_tagless_spool(spool):
        return False
    if spool.spent_at is not None:
        return True  # W1: keep the spent binding as the latch until a physical swap
    return not effectively_empty(spool, threshold_g)


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
        # Generic-id self-perpetuation guard (W4.4): a tray re-reporting the GENERIC
        # id an earlier bare-tray auto-config wrote mints the tagless default's
        # SPECIFIC id/name/temps instead — see :func:`override_generic_identity`.
        _override = await override_generic_identity(db, slicer_filament, material, rgba)
        if _override is not None:
            slicer_filament = _override["slicer_filament"]
            slicer_filament_name = None
            nozzle_temp_min = _override["nozzle_temp_min"]
            nozzle_temp_max = _override["nozzle_temp_max"]
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
        # W4: stamp the configured default's canonical nozzle range onto the row so
        # the resolver emits it verbatim (a byte-identical backup-group peer).
        nozzle_temp_min = default_filament.get("nozzle_temp_min")
        nozzle_temp_max = default_filament.get("nozzle_temp_max")
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


# --- W1 spent-binding release / fresh-roll transition ----------------------


def _apply_new_fields(spool: Spool, fields: dict | None) -> None:
    """Apply the tagless-fresh route's optional manual fields onto a fresh row.

    Only non-empty values write (a blank field leaves the mint default). Used when
    the operator records a Fresh roll with brand / label weight / cost / note.
    """
    if not fields:
        return
    brand = (fields.get("brand") or "").strip()
    if brand:
        spool.brand = brand
    lw = fields.get("label_weight")
    if lw:
        try:
            spool.label_weight = int(lw)
        except (TypeError, ValueError):
            pass
    cost = fields.get("cost_per_kg")
    if cost is not None:
        spool.cost_per_kg = cost
    note = (fields.get("note") or "").strip()
    if note:
        spool.note = note


async def _replace_row_after_cycle(
    db: AsyncSession,
    printer_id: int,
    ams_id: int,
    tray_id: int,
    tray: dict | None,
    departed: Spool,
    *,
    new_fields: dict | None = None,
) -> Spool:
    """Archive a departed tagless row and mint+bind+push its replacement (W1/W5).

    The SINGLE spent-binding / fresh-roll transition, shared by
    :func:`handle_tagless_slot` branch (3), :func:`maybe_autoconfigure_bare_tray`,
    and the W5 tagless-fresh route. Default-mints from the configured tagless default
    when the tray is bare/absent OR still carries the departed row's config (firmware
    leftover — :func:`fingerprint_matches`), so a physically-fresh roll gets a clean
    4-dimension identity; else mints from the tray's own (genuinely different) config.
    Optional ``new_fields`` (brand/label_weight/cost_per_kg/note) ride the new row.
    Commits; broadcasts ``spool_auto_assigned(origin="tagless")``. Returns the new spool.
    """
    departed.archived_at = datetime.utcnow()  # keep the ledger row + its grams
    res = await db.execute(
        select(SpoolAssignment).where(
            SpoolAssignment.printer_id == printer_id,
            SpoolAssignment.ams_id == ams_id,
            SpoolAssignment.tray_id == tray_id,
        )
    )
    old = res.scalar_one_or_none()
    if old is not None:
        await db.delete(old)
        await db.flush()

    default = await _tagless_default(db)
    tray_configured = bool(tray and (tray.get("tray_type") or "").strip())
    use_default = default is not None and (not tray_configured or fingerprint_matches(departed, tray))

    if use_default:
        new_spool = await mint_tagless_spool(db, default_filament=default)
        _apply_new_fields(new_spool, new_fields)
        await _assign_from_setting(db, new_spool, printer_id, ams_id, tray_id, default)
        await db.commit()
        await _push_config(db, new_spool, printer_id, ams_id, tray_id, tray or {})
    else:
        if not tray_configured:
            # No configured tray to mint from and no default → cannot build an identity.
            raise ValueError("cannot replace tagless row: no tagless default and tray is not configured")
        new_spool = await mint_tagless_spool(db, tray=tray)
        _apply_new_fields(new_spool, new_fields)
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
    return new_spool


# --- W5 tagless fresh-roll prompt ------------------------------------------


def _live_tray(printer_id: int, ams_id: int, tray_id: int) -> dict | None:
    """The live tray dict for a slot from the printer's merged AMS state, or None.

    Regular AMS units only (tagless fresh-roll prompts key on AMS trays). Never
    raises — an unreachable printer reads as no tray.
    """
    try:
        state = printer_manager.get_status(printer_id)
    except Exception:  # noqa: BLE001 — resolution must never raise into the callback/route
        return None
    if state is None or not getattr(state, "raw_data", None):
        return None
    ams = state.raw_data.get("ams")
    if isinstance(ams, dict):
        ams = ams.get("ams", [])
    for unit in ams or []:
        if not isinstance(unit, dict):
            continue
        try:
            if int(unit.get("id", -1)) != ams_id:
                continue
        except (TypeError, ValueError):
            continue
        for tray in unit.get("tray", []) or []:
            if not isinstance(tray, dict):
                continue
            try:
                if int(tray.get("id", -1)) == tray_id:
                    return tray
            except (TypeError, ValueError):
                continue
    return None


def _tagless_fresh_payload(printer_id: int, ams_id: int, tray_id: int, spool: Spool) -> dict:
    """Frozen ``tagless_fresh_prompt`` WS payload (W5) — one origin for the live
    broadcast and the reconnect replay. Matches the frontend useWebSocket bridge +
    TaglessFreshPromptMessage: {printer_id, ams_id, tray_id, spool_id, remaining_g,
    material, rgba}."""
    return {
        "type": "tagless_fresh_prompt",
        "printer_id": printer_id,
        "ams_id": ams_id,
        "tray_id": tray_id,
        "spool_id": spool.id,
        "remaining_g": float((spool.label_weight or 0) - (spool.weight_used or 0)),
        "material": spool.material or "",
        "rgba": spool.rgba,
    }


async def _broadcast_tagless_fresh_prompt(printer_id: int, ams_id: int, tray_id: int, spool: Spool) -> None:
    await ws_manager.broadcast(_tagless_fresh_payload(printer_id, ams_id, tray_id, spool))


async def broadcast_tagless_fresh_dismissed(printer_id: int, ams_id: int, tray_id: int) -> None:
    """Cross-client clear of a tagless fresh-roll prompt (either answer, W5)."""
    await ws_manager.broadcast(
        {
            "type": "tagless_fresh_prompt_dismissed",
            "printer_id": printer_id,
            "ams_id": ams_id,
            "tray_id": tray_id,
        }
    )


def clear_fresh_prompt(printer_id: int, ams_id: int, tray_id: int) -> None:
    """Clear a slot's fresh-roll prompt unanswered entry (route answer / release)."""
    _fresh_prompt_unanswered.discard((printer_id, ams_id, tray_id))


async def _maybe_prompt_fresh_roll(db: AsyncSession, printer_id: int, ams_id: int, tray_id: int) -> None:
    """W5 over-consumption / fresh-roll prompt for a physical cycle on a tagless slot.

    Reads the slot's kept assignment. A SPENT bound row leaves the pending cycle for
    the W1 spent→mint transition (certain fresh roll — silent, no prompt). A NON-spent
    row consumed past :data:`_FRESH_ROLL_PROMPT_USED_FRAC` of its label, still
    unanswered for this cycle, broadcasts ``tagless_fresh_prompt`` and records the
    unanswered entry. Every non-spent outcome (prompt or sub-threshold) POPs the
    pending cycle — no latch is involved for non-spent rows.
    """
    key = (printer_id, ams_id, tray_id)
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
    spool = assignment.spool if assignment is not None else None
    if spool is None or not is_tagless_spool(spool):
        _pending_physical_cycles.discard(key)  # nothing tagless bound to latch/prompt
        return
    if spool.spent_at is not None:
        return  # leave the pending cycle for the W1 spent→mint transition (silent)
    label = spool.label_weight or 0
    used = spool.weight_used or 0
    if label > 0 and used >= _FRESH_ROLL_PROMPT_USED_FRAC * label and key not in _fresh_prompt_unanswered:
        await _broadcast_tagless_fresh_prompt(printer_id, ams_id, tray_id, spool)
        _fresh_prompt_unanswered.add(key)
        logger.info(
            "tagless_fresh_prompt broadcast: printer=%d AMS%d-T%d spool=%d used=%.0f/%d g",
            printer_id,
            ams_id,
            tray_id,
            spool.id,
            float(used),
            int(label),
        )
    _pending_physical_cycles.discard(key)  # non-spent processed (prompt or sub-threshold)


async def note_physical_cycle(printer_id: int, ams_id: int, tray_id: int) -> None:
    """Record a QUALIFIED physical roll swap on a slot — the W1 latch release + W5 prompt.

    Called (guarded, awaited) from ``ams_presence.on_ams_change`` on a genuine presence
    GAIN whose preceding absence lasted ≥ ``ams_presence._MIN_PHYSICAL_ABSENT_S``. Arms
    :data:`_pending_physical_cycles` (the spent-binding latch's release signal that
    branch (3) / :func:`maybe_autoconfigure_bare_tray` consume on the next push) then
    runs the W5 over-consumption prompt in its OWN session (mirrors
    ``ams_presence.on_printer_terminal``). Never raises — a farm-side failure must never
    break the AMS callback chain.
    """
    key = (printer_id, ams_id, tray_id)
    _pending_physical_cycles.add(key)
    try:
        from backend.app.core.database import async_session

        async with async_session() as db:
            await _maybe_prompt_fresh_roll(db, printer_id, ams_id, tray_id)
    except Exception:  # noqa: BLE001 — must never break the AMS callback chain
        logger.exception("note_physical_cycle W5 prompt failed for printer %d AMS%d-T%d", printer_id, ams_id, tray_id)


async def apply_fresh_roll(
    db: AsyncSession,
    spool: Spool,
    printer_id: int,
    ams_id: int,
    tray_id: int,
    *,
    brand: str | None = None,
    label_weight: int | None = None,
    cost_per_kg: float | None = None,
    note: str | None = None,
) -> Spool:
    """Answer a W5 fresh-roll prompt with "Fresh roll" — archive the current tagless
    row and mint+bind+push a replacement (default-vs-tray via the shared transition),
    applying the operator's optional brand/label_weight/cost_per_kg/note to the new row.
    Clears the prompt's unanswered entry. Returns the new spool. Raises ``ValueError``
    when the slot's live tray can't be resolved (the route maps it to HTTP 409)."""
    tray = _live_tray(printer_id, ams_id, tray_id)
    if tray is None:
        raise ValueError("slot is no longer readable")
    new_spool = await _replace_row_after_cycle(
        db,
        printer_id,
        ams_id,
        tray_id,
        tray,
        spool,
        new_fields={"brand": brand, "label_weight": label_weight, "cost_per_kg": cost_per_kg, "note": note},
    )
    clear_fresh_prompt(printer_id, ams_id, tray_id)
    _pending_physical_cycles.discard((printer_id, ams_id, tray_id))
    return new_spool


async def rebroadcast_unresolved_tagless_prompts(db: AsyncSession, send) -> int:
    """Replay unresolved ``tagless_fresh_prompt`` events to a (re)connecting client (W5).

    Sibling of ``spool_respool.rebroadcast_unresolved_respool_prompts``. The prompt WS
    event is fire-once (``ws_manager.broadcast`` keeps no backlog), so a client that was
    disconnected when a prompt fired never learns of it. Re-validate each unanswered
    entry against durable + live state before re-sending — the assignment must still
    exist, the spool must still be tagless + non-spent + non-archived, and the slot must
    still be physically present. A stale entry is skipped (never mutates the set — the
    per-cycle set is cleared only by an answer or a new cycle). Returns the count
    re-sent. Never raises (a reconnect must not break on a farm-side hook).
    """
    snapshot = list(_fresh_prompt_unanswered)
    sent = 0
    for printer_id, ams_id, tray_id in snapshot:
        try:
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
            spool = assignment.spool if assignment is not None else None
            if spool is None or not is_tagless_spool(spool):
                continue
            if spool.spent_at is not None or spool.archived_at is not None:
                continue
            tray = _live_tray(printer_id, ams_id, tray_id)
            if tray is None or not tray_present(tray):
                continue
            await send(_tagless_fresh_payload(printer_id, ams_id, tray_id, spool))
            sent += 1
        except Exception:  # noqa: BLE001 — one slot's failure must not abort the replay
            logger.exception(
                "tagless_fresh_prompt re-broadcast failed for printer %s AMS%d-T%d", printer_id, ams_id, tray_id
            )
    if sent:
        logger.info("Re-broadcast %d unresolved tagless_fresh_prompt(s) to a (re)connecting client", sent)
    return sent


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


def _mint_settling(printer_id: int, ams_id: int, tray_id: int) -> bool:
    """True while a just-inserted spool's gain is still settling (F1).

    The insert's first MQTT push carries the slot's CONFIG but not yet its tag —
    the firmware's own RFID read lands ~1 s later. Minting on that push creates a
    provisional tagless row the tag read immediately hard-deletes. A gain younger
    than :data:`_MINT_SETTLE_S` therefore defers a FRESH mint by one push; a slot
    with no recorded gain (restart, never observed) reads as settled, so the defer
    can never wedge a slot.
    """
    age = ams_presence.recent_gain_age(printer_id, ams_id, tray_id)
    return age is not None and age < _MINT_SETTLE_S


def _printer_busy(printer_id: int) -> bool:
    """True while the printer is mid-job (RUNNING/PAUSE).

    Delegates to ``ams_presence._printer_running`` rather than re-deriving the
    predicate: that module already owns the running-state reading every AMS-side
    guard shares, and a second copy is exactly the drift this fork avoids. Never
    raises — an unreachable printer reads as busy (the conservative answer for the
    idle-only reconcile below).
    """
    try:
        return ams_presence._printer_running(printer_manager.get_status(printer_id))
    except Exception:  # noqa: BLE001 — must never break the AMS callback chain
        return True


def _live_nozzle_temp(tray: dict, key: str) -> int | None:
    """A live tray's reported nozzle temperature as an int, or None when unset.

    Mirrors ``spool_tag_matcher.parse_tray_fields``'s convention: a missing,
    unparseable, or ZERO value is "unset" (the firmware reports 0 for an
    unconfigured temp). An unset temp is no evidence of divergence for the identity
    reconcile — only a temp the firmware POSITIVELY reports can diverge.
    """
    try:
        v = int(tray.get(key))
    except (TypeError, ValueError):
        return None
    return v or None


def _temp_diverges(resolved_temp: int | None, live_temp: int | None) -> bool:
    """True only when the firmware POSITIVELY reports a nozzle temp differing from the
    resolver's. A resolver that produced no temp, or a slot that reported none this
    push, is not divergence on that dimension (the id / other temp still can be)."""
    return resolved_temp is not None and live_temp is not None and int(resolved_temp) != live_temp


async def _maybe_reconcile_slot_identity(
    db: AsyncSession, printer_id: int, ams_id: int, tray_id: int, tray: dict, spool: Spool
) -> bool:
    """One-shot re-push when a bound slot's LIVE identity diverges from the resolver (E3/W6).

    The rebind branch is one natural observation point (the slot is bound,
    configured, and the firmware has just told us what it holds); the idle
    terminal-callback sweep (:func:`reconcile_bound_slot_identities`) is the other.
    Convergence is compared across the WHOLE wire identity the firmware's auto-refill
    backup group keys on — ``tray_info_idx`` AND both nozzle temps — not
    ``tray_info_idx`` alone: the 011-H2S state had trays on the right ``GFG02`` id but
    STALE 220/260 temps beside a 230/270 peer, which a tray_info_idx-only check
    declared converged (burning the one shot without ever healing the split group). A
    temp the firmware did not report this push (missing/0 == unset) is no evidence of
    divergence and never forces a push. When ANY dimension diverges, re-push the slot
    config once so the fleet converges without a migration or a repair tool.

    Gated three ways: the printer must be IDLE (a config write mid-print is exactly
    what the AMS-write doctrine forbids), the client must not be refusing AMS writes
    (drying / identifying / identify gate), and a per-slot once-per-process set caps
    it at a single evaluation so a resolver that keeps disagreeing with the firmware
    can never loop and a converged slot never re-resolves. Only DEFERRALS (busy /
    disconnected / refused write) leave the shot unconsumed — a later push retries.
    Returns True when a re-push was dispatched. Never raises.
    """
    key = (printer_id, ams_id, tray_id)
    if key in _identity_reconciled:
        return False
    try:
        from backend.app.services.slicer_filament_resolver import resolve_slicer_filament

        resolved, _setting_id, _sub_brand, r_tmin, r_tmax = await resolve_slicer_filament(
            db=db,
            current_user=None,
            slicer_filament=spool.slicer_filament,
            slicer_filament_name=spool.slicer_filament_name,
            material=spool.material,
            rgba=spool.rgba,
            nozzle_temp_min=spool.nozzle_temp_min,
            nozzle_temp_max=spool.nozzle_temp_max,
        )
        live_idx = (tray.get("tray_info_idx") or "").strip()
        live_tmin = _live_nozzle_temp(tray, "nozzle_temp_min")
        live_tmax = _live_nozzle_temp(tray, "nozzle_temp_max")
        converged = (
            resolved == live_idx
            and not _temp_diverges(r_tmin, live_tmin)
            and not _temp_diverges(r_tmax, live_tmax)
        )
        if not resolved or converged:
            # Nothing resolvable, or the slot already carries the whole identity: the
            # reconcile is DONE for this slot. Consuming the shot here is what keeps a
            # converged fleet (the steady state) at one resolver call per slot per
            # process rather than one per AMS push.
            _identity_reconciled.add(key)
            return False
        if _printer_busy(printer_id):
            return False  # idle-only — retried on a later push
        client = printer_manager.get_client(printer_id)
        if client is None:
            return False
        refusal = client.ams_write_refusal(ams_id)
        if refusal is not None:
            logger.debug(
                "Deferring slot-identity reconcile for printer %d AMS%d-T%d: %s",
                printer_id,
                ams_id,
                tray_id,
                refusal,
            )
            return False
        _identity_reconciled.add(key)  # one shot, armed BEFORE the push so it cannot loop
        logger.info(
            "Reconciling slot identity on printer %d AMS%d-T%d: live idx=%r temps=%s/%s -> "
            "idx=%r temps=%s/%s (spool %d)",
            printer_id,
            ams_id,
            tray_id,
            live_idx,
            live_tmin,
            live_tmax,
            resolved,
            r_tmin,
            r_tmax,
            spool.id,
        )
        await _push_config(db, spool, printer_id, ams_id, tray_id, tray)
        return True
    except Exception:  # noqa: BLE001 — a reconcile failure must not change the rebind outcome
        logger.exception("Slot-identity reconcile failed for printer %d AMS%d-T%d", printer_id, ams_id, tray_id)
        return False


async def reconcile_bound_slot_identities(db: AsyncSession, printer_id: int) -> int:
    """Idle sweep: re-push every bound+configured slot whose live identity diverges (E3/W6).

    The SECOND call site for :func:`_maybe_reconcile_slot_identity` (one
    implementation). ``handle_tagless_slot``'s rebind branch only reaches the
    reconcile from an AMS-change push, and on a busy farm those pushes overwhelmingly
    arrive WHILE printing — so the idle gate defers every one and a divergent slot
    (011-H2S: tray_info_idx / stale-temps split) never gets its idle evaluation.
    Called from the printer-terminal callback, where the printer is idle by
    construction, this iterates the printer's live AMS trays and runs the same
    once-per-process reconcile on every slot that is bound (a :class:`SpoolAssignment`)
    and configured (non-empty ``tray_type`` AND ``tray_info_idx``).

    Never raises (module never-crash-guard style); returns 0 on any unavailability
    (unreachable printer, no merged state). Returns the number of slots re-pushed.
    """
    pushed = 0
    try:
        state = printer_manager.get_status(printer_id)
    except Exception:  # noqa: BLE001 — an unreachable printer reconciles nothing
        return 0
    if state is None or not getattr(state, "raw_data", None):
        return 0
    ams = state.raw_data.get("ams")
    if isinstance(ams, dict):
        ams = ams.get("ams", [])
    try:
        for unit in ams or []:
            if not isinstance(unit, dict):
                continue
            try:
                ams_id = int(unit.get("id", -1))
            except (TypeError, ValueError):
                continue
            for tray in unit.get("tray", []) or []:
                if not isinstance(tray, dict):
                    continue
                if not (tray.get("tray_type") or "").strip():
                    continue  # not configured — nothing to reconcile
                if not (tray.get("tray_info_idx") or "").strip():
                    continue  # not configured
                try:
                    tray_id = int(tray.get("id", -1))
                except (TypeError, ValueError):
                    continue
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
                spool = assignment.spool if assignment is not None else None
                if spool is None:
                    continue  # not bound
                if await _maybe_reconcile_slot_identity(db, printer_id, ams_id, tray_id, tray, spool):
                    pushed += 1
    except Exception:  # noqa: BLE001 — a farm-side sweep must never break the terminal callback
        logger.exception("reconcile_bound_slot_identities failed for printer %d", printer_id)
    return pushed


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
        spool = existing_assignment.spool

        # (2) Bound to a TAGGED spool → RFID/respool flows own it (this also lets
        # a spent TAGGED spool reach the respool gate). Not ours.
        if not is_tagless_spool(spool):
            return False

        # (3) Bound spool is spent → the spent binding IS the durable "ran dry" latch
        # (W1). Only a QUALIFIED physical roll swap — a pending cycle recorded by
        # note_physical_cycle after a ≥ _MIN_PHYSICAL_ABSENT_S absence — releases it
        # into a fresh mint; with no pending cycle the runout-instant state flap keeps
        # the binding, so a phantom fresh row can no longer be minted over a
        # still-present spool. _replace_row_after_cycle default-mints when the tray
        # still carries the departed config (firmware leftover), else tray-mints.
        if spool.spent_at is not None:
            if not _tray_loaded(tray):
                return True  # dead roll re-seated, filament not fed — no churn
            if key not in _pending_physical_cycles:
                logger.info(
                    "Spent binding latched — awaiting physical roll swap (printer %d AMS%d-T%d spool %d)",
                    printer_id,
                    ams_id,
                    tray_id,
                    spool.id,
                )
                return True  # keep the binding: no archive, no unlink, no mint
            _pending_physical_cycles.discard(key)  # consume the cycle
            await _replace_row_after_cycle(db, printer_id, ams_id, tray_id, tray, spool)
            return True

        # (4) Same filament → rebind: refresh a drifted fingerprint, write NOTHING
        # to the spool (operator edits are sacred). This is also the one place the
        # farm sees an already-bound, already-configured slot's LIVE identity, so
        # the one-shot identity reconcile (E3) rides here.
        if fingerprint_matches(spool, tray):
            _refresh_assignment_fingerprint(existing_assignment, tray)
            await db.commit()
            await _maybe_reconcile_slot_identity(db, printer_id, ams_id, tray_id, tray, spool)
            return True

        # (5) Different filament → mint a fresh row. Settle first: an insertion's
        # first push (config seen, tag not yet read) must not mint a row the
        # firmware's own RFID read then destroys. Checked BEFORE the unlink so a
        # defer leaves the slot's state untouched for the next push.
        if _mint_settling(printer_id, ams_id, tray_id):
            logger.debug(
                "Deferring tagless mint for printer %d AMS%d-T%d: insertion still settling",
                printer_id,
                ams_id,
                tray_id,
            )
            return True

        # Unlink (old spool stays active, just unbound), then mint from the tray.
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

    # Settle before minting a FRESH row (F1) — the slot-move above re-binds an
    # EXISTING ledger row and is deliberately not deferred.
    if _mint_settling(printer_id, ams_id, tray_id):
        logger.debug(
            "Deferring tagless mint for printer %d AMS%d-T%d: insertion still settling",
            printer_id,
            ams_id,
            tray_id,
        )
        return True

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
    # drying tray (HMS 0700_C069). Return BEFORE the retry window is stamped so it is
    # not burned on a push that never went out. force= bypasses only the retry
    # window — never these hardware-state guards.
    if ams_presence.identify_in_flight(printer_id, ams_id, tray_id) or ams_presence.unit_drying(printer_id, ams_id):
        logger.debug(
            "Deferring bare-tray auto-config for printer %d AMS%d-T%d: AMS identify/drying in progress",
            printer_id,
            ams_id,
            tray_id,
        )
        return False

    key = (printer_id, ams_id, tray_id)

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

    # W1: a spent ams_auto binding is the "ran dry" latch — never re-push a spent
    # row's config. Only a QUALIFIED physical roll swap (a pending cycle recorded by
    # note_physical_cycle) releases it into the same archive→unlink→default-mint→push
    # transition as branch (3). Checked BEFORE stamping the retry window so a latched
    # slot never burns it.
    if assignment is not None and assignment.spool is not None and assignment.spool.spent_at is not None:
        if key not in _pending_physical_cycles:
            return False  # latched — no re-push of a spent slot's config
        _pending_physical_cycles.discard(key)  # consume the cycle
        await _replace_row_after_cycle(db, printer_id, ams_id, tray_id, tray, assignment.spool)
        return True

    # Settle before the FIRST mint on this slot (F1): a bare tray whose spool was
    # just inserted may still have the firmware's RFID read in flight, and a row
    # minted now is the one the tag read hard-deletes. A slot we ALREADY track only
    # re-pushes config, so it is not deferred. Before the retry window, so the defer
    # does not burn it.
    if assignment is None and _mint_settling(printer_id, ams_id, tray_id):
        logger.debug(
            "Deferring bare-tray mint for printer %d AMS%d-T%d: insertion still settling",
            printer_id,
            ams_id,
            tray_id,
        )
        return False

    # The retry-cadence gate is the LAST guard: every ineligible/deferred path above
    # returns without stamping, so the window is armed only by an attempt that is
    # actually about to publish. force= clears the previous stamp instead of skipping
    # the gate, so a forced push still re-arms the cadence for the pushes after it.
    if force:
        _autoconfig_window.clear(key)
    if not _autoconfig_window.allow(key):
        return False  # config attempt still inside its retry window

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


# --- dedup lifecycle -------------------------------------------------------


def clear_autoconfig_dedup(printer_id: int, ams_id: int, tray_id: int) -> None:
    """Drop the bare-tray retry timestamp for a slot (called when it empties)."""
    _autoconfig_window.clear((printer_id, ams_id, tray_id))


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
