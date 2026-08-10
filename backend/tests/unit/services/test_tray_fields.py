"""Tray-field vocabulary pins: the named `state` codes, the tri-state presence table
they describe, and the `filam_bak` backup-group parser.

``tray_fields`` is the one origin every wire consumer reads (the MQTT merge, the
observation layer, the decision table, the spool machines). The constants added in WS3
change NO behavior — every non-9/10/11 value already fell through to UNKNOWN — so what
these tests defend is that the TABLE and the CODE cannot drift apart: the named
vocabulary is now cited by ``bambu_mqtt._normalize_cleared_trays``, and a future edit
that quietly moved a dialect code into the "empty" bucket would authorize a destructive
release on a possibly-loaded tray.
"""

import pytest

from backend.app.services import bambu_mqtt, tray_fields
from backend.app.services.tray_fields import (
    TRAY_PRESENT_STATES,
    TRAY_STATE_DIALECT,
    TRAY_STATE_EMPTY,
    TRAY_STATE_FED,
    TRAY_STATE_SEATED,
    TRAY_STATE_TRANSITIONAL,
    TRAY_STATE_UNREPORTED,
    parse_filam_bak,
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
