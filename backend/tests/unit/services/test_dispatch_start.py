"""``dispatch_start`` — a dispatch's START answer, applied to its claim (owner 9, 2026-10-11).

The unit marked ``printing`` is decided by its ``print_start`` action's answer on the record, read on
EVENTS — never a clock. These tests drive the REAL record (``printer_action`` over the test database)
through the events that answer a start, then apply the answer:

* reached (the dispatched job reported, however late) ⇒ the claim stands, the lease is released;
* refused (a fail ACK) / void (a later session began unacknowledged) / taken over (another job) ⇒ the
  unit goes back to the queue next in line, ONCE, with the right cause;
* across a restart: the rehydrated start is answered at the printer's first fresh report and the
  session start applies it; a claim with no start on the record is judged against that report;
* the watch decides nothing: it shows the UI phases and, at the start's Phase A bound, rebuilds a
  half-broken session ONCE.

Production's start watchdog as the record's self-heal bounds (R17/R18, 2026-10-12, operator ruling: "if it
was self heal, it should stay self heal"), driven on the record's clock (``printer_action._now``) and the
printer's online seconds:

* SLOW FIRMWARE — an event after the old bound still decides: an H2D at FINISH with our id for 50 s then
  PREPARE is reached; an ACK at 80 s with the job active at 85 s is reached;
* BOUNDS — nothing by 90 s returns the unit ``start_unanswered`` (a session rebuild only when unacked and the
  file was not taken); landed at IDLE and never active by 180 s after the landing returns it
  ``start_not_active`` with no rebuild; an eject start at IDLE with our id keeps its 180 s bound;
* LATE — an answer after the bound returned the unit touches no claim and no lease;
* RESTART — a rehydrated outstanding start re-arms through its factory and returns with no watch alive;
* OFFLINE — time with no MQTT session is not counted;
* LIVENESS — a normal dispatch completes and is never returned.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_action import PrinterAction
from backend.app.services import dispatch_start, printer_action, printer_events
from backend.app.services.bambu_mqtt import CommandAck, PrinterState
from backend.app.services.command_outcome import Answer, Reading
from backend.app.services.eject import remote as eject_remote
from backend.app.services.plate_occupancy import Evidence, plate_occupancy
from backend.app.services.printer_events import (
    ActionAnswered,
    CommandAcked,
    SelfHealDue,
    SessionLost,
    StatusReported,
)
from backend.app.services.printer_manager import printer_manager
from backend.tests._fixtures.clock import FakeClock

pytestmark = pytest.mark.asyncio

_PID = 7
_MINTED = "424242"
_T0 = datetime(2026, 10, 12, 8, 0, 0)
_LOG = "backend.app.services.dispatch_start"


@dataclass
class _FakeClient:
    state: PrinterState = field(default_factory=PrinterState)
    published: list[tuple[str, str]] = field(default_factory=list)
    accept: bool = True
    last_dispatch_subtask_id: str | None = None
    reconnects: list[str] = field(default_factory=list)

    def start_print(self, filename: str, *, sequence_id: str, **options: object) -> bool:
        self.last_dispatch_subtask_id = _MINTED
        self.published.append((f"start:{filename}", sequence_id))
        return self.accept

    def stop_print(self, *, sequence_id: str) -> bool:
        self.published.append(("stop", sequence_id))
        return self.accept

    def ack_for(self, sequence_id: str) -> CommandAck | None:
        for ack in reversed(tuple(self.state.command_acks)):
            if ack.sequence_id == sequence_id:
                return ack
        return None

    def force_reconnect_stale_session(self, reason: str) -> None:
        self.reconnects.append(reason)

    def feed(self):  # pragma: no cover - not read here
        return None


def _state(*, state: str = "FINISH", subtask: str | None = "900", epoch: int = 1, gcode_file: str | None = "old.3mf"):
    st = PrinterState()
    st.connected = True
    st.connection_epoch = epoch
    st.report_epoch = epoch
    st.state = state
    st.subtask_id = subtask
    st.gcode_file = gcode_file
    return st


async def _drain() -> None:
    for _ in range(6):
        pending = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and (task.get_name() or "").startswith("printer-action")
        ]
        if not pending:
            await asyncio.sleep(0)
            continue
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.fixture(autouse=True)
async def _fresh():
    printer_action._reset_state()
    plate_occupancy.reset_for_tests()
    dispatch_start._landed_armed.clear()
    yield
    await _drain()
    printer_action._reset_state()
    plate_occupancy.reset_for_tests()
    dispatch_start._landed_armed.clear()


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    """The record's clock (``printer_action._now``), stepped by the test: seconds past ``_T0``."""
    fake = FakeClock(start=0.0)
    monkeypatch.setattr(printer_action, "_now", lambda: _T0 + timedelta(seconds=fake.t))
    return fake


@pytest.fixture
def hub() -> Iterator[list[SelfHealDue]]:
    """The dispatch lane's SelfHealDue subscription, as ``printer_event_wiring`` makes it — plus a record of
    every SelfHealDue the record published."""
    heard: list[SelfHealDue] = []
    unsubscribes = [
        printer_events.subscribe(SelfHealDue, heard.append),
        printer_events.subscribe(SelfHealDue, dispatch_start.on_self_heal_due),
    ]
    yield heard
    for unsubscribe in unsubscribes:
        unsubscribe()


@pytest.fixture
def client(monkeypatch, own_session_factory) -> _FakeClient:
    from backend.app.core import database as core_db

    monkeypatch.setattr(core_db, "async_session", own_session_factory)
    fake = _FakeClient(state=_state())
    monkeypatch.setattr(printer_manager, "get_client", lambda pid: fake if pid == _PID else None)
    monkeypatch.setattr(printer_manager, "get_status", lambda pid: fake.state if pid == _PID else None)
    return fake


async def _claim(own_session_factory) -> int:
    """A unit claimed ``printing`` on the printer, with its dispatch lease committed."""
    async with own_session_factory() as db:
        item = PrintQueueItem(
            printer_id=_PID,
            status="printing",
            first_article=False,
            plate_id=1,
            position=1,
            dispatch_subtask_id=_MINTED,
        )
        db.add(item)
        await db.commit()
        await db.refresh(item)
        item_id = item.id
    lease = plate_occupancy.claim_for_dispatch(_PID, item_id, ev=Evidence(live_state="FINISH"))
    assert plate_occupancy.commit_dispatch(_PID, lease) is None
    return item_id


async def _start(item_id: int) -> printer_action.Sent:
    """The dispatcher's send (``dispatch_start.send_start``): Phase A's bound armed at the send."""
    sent = await dispatch_start.send_start(
        _PID, printer_action.print_start("unit.3mf", plate_id=1), queue_item_id=item_id
    )
    await _drain()
    return sent


def _ack(sent: printer_action.Sent) -> None:
    printer_action.on_command_acked(
        CommandAcked(
            printer_id=_PID,
            ack=CommandAck(command="project_file", sequence_id=sent.sequence_id, result="success", reason=None, at=0.0),
        )
    )


def _push(client: _FakeClient, *, state: str, subtask: str | None) -> None:
    """One status push as the hub delivers it: the record reads it, then the dispatch's landing check."""
    client.state.state = state
    client.state.subtask_id = subtask
    event = StatusReported(printer_id=_PID, state=client.state)
    printer_action.on_status_reported(event)
    dispatch_start.on_status_reported(event)


async def _run(client: _FakeClient, clock: FakeClock, seconds: int, *, state: str, subtask: str | None) -> None:
    """``seconds`` of 1 Hz pushes on the record's clock, then let the record's writes and publishes land."""
    for _ in range(seconds):
        clock.advance(1.0)
        _push(client, state=state, subtask=subtask)
    await _drain()


async def _online(client: _FakeClient) -> None:
    """The printer's first fresh report in this process — the record's online clock starts here."""
    await printer_action.reread_session(_PID, client.state.connection_epoch)


async def _status_of(own_session_factory, item_id: int) -> str:
    async with own_session_factory() as db:
        return (await db.execute(select(PrintQueueItem.status).where(PrintQueueItem.id == item_id))).scalar_one()


async def _apply(sent: printer_action.Sent) -> Answer:
    """The hub's delivery of the record's final answer, as ``printer_event_wiring`` wires it."""
    answer = await printer_action.await_outcome(sent)
    await dispatch_start.on_action_answered(
        ActionAnswered(
            printer_id=_PID,
            action_id=sent.action_id,
            command="print_start",
            actor=printer_action.ACTOR_FARM,
            incident_id=None,
            answer=answer.value,
            reason=await printer_action.reason_of(sent.action_id),
        )
    )
    return answer


def _report(client: _FakeClient, *, state: str, subtask: str | None) -> None:
    client.state.state = state
    client.state.subtask_id = subtask
    printer_action.on_status_reported(StatusReported(printer_id=_PID, state=client.state))


class TestTheStartsAnswerDecidesTheClaim:
    async def test_a_reached_start_keeps_the_claim_and_releases_the_lease(self, client, own_session_factory):
        item_id = await _claim(own_session_factory)
        sent = await _start(item_id)

        _report(client, state="PREPARE", subtask=_MINTED)

        assert await _apply(sent) is Answer.REACHED
        assert await _status_of(own_session_factory, item_id) == "printing"
        assert plate_occupancy.snapshot(_PID).lease_unit_id is None

    async def test_slow_firmware_a_start_reported_after_130_pushes_is_reached_never_requeued(
        self, client, own_session_factory
    ):
        """The H2D can sit at FINISH for minutes after project_file: nothing answers the start until the
        printer reports the job, and nothing requeues it meanwhile."""
        item_id = await _claim(own_session_factory)
        sent = await _start(item_id)

        for _ in range(130):
            _report(client, state="FINISH", subtask="900")
        assert printer_action.outstanding(sent.action_id)
        assert await _status_of(own_session_factory, item_id) == "printing"
        assert plate_occupancy.snapshot(_PID, Evidence(live_state="FINISH")).lease_active is True

        _report(client, state="RUNNING", subtask=_MINTED)
        assert await _apply(sent) is Answer.REACHED
        assert await _status_of(own_session_factory, item_id) == "printing"

    async def test_a_fail_ack_requeues_the_unit_once(self, client, own_session_factory, caplog):
        item_id = await _claim(own_session_factory)
        sent = await _start(item_id)

        printer_action.on_command_acked(
            CommandAcked(
                printer_id=_PID,
                ack=CommandAck(
                    command="project_file", sequence_id=sent.sequence_id, result="fail", reason="busy", at=0.0
                ),
            )
        )
        with caplog.at_level("WARNING"):
            assert await _apply(sent) is Answer.REFUSED
        assert await _status_of(own_session_factory, item_id) == "pending"
        assert "cause=start_refused" in caplog.text
        assert plate_occupancy.snapshot(_PID).lease_unit_id is None

        # Applying the same answer again is a no-op: the return is a conditional transition.
        caplog.clear()
        with caplog.at_level("WARNING"):
            await _apply(sent)
        assert "returned to the queue" not in caplog.text
        assert await _status_of(own_session_factory, item_id) == "pending"

    async def test_an_unacknowledged_start_at_a_new_session_is_void_and_requeued(self, client, own_session_factory):
        item_id = await _claim(own_session_factory)
        sent = await _start(item_id)

        client.state = _state(state="IDLE", subtask=None, epoch=2)
        await printer_action.reread_session(_PID, 2)

        assert await _apply(sent) is Answer.VOID
        assert await _status_of(own_session_factory, item_id) == "pending"  # the row claim
        assert plate_occupancy.snapshot(_PID).lease_unit_id is None  # the printer claim
        assert _PID not in plate_occupancy.printers_with_lease_or_eject()
        # And the plate was never written — un-making a dispatch is nothing more.
        assert plate_occupancy.is_plate_occupied(_PID) is False

    async def test_another_job_taking_the_printer_requeues_the_unit(self, client, own_session_factory, caplog):
        item_id = await _claim(own_session_factory)
        sent = await _start(item_id)

        _report(client, state="RUNNING", subtask="777")

        with caplog.at_level("WARNING"):
            assert await _apply(sent) is Answer.TAKEN_OVER
        assert await _status_of(own_session_factory, item_id) == "pending"
        assert "cause=start_taken_over" in caplog.text

    async def test_a_persons_stop_of_an_unanswered_start_returns_the_unit(self, client, own_session_factory):
        """A person's act decides: their stop ends a dispatch the printer never reported, so the unit goes
        back instead of waiting on a start nobody will answer."""
        item_id = await _claim(own_session_factory)
        sent = await _start(item_id)

        await printer_action.send(_PID, printer_action.stop(), actor=printer_action.ACTOR_OPERATOR)

        assert await _apply(sent) is Answer.TAKEN_OVER
        assert await _status_of(own_session_factory, item_id) == "pending"

    async def test_another_commands_answer_is_not_a_starts(self, client, own_session_factory):
        item_id = await _claim(own_session_factory)
        await dispatch_start.on_action_answered(
            ActionAnswered(
                printer_id=_PID, action_id=1, command="resume", actor="farm", incident_id=None, answer="void"
            )
        )
        assert await _status_of(own_session_factory, item_id) == "printing"


class TestTheRestartMatrix:
    async def _restart(self, own_session_factory) -> None:
        printer_action._reset_state()
        async with own_session_factory() as db:
            await printer_action.rehydrate(db)

    async def test_an_unacked_start_across_a_restart_is_void_at_the_first_report_and_released(
        self, client, own_session_factory
    ):
        item_id = await _claim(own_session_factory)
        await _start(item_id)
        await self._restart(own_session_factory)

        client.state = _state(state="IDLE", subtask=None, epoch=2)
        await printer_action.reread_session(_PID, 2)
        await _drain()
        assert await dispatch_start.reevaluate_session(_PID) == 1
        assert await _status_of(own_session_factory, item_id) == "pending"

    async def test_a_start_the_printer_ran_across_a_restart_keeps_its_claim(self, client, own_session_factory):
        item_id = await _claim(own_session_factory)
        await _start(item_id)
        await self._restart(own_session_factory)

        client.state = _state(state="RUNNING", subtask=_MINTED, epoch=2)
        await printer_action.reread_session(_PID, 2)
        await _drain()
        assert await dispatch_start.reevaluate_session(_PID) == 0
        assert await _status_of(own_session_factory, item_id) == "printing"

    async def test_an_acknowledged_start_across_a_restart_waits_for_its_event(self, client, own_session_factory):
        item_id = await _claim(own_session_factory)
        sent = await _start(item_id)
        printer_action.on_command_acked(
            CommandAcked(
                printer_id=_PID,
                ack=CommandAck(
                    command="project_file", sequence_id=sent.sequence_id, result="success", reason=None, at=0.0
                ),
            )
        )
        await _drain()
        await self._restart(own_session_factory)

        client.state = _state(state="FINISH", subtask="900", epoch=2)
        await printer_action.reread_session(_PID, 2)
        await _drain()
        assert printer_action.outstanding(sent.action_id)
        assert await dispatch_start.reevaluate_session(_PID) == 0
        assert await _status_of(own_session_factory, item_id) == "printing"

    @pytest.mark.parametrize(
        ("state", "subtask", "released"),
        [("IDLE", None, True), ("RUNNING", "999", False), ("FINISH", _MINTED, False), ("PAUSE", None, False)],
    )
    async def test_a_claim_with_no_start_on_the_record_is_judged_at_the_first_report(
        self, client, own_session_factory, state, subtask, released
    ):
        item_id = await _claim(own_session_factory)
        client.state = _state(state=state, subtask=subtask, epoch=2)

        assert await dispatch_start.reevaluate_session(_PID) == int(released)
        assert await _status_of(own_session_factory, item_id) == ("pending" if released else "printing")


class TestTheWatchDecidesNothing:
    async def test_it_shows_preparing_then_printing_and_exits(self, client, own_session_factory, monkeypatch):
        monkeypatch.setattr(dispatch_start, "_WATCH_POLL_S", 0.0)
        phases: list[str] = []
        monkeypatch.setattr(
            dispatch_start.dispatch_progress, "emit_queue_item_status", lambda **kw: phases.append(kw["phase"])
        )
        item_id = await _claim(own_session_factory)
        sent = await _start(item_id)
        client.state.state = "PREPARE"
        watch = asyncio.create_task(
            dispatch_start.watch_start(sent, item_id=item_id, batch_id=None, pre_gcode_file=None)
        )
        for _ in range(20):
            await asyncio.sleep(0)
        assert phases == ["preparing"]

        _report(client, state="RUNNING", subtask=_MINTED)
        await asyncio.wait_for(watch, 2.0)
        assert phases == ["preparing", "printing"]
        assert client.reconnects == []

    @pytest.mark.parametrize(
        ("acked", "taken_file", "rebuilds"),
        [(False, False, 1), (True, False, 0), (False, True, 0)],
        ids=["unacked-file-not-taken", "acked", "file-taken"],
    )
    async def test_at_the_phase_a_bound_a_half_broken_session_is_rebuilt_once(
        self, client, own_session_factory, monkeypatch, clock, hub, acked, taken_file, rebuilds
    ):
        """#887/#936: a start unacknowledged at its Phase A bound on a printer that never took the file is a
        half-broken session, rebuilt ONCE (read through the record: its bound, its reading). An ACK, or a
        printer parsing the file (#1150: a reconnect mid-parse is 0500_4003), never rebuilds it."""
        monkeypatch.setattr(dispatch_start, "_WATCH_POLL_S", 0.0)
        await _online(client)
        item_id = await _claim(own_session_factory)
        sent = await _start(item_id)
        if acked:
            _ack(sent)
        if taken_file:
            client.state.gcode_file = "unit.3mf"
        watch = asyncio.create_task(
            dispatch_start.watch_start(sent, item_id=item_id, batch_id=None, pre_gcode_file="old.3mf")
        )
        await _run(client, clock, 89, state="FINISH", subtask="900")
        for _ in range(10):
            await asyncio.sleep(0)
        assert client.reconnects == []
        await _run(client, clock, 2, state="FINISH", subtask="900")
        await asyncio.wait_for(watch, 2.0)
        assert len(client.reconnects) == rebuilds

        # The rebuilt session's VOID finds the unit already returned: late — nothing is touched twice.
        client.state = _state(state="IDLE", subtask=None, epoch=2)
        await printer_action.reread_session(_PID, 2)
        await _drain()
        assert await _status_of(own_session_factory, item_id) == "pending"
        assert len(client.reconnects) == rebuilds


class TestTheSlowFirmware:
    """An event after production's old bound still decides: the start is reached, never returned."""

    async def test_an_h2d_finish_with_our_id_for_50s_then_prepare_is_reached(
        self, client, own_session_factory, clock, hub
    ):
        """#1078: the H2D sits at FINISH ~50 s after taking project_file, already reporting our job id."""
        await _online(client)
        item_id = await _claim(own_session_factory)
        sent = await _start(item_id)

        await _run(client, clock, 50, state="FINISH", subtask=_MINTED)
        assert printer_action.reading_of(sent.action_id) is Reading.LANDED
        await _run(client, clock, 45, state="FINISH", subtask=_MINTED)  # past Phase A's 90 s: landed, not returned
        assert await _status_of(own_session_factory, item_id) == "printing"

        _push(client, state="PREPARE", subtask=_MINTED)
        assert await _apply(sent) is Answer.REACHED
        assert await _status_of(own_session_factory, item_id) == "printing"
        assert plate_occupancy.snapshot(_PID).lease_unit_id is None
        assert hub == []

    async def test_an_ack_at_80s_and_the_job_active_at_85s_is_reached(self, client, own_session_factory, clock, hub):
        await _online(client)
        item_id = await _claim(own_session_factory)
        sent = await _start(item_id)

        await _run(client, clock, 80, state="FINISH", subtask="900")
        _ack(sent)
        await _run(client, clock, 5, state="RUNNING", subtask=_MINTED)
        assert await _apply(sent) is Answer.REACHED

        await _run(client, clock, 200, state="RUNNING", subtask=_MINTED)
        assert await _status_of(own_session_factory, item_id) == "printing"
        assert hub == []


class TestTheStartBounds:
    """Production ``_watchdog_print_start`` at 337f4954, restored as the record's bounds."""

    async def test_nothing_by_90s_returns_the_unit_start_unanswered(
        self, client, own_session_factory, clock, hub, caplog
    ):
        await _online(client)
        item_id = await _claim(own_session_factory)
        sent = await _start(item_id)

        await _run(client, clock, 89, state="FINISH", subtask="900")
        assert await _status_of(own_session_factory, item_id) == "printing"
        with caplog.at_level(logging.WARNING, logger=_LOG):
            await _run(client, clock, 2, state="FINISH", subtask="900")
        assert [event.bound_name for event in hub] == ["_START_SELF_HEAL"]
        assert await _status_of(own_session_factory, item_id) == "pending"
        assert "cause=start_unanswered" in caplog.text
        assert plate_occupancy.snapshot(_PID).lease_unit_id is None
        # The record is never answered by the clock: the start stays outstanding for a late event.
        assert printer_action.outstanding(sent.action_id)
        async with own_session_factory() as db:
            row = (await db.execute(select(PrinterAction).where(PrinterAction.id == sent.action_id))).scalar_one()
        assert row.bound_passed_at is not None and row.answered_at is None

    async def test_landed_at_idle_and_never_active_by_180s_returns_it_start_not_active_with_no_rebuild(
        self, client, own_session_factory, monkeypatch, clock, hub, caplog
    ):
        """#1678: the printer took the file and stays IDLE. Phase B counts from the LANDING, so Phase A's 90 s
        passes without a return; no session rebuild (#1150), even unacknowledged with the file unchanged."""
        monkeypatch.setattr(dispatch_start, "_WATCH_POLL_S", 0.0)
        await _online(client)
        item_id = await _claim(own_session_factory)
        sent = await _start(item_id)
        watch = asyncio.create_task(
            dispatch_start.watch_start(sent, item_id=item_id, batch_id=None, pre_gcode_file="old.3mf")
        )

        await _run(client, clock, 10, state="FINISH", subtask="900")
        await _run(client, clock, 1, state="IDLE", subtask=_MINTED)  # the landing, at 11 s
        await _run(client, clock, 178, state="IDLE", subtask=_MINTED)  # 189 s from the send, 178 s landed
        assert await _status_of(own_session_factory, item_id) == "printing"
        assert hub == []
        with caplog.at_level(logging.WARNING, logger=_LOG):
            await _run(client, clock, 2, state="IDLE", subtask=_MINTED)
        await asyncio.wait_for(watch, 2.0)
        assert [event.bound_name for event in hub] == ["_START_ACTIVE_SELF_HEAL"]
        assert await _status_of(own_session_factory, item_id) == "pending"
        assert "cause=start_not_active" in caplog.text
        assert client.reconnects == []

    async def test_an_eject_start_at_idle_with_our_id_is_retired_by_its_180s_bound(self, client, clock):
        """The capsule 8 finding: an eject start is reached only on an active word or its terminal (FINISH /
        FAILED), so one landed at IDLE stays outstanding and its start bound fires — the SelfHealDue that
        ``eject.remote.on_self_heal_due`` retires the eject on (the plate stays gated)."""
        heard: list[SelfHealDue] = []
        unsubscribe = printer_events.subscribe(SelfHealDue, heard.append)
        try:
            await _online(client)
            sent = await printer_action.send(
                _PID,
                printer_action.eject_start("eject.3mf", plate_id=1),
                actor=printer_action.ACTOR_FARM,
                bound=eject_remote._EJECT_START_SELF_HEAL,
            )
            await _run(client, clock, 179, state="IDLE", subtask=_MINTED)
            assert printer_action.reading_of(sent.action_id) is Reading.LANDED
            assert heard == []
            await _run(client, clock, 2, state="IDLE", subtask=_MINTED)
            assert [(e.command, e.bound_name) for e in heard] == [("eject_start", "_EJECT_START_SELF_HEAL")]
            assert printer_action.outstanding(sent.action_id)
        finally:
            unsubscribe()

    async def test_offline_time_is_not_counted(self, client, own_session_factory, clock, hub):
        await _online(client)
        item_id = await _claim(own_session_factory)
        sent = await _start(item_id)
        _ack(sent)  # an acknowledged start survives the new session (the firmware's queue survives a blip)

        await _run(client, clock, 60, state="FINISH", subtask="900")
        printer_action.on_session_lost(SessionLost(printer_id=_PID))
        clock.advance(600)
        client.state = _state(state="FINISH", subtask="900", epoch=2)
        await printer_action.reread_session(_PID, 2)
        await _run(client, clock, 25, state="FINISH", subtask="900")  # 85 s online
        assert await _status_of(own_session_factory, item_id) == "printing"
        assert hub == []
        await _run(client, clock, 6, state="FINISH", subtask="900")  # 91 s online
        assert await _status_of(own_session_factory, item_id) == "pending"


class TestTheLateAnswer:
    """[AR-H5]: an answer after the bound returned the unit touches NO claim and NO lease — the lease may be
    a new dispatch's — and the print it reached is the FOREIGN path's (no ``printing`` unit carries it)."""

    async def test_a_start_reached_after_its_return_touches_no_claim_and_no_lease(
        self, client, own_session_factory, clock, hub, caplog
    ):
        await _online(client)
        item_id = await _claim(own_session_factory)
        sent = await _start(item_id)
        await _run(client, clock, 91, state="FINISH", subtask="900")
        assert await _status_of(own_session_factory, item_id) == "pending"

        # A new dispatch claims the printer.
        async with own_session_factory() as db:
            other = PrintQueueItem(printer_id=_PID, status="printing", first_article=False, plate_id=1, position=2)
            db.add(other)
            await db.commit()
            await db.refresh(other)
            other_id = other.id
        lease = plate_occupancy.claim_for_dispatch(_PID, other_id, ev=Evidence(live_state="FINISH"))
        assert plate_occupancy.commit_dispatch(_PID, lease) is None

        # The printer starts the OLD job late.
        _push(client, state="RUNNING", subtask=_MINTED)
        with caplog.at_level(logging.WARNING, logger=_LOG):
            assert await _apply(sent) is Answer.REACHED
        assert "FOREIGN" in caplog.text
        assert plate_occupancy.snapshot(_PID).lease_unit_id == other_id  # the new dispatch's lease stands
        assert await _status_of(own_session_factory, item_id) == "pending"  # no claim re-made
        assert await _status_of(own_session_factory, other_id) == "printing"

    async def test_a_late_void_never_returns_a_redispatched_unit(self, client, own_session_factory, clock, hub):
        """The returned unit was dispatched again (a NEWER start names it); the old start's VOID touches it not."""
        await _online(client)
        item_id = await _claim(own_session_factory)
        sent = await _start(item_id)
        await _run(client, clock, 91, state="FINISH", subtask="900")
        assert await _status_of(own_session_factory, item_id) == "pending"

        async with own_session_factory() as db:
            row = (await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one()
            row.status = "printing"
            await db.commit()
        lease = plate_occupancy.claim_for_dispatch(_PID, item_id, ev=Evidence(live_state="FINISH"))
        assert plate_occupancy.commit_dispatch(_PID, lease) is None
        newer = await _start(item_id)

        client.state = _state(state="IDLE", subtask=None, epoch=2)
        await printer_action.reread_session(_PID, 2)  # the old start (unacked) is VOID — late
        await _drain()
        await dispatch_start.on_action_answered(
            ActionAnswered(
                printer_id=_PID,
                action_id=sent.action_id,
                command="print_start",
                actor="farm",
                incident_id=None,
                answer="void",
            )
        )
        assert await _status_of(own_session_factory, item_id) == "printing"
        assert plate_occupancy.snapshot(_PID).lease_unit_id == item_id
        assert newer.action_id != sent.action_id


class TestTheRestart:
    async def _restart(self, own_session_factory) -> None:
        printer_action._reset_state()  # also drops the factories: the composition root registers them again
        dispatch_start._landed_armed.clear()
        async with own_session_factory() as db:
            await printer_action.rehydrate(db)
        printer_action.register_bound_factory("print_start", dispatch_start.rehydrated_start_bound)

    async def test_a_rehydrated_outstanding_start_returns_at_its_bound_with_no_watch_alive(
        self, client, own_session_factory, clock, hub
    ):
        await _online(client)
        item_id = await _claim(own_session_factory)
        sent = await _start(item_id)
        _ack(sent)
        await _drain()
        await _run(client, clock, 30, state="FINISH", subtask="900")
        await self._restart(own_session_factory)

        clock.advance(120)  # the restart's downtime: not online in this process
        client.state = _state(state="FINISH", subtask="900", epoch=2)
        await printer_action.reread_session(_PID, 2)
        await _drain()
        assert await dispatch_start.reevaluate_session(_PID) == 0  # left to its event
        await _run(client, clock, 89, state="FINISH", subtask="900")
        assert await _status_of(own_session_factory, item_id) == "printing"
        await _run(client, clock, 2, state="FINISH", subtask="900")  # 90 s from the first fresh report
        assert [event.bound_name for event in hub] == ["_START_SELF_HEAL"]
        assert await _status_of(own_session_factory, item_id) == "pending"

    async def test_a_rehydrated_start_that_landed_counts_phase_b_from_the_landing(
        self, client, own_session_factory, clock, hub
    ):
        await _online(client)
        item_id = await _claim(own_session_factory)
        await _start(item_id)
        await self._restart(own_session_factory)

        client.state = _state(state="IDLE", subtask=_MINTED, epoch=2)
        await printer_action.reread_session(_PID, 2)  # unacked but landed: never voided
        await _drain()
        await _run(client, clock, 120, state="IDLE", subtask=_MINTED)
        assert await _status_of(own_session_factory, item_id) == "printing"
        await _run(client, clock, 62, state="IDLE", subtask=_MINTED)
        assert [event.bound_name for event in hub] == ["_START_ACTIVE_SELF_HEAL"]
        assert await _status_of(own_session_factory, item_id) == "pending"

    async def test_a_bound_that_passed_before_the_restart_is_taken_at_the_session_start(
        self, client, own_session_factory, clock
    ):
        """A restart between the bound's pass (``bound_passed_at``) and the return: the session start takes
        the fallback — no SelfHealDue will come again for it."""
        await _online(client)
        item_id = await _claim(own_session_factory)
        sent = await _start(item_id)
        _ack(sent)
        await _run(client, clock, 91, state="FINISH", subtask="900")  # no lane subscribed: nothing returned it
        assert await _status_of(own_session_factory, item_id) == "printing"
        await self._restart(own_session_factory)

        client.state = _state(state="FINISH", subtask="900", epoch=2)
        await printer_action.reread_session(_PID, 2)
        await _drain()
        assert await dispatch_start.reevaluate_session(_PID) == 1
        assert await _status_of(own_session_factory, item_id) == "pending"


class TestLiveness:
    async def test_a_normal_dispatch_completes_and_is_never_returned(self, client, own_session_factory, clock, hub):
        await _online(client)
        item_id = await _claim(own_session_factory)
        sent = await _start(item_id)
        _ack(sent)
        await _run(client, clock, 3, state="FINISH", subtask="900")
        await _run(client, clock, 20, state="PREPARE", subtask=_MINTED)
        assert await _apply(sent) is Answer.REACHED
        assert plate_occupancy.snapshot(_PID).lease_unit_id is None
        await _run(client, clock, 600, state="RUNNING", subtask=_MINTED)
        assert hub == []
        assert await _status_of(own_session_factory, item_id) == "printing"
        assert dispatch_start._landed_armed == {}
