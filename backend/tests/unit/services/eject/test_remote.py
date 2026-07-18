"""Tests for the shared part-present eject dispatcher (eject.remote)."""

import os
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.models.eject_profile import EjectProfile
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services.eject import remote
from backend.app.services.printer_manager import printer_manager

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _auto_seed_geometry(seed_geometry):
    """dispatch_part_present_eject resolves H2S geometry from the DB registry."""
    return seed_geometry


_PLATE_GCODE = (
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
        zf.writestr("Metadata/plate_1.gcode", _PLATE_GCODE)
        zf.writestr("3D/3dmodel.model", "<model/>")
    return path


async def _seed(db, source: Path):
    printer = Printer(name="RM", serial_number="RM1", ip_address="1.2.3.4", access_code="x", model="H2S")
    db.add(printer)
    await db.flush()
    lib = LibraryFile(
        filename="src.gcode.3mf",
        file_path=str(source),
        file_type="gcode.3mf",
        file_size=source.stat().st_size,
        is_external=True,
    )
    db.add(lib)
    await db.flush()
    prof = EjectProfile(name="rm-ep")
    db.add(prof)
    await db.flush()
    item = PrintQueueItem(
        printer_id=printer.id,
        library_file_id=lib.id,
        eject_profile_id=prof.id,
        status="completed",
        plate_id=1,
        position=1,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    await db.refresh(printer)
    return printer, item


def _ftp_patches(*, connected=True, upload=True, started=True):
    return (
        patch.object(printer_manager, "is_connected", return_value=connected),
        patch("backend.app.services.bambu_ftp.get_ftp_retry_settings", AsyncMock(return_value=(False, 0, 0, 30))),
        patch("backend.app.services.bambu_ftp.upload_file_async", AsyncMock(return_value=upload)),
    )


class TestDispatchPartPresentEject:
    async def test_success_registers_pending_and_starts_all_off(self, db_session):
        source = _make_source_3mf()
        try:
            printer, item = await _seed(db_session, source)
            start = MagicMock(return_value=True)
            c1, c2, c3 = _ftp_patches()
            with c1, c2, c3, patch.object(printer_manager, "start_print", start):
                await remote.dispatch_part_present_eject(
                    db_session, printer_id=printer.id, queue_item_id=item.id, purpose="production", run_id=555
                )
            # Pending registered with the typed tuple.
            pending = remote.peek_pending_eject(printer.id)
            assert pending == remote.PendingEject("production", 555, item.id)
            # EVERY pre-print calibration OFF (never probe/shake with a part present).
            start.assert_called_once()
            kwargs = start.call_args.kwargs
            assert kwargs["bed_levelling"] is False
            assert kwargs["flow_cali"] is False
            assert kwargs["vibration_cali"] is False
            assert kwargs["layer_inspect"] is False
            assert kwargs["timelapse"] is False
            assert kwargs["use_ams"] is False
            assert kwargs["plate_id"] == 1
        finally:
            remote.pop_pending_eject(printer.id)
            source.unlink(missing_ok=True)

    async def test_not_connected_raises_409_no_pending(self, db_session):
        source = _make_source_3mf()
        try:
            printer, item = await _seed(db_session, source)
            c1, c2, c3 = _ftp_patches(connected=False)
            with (
                c1,
                c2,
                c3,
                patch.object(printer_manager, "start_print", MagicMock(return_value=True)),
                pytest.raises(remote.EjectDispatchError) as exc,
            ):
                await remote.dispatch_part_present_eject(
                    db_session, printer_id=printer.id, queue_item_id=item.id, purpose="production", run_id=1
                )
            assert exc.value.status_code == 409
            assert remote.peek_pending_eject(printer.id) is None
        finally:
            remote.pop_pending_eject(printer.id)
            source.unlink(missing_ok=True)

    async def test_upload_failure_raises_502_no_pending(self, db_session):
        source = _make_source_3mf()
        try:
            printer, item = await _seed(db_session, source)
            c1, c2, c3 = _ftp_patches(upload=False)
            with (
                c1,
                c2,
                c3,
                patch.object(printer_manager, "start_print", MagicMock(return_value=True)),
                pytest.raises(remote.EjectDispatchError) as exc,
            ):
                await remote.dispatch_part_present_eject(
                    db_session, printer_id=printer.id, queue_item_id=item.id, purpose="fa", run_id=1
                )
            assert exc.value.status_code == 502
            assert remote.peek_pending_eject(printer.id) is None
        finally:
            remote.pop_pending_eject(printer.id)
            source.unlink(missing_ok=True)

    async def test_start_print_failure_raises_502(self, db_session):
        source = _make_source_3mf()
        try:
            printer, item = await _seed(db_session, source)
            c1, c2, c3 = _ftp_patches()
            with (
                c1,
                c2,
                c3,
                patch.object(printer_manager, "start_print", MagicMock(return_value=False)),
                pytest.raises(remote.EjectDispatchError) as exc,
            ):
                await remote.dispatch_part_present_eject(
                    db_session, printer_id=printer.id, queue_item_id=item.id, purpose="production", run_id=1
                )
            assert exc.value.status_code == 502
        finally:
            remote.pop_pending_eject(printer.id)
            source.unlink(missing_ok=True)


class TestMatchesPendingEject:
    """The shared eject-terminal detection helper (single origin of the mismatch
    rule shared by farm_policy.on_terminal and the main.py start/complete callbacks).
    Lenient: a positive mismatch needs BOTH ids truthy AND unequal."""

    @staticmethod
    def _client(subtask):
        from types import SimpleNamespace

        return SimpleNamespace(last_dispatch_subtask_id=subtask)

    async def test_no_pending_registered_is_false(self):
        with patch.object(printer_manager, "get_client", return_value=self._client("SUB")):
            assert remote.matches_pending_eject(9001, "SUB") is False

    async def test_echo_absent_lenient_match(self):
        remote.register_pending_eject(9002, remote.PendingEject("production", 1, 2))
        try:
            with patch.object(printer_manager, "get_client", return_value=self._client("SUB")):
                assert remote.matches_pending_eject(9002, None) is True
            # Never pops — the caller owns the pop.
            assert remote.peek_pending_eject(9002) is not None
        finally:
            remote.pop_pending_eject(9002)

    async def test_echo_equal_matches(self):
        remote.register_pending_eject(9003, remote.PendingEject("production", 1, 2))
        try:
            with patch.object(printer_manager, "get_client", return_value=self._client("SUB-E")):
                assert remote.matches_pending_eject(9003, "SUB-E") is True
        finally:
            remote.pop_pending_eject(9003)

    async def test_echo_mismatch_both_truthy_is_false(self):
        remote.register_pending_eject(9004, remote.PendingEject("production", 1, 2))
        try:
            with patch.object(printer_manager, "get_client", return_value=self._client("SUB-E")):
                assert remote.matches_pending_eject(9004, "OTHER") is False
            # Mismatch does NOT pop the registry — the real terminal still owns it.
            assert remote.peek_pending_eject(9004) is not None
        finally:
            remote.pop_pending_eject(9004)

    async def test_expected_absent_lenient_match(self):
        remote.register_pending_eject(9005, remote.PendingEject("production", 1, 2))
        try:
            with patch.object(printer_manager, "get_client", return_value=self._client(None)):
                assert remote.matches_pending_eject(9005, "SUB") is True
        finally:
            remote.pop_pending_eject(9005)

    async def test_no_client_lenient_match(self):
        remote.register_pending_eject(9006, remote.PendingEject("production", 1, 2))
        try:
            with patch.object(printer_manager, "get_client", return_value=None):
                assert remote.matches_pending_eject(9006, "SUB") is True
        finally:
            remote.pop_pending_eject(9006)


class TestEjectNameHelpers:
    """The durable eject-job-name convention (single origin of the
    ``eject_{purpose}_item{N}`` stem) — parse / detect / expected-stem."""

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("eject_production_item32", ("production", 32)),
            ("eject_fa_item7", ("fa", 7)),
            ("eject_production_item32.3mf", ("production", 32)),  # filename suffix
            ("eject_production_item32.gcode.3mf", ("production", 32)),  # doubled suffix
            ("/eject_fa_item9.3mf", ("fa", 9)),  # leading path
            ("EJECT_PRODUCTION_ITEM5", ("production", 5)),  # case-insensitive
        ],
    )
    def test_parse_positive(self, name, expected):
        assert remote.parse_eject_job_name(name) == expected
        assert remote.is_eject_job_name(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            None,
            "",
            "OperatorLocalPrint",
            "widget_v3.gcode.3mf",
            "eject_item32",
            "eject_production_item",
            "reject_production_item1",
        ],
    )
    def test_parse_negative(self, name):
        assert remote.parse_eject_job_name(name) is None
        assert remote.is_eject_job_name(name) is False

    def test_expected_stem(self):
        pe = remote.PendingEject("production", 5, 32)
        assert remote.expected_eject_stem(pe) == "eject_production_item32"
        assert remote.expected_eject_stem(remote.PendingEject("fa", 1, 7)) == "eject_fa_item7"


class TestMatchesPendingEjectNameTightening:
    """The tightened matcher (W1/R2): a truthy subtask_name whose stem != the
    pending's expected stem is a POSITIVE mismatch even when the id path is lenient
    (post-restart, no client). Name-match alone with an empty registry is still
    False — only the pending identity gates the resolution."""

    @staticmethod
    def _client(subtask):
        from types import SimpleNamespace

        return SimpleNamespace(last_dispatch_subtask_id=subtask)

    async def test_name_mismatch_positive_mismatch_no_client(self):
        # Post-restart hole: no client (id lenient), but the echoed name is a foreign
        # job → the name check re-establishes the mismatch → NOT our eject.
        remote.register_pending_eject(9101, remote.PendingEject("production", 1, 32))
        try:
            with patch.object(printer_manager, "get_client", return_value=None):
                assert remote.matches_pending_eject(9101, "ANY", subtask_name="OperatorLocalPrint") is False
                assert remote.peek_pending_eject(9101) is not None  # foreign — pending kept
        finally:
            remote.pop_pending_eject(9101)

    async def test_name_matches_expected_stem_no_client(self):
        remote.register_pending_eject(9102, remote.PendingEject("production", 1, 32))
        try:
            with patch.object(printer_manager, "get_client", return_value=None):
                assert remote.matches_pending_eject(9102, None, subtask_name="eject_production_item32") is True
        finally:
            remote.pop_pending_eject(9102)

    async def test_missing_name_stays_lenient(self):
        # No name supplied → only the (lenient) id path applies.
        remote.register_pending_eject(9103, remote.PendingEject("production", 1, 32))
        try:
            with patch.object(printer_manager, "get_client", return_value=self._client(None)):
                assert remote.matches_pending_eject(9103, "SUB", subtask_name=None) is True
        finally:
            remote.pop_pending_eject(9103)

    async def test_name_alone_empty_registry_never_matches(self):
        # is_eject_job_name is the suppress-only signal; matches_pending_eject still
        # requires a registered pending identity.
        with patch.object(printer_manager, "get_client", return_value=None):
            assert remote.matches_pending_eject(9104, "ANY", subtask_name="eject_production_item32") is False

    async def test_name_wrong_item_number_mismatches(self):
        remote.register_pending_eject(9105, remote.PendingEject("production", 1, 32))
        try:
            with patch.object(printer_manager, "get_client", return_value=None):
                # Right shape, wrong item id → foreign instance's eject → mismatch.
                assert remote.matches_pending_eject(9105, None, subtask_name="eject_production_item99") is False
        finally:
            remote.pop_pending_eject(9105)


class TestManualEjectName:
    """The foreign-plate manual eject job stem ``eject_manual_p{printer_id}`` — parse
    round-trip + the printer-keyed name check in matches_pending_eject."""

    @staticmethod
    def _client(subtask):
        from types import SimpleNamespace

        return SimpleNamespace(last_dispatch_subtask_id=subtask)

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("eject_manual_p7", ("manual", 7)),
            ("eject_manual_p7.3mf", ("manual", 7)),
            ("eject_manual_p7.gcode.3mf", ("manual", 7)),
            ("/eject_manual_p42.3mf", ("manual", 42)),
            ("EJECT_MANUAL_P5", ("manual", 5)),
        ],
    )
    def test_parse_manual_positive(self, name, expected):
        assert remote.parse_eject_job_name(name) == expected
        assert remote.is_eject_job_name(name) is True

    @pytest.mark.parametrize("name", ["eject_manual_item7", "eject_manual_p", "eject_fa_p7", "eject_manual"])
    def test_parse_manual_negative(self, name):
        assert remote.parse_eject_job_name(name) is None
        assert remote.is_eject_job_name(name) is False

    async def test_manual_pending_name_match_by_printer_id(self):
        # A manual pending has queue_item_id=None; the name check keys off the PRINTER
        # id, closing the old queue_item_id-None leniency for this purpose.
        remote.register_pending_eject(9201, remote.PendingEject("manual", None, None))
        try:
            with patch.object(printer_manager, "get_client", return_value=None):
                assert remote.matches_pending_eject(9201, None, subtask_name="eject_manual_p9201") is True
                assert remote.matches_pending_eject(9201, None, subtask_name="eject_manual_p9999") is False
                assert remote.matches_pending_eject(9201, None, subtask_name="OperatorLocalPrint") is False
        finally:
            remote.pop_pending_eject(9201)


class TestDispatchForeignEject:
    async def test_registers_manual_pending_and_starts_all_off(self, db_session):
        source = _make_source_3mf()
        try:
            printer = Printer(name="FE", serial_number="FE1", ip_address="1.2.3.4", access_code="x", model="H2S")
            db_session.add(printer)
            await db_session.flush()
            prof = EjectProfile(name="fe-ep")
            db_session.add(prof)
            await db_session.commit()
            await db_session.refresh(printer)
            await db_session.refresh(prof)
            start = MagicMock(return_value=True)
            c1, c2, c3 = _ftp_patches()
            with c1, c2, c3, patch.object(printer_manager, "start_print", start):
                await remote.dispatch_foreign_eject(
                    db_session, printer_id=printer.id, profile_id=prof.id, source_path=source, plate_id=1
                )
            pending = remote.peek_pending_eject(printer.id)
            assert pending == remote.PendingEject("manual", None, None)
            assert pending.queue_item_id is None
            # The uploaded/started filename derives from the printer-keyed manual stem.
            started_name = start.call_args.args[1]
            assert started_name == f"eject_manual_p{printer.id}.3mf"
            kwargs = start.call_args.kwargs
            assert kwargs["bed_levelling"] is False
            assert kwargs["vibration_cali"] is False
            assert kwargs["use_ams"] is False
        finally:
            remote.pop_pending_eject(printer.id)
            source.unlink(missing_ok=True)

    async def test_upload_failure_raises_502_no_pending(self, db_session):
        source = _make_source_3mf()
        printer_id = None
        try:
            printer = Printer(name="FEu", serial_number="FEu1", ip_address="1.2.3.4", access_code="x", model="H2S")
            db_session.add(printer)
            await db_session.flush()
            prof = EjectProfile(name="feu-ep")
            db_session.add(prof)
            await db_session.commit()
            await db_session.refresh(printer)
            await db_session.refresh(prof)
            printer_id = printer.id
            c1, c2, c3 = _ftp_patches(upload=False)
            with (
                c1,
                c2,
                c3,
                patch.object(printer_manager, "start_print", MagicMock(return_value=True)),
                pytest.raises(remote.EjectDispatchError) as exc,
            ):
                await remote.dispatch_foreign_eject(
                    db_session, printer_id=printer.id, profile_id=prof.id, source_path=source, plate_id=1
                )
            assert exc.value.status_code == 502
            assert remote.peek_pending_eject(printer.id) is None
        finally:
            if printer_id is not None:
                remote.pop_pending_eject(printer_id)
            source.unlink(missing_ok=True)

    async def test_unknown_profile_raises_409(self, db_session):
        source = _make_source_3mf()
        try:
            printer = Printer(name="FEp", serial_number="FEp1", ip_address="1.2.3.4", access_code="x", model="H2S")
            db_session.add(printer)
            await db_session.commit()
            await db_session.refresh(printer)
            c1, c2, c3 = _ftp_patches()
            with c1, c2, c3, pytest.raises(remote.EjectDispatchError) as exc:
                await remote.dispatch_foreign_eject(
                    db_session, printer_id=printer.id, profile_id=987654, source_path=source, plate_id=1
                )
            assert exc.value.status_code == 409
        finally:
            source.unlink(missing_ok=True)


class TestDispatchPartPresentEjectProfileGuard:
    async def test_item_without_profile_raises_409(self, db_session):
        source = _make_source_3mf()
        try:
            printer = Printer(name="NP", serial_number="NP1", ip_address="1.2.3.4", access_code="x", model="H2S")
            db_session.add(printer)
            await db_session.flush()
            item = PrintQueueItem(printer_id=printer.id, status="completed", plate_id=1, position=1)
            db_session.add(item)
            await db_session.commit()
            await db_session.refresh(item)
            await db_session.refresh(printer)
            c1, c2, c3 = _ftp_patches()
            with (
                c1,
                c2,
                c3,
                patch.object(printer_manager, "start_print", MagicMock(return_value=True)),
                pytest.raises(remote.EjectDispatchError) as exc,
            ):
                await remote.dispatch_part_present_eject(
                    db_session, printer_id=printer.id, queue_item_id=item.id, purpose="production", run_id=1
                )
            assert exc.value.status_code == 409
            assert "eject profile" in str(exc.value).lower()
        finally:
            source.unlink(missing_ok=True)
