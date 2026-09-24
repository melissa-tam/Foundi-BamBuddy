"""Tests for hms_errors.format_hms_error_summary — the helper that turns an MQTT
hms_errors payload into human-readable fault text.

It moved out of ``main`` on 2026-09-17: ``farm_policy`` names the codes in the "the
printer rejected the eject file" page and a service may never import the monolith.
Two consumers now, one implementation — ``main.on_print_complete``'s
``PrintQueueItem.error_message`` on a pre-print failure (#1111) and that page."""


def _format(hms_errors):
    from backend.app.services.hms_errors import format_hms_error_summary

    return format_hms_error_summary(hms_errors)


def test_returns_none_for_empty_list():
    assert _format([]) is None
    assert _format(None) is None  # the terminal payload's key can be absent


def test_formats_known_nozzle_mismatch_code():
    """0500_4038 is the nozzle-size-mismatch code from the HMS table — the common
    trigger for issue #1111."""
    summary = _format([{"code": "0x4038", "attr": 0x05000000, "module": 0x5, "severity": 1}])
    assert summary is not None
    assert "0500_4038" in summary
    assert "nozzle diameter" in summary.lower()


def test_formats_unknown_code_as_bare_short_code():
    summary = _format([{"code": "0x9999", "attr": 0x99990000, "module": 0x99, "severity": 1}])
    assert summary == "[9999_9999]"


def test_joins_multiple_errors_with_semicolons():
    summary = _format(
        [
            {"code": "0x4038", "attr": 0x05000000, "module": 0x5, "severity": 1},
            {"code": "0x9999", "attr": 0x99990000, "module": 0x99, "severity": 1},
        ]
    )
    assert summary is not None
    assert "; " in summary
    assert summary.count("[") == 2


def test_tolerates_malformed_entry_and_skips_it():
    summary = _format(
        [
            {"code": "not-hex", "attr": "also-not-int"},
            {"code": "0x4038", "attr": 0x05000000, "module": 0x5, "severity": 1},
        ]
    )
    assert summary is not None
    assert "0500_4038" in summary


def test_all_malformed_returns_none():
    assert _format([{"code": "not-hex", "attr": "also-not-int"}]) is None


def test_full_code_wins_over_two_group_lookup():
    """The live MicroSD fault: short_code 0500_0004 is NOT in the legacy table,
    but the full ecode 0500010000030004 IS in the vendored catalog. The summary
    must resolve text via the full code while keeping the [MMMM_CCCC] shape."""
    summary = _format([{"code": "0x30004", "attr": 0x05000100, "module": 0x5, "severity": 3}])
    assert summary is not None
    assert summary.startswith("[0500_0004]")
    assert "Not enough space" in summary


# --- the recorded printer words (2026-09-24) ---------------------------------------
#
# A hold keeps the printer's own words after the printer stops showing them. They are
# stored as firmware FULL codes and rendered through the same lookup a live payload uses.


def test_a_16_hex_full_code_renders_the_catalog_words():
    from backend.app.services.hms_errors import printer_message_from_full_code

    message = printer_message_from_full_code("0500080C0000808C")
    assert message.short_code == "0500_808C"
    assert message.description.startswith("Detected build plate offset")


def test_an_8_hex_print_error_code_renders_the_same_short_code():
    from backend.app.services.hms_errors import printer_message_from_full_code

    assert printer_message_from_full_code("0500808C").short_code == "0500_808C"


def test_an_undecodable_full_code_is_not_a_message():
    from backend.app.services.hms_errors import printer_message_from_full_code

    assert printer_message_from_full_code("ZZZ") is None
    assert printer_message_from_full_code("") is None


def test_a_kind_token_is_not_something_the_printer_said():
    """A lost-Z hold stores ``code='power_loss'`` — the fallback must refuse it."""
    from backend.app.services.hms_errors import printer_message_from_short_code

    assert printer_message_from_short_code("power_loss") is None
    assert printer_message_from_short_code("0500_808C").short_code == "0500_808C"


def test_recorded_codes_fall_back_to_the_short_codes_and_read_once():
    from backend.app.services.hms_errors import messages_from_full_codes

    assert [m.short_code for m in messages_from_full_codes([], fallback_short_codes=["0500_808C"])] == ["0500_808C"]
    twice = messages_from_full_codes(["0500808C", "0500080C0000808C"])
    assert [m.short_code for m in twice] == ["0500_808C"]


def test_full_codes_are_read_off_the_live_entries_the_lane_acted_on():
    from backend.app.services.bambu_mqtt import HMSError
    from backend.app.services.hms_errors import full_codes_of

    vision = HMSError(code="0x808c", attr=0x0500808C, module=5, severity=3, full_code="0500808C")
    other = HMSError(code="0x8011", attr=0x07FF0000, module=7, severity=2, full_code="07FF000000008011")

    assert full_codes_of([vision, other], {"0500_808C"}) == ("0500808C",)
    assert full_codes_of([vision], set()) == ()


def test_an_int_code_field_renders_like_a_hex_string():
    """The MQTT parser sometimes leaves ``code`` as an int (the failure-category path)."""
    from backend.app.services.hms_errors import messages_from_payload

    as_int = messages_from_payload([{"code": 0x4057, "attr": 0x0300_0000}])
    as_str = messages_from_payload([{"code": "0x4057", "attr": 0x0300_0000}])
    assert as_int == as_str
    assert as_int[0].short_code == "0300_4057"
