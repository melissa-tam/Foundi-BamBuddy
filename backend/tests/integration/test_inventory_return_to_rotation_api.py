"""The operator's "Return to rotation" clears the PAIR (2026-09-11, 002-H2S spool 599).

``OutOfRotationChip`` PATCHes ``{"feed_fault_at": null}``. The generic spool update
used to null exactly the column it named and leave ``feed_fault_code`` standing — a
stale diagnosis on a roll the operator had just declared healthy. The pair now clears
through ``spool_recovery.clear_out_of_rotation``, the one owner of what "back in
rotation" writes.

The other half of the same contract: the field is CLEAR-ONLY. A non-null value used to
reach the column through a bare ``setattr`` and park a roll with a NULL diagnosis —
the mirror of the pair bug above — so the schema refuses it (422).
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


class TestFeedFaultAtIsClearOnly:
    """Parking a roll is the recovery driver's verdict about a live fault, and it writes
    the PAIR. Over the API there is no "take this out of service" — only the clear."""

    async def test_a_non_null_feed_fault_at_is_refused(
        self, async_client: AsyncClient, out_of_rotation_spool: Spool, db_session: AsyncSession
    ):
        response = await async_client.patch(
            f"/api/v1/inventory/spools/{out_of_rotation_spool.id}",
            json={"feed_fault_at": "2026-09-21T04:57:35"},
        )

        assert response.status_code == 422
        assert "clear-only" in response.text
        db_session.expunge_all()
        row = (await db_session.execute(select(Spool).where(Spool.id == out_of_rotation_spool.id))).scalar_one()
        assert (row.feed_fault_at, row.feed_fault_code) == (
            out_of_rotation_spool.feed_fault_at,
            "0700_8005",
        ), "the refused call wrote nothing at all"

    async def test_a_non_null_value_cannot_park_a_healthy_spool(
        self, async_client: AsyncClient, db_session: AsyncSession
    ):
        """The bug this closes: a PATCH could park a roll with a NULL diagnosis, which no
        clear-on-reinsert or operator chip explains and no code identifies."""
        spool = Spool(brand="Bambu", material="PETG", color_name="Black", label_weight=1000.0, weight_used=0.0)
        db_session.add(spool)
        await db_session.commit()
        await db_session.refresh(spool)

        response = await async_client.patch(
            f"/api/v1/inventory/spools/{spool.id}", json={"feed_fault_at": "2026-09-21T04:57:35"}
        )

        assert response.status_code == 422
        db_session.expunge_all()
        row = (await db_session.execute(select(Spool).where(Spool.id == spool.id))).scalar_one()
        assert (row.feed_fault_at, row.feed_fault_code) == (None, None), "still in rotation"

    async def test_omitting_the_field_leaves_the_stamp_standing(
        self, async_client: AsyncClient, out_of_rotation_spool: Spool, db_session: AsyncSession
    ):
        """``exclude_unset`` semantics are load-bearing here: a clear-only field must not
        force every unrelated edit to carry it."""
        response = await async_client.patch(
            f"/api/v1/inventory/spools/{out_of_rotation_spool.id}", json={"note": "left parked on purpose"}
        )

        assert response.status_code == 200
        db_session.expunge_all()
        row = (await db_session.execute(select(Spool).where(Spool.id == out_of_rotation_spool.id))).scalar_one()
        assert row.feed_fault_at is not None and row.feed_fault_code == "0700_8005"
        assert row.note == "left parked on purpose"


class TestBulkUpdateSharesTheOneClear:
    """The bulk lane applies the SAME prepared payload to every spool in the batch, and
    it used to do so through a bare ``setattr`` loop — a second copy of the pair bug,
    one route over. Both routes now land through ``_apply_spool_update``."""

    @pytest.fixture
    async def two_parked_spools(self, db_session: AsyncSession) -> list[Spool]:
        spools = [
            Spool(
                brand="Bambu",
                material="PETG",
                color_name=name,
                label_weight=1000.0,
                weight_used=100.0,
                feed_fault_at=datetime.now(timezone.utc).replace(tzinfo=None),
                feed_fault_code=code,
            )
            for name, code in (("Black", "0700_8005"), ("Jade White", "0300_801E"))
        ]
        db_session.add_all(spools)
        await db_session.commit()
        for spool in spools:
            await db_session.refresh(spool)
        return spools

    async def test_bulk_null_clears_the_pair_on_every_spool(
        self, async_client: AsyncClient, two_parked_spools: list[Spool], db_session: AsyncSession
    ):
        ids = [s.id for s in two_parked_spools]

        response = await async_client.post(
            "/api/v1/inventory/spools/bulk-update", json={"ids": ids, "update": {"feed_fault_at": None}}
        )

        assert response.status_code == 200
        assert response.json()["updated"] == 2
        db_session.expunge_all()
        rows = (await db_session.execute(select(Spool).where(Spool.id.in_(ids)).order_by(Spool.id))).scalars().all()
        assert [(r.feed_fault_at, r.feed_fault_code) for r in rows] == [(None, None), (None, None)], (
            "both columns, on both rolls — the clear does not decay into a flag-only null "
            "just because it arrived over the batch route"
        )

    async def test_bulk_non_null_is_refused(
        self, async_client: AsyncClient, two_parked_spools: list[Spool], db_session: AsyncSession
    ):
        """The schema is the gate, so the batch route inherits the 422 — no second guard."""
        ids = [s.id for s in two_parked_spools]

        response = await async_client.post(
            "/api/v1/inventory/spools/bulk-update",
            json={"ids": ids, "update": {"feed_fault_at": "2026-09-21T04:57:35"}},
        )

        assert response.status_code == 422
        assert "clear-only" in response.text
        db_session.expunge_all()
        rows = (await db_session.execute(select(Spool).where(Spool.id.in_(ids)).order_by(Spool.id))).scalars().all()
        assert [r.feed_fault_code for r in rows] == ["0700_8005", "0300_801E"], "the refused batch wrote nothing"

    async def test_an_unrelated_bulk_edit_leaves_every_stamp_standing(
        self, async_client: AsyncClient, two_parked_spools: list[Spool], db_session: AsyncSession
    ):
        """The payload is shared across the batch, so a field the helper skips for one
        spool must not go missing for the next — the regression a per-spool ``pop`` on
        the shared dict would have introduced."""
        ids = [s.id for s in two_parked_spools]

        response = await async_client.post(
            "/api/v1/inventory/spools/bulk-update",
            json={"ids": ids, "update": {"feed_fault_at": None, "storage_location": "Shelf B"}},
        )

        assert response.status_code == 200
        db_session.expunge_all()
        rows = (await db_session.execute(select(Spool).where(Spool.id.in_(ids)).order_by(Spool.id))).scalars().all()
        assert [(r.feed_fault_at, r.feed_fault_code, r.storage_location) for r in rows] == [
            (None, None, "Shelf B"),
            (None, None, "Shelf B"),
        ], "every spool in the batch got BOTH the clear and the ordinary column write"
