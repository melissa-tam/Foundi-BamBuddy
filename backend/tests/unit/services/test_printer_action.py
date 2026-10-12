"""``services/printer_action`` — THE record of what was done to a printer and what it answered.

Driven through the record's public entry points — :func:`printer_action.send` / ``prepare`` +
``publish``, the hub handlers (``on_command_acked``, ``on_status_reported``, ``on_feed_event``,
``on_job_terminal``), :func:`printer_action.reread_session` and :func:`printer_action.rehydrate` — with
a fake client in the registry and the rows read back from the test database:

* the row is written BEFORE the publish and its id IS the frame's ``sequence_id``;
* the restart matrix — send, lose the process memory, rehydrate, and the ACK that lands afterwards
  (naming the row by its id) still answers the row;
* a fail ACK refuses, a reboot voids, a later session voids an unacknowledged command;
* a landing of the commanded tray reaches the farm's load and is attributed to it; a landing nobody
  commanded is recorded as ``observed_load`` (the firmware's own backup switch);
* an operator's AMS command takes the farm's outstanding one over;
* no clock answers anything: an action no event answers stays outstanding.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest
from sqlalchemy import select

from backend.app.models.printer_action import PrinterAction
from backend.app.services import printer_action
from backend.app.services.bambu_mqtt import CommandAck, PrinterState
from backend.app.services.command_outcome import Answer
from backend.app.services.feed_state import AutoSwitched, FeedPhase, LoadLanded
from backend.app.services.printer_events import CommandAcked, FeedEventReported, JobTerminal, StatusReported
from backend.app.services.printer_manager import printer_manager

_PID = 7


@dataclass
class _FakeClient:
    """The registry's client, as far as the record reaches it: its state and the publishers it calls."""

    state: PrinterState = field(default_factory=PrinterState)
    published: list[tuple[str, str]] = field(default_factory=list)
    accept: bool = True
    last_dispatch_subtask_id: str | None = None

    def _publish(self, name: str, seq: str) -> bool:
        self.published.append((name, seq))
        return self.accept

    def resume_print(self, *, sequence_id: str) -> bool:
        return self._publish("resume", sequence_id)

    def pause_print(self, *, sequence_id: str) -> bool:
        return self._publish("pause", sequence_id)

    def stop_print(self, *, sequence_id: str) -> bool:
        return self._publish("stop", sequence_id)

    def ams_load_filament(self, tray: int, *, sequence_id: str) -> bool:
        return self._publish(f"load:{tray}", sequence_id)

    def ams_unload_filament(self, *, sequence_id: str) -> bool:
        return self._publish("unload", sequence_id)

    def send_gcode(self, text: str, *, sequence_id: str) -> bool:
        return self._publish(f"gcode:{text}", sequence_id)

    def start_print(self, filename: str, *, sequence_id: str, **options: object) -> bool:
        self.last_dispatch_subtask_id = "424242"
        return self._publish(f"start:{filename}", sequence_id)

    def ack_for(self, sequence_id: str) -> CommandAck | None:
        for ack in reversed(tuple(self.state.command_acks)):
            if ack.sequence_id == sequence_id:
                return ack
        return None

    def feed(self):  # pragma: no cover - not read by the record
        return None


def _live_state(
    *, state: str = "PAUSE", tray_now: int = 255, subtask: str | None = "900", epoch: int = 1
) -> PrinterState:
    st = PrinterState()
    st.connected = True
    st.connection_epoch = epoch
    st.report_epoch = epoch
    st.state = state
    st.tray_now = tray_now
    st.subtask_id = subtask
    return st


@pytest.fixture(autouse=True)
async def _fresh_record():
    printer_action._reset_state()
    yield
    # A record write still in flight must land in THIS test's database, never the next one's (ids
    # restart per test database; they never restart in production — AUTOINCREMENT).
    await _drain()
    printer_action._reset_state()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, own_session_factory) -> _FakeClient:
    from backend.app.core import database as core_db

    monkeypatch.setattr(core_db, "async_session", own_session_factory)
    fake = _FakeClient(state=_live_state())
    monkeypatch.setattr(printer_manager, "get_client", lambda pid: fake if pid == _PID else None)
    monkeypatch.setattr(printer_manager, "get_status", lambda pid: fake.state if pid == _PID else None)
    return fake


async def _drain() -> None:
    """Let the record's spawned writes (an answer, an ACK stamp, an observed load) land."""
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


def _ledctrl() -> printer_action.Command:
    """The chamber light: an ACK-only verb whose echo rides the ``system`` block."""
    return printer_action.Command("ledctrl", lambda client, seq: client._publish("ledctrl", seq))


def _ack(seq: str, result: str = "success", reason: str | None = None, command: str = "resume") -> CommandAcked:
    return CommandAcked(
        printer_id=_PID, ack=CommandAck(command=command, sequence_id=seq, result=result, reason=reason, at=0.0)
    )


class TestTheSendIsARow:
    async def test_the_row_is_written_before_the_publish_and_its_id_is_the_sequence_id(
        self, client: _FakeClient, own_session_factory
    ) -> None:
        sent = await printer_action.send(
            _PID, printer_action.resume(), actor=printer_action.ACTOR_FARM, incident_id=None
        )

        assert sent.published
        assert client.published == [("resume", str(sent.action_id))]
        row = await _row(own_session_factory, sent.action_id)
        assert (row.printer_id, row.actor, row.command, row.answered_at) == (_PID, "farm", "resume", None)
        assert row.job_id == "900" and row.entry == "PAUSE"

    async def test_two_sends_never_share_an_id(self, client: _FakeClient) -> None:
        first = await printer_action.send(_PID, printer_action.pause(), actor=printer_action.ACTOR_FARM)
        second = await printer_action.send(_PID, printer_action.pause(), actor=printer_action.ACTOR_FARM)

        assert first.action_id != second.action_id
        assert [seq for _name, seq in client.published] == [str(first.action_id), str(second.action_id)]

    async def test_no_client_is_refused_at_once_and_recorded(
        self, monkeypatch: pytest.MonkeyPatch, own_session_factory
    ) -> None:
        from backend.app.core import database as core_db

        monkeypatch.setattr(core_db, "async_session", own_session_factory)
        monkeypatch.setattr(printer_manager, "get_client", lambda pid: None)
        monkeypatch.setattr(printer_manager, "get_status", lambda pid: None)

        sent = await printer_action.send(_PID, printer_action.resume(), actor=printer_action.ACTOR_OPERATOR)

        assert not sent.published and sent.refusal == "not_connected"
        assert await printer_action.await_outcome(sent) is Answer.REFUSED
        row = await _row(own_session_factory, sent.action_id)
        assert row.answer == "refused" and row.answered_at is not None

    async def test_a_publish_that_did_not_go_out_is_refused(self, client: _FakeClient, own_session_factory) -> None:
        client.accept = False

        sent = await printer_action.send(_PID, printer_action.stop(), actor=printer_action.ACTOR_FARM)
        await _drain()

        assert not sent.published and sent.refusal == "not_sent"
        assert (await _row(own_session_factory, sent.action_id)).answer == "refused"

    async def test_a_load_under_a_standing_runout_hold_is_refused_before_the_publish(
        self, client: _FakeClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(printer_action, "runout_hold_active", lambda state: True)
        monkeypatch.setattr(printer_manager, "get_printer", lambda pid: None)

        sent = await printer_action.send(_PID, printer_action.load(3), actor=printer_action.ACTOR_OPERATOR)

        assert not sent.published and sent.refusal == "runout_hold"
        assert client.published == []
        assert "latch" in (sent.refusal_detail or "")

    def test_an_unknown_command_is_refused_at_construction(self) -> None:
        with pytest.raises(LookupError):
            printer_action.Command("purge", lambda client, seq: True)


class TestTheAnswerIsAnEvent:
    async def test_a_resume_is_reached_when_the_printer_reports_running(
        self, client: _FakeClient, own_session_factory
    ) -> None:
        sent = await printer_action.send(_PID, printer_action.resume(), actor=printer_action.ACTOR_FARM)
        printer_action.on_command_acked(_ack(sent.sequence_id))
        assert printer_action.outstanding(sent.action_id)  # an ACK is acceptance, never execution

        client.state.state = "RUNNING"
        printer_action.on_status_reported(StatusReported(printer_id=_PID, state=client.state))
        await _drain()

        assert await printer_action.await_outcome(sent) is Answer.REACHED
        row = await _row(own_session_factory, sent.action_id)
        assert row.answer == "reached" and row.acked_at is not None

    async def test_a_resume_reached_however_late(self, client: _FakeClient) -> None:
        """No window: the RUNNING report arrives whenever it arrives (the old 30 s answered
        ``toolhead_resume_unconfirmed`` over a print that resumed at 45 s)."""
        sent = await printer_action.send(_PID, printer_action.resume(), actor=printer_action.ACTOR_FARM)
        waiter = asyncio.ensure_future(printer_action.await_outcome(sent))
        for _ in range(20):
            printer_action.on_status_reported(StatusReported(printer_id=_PID, state=client.state))
        await asyncio.sleep(0)
        assert not waiter.done()

        client.state.state = "RUNNING"
        printer_action.on_status_reported(StatusReported(printer_id=_PID, state=client.state))

        assert await waiter is Answer.REACHED

    async def test_a_fail_ack_refuses_with_the_printers_reason(self, client: _FakeClient, own_session_factory) -> None:
        sent = await printer_action.send(_PID, printer_action.resume(), actor=printer_action.ACTOR_FARM)

        printer_action.on_command_acked(_ack(sent.sequence_id, result="fail", reason="mqtt message verify failed"))
        await _drain()

        assert await printer_action.await_outcome(sent) is Answer.REFUSED
        row = await _row(own_session_factory, sent.action_id)
        assert row.answer == "refused" and row.reason == "mqtt message verify failed"

    async def test_a_reboot_voids_an_outstanding_command(
        self, client: _FakeClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent = await printer_action.send(_PID, printer_action.unload(), actor=printer_action.ACTOR_FARM)
        monkeypatch.setattr("backend.app.services.command_outcome.power_loss_prompt_standing", lambda hms: True)

        printer_action.on_status_reported(StatusReported(printer_id=_PID, state=client.state))

        assert await printer_action.await_outcome(sent) is Answer.VOID

    async def test_a_stop_is_reached_by_the_jobs_terminal_however_late(self, client: _FakeClient) -> None:
        client.state.state = "PAUSE"
        sent = await printer_action.send(_PID, printer_action.stop(), actor=printer_action.ACTOR_FARM)

        printer_action.on_job_terminal(
            JobTerminal(
                printer_id=_PID,
                status="failed",
                recorded_status="cancelled",
                job_id="900",
                is_eject=False,
                reconciled=True,
                archive_id=None,
                payload={},
            )
        )

        assert await printer_action.await_outcome(sent) is Answer.REACHED

    async def test_a_start_is_reached_when_the_dispatched_job_reports(
        self, client: _FakeClient, own_session_factory
    ) -> None:
        client.state.state = "IDLE"
        client.state.subtask_id = None
        sent = await printer_action.send(
            _PID,
            printer_action.print_start("unit.3mf", plate_id=1),
            actor=printer_action.ACTOR_FARM,
            queue_item_id=None,
        )
        await _drain()
        row = await _row(own_session_factory, sent.action_id)
        assert (row.job_id, row.prior_job_id) == ("424242", None)

        client.state.state = "PREPARE"
        client.state.subtask_id = "424242"
        printer_action.on_status_reported(StatusReported(printer_id=_PID, state=client.state))

        assert await printer_action.await_outcome(sent) is Answer.REACHED

    async def test_an_unacknowledged_command_is_void_when_a_later_session_begins(self, client: _FakeClient) -> None:
        sent = await printer_action.send(_PID, _ledctrl(), actor=printer_action.ACTOR_OPERATOR)
        client.state.connection_epoch = client.state.report_epoch = 2

        await printer_action.reread_session(_PID, 2)

        assert await printer_action.await_outcome(sent) is Answer.VOID

    async def test_an_ack_only_verb_is_answered_by_its_ack(self, client: _FakeClient) -> None:
        sent = await printer_action.send(_PID, _ledctrl(), actor=printer_action.ACTOR_OPERATOR)

        printer_action.on_command_acked(_ack(sent.sequence_id, command="ledctrl"))

        assert await printer_action.await_outcome(sent) is Answer.REACHED


class TestEveryCommandEcho:
    """R28 (invariant 14, every echo): the client's ONE parse point reads every block carrying ``command`` +
    ``result``, so the chamber light (``ledctrl``, a ``system`` echo) and the camera (``xcam_control_set``, an
    ``xcam`` echo) are answered by their REAL echo, end to end through the client's hand-over to the record."""

    @staticmethod
    def _parsing_client():
        from backend.app.services.bambu_mqtt import BambuMQTTClient

        return BambuMQTTClient(
            serial_number="ECHO",
            ip_address="192.168.1.100",
            access_code="12345678",
            on_command_ack=lambda ack: printer_action.on_command_acked(CommandAcked(printer_id=_PID, ack=ack)),
        )

    @pytest.mark.parametrize(
        ("block", "command", "result", "answer"),
        [
            ("system", "ledctrl", "success", Answer.REACHED),
            ("xcam", "xcam_control_set", "success", Answer.REACHED),
            ("system", "ledctrl", "fail", Answer.REFUSED),
        ],
    )
    async def test_an_echo_outside_the_print_block_answers_its_action(
        self, client: _FakeClient, own_session_factory, block, command, result, answer
    ) -> None:
        sent = await printer_action.send(
            _PID,
            printer_action.Command(command, lambda live, seq: live._publish(command, seq)),
            actor=printer_action.ACTOR_OPERATOR,
        )
        assert printer_action.outstanding(sent.action_id)

        self._parsing_client()._process_message(
            {block: {"command": command, "sequence_id": sent.sequence_id, "result": result}}
        )
        await _drain()

        assert await printer_action.await_outcome(sent) is answer
        assert (await _row(own_session_factory, sent.action_id)).answer == answer.value


class TestNoEchoByDesign:
    """R28: ``set_airduct``, ``gcode_line`` and ``print_speed`` carry no echo by design (``CommandSpec.echoes``
    False): recorded, never outstanding, never rehydrated, never paged — the row keeps a NULL answer with the
    reason "no echo by design", and no answer is fabricated."""

    def test_the_vocabulary_names_exactly_the_three(self) -> None:
        from backend.app.services.command_outcome import COMMANDS, echoes

        assert {name for name, spec in COMMANDS.items() if not spec.echoes} == {
            "set_airduct",
            "gcode_line",
            "print_speed",
        }
        assert echoes("an_older_builds_name") is True  # a name outside the vocabulary stays "outstanding"

    def test_every_command_a_null_answer_reader_asks_about_echoes(self) -> None:
        """The readers of a NULL answer — the resume budget and the hold chip (``resume``), the pending
        command and the refill verdict (``load`` / ``unload``), the plate-check and air stops (``stop``), the
        dispatch (``print_start``), the levers and dialog buttons — each ask about a command that echoes, so
        a no-echo command is never read as one of them (never a resume)."""
        from backend.app.services.command_outcome import COMMANDS, Family

        asked = {"resume", "power_loss_resume", "pause", "stop", "load", "unload", "print_start", "eject_start"}
        asked |= {name for name, spec in COMMANDS.items() if spec.family in (Family.AMS_LEVER, Family.DIALOG)}
        assert all(COMMANDS[name].echoes for name in asked)

    async def test_it_is_recorded_never_outstanding_and_keeps_a_null_answer(
        self, client: _FakeClient, own_session_factory
    ) -> None:
        sent = await printer_action.send(_PID, printer_action.gcode("M106 P2 S0"), actor=printer_action.ACTOR_FARM)
        await _drain()

        assert sent.published and client.published == [("gcode:M106 P2 S0", sent.sequence_id)]
        assert not printer_action.outstanding(sent.action_id)
        assert printer_action.outstanding_actions() == []
        row = await _row(own_session_factory, sent.action_id)
        assert (row.answer, row.answered_at, row.reason) == (None, None, "no echo by design")
        assert await printer_action.reason_of(sent.action_id) == "no echo by design"
        with pytest.raises(ValueError):
            await printer_action.await_outcome(sent)
        with pytest.raises(ValueError):
            await printer_action.settled(sent.action_id)

    async def test_it_is_never_rehydrated(self, client: _FakeClient, own_session_factory) -> None:
        await printer_action.send(_PID, printer_action.gcode("M400"), actor=printer_action.ACTOR_FARM)
        kept = await printer_action.send(_PID, printer_action.stop(), actor=printer_action.ACTOR_FARM)
        await _drain()

        printer_action._reset_state()
        async with own_session_factory() as db:
            assert await printer_action.rehydrate(db) == 1
        assert [action.action_id for action in printer_action.outstanding_actions()] == [kept.action_id]

    async def test_it_is_never_paged(self, client: _FakeClient, own_session_factory, monkeypatch) -> None:
        """Past every bound in the printer's online time, the attention owner pages nothing about it."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from backend.app.services import farm_stall, printer_incidents
        from backend.app.services.eject import monitor as monitor_mod
        from backend.app.services.notification_service import notification_service

        await printer_action.reread_session(_PID, 1)
        await printer_action.send(_PID, printer_action.gcode("M400"), actor=printer_action.ACTOR_FARM)
        await _drain()
        monkeypatch.setattr(printer_action, "online_seconds_since", lambda pid, anchor: 10 * 3600.0)
        monkeypatch.setattr(monitor_mod, "cooldown_waits", lambda: {})
        monkeypatch.setattr(printer_incidents, "automation_held", lambda pid: False)
        page = AsyncMock()
        monkeypatch.setattr(notification_service, "on_printer_waiting", page)
        manager = SimpleNamespace(
            is_connected=lambda pid: False, get_status=lambda pid: None, get_client=lambda pid: None
        )

        async with own_session_factory() as db:
            await farm_stall.check_waiting_on_printers(db, manager=manager, now=1_800_000_000.0)

        page.assert_not_awaited()


class TestTheRestartMatrix:
    async def test_a_start_the_printer_ran_across_a_restart_is_reached_never_void(
        self, client: _FakeClient, own_session_factory
    ) -> None:
        """Unacknowledged when the farm restarted, and the printer runs the dispatched job: the session's
        first report answers it REACHED (a VOID start would requeue the unit over its own print)."""
        client.state.state = "FINISH"
        client.state.subtask_id = "900"
        sent = await printer_action.send(
            _PID, printer_action.print_start("unit.3mf", plate_id=1), actor=printer_action.ACTOR_FARM
        )
        await _drain()
        row = await _row(own_session_factory, sent.action_id)
        assert (row.job_id, row.prior_job_id) == ("424242", "900")

        printer_action._reset_state()
        async with own_session_factory() as db:
            assert await printer_action.rehydrate(db) == 1
        client.state.state = "RUNNING"
        client.state.subtask_id = "424242"
        client.state.connection_epoch = client.state.report_epoch = 2
        await printer_action.reread_session(_PID, 2)
        await _drain()

        assert (await _row(own_session_factory, sent.action_id)).answer == "reached"

    async def test_a_stop_the_printer_took_across_a_reconnect_is_reached(
        self, client: _FakeClient, own_session_factory
    ) -> None:
        client.state.state = "PAUSE"
        sent = await printer_action.send(_PID, printer_action.stop(), actor=printer_action.ACTOR_FARM)
        client.state.state = "IDLE"
        client.state.connection_epoch = client.state.report_epoch = 2

        await printer_action.reread_session(_PID, 2)

        assert await printer_action.await_outcome(sent) is Answer.REACHED

    async def test_an_ack_after_a_restart_answers_the_row_by_its_id(
        self, client: _FakeClient, own_session_factory
    ) -> None:
        sent = await printer_action.send(_PID, _ledctrl(), actor=printer_action.ACTOR_OPERATOR)

        printer_action._reset_state()  # the restart: every slot of process memory is gone
        async with own_session_factory() as db:
            assert await printer_action.rehydrate(db) == 1
        printer_action.on_command_acked(_ack(sent.sequence_id, command="ledctrl"))
        await _drain()

        assert (await _row(own_session_factory, sent.action_id)).answer == "reached"

    async def test_an_accepted_load_survives_a_restart_and_is_reached_when_it_runs(
        self, client: _FakeClient, own_session_factory
    ) -> None:
        """The 011/014-H2S shape across a deploy: a load the AMS accepted and has not run, the farm
        restarts, and the AMS runs it on its own — the record still answers it."""
        sent = await printer_action.send(
            _PID, printer_action.load(2), actor=printer_action.ACTOR_FARM, incident_id=None
        )
        printer_action.on_command_acked(_ack(sent.sequence_id, command="ams_change_filament"))
        await _drain()

        printer_action._reset_state()
        async with own_session_factory() as db:
            await printer_action.rehydrate(db)
        await printer_action.reread_session(_PID, 1)  # acknowledged: a new session does not void it
        assert printer_action.outstanding(sent.action_id)

        client.state.tray_now = 2
        printer_action.on_status_reported(StatusReported(printer_id=_PID, state=client.state))
        await _drain()

        assert (await _row(own_session_factory, sent.action_id)).answer == "reached"


def _landing(seq: int, tray: int) -> FeedEventReported:
    return FeedEventReported(
        printer_id=_PID,
        event=LoadLanded(seq, 0, tray, FeedPhase.CHANGING),
        reading=None,  # type: ignore[arg-type]
    )


class TestWhoseLoadLanded:
    async def test_a_landing_of_the_commanded_tray_reaches_the_farms_load(self, client: _FakeClient) -> None:
        sent = await printer_action.send(_PID, printer_action.load(2), actor=printer_action.ACTOR_FARM)

        printer_action.on_feed_event(_landing(100, 2))

        assert await printer_action.await_outcome(sent) is Answer.REACHED
        assert printer_action.observed_load_at(_PID) is None
        assert printer_action.landed_by_command(_PID, 2)

    async def test_a_landing_nobody_commanded_is_the_printers_own(
        self, client: _FakeClient, own_session_factory
    ) -> None:
        """The firmware's backup switch (001-H2S 2026-10-10 23:43:31): no farm or operator load explains it."""
        printer_action.on_feed_event(_landing(101, 1))
        await _drain()

        assert printer_action.observed_load_at(_PID) is not None
        assert not printer_action.landed_by_command(_PID, 1)
        async with own_session_factory() as db:
            rows = (
                (await db.execute(select(PrinterAction).where(PrinterAction.command == "observed_load")))
                .scalars()
                .all()
            )
        assert [(row.actor, row.target, row.answer) for row in rows] == [("printer", 1, "reached")]

    async def test_the_uncommanded_landing_survives_a_restart(self, client: _FakeClient, own_session_factory) -> None:
        printer_action.on_feed_event(_landing(102, 1))
        await _drain()
        printer_action._reset_state()

        async with own_session_factory() as db:
            await printer_action.rehydrate(db)

        assert printer_action.observed_load_at(_PID) is not None

    async def test_a_load_reached_by_a_report_still_owns_its_landing(self, client: _FakeClient) -> None:
        """Order-proof: the report that shows the target feeding answers the load first; the landing
        event of the same push is still the load's, never an ``observed_load``."""
        sent = await printer_action.send(_PID, printer_action.load(3), actor=printer_action.ACTOR_FARM)
        client.state.tray_now = 3
        printer_action.on_status_reported(StatusReported(printer_id=_PID, state=client.state))
        assert await printer_action.await_outcome(sent) is Answer.REACHED

        printer_action.on_feed_event(_landing(103, 3))

        assert printer_action.observed_load_at(_PID) is None

    def test_an_auto_switch_event_is_not_a_landing(self, client: _FakeClient) -> None:
        printer_action.on_feed_event(
            FeedEventReported(printer_id=_PID, event=AutoSwitched(104, 0, 0, 1), reading=None)  # type: ignore[arg-type]
        )

        assert printer_action.observed_load_at(_PID) is None


class TestAPersonsAct:
    async def test_an_operators_ams_command_leaves_the_farms_outstanding(self, client: _FakeClient) -> None:
        """K11 (production; R13 of 2026-10-12): the AMS runs an accepted farm command when it runs, so a
        person's AMS command answers nothing of the farm's — it stays outstanding until an event (the
        toolhead, a reboot) answers it. The person's command is the record's operator motion, which the
        recovery driver reads as its own takeover."""
        from datetime import datetime

        farm = await printer_action.send(_PID, printer_action.unload(), actor=printer_action.ACTOR_FARM)
        clicked = await printer_action.send(_PID, printer_action.load(1), actor=printer_action.ACTOR_OPERATOR)

        assert printer_action.outstanding(farm.action_id)
        assert await printer_action.settled(farm.action_id) is None
        assert clicked.published and printer_action.operator_commanded_since(_PID, datetime.min)

    async def test_an_operators_command_is_the_records_operator_command_since(self, client: _FakeClient) -> None:
        from datetime import datetime, timedelta

        before = datetime.utcnow() - timedelta(seconds=1)
        await printer_action.send(_PID, printer_action.unload(), actor=printer_action.ACTOR_OPERATOR)

        assert printer_action.operator_commanded_since(_PID, before)
        assert not printer_action.operator_commanded_since(_PID, datetime.utcnow() + timedelta(seconds=1))


class TestNoClockAnswers:
    async def test_an_action_no_event_answers_stays_outstanding(self, client: _FakeClient) -> None:
        sent = await printer_action.send(_PID, printer_action.unload(), actor=printer_action.ACTOR_FARM)
        client.state.tray_now = 3  # loaded at the send, still loaded

        for _ in range(50):
            printer_action.on_status_reported(StatusReported(printer_id=_PID, state=client.state))

        assert printer_action.outstanding(sent.action_id)


class TestTheNextSessionWait:
    """``await_next_session`` — THE wait for the printer's next session (a frame that never left the farm
    is sent again then; never a retry clock). Woken by the record's own session re-read on the printer's
    first fresh report; the level is read first and re-read at the caller's cadence as its backstop."""

    async def test_a_session_that_already_began_answers_at_once(self, client: _FakeClient) -> None:
        client.state.connection_epoch = client.state.report_epoch = 2

        await asyncio.wait_for(printer_action.await_next_session(_PID, 1, recheck_s=60.0), 1.0)

    async def test_the_records_session_re_read_wakes_it(self, client: _FakeClient) -> None:
        waiter = asyncio.ensure_future(printer_action.await_next_session(_PID, 1, recheck_s=60.0))
        for _ in range(5):
            await asyncio.sleep(0)
        assert not waiter.done()  # the same session: no event yet

        client.state.connection_epoch = client.state.report_epoch = 2  # the new session's first fresh report
        await printer_action.reread_session(_PID, 2)
        await asyncio.wait_for(waiter, 1.0)  # woken by the event, never by the 60 s backstop

    async def test_a_reconnect_not_yet_reporting_is_no_new_session(self, client: _FakeClient) -> None:
        """A new session whose first report has not landed (``report_epoch`` behind) is not live: the
        waiter keeps waiting, and the level backstop answers once the report lands."""
        client.state.connection_epoch = 2  # reconnected, the re-broadcast of the last session's cache
        waiter = asyncio.ensure_future(printer_action.await_next_session(_PID, 1, recheck_s=0.005))
        await asyncio.sleep(0.03)
        assert not waiter.done()

        client.state.report_epoch = 2
        await asyncio.wait_for(waiter, 1.0)


class TestTheSyncSend:
    async def test_it_sends_in_order_and_stops_at_a_publish_that_did_not_go_out(self, client: _FakeClient) -> None:
        client.accept = False
        assert printer_action.send_soon(
            _PID, printer_action.gcode("A"), printer_action.gcode("B"), actor=printer_action.ACTOR_FARM
        )
        await _drain()

        assert [name for name, _seq in client.published] == ["gcode:A"]

    def test_no_live_session_sends_nothing(self, client: _FakeClient) -> None:
        client.state.connected = False

        assert not printer_action.send_soon(_PID, printer_action.gcode("A"), actor=printer_action.ACTOR_FARM)
