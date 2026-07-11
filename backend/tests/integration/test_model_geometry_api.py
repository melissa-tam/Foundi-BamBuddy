"""Integration tests for the model-geometry registry API (Phase 2)."""

import logging

import pytest
from httpx import AsyncClient


@pytest.fixture(autouse=True)
async def _seed_geometry(db_session):
    """Seed H2S (validated) + H2C (unvalidated) — the create_all test DB has no
    run_migrations seeds. Committed via db_session; the route reads it back."""
    from backend.app.models.printer_model_geometry import PrinterModelGeometry

    db_session.add_all(
        [
            PrinterModelGeometry(
                model_key="H2S",
                bed_x=340,
                bed_y=320,
                env_x_min=0,
                env_x_max=340,
                env_y_min=-16,
                env_y_max=325,
                max_part_height_mm=42,
                validated=True,
                notes="seed",
            ),
            PrinterModelGeometry(
                model_key="H2C",
                bed_x=330,
                bed_y=320,
                env_x_min=25,
                env_x_max=325,
                env_y_min=0,
                env_y_max=320,
                max_part_height_mm=42,
                validated=False,
                notes="MEASURE AT LADDER",
            ),
        ]
    )
    await db_session.commit()


@pytest.mark.asyncio
@pytest.mark.integration
class TestModelGeometryGet:
    async def test_lists_rows_with_band_constant(self, async_client: AsyncClient):
        resp = await async_client.get("/api/v1/model-geometry")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # The GET envelope carries the sweep-band minimum so the client reads it
        # from the API, not a hardcoded copy.
        assert body["sweep_band_min_width_mm"] == 10.0
        by_key = {g["model_key"]: g for g in body["geometries"]}
        assert set(by_key) == {"H2S", "H2C"}
        assert by_key["H2S"]["validated"] is True
        assert by_key["H2S"]["bed_x"] == 340.0
        assert by_key["H2C"]["validated"] is False
        assert by_key["H2C"]["env_x_min"] == 25.0


@pytest.mark.asyncio
@pytest.mark.integration
class TestModelGeometryPut:
    async def test_put_flips_validated_and_logs_warning(self, async_client: AsyncClient, caplog):
        with caplog.at_level(logging.WARNING, logger="backend.app.api.routes.model_geometry"):
            resp = await async_client.put(
                "/api/v1/model-geometry/H2C",
                json={"validated": True, "env_x_min": 20.0, "notes": "ladder done"},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["validated"] is True
        assert body["env_x_min"] == 20.0
        assert body["notes"] == "ladder done"
        # The WARNING audit log names the field old->new transitions.
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("model-geometry H2C" in m and "validated" in m for m in warnings), warnings
        assert any("False" in m and "True" in m for m in warnings), warnings

    async def test_put_canonicalises_key(self, async_client: AsyncClient):
        # O1S canonicalises to the H2S row.
        resp = await async_client.put("/api/v1/model-geometry/O1S", json={"max_part_height_mm": 40.0})
        assert resp.status_code == 200, resp.text
        assert resp.json()["model_key"] == "H2S"
        assert resp.json()["max_part_height_mm"] == 40.0

    async def test_put_unknown_model_404(self, async_client: AsyncClient):
        resp = await async_client.put("/api/v1/model-geometry/ZZZ", json={"validated": True})
        assert resp.status_code == 404

    async def test_put_rejects_nonpositive_bed(self, async_client: AsyncClient):
        resp = await async_client.put("/api/v1/model-geometry/H2S", json={"bed_x": 0})
        assert resp.status_code == 422


@pytest.mark.asyncio
@pytest.mark.integration
class TestModelGeometryPermissions:
    async def test_put_forbidden_for_api_key_403(self, async_client: AsyncClient, db_session):
        # EJECT_PROFILES_UPDATE is administrative — never granted to an API key.
        from backend.app.core.auth import generate_api_key
        from backend.app.models.api_key import APIKey
        from backend.app.models.settings import Settings

        db_session.add(Settings(key="auth_enabled", value="true"))
        full_key, key_hash, key_prefix = generate_api_key()
        db_session.add(
            APIKey(
                name="ro-key",
                key_hash=key_hash,
                key_prefix=key_prefix,
                can_read_status=True,
                enabled=True,
            )
        )
        await db_session.commit()

        resp = await async_client.put(
            "/api/v1/model-geometry/H2C",
            json={"validated": True},
            headers={"X-API-Key": full_key},
        )
        assert resp.status_code == 403

    async def test_get_allowed_for_read_key_200(self, async_client: AsyncClient, db_session):
        # EJECT_PROFILES_READ maps to can_read_status → the key may GET.
        from backend.app.core.auth import generate_api_key
        from backend.app.models.api_key import APIKey
        from backend.app.models.settings import Settings

        db_session.add(Settings(key="auth_enabled", value="true"))
        full_key, key_hash, key_prefix = generate_api_key()
        db_session.add(
            APIKey(
                name="read-key",
                key_hash=key_hash,
                key_prefix=key_prefix,
                can_read_status=True,
                enabled=True,
            )
        )
        await db_session.commit()

        resp = await async_client.get("/api/v1/model-geometry", headers={"X-API-Key": full_key})
        assert resp.status_code == 200
