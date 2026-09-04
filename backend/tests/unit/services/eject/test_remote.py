"""Tests for the shared part-present eject dispatcher (eject.remote).

Since the 2026-08-30 cut-over this module holds NO eject state of its own: the one
pending-eject record per printer lives in the ``plate_occupancy`` authority. Every test
therefore SEEDS through the authority's transitions — ``hydrate_plate`` + ``claim_for_eject``
for a LIVE record (the shape a dispatch leaves behind), ``hydrate_eject`` for one rebuilt
at startup — and ASSERTS through its queries (``pending_eject_view`` / ``eject_identity``).

What stays here are the two DISPATCHERS and the two TIMERS that bound a dispatched sweep:
the START deadline (did the printer ever begin it?) and the RUNTIME watchdog (is it still
running long past its estimate?).
"""

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
from backend.app.services.plate_occupancy import (
    CooldownEject,
    EscalationOnly,
    Evidence,
    OccupancyView,
    PendingEject,
    plate_occupancy,
)
from backend.app.services.printer_manager import printer_manager

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _reset_occupancy():
    """Every test starts on an empty fleet and leaves no timer armed.

    ``reset_for_tests`` also un-wires every injected callable, so a transition driven
    from a test fans out into no persist, no broadcast, no kick and no watch."""
    plate_occupancy.reset_for_tests()
    yield
    for printer_id in list(remote._start_deadlines) + list(remote._runtime_watchdogs):
        remote.cancel_eject_timers(printer_id)
    plate_occupancy.reset_for_tests()


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


def _gate(printer_id: int, *, source: str | None = "SUB-GATE", policy=None) -> None:
    """Raise the plate gate the way a restart rebuild does — no persist, no kick.

    An eject is refused ``not_occupied`` on a clear plate, so every dispatch test needs
    a deposit on the plate before the sweep it dispatches can be legal at all."""
    plate_occupancy.hydrate_plate(printer_id, source, policy or EscalationOnly())


def _claim(printer_id: int, pending: PendingEject) -> PendingEject:
    """Seed a LIVE claimed eject on an occupied plate — what a dispatch leaves behind."""
    if not plate_occupancy.is_plate_occupied(printer_id):
        _gate(printer_id)
    assert plate_occupancy.claim_for_eject(printer_id, pending, Evidence()) is None
    claimed = plate_occupancy.pending_eject_view(printer_id)
    assert claimed is not None
    return claimed


def _ftp_patches(*, connected=True, upload=True, started=True):
    return (
        patch.object(printer_manager, "is_connected", return_value=connected),
        patch("backend.app.services.bambu_ftp.get_ftp_retry_settings", AsyncMock(return_value=(False, 0, 0, 30))),
        patch("backend.app.services.bambu_ftp.upload_file_async", AsyncMock(return_value=upload)),
    )


class TestVocabularyReExport:
    """The eject lane keeps ONE import site for the vocabulary it dispatches with, even
    though the definitions moved into the authority."""

    def test_pending_eject_is_the_authoritys_type(self):
        assert remote.PendingEject is PendingEject


class TestDispatchPartPresentEject:
    async def test_success_claims_the_printer_and_starts_all_off(self, db_session):
        source = _make_source_3mf()
        try:
            printer, item = await _seed(db_session, source)
            _gate(printer.id)
            start = MagicMock(return_value=True)
            c1, c2, c3 = _ftp_patches()
            with c1, c2, c3, patch.object(printer_manager, "start_print", start):
                await remote.dispatch_part_present_eject(
                    db_session, printer_id=printer.id, queue_item_id=item.id, purpose="production", run_id=555
                )
            # The printer is CLAIMED with the typed identity...
            pending = plate_occupancy.pending_eject_view(printer.id)
            assert (pending.purpose, pending.run_id, pending.queue_item_id) == ("production", 555, item.id)
            # ...carrying the build's runtime estimate for the in-flight watchdog,
            # and NOT yet started (that stamp lands on the printer's start echo).
            assert pending.expected_runtime_s is not None
            assert pending.expected_runtime_s > 0
            assert pending.started_at is None
            # A claim is a LIVE dispatch by definition: stamped now, never hydrated.
            assert pending.hydrated is False
            assert pending.dispatched_at is not None
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
            source.unlink(missing_ok=True)

    async def test_success_leaves_an_identity_and_an_armed_start_deadline(self, db_session):
        """The firmware silently ignores a ``project_file`` sent while it is busy, so a
        dispatched eject that nothing bounds can never be observed to have failed — the
        2026-08-30 "8 consecutive eject 409s" shape. Every accepted dispatch therefore
        ends with BOTH the authority's identity and the deadline that retires it."""
        source = _make_source_3mf()
        try:
            printer, item = await _seed(db_session, source)
            _gate(printer.id)
            c1, c2, c3 = _ftp_patches()
            with c1, c2, c3, patch.object(printer_manager, "start_print", MagicMock(return_value=True)):
                await remote.dispatch_part_present_eject(
                    db_session, printer_id=printer.id, queue_item_id=item.id, purpose="production", run_id=None
                )
            identity = plate_occupancy.eject_identity(printer.id)
            assert identity is not None
            assert (identity.purpose, identity.queue_item_id) == ("production", item.id)
            assert identity.started_at is None
            assert printer.id in remote._start_deadlines
            assert not remote._start_deadlines[printer.id].done()
        finally:
            source.unlink(missing_ok=True)

    async def test_not_connected_raises_409_and_claims_nothing(self, db_session):
        source = _make_source_3mf()
        try:
            printer, item = await _seed(db_session, source)
            _gate(printer.id)
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
            assert "not connected" in str(exc.value).lower()
            assert plate_occupancy.pending_eject_view(printer.id) is None
        finally:
            source.unlink(missing_ok=True)

    async def test_upload_failure_raises_502_and_claims_nothing(self, db_session):
        source = _make_source_3mf()
        try:
            printer, item = await _seed(db_session, source)
            _gate(printer.id)
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
            assert plate_occupancy.pending_eject_view(printer.id) is None
            assert printer.id not in remote._start_deadlines
        finally:
            source.unlink(missing_ok=True)

    async def test_start_print_failure_raises_502_and_claims_nothing(self, db_session):
        source = _make_source_3mf()
        try:
            printer, item = await _seed(db_session, source)
            _gate(printer.id)
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
            # Nothing is claimed unless start_print was accepted.
            assert plate_occupancy.pending_eject_view(printer.id) is None
            assert printer.id not in remote._start_deadlines
        finally:
            source.unlink(missing_ok=True)


class TestDispatchRefusesBeforeItBuilds:
    """The occupancy gate runs BEFORE the build, and its refusal token rides the error.

    Building and uploading an eject costs seconds of FTPS work a refused sweep must not
    spend, so ``ejectable`` is asked first; the claim afterwards re-runs the identical
    gate, which is what still catches a race that opened during the upload. The
    ``EjectDispatchError.code`` carries the authority's own refusal token verbatim, so
    the operator is told WHICH condition held — one vocabulary from the state machine to
    the dialog."""

    async def test_job_active_refuses_without_building_or_uploading(self, db_session):
        source = _make_source_3mf()
        try:
            printer, item = await _seed(db_session, source)
            _gate(printer.id)
            build = AsyncMock()
            upload = AsyncMock(return_value=True)
            with (
                patch.object(printer_manager, "is_connected", return_value=True),
                patch.object(printer_manager, "get_status", MagicMock(return_value=SimpleNamespace(state="RUNNING"))),
                patch.object(remote, "build_part_present_eject_file", build),
                patch("backend.app.services.bambu_ftp.upload_file_async", upload),
                patch.object(printer_manager, "start_print", MagicMock(return_value=True)) as start,
                pytest.raises(remote.EjectDispatchError) as exc,
            ):
                await remote.dispatch_part_present_eject(
                    db_session, printer_id=printer.id, queue_item_id=item.id, purpose="production", run_id=1
                )
            assert exc.value.status_code == 409
            assert exc.value.code == "job_active"
            build.assert_not_awaited()
            upload.assert_not_awaited()
            start.assert_not_called()
            assert plate_occupancy.pending_eject_view(printer.id) is None
        finally:
            source.unlink(missing_ok=True)

    async def test_clear_plate_refuses_not_occupied(self, db_session):
        # No gate raised: there is nothing on the plate to sweep off it.
        source = _make_source_3mf()
        try:
            printer, item = await _seed(db_session, source)
            build = AsyncMock()
            c1, c2, c3 = _ftp_patches()
            with (
                c1,
                c2,
                c3,
                patch.object(remote, "build_part_present_eject_file", build),
                patch.object(printer_manager, "start_print", MagicMock(return_value=True)),
                pytest.raises(remote.EjectDispatchError) as exc,
            ):
                await remote.dispatch_part_present_eject(
                    db_session, printer_id=printer.id, queue_item_id=item.id, purpose="production", run_id=1
                )
            assert exc.value.status_code == 409
            assert exc.value.code == "not_occupied"
            build.assert_not_awaited()
        finally:
            source.unlink(missing_ok=True)

    async def test_a_live_eject_refuses_a_second_one(self, db_session):
        source = _make_source_3mf()
        try:
            printer, item = await _seed(db_session, source)
            _claim(printer.id, PendingEject("production", 1, item.id, expected_runtime_s=83.0))
            build = AsyncMock()
            c1, c2, c3 = _ftp_patches()
            with (
                c1,
                c2,
                c3,
                patch.object(remote, "build_part_present_eject_file", build),
                patch.object(printer_manager, "start_print", MagicMock(return_value=True)),
                pytest.raises(remote.EjectDispatchError) as exc,
            ):
                await remote.dispatch_part_present_eject(
                    db_session, printer_id=printer.id, queue_item_id=item.id, purpose="production", run_id=1
                )
            assert exc.value.code == "eject_in_flight"
            build.assert_not_awaited()
        finally:
            source.unlink(missing_ok=True)

    async def test_the_foreign_dispatcher_refuses_on_the_same_gate(self, db_session):
        source = _make_source_3mf()
        try:
            printer = Printer(name="FR", serial_number="FR1", ip_address="1.2.3.4", access_code="x", model="H2S")
            db_session.add(printer)
            await db_session.flush()
            prof = EjectProfile(name="fr-ep")
            db_session.add(prof)
            await db_session.commit()
            await db_session.refresh(printer)
            await db_session.refresh(prof)
            build = AsyncMock()
            c1, c2, c3 = _ftp_patches()
            with (
                c1,
                c2,
                c3,
                patch.object(remote, "build_part_present_eject_file", build),
                pytest.raises(remote.EjectDispatchError) as exc,
            ):
                await remote.dispatch_foreign_eject(
                    db_session, printer_id=printer.id, profile_id=prof.id, source_path=source, plate_id=1
                )
            assert exc.value.code == "not_occupied"
            build.assert_not_awaited()
        finally:
            source.unlink(missing_ok=True)


class TestStandingFaultDoesNotGateEjects:
    """SCOPE pin for the 2026-08-29 dispatch gate: ``_is_printer_idle`` refuses PRINT
    dispatch onto a printer whose wire carries an actionable AMS fault. An eject is
    filament-less and motion-only, and gating it the same way would be a deadlock —
    the plate stays occupied, so the fault's own printer can never be freed by the
    sweep that would free it. This dispatcher is the ONE eject path (production, FA
    and manual/foreign all funnel through it)."""

    @staticmethod
    def _ptfe_breakage_hms():
        """``0700_0006`` — PTFE tube breakage, PHYSICAL class, the code that held
        001-H2S. Actionable, so it blocks print dispatch."""
        from backend.app.services.bambu_mqtt import HMSError

        attr = 0x07000000 | ((0x20 + 3) << 8)
        return HMSError(code="0x20006", attr=attr, module=7, severity=2, full_code=f"{attr:08X}00020006")

    async def test_an_eject_still_dispatches_with_an_actionable_fault_standing(self, db_session, monkeypatch):
        source = _make_source_3mf()
        try:
            printer, item = await _seed(db_session, source)
            # The print-dispatch gate would refuse this printer outright — and with the
            # plate still CLEAR, the standing fault is the only thing that can refuse it.
            from backend.app.services.print_scheduler import scheduler
            from backend.app.services.printer_manager import printer_manager as pm

            faulted = SimpleNamespace(state="IDLE", hms_errors=[self._ptfe_breakage_hms()], subtask_id="t")
            monkeypatch.setattr(pm, "get_status", lambda _pid: faulted)
            monkeypatch.setattr(pm, "is_quarantined", lambda _pid: False)
            monkeypatch.setattr(pm, "is_model_mismatch", lambda _pid: False)
            monkeypatch.setattr(pm, "is_connected", lambda _pid: True)
            assert plate_occupancy.is_plate_occupied(printer.id) is False
            assert scheduler._is_printer_idle(printer.id) is False
            # The per-tick DB-claim evidence is a keyword now; the fault refuses either way.
            assert scheduler._is_printer_idle(printer.id, db_claim=True) is False

            # ...and the eject goes out anyway, once there is a deposit to sweep.
            _gate(printer.id)
            start = MagicMock(return_value=True)
            c1, c2, c3 = _ftp_patches()
            with c1, c2, c3, patch.object(printer_manager, "start_print", start):
                await remote.dispatch_part_present_eject(
                    db_session, printer_id=printer.id, queue_item_id=item.id, purpose="production", run_id=None
                )
            start.assert_called_once()
            assert plate_occupancy.eject_identity(printer.id) is not None
        finally:
            source.unlink(missing_ok=True)


class TestMatchesPendingEject:
    """The shared eject-terminal detection helper (single origin of the mismatch
    rule shared by farm_policy.on_terminal and the main.py start/complete callbacks).
    Lenient: a positive mismatch needs BOTH ids truthy AND unequal."""

    @staticmethod
    def _client(subtask):
        return SimpleNamespace(last_dispatch_subtask_id=subtask)

    async def test_no_eject_claimed_is_false(self):
        with patch.object(printer_manager, "get_client", return_value=self._client("SUB")):
            assert remote.matches_pending_eject(9001, "SUB") is False

    async def test_echo_absent_lenient_match(self):
        _claim(9002, PendingEject("production", 1, 2))
        with patch.object(printer_manager, "get_client", return_value=self._client("SUB")):
            assert remote.matches_pending_eject(9002, None) is True
        # A pure QUERY: retiring the eject is the authority's business, never the matcher's.
        assert plate_occupancy.pending_eject_view(9002) is not None

    async def test_echo_equal_matches(self):
        _claim(9003, PendingEject("production", 1, 2))
        with patch.object(printer_manager, "get_client", return_value=self._client("SUB-E")):
            assert remote.matches_pending_eject(9003, "SUB-E") is True

    async def test_echo_mismatch_both_truthy_is_false(self):
        _claim(9004, PendingEject("production", 1, 2))
        with patch.object(printer_manager, "get_client", return_value=self._client("SUB-E")):
            assert remote.matches_pending_eject(9004, "OTHER") is False
        # A mismatch does NOT retire the eject — the real terminal still owns it.
        assert plate_occupancy.pending_eject_view(9004) is not None

    async def test_expected_absent_lenient_match(self):
        _claim(9005, PendingEject("production", 1, 2))
        with patch.object(printer_manager, "get_client", return_value=self._client(None)):
            assert remote.matches_pending_eject(9005, "SUB") is True

    async def test_no_client_lenient_match(self):
        _claim(9006, PendingEject("production", 1, 2))
        with patch.object(printer_manager, "get_client", return_value=None):
            assert remote.matches_pending_eject(9006, "SUB") is True

    async def test_a_hydrated_eject_is_matchable_too(self):
        # A record rebuilt at startup is still the identity a terminal must be matched
        # against — that is the whole reason the name check exists (W1/R2).
        plate_occupancy.hydrate_eject(9007, PendingEject("production", 1, 32))
        with patch.object(printer_manager, "get_client", return_value=None):
            assert remote.matches_pending_eject(9007, None, subtask_name="eject_production_item32") is True
            assert remote.matches_pending_eject(9007, None, subtask_name="OperatorLocalPrint") is False


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

    def test_expected_stem_accepts_the_identity_projection(self):
        # The matcher only ever holds the projection, never the record.
        _claim(9010, PendingEject("production", 5, 32))
        identity = plate_occupancy.eject_identity(9010)
        assert remote.expected_eject_stem(identity) == "eject_production_item32"


class TestMatchesPendingEjectNameTightening:
    """The tightened matcher (W1/R2): a truthy subtask_name whose stem != the
    pending's expected stem is a POSITIVE mismatch even when the id path is lenient
    (post-restart, no client). Name-match alone with no claimed eject is still
    False — only the pending identity gates the resolution."""

    @staticmethod
    def _client(subtask):
        return SimpleNamespace(last_dispatch_subtask_id=subtask)

    async def test_name_mismatch_positive_mismatch_no_client(self):
        # Post-restart hole: no client (id lenient), but the echoed name is a foreign
        # job → the name check re-establishes the mismatch → NOT our eject.
        _claim(9101, PendingEject("production", 1, 32))
        with patch.object(printer_manager, "get_client", return_value=None):
            assert remote.matches_pending_eject(9101, "ANY", subtask_name="OperatorLocalPrint") is False
            assert plate_occupancy.pending_eject_view(9101) is not None  # foreign — eject kept

    async def test_name_matches_expected_stem_no_client(self):
        _claim(9102, PendingEject("production", 1, 32))
        with patch.object(printer_manager, "get_client", return_value=None):
            assert remote.matches_pending_eject(9102, None, subtask_name="eject_production_item32") is True

    async def test_missing_name_stays_lenient(self):
        # No name supplied → only the (lenient) id path applies.
        _claim(9103, PendingEject("production", 1, 32))
        with patch.object(printer_manager, "get_client", return_value=self._client(None)):
            assert remote.matches_pending_eject(9103, "SUB", subtask_name=None) is True

    async def test_name_alone_with_no_claimed_eject_never_matches(self):
        # is_eject_job_name is the suppress-only signal; matches_pending_eject still
        # requires a claimed pending identity.
        with patch.object(printer_manager, "get_client", return_value=None):
            assert remote.matches_pending_eject(9104, "ANY", subtask_name="eject_production_item32") is False

    async def test_name_wrong_item_number_mismatches(self):
        _claim(9105, PendingEject("production", 1, 32))
        with patch.object(printer_manager, "get_client", return_value=None):
            # Right shape, wrong item id → foreign instance's eject → mismatch.
            assert remote.matches_pending_eject(9105, None, subtask_name="eject_production_item99") is False


class TestManualEjectName:
    """The foreign-plate manual eject job stem ``eject_manual_p{printer_id}`` — parse
    round-trip + the printer-keyed name check in matches_pending_eject."""

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
        _claim(9201, PendingEject("manual", None, None))
        with patch.object(printer_manager, "get_client", return_value=None):
            assert remote.matches_pending_eject(9201, None, subtask_name="eject_manual_p9201") is True
            assert remote.matches_pending_eject(9201, None, subtask_name="eject_manual_p9999") is False
            assert remote.matches_pending_eject(9201, None, subtask_name="OperatorLocalPrint") is False


class TestDispatchForeignEject:
    async def test_claims_a_manual_pending_and_starts_all_off(self, db_session):
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
            _gate(printer.id)
            start = MagicMock(return_value=True)
            c1, c2, c3 = _ftp_patches()
            with c1, c2, c3, patch.object(printer_manager, "start_print", start):
                await remote.dispatch_foreign_eject(
                    db_session, printer_id=printer.id, profile_id=prof.id, source_path=source, plate_id=1
                )
            pending = plate_occupancy.pending_eject_view(printer.id)
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
            source.unlink(missing_ok=True)

    async def test_upload_failure_raises_502_and_claims_nothing(self, db_session):
        source = _make_source_3mf()
        try:
            printer = Printer(name="FEu", serial_number="FEu1", ip_address="1.2.3.4", access_code="x", model="H2S")
            db_session.add(printer)
            await db_session.flush()
            prof = EjectProfile(name="feu-ep")
            db_session.add(prof)
            await db_session.commit()
            await db_session.refresh(printer)
            await db_session.refresh(prof)
            _gate(printer.id)
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
            assert plate_occupancy.pending_eject_view(printer.id) is None
        finally:
            source.unlink(missing_ok=True)

    async def test_unknown_profile_raises_409(self, db_session):
        source = _make_source_3mf()
        try:
            printer = Printer(name="FEp", serial_number="FEp1", ip_address="1.2.3.4", access_code="x", model="H2S")
            db_session.add(printer)
            await db_session.commit()
            await db_session.refresh(printer)
            # Gated, so the occupancy check passes and the PROFILE is what refuses.
            _gate(printer.id)
            c1, c2, c3 = _ftp_patches()
            with c1, c2, c3, pytest.raises(remote.EjectDispatchError) as exc:
                await remote.dispatch_foreign_eject(
                    db_session, printer_id=printer.id, profile_id=987654, source_path=source, plate_id=1
                )
            assert exc.value.status_code == 409
            assert "profile" in str(exc.value).lower()
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
            _gate(printer.id)
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
            _gate(printer.id)
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


class _DriverLog:
    """Records what the authority hands its policy driver.

    The driver is the ONE place a watch is armed, so recording its calls is how a test
    pins that a transition did — or did NOT — arrange a hold."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str, OccupancyView]] = []

    def __call__(self, printer_id: int, view: OccupancyView, cause: str) -> None:
        self.calls.append((printer_id, cause, view))

    @property
    def causes(self) -> list[str]:
        return [cause for _pid, cause, _view in self.calls]


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


class TestEjectStartEcho:
    """The START echo is the only moment a deadline can be measured from — and it is
    also what proves the firmware did not silently ignore our ``project_file``."""

    async def test_start_echo_stamps_once_cancels_the_deadline_and_arms_one_watchdog(self):
        pid = 90001
        _claim(pid, PendingEject("production", 1, 5, expected_runtime_s=82.0))
        remote._arm_start_deadline(pid)
        deadline = remote._start_deadlines[pid]
        assert plate_occupancy.eject_identity(pid).started_at is None

        remote.on_eject_start_echo(pid)

        first = plate_occupancy.eject_identity(pid).started_at
        assert first is not None
        # The sweep demonstrably started: the never-started timer has nothing to catch.
        assert pid not in remote._start_deadlines
        with contextlib.suppress(asyncio.CancelledError):
            await deadline
        assert deadline.cancelled()
        assert pid in remote._runtime_watchdogs
        armed_task = remote._runtime_watchdogs[pid]

        # A duplicate/replayed start echo must NOT restart the clock — that would
        # shorten a measured runtime back under the deadline — nor double-arm.
        remote.on_eject_start_echo(pid)
        assert plate_occupancy.eject_identity(pid).started_at == first
        assert remote._runtime_watchdogs[pid] is armed_task

        # The rest of the identity survives the stamp.
        pending = plate_occupancy.pending_eject_view(pid)
        assert (pending.purpose, pending.run_id, pending.queue_item_id) == ("production", 1, 5)
        assert pending.expected_runtime_s == 82.0
        assert pending.runtime_exceeded_at is None

    async def test_start_echo_with_no_claimed_eject_is_a_noop(self):
        remote.on_eject_start_echo(90002)  # must not raise
        assert plate_occupancy.eject_identity(90002) is None
        assert 90002 not in remote._runtime_watchdogs

    async def test_a_pending_without_an_estimate_never_arms_a_watchdog(self):
        # A rehydrated post-restart pending carries no estimate: there is nothing to
        # judge against, and the startup reconciler already owns those gates.
        pid = 90003
        plate_occupancy.hydrate_eject(pid, PendingEject("production", 1, 5))

        remote.on_eject_start_echo(pid)

        assert plate_occupancy.eject_identity(pid).started_at is not None  # still stamped
        assert pid not in remote._runtime_watchdogs


class TestEjectStartDeadline:
    """The 2026-08-30 "8 consecutive eject 409s" class (printer 4, 01:46-01:49).

    The firmware silently IGNORES a ``project_file`` sent while it is busy — no error,
    no terminal, nothing — so an eject dispatched into a print that had just started
    simply never happens. Before this deadline existed such a pending stayed registered
    forever, every later eject 409'd ``eject_in_flight``, and the operator ended up
    hand-jogging the toolhead. The deadline is the ONLY signal that shape produces."""

    async def test_an_eject_the_printer_never_started_expires_and_frees_the_printer(self):
        from backend.app.services.eject import monitor as monitor_mod

        pid = 90060
        _gate(pid, source="SUB-DEAD", policy=CooldownEject(unit_id=5, run_id=1))
        _claim(pid, PendingEject("production", 1, 5, expected_runtime_s=83.0))
        sleep = _FakeSleep()

        with patch.object(monitor_mod, "notify_plate_not_empty", new_callable=AsyncMock) as notify:
            await remote._start_deadline(pid, sleep=sleep, timeout_s=remote.EJECT_START_TIMEOUT_S)

        assert sleep.delays == [remote.EJECT_START_TIMEOUT_S]
        # The dead pending is GONE — that is what unblocks every later eject...
        assert plate_occupancy.eject_identity(pid) is None
        # ...while the plate stays OCCUPIED, demoted to escalation-only: the sweep never
        # ran, so the part is still on the bed and only a human can say otherwise.
        view = plate_occupancy.snapshot(pid)
        assert view.plate_occupied is True
        assert isinstance(view.plate_policy, EscalationOnly)
        assert view.plate_source_subtask_id == "SUB-DEAD"
        notify.assert_awaited_once()
        assert "never started" in notify.await_args.kwargs["source_detail"]
        # The printer is ejectable again — the operator can simply eject now.
        assert plate_occupancy.ejectable(pid, Evidence()) is None

    async def test_a_started_eject_is_never_expired_by_a_late_deadline(self):
        # The task can wake late or spuriously: the authority, not the timer, decides.
        from backend.app.services.eject import monitor as monitor_mod

        pid = 90061
        _claim(pid, PendingEject("production", 1, 5, expected_runtime_s=83.0))
        plate_occupancy.note_eject_started(pid)

        with patch.object(monitor_mod, "notify_plate_not_empty", new_callable=AsyncMock) as notify:
            await remote._start_deadline(pid, sleep=_FakeSleep(), timeout_s=1.0)

        assert plate_occupancy.eject_identity(pid) is not None
        notify.assert_not_awaited()

    async def test_a_hydrated_eject_never_expires_on_the_start_deadline(self):
        """Its ``started_at`` is None BY CONSTRUCTION, so an expiry keyed on that would
        fire on every restart and discard a record the startup reconciler still owns."""
        from backend.app.services.eject import monitor as monitor_mod

        pid = 90062
        _gate(pid)
        plate_occupancy.hydrate_eject(pid, PendingEject("production", 1, 5))

        with patch.object(monitor_mod, "notify_plate_not_empty", new_callable=AsyncMock) as notify:
            await remote._start_deadline(pid, sleep=_FakeSleep(), timeout_s=1.0)

        identity = plate_occupancy.eject_identity(pid)
        assert identity is not None and identity.hydrated is True
        notify.assert_not_awaited()


class TestCancelEjectTimers:
    """THE one deregistration point, called by the occupancy policy driver on every
    transition that leaves the printer with no eject."""

    async def test_cancels_both_timers_and_is_idempotent(self):
        pid = 90070
        _claim(pid, PendingEject("production", 1, 5, expected_runtime_s=82.0))
        remote.on_eject_start_echo(pid)
        # The echo cancels the deadline it replaces, so arm a fresh one to prove BOTH
        # lanes are dropped by one call.
        remote._arm_start_deadline(pid)
        deadline = remote._start_deadlines[pid]
        watchdog = remote._runtime_watchdogs[pid]

        remote.cancel_eject_timers(pid)
        remote.cancel_eject_timers(pid)  # idempotent — never raises, never resurrects

        assert pid not in remote._start_deadlines
        assert pid not in remote._runtime_watchdogs
        for task in (deadline, watchdog):
            with contextlib.suppress(asyncio.CancelledError):
                await task
            assert task.cancelled()

    async def test_cancelling_when_nothing_is_armed_is_a_noop(self):
        remote.cancel_eject_timers(90071)  # must not raise

    async def test_retiring_the_eject_hands_the_driver_the_no_eject_view(self):
        """The level-trigger the monitor's driver cancels both timers on: whatever
        retires the eject (a matched terminal, an unverified resolve, a start expiry,
        the reconciler's disposal, an operator recover), the view it sees carries
        ``eject_present is False``."""
        pid = 90072
        _claim(pid, PendingEject("production", 1, 5, expected_runtime_s=82.0))
        driver = _DriverLog()
        plate_occupancy.configure(policy_driver=driver)

        assert plate_occupancy.resolve_eject(pid, "completed") is None

        assert driver.causes == ["eject_completed"]
        _pid, _cause, view = driver.calls[-1]
        assert view.eject_present is False
        assert view.plate_occupied is False

    async def test_a_successor_sweep_gets_its_own_watchdog(self):
        """A stale watchdog must never judge the NEXT sweep against the OLD deadline —
        and the successor must not be left unwatched by a leftover registry entry."""
        pid = 90073
        _claim(pid, PendingEject("production", 1, 5, expected_runtime_s=82.0))
        remote.on_eject_start_echo(pid)
        stale = remote._runtime_watchdogs[pid]

        # The eject's terminal retires it, and the driver drops both timers.
        assert plate_occupancy.resolve_eject(pid, "completed") is None
        remote.cancel_eject_timers(pid)
        with contextlib.suppress(asyncio.CancelledError):
            await stale
        assert stale.cancelled()

        _claim(pid, PendingEject("production", 1, 6, expected_runtime_s=82.0))
        remote.on_eject_start_echo(pid)
        assert remote._runtime_watchdogs[pid] is not stale


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

    async def test_marks_before_stopping_then_notifies_and_deregisters(self):
        from backend.app.services.eject import monitor as monitor_mod

        pid = 90010
        armed = _armed()
        _claim(pid, armed)
        sleep = _FakeSleep()
        mark_at_stop: list[object] = []

        def _stop(printer_id: int) -> bool:
            # Captured INSIDE the stop: a terminal racing the stop must already find
            # the mark set, or it would release the gate onto an unverified plate.
            mark_at_stop.append(plate_occupancy.pending_eject_view(printer_id).runtime_exceeded_at)
            return True

        with (
            patch.object(printer_manager, "stop_print", MagicMock(side_effect=_stop)) as stop,
            patch.object(monitor_mod, "notify_plate_not_empty", new_callable=AsyncMock) as notify,
        ):
            await self._run(pid, armed, sleep)
        assert sleep.delays == [103.75]  # the 83 s estimate's deadline
        stop.assert_called_once_with(pid)
        assert mark_at_stop and mark_at_stop[0] is not None
        assert plate_occupancy.pending_eject_view(pid).runtime_exceeded_at is not None
        notify.assert_awaited_once()
        detail = notify.await_args.kwargs["source_detail"]
        assert "STOPPED" in detail and "104" in detail and "83" in detail
        assert pid not in remote._runtime_watchdogs  # deregistered on exit

    async def test_the_stop_arms_no_hold_of_its_own(self):
        """The escalation hold moved OUT of the kill path: while an eject owns the
        printer the plate carries no watch BY CONSTRUCTION (an armed cooldown over a
        plate a sweep is crossing is the double dispatch the authority forbids). The
        hold arrives one step later, from the stopped sweep's ``unverified`` resolve."""
        from backend.app.services.eject import monitor as monitor_mod

        # The old kill-path entry point is gone; the driver is the one arming site.
        assert not hasattr(monitor_mod.eject_cooldown_monitor, "start_escalation_only_watch")

        pid = 90015
        armed = _armed()
        # The production shape: a cooldown plate, swept by the eject that plate armed.
        _gate(pid, policy=CooldownEject(unit_id=5, run_id=1))
        _claim(pid, armed)
        driver = _DriverLog()
        plate_occupancy.configure(policy_driver=driver)

        with (
            patch.object(printer_manager, "stop_print", MagicMock(return_value=True)),
            patch.object(monitor_mod, "notify_plate_not_empty", new_callable=AsyncMock),
        ):
            await self._run(pid, armed, _FakeSleep())

        # The kill stamps the verdict and NOTHING else: every view the driver saw still
        # has an eject on the printer, which is exactly "no watch may be armed here".
        assert driver.causes == ["eject_runtime_exceeded"]
        assert all(view.eject_present and view.plate_occupied for _pid, _c, view in driver.calls)

        # ...and the hold appears only once the stopped sweep's terminal resolves it.
        assert plate_occupancy.resolve_eject(pid, "unverified") is None
        _pid, cause, view = driver.calls[-1]
        assert cause == "eject_unverified"
        assert view.eject_present is False
        assert view.plate_occupied is True
        assert isinstance(view.plate_policy, EscalationOnly)

    async def test_undelivered_stop_is_retried_exactly_once(self):
        # stop_print returning False means the command was NOT delivered (no live MQTT
        # session). One retry, then proceed regardless — the mark alone already keeps
        # the gate closed, and the operator is paged either way.
        from backend.app.services.eject import monitor as monitor_mod

        pid = 90011
        armed = _armed()
        _claim(pid, armed)
        sleep = _FakeSleep()
        with (
            patch.object(printer_manager, "stop_print", MagicMock(return_value=False)) as stop,
            patch.object(monitor_mod, "notify_plate_not_empty", new_callable=AsyncMock) as notify,
        ):
            await self._run(pid, armed, sleep)
        assert stop.call_count == 2
        assert sleep.delays == [103.75, remote._STOP_RETRY_DELAY_S]
        assert plate_occupancy.pending_eject_view(pid).runtime_exceeded_at is not None
        notify.assert_awaited_once()

    async def test_notify_failure_never_kills_the_watchdog(self):
        from backend.app.services.eject import monitor as monitor_mod

        pid = 90012
        armed = _armed()
        _claim(pid, armed)
        with (
            patch.object(printer_manager, "stop_print", MagicMock(return_value=True)),
            patch.object(
                monitor_mod,
                "notify_plate_not_empty",
                new_callable=AsyncMock,
                side_effect=RuntimeError("smtp down"),
            ),
        ):
            await self._run(pid, armed, _FakeSleep())
        # The verdict still stands and the task still deregisters cleanly.
        assert plate_occupancy.pending_eject_view(pid).runtime_exceeded_at is not None
        assert pid not in remote._runtime_watchdogs

    async def test_superseded_pending_stands_the_watchdog_down(self):
        # The eject this task was armed for resolved while it slept and a NEW one was
        # dispatched onto the same printer. Stopping now would abort a healthy job.
        from backend.app.services.eject import monitor as monitor_mod

        pid = 90013
        armed = _armed(5)
        _claim(pid, _armed(6))
        with (
            patch.object(printer_manager, "stop_print", MagicMock(return_value=True)) as stop,
            patch.object(monitor_mod, "notify_plate_not_empty", new_callable=AsyncMock) as notify,
        ):
            await self._run(pid, armed, _FakeSleep())
        stop.assert_not_called()
        notify.assert_not_awaited()
        assert plate_occupancy.pending_eject_view(pid).runtime_exceeded_at is None  # successor unmarked
        assert pid not in remote._runtime_watchdogs

    async def test_resolved_pending_stands_the_watchdog_down(self):
        pid = 90014
        with patch.object(printer_manager, "stop_print", MagicMock(return_value=True)) as stop:
            await self._run(pid, _armed(), _FakeSleep())
        stop.assert_not_called()
        assert pid not in remote._runtime_watchdogs

    async def test_the_identity_check_reads_the_authority_not_a_captured_copy(self):
        # ``_watchdog_still_owns`` returns the printer's CURRENT identity, because the
        # record it must not act on is precisely one that changed underneath it.
        pid = 90016
        armed = _armed(5)
        _claim(pid, armed)
        current = remote._watchdog_still_owns(pid, armed)
        assert current is not None
        assert (current.purpose, current.queue_item_id, current.started_at) == (
            armed.purpose,
            armed.queue_item_id,
            armed.started_at,
        )
        assert current.hydrated is False
        assert remote._watchdog_still_owns(pid, _armed(6)) is None


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
        _claim(pid, armed)
        with (
            patch.object(printer_manager, "get_client", MagicMock(return_value=client)),
            patch.object(printer_manager, "stop_print", MagicMock(return_value=True)),
            patch.object(monitor_mod, "notify_plate_not_empty", new_callable=AsyncMock),
            caplog.at_level(logging.WARNING, logger="backend.app.services.eject.remote"),
        ):
            await TestRuntimeWatchdogFires._run(pid, armed, _FakeSleep())
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
        patch.object(monitor_mod, "notify_plate_not_empty", new_callable=AsyncMock) as notify,
    ):
        yield SimpleNamespace(stop=stop, pushall=pushall, notify=notify)


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
        _claim(pid, armed)
        clock = _FakeClock()
        feed = _ProgressFeed(clock, [(5.0, 0.0)])
        with (
            _watchdog_env(feed) as env,
            caplog.at_level(logging.WARNING, logger="backend.app.services.eject.remote"),
        ):
            await _spawn_watchdog(pid, armed, clock)
        env.stop.assert_called_once_with(pid)
        assert plate_occupancy.pending_eject_view(pid).runtime_exceeded_at is not None
        message = _kill_line(caplog)
        assert "stage=drop" in message and "stage=drop_late" not in message
        detail = env.notify.await_args.kwargs["source_detail"]
        assert "bed-drop" in detail and "under the heatbed" in detail
        assert pid not in remote._runtime_watchdogs
        # Killed on the DROP deadline (t5 + 38 s), far short of the 103.75 s whole-job one.
        assert clock.now - 1000.0 == pytest.approx(42.0)

    async def test_sweep_beacon_arriving_after_the_drop_deadline_still_stops(self, caplog):
        # The drop verifiably ran long. The sweep opens with rear positioning at lift
        # height, so the stop still lands before the toolhead can reach the plate.
        pid = 90041
        armed = _armed_with_drop()
        _claim(pid, armed)
        clock = _FakeClock()
        feed = _ProgressFeed(clock, [(5.0, 0.0)] * 20 + [(50.0, 0.0)])
        with (
            _watchdog_env(feed) as env,
            caplog.at_level(logging.WARNING, logger="backend.app.services.eject.remote"),
        ):
            await _spawn_watchdog(pid, armed, clock)
        env.stop.assert_called_once_with(pid)
        assert "stage=drop_late" in _kill_line(caplog)
        assert "bed-drop" in env.notify.await_args.kwargs["source_detail"]
        assert plate_occupancy.pending_eject_view(pid).runtime_exceeded_at is not None

    async def test_healthy_phase_sequence_never_kills_and_cancels_on_resolution(self, caplog):
        # 0 → 5 → 50 → 75: the drop cleared inside its budget, so no phase rule may fire.
        # The eject's terminal then cancels the task, which is the NORMAL exit.
        pid = 90042
        armed = _armed_with_drop()
        _claim(pid, armed)
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
            # The matched terminal retires the eject; the policy driver's level-triggered
            # timer hygiene then drops both timers.
            assert plate_occupancy.resolve_eject(pid, "completed") is None
            remote.cancel_eject_timers(pid)
            env.stop.assert_not_called()
            env.notify.assert_not_awaited()
        # LIVENESS: the lane really ran the sequence — a silent watchdog would satisfy
        # every "did not fire" assertion above without ever observing a phase edge.
        assert feed.reads >= 4
        assert [r for r in caplog.records if "bed-drop phase cleared" in r.getMessage()]
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert task.cancelled()
        assert pid not in remote._runtime_watchdogs
        assert plate_occupancy.pending_eject_view(pid) is None

    async def test_beacons_never_reflected_falls_back_to_the_whole_job_deadline(self, caplog):
        # mc_percent stuck at 0 while fresh: the firmware is publishing, but the beacons
        # are not landing in it. The lane must degrade to the original rule, exactly once,
        # and the whole-job deadline must still fire.
        pid = 90043
        armed = _armed_with_drop()
        _claim(pid, armed)
        clock = _FakeClock()
        feed = _ProgressFeed(clock, [(0.0, 0.0)])
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

    async def test_stale_evidence_never_kills_and_asks_once_for_a_fresh_report(self, caplog):
        # One fresh sample opens the drop window, then the link goes quiet: every later
        # sample is stamped before this eject even armed. Stale evidence advances nothing
        # and justifies no stop — the lane asks for ONE pushall and leaves the whole-job
        # deadline as the backstop.
        pid = 90044
        armed = _armed_with_drop()
        _claim(pid, armed)
        clock = _FakeClock()
        feed = _ProgressFeed(clock, [(5.0, 0.0), (5.0, None)])
        with (
            _watchdog_env(feed) as env,
            caplog.at_level(logging.WARNING, logger="backend.app.services.eject.remote"),
        ):
            await _spawn_watchdog(pid, armed, clock)
        env.pushall.assert_called_once_with(pid, remote._EVIDENCE_REASON)
        env.stop.assert_called_once_with(pid)
        assert "stage=total" in _kill_line(caplog)  # NOT a drop kill
        assert clock.now - 1000.0 >= remote.eject_abort_deadline_s(83.0)

    async def test_aged_out_sample_is_not_evidence_either(self, caplog):
        # Stamped after arming but far older than the freshness window: a link that went
        # silent mid-eject is not a phase report, so the drop rule must not fire on it.
        pid = 90045
        armed = _armed_with_drop()
        _claim(pid, armed)
        clock = _FakeClock()
        feed = _ProgressFeed(clock, [(5.0, 0.0), (5.0, remote._PROGRESS_FRESH_S + 30.0)])
        with (
            _watchdog_env(feed) as env,
            caplog.at_level(logging.WARNING, logger="backend.app.services.eject.remote"),
        ):
            await _spawn_watchdog(pid, armed, clock)
        assert "stage=total" in _kill_line(caplog)
        env.stop.assert_called_once_with(pid)

    async def test_superseded_pending_stands_the_edge_lane_down(self):
        # The eject this task was armed for resolved and a NEW one took the printer.
        pid = 90046
        armed = _armed_with_drop(5)
        _claim(pid, _armed_with_drop(6))
        clock = _FakeClock()
        feed = _ProgressFeed(clock, [(5.0, 0.0)])
        with _watchdog_env(feed) as env:
            await _spawn_watchdog(pid, armed, clock)
        env.stop.assert_not_called()
        env.notify.assert_not_awaited()
        assert plate_occupancy.pending_eject_view(pid).runtime_exceeded_at is None
        assert pid not in remote._runtime_watchdogs


class TestRuntimeWatchdogDeadlineOnlyFallback:
    """Without a drop budget the watchdog is EXACTLY what it was before the edge lane:
    one sleep to the whole-job deadline, then the same stop. A rehydrated post-restart
    pending and every drop-less profile land here."""

    async def test_no_drop_span_sleeps_straight_to_the_total_deadline(self, caplog):
        pid = 90050
        armed = _armed()  # drop_span_s defaults to None
        _claim(pid, armed)
        clock = _FakeClock()
        feed = _ProgressFeed(clock, [(5.0, 0.0)])
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

    async def test_a_drop_budget_that_cannot_beat_the_total_deadline_polls_nothing(self):
        # 100 s span + 20 s margin = 120 s, past the 103.75 s whole-job deadline: the
        # phase rule could never fire first, so arming it would only add failure modes.
        pid = 90051
        armed = _armed_with_drop(drop_span_s=100.0)
        _claim(pid, armed)
        clock = _FakeClock()
        feed = _ProgressFeed(clock, [(5.0, 0.0)])
        with _watchdog_env(feed) as env:
            await _spawn_watchdog(pid, armed, clock)
        assert clock.delays == [103.75]
        env.stop.assert_called_once_with(pid)


def _armed_with_phases(
    pid_item: int = 5,
    *,
    drop_span_s: float = 30.0,
    sweep_span_s: float | None = 20.0,
    tail_s: float | None = 15.0,
    **kw,
) -> remote.PendingEject:
    """A started production pending carrying ALL the build's phase budgets.

    83 s expected → a 103.75 s whole-job deadline; 30 s drop → an 8 s margin (the floor)
    → the drop deadline lands 38 s after the observed P5 edge; 20 s sweep → the same 8 s
    floor → the sweep BUDGET line lands 28 s after the observed P50 edge (a line that
    only warns); 15 s tail → the epilogue deadline lands 215 s after the observed P75."""
    kw.setdefault("expected_runtime_s", 83.0)
    kw.setdefault("started_at", datetime.now(timezone.utc))
    return remote.PendingEject(
        "production", 1, pid_item, drop_span_s=drop_span_s, sweep_span_s=sweep_span_s, tail_s=tail_s, **kw
    )


def _records(caplog, needle: str) -> list:
    return [r for r in caplog.records if needle in r.getMessage()]


class TestBindingDeadline:
    """The ONE deadline-selection point. Whichever phase the poller is in, exactly one
    deadline is in force, and the whole-job figure it is derived from is never mutated."""

    async def test_every_phase_but_the_epilogue_holds_the_whole_job_deadline(self):
        for phase in ("await_p5", "await_p50", "await_p75", "deadline_only"):
            assert remote._binding_deadline(
                phase, armed_at=1000.0, total_deadline_s=103.75, t75=1050.0, tail_s=15.0
            ) == (1103.75, "total")

    async def test_the_epilogue_deadline_is_anchored_on_the_observed_park_edge(self):
        deadline, stage = remote._binding_deadline(
            "epilogue", armed_at=1000.0, total_deadline_s=103.75, t75=1060.0, tail_s=15.0
        )
        assert (deadline, stage) == (1060.0 + 15.0 + remote.UNMODELLED_EPILOGUE_ALLOWANCE_S, "epilogue")

    async def test_the_epilogue_deadline_can_only_ever_loosen(self):
        # The invariant behind the `max`: at a smaller future allowance the epilogue rule
        # must silently revert to the whole-job deadline rather than become TIGHTER than
        # the rule it replaces — a patient window that kills sooner is the one outcome
        # this change may never produce.
        deadline, stage = remote._binding_deadline(
            "epilogue", armed_at=1000.0, total_deadline_s=900.0, t75=1005.0, tail_s=1.0
        )
        assert (deadline, stage) == (1900.0, "epilogue")
        assert deadline >= 1000.0 + 900.0

    async def test_missing_build_figures_fail_closed_onto_the_whole_job_deadline(self):
        assert remote._binding_deadline(
            "epilogue", armed_at=1000.0, total_deadline_s=103.75, t75=1060.0, tail_s=None
        ) == (1103.75, "total")
        assert remote._binding_deadline(
            "epilogue", armed_at=1000.0, total_deadline_s=103.75, t75=None, tail_s=15.0
        ) == (1103.75, "total")


class TestStopReasonCopy:
    """One origin for kill copy, two renderings. Before it, every stage was paged as an
    'under-bed obstruction' — already wrong for `drop_late`/`total`, and unusable for a
    stage that fires after the wire says the sweep completed."""

    def _reason(self, stage, **kw):
        kw.setdefault("fired_deadline_s", 104.0)
        kw.setdefault("phase_elapsed_s", 42.0)
        kw.setdefault("expected_s", 83.0)
        kw.setdefault("drop_span_s", None)
        kw.setdefault("sweep_span_s", None)
        return remote._stop_reason(stage, **kw)

    async def test_a_drop_kill_names_the_bed_drop_budget_and_the_heatbed(self):
        diagnostic, detail = self._reason("drop", drop_span_s=30.0)
        assert "bed-drop" in diagnostic and "30s" in diagnostic
        assert "bed-drop" in detail and "under the heatbed" in detail

    async def test_a_drop_late_kill_says_the_sweep_started_after_the_overrun(self):
        diagnostic, detail = self._reason("drop_late", drop_span_s=30.0)
        assert "sweep beacon arrived only after" in diagnostic
        assert "overran" in detail and "under the heatbed" in detail

    async def test_a_beacon_blind_total_keeps_the_obstruction_wording(self):
        # No phase was ever observed, so an obstruction is still the honest suspicion.
        diagnostic, detail = self._reason("total", phase_elapsed_s=104.0)
        assert "under-bed obstruction" in diagnostic
        assert "STOPPED mid-job" in detail and "104" in detail and "83" in detail

    async def test_a_total_that_fired_past_an_in_time_p50_blames_the_sweep_not_the_bed(self):
        # The sweep budget can only have been armed by an on-time P50 edge, so the drop
        # demonstrably cleared: pointing the operator under the heatbed would send them
        # to a place the wire has already ruled out.
        diagnostic, detail = self._reason("total", phase_elapsed_s=100.0, drop_span_s=30.0, sweep_span_s=20.0)
        assert "cleared on budget" in diagnostic and "20s" in diagnostic
        assert "did not release" in diagnostic
        assert "under the heatbed" not in detail
        assert "may not have released" in detail

    async def test_an_epilogue_kill_says_both_phases_cleared_and_the_job_never_finished(self):
        diagnostic, detail = self._reason("epilogue", phase_elapsed_s=215.0, fired_deadline_s=280.0)
        assert "both cleared" in diagnostic and "firmware tail" in diagnostic
        assert "280" in diagnostic
        assert "sweep COMPLETED" in detail and "under the heatbed" not in detail


class TestRuntimeWatchdogSweepLane:
    """The sweep phase MEASURES and never kills (2026-08-31, principle 4).

    A part that fails to release presents as a sweep overrun, so this phase is never
    given patience: the whole-job deadline keeps today's exact stuck-part timing through
    it, and the budget line only produces the P50→P75 distribution a calibrated sweep
    kill will be armed from."""

    @staticmethod
    async def _run(pid: int, armed, samples, caplog) -> tuple:
        clock = _FakeClock()
        feed = _ProgressFeed(clock, samples)
        _claim(pid, armed)
        with (
            _watchdog_env(feed) as env,
            caplog.at_level(logging.INFO, logger="backend.app.services.eject.remote"),
        ):
            await _spawn_watchdog(pid, armed, clock)
        return env, clock

    async def test_a_sweep_running_past_its_budget_warns_once_and_never_kills_there(self, caplog):
        # Percent parked at the sweep beacon: the sweep budget line (t50 + 28 s) passes
        # at ~34 s, and nothing may fire there this wave.
        env, clock = await self._run(90060, _armed_with_phases(), [(5.0, 0.0), (50.0, 0.0)], caplog)
        warnings = _records(caplog, "sweep phase running")
        assert len(warnings) == 1
        assert "20s budget" in warnings[0].getMessage()
        assert warnings[0].levelno == logging.WARNING
        # The kill that DID happen is the whole-job backstop, not a sweep rule.
        assert "stage=total" in _kill_line(caplog)
        assert not _records(caplog, "stage=drop")
        env.stop.assert_called_once_with(90060)

    async def test_the_whole_job_deadline_is_still_enforced_inside_the_sweep_phase(self, caplog):
        # The complement of the epilogue's patience: while the plate may still be under
        # the toolhead, the stuck-part timing is EXACTLY what it was before this wave.
        env, clock = await self._run(90061, _armed_with_phases(), [(5.0, 0.0), (50.0, 0.0)], caplog)
        assert clock.now - 1000.0 == pytest.approx(104.0)
        assert clock.now - 1000.0 >= remote.eject_abort_deadline_s(83.0)
        detail = env.notify.await_args.kwargs["source_detail"]
        assert "may not have released" in detail and "under the heatbed" not in detail

    async def test_a_park_beacon_after_the_budget_line_advances_with_a_warning(self, caplog):
        # A 5 s sweep budget (+8 s floor) with the park beacon arriving at ~18 s: the
        # overrun is recorded for the distribution and the job is NOT killed for it —
        # deliberately asymmetric with drop_late, whose stop still precedes plate
        # contact, while past the park beacon there is no contact left to pre-empt.
        armed = _armed_with_phases(sweep_span_s=5.0)
        samples = [(5.0, 0.0), (50.0, 0.0)] + [(50.0, 0.0)] * 8 + [(75.0, 0.0)]
        env, clock = await self._run(90062, armed, samples, caplog)
        cleared = _records(caplog, "sweep phase cleared")
        assert len(cleared) == 1
        assert cleared[0].levelno == logging.WARNING
        assert "OVERRAN" in cleared[0].getMessage()
        # It advanced rather than died: the only kill is the epilogue's own deadline.
        assert "stage=epilogue" in _kill_line(caplog)
        env.stop.assert_called_once_with(90062)

    async def test_a_stale_park_beacon_never_opens_the_epilogue(self, caplog):
        # Patience is bought by FRESH wire evidence only. A held-over percent proves
        # nothing about the phase running now, so the job keeps the tight deadline.
        armed = _armed_with_phases()
        env, clock = await self._run(90063, armed, [(5.0, 0.0), (50.0, 0.0), (75.0, None)], caplog)
        assert not _records(caplog, "sweep phase cleared")
        assert not _records(caplog, "sweep phase running")  # the WARN is fresh-gated too
        assert "stage=total" in _kill_line(caplog)
        assert clock.now - 1000.0 == pytest.approx(104.0)

    async def test_without_a_sweep_budget_both_new_lanes_stay_silent(self, caplog):
        # A rehydrated pending carries no build figures. The drop lane still runs to
        # completion (liveness) and the job then keeps exactly today's behaviour.
        armed = _armed_with_phases(sweep_span_s=None, tail_s=None)
        env, clock = await self._run(90064, armed, [(5.0, 0.0), (50.0, 0.0), (75.0, 0.0)], caplog)
        assert _records(caplog, "bed-drop phase cleared")
        assert not _records(caplog, "sweep phase")
        assert "stage=total" in _kill_line(caplog)
        assert clock.now - 1000.0 == pytest.approx(104.0)
        detail = env.notify.await_args.kwargs["source_detail"]
        assert "STOPPED mid-job" in detail and "under the heatbed" in detail


class TestRuntimeWatchdogEpilogue:
    """The one patient window, and the trap it must not fall into.

    23 of the 24 kills in the beacon era fired AFTER the bed-drop phase had cleared, on
    jobs whose parts were physically ejected: the whole-job deadline was expiring during
    a firmware tail the estimator does not model. Past the park beacon no motion over the
    plate remains, so the only thing a longer deadline exposes is downtime."""

    @staticmethod
    async def _run_to_park(pid: int, caplog, armed=None) -> tuple:
        clock = _FakeClock()
        feed = _ProgressFeed(clock, [(5.0, 0.0), (50.0, 0.0), (75.0, 0.0)])
        armed = armed or _armed_with_phases()
        _claim(pid, armed)
        with (
            _watchdog_env(feed) as env,
            caplog.at_level(logging.INFO, logger="backend.app.services.eject.remote"),
        ):
            await _spawn_watchdog(pid, armed, clock)
        return env, clock

    async def test_a_park_beacon_sample_never_produces_a_drop_late_kill(self, caplog):
        # PINS THE FALLTHROUGH TRAP: the predecessor skipped the phase rules for a TUPLE
        # of states and let everything else fall into the bed-drop branch — where a
        # percent at/above the SWEEP beacon arriving past the drop deadline is a
        # drop_late kill. This is that exact shape: a 0.5 s drop budget (deadline t5 +
        # 8.5 s), the link blind through the sweep, and the first sample back is the PARK
        # beacon at ~12 s. Read by the drop lane it is a drop_late kill; read by the
        # phase that actually owns it, it is proof the sweep is over.
        pid = 90070
        clock = _FakeClock()
        feed = _ProgressFeed(clock, [(5.0, 0.0), (50.0, 0.0), (50.0, None), (50.0, None), (50.0, None), (75.0, 0.0)])
        armed = _armed_with_phases(drop_span_s=0.5)
        _claim(pid, armed)
        with (
            _watchdog_env(feed) as env,
            caplog.at_level(logging.INFO, logger="backend.app.services.eject.remote"),
        ):
            await _spawn_watchdog(pid, armed, clock)
        assert not _records(caplog, "stage=drop_late")
        assert not _records(caplog, "stage=drop")
        assert _records(caplog, "sweep phase cleared")
        assert "stage=epilogue" in _kill_line(caplog)
        env.stop.assert_called_once_with(pid)

    async def test_the_epilogue_is_entered_on_the_first_fresh_park_sample(self, caplog):
        await self._run_to_park(90071, caplog)
        cleared = _records(caplog, "sweep phase cleared")
        assert len(cleared) == 1
        assert cleared[0].levelno == logging.INFO
        assert "in <=2.0s" in cleared[0].getMessage() and "budget 20s" in cleared[0].getMessage()

    async def test_the_whole_job_deadline_is_not_enforced_once_the_sweep_is_proven_done(self, caplog):
        # The change itself: this job ran 118 s past the rule that used to kill it.
        env, clock = await self._run_to_park(90072, caplog)
        assert clock.now - 1000.0 > remote.eject_abort_deadline_s(83.0)
        # Killed at the epilogue deadline instead: park edge (t=6 s) + 15 s tail + the
        # 200 s allowance, caught on the following 2 s poll.
        assert clock.now - 1000.0 == pytest.approx(222.0)

    async def test_a_job_wedged_after_the_sweep_is_still_stopped_and_escalated(self, caplog):
        # LIVENESS for the patient window: patience is bounded, the printer is freed, and
        # the plate stays gated exactly as every other kill leaves it (the mark is what
        # resolves the eject `unverified` → EscalationOnly).
        env, clock = await self._run_to_park(90073, caplog)
        env.stop.assert_called_once_with(90073)
        assert plate_occupancy.pending_eject_view(90073).runtime_exceeded_at is not None
        kill = _kill_line(caplog)
        assert "stage=epilogue" in kill and "deadline 221s" in kill
        detail = env.notify.await_args.kwargs["source_detail"]
        assert "sweep COMPLETED" in detail and "under the heatbed" not in detail
        assert 90073 not in remote._runtime_watchdogs


class TestZReferenceEvidence:
    """The eject lane's ONE origin for "is this printer's Z frame still real?".

    Reads the durable ``z_reference_lost`` hold through the incident store's process
    cache (the same projection the printer card's chip renders), so the answer is
    identical at the automatic release boundary and in the operator's dialog."""

    @staticmethod
    def _snapshot(payload):
        from backend.app.services import printer_incidents

        return patch.object(printer_incidents, "snapshot", MagicMock(return_value=payload))

    async def test_an_open_z_reference_hold_reads_false(self):
        from backend.app.models.printer_incident import KIND_Z_REFERENCE_LOST

        with self._snapshot({"kind": KIND_Z_REFERENCE_LOST, "status": "escalated"}):
            assert remote.z_reference_evidence(7) is False

    async def test_no_incident_reads_unknown_never_true(self):
        # None, not True: the farm has no POSITIVE evidence that Z is referenced, and
        # inventing one would make the gate assert something it cannot know.
        with self._snapshot(None):
            assert remote.z_reference_evidence(7) is None

    async def test_another_kind_of_hold_does_not_gate_the_eject(self):
        # 2026-08-29 gotcha (d): the eject lane stays ungated by AMS faults — an eject
        # is filament-less, and holding the plate behind one deadlocks the printer.
        from backend.app.models.printer_incident import KIND_RUNOUT

        with self._snapshot({"kind": KIND_RUNOUT, "status": "escalated"}):
            assert remote.z_reference_evidence(7) is None

    async def test_the_live_evidence_builder_carries_it(self):
        from backend.app.models.printer_incident import KIND_Z_REFERENCE_LOST

        with (
            patch.object(printer_manager, "get_status", MagicMock(return_value=SimpleNamespace(state="IDLE"))),
            self._snapshot({"kind": KIND_Z_REFERENCE_LOST, "status": "escalated"}),
        ):
            ev = remote._live_evidence(7)
        assert ev.live_state == "IDLE"
        assert ev.z_reference is False


class TestTerminalRefusalVocabulary:
    """Terminal-ness is a property of the refusal VOCABULARY, set by ``_refusal_error``
    — never a flag a raiser chooses, which is how two raisers of one token come to
    disagree about whether waiting could help."""

    def test_the_z_refusal_is_terminal_and_carries_its_token(self):
        err = remote._refusal_error("z_unreferenced")
        assert err.terminal is True
        assert err.code == "z_unreferenced"
        assert err.status_code == 409
        assert "Z reference is lost" in str(err)
        assert "Mark plate cleared" in str(err)

    def test_transient_refusals_are_not_terminal(self):
        for token in ("job_active", "dispatch_in_flight", "eject_in_flight", "not_occupied"):
            assert remote._refusal_error(token).terminal is False, token

    def test_every_terminal_refusal_has_operator_copy(self):
        # The map is what the API hands the dialog; a terminal token with no sentence
        # would refuse the operator in a vocabulary only the state machine speaks.
        for token in remote._TERMINAL_REFUSALS:
            assert token in remote._EJECT_REFUSAL_MESSAGES

    def test_the_escalation_detail_lives_with_the_vocabulary(self):
        assert remote.terminal_refusal_detail("z_unreferenced") == "z_reference_lost_after_reboot"
        # An unmapped token pages under its own name rather than another's wording.
        assert remote.terminal_refusal_detail("some_future_token") == "some_future_token"

    def test_the_default_error_is_not_terminal(self):
        assert remote.EjectDispatchError("boom").terminal is False


class TestRedriveEjectStop:
    """Re-drive the ONE kill path for a pending whose stop never landed.

    2026-09-04: the watchdog fired while the printer was off the wire, BOTH stop sends
    returned False, the task exited — and nothing re-sent on reconnect. The pending and
    its ``runtime_exceeded_at`` stayed registered, so every later eject on that printer
    refused ``eject_in_flight`` forever."""

    @staticmethod
    def _stop_patch(*, delivered: bool = True):
        return patch.object(printer_manager, "stop_print", MagicMock(return_value=delivered))

    @staticmethod
    def _notify_patch():
        from backend.app.services.eject import monitor as monitor_mod

        return patch.object(monitor_mod, "notify_plate_not_empty", new_callable=AsyncMock)

    async def test_stamps_the_verdict_before_the_stop_goes_out(self):
        pid = 90200
        _claim(pid, _armed())
        mark_at_stop: list[object] = []

        def _stop(printer_id: int) -> bool:
            mark_at_stop.append(plate_occupancy.pending_eject_view(printer_id).runtime_exceeded_at)
            return True

        with (
            patch.object(printer_manager, "stop_print", MagicMock(side_effect=_stop)) as stop,
            self._notify_patch() as notify,
        ):
            assert await remote.redrive_eject_stop(pid, stage="power_loss") is True
        stop.assert_called_once_with(pid)
        assert mark_at_stop and mark_at_stop[0] is not None
        notify.assert_awaited_once()
        assert notify.await_args.kwargs["source_detail"] == "power_loss_eject_interrupted"

    async def test_an_undelivered_stop_is_retried_exactly_once(self):
        pid = 90201
        _claim(pid, _armed())
        sleep = _FakeSleep()
        with self._stop_patch(delivered=False) as stop, self._notify_patch():
            assert await remote.redrive_eject_stop(pid, stage="power_loss", sleep=sleep) is True
        assert stop.call_count == 2
        assert sleep.delays == [remote._STOP_RETRY_DELAY_S]

    async def test_it_is_idempotent_over_an_already_stamped_pending(self):
        # The outage shape exactly: the watchdog stamped, the stop could not be
        # delivered. Re-driving must NOT move the verdict time (first-write-wins) but
        # MUST redo the delivery — the stop is the part that failed.
        pid = 90202
        _claim(pid, _armed())
        stamped_at = datetime.now(timezone.utc) - timedelta(minutes=40)
        plate_occupancy.note_eject_runtime_exceeded(pid, stamped_at, "total")

        with self._stop_patch() as stop, self._notify_patch() as notify:
            assert await remote.redrive_eject_stop(pid, stage="power_loss") is True
        assert plate_occupancy.pending_eject_view(pid).runtime_exceeded_at == stamped_at
        stop.assert_called_once_with(pid)
        notify.assert_awaited_once()

    async def test_no_pending_means_nothing_to_stop_and_nothing_to_say(self):
        pid = 90203
        _gate(pid)
        with self._stop_patch() as stop, self._notify_patch() as notify:
            assert await remote.redrive_eject_stop(pid, stage="power_loss") is False
        stop.assert_not_called()
        notify.assert_not_awaited()

    async def test_it_never_resolves_the_eject_itself(self):
        """``farm_policy.on_terminal`` stays the ONE ``resolve_eject`` caller: the
        stopped job's own terminal resolves it ``unverified``. A second caller here
        would decide the plate's fate on a criterion other than the terminal."""
        pid = 90204
        _claim(pid, _armed())
        with self._stop_patch(), self._notify_patch():
            await remote.redrive_eject_stop(pid, stage="power_loss")
        assert plate_occupancy.pending_eject_view(pid) is not None
        assert "resolve_eject(" not in Path(remote.__file__).read_text(encoding="utf-8")

    async def test_the_power_loss_stage_invents_no_runtime_evidence(self):
        # Every other stage renders spans and deadlines. Here the machine was off the
        # wire, so any span would describe a job nobody was watching.
        diagnostic, detail = remote._stop_reason(
            "power_loss",
            fired_deadline_s=0.0,
            phase_elapsed_s=0.0,
            expected_s=0.0,
            drop_span_s=None,
            sweep_span_s=None,
        )
        assert diagnostic == "eject interrupted by a power loss; sweep unverified"
        assert detail == "power_loss_eject_interrupted"
        assert "budget" not in diagnostic and "deadline" not in diagnostic
