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
        err = _fake_hms_error(
            code="0x30004", attr=0x05000100, module=5, severity=3, full_code="0500010000030004"
        )
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
        err = _fake_hms_error(
            code="0x30004", attr=0x05000100, module=5, severity=3, full_code="0500010000030004"
        )
        payload = hms_error_payload(err)
        assert payload["wiki_url"].startswith("https://wiki.bambulab.com/en/")
        assert "/hmscode/" in payload["wiki_url"]

    def test_wiki_falls_back_to_landing_page(self):
        err = _fake_hms_error(code="0xFFFF", attr=0xFFFF0000, full_code="FFFF00000000FFFF")
        payload = hms_error_payload(err)
        assert payload["wiki_url"] == HMS_WIKI_URL
