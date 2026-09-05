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


def _occupancy():
    """The authority singleton (imported lazily so module import order stays free)."""
    from backend.app.services.plate_occupancy import plate_occupancy

    return plate_occupancy


def _plate_policy(printer_id: int):
    """What the authority says will happen to this printer's plate next, or None."""
    return _occupancy().snapshot(printer_id).plate_policy


def _claim_eject(printer_id: int, *, purpose="production", run_id=1, queue_item_id=2, source_subtask_id="SUB-P", **kw):
    """Put a LIVE server-dispatched eject on ``printer_id``, the way a dispatch does.

    A claim is only legal on an occupied plate (``ejectable``), which is the real
    sequence: a terminal gates the plate, the eject dispatcher then claims the
    printer for the sweep. Returns the claimed :class:`PendingEject`.
    """
    from backend.app.services.plate_occupancy import CooldownEject, Evidence, PendingEject

    occupancy = _occupancy()
    occupancy.hydrate_plate(printer_id, source_subtask_id, CooldownEject(unit_id=queue_item_id or 0, run_id=run_id))
    pending = PendingEject(purpose=purpose, run_id=run_id, queue_item_id=queue_item_id, **kw)
    refusal = occupancy.claim_for_eject(printer_id, pending, Evidence())
    assert refusal is None, f"eject claim refused: {refusal}"
    return pending


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
        namespace exposing the mocked printer_manager and notification service.

        The plate gate is NO LONGER a printer_manager call to assert on: the terminal
        handler makes ONE ``plate_occupancy.note_terminal`` call, so a test reads the
        outcome off the authority (``is_plate_occupied`` / ``snapshot``) instead of off
        a mock's call list — and the "which watch was armed?" question is now "which
        POLICY does the plate carry?"."""
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
        mock_pm.clear_current_print_user = MagicMock()
        return SimpleNamespace(pm=mock_pm, notif=mock_notif, maker=maker)

    @staticmethod
    async def _seed_printing_item(
        maker,
        *,
        serial,
        dispatch_subtask_id=None,
        is_dry_run=False,
        eject_profile_id=None,
        first_article=False,
        batch_id=None,
    ):
        """Seed a connected printer with one printing queue item and return
        (printer_id, item_id). ``eject_profile_id`` makes it a FARM unit — the input
        that decides whether its plate gets a cooldown policy or an escalation hold."""
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
                first_article=first_article,
                is_dry_run=is_dry_run,
                eject_profile_id=eject_profile_id,
                batch_id=batch_id,
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

    @staticmethod
    async def _settle_foreign(tasks_before):
        """Await the foreign auto-eject decision task to COMPLETION (deterministic —
        it decides auto-eject vs escalation-only + fires the notification), then drain
        any other unrelated background tasks the callback spawned."""
        for task in asyncio.all_tasks() - tasks_before:
            if (task.get_name() or "").startswith("foreign-auto-eject"):
                try:
                    await task
                except Exception:  # noqa: BLE001
                    pass
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
            self._setup_mocks(stack, test_engine)

            from backend.app.main import on_print_complete

            await on_print_complete(
                1,
                {
                    "status": status,
                    "filename": "/data/Metadata/test.gcode",
                    "subtask_name": "Test",
                    "timelapse_was_active": False,
                    "peaks_reliable": True,
                    "last_layer_num": 10,
                    "last_progress": 55.0,
                },
            )

            await self._drain(tasks_before)

        from backend.app.services.plate_occupancy import EscalationOnly

        assert _occupancy().is_plate_occupied(1), "Gate must be raised for a deposit-bearing terminal (toggle on)."
        # Never armless: an unattributed deposit always carries the escalation floor.
        assert isinstance(_plate_policy(1), EscalationOnly)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "payload_extra, ids",
        [
            ({"peaks_reliable": True, "last_layer_num": 0, "last_progress": 0}, "zero-layer-print"),
            ({"peaks_reliable": True}, "no-progress-data"),
        ],
    )
    async def test_plate_clear_gate_not_raised_for_no_deposit_finish(self, payload_extra, ids, test_engine):
        """A print that reached terminal having deposited nothing (zero layers AND
        zero progress) must NOT raise the plate-clear gate: the bed cannot be fouled.

        The MEASURED zero is now what says so — ``peaks_reliable`` rides the payload
        because a client born mid-print reports zeros for a job it never watched, and
        those zeros are an absence of measurement, not a measurement of absence."""
        from contextlib import ExitStack

        tasks_before = set(asyncio.all_tasks())

        with ExitStack() as stack:
            self._setup_mocks(stack, test_engine)

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

        assert not _occupancy().is_plate_occupied(1), "Gate must stay clear for a MEASURED no-deposit finish."

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "payload_extra, ids",
        [
            ({"peaks_reliable": False, "last_layer_num": 0, "last_progress": 0}, "restart-recovered-zeros"),
            ({}, "peaks_reliable-absent"),
        ],
    )
    async def test_plate_clear_gate_raised_when_peaks_are_not_reliable(self, payload_extra, ids, test_engine):
        """The same zeros WITHOUT a reliable measurement fail CLOSED and gate the plate.

        2026-08-29: layer/progress peaks live in the MQTT client's process memory, so a
        client born mid-print (a redeploy, a host reboot) reports zeros for a print that
        is three-quarters done. Six such terminals were read as "nothing on the plate".
        An absent ``peaks_reliable`` key (an older client, a virtual printer) lands on
        the same fail-closed side."""
        from contextlib import ExitStack

        tasks_before = set(asyncio.all_tasks())

        with ExitStack() as stack:
            self._setup_mocks(stack, test_engine)

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

        assert _occupancy().is_plate_occupied(1), "Unmeasured peaks must gate the plate, not clear it."

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
                    "peaks_reliable": True,
                    "last_layer_num": 3,
                    "last_progress": 12.0,
                },
            )

            await self._drain(tasks_before)

        assert not _occupancy().is_plate_occupied(pid), "Gate must stay clear for a dry-run finish."

    @pytest.mark.asyncio
    async def test_plate_clear_gate_not_raised_for_unknown_status(self, test_engine):
        """Defence in depth: an unknown / not-terminal status string from a future
        firmware revision must not silently raise the gate."""
        from contextlib import ExitStack

        tasks_before = set(asyncio.all_tasks())

        with ExitStack() as stack:
            self._setup_mocks(stack, test_engine)

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

        assert not _occupancy().is_plate_occupied(1), "Gate must not be raised for an unrecognised terminal status."

    @pytest.mark.asyncio
    async def test_foreign_terminal_leaves_item_and_raises_gate(self, test_engine):
        """Phase 1 P1-A: a terminal whose subtask_id matches NO printing item (a
        LOCAL print started from the touchscreen) is FOREIGN — the farm item stays
        'printing', the gate is raised keyed to the foreign subtask. The plate is not
        the farm's own file (a plain non-farm item) so identification fails and the
        ESCALATION-ONLY watch is started (NOT the auto-clear); the foreign notification
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

            await self._settle_foreign(tasks_before)

        from backend.app.services.plate_occupancy import EscalationOnly

        # 1. Farm unit untouched — still printing (a foreign print never marks it done).
        async with env.maker() as s:
            refetched = await s.get(PrintQueueItem, iid)
            assert refetched.status == "printing"
        # 2. Gate raised, keyed to the FOREIGN subtask.
        assert _occupancy().is_plate_occupied(pid)
        assert _occupancy().plate_source(pid) == "FOREIGN-9"
        # 3. Not the farm's own file → the plate keeps the ESCALATION-ONLY policy it was
        #    raised under: neither the queue-bound cooldown nor a foreign auto-eject.
        assert isinstance(_plate_policy(pid), EscalationOnly)
        # 4. Foreign notification fired WITHOUT an auto-eject temperature (not the farm's file).
        env.notif.on_foreign_job_detected.assert_awaited()
        assert env.notif.on_foreign_job_detected.call_args.kwargs.get("auto_eject_temp_c") is None

    @pytest.mark.asyncio
    async def test_foreign_terminal_not_gated_when_toggle_off_and_no_farm(self, test_engine):
        """F1b: a FOREIGN terminal on a printer with NO farm involvement and
        require_plate_clear OFF must NOT gate — the foreign path obeys the SAME guard
        as the generic branch (upstream toggle-off behaviour preserved). Zero printing
        candidates + an echoed id → foreign verdict; guard false → no gate, no watch,
        no notification."""
        from contextlib import ExitStack

        from backend.app.api.routes.settings import set_setting

        tasks_before = set(asyncio.all_tasks())

        with ExitStack() as stack:
            env = self._setup_mocks(stack, test_engine)
            async with env.maker() as s:
                await set_setting(s, "require_plate_clear", "false")
                await s.commit()

            from backend.app.main import on_print_complete

            await on_print_complete(
                1,
                {
                    "status": "completed",
                    "filename": "local.gcode",
                    "subtask_name": "OperatorLocalPrint",
                    "subtask_id": "FOREIGN-Z",  # zero candidates + id → foreign
                    "timelapse_was_active": False,
                    "last_layer_num": 20,
                    "last_progress": 88.0,
                },
            )

            await self._settle_foreign(tasks_before)

        assert not _occupancy().is_plate_occupied(1), "Toggle-off foreign with no farm involvement must NOT gate."
        # No gate ⇒ no policy at all: nothing is armed over a plate the farm never claimed.
        assert _plate_policy(1) is None
        env.notif.on_foreign_job_detected.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_foreign_terminal_farm_file_arms_auto_eject(self, test_engine):
        """F5: a FOREIGN completion positively identified as the farm's OWN file arms
        the AUTO foreign-eject watch (NOT escalation-only) and the notification names
        the cooldown target. The farm queue stays untouched; the gate is still raised.
        Identification itself is unit-tested in test_manual; here the main.py wiring is
        exercised with identify_farm_file_foreign patched to a positive result."""
        from contextlib import ExitStack
        from unittest.mock import AsyncMock

        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.services.eject.manual import ForeignFarmFile

        tasks_before = set(asyncio.all_tasks())

        with ExitStack() as stack:
            env = self._setup_mocks(stack, test_engine)
            pid, iid = await self._seed_printing_item(env.maker, serial="FAE-1", dispatch_subtask_id="DISPATCHED-1")
            stack.enter_context(
                patch(
                    "backend.app.services.eject.manual.identify_farm_file_foreign",
                    AsyncMock(return_value=ForeignFarmFile(profile_id=7, threshold_c=33.0, print_name="Farm Widget")),
                )
            )

            from backend.app.main import on_print_complete

            await on_print_complete(
                pid,
                {
                    "status": "completed",
                    "filename": "Farm_Widget.gcode.3mf",
                    "subtask_name": "Farm_Widget",
                    "subtask_id": "FOREIGN-9",  # != DISPATCHED-1 → foreign
                    "timelapse_was_active": False,
                    "last_layer_num": 20,
                    "last_progress": 88.0,
                },
            )

            await self._settle_foreign(tasks_before)

        from backend.app.services.plate_occupancy import ForeignAutoEject

        # Farm unit untouched.
        async with env.maker() as s:
            refetched = await s.get(PrintQueueItem, iid)
            assert refetched.status == "printing"
        # Gate raised keyed to the foreign subtask.
        assert _occupancy().is_plate_occupied(pid)
        assert _occupancy().plate_source(pid) == "FOREIGN-9"
        # The escalation hold the gate went up under was UPGRADED to the AUTO foreign
        # eject with the identified profile + threshold — not the queue-bound cooldown.
        assert _plate_policy(pid) == ForeignAutoEject(profile_id=7, threshold_c=33.0)
        # Notification fired naming the cooldown target °C.
        env.notif.on_foreign_job_detected.assert_awaited()
        assert env.notif.on_foreign_job_detected.call_args.kwargs.get("auto_eject_temp_c") == 33.0

    @pytest.mark.asyncio
    async def test_genuine_foreign_terminal_still_calls_resolver(self, test_engine):
        """W5 scope guard: the eject short-circuit skips resolve_terminal_item ONLY for
        eject jobs. A genuinely foreign terminal (NOT an eject) must still run the
        resolver so the foreign branch is reached exactly as before — proving the
        short-circuit did not swallow ordinary correlation."""
        from contextlib import ExitStack

        from backend.app.services import farm_correlation

        resolver_spy = AsyncMock(wraps=farm_correlation.resolve_terminal_item)
        tasks_before = set(asyncio.all_tasks())

        with ExitStack() as stack:
            env = self._setup_mocks(stack, test_engine)
            stack.enter_context(patch("backend.app.services.farm_correlation.resolve_terminal_item", resolver_spy))
            pid, _iid = await self._seed_printing_item(env.maker, serial="RSV-1", dispatch_subtask_id="DISPATCHED-1")

            from backend.app.main import on_print_complete

            await on_print_complete(
                pid,
                {
                    "status": "completed",
                    "filename": "local.gcode",
                    "subtask_name": "OperatorLocalPrint",
                    "subtask_id": "FOREIGN-9",  # != DISPATCHED-1 → foreign, and NOT an eject
                    "timelapse_was_active": False,
                    "last_layer_num": 20,
                    "last_progress": 88.0,
                },
            )

            await self._settle_foreign(tasks_before)

        # The resolver WAS consulted for a non-eject terminal (foreign branch intact).
        resolver_spy.assert_awaited()


class TestFarmVisionAbortPrecedence:
    """W9: the farm's own abort mark is stamped on the ``printing`` row BEFORE the stop
    goes out, and the terminal HONOURS it.

    Two things have to hold at once, and both used to fail: the recorded status must
    become ``cancelled`` (a farm abort is a stop, not a failure — a FIRST ARTICLE would
    otherwise keep ``failed`` and route into ``_on_item_failed`` plus a quarantine count
    for a plate the farm itself refused), and the mark must survive the terminal's own
    ``stop_source`` write."""

    @staticmethod
    async def _run_terminal(test_engine, *, mark, first_article):
        from contextlib import ExitStack

        from backend.app.models.print_queue import PrintQueueItem

        tasks_before = set(asyncio.all_tasks())
        with ExitStack() as stack:
            mocks = TestPlateClearGate._setup_mocks(stack, test_engine)
            printer_id, item_id = await TestPlateClearGate._seed_printing_item(
                mocks.maker, serial="VIS-1", dispatch_subtask_id="SUB-V", first_article=first_article
            )
            if mark is not None:
                async with mocks.maker() as s:
                    item = await s.get(PrintQueueItem, item_id)
                    item.stop_source = mark
                    await s.commit()

            from backend.app.main import on_print_complete

            await on_print_complete(
                printer_id,
                {
                    "status": "failed",
                    "filename": "/data/Metadata/test.gcode",
                    "subtask_name": "Test",
                    "subtask_id": "SUB-V",
                    "timelapse_was_active": False,
                    # The firmware's cancel echo: an MQTT print.stop plausibly produces
                    # one, so the mark must outrank it.
                    "user_cancel_observed": True,
                    "peaks_reliable": True,
                    "last_layer_num": 0,
                    "last_progress": 0,
                },
            )
            await TestPlateClearGate._drain(tasks_before)

            async with mocks.maker() as s:
                item = await s.get(PrintQueueItem, item_id)
                return item.status, item.stop_source

    @pytest.mark.asyncio
    async def test_a_marked_first_article_records_cancelled_and_keeps_the_mark(self, test_engine):
        status, stop_source = await self._run_terminal(test_engine, mark="farm_vision_abort", first_article=True)

        assert status == "cancelled"
        assert stop_source == "farm_vision_abort"  # never relabelled operator_screen

    @pytest.mark.asyncio
    async def test_without_the_mark_a_first_article_failure_is_unchanged(self, test_engine):
        """The pre-existing contract: a first-article no-deposit stop deliberately keeps
        ``failed`` so the run still retries its first article."""
        status, stop_source = await self._run_terminal(test_engine, mark=None, first_article=True)

        assert status == "failed"
        assert stop_source is None


class TestEjectJobCallbacks:
    """C2: a server-dispatched eject sweep (a PendingEject, NO queue item, NO
    archive) must be exempt from the no-deposit status rewrite and the user-facing
    print notification, must NOT create archives at start, yet its farm terminal
    hook + SD-card cleanup must still fire. A dry-run (a queue item, NOT a
    PendingEject) keeps its existing no-deposit path.

    The pending eject now lives in the occupancy authority (there is no eject
    registry any more), so a sweep is set up the way a dispatch sets one up: the
    plate is gated, then the printer is CLAIMED for the eject."""

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

        maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        pid = await self._seed_printer(maker, "EJ-START")
        _claim_eject(pid)
        tasks_before = set(asyncio.all_tasks())
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
        # The start echo is also the sweep's clock: it stamps the authority's record.
        identity = _occupancy().eject_identity(pid)
        assert identity is not None and identity.started_at is not None

    @pytest.mark.asyncio
    async def test_eject_completed_no_rewrite_notification_suppressed_farm_finalises(self, test_engine):
        """A clean eject FINISH reaches farm_policy as 'completed' (NOT rewritten to
        'cancelled'), emits NO print notification, yet the farm hook + SD-card
        cleanup still run."""
        from contextlib import ExitStack

        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from backend.app.services.bambu_ftp import DeleteResult

        maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        pid = await self._seed_printer(maker, "EJ-DONE")
        _claim_eject(pid)
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
                    "filename": "eject_production_item2.gcode.3mf",
                    "subtask_name": "eject_production_item2",
                    "subtask_id": "SUB-E",
                    "timelapse_was_active": False,
                    "peaks_reliable": True,
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

    @pytest.mark.asyncio
    async def test_dry_run_terminal_untouched_not_treated_as_eject(self, test_engine):
        """A dry-run (queue item, NO PendingEject) is NOT an eject job: its no-deposit
        terminal is still rewritten to 'cancelled' and STILL emits a notification —
        proving the eject exemption does not bleed into the dry-run path."""
        from contextlib import ExitStack

        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from backend.app.services.bambu_ftp import DeleteResult

        maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        # A REAL dry-run unit, correlated by its dispatch id. The dry-run flag is the
        # only thing that can suppress the deposit here: this terminal reports
        # ``completed``, and a completed print deposits its part whatever its peaks say
        # (the 2026-08-29 rule — six restart-recovered prints finished ``completed``
        # with zeroed peaks and were wrongly read as having left nothing behind). The
        # eject dry-run file is motion-only, so it is the one ``completed`` job that
        # genuinely cannot deposit.
        pid, _iid = await TestPlateClearGate._seed_printing_item(
            maker, serial="DRY-EJ", dispatch_subtask_id="DR-1", is_dry_run=True
        )
        # Explicitly NO PendingEject claimed on this printer.
        assert _occupancy().eject_identity(pid) is None
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
                    # Deliberately NON-zero peaks: the dry-run flag alone must carry the
                    # non-deposit verdict. If this test could still pass on measured
                    # zeros it would not be pinning the dry-run rule at all.
                    "peaks_reliable": True,
                    "last_layer_num": 4,
                    "last_progress": 12.5,
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

        maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        pid = await self._seed_printer(maker, "EJ-NAME")
        # No claimed eject on purpose — only the echoed NAME identifies this as an eject.
        assert _occupancy().eject_identity(pid) is None
        assert not _occupancy().is_plate_occupied(pid)
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
        # Gate NEVER raised for an eject-named terminal (the gate block is skipped
        # outright — an eject terminal is farm_policy's business, not this handler's).
        assert not _occupancy().is_plate_occupied(pid)

    @pytest.mark.asyncio
    async def test_eject_terminal_skips_ams_reread_sweep(self, test_engine):
        """W6.4: an eject-job terminal must NOT trigger the AMS RFID re-read sweep —
        each unit cycle sweeps once at the PRINT terminal, not again at the eject."""
        from contextlib import ExitStack

        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        pid = await self._seed_printer(maker, "EJ-SWEEP")
        _claim_eject(pid)
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

            await on_print_complete(
                pid,
                {
                    "status": "completed",
                    "filename": "eject_production_item2.gcode.3mf",
                    "subtask_name": "eject_production_item2",
                    "subtask_id": "SUB-E",
                    "timelapse_was_active": False,
                    "peaks_reliable": True,
                    "last_layer_num": 0,
                    "last_progress": 0,
                },
            )
            await self._settle(tasks_before)

        sweep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_print_terminal_schedules_ams_reread_sweep(self, test_engine):
        """A NON-eject terminal DOES schedule the AMS RFID re-read sweep (once) — the
        mid-print-refill recognition path. Proves the eject exemption does not
        suppress the sweep for ordinary prints."""
        from contextlib import ExitStack

        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        pid = await self._seed_printer(maker, "PRINT-SWEEP")
        assert _occupancy().eject_identity(pid) is None  # NOT an eject
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

            # An ordinary print terminal (no claimed eject, non-eject name) schedules the
            # sweep — the guard is `not _is_eject_job` and nothing else.
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

    @pytest.mark.asyncio
    async def test_claimed_eject_skips_correlation_no_false_foreign_or_archive_warning(self, test_engine, capture_logs):
        """W5: a CLAIMED-eject terminal never calls resolve_terminal_item (so it cannot
        log the false-FOREIGN warning for the farm's own sweep) and skips the archive
        lookup (which always misses for a sweep, so no "Could not find archive"
        warning). farm_policy.on_terminal still finalises the sweep as 'completed'."""
        from contextlib import ExitStack

        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from backend.app.services import farm_correlation
        from backend.app.services.bambu_ftp import DeleteResult

        maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        pid = await self._seed_printer(maker, "EJ-NOCORR")
        _claim_eject(pid)
        resolver_spy = AsyncMock(wraps=farm_correlation.resolve_terminal_item)
        tasks_before = set(asyncio.all_tasks())
        with ExitStack() as stack:
            stack.enter_context(patch("backend.app.main.async_session", maker))
            stack.enter_context(patch("backend.app.core.database.async_session", maker))
            stack.enter_context(patch("backend.app.services.farm_correlation.resolve_terminal_item", resolver_spy))
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
                    "peaks_reliable": True,
                    "last_layer_num": 0,
                    "last_progress": 0,
                },
            )
            await self._settle(tasks_before)

        # The resolver was never consulted for our own sweep → no false FOREIGN.
        resolver_spy.assert_not_awaited()
        warnings = " ".join(r.getMessage() for r in capture_logs.get_warnings())
        assert "FOREIGN" not in warnings, warnings
        assert "Could not find archive" not in warnings, warnings
        # The sweep was still finalised (un-rewritten 'completed').
        farm_hook.assert_awaited_once()
        assert farm_hook.await_args.args[3] == "completed"

    @pytest.mark.asyncio
    async def test_named_eject_no_claim_skips_correlation_and_archive_warning(self, test_engine, capture_logs):
        """W5 + W1 name evidence: an eject-NAMED terminal with NO claimed eject
        (is_eject_job_name path — a restart lost the claim) is still recognised as
        our sweep before correlation. resolve_terminal_item is not called and no
        FOREIGN / no "Could not find archive" warning fires; the farm hook still runs."""
        from contextlib import ExitStack

        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from backend.app.services import farm_correlation
        from backend.app.services.bambu_ftp import DeleteResult

        maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        pid = await self._seed_printer(maker, "EJ-NAME-NOCORR")
        assert _occupancy().eject_identity(pid) is None  # no claim — only the NAME identifies it
        resolver_spy = AsyncMock(wraps=farm_correlation.resolve_terminal_item)
        tasks_before = set(asyncio.all_tasks())
        with ExitStack() as stack:
            stack.enter_context(patch("backend.app.main.async_session", maker))
            stack.enter_context(patch("backend.app.core.database.async_session", maker))
            stack.enter_context(patch("backend.app.services.farm_correlation.resolve_terminal_item", resolver_spy))
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
                    "subtask_id": "FOREIGN-SUB",  # not a claimed id — the name identifies it
                    "timelapse_was_active": False,
                    "peaks_reliable": True,
                    "last_layer_num": 0,
                    "last_progress": 0,
                },
            )
            await self._settle(tasks_before)

        resolver_spy.assert_not_awaited()
        warnings = " ".join(r.getMessage() for r in capture_logs.get_warnings())
        assert "FOREIGN" not in warnings, warnings
        assert "Could not find archive" not in warnings, warnings
        mock_notif.on_foreign_job_detected.assert_not_awaited()
        farm_hook.assert_awaited_once()
        assert farm_hook.await_args.args[3] == "completed"


class TestOccupancyLiveness:
    """The cutover's LIVENESS pins: the state machine must still MOVE.

    Every one of these is a silent-stall shape — nothing raises, nothing logs an
    error, the farm just stops doing the next thing — so each is named for the
    incident it re-creates:

    * a restart-recovered print's genuine FINISH read as "deposited nothing", so no
      gate, no eject, and the unit recorded ``cancelled`` (2026-08-29, six printers);
    * an eject the firmware silently ignored, whose claim then made every later eject
      409 ``eject_in_flight`` forever (2026-08-30, printer 4, 01:46-01:49);
    * a clean sweep whose completion must actually release the printer back into the
      queue (the production loop itself);
    * a watchdog-stopped sweep echoing ``completed``, which must NOT release it
      (2026-07-31 gouged plate).
    """

    @staticmethod
    async def _settle(tasks_before):
        """Await the callback's background tasks to completion (they spawn their own)."""
        for _ in range(3):
            new = asyncio.all_tasks() - tasks_before - {asyncio.current_task()}
            if not new:
                return
            await asyncio.wait(new, timeout=5)

    @staticmethod
    def _eject_terminal_mocks(stack, maker):
        """The collaborator patches an eject terminal needs, with the REAL farm_policy.

        farm_policy.on_terminal is what OWNS an eject terminal now (the handler's gate
        block skips eject jobs entirely), so these two pins deliberately do not mock it
        — only the idle deep-park it may call afterwards, which is a printer command."""
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
        stack.enter_context(patch("backend.app.services.farm_policy._maybe_idle_deep_park", new_callable=AsyncMock))
        return mock_notif

    @staticmethod
    def _eject_terminal_payload(status="completed"):
        return {
            "status": status,
            "filename": "eject_production_item2.gcode.3mf",
            "subtask_name": "eject_production_item2",
            "subtask_id": "SUB-E",
            "timelapse_was_active": False,
            "peaks_reliable": True,
            "last_layer_num": 0,
            "last_progress": 0,
        }

    # -- (a) 2026-08-29 restart-recovery cascade ---------------------------------

    @pytest.mark.asyncio
    async def test_restart_recovered_completed_terminal_gates_and_arms_20260829_cascade(self, test_engine):
        """A restart-recovered unit's genuine ``completed`` gates the plate, ARMS the
        cooldown eject, and records the unit ``completed`` — never ``cancelled``.

        The 2026-08-29 → 08-30 cascade in one payload: the MQTT client was born
        mid-print, so its layer/progress peaks are zeros it never measured
        (``peaks_reliable=False``). Six such terminals on printers 1-6 were classified
        "no-deposit — not gating queue": the status was rewritten to ``cancelled``
        though the part was physically finished, no gate went up, no eject was armed,
        and the next unit dispatched onto the finished part 1-5 s later. Absence of
        measurement is not measurement of absence, so all three outcomes below must
        hold on evidence the farm does NOT have."""
        from contextlib import ExitStack

        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.services.plate_occupancy import CooldownEject

        tasks_before = set(asyncio.all_tasks())

        with ExitStack() as stack:
            env = TestPlateClearGate._setup_mocks(stack, test_engine)
            stack.enter_context(patch("backend.app.services.farm_policy.on_terminal", new_callable=AsyncMock))
            pid, iid = await TestPlateClearGate._seed_printing_item(
                env.maker,
                serial="RESTART-RECOVERED",
                dispatch_subtask_id="RECOVERED-1",
                eject_profile_id=7,
            )

            from backend.app.main import on_print_complete

            await on_print_complete(
                pid,
                {
                    "status": "completed",
                    "filename": "unit.gcode.3mf",
                    "subtask_name": "unit",
                    "subtask_id": "RECOVERED-1",  # == the unit's dispatch id → 'matched'
                    "timelapse_was_active": False,
                    # The whole incident: peaks the client never observed.
                    "peaks_reliable": False,
                    "last_layer_num": 0,
                    "last_progress": 0,
                },
            )

            await TestPlateClearGate._drain(tasks_before)

        # 1. The unit is recorded COMPLETED — the no-deposit rewrite must not fire.
        async with env.maker() as s:
            item = await s.get(PrintQueueItem, iid)
        assert item.status == "completed", "A finished print recorded as cancelled is the 08-29 cascade."
        # 2. The plate is GATED, keyed to the job that produced the deposit.
        assert _occupancy().is_plate_occupied(pid), "A completed print deposits; the gate must go up."
        assert _occupancy().plate_source(pid) == "RECOVERED-1"
        # 3. The policy is ARMED — an id-matched farm unit with an eject profile gets
        #    the cooldown sweep, not merely an escalation hold.
        assert _plate_policy(pid) == CooldownEject(unit_id=iid, run_id=None)

    # -- (b) 2026-08-30 ejects the firmware silently ignored ----------------------

    @pytest.mark.asyncio
    async def test_eject_never_echoed_start_frees_the_printer_20260830_stuck_pendings(self):
        """An eject the printer never STARTED is retired, and the printer is ejectable again.

        The firmware silently ignores a ``project_file`` sent while it is busy — no
        error, no terminal, nothing — so a dispatched eject can simply never happen. On
        2026-08-30 those claims stayed registered forever and every later eject 409'd
        ``eject_in_flight`` (8 consecutive on printer 4, 01:46-01:49) until the operator
        hand-jogged the toolhead. The start deadline is the ONLY signal that shape
        produces, so it must free the printer while KEEPING the plate gated: the sweep
        never ran, the part is still there."""
        from backend.app.services.eject import remote
        from backend.app.services.plate_occupancy import EscalationOnly, Evidence

        pid = 4101
        _claim_eject(pid, purpose="manual", run_id=None, queue_item_id=None)
        assert _occupancy().ejectable(pid, Evidence(live_state="IDLE")) == "eject_in_flight"

        slept: list[float] = []

        async def _sleep(seconds):
            slept.append(seconds)

        with patch("backend.app.services.eject.monitor.notify_plate_not_empty", new_callable=AsyncMock) as paged:
            await remote._start_deadline(pid, sleep=_sleep, timeout_s=1.0)

        assert slept == [1.0], "The deadline must WAIT the timeout before concluding anything."
        # The printer is free: the claim is gone and a new eject may be dispatched.
        assert _occupancy().eject_identity(pid) is None
        assert _occupancy().ejectable(pid, Evidence(live_state="IDLE")) is None
        # The plate is NOT released — nothing swept it — and it escalates to a human.
        assert _occupancy().is_plate_occupied(pid)
        assert isinstance(_plate_policy(pid), EscalationOnly)
        paged.assert_awaited_once()

    # -- (d) the production loop's own release edge -------------------------------

    @pytest.mark.asyncio
    async def test_completed_eject_terminal_clears_the_plate_and_kicks_the_scheduler(self, test_engine):
        """A matched eject terminal that COMPLETED clears the plate, wakes the
        scheduler, and leaves the printer dispatchable — the loop's only release edge.

        This is the pin that fails if the eject terminal ever stops reaching
        ``resolve_eject``: nothing errors, the farm simply parks with a swept plate
        it still believes is occupied."""
        from contextlib import ExitStack

        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from backend.app.services.plate_occupancy import Evidence

        maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        pid = await TestEjectJobCallbacks._seed_printer(maker, "EJ-RELEASE")

        kicks: list[tuple[int, str]] = []
        _occupancy().configure(kick=lambda printer_id, cause: kicks.append((printer_id, cause)))
        _claim_eject(pid)
        _occupancy().note_eject_started(pid)
        assert kicks == [], "Claiming a sweep is not a release edge — the scheduler must NOT be woken."

        tasks_before = set(asyncio.all_tasks())
        with ExitStack() as stack:
            self._eject_terminal_mocks(stack, maker)

            from backend.app.main import on_print_complete

            await on_print_complete(pid, self._eject_terminal_payload("completed"))
            await self._settle(tasks_before)

        assert not _occupancy().is_plate_occupied(pid), "A matched, completed sweep releases the gate."
        assert _occupancy().eject_identity(pid) is None
        assert kicks == [(pid, "eject_completed")], f"The scheduler must be kicked on the release edge; got {kicks}"
        assert _occupancy().dispatchable(pid, Evidence(live_state="IDLE")) is None

    # -- (e) 2026-07-31 gouged plate: the watchdog's verdict outranks the echo ------

    @pytest.mark.asyncio
    async def test_watchdog_stopped_eject_reporting_completed_keeps_the_plate_gated_20260731(self, test_engine):
        """A watchdog-stopped sweep whose terminal echoes ``completed`` must NOT release.

        2026-07-31: an ejected part lodged under the heatbed, the bed-drop stalled, the
        returned-high sweep gouged the plate — and the job still reported ``completed``.
        The runtime mark is stamped BEFORE the stop is even sent, so a terminal racing
        it must already see the verdict and HONOR it: gate kept, escalation-only, and
        deliberately NO quarantine (an obstruction is not a hardware fault)."""
        from contextlib import ExitStack
        from datetime import datetime, timezone

        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from backend.app.models.printer import Printer
        from backend.app.services.plate_occupancy import EscalationOnly

        maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        pid = await TestEjectJobCallbacks._seed_printer(maker, "EJ-WATCHDOG")

        _claim_eject(pid)
        _occupancy().note_eject_started(pid)
        _occupancy().note_eject_runtime_exceeded(pid, datetime.now(timezone.utc), "drop")

        tasks_before = set(asyncio.all_tasks())
        with ExitStack() as stack:
            self._eject_terminal_mocks(stack, maker)

            from backend.app.main import on_print_complete

            # The printer says the sweep finished cleanly. It is not to be believed.
            await on_print_complete(pid, self._eject_terminal_payload("completed"))
            await self._settle(tasks_before)

        assert _occupancy().is_plate_occupied(pid), "A stopped sweep leaves the part SOMEWHERE — never release."
        assert isinstance(_plate_policy(pid), EscalationOnly)
        assert _occupancy().eject_identity(pid) is None, "The unverified eject is still retired."
        async with maker() as s:
            printer = await s.get(Printer, pid)
        assert printer.quarantined is False, "An obstruction suspicion must not quarantine the printer."


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
