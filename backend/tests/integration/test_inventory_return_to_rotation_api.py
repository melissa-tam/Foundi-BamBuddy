"""The operator's "Return to rotation" clears the PAIR (2026-09-11, 002-H2S spool 599).

``OutOfRotationChip`` PATCHes ``{"feed_fault_at": null}``. The generic spool update
used to null exactly the column it named and leave ``feed_fault_code`` standing — a
stale diagnosis on a roll the operator had just declared healthy. The pair now clears
through ``spool_recovery.clear_out_of_rotation``, the one owner of what "back in
rotation" writes.
"""

from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.spool import Spool

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def out_of_rotation_spool(db_session: AsyncSession) -> Spool:
    spool = Spool(
        brand="Bambu",
        material="PETG",
        color_name="Black",
        label_weight=1000.0,
        weight_used=100.0,
        feed_fault_at=datetime.now(timezone.utc).replace(tzinfo=None),
        feed_fault_code="0700_8005",
    )
    db_session.add(spool)
    await db_session.commit()
    await db_session.refresh(spool)
    return spool


class TestReturnToRotation:
    async def test_a_null_feed_fault_at_clears_the_code_too(
        self, async_client: AsyncClient, out_of_rotation_spool: Spool, db_session: AsyncSession
    ):
        response = await async_client.patch(
            f"/api/v1/inventory/spools/{out_of_rotation_spool.id}", json={"feed_fault_at": None}
        )

        assert response.status_code == 200
        db_session.expunge_all()
        row = (await db_session.execute(select(Spool).where(Spool.id == out_of_rotation_spool.id))).scalar_one()
        assert row.feed_fault_at is None
        assert row.feed_fault_code is None
        assert response.json()["feed_fault_code"] is None

    async def test_other_fields_still_update_in_the_same_call(
        self, async_client: AsyncClient, out_of_rotation_spool: Spool, db_session: AsyncSession
    ):
        response = await async_client.patch(
            f"/api/v1/inventory/spools/{out_of_rotation_spool.id}",
            json={"feed_fault_at": None, "note": "returned after a feeder clean"},
        )

        assert response.status_code == 200
        db_session.expunge_all()
        row = (await db_session.execute(select(Spool).where(Spool.id == out_of_rotation_spool.id))).scalar_one()
        assert (row.feed_fault_at, row.feed_fault_code, row.note) == (None, None, "returned after a feeder clean")

    async def test_a_spool_already_in_rotation_is_untouched(self, async_client: AsyncClient, db_session: AsyncSession):
        spool = Spool(brand="Bambu", material="PETG", color_name="Black", label_weight=1000.0, weight_used=0.0)
        db_session.add(spool)
        await db_session.commit()
        await db_session.refresh(spool)

        response = await async_client.patch(f"/api/v1/inventory/spools/{spool.id}", json={"feed_fault_at": None})

        assert response.status_code == 200
        assert response.json()["feed_fault_at"] is None
