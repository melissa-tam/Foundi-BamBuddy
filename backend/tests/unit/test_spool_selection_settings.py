"""Unit tests for the WI-5 spool-selection settings (schema layer).

Covers the new ``AppSettings`` defaults, the ``spool_selection_policy`` /
``tagless_default_filament`` validators on ``AppSettingsUpdate``, and confirms
the retired ``prefer_lowest_filament`` field is gone from both schemas.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from backend.app.schemas.settings import (
    AppSettings,
    AppSettingsUpdate,
    TaglessDefaultFilament,
)


class TestDefaults:
    def test_new_field_defaults(self):
        s = AppSettings()
        assert s.spool_selection_policy == "first_loaded"
        assert s.min_start_spool_g == 120
        assert s.auto_add_untagged is True

    def test_tagless_default_filament_default_shape(self):
        s = AppSettings()
        parsed = json.loads(s.tagless_default_filament)
        assert parsed["brand"] == "Bambu Lab"
        assert parsed["material"] == "PETG"
        assert parsed["subtype"] == "HF"
        assert parsed["rgba"] == "000000FF"
        # Round-trips through the typed sub-model.
        assert TaglessDefaultFilament(**parsed).slicer_filament is None

    def test_prefer_lowest_filament_is_removed(self):
        assert "prefer_lowest_filament" not in AppSettings.model_fields
        assert "prefer_lowest_filament" not in AppSettingsUpdate.model_fields


class TestSpoolSelectionPolicyValidator:
    @pytest.mark.parametrize("policy", ["slot_order", "lowest_remaining", "first_loaded"])
    def test_accepts_valid_policies(self, policy):
        assert AppSettingsUpdate(spool_selection_policy=policy).spool_selection_policy == policy

    def test_rejects_invalid_policy(self):
        with pytest.raises(ValidationError):
            AppSettingsUpdate(spool_selection_policy="bogus")

    def test_none_is_allowed(self):
        assert AppSettingsUpdate(spool_selection_policy=None).spool_selection_policy is None


class TestMinStartSpoolValidator:
    @pytest.mark.parametrize("value", [0, 120, 10000])
    def test_accepts_in_range(self, value):
        assert AppSettingsUpdate(min_start_spool_g=value).min_start_spool_g == value

    @pytest.mark.parametrize("value", [-1, 10001])
    def test_rejects_out_of_range(self, value):
        with pytest.raises(ValidationError):
            AppSettingsUpdate(min_start_spool_g=value)


class TestTaglessDefaultFilamentValidator:
    def test_accepts_valid_json(self):
        blob = json.dumps({"brand": "Polymaker", "material": "PLA", "rgba": "FF0000FF"})
        assert AppSettingsUpdate(tagless_default_filament=blob).tagless_default_filament == blob

    def test_empty_string_clears(self):
        assert AppSettingsUpdate(tagless_default_filament="").tagless_default_filament == ""

    def test_none_clears(self):
        assert AppSettingsUpdate(tagless_default_filament=None).tagless_default_filament is None

    def test_rejects_malformed_json(self):
        with pytest.raises(ValidationError):
            AppSettingsUpdate(tagless_default_filament="{not json")

    def test_rejects_non_object_json(self):
        with pytest.raises(ValidationError):
            AppSettingsUpdate(tagless_default_filament="[1, 2, 3]")

    def test_rejects_missing_required_field(self):
        # brand/material/rgba are required by TaglessDefaultFilament.
        blob = json.dumps({"material": "PLA", "rgba": "FF0000FF"})
        with pytest.raises(ValidationError):
            AppSettingsUpdate(tagless_default_filament=blob)
