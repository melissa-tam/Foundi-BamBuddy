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
* Field parsing is shared with ``bambu_mqtt`` through ``services.tray_fields``
  (one origin — a second wire parser would drift), and presence keys off
  ``bambu_mqtt.TRAY_PRESENT_STATES`` (one origin for that magic value).
* Pure: no DB, no awaits, no module state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from backend.app.services.bambu_mqtt import TRAY_PRESENT_STATES
from backend.app.services.tray_fields import (
    normalized_tag_uid,
    normalized_tray_uuid,
    parse_int_field,
    parse_tray_exist_bits,
    parse_tray_state,
    slot_exist_bit_set,
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

    Tri-state ``present`` (doctrine: presence ≠ identity):

    * ``True``  — ``state`` is in ``TRAY_PRESENT_STATES`` (10 = seated, 11 = fed).
    * ``False`` — ``state`` parsed, is not a present code, AND ``tray_type`` was
      asserted EMPTY. That pair is the verified prod cleared-tray shape (state 9 +
      ``tray_type: ""``); it is the only shape that may drive a release.
    * ``None``  — anything else: no parseable state (partial push), a dialect that
      never reports presence (the A1-family/P1S always-``state=3`` firmwares), a
      state-9 slot that still asserts a filament type (the 004-H2S
      state-9-while-feeding dialect), or a state/bitmask CONTRADICTION. Unknown
      always beats a guessed "empty" — consumers gate on ``present is False``.
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

    # ``tray_exist_bits`` bit for this slot when the push carried the bitmask and
    # the slot is bitmask-addressable (AMS-HT units are not). ``None`` = no
    # evidence. Positive evidence only — it can veto a "present is False", never
    # manufacture identity.
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
    """Asserted string field: ``None`` when absent, stripped value when present."""
    if key not in tray:
        return None
    value = tray.get(key)
    if value is None:
        # An explicit null asserts "nothing here" for a string field — the merge
        # treats null and "" the same way in its always-update list.
        return ""
    return str(value).strip()


def observe_tray(
    printer_id: int,
    ams_id: int,
    tray: dict,
    *,
    exist_bits: int | None = None,
    index: int | None = None,
) -> TrayObservation:
    """Build ONE observation from a RAW (pre-merge) AMS tray dict.

    ``exist_bits`` is the parsed ``tray_exist_bits`` of the SAME push (``None``
    when it carried none). ``index`` is the tray's position in its unit's list,
    used only as the fallback tray id when the dict omits ``id`` — with a WARNING,
    because a silent default-to-0 (what the pre-W1 call sites did) writes slot 0's
    identity from another slot's data.
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

    bit: bool | None = None
    if exist_bits is not None and isinstance(ams_id, int) and 0 <= ams_id < 128 and tray_id >= 0:
        bit = slot_exist_bit_set(exist_bits, ams_id, tray_id)

    present = _derive_present(state, tray_type, bit)

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


def _derive_present(state: int | None, tray_type: str | None, exist_bit: bool | None) -> bool | None:
    """The tri-state presence rule. See :class:`TrayObservation` for the contract."""
    if state in TRAY_PRESENT_STATES:
        return True
    if state is not None and tray_type == "":
        # Cleared-tray shape. One veto: the push's own bitmask still marks the slot
        # occupied (the 003-H2S mid-print-insert quirk, where the per-tray state
        # sticks at 9 while the bitmask reports the spool — the merge pipeline
        # promotes exactly this case 9→10 downstream). Contradiction resolves to
        # UNKNOWN, never to EMPTY, because "empty" is what authorizes a release.
        if exist_bit is True:
            return None
        return False
    return None


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
    """
    units, exist_bits = _units_and_bits(ams_payload)
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
        for index, tray in enumerate(trays):
            if not isinstance(tray, dict):
                continue
            observations.append(observe_tray(printer_id, ams_id, tray, exist_bits=exist_bits, index=index))
    return observations


def _units_and_bits(ams_payload: object) -> tuple[list, int | None]:
    """Split a raw push into (unit list, parsed tray_exist_bits)."""
    if isinstance(ams_payload, dict):
        units = ams_payload.get("ams")
        bits = parse_tray_exist_bits(ams_payload.get("tray_exist_bits"))
        if isinstance(units, dict):
            # Nested {"ams": {"ams": [...]}} shape, as tolerated by _handle_ams_data.
            nested = units.get("ams")
            units = nested if isinstance(nested, list) else []
        return (units if isinstance(units, list) else []), bits
    if isinstance(ams_payload, list):
        return ams_payload, None
    return [], None
