"""Settings plumbing for the latency Phase A dispatch tunables (schema layer)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.app.schemas.settings import AppSettings, AppSettingsUpdate


class TestDefaults:
    def test_new_field_defaults(self):
        s = AppSettings()
        assert s.queue_check_interval_seconds == 30
        assert s.dispatch_kick_debounce_seconds == 1.0
        assert s.usb_preflight_fresh_window_seconds == 10
        assert s.usb_preflight_max_wait_seconds == 2.5


class TestUpdateTwins:
    @pytest.mark.parametrize(
        "field,value",
        [
            ("queue_check_interval_seconds", 45),
            ("dispatch_kick_debounce_seconds", 2.0),
            ("usb_preflight_fresh_window_seconds", 30),
            ("usb_preflight_max_wait_seconds", 5.0),
        ],
    )
    def test_accepts_in_range(self, field, value):
        assert getattr(AppSettingsUpdate(**{field: value}), field) == value

    @pytest.mark.parametrize(
        "field",
        [
            "queue_check_interval_seconds",
            "dispatch_kick_debounce_seconds",
            "usb_preflight_fresh_window_seconds",
            "usb_preflight_max_wait_seconds",
        ],
    )
    def test_accepts_none(self, field):
        assert getattr(AppSettingsUpdate(**{field: None}), field) is None

    @pytest.mark.parametrize(
        "field,value",
        [
            ("queue_check_interval_seconds", 4),  # < 5
            ("queue_check_interval_seconds", 301),  # > 300
            ("dispatch_kick_debounce_seconds", 0.1),  # < 0.2
            ("dispatch_kick_debounce_seconds", 11),  # > 10
            ("usb_preflight_fresh_window_seconds", -1),  # < 0
            ("usb_preflight_fresh_window_seconds", 121),  # > 120
            ("usb_preflight_max_wait_seconds", -0.1),  # < 0
            ("usb_preflight_max_wait_seconds", 10.5),  # > 10
        ],
    )
    def test_rejects_out_of_range(self, field, value):
        with pytest.raises(ValidationError):
            AppSettingsUpdate(**{field: value})
