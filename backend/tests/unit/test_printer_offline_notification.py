"""Tests for the connected → disconnected edge that fires the
`on_printer_offline` notification (#1752).

The provider toggle, schema, and dispatcher already existed; what was missing
was a caller that fires `notification_service.on_printer_offline` when a
printer goes offline. These tests pin both layers:

  * `_maybe_notify_printer_offline` — the debounced background task. Must
    fire when the printer is still offline at the end of the window, and
    must NOT fire if the printer reconnected during the window.

  * Edge detection inside `on_printer_status_change` — schedules the task
    only on the True → False transition, cancels any pending task on
    reconnect, and stays silent on startup (no prior connected state).
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app import main as main_module

# One outage's wall-clock stamp. The transport writes it once on the
# connected -> disconnected edge and keeps it, so every offline push of the same
# outage carries this identical value — which is what makes the repeat silent.
_OUTAGE_AT = 1_757_000_000.0


def _state(connected: bool, state: str = "IDLE", disconnected_at: float | None = None) -> SimpleNamespace:
    """Minimal PrinterState stub. `state="IDLE"` keeps the reconcile-edge
    branch quiescent (it only fires on `connected=True` with a non-unknown
    state-string, which we exercise separately) but otherwise lets the
    handler thread through without doing extra DB / WS work.

    ``disconnected_at`` models the transport contract the offline edge now reads
    (`BambuMQTTClient.note_disconnected`): ONE wall-clock stamp per outage, written on
    the connected -> disconnected transition and KEPT unchanged across every later push
    — including the reconnect. Tests therefore pass the SAME value for repeated offline
    pushes of one outage, and a different one for a second outage."""
    return SimpleNamespace(
        connected=connected,
        connection_epoch=1,
        disconnected_at=disconnected_at,
        state=state,
        progress=0,
        layer_num=0,
        temperatures={},
        raw_data={},
        stg_cur=0,
        cooling_fan_speed=0,
        big_fan1_speed=0,
        big_fan2_speed=0,
        chamber_light="",
        active_extruder=0,
        tray_now=0,
        door_open=False,
        subtask_name="",
        ams_filament_backup=None,
    )


@pytest.fixture(autouse=True)
def _reset_edge_state():
    """Clear the module-level edge dicts between tests so one test's
    True-edge doesn't leak into the next."""
    main_module._printer_offline_edge_at.clear()
    for task in list(main_module._printer_offline_notify_tasks.values()):
        if not task.done():
            task.cancel()
    main_module._printer_offline_notify_tasks.clear()
    main_module._printer_reconciled_epoch.clear()
    main_module._last_status_broadcast.clear()
    yield
    main_module._printer_offline_edge_at.clear()
    for task in list(main_module._printer_offline_notify_tasks.values()):
        if not task.done():
            task.cancel()
    main_module._printer_offline_notify_tasks.clear()


class TestMaybeNotifyPrinterOffline:
    """The debounced background task — fires notification at the end of the
    window only if the printer is still offline."""

    @pytest.mark.asyncio
    async def test_fires_notification_when_still_offline_after_debounce(self):
        printer = SimpleNamespace(id=1, name="Workshop")
        scalar = MagicMock()
        scalar.scalar_one_or_none.return_value = printer
        db = AsyncMock()
        db.execute = AsyncMock(return_value=scalar)
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(return_value=db)
        session_cm.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("backend.app.main.asyncio.sleep", new=AsyncMock()),
            patch("backend.app.main.printer_manager") as mock_pm,
            patch("backend.app.main.async_session", return_value=session_cm),
            patch("backend.app.main.notification_service") as mock_notif,
        ):
            mock_pm.is_connected.return_value = False
            mock_notif.on_printer_offline = AsyncMock()

            await main_module._maybe_notify_printer_offline(printer_id=1)

            mock_notif.on_printer_offline.assert_awaited_once_with(1, "Workshop", db)

    @pytest.mark.asyncio
    async def test_does_not_fire_when_printer_reconnected_during_debounce(self):
        with (
            patch("backend.app.main.asyncio.sleep", new=AsyncMock()),
            patch("backend.app.main.printer_manager") as mock_pm,
            patch("backend.app.main.notification_service") as mock_notif,
        ):
            mock_pm.is_connected.return_value = True
            mock_notif.on_printer_offline = AsyncMock()

            await main_module._maybe_notify_printer_offline(printer_id=1)

            mock_notif.on_printer_offline.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_does_not_fire_when_printer_missing_from_db(self):
        scalar = MagicMock()
        scalar.scalar_one_or_none.return_value = None
        db = AsyncMock()
        db.execute = AsyncMock(return_value=scalar)
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(return_value=db)
        session_cm.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("backend.app.main.asyncio.sleep", new=AsyncMock()),
            patch("backend.app.main.printer_manager") as mock_pm,
            patch("backend.app.main.async_session", return_value=session_cm),
            patch("backend.app.main.notification_service") as mock_notif,
        ):
            mock_pm.is_connected.return_value = False
            mock_notif.on_printer_offline = AsyncMock()

            await main_module._maybe_notify_printer_offline(printer_id=1)

            mock_notif.on_printer_offline.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_clears_task_entry_after_run(self):
        with (
            patch("backend.app.main.asyncio.sleep", new=AsyncMock()),
            patch("backend.app.main.printer_manager") as mock_pm,
        ):
            mock_pm.is_connected.return_value = True  # No notification path
            main_module._printer_offline_notify_tasks[1] = MagicMock()
            await main_module._maybe_notify_printer_offline(printer_id=1)
            assert 1 not in main_module._printer_offline_notify_tasks


class TestOfflineEdgeDetection:
    """Edge detection inside `on_printer_status_change` — only the
    True → False transition schedules a task. Reconnects cancel pending
    tasks. Startup-with-disconnected does not fire."""

    @staticmethod
    def _patch_handler_deps():
        """Patch out the heavy side-effects of `on_printer_status_change`
        (MQTT relay, WebSocket broadcast, state serializer) so we can focus
        on edge state."""
        ws_mgr = MagicMock()
        ws_mgr.send_printer_status = AsyncMock()
        relay = MagicMock()
        relay.on_printer_status = AsyncMock()
        pm = MagicMock()
        pm.get_printer.return_value = None  # Skip the relay payload branch.
        pm.get_model.return_value = ""
        return ws_mgr, relay, pm

    @pytest.mark.asyncio
    async def test_first_call_connected_does_not_schedule(self):
        ws_mgr, relay, pm = self._patch_handler_deps()
        with (
            patch("backend.app.main.ws_manager", ws_mgr),
            patch("backend.app.main.mqtt_relay", relay),
            patch("backend.app.main.printer_manager", pm),
            patch("backend.app.main.spawn_background_task"),
            patch("backend.app.main.printer_state_to_dict", return_value={}),
        ):
            await main_module.on_printer_status_change(1, _state(connected=True))
        assert 1 not in main_module._printer_offline_notify_tasks
        assert main_module._printer_offline_edge_at[1] is None

    @pytest.mark.asyncio
    async def test_first_call_disconnected_does_not_schedule(self):
        """Startup with an already-offline printer must not fire.

        A printer this process has never seen connected carries NO outage stamp —
        ``note_disconnected`` only writes one on a connected -> disconnected transition —
        so there is no edge to trigger on. That is the same silence the old
        prior-observation latch produced, now stated by the transport instead of
        inferred from the absence of a previous callback."""
        ws_mgr, relay, pm = self._patch_handler_deps()
        with (
            patch("backend.app.main.ws_manager", ws_mgr),
            patch("backend.app.main.mqtt_relay", relay),
            patch("backend.app.main.printer_manager", pm),
            patch("backend.app.main.spawn_background_task"),
            patch("backend.app.main.printer_state_to_dict", return_value={}),
        ):
            await main_module.on_printer_status_change(1, _state(connected=False))
        assert 1 not in main_module._printer_offline_notify_tasks
        assert main_module._printer_offline_edge_at[1] is None

    @pytest.mark.asyncio
    async def test_connected_to_disconnected_schedules_task(self):
        ws_mgr, relay, pm = self._patch_handler_deps()
        with (
            patch("backend.app.main.ws_manager", ws_mgr),
            patch("backend.app.main.mqtt_relay", relay),
            patch("backend.app.main.printer_manager", pm),
            patch("backend.app.main.spawn_background_task"),
            patch("backend.app.main._maybe_notify_printer_offline", new=AsyncMock()),
            patch("backend.app.main.printer_state_to_dict", return_value={}),
        ):
            await main_module.on_printer_status_change(1, _state(connected=True))
            await main_module.on_printer_status_change(1, _state(connected=False, disconnected_at=_OUTAGE_AT))

        task = main_module._printer_offline_notify_tasks.get(1)
        assert task is not None
        task.cancel()

    @pytest.mark.asyncio
    async def test_reconnect_cancels_pending_task(self):
        ws_mgr, relay, pm = self._patch_handler_deps()
        with (
            patch("backend.app.main.ws_manager", ws_mgr),
            patch("backend.app.main.mqtt_relay", relay),
            patch("backend.app.main.printer_manager", pm),
            patch("backend.app.main.spawn_background_task"),
            patch("backend.app.main._maybe_notify_printer_offline", new=AsyncMock()),
            patch("backend.app.main.printer_state_to_dict", return_value={}),
        ):
            await main_module.on_printer_status_change(1, _state(connected=True))
            await main_module.on_printer_status_change(1, _state(connected=False, disconnected_at=_OUTAGE_AT))
            scheduled = main_module._printer_offline_notify_tasks.get(1)
            assert scheduled is not None
            await main_module.on_printer_status_change(1, _state(connected=True))
            # Yield so the cancellation propagates through the event loop.
            await asyncio.sleep(0)

        assert 1 not in main_module._printer_offline_notify_tasks
        assert scheduled.cancelled() or scheduled.done()

    @pytest.mark.asyncio
    async def test_repeated_disconnected_does_not_reschedule(self):
        """A second False observation while a task is already pending must
        not replace the in-flight task — otherwise the debounce clock
        resets on every status callback and the notification never fires."""
        ws_mgr, relay, pm = self._patch_handler_deps()
        with (
            patch("backend.app.main.ws_manager", ws_mgr),
            patch("backend.app.main.mqtt_relay", relay),
            patch("backend.app.main.printer_manager", pm),
            patch("backend.app.main.spawn_background_task"),
            patch("backend.app.main._maybe_notify_printer_offline", new=AsyncMock()),
            patch("backend.app.main.printer_state_to_dict", return_value={}),
        ):
            await main_module.on_printer_status_change(1, _state(connected=True))
            await main_module.on_printer_status_change(1, _state(connected=False, disconnected_at=_OUTAGE_AT))
            first_task = main_module._printer_offline_notify_tasks.get(1)
            await main_module.on_printer_status_change(1, _state(connected=False, disconnected_at=_OUTAGE_AT))
            second_task = main_module._printer_offline_notify_tasks.get(1)

        assert first_task is second_task
        if first_task is not None:
            first_task.cancel()

    @pytest.mark.asyncio
    async def test_a_second_outage_notifies_again(self):
        """The bookmark is the OUTAGE, not merely "was offline".

        A printer that goes down, comes back and goes down again must page twice. The
        retired boolean latch got this right by observing the True in between; the
        replacement gets it right because the transport stamps a NEW `disconnected_at`
        on each connected -> disconnected transition, which is the fact that actually
        distinguishes two outages from one long one."""
        ws_mgr, relay, pm = self._patch_handler_deps()
        second_outage_at = _OUTAGE_AT + 600.0
        with (
            patch("backend.app.main.ws_manager", ws_mgr),
            patch("backend.app.main.mqtt_relay", relay),
            patch("backend.app.main.printer_manager", pm),
            patch("backend.app.main.spawn_background_task"),
            patch("backend.app.main._maybe_notify_printer_offline", new=AsyncMock()),
            patch("backend.app.main.printer_state_to_dict", return_value={}),
        ):
            await main_module.on_printer_status_change(1, _state(connected=True))
            await main_module.on_printer_status_change(1, _state(connected=False, disconnected_at=_OUTAGE_AT))
            first_task = main_module._printer_offline_notify_tasks.get(1)
            assert first_task is not None
            # Reconnect cancels the pending page; the stamp is KEPT across it by design.
            await main_module.on_printer_status_change(1, _state(connected=True, disconnected_at=_OUTAGE_AT))
            await asyncio.sleep(0)
            assert 1 not in main_module._printer_offline_notify_tasks

            await main_module.on_printer_status_change(1, _state(connected=False, disconnected_at=second_outage_at))
            second_task = main_module._printer_offline_notify_tasks.get(1)

        assert second_task is not None
        assert second_task is not first_task
        second_task.cancel()


class TestReconcileOncePerSession:
    """`reconcile_stale_active_prints` fires exactly once per MQTT SESSION (#1542).

    The latch this replaced re-armed only when a `state.connected = False` callback was
    OBSERVED. The 2026-09-04 fleet outage arrived as a stale-reconnect, whose disconnect
    callback returns early by design — so the boolean could stay True across a genuine
    reboot and skip the reconciliation that outage most needed. The session epoch is
    incremented by the transport on every successful connect, observed or not.
    """

    @staticmethod
    def _patch_handler_deps():
        ws_mgr = MagicMock()
        ws_mgr.send_printer_status = AsyncMock()
        relay = MagicMock()
        relay.on_printer_status = AsyncMock()
        pm = MagicMock()
        pm.get_printer.return_value = None
        pm.get_model.return_value = ""
        return ws_mgr, relay, pm

    async def _push(self, spawn, state) -> None:
        ws_mgr, relay, pm = self._patch_handler_deps()
        with (
            patch("backend.app.main.ws_manager", ws_mgr),
            patch("backend.app.main.mqtt_relay", relay),
            patch("backend.app.main.printer_manager", pm),
            patch("backend.app.main.spawn_background_task", spawn),
            patch("backend.app.main.printer_state_to_dict", return_value={}),
        ):
            await main_module.on_printer_status_change(1, state)

    @staticmethod
    def _reconcile_spawns(spawn) -> int:
        return sum(1 for call in spawn.call_args_list if "reconcile-stale-prints" in str(call.kwargs.get("name", "")))

    @pytest.mark.asyncio
    async def test_fires_once_per_session_and_again_on_the_next(self):
        spawn = MagicMock()
        first = _state(connected=True)
        await self._push(spawn, first)
        await self._push(spawn, _state(connected=True))  # same epoch, same session
        assert self._reconcile_spawns(spawn) == 1

        # A reconnect: the transport increments the epoch. No disconnected callback is
        # required in between — that is the whole point.
        reconnected = _state(connected=True)
        reconnected.connection_epoch = 2
        await self._push(spawn, reconnected)
        assert self._reconcile_spawns(spawn) == 2

    @pytest.mark.asyncio
    async def test_does_not_fire_before_a_real_push_status(self):
        """`_on_connect` broadcasts before the first push_status, when `state.state` is
        still the construction default — reconciling there synthesises `aborted` for
        every in-flight archive."""
        spawn = MagicMock()
        await self._push(spawn, _state(connected=True, state="unknown"))
        assert self._reconcile_spawns(spawn) == 0
        await self._push(spawn, _state(connected=True))
        assert self._reconcile_spawns(spawn) == 1
