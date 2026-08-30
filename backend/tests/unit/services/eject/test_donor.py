"""The eject donor chain — tier order, tier rules, and the two compositions.

Donor resolution used to be two hand-rolled resolvers inside ``eject/manual.py`` chained
by an ``except``. It is now a Chain of Responsibility with the compositions declared once,
and these tests pin the three things that made the extraction worth doing:

* the ORDER (a stronger tier's answer is never displaced by a weaker one),
* the AUTO chain's fail-closed membership (an unattended sweep may only use the tier that
  positively identifies what is on the bed), and
* that the gate id comes from the occupancy AUTHORITY, never from the printer row's
  ``plate_gate_subtask_id`` column — which since the 2026-08-30 cut-over is write-only
  persistence and can hold a dead key after a failed persist.
"""

import os
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services.eject import donor as donor_mod
from backend.app.services.eject.donor import (
    AUTO_DONOR_CHAIN,
    MANUAL_DONOR_CHAIN,
    ContainerLibraryFile,
    DonorContext,
    GateSubtaskArchive,
    LastFarmItemFile,
    resolve_donor,
)

pytestmark = pytest.mark.asyncio


_PLATE_GCODE = (
    "; HEADER_BLOCK_START\n"
    "; max_z_height: 18.00\n"
    "; HEADER_BLOCK_END\n"
    "; EXECUTABLE_BLOCK_START\n"
    "G1 X10 Y10\n"
    "; EXECUTABLE_BLOCK_END\n"
)


def _make_3mf(*, plates=(1,)) -> Path:
    """A ``.gcode.3mf`` carrying a G-code member for each id in ``plates``."""
    fd, name = tempfile.mkstemp(suffix=".gcode.3mf")
    os.close(fd)
    path = Path(name)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for plate in plates:
            zf.writestr(f"Metadata/plate_{plate}.gcode", _PLATE_GCODE)
        zf.writestr("3D/3dmodel.model", "<model/>")
    return path


def _make_bare_3mf() -> Path:
    """A ``.gcode.3mf`` with NO G-code plate — a container that cannot be repacked."""
    fd, name = tempfile.mkstemp(suffix=".gcode.3mf")
    os.close(fd)
    path = Path(name)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")
    return path


async def _mk_printer(db, name="DNR", model="H2S", row_gate="ROW-KEY"):
    """A printer whose ROW carries ``row_gate`` — deliberately a different value from the
    authority's plate source in most tests, so a tier reading the column would fail."""
    p = Printer(
        name=name,
        serial_number=f"S{name}",
        ip_address="1.2.3.4",
        access_code="x",
        model=model,
        plate_gate_subtask_id=row_gate,
    )
    db.add(p)
    await db.flush()
    return p


async def _mk_archive(db, *, printer_id, subtask, file_path, filename="foreign.gcode.3mf", print_name="Foreign Widget"):
    arch = PrintArchive(
        printer_id=printer_id,
        filename=filename,
        file_path=file_path,
        file_size=123,
        subtask_id=subtask,
        print_name=print_name,
        status="completed",
    )
    db.add(arch)
    await db.flush()
    return arch


async def _mk_library(db, *, filename, file_path, sliced_for_model=None, deleted=False):
    lf = LibraryFile(
        filename=filename,
        file_path=file_path,
        file_type="3mf",
        file_size=1,
        file_metadata={"sliced_for_model": sliced_for_model} if sliced_for_model is not None else None,
        deleted_at=datetime.now(timezone.utc) if deleted else None,
    )
    db.add(lf)
    await db.flush()
    return lf


async def _mk_item(db, *, printer_id, library_file_id=None, archive_id=None, plate_id=1, started_at=None):
    item = PrintQueueItem(
        printer_id=printer_id,
        status="cancelled",
        first_article=False,
        library_file_id=library_file_id,
        archive_id=archive_id,
        plate_id=plate_id,
        started_at=started_at or datetime.now(timezone.utc),
        position=1,
    )
    db.add(item)
    await db.flush()
    return item


@pytest.fixture(autouse=True)
def _clear_donor_cache():
    """Isolate the module-level FTPS re-fetch cache between tests (unlink temp files)."""
    donor_mod._donor_cache.clear()
    yield
    for _key, (path, _exp) in list(donor_mod._donor_cache.items()):
        path.unlink(missing_ok=True)
    donor_mod._donor_cache.clear()


class TestGateSubtaskArchive:
    """Tier 1 — the archive of the print that raised the gate. The only tier that
    positively identifies what is on the bed."""

    async def test_resolves_the_on_disk_archive_for_the_gate(self, db_session):
        source = _make_3mf()
        try:
            printer = await _mk_printer(db_session, "T1OK")
            await _mk_archive(db_session, printer_id=printer.id, subtask="SUB-F", file_path=str(source))
            await db_session.commit()
            ctx = DonorContext(db=db_session, printer=printer, plate_source="SUB-F", item=None)

            result = await GateSubtaskArchive.resolve(ctx)

            assert result is not None
            assert result.path == source
            assert result.plate_id == 1
            assert result.max_z == 18.0
            assert result.print_name == "Foreign Widget"
            assert result.tmp_path is None
        finally:
            source.unlink(missing_ok=True)

    async def test_gate_id_comes_from_plate_source_not_the_printer_row(self, db_session):
        """THE STALE-KEY PIN. ``Printer.plate_gate_subtask_id`` is write-only persistence
        since the 2026-08-30 cut-over — a failed persist leaves it holding a dead key. A
        tier steering off that key would build a sweep for a print that is NOT on this
        plate, which is the exact staleness ``manual.py`` used to patch by NULLing the
        column mid-flow. Here the row says "ROW-KEY" and only the row's archive exists;
        the authority says nothing, and the tier must decline."""
        source = _make_3mf()
        try:
            printer = await _mk_printer(db_session, "T1STALE", row_gate="ROW-KEY")
            await _mk_archive(db_session, printer_id=printer.id, subtask="ROW-KEY", file_path=str(source))
            await db_session.commit()

            # The authority's plate carries NO source id.
            assert (
                await GateSubtaskArchive.resolve(
                    DonorContext(db=db_session, printer=printer, plate_source=None, item=None)
                )
                is None
            )
            # A DIFFERENT live gate must not serve the row key's archive either.
            assert (
                await GateSubtaskArchive.resolve(
                    DonorContext(db=db_session, printer=printer, plate_source="LIVE-KEY", item=None)
                )
                is None
            )
            # And the column is untouched — nothing in the donor lane writes it.
            assert printer.plate_gate_subtask_id == "ROW-KEY"
        finally:
            source.unlink(missing_ok=True)

    async def test_other_printers_archive_is_not_a_donor(self, db_session):
        source = _make_3mf()
        try:
            mine = await _mk_printer(db_session, "T1MINE")
            theirs = await _mk_printer(db_session, "T1THEIRS")
            await _mk_archive(db_session, printer_id=theirs.id, subtask="SUB-F", file_path=str(source))
            await db_session.commit()

            result = await GateSubtaskArchive.resolve(
                DonorContext(db=db_session, printer=mine, plate_source="SUB-F", item=None)
            )
            assert result is None
        finally:
            source.unlink(missing_ok=True)

    async def test_unfetchable_fallback_archive_declines(self, db_session):
        # A download-failed archive row carries file_path="" → FTPS re-fetch → unfetchable.
        printer = await _mk_printer(db_session, "T1FF")
        await _mk_archive(db_session, printer_id=printer.id, subtask="SUB-F", file_path="", filename="gone.gcode.3mf")
        await db_session.commit()
        with patch("backend.app.services.bambu_ftp.download_file_try_paths_async", AsyncMock(return_value=False)):
            result = await GateSubtaskArchive.resolve(
                DonorContext(db=db_session, printer=printer, plate_source="SUB-F", item=None)
            )
        assert result is None


class TestLastFarmItemFile:
    """Tier 2 — the caller's known unit, else the printer's last-started farm unit. An
    ASSUMED identity, which is why it is MANUAL-only."""

    async def test_uses_the_context_item_when_the_caller_supplies_one(self, db_session):
        wanted = _make_3mf()
        other = _make_3mf()
        try:
            printer = await _mk_printer(db_session, "T2CTX")
            lf_wanted = await _mk_library(db_session, filename="Wanted.gcode.3mf", file_path=str(wanted))
            lf_other = await _mk_library(db_session, filename="Newer.gcode.3mf", file_path=str(other))
            item = await _mk_item(
                db_session,
                printer_id=printer.id,
                library_file_id=lf_wanted.id,
                started_at=datetime.now(timezone.utc) - timedelta(hours=1),
            )
            # A NEWER start exists; supplying the item must beat "the printer's last start".
            await _mk_item(db_session, printer_id=printer.id, library_file_id=lf_other.id)
            await db_session.commit()

            result = await LastFarmItemFile.resolve(
                DonorContext(db=db_session, printer=printer, plate_source=None, item=item)
            )
            assert result is not None
            assert result.path == wanted
            assert result.print_name == "Wanted.gcode.3mf"
        finally:
            wanted.unlink(missing_ok=True)
            other.unlink(missing_ok=True)

    async def test_falls_back_to_the_printers_last_start(self, db_session):
        source = _make_3mf()
        try:
            printer = await _mk_printer(db_session, "T2LAST")
            lf = await _mk_library(db_session, filename="Last.gcode.3mf", file_path=str(source))
            await _mk_item(db_session, printer_id=printer.id, library_file_id=lf.id)
            await db_session.commit()

            result = await LastFarmItemFile.resolve(
                DonorContext(db=db_session, printer=printer, plate_source=None, item=None)
            )
            assert result is not None
            assert result.path == source
            assert result.max_z == 18.0
        finally:
            source.unlink(missing_ok=True)

    async def test_declines_when_the_printer_never_started_anything(self, db_session):
        printer = await _mk_printer(db_session, "T2NONE")
        await db_session.commit()
        assert (
            await LastFarmItemFile.resolve(DonorContext(db=db_session, printer=printer, plate_source=None, item=None))
            is None
        )


class TestContainerLibraryFile:
    """Tier 3 — a valid ZIP skeleton and nothing more. It identifies NOTHING, which is
    why it reports ``max_z=None`` and ``print_name=None`` and the manual lane refuses to
    confirm without the operator's part height."""

    async def test_newest_matching_slice_wins_and_carries_no_identity(self, db_session):
        older = _make_3mf()
        newer = _make_3mf()
        try:
            printer = await _mk_printer(db_session, "T3NEW")
            await _mk_library(db_session, filename="Older.gcode.3mf", file_path=str(older), sliced_for_model="H2S")
            await _mk_library(db_session, filename="Newer.gcode.3mf", file_path=str(newer), sliced_for_model="H2S")
            await db_session.commit()

            result = await ContainerLibraryFile.resolve(
                DonorContext(db=db_session, printer=printer, plate_source=None, item=None)
            )
            assert result is not None
            assert result.path == newer  # newest row first
            assert result.plate_id == 1
            assert result.max_z is None  # a container knows no part height
            assert result.print_name is None  # nor what is on the plate
        finally:
            older.unlink(missing_ok=True)
            newer.unlink(missing_ok=True)

    async def test_model_mismatched_slice_is_skipped(self, db_session):
        wrong = _make_3mf()
        right = _make_3mf()
        try:
            printer = await _mk_printer(db_session, "T3MODEL", model="H2S")
            # The H2C slice is NEWER, so only the model gate can keep it out.
            await _mk_library(db_session, filename="Right.gcode.3mf", file_path=str(right), sliced_for_model="H2S")
            await _mk_library(db_session, filename="Wrong.gcode.3mf", file_path=str(wrong), sliced_for_model="H2C")
            await db_session.commit()

            result = await ContainerLibraryFile.resolve(
                DonorContext(db=db_session, printer=printer, plate_source=None, item=None)
            )
            assert result is not None
            assert result.path == right
        finally:
            wrong.unlink(missing_ok=True)
            right.unlink(missing_ok=True)

    async def test_model_match_is_canonicalised(self, db_session):
        """A slicer writes the internal model id (``O1S``); the printer row carries the
        display name (``H2S``). One canonical key, or every H2S container is invisible."""
        source = _make_3mf()
        try:
            printer = await _mk_printer(db_session, "T3CANON", model="H2S")
            await _mk_library(db_session, filename="Sliced.gcode.3mf", file_path=str(source), sliced_for_model="O1S")
            await db_session.commit()

            result = await ContainerLibraryFile.resolve(
                DonorContext(db=db_session, printer=printer, plate_source=None, item=None)
            )
            assert result is not None
            assert result.path == source
        finally:
            source.unlink(missing_ok=True)

    async def test_unsliced_and_deleted_and_missing_and_plateless_rows_are_skipped(self, db_session):
        plain = _make_3mf()
        trashed = _make_3mf()
        bare = _make_bare_3mf()
        good = _make_3mf()
        try:
            printer = await _mk_printer(db_session, "T3SKIP")
            # (a) a plain .3mf project — never a repack container (filename gate).
            await _mk_library(db_session, filename="Project.3mf", file_path=str(plain), sliced_for_model="H2S")
            # (b) no metadata at all → no model claim.
            await _mk_library(db_session, filename="Unknown.gcode.3mf", file_path=str(plain))
            # (c) trashed.
            await _mk_library(
                db_session, filename="Trashed.gcode.3mf", file_path=str(trashed), sliced_for_model="H2S", deleted=True
            )
            # (d) row exists, bytes do not.
            await _mk_library(
                db_session, filename="Gone.gcode.3mf", file_path="/nonexistent/Gone.gcode.3mf", sliced_for_model="H2S"
            )
            # (e) on disk but carries no G-code plate — nothing to repack.
            await _mk_library(db_session, filename="Bare.gcode.3mf", file_path=str(bare), sliced_for_model="H2S")
            await db_session.commit()

            assert (
                await ContainerLibraryFile.resolve(
                    DonorContext(db=db_session, printer=printer, plate_source=None, item=None)
                )
                is None
            )

            # And with one good row present, THAT is the answer.
            await _mk_library(db_session, filename="Good.gcode.3mf", file_path=str(good), sliced_for_model="H2S")
            await db_session.commit()
            result = await ContainerLibraryFile.resolve(
                DonorContext(db=db_session, printer=printer, plate_source=None, item=None)
            )
            assert result is not None
            assert result.path == good
        finally:
            for p in (plain, trashed, bare, good):
                p.unlink(missing_ok=True)


class TestChainComposition:
    """The two compositions and the order they impose."""

    async def test_manual_chain_prefers_the_strict_tier(self, db_session):
        archive_src = _make_3mf()
        item_src = _make_3mf()
        container_src = _make_3mf()
        try:
            printer = await _mk_printer(db_session, "CHSTRICT")
            await _mk_archive(
                db_session,
                printer_id=printer.id,
                subtask="SUB-F",
                file_path=str(archive_src),
                print_name="Gate Print",
            )
            lf = await _mk_library(db_session, filename="Item.gcode.3mf", file_path=str(item_src))
            await _mk_item(db_session, printer_id=printer.id, library_file_id=lf.id)
            await _mk_library(
                db_session, filename="Container.gcode.3mf", file_path=str(container_src), sliced_for_model="H2S"
            )
            await db_session.commit()

            result = await resolve_donor(
                MANUAL_DONOR_CHAIN, DonorContext(db=db_session, printer=printer, plate_source="SUB-F", item=None)
            )
            assert result is not None
            assert result.path == archive_src
            assert result.print_name == "Gate Print"
        finally:
            for p in (archive_src, item_src, container_src):
                p.unlink(missing_ok=True)

    async def test_manual_chain_falls_to_the_item_tier_when_the_strict_tier_declines(self, db_session):
        item_src = _make_3mf()
        container_src = _make_3mf()
        try:
            printer = await _mk_printer(db_session, "CHITEM")
            lf = await _mk_library(db_session, filename="Item.gcode.3mf", file_path=str(item_src))
            await _mk_item(db_session, printer_id=printer.id, library_file_id=lf.id)
            await _mk_library(
                db_session, filename="Container.gcode.3mf", file_path=str(container_src), sliced_for_model="H2S"
            )
            await db_session.commit()

            # No gate source at all → tier 1 declines; the item tier answers, and its
            # answer carries a real height (which is what keeps the container out).
            result = await resolve_donor(
                MANUAL_DONOR_CHAIN, DonorContext(db=db_session, printer=printer, plate_source=None, item=None)
            )
            assert result is not None
            assert result.path == item_src
            assert result.max_z == 18.0
        finally:
            item_src.unlink(missing_ok=True)
            container_src.unlink(missing_ok=True)

    async def test_manual_chain_falls_to_the_container_tier_last(self, db_session):
        container_src = _make_3mf()
        try:
            printer = await _mk_printer(db_session, "CHCONT")
            await _mk_library(
                db_session, filename="Container.gcode.3mf", file_path=str(container_src), sliced_for_model="H2S"
            )
            await db_session.commit()

            result = await resolve_donor(
                MANUAL_DONOR_CHAIN, DonorContext(db=db_session, printer=printer, plate_source=None, item=None)
            )
            assert result is not None
            assert result.path == container_src
            assert result.max_z is None
        finally:
            container_src.unlink(missing_ok=True)

    async def test_auto_chain_is_fail_closed_where_the_manual_chain_falls_back(self, db_session):
        """THE RED-LINE PIN. The automatic foreign eject sweeps with nobody watching, so
        an ASSUMED (last-farm-item) or ANONYMOUS (container) donor must never reach it.
        Same state, two chains, two answers."""
        item_src = _make_3mf()
        container_src = _make_3mf()
        try:
            printer = await _mk_printer(db_session, "CHAUTO")
            lf = await _mk_library(db_session, filename="Item.gcode.3mf", file_path=str(item_src))
            await _mk_item(db_session, printer_id=printer.id, library_file_id=lf.id)
            await _mk_library(
                db_session, filename="Container.gcode.3mf", file_path=str(container_src), sliced_for_model="H2S"
            )
            await db_session.commit()
            ctx = DonorContext(db=db_session, printer=printer, plate_source=None, item=None)

            assert await resolve_donor(AUTO_DONOR_CHAIN, ctx) is None
            assert await resolve_donor(MANUAL_DONOR_CHAIN, ctx) is not None
        finally:
            item_src.unlink(missing_ok=True)
            container_src.unlink(missing_ok=True)

    async def test_the_compositions_are_declared_once_and_stay_ordered(self):
        # Membership IS the safety property here; a tier appended to AUTO_DONOR_CHAIN
        # would silently widen what an unattended sweep may build from.
        assert list(AUTO_DONOR_CHAIN) == [GateSubtaskArchive]
        assert list(MANUAL_DONOR_CHAIN) == [GateSubtaskArchive, LastFarmItemFile, ContainerLibraryFile]
