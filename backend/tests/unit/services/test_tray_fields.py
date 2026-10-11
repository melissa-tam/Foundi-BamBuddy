"""Tray-field vocabulary pins: the named `state` codes, the tri-state presence table
they describe, and the `filam_bak` backup-group parser.

``tray_fields`` is the one origin every wire consumer reads (the MQTT merge, the
observation layer, the decision table, the spool machines). The named constants
change NO behavior — every non-9/10/11 value already fell through to UNKNOWN — so what
these tests defend is that the TABLE and the CODE cannot drift apart: the named
vocabulary is now cited by ``bambu_mqtt._normalize_cleared_trays``, and a future edit
that quietly moved a dialect code into the "empty" bucket would authorize a destructive
release on a possibly-loaded tray.
"""

from types import SimpleNamespace

import pytest

from backend.app.services import bambu_mqtt, tray_fields
from backend.app.services.tray_fields import (
    TRAY_NOW_EXTERNAL_SPOOL,
    TRAY_NOW_NOTHING_FED,
    TRAY_PRESENT_STATES,
    TRAY_STATE_DIALECT,
    TRAY_STATE_EMPTY,
    TRAY_STATE_FED,
    TRAY_STATE_SEATED,
    TRAY_STATE_TRANSITIONAL,
    TRAY_STATE_UNREPORTED,
    ExtruderFeed,
    filam_bak_groups,
    parse_filam_bak,
    toolhead_feed,
    tray_presence,
)

# --- the named vocabulary ----------------------------------------------------


def test_state_constants_have_the_wire_values():
    """The numbers are the firmware's, not ours — naming them must not renumber them."""
    assert TRAY_STATE_EMPTY == 9
    assert TRAY_STATE_SEATED == 10
    assert TRAY_STATE_FED == 11
    assert TRAY_STATE_UNREPORTED == 0
    assert TRAY_STATE_TRANSITIONAL == (8, 26)
    assert TRAY_STATE_DIALECT == (3, 25, 27)


def test_present_states_tuple_is_unchanged_and_shared():
    """``TRAY_PRESENT_STATES`` is composed FROM the named constants now, but it must
    remain the same value AND the same object ``bambu_mqtt`` re-exports — doctrine
    invariant 1 (one origin per magic value) is enforced by object identity here, and
    ``ams_presence`` / ``tray_observation`` / ``spool_recovery`` all read it through
    that re-export."""
    assert TRAY_PRESENT_STATES == (10, 11)
    assert bambu_mqtt.TRAY_PRESENT_STATES is TRAY_PRESENT_STATES


@pytest.mark.parametrize(
    ("state", "tray_type", "expected", "why"),
    [
        (TRAY_STATE_SEATED, None, True, "seated is presence, with or without an identity assertion"),
        (TRAY_STATE_FED, None, True, "fed is presence"),
        (TRAY_STATE_SEATED, "", True, "presence outranks an asserted-empty type"),
        (TRAY_STATE_EMPTY, "", False, "the verified cleared shape — the ONLY releasable answer"),
        (TRAY_STATE_EMPTY, None, None, "state 9 alone asserts nothing; a partial push is UNKNOWN"),
        (TRAY_STATE_EMPTY, "PETG", None, "004-H2S feeds whole prints at state 9 — never empty"),
        (TRAY_STATE_UNREPORTED, None, None, "H2C long idle: the tray is not described, not reported bare"),
        (TRAY_STATE_UNREPORTED, "", False, "an ASSERTED-empty type beside any non-present state clears"),
        (None, "", None, "no parseable state — nothing to reason from"),
        (None, None, None, "silence"),
    ],
)
def test_presence_table_over_the_named_states(state, tray_type, expected, why):
    assert tray_presence(state, tray_type) is expected, why


@pytest.mark.parametrize("state", [*TRAY_STATE_TRANSITIONAL, *TRAY_STATE_DIALECT])
def test_transitional_and_dialect_states_are_never_present(state):
    """Load/unload transit and the A1/P1S + H2C dialect codes are UNKNOWN, never
    presence: they appear on trays that are visibly LOADED, so answering True would be a
    lie and answering False (without an asserted-empty type) would authorize a release."""
    assert tray_presence(state, None) is None
    assert tray_presence(state, "PETG") is None


@pytest.mark.parametrize("state", [*TRAY_STATE_TRANSITIONAL, *TRAY_STATE_DIALECT, TRAY_STATE_UNREPORTED])
def test_an_asserted_empty_type_still_clears_a_dialect_state(state):
    """The dialect codes are excluded from the ``_normalize_cleared_trays`` INJECTION —
    the farm never manufactures an empty assertion for them — but a push that asserts
    emptiness ITSELF is wire truth and is honored. Presence is positive-evidence-only in
    both directions."""
    assert tray_presence(state, "") is False


def test_a_set_exist_bit_is_seating_whatever_the_state_says():
    """The 003-H2S mid-print insert: the per-tray state sticks at 9 while the push's own
    bitmask reports the spool. The bit is the firmware's answer, so it decides."""
    assert tray_presence(TRAY_STATE_EMPTY, "", exist_bit=True) is True
    assert tray_presence(TRAY_STATE_EMPTY, None, exist_bit=True) is True
    assert tray_presence(None, None, exist_bit=True) is True, "no state at all is still seated"


def test_a_clear_exist_bit_empties_a_slot_that_asserts_nothing_else():
    """The printer-1 shape: a stable-empty tray reduced to ``{"id": N}``. Without the
    bit nothing in the block asserts emptiness and the slot is UNKNOWN forever."""
    assert tray_presence(None, None, exist_bit=False) is False
    assert tray_presence(TRAY_STATE_EMPTY, None, exist_bit=False) is False
    assert tray_presence(TRAY_STATE_EMPTY, "", exist_bit=False) is False
    assert tray_presence(None, None) is None, "…and with no bit it stays UNKNOWN"


def test_an_in_push_contradiction_resolves_to_unknown():
    """A clear bit beside a tray asserting a PRESENT state is the push disagreeing with
    itself. A release needs uncontradicted emptiness, so neither side wins."""
    assert tray_presence(TRAY_STATE_SEATED, "PETG", exist_bit=False) is None
    assert tray_presence(TRAY_STATE_FED, "PETG", exist_bit=False) is None
    assert tray_presence(TRAY_STATE_DIALECT[0], "PETG", exist_bit=False) is False, (
        "a dialect state is not a present state — the bit still answers"
    )


# --- filam_bak ---------------------------------------------------------------


def test_parse_filam_bak_reads_an_int_array():
    assert parse_filam_bak({"filam_bak": [0, 1]}) == [0, 1]
    assert parse_filam_bak({"filam_bak": ["2", 3]}) == [2, 3], "firmware sends numbers as strings sometimes"


def test_parse_filam_bak_distinguishes_empty_from_absent():
    """Firmware clears and refills this field on every report it appears in, so ``[]``
    is a real answer ("nothing enrolled right now") and an absent key is silence. The
    corroboration consumer treats both as "no evidence", but the log says which."""
    assert parse_filam_bak({"filam_bak": []}) == []
    assert parse_filam_bak({}) is None


@pytest.mark.parametrize(
    "source",
    [None, [], "filam_bak", 7, {"filam_bak": None}, {"filam_bak": "0,1"}, {"filam_bak": {"0": 1}}],
)
def test_parse_filam_bak_fails_closed_on_anything_else(source):
    assert parse_filam_bak(source) is None


def test_parse_filam_bak_drops_unparseable_members():
    """One bad element must not poison the group — and ``True`` is not the number 1
    (``parse_int_field`` rejects bools on purpose)."""
    assert parse_filam_bak({"filam_bak": [0, None, "x", 1, True]}) == [0, 1]


def test_parse_filam_bak_is_agnostic_to_where_the_field_lives():
    """VERIFIED 2026-08-09 against the OpenBambuAPI pushall reference, BambuStudio /
    OrcaSlicer ``DeviceManager.cpp`` + ``DevExtruderSystem.cpp``, and a live production
    status pull: the field is top-level ``print.filam_bak`` or per-EXTRUDER
    ``print.device.extruder.info[i].filam_bak`` — it is NOT nested per AMS unit. This
    parser therefore reads ONE key off whatever carrier the caller hands it, and the
    caller owns the shape walk."""
    top_level = {"gcode_state": "RUNNING", "filam_bak": [4, 5]}
    per_extruder = {"id": 0, "snow": 258, "filam_bak": [4, 5]}
    assert parse_filam_bak(top_level) == parse_filam_bak(per_extruder) == [4, 5]
    # An AMS unit block carries no such key on any observed firmware.
    assert parse_filam_bak({"id": 0, "humidity": "4", "tray": []}) is None


def test_filam_bak_groups_expands_one_mask_per_group():
    """Each element is a group's slot BITMASK — confirmed 2026-08-25 against production
    occupancy: 001-H2S full reported ``[15]``, 002-H2S with T0..T2 reported ``[7]``,
    004-H2S with T2+T3 reported ``[12]``, 012-H2S with T1..T3 reported ``[14]``."""
    assert filam_bak_groups([15]) == [{0, 1, 2, 3}]
    assert filam_bak_groups([7]) == [{0, 1, 2}]
    assert filam_bak_groups([12]) == [{2, 3}]


def test_filam_bak_groups_keeps_separate_masks_separate():
    """Two masks are two groups, not one union: a printer that pairs 1+2 and 3+4 must not
    read as a single four-slot group, which is the whole question the corroboration asks."""
    assert filam_bak_groups([3, 12]) == [{0, 1}, {2, 3}]


def test_filam_bak_groups_drops_a_mask_with_no_members():
    """A group with no members is not a group. ``0`` is the firmware saying this lane has
    no group at all — reading it as "slot 0, alone" would invent one."""
    assert filam_bak_groups([0]) == []
    assert filam_bak_groups([0, 3]) == [{0, 1}]


def test_filam_bak_groups_has_nothing_to_expand():
    """Empty and absent both expand to nothing HERE; the CALLER keeps them distinct
    (a reported-but-groupless answer is evidence, silence is not)."""
    assert filam_bak_groups([]) == []
    assert filam_bak_groups(None) == []


def test_filam_bak_groups_tolerates_garbage():
    """It rides the ~1 Hz status callback (invariant 10), so a nonsensical int contributes
    no members instead of raising — a negative one especially, whose two's-complement bits
    are infinite."""
    assert filam_bak_groups([-1]) == []
    assert filam_bak_groups([-8, 5]) == [{0, 2}]


def test_module_exports_the_vocabulary_by_name():
    """A cheap guard against a rename silently orphaning the citations in
    ``bambu_mqtt._normalize_cleared_trays``' docstring."""
    for name in (
        "TRAY_STATE_EMPTY",
        "TRAY_STATE_SEATED",
        "TRAY_STATE_FED",
        "TRAY_STATE_UNREPORTED",
        "TRAY_STATE_TRANSITIONAL",
        "TRAY_STATE_DIALECT",
    ):
        assert hasattr(tray_fields, name), name


# --- the toolhead feed (K1: ONE reader of "is the toolhead fed") ----------------------
#
# ``toolhead_feed`` reads the three wire fields the client keeps for it — ``tray_now``,
# ``h2d_extruder_snow`` (``{extruder_id: global_tray}``, populated only on dual-nozzle
# machines) and ``active_extruder`` — and answers per extruder plus for the ACTIVE one.
# The active reading is the one a refill decision needs: on an H2C an empty active nozzle
# beside a loaded one prints air, so "any extruder fed" is the wrong question.


def _feed_state(**fields: object) -> SimpleNamespace:
    """A duck-typed state: the reader asks for three fields by name, nothing else."""
    base: dict[str, object] = {"tray_now": 255, "h2d_extruder_snow": {}, "active_extruder": 0}
    base.update(fields)
    return SimpleNamespace(**base)


@pytest.mark.parametrize(
    ("tray_now", "expected"),
    [
        (3, ExtruderFeed("fed", 3)),
        (0, ExtruderFeed("fed", 0)),
        (128, ExtruderFeed("fed", 128)),  # an AMS-HT unit's one tray is a real feeder
        (TRAY_NOW_EXTERNAL_SPOOL, ExtruderFeed("external", None)),
        (TRAY_NOW_NOTHING_FED, ExtruderFeed("empty", None)),
        (None, ExtruderFeed("unknown", None)),
        ("garbage", ExtruderFeed("unknown", None)),
        (300, ExtruderFeed("unknown", None)),
        (-1, ExtruderFeed("unknown", None)),
    ],
)
def test_a_single_nozzle_reads_tray_now(tray_now, expected):
    feed = toolhead_feed(_feed_state(tray_now=tray_now))

    assert feed.active == expected
    assert feed.active_extruder == 0
    assert feed.extruders == ((0, expected),)
    assert feed.extruder(0) == expected


def test_a_numeric_string_is_parsed_like_every_tray_field():
    assert toolhead_feed(_feed_state(tray_now="5")).active == ExtruderFeed("fed", 5)


def test_no_state_is_unknown_with_no_extruders():
    """No status yet: nothing was read, so nothing is claimed — never "empty"."""
    feed = toolhead_feed(None)

    assert feed.active == ExtruderFeed("unknown", None)
    assert feed.active_extruder is None
    assert feed.extruders == ()


def test_a_state_without_the_fields_is_unknown():
    feed = toolhead_feed(SimpleNamespace(state="RUNNING"))

    assert feed.active == ExtruderFeed("unknown", None)


def test_a_dual_nozzle_reads_the_active_extruders_own_feeder():
    """The H2C hazard: the RIGHT nozzle (0) is loaded from slot 5, the LEFT (1) is active
    and empty. ``tray_now`` still names slot 5 — the active reading must not."""
    feed = toolhead_feed(_feed_state(tray_now=5, h2d_extruder_snow={0: 5, 1: 255}, active_extruder=1))

    assert feed.active == ExtruderFeed("empty", None)
    assert feed.active_extruder == 1
    assert feed.extruder(0) == ExtruderFeed("fed", 5)
    assert feed.extruder(1) == ExtruderFeed("empty", None)
    assert feed.extruders == ((0, ExtruderFeed("fed", 5)), (1, ExtruderFeed("empty", None)))


def test_a_dual_nozzle_reads_a_fed_and_an_external_active_extruder():
    assert toolhead_feed(_feed_state(h2d_extruder_snow={0: 2, 1: 6}, active_extruder=1)).active == ExtruderFeed(
        "fed", 6
    )
    assert toolhead_feed(
        _feed_state(h2d_extruder_snow={0: 2, 1: TRAY_NOW_EXTERNAL_SPOOL}, active_extruder=1)
    ).active == ExtruderFeed("external", None)


def test_a_dual_nozzle_whose_active_extruder_has_no_reading_is_unknown():
    """The per-extruder map is the dual-nozzle truth; an extruder it does not list was
    not read, and ``tray_now`` (a single value guessed onto a unit) does not stand in."""
    feed = toolhead_feed(_feed_state(tray_now=2, h2d_extruder_snow={0: 2}, active_extruder=1))

    assert feed.active == ExtruderFeed("unknown", None)
    assert feed.extruder(1) == ExtruderFeed("unknown", None)


def test_an_unlisted_extruder_is_unknown():
    assert toolhead_feed(_feed_state(tray_now=3)).extruder(1) == ExtruderFeed("unknown", None)


def test_the_reader_is_a_leaf_that_imports_no_client():
    """K1: a pure leaf — the MQTT client imports THIS module, so the reverse would be a
    cycle, and a reader that needed the client could not be used on a recorded state."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(tray_fields.__file__).read_text(encoding="utf-8"))
    imported = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)} | {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    }

    assert not any("bambu_mqtt" in name or "printer_manager" in name for name in imported), imported


class TestGlobalTrayCodec:
    """The ONE global-tray encoding (invariant 1), and its exact inverse.

    A bare ``ams_id * 4 + tray_id`` is correct for a regular AMS and silently wrong for the
    other two conventions the fleet actually runs — which is why the arithmetic is not to be
    re-spelled at call sites.
    """

    def _codec(self):
        return tray_fields.encode_global_tray, tray_fields.decode_global_tray

    def test_regular_ams_round_trips(self):
        encode, decode = self._codec()
        for ams_id in range(4):
            for tray_id in range(4):
                assert decode(encode(ams_id, tray_id)) == (ams_id, tray_id)
        assert encode(0, 3) == 3 and encode(1, 0) == 4

    def test_ams_ht_is_its_own_unit_id_not_the_multiplication(self):
        encode, decode = self._codec()
        assert encode(128, 0) == 128, "a single-tray AMS-HT reports global == ams_id"
        assert decode(128) == (128, 0)
        assert encode(128, 1) is None, "an AMS-HT has no second tray to name"

    def test_the_external_holder_uses_the_254_convention(self):
        encode, decode = self._codec()
        assert (encode(255, 0), encode(255, 1)) == (254, 255)
        assert decode(254) == (255, 0) and decode(255) == (255, 1)
        assert encode(255, 2) is None

    def test_an_unaddressable_slot_fails_closed(self):
        encode, _decode = self._codec()
        # A fabricated global id would compare EQUAL to some real slot and mis-attribute a
        # fault to it, so every unrepresentable input answers None rather than guessing.
        assert encode(None, 0) is None
        assert encode(0, None) is None
        assert encode(-1, 0) is None
        assert encode(0, 4) is None
        assert encode(200, 0) is None
        assert encode("x", 0) is None
