"""Integration tests for the production-run API: creation + lifecycle."""

import zipfile
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.services.dispatch_target import decode_printer_ids

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


async def _make_eject_profile(async_client, name="petg-textured-pei"):
    r = await async_client.post("/api/v1/eject-profiles", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _make_sku_with_file(
    async_client,
    db_session,
    tmp_path,
    *,
    code,
    units_per_plate=1,
    plate_index=1,
    default_eject_profile_id=None,
    name="file.gcode.3mf",
):
    payload = {"code": code, "name": f"{code} thing"}
    if default_eject_profile_id is not None:
        payload["default_eject_profile_id"] = default_eject_profile_id
    r = await async_client.post("/api/v1/skus", json=payload)
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    lib = await _add_library_file(db_session, tmp_path, name=name, plate_id=plate_index)
    r = await async_client.post(
        f"/api/v1/skus/{sid}/files",
        json={"library_file_id": lib.id, "plate_index": plate_index, "units_per_plate": units_per_plate},
    )
    assert r.status_code == 201, r.text
    return sid, r.json()["id"]


async def _pending_items(db_session, run_id):
    from backend.app.models.print_queue import PrintQueueItem

    result = await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.batch_id == run_id))
    return list(result.scalars().all())


@pytest.mark.asyncio
@pytest.mark.integration
class TestProductionRunCreate:
    async def test_create_target_model_happy(self, async_client, db_session, tmp_path):
        eject = await _make_eject_profile(async_client)
        _, file_link_id = await _make_sku_with_file(async_client, db_session, tmp_path, code="SKU007.01")
        resp = await async_client.post(
            "/api/v1/production-runs",
            json={"sku_file_id": file_link_id, "target_units": 2, "target_model": "H2S", "eject_profile_id": eject},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["plates_total"] == 2
        assert body["units_planned"] == 2
        assert body["plates_pending"] == 2
        assert body["plates_completed"] == 0
        assert body["sku_code"] == "SKU007.01"
        assert body["status"] == "active"

    async def test_overproduction_plates_math(self, async_client, db_session, tmp_path):
        eject = await _make_eject_profile(async_client, name="ep-over")
        _, file_link_id = await _make_sku_with_file(
            async_client, db_session, tmp_path, code="SKU014.01", units_per_plate=3
        )
        resp = await async_client.post(
            "/api/v1/production-runs",
            json={"sku_file_id": file_link_id, "target_units": 10, "target_model": "H2S", "eject_profile_id": eject},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        # ceil(10/3) = 4 plates → 12 units planned (over-production).
        assert body["plates_total"] == 4
        assert body["units_planned"] == 12

    async def test_eject_falls_back_to_sku_default(self, async_client, db_session, tmp_path):
        eject = await _make_eject_profile(async_client, name="ep-default")
        _, file_link_id = await _make_sku_with_file(
            async_client, db_session, tmp_path, code="SKU015.01", default_eject_profile_id=eject
        )
        resp = await async_client.post(
            "/api/v1/production-runs",
            json={"sku_file_id": file_link_id, "target_units": 1, "target_model": "H2S"},
        )
        assert resp.status_code == 201, resp.text
        # The queue items carry the SKU's default eject profile.
        items = await _pending_items(db_session, resp.json()["id"])
        assert items and all(it.eject_profile_id == eject for it in items)

    async def test_missing_library_file_422(self, async_client, db_session, tmp_path):
        """A SKU file whose library file is gone must refuse at CREATION.

        ``library_file_id`` is what the scheduler uploads and what
        ``locate_3mf_for_print`` resolves the donor from, so a run created over a
        missing row can only fail or wait forever at dispatch — every unit sitting
        on ``library_file_missing`` with nothing the operator can act on. The row
        goes missing legitimately: deleting a library file CASCADEs its
        ``sku_files`` rows away, and on SQLite (which enforces no FK at all, the
        farm's production engine) the link survives the file it points at.
        """
        from sqlalchemy import delete as sa_delete

        from backend.app.models.library import LibraryFile

        eject = await _make_eject_profile(async_client, name="ep-missing-file")
        _, file_link_id = await _make_sku_with_file(
            async_client, db_session, tmp_path, code="SKU017.01", name="missing.gcode.3mf"
        )
        await db_session.execute(sa_delete(LibraryFile).where(LibraryFile.filename == "missing.gcode.3mf"))
        await db_session.commit()

        resp = await async_client.post(
            "/api/v1/production-runs",
            json={"sku_file_id": file_link_id, "target_units": 1, "target_model": "H2S", "eject_profile_id": eject},
        )
        assert resp.status_code == 422, resp.text
        assert "no library file" in resp.json()["detail"]

    async def test_no_eject_profile_422(self, async_client, db_session, tmp_path):
        _, file_link_id = await _make_sku_with_file(async_client, db_session, tmp_path, code="SKU016.01")
        resp = await async_client.post(
            "/api/v1/production-runs",
            json={"sku_file_id": file_link_id, "target_units": 1, "target_model": "H2S"},
        )
        assert resp.status_code == 422

    async def test_missing_sku_file_404(self, async_client):
        eject = await _make_eject_profile(async_client, name="ep-404")
        resp = await async_client.post(
            "/api/v1/production-runs",
            json={"sku_file_id": 987654, "target_units": 1, "target_model": "H2S", "eject_profile_id": eject},
        )
        assert resp.status_code == 404

    async def test_both_targets_422(self, async_client, db_session, tmp_path):
        eject = await _make_eject_profile(async_client, name="ep-both")
        _, file_link_id = await _make_sku_with_file(async_client, db_session, tmp_path, code="SKU017.01")
        resp = await async_client.post(
            "/api/v1/production-runs",
            json={
                "sku_file_id": file_link_id,
                "target_units": 1,
                "target_model": "H2S",
                "printer_ids": [1],
                "eject_profile_id": eject,
            },
        )
        assert resp.status_code == 422

    async def test_neither_target_422(self, async_client, db_session, tmp_path):
        eject = await _make_eject_profile(async_client, name="ep-none")
        _, file_link_id = await _make_sku_with_file(async_client, db_session, tmp_path, code="SKU018.01")
        resp = await async_client.post(
            "/api/v1/production-runs",
            json={"sku_file_id": file_link_id, "target_units": 1, "eject_profile_id": eject},
        )
        assert resp.status_code == 422

    async def test_printer_ids_not_found_422(self, async_client, db_session, tmp_path):
        eject = await _make_eject_profile(async_client, name="ep-badprinter")
        _, file_link_id = await _make_sku_with_file(async_client, db_session, tmp_path, code="SKU019.01")
        resp = await async_client.post(
            "/api/v1/production-runs",
            json={"sku_file_id": file_link_id, "target_units": 1, "printer_ids": [987654], "eject_profile_id": eject},
        )
        assert resp.status_code == 422

    async def test_printer_ids_create_a_pool(self, async_client, db_session, tmp_path, printer_factory):
        """``printer_ids`` is a POOL, not a creation-time round-robin.

        Every plate is minted unplaced — the scheduler picks the next idle member at
        dispatch time — so no pending row carries a ``printer_id`` (on a pending row
        that column is an operator PIN and nothing else). ``printers`` lists where
        units have LANDED, which is nowhere yet; ``target_printers`` is what the
        operator chose.
        """
        p1 = await printer_factory(name="P1", model="H2S")
        p2 = await printer_factory(name="P2", model="H2S")
        eject = await _make_eject_profile(async_client, name="ep-rr")
        _, file_link_id = await _make_sku_with_file(async_client, db_session, tmp_path, code="SKU021.01")
        resp = await async_client.post(
            "/api/v1/production-runs",
            json={
                "sku_file_id": file_link_id,
                "target_units": 3,
                "printer_ids": [p1.id, p2.id],
                "eject_profile_id": eject,
                # Disable the first-article gate so all 3 plates are created up
                # front (a gated run defers the rest until FA approval).
                "require_first_article": False,
            },
        )
        assert resp.status_code == 201, resp.text
        items = await _pending_items(db_session, resp.json()["id"])
        assert len(items) == 3
        for it in items:
            assert it.printer_id is None
            assert decode_printer_ids(it.target_printer_ids) == {p1.id, p2.id}
            assert it.target_model is None
        body = resp.json()
        assert body["target_printers"] == [{"id": p1.id, "name": "P1"}, {"id": p2.id, "name": "P2"}]
        assert body["printers"] == []

    async def test_printer_pool_run_detail_lists_both_members(
        self, async_client, db_session, tmp_path, printer_factory
    ):
        # The run-detail panel lists the scheduler's UNIVERSE for the pool, even
        # though no plate has been placed on either member yet.
        p1 = await printer_factory(name="POOL-1", model="H2S")
        p2 = await printer_factory(name="POOL-2", model="H2S")
        eject = await _make_eject_profile(async_client, name="ep-pool-detail")
        _, file_link_id = await _make_sku_with_file(async_client, db_session, tmp_path, code="SKU022.01")
        created = await async_client.post(
            "/api/v1/production-runs",
            json={
                "sku_file_id": file_link_id,
                "target_units": 2,
                "printer_ids": [p1.id, p2.id],
                "eject_profile_id": eject,
                "require_first_article": False,
            },
        )
        assert created.status_code == 201, created.text

        detail = await async_client.get(f"/api/v1/production-runs/{created.json()['id']}")
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert {s["printer_id"] for s in body["printer_states"]} == {p1.id, p2.id}
        assert body["target_printers"] == [{"id": p1.id, "name": "POOL-1"}, {"id": p2.id, "name": "POOL-2"}]


@pytest.mark.asyncio
@pytest.mark.integration
class TestProductionRunLifecycle:
    async def _create_run(self, async_client, db_session, tmp_path, code):
        eject = await _make_eject_profile(async_client, name=f"ep-{code}")
        _, file_link_id = await _make_sku_with_file(
            async_client, db_session, tmp_path, code=code, name=f"{code}.gcode.3mf"
        )
        resp = await async_client.post(
            "/api/v1/production-runs",
            json={"sku_file_id": file_link_id, "target_units": 2, "target_model": "H2S", "eject_profile_id": eject},
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    async def test_pause_sets_manual_start(self, async_client, db_session, tmp_path):
        run_id = await self._create_run(async_client, db_session, tmp_path, "SKU030.01")
        resp = await async_client.post(f"/api/v1/production-runs/{run_id}/pause")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "paused"
        items = await _pending_items(db_session, run_id)
        assert items and all(it.manual_start for it in items)

    async def test_resume_clears_manual_start(self, async_client, db_session, tmp_path):
        run_id = await self._create_run(async_client, db_session, tmp_path, "SKU031.01")
        await async_client.post(f"/api/v1/production-runs/{run_id}/pause")
        resp = await async_client.post(f"/api/v1/production-runs/{run_id}/resume")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "active"
        items = await _pending_items(db_session, run_id)
        assert items and all(not it.manual_start for it in items)

    async def test_abort_cancels_pending_items(self, async_client, db_session, tmp_path):
        run_id = await self._create_run(async_client, db_session, tmp_path, "SKU032.01")
        resp = await async_client.post(f"/api/v1/production-runs/{run_id}/abort")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "cancelled"
        items = await _pending_items(db_session, run_id)
        assert items and all(it.status == "cancelled" for it in items)
        # Terminal rows: the shared cancel primitive stamps completed_at (the queue
        # routes always did; run-abort joined them) and NULLs any hold token.
        assert all(it.completed_at is not None for it in items)
        assert all(it.waiting_reason is None for it in items)

    async def test_abort_leaves_a_unit_dispatched_in_the_gap_printing(self, async_client, db_session, tmp_path):
        """The 2026-08-20 lost update: abort must not overwrite a live dispatch.

        Run 112 / item 865 on 010-H2S — the scheduler committed status='printing'
        + started_at + ams_mapping one second before the operator aborted, and the
        abort's stale ORM view wrote 'cancelled' over the status column alone. The
        printer kept printing a job no farm unit claimed.
        """
        run_id = await self._create_run(async_client, db_session, tmp_path, "SKU035.01")
        items = sorted(await _pending_items(db_session, run_id), key=lambda it: it.id)
        assert len(items) >= 2
        dispatched, still_pending = items[0], items[1]

        dispatched.status = "printing"
        dispatched.started_at = datetime(2026, 8, 20, 21, 21, 56, tzinfo=timezone.utc)
        dispatched.ams_mapping = "[1]"
        still_pending.waiting_reason = "filament_short"
        await db_session.commit()

        resp = await async_client.post(f"/api/v1/production-runs/{run_id}/abort")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "cancelled"

        for item in (dispatched, still_pending):
            await db_session.refresh(item)

        assert dispatched.status == "printing"
        assert dispatched.started_at is not None
        assert dispatched.ams_mapping == "[1]"
        assert dispatched.completed_at is None

        assert still_pending.status == "cancelled"
        assert still_pending.completed_at is not None
        assert still_pending.waiting_reason is None

    async def test_invalid_transition_409(self, async_client, db_session, tmp_path):
        run_id = await self._create_run(async_client, db_session, tmp_path, "SKU033.01")
        # resume without pause is invalid from 'active'
        assert (await async_client.post(f"/api/v1/production-runs/{run_id}/resume")).status_code == 409
        # abort then pause is invalid from 'cancelled'
        await async_client.post(f"/api/v1/production-runs/{run_id}/abort")
        assert (await async_client.post(f"/api/v1/production-runs/{run_id}/pause")).status_code == 409

    async def test_get_and_list_only_farm_runs(self, async_client, db_session, tmp_path):
        run_id = await self._create_run(async_client, db_session, tmp_path, "SKU034.01")
        resp = await async_client.get(f"/api/v1/production-runs/{run_id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == run_id
        listing = await async_client.get("/api/v1/production-runs")
        assert listing.status_code == 200
        assert all(r["sku_file_id"] is not None for r in listing.json())
        assert any(r["id"] == run_id for r in listing.json())

    async def test_get_missing_404(self, async_client):
        assert (await async_client.get("/api/v1/production-runs/987654")).status_code == 404


async def _enable_auth(async_client, username="run_admin", password="RunAdmin!1"):
    """Turn on auth by running first-time setup (also creates an admin user)."""
    resp = await async_client.post(
        "/api/v1/auth/setup",
        json={"auth_enabled": True, "admin_username": username, "admin_password": password},
    )
    assert resp.status_code in (200, 201), resp.text


@pytest.mark.asyncio
@pytest.mark.integration
class TestProductionRunDelete:
    async def _make_run(self, async_client, db_session, tmp_path, code, *, target_units=2):
        # first-article gate OFF so all plates (and their queue items) are
        # created up front — makes the "all items deleted" assertion meaningful.
        eject = await _make_eject_profile(async_client, name=f"ep-{code}")
        _, file_link_id = await _make_sku_with_file(
            async_client, db_session, tmp_path, code=code, name=f"{code}.gcode.3mf"
        )
        resp = await async_client.post(
            "/api/v1/production-runs",
            json={
                "sku_file_id": file_link_id,
                "target_units": target_units,
                "target_model": "H2S",
                "eject_profile_id": eject,
                "require_first_article": False,
            },
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    async def test_delete_cancelled_run_removes_batch_and_items(self, async_client, db_session, tmp_path):
        from backend.app.models.print_batch import PrintBatch

        run_id = await self._make_run(async_client, db_session, tmp_path, "SKU040.01")
        # A run must be terminal to delete: abort it first → 'cancelled'.
        assert (await async_client.post(f"/api/v1/production-runs/{run_id}/abort")).status_code == 200
        assert len(await _pending_items(db_session, run_id)) == 2

        resp = await async_client.delete(f"/api/v1/production-runs/{run_id}")
        assert resp.status_code == 204, resp.text

        db_session.expire_all()
        # Batch row gone…
        batch = (await db_session.execute(select(PrintBatch).where(PrintBatch.id == run_id))).scalar_one_or_none()
        assert batch is None
        # …and every queue item with it.
        assert await _pending_items(db_session, run_id) == []
        # And the run now 404s through the API.
        assert (await async_client.get(f"/api/v1/production-runs/{run_id}")).status_code == 404

    async def test_delete_completed_run_204(self, async_client, db_session, tmp_path):
        from backend.app.models.print_batch import PrintBatch

        run_id = await self._make_run(async_client, db_session, tmp_path, "SKU041.01")
        batch = (await db_session.execute(select(PrintBatch).where(PrintBatch.id == run_id))).scalar_one()
        batch.status = "completed"
        await db_session.commit()

        resp = await async_client.delete(f"/api/v1/production-runs/{run_id}")
        assert resp.status_code == 204, resp.text

        db_session.expire_all()
        assert (
            await db_session.execute(select(PrintBatch).where(PrintBatch.id == run_id))
        ).scalar_one_or_none() is None

    async def test_delete_preserves_archives(
        self, async_client, db_session, tmp_path, printer_factory, archive_factory
    ):
        from backend.app.models.archive import PrintArchive

        printer = await printer_factory(model="H2S")
        archive = await archive_factory(printer.id)
        archive_id = archive.id  # capture before expire_all() (avoids a sync lazy-load)
        run_id = await self._make_run(async_client, db_session, tmp_path, "SKU042.01")
        assert (await async_client.post(f"/api/v1/production-runs/{run_id}/abort")).status_code == 200

        # Point a queue item at the archive: proves deleting an item that
        # references an archive does NOT cascade into archive history.
        items = await _pending_items(db_session, run_id)
        assert items
        items[0].archive_id = archive_id
        await db_session.commit()

        resp = await async_client.delete(f"/api/v1/production-runs/{run_id}")
        assert resp.status_code == 204, resp.text

        db_session.expire_all()
        assert await _pending_items(db_session, run_id) == []
        surviving = (
            await db_session.execute(select(PrintArchive).where(PrintArchive.id == archive_id))
        ).scalar_one_or_none()
        assert surviving is not None

    async def test_delete_active_run_409(self, async_client, db_session, tmp_path):
        run_id = await self._make_run(async_client, db_session, tmp_path, "SKU043.01")
        resp = await async_client.delete(f"/api/v1/production-runs/{run_id}")
        assert resp.status_code == 409, resp.text
        # The run is untouched and still reachable.
        assert (await async_client.get(f"/api/v1/production-runs/{run_id}")).status_code == 200

    async def test_delete_paused_run_409(self, async_client, db_session, tmp_path):
        run_id = await self._make_run(async_client, db_session, tmp_path, "SKU044.01")
        assert (await async_client.post(f"/api/v1/production-runs/{run_id}/pause")).status_code == 200
        resp = await async_client.delete(f"/api/v1/production-runs/{run_id}")
        assert resp.status_code == 409, resp.text

    async def test_delete_unknown_404(self, async_client):
        assert (await async_client.delete("/api/v1/production-runs/987654")).status_code == 404

    async def test_delete_run_with_a_printing_unit_409_names_the_printers(
        self, async_client, db_session, tmp_path, printer_factory
    ):
        """A live unit refuses the delete, and the 409 names printers, not ids."""
        printer = await printer_factory(name="001-H2S", model="H2S")
        run_id = await self._make_run(async_client, db_session, tmp_path, "SKU045.01")
        items = sorted(await _pending_items(db_session, run_id), key=lambda it: it.id)
        items[0].status = "printing"
        items[0].printer_id = printer.id
        items[0].started_at = datetime(2026, 8, 22, 2, 30, 0, tzinfo=timezone.utc)
        await db_session.commit()
        printing_id = items[0].id

        # Terminal run status is a separate precondition — satisfy it so the
        # printing check is what this test is actually exercising.
        from backend.app.models.print_batch import PrintBatch

        batch = (await db_session.execute(select(PrintBatch).where(PrintBatch.id == run_id))).scalar_one()
        batch.status = "cancelled"
        await db_session.commit()

        resp = await async_client.delete(f"/api/v1/production-runs/{run_id}")
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["code"] == "run_has_printing_units"
        assert detail["printers"] == ["001-H2S"]
        assert "001-H2S" in detail["message"]

        # Nothing was deleted — not the live unit, not its terminal siblings.
        db_session.expire_all()
        surviving = await _pending_items(db_session, run_id)
        assert {it.id for it in surviving} == {it.id for it in items}
        assert next(it for it in surviving if it.id == printing_id).status == "printing"
        assert (await async_client.get(f"/api/v1/production-runs/{run_id}")).status_code == 200

    async def test_delete_run_whose_units_are_all_terminal_204(
        self, async_client, db_session, tmp_path, printer_factory
    ):
        """Terminal units are queue history: the delete still removes them (204)."""
        from backend.app.models.print_batch import PrintBatch

        printer = await printer_factory(name="004-H2S", model="H2S")
        run_id = await self._make_run(async_client, db_session, tmp_path, "SKU046.01")
        items = sorted(await _pending_items(db_session, run_id), key=lambda it: it.id)
        for item, status_ in zip(items, ("completed", "failed"), strict=False):
            item.status = status_
            item.printer_id = printer.id
        batch = (await db_session.execute(select(PrintBatch).where(PrintBatch.id == run_id))).scalar_one()
        batch.status = "completed"
        await db_session.commit()

        resp = await async_client.delete(f"/api/v1/production-runs/{run_id}")
        assert resp.status_code == 204, resp.text

        db_session.expire_all()
        assert await _pending_items(db_session, run_id) == []
        assert (
            await db_session.execute(select(PrintBatch).where(PrintBatch.id == run_id))
        ).scalar_one_or_none() is None

    async def test_abort_then_delete_leaves_the_printing_units_alive(
        self, async_client, db_session, tmp_path, printer_factory
    ):
        """The 2026-08-22 incident sequence: abort at 02:34:31, delete at 02:34:36.

        Run 114 was aborted and hard-deleted five seconds later while three units
        were still printing on 001/002/003-H2S. The delete removed those rows, so
        when each print finished hours later ``resolve_terminal_item`` found no
        printing candidate, classified the terminal FOREIGN, raised the plate gate
        and armed no eject — three printers idle for ~27 printer-hours.

        Abort is the CORRECT first step and stays 200: it cancels the pending
        units and deliberately lets in-progress plates finish. It is the delete
        that must refuse, which is why a terminal run status can never stand in
        for this check.
        """
        printers = [await printer_factory(name=f"00{n}-H2S", model="H2S") for n in (1, 2, 3)]
        run_id = await self._make_run(async_client, db_session, tmp_path, "SKU047.01", target_units=3)
        items = sorted(await _pending_items(db_session, run_id), key=lambda it: it.id)
        assert len(items) == 3
        for item, printer in zip(items, printers, strict=True):
            item.status = "printing"
            item.printer_id = printer.id
        await db_session.commit()
        live_ids = {it.id for it in items}

        assert (await async_client.post(f"/api/v1/production-runs/{run_id}/abort")).status_code == 200

        resp = await async_client.delete(f"/api/v1/production-runs/{run_id}")
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["code"] == "run_has_printing_units"
        assert detail["printers"] == ["001-H2S", "002-H2S", "003-H2S"]

        # All three units survive, still printing, still correlatable.
        db_session.expire_all()
        surviving = await _pending_items(db_session, run_id)
        assert {it.id for it in surviving} == live_ids
        assert all(it.status == "printing" for it in surviving)

    async def test_a_unit_dispatched_in_the_gap_is_not_deleted(
        self, async_client, db_session, tmp_path, printer_factory, test_engine, monkeypatch
    ):
        """The race the primitive exists for: dispatch lands between load and delete.

        ``delete_production_run`` loads the run, and only then deletes. The
        scheduler sets ``status='printing'`` and COMMITS before it sends the print
        command, so a unit can go live inside that gap — invisible to any guard
        that reads the loaded ORM rows. Here a SECOND session commits exactly that
        flip after the run load, and the DELETE's own WHERE clause is the only
        thing standing between it and the 2026-08-22 outcome.
        """
        from sqlalchemy.ext.asyncio import AsyncSession as _Session, async_sessionmaker as _maker

        from backend.app.models.print_batch import PrintBatch
        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.services import production_run as production_run_service

        printer = await printer_factory(name="010-H2S", model="H2S")
        run_id = await self._make_run(async_client, db_session, tmp_path, "SKU048.01")
        items = sorted(await _pending_items(db_session, run_id), key=lambda it: it.id)
        racing_id = items[0].id
        # A terminal run whose units are still pending — the run load sees them
        # pending, exactly as the request did on 2026-08-22.
        batch = (await db_session.execute(select(PrintBatch).where(PrintBatch.id == run_id))).scalar_one()
        batch.status = "cancelled"
        await db_session.commit()

        real_delete = production_run_service.delete_items_unless_printing
        maker = _maker(test_engine, class_=_Session, expire_on_commit=False)

        async def _dispatch_then_delete(db, *, batch_id):
            async with maker() as scheduler:
                racing = (
                    await scheduler.execute(select(PrintQueueItem).where(PrintQueueItem.id == racing_id))
                ).scalar_one()
                racing.status = "printing"
                racing.printer_id = printer.id
                racing.started_at = datetime(2026, 8, 22, 2, 34, 33, tzinfo=timezone.utc)
                await scheduler.commit()
            return await real_delete(db, batch_id=batch_id)

        monkeypatch.setattr(production_run_service, "delete_items_unless_printing", _dispatch_then_delete)

        resp = await async_client.delete(f"/api/v1/production-runs/{run_id}")
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["code"] == "run_has_printing_units"

        # The unit that raced in survives — and so does the whole run, because the
        # refusal rolls back rather than deleting the rest.
        db_session.expire_all()
        surviving = await _pending_items(db_session, run_id)
        assert {it.id for it in surviving} == {it.id for it in items}
        assert next(it for it in surviving if it.id == racing_id).status == "printing"
        assert (
            await db_session.execute(select(PrintBatch).where(PrintBatch.id == run_id))
        ).scalar_one_or_none() is not None

    async def test_delete_requires_auth_401(self, async_client):
        await _enable_auth(async_client)
        # Auth enabled + no credentials → 401 (the permission dep runs first).
        resp = await async_client.delete("/api/v1/production-runs/987654")
        assert resp.status_code == 401, resp.text

    async def test_delete_forbidden_for_viewer_403(self, async_client, db_session):
        from sqlalchemy import insert

        from backend.app.core.auth import get_password_hash
        from backend.app.models.group import Group, user_groups
        from backend.app.models.user import User

        await _enable_auth(async_client)
        viewer = User(
            username="run_viewer",
            email="run_viewer@example.com",
            password_hash=get_password_hash("RunViewer!1"),
            role="user",
            is_active=True,
        )
        db_session.add(viewer)
        await db_session.flush()
        viewers = (await db_session.execute(select(Group).where(Group.name == "Viewers"))).scalar_one()
        await db_session.execute(insert(user_groups).values(user_id=viewer.id, group_id=viewers.id))
        await db_session.commit()

        login = await async_client.post(
            "/api/v1/auth/login", json={"username": "run_viewer", "password": "RunViewer!1"}
        )
        assert login.status_code == 200, login.text
        token = login.json()["access_token"]
        # Viewers carry production_runs:read but NOT :delete → 403.
        resp = await async_client.delete("/api/v1/production-runs/987654", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
@pytest.mark.integration
class TestQueueRetryLineageExposure:
    """The queue API must expose farm retry lineage, not just store it (2026-07-05).

    Found live: retry items carried retry_of_id/retry_count in the DB while the
    API returned neither, so retry chains were invisible to the UI/operator.
    """

    async def test_queue_response_carries_retry_fields(self, async_client, db_session, tmp_path):
        profile_id = await _make_eject_profile(async_client)
        _sid, sku_file_id = await _make_sku_with_file(
            async_client, db_session, tmp_path, code="SKU900.01", default_eject_profile_id=profile_id
        )
        r = await async_client.post(
            "/api/v1/production-runs",
            json={
                "sku_file_id": sku_file_id,
                "target_units": 2,
                "target_model": "H2S",
                "require_first_article": False,
            },
        )
        assert r.status_code == 201, r.text
        items = await _pending_items(db_session, r.json()["id"])
        assert len(items) == 2
        original, retry = items[0], items[1]
        retry.retry_of_id = original.id
        retry.retry_count = 1
        await db_session.commit()

        resp = await async_client.get(f"/api/v1/queue/{retry.id}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["retry_of_id"] == original.id
        assert body["retry_count"] == 1

        resp = await async_client.get(f"/api/v1/queue/{original.id}")
        body = resp.json()
        assert body["retry_of_id"] is None
        assert body["retry_count"] == 0


@pytest.mark.asyncio
@pytest.mark.integration
class TestRunDetailPhase4:
    """Run visibility (Phase 4.1): lean list vs full detail, pause_reason
    lifecycle over HTTP, staged counts, and the queue rows' run identity."""

    async def _make_run(self, async_client, db_session, tmp_path, code, *, target_units=2):
        eject = await _make_eject_profile(async_client, name=f"ep-{code}")
        _, file_link_id = await _make_sku_with_file(async_client, db_session, tmp_path, code=code)
        r = await async_client.post(
            "/api/v1/production-runs",
            json={
                "sku_file_id": file_link_id,
                "target_units": target_units,
                "target_model": "H2S",
                "eject_profile_id": eject,
                "require_first_article": False,
            },
        )
        assert r.status_code == 201, r.text
        return r.json()["id"]

    async def test_list_is_lean_and_detail_is_full(self, async_client, db_session, tmp_path):
        run_id = await self._make_run(async_client, db_session, tmp_path, "SKU401.01")

        lean = (await async_client.get("/api/v1/production-runs")).json()
        row = next(r for r in lean if r["id"] == run_id)
        assert row["pause_reason"] is None
        assert row["staged_filament_short"] == 0
        assert row["staged_other"] == 0
        assert row["has_blocked_printers"] is False
        assert row["printer_states"] is None  # detail-only payloads stay null
        assert row["units"] is None

        detail = (await async_client.get(f"/api/v1/production-runs/{run_id}")).json()
        assert isinstance(detail["printer_states"], list)
        assert len(detail["units"]) == 2
        unit = detail["units"][0]
        for key in (
            "id",
            "status",
            "stop_source",
            "waiting_reason",
            "printer_id",
            "printer_name",
            "retry_of_id",
            "retry_count",
            "filament_short",
            "manual_start",
            "first_article",
            "error_message",
            "started_at",
            "completed_at",
        ):
            assert key in unit

    async def test_pause_reason_lifecycle_over_http(self, async_client, db_session, tmp_path):
        run_id = await self._make_run(async_client, db_session, tmp_path, "SKU402.01")

        paused = (await async_client.post(f"/api/v1/production-runs/{run_id}/pause")).json()
        assert paused["status"] == "paused"
        assert paused["pause_reason"] == "operator"

        resumed = (await async_client.post(f"/api/v1/production-runs/{run_id}/resume")).json()
        assert resumed["status"] == "active"
        assert resumed["pause_reason"] is None

    async def test_staged_counts_and_unit_flags_in_detail(self, async_client, db_session, tmp_path):
        run_id = await self._make_run(async_client, db_session, tmp_path, "SKU403.01")
        items = await _pending_items(db_session, run_id)
        items[0].manual_start = True
        items[0].filament_short = True  # system-staged (low spool)
        items[1].manual_start = True  # operator-staged
        await db_session.commit()

        detail = (await async_client.get(f"/api/v1/production-runs/{run_id}")).json()
        assert detail["staged_filament_short"] == 1
        assert detail["staged_other"] == 1
        flagged = next(u for u in detail["units"] if u["id"] == items[0].id)
        assert flagged["filament_short"] is True and flagged["manual_start"] is True

    async def test_detail_units_carry_retry_lineage_and_stop_source(self, async_client, db_session, tmp_path):
        run_id = await self._make_run(async_client, db_session, tmp_path, "SKU404.01")
        items = await _pending_items(db_session, run_id)
        original, second = items[0], items[1]
        original.status = "failed"
        second.retry_of_id = original.id
        second.retry_count = 1
        second.stop_source = "operator_screen"
        await db_session.commit()

        detail = (await async_client.get(f"/api/v1/production-runs/{run_id}")).json()
        units = {u["id"]: u for u in detail["units"]}
        assert units[second.id]["retry_of_id"] == original.id
        assert units[second.id]["retry_count"] == 1
        assert units[second.id]["stop_source"] == "operator_screen"

    async def test_queue_rows_carry_production_run_id(self, async_client, db_session, tmp_path):
        run_id = await self._make_run(async_client, db_session, tmp_path, "SKU405.01")
        items = await _pending_items(db_session, run_id)

        resp = await async_client.get(f"/api/v1/queue/{items[0].id}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["production_run_id"] == run_id

    async def test_plain_batch_items_have_null_production_run_id(self, async_client, db_session, tmp_path):
        # A non-farm batch (no sku_file_id) must NOT masquerade as a run.
        from backend.app.models.print_batch import PrintBatch
        from backend.app.models.print_queue import PrintQueueItem

        lib = await _add_library_file(db_session, tmp_path, name="plain.gcode.3mf")
        batch = PrintBatch(name="plain", quantity=1, status="active")
        db_session.add(batch)
        await db_session.flush()
        item = PrintQueueItem(batch_id=batch.id, library_file_id=lib.id, status="pending", position=1)
        db_session.add(item)
        await db_session.commit()

        resp = await async_client.get(f"/api/v1/queue/{item.id}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["batch_id"] == batch.id
        assert body["production_run_id"] is None


@pytest.mark.asyncio
@pytest.mark.integration
class TestReleaseStagedRoute:
    """POST /queue/release-staged (Phase 4.2): re-checks system-staged items and
    releases only the resolved ones. Queue-update permission required."""

    async def _stage_run_items(self, async_client, db_session, tmp_path, code):
        eject = await _make_eject_profile(async_client, name=f"ep-{code}")
        _, file_link_id = await _make_sku_with_file(async_client, db_session, tmp_path, code=code)
        r = await async_client.post(
            "/api/v1/production-runs",
            json={
                "sku_file_id": file_link_id,
                "target_units": 2,
                "target_model": "H2S",
                "eject_profile_id": eject,
                "require_first_article": False,
            },
        )
        assert r.status_code == 201, r.text
        items = await _pending_items(db_session, r.json()["id"])
        for i, item in enumerate(items):
            item.printer_id = 100 + i
            item.manual_start = True
            item.filament_short = True
        await db_session.commit()
        return items

    async def test_release_all_when_deficits_cleared(self, async_client, db_session, tmp_path, monkeypatch):
        from unittest.mock import AsyncMock

        from backend.app.services import farm_staging

        items = await self._stage_run_items(async_client, db_session, tmp_path, "SKU410.01")
        item_ids = [item.id for item in items]
        monkeypatch.setattr(farm_staging, "compute_deficit_for_queue_item", AsyncMock(return_value=[]))

        resp = await async_client.post("/api/v1/queue/release-staged")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"released": 2}

        for item_id in item_ids:
            row = (await async_client.get(f"/api/v1/queue/{item_id}")).json()
            assert row["manual_start"] is False
            assert row["filament_short"] is False

    async def test_release_scoped_to_printer(self, async_client, db_session, tmp_path, monkeypatch):
        from unittest.mock import AsyncMock

        from backend.app.services import farm_staging

        items = await self._stage_run_items(async_client, db_session, tmp_path, "SKU411.01")
        monkeypatch.setattr(farm_staging, "compute_deficit_for_queue_item", AsyncMock(return_value=[]))

        resp = await async_client.post(f"/api/v1/queue/release-staged?printer_id={items[0].printer_id}")
        assert resp.json() == {"released": 1}
        other = (await async_client.get(f"/api/v1/queue/{items[1].id}")).json()
        assert other["manual_start"] is True  # untouched — other printer

    async def test_still_short_items_stay_staged(self, async_client, db_session, tmp_path, monkeypatch):
        from unittest.mock import AsyncMock

        from backend.app.services import farm_staging

        items = await self._stage_run_items(async_client, db_session, tmp_path, "SKU412.01")
        monkeypatch.setattr(farm_staging, "compute_deficit_for_queue_item", AsyncMock(return_value=[{"slot_id": 1}]))
        resp = await async_client.post("/api/v1/queue/release-staged")
        assert resp.json() == {"released": 0}
        row = (await async_client.get(f"/api/v1/queue/{items[0].id}")).json()
        assert row["manual_start"] is True and row["filament_short"] is True

    async def test_requires_auth_401(self, async_client):
        await _enable_auth(async_client, username="rs_admin")
        resp = await async_client.post("/api/v1/queue/release-staged")
        assert resp.status_code == 401, resp.text

    async def test_forbidden_for_viewer_403(self, async_client, db_session):
        from sqlalchemy import insert

        from backend.app.core.auth import get_password_hash
        from backend.app.models.group import Group, user_groups
        from backend.app.models.user import User

        await _enable_auth(async_client, username="rs_admin2")
        viewer = User(
            username="rs_viewer",
            email="rs_viewer@example.com",
            password_hash=get_password_hash("RsViewer!1"),
            role="user",
            is_active=True,
        )
        db_session.add(viewer)
        await db_session.flush()
        viewers = (await db_session.execute(select(Group).where(Group.name == "Viewers"))).scalar_one()
        await db_session.execute(insert(user_groups).values(user_id=viewer.id, group_id=viewers.id))
        await db_session.commit()

        login = await async_client.post("/api/v1/auth/login", json={"username": "rs_viewer", "password": "RsViewer!1"})
        assert login.status_code == 200, login.text
        token = login.json()["access_token"]
        # Viewers lack queue:update_all -> 403.
        resp = await async_client.post("/api/v1/queue/release-staged", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
@pytest.mark.integration
class TestFarmPrinterStates:
    """GET /production-runs/printer-states (Phase 3, F2): fleet-scoped per-printer
    farm context surfaced on the Printers page."""

    async def _run_on_printers(self, async_client, db_session, tmp_path, code, printer_ids, *, target_units):
        """Create a run and PLACE its units on the printers, as the scheduler would.

        Every test in this class asserts a per-printer context, and a context is
        resolved from a unit's ``printer_id``. Since ``printer_ids`` became a POOL
        the creation call no longer writes one — a pool unit acquires its printer at
        the dispatch claim — so the placement that these tests take as their premise
        is done here explicitly instead of being a side effect of creation. The
        pool-unit-without-a-printer case is its own test below.
        """
        eject = await _make_eject_profile(async_client, name=f"ep-{code}")
        _, file_link_id = await _make_sku_with_file(
            async_client, db_session, tmp_path, code=code, name=f"{code}.gcode.3mf"
        )
        resp = await async_client.post(
            "/api/v1/production-runs",
            json={
                "sku_file_id": file_link_id,
                "target_units": target_units,
                "printer_ids": printer_ids,
                "eject_profile_id": eject,
                # No FA gate so all plates are created up front.
                "require_first_article": False,
            },
        )
        assert resp.status_code == 201, resp.text
        run_id = resp.json()["id"]
        items = sorted(await _pending_items(db_session, run_id), key=lambda i: i.id)
        for offset, item in enumerate(items):
            item.printer_id = printer_ids[offset % len(printer_ids)]
        await db_session.commit()
        return run_id

    async def test_printing_unit_context_shape(self, async_client, db_session, tmp_path, printer_factory):
        p = await printer_factory(name="FP1", model="H2S")
        run_id = await self._run_on_printers(async_client, db_session, tmp_path, "SKU500.01", [p.id], target_units=1)
        items = await _pending_items(db_session, run_id)
        items[0].status = "printing"
        await db_session.commit()

        resp = await async_client.get("/api/v1/production-runs/printer-states")
        assert resp.status_code == 200, resp.text
        ctx = next(r for r in resp.json() if r["printer_id"] == p.id)
        for key in (
            "printer_id",
            "run_id",
            "run_name",
            "sku_code",
            "run_status",
            "pause_reason",
            "unit_id",
            "unit_status",
            "waiting_reason",
            "error_message",
            "staged",
            "filament_short",
            "first_article",
            "first_article_state",
        ):
            assert key in ctx
        assert ctx["run_id"] == run_id
        assert ctx["sku_code"] == "SKU500.01"
        assert ctx["unit_status"] == "printing"
        assert ctx["unit_id"] == items[0].id
        assert ctx["run_status"] == "active"
        assert ctx["error_message"] is None

    async def test_waiting_reason_surfaces(self, async_client, db_session, tmp_path, printer_factory):
        p = await printer_factory(name="FP2", model="H2S")
        run_id = await self._run_on_printers(async_client, db_session, tmp_path, "SKU501.01", [p.id], target_units=1)
        items = await _pending_items(db_session, run_id)
        items[0].status = "printing"
        items[0].waiting_reason = "printer_offline_stalled"
        await db_session.commit()

        rows = (await async_client.get("/api/v1/production-runs/printer-states")).json()
        ctx = next(r for r in rows if r["printer_id"] == p.id)
        assert ctx["waiting_reason"] == "printer_offline_stalled"

    async def test_failed_error_only_when_no_live_unit(self, async_client, db_session, tmp_path, printer_factory):
        p = await printer_factory(name="FP3", model="H2S")
        run_id = await self._run_on_printers(async_client, db_session, tmp_path, "SKU502.01", [p.id], target_units=2)
        items = sorted(await _pending_items(db_session, run_id), key=lambda i: i.id)
        # A failed unit alongside a live printing unit → error suppressed.
        items[0].status = "failed"
        items[0].error_message = "HMS 0300_8017"
        items[1].status = "printing"
        await db_session.commit()
        rows = (await async_client.get("/api/v1/production-runs/printer-states")).json()
        ctx = next(r for r in rows if r["printer_id"] == p.id)
        assert ctx["unit_status"] == "printing"
        assert ctx["error_message"] is None

        # No live unit → the last failure explains the idle printer.
        items[1].status = "completed"
        await db_session.commit()
        rows = (await async_client.get("/api/v1/production-runs/printer-states")).json()
        ctx = next(r for r in rows if r["printer_id"] == p.id)
        assert ctx["unit_status"] == "failed"
        assert ctx["error_message"] == "HMS 0300_8017"

    async def test_non_farm_batch_excluded(self, async_client, db_session, tmp_path, printer_factory):
        from backend.app.models.print_batch import PrintBatch
        from backend.app.models.print_queue import PrintQueueItem

        p = await printer_factory(name="FP4", model="H2S")
        lib = await _add_library_file(db_session, tmp_path, name="plain2.gcode.3mf")
        batch = PrintBatch(name="plain", quantity=1, status="active")
        db_session.add(batch)
        await db_session.flush()
        item = PrintQueueItem(batch_id=batch.id, library_file_id=lib.id, printer_id=p.id, status="printing", position=1)
        db_session.add(item)
        await db_session.commit()

        rows = (await async_client.get("/api/v1/production-runs/printer-states")).json()
        assert all(r["printer_id"] != p.id for r in rows)

    async def test_paused_run_included_with_reason(self, async_client, db_session, tmp_path, printer_factory):
        p = await printer_factory(name="FP5", model="H2S")
        run_id = await self._run_on_printers(async_client, db_session, tmp_path, "SKU503.01", [p.id], target_units=1)
        assert (await async_client.post(f"/api/v1/production-runs/{run_id}/pause")).status_code == 200

        rows = (await async_client.get("/api/v1/production-runs/printer-states")).json()
        ctx = next(r for r in rows if r["printer_id"] == p.id)
        assert ctx["run_status"] == "paused"
        assert ctx["pause_reason"] == "operator"
        assert ctx["staged"] is True  # pause staged the pending unit (manual_start)

    async def test_printer_states_not_captured_by_run_id(self, async_client, db_session, tmp_path, printer_factory):
        # Route-ordering regression pair: an int id still routes to the detail
        # handler, AND the literal /printer-states path resolves to the fleet
        # endpoint (a list) rather than 404/422 from int coercion.
        p = await printer_factory(name="FP6", model="H2S")
        run_id = await self._run_on_printers(async_client, db_session, tmp_path, "SKU504.01", [p.id], target_units=1)
        by_id = await async_client.get(f"/api/v1/production-runs/{run_id}")
        assert by_id.status_code == 200
        assert by_id.json()["id"] == run_id
        states = await async_client.get("/api/v1/production-runs/printer-states")
        assert states.status_code == 200
        assert isinstance(states.json(), list)

    async def test_pending_pool_unit_yields_no_printer_context(
        self, async_client, db_session, tmp_path, printer_factory
    ):
        """Documented behaviour delta: a pool unit contributes a context only once it
        has LANDED. Its pending row carries no ``printer_id`` at all, and the fleet
        page answers "what is this printer doing" — a plate that may go to any member
        of a pool is not yet any printer's work. (Contrast the helper above, which
        places its units explicitly.)"""
        p1 = await printer_factory(name="FP7", model="H2S")
        p2 = await printer_factory(name="FP8", model="H2S")
        eject = await _make_eject_profile(async_client, name="ep-SKU505.01")
        _, file_link_id = await _make_sku_with_file(
            async_client, db_session, tmp_path, code="SKU505.01", name="SKU505.01.gcode.3mf"
        )
        created = await async_client.post(
            "/api/v1/production-runs",
            json={
                "sku_file_id": file_link_id,
                "target_units": 2,
                "printer_ids": [p1.id, p2.id],
                "eject_profile_id": eject,
                "require_first_article": False,
            },
        )
        assert created.status_code == 201, created.text
        items = await _pending_items(db_session, created.json()["id"])
        assert items and all(it.printer_id is None for it in items)

        rows = (await async_client.get("/api/v1/production-runs/printer-states")).json()
        assert all(r["printer_id"] not in (p1.id, p2.id) for r in rows)
