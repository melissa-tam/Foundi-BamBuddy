"""Shared spool ``slicer_filament`` → ``(tray_info_idx, setting_id)`` resolver.

The internal-inventory and Spoolman-inventory routes both need to translate
a spool's stored slicer-preset reference (cloud preset ID / local preset ID /
GF-prefix Bambu filament ID / free-text material name) into the two MQTT
fields ``ams_filament_setting`` consumes: the printer-side ``tray_info_idx``
(filament_id) and the slicer-side ``setting_id``. The two routes were drifting
in lockstep before #1713 — internal mode resolved everything, Spoolman mode
silently dropped slicer_filament on the floor and only the generic-material
fallback fired. This module is the single chokepoint so the two flows can't
diverge again.

Resolver outcomes:

- Returns ``("", "", None)`` when ``slicer_filament`` is empty, unresolvable,
  or sanitised away as a slicer-rejected value (literal material name,
  PFUS / PFCN cloud setting_id) — UNLESS the caller passes
  ``generic_fallback=True``, in which case the identity is re-composed from
  the spool's MATERIAL and re-resolved through this same function (so the
  generic id it composes still passes the ``canonical_default_identity``
  substitution). Without that flag the caller owns the generic-material
  fallback.
- Returns ``(tray_info_idx, setting_id, sub_brand_override)`` otherwise.
  The third element is non-empty when a cloud-detail lookup or a local-
  preset name provides a more specific brand label than the spool's own
  ``"<brand> <material> <subtype>"`` concatenation — the caller should
  prefer it over its computed default.

The resolver is async because the GFS / PFUS / PFCN branches need cloud
authentication and the local-preset branch reads ``LocalPreset`` from the
DB. Pass ``current_user=None`` to skip cloud auth (the on_ams_change
replay path uses this); cloud-prefix presets then fall back to a static
``normalize_slicer_filament`` parse, which is correct when the slot was
already configured by an earlier authenticated assign and the printer's
calibration table preserves the real filament_id.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.user import User
from backend.app.utils.filament_ids import (
    GENERIC_FILAMENT_IDS,
    MATERIAL_TEMPS,
    filament_id_to_setting_id,
    normalize_slicer_filament,
)

logger = logging.getLogger(__name__)

_KNOWN_MATERIALS = set(MATERIAL_TEMPS.keys()) | set(GENERIC_FILAMENT_IDS.keys())


async def _tagless_default(db: AsyncSession) -> dict | None:
    """The parsed ``tagless_default_filament`` dict — read ONCE per resolve.

    Lazy import: ``spool_tagless`` owns the single JSON parse, and keeping the import
    function-local is what stops the resolver and that module forming a cycle. The read
    is best-effort by design — both tiers that consume it have an always-valid fallback
    below them, so a settings failure must degrade rather than raise into a slot write.
    """
    try:
        from backend.app.services.spool_tagless import tagless_default_filament

        return await tagless_default_filament(db)
    except Exception as e:  # noqa: BLE001 — the un-canonicalised identity is a valid answer
        logger.debug("Tagless-default lookup failed: %s", e)
        return None


def _canonical_identity(
    default: dict | None,
    *,
    slicer_filament: str | None,
    material: str | None,
    rgba: str | None,
    nozzle_temp_min: int | None,
    nozzle_temp_max: int | None,
) -> dict | None:
    """``spool_tagless.canonical_default_identity``, guarded and lazily imported.

    Both consumers below ask the SAME predicate the tagless mint and the slot-config
    harmonise arm ask, so no write site can disagree with another about what the fleet's
    default filament looks like.
    """
    try:
        from backend.app.services.spool_tagless import canonical_default_identity

        return canonical_default_identity(
            default,
            slicer_filament=slicer_filament,
            material=material,
            rgba=rgba,
            nozzle_temp_min=nozzle_temp_min,
            nozzle_temp_max=nozzle_temp_max,
        )
    except Exception as e:  # noqa: BLE001 — the un-canonicalised identity is a valid answer
        logger.debug("Canonical-identity lookup failed for %r: %s", slicer_filament, e)
        return None


def _resolve_nozzle_temps(
    material: str | None,
    rgba: str | None,
    row_min: int | None,
    row_max: int | None,
    *,
    slicer_filament: str | None,
    default: dict | None,
) -> tuple[int, int]:
    """The slot's nozzle temperature range, resolved in ONE place (W4).

    Tier order, each temp independently: the spool ROW's temp when set → the configured
    tagless default's pair when the row's identity is CANONICALISABLE onto that default
    (:func:`_canonical_identity`) → ``MATERIAL_TEMPS`` (final always-valid fallback).

    The middle tier used to be a fingerprint match alone, blind to the stored preset.
    It now rides the same eligibility every other canonicalisation site does, which
    tightens exactly one case and deliberately: a row naming a DIFFERENT specific preset
    (``GFG00`` beside a ``GFG02`` default) no longer inherits the default's temperatures.
    That row is an operator statement, it is not a backup-group peer on the preset
    dimension, and lending it the default's temps only made its identity look canonical.
    An UNSTATED preset (``""``/None) stays eligible — it asserts nothing to contradict —
    so the legacy-row shape this tier was written for is untouched.
    """
    temp_min, temp_max = row_min, row_max
    if temp_min is None or temp_max is None:
        canon = _canonical_identity(
            default,
            slicer_filament=slicer_filament,
            material=material,
            rgba=rgba,
            nozzle_temp_min=row_min,
            nozzle_temp_max=row_max,
        )
        if canon is not None:
            if temp_min is None and canon["nozzle_temp_min"] is not None:
                temp_min = int(canon["nozzle_temp_min"])
            if temp_max is None and canon["nozzle_temp_max"] is not None:
                temp_max = int(canon["nozzle_temp_max"])
    if temp_min is None or temp_max is None:
        m_min, m_max = MATERIAL_TEMPS.get((material or "").upper(), (200, 240))
        if temp_min is None:
            temp_min = m_min
        if temp_max is None:
            temp_max = m_max
    return (temp_min, temp_max)


async def resolve_slicer_filament(
    *,
    db: AsyncSession,
    current_user: User | None,
    slicer_filament: str | None,
    slicer_filament_name: str | None,
    material: str | None,
    rgba: str | None = None,
    nozzle_temp_min: int | None = None,
    nozzle_temp_max: int | None = None,
    generic_fallback: bool = False,
) -> tuple[str, str, str | None, int, int]:
    """Resolve a spool's stored identity to the COMPLETE printer-side wire tuple.

    ``slicer_filament``: the spool's stored reference (e.g. ``"GFA01"``,
    ``"PFUS990b6e19965353"``, ``"38"`` for a numeric LocalPreset id, or
    free-text). May be empty or None — the id/setting pair is empty in that case
    but the temps are still resolved (the caller consumes them unconditionally).

    ``slicer_filament_name``: optional builtin-name realignment hint. When
    set and the resolved tray_info_idx maps to a different builtin name,
    the resolver swaps to the builtin whose name matches (e.g. user picked
    "Bambu PLA Matte" but the cloud lookup landed on "Bambu PLA Basic").

    ``material``: spool material string for the local-preset fallback
    branch when the LocalPreset's setting JSON doesn't carry a filament_id, AND
    for the nozzle-temp resolution (fingerprint / MATERIAL_TEMPS).

    ``rgba`` / ``nozzle_temp_min`` / ``nozzle_temp_max``: the spool row's colour
    and its stored temps — feed the temp resolution (:func:`_resolve_nozzle_temps`)
    so the WHOLE wire identity is composed here and the two write-site consumers
    can't diverge (W4). Optional so pre-W4 callers still type-check.

    ``generic_fallback``: opt-in rescue for a row whose stored identity names no
    filament but whose MATERIAL does. When the FINAL ``tray_info_idx`` would be
    empty — an empty/NULL ``slicer_filament``, or a value the sanitiser cleared —
    the generic id for the material is composed and re-resolved through this same
    function once. Default False keeps every caller that owns its own fallback
    byte-identical.

    A resolved id that is CANONICALISABLE onto the configured tagless default —
    generic (``GFG99`` …) or the default's own — is finally re-composed as that
    default's SPECIFIC identity (id, setting_id and temps) when the spool
    fingerprint-matches it (``spool_tagless.canonical_default_identity``), so no write
    site can re-publish an identity that splits the firmware's auto-refill backup
    group. The COLOUR dimension of that same predicate is the ROW's, not this
    function's — colour never was part of the resolver's return.

    Returns ``(tray_info_idx, setting_id, sub_brand_override, nozzle_temp_min,
    nozzle_temp_max)``. id/setting are empty when nothing resolved;
    ``sub_brand_override`` is non-None when a more specific brand label is
    available (cloud detail name or local preset name). The two temps are ALWAYS
    concrete ints (MATERIAL_TEMPS is the final fallback).
    """
    # ONE settings read per resolve, shared by the nozzle-temp tier and the canonical-id
    # substitution below — the two used to fetch it independently.
    default = await _tagless_default(db)
    temp_min, temp_max = _resolve_nozzle_temps(
        material,
        rgba,
        nozzle_temp_min,
        nozzle_temp_max,
        slicer_filament=slicer_filament,
        default=default,
    )

    tray_info_idx = ""
    setting_id = ""
    sub_brand_override: str | None = None

    # An empty stored identity resolves nothing, but must still reach the single
    # generic-material fallback below — the sanitised-away case shares that exit and
    # the two must not answer a legacy row differently.
    sf = (slicer_filament or "").strip()
    if sf:
        base_sf = sf.split("_")[0] if "_" in sf else sf

        # Cloud-side preset IDs in three known shapes:
        #   GFS…   — Bambu official cloud preset
        #   PFUS…  — cloud user-created preset
        #   PFCN…  — cloud shared / partner preset (e.g. Polymaker's "(Custom)"
        #            Bambu Lab H2D variant, #1648)
        # All three need a cloud-detail lookup to extract the underlying
        # filament_id; without it the raw cloud id ends up in tray_info_idx
        # and the printer's calibration table can't resolve it.
        if base_sf.startswith("GFS") or base_sf.startswith("PFUS") or base_sf.startswith("PFCN"):
            setting_id = base_sf
            try:
                from backend.app.api.routes.cloud import build_authenticated_cloud

                cloud = await build_authenticated_cloud(db, current_user)
                if cloud is not None and cloud.is_authenticated:
                    try:
                        detail = await cloud.get_setting_detail(base_sf)
                        if detail.get("filament_id"):
                            tray_info_idx = detail["filament_id"]
                            cloud_name = detail.get("name", "")
                            if cloud_name:
                                sub_brand_override = cloud_name.replace(r"@.*$", "").split("@")[0].strip()
                        elif detail.get("base_id"):
                            bid = detail["base_id"].split("_")[0]
                            if bid.startswith("GFS") and len(bid) >= 5:
                                tray_info_idx = f"GF{bid[3:]}"
                            else:
                                tray_info_idx = bid
                    finally:
                        await cloud.close()
                elif cloud is not None:
                    await cloud.close()
            except Exception as e:
                logger.warning("Slicer-filament resolve: cloud lookup failed for %r: %s", sf, e)

            if not tray_info_idx:
                tray_info_idx, setting_id = normalize_slicer_filament(sf)
        elif base_sf.startswith("GF"):
            tray_info_idx, setting_id = normalize_slicer_filament(sf)
        else:
            try:
                local_id = int(sf)
                from backend.app.models.local_preset import LocalPreset as LP

                lp_result = await db.execute(select(LP).where(LP.id == local_id, LP.preset_type == "filament"))
                lp = lp_result.scalar_one_or_none()
                if lp:
                    # Local preset's setting JSON carries the printer-recognized
                    # filament_id (e.g. "P4d64437") — use that directly so the
                    # slicer can resolve the specific preset. Falls through to
                    # generic material id only when the JSON doesn't carry one.
                    lp_filament_id = ""
                    if lp.setting:
                        try:
                            setting_data = json.loads(lp.setting)
                            raw_fid = setting_data.get("filament_id")
                            if isinstance(raw_fid, str) and raw_fid:
                                lp_filament_id = raw_fid
                        except (json.JSONDecodeError, AttributeError):
                            pass
                    if lp_filament_id:
                        tray_info_idx = lp_filament_id
                        setting_id = filament_id_to_setting_id(lp_filament_id)
                    else:
                        mat = (material or lp.filament_type or "").upper().strip()
                        tray_info_idx = (
                            GENERIC_FILAMENT_IDS.get(mat)
                            or GENERIC_FILAMENT_IDS.get(mat.split("-")[0].split(" ")[0])
                            or ""
                        )
                    if lp.name:
                        sub_brand_override = lp.name.split("@")[0].strip()
            except (ValueError, TypeError):
                tray_info_idx, setting_id = normalize_slicer_filament(sf)

    # Realign tray_info_idx to a builtin whose name matches slicer_filament_name
    # when the current resolution lands on a builtin with a different name
    # (e.g. cloud detail returned PLA Basic but the spool was labelled PLA Matte).
    if tray_info_idx and slicer_filament_name:
        from backend.app.api.routes.cloud import _BUILTIN_FILAMENT_NAMES

        expected_name = _BUILTIN_FILAMENT_NAMES.get(tray_info_idx, "")
        if expected_name and expected_name != slicer_filament_name:
            for fid, fname in _BUILTIN_FILAMENT_NAMES.items():
                if fname == slicer_filament_name:
                    tray_info_idx = fid
                    setting_id = filament_id_to_setting_id(fid)
                    break

    # Defend against tray_info_idx values the slicer cannot resolve. Three
    # shapes leak through and must be discarded so the caller's generic-
    # material fallback can rescue the slot:
    #   1. Literal material names ("PLA", "PETG-CF") that pass through
    #      normalize_slicer_filament unchanged when the spool's slicer_filament
    #      is free-text rather than a real preset ID.
    #   2. PFUS-prefix cloud setting_ids — valid as setting_id but rejected
    #      by the slicer as tray_info_idx (the printer's calibration table
    #      indexes by filament_id, and a PFUS isn't one). This normally gets
    #      realigned to a P-prefix local id via the caller's printer_kp
    #      lookup, but on the replay path in main.py.on_ams_change
    #      current_user=None skips cloud auth and leaves the raw PFUS in
    #      tray_info_idx — overwriting the correctly-configured slot from
    #      the original assign.
    #   3. PFCN-prefix cloud shared / partner presets (e.g. Polymaker's
    #      "(Custom)" H2D variants, #1648) — same shape problem as PFUS.
    # Valid tray_info_idx values: "GF" + letter + digits (Bambu official) or
    # "P" followed by hex (user/local presets, NOT "PFUS" or "PFCN").
    if tray_info_idx and (
        tray_info_idx.upper() in _KNOWN_MATERIALS
        or tray_info_idx.startswith("PFUS")
        or tray_info_idx.startswith("PFCN")
    ):
        tray_info_idx = ""
        # Preserve setting_id when it's still a valid slicer reference
        # (PFUS / PFCN cloud user/shared preset, or GFS Bambu official
        # preset). The slicer accepts these as setting_id even though
        # they're rejected as tray_info_idx; without preservation the
        # slicer falls back to whatever generic filament the caller's
        # tray_info_idx fallback produces and shows "Generic <Material>"
        # instead of the user's actual custom preset (#1815). Material-name
        # leaks (e.g. setting_id="PETG") are still cleared — those are
        # never valid slicer references.
        if not (
            setting_id
            and (setting_id.startswith("PFUS") or setting_id.startswith("PFCN") or setting_id.startswith("GFS"))
        ):
            setting_id = ""

    # Canonical-identity substitution (E3, 2026-07-20; colour dimension 2026-08-21): a
    # resolved id that is CANONICALISABLE onto the configured tagless default — generic
    # (GFG99 …) or the default's own — is re-composed as the default's SPECIFIC identity:
    # id, setting_id AND nozzle temps. The firmware's auto-refill backup group pairs
    # slots only on an exact preset + colour match, so a stale spool row
    # carrying a leftover generic id re-published a GFG99 that split the group (011-H2S,
    # live). Every write site routes through this resolver, so the substitution happens
    # once here instead of at each caller. The COLOUR half of the same predicate belongs
    # to the ROW rather than to this function: colour is not part of the resolver's
    # contract (the wire takes ``spool.rgba`` directly at the write site), so
    # ``spool_tagless.maybe_harmonize_backup_identity`` is what canonicalises it.
    #
    # Gated on a non-empty resolved id: composing one from nothing is ``generic_fallback``'s
    # job below, and the write site's own keep-the-live-specific-preset tier must outrank
    # it. Best-effort — a lookup failure degrades to the resolved identity rather than
    # raising into a slot write.
    canon = (
        _canonical_identity(
            default,
            slicer_filament=tray_info_idx,
            material=material,
            rgba=rgba,
            nozzle_temp_min=temp_min,
            nozzle_temp_max=temp_max,
        )
        if tray_info_idx
        else None
    )
    if canon is not None and canon["slicer_filament"]:
        tray_info_idx = canon["slicer_filament"]
        setting_id = filament_id_to_setting_id(tray_info_idx)
        if canon["nozzle_temp_min"] is not None:
            temp_min = int(canon["nozzle_temp_min"])
        if canon["nozzle_temp_max"] is not None:
            temp_max = int(canon["nozzle_temp_max"])

    # Generic-material fallback (2026-07-26), opt-in: the SINGLE exit for every shape
    # that resolved no id — an empty/NULL slicer_filament and a sanitised-away value
    # both arrive here. The composed generic id is deliberately NOT returned directly:
    # re-entering routes it through the canonical-identity substitution above, the only
    # thing that turns a fingerprint-matching row into the fleet's SPECIFIC id instead
    # of a GFG99 that splits the firmware's auto-refill backup group. Re-entry is one
    # level deep (generic_fallback=False) and only from a non-empty composed id, so it
    # cannot recurse. No material, or one with no generic id, keeps the empty result —
    # a caller with a preserved setting_id (#1815) therefore never loses it.
    if not tray_info_idx and generic_fallback:
        mat = (material or "").upper().strip()
        generic = GENERIC_FILAMENT_IDS.get(mat) or GENERIC_FILAMENT_IDS.get(mat.split("-")[0].split(" ")[0]) or ""
        if generic:
            return await resolve_slicer_filament(
                db=db,
                current_user=current_user,
                slicer_filament=generic,
                slicer_filament_name=None,
                material=material,
                rgba=rgba,
                nozzle_temp_min=nozzle_temp_min,
                nozzle_temp_max=nozzle_temp_max,
                generic_fallback=False,
            )

    return (tray_info_idx, setting_id, sub_brand_override, temp_min, temp_max)
