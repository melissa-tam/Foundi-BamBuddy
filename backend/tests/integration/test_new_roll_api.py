"""Integration tests for POST /api/v1/inventory/spools/{id}/new-roll.

The merged slot verb over HTTP, exercised on its TAGGED lane (the re-spool successor):
auth gate (401), Spoolman-mode refusal (409), not-connected (404), no-tag (400), the
unbound-row guard (409), and a SELECT-verified happy path (fresh locked spool + archived
donor). Also pins that the two routes this verb replaced are GONE.
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
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


async def _seed_donor(db_session, printer_id, *, used=990, spent=True):
    """A tagged, slot-bound donor row — the shape the verb is keyed by."""
    donor = Spool(
        material="PETG",
        subtype="HF",
        brand="Bambu Lab",
        label_weight=1000,
        core_weight=250,
        weight_used=used,
        tag_uid=DONOR_TAG_UID,
        tray_uuid=DONOR_TRAY_UUID,
        data_origin="rfid_auto",
        tag_type="bambulab",
        spent_at=datetime.utcnow() if spent else None,
    )
    donor.k_profiles = []
    donor.assignments = []
    db_session.add(donor)
    await db_session.flush()
    db_session.add(
        SpoolAssignment(
            spool_id=donor.id,
            printer_id=printer_id,
            ams_id=0,
            tray_id=0,
            fingerprint_color="00FF00FF",
            fingerprint_type="PETG",
        )
    )
    await db_session.commit()
    return donor


@pytest.mark.asyncio
@pytest.mark.integration
async def test_new_roll_requires_auth_401(async_client: AsyncClient, db_session):
    from backend.app.models.settings import Settings

    db_session.add(Settings(key="auth_enabled", value="true"))
    await db_session.commit()

    resp = await async_client.post(
        "/api/v1/inventory/spools/1/new-roll",
        json={"printer_id": 1, "ams_id": 0, "tray_id": 0, "brand": "Polymaker"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
@pytest.mark.integration
async def test_new_roll_unbound_row_409(async_client: AsyncClient, db_session, printer_factory):
    printer = await printer_factory()
    donor = Spool(
        material="PETG",
        brand="Bambu Lab",
        label_weight=1000,
        weight_used=990,
        tag_uid=DONOR_TAG_UID,
        tray_uuid=DONOR_TRAY_UUID,
        data_origin="rfid_auto",
        tag_type="bambulab",
    )
    donor.k_profiles = []
    donor.assignments = []
    db_session.add(donor)
    await db_session.commit()

    resp = await async_client.post(
        f"/api/v1/inventory/spools/{donor.id}/new-roll",
        json={"printer_id": printer.id, "ams_id": 0, "tray_id": 0, "brand": "Polymaker"},
    )
    assert resp.status_code == 409
    assert "not assigned" in resp.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_new_roll_spoolman_mode_409(async_client: AsyncClient, db_session, printer_factory):
    from backend.app.models.settings import Settings

    printer = await printer_factory()
    donor = await _seed_donor(db_session, printer.id)
    db_session.add(Settings(key="spoolman_enabled", value="true"))
    await db_session.commit()

    with patch(
        "backend.app.services.printer_manager.printer_manager.get_status",
        return_value=_mock_status(),
    ):
        resp = await async_client.post(
            f"/api/v1/inventory/spools/{donor.id}/new-roll",
            json={"printer_id": printer.id, "ams_id": 0, "tray_id": 0, "brand": "Polymaker"},
        )
    assert resp.status_code == 409
    assert "Spoolman" in resp.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_new_roll_printer_not_connected_404(async_client: AsyncClient, db_session, printer_factory):
    printer = await printer_factory()
    donor = await _seed_donor(db_session, printer.id)
    with patch(
        "backend.app.services.printer_manager.printer_manager.get_status",
        return_value=None,
    ):
        resp = await async_client.post(
            f"/api/v1/inventory/spools/{donor.id}/new-roll",
            json={"printer_id": printer.id, "ams_id": 0, "tray_id": 0, "brand": "Polymaker"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
@pytest.mark.integration
async def test_new_roll_no_tag_on_the_live_tray_400(async_client: AsyncClient, db_session, printer_factory):
    printer = await printer_factory()
    donor = await _seed_donor(db_session, printer.id)
    no_tag = {**_tray(), "tag_uid": "0000000000000000", "tray_uuid": "00000000000000000000000000000000"}
    with patch(
        "backend.app.services.printer_manager.printer_manager.get_status",
        return_value=_mock_status(tray=no_tag),
    ):
        resp = await async_client.post(
            f"/api/v1/inventory/spools/{donor.id}/new-roll",
            json={"printer_id": printer.id, "ams_id": 0, "tray_id": 0, "brand": "Polymaker"},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
@pytest.mark.integration
async def test_new_roll_tagged_happy_path_select_verified(async_client: AsyncClient, db_session, printer_factory):
    printer = await printer_factory()
    donor = await _seed_donor(db_session, printer.id)
    db_session.add(SpoolUsageHistory(spool_id=donor.id, weight_used=500, status="completed"))
    await db_session.commit()
    donor_id = donor.id
    printer_id = printer.id  # read before the expire_all below — an expired ORM row cannot lazy-load here

    with (
        patch(
            "backend.app.services.printer_manager.printer_manager.get_status",
            return_value=_mock_status(),
        ),
        patch("backend.app.services.printer_manager.printer_manager.get_client", return_value=None),
    ):
        resp = await async_client.post(
            f"/api/v1/inventory/spools/{donor_id}/new-roll",
            json={
                "printer_id": printer.id,
                "ams_id": 0,
                "tray_id": 0,
                "brand": "Polymaker",
                "label_weight": 1000,
            },
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

    # The slot now carries the successor, not the donor.
    bound = await db_session.execute(
        select(SpoolAssignment.spool_id).where(
            SpoolAssignment.printer_id == printer_id,
            SpoolAssignment.ams_id == 0,
            SpoolAssignment.tray_id == 0,
        )
    )
    assert bound.scalar_one() == new_id


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/inventory/spools/respool",
        "/api/v1/inventory/spools/1/tagless-fresh",
    ],
)
async def test_replaced_routes_are_gone(async_client: AsyncClient, path):
    """Delete, don't deprecate: the two verbs `/new-roll` merged no longer answer."""
    resp = await async_client.post(path, json={"printer_id": 1, "ams_id": 0, "tray_id": 0, "brand": "X"})
    assert resp.status_code in (404, 405), resp.text
