"""``tray_fields.backup_group_key`` — the ONE origin for the firmware's backup grouping.

The AMS pairs slots into an auto-refill backup group only on an exact preset / colour /
nozzle-temperature match, so two trays back each other up if and only if this key is
equal for both. Before 2026-08-21 the rule lived as an inline f-string inside
``filament_deficit._live_tray_identities`` that omitted the temperature dimension
entirely, and nothing else in the backend spelled the rule at all — which is how the
tagless lanes could harmonise a slot's preset and temps for three weeks while leaving
its COLOUR split (010-H2S: slots 1+2 on ``161616FF``, slots 3+4 on ``000000FF``, 69
auto-switches inside the first pair, 14 inside the second, none across, and the printer
ran dry twice in 28 h with a full black roll one slot away).

These cases pin the key itself. Its two consumers are pinned where they live:
``test_filament_deficit`` for the pooling map, ``test_spool_tagless_reconcile`` for the
harmonise arm.
"""

import pytest

from backend.app.services.tray_fields import backup_group_key, normalize_color_for_id


class TestNormalizeColorForId:
    """The EXACT-comparison normaliser — deliberately not ``colors_similar``."""

    def test_alpha_and_case_and_hash_collapse(self):
        assert normalize_color_for_id("#000000ff") == "000000"
        assert normalize_color_for_id("000000FF") == normalize_color_for_id("000000")

    def test_near_colours_stay_distinct(self):
        """``colors_similar`` calls these one filament; the firmware does not pair them,
        and this normaliser answers the firmware's question."""
        assert normalize_color_for_id("161616FF") != normalize_color_for_id("000000FF")

    def test_empty_is_empty(self):
        assert normalize_color_for_id(None) == ""
        assert normalize_color_for_id("  ") == ""


class TestBackupGroupKey:
    @staticmethod
    def _tray(**kw):
        tray = {
            "id": 0,
            "state": 11,
            "tray_type": "PETG",
            "tray_info_idx": "GFG02",
            "tray_color": "000000FF",
            "nozzle_temp_min": 230,
            "nozzle_temp_max": 270,
        }
        tray.update(kw)
        return tray

    def test_shape(self):
        assert backup_group_key(self._tray()) == "tray:GFG02|color:000000|temps:230-270"

    def test_two_trays_pair_only_when_all_three_dimensions_agree(self):
        base = backup_group_key(self._tray())
        assert backup_group_key(self._tray(tray_color="000000")) == base  # alpha only
        assert backup_group_key(self._tray(nozzle_temp_min="230")) == base  # wire sends strings
        assert backup_group_key(self._tray(tray_color="161616FF")) != base  # 010-H2S
        assert backup_group_key(self._tray(tray_info_idx="GFG99")) != base  # 011-H2S
        assert backup_group_key(self._tray(nozzle_temp_max=260)) != base

    def test_preset_falls_back_to_the_configured_type(self):
        """A tray configured with a material but no preset id is still groupable — by the
        only name the firmware has for it."""
        assert backup_group_key(self._tray(tray_info_idx="")) == "tray:PETG|color:000000|temps:230-270"

    def test_unreported_temps_still_compare_equal_to_each_other(self):
        """Most third-party trays report no temps at all. Two equally-silent trays are
        peers; the key must not invent a difference between them."""
        silent = self._tray(nozzle_temp_min=None, nozzle_temp_max=None)
        assert backup_group_key(silent) == backup_group_key(self._tray(nozzle_temp_min=None, nozzle_temp_max=None))
        assert backup_group_key(silent) != backup_group_key(self._tray())

    @pytest.mark.parametrize(
        "tray",
        [
            {"id": 0, "state": 10},  # seated, unread — nothing to pair
            {"id": 0, "state": 10, "tray_type": "", "tray_info_idx": ""},  # bare
            {"id": 0, "state": 9, "tray_type": ""},  # wire-asserted empty
            "not a dict",
        ],
    )
    def test_no_group_for_an_empty_or_unidentified_tray(self, tray):
        assert backup_group_key(tray) is None

    def test_unknown_presence_still_groups(self):
        """The A1/P1S dialect pins ``state`` at a constant, so presence is permanently
        unknown. Unknown is not emptiness — a configured tray on that dialect is still in
        the firmware's group, and dropping it would silently un-pool a whole printer."""
        assert backup_group_key(self._tray(state=3)) == "tray:GFG02|color:000000|temps:230-270"
