"""Manual "Eject now" service (W2) — ordered preconditions + execution paths."""

import asyncio
import os
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.models.archive import PrintArchive
from backend.app.models.eject_profile import EjectProfile
from backend.app.models.library import LibraryFile
from backend.app.models.print_batch import PrintBatch
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services.eject import manual, remote as eject_remote
from backend.app.services.eject.monitor import _ActiveWatch

pytestmark = pytest.mark.asyncio


async def _mk_printer(db, name="MPE", gate="SUB-1", model="H2S"):
    p = Printer(
        name=name,
        serial_number=f"S{name}",
        ip_address="1.2.3.4",
        access_code="x",
        model=model,
        plate_gate_subtask_id=gate,
    )
    db.add(p)
    await db.flush()
    return p


async def _mk_item(db, *, printer_id, dispatch_subtask="SUB-1", first_article=False, eject_profile_id=42):
    item = PrintQueueItem(
        printer_id=printer_id,
        status="completed",
        eject_profile_id=eject_profile_id,
        first_article=first_article,
        dispatch_subtask_id=dispatch_subtask,
        started_at=datetime.now(timezone.utc),
        plate_id=1,
        position=1,
    )
    db.add(item)
    await db.flush()
    return item


def _state(state="FINISH", *, bed=25.0, connected=True):
    return SimpleNamespace(state=state, connected=connected, temperatures={"bed": bed})


def _prod_identity(qid):
    return _ActiveWatch(threshold_c=30.0, queue_item_id=qid, purpose="production", release_now=asyncio.Event())


def _connected_awaiting(status):
    """The three printer_manager patches that pass the connect/busy/gate gates."""
    return (
        patch.object(manual.printer_manager, "is_connected", return_value=True),
        patch.object(manual.printer_manager, "get_status", return_value=status),
        patch.object(manual.printer_manager, "is_awaiting_plate_clear", return_value=True),
    )


class TestManualEjectPreconditions:
    async def test_unknown_printer_404(self, db_session):
        with pytest.raises(manual.ManualEjectError) as exc:
            await manual.manual_eject(db_session, 999999)
        assert exc.value.status_code == 404
        assert exc.value.code == "not_found"

    async def test_not_connected_409(self, db_session):
        printer = await _mk_printer(db_session, "NC")
        await db_session.commit()
        with (
            patch.object(manual.printer_manager, "is_connected", return_value=False),
            pytest.raises(manual.ManualEjectError) as exc,
        ):
            await manual.manual_eject(db_session, printer.id)
        assert exc.value.code == "not_connected"

    async def test_busy_409(self, db_session):
        printer = await _mk_printer(db_session, "BUSY")
        await db_session.commit()
        with (
            patch.object(manual.printer_manager, "is_connected", return_value=True),
            patch.object(manual.printer_manager, "get_status", return_value=_state("RUNNING")),
            pytest.raises(manual.ManualEjectError) as exc,
        ):
            await manual.manual_eject(db_session, printer.id)
        assert exc.value.code == "printer_busy"

    async def test_no_plate_gate_409(self, db_session):
        printer = await _mk_printer(db_session, "NG")
        await db_session.commit()
        with (
            patch.object(manual.printer_manager, "is_connected", return_value=True),
            patch.object(manual.printer_manager, "get_status", return_value=_state("FINISH")),
            patch.object(manual.printer_manager, "is_awaiting_plate_clear", return_value=False),
            pytest.raises(manual.ManualEjectError) as exc,
        ):
            await manual.manual_eject(db_session, printer.id)
        assert exc.value.code == "no_plate_gate"

    async def test_eject_in_flight_409(self, db_session):
        printer = await _mk_printer(db_session, "INF")
        await db_session.commit()
        eject_remote.register_pending_eject(printer.id, eject_remote.PendingEject("production", None, 5))
        c1, c2, c3 = _connected_awaiting(_state("FINISH"))
        try:
            with c1, c2, c3, pytest.raises(manual.ManualEjectError) as exc:
                await manual.manual_eject(db_session, printer.id)
            assert exc.value.code == "eject_in_flight"
        finally:
            eject_remote.pop_pending_eject(printer.id)

    async def test_no_eligible_unit_409(self, db_session):
        # No armed watch and no DB-resolvable unit → no_eligible_unit.
        printer = await _mk_printer(db_session, "NE")
        await db_session.commit()
        c1, c2, c3 = _connected_awaiting(_state("FINISH"))
        with (
            c1,
            c2,
            c3,
            patch.object(manual.eject_cooldown_monitor, "active_watch_identity", return_value=None),
            pytest.raises(manual.ManualEjectError) as exc,
        ):
            await manual.manual_eject(db_session, printer.id)
        assert exc.value.code == "no_eligible_unit"

    async def test_first_article_refused(self, db_session):
        # A completed FA unit matching the gate is deliberately NOT eligible.
        printer = await _mk_printer(db_session, "FA", gate="SUB-1")
        await _mk_item(db_session, printer_id=printer.id, dispatch_subtask="SUB-1", first_article=True)
        await db_session.commit()
        c1, c2, c3 = _connected_awaiting(_state("FINISH"))
        with (
            c1,
            c2,
            c3,
            patch.object(manual.eject_cooldown_monitor, "active_watch_identity", return_value=None),
            pytest.raises(manual.ManualEjectError) as exc,
        ):
            await manual.manual_eject(db_session, printer.id)
        assert exc.value.code == "no_eligible_unit"


class TestManualEjectThermal:
    async def test_bed_hot_carries_temps(self, db_session):
        printer = await _mk_printer(db_session, "HOT")
        await db_session.commit()
        c1, c2, c3 = _connected_awaiting(_state("FINISH", bed=50.0))
        with (
            c1,
            c2,
            c3,
            patch.object(manual.eject_cooldown_monitor, "active_watch_identity", return_value=_prod_identity(555)),
            patch.object(manual, "_resolve_eject_threshold", AsyncMock(return_value=30.0)),
            pytest.raises(manual.BedTooHot) as exc,
        ):
            await manual.manual_eject(db_session, printer.id, allow_hot=False)
        assert exc.value.code == "bed_hot"
        assert exc.value.bed_c == 50.0
        assert exc.value.threshold_c == 30.0

    async def test_allow_hot_bypasses_thermal(self, db_session):
        printer = await _mk_printer(db_session, "HOTOK")
        await db_session.commit()
        c1, c2, c3 = _connected_awaiting(_state("FINISH", bed=50.0))
        with (
            c1,
            c2,
            c3,
            patch.object(manual.eject_cooldown_monitor, "active_watch_identity", return_value=_prod_identity(555)),
            patch.object(manual.eject_cooldown_monitor, "request_release_now", return_value=True),
            patch.object(manual, "_resolve_eject_threshold", AsyncMock(return_value=30.0)),
        ):
            result = await manual.manual_eject(db_session, printer.id, allow_hot=True)
        assert result["mode"] == "released_watch"

    async def test_bed_unreadable_is_retryable_409_not_bed_hot(self, db_session):
        # Connected but no live bed reading (post-reconnect telemetry window):
        # a bed_unreadable 409, NEVER BedTooHot — the confirm dialog must not be
        # built on a missing reading (frontend would render Number(null) → "0°C").
        printer = await _mk_printer(db_session, "NOBED")
        await db_session.commit()
        c1, c2, c3 = _connected_awaiting(_state("FINISH", bed=None))
        with (
            c1,
            c2,
            c3,
            patch.object(manual.eject_cooldown_monitor, "active_watch_identity", return_value=_prod_identity(555)),
            patch.object(manual, "_resolve_eject_threshold", AsyncMock(return_value=30.0)),
            pytest.raises(manual.ManualEjectError) as exc,
        ):
            await manual.manual_eject(db_session, printer.id, allow_hot=False)
        assert exc.value.code == "bed_unreadable"
        assert exc.value.status_code == 409
        assert not isinstance(exc.value, manual.BedTooHot)

    async def test_bed_unreadable_allow_hot_proceeds(self, db_session):
        # The explicit override still proceeds with no reading — unchanged behavior.
        printer = await _mk_printer(db_session, "NOBEDOK")
        await db_session.commit()
        c1, c2, c3 = _connected_awaiting(_state("FINISH", bed=None))
        with (
            c1,
            c2,
            c3,
            patch.object(manual.eject_cooldown_monitor, "active_watch_identity", return_value=_prod_identity(555)),
            patch.object(manual.eject_cooldown_monitor, "request_release_now", return_value=True),
            patch.object(manual, "_resolve_eject_threshold", AsyncMock(return_value=30.0)),
        ):
            result = await manual.manual_eject(db_session, printer.id, allow_hot=True)
        assert result["mode"] == "released_watch"


class TestManualEjectExecution:
    async def test_watch_armed_signals_release_only(self, db_session):
        printer = await _mk_printer(db_session, "WARM")
        await db_session.commit()
        dispatch = AsyncMock()
        c1, c2, c3 = _connected_awaiting(_state("FINISH", bed=25.0))
        with (
            c1,
            c2,
            c3,
            patch.object(manual.eject_cooldown_monitor, "active_watch_identity", return_value=_prod_identity(555)),
            patch.object(manual.eject_cooldown_monitor, "request_release_now", return_value=True) as req,
            patch.object(manual, "_resolve_eject_threshold", AsyncMock(return_value=30.0)),
            patch.object(manual.eject_remote, "dispatch_part_present_eject", dispatch),
        ):
            result = await manual.manual_eject(db_session, printer.id)
        assert result == {"mode": "released_watch", "queue_item_id": 555}
        req.assert_called_once_with(printer.id)
        dispatch.assert_not_called()  # NO parallel dispatch

    async def test_no_watch_dispatches(self, db_session):
        printer = await _mk_printer(db_session, "DISP", gate="SUB-1")
        item = await _mk_item(db_session, printer_id=printer.id, dispatch_subtask="SUB-1")
        await db_session.commit()
        dispatch = AsyncMock()
        c1, c2, c3 = _connected_awaiting(_state("FINISH", bed=25.0))
        with (
            c1,
            c2,
            c3,
            patch.object(manual.eject_cooldown_monitor, "active_watch_identity", return_value=None),
            patch.object(manual, "_resolve_eject_threshold", AsyncMock(return_value=30.0)),
            patch.object(manual.eject_remote, "dispatch_part_present_eject", dispatch),
        ):
            result = await manual.manual_eject(db_session, printer.id)
        assert result == {"mode": "dispatched", "queue_item_id": item.id}
        dispatch.assert_awaited_once()
        assert dispatch.await_args.kwargs["queue_item_id"] == item.id
        assert dispatch.await_args.kwargs["purpose"] == "production"


_FOREIGN_PLATE_GCODE = (
    "; HEADER_BLOCK_START\n"
    "; max_z_height: 18.00\n"
    "; HEADER_BLOCK_END\n"
    "; EXECUTABLE_BLOCK_START\n"
    "G1 X10 Y10\n"
    "; EXECUTABLE_BLOCK_END\n"
)


def _make_source_3mf() -> Path:
    fd, name = tempfile.mkstemp(suffix=".gcode.3mf")
    os.close(fd)
    path = Path(name)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Metadata/plate_1.gcode", _FOREIGN_PLATE_GCODE)
        zf.writestr("3D/3dmodel.model", "<model/>")
    return path


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


class TestManualEjectForeignPlate:
    """The two-step foreign-plate flow: a gate raised by a non-farm print resolves the
    donor from the archive → confirm prompt → dispatch on the second call. A farm-known-
    but-ineligible gate is never treated as foreign."""

    async def test_foreign_plate_raises_confirm_payload(self, db_session):
        source = _make_source_3mf()
        try:
            printer = await _mk_printer(db_session, "FGN", gate="SUB-F")
            # A prior eject-profiled unit (NOT gate-matching → stays foreign) seeds the
            # profile suggestion.
            await _mk_item(db_session, printer_id=printer.id, dispatch_subtask="OTHER", eject_profile_id=77)
            await _mk_archive(db_session, printer_id=printer.id, subtask="SUB-F", file_path=str(source))
            await db_session.commit()
            c1, c2, c3 = _connected_awaiting(_state("FINISH", bed=25.0))
            with (
                c1,
                c2,
                c3,
                patch.object(manual.eject_cooldown_monitor, "active_watch_identity", return_value=None),
                pytest.raises(manual.ForeignPlateEject) as exc,
            ):
                await manual.manual_eject(db_session, printer.id)
            assert exc.value.code == "foreign_plate"
            assert exc.value.status_code == 409
            assert exc.value.print_name == "Foreign Widget"
            assert exc.value.max_z_height_mm == 18.0
            assert exc.value.suggested_eject_profile_id == 77
        finally:
            source.unlink(missing_ok=True)

    async def test_confirm_with_profile_dispatches_foreign(self, db_session):
        source = _make_source_3mf()
        try:
            printer = await _mk_printer(db_session, "FGC", gate="SUB-F")
            prof = EjectProfile(name="fgc-ep")
            db_session.add(prof)
            await db_session.flush()
            await _mk_archive(db_session, printer_id=printer.id, subtask="SUB-F", file_path=str(source))
            await db_session.commit()
            dispatch = AsyncMock()
            c1, c2, c3 = _connected_awaiting(_state("FINISH", bed=25.0))
            with (
                c1,
                c2,
                c3,
                patch.object(manual.eject_cooldown_monitor, "active_watch_identity", return_value=None),
                patch.object(manual.eject_remote, "dispatch_foreign_eject", dispatch),
            ):
                result = await manual.manual_eject(db_session, printer.id, eject_profile_id=prof.id)
            assert result == {"mode": "dispatched", "queue_item_id": None}
            dispatch.assert_awaited_once()
            assert dispatch.await_args.kwargs["printer_id"] == printer.id
            assert dispatch.await_args.kwargs["profile_id"] == prof.id
            assert dispatch.await_args.kwargs["plate_id"] == 1
        finally:
            source.unlink(missing_ok=True)

    async def test_confirm_hot_bed_uses_profile_cooldown(self, db_session):
        source = _make_source_3mf()
        try:
            printer = await _mk_printer(db_session, "FGH", gate="SUB-F")
            prof = EjectProfile(name="fgh-ep", cooldown_temp_c=30.0)
            db_session.add(prof)
            await db_session.flush()
            await _mk_archive(db_session, printer_id=printer.id, subtask="SUB-F", file_path=str(source))
            await db_session.commit()
            c1, c2, c3 = _connected_awaiting(_state("FINISH", bed=45.0))
            with (
                c1,
                c2,
                c3,
                patch.object(manual.eject_cooldown_monitor, "active_watch_identity", return_value=None),
                pytest.raises(manual.BedTooHot) as exc,
            ):
                await manual.manual_eject(db_session, printer.id, eject_profile_id=prof.id)
            assert exc.value.threshold_c == 30.0
            assert exc.value.bed_c == 45.0
        finally:
            source.unlink(missing_ok=True)

    async def test_null_gate_refuses_not_foreign(self, db_session):
        printer = await _mk_printer(db_session, "FGN0", gate=None)
        await db_session.commit()
        c1, c2, c3 = _connected_awaiting(_state("FINISH", bed=25.0))
        with (
            c1,
            c2,
            c3,
            patch.object(manual.eject_cooldown_monitor, "active_watch_identity", return_value=None),
            pytest.raises(manual.ManualEjectError) as exc,
        ):
            await manual.manual_eject(db_session, printer.id)
        assert exc.value.code == "no_eligible_unit"
        assert not isinstance(exc.value, manual.ForeignPlateEject)

    async def test_no_archive_refuses(self, db_session):
        printer = await _mk_printer(db_session, "FGNA", gate="SUB-F")
        await db_session.commit()
        c1, c2, c3 = _connected_awaiting(_state("FINISH", bed=25.0))
        with (
            c1,
            c2,
            c3,
            patch.object(manual.eject_cooldown_monitor, "active_watch_identity", return_value=None),
            pytest.raises(manual.ManualEjectError) as exc,
        ):
            await manual.manual_eject(db_session, printer.id)
        assert exc.value.code == "no_eligible_unit"

    async def test_archive_file_missing_and_fetch_fails_refuses(self, db_session):
        # Fallback archive row (file_path="") → FTPS re-fetch attempted; unfetchable →
        # the actionable by-hand-clear 409.
        printer = await _mk_printer(db_session, "FGFF", gate="SUB-F")
        await _mk_archive(db_session, printer_id=printer.id, subtask="SUB-F", file_path="", filename="gone.gcode.3mf")
        await db_session.commit()
        c1, c2, c3 = _connected_awaiting(_state("FINISH", bed=25.0))
        with (
            c1,
            c2,
            c3,
            patch.object(manual.eject_cooldown_monitor, "active_watch_identity", return_value=None),
            patch(
                "backend.app.services.bambu_ftp.download_file_try_paths_async",
                AsyncMock(return_value=False),
            ),
            pytest.raises(manual.ManualEjectError) as exc,
        ):
            await manual.manual_eject(db_session, printer.id)
        assert exc.value.code == "no_eligible_unit"
        assert "by hand" in str(exc.value)


async def _mk_library_file(db, filename):
    lf = LibraryFile(filename=filename, file_path=f"/lib/{filename}", file_type="3mf", file_size=1)
    db.add(lf)
    await db.flush()
    return lf


async def _mk_farm_item(
    db,
    *,
    printer_id,
    library_file_id=None,
    eject_profile_id=None,
    batch_id=None,
    archive_id=None,
    plate_id=None,
    status="cancelled",
):
    """A farm queue item (eject_profile_id OR a farm batch) dispatched to a printer —
    the identity anchor identify_farm_file_foreign matches a foreign echo against, and
    the donor anchor the last-farm-item foreign fallback resolves the plate from."""
    item = PrintQueueItem(
        printer_id=printer_id,
        status=status,  # the incident shape: farm units were cancelled
        first_article=False,
        eject_profile_id=eject_profile_id,
        library_file_id=library_file_id,
        archive_id=archive_id,
        batch_id=batch_id,
        plate_id=plate_id,
        started_at=datetime.now(timezone.utc),
        position=1,
    )
    db.add(item)
    await db.flush()
    return item


async def _mk_ondisk_library_file(db, *, filename, file_path):
    """A library row whose ``file_path`` is a REAL on-disk (absolute) donor — the anchor
    the last-farm-item foreign fallback resolves the plate from."""
    lf = LibraryFile(filename=filename, file_path=file_path, file_type="3mf", file_size=1)
    db.add(lf)
    await db.flush()
    return lf


def _make_bare_3mf() -> Path:
    """A .gcode.3mf with NO G-code plate (only a model) — list_gcode_plate_ids → []."""
    fd, name = tempfile.mkstemp(suffix=".gcode.3mf")
    os.close(fd)
    path = Path(name)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")
    return path


class TestManualEjectForeignFallbackLastFarmItem:
    """The screen-RESTART incident fix: when the strict archive resolver 409s (blank
    gate id + download-failed fallback archive), the MANUAL foreign path falls back to
    the printer's last-started farm item's on-disk donor before giving up. The strict
    AUTO path stays fail-closed (unchanged)."""

    @pytest.mark.parametrize("gate", ["", None])
    async def test_blank_gate_falls_back_to_last_item_library_donor(self, db_session, gate):
        source = _make_source_3mf()  # plate_1, max_z 18.0mm
        try:
            printer = await _mk_printer(db_session, "FBLIB", gate=gate)
            prof = EjectProfile(name="fblib-ep", cooldown_temp_c=30.0)
            db_session.add(prof)
            await db_session.flush()
            lf = await _mk_ondisk_library_file(
                db_session, filename="Farm Widget.gcode.3mf", file_path=str(source)
            )
            await _mk_farm_item(
                db_session,
                printer_id=printer.id,
                library_file_id=lf.id,
                eject_profile_id=prof.id,
                plate_id=1,
            )
            await db_session.commit()
            # First call (no profile) → the confirm prompt, carrying the item's donor.
            c1, c2, c3 = _connected_awaiting(_state("FINISH", bed=25.0))
            with (
                c1,
                c2,
                c3,
                patch.object(manual.eject_cooldown_monitor, "active_watch_identity", return_value=None),
                pytest.raises(manual.ForeignPlateEject) as exc,
            ):
                await manual.manual_eject(db_session, printer.id)
            assert exc.value.code == "foreign_plate"
            assert exc.value.print_name == "Farm Widget.gcode.3mf"
            assert exc.value.max_z_height_mm == 18.0
            # Second call (profile chosen) → dispatch the sweep with the fallback donor.
            dispatch = AsyncMock()
            c1, c2, c3 = _connected_awaiting(_state("FINISH", bed=25.0))
            with (
                c1,
                c2,
                c3,
                patch.object(manual.eject_cooldown_monitor, "active_watch_identity", return_value=None),
                patch.object(manual.eject_remote, "dispatch_foreign_eject", dispatch),
            ):
                result = await manual.manual_eject(db_session, printer.id, eject_profile_id=prof.id)
            assert result == {"mode": "dispatched", "queue_item_id": None}
            dispatch.assert_awaited_once()
            assert dispatch.await_args.kwargs["printer_id"] == printer.id
            assert dispatch.await_args.kwargs["profile_id"] == prof.id
            assert dispatch.await_args.kwargs["plate_id"] == 1  # the item's own plate
            assert dispatch.await_args.kwargs["source_path"] == source  # the on-disk fallback donor
        finally:
            source.unlink(missing_ok=True)

    async def test_archive_donor_preferred_over_library(self, db_session):
        source_arch = _make_source_3mf()
        source_lib = _make_source_3mf()
        try:
            printer = await _mk_printer(db_session, "FBARCH", gate="")
            prof = EjectProfile(name="fbarch-ep", cooldown_temp_c=30.0)
            db_session.add(prof)
            await db_session.flush()
            # Archive donor (subtask "OTHER" → the strict gate resolver never finds it).
            archive = await _mk_archive(
                db_session,
                printer_id=printer.id,
                subtask="OTHER",
                file_path=str(source_arch),
                filename="arch.gcode.3mf",
                print_name="Arch Widget",
            )
            lf = await _mk_ondisk_library_file(
                db_session, filename="Lib Widget.gcode.3mf", file_path=str(source_lib)
            )
            await _mk_farm_item(
                db_session,
                printer_id=printer.id,
                archive_id=archive.id,
                library_file_id=lf.id,
                eject_profile_id=prof.id,
                plate_id=1,
            )
            await db_session.commit()
            # Confirm prompt names the ARCHIVE donor (its print_name), not the library.
            c1, c2, c3 = _connected_awaiting(_state("FINISH", bed=25.0))
            with (
                c1,
                c2,
                c3,
                patch.object(manual.eject_cooldown_monitor, "active_watch_identity", return_value=None),
                pytest.raises(manual.ForeignPlateEject) as exc,
            ):
                await manual.manual_eject(db_session, printer.id)
            assert exc.value.print_name == "Arch Widget"
            # And the dispatch uses the archive path, not the library path.
            dispatch = AsyncMock()
            c1, c2, c3 = _connected_awaiting(_state("FINISH", bed=25.0))
            with (
                c1,
                c2,
                c3,
                patch.object(manual.eject_cooldown_monitor, "active_watch_identity", return_value=None),
                patch.object(manual.eject_remote, "dispatch_foreign_eject", dispatch),
            ):
                await manual.manual_eject(db_session, printer.id, eject_profile_id=prof.id)
            assert dispatch.await_args.kwargs["source_path"] == source_arch
        finally:
            source_arch.unlink(missing_ok=True)
            source_lib.unlink(missing_ok=True)

    async def test_no_last_item_refuses_unchanged(self, db_session):
        # No queue item at all → fallback returns None → the ORIGINAL strict 409.
        printer = await _mk_printer(db_session, "FBNONE", gate="")
        await db_session.commit()
        c1, c2, c3 = _connected_awaiting(_state("FINISH", bed=25.0))
        with (
            c1,
            c2,
            c3,
            patch.object(manual.eject_cooldown_monitor, "active_watch_identity", return_value=None),
            pytest.raises(manual.ManualEjectError) as exc,
        ):
            await manual.manual_eject(db_session, printer.id)
        assert exc.value.code == "no_eligible_unit"
        assert "Could not resolve the file" in str(exc.value)
        assert not isinstance(exc.value, manual.ForeignPlateEject)

    async def test_donor_missing_on_disk_refuses_unchanged(self, db_session):
        # The last item's library donor is not on disk → fallback None → strict 409.
        printer = await _mk_printer(db_session, "FBMISS", gate="")
        lf = await _mk_ondisk_library_file(
            db_session, filename="Gone.gcode.3mf", file_path="/nonexistent/Gone.gcode.3mf"
        )
        await _mk_farm_item(db_session, printer_id=printer.id, library_file_id=lf.id, plate_id=1)
        await db_session.commit()
        c1, c2, c3 = _connected_awaiting(_state("FINISH", bed=25.0))
        with (
            c1,
            c2,
            c3,
            patch.object(manual.eject_cooldown_monitor, "active_watch_identity", return_value=None),
            pytest.raises(manual.ManualEjectError) as exc,
        ):
            await manual.manual_eject(db_session, printer.id)
        assert exc.value.code == "no_eligible_unit"
        assert "Could not resolve the file" in str(exc.value)

    async def test_donor_without_gcode_plate_refuses_unchanged(self, db_session):
        # The donor exists but carries no G-code plate → fallback None → strict 409.
        bare = _make_bare_3mf()
        try:
            printer = await _mk_printer(db_session, "FBBARE", gate="")
            lf = await _mk_ondisk_library_file(
                db_session, filename="Bare.gcode.3mf", file_path=str(bare)
            )
            await _mk_farm_item(db_session, printer_id=printer.id, library_file_id=lf.id, plate_id=1)
            await db_session.commit()
            c1, c2, c3 = _connected_awaiting(_state("FINISH", bed=25.0))
            with (
                c1,
                c2,
                c3,
                patch.object(manual.eject_cooldown_monitor, "active_watch_identity", return_value=None),
                pytest.raises(manual.ManualEjectError) as exc,
            ):
                await manual.manual_eject(db_session, printer.id)
            assert exc.value.code == "no_eligible_unit"
            assert "Could not resolve the file" in str(exc.value)
        finally:
            bare.unlink(missing_ok=True)

    async def test_nonblank_gate_matching_item_never_reaches_foreign_resolution(self, db_session):
        # Regression: a NON-empty gate whose id matches a queue item (an unapproved first
        # article) still 409s with the "No farm-known finished unit" message and NEVER
        # touches either foreign resolver.
        printer = await _mk_printer(db_session, "FBFA", gate="SUB-1")
        await _mk_item(db_session, printer_id=printer.id, dispatch_subtask="SUB-1", first_article=True)
        await db_session.commit()
        strict = AsyncMock()
        fallback = AsyncMock()
        c1, c2, c3 = _connected_awaiting(_state("FINISH", bed=25.0))
        with (
            c1,
            c2,
            c3,
            patch.object(manual.eject_cooldown_monitor, "active_watch_identity", return_value=None),
            patch.object(manual, "_resolve_foreign_source", strict),
            patch.object(manual, "_resolve_foreign_source_from_last_farm_item", fallback),
            pytest.raises(manual.ManualEjectError) as exc,
        ):
            await manual.manual_eject(db_session, printer.id)
        assert exc.value.code == "no_eligible_unit"
        assert "No farm-known finished unit" in str(exc.value)
        strict.assert_not_called()
        fallback.assert_not_called()


class TestCanonicalNames:
    """The underscore/extension canonicalisation that makes a screen-started print's
    UNDERSCORED USB echo compare equal to the farm's SPACED library/archive name —
    the fold farm_correlation._normalize_name deliberately does NOT do."""

    async def test_underscored_echo_matches_spaced_stored_name(self):
        # (async only to satisfy the module-level asyncio pytestmark; the fn is pure.)
        echoed = manual._canonical_names(".6_nozzle_(Battery_holders_X2)", None)
        stored = manual._canonical_names(".6 nozzle (Battery holders X2).gcode.3mf")
        assert echoed == stored
        assert echoed == {".6_nozzle_(Battery_holders_X2).3mf"}

    async def test_blanks_skipped_and_basename_stripped(self):
        assert manual._canonical_names(None, "") == set()
        # A path-prefixed name is basename-stripped before canonicalising.
        assert manual._canonical_names("/data/Widget A.3mf") == {"Widget_A.3mf"}


class TestIdentifyFarmFileForeign:
    """F5: a foreign completion is auto-ejected ONLY when positively the farm's OWN
    file — name match (canonicalised) AND validated geometry AND a suggested profile
    AND the donor height within that profile's guard. Any miss → None (escalation)."""

    async def test_positive_identification_returns_profile_and_threshold(self, db_session, seed_geometry):
        source = _make_source_3mf()  # plate max_z 18.0mm, within the 42mm guard
        try:
            printer = await _mk_printer(db_session, "IDN", gate="SUB-F")  # H2S → validated
            prof = EjectProfile(name="idn-ep", cooldown_temp_c=30.0, max_part_height_mm=42.0)
            db_session.add(prof)
            await db_session.flush()
            lf = await _mk_library_file(db_session, "Farm Widget.gcode.3mf")  # SPACED display name
            await _mk_farm_item(db_session, printer_id=printer.id, library_file_id=lf.id, eject_profile_id=prof.id)
            await _mk_archive(db_session, printer_id=printer.id, subtask="SUB-F", file_path=str(source))
            await db_session.commit()
            # Echoed name = the UNDERSCORED USB filename a screen-start reports.
            result = await manual.identify_farm_file_foreign(
                db_session, printer.id, subtask_name="Farm_Widget", filename="Farm_Widget.gcode.3mf"
            )
            assert result is not None
            assert result.profile_id == prof.id
            assert result.threshold_c == 30.0
            assert result.print_name == "Foreign Widget"
        finally:
            source.unlink(missing_ok=True)

    async def test_negative_when_name_is_not_a_farm_file(self, db_session, seed_geometry):
        source = _make_source_3mf()
        try:
            printer = await _mk_printer(db_session, "IDNEG", gate="SUB-F")
            prof = EjectProfile(name="idneg-ep")
            db_session.add(prof)
            await db_session.flush()
            lf = await _mk_library_file(db_session, "Some Other File.gcode.3mf")
            await _mk_farm_item(db_session, printer_id=printer.id, library_file_id=lf.id, eject_profile_id=prof.id)
            await _mk_archive(db_session, printer_id=printer.id, subtask="SUB-F", file_path=str(source))
            await db_session.commit()
            result = await manual.identify_farm_file_foreign(
                db_session, printer.id, subtask_name="Totally_Unrelated_Local", filename="local.gcode"
            )
            assert result is None
        finally:
            source.unlink(missing_ok=True)

    async def test_negative_when_geometry_unvalidated(self, db_session, seed_geometry):
        source = _make_source_3mf()
        try:
            printer = await _mk_printer(db_session, "IDU", gate="SUB-F", model="H2C")  # H2C → unvalidated
            prof = EjectProfile(name="idu-ep")
            db_session.add(prof)
            await db_session.flush()
            lf = await _mk_library_file(db_session, "Farm Widget.gcode.3mf")
            await _mk_farm_item(db_session, printer_id=printer.id, library_file_id=lf.id, eject_profile_id=prof.id)
            await _mk_archive(db_session, printer_id=printer.id, subtask="SUB-F", file_path=str(source))
            await db_session.commit()
            # Name matches, but H2C geometry is not hardware-validated → no auto-eject.
            result = await manual.identify_farm_file_foreign(
                db_session, printer.id, subtask_name="Farm_Widget", filename=None
            )
            assert result is None
        finally:
            source.unlink(missing_ok=True)

    async def test_negative_when_no_suggested_profile(self, db_session, seed_geometry):
        source = _make_source_3mf()
        try:
            printer = await _mk_printer(db_session, "IDNP", gate="SUB-F")
            batch = PrintBatch(name="run", sku_file_id=1)  # farm via batch, NOT an eject-profiled unit
            db_session.add(batch)
            await db_session.flush()
            lf = await _mk_library_file(db_session, "Farm Widget.gcode.3mf")
            await _mk_farm_item(db_session, printer_id=printer.id, library_file_id=lf.id, batch_id=batch.id)
            await _mk_archive(db_session, printer_id=printer.id, subtask="SUB-F", file_path=str(source))
            await db_session.commit()
            # Name matches + geometry validated, but no eject-profiled unit → no profile.
            result = await manual.identify_farm_file_foreign(
                db_session, printer.id, subtask_name="Farm_Widget", filename=None
            )
            assert result is None
        finally:
            source.unlink(missing_ok=True)

    async def test_negative_when_height_exceeds_profile_guard(self, db_session, seed_geometry):
        source = _make_source_3mf()  # 18.0mm part
        try:
            printer = await _mk_printer(db_session, "IDH", gate="SUB-F")
            prof = EjectProfile(name="idh-ep", max_part_height_mm=10.0)  # guard below the 18mm part
            db_session.add(prof)
            await db_session.flush()
            lf = await _mk_library_file(db_session, "Farm Widget.gcode.3mf")
            await _mk_farm_item(db_session, printer_id=printer.id, library_file_id=lf.id, eject_profile_id=prof.id)
            await _mk_archive(db_session, printer_id=printer.id, subtask="SUB-F", file_path=str(source))
            await db_session.commit()
            result = await manual.identify_farm_file_foreign(
                db_session, printer.id, subtask_name="Farm_Widget", filename=None
            )
            assert result is None
        finally:
            source.unlink(missing_ok=True)


class TestDispatchIdentifiedForeignEject:
    """The auto foreign-eject on_release: resolve the donor fresh and dispatch the
    foreign-plate sweep exactly as the manual confirm does (no thermal gate)."""

    async def test_dispatches_foreign_eject_for_gate_source(self, db_session, monkeypatch):
        import contextlib

        source = _make_source_3mf()
        try:
            printer = await _mk_printer(db_session, "IDD", gate="SUB-F")
            await _mk_archive(db_session, printer_id=printer.id, subtask="SUB-F", file_path=str(source))
            await db_session.commit()

            @contextlib.asynccontextmanager
            async def _fake_session():
                yield db_session

            # dispatch_identified_foreign_eject opens its OWN session — back it with db_session.
            monkeypatch.setattr("backend.app.core.database.async_session", _fake_session, raising=False)
            dispatch = AsyncMock()
            with patch.object(manual.eject_remote, "dispatch_foreign_eject", dispatch):
                await manual.dispatch_identified_foreign_eject(printer_id=printer.id, profile_id=5)
            dispatch.assert_awaited_once()
            assert dispatch.await_args.kwargs["printer_id"] == printer.id
            assert dispatch.await_args.kwargs["profile_id"] == 5
            assert dispatch.await_args.kwargs["plate_id"] == 1
        finally:
            source.unlink(missing_ok=True)
