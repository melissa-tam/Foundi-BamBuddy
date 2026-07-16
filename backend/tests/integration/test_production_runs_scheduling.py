"""Integration tests for scheduled production runs (Phase 5).

A run can carry a one-time deferred start: the operator's ``scheduled_start_at`` is
stamped onto every plate item's ``scheduled_time`` (the existing scheduler gate then
holds dispatch until then). Reschedule / Start-now re-stamp or clear those items; the
run-level ``scheduled_start_at`` in the response is DERIVED from them.
"""

import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

_PLATE_GCODE = (
    "; HEADER_BLOCK_START\n"
    "; max_z_height: 20.00\n"
    "; HEADER_BLOCK_END\n"
    "; EXECUTABLE_BLOCK_START\n"
    "G1 X100 Y100 E5\n"
    "; EXECUTABLE_BLOCK_END\n"
)
_META = {"nozzle_diameter": 0.6, "filament_type": "PETG", "sliced_for_model": "H2S", "print_time_seconds": 1000}


def _write_3mf(path, plate_id=1):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"Metadata/plate_{plate_id}.gcode", _PLATE_GCODE)
        zf.writestr(f"Metadata/plate_{plate_id}.gcode.md5", "STALE")
        zf.writestr("3D/3dmodel.model", "<model/>")


async def _add_library_file(db_session, tmp_path, name="run.gcode.3mf", plate_id=1):
    from backend.app.models.library import LibraryFile

    disk = tmp_path / name
    _write_3mf(disk, plate_id)
    lib = LibraryFile(
        filename=name,
        file_path=str(disk),
        file_type="gcode.3mf",
        file_size=disk.stat().st_size,
        is_external=True,
        file_metadata=_META,
    )
    db_session.add(lib)
    await db_session.commit()
    await db_session.refresh(lib)
    return lib


async def _make_eject_profile(async_client, name):
    r = await async_client.post("/api/v1/eject-profiles", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _make_sku_with_file(async_client, db_session, tmp_path, *, code, units_per_plate=1):
    r = await async_client.post("/api/v1/skus", json={"code": code, "name": f"{code} thing"})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    lib = await _add_library_file(db_session, tmp_path, name=f"{code}.gcode.3mf")
    r = await async_client.post(
        f"/api/v1/skus/{sid}/files",
        json={"library_file_id": lib.id, "plate_index": 1, "units_per_plate": units_per_plate},
    )
    assert r.status_code == 201, r.text
    return sid, r.json()["id"]


async def _items(db_session, run_id):
    from backend.app.models.print_queue import PrintQueueItem

    result = await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.batch_id == run_id))
    return list(result.scalars().all())


def _iso_in(hours):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


async def _create_run(async_client, db_session, tmp_path, code, *, scheduled=None, require_fa=False, units=2):
    eject = await _make_eject_profile(async_client, name=f"ep-{code}")
    _, file_link_id = await _make_sku_with_file(async_client, db_session, tmp_path, code=code)
    body = {
        "sku_file_id": file_link_id,
        "target_units": units,
        "target_model": "H2S",
        "eject_profile_id": eject,
        "require_first_article": require_fa,
    }
    if scheduled is not None:
        body["scheduled_start_at"] = scheduled
    resp = await async_client.post("/api/v1/production-runs", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.asyncio
@pytest.mark.integration
class TestScheduledCreate:
    async def test_future_start_stamps_all_plates(self, async_client, db_session, tmp_path):
        body = await _create_run(async_client, db_session, tmp_path, "SKU200.01", scheduled=_iso_in(2))
        # Response echoes a derived (future) scheduled_start_at.
        assert body["scheduled_start_at"] is not None
        assert body["status"] == "active"
        items = await _items(db_session, body["id"])
        assert items
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        assert all(it.scheduled_time is not None and it.scheduled_time > now for it in items)

    async def test_gated_run_stamps_the_first_article(self, async_client, db_session, tmp_path):
        body = await _create_run(async_client, db_session, tmp_path, "SKU201.01", scheduled=_iso_in(2), require_fa=True)
        items = await _items(db_session, body["id"])
        # A gated run creates ONLY the first-article plate now — and it is stamped.
        assert len(items) == 1 and items[0].first_article
        assert items[0].scheduled_time is not None
        assert body["scheduled_start_at"] is not None

    async def test_past_start_is_immediate(self, async_client, db_session, tmp_path):
        body = await _create_run(async_client, db_session, tmp_path, "SKU202.01", scheduled=_iso_in(-2))
        assert body["scheduled_start_at"] is None
        items = await _items(db_session, body["id"])
        assert items and all(it.scheduled_time is None for it in items)

    async def test_no_schedule_is_asap(self, async_client, db_session, tmp_path):
        body = await _create_run(async_client, db_session, tmp_path, "SKU203.01")
        assert body["scheduled_start_at"] is None
        items = await _items(db_session, body["id"])
        assert items and all(it.scheduled_time is None for it in items)

    async def test_detail_units_carry_scheduled_time(self, async_client, db_session, tmp_path):
        body = await _create_run(async_client, db_session, tmp_path, "SKU204.01", scheduled=_iso_in(2))
        detail = await async_client.get(f"/api/v1/production-runs/{body['id']}")
        assert detail.status_code == 200, detail.text
        units = detail.json()["units"]
        assert units and all(u["scheduled_time"] is not None for u in units)


@pytest.mark.asyncio
@pytest.mark.integration
class TestReschedule:
    async def test_reschedule_future_restamps_items(self, async_client, db_session, tmp_path):
        body = await _create_run(async_client, db_session, tmp_path, "SKU210.01", scheduled=_iso_in(2))
        run_id = body["id"]
        resp = await async_client.post(
            f"/api/v1/production-runs/{run_id}/reschedule", json={"scheduled_start_at": _iso_in(5)}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["scheduled_start_at"] is not None
        items = await _items(db_session, run_id)
        # Every pending plate now carries the new (further-out) time.
        later = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=4)
        assert items and all(it.scheduled_time is not None and it.scheduled_time > later for it in items)

    async def test_start_now_clears_the_gate(self, async_client, db_session, tmp_path):
        body = await _create_run(async_client, db_session, tmp_path, "SKU211.01", scheduled=_iso_in(2))
        run_id = body["id"]
        resp = await async_client.post(
            f"/api/v1/production-runs/{run_id}/reschedule", json={"scheduled_start_at": None}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["scheduled_start_at"] is None
        items = await _items(db_session, run_id)
        assert items and all(it.scheduled_time is None for it in items)

    async def test_reschedule_past_is_start_now(self, async_client, db_session, tmp_path):
        body = await _create_run(async_client, db_session, tmp_path, "SKU212.01", scheduled=_iso_in(2))
        run_id = body["id"]
        resp = await async_client.post(
            f"/api/v1/production-runs/{run_id}/reschedule", json={"scheduled_start_at": _iso_in(-1)}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["scheduled_start_at"] is None

    async def test_schedule_an_asap_run(self, async_client, db_session, tmp_path):
        # A not-yet-started ASAP run can be moved onto a schedule (free generalization).
        body = await _create_run(async_client, db_session, tmp_path, "SKU213.01")
        run_id = body["id"]
        resp = await async_client.post(
            f"/api/v1/production-runs/{run_id}/reschedule", json={"scheduled_start_at": _iso_in(3)}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["scheduled_start_at"] is not None

    async def test_reschedule_started_run_409(self, async_client, db_session, tmp_path):
        body = await _create_run(async_client, db_session, tmp_path, "SKU214.01", scheduled=_iso_in(2))
        run_id = body["id"]
        # Simulate the run having begun: one plate started printing.
        items = await _items(db_session, run_id)
        items[0].status = "printing"
        items[0].started_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await db_session.commit()
        resp = await async_client.post(
            f"/api/v1/production-runs/{run_id}/reschedule", json={"scheduled_start_at": _iso_in(5)}
        )
        assert resp.status_code == 409, resp.text

    async def test_reschedule_unknown_run_404(self, async_client):
        resp = await async_client.post(
            "/api/v1/production-runs/987654/reschedule", json={"scheduled_start_at": None}
        )
        assert resp.status_code == 404
