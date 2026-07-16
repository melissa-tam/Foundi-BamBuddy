"""Tests for the AMS Filament Backup gate on the spool-selection policy (#1766).

The reporter set the (legacy) lowest-remaining preference but the printer kept
picking the first matching spool. Root cause: without the printer's AMS Filament
Backup enabled, switching to the second spool is impossible — so sorting toward
the lowest leaves the print at risk. In the policy world this gate lives in
``spool_selection.effective_policy``: ``lowest_remaining`` degrades to
``slot_order`` when backup is OFF, with None (unknown / A1 family) preserving
the requested policy. ``first_loaded`` passes through (its backup handling is the
matcher's smart-cover partition).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services.print_scheduler import PrintScheduler
from backend.app.services.spool_selection import MatchOutcome, effective_policy


@pytest.fixture
def scheduler():
    return PrintScheduler()


def _patch_status(backup):
    """Patch ``printer_manager.get_status`` to return a stub PrinterState whose
    ``ams_filament_backup`` is the requested tri-state value."""
    return patch(
        "backend.app.services.print_scheduler.printer_manager.get_status",
        return_value=SimpleNamespace(ams_filament_backup=backup, raw_data={}),
    )


async def _effective_policy_handed_to_matcher(scheduler, backup_state, policy_setting):
    """Drive ``_compute_ams_mapping_for_printer`` and capture the policy the
    matcher actually receives (i.e. post-``effective_policy`` gate)."""
    db = MagicMock()
    item = SimpleNamespace(filament_overrides=None, skip_filament_check=False, id=1)
    reqs = [{"slot_id": 1, "type": "PLA", "color": "#000000", "tray_info_idx": "", "used_grams": 10.0}]
    loaded = [
        {
            "ams_id": 0,
            "tray_id": 0,
            "global_tray_id": 0,
            "is_external": False,
            "type": "PLA",
            "color": "#000000",
            "tray_info_idx": "",
            "remain": -1,
        }
    ]
    captured: dict = {}

    def _capture(required, loaded_, *, policy, inv, backup_on, min_start_g):
        captured["policy"] = policy
        captured["backup_on"] = backup_on
        captured["min_start_g"] = min_start_g
        return MatchOutcome(mapping=[0])

    with (
        _patch_status(backup_state),
        patch.object(scheduler, "_get_filament_requirements", new=AsyncMock(return_value=reqs)),
        patch.object(scheduler, "_build_loaded_filaments", return_value=loaded),
        patch.object(scheduler, "_get_setting", new=AsyncMock(return_value=policy_setting)),
        patch.object(scheduler, "_get_int_setting", new=AsyncMock(return_value=0)),
        patch("backend.app.services.print_scheduler.build_slot_inventory", new=AsyncMock(return_value={})),
        patch("backend.app.services.print_scheduler.match_filaments_to_slots", side_effect=_capture),
    ):
        await scheduler._compute_ams_mapping_for_printer(db, printer_id=1, item=item)

    return captured.get("policy")


class TestEffectivePolicyGate:
    def test_lowest_remaining_backup_off_degrades(self):
        assert effective_policy("lowest_remaining", False) == "slot_order"

    def test_lowest_remaining_backup_on_preserved(self):
        assert effective_policy("lowest_remaining", True) == "lowest_remaining"

    def test_lowest_remaining_backup_unknown_preserved(self):
        assert effective_policy("lowest_remaining", None) == "lowest_remaining"

    def test_first_loaded_passes_through(self):
        assert effective_policy("first_loaded", False) == "first_loaded"


class TestBackupGateThroughScheduler:
    @pytest.mark.asyncio
    async def test_backup_off_coerces_lowest_remaining_to_slot_order(self, scheduler):
        # #1766: backup OFF + lowest_remaining must reach the matcher as slot_order.
        out = await _effective_policy_handed_to_matcher(
            scheduler, backup_state=False, policy_setting="lowest_remaining"
        )
        assert out == "slot_order"

    @pytest.mark.asyncio
    async def test_backup_on_preserves_lowest_remaining(self, scheduler):
        out = await _effective_policy_handed_to_matcher(scheduler, backup_state=True, policy_setting="lowest_remaining")
        assert out == "lowest_remaining"

    @pytest.mark.asyncio
    async def test_backup_unknown_preserves_lowest_remaining(self, scheduler):
        # None = unknown / unsupported (A1 family). Must NOT be treated as OFF.
        out = await _effective_policy_handed_to_matcher(scheduler, backup_state=None, policy_setting="lowest_remaining")
        assert out == "lowest_remaining"

    @pytest.mark.asyncio
    async def test_first_loaded_reaches_matcher_regardless_of_backup(self, scheduler):
        # The farm default passes through unchanged even with backup OFF.
        out = await _effective_policy_handed_to_matcher(scheduler, backup_state=False, policy_setting="first_loaded")
        assert out == "first_loaded"
