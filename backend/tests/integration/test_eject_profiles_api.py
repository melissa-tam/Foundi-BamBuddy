"""Integration tests for the eject-profiles API: CRUD + preview + dry-run +
one-click dry-run dispatch."""

import hashlib
import io
import zipfile

import pytest
from httpx import AsyncClient
from sqlalchemy import select

_PLATE_GCODE = (
    "; HEADER_BLOCK_START\n"
    "; BambuStudio 02.07.01.57\n"
    "; max_z_height: 20.00\n"
    "; HEADER_BLOCK_END\n"
    "; CONFIG_BLOCK_START\n"
    "; nozzle_diameter = 0.6\n"
    "; CONFIG_BLOCK_END\n"
    "; EXECUTABLE_BLOCK_START\n"
    "M104 S220\n"
    "G1 X100 Y100 E5\n"
    "; MACHINE_END_GCODE_START\n"
    "M104 S0\n"
    "; EXECUTABLE_BLOCK_END\n"
)

# Same shape but with the machine-end markers absent — used to prove the dry-run
# builder refuses to produce a file that would end FAILED-at-EOF on the printer.
_PLATE_GCODE_NO_MACHINE_END = (
    "; HEADER_BLOCK_START\n"
    "; max_z_height: 20.00\n"
    "; HEADER_BLOCK_END\n"
    "; EXECUTABLE_BLOCK_START\n"
    "M104 S220\n"
    "G1 X100 Y100 E5\n"
    "; EXECUTABLE_BLOCK_END\n"
)


def _valid_profile_body(name="rack-a", **overrides):
    body = {"name": name}
    body.update(overrides)
    return body


def _write_3mf(path, *, gcode=_PLATE_GCODE, plate_id=1, with_md5=True, with_gcode=True):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        if with_gcode:
            zf.writestr(f"Metadata/plate_{plate_id}.gcode", gcode)
            if with_md5:
                zf.writestr(f"Metadata/plate_{plate_id}.gcode.md5", "STALEHASH")
        zf.writestr("3D/3dmodel.model", "<model/>")


async def _add_library_file(db_session, tmp_path, *, name="src.gcode.3mf", **kw):
    from backend.app.models.library import LibraryFile

    disk = tmp_path / name
    _write_3mf(disk, **kw)
    lib = LibraryFile(
        filename=name,
        file_path=str(disk),
        file_type="gcode.3mf",
        file_size=disk.stat().st_size,
        is_external=True,
    )
    db_session.add(lib)
    await db_session.commit()
    await db_session.refresh(lib)
    return lib


@pytest.mark.asyncio
@pytest.mark.integration
class TestEjectProfileCrud:
    async def test_create_list_get_update_delete(self, async_client: AsyncClient):
        # CREATE -> 201
        resp = await async_client.post("/api/v1/eject-profiles", json=_valid_profile_body())
        assert resp.status_code == 201, resp.text
        created = resp.json()
        pid = created["id"]
        assert created["name"] == "rack-a"
        assert created["cooldown_temp_c"] == 28.0
        assert created["cooldown_retries"] == 5

        # LIST -> 200 includes it
        resp = await async_client.get("/api/v1/eject-profiles")
        assert resp.status_code == 200
        assert any(p["id"] == pid for p in resp.json())

        # GET -> 200
        resp = await async_client.get(f"/api/v1/eject-profiles/{pid}")
        assert resp.status_code == 200
        assert resp.json()["id"] == pid

        # PUT -> 200 updates cooldown
        resp = await async_client.put(f"/api/v1/eject-profiles/{pid}", json={"cooldown_temp_c": 25.0})
        assert resp.status_code == 200
        assert resp.json()["cooldown_temp_c"] == 25.0

        # DELETE -> 204
        resp = await async_client.delete(f"/api/v1/eject-profiles/{pid}")
        assert resp.status_code == 204

        # GET after delete -> 404
        resp = await async_client.get(f"/api/v1/eject-profiles/{pid}")
        assert resp.status_code == 404

    async def test_duplicate_name_409(self, async_client: AsyncClient):
        await async_client.post("/api/v1/eject-profiles", json=_valid_profile_body(name="dup"))
        resp = await async_client.post("/api/v1/eject-profiles", json=_valid_profile_body(name="dup"))
        assert resp.status_code == 409

    async def test_invalid_z_offset_422(self, async_client: AsyncClient):
        resp = await async_client.post("/api/v1/eject-profiles", json=_valid_profile_body(name="lowz", z_offset_mm=0.1))
        assert resp.status_code == 422

    async def test_invalid_retries_422(self, async_client: AsyncClient):
        resp = await async_client.post(
            "/api/v1/eject-profiles", json=_valid_profile_body(name="manyretry", cooldown_retries=99)
        )
        assert resp.status_code == 422

    async def test_get_missing_404(self, async_client: AsyncClient):
        assert (await async_client.get("/api/v1/eject-profiles/987654")).status_code == 404

    async def test_delete_missing_404(self, async_client: AsyncClient):
        assert (await async_client.delete("/api/v1/eject-profiles/987654")).status_code == 404

    async def test_sweep_tuning_round_trips(self, async_client: AsyncClient):
        # Create with band + fractional start height; CREATE and GET echo them back.
        body = _valid_profile_body(name="banded", sweep_x_min_mm=50.0, sweep_x_max_mm=200.0, sweep_start_frac=0.5)
        resp = await async_client.post("/api/v1/eject-profiles", json=body)
        assert resp.status_code == 201, resp.text
        created = resp.json()
        assert created["sweep_x_min_mm"] == 50.0
        assert created["sweep_x_max_mm"] == 200.0
        assert created["sweep_start_frac"] == 0.5

        got = (await async_client.get(f"/api/v1/eject-profiles/{created['id']}")).json()
        assert got["sweep_x_min_mm"] == 50.0
        assert got["sweep_x_max_mm"] == 200.0
        assert got["sweep_start_frac"] == 0.5

    async def test_sweep_tuning_defaults(self, async_client: AsyncClient):
        # Omitting the new fields -> band null/null, frac 1.0 (unchanged behaviour).
        resp = await async_client.post("/api/v1/eject-profiles", json=_valid_profile_body(name="plain"))
        assert resp.status_code == 201, resp.text
        created = resp.json()
        assert created["sweep_x_min_mm"] is None
        assert created["sweep_x_max_mm"] is None
        assert created["sweep_start_frac"] == 1.0

    async def test_one_sided_band_422(self, async_client: AsyncClient):
        resp = await async_client.post(
            "/api/v1/eject-profiles", json=_valid_profile_body(name="oneside", sweep_x_min_mm=50.0)
        )
        assert resp.status_code == 422

    async def test_narrow_band_422(self, async_client: AsyncClient):
        resp = await async_client.post(
            "/api/v1/eject-profiles",
            json=_valid_profile_body(name="narrow", sweep_x_min_mm=50.0, sweep_x_max_mm=55.0),
        )
        assert resp.status_code == 422

    async def test_inverted_band_422(self, async_client: AsyncClient):
        resp = await async_client.post(
            "/api/v1/eject-profiles",
            json=_valid_profile_body(name="inv", sweep_x_min_mm=200.0, sweep_x_max_mm=50.0),
        )
        assert resp.status_code == 422

    async def test_frac_zero_422(self, async_client: AsyncClient):
        resp = await async_client.post(
            "/api/v1/eject-profiles", json=_valid_profile_body(name="fraczero", sweep_start_frac=0.0)
        )
        assert resp.status_code == 422

    async def test_frac_above_one_422(self, async_client: AsyncClient):
        resp = await async_client.post(
            "/api/v1/eject-profiles", json=_valid_profile_body(name="frachigh", sweep_start_frac=1.5)
        )
        assert resp.status_code == 422

    async def test_update_sweep_start_frac(self, async_client: AsyncClient):
        pid = (await async_client.post("/api/v1/eject-profiles", json=_valid_profile_body(name="updfrac"))).json()["id"]
        resp = await async_client.put(f"/api/v1/eject-profiles/{pid}", json={"sweep_start_frac": 0.25})
        assert resp.status_code == 200, resp.text
        assert resp.json()["sweep_start_frac"] == 0.25

    async def test_update_band_together(self, async_client: AsyncClient):
        pid = (await async_client.post("/api/v1/eject-profiles", json=_valid_profile_body(name="updband"))).json()["id"]
        resp = await async_client.put(
            f"/api/v1/eject-profiles/{pid}", json={"sweep_x_min_mm": 40.0, "sweep_x_max_mm": 180.0}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["sweep_x_min_mm"] == 40.0
        assert resp.json()["sweep_x_max_mm"] == 180.0

    async def test_update_one_sided_band_422(self, async_client: AsyncClient):
        pid = (await async_client.post("/api/v1/eject-profiles", json=_valid_profile_body(name="updone"))).json()["id"]
        resp = await async_client.put(f"/api/v1/eject-profiles/{pid}", json={"sweep_x_min_mm": 40.0})
        assert resp.status_code == 422

    async def test_final_skim_defaults_true(self, async_client: AsyncClient):
        # Omitting final_skim -> True (unchanged behaviour: trailing skim pass on).
        resp = await async_client.post("/api/v1/eject-profiles", json=_valid_profile_body(name="skimdefault"))
        assert resp.status_code == 201, resp.text
        assert resp.json()["final_skim"] is True

    async def test_final_skim_round_trips(self, async_client: AsyncClient):
        # Create with final_skim False; CREATE and GET echo it back.
        body = _valid_profile_body(name="noskim", final_skim=False)
        resp = await async_client.post("/api/v1/eject-profiles", json=body)
        assert resp.status_code == 201, resp.text
        created = resp.json()
        assert created["final_skim"] is False
        got = (await async_client.get(f"/api/v1/eject-profiles/{created['id']}")).json()
        assert got["final_skim"] is False

    async def test_update_final_skim(self, async_client: AsyncClient):
        # Default (True) profile -> PUT flips final_skim to False and back.
        pid = (await async_client.post("/api/v1/eject-profiles", json=_valid_profile_body(name="updskim"))).json()["id"]
        resp = await async_client.put(f"/api/v1/eject-profiles/{pid}", json={"final_skim": False})
        assert resp.status_code == 200, resp.text
        assert resp.json()["final_skim"] is False
        resp = await async_client.put(f"/api/v1/eject-profiles/{pid}", json={"final_skim": True})
        assert resp.status_code == 200, resp.text
        assert resp.json()["final_skim"] is True


@pytest.mark.asyncio
@pytest.mark.integration
class TestEjectProfilePreview:
    async def test_preview_happy(self, async_client: AsyncClient, db_session, tmp_path):
        resp = await async_client.post("/api/v1/eject-profiles", json=_valid_profile_body(name="prev"))
        pid = resp.json()["id"]
        lib = await _add_library_file(db_session, tmp_path, name="prev.gcode.3mf")

        resp = await async_client.post(
            f"/api/v1/eject-profiles/{pid}/preview",
            json={"library_file_id": lib.id, "plate_index": 1},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["max_z_height"] == 20.0
        assert body["validation"]["ok"] is True
        assert body["validation"]["errors"] == []
        assert "; ===== FARM EJECT BLOCK profile=prev =====" in body["gcode"]
        assert body["gcode"].count("M190 R28") == 5

    async def test_preview_tall_part_validation_false(self, async_client: AsyncClient, db_session, tmp_path):
        # Profile guard below the file's 20mm part -> generation refused, surfaced as ok=false.
        resp = await async_client.post(
            "/api/v1/eject-profiles", json=_valid_profile_body(name="prevshort", max_part_height_mm=10.0)
        )
        pid = resp.json()["id"]
        lib = await _add_library_file(db_session, tmp_path, name="prevshort.gcode.3mf")
        resp = await async_client.post(
            f"/api/v1/eject-profiles/{pid}/preview", json={"library_file_id": lib.id, "plate_index": 1}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["validation"]["ok"] is False
        assert body["gcode"] == ""

    async def test_preview_unknown_profile_404(self, async_client: AsyncClient, db_session, tmp_path):
        lib = await _add_library_file(db_session, tmp_path, name="p404.gcode.3mf")
        resp = await async_client.post(
            "/api/v1/eject-profiles/987654/preview", json={"library_file_id": lib.id, "plate_index": 1}
        )
        assert resp.status_code == 404

    async def test_preview_unknown_file_404(self, async_client: AsyncClient):
        resp = await async_client.post("/api/v1/eject-profiles", json=_valid_profile_body(name="pf404"))
        pid = resp.json()["id"]
        resp = await async_client.post(
            f"/api/v1/eject-profiles/{pid}/preview", json={"library_file_id": 987654, "plate_index": 1}
        )
        assert resp.status_code == 404

    async def test_preview_no_gcode_422(self, async_client: AsyncClient, db_session, tmp_path):
        resp = await async_client.post("/api/v1/eject-profiles", json=_valid_profile_body(name="p422"))
        pid = resp.json()["id"]
        lib = await _add_library_file(db_session, tmp_path, name="empty.3mf", with_gcode=False)
        resp = await async_client.post(
            f"/api/v1/eject-profiles/{pid}/preview", json={"library_file_id": lib.id, "plate_index": 1}
        )
        assert resp.status_code == 422


@pytest.mark.asyncio
@pytest.mark.integration
class TestEjectProfileDryRun:
    async def test_dry_run_replaces_gcode_and_recomputes_md5(self, async_client: AsyncClient, db_session, tmp_path):
        resp = await async_client.post("/api/v1/eject-profiles", json=_valid_profile_body(name="dry"))
        pid = resp.json()["id"]
        lib = await _add_library_file(db_session, tmp_path, name="dry.gcode.3mf")

        resp = await async_client.post(
            f"/api/v1/eject-profiles/{pid}/dry-run", json={"library_file_id": lib.id, "plate_index": 1}
        )
        assert resp.status_code == 200, resp.text
        assert "dryrun_dry_" in resp.headers.get("content-disposition", "")

        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            gcode_bytes = zf.read("Metadata/plate_1.gcode")
            sidecar = zf.read("Metadata/plate_1.gcode.md5")
        gcode = gcode_bytes.decode()
        # Eject block replaced the executable body; the original print move is gone.
        assert "; ===== FARM EJECT BLOCK profile=dry =====" in gcode
        assert "G1 X100 Y100 E5" not in gcode
        # Dry run validates GEOMETRY on an empty ambient bed, so the thermal gate
        # is stripped: NO M190 release waits (they could never complete and would
        # hang the job) and no cooldown fan wait. The heater safety-off stays.
        assert "M190" not in gcode
        assert "M106 S255" not in gcode
        assert "M140 S0" in gcode
        # But the sweep + centre park geometry is still present.
        assert "G1 X170 Y160 Z10 F9000" in gcode
        # Header/config comment block preserved.
        assert "; max_z_height: 20.00" in gcode
        assert gcode.rstrip().endswith("; EXECUTABLE_BLOCK_END")
        # Dry-run homing safety: a FULL G28 (incl. Z — safe only on the dry
        # run's by-definition-empty bed) is prepended BEFORE the eject block so
        # the block's G1 Z moves run against a homed Z, not an unknown datum.
        lines = gcode.splitlines()
        bare_g28_idxs = [i for i, ln in enumerate(lines) if ln.strip() == "G28"]
        block_start_idx = lines.index("; ===== FARM EJECT BLOCK profile=dry =====")
        assert len(bare_g28_idxs) == 1  # exactly one, and none inside the block
        assert bare_g28_idxs[0] < block_start_idx
        # md5 sidecar recomputed to match the rewritten bytes (uppercase hex).
        expected = hashlib.md5(gcode_bytes, usedforsecurity=False).hexdigest().upper().encode("ascii")
        assert sidecar == expected

    async def test_dry_run_splices_machine_end_block(self, async_client: AsyncClient, db_session, tmp_path):
        # The stock machine-end block (MACHINE_END_GCODE_START..EXECUTABLE_BLOCK_END)
        # must be spliced in VERBATIM after the eject block so the printer sees the
        # job-completion handshake (else it ends FAILED at EOF). The eject block
        # comes BEFORE the machine-end (opposite of production injection).
        pid = (await async_client.post("/api/v1/eject-profiles", json=_valid_profile_body(name="mend"))).json()["id"]
        lib = await _add_library_file(db_session, tmp_path, name="mend.gcode.3mf")

        resp = await async_client.post(
            f"/api/v1/eject-profiles/{pid}/dry-run", json={"library_file_id": lib.id, "plate_index": 1}
        )
        assert resp.status_code == 200, resp.text
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            gcode_bytes = zf.read("Metadata/plate_1.gcode")
            sidecar = zf.read("Metadata/plate_1.gcode.md5")
        gcode = gcode_bytes.decode()

        # The source machine-end block is present, in full, exactly once.
        assert "; MACHINE_END_GCODE_START" in gcode
        assert gcode.count("; EXECUTABLE_BLOCK_END") == 1
        assert "M104 S0" in gcode  # a line unique to the stock machine-end block
        # Ordering: eject block, THEN machine-end block, THEN the closing marker.
        eject_idx = gcode.index("; ===== FARM EJECT BLOCK profile=mend =====")
        mend_idx = gcode.index("; MACHINE_END_GCODE_START")
        end_idx = gcode.index("; EXECUTABLE_BLOCK_END")
        assert eject_idx < mend_idx < end_idx
        # Thermal gate still stripped, file still ends at the completion marker.
        assert "M190" not in gcode
        assert gcode.rstrip().endswith("; EXECUTABLE_BLOCK_END")
        # MD5 sidecar tracks the rewritten bytes (uppercase hex).
        expected = hashlib.md5(gcode_bytes, usedforsecurity=False).hexdigest().upper().encode("ascii")
        assert sidecar == expected

    async def test_dry_run_missing_machine_end_markers_422(self, async_client: AsyncClient, db_session, tmp_path):
        # A source without the machine-end markers can't be given a completion
        # handshake — the builder must refuse rather than ship a FAILED-at-EOF file.
        pid = (await async_client.post("/api/v1/eject-profiles", json=_valid_profile_body(name="nomend"))).json()["id"]
        lib = await _add_library_file(db_session, tmp_path, name="nomend.gcode.3mf", gcode=_PLATE_GCODE_NO_MACHINE_END)
        resp = await async_client.post(
            f"/api/v1/eject-profiles/{pid}/dry-run", json={"library_file_id": lib.id, "plate_index": 1}
        )
        assert resp.status_code == 422
        assert "machine-end" in resp.json()["detail"].lower()

    async def test_dry_run_unknown_profile_404(self, async_client: AsyncClient, db_session, tmp_path):
        lib = await _add_library_file(db_session, tmp_path, name="d404.gcode.3mf")
        resp = await async_client.post(
            "/api/v1/eject-profiles/987654/dry-run", json={"library_file_id": lib.id, "plate_index": 1}
        )
        assert resp.status_code == 404

    async def test_dry_run_no_gcode_422(self, async_client: AsyncClient, db_session, tmp_path):
        resp = await async_client.post("/api/v1/eject-profiles", json=_valid_profile_body(name="d422"))
        pid = resp.json()["id"]
        lib = await _add_library_file(db_session, tmp_path, name="dempty.3mf", with_gcode=False)
        resp = await async_client.post(
            f"/api/v1/eject-profiles/{pid}/dry-run", json={"library_file_id": lib.id, "plate_index": 1}
        )
        assert resp.status_code == 422


def _isolate_library_dir(monkeypatch, tmp_path):
    """Redirect library writes (save_3mf_bytes_to_library) into the pytest tmp
    dir so a dispatch test never pollutes the real data/archive dir."""
    from backend.app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "archive_dir", tmp_path)
    monkeypatch.setattr(app_settings, "base_dir", tmp_path)


@pytest.mark.asyncio
@pytest.mark.integration
class TestEjectDryRunDispatch:
    async def test_dispatch_happy_path(
        self, async_client: AsyncClient, db_session, tmp_path, printer_factory, monkeypatch
    ):
        from backend.app.models.library import LibraryFile
        from backend.app.models.print_queue import PrintQueueItem

        _isolate_library_dir(monkeypatch, tmp_path)
        printer = await printer_factory(model="H2S", name="H2S-D1")
        pid = (await async_client.post("/api/v1/eject-profiles", json=_valid_profile_body(name="disp"))).json()["id"]
        lib = await _add_library_file(db_session, tmp_path, name="disp.gcode.3mf")

        resp = await async_client.post(
            f"/api/v1/eject-profiles/{pid}/dry-run/dispatch",
            json={"library_file_id": lib.id, "plate_index": 1, "printer_id": printer.id},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["queue_item_id"]
        assert data["library_file_id"]
        # Response guides the operator to confirm the bed is empty (softened: the
        # dry run is now handled as a no-deposit finish, so no plate-clear caveat).
        assert "empty" in data["message"].lower()

        # Library file persisted under the canonical DRY-RUN name.
        created = (
            await db_session.execute(LibraryFile.active().where(LibraryFile.id == data["library_file_id"]))
        ).scalar_one()
        assert created.filename == "DRY-RUN disp.gcode.3mf"
        assert created.file_type == "gcode.3mf"

        # Queue item exists in the DB with the safe test flags, ASAP, on the printer.
        qi = (
            await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.id == data["queue_item_id"]))
        ).scalar_one()
        assert qi.printer_id == printer.id
        assert qi.plate_id == 1
        assert qi.library_file_id == created.id
        assert qi.bed_levelling is False
        assert qi.vibration_cali is False
        assert qi.use_ams is False
        assert qi.skip_filament_check is True
        assert qi.manual_start is False
        assert qi.scheduled_time is None
        assert qi.status == "pending"
        # Marked as a dry run so a stop/finish is treated as a no-deposit finish.
        assert qi.is_dry_run is True

    async def test_dispatch_replaces_prior_dryrun_file(
        self, async_client: AsyncClient, db_session, tmp_path, printer_factory, monkeypatch
    ):
        from backend.app.models.library import LibraryFile

        _isolate_library_dir(monkeypatch, tmp_path)
        printer = await printer_factory(model="H2S", name="H2S-D2")
        pid = (await async_client.post("/api/v1/eject-profiles", json=_valid_profile_body(name="disp2"))).json()["id"]
        lib = await _add_library_file(db_session, tmp_path, name="disp2.gcode.3mf")
        payload = {"library_file_id": lib.id, "plate_index": 1, "printer_id": printer.id}

        first = await async_client.post(f"/api/v1/eject-profiles/{pid}/dry-run/dispatch", json=payload)
        assert first.status_code == 200, first.text
        second = await async_client.post(f"/api/v1/eject-profiles/{pid}/dry-run/dispatch", json=payload)
        assert second.status_code == 200, second.text

        # Exactly ONE active DRY-RUN file survives (the prior copy was soft-deleted),
        # and it is the second dispatch file — artifacts do not accumulate.
        db_session.expire_all()
        active = (
            (await db_session.execute(LibraryFile.active().where(LibraryFile.filename == "DRY-RUN disp2.gcode.3mf")))
            .scalars()
            .all()
        )
        assert len(active) == 1
        assert active[0].id == second.json()["library_file_id"]
        assert active[0].id != first.json()["library_file_id"]

    async def test_dispatch_busy_printer_409(
        self, async_client: AsyncClient, db_session, tmp_path, printer_factory, monkeypatch
    ):
        from types import SimpleNamespace

        from backend.app.services.printer_manager import printer_manager

        _isolate_library_dir(monkeypatch, tmp_path)
        printer = await printer_factory(model="H2S", name="H2S-D3")
        pid = (await async_client.post("/api/v1/eject-profiles", json=_valid_profile_body(name="disp3"))).json()["id"]
        lib = await _add_library_file(db_session, tmp_path, name="disp3.gcode.3mf")

        # Printer live-reports a RUNNING print — must refuse (409), never queue a
        # bed-homing sweep into an active job.
        monkeypatch.setattr(printer_manager, "get_status", lambda _pid: SimpleNamespace(state="RUNNING"))
        resp = await async_client.post(
            f"/api/v1/eject-profiles/{pid}/dry-run/dispatch",
            json={"library_file_id": lib.id, "plate_index": 1, "printer_id": printer.id},
        )
        assert resp.status_code == 409

        # PAUSE is equally unsafe.
        monkeypatch.setattr(printer_manager, "get_status", lambda _pid: SimpleNamespace(state="PAUSE"))
        resp = await async_client.post(
            f"/api/v1/eject-profiles/{pid}/dry-run/dispatch",
            json={"library_file_id": lib.id, "plate_index": 1, "printer_id": printer.id},
        )
        assert resp.status_code == 409

    async def test_dispatch_unsliced_plate_422(
        self, async_client: AsyncClient, db_session, tmp_path, printer_factory, monkeypatch
    ):
        _isolate_library_dir(monkeypatch, tmp_path)
        printer = await printer_factory(model="H2S", name="H2S-D4")
        pid = (await async_client.post("/api/v1/eject-profiles", json=_valid_profile_body(name="disp4"))).json()["id"]
        lib = await _add_library_file(db_session, tmp_path, name="disp4.3mf", with_gcode=False)

        resp = await async_client.post(
            f"/api/v1/eject-profiles/{pid}/dry-run/dispatch",
            json={"library_file_id": lib.id, "plate_index": 1, "printer_id": printer.id},
        )
        assert resp.status_code == 422

    async def test_dispatch_unknown_printer_404(self, async_client: AsyncClient, db_session, tmp_path, monkeypatch):
        _isolate_library_dir(monkeypatch, tmp_path)
        pid = (await async_client.post("/api/v1/eject-profiles", json=_valid_profile_body(name="disp5"))).json()["id"]
        lib = await _add_library_file(db_session, tmp_path, name="disp5.gcode.3mf")

        resp = await async_client.post(
            f"/api/v1/eject-profiles/{pid}/dry-run/dispatch",
            json={"library_file_id": lib.id, "plate_index": 1, "printer_id": 987654},
        )
        assert resp.status_code == 404

    async def test_dispatch_unknown_profile_404(
        self, async_client: AsyncClient, db_session, tmp_path, printer_factory, monkeypatch
    ):
        _isolate_library_dir(monkeypatch, tmp_path)
        printer = await printer_factory(model="H2S", name="H2S-D6")
        lib = await _add_library_file(db_session, tmp_path, name="disp6.gcode.3mf")
        resp = await async_client.post(
            "/api/v1/eject-profiles/987654/dry-run/dispatch",
            json={"library_file_id": lib.id, "plate_index": 1, "printer_id": printer.id},
        )
        assert resp.status_code == 404

    async def test_dispatch_unknown_file_404(
        self, async_client: AsyncClient, db_session, tmp_path, printer_factory, monkeypatch
    ):
        _isolate_library_dir(monkeypatch, tmp_path)
        printer = await printer_factory(model="H2S", name="H2S-D7")
        pid = (await async_client.post("/api/v1/eject-profiles", json=_valid_profile_body(name="disp7"))).json()["id"]
        resp = await async_client.post(
            f"/api/v1/eject-profiles/{pid}/dry-run/dispatch",
            json={"library_file_id": 987654, "plate_index": 1, "printer_id": printer.id},
        )
        assert resp.status_code == 404

    async def test_dispatch_requires_auth_401(self, async_client: AsyncClient, db_session):
        # Auth enabled + no credentials -> 401 before any body logic runs.
        from backend.app.models.settings import Settings

        db_session.add(Settings(key="auth_enabled", value="true"))
        await db_session.commit()
        resp = await async_client.post(
            "/api/v1/eject-profiles/1/dry-run/dispatch",
            json={"library_file_id": 1, "plate_index": 1, "printer_id": 1},
        )
        assert resp.status_code == 401

    async def test_dispatch_forbidden_without_queue_create_403(self, async_client: AsyncClient, db_session):
        # Auth enabled + an API key WITHOUT can_queue (QUEUE_CREATE scope) -> 403.
        from backend.app.core.auth import generate_api_key
        from backend.app.models.api_key import APIKey
        from backend.app.models.settings import Settings

        db_session.add(Settings(key="auth_enabled", value="true"))
        full_key, key_hash, key_prefix = generate_api_key()
        db_session.add(
            APIKey(
                name="ro-key",
                key_hash=key_hash,
                key_prefix=key_prefix,
                can_read_status=True,
                can_queue=False,
                enabled=True,
            )
        )
        await db_session.commit()

        resp = await async_client.post(
            "/api/v1/eject-profiles/1/dry-run/dispatch",
            json={"library_file_id": 1, "plate_index": 1, "printer_id": 1},
            headers={"X-API-Key": full_key},
        )
        assert resp.status_code == 403
