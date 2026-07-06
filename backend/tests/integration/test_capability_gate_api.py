"""Integration tests for the Phase 4 dispatch gate + power-stagger consumer.

- A farm queue item BLOCKED by the capability gate surfaces its reason on
  ``waiting_reason`` (visible via the queue API, no frontend change).
- The power-stagger consumer limits how many prints START within one simulated
  scheduler tick, honouring the persisted ``stagger_group_size``.
"""

import zipfile
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_PLATE_GCODE = (
    "; HEADER_BLOCK_START\n"
    "; max_z_height: 20.00\n"
    "; HEADER_BLOCK_END\n"
    "; EXECUTABLE_BLOCK_START\n"
    "G1 X100 Y100 E5\n"
    "; EXECUTABLE_BLOCK_END\n"
)


def _write_3mf(path, plate_id=1):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"Metadata/plate_{plate_id}.gcode", _PLATE_GCODE)
        zf.writestr("3D/3dmodel.model", "<model/>")


async def _add_library_file(db_session, tmp_path, metadata, name="cap.gcode.3mf"):
    from backend.app.models.library import LibraryFile

    disk = tmp_path / name
    _write_3mf(disk)
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
class TestCapabilityGateBlockSurfacesReason:
    async def test_farm_item_blocked_by_nozzle_shows_reason_via_api(
        self, async_client, db_session, tmp_path, printer_factory, monkeypatch
    ):
        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.services.print_scheduler import scheduler
        from backend.app.services.printer_manager import printer_manager

        # H2S printer; file sliced for H2S needing a 0.6 nozzle + PETG.
        printer = await printer_factory(model="H2S", name="H2S-1")
        lib = await _add_library_file(
            db_session,
            tmp_path,
            {"sliced_for_model": "H2S", "nozzle_diameter": 0.6, "filament_type": "PETG"},
        )

        # SKU + file link + eject profile via the API.
        eject = (await async_client.post("/api/v1/eject-profiles", json={"name": "cap-ep"})).json()["id"]
        sid = (await async_client.post("/api/v1/skus", json={"code": "SKU-CAP.01", "name": "cap"})).json()["id"]
        sf = await async_client.post(
            f"/api/v1/skus/{sid}/files",
            json={"library_file_id": lib.id, "plate_index": 1, "units_per_plate": 1},
        )
        sku_file_id = sf.json()["id"]

        # Non-gated run pinned to the printer → the plate item is created + assigned.
        run = await async_client.post(
            "/api/v1/production-runs",
            json={
                "sku_file_id": sku_file_id,
                "target_units": 1,
                "printer_ids": [printer.id],
                "eject_profile_id": eject,
                "require_first_article": False,
            },
        )
        assert run.status_code == 201, run.text
        run_id = run.json()["id"]

        result = await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.batch_id == run_id))
        item = result.scalar_one()

        # Printer is connected + idle but reports a 0.4 nozzle (mismatch) with PETG.
        monkeypatch.setattr(printer_manager, "is_connected", lambda pid: True)
        monkeypatch.setattr(
            printer_manager,
            "get_status",
            lambda pid: SimpleNamespace(
                nozzles=[SimpleNamespace(nozzle_diameter="0.4")],
                raw_data={"ams": [{"tray": [{"tray_type": "PETG"}]}]},
            ),
        )

        # Drive the single dispatch path. The gate must BLOCK (not fail).
        await scheduler._start_print(db_session, item)

        await db_session.refresh(item)
        assert item.status == "pending"  # not failed — re-evaluated later
        assert item.waiting_reason and "nozzle mismatch" in item.waiting_reason

        # The reason is visible through the queue API with no frontend change.
        listing = await async_client.get("/api/v1/queue/")
        assert listing.status_code == 200, listing.text
        rows = [r for r in listing.json() if r["id"] == item.id]
        assert rows and rows[0]["waiting_reason"] and "Capability" in rows[0]["waiting_reason"]


@pytest.mark.asyncio
@pytest.mark.integration
class TestStaggerLimitsStartsPerTick:
    async def test_group_size_one_starts_only_one_per_tick(
        self, test_engine, db_session, printer_factory, monkeypatch
    ):
        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.models.settings import Settings
        from backend.app.services import print_scheduler as ps
        from backend.app.services.print_scheduler import PrintScheduler

        # check_queue opens its OWN session via the module-level async_session —
        # point it at the test engine so it sees our rows and its commits are
        # visible to db_session afterwards.
        maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr(ps, "async_session", maker)

        # Three idle H2S printers, one pending item each.
        printers = [await printer_factory(model="H2S", name=f"H2S-{i}") for i in range(3)]
        for pos, p in enumerate(printers):
            db_session.add(PrintQueueItem(printer_id=p.id, status="pending", position=pos))
        # Stagger: at most ONE start per 5-minute window.
        db_session.add(Settings(key="stagger_group_size", value="1"))
        db_session.add(Settings(key="stagger_interval_minutes", value="5"))
        await db_session.commit()

        sched = PrintScheduler()
        monkeypatch.setattr(ps.printer_manager, "is_connected", lambda pid: True)
        monkeypatch.setattr(ps.printer_manager, "is_quarantined", lambda pid: False)
        monkeypatch.setattr(ps.printer_manager, "is_awaiting_plate_clear", lambda pid: False)
        monkeypatch.setattr(ps.printer_manager, "get_status", lambda pid: SimpleNamespace(state="IDLE"))

        started: list[int] = []

        async def fake_start(db, item):
            item.status = "printing"
            item.started_at = datetime.now(timezone.utc)
            await db.commit()
            started.append(item.id)

        monkeypatch.setattr(sched, "_start_print", fake_start)
        monkeypatch.setattr(sched, "_check_auto_drying", AsyncMock())
        monkeypatch.setattr(sched, "_block_on_filament_deficit", AsyncMock(return_value=False))
        monkeypatch.setattr(sched, "_compute_ams_mapping_for_printer", AsyncMock(return_value=None))

        await sched.check_queue()

        # Only the window budget (1) may start this tick; the rest wait.
        assert len(started) == 1, started

        result = await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.status == "printing"))
        assert len(list(result.scalars().all())) == 1

    async def test_budget_two_starts_two_per_tick(
        self, test_engine, db_session, printer_factory, monkeypatch
    ):
        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.models.settings import Settings
        from backend.app.services import print_scheduler as ps
        from backend.app.services.print_scheduler import PrintScheduler

        maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr(ps, "async_session", maker)

        printers = [await printer_factory(model="H2S", name=f"H2S2-{i}") for i in range(3)]
        for pos, p in enumerate(printers):
            db_session.add(PrintQueueItem(printer_id=p.id, status="pending", position=pos))
        db_session.add(Settings(key="stagger_group_size", value="2"))
        db_session.add(Settings(key="stagger_interval_minutes", value="5"))
        await db_session.commit()

        sched = PrintScheduler()
        monkeypatch.setattr(ps.printer_manager, "is_connected", lambda pid: True)
        monkeypatch.setattr(ps.printer_manager, "is_quarantined", lambda pid: False)
        monkeypatch.setattr(ps.printer_manager, "is_awaiting_plate_clear", lambda pid: False)
        monkeypatch.setattr(ps.printer_manager, "get_status", lambda pid: SimpleNamespace(state="IDLE"))

        started: list[int] = []

        async def fake_start(db, item):
            item.status = "printing"
            item.started_at = datetime.now(timezone.utc)
            await db.commit()
            started.append(item.id)

        monkeypatch.setattr(sched, "_start_print", fake_start)
        monkeypatch.setattr(sched, "_check_auto_drying", AsyncMock())
        monkeypatch.setattr(sched, "_block_on_filament_deficit", AsyncMock(return_value=False))
        monkeypatch.setattr(sched, "_compute_ams_mapping_for_printer", AsyncMock(return_value=None))

        await sched.check_queue()
        assert len(started) == 2, started
