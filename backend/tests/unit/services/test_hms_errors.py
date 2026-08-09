"""Tests for HMS error code translations."""

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

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


# ===========================================================================
# Runout DEMAND decoder + shared runout-hold predicate (006-H2S 2026-07-26)
# ===========================================================================
# The incident's two live hms[] snapshots, in the firmware's own list order and in
# the shape the MQTT parser hands every consumer (HMSError: int attr + hex-string
# code). 01:23 — slot 1 had auto-switched, slot 3 was the standing DEMAND, and the
# bare slot-agnostic 8011 sat alongside. 13:51 — a slot-2 demand had been APPENDED
# (a second roll emptied), which is the move the escalation never told the operator
# about.
_D_SLOT1_AUTOSWITCHED = _fake_hms_error(  # 0700_2000_0003_0002 — INFO, never a demand
    code="0x30002", attr=0x07002000, module=7, severity=3, full_code="0700200000030002"
)
_D_SLOT3_DEMAND = _fake_hms_error(  # 0700_2200_0002_0001 — "Please insert a new filament."
    code="0x20001", attr=0x07002200, module=7, severity=2, full_code="0700220000020001"
)
_D_SLOT2_DEMAND = _fake_hms_error(  # 0700_2100_0002_0001 — the 13:51 arrival
    code="0x20001", attr=0x07002100, module=7, severity=2, full_code="0700210000020001"
)
_D_BARE_8011 = _fake_hms_error(  # slot-agnostic "insert into the SAME AMS slot"
    code="0x8011", attr=0x07000000, module=7, severity=2, full_code="0700000000008011"
)

_INCIDENT_0123 = [_D_SLOT1_AUTOSWITCHED, _D_SLOT3_DEMAND, _D_BARE_8011]
_INCIDENT_1351 = [*_INCIDENT_0123, _D_SLOT2_DEMAND]


class TestCurrentRunoutDemand:
    """Which slot is the firmware asking for RIGHT NOW — the decoder that replaced
    the dispatch-mapping guess that sent the operator to the wrong slot."""

    def test_incident_0123_names_slot_3(self):
        from backend.app.services.hms_errors import current_runout_demand

        # The escalation said "AMS A slot 1" off the mapping; the wire said slot 3.
        assert current_runout_demand(_INCIDENT_0123) == (0, 2)

    def test_incident_1351_appended_demand_wins(self):
        from backend.app.services.hms_errors import current_runout_demand

        # Firmware APPENDS: the newest demand is last, so slot 2 is the live ask.
        assert current_runout_demand(_INCIDENT_1351) == (0, 1)

    def test_empty_list_is_none(self):
        from backend.app.services.hms_errors import current_runout_demand

        assert current_runout_demand([]) is None
        assert current_runout_demand(None) is None

    def test_no_demand_family_member_is_none(self):
        from backend.app.services.hms_errors import current_runout_demand

        # Auto-switched INFO + the slot-agnostic 8011: both name a runout, neither asks.
        assert current_runout_demand([_D_SLOT1_AUTOSWITCHED, _D_BARE_8011]) is None

    def test_auto_switched_never_counts_even_alone(self):
        from backend.app.services.hms_errors import current_runout_demand

        # "…has run out and automatically switched to the slot with the same
        # filament" — the backup already rescued the print; nothing is demanded.
        assert current_runout_demand([_D_SLOT1_AUTOSWITCHED]) is None

    def test_purge_abnormal_variant_is_not_a_demand(self):
        from backend.app.services.hms_errors import current_runout_demand

        # 0x00020005 — "…and purging the old filament went abnormally; please check
        # whether the filament is stuck in the tool head." A tool-head ask, not an
        # insert ask, so the refill assist must not treat it as one.
        purge = _fake_hms_error(code="0x20005", attr=0x07002100, module=7, full_code="0700210000020005")
        assert current_runout_demand([purge]) is None

    def test_please_wait_variant_is_not_a_demand(self):
        from backend.app.services.hms_errors import current_runout_demand

        # 0x00030001 — "…Please wait while old filament is purged." An in-progress
        # notice; the firmware has not asked for anything yet.
        wait = _fake_hms_error(code="0x30001", attr=0x07002000, module=7, severity=3, full_code="0700200000030001")
        assert current_runout_demand([wait]) is None

    def test_demand_still_found_after_a_later_non_demand(self):
        from backend.app.services.hms_errors import current_runout_demand

        # Last DEMAND wins — a later non-demand entry must not blank the answer.
        assert current_runout_demand([_D_SLOT3_DEMAND, _D_BARE_8011, _D_SLOT1_AUTOSWITCHED]) == (0, 2)

    def test_malformed_entry_is_skipped_not_raised(self):
        from backend.app.services.hms_errors import current_runout_demand

        junk = SimpleNamespace(code=object(), attr="not-an-int")
        assert current_runout_demand([junk, _D_SLOT3_DEMAND]) == (0, 2)

    def test_non_ams_module_with_the_demand_code_is_rejected(self):
        from backend.app.services.hms_errors import current_runout_demand

        motion = _fake_hms_error(code="0x20001", attr=0x03002000, module=3, full_code="0300200000020001")
        assert current_runout_demand([motion]) is None


class TestRunoutHoldActive:
    """The shared PAUSE+runout predicate behind the guidance, the refill auto-resume
    and the /ams/load 409 — so they can never disagree about the hold state."""

    def _state(self, gcode_state, hms):
        return SimpleNamespace(state=gcode_state, hms_errors=hms)

    def test_pause_with_bare_8011_is_a_hold(self):
        from backend.app.services.hms_errors import runout_hold_active

        assert runout_hold_active(self._state("PAUSE", [_D_BARE_8011])) is True

    def test_pause_with_a_slot_demand_is_a_hold(self):
        from backend.app.services.hms_errors import runout_hold_active

        assert runout_hold_active(self._state("PAUSE", [_D_SLOT3_DEMAND])) is True

    def test_running_is_never_a_hold(self):
        from backend.app.services.hms_errors import runout_hold_active

        assert runout_hold_active(self._state("RUNNING", _INCIDENT_0123)) is False

    def test_pause_without_runout_codes_is_not_a_hold(self):
        from backend.app.services.hms_errors import runout_hold_active

        door = _fake_hms_error(code="0x8042", attr=0x03000000, module=3, full_code="0300000000008042")
        assert runout_hold_active(self._state("PAUSE", [door])) is False

    def test_missing_state_fails_closed(self):
        from backend.app.services.hms_errors import runout_hold_active

        assert runout_hold_active(None) is False
        assert runout_hold_active(SimpleNamespace()) is False


# ===========================================================================
# AMS fault taxonomy
# ===========================================================================
# The expected tables are written out HERE, independently of the module's own
# data, so a silent edit to the taxonomy fails these pins instead of moving with
# them. Family membership is spelled out the same way the catalog publishes it.

_AMS_UNIT_MODULES = ("0700", "0701", "0702", "0703", "0704", "0705", "0706", "0707")


def _family(modules, suffix):
    """The short codes one fault family spans, as ``{MMMM_CCCC}``."""
    return {f"{module}_{suffix}" for module in modules}


_SWAP_8010_MODULES = (
    *_AMS_UNIT_MODULES,
    "1800",
    "1801",
    "1802",
    "1200",
    "1201",
    "1202",
    "1203",
    "12FF",
)

# The trigger vocabulary the jam-swap machine has always acted on. WS2a keeps it
# byte-identical while the taxonomy already classifies a wider mechanical family.
_EXPECTED_LEGACY_SWAP = _family(_SWAP_8010_MODULES, "8010") | {"0300_801E"}

_EXPECTED_MECHANICAL = (
    _EXPECTED_LEGACY_SWAP
    | _family((*_AMS_UNIT_MODULES, "07FF"), "8005")
    | _family((*_AMS_UNIT_MODULES, "07FF"), "8006")
    | {"0700_8028", "07FF_8028"}
)
_EXPECTED_RUNOUT = _family(_AMS_UNIT_MODULES, "8011") | {"0300_8004"}
_EXPECTED_RUNOUT_EXTERNAL = {"07FF_8011", "18FE_8011", "18FF_8011", "0300_8015"}
_EXPECTED_PHYSICAL = (
    _family((*_AMS_UNIT_MODULES, "07FF"), "8003")
    | _family((*_AMS_UNIT_MODULES, "07FF"), "8004")
    | _family(_AMS_UNIT_MODULES, "8007")
    | {"0700_8013", "0700_8016", "0300_801A", "0300_801C", "0300_8016", "0300_4006"}
    | {"07FF_C011", "07FE_C011", "07FF_C012", "07FE_C012"}
)
_EXPECTED_RFID = _family(_AMS_UNIT_MODULES, "4025")
_EXPECTED_INFORMATIONAL = _family(_AMS_UNIT_MODULES, "0025")
_EXPECTED_EXTRUDER_SIDE = {"0300_801E"}

_EXPECTED_SHORT_CLASSES = {
    "mechanical_feed": _EXPECTED_MECHANICAL,
    "runout": _EXPECTED_RUNOUT,
    "runout_external": _EXPECTED_RUNOUT_EXTERNAL,
    "physical_fault": _EXPECTED_PHYSICAL,
    "rfid_read": _EXPECTED_RFID,
    "informational": _EXPECTED_INFORMATIONAL,
}

# The pre-relocation literal from spool_respool.py, copied verbatim. RUNOUT_HMS_CODES
# must still be exactly this after moving into hms_errors — a relocation that also
# widened the runout vocabulary would silently change what stamps spent_at.
_OLD_RUNOUT_HMS_CODES_LITERAL = frozenset(
    {
        "0300_8004",
        "0700_8011",
        "0701_8011",
        "0702_8011",
        "0703_8011",
        "0704_8011",
        "0705_8011",
        "0706_8011",
        "0707_8011",
    }
)


def _tray_attr(ams_id: int = 0, tray: int = 0) -> int:
    """A slot-attributed AMS attr — the shape ``ams_slot_from_attr`` decodes."""
    return (0x07 << 24) | (ams_id << 16) | ((0x20 + tray) << 8)


def _submodule_attr(submodule_byte: int, ams_id: int = 0) -> int:
    """An AMS attr naming a submodule rather than a tray (motor, RFID reader)."""
    return (0x07 << 24) | (ams_id << 16) | (submodule_byte << 8)


# code word -> (class value, extruder_side), all under TRAY-attributed attrs.
_EXPECTED_TRAY_CODE_WORDS = {
    0x0002000A: ("mechanical_feed", False),
    0x00020010: ("mechanical_feed", False),
    0x00020012: ("mechanical_feed", False),
    0x00020016: ("mechanical_feed", False),
    0x00020017: ("mechanical_feed", False),
    0x00020018: ("mechanical_feed", False),
    0x00020019: ("mechanical_feed", False),
    0x00020020: ("mechanical_feed", False),
    0x00020021: ("mechanical_feed", True),
    0x00020022: ("mechanical_feed", True),
    0x00020026: ("mechanical_feed", False),
    0x00020027: ("mechanical_feed", False),
    0x00020003: ("physical_fault", False),
    0x00020004: ("physical_fault", False),
    0x00020005: ("physical_fault", False),
    0x00020006: ("physical_fault", False),
    0x00020009: ("physical_fault", False),
    0x00020011: ("physical_fault", False),
    0x00020013: ("physical_fault", False),
    0x00020015: ("physical_fault", False),
    0x00020023: ("physical_fault", False),
    0x00020024: ("physical_fault", False),
    0x00010081: ("rfid_read", False),
    0x00010082: ("rfid_read", False),
    0x00010083: ("rfid_read", False),
    0x00010084: ("rfid_read", False),
    0x00010085: ("rfid_read", False),
    0x00010086: ("rfid_read", False),
    0x00020057: ("rfid_read", False),
    0x00020025: ("informational", False),
    0x00030001: ("informational", False),
    0x00030007: ("informational", False),
}


class TestRunoutCodeRelocation:
    """RUNOUT_HMS_CODES moved to hms_errors — one origin, identical membership."""

    def test_membership_is_byte_identical_to_the_pre_move_literal(self):
        from backend.app.services.hms_errors import RUNOUT_HMS_CODES

        assert RUNOUT_HMS_CODES == _OLD_RUNOUT_HMS_CODES_LITERAL

    def test_it_is_the_taxonomy_view_not_a_second_literal(self):
        from backend.app.services.hms_errors import RUNOUT_HMS_CODES, runout_short_codes

        assert RUNOUT_HMS_CODES is runout_short_codes()

    def test_the_external_spool_family_never_leaks_in(self):
        from backend.app.services.hms_errors import (
            RUNOUT_HMS_CODES,
            runout_external_short_codes,
        )

        # An external-spool runout names no AMS slot and has no sibling tray to
        # swap to, so it must never reach a consumer that resolves one.
        assert runout_external_short_codes() == _EXPECTED_RUNOUT_EXTERNAL
        assert not (RUNOUT_HMS_CODES & runout_external_short_codes())

    def test_the_consumers_read_the_relocated_constant(self):
        from backend.app.services import spool_recovery, spool_respool
        from backend.app.services.hms_errors import RUNOUT_HMS_CODES

        assert spool_respool.RUNOUT_HMS_CODES is RUNOUT_HMS_CODES
        assert spool_recovery.RUNOUT_HMS_CODES is RUNOUT_HMS_CODES


class TestAmsFaultTaxonomyShortLane:
    """The print_error lane: every classified short code, and nothing else."""

    @pytest.mark.parametrize(
        ("short", "expected_class"),
        sorted(
            (short, class_value)
            for class_value, shorts in _EXPECTED_SHORT_CLASSES.items()
            for short in shorts
        ),
    )
    def test_each_short_code_classifies(self, short, expected_class):
        from backend.app.services.hms_errors import classify_short_code

        verdict = classify_short_code(short)
        assert verdict is not None, short
        assert verdict.fault_class.value == expected_class
        assert verdict.extruder_side is (short in _EXPECTED_EXTRUDER_SIDE)
        # The short form discarded the attr low byte, so it can never name a slot.
        assert verdict.slot is None

    def test_the_table_holds_no_unpinned_row(self):
        from backend.app.services.hms_errors import _SHORT_CODE_TAXONOMY

        expected = set().union(*_EXPECTED_SHORT_CLASSES.values())
        assert set(_SHORT_CODE_TAXONOMY) == expected

    def test_the_derived_consumer_sets_match(self):
        from backend.app.services.hms_errors import (
            extruder_side_short_codes,
            legacy_swap_short_codes,
            mechanical_feed_short_codes,
            runout_short_codes,
        )

        assert mechanical_feed_short_codes() == _EXPECTED_MECHANICAL
        assert runout_short_codes() == _EXPECTED_RUNOUT
        assert extruder_side_short_codes() == _EXPECTED_EXTRUDER_SIDE
        assert legacy_swap_short_codes() == _EXPECTED_LEGACY_SWAP

    def test_the_send_out_families_are_classified_but_not_swap_triggers(self):
        from backend.app.services.hms_errors import (
            classify_short_code,
            legacy_swap_short_codes,
        )

        # WS2a behavior neutrality: the taxonomy carries the wider classification,
        # the swap machine's trigger marker does not — the consumer wave widens it.
        for short in ("0700_8005", "0700_8006", "0700_8028"):
            assert classify_short_code(short).fault_class.value == "mechanical_feed"
            assert short not in legacy_swap_short_codes()

    def test_an_unclassified_short_code_is_none(self):
        from backend.app.services.hms_errors import classify_short_code

        assert classify_short_code("0500_808C") is None
        assert classify_short_code("0300_400C") is None

    def test_lookup_is_case_insensitive(self):
        from backend.app.services.hms_errors import classify_short_code

        assert classify_short_code("0700_8011").fault_class.value == "runout"
        assert classify_short_code("0700_8011") == classify_short_code("0700_8011".lower())


class TestAmsFaultTaxonomyCodeWordLane:
    """The hms[] lane: attr-scoped code words, with the slot the attr names."""

    @pytest.mark.parametrize(
        ("code_word", "expected_class", "expected_extruder_side"),
        sorted((cw, cls, ext) for cw, (cls, ext) in _EXPECTED_TRAY_CODE_WORDS.items()),
    )
    def test_each_tray_attributed_code_word_classifies(
        self, code_word, expected_class, expected_extruder_side
    ):
        from backend.app.services.hms_errors import classify_ams_fault

        verdict = classify_ams_fault(_tray_attr(ams_id=1, tray=2), code_word)
        assert verdict is not None, hex(code_word)
        assert verdict.fault_class.value == expected_class
        assert verdict.extruder_side is expected_extruder_side
        assert verdict.slot == (1, 2)

    @pytest.mark.parametrize("tray", [0, 1, 2, 3])
    def test_the_slot_comes_from_the_attr(self, tray):
        from backend.app.services.hms_errors import classify_ams_fault

        verdict = classify_ams_fault(_tray_attr(ams_id=0, tray=tray), 0x00020010)
        assert verdict.slot == (0, tray)

    def test_the_table_holds_no_unpinned_code_word(self):
        from backend.app.services.hms_errors import _CODE_WORD_TAXONOMY

        # The tray rows plus the two attr-scoped code words classified only under a
        # submodule attr (0x00020002's motor/RFID meanings, 0x00030003).
        assert set(_CODE_WORD_TAXONOMY) == set(_EXPECTED_TRAY_CODE_WORDS) | {
            0x00020002,
            0x00030003,
        }

    def test_a_non_ams_module_is_never_an_ams_fault(self):
        from backend.app.services.hms_errors import classify_ams_fault

        # 0x00020010 under the motion module is a different fault entirely.
        assert classify_ams_fault(0x03000900, 0x00020010) is None

    def test_an_ams_attr_outside_the_rows_submodule_is_none(self):
        from backend.app.services.hms_errors import classify_ams_fault

        # 0x00020010 was read under TRAY attrs; under the assist-motor submodule the
        # catalog says something else ("assist motor resistance is abnormal").
        assert classify_ams_fault(_submodule_attr(0x01), 0x00020010) is None

    def test_an_unclassified_code_word_is_none(self):
        from backend.app.services.hms_errors import classify_ams_fault

        assert classify_ams_fault(_tray_attr(), 0x00025000) is None

    def test_the_rfid_submodule_row_names_no_slot(self):
        from backend.app.services.hms_errors import classify_ams_fault

        verdict = classify_ams_fault(_submodule_attr(0x30), 0x00030003)
        assert verdict.fault_class.value == "rfid_read"
        assert verdict.slot is None


class TestAmsFaultTaxonomyCollisionPins:
    """The code words a dedicated decoder owns must never classify generically."""

    def test_the_runout_demand_is_not_classified(self):
        from backend.app.services.hms_errors import (
            _RUNOUT_DEMAND_CODE32,
            classify_ams_fault,
            runout_slot_from_hms,
        )

        # Doctrine rule 9: runouts escalate for a same-slot refill, jams swap. The
        # demand stays with current_runout_demand; it is also NOT spent evidence
        # (006-H2S 2026-07-26 latched a bogus demand for a slot that never ran dry).
        assert classify_ams_fault(_tray_attr(tray=2), 0x00020001) is None
        assert 0x00020001 in _RUNOUT_DEMAND_CODE32
        assert runout_slot_from_hms(_tray_attr(tray=2), 0x00020001) == (0, 2)

    def test_the_auto_switch_spent_evidence_is_not_classified(self):
        from backend.app.services.hms_errors import (
            _RUNOUT_AUTO_SWITCH_SPENT_CODE32,
            classify_ams_fault,
        )

        assert classify_ams_fault(_tray_attr(), 0x00030002) is None
        assert 0x00030002 in _RUNOUT_AUTO_SWITCH_SPENT_CODE32

    def test_short_0700_0001_is_banned(self):
        from backend.app.services.hms_errors import classify_short_code

        # The same low-16 word rides the slot-attributed runout attr family, so a
        # short-code match would route runouts into the jam-swap machine.
        for unit in _AMS_UNIT_MODULES:
            assert classify_short_code(f"{unit}_0001") is None

    def test_0x00020002_splits_three_ways_by_submodule(self):
        from backend.app.services.hms_errors import classify_ams_fault

        # Tray attr: "AMS A Slot 1 is empty; please insert a new filament." An
        # empty-slot ASK — hazard-identical to the banned demand, so it stays with
        # the generic notify lane.
        assert classify_ams_fault(_tray_attr(), 0x00020002) is None
        # Motor attrs: "The AMS A slot 1 motor is overloaded…" — the 16-hex twin of
        # the 8010 swap trigger.
        for submodule in (0x01, 0x10, 0x11, 0x12, 0x13):
            verdict = classify_ams_fault(_submodule_attr(submodule), 0x00020002)
            assert verdict.fault_class.value == "mechanical_feed"
        # RFID attrs: "The RFID-tag on AMS A Slot1 is damaged…"
        verdict = classify_ams_fault(_submodule_attr(0x30), 0x00020002)
        assert verdict.fault_class.value == "rfid_read"

    def test_the_feed_resistance_precursor_stays_informational(self):
        from backend.app.services.hms_errors import (
            classify_ams_fault,
            classify_short_code,
            mechanical_feed_short_codes,
        )

        # Observed ~5 min before an 8010 once; one incident is not a lead-time proof
        # and the 8010 always follows, so acting on it only widens the surface.
        assert classify_ams_fault(_tray_attr(), 0x00020025).fault_class.value == "informational"
        assert classify_short_code("0700_0025").fault_class.value == "informational"
        assert "0700_0025" not in mechanical_feed_short_codes()


class TestHmsErrorsImportGraph:
    """hms_errors is a leaf of the spool stack — the cycle that forced the old
    call-time import is gone with the constant it worked around."""

    def test_it_imports_nothing_from_the_spool_services(self):
        import backend.app.services.hms_errors as hms_errors_module

        source = Path(inspect.getfile(hms_errors_module)).read_text(encoding="utf-8")
        imported: list[str] = []
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
            elif isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)

        # ast.walk reaches function bodies too, so a re-introduced call-time import
        # fails this just as a module-level one would.
        assert not [m for m in imported if "spool" in m], imported

    def test_it_imports_cleanly_before_any_spool_module(self):
        import subprocess
        import sys

        import backend.app.services.hms_errors as hms_errors_module

        # A fresh interpreter: importing hms_errors alone must not drag in the spool
        # stack transitively either (which would mean the cycle is merely deferred).
        repo_root = Path(inspect.getfile(hms_errors_module)).parents[3]
        probe = (
            "import sys; import backend.app.services.hms_errors; "
            "print([m for m in sys.modules if 'spool' in m])"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=True,
            cwd=repo_root,
        )
        assert result.stdout.strip() == "[]", result.stdout
