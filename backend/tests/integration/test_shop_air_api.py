"""``GET /api/v1/shop-air`` — the measured shop air and the ONE eject line, over HTTP.

The route is thin: it maps the owner's single read (``shop_air.current_line``) onto
``{value_c, as_of, basis, printers, margin_c, eject_line_c}``. These tests pin that shape
end to end over a seeded sample cache and the margin setting; the qualification and the
estimate themselves are pinned on production-cut fixtures in
``unit/services/test_shop_air.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.app.models.shop_air_sample import ShopAirSample
from backend.app.services.eject import shop_air


async def _seed_fresh(db_session, printer_factory, values) -> datetime:
    """One at-rest sample per printer inside the last hour — a ``fresh`` quorum."""
    now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    for index, value in enumerate(values, start=1):
        printer = await printer_factory(name=f"AIR-{index}", model="H2S")
        db_session.add(
            ShopAirSample(
                printer_id=printer.id,
                recorded_at=now - timedelta(minutes=5 * index),
                value_c=value,
                rule_version=shop_air.RULE_VERSION,
            )
        )
    await db_session.commit()
    return now


@pytest.mark.asyncio
@pytest.mark.integration
class TestShopAirRead:
    async def test_no_samples_reads_unknown_with_no_line(self, async_client):
        r = await async_client.get("/api/v1/shop-air")
        assert r.status_code == 200, r.text
        assert r.json() == {
            "value_c": None,
            "as_of": None,
            "basis": "unknown",
            "printers": 0,
            "margin_c": 2.0,
            "eject_line_c": None,
        }

    async def test_a_fresh_quorum_reads_its_median_plus_the_margin(self, async_client, db_session, printer_factory):
        now = await _seed_fresh(db_session, printer_factory, (27.0, 27.5, 28.0))
        put = await async_client.put("/api/v1/settings/", json={"farm_cooldown_margin_c": 2.5})
        assert put.status_code == 200, put.text

        r = await async_client.get("/api/v1/shop-air")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["basis"] == "fresh"
        assert body["printers"] == 3
        assert body["value_c"] == 27.5
        assert body["margin_c"] == 2.5
        assert body["eject_line_c"] == 30.0
        # UTC on the wire, marked as such — the readout renders "N min ago" from it.
        assert body["as_of"] == (now - timedelta(minutes=5)).isoformat() + "Z"
