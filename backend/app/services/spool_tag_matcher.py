"""RFID tag matching and auto-assignment for spool inventory."""

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spool_k_profile import SpoolKProfile
from backend.app.utils.printer_models import extruder_for_ams, nozzle_for_ams_unit
from backend.app.utils.retry_window import RetryWindow
from backend.app.utils.tag_normalization import (
    normalize_tag_uid as _normalize_tag_uid,
    normalize_tray_uuid as _normalize_tray_uuid,
)

logger = logging.getLogger(__name__)

# Zero-value constants for tag validation
ZERO_TAG_UID = "0000000000000000"
ZERO_TRAY_UUID = "00000000000000000000000000000000"

# Minimum spacing between K-profile drift re-applies on ONE slot. The drift check
# runs on every AMS push, and an identify's tray-state flap republishes cali_idx
# repeatedly — un-gated that is one extrusion_cali_sel per push into an AMS that is
# already mid-read. One attempt per window per slot; a refused push waits out the
# window instead of re-firing immediately.
_KDRIFT_RETRY_S = 30.0

_kdrift_window = RetryWindow(_KDRIFT_RETRY_S)


def stamp_first_loaded(spool: Spool) -> None:
    """Stamp ``Spool.first_loaded_at`` the first time a spool enters service.

    Single stamping origin for the FIFO substrate: called wherever a spool
    acquires its first ``SpoolAssignment`` — the RFID auto-assign path
    (:func:`auto_assign_spool`), the manual ``POST /assignments`` route, and the
    tagless bare-tray auto-config (``services.spool_tagless``). Idempotent: only
    writes when the column is still NULL, so a spool pulled and re-assigned keeps
    its original in-service timestamp (the spool-selection policy orders
    candidates oldest-first).

    Lives here — not in ``spool_tagless`` — because this is the lowest module all
    three assignment-creating callers already import: ``spool_tagless`` imports
    ``spool_tag_matcher`` (not the reverse) and the inventory route imports it
    too, so defining it here is the one clean direction with no import cycle.
    """
    if spool.first_loaded_at is None:
        spool.first_loaded_at = datetime.utcnow()


def is_valid_tag(tag_uid: str, tray_uuid: str) -> bool:
    """Check if a tag/UUID pair contains a non-zero, non-empty value."""
    uid = _normalize_tag_uid(tag_uid)
    uuid = _normalize_tray_uuid(tray_uuid)
    uid_valid = bool(uid) and uid != ZERO_TAG_UID and uid != "0" * len(uid)
    uuid_valid = bool(uuid) and uuid != ZERO_TRAY_UUID and uuid != "0" * len(uuid)
    return uid_valid or uuid_valid


def is_bambu_tag(tag_uid: str, tray_uuid: str, tray_info_idx: str) -> bool:
    """Check if an AMS tray contains a Bambu Lab RFID spool (has valid UUID or slicer preset)."""
    uuid = _normalize_tray_uuid(tray_uuid)
    uuid_valid = bool(uuid) and uuid != ZERO_TRAY_UUID and uuid != "0" * len(uuid)
    has_preset = bool(tray_info_idx)
    return uuid_valid or (is_valid_tag(tag_uid, tray_uuid) and has_preset)


@dataclass
class ParsedTrayFields:
    """Identity + metadata resolved from an AMS tray MQTT dict.

    Shared output of :func:`parse_tray_fields`. Consumed by
    :func:`create_spool_from_tray` (legacy Bambu auto-create) and the reused-tag
    re-spool service (``spool_respool``). ``weight_used`` is derived from the AMS
    remain% and is only meaningful for the legacy auto-create path; the re-spool
    flow forces a fresh 0 g by definition and ignores it.
    """

    material: str
    subtype: str | None
    color_name: str | None
    rgba: str | None
    label_weight: int
    core_weight: int
    weight_used: float
    slicer_filament: str | None
    slicer_filament_name: str | None
    nozzle_temp_min: int | None
    nozzle_temp_max: int | None
    tag_uid: str | None
    tray_uuid: str | None


async def parse_tray_fields(db: AsyncSession, tray_data: dict) -> ParsedTrayFields:
    """Resolve material/subtype/color/temps/tag identity from an AMS tray dict.

    Extracted verbatim from :func:`create_spool_from_tray` so both the legacy
    auto-create path and the reused-tag re-spool service resolve identity the
    same way (material split, gradient/multi-color subtype upgrade, color-catalog
    lookup, core-weight catalog, slicer preset name, remain%→weight_used, and the
    zero-tag normalization). Behaviour of :func:`create_spool_from_tray` is
    unchanged — it now delegates the parse here and only owns spool construction.
    """
    from backend.app.models.color_catalog import ColorCatalogEntry
    from backend.app.models.spool_catalog import SpoolCatalogEntry

    tray_type = tray_data.get("tray_type", "")  # "PLA"
    tray_sub_brands = tray_data.get("tray_sub_brands", "")  # "PLA Basic"
    tray_color = tray_data.get("tray_color", "FFFFFFFF")  # RRGGBBAA
    tray_id_name = tray_data.get("tray_id_name", "")  # Color name e.g. "Jade White"
    tag_uid = _normalize_tag_uid(tray_data.get("tag_uid", ""))
    tray_uuid = _normalize_tray_uuid(tray_data.get("tray_uuid", ""))
    tray_info_idx = tray_data.get("tray_info_idx", "")
    nozzle_min = tray_data.get("nozzle_temp_min", 0)
    nozzle_max = tray_data.get("nozzle_temp_max", 0)
    label_weight = int(tray_data.get("tray_weight", 1000))

    # Parse material and subtype from tray_sub_brands ("PLA Basic" → material="PLA", subtype="Basic")
    material = tray_type or "PLA"
    subtype = None
    if tray_sub_brands and " " in tray_sub_brands:
        parts = tray_sub_brands.split(" ", 1)
        if parts[0].upper() == material.upper():
            subtype = parts[1]
        else:
            # tray_sub_brands is the full material name (e.g. "PETG-HF")
            material = tray_sub_brands
    elif tray_sub_brands and tray_sub_brands.upper() != material.upper():
        material = tray_sub_brands

    # Upgrade subtype for gradient/multi-color variants based on tray_id_name color code.
    # Firmware sends tray_sub_brands="PLA Basic" for gradients and "PLA Silk" for dual/tri-color,
    # but the M*/T* suffix in tray_id_name distinguishes them:
    #   A00-M* = PLA Basic Gradient, A05-M* = PLA Silk Dual Color, A05-T* = PLA Silk Tri Color
    if tray_id_name and "-" in tray_id_name:
        color_code = tray_id_name.split("-", 1)[1]
        if color_code and color_code[0] == "M":
            # M* = gradient for PLA Basic (A00), dual-color for PLA Silk (A05)
            prefix = tray_id_name.split("-", 1)[0]
            if prefix == "A05":
                subtype = "Dual Color"
            else:
                subtype = "Gradient"
        elif color_code and color_code[0] == "T":
            subtype = "Tri Color"

    # Resolve color name from the color catalog by hex. The catalog is the single
    # source of truth — tray_id_name codes (e.g. "A17-R1") are NOT globally unique
    # across material families (A17-R1 is PLA Translucent Cherry Pink; A01-R1 is
    # PLA Matte Scarlet Red), so a suffix-based fallback would pick the wrong name.
    # See #857.
    #
    # Hex isn't unique either: #FFFFFF maps to "Jade White" (PLA Basic), "Ivory
    # White" (PLA Matte), and "White" (PLA Silk) in the Bambu catalog. Filter by
    # the printer-reported material variant (`tray_sub_brands`, e.g. "PLA Matte")
    # so a new Ivory White roll doesn't get auto-named Jade White just because
    # PLA Basic happens to come first in catalog insertion order. See #1227.
    rgba = tray_color if tray_color else None
    color_name = None

    # Transparent filament (#1545): the AMS reports alpha=00 for clear spools.
    # Skip the catalog lookup — the catalog only stores RGB so 000000 would
    # resolve to "Black" (or whatever else lives at that RGB), which is exactly
    # the bug the cream rewrite in parse_ams_tray used to paper over. Store
    # "Clear" directly and let the frontend's resolveSpoolColorName +
    # hexToColorName render the swatch as a checkerboard.
    if rgba and len(rgba) == 8 and rgba[6:8].lower() == "00":
        color_name = "Clear"
    elif rgba and len(rgba) >= 6:
        hex_prefix = f"#{rgba[:6].upper()}"
        cat_query = (
            select(ColorCatalogEntry)
            .where(func.upper(ColorCatalogEntry.hex_color) == hex_prefix)
            .where(func.upper(ColorCatalogEntry.manufacturer) == "BAMBU LAB")
        )
        if tray_sub_brands:
            cat_query = cat_query.where(func.upper(ColorCatalogEntry.material) == tray_sub_brands.upper())
        # Deterministic tiebreak when the material filter can't disambiguate
        # (e.g. third-party spools with empty tray_sub_brands).
        cat_query = cat_query.order_by(ColorCatalogEntry.id).limit(1)
        cat_result = await db.execute(cat_query)
        entry = cat_result.scalar_one_or_none()
        if entry:
            color_name = entry.color_name

    # If tray_id_name is a human-readable name (no "-" code), fall back to it.
    if not color_name and tray_id_name and "-" not in tray_id_name:
        color_name = tray_id_name

    logger.info(
        "Color resolve: tray_id_name=%r rgba=%r → resolved=%r",
        tray_id_name,
        rgba,
        color_name,
    )

    # Look up core weight from spool catalog
    core_weight = 250  # Default for Bambu Lab plastic spools
    cat_result = await db.execute(select(SpoolCatalogEntry).where(SpoolCatalogEntry.name.ilike("Bambu Lab%")).limit(10))
    for entry in cat_result.scalars().all():
        # Pick the best match (prefer exact, fallback to first Bambu Lab entry)
        core_weight = entry.weight
        break

    # Resolve slicer filament name from builtin table
    slicer_filament_name = None
    if tray_info_idx:
        try:
            from backend.app.api.routes.cloud import _BUILTIN_FILAMENT_NAMES

            slicer_filament_name = _BUILTIN_FILAMENT_NAMES.get(tray_info_idx)
        except Exception:
            pass
        # Fallback: use tray_sub_brands as the display name
        if not slicer_filament_name and tray_sub_brands:
            slicer_filament_name = tray_sub_brands

    # Calculate initial weight_used from AMS remain percentage
    remain_raw = tray_data.get("remain")
    try:
        remain_pct = int(remain_raw) if remain_raw is not None else 100
    except (TypeError, ValueError):
        remain_pct = 100
    # Clamp to valid range: negative means unknown, >100 is invalid
    if remain_pct < 0 or remain_pct > 100:
        remain_pct = 100  # Unknown → assume full
    weight_used = round(label_weight * (100 - remain_pct) / 100.0, 1)

    return ParsedTrayFields(
        material=material,
        subtype=subtype,
        color_name=color_name,
        rgba=rgba,
        label_weight=label_weight,
        core_weight=core_weight,
        weight_used=weight_used,
        slicer_filament=tray_info_idx or None,
        slicer_filament_name=slicer_filament_name,
        nozzle_temp_min=int(nozzle_min) if nozzle_min else None,
        nozzle_temp_max=int(nozzle_max) if nozzle_max else None,
        tag_uid=tag_uid if tag_uid and tag_uid != ZERO_TAG_UID else None,
        tray_uuid=tray_uuid if tray_uuid and tray_uuid != ZERO_TRAY_UUID else None,
    )


async def create_spool_from_tray(db: AsyncSession, tray_data: dict) -> Spool:
    """Create a new Spool inventory entry from AMS tray MQTT data.

    Extracts material, subtype, color, temps, and tag info from the tray dict
    (via :func:`parse_tray_fields`). Looks up core_weight from the spool catalog
    if a Bambu Lab entry matches.
    """
    parsed = await parse_tray_fields(db, tray_data)

    spool = Spool(
        material=parsed.material,
        subtype=parsed.subtype,
        color_name=parsed.color_name,
        rgba=parsed.rgba,
        brand="Bambu Lab",
        label_weight=parsed.label_weight,
        core_weight=parsed.core_weight,
        weight_used=parsed.weight_used,
        slicer_filament=parsed.slicer_filament,
        slicer_filament_name=parsed.slicer_filament_name,
        nozzle_temp_min=parsed.nozzle_temp_min,
        nozzle_temp_max=parsed.nozzle_temp_max,
        tag_uid=parsed.tag_uid,
        tray_uuid=parsed.tray_uuid,
        data_origin="rfid_auto",
        tag_type="bambulab",
    )
    # Initialize relationships BEFORE db.add() to prevent lazy loads.
    # Setting them after flush() would trigger a lazy load because SQLAlchemy
    # loads the current collection before replacing it on a persistent object.
    # They must also be set before add() because cascade processing during
    # add/flush accesses these collections, and back_populates resolution
    # when creating SpoolAssignment runs synchronously outside the greenlet.
    spool.k_profiles = []
    spool.assignments = []
    db.add(spool)
    await db.flush()

    logger.info(
        "Auto-created spool %d from AMS tray data: %s %s %s (tag=%s uuid=%s)",
        spool.id,
        parsed.material,
        parsed.subtype or "",
        parsed.color_name or "",
        parsed.tag_uid or "",
        parsed.tray_uuid or "",
    )
    return spool


async def find_matching_untagged_spool(db: AsyncSession, tray_data: dict) -> Spool | None:
    """Find an existing untagged Bambu inventory spool matching material/color.

    When a Bambu Lab spool is detected in the AMS but no tag match exists,
    check if the user has a manually-added spool with the same properties
    that hasn't been linked to a tag yet. Returns the best match (#918):

    - **Brand**: only consider spools whose brand is unspecified or contains
      "bambu" (case-insensitive — covers both "Bambu" and "Bambu Lab" as
      stored by the form's brand dropdown). This prevents a same-color
      Polymaker / generic spool from accidentally attracting a Bambu UUID.
    - **Subtype**: prefer an exact match (e.g. AMS "Basic" → spool subtype
      "Basic"), but fall back to a NULL-subtype spool — the form's Quick Add
      mode leaves subtype empty, so bulk-logged spools rely on this fallback
      to attract their RFID tag instead of duplicating on first AMS read.
    - **FIFO** within each preference group (user likely logged in purchase
      order).
    """
    tray_type = tray_data.get("tray_type", "")
    tray_sub_brands = tray_data.get("tray_sub_brands", "")
    tray_color = tray_data.get("tray_color", "")  # RRGGBBAA

    if not tray_type or not tray_color:
        return None

    # Parse material the same way create_spool_from_tray does
    material = tray_type
    subtype = None
    if tray_sub_brands and " " in tray_sub_brands:
        parts = tray_sub_brands.split(" ", 1)
        if parts[0].upper() == material.upper():
            subtype = parts[1]
        else:
            material = tray_sub_brands
    elif tray_sub_brands and tray_sub_brands.upper() != material.upper():
        material = tray_sub_brands

    # Upgrade subtype for gradient/multi-color variants (same logic as create_spool_from_tray)
    tray_id_name = tray_data.get("tray_id_name", "")
    if tray_id_name and "-" in tray_id_name:
        color_code = tray_id_name.split("-", 1)[1]
        if color_code and color_code[0] == "M":
            prefix = tray_id_name.split("-", 1)[0]
            if prefix == "A05":
                subtype = "Dual Color"
            else:
                subtype = "Gradient"
        elif color_code and color_code[0] == "T":
            subtype = "Tri Color"

    # Active, untagged spools matching material + color + Bambu-or-unset brand.
    #
    # Two exclusions keep an incoming Bambu RFID tag from hijacking a tagless
    # row (silent-tracking work item): never attract a spool that is already
    # bound to an AMS slot (``~assignments.any()``), and never attract an
    # auto-minted tagless row (``data_origin == "ams_auto"``) — those are the
    # farm's own silently-tracked third-party spools, not manually-logged Bambu
    # rolls awaiting their tag.
    query = (
        select(Spool)
        .options(selectinload(Spool.k_profiles), selectinload(Spool.assignments))
        .where(
            Spool.archived_at.is_(None),
            Spool.tag_uid.is_(None),
            Spool.tray_uuid.is_(None),
            ~Spool.assignments.any(),
            or_(Spool.data_origin.is_(None), Spool.data_origin != "ams_auto"),
            func.upper(Spool.material) == material.upper(),
            func.upper(Spool.rgba) == tray_color.upper(),
            or_(
                Spool.brand.is_(None),
                func.lower(Spool.brand).like("%bambu%"),
            ),
        )
    )

    if subtype:
        # Exact subtype OR NULL fallback. The CASE in ORDER BY ensures an
        # exact-subtype row beats a NULL-subtype row when both exist; FIFO
        # within each group.
        query = query.where(
            or_(
                func.upper(Spool.subtype) == subtype.upper(),
                Spool.subtype.is_(None),
            )
        ).order_by(
            case((func.upper(Spool.subtype) == subtype.upper(), 0), else_=1),
            Spool.created_at.asc(),
        )
    else:
        query = query.where(Spool.subtype.is_(None)).order_by(Spool.created_at.asc())

    query = query.limit(1)

    result = await db.execute(query)
    spool = result.scalar_one_or_none()

    if spool:
        logger.info(
            "Found matching untagged spool %d: %s %s %s (rgba=%s)",
            spool.id,
            spool.brand or "",
            spool.material,
            spool.color_name or "",
            spool.rgba or "",
        )

    return spool


async def link_tag_to_inventory_spool(db: AsyncSession, spool: Spool, tray_data: dict) -> None:
    """Link RFID tag data from AMS tray to an existing inventory spool."""
    tag_uid = tray_data.get("tag_uid", "")
    tray_uuid = tray_data.get("tray_uuid", "")
    tray_info_idx = tray_data.get("tray_info_idx", "")

    if tag_uid and tag_uid != ZERO_TAG_UID:
        spool.tag_uid = tag_uid
    if tray_uuid and tray_uuid != ZERO_TRAY_UUID:
        spool.tray_uuid = tray_uuid
    spool.data_origin = "rfid_linked"
    spool.tag_type = "bambulab"

    # Update slicer preset if not already set
    if tray_info_idx and not spool.slicer_filament:
        spool.slicer_filament = tray_info_idx
        try:
            from backend.app.api.routes.cloud import _BUILTIN_FILAMENT_NAMES

            name = _BUILTIN_FILAMENT_NAMES.get(tray_info_idx)
            if name and not spool.slicer_filament_name:
                spool.slicer_filament_name = name
        except Exception:
            pass

    await db.flush()
    logger.info(
        "Linked RFID tag to existing spool %d (tag=%s uuid=%s origin=rfid_linked)",
        spool.id,
        spool.tag_uid or "",
        spool.tray_uuid or "",
    )


async def get_spool_by_tag(db: AsyncSession, tag_uid: str, tray_uuid: str, *, converge: bool = False) -> Spool | None:
    """Look up an active spool by RFID tag UID or Bambu Lab tray UUID.

    Prefers tray_uuid match over tag_uid (more reliable). Falls back to first-char /
    short-UID *variance* matching for the same physical chip read slightly
    differently across readers (one reader reports "8C0E…", another "1C0E…").

    ``converge`` (write-owning callers ONLY — the RFID auto-assign + unlink-damping
    paths in ``main.on_ams_change``): on a variance match, persist the SCANNED
    tag_uid + tray_uuid back onto the matched spool ONCE, so the next read is an
    exact match and the auto-unlink ⇄ variance-rematch loop cannot recur (printer 3
    looped all day 2026-07-14). Read-only callers (SpoolBuddy lookup, re-spool donor
    resolution) leave it False and never write. A genuine different-roll signal —
    the scanned tray_uuid already owned by a DIFFERENT non-archived spool —
    suppresses the variance match entirely, protecting the reused-tag re-spool
    sibling guard (which keys on tray_uuid uniqueness).
    """
    tray_uuid_norm = _normalize_tray_uuid(tray_uuid)
    tag_uid_norm = _normalize_tag_uid(tag_uid)
    tray_uuid_valid = bool(
        tray_uuid_norm and tray_uuid_norm != ZERO_TRAY_UUID and tray_uuid_norm != "0" * len(tray_uuid_norm)
    )

    async def _accept_variance(candidate: Spool, kind: str) -> Spool | None:
        """Different-roll guard + one-time convergence for a variance match.

        Returns ``candidate`` to accept the match, or None to skip it. When the
        scanned tray_uuid is valid and already belongs to a DIFFERENT non-archived
        spool, this is a genuine different-roll signal (a reused tag on a fresh
        roll): skip the variance match so exact/uuid matching — or the caller's
        unlink — stands, and never converge onto a colliding tray_uuid. On a real
        variance match, ``converge`` callers persist the scanned identifiers onto
        the spool once so the reader-variance loop cannot recur.
        """
        if tray_uuid_valid:
            other = await db.execute(
                select(Spool.id)
                .where(
                    func.upper(Spool.tray_uuid) == tray_uuid_norm,
                    Spool.archived_at.is_(None),
                    Spool.id != candidate.id,
                )
                .limit(1)
            )
            if other.scalar_one_or_none() is not None:
                logger.warning(
                    "Skipping %s variance match on spool %d: scanned tray_uuid %s already belongs to a "
                    "different non-archived spool (different roll — reused tag, one tag per donor)",
                    kind,
                    candidate.id,
                    tray_uuid_norm,
                )
                return None
        logger.warning(
            "Matched spool %d via %s variance: stored=%s → scanned=%s",
            candidate.id,
            kind,
            _normalize_tag_uid(candidate.tag_uid),
            tag_uid_norm,
        )
        if converge:
            old_uid = candidate.tag_uid
            old_uuid = candidate.tray_uuid
            candidate.tag_uid = tag_uid_norm
            if tray_uuid_valid:
                candidate.tray_uuid = tray_uuid_norm
            await db.flush()
            logger.warning(
                "Converged stored tag identifiers on spool %d: tag_uid %s→%s tray_uuid %s→%s",
                candidate.id,
                old_uid,
                candidate.tag_uid,
                old_uuid,
                candidate.tray_uuid,
            )
        return candidate

    # Try tray_uuid first (Bambu Lab spools — more reliable)
    if tray_uuid_valid:
        result = await db.execute(
            select(Spool)
            .options(selectinload(Spool.k_profiles), selectinload(Spool.assignments))
            .where(func.upper(Spool.tray_uuid) == tray_uuid_norm, Spool.archived_at.is_(None))
            .limit(1)
        )
        spool = result.scalar_one_or_none()
        if spool:
            return spool

    # Fall back to tag_uid
    if tag_uid_norm and tag_uid_norm != ZERO_TAG_UID and tag_uid_norm != "0" * len(tag_uid_norm):
        result = await db.execute(
            select(Spool)
            .options(selectinload(Spool.k_profiles), selectinload(Spool.assignments))
            .where(func.upper(Spool.tag_uid) == tag_uid_norm, Spool.archived_at.is_(None))
            .limit(1)
        )
        spool = result.scalar_one_or_none()
        if spool:
            return spool

        # Compatibility fallback: some readers report 4-byte UID (8 hex) while
        # stored values may contain longer forms. Prefer suffix match only.
        if len(tag_uid_norm) >= 8:
            suffix8 = tag_uid_norm[-8:]
            short_uid_body = tag_uid_norm[1:] if len(tag_uid_norm) == 8 else ""

            # Build LIKE patterns for candidates search
            like_patterns = [
                func.upper(Spool.tag_uid).like(f"%{tag_uid_norm}"),
                func.upper(Spool.tag_uid).like(f"%{suffix8}"),
            ]
            if short_uid_body:
                like_patterns.append(func.upper(Spool.tag_uid).like(f"%{short_uid_body}%"))

            candidates = await db.execute(
                select(Spool)
                .options(selectinload(Spool.k_profiles), selectinload(Spool.assignments))
                .where(
                    Spool.tag_uid.is_not(None),
                    Spool.archived_at.is_(None),
                    or_(*like_patterns),
                )
                .limit(100)
            )
            for candidate in candidates.scalars().all():
                candidate_uid = _normalize_tag_uid(candidate.tag_uid)
                if not candidate_uid:
                    continue
                if candidate_uid == tag_uid_norm:
                    return candidate
                if len(candidate_uid) > len(tag_uid_norm) and candidate_uid.endswith(tag_uid_norm):
                    return candidate
                if len(tag_uid_norm) > len(candidate_uid) and tag_uid_norm.endswith(candidate_uid):
                    return candidate
                # Backward-compatible matching: allow first-character mismatch
                # when remaining characters match. This handles cases where the same
                # physical tag reports different first bytes across different readers
                # (e.g., one reader reports "A45012F", another reports "B45012F").
                # Routed through _accept_variance: a different-roll signal skips the
                # match; converge callers persist the scanned values so it can't loop.
                if len(tag_uid_norm) == len(candidate_uid) and len(tag_uid_norm) > 1:
                    # Same length: check if all chars except the first match
                    if candidate_uid[1:] == tag_uid_norm[1:]:
                        matched = await _accept_variance(candidate, "first-char")
                        if matched is not None:
                            return matched
                        continue
                # Short UID (8 chars) matching: allow first-character mismatch
                # within the first 8 bytes when remaining 7 chars match.
                if len(tag_uid_norm) == 8 and len(candidate_uid) >= 8:
                    if candidate_uid[:8][1:] == tag_uid_norm[1:]:
                        matched = await _accept_variance(candidate, "short UID")
                        if matched is not None:
                            return matched
                        continue

    return None


async def auto_assign_spool(
    printer_id: int,
    ams_id: int,
    tray_id: int,
    spool: Spool,
    printer_manager,
    db: AsyncSession,
    tray_info_idx: str = "",
) -> SpoolAssignment:
    """Create a SpoolAssignment and auto-configure the AMS slot via MQTT.

    For BL spools (RFID-detected), only K-profile commands are sent.
    ams_set_filament_setting is NOT sent because the firmware already has
    filament configuration from the RFID tag, and sending it would destroy
    the RFID-detected state (eye → pen icon in BambuStudio).
    """
    # Get current tray state for fingerprint
    fingerprint_color = None
    fingerprint_type = None
    tray = None
    state = printer_manager.get_status(printer_id)
    if state and state.raw_data:
        from backend.app.api.routes.inventory import _find_tray_in_ams_data

        ams = state.raw_data.get("ams", [])
        if isinstance(ams, dict):
            ams = ams.get("ams", [])
        tray = _find_tray_in_ams_data(
            ams,
            ams_id,
            tray_id,
        )
        if tray:
            fingerprint_color = tray.get("tray_color", "")
            fingerprint_type = tray.get("tray_type", "")

    # Upsert: remove old assignment for this slot
    existing = await db.execute(
        select(SpoolAssignment).where(
            SpoolAssignment.printer_id == printer_id,
            SpoolAssignment.ams_id == ams_id,
            SpoolAssignment.tray_id == tray_id,
        )
    )
    old = existing.scalar_one_or_none()
    if old:
        await db.delete(old)
        await db.flush()

    assignment = SpoolAssignment(
        spool_id=spool.id,
        printer_id=printer_id,
        ams_id=ams_id,
        tray_id=tray_id,
        fingerprint_color=fingerprint_color,
        fingerprint_type=fingerprint_type,
    )
    db.add(assignment)
    await db.flush()

    # First-in-service stamp (FIFO substrate). Idempotent — only the first
    # assignment sets it; a spool pulled and re-assigned keeps its timestamp.
    stamp_first_loaded(spool)

    # Apply K-profile via MQTT (if available)
    # NOTE: Do NOT send ams_set_filament_setting here. This function is only
    # called for BL spools (RFID-detected). The firmware already has the filament
    # configuration from the RFID tag. Sending ams_set_filament_setting would
    # destroy the RFID-detected state (eye → pen icon in BambuStudio/OrcaSlicer).
    try:
        client = printer_manager.get_client(printer_id)
        if client:
            # Apply K-profile if available. Diameter comes from the SERVING
            # extruder's hotend (mixed-nozzle H2C safe).
            nozzle_diameter = nozzle_for_ams_unit(state, ams_id, tray_id)

            matching_kp = None
            for kp in spool.k_profiles:
                if kp.printer_id == printer_id and kp.nozzle_diameter == nozzle_diameter:
                    matching_kp = kp
                    break

            if matching_kp and matching_kp.cali_idx is not None:
                # The filament_id in extrusion_cali_sel must match the filament preset
                # under which the K-profile was calibrated. Use spool.slicer_filament
                # (the preset assigned in inventory), falling back to tray's RFID value.
                cali_filament_id = spool.slicer_filament or tray_info_idx or ""
                client.extrusion_cali_sel(
                    ams_id=ams_id,
                    tray_id=tray_id,
                    cali_idx=matching_kp.cali_idx,
                    filament_id=cali_filament_id,
                    nozzle_diameter=nozzle_diameter,
                )

                # NOTE: Do NOT send extrusion_cali_set here. extrusion_cali_sel already
                # selected the correct profile by cali_idx. Sending extrusion_cali_set
                # with the same cali_idx would MODIFY the existing profile's metadata
                # (extruder_id, nozzle_id, name), corrupting it.

                logger.info(
                    "Applied K-profile cali_idx=%d for spool %d on printer %d AMS%d-T%d",
                    matching_kp.cali_idx,
                    spool.id,
                    printer_id,
                    ams_id,
                    tray_id,
                )
            elif tray is not None:
                # No stored K-profile: fall back to the slot's current live cali_idx
                # so the printer keeps its existing calibration selection.
                live_cali_idx = tray.get("cali_idx")
                if live_cali_idx is not None and live_cali_idx >= 0:
                    cali_filament_id = spool.slicer_filament or tray_info_idx or ""
                    client.extrusion_cali_sel(
                        ams_id=ams_id,
                        tray_id=tray_id,
                        cali_idx=live_cali_idx,
                        filament_id=cali_filament_id,
                        nozzle_diameter=nozzle_diameter,
                    )
                    logger.info(
                        "No stored K-profile for spool %d on printer %d AMS%d-T%d — preserved live cali_idx=%d",
                        spool.id,
                        printer_id,
                        ams_id,
                        tray_id,
                        live_cali_idx,
                    )

            logger.info(
                "Auto-assigned spool %d to printer %d AMS%d-T%d (RFID match)",
                spool.id,
                printer_id,
                ams_id,
                tray_id,
            )
    except Exception as e:
        logger.warning("K-profile apply failed for spool %d (RFID match): %s", spool.id, e)

    # Reconcile slot_preset_mappings so the AMS slot card stops surfacing the
    # previous spool's preset name. Shared with the manual-assign path
    # (inventory.apply_spool_to_slot_via_mqtt). Outside the try above so a
    # transient MQTT failure doesn't leave the display row stale.
    from backend.app.services.slot_preset_writer import upsert_slot_preset_for_spool

    await upsert_slot_preset_for_spool(
        db=db,
        spool=spool,
        printer_id=printer_id,
        ams_id=ams_id,
        tray_id=tray_id,
        tray_info_idx=tray_info_idx,
        tray_sub_brands=tray.get("tray_sub_brands", "") if tray else "",
        tray_type=tray.get("tray_type", "") if tray else "",
    )

    return assignment


async def reapply_k_profile_if_drifted(
    db: AsyncSession,
    printer_id: int,
    ams_id: int,
    tray_id: int,
    tray: dict,
    spool: Spool | None,
    state,
) -> bool:
    """Re-select a spool's stored K-profile when the slot's live ``cali_idx`` drifted.

    "Reset slot → re-read" and any other path where the firmware loses the user's
    K-profile selection leaves the :class:`SpoolAssignment` intact but the slot
    calibrated to the firmware default. The maintainer's rule is that an identified
    spool matching inventory must be configured with the spool's stored settings, so
    the drift is corrected here — the assignment-ops owner every AMS caller already
    imports — instead of inline in the AMS callback.

    Only fires for a Bambu-tagged tray bound to a spool with a stored profile for
    THIS printer + serving nozzle (exact extruder match preferred, extruder-agnostic
    fallback so a shifted AMS-extruder mapping doesn't hard-skip a valid profile) and
    only when the live ``cali_idx`` differs from the stored one.

    Rate-limited per slot by :data:`_KDRIFT_RETRY_S`. The publish stays
    FIRE-AND-FORGET by design: a refused push (AMS identifying / drying) is not
    inspected and not retried inside the window — it self-heals on a later drift tick
    once the window elapses, which is the whole point of the gate (the un-gated
    version re-fired on every push, storming the AMS during exactly the identify flap
    that caused the drift). Returns True when a re-apply was published.
    """
    if spool is None:
        return False
    if not is_bambu_tag(tray.get("tag_uid", "") or "", tray.get("tray_uuid", "") or "", tray.get("tray_info_idx", "")):
        return False

    nozzle_diameter = nozzle_for_ams_unit(state, ams_id, tray_id)
    slot_extruder: int | None = (
        extruder_for_ams(state.ams_extruder_map, ams_id, tray_id) if (state and state.ams_extruder_map) else None
    )

    # Explicit query rather than walking ``spool.k_profiles``: callers hand us spools
    # loaded without that relationship eager-loaded, and touching it inside an async
    # session raises a greenlet error (the 2026-07-17 bare-tray production crash).
    # Same selection as the relationship walk — this printer + serving nozzle, a
    # usable cali_idx, exact extruder match preferred.
    kp_result = await db.execute(
        select(SpoolKProfile).where(
            SpoolKProfile.spool_id == spool.id,
            SpoolKProfile.printer_id == printer_id,
            SpoolKProfile.nozzle_diameter == nozzle_diameter,
            SpoolKProfile.cali_idx.isnot(None),
        )
    )
    matching_kp = None
    fallback_kp = None
    for kp in kp_result.scalars().all():
        if slot_extruder is not None and kp.extruder is not None and kp.extruder == slot_extruder:
            matching_kp = kp
            break
        if fallback_kp is None:
            fallback_kp = kp
    chosen_kp = matching_kp or fallback_kp
    if chosen_kp is None:
        return False

    live_cali_idx = tray.get("cali_idx")
    if live_cali_idx == chosen_kp.cali_idx:
        return False  # no drift — the slot already holds the stored selection

    from backend.app.services.printer_manager import printer_manager

    client = printer_manager.get_client(printer_id)
    if client is None:
        return False
    if not _kdrift_window.allow((printer_id, ams_id, tray_id)):
        return False  # a re-apply for this slot is still inside its retry window

    cali_filament_id = spool.slicer_filament or tray.get("tray_info_idx", "") or ""
    client.extrusion_cali_sel(
        ams_id=ams_id,
        tray_id=tray_id,
        cali_idx=chosen_kp.cali_idx,
        filament_id=cali_filament_id,
        nozzle_diameter=nozzle_diameter,
    )
    logger.info(
        "Re-applied K-profile cali_idx=%d for spool %d on printer %d AMS%d-T%d (live=%s drift detected)",
        chosen_kp.cali_idx,
        spool.id,
        printer_id,
        ams_id,
        tray_id,
        live_cali_idx,
    )
    return True
