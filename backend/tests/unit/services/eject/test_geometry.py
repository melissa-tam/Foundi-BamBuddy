"""Unit tests for the eject geometry accessor (Phase 2 registry).

Uses the shared ``seed_geometry`` fixture (H2S validated / H2C unvalidated).
"""

import pytest

from backend.app.services.eject.geometry import (
    GeometryUnavailable,
    ModelGeometry,
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


class TestCooldownHoldPair:
    """The two cooldown plate-hold limits are ONE fact in two numbers.

    Both are PHYSICAL model limits (the toolhead's chute-park keep-out line in bed Y, and
    the clear height above the nozzle plane there), seeded by migration and never
    operator-settable. Both NULL = the hold is off for that model, which is where every
    model starts.
    """

    async def test_mapper_carries_both_numbers(self, db_session):
        from backend.app.models.printer_model_geometry import PrinterModelGeometry

        db_session.add(
            PrinterModelGeometry(
                model_key="H2X",
                bed_x=340,
                bed_y=320,
                env_x_min=0,
                env_x_max=340,
                env_y_min=-16,
                env_y_max=325,
                max_part_height_mm=42,
                z_travel_mm=340,
                validated=True,
                cooldown_hold_keepout_y_mm=285.0,
                cooldown_hold_clear_above_mm=51.0,
                notes="test seed",
            )
        )
        await db_session.commit()

        geo = await get_geometry(db_session, "H2X")
        assert geo is not None
        assert geo.cooldown_hold_keepout_y_mm == 285.0
        assert geo.cooldown_hold_clear_above_mm == 51.0

    async def test_unseeded_row_maps_to_hold_off(self, db_session):
        """The shared fixture seeds neither number — a model with no measured clearance
        must read as hold OFF, not as an unbounded hold."""
        geo = await get_geometry(db_session, "H2S")
        assert geo is not None
        assert geo.cooldown_hold_keepout_y_mm is None
        assert geo.cooldown_hold_clear_above_mm is None

    async def test_default_construction_is_hold_off(self):
        """Load-bearing default: every transient geometry and unmigrated fixture reads as
        hold OFF, so the hold can never appear on a model by omission."""
        geo = ModelGeometry(
            model_key="H2S",
            bed=(340.0, 320.0),
            envelope=(0.0, 340.0, -16.0, 325.0),
            max_part_height_mm=42.0,
            validated=True,
        )
        assert geo.cooldown_hold_keepout_y_mm is None
        assert geo.cooldown_hold_clear_above_mm is None

    @pytest.mark.parametrize(
        ("keepout", "clear"),
        [(285.0, None), (None, 51.0)],
    )
    async def test_one_sided_pair_is_refused(self, keepout, clear):
        """A keep-out line with no clear height (or the reverse) describes no hold at all.
        Treating the missing half as "unbounded" would authorise holding a plate under a
        clearance nobody measured, so it raises instead of half-honouring the pair."""
        with pytest.raises(ValueError) as exc:
            ModelGeometry(
                model_key="H2S",
                bed=(340.0, 320.0),
                envelope=(0.0, 340.0, -16.0, 325.0),
                max_part_height_mm=42.0,
                validated=True,
                cooldown_hold_keepout_y_mm=keepout,
                cooldown_hold_clear_above_mm=clear,
            )
        assert "both be set or both be None" in str(exc.value)
        assert "H2S" in str(exc.value)

    async def test_both_set_is_accepted(self):
        geo = ModelGeometry(
            model_key="H2S",
            bed=(340.0, 320.0),
            envelope=(0.0, 340.0, -16.0, 325.0),
            max_part_height_mm=42.0,
            validated=True,
            cooldown_hold_keepout_y_mm=285.0,
            cooldown_hold_clear_above_mm=51.0,
        )
        assert geo.cooldown_hold_keepout_y_mm == 285.0
        assert geo.cooldown_hold_clear_above_mm == 51.0
