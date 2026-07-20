"""Tests for HMS error code translations."""

from types import SimpleNamespace

from backend.app.services.hms_errors import (
    HMS_ERROR_DESCRIPTIONS,
    HMS_WIKI_URL,
    get_error_description,
    hms_error_payload,
    hms_severity,
    hms_short_code,
    lookup_description_any,
    runout_slot_from_hms,
)


class TestHMSErrorDescriptions:
    """Tests for the HMS error descriptions dictionary."""

    def test_dictionary_is_not_empty(self):
        """Verify the error descriptions dictionary has entries."""
        assert len(HMS_ERROR_DESCRIPTIONS) > 0

    def test_dictionary_has_expected_count(self):
        """Verify we have the expected number of error codes."""
        # Should have 853 error codes from the frontend
        assert len(HMS_ERROR_DESCRIPTIONS) == 853

    def test_all_keys_are_valid_format(self):
        """Verify all keys follow the XXXX_YYYY format."""
        import re

        pattern = re.compile(r"^[0-9A-F]{4}_[0-9A-F]{4}$")
        for code in HMS_ERROR_DESCRIPTIONS:
            assert pattern.match(code), f"Invalid error code format: {code}"

    def test_all_values_are_non_empty_strings(self):
        """Verify all descriptions are non-empty strings."""
        for code, description in HMS_ERROR_DESCRIPTIONS.items():
            assert isinstance(description, str), f"Description for {code} is not a string"
            assert len(description) > 0, f"Description for {code} is empty"


class TestGetErrorDescription:
    """Tests for the get_error_description function."""

    def test_returns_description_for_known_code(self):
        """Verify known error codes return their descriptions."""
        # 0300_400C = "The task was canceled."
        result = get_error_description("0300_400C")
        assert result == "The task was canceled."

    def test_returns_description_for_ams_error(self):
        """Verify AMS error codes return their descriptions."""
        # 0700_8010 = AMS assist motor overloaded
        result = get_error_description("0700_8010")
        assert "AMS assist motor" in result

    def test_returns_none_for_unknown_code(self):
        """Verify unknown error codes return None."""
        result = get_error_description("XXXX_YYYY")
        assert result is None

    def test_handles_lowercase_input(self):
        """Verify function handles lowercase input."""
        result = get_error_description("0300_400c")
        assert result == "The task was canceled."

    def test_handles_mixed_case_input(self):
        """Verify function handles mixed case input."""
        result = get_error_description("0300_400C")
        assert result == "The task was canceled."

    def test_common_error_codes_have_descriptions(self):
        """Verify common error codes have descriptions."""
        common_codes = [
            "0300_4000",  # Z axis homing failed
            "0300_4006",  # Nozzle clogged
            "0300_8004",  # Filament ran out
            "0500_4001",  # Failed to connect to Bambu Cloud
            "0700_8010",  # AMS assist motor overloaded
        ]
        for code in common_codes:
            result = get_error_description(code)
            assert result is not None, f"Missing description for common code: {code}"


class TestHmsShortCode:
    """Tests for hms_short_code — canonical MMMM_CCCC across both wire shapes."""

    def test_hms_array_shape_int_code(self):
        """hms[] faults arrive with attr/code as ints (code pre-masked)."""
        # attr carries module in bits 16-31; code is the raw error number.
        assert hms_short_code(0x03008000, 0x400C) == "0300_400C"

    def test_print_error_shape_hex_string_code(self):
        """print_error faults store attr=full 32-bit value, code="0x{low16}"."""
        assert hms_short_code(0x05008061, "0x8061") == "0500_8061"

    def test_hex_string_without_prefix(self):
        """A bare hex string (no 0x prefix) parses the same."""
        assert hms_short_code(0x0300_0000, "400C") == "0300_400C"

    def test_zero_and_empty_inputs(self):
        """Falsy attr/code degrade to the 0000_0000 code, never raise."""
        assert hms_short_code(0, 0) == "0000_0000"
        assert hms_short_code(0, "") == "0000_0000"

    def test_masks_to_low_16_bits(self):
        """Only the low 16 bits of the code survive into the short code."""
        assert hms_short_code(0x0700_0000, 0x1_8011) == "0700_8011"


def _fake_hms_error(**overrides):
    """Minimal stand-in matching the HMSError attribute surface."""
    base = {
        "code": "0x400C",
        "attr": 0x03000000,
        "module": 3,
        "severity": 2,
        "actions": [],
        "job_id": None,
        "full_code": "030000000000400C",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestHmsErrorPayload:
    """Tests for hms_error_payload — the single REST/WS serialization site."""

    _EXPECTED_KEYS = {
        "code",
        "attr",
        "module",
        "severity",
        "actions",
        "job_id",
        "full_code",
        "short_code",
        "description",
        "wiki_url",
    }

    def test_all_ten_keys_present(self):
        payload = hms_error_payload(_fake_hms_error())
        assert set(payload.keys()) == self._EXPECTED_KEYS

    def test_known_code_resolves_description(self):
        # 0300_400C = "The task was canceled."
        payload = hms_error_payload(_fake_hms_error())
        assert payload["short_code"] == "0300_400C"
        assert payload["description"] == "The task was canceled."
        assert payload["wiki_url"] == HMS_WIKI_URL

    def test_unknown_code_description_is_none(self):
        payload = hms_error_payload(_fake_hms_error(code="0xFFFF", attr=0xFFFF0000))
        assert payload["short_code"] == "FFFF_FFFF"
        assert payload["description"] is None
        # An unknown code still carries a wiki link and preserves the raw fields.
        assert payload["wiki_url"] == HMS_WIKI_URL

    def test_raw_fields_passed_through(self):
        err = _fake_hms_error(actions=["RESUME_PRINTING"], job_id="task-7")
        payload = hms_error_payload(err)
        assert payload["code"] == "0x400C"
        assert payload["attr"] == 0x03000000
        assert payload["module"] == 3
        assert payload["severity"] == 2
        assert payload["actions"] == ["RESUME_PRINTING"]
        assert payload["job_id"] == "task-7"
        assert payload["full_code"] == "030000000000400C"

    def test_print_error_shape_round_trips(self):
        """A print_error-derived HMSError (severity 3) serializes correctly."""
        err = _fake_hms_error(code="0x8061", attr=0x05008061, module=5, severity=3, full_code="05008061")
        payload = hms_error_payload(err)
        assert payload["short_code"] == "0500_8061"
        assert payload["severity"] == 3
        assert payload["description"] == "No print plate detected. Please make sure it is placed correctly."


class TestHmsSeverity:
    """hms_severity decodes the high 16 bits of the error `code` word.

    1=fatal, 2=serious, 3=common, 4=info; anything else degrades to 2 (serious)
    so an unrecognised value never silences a fault. Replaces the legacy
    ``(attr >> 8) & 0xF`` decode which read every real fault as fatal(1)."""

    def test_fatal(self):
        assert hms_severity(0x00010000) == 1

    def test_serious(self):
        assert hms_severity(0x00020000) == 2

    def test_common_live_microsd_code(self):
        # The live-fleet MicroSD fault code — must decode to 3, not 1.
        assert hms_severity(0x00030004) == 3

    def test_info(self):
        assert hms_severity(0x00040000) == 4

    def test_zero_degrades_to_serious(self):
        assert hms_severity(0) == 2

    def test_out_of_range_degrades_to_serious(self):
        assert hms_severity(0x00090000) == 2

    def test_hex_string_input(self):
        assert hms_severity("0x00030004") == 3
        assert hms_severity("30004") == 3
        assert hms_severity("") == 2


class TestLookupDescriptionAny:
    """Full-code (vendored catalog) first, then legacy 2-group table."""

    def test_full_code_hit(self):
        # attr 0x05000100 + code 0x00030004 → ecode 0500010000030004 (MicroSD).
        result = lookup_description_any(0x05000100, 0x00030004)
        assert result is not None
        assert "Not enough space" in result

    def test_falls_back_to_two_group(self):
        # 030000000000400C isn't a real ecode, but 0300_400C is in the legacy table.
        assert lookup_description_any(0x03000000, 0x400C) == "The task was canceled."

    def test_none_when_both_miss(self):
        assert lookup_description_any(0xFFFF0000, 0xFFFF) is None

    def test_hex_string_code(self):
        assert lookup_description_any(0x05000100, "0x00030004") is not None


class TestHmsErrorPayloadCatalog:
    """hms_error_payload: full-code description precedence + wiki deep link."""

    def test_full_code_description_wins_over_two_group(self):
        # short_code 0500_0004 is NOT in the legacy table, but the full ecode
        # 0500010000030004 IS in the vendored catalog — full-code must win.
        err = _fake_hms_error(code="0x30004", attr=0x05000100, module=5, severity=3, full_code="0500010000030004")
        payload = hms_error_payload(err)
        assert payload["short_code"] == "0500_0004"
        assert get_error_description("0500_0004") is None
        assert "Not enough space" in payload["description"]

    def test_falls_back_to_two_group_description(self):
        err = _fake_hms_error(code="0x400C", attr=0x03000000, full_code="030000000000400C")
        payload = hms_error_payload(err)
        assert payload["description"] == "The task was canceled."

    def test_description_none_when_both_miss(self):
        err = _fake_hms_error(code="0xFFFF", attr=0xFFFF0000, full_code="FFFF00000000FFFF")
        payload = hms_error_payload(err)
        assert payload["description"] is None

    def test_wiki_deep_link_for_known_code(self):
        err = _fake_hms_error(code="0x30004", attr=0x05000100, module=5, severity=3, full_code="0500010000030004")
        payload = hms_error_payload(err)
        assert payload["wiki_url"].startswith("https://wiki.bambulab.com/en/")
        assert "/hmscode/" in payload["wiki_url"]

    def test_wiki_falls_back_to_landing_page(self):
        err = _fake_hms_error(code="0xFFFF", attr=0xFFFF0000, full_code="FFFF00000000FFFF")
        payload = hms_error_payload(err)
        assert payload["wiki_url"] == HMS_WIKI_URL


class TestRunoutSlotFromHms:
    """Pure decode of the 0700_2X00 per-slot runout family (attr → AMS+slot).

    Probe-verified against the two live 2026-07-19 incident faults and real
    catalog ecodes; the slot-agnostic 8011 runout and every non-runout code
    fail closed so the caller falls back to tray_now/mapping inference."""

    def test_live_incident_vector_slot1(self):
        # 117448704 = 0x07002000, code 0x00020001 → AMS0 slot0 ("AMS A Slot 1").
        assert runout_slot_from_hms(117448704, 0x00020001) == (0, 0)

    def test_live_incident_vector_slot3(self):
        # 117449216 = 0x07002200, code 0x00020001 → AMS0 slot2 ("AMS A Slot 3").
        assert runout_slot_from_hms(117449216, 0x00020001) == (0, 2)

    def test_catalog_ecode_ams_a_slot2_purge_variant(self):
        # 0700210000020005 "AMS A Slot 2 filament has run out, and purging …".
        assert runout_slot_from_hms(0x07002100, 0x00020005) == (0, 1)

    def test_catalog_ecode_ams_b_slot1_wait_variant(self):
        # 0701200000030001 "AMS B Slot 1 filament has run out. Please wait …".
        assert runout_slot_from_hms(0x07012000, 0x00030001) == (1, 0)

    def test_catalog_ecode_ams_b_slot4_autoswitch_variant(self):
        # 0701230000030002 "AMS B Slot 4 filament has run out and automatically switched".
        assert runout_slot_from_hms(0x07012300, 0x00030002) == (1, 3)

    def test_catalog_ecode_ams_c_slot3(self):
        # 0702220000020001 "AMS C Slot 3 …".
        assert runout_slot_from_hms(0x07022200, 0x00020001) == (2, 2)

    def test_wrong_module_byte_rejected(self):
        # Same slot layout but module byte 0x03 (motion), not 0x07 (AMS).
        assert runout_slot_from_hms(0x03002000, 0x00020001) is None

    def test_slot_byte_out_of_range_rejected(self):
        # slot byte 0x24 is one past the last valid slot (0x20..0x23).
        assert runout_slot_from_hms(0x07002400, 0x00020001) is None

    def test_non_runout_code32_rejected(self):
        # 0x8011 is the slot-AGNOSTIC "insert into the same slot" runout — no slot.
        assert runout_slot_from_hms(0x07002000, 0x00008011) is None

    def test_arbitrary_non_runout_code_rejected(self):
        assert runout_slot_from_hms(0x07002000, 0x0000400C) is None

    def test_read_failure_on_a_slot_attr_is_not_a_runout(self):
        # Same slot-carrying attr, but 0x00010081 is "failed to read the filament
        # information" — a tag-read failure, NOT an empty spool. Fails closed so no
        # caller can mistake a dead RFID read for a runout.
        assert runout_slot_from_hms(0x07002000, 0x00010081) is None

    def test_feed_fault_8010_family_is_not_slot_decoded(self):
        # The live 009-H2S 2026-07-20 tangle: attr 0x07008210 / code 0x8010. The
        # 8010 family carries no slot attribution, so jam attribution falls back to
        # tray_now / the item's ams_mapping (see spool_recovery._resolve_jammed_tray).
        assert runout_slot_from_hms(0x07008210, 0x00008010) is None


class TestLiveCapturedAttrDecodes:
    """Pins for the shared attr-layout decoders against LIVE-captured 2026-07-19/20
    fleet values, in the shape the current API exposes them (``ams_slot_from_attr``
    + the per-family predicates).

    Note the off-by-one that makes these easy to misread: the firmware's own text is
    1-indexed ("AMS A Slot 1") while the attr's slot nibble is 0-indexed
    (``0x20 + tray``). Every decoder here returns the 0-indexed ``tray_id``.
    """

    def test_slot_nibble_is_zero_indexed_across_the_family(self):
        from backend.app.services.hms_errors import ams_slot_from_attr

        # 0x07002000 / 2100 / 2200 = firmware "AMS A Slot 1 / 2 / 3".
        assert ams_slot_from_attr(0x07002000) == (0, 0)
        assert ams_slot_from_attr(0x07002100) == (0, 1)
        assert ams_slot_from_attr(0x07002200) == (0, 2)

    def test_runout_code_word_decodes_the_same_three_slots(self):
        assert runout_slot_from_hms(0x07002000, 0x00020001) == (0, 0)
        assert runout_slot_from_hms(0x07002100, 0x00020001) == (0, 1)
        assert runout_slot_from_hms(0x07002200, 0x00020001) == (0, 2)

    def test_decimal_forms_from_the_incident_log(self):
        # The two attrs as they appear in the raw MQTT payloads.
        assert runout_slot_from_hms(117448704, 0x00020001) == (0, 0)  # 0x07002000
        assert runout_slot_from_hms(117449216, 0x00020001) == (0, 2)  # 0x07002200

    def test_read_failure_is_classified_and_slot_decoded(self):
        from backend.app.services.hms_errors import filament_read_failure_slot, is_filament_read_failure

        # 0700_2X00_0001_0081 — "Failed to read the filament information from AMS A
        # slot 1. The AMS main board may be malfunctioning."
        assert is_filament_read_failure(0x07002000, 0x00010081) is True
        assert filament_read_failure_slot(0x07002000, 0x00010081) == (0, 0)

    def test_unit_scoped_read_failure_names_no_slot(self):
        from backend.app.services.hms_errors import (
            ams_unit_from_attr,
            filament_read_failure_slot,
            is_filament_read_failure,
        )

        # 07XX_4025 names the AMS unit but no slot.
        assert is_filament_read_failure(0x07010000, 0x00004025) is True
        assert filament_read_failure_slot(0x07010000, 0x00004025) is None
        assert ams_unit_from_attr(0x07010000) == 1

    def test_feed_fault_is_neither_a_runout_nor_a_read_failure(self):
        from backend.app.services.hms_errors import ams_unit_from_attr, is_filament_read_failure

        assert runout_slot_from_hms(0x07008210, 0x00008010) is None
        assert is_filament_read_failure(0x07008210, 0x00008010) is False
        assert ams_unit_from_attr(0x07008210) == 0  # the unit is still attributable


class TestHmsErrorPayloadRunoutSlot:
    """hms_error_payload adds `runout_slot` ONLY for a per-slot runout fault."""

    def test_runout_fault_carries_slot(self):
        err = _fake_hms_error(code="0x20001", attr=0x07002200, module=7, severity=2, full_code="0700220000020001")
        payload = hms_error_payload(err)
        assert payload["runout_slot"] == {"ams_id": 0, "tray_id": 2}

    def test_non_runout_fault_omits_slot(self):
        # The default fake (0300_400C) is not a runout → no extra key.
        assert "runout_slot" not in hms_error_payload(_fake_hms_error())

    def test_slot_agnostic_8011_omits_slot(self):
        err = _fake_hms_error(code="0x8011", attr=0x07000000, module=7, severity=2, full_code="0700000000008011")
        assert "runout_slot" not in hms_error_payload(err)
