"""Tests for GET /printers/{id}/inventory-remain.

The endpoint exposes the same slot inventory (`spool_selection.build_slot_inventory`)
the dispatcher's selection policies consume, so PrintModal's client-side sort
agrees with what gets dispatched. It returns `inventory_remain_g`
(`{global_tray_id: grams}`, backward-compatible) plus `first_loaded`
(`{global_tray_id: iso8601 | null}`) for the FIFO policy — including the
Spoolman `first_used` → `first_loaded` mapping.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.api.routes.printers import get_inventory_remain
from backend.app.services.spool_selection import SlotInventory, epoch_to_iso


@pytest.fixture
def db():
    return MagicMock()


async def _call_endpoint(db, printer_id=1):
    return await get_inventory_remain(printer_id=printer_id, _=None, db=db)


def _patch_inventory(inv):
    return patch(
        "backend.app.services.spool_selection.build_slot_inventory",
        new=AsyncMock(return_value=inv),
    )


class TestGetInventoryRemain:
    @pytest.mark.asyncio
    async def test_returns_empty_when_printer_has_no_status(self, db):
        # Printer disconnected / unknown — endpoint must not error, return empties.
        with patch(
            "backend.app.services.printer_manager.printer_manager.get_status",
            return_value=None,
        ):
            result = await _call_endpoint(db)
        assert result == {"inventory_remain_g": {}, "first_loaded": {}}

    @pytest.mark.asyncio
    async def test_serialises_globaltrayid_keys_and_first_loaded(self, db):
        # JSON requires string keys; client converts back to Number on receive.
        # first_loaded renders the FIFO ordinal as ISO-8601 (null when unknown).
        state = SimpleNamespace(raw_data={})
        ord0 = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
        inv = {
            0: SlotInventory(remaining_g=950.0, first_loaded_ord=ord0),
            3: SlotInventory(remaining_g=50.0, first_loaded_ord=None),
        }
        with (
            patch(
                "backend.app.services.printer_manager.printer_manager.get_status",
                return_value=state,
            ),
            patch(
                "backend.app.services.print_scheduler.PrintScheduler._build_loaded_filaments",
                return_value=[
                    {"ams_id": 0, "tray_id": 0, "global_tray_id": 0, "is_external": False},
                    {"ams_id": 0, "tray_id": 3, "global_tray_id": 3, "is_external": False},
                ],
            ),
            _patch_inventory(inv),
        ):
            result = await _call_endpoint(db)

        assert result["inventory_remain_g"] == {"0": 950.0, "3": 50.0}
        assert result["first_loaded"] == {"0": epoch_to_iso(ord0), "3": None}

    @pytest.mark.asyncio
    async def test_returns_empty_dict_when_no_bound_slots(self, db):
        # Loaded filaments exist but none are bound to an inventory spool.
        state = SimpleNamespace(raw_data={})
        with (
            patch(
                "backend.app.services.printer_manager.printer_manager.get_status",
                return_value=state,
            ),
            patch(
                "backend.app.services.print_scheduler.PrintScheduler._build_loaded_filaments",
                return_value=[
                    {"ams_id": 0, "tray_id": 0, "global_tray_id": 0, "is_external": False},
                ],
            ),
            _patch_inventory({}),
        ):
            result = await _call_endpoint(db)

        assert result == {"inventory_remain_g": {}, "first_loaded": {}}

    @pytest.mark.asyncio
    async def test_spoolman_first_used_maps_to_first_loaded(self, db):
        # Spoolman-mode slot: build_slot_inventory converts `first_used` (ISO) to
        # the epoch ordinal; the endpoint renders it back to ISO under first_loaded.
        state = SimpleNamespace(raw_data={})
        first_used = "2026-05-04T08:00:00+00:00"
        ord_ = datetime.fromisoformat(first_used).timestamp()
        inv = {2: SlotInventory(remaining_g=720.0, first_loaded_ord=ord_)}
        with (
            patch(
                "backend.app.services.printer_manager.printer_manager.get_status",
                return_value=state,
            ),
            patch(
                "backend.app.services.print_scheduler.PrintScheduler._build_loaded_filaments",
                return_value=[
                    {"ams_id": 0, "tray_id": 2, "global_tray_id": 2, "is_external": False},
                ],
            ),
            _patch_inventory(inv),
        ):
            result = await _call_endpoint(db)

        assert result["inventory_remain_g"] == {"2": 720.0}
        assert result["first_loaded"] == {"2": epoch_to_iso(ord_)}
