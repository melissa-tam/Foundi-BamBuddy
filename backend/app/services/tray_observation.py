"""Per-push, per-tray AMS observations with EXPLICIT epistemic status (W1).

An observation answers three questions separately and honestly for ONE tray in ONE
push: *is something in the slot* (``present``), *what identity did this push
assert* (``tag_uid``/``tray_uuid``), *what configuration did this push assert*
(``tray_type``/``tray_color``/…). A field the push does not carry is UNKNOWN — it
is never inherited from an earlier push and never inferred from a neighbouring
field.

Why this exists (2026-08-01, 001-H2S T3): the MQTT merge in
``bambu_mqtt._handle_ams_data`` deliberately never overwrites ``tag_uid`` /
``tray_uuid`` with empty values, so the merged view of a slot can be a CHIMERA —
the new roll's ``tag_uid`` beside the departed roll's ``tray_uuid``. Phase-1's
OR-keep then matched on the stale uuid alone, kept the wrong binding, and the new
tag was never minted (zero log lines, 820 g charged to a roll that had left the
fleet). Observations are therefore built from the RAW pre-merge push dict, and
identity is an ATOMIC PAIR: a push that asserts one member and not the other
leaves the other ``None``.

Contract notes:

* Built synchronously in the MQTT callback, PRE-merge, from dicts the merge is
  about to mutate — every value is copied into a frozen object, nothing aliases
  the wire dict.
* PRE-merge, not pre-normalization: ``bambu_mqtt._normalize_cleared_trays`` runs
  first and injects the asserted-empty shape into minimal ``{id, state == 9}``
  partials that pass the exist-bit veto (deliberate), so "asserted" here means
  asserted by the normalized wire.
* The push's ``tray_exist_bits`` is likewise pre-judged: the client alone decides
  whether a mask may be believed and removes the key when it may not, so anything
  that arrives here is evidence this layer may act on in both polarities.
* Field parsing is shared with ``bambu_mqtt`` through ``services.tray_fields``
  (one origin — a second wire parser would drift), and presence IS
  ``tray_fields.tray_presence`` — the same tri-state rule every downstream
  consumer gates on, so an observation and an assignments-API row can never
  disagree about whether a slot is empty.
* Pure: no DB, no awaits, no module state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from backend.app.services.tray_fields import (
    asserted_str_field,
    normalized_tag_uid,
    normalized_tray_uuid,
    parse_int_field,
    parse_tray_exist_bits,
    parse_tray_state,
    slot_exist_bit,
    tray_presence,
    unit_exist_bit_set,
)

logger = logging.getLogger(__name__)

# Keys whose PRESENCE in a tray dict means "this push asserted the slot's
# configuration". ``tray_type == ""`` is an assertion too — it is the verified prod
# cleared-tray shape (state 9 + empty type) — which is why assertion is keyed on
# the key existing, not on the value being truthy.
CONFIG_KEYS = ("tray_type", "tray_color", "tray_info_idx", "tray_sub_brands")


@dataclass(frozen=True)
class TrayObservation:
    """What ONE AMS push said about ONE tray. Immutable, self-describing.

    Tri-state ``present`` (doctrine: presence ≠ identity) — computed by
    :func:`tray_fields.tray_presence`, which owns the rule and documents every
    branch. In short: the push's own exist bit decides when it has one (set = seated,
    clear = empty unless the same push contradicts it with a present state code);
    failing that, a present state code is ``True`` and the verified prod cleared-tray
    shape (non-present state + asserted-empty ``tray_type``) is ``False``. Everything
    else is ``None``, and unknown always beats a guessed "empty" — consumers gate on
    ``present is False``.
    """

    printer_id: int
    ams_id: int
    tray_id: int

    # Raw parsed firmware state code (9 = empty, 10 = seated, 11 = fed, 0/3/25/27 =
    # model dialects). ``None`` = this push carried no parseable state.
    state: int | None
    present: bool | None

    # Identity — ATOMIC PAIR. Each member is None unless THIS push asserted it with
    # a non-empty, non-zero value. A tag asserted without a uuid does NOT inherit
    # the uuid the slot reported last time (the 001-T3 chimera class).
    identity_asserted: bool
    tag_uid: str | None
    tray_uuid: str | None

    # Configuration. ``config_asserted`` is True when the push carried ANY config
    # key; ``tray_type == ""`` is an asserted-empty (cleared) slot, ``None`` is
    # "this push said nothing about the type".
    config_asserted: bool
    tray_type: str | None
    tray_color: str | None
    tray_info_idx: str | None
    tray_sub_brands: str | None

    remain: int | None
    nozzle_temp_min: int | None
    nozzle_temp_max: int | None

    # ``tray_exist_bits`` bit for this slot, when the push carried a TRUSTED mask, the
    # slot is bitmask-addressable (AMS-HT units are not) and ``ams_exist_bits`` lists
    # the unit. ``None`` = no evidence. Evidence in BOTH polarities — a set bit is
    # seating, a clear bit is emptiness — but never identity: what is in the slot is
    # the tag/config members' answer, not this one's.
    exist_bit: bool | None = None

    @property
    def slot(self) -> tuple[int, int, int]:
        """``(printer_id, ams_id, tray_id)`` — the canonical slot key."""
        return (self.printer_id, self.ams_id, self.tray_id)

    @property
    def config_nonempty(self) -> bool:
        """This push asserted a NON-empty filament type (a configured slot)."""
        return bool(self.tray_type)

    @property
    def tray_type_asserted_empty(self) -> bool:
        """This push asserted ``tray_type`` and it was empty (cleared-tray shape)."""
        return self.tray_type == ""

    @property
    def occupancy_signal(self) -> bool:
        """Any positive evidence at all that something is in this slot.

        Used by the state classifier to fail toward UNRESOLVED instead of EMPTY
        when presence is unknown but the slot is clearly not blank.
        """
        return (
            self.present is True
            or self.exist_bit is True
            or self.identity_asserted
            or self.config_nonempty
            or (self.remain is not None and self.remain > 0)
        )


def _asserted(tray: dict, keys: tuple[str, ...]) -> bool:
    return any(k in tray for k in keys)


def _str_field(tray: dict, key: str) -> str | None:
    """Asserted string field — delegates to ``tray_fields.asserted_str_field``.

    Kept as a local name because every field below reads through it; the rule
    itself lives in ``tray_fields`` so the merged-data presence convenience
    (``tray_presence_from_dict``) applies EXACTLY the same assertion semantics.
    """
    return asserted_str_field(tray, key)


def observe_tray(
    printer_id: int,
    ams_id: int,
    tray: dict,
    *,
    exist_bits: int | None = None,
    index: int | None = None,
) -> TrayObservation:
    """Build ONE observation from a RAW (pre-merge) AMS tray dict.

    ``exist_bits`` is the parsed ``tray_exist_bits`` of the SAME push, already
    TRUST-FILTERED and unit-gated by the caller (``None`` when this push offers the
    slot no bit evidence). ``index`` is the tray's position in its unit's list, used
    only as the fallback tray id when the dict omits ``id`` — with a WARNING, because
    a silent default-to-0 (what the pre-W1 call sites did) writes slot 0's identity
    from another slot's data.
    """
    tray_id = parse_int_field(tray.get("id"))
    if tray_id is None:
        tray_id = index if index is not None else 0
        logger.warning(
            "[tray-observe] printer=%s A%s tray dict has no usable 'id' (keys=%s) — falling back to list position %s",
            printer_id,
            ams_id,
            sorted(tray.keys()),
            tray_id,
        )

    state = parse_tray_state(tray.get("state"))

    # Identity, member by member. An absent key and a zero sentinel are the same
    # answer — "this push asserts no tag" — and neither may borrow the OTHER
    # member's assertion (the atomic-pair rule).
    tag_uid = normalized_tag_uid(tray.get("tag_uid"))
    tray_uuid = normalized_tray_uuid(tray.get("tray_uuid"))
    identity_asserted = tag_uid is not None or tray_uuid is not None

    config_asserted = _asserted(tray, CONFIG_KEYS)
    tray_type = _str_field(tray, "tray_type")

    # Tri-state on purpose: an unaddressable slot (AMS-HT, an id outside the mask's
    # four-per-unit stride) must read "no evidence", never a fabricated clear bit.
    bit = slot_exist_bit(exist_bits, ams_id, tray_id)

    present = tray_presence(state, tray_type, bit)

    return TrayObservation(
        printer_id=printer_id,
        ams_id=ams_id,
        tray_id=tray_id,
        state=state,
        present=present,
        identity_asserted=identity_asserted,
        tag_uid=tag_uid,
        tray_uuid=tray_uuid,
        config_asserted=config_asserted,
        tray_type=tray_type,
        tray_color=_str_field(tray, "tray_color"),
        tray_info_idx=_str_field(tray, "tray_info_idx"),
        tray_sub_brands=_str_field(tray, "tray_sub_brands"),
        remain=parse_int_field(tray.get("remain")),
        nozzle_temp_min=parse_int_field(tray.get("nozzle_temp_min")),
        nozzle_temp_max=parse_int_field(tray.get("nozzle_temp_max")),
        exist_bit=bit,
    )


def observe_ams_push(printer_id: int, ams_payload: object) -> list[TrayObservation]:
    """Observations for every tray in ONE raw AMS push.

    Accepts either the full AMS dict (``{"ams": [...], "tray_exist_bits": "f", …}``)
    or the bare unit list (``[{"id": 0, "tray": [...]}]``) — both shapes reach
    ``_handle_ams_data``/``on_ams_change`` today. Malformed entries are skipped, a
    unit without a usable ``id`` falls back to its list position with a WARNING
    (AMS-HT units legitimately carry ids >= 128 and a single tray).

    Trays are returned in payload order; a push that carries only some units yields
    observations only for those units — silence about a slot is NOT an observation
    of it, and the caller must not treat a missing slot as empty.

    ``tray_exist_bits`` is consumed as the firmware's presence answer in both
    polarities, so it is taken on TWO conditions, both owned upstream of here:

    * the mask is TRUSTED — the client strips the key from the payload it hands the
      raw hook when it is not (an all-zero mask that has not repeated), so an untrusted
      push is indistinguishable here from one that carried no mask at all;
    * ``ams_exist_bits`` lists the unit. A unit the firmware is not reporting has an
      all-zero slice of the tray mask for the trivial reason that it is not being
      described, and reading those zeros as four empty trays would invent the one
      answer that authorizes a release. An ABSENT ``ams_exist_bits`` gates nothing
      (unknown fails open).
    """
    units, exist_bits, unit_bits = _units_and_bits(ams_payload)
    observations: list[TrayObservation] = []
    for position, unit in enumerate(units):
        if not isinstance(unit, dict):
            continue
        ams_id = parse_int_field(unit.get("id"))
        if ams_id is None:
            ams_id = position
            logger.warning(
                "[tray-observe] printer=%s AMS unit has no usable 'id' (keys=%s) — falling back to list position %s",
                printer_id,
                sorted(unit.keys()),
                ams_id,
            )
        trays = unit.get("tray")
        if not isinstance(trays, list):
            continue
        unit_bits_for_trays = exist_bits
        if unit_bits is not None and not unit_exist_bit_set(unit_bits, ams_id):
            unit_bits_for_trays = None
        for index, tray in enumerate(trays):
            if not isinstance(tray, dict):
                continue
            observations.append(observe_tray(printer_id, ams_id, tray, exist_bits=unit_bits_for_trays, index=index))
    return observations


def _units_and_bits(ams_payload: object) -> tuple[list, int | None, int | None]:
    """Split a raw push into (unit list, parsed tray_exist_bits, parsed ams_exist_bits)."""
    if isinstance(ams_payload, dict):
        units = ams_payload.get("ams")
        bits = parse_tray_exist_bits(ams_payload.get("tray_exist_bits"))
        unit_bits = parse_tray_exist_bits(ams_payload.get("ams_exist_bits"))
        if isinstance(units, dict):
            # Nested {"ams": {"ams": [...]}} shape, as tolerated by _handle_ams_data.
            nested = units.get("ams")
            units = nested if isinstance(nested, list) else []
        return (units if isinstance(units, list) else []), bits, unit_bits
    if isinstance(ams_payload, list):
        return ams_payload, None, None
    return [], None, None


def observation_tray_dict(obs: TrayObservation) -> dict:
    """The observation as a tray dict, for the existing tray-shaped helpers.

    Only ASSERTED members are included — a key this push did not carry stays absent, so
    the helpers see exactly what the wire said (the atomic-pair rule survives the
    round-trip). The fork's ONE observation→dict projection: the slot pipeline's
    decision/apply lanes and ``ams_presence``'s edge lanes judge the SAME push through
    the same projection, so the tray-shaped helpers they share can never see two
    different renderings of one wire assertion.
    """
    tray: dict = {"id": obs.tray_id}
    if obs.state is not None:
        tray["state"] = obs.state
    for key, value in (
        ("tag_uid", obs.tag_uid),
        ("tray_uuid", obs.tray_uuid),
        ("tray_type", obs.tray_type),
        ("tray_color", obs.tray_color),
        ("tray_info_idx", obs.tray_info_idx),
        ("tray_sub_brands", obs.tray_sub_brands),
        ("remain", obs.remain),
        ("nozzle_temp_min", obs.nozzle_temp_min),
        ("nozzle_temp_max", obs.nozzle_temp_max),
    ):
        if value is not None:
            tray[key] = value
    return tray
