"""Tests for the shared part-present eject dispatcher (eject.remote).

Since the 2026-08-30 cut-over this module holds NO eject state of its own: the one
pending-eject record per printer lives in the ``plate_occupancy`` authority. Every test
therefore SEEDS through the authority's transitions — ``hydrate_plate`` + ``claim_for_eject``
for a LIVE record (the shape a dispatch leaves behind), ``hydrate_eject`` for one rebuilt
at startup — and ASSERTS through its queries (``pending_eject_view`` / ``eject_identity``).

What stays here are the two DISPATCHERS and what bounds a dispatched sweep: the RECORD's answer
to its ``eject_start`` (did the printer ever begin it?) with production's START bound as the
fallback, and the runtime watch — the bed-drop guard and production's WHOLE-JOB bound (restored
2026-10-12, operator ruling: "if it was self heal, it should stay self heal"). Both bounds count
the printer's ONLINE seconds on the record's clock (``printer_action._now``), which every test here
ties to its own fake clock (:func:`_record_clock`).
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
from backend.app.services import printer_action
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
from backend.tests._fixtures.printer_action import route_verbs

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _reset_occupancy():
    """Every test starts on an empty fleet and leaves no timer armed.

    ``reset_for_tests`` also un-wires every injected callable, so a transition driven
    from a test fans out into no persist, no broadcast, no kick and no watch."""
    plate_occupancy.reset_for_tests()
    yield
    for printer_id in list(remote._runtime_watchdogs):
        remote.cancel_eject_timers(printer_id)
    plate_occupancy.reset_for_tests()


# The fake clock the record's clock (``printer_action._now``) follows in this test, if any.
_RECORD_CLOCK: dict[str, "_FakeClock | None"] = {"clock": None}


@pytest.fixture(autouse=True)
def _record_clock(monkeypatch):
    """The record's clock — what the self-heal bounds count ONLINE seconds on — follows the test's
    :class:`_FakeClock` (the newest one built), so a bound passes exactly when the polling watch's own
    sleeps say it should; with no fake clock it is the wall clock."""
    _RECORD_CLOCK["clock"] = None
    wall = printer_action._now

    def now() -> datetime:
        clock = _RECORD_CLOCK["clock"]
        return wall() if clock is None else clock.wall()

    monkeypatch.setattr(printer_action, "_now", now)
    yield
    _RECORD_CLOCK["clock"] = None


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


async def _seed(db, source: Path, *, model: str = "H2S"):
    printer = Printer(name="RM", serial_number="RM1", ip_address="1.2.3.4", access_code="x", model=model)
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


def _assert_removed(remove: AsyncMock, printer_id: int, remote_path: str, why: str) -> None:
    """The abandonment exit called THE shared helper once, for this lane's own file."""
    remove.assert_awaited_once()
    printer, path = remove.await_args.args
    assert (printer.id, path) == (printer_id, remote_path)
    assert remove.await_args.kwargs == {"lane": "eject", "why": why}


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
            with c1, c2, c3, route_verbs(start=start):
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

    async def test_success_leaves_an_identity_linked_to_its_start_action(self, db_session):
        """The firmware silently ignores a ``project_file`` sent while it is busy — the 2026-08-30
        "8 consecutive eject 409s" shape. Every accepted dispatch therefore ends with the authority's
        identity carrying its start's RECORD id, which is what the record's answer to that start is
        matched against (``on_action_answered``) — never a deadline."""
        source = _make_source_3mf()
        try:
            printer, item = await _seed(db_session, source)
            _gate(printer.id)
            c1, c2, c3 = _ftp_patches()
            with c1, c2, c3, route_verbs(start=MagicMock(return_value=True)):
                await remote.dispatch_part_present_eject(
                    db_session, printer_id=printer.id, queue_item_id=item.id, purpose="production", run_id=None
                )
            identity = plate_occupancy.eject_identity(printer.id)
            assert identity is not None
            assert (identity.purpose, identity.queue_item_id) == ("production", item.id)
            assert identity.started_at is None
            assert plate_occupancy.pending_eject_view(printer.id).start_action_id is not None
            assert not hasattr(remote, "_start_deadlines")
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
                route_verbs(start=MagicMock(return_value=True)),
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
            remove = AsyncMock(return_value=True)
            with (
                c1,
                c2,
                c3,
                patch("backend.app.services.bambu_ftp.remove_abandoned_upload", remove),
                route_verbs(start=MagicMock(return_value=True)),
                pytest.raises(remote.EjectDispatchError) as exc,
            ):
                await remote.dispatch_part_present_eject(
                    db_session, printer_id=printer.id, queue_item_id=item.id, purpose="fa", run_id=1
                )
            assert exc.value.status_code == 502
            assert plate_occupancy.pending_eject_view(printer.id) is None
            # A failed or partial upload may still have left bytes: the lane removes its own file.
            _assert_removed(remove, printer.id, f"/eject_fa_item{item.id}.3mf", "the upload failed")
        finally:
            source.unlink(missing_ok=True)

    async def test_start_print_failure_raises_502_and_claims_nothing(self, db_session):
        source = _make_source_3mf()
        try:
            printer, item = await _seed(db_session, source)
            _gate(printer.id)
            c1, c2, c3 = _ftp_patches()
            remove = AsyncMock(return_value=True)
            with (
                c1,
                c2,
                c3,
                patch("backend.app.services.bambu_ftp.remove_abandoned_upload", remove),
                route_verbs(start=MagicMock(return_value=False)),
                pytest.raises(remote.EjectDispatchError) as exc,
            ):
                await remote.dispatch_part_present_eject(
                    db_session, printer_id=printer.id, queue_item_id=item.id, purpose="production", run_id=1
                )
            assert exc.value.status_code == 502
            # Nothing is claimed unless start_print was accepted.
            assert plate_occupancy.pending_eject_view(printer.id) is None
            _assert_removed(remove, printer.id, f"/eject_production_item{item.id}.3mf", "the start command was refused")
        finally:
            source.unlink(missing_ok=True)

    async def test_a_claim_refused_after_the_start_stops_the_sweep_and_removes_its_file(self, db_session):
        """The file IS on the printer and the firmware may act on it: the sweep is stopped and
        the upload removed, through the same shared helper as every other abandonment."""
        source = _make_source_3mf()
        try:
            printer, item = await _seed(db_session, source)
            _gate(printer.id)
            c1, c2, c3 = _ftp_patches()
            remove = AsyncMock(return_value=True)
            stop = MagicMock(return_value=True)
            with (
                c1,
                c2,
                c3,
                patch("backend.app.services.bambu_ftp.remove_abandoned_upload", remove),
                route_verbs(start=MagicMock(return_value=True)),
                route_verbs(stop=stop),
                patch.object(plate_occupancy, "claim_for_eject", MagicMock(return_value="dispatch_in_flight")),
                pytest.raises(remote.EjectDispatchError) as exc,
            ):
                await remote.dispatch_part_present_eject(
                    db_session, printer_id=printer.id, queue_item_id=item.id, purpose="production", run_id=1
                )
            assert exc.value.code == "dispatch_in_flight"
            stop.assert_called_once_with(printer.id)
            _assert_removed(
                remove,
                printer.id,
                f"/eject_production_item{item.id}.3mf",
                "the claim was refused (dispatch_in_flight)",
            )
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
                route_verbs(start=MagicMock(return_value=True)) as start,
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
                route_verbs(start=MagicMock(return_value=True)),
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
                route_verbs(start=MagicMock(return_value=True)),
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


class TestGeometryFailsClosedOnBothAutoLanes:
    """Red line 2: a model only ejects unattended once its envelope has been through the
    hardware ladder. Both dispatchers resolve geometry with ``require_validated=True``, and
    this is the UNATTENDED copy of that rule — the eject the farm dispatches with nobody
    watching. The eject conftest seeds ``H2C`` unvalidated and no ``P1S`` row at all, which
    is exactly the two causes :class:`GeometryUnavailable` distinguishes.

    The refusal must land BEFORE the build: an unvalidated envelope must never be turned
    into G-code at all, not merely withheld after it exists.
    """

    _CASES = [("H2C", "not hardware-validated"), ("P1S", "no eject geometry")]
    _IDS = ["a_row_that_never_passed_the_ladder", "a_model_with_no_row_at_all"]

    @pytest.mark.parametrize(("model", "fragment"), _CASES, ids=_IDS)
    async def test_the_unit_dispatcher_refuses_without_building(self, db_session, model, fragment):
        source = _make_source_3mf()
        try:
            printer, item = await _seed(db_session, source, model=model)
            _gate(printer.id)
            build = AsyncMock()
            c1, c2, c3 = _ftp_patches()
            with (
                c1,
                c2,
                c3,
                patch.object(remote, "build_part_present_eject_file", build),
                route_verbs(start=MagicMock(return_value=True)) as start,
                pytest.raises(remote.EjectDispatchError) as exc,
            ):
                await remote.dispatch_part_present_eject(
                    db_session, printer_id=printer.id, queue_item_id=item.id, purpose="production", run_id=1
                )
            assert exc.value.status_code == 409
            assert fragment in str(exc.value)
            build.assert_not_awaited()
            start.assert_not_called()
            assert plate_occupancy.pending_eject_view(printer.id) is None
        finally:
            source.unlink(missing_ok=True)

    @pytest.mark.parametrize(("model", "fragment"), _CASES, ids=_IDS)
    async def test_the_foreign_dispatcher_refuses_without_building(self, db_session, model, fragment):
        source = _make_source_3mf()
        try:
            printer, item = await _seed(db_session, source, model=model)
            _gate(printer.id)
            build = AsyncMock()
            c1, c2, c3 = _ftp_patches()
            with (
                c1,
                c2,
                c3,
                patch.object(remote, "build_part_present_eject_file", build),
                route_verbs(start=MagicMock(return_value=True)) as start,
                pytest.raises(remote.EjectDispatchError) as exc,
            ):
                await remote.dispatch_foreign_eject(
                    db_session,
                    printer_id=printer.id,
                    profile_id=item.eject_profile_id,
                    source_path=source,
                    plate_id=1,
                )
            assert exc.value.status_code == 409
            assert fragment in str(exc.value)
            build.assert_not_awaited()
            start.assert_not_called()
            assert plate_occupancy.pending_eject_view(printer.id) is None
        finally:
            source.unlink(missing_ok=True)

    async def test_a_validated_row_is_what_lets_the_same_call_through(self, db_session):
        """The negative cases above must fail on the geometry gate and nothing else: the
        identical arrangement on the validated H2S row reaches the build and claims."""
        source = _make_source_3mf()
        try:
            printer, item = await _seed(db_session, source)
            _gate(printer.id)
            c1, c2, c3 = _ftp_patches()
            with c1, c2, c3, route_verbs(start=MagicMock(return_value=True)):
                await remote.dispatch_part_present_eject(
                    db_session, printer_id=printer.id, queue_item_id=item.id, purpose="production", run_id=1
                )
            assert plate_occupancy.pending_eject_view(printer.id) is not None
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
            with c1, c2, c3, route_verbs(start=start):
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
        # against — that is the whole reason the name check exists.
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
        assert remote.expected_eject_stem(pe, 4) == "eject_production_item32"
        assert remote.expected_eject_stem(remote.PendingEject("fa", 1, 7), 4) == "eject_fa_item7"

    def test_expected_stem_covers_the_manual_sweep_by_printer_id(self):
        """The foreign-plate sweep has no unit: its stem is keyed by the PRINTER, and it is the
        same builder's answer (2026-10-05: three inline copies of this rule are gone)."""
        assert remote.expected_eject_stem(remote.PendingEject("manual", None, None), 4) == "eject_manual_p4"
        assert remote.parse_eject_job_name(remote.expected_eject_stem(PendingEject("manual", None, None), 42)) == (
            "manual",
            42,
        )

    def test_a_unit_bound_record_without_its_unit_names_nothing(self):
        assert remote.expected_eject_stem(remote.PendingEject("production", 1, None), 4) is None

    def test_a_dispatch_refuses_a_stem_it_cannot_name(self):
        with pytest.raises(remote.EjectDispatchError) as exc:
            remote._dispatch_stem(remote.PendingEject("production", 1, None), 4)
        assert exc.value.status_code == 409
        assert remote._dispatch_stem(remote.PendingEject("fa", 1, 7), 4) == "eject_fa_item7"

    def test_the_remote_path_is_the_dispatchers_naming_rule(self):
        assert remote._eject_remote_path("eject_production_item32") == "/eject_production_item32.3mf"

    def test_expected_stem_accepts_the_identity_projection(self):
        # The matcher only ever holds the projection, never the record.
        _claim(9010, PendingEject("production", 5, 32))
        identity = plate_occupancy.eject_identity(9010)
        assert remote.expected_eject_stem(identity, 9010) == "eject_production_item32"


class TestMatchesPendingEjectNameTightening:
    """The tightened matcher: a truthy subtask_name whose stem != the
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
            with c1, c2, c3, route_verbs(start=start):
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
            remove = AsyncMock(return_value=True)
            with (
                c1,
                c2,
                c3,
                patch("backend.app.services.bambu_ftp.remove_abandoned_upload", remove),
                route_verbs(start=MagicMock(return_value=True)),
                pytest.raises(remote.EjectDispatchError) as exc,
            ):
                await remote.dispatch_foreign_eject(
                    db_session, printer_id=printer.id, profile_id=prof.id, source_path=source, plate_id=1
                )
            assert exc.value.status_code == 502
            assert plate_occupancy.pending_eject_view(printer.id) is None
            _assert_removed(remove, printer.id, f"/eject_manual_p{printer.id}.3mf", "the upload failed")
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
                route_verbs(start=start),
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
                route_verbs(start=MagicMock(return_value=True)),
                pytest.raises(remote.EjectDispatchError) as exc,
            ):
                await remote.dispatch_part_present_eject(
                    db_session, printer_id=printer.id, queue_item_id=item.id, purpose="production", run_id=1
                )
            assert exc.value.status_code == 409
            assert "eject profile" in str(exc.value).lower()
        finally:
            source.unlink(missing_ok=True)


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
    """A started production pending (no phase budgets)."""
    kw.setdefault("expected_runtime_s", 83.0)
    kw.setdefault("started_at", datetime.now(timezone.utc) - timedelta(seconds=104))
    return remote.PendingEject("production", 1, pid_item, **kw)


class _FakeClock:
    """The ONE time source a polling-guard test drives.

    The guard's sleeps and its clock must come from the same source, so this supplies
    both: ``sleep`` records the delay, advances ``now`` by it and yields to the event
    loop so the test can interleave (cancelling mid-run, for one). No wall-clock time
    passes, so a whole drop sequence runs in microseconds."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.start = start
        self.delays: list[float] = []
        # The record's naive-UTC instant at ``start`` — real time, so a pending's tz-aware
        # ``started_at`` stamped just before or after lands on the same timeline.
        self.base_wall = datetime.now(timezone.utc).replace(tzinfo=None)
        _RECORD_CLOCK["clock"] = self

    def __call__(self) -> float:
        return self.now

    def wall(self) -> datetime:
        """The record's clock (naive UTC) at this fake clock's ``now``."""
        return self.base_wall + timedelta(seconds=self.now - self.start)

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        self.now += delay
        await asyncio.sleep(0)


class _ProgressFeed:
    """Scripted ``mc_percent`` samples, one consumed per telemetry read.

    Each entry is ``(percent, wire_age_s)``: what the live session reports and how old
    that push is when read. ``wire_age_s=None`` means the stamp PREDATES the arming — a
    value held over from before this eject, which the guard must treat as no evidence
    at all. The last entry repeats once exhausted, so a test scripts only the interesting
    part of the timeline. ``line_number`` is the breadcrumb the kill line carries."""

    def __init__(
        self, clock: _FakeClock, samples: list[tuple[float, float | None]], *, line_number: int | None = None
    ) -> None:
        self._clock = clock
        self._samples = list(samples)
        self._line_number = line_number
        self.reads = 0

    def __call__(self, printer_id: int) -> SimpleNamespace:
        percent, age = self._samples[min(self.reads, len(self._samples) - 1)]
        self.reads += 1
        wire_at = 0.0 if age is None else self._clock.now - age
        return SimpleNamespace(
            state=SimpleNamespace(mc_print_line_number=self._line_number, progress=percent, progress_wire_at=wire_at)
        )


def _armed_with_drop(pid_item: int = 5, *, drop_span_s: float = 30.0, **kw) -> remote.PendingEject:
    """A started production pending carrying a bed-drop budget (arms the drop guard).

    A 30 s drop span → an 8 s margin (the floor) → the drop deadline lands 38 s after the
    observed P5 edge."""
    kw.setdefault("expected_runtime_s", 83.0)
    kw.setdefault("started_at", datetime.now(timezone.utc))
    return remote.PendingEject("production", 1, pid_item, drop_span_s=drop_span_s, **kw)


def _armed_with_phases(
    pid_item: int = 5,
    *,
    drop_span_s: float = 30.0,
    sweep_span_s: float | None = 20.0,
    tail_s: float | None = 15.0,
    **kw,
) -> remote.PendingEject:
    """A started production pending carrying ALL the build's phase budgets.

    30 s drop → an 8 s margin (the floor) → the drop deadline lands 38 s after the observed
    P5 edge; 20 s sweep → the same 8 s floor → the sweep BUDGET line lands 28 s after the
    observed P50 edge (a line that only warns)."""
    kw.setdefault("expected_runtime_s", 83.0)
    kw.setdefault("started_at", datetime.now(timezone.utc))
    return remote.PendingEject(
        "production", 1, pid_item, drop_span_s=drop_span_s, sweep_span_s=sweep_span_s, tail_s=tail_s, **kw
    )


@contextlib.contextmanager
def _watchdog_env(feed: _ProgressFeed, *, stop_delivered: bool = True):
    """Patch every collaborator the fire path touches; yields the mocks."""
    from backend.app.services.eject import monitor as monitor_mod

    with (
        patch.object(printer_manager, "get_client", MagicMock(side_effect=feed)),
        route_verbs(stop=MagicMock(return_value=stop_delivered)) as stop,
        patch.object(printer_manager, "request_evidence_pushall", MagicMock(return_value=True)) as pushall,
        patch.object(monitor_mod, "notify_plate_not_empty", new_callable=AsyncMock) as notify,
    ):
        yield SimpleNamespace(stop=stop, pushall=pushall, notify=notify)


def _spawn_watchdog(pid: int, armed: remote.PendingEject, clock: _FakeClock) -> asyncio.Task:
    """Spawn the watch exactly as the arming path does (real registry lifecycle), on a printer whose
    session began at the clock's start (the record's online clock runs from its first fresh report)."""
    from backend.app.core.tasks import spawn_background_task

    assert clock.wall() == clock.base_wall, "spawn the watch before advancing the clock"
    printer_action._session_began(pid)

    task = spawn_background_task(
        remote._runtime_watchdog(pid, armed, sleep=clock.sleep, clock=clock), name=f"wd-edge-{pid}"
    )
    remote._runtime_watchdogs[pid] = task
    return task


async def _poll_then_cancel(pid: int, armed: remote.PendingEject, clock: _FakeClock, feed: _ProgressFeed, polls: int):
    """Run a guard that has nothing to decide for ``polls`` reads, then cancel it the way the eject's
    terminal does (``cancel_eject_timers``). Returns the task, cancelled — proof the guard was still
    WAITING, never deciding, when the terminal arrived."""
    task = _spawn_watchdog(pid, armed, clock)
    for _ in range(polls * 20):
        if feed.reads >= polls or task.done():
            break
        await asyncio.sleep(0)
    assert not task.done(), "the guard decided something where it should only have waited"
    remote.cancel_eject_timers(pid)
    with contextlib.suppress(asyncio.CancelledError):
        await task
    return task


def _kill_line(caplog) -> str:
    return next(r.getMessage() for r in caplog.records if "still running at" in r.getMessage())


def _records(caplog, needle: str) -> list:
    return [r for r in caplog.records if needle in r.getMessage()]


class TestEjectStartEcho:
    """The START echo is the first moment a deadline can be measured from — it stamps the whole-job
    bound's anchor and arms the runtime watch (production's arming gate: a verifiable sweep, one that
    carries its build's expected runtime)."""

    async def test_start_echo_stamps_once_and_arms_one_guard(self):
        pid = 90001
        _claim(pid, PendingEject("production", 1, 5, expected_runtime_s=82.0, drop_span_s=30.0))
        assert plate_occupancy.eject_identity(pid).started_at is None

        remote.on_eject_start_echo(pid)

        first = plate_occupancy.eject_identity(pid).started_at
        assert first is not None
        assert pid in remote._runtime_watchdogs
        armed_task = remote._runtime_watchdogs[pid]

        # A duplicate/replayed start echo must NOT restart the clock — that would
        # shorten a measured runtime — nor double-arm.
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

    async def test_a_rehydrated_pending_never_arms_a_watch_but_a_drop_less_sweep_does(self):
        # A rehydrated post-restart pending carries no build figures: nothing to judge against, so no
        # watch (the session start's reconcile owns it). A drop-less LIVE sweep has no drop to guard
        # but still carries its estimate: its whole-job bound is watched (production's single-deadline
        # form).
        hydrated, live = 90003, 90004
        plate_occupancy.hydrate_eject(hydrated, PendingEject("production", 1, 5))
        _claim(live, PendingEject("production", 1, 6, expected_runtime_s=82.0))

        remote.on_eject_start_echo(hydrated)
        remote.on_eject_start_echo(live)

        assert plate_occupancy.eject_identity(hydrated).started_at is not None  # still stamped
        assert plate_occupancy.eject_identity(live).started_at is not None
        assert hydrated not in remote._runtime_watchdogs
        assert live in remote._runtime_watchdogs


def _answered(pid: int, action_id: int, answer: str, *, command: str = "eject_start", reason: str | None = None):
    from backend.app.services.printer_events import ActionAnswered

    return ActionAnswered(
        printer_id=pid,
        action_id=action_id,
        command=command,
        actor="farm",
        incident_id=None,
        answer=answer,
        reason=reason,
    )


class TestEjectStartAnswer:
    """The 2026-08-30 "8 consecutive eject 409s" class (printer 4, 01:46-01:49), answered by the
    RECORD, never a deadline (2026-10-11).

    The firmware silently IGNORES a ``project_file`` sent while it is busy — so an eject dispatched
    into a print that had just started simply never happens. The record answers that shape (the busy
    job's end with the sweep's own id never reported is VOID), and a refused or never-received start
    likewise; that answer — however late — retires the claim. A start nobody has answered WAITS (the
    attention owner pages it)."""

    async def test_a_void_start_retires_the_claim_and_frees_the_printer(self):
        from backend.app.services.eject import monitor as monitor_mod

        pid = 90060
        _gate(pid, source="SUB-DEAD", policy=CooldownEject(unit_id=5, run_id=1))
        _claim(pid, PendingEject("production", 1, 5, expected_runtime_s=83.0, start_action_id=501))

        with patch.object(monitor_mod, "notify_plate_not_empty", new_callable=AsyncMock) as notify:
            await remote.on_action_answered(
                _answered(pid, 501, "void", reason="the printer was busy with another job and never started it")
            )

        # The dead pending is GONE — that is what unblocks every later eject...
        assert plate_occupancy.eject_identity(pid) is None
        # ...while the plate stays OCCUPIED, demoted to escalation-only: the sweep never
        # ran, so the part is still on the bed and only a human can say otherwise.
        view = plate_occupancy.snapshot(pid)
        assert view.plate_occupied is True
        assert isinstance(view.plate_policy, EscalationOnly)
        assert view.plate_source_subtask_id == "SUB-DEAD"
        notify.assert_awaited_once()
        detail = notify.await_args.kwargs["source_detail"]
        assert "never started" in detail and "busy with another job" in detail
        # The printer is ejectable again — the operator can simply eject now.
        assert plate_occupancy.ejectable(pid, Evidence()) is None

    @pytest.mark.parametrize("answer", ["refused", "void"])
    @pytest.mark.parametrize(
        "pending,stem",
        [
            (PendingEject("production", 1, 5, expected_runtime_s=83.0, start_action_id=502), "eject_production_item5"),
            (None, None),
        ],
    )
    async def test_a_start_that_never_ran_removes_its_file_named_by_the_one_builder(
        self, db_session, own_session_factory, pending, stem, answer
    ):
        """The retired record names the file through ``expected_eject_stem`` — the unit-bound stem, or
        the printer-keyed manual one (``pending=None`` stands for the manual record, whose stem needs
        the printer row's id). REFUSED and VOID both prove the file never ran."""
        from backend.app.services.eject import monitor as monitor_mod

        printer = Printer(name="SD", serial_number="SD1", ip_address="1.2.3.4", access_code="x", model="H2S")
        db_session.add(printer)
        await db_session.commit()
        await db_session.refresh(printer)
        if pending is None:
            pending = PendingEject("manual", None, None, expected_runtime_s=60.0, start_action_id=502)
            stem = f"eject_manual_p{printer.id}"
        _claim(printer.id, pending)
        remove = AsyncMock(return_value=True)

        with (
            patch.object(monitor_mod, "notify_plate_not_empty", new_callable=AsyncMock),
            patch("backend.app.core.database.async_session", own_session_factory),
            patch("backend.app.services.bambu_ftp.remove_abandoned_upload", remove),
        ):
            await remote.on_action_answered(_answered(printer.id, 502, answer))

        assert plate_occupancy.eject_identity(printer.id) is None
        _assert_removed(remove, printer.id, f"/{stem}.3mf", "the printer never started the sweep")

    async def test_a_taken_over_start_keeps_its_file(self):
        """Another job took the printer: the claim is retired and paged, but the file stays — the job
        now running may BE that file, started from the screen under a firmware-minted id."""
        from backend.app.services.eject import monitor as monitor_mod

        pid = 90063
        _claim(pid, PendingEject("production", 1, 5, expected_runtime_s=83.0, start_action_id=503))
        remove = AsyncMock(return_value=True)
        with (
            patch.object(monitor_mod, "notify_plate_not_empty", new_callable=AsyncMock) as notify,
            patch("backend.app.services.bambu_ftp.remove_abandoned_upload", remove),
        ):
            await remote.on_action_answered(_answered(pid, 503, "taken_over"))

        assert plate_occupancy.eject_identity(pid) is None
        notify.assert_awaited_once()
        assert "another job took the printer" in notify.await_args.kwargs["source_detail"]
        remove.assert_not_awaited()

    @pytest.mark.parametrize(
        "event",
        [
            _answered(90064, 504, "reached"),  # the sweep began: its echo armed the guard
            _answered(90064, 999, "void"),  # another action's answer
            _answered(90064, 504, "void", command="print_start"),  # another command family
        ],
        ids=["reached", "another_action", "another_command"],
    )
    async def test_an_answer_that_is_not_this_sweeps_never_retires_anything(self, event):
        from backend.app.services.eject import monitor as monitor_mod

        pid = 90064
        _claim(pid, PendingEject("production", 1, 5, expected_runtime_s=83.0, start_action_id=504))
        with patch.object(monitor_mod, "notify_plate_not_empty", new_callable=AsyncMock) as notify:
            await remote.on_action_answered(event)
        assert plate_occupancy.eject_identity(pid) is not None
        notify.assert_not_awaited()

    async def test_a_started_eject_is_never_retired_by_a_late_answer(self):
        # The authority, not the answer's arrival, decides: a sweep the printer started is the guard's
        # and its terminal's.
        from backend.app.services.eject import monitor as monitor_mod

        pid = 90065
        _claim(pid, PendingEject("production", 1, 5, expected_runtime_s=83.0, start_action_id=505))
        plate_occupancy.note_eject_started(pid)

        remove = AsyncMock(return_value=True)
        with (
            patch.object(monitor_mod, "notify_plate_not_empty", new_callable=AsyncMock) as notify,
            patch("backend.app.services.bambu_ftp.remove_abandoned_upload", remove),
        ):
            await remote.on_action_answered(_answered(pid, 505, "void"))

        assert plate_occupancy.eject_identity(pid) is not None
        notify.assert_not_awaited()
        remove.assert_not_awaited()  # a sweep that started keeps its file

    async def test_a_hydrated_eject_is_never_retired_by_an_answer(self):
        """A record rebuilt at startup carries no start action — the session start's reconcile owns
        it, decided against the printer's own report."""
        from backend.app.services.eject import monitor as monitor_mod

        pid = 90066
        _gate(pid)
        plate_occupancy.hydrate_eject(pid, PendingEject("production", 1, 5))

        with patch.object(monitor_mod, "notify_plate_not_empty", new_callable=AsyncMock) as notify:
            await remote.on_action_answered(_answered(pid, 506, "void"))

        identity = plate_occupancy.eject_identity(pid)
        assert identity is not None and identity.hydrated is True
        notify.assert_not_awaited()

    async def test_an_answer_that_beat_the_claim_is_applied_at_the_claim(self, db_session, own_session_factory):
        """A fail ACK can land inside the publish's own round trip — before the claim exists for the
        subscriber to retire. The dispatcher asks the record once more after claiming, so that answer
        is never lost and the claim never strands."""
        from backend.app.services import printer_action
        from backend.app.services.command_outcome import Answer
        from backend.app.services.eject import monitor as monitor_mod

        source = _make_source_3mf()
        try:
            printer, item = await _seed(db_session, source)
            _gate(printer.id)
            c1, c2, c3 = _ftp_patches()
            remove = AsyncMock(return_value=True)
            with (
                c1,
                c2,
                c3,
                route_verbs(start=MagicMock(return_value=True)),
                patch.object(printer_action, "settled", AsyncMock(return_value=Answer.REFUSED)),
                patch.object(printer_action, "reason_of", AsyncMock(return_value="the printer is busy")),
                patch.object(monitor_mod, "notify_plate_not_empty", new_callable=AsyncMock) as notify,
                patch("backend.app.services.bambu_ftp.remove_abandoned_upload", remove),
                patch("backend.app.core.database.async_session", own_session_factory),
            ):
                await remote.dispatch_part_present_eject(
                    db_session, printer_id=printer.id, queue_item_id=item.id, purpose="production", run_id=None
                )
            assert plate_occupancy.eject_identity(printer.id) is None
            assert isinstance(plate_occupancy.snapshot(printer.id).plate_policy, EscalationOnly)
            notify.assert_awaited_once()
            assert "the printer is busy" in notify.await_args.kwargs["source_detail"]
            remove.assert_awaited_once()
        finally:
            source.unlink(missing_ok=True)


class TestCancelEjectTimers:
    """THE one deregistration point, called by the occupancy policy driver on every
    transition that leaves the printer with no eject."""

    async def test_cancels_the_guard_and_is_idempotent(self):
        pid = 90070
        _claim(pid, PendingEject("production", 1, 5, expected_runtime_s=82.0, drop_span_s=30.0))
        remote.on_eject_start_echo(pid)
        watchdog = remote._runtime_watchdogs[pid]

        remote.cancel_eject_timers(pid)
        remote.cancel_eject_timers(pid)  # idempotent — never raises, never resurrects

        assert pid not in remote._runtime_watchdogs
        with contextlib.suppress(asyncio.CancelledError):
            await watchdog
        assert watchdog.cancelled()

    async def test_cancelling_when_nothing_is_armed_is_a_noop(self):
        remote.cancel_eject_timers(90071)  # must not raise

    async def test_retiring_the_eject_hands_the_driver_the_no_eject_view(self):
        """The level-trigger the monitor's driver cancels the guard on: whatever retires the eject (a
        matched terminal, an unverified resolve, a start the record answered never-started, the
        reconciler's disposal, an operator recover), the view it sees carries
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

    async def test_a_successor_sweep_gets_its_own_guard(self):
        """A stale guard must never judge the NEXT sweep — and the successor must not be left
        unguarded by a leftover registry entry."""
        pid = 90073
        _claim(pid, PendingEject("production", 1, 5, expected_runtime_s=82.0, drop_span_s=30.0))
        remote.on_eject_start_echo(pid)
        stale = remote._runtime_watchdogs[pid]

        assert plate_occupancy.resolve_eject(pid, "completed") is None
        remote.cancel_eject_timers(pid)
        with contextlib.suppress(asyncio.CancelledError):
            await stale
        assert stale.cancelled()

        _claim(pid, PendingEject("production", 1, 6, expected_runtime_s=82.0, drop_span_s=30.0))
        remote.on_eject_start_echo(pid)
        assert remote._runtime_watchdogs[pid] is not stale


class TestDropGuardFires:
    """Past the drop deadline the guard stops the machine MID-JOB — before the sweep can scrape the
    plate. The named exception's whole behaviour, unchanged since 2026-08-31."""

    async def test_marks_before_stopping_then_notifies_and_deregisters(self):
        from backend.app.services.eject import monitor as monitor_mod

        pid = 90010
        armed = _armed_with_drop()
        _claim(pid, armed)
        clock = _FakeClock()
        feed = _ProgressFeed(clock, [(5.0, 0.0)])
        mark_at_stop: list[object] = []

        def _stop(printer_id: int) -> bool:
            # Captured INSIDE the stop: a terminal racing the stop must already find
            # the mark set, or it would release the gate onto an unverified plate.
            mark_at_stop.append(plate_occupancy.pending_eject_view(printer_id).runtime_exceeded_at)
            return True

        with (
            patch.object(printer_manager, "get_client", MagicMock(side_effect=feed)),
            route_verbs(stop=MagicMock(side_effect=_stop)) as stop,
            patch.object(monitor_mod, "notify_plate_not_empty", new_callable=AsyncMock) as notify,
        ):
            await _spawn_watchdog(pid, armed, clock)
        stop.assert_called_once_with(pid)
        assert mark_at_stop and mark_at_stop[0] is not None
        assert plate_occupancy.pending_eject_view(pid).runtime_exceeded_at is not None
        notify.assert_awaited_once()
        detail = notify.await_args.kwargs["source_detail"]
        assert "STOPPED DURING the bed-drop" in detail and "30s" in detail
        assert pid not in remote._runtime_watchdogs  # deregistered on exit

    async def test_the_stop_arms_no_hold_of_its_own(self):
        """The escalation hold moved OUT of the kill path: while an eject owns the printer the plate
        carries no watch BY CONSTRUCTION. The hold arrives one step later, from the stopped sweep's
        ``unverified`` resolve."""
        from backend.app.services.eject import monitor as monitor_mod

        assert not hasattr(monitor_mod.eject_cooldown_monitor, "start_escalation_only_watch")

        pid = 90015
        armed = _armed_with_drop()
        _gate(pid, policy=CooldownEject(unit_id=5, run_id=1))
        _claim(pid, armed)
        driver = _DriverLog()
        plate_occupancy.configure(policy_driver=driver)
        clock = _FakeClock()

        with _watchdog_env(_ProgressFeed(clock, [(5.0, 0.0)])):
            await _spawn_watchdog(pid, armed, clock)

        assert driver.causes == ["eject_runtime_exceeded"]
        assert all(view.eject_present and view.plate_occupied for _pid, _c, view in driver.calls)

        assert plate_occupancy.resolve_eject(pid, "unverified") is None
        _pid, cause, view = driver.calls[-1]
        assert cause == "eject_unverified"
        assert view.eject_present is False
        assert view.plate_occupied is True
        assert isinstance(view.plate_policy, EscalationOnly)

    async def test_an_undelivered_stop_is_never_retried_on_a_clock(self):
        """A stop that found no live session is NOT retried after a delay (2026-10-11): the page says
        the truth, and the stamped pending is UNOWNED (``unowned_eject``) — exactly what the session
        start's reconcile re-drives the stop on, the moment the printer reports again."""
        pid = 90011
        armed = _armed_with_drop()
        _claim(pid, armed)
        clock = _FakeClock()
        with _watchdog_env(_ProgressFeed(clock, [(5.0, 0.0)]), stop_delivered=False) as env:
            await _spawn_watchdog(pid, armed, clock)
        assert env.stop.call_count == 1
        assert plate_occupancy.pending_eject_view(pid).runtime_exceeded_at is not None
        env.notify.assert_awaited_once()
        assert env.notify.await_args.kwargs["source_detail"] == remote._UNDELIVERED_STOP_DETAIL
        assert plate_occupancy.unowned_eject(pid) is True

    async def test_notify_failure_never_kills_the_guard(self):
        from backend.app.services.eject import monitor as monitor_mod

        pid = 90012
        armed = _armed_with_drop()
        _claim(pid, armed)
        clock = _FakeClock()
        feed = _ProgressFeed(clock, [(5.0, 0.0)])
        with (
            patch.object(printer_manager, "get_client", MagicMock(side_effect=feed)),
            route_verbs(stop=MagicMock(return_value=True)),
            patch.object(
                monitor_mod, "notify_plate_not_empty", new_callable=AsyncMock, side_effect=RuntimeError("smtp down")
            ),
        ):
            await _spawn_watchdog(pid, armed, clock)
        assert plate_occupancy.pending_eject_view(pid).runtime_exceeded_at is not None
        assert pid not in remote._runtime_watchdogs

    async def test_superseded_pending_stands_the_guard_down(self):
        # The eject this task was armed for resolved and a NEW one took the printer.
        pid = 90013
        armed = _armed_with_drop(5)
        _claim(pid, _armed_with_drop(6))
        clock = _FakeClock()
        with _watchdog_env(_ProgressFeed(clock, [(5.0, 0.0)])) as env:
            await _spawn_watchdog(pid, armed, clock)
        env.stop.assert_not_called()
        env.notify.assert_not_awaited()
        assert plate_occupancy.pending_eject_view(pid).runtime_exceeded_at is None  # successor unmarked
        assert pid not in remote._runtime_watchdogs

    async def test_resolved_pending_stands_the_guard_down(self):
        pid = 90014
        clock = _FakeClock()
        with _watchdog_env(_ProgressFeed(clock, [(5.0, 0.0)])) as env:
            await _spawn_watchdog(pid, _armed_with_drop(), clock)
        env.stop.assert_not_called()
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
    """The kill line must say WHERE the eject was when it was stopped (the 2026-08-14 009-H2S lesson).
    Log-only: no assertion here may become a stop condition."""

    async def _fire(self, pid: int, caplog, *, line_number, percent: float) -> str:
        armed = _armed_with_drop()
        _claim(pid, armed)
        clock = _FakeClock()
        feed = _ProgressFeed(clock, [(percent, 0.0)], line_number=line_number)
        with (
            _watchdog_env(feed),
            caplog.at_level(logging.WARNING, logger="backend.app.services.eject.remote"),
        ):
            await _spawn_watchdog(pid, armed, clock)
        return _kill_line(caplog)

    async def test_kill_line_carries_the_line_number_and_percent(self, caplog):
        message = await self._fire(90030, caplog, line_number=41207, percent=7.0)
        assert "gcode line 41207" in message
        assert "7.0% done" in message
        assert "expected 83s" in message and "stage=drop" in message and "deadline 40s" in message

    async def test_kill_line_tolerates_firmware_that_never_publishes_the_line_number(self, caplog):
        # mc_print_line_number is absent on the H2S wire: absent field, not absent printer. Percent
        # must still land so the breadcrumb is not all-or-nothing.
        message = await self._fire(90032, caplog, line_number=None, percent=12.0)
        assert "gcode line None" in message and "12.0% done" in message

    async def test_a_printer_with_no_live_session_is_never_stopped_by_the_drop_guard(self):
        # No session, no evidence: the guard cannot arm its drop window, so it never stops a sweep it
        # cannot see. (Only the whole-job bound, which needs no evidence, could — not inside 60 s.)
        pid = 90031
        armed = _armed_with_drop()
        _claim(pid, armed)
        clock = _FakeClock()
        reads = {"n": 0}

        def no_session(printer_id):
            reads["n"] += 1
            return None

        with (
            patch.object(printer_manager, "get_client", MagicMock(side_effect=no_session)),
            route_verbs(stop=MagicMock(return_value=True)) as stop,
        ):
            task = _spawn_watchdog(pid, armed, clock)
            for _ in range(400):
                if reads["n"] >= 30:
                    break
                await asyncio.sleep(0)
            assert not task.done()
            remote.cancel_eject_timers(pid)
            with contextlib.suppress(asyncio.CancelledError):
                await task
        stop.assert_not_called()

    async def test_telemetry_reader_tolerates_a_stateless_client(self):
        with patch.object(printer_manager, "get_client", MagicMock(return_value=SimpleNamespace())):
            assert remote._live_phase_telemetry(90033) == (None, None, None)

    async def test_telemetry_reader_carries_the_progress_recency_stamp(self):
        state = SimpleNamespace(mc_print_line_number=7, progress=42.0, progress_wire_at=1234.5)
        with patch.object(printer_manager, "get_client", MagicMock(return_value=SimpleNamespace(state=state))):
            assert remote._live_phase_telemetry(90034) == (7, 42.0, 1234.5)


class TestEjectMarginMath:
    """One clamp. The drop margin is measured from an OBSERVED beacon edge, so it pays for none of the
    upload / spin-up / start-echo variance."""

    async def test_drop_margin_floors_at_eight_and_caps_at_twenty(self):
        # The 8 s floor covers the estimator's ~±3 s, the ~1 Hz push cadence and the 2 s
        # poll — below that a healthy drop could be killed by sampling alone.
        assert remote._drop_margin_s(4.0) == 8.0
        assert remote._drop_margin_s(30.0) == 8.0  # 7.5 → floored
        assert remote._drop_margin_s(48.0) == 12.0
        assert remote._drop_margin_s(200.0) == 20.0


class TestRuntimeWatchdogPhaseEdges:
    """The pre-sweep guarantee (2026-08-15 009-H2S bed-drop stalls): a percent below the sweep beacon
    PROVES the sweep has not started, which is what makes stopping there safe. The whole-job bound
    (83 s → 103.75 s here, caught on the 104 s poll) is the evidence-free backstop behind it."""

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
        assert clock.now - 1000.0 == pytest.approx(42.0)

    async def test_sweep_beacon_arriving_after_the_drop_deadline_still_stops(self, caplog):
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

    async def test_healthy_phase_sequence_never_kills_and_ends_at_the_park_beacon(self, caplog):
        # 0 → 5 → 50 → 75: the drop cleared inside its budget, so no phase rule may fire, and past the
        # park beacon there is nothing left to guard — the guard ENDS on its own; the eject's terminal
        # answers the rest.
        pid = 90042
        armed = _armed_with_phases()
        _claim(pid, armed)
        clock = _FakeClock()
        feed = _ProgressFeed(clock, [(0.0, 0.0), (5.0, 0.0), (50.0, 0.0), (75.0, 0.0)])
        with (
            _watchdog_env(feed) as env,
            caplog.at_level(logging.INFO, logger="backend.app.services.eject.remote"),
        ):
            task = _spawn_watchdog(pid, armed, clock)
            await task
        env.stop.assert_not_called()
        env.notify.assert_not_awaited()
        # LIVENESS: the lane really ran the sequence.
        assert feed.reads >= 4
        assert _records(caplog, "bed-drop phase cleared")
        assert _records(caplog, "sweep phase cleared")
        assert not task.cancelled()
        assert pid not in remote._runtime_watchdogs
        assert plate_occupancy.pending_eject_view(pid) is not None  # the terminal ends the eject

    async def test_beacons_never_reflected_warn_once_then_the_whole_job_bound_stops_the_sweep(self, caplog):
        # mc_percent stuck at 0 while fresh: the firmware is publishing, but the beacons are not
        # landing in it. The drop guard cannot arm without P5: ONE warning at production's line (job
        # spin-up 25 s + the 20.75 s whole-job grace) — and the whole-job bound, which needs no
        # evidence, still stops the sweep at expected + margin (production's beacon-dead fallback).
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
        silent = _records(caplog, "phase beacons not reflected")
        assert len(silent) == 1
        assert "at 46s after the start echo" in silent[0].getMessage()
        env.stop.assert_called_once_with(pid)
        assert clock.now - clock.start == pytest.approx(104.0)
        assert "stage=total" in _kill_line(caplog)
        assert "STOPPED mid-job after" in env.notify.await_args.kwargs["source_detail"]
        assert plate_occupancy.pending_eject_view(pid).runtime_exceeded_at is not None

    async def test_stale_evidence_never_kills_and_asks_once_for_a_fresh_report(self, caplog):
        # One fresh sample opens the drop window, then the link goes quiet: every later sample is
        # stamped before this eject even armed. Stale evidence advances nothing and justifies no
        # stop — the guard asks for ONE pushall and keeps waiting.
        pid = 90044
        armed = _armed_with_drop()
        _claim(pid, armed)
        clock = _FakeClock()
        feed = _ProgressFeed(clock, [(5.0, 0.0), (5.0, None)])
        with _watchdog_env(feed) as env:
            await _poll_then_cancel(pid, armed, clock, feed, polls=50)  # 100 s: inside the whole-job bound
        env.pushall.assert_called_once_with(pid, remote._EVIDENCE_REASON)
        env.stop.assert_not_called()

    async def test_aged_out_sample_is_not_evidence_either(self):
        pid = 90045
        armed = _armed_with_drop()
        _claim(pid, armed)
        clock = _FakeClock()
        feed = _ProgressFeed(clock, [(5.0, 0.0), (5.0, remote._PROGRESS_FRESH_S + 30.0)])
        with _watchdog_env(feed) as env:
            await _poll_then_cancel(pid, armed, clock, feed, polls=50)
        env.stop.assert_not_called()

    async def test_superseded_pending_stands_the_edge_lane_down(self):
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


class TestNoDropPhase:
    """Without a drop budget there is nothing for the drop guard to bound, and no beacon lane: a
    drop-less sweep is watched in production's SINGLE-DEADLINE form — the whole-job bound until the
    terminal, with no beacon loosening it. A rehydrated pending (no build figures) is not watched."""

    async def test_a_rehydrated_pending_returns_without_polling_or_stopping(self):
        pid = 90050
        armed = remote.PendingEject("production", 1, 5, started_at=datetime.now(timezone.utc))
        _claim(pid, armed)
        clock = _FakeClock()
        with _watchdog_env(_ProgressFeed(clock, [(5.0, 0.0)])) as env:
            await _spawn_watchdog(pid, armed, clock)
        assert clock.delays == []
        env.stop.assert_not_called()
        assert pid not in remote._runtime_watchdogs

    async def test_a_drop_less_sweep_is_stopped_at_its_whole_job_bound_whatever_the_beacons_say(self, caplog):
        # Even a FRESH park-beacon sample does not end the single-deadline form (production: no edge
        # lane, no patience) — the bound passes at 103.75 s online and the 104 s poll stops the sweep.
        pid = 90052
        armed = _armed(started_at=datetime.now(timezone.utc))  # drop_span_s None, 83 s estimate
        _claim(pid, armed)
        clock = _FakeClock()
        feed = _ProgressFeed(clock, [(75.0, 0.0)])
        with (
            _watchdog_env(feed) as env,
            caplog.at_level(logging.WARNING, logger="backend.app.services.eject.remote"),
        ):
            await _spawn_watchdog(pid, armed, clock)
        env.stop.assert_called_once_with(pid)
        assert clock.now - clock.start == pytest.approx(104.0)
        assert "stage=total" in _kill_line(caplog)
        assert "against an expected 83s" in env.notify.await_args.kwargs["source_detail"]
        assert plate_occupancy.pending_eject_view(pid).runtime_exceeded_at is not None
        assert len(_records(caplog, "[self-heal]")) == 1
        assert "lane=eject whole-job" in _records(caplog, "[self-heal]")[0].getMessage()

    async def test_a_drop_less_sweep_that_finishes_inside_its_bound_is_never_stopped(self):
        # The terminal arrives at 100 s (the 2026-07-31 band: 80-83 s against 83 s): the occupancy
        # driver cancels the watch, which was still WAITING.
        pid = 90053
        armed = _armed(started_at=datetime.now(timezone.utc))
        _claim(pid, armed)
        clock = _FakeClock()
        feed = _ProgressFeed(clock, [(75.0, 0.0)])
        with _watchdog_env(feed) as env:
            task = _spawn_watchdog(pid, armed, clock)
            for _ in range(2000):
                if clock.now - clock.start >= 100.0:
                    break
                await asyncio.sleep(0)
            assert not task.done()
            remote.cancel_eject_timers(pid)
            with contextlib.suppress(asyncio.CancelledError):
                await task
        env.stop.assert_not_called()
        assert plate_occupancy.pending_eject_view(pid).runtime_exceeded_at is None


class TestALongDropBudget:
    async def test_a_long_drop_budget_is_still_guarded(self):
        # 100 s span + 20 s margin = 120 s, inside a 250 s whole-job bound (a 200 s estimate): the drop
        # guard arms on any span the build states, with no lead it must hold over the whole-job bound.
        pid = 90051
        armed = _armed_with_drop(drop_span_s=100.0, expected_runtime_s=200.0)
        _claim(pid, armed)
        clock = _FakeClock()
        with _watchdog_env(_ProgressFeed(clock, [(5.0, 0.0)])) as env:
            await _spawn_watchdog(pid, armed, clock)
        env.stop.assert_called_once_with(pid)
        assert clock.now - 1000.0 == pytest.approx(124.0)  # t5 (2 s) + 100 s + 20 s, caught on the next poll


class TestStopReasonCopy:
    """One origin for kill copy, two renderings. Only the timed stops (the whole-job bound and the drop
    guard) carry runtime figures."""

    async def test_a_whole_job_kill_mid_sweep_suspects_a_part_that_did_not_release(self):
        # A sweep budget in force means an on-time P50 cleared the drop: blaming the heatbed would be
        # a guess the wire contradicts (production's copy).
        diagnostic, detail = remote._stop_reason(
            "total", phase_elapsed_s=100.0, drop_span_s=30.0, deadline_s=103.75, expected_s=83.0, sweep_span_s=20.0
        )
        assert "whole-job deadline (104s)" in diagnostic and "did not release" in diagnostic
        assert "STOPPED at its whole-job deadline after 100s of sweeping" in detail
        assert "under the heatbed" not in detail

    async def test_a_whole_job_kill_with_no_phase_observed_keeps_the_obstruction_wording(self):
        diagnostic, detail = remote._stop_reason(
            "total", phase_elapsed_s=104.0, drop_span_s=None, deadline_s=103.75, expected_s=83.0
        )
        assert "under-bed obstruction" in diagnostic
        assert "STOPPED mid-job after 104s against an expected 83s" in detail and "under the heatbed" in detail

    async def test_a_drop_kill_names_the_bed_drop_budget_and_the_heatbed(self):
        diagnostic, detail = remote._stop_reason("drop", phase_elapsed_s=42.0, drop_span_s=30.0)
        assert "bed-drop" in diagnostic and "30s" in diagnostic
        assert "bed-drop" in detail and "under the heatbed" in detail

    async def test_a_drop_late_kill_says_the_sweep_started_after_the_overrun(self):
        diagnostic, detail = remote._stop_reason("drop_late", phase_elapsed_s=42.0, drop_span_s=30.0)
        assert "sweep beacon arrived only after" in diagnostic
        assert "overran" in detail and "under the heatbed" in detail

    @pytest.mark.parametrize("stage", ["service_hold", "reconnect"])
    async def test_the_untimed_stages_invent_no_runtime_evidence(self, stage):
        diagnostic, detail = remote._stop_reason(stage, phase_elapsed_s=0.0, drop_span_s=None)
        assert "budget" not in diagnostic and "deadline" not in detail
        assert "plate" in detail


class TestRuntimeWatchdogSweepLane:
    """The sweep phase MEASURES and never kills on its own budget (2026-08-31): a part that fails to
    release presents as a sweep overrun, which the WHOLE-JOB bound stops — production's stuck-part
    timing, restored 2026-10-12."""

    async def test_a_sweep_running_past_its_budget_warns_once_and_is_never_killed_by_that_budget(self, caplog):
        pid = 90060
        armed = _armed_with_phases()
        _claim(pid, armed)
        clock = _FakeClock()
        feed = _ProgressFeed(clock, [(5.0, 0.0), (50.0, 0.0)])
        with (
            _watchdog_env(feed) as env,
            caplog.at_level(logging.INFO, logger="backend.app.services.eject.remote"),
        ):
            await _poll_then_cancel(pid, armed, clock, feed, polls=50)  # 100 s: inside the whole-job bound
        warnings = _records(caplog, "sweep phase running")
        assert len(warnings) == 1
        assert "20s budget" in warnings[0].getMessage()
        assert warnings[0].levelno == logging.WARNING
        env.stop.assert_not_called()

    async def test_a_park_beacon_after_the_budget_line_ends_the_guard_with_a_warning(self, caplog):
        pid = 90062
        armed = _armed_with_phases(sweep_span_s=5.0)
        _claim(pid, armed)
        clock = _FakeClock()
        feed = _ProgressFeed(clock, [(5.0, 0.0), (50.0, 0.0)] + [(50.0, 0.0)] * 8 + [(75.0, 0.0)])
        with (
            _watchdog_env(feed) as env,
            caplog.at_level(logging.INFO, logger="backend.app.services.eject.remote"),
        ):
            await _spawn_watchdog(pid, armed, clock)
        cleared = _records(caplog, "sweep phase cleared")
        assert len(cleared) == 1
        assert cleared[0].levelno == logging.WARNING
        assert "OVERRAN" in cleared[0].getMessage()
        env.stop.assert_not_called()

    async def test_a_stale_park_beacon_never_ends_the_sweep_lane(self, caplog):
        pid = 90063
        armed = _armed_with_phases()
        _claim(pid, armed)
        clock = _FakeClock()
        feed = _ProgressFeed(clock, [(5.0, 0.0), (50.0, 0.0), (75.0, None)])
        with (
            _watchdog_env(feed) as env,
            caplog.at_level(logging.INFO, logger="backend.app.services.eject.remote"),
        ):
            await _poll_then_cancel(pid, armed, clock, feed, polls=45)
        assert not _records(caplog, "sweep phase cleared")
        assert not _records(caplog, "sweep phase running")  # the WARN is fresh-gated too
        env.stop.assert_not_called()

    async def test_without_a_sweep_budget_the_watch_still_waits_for_the_park_beacon(self, caplog):
        # No budget line to warn on, but the whole-job bound keeps binding until a fresh P75.
        pid = 90064
        armed = _armed_with_phases(sweep_span_s=None, tail_s=None)
        _claim(pid, armed)
        clock = _FakeClock()
        feed = _ProgressFeed(clock, [(5.0, 0.0), (50.0, 0.0), (60.0, 0.0), (75.0, 0.0)])
        with (
            _watchdog_env(feed) as env,
            caplog.at_level(logging.INFO, logger="backend.app.services.eject.remote"),
        ):
            await _spawn_watchdog(pid, armed, clock)
        assert _records(caplog, "bed-drop phase cleared")
        cleared = _records(caplog, "sweep phase cleared")
        assert len(cleared) == 1 and "budget none" in cleared[0].getMessage()
        assert not _records(caplog, "sweep phase running")
        assert clock.now - clock.start == pytest.approx(8.0)
        env.stop.assert_not_called()

    async def test_without_a_sweep_budget_a_stuck_sweep_is_still_stopped_at_the_bound(self, caplog):
        pid = 90065
        armed = _armed_with_phases(sweep_span_s=None, tail_s=None)
        _claim(pid, armed)
        clock = _FakeClock()
        feed = _ProgressFeed(clock, [(5.0, 0.0), (50.0, 0.0), (60.0, 0.0)])
        with (
            _watchdog_env(feed) as env,
            caplog.at_level(logging.WARNING, logger="backend.app.services.eject.remote"),
        ):
            await _spawn_watchdog(pid, armed, clock)
        env.stop.assert_called_once_with(pid)
        assert clock.now - clock.start == pytest.approx(104.0)
        assert "stage=total" in _kill_line(caplog)


class TestThePostSweepIsTheTerminals:
    """Past a FRESH park beacon no motion over the plate remains: the watch ends — the whole-job bound
    with it, production's rule — and the job's terminal, however long its firmware tail runs, is what
    ends the eject. 23 of the 24 beacon-era kills fired there, on jobs whose parts had physically been
    ejected; the epilogue deadline that made them stays deleted."""

    async def test_a_park_beacon_sample_never_produces_a_drop_late_kill(self, caplog):
        # PINS THE FALLTHROUGH TRAP: a 0.5 s drop budget (deadline t5 + 8.5 s), the link blind through
        # the sweep, and the first sample back is the PARK beacon at ~12 s. Read by the drop lane it
        # would be a drop_late kill; read by the phase that owns it, it is proof the sweep is over.
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
        env.stop.assert_not_called()

    async def test_the_guard_ends_on_the_first_fresh_park_sample(self, caplog):
        pid = 90071
        armed = _armed_with_phases()
        _claim(pid, armed)
        clock = _FakeClock()
        feed = _ProgressFeed(clock, [(5.0, 0.0), (50.0, 0.0), (75.0, 0.0)])
        with (
            _watchdog_env(feed),
            caplog.at_level(logging.INFO, logger="backend.app.services.eject.remote"),
        ):
            await _spawn_watchdog(pid, armed, clock)
        cleared = _records(caplog, "sweep phase cleared")
        assert len(cleared) == 1
        assert cleared[0].levelno == logging.INFO
        assert "in <=2.0s" in cleared[0].getMessage() and "budget 20s" in cleared[0].getMessage()

    async def test_a_job_slow_to_finish_after_the_sweep_is_never_stopped(self, caplog):
        # The old epilogue kill's shape: the sweep proven done, the firmware tail running long. The
        # watch has ended (a fresh P75 releases the whole-job bound); nothing stops the job; the plate
        # stays gated until its terminal.
        pid = 90073
        armed = _armed_with_phases()
        _claim(pid, armed)
        clock = _FakeClock()
        feed = _ProgressFeed(clock, [(5.0, 0.0), (50.0, 0.0), (75.0, 0.0)])
        with (
            _watchdog_env(feed) as env,
            caplog.at_level(logging.INFO, logger="backend.app.services.eject.remote"),
        ):
            await _spawn_watchdog(pid, armed, clock)
        env.stop.assert_not_called()
        assert plate_occupancy.pending_eject_view(pid).runtime_exceeded_at is None
        assert plate_occupancy.is_plate_occupied(pid)
        assert pid not in remote._runtime_watchdogs


class _ScriptedClock(_FakeClock):
    """A fake clock that runs scripted acts (``(seconds since start, async act)``) once its own
    sleeps carry it past each one — how a test drops and restores the printer's session mid-sweep."""

    def __init__(self, script) -> None:
        super().__init__()
        self._script = sorted(script, key=lambda step: step[0])

    async def sleep(self, delay: float) -> None:
        await super().sleep(delay)
        while self._script and self.now - self.start >= self._script[0][0]:
            _at, act = self._script.pop(0)
            await act()


async def _eject_terminal(db, printer_id: int, final_status: str):
    """Drive the eject's matched terminal through ``farm_policy.on_terminal`` — the ONE resolver —
    and return its ``_finalize_remote_eject`` spy."""
    from backend.app.services import farm_policy
    from backend.app.services.eject import monitor as monitor_mod

    with (
        patch.object(
            farm_policy.printer_manager, "get_client", return_value=SimpleNamespace(last_dispatch_subtask_id="SUB-E")
        ),
        patch.object(farm_policy, "quarantine_printer", new_callable=AsyncMock),
        patch.object(farm_policy, "_finalize_remote_eject", new_callable=AsyncMock) as finalize,
        patch.object(farm_policy, "_maybe_idle_deep_park", new_callable=AsyncMock),
        patch.object(monitor_mod, "notify_plate_not_empty", new_callable=AsyncMock),
    ):
        await farm_policy.on_terminal(db, printer_id, None, final_status, completed_subtask_id="SUB-E")
    return finalize


class TestWholeJobBound:
    """R20 — production's WHOLE-JOB deadline, restored (2026-10-12, operator ruling: "The behaviour
    should stay the same for the most part … so if it was self heal, it should stay self heal").

    The 2026-07-31 gouged plate (012-H2S): a stalled bed-drop ran 179 s against the 80-83 s band of
    11 measured ejects (83 s estimate), still echoed ``completed`` and released the gate onto the
    damaged plate. The bound is expected + clamp(25 %, 20 s, 60 s) of the printer's ONLINE seconds
    from the PRINT START echo; past it the sweep is STOPPED under ``"total"``, the plate stays gated,
    and a FINISH that lands after the stop clears nothing. Here: 83 s → 103.75 s, caught on the
    104 s poll."""

    async def test_the_bound_is_productions_figure_with_its_witness(self):
        assert (remote.EJECT_ABORT_MARGIN_FRAC, remote.EJECT_ABORT_MARGIN_MIN_S, remote.EJECT_ABORT_MARGIN_MAX_S) == (
            0.25,
            20.0,
            60.0,
        )
        assert remote.eject_abort_deadline_s(83.0) == pytest.approx(103.75)
        assert remote.eject_abort_deadline_s(40.0) == pytest.approx(60.0)  # the 20 s floor
        assert remote.eject_abort_deadline_s(600.0) == pytest.approx(660.0)  # the 60 s cap
        bound = remote._whole_job_bound(83.0)
        assert isinstance(bound, printer_action.SelfHealBound)
        assert (bound.name, bound.seconds) == ("_EJECT_WHOLE_JOB_SELF_HEAL", pytest.approx(103.75))
        assert "2026-07-31" in bound.witness and "80-83 s" in bound.witness

    async def test_slow_firmware_a_sweep_finishing_just_inside_the_deadline_is_never_stopped(self, caplog):
        # The sweep crawls (P75 only at 102 s, past the old 83 s estimate): one poll inside the bound,
        # the fresh park beacon ends the watch and nothing is stopped.
        pid = 90300
        armed = _armed_with_phases()
        _claim(pid, armed)
        clock = _FakeClock()
        feed = _ProgressFeed(clock, [(5.0, 0.0), (50.0, 0.0)] + [(60.0, 0.0)] * 48 + [(75.0, 0.0)])
        with (
            _watchdog_env(feed) as env,
            caplog.at_level(logging.INFO, logger="backend.app.services.eject.remote"),
        ):
            await _spawn_watchdog(pid, armed, clock)
        assert clock.now - clock.start == pytest.approx(102.0)
        env.stop.assert_not_called()
        env.notify.assert_not_awaited()
        assert _records(caplog, "sweep phase cleared")
        assert not _records(caplog, "[self-heal]")
        assert plate_occupancy.pending_eject_view(pid).runtime_exceeded_at is None

    async def test_an_overrun_is_stopped_at_the_bound_with_the_plate_gated_and_a_late_finish_clears_nothing(
        self, db_session, caplog
    ):
        # The stuck-part shape: the drop cleared on budget, the sweep never reaches the park beacon.
        pid = 90301
        armed = _armed_with_phases()
        _claim(pid, armed)
        clock = _FakeClock()
        feed = _ProgressFeed(clock, [(5.0, 0.0), (50.0, 0.0), (60.0, 0.0)])
        mark_at_stop: list[object] = []

        def _stop(printer_id: int) -> bool:
            mark_at_stop.append(plate_occupancy.pending_eject_view(printer_id).runtime_exceeded_at)
            return True

        from backend.app.services.eject import monitor as monitor_mod

        with (
            patch.object(printer_manager, "get_client", MagicMock(side_effect=feed)),
            route_verbs(stop=MagicMock(side_effect=_stop)) as stop,
            patch.object(monitor_mod, "notify_plate_not_empty", new_callable=AsyncMock) as notify,
            caplog.at_level(logging.WARNING, logger="backend.app.services.eject.remote"),
        ):
            await _spawn_watchdog(pid, armed, clock)
        stop.assert_called_once_with(pid)
        assert clock.now - clock.start == pytest.approx(104.0)
        assert mark_at_stop and mark_at_stop[0] is not None  # stamped BEFORE the stop went out
        assert "stage=total" in _kill_line(caplog)
        detail = notify.await_args.kwargs["source_detail"]
        assert "STOPPED at its whole-job deadline after 100s of sweeping" in detail
        healed = _records(caplog, "[self-heal]")
        assert len(healed) == 1
        assert "lane=eject whole-job" in healed[0].getMessage()
        assert "_EJECT_WHOLE_JOB_SELF_HEAL" in healed[0].getMessage()
        assert pid not in remote._runtime_watchdogs

        # The printer then reports FINISH for the stopped sweep: the mark keeps the plate gated.
        finalize = await _eject_terminal(db_session, pid, "completed")
        view = plate_occupancy.snapshot(pid)
        assert view.eject_present is False
        assert view.plate_occupied is True
        assert isinstance(view.plate_policy, EscalationOnly)
        finalize.assert_not_awaited()

    async def test_time_offline_does_not_count_toward_the_bound(self, caplog):
        # The printer drops off the wire 50 s into the sweep and is back at 110 s: those 60 s are not
        # online, so the bound passes at 163.75 s of wall time — the 164 s poll — not at 104 s.
        from backend.app.services.printer_events import SessionLost

        pid = 90302
        armed = _armed_with_phases()
        _claim(pid, armed)

        async def lose() -> None:
            printer_action.on_session_lost(SessionLost(printer_id=pid))

        async def back() -> None:
            await printer_action.reread_session(pid, 2)

        clock = _ScriptedClock([(50.0, lose), (110.0, back)])
        feed = _ProgressFeed(clock, [(5.0, 0.0), (50.0, 0.0), (60.0, 0.0)])
        with (
            _watchdog_env(feed) as env,
            caplog.at_level(logging.WARNING, logger="backend.app.services.eject.remote"),
        ):
            await _spawn_watchdog(pid, armed, clock)
        env.stop.assert_called_once_with(pid)
        assert clock.now - clock.start == pytest.approx(164.0)
        assert "stage=total" in _kill_line(caplog)

    async def test_liveness_a_normal_eject_completes_and_clears_the_gate(self, db_session):
        # The base journey: drop and sweep inside their budgets (80 s, the 2026-07-31 band), a fresh
        # park beacon ends the watch, and the matched FINISH clears the plate for the next unit.
        pid = 90303
        armed = _armed_with_phases()
        _claim(pid, armed)
        clock = _FakeClock()
        feed = _ProgressFeed(
            clock,
            [(0.0, 0.0)] * 5 + [(5.0, 0.0)] + [(20.0, 0.0)] * 12 + [(50.0, 0.0)] + [(60.0, 0.0)] * 9 + [(75.0, 0.0)],
        )
        with _watchdog_env(feed) as env:
            task = _spawn_watchdog(pid, armed, clock)
            await task
        assert not task.cancelled()
        env.stop.assert_not_called()
        assert clock.now - clock.start == pytest.approx(58.0)

        await _eject_terminal(db_session, pid, "completed")
        view = plate_occupancy.snapshot(pid)
        assert view.eject_present is False
        assert view.plate_occupied is False


class TestEjectStartBound:
    """R30 — production's START deadline, restored as the record's self-heal bound (2026-10-12).

    The 2026-08-30 shape: a busy printer silently ignores the ``project_file``, so the sweep never
    starts and its claim wedges every later eject behind ``eject_in_flight``. The record's answer
    decides first (a VOID / REFUSED / TAKEN_OVER start retires at once — :class:`TestEjectStartAnswer`);
    the fallback is ``_EJECT_START_SELF_HEAL``, 180 s of the printer's ONLINE time from the send (the
    fleet's start echo is ~45 s: 44/43 s measured 2026-08-30). Driven through the real dispatcher, the
    record's own bound evaluation (``printer_action.check_bounds``) and the hub's ``SelfHealDue``."""

    @pytest.fixture
    def hub(self):
        from backend.app.services import printer_events
        from backend.app.services.printer_events import SelfHealDue

        unsubscribe = printer_events.subscribe(SelfHealDue, remote.on_self_heal_due)
        yield
        unsubscribe()

    @staticmethod
    async def _drain() -> None:
        for _ in range(5):
            pending = [
                task
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task() and (task.get_name() or "").startswith("printer-action")
            ]
            if not pending:
                await asyncio.sleep(0)
                continue
            await asyncio.gather(*pending, return_exceptions=True)

    async def _dispatch(self, db_session, own_session_factory, source: Path):
        """A dispatched, claimed, NOT started sweep on a printer whose session began at the clock's start."""
        printer, item = await _seed(db_session, source)
        _gate(printer.id, source="SUB-GATE", policy=CooldownEject(unit_id=item.id, run_id=None))
        clock = _FakeClock()
        printer_action._session_began(printer.id)
        c1, c2, c3 = _ftp_patches()
        with (
            c1,
            c2,
            c3,
            route_verbs(start=MagicMock(return_value=True)),
            patch("backend.app.core.database.async_session", own_session_factory),
        ):
            await remote.dispatch_part_present_eject(
                db_session, printer_id=printer.id, queue_item_id=item.id, purpose="production", run_id=None
            )
        pending = plate_occupancy.pending_eject_view(printer.id)
        assert pending is not None and pending.started_at is None
        assert printer_action.outstanding(pending.start_action_id)
        self.item_id = item.id
        return printer, clock, pending.start_action_id

    def _expiry_patches(self, own_session_factory):
        from backend.app.services.eject import monitor as monitor_mod

        notify = AsyncMock()
        remove = AsyncMock(return_value=True)
        return (
            notify,
            remove,
            (
                patch.object(monitor_mod, "notify_plate_not_empty", notify),
                patch("backend.app.services.bambu_ftp.remove_abandoned_upload", remove),
                patch("backend.app.core.database.async_session", own_session_factory),
            ),
        )

    async def test_the_bound_is_productions_figure_with_its_witness(self):
        bound = remote._EJECT_START_SELF_HEAL
        assert (bound.name, bound.seconds) == ("_EJECT_START_SELF_HEAL", 180.0)
        assert "44/43 s measured 2026-08-30" in bound.witness and "EJECT_START_TIMEOUT_S" in bound.witness

    async def test_no_echo_by_180s_online_expires_pages_and_removes_the_file(
        self, db_session, own_session_factory, hub, caplog
    ):
        source = _make_source_3mf()
        try:
            printer, clock, action_id = await self._dispatch(db_session, own_session_factory, source)
            notify, remove, patches = self._expiry_patches(own_session_factory)
            with patches[0], patches[1], patches[2], caplog.at_level(logging.WARNING):
                clock.now += 179.0
                printer_action.check_bounds()
                await self._drain()
                assert plate_occupancy.eject_identity(printer.id) is not None  # not yet
                notify.assert_not_awaited()

                clock.now += 1.0
                printer_action.check_bounds()
                await self._drain()

            # The claim is gone (every later eject is free again) and the plate stays a person's.
            assert plate_occupancy.eject_identity(printer.id) is None
            view = plate_occupancy.snapshot(printer.id)
            assert view.plate_occupied is True
            assert isinstance(view.plate_policy, EscalationOnly)
            assert plate_occupancy.ejectable(printer.id, Evidence()) is None
            notify.assert_awaited_once()
            assert notify.await_args.kwargs["source_detail"].startswith(
                "the eject sweep was dispatched but the printer never started it within 180s — the plate has "
                "NOT been swept."
            )
            _assert_removed(
                remove,
                printer.id,
                remote._eject_remote_path(f"eject_production_item{self.item_id}"),
                "the printer never started the sweep",
            )
            assert any("never echoed a PRINT START for its eject within 180s" in r.getMessage() for r in caplog.records)
            assert any(
                r.getMessage().startswith("[self-heal]") and "_EJECT_START_SELF_HEAL" in r.getMessage()
                for r in caplog.records
            )
            # The action is NOT answered by the clock: a late answer still lands on the record.
            assert printer_action.outstanding(action_id)
        finally:
            source.unlink(missing_ok=True)

    async def test_slow_firmware_a_start_echo_at_150s_is_honoured(self, db_session, own_session_factory, hub):
        source = _make_source_3mf()
        try:
            printer, clock, _action_id = await self._dispatch(db_session, own_session_factory, source)
            notify, remove, patches = self._expiry_patches(own_session_factory)
            with patches[0], patches[1], patches[2]:
                clock.now += 150.0
                remote.on_eject_start_echo(printer.id)  # the sweep began, late
                clock.now += 30.0
                printer_action.check_bounds()  # the bound passes over a STARTED sweep
                await self._drain()
            identity = plate_occupancy.eject_identity(printer.id)
            assert identity is not None and identity.started_at is not None  # the authority refused
            notify.assert_not_awaited()
            remove.assert_not_awaited()
            assert printer.id in remote._runtime_watchdogs  # the started sweep is watched
        finally:
            source.unlink(missing_ok=True)

    async def test_time_offline_does_not_count_toward_the_start_bound(self, db_session, own_session_factory, hub):
        from backend.app.services.bambu_mqtt import CommandAck
        from backend.app.services.printer_events import CommandAcked, SessionLost

        source = _make_source_3mf()
        try:
            printer, clock, action_id = await self._dispatch(db_session, own_session_factory, source)
            # The printer accepted the frame (a busy printer ACKs it and then ignores it), so the new
            # session's re-read does not void the start.
            printer_action.on_command_acked(
                CommandAcked(
                    printer_id=printer.id,
                    ack=CommandAck(
                        command="project_file", sequence_id=str(action_id), result="success", reason=None, at=0.0
                    ),
                )
            )
            notify, remove, patches = self._expiry_patches(own_session_factory)
            with patches[0], patches[1], patches[2]:
                clock.now += 100.0
                printer_action.on_session_lost(SessionLost(printer_id=printer.id))
                clock.now += 100.0  # offline
                await printer_action.reread_session(printer.id, 2)
                clock.now += 79.0  # 179 s online, 279 s of wall time
                printer_action.check_bounds()
                await self._drain()
                assert plate_occupancy.eject_identity(printer.id) is not None
                notify.assert_not_awaited()

                clock.now += 1.0
                printer_action.check_bounds()
                await self._drain()
            assert plate_occupancy.eject_identity(printer.id) is None
            notify.assert_awaited_once()
        finally:
            source.unlink(missing_ok=True)

    async def test_a_late_echo_after_the_expiry_takes_productions_path(self, db_session, own_session_factory, hub):
        # The sweep starts after all: no record is left to stamp, so nothing is armed and the plate
        # stays gated for the person the expiry paged.
        source = _make_source_3mf()
        try:
            printer, clock, _action_id = await self._dispatch(db_session, own_session_factory, source)
            _notify, _remove, patches = self._expiry_patches(own_session_factory)
            with patches[0], patches[1], patches[2]:
                clock.now += 180.0
                printer_action.check_bounds()
                await self._drain()
            assert plate_occupancy.eject_identity(printer.id) is None

            remote.on_eject_start_echo(printer.id)

            assert plate_occupancy.eject_identity(printer.id) is None
            assert printer.id not in remote._runtime_watchdogs
            view = plate_occupancy.snapshot(printer.id)
            assert view.plate_occupied is True and isinstance(view.plate_policy, EscalationOnly)
        finally:
            source.unlink(missing_ok=True)

    @pytest.mark.parametrize(
        ("command", "action_id", "bound_name"),
        [
            ("print_start", 701, "_EJECT_START_SELF_HEAL"),  # another family
            ("eject_start", 999, "_EJECT_START_SELF_HEAL"),  # another action
            ("eject_start", 701, "_SOME_OTHER_SELF_HEAL"),  # another bound
        ],
        ids=["another_command", "another_action", "another_bound"],
    )
    async def test_a_self_heal_that_is_not_this_sweeps_start_retires_nothing(self, command, action_id, bound_name):
        from backend.app.services.eject import monitor as monitor_mod
        from backend.app.services.printer_events import SelfHealDue

        pid = 90310
        _claim(pid, PendingEject("production", 1, 5, expected_runtime_s=83.0, start_action_id=701))
        with patch.object(monitor_mod, "notify_plate_not_empty", new_callable=AsyncMock) as notify:
            await remote.on_self_heal_due(
                SelfHealDue(
                    printer_id=pid,
                    action_id=action_id,
                    command=command,
                    bound_name=bound_name,
                    incident_id=None,
                    queue_item_id=5,
                )
            )
        assert plate_occupancy.eject_identity(pid) is not None
        notify.assert_not_awaited()

    async def test_liveness_a_start_echoed_in_45s_runs_its_sweep(self, db_session, own_session_factory, hub):
        # The fleet's measured shape: the echo at ~45 s stamps the start and arms the watch; the
        # bound has nothing to retire.
        source = _make_source_3mf()
        try:
            printer, clock, _action_id = await self._dispatch(db_session, own_session_factory, source)
            notify, _remove, patches = self._expiry_patches(own_session_factory)
            with patches[0], patches[1], patches[2]:
                clock.now += 45.0
                remote.on_eject_start_echo(printer.id)
                clock.now += 135.0
                printer_action.check_bounds()
                await self._drain()
            assert plate_occupancy.eject_identity(printer.id).started_at is not None
            assert printer.id in remote._runtime_watchdogs
            notify.assert_not_awaited()
        finally:
            source.unlink(missing_ok=True)


class TestZReferenceEvidence:
    """The eject lane's ONE origin for "is this printer's Z frame still real?".

    Reads the durable ``z_reference_lost`` hold through the incident store's process
    cache (the same projection the printer card's chip renders), so the answer is
    identical at the automatic release boundary and in the operator's dialog. Seeded
    through the REAL cache rather than a mocked ``snapshot``: since 2026-09-11 a
    printer can hold several rows and the evidence is asked BY KIND, which a mock
    that ignores its ``kind`` argument cannot pin."""

    @staticmethod
    def _held(printer_id, *kinds):
        from backend.app.services import printer_incidents

        printer_incidents._reset_state()
        printer_incidents._open_cache[printer_id] = {
            n: {"id": n, "kind": kind, "status": "escalated", "slot_desc": None, "created_at": None}
            for n, kind in enumerate(kinds, start=1)
        }

    @pytest.fixture(autouse=True)
    def _clean_cache(self):
        from backend.app.services import printer_incidents

        printer_incidents._reset_state()
        yield
        printer_incidents._reset_state()

    async def test_an_open_z_reference_hold_reads_false(self):
        from backend.app.models.printer_incident import KIND_Z_REFERENCE_LOST

        self._held(7, KIND_Z_REFERENCE_LOST)
        assert remote.z_reference_evidence(7) is False

    async def test_no_incident_reads_unknown_never_true(self):
        # None, not True: the farm has no POSITIVE evidence that Z is referenced, and
        # inventing one would make the gate assert something it cannot know.
        assert remote.z_reference_evidence(7) is None

    async def test_another_kind_of_hold_does_not_gate_the_eject(self):
        # 2026-08-29 gotcha (d): the eject lane stays ungated by AMS faults — an eject
        # is filament-less, and holding the plate behind one deadlocks the printer.
        from backend.app.models.printer_incident import KIND_RUNOUT

        self._held(7, KIND_RUNOUT)
        assert remote.z_reference_evidence(7) is None

    async def test_a_z_hold_beside_an_ams_fault_still_refuses(self):
        """Multi-alarm rule (2026-09-11): the runout ranks higher on the chip, and a
        single-slot read used to hand back the runout and let the sweep through —
        the 2026-09-04 bed-past-the-floor mechanism. Asked by kind, the jam cannot
        hide the Z hold."""
        from backend.app.models.printer_incident import KIND_RUNOUT, KIND_Z_REFERENCE_LOST

        self._held(7, KIND_RUNOUT, KIND_Z_REFERENCE_LOST)
        assert remote.z_reference_evidence(7) is False

    async def test_the_live_evidence_builder_carries_it(self):
        from backend.app.models.printer_incident import KIND_Z_REFERENCE_LOST

        self._held(7, KIND_Z_REFERENCE_LOST)
        with patch.object(printer_manager, "get_status", MagicMock(return_value=SimpleNamespace(state="IDLE"))):
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
        return route_verbs(stop=MagicMock(return_value=delivered))

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
            route_verbs(stop=MagicMock(side_effect=_stop)) as stop,
            self._notify_patch() as notify,
        ):
            assert await remote.redrive_eject_stop(pid, stage="power_loss") is True
        stop.assert_called_once_with(pid)
        assert mark_at_stop and mark_at_stop[0] is not None
        notify.assert_awaited_once()
        assert notify.await_args.kwargs["source_detail"] == "power_loss_eject_interrupted"

    async def test_an_undelivered_stop_is_not_retried_on_a_clock(self):
        # Sent once; the page says the truth, and the stamped pending stays unowned — the session
        # start's reconcile re-drives it on the printer's next report (2026-10-11).
        pid = 90201
        _claim(pid, _armed())
        with self._stop_patch(delivered=False) as stop, self._notify_patch() as notify:
            assert await remote.redrive_eject_stop(pid, stage="power_loss") is True
        assert stop.call_count == 1
        assert notify.await_args.kwargs["source_detail"] == remote._UNDELIVERED_STOP_DETAIL
        assert plate_occupancy.unowned_eject(pid) is True

    async def test_it_is_idempotent_over_an_already_stamped_pending(self):
        # The outage shape exactly: the watchdog stamped, the stop could not be
        # delivered. Re-driving must NOT move the verdict time (first-write-wins) but
        # MUST redo the delivery — the stop is the part that failed.
        pid = 90202
        _claim(pid, _armed())
        stamped_at = datetime.now(timezone.utc) - timedelta(minutes=40)
        plate_occupancy.note_eject_runtime_exceeded(pid, stamped_at, "drop")

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
        diagnostic, detail = remote._stop_reason("power_loss", phase_elapsed_s=0.0, drop_span_s=None)
        assert diagnostic == "eject interrupted by a power loss; sweep unverified"
        assert detail == "power_loss_eject_interrupted"
        assert "budget" not in diagnostic and "deadline" not in diagnostic


def _armed_with_reference(pid_item: int = 5, *, reference_s: float = 19.5, **kw) -> remote.PendingEject:
    """A started pending whose block carries the Z re-reference phase.

    A 19.5 s drive → the 8 s margin floor → its own budget line lands at overhead + 19.5 + 8
    after the start echo, and the beacon-silence warning not before overhead + 19.5 + the
    20.75 s whole-job grace of the 83 s estimate."""
    kw.setdefault("expected_runtime_s", 83.0)
    kw.setdefault("started_at", datetime.now(timezone.utc))
    kw.setdefault("drop_span_s", 30.0)
    kw.setdefault("sweep_span_s", 20.0)
    kw.setdefault("tail_s", 15.0)
    return remote.PendingEject("production", 1, pid_item, reference_s=reference_s, **kw)


class TestRuntimeWatchdogReferencePhase:
    """The guarded Z re-reference drive's own lane (2026-09-04).

    It MEASURES and WARNs; it never kills — two independent reasons, either sufficient.
    EVIDENCE: no P2 span has been observed on hardware (the recipe is dormant until a
    ladder opens it), and at the FIRST beacon a stuck percent cannot be told apart from
    firmware that never reflects M73. MECHANISM (decisive): a stop there lands inside the
    prologue's own unguarded window — soft end stops off, frame not yet declared — and
    the drive moves the bed AWAY from the nozzle, so there is no plate contact to
    pre-empt and nothing is bought by stopping early."""

    @staticmethod
    async def _run(pid: int, armed, samples, caplog, *, level=logging.INFO):
        _claim(pid, armed)
        clock = _FakeClock()
        feed = _ProgressFeed(clock, samples)
        with (
            _watchdog_env(feed) as env,
            caplog.at_level(level, logger="backend.app.services.eject.remote"),
        ):
            await _spawn_watchdog(pid, armed, clock)
        return env, clock

    async def test_a_block_without_the_phase_opens_where_it_always_did(self, caplog):
        # reference_s None ⇒ the lane is DISARMED, not armed with a zero-length deadline.
        env, _clock = await self._run(90080, _armed_with_phases(), [(5.0, 0.0), (50.0, 0.0), (75.0, 0.0)], caplog)
        assert _records(caplog, "Z re-reference") == []
        assert _records(caplog, "bed-drop phase cleared")

    async def test_the_drive_clearing_advances_to_the_existing_prologue_phase(self, caplog):
        env, _clock = await self._run(
            90081, _armed_with_reference(), [(2.0, 0.0), (5.0, 0.0), (50.0, 0.0), (75.0, 0.0)], caplog
        )
        cleared = _records(caplog, "Z re-reference cleared")
        assert len(cleared) == 1
        assert cleared[0].levelno == logging.INFO
        assert "budget 20s" in cleared[0].getMessage()  # 19.5 s, rendered %.0f
        # ...and the phases after it run exactly as they do without the prologue.
        assert _records(caplog, "bed-drop phase cleared")

    async def _run_waiting(self, pid: int, armed, samples, caplog, *, polls: int, level=logging.WARNING):
        _claim(pid, armed)
        clock = _FakeClock()
        feed = _ProgressFeed(clock, samples)
        with (
            _watchdog_env(feed) as env,
            caplog.at_level(level, logger="backend.app.services.eject.remote"),
        ):
            await _poll_then_cancel(pid, armed, clock, feed, polls=polls)
        return env, clock

    async def test_a_drive_past_its_budget_warns_once_and_never_kills_there(self, caplog):
        # Percent parked at 0: the drive's budget line passes and NOTHING may fire there — the
        # whole-job bound (103.75 s) is the only backstop, and it has not passed at 100 s.
        env, _clock = await self._run_waiting(90082, _armed_with_reference(), [(0.0, 0.0)], caplog, polls=50)
        warnings = _records(caplog, "Z re-reference running")
        assert len(warnings) == 1
        assert "19s budget" in warnings[0].getMessage() or "20s budget" in warnings[0].getMessage()
        assert "no phase kill fires here" in warnings[0].getMessage()
        env.stop.assert_not_called()

    async def test_dead_beacons_warn_once_and_leave_the_stop_to_the_whole_job_bound(self, caplog):
        # Never a kill at the first beacon: a stuck percent there is indistinguishable from firmware
        # that does not reflect M73. ONE warning — and the whole-job bound, which needs no evidence,
        # stops the sweep at expected + margin (production's beacon-dead fallback).
        armed = _armed_with_reference()
        env, clock = await self._run(90083, armed, [(0.0, 0.0)], caplog, level=logging.WARNING)
        fallback = _records(caplog, "not reflected in mc_percent")
        assert len(fallback) == 1
        assert "cannot arm without the P5 beacon" in fallback[0].getMessage()
        env.stop.assert_called_once_with(90083)
        assert clock.now - clock.start == pytest.approx(104.0)
        assert "stage=total" in _kill_line(caplog)

    async def test_the_beacon_silence_warning_is_pushed_out_by_the_drive(self, caplog):
        # Without this, enabling the prologue would look like beacon-death on EVERY job: the drive
        # genuinely delays P5, so its budget moves the warning line out by exactly the drive.
        armed = _armed_with_reference()
        env, _clock = await self._run_waiting(90087, armed, [(2.0, 0.0)], caplog, polls=40)
        fallback = _records(caplog, "not reflected in mc_percent")
        assert len(fallback) == 1
        at_s = float(fallback[0].getMessage().split(" at ")[1].split("s after")[0])
        assert at_s >= remote.EJECT_RUNTIME_OVERHEAD_S + 19.5 + remote._abort_margin_s(83.0)
        env.stop.assert_not_called()

    async def test_a_stale_sample_never_advances_the_phase(self, caplog):
        # FRESH-only, like every other lane: a percent published before this eject
        # started is not evidence that its drive completed.
        env, _clock = await self._run_waiting(90084, _armed_with_reference(), [(2.0, None)], caplog, polls=45)
        assert _records(caplog, "Z re-reference cleared") == []

    async def test_going_blind_asks_for_evidence_once_rather_than_judging_on_silence(self, caplog):
        env, _clock = await self._run_waiting(90085, _armed_with_reference(), [(0.0, None)], caplog, polls=45)
        env.pushall.assert_called_once_with(90085, remote._EVIDENCE_REASON)

    async def test_the_healthy_sequence_still_reaches_the_park_beacon(self, caplog):
        # LIVENESS: the added phase must not strand the poller short of the lanes that
        # come after it (the 2026-08-08 lesson — a cured storm and a starved deadlock
        # look identical on absence metrics).
        env, _clock = await self._run(
            90086, _armed_with_reference(), [(2.0, 0.0), (5.0, 0.0), (50.0, 0.0), (75.0, 0.0)], caplog
        )
        assert _records(caplog, "Z re-reference cleared")
        assert _records(caplog, "bed-drop phase cleared")
        assert _records(caplog, "sweep phase cleared")
        env.stop.assert_not_called()


class TestTheCommandedPlateIsTheBuiltPlate:
    """What the build PACKED and what the firmware is TOLD are one value.

    The 2026-09-17 incident (005-H2S) lived in the gap between them: the dispatcher
    re-read ``item.plate_id or 1`` for ``start_print`` while the builder had resolved a
    G-code member with a first-member fallback, so a donor that did not carry plate 3
    was uploaded with the sweep in ``plate_1.gcode`` and a ``project_file`` naming
    ``Metadata/plate_3.gcode``. The printer answered "the content of print file is
    unreadable" (HMS ``0500_0003`` / ``0500_4003``) and never started.
    """

    @staticmethod
    def _multi_plate_3mf(plates: list[int]) -> Path:
        fd, name = tempfile.mkstemp(suffix=".gcode.3mf")
        os.close(fd)
        path = Path(name)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for plate in plates:
                zf.writestr(f"Metadata/plate_{plate}.gcode", _PLATE_GCODE)
                zf.writestr(f"Metadata/plate_{plate}.gcode.md5", "STALE")
            zf.writestr("3D/3dmodel.model", "<model/>")
        return path

    async def test_the_units_plate_is_packed_and_commanded(self, db_session):
        """A unit that printed plate 3 of a 4-plate donor: the uploaded container holds
        the sweep in ``plate_3.gcode`` and ``start_print`` commands plate 3."""
        source = self._multi_plate_3mf([1, 2, 3, 4])
        uploaded: dict[str, bytes] = {}

        async def _capture(_ip, _code, eject_path, _remote, **_kwargs):
            with zipfile.ZipFile(eject_path, "r") as zf:
                uploaded.update({name: zf.read(name) for name in zf.namelist() if name.endswith(".gcode")})
            return True

        try:
            printer, item = await _seed(db_session, source)
            item.plate_id = 3
            await db_session.commit()
            _gate(printer.id)
            start = MagicMock(return_value=True)
            with (
                patch.object(printer_manager, "is_connected", return_value=True),
                patch(
                    "backend.app.services.bambu_ftp.get_ftp_retry_settings", AsyncMock(return_value=(False, 0, 0, 30))
                ),
                patch("backend.app.services.bambu_ftp.upload_file_async", AsyncMock(side_effect=_capture)),
                route_verbs(start=start),
            ):
                await remote.dispatch_part_present_eject(
                    db_session, printer_id=printer.id, queue_item_id=item.id, purpose="production", run_id=7
                )
            assert start.call_args.kwargs["plate_id"] == 3, "the firmware is told the plate that was packed"
            packed = uploaded["Metadata/plate_3.gcode"].decode("utf-8")
            assert "FARM EJECT BLOCK" in packed
            assert "FARM EJECT BLOCK" not in uploaded["Metadata/plate_1.gcode"].decode("utf-8")
        finally:
            source.unlink(missing_ok=True)

    async def test_a_donor_without_the_units_plate_is_a_409(self, db_session):
        """The donor resolver refuses a file that cannot supply the unit's plate, and
        this lane turns that into the precondition failure the operator sees — never a
        sweep built on some other plate."""
        source = self._multi_plate_3mf([1])
        try:
            printer, item = await _seed(db_session, source)
            item.plate_id = 3  # the re-bound-library shape: the file is a stranger
            await db_session.commit()
            _gate(printer.id)
            c1, c2, c3 = _ftp_patches()
            with (
                c1,
                c2,
                c3,
                route_verbs(start=MagicMock(return_value=True)) as start,
                pytest.raises(remote.EjectDispatchError) as exc,
            ):
                await remote.dispatch_part_present_eject(
                    db_session, printer_id=printer.id, queue_item_id=item.id, purpose="production", run_id=7
                )
            assert exc.value.status_code == 409
            start.assert_not_called()
            assert plate_occupancy.pending_eject_view(printer.id) is None, "nothing claimed"
        finally:
            source.unlink(missing_ok=True)
