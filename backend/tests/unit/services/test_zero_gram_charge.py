"""The zero-gram accounting hole: a farm print whose own 3MF was thrown away.

**The incident (2026-08-18 00:12 onward, 15 consecutive prints).** The production
run printed a hand-spliced ladder file whose ``Metadata/slice_info.config`` declares
plate **1** first while dispatch ran its ``Metadata/plate_3.gcode``. The print-start
capture downloaded that file correctly, then the #1204 stale-name cross-check
compared the internal 1 against the 3 parsed from the printer's ``gcode_file``,
declared the farm's OWN file a mismatch and discarded it. The name-correction retry
had nothing to work with (the filename carries no ``- Plate N`` suffix to swap), so
the print archived with NO 3MF at all.

For a TAGGED roll that is survivable — the AMS remain%-delta lane prices it anyway.
For a TAGLESS roll it is total: this fleet's AMS answers ``remain: -1`` for every
untagged roll, so the 3MF is the only gram source there is. And because every
failure inside ``usage_tracker``'s primary path returns an EMPTY list rather than
raising, the outcome was not an error but a SILENCE — spools 349 and 350 read
"1000 g, 0 g used" after 5.8 h prints each.

Two layers, both covered here:

* **Layer 1** — a farm dispatch never has to guess which file it is printing. The
  queue item names the source row and the plate, so the dispatch path SUPPLIES that
  donor and the guess-and-verify is skipped whole. The guard itself is untouched and
  still protects the foreign prints it was written for.
* **Layer 2** — a completed print that charged zero grams on a tagless feeder WARNs
  and notifies. Doctrine rule 4 makes tagless gram tracking mandatory, so the no-op
  is a defect and must never be quiet again (scenario C6).

The liveness pair is mandatory here (memory ``liveness-paired-verification``): layer
1 narrows when a lane runs and layer 2 is a suppression-shaped detector, so both
positive paths — a normal farm print charging its real grams, and a normal foreign
print archiving through the unchanged lane — are asserted alongside the fixes.
"""

import logging
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services import foreign_archive, usage_tracker
from backend.app.services.bambu_ftp import FileNotOnPrinterError
from backend.app.services.farm_correlation import DispatchDonor

# What the printer echoes for the incident's prints: the plate MEMBER it is running.
RUNNING_GCODE_FILE = "/data/Metadata/plate_3.gcode"
# The library file's own name — no "- Plate N" suffix, which is why the #1204
# name-correction retry had nothing to swap and the capture gave up entirely.
SPLICED_NAME = ".6_Half_Shell_top_surfaces_painted_seams_Topright_L1-88_spliced.gcode.3mf"
SPLICED_SUBTASK = ".6_Half_Shell_top_surfaces_painted_seams_Topright_L1-88_spliced"

DISPATCHED_PLATE = 3
PLATE_3_GRAMS = 418.13
PLATE_1_GRAMS = 120.0


def _multi_plate_3mf(dest: Path, grams_by_plate: dict[int, float], seconds: int = 19860) -> Path:
    """A 3MF whose slice_info declares several plates, FIRST one first.

    ``peek_plate_index_in_3mf`` reads ``.//plate`` — the first block — which is what
    makes a file declaring plate 1 look "wrong" to a printer running plate 3, and is
    the exact shape of the spliced ladder file the incident ran.
    """
    blocks: list[str] = []
    for index, grams in grams_by_plate.items():
        blocks.extend(
            [
                "  <plate>",
                f'    <metadata key="index" value="{index}"/>',
                f'    <metadata key="prediction" value="{seconds}"/>',
                f'    <metadata key="weight" value="{grams}"/>',
                f'    <filament id="1" used_g="{grams}" used_m="130.96" type="PETG" color="#161616"/>',
                "  </plate>",
            ]
        )
    slice_info = "\n".join(['<?xml version="1.0" encoding="UTF-8"?>', "<config>", *blocks, "</config>"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr("Metadata/slice_info.config", slice_info)
        zf.writestr(f"Metadata/plate_{DISPATCHED_PLATE}.gcode", "; truncated ladder gcode\n")
    return dest


def _library_donor(tmp_path: Path) -> Path:
    """The on-disk library file the farm dispatched — plate 1 declared first."""
    return _multi_plate_3mf(
        tmp_path / "library" / "a1a264fdc2ee457db79c1a295895614f.3mf",
        {1: PLATE_1_GRAMS, DISPATCHED_PLATE: PLATE_3_GRAMS},
    )


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


def _locate_lane(cached: Path | None, available: dict[str, bytes]):
    """Patch bundle for ``locate_3mf_for_print`` — cache + FTPS under our control."""
    lane = _ftp_lane(available)
    patches = [
        patch.object(foreign_archive, "get_cached_3mf", MagicMock(return_value=cached)),
        patch.object(foreign_archive, "cache_3mf_download", MagicMock()),
        patch.object(foreign_archive, "get_ftp_retry_settings", AsyncMock(return_value=(False, 0, 0.0, 5.0))),
        patch.object(foreign_archive, "download_file_async", lane),
        patch.object(foreign_archive, "list_files_async", AsyncMock(return_value=[])),
    ]
    return patches, lane


def _settled_printer_manager(last_loaded_tray: int = 0) -> MagicMock:
    """printer_manager stand-in for a finished print — no live AMS mapping left."""
    pm = MagicMock()
    pm.get_status.return_value = SimpleNamespace(
        raw_data={},
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


async def _seed_farm_item(db, printer_id: int, subtask_id: str, *, plate_id: int | None = DISPATCHED_PLATE):
    """The dispatched unit: the row that KNOWS the file and the plate."""
    from backend.app.models.print_queue import PrintQueueItem

    item = PrintQueueItem(
        printer_id=printer_id,
        status="printing",
        dispatch_subtask_id=subtask_id,
        plate_id=plate_id,
        ams_mapping="[0]",
        started_at=datetime.now(timezone.utc) - timedelta(hours=5),
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


async def _seed_archive(db, printer_id: int, *, file_path: str = "", print_name: str = "plate_3"):
    """The archive row on_print_start writes. ``file_path=""`` is the incident's."""
    from backend.app.models.archive import PrintArchive

    archive = PrintArchive(
        printer_id=printer_id,
        filename=RUNNING_GCODE_FILE,
        file_path=file_path,
        file_size=0,
        print_name=print_name,
        status="printing",
        started_at=datetime.now(timezone.utc) - timedelta(hours=5),
    )
    db.add(archive)
    await db.commit()
    await db.refresh(archive)
    return archive


# ---------------------------------------------------------------------------
# Layer 1 — the supplied donor outranks the internal plate index
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_supplied_donor_survives_a_disagreeing_internal_plate(tmp_path, monkeypatch):
    """THE INCIDENT: internal plate 1, dispatched plate 3, and the file still wins.

    Without the donor this exact input is what the mismatch guard discarded 15 times.
    """
    from backend.app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archives")
    donor_path = _library_donor(tmp_path)
    donor = DispatchDonor(local_path=donor_path, filename=SPLICED_NAME, plate_id=DISPATCHED_PLATE, item_id=815)

    patches, lane = _locate_lane(None, {})
    for p in patches:
        p.start()
    try:
        lookup = await foreign_archive.locate_3mf_for_print(
            _printer_stub(), SPLICED_SUBTASK, RUNNING_GCODE_FILE, known_donor=donor
        )
    finally:
        for p in reversed(patches):
            p.stop()

    assert lookup.found is True, "a farm dispatch must never fail to resolve the file it is printing"
    assert lookup.local_path == donor_path
    assert lookup.filename == SPLICED_NAME
    assert lookup.expected_plate == DISPATCHED_PLATE, (
        "the DISPATCHED plate is authoritative, not the file's first block"
    )
    lane.assert_not_awaited(), "a known donor needs no printer round-trip at all"
    assert donor_path.exists(), "the donor is BORROWED — the locate lane must never consume it"


@pytest.mark.asyncio
async def test_supplied_donor_falls_back_to_derivation_when_its_bytes_are_gone(tmp_path, monkeypatch):
    """Fail-open by design: a deleted library file degrades to guessing, not to a
    hard failure. The guessing path is a worse answer, never a wrong one."""
    from backend.app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archives")
    payload = _multi_plate_3mf(tmp_path / "remote.3mf", {DISPATCHED_PLATE: PLATE_3_GRAMS}).read_bytes()
    missing = DispatchDonor(
        local_path=tmp_path / "library" / "deleted.3mf",
        filename=SPLICED_NAME,
        plate_id=DISPATCHED_PLATE,
        item_id=815,
    )

    patches, lane = _locate_lane(None, {f"/cache/{SPLICED_NAME}": payload})
    for p in patches:
        p.start()
    try:
        lookup = await foreign_archive.locate_3mf_for_print(
            _printer_stub(), SPLICED_SUBTASK, RUNNING_GCODE_FILE, known_donor=missing
        )
    finally:
        for p in reversed(patches):
            p.stop()

    assert lookup.found is True, "the derivation lane must still run when the donor's bytes are gone"
    assert lookup.filename == SPLICED_NAME
    lane.assert_awaited()


@pytest.mark.asyncio
async def test_the_1204_guard_still_rejects_a_stale_named_foreign_print(tmp_path, monkeypatch, caplog):
    """REGRESSION: no donor ⇒ the guard is exactly what it was.

    A foreign print's ``subtask_name`` lags across consecutive plates of the same
    model, so the first candidate lands on the PREVIOUS plate's still-resident
    upload. That file must still be rejected — the whole reason the cross-check
    exists. Layer 1 bypasses it only where the farm has a known donor; here it has
    none, so nothing may change.
    """
    from backend.app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archives")
    wrong_plate_upload = _multi_plate_3mf(tmp_path / "stale.3mf", {1: PLATE_1_GRAMS})

    patches, _lane = _locate_lane(wrong_plate_upload, {})
    with caplog.at_level(logging.WARNING, logger="backend.app.services.foreign_archive"):
        for p in patches:
            p.start()
        try:
            lookup = await foreign_archive.locate_3mf_for_print(
                _printer_stub(), "Bracket v4 - Plate 1", "Metadata/plate_3.gcode"
            )
        finally:
            for p in reversed(patches):
                p.stop()

    assert lookup.found is False, "the stale-name mismatch must still be refused for a foreign print"
    assert lookup.local_path is None
    assert lookup.subtask_name == "Bracket v4 - Plate 3", "the fallback archive is still named for the right plate"
    assert any("plate mismatch" in r.getMessage() for r in caplog.records)
    assert wrong_plate_upload.exists(), "the borrowed cache file must survive the give-up branch"


@pytest.mark.asyncio
async def test_foreign_print_still_archives_through_the_unchanged_lane(tmp_path, monkeypatch):
    """LIVENESS: the ordinary foreign capture — guess the name, fetch it, agree on
    the plate — still works end to end with the new parameter defaulted away."""
    from backend.app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archives")
    payload = _multi_plate_3mf(tmp_path / "bracket.3mf", {1: PLATE_1_GRAMS}).read_bytes()

    patches, lane = _locate_lane(None, {"/cache/Bracket v4.gcode.3mf": payload})
    for p in patches:
        p.start()
    try:
        lookup = await foreign_archive.locate_3mf_for_print(_printer_stub(), "Bracket v4", "Metadata/plate_1.gcode")
    finally:
        for p in reversed(patches):
            p.stop()

    assert lookup.found is True
    assert lookup.filename == "Bracket v4.gcode.3mf"
    assert lookup.expected_plate == 1
    lane.assert_awaited()


# ---------------------------------------------------------------------------
# Layer 1, end to end — the donor is what makes the print chargeable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_donor_resolved_print_charges_the_dispatched_plates_grams(
    db_session, printer_factory, tmp_path, monkeypatch
):
    """LIVENESS + the fix's whole point: a farm print on a tagless feeder charges
    its REAL grams — the dispatched plate's, not the file's first plate's."""
    from backend.app.core.config import settings as app_settings
    from backend.app.services.usage_tracker import _active_sessions, on_print_complete

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archives")
    _active_sessions.clear()

    printer = await printer_factory()
    donor_path = _library_donor(tmp_path)
    spool = await _seed_tagless_spool(db_session, printer.id)
    await _seed_farm_item(db_session, printer.id, "FARM-815")
    archive = await _seed_archive(
        db_session, printer.id, file_path=str(donor_path.relative_to(tmp_path)), print_name=SPLICED_SUBTASK
    )

    notify = AsyncMock()
    with patch("backend.app.services.notification_service.notification_service.on_zero_gram_charge", notify):
        results = await on_print_complete(
            printer_id=printer.id,
            data={"status": "completed", "subtask_id": "FARM-815"},
            printer_manager=_settled_printer_manager(),
            db=db_session,
            archive_id=archive.id,
        )

    await db_session.refresh(spool)
    charged = sum(r["weight_used"] for r in results)
    assert charged == pytest.approx(PLATE_3_GRAMS, abs=0.1), "the DISPATCHED plate's grams, not plate 1's"
    assert spool.weight_used == pytest.approx(PLATE_3_GRAMS, abs=0.1)
    notify.assert_not_awaited(), "a print that charged its grams must never raise the zero-gram page"


@pytest.mark.asyncio
async def test_donor_lookup_through_to_the_charge(db_session, printer_factory, tmp_path, monkeypatch):
    """THE WHOLE CLAIM, chained through real code: the supplied donor resolves, the
    archive gains that 3MF, and the print charges the dispatched plate's grams.

    The two halves are asserted together on purpose — layer 1 is only worth
    anything if the file it rescues is the file the ledger ends up charging from.
    """
    from backend.app.core.config import settings as app_settings
    from backend.app.services.archive import ArchiveService
    from backend.app.services.usage_tracker import _active_sessions, on_print_complete

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archives")
    _active_sessions.clear()

    printer = await printer_factory()
    donor_path = _library_donor(tmp_path)
    spool = await _seed_tagless_spool(db_session, printer.id)
    await _seed_farm_item(db_session, printer.id, "FARM-815")
    archive = await _seed_archive(db_session, printer.id)  # file-less, as on_print_start wrote it

    donor = DispatchDonor(local_path=donor_path, filename=SPLICED_NAME, plate_id=DISPATCHED_PLATE, item_id=815)
    patches, _lane = _locate_lane(None, {})
    for p in patches:
        p.start()
    try:
        lookup = await foreign_archive.locate_3mf_for_print(
            _printer_stub(), SPLICED_SUBTASK, RUNNING_GCODE_FILE, known_donor=donor
        )
    finally:
        for p in reversed(patches):
            p.stop()

    assert lookup.found is True
    attached = await ArchiveService(db_session).attach_3mf_to_archive(archive, lookup.local_path, lookup.expected_plate)
    assert attached is True, "the rescued donor must attach to the archive row"

    notify = AsyncMock()
    with patch("backend.app.services.notification_service.notification_service.on_zero_gram_charge", notify):
        results = await on_print_complete(
            printer_id=printer.id,
            data={"status": "completed", "subtask_id": "FARM-815"},
            printer_manager=_settled_printer_manager(),
            db=db_session,
            archive_id=archive.id,
        )

    await db_session.refresh(spool)
    assert sum(r["weight_used"] for r in results) == pytest.approx(PLATE_3_GRAMS, abs=0.1)
    assert spool.weight_used == pytest.approx(PLATE_3_GRAMS, abs=0.1)
    notify.assert_not_awaited()
    assert donor_path.exists(), "the library file is borrowed for all of this and must survive it"


# ---------------------------------------------------------------------------
# Layer 2 — the silence is made loud (scenario C6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_zero_gram_completion_on_a_tagless_feeder_warns_and_notifies(
    db_session, printer_factory, tmp_path, monkeypatch, caplog
):
    """C6: the incident's own shape — a completed print, a file-less archive, a
    tagless roll in the feeder, and nothing charged. It must say so."""
    from backend.app.core.config import settings as app_settings
    from backend.app.services.usage_tracker import _active_sessions, on_print_complete

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archives")
    _active_sessions.clear()

    printer = await printer_factory(name="002-H2S")
    spool = await _seed_tagless_spool(db_session, printer.id)
    await _seed_farm_item(db_session, printer.id, "FARM-810")
    archive = await _seed_archive(db_session, printer.id)  # no 3MF — the incident

    notify = AsyncMock()
    with (
        patch("backend.app.services.notification_service.notification_service.on_zero_gram_charge", notify),
        caplog.at_level(logging.WARNING, logger="backend.app.services.usage_tracker"),
    ):
        results = await on_print_complete(
            printer_id=printer.id,
            data={"status": "completed", "subtask_id": "FARM-810"},
            printer_manager=_settled_printer_manager(),
            db=db_session,
            archive_id=archive.id,
        )

    assert results == [], "the zero-charge itself is unchanged — this layer reports, it does not invent grams"
    assert any("ZERO-GRAM CHARGE" in r.getMessage() for r in caplog.records), "the WARN is the per-print record"
    notify.assert_awaited_once()
    args = notify.await_args.args
    assert args[0] == printer.id
    assert args[1] == "002-H2S"
    assert args[3] == "AMS A slot 1", "the page names the slot the way every other operator surface does"
    assert args[4] == spool.id
    assert args[5] == "PETG"


@pytest.mark.asyncio
async def test_zero_gram_page_is_deduped_per_printer(db_session, printer_factory, tmp_path, monkeypatch):
    """A bleed of this class repeats on EVERY print. Fifteen identical pages bury
    the fact they report, so the page is one per printer per window — while the WARN
    log stays per print."""
    from backend.app.core.config import settings as app_settings
    from backend.app.services.usage_tracker import _active_sessions, on_print_complete

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archives")
    _active_sessions.clear()

    printer = await printer_factory()
    await _seed_tagless_spool(db_session, printer.id)
    await _seed_farm_item(db_session, printer.id, "FARM-1")

    notify = AsyncMock()
    with patch("backend.app.services.notification_service.notification_service.on_zero_gram_charge", notify):
        for index in range(3):
            archive = await _seed_archive(db_session, printer.id)
            await on_print_complete(
                printer_id=printer.id,
                data={"status": "completed", "subtask_id": "FARM-1"},
                printer_manager=_settled_printer_manager(),
                db=db_session,
                archive_id=archive.id,
            )
            assert notify.await_count == 1, f"page {index + 1} must ride the per-printer dedup window"


@pytest.mark.asyncio
async def test_a_tagged_feeder_charging_zero_does_not_page(db_session, printer_factory, tmp_path, monkeypatch):
    """Scope guard: a TAGGED roll has a second gram source (the AMS remain%-delta),
    so a zero there is not the impossibility this event reports. Paging on it would
    train the operator to ignore the real one."""
    from backend.app.core.config import settings as app_settings
    from backend.app.models.spool import Spool
    from backend.app.models.spool_assignment import SpoolAssignment
    from backend.app.services.usage_tracker import _active_sessions, on_print_complete

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archives")
    _active_sessions.clear()

    printer = await printer_factory()
    spool = Spool(material="PETG", label_weight=1000, weight_used=0.0, tag_uid="C93B67FE00000100")
    db_session.add(spool)
    await db_session.commit()
    await db_session.refresh(spool)
    db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=printer.id, ams_id=0, tray_id=0))
    await db_session.commit()
    await _seed_farm_item(db_session, printer.id, "FARM-2")
    archive = await _seed_archive(db_session, printer.id)

    notify = AsyncMock()
    with patch("backend.app.services.notification_service.notification_service.on_zero_gram_charge", notify):
        await on_print_complete(
            printer_id=printer.id,
            data={"status": "completed", "subtask_id": "FARM-2"},
            printer_manager=_settled_printer_manager(),
            db=db_session,
            archive_id=archive.id,
        )

    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_cancelled_print_charging_zero_does_not_page(db_session, printer_factory, tmp_path, monkeypatch):
    """Only a COMPLETED print makes zero grams impossible. A print that died early
    legitimately charged ~nothing."""
    from backend.app.core.config import settings as app_settings
    from backend.app.services.usage_tracker import _active_sessions, on_print_complete

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archives")
    _active_sessions.clear()

    printer = await printer_factory()
    await _seed_tagless_spool(db_session, printer.id)
    await _seed_farm_item(db_session, printer.id, "FARM-3")
    archive = await _seed_archive(db_session, printer.id)

    notify = AsyncMock()
    with patch("backend.app.services.notification_service.notification_service.on_zero_gram_charge", notify):
        await on_print_complete(
            printer_id=printer.id,
            data={"status": "cancelled"},
            printer_manager=_settled_printer_manager(),
            db=db_session,
            archive_id=archive.id,
        )

    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_motion_only_job_with_no_ams_mapping_does_not_page(db_session, printer_factory, tmp_path, monkeypatch):
    """The false-page this guard is narrowed to avoid.

    An eject sweep and an empty-bed dry-run dispatch with ``use_ams=False`` and
    consume nothing BY DESIGN — but they run with the previous print's roll still in
    the feed path, so ``tray_now`` (and therefore ``last_loaded_tray`` and the
    change log, which is seeded from it) still names that production slot. Deriving
    the feeder from those observations would page after every single eject on every
    printer. The dispatch mapping is a decision, not an observation, and a motion-only
    job decides no AMS slot — so nothing is named and nothing is paged.
    """
    from backend.app.core.config import settings as app_settings
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.services.usage_tracker import _active_sessions, on_print_complete

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archives")
    _active_sessions.clear()

    printer = await printer_factory()
    await _seed_tagless_spool(db_session, printer.id)
    # use_ams=False ⇒ the matcher decided no AMS slot at all.
    item = PrintQueueItem(
        printer_id=printer.id,
        status="printing",
        dispatch_subtask_id="EJECT-9",
        plate_id=1,
        ams_mapping="[-1, -1, -1, -1]",
        is_dry_run=True,
        started_at=datetime.now(timezone.utc) - timedelta(minutes=2),
    )
    db_session.add(item)
    await db_session.commit()
    archive = await _seed_archive(db_session, printer.id)

    # The production roll is STILL LOADED — every observational witness points at it.
    pm = _settled_printer_manager(last_loaded_tray=0)
    pm.get_status.return_value.tray_now = 0
    pm.get_status.return_value.tray_change_log = [(0, 0)]

    notify = AsyncMock()
    with patch("backend.app.services.notification_service.notification_service.on_zero_gram_charge", notify):
        await on_print_complete(
            printer_id=printer.id,
            data={"status": "completed", "subtask_id": "EJECT-9"},
            printer_manager=pm,
            db=db_session,
            archive_id=archive.id,
        )

    notify.assert_not_awaited(), "a motion-only job that consumed nothing by design must never page"


# ---------------------------------------------------------------------------
# The donor resolver itself — one origin, now shared with the eject lane
# ---------------------------------------------------------------------------


async def _seed_library_file(db, path: Path, base_dir: Path, *, filename: str = SPLICED_NAME):
    from backend.app.models.library import LibraryFile

    row = LibraryFile(
        filename=filename,
        file_path=str(path.relative_to(base_dir)),
        file_type="3mf",
        file_size=path.stat().st_size,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@pytest.mark.asyncio
async def test_donor_prefers_the_library_row_and_carries_the_items_plate(
    db_session, printer_factory, tmp_path, monkeypatch
):
    """The dispatched source, present for the whole run's lifetime."""
    from backend.app.core.config import settings as app_settings
    from backend.app.services.farm_correlation import resolve_dispatch_donor

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    printer = await printer_factory()
    donor_path = _library_donor(tmp_path)
    library_row = await _seed_library_file(db_session, donor_path, tmp_path)
    item = await _seed_farm_item(db_session, printer.id, "FARM-815")
    item.library_file_id = library_row.id
    await db_session.commit()

    donor = await resolve_dispatch_donor(db_session, printer.id, "FARM-815")

    assert donor is not None
    assert donor.local_path == donor_path
    assert donor.filename == SPLICED_NAME
    assert donor.plate_id == DISPATCHED_PLATE, "the plate the FARM dispatched, not one parsed from the file"
    assert donor.item_id == item.id


@pytest.mark.asyncio
async def test_donor_falls_back_to_the_archive_copy_when_the_library_bytes_are_gone(
    db_session, printer_factory, tmp_path, monkeypatch
):
    """What remains after a transient Direct-Print library row was reaped."""
    from backend.app.core.config import settings as app_settings
    from backend.app.services.farm_correlation import resolve_dispatch_donor

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    printer = await printer_factory()
    archive_copy = _multi_plate_3mf(tmp_path / "archive" / "1" / "copy.3mf", {DISPATCHED_PLATE: PLATE_3_GRAMS})
    library_row = await _seed_library_file(db_session, archive_copy, tmp_path)
    library_row.file_path = "library/files/deleted.3mf"  # row survives, bytes do not
    archive = await _seed_archive(db_session, printer.id, file_path=str(archive_copy.relative_to(tmp_path)))
    item = await _seed_farm_item(db_session, printer.id, "FARM-816")
    item.library_file_id = library_row.id
    item.archive_id = archive.id
    await db_session.commit()

    donor = await resolve_dispatch_donor(db_session, printer.id, "FARM-816")

    assert donor is not None
    assert donor.local_path == archive_copy


@pytest.mark.asyncio
async def test_a_file_less_archive_row_is_never_offered_as_a_donor(db_session, printer_factory, tmp_path, monkeypatch):
    """``base_dir / ""`` is the base DIRECTORY, and ``exists()`` says yes to it.

    The no-3MF archive row this whole wave is about carries exactly that empty
    ``file_path``, so an ``exists()`` check would hand the eject builder and the
    archive capture a directory to open.
    """
    from backend.app.core.config import settings as app_settings
    from backend.app.services.farm_correlation import resolve_dispatch_donor

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    printer = await printer_factory()
    archive = await _seed_archive(db_session, printer.id, file_path="")
    item = await _seed_farm_item(db_session, printer.id, "FARM-817")
    item.archive_id = archive.id
    await db_session.commit()

    assert await resolve_dispatch_donor(db_session, printer.id, "FARM-817") is None


@pytest.mark.asyncio
async def test_a_foreign_print_has_no_donor(db_session, printer_factory, tmp_path, monkeypatch):
    """LIVENESS for the other half: nothing the farm dispatched ⇒ None ⇒ the
    derivation lane runs, exactly as it always did."""
    from backend.app.core.config import settings as app_settings
    from backend.app.services.farm_correlation import resolve_dispatch_donor

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    printer = await printer_factory()

    assert await resolve_dispatch_donor(db_session, printer.id, "STUDIO-77") is None


@pytest.mark.asyncio
async def test_a_foreign_print_gets_no_donor_even_while_a_farm_unit_is_printing(
    db_session, printer_factory, tmp_path, monkeypatch
):
    """The mis-attribution window this lane must not open.

    A screen-started print can begin on a printer whose farm unit has not gone
    terminal yet. ``resolve_printing_item``'s sole-item fallback would hand that
    foreign print the farm's file — recording somebody else's job as having run our
    3MF. The echoed id decides: it is not the one Bambuddy minted, so there is no
    donor and the derivation lane handles it exactly as before.
    """
    from backend.app.core.config import settings as app_settings
    from backend.app.services.farm_correlation import resolve_dispatch_donor

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    printer = await printer_factory()
    donor_path = _library_donor(tmp_path)
    library_row = await _seed_library_file(db_session, donor_path, tmp_path)
    item = await _seed_farm_item(db_session, printer.id, "FARM-815")
    item.library_file_id = library_row.id
    await db_session.commit()

    # Same printer, same instant, a different job.
    assert await resolve_dispatch_donor(db_session, printer.id, "409226156") is None
    # And a degenerate echo (screen RESTART) is refused rather than assumed.
    assert await resolve_dispatch_donor(db_session, printer.id, "") is None
    assert await resolve_dispatch_donor(db_session, printer.id, None) is None
    # Liveness: the farm's own dispatch still resolves.
    assert await resolve_dispatch_donor(db_session, printer.id, "FARM-815") is not None
