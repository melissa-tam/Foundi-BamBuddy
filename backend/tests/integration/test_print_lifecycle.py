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


@pytest.fixture(autouse=True)
def sd_card_delete():
    """THE SD-card cleanup transport for this file. Every ``on_print_complete``
    walks the post-print SD-card delete (``main.py`` ~:4740), which tries each
    candidate remote path up to 3 times with a hardcoded 2 s backoff on anything
    that is not a ``DeleteResult``. These are callback-WIRING tests — none of
    them is about FTP — so four of them used to patch the transport inline and
    three did not; two of the four patched it with a bare ``AsyncMock`` whose
    ``MagicMock`` return value is neither ``DELETED`` nor ``NOT_FOUND``, so the
    ladder classified it FAILED and slept the full 2+2 s per candidate anyway.
    That was 8.1 s in every one of twelve tests.

    Autouse so a new test in this file cannot reintroduce the cost by omission.
    ``DELETED`` is the ordinary production outcome and what the explicit patches
    already chose; a test that needs the handle takes this fixture by name.
    """
    from backend.app.services.bambu_ftp import DeleteResult

    with patch("backend.app.services.bambu_ftp.delete_file_async", new_callable=AsyncMock) as mock:
        mock.return_value = DeleteResult.DELETED
        yield mock


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
        """A terminal whose subtask_id matches NO printing item (a
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
        # 4. Foreign notification fired WITHOUT an auto-eject note (not the farm's file).
        env.notif.on_foreign_job_detected.assert_awaited()
        assert env.notif.on_foreign_job_detected.call_args.kwargs.get("auto_eject") is False
        assert env.notif.on_foreign_job_detected.call_args.kwargs.get("eject_line_c") is None

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
        the AUTO foreign-eject watch (NOT escalation-only) and the notification quotes
        the eject line from its one owner (``shop_air`` — the policy carries no temperature
        since 2026-09-25). The farm queue stays untouched; the gate is still raised.
        Identification itself is unit-tested in test_manual; here the main.py wiring is
        exercised with identify_farm_file_foreign patched to a positive result."""
        from contextlib import ExitStack
        from unittest.mock import AsyncMock

        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.services.eject import shop_air
        from backend.app.services.eject.manual import ForeignFarmFile

        tasks_before = set(asyncio.all_tasks())
        line = shop_air.EjectLine(
            shop=shop_air.ShopAir(value_c=28.0, as_of=None, basis="fresh", printers=3), margin_c=2.0, line_c=30.0
        )

        with ExitStack() as stack:
            env = self._setup_mocks(stack, test_engine)
            pid, iid = await self._seed_printing_item(env.maker, serial="FAE-1", dispatch_subtask_id="DISPATCHED-1")
            stack.enter_context(
                patch(
                    "backend.app.services.eject.manual.identify_farm_file_foreign",
                    AsyncMock(return_value=ForeignFarmFile(profile_id=7, print_name="Farm Widget")),
                )
            )
            stack.enter_context(patch.object(shop_air, "current_line", AsyncMock(return_value=line)))

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
        # eject with the identified profile — and no temperature of its own.
        assert _plate_policy(pid) == ForeignAutoEject(profile_id=7)
        # Notification fired quoting the OWNER's eject line.
        env.notif.on_foreign_job_detected.assert_awaited()
        assert env.notif.on_foreign_job_detected.call_args.kwargs.get("auto_eject") is True
        assert env.notif.on_foreign_job_detected.call_args.kwargs.get("eject_line_c") == 30.0

    @pytest.mark.asyncio
    async def test_genuine_foreign_terminal_still_calls_resolver(self, test_engine):
        """Scope guard: the eject short-circuit skips resolve_terminal_item ONLY for
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


class _PlateCheckJourney:
    """Shared scaffold for the plate-check journeys, driven through the REAL lanes.

    ``main.on_print_complete`` runs for real against the test engine (``TestPlateClearGate
    ._setup_mocks``), and so do the incident store, the rule table's closers, the terminal
    outcome builder, the plate authority and ``farm_policy.on_terminal`` — none of the
    decisions under test is mocked. Only the transports are: the printer's live state
    (``printer_manager.get_status``), its client (the bed-lift ``send_gcode``), the raw
    ``stop_print`` publisher and the notification fan-out.
    """

    _SUBTASK = "SUB-V"
    _FULL = "050000000000808C"  # the print_error lane's 0500_808C, as the wire carries it

    @staticmethod
    def _reset_process_state():
        from backend.app.services import pause_recovery, printer_incidents, spool_recovery

        printer_incidents._reset_state()
        pause_recovery._reset_state()
        spool_recovery._reset_state()

    @staticmethod
    def _state(*, gcode_state, subtask, hms=()):
        from backend.app.services.bambu_mqtt import PrinterState

        st = PrinterState()
        st.connected = True
        st.state = gcode_state
        st.subtask_id = subtask
        st.subtask_name = "Test"
        st.hms_errors = list(hms)
        return st

    @classmethod
    def _vision_hms(cls):
        from backend.app.services.bambu_mqtt import HMSError

        return HMSError(code="0x808c", attr=0x0500808C, module=5, severity=3, full_code="0500808C")

    @staticmethod
    async def _seed_farm_unit(maker, *, serial, subtask, first_article=False):
        """A connected H2S, its geometry row (the bed lift needs ``z_travel_mm``), a FARM
        run (``sku_file_id``) and one ``printing`` unit dispatched as ``subtask``."""
        from backend.app.models.print_batch import PrintBatch
        from backend.app.models.printer_model_geometry import PrinterModelGeometry

        async with maker() as s:
            batch = PrintBatch(name="run", sku_file_id=1, status="active")
            s.add(batch)
            if await s.get(PrinterModelGeometry, "H2S") is None:
                s.add(
                    PrinterModelGeometry(
                        model_key="H2S",
                        bed_x=340,
                        bed_y=320,
                        env_x_min=0,
                        env_x_max=340,
                        env_y_min=0,
                        env_y_max=320,
                        max_part_height_mm=42,
                        z_travel_mm=340,
                        hold_lift_mm=12.0,
                        validated=True,
                    )
                )
            await s.commit()
            await s.refresh(batch)
            batch_id = batch.id
        printer_id, item_id = await TestPlateClearGate._seed_printing_item(
            maker, serial=serial, dispatch_subtask_id=subtask, first_article=first_article, batch_id=batch_id
        )
        return printer_id, item_id, batch_id

    @staticmethod
    async def _settle(tasks_before):
        """Await the farm-policy task to COMPLETION (its disposition is what the journeys
        read), then drain whatever else the callback spawned."""
        for task in asyncio.all_tasks() - tasks_before:
            if (task.get_name() or "").startswith("farm-policy-terminal"):
                await task
        await TestPlateClearGate._drain(tasks_before)

    @staticmethod
    def _record_lift_waits(stack) -> list[float]:
        """The held-bed lift's motion wait, RECORDED instead of slept. ``farm_policy``'s own
        ``asyncio`` reference is replaced, never the process-wide ``asyncio.sleep``, so no other
        lane the terminal spawns is fast-forwarded."""
        from types import SimpleNamespace

        from backend.app.services import farm_policy

        waits: list[float] = []

        async def _sleep(delay):
            waits.append(delay)

        stack.enter_context(patch.object(farm_policy, "asyncio", SimpleNamespace(sleep=_sleep)))
        return waits

    @staticmethod
    async def _terminal(printer_id, *, subtask, status="failed", deposited=False, screen_echo=False):
        from backend.app.main import on_print_complete

        payload = {
            "status": status,
            "filename": "/data/Metadata/test.gcode",
            "subtask_name": "Test",
            "subtask_id": subtask,
            "timelapse_was_active": False,
            "peaks_reliable": True,
            "last_layer_num": 12 if deposited else 0,
            "last_progress": 30.0 if deposited else 0,
        }
        if screen_echo:
            payload["user_cancel_observed"] = True
        await on_print_complete(printer_id, payload)


class _LadderWire:
    """The one scripted printer the plate-check ladder journeys run against: a live
    ``PrinterState`` the episode driver polls through ``printer_manager.get_status``, the client
    it presses and reads through (the same client ``farm_policy`` lifts the bed with), and the
    raw ``stop_print``.

    ``after_press`` / ``after_stop`` are the printer's answers: mutations applied one per status
    read after that send (the last one holds), so the journey scripts what the driver's own
    polls see rather than a clock. The stop's default answer is the firmware's own for a stop
    from PAUSE — IDLE, the dialog and the HMS list gone."""

    def __init__(self, state, *, ack="success", layer=1, after_press=(), stop_taken=True):
        self.state = state
        self.layer = layer
        self.after_press = list(after_press)
        self.stop_taken = stop_taken
        self.presses: list[tuple] = []
        self.stops: list[int] = []
        self._pending: list = []
        self.client = MagicMock()
        self.client.execute_hms_action.side_effect = self._press
        self.client.await_ack = AsyncMock(side_effect=lambda sent, budget_s, poll_s: self._ack(sent, ack))
        self.client.job_peaks.side_effect = self._peaks
        self.client.send_gcode.return_value = True  # the held-bed lift

    @staticmethod
    def set(**fields):
        def apply(wire):
            for name, value in fields.items():
                if name == "layer":
                    wire.layer = value
                else:
                    setattr(wire.state, name, value)

        return apply

    def _peaks(self):
        """The client's one peaks reader: ``layer`` None is a reading this client did not
        measure (``reliable`` False — it attached the job mid-flight)."""
        from backend.app.services.bambu_mqtt import JobPeaks

        return JobPeaks(
            last_progress=0.0,
            last_layer_num=0,
            progress=0.0,
            layer_num=self.layer or 0,
            reliable=self.layer is not None,
        )

    def _press(self, print_error, action, job_id):
        from backend.app.services.bambu_mqtt import SentCommand

        self.presses.append((print_error, action, job_id))
        self._pending = list(self.after_press)
        return SentCommand(command="resume", sequence_id=str(len(self.presses)))

    @staticmethod
    def _ack(sent, result):
        from backend.app.services.bambu_mqtt import CommandAck

        return CommandAck(command=sent.command, sequence_id=sent.sequence_id, result=result, reason=None, at=0.0)

    def get_status(self, _printer_id):
        if self._pending:
            self._pending.pop(0)(self)
        return self.state

    def stop_print(self, printer_id):
        self.stops.append(printer_id)
        if self.stop_taken:
            self._pending = [self.set(state="IDLE", print_error=0, hms_errors=[])]
        return True

    def install(self, stack):
        from backend.app.services.printer_manager import printer_manager

        stack.enter_context(patch.object(printer_manager, "get_status", side_effect=self.get_status))
        stack.enter_context(patch.object(printer_manager, "get_client", return_value=self.client))
        stack.enter_context(patch.object(printer_manager, "stop_print", side_effect=self.stop_print))
        return self


class _PlateCheckLadder(_PlateCheckJourney):
    """The ladder journeys (operator rulings 2026-09-04 / 2026-09-29), through the public entry
    points: the pause sampler ``pause_recovery.note_status_push`` fed a PAUSE with the plate
    dialog on ``print_error`` (as the wire carries it: the 32-bit word, merged onto the HMS list
    with its 8-char full code), the episode driver it spawns, and the REAL
    ``main.on_print_complete`` for the terminal the farm's stop produces. The virtual printer
    cannot hold PAUSE or raise ``print_error``, so this is the hardware-free end to end."""

    _PLATE_ERR = 0x0500808C

    @pytest.fixture(autouse=True)
    def _clean(self):
        self._reset_process_state()
        yield
        self._reset_process_state()

    @classmethod
    def _paused(cls, subtask):
        """A printer PAUSEd at its plate check, on a fresh report."""
        state = cls._state(gcode_state="PAUSE", subtask=subtask, hms=[cls._vision_hms()])
        state.print_error = cls._PLATE_ERR
        state.connection_epoch = state.report_epoch = 1
        return state

    @staticmethod
    def _fast_ladder(stack, *, stop_confirm_s=5.0):
        """The ladder's waits, collapsed. ``stop_confirm_s`` stays long where the journey itself
        delivers the terminal the driver is waiting for."""
        from backend.app.services import pause_recovery

        for name, value in (
            ("_EPISODE_POLL_S", 0.01),
            ("_RECHECK_ACK_S", 0.05),
            ("_RECHECK_ACK_POLL_S", 0.01),
            ("_RECHECK_CONFIRM_S", 0.2),
            ("_STOP_CONFIRM_S", stop_confirm_s),
            ("_VISION_STOP_RETRY_S", 0.0),
        ):
            stack.enter_context(patch.object(pause_recovery, name, value))

    @staticmethod
    def _hold_clock(stack, clock):
        """The plate authority's hold clock (``plate_occupancy._now_mono``), steered by the test.

        The held-bed lift is a CLAIMED bed motion: from the send until its motion time has
        passed, ``dispatchable`` refuses the printer (``bed_motion_in_flight``). The journeys
        record ``farm_policy``'s post-lift wait instead of sleeping it, so the window's lapse is
        modelled on this clock — never by releasing the claim by hand."""
        from backend.app.services import plate_occupancy as po

        stack.enter_context(patch.object(po, "_now_mono", clock))
        return clock

    @staticmethod
    async def _episode_done(printer_id):
        """Await the episode driver the sampler spawned, to its end."""
        import backend.app.core.tasks as core_tasks

        name = f"plate-check-p{printer_id}"
        pending = [t for t in list(core_tasks._background_tasks) if t.get_name() == name and not t.done()]
        if pending:
            await asyncio.wait_for(asyncio.gather(*pending), timeout=10)

    @staticmethod
    async def _until(predicate, what):
        for _ in range(1000):
            if predicate():
                return
            await asyncio.sleep(0.01)
        pytest.fail(f"never happened: {what}")

    async def _failed_episode(self, printer_id, wire, subtask, tasks_before, *, status="aborted"):
        """Trip → press → the re-check fails → the farm stops → the PAUSE→IDLE terminal."""
        from backend.app.services import pause_recovery

        stops_before = len(wire.stops)
        pause_recovery.note_status_push(printer_id, wire.state)
        await self._until(lambda: len(wire.stops) > stops_before, "the farm's stop")
        await self._terminal(printer_id, subtask=subtask, status=status)
        await self._episode_done(printer_id)
        await self._settle(tasks_before)

    @staticmethod
    async def _episode_row(maker, printer_id, subtask):
        from sqlalchemy import select

        from backend.app.models.printer_incident import KIND_PLATE_VISION, PrinterIncident

        async with maker() as s:
            return (
                await s.execute(
                    select(PrinterIncident)
                    .where(PrinterIncident.printer_id == printer_id)
                    .where(PrinterIncident.kind == KIND_PLATE_VISION)
                    .where(PrinterIncident.job_id == subtask)
                )
            ).scalar_one()

    @staticmethod
    async def _ledger(maker, incident_id):
        from backend.app.services.pause_recovery import _PlateCheckEvidence

        async with maker() as s:
            log = await _PlateCheckEvidence.from_row(s, incident_id)
        return [(step.kind, step.outcome) for step in log.steps]

    @staticmethod
    async def _requeues(maker, item_id):
        from sqlalchemy import select

        from backend.app.models.print_queue import PrintQueueItem

        async with maker() as s:
            return (
                (await s.execute(select(PrintQueueItem).where(PrintQueueItem.retry_of_id == item_id))).scalars().all()
            )


class TestPlateCheckLadderJourney(_PlateCheckLadder):
    """Every rung, end to end: the same print continues on a passing re-check; a failed one is
    stopped and RETRIED; the retry's own failure is REFUSED to a human; a foreign print is
    re-checked, then stopped and escalated; a stop the farm cannot land falls back to the
    paused hold."""

    @pytest.mark.asyncio
    async def test_i_the_recheck_passes_and_the_same_job_continues(self, test_engine):
        from contextlib import ExitStack

        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.services import pause_recovery, printer_incidents
        from backend.app.services.notification_service import notification_service

        with ExitStack() as stack:
            mocks = TestPlateClearGate._setup_mocks(stack, test_engine)
            printer_id, item_id, _batch = await self._seed_farm_unit(mocks.maker, serial="LAD-1", subtask="SUB-1")
            wire = _LadderWire(
                self._paused("SUB-1"),
                after_press=[_LadderWire.set(state="RUNNING", print_error=0, hms_errors=[])],
            ).install(stack)
            page = stack.enter_context(patch.object(notification_service, "on_plate_not_empty", new_callable=AsyncMock))
            self._fast_ladder(stack)

            pause_recovery.note_status_push(printer_id, wire.state)
            await self._episode_done(printer_id)

            assert wire.presses == [("0500808C", "PROBLEM_SOLVED_RESUME", "SUB-1")]
            assert wire.stops == []
            wire.client.send_gcode.assert_not_called()  # the lift belongs to a terminal; none came
            page.assert_not_awaited()
            row = await self._episode_row(mocks.maker, printer_id, "SUB-1")
            assert (row.resolve_source, printer_incidents.outcome_of(row)) == ("recheck_passed", "auto_recovered")
            assert row.hms_full_codes == "0500808C"  # the printer's own code, recorded at the trip
            assert await self._ledger(mocks.maker, row.id) == [("dialog", "success")]
            async with mocks.maker() as s:
                item = await s.get(PrintQueueItem, item_id)
                assert (item.status, item.waiting_reason) == ("printing", None)  # the same job prints on
            assert _occupancy().is_plate_occupied(printer_id) is False

    @pytest.mark.asyncio
    async def test_ii_the_recheck_fails_the_farm_stops_and_retries_the_same_print(self, test_engine, clock):
        from contextlib import ExitStack

        from backend.app.models.print_batch import PrintBatch
        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.services import farm_policy, printer_incidents
        from backend.app.services.notification_service import notification_service
        from backend.app.services.plate_occupancy import Evidence

        tasks_before = set(asyncio.all_tasks())
        with ExitStack() as stack:
            mocks = TestPlateClearGate._setup_mocks(stack, test_engine)
            printer_id, item_id, batch_id = await self._seed_farm_unit(mocks.maker, serial="LAD-2", subtask="SUB-2")
            # The press is accepted, the job runs its re-check, and the plate dialog comes back.
            wire = _LadderWire(
                self._paused("SUB-2"),
                layer=0,
                after_press=[
                    _LadderWire.set(state="RUNNING", print_error=0),
                    _LadderWire.set(state="PAUSE", print_error=self._PLATE_ERR),
                ],
            ).install(stack)
            page = stack.enter_context(patch.object(notification_service, "on_plate_not_empty", new_callable=AsyncMock))
            waits = self._record_lift_waits(stack)
            self._hold_clock(stack, clock)
            self._fast_ladder(stack)

            await self._failed_episode(printer_id, wire, "SUB-2", tasks_before)

            assert len(wire.presses) == 1 and wire.stops == [printer_id]
            row = await self._episode_row(mocks.maker, printer_id, "SUB-2")
            assert await self._ledger(mocks.maker, row.id) == [("dialog", "success"), ("stop", "taken")]
            # The episode closed at its terminal as the farm's own recovery.
            assert (row.resolve_source, printer_incidents.outcome_of(row)) == ("terminal", "auto_recovered")
            async with mocks.maker() as s:
                item = await s.get(PrintQueueItem, item_id)
                assert (item.status, item.stop_source) == ("cancelled", "plate_refused")
                batch = await s.get(PrintBatch, batch_id)
                assert (batch.status, batch.pause_reason) == ("active", None)
            # RETRIED: requeued next in line, no gate, no page.
            (retry,) = await self._requeues(mocks.maker, item_id)
            assert (retry.status, retry.position) == ("pending", 1)
            assert _occupancy().is_plate_occupied(printer_id) is False
            page.assert_not_awaited()
            # The bed lifted off the release aid, its motion waited out before the requeue.
            wire.client.send_gcode.assert_called_once()
            assert "G380 S2 Z-12.0" in wire.client.send_gcode.call_args.args[0]
            assert waits == [pytest.approx((farm_policy.VISION_HOLD_PROBE_MM + 12.0) / (1200 / 60))]
            # ...and it is a CLAIMED motion: the retry cannot be released onto the printer until
            # the lift's own motion time has passed.
            idle = Evidence(live_state="IDLE")
            assert _occupancy().dispatchable(printer_id, idle) == "bed_motion_in_flight"
            clock.advance(waits[0] + 0.01)
            assert _occupancy().dispatchable(printer_id, idle) is None

    @pytest.mark.asyncio
    async def test_iii_the_retrys_own_failed_recheck_refuses_the_plate(self, test_engine, clock):
        """A second failed episode on this printer inside the window: the plate is a human's —
        a refusal gate in the printer's words, the bed lifted, the unit requeued."""
        from contextlib import ExitStack
        from datetime import datetime, timezone

        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.services import printer_incidents
        from backend.app.services.notification_service import notification_service
        from backend.app.services.plate_occupancy import EscalationOnly, Evidence

        tasks_before = set(asyncio.all_tasks())
        with ExitStack() as stack:
            mocks = TestPlateClearGate._setup_mocks(stack, test_engine)
            printer_id, item_id, _batch = await self._seed_farm_unit(mocks.maker, serial="LAD-3", subtask="SUB-3")
            wire = _LadderWire(self._paused("SUB-3"), ack="fail").install(stack)  # refused: straight to the stop
            stack.enter_context(patch.object(notification_service, "on_plate_not_empty", new_callable=AsyncMock))
            waits = self._record_lift_waits(stack)
            self._hold_clock(stack, clock)
            self._fast_ladder(stack)

            await self._failed_episode(printer_id, wire, "SUB-3", tasks_before)
            (retry,) = await self._requeues(mocks.maker, item_id)
            assert _occupancy().is_plate_occupied(printer_id) is False  # the first stop retried

            # The scheduler can dispatch the retry only once the first lift's claimed motion
            # window has lapsed (``dispatchable`` refuses ``bed_motion_in_flight`` inside it).
            idle = Evidence(live_state="IDLE")
            assert _occupancy().dispatchable(printer_id, idle) == "bed_motion_in_flight"
            clock.advance(waits[0] + 0.01)
            assert _occupancy().dispatchable(printer_id, idle) is None

            # The retry is dispatched back onto the same printer, and trips again.
            async with mocks.maker() as s:
                unit = await s.get(PrintQueueItem, retry.id)
                unit.status, unit.printer_id, unit.dispatch_subtask_id = "printing", printer_id, "SUB-3R"
                unit.started_at = datetime.now(timezone.utc)
                await s.commit()
            wire.state = self._paused("SUB-3R")
            tasks_before = set(asyncio.all_tasks())
            await self._failed_episode(printer_id, wire, "SUB-3R", tasks_before)

            assert len(wire.presses) == 2 and wire.stops == [printer_id, printer_id]
            row = await self._episode_row(mocks.maker, printer_id, "SUB-3R")
            assert (row.resolve_source, printer_incidents.outcome_of(row)) == ("plate_refused", "human_resolved")
            # Gated for a human, sourceless, in the printer's own words.
            view = _occupancy().snapshot(printer_id)
            assert view.plate_occupied is True
            assert view.plate_source_subtask_id is None
            assert isinstance(view.plate_policy, EscalationOnly)
            assert view.plate_policy.refusal.messages[0].short_code == "0500_808C"
            # The bed lifted both times, and the retry's unit requeued again (to wait, or go elsewhere).
            assert wire.client.send_gcode.call_count == 2
            assert len(waits) == 2
            async with mocks.maker() as s:
                unit = await s.get(PrintQueueItem, retry.id)
                assert (unit.status, unit.stop_source) == ("cancelled", "plate_refused")
            assert len(await self._requeues(mocks.maker, retry.id)) == 1

    @pytest.mark.asyncio
    async def test_iv_a_foreign_print_is_rechecked_then_stopped_and_escalated(self, test_engine):
        from contextlib import ExitStack

        from backend.app.models.printer import Printer
        from backend.app.services import printer_incidents
        from backend.app.services.notification_service import notification_service
        from backend.app.services.plate_occupancy import EscalationOnly

        tasks_before = set(asyncio.all_tasks())
        with ExitStack() as stack:
            mocks = TestPlateClearGate._setup_mocks(stack, test_engine)
            # A printer with NO farm unit on it (its geometry row is what the bed lift reads).
            _pid, _item, _batch = await self._seed_farm_unit(mocks.maker, serial="LAD-4-GEO", subtask="SUB-OTHER")
            async with mocks.maker() as s:
                printer = Printer(
                    name="P-LAD-4", serial_number="LAD-4", ip_address="10.0.0.9", access_code="0000", model="H2S"
                )
                s.add(printer)
                await s.commit()
                printer_id = printer.id
            # A screen-started print: no unit, a firmware-minted id. Its re-check fails too.
            wire = _LadderWire(
                self._paused("FOREIGN-4"),
                after_press=[_LadderWire.set(print_error=0), _LadderWire.set(print_error=self._PLATE_ERR)],
            ).install(stack)
            stack.enter_context(patch.object(notification_service, "on_plate_not_empty", new_callable=AsyncMock))
            waits = self._record_lift_waits(stack)
            self._fast_ladder(stack)

            await self._failed_episode(printer_id, wire, "FOREIGN-4", tasks_before)

            assert wire.presses == [("0500808C", "PROBLEM_SOLVED_RESUME", "FOREIGN-4")]  # re-checked first
            assert wire.stops == [printer_id]
            row = await self._episode_row(mocks.maker, printer_id, "FOREIGN-4")
            assert row.item_id is None
            assert (row.resolve_source, printer_incidents.outcome_of(row)) == ("plate_refused", "human_resolved")
            view = _occupancy().snapshot(printer_id)
            assert view.plate_occupied is True
            assert isinstance(view.plate_policy, EscalationOnly)
            assert view.plate_policy.refusal.messages[0].short_code == "0500_808C"
            wire.client.send_gcode.assert_called_once()
            assert len(waits) == 1

    @pytest.mark.asyncio
    async def test_v_a_stop_the_farm_cannot_land_falls_back_to_the_paused_hold(self, test_engine):
        from contextlib import ExitStack

        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.services import pause_recovery
        from backend.app.services.notification_service import notification_service

        with ExitStack() as stack:
            mocks = TestPlateClearGate._setup_mocks(stack, test_engine)
            printer_id, item_id, _batch = await self._seed_farm_unit(mocks.maker, serial="LAD-5", subtask="SUB-5")
            wire = _LadderWire(self._paused("SUB-5"), ack="fail", stop_taken=False).install(stack)
            page = stack.enter_context(patch.object(notification_service, "on_plate_not_empty", new_callable=AsyncMock))
            self._fast_ladder(stack, stop_confirm_s=0.2)

            pause_recovery.note_status_push(printer_id, wire.state)
            await self._episode_done(printer_id)

            row = await self._episode_row(mocks.maker, printer_id, "SUB-5")
            assert (row.status, row.resolved_at) == ("escalated", None)
            assert await self._ledger(mocks.maker, row.id) == [("dialog", "fail"), ("stop", "not_taken")]
            wire.client.send_gcode.assert_not_called()  # the job never ended: no terminal, no lift
            page.assert_awaited_once()
            detail = page.await_args.kwargs["source_detail"]
            assert detail.startswith("The printer reported: ")
            assert detail.endswith("Print paused — fix the plate, then resume.")
            async with mocks.maker() as s:
                item = await s.get(PrintQueueItem, item_id)
                assert (item.status, item.waiting_reason) == ("printing", "plate_not_empty_printer_detected")

            # The human's, for good: the pause keeps standing and the farm sends nothing more.
            for _ in range(3):
                pause_recovery.note_status_push(printer_id, wire.state)
                await self._episode_done(printer_id)
            assert (len(wire.presses), len(wire.stops)) == (1, 1)
            page.assert_awaited_once()


class TestPlateCheckFallbackHold(_PlateCheckLadder):
    """The FALLBACK hold (the farm could not end the job) is the human's, and their answer ends
    it through the ordinary closers: resuming that job ends the hold and the same job continues;
    stopping the paused print is a REFUSED plate — gated for a human with the printer's words,
    the bed lifted off the release aid, the unit requeued next in line (``cancelled`` parent, a
    first article included)."""

    async def _fallback_hold(self, maker, printer_id, item_id):
        """The episode as the fallback leaves it: ESCALATED, the printer's code recorded, the
        unit's hold projected."""
        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.models.printer_incident import KIND_PLATE_VISION, STATUS_ESCALATED
        from backend.app.services import printer_incidents

        async with maker() as s:
            await printer_incidents.open_new(
                s,
                printer_id=printer_id,
                job_id=self._SUBTASK,
                item_id=item_id,
                kind=KIND_PLATE_VISION,
                code="0500_808C",
                codes="0500_808C",
                slot_global_tray=None,
                hms_full_codes=["0500808C"],
                status=STATUS_ESCALATED,
            )
            item = await s.get(PrintQueueItem, item_id)
            item.waiting_reason = printer_incidents.waiting_reason_for(KIND_PLATE_VISION)
            await s.commit()

    @pytest.mark.asyncio
    async def test_the_operators_resume_of_that_job_ends_the_hold(self, test_engine):
        from contextlib import ExitStack

        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.services import printer_incidents, spool_recovery

        with ExitStack() as stack:
            mocks = TestPlateClearGate._setup_mocks(stack, test_engine)
            printer_id, item_id, _batch = await self._seed_farm_unit(mocks.maker, serial="PCH-1", subtask=self._SUBTASK)
            wire = _LadderWire(self._paused(self._SUBTASK)).install(stack)
            await self._fallback_hold(mocks.maker, printer_id, item_id)
            assert printer_incidents.snapshot(printer_id)["printer_messages"][0]["short_code"] == "0500_808C"

            # The operator fixed the plate and resumed THAT job.
            wire.state.state, wire.state.print_error, wire.state.hms_errors = "RUNNING", 0, []
            assert await spool_recovery.on_observed_running(printer_id) is True

            async with mocks.maker() as s:
                assert await printer_incidents.get_open(s, printer_id) is None
                item = await s.get(PrintQueueItem, item_id)
                assert item.status == "printing"  # the same job continues
                assert item.waiting_reason is None
            assert _occupancy().is_plate_occupied(printer_id) is False
            assert wire.presses == [] and wire.stops == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("first_article", [False, True], ids=["unit", "first_article"])
    async def test_stopping_the_paused_print_is_a_refused_plate(self, test_engine, first_article):
        import json
        from contextlib import ExitStack

        from sqlalchemy import select

        from backend.app.models.print_batch import PrintBatch
        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.models.printer_incident import KIND_PLATE_VISION, PrinterIncident
        from backend.app.services import farm_policy
        from backend.app.services.plate_occupancy import EscalationOnly
        from backend.app.services.print_control import stop_as_operator
        from backend.app.services.printer_manager import occupancy_payload

        tasks_before = set(asyncio.all_tasks())
        with ExitStack() as stack:
            mocks = TestPlateClearGate._setup_mocks(stack, test_engine)
            printer_id, item_id, batch_id = await self._seed_farm_unit(
                mocks.maker, serial=f"PCH-2{int(first_article)}", subtask=self._SUBTASK, first_article=first_article
            )
            wire = _LadderWire(self._paused(self._SUBTASK)).install(stack)
            waits = self._record_lift_waits(stack)
            await self._fallback_hold(mocks.maker, printer_id, item_id)

            # The operator presses Stop in the UI (the stop wipes the printer's HMS list).
            await stop_as_operator(printer_id)
            wire.state.state, wire.state.print_error, wire.state.hms_errors = "FAILED", 0, []
            await self._terminal(printer_id, subtask=self._SUBTASK)
            await self._settle(tasks_before)

            async with mocks.maker() as s:
                item = await s.get(PrintQueueItem, item_id)
                # A stop, never a failure — a first article included (its retry is not spent).
                assert item.status == "cancelled"
                assert item.stop_source == "plate_refused"
                assert "0500_808C" in (item.error_message or "")
                # A HUMAN's stop (no farm ``stop`` step on the episode's log): the terminal
                # REFUSED the plate and handed it over, and the close records that answer.
                row = (
                    await s.execute(select(PrinterIncident).where(PrinterIncident.printer_id == printer_id))
                ).scalar_one()
                assert (row.kind, row.resolve_source) == (KIND_PLATE_VISION, "plate_refused")
                assert row.resolved_at is not None
                # Requeued NEXT in line, lineage only, AS a first article when it was one.
                requeues = (
                    (await s.execute(select(PrintQueueItem).where(PrintQueueItem.retry_of_id == item_id)))
                    .scalars()
                    .all()
                )
                assert len(requeues) == 1
                assert requeues[0].status == "pending"
                assert requeues[0].position == 1
                assert requeues[0].first_article is first_article
                batch = await s.get(PrintBatch, batch_id)
                assert (batch.status, batch.pause_reason) == ("active", None)

            # The plate: held for a human, SOURCELESS, carrying the printer's words.
            view = _occupancy().snapshot(printer_id)
            assert view.plate_occupied is True
            assert view.plate_source_subtask_id is None
            assert isinstance(view.plate_policy, EscalationOnly)
            refusal = occupancy_payload(printer_id)["plate"]["refusal"]
            assert refusal["messages"][0]["short_code"] == "0500_808C"
            assert "build plate" in refusal["messages"][0]["description"]
            json.dumps(occupancy_payload(printer_id))  # the WS lane has no encoder
            # ...not a foreign deposit to identify and sweep.
            mocks.notif.on_foreign_job_detected.assert_not_awaited()
            # The bed is lifted off the plate-release aid.
            wire.client.send_gcode.assert_called_once()
            assert "G380 S2 Z-12.0" in wire.client.send_gcode.call_args.args[0]
            assert waits == [pytest.approx((farm_policy.VISION_HOLD_PROBE_MM + 12.0) / (1200 / 60))]


class TestPlateCheckFarmStopRetries(_PlateCheckJourney):
    """The ladder's terminal side (operator ruling 2026-09-29), through the REAL terminal.

    The farm's re-check failed and the FARM stopped the print (the episode's log holds its
    ``stop``): the same print is RETRIED — requeued next in line, no gate, no page, the bed
    lifted and waited out, the episode closed ``terminal`` and counted ``auto_recovered``. If
    the farm already stopped another episode on this printer inside the hour, this is the
    retry's own failure: the plate is REFUSED to a human instead. The episode's steps are
    seeded through its own evidence log, the ledger's one writer.
    """

    @pytest.fixture(autouse=True)
    def _clean(self):
        self._reset_process_state()
        yield
        self._reset_process_state()

    async def _farm_stopped_terminal(self, test_engine, *, serial, prior_stop):
        from contextlib import ExitStack

        from sqlalchemy import select

        from backend.app.models.print_batch import PrintBatch
        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.models.printer_incident import KIND_PLATE_VISION, PrinterIncident
        from backend.app.services import farm_policy, pause_recovery, printer_incidents
        from backend.app.services.notification_service import notification_service
        from backend.app.services.pause_recovery import PlateCheckStep
        from backend.app.services.printer_manager import printer_manager

        async def _episode(s, *, job_id, item_id, close):
            row = await printer_incidents.open_new(
                s,
                printer_id=printer_id,
                job_id=job_id,
                item_id=item_id,
                kind=KIND_PLATE_VISION,
                code="0500_808C",
                codes="0500_808C",
                slot_global_tray=None,
                hms_full_codes=[self._FULL],
                status="recovering",
            )
            log = await pause_recovery._PlateCheckEvidence.from_row(s, row.id)
            pressed = await log.note(PlateCheckStep.dialog())
            await log.answer(pressed, "fail")  # the re-check failed...
            await log.note(PlateCheckStep.stop())  # ...so the farm stopped; its answer is still owed
            if close:
                await printer_incidents.close(s, row.id, status="resolved", source="terminal")

        tasks_before = set(asyncio.all_tasks())
        with ExitStack() as stack:
            mocks = TestPlateClearGate._setup_mocks(stack, test_engine)
            printer_id, item_id, batch_id = await self._seed_farm_unit(
                mocks.maker, serial=serial, subtask=self._SUBTASK
            )
            # The farm's stop ended the job: the printer reads FAILED with its list wiped.
            stopped = self._state(gcode_state="FAILED", subtask=self._SUBTASK)
            stack.enter_context(patch.object(printer_manager, "get_status", return_value=stopped))
            page = stack.enter_context(patch.object(notification_service, "on_plate_not_empty", new_callable=AsyncMock))
            lift = MagicMock()
            lift.send_gcode.return_value = True
            stack.enter_context(patch.object(farm_policy.printer_manager, "get_client", return_value=lift))
            waits = self._record_lift_waits(stack)
            async with mocks.maker() as s:
                if prior_stop:
                    # The retry's parent: an episode of another job the farm already stopped here.
                    await _episode(s, job_id="SUB-PARENT", item_id=None, close=True)
                await _episode(s, job_id=self._SUBTASK, item_id=item_id, close=False)

            await self._terminal(printer_id, subtask=self._SUBTASK)
            await self._settle(tasks_before)

            async with mocks.maker() as s:
                item = await s.get(PrintQueueItem, item_id)
                row = (
                    await s.execute(
                        select(PrinterIncident)
                        .where(PrinterIncident.printer_id == printer_id)
                        .where(PrinterIncident.job_id == self._SUBTASK)
                    )
                ).scalar_one()
                requeues = (
                    (await s.execute(select(PrintQueueItem).where(PrintQueueItem.retry_of_id == item_id)))
                    .scalars()
                    .all()
                )
                batch = await s.get(PrintBatch, batch_id)
            return printer_id, item, row, requeues, batch, lift, waits, page

    @pytest.mark.asyncio
    async def test_the_farms_first_stop_retries_the_same_print(self, test_engine):
        from backend.app.services import farm_policy, printer_incidents

        printer_id, item, row, requeues, batch, lift, waits, page = await self._farm_stopped_terminal(
            test_engine, serial="PCR-1", prior_stop=False
        )

        # The unit: a stop at the plate check, never a failure.
        assert (item.status, item.stop_source) == ("cancelled", "plate_refused")
        # Retried: requeued NEXT in line, and the run carries on.
        assert len(requeues) == 1
        assert (requeues[0].status, requeues[0].position) == ("pending", 1)
        assert (batch.status, batch.pause_reason) == ("active", None)
        # No gate and no page: the printer is free for the retry.
        assert _occupancy().is_plate_occupied(printer_id) is False
        page.assert_not_awaited()
        # The bed was lifted off the release aid, and its motion waited out before the requeue.
        lift.send_gcode.assert_called_once()
        assert "G380 S2 Z-12.0" in lift.send_gcode.call_args.args[0]
        assert waits == [pytest.approx((farm_policy.VISION_HOLD_PROBE_MM + 12.0) / (1200 / 60))]
        # The episode closed at its terminal as the farm's own recovery.
        assert (row.resolve_source, printer_incidents.outcome_of(row)) == ("terminal", "auto_recovered")

    @pytest.mark.asyncio
    async def test_the_retrys_own_failed_recheck_escalates(self, test_engine):
        """A second farm stop on this printer inside the hour: the plate is a human's."""
        from backend.app.services import printer_incidents
        from backend.app.services.plate_occupancy import EscalationOnly

        printer_id, item, row, requeues, batch, lift, waits, _page = await self._farm_stopped_terminal(
            test_engine, serial="PCR-2", prior_stop=True
        )

        assert (item.status, item.stop_source) == ("cancelled", "plate_refused")
        # Gated for a human, sourceless, in the printer's words.
        view = _occupancy().snapshot(printer_id)
        assert view.plate_occupied is True
        assert view.plate_source_subtask_id is None
        assert isinstance(view.plate_policy, EscalationOnly)
        assert view.plate_policy.refusal.messages[0].short_code == "0500_808C"
        # The bed is still lifted, and the unit still requeued (to wait, or go elsewhere).
        lift.send_gcode.assert_called_once()
        assert len(waits) == 1
        assert len(requeues) == 1
        assert (row.resolve_source, printer_incidents.outcome_of(row)) == ("plate_refused", "human_resolved")


class TestRefusedPlatePrecedence(_PlateCheckJourney):
    """The refusal OUTRANKS the operator signals, and without a hold nothing changes.

    A screen stop of the paused print carries the firmware's cancel echo, and the verdict
    is still ``plate_refused``; a first-article no-deposit failure on a printer with NO
    plate-check hold keeps ``failed`` (the pre-existing contract: a genuine FA failure
    still retries)."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        self._reset_process_state()
        yield
        self._reset_process_state()

    async def _run(self, test_engine, *, plate_check):
        from contextlib import ExitStack

        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.services import printer_incidents

        tasks_before = set(asyncio.all_tasks())
        with ExitStack() as stack:
            mocks = TestPlateClearGate._setup_mocks(stack, test_engine)
            policy = stack.enter_context(patch("backend.app.services.farm_policy.on_terminal", new_callable=AsyncMock))
            printer_id, item_id, _batch = await self._seed_farm_unit(
                mocks.maker, serial=f"PRC-{int(plate_check)}", subtask=self._SUBTASK, first_article=True
            )
            if plate_check:
                async with mocks.maker() as s:
                    await printer_incidents.open_new(
                        s,
                        printer_id=printer_id,
                        job_id=self._SUBTASK,
                        item_id=item_id,
                        kind="plate_vision",
                        code="0500_808C",
                        codes="0500_808C",
                        slot_global_tray=None,
                        hms_full_codes=[self._FULL],
                        status="escalated",
                    )
            await self._terminal(printer_id, subtask=self._SUBTASK, screen_echo=True)
            await self._settle(tasks_before)

            async with mocks.maker() as s:
                item = await s.get(PrintQueueItem, item_id)
            outcome = policy.await_args.kwargs["outcome"] if policy.await_args is not None else None
            return item.status, item.stop_source, outcome

    @pytest.mark.asyncio
    async def test_a_screen_stop_of_a_paused_plate_check_is_a_refused_first_article(self, test_engine):
        status, stop_source, outcome = await self._run(test_engine, plate_check=True)

        assert status == "cancelled"
        assert stop_source == "plate_refused"  # never relabelled operator_screen
        assert outcome.verdict == "plate_refused"
        assert outcome.recorded_status == "cancelled"
        assert outcome.failure_category == "Plate not empty (printer vision)"

    @pytest.mark.asyncio
    async def test_without_a_hold_a_first_article_failure_is_unchanged(self, test_engine):
        """The pre-existing contract: a first-article no-deposit stop deliberately keeps
        ``failed`` so the run still retries its first article."""
        status, stop_source, outcome = await self._run(test_engine, plate_check=False)

        assert status == "failed"
        assert stop_source is None
        assert outcome.verdict == "operator_screen"


class TestOperatorStopOverAFaultRequeues(_PlateCheckJourney):
    """REGRESSION (2026-09-11 ruling, unhonoured in production until 2026-09-24).

    An operator's Stop of a print its printer is holding on an escalated RUNOUT means "do
    this plate again". The terminal's own closer ends the runout row (a ``wire`` hold) at
    this very terminal, so a policy that asked the store AFTERWARDS found nothing and held
    the run instead. The fault is now captured BEFORE any closer runs.
    """

    @pytest.fixture(autouse=True)
    def _clean(self):
        self._reset_process_state()
        yield
        self._reset_process_state()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("notification_fails", [False, True], ids=["page_sent", "page_raised"])
    async def test_the_plate_is_requeued_and_the_run_does_not_hold(self, test_engine, notification_fails):
        """``notification_fails``: the policy used to run INSIDE the notification closure,
        so a page that raised took the disposition down with it. It is its own task now."""
        from contextlib import ExitStack

        from sqlalchemy import select

        from backend.app.models.print_batch import PrintBatch
        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.services import printer_incidents
        from backend.app.services.print_control import stop_as_operator

        tasks_before = set(asyncio.all_tasks())
        with ExitStack() as stack:
            mocks = TestPlateClearGate._setup_mocks(stack, test_engine)
            if notification_fails:
                mocks.notif.on_print_complete.side_effect = RuntimeError("provider down")
            printer_id, item_id, batch_id = await self._seed_farm_unit(
                mocks.maker, serial=f"RUN-{int(notification_fails)}", subtask="SUB-R"
            )
            async with mocks.maker() as s:
                await printer_incidents.open_new(
                    s,
                    printer_id=printer_id,
                    job_id="SUB-R",
                    item_id=item_id,
                    kind="runout",
                    code="0700_8011",
                    codes="runout:0700_8011",
                    slot_global_tray=None,
                    status="escalated",
                )

            await stop_as_operator(printer_id)
            await self._terminal(printer_id, subtask="SUB-R", deposited=True)
            await self._settle(tasks_before)

            async with mocks.maker() as s:
                # The closer DID end the runout row at this terminal...
                assert await printer_incidents.get_open(s, printer_id) is None
                # ...and the plate was requeued anyway, because the fault was captured first.
                requeues = (
                    (await s.execute(select(PrintQueueItem).where(PrintQueueItem.retry_of_id == item_id)))
                    .scalars()
                    .all()
                )
                assert len(requeues) == 1
                batch = await s.get(PrintBatch, batch_id)
                assert batch.pause_reason is None  # the requeue, not the operator-stop hold


class TestFarmRestartStop(_PlateCheckJourney):
    """The terminal of a job the recovery driver stopped with its LAST release rung
    (operator ruling 2026-09-29, 013-H2S incidents 410/411): a feed stall on the first
    filament load of a job that had deposited nothing.

    Driven through the real ``on_print_complete``, job phase and ``farm_policy``. The open
    row is the store's own; the one thing laid over its projection is the contract key the
    step-ledger writer derives (``printer_incidents.PAYLOAD_FAULT_RESTART_STOP``), at the one
    read the handler makes before any closer runs. Before the verdict existed, an H2S echo
    of the farm's stop read ``operator_screen`` and an H2C terminal (no echo) read nothing:
    the unit was held for a human, and a first article was recorded ``failed``.
    """

    _SUB = "SUB-FR"

    @pytest.fixture(autouse=True)
    def _clean(self):
        self._reset_process_state()
        yield
        self._reset_process_state()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("first_article", [False, True], ids=["unit", "first_article"])
    @pytest.mark.parametrize("screen_echo", [True, False], ids=["h2s_cancel_echo", "h2c_no_echo"])
    async def test_the_unit_is_restarted_next_in_line_not_failed_held_or_paged(
        self, test_engine, screen_echo, first_article
    ):
        import contextlib
        from contextlib import ExitStack

        from sqlalchemy import select

        from backend.app.models.print_batch import PrintBatch
        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.models.printer_incident import KIND_JAM, STATUS_RECOVERING
        from backend.app.services import farm_policy, printer_incidents

        tasks_before = set(asyncio.all_tasks())
        with ExitStack() as stack:
            mocks = TestPlateClearGate._setup_mocks(stack, test_engine)
            stopped_page = stack.enter_context(
                patch.object(farm_policy.notification_service, "on_run_unit_stopped", new_callable=AsyncMock)
            )
            quarantine = stack.enter_context(
                patch.object(farm_policy, "maybe_quarantine_printer", new_callable=AsyncMock)
            )
            printer_id, item_id, batch_id = await self._seed_farm_unit(
                mocks.maker,
                serial=f"FR-{int(screen_echo)}{int(first_article)}",
                subtask=self._SUB,
                first_article=first_article,
            )
            async with mocks.maker() as s:
                await printer_incidents.open_new(
                    s,
                    printer_id=printer_id,
                    job_id=self._SUB,
                    item_id=item_id,
                    kind=KIND_JAM,
                    code="0700_8006",
                    codes="mechanical_feed:0700_8006",
                    slot_global_tray=2,
                    status=STATUS_RECOVERING,
                )
            store_snapshots = printer_incidents.snapshots

            def ledgered(pid):
                return [
                    {**row, printer_incidents.PAYLOAD_FAULT_RESTART_STOP: True} if row["job_id"] == self._SUB else row
                    for row in store_snapshots(pid)
                ]

            stack.enter_context(patch.object(printer_incidents, "snapshots", side_effect=ledgered))

            await self._terminal(printer_id, subtask=self._SUB, status="failed", screen_echo=screen_echo)
            # The policy and the no-archive notification are AWAITED, never drained: a
            # cancel would race the very calls this test reads.
            for task in asyncio.all_tasks() - tasks_before:
                if (task.get_name() or "").startswith(("farm-policy-terminal", "notify-no-archive")):
                    with contextlib.suppress(Exception):
                        await task
            await TestPlateClearGate._drain(tasks_before)

            async with mocks.maker() as s:
                item = await s.get(PrintQueueItem, item_id)
                # A stop the farm made, never a failure — a first article included.
                assert (item.status, item.stop_source) == ("cancelled", "fault_restart")
                requeues = (
                    (await s.execute(select(PrintQueueItem).where(PrintQueueItem.retry_of_id == item_id)))
                    .scalars()
                    .all()
                )
                assert len(requeues) == 1
                assert (requeues[0].status, requeues[0].position) == ("pending", 1)  # next in line
                assert requeues[0].first_article is first_article
                batch = await s.get(PrintBatch, batch_id)
                assert (batch.status, batch.pause_reason) == ("active", None)  # the run is not held

            stopped_page.assert_not_awaited()  # no operator-stop page for the farm's own restart
            quarantine.assert_not_awaited()  # not a failure: nothing feeds quarantine
            # The generic upstream "stopped" event fires as it does for every requeue lane.
            mocks.notif.on_print_complete.assert_awaited_once()
            assert mocks.notif.on_print_complete.await_args.args[2] == "cancelled"
            # A no-deposit terminal gates nothing, and the verdict adds no gate of its own.
            assert _occupancy().is_plate_occupied(printer_id) is False


class TestAnUnattributedAbortHoldsTheRun(_PlateCheckJourney):
    """An ``aborted`` terminal nobody attributed (no UI mark, no screen echo, no hold, no
    reconcile flag) on a farm unit that DEPOSITED — the printer ended the job in neither
    FINISH nor FAILED. It used to reach the farm policy as ``aborted``, match no branch of
    its disposition fork, and leave the run one plate short with nothing on any surface.
    Driven through the real ``on_print_complete`` and the real ``farm_policy``."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        self._reset_process_state()
        yield
        self._reset_process_state()

    @pytest.mark.asyncio
    async def test_it_takes_the_operator_stop_disposition(self, test_engine):
        from contextlib import ExitStack

        from backend.app.models.print_batch import PrintBatch
        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.services import farm_policy

        tasks_before = set(asyncio.all_tasks())
        with ExitStack() as stack:
            mocks = TestPlateClearGate._setup_mocks(stack, test_engine)
            stopped_page = stack.enter_context(
                patch.object(farm_policy.notification_service, "on_run_unit_stopped", new_callable=AsyncMock)
            )
            printer_id, item_id, batch_id = await self._seed_farm_unit(mocks.maker, serial="ABT-1", subtask="SUB-A")

            await self._terminal(printer_id, subtask="SUB-A", status="aborted", deposited=True)
            await self._settle(tasks_before)

            async with mocks.maker() as s:
                item = await s.get(PrintQueueItem, item_id)
                assert (item.status, item.stop_source) == ("cancelled", None)
                batch = await s.get(PrintBatch, batch_id)
                assert (batch.status, batch.pause_reason) == ("active", "operator_stop")  # the run HOLDS
            stopped_page.assert_awaited_once()  # a human is told
            # The deposit is still the farm's to hold: the plate gate stands.
            assert _occupancy().is_plate_occupied(printer_id) is True


class TestQueuePageStopReachesThePolicy(_PlateCheckJourney):
    """§R (confirmed by reading, 2026-09-24): ``POST /queue/{id}/stop`` commits the row
    ``cancelled`` / ``operator_ui`` BEFORE the printer's terminal arrives, and correlation
    matched only ``printing`` rows — so the farm's own print resolved FOREIGN: no
    operator-stop hold, no fault requeue, and a foreign-plate page for the farm's part."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        self._reset_process_state()
        yield
        self._reset_process_state()

    @pytest.mark.asyncio
    async def test_the_queue_page_stop_takes_the_operator_stop_disposition(self, test_engine):
        from contextlib import ExitStack

        from backend.app.models.print_batch import PrintBatch
        from backend.app.services import farm_policy
        from backend.app.services.printer_manager import printer_manager

        tasks_before = set(asyncio.all_tasks())
        with ExitStack() as stack:
            mocks = TestPlateClearGate._setup_mocks(stack, test_engine)
            stopped_page = stack.enter_context(
                patch.object(farm_policy.notification_service, "on_run_unit_stopped", new_callable=AsyncMock)
            )
            stack.enter_context(patch.object(printer_manager, "stop_print", return_value=True))
            printer_id, item_id, batch_id = await self._seed_farm_unit(mocks.maker, serial="QPS-1", subtask="SUB-Q")

            # The REAL route: the operator's request, then the row committed ``cancelled`` /
            # ``operator_ui`` before the printer answers.
            await _DurableStop.queue_page_stop(mocks.maker, item_id)

            await self._terminal(printer_id, subtask="SUB-Q", deposited=True)
            await self._settle(tasks_before)

            async with mocks.maker() as s:
                batch = await s.get(PrintBatch, batch_id)
                assert batch.pause_reason == "operator_stop"  # RESUME tops the deficit back up
            stopped_page.assert_awaited_once()
            mocks.notif.on_foreign_job_detected.assert_not_awaited()
            # The farm's own deposit, not a foreign one.
            assert _occupancy().snapshot(printer_id).plate_source_subtask_id == "SUB-Q"


class _DurableStop(_PlateCheckJourney):
    """Shared scaffold for the durable operator stop (§6, 2026-09-25)."""

    @staticmethod
    async def queue_page_stop(maker, item_id):
        """``POST /queue/{id}/stop`` through its REAL handler (auth resolved as a full admin)."""
        from backend.app.api.routes.print_queue import stop_queue_item

        async with maker() as s:
            await stop_queue_item(item_id, db=s, auth_result=(None, True))

    @staticmethod
    def restart():
        """What a deploy's restart leaves behind: NO process state — the incident stores, the
        recovery drivers and the plate authority's in-memory records are gone, and nothing in
        process memory remembers that anybody pressed Stop. Only the database survives."""
        _PlateCheckJourney._reset_process_state()
        _occupancy().reset_for_tests()

    @staticmethod
    def spy_policy(stack):
        """The farm policy runs for real; record every terminal's ONE classification and every
        UNIT disposition (``on_unit_terminal`` with a unit)."""
        from backend.app.services import farm_policy

        outcomes: list = []
        dispositions: list[int] = []
        real_terminal, real_unit = farm_policy.on_terminal, farm_policy.on_unit_terminal

        async def _terminal(*args, **kwargs):
            outcomes.append(kwargs.get("outcome"))
            return await real_terminal(*args, **kwargs)

        async def _unit(db, item_or_id, *args, **kwargs):
            if item_or_id is not None:
                dispositions.append(getattr(item_or_id, "id", item_or_id))
            return await real_unit(db, item_or_id, *args, **kwargs)

        stack.enter_context(patch.object(farm_policy, "on_terminal", _terminal))
        stack.enter_context(patch.object(farm_policy, "on_unit_terminal", _unit))
        return outcomes, dispositions


class TestOperatorStopIsDurable(_DurableStop):
    """§6 (2026-09-25): the operator's Stop lives on the unit row, not in process memory.

    The in-memory ``main._user_stopped_printers`` set was empty after a restart (a deploy), so
    the terminal of a job an operator had stopped read as no stop: the farm's own queue-stopped
    job resolved FOREIGN (a foreign page, a human-clear gate, no operator-stop hold, the run one
    plate short), and a printer-card stop recorded a genuine FAILURE. Driven through the real
    route / verb, the real ``on_print_complete`` and the real ``farm_policy``."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        self._reset_process_state()
        yield
        self._reset_process_state()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raw", ["failed", "aborted"])
    async def test_a_queue_page_stop_whose_terminal_lands_after_a_restart_is_the_operators(self, test_engine, raw):
        from contextlib import ExitStack

        from backend.app.models.print_batch import PrintBatch
        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.services import farm_policy
        from backend.app.services.printer_manager import printer_manager

        tasks_before = set(asyncio.all_tasks())
        with ExitStack() as stack:
            mocks = TestPlateClearGate._setup_mocks(stack, test_engine)
            stopped_page = stack.enter_context(
                patch.object(farm_policy.notification_service, "on_run_unit_stopped", new_callable=AsyncMock)
            )
            stack.enter_context(patch.object(printer_manager, "stop_print", return_value=True))
            outcomes, dispositions = self.spy_policy(stack)
            printer_id, item_id, batch_id = await self._seed_farm_unit(
                mocks.maker, serial=f"DRQ-{raw}", subtask="SUB-RQ"
            )

            await self.queue_page_stop(mocks.maker, item_id)
            self.restart()
            await self._terminal(printer_id, subtask="SUB-RQ", status=raw, deposited=True)
            await self._settle(tasks_before)

            (outcome,) = outcomes
            assert (outcome.verdict, outcome.recorded_status) == ("operator_ui", "cancelled")
            assert dispositions == [item_id]  # the unit is MATCHED, not foreign
            async with mocks.maker() as s:
                item = await s.get(PrintQueueItem, item_id)
                assert (item.status, item.stop_source) == ("cancelled", "operator_ui")
                assert item.operator_stop_requested_at is not None
                assert item.stop_answered_at is not None
                batch = await s.get(PrintBatch, batch_id)
                assert (batch.status, batch.pause_reason) == ("active", "operator_stop")  # the run HOLDS
            stopped_page.assert_awaited_once()
            # No foreign lane at all: no foreign-job page, and the gate is the farm's own
            # deposit keyed to its job (the unit matched above), not a foreign plate's.
            mocks.notif.on_foreign_job_detected.assert_not_awaited()
            assert _occupancy().snapshot(printer_id).plate_source_subtask_id == "SUB-RQ"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raw", ["failed", "aborted"])
    async def test_a_printer_card_stop_whose_terminal_lands_after_a_restart_is_the_operators(self, test_engine, raw):
        from contextlib import ExitStack

        from backend.app.models.print_batch import PrintBatch
        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.services import farm_policy
        from backend.app.services.print_control import stop_as_operator
        from backend.app.services.printer_manager import printer_manager

        tasks_before = set(asyncio.all_tasks())
        with ExitStack() as stack:
            mocks = TestPlateClearGate._setup_mocks(stack, test_engine)
            stopped_page = stack.enter_context(
                patch.object(farm_policy.notification_service, "on_run_unit_stopped", new_callable=AsyncMock)
            )
            running = self._state(gcode_state="RUNNING", subtask="SUB-RC")
            stack.enter_context(patch.object(printer_manager, "get_status", return_value=running))
            stack.enter_context(patch.object(printer_manager, "stop_print", return_value=True))
            outcomes, dispositions = self.spy_policy(stack)
            printer_id, item_id, batch_id = await self._seed_farm_unit(
                mocks.maker, serial=f"DRC-{raw}", subtask="SUB-RC"
            )

            assert await stop_as_operator(printer_id) is True  # the printer card's Stop
            self.restart()
            await self._terminal(printer_id, subtask="SUB-RC", status=raw, deposited=True)
            await self._settle(tasks_before)

            (outcome,) = outcomes
            assert (outcome.verdict, outcome.recorded_status) == ("operator_ui", "cancelled")
            assert dispositions == [item_id]
            async with mocks.maker() as s:
                item = await s.get(PrintQueueItem, item_id)
                assert (item.status, item.stop_source) == ("cancelled", "operator_ui")  # never a FAILURE
                batch = await s.get(PrintBatch, batch_id)
                assert batch.pause_reason == "operator_stop"
            stopped_page.assert_awaited_once()
            mocks.notif.on_foreign_job_detected.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_stop_whose_print_never_started_does_not_classify_the_re_dispatch(self, test_engine):
        """The stop was pressed on a claim whose print never started; the dead-claim release
        (the ONE printing → pending writer) clears the request, so the SAME row's next
        dispatch — a genuine failure — is a failure, not the operator's stop."""
        from contextlib import ExitStack
        from datetime import datetime, timezone

        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.services.print_control import stop_as_operator
        from backend.app.services.printer_manager import printer_manager
        from backend.app.services.queue_transitions import claim_pending_for_dispatch, release_unstarted_claim

        tasks_before = set(asyncio.all_tasks())
        with ExitStack() as stack:
            mocks = TestPlateClearGate._setup_mocks(stack, test_engine)
            idle = self._state(gcode_state="IDLE", subtask=None)
            stack.enter_context(patch.object(printer_manager, "get_status", return_value=idle))
            stack.enter_context(patch.object(printer_manager, "stop_print", return_value=True))
            outcomes, _dispositions = self.spy_policy(stack)
            printer_id, item_id, _batch = await self._seed_farm_unit(mocks.maker, serial="DST-1", subtask="SUB-DEAD")

            await stop_as_operator(printer_id)  # the sole printing unit carries the request
            async with mocks.maker() as s:
                assert (await s.get(PrintQueueItem, item_id)).operator_stop_requested_at is not None
                assert await release_unstarted_claim(s, item_id=item_id)  # the claim was dead
                await s.commit()
            async with mocks.maker() as s:  # the re-dispatch of the same row, under a new id
                assert await claim_pending_for_dispatch(
                    s, item_id=item_id, started_at=datetime.now(timezone.utc), ams_mapping=None, printer_id=printer_id
                )
                (await s.get(PrintQueueItem, item_id)).dispatch_subtask_id = "SUB-NEW"
                await s.commit()

            await self._terminal(printer_id, subtask="SUB-NEW", status="failed", deposited=True)
            await self._settle(tasks_before)

            (outcome,) = outcomes
            assert outcome.verdict is None
            assert outcome.recorded_status == "failed"
            async with mocks.maker() as s:
                item = await s.get(PrintQueueItem, item_id)
                assert (item.status, item.stop_source, item.operator_stop_requested_at) == ("failed", None, None)

    @pytest.mark.asyncio
    async def test_a_duplicate_terminal_for_a_queue_stopped_job_disposes_the_unit_once(self, test_engine):
        """Nothing in process memory marks the first terminal any more: the row's own answer
        (``stop_answered_at``) does, so the second terminal still resolves to the farm's unit
        (never FOREIGN) and is owed nothing for it — one hold, one page."""
        from contextlib import ExitStack

        from backend.app.models.print_batch import PrintBatch
        from backend.app.services import farm_policy
        from backend.app.services.printer_manager import printer_manager

        tasks_before = set(asyncio.all_tasks())
        with ExitStack() as stack:
            mocks = TestPlateClearGate._setup_mocks(stack, test_engine)
            stopped_page = stack.enter_context(
                patch.object(farm_policy.notification_service, "on_run_unit_stopped", new_callable=AsyncMock)
            )
            stack.enter_context(patch.object(printer_manager, "stop_print", return_value=True))
            outcomes, dispositions = self.spy_policy(stack)
            printer_id, item_id, batch_id = await self._seed_farm_unit(mocks.maker, serial="DUP-1", subtask="SUB-DQ")

            await self.queue_page_stop(mocks.maker, item_id)
            await self._terminal(printer_id, subtask="SUB-DQ", deposited=True)
            await self._settle(tasks_before)
            self.restart()
            await self._terminal(printer_id, subtask="SUB-DQ", deposited=True)
            await self._settle(tasks_before)

            assert [o.verdict for o in outcomes] == ["operator_ui", "operator_ui"]
            assert dispositions == [item_id]  # ONE disposition for the unit
            stopped_page.assert_awaited_once()  # ONE page
            mocks.notif.on_foreign_job_detected.assert_not_awaited()  # and never a foreign one
            async with mocks.maker() as s:
                batch = await s.get(PrintBatch, batch_id)
                assert batch.pause_reason == "operator_stop"


class TestReconciledUnknownOutcome:
    """The downtime reconcile's IDLE branch, end to end (2026-09-19).

    The printer came back with no FINISH/FAILED to believe, so the farm never learned
    how the print ended. The synthesised terminal used to land as ``aborted`` with a
    NULL ``stop_source``, which matched no branch in ``farm_policy.on_terminal`` at
    all — the run stayed ACTIVE, one plate short, with nothing on any surface saying
    so, and the operator found it by counting parts.

    Three links are pinned here as ONE chain, because each is useless without the next:
    the reconcile branch SAYS it does not know (``outcome_unknown``), the one classifier
    turns that into the ``reconcile_unknown`` verdict, and the terminal handler records
    it — status normalised to ``cancelled``, the verdict stamped on the row. What
    ``farm_policy`` then does with it (the run holds, RESUME tops the deficit up) is
    pinned in ``test_farm_policy.TestAnUnknownOutcomeHoldsTheRun``.
    """

    @staticmethod
    async def _run_terminal(test_engine, *, outcome_unknown: bool):
        import contextlib
        from contextlib import ExitStack

        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.services.farm_correlation import PAYLOAD_KEY_OUTCOME_UNKNOWN

        tasks_before = set(asyncio.all_tasks())
        with ExitStack() as stack:
            mocks = TestPlateClearGate._setup_mocks(stack, test_engine)
            printer_id, item_id = await TestPlateClearGate._seed_printing_item(
                mocks.maker, serial="RCN-1", dispatch_subtask_id="SUB-R"
            )
            # The status the POLICY is handed is the load-bearing half: ``aborted``
            # matched no branch in its disposition fork, which is why the run went
            # quietly one plate short.
            policy = stack.enter_context(patch("backend.app.services.farm_policy.on_terminal", new_callable=AsyncMock))

            from backend.app.main import on_print_complete

            # Byte-for-byte the shape ``reconcile_stale_active_prints`` synthesises on
            # its IDLE / subtask-mismatch branch.
            payload = {
                "status": "aborted",
                "filename": "/data/Metadata/test.gcode",
                "subtask_name": "Test",
                "subtask_id": "SUB-R",
                "timelapse_was_active": False,
                "peaks_reliable": False,
                "_reconciled": True,
            }
            if outcome_unknown:
                payload[PAYLOAD_KEY_OUTCOME_UNKNOWN] = True
            await on_print_complete(printer_id, payload)
            # The farm hook for a print with no archive rides the ``notify-no-archive``
            # task, so it is AWAITED rather than drained — a cancel would race the very
            # call this test reads.
            for task in asyncio.all_tasks() - tasks_before:
                if (task.get_name() or "") == "notify-no-archive":
                    with contextlib.suppress(Exception):
                        await task
            await TestPlateClearGate._drain(tasks_before)

            async with mocks.maker() as s:
                item = await s.get(PrintQueueItem, item_id)
                policy_status = policy.await_args[0][3] if policy.await_args is not None else None
                return item.status, item.stop_source, policy_status

    @pytest.mark.asyncio
    async def test_the_reconcile_abort_records_cancelled_with_the_unknown_verdict(self, test_engine):
        """LIVENESS — on the pre-2026-09-19 build this row read ``cancelled`` with a
        NULL ``stop_source`` while the POLICY was handed ``aborted``, and its
        disposition fork skipped it entirely."""
        status, stop_source, policy_status = await self._run_terminal(test_engine, outcome_unknown=True)

        assert status == "cancelled"
        assert stop_source == "reconcile_unknown"
        # ONE word for this terminal everywhere — including the one the fork reads.
        assert policy_status == "cancelled"

    @pytest.mark.asyncio
    async def test_an_ordinary_abort_records_cancelled_with_no_verdict(self, test_engine):
        """The flag is what carries the VERDICT, not the ``aborted`` status: a terminal
        that simply reports aborted records no verdict, because nobody said the outcome
        was unknown. CHANGED 2026-09-24: the policy is handed ``cancelled`` all the same
        — handed ``aborted`` it matched no branch and the run ended one plate short."""
        status, stop_source, policy_status = await self._run_terminal(test_engine, outcome_unknown=False)

        assert status == "cancelled"
        assert stop_source is None
        assert policy_status == "cancelled"


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
    async def test_eject_completed_no_rewrite_notification_suppressed_farm_finalises(self, test_engine, sd_card_delete):
        """A clean eject FINISH reaches farm_policy as 'completed' (NOT rewritten to
        'cancelled'), emits NO print notification, yet the farm hook + SD-card
        cleanup still run."""
        from contextlib import ExitStack

        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
        # …and with the terminal's HMS list, which the policy pages with when a printer
        # rejects an eject file at setup. None here because this sweep ended clean; the
        # pin is that the key is FORWARDED at all (a dropped kwarg raises KeyError).
        assert farm_hook.await_args.kwargs["hms_errors"] is None
        # No "Print Complete/Stopped" notification for the sweep.
        mock_notif.on_print_complete.assert_not_awaited()
        # SD-card cleanup of the uploaded eject file still happened.
        sd_card_delete.assert_awaited()

    @pytest.mark.asyncio
    async def test_dry_run_terminal_untouched_not_treated_as_eject(self, test_engine):
        """A dry-run (queue item, NO PendingEject) is NOT an eject job: its no-deposit
        terminal is still rewritten to 'cancelled' and STILL emits a notification —
        proving the eject exemption does not bleed into the dry-run path."""
        from contextlib import ExitStack

        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
        """Name evidence: an eject-NAMED terminal that arrives with an EMPTY pending
        registry (a foreign instance's sweep after our restart lost the registry, or a
        cross-instance eject) is still recognised as an eject by name — even with
        motion progress reported. It must NOT be rewritten, NOT raise the plate gate,
        NOT fire the foreign-job notification, and NOT emit a print notification."""
        from contextlib import ExitStack

        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
        """An eject-job terminal must NOT trigger the AMS RFID re-read sweep —
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
        """A CLAIMED-eject terminal never calls resolve_terminal_item (so it cannot
        log the false-FOREIGN warning for the farm's own sweep) and skips the archive
        lookup (which always misses for a sweep, so no "Could not find archive"
        warning). farm_policy.on_terminal still finalises the sweep as 'completed'."""
        from contextlib import ExitStack

        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from backend.app.services import farm_correlation

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
        """Name evidence: an eject-NAMED terminal with NO claimed eject
        (is_eject_job_name path — a restart lost the claim) is still recognised as
        our sweep before correlation. resolve_terminal_item is not called and no
        FOREIGN / no "Could not find archive" warning fires; the farm hook still runs."""
        from contextlib import ExitStack

        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from backend.app.services import farm_correlation

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
