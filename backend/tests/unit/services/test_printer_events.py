"""The printer-event hub (``services/printer_events``) and its composition root (``printer_event_wiring``).

The hub re-publishes facts that already had one producer each, so what these cases pin is the
DISPATCH contract every lane relies on: subscription order, an async handler awaited before the
next one runs (the order the hand-written fan-out in ``main`` had), a failing handler that never
stops the others (invariant 10), and the one wiring that writes the order down. The ownership pins
(one construction site per event, one ``publish`` / ``subscribe`` site) live in
``test_code_quality.TestPrinterEventOwnership``.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.services import printer_event_wiring, printer_events
from backend.app.services.printer_events import (
    CommandAcked,
    FirstFreshReport,
    HmsEdgesAppeared,
    SelfHealDue,
    SessionLost,
    StatusReported,
)
from backend.app.services.session_start import SessionStart

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _no_wiring():
    printer_event_wiring.unwire()
    yield
    printer_event_wiring.unwire()


class TestDispatch:
    async def test_handlers_run_in_subscription_order_and_an_async_one_is_awaited_in_place(self):
        seen: list[str] = []

        def first(event):
            seen.append(f"first:{event.printer_id}")

        async def second(event):
            seen.append("second:start")
            await AsyncMock()()
            seen.append("second:end")

        def third(_event):
            seen.append("third")

        unsubscribers = [printer_events.subscribe(StatusReported, handler) for handler in (first, second, third)]
        try:
            await printer_events.publish(StatusReported(printer_id=7, state=SimpleNamespace()))
        finally:
            for unsubscribe in unsubscribers:
                unsubscribe()

        assert seen == ["first:7", "second:start", "second:end", "third"]

    async def test_a_failing_handler_is_logged_and_the_next_one_still_runs(self, caplog):
        seen: list[int] = []

        def boom(_event):
            raise RuntimeError("one lane on fire")

        unsubscribers = [
            printer_events.subscribe(FirstFreshReport, boom),
            printer_events.subscribe(FirstFreshReport, lambda event: seen.append(event.connection_epoch)),
        ]
        try:
            with caplog.at_level(logging.ERROR, logger="backend.app.services.printer_events"):
                await printer_events.publish(FirstFreshReport(printer_id=3, connection_epoch=2))
        finally:
            for unsubscribe in unsubscribers:
                unsubscribe()

        assert seen == [2]
        assert "FirstFreshReport handler" in caplog.text and "printer 3" in caplog.text

    async def test_an_event_reaches_its_own_kind_only(self):
        seen: list[str] = []
        unsubscribe = printer_events.subscribe(CommandAcked, lambda event: seen.append("ack"))
        try:
            await printer_events.publish(StatusReported(printer_id=1, state=SimpleNamespace()))
            await printer_events.publish(CommandAcked(printer_id=1, ack=SimpleNamespace()))
        finally:
            unsubscribe()

        assert seen == ["ack"]

    async def test_subscribing_twice_is_one_subscription_and_unsubscribe_removes_it(self):
        seen: list[int] = []

        def handler(event):
            seen.append(event.printer_id)

        unsubscribe = printer_events.subscribe(StatusReported, handler)
        printer_events.subscribe(StatusReported, handler)
        await printer_events.publish(StatusReported(printer_id=4, state=SimpleNamespace()))
        unsubscribe()
        await printer_events.publish(StatusReported(printer_id=5, state=SimpleNamespace()))

        assert seen == [4]


class TestTheWiring:
    """The composition root is the one place the order is written (its module docstring)."""

    async def test_every_kind_is_wired_in_the_documented_order(self):
        printer_event_wiring.wire(downtime_reconcile=AsyncMock(return_value=0))

        def names(kind):
            return [getattr(handler, "__name__", "") for handler in printer_events.subscribers(kind)]

        def owners(kind):
            return [
                f"{getattr(handler, '__module__', '').rsplit('.', 1)[-1]}.{getattr(handler, '__name__', '')}"
                for handler in printer_events.subscribers(kind)
            ]

        assert names(HmsEdgesAppeared) == ["_runout_spent_stamps", "_storage_low", "_runout_guidance"]
        # The record reads the push first; the dispatch's landing re-arm reads the record's reading next
        # (2026-10-12, R17: production's Phase B bound anchored at the landing).
        assert owners(StatusReported)[:2] == ["printer_action.on_status_reported", "dispatch_start.on_status_reported"]
        assert names(StatusReported) == [
            "on_status_reported",
            "on_status_reported",
            "_stagger_bed_watch",
            "_backup_swaps",
            "_identify_activity",
            "_incident_wire_sampler",
            "_pause_recovery_sampler",
            "_ams_fault_entry",
        ]
        assert names(CommandAcked) == ["on_command_acked"]
        # The record's online clock stops at a lost session (2026-10-12, the self-heal bound).
        assert names(SessionLost) == ["on_session_lost"]
        # A bounded lane hears its self-heal fallback here: the eject start bound, the dispatch's start
        # bounds and the runout refill's resume bound (2026-10-12).
        assert owners(SelfHealDue) == [
            "remote.on_self_heal_due",
            "dispatch_start.on_self_heal_due",
            "spool_recovery.on_self_heal_due",
        ]

    async def test_the_rehydrated_start_bound_factory_is_wired_and_unwired(self):
        """A dispatch start rehydrated after a restart re-arms production's Phase A bound (R18, 2026-10-12);
        a runout refill's farm resume re-arms its own (R14), and a power-loss resume its own (R5: no subscriber —
        the re-spawned driver's new resume holds the printer; the passed bound keeps the orphan off the page)."""
        from backend.app.services import dispatch_start, pause_recovery, printer_action, spool_recovery

        printer_event_wiring.wire(downtime_reconcile=AsyncMock(return_value=0))
        assert printer_action._bound_factories["print_start"] is dispatch_start.rehydrated_start_bound
        assert printer_action._bound_factories["resume"] is spool_recovery.rehydrated_runout_resume_bound
        assert printer_action._bound_factories["power_loss_resume"] is pause_recovery.rehydrated_power_loss_resume_bound
        printer_event_wiring.unwire()
        assert "print_start" not in printer_action._bound_factories
        assert "resume" not in printer_action._bound_factories
        assert "power_loss_resume" not in printer_action._bound_factories

    async def test_the_first_fresh_report_has_one_subscriber_the_session_start(self):
        printer_event_wiring.wire(downtime_reconcile=AsyncMock(return_value=0))

        (handler,) = printer_events.subscribers(FirstFreshReport)
        assert isinstance(handler.__self__, SessionStart)
        assert handler.__func__ is SessionStart.on_first_fresh_report

    async def test_wiring_twice_replaces_the_first_and_unwire_removes_everything(self):
        printer_event_wiring.wire(downtime_reconcile=AsyncMock(return_value=0))
        printer_event_wiring.wire(downtime_reconcile=AsyncMock(return_value=0))

        assert len(printer_events.subscribers(StatusReported)) == 8
        assert len(printer_events.subscribers(FirstFreshReport)) == 1

        printer_event_wiring.unwire()
        for kind in (HmsEdgesAppeared, StatusReported, FirstFreshReport, SessionLost):
            assert printer_events.subscribers(kind) == ()


class TestTheAmsFaultEntryRidesTheHub:
    """The automatic AMS incident entry (``spool_recovery.on_ams_fault``) is a StatusReported subscriber
    (moved out of ``main``'s HMS-notify block, lane B of the 2026-10-11 plan): spawned on every push whose
    live HMS carries an ACTIONABLE fault — the LEVEL, never an HMS edge (a code standing since a restart
    never edges), and never for a push with none."""

    async def test_an_actionable_push_spawns_the_entry_and_a_quiet_one_does_not(self, monkeypatch):
        from types import SimpleNamespace

        from backend.app.services import hms_errors, spool_recovery

        spawned: list[str] = []

        def _spawn(coro, *, name):
            coro.close()
            spawned.append(name)

        monkeypatch.setattr(printer_event_wiring, "spawn_background_task", _spawn)
        monkeypatch.setattr(spool_recovery, "on_ams_fault", lambda pid, st: _noop())
        actionable = {"yes"}
        monkeypatch.setattr(hms_errors, "live_candidates", lambda st: actionable if st.fault else frozenset())

        printer_event_wiring._ams_fault_entry(StatusReported(printer_id=3, state=SimpleNamespace(fault=True)))
        printer_event_wiring._ams_fault_entry(StatusReported(printer_id=3, state=SimpleNamespace(fault=False)))

        assert spawned == ["ams-fault-p3"]


async def _noop():
    return None
