"""Route-level contract for POST /printers/{id}/eject.

The route is a thin translator over ``services.eject.manual.manual_eject``; these
tests pin the wire shape the frontend depends on — most importantly the two-step
foreign-plate ``409 {"code": "foreign_plate", ...}`` detail, a PINNED API contract —
by patching the service and asserting the serialised HTTPException body and that the
new ``eject_profile_id`` body field is threaded through.
"""

from unittest.mock import AsyncMock, patch

import pytest

from backend.app.services.eject.manual import BedTooHot, ForeignPlateEject, ManualEjectError

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

_MANUAL = "backend.app.api.routes.printer_eject.manual_eject"


async def test_foreign_plate_409_detail_shape(async_client, printer_factory):
    printer = await printer_factory(name="EJR", model="H2S")
    exc = ForeignPlateEject(print_name="Foreign Widget", max_z_height_mm=18.0, suggested_eject_profile_id=7)
    with patch(_MANUAL, AsyncMock(side_effect=exc)):
        r = await async_client.post(f"/api/v1/printers/{printer.id}/eject", json={})
    assert r.status_code == 409
    assert r.json()["detail"] == {
        "code": "foreign_plate",
        "message": str(exc),
        "print_name": "Foreign Widget",
        "max_z_height_mm": 18.0,
        "suggested_eject_profile_id": 7,
    }


async def test_eject_profile_id_threaded_through(async_client, printer_factory):
    printer = await printer_factory(name="EJR2", model="H2S")
    mock = AsyncMock(return_value={"mode": "dispatched", "queue_item_id": None})
    with patch(_MANUAL, mock):
        r = await async_client.post(
            f"/api/v1/printers/{printer.id}/eject", json={"eject_profile_id": 42, "allow_hot": True}
        )
    assert r.status_code == 200
    assert r.json() == {"mode": "dispatched", "queue_item_id": None}
    assert mock.await_args.kwargs["eject_profile_id"] == 42
    assert mock.await_args.kwargs["allow_hot"] is True


async def test_bed_hot_still_409(async_client, printer_factory):
    # The pre-existing hot-bed confirm shape is unchanged by the foreign-plate branch.
    printer = await printer_factory(name="EJR3", model="H2S")
    with patch(_MANUAL, AsyncMock(side_effect=BedTooHot(50.0, 33.0))):
        r = await async_client.post(f"/api/v1/printers/{printer.id}/eject", json={})
    assert r.status_code == 409
    assert r.json()["detail"] == {"code": "bed_hot", "bed_c": 50.0, "threshold_c": 33.0}


async def test_other_manual_error_carries_code_and_message(async_client, printer_factory):
    printer = await printer_factory(name="EJR4", model="H2S")
    exc = ManualEjectError("no_eligible_unit", "nothing to eject by hand", status_code=409)
    with patch(_MANUAL, AsyncMock(side_effect=exc)):
        r = await async_client.post(f"/api/v1/printers/{printer.id}/eject", json={})
    assert r.status_code == 409
    assert r.json()["detail"] == {"code": "no_eligible_unit", "message": "nothing to eject by hand"}
