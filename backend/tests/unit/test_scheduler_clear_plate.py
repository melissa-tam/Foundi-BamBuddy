"""Tests for the plate gate in the print scheduler.

The gate itself moved on 2026-08-30: ``printer_manager`` used to own it as an
in-memory set plus a DB writer plus a loader, which made it the second of five
stores answering "who owns this printer". It is now ONE fact on the
``plate_occupancy`` authority, and ``printer_manager.is_awaiting_plate_clear`` is a
thin projection of it. Every raise and clear in this file therefore drives a named
transition on the authority; the behaviours pinned are unchanged.

One behaviour is deliberately INVERTED, and it is why the cut-over exists. The
dispatch path used to call ``set_awaiting_plate_clear(False)`` unconditionally
immediately before ``start_print``. On 2026-08-30 at 01:06:57 an operator's
``declare_occupied`` on printer 4 landed between the scheduler's claim (01:06:56.8)
and its ``start_print`` (01:06:59.8) and was simply erased, and the unit printed
onto the declared plate. A dispatch does not get to decide that a plate is empty;
it only gets to be REFUSED by one that is not. So a healthy dispatch now writes
NOTHING to the plate, and a declaration under an in-flight lease revokes it —
``TestHealthyDispatchDoesNotTouchThePlate`` and
``TestDeclareDuringDispatch`` respectively.
"""

import contextlib
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services import print_scheduler as ps_module
from backend.app.services.plate_occupancy import (
    CooldownEject,
    EscalationOnly,
    Evidence,
    plate_occupancy,
)
from backend.app.services.print_scheduler import PrintScheduler
from backend.app.services.printer_manager import PrinterManager, printer_manager
from backend.app.services.spool_selection import MatchOutcome
from backend.app.utils.filename import derive_remote_filename


@pytest.fixture(autouse=True)
def _clean_authority():
    """Every test starts with an empty fleet and un-wired callables."""
    plate_occupancy.reset_for_tests()
    yield
    plate_occupancy.reset_for_tests()


def _raise_gate(printer_id: int, *, subtask_id: str | None = None, policy=None) -> None:
    """Put a deposit on the plate the way startup hydration does — no fan-out noise."""
    plate_occupancy.hydrate_plate(printer_id, subtask_id, policy or EscalationOnly())


class TestPrinterManagerPlateProjection:
    """``is_awaiting_plate_clear`` is a projection of the authority's plate fact.

    There is no setter on the manager any more: the flag is raised and cleared by
    named transitions (``declare_occupied`` / ``note_terminal`` / ``clear_plate``),
    and the projection is what the fourteen existing readers keep using.
    """

    @pytest.fixture
    def manager(self):
        return PrinterManager()

    def test_plate_cleared_initially_false(self, manager):
        """No printers should have a deposit on the plate by default."""
        assert not manager.is_awaiting_plate_clear(1)
        assert not manager.is_awaiting_plate_clear(999)

    def test_declaring_the_plate_occupied_raises_the_projection(self, manager):
        """An operator declaration makes is_awaiting_plate_clear return True."""
        assert plate_occupancy.declare_occupied(1, Evidence()) is None
        assert manager.is_awaiting_plate_clear(1)
        assert plate_occupancy.is_plate_occupied(1)
        assert not manager.is_awaiting_plate_clear(2)

    def test_clearing_the_plate_drops_the_projection(self, manager):
        """Clearing the plate resets the flag."""
        plate_occupancy.declare_occupied(1, Evidence())
        assert manager.is_awaiting_plate_clear(1)
        assert plate_occupancy.clear_plate(1) is None
        assert not manager.is_awaiting_plate_clear(1)

    def test_clearing_a_clear_plate_refuses_rather_than_raising(self, manager):
        """Clearing when nothing is on the plate is a refusal token, never an
        exception — the controller owns the reason → copy map."""
        assert plate_occupancy.clear_plate(1) == "not_occupied"
        assert not manager.is_awaiting_plate_clear(1)

    def test_the_plate_is_tracked_per_printer(self, manager):
        plate_occupancy.declare_occupied(1, Evidence())
        plate_occupancy.declare_occupied(3, Evidence())
        assert manager.is_awaiting_plate_clear(1)
        assert not manager.is_awaiting_plate_clear(2)
        assert manager.is_awaiting_plate_clear(3)

    def test_clearing_only_affects_the_target_printer(self, manager):
        plate_occupancy.declare_occupied(1, Evidence())
        plate_occupancy.declare_occupied(2, Evidence())
        plate_occupancy.clear_plate(1)
        assert not manager.is_awaiting_plate_clear(1)
        assert manager.is_awaiting_plate_clear(2)

    def test_the_manager_owns_no_writer_for_the_gate(self, manager):
        """The five-store collapse, asserted as an ABSENCE: a second writer here is
        how the gate came to have 5 raise sites and 8 clear sites in the first
        place."""
        for gone in (
            "set_awaiting_plate_clear",
            "load_awaiting_plate_clear_from_db",
            "_persist_awaiting_plate_clear",
            "_awaiting_plate_clear",
        ):
            assert not hasattr(manager, gone), f"{gone} must not come back onto PrinterManager"


class TestAwaitingPlateClearSurvivesARestart:
    """#961: a gate raised before a restart must still hold the queue afterwards.

    The DURABLE half moved with the gate — ``plate_occupancy_store`` is the persist
    callable and the startup hydrator, and the round-trip through the
    ``printers.awaiting_plate_clear`` column is pinned in its own suite
    (``services/test_plate_occupancy_store.py``: ``TestPersistCallable`` and
    ``TestHydratePlate``). What this file still owns is the CONSEQUENCE: a hydrated
    plate reads as occupied on every surface the scheduler and the UI use.
    """

    def test_a_hydrated_plate_reads_occupied_on_every_surface(self):
        manager = PrinterManager()
        _raise_gate(1, subtask_id="task-abc")

        assert plate_occupancy.is_plate_occupied(1) is True
        assert manager.is_awaiting_plate_clear(1) is True
        assert plate_occupancy.plate_source(1) == "task-abc"
        assert isinstance(plate_occupancy.snapshot(1).plate_policy, EscalationOnly)
        # A printer that was clear at shutdown stays clear.
        assert plate_occupancy.is_plate_occupied(2) is False
        assert manager.is_awaiting_plate_clear(2) is False

    def test_hydration_keeps_the_policy_that_says_what_happens_next(self):
        """Rebuilding the plate is only worth doing if what should happen to it is
        rebuilt with it — an armless gate is the 2026-07-18/07-21 class."""
        _raise_gate(1, subtask_id="task-abc", policy=CooldownEject(unit_id=7, run_id=3))

        policy = plate_occupancy.snapshot(1).plate_policy
        assert isinstance(policy, CooldownEject)
        assert policy.unit_id == 7


class TestSchedulerIdleCheckWithPlateOccupied:
    """``_is_printer_idle`` asks the authority whether the printer may take work."""

    @pytest.fixture
    def scheduler(self):
        return PrintScheduler()

    @patch("backend.app.services.print_scheduler.printer_manager")
    def test_idle_state_is_idle(self, mock_pm, scheduler):
        """IDLE state with a clear plate → idle."""
        mock_pm.is_connected.return_value = True
        mock_pm.is_quarantined.return_value = False
        mock_pm.is_model_mismatch.return_value = False
        mock_pm.get_status.return_value = MagicMock(state="IDLE", hms_errors=[])
        assert scheduler._is_printer_idle(1) is True

    @patch("backend.app.services.print_scheduler.printer_manager")
    def test_running_state_not_idle(self, mock_pm, scheduler):
        """RUNNING state is never idle."""
        mock_pm.is_connected.return_value = True
        mock_pm.is_quarantined.return_value = False
        mock_pm.is_model_mismatch.return_value = False
        mock_pm.get_status.return_value = MagicMock(state="RUNNING", hms_errors=[])
        assert scheduler._is_printer_idle(1) is False

    @patch("backend.app.services.print_scheduler.printer_manager")
    def test_finish_state_not_idle_when_plate_occupied(self, mock_pm, scheduler):
        """FINISH + a deposit on the plate → NOT idle."""
        mock_pm.is_connected.return_value = True
        mock_pm.is_quarantined.return_value = False
        mock_pm.is_model_mismatch.return_value = False
        mock_pm.get_status.return_value = MagicMock(state="FINISH", hms_errors=[])
        _raise_gate(1)
        assert scheduler._is_printer_idle(1) is False

    @patch("backend.app.services.print_scheduler.printer_manager")
    def test_finish_state_idle_when_plate_cleared(self, mock_pm, scheduler):
        """FINISH with the plate cleared → idle."""
        mock_pm.is_connected.return_value = True
        mock_pm.is_quarantined.return_value = False
        mock_pm.is_model_mismatch.return_value = False
        mock_pm.get_status.return_value = MagicMock(state="FINISH", hms_errors=[])
        _raise_gate(1)
        assert plate_occupancy.clear_plate(1) is None
        assert scheduler._is_printer_idle(1) is True

    @patch("backend.app.services.print_scheduler.printer_manager")
    def test_failed_state_not_idle_when_plate_occupied(self, mock_pm, scheduler):
        """FAILED + a deposit → NOT idle."""
        mock_pm.is_connected.return_value = True
        mock_pm.is_quarantined.return_value = False
        mock_pm.is_model_mismatch.return_value = False
        mock_pm.get_status.return_value = MagicMock(state="FAILED", hms_errors=[])
        _raise_gate(1)
        assert scheduler._is_printer_idle(1) is False

    @patch("backend.app.services.print_scheduler.printer_manager")
    def test_failed_state_idle_when_plate_cleared(self, mock_pm, scheduler):
        """FAILED with the plate cleared → idle."""
        mock_pm.is_connected.return_value = True
        mock_pm.is_quarantined.return_value = False
        mock_pm.is_model_mismatch.return_value = False
        mock_pm.get_status.return_value = MagicMock(state="FAILED", hms_errors=[])
        assert scheduler._is_printer_idle(1) is True

    @patch("backend.app.services.print_scheduler.printer_manager")
    def test_idle_state_not_idle_when_gate_survives_power_cycle(self, mock_pm, scheduler):
        """Regression for #961: after Auto Off power-cycles the printer it boots into
        IDLE with no memory of the previous finish. The rebuilt plate record must
        still gate the queue — IDLE + occupied → NOT idle.
        """
        mock_pm.is_connected.return_value = True
        mock_pm.is_quarantined.return_value = False
        mock_pm.is_model_mismatch.return_value = False
        mock_pm.get_status.return_value = MagicMock(state="IDLE", hms_errors=[])
        _raise_gate(1)
        assert scheduler._is_printer_idle(1) is False

    @patch("backend.app.services.print_scheduler.printer_manager")
    def test_disconnected_printer_not_idle(self, mock_pm, scheduler):
        mock_pm.is_connected.return_value = False
        assert scheduler._is_printer_idle(1) is False

    @patch("backend.app.services.print_scheduler.printer_manager")
    def test_no_status_not_idle(self, mock_pm, scheduler):
        mock_pm.is_connected.return_value = True
        mock_pm.is_quarantined.return_value = False
        mock_pm.is_model_mismatch.return_value = False
        mock_pm.get_status.return_value = None
        assert scheduler._is_printer_idle(1) is False

    @patch("backend.app.services.print_scheduler.printer_manager")
    @pytest.mark.parametrize("state", ["FINISH", "FAILED", "IDLE"])
    def test_gate_blocks_unconditionally(self, mock_pm, scheduler, state):
        """Phase 1 (P1-B): the plate gate is UNCONDITIONAL — there is no
        require_plate_clear parameter to bypass it. Any idle-shaped state with a
        deposit on the plate is NOT idle. (The global toggle now only governs whether
        the gate is RAISED, via the terminal disposition's ``raise_gate`` — not
        whether the scheduler honours it.)"""
        mock_pm.is_connected.return_value = True
        mock_pm.is_quarantined.return_value = False
        mock_pm.is_model_mismatch.return_value = False
        mock_pm.get_status.return_value = MagicMock(state=state, hms_errors=[])
        _raise_gate(1)
        assert scheduler._is_printer_idle(1) is False

    @patch("backend.app.services.print_scheduler.printer_manager")
    def test_a_db_claim_blocks_dispatch_during_the_idle_to_running_lag(self, mock_pm, scheduler):
        """``db_claim`` is the caller's per-tick "a print_queue row on this printer
        already reads printing" evidence — the only witness that a unit is on its way
        while the wire still says IDLE. Optional keyword: existing positional callers
        are unaffected."""
        mock_pm.is_connected.return_value = True
        mock_pm.is_quarantined.return_value = False
        mock_pm.is_model_mismatch.return_value = False
        mock_pm.get_status.return_value = MagicMock(state="IDLE", hms_errors=[])

        assert scheduler._is_printer_idle(1) is True
        assert scheduler._is_printer_idle(1, db_claim=True) is False


class TestSchedulerQueueCheckLogging:
    """Test queue check logging when pending items are found (#374)."""

    @pytest.fixture
    def scheduler(self):
        return PrintScheduler()

    @pytest.mark.asyncio
    @patch("backend.app.services.print_scheduler.printer_manager")
    async def test_check_queue_logs_pending_items(self, mock_pm, scheduler, caplog):
        """Verify pending items are logged when found in check_queue."""
        mock_item = MagicMock()
        mock_item.id = 42
        mock_item.printer_id = 1
        mock_item.archive_id = 100
        mock_item.library_file_id = None
        mock_item.scheduled_time = None
        mock_item.manual_start = False
        mock_item.target_model = None

        mock_pm.is_connected.return_value = True
        mock_pm.is_quarantined.return_value = False
        mock_pm.is_model_mismatch.return_value = False
        mock_pm.get_status.return_value = MagicMock(state="RUNNING", hms_errors=[])

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_item]

        with (
            patch("backend.app.services.print_scheduler.async_session") as mock_session_ctx,
            caplog.at_level(logging.INFO, logger="backend.app.services.print_scheduler"),
        ):
            mock_db = AsyncMock()
            mock_db.execute = AsyncMock(return_value=mock_result)
            mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

            await scheduler.check_queue()

        queue_logs = [r for r in caplog.records if "Queue check" in r.message]
        assert len(queue_logs) == 1
        assert "1 pending items" in queue_logs[0].message
        assert "42" in queue_logs[0].message  # item ID

    @pytest.mark.asyncio
    async def test_check_queue_no_log_when_empty(self, scheduler, caplog):
        """Verify no queue log when no pending items found."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []

        with (
            patch("backend.app.services.print_scheduler.async_session") as mock_session_ctx,
            caplog.at_level(logging.INFO, logger="backend.app.services.print_scheduler"),
        ):
            mock_db = AsyncMock()
            mock_db.execute = AsyncMock(return_value=mock_result)
            mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

            await scheduler.check_queue()

        queue_logs = [r for r in caplog.records if "Queue check" in r.message]
        assert len(queue_logs) == 0


class TestFarmItemEnforcesPlateClearGate:
    """Phase 1 (P1-B): the scheduler's plate gate is UNCONDITIONAL — a deposit on
    the plate holds EVERY item on that printer, farm or plain, regardless of the
    global require_plate_clear setting. (The toggle now only decides whether the gate
    is RAISED by a terminal; the scheduler always honours a raised gate.) Incident
    PCO-M18-2904: unit 2 dispatched 6 s after unit 1's FINISH because the global
    toggle was false — the fix is that a raised gate is never bypassed here."""

    async def _run_check_queue(self, *, eject_profile_id, occupied, caplog=None):
        """Drive check_queue against a real in-memory DB with one FINISH printer
        (plate occupied as given) and require_plate_clear=False. Returns the
        _start_print mock so callers assert dispatch or hold."""
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

        import backend.app.models  # noqa: F401
        from backend.app.core.database import Base

        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        async with session_maker() as db:
            db.add(
                PrintQueueItem(
                    printer_id=1,
                    status="pending",
                    position=1,
                    archive_id=84,
                    eject_profile_id=eject_profile_id,
                )
            )
            await db.commit()

        if occupied:
            _raise_gate(1)

        scheduler = PrintScheduler()
        start_print_mock = AsyncMock()

        with (
            patch("backend.app.services.print_scheduler.async_session", session_maker),
            # require_plate_clear (and every other bool setting) resolves False —
            # the global toggle is OFF, the exact incident condition.
            patch.object(scheduler, "_get_bool_setting", AsyncMock(return_value=False)),
            patch.object(scheduler, "_check_auto_drying", AsyncMock()),
            # Phase E: budget is owned by the module singleton (patch it there).
            patch("backend.app.services.print_scheduler.stagger_policy.budget", AsyncMock(return_value=99)),
            patch.object(
                scheduler, "_compute_ams_mapping_for_printer", AsyncMock(return_value=MatchOutcome(mapping=None))
            ),
            patch.object(scheduler, "_block_on_filament_deficit", AsyncMock(return_value=False)),
            patch.object(scheduler, "_start_print", start_print_mock),
            patch("backend.app.services.print_scheduler.printer_manager") as mock_pm,
        ):
            # Real _is_printer_idle runs: FINISH + connected + not quarantined, so
            # the ONLY thing that can hold the printer is the plate gate.
            mock_pm.is_connected.return_value = True
            mock_pm.is_quarantined.return_value = False
            mock_pm.is_model_mismatch.return_value = False
            mock_pm.get_status.return_value = MagicMock(state="FINISH", hms_errors=[], subtask_id="task-1")
            if caplog is not None:
                with caplog.at_level(logging.INFO, logger="backend.app.services.print_scheduler"):
                    await scheduler.check_queue()
            else:
                await scheduler.check_queue()

        await engine.dispose()
        return start_print_mock

    @pytest.mark.asyncio
    async def test_farm_item_held_when_gate_raised(self):
        """(a) Farm item + occupied plate → NOT dispatched (gate unconditional)."""
        start_print_mock = await self._run_check_queue(eject_profile_id=2, occupied=True)
        start_print_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_plain_item_also_held_when_gate_raised(self):
        """(b) Plain item (no eject profile) + occupied plate → ALSO NOT dispatched.
        The scheduler no longer bypasses a raised gate for plain items; the toggle-
        off "keep dispatching" behaviour now lives on the RAISE side (a plain print
        under a toggle-off install simply never raises the gate)."""
        start_print_mock = await self._run_check_queue(eject_profile_id=None, occupied=True)
        start_print_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_farm_item_dispatches_when_gate_released(self):
        """(c) Farm item with the plate clear → dispatched."""
        start_print_mock = await self._run_check_queue(eject_profile_id=2, occupied=False)
        start_print_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_plain_item_dispatches_when_gate_released(self):
        """(d) Plain item with the plate clear → dispatched."""
        start_print_mock = await self._run_check_queue(eject_profile_id=None, occupied=False)
        start_print_mock.assert_called_once()


# --------------------------------------------------------------------------- #
# The dispatch path and the plate (2026-08-30 01:06:57, printer 4)
# --------------------------------------------------------------------------- #


async def _seed_dispatchable(db, tmp_path, *, name="CP"):
    """A pending, archive-backed queue item whose source file exists on disk."""
    printer = Printer(name=name, serial_number=f"S{name}", ip_address="1.2.3.4", access_code="x", model="H2S")
    db.add(printer)
    await db.flush()

    source = tmp_path / f"{name}.gcode.3mf"
    source.write_bytes(b"PK\x03\x04 not really a 3mf")
    archive = PrintArchive(
        printer_id=printer.id,
        filename=source.name,
        file_path=str(source),  # absolute → base_dir / this yields this
        file_size=source.stat().st_size,
        status="completed",
    )
    db.add(archive)
    await db.flush()

    item = PrintQueueItem(printer_id=printer.id, archive_id=archive.id, status="pending", plate_id=1, position=1)
    db.add(item)
    await db.commit()
    return printer, item, f"/{derive_remote_filename(source.name)}"


@contextlib.contextmanager
def _dispatch_env(scheduler, *, upload=None):
    """Patch everything ``_start_print`` touches between the USB gate and the commit.

    ``upload`` overrides the FTPS upload coroutine — the hook the declare test uses
    to move the world while the dispatcher is "uploading". ``start`` is
    ``printer_manager.start_print``, i.e. the print command itself; ``delete`` is
    the USB cleanup.
    """
    start = MagicMock(return_value=True)
    upload_mock = AsyncMock(return_value=True) if upload is None else AsyncMock(side_effect=upload)
    delete_mock = AsyncMock(return_value=True)
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(printer_manager, "is_connected", return_value=True))
        # No ``state`` attribute → pre_state is None → no start watchdog task.
        stack.enter_context(
            patch.object(printer_manager, "get_status", MagicMock(return_value=SimpleNamespace(sdcard=True)))
        )
        stack.enter_context(patch.object(printer_manager, "get_client", MagicMock(return_value=None)))
        stack.enter_context(patch.object(printer_manager, "request_status_update", MagicMock(return_value=True)))
        stack.enter_context(patch.object(printer_manager, "start_print", start))
        stack.enter_context(
            patch.object(ps_module, "get_ftp_retry_settings", AsyncMock(return_value=(False, 3, 1.0, 30.0)))
        )
        stack.enter_context(patch.object(ps_module, "delete_file_async", delete_mock))
        stack.enter_context(patch.object(ps_module, "upload_file_async", upload_mock))
        stack.enter_context(patch.object(ps_module, "with_ftp_retry", AsyncMock(return_value=True)))
        stack.enter_context(patch.object(ps_module, "cache_3mf_download", MagicMock()))
        stack.enter_context(patch.object(ps_module.notification_service, "on_queue_job_started", AsyncMock()))
        stack.enter_context(patch.object(ps_module.notification_service, "on_queue_job_failed", AsyncMock()))
        stack.enter_context(patch("backend.app.main.register_expected_print", MagicMock()))
        stack.enter_context(patch.object(scheduler, "_power_off_if_needed", AsyncMock()))
        yield SimpleNamespace(start=start, upload=upload_mock, delete=delete_mock)


@pytest.mark.asyncio
class TestHealthyDispatchDoesNotTouchThePlate:
    """THE INVERSION. The unconditional ``set_awaiting_plate_clear(False)`` that used
    to stand immediately before ``start_print`` is gone: a dispatch only ever
    COMMITS its lease there, and the plate is not its to write."""

    async def test_a_healthy_dispatch_leaves_the_plate_exactly_as_it_found_it(self, db_session, tmp_path):
        printer, item, _remote = await _seed_dispatchable(db_session, tmp_path, name="CPOK")
        scheduler = PrintScheduler()

        causes: list[str] = []
        plate_occupancy.configure(policy_driver=lambda pid, view, cause: causes.append(cause))

        with _dispatch_env(scheduler) as m:
            await scheduler._start_print(db_session, item, ams_mapping=[1, -1])

        m.start.assert_called_once()
        row = (await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.id == item.id))).scalar_one()
        assert row.status == "printing"

        # Nothing on the dispatch path wrote the plate: no clear, no raise. The only
        # transitions are the two halves of the printer CLAIM.
        assert causes == ["claim_for_dispatch", "commit_dispatch"]
        view = plate_occupancy.snapshot(printer.id)
        assert view.plate_occupied is False
        assert view.plate_since is None
        assert view.lease_unit_id == item.id

    async def test_a_dispatch_is_refused_by_an_occupied_plate_rather_than_clearing_it(self, db_session, tmp_path):
        """The plate stands. Its identity, policy and ``since`` all survive a dispatch
        attempt — the gate is not a thing a print command may consume."""
        printer, item, _remote = await _seed_dispatchable(db_session, tmp_path, name="CPGATE")
        scheduler = PrintScheduler()
        _raise_gate(printer.id, subtask_id="task-earlier", policy=CooldownEject(unit_id=999, run_id=1))
        since = plate_occupancy.snapshot(printer.id).plate_since

        with _dispatch_env(scheduler) as m:
            await scheduler._start_print(db_session, item, ams_mapping=[1, -1])

        m.start.assert_not_called()
        view = plate_occupancy.snapshot(printer.id)
        assert view.plate_occupied is True
        assert view.plate_source_subtask_id == "task-earlier"
        assert isinstance(view.plate_policy, CooldownEject)
        assert view.plate_since == since
        row = (await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.id == item.id))).scalar_one()
        assert row.status == "pending"


@pytest.mark.asyncio
class TestDeclareDuringDispatch:
    """2026-08-30 01:06:57 on printer 4 — the race the whole cut-over exists for."""

    async def test_declare_during_upload_refuses_commit_and_row_returns_to_pending(self, db_session, tmp_path, caplog):
        """LIVENESS + the incident, end to end.

        An operator declares the plate occupied while the dispatcher is inside its
        FTPS upload — after the claim, before ``start_print``. The declaration REVOKES
        the lease, ``commit_dispatch`` refuses ``lease_revoked``, and the dispatch
        unwinds: the queue row goes back to ``pending``, the plate STAYS occupied (it
        is the operator's statement and it outranks a dispatch that has not happened),
        the uploaded file is removed so it cannot be screen-started as a foreign
        print, and no print command is sent.

        Then the liveness half: once the operator clears the plate, the SAME unit
        dispatches. A cure that leaves the unit parked is not a cure.
        """
        printer, item, remote_path = await _seed_dispatchable(db_session, tmp_path, name="CPRACE")
        printer_id, item_id = printer.id, item.id
        scheduler = PrintScheduler()

        async def _declare_mid_upload(*_args, **_kwargs):
            # Exactly what POST /printers/{id}/mark-plate-occupied does.
            assert plate_occupancy.declare_occupied(printer_id, Evidence(live_state="IDLE")) is None
            return True

        lease = scheduler._claim_dispatch_lease(printer_id, item_id)
        assert lease is not None

        with (
            caplog.at_level(logging.WARNING, logger="backend.app.services.print_scheduler"),
            _dispatch_env(scheduler, upload=_declare_mid_upload) as m,
        ):
            await scheduler._start_print(db_session, item, ams_mapping=[1, -1], lease=lease)

        m.upload.assert_awaited_once()
        m.start.assert_not_called()  # the print command never went out
        assert lease.revoked is True

        row = (await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one()
        assert row.status == "pending"  # returned for the next tick, not failed
        assert row.started_at is None
        assert row.ams_mapping is None  # the decided mapping is not a pin

        # The operator's statement stands, and the printer claim is gone.
        assert plate_occupancy.is_plate_occupied(printer_id) is True
        assert plate_occupancy.snapshot(printer_id).lease_unit_id is None

        # The upload is undone: the routine pre-upload clear, then the cleanup.
        assert m.delete.await_count == 2
        assert m.delete.await_args.args[2] == remote_path
        assert any("plate gate rose mid-dispatch" in r.message for r in caplog.records), caplog.text

        # --- LIVENESS: clearing the plate lets the SAME unit dispatch -----------
        assert plate_occupancy.clear_plate(printer_id) is None
        db_session.expire_all()
        again = await db_session.get(PrintQueueItem, item_id)

        with _dispatch_env(scheduler) as m2:
            await scheduler._start_print(db_session, again, ams_mapping=[1, -1])

        m2.start.assert_called_once()
        row2 = (await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one()
        assert row2.status == "printing"
        assert plate_occupancy.is_plate_occupied(printer_id) is False

    async def test_a_declaration_is_refused_while_a_job_is_actually_running(self, db_session, tmp_path):
        """The boundary: a running job would be depositing onto the plate anyway, so
        the declaration has nothing to add and the lease is NOT revoked."""
        printer, item, _remote = await _seed_dispatchable(db_session, tmp_path, name="CPRUN")
        scheduler = PrintScheduler()

        lease = scheduler._claim_dispatch_lease(printer.id, item.id)
        assert lease is not None
        assert plate_occupancy.declare_occupied(printer.id, Evidence(live_state="RUNNING")) == "job_active"
        assert lease.revoked is False
        assert plate_occupancy.is_plate_occupied(printer.id) is False


# --------------------------------------------------------------------------- #
# The dead dispatch claim releases BOTH claims (2026-08-29 W3 + the WS3 cut-over)
# --------------------------------------------------------------------------- #


class _DeadClaimManager:
    """The minimum ``printer_manager`` surface ``check_dead_dispatch_claims`` reads."""

    def __init__(self, printer_id: int, state: str, subtask_id: str | None = None):
        self._printer_id = printer_id
        self._state = SimpleNamespace(state=state, subtask_id=subtask_id)

    def is_connected(self, printer_id: int) -> bool:
        return printer_id == self._printer_id

    def get_status(self, printer_id: int):
        return self._state if printer_id == self._printer_id else None


@pytest.mark.asyncio
class TestDeadDispatchClaimReleasesTheLease:
    """The row claim and the PRINTER claim are two different things with two
    different writers, and un-making a dispatch means dropping both.

    ``release_unstarted_claim`` releases the queue row; ``release_dispatch`` releases
    the occupancy lease that would otherwise hold the printer out of the queue for
    the full ceiling. Releasing only the row was the shape that left 001-H2S taking
    no work while the row it was waiting on had already been retired.
    """

    async def test_a_matured_dead_claim_releases_the_row_and_the_lease(self, db_session, printer_factory):
        from backend.app.services import farm_stall

        printer = await printer_factory(model="H2S")
        printer_id = printer.id
        now = 1_800_000_000.0
        started = datetime.fromtimestamp(now - farm_stall._DEAD_CLAIM_MIN_AGE_S - 60, tz=timezone.utc).replace(
            tzinfo=None
        )
        item = PrintQueueItem(
            printer_id=printer_id,
            status="printing",
            plate_id=1,
            position=1,
            started_at=started,
            dispatch_subtask_id="dispatch-1",
            ams_mapping="[0, -1, -1, -1]",
        )
        db_session.add(item)
        await db_session.commit()
        await db_session.refresh(item)

        # The dispatch that never started still holds its printer claim.
        scheduler = PrintScheduler()
        with patch.object(
            printer_manager, "get_status", MagicMock(return_value=SimpleNamespace(state="IDLE", subtask_id="other"))
        ):
            lease = scheduler._claim_dispatch_lease(printer_id, item.id)
        assert lease is not None
        plate_occupancy.commit_dispatch(printer_id, lease)
        assert plate_occupancy.snapshot(printer_id).lease_unit_id == item.id
        assert printer_id in plate_occupancy.printers_with_lease_or_eject()

        mgr = _DeadClaimManager(printer_id, "IDLE", subtask_id="a-different-job")
        farm_stall._dead_claim_since.clear()
        try:
            await farm_stall.check_dead_dispatch_claims(db_session, manager=mgr, now=now)  # seed the dwell
            await farm_stall.check_dead_dispatch_claims(
                db_session, manager=mgr, now=now + farm_stall._DEAD_CLAIM_DWELL_S + 1
            )
        finally:
            farm_stall._dead_claim_since.clear()

        db_session.expunge_all()
        after = await db_session.get(PrintQueueItem, item.id)
        assert after.status == "pending"  # the row claim
        assert plate_occupancy.snapshot(printer_id).lease_unit_id is None  # the printer claim
        assert printer_id not in plate_occupancy.printers_with_lease_or_eject()
        # And the plate was never written — the watch un-makes a dispatch, nothing more.
        assert plate_occupancy.is_plate_occupied(printer_id) is False

    async def test_a_claim_the_watch_leaves_alone_keeps_its_lease(self, db_session, printer_factory):
        """The guard side: a young claim is the dispatch watchdog's business, and the
        printer claim must survive with it — releasing the lease under a live
        dispatch is a double-dispatch onto an occupied plate."""
        from backend.app.services import farm_stall

        printer = await printer_factory(model="H2S")
        printer_id = printer.id
        now = 1_800_000_000.0
        item = PrintQueueItem(
            printer_id=printer_id,
            status="printing",
            plate_id=1,
            position=1,
            started_at=datetime.fromtimestamp(now, tz=timezone.utc).replace(tzinfo=None) - timedelta(seconds=30),
            dispatch_subtask_id="dispatch-1",
        )
        db_session.add(item)
        await db_session.commit()

        scheduler = PrintScheduler()
        with patch.object(
            printer_manager, "get_status", MagicMock(return_value=SimpleNamespace(state="IDLE", subtask_id="other"))
        ):
            lease = scheduler._claim_dispatch_lease(printer_id, item.id)
        assert lease is not None
        plate_occupancy.commit_dispatch(printer_id, lease)

        mgr = _DeadClaimManager(printer_id, "IDLE", subtask_id="a-different-job")
        farm_stall._dead_claim_since.clear()
        try:
            await farm_stall.check_dead_dispatch_claims(db_session, manager=mgr, now=now)
            await farm_stall.check_dead_dispatch_claims(
                db_session, manager=mgr, now=now + farm_stall._DEAD_CLAIM_DWELL_S + 1
            )
        finally:
            farm_stall._dead_claim_since.clear()

        db_session.expunge_all()
        assert (await db_session.get(PrintQueueItem, item.id)).status == "printing"
        assert plate_occupancy.snapshot(printer_id).lease_unit_id == item.id
