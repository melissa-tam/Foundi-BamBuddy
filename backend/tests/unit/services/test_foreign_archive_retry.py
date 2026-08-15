"""In-flight 3MF capture for prints the farm did not dispatch.

Thirteen foreign terminals between 2026-08-01 and 2026-08-09 charged ZERO grams:
a Bambu Studio / hand-spliced LAN print echoes a degenerate
``Metadata/plate_N.gcode`` name, the print-start locate misses, and by the terminal
event the printer has already deleted its copy of the source ``.gcode.3mf`` — so
the archive row never gets a file and ``usage_tracker`` has no slicer data to
charge from. The file is guaranteed present while the job RUNS, which is the only
window these tests care about.

Covered here: the start-side arming decision (foreign arms, farm does not, a
successful start-time capture does not), the retry's own behaviour (re-derives
names from the live printer state, attaches file + parsed metadata, no-ops when a
file is already attached), and the end-to-end claim that a captured foreign print
charges filament again.
"""

import zipfile
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services import foreign_archive
from backend.app.services.bambu_ftp import FileNotOnPrinterError
from backend.app.services.foreign_archive import ThreeMFLookup

# The degenerate echo the incident's foreign prints all carried.
DEGENERATE_FILE = "Metadata/plate_1.gcode"
DEGENERATE_SUBTASK = "project_file"
# What the printer publishes a little later — the names the file is actually
# stored under, and the whole reason a retry can succeed where the start missed.
LIVE_SUBTASK = "Bracket v4"
LIVE_FILE = "Bracket v4.gcode.3mf"


def _three_mf_bytes(tmp_path: Path, grams: float = 50.0, seconds: int = 1800, plates: tuple[int, ...] = (1,)) -> bytes:
    """A 3MF carrying both readers' shapes: plate metadata for the archive parse
    (ThreeMFParser) and a filament entry for the usage extractor. The archive row
    and the charge come off the same captured file, so a fixture that satisfies
    only one of them would prove nothing.

    ``plates`` names the plate indices to emit; the plate-mismatch check reads the
    FIRST one, which is what makes a multi-plate project file look "wrong" to a
    printer that is running a later plate (#1204).
    """
    plate_blocks = []
    for index in plates:
        plate_blocks.extend(
            [
                "  <plate>",
                f'    <metadata key="index" value="{index}"/>',
                f'    <metadata key="prediction" value="{seconds}"/>',
                f'    <metadata key="weight" value="{grams}"/>',
                f'    <filament id="1" used_g="{grams}" type="PLA" color="#FF0000"/>',
                "  </plate>",
            ]
        )
    slice_info = "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            "<config>",
            *plate_blocks,
            "</config>",
        ]
    )
    source = tmp_path / "source.gcode.3mf"
    with zipfile.ZipFile(source, "w") as zf:
        zf.writestr("Metadata/slice_info.config", slice_info)
    return source.read_bytes()


def _settled_printer_manager() -> MagicMock:
    """printer_manager stand-in for a finished print — no live AMS mapping left."""
    pm = MagicMock()
    pm.get_status.return_value = SimpleNamespace(
        raw_data={},
        progress=100,
        layer_num=1,
        tray_now=0,
        last_loaded_tray=0,
        tray_change_log=[],
        total_layers=1,
    )
    return pm


def _ftp_lane(available: dict[str, bytes]) -> AsyncMock:
    """download_file_async stand-in: writes a known remote path, 550s everything else."""

    async def _download(_ip, _access_code, remote_path, dest, **_kwargs):
        payload = available.get(remote_path)
        if payload is None:
            raise FileNotOnPrinterError(f"550 {remote_path}")
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)
        return True

    return AsyncMock(side_effect=_download)


def _session_factory(db):
    """Stand in for ``async_session()`` with the test's own session."""

    class _CM:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_exc):
            return False

    return lambda: _CM()


async def _seed_foreign_archive(db, printer_id: int):
    """The row on_print_start writes when it cannot find the 3MF: no file at all."""
    from backend.app.models.archive import PrintArchive

    archive = PrintArchive(
        printer_id=printer_id,
        filename=DEGENERATE_FILE,
        file_path="",
        file_size=0,
        print_name="plate_1",
        status="printing",
        started_at=datetime.now(timezone.utc) - timedelta(minutes=3),
        extra_data={"no_3mf_available": True, "original_subtask": DEGENERATE_SUBTASK},
    )
    db.add(archive)
    await db.commit()
    await db.refresh(archive)
    return archive


# ---------------------------------------------------------------------------
# Start-side arming decision (driven through the real on_print_start callback)
# ---------------------------------------------------------------------------


def _start_harness(printer, queue_items, *, spawned):
    """Patch bundle for driving main.on_print_start down to the no-3MF fallback."""
    from unittest.mock import patch as _patch

    def execute_router(stmt, *_args, **_kwargs):
        sql = str(stmt).lower()
        if "from printers" in sql:
            return MagicMock(
                scalar_one_or_none=MagicMock(return_value=printer),
                scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[printer]))),
            )
        if "from print_queue" in sql:
            return MagicMock(
                scalar_one_or_none=MagicMock(return_value=queue_items[0] if queue_items else None),
                scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=list(queue_items)))),
            )
        return MagicMock(
            scalar_one_or_none=MagicMock(return_value=None),
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))),
        )

    async def _refresh(obj, *_a, **_kw):
        obj.id = 4242

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock()
    session.execute = AsyncMock(side_effect=execute_router)
    session.commit = AsyncMock()
    session.refresh = AsyncMock(side_effect=_refresh)

    def fake_spawn(coro, *, name=None):
        spawned.append(name)
        coro.close()  # never run it here — arming is what this asserts

    patches = [
        _patch("backend.app.main.async_session", MagicMock(return_value=session)),
        _patch(
            "backend.app.main.ws_manager", MagicMock(send_print_start=AsyncMock(), send_archive_created=AsyncMock())
        ),
        _patch("backend.app.main.mqtt_relay", MagicMock(on_print_start=AsyncMock(), on_archive_created=AsyncMock())),
        _patch("backend.app.main.smart_plug_manager", MagicMock(on_print_start=AsyncMock())),
        _patch("backend.app.main.printer_manager", MagicMock(get_printer=MagicMock(return_value=None))),
        _patch("backend.app.main.notify_missing_spool_assignments_on_print_start", new=AsyncMock()),
        _patch("backend.app.main._send_print_start_notification", new=AsyncMock()),
        _patch("backend.app.main._store_spoolman_print_data", new=AsyncMock()),
        _patch("backend.app.main._record_energy_start", new=AsyncMock()),
        _patch("backend.app.main._capture_timelapse_baseline_at_start", new=AsyncMock()),
        _patch("backend.app.main._maybe_start_layer_timelapse", MagicMock()),
        _patch("backend.app.main._load_objects_from_archive", MagicMock()),
        _patch.object(foreign_archive, "spawn_background_task", fake_spawn),
    ]
    return patches, session


def _farm_printer():
    printer = MagicMock()
    printer.id = 3
    printer.name = "003-H2S"
    printer.auto_archive = True
    printer.plate_detection_enabled = False
    printer.external_camera_enabled = False
    printer.external_camera_url = None
    return printer


@pytest.fixture(autouse=True)
def _clear_main_state():
    from backend.app.main import _active_prints, _expected_prints, _print_ams_mappings

    _expected_prints.clear()
    _active_prints.clear()
    _print_ams_mappings.clear()
    yield
    _expected_prints.clear()
    _active_prints.clear()
    _print_ams_mappings.clear()


@pytest.mark.asyncio
async def test_foreign_start_miss_arms_the_capture_retry():
    """Degenerate names + a failing locate + no farm queue item → retry armed.

    This is the incident shape: without the retry the archive stays file-less and
    the print charges nothing for the rest of its life.
    """
    from backend.app.main import on_print_start

    spawned: list[str | None] = []
    patches, session = _start_harness(_farm_printer(), [], spawned=spawned)
    miss = ThreeMFLookup(local_path=None, filename=None, subtask_name=DEGENERATE_SUBTASK, expected_plate=1)

    with patch("backend.app.main.locate_3mf_for_print", new=AsyncMock(return_value=miss)):
        for p in patches:
            p.start()
        try:
            await on_print_start(
                3,
                {
                    "filename": DEGENERATE_FILE,
                    "subtask_name": DEGENERATE_SUBTASK,
                    "raw_data": {"subtask_id": "FOREIGN-77"},
                },
            )
        finally:
            for p in reversed(patches):
                p.stop()

    assert spawned == ["foreign-3mf-capture-3-4242"], (
        "a foreign print that missed its 3MF at start must retry the capture in flight"
    )
    # The fallback archive was still created — the retry is additive, not a detour.
    assert session.add.called


@pytest.mark.asyncio
async def test_farm_print_start_miss_does_not_arm_the_retry():
    """A dispatched farm unit owns this print: its 3MF is already in the library,
    where usage_tracker's fallback lookup finds it. No printer round-trip needed."""
    from backend.app.main import on_print_start

    farm_item = MagicMock()
    farm_item.id = 91
    farm_item.dispatch_subtask_id = "FARM-11"

    spawned: list[str | None] = []
    patches, _session = _start_harness(_farm_printer(), [farm_item], spawned=spawned)
    miss = ThreeMFLookup(local_path=None, filename=None, subtask_name="SKU007.01", expected_plate=1)

    with patch("backend.app.main.locate_3mf_for_print", new=AsyncMock(return_value=miss)):
        for p in patches:
            p.start()
        try:
            await on_print_start(
                3,
                {
                    "filename": "SKU007.01.gcode.3mf",
                    "subtask_name": "SKU007.01",
                    "raw_data": {"subtask_id": "FARM-11"},
                },
            )
        finally:
            for p in reversed(patches):
                p.stop()

    assert spawned == [], "an attributed farm print must not schedule a printer capture retry"


@pytest.mark.asyncio
async def test_successful_start_capture_does_not_arm_the_retry(tmp_path):
    """Nothing to retry when the start-time capture already has the file."""
    from backend.app.main import on_print_start

    source = tmp_path / "Bracket v4.gcode.3mf"
    source.write_bytes(_three_mf_bytes(tmp_path))
    hit = ThreeMFLookup(local_path=source, filename=LIVE_FILE, subtask_name=LIVE_SUBTASK, expected_plate=1)

    spawned: list[str | None] = []
    patches, _session = _start_harness(_farm_printer(), [], spawned=spawned)
    schedule = AsyncMock(return_value=False)
    archive_service = MagicMock()
    archive_service.archive_print = AsyncMock(return_value=None)

    with (
        patch("backend.app.main.locate_3mf_for_print", new=AsyncMock(return_value=hit)),
        patch("backend.app.main.maybe_schedule_foreign_3mf_retry", new=schedule),
        patch("backend.app.main.ArchiveService", MagicMock(return_value=archive_service)),
    ):
        for p in patches:
            p.start()
        try:
            await on_print_start(
                3,
                {
                    "filename": LIVE_FILE,
                    "subtask_name": LIVE_SUBTASK,
                    "raw_data": {"subtask_id": "FOREIGN-78"},
                },
            )
        finally:
            for p in reversed(patches):
                p.stop()

    assert archive_service.archive_print.await_count == 1
    schedule.assert_not_awaited()
    assert spawned == []


# ---------------------------------------------------------------------------
# The retry itself (real DB rows, real archive attach, faked FTPS lane)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_captures_from_live_state_and_attaches_metadata(db_session, printer_factory, tmp_path, monkeypatch):
    """The retry re-derives candidate names from the ENRICHED live printer state
    (which the degenerate start echo did not carry) and attaches the file plus its
    parsed metadata to the archive row that was created without one."""
    from backend.app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archives")

    printer = await printer_factory()
    archive = await _seed_foreign_archive(db_session, printer.id)

    lane = _ftp_lane({f"/cache/{LIVE_FILE}": _three_mf_bytes(tmp_path, grams=64.0)})
    live_state = SimpleNamespace(subtask_name=LIVE_SUBTASK, gcode_file=LIVE_FILE)

    with (
        patch.object(foreign_archive, "async_session", _session_factory(db_session)),
        patch.object(foreign_archive, "get_cached_3mf", MagicMock(return_value=None)),
        patch.object(foreign_archive, "cache_3mf_download", MagicMock()),
        patch.object(foreign_archive, "get_ftp_retry_settings", AsyncMock(return_value=(False, 0, 0.0, 5.0))),
        patch.object(foreign_archive, "download_file_async", lane),
        patch.object(foreign_archive, "list_files_async", AsyncMock(return_value=[])),
        patch.object(foreign_archive.printer_manager, "get_status", MagicMock(return_value=live_state)),
    ):
        # Zero delays keep the real retry loop (not a hand-called attempt) in play.
        await foreign_archive._retry_capture(printer.id, archive.id, DEGENERATE_SUBTASK, DEGENERATE_FILE, (0.0, 0.0))

    await db_session.refresh(archive)
    assert archive.file_path, "the captured 3MF must be attached to the existing archive row"
    assert (tmp_path / archive.file_path).is_file()
    assert archive.file_size > 0
    assert archive.filament_used_grams == pytest.approx(64.0, abs=0.1), "parsed metadata must land on the row too"
    assert archive.extra_data.get("no_3mf_available") is None, "the no-3MF marker must not survive a capture"
    assert archive.extra_data.get("original_subtask") == DEGENERATE_SUBTASK, "row's own keys survive the merge"
    # Row identity is the start callback's, not the capture's.
    assert archive.filename == DEGENERATE_FILE
    assert archive.status == "printing"


@pytest.mark.asyncio
async def test_retry_no_ops_when_a_file_is_already_attached(db_session, printer_factory, tmp_path, monkeypatch):
    """Idempotency vs the terminal path: if the archive already gained a file, the
    retry must not download, re-parse, or overwrite anything."""
    from backend.app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archives")

    printer = await printer_factory()
    archive = await _seed_foreign_archive(db_session, printer.id)
    archive.file_path = "archives/already/there.gcode.3mf"
    archive.file_size = 123
    await db_session.commit()

    lane = _ftp_lane({f"/cache/{LIVE_FILE}": _three_mf_bytes(tmp_path)})

    with (
        patch.object(foreign_archive, "async_session", _session_factory(db_session)),
        patch.object(foreign_archive, "download_file_async", lane),
        patch.object(foreign_archive, "get_ftp_retry_settings", AsyncMock(return_value=(False, 0, 0.0, 5.0))),
    ):
        done = await foreign_archive._attempt_capture(printer.id, archive.id, DEGENERATE_SUBTASK, DEGENERATE_FILE, 1)

    assert done is True, "an already-attached archive ends the retry"
    lane.assert_not_awaited()
    await db_session.refresh(archive)
    assert archive.file_path == "archives/already/there.gcode.3mf"
    assert archive.file_size == 123


@pytest.mark.asyncio
async def test_retry_gives_up_loudly_when_the_file_is_gone(db_session, printer_factory, tmp_path, monkeypatch, caplog):
    """Every candidate 550s (the printer already deleted its copy) → no write, and
    one honest line saying the print will charge nothing."""
    import logging

    from backend.app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archives")

    printer = await printer_factory()
    archive = await _seed_foreign_archive(db_session, printer.id)

    with (
        patch.object(foreign_archive, "async_session", _session_factory(db_session)),
        patch.object(foreign_archive, "get_cached_3mf", MagicMock(return_value=None)),
        patch.object(foreign_archive, "get_ftp_retry_settings", AsyncMock(return_value=(False, 0, 0.0, 5.0))),
        patch.object(foreign_archive, "download_file_async", _ftp_lane({})),
        patch.object(foreign_archive, "list_files_async", AsyncMock(return_value=[])),
        patch.object(foreign_archive.printer_manager, "get_status", MagicMock(return_value=None)),
        caplog.at_level(logging.WARNING, logger="backend.app.services.foreign_archive"),
    ):
        await foreign_archive._retry_capture(printer.id, archive.id, DEGENERATE_SUBTASK, DEGENERATE_FILE, (0.0, 0.0))

    await db_session.refresh(archive)
    assert archive.file_path == ""
    assert any("will charge no filament" in record.getMessage() for record in caplog.records)


# ---------------------------------------------------------------------------
# Charge liveness — the claim the whole wave exists for
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_captured_foreign_print_charges_filament_again(db_session, printer_factory, tmp_path, monkeypatch):
    """END-TO-END: a foreign print whose archive gained its 3MF through the retry
    charges grams at completion. This is the 13-terminal zero-charge class — with
    a file-less archive ``on_print_complete`` returns [] and the spool never moves.
    """
    from backend.app.core.config import settings as app_settings
    from backend.app.models.spool import Spool
    from backend.app.models.spool_assignment import SpoolAssignment
    from backend.app.services.usage_tracker import _active_sessions, on_print_complete

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archives")
    _active_sessions.clear()

    printer = await printer_factory()
    archive = await _seed_foreign_archive(db_session, printer.id)
    spool = Spool(material="PLA", label_weight=1000, weight_used=0.0)
    db_session.add(spool)
    await db_session.commit()
    await db_session.refresh(spool)
    db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=printer.id, ams_id=0, tray_id=0))
    await db_session.commit()

    # Zero-charge baseline: the file-less archive has nothing to charge from.
    assert (
        await on_print_complete(
            printer_id=printer.id,
            data={"status": "completed"},
            printer_manager=_settled_printer_manager(),
            db=db_session,
            archive_id=archive.id,
        )
        == []
    )

    lane = _ftp_lane({f"/{LIVE_FILE}": _three_mf_bytes(tmp_path, grams=42.0)})
    with (
        patch.object(foreign_archive, "async_session", _session_factory(db_session)),
        patch.object(foreign_archive, "get_cached_3mf", MagicMock(return_value=None)),
        patch.object(foreign_archive, "cache_3mf_download", MagicMock()),
        patch.object(foreign_archive, "get_ftp_retry_settings", AsyncMock(return_value=(False, 0, 0.0, 5.0))),
        patch.object(foreign_archive, "download_file_async", lane),
        patch.object(foreign_archive, "list_files_async", AsyncMock(return_value=[])),
        patch.object(
            foreign_archive.printer_manager,
            "get_status",
            MagicMock(return_value=SimpleNamespace(subtask_name=LIVE_SUBTASK, gcode_file=LIVE_FILE)),
        ),
    ):
        captured = await foreign_archive._attempt_capture(
            printer.id, archive.id, DEGENERATE_SUBTASK, DEGENERATE_FILE, 1
        )
    assert captured is True

    results = await on_print_complete(
        printer_id=printer.id,
        data={"status": "completed"},
        printer_manager=_settled_printer_manager(),
        db=db_session,
        archive_id=archive.id,
    )

    await db_session.refresh(spool)
    charged = sum(r["weight_used"] for r in results)
    assert charged > 0, "a captured foreign print must charge filament"
    assert charged == pytest.approx(42.0, abs=0.1)
    assert spool.weight_used == pytest.approx(42.0, abs=0.1)


# ---------------------------------------------------------------------------
# Cache-hit lane: a donor served from the shared download cache is BORROWED
# ---------------------------------------------------------------------------
# The cache also holds durable paths — dispatch publishes the library file's own
# path so /cover can skip FTP (#1166). The plate-mismatch branch used to unlink
# whatever the cache handed it, which in production deleted a library file.


@contextmanager
def _mismatch_lane(cached: Path, available: dict[str, bytes]):
    """locate_3mf_for_print with the cache serving ``cached`` and FTPS serving
    ``available`` — the shape that drives the #1204 plate-mismatch correction."""
    with ExitStack() as stack:
        for p in (
            patch.object(foreign_archive, "get_cached_3mf", MagicMock(return_value=cached)),
            patch.object(foreign_archive, "cache_3mf_download", MagicMock()),
            patch.object(foreign_archive, "get_ftp_retry_settings", AsyncMock(return_value=(False, 0, 0.0, 5.0))),
            patch.object(foreign_archive, "download_file_async", _ftp_lane(available)),
            patch.object(foreign_archive, "list_files_async", AsyncMock(return_value=[])),
        ):
            stack.enter_context(p)
        yield


def _library_donor(tmp_path: Path) -> tuple[Path, bytes]:
    """A multi-plate library file the cache serves for a print running plate 3."""
    payload = _three_mf_bytes(tmp_path, plates=(1, 3))
    library_file = tmp_path / "library_files" / "abcd.3mf"
    library_file.parent.mkdir(parents=True, exist_ok=True)
    library_file.write_bytes(payload)
    return library_file, payload


MISMATCH_SUBTASK = "Bracket v4 - Plate 1"  # stale echo: the printer is on plate 3
MISMATCH_FILE = "Metadata/plate_3.gcode"
CORRECTED_FILE = "Bracket v4 - Plate 3.gcode.3mf"


@pytest.mark.asyncio
async def test_plate_mismatch_keeps_the_borrowed_library_file_when_the_retry_succeeds(tmp_path, monkeypatch):
    """The cache hit is somebody else's file — replacing it must not delete it."""
    from backend.app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archives")
    library_file, payload = _library_donor(tmp_path)
    printer = SimpleNamespace(id=3, ip_address="10.0.0.3", access_code="12345678", model="H2S")

    with _mismatch_lane(library_file, {f"/cache/{CORRECTED_FILE}": _three_mf_bytes(tmp_path, plates=(3,))}):
        lookup = await foreign_archive.locate_3mf_for_print(printer, MISMATCH_SUBTASK, MISMATCH_FILE)

    assert lookup.filename == CORRECTED_FILE, "the corrected re-download is what the caller archives"
    assert lookup.local_path is not None and lookup.local_path != library_file
    assert library_file.exists(), "the borrowed library file must survive the plate-mismatch swap"
    assert library_file.read_bytes() == payload


@pytest.mark.asyncio
async def test_plate_mismatch_keeps_the_borrowed_library_file_when_the_retry_fails(tmp_path, monkeypatch):
    """Same file, and the fallback branch that gives up on the correction —
    the one that unlinked a production library file."""
    from backend.app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archives")
    library_file, payload = _library_donor(tmp_path)
    printer = SimpleNamespace(id=3, ip_address="10.0.0.3", access_code="12345678", model="H2S")

    with _mismatch_lane(library_file, {}):
        lookup = await foreign_archive.locate_3mf_for_print(printer, MISMATCH_SUBTASK, MISMATCH_FILE)

    assert lookup.found is False, "no correct plate available → the caller falls back to a no-3MF archive"
    assert lookup.local_path is None
    assert lookup.subtask_name == "Bracket v4 - Plate 3", "the fallback archive is named for the right plate"
    assert library_file.exists(), "the borrowed library file must survive the give-up branch"
    assert library_file.read_bytes() == payload
