"""State-transition tests for the cooldown-verified plate-clear monitor."""

from types import SimpleNamespace

import pytest

from backend.app.services.eject.monitor import (
    EjectCooldownMonitor,
    deposited_nothing,
    should_auto_clear,
    should_rearm,
    watch_bed_and_clear,
    watch_gate_escalation_only,
)


class TestDepositedNothing:
    """Truth table for the no-deposit classifier used by the plate-clear gate."""

    def test_dry_run_is_always_no_deposit(self):
        # is_dry_run wins regardless of any progress the non-print gcode reported.
        assert deposited_nothing(is_dry_run=True, last_layer_num=None, last_progress=None) is True
        assert deposited_nothing(is_dry_run=True, last_layer_num=5, last_progress=99.0) is True

    def test_zero_layers_zero_progress_is_no_deposit(self):
        assert deposited_nothing(is_dry_run=False, last_layer_num=0, last_progress=0) is True

    def test_both_none_is_no_deposit(self):
        assert deposited_nothing(is_dry_run=False, last_layer_num=None, last_progress=None) is True

    def test_zero_layers_but_progress_deposited(self):
        # Lag-by-one guard: layer 0 but nonzero progress means a print started.
        assert deposited_nothing(is_dry_run=False, last_layer_num=0, last_progress=3.2) is False

    def test_layers_produced_deposited(self):
        assert deposited_nothing(is_dry_run=False, last_layer_num=5, last_progress=0) is False
        assert deposited_nothing(is_dry_run=False, last_layer_num=5, last_progress=50.0) is False


class _FakeManager:
    """Scripted printer_manager: yields the next status per get_status call and
    records set_awaiting_plate_clear calls."""

    def __init__(self, statuses):
        self._statuses = list(statuses)
        self._i = 0
        self.clear_calls = []

    def get_status(self, printer_id):
        if self._i < len(self._statuses):
            s = self._statuses[self._i]
        else:
            s = self._statuses[-1] if self._statuses else None
        self._i += 1
        return s

    def set_awaiting_plate_clear(self, printer_id, awaiting):
        self.clear_calls.append((printer_id, awaiting))


def _status(bed, connected=True):
    return SimpleNamespace(connected=connected, temperatures={"bed": bed})


async def _noop_sleep(_seconds):
    return None


class TestShouldAutoClear:
    def test_completed_clears(self):
        assert should_auto_clear("completed") is True

    @pytest.mark.parametrize("status", ["failed", "aborted", "cancelled", "printing"])
    def test_non_success_does_not_clear(self, status):
        assert should_auto_clear(status) is False


class TestShouldRearm:
    """Startup re-arm decision: gate raised + last job completed + eject profile."""

    def test_rearms_completed_eject_job_with_gate_set(self):
        assert should_rearm(True, "completed", 5) is True

    def test_no_rearm_when_gate_not_set(self):
        assert should_rearm(False, "completed", 5) is False

    @pytest.mark.parametrize("status", ["failed", "aborted", "cancelled", "printing", "pending", None])
    def test_no_rearm_on_non_completed_status(self, status):
        # Failures/stops presume an occupied plate — the gate stays for a human.
        assert should_rearm(True, status, 5) is False

    def test_no_rearm_without_eject_profile(self):
        # Non-eject jobs keep the manual plate-clear flow untouched.
        assert should_rearm(True, "completed", None) is False

    def test_first_article_never_rearms(self):
        # A completed first-article item carries an eject profile but its eject
        # block is deliberately NOT injected — the part stays on the plate, so the
        # gate must not auto-clear.
        assert should_rearm(True, "completed", 5, first_article=True) is False
        # Non-FA item with the same inputs still re-arms.
        assert should_rearm(True, "completed", 5, first_article=False) is True


class TestStartWatchDedup:
    """Terminal-status and startup re-arm share _start_watch; it must not
    double-spawn for a printer whose watch is already in flight."""

    def test_second_start_is_a_noop(self, monkeypatch):
        from backend.app.services.eject import monitor as monitor_mod

        spawned = []

        def fake_spawn(coro, *, name=None):
            spawned.append(name)
            coro.close()  # never run it — we only count spawns

        monkeypatch.setattr(monitor_mod, "spawn_background_task", fake_spawn)
        mon = EjectCooldownMonitor()
        assert mon._start_watch(7, 100) is True
        assert mon._start_watch(7, 100) is False  # dedup while in flight
        assert mon._start_watch(8, 101) is True  # other printers unaffected
        assert spawned == ["eject-cooldown-watch-7", "eject-cooldown-watch-8"]


class TestResolveEjectThresholdFirstArticle:
    """`_resolve_eject_threshold` must resolve first-article items to no-auto-clear
    even though they carry an eject profile — and it keys off the SPECIFIC item id
    (db.get), not the most-recently-started item on the printer (Phase 1)."""

    async def test_first_article_resolves_to_none(self, db_session, monkeypatch):
        import contextlib

        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.services.eject import monitor as monitor_mod

        @contextlib.asynccontextmanager
        async def _fake_session():
            yield db_session

        monkeypatch.setattr("backend.app.core.database.async_session", _fake_session, raising=False)

        # first_article short-circuits before the profile lookup, so eject_profile_id
        # just has to be non-null (FK enforcement is off in tests).
        item = PrintQueueItem(printer_id=7, eject_profile_id=5, first_article=True, status="printing")
        db_session.add(item)
        await db_session.commit()
        await db_session.refresh(item)

        threshold = await monitor_mod._resolve_eject_threshold(item.id)
        assert threshold is None


class _NotifyRecorder:
    """Injectable notify callable: records printer_ids and optionally raises."""

    def __init__(self, raise_exc: bool = False):
        self.calls: list[int] = []
        self._raise = raise_exc

    async def __call__(self, printer_id):
        self.calls.append(printer_id)
        if self._raise:
            raise RuntimeError("notify boom")


class TestWatchBedAndClear:
    async def test_clears_when_bed_reaches_threshold(self):
        mgr = _FakeManager([_status(60), _status(40), _status(27)])
        notify = _NotifyRecorder()
        outcome = await watch_bed_and_clear(
            7, 28.0, manager=mgr, escalate_s=100, check_interval_s=20, sleep=_noop_sleep, notify=notify
        )
        assert outcome == "cleared"
        assert mgr.clear_calls == [(7, False)]
        assert notify.calls == []  # crossed well before escalation

    async def test_clears_at_exact_threshold(self):
        mgr = _FakeManager([_status(28.0)])
        notify = _NotifyRecorder()
        outcome = await watch_bed_and_clear(
            3, 28.0, manager=mgr, escalate_s=100, check_interval_s=20, sleep=_noop_sleep, notify=notify
        )
        assert outcome == "cleared"
        assert mgr.clear_calls == [(3, False)]

    async def test_stale_when_status_none(self):
        mgr = _FakeManager([None])
        outcome = await watch_bed_and_clear(
            9, 28.0, manager=mgr, escalate_s=100, check_interval_s=20, sleep=_noop_sleep
        )
        assert outcome == "stale"
        assert mgr.clear_calls == []  # gate left SET

    async def test_stale_when_disconnected(self):
        mgr = _FakeManager([_status(60, connected=False)])
        outcome = await watch_bed_and_clear(
            9, 28.0, manager=mgr, escalate_s=100, check_interval_s=20, sleep=_noop_sleep
        )
        assert outcome == "stale"
        assert mgr.clear_calls == []

    async def test_escalates_once_then_keeps_watching_until_crossing(self):
        # Hot past the escalate window, THEN cools. escalate_s=40, interval=20 →
        # escalation fires at the tick where elapsed==40, watch continues, and the
        # later crossing still releases the gate.
        mgr = _FakeManager([_status(60), _status(60), _status(60), _status(27)])
        notify = _NotifyRecorder()
        outcome = await watch_bed_and_clear(
            5, 33.0, manager=mgr, escalate_s=40, check_interval_s=20, sleep=_noop_sleep, notify=notify
        )
        assert outcome == "cleared"  # watch did NOT stop at the escalate window
        assert mgr.clear_calls == [(5, False)]
        assert notify.calls == [5]  # fired exactly once

    async def test_escalation_notify_failure_does_not_kill_watch(self):
        # A notify that raises must be swallowed — the watch keeps polling and the
        # later crossing still releases the gate.
        mgr = _FakeManager([_status(60), _status(60), _status(60), _status(27)])
        notify = _NotifyRecorder(raise_exc=True)
        outcome = await watch_bed_and_clear(
            6, 33.0, manager=mgr, escalate_s=40, check_interval_s=20, sleep=_noop_sleep, notify=notify
        )
        assert outcome == "cleared"
        assert mgr.clear_calls == [(6, False)]
        assert notify.calls == [6]  # attempted once, exception did not propagate

    async def test_stale_exit_still_bounds_a_never_cooling_watch(self):
        # A never-cooling bed that later goes offline still ends the watch (the
        # stale exit is what bounds the now-unbounded loop).
        mgr = _FakeManager([_status(60), _status(60), _status(60, connected=False)])
        notify = _NotifyRecorder()
        outcome = await watch_bed_and_clear(
            9, 28.0, manager=mgr, escalate_s=40, check_interval_s=20, sleep=_noop_sleep, notify=notify
        )
        assert outcome == "stale"
        assert mgr.clear_calls == []


class TestOnTerminalStatusArming:
    """Phase 1: the auto-clear watch arms ONLY with a positively correlated
    queue_item_id AND a success terminal. Everything else leaves the gate alone."""

    def _mon_with_recording_start(self, monkeypatch):
        mon = EjectCooldownMonitor()
        calls: list[tuple[int, int]] = []

        def fake_start(pid, qid):
            calls.append((pid, qid))
            return True

        monkeypatch.setattr(mon, "_start_watch", fake_start)
        return mon, calls

    def test_no_arm_without_queue_item_id(self, monkeypatch):
        # A finish we could not attribute to a unit (foreign/none/fallback) passes
        # queue_item_id=None — it must NOT auto-clear.
        mon, calls = self._mon_with_recording_start(monkeypatch)
        mon.on_terminal_status(7, "completed", queue_item_id=None)
        assert calls == []

    @pytest.mark.parametrize("status", ["failed", "aborted", "cancelled"])
    def test_no_arm_on_non_success(self, status, monkeypatch):
        mon, calls = self._mon_with_recording_start(monkeypatch)
        mon.on_terminal_status(7, status, queue_item_id=42)
        assert calls == []

    def test_arms_on_completed_with_item(self, monkeypatch):
        mon, calls = self._mon_with_recording_start(monkeypatch)
        mon.on_terminal_status(7, "completed", queue_item_id=42)
        assert calls == [(7, 42)]


class _FakeGateManager:
    """Scripted gate/status source for the escalation-only watch. Advances one
    scripted step per poll (both is_awaiting_plate_clear and get_status read the
    same step) and records any set_awaiting_plate_clear call so a test can prove
    the escalation watch NEVER releases the gate."""

    def __init__(self, script):
        self._script = list(script)  # [(awaiting: bool, status: SimpleNamespace | None), ...]
        self._i = 0
        self._current = (False, None)
        self.clear_calls: list[tuple] = []

    def is_awaiting_plate_clear(self, printer_id):
        self._current = (
            self._script[self._i] if self._i < len(self._script) else (self._script[-1:] or [(False, None)])[0]
        )
        self._i += 1
        return self._current[0]

    def get_status(self, printer_id):
        return self._current[1]

    def set_awaiting_plate_clear(self, printer_id, awaiting, source_subtask_id=None):
        self.clear_calls.append((printer_id, awaiting, source_subtask_id))


class TestWatchGateEscalationOnly:
    """The foreign-deposit gate watch: holds the gate, escalates once, exits on
    external clear or stale — and NEVER releases the gate itself."""

    async def test_exits_when_gate_cleared_externally(self):
        mgr = _FakeGateManager([(True, _status(60)), (False, None)])
        notify = _NotifyRecorder()
        outcome = await watch_gate_escalation_only(
            7, manager=mgr, escalate_s=100, check_interval_s=20, sleep=_noop_sleep, notify=notify
        )
        assert outcome == "cleared"
        assert notify.calls == []  # cleared before the escalation window
        assert mgr.clear_calls == []  # the watch itself never releases

    async def test_escalates_once_then_exits_on_external_clear(self):
        mgr = _FakeGateManager([(True, _status(60)), (True, _status(60)), (True, _status(60)), (False, None)])
        notify = _NotifyRecorder()
        outcome = await watch_gate_escalation_only(
            9, manager=mgr, escalate_s=40, check_interval_s=20, sleep=_noop_sleep, notify=notify
        )
        assert outcome == "cleared"
        assert notify.calls == [9]  # fired exactly once
        assert mgr.clear_calls == []

    async def test_cold_bed_does_not_release_gate(self):
        # The KEY difference from watch_bed_and_clear: even a fully cooled bed does
        # NOT auto-release a foreign gate — only the operator (external clear) does.
        mgr = _FakeGateManager([(True, _status(20)), (True, _status(20)), (False, None)])
        notify = _NotifyRecorder()
        outcome = await watch_gate_escalation_only(
            5, manager=mgr, escalate_s=100, check_interval_s=20, sleep=_noop_sleep, notify=notify
        )
        assert outcome == "cleared"
        assert mgr.clear_calls == []

    async def test_stale_when_disconnected(self):
        mgr = _FakeGateManager([(True, _status(60, connected=False))])
        notify = _NotifyRecorder()
        outcome = await watch_gate_escalation_only(
            3, manager=mgr, escalate_s=100, check_interval_s=20, sleep=_noop_sleep, notify=notify
        )
        assert outcome == "stale"
        assert notify.calls == []
        assert mgr.clear_calls == []

    async def test_escalation_notify_failure_does_not_kill_watch(self):
        mgr = _FakeGateManager([(True, _status(60)), (True, _status(60)), (True, _status(60)), (False, None)])
        notify = _NotifyRecorder(raise_exc=True)
        outcome = await watch_gate_escalation_only(
            6, manager=mgr, escalate_s=40, check_interval_s=20, sleep=_noop_sleep, notify=notify
        )
        assert outcome == "cleared"
        assert notify.calls == [6]  # attempted once; exception swallowed


async def _seed_printer(db_session, *, awaiting, gate_subtask_id, serial):
    from backend.app.models.printer import Printer

    printer = Printer(
        name=f"P-{serial}",
        serial_number=serial,
        ip_address="10.0.0.1",
        access_code="0000",
        model="H2S",
        awaiting_plate_clear=awaiting,
        plate_gate_subtask_id=gate_subtask_id,
    )
    db_session.add(printer)
    await db_session.commit()
    await db_session.refresh(printer)
    return printer


async def _seed_completed_eject_item(db_session, *, printer_id, dispatch_subtask_id):
    from datetime import datetime, timezone

    from backend.app.models.print_queue import PrintQueueItem

    item = PrintQueueItem(
        printer_id=printer_id,
        eject_profile_id=1,
        status="completed",
        first_article=False,
        dispatch_subtask_id=dispatch_subtask_id,
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(item)
    await db_session.commit()
    await db_session.refresh(item)
    return item


class TestRearmRequiresSubtaskMatch:
    """rearm_on_startup only re-arms a gate it can positively tie to the eject job:
    the last-started item's dispatch_subtask_id must equal the printer's persisted
    plate_gate_subtask_id (both non-null). Foreign / pre-migration NULL never re-arm."""

    @staticmethod
    def _patch(monkeypatch, db_session):
        import contextlib

        from backend.app.services.eject import monitor as monitor_mod

        @contextlib.asynccontextmanager
        async def _fake_session():
            yield db_session

        monkeypatch.setattr("backend.app.core.database.async_session", _fake_session, raising=False)
        mon = EjectCooldownMonitor()
        started: list[tuple[int, int]] = []
        monkeypatch.setattr(mon, "_start_watch", lambda pid, qid: started.append((pid, qid)) or True)
        return mon, started

    async def test_rearms_when_subtask_matches(self, db_session, monkeypatch):
        printer = await _seed_printer(db_session, awaiting=True, gate_subtask_id="SUB-1", serial="RE-1")
        item = await _seed_completed_eject_item(db_session, printer_id=printer.id, dispatch_subtask_id="SUB-1")
        mon, started = self._patch(monkeypatch, db_session)
        rearmed = await mon.rearm_on_startup()
        assert rearmed == 1
        assert started == [(printer.id, item.id)]

    async def test_no_rearm_on_subtask_mismatch(self, db_session, monkeypatch):
        printer = await _seed_printer(db_session, awaiting=True, gate_subtask_id="SUB-1", serial="RE-2")
        await _seed_completed_eject_item(db_session, printer_id=printer.id, dispatch_subtask_id="SUB-2")
        mon, started = self._patch(monkeypatch, db_session)
        rearmed = await mon.rearm_on_startup()
        assert rearmed == 0
        assert started == []

    async def test_no_rearm_on_null_gate_source(self, db_session, monkeypatch):
        # A pre-migration / foreign gate has no source id — never auto-clears.
        printer = await _seed_printer(db_session, awaiting=True, gate_subtask_id=None, serial="RE-3")
        await _seed_completed_eject_item(db_session, printer_id=printer.id, dispatch_subtask_id="SUB-9")
        mon, started = self._patch(monkeypatch, db_session)
        rearmed = await mon.rearm_on_startup()
        assert rearmed == 0
        assert started == []


class TestActiveWatch:
    """Phase 4.3c: the monitor exposes the in-flight cooldown watch's release
    threshold so the UI can render the cooldown phase. The escalation-only
    (foreign-gate) watch and a still-resolving watch expose None."""

    def test_none_when_nothing_armed(self):
        mon = EjectCooldownMonitor()
        assert mon.active_watch(7) is None

    @pytest.mark.asyncio
    async def test_cooldown_watch_exposes_threshold_then_clears(self, monkeypatch):
        import backend.app.services.eject.monitor as monitor_mod

        mon = EjectCooldownMonitor()
        seen: dict[str, float | None] = {}

        async def fake_resolve(qid):
            assert qid == 42
            return 33.0

        async def fake_watch(pid, threshold):
            # Mid-watch, the threshold is visible to status consumers.
            seen["mid"] = mon.active_watch(pid)
            return "cleared"

        monkeypatch.setattr(monitor_mod, "_resolve_eject_threshold", fake_resolve)
        monkeypatch.setattr(monitor_mod, "watch_bed_and_clear", fake_watch)
        mon._watching[7] = None  # what _start_watch records before spawning
        await mon._watch(7, 42)
        assert seen["mid"] == 33.0
        assert mon.active_watch(7) is None  # popped when the watch exits

    @pytest.mark.asyncio
    async def test_non_eject_item_exposes_nothing(self, monkeypatch):
        import backend.app.services.eject.monitor as monitor_mod

        mon = EjectCooldownMonitor()

        async def fake_resolve(qid):
            return None  # not an eject job

        monkeypatch.setattr(monitor_mod, "_resolve_eject_threshold", fake_resolve)
        mon._watching[7] = None
        await mon._watch(7, 42)
        assert mon.active_watch(7) is None

    def test_escalation_only_watch_has_no_threshold_and_dedupes(self, monkeypatch):
        import backend.app.services.eject.monitor as monitor_mod

        def fake_spawn(coro, name=None):
            coro.close()  # never run — avoid "never awaited" warnings
            return None

        monkeypatch.setattr(monitor_mod, "spawn_background_task", fake_spawn)
        mon = EjectCooldownMonitor()
        assert mon.start_escalation_only_watch(7) is True
        assert mon.active_watch(7) is None  # sentinel: gate held, no cooldown target
        assert mon.start_escalation_only_watch(7) is False  # deduped
        assert mon._start_watch(7, 42) is False  # cooldown watch also deduped

    def test_printer_state_payload_helper(self):
        # printer_manager exposes the watch as {"threshold_c": t} / None.
        from backend.app.services.eject.monitor import eject_cooldown_monitor
        from backend.app.services.printer_manager import _eject_watch_payload

        assert _eject_watch_payload(None) is None
        assert _eject_watch_payload(901) is None
        eject_cooldown_monitor._watching[901] = 33.0
        try:
            assert _eject_watch_payload(901) == {"threshold_c": 33.0}
            eject_cooldown_monitor._watching[901] = None  # escalation-only sentinel
            assert _eject_watch_payload(901) is None
        finally:
            eject_cooldown_monitor._watching.pop(901, None)
