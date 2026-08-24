"""Filament accounting for prints the farm did not dispatch.

A large share of this farm's work is started from Bambu Studio over LAN or from
the printer's own screen. Those prints turn real filament off real rolls, and
three independent breaks meant they were charging nothing. Measured on production
``desktop-g6cgc9k``, the 30 days to 2026-08-23: **68** archives created with no 3MF
at all, **57** of them (84 %) logged as ``3MF plate mismatch … reports plate 1 but
printer is running plate 3``; a further **24** prints captured their file but had
the charge skipped with ``skipping 3MF usage to avoid charging the whole file``;
40 of 200 archives since 08-14 carry zero or NULL grams, 38 of those from full
~5.8 h prints. Two 5.7-hour prints finishing 2026-08-23 00:48/00:50 on printers 1
and 6 charged nothing at all.

Nothing corrects this direction. An under-charged roll reads FULLER than it is,
clears the 150 g ``min_start_spool_g`` floor, and starts a print it cannot finish.
For a tagless tray the slicer 3MF is the ONLY gram source — this fleet's AMS
answers ``remain: -1`` for every untagged roll — and doctrine rule 4 makes tagless
gram tracking mandatory.

The three breaks, each pinned below:

* **B1 — the plate predicate.** ``peek_plate_index_in_3mf`` read the FIRST
  ``<plate>`` block, so every multi-plate 3MF answered "plate 1" whichever plate
  was running, and the print-start guard threw the correct file away.
  ``plate_indices_in_3mf`` asks the question that was always meant: does this file
  CONTAIN the running plate.
* **B2 — the name comparison.** The directory-listing lane normalised the two
  sides differently, leaving a mid-stem ``.gcode`` token on the candidate side, so
  the substring test could never be true for this corpus. Both sides now go
  through ``print_identity_key``. That lane had never been exercised by a test:
  every existing one stubs ``list_files_async`` to ``[]``.
* **B3 — the plate had nowhere to live.** ``PrintArchive.plate_id`` stores what
  the printer states at print start, so completion can read it hours later when
  neither a session nor a queue item exists.

And **B4** — when a loss does happen anyway, it must be loud rather than silent,
without paging on the eject sweeps whose zero grams are correct.
"""

import logging
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.services import foreign_archive
from backend.app.services.bambu_ftp import FileNotOnPrinterError

# The real production shape: the splicer names files
# "<project>.gcode_L1-NN_spliced.3mf" and the printer echoes the same stem with
# the ".gcode" token gone. The token sits INSIDE the candidate, which is what
# broke contiguity for the old substring test.
SPLICED_FILE = "Rotary_tool_top_surfaces_PCO-M12-2525.gcode_L1-88_spliced.3mf"
SPLICED_ECHO = "Rotary_tool_top_surfaces_PCO-M12-2525_L1-88_spliced"
OTHER_CUT_FILE = "Rotary_tool_top_surfaces_PCO-M12-2525.gcode_L1-90_spliced.3mf"

RUNNING_PLATE = 3
RUNNING_GCODE_FILE = "/data/Metadata/plate_3.gcode"

PLATE_GRAMS = {1: 120.0, 2: 61.5, 3: 418.13, 4: 92.0}


def _multi_plate_3mf(dest: Path, grams_by_plate: dict[int, float]) -> Path:
    """A 3MF whose slice_info declares several plates, in the order given.

    Plate 1 first is the shape that made a printer running plate 3 look like a
    stale-name mismatch to the old first-block predicate.
    """
    blocks: list[str] = []
    for index, grams in grams_by_plate.items():
        blocks.extend(
            [
                "  <plate>",
                f'    <metadata key="index" value="{index}"/>',
                '    <metadata key="prediction" value="19860"/>',
                f'    <metadata key="weight" value="{grams}"/>',
                f'    <filament id="1" used_g="{grams}" used_m="130.96" type="PETG" color="#161616"/>',
                "  </plate>",
            ]
        )
    slice_info = "\n".join(['<?xml version="1.0" encoding="UTF-8"?>', "<config>", *blocks, "</config>"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr("Metadata/slice_info.config", slice_info)
        zf.writestr(f"Metadata/plate_{RUNNING_PLATE}.gcode", "; truncated ladder gcode\n")
    return dest


def _printer_stub() -> SimpleNamespace:
    return SimpleNamespace(id=3, ip_address="10.0.0.3", access_code="12345678", model="H2S")


def _ftp_lane(available: dict[str, bytes]) -> AsyncMock:
    """download_file_async stand-in: serves known remote paths, 550s everything else."""

    async def _download(_ip, _access_code, remote_path, dest, **_kwargs):
        payload = available.get(remote_path)
        if payload is None:
            raise FileNotOnPrinterError(f"550 {remote_path}")
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)
        return True

    return AsyncMock(side_effect=_download)


def _locate_lane(cached: Path | None, available: dict[str, bytes], listing: list[dict] | None = None):
    """Patch bundle for ``locate_3mf_for_print`` — cache, FTPS and the directory
    listing all under our control.

    ``listing`` is the piece every other test in the tree stubs to ``[]``; the
    directory-search lane has never been exercised until this file.
    """
    lane = _ftp_lane(available)
    patches = [
        patch.object(foreign_archive, "get_cached_3mf", MagicMock(return_value=cached)),
        patch.object(foreign_archive, "cache_3mf_download", MagicMock()),
        patch.object(foreign_archive, "get_ftp_retry_settings", AsyncMock(return_value=(False, 0, 0.0, 5.0))),
        patch.object(foreign_archive, "download_file_async", lane),
        patch.object(foreign_archive, "list_files_async", AsyncMock(return_value=listing or [])),
    ]
    return patches, lane


async def _locate(printer, subtask_name, filename, *, cached=None, available=None, listing=None):
    patches, lane = _locate_lane(cached, available or {}, listing)
    for p in patches:
        p.start()
    try:
        return await foreign_archive.locate_3mf_for_print(printer, subtask_name, filename), lane
    finally:
        for p in reversed(patches):
            p.stop()


def _settled_printer_manager(last_loaded_tray: int = 0, mapping: list[int] | None = None) -> MagicMock:
    """printer_manager stand-in for a finished print."""
    pm = MagicMock()
    pm.get_status.return_value = SimpleNamespace(
        raw_data={"mapping": mapping} if mapping is not None else {},
        progress=100,
        layer_num=88,
        tray_now=255,
        last_loaded_tray=last_loaded_tray,
        tray_change_log=[],
        total_layers=88,
    )
    return pm


async def _seed_tagless_spool(db, printer_id: int, *, ams_id: int = 0, tray_id: int = 0):
    """A tagless roll seated in a slot — no tag_uid, no tray_uuid, no sibling."""
    from backend.app.models.spool import Spool
    from backend.app.models.spool_assignment import SpoolAssignment

    spool = Spool(material="PETG", label_weight=1000, weight_used=0.0, data_origin="ams_auto")
    db.add(spool)
    await db.commit()
    await db.refresh(spool)
    db.add(SpoolAssignment(spool_id=spool.id, printer_id=printer_id, ams_id=ams_id, tray_id=tray_id))
    await db.commit()
    return spool


async def _seed_foreign_archive(db, printer_id: int, *, file_path: str = "", plate_id: int | None = None, extra=None):
    """The archive row ``on_print_start`` writes for a print no farm unit claims."""
    from backend.app.models.archive import PrintArchive

    archive = PrintArchive(
        printer_id=printer_id,
        filename=SPLICED_FILE,
        file_path=file_path,
        file_size=0,
        print_name=SPLICED_ECHO,
        status="printing",
        started_at=datetime.now(timezone.utc) - timedelta(hours=5),
        plate_id=plate_id,
        extra_data=extra,
    )
    db.add(archive)
    await db.commit()
    await db.refresh(archive)
    return archive


# ---------------------------------------------------------------------------
# B1 — containment, not "is the first plate the running plate"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_multi_plate_file_containing_the_running_plate_is_accepted(tmp_path, monkeypatch):
    """THE 57: four plates, plate 1 declared first, printer running plate 3.

    The old predicate read the first block, compared 1 against 3 and discarded the
    file — the only gram source the print had.
    """
    from backend.app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archives")
    payload = _multi_plate_3mf(tmp_path / "ladder.3mf", PLATE_GRAMS).read_bytes()

    lookup, _lane = await _locate(
        _printer_stub(),
        SPLICED_ECHO,
        RUNNING_GCODE_FILE,
        available={f"/cache/{SPLICED_ECHO}.gcode.3mf": payload},
    )

    assert lookup.found is True, "a file that CONTAINS the running plate is the right file"
    assert lookup.expected_plate == RUNNING_PLATE, "and it is priced at the plate the printer is running"


@pytest.mark.asyncio
async def test_a_single_plate_upload_for_another_plate_is_still_refused(tmp_path, monkeypatch, caplog):
    """REGRESSION #1204: a stale ``subtask_name`` fetches the PREVIOUS plate's own
    single-plate upload. Its index set does not contain the running plate, so it is
    refused exactly as before — containment widens nothing here."""
    from backend.app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archives")
    stale = _multi_plate_3mf(tmp_path / "stale.3mf", {1: PLATE_GRAMS[1]})

    with caplog.at_level(logging.WARNING, logger="backend.app.services.foreign_archive"):
        lookup, _lane = await _locate(
            _printer_stub(), "Bracket v4 - Plate 1", "/data/Metadata/plate_2.gcode", cached=stale
        )

    assert lookup.found is False, "the stale-name mismatch must still be refused"
    assert lookup.local_path is None
    assert any("plate mismatch" in r.getMessage() for r in caplog.records)
    assert stale.exists(), "the borrowed cache file must survive the give-up branch"


@pytest.mark.asyncio
async def test_the_multi_plate_file_charges_the_running_plates_grams(db_session, printer_factory, tmp_path, monkeypatch):
    """B1's whole point: accepted AND priced at plate 3 — not zero, not the sum.

    ``419.63`` (the four plates added up) is the over-charge the plate scoping
    exists to prevent; ``0`` is the under-charge the discarded file produced.
    """
    from backend.app.core.config import settings as app_settings
    from backend.app.services.usage_tracker import _active_sessions, on_print_complete

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archives")
    _active_sessions.clear()

    printer = await printer_factory()
    ladder = _multi_plate_3mf(tmp_path / "captured" / "ladder.3mf", PLATE_GRAMS)
    spool = await _seed_tagless_spool(db_session, printer.id)
    archive = await _seed_foreign_archive(
        db_session, printer.id, file_path=str(ladder.relative_to(tmp_path)), plate_id=RUNNING_PLATE
    )

    results = await on_print_complete(
        printer_id=printer.id,
        data={"status": "completed"},
        printer_manager=_settled_printer_manager(mapping=[0]),
        db=db_session,
        archive_id=archive.id,
    )

    await db_session.refresh(spool)
    assert sum(r["weight_used"] for r in results) == pytest.approx(PLATE_GRAMS[RUNNING_PLATE], abs=0.1)
    assert spool.weight_used == pytest.approx(PLATE_GRAMS[RUNNING_PLATE], abs=0.1)
    assert spool.weight_used < sum(PLATE_GRAMS.values()), "the whole file is never charged to one run"


# ---------------------------------------------------------------------------
# B2 — one normaliser on both sides of the name comparison
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_directory_listing_finds_the_spliced_file_from_the_printers_echo(tmp_path, monkeypatch):
    """The lane no test has ever run. The stored file keeps a mid-stem ``.gcode``
    token the printer's echo drops, which is exactly what the old one-sided
    normalisation could not see past."""
    from backend.app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archives")
    payload = _multi_plate_3mf(tmp_path / "ladder.3mf", PLATE_GRAMS).read_bytes()

    lookup, lane = await _locate(
        _printer_stub(),
        SPLICED_ECHO,
        RUNNING_GCODE_FILE,
        available={f"/cache/{SPLICED_FILE}": payload},
        listing=[{"name": SPLICED_FILE, "is_directory": False}],
    )

    assert lookup.found is True, "the directory search must find the file the echo names"
    assert lookup.filename == SPLICED_FILE
    assert lookup.expected_plate == RUNNING_PLATE
    lane.assert_awaited()


@pytest.mark.asyncio
async def test_the_directory_listing_refuses_a_different_ladder_cut(tmp_path, monkeypatch):
    """NEAR MISS: ``_L1-88_`` and ``_L1-90_`` are different prints off the same
    project. Widening the comparison must not turn it into a fuzzy one — charging
    the wrong cut's grams is a wrong number, not a missing one."""
    from backend.app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archives")
    payload = _multi_plate_3mf(tmp_path / "other.3mf", PLATE_GRAMS).read_bytes()

    lookup, _lane = await _locate(
        _printer_stub(),
        SPLICED_ECHO,
        RUNNING_GCODE_FILE,
        available={f"/cache/{OTHER_CUT_FILE}": payload},
        listing=[{"name": OTHER_CUT_FILE, "is_directory": False}],
    )

    assert lookup.found is False, "a different layer range is a different print"


@pytest.mark.asyncio
async def test_the_directory_listing_prefers_the_exact_key_over_a_longer_neighbour(tmp_path, monkeypatch):
    """Equality is tested before containment, so listing order cannot hand the
    print to a file that merely CONTAINS its name."""
    from backend.app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archives")
    payload = _multi_plate_3mf(tmp_path / "ladder.3mf", PLATE_GRAMS).read_bytes()
    neighbour = "Rotary_tool_top_surfaces_PCO-M12-2525.gcode_L1-88_spliced_v2.3mf"

    lookup, _lane = await _locate(
        _printer_stub(),
        SPLICED_ECHO,
        RUNNING_GCODE_FILE,
        available={f"/cache/{SPLICED_FILE}": payload, f"/cache/{neighbour}": payload},
        # The longer neighbour is listed FIRST — under the old first-hit-wins scan
        # it would have won.
        listing=[{"name": neighbour, "is_directory": False}, {"name": SPLICED_FILE, "is_directory": False}],
    )

    assert lookup.filename == SPLICED_FILE


async def _seed_library_file(
    db, stored_name: str, base_dir: Path, grams_by_plate: dict[int, float], *, age_minutes: int = 0
) -> Path:
    """A library 3MF on disk plus its row, stored under its real name.

    ``age_minutes`` is set explicitly rather than left to ``server_default``: two
    rows added in the same instant get indistinguishable timestamps, and the
    newest-first ordering is load-bearing here.
    """
    from backend.app.models.library import LibraryFile

    path = _multi_plate_3mf(base_dir / "library" / stored_name, grams_by_plate)
    db.add(
        LibraryFile(
            filename=stored_name,
            file_path=f"library/{stored_name}",
            file_type="3mf",
            file_size=path.stat().st_size,
            created_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=age_minutes),
        )
    )
    await db.commit()
    return path


@pytest.mark.asyncio
async def test_the_library_rescue_finds_the_spliced_file_by_print_identity(
    db_session, printer_factory, tmp_path, monkeypatch
):
    """The lane that rescues a foreign print whose own 3MF is already gone.

    The printer deletes a print's source when it finishes, so B1 cannot help here —
    the only surviving copy is the library's. Matching it needs the two names to
    key the same: the stored file keeps its mid-stem ``.gcode``
    (``…PCO-M12-2525.gcode_L1-88_spliced.3mf``) and the printer echoes it without
    (``…PCO-M12-2525_L1-88_spliced``).

    This missed for the whole corpus under the old ``ilike`` pattern, and would have
    kept missing under a pattern built from ``print_identity_key`` — that key strips
    the ``.gcode`` the stored path still has, so no such pattern can ever match it.

    A NEWER, non-matching library row sits in front of the right one on purpose: it
    pins the query-shape ruling of 2026-08-20. Any ``LIMIT`` applied before the
    identity comparison would return only that decoy and make the true answer
    invisible — the same defect as the shape-32 resurrection, whose eligibility
    filter ahead of ``LIMIT 1`` silently answered with the newest row that passed.
    The decoy carries different grams so a wrong pick cannot hide behind an equal
    number.
    """
    from backend.app.core.config import settings as app_settings
    from backend.app.services.usage_tracker import _active_sessions, on_print_complete

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archives")
    _active_sessions.clear()

    printer = await printer_factory()
    await _seed_library_file(db_session, SPLICED_FILE, tmp_path, PLATE_GRAMS, age_minutes=60)
    await _seed_library_file(db_session, OTHER_CUT_FILE, tmp_path, {RUNNING_PLATE: 999.0}, age_minutes=0)
    spool = await _seed_tagless_spool(db_session, printer.id)
    # The archive as on_print_start wrote it: no file of its own, named by the echo.
    archive = await _seed_foreign_archive(db_session, printer.id, plate_id=RUNNING_PLATE)
    archive.filename = SPLICED_ECHO
    await db_session.commit()

    results = await on_print_complete(
        printer_id=printer.id,
        data={"status": "completed"},
        printer_manager=_settled_printer_manager(mapping=[0]),
        db=db_session,
        archive_id=archive.id,
    )

    await db_session.refresh(spool)
    assert sum(r["weight_used"] for r in results) == pytest.approx(PLATE_GRAMS[RUNNING_PLATE], abs=0.1), (
        "the library copy is found and the running plate's grams are charged"
    )
    assert spool.weight_used == pytest.approx(PLATE_GRAMS[RUNNING_PLATE], abs=0.1)


@pytest.mark.asyncio
async def test_the_library_rescue_refuses_a_different_ladder_cut(db_session, printer_factory, tmp_path, monkeypatch):
    """NEAR MISS: only ``_L1-90_`` is in the library and the printer ran ``_L1-88_``.

    Identity is compared as an EQUALITY on the normalised key, never as a SQL
    pattern — where ``_`` is a single-character wildcard and every one of these
    names is mostly underscores. Charging a different cut's grams is a wrong
    number, which is worse than the missing one.
    """
    from backend.app.core.config import settings as app_settings
    from backend.app.services.usage_tracker import _active_sessions, on_print_complete

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archives")
    _active_sessions.clear()

    printer = await printer_factory()
    await _seed_library_file(db_session, OTHER_CUT_FILE, tmp_path, PLATE_GRAMS)
    spool = await _seed_tagless_spool(db_session, printer.id)
    archive = await _seed_foreign_archive(db_session, printer.id, plate_id=RUNNING_PLATE)
    archive.filename = SPLICED_ECHO
    await db_session.commit()

    results = await on_print_complete(
        printer_id=printer.id,
        data={"status": "completed"},
        printer_manager=_settled_printer_manager(mapping=[0]),
        db=db_session,
        archive_id=archive.id,
    )

    await db_session.refresh(spool)
    assert results == []
    assert spool.weight_used == 0.0, "a different layer range is a different print"


# ---------------------------------------------------------------------------
# B3 — the plate is stored on the print, not re-derived from farm state
# ---------------------------------------------------------------------------


def _start_harness(printer, *, added):
    """Patch bundle driving ``main.on_print_start`` down to the no-3MF fallback."""

    def execute_router(stmt, *_args, **_kwargs):
        sql = str(stmt).lower()
        if "from printers" in sql:
            return MagicMock(
                scalar_one_or_none=MagicMock(return_value=printer),
                scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[printer]))),
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
    session.add = MagicMock(side_effect=added.append)

    return [
        patch("backend.app.main.async_session", MagicMock(return_value=session)),
        patch("backend.app.main.ws_manager", MagicMock(send_print_start=AsyncMock(), send_archive_created=AsyncMock())),
        patch("backend.app.main.mqtt_relay", MagicMock(on_print_start=AsyncMock(), on_archive_created=AsyncMock())),
        patch("backend.app.main.smart_plug_manager", MagicMock(on_print_start=AsyncMock())),
        patch("backend.app.main.printer_manager", MagicMock(get_printer=MagicMock(return_value=None))),
        patch("backend.app.main.notify_missing_spool_assignments_on_print_start", new=AsyncMock()),
        patch("backend.app.main._send_print_start_notification", new=AsyncMock()),
        patch("backend.app.main._record_energy_start", new=AsyncMock()),
        patch("backend.app.main._maybe_start_layer_timelapse", MagicMock()),
        patch("backend.app.main.maybe_schedule_foreign_3mf_retry", new=AsyncMock(return_value=False)),
        patch("backend.app.main._store_spoolman_print_data", new=AsyncMock()),
        patch("backend.app.main._capture_timelapse_baseline_at_start", new=AsyncMock()),
    ]


def _foreign_printer() -> MagicMock:
    printer = MagicMock()
    printer.id = 3
    printer.name = "003-H2S"
    printer.auto_archive = True
    printer.plate_detection_enabled = False
    printer.external_camera_enabled = False
    printer.external_camera_url = None
    return printer


@pytest.mark.asyncio
async def test_a_foreign_print_start_stamps_the_plate_from_the_gcode_echo():
    """The printer states the plate on every push; this is where it gets recorded.

    A fallback archive is exactly the row that will have no 3MF of its own, so it
    is the row that most needs the plate stored — by completion the echo is gone
    and no queue item can answer for a print the farm did not dispatch.
    """
    from backend.app.main import _active_prints, _expected_prints, on_print_start
    from backend.app.models.archive import PrintArchive
    from backend.app.services.foreign_archive import ThreeMFLookup

    _expected_prints.clear()
    _active_prints.clear()
    printer = _foreign_printer()

    added: list[object] = []
    miss = ThreeMFLookup(
        local_path=None, filename=None, subtask_name=SPLICED_ECHO, expected_plate=RUNNING_PLATE
    )
    patches = _start_harness(printer, added=added)

    with patch("backend.app.main.locate_3mf_for_print", new=AsyncMock(return_value=miss)):
        for p in patches:
            p.start()
        try:
            await on_print_start(
                3,
                {
                    "filename": RUNNING_GCODE_FILE,
                    "subtask_name": SPLICED_ECHO,
                    "raw_data": {"subtask_id": "FOREIGN-77"},
                },
            )
        finally:
            for p in reversed(patches):
                p.stop()
    _active_prints.clear()

    archives = [obj for obj in added if isinstance(obj, PrintArchive)]
    assert len(archives) == 1, "the fallback archive is still created"
    assert archives[0].plate_id == RUNNING_PLATE, "and it records the plate the printer is running"
    assert archives[0].extra_data.get("no_3mf_available") is True


@pytest.mark.asyncio
async def test_a_captured_foreign_print_start_also_stamps_the_plate(tmp_path):
    """The OTHER stamp site: the capture succeeded, so a real archive is written.

    Both paths must record it. A captured foreign print can still lose its file
    later (the archive's own copy is what completion reads), and its queue item
    never exists — so the archive row is the only place the plate can survive to
    completion here too.
    """
    from backend.app.main import _active_prints, _expected_prints, on_print_start
    from backend.app.services.foreign_archive import ThreeMFLookup

    _expected_prints.clear()
    _active_prints.clear()
    captured = _multi_plate_3mf(tmp_path / "captured" / "ladder.3mf", PLATE_GRAMS)
    hit = ThreeMFLookup(
        local_path=captured, filename=SPLICED_FILE, subtask_name=SPLICED_ECHO, expected_plate=RUNNING_PLATE
    )

    stub_archive = SimpleNamespace(
        id=99,
        printer_id=3,
        filename=SPLICED_FILE,
        print_name=SPLICED_ECHO,
        status="printing",
        file_path="captured/ladder.3mf",
        created_by_id=None,
        print_time_seconds=None,
        plate_id=None,
    )
    service = MagicMock()
    service.archive_print = AsyncMock(return_value=stub_archive)

    patches = _start_harness(_foreign_printer(), added=[])
    patches.append(patch("backend.app.main.ArchiveService", MagicMock(return_value=service)))

    with patch("backend.app.main.locate_3mf_for_print", new=AsyncMock(return_value=hit)):
        for p in patches:
            p.start()
        try:
            await on_print_start(
                3,
                {
                    "filename": RUNNING_GCODE_FILE,
                    "subtask_name": SPLICED_ECHO,
                    "raw_data": {"subtask_id": "FOREIGN-78"},
                },
            )
        finally:
            for p in reversed(patches):
                p.stop()
    _active_prints.clear()

    assert stub_archive.plate_id == RUNNING_PLATE, "the real-archive path stamps the plate too"


@pytest.mark.asyncio
async def test_completion_with_no_session_and_no_queue_item_charges_the_stamped_plate(
    db_session, printer_factory, tmp_path, monkeypatch, caplog
):
    """THE 24: a foreign print, a multi-plate 3MF, and neither farm tier can answer.

    This used to log ``skipping 3MF usage to avoid charging the whole file`` and
    charge nothing. The archive's own stamped plate is the durable fourth tier.
    """
    from backend.app.core.config import settings as app_settings
    from backend.app.services.usage_tracker import _active_sessions, on_print_complete

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archives")
    _active_sessions.clear()

    printer = await printer_factory()
    ladder = _multi_plate_3mf(tmp_path / "captured" / "ladder.3mf", PLATE_GRAMS)
    spool = await _seed_tagless_spool(db_session, printer.id)
    archive = await _seed_foreign_archive(
        db_session, printer.id, file_path=str(ladder.relative_to(tmp_path)), plate_id=RUNNING_PLATE
    )

    with caplog.at_level(logging.INFO, logger="backend.app.services.usage_tracker"):
        results = await on_print_complete(
            printer_id=printer.id,
            data={"status": "completed"},
            printer_manager=_settled_printer_manager(mapping=[0]),
            db=db_session,
            archive_id=archive.id,
        )

    await db_session.refresh(spool)
    assert sum(r["weight_used"] for r in results) == pytest.approx(PLATE_GRAMS[RUNNING_PLATE], abs=0.1)
    assert spool.weight_used == pytest.approx(PLATE_GRAMS[RUNNING_PLATE], abs=0.1)
    assert any("tier=archive" in r.getMessage() for r in caplog.records), (
        "the answering tier is named in the log — a plate from the wrong tier is the "
        "difference between charging one plate and charging none"
    )


@pytest.mark.asyncio
async def test_an_unstamped_foreign_completion_still_refuses_a_multi_plate_file(
    db_session, printer_factory, tmp_path, monkeypatch
):
    """LIVENESS of the guard the fix must not remove: with NO plate from any tier,
    a multi-plate file is still refused rather than summed into one run."""
    from backend.app.core.config import settings as app_settings
    from backend.app.services.usage_tracker import _active_sessions, on_print_complete

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archives")
    _active_sessions.clear()

    printer = await printer_factory()
    ladder = _multi_plate_3mf(tmp_path / "captured" / "ladder.3mf", PLATE_GRAMS)
    spool = await _seed_tagless_spool(db_session, printer.id)
    archive = await _seed_foreign_archive(
        db_session, printer.id, file_path=str(ladder.relative_to(tmp_path)), plate_id=None
    )

    results = await on_print_complete(
        printer_id=printer.id,
        data={"status": "completed"},
        printer_manager=_settled_printer_manager(mapping=[0]),
        db=db_session,
        archive_id=archive.id,
    )

    await db_session.refresh(spool)
    assert results == []
    assert spool.weight_used == 0.0, "an unknown plate is never charged as the whole file"


@pytest.mark.asyncio
async def test_a_stamped_plate_the_file_does_not_contain_refuses_to_charge(
    db_session, printer_factory, tmp_path, monkeypatch, caplog
):
    """VALIDITY, not trust: the degenerate screen-restart echo
    (``subtask_name="project_file"``, only ``/data/Metadata/plate_N.gcode``) can
    name a plate belonging to another print. A stamped plate the file never held
    charges nothing — an honest zero beats a confident wrong number."""
    from backend.app.core.config import settings as app_settings
    from backend.app.services.usage_tracker import _active_sessions, on_print_complete

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archives")
    _active_sessions.clear()

    printer = await printer_factory()
    single = _multi_plate_3mf(tmp_path / "captured" / "single.3mf", {1: PLATE_GRAMS[1]})
    spool = await _seed_tagless_spool(db_session, printer.id)
    archive = await _seed_foreign_archive(
        db_session, printer.id, file_path=str(single.relative_to(tmp_path)), plate_id=RUNNING_PLATE
    )

    with caplog.at_level(logging.WARNING, logger="backend.app.services.usage_tracker"):
        results = await on_print_complete(
            printer_id=printer.id,
            data={"status": "completed"},
            printer_manager=_settled_printer_manager(mapping=[0]),
            db=db_session,
            archive_id=archive.id,
        )

    await db_session.refresh(spool)
    assert results == []
    assert spool.weight_used == 0.0, "plate 1's grams are not plate 3's"
    assert any("never held this plate" in r.getMessage() for r in caplog.records)


@pytest.fixture
def _force_sqlite_dialect(monkeypatch):
    """The migration branches on the dialect; pin the SQLite arm."""
    from backend.app.core import database as database_module

    monkeypatch.setattr(database_module, "is_sqlite", lambda: True)


@pytest.mark.asyncio
async def test_the_plate_column_is_added_idempotently_to_an_existing_db(_force_sqlite_dialect):
    """``create_all`` never runs for a live install, so the raw ADD COLUMN in
    ``run_migrations`` is what actually builds this column in production — and a
    restart runs the whole sequence again."""
    import backend.app.models  # noqa: F401 — register the mapped models
    from backend.app.core.database import Base, run_migrations

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # Re-create the pre-migration shape: the column create_all just made.
            await conn.execute(text("ALTER TABLE print_archives DROP COLUMN plate_id"))
            await run_migrations(conn)
            await run_migrations(conn)  # second pass must be a no-op

        async with engine.connect() as conn:
            columns = (await conn.execute(text("PRAGMA table_info(print_archives)"))).fetchall()
        plate_columns = [c for c in columns if c[1] == "plate_id"]
        assert len(plate_columns) == 1, "added exactly once, however many times migrations run"
        assert plate_columns[0][2].upper() == "INTEGER"
        assert plate_columns[0][3] == 0, "NULLable — NULL is the honest value for every pre-existing row"
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# B4 — the loss is loud, and the eject sweeps stay silent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_lost_foreign_print_on_a_tagless_feeder_warns_and_pages(
    db_session, printer_factory, tmp_path, monkeypatch, caplog
):
    """The population the guard was written for and could not see.

    A screen start decides no dispatch mapping, so ``dispatch_only`` named no
    feeder and the page never fired. A real archive flagged ``no_3mf_available``
    is not an eject, so the observational witnesses are admitted here.
    """
    from backend.app.core.config import settings as app_settings
    from backend.app.services.usage_tracker import _active_sessions, on_print_complete

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archives")
    _active_sessions.clear()

    printer = await printer_factory(name="006-H2S")
    spool = await _seed_tagless_spool(db_session, printer.id)
    archive = await _seed_foreign_archive(
        db_session, printer.id, plate_id=RUNNING_PLATE, extra={"no_3mf_available": True}
    )

    notify = AsyncMock()
    with (
        patch("backend.app.services.notification_service.notification_service.on_zero_gram_charge", notify),
        caplog.at_level(logging.WARNING, logger="backend.app.services.usage_tracker"),
    ):
        results = await on_print_complete(
            printer_id=printer.id,
            data={"status": "completed"},
            printer_manager=_settled_printer_manager(last_loaded_tray=0),
            db=db_session,
            archive_id=archive.id,
        )

    assert results == [], "this layer reports the hole, it never invents grams"
    assert any("ZERO-GRAM CHARGE" in r.getMessage() for r in caplog.records)
    notify.assert_awaited_once()
    args = notify.await_args.args
    assert args[0] == printer.id
    assert args[1] == "006-H2S"
    assert args[4] == spool.id


@pytest.mark.asyncio
async def test_an_eject_sweep_completion_stays_silent(db_session, printer_factory, tmp_path, monkeypatch, caplog):
    """THE REGRESSION THAT MATTERS MOST: a false page here trains the operator to
    ignore a true one.

    An eject sweep runs with the previous print's roll still loaded, so the
    observational witnesses WOULD name that production slot for a job whose zero
    grams are correct. It creates no archive — ``on_print_start`` returns before
    archiving for any eject job — and that absence is the discriminator.
    """
    from backend.app.core.config import settings as app_settings
    from backend.app.services.usage_tracker import _active_sessions, on_print_complete

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archives")
    _active_sessions.clear()

    printer = await printer_factory()
    await _seed_tagless_spool(db_session, printer.id)

    notify = AsyncMock()
    with (
        patch("backend.app.services.notification_service.notification_service.on_zero_gram_charge", notify),
        caplog.at_level(logging.WARNING, logger="backend.app.services.usage_tracker"),
    ):
        results = await on_print_complete(
            printer_id=printer.id,
            data={"status": "completed", "subtask_name": "eject_production_item890"},
            printer_manager=_settled_printer_manager(last_loaded_tray=0),
            db=db_session,
            archive_id=None,
        )

    assert results == []
    notify.assert_not_awaited(), "an eject's zero grams are correct and must never page"
    assert not any("ZERO-GRAM CHARGE" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_farm_dispatched_dry_run_that_lost_its_3mf_stays_silent(
    db_session, printer_factory, tmp_path, monkeypatch, caplog
):
    """The near-miss an "is it an eject" test would have let through.

    An eject never archives — ``on_print_start`` returns before the archive body —
    but a DRY-RUN does: production ``desktop-g6cgc9k`` carries archives 733/734 on
    printer 2, 2026-08-18, ``print_name='DRY-RUN single-pass-dwell-jitter'``, 6.4 MB
    of captured 3MF. Only the successful fetch kept it out of the flagged
    population, so one transient FTPS failure would have handed a motion-only job
    the observational witnesses — which name the PREVIOUS print's still-loaded roll
    — and paged for zero grams that are entirely correct.

    Attribution is what excludes it: the dry-run is farm-dispatched and owns a queue
    item, so the widening never applies however its capture went.
    """
    from backend.app.core.config import settings as app_settings
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.services.usage_tracker import _active_sessions, on_print_complete

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archives")
    _active_sessions.clear()

    printer = await printer_factory()
    await _seed_tagless_spool(db_session, printer.id)
    # The dispatched unit. A motion-only job decides no AMS slot, so it carries no
    # mapping — exactly the shape that makes ``dispatch_only`` name no feeder.
    db_session.add(
        PrintQueueItem(
            printer_id=printer.id,
            status="printing",
            dispatch_subtask_id="DRYRUN-733",
            plate_id=None,
            ams_mapping=None,
            started_at=datetime.now(timezone.utc) - timedelta(minutes=4),
        )
    )
    await db_session.commit()
    # Its capture failed, so the archive carries the same flag a lost foreign print does.
    archive = await _seed_foreign_archive(db_session, printer.id, extra={"no_3mf_available": True})

    notify = AsyncMock()
    with (
        patch("backend.app.services.notification_service.notification_service.on_zero_gram_charge", notify),
        caplog.at_level(logging.WARNING, logger="backend.app.services.usage_tracker"),
    ):
        results = await on_print_complete(
            printer_id=printer.id,
            data={"status": "completed", "subtask_id": "DRYRUN-733"},
            printer_manager=_settled_printer_manager(last_loaded_tray=0),
            db=db_session,
            archive_id=archive.id,
        )

    assert results == []
    notify.assert_not_awaited(), "a farm-dispatched motion job's zero grams are correct and must never page"
    assert not any("ZERO-GRAM CHARGE" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_captured_foreign_print_that_charges_zero_does_not_widen_the_witnesses(
    db_session, printer_factory, tmp_path, monkeypatch
):
    """SCOPE: the widening is keyed to ``no_3mf_available``, not to "foreign".

    An archive that HAS its 3MF has no missing gram source to report — and
    ``attach_3mf_to_archive`` pops the flag when a late retry lands the file, so a
    rescued print stops matching this constellation.
    """
    from backend.app.core.config import settings as app_settings
    from backend.app.services.usage_tracker import _active_sessions, on_print_complete

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archives")
    _active_sessions.clear()

    printer = await printer_factory()
    await _seed_tagless_spool(db_session, printer.id)
    # A file with no filament entry for the stamped plate: captured, but charging
    # nothing — and NOT flagged as missing its 3MF.
    empty = tmp_path / "captured" / "empty.3mf"
    empty.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(empty, "w") as zf:
        zf.writestr("Metadata/slice_info.config", "<config></config>")
    archive = await _seed_foreign_archive(
        db_session, printer.id, file_path=str(empty.relative_to(tmp_path)), plate_id=RUNNING_PLATE
    )

    notify = AsyncMock()
    with patch("backend.app.services.notification_service.notification_service.on_zero_gram_charge", notify):
        await on_print_complete(
            printer_id=printer.id,
            data={"status": "completed"},
            printer_manager=_settled_printer_manager(last_loaded_tray=0),
            db=db_session,
            archive_id=archive.id,
        )

    notify.assert_not_awaited()
