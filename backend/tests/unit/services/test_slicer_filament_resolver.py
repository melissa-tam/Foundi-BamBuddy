"""Tests for ``resolve_slicer_filament`` (#1815).

The defensive filter at the end of the resolver clears ``tray_info_idx``
when its value isn't slicer-acceptable (literal material names + PFUS /
PFCN cloud-preset prefixes that the printer's calibration table can't
key on). Pre-#1815 it cleared ``setting_id`` alongside, which dropped
the slicer's only handle on the user's actual custom preset and forced
the caller into the generic-material fallback — Bambu Studio then
displayed "Generic <Material>" for spools whose Bambu Cloud detail
lookup didn't resolve a ``filament_id`` (cloud unauth on the on_ams_change
replay path, transient cloud failure, or custom presets whose detail
JSON omits ``filament_id``).

Post-#1815 the filter preserves a setting_id that's still a valid
slicer reference (PFUS / PFCN cloud user/shared preset, or GFS Bambu
official preset) even when ``tray_info_idx`` is cleared.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services.slicer_filament_resolver import resolve_slicer_filament


@pytest.mark.asyncio
async def test_pfus_cloud_unavailable_preserves_setting_id():
    """Reporter scenario: PFUS cloud user preset, cloud lookup fails to
    return a filament_id. setting_id must survive so the slicer can
    still find the user's actual custom preset."""
    db = MagicMock()
    with patch(
        "backend.app.api.routes.cloud.build_authenticated_cloud",
        AsyncMock(return_value=None),
    ):
        tray_info_idx, setting_id, sub_brand, temp_min, temp_max = await resolve_slicer_filament(
            db=db,
            current_user=None,
            slicer_filament="PFUS990b6e19965353",
            slicer_filament_name="Jayo PETG HF",
            material="PETG",
        )
    assert tray_info_idx == ""
    assert setting_id == "PFUS990b6e19965353"
    assert sub_brand is None


@pytest.mark.asyncio
async def test_pfcn_cloud_unavailable_preserves_setting_id():
    """PFCN partner/shared cloud preset (e.g. Polymaker H2D variants,
    #1648) shares the same shape problem as PFUS."""
    db = MagicMock()
    with patch(
        "backend.app.api.routes.cloud.build_authenticated_cloud",
        AsyncMock(return_value=None),
    ):
        tray_info_idx, setting_id, sub_brand, temp_min, temp_max = await resolve_slicer_filament(
            db=db,
            current_user=None,
            slicer_filament="PFCN1234567890",
            slicer_filament_name="Polymaker PolyTerra PLA",
            material="PLA",
        )
    assert tray_info_idx == ""
    assert setting_id == "PFCN1234567890"
    assert sub_brand is None


@pytest.mark.asyncio
async def test_pfus_cloud_resolves_filament_id_regression_guard():
    """When cloud auth works and returns a filament_id, the resolver
    keeps its existing behaviour: tray_info_idx = real filament_id,
    setting_id = original PFUS reference."""
    db = MagicMock()
    cloud_mock = MagicMock()
    cloud_mock.is_authenticated = True
    cloud_mock.get_setting_detail = AsyncMock(return_value={"filament_id": "P285e239", "name": "Jayo PETG HF @P1S"})
    cloud_mock.close = AsyncMock()
    with patch(
        "backend.app.api.routes.cloud.build_authenticated_cloud",
        AsyncMock(return_value=cloud_mock),
    ):
        tray_info_idx, setting_id, sub_brand, temp_min, temp_max = await resolve_slicer_filament(
            db=db,
            current_user=MagicMock(),
            slicer_filament="PFUS990b6e19965353",
            slicer_filament_name="Jayo PETG HF",
            material="PETG",
        )
    assert tray_info_idx == "P285e239"
    assert setting_id == "PFUS990b6e19965353"
    assert sub_brand == "Jayo PETG HF"


@pytest.mark.asyncio
async def test_gfs_cloud_unavailable_resolves_via_normalize():
    """GFS Bambu official preset + cloud unavailable: normalize strips
    the 'S' to give a real filament_id ('GFG02'), so tray_info_idx is
    valid and the defensive filter doesn't trigger. setting_id stays as
    the original GFS reference. Regression guard for the cloud-down
    Bambu-official path."""
    db = MagicMock()
    with patch(
        "backend.app.api.routes.cloud.build_authenticated_cloud",
        AsyncMock(return_value=None),
    ):
        tray_info_idx, setting_id, sub_brand, temp_min, temp_max = await resolve_slicer_filament(
            db=db,
            current_user=None,
            slicer_filament="GFSG02",
            slicer_filament_name=None,
            material="PETG",
        )
    assert tray_info_idx == "GFG02"
    assert setting_id == "GFSG02"
    assert sub_brand is None


@pytest.mark.asyncio
async def test_literal_material_name_clears_both():
    """slicer_filament='PETG' (free-text material leak from legacy
    spools): both tray_info_idx and setting_id must be cleared so the
    caller's generic-material fallback rescues the slot. Regression
    guard that the PFUS preservation doesn't accidentally preserve
    literal material names."""
    db = MagicMock()
    with patch(
        "backend.app.api.routes.cloud.build_authenticated_cloud",
        AsyncMock(return_value=None),
    ):
        tray_info_idx, setting_id, sub_brand, temp_min, temp_max = await resolve_slicer_filament(
            db=db,
            current_user=None,
            slicer_filament="PETG",
            slicer_filament_name=None,
            material="PETG",
        )
    assert tray_info_idx == ""
    assert setting_id == ""
    assert sub_brand is None


class TestNozzleTempResolution:
    """W4: the resolver returns the full wire-identity tuple and resolves temps in
    tier order - spool-row temps -> tagless-default fingerprint -> MATERIAL_TEMPS."""

    @pytest.mark.asyncio
    async def test_returns_five_tuple(self):
        db = MagicMock()
        result = await resolve_slicer_filament(
            db=db, current_user=None, slicer_filament="", slicer_filament_name=None, material="PETG"
        )
        assert len(result) == 5
        _, _, _, tmin, tmax = result
        assert isinstance(tmin, int) and isinstance(tmax, int)

    @pytest.mark.asyncio
    async def test_row_temps_win(self):
        db = MagicMock()
        _, _, _, tmin, tmax = await resolve_slicer_filament(
            db=db,
            current_user=None,
            slicer_filament="",
            slicer_filament_name=None,
            material="PETG",
            rgba="112233FF",
            nozzle_temp_min=245,
            nozzle_temp_max=265,
        )
        assert (tmin, tmax) == (245, 265)  # the spool row overrides every lower tier

    @pytest.mark.asyncio
    async def test_default_fingerprint_tier(self, monkeypatch):
        # No row temps, fingerprint matches the default -> the default's pair.
        from backend.app.services import spool_tagless

        monkeypatch.setattr(spool_tagless, "default_temps_for_fingerprint", AsyncMock(return_value=(230, 270)))
        db = MagicMock()
        _, _, _, tmin, tmax = await resolve_slicer_filament(
            db=db, current_user=None, slicer_filament="", slicer_filament_name=None, material="PETG", rgba="000000FF"
        )
        assert (tmin, tmax) == (230, 270)

    @pytest.mark.asyncio
    async def test_material_temps_fallback(self, monkeypatch):
        # No row temps, no fingerprint match -> MATERIAL_TEMPS.
        from backend.app.services import spool_tagless

        monkeypatch.setattr(spool_tagless, "default_temps_for_fingerprint", AsyncMock(return_value=None))
        db = MagicMock()
        _, _, _, tmin, tmax = await resolve_slicer_filament(
            db=db, current_user=None, slicer_filament="", slicer_filament_name=None, material="PETG", rgba="FF0000FF"
        )
        assert (tmin, tmax) == (220, 260)  # MATERIAL_TEMPS["PETG"]

    @pytest.mark.asyncio
    async def test_partial_row_temp_fills_from_material(self, monkeypatch):
        # Only min set on the row -> max fills from MATERIAL_TEMPS (independent tiers).
        from backend.app.services import spool_tagless

        monkeypatch.setattr(spool_tagless, "default_temps_for_fingerprint", AsyncMock(return_value=None))
        db = MagicMock()
        _, _, _, tmin, tmax = await resolve_slicer_filament(
            db=db,
            current_user=None,
            slicer_filament="",
            slicer_filament_name=None,
            material="PETG",
            nozzle_temp_min=235,
        )
        assert (tmin, tmax) == (235, 260)


class TestGenericIdOverride:
    """E3 (2026-07-20): a stale spool row carrying a GENERIC id must not be able to
    re-publish it — the firmware's auto-refill backup group pairs slots only on an
    exact brand-class/type/colour/TEMPS match, and 011-H2S's trays 1-2 sat on GFG99
    beside GFG02 peers, splitting the group. Every write site routes through this
    resolver, so the substitution belongs here (one chokepoint, all consumers)."""

    _DEFAULT = {"slicer_filament": "GFG02", "nozzle_temp_min": 230, "nozzle_temp_max": 270}

    def _override(self, monkeypatch, value):
        from backend.app.services import spool_tagless

        monkeypatch.setattr(spool_tagless, "override_generic_identity", AsyncMock(return_value=value))

    @pytest.mark.asyncio
    async def test_generic_id_is_replaced_with_the_defaults_id_and_temps(self, monkeypatch):
        self._override(monkeypatch, self._DEFAULT)
        db = MagicMock()
        tray_info_idx, setting_id, _sub, tmin, tmax = await resolve_slicer_filament(
            db=db,
            current_user=None,
            slicer_filament="GFG99",
            slicer_filament_name=None,
            material="PETG",
            rgba="000000FF",
            # Stale row temps: substituting the id alone would still split the backup
            # group on the temperature dimension, so these must be replaced too.
            nozzle_temp_min=220,
            nozzle_temp_max=260,
        )
        assert tray_info_idx == "GFG02"
        assert setting_id == "GFSG02"  # re-derived via the shared filament_id_to_setting_id
        assert (tmin, tmax) == (230, 270)

    @pytest.mark.asyncio
    async def test_no_default_or_no_fingerprint_match_passes_through(self, monkeypatch):
        self._override(monkeypatch, None)  # helper vetoed (feature off / different filament)
        db = MagicMock()
        tray_info_idx, setting_id, _sub, tmin, tmax = await resolve_slicer_filament(
            db=db,
            current_user=None,
            slicer_filament="GFG99",
            slicer_filament_name=None,
            material="PETG",
            rgba="FF0000FF",
            nozzle_temp_min=220,
            nozzle_temp_max=260,
        )
        assert tray_info_idx == "GFG99"  # unchanged
        assert setting_id == "GFSG99"
        assert (tmin, tmax) == (220, 260)

    @pytest.mark.asyncio
    async def test_lookup_failure_degrades_to_the_resolved_identity(self, monkeypatch):
        from backend.app.services import spool_tagless

        monkeypatch.setattr(
            spool_tagless, "override_generic_identity", AsyncMock(side_effect=RuntimeError("settings down"))
        )
        db = MagicMock()
        tray_info_idx, setting_id, _sub, tmin, tmax = await resolve_slicer_filament(
            db=db,
            current_user=None,
            slicer_filament="GFG99",
            slicer_filament_name=None,
            material="PETG",
            rgba="000000FF",
            nozzle_temp_min=220,
            nozzle_temp_max=260,
        )
        assert tray_info_idx == "GFG99"  # never raises into a slot write
        assert (tmin, tmax) == (220, 260)

    @pytest.mark.asyncio
    async def test_end_to_end_through_the_real_helper(self, monkeypatch):
        # No stubbing of the override: the configured tagless default drives it.
        import json

        from backend.app.services import spool_tagless

        default = json.dumps(
            {
                "brand": "Bambu Lab",
                "material": "PETG",
                "subtype": "HF",
                "rgba": "000000FF",
                "slicer_filament": "GFG02",
                "nozzle_temp_min": 230,
                "nozzle_temp_max": 270,
            }
        )

        async def fake_get_setting(db, key):
            return default if key == "tagless_default_filament" else None

        monkeypatch.setattr("backend.app.api.routes.settings.get_setting", fake_get_setting)
        assert spool_tagless.override_generic_identity  # the resolver's real collaborator
        db = MagicMock()
        tray_info_idx, setting_id, _sub, tmin, tmax = await resolve_slicer_filament(
            db=db,
            current_user=None,
            slicer_filament="GFG99",
            slicer_filament_name=None,
            material="PETG",
            rgba="000000FF",
            nozzle_temp_min=220,
            nozzle_temp_max=260,
        )
        assert (tray_info_idx, setting_id, tmin, tmax) == ("GFG02", "GFSG02", 230, 270)
