"""Integration tests for POST /api/v1/inventory/spools/respool.

Auth gate (401), Spoolman-mode refusal (409), not-connected (404), no-tag (400),
and a SELECT-verified happy path (fresh locked spool + archived donor).
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.models.spool import Spool
from backend.app.models.spool_usage_history import SpoolUsageHistory

DONOR_TAG_UID = "AABBCCDD11223344"
DONOR_TRAY_UUID = "AABBCCDD11223344AABBCCDD11223344"


def _tray():
    return {
        "tray_type": "PETG",
        "tray_sub_brands": "PETG HF",
        "tray_color": "00FF00FF",
        "tag_uid": DONOR_TAG_UID,
        "tray_uuid": DONOR_TRAY_UUID,
        "tray_info_idx": "GFG99",
        "tray_weight": "1000",
        "state": 11,
        "remain": 100,
    }


def _mock_status(ams_id=0, tray_id=0, tray=None):
    st = MagicMock()
    st.state = "IDLE"
    st.tray_now = 255
    st.nozzles = []
    st.raw_data = {"ams": [{"id": ams_id, "tray": [{"id": tray_id, **(tray or _tray())}]}]}
    return st


@pytest.mark.asyncio
@pytest.mark.integration
async def test_respool_requires_auth_401(async_client: AsyncClient, db_session):
    from backend.app.models.settings import Settings

    db_session.add(Settings(key="auth_enabled", value="true"))
    await db_session.commit()

    resp = await async_client.post(
        "/api/v1/inventory/spools/respool",
        json={"printer_id": 1, "ams_id": 0, "tray_id": 0, "brand": "Polymaker"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
@pytest.mark.integration
async def test_respool_spoolman_mode_409(async_client: AsyncClient, db_session, printer_factory):
    from backend.app.models.settings import Settings

    printer = await printer_factory()
    db_session.add(Settings(key="spoolman_enabled", value="true"))
    await db_session.commit()

    with patch(
        "backend.app.services.printer_manager.printer_manager.get_status",
        return_value=_mock_status(),
    ):
        resp = await async_client.post(
            "/api/v1/inventory/spools/respool",
            json={"printer_id": printer.id, "ams_id": 0, "tray_id": 0, "brand": "Polymaker"},
        )
    assert resp.status_code == 409
    assert "Spoolman" in resp.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_respool_printer_not_connected_404(async_client: AsyncClient, db_session, printer_factory):
    printer = await printer_factory()
    with patch(
        "backend.app.services.printer_manager.printer_manager.get_status",
        return_value=None,
    ):
        resp = await async_client.post(
            "/api/v1/inventory/spools/respool",
            json={"printer_id": printer.id, "ams_id": 0, "tray_id": 0, "brand": "Polymaker"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
@pytest.mark.integration
async def test_respool_no_tag_400(async_client: AsyncClient, db_session, printer_factory):
    printer = await printer_factory()
    no_tag = {**_tray(), "tag_uid": "0000000000000000", "tray_uuid": "00000000000000000000000000000000"}
    with patch(
        "backend.app.services.printer_manager.printer_manager.get_status",
        return_value=_mock_status(tray=no_tag),
    ):
        resp = await async_client.post(
            "/api/v1/inventory/spools/respool",
            json={"printer_id": printer.id, "ams_id": 0, "tray_id": 0, "brand": "Polymaker"},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
@pytest.mark.integration
async def test_respool_happy_path_select_verified(async_client: AsyncClient, db_session, printer_factory):
    printer = await printer_factory()
    donor = Spool(
        material="PETG",
        subtype="HF",
        brand="Bambu Lab",
        label_weight=1000,
        core_weight=250,
        weight_used=990,
        tag_uid=DONOR_TAG_UID,
        tray_uuid=DONOR_TRAY_UUID,
        data_origin="rfid_auto",
        tag_type="bambulab",
        spent_at=datetime.utcnow(),
    )
    donor.k_profiles = []
    donor.assignments = []
    db_session.add(donor)
    await db_session.flush()
    db_session.add(SpoolUsageHistory(spool_id=donor.id, weight_used=500, status="completed"))
    await db_session.commit()
    donor_id = donor.id

    with (
        patch(
            "backend.app.services.printer_manager.printer_manager.get_status",
            return_value=_mock_status(),
        ),
        patch("backend.app.services.printer_manager.printer_manager.get_client", return_value=None),
    ):
        resp = await async_client.post(
            "/api/v1/inventory/spools/respool",
            json={"printer_id": printer.id, "ams_id": 0, "tray_id": 0, "brand": "Polymaker", "label_weight": 1000},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["brand"] == "Polymaker"
    assert body["weight_used"] == 0
    assert body["weight_locked"] is True
    assert body["tag_type"] == "bambulab_reused"
    assert body["spent_at"] is None
    new_id = body["id"]
    assert new_id != donor_id

    # SELECT-verify end state in a fresh read.
    db_session.expire_all()
    new_spool = await db_session.get(Spool, new_id)
    assert new_spool is not None
    assert new_spool.weight_used == 0
    assert new_spool.weight_locked is True
    assert new_spool.tag_uid == DONOR_TAG_UID

    donor_after = await db_session.get(Spool, donor_id)
    assert donor_after.archived_at is not None  # history-bearing donor archived
    assert donor_after.tag_uid is None
