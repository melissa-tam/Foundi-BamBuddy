"""Maintenance mode: the hold's record, the quiesce it runs, and the release.

Everything here drives the REAL ``service_hold`` verbs against the REAL collaborators
the 2026-09-12 incident implicated — the plate-occupancy authority, the eject monitor
singleton and (for the fans) the real ``cooldown_prep`` over a spy MQTT client. Two
things are faked and both on purpose: the eject kill path (pinned end-to-end in
``eject/test_reconcile.py``) and the printer manager's wire answers.

The shapes this suite exists to hold, all from that morning:

* the fans come OFF while the session is still up when the SESSION is what is going
  (010-H2S ran them 6.3 h because the MQTT teardown went first and ``prep.end()`` landed
  on ``skipped:no_client``) — and, since 2026-09-13, they deliberately do NOT come off when
  a human merely takes the printer: entering a hold leaves the cooldown running (fans
  only) and withholds the eject, so the two verbs are pinned apart here;
* an in-flight sweep is stopped through the ONE kill path, not a re-composed one;
* the dispatch lease is revoked, so a unit already past its decision point unwinds
  instead of printing onto a printer somebody has taken (002-H2S got a new unit within
  5 s of leaving maintenance three times);
* the durable row comes FIRST, and re-entering a standing hold still quiesces;
* and the hold ends ONLY through its own verb — the two operator entry points that end
  every other human-owned hold leave it standing.
"""

import asyncio
import logging
from types import SimpleNamespace

import pytest

# Imported at module level so the test engine's create_all registers the table.
from backend.app.models.printer_incident import (
    KIND_SERVICE_HOLD,
    STATUS_ESCALATED,
    PrinterIncident,
)
from backend.app.services import farm_policy, pause_recovery, print_control, printer_incidents, service_hold
from backend.app.services.dispatch_kick import dispatch_kick
from backend.app.services.eject import cooldown_prep, monitor as monitor_mod, remote as eject_remote
from backend.app.services.eject.monitor import CooldownWatchSettings, eject_cooldown_monitor
from backend.app.services.plate_occupancy import (
    CooldownEject,
    DepositEvidence,
    Evidence,
    PendingEject,
    TerminalDisposition,
    plate_occupancy,
)

pytestmark = pytest.mark.asyncio


# --- fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset():
    printer_incidents._reset_state()
    yield
    printer_incidents._reset_state()


@pytest.fixture(autouse=True)
def _disarm_monitor():
    """The monitor is a process-lifetime SINGLETON (``service_hold`` reads that one, so
    the tests must too). A watch armed by one case may not survive into the next."""
    yield
    for printer_id in list(eject_cooldown_monitor._armed):
        eject_cooldown_monitor.stand_down(printer_id, "test teardown")


@pytest.fixture(autouse=True)
def _own_sessions(test_engine, monkeypatch):
    """Point the lanes that open their OWN session at the test engine.

    ``pause_recovery.on_plate_cleared`` is one, and it is the closer this suite pins.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    import backend.app.core.database as core_db

    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(core_db, "async_session", maker)
    return maker


# --- doubles ----------------------------------------------------------------


class _SpyClient:
    """The MQTT client, recording what it was told to publish."""

    def __init__(self) -> None:
        self.gcode: list[str] = []
        self.fans: list[tuple[int, int]] = []

    def send_gcode(self, text: str) -> bool:
        self.gcode.append(text)
        return True

    def set_fan_percent(self, fan: int, percent: int) -> bool:
        self.fans.append((fan, percent))
        return True


class _FakeManager:
    """``printer_manager`` reduced to the questions the quiesce and the prep ask it."""

    def __init__(self, client: _SpyClient | None = None, *, state: str | None = "FINISH", model: str = "H2S") -> None:
        self.client = client
        self.state = state
        self.model = model
        self.stopped: list[int] = []

    def get_client(self, printer_id: int):
        return self.client

    def is_connected(self, printer_id: int) -> bool:
        return self.client is not None

    def get_status(self, printer_id: int):
        if self.state is None:
            return None
        return SimpleNamespace(state=self.state, big_fan1_speed=None, big_fan2_speed=None)

    def get_model(self, printer_id: int) -> str:
        return self.model

    def drop(self) -> None:
        """What ``disconnect_printer`` does: from this instant there is no client."""
        self.client = None

    async def _broadcast_status_change(self, printer_id: int) -> None:
        """The Bambuddy-side status emit. A no-op here: the WS fan-out has its own
        tests, and a double that omitted it would make ``enter`` fail on a socket."""
        return None

    def stop_print(self, printer_id: int) -> bool:
        self.stopped.append(printer_id)
        return True


def _watch_settings() -> CooldownWatchSettings:
    """The production fan shape with the plate hold switched OFF.

    The hold is what needs a geometry row, a donor 3MF and a measured part height; this
    suite is about the FANS, and the hold's own decisions are pinned in
    ``eject/test_cooldown_prep.py``.
    """
    return CooldownWatchSettings(
        stall_window_s=0,
        stall_epsilon_c=1.0,
        max_hold_s=0,
        plateau_eject_margin_c=3.0,
        aux_fan_enabled=True,
        aux_fan_percent=100,
        chamber_fan_enabled=True,
        chamber_fan_percent=100,
        chamber_fan_sustain_percent=50,
        hold_enabled=False,
        hold_part_top_mm=100,
    )


def _occupy(printer_id: int, policy) -> None:
    """Raise the plate gate with ``policy``, the way a deposit-bearing terminal does."""
    plate_occupancy.note_terminal(
        printer_id,
        TerminalDisposition(
            queue_item_id=None,
            source_subtask_id="SUB-1",
            evidence=DepositEvidence(
                final_status="completed",
                is_dry_run=False,
                peaks_reliable=True,
                last_layer_num=42,
                last_progress=100.0,
            ),
            policy=policy,
            raise_gate=True,
        ),
    )


def _fake_spawns(monkeypatch) -> list[str]:
    """Record the watches the monitor arms without running any of them."""
    names: list[str] = []

    def _spawn(coro, *, name=None):
        coro.close()
        names.append(name)
        return SimpleNamespace(done=lambda: False, cancel=lambda: None, get_name=lambda: name)

    monkeypatch.setattr(monitor_mod, "spawn_background_task", _spawn)
    return names


async def _settle(predicate, *, turns: int = 200) -> bool:
    """Yield to the loop until ``predicate`` holds (or give up). No wall-clock waits."""
    for _ in range(turns):
        if predicate():
            return True
        await asyncio.sleep(0)
    return predicate()


# --- the record -------------------------------------------------------------


class TestEnterOpensTheHold:
    async def test_enter_opens_an_escalated_declared_row(self, db_session, printer_factory):
        printer = await printer_factory()

        verdict = await service_hold.enter(db_session, printer.id, actor="raymond")

        row = await printer_incidents.get_open(db_session, printer.id, kinds={KIND_SERVICE_HOLD})
        assert row is not None
        assert row.kind == KIND_SERVICE_HOLD
        # A declared hold is a human's from the instant it opens — nothing is
        # "recovering" it, and ``escalated_at`` is when the human took the printer.
        assert row.status == STATUS_ESCALATED
        assert row.escalated_at is not None
        assert verdict.held is True
        assert verdict.already_held is False
        # THE predicate every automatic lane reads now answers yes.
        assert printer_incidents.automation_held(printer.id) is True

    async def test_a_printer_with_no_session_is_still_held_and_reports_nothing_quiesced(
        self, db_session, printer_factory
    ):
        """Entering on a deactivated / disconnected printer records the hold and does
        nothing else — the hold is what stops the kick-driven scheduler dispatching
        within ~1 s of re-activation, so it must be reachable there."""
        printer = await printer_factory(is_active=False)

        verdict = await service_hold.enter(db_session, printer.id, actor="raymond")

        assert (verdict.held, verdict.already_held) == (True, False)
        assert (verdict.eject_stopped, verdict.job_stopped, verdict.lease_revoked) == (False, False, False)
        assert printer_incidents.automation_held(printer.id) is True

    async def test_re_entering_reports_already_held_and_quiesces_again(self, db_session, printer_factory):
        """The second click means "make this machine quiet" — usually because something
        came back that the first entry never saw. Here it is a dispatch lease."""
        printer = await printer_factory()
        first = await service_hold.enter(db_session, printer.id, actor="raymond")
        assert first.already_held is False

        lease = plate_occupancy.claim_for_dispatch(
            printer.id,
            unit_id=1857,
            pre_state="IDLE",
            pre_subtask=None,
            min_hold_s=0.0,
            max_hold_s=0.0,
            ev=Evidence(live_state="IDLE"),
        )

        second = await service_hold.enter(db_session, printer.id, actor="raymond")

        assert second.already_held is True
        assert second.lease_revoked is True  # the quiesce ran a second time
        assert plate_occupancy.commit_dispatch(printer.id, lease) == "lease_revoked"
        # Still exactly one row: the hold is not re-opened, it is re-quiesced.
        rows = await printer_incidents.open_rows(db_session, printer.id)
        assert [row.kind for row in rows] == [KIND_SERVICE_HOLD]


# --- the quiesce ------------------------------------------------------------


class TestEnteringKeepsTheCooldown:
    """The REAL monitor and the REAL ``cooldown_prep`` over a spy client.

    Two verbs, two opposite consequences, pinned side by side because conflating them is
    what produced both of the operator's complaints:

    * :func:`service_hold.quiesce` (what ENTERING runs) leaves the watch armed and both
      fans running — a hold stops what the farm DOES to the machine, and moving air over
      a hot plate is not that. The eject is withheld inside the watch instead;
    * :func:`service_hold.quiesce_for_teardown` (what DEACTIVATION runs) retires the
      watch, and asserts the consequence — ``M106 P2 S0`` / ``M106 P3 S0`` published — at
      the instant the call RETURNS, because that is the instant the session goes. A pin
      that awaited the task first would have passed on the 2026-09-12 probe defect, where
      the publishes happened one loop turn too late and landed on a deleted client.
    """

    async def _arm_cooldown(self, printer, monkeypatch):
        """Arm a real CooldownEject watch on a connected fake printer; return its parts."""
        client = _SpyClient()
        manager = _FakeManager(client)
        monkeypatch.setattr(cooldown_prep, "printer_manager", manager)
        monkeypatch.setattr(monitor_mod, "printer_manager", manager)

        async def _threshold(queue_item_id, *, for_first_article=False):
            return 33.0

        async def _settings():
            return _watch_settings()

        async def _never_releases(printer_id, threshold, **kwargs):
            await asyncio.Event().wait()  # the bed never reaches the line in this test

        monkeypatch.setattr(monitor_mod, "_resolve_eject_threshold", _threshold)
        monkeypatch.setattr(monitor_mod, "_resolve_stall_settings", _settings)
        monkeypatch.setattr(monitor_mod, "watch_bed_and_clear", _never_releases)
        plate_occupancy.configure(policy_driver=eject_cooldown_monitor.on_occupancy_change)

        _occupy(printer.id, CooldownEject(unit_id=1857, run_id=None))
        assert await _settle(lambda: len(client.fans) >= 2), client.fans
        assert client.fans == [(2, 100), (3, 100)]  # both lanes armed at boost
        return client, manager, eject_cooldown_monitor._armed[printer.id].task

    async def test_entering_leaves_the_watch_armed_and_both_fans_running(
        self, db_session, printer_factory, monkeypatch, caplog
    ):
        """The operator requirement, 2026-09-13: under maintenance mode the cooldown runs
        as in production — and the eject does not. Cancelling the watch here commanded both
        fans off over a bed that was still hot, because the watch's ``finally`` cannot tell
        "the session is going" from "a human is at the machine"."""
        printer = await printer_factory(model="H2S")
        client, _manager, task = await self._arm_cooldown(printer, monkeypatch)

        with caplog.at_level(logging.INFO, logger=service_hold.__name__):
            verdict = await service_hold.enter(db_session, printer.id, actor="raymond")

        assert verdict.held is True
        assert task.done() is False  # the watch is still polling the bed
        assert eject_cooldown_monitor._armed[printer.id].task is task
        assert client.fans == [(2, 100), (3, 100)]  # nothing was switched off
        assert "cooldown watch continues under the hold (fans only, eject withheld)" in caplog.text
        # The plate's STORED policy is untouched, as it always was.
        assert plate_occupancy.current_view(printer.id).plate_policy == CooldownEject(unit_id=1857, run_id=None)

    async def test_the_teardown_verb_publishes_the_fans_off_before_the_session_drops(
        self, db_session, printer_factory, monkeypatch
    ):
        """THE 2026-09-12 probe defect, pinned as a consequence rather than as call order.

        ``update_printer`` calls ``quiesce_for_teardown(cause="deactivate")`` and then
        ``disconnect_printer``, which DELETES the client. The order was always right; what
        was wrong is that ``cancel()`` only schedules the CancelledError, so the watch's
        ``finally`` ran a loop turn later and published into a client that was already
        gone — the live probe caught the quiesce's own report at 07:07:08,545 and
        ``off=skipped:no_client`` at 07:07:08,546. So this asserts what was published at
        the instant the call RETURNED, which is the instant the session goes.

        Losing the session is the one thing that genuinely ends a cooldown — which is why
        this verb exists separately from the hold's quiesce, rather than the hold
        borrowing it.
        """
        printer = await printer_factory(model="H2S")
        client, manager, task = await self._arm_cooldown(printer, monkeypatch)

        await service_hold.quiesce_for_teardown(printer.id, cause="deactivate")
        published_while_connected = list(client.fans)
        manager.drop()  # exactly what update_printer does next: disconnect_printer

        assert task.done(), "the teardown must not return while the watch is still retiring"
        assert printer.id not in eject_cooldown_monitor._armed
        assert published_while_connected[-2:] == [(2, 0), (3, 0)], (
            "both fans must be commanded OFF while the MQTT session is still up"
        )
        assert client.fans == published_while_connected, "nothing may be published after the client is dropped"

    async def test_the_teardown_verb_still_runs_the_whole_quiesce(self, db_session, printer_factory, monkeypatch):
        """It is the quiesce PLUS the watch, never a second sequence of its own."""
        printer = await printer_factory()
        _fake_spawns(monkeypatch)
        lease = plate_occupancy.claim_for_dispatch(
            printer.id,
            unit_id=1856,
            pre_state="IDLE",
            pre_subtask=None,
            min_hold_s=0.0,
            max_hold_s=0.0,
            ev=Evidence(live_state="IDLE"),
        )

        report = await service_hold.quiesce_for_teardown(printer.id, cause="deactivate")

        assert report.lease_revoked is True
        assert plate_occupancy.commit_dispatch(printer.id, lease) == "lease_revoked"

    async def test_no_armed_watch_says_nothing_about_a_cooldown(self, db_session, printer_factory, monkeypatch, caplog):
        """The line is a statement about THIS printer's plate, so a printer whose plate
        nothing is watching must not produce it."""
        printer = await printer_factory()
        _fake_spawns(monkeypatch)

        with caplog.at_level(logging.INFO, logger=service_hold.__name__):
            verdict = await service_hold.enter(db_session, printer.id, actor="raymond")

        assert verdict.held is True
        assert "cooldown watch continues" not in caplog.text


class TestQuiesceStopsTheSweep:
    async def test_an_in_flight_eject_is_stopped_through_the_one_kill_path(
        self, db_session, printer_factory, monkeypatch
    ):
        printer = await printer_factory()
        calls: list[tuple[int, str]] = []

        async def _fake_redrive(printer_id, *, stage, sleep=None):
            calls.append((printer_id, stage))
            return True

        monkeypatch.setattr(eject_remote, "redrive_eject_stop", _fake_redrive)
        plate_occupancy.hydrate_eject(printer.id, PendingEject(purpose="production", run_id=None, queue_item_id=1857))

        verdict = await service_hold.enter(db_session, printer.id, actor="raymond")

        assert calls == [(printer.id, "service_hold")]
        assert verdict.eject_stopped is True

    async def test_no_eject_means_no_kill_is_driven(self, db_session, printer_factory, monkeypatch):
        printer = await printer_factory()
        calls: list[str] = []

        async def _fake_redrive(printer_id, *, stage, sleep=None):
            calls.append(stage)
            return True

        monkeypatch.setattr(eject_remote, "redrive_eject_stop", _fake_redrive)

        verdict = await service_hold.enter(db_session, printer.id, actor="raymond")

        assert calls == []
        assert verdict.eject_stopped is False


class TestQuiesceStopsTheJob:
    @pytest.fixture()
    def _marks(self, monkeypatch):
        """Record the user-stopped mark ``print_control`` sets through ``main``."""
        import backend.app.main as main_mod

        marked: list[int] = []
        monkeypatch.setattr(main_mod, "mark_printer_stopped_by_user", marked.append)
        return marked

    @pytest.mark.parametrize("live", ["RUNNING", "PAUSE", "PREPARE", "SLICING"])
    async def test_an_active_job_is_stopped_as_the_operator(
        self, db_session, printer_factory, monkeypatch, _marks, live
    ):
        """Every state the plate authority calls a job — a PAUSEd print is still a job
        to stop before hands go in, and PREPARE is about to deposit onto the plate."""
        printer = await printer_factory()
        manager = _FakeManager(_SpyClient(), state=live)
        monkeypatch.setattr(service_hold, "printer_manager", manager)
        monkeypatch.setattr(print_control, "printer_manager", manager)

        verdict = await service_hold.enter(db_session, printer.id, actor="raymond")

        assert manager.stopped == [printer.id]
        # The mark is the half that makes the terminal a CANCEL rather than a failure.
        assert _marks == [printer.id]
        assert verdict.job_stopped is True

    @pytest.mark.parametrize("live", ["IDLE", "FINISH", "FAILED", None])
    async def test_an_idle_printer_is_not_stopped(self, db_session, printer_factory, monkeypatch, _marks, live):
        printer = await printer_factory()
        manager = _FakeManager(_SpyClient(), state=live)
        monkeypatch.setattr(service_hold, "printer_manager", manager)
        monkeypatch.setattr(print_control, "printer_manager", manager)

        verdict = await service_hold.enter(db_session, printer.id, actor="raymond")

        assert manager.stopped == []
        assert _marks == []
        assert verdict.job_stopped is False


class TestQuiesceRevokesTheLease:
    async def test_a_dispatch_past_its_decision_point_is_refused_at_commit(self, db_session, printer_factory):
        """002-H2S 2026-09-12: leaving maintenance handed the printer a new unit within
        5 s. The lease is revoked rather than dropped, so the scheduler holding it gets
        ``lease_revoked`` — "somebody took this printer" — and unwinds its row, instead
        of ``lease_unknown``, which is a different story with a different cure."""
        printer = await printer_factory()
        lease = plate_occupancy.claim_for_dispatch(
            printer.id,
            unit_id=1855,
            pre_state="IDLE",
            pre_subtask=None,
            min_hold_s=0.0,
            max_hold_s=0.0,
            ev=Evidence(live_state="IDLE"),
        )

        verdict = await service_hold.enter(db_session, printer.id, actor="raymond")

        assert verdict.lease_revoked is True
        assert plate_occupancy.commit_dispatch(printer.id, lease) == "lease_revoked"


class TestQuiesceNeverRaises:
    async def test_one_failing_step_does_not_skip_the_rest(self, db_session, printer_factory, monkeypatch):
        """A human is standing at the machine: the fans, the sweep, the job and the
        lease are independent, and the one thing worse than a step failing is a step
        failing and skipping the three after it."""
        printer = await printer_factory()

        def _boom(printer_id):
            raise RuntimeError("no wire")

        monkeypatch.setattr(eject_cooldown_monitor, "active_watch", _boom)
        lease = plate_occupancy.claim_for_dispatch(
            printer.id,
            unit_id=1,
            pre_state="IDLE",
            pre_subtask=None,
            min_hold_s=0.0,
            max_hold_s=0.0,
            ev=Evidence(live_state="IDLE"),
        )

        report = await service_hold.quiesce(printer.id, cause="service hold")

        assert report.eject_stopped is False  # the step that threw reported nothing
        assert report.lease_revoked is True  # the step after it still ran
        assert plate_occupancy.commit_dispatch(printer.id, lease) == "lease_revoked"


# --- the release ------------------------------------------------------------


class TestExit:
    async def test_exit_closes_the_row_leaves_the_watch_alone_and_kicks_dispatch(
        self, db_session, printer_factory, monkeypatch
    ):
        printer = await printer_factory()
        spawned = _fake_spawns(monkeypatch)
        plate_occupancy.configure(policy_driver=eject_cooldown_monitor.on_occupancy_change)
        await service_hold.enter(db_session, printer.id, actor="raymond")

        # A plate that deposits DURING the hold arms its watch: the hold decides what that
        # watch may DO (fans only, eject withheld), never whether it exists.
        _occupy(printer.id, CooldownEject(unit_id=1857, run_id=None))
        assert spawned == [f"eject-cooldown-watch-{printer.id}"]
        armed = eject_cooldown_monitor._armed[printer.id]

        released = await service_hold.exit(db_session, printer.id, actor="raymond")

        assert released is True
        assert await printer_incidents.get_open(db_session, printer.id, kinds={KIND_SERVICE_HOLD}) is None
        assert printer_incidents.automation_held(printer.id) is False
        # Nothing is re-armed: the armed watch is untouched, and it reads the hold as a
        # per-tick level — so the eject dispatches on its very next poll. A respawn here
        # would restart the cooldown from zero and re-boost the fans.
        assert spawned == [f"eject-cooldown-watch-{printer.id}"]
        assert eject_cooldown_monitor._armed[printer.id] is armed
        assert dispatch_kick._reasons[-1][1:] == ("service_hold_released", printer.id)

    async def test_exit_on_an_unheld_printer_releases_nothing(self, db_session, printer_factory):
        printer = await printer_factory()

        assert await service_hold.exit(db_session, printer.id, actor="raymond") is False

    async def test_the_release_closes_only_its_own_row(self, db_session, printer_factory):
        """A fault recorded DURING the hold outlives it: ``hold_blocks_dispatch`` must
        keep refusing work until that fault resolves by its own rule."""
        printer = await printer_factory()
        await service_hold.enter(db_session, printer.id, actor="raymond")
        jam = await printer_incidents.open_new(
            db_session,
            printer_id=printer.id,
            job_id="task-1",
            item_id=None,
            kind="jam",
            code="0700_8010",
            codes="jam:0700_8010",
            slot_global_tray=None,
            status=STATUS_ESCALATED,
        )
        assert jam is not None

        assert await service_hold.exit(db_session, printer.id, actor="raymond") is True

        assert printer_incidents.automation_held(printer.id) is False
        assert printer_incidents.hold_blocks_dispatch(printer.id) is True
        assert [row.kind for row in await printer_incidents.open_rows(db_session, printer.id)] == ["jam"]


class TestOnlyItsOwnVerbEndsTheHold:
    """The two OPERATOR entry points that end every other human-owned hold.

    B pinned the five closers inside the store; these are the verbs an operator
    actually presses. ``clear-plate`` and ``Recover`` both mean "the part is off the
    plate" — neither means "I am finished working on this printer".
    """

    async def test_clear_plate_leaves_the_hold_standing(self, db_session, printer_factory):
        printer = await printer_factory()
        await service_hold.enter(db_session, printer.id, actor="raymond")

        closed = await pause_recovery.on_plate_cleared(printer.id, recover=False)

        assert closed == []
        assert printer_incidents.automation_held(printer.id) is True
        assert await printer_incidents.get_open(db_session, printer.id, kinds={KIND_SERVICE_HOLD}) is not None

    async def test_recover_leaves_the_hold_standing(self, db_session, printer_factory):
        printer = await printer_factory()
        await service_hold.enter(db_session, printer.id, actor="raymond")

        result = await farm_policy.recover_printer(db_session, printer.id)

        assert result["quarantine_cleared"] is False
        # ...and the verb REPORTS that it closed nothing, rather than leaving the
        # operator to infer it from a chip that stayed lit.
        assert result["incidents_closed"] == []
        assert printer_incidents.automation_held(printer.id) is True
        assert await printer_incidents.get_open(db_session, printer.id, kinds={KIND_SERVICE_HOLD}) is not None
