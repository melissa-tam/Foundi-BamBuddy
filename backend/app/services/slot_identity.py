"""The ONE builder of an AMS slot's wire identity, and the internal-inventory
write that publishes it.

Four lanes configure a slot's filament — the internal manual assign, the farm's
own "Configure Slot" endpoint, and both Spoolman lanes — and every one of them
publishes the same seven fields through
:meth:`BambuMQTTClient.ams_set_filament_setting`. Three of them used to COMPOSE
those fields themselves, each with a private generic-id table, its own nozzle-temp
fallback and its own colour default. The firmware pairs two slots into one
auto-refill backup group only on an EXACT preset + colour match, so a lane that
composes an identity a hair differently from its neighbour splits that group and
the printer runs a slot dry beside a full one (011-H2S ``GFG99``, 010-H2S
``161616FF``). :func:`resolve_slot_identity` is that composition, once.

Layering: this is a SERVICE. The reference implementation lived in
``api.routes.inventory`` — a route module, which is both a Controller→Service
violation and the plausible reason the other three lanes grew private copies
rather than importing from a route.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.spool import Spool
from backend.app.models.spool_k_profile import SpoolKProfile
from backend.app.models.user import User
from backend.app.services.slicer_filament_resolver import resolve_slicer_filament
from backend.app.services.slot_preset_writer import upsert_slot_preset_for_spool
from backend.app.utils.filament_ids import (
    GENERIC_FILAMENT_IDS,
    MATERIAL_TEMPS,
    filament_id_to_setting_id,
    normalize_slicer_filament,
)
from backend.app.utils.printer_models import extruder_for_ams, nozzle_for_ams_unit

logger = logging.getLogger(__name__)

#: The colour published when a row carries none. Colour is one of the two
#: dimensions the firmware's auto-refill backup grouping matches on (preset is the
#: other), so an unstated colour must resolve to the SAME bytes on every lane —
#: the Spoolman lanes defaulted to ``808080FF`` against this one's ``FFFFFFFF``,
#: which is a split group for any pair of slots that lands on opposite sides.
DEFAULT_TRAY_COLOR = "FFFFFFFF"

_HEX_DIGITS = frozenset("0123456789ABCDEF")
_GENERIC_ID_VALUES = frozenset(GENERIC_FILAMENT_IDS.values())
_KNOWN_MATERIALS = frozenset(MATERIAL_TEMPS.keys()) | frozenset(GENERIC_FILAMENT_IDS.keys())


@dataclass(frozen=True)
class SlotIdentity:
    """The complete set of fields ``ams_filament_setting`` publishes for one slot.

    Immutable because it is a decided identity: a caller that wants different bytes
    must ask :func:`resolve_slot_identity` a different question, not edit the answer
    on its way to the wire — that editing is exactly what let four lanes drift.
    """

    tray_info_idx: str
    tray_type: str
    tray_sub_brands: str
    tray_color: str
    nozzle_temp_min: int
    nozzle_temp_max: int
    setting_id: str


def compose_sub_brands(brand: str | None, material: str | None, subtype: str | None) -> str:
    """The ``"<brand> <material> <subtype>"`` label, composed identically everywhere.

    A display/label field rather than a grouping dimension, but all three inventory
    lanes built it with the same three-branch expression and one of them is enough.
    """
    mat = material or ""
    sub = (subtype or "").strip()
    if brand:
        return f"{brand} {mat} {sub}".strip()
    if sub:
        return f"{mat} {sub}".strip()
    return mat


def normalize_tray_color(rgba: str | None) -> str:
    """A row's colour as the wire takes it: 8 uppercase hex digits.

    Case and alpha are NOT cosmetic here. The firmware groups backup slots on an
    exact colour match, so ``ff0000ff`` beside ``FF0000FF`` — or a stored 6-digit
    value beside an 8-digit one — is two different colours to the AMS and therefore
    two groups. Normalising at the one composition point is what makes the two
    slots peers.

    Anything that is not 6 or 8 HEX digits falls back to :data:`DEFAULT_TRAY_COLOR`:
    length alone is not the test, because a non-hex string of the right length would
    otherwise reach the wire as a colour.
    """
    value = (rgba or "").strip().upper().removeprefix("#")
    if len(value) == 6:
        value = f"{value}FF"
    if len(value) != 8 or not _HEX_DIGITS.issuperset(value):
        return DEFAULT_TRAY_COLOR
    return value


async def resolve_slot_identity(
    *,
    db: AsyncSession,
    current_user: User | None,
    material: str | None,
    slicer_filament: str | None,
    slicer_filament_name: str | None,
    rgba: str | None,
    nozzle_temp_min: int | None,
    nozzle_temp_max: int | None,
    sub_brands: str,
    stated_tray_info_idx: str = "",
    stated_setting_id: str = "",
    current_tray_info_idx: str = "",
    current_tray_type: str = "",
) -> SlotIdentity:
    """Compose the identity to publish for one slot — the ONE chokepoint.

    The filament reference resolves down a four-tier ladder, and the tiers are the
    same whichever lane asks:

    1. **An identity the operator STATED** (``stated_tray_info_idx``) — returned
       verbatim, never passed through the resolver's sanitiser. An explicit preset
       choice is a statement about the roll in the slot, not a guess to correct; a
       specific preset standing beside the fleet default is an operator statement
       for exactly the same reason ``spool_tagless.maybe_harmonize_backup_identity``
       leaves one alone. Empty (the default) means nothing was stated.
    2. **The row's own stored reference**, through
       :func:`~backend.app.services.slicer_filament_resolver.resolve_slicer_filament`
       — which owns the GFS/PFUS/PFCN cloud lookup, the GF normalise, the numeric
       LocalPreset branch, the builtin-name realignment, the sanitisation of values
       the slicer rejects, AND the canonical-identity substitution that keeps a
       fingerprint-matching row on the fleet default's SPECIFIC id.
    3. **The live tray's own non-generic id** for the same material, when the row
       resolved nothing — the slot is already carrying a specific preset and
       overwriting it with a generic would split its backup group.
    4. **The resolver's own generic fallback** (``generic_fallback=True``), which
       re-enters the resolver so a composed ``GFG99`` still passes the
       canonical-identity substitution. Composing a generic id at a CALL SITE is
       what published the bare ``GFG99`` behind the 011-H2S no-auto-refill
       incident, and it is why no caller may own this tier.

    ``nozzle_temp_min`` / ``nozzle_temp_max`` are the ROW's stored temps or None;
    the resolver returns concrete ints either way (its ``MATERIAL_TEMPS`` tier is
    the floor). ``sub_brands`` is the caller's label, superseded by the resolver's
    ``sub_brand_override`` when a cloud detail or local preset names the filament
    more precisely.
    """
    tray_type = material or ""

    tray_info_idx, setting_id, sub_brand_override, temp_min, temp_max = await resolve_slicer_filament(
        db=db,
        current_user=current_user,
        slicer_filament=slicer_filament,
        slicer_filament_name=slicer_filament_name,
        material=material,
        rgba=rgba,
        nozzle_temp_min=nozzle_temp_min,
        nozzle_temp_max=nozzle_temp_max,
    )
    if sub_brand_override:
        sub_brands = sub_brand_override

    if stated_tray_info_idx:
        tray_info_idx = stated_tray_info_idx
        setting_id = stated_setting_id
    elif not tray_info_idx:
        if (
            current_tray_info_idx
            and current_tray_info_idx not in _GENERIC_ID_VALUES
            and not current_tray_info_idx.startswith("PFUS")
            and not current_tray_info_idx.startswith("PFCN")
            and current_tray_info_idx.upper() not in _KNOWN_MATERIALS
            and current_tray_type
            and current_tray_type.upper() == tray_type.upper()
        ):
            tray_info_idx = current_tray_info_idx
        elif tray_type:
            tray_info_idx, setting_id, _generic_sub_brand, temp_min, temp_max = await resolve_slicer_filament(
                db=db,
                current_user=current_user,
                slicer_filament=slicer_filament,
                slicer_filament_name=slicer_filament_name,
                material=material,
                rgba=rgba,
                nozzle_temp_min=nozzle_temp_min,
                nozzle_temp_max=nozzle_temp_max,
                generic_fallback=True,
            )

    # A slot configured with a filament id but no setting id is half-configured: the
    # slicer renders empty fields in the slot detail modal. The id is always the
    # authority the setting derives from.
    if tray_info_idx and not setting_id:
        setting_id = filament_id_to_setting_id(tray_info_idx)

    return SlotIdentity(
        tray_info_idx=tray_info_idx,
        tray_type=tray_type,
        tray_sub_brands=sub_brands,
        tray_color=normalize_tray_color(rgba),
        nozzle_temp_min=temp_min,
        nozzle_temp_max=temp_max,
        setting_id=setting_id,
    )


async def apply_spool_to_slot_via_mqtt(
    *,
    db: AsyncSession,
    current_user: User | None,
    spool: Spool,
    printer_id: int,
    ams_id: int,
    tray_id: int,
    current_tray_info_idx: str = "",
    current_tray_type: str = "",
) -> bool:
    """Publish ams_filament_setting + extrusion_cali_sel for a spool on a slot.

    Shared by `assign_spool` (initial assign for a loaded slot) and
    `on_ams_change` (re-fire when a SpoolBuddy-pre-assigned slot transitions
    empty → loaded). Returns True when MQTT commands were published, False if
    no client was available or setup failed mid-way.

    `current_tray_info_idx` / `current_tray_type` describe the live tray state
    used as fallback hints when the spool's slicer_filament can't be resolved.
    Caller should not pass these for the empty-slot re-fire path (they'll be
    the freshly-loaded values, which is the intended fallback).
    """
    from backend.app.services.printer_manager import printer_manager

    client = printer_manager.get_client(printer_id)
    if client is None:
        return False

    state = printer_manager.get_status(printer_id)

    identity = await resolve_slot_identity(
        db=db,
        current_user=current_user,
        material=spool.material,
        slicer_filament=spool.slicer_filament,
        slicer_filament_name=spool.slicer_filament_name,
        rgba=spool.rgba,
        nozzle_temp_min=spool.nozzle_temp_min,
        nozzle_temp_max=spool.nozzle_temp_max,
        sub_brands=compose_sub_brands(spool.brand, spool.material, spool.subtype),
        current_tray_info_idx=current_tray_info_idx,
        current_tray_type=current_tray_type,
    )

    nozzle_diameter = nozzle_for_ams_unit(state, ams_id, tray_id)

    slot_extruder = (
        extruder_for_ams(state.ams_extruder_map, ams_id, tray_id) if (state and state.ams_extruder_map) else None
    )

    # Fetch this spool's K-profiles with an explicit query instead of walking the
    # `spool.k_profiles` relationship. Callers pass spools loaded WITHOUT eager-
    # loading that relationship (the tagless bare-tray re-push hands us
    # `assignment.spool` from a selectinload of the assignment alone), and touching
    # a lazy relationship inside an async session raises a greenlet/lazy-load error
    # — deterministically, not intermittently (production 2026-07-17: bare-tray
    # auto-config crashed on every pre-existing spool). The printer + nozzle filter
    # that the old loop applied via `continue` is pushed into SQL.
    kp_result = await db.execute(
        select(SpoolKProfile).where(
            SpoolKProfile.spool_id == spool.id,
            SpoolKProfile.printer_id == printer_id,
            SpoolKProfile.nozzle_diameter == nozzle_diameter,
        )
    )
    # Prefer exact extruder match, fall back to an extruder-agnostic kp for the
    # same nozzle. Hard-skipping on mismatch silently drops valid stored profiles
    # when the AMS-extruder mapping has shifted.
    exact_kp = None
    fallback_kp = None
    for kp in kp_result.scalars().all():
        if slot_extruder is not None and kp.extruder is not None and kp.extruder == slot_extruder:
            exact_kp = kp
            break
        if fallback_kp is None:
            fallback_kp = kp
    matching_kp = exact_kp or fallback_kp

    # Resolve the printer-side calibration entry by looking up the cali_idx
    # in state.kprofiles. The printer keys its calibration table by
    # (filament_id, cali_idx) — for the cali_idx to stick, the slot's
    # filament_id must match the kp's. PFUS-prefix cloud user presets are
    # rejected by the slicer in tray_info_idx; the printer-reported
    # filament_id is typically a P-prefix local preset which is valid.
    printer_kp = None
    if matching_kp and matching_kp.cali_idx is not None and state and getattr(state, "kprofiles", None):
        for pkp in state.kprofiles:
            if pkp.slot_id == matching_kp.cali_idx and pkp.nozzle_diameter == nozzle_diameter:
                printer_kp = pkp
                break

    effective_tray_info_idx = identity.tray_info_idx
    effective_setting_id = identity.setting_id
    if printer_kp and printer_kp.filament_id:
        effective_tray_info_idx = printer_kp.filament_id
    target_setting_id = (printer_kp.setting_id if printer_kp else None) or (
        matching_kp.setting_id if matching_kp else None
    )
    if target_setting_id:
        effective_setting_id = target_setting_id
    if effective_tray_info_idx != identity.tray_info_idx or effective_setting_id != identity.setting_id:
        logger.info(
            "Spool assign: realigning tray_info_idx %r → %r, setting_id %r → %r (source=%s)",
            identity.tray_info_idx,
            effective_tray_info_idx,
            identity.setting_id,
            effective_setting_id,
            "printer" if printer_kp else "stored",
        )

    setting_ok = client.ams_set_filament_setting(
        ams_id=ams_id,
        tray_id=tray_id,
        tray_info_idx=effective_tray_info_idx,
        tray_type=identity.tray_type,
        tray_sub_brands=identity.tray_sub_brands,
        tray_color=identity.tray_color,
        nozzle_temp_min=identity.nozzle_temp_min,
        nozzle_temp_max=identity.nozzle_temp_max,
        setting_id=effective_setting_id,
    )
    if not setting_ok:
        # The filament-setting write was refused (AMS identifying/drying, or offline).
        # Do NOT proceed to extrusion_cali_sel or persist a slot-preset row for a
        # write that never reached the printer — the preset row must not record a
        # config that did not land. Two lanes re-apply it: the AMS callback while the
        # AMS state keeps churning (change-gated on bambu_mqtt's state hash, so a
        # SETTLED AMS never re-fires it — the 2026-07-24 15-minute blank-slot
        # incident), and spool_tagless.reconcile_slot_config on the scheduler tick,
        # which is the durable backstop that does not depend on state churn.
        logger.warning(
            "Spool assign: ams_filament_setting refused for spool %d AMS%d-T%d on printer %d "
            "(AMS busy identifying/drying, or offline) — skipping calibration + preset persist",
            spool.id,
            ams_id,
            tray_id,
            printer_id,
        )
        return False

    if matching_kp and matching_kp.cali_idx is not None:
        # filament_id for cali_sel must match the preset under which the kp
        # was registered. Priority: live printer kp > stored kp.setting_id >
        # spool.slicer_filament > realigned tray_info_idx.
        if printer_kp and printer_kp.filament_id:
            cali_filament_id = printer_kp.filament_id
        elif matching_kp.setting_id:
            cali_filament_id = normalize_slicer_filament(matching_kp.setting_id)[0] or matching_kp.setting_id
        else:
            cali_filament_id = spool.slicer_filament or effective_tray_info_idx
        client.extrusion_cali_sel(
            ams_id=ams_id,
            tray_id=tray_id,
            cali_idx=matching_kp.cali_idx,
            filament_id=cali_filament_id,
            nozzle_diameter=nozzle_diameter,
        )
    else:
        # No stored K-profile for this spool — always reset the slot to Default
        # K (cali_idx=-1). The live cali_idx on the slot belongs to whatever
        # filament was there before, so preserving it would apply the wrong
        # filament's calibration to the new spool. Default K is the firmware's
        # documented "no specific profile" value (see BambuClient.extrusion_cali_sel
        # docstring).
        cali_filament_id = spool.slicer_filament or effective_tray_info_idx
        client.extrusion_cali_sel(
            ams_id=ams_id,
            tray_id=tray_id,
            cali_idx=-1,
            filament_id=cali_filament_id,
            nozzle_diameter=nozzle_diameter,
        )
        logger.info(
            "No stored K-profile for spool %d — reset slot to Default K (cali_idx=-1)",
            spool.id,
        )

    # Persist slot preset mapping for UI display (preset_name on hover card).
    # Shared with the RFID auto-assign path — both must keep this row in sync
    # with the currently-assigned spool, otherwise the slot card surfaces the
    # previous spool's preset name (the PrintersPage display chain consults
    # slot_preset_mappings.preset_name first).
    await upsert_slot_preset_for_spool(
        db=db,
        spool=spool,
        printer_id=printer_id,
        ams_id=ams_id,
        tray_id=tray_id,
        tray_info_idx=identity.tray_info_idx,
        tray_sub_brands=identity.tray_sub_brands,
        tray_type=identity.tray_type,
        setting_id=identity.setting_id,
    )

    logger.info(
        "Auto-configured AMS slot ams=%d tray=%d for spool %d on printer %d",
        ams_id,
        tray_id,
        spool.id,
        printer_id,
    )
    return True
