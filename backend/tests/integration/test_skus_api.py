"""Integration tests for the SKU catalog API: CRUD + file links + suggest + stats."""

import zipfile

import pytest
from httpx import AsyncClient

_PLATE_GCODE = (
    "; HEADER_BLOCK_START\n"
    "; BambuStudio 02.07.01.57\n"
    "; max_z_height: 20.00\n"
    "; HEADER_BLOCK_END\n"
    "; EXECUTABLE_BLOCK_START\n"
    "M104 S220\n"
    "G1 X100 Y100 E5\n"
    "; EXECUTABLE_BLOCK_END\n"
)

_PRODUCTION_META = {
    "nozzle_diameter": 0.6,
    "filament_type": "PETG",
    "sliced_for_model": "H2S",
    "print_time_seconds": 20636,
    "printable_objects": {"240": "SKU007.01 M18 Hex Impact Driver (#2656-20).stl"},
}


def _write_3mf(path, *, plate_id=1, with_gcode=True):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        if with_gcode:
            zf.writestr(f"Metadata/plate_{plate_id}.gcode", _PLATE_GCODE)
            zf.writestr(f"Metadata/plate_{plate_id}.gcode.md5", "STALEHASH")
        zf.writestr("3D/3dmodel.model", "<model/>")


async def _add_library_file(db_session, tmp_path, *, name="src.gcode.3mf", metadata=None, plate_id=1, with_gcode=True):
    from backend.app.models.library import LibraryFile

    disk = tmp_path / name
    _write_3mf(disk, plate_id=plate_id, with_gcode=with_gcode)
    lib = LibraryFile(
        filename=name,
        file_path=str(disk),
        file_type="gcode.3mf",
        file_size=disk.stat().st_size,
        is_external=True,
        file_metadata=metadata,
    )
    db_session.add(lib)
    await db_session.commit()
    await db_session.refresh(lib)
    return lib


@pytest.mark.asyncio
@pytest.mark.integration
class TestSkuCrud:
    async def test_create_list_get_update_delete(self, async_client: AsyncClient):
        resp = await async_client.post("/api/v1/skus", json={"code": "SKU007.01", "name": "Hex Driver"})
        assert resp.status_code == 201, resp.text
        created = resp.json()
        sid = created["id"]
        assert created["code"] == "SKU007.01"
        assert created["files"] == []
        assert created["stats"]["units_completed"] == 0
        assert created["stats"]["success_rate"] == 0.0

        resp = await async_client.get("/api/v1/skus")
        assert resp.status_code == 200
        assert any(s["id"] == sid for s in resp.json())

        resp = await async_client.get(f"/api/v1/skus/{sid}")
        assert resp.status_code == 200
        assert resp.json()["id"] == sid

        resp = await async_client.put(f"/api/v1/skus/{sid}", json={"part_number": "2656-20", "name": "M18 Hex Driver"})
        assert resp.status_code == 200
        assert resp.json()["part_number"] == "2656-20"
        assert resp.json()["name"] == "M18 Hex Driver"

        resp = await async_client.delete(f"/api/v1/skus/{sid}")
        assert resp.status_code == 204
        assert (await async_client.get(f"/api/v1/skus/{sid}")).status_code == 404

    async def test_duplicate_code_409(self, async_client: AsyncClient):
        await async_client.post("/api/v1/skus", json={"code": "SKU001.01", "name": "A"})
        resp = await async_client.post("/api/v1/skus", json={"code": "SKU001.01", "name": "B"})
        assert resp.status_code == 409

    async def test_update_to_existing_code_409(self, async_client: AsyncClient):
        await async_client.post("/api/v1/skus", json={"code": "SKU010.01", "name": "A"})
        r = await async_client.post("/api/v1/skus", json={"code": "SKU010.02", "name": "B"})
        sid = r.json()["id"]
        resp = await async_client.put(f"/api/v1/skus/{sid}", json={"code": "SKU010.01"})
        assert resp.status_code == 409

    async def test_get_missing_404(self, async_client: AsyncClient):
        assert (await async_client.get("/api/v1/skus/987654")).status_code == 404


@pytest.mark.asyncio
@pytest.mark.integration
class TestSkuFiles:
    async def test_add_file_link_returns_live_capabilities(self, async_client, db_session, tmp_path):
        r = await async_client.post("/api/v1/skus", json={"code": "SKU007.01", "name": "Hex"})
        sid = r.json()["id"]
        lib = await _add_library_file(db_session, tmp_path, name="hex.gcode.3mf", metadata=_PRODUCTION_META)

        resp = await async_client.post(
            f"/api/v1/skus/{sid}/files",
            json={"library_file_id": lib.id, "plate_index": 1, "units_per_plate": 3},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["units_per_plate"] == 3
        assert body["library_file_name"] == "hex.gcode.3mf"
        # Capability facts read live from metadata + gcode header.
        assert body["nozzle_diameter"] == 0.6
        assert body["filament_type"] == "PETG"
        assert body["printer_model"] == "H2S"
        assert body["max_z_height"] == 20.0

        # It appears on the SKU detail.
        detail = (await async_client.get(f"/api/v1/skus/{sid}")).json()
        assert len(detail["files"]) == 1

    async def test_add_file_link_no_gcode_on_plate_422(self, async_client, db_session, tmp_path):
        r = await async_client.post("/api/v1/skus", json={"code": "SKU008.01", "name": "NoPlate"})
        sid = r.json()["id"]
        lib = await _add_library_file(db_session, tmp_path, name="p1.gcode.3mf")  # only plate 1
        resp = await async_client.post(
            f"/api/v1/skus/{sid}/files",
            json={"library_file_id": lib.id, "plate_index": 3, "units_per_plate": 1},
        )
        assert resp.status_code == 422

    async def test_add_file_link_missing_file_404(self, async_client, db_session, tmp_path):
        r = await async_client.post("/api/v1/skus", json={"code": "SKU009.01", "name": "X"})
        sid = r.json()["id"]
        resp = await async_client.post(f"/api/v1/skus/{sid}/files", json={"library_file_id": 987654, "plate_index": 1})
        assert resp.status_code == 404

    async def test_duplicate_file_link_409(self, async_client, db_session, tmp_path):
        r = await async_client.post("/api/v1/skus", json={"code": "SKU011.01", "name": "Dup"})
        sid = r.json()["id"]
        lib = await _add_library_file(db_session, tmp_path, name="dup.gcode.3mf")
        body = {"library_file_id": lib.id, "plate_index": 1, "units_per_plate": 1}
        assert (await async_client.post(f"/api/v1/skus/{sid}/files", json=body)).status_code == 201
        assert (await async_client.post(f"/api/v1/skus/{sid}/files", json=body)).status_code == 409

    async def test_delete_file_link_204(self, async_client, db_session, tmp_path):
        r = await async_client.post("/api/v1/skus", json={"code": "SKU012.01", "name": "Del"})
        sid = r.json()["id"]
        lib = await _add_library_file(db_session, tmp_path, name="del.gcode.3mf")
        link = await async_client.post(f"/api/v1/skus/{sid}/files", json={"library_file_id": lib.id, "plate_index": 1})
        link_id = link.json()["id"]
        assert (await async_client.delete(f"/api/v1/skus/{sid}/files/{link_id}")).status_code == 204
        assert (await async_client.get(f"/api/v1/skus/{sid}")).json()["files"] == []

    async def test_delete_file_link_missing_404(self, async_client):
        r = await async_client.post("/api/v1/skus", json={"code": "SKU013.01", "name": "M"})
        sid = r.json()["id"]
        assert (await async_client.delete(f"/api/v1/skus/{sid}/files/987654")).status_code == 404


@pytest.mark.asyncio
@pytest.mark.integration
class TestSkuSuggest:
    async def test_suggest_from_object_names(self, async_client, db_session, tmp_path):
        lib = await _add_library_file(db_session, tmp_path, name="hex.gcode.3mf", metadata=_PRODUCTION_META)
        resp = await async_client.get(f"/api/v1/skus/suggest?library_file_id={lib.id}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["code"] == "SKU007.01"
        assert body["part_number"] == "2656-20"
        assert body["matched_from"] == "object_name"

    async def test_suggest_missing_file_404(self, async_client):
        assert (await async_client.get("/api/v1/skus/suggest?library_file_id=987654")).status_code == 404


@pytest.mark.asyncio
@pytest.mark.integration
class TestSkuStats:
    async def test_stats_zeros_for_new_sku(self, async_client):
        r = await async_client.post("/api/v1/skus", json={"code": "SKU020.01", "name": "Fresh"})
        sid = r.json()["id"]
        resp = await async_client.get(f"/api/v1/skus/{sid}/stats")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body == {
            "units_completed": 0,
            "units_failed": 0,
            "plates_completed": 0,
            "plates_failed": 0,
            "success_rate": 0.0,
            "median_cycle_seconds": None,
        }

    async def test_stats_missing_sku_404(self, async_client):
        assert (await async_client.get("/api/v1/skus/987654/stats")).status_code == 404
