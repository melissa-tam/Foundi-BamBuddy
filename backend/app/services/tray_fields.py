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

# --- tray `state` vocabulary -------------------------------------------------
#
# Every per-tray ``state`` value the fleet emits, named once so no consumer spells
# a bare number again (doctrine invariant 1 — one origin per magic value). The
# table below IS the contract; :func:`tray_presence` implements it and nothing
# else may re-derive it:
#
# | constant                  | value(s) | what the firmware means            | tray_presence |
# |---------------------------|----------|------------------------------------|---------------|
# | ``TRAY_STATE_EMPTY``      | 9        | "no spool" — the ONLY empty code   | ``False``*    |
# | ``TRAY_STATE_SEATED``     | 10       | spool seated, filament not fed     | ``True``      |
# | ``TRAY_STATE_FED``        | 11       | loaded / feeding                   | ``True``      |
# | ``TRAY_STATE_UNREPORTED`` | 0        | "tray detail not being reported"   | ``None``      |
# | ``TRAY_STATE_TRANSITIONAL`` | 8, 26  | load / unload transit              | ``None``      |
# | ``TRAY_STATE_DIALECT``    | 3, 25, 27| A1/P1S constant + H2C loaded-tray  | ``None``      |
#
# \* 9 alone is NOT enough: presence answers ``False`` only when ``tray_type`` is
# ASSERTED empty beside it (:func:`asserted_str_field`), because "empty" is what
# authorizes a destructive release and 004-H2S feeds whole prints at state 9.
#
# The last three rows are the reason presence is TRI-state. ``0`` is the H2C
# long-idle dialect (the tray is not being described, which is not a claim that it
# is bare); ``8``/``26`` are seen mid-load and mid-unload (state 8 observed live on
# a mid-unload H2S slot); ``3`` is the A1-family/P1S constant that never varies and
# ``25``/``27`` are H2C values observed on visibly LOADED trays. Reading any of them
# as empty would release a live binding out from under a seated roll, so they all
# answer UNKNOWN — and unknown fails OPEN at every consumer. Naming them changes no
# behavior (every non-9/10/11 value already fell through to ``None``); it makes the
# vocabulary auditable and gives ``bambu_mqtt._normalize_cleared_trays`` something to
# cite instead of re-listing the codes in prose.
TRAY_STATE_EMPTY = 9
TRAY_STATE_SEATED = 10
TRAY_STATE_FED = 11
TRAY_STATE_UNREPORTED = 0
TRAY_STATE_TRANSITIONAL: tuple[int, int] = (8, 26)
TRAY_STATE_DIALECT: tuple[int, int, int] = (3, 25, 27)

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
TRAY_PRESENT_STATES = (TRAY_STATE_SEATED, TRAY_STATE_FED)

# Trays per bitmask-addressable AMS unit. The firmware packs `tray_exist_bits` (and
# every sibling per-tray mask) as one flat integer addressed ``ams_id * 4 + tray_id``,
# which is why an AMS-HT unit — one tray, ids >= 128 — cannot be addressed in it at all.
# Named here because both the bit arithmetic below and ``bambu_mqtt``'s merge-side apply
# need the stride, and two spellings of it would silently address different slots.
TRAYS_PER_AMS_UNIT = 4


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
    """Parse a firmware exist-bit mask (hex string, or int) to an int.

    THE hex-mask parser for this dialect: ``tray_exist_bits`` (one bit per slot,
    addressed ``ams_id * TRAYS_PER_AMS_UNIT + tray_id``) and ``ams_exist_bits`` (one
    bit per AMS unit) share the encoding, so they share this reader — a second parse
    is a second place for a mask to be mis-read.

    Firmware sends the mask as a hex string; ints are tolerated for defensive
    symmetry. ``None`` / empty / unparseable → ``None`` (the caller reads that as
    "this push carried no mask"). A genuine ``"0"`` parses to ``0`` — all slots empty
    is a real answer, distinct from "absent", and the two must never collapse: ``0``
    IS the firmware saying the unit is bare.
    """
    if value is None or value == "":
        return None
    try:
        return value if isinstance(value, int) else int(value, 16)
    except (ValueError, TypeError):
        return None


def slot_exist_bit(bits: int | None, ams_id: object, tray_id: object) -> bool | None:
    """The exist-bit EVIDENCE for one slot: ``True``/``False``, or ``None`` when none.

    ``None`` is returned for every case in which the mask says nothing ABOUT THIS SLOT,
    and the distinction carries weight now that a clear bit is release-authorizing
    evidence (:func:`tray_presence`): a 0 read off an address the mask does not cover
    would be a fabricated "empty".

    Unaddressable, hence ``None``:

    * no mask at all;
    * an unparseable ``ams_id`` / ``tray_id``;
    * an AMS-HT unit (``id >= 128``) — a separate addressing scheme, never packed here;
    * a ``tray_id`` outside ``0 .. TRAYS_PER_AMS_UNIT - 1``, whose "bit" would in fact
      be a NEIGHBOURING unit's slot.
    """
    if bits is None:
        return None
    try:
        a = int(ams_id)  # type: ignore[arg-type]
        t = int(tray_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if a < 0 or a >= 128:
        return None
    if t < 0 or t >= TRAYS_PER_AMS_UNIT:
        return None
    return bool((bits >> (a * TRAYS_PER_AMS_UNIT + t)) & 1)


def slot_exist_bit_set(bits: int | None, ams_id: object, tray_id: object) -> bool:
    """True when ``bits`` POSITIVELY marks (ams_id, tray_id) occupied.

    The positive-evidence-only view of :func:`slot_exist_bit`, for the callers whose
    question is "may I treat this slot as occupied?" — ``BambuMQTTClient``'s stale-clear
    veto and its raw-side normalizer. Absent evidence and a clear bit are the same
    answer here (False); a caller that needs to tell them apart wants the tri-state.
    """
    return slot_exist_bit(bits, ams_id, tray_id) is True


def unit_exist_bit_set(bits: int | None, ams_id: object) -> bool:
    """True when an ``ams_exist_bits`` mask positively marks AMS unit ``ams_id`` present.

    Unit-level twin of :func:`slot_exist_bit_set` (bit index = the unit id, AMS-HT
    excluded for the same reason). Callers use it to GATE tray-level exist-bit evidence:
    a unit the firmware does not list is a unit whose slice of ``tray_exist_bits`` is
    zero for the trivial reason that it is not being reported, and reading those zeros
    as "four empty trays" invents the one answer that authorizes a release.

    Positive evidence only, and the gating is evidence-REMOVING by construction: an
    absent/unparseable ``ams_exist_bits`` must be handled by the caller as "no gating"
    (unknown fails OPEN), never as "no unit exists".
    """
    if bits is None:
        return False
    try:
        a = int(ams_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if a < 0 or a >= 128:
        return False
    return bool((bits >> a) & 1)


def parse_filam_bak(source: object) -> list[int] | None:
    """Parse a firmware ``filam_bak`` array — the trays enrolled in AUTO-REFILL BACKUP.

    ``filam_bak`` is the firmware's own statement of which trays it will switch between
    when one runs dry. Do NOT confuse it with ``ams_filament_backup``
    (``bambu_mqtt.parse_ams_filament_backup_from_cfg``, bit 18 of ``print.cfg``): that is
    the CAPABILITY boolean "is the feature on", this is the live MEMBERSHIP list.

    **The field is not per-AMS-unit** (verified 2026-08-09 against the OpenBambuAPI
    ``pushall`` reference, BambuStudio/OrcaSlicer ``DeviceManager.cpp`` +
    ``DevExtruderSystem.cpp``, and a live production status pull — no AMS unit or tray
    block carries it). Two wire shapes exist, and this parser is deliberately agnostic
    to both because it reads ONE key off whatever dict the caller hands it:

    * ``print.filam_bak`` — flat, machine-level (legacy / general);
    * ``print.device.extruder.info[i].filam_bak`` — per EXTRUDER, the newer
      substructure that a dual-nozzle machine reports.

    Returns the parsed ints, or ``None`` when the source asserts no array at all (not a
    dict, key absent, or the value is not a list). An EMPTY list is preserved as ``[]``,
    a real answer distinct from ``None``: firmware clears and refills this field on
    every report, so ``[]`` means "nothing is enrolled right now". Elements that are not
    parseable ints are dropped rather than poisoning the list.

    **Element encoding is UNCONFIRMED** — no source states whether the ints are global
    tray ids (``ams_id * 4 + tray_id``) or per-unit slot ids, and both clients parse them
    as bare ints with no bit-extraction. Consumers must therefore be written so that the
    global-id reading can only UNDER-match: for any tray outside AMS 0 the two encodings
    occupy disjoint value ranges, so a global-id comparison silently fails to corroborate
    rather than corroborating something false.
    """
    if not isinstance(source, dict):
        return None
    raw = source.get("filam_bak")
    if not isinstance(raw, list):
        return None
    out: list[int] = []
    for item in raw:
        value = parse_int_field(item)
        if value is not None:
            out.append(value)
    return out


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
    unassigned-tray alert, the K-persist route). Consumers gate on ``is False`` ONLY —
    unknown always fails OPEN.

    ``exist_bit`` is THE firmware's own answer for this slot and outranks the per-tray
    ``state``/``tray_type`` pair in BOTH directions, because the pair is what the
    firmware is unreliable about: a mid-print insert leaves ``state`` stuck at 9 with
    the bit set (003-H2S), and a stable-empty tray is reduced to a keyless ``{"id": N}``
    stub that asserts nothing at all. It must therefore reach here only when it is
    TRUSTED — the client is the one authority on that (``bambu_mqtt``: a set bit is
    positive evidence and trusted at once, an all-zero mask must repeat before it may
    empty a slot) and strips an untrusted mask before the observation layer ever sees
    it. ``None`` here means the caller HAS no bit for the slot: no mask in the push, an
    AMS-HT / out-of-range address, or a unit ``ams_exist_bits`` does not list.

    | ``exist_bit`` | ``state``              | answer  | why                                    |
    |---------------|------------------------|---------|----------------------------------------|
    | ``True``      | any                    | ``True``  | the bit IS the seating; subsumes the 003-H2S stuck-9 class |
    | ``False``     | in TRAY_PRESENT_STATES | ``None``  | in-push contradiction — a release needs UNCONTRADICTED emptiness |
    | ``False``     | anything else          | ``False`` | the firmware says the slot is bare, whatever the tray block omitted |
    | ``None``      | in TRAY_PRESENT_STATES | ``True``  | 10 = seated, 11 = fed                  |
    | ``None``      | parsed, ``tray_type == ""`` | ``False`` | the verified prod cleared shape (state 9 + empty type) |
    | ``None``      | anything else          | ``None``  | a partial push, an always-``state=3`` dialect, or state 9 still asserting a type (004-H2S feeds whole prints there) |

    ``tray_type`` must carry assertion (see :func:`asserted_str_field`): ``None``
    means "not asserted", ``""`` means "asserted empty". The raw stream is normalized
    BEFORE observation, so a minimal ``{id, state == 9}`` partial that passes the
    exist-bit veto reaches here with an injected asserted-empty shape (deliberate —
    ``bambu_mqtt._normalize_cleared_trays``), which is how a boot-forgotten slot
    becomes releasable on a dialect that carries no mask at all.
    """
    if exist_bit is True:
        return True
    if exist_bit is False:
        # A trusted clear bit is the firmware's answer, and the ONLY thing that may
        # override it is the same push contradicting itself.
        return None if state in TRAY_PRESENT_STATES else False
    if state in TRAY_PRESENT_STATES:
        return True
    if state is not None and tray_type == "":
        return False
    return None


def tray_presence_from_dict(tray: object) -> bool | None:
    """:func:`tray_presence` for a MERGED AMS tray dict (the display-side shape).

    Convenience for the consumers that read ``printer_manager`` status rather than
    a raw pre-merge push: parses ``state`` and ``tray_type`` with THIS module's own
    parsers so a merged-data consumer can never disagree with the observation layer.

    No exist-bit is consulted: the bitmask is a per-PUSH sibling field, and the merged
    view keeps no honest per-tray copy of it, so passing a stale bit would manufacture
    an answer out of an old push. It costs nothing, because the merge applies the mask
    to the trays themselves (``bambu_mqtt.apply_tray_exist_bits``): a set bit promotes
    the stuck-9 tray to 10, and a clear bit writes the FULL cleared shape, so both
    polarities reach this function through ``state``/``tray_type`` and a merged reader
    can no longer answer UNKNOWN for a slot the firmware called empty.
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
