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
