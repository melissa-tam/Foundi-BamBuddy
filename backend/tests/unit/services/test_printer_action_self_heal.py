"""The record's self-heal bound (``services/printer_action``, capsule R-C0 of the 2026-10-12 plan).

Raymond, 2026-10-12: "if it was self heal, it should stay self heal". The printer's own events decide
every outcome; a wait production bounded keeps a NAMED bound (:class:`printer_action.SelfHealBound`, with
the measured witness behind its number), counted in the printer's ONLINE seconds by the record. When it
passes with no event, the lane takes its fallback — and the action stays outstanding, so a late answer
still lands. Driven through the record's public entry points with a fake client in the registry, the
record's clock (``printer_action._now``) stepped by hand, and the rows read back from the test database:

* the online clock: a lost session's gap is excluded, an anchor older than the printer's first fresh
  report counts from it, no fresh report is 0 s;
* a bound passes ONCE: ``bound_passed_at`` written once, one ``[self-heal]`` line, one ``SelfHealDue``,
  the bounded waiter gets ``Unanswered``, the action still outstanding;
* a late answer after the pass still answers, persists and publishes ``ActionAnswered``; an answer before
  the bound passes nothing;
* offline time never counts toward a bound;
* a REHYDRATED action gets its family's bound from a registered factory (whichever was first) and fires
  ``SelfHealDue`` with no lane coroutine alive, driven only by status reports and ``check_bounds``;
* an AMS motion command's ``Unanswered.reading`` is the motion classifier's;
* the attention owner never pages an action whose bound passed, and ages one in online seconds;
* the session's end is handed over by the client on its disconnect edge, guarded.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from backend.app.models.printer_action import PrinterAction
from backend.app.services import farm_stall, printer_action, printer_events, printer_incidents
from backend.app.services.bambu_mqtt import CommandAck, PrinterState
from backend.app.services.command_outcome import Answer, Family, Reading
from backend.app.services.notification_service import notification_service
from backend.app.services.printer_events import (
    ActionAnswered,
    CommandAcked,
    SelfHealDue,
    SessionLost,
    StatusReported,
)
from backend.app.services.printer_manager import printer_manager
from backend.tests._fixtures.clock import FakeClock

_PID = 7
_T0 = datetime(2026, 10, 12, 8, 0, 0)
_BOUND = printer_action.SelfHealBound("_TEST_SELF_HEAL", 30.0, "a healthy answer measured at 12 s")
_LOG = "backend.app.services.printer_action"


@dataclass
class _FakeClient:
    """The registry's client, as far as the record reaches it: its state and the publishers it calls."""

    state: PrinterState = field(default_factory=PrinterState)
    published: list[tuple[str, str]] = field(default_factory=list)

    def _publish(self, name: str, seq: str) -> bool:
        self.published.append((name, seq))
        return True

    def resume_print(self, *, sequence_id: str) -> bool:
        return self._publish("resume", sequence_id)

    def stop_print(self, *, sequence_id: str) -> bool:
        return self._publish("stop", sequence_id)

    def ams_load_filament(self, tray: int, *, sequence_id: str) -> bool:
        return self._publish(f"load:{tray}", sequence_id)

    def ack_for(self, sequence_id: str) -> CommandAck | None:
        return None


def _live_state(*, state: str = "PAUSE", tray_now: int = 255, epoch: int = 1) -> PrinterState:
    st = PrinterState()
    st.connected = True
    st.connection_epoch = epoch
    st.report_epoch = epoch
    st.state = state
    st.tray_now = tray_now
    st.subtask_id = "900"
    return st


def _wall(clock: FakeClock) -> datetime:
    """The record's naive-UTC instant the clock stands at (``_T0`` plus its seconds)."""
    return _T0 + timedelta(seconds=clock.t)


@pytest.fixture(autouse=True)
async def _fresh_record() -> AsyncIterator[None]:
    printer_action._reset_state()
    yield
    await _drain()
    printer_action._reset_state()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """The record's clock (``printer_action._now``), stepped by the test: seconds past ``_T0``."""
    fake = FakeClock(start=0.0)
    monkeypatch.setattr(printer_action, "_now", lambda: _wall(fake))
    return fake


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, own_session_factory, clock: FakeClock) -> _FakeClient:
    from backend.app.core import database as core_db

    monkeypatch.setattr(core_db, "async_session", own_session_factory)
    fake = _FakeClient(state=_live_state())
    monkeypatch.setattr(printer_manager, "get_client", lambda pid: fake if pid == _PID else None)
    monkeypatch.setattr(printer_manager, "get_status", lambda pid: fake.state if pid == _PID else None)
    return fake


@pytest.fixture
def heard() -> Iterator[dict[type, list]]:
    """What the hub published: every ``SelfHealDue`` and ``ActionAnswered``."""
    seen: dict[type, list] = {SelfHealDue: [], ActionAnswered: []}
    unsubscribes = [printer_events.subscribe(kind, seen[kind].append) for kind in seen]
    yield seen
    for unsubscribe in unsubscribes:
        unsubscribe()


async def _drain() -> None:
    """Let the record's spawned writes and publishes land."""
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


async def _row(own_session_factory, action_id: int) -> PrinterAction:
    async with own_session_factory() as db:
        return (await db.execute(select(PrinterAction).where(PrinterAction.id == action_id))).scalar_one()


def _report(client: _FakeClient) -> None:
    printer_action.on_status_reported(StatusReported(printer_id=_PID, state=client.state))


def _ack(seq: str, command: str = "resume") -> CommandAcked:
    return CommandAcked(
        printer_id=_PID, ack=CommandAck(command=command, sequence_id=seq, result="success", reason=None, at=0.0)
    )


def _self_heal_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == _LOG and r.getMessage().startswith("[self-heal]")]


# --- the bound itself ---------------------------------------------------------------------------------


class TestTheBound:
    @pytest.mark.parametrize(
        ("name", "seconds", "witness"),
        [("", 30.0, "w"), ("  ", 30.0, "w"), ("_X", 30.0, ""), ("_X", 30.0, " "), ("_X", 0.0, "w"), ("_X", -1, "w")],
    )
    def test_a_bound_with_no_name_no_witness_or_no_length_is_refused(self, name, seconds, witness) -> None:
        with pytest.raises(ValueError):
            printer_action.SelfHealBound(name, seconds, witness)

    def test_a_bound_is_frozen(self) -> None:
        with pytest.raises(AttributeError):
            _BOUND.seconds = 1.0  # type: ignore[misc]


# --- the online clock ---------------------------------------------------------------------------------


class TestOnlineSeconds:
    async def test_a_printer_with_no_fresh_report_in_this_process_has_been_online_zero_seconds(
        self, client: _FakeClient, clock: FakeClock
    ) -> None:
        clock.advance(600)
        assert printer_action.online_seconds_since(_PID, _T0) == 0.0

    async def test_an_anchor_before_the_first_fresh_report_counts_from_the_first_fresh_report(
        self, client: _FakeClient, clock: FakeClock
    ) -> None:
        clock.advance(500)
        await printer_action.reread_session(_PID, 1)  # the process's first fresh report of the printer
        clock.advance(40)

        assert printer_action.online_seconds_since(_PID, _T0) == 40.0

    async def test_a_lost_session_is_excluded_until_the_next_sessions_first_fresh_report(
        self, client: _FakeClient, clock: FakeClock
    ) -> None:
        await printer_action.reread_session(_PID, 1)
        clock.advance(20)
        printer_action.on_session_lost(SessionLost(printer_id=_PID))
        clock.advance(300)
        assert printer_action.online_seconds_since(_PID, _T0) == 20.0  # offline: the clock stopped at the loss

        client.state = _live_state(epoch=2)
        await printer_action.reread_session(_PID, 2)
        clock.advance(10)

        assert printer_action.online_seconds_since(_PID, _T0) == 30.0
        # An anchor inside the gap counts from the session's return; one after it, from itself.
        assert printer_action.online_seconds_since(_PID, _T0 + timedelta(seconds=100)) == 10.0
        assert printer_action.online_seconds_since(_PID, _wall(clock) - timedelta(seconds=4)) == 4.0

    async def test_a_quiet_printer_is_still_online(self, client: _FakeClient, clock: FakeClock) -> None:
        """Offline is a lost MQTT session, never a quiet report."""
        await printer_action.reread_session(_PID, 1)
        clock.advance(900)  # no report at all

        assert printer_action.online_seconds_since(_PID, _T0) == 900.0

    async def test_a_session_lost_before_any_fresh_report_stops_nothing(
        self, client: _FakeClient, clock: FakeClock
    ) -> None:
        printer_action.on_session_lost(SessionLost(printer_id=_PID))
        clock.advance(10)
        await printer_action.reread_session(_PID, 1)
        clock.advance(5)

        assert printer_action.online_seconds_since(_PID, _T0) == 5.0
        assert printer_action._gaps.get(_PID) is None

    async def test_the_gap_memory_is_bounded_and_kept_while_an_anchor_reaches_it(
        self, client: _FakeClient, clock: FakeClock
    ) -> None:
        await printer_action.reread_session(_PID, 1)
        sent = await printer_action.send(_PID, printer_action.stop(), actor=printer_action.ACTOR_FARM)
        printer_action.on_command_acked(_ack(sent.sequence_id, command="stop"))  # outstanding across sessions
        for epoch in range(2, 2 + 3 * printer_action._GAP_KEEP):
            clock.advance(1)
            printer_action.on_session_lost(SessionLost(printer_id=_PID))
            clock.advance(1)
            client.state = _live_state(epoch=epoch)
            await printer_action.reread_session(_PID, epoch)
        # The outstanding stop's anchor reaches every gap: all kept, its online seconds exact.
        assert len(printer_action._gaps[_PID]) == 3 * printer_action._GAP_KEEP
        assert printer_action.online_seconds_since(_PID, _T0) == 3 * printer_action._GAP_KEEP

        client.state.state = "IDLE"  # the stop is taken: no anchor reaches the old gaps any more
        _report(client)
        clock.advance(1)
        printer_action.on_session_lost(SessionLost(printer_id=_PID))
        client.state = _live_state(state="IDLE", epoch=99)
        await printer_action.reread_session(_PID, 99)

        assert len(printer_action._gaps[_PID]) == printer_action._GAP_KEEP


# --- a bound on an action -----------------------------------------------------------------------------


class TestABoundPassesOnce:
    async def test_the_pass_writes_once_logs_once_publishes_once_and_answers_nothing(
        self, client: _FakeClient, clock: FakeClock, own_session_factory, heard, caplog
    ) -> None:
        await printer_action.reread_session(_PID, 1)
        sent = await printer_action.send(
            _PID, printer_action.resume(), actor=printer_action.ACTOR_FARM, incident_id=None, queue_item_id=None
        )
        waiter = asyncio.ensure_future(printer_action.await_outcome(sent, bound=_BOUND))
        await asyncio.sleep(0)
        clock.advance(29)
        _report(client)
        await asyncio.sleep(0)
        assert not waiter.done()

        caplog.set_level(logging.WARNING, logger=_LOG)
        clock.advance(2)
        _report(client)
        await _drain()

        unanswered = await waiter
        assert unanswered == printer_action.Unanswered("_TEST_SELF_HEAL", Reading.SENT)
        assert printer_action.outstanding(sent.action_id), "the bound never answers the action"
        row = await _row(own_session_factory, sent.action_id)
        assert row.bound_passed_at == _T0 + timedelta(seconds=31)
        assert row.answer is None and row.answered_at is None
        assert [(e.action_id, e.command, e.bound_name) for e in heard[SelfHealDue]] == [
            (sent.action_id, "resume", "_TEST_SELF_HEAL")
        ]
        assert _self_heal_lines(caplog) == [
            f"[self-heal] action {sent.action_id} printer={_PID} command=resume bound=_TEST_SELF_HEAL "
            "unanswered after 31s online (witness: a healthy answer measured at 12 s)"
        ]
        assert [a.bound_passed for a in printer_action.outstanding_actions()] == [True]

        # Later pushes and the attention owner's pass fire nothing again; the column keeps the first stamp.
        clock.advance(60)
        _report(client)
        printer_action.check_bounds()
        await _drain()
        assert len(heard[SelfHealDue]) == 1
        assert len(_self_heal_lines(caplog)) == 1
        assert (await _row(own_session_factory, sent.action_id)).bound_passed_at == _T0 + timedelta(seconds=31)
        # An await after the pass is Unanswered at once.
        assert await printer_action.await_outcome(sent, bound=_BOUND) == unanswered

    async def test_the_in_memory_reader_follows_the_outstanding_action_and_the_column_the_answered_one(
        self, client: _FakeClient, clock: FakeClock, own_session_factory
    ) -> None:
        """``bound_passed`` is THE in-memory read for an OUTSTANDING action (a lane awaiting its own send);
        once an event answers it the registry entry is gone and the row's column answers
        (``bound_passed_of``) — two readers, two lifetimes, never a second spelling of either."""
        await printer_action.reread_session(_PID, 1)
        sent = await printer_action.send(_PID, printer_action.resume(), actor=printer_action.ACTOR_FARM, bound=_BOUND)
        other = await printer_action.send(_PID, printer_action.resume(), actor=printer_action.ACTOR_FARM)
        assert printer_action.bound_passed(sent.action_id) is False
        assert printer_action.bound_passed(None) is False

        clock.advance(31)
        _report(client)
        await _drain()
        assert printer_action.bound_passed(sent.action_id) is True
        assert printer_action.bound_passed(other.action_id) is False  # no bound of its own

        client.state.state = "RUNNING"
        clock.advance(14)
        _report(client)
        await _drain()
        assert not printer_action.outstanding(sent.action_id)
        assert printer_action.bound_passed(sent.action_id) is False  # answered: memory holds it no longer
        async with own_session_factory() as db:
            assert await printer_action.bound_passed_of(db, sent.action_id) is True

    async def test_a_late_answer_after_the_pass_still_answers_persists_and_publishes(
        self, client: _FakeClient, clock: FakeClock, own_session_factory, heard
    ) -> None:
        """The slow-firmware case: the printer resumes at 45 s, past the 30 s bound — the lane already took
        its fallback, and the record still credits the resume."""
        await printer_action.reread_session(_PID, 1)
        sent = await printer_action.send(_PID, printer_action.resume(), actor=printer_action.ACTOR_FARM, bound=_BOUND)
        clock.advance(31)
        _report(client)
        await _drain()
        assert len(heard[SelfHealDue]) == 1

        clock.advance(14)
        client.state.state = "RUNNING"
        _report(client)
        await _drain()

        assert await printer_action.await_outcome(sent) is Answer.REACHED
        row = await _row(own_session_factory, sent.action_id)
        assert (row.answer, row.bound_passed_at) == ("reached", _T0 + timedelta(seconds=31))
        assert [(e.action_id, e.answer) for e in heard[ActionAnswered]] == [(sent.action_id, "reached")]
        assert not printer_action.outstanding(sent.action_id)

    async def test_an_answer_before_the_bound_passes_nothing(
        self, client: _FakeClient, clock: FakeClock, own_session_factory, heard, caplog
    ) -> None:
        await printer_action.reread_session(_PID, 1)
        sent = await printer_action.send(_PID, printer_action.resume(), actor=printer_action.ACTOR_FARM)
        waiter = asyncio.ensure_future(printer_action.await_outcome(sent, bound=_BOUND))
        await asyncio.sleep(0)
        clock.advance(12)
        client.state.state = "RUNNING"
        caplog.set_level(logging.WARNING, logger=_LOG)
        _report(client)
        clock.advance(60)
        printer_action.check_bounds()
        await _drain()

        assert await waiter is Answer.REACHED
        assert (await _row(own_session_factory, sent.action_id)).bound_passed_at is None
        assert heard[SelfHealDue] == []
        assert _self_heal_lines(caplog) == []

    async def test_offline_time_never_counts_toward_a_bound(
        self, client: _FakeClient, clock: FakeClock, own_session_factory, heard
    ) -> None:
        await printer_action.reread_session(_PID, 1)
        sent = await printer_action.send(_PID, printer_action.resume(), actor=printer_action.ACTOR_FARM, bound=_BOUND)
        printer_action.on_command_acked(_ack(sent.sequence_id))  # accepted: a new session does not void it
        clock.advance(20)
        printer_action.on_session_lost(SessionLost(printer_id=_PID))
        clock.advance(600)
        printer_action.check_bounds()
        await _drain()
        assert heard[SelfHealDue] == []

        client.state = _live_state(epoch=2)
        await printer_action.reread_session(_PID, 2)
        clock.advance(9)
        _report(client)
        await _drain()
        assert heard[SelfHealDue] == [], "20 s + 9 s online: inside the 30 s bound"

        clock.advance(2)
        _report(client)
        await _drain()
        assert len(heard[SelfHealDue]) == 1
        assert (await _row(own_session_factory, sent.action_id)).bound_passed_at == _wall(clock)

    async def test_an_unbounded_waiter_is_not_resolved_by_a_pass(self, client: _FakeClient, clock: FakeClock) -> None:
        """``await_outcome`` without a bound keeps today's contract: only an event resolves it."""
        await printer_action.reread_session(_PID, 1)
        sent = await printer_action.send(_PID, printer_action.resume(), actor=printer_action.ACTOR_FARM, bound=_BOUND)
        waiter = asyncio.ensure_future(printer_action.await_outcome(sent))
        await asyncio.sleep(0)
        clock.advance(31)
        _report(client)
        await asyncio.sleep(0)
        assert not waiter.done()

        client.state.state = "RUNNING"
        _report(client)
        assert await waiter is Answer.REACHED

    async def test_a_bound_armed_later_counts_from_its_own_anchor_and_a_passed_one_is_never_rearmed(
        self, client: _FakeClient, clock: FakeClock, heard
    ) -> None:
        await printer_action.reread_session(_PID, 1)
        sent = await printer_action.send(_PID, printer_action.resume(), actor=printer_action.ACTOR_FARM)
        clock.advance(100)
        assert printer_action.arm_bound(sent.action_id, _BOUND, anchor_at=_wall(clock)) is True
        clock.advance(29)
        _report(client)
        await _drain()
        assert heard[SelfHealDue] == []

        clock.advance(1)
        _report(client)
        await _drain()
        assert len(heard[SelfHealDue]) == 1
        assert printer_action.arm_bound(sent.action_id, _BOUND, anchor_at=_wall(clock)) is False
        assert printer_action.arm_bound(999_999, _BOUND, anchor_at=_wall(clock)) is False

    async def test_a_command_that_never_went_out_is_refused_never_unanswered(
        self, client: _FakeClient, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(printer_manager, "get_client", lambda pid: None)
        sent = await printer_action.send(_PID, printer_action.resume(), actor=printer_action.ACTOR_FARM, bound=_BOUND)

        assert await printer_action.await_outcome(sent, bound=_BOUND) is Answer.REFUSED


class TestBoundsOnANonActionAnchor:
    async def test_bound_due_counts_online_seconds_and_the_lanes_note_marks_the_action(
        self, client: _FakeClient, clock: FakeClock, own_session_factory, heard, caplog
    ) -> None:
        await printer_action.reread_session(_PID, 1)
        sent = await printer_action.send(_PID, printer_action.resume(), actor=printer_action.ACTOR_FARM)
        edge = _wall(clock)
        clock.advance(29)
        assert printer_action.bound_due(_PID, _BOUND, edge) is False
        clock.advance(1)
        assert printer_action.bound_due(_PID, _BOUND, edge) is True

        caplog.set_level(logging.WARNING, logger=_LOG)
        printer_action.note_bound_passed(_PID, _BOUND, lane="fault-edge", action_id=sent.action_id)
        await _drain()

        assert _self_heal_lines(caplog) == [
            f"[self-heal] action {sent.action_id} printer={_PID} lane=fault-edge bound=_TEST_SELF_HEAL "
            "unanswered after 30s online (witness: a healthy answer measured at 12 s)"
        ]
        assert (await _row(own_session_factory, sent.action_id)).bound_passed_at == _wall(clock)
        assert [a.bound_passed for a in printer_action.outstanding_actions()] == [True]
        assert heard[SelfHealDue] == [], "the lane acts itself"

    async def test_a_lanes_note_resolves_a_bounded_waiter_of_the_action(
        self, client: _FakeClient, clock: FakeClock
    ) -> None:
        await printer_action.reread_session(_PID, 1)
        sent = await printer_action.send(_PID, printer_action.resume(), actor=printer_action.ACTOR_FARM)
        long_bound = printer_action.SelfHealBound("_LONG_SELF_HEAL", 3600.0, "a measured hour-long cycle")
        waiter = asyncio.ensure_future(printer_action.await_outcome(sent, bound=long_bound))
        await asyncio.sleep(0)

        printer_action.note_bound_passed(_PID, _BOUND, lane="fault-edge", action_id=sent.action_id)

        assert await waiter == printer_action.Unanswered("_TEST_SELF_HEAL", Reading.SENT)
        assert printer_action.outstanding(sent.action_id)

    async def test_a_lanes_note_on_an_answered_action_says_so_and_writes_no_late_answer(
        self, client: _FakeClient, clock: FakeClock, own_session_factory, caplog
    ) -> None:
        """F13 (2026-10-16): a lane's bound passing on an action the printer ALREADY answered is no
        unanswered command. 014-H2S 2026-10-15: the swap round's resume (action 2699) was answered ``reached``
        at 02:16:43, and at 02:18:13 the reader's held-success fallback logged it "unanswered after 90s".
        The line now says ``bound passed (answered: reached)``, and ``bound_passed_at`` — "answered LATE" to its
        readers (``dispatch_claim``) — is never written for it."""
        await printer_action.reread_session(_PID, 1)
        sent = await printer_action.send(_PID, printer_action.resume(), actor=printer_action.ACTOR_FARM)
        client.state.state = "RUNNING"
        _report(client)  # the RESUME family is answered by RUNNING
        await _drain()
        assert not printer_action.outstanding(sent.action_id)
        clock.advance(90)

        caplog.set_level(logging.WARNING, logger=_LOG)
        printer_action.note_bound_passed(
            _PID, _BOUND, lane="spool_recovery.lever=resume swapped", action_id=sent.action_id
        )
        await _drain()

        assert _self_heal_lines(caplog) == [
            f"[self-heal] action {sent.action_id} printer={_PID} lane=spool_recovery.lever=resume swapped "
            "bound=_TEST_SELF_HEAL passed (answered: reached) after 30s online"
        ]
        row = await _row(own_session_factory, sent.action_id)
        assert (row.answer, row.bound_passed_at) == ("reached", None)

    async def test_a_note_with_no_action_only_logs(self, client: _FakeClient, clock: FakeClock, caplog) -> None:
        caplog.set_level(logging.WARNING, logger=_LOG)
        printer_action.note_bound_passed(_PID, _BOUND, lane="eject-start")
        assert _self_heal_lines(caplog) == [
            f"[self-heal] action - printer={_PID} lane=eject-start bound=_TEST_SELF_HEAL "
            "unanswered after 30s online (witness: a healthy answer measured at 12 s)"
        ]


# --- after a restart ----------------------------------------------------------------------------------


async def _restart(own_session_factory) -> None:
    """A deploy: every slot of process memory is gone, the registry is rebuilt from the rows."""
    printer_action._reset_state()
    async with own_session_factory() as db:
        await printer_action.rehydrate(db)


class TestARehydratedBound:
    @pytest.mark.parametrize("factory_first", [False, True], ids=["wired-after-rehydrate", "wired-before"])
    async def test_a_rehydrated_action_fires_with_no_lane_coroutine_alive(
        self, client: _FakeClient, clock: FakeClock, own_session_factory, heard, factory_first: bool
    ) -> None:
        """An accepted stop not taken when the farm restarts: no coroutine waits on it any more. The family's
        factory arms its bound — whichever of the rehydrate and the wiring ran first — and the record alone
        (status reports, the attention owner's pass) fires ``SelfHealDue``."""
        sent = await printer_action.send(_PID, printer_action.stop(), actor=printer_action.ACTOR_FARM, incident_id=None)
        printer_action.on_command_acked(_ack(sent.sequence_id, command="stop"))
        await _drain()
        offered: list[printer_action.OutstandingAction] = []

        def stop_bound(action: printer_action.OutstandingAction):
            offered.append(action)
            return _BOUND, action.sent_at

        if factory_first:
            printer_action._reset_state()
            printer_action.register_bound_factory(Family.STOP, stop_bound)
            async with own_session_factory() as db:
                await printer_action.rehydrate(db)
        else:
            await _restart(own_session_factory)
            printer_action.register_bound_factory(Family.STOP, stop_bound)

        client.state = _live_state(epoch=2)
        await printer_action.reread_session(_PID, 2)  # accepted: the new session does not void it
        assert printer_action.outstanding(sent.action_id)
        clock.advance(29)
        _report(client)
        await _drain()
        assert heard[SelfHealDue] == []
        assert [a.action_id for a in offered] == [sent.action_id]

        clock.advance(1)
        printer_action.check_bounds()  # the idle printer: the attention owner's pass, no push
        await _drain()

        assert [(e.action_id, e.command) for e in heard[SelfHealDue]] == [(sent.action_id, "stop")]
        assert (await _row(own_session_factory, sent.action_id)).bound_passed_at == _wall(clock)
        assert len(offered) == 1, "offered once"

    async def test_a_pass_recorded_before_the_restart_never_fires_again(
        self, client: _FakeClient, clock: FakeClock, own_session_factory, heard
    ) -> None:
        await printer_action.reread_session(_PID, 1)
        sent = await printer_action.send(_PID, printer_action.stop(), actor=printer_action.ACTOR_FARM, bound=_BOUND)
        printer_action.on_command_acked(_ack(sent.sequence_id, command="stop"))
        clock.advance(31)
        _report(client)
        await _drain()
        assert len(heard[SelfHealDue]) == 1

        await _restart(own_session_factory)
        printer_action.register_bound_factory(Family.STOP, lambda action: (_BOUND, action.sent_at))
        client.state = _live_state(epoch=2)
        await printer_action.reread_session(_PID, 2)
        clock.advance(600)
        _report(client)
        printer_action.check_bounds()
        await _drain()

        assert len(heard[SelfHealDue]) == 1
        assert [a.bound_passed for a in printer_action.outstanding_actions()] == [True]

    async def test_a_factory_that_raises_leaves_the_action_unbounded(
        self, client: _FakeClient, clock: FakeClock, own_session_factory, heard, caplog
    ) -> None:
        sent = await printer_action.send(_PID, printer_action.stop(), actor=printer_action.ACTOR_FARM)
        printer_action.on_command_acked(_ack(sent.sequence_id, command="stop"))
        await _drain()
        await _restart(own_session_factory)

        def broken(action):
            raise RuntimeError("factory bug")

        printer_action.register_bound_factory("stop", broken)
        client.state = _live_state(epoch=2)
        await printer_action.reread_session(_PID, 2)
        clock.advance(600)
        _report(client)
        await _drain()

        assert heard[SelfHealDue] == []
        assert printer_action.outstanding(sent.action_id)
        assert "bound factory raised" in caplog.text

    def test_a_factory_for_a_name_outside_the_vocabulary_is_refused(self) -> None:
        with pytest.raises(LookupError):
            printer_action.register_bound_factory("not_a_command", lambda action: None)

    def test_the_unregister_removes_the_factory(self) -> None:
        unregister = printer_action.register_bound_factory(Family.STOP, lambda action: None)
        unregister()
        assert Family.STOP not in printer_action._bound_factories


# --- the motion reading -------------------------------------------------------------------------------


class TestTheMotionReading:
    """An AMS motion command's ``Unanswered.reading`` is the motion classifier's (``command_outcome.classify``
    over the snapshot taken at the publish, the live one, and the observation folded since) — so no driver
    classifies on its own."""

    async def _load_into_an_empty_path(self, client: _FakeClient) -> printer_action.Sent:
        await printer_action.reread_session(_PID, 1)
        client.state.state = "IDLE"
        client.state.tray_now = 255
        return await printer_action.send(_PID, printer_action.load(2), actor=printer_action.ACTOR_FARM)

    async def test_an_accepted_load_that_never_moved_reads_held(self, client: _FakeClient, clock: FakeClock) -> None:
        sent = await self._load_into_an_empty_path(client)
        printer_action.on_command_acked(_ack(sent.sequence_id, command="ams_change_filament"))
        waiter = asyncio.ensure_future(printer_action.await_outcome(sent, bound=_BOUND))
        await asyncio.sleep(0)
        for _ in range(3):
            clock.advance(11)
            _report(client)

        assert await waiter == printer_action.Unanswered("_TEST_SELF_HEAL", "held")

    async def test_an_unacknowledged_load_that_never_moved_reads_no_movement(
        self, client: _FakeClient, clock: FakeClock
    ) -> None:
        sent = await self._load_into_an_empty_path(client)
        waiter = asyncio.ensure_future(printer_action.await_outcome(sent, bound=_BOUND))
        await asyncio.sleep(0)
        for _ in range(3):
            clock.advance(11)
            _report(client)

        assert await waiter == printer_action.Unanswered("_TEST_SELF_HEAL", "no_movement")

    async def test_a_load_whose_ams_moved_without_landing_reads_acted(
        self, client: _FakeClient, clock: FakeClock
    ) -> None:
        sent = await self._load_into_an_empty_path(client)
        waiter = asyncio.ensure_future(printer_action.await_outcome(sent, bound=_BOUND))
        await asyncio.sleep(0)
        clock.advance(10)
        client.state.tray_tar = 2  # the AMS moved toward the target and never landed it
        _report(client)
        clock.advance(10)
        client.state.tray_tar = 255
        _report(client)
        clock.advance(11)
        _report(client)

        assert await waiter == printer_action.Unanswered("_TEST_SELF_HEAL", "acted")

    async def test_a_rehydrated_motion_command_reads_the_records_reading(
        self, client: _FakeClient, clock: FakeClock, own_session_factory
    ) -> None:
        sent = await self._load_into_an_empty_path(client)
        printer_action.on_command_acked(_ack(sent.sequence_id, command="ams_change_filament"))
        await _drain()
        await _restart(own_session_factory)
        await printer_action.reread_session(_PID, 1)
        waiter = asyncio.ensure_future(printer_action.await_outcome(sent, bound=_BOUND))
        await asyncio.sleep(0)
        clock.advance(31)
        _report(client)

        assert await waiter == printer_action.Unanswered("_TEST_SELF_HEAL", Reading.ACKED)


# --- the attention owner ------------------------------------------------------------------------------


class TestTheAttentionOwner:
    @pytest.fixture
    def waiting_page(self):
        with patch.object(notification_service, "on_printer_waiting", new_callable=AsyncMock) as page:
            yield page

    @pytest.fixture(autouse=True)
    def _quiet_levels(self, monkeypatch: pytest.MonkeyPatch):
        from backend.app.services.eject import monitor as monitor_mod

        farm_stall._reset_state()
        monkeypatch.setattr(monitor_mod, "cooldown_waits", lambda: {})
        monkeypatch.setattr(printer_incidents, "automation_held", lambda pid: False)
        yield
        farm_stall._reset_state()

    @staticmethod
    def _manager(client: _FakeClient) -> SimpleNamespace:
        return SimpleNamespace(
            is_connected=lambda pid: False,
            get_status=lambda pid: client.state if pid == _PID else None,
            get_client=lambda pid: None,
        )

    async def test_an_action_whose_bound_passed_is_never_paged(
        self, client: _FakeClient, clock: FakeClock, db_session, waiting_page, heard
    ) -> None:
        """The attention owner's pass evaluates the bound FIRST (an idle printer reports rarely), and the
        action is then the lane's — it pages its own hand-over."""
        await printer_action.reread_session(_PID, 1)
        long_bound = printer_action.SelfHealBound("_LONG_SELF_HEAL", 3600.0, "a measured hour-long cycle")
        await printer_action.send(_PID, printer_action.resume(), actor=printer_action.ACTOR_FARM, bound=long_bound)
        clock.advance(2 * 3600)

        await farm_stall.check_waiting_on_printers(db_session, manager=self._manager(client), now=1_800_000_000.0)
        await _drain()

        assert len(heard[SelfHealDue]) == 1
        waiting_page.assert_not_awaited()

    @pytest.mark.parametrize("factory", [True, False], ids=["the-wired-factory", "no-factory"])
    async def test_a_rehydrated_power_loss_resume_is_not_paged_after_its_bound(
        self, client: _FakeClient, clock: FakeClock, db_session, own_session_factory, waiting_page, factory: bool
    ) -> None:
        """A farm power-loss resume the printer accepted is still out when the farm restarts. Its lane died with
        the old process; the re-spawned power-loss driver sends a NEW resume and holds the printer on that one's
        bound — so the orphan must not be paged beside it. The factory ``printer_event_wiring`` registers for
        ``power_loss_resume`` (``pause_recovery.rehydrated_power_loss_resume_bound``) gives it production's 30 s
        back from its send, the bound passes, and the attention owner skips it. Without the factory the same
        orphan is paged at the page bound — the double page this closes."""
        from backend.app.services import pause_recovery

        await printer_action.reread_session(_PID, 1)
        sent = await printer_action.send(_PID, printer_action.power_loss_resume(), actor=printer_action.ACTOR_FARM)
        printer_action.on_command_acked(_ack(sent.sequence_id))
        await _drain()
        await _restart(own_session_factory)
        if factory:
            printer_action.register_bound_factory(
                "power_loss_resume", pause_recovery.rehydrated_power_loss_resume_bound
            )
        client.state = _live_state(epoch=2)  # still PAUSEd at the prompt: accepted, the new session keeps it out
        await printer_action.reread_session(_PID, 2)
        assert printer_action.outstanding(sent.action_id)
        clock.advance(farm_stall._DEFAULT_PAGE_BOUND_MINUTES * 60 + 60)

        await farm_stall.check_waiting_on_printers(db_session, manager=self._manager(client), now=1_800_000_000.0)
        await _drain()

        assert printer_action.bound_passed(sent.action_id) is factory
        assert printer_action.outstanding(sent.action_id)  # the record still awaits its late answer
        if factory:
            waiting_page.assert_not_awaited()
            assert (await _row(own_session_factory, sent.action_id)).bound_passed_at is not None
        else:
            waiting_page.assert_awaited_once()

    async def test_an_actions_age_excludes_offline_time(
        self, client: _FakeClient, clock: FakeClock, db_session, waiting_page
    ) -> None:
        await printer_action.reread_session(_PID, 1)
        sent = await printer_action.send(_PID, printer_action.resume(), actor=printer_action.ACTOR_FARM)
        printer_action.on_command_acked(_ack(sent.sequence_id))
        bound_s = farm_stall._DEFAULT_PAGE_BOUND_MINUTES * 60
        clock.advance(bound_s - 60)
        printer_action.on_session_lost(SessionLost(printer_id=_PID))
        clock.advance(4 * bound_s)  # a long outage: wall age far past the page bound

        await farm_stall.check_waiting_on_printers(db_session, manager=self._manager(client), now=1_800_000_000.0)
        waiting_page.assert_not_awaited()

        client.state = _live_state(epoch=2)
        await printer_action.reread_session(_PID, 2)
        clock.advance(61)
        await farm_stall.check_waiting_on_printers(db_session, manager=self._manager(client), now=1_800_000_100.0)

        waiting_page.assert_awaited_once()
        assert waiting_page.await_args.args[2].startswith(
            f"The printer accepted the resume {farm_stall._DEFAULT_PAGE_BOUND_MINUTES} min ago"
        )


# --- the session's end on the client ------------------------------------------------------------------


class TestTheClientHandsOverTheSessionsEnd:
    @staticmethod
    def _client(on_session_lost):
        from unittest.mock import MagicMock

        from backend.app.services.bambu_mqtt import BambuMQTTClient

        client = BambuMQTTClient(
            ip_address="127.0.0.1", serial_number="S1", access_code="x", on_session_lost=on_session_lost
        )
        client._client = MagicMock()
        return client

    def test_it_is_handed_over_on_the_disconnect_edge_only(self) -> None:
        calls: list[None] = []
        client = self._client(lambda: calls.append(None))
        client.state.connected = True

        client.note_disconnected()
        client.note_disconnected()  # "still offline": no second session end

        assert len(calls) == 1

    def test_a_failing_handler_never_breaks_the_disconnect(self, caplog) -> None:
        def broken() -> None:
            raise RuntimeError("farm-side bug")

        client = self._client(broken)
        client.state.connected = True

        client.note_disconnected()  # must not raise (invariant 10)

        assert client.state.connected is False
        assert client.state.disconnected_at is not None
        assert "printer-event hand-over failed" in caplog.text
