"""Unit tests for the eject geometry accessor (Phase 2 registry).

Uses the shared ``seed_geometry`` fixture (H2S validated / H2C unvalidated).
"""

import pytest

from backend.app.services.eject.geometry import (
    GeometryUnavailable,
    get_geometry,
    get_geometry_required,
    list_geometries,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seed_geometry")]


class TestGetGeometryCanonResolution:
    @pytest.mark.parametrize("spelling", ["H2S", "O1S", "Bambu Lab H2S", "h2s", " H2S "])
    async def test_all_spellings_resolve_to_h2s_row(self, db_session, spelling):
        geo = await get_geometry(db_session, spelling)
        assert geo is not None
        assert geo.model_key == "H2S"
        assert geo.bed == (340.0, 320.0)
        assert geo.envelope == (0.0, 340.0, -16.0, 325.0)
        assert geo.validated is True

    @pytest.mark.parametrize("spelling", ["H2C", "O1C", "O1C2", "Bambu Lab H2C"])
    async def test_h2c_spellings_resolve_to_h2c_row(self, db_session, spelling):
        geo = await get_geometry(db_session, spelling)
        assert geo is not None
        assert geo.model_key == "H2C"
        assert geo.validated is False

    async def test_unknown_model_returns_none(self, db_session):
        assert await get_geometry(db_session, "X1C") is None

    @pytest.mark.parametrize("blank", [None, "", "   "])
    async def test_blank_model_returns_none(self, db_session, blank):
        assert await get_geometry(db_session, blank) is None


class TestGetGeometryRequired:
    async def test_validated_model_returns_geometry(self, db_session):
        geo = await get_geometry_required(db_session, "H2S", require_validated=True)
        assert geo.model_key == "H2S"

    async def test_unvalidated_model_allowed_when_not_required(self, db_session):
        geo = await get_geometry_required(db_session, "H2C", require_validated=False)
        assert geo.model_key == "H2C"
        assert geo.validated is False

    async def test_unvalidated_model_raises_when_required(self, db_session):
        with pytest.raises(GeometryUnavailable) as exc:
            await get_geometry_required(db_session, "H2C", require_validated=True)
        # Reason distinguishes the unvalidated cause and names the ladder.
        assert "not hardware-validated" in exc.value.reason
        assert "hardware ladder" in exc.value.reason
        assert "H2C" in exc.value.reason

    async def test_missing_model_raises_distinct_reason(self, db_session):
        with pytest.raises(GeometryUnavailable) as exc:
            await get_geometry_required(db_session, "X1C", require_validated=True)
        # Distinct from the unvalidated reason: no-geometry, not not-validated.
        assert "no eject geometry" in exc.value.reason
        assert "not hardware-validated" not in exc.value.reason
        assert "X1C" in exc.value.reason

    async def test_slice_code_resolves_before_validation_gate(self, db_session):
        # O1S canonicalises to H2S (validated) — the slice code must pass.
        geo = await get_geometry_required(db_session, "O1S", require_validated=True)
        assert geo.model_key == "H2S"


class TestListGeometries:
    async def test_lists_both_rows_ordered(self, db_session):
        geos = await list_geometries(db_session)
        keys = [g.model_key for g in geos]
        assert keys == ["H2C", "H2S"]  # ordered by model_key
        by_key = {g.model_key: g for g in geos}
        assert by_key["H2S"].validated is True
        assert by_key["H2C"].validated is False
