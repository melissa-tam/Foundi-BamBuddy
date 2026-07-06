"""Integration tests for the first-article + quarantine + notification surface (Phase 3)."""

import zipfile

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


async def _add_library_file(db_session, tmp_path, name="fa.gcode.3mf"):
    from backend.app.models.library import LibraryFile

    disk = tmp_path / name
    _write_3mf(disk)
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


async def _sku_with_file(async_client, db_session, tmp_path, code, eject_id):
    r = await async_client.post("/api/v1/skus", json={"code": code, "name": f"{code} thing"})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    lib = await _add_library_file(db_session, tmp_path, name=f"{code}.gcode.3mf")
    r = await async_client.post(
        f"/api/v1/skus/{sid}/files",
        json={"library_file_id": lib.id, "plate_index": 1, "units_per_plate": 1},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _eject(async_client, name):
    r = await async_client.post("/api/v1/eject-profiles", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _create_run(async_client, db_session, tmp_path, code, **extra):
    eject_id = await _eject(async_client, f"ep-{code}")
    file_link_id = await _sku_with_file(async_client, db_session, tmp_path, code, eject_id)
    body = {"sku_file_id": file_link_id, "target_units": 3, "target_model": "H2S", "eject_profile_id": eject_id}
    body.update(extra)
    r = await async_client.post("/api/v1/production-runs", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def _run_items(db_session, run_id):
    from backend.app.models.print_queue import PrintQueueItem

    r = await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.batch_id == run_id))
    return list(r.scalars().all())


async def _drive_fa_to_awaiting(db_session, run_id):
    """Simulate the first-article print completing, via the service function."""
    from backend.app.services import farm_policy

    items = await _run_items(db_session, run_id)
    fa = next(i for i in items if i.first_article)
    fa.status = "completed"
    fa.printer_id = fa.printer_id or None
    await db_session.commit()
    await farm_policy.on_terminal(db_session, fa.printer_id, fa.id, "completed")
    return fa


@pytest.mark.asyncio
@pytest.mark.integration
class TestRunCreateNewFields:
    async def test_create_defaults_gate_on_first_article(self, async_client, db_session, tmp_path):
        body = await _create_run(async_client, db_session, tmp_path, "SKU200.01")
        assert body["require_first_article"] is True
        assert body["first_article_state"] == "pending_print"
        assert body["retry_max_per_unit"] == 1
        assert body["escalate_consecutive_failures"] == 2
        # Only the first plate exists; the rest are deferred but counted.
        items = await _run_items(db_session, body["id"])
        assert len(items) == 1
        assert items[0].first_article is True
        assert body["plates_total"] == 3
        assert body["plates_pending"] == 3

    async def test_create_without_gate_creates_all_plates(self, async_client, db_session, tmp_path):
        body = await _create_run(
            async_client, db_session, tmp_path, "SKU201.01", require_first_article=False
        )
        assert body["require_first_article"] is False
        assert body["first_article_state"] is None
        items = await _run_items(db_session, body["id"])
        assert len(items) == 3
        assert all(not i.first_article for i in items)

    async def test_create_honors_explicit_policy_numbers(self, async_client, db_session, tmp_path):
        body = await _create_run(
            async_client, db_session, tmp_path, "SKU202.01", retry_max_per_unit=3, escalate_consecutive_failures=5
        )
        assert body["retry_max_per_unit"] == 3
        assert body["escalate_consecutive_failures"] == 5


@pytest.mark.asyncio
@pytest.mark.integration
class TestFirstArticleEndpoints:
    async def test_approve_before_awaiting_409(self, async_client, db_session, tmp_path):
        body = await _create_run(async_client, db_session, tmp_path, "SKU210.01")
        r = await async_client.post(
            f"/api/v1/production-runs/{body['id']}/first-article/approve", json={"eject_remotely": False}
        )
        assert r.status_code == 409

    async def test_reject_before_awaiting_409(self, async_client, db_session, tmp_path):
        body = await _create_run(async_client, db_session, tmp_path, "SKU211.01")
        r = await async_client.post(
            f"/api/v1/production-runs/{body['id']}/first-article/reject", json={"reason": "too soon"}
        )
        assert r.status_code == 409

    async def test_approve_local_happy(self, async_client, db_session, tmp_path):
        body = await _create_run(async_client, db_session, tmp_path, "SKU212.01")
        await _drive_fa_to_awaiting(db_session, body["id"])

        # State is visible to the API session (committed to the shared engine).
        got = await async_client.get(f"/api/v1/production-runs/{body['id']}")
        assert got.json()["first_article_state"] == "awaiting_approval"

        r = await async_client.post(
            f"/api/v1/production-runs/{body['id']}/first-article/approve", json={"eject_remotely": False}
        )
        assert r.status_code == 200, r.text
        assert r.json()["first_article_state"] == "approved"
        items = await _run_items(db_session, body["id"])
        assert len(items) == 3  # FA + 2 materialised remaining plates

    async def test_reject_pauses_and_reason_exposed(self, async_client, db_session, tmp_path):
        body = await _create_run(async_client, db_session, tmp_path, "SKU213.01")
        await _drive_fa_to_awaiting(db_session, body["id"])

        r = await async_client.post(
            f"/api/v1/production-runs/{body['id']}/first-article/reject",
            json={"reason": "warping on the edge"},
        )
        assert r.status_code == 200, r.text
        payload = r.json()
        assert payload["first_article_state"] == "rejected"
        assert payload["status"] == "paused"
        assert payload["first_article_reject_reason"] == "warping on the edge"

    async def test_reject_then_resume_new_first_article(self, async_client, db_session, tmp_path):
        body = await _create_run(async_client, db_session, tmp_path, "SKU214.01")
        await _drive_fa_to_awaiting(db_session, body["id"])
        await async_client.post(
            f"/api/v1/production-runs/{body['id']}/first-article/reject", json={"reason": "bad"}
        )
        r = await async_client.post(f"/api/v1/production-runs/{body['id']}/resume")
        assert r.status_code == 200, r.text
        payload = r.json()
        assert payload["first_article_state"] == "pending_print"
        assert payload["status"] == "active"
        assert payload["first_article_reject_reason"] is None

    async def test_reject_reason_length_validation(self, async_client, db_session, tmp_path):
        body = await _create_run(async_client, db_session, tmp_path, "SKU215.01")
        await _drive_fa_to_awaiting(db_session, body["id"])
        r = await async_client.post(
            f"/api/v1/production-runs/{body['id']}/first-article/reject", json={"reason": ""}
        )
        assert r.status_code == 422  # min_length=1


@pytest.mark.asyncio
@pytest.mark.integration
class TestClearQuarantine:
    async def test_clear_quarantine_resets_flag(self, async_client, db_session, printer_factory):
        from backend.app.models.printer import Printer
        from backend.app.services.printer_manager import printer_manager

        printer = await printer_factory(name="QP", model="H2S")
        printer.quarantined = True
        printer.quarantine_reason = "2 consecutive farm print failures"
        await db_session.commit()
        printer_manager.set_quarantined(printer.id, True)

        r = await async_client.post(f"/api/v1/printers/{printer.id}/clear-quarantine")
        assert r.status_code == 200, r.text
        assert r.json() == {"id": printer.id, "quarantined": False}
        assert printer_manager.is_quarantined(printer.id) is False

        # DB reflects the cleared state.
        row = (await db_session.execute(select(Printer).where(Printer.id == printer.id))).scalar_one()
        await db_session.refresh(row)
        assert row.quarantined is False
        assert row.quarantine_reason is None

    async def test_clear_quarantine_on_non_quarantined_is_ok(self, async_client, printer_factory):
        printer = await printer_factory(name="QP2", model="H2S")
        r = await async_client.post(f"/api/v1/printers/{printer.id}/clear-quarantine")
        assert r.status_code == 200, r.text
        assert r.json()["quarantined"] is False

    async def test_clear_quarantine_missing_printer_404(self, async_client):
        r = await async_client.post("/api/v1/printers/987654/clear-quarantine")
        assert r.status_code == 404


@pytest.mark.asyncio
@pytest.mark.integration
class TestNotificationEventRegistration:
    async def test_provider_accepts_farm_event_fields(self, async_client):
        r = await async_client.post(
            "/api/v1/notifications/",
            json={
                "name": "Farm Discord",
                "provider_type": "discord",
                "enabled": True,
                "config": {"webhook_url": "https://discord.com/api/webhooks/1/abc"},
                "on_first_article_pending": True,
                "on_printer_quarantined": True,
                "on_run_paused": True,
                "on_run_completed": True,
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["on_first_article_pending"] is True
        assert body["on_printer_quarantined"] is True
        assert body["on_run_paused"] is True
        assert body["on_run_completed"] is True

        listing = await async_client.get("/api/v1/notifications/")
        assert listing.status_code == 200
        got = next(p for p in listing.json() if p["id"] == body["id"])
        assert got["on_first_article_pending"] is True

    async def test_farm_templates_seeded(self, async_client):
        # The httpx ASGITransport doesn't run the app lifespan, so seed the
        # default templates explicitly (uses the patched test session) to prove
        # the four farm events register through the standard seeding path.
        from backend.app.core.database import seed_notification_templates

        await seed_notification_templates()
        r = await async_client.get("/api/v1/notification-templates/")
        assert r.status_code == 200, r.text
        event_types = {t["event_type"] for t in r.json()}
        assert {"first_article_pending", "printer_quarantined", "run_paused", "run_completed"} <= event_types


@pytest.mark.asyncio
@pytest.mark.integration
class TestFarmSettings:
    async def test_settings_expose_and_update_farm_defaults(self, async_client):
        r = await async_client.get("/api/v1/settings/")
        assert r.status_code == 200, r.text
        s = r.json()
        assert s["farm_retry_max_per_unit"] == 1
        assert s["farm_escalate_consecutive_failures"] == 2

        put = await async_client.put(
            "/api/v1/settings/",
            json={"farm_retry_max_per_unit": 2, "farm_escalate_consecutive_failures": 3},
        )
        assert put.status_code == 200, put.text
        assert put.json()["farm_retry_max_per_unit"] == 2
        assert put.json()["farm_escalate_consecutive_failures"] == 3
