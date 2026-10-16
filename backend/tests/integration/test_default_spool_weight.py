"""``default_spool_weight_g`` over HTTP (Part 2, 2026-10-16).

The setting round-trips through the route's int whitelist, refuses values outside 50-5000, and is
what a new roll created over the API starts from: Add spool, bulk add and CSV import, each with and
without a stated figure. The mint lanes behind the AMS are pinned in
``tests/unit/services/test_spool_defaults.py``.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.spool import Spool


async def _set(async_client: AsyncClient, grams: int) -> None:
    response = await async_client.put("/api/v1/settings/", json={"default_spool_weight_g": grams})
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
@pytest.mark.integration
class TestTheSetting:
    async def test_default_is_1000(self, async_client: AsyncClient):
        response = await async_client.get("/api/v1/settings/")
        assert response.status_code == 200
        assert response.json()["default_spool_weight_g"] == 1000

    async def test_round_trips(self, async_client: AsyncClient):
        response = await async_client.put("/api/v1/settings/", json={"default_spool_weight_g": 1100})
        assert response.status_code == 200
        assert response.json()["default_spool_weight_g"] == 1100
        # Read back through the int whitelist (a str would fail the equality).
        assert (await async_client.get("/api/v1/settings/")).json()["default_spool_weight_g"] == 1100

    @pytest.mark.parametrize("grams", [49, 5001, -1])
    async def test_rejects_out_of_range(self, async_client: AsyncClient, grams: int):
        response = await async_client.put("/api/v1/settings/", json={"default_spool_weight_g": grams})
        assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.integration
class TestNewRollsOverTheApi:
    async def test_add_spool_without_a_figure_uses_the_setting(self, async_client: AsyncClient):
        await _set(async_client, 1100)
        response = await async_client.post("/api/v1/inventory/spools", json={"material": "PLA"})
        assert response.status_code == 200, response.text
        assert response.json()["label_weight"] == 1100

    async def test_add_spool_with_a_figure_keeps_it(self, async_client: AsyncClient):
        await _set(async_client, 1100)
        response = await async_client.post("/api/v1/inventory/spools", json={"material": "PLA", "label_weight": 750})
        assert response.status_code == 200, response.text
        assert response.json()["label_weight"] == 750

    async def test_bulk_add_without_a_figure_uses_the_setting(self, async_client: AsyncClient):
        await _set(async_client, 1100)
        response = await async_client.post(
            "/api/v1/inventory/spools/bulk", json={"spool": {"material": "PETG"}, "quantity": 3}
        )
        assert response.status_code == 200, response.text
        assert [s["label_weight"] for s in response.json()] == [1100, 1100, 1100]

    async def test_csv_import_fills_an_empty_label_cell_from_the_setting(
        self, async_client: AsyncClient, db_session: AsyncSession
    ):
        await _set(async_client, 1100)
        csv_text = "material,brand,label_weight\nPLA,NoLabel,\nPETG,Stated,800\n"
        response = await async_client.post(
            "/api/v1/inventory/spools/import",
            files={"file": ("inventory.csv", csv_text.encode("utf-8"), "text/csv")},
        )
        assert response.status_code == 200, response.text
        assert response.json()["created"] == 2
        rows = (await db_session.execute(select(Spool.brand, Spool.label_weight).order_by(Spool.id))).all()
        assert [(r.brand, r.label_weight) for r in rows] == [("NoLabel", 1100), ("Stated", 800)]
