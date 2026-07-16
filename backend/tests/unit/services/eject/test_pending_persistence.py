"""Durable pending-eject mirror: persist / clear / hydrate (W1.1)."""

import contextlib
import os
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend.app.models.eject_profile import EjectProfile
from backend.app.models.library import LibraryFile
from backend.app.models.print_batch import PrintBatch
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services.eject import remote
from backend.app.services.printer_manager import printer_manager

pytestmark = pytest.mark.asyncio


async def _mk_item(db, *, printer_id, first_article=False, batch_id=None, status="completed", stamp=None):
    item = PrintQueueItem(
        printer_id=printer_id,
        status=status,
        first_article=first_article,
        batch_id=batch_id,
        plate_id=1,
        position=1,
        eject_dispatched_at=stamp,
    )
    db.add(item)
    await db.flush()
    return item


async def _mk_printer(db, name="PP"):
    p = Printer(name=name, serial_number=f"S{name}", ip_address="1.2.3.4", access_code="x", model="H2S")
    db.add(p)
    await db.flush()
    return p


def _patch_session(monkeypatch, db_session):
    @contextlib.asynccontextmanager
    async def _fake_session():
        yield db_session

    monkeypatch.setattr("backend.app.core.database.async_session", _fake_session, raising=False)


class TestPersistAndClear:
    async def test_persist_stamps_owning_item(self, db_session):
        printer = await _mk_printer(db_session, "PST")
        item = await _mk_item(db_session, printer_id=printer.id)
        await db_session.commit()
        pe = remote.PendingEject("production", None, item.id)
        try:
            await remote.persist_pending_eject(db_session, printer.id, pe)
            await db_session.refresh(item)
            assert item.eject_dispatched_at is not None
        finally:
            remote.pop_pending_eject(printer.id)

    async def test_persist_missing_item_is_noop(self, db_session):
        # queue_item_id that does not exist → no crash, nothing stamped.
        await remote.persist_pending_eject(db_session, 12345, remote.PendingEject("production", None, 999999))

    async def test_clear_pops_registry_and_nulls_stamp(self, db_session):
        printer = await _mk_printer(db_session, "PCL")
        item = await _mk_item(db_session, printer_id=printer.id, stamp=datetime.now(timezone.utc))
        await db_session.commit()
        remote.register_pending_eject(printer.id, remote.PendingEject("production", None, item.id))
        popped = await remote.clear_pending_eject(db_session, printer.id)
        assert popped is not None
        assert remote.peek_pending_eject(printer.id) is None
        await db_session.refresh(item)
        assert item.eject_dispatched_at is None

    async def test_clear_nulls_all_stamps_for_printer(self, db_session):
        # Defensive printer-scoped NULL: two stamped rows on one printer both clear.
        printer = await _mk_printer(db_session, "PCL2")
        a = await _mk_item(db_session, printer_id=printer.id, stamp=datetime.now(timezone.utc))
        b = await _mk_item(db_session, printer_id=printer.id, stamp=datetime.now(timezone.utc))
        await db_session.commit()
        remote.register_pending_eject(printer.id, remote.PendingEject("production", None, b.id))
        await remote.clear_pending_eject(db_session, printer.id)
        await db_session.refresh(a)
        await db_session.refresh(b)
        assert a.eject_dispatched_at is None
        assert b.eject_dispatched_at is None


class TestHydration:
    async def test_hydrates_purpose_and_run_from_item(self, db_session, monkeypatch):
        printer = await _mk_printer(db_session, "HYD")
        batch = PrintBatch(name="r", quantity=1, status="active")
        db_session.add(batch)
        await db_session.flush()
        prod = await _mk_item(
            db_session, printer_id=printer.id, batch_id=batch.id, stamp=datetime.now(timezone.utc)
        )
        await db_session.commit()
        _patch_session(monkeypatch, db_session)
        try:
            n = await remote.hydrate_pending_ejects_from_db()
            assert n == 1
            pe = remote.peek_pending_eject(printer.id)
            assert pe == remote.PendingEject("production", batch.id, prod.id)
        finally:
            remote.pop_pending_eject(printer.id)

    async def test_hydrates_fa_purpose(self, db_session, monkeypatch):
        printer = await _mk_printer(db_session, "HYDFA")
        await _mk_item(db_session, printer_id=printer.id, first_article=True, stamp=datetime.now(timezone.utc))
        await db_session.commit()
        _patch_session(monkeypatch, db_session)
        try:
            await remote.hydrate_pending_ejects_from_db()
            assert remote.peek_pending_eject(printer.id).purpose == "fa"
        finally:
            remote.pop_pending_eject(printer.id)

    async def test_stale_stamp_dropped_and_warned(self, db_session, monkeypatch, caplog):
        printer = await _mk_printer(db_session, "HYDSTALE")
        stale = datetime.now(timezone.utc) - timedelta(hours=remote._PENDING_EJECT_STALE_TTL_H + 1)
        item = await _mk_item(db_session, printer_id=printer.id, stamp=stale)
        await db_session.commit()
        _patch_session(monkeypatch, db_session)
        try:
            import logging

            with caplog.at_level(logging.WARNING):
                n = await remote.hydrate_pending_ejects_from_db()
            assert n == 0
            assert remote.peek_pending_eject(printer.id) is None
            await db_session.refresh(item)
            assert item.eject_dispatched_at is None  # NULLed
            assert any("stale pending-eject" in r.message for r in caplog.records)
        finally:
            remote.pop_pending_eject(printer.id)

    async def test_multiple_rows_one_printer_keeps_newest(self, db_session, monkeypatch):
        printer = await _mk_printer(db_session, "HYDDUP")
        now = datetime.now(timezone.utc)
        older = await _mk_item(db_session, printer_id=printer.id, stamp=now - timedelta(minutes=30))
        newer = await _mk_item(db_session, printer_id=printer.id, stamp=now)
        await db_session.commit()
        _patch_session(monkeypatch, db_session)
        try:
            n = await remote.hydrate_pending_ejects_from_db()
            assert n == 1
            assert remote.peek_pending_eject(printer.id).queue_item_id == newer.id
            await db_session.refresh(older)
            assert older.eject_dispatched_at is None  # older stamp NULLed
        finally:
            remote.pop_pending_eject(printer.id)


class TestDispatchStampsOnDispatch:
    """Full dispatch path stamps the owning unit; a re-dispatch re-stamps."""

    @staticmethod
    def _make_source_3mf() -> Path:
        fd, name = tempfile.mkstemp(suffix=".gcode.3mf")
        os.close(fd)
        path = Path(name)
        gcode = (
            "; HEADER_BLOCK_START\n"
            "; max_z_height: 18.00\n"
            "; HEADER_BLOCK_END\n"
            "; EXECUTABLE_BLOCK_START\n"
            "G1 X10 Y10\n"
            "; EXECUTABLE_BLOCK_END\n"
        )
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("Metadata/plate_1.gcode", gcode)
            zf.writestr("3D/3dmodel.model", "<model/>")
        return path

    async def test_dispatch_stamps_eject_dispatched_at(self, db_session, seed_geometry):
        from unittest.mock import AsyncMock

        source = self._make_source_3mf()
        try:
            printer = await _mk_printer(db_session, "PDISP")
            lib = LibraryFile(
                filename="s.gcode.3mf", file_path=str(source), file_type="gcode.3mf",
                file_size=source.stat().st_size, is_external=True,
            )
            db_session.add(lib)
            await db_session.flush()
            prof = EjectProfile(name="pd-ep")
            db_session.add(prof)
            await db_session.flush()
            item = await _mk_item(db_session, printer_id=printer.id)
            item.library_file_id = lib.id
            item.eject_profile_id = prof.id
            await db_session.commit()
            with (
                patch.object(printer_manager, "is_connected", return_value=True),
                patch("backend.app.services.bambu_ftp.get_ftp_retry_settings", AsyncMock(return_value=(False, 0, 0, 30))),
                patch("backend.app.services.bambu_ftp.upload_file_async", AsyncMock(return_value=True)),
                patch.object(printer_manager, "start_print", MagicMock(return_value=True)),
            ):
                await remote.dispatch_part_present_eject(
                    db_session, printer_id=printer.id, queue_item_id=item.id, purpose="production", run_id=None
                )
            await db_session.refresh(item)
            assert item.eject_dispatched_at is not None
            first_stamp = item.eject_dispatched_at
            # Clear then re-dispatch re-stamps.
            await remote.clear_pending_eject(db_session, printer.id)
            await db_session.refresh(item)
            assert item.eject_dispatched_at is None
            with (
                patch.object(printer_manager, "is_connected", return_value=True),
                patch("backend.app.services.bambu_ftp.get_ftp_retry_settings", AsyncMock(return_value=(False, 0, 0, 30))),
                patch("backend.app.services.bambu_ftp.upload_file_async", AsyncMock(return_value=True)),
                patch.object(printer_manager, "start_print", MagicMock(return_value=True)),
            ):
                await remote.dispatch_part_present_eject(
                    db_session, printer_id=printer.id, queue_item_id=item.id, purpose="production", run_id=None
                )
            await db_session.refresh(item)
            assert item.eject_dispatched_at is not None
            assert first_stamp is not None
        finally:
            _pid = locals().get("printer")
            if _pid is not None:
                remote.pop_pending_eject(_pid.id)
            source.unlink(missing_ok=True)
