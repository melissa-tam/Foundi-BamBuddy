"""Tests for main._hms_should_notify_severity — the HMS notification gate.

Bambu severity: 1=fatal, 2=serious, 3=common, 4=info. Under the corrected
decode, notifications must fire for fatal..common and drop only info(4). The
legacy gate (``severity >= 2`` over the OLD wrong decode where fatal read as 1)
silently dropped genuine fatal faults — this is the corrected predicate.
"""

from backend.app.main import _hms_should_notify_severity


def test_fatal_now_notifies():
    assert _hms_should_notify_severity(1) is True


def test_serious_notifies():
    assert _hms_should_notify_severity(2) is True


def test_common_notifies():
    assert _hms_should_notify_severity(3) is True


def test_info_does_not_notify():
    assert _hms_should_notify_severity(4) is False
