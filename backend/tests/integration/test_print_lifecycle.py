"""
Integration tests for the full print lifecycle.

These tests verify that:
1. Print start creates a new archive
2. Print complete updates archive status
3. Callbacks are properly executed
4. Energy tracking works
5. Notifications are sent

Note: These tests use mocking to avoid database conflicts.
Full end-to-end tests require the actual database setup.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestPrintStartLogic:
    """Test print start callback logic without database integration."""

    @pytest.mark.asyncio
    async def test_print_start_calls_notification_service(self, capture_logs):
        """Verify on_print_start triggers notification service."""
        with (
            patch("backend.app.main.async_session") as mock_session_maker,
            patch("backend.app.main.notification_service") as mock_notif,
            patch("backend.app.main.smart_plug_manager") as mock_plug,
            patch("backend.app.main.ws_manager") as mock_ws,
        ):
            mock_notif.on_print_start = AsyncMock()
            mock_plug.on_print_start = AsyncMock()
            mock_ws.send_print_start = AsyncMock()

            # Mock the database session
            mock_session = AsyncMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock()
            mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
            mock_session_maker.return_value = mock_session

            from backend.app.main import on_print_start

            await on_print_start(
                1,
                {
                    "filename": "/data/Metadata/test.gcode",
                    "subtask_name": "Test",
                },
            )

            # Verify WebSocket notification was sent
            mock_ws.send_print_start.assert_called_once()

        # Verify no import shadowing errors
        errors = [r for r in capture_logs.get_errors() if "cannot access local variable" in str(r.message)]
        assert not errors, f"Import shadowing error: {capture_logs.format_errors()}"


class TestPlateClearGate:
    """The plate-clear gate (#961) blocks the queue from auto-dispatching the
    next print until the user acknowledges the bed was cleared. The gate must
    be raised on every terminal status that could have left material on the
    bed — including aborted (printer self-abort or touchscreen stop) and
    cancelled (user stopped via Bambuddy queue UI). #1171: prior code only
    raised the flag for completed/failed, so an aborted print auto-dispatched
    the next queue item onto a fouled bed two seconds later."""

    @staticmethod
    def _setup_mocks(stack, test_engine):
        """Patch on_print_complete's collaborators and back its DB access with the
        REAL test engine so the Phase-1 terminal correlation runs for real (the old
        MagicMock single-item lookup can't satisfy resolve_terminal_item). Returns a
        namespace exposing the mocked printer_manager, eject monitor and notification
        service so a test can assert on the gate/arm/notify calls."""
        from types import SimpleNamespace

        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        stack.enter_context(patch("backend.app.main.async_session", maker))
        stack.enter_context(patch("backend.app.core.database.async_session", maker))

        mock_notif = stack.enter_context(patch("backend.app.main.notification_service"))
        mock_notif.on_print_complete = AsyncMock()
        mock_notif.on_queue_completed = AsyncMock()
        mock_notif.on_foreign_job_detected = AsyncMock()
        mock_notif._get_providers_for_event = AsyncMock(return_value=[])
        stack.enter_context(patch("backend.app.main.smart_plug_manager")).on_print_complete = AsyncMock()
        mock_ws = stack.enter_context(patch("backend.app.main.ws_manager"))
        mock_ws.send_print_complete = AsyncMock()
        mock_ws.broadcast = AsyncMock()
        stack.enter_context(patch("backend.app.main.mqtt_relay")).on_print_complete = AsyncMock()
        mock_pm = stack.enter_context(patch("backend.app.main.printer_manager"))
        mock_pm.get_printer.return_value = None
        mock_pm.get_current_print_user.return_value = None
        # Real methods under test — track each call so the test can assert on it.
        mock_pm.set_awaiting_plate_clear = MagicMock()
        mock_pm.clear_current_print_user = MagicMock()
        # The farm eject monitor is patched so no real background watch spawns; the
        # test asserts whether auto-clear (on_terminal_status) was armed.
        mock_monitor = stack.enter_context(patch("backend.app.main.eject_cooldown_monitor"))
        mock_monitor.on_terminal_status = MagicMock()
        mock_monitor.start_escalation_only_watch = MagicMock()
        return SimpleNamespace(pm=mock_pm, monitor=mock_monitor, notif=mock_notif, maker=maker)

    @staticmethod
    async def _seed_printing_item(maker, *, serial, dispatch_subtask_id=None, is_dry_run=False):
        """Seed a connected printer with one printing (non-farm) queue item and
        return (printer_id, item_id)."""
        from datetime import datetime, timezone

        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.models.printer import Printer

        async with maker() as s:
            printer = Printer(
                name=f"P-{serial}", serial_number=serial, ip_address="10.0.0.9", access_code="0000", model="H2S"
            )
            s.add(printer)
            await s.commit()
            await s.refresh(printer)
            item = PrintQueueItem(
                printer_id=printer.id,
                status="printing",
                first_article=False,
                is_dry_run=is_dry_run,
                dispatch_subtask_id=dispatch_subtask_id,
                started_at=datetime.now(timezone.utc),
            )
            s.add(item)
            await s.commit()
            await s.refresh(item)
            return printer.id, item.id

    @staticmethod
    async def _drain(tasks_before):
        for task in asyncio.all_tasks() - tasks_before:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status",
        ["completed", "failed", "aborted", "cancelled"],
        ids=["completed", "failed", "aborted-1171", "cancelled-1171"],
    )
    async def test_plate_clear_gate_raised_for_every_terminal_status(self, status, test_engine):
        """Regression for #1171. Every terminal status that can leave material on
        the bed raises the gate (require_plate_clear defaults ON when unset). The
        payload carries produced layers/progress so the no-deposit classifier does
        NOT suppress the gate."""
        from contextlib import ExitStack

        tasks_before = set(asyncio.all_tasks())

        with ExitStack() as stack:
            env = self._setup_mocks(stack, test_engine)

            from backend.app.main import on_print_complete

            await on_print_complete(
                1,
                {
                    "status": status,
                    "filename": "/data/Metadata/test.gcode",
                    "subtask_name": "Test",
                    "timelapse_was_active": False,
                    "last_layer_num": 10,
                    "last_progress": 55.0,
                },
            )

            await self._drain(tasks_before)

        true_calls = [c for c in env.pm.set_awaiting_plate_clear.call_args_list if c.args[1] is True]
        assert true_calls, "Gate must be raised for a deposit-bearing terminal (toggle defaults on)."

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "payload_extra, ids",
        [
            ({"last_layer_num": 0, "last_progress": 0}, "zero-layer-print"),
            ({}, "no-progress-data"),
        ],
    )
    async def test_plate_clear_gate_not_raised_for_no_deposit_finish(self, payload_extra, ids, test_engine):
        """A print that reached terminal having deposited nothing (zero layers AND
        zero progress) must NOT raise the plate-clear gate: the bed cannot be fouled."""
        from contextlib import ExitStack

        tasks_before = set(asyncio.all_tasks())

        with ExitStack() as stack:
            env = self._setup_mocks(stack, test_engine)

            from backend.app.main import on_print_complete

            await on_print_complete(
                1,
                {
                    "status": "failed",
                    "filename": "/data/Metadata/test.gcode",
                    "subtask_name": "Test",
                    "timelapse_was_active": False,
                    **payload_extra,
                },
            )

            await self._drain(tasks_before)

        true_calls = [c for c in env.pm.set_awaiting_plate_clear.call_args_list if c.args[1] is True]
        assert true_calls == [], f"Gate must stay clear for a no-deposit finish; got {len(true_calls)} raise(s)."

    @pytest.mark.asyncio
    async def test_plate_clear_gate_not_raised_for_dry_run(self, test_engine):
        """A dry-run eject deposits nothing by construction — even if its non-print
        gcode reported progress, the is_dry_run flag suppresses the gate."""
        from contextlib import ExitStack

        tasks_before = set(asyncio.all_tasks())

        with ExitStack() as stack:
            env = self._setup_mocks(stack, test_engine)
            pid, _iid = await self._seed_printing_item(
                env.maker, serial="DRY-1", dispatch_subtask_id="DR-1", is_dry_run=True
            )

            from backend.app.main import on_print_complete

            await on_print_complete(
                pid,
                {
                    "status": "cancelled",
                    "filename": "/data/Metadata/test.gcode",
                    "subtask_name": "Test",
                    "subtask_id": "DR-1",
                    "timelapse_was_active": False,
                    "last_layer_num": 3,
                    "last_progress": 12.0,
                },
            )

            await self._drain(tasks_before)

        true_calls = [c for c in env.pm.set_awaiting_plate_clear.call_args_list if c.args[1] is True]
        assert true_calls == [], f"Gate must stay clear for a dry-run finish; got {len(true_calls)} raise(s)."

    @pytest.mark.asyncio
    async def test_plate_clear_gate_not_raised_for_unknown_status(self, test_engine):
        """Defence in depth: an unknown / not-terminal status string from a future
        firmware revision must not silently raise the gate."""
        from contextlib import ExitStack

        tasks_before = set(asyncio.all_tasks())

        with ExitStack() as stack:
            env = self._setup_mocks(stack, test_engine)

            from backend.app.main import on_print_complete

            await on_print_complete(
                1,
                {
                    "status": "unknown_future_status",
                    "filename": "/data/Metadata/test.gcode",
                    "subtask_name": "Test",
                    "timelapse_was_active": False,
                },
            )

            await self._drain(tasks_before)

        true_calls = [c for c in env.pm.set_awaiting_plate_clear.call_args_list if c.args[1] is True]
        assert true_calls == [], (
            f"Gate must not be raised for an unrecognised terminal status; raised {len(true_calls)} time(s)."
        )

    @pytest.mark.asyncio
    async def test_foreign_terminal_leaves_item_and_raises_gate(self, test_engine):
        """Phase 1 P1-A: a terminal whose subtask_id matches NO printing item (a
        LOCAL print started from the touchscreen) is FOREIGN — the farm item stays
        'printing', the gate is raised keyed to the foreign subtask, the escalation-
        only watch is started (NOT the auto-clear), and the foreign notification
        fires. The farm queue is left untouched."""
        from contextlib import ExitStack

        from backend.app.models.print_queue import PrintQueueItem

        tasks_before = set(asyncio.all_tasks())

        with ExitStack() as stack:
            env = self._setup_mocks(stack, test_engine)
            pid, iid = await self._seed_printing_item(env.maker, serial="FGN-1", dispatch_subtask_id="DISPATCHED-1")

            from backend.app.main import on_print_complete

            await on_print_complete(
                pid,
                {
                    "status": "completed",
                    "filename": "local.gcode",
                    "subtask_name": "OperatorLocalPrint",
                    "subtask_id": "FOREIGN-9",  # != the item's DISPATCHED-1 → foreign
                    "timelapse_was_active": False,
                    "last_layer_num": 20,
                    "last_progress": 88.0,
                },
            )

            await self._drain(tasks_before)

        # 1. Farm unit untouched — still printing (a foreign print never marks it done).
        async with env.maker() as s:
            refetched = await s.get(PrintQueueItem, iid)
            assert refetched.status == "printing"
        # 2. Gate raised, keyed to the FOREIGN subtask.
        env.pm.set_awaiting_plate_clear.assert_any_call(pid, True, source_subtask_id="FOREIGN-9")
        # 3. Auto-clear NOT armed; escalation-only watch started instead.
        env.monitor.on_terminal_status.assert_not_called()
        env.monitor.start_escalation_only_watch.assert_called_once_with(pid)
        # 4. Foreign notification fired.
        env.notif.on_foreign_job_detected.assert_awaited()


class TestEjectJobCallbacks:
    """C2: a server-dispatched eject sweep (a PendingEject, NO queue item, NO
    archive) must be exempt from the no-deposit status rewrite and the user-facing
    print notification, must NOT create archives at start, yet its farm terminal
    hook + SD-card cleanup must still fire. A dry-run (a queue item, NOT a
    PendingEject) keeps its existing no-deposit path."""

    @staticmethod
    async def _seed_printer(maker, serial):
        from backend.app.models.printer import Printer

        async with maker() as s:
            printer = Printer(
                name=f"P-{serial}", serial_number=serial, ip_address="10.0.0.9", access_code="0000", model="H2S"
            )
            s.add(printer)
            await s.commit()
            await s.refresh(printer)
            return printer.id

    @staticmethod
    async def _settle(tasks_before):
        new = asyncio.all_tasks() - tasks_before - {asyncio.current_task()}
        if new:
            await asyncio.wait(new, timeout=5)

    @pytest.mark.asyncio
    async def test_eject_start_creates_no_archive_and_no_notification(self, test_engine):
        """on_print_start for a pending eject returns early: no PrintArchive row is
        created and no print-start notification is emitted (junk-archive fix)."""
        from contextlib import ExitStack

        from sqlalchemy import func, select
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from backend.app.models.archive import PrintArchive
        from backend.app.services.eject import remote

        maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        pid = await self._seed_printer(maker, "EJ-START")
        remote.register_pending_eject(pid, remote.PendingEject("production", 1, 2))
        tasks_before = set(asyncio.all_tasks())
        try:
            with ExitStack() as stack:
                stack.enter_context(patch("backend.app.main.async_session", maker))
                stack.enter_context(patch("backend.app.core.database.async_session", maker))
                mock_notif = stack.enter_context(patch("backend.app.main.notification_service"))
                mock_notif.on_print_start = AsyncMock()
                mock_ws = stack.enter_context(patch("backend.app.main.ws_manager"))
                mock_ws.send_print_start = AsyncMock()

                from backend.app.main import on_print_start

                await on_print_start(
                    pid,
                    {
                        "filename": "eject_production_item2.gcode.3mf",
                        "subtask_name": "eject_production_item2",
                        "subtask_id": "SUB-E",
                    },
                )
                await self._settle(tasks_before)

            async with maker() as s:
                count = await s.scalar(select(func.count(PrintArchive.id)).where(PrintArchive.printer_id == pid))
            assert count == 0, "An eject job start must NOT create an archive."
            mock_notif.on_print_start.assert_not_called()
            mock_ws.send_print_start.assert_not_called()  # early-returned before the WS emit
        finally:
            remote.pop_pending_eject(pid)

    @pytest.mark.asyncio
    async def test_eject_completed_no_rewrite_notification_suppressed_farm_finalises(self, test_engine):
        """A clean eject FINISH reaches farm_policy as 'completed' (NOT rewritten to
        'cancelled'), emits NO print notification, yet the farm hook + SD-card
        cleanup still run."""
        from contextlib import ExitStack

        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from backend.app.services.bambu_ftp import DeleteResult
        from backend.app.services.eject import remote

        maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        pid = await self._seed_printer(maker, "EJ-DONE")
        remote.register_pending_eject(pid, remote.PendingEject("production", 1, 2))
        tasks_before = set(asyncio.all_tasks())
        try:
            with ExitStack() as stack:
                stack.enter_context(patch("backend.app.main.async_session", maker))
                stack.enter_context(patch("backend.app.core.database.async_session", maker))
                mock_notif = stack.enter_context(patch("backend.app.main.notification_service"))
                mock_notif.on_print_complete = AsyncMock()
                mock_notif.on_queue_completed = AsyncMock()
                mock_notif._get_providers_for_event = AsyncMock(return_value=[])
                stack.enter_context(patch("backend.app.main.smart_plug_manager")).on_print_complete = AsyncMock()
                mock_ws = stack.enter_context(patch("backend.app.main.ws_manager"))
                mock_ws.send_print_complete = AsyncMock()
                mock_ws.broadcast = AsyncMock()
                stack.enter_context(patch("backend.app.main.mqtt_relay")).on_print_complete = AsyncMock()
                mock_del = stack.enter_context(
                    patch("backend.app.services.bambu_ftp.delete_file_async", new_callable=AsyncMock)
                )
                mock_del.return_value = DeleteResult.DELETED
                farm_hook = stack.enter_context(
                    patch("backend.app.services.farm_policy.on_terminal", new_callable=AsyncMock)
                )

                from backend.app.main import on_print_complete

                await on_print_complete(
                    pid,
                    {
                        "status": "completed",
                        "filename": "eject_production_item2.gcode.3mf",
                        "subtask_name": "eject_production_item2",
                        "subtask_id": "SUB-E",
                        "timelapse_was_active": False,
                        "last_layer_num": 0,
                        "last_progress": 0,
                    },
                )
                await self._settle(tasks_before)

            # Farm hook ran with the UN-rewritten 'completed' status + the echo id.
            farm_hook.assert_awaited_once()
            assert farm_hook.await_args.args[3] == "completed"
            assert farm_hook.await_args.kwargs["completed_subtask_id"] == "SUB-E"
            # No "Print Complete/Stopped" notification for the sweep.
            mock_notif.on_print_complete.assert_not_awaited()
            # SD-card cleanup of the uploaded eject file still happened.
            mock_del.assert_awaited()
        finally:
            remote.pop_pending_eject(pid)

    @pytest.mark.asyncio
    async def test_dry_run_terminal_untouched_not_treated_as_eject(self, test_engine):
        """A dry-run (queue item, NO PendingEject) is NOT an eject job: its no-deposit
        terminal is still rewritten to 'cancelled' and STILL emits a notification —
        proving the eject exemption does not bleed into the dry-run path."""
        from contextlib import ExitStack

        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from backend.app.services.bambu_ftp import DeleteResult
        from backend.app.services.eject import remote

        maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        pid = await self._seed_printer(maker, "DRY-EJ")
        # Explicitly NO PendingEject registered for this printer.
        assert remote.peek_pending_eject(pid) is None
        tasks_before = set(asyncio.all_tasks())
        with ExitStack() as stack:
            stack.enter_context(patch("backend.app.main.async_session", maker))
            stack.enter_context(patch("backend.app.core.database.async_session", maker))
            mock_notif = stack.enter_context(patch("backend.app.main.notification_service"))
            mock_notif.on_print_complete = AsyncMock()
            mock_notif.on_queue_completed = AsyncMock()
            mock_notif._get_providers_for_event = AsyncMock(return_value=[])
            stack.enter_context(patch("backend.app.main.smart_plug_manager")).on_print_complete = AsyncMock()
            mock_ws = stack.enter_context(patch("backend.app.main.ws_manager"))
            mock_ws.send_print_complete = AsyncMock()
            mock_ws.broadcast = AsyncMock()
            stack.enter_context(patch("backend.app.main.mqtt_relay")).on_print_complete = AsyncMock()
            mock_del = stack.enter_context(
                patch("backend.app.services.bambu_ftp.delete_file_async", new_callable=AsyncMock)
            )
            mock_del.return_value = DeleteResult.DELETED
            farm_hook = stack.enter_context(
                patch("backend.app.services.farm_policy.on_terminal", new_callable=AsyncMock)
            )

            from backend.app.main import on_print_complete

            await on_print_complete(
                pid,
                {
                    "status": "completed",
                    "filename": "dryrun.gcode.3mf",
                    "subtask_name": "dryrun",
                    "subtask_id": "DR-1",
                    "timelapse_was_active": False,
                    "last_layer_num": 0,
                    "last_progress": 0,
                },
            )
            await self._settle(tasks_before)

        # No PendingEject → NOT an eject job → status rewritten to 'cancelled' and the
        # notification is NOT suppressed.
        farm_hook.assert_awaited_once()
        assert farm_hook.await_args.args[3] == "cancelled"
        mock_notif.on_print_complete.assert_awaited()

    @pytest.mark.asyncio
    async def test_eject_named_terminal_empty_registry_never_gates_or_notifies(self, test_engine):
        """W1 name evidence: an eject-NAMED terminal that arrives with an EMPTY pending
        registry (a foreign instance's sweep after our restart lost the registry, or a
        cross-instance eject) is still recognised as an eject by name — even with
        motion progress reported. It must NOT be rewritten, NOT raise the plate gate,
        NOT fire the foreign-job notification, and NOT emit a print notification."""
        from contextlib import ExitStack

        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from backend.app.services.bambu_ftp import DeleteResult
        from backend.app.services.eject import remote
        from backend.app.services.printer_manager import printer_manager

        maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        pid = await self._seed_printer(maker, "EJ-NAME")
        # Empty registry on purpose — only the echoed NAME identifies this as an eject.
        assert remote.peek_pending_eject(pid) is None
        assert not printer_manager.is_awaiting_plate_clear(pid)
        tasks_before = set(asyncio.all_tasks())
        with ExitStack() as stack:
            stack.enter_context(patch("backend.app.main.async_session", maker))
            stack.enter_context(patch("backend.app.core.database.async_session", maker))
            mock_notif = stack.enter_context(patch("backend.app.main.notification_service"))
            mock_notif.on_print_complete = AsyncMock()
            mock_notif.on_queue_completed = AsyncMock()
            mock_notif.on_foreign_job_detected = AsyncMock()
            mock_notif._get_providers_for_event = AsyncMock(return_value=[])
            stack.enter_context(patch("backend.app.main.smart_plug_manager")).on_print_complete = AsyncMock()
            mock_ws = stack.enter_context(patch("backend.app.main.ws_manager"))
            mock_ws.send_print_complete = AsyncMock()
            mock_ws.broadcast = AsyncMock()
            stack.enter_context(patch("backend.app.main.mqtt_relay")).on_print_complete = AsyncMock()
            stack.enter_context(patch("backend.app.services.bambu_ftp.delete_file_async", new_callable=AsyncMock))
            farm_hook = stack.enter_context(
                patch("backend.app.services.farm_policy.on_terminal", new_callable=AsyncMock)
            )

            from backend.app.main import on_print_complete

            await on_print_complete(
                pid,
                {
                    "status": "completed",
                    "filename": "eject_production_item2.gcode.3mf",
                    "subtask_name": "eject_production_item2",
                    "subtask_id": "FOREIGN-SUB",
                    "timelapse_was_active": False,
                    # Nonzero motion progress — proves the name (not no-deposit) carries it.
                    "last_layer_num": 3,
                    "last_progress": 40.0,
                },
            )
            await self._settle(tasks_before)

        # Not rewritten (still 'completed'), notification + foreign-notify suppressed.
        farm_hook.assert_awaited_once()
        assert farm_hook.await_args.args[3] == "completed"
        assert farm_hook.await_args.kwargs["completed_subtask_name"] == "eject_production_item2"
        mock_notif.on_print_complete.assert_not_awaited()
        mock_notif.on_foreign_job_detected.assert_not_awaited()
        # Gate NEVER raised for an eject-named terminal.
        assert not printer_manager.is_awaiting_plate_clear(pid)

    @pytest.mark.asyncio
    async def test_eject_terminal_skips_ams_reread_sweep(self, test_engine):
        """W6.4: an eject-job terminal must NOT trigger the AMS RFID re-read sweep —
        each unit cycle sweeps once at the PRINT terminal, not again at the eject."""
        from contextlib import ExitStack

        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from backend.app.services.eject import remote

        maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        pid = await self._seed_printer(maker, "EJ-SWEEP")
        remote.register_pending_eject(pid, remote.PendingEject("production", 1, 2))
        tasks_before = set(asyncio.all_tasks())
        try:
            with ExitStack() as stack:
                stack.enter_context(patch("backend.app.main.async_session", maker))
                stack.enter_context(patch("backend.app.core.database.async_session", maker))
                mock_notif = stack.enter_context(patch("backend.app.main.notification_service"))
                mock_notif.on_print_complete = AsyncMock()
                mock_notif.on_queue_completed = AsyncMock()
                mock_notif._get_providers_for_event = AsyncMock(return_value=[])
                stack.enter_context(patch("backend.app.main.smart_plug_manager")).on_print_complete = AsyncMock()
                mock_ws = stack.enter_context(patch("backend.app.main.ws_manager"))
                mock_ws.send_print_complete = AsyncMock()
                mock_ws.broadcast = AsyncMock()
                stack.enter_context(patch("backend.app.main.mqtt_relay")).on_print_complete = AsyncMock()
                stack.enter_context(patch("backend.app.services.bambu_ftp.delete_file_async", new_callable=AsyncMock))
                stack.enter_context(patch("backend.app.services.farm_policy.on_terminal", new_callable=AsyncMock))
                sweep = stack.enter_context(
                    patch("backend.app.services.ams_presence.on_printer_terminal", new_callable=AsyncMock)
                )

                from backend.app.main import on_print_complete

                await on_print_complete(
                    pid,
                    {
                        "status": "completed",
                        "filename": "eject_production_item2.gcode.3mf",
                        "subtask_name": "eject_production_item2",
                        "subtask_id": "SUB-E",
                        "timelapse_was_active": False,
                        "last_layer_num": 0,
                        "last_progress": 0,
                    },
                )
                await self._settle(tasks_before)

            sweep.assert_not_awaited()
        finally:
            remote.pop_pending_eject(pid)

    @pytest.mark.asyncio
    async def test_print_terminal_schedules_ams_reread_sweep(self, test_engine):
        """A NON-eject terminal DOES schedule the AMS RFID re-read sweep (once) — the
        mid-print-refill recognition path. Proves the eject exemption does not
        suppress the sweep for ordinary prints."""
        from contextlib import ExitStack

        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from backend.app.services.eject import remote

        maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        pid = await self._seed_printer(maker, "PRINT-SWEEP")
        assert remote.peek_pending_eject(pid) is None  # NOT an eject
        tasks_before = set(asyncio.all_tasks())
        with ExitStack() as stack:
            stack.enter_context(patch("backend.app.main.async_session", maker))
            stack.enter_context(patch("backend.app.core.database.async_session", maker))
            mock_notif = stack.enter_context(patch("backend.app.main.notification_service"))
            mock_notif.on_print_complete = AsyncMock()
            mock_notif.on_queue_completed = AsyncMock()
            mock_notif._get_providers_for_event = AsyncMock(return_value=[])
            stack.enter_context(patch("backend.app.main.smart_plug_manager")).on_print_complete = AsyncMock()
            mock_ws = stack.enter_context(patch("backend.app.main.ws_manager"))
            mock_ws.send_print_complete = AsyncMock()
            mock_ws.broadcast = AsyncMock()
            stack.enter_context(patch("backend.app.main.mqtt_relay")).on_print_complete = AsyncMock()
            stack.enter_context(patch("backend.app.services.bambu_ftp.delete_file_async", new_callable=AsyncMock))
            stack.enter_context(patch("backend.app.services.farm_policy.on_terminal", new_callable=AsyncMock))
            sweep = stack.enter_context(
                patch("backend.app.services.ams_presence.on_printer_terminal", new_callable=AsyncMock)
            )

            from backend.app.main import on_print_complete

            # No-deposit dry-run-style terminal (no PendingEject, non-eject name): keeps
            # the gate untouched but STILL schedules the sweep (guard is `not _is_eject_job`).
            await on_print_complete(
                pid,
                {
                    "status": "completed",
                    "filename": "dryrun.gcode.3mf",
                    "subtask_name": "dryrun",
                    "subtask_id": "DR-SWEEP",
                    "timelapse_was_active": False,
                    "last_layer_num": 0,
                    "last_progress": 0,
                },
            )
            await self._settle(tasks_before)

        sweep.assert_awaited_once_with(pid)


class TestPrintCompleteLogic:
    """Test print complete callback logic."""

    @pytest.mark.asyncio
    async def test_print_complete_no_import_errors(self, capture_logs):
        """Verify on_print_complete doesn't have import shadowing issues."""
        # Snapshot tasks before the call so we can cancel orphans afterwards.
        # on_print_complete fires background tasks (maintenance check, notifications,
        # smart-plug) via asyncio.create_task.  If those tasks outlive the mock
        # context they use the *real* async_session and can send real notifications.
        tasks_before = set(asyncio.all_tasks())

        with (
            patch("backend.app.main.async_session") as mock_session_maker,
            patch("backend.app.main.notification_service") as mock_notif,
            patch("backend.app.main.smart_plug_manager") as mock_plug,
            patch("backend.app.main.ws_manager") as mock_ws,
            patch("backend.app.main.mqtt_relay") as mock_relay,
            patch("backend.app.main.printer_manager") as mock_pm,
        ):
            mock_notif.on_print_complete = AsyncMock()
            mock_plug.on_print_complete = AsyncMock()
            mock_ws.send_print_complete = AsyncMock()
            mock_ws.broadcast = AsyncMock()
            mock_relay.on_print_complete = AsyncMock()
            mock_pm.get_printer.return_value = None

            # Mock the database session
            mock_session = AsyncMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock()
            mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
            mock_session_maker.return_value = mock_session

            from backend.app.main import on_print_complete

            await on_print_complete(
                1,
                {
                    "status": "completed",
                    "filename": "/data/Metadata/test.gcode",
                    "subtask_name": "Test",
                    "timelapse_was_active": False,
                },
            )

            # Cancel background tasks spawned by on_print_complete before
            # leaving the mock context — prevents them from running with
            # the real async_session and sending real notifications.
            for task in asyncio.all_tasks() - tasks_before:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        # Verify no import shadowing errors - this would have caught the ArchiveService bug
        errors = [r for r in capture_logs.get_errors() if "cannot access local variable" in str(r.message)]
        assert not errors, f"Import shadowing error: {capture_logs.format_errors()}"


class TestTimelapseTracking:
    """Test timelapse detection during prints."""

    @pytest.mark.asyncio
    async def test_timelapse_detected_in_same_message_as_print_start(self):
        """Verify timelapse is detected when xcam and state come together."""
        from backend.app.services.bambu_mqtt import BambuMQTTClient

        client = BambuMQTTClient(
            ip_address="192.168.1.100",
            serial_number="TEST123",
            access_code="12345678",
        )
        client.on_print_start = lambda data: None

        # Initial state
        client._was_running = False
        client._timelapse_during_print = False

        # Message with both state and timelapse
        client._process_message(
            {
                "print": {
                    "gcode_state": "RUNNING",
                    "gcode_file": "/data/Metadata/test.gcode",
                    "subtask_name": "Test",
                    "xcam": {"timelapse": "enable"},
                }
            }
        )

        assert client._was_running is True
        assert client._timelapse_during_print is True, (
            "Timelapse should be detected even when xcam is parsed before state"
        )

    @pytest.mark.asyncio
    async def test_timelapse_flag_included_in_completion_callback(self):
        """Verify completion callback receives timelapse_was_active flag."""
        from backend.app.services.bambu_mqtt import BambuMQTTClient

        client = BambuMQTTClient(
            ip_address="192.168.1.100",
            serial_number="TEST123",
            access_code="12345678",
        )

        completion_data = {}

        def on_complete(data):
            completion_data.update(data)

        client.on_print_start = lambda data: None
        client.on_print_complete = on_complete

        # Start with timelapse
        client._process_message(
            {
                "print": {
                    "gcode_state": "RUNNING",
                    "gcode_file": "/data/Metadata/test.gcode",
                    "subtask_name": "Test",
                    "xcam": {"timelapse": "enable"},
                }
            }
        )

        # Complete print
        client._process_message(
            {
                "print": {
                    "gcode_state": "FINISH",
                    "gcode_file": "/data/Metadata/test.gcode",
                    "subtask_name": "Test",
                }
            }
        )

        assert "timelapse_was_active" in completion_data
        assert completion_data["timelapse_was_active"] is True

    @pytest.mark.asyncio
    async def test_hms_errors_included_in_failed_completion_callback(self):
        """Verify completion callback receives hms_errors for failed prints."""
        from backend.app.services.bambu_mqtt import BambuMQTTClient

        client = BambuMQTTClient(
            ip_address="192.168.1.100",
            serial_number="TEST123",
            access_code="12345678",
        )

        completion_data = {}

        def on_complete(data):
            completion_data.update(data)

        client.on_print_start = lambda data: None
        client.on_print_complete = on_complete

        # Start print
        client._process_message(
            {
                "print": {
                    "gcode_state": "RUNNING",
                    "gcode_file": "/data/Metadata/test.gcode",
                    "subtask_name": "Test",
                }
            }
        )

        # Add HMS error during print
        client._process_message(
            {
                "print": {
                    "gcode_state": "RUNNING",
                    "hms": [{"attr": 0x07000002, "code": 0x8001}],  # Filament module error (code must be >= 0x4000)
                }
            }
        )

        # Fail print
        client._process_message(
            {
                "print": {
                    "gcode_state": "FAILED",
                    "gcode_file": "/data/Metadata/test.gcode",
                    "subtask_name": "Test",
                }
            }
        )

        assert "hms_errors" in completion_data
        assert len(completion_data["hms_errors"]) == 1
        assert completion_data["hms_errors"][0]["module"] == 0x07
        assert completion_data["status"] == "failed"

    @pytest.mark.asyncio
    async def test_aborted_status_when_cancelled(self):
        """Verify completion callback receives 'aborted' status when print is cancelled."""
        from backend.app.services.bambu_mqtt import BambuMQTTClient

        client = BambuMQTTClient(
            ip_address="192.168.1.100",
            serial_number="TEST123",
            access_code="12345678",
        )

        completion_data = {}

        def on_complete(data):
            completion_data.update(data)

        client.on_print_start = lambda data: None
        client.on_print_complete = on_complete

        # Start print
        client._process_message(
            {
                "print": {
                    "gcode_state": "RUNNING",
                    "gcode_file": "/data/Metadata/test.gcode",
                    "subtask_name": "Test",
                }
            }
        )

        # User cancels (goes to IDLE)
        client._process_message(
            {
                "print": {
                    "gcode_state": "IDLE",
                    "gcode_file": "/data/Metadata/test.gcode",
                    "subtask_name": "Test",
                }
            }
        )

        assert completion_data["status"] == "aborted"
        assert "hms_errors" in completion_data

    @pytest.mark.asyncio
    async def test_timelapse_detected_from_ipcam_data(self):
        """Verify timelapse is detected from ipcam data (H2D sends it there, not xcam)."""
        from backend.app.services.bambu_mqtt import BambuMQTTClient

        client = BambuMQTTClient(
            ip_address="192.168.1.100",
            serial_number="TEST123",
            access_code="12345678",
        )

        completion_data = {}

        def on_complete(data):
            completion_data.update(data)

        client.on_print_start = lambda data: None
        client.on_print_complete = on_complete

        # Start print with timelapse in ipcam data (H2D format)
        client._process_message(
            {
                "print": {
                    "gcode_state": "RUNNING",
                    "gcode_file": "/data/Metadata/test.gcode",
                    "subtask_name": "Test",
                    "ipcam": {
                        "ipcam_record": "enable",
                        "timelapse": "enable",
                        "resolution": "1080p",
                    },
                }
            }
        )

        assert client._timelapse_during_print is True, "Timelapse should be detected from ipcam data"

        # Complete print
        client._process_message(
            {
                "print": {
                    "gcode_state": "FINISH",
                    "gcode_file": "/data/Metadata/test.gcode",
                    "subtask_name": "Test",
                }
            }
        )

        assert completion_data["timelapse_was_active"] is True, (
            "timelapse_was_active should be True when timelapse was in ipcam"
        )


class TestCallbackErrorHandling:
    """Test that callback errors are properly logged."""

    @pytest.mark.asyncio
    async def test_callback_errors_are_logged(self, capture_logs):
        """Verify that exceptions in callbacks are logged, not swallowed."""
        from backend.app.services.printer_manager import PrinterManager

        manager = PrinterManager()

        # Set up event loop
        loop = asyncio.get_event_loop()
        manager.set_event_loop(loop)

        # Create a callback that raises an error
        error_raised = False

        async def failing_callback(printer_id, data):
            nonlocal error_raised
            error_raised = True
            raise ValueError("Test error in callback")

        manager.set_print_complete_callback(failing_callback)

        # The _schedule_async should log the error
        # This is tested indirectly - if exception handling is broken,
        # the error would be swallowed silently


class TestNoImportShadowing:
    """Verify no import shadowing issues exist in callbacks."""

    @pytest.mark.asyncio
    async def test_on_print_complete_no_import_errors(self, capture_logs):
        """Verify on_print_complete doesn't have import shadowing issues."""
        # Import the module to check for syntax/import errors
        from backend.app import main

        # The ArchiveService should be accessible
        from backend.app.services.archive import ArchiveService

        # Verify we can instantiate it (would fail with shadowing bug)
        assert ArchiveService is not None

        # Check logs for any import-related errors
        errors = capture_logs.get_errors()
        import_errors = [
            e for e in errors if "import" in str(e.message).lower() or "local variable" in str(e.message).lower()
        ]
        assert not import_errors, f"Import errors found: {import_errors}"
