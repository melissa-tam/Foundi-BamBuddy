"""Per-push AMS tray observations — the epistemic contract (W1).

Every assertion here defends ONE property: an observation states only what THIS
push said. Fields the push omitted are UNKNOWN, never inherited, never inferred.

The incident that forced the layer (2026-08-01, 001-H2S T3): the MQTT merge never
overwrites ``tag_uid``/``tray_uuid`` with empty values, so the merged view of that
slot became a CHIMERA — the newly-inserted roll's ``tag_uid`` (3CF1F3E7…) beside the
DEPARTED roll's ``tray_uuid`` (8AC9EC08…, stored on spool 46 whose tag is EC96F1E7…).
Phase-1's OR-keep matched on the stale uuid alone, kept the wrong binding, never
minted the new tag, and charged 820 g to a roll that had left the fleet. The wire
shapes replayed below are the ones captured from that printer.
"""

import logging
from dataclasses import FrozenInstanceError

import pytest

from backend.app.services.tray_fields import (
    normalized_tag_uid,
    normalized_tray_uuid,
    parse_int_field,
    parse_tray_exist_bits,
    parse_tray_state,
    slot_exist_bit_set,
)
from backend.app.services.tray_observation import (
    TrayObservation,
    observe_ams_push,
    observe_tray,
)

# --- captured prod wire shapes (001-H2S T3, 2026-08-01) ---------------------

CHIMERA_PUSH_A = {
    "id": 3,
    "tag_uid": "EC96F1E700000100",
    "tray_uuid": "8AC9EC0847FD41D0890870319F2E1975",
    "tray_type": "PETG",
    "tray_color": "000000FF",
    "state": 11,
}
CHIMERA_PUSH_B = {"id": 3, "state": 9, "tray_type": ""}
# The re-insert: a NEW tag asserted, and NO tray_uuid key at all.
CHIMERA_PUSH_C = {"id": 3, "tag_uid": "3CF1F3E700000100", "tray_type": "PETG", "state": 11}

# 003-H2S T2 / spool 140 — the stale-empty binding the release lane must clear.
STALE_EMPTY_TRAY = {"id": 2, "state": 9, "tray_type": ""}


# --- tray_fields: the shared parsers ----------------------------------------


class TestTrayFields:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (9, 9),
            (0, 0),
            ("11", 11),
            (10, 10),
            (None, None),
            ("", None),
            ("garbage", None),
            (True, None),  # a JSON bool in a numeric slot is malformed, not 1
            ({}, None),
        ],
    )
    def test_parse_int_field(self, raw, expected):
        assert parse_int_field(raw) == expected

    def test_parse_tray_state_is_the_same_parser(self):
        # Documented alias — one origin for "what does 'no state' mean".
        assert parse_tray_state("9") == 9
        assert parse_tray_state(None) is None

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("f", 15),
            ("0", 0),  # all slots empty is a real answer, not "absent"
            (15, 15),
            (None, None),
            ("", None),
            ("zz", None),
        ],
    )
    def test_parse_tray_exist_bits(self, raw, expected):
        assert parse_tray_exist_bits(raw) == expected

    @pytest.mark.parametrize(
        ("bits", "ams_id", "tray_id", "expected"),
        [
            (0b1111, 0, 0, True),
            (0b1111, 0, 3, True),
            (0b0001, 0, 1, False),
            (0b1_0000, 1, 0, True),  # bit 4 = AMS1 T0
            (None, 0, 0, False),  # no bitmask → no positive evidence
            (0b1111, 128, 0, False),  # AMS-HT uses a different addressing scheme
            (0b1111, -1, 0, False),
            (0b1111, "x", 0, False),
        ],
    )
    def test_slot_exist_bit_set(self, bits, ams_id, tray_id, expected):
        assert slot_exist_bit_set(bits, ams_id, tray_id) is expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("3CF1F3E700000100", "3CF1F3E700000100"),
            ("3cf1f3e700000100", "3CF1F3E700000100"),
            ("0000000000000000", None),  # firmware "no tag" sentinel
            ("0000", None),  # all-zero of any length
            ("", None),
            (None, None),
        ],
    )
    def test_normalized_tag_uid(self, raw, expected):
        assert normalized_tag_uid(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("8AC9EC0847FD41D0890870319F2E1975", "8AC9EC0847FD41D0890870319F2E1975"),
            ("00000000000000000000000000000000", None),
            ("", None),
            (None, None),
        ],
    )
    def test_normalized_tray_uuid(self, raw, expected):
        assert normalized_tray_uuid(raw) == expected


# --- assertion semantics: absent vs asserted-empty vs asserted --------------


class TestAssertionSemantics:
    def test_absent_config_keys_are_unknown_not_empty(self):
        obs = observe_tray(1, 0, {"id": 0, "state": 10})
        assert obs.config_asserted is False
        assert obs.tray_type is None
        assert obs.tray_color is None
        assert obs.tray_info_idx is None
        assert obs.tray_sub_brands is None
        assert obs.tray_type_asserted_empty is False

    def test_asserted_empty_type_is_distinct_from_absent(self):
        obs = observe_tray(1, 0, STALE_EMPTY_TRAY)
        assert obs.config_asserted is True
        assert obs.tray_type == ""
        assert obs.tray_type_asserted_empty is True
        assert obs.config_nonempty is False

    def test_asserted_config(self):
        obs = observe_tray(1, 0, CHIMERA_PUSH_A)
        assert obs.config_asserted is True
        assert obs.tray_type == "PETG"
        assert obs.tray_color == "000000FF"
        assert obs.config_nonempty is True

    def test_explicit_null_type_reads_as_asserted_empty(self):
        obs = observe_tray(1, 0, {"id": 0, "state": 9, "tray_type": None})
        assert obs.config_asserted is True
        assert obs.tray_type == ""

    def test_values_are_stripped(self):
        obs = observe_tray(1, 0, {"id": 0, "state": 11, "tray_type": " PETG "})
        assert obs.tray_type == "PETG"

    @pytest.mark.parametrize(
        ("tray", "expect_remain", "expect_min", "expect_max"),
        [
            ({"id": 0, "remain": 100, "nozzle_temp_min": 230, "nozzle_temp_max": 270}, 100, 230, 270),
            ({"id": 0, "remain": "20"}, 20, None, None),
            ({"id": 0, "remain": -1}, -1, None, None),  # tagless trays always report -1
            ({"id": 0}, None, None, None),
            ({"id": 0, "remain": "n/a"}, None, None, None),
        ],
    )
    def test_numeric_fields(self, tray, expect_remain, expect_min, expect_max):
        obs = observe_tray(1, 0, tray)
        assert obs.remain == expect_remain
        assert obs.nozzle_temp_min == expect_min
        assert obs.nozzle_temp_max == expect_max


# --- identity: the ATOMIC PAIR rule ----------------------------------------


class TestAtomicIdentityPair:
    def test_tag_without_uuid_key_leaves_uuid_none(self):
        obs = observe_tray(1, 0, CHIMERA_PUSH_C)
        assert obs.tag_uid == "3CF1F3E700000100"
        assert obs.tray_uuid is None  # the chimera killer: nothing to inherit from
        assert obs.identity_asserted is True

    def test_uuid_without_tag_key_leaves_tag_none(self):
        obs = observe_tray(1, 0, {"id": 0, "state": 11, "tray_uuid": "8AC9EC0847FD41D0890870319F2E1975"})
        assert obs.tag_uid is None
        assert obs.tray_uuid == "8AC9EC0847FD41D0890870319F2E1975"
        assert obs.identity_asserted is True

    def test_zero_sentinels_assert_nothing(self):
        obs = observe_tray(
            1,
            0,
            {
                "id": 0,
                "state": 11,
                "tray_type": "PETG",
                "tag_uid": "0000000000000000",
                "tray_uuid": "00000000000000000000000000000000",
            },
        )
        assert obs.tag_uid is None
        assert obs.tray_uuid is None
        assert obs.identity_asserted is False

    def test_both_members_asserted(self):
        obs = observe_tray(1, 0, CHIMERA_PUSH_A)
        assert obs.tag_uid == "EC96F1E700000100"
        assert obs.tray_uuid == "8AC9EC0847FD41D0890870319F2E1975"

    def test_identity_is_never_inherited_across_pushes(self):
        """001-T3 replay: A (tag+uuid) → B (cleared) → C (new tag, no uuid key)."""
        a = observe_tray(1, 0, CHIMERA_PUSH_A)
        b = observe_tray(1, 0, CHIMERA_PUSH_B)
        c = observe_tray(1, 0, CHIMERA_PUSH_C)
        assert (a.tag_uid, a.tray_uuid) == ("EC96F1E700000100", "8AC9EC0847FD41D0890870319F2E1975")
        assert (b.tag_uid, b.tray_uuid) == (None, None)
        assert b.identity_asserted is False
        assert (c.tag_uid, c.tray_uuid) == ("3CF1F3E700000100", None)


# --- presence: tri-state ----------------------------------------------------


class TestPresence:
    @pytest.mark.parametrize("state", [10, 11])
    def test_present_states(self, state):
        assert observe_tray(1, 0, {"id": 0, "state": state}).present is True

    def test_cleared_tray_shape_is_the_only_false(self):
        assert observe_tray(1, 0, STALE_EMPTY_TRAY).present is False

    @pytest.mark.parametrize(
        ("tray", "why"),
        [
            ({"id": 0, "state": 9}, "state 9 but the push asserted no tray_type"),
            ({"id": 0, "state": 9, "tray_type": "PETG"}, "004-H2S state-9-while-feeding dialect"),
            ({"id": 0, "state": 3, "tray_type": "PLA"}, "A1-family/P1S always-state-3 dialect, spool loaded"),
            ({"id": 0, "state": 0, "tray_type": "PETG"}, "H2C long-idle 'detail not reported'"),
            (
                {"id": 0, "state": 27, "tray_type": "PETG", "tag_uid": "1C63F1E700000100", "remain": 100},
                "008-H2C A2T0: unknown dialect state 27 on a visibly loaded tray",
            ),
            ({"id": 0, "tray_type": ""}, "no parseable state at all"),
            ({"id": 0}, "minimal partial: nothing asserted"),
        ],
    )
    def test_unknown_presence(self, tray, why):
        assert observe_tray(1, 0, tray).present is None, why

    @pytest.mark.parametrize("state", [9, 3, 0, 27, 8])
    def test_any_non_present_state_plus_asserted_empty_type_is_empty(self, state):
        """The cleared-tray shape is state-code agnostic: what makes it EMPTY is the
        pair (a parsed non-present state) + (tray_type asserted EMPTY). A dialect
        code we don't recognise still means "not seated" once the firmware also says
        the slot carries no filament."""
        assert observe_tray(1, 0, {"id": 0, "state": state, "tray_type": ""}).present is False

    def test_exist_bit_vetoes_a_false_presence(self):
        """003-H2S: a mid-print insert sticks at state 9 while the bitmask reports it.

        The merge pipeline promotes exactly this case 9→10 downstream; pre-merge we
        resolve the contradiction to UNKNOWN — never to EMPTY, because EMPTY is what
        authorizes releasing a binding.
        """
        obs = observe_tray(1, 0, STALE_EMPTY_TRAY, exist_bits=0b0100)  # AMS0 T2 bit set
        assert obs.exist_bit is True
        assert obs.present is None

    def test_exist_bit_clear_leaves_the_empty_shape_empty(self):
        obs = observe_tray(1, 0, STALE_EMPTY_TRAY, exist_bits=0b0000)
        assert obs.exist_bit is False
        assert obs.present is False

    def test_exist_bit_is_none_without_a_bitmask(self):
        assert observe_tray(1, 0, STALE_EMPTY_TRAY).exist_bit is None

    def test_exist_bit_is_none_for_ams_ht(self):
        obs = observe_tray(1, 128, {"id": 0, "state": 9, "tray_type": ""}, exist_bits=0xFFFF)
        assert obs.exist_bit is None
        assert obs.present is False

    def test_exist_bit_never_manufactures_presence(self):
        # Positive evidence only: a set bit does not turn an unknown into a True.
        obs = observe_tray(1, 0, {"id": 0, "state": 3}, exist_bits=0b0001)
        assert obs.exist_bit is True
        assert obs.present is None

    @pytest.mark.parametrize(
        ("tray", "expected"),
        [
            ({"id": 0, "state": 11, "tray_type": "PETG"}, True),
            ({"id": 0, "state": 9, "tray_type": ""}, False),
            ({"id": 0, "remain": 40}, True),
            ({"id": 0, "remain": 0}, False),
            ({"id": 0, "tag_uid": "3CF1F3E700000100"}, True),
            ({"id": 0}, False),
        ],
    )
    def test_occupancy_signal(self, tray, expected):
        assert observe_tray(1, 0, tray).occupancy_signal is expected


# --- observe_ams_push: walking a whole push ---------------------------------


class TestObserveAmsPush:
    def test_full_push_shape(self):
        payload = {
            "ams": [
                {
                    "id": 0,
                    "tray": [
                        {"id": 0, "state": 11, "tray_type": "PETG", "tray_color": "000000FF"},
                        {"id": 1, "state": 10, "tray_type": "PETG"},
                        STALE_EMPTY_TRAY,
                        CHIMERA_PUSH_C,
                    ],
                }
            ],
            "tray_exist_bits": "b",  # 0b1011 → T0,T1,T3 occupied; T2 empty
        }
        obs = observe_ams_push(3, payload)
        assert [o.tray_id for o in obs] == [0, 1, 2, 3]
        assert [o.printer_id for o in obs] == [3, 3, 3, 3]
        assert [o.present for o in obs] == [True, True, False, True]
        assert [o.exist_bit for o in obs] == [True, True, False, True]

    def test_bare_unit_list_payload(self):
        obs = observe_ams_push(1, [{"id": 0, "tray": [{"id": 0, "state": 11, "tray_type": "PLA"}]}])
        assert len(obs) == 1
        assert obs[0].ams_id == 0
        assert obs[0].exist_bit is None  # a bare list carries no bitmask

    def test_nested_ams_payload(self):
        obs = observe_ams_push(1, {"ams": {"ams": [{"id": 1, "tray": [{"id": 2, "state": 11}]}]}})
        assert [(o.ams_id, o.tray_id) for o in obs] == [(1, 2)]

    def test_ams_ht_single_tray_unit(self):
        payload = {"ams": [{"id": 128, "tray": [{"id": 0, "state": 11, "tray_type": "PETG"}]}]}
        obs = observe_ams_push(7, payload)
        assert len(obs) == 1
        assert (obs[0].ams_id, obs[0].tray_id) == (128, 0)
        assert obs[0].present is True

    def test_multiple_units(self):
        payload = {
            "ams": [
                {"id": 0, "tray": [{"id": 0, "state": 11, "tray_type": "PETG"}]},
                {"id": 1, "tray": [{"id": 0, "state": 9, "tray_type": ""}]},
            ]
        }
        obs = observe_ams_push(8, payload)
        assert [(o.ams_id, o.tray_id, o.present) for o in obs] == [(0, 0, True), (1, 0, False)]

    def test_tray_without_id_warns_and_falls_back_to_position(self, caplog):
        """The pre-W1 call sites silently defaulted to 0 — that writes slot 0's
        identity from another slot's data. Position fallback + a WARNING instead."""
        payload = {"ams": [{"id": 0, "tray": [{"state": 11, "tray_type": "PETG"}, {"state": 9, "tray_type": ""}]}]}
        with caplog.at_level(logging.WARNING):
            obs = observe_ams_push(2, payload)
        assert [o.tray_id for o in obs] == [0, 1]
        warnings = [r for r in caplog.records if "no usable 'id'" in r.getMessage()]
        assert len(warnings) == 2

    def test_unit_without_id_warns_and_falls_back_to_position(self, caplog):
        payload = {"ams": [{"tray": [{"id": 0, "state": 11}]}]}
        with caplog.at_level(logging.WARNING):
            obs = observe_ams_push(2, payload)
        assert obs[0].ams_id == 0
        assert any("AMS unit has no usable 'id'" in r.getMessage() for r in caplog.records)

    def test_no_warning_when_ids_are_present(self, caplog):
        payload = {"ams": [{"id": 0, "tray": [{"id": 0, "state": 11}]}]}
        with caplog.at_level(logging.WARNING):
            observe_ams_push(2, payload)
        assert not [r for r in caplog.records if "no usable 'id'" in r.getMessage()]

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"ams": None},
            {"ams": []},
            [],
            None,
            "garbage",
            {"ams": [{"id": 0}]},  # unit without a tray list
            {"ams": [{"id": 0, "tray": "not-a-list"}]},
            {"ams": ["not-a-dict"]},
            {"ams": [{"id": 0, "tray": ["not-a-dict", None]}]},
        ],
    )
    def test_malformed_payloads_yield_no_observations(self, payload):
        assert observe_ams_push(1, payload) == []

    def test_string_ids_are_parsed(self):
        payload = {"ams": [{"id": "1", "tray": [{"id": "2", "state": "11"}]}]}
        obs = observe_ams_push(1, payload)
        assert (obs[0].ams_id, obs[0].tray_id, obs[0].state) == (1, 2, 11)

    def test_silence_about_a_slot_is_not_an_observation_of_it(self):
        """A partial push carrying one unit yields observations ONLY for that unit."""
        payload = {"ams": [{"id": 1, "tray": [{"id": 0, "state": 11, "tray_type": "PETG"}]}]}
        obs = observe_ams_push(1, payload)
        assert [(o.ams_id, o.tray_id) for o in obs] == [(1, 0)]


# --- object contract --------------------------------------------------------


class TestObservationObject:
    def test_is_frozen(self):
        obs = observe_tray(1, 0, CHIMERA_PUSH_A)
        with pytest.raises(FrozenInstanceError):
            obs.tag_uid = "X"  # type: ignore[misc]

    def test_does_not_alias_the_wire_dict(self):
        """The merge MUTATES the raw tray dicts right after we observe them."""
        tray = dict(CHIMERA_PUSH_A)
        obs = observe_tray(1, 0, tray)
        tray["tray_type"] = ""
        tray["tag_uid"] = "0000000000000000"
        assert obs.tray_type == "PETG"
        assert obs.tag_uid == "EC96F1E700000100"

    def test_slot_key(self):
        assert observe_tray(4, 1, {"id": 2, "state": 11}).slot == (4, 1, 2)

    def test_is_a_dataclass_with_the_documented_fields(self):
        obs = observe_tray(1, 0, CHIMERA_PUSH_A)
        assert isinstance(obs, TrayObservation)
        for field in (
            "printer_id",
            "ams_id",
            "tray_id",
            "state",
            "present",
            "identity_asserted",
            "tag_uid",
            "tray_uuid",
            "config_asserted",
            "tray_type",
            "tray_color",
            "tray_info_idx",
            "tray_sub_brands",
            "remain",
            "nozzle_temp_min",
            "nozzle_temp_max",
            "exist_bit",
        ):
            assert hasattr(obs, field)
