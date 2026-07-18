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
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services.eject import manual, remote as eject_remote
from backend.app.services.eject.monitor import _ActiveWatch

pytestmark = pytest.mark.asyncio


async def _mk_printer(db, name="MPE", gate="SUB-1"):
    p = Printer(
        name=name,
        serial_number=f"S{name}",
        ip_address="1.2.3.4",
        access_code="x",
        model="H2S",
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
