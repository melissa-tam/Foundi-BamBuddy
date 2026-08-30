"""Regression tests for ``PrinterManager._broadcast_status_change`` and its wiring
from an occupancy transition (#1128).

The bug: the plate flag is a Bambuddy-side fact, so toggling it produces no MQTT push
from the printer. Before the fix the mutation persisted to the DB but never notified
WebSocket subscribers — the plate-clear button on the printer card only appeared to
update "immediately" because of an optimistic React Query cache write on the click
path; any other caller (an admin script, a second tab, an automation hitting
``POST /printers/{id}/clear-plate``) silently left the UI stale until the next
coincidental status refresh.

**Moved, not retired (2026-08-30 occupancy cut-over).** The trigger used to be
``PrinterManager.set_awaiting_plate_clear``, which scheduled the persist and the
broadcast itself. That method is gone: the plate is owned by the occupancy authority
and the broadcast is one of its four injected side effects —
``plate_occupancy_store.broadcast_occupancy``, which schedules exactly the same
``printer_manager._broadcast_status_change(printer_id)``. So the contract is pinned
where it now lives: every occupancy transition schedules a ``printer_status``
broadcast, in BOTH directions, and the broadcast carries the new state so subscribers
see it without polling.

``_broadcast_status_change`` itself is UNCHANGED by the cut-over and keeps its own
pins below (present state → emit, unknown state → skip, WS failure → swallow).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services import plate_occupancy_store
from backend.app.services.plate_occupancy import (
    DepositEvidence,
    EscalationOnly,
    OccupancyView,
    TerminalDisposition,
    plate_occupancy,
)
from backend.app.services.printer_manager import PrinterManager, printer_manager as manager_singleton


@pytest.fixture(autouse=True)
def _clean_authority():
    """Isolate the module-singleton authority; wire ONLY the broadcast lane.

    ``reset_for_tests`` un-wires everything, so persist stays a no-op unless a test
    asks for it — no DB session is ever opened here.
    """
    plate_occupancy.reset_for_tests()
    plate_occupancy.configure(broadcast=plate_occupancy_store.broadcast_occupancy)
    yield
    plate_occupancy.reset_for_tests()


@pytest.fixture
def manager():
    """Fresh manager for the ``_broadcast_status_change`` unit tests."""
    return PrinterManager()


def _deposit() -> DepositEvidence:
    return DepositEvidence(
        final_status="completed",
        is_dry_run=False,
        peaks_reliable=True,
        last_layer_num=120,
        last_progress=100.0,
    )


def _terminal() -> TerminalDisposition:
    """A deposit-bearing terminal the raise guard allows through — raises the gate."""
    return TerminalDisposition(
        queue_item_id=None,
        source_subtask_id="SUB-1",
        evidence=_deposit(),
        policy=EscalationOnly(),
        raise_gate=True,
    )


def _fake_state(**overrides):
    """Stand-in for a ``PrinterState``.

    The fake carries every attribute the real ``printer_state_to_dict`` reads, so the
    tests never need to patch it — an earlier version patched the serializer and the
    patch was observed to race on parallel xdist runners, after which the real
    function ran against an incomplete fake and ``AttributeError``'d.

    Iterables (``kprofiles``, ``printable_objects``, ``hms_errors``, ``temperatures``,
    …) default to empty so the function's loops are no-ops; scalars default to
    ``None`` so any "if state.x is None" guard falls through cleanly.
    """
    base = {
        # State the test bodies explicitly set / read
        "connected": True,
        "state": "FINISH",
        "raw_data": {},
        "progress": 100.0,
        # Iterables — must be iterable for the loops inside printer_state_to_dict
        "kprofiles": [],
        "printable_objects": [],
        "hms_errors": [],
        "temperatures": {},
        "nozzle_rack": [],
        # Nullable scalars — printer_state_to_dict tolerates None for these
        "active_extruder": None,
        "ams_status_main": None,
        "ams_status_sub": None,
        "big_fan1_speed": None,
        "big_fan2_speed": None,
        "chamber_light": None,
        "cooling_fan_speed": None,
        "current_print": None,
        "door_open": None,
        "firmware_version": None,
        "gcode_file": None,
        "heatbreak_fan_speed": None,
        "layer_num": None,
        "remaining_time": None,
        "speed_level": None,
        "stg_cur": 0,  # get_derived_status_name does ``0 <= state.stg_cur < 255``
        "subtask_name": None,
        "total_layers": None,
        "tray_now": None,
        # printer_state_to_dict reads state.last_loaded_tray unconditionally beside
        # tray_now (W6.1 was-feeding ring); the real PrinterState always has it.
        "last_loaded_tray": -1,
        "wifi_signal": None,
        "wired_network": None,
        "ams_filament_backup": None,
        # USB/SD presence — printer_state_to_dict reads state.sdcard unconditionally
        # (#F8). Omitting it raised AttributeError before reaching send_printer_status.
        "sdcard": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


async def _drain() -> None:
    """Give ``spawn_background_task`` a chance to land its scheduled coroutine."""
    for _ in range(10):
        await asyncio.sleep(0)


class TestSchedulingFromAnOccupancyTransition:
    """The hook from a state change on the authority into the broadcast.

    The trigger moved from ``set_awaiting_plate_clear`` to the injected ``broadcast``
    callable, so these drive real transitions through the real store wiring.
    """

    async def test_a_transition_schedules_the_broadcast(self):
        """Every occupancy transition must enqueue the ``printer_status`` emit —
        without it, subscribers that are not the originating tab stay stale."""
        with patch.object(manager_singleton, "_broadcast_status_change", new_callable=AsyncMock) as broadcast:
            plate_occupancy.note_terminal(7, _terminal())
            await _drain()

        broadcast.assert_awaited_once_with(7)

    async def test_both_the_raise_and_the_release_broadcast(self):
        """The bug only became visible on a release, but a regression that broadcast
        one direction only would re-introduce the original symptom for the other.
        Make both directions a contract."""
        with patch.object(manager_singleton, "_broadcast_status_change", new_callable=AsyncMock) as broadcast:
            plate_occupancy.note_terminal(7, _terminal())  # raise
            await _drain()
            assert broadcast.await_count == 1

            assert plate_occupancy.clear_plate(7) is None  # release
            await _drain()

        assert broadcast.await_count == 2

    def test_does_not_schedule_without_a_running_loop(self):
        """The sync unit-test path (and any call from a non-loop thread).

        ``plate_occupancy_store._schedule`` checks for a running loop BEFORE building
        the coroutine, so nothing is scheduled and — just as importantly — no
        coroutine is constructed and dropped, which would emit "coroutine was never
        awaited" and turn harmless flag twiddling into a red suite.
        """
        with patch.object(manager_singleton, "_broadcast_status_change", new_callable=MagicMock) as broadcast:
            plate_occupancy.note_terminal(7, _terminal())

        broadcast.assert_not_called()
        # The transition itself still happened — a skipped side effect must never
        # unwind an occupancy fact.
        assert plate_occupancy.is_plate_occupied(7) is True

    async def test_hydration_schedules_neither_persist_nor_broadcast(self):
        """``cause="hydrate"`` skips persist, broadcast AND kick.

        The state was just READ from the DB, so echoing it back is a write with no
        information in it and a broadcast describing a fact no subscriber's view has
        changed over. Only the policy driver runs at hydration.
        """
        persisted: list[int] = []

        def _record_persist(printer_id: int, view: OccupancyView) -> None:
            persisted.append(printer_id)

        plate_occupancy.configure(persist=_record_persist)

        with patch.object(manager_singleton, "_broadcast_status_change", new_callable=AsyncMock) as broadcast:
            plate_occupancy.hydrate_plate(7, "SUB-1", EscalationOnly())
            await _drain()

        assert persisted == []
        broadcast.assert_not_awaited()
        assert plate_occupancy.is_plate_occupied(7) is True


class TestBroadcastStatusChange:
    """The broadcast coroutine itself — unchanged by the cut-over."""

    async def test_emits_ws_update_when_state_present(self, manager):
        """Happy path: the printer has a known status, so the broadcast goes out with
        the dict produced by ``printer_state_to_dict``.

        The serializer is deliberately NOT patched (see ``_fake_state``): we assert
        structural shape, not the exact key set, because pinning ~36 keys couples this
        test to an evolving function and adds nothing over ``test_printer_manager.py``.
        """
        state = _fake_state()
        with (
            patch.object(manager, "get_status", return_value=state),
            patch.object(manager, "get_model", return_value="P1S"),
            patch(
                "backend.app.core.websocket.ws_manager.send_printer_status",
                new_callable=AsyncMock,
            ) as send_status,
        ):
            await manager._broadcast_status_change(7)

        send_status.assert_awaited_once()
        printer_id_arg, payload_arg = send_status.await_args.args
        assert printer_id_arg == 7
        assert isinstance(payload_arg, dict)
        # ``awaiting_plate_clear`` is the whole point of this broadcast path (#1128),
        # and ``occupancy`` is its cut-over companion (the same builder both REST
        # branches use). Any restructuring that drops either would silently break the
        # UI; pin their presence.
        assert "awaiting_plate_clear" in payload_arg
        assert "occupancy" in payload_arg

    async def test_skips_when_status_unknown(self, manager):
        """Printer not connected / unknown id → no point broadcasting a snapshot we
        do not have. A future reconnect produces a fresh status push anyway, so we
        would only be forcing a stale payload onto subscribers now."""
        with (
            patch.object(manager, "get_status", return_value=None),
            patch(
                "backend.app.core.websocket.ws_manager.send_printer_status",
                new_callable=AsyncMock,
            ) as send_status,
        ):
            await manager._broadcast_status_change(999)

        send_status.assert_not_awaited()

    async def test_swallows_websocket_errors(self, manager):
        """The broadcast is a courtesy, not a correctness path. Letting an exception
        bubble out would surface as an ``Exception in scheduled callback`` traceback
        and — since the core calls this from inside a fan-out — could cost the
        transition its remaining side effects. Swallow + warn instead."""
        with (
            patch.object(manager, "get_status", return_value=_fake_state()),
            patch.object(manager, "get_model", return_value="P1S"),
            patch(
                "backend.app.core.websocket.ws_manager.send_printer_status",
                new_callable=AsyncMock,
                side_effect=RuntimeError("websocket layer unavailable"),
            ),
        ):
            # Must not raise.
            await manager._broadcast_status_change(7)


class TestEndToEndUnderRunningLoop:
    """The full flow under a real running loop — transition → injected broadcast →
    ``_broadcast_status_change`` → ``ws_manager.send_printer_status`` — with nothing
    between the halves mocked out. Catches regressions where each piece passes but
    the wiring is broken (the #1128 shape itself).
    """

    async def test_a_release_emits_a_broadcast_carrying_the_new_state(self):
        """Reproduces the #1128 fix path end to end.

        ``printer_state_to_dict`` reads the flag through
        ``printer_manager.is_awaiting_plate_clear``, which since the cut-over is a
        thin projection of the authority — so the payload reflects the transition
        that triggered the emit, in both directions.
        """
        with (
            patch.object(manager_singleton, "get_status", return_value=_fake_state()),
            patch.object(manager_singleton, "get_model", return_value="P1S"),
            patch(
                "backend.app.core.websocket.ws_manager.send_printer_status",
                new_callable=AsyncMock,
            ) as send_status,
        ):
            plate_occupancy.note_terminal(7, _terminal())
            await _drain()

            raised_payload = send_status.await_args.args[1]
            assert raised_payload["awaiting_plate_clear"] is True
            assert raised_payload["occupancy"]["plate"]["occupied"] is True

            assert plate_occupancy.clear_plate(7) is None
            await _drain()

        send_status.assert_awaited()
        printer_id_arg, payload_arg = send_status.await_args.args
        assert printer_id_arg == 7
        assert payload_arg["awaiting_plate_clear"] is False
        assert payload_arg["occupancy"]["plate"]["occupied"] is False
