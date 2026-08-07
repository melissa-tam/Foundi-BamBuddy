"""Integration tests for inventory spool assignment — tray_info_idx resolution.

Tests that the spool's own slicer_filament (including PFUS* cloud-synced
custom presets) takes priority, with slot reuse and generic fallback as
lower-priority fallbacks.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.spool import Spool


@pytest.fixture
async def spool_factory(db_session: AsyncSession):
    """Factory to create test spools."""
    _counter = [0]

    async def _create_spool(**kwargs):
        _counter[0] += 1
        defaults = {
            "material": "PLA",
            "subtype": "Basic",
            "brand": "Devil Design",
            "color_name": "Red",
            "rgba": "FF0000FF",
            "label_weight": 1000,
            "weight_used": 0,
            "slicer_filament": "PFUS9ac902733670a9",
        }
        defaults.update(kwargs)
        spool = Spool(**defaults)
        db_session.add(spool)
        await db_session.commit()
        await db_session.refresh(spool)
        return spool

    return _create_spool


async def _assignment_row(db_session, printer_id, ams_id, tray_id):
    """The slot's live SpoolAssignment, re-queried.

    Always re-queried rather than refreshed from a held instance: the binding writer's
    upsert may rebuild the row (move semantics), which expunges any instance a test
    seeded earlier.
    """
    from sqlalchemy import select

    from backend.app.models.spool_assignment import SpoolAssignment

    res = await db_session.execute(
        select(SpoolAssignment).where(
            SpoolAssignment.printer_id == printer_id,
            SpoolAssignment.ams_id == ams_id,
            SpoolAssignment.tray_id == tray_id,
        )
    )
    return res.scalar_one_or_none()


async def _pre_configured_at(db_session, printer_id, ams_id, tray_id):
    """The slot's explicit "configuration has not landed yet" stamp, straight from the DB.

    Read here rather than trusted from the response so the route's marker and the value
    the slot pipeline later consumes are pinned to be the same fact.
    """
    row = await _assignment_row(db_session, printer_id, ams_id, tray_id)
    return None if row is None else row.pre_configured_at


def _make_mock_status(ams_data=None, vt_tray=None, nozzles=None, ams_extruder_map=None):
    """Build a mock printer status with optional AMS/nozzle data."""
    status = MagicMock()
    raw = {}
    if ams_data is not None:
        raw["ams"] = {"ams": ams_data}
    if vt_tray is not None:
        raw["vt_tray"] = vt_tray
    status.raw_data = raw
    status.nozzles = nozzles or [MagicMock(nozzle_diameter="0.4")]
    status.ams_extruder_map = ams_extruder_map
    return status


class TestAssignSpoolTrayInfoIdx:
    """Tests for tray_info_idx resolution during spool assignment."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_pfus_slicer_filament_falls_back_to_generic(
        self, async_client: AsyncClient, printer_factory, spool_factory
    ):
        """PFUS* cloud setting_ids are rejected by the slicer as tray_info_idx, so the
        no-kp path falls back to the generic material id (PLA → GFL99). The K-profile
        realignment path translates PFUS → P-prefix when a stored kp exists; that's
        covered separately."""
        printer = await printer_factory(name="H2D")
        spool = await spool_factory(slicer_filament="PFUS9ac902733670a9", material="PLA")

        mock_client = MagicMock()
        mock_client.ams_set_filament_setting.return_value = True
        mock_client.extrusion_cali_sel.return_value = True

        status = _make_mock_status(ams_data=[{"id": 2, "tray": [{"id": 3, "tray_info_idx": "", "tray_type": "PLA"}]}])

        with patch("backend.app.services.printer_manager.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client
            mock_pm.get_status.return_value = status

            response = await async_client.post(
                "/api/v1/inventory/assignments",
                json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": 2, "tray_id": 3},
            )

            assert response.status_code == 200
            call_kwargs = mock_client.ams_set_filament_setting.call_args
            assert call_kwargs.kwargs["tray_info_idx"] == "GFL99"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_pfus_spool_reuses_valid_slot_preset(self, async_client: AsyncClient, printer_factory, spool_factory):
        """When the spool's PFUS gets discarded as slicer-invalid, the slot's existing
        valid P-prefix preset is reused if it matches the spool's material — preserves
        the printer's calibration context rather than resetting to generic."""
        printer = await printer_factory(name="H2D")
        spool = await spool_factory(slicer_filament="PFUS9ac902733670a9", material="PLA")

        mock_client = MagicMock()
        mock_client.ams_set_filament_setting.return_value = True
        mock_client.extrusion_cali_sel.return_value = True

        # Slot already configured by slicer with cloud-synced preset
        status = _make_mock_status(
            ams_data=[{"id": 2, "tray": [{"id": 3, "tray_info_idx": "P4d64437", "tray_type": "PLA"}]}]
        )

        with patch("backend.app.services.printer_manager.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client
            mock_pm.get_status.return_value = status

            response = await async_client.post(
                "/api/v1/inventory/assignments",
                json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": 2, "tray_id": 3},
            )

            assert response.status_code == 200
            call_kwargs = mock_client.ams_set_filament_setting.call_args
            assert call_kwargs.kwargs["tray_info_idx"] == "P4d64437"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_spool_preset_used_even_if_different_material_on_slot(
        self, async_client: AsyncClient, printer_factory, spool_factory
    ):
        """Spool's material drives the fallback generic id. Slot's existing PLA preset
        is overridden because the spool is PETG → GFG99."""
        printer = await printer_factory(name="H2D")
        spool = await spool_factory(slicer_filament="PFUS9ac902733670a9", material="PETG")

        mock_client = MagicMock()
        mock_client.ams_set_filament_setting.return_value = True
        mock_client.extrusion_cali_sel.return_value = True

        # Slot currently has PLA but spool is PETG
        status = _make_mock_status(
            ams_data=[{"id": 2, "tray": [{"id": 3, "tray_info_idx": "P4d64437", "tray_type": "PLA"}]}]
        )

        with patch("backend.app.services.printer_manager.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client
            mock_pm.get_status.return_value = status

            response = await async_client.post(
                "/api/v1/inventory/assignments",
                json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": 2, "tray_id": 3},
            )

            assert response.status_code == 200
            call_kwargs = mock_client.ams_set_filament_setting.call_args
            assert call_kwargs.kwargs["tray_info_idx"] == "GFG99"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_gf_slicer_filament_kept(self, async_client: AsyncClient, printer_factory, spool_factory):
        """Standard GF* IDs from spool.slicer_filament are used directly."""
        printer = await printer_factory(name="X1C")
        spool = await spool_factory(slicer_filament="GFL05", material="PLA")

        mock_client = MagicMock()
        mock_client.ams_set_filament_setting.return_value = True
        mock_client.extrusion_cali_sel.return_value = True

        status = _make_mock_status(ams_data=[{"id": 0, "tray": [{"id": 0, "tray_type": "PLA"}]}])

        with patch("backend.app.services.printer_manager.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client
            mock_pm.get_status.return_value = status

            response = await async_client.post(
                "/api/v1/inventory/assignments",
                json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": 0, "tray_id": 0},
            )

            assert response.status_code == 200
            call_kwargs = mock_client.ams_set_filament_setting.call_args
            assert call_kwargs.kwargs["tray_info_idx"] == "GFL05"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_empty_slicer_filament_uses_generic(self, async_client: AsyncClient, printer_factory, spool_factory):
        """Spool with no slicer_filament gets a generic ID from material type."""
        printer = await printer_factory(name="X1C")
        spool = await spool_factory(slicer_filament=None, material="ABS")

        mock_client = MagicMock()
        mock_client.ams_set_filament_setting.return_value = True
        mock_client.extrusion_cali_sel.return_value = True

        status = _make_mock_status(ams_data=[{"id": 0, "tray": [{"id": 0, "tray_type": "ABS"}]}])

        with patch("backend.app.services.printer_manager.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client
            mock_pm.get_status.return_value = status

            response = await async_client.post(
                "/api/v1/inventory/assignments",
                json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": 0, "tray_id": 0},
            )

            assert response.status_code == 200
            call_kwargs = mock_client.ams_set_filament_setting.call_args
            assert call_kwargs.kwargs["tray_info_idx"] == "GFB99"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_spool_pfus_falls_back_to_generic_over_slot_pfus(
        self, async_client: AsyncClient, printer_factory, spool_factory
    ):
        """Both spool and slot have PFUS values — both rejected as tray_info_idx —
        falls back to generic material id (PLA → GFL99)."""
        printer = await printer_factory(name="H2D")
        spool = await spool_factory(slicer_filament="PFUS1111111111", material="PLA")

        mock_client = MagicMock()
        mock_client.ams_set_filament_setting.return_value = True
        mock_client.extrusion_cali_sel.return_value = True

        # Slot has a PFUS* ID from some previous config
        status = _make_mock_status(
            ams_data=[{"id": 0, "tray": [{"id": 0, "tray_info_idx": "PFUS2222222222", "tray_type": "PLA"}]}]
        )

        with patch("backend.app.services.printer_manager.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client
            mock_pm.get_status.return_value = status

            response = await async_client.post(
                "/api/v1/inventory/assignments",
                json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": 0, "tray_id": 0},
            )

            assert response.status_code == 200
            call_kwargs = mock_client.ams_set_filament_setting.call_args
            assert call_kwargs.kwargs["tray_info_idx"] == "GFL99"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_generic_on_slot_falls_back_to_material_generic(
        self, async_client: AsyncClient, printer_factory, spool_factory
    ):
        """When spool's PFUS is discarded and slot only has a generic ID, the result
        comes from the spool's material (ABS → GFB99) — not from the slot. Important
        because the generic-id check (`not in _generic_id_values`) prevents stale
        generic reuse and routes the decision through the material fallback."""
        printer = await printer_factory(name="P2S")
        spool = await spool_factory(slicer_filament="PFUScda4c46fc9031", material="ABS")

        mock_client = MagicMock()
        mock_client.ams_set_filament_setting.return_value = True
        mock_client.extrusion_cali_sel.return_value = True

        # Slot stuck on generic ABS from a previous assignment
        status = _make_mock_status(
            ams_data=[{"id": 0, "tray": [{"id": 1, "tray_info_idx": "GFB99", "tray_type": "ABS"}]}]
        )

        with patch("backend.app.services.printer_manager.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client
            mock_pm.get_status.return_value = status

            response = await async_client.post(
                "/api/v1/inventory/assignments",
                json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": 0, "tray_id": 1},
            )

            assert response.status_code == 200
            call_kwargs = mock_client.ams_set_filament_setting.call_args
            assert call_kwargs.kwargs["tray_info_idx"] == "GFB99"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_no_preset_with_generic_on_slot_still_uses_generic(
        self, async_client: AsyncClient, printer_factory, spool_factory
    ):
        """Spool without preset + generic on slot → generic fallback (not slot reuse)."""
        printer = await printer_factory(name="P2S")
        spool = await spool_factory(slicer_filament=None, material="ABS")

        mock_client = MagicMock()
        mock_client.ams_set_filament_setting.return_value = True
        mock_client.extrusion_cali_sel.return_value = True

        # Slot has generic ABS
        status = _make_mock_status(
            ams_data=[{"id": 0, "tray": [{"id": 1, "tray_info_idx": "GFB99", "tray_type": "ABS"}]}]
        )

        with patch("backend.app.services.printer_manager.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client
            mock_pm.get_status.return_value = status

            response = await async_client.post(
                "/api/v1/inventory/assignments",
                json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": 0, "tray_id": 1},
            )

            assert response.status_code == 200
            call_kwargs = mock_client.ams_set_filament_setting.call_args
            # Still gets generic, but via fallback — not via sticky reuse
            assert call_kwargs.kwargs["tray_info_idx"] == "GFB99"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_no_preset_reuses_specific_slot_preset(
        self, async_client: AsyncClient, printer_factory, spool_factory
    ):
        """Spool without preset + specific preset on slot → reuse slot's preset."""
        printer = await printer_factory(name="X1C")
        spool = await spool_factory(slicer_filament=None, material="PLA")

        mock_client = MagicMock()
        mock_client.ams_set_filament_setting.return_value = True
        mock_client.extrusion_cali_sel.return_value = True

        # Slot has a specific Bambu PLA preset (not generic)
        status = _make_mock_status(
            ams_data=[{"id": 0, "tray": [{"id": 0, "tray_info_idx": "GFA05", "tray_type": "PLA"}]}]
        )

        with patch("backend.app.services.printer_manager.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client
            mock_pm.get_status.return_value = status

            response = await async_client.post(
                "/api/v1/inventory/assignments",
                json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": 0, "tray_id": 0},
            )

            assert response.status_code == 200
            call_kwargs = mock_client.ams_set_filament_setting.call_args
            # Slot's specific preset is reused when spool has no own preset
            assert call_kwargs.kwargs["tray_info_idx"] == "GFA05"


class TestAssignSpoolPresetMapping:
    """Tests that assign_spool saves the slot preset mapping for correct UI display."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_preset_mapping_saved_with_slicer_filament_name(
        self, async_client: AsyncClient, printer_factory, spool_factory
    ):
        """Slot preset mapping uses slicer_filament_name (not material+subtype)."""

        printer = await printer_factory(name="X1C")
        spool = await spool_factory(
            slicer_filament="GFA05",
            slicer_filament_name="Bambu PLA Silk",
            material="PLA",
            subtype="Silk",
            brand="Bambu",
        )

        mock_client = MagicMock()
        mock_client.ams_set_filament_setting.return_value = True
        mock_client.extrusion_cali_sel.return_value = True
        status = _make_mock_status(ams_data=[{"id": 0, "tray": [{"id": 1, "tray_type": "PLA"}]}])

        with patch("backend.app.services.printer_manager.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client
            mock_pm.get_status.return_value = status

            response = await async_client.post(
                "/api/v1/inventory/assignments",
                json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": 0, "tray_id": 1},
            )

        assert response.status_code == 200

        # Verify via the slot presets API
        presets_resp = await async_client.get(f"/api/v1/printers/{printer.id}/slot-presets")
        assert presets_resp.status_code == 200
        presets = presets_resp.json()
        # Key is str(ams_id * 4 + tray_id) — ams 0, tray 1 → "1"
        assert "1" in presets
        # Must use slicer_filament_name, NOT "PLA Silk" from material+subtype
        assert presets["1"]["preset_name"] == "Bambu PLA Silk"
        assert presets["1"]["preset_id"] == "GFSA05"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_preset_mapping_overwrites_old_mapping(
        self, async_client: AsyncClient, printer_factory, spool_factory, db_session: AsyncSession
    ):
        """Assigning a new spool overwrites the old slot preset mapping."""
        from backend.app.models.slot_preset import SlotPresetMapping

        printer = await printer_factory(name="X1C")

        # Pre-existing mapping (e.g. from previous manual configuration)
        old_mapping = SlotPresetMapping(
            printer_id=printer.id,
            ams_id=0,
            tray_id=2,
            preset_id="GFSA01",
            preset_name="Bambu PLA Matte",
            preset_source="cloud",
        )
        db_session.add(old_mapping)
        await db_session.commit()

        # Assign a "Generic PLA Silk" spool to same slot
        spool = await spool_factory(
            slicer_filament="GFL96",
            slicer_filament_name="Generic PLA Silk",
            material="PLA",
            subtype="Silk",
        )

        mock_client = MagicMock()
        mock_client.ams_set_filament_setting.return_value = True
        mock_client.extrusion_cali_sel.return_value = True
        status = _make_mock_status(ams_data=[{"id": 0, "tray": [{"id": 2, "tray_type": "PLA"}]}])

        with patch("backend.app.services.printer_manager.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client
            mock_pm.get_status.return_value = status

            response = await async_client.post(
                "/api/v1/inventory/assignments",
                json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": 0, "tray_id": 2},
            )

        assert response.status_code == 200

        # Verify via the slot presets API to avoid stale session cache
        presets_resp = await async_client.get(f"/api/v1/printers/{printer.id}/slot-presets")
        assert presets_resp.status_code == 200
        presets = presets_resp.json()
        # Key is str(ams_id * 4 + tray_id) — ams 0, tray 2 → "2"
        assert "2" in presets
        # Old "Bambu PLA Matte" must be overwritten
        assert presets["2"]["preset_name"] == "Generic PLA Silk"
        assert presets["2"]["preset_id"] == "GFSL96"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_preset_mapping_fallback_to_tray_sub_brands(
        self, async_client: AsyncClient, printer_factory, spool_factory
    ):
        """When slicer_filament_name is null, falls back to tray_sub_brands."""
        from backend.app.models.slot_preset import SlotPresetMapping

        printer = await printer_factory(name="A1M")
        spool = await spool_factory(
            slicer_filament="GFL05",
            slicer_filament_name=None,
            material="PLA",
            subtype="Matte",
            brand="Overture",
        )

        mock_client = MagicMock()
        mock_client.ams_set_filament_setting.return_value = True
        mock_client.extrusion_cali_sel.return_value = True
        status = _make_mock_status(ams_data=[{"id": 0, "tray": [{"id": 0, "tray_type": "PLA"}]}])

        with patch("backend.app.services.printer_manager.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client
            mock_pm.get_status.return_value = status

            response = await async_client.post(
                "/api/v1/inventory/assignments",
                json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": 0, "tray_id": 0},
            )

        assert response.status_code == 200

        # Verify via the slot presets API
        presets_resp = await async_client.get(f"/api/v1/printers/{printer.id}/slot-presets")
        assert presets_resp.status_code == 200
        presets = presets_resp.json()
        # Key is str(ams_id * 4 + tray_id) — ams 0, tray 0 → "0"
        assert "0" in presets
        # Falls back to tray_sub_brands ("Overture PLA Matte")
        assert presets["0"]["preset_name"] == "Overture PLA Matte"


class TestAssignSpoolLiveCaliIdx:
    """assign_spool always resets the slot to Default K when the spool has no stored K-profile."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_no_kprofile_resets_to_default_k(self, async_client: AsyncClient, printer_factory, spool_factory):
        """When no KProfile row exists, slot resets to cali_idx=-1 (Default K) regardless of live value."""
        printer = await printer_factory()
        spool = await spool_factory()

        mock_client = MagicMock()
        mock_client.ams_set_filament_setting.return_value = True
        mock_client.extrusion_cali_sel.return_value = True
        # Live cali_idx=42 belongs to whatever filament was previously calibrated
        # in this slot. Applying it to a different spool would use the wrong K
        # value, so the assign flow must override it with Default K (-1).
        tray_data = {
            "id": 1,
            "cali_idx": 42,
            "tray_color": "FF0000FF",
            "tray_type": "PLA",
            "tray_sub_brands": "PLA Basic",
            "tray_id_name": "GFL99",
        }
        status = _make_mock_status(ams_data=[{"id": 0, "tray": [tray_data]}])

        with patch("backend.app.services.printer_manager.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client
            mock_pm.get_status.return_value = status

            response = await async_client.post(
                "/api/v1/inventory/assignments",
                json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": 0, "tray_id": 1},
            )

        assert response.status_code == 200
        mock_client.extrusion_cali_sel.assert_called_once()
        assert mock_client.extrusion_cali_sel.call_args[1]["cali_idx"] == -1

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_no_kprofile_no_live_cali_idx_sends_default(
        self, async_client: AsyncClient, printer_factory, spool_factory
    ):
        """When tray has no cali_idx, extrusion_cali_sel is sent with cali_idx=-1 (Default)."""
        printer = await printer_factory()
        spool = await spool_factory()

        mock_client = MagicMock()
        mock_client.ams_set_filament_setting.return_value = True
        mock_client.extrusion_cali_sel.return_value = True
        tray_data = {
            "id": 0,
            "cali_idx": None,
            "tray_color": "FF0000FF",
            "tray_type": "PLA",
            "tray_sub_brands": "PLA Basic",
            "tray_id_name": "GFL99",
        }
        status = _make_mock_status(ams_data=[{"id": 0, "tray": [tray_data]}])

        with patch("backend.app.services.printer_manager.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client
            mock_pm.get_status.return_value = status

            response = await async_client.post(
                "/api/v1/inventory/assignments",
                json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": 0, "tray_id": 0},
            )

        assert response.status_code == 200
        mock_client.extrusion_cali_sel.assert_called_once()
        assert mock_client.extrusion_cali_sel.call_args[1]["cali_idx"] == -1

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_negative_live_cali_idx_sends_default(
        self, async_client: AsyncClient, printer_factory, spool_factory
    ):
        """A negative live cali_idx (-1) falls through and is sent as Default (cali_idx=-1)."""
        printer = await printer_factory()
        spool = await spool_factory()

        mock_client = MagicMock()
        mock_client.ams_set_filament_setting.return_value = True
        mock_client.extrusion_cali_sel.return_value = True
        tray_data = {
            "id": 0,
            "cali_idx": -1,
            "tray_color": "FF0000FF",
            "tray_type": "PLA",
            "tray_sub_brands": "PLA Basic",
            "tray_id_name": "GFL99",
        }
        status = _make_mock_status(ams_data=[{"id": 0, "tray": [tray_data]}])

        with patch("backend.app.services.printer_manager.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client
            mock_pm.get_status.return_value = status

            response = await async_client.post(
                "/api/v1/inventory/assignments",
                json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": 0, "tray_id": 0},
            )

        assert response.status_code == 200
        mock_client.extrusion_cali_sel.assert_called_once()
        assert mock_client.extrusion_cali_sel.call_args[1]["cali_idx"] == -1


class TestAssignSpoolEmptySlotPreConfig:
    """Assign path under ambiguous / explicit-empty AMS state.

    Two facts are pinned together here. (1) Only the firmware's *explicit* empty
    signal (state ∈ {9, 10}) skips MQTT (#1322 follow-up); anything else — including
    the SpoolBuddy weigh-then-assign-before-insert case where state/tray_type cannot
    tell us whether a spool is loaded — attempts MQTT. (2) When the configuration did
    NOT land, the route records that as an EXPLICIT fact on
    ``spool_assignment.pre_configured_at`` rather than leaving it to be inferred from a
    blank ``fingerprint_type``, and the response's ``pending_config`` is read straight
    off that column.

    The deferred-config workflow itself is the slot pipeline's one-shot apply
    (``pre_configured_apply``), driven below from a raw observation — the pre-cutover
    replay inside ``main.on_ams_change`` is gone.
    """

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_empty_tray_type_without_state_still_fires_mqtt(
        self, async_client: AsyncClient, printer_factory, spool_factory, db_session: AsyncSession
    ):
        """tray_type='' with no state field: AMS can't tell us whether a
        spool is loaded. Trust the user's Assign click and fire MQTT —
        firmware accepts it when a spool is physically there, drops it
        silently otherwise (no harm)."""
        printer = await printer_factory(name="H2D")
        spool = await spool_factory(slicer_filament="GFL05", material="PLA")

        mock_client = MagicMock()
        mock_client.ams_set_filament_setting.return_value = True
        mock_client.extrusion_cali_sel.return_value = True

        status = _make_mock_status(ams_data=[{"id": 2, "tray": [{"id": 3, "tray_type": ""}]}])

        with patch("backend.app.services.printer_manager.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client
            mock_pm.get_status.return_value = status

            response = await async_client.post(
                "/api/v1/inventory/assignments",
                json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": 2, "tray_id": 3},
            )

        assert response.status_code == 200
        mock_client.ams_set_filament_setting.assert_called_once()
        body = response.json()
        assert body["pending_config"] is False
        assert body["configured"] is True
        # The config landed → no "awaiting insert" marker is written.
        assert await _pre_configured_at(db_session, printer.id, 2, 3) is None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_no_ams_data_with_no_client_marks_pending(
        self, async_client: AsyncClient, printer_factory, spool_factory, db_session: AsyncSession
    ):
        """No AMS data + no MQTT client (printer offline, no telemetry):
        publish can't happen, so configured=False and pending_config=True so
        on_ams_change replay picks it up when the printer comes online."""
        printer = await printer_factory(name="X1C")
        spool = await spool_factory(slicer_filament="GFL05", material="PLA")

        # No AMS data — fingerprint_type stays None.
        status = _make_mock_status(ams_data=[])

        with patch("backend.app.services.printer_manager.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = None  # Printer offline, no MQTT client.
            mock_pm.get_status.return_value = status

            response = await async_client.post(
                "/api/v1/inventory/assignments",
                json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": 0, "tray_id": 0},
            )

        assert response.status_code == 200
        body = response.json()
        assert body["pending_config"] is True
        assert body["configured"] is False
        # ...and pending_config is READ OFF the explicit column, not inferred from a
        # blank fingerprint. This stamp is what the pipeline's one-shot apply consumes.
        assert await _pre_configured_at(db_session, printer.id, 0, 0) is not None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_loaded_slot_publishes_mqtt_immediately(
        self, async_client: AsyncClient, printer_factory, spool_factory, db_session: AsyncSession
    ):
        """Loaded slot (tray_type non-empty) → MQTT fires + pending_config=False."""
        printer = await printer_factory(name="X1C")
        spool = await spool_factory(slicer_filament="GFL05", material="PLA")

        mock_client = MagicMock()
        mock_client.ams_set_filament_setting.return_value = True
        mock_client.extrusion_cali_sel.return_value = True

        status = _make_mock_status(
            ams_data=[{"id": 0, "tray": [{"id": 0, "tray_type": "PLA", "tray_info_idx": "GFL05"}]}]
        )

        with patch("backend.app.services.printer_manager.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client
            mock_pm.get_status.return_value = status

            response = await async_client.post(
                "/api/v1/inventory/assignments",
                json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": 0, "tray_id": 0},
            )

        assert response.status_code == 200
        body = response.json()
        assert body["pending_config"] is False
        assert body["configured"] is True
        mock_client.ams_set_filament_setting.assert_called_once()
        assert await _pre_configured_at(db_session, printer.id, 0, 0) is None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_pipeline_applies_the_pre_config_when_the_slot_loads(
        self, async_client: AsyncClient, printer_factory, spool_factory, db_session: AsyncSession
    ):
        """One-shot apply: a PRE-CONFIGURED binding whose slot now holds filament gets
        its deferred configuration published, its fingerprint stamped, and the marker
        cleared — all from the raw AMS observation, through the pipeline."""
        from backend.app.models.spool_assignment import SpoolAssignment
        from backend.app.services.slot_pipeline import PipelineDeps, run_slot_pipeline
        from backend.app.services.slot_state import DecisionKind
        from backend.app.services.tray_observation import observe_tray

        printer = await printer_factory(name="H2D")
        spool = await spool_factory(slicer_filament="GFL05", material="PLA")

        pre_assignment = SpoolAssignment(
            spool_id=spool.id,
            printer_id=printer.id,
            ams_id=2,
            tray_id=3,
            fingerprint_color=None,
            fingerprint_type=None,
            pre_configured_at=datetime.utcnow(),
        )
        db_session.add(pre_assignment)
        await db_session.commit()

        # Filament has now been physically inserted: state=11 ("fed to extruder").
        tray = {"id": 3, "tray_type": "PLA", "tray_color": "FF0000FF", "state": 11}

        mock_client = MagicMock()
        mock_client.ams_set_filament_setting.return_value = True
        mock_client.extrusion_cali_sel.return_value = True
        mock_client.ams_unit_drying.return_value = False
        mock_client.ams_write_refusal.return_value = None

        status = _make_mock_status(ams_data=[{"id": 2, "tray": [tray]}])

        with (
            patch("backend.app.services.printer_manager.printer_manager") as mock_pm_inv,
            patch("backend.app.services.slot_pipeline.ws_manager") as mock_ws,
        ):
            mock_pm_inv.get_client.return_value = mock_client
            mock_pm_inv.get_status.return_value = status
            mock_ws.broadcast = AsyncMock()

            transitions = await run_slot_pipeline(
                printer.id,
                [observe_tray(printer.id, 2, tray)],
                PipelineDeps(db=db_session, client=mock_client),
            )

        assert [t.decision.kind for t in transitions] == [DecisionKind.BIND]
        assert transitions[0].decision.reason == "pre_configured_apply"

        # The deferred filament setting was published for the right slot.
        mock_client.ams_set_filament_setting.assert_called_once()
        call_kwargs = mock_client.ams_set_filament_setting.call_args.kwargs
        assert call_kwargs["ams_id"] == 2
        assert call_kwargs["tray_id"] == 3
        assert call_kwargs["tray_info_idx"] == "GFL05"

        # The marker is a ONE-SHOT: cleared on apply, with the fingerprint now stamped
        # so the next push reads the slot as an ordinary match. Re-queried rather than
        # refreshed — the binding writer's upsert may rebuild the row, so the instance
        # seeded above is not guaranteed to still be the live one.
        applied = await _assignment_row(db_session, printer.id, 2, 3)
        assert applied is not None
        assert applied.spool_id == spool.id
        assert applied.pre_configured_at is None
        assert applied.fingerprint_type == "PLA"
        assert applied.fingerprint_color == "FF0000FF"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_pipeline_does_not_refire_for_an_already_configured_slot(
        self, async_client: AsyncClient, printer_factory, spool_factory, db_session: AsyncSession
    ):
        """No pre-config marker + a matching fingerprint → KEEP: no second publish."""
        from backend.app.models.spool_assignment import SpoolAssignment
        from backend.app.services.slot_pipeline import PipelineDeps, run_slot_pipeline
        from backend.app.services.slot_state import DecisionKind
        from backend.app.services.tray_observation import observe_tray

        printer = await printer_factory(name="X1C")
        spool = await spool_factory(slicer_filament="GFL05", material="PLA")

        configured_assignment = SpoolAssignment(
            spool_id=spool.id,
            printer_id=printer.id,
            ams_id=0,
            tray_id=0,
            fingerprint_color="FF0000FF",
            fingerprint_type="PLA",
        )
        db_session.add(configured_assignment)
        await db_session.commit()

        tray = {"id": 0, "tray_type": "PLA", "tray_color": "FF0000FF", "state": 11}

        mock_client = MagicMock()
        mock_client.ams_set_filament_setting.return_value = True
        mock_client.extrusion_cali_sel.return_value = True
        mock_client.ams_unit_drying.return_value = False
        mock_client.ams_write_refusal.return_value = None

        status = _make_mock_status(ams_data=[{"id": 0, "tray": [tray]}])

        with (
            patch("backend.app.services.printer_manager.printer_manager") as mock_pm_inv,
            patch("backend.app.services.slot_pipeline.ws_manager") as mock_ws,
        ):
            mock_pm_inv.get_client.return_value = mock_client
            mock_pm_inv.get_status.return_value = status
            mock_ws.broadcast = AsyncMock()

            transitions = await run_slot_pipeline(
                printer.id,
                [observe_tray(printer.id, 0, tray)],
                PipelineDeps(db=db_session, client=mock_client),
            )

        assert [t.decision.kind for t in transitions] == [DecisionKind.KEEP]
        mock_client.ams_set_filament_setting.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_pre_configured_binding_is_kept_while_the_slot_stays_empty(
        self, async_client: AsyncClient, printer_factory, spool_factory, db_session: AsyncSession
    ):
        """ "Awaiting insert" is an operator INTENT, not a stale location claim: an empty
        slot releases a live binding (doctrine rule 9) but must NEVER release a
        pre-configured one, or the weigh-then-assign workflow would erase itself."""
        from backend.app.models.spool_assignment import SpoolAssignment
        from backend.app.services.slot_pipeline import PipelineDeps, run_slot_pipeline
        from backend.app.services.slot_state import DecisionKind
        from backend.app.services.tray_observation import observe_tray

        printer = await printer_factory(name="X1C")
        spool = await spool_factory(slicer_filament="GFL05", material="PLA")

        pre_assignment = SpoolAssignment(
            spool_id=spool.id,
            printer_id=printer.id,
            ams_id=0,
            tray_id=1,
            fingerprint_color=None,
            fingerprint_type=None,
            pre_configured_at=datetime.utcnow(),
        )
        db_session.add(pre_assignment)
        await db_session.commit()

        empty = {"id": 1, "state": 9, "tray_type": ""}

        mock_client = MagicMock()
        mock_client.ams_unit_drying.return_value = False
        mock_client.ams_write_refusal.return_value = None

        with patch("backend.app.services.slot_pipeline.ws_manager") as mock_ws:
            mock_ws.broadcast = AsyncMock()
            transitions = await run_slot_pipeline(
                printer.id,
                [observe_tray(printer.id, 0, empty)],
                PipelineDeps(db=db_session, client=mock_client),
            )

        assert [t.decision.kind for t in transitions] == [DecisionKind.KEEP]
        assert transitions[0].decision.reason == "pre_configured_awaiting_insert"
        assert await _pre_configured_at(db_session, printer.id, 0, 1) is not None  # intent survives


class TestAssignSpoolEmptyDetection:
    """Bambu firmware reports tray.state — 11=loaded, 9=empty, 10=spool present
    but filament not in feeder. The assign route must prefer that signal over
    tray_type for the empty-vs-loaded check, because a manual "Reset slot"
    clears tray_type to "" while leaving filament physically loaded — the
    legacy heuristic would route to the pending-config path and skip MQTT
    forever, since on_ams_change replay only fires on an empty→loaded
    transition that never comes when the slot is already loaded.
    """

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_state_loaded_with_empty_tray_type_fires_mqtt(
        self, async_client: AsyncClient, printer_factory, spool_factory
    ):
        """Post-reset case: state=11 (loaded) but tray_type='' — MQTT must fire."""
        printer = await printer_factory()
        spool = await spool_factory(slicer_filament="PFUS9ac902733670a9", material="PLA")

        mock_client = MagicMock()
        mock_client.ams_set_filament_setting.return_value = True
        mock_client.extrusion_cali_sel.return_value = True

        # Simulates the "reset slot" aftermath: filament physically loaded
        # (state=11) but tray_type/tray_color/tray_info_idx have been cleared.
        tray_data = {"id": 3, "state": 11, "tray_type": "", "tray_color": "", "tray_info_idx": ""}
        status = _make_mock_status(ams_data=[{"id": 2, "tray": [tray_data]}])

        with patch("backend.app.services.printer_manager.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client
            mock_pm.get_status.return_value = status

            response = await async_client.post(
                "/api/v1/inventory/assignments",
                json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": 2, "tray_id": 3},
            )

        assert response.status_code == 200
        # MQTT must have fired — the bug was that legacy detection saw the
        # empty tray_type and skipped this entirely.
        mock_client.ams_set_filament_setting.assert_called_once()
        # Response must report configured=True, pending_config=False — the
        # slot is loaded, just had stale metadata cleared.
        body = response.json()
        assert body["pending_config"] is False
        assert body["configured"] is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_state_empty_skips_mqtt_and_marks_pending(
        self, async_client: AsyncClient, printer_factory, spool_factory
    ):
        """Genuinely empty slot: state=9 — MQTT skipped, pending_config=True."""
        printer = await printer_factory()
        spool = await spool_factory(slicer_filament="PFUS9ac902733670a9", material="PLA")

        mock_client = MagicMock()
        mock_client.ams_set_filament_setting.return_value = True

        tray_data = {"id": 3, "state": 9, "tray_type": "", "tray_color": "", "tray_info_idx": ""}
        status = _make_mock_status(ams_data=[{"id": 2, "tray": [tray_data]}])

        with patch("backend.app.services.printer_manager.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client
            mock_pm.get_status.return_value = status

            response = await async_client.post(
                "/api/v1/inventory/assignments",
                json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": 2, "tray_id": 3},
            )

        assert response.status_code == 200
        # SpoolBuddy weigh-then-assign workflow: firmware drops MQTT for
        # unloaded slots, so we don't bother sending it.
        mock_client.ams_set_filament_setting.assert_not_called()
        body = response.json()
        assert body["pending_config"] is True
        assert body["configured"] is False

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_state_missing_falls_back_to_tray_type_loaded(
        self, async_client: AsyncClient, printer_factory, spool_factory
    ):
        """Older firmware without state field: tray_type='PLA' → treated as loaded."""
        printer = await printer_factory()
        spool = await spool_factory(slicer_filament="PFUS9ac902733670a9", material="PLA")

        mock_client = MagicMock()
        mock_client.ams_set_filament_setting.return_value = True

        # No 'state' key at all — older firmware behaviour.
        tray_data = {"id": 3, "tray_type": "PLA", "tray_color": "FF0000FF"}
        status = _make_mock_status(ams_data=[{"id": 2, "tray": [tray_data]}])

        with patch("backend.app.services.printer_manager.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client
            mock_pm.get_status.return_value = status

            response = await async_client.post(
                "/api/v1/inventory/assignments",
                json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": 2, "tray_id": 3},
            )

        assert response.status_code == 200
        # Legacy fallback: tray_type non-empty → treated as loaded → MQTT fires.
        mock_client.ams_set_filament_setting.assert_called_once()
        body = response.json()
        assert body["pending_config"] is False

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_state_missing_with_empty_tray_type_still_fires_mqtt(
        self, async_client: AsyncClient, printer_factory, spool_factory
    ):
        """Older firmware without state field + empty tray_type still fires MQTT.

        The AMS doesn't tell us whether a spool is physically loaded in this
        case (no state, no tray_type), so the assign click is the user's
        assertion that a spool is there. Firmware silently drops the push on
        a truly empty slot — no harm done, and on_ams_change replay handles
        the deferred-config case (#1322 follow-up).
        """
        printer = await printer_factory()
        spool = await spool_factory(slicer_filament="PFUS9ac902733670a9", material="PLA")

        mock_client = MagicMock()
        mock_client.ams_set_filament_setting.return_value = True
        mock_client.extrusion_cali_sel.return_value = True

        tray_data = {"id": 3, "tray_type": "", "tray_color": ""}
        status = _make_mock_status(ams_data=[{"id": 2, "tray": [tray_data]}])

        with patch("backend.app.services.printer_manager.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client
            mock_pm.get_status.return_value = status

            response = await async_client.post(
                "/api/v1/inventory/assignments",
                json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": 2, "tray_id": 3},
            )

        assert response.status_code == 200
        mock_client.ams_set_filament_setting.assert_called_once()
        body = response.json()
        assert body["pending_config"] is False
        assert body["configured"] is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_state_never_eleven_firmware_with_loaded_tray_fires_mqtt(
        self, async_client: AsyncClient, printer_factory, spool_factory
    ):
        """A1 Mini BMCU 01.07.02.00 and P1S Standard AMS 00.00.06.75 always
        report tray.state=3, never 11 — even for fully-loaded configured slots.
        A state-only check classified those as empty and skipped MQTT (#1322).
        With the disjunctive check, tray_type='PLA' alone is enough to fire."""
        printer = await printer_factory()
        spool = await spool_factory(slicer_filament="PFUS9ac902733670a9", material="PLA")

        mock_client = MagicMock()
        mock_client.ams_set_filament_setting.return_value = True
        mock_client.extrusion_cali_sel.return_value = True

        # state=3, tray_type non-empty — A1 Mini / P1S configured slot.
        tray_data = {"id": 3, "state": 3, "tray_type": "PLA", "tray_color": "FF0000FF", "tray_info_idx": "GFL99"}
        status = _make_mock_status(ams_data=[{"id": 2, "tray": [tray_data]}])

        with patch("backend.app.services.printer_manager.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client
            mock_pm.get_status.return_value = status

            response = await async_client.post(
                "/api/v1/inventory/assignments",
                json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": 2, "tray_id": 3},
            )

        assert response.status_code == 200
        mock_client.ams_set_filament_setting.assert_called_once()
        body = response.json()
        assert body["pending_config"] is False
        assert body["configured"] is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_post_reset_slot_with_state_3_still_fires_mqtt(
        self, async_client: AsyncClient, printer_factory, spool_factory
    ):
        """A1 Mini BMCU / P1S Standard AMS post-"Reset Slot" with spool still
        inserted: state=3, tray_type="". The AMS gives us no signal to tell
        this apart from a truly-empty slot. We trust the user's Assign click
        and fire MQTT — firmware accepts the push because a spool is
        physically there (#1322 follow-up by @RosdasHH).

        Replaces the previous "marks_pending" assertion which was the bug:
        that gate created a deadlock because the AMS would never report a
        state change (nothing physically changed), so on_ams_change replay
        never re-fired the deferred config either.
        """
        printer = await printer_factory()
        spool = await spool_factory(slicer_filament="PFUS9ac902733670a9", material="PLA")

        mock_client = MagicMock()
        mock_client.ams_set_filament_setting.return_value = True
        mock_client.extrusion_cali_sel.return_value = True

        tray_data = {"id": 3, "state": 3, "tray_type": "", "tray_color": "00000000", "tray_info_idx": ""}
        status = _make_mock_status(ams_data=[{"id": 2, "tray": [tray_data]}])

        with patch("backend.app.services.printer_manager.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client
            mock_pm.get_status.return_value = status

            response = await async_client.post(
                "/api/v1/inventory/assignments",
                json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": 2, "tray_id": 3},
            )

        assert response.status_code == 200
        mock_client.ams_set_filament_setting.assert_called_once()
        body = response.json()
        assert body["pending_config"] is False
        assert body["configured"] is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_external_slot_state_loaded_with_empty_tray_type_fires_mqtt(
        self, async_client: AsyncClient, printer_factory, spool_factory
    ):
        """External (vt_tray) slot post-reset: same fix applies for ams_id=255."""
        printer = await printer_factory(name="X1C")
        spool = await spool_factory(slicer_filament="PFUS9ac902733670a9", material="PLA")

        mock_client = MagicMock()
        mock_client.ams_set_filament_setting.return_value = True

        # External slot tray_id=0 → vt_tray id=254. state=11 (loaded), tray_type
        # cleared by reset.
        vt_data = [{"id": 254, "state": 11, "tray_type": "", "tray_color": "", "tray_info_idx": ""}]
        status = _make_mock_status(ams_data=[], vt_tray=vt_data)

        with patch("backend.app.services.printer_manager.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client
            mock_pm.get_status.return_value = status

            response = await async_client.post(
                "/api/v1/inventory/assignments",
                json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": 255, "tray_id": 0},
            )

        assert response.status_code == 200
        mock_client.ams_set_filament_setting.assert_called_once()
        body = response.json()
        assert body["pending_config"] is False
        assert body["configured"] is True


class TestAssignSpoolPfcnCloudPreset:
    """Assign path for PFCN-prefix cloud presets (#1648).

    PFCN is a third Bambu cloud preset shape alongside PFUS (cloud user-created)
    and GFS (Bambu official) — used for cloud-shared / partner-uploaded
    presets like Polymaker's "(Custom)" Bambu Lab H2D variants. Before #1648
    the assign path skipped the cloud-detail lookup and left the raw PFCN
    string in tray_info_idx, which the printer's calibration table can't
    resolve. ConfigureAmsSlotModal rescued each assignment by doing the lookup
    itself, making "Configure" feel like a mandatory follow-up step.
    """

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_pfcn_falls_back_to_generic_when_cloud_unavailable(
        self, async_client: AsyncClient, printer_factory, spool_factory
    ):
        """When cloud auth isn't available (e.g. user not logged into Bambu Cloud),
        the raw PFCN must be discarded as slicer-invalid and the slot configures
        with the spool's generic material id (PLA → GFL99). Pre-fix behaviour
        was to leak the raw PFCN, which the slicer can't resolve."""
        printer = await printer_factory(name="H2D")
        spool = await spool_factory(slicer_filament="PFCN80e80c1f79db85", material="PLA")

        mock_client = MagicMock()
        mock_client.ams_set_filament_setting.return_value = True
        mock_client.extrusion_cali_sel.return_value = True

        status = _make_mock_status(ams_data=[{"id": 2, "tray": [{"id": 3, "tray_info_idx": "", "tray_type": "PLA"}]}])

        with patch("backend.app.services.printer_manager.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client
            mock_pm.get_status.return_value = status

            response = await async_client.post(
                "/api/v1/inventory/assignments",
                json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": 2, "tray_id": 3},
            )

            assert response.status_code == 200
            call_kwargs = mock_client.ams_set_filament_setting.call_args
            # PFCN never leaks into tray_info_idx — must resolve to the
            # generic-material fallback when cloud lookup couldn't.
            assert call_kwargs.kwargs["tray_info_idx"] == "GFL99"
            assert not call_kwargs.kwargs["tray_info_idx"].startswith("PFCN")

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_pfcn_spool_reuses_valid_slot_preset(self, async_client: AsyncClient, printer_factory, spool_factory):
        """Symmetry with the PFUS case: when the spool's PFCN is discarded as
        slicer-invalid, the slot's existing valid P-prefix preset is reused
        if material matches — preserves calibration context instead of
        resetting to generic."""
        printer = await printer_factory(name="H2D")
        spool = await spool_factory(slicer_filament="PFCN80e80c1f79db85", material="PLA")

        mock_client = MagicMock()
        mock_client.ams_set_filament_setting.return_value = True
        mock_client.extrusion_cali_sel.return_value = True

        status = _make_mock_status(
            ams_data=[{"id": 2, "tray": [{"id": 3, "tray_info_idx": "P4d64437", "tray_type": "PLA"}]}]
        )

        with patch("backend.app.services.printer_manager.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client
            mock_pm.get_status.return_value = status

            response = await async_client.post(
                "/api/v1/inventory/assignments",
                json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": 2, "tray_id": 3},
            )

            assert response.status_code == 200
            call_kwargs = mock_client.ams_set_filament_setting.call_args
            assert call_kwargs.kwargs["tray_info_idx"] == "P4d64437"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_pfcn_resolves_to_filament_id_via_cloud_lookup(
        self, async_client: AsyncClient, printer_factory, spool_factory
    ):
        """When the user is authenticated against Bambu Cloud, the PFCN setting_id
        triggers the same cloud-detail lookup as PFUS / GFS — extracts the real
        filament_id from `detail["filament_id"]` and ships that as
        tray_info_idx. This is the happy path the Configure modal already had
        but the assign path didn't, #1648."""
        printer = await printer_factory(name="H2D")
        spool = await spool_factory(slicer_filament="PFCN80e80c1f79db85", material="PLA")

        mock_client = MagicMock()
        mock_client.ams_set_filament_setting.return_value = True
        mock_client.extrusion_cali_sel.return_value = True

        status = _make_mock_status(ams_data=[{"id": 2, "tray": [{"id": 3, "tray_info_idx": "", "tray_type": "PLA"}]}])

        # Cloud responds with a real filament_id for the PFCN preset — exactly
        # what the Configure modal already exploits.
        mock_cloud = MagicMock()
        mock_cloud.is_authenticated = True

        async def fake_get_detail(setting_id):
            assert setting_id == "PFCN80e80c1f79db85"
            return {"filament_id": "GFL05", "name": "Polymaker PLA Matte"}

        async def fake_close():
            return None

        mock_cloud.get_setting_detail = fake_get_detail
        mock_cloud.close = fake_close

        async def fake_build_cloud(_db, _user):
            return mock_cloud

        with (
            patch("backend.app.services.printer_manager.printer_manager") as mock_pm,
            patch("backend.app.api.routes.cloud.build_authenticated_cloud", new=fake_build_cloud),
        ):
            mock_pm.get_client.return_value = mock_client
            mock_pm.get_status.return_value = status

            response = await async_client.post(
                "/api/v1/inventory/assignments",
                json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": 2, "tray_id": 3},
            )

            assert response.status_code == 200
            call_kwargs = mock_client.ams_set_filament_setting.call_args
            # tray_info_idx is the resolved cloud filament_id; setting_id is the
            # original PFCN (which the slicer needs separately).
            assert call_kwargs.kwargs["tray_info_idx"] == "GFL05"
            assert call_kwargs.kwargs["setting_id"] == "PFCN80e80c1f79db85"


class TestAssignMovesExistingBinding:
    """012-H2S: ``POST /assignments`` binds through ``spool_binding.bind_spool_to_slot``,
    so assigning a spool that is currently bound to another slot MOVES it. Before the
    fix the route deleted only the target slot's row and the spool ended up bound to two
    trays at once, feeding one ledger to two feeders."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_assign_moves_spool_off_its_previous_slot(
        self, async_client: AsyncClient, printer_factory, spool_factory
    ):
        printer = await printer_factory(name="H2S")
        spool = await spool_factory(material="PETG")

        mock_client = MagicMock()
        mock_client.ams_set_filament_setting.return_value = True
        mock_client.extrusion_cali_sel.return_value = True
        status = _make_mock_status(
            ams_data=[
                {
                    "id": 0,
                    "tray": [
                        {"id": 0, "state": 11, "tray_type": "PETG"},
                        {"id": 1, "state": 11, "tray_type": "PETG"},
                    ],
                }
            ]
        )

        with patch("backend.app.services.printer_manager.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client
            mock_pm.get_status.return_value = status

            first = await async_client.post(
                "/api/v1/inventory/assignments",
                json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": 0, "tray_id": 0},
            )
            assert first.status_code == 200

            second = await async_client.post(
                "/api/v1/inventory/assignments",
                json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": 0, "tray_id": 1},
            )
            assert second.status_code == 200
            assert second.json()["tray_id"] == 1

        listing = await async_client.get("/api/v1/inventory/assignments")
        assert listing.status_code == 200
        mine = [a for a in listing.json() if a["spool_id"] == spool.id]
        assert len(mine) == 1, "the roll is in exactly one place — the old row must be gone"
        assert (mine[0]["ams_id"], mine[0]["tray_id"]) == (0, 1)


class TestAssignSpoolReleasesStaged:
    """W6.3: a successful manual assign releases low-spool staged (``filament_short``)
    units immediately — the deficit changes via the new DB assignment, so it must
    NOT wait for the printer's MQTT tray echo."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_assign_releases_staged_unit(self, async_client, printer_factory, spool_factory, db_session):
        from unittest.mock import AsyncMock

        from sqlalchemy import select

        from backend.app.models.print_queue import PrintQueueItem

        printer = await printer_factory(name="H2S")
        spool = await spool_factory(material="PETG")

        staged = PrintQueueItem(
            printer_id=printer.id,
            status="pending",
            manual_start=True,
            filament_short=True,
            waiting_reason="filament_short",
            plate_id=1,
            position=1,
        )
        db_session.add(staged)
        await db_session.commit()
        staged_id = staged.id

        mock_client = MagicMock()
        mock_client.ams_set_filament_setting.return_value = True
        status = _make_mock_status(ams_data=[{"id": 0, "tray": [{"id": 0, "state": 11, "tray_type": "PETG"}]}])

        with (
            patch("backend.app.services.printer_manager.printer_manager") as mock_pm,
            patch(
                "backend.app.services.farm_staging.compute_deficit_for_queue_item", new_callable=AsyncMock
            ) as mock_deficit,
        ):
            mock_pm.get_client.return_value = mock_client
            mock_pm.get_status.return_value = status
            mock_deficit.return_value = []  # the new assignment clears the deficit

            response = await async_client.post(
                "/api/v1/inventory/assignments",
                json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": 0, "tray_id": 0},
            )
            assert response.status_code == 200

        db_session.expire_all()
        refreshed = (
            await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.id == staged_id))
        ).scalar_one()
        # Released without any MQTT echo — no on_ams_change fired in this test.
        assert refreshed.manual_start is False
        assert refreshed.filament_short is False
        assert refreshed.waiting_reason is None


class TestUnassignDismissesAStaleFreshRollPrompt:
    """``DELETE /assignments`` ends a location claim, so it ends the fresh-roll question
    that named that slot too (2026-08-07: a stamp survived every kind of release and the
    toast replayed for days for a slot whose roll had left).

    The stamp is cleared by the ONE unbind writer (``spool_binding.release_spool_from_slot``,
    which reports it); the route's only job is the cross-client dismissal — the same event
    the operator's own answer emits.
    """

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_delete_clears_the_stamp_and_dismisses_the_toast(
        self, async_client: AsyncClient, printer_factory, spool_factory, db_session
    ):
        from sqlalchemy import select

        from backend.app.models.spool_assignment import SpoolAssignment

        printer = await printer_factory(name="H2S")
        spool = await spool_factory(fresh_prompt_pending_at=datetime.utcnow())
        db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=printer.id, ams_id=0, tray_id=1))
        await db_session.commit()
        # Ids captured up front: the route deletes the assignment on its own session, and
        # a held instance may be expired by the time the assertions run.
        printer_id, spool_id = printer.id, spool.id

        with patch(
            "backend.app.services.spool_tagless.broadcast_tagless_fresh_dismissed", new_callable=AsyncMock
        ) as dismissed:
            response = await async_client.delete(f"/api/v1/inventory/assignments/{printer_id}/0/1")

        assert response.status_code == 200
        dismissed.assert_awaited_once_with(printer_id, 0, 1)
        db_session.expire_all()
        stamp = await db_session.scalar(select(Spool.fresh_prompt_pending_at).where(Spool.id == spool_id))
        assert stamp is None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_delete_of_an_unstamped_row_dismisses_nothing(
        self, async_client: AsyncClient, printer_factory, spool_factory, db_session
    ):
        """No stamp, no toast — the dismissal rides the writer's signal, never a blind emit."""
        from backend.app.models.spool_assignment import SpoolAssignment

        printer = await printer_factory(name="H2S")
        spool = await spool_factory()
        db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=printer.id, ams_id=0, tray_id=1))
        await db_session.commit()

        with patch(
            "backend.app.services.spool_tagless.broadcast_tagless_fresh_dismissed", new_callable=AsyncMock
        ) as dismissed:
            response = await async_client.delete(f"/api/v1/inventory/assignments/{printer.id}/0/1")

        assert response.status_code == 200
        dismissed.assert_not_awaited()
