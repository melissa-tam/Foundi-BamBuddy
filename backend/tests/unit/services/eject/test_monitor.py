"""State-transition tests for the cooldown-verified plate-clear monitor."""

import asyncio
from types import SimpleNamespace

import pytest

from backend.app.services.eject.monitor import (
    EjectCooldownMonitor,
    _ActiveWatch,
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
    records set_awaiting_plate_clear calls.

    ``awaiting`` scripts the plate-clear gate the cooldown watch now checks at the
    top of every poll: a bool constant (default True — gate raised for the whole
    watch), or a list consumed one value per poll (last value repeats) so a test
    can drop the gate mid-watch and prove the watch exits ``"cleared"``."""

    def __init__(self, statuses, awaiting=True):
        self._statuses = list(statuses)
        self._i = 0
        self.clear_calls = []
        self._awaiting = awaiting
        self._gate_i = 0

    def get_status(self, printer_id):
        if self._i < len(self._statuses):
            s = self._statuses[self._i]
        else:
            s = self._statuses[-1] if self._statuses else None
        self._i += 1
        return s

    def is_awaiting_plate_clear(self, printer_id):
        if isinstance(self._awaiting, bool):
            return self._awaiting
        val = self._awaiting[self._gate_i] if self._gate_i < len(self._awaiting) else self._awaiting[-1]
        self._gate_i += 1
        return val

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
    """Injectable notify callable: records printer_ids (and the live bed the cooldown
    watch passes) and optionally raises. Accepts the foreign-gate watch's bare
    ``notify(printer_id)`` and the cooldown watch's ``notify(printer_id, bed_c=...)``."""

    def __init__(self, raise_exc: bool = False):
        self.calls: list[int] = []
        self.bed_calls: list[float | None] = []
        self._raise = raise_exc

    async def __call__(self, printer_id, *, bed_c=None):
        self.calls.append(printer_id)
        self.bed_calls.append(bed_c)
        if self._raise:
            raise RuntimeError("notify boom")


class _ReleaseRecorder:
    """Injectable on_release: records each call and optionally raises (dispatch fail)."""

    def __init__(self, raise_times: int = 0):
        self.calls = 0
        self._raise_times = raise_times

    async def __call__(self):
        self.calls += 1
        if self.calls <= self._raise_times:
            raise RuntimeError("dispatch boom")


class _StallRecorder:
    """Injectable on_stall: records the reasons it was invoked with."""

    def __init__(self):
        self.reasons: list[str] = []

    async def __call__(self, reason):
        self.reasons.append(reason)


class TestWatchBedAndClear:
    """The reworked policy: bed ≤ threshold dispatches the eject (on_release) and
    the monitor NEVER clears the plate gate itself; plateau / cap / dispatch-retry
    drive the stall/cap paths. Returns released|stalled|stale."""

    async def test_releases_when_bed_reaches_threshold(self):
        mgr = _FakeManager([_status(60), _status(40), _status(27)])
        rel, stall = _ReleaseRecorder(), _StallRecorder()
        outcome = await watch_bed_and_clear(
            7,
            28.0,
            manager=mgr,
            escalate_s=100,
            check_interval_s=20,
            sleep=_noop_sleep,
            on_release=rel,
            on_stall=stall,
        )
        assert outcome == "released"
        assert rel.calls == 1  # dispatched exactly once
        assert mgr.clear_calls == []  # monitor NEVER clears the gate now
        assert stall.reasons == []

    async def test_releases_at_exact_threshold(self):
        mgr = _FakeManager([_status(28.0)])
        rel = _ReleaseRecorder()
        outcome = await watch_bed_and_clear(
            3, 28.0, manager=mgr, escalate_s=100, check_interval_s=20, sleep=_noop_sleep, on_release=rel
        )
        assert outcome == "released"
        assert rel.calls == 1
        assert mgr.clear_calls == []

    async def test_dispatch_failure_retries_then_stalls_after_three(self):
        # on_release raises every time (dispatch keeps failing). The watch retries
        # on each poll; after the THIRD consecutive failure it stalls.
        mgr = _FakeManager([_status(27)])  # always at threshold
        rel, stall = _ReleaseRecorder(raise_times=99), _StallRecorder()
        outcome = await watch_bed_and_clear(
            9,
            28.0,
            manager=mgr,
            escalate_s=1000,
            check_interval_s=20,
            sleep=_noop_sleep,
            on_release=rel,
            on_stall=stall,
        )
        assert outcome == "stalled"
        assert rel.calls == 3  # retried until the third failure
        assert stall.reasons == ["eject dispatch failed ×3"]

    async def test_dispatch_failure_then_success_releases(self):
        # Two failures then the poll's retry succeeds → released, no stall.
        mgr = _FakeManager([_status(27)])
        rel, stall = _ReleaseRecorder(raise_times=2), _StallRecorder()
        outcome = await watch_bed_and_clear(
            9,
            28.0,
            manager=mgr,
            escalate_s=1000,
            check_interval_s=20,
            sleep=_noop_sleep,
            on_release=rel,
            on_stall=stall,
        )
        assert outcome == "released"
        assert rel.calls == 3  # 2 fail + 1 success
        assert stall.reasons == []

    async def test_plateau_two_strikes_stalls(self):
        # Bed stuck at 50 (above 28); window 40s, epsilon 1.0. Boundaries at 40/80 →
        # two strikes (never cooled) → stalled, NO release.
        mgr = _FakeManager([_status(50)] * 10)
        rel, stall = _ReleaseRecorder(), _StallRecorder()
        outcome = await watch_bed_and_clear(
            3,
            28.0,
            manager=mgr,
            escalate_s=1000,
            check_interval_s=20,
            sleep=_noop_sleep,
            stall_window_s=40,
            stall_epsilon_c=1.0,
            on_release=rel,
            on_stall=stall,
        )
        assert outcome == "stalled"
        assert rel.calls == 0  # never ejected onto a bed that won't cool
        assert len(stall.reasons) == 1

    async def test_plateau_reset_on_cooling_window_never_stalls(self):
        # Steady cool 0.6°C/poll with epsilon 1.0: a window that cools ≥ epsilon
        # resets the strike streak, so a steadily-cooling bed NEVER false-stalls —
        # it eventually crosses the threshold and releases.
        temps = [60 - 0.6 * i for i in range(80)]
        mgr = _FakeManager([_status(t) for t in temps])
        rel, stall = _ReleaseRecorder(), _StallRecorder()
        outcome = await watch_bed_and_clear(
            4,
            28.0,
            manager=mgr,
            escalate_s=100000,
            check_interval_s=20,
            sleep=_noop_sleep,
            stall_window_s=20,
            stall_epsilon_c=1.0,
            on_release=rel,
            on_stall=stall,
        )
        assert outcome == "released"
        assert stall.reasons == []
        assert rel.calls == 1

    async def test_rising_bed_counts_as_plateau_strike(self):
        # A bed that RISES (anchor - bed < 0 < epsilon) strikes twice → stalled.
        mgr = _FakeManager([_status(50), _status(51), _status(52), _status(53), _status(54)])
        rel, stall = _ReleaseRecorder(), _StallRecorder()
        outcome = await watch_bed_and_clear(
            3,
            28.0,
            manager=mgr,
            escalate_s=1000,
            check_interval_s=20,
            sleep=_noop_sleep,
            stall_window_s=20,
            stall_epsilon_c=1.0,
            on_release=rel,
            on_stall=stall,
        )
        assert outcome == "stalled"
        assert rel.calls == 0

    async def test_epsilon_boundary_equal_is_a_reset_not_a_strike(self):
        # Rule is `< epsilon` strikes, so cooling EXACTLY epsilon per window is a
        # reset (progress). Cool exactly 1.0/window with epsilon 1.0 → never stalls;
        # eventually crosses threshold and releases.
        temps = [50 - 1.0 * i for i in range(40)]
        mgr = _FakeManager([_status(t) for t in temps])
        rel, stall = _ReleaseRecorder(), _StallRecorder()
        outcome = await watch_bed_and_clear(
            3,
            28.0,
            manager=mgr,
            escalate_s=100000,
            check_interval_s=20,
            sleep=_noop_sleep,
            stall_window_s=20,
            stall_epsilon_c=1.0,
            on_release=rel,
            on_stall=stall,
        )
        assert outcome == "released"
        assert stall.reasons == []

    async def test_window_zero_disables_plateau_watchdog(self):
        # stall_window_s=0 → plateau never evaluated; a stuck bed with no cap never
        # false-stalls and never ejects. The watch's lifetime is the gated phase, so
        # it ends only when the plate-clear gate drops ("cleared").
        mgr = _FakeManager([_status(50)] * 5, awaiting=[True, True, True, False])
        rel, stall = _ReleaseRecorder(), _StallRecorder()
        outcome = await watch_bed_and_clear(
            3,
            28.0,
            manager=mgr,
            escalate_s=100000,
            check_interval_s=20,
            sleep=_noop_sleep,
            stall_window_s=0,
            stall_epsilon_c=1.0,
            max_hold_s=0,
            on_release=rel,
            on_stall=stall,
        )
        assert outcome == "cleared"
        assert stall.reasons == []
        assert rel.calls == 0

    async def test_cap_fires_release_above_threshold(self):
        # No plateau watchdog (window 0); bed stuck at 50 above 28; cap 60s →
        # dispatch the eject anyway at the cap.
        mgr = _FakeManager([_status(50)] * 10)
        rel, stall = _ReleaseRecorder(), _StallRecorder()
        outcome = await watch_bed_and_clear(
            5,
            28.0,
            manager=mgr,
            escalate_s=100000,
            check_interval_s=20,
            sleep=_noop_sleep,
            stall_window_s=0,
            max_hold_s=60,
            on_release=rel,
            on_stall=stall,
        )
        assert outcome == "released"
        assert rel.calls == 1
        assert stall.reasons == []

    async def test_cap_zero_never_forces_release(self):
        # max_hold_s=0 → no cap; a stuck bed with no plateau watchdog never ejects.
        # It runs until the plate-clear gate drops (phase end), never forcing a release.
        mgr = _FakeManager([_status(50)] * 5, awaiting=[True, True, True, False])
        rel = _ReleaseRecorder()
        outcome = await watch_bed_and_clear(
            5,
            28.0,
            manager=mgr,
            escalate_s=100000,
            check_interval_s=20,
            sleep=_noop_sleep,
            stall_window_s=0,
            max_hold_s=0,
            on_release=rel,
        )
        assert outcome == "cleared"
        assert rel.calls == 0

    async def test_plateau_evaluated_before_cap_at_shared_boundary(self):
        # A poll that is BOTH the plateau's 2nd-strike boundary AND at the cap must
        # STALL (plateau first), not eject. window 40s → 2nd strike at 80s; cap 80s
        # coincides → plateau wins.
        mgr = _FakeManager([_status(50)] * 10)
        rel, stall = _ReleaseRecorder(), _StallRecorder()
        outcome = await watch_bed_and_clear(
            5,
            28.0,
            manager=mgr,
            escalate_s=100000,
            check_interval_s=20,
            sleep=_noop_sleep,
            stall_window_s=40,
            stall_epsilon_c=1.0,
            max_hold_s=80,
            on_release=rel,
            on_stall=stall,
        )
        assert outcome == "stalled"
        assert rel.calls == 0

    async def test_none_status_survives_and_later_releases(self):
        # A None status tick = an unreadable bed, NOT a stop: the watch keeps polling
        # and the later readable crossing still dispatches the eject.
        mgr = _FakeManager([None, None, _status(27)])
        rel = _ReleaseRecorder()
        outcome = await watch_bed_and_clear(
            9, 28.0, manager=mgr, escalate_s=100000, check_interval_s=20, sleep=_noop_sleep, on_release=rel
        )
        assert outcome == "released"
        assert rel.calls == 1
        assert mgr.clear_calls == []  # monitor never clears the gate itself

    async def test_disconnect_survives_and_later_releases(self):
        # A disconnected tick = an unreadable bed; the watch keeps polling and releases
        # once the printer reconnects with a bed at/below threshold.
        mgr = _FakeManager([_status(60, connected=False), _status(60, connected=False), _status(27)])
        rel = _ReleaseRecorder()
        outcome = await watch_bed_and_clear(
            9, 28.0, manager=mgr, escalate_s=100000, check_interval_s=20, sleep=_noop_sleep, on_release=rel
        )
        assert outcome == "released"
        assert rel.calls == 1
        assert mgr.clear_calls == []

    async def test_plateau_near_threshold_releases_not_quarantines(self):
        # Bed asymptotically settles at 30°C — it plateaus (won't cool further) but is
        # only 2°C above the 28°C threshold, within the 3°C eject margin. The two-armed
        # plateau RELEASES (equilibrated at ambient) instead of quarantining.
        mgr = _FakeManager([_status(30)] * 10)
        rel, stall = _ReleaseRecorder(), _StallRecorder()
        outcome = await watch_bed_and_clear(
            7,
            28.0,
            manager=mgr,
            escalate_s=100000,
            check_interval_s=20,
            sleep=_noop_sleep,
            stall_window_s=40,
            stall_epsilon_c=1.0,
            plateau_eject_margin_c=3.0,
            on_release=rel,
            on_stall=stall,
        )
        assert outcome == "released"
        assert rel.calls == 1  # dispatched the eject
        assert stall.reasons == []  # NOT quarantined

    async def test_plateau_far_above_threshold_still_quarantines(self):
        # Bed plateaus at 40°C, 12°C above the 28°C threshold and well past the 3°C
        # margin — genuinely stuck hot → quarantine, NO eject.
        mgr = _FakeManager([_status(40)] * 10)
        rel, stall = _ReleaseRecorder(), _StallRecorder()
        outcome = await watch_bed_and_clear(
            7,
            28.0,
            manager=mgr,
            escalate_s=100000,
            check_interval_s=20,
            sleep=_noop_sleep,
            stall_window_s=40,
            stall_epsilon_c=1.0,
            plateau_eject_margin_c=3.0,
            on_release=rel,
            on_stall=stall,
        )
        assert outcome == "stalled"
        assert rel.calls == 0
        assert len(stall.reasons) == 1

    async def test_gate_cleared_midwatch_exits_cleared_before_dispatch(self):
        # An operator clears the plate mid-cooldown. The bed would reach threshold on
        # poll 2's status (27°C), but the gate-check at the top of poll 2 exits
        # "cleared" BEFORE that bed is ever read — so NO eject is dispatched onto the
        # now-empty plate (the eject-onto-cleared-plate latent bug is closed).
        mgr = _FakeManager([_status(50), _status(27)], awaiting=[True, False])
        rel = _ReleaseRecorder()
        outcome = await watch_bed_and_clear(
            7, 28.0, manager=mgr, escalate_s=100000, check_interval_s=20, sleep=_noop_sleep, on_release=rel
        )
        assert outcome == "cleared"
        assert rel.calls == 0  # never ejected onto the cleared plate

    async def test_escalates_once_then_keeps_watching_until_release(self):
        # Hot past the escalate window, THEN cools. escalate_s=40, interval=20 →
        # escalation fires at elapsed==40, watch continues, and the later crossing
        # still dispatches the eject (released).
        mgr = _FakeManager([_status(60), _status(60), _status(60), _status(27)])
        notify, rel = _NotifyRecorder(), _ReleaseRecorder()
        outcome = await watch_bed_and_clear(
            5, 33.0, manager=mgr, escalate_s=40, check_interval_s=20, sleep=_noop_sleep, notify=notify, on_release=rel
        )
        assert outcome == "released"  # watch did NOT stop at the escalate window
        assert rel.calls == 1
        assert mgr.clear_calls == []
        assert notify.calls == [5]  # fired exactly once

    async def test_escalation_notify_failure_does_not_kill_watch(self):
        mgr = _FakeManager([_status(60), _status(60), _status(60), _status(27)])
        notify, rel = _NotifyRecorder(raise_exc=True), _ReleaseRecorder()
        outcome = await watch_bed_and_clear(
            6, 33.0, manager=mgr, escalate_s=40, check_interval_s=20, sleep=_noop_sleep, notify=notify, on_release=rel
        )
        assert outcome == "released"
        assert rel.calls == 1
        assert notify.calls == [6]  # attempted once, exception did not propagate

    async def test_disconnect_does_not_kill_watch_gate_clear_ends_it(self):
        # A never-cooling bed that later goes offline does NOT end the watch — a
        # disconnected tick is survived (unreadable bed). The watch's lifetime is the
        # gated phase, so it ends only when the plate-clear gate drops ("cleared").
        mgr = _FakeManager(
            [_status(60), _status(60), _status(60, connected=False)],
            awaiting=[True, True, True, False],
        )
        notify = _NotifyRecorder()
        outcome = await watch_bed_and_clear(
            9, 28.0, manager=mgr, escalate_s=100000, check_interval_s=20, sleep=_noop_sleep, notify=notify
        )
        assert outcome == "cleared"
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
    """The foreign-deposit gate watch: holds the gate, escalates once, exits ONLY on
    external clear (a disconnected tick no longer aborts) — and NEVER releases the
    gate itself."""

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

    async def test_disconnected_tick_keeps_polling_then_escalates_and_clears(self):
        # F3: a disconnected/stale tick no longer ABORTS the foreign-gate watch (the
        # old "stale" exit is gone — it stranded printers that briefly dropped off).
        # The gate is held for the whole PHASE: the watch keeps polling across the
        # disconnect, escalates ONCE, and exits only when the gate is cleared.
        mgr = _FakeGateManager(
            [
                (True, _status(60)),  # tick 0 — elapsed 0
                (True, _status(60, connected=False)),  # tick 1 — DISCONNECTED, must NOT abort
                (True, _status(60)),  # tick 2 — elapsed 40 → escalation window
                (False, None),  # tick 3 — operator clears the gate
            ]
        )
        notify = _NotifyRecorder()
        outcome = await watch_gate_escalation_only(
            4, manager=mgr, escalate_s=40, check_interval_s=20, sleep=_noop_sleep, notify=notify
        )
        assert outcome == "cleared"
        assert notify.calls == [4]  # escalated exactly once despite the mid-hold disconnect
        assert mgr.clear_calls == []  # the watch itself never releases

    async def test_escalation_notify_failure_does_not_kill_watch(self):
        mgr = _FakeGateManager([(True, _status(60)), (True, _status(60)), (True, _status(60)), (False, None)])
        notify = _NotifyRecorder(raise_exc=True)
        outcome = await watch_gate_escalation_only(
            6, manager=mgr, escalate_s=40, check_interval_s=20, sleep=_noop_sleep, notify=notify
        )
        assert outcome == "cleared"
        assert notify.calls == [6]  # attempted once; exception swallowed


async def _seed_printer(db_session, *, awaiting, gate_subtask_id, serial, quarantined=False):
    from backend.app.models.printer import Printer

    printer = Printer(
        name=f"P-{serial}",
        serial_number=serial,
        ip_address="10.0.0.1",
        access_code="0000",
        model="H2S",
        awaiting_plate_clear=awaiting,
        plate_gate_subtask_id=gate_subtask_id,
        quarantined=quarantined,
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
    """rearm_on_startup only re-arms a COOLDOWN watch on a gate it can positively tie
    to the eject job: the last-started item's dispatch_subtask_id must equal the
    printer's persisted plate_gate_subtask_id (both non-null). A gate it CANNOT tie
    (foreign / pre-migration NULL / mismatch) never auto-clears — but instead of being
    left watch-less (a silent stall) it arms the escalation-only hold (F3). A
    quarantined printer is excluded entirely."""

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
        escalated: list[int] = []
        monkeypatch.setattr(mon, "_start_watch", lambda pid, qid: started.append((pid, qid)) or True)
        monkeypatch.setattr(mon, "start_escalation_only_watch", lambda pid: escalated.append(pid) or True)
        return mon, started, escalated

    async def test_rearms_when_subtask_matches(self, db_session, monkeypatch):
        printer = await _seed_printer(db_session, awaiting=True, gate_subtask_id="SUB-1", serial="RE-1")
        item = await _seed_completed_eject_item(db_session, printer_id=printer.id, dispatch_subtask_id="SUB-1")
        mon, started, escalated = self._patch(monkeypatch, db_session)
        rearmed = await mon.rearm_on_startup()
        assert rearmed == 1
        assert started == [(printer.id, item.id)]
        assert escalated == []  # a cooldown re-arm never also arms escalation-only

    async def test_escalation_only_on_subtask_mismatch(self, db_session, monkeypatch):
        # Gate-subtask mismatch → NOT cooldown-re-armed, but NOT left watch-less: the
        # stranded plate arms the escalation-only hold instead (rearmed count stays 0).
        printer = await _seed_printer(db_session, awaiting=True, gate_subtask_id="SUB-1", serial="RE-2")
        await _seed_completed_eject_item(db_session, printer_id=printer.id, dispatch_subtask_id="SUB-2")
        mon, started, escalated = self._patch(monkeypatch, db_session)
        rearmed = await mon.rearm_on_startup()
        assert rearmed == 0
        assert started == []
        assert escalated == [printer.id]

    async def test_escalation_only_on_null_gate_source(self, db_session, monkeypatch):
        # A pre-migration / foreign gate has no source id — never auto-clears, but the
        # gate must still escalate rather than sit silently → escalation-only armed.
        printer = await _seed_printer(db_session, awaiting=True, gate_subtask_id=None, serial="RE-3")
        await _seed_completed_eject_item(db_session, printer_id=printer.id, dispatch_subtask_id="SUB-9")
        mon, started, escalated = self._patch(monkeypatch, db_session)
        rearmed = await mon.rearm_on_startup()
        assert rearmed == 0
        assert started == []
        assert escalated == [printer.id]

    async def test_escalation_only_when_no_prior_item(self, db_session, monkeypatch):
        # A gate raised with NO last-started item at all (e.g. a native-vision plate
        # gate) still escalates rather than sitting watch-less.
        printer = await _seed_printer(db_session, awaiting=True, gate_subtask_id="SUB-1", serial="RE-N")
        mon, started, escalated = self._patch(monkeypatch, db_session)
        rearmed = await mon.rearm_on_startup()
        assert rearmed == 0
        assert started == []
        assert escalated == [printer.id]

    async def test_no_rearm_when_printer_quarantined(self, db_session, monkeypatch):
        # A quarantined printer is excluded from the query entirely — with a matching
        # gate it re-arms NOTHING (not even escalation-only): its plate stays gated for
        # a human and quarantine handling owns it.
        printer = await _seed_printer(
            db_session, awaiting=True, gate_subtask_id="SUB-1", serial="RE-Q", quarantined=True
        )
        await _seed_completed_eject_item(db_session, printer_id=printer.id, dispatch_subtask_id="SUB-1")
        mon, started, escalated = self._patch(monkeypatch, db_session)
        rearmed = await mon.rearm_on_startup()
        assert rearmed == 0
        assert started == []
        assert escalated == []


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

        async def fake_resolve(qid, *, for_first_article=False):
            assert qid == 42
            assert for_first_article is False  # production watch keeps the FA guard
            return 33.0

        async def fake_settings():
            return (0, 1.0, 0, 3.0)  # isolate from the settings DB — not under test here

        async def fake_watch(pid, threshold, **kwargs):
            # Mid-watch, the threshold is visible to status consumers.
            seen["mid"] = mon.active_watch(pid)
            return "released"

        monkeypatch.setattr(monitor_mod, "_resolve_eject_threshold", fake_resolve)
        monkeypatch.setattr(monitor_mod, "_resolve_stall_settings", fake_settings)
        monkeypatch.setattr(monitor_mod, "watch_bed_and_clear", fake_watch)
        mon._watching[7] = None  # what _start_watch records before spawning
        await mon._watch(7, 42)
        assert seen["mid"] == 33.0
        assert mon.active_watch(7) is None  # popped when the watch exits

    @pytest.mark.asyncio
    async def test_non_eject_item_exposes_nothing(self, monkeypatch):
        import backend.app.services.eject.monitor as monitor_mod

        mon = EjectCooldownMonitor()

        async def fake_resolve(qid, *, for_first_article=False):
            return None  # not an eject job

        monkeypatch.setattr(monitor_mod, "_resolve_eject_threshold", fake_resolve)
        mon._watching[7] = None
        await mon._watch(7, 42)
        assert mon.active_watch(7) is None

    @pytest.mark.asyncio
    async def test_fa_watch_resolves_fa_threshold_and_releases_into_fa_dispatch(self, monkeypatch):
        """start_fa_eject_watch → _watch(purpose='fa'): the FA guard is skipped
        (for_first_article=True) and the release action is the FA dispatcher."""
        import backend.app.services.eject.monitor as monitor_mod

        mon = EjectCooldownMonitor()
        seen: dict[str, object] = {}

        async def fake_resolve(qid, *, for_first_article=False):
            seen["resolve"] = (qid, for_first_article)
            return 33.0

        async def fake_settings():
            return (0, 1.0, 0, 3.0)  # isolate from the settings DB — not under test here

        async def fake_watch(pid, threshold, **kwargs):
            seen["threshold"] = threshold
            await kwargs["on_release"]()  # release fires the bound FA dispatch
            return "released"

        async def fake_fa_dispatch(*, printer_id, queue_item_id, run_id):
            seen["fa_dispatch"] = (printer_id, queue_item_id, run_id)

        monkeypatch.setattr(monitor_mod, "_resolve_eject_threshold", fake_resolve)
        monkeypatch.setattr(monitor_mod, "_resolve_stall_settings", fake_settings)
        monkeypatch.setattr(monitor_mod, "watch_bed_and_clear", fake_watch)
        monkeypatch.setattr(monitor_mod, "_dispatch_fa_eject", fake_fa_dispatch)
        mon._watching[7] = None  # what start_fa_eject_watch records before spawning
        await mon._watch(7, 42, purpose="fa", run_id=9)
        assert seen["resolve"] == (42, True)
        assert seen["threshold"] == 33.0
        assert seen["fa_dispatch"] == (7, 42, 9)
        assert mon.active_watch(7) is None  # popped on exit

    @pytest.mark.asyncio
    async def test_stall_settings_read_failure_falls_back_to_schema_defaults(self, monkeypatch):
        """A settings-store failure at arm time must arm with schema defaults,
        never kill the watch (a dead watch strands the plate-clear gate)."""
        import backend.app.core.database as db_mod
        import backend.app.services.eject.monitor as monitor_mod
        from backend.app.schemas.settings import AppSettings

        def broken_session():
            raise RuntimeError("settings DB unavailable")

        monkeypatch.setattr(db_mod, "async_session", broken_session)
        window_s, epsilon, max_hold_s, margin = await monitor_mod._resolve_stall_settings()
        fields = AppSettings.model_fields
        assert window_s == int(fields["farm_cooldown_stall_window_minutes"].default) * 60
        assert epsilon == float(fields["farm_cooldown_stall_epsilon_c"].default)
        assert max_hold_s == int(fields["farm_cooldown_max_hold_minutes"].default) * 60
        assert margin == float(fields["farm_cooldown_plateau_eject_margin_c"].default)

    def test_start_fa_eject_watch_dedupes_against_inflight_watch(self, monkeypatch):
        import backend.app.services.eject.monitor as monitor_mod

        def fake_spawn(coro, name=None):
            coro.close()
            return None

        monkeypatch.setattr(monitor_mod, "spawn_background_task", fake_spawn)
        mon = EjectCooldownMonitor()
        assert mon.start_fa_eject_watch(7, 42, 9) is True
        assert mon.start_fa_eject_watch(7, 42, 9) is False  # deduped
        mon._watching.pop(7, None)

    @pytest.mark.asyncio
    async def test_foreign_watch_uses_direct_threshold_and_releases_into_foreign_dispatch(self, monkeypatch):
        """start_foreign_eject_watch → _watch(purpose='foreign'): the threshold is
        passed DIRECTLY (no queue item, so _resolve_eject_threshold is NEVER called),
        the watch exposes it, and the release action is the foreign-plate dispatcher
        bound to the chosen profile (F5)."""
        import backend.app.services.eject.manual as manual_mod
        import backend.app.services.eject.monitor as monitor_mod

        mon = EjectCooldownMonitor()
        seen: dict[str, object] = {}

        async def fake_resolve(qid, *, for_first_article=False):
            seen["resolve_called"] = True  # must NOT run for the direct-threshold path
            return 99.0

        async def fake_settings():
            return (0, 1.0, 0, 3.0)  # isolate from the settings DB

        async def fake_watch(pid, threshold, **kwargs):
            seen["threshold"] = threshold
            seen["mid"] = mon.active_watch(pid)  # visible to status consumers mid-watch
            await kwargs["on_release"]()  # release fires the bound foreign dispatch
            return "released"

        async def fake_foreign_dispatch(*, printer_id, profile_id):
            seen["dispatch"] = (printer_id, profile_id)

        monkeypatch.setattr(monitor_mod, "_resolve_eject_threshold", fake_resolve)
        monkeypatch.setattr(monitor_mod, "_resolve_stall_settings", fake_settings)
        monkeypatch.setattr(monitor_mod, "watch_bed_and_clear", fake_watch)
        # The foreign on_release lazy-imports this name from the manual module.
        monkeypatch.setattr(manual_mod, "dispatch_identified_foreign_eject", fake_foreign_dispatch)
        mon._watching[7] = None  # what start_foreign_eject_watch records before spawning
        await mon._watch(7, None, purpose="foreign", threshold_override=33.0, profile_id=5)
        assert seen["threshold"] == 33.0
        assert seen["mid"] == 33.0  # foreign watch exposes its release threshold to the UI
        assert seen["dispatch"] == (7, 5)
        assert "resolve_called" not in seen  # direct threshold skips _resolve_eject_threshold
        assert mon.active_watch(7) is None  # popped on exit

    def test_start_foreign_eject_watch_dedupes_against_inflight_watch(self, monkeypatch):
        import backend.app.services.eject.monitor as monitor_mod

        def fake_spawn(coro, name=None):
            coro.close()  # never run — avoid "never awaited" warnings
            return None

        monkeypatch.setattr(monitor_mod, "spawn_background_task", fake_spawn)
        mon = EjectCooldownMonitor()
        assert mon.start_foreign_eject_watch(7, 5, 33.0) is True
        assert mon.active_watch(7) is None  # sentinel until the watch resolves (mirrors prod/FA)
        assert mon.start_foreign_eject_watch(7, 5, 33.0) is False  # deduped
        assert mon.start_escalation_only_watch(7) is False  # any other watch also deduped
        mon._watching.pop(7, None)

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


class TestResolveStallSettings:
    """The plateau/cap policy numbers are resolved from farm settings at arm, with
    fallbacks that come from the AppSettings schema defaults (single origin)."""

    @staticmethod
    def _patch_session(monkeypatch, db_session):
        import contextlib

        @contextlib.asynccontextmanager
        async def _fake_session():
            yield db_session

        monkeypatch.setattr("backend.app.core.database.async_session", _fake_session, raising=False)

    async def test_defaults_when_no_rows(self, db_session, monkeypatch):
        from backend.app.schemas.settings import AppSettings
        from backend.app.services.eject import monitor as monitor_mod

        self._patch_session(monkeypatch, db_session)
        window_s, epsilon, max_hold_s, margin = await monitor_mod._resolve_stall_settings()
        fields = AppSettings.model_fields
        assert window_s == fields["farm_cooldown_stall_window_minutes"].default * 60
        assert epsilon == fields["farm_cooldown_stall_epsilon_c"].default
        assert max_hold_s == fields["farm_cooldown_max_hold_minutes"].default * 60
        assert margin == fields["farm_cooldown_plateau_eject_margin_c"].default  # default 3.0 fallback

    async def test_reads_settings_rows_and_converts_minutes(self, db_session, monkeypatch):
        from backend.app.api.routes.settings import set_setting
        from backend.app.services.eject import monitor as monitor_mod

        self._patch_session(monkeypatch, db_session)
        await set_setting(db_session, "farm_cooldown_stall_window_minutes", "10")
        await set_setting(db_session, "farm_cooldown_stall_epsilon_c", "2.5")
        await set_setting(db_session, "farm_cooldown_max_hold_minutes", "0")  # 0 disables the cap
        await set_setting(db_session, "farm_cooldown_plateau_eject_margin_c", "4.5")
        window_s, epsilon, max_hold_s, margin = await monitor_mod._resolve_stall_settings()
        assert window_s == 10 * 60
        assert epsilon == 2.5
        assert max_hold_s == 0
        assert margin == 4.5


class TestManualReleaseNow:
    """W2: an armed watch's release_now event drives an immediate manual eject
    through the SAME _do_release path, bypassing the cooldown threshold."""

    @pytest.mark.asyncio
    async def test_preset_event_releases_even_hot(self):
        # Bed 60 is well ABOVE the 30 threshold — a normal poll would NOT release.
        # The pre-set release_now event fires the manual release on the first poll.
        event = asyncio.Event()
        event.set()
        mgr = _FakeManager([_status(60)] * 3)
        rel = _ReleaseRecorder()
        outcome = await watch_bed_and_clear(
            7,
            30.0,
            manager=mgr,
            escalate_s=100000,
            check_interval_s=20,
            sleep=_noop_sleep,
            on_release=rel,
            release_now=event,
        )
        assert outcome == "released"
        assert rel.calls == 1  # dispatched despite the hot bed
        assert not event.is_set()  # consumed

    @pytest.mark.asyncio
    async def test_no_event_falls_through_to_threshold(self):
        # Without release_now, the same hot bed keeps cooling (no manual release).
        mgr = _FakeManager([_status(60), _status(25)])
        rel = _ReleaseRecorder()
        outcome = await watch_bed_and_clear(
            7, 30.0, manager=mgr, escalate_s=100000, check_interval_s=20, sleep=_noop_sleep, on_release=rel
        )
        assert outcome == "released"
        assert rel.calls == 1  # released only once the bed reached 25 ≤ 30


class TestDoReleaseGateGuard:
    """W2/W3 hardening: _do_release re-checks the plate-clear gate at the release
    boundary. If the gate dropped between the top-of-poll check and here, the watch
    exits 'cleared' and NEVER sweeps an already-emptied plate."""

    @pytest.mark.asyncio
    async def test_gate_dropped_at_release_boundary_exits_cleared(self):
        # awaiting: True at the top-of-poll check, False when _do_release re-checks.
        mgr = _FakeManager([_status(20)], awaiting=[True, False])
        rel = _ReleaseRecorder()
        outcome = await watch_bed_and_clear(
            3, 30.0, manager=mgr, escalate_s=100000, check_interval_s=20, sleep=_noop_sleep, on_release=rel
        )
        assert outcome == "cleared"
        assert rel.calls == 0  # never swept the emptied plate

    @pytest.mark.asyncio
    async def test_gate_up_throughout_releases_normally(self):
        # Sanity: gate stays up → the same bed ≤ threshold releases.
        mgr = _FakeManager([_status(20)], awaiting=True)
        rel = _ReleaseRecorder()
        outcome = await watch_bed_and_clear(
            3, 30.0, manager=mgr, escalate_s=100000, check_interval_s=20, sleep=_noop_sleep, on_release=rel
        )
        assert outcome == "released"
        assert rel.calls == 1


class TestActiveWatchIdentityAndRelease:
    """W2 accessors: active_watch keeps its float|None contract; the new identity +
    release_now accessors expose the _ActiveWatch only for a real cooldown/FA watch."""

    def test_active_watch_identity_only_for_active_watch(self):
        mon = EjectCooldownMonitor()
        assert mon.active_watch_identity(7) is None  # nothing armed
        aw = _ActiveWatch(threshold_c=33.0, queue_item_id=42, purpose="production", release_now=asyncio.Event())
        mon._watching[7] = aw
        assert mon.active_watch(7) == 33.0  # unchanged contract
        assert mon.active_watch_identity(7) is aw
        # None sentinel (escalation-only / resolving) exposes no identity.
        mon._watching[8] = None
        assert mon.active_watch_identity(8) is None
        assert mon.active_watch(8) is None

    def test_request_release_now(self):
        mon = EjectCooldownMonitor()
        assert mon.request_release_now(7) is False  # nothing armed
        aw = _ActiveWatch(threshold_c=33.0, queue_item_id=42, purpose="production", release_now=asyncio.Event())
        mon._watching[7] = aw
        assert mon.request_release_now(7) is True
        assert aw.release_now.is_set()
        # Escalation-only sentinel is not releasable.
        mon._watching[9] = None
        assert mon.request_release_now(9) is False
