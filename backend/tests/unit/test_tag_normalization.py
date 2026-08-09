"""Unit pins for ``utils.tag_normalization.tag_matches_row`` — THE tag comparison.

A Bambu roll carries TWO RFID chips, one per flange, sharing a single ``tray_uuid``,
and the AMS reads whichever side faces its antenna. So a row's tag identity is a PAIR
and ``scanned != row.tag_uid`` is not evidence of a different roll. Every "same roll?"
tag question in the fork goes through this one function (the slot-state decision table,
the pipeline's owner lookup, and the tag matcher's resolution lane) so the answer cannot
diverge between them.
"""

from backend.app.utils.tag_normalization import tag_matches_row

NEAR = "EC96F1E700000100"
FAR = "3CF1F3E700000100"
OTHER = "A5E7210D00000100"


def test_the_near_chip_matches():
    assert tag_matches_row(NEAR, NEAR, FAR) is True


def test_the_far_chip_matches_too():
    """The whole point: the roll's other side is an identification, not a mismatch."""
    assert tag_matches_row(FAR, NEAR, FAR) is True


def test_an_unrelated_chip_does_not_match():
    assert tag_matches_row(OTHER, NEAR, FAR) is False


def test_an_unrecorded_sibling_does_not_match():
    """Before the pair is recorded the far chip is genuinely unknown — answering "same"
    on faith is how a swap gets absorbed as a sibling read."""
    assert tag_matches_row(FAR, NEAR, None) is False


def test_comparison_is_case_and_whitespace_insensitive():
    assert tag_matches_row(f"  {FAR.lower()} ", NEAR, FAR) is True


def test_an_empty_scan_never_matches():
    """ "No tag was read" is not an identification."""
    assert tag_matches_row(None, NEAR, FAR) is False
    assert tag_matches_row("", NEAR, FAR) is False
    assert tag_matches_row("   ", NEAR, FAR) is False


def test_an_empty_stored_side_is_not_a_wildcard():
    """A tagless row must not swallow every scan that comes past it."""
    assert tag_matches_row(NEAR, None, None) is False
    assert tag_matches_row(NEAR, "", "") is False


def test_non_hex_identifiers_are_not_collapsed():
    """Deliberately NOT normalize_hex: stripping non-hex characters would make two
    distinct identifiers compare equal."""
    assert tag_matches_row("TAG-X", "TAG-Y", None) is False
