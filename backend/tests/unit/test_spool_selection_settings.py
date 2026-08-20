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
        assert s.min_start_spool_g == 150  # R1: floor raised 120 -> 150
        # `auto_add_untagged` was deleted 2026-08-10 — it was a kill switch for
        # the tagless auto-mint lane, which doctrine rules 1/2/4 make load-bearing.
        assert not hasattr(s, "auto_add_untagged")

    def test_tagless_default_filament_default_shape(self):
        # W4: the shipped default carries the SPECIFIC Bambu PETG HF wire identity
        # (GFG02 + 230/270 nozzle range) so a bare-tray push is a byte-identical
        # firmware backup-group peer instead of the generic GFG99 that split the group.
        s = AppSettings()
        parsed = json.loads(s.tagless_default_filament)
        assert parsed["brand"] == "Bambu Lab"
        assert parsed["material"] == "PETG"
        assert parsed["subtype"] == "HF"
        assert parsed["rgba"] == "000000FF"
        assert parsed["slicer_filament"] == "GFG02"
        assert parsed["nozzle_temp_min"] == 230
        assert parsed["nozzle_temp_max"] == 270
        # Round-trips through the typed sub-model.
        model = TaglessDefaultFilament(**parsed)
        assert model.slicer_filament == "GFG02"
        assert (model.nozzle_temp_min, model.nozzle_temp_max) == (230, 270)

    def test_prefer_lowest_filament_is_removed(self):
        assert "prefer_lowest_filament" not in AppSettings.model_fields
        assert "prefer_lowest_filament" not in AppSettingsUpdate.model_fields


class TestRespoolAutoEnabled:
    """The W3 Tier-2 auto-respool toggle is GONE (2026-08-19, operator ruling 3).

    It used to default OFF and make tier 2 ask instead of concluding, encoding the
    superseded directive "the farm does NOT reuse tags yet". What superseded it is not a
    preference but a physical fact — you cannot add filament to a 0 g roll — so a FINISHED
    roll reading LOADED can only be a new roll on a reused core, and the tier concludes.
    Deleted rather than defaulted ON: leaving it would leave a dual path.
    """

    def test_the_toggle_is_gone_from_both_schemas(self):
        assert "respool_auto_enabled" not in AppSettings.model_fields
        assert "respool_auto_enabled" not in AppSettingsUpdate.model_fields


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
