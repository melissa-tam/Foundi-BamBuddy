"""Integration tests for POST /api/v1/inventory/spools/{id}/respool-dismiss.

Persists the operator's tier-3 "Same spool" answer (``respool_dismissed_at``),
is idempotent, 404s an unknown spool, and broadcasts ``respool_prompt_dismissed``
carrying the optional AMS slot triple so open clients drop the matching prompt.
"""

import pytest
from httpx import AsyncClient

from backend.app.models.spool import Spool


@pytest.fixture
async def spool_factory(db_session):
    """Create a near-empty Spool (a plausible tier-3 prompt subject)."""

    async def _create(**kwargs):
        defaults = {
            "material": "PETG",
            "subtype": "HF",
            "brand": "Bambu Lab",
            "rgba": "00FF00FF",
            "label_weight": 1000,
            "weight_used": 990.0,
        }
        defaults.update(kwargs)
        spool = Spool(**defaults)
        db_session.add(spool)
        await db_session.commit()
        await db_session.refresh(spool)
        return spool

    return _create


@pytest.mark.asyncio
@pytest.mark.integration
async def test_dismiss_stamps_timestamp_select_verified(async_client: AsyncClient, spool_factory, db_session):
    """200 stamps respool_dismissed_at; a fresh SELECT proves the durable write."""
    spool = await spool_factory()
    assert spool.respool_dismissed_at is None

    resp = await async_client.post(
        f"/api/v1/inventory/spools/{spool.id}/respool-dismiss",
        json={"printer_id": 1, "ams_id": 0, "tray_id": 2},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["respool_dismissed_at"] is not None

    # SELECT-verify the durable write in a genuinely fresh instance. The endpoint
    # committed on its own session; evict the factory row from our identity map so
    # this is a clean load, not an expired-row merge (which would fault sync IO).
    spool_id = spool.id
    db_session.expunge_all()
    refetched = await db_session.get(Spool, spool_id)
    assert refetched is not None
    assert refetched.respool_dismissed_at is not None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_dismiss_is_idempotent(async_client: AsyncClient, spool_factory):
    """A second dismissal of an already-dismissed spool is a fine 200 (re-stamp)."""
    spool = await spool_factory()

    first = await async_client.post(f"/api/v1/inventory/spools/{spool.id}/respool-dismiss", json={})
    assert first.status_code == 200
    assert first.json()["respool_dismissed_at"] is not None

    second = await async_client.post(f"/api/v1/inventory/spools/{spool.id}/respool-dismiss", json={})
    assert second.status_code == 200
    assert second.json()["respool_dismissed_at"] is not None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_dismiss_unknown_spool_404(async_client: AsyncClient):
    resp = await async_client.post("/api/v1/inventory/spools/999999/respool-dismiss", json={})
    assert resp.status_code == 404


@pytest.mark.asyncio
@pytest.mark.integration
async def test_dismiss_broadcasts_with_slot_triple(async_client: AsyncClient, spool_factory, monkeypatch):
    """The WS broadcast carries the slot triple verbatim (nulls when absent)."""
    from backend.app.core.websocket import ws_manager

    spool = await spool_factory()
    captured: list[dict] = []

    async def _spy(msg):
        captured.append(msg)

    monkeypatch.setattr(ws_manager, "broadcast", _spy)

    resp = await async_client.post(
        f"/api/v1/inventory/spools/{spool.id}/respool-dismiss",
        json={"printer_id": 7, "ams_id": 1, "tray_id": 3},
    )
    assert resp.status_code == 200
    events = [m for m in captured if m.get("type") == "respool_prompt_dismissed"]
    assert events == [
        {
            "type": "respool_prompt_dismissed",
            "spool_id": spool.id,
            "printer_id": 7,
            "ams_id": 1,
            "tray_id": 3,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_dismiss_broadcast_nulls_when_no_slot(async_client: AsyncClient, spool_factory, monkeypatch):
    """No slot in the body → the broadcast still fires with null slot fields."""
    from backend.app.core.websocket import ws_manager

    spool = await spool_factory()
    captured: list[dict] = []

    async def _spy(msg):
        captured.append(msg)

    monkeypatch.setattr(ws_manager, "broadcast", _spy)

    resp = await async_client.post(f"/api/v1/inventory/spools/{spool.id}/respool-dismiss", json={})
    assert resp.status_code == 200
    events = [m for m in captured if m.get("type") == "respool_prompt_dismissed"]
    assert events == [
        {
            "type": "respool_prompt_dismissed",
            "spool_id": spool.id,
            "printer_id": None,
            "ams_id": None,
            "tray_id": None,
        }
    ]
