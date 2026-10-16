"""The attention owner (owner 5, 2026-10-11): ``farm_stall.check_waiting_on_printers``.

The farm decides no printer outcome by a clock — a command the printer accepted and has not carried
out, a printer stuck in a state the farm waits on, each WAITS for the printer's own event. This module
is how a person hears of the wait: past the bound ONE page in the printer's terms, then hourly; never
inside the bound (the slow-firmware matrix: an answer that arrives late but inside the bound pages
nothing); never a printer a person is standing at (maintenance mode). It reads, it pages; it never
resolves, escalates or acts.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.models.print_queue import PrintQueueItem
from backend.app.services import farm_stall, printer_action, printer_incidents
from backend.app.services.eject import monitor as monitor_mod
from backend.app.services.feed_state import FeedPhase
from backend.app.services.notification_service import notification_service
from backend.app.services.plate_occupancy import EscalationOnly
from backend.app.services.printer_action import OutstandingAction

pytestmark = pytest.mark.asyncio

_BOUND_S = farm_stall._DEFAULT_PAGE_BOUND_MINUTES * 60
_HOUR = farm_stall._ATTENTION_REMINDER_S
_NOW = 1_800_000_000.0


class _Client:
    def __init__(self, phase, feeder=None):
        self._phase = phase
        self._feeder = feeder

    def feed(self):
        """The two fields the attention owner reads off ``FeedReading``: the phase and the effective feeder
        of the active extruder (``None`` — nothing fed)."""
        return SimpleNamespace(phase=self._phase, feeder=self._feeder)


class _Manager:
    def __init__(self, states=None, phases=None, *, connected=True, feeders=None):
        self._states = states or {}
        self._phases = phases or {}
        self._feeders = feeders or {}
        self._connected = connected

    def is_connected(self, pid):
        return self._connected

    def get_status(self, pid):
        return self._states.get(pid)

    def get_client(self, pid):
        return _Client(self._phases.get(pid, FeedPhase.FED), self._feeders.get(pid))


def _state(state="PAUSE", *, ams_status_main=0, layer_num=5, connected=True):
    return SimpleNamespace(state=state, ams_status_main=ams_status_main, layer_num=layer_num, connected=connected)


def _action(
    printer_id,
    *,
    age_s,
    command="stop",
    acked=True,
    actor="farm",
    target=None,
    action_id=1,
    incident_id=None,
    bound_passed=False,
):
    return OutstandingAction(
        action_id=action_id,
        printer_id=printer_id,
        command=command,
        actor=actor,
        target=target,
        incident_id=incident_id,
        sent_at=datetime.utcnow() - timedelta(seconds=age_s),
        acked=acked,
        bound_passed=bound_passed,
    )


def _online_throughout(printer_id, anchor):
    """The record's online clock for a printer that held its session the whole time: wall seconds since the
    send (the offline exclusion is pinned against the real clock in ``test_printer_action_self_heal``)."""
    return (datetime.utcnow() - anchor).total_seconds()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    farm_stall._reset_state()
    monkeypatch.setattr(printer_action, "outstanding_actions", lambda: [])
    monkeypatch.setattr(printer_action, "online_seconds_since", _online_throughout)
    monkeypatch.setattr(monitor_mod, "cooldown_waits", lambda: {})
    yield
    farm_stall._reset_state()


@pytest.fixture
def waiting_page():
    with patch.object(notification_service, "on_printer_waiting", new_callable=AsyncMock) as page:
        yield page


class TestTheRecordsUnansweredActions:
    async def test_an_answer_late_but_inside_the_bound_pages_nothing(self, db_session, monkeypatch, waiting_page):
        """The slow-firmware matrix: a stop the printer takes 56-62 s to end, a start that sits ~50 s at
        FINISH — every healthy answer lands inside the bound, so nothing pages."""
        _rows = [_action(1, age_s=_BOUND_S - 10)]
        monkeypatch.setattr(printer_action, "outstanding_actions", lambda: _rows)

        await farm_stall.check_waiting_on_printers(db_session, manager=_Manager({1: _state()}), now=_NOW)

        waiting_page.assert_not_awaited()

    async def test_an_accepted_stop_never_carried_out_pages_once_then_hourly(
        self, db_session, monkeypatch, waiting_page
    ):
        _rows = [_action(1, age_s=_BOUND_S + 60)]
        monkeypatch.setattr(printer_action, "outstanding_actions", lambda: _rows)
        manager = _Manager({1: _state("PAUSE")})

        await farm_stall.check_waiting_on_printers(db_session, manager=manager, now=_NOW)
        await farm_stall.check_waiting_on_printers(db_session, manager=manager, now=_NOW + 60)

        waiting_page.assert_awaited_once()
        detail = waiting_page.await_args.args[2]
        assert detail == "The printer accepted the stop 11 min ago and has not carried it out. The printer reads PAUSE."

        await farm_stall.check_waiting_on_printers(db_session, manager=manager, now=_NOW + _HOUR)
        assert waiting_page.await_count == 2

    async def test_an_unanswered_start_on_a_printer_that_is_off_its_session(
        self, db_session, monkeypatch, waiting_page
    ):
        """A start the printer never answered in 11 min ONLINE, whose session then dropped: the record keeps
        it outstanding (no clock voids it) and the page says the printer is not connected."""
        _rows = [_action(2, age_s=_BOUND_S + 60, command="print_start", acked=False)]
        monkeypatch.setattr(printer_action, "outstanding_actions", lambda: _rows)

        await farm_stall.check_waiting_on_printers(db_session, manager=_Manager(connected=False), now=_NOW)

        assert waiting_page.await_args.args[2] == (
            "The farm sent the print start 11 min ago and the printer has not answered. The printer is not connected."
        )

    async def test_a_persons_dialog_press_never_answered(self, db_session, monkeypatch, waiting_page):
        _rows = [_action(3, age_s=_BOUND_S + 60, command="dialog_press", acked=False, actor="operator")]
        monkeypatch.setattr(printer_action, "outstanding_actions", lambda: _rows)

        await farm_stall.check_waiting_on_printers(db_session, manager=_Manager({3: _state()}), now=_NOW)

        assert waiting_page.await_args.args[2].startswith("A person sent the dialog button 11 min ago")

    async def test_a_swap_command_held_inside_a_change_takes_the_recovery_copy(
        self, db_session, monkeypatch, waiting_page
    ):
        """Capsule 3's owed page (a): the AMS went back into its own filament change and holds the farm's
        swap command behind it — the driver waits for it, and this is the page."""
        _rows = [_action(4, age_s=_BOUND_S + 60, command="unload")]
        monkeypatch.setattr(printer_action, "outstanding_actions", lambda: _rows)

        await farm_stall.check_waiting_on_printers(
            db_session, manager=_Manager({4: _state(ams_status_main=1)}), now=_NOW
        )

        assert waiting_page.await_args.args[2] == (
            "The AMS went back into a filament change and holds the farm's swap command behind it. "
            "Free the spool, then press Retry on the printer."
        )

    async def test_an_accepted_pull_back_outside_a_change(self, db_session, monkeypatch, waiting_page):
        _rows = [_action(4, age_s=_BOUND_S + 60, command="unload")]
        monkeypatch.setattr(printer_action, "outstanding_actions", lambda: _rows)

        await farm_stall.check_waiting_on_printers(db_session, manager=_Manager({4: _state()}), now=_NOW)

        assert waiting_page.await_args.args[2] == (
            "The AMS accepted the pull-back 11 min ago and has not run it. Recovery resumes when it runs."
        )

    async def test_a_held_motion_command_past_its_step_bound_is_paged_at_the_page_bound(
        self, db_session, monkeypatch, waiting_page
    ):
        """Capsule 4+5's finding: a farm pull-back the AMS accepted OUTSIDE a change passes its step bound and
        sits in the recovery driver's unbounded wait for it to run, which pages nothing. While its row's
        pending command names it, the attention owner pages it at the page bound (the 2026-10-10 ruling: "One
        page goes out if it has not run 10 min after the send"; production paged at 600 s) — never before."""
        from backend.app.services.printer_incidents import PendingCommand

        held = PendingCommand(
            incident_id=70, action_id=7, seq=3, name="unload", target=None, loaded_at_send=True, sent_at=None
        )
        monkeypatch.setattr(
            printer_incidents,
            "pending_command",
            lambda pid, *, incident_id=None: held if (pid, incident_id) == (4, 70) else None,
        )
        manager = _Manager({4: _state()})
        early = [_action(4, age_s=_BOUND_S - 30, command="unload", action_id=7, incident_id=70, bound_passed=True)]
        monkeypatch.setattr(printer_action, "outstanding_actions", lambda: early)
        await farm_stall.check_waiting_on_printers(db_session, manager=manager, now=_NOW)
        waiting_page.assert_not_awaited()

        late = [_action(4, age_s=_BOUND_S + 60, command="unload", action_id=7, incident_id=70, bound_passed=True)]
        monkeypatch.setattr(printer_action, "outstanding_actions", lambda: late)
        await farm_stall.check_waiting_on_printers(db_session, manager=manager, now=_NOW)

        waiting_page.assert_awaited_once()
        assert waiting_page.await_args.args[2] == (
            "The AMS accepted the pull-back 11 min ago and has not run it. Recovery resumes when it runs."
        )

    async def test_every_other_bound_passed_action_stays_its_lanes(self, db_session, monkeypatch, waiting_page):
        """A bound-passed action its row does not hold pending is its lane's (the lane paged its own
        hand-over): a stop, and a motion command the row's pending command no longer names."""
        from backend.app.services.printer_incidents import PendingCommand

        newer = PendingCommand(
            incident_id=70, action_id=9, seq=4, name="load", target=2, loaded_at_send=False, sent_at=None
        )
        monkeypatch.setattr(printer_incidents, "pending_command", lambda pid, *, incident_id=None: newer)
        _rows = [
            _action(4, age_s=_BOUND_S + 60, command="stop", action_id=6, incident_id=70, bound_passed=True),
            _action(4, age_s=_BOUND_S + 60, command="unload", action_id=7, incident_id=70, bound_passed=True),
            _action(4, age_s=_BOUND_S + 60, command="unload", action_id=8, bound_passed=True),
        ]
        monkeypatch.setattr(printer_action, "outstanding_actions", lambda: _rows)

        await farm_stall.check_waiting_on_printers(db_session, manager=_Manager({4: _state()}), now=_NOW)

        waiting_page.assert_not_awaited()

    async def test_maintenance_mode_pages_nothing(self, db_session, monkeypatch, waiting_page):
        _rows = [_action(5, age_s=_BOUND_S + 60)]
        monkeypatch.setattr(printer_action, "outstanding_actions", lambda: _rows)
        monkeypatch.setattr(printer_incidents, "automation_held", lambda pid: True)

        await farm_stall.check_waiting_on_printers(db_session, manager=_Manager({5: _state()}), now=_NOW)

        waiting_page.assert_not_awaited()


class TestTheLevels:
    async def test_a_print_running_inside_a_filament_change_pages_past_the_bound(
        self, db_session, printer_factory, waiting_page
    ):
        """The 009 hang (capsule 3's owed page b): it used to be paused at 90 s; now it is waited for
        and paged."""
        printer = await printer_factory()
        manager = _Manager({printer.id: _state("RUNNING")}, {printer.id: FeedPhase.CHANGING})

        await farm_stall.check_waiting_on_printers(db_session, manager=manager, now=_NOW)
        await farm_stall.check_waiting_on_printers(db_session, manager=manager, now=_NOW + _BOUND_S - 1)
        waiting_page.assert_not_awaited()

        await farm_stall.check_waiting_on_printers(db_session, manager=manager, now=_NOW + _BOUND_S)
        waiting_page.assert_awaited_once()
        assert "RUNNING inside a filament change for 10 min" in waiting_page.await_args.args[2]

    async def test_a_change_that_ends_inside_the_bound_pages_nothing(self, db_session, printer_factory, waiting_page):
        printer = await printer_factory()
        changing = _Manager({printer.id: _state("RUNNING")}, {printer.id: FeedPhase.CHANGING})
        fed = _Manager({printer.id: _state("RUNNING")}, {printer.id: FeedPhase.FED})

        await farm_stall.check_waiting_on_printers(db_session, manager=changing, now=_NOW)
        await farm_stall.check_waiting_on_printers(db_session, manager=fed, now=_NOW + _BOUND_S - 30)
        await farm_stall.check_waiting_on_printers(db_session, manager=changing, now=_NOW + _BOUND_S)

        waiting_page.assert_not_awaited()  # a new episode began at the second sighting

    async def test_nothing_fed_pages_on_its_own_short_bound(self, db_session, printer_factory, waiting_page):
        printer = await printer_factory()
        manager = _Manager({printer.id: _state("RUNNING")}, {printer.id: FeedPhase.EMPTY_UNCONFIRMED})

        await farm_stall.check_waiting_on_printers(db_session, manager=manager, now=_NOW)
        await farm_stall.check_waiting_on_printers(
            db_session, manager=manager, now=_NOW + farm_stall._NOTHING_FED_PAGE_S
        )

        waiting_page.assert_awaited_once()
        assert waiting_page.await_args.args[2].startswith("The printer is RUNNING with the toolhead empty")

    async def test_an_end_of_print_retract_that_never_ends_pages_as_a_bare_level(
        self, db_session, printer_factory, waiting_page
    ):
        """F9 (2026-10-16, operator-approved): the printer's own END-OF-PRINT retract — RUNNING at the last
        layer (the feed state's END_RETRACT) with the active extruder EMPTY (the end unload has begun) — is never
        acted on by the farm; one that never ends is a person's to hear about past the page bound, ONCE — a bare
        level, no hold required (004-H2S #621: the farm paused it three times at 167/167 instead). The episode
        begins at the first EMPTY sighting: the fed last layer before it counts for nothing."""
        printer = await printer_factory()
        printing = _Manager(
            {printer.id: _state("RUNNING")}, {printer.id: FeedPhase.END_RETRACT}, feeders={printer.id: 0}
        )
        retracting = _Manager({printer.id: _state("RUNNING")}, {printer.id: FeedPhase.END_RETRACT})

        await farm_stall.check_waiting_on_printers(db_session, manager=printing, now=_NOW - _BOUND_S)
        await farm_stall.check_waiting_on_printers(db_session, manager=retracting, now=_NOW)
        await farm_stall.check_waiting_on_printers(db_session, manager=retracting, now=_NOW + _BOUND_S - 1)
        waiting_page.assert_not_awaited()

        await farm_stall.check_waiting_on_printers(db_session, manager=retracting, now=_NOW + _BOUND_S)
        await farm_stall.check_waiting_on_printers(db_session, manager=retracting, now=_NOW + _BOUND_S + 60)
        waiting_page.assert_awaited_once()
        assert waiting_page.await_args.args[2] == (
            "The printer has been RUNNING in its end-of-print retract at the last layer for 10 min. Check the toolhead."
        )

    async def test_a_long_last_layer_still_printing_fed_pages_nothing(self, db_session, printer_factory, waiting_page):
        """END_RETRACT spans printing the whole last layer: a healthy last layer printing FED (a feeder at the
        active extruder) for longer than the bound never starts the level."""
        printer = await printer_factory()
        printing = _Manager(
            {printer.id: _state("RUNNING")}, {printer.id: FeedPhase.END_RETRACT}, feeders={printer.id: 2}
        )

        for offset in (0, _BOUND_S, 3 * _BOUND_S):
            await farm_stall.check_waiting_on_printers(db_session, manager=printing, now=_NOW + offset)

        waiting_page.assert_not_awaited()

    async def test_a_finish_clears_the_retract_level(self, db_session, printer_factory, waiting_page):
        """The job's FINISH ends the episode: a retract that finishes inside the bound pages nothing, and a new
        retract on a later job starts a new episode from its own first sighting."""
        printer = await printer_factory()
        retracting = _Manager({printer.id: _state("RUNNING")}, {printer.id: FeedPhase.END_RETRACT})
        finished = _Manager({printer.id: _state("FINISH")}, {printer.id: FeedPhase.IDLE})

        await farm_stall.check_waiting_on_printers(db_session, manager=retracting, now=_NOW)
        await farm_stall.check_waiting_on_printers(db_session, manager=finished, now=_NOW + _BOUND_S - 30)
        await farm_stall.check_waiting_on_printers(db_session, manager=finished, now=_NOW + _BOUND_S)
        await farm_stall.check_waiting_on_printers(db_session, manager=retracting, now=_NOW + 2 * _BOUND_S)
        await farm_stall.check_waiting_on_printers(db_session, manager=retracting, now=_NOW + 3 * _BOUND_S - 1)

        waiting_page.assert_not_awaited()

    async def test_a_start_sequence_that_never_ends_pages_only_while_an_ams_row_is_open(
        self, db_session, printer_factory, monkeypatch, waiting_page
    ):
        """F9: the start sequence (START_BLOCK, before layer 1) runs minutes on every healthy print, so it
        pages only while an AMS row is open on that printer — the farm waits for the printer's event there and
        never pauses it. Without one: nothing, however long."""
        printer = await printer_factory()
        manager = _Manager({printer.id: _state("RUNNING", layer_num=0)}, {printer.id: FeedPhase.START_BLOCK})

        await farm_stall.check_waiting_on_printers(db_session, manager=manager, now=_NOW)
        await farm_stall.check_waiting_on_printers(db_session, manager=manager, now=_NOW + 3 * _BOUND_S)
        waiting_page.assert_not_awaited()  # no AMS row: a long start block is no wait the farm owns

        monkeypatch.setattr(printer_incidents, "open_kinds", lambda pid: frozenset({"jam"}))
        await farm_stall.check_waiting_on_printers(db_session, manager=manager, now=_NOW + 4 * _BOUND_S)
        waiting_page.assert_not_awaited()  # the episode begins at the first sighting WITH the row
        await farm_stall.check_waiting_on_printers(db_session, manager=manager, now=_NOW + 5 * _BOUND_S)

        waiting_page.assert_awaited_once()
        assert waiting_page.await_args.args[2] == (
            "The printer has been RUNNING in its start sequence for 10 min with an AMS hold open and no layer "
            "printed. Check the AMS and the toolhead."
        )

    async def test_a_paused_start_block_or_retract_pages_nothing(
        self, db_session, printer_factory, monkeypatch, waiting_page
    ):
        """The levels are the printer RUNNING its own sequence: a PAUSE is the hold's (its own pages)."""
        printer = await printer_factory()
        monkeypatch.setattr(printer_incidents, "open_kinds", lambda pid: frozenset({"jam"}))
        for phase in (FeedPhase.START_BLOCK, FeedPhase.END_RETRACT):
            farm_stall._reset_state()
            manager = _Manager({printer.id: _state("PAUSE")}, {printer.id: phase})
            await farm_stall.check_waiting_on_printers(db_session, manager=manager, now=_NOW)
            await farm_stall.check_waiting_on_printers(db_session, manager=manager, now=_NOW + 2 * _BOUND_S)

        waiting_page.assert_not_awaited()

    async def test_an_unreadable_feed_pages_past_the_bound(self, db_session, printer_factory, waiting_page):
        printer = await printer_factory()
        manager = _Manager({printer.id: _state("PAUSE")}, {printer.id: FeedPhase.UNKNOWN})

        await farm_stall.check_waiting_on_printers(db_session, manager=manager, now=_NOW)
        await farm_stall.check_waiting_on_printers(db_session, manager=manager, now=_NOW + _BOUND_S)

        waiting_page.assert_awaited_once()
        assert "feed has been unreadable" in waiting_page.await_args.args[2]

    async def test_a_cooldown_running_long_pages_through_its_own_event(self, db_session, printer_factory, monkeypatch):
        printer = await printer_factory()
        wait = monitor_mod.CooldownWait(waited_s=5400, held=False, bed_c=50.0, line_c=31.0)
        monkeypatch.setattr(monitor_mod, "cooldown_waits", lambda: {printer.id: wait})

        with patch.object(notification_service, "on_cooldown_escalation", new_callable=AsyncMock) as page:
            await farm_stall.check_waiting_on_printers(
                db_session, manager=_Manager({printer.id: _state("FINISH")}), now=_NOW
            )
            await farm_stall.check_waiting_on_printers(
                db_session, manager=_Manager({printer.id: _state("FINISH")}), now=_NOW + 60
            )

        page.assert_awaited_once()
        assert page.await_args.kwargs["bed_c"] == 50.0

    @pytest.mark.parametrize(
        ("ran_past_estimate_s", "pages"),
        [(_BOUND_S - 30, 0), (_BOUND_S + 30, 1)],
        ids=["inside_the_bound", "past_the_bound"],
    )
    async def test_an_eject_sweep_running_long_pages_past_its_estimate_and_bound(
        self, db_session, printer_factory, monkeypatch, waiting_page, ran_past_estimate_s, pages
    ):
        """No deadline stops a sweep any more (2026-10-11 — only the bed-drop guard can): a sweep still
        running its estimate plus the page bound after the printer started it is a person's to look at.
        A slow firmware tail inside the bound (the unmodelled ~+200 s purge) pages nothing."""
        from datetime import timezone

        printer = await printer_factory()
        pending = SimpleNamespace(
            started_at=datetime.now(timezone.utc) - timedelta(seconds=83.0 + ran_past_estimate_s),
            expected_runtime_s=83.0,
            runtime_exceeded_at=None,
        )
        monkeypatch.setattr(farm_stall.plate_occupancy, "pending_eject_view", lambda pid: pending)

        await farm_stall.check_waiting_on_printers(
            db_session, manager=_Manager({printer.id: _state("RUNNING")}), now=_NOW
        )
        await farm_stall.check_waiting_on_printers(
            db_session, manager=_Manager({printer.id: _state("RUNNING")}), now=_NOW + 60
        )

        assert waiting_page.await_count == pages
        if pages:
            assert waiting_page.await_args.args[2].startswith("The eject sweep has run")
            assert "expected 83 s" in waiting_page.await_args.args[2]

    async def test_a_sweep_the_farm_already_stopped_is_the_kill_paths_page(
        self, db_session, printer_factory, monkeypatch, waiting_page
    ):
        from datetime import timezone

        printer = await printer_factory()
        pending = SimpleNamespace(
            started_at=datetime.now(timezone.utc) - timedelta(seconds=83.0 + _BOUND_S + 300),
            expected_runtime_s=83.0,
            runtime_exceeded_at=datetime.now(timezone.utc),
        )
        monkeypatch.setattr(farm_stall.plate_occupancy, "pending_eject_view", lambda pid: pending)

        await farm_stall.check_waiting_on_printers(
            db_session, manager=_Manager({printer.id: _state("RUNNING")}), now=_NOW
        )

        waiting_page.assert_not_awaited()

    async def test_a_held_cooldown_is_not_paged(self, db_session, printer_factory, monkeypatch):
        printer = await printer_factory()
        wait = monitor_mod.CooldownWait(waited_s=9000, held=True, bed_c=50.0, line_c=31.0)
        monkeypatch.setattr(monitor_mod, "cooldown_waits", lambda: {printer.id: wait})

        with patch.object(notification_service, "on_cooldown_escalation", new_callable=AsyncMock) as page:
            await farm_stall.check_waiting_on_printers(
                db_session, manager=_Manager({printer.id: _state("FINISH")}), now=_NOW
            )

        page.assert_not_awaited()

    async def test_a_plate_only_a_person_may_clear_pages_past_its_bound(self, db_session, printer_factory, monkeypatch):
        printer = await printer_factory()
        view = SimpleNamespace(
            plate_occupied=True,
            plate_policy=EscalationOnly(),
            plate_since=datetime.utcnow() - timedelta(seconds=farm_stall._GATED_PLATE_PAGE_S + 5),
            plate_source_subtask_id="SUB-1",
        )
        monkeypatch.setattr(farm_stall.plate_occupancy, "snapshot", lambda pid: view)

        with patch.object(monitor_mod, "notify_plate_not_empty", new_callable=AsyncMock) as page:
            await farm_stall.check_waiting_on_printers(
                db_session, manager=_Manager({printer.id: _state("IDLE")}), now=_NOW
            )

        page.assert_awaited_once()
        assert page.await_args.kwargs["source_detail"].startswith("A farm unit's eject did not run")

    async def test_a_stalled_upload_hold_pages_past_the_bound(self, db_session, printer_factory, waiting_page):
        printer = await printer_factory()
        item = PrintQueueItem(
            printer_id=printer.id,
            status="pending",
            first_article=False,
            plate_id=1,
            position=1,
            waiting_reason="upload_stalled",
        )
        db_session.add(item)
        await db_session.commit()
        manager = _Manager({printer.id: _state("IDLE")})

        await farm_stall.check_waiting_on_printers(db_session, manager=manager, now=_NOW)
        await farm_stall.check_waiting_on_printers(db_session, manager=manager, now=_NOW + _BOUND_S)

        waiting_page.assert_awaited_once()
        # R19 (2026-10-12): only a PINNED unit is held on a stall (a pooled one skips the printer).
        assert waiting_page.await_args.args[2].startswith(
            f"Queue unit {item.id}: upload to its pinned printer stalled; unit held"
        )


class TestTheCooldownStallPagesAtItsEventAndNeverQuarantines:
    async def test_the_stall_pages_the_plate_and_takes_no_printer_out_of_rotation(self, monkeypatch):
        """Owner 6: the eject monitor never quarantines. A bed that will not cool, or an eject that would
        not dispatch, pages at its event (the stall) and the plate stays gated for a person."""
        from backend.app.services import farm_policy

        quarantine = AsyncMock()
        monkeypatch.setattr(farm_policy, "quarantine_printer", quarantine)
        with patch.object(monitor_mod, "notify_plate_not_empty", new_callable=AsyncMock) as page:
            await monitor_mod._page_cooldown_stall("bed cooling plateaued", printer_id=7)

        page.assert_awaited_once()
        assert page.await_args.kwargs["source_detail"].startswith("Cooldown stopped: bed cooling plateaued.")
        quarantine.assert_not_awaited()
