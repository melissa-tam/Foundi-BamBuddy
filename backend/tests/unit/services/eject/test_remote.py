"""Tests for the shared part-present eject dispatcher (eject.remote)."""

import asyncio
import contextlib
import logging
import os
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
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
            # Pending registered with the typed identity...
            pending = remote.peek_pending_eject(printer.id)
            assert (pending.purpose, pending.run_id, pending.queue_item_id) == ("production", 555, item.id)
            # ...carrying the build's runtime estimate for the terminal-side guard,
            # and NOT yet started (that stamp lands on the printer's start echo).
            assert pending.expected_runtime_s is not None
            assert pending.expected_runtime_s > 0
            assert pending.started_at is None
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
            assert (pending.purpose, pending.run_id, pending.queue_item_id) == ("manual", None, None)
            # A manual sweep carries the estimate too (logged at its terminal), but
            # never gates the plate on it — an operator is standing at the machine.
            assert pending.expected_runtime_s is not None
            assert pending.expected_runtime_s > 0
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


class TestEjectUploadBareName:
    """The eject file is always uploaded under the bare ``eject_{purpose}_item{id}``
    stem — no content-hash suffix, no FTPS SIZE de-dupe probe (the skip-if-identical
    feature was deleted; slim builds are always-on)."""

    async def test_uploads_under_bare_name_with_no_size_probe(self, db_session):
        # The SIZE-probe de-dupe machinery is gone entirely — the FTP helper it used
        # was deleted with the feature.
        from backend.app.services import bambu_ftp

        assert not hasattr(bambu_ftp, "get_file_size_async")

        source = _make_source_3mf()
        try:
            printer, item = await _seed(db_session, source)
            start = MagicMock(return_value=True)
            upload = AsyncMock(return_value=True)
            c1, _c2, _c3 = _ftp_patches()  # only reuse the is_connected patch
            with (
                c1,
                patch(
                    "backend.app.services.bambu_ftp.get_ftp_retry_settings", AsyncMock(return_value=(False, 0, 0, 30))
                ),
                patch("backend.app.services.bambu_ftp.upload_file_async", upload),
                patch.object(printer_manager, "start_print", start),
            ):
                await remote.dispatch_part_present_eject(
                    db_session, printer_id=printer.id, queue_item_id=item.id, purpose="production", run_id=1
                )
            # The upload always runs, and the name is the bare stem (no hash suffix).
            upload.assert_awaited_once()
            assert start.call_args.args[1] == f"eject_production_item{item.id}.3mf"
        finally:
            remote.pop_pending_eject(printer.id)
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


class _FakeSleep:
    """A ``sleep`` stand-in that records each requested delay and returns at once.

    Lets a watchdog test drive the full deadline → stop → retry sequence without any
    wall-clock wait, while still asserting WHAT was waited on."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def _armed(pid_item: int = 5, **kw) -> remote.PendingEject:
    """A started production pending — what the watchdog is armed with."""
    kw.setdefault("expected_runtime_s", 83.0)
    kw.setdefault("started_at", datetime.now(timezone.utc) - timedelta(seconds=104))
    return remote.PendingEject("production", 1, pid_item, **kw)


class TestEjectAbortDeadline:
    """The abort deadline in all three regimes (2026-07-31 gouged-plate incident).

    A stalled bed-drop leaves NO trace in MQTT — no Z telemetry, no HMS — and the job
    still reports ``completed``. Elapsed execution time is the only signature, so the
    watchdog turns the build's estimate into a deadline and stops the job at it."""

    async def test_short_profile_gets_the_twenty_second_floor(self):
        # 25% of 40 s is 10 s — below the floor. The floor protects against TELEMETRY
        # (the FINISH echo is what cancels the watchdog), not against motion.
        assert remote.eject_abort_deadline_s(40.0) == 60.0

    async def test_mid_range_profile_gets_the_fraction(self):
        assert remote.eject_abort_deadline_s(160.0) == 200.0

    async def test_long_profile_is_capped_at_sixty_seconds(self):
        # A ~7 min multi-pass eject must not inherit minutes of scraping margin, which
        # a pure ×1.5 factor (600 s → 900 s) would hand it.
        assert remote.eject_abort_deadline_s(400.0) == 460.0

    async def test_production_calibration(self):
        # 11 measured production ejects ran 80-83 s against this 83 s estimate; the
        # sweep phase starts ~45 s in, so 103.75 s pre-empts any pre-sweep stall
        # longer than 59 s (the incident's was ~97 s).
        assert remote.eject_abort_deadline_s(83.0) == 103.75


class TestRuntimeWatchdogArming:
    """The START echo is the only moment a deadline can be measured from."""

    async def test_mark_started_stamps_once_and_arms_one_watchdog(self):
        pid = 90001
        remote.register_pending_eject(pid, remote.PendingEject("production", 1, 5, expected_runtime_s=82.0))
        try:
            assert remote.peek_pending_eject(pid).started_at is None
            remote.mark_pending_eject_started(pid)
            first = remote.peek_pending_eject(pid).started_at
            assert first is not None
            assert pid in remote._runtime_watchdogs
            armed_task = remote._runtime_watchdogs[pid]
            # A duplicate/replayed start echo must NOT restart the clock — that would
            # shorten a measured runtime back under the deadline — nor double-arm.
            remote.mark_pending_eject_started(pid)
            assert remote.peek_pending_eject(pid).started_at == first
            assert remote._runtime_watchdogs[pid] is armed_task
            # The rest of the identity survives the re-registration.
            pending = remote.peek_pending_eject(pid)
            assert (pending.purpose, pending.run_id, pending.queue_item_id) == ("production", 1, 5)
            assert pending.expected_runtime_s == 82.0
            assert pending.runtime_exceeded_at is None
        finally:
            await remote.cancel_runtime_watchdog(pid)
            remote.pop_pending_eject(pid)

    async def test_mark_started_on_absent_pending_is_a_noop(self):
        remote.mark_pending_eject_started(90002)  # must not raise
        assert remote.peek_pending_eject(90002) is None
        assert 90002 not in remote._runtime_watchdogs

    async def test_pending_without_an_estimate_never_arms(self):
        # A rehydrated post-restart pending carries no estimate: there is nothing to
        # judge against, and the startup reconciler already owns those gates.
        pid = 90003
        remote.register_pending_eject(pid, remote.PendingEject("production", 1, 5))
        try:
            remote.mark_pending_eject_started(pid)
            assert remote.peek_pending_eject(pid).started_at is not None  # still stamped
            assert pid not in remote._runtime_watchdogs
        finally:
            remote.pop_pending_eject(pid)


class TestRuntimeWatchdogFires:
    """Past the deadline the watchdog stops the machine MID-JOB — the whole point of
    replacing the terminal-time judgement: the stall accrues in the drop/return phase,
    so stopping now happens before the sweep can scrape the plate."""

    @staticmethod
    def _run(pid: int, armed: remote.PendingEject, sleep: _FakeSleep):
        """Spawn the watchdog exactly as the arming path does, so the test exercises
        the real registry lifecycle (including the ``finally`` deregistration)."""
        from backend.app.core.tasks import spawn_background_task

        task = spawn_background_task(remote._runtime_watchdog(pid, armed, sleep=sleep), name=f"wd-test-{pid}")
        remote._runtime_watchdogs[pid] = task
        return task

    async def test_marks_before_stopping_then_notifies_escalates_and_deregisters(self):
        from backend.app.services.eject import monitor as monitor_mod

        pid = 90010
        armed = _armed()
        remote.register_pending_eject(pid, armed)
        sleep = _FakeSleep()
        mark_at_stop: list[object] = []

        def _stop(printer_id: int) -> bool:
            # Captured INSIDE the stop: a terminal racing the stop must already find
            # the mark set, or it would release the gate onto an unverified plate.
            mark_at_stop.append(remote.peek_pending_eject(printer_id).runtime_exceeded_at)
            return True

        try:
            with (
                patch.object(printer_manager, "stop_print", MagicMock(side_effect=_stop)) as stop,
                patch.object(monitor_mod, "_default_notify_plate_not_empty", new_callable=AsyncMock) as notify,
                patch.object(monitor_mod.eject_cooldown_monitor, "start_escalation_only_watch") as escalate,
            ):
                await self._run(pid, armed, sleep)
            assert sleep.delays == [103.75]  # the 83 s estimate's deadline
            stop.assert_called_once_with(pid)
            assert mark_at_stop and mark_at_stop[0] is not None
            assert remote.peek_pending_eject(pid).runtime_exceeded_at is not None
            notify.assert_awaited_once()
            detail = notify.await_args.kwargs["source_detail"]
            assert "STOPPED" in detail and "104" in detail and "83" in detail
            escalate.assert_called_once_with(pid)
            assert pid not in remote._runtime_watchdogs  # deregistered on exit
        finally:
            remote.pop_pending_eject(pid)

    async def test_undelivered_stop_is_retried_exactly_once(self):
        # stop_print returning False means the command was NOT delivered (no live MQTT
        # session). One retry, then proceed regardless — the mark alone already keeps
        # the gate closed, and the operator is paged either way.
        from backend.app.services.eject import monitor as monitor_mod

        pid = 90011
        armed = _armed()
        remote.register_pending_eject(pid, armed)
        sleep = _FakeSleep()
        try:
            with (
                patch.object(printer_manager, "stop_print", MagicMock(return_value=False)) as stop,
                patch.object(monitor_mod, "_default_notify_plate_not_empty", new_callable=AsyncMock) as notify,
                patch.object(monitor_mod.eject_cooldown_monitor, "start_escalation_only_watch") as escalate,
            ):
                await self._run(pid, armed, sleep)
            assert stop.call_count == 2
            assert sleep.delays == [103.75, remote._STOP_RETRY_DELAY_S]
            assert remote.peek_pending_eject(pid).runtime_exceeded_at is not None
            notify.assert_awaited_once()
            escalate.assert_called_once_with(pid)
        finally:
            remote.pop_pending_eject(pid)

    async def test_notify_failure_never_kills_the_escalation(self):
        from backend.app.services.eject import monitor as monitor_mod

        pid = 90012
        armed = _armed()
        remote.register_pending_eject(pid, armed)
        try:
            with (
                patch.object(printer_manager, "stop_print", MagicMock(return_value=True)),
                patch.object(
                    monitor_mod,
                    "_default_notify_plate_not_empty",
                    new_callable=AsyncMock,
                    side_effect=RuntimeError("smtp down"),
                ),
                patch.object(monitor_mod.eject_cooldown_monitor, "start_escalation_only_watch") as escalate,
            ):
                await self._run(pid, armed, _FakeSleep())
            escalate.assert_called_once_with(pid)
            assert pid not in remote._runtime_watchdogs
        finally:
            remote.pop_pending_eject(pid)

    async def test_superseded_pending_stands_the_watchdog_down(self):
        # The eject this task was armed for resolved while it slept and a NEW one was
        # dispatched onto the same printer. Stopping now would abort a healthy job.
        from backend.app.services.eject import monitor as monitor_mod

        pid = 90013
        armed = _armed(5)
        successor = _armed(6)
        remote.register_pending_eject(pid, successor)
        try:
            with (
                patch.object(printer_manager, "stop_print", MagicMock(return_value=True)) as stop,
                patch.object(monitor_mod, "_default_notify_plate_not_empty", new_callable=AsyncMock) as notify,
                patch.object(monitor_mod.eject_cooldown_monitor, "start_escalation_only_watch") as escalate,
            ):
                await self._run(pid, armed, _FakeSleep())
            stop.assert_not_called()
            notify.assert_not_awaited()
            escalate.assert_not_called()
            assert remote.peek_pending_eject(pid).runtime_exceeded_at is None  # successor unmarked
            assert pid not in remote._runtime_watchdogs
        finally:
            remote.pop_pending_eject(pid)

    async def test_resolved_pending_stands_the_watchdog_down(self):
        pid = 90014
        with patch.object(printer_manager, "stop_print", MagicMock(return_value=True)) as stop:
            await self._run(pid, _armed(), _FakeSleep())
        stop.assert_not_called()
        assert pid not in remote._runtime_watchdogs


class TestRuntimeWatchdogCancellation:
    """Cancellation is the NORMAL exit — a nominal eject never reaches the fire path."""

    async def test_clear_pending_eject_cancels_and_removes_the_watchdog(self, db_session):
        pid = 90020
        remote.register_pending_eject(pid, remote.PendingEject("production", 1, 5, expected_runtime_s=82.0))
        remote.mark_pending_eject_started(pid)
        task = remote._runtime_watchdogs[pid]
        await remote.clear_pending_eject(db_session, pid)
        assert task.cancelled()
        assert pid not in remote._runtime_watchdogs
        assert remote.peek_pending_eject(pid) is None

    async def test_registering_a_new_eject_cancels_a_stale_watchdog(self):
        pid = 90021
        remote.register_pending_eject(pid, remote.PendingEject("production", 1, 5, expected_runtime_s=82.0))
        remote.mark_pending_eject_started(pid)
        stale = remote._runtime_watchdogs[pid]
        try:
            # A stale watchdog would judge the NEW sweep against the OLD deadline.
            remote.register_pending_eject(pid, remote.PendingEject("production", 1, 6, expected_runtime_s=82.0))
            assert pid not in remote._runtime_watchdogs
            with pytest.raises(asyncio.CancelledError):
                await stale
        finally:
            remote.pop_pending_eject(pid)

    async def test_cancelling_when_nothing_is_armed_is_a_noop(self):
        await remote.cancel_runtime_watchdog(90022)  # must not raise


class TestKillDecisionTelemetry:
    """The kill line must say WHERE the eject was when it was stopped.

    The 2026-08-14 009-H2S eject ran 106 s against an expected 84 s and was stopped
    with nothing in the record to attribute the +21 s to the bed-drop or the sweep.
    The executing G-code line number falls inside exactly one of those phases, so
    sampling it at the kill instant is what makes the next one diagnosable. Log-only:
    no assertion here may become a stop condition."""

    @staticmethod
    def _client(line_number, percent):
        return SimpleNamespace(state=SimpleNamespace(mc_print_line_number=line_number, progress=percent))

    async def _fire(self, pid: int, caplog, client) -> str:
        """Drive the fire path with ``client`` as the live session; return the kill line."""
        from backend.app.services.eject import monitor as monitor_mod

        armed = _armed()
        remote.register_pending_eject(pid, armed)
        try:
            with (
                patch.object(printer_manager, "get_client", MagicMock(return_value=client)),
                patch.object(printer_manager, "stop_print", MagicMock(return_value=True)),
                patch.object(monitor_mod, "_default_notify_plate_not_empty", new_callable=AsyncMock),
                patch.object(monitor_mod.eject_cooldown_monitor, "start_escalation_only_watch"),
                caplog.at_level(logging.WARNING, logger="backend.app.services.eject.remote"),
            ):
                await TestRuntimeWatchdogFires._run(pid, armed, _FakeSleep())
        finally:
            remote.pop_pending_eject(pid)
        return next(r.getMessage() for r in caplog.records if "still running at" in r.getMessage())

    async def test_kill_line_carries_the_line_number_and_percent(self, caplog):
        message = await self._fire(90030, caplog, self._client(41207, 57.0))
        assert "gcode line 41207" in message
        assert "57.0% done" in message
        # The runtimes it has always carried must survive the added fields.
        assert "still running at 104s" in message and "expected 83s" in message

    async def test_kill_line_tolerates_a_printer_with_no_live_session(self, caplog):
        # A disconnected printer is exactly when a stop goes undelivered — the kill
        # line still has to render rather than blow up inside the watchdog.
        message = await self._fire(90031, caplog, None)
        assert "gcode line None" in message and "None% done" in message

    async def test_kill_line_tolerates_firmware_that_never_publishes_the_line_number(self, caplog):
        # mc_print_line_number is UNVERIFIED on the H2S wire: absent field, not absent
        # printer. Percent must still land so the breadcrumb is not all-or-nothing.
        message = await self._fire(90032, caplog, self._client(None, 12.0))
        assert "gcode line None" in message and "12.0% done" in message

    async def test_telemetry_reader_tolerates_a_stateless_client(self):
        with patch.object(printer_manager, "get_client", MagicMock(return_value=SimpleNamespace())):
            assert remote._live_phase_telemetry(90033) == (None, None, None)

    async def test_telemetry_reader_carries_the_progress_recency_stamp(self):
        # The percent field always holds SOME value; only its wire stamp says whether
        # that value describes the phase running now (see PrinterState.progress_wire_at).
        state = SimpleNamespace(mc_print_line_number=7, progress=42.0, progress_wire_at=1234.5)
        with patch.object(printer_manager, "get_client", MagicMock(return_value=SimpleNamespace(state=state))):
            assert remote._live_phase_telemetry(90034) == (7, 42.0, 1234.5)


class _FakeClock:
    """The ONE time source a polling-watchdog test drives.

    The watchdog's sleeps and its clock must come from the same source, so this supplies
    both: ``sleep`` records the delay, advances ``now`` by it and yields to the event
    loop so the test can interleave (cancelling mid-run, for one). No wall-clock time
    passes, so a 104 s deadline sequence runs in microseconds."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.delays: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        self.now += delay
        await asyncio.sleep(0)


class _ProgressFeed:
    """Scripted ``mc_percent`` samples, one consumed per telemetry read.

    Each entry is ``(percent, wire_age_s)``: what the live session reports and how old
    that push is when read. ``wire_age_s=None`` means the stamp PREDATES the arming — a
    value held over from before this eject, which the watchdog must treat as no evidence
    at all. The last entry repeats once exhausted, so a test scripts only the interesting
    part of the timeline."""

    def __init__(self, clock: _FakeClock, samples: list[tuple[float, float | None]]) -> None:
        self._clock = clock
        self._samples = list(samples)
        self.reads = 0

    def __call__(self, printer_id: int) -> SimpleNamespace:
        percent, age = self._samples[min(self.reads, len(self._samples) - 1)]
        self.reads += 1
        wire_at = 0.0 if age is None else self._clock.now - age
        return SimpleNamespace(
            state=SimpleNamespace(mc_print_line_number=None, progress=percent, progress_wire_at=wire_at)
        )


def _armed_with_drop(pid_item: int = 5, *, drop_span_s: float = 30.0, **kw) -> remote.PendingEject:
    """A started production pending carrying a bed-drop budget (arms the edge lane).

    83 s expected → a 103.75 s whole-job deadline; a 30 s drop span → an 8 s margin (the
    floor) → the drop deadline lands 38 s after the observed P5 edge."""
    kw.setdefault("expected_runtime_s", 83.0)
    kw.setdefault("started_at", datetime.now(timezone.utc))
    return remote.PendingEject("production", 1, pid_item, drop_span_s=drop_span_s, **kw)


@contextlib.contextmanager
def _watchdog_env(feed: _ProgressFeed, *, stop_delivered: bool = True):
    """Patch every collaborator the fire path touches; yields the mocks."""
    from backend.app.services.eject import monitor as monitor_mod

    with (
        patch.object(printer_manager, "get_client", MagicMock(side_effect=feed)),
        patch.object(printer_manager, "stop_print", MagicMock(return_value=stop_delivered)) as stop,
        patch.object(printer_manager, "request_evidence_pushall", MagicMock(return_value=True)) as pushall,
        patch.object(monitor_mod, "_default_notify_plate_not_empty", new_callable=AsyncMock) as notify,
        patch.object(monitor_mod.eject_cooldown_monitor, "start_escalation_only_watch") as escalate,
    ):
        yield SimpleNamespace(stop=stop, pushall=pushall, notify=notify, escalate=escalate)


def _spawn_watchdog(pid: int, armed: remote.PendingEject, clock: _FakeClock) -> asyncio.Task:
    """Spawn the watchdog exactly as the arming path does (real registry lifecycle)."""
    from backend.app.core.tasks import spawn_background_task

    task = spawn_background_task(
        remote._runtime_watchdog(pid, armed, sleep=clock.sleep, clock=clock), name=f"wd-edge-{pid}"
    )
    remote._runtime_watchdogs[pid] = task
    return task


def _kill_line(caplog) -> str:
    return next(r.getMessage() for r in caplog.records if "still running at" in r.getMessage())


class TestEjectMarginMath:
    """Two margins, one clamp. The drop margin is tighter on both ends because it is
    measured from an OBSERVED beacon edge, so it pays for none of the upload / spin-up /
    start-echo variance the whole-job margin has to absorb."""

    async def test_abort_margin_keeps_the_twenty_and_sixty_second_clamps(self):
        assert remote._abort_margin_s(40.0) == 20.0  # floor
        assert remote._abort_margin_s(160.0) == 40.0  # fraction
        assert remote._abort_margin_s(400.0) == 60.0  # cap

    async def test_drop_margin_floors_at_eight_and_caps_at_twenty(self):
        # The 8 s floor covers the estimator's ~±3 s, the ~1 Hz push cadence and the 2 s
        # poll — below that a healthy drop could be killed by sampling alone.
        assert remote._drop_margin_s(4.0) == 8.0
        assert remote._drop_margin_s(30.0) == 8.0  # 7.5 → floored
        assert remote._drop_margin_s(48.0) == 12.0
        assert remote._drop_margin_s(200.0) == 20.0

    async def test_deadline_is_the_estimate_plus_its_margin(self):
        assert remote.eject_abort_deadline_s(83.0) == 83.0 + remote._abort_margin_s(83.0)


class TestRuntimeWatchdogPhaseEdges:
    """The pre-sweep guarantee (2026-08-15 009-H2S bed-drop stalls).

    The whole-job deadline can only catch a stall that overruns the ENTIRE eject (~59 s
    on the production profile); a shorter one lets the sweep run on a bed that lost Z
    steps. mc_percent is M73-driven, so the block's phase beacons make the bed-drop phase
    boundable on its own — and a percent below the sweep beacon PROVES the sweep has not
    started, which is what makes stopping there safe."""

    async def test_drop_overrun_with_the_sweep_unreached_stops_the_job(self, caplog):
        # Percent parked at the lifted beacon: the printer is still executing drop-phase
        # lines 42 s in, against a 30 s budget + 8 s margin. This is the incident shape.
        pid = 90040
        armed = _armed_with_drop()
        remote.register_pending_eject(pid, armed)
        clock = _FakeClock()
        feed = _ProgressFeed(clock, [(5.0, 0.0)])
        try:
            with (
                _watchdog_env(feed) as env,
                caplog.at_level(logging.WARNING, logger="backend.app.services.eject.remote"),
            ):
                await _spawn_watchdog(pid, armed, clock)
            env.stop.assert_called_once_with(pid)
            assert remote.peek_pending_eject(pid).runtime_exceeded_at is not None
            message = _kill_line(caplog)
            assert "stage=drop" in message and "stage=drop_late" not in message
            detail = env.notify.await_args.kwargs["source_detail"]
            assert "bed-drop" in detail and "under the heatbed" in detail
            env.escalate.assert_called_once_with(pid)
            assert pid not in remote._runtime_watchdogs
            # Killed on the DROP deadline (t5 + 38 s), far short of the 103.75 s whole-job one.
            assert clock.now - 1000.0 == pytest.approx(42.0)
        finally:
            remote.pop_pending_eject(pid)

    async def test_sweep_beacon_arriving_after_the_drop_deadline_still_stops(self, caplog):
        # The drop verifiably ran long. The sweep opens with rear positioning at lift
        # height, so the stop still lands before the toolhead can reach the plate.
        pid = 90041
        armed = _armed_with_drop()
        remote.register_pending_eject(pid, armed)
        clock = _FakeClock()
        feed = _ProgressFeed(clock, [(5.0, 0.0)] * 20 + [(50.0, 0.0)])
        try:
            with (
                _watchdog_env(feed) as env,
                caplog.at_level(logging.WARNING, logger="backend.app.services.eject.remote"),
            ):
                await _spawn_watchdog(pid, armed, clock)
            env.stop.assert_called_once_with(pid)
            assert "stage=drop_late" in _kill_line(caplog)
            assert "bed-drop" in env.notify.await_args.kwargs["source_detail"]
            assert remote.peek_pending_eject(pid).runtime_exceeded_at is not None
        finally:
            remote.pop_pending_eject(pid)

    async def test_healthy_phase_sequence_never_kills_and_cancels_on_resolution(self, db_session, caplog):
        # 0 → 5 → 50 → 75: the drop cleared inside its budget, so no phase rule may fire.
        # The eject's terminal then cancels the task, which is the NORMAL exit.
        pid = 90042
        armed = _armed_with_drop()
        remote.register_pending_eject(pid, armed)
        clock = _FakeClock()
        feed = _ProgressFeed(clock, [(0.0, 0.0), (5.0, 0.0), (50.0, 0.0), (75.0, 0.0)])
        with (
            _watchdog_env(feed) as env,
            caplog.at_level(logging.INFO, logger="backend.app.services.eject.remote"),
        ):
            task = _spawn_watchdog(pid, armed, clock)
            for _ in range(200):
                if feed.reads >= 4:
                    break
                await asyncio.sleep(0)
            await remote.clear_pending_eject(db_session, pid)
            env.stop.assert_not_called()
            env.notify.assert_not_awaited()
        # LIVENESS: the lane really ran the sequence — a silent watchdog would satisfy
        # every "did not fire" assertion above without ever observing a phase edge.
        assert feed.reads >= 4
        assert [r for r in caplog.records if "bed-drop phase cleared" in r.getMessage()]
        assert task.cancelled()
        assert pid not in remote._runtime_watchdogs
        assert remote.peek_pending_eject(pid) is None

    async def test_beacons_never_reflected_falls_back_to_the_whole_job_deadline(self, caplog):
        # mc_percent stuck at 0 while fresh: the firmware is publishing, but the beacons
        # are not landing in it. The lane must degrade to the original rule, exactly once,
        # and the whole-job deadline must still fire.
        pid = 90043
        armed = _armed_with_drop()
        remote.register_pending_eject(pid, armed)
        clock = _FakeClock()
        feed = _ProgressFeed(clock, [(0.0, 0.0)])
        try:
            with (
                _watchdog_env(feed) as env,
                caplog.at_level(logging.WARNING, logger="backend.app.services.eject.remote"),
            ):
                await _spawn_watchdog(pid, armed, clock)
            fallbacks = [r for r in caplog.records if "phase beacons not reflected" in r.getMessage()]
            assert len(fallbacks) == 1
            env.stop.assert_called_once_with(pid)
            assert "stage=total" in _kill_line(caplog)
            assert clock.now - 1000.0 >= remote.eject_abort_deadline_s(83.0)
        finally:
            remote.pop_pending_eject(pid)

    async def test_stale_evidence_never_kills_and_asks_once_for_a_fresh_report(self, caplog):
        # One fresh sample opens the drop window, then the link goes quiet: every later
        # sample is stamped before this eject even armed. Stale evidence advances nothing
        # and justifies no stop — the lane asks for ONE pushall and leaves the whole-job
        # deadline as the backstop.
        pid = 90044
        armed = _armed_with_drop()
        remote.register_pending_eject(pid, armed)
        clock = _FakeClock()
        feed = _ProgressFeed(clock, [(5.0, 0.0), (5.0, None)])
        try:
            with (
                _watchdog_env(feed) as env,
                caplog.at_level(logging.WARNING, logger="backend.app.services.eject.remote"),
            ):
                await _spawn_watchdog(pid, armed, clock)
            env.pushall.assert_called_once_with(pid, remote._EVIDENCE_REASON)
            env.stop.assert_called_once_with(pid)
            assert "stage=total" in _kill_line(caplog)  # NOT a drop kill
            assert clock.now - 1000.0 >= remote.eject_abort_deadline_s(83.0)
        finally:
            remote.pop_pending_eject(pid)

    async def test_aged_out_sample_is_not_evidence_either(self, caplog):
        # Stamped after arming but far older than the freshness window: a link that went
        # silent mid-eject is not a phase report, so the drop rule must not fire on it.
        pid = 90045
        armed = _armed_with_drop()
        remote.register_pending_eject(pid, armed)
        clock = _FakeClock()
        feed = _ProgressFeed(clock, [(5.0, 0.0), (5.0, remote._PROGRESS_FRESH_S + 30.0)])
        try:
            with (
                _watchdog_env(feed) as env,
                caplog.at_level(logging.WARNING, logger="backend.app.services.eject.remote"),
            ):
                await _spawn_watchdog(pid, armed, clock)
            assert "stage=total" in _kill_line(caplog)
            env.stop.assert_called_once_with(pid)
        finally:
            remote.pop_pending_eject(pid)

    async def test_superseded_pending_stands_the_edge_lane_down(self):
        # The eject this task was armed for resolved and a NEW one took the printer.
        pid = 90046
        armed = _armed_with_drop(5)
        remote.register_pending_eject(pid, _armed_with_drop(6))
        clock = _FakeClock()
        feed = _ProgressFeed(clock, [(5.0, 0.0)])
        try:
            with _watchdog_env(feed) as env:
                await _spawn_watchdog(pid, armed, clock)
            env.stop.assert_not_called()
            env.notify.assert_not_awaited()
            assert remote.peek_pending_eject(pid).runtime_exceeded_at is None
            assert pid not in remote._runtime_watchdogs
        finally:
            remote.pop_pending_eject(pid)


class TestRuntimeWatchdogDeadlineOnlyFallback:
    """Without a drop budget the watchdog is EXACTLY what it was before the edge lane:
    one sleep to the whole-job deadline, then the same stop. A rehydrated post-restart
    pending and every drop-less profile land here."""

    async def test_no_drop_span_sleeps_straight_to_the_total_deadline(self, caplog):
        pid = 90050
        armed = _armed()  # drop_span_s defaults to None
        remote.register_pending_eject(pid, armed)
        clock = _FakeClock()
        feed = _ProgressFeed(clock, [(5.0, 0.0)])
        try:
            with (
                _watchdog_env(feed) as env,
                caplog.at_level(logging.WARNING, logger="backend.app.services.eject.remote"),
            ):
                await _spawn_watchdog(pid, armed, clock)
            assert clock.delays == [103.75]  # one sleep, no polling
            env.stop.assert_called_once_with(pid)
            assert "stage=total" in _kill_line(caplog)
            # The pre-edge-lane operator wording is untouched for this stage.
            detail = env.notify.await_args.kwargs["source_detail"]
            assert "STOPPED mid-job" in detail and "104" in detail and "83" in detail
        finally:
            remote.pop_pending_eject(pid)

    async def test_a_drop_budget_that_cannot_beat_the_total_deadline_polls_nothing(self):
        # 100 s span + 20 s margin = 120 s, past the 103.75 s whole-job deadline: the
        # phase rule could never fire first, so arming it would only add failure modes.
        pid = 90051
        armed = _armed_with_drop(drop_span_s=100.0)
        remote.register_pending_eject(pid, armed)
        clock = _FakeClock()
        feed = _ProgressFeed(clock, [(5.0, 0.0)])
        try:
            with _watchdog_env(feed) as env:
                await _spawn_watchdog(pid, armed, clock)
            assert clock.delays == [103.75]
            env.stop.assert_called_once_with(pid)
        finally:
            remote.pop_pending_eject(pid)
