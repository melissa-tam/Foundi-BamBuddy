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

# Tray `state` codes that mean a spool is physically PRESENT: 11 = loaded, 10 =
# "spool present, filament not in feeder" (see the merge comment in
# ``bambu_mqtt._handle_ams_data``). Wiping tray identity for a present spool is the
# bug behind the AMS-drying incident (drying disengages trays to state 10, and the
# identity wipe then storms the RFID pipeline into HMS 0700_C069) and behind routine
# load/unload transit wipes (~50×/week fleet-wide). It lives HERE, beside the parser
# that reads the field, because :func:`tray_presence` — the one presence rule every
# consumer gates on — needs it and ``bambu_mqtt`` already imports this module (the
# reverse import would be a cycle). ``bambu_mqtt`` re-exports the same tuple object,
# so ``ams_presence``/``tray_observation``/``spool_recovery`` keep their one origin.
TRAY_PRESENT_STATES = (10, 11)


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


def asserted_str_field(tray: dict, key: str) -> str | None:
    """A tray dict's string field with ASSERTION preserved.

    ``None`` when the push carried no such key (this push said nothing about it),
    ``""`` when it carried an explicit null or an empty/whitespace value (the push
    asserts "nothing here" — the merge treats null and ``""`` identically in its
    always-update list), otherwise the stripped value. The distinction is
    load-bearing for :func:`tray_presence`: only an ASSERTED-empty ``tray_type``
    may drive a release.

    "Asserted" means asserted by the NORMALIZED wire: the raw stream is normalized
    before observation, so a minimal ``{id, state == 9}`` partial that passes the
    exist-bit veto arrives carrying the injected asserted-empty shape (deliberate —
    ``bambu_mqtt._normalize_cleared_trays``).
    """
    if key not in tray:
        return None
    value = tray.get(key)
    if value is None:
        return ""
    return str(value).strip()


def tray_presence(state: int | None, tray_type: str | None, exist_bit: bool | None = None) -> bool | None:
    """THE tri-state presence rule for one tray — presence ≠ identity.

    One origin for every consumer (the observation layer, the assignments API, the
    weight-sync tool, the deficit pricer, the scheduler's candidate filter, the
    unassigned-tray alert, the K-persist route). Answers:

    * ``True``  — ``state`` is in :data:`TRAY_PRESENT_STATES` (10 = seated, 11 = fed).
    * ``False`` — ``state`` parsed, is not a present code, AND ``tray_type`` was
      asserted EMPTY (``""``). That pair is the verified prod cleared-tray shape
      (state 9 + ``tray_type: ""``); it is the ONLY shape that may drive a release
      or a consumer skip. One veto: when the push's own ``tray_exist_bits`` still
      marks the slot occupied (the 003-H2S mid-print-insert quirk, where the
      per-tray state sticks at 9 while the bitmask reports the spool), the
      contradiction resolves to UNKNOWN — never to EMPTY, because "empty" is what
      authorizes destructive action.
    * ``None``  — anything else: no parseable state (a partial push), a dialect that
      never reports presence (the A1-family/P1S always-``state=3`` firmwares), or a
      state-9 slot that still asserts a filament type (the 004-H2S
      state-9-while-feeding dialect).

    Consumers gate on ``is False`` ONLY — unknown always fails OPEN.

    ``tray_type`` must carry assertion (see :func:`asserted_str_field`): ``None``
    means "not asserted", ``""`` means "asserted empty". The raw stream is
    normalized BEFORE observation, so a minimal ``{id, state == 9}`` partial that
    passes the exist-bit veto reaches here with an injected asserted-empty shape
    (deliberate — ``bambu_mqtt._normalize_cleared_trays``) and answers ``False``
    instead of ``None``; that is the ONLY way a boot-forgotten slot ever becomes
    releasable.
    """
    if state in TRAY_PRESENT_STATES:
        return True
    if state is not None and tray_type == "":
        if exist_bit is True:
            return None
        return False
    return None


def tray_presence_from_dict(tray: object) -> bool | None:
    """:func:`tray_presence` for a MERGED AMS tray dict (the display-side shape).

    Convenience for the consumers that read ``printer_manager`` status rather than
    a raw pre-merge push: parses ``state`` and ``tray_type`` with THIS module's own
    parsers so a merged-data consumer can never disagree with the observation layer.

    No exist-bit is consulted: the bitmask is a per-PUSH sibling field, and the
    merged view keeps no honest per-tray copy of it, so passing a stale bit would
    manufacture a veto. Omitting it can only make the answer *less* decisive
    (``False`` instead of ``None``) for the one 003-H2S shape — and the merge's own
    9→10 promotion already resolves that case before a consumer sees it.
    """
    if not isinstance(tray, dict):
        return None
    return tray_presence(parse_tray_state(tray.get("state")), asserted_str_field(tray, "tray_type"))


def tray_identity_asserted(tray: object) -> bool:
    """Does this tray dict assert ANY identity for what is in it?

    Identity is the OTHER half of invariant 3 (presence ≠ identity), and it has three
    carriers, any one of which is enough:

    * an asserted, non-empty ``tray_type`` (the configured filament — an RFID read or a
      tagless autoconfig put it there);
    * a non-empty ``tray_info_idx`` (the Bambu preset id);
    * a valid RFID pair (``tag_uid`` / ``tray_uuid``), normalized through
      :func:`normalized_tag_uid` / :func:`normalized_tray_uuid` so every "no tag"
      spelling — absent, empty, all-zero — collapses to "no identity".

    ``False`` therefore means the wire says NOTHING about what is in the slot. Paired
    with :func:`tray_presence` it yields the seated-but-unidentified class
    (:func:`tray_unread`). Pure, never raises: a non-dict reads as no identity.
    """
    if not isinstance(tray, dict):
        return False
    if asserted_str_field(tray, "tray_type"):
        return True
    if asserted_str_field(tray, "tray_info_idx"):
        return True
    return (
        normalized_tag_uid(tray.get("tag_uid")) is not None or normalized_tray_uuid(tray.get("tray_uuid")) is not None
    )


def tray_unread(tray: object) -> bool:
    """SEATED but UNIDENTIFIED — a roll is physically in the tray and the farm has no
    idea which one.

    ``tray_presence_from_dict(tray) is True`` AND :func:`tray_identity_asserted` is
    False. This is the class 12 fleet trays sat in for days while the selection,
    deficit and staging layers priced them as EMPTY (remain 0, no material) and staged
    work behind phantom "Low filament" deficits — a slot that physically holds filament
    is not an empty slot, it is an UNKNOWN one, and the only cure is to read it.

    Deliberately gated on the canonical tri-state presence rule, never on
    ``tray_type`` emptiness: an asserted-empty ``tray_type`` beside a non-present state
    is the CLEARED shape (presence ``False``), which is a different fact with a
    different consequence (release, doctrine rule 9). Unknown presence is likewise NOT
    unread — nothing is known about the slot at all, and WS1's presence-stale machinery
    owns that case.
    """
    return tray_presence_from_dict(tray) is True and not tray_identity_asserted(tray)


def tray_presence_map(ams_payload: object) -> dict[tuple[int, int], bool | None]:
    """``{(ams_id, tray_id): present}`` for every tray in a live/merged AMS payload.

    Accepts what ``PrinterState.raw_data`` actually holds — the full status dict
    (``{"ams": [...]}`` or the nested ``{"ams": {"ams": [...]}}`` variant), a bare
    unit list, or ``None`` — and never raises: an unreadable payload yields ``{}``,
    which every consumer reads as "no presence evidence" (fail open). Slots the
    payload does not mention are ABSENT FROM THE MAP, never ``False``: silence
    about a slot is not an observation of it.
    """
    units = ams_payload
    if isinstance(units, dict):
        units = units.get("ams")
        if isinstance(units, dict):
            units = units.get("ams")
    if not isinstance(units, list):
        return {}

    out: dict[tuple[int, int], bool | None] = {}
    for unit in units:
        if not isinstance(unit, dict):
            continue
        ams_id = parse_int_field(unit.get("id"))
        if ams_id is None:
            continue
        trays = unit.get("tray")
        if not isinstance(trays, list):
            continue
        for tray in trays:
            if not isinstance(tray, dict):
                continue
            tray_id = parse_int_field(tray.get("id"))
            if tray_id is None:
                continue
            out[(ams_id, tray_id)] = tray_presence_from_dict(tray)
    return out


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
