"""Tests for the clear plate queue flow in the print scheduler."""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services.print_scheduler import PrintScheduler
from backend.app.services.printer_manager import PrinterManager
from backend.app.services.spool_selection import MatchOutcome


class TestPrinterManagerPlateCleared:
    """Test the plate-cleared flag management in PrinterManager."""

    @pytest.fixture
    def manager(self):
        return PrinterManager()

    def test_plate_cleared_initially_false(self, manager):
        """No printers should have plate cleared by default."""
        assert not manager.is_awaiting_plate_clear(1)
        assert not manager.is_awaiting_plate_clear(999)

    def test_set_plate_cleared(self, manager):
        """Setting plate cleared should make is_awaiting_plate_clear return True."""
        manager.set_awaiting_plate_clear(1, True)
        assert manager.is_awaiting_plate_clear(1)
        assert not manager.is_awaiting_plate_clear(2)

    def test_consume_plate_cleared(self, manager):
        """Consuming plate cleared should reset the flag."""
        manager.set_awaiting_plate_clear(1, True)
        assert manager.is_awaiting_plate_clear(1)
        manager.set_awaiting_plate_clear(1, False)
        assert not manager.is_awaiting_plate_clear(1)

    def test_consume_plate_cleared_idempotent(self, manager):
        """Consuming when not set should not raise."""
        manager.set_awaiting_plate_clear(1, False)  # Should not raise
        assert not manager.is_awaiting_plate_clear(1)

    def test_set_plate_cleared_multiple_printers(self, manager):
        """Plate cleared should be tracked per printer."""
        manager.set_awaiting_plate_clear(1, True)
        manager.set_awaiting_plate_clear(3, True)
        assert manager.is_awaiting_plate_clear(1)
        assert not manager.is_awaiting_plate_clear(2)
        assert manager.is_awaiting_plate_clear(3)

    def test_consume_only_affects_target_printer(self, manager):
        """Consuming plate cleared for one printer should not affect others."""
        manager.set_awaiting_plate_clear(1, True)
        manager.set_awaiting_plate_clear(2, True)
        manager.set_awaiting_plate_clear(1, False)
        assert not manager.is_awaiting_plate_clear(1)
        assert manager.is_awaiting_plate_clear(2)


class TestAwaitingPlateClearPersistence:
    """Verify the awaiting-plate-clear flag round-trips through the DB (#961)."""

    @pytest.mark.asyncio
    async def test_load_rehydrates_in_memory_set_from_db(self):
        """Printers flagged in DB must re-appear in the in-memory set on startup."""
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

        # Ensure all models are imported so Base.metadata includes them
        import backend.app.models  # noqa: F401
        from backend.app.core.database import Base
        from backend.app.models.printer import Printer

        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        # Seed: two printers, one flagged awaiting, one not
        async with session_maker() as db:
            db.add_all(
                [
                    Printer(
                        id=1,
                        name="P1",
                        serial_number="S1",
                        ip_address="1.1.1.1",
                        access_code="x",
                        awaiting_plate_clear=True,
                    ),
                    Printer(
                        id=2,
                        name="P2",
                        serial_number="S2",
                        ip_address="2.2.2.2",
                        access_code="y",
                        awaiting_plate_clear=False,
                    ),
                ]
            )
            await db.commit()

        # Point the manager's session factory at our in-memory DB and load
        manager = PrinterManager()
        with patch("backend.app.core.database.async_session", session_maker):
            await manager.load_awaiting_plate_clear_from_db()

        assert manager.is_awaiting_plate_clear(1) is True
        assert manager.is_awaiting_plate_clear(2) is False
        await engine.dispose()

    @pytest.mark.asyncio
    async def test_persist_writes_flag_to_db(self):
        """set_awaiting_plate_clear + _persist writes the flag to the DB row."""
        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

        import backend.app.models  # noqa: F401
        from backend.app.core.database import Base
        from backend.app.models.printer import Printer

        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        async with session_maker() as db:
            db.add(
                Printer(
                    id=1,
                    name="P1",
                    serial_number="S1",
                    ip_address="1.1.1.1",
                    access_code="x",
                    awaiting_plate_clear=False,
                )
            )
            await db.commit()

        manager = PrinterManager()
        with patch("backend.app.core.database.async_session", session_maker):
            await manager._persist_awaiting_plate_clear(1, True)

        async with session_maker() as db:
            row = (await db.execute(select(Printer).where(Printer.id == 1))).scalar_one()
            assert row.awaiting_plate_clear is True

        with patch("backend.app.core.database.async_session", session_maker):
            await manager._persist_awaiting_plate_clear(1, False)

        async with session_maker() as db:
            row = (await db.execute(select(Printer).where(Printer.id == 1))).scalar_one()
            assert row.awaiting_plate_clear is False

        await engine.dispose()

    @pytest.mark.asyncio
    async def test_persist_missing_printer_does_not_raise(self):
        """Persisting for a non-existent printer should be a silent no-op."""
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

        import backend.app.models  # noqa: F401
        from backend.app.core.database import Base

        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        manager = PrinterManager()
        with patch("backend.app.core.database.async_session", session_maker):
            # Should not raise even though printer 999 does not exist
            await manager._persist_awaiting_plate_clear(999, True)

        await engine.dispose()


class TestSchedulerIdleCheckWithPlateCleared:
    """Test _is_printer_idle interactions with the awaiting-plate-clear flag (#961)."""

    @pytest.fixture
    def scheduler(self):
        return PrintScheduler()

    @patch("backend.app.services.print_scheduler.printer_manager")
    def test_idle_state_is_idle(self, mock_pm, scheduler):
        """IDLE state with no awaiting flag → idle."""
        mock_pm.is_connected.return_value = True
        mock_pm.is_quarantined.return_value = False
        mock_pm.is_model_mismatch.return_value = False
        mock_pm.get_status.return_value = MagicMock(state="IDLE")
        mock_pm.is_awaiting_plate_clear.return_value = False
        assert scheduler._is_printer_idle(1) is True

    @patch("backend.app.services.print_scheduler.printer_manager")
    def test_running_state_not_idle(self, mock_pm, scheduler):
        """RUNNING state is never idle."""
        mock_pm.is_connected.return_value = True
        mock_pm.is_quarantined.return_value = False
        mock_pm.is_model_mismatch.return_value = False
        mock_pm.get_status.return_value = MagicMock(state="RUNNING")
        mock_pm.is_awaiting_plate_clear.return_value = False
        assert scheduler._is_printer_idle(1) is False

    @patch("backend.app.services.print_scheduler.printer_manager")
    def test_finish_state_not_idle_when_awaiting(self, mock_pm, scheduler):
        """FINISH + awaiting plate-clear ack → NOT idle."""
        mock_pm.is_connected.return_value = True
        mock_pm.is_quarantined.return_value = False
        mock_pm.is_model_mismatch.return_value = False
        mock_pm.get_status.return_value = MagicMock(state="FINISH")
        mock_pm.is_awaiting_plate_clear.return_value = True
        assert scheduler._is_printer_idle(1) is False

    @patch("backend.app.services.print_scheduler.printer_manager")
    def test_finish_state_idle_when_acknowledged(self, mock_pm, scheduler):
        """FINISH with flag cleared → idle."""
        mock_pm.is_connected.return_value = True
        mock_pm.is_quarantined.return_value = False
        mock_pm.is_model_mismatch.return_value = False
        mock_pm.get_status.return_value = MagicMock(state="FINISH")
        mock_pm.is_awaiting_plate_clear.return_value = False
        assert scheduler._is_printer_idle(1) is True

    @patch("backend.app.services.print_scheduler.printer_manager")
    def test_failed_state_not_idle_when_awaiting(self, mock_pm, scheduler):
        """FAILED + awaiting → NOT idle."""
        mock_pm.is_connected.return_value = True
        mock_pm.is_quarantined.return_value = False
        mock_pm.is_model_mismatch.return_value = False
        mock_pm.get_status.return_value = MagicMock(state="FAILED")
        mock_pm.is_awaiting_plate_clear.return_value = True
        assert scheduler._is_printer_idle(1) is False

    @patch("backend.app.services.print_scheduler.printer_manager")
    def test_failed_state_idle_when_acknowledged(self, mock_pm, scheduler):
        """FAILED with flag cleared → idle."""
        mock_pm.is_connected.return_value = True
        mock_pm.is_quarantined.return_value = False
        mock_pm.is_model_mismatch.return_value = False
        mock_pm.get_status.return_value = MagicMock(state="FAILED")
        mock_pm.is_awaiting_plate_clear.return_value = False
        assert scheduler._is_printer_idle(1) is True

    @patch("backend.app.services.print_scheduler.printer_manager")
    def test_idle_state_not_idle_when_awaiting_survives_power_cycle(self, mock_pm, scheduler):
        """Regression for #961: after Auto Off power-cycles the printer it boots into IDLE
        with no memory of the previous finish. The persisted awaiting flag must still gate
        the queue — IDLE + awaiting → NOT idle.
        """
        mock_pm.is_connected.return_value = True
        mock_pm.is_quarantined.return_value = False
        mock_pm.is_model_mismatch.return_value = False
        mock_pm.get_status.return_value = MagicMock(state="IDLE")
        mock_pm.is_awaiting_plate_clear.return_value = True
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
        """Phase 1 (P1-B): the plate-clear gate is now UNCONDITIONAL — there is no
        require_plate_clear parameter to bypass it. Any idle-shaped state with the
        awaiting flag set is NOT idle. (The global toggle now only governs whether
        the gate is RAISED, in main.on_print_complete — not whether the scheduler
        honours it.)"""
        mock_pm.is_connected.return_value = True
        mock_pm.is_quarantined.return_value = False
        mock_pm.is_model_mismatch.return_value = False
        mock_pm.get_status.return_value = MagicMock(state=state)
        mock_pm.is_awaiting_plate_clear.return_value = True
        assert scheduler._is_printer_idle(1) is False


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
        mock_pm.get_status.return_value = MagicMock(state="RUNNING")

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
    """Phase 1 (P1-B): the scheduler's plate-clear gate is now UNCONDITIONAL — a
    raised awaiting_plate_clear flag holds EVERY item on that printer, farm or plain,
    regardless of the global require_plate_clear setting. (The toggle now only
    decides whether the gate is RAISED, in main.on_print_complete; the scheduler
    always honours a raised gate.) Incident PCO-M18-2904: unit 2 dispatched 6 s
    after unit 1's FINISH because the global toggle was false — the fix is that a
    raised gate is never bypassed here."""

    async def _run_check_queue(self, *, eject_profile_id, awaiting, caplog=None):
        """Drive check_queue against a real in-memory DB with one FINISH printer
        (awaiting_plate_clear as given) and require_plate_clear=False. Returns the
        _start_print mock so callers assert dispatch or hold."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

        import backend.app.models  # noqa: F401
        from backend.app.core.database import Base
        from backend.app.models.print_queue import PrintQueueItem

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

        scheduler = PrintScheduler()
        start_print_mock = AsyncMock()

        with (
            patch("backend.app.services.print_scheduler.async_session", session_maker),
            # require_plate_clear (and every other bool setting) resolves False —
            # the global toggle is OFF, the exact incident condition.
            patch.object(scheduler, "_get_bool_setting", AsyncMock(return_value=False)),
            patch.object(scheduler, "_check_auto_drying", AsyncMock()),
            patch.object(scheduler, "_stagger_budget", AsyncMock(return_value=99)),
            patch.object(scheduler, "_compute_ams_mapping_for_printer", AsyncMock(return_value=MatchOutcome(mapping=None))),
            patch.object(scheduler, "_block_on_filament_deficit", AsyncMock(return_value=False)),
            patch.object(scheduler, "_start_print", start_print_mock),
            patch("backend.app.services.print_scheduler.printer_manager") as mock_pm,
        ):
            # Real _is_printer_idle runs: FINISH + connected + not quarantined, so
            # the ONLY thing that can hold the printer is the plate-clear gate.
            mock_pm.is_connected.return_value = True
            mock_pm.is_quarantined.return_value = False
            mock_pm.is_model_mismatch.return_value = False
            mock_pm.get_status.return_value = MagicMock(state="FINISH")
            mock_pm.is_awaiting_plate_clear.return_value = awaiting
            if caplog is not None:
                with caplog.at_level(logging.INFO, logger="backend.app.services.print_scheduler"):
                    await scheduler.check_queue()
            else:
                await scheduler.check_queue()

        await engine.dispose()
        return start_print_mock

    @pytest.mark.asyncio
    async def test_farm_item_held_when_gate_raised(self):
        """(a) Farm item + awaiting=True → NOT dispatched (gate unconditional)."""
        start_print_mock = await self._run_check_queue(eject_profile_id=2, awaiting=True)
        start_print_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_plain_item_also_held_when_gate_raised(self):
        """(b) Plain item (no eject profile) + awaiting=True → ALSO NOT dispatched.
        The scheduler no longer bypasses a raised gate for plain items; the toggle-
        off "keep dispatching" behaviour now lives on the RAISE side (a plain print
        under a toggle-off install simply never raises the gate)."""
        start_print_mock = await self._run_check_queue(eject_profile_id=None, awaiting=True)
        start_print_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_farm_item_dispatches_when_gate_released(self):
        """(c) Farm item with the plate-clear gate released → dispatched."""
        start_print_mock = await self._run_check_queue(eject_profile_id=2, awaiting=False)
        start_print_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_plain_item_dispatches_when_gate_released(self):
        """(d) Plain item with the plate-clear gate released → dispatched."""
        start_print_mock = await self._run_check_queue(eject_profile_id=None, awaiting=False)
        start_print_mock.assert_called_once()
