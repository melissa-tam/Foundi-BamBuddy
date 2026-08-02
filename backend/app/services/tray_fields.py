"""Pure per-tray wire-field helpers shared by the MQTT client and the spool core.

Extracted from ``bambu_mqtt`` (W1 of the spool-core re-architecture) so the
observation layer (``services.tray_observation``) parses a raw AMS tray dict with
EXACTLY the same rules the MQTT merge uses — a second wire parser would drift, and
drift between "what the merge believes" and "what the spool pipeline believes" is
the root of the 2026-08-01 001-T3 chimera class.

Everything here is a pure function of its arguments: no DB, no I/O, no awaits, no
module state. ``bambu_mqtt`` and ``spool_tag_matcher`` import these (the originals
were deleted in both — one origin per helper and per sentinel).
"""

from __future__ import annotations

from backend.app.utils.tag_normalization import normalize_tag_uid, normalize_tray_uuid

# Firmware's "no tag" sentinels. The merge in ``bambu_mqtt._handle_ams_data``
# writes these on a slot-clearing update and treats them as "empty" when deciding
# whether an incoming value may overwrite stored identity, so they are wire
# vocabulary, not DB vocabulary.
ZERO_TAG_UID = "0000000000000000"
ZERO_TRAY_UUID = "00000000000000000000000000000000"


def parse_int_field(raw: object) -> int | None:
    """Parse a numeric tray field to ``int``, or ``None`` when it asserts nothing.

    Firmware sends numbers as ints and occasionally as numeric strings; a partial
    push may omit a field entirely. ``None`` means "this push did not assert a
    parseable value" — it never stands in for a zero (``remain=0`` and ``state=9``
    are real answers). ``bool`` is rejected on purpose: a JSON ``true`` in a numeric
    slot is malformed, not the number 1.
    """
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def parse_tray_state(raw: object) -> int | None:
    """Parse a tray ``state`` value to ``int``, or ``None`` when unparseable.

    Documented alias of :func:`parse_int_field` for the state field — extracted
    from the three inline try/int sites in ``bambu_mqtt`` (the merge's stale-clear
    guard, ``apply_tray_exist_bits``'s 9→10 promotion, and
    ``_slot_exist_bit_known_set``) so every reader agrees on what "no state" means.
    ``None`` is NOT "empty": 9 is firmware's empty code and parses to 9.
    """
    return parse_int_field(raw)


def parse_tray_exist_bits(value: str | int | None) -> int | None:
    """Parse a firmware ``tray_exist_bits`` value (hex string, or int) to an int.

    Firmware sends the bitmask as a hex string; ints are tolerated for defensive
    symmetry. ``None`` / empty / unparseable → ``None`` (the caller reads that as
    "this push carried no bitmask"). A genuine ``"0"`` parses to ``0`` — all slots
    empty is a real answer, distinct from "absent". Used by the ``_handle_ams_data``
    last-seen cache and by the observation layer; ``apply_tray_exist_bits`` keeps
    its own inline parse so its bit=0 / power-off contract stays byte-identical.
    """
    if value is None or value == "":
        return None
    try:
        return value if isinstance(value, int) else int(value, 16)
    except (ValueError, TypeError):
        return None


def slot_exist_bit_set(bits: int | None, ams_id: object, tray_id: object) -> bool:
    """True when ``bits`` positively marks (ams_id, tray_id) occupied.

    The pure core of ``BambuMQTTClient._slot_exist_bit_known_set``. A missing
    bitmask (``None``), an unparseable/out-of-range address, or a 0 bit all return
    False, so callers can treat this as "positive evidence only". AMS-HT units
    (``id >= 128``) use a separate addressing scheme and are never matched here,
    mirroring ``apply_tray_exist_bits``.
    """
    if bits is None:
        return False
    try:
        a = int(ams_id)  # type: ignore[arg-type]
        t = int(tray_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if a < 0 or a >= 128 or t < 0:
        return False
    return bool((bits >> (a * 4 + t)) & 1)


def normalized_tag_uid(value: object) -> str | None:
    """Canonical ``tag_uid``, or ``None`` when the value asserts no tag.

    Normalizes through the one identity normalizer
    (``utils.tag_normalization.normalize_tag_uid``) and then collapses every
    "no tag" spelling — absent, empty, and all-zero of any length (the firmware
    sentinel ``ZERO_TAG_UID`` included) — to ``None``. Same zero/emptiness rule
    ``spool_tag_matcher.is_valid_tag`` applies, expressed per-member so the
    observation layer can hold the atomic identity pair honestly.
    """
    if value is None:
        return None
    uid = normalize_tag_uid(str(value))
    if not uid or uid == ZERO_TAG_UID or uid == "0" * len(uid):
        return None
    return uid


def normalized_tray_uuid(value: object) -> str | None:
    """Canonical ``tray_uuid``, or ``None`` when the value asserts no uuid.

    Member twin of :func:`normalized_tag_uid` — see its docstring.
    """
    if value is None:
        return None
    uuid = normalize_tray_uuid(str(value))
    if not uuid or uuid == ZERO_TRAY_UUID or uuid == "0" * len(uuid):
        return None
    return uuid
