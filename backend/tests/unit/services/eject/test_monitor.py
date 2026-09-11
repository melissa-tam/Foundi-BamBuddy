"""State-transition tests for the cooldown-verified plate-clear monitor.

Since the 2026-08-30 cut-over the monitor decides NOTHING about which watch to
arm: it is the plate-occupancy authority's injected POLICY DRIVER. The four
``start_*_watch`` entry points, the ``_watching`` registry they deduped against
and ``on_terminal_status`` are gone with the decision.

So every arming test here drives a REAL transition through the authority
(``note_terminal`` / ``hydrate_plate`` / ``hydrate_eject`` / ``set_policy`` /
``clear_plate`` / ``claim_for_eject`` / ``resolve_eject``) with the monitor wired
as ``policy_driver``, and asserts what the driver made of it — the watch record in
``_armed``, ``active_watch`` and ``request_release_now``. Likewise both watch
bodies read the gate from ``plate_occupancy.is_plate_occupied``, never from the
injected manager (which now supplies the BED only), so the tests drop the gate
through the authority rather than scripting a manager flag.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.app.services.eject import monitor as monitor_mod, remote as eject_remote
from backend.app.services.eject.monitor import (
    EjectCooldownMonitor,
    _ArmedWatch,
    notify_plate_not_empty,
    should_auto_clear,
    should_rearm,
    watch_bed_and_clear,
    watch_gate_escalation_only,
)
from backend.app.services.plate_occupancy import (
    CooldownEject,
    DepositEvidence,
    EscalationOnly,
    Evidence,
    FirstArticleEject,
    ForeignAutoEject,
    PendingEject,
    TerminalDisposition,
    plate_occupancy,
)


def _settings(**overrides) -> monitor_mod.CooldownWatchSettings:
    """The watch's arm-time settings, with the plateau/cap machinery and BOTH cooldown
    fans switched off — every test that uses this is about something else, and reading
    the real settings DB would couple it to a table it never writes.

    The fans are off by their SWITCHES, not by a zero speed: the switch is the one
    on/off owner since the two-fan wave, and a speed of 0 is no longer an encoding of
    "off" anywhere."""
    defaults = {
        "stall_window_s": 0,
        "stall_epsilon_c": 1.0,
        "max_hold_s": 0,
        "plateau_eject_margin_c": 3.0,
        "aux_fan_enabled": False,
        "aux_fan_percent": 100,
        "chamber_fan_enabled": False,
        "chamber_fan_percent": 100,
        "chamber_fan_sustain_percent": 50,
        "hold_enabled": True,
        "hold_part_top_mm": 100,
    }
    defaults.update(overrides)
    return monitor_mod.CooldownWatchSettings(**defaults)


def _settings_with_fans(**overrides) -> monitor_mod.CooldownWatchSettings:
    """The same, with both fan switches ON — the production shape."""
    return _settings(aux_fan_enabled=True, chamber_fan_enabled=True, **overrides)


# What ``CooldownWatchSettings.fans`` composes for the production shape: the value the
# watch hands the prep, asserted against rather than re-spelled at each call site.
FANS_ON = _settings_with_fans().fans


class _PrepRecorder:
    """Stand-in for the cooldown prep: records the arm, and how it was retired.

    A double, not a re-implementation — what ``begin`` decides is pinned end-to-end in
    ``test_cooldown_prep.py``. What matters HERE is only the wiring: that the watch
    arms the prep before it polls, seeds ``hold_z`` from it, and retires it exactly
    once on every exit path.
    """

    def __init__(self, *, hold: str = "skipped:no_numbers", hold_z: float | None = None) -> None:
        self.hold = hold
        self.hold_z = hold_z
        self.begun: list[tuple[int, int | None, object]] = []
        # The operator's two hold inputs as they arrived — the wiring this double exists
        # to prove, since what ``begin`` DOES with them is pinned in test_cooldown_prep.
        self.holds: list[tuple[bool | None, int | None]] = []
        # The other two resolve-once inputs of the two-fan wave: the printer's model
        # (the chamber lane's capability gate) and the release threshold (its step-down
        # line). Both are the watch's to resolve, never the prep's to look up.
        self.models: list[str | None] = []
        self.thresholds: list[float | None] = []
        # Every temperature map the bed poll handed the prep — the boost-end lane.
        self.samples: list[dict] = []
        self.ended: list[bool] = []
        self.order: list[str] = []

    def install(self, monkeypatch) -> None:
        recorder = self

        class _Prep:
            hold = recorder.hold
            hold_z = recorder.hold_z

            async def observe_start(self) -> None:
                recorder.order.append("observe")

            def note_sample(self, temperatures) -> None:
                recorder.samples.append(dict(temperatures))

            def end(self, *, fan_off: bool) -> None:
                recorder.ended.append(fan_off)
                recorder.order.append("end")

        async def fake_begin(printer_id, *, queue_item_id, fans, release_threshold_c, model, **kwargs):
            recorder.begun.append((printer_id, queue_item_id, fans))
            recorder.holds.append((kwargs.get("hold_enabled"), kwargs.get("hold_part_top_mm")))
            recorder.models.append(model)
            recorder.thresholds.append(release_threshold_c)
            recorder.order.append("begin")
            return _Prep()

        monkeypatch.setattr(monitor_mod.cooldown_prep, "begin", fake_begin)


@pytest.fixture
def stub_prep(monkeypatch):
    """A cooldown prep that touches no hardware. Returns the recorder."""
    recorder = _PrepRecorder()
    recorder.install(monkeypatch)
    return recorder


@pytest.fixture(autouse=True)
def _printer_connected(monkeypatch):
    """Every watch here runs against a CONNECTED printer unless the test says otherwise.

    ``_watch`` waits for the MQTT session before it arms the cooldown prep (a restart
    re-arms watches from the hydrated plates BEFORE the lifespan opens the sessions),
    so without an ambient answer a test about anything else would sit in that wait for
    its full bound. The wait itself is pinned in ``TestCooldownPrepWiring``."""
    monkeypatch.setattr(monitor_mod.printer_manager, "is_connected", lambda printer_id: True)


@pytest.fixture(autouse=True)
def _clean_occupancy():
    """Every test starts with an empty fleet and NO injected callables.

    ``reset_for_tests`` un-wires the policy driver too, so a test that does not
    wire the monitor cannot arm a watch by accident — and one that does cannot
    leak an armed watch into the next test.
    """
    plate_occupancy.reset_for_tests()
    yield
    plate_occupancy.reset_for_tests()


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class _FakeTask:
    """Stand-in for the ``asyncio.Task`` ``_arm`` spawns.

    The driver only ever calls ``done()``/``cancel()`` on it, so a watch can be
    armed, identified and cancelled without running any real DB work — which is
    what lets a test assert the task OBJECT is the same one across a
    re-notification (the idempotence pin)."""

    def __init__(self, name: str | None) -> None:
        self.name = name
        self.cancelled = False
        self._done = False

    def done(self) -> bool:
        return self._done

    def cancel(self) -> None:
        self.cancelled = True
        self._done = True


@pytest.fixture()
def spawns(monkeypatch):
    """Record every watch the monitor spawns, without running any of them."""
    records: list[_FakeTask] = []

    def _fake_spawn(coro, *, name=None):
        coro.close()  # never run it — we only count and identify spawns
        task = _FakeTask(name)
        records.append(task)
        return task

    monkeypatch.setattr(monitor_mod, "spawn_background_task", _fake_spawn)
    return records


def _wire(monitor: EjectCooldownMonitor) -> None:
    """Inject ``monitor`` as the authority's policy driver (what lifespan does)."""
    plate_occupancy.configure(policy_driver=monitor.on_occupancy_change)


def _deposit_evidence() -> DepositEvidence:
    """A terminal that unambiguously left a part on the plate."""
    return DepositEvidence(
        final_status="completed",
        is_dry_run=False,
        peaks_reliable=True,
        last_layer_num=42,
        last_progress=100.0,
    )


def _occupy(printer_id: int, policy, *, source: str | None = "SUB-1") -> None:
    """Raise the gate with ``policy`` the way a deposit-bearing terminal does."""
    plate_occupancy.note_terminal(
        printer_id,
        TerminalDisposition(
            queue_item_id=None,
            source_subtask_id=source,
            evidence=_deposit_evidence(),
            policy=policy,
            raise_gate=True,
        ),
    )


def _gate_up(printer_id: int) -> None:
    """Occupy the plate the short way, for the watch-body tests."""
    assert plate_occupancy.declare_occupied(printer_id, Evidence()) is None


def _pending(*, purpose="production", queue_item_id=42, run_id=None) -> PendingEject:
    return PendingEject(purpose=purpose, run_id=run_id, queue_item_id=queue_item_id, expected_runtime_s=83.0)


class _FakeManager:
    """Scripted printer_manager: yields the next status per ``get_status`` call.

    The gate is NO LONGER read through the manager (the authority owns it), so
    this supplies the BED only. ``on_status`` is the seam for the one test that
    must drop the gate BETWEEN the top-of-poll check and ``_do_release``'s
    re-check — the two happen either side of this sync call."""

    def __init__(self, statuses, on_status=None):
        self._statuses = list(statuses)
        self._i = 0
        self._on_status = on_status

    def get_status(self, printer_id):
        if self._i < len(self._statuses):
            s = self._statuses[self._i]
        else:
            s = self._statuses[-1] if self._statuses else None
        self._i += 1
        if self._on_status is not None:
            self._on_status()
        return s


class _ClearAfter:
    """A ``sleep`` stand-in that clears the plate after N polls.

    The watch's phase boundary is the plate-clear gate, checked at the top of
    every poll, so dropping it from the sleep between polls is how a test scripts
    "an operator (or the eject's own terminal) cleared the plate mid-watch".
    ``gate_seen`` records the gate at each poll boundary, which is how a test
    proves the watch itself never released it."""

    def __init__(self, printer_id: int, after_polls: int):
        self.printer_id = printer_id
        self.after_polls = after_polls
        self.calls = 0
        self.gate_seen: list[bool] = []
        self._cleared = False

    async def __call__(self, _seconds):
        self.calls += 1
        self.gate_seen.append(plate_occupancy.is_plate_occupied(self.printer_id))
        if self.calls >= self.after_polls and not self._cleared:
            self._cleared = True
            plate_occupancy.clear_plate(self.printer_id)


def _status(bed, connected=True):
    return SimpleNamespace(connected=connected, temperatures={"bed": bed})


async def _noop_sleep(_seconds):
    return None


class _NotifyRecorder:
    """Injectable notify callable: records printer_ids (and the live bed the cooldown
    watch passes) and optionally raises. Accepts the foreign-gate watch's bare
    ``notify(printer_id)``, the cooldown watch's ``notify(printer_id, bed_c=...)`` and
    the plate-not-empty page's ``notify(printer_id, source_detail=...)``."""

    def __init__(self, raise_exc: bool = False):
        self.calls: list[int] = []
        self.bed_calls: list[float | None] = []
        self.details: list[str] = []
        self._raise = raise_exc

    async def __call__(self, printer_id, *, bed_c=None, source_detail=""):
        self.calls.append(printer_id)
        self.bed_calls.append(bed_c)
        self.details.append(source_detail)
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


# --------------------------------------------------------------------------- #
# Pure classifiers (unchanged by the cut-over)
# --------------------------------------------------------------------------- #
class TestShouldAutoClear:
    def test_completed_clears(self):
        assert should_auto_clear("completed") is True

    @pytest.mark.parametrize("status", ["failed", "aborted", "cancelled", "printing"])
    def test_non_success_does_not_clear(self, status):
        assert should_auto_clear(status) is False


class TestShouldRearm:
    """Startup re-arm decision: gate raised + last job completed + eject profile.

    Still exported and still the one rule — its CALLER moved to
    ``plate_occupancy_store.hydrate()``, which turns the same answer into the
    plate's hydrated policy instead of a watch."""

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


# --------------------------------------------------------------------------- #
# The policy driver
# --------------------------------------------------------------------------- #
class TestPolicyDriverArming:
    """``on_occupancy_change`` is the ONE arming path: the plate's policy decides
    which watch runs, an eject or a clear plate means no watch at all, and a
    re-notification carrying the SAME policy must never respawn."""

    def test_legacy_arming_entry_points_are_gone(self):
        """No second opinion about whether a plate is occupied may grow back."""
        for name in (
            "_watching",
            "_start_watch",
            "on_terminal_status",
            "active_watch_identity",
            "start_fa_eject_watch",
            "start_foreign_eject_watch",
            "start_escalation_only_watch",
            "rearm_on_startup",
        ):
            assert not hasattr(EjectCooldownMonitor, name), name
        for name in ("deposited_nothing", "_default_notify_plate_not_empty", "_latest_started_item"):
            assert not hasattr(monitor_mod, name), name

    def test_occupied_plate_arms_exactly_one_cooldown_watch(self, spawns):
        mon = EjectCooldownMonitor()
        _wire(mon)

        _occupy(7, CooldownEject(unit_id=42, run_id=9))

        assert [t.name for t in spawns] == ["eject-cooldown-watch-7"]
        armed = mon._armed[7]
        assert armed.policy == CooldownEject(unit_id=42, run_id=9)
        assert armed.queue_item_id == 42
        assert armed.release_now is not None  # a releasing policy carries the manual channel
        assert mon.request_release_now(7) is True

    def test_same_policy_renotification_does_not_respawn(self, spawns):
        """The authority fans out on EVERY transition; an identical policy is a no-op.

        Respawning would lose the watch's elapsed cooldown, its plateau anchor and
        its escalation state — which is why the record's identity IS the policy
        (frozen dataclasses, structural equality)."""
        mon = EjectCooldownMonitor()
        _wire(mon)
        _occupy(7, CooldownEject(unit_id=42, run_id=9))
        first = mon._armed[7].task

        # Two more notifications carrying the very same policy: a repeat terminal,
        # and an explicit set_policy to an equal value.
        _occupy(7, CooldownEject(unit_id=42, run_id=9))
        assert plate_occupancy.set_policy(7, CooldownEject(unit_id=42, run_id=9)) is None

        assert len(spawns) == 1
        assert mon._armed[7].task is first  # the SAME task object, never a successor
        assert first.cancelled is False

    def test_policy_change_cancels_the_old_task_and_spawns_a_new_one(self, spawns):
        mon = EjectCooldownMonitor()
        _wire(mon)
        plate_occupancy.hydrate_plate(7, "SUB-1", EscalationOnly())
        escalation_task = mon._armed[7].task
        assert escalation_task.name == "eject-gate-escalation-7"

        assert plate_occupancy.set_policy(7, ForeignAutoEject(profile_id=5, threshold_c=33.0)) is None

        assert escalation_task.cancelled is True
        assert [t.name for t in spawns] == ["eject-gate-escalation-7", "eject-foreign-watch-7"]
        assert mon._armed[7].task is spawns[1]
        assert mon._armed[7].policy == ForeignAutoEject(profile_id=5, threshold_c=33.0)

    def test_clear_plate_cancels_the_watch_and_arms_nothing(self, spawns):
        mon = EjectCooldownMonitor()
        _wire(mon)
        _occupy(7, CooldownEject(unit_id=42, run_id=9))
        task = mon._armed[7].task

        assert plate_occupancy.clear_plate(7) is None

        assert task.cancelled is True
        assert 7 not in mon._armed
        assert len(spawns) == 1  # nothing new was armed over the empty plate
        assert mon.active_watch(7) is None
        assert mon.request_release_now(7) is False

    def test_a_transition_on_a_clear_plate_arms_nothing(self, spawns):
        """A dispatch lease is not a plate: the driver runs and arms nothing."""
        mon = EjectCooldownMonitor()
        _wire(mon)

        lease = plate_occupancy.claim_for_dispatch(
            7, 42, pre_state="IDLE", pre_subtask=None, min_hold_s=60.0, max_hold_s=180.0, ev=Evidence()
        )
        plate_occupancy.release_dispatch(7, "test")

        assert not isinstance(lease, str)
        assert spawns == []
        assert mon._armed == {}

    def test_hydrated_eject_arms_nothing_from_the_plate_policy(self, spawns):
        """The startup reconciler owns a hydrated-eject printer — no watch beside it.

        A cooldown watch armed over a plate a (possibly still running) sweep is
        crossing is the double dispatch the legacy re-arm avoided by skipping such
        printers outright."""
        mon = EjectCooldownMonitor()
        _wire(mon)

        plate_occupancy.hydrate_eject(7, _pending())
        plate_occupancy.hydrate_plate(7, "SUB-1", CooldownEject(unit_id=42, run_id=9))

        assert plate_occupancy.snapshot(7).eject_hydrated is True
        assert plate_occupancy.snapshot(7).plate_occupied is True
        assert spawns == []
        assert mon._armed == {}

    def test_live_eject_cancels_the_plate_watch(self, spawns):
        mon = EjectCooldownMonitor()
        _wire(mon)
        _occupy(7, CooldownEject(unit_id=42, run_id=9))
        cooldown_task = mon._armed[7].task

        assert plate_occupancy.claim_for_eject(7, _pending(), Evidence()) is None

        assert cooldown_task.cancelled is True
        assert mon._armed == {}
        assert len(spawns) == 1

    def test_unverified_resolve_hands_the_plate_to_an_escalation_hold(self, spawns):
        """The stopped-sweep path: farm_policy arms nothing by hand — this does.

        ``resolve_eject("unverified")`` leaves the plate occupied under
        EscalationOnly, and the policy driver arms the hold off THAT."""
        mon = EjectCooldownMonitor()
        _wire(mon)
        _occupy(7, CooldownEject(unit_id=42, run_id=9))
        assert plate_occupancy.claim_for_eject(7, _pending(), Evidence()) is None

        assert plate_occupancy.resolve_eject(7, "unverified") is None

        assert [t.name for t in spawns] == ["eject-cooldown-watch-7", "eject-gate-escalation-7"]
        assert isinstance(mon._armed[7].policy, EscalationOnly)
        assert mon.request_release_now(7) is False  # an escalation hold cannot release

    def test_completed_resolve_leaves_no_watch(self, spawns):
        mon = EjectCooldownMonitor()
        _wire(mon)
        _occupy(7, CooldownEject(unit_id=42, run_id=9))
        assert plate_occupancy.claim_for_eject(7, _pending(), Evidence()) is None

        assert plate_occupancy.resolve_eject(7, "completed") is None

        assert plate_occupancy.is_plate_occupied(7) is False
        assert mon._armed == {}
        assert len(spawns) == 1  # only the original cooldown watch ever existed

    @pytest.mark.parametrize(
        ("policy", "task_name", "queue_item_id", "releasable"),
        [
            (CooldownEject(unit_id=42, run_id=9), "eject-cooldown-watch-7", 42, True),
            (FirstArticleEject(unit_id=43, run_id=9), "eject-fa-watch-7", 43, True),
            (ForeignAutoEject(profile_id=5, threshold_c=33.0), "eject-foreign-watch-7", None, True),
            (EscalationOnly(), "eject-gate-escalation-7", None, False),
        ],
    )
    def test_each_policy_arms_its_own_watch(self, spawns, policy, task_name, queue_item_id, releasable):
        """The policy → watch mapping, including which policies can release at all."""
        mon = EjectCooldownMonitor()
        _wire(mon)

        _occupy(7, policy)

        assert [t.name for t in spawns] == [task_name]
        armed = mon._armed[7]
        assert armed.queue_item_id == queue_item_id
        assert (armed.release_now is not None) is releasable
        assert mon.request_release_now(7) is releasable
        if releasable:
            assert armed.release_now.is_set()  # request_release_now signalled it

    def test_release_now_is_unset_until_requested(self, spawns):
        mon = EjectCooldownMonitor()
        _wire(mon)
        _occupy(7, CooldownEject(unit_id=42, run_id=9))

        assert mon._armed[7].release_now.is_set() is False
        assert mon.request_release_now(7) is True
        assert mon._armed[7].release_now.is_set() is True

    def test_request_release_now_is_false_for_an_unarmed_printer(self):
        assert EjectCooldownMonitor().request_release_now(7) is False


class TestEjectTimerHygiene:
    """Level-triggered: whenever no eject is registered, both eject timers are dropped."""

    @pytest.fixture()
    def cancelled(self, monkeypatch):
        calls: list[int] = []
        monkeypatch.setattr(monitor_mod.eject_remote, "cancel_eject_timers", calls.append)
        return calls

    def test_no_eject_present_cancels_the_timers(self, spawns, cancelled):
        mon = EjectCooldownMonitor()
        _wire(mon)

        _occupy(7, EscalationOnly())

        assert cancelled == [7]

    def test_an_eject_present_leaves_the_timers_alone(self, spawns, cancelled):
        mon = EjectCooldownMonitor()
        _wire(mon)
        _occupy(7, CooldownEject(unit_id=42, run_id=9))
        cancelled.clear()

        assert plate_occupancy.claim_for_eject(7, _pending(), Evidence()) is None

        assert cancelled == []  # the sweep's own timers must survive its claim

    def test_every_eject_retirement_cancels_the_timers(self, spawns, cancelled):
        mon = EjectCooldownMonitor()
        _wire(mon)
        _occupy(7, CooldownEject(unit_id=42, run_id=9))
        assert plate_occupancy.claim_for_eject(7, _pending(), Evidence()) is None
        cancelled.clear()

        assert plate_occupancy.resolve_eject(7, "completed") is None

        assert cancelled == [7]


class TestPolicyDriverFailure:
    """A policy that cannot arm leaves the plate ARMLESS — the one outcome
    2026-07-18/07-21 forbids. The driver must therefore NEVER swallow: the
    authority's repair to EscalationOnly depends on the exception escaping."""

    def test_arm_failure_propagates_out_of_on_occupancy_change(self, monkeypatch):
        mon = EjectCooldownMonitor()
        _gate_up(1)  # driver not wired yet — just build the occupied view
        view = plate_occupancy.snapshot(1)

        def _boom(coro, *, name=None):
            coro.close()
            raise RuntimeError("spawn failed")

        monkeypatch.setattr(monitor_mod, "spawn_background_task", _boom)

        with pytest.raises(RuntimeError, match="spawn failed"):
            mon.on_occupancy_change(1, view, "terminal")

        assert mon._armed == {}  # nothing half-registered behind the failure

    def test_a_failed_cooldown_arm_is_repaired_to_an_escalation_hold(self, monkeypatch):
        """End-to-end never-armless floor: the authority repairs and re-calls."""
        spawned: list[_FakeTask] = []

        def _spawn(coro, *, name=None):
            coro.close()
            if name is not None and name.startswith("eject-cooldown-watch"):
                raise RuntimeError("cooldown arm failed")
            task = _FakeTask(name)
            spawned.append(task)
            return task

        monkeypatch.setattr(monitor_mod, "spawn_background_task", _spawn)
        mon = EjectCooldownMonitor()
        _wire(mon)

        _occupy(1, CooldownEject(unit_id=42, run_id=9))

        assert [t.name for t in spawned] == ["eject-gate-escalation-1"]
        assert isinstance(mon._armed[1].policy, EscalationOnly)
        assert isinstance(plate_occupancy.snapshot(1).plate_policy, EscalationOnly)
        assert plate_occupancy.is_plate_occupied(1) is True  # the plate is never dropped by a repair


# --------------------------------------------------------------------------- #
# The watch bodies
# --------------------------------------------------------------------------- #
class TestResolveEjectThresholdFirstArticle:
    """`_resolve_eject_threshold` must resolve first-article items to no-auto-clear
    even though they carry an eject profile — and it keys off the SPECIFIC item id
    (db.get), not the most-recently-started item on the printer (Phase 1)."""

    async def test_first_article_resolves_to_none(self, db_session, monkeypatch):
        import contextlib

        from backend.app.models.print_queue import PrintQueueItem

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


class TestWatchBedAndClear:
    """The reworked policy: bed ≤ threshold dispatches the eject (on_release) and
    the monitor NEVER clears the plate gate itself; plateau / cap / dispatch-retry
    drive the stall/cap paths. Returns released|stalled|cleared.

    The gate is the plate-occupancy authority's — ``manager`` supplies the bed only."""

    async def test_releases_when_bed_reaches_threshold(self):
        _gate_up(7)
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
        assert plate_occupancy.is_plate_occupied(7) is True  # monitor NEVER clears the gate now
        assert stall.reasons == []

    async def test_on_sample_sees_every_readable_tick(self):
        """The cooldown prep's chamber-boost lane rides THIS poll — it is handed the
        whole live ``temperatures`` map, not just the bed, because the chamber reading
        is what ends the boost."""
        _gate_up(7)
        mgr = _FakeManager([_status(60), _status(40), _status(27)])
        seen: list[dict] = []
        outcome = await watch_bed_and_clear(
            7,
            28.0,
            manager=mgr,
            escalate_s=100,
            check_interval_s=20,
            sleep=_noop_sleep,
            on_release=_ReleaseRecorder(),
            on_sample=seen.append,
        )
        assert outcome == "released"
        assert seen == [{"bed": 60}, {"bed": 40}, {"bed": 27}]

    @pytest.mark.parametrize(
        ("first", "label"),
        [(None, "no-status"), (_status(60, connected=False), "disconnected")],
    )
    async def test_on_sample_is_not_called_for_an_unreadable_tick(self, first, label):
        """An unreadable tick carries no measurement — sampling a stale map would feed
        the boost decision a reading this poll never actually took."""
        _gate_up(7)
        mgr = _FakeManager([first, _status(27)])
        seen: list[dict] = []
        outcome = await watch_bed_and_clear(
            7,
            28.0,
            manager=mgr,
            escalate_s=100,
            check_interval_s=20,
            sleep=_noop_sleep,
            on_release=_ReleaseRecorder(),
            on_sample=seen.append,
        )
        assert outcome == "released"
        assert seen == [{"bed": 27}]

    async def test_a_sampler_that_raises_never_kills_the_watch(self):
        """Fire-and-forget by contract: a plate's watch is worth more than a
        measurement, so the poll logs and carries on to its release."""

        def boom(_temperatures):
            raise RuntimeError("sampler boom")

        _gate_up(7)
        mgr = _FakeManager([_status(60), _status(27)])
        rel = _ReleaseRecorder()
        outcome = await watch_bed_and_clear(
            7,
            28.0,
            manager=mgr,
            escalate_s=100,
            check_interval_s=20,
            sleep=_noop_sleep,
            on_release=rel,
            on_sample=boom,
        )
        assert outcome == "released"
        assert rel.calls == 1

    async def test_releases_at_exact_threshold(self):
        _gate_up(3)
        mgr = _FakeManager([_status(28.0)])
        rel = _ReleaseRecorder()
        outcome = await watch_bed_and_clear(
            3, 28.0, manager=mgr, escalate_s=100, check_interval_s=20, sleep=_noop_sleep, on_release=rel
        )
        assert outcome == "released"
        assert rel.calls == 1
        assert plate_occupancy.is_plate_occupied(3) is True

    async def test_dispatch_failure_retries_then_stalls_after_three(self):
        # on_release raises every time (dispatch keeps failing). The watch retries
        # on each poll; after the THIRD consecutive failure it stalls.
        _gate_up(9)
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
        assert plate_occupancy.is_plate_occupied(9) is True

    async def test_dispatch_failure_then_success_releases(self):
        # Two failures then the poll's retry succeeds → released, no stall.
        _gate_up(9)
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
        _gate_up(3)
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
        assert plate_occupancy.is_plate_occupied(3) is True

    async def test_plateau_reset_on_cooling_window_never_stalls(self):
        # Steady cool 0.6°C/poll with epsilon 1.0: a window that cools ≥ epsilon
        # resets the strike streak, so a steadily-cooling bed NEVER false-stalls —
        # it eventually crosses the threshold and releases.
        _gate_up(4)
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
        _gate_up(3)
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
        _gate_up(3)
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
        _gate_up(3)
        mgr = _FakeManager([_status(50)] * 5)
        sleep = _ClearAfter(3, after_polls=3)
        rel, stall = _ReleaseRecorder(), _StallRecorder()
        outcome = await watch_bed_and_clear(
            3,
            28.0,
            manager=mgr,
            escalate_s=100000,
            check_interval_s=20,
            sleep=sleep,
            stall_window_s=0,
            stall_epsilon_c=1.0,
            max_hold_s=0,
            on_release=rel,
            on_stall=stall,
        )
        assert outcome == "cleared"
        assert stall.reasons == []
        assert rel.calls == 0
        assert sleep.gate_seen == [True, True, True]  # the watch itself never released it

    async def test_cap_fires_release_above_threshold(self):
        # No plateau watchdog (window 0); bed stuck at 50 above 28; cap 60s →
        # dispatch the eject anyway at the cap.
        _gate_up(5)
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
        _gate_up(5)
        mgr = _FakeManager([_status(50)] * 5)
        sleep = _ClearAfter(5, after_polls=3)
        rel = _ReleaseRecorder()
        outcome = await watch_bed_and_clear(
            5,
            28.0,
            manager=mgr,
            escalate_s=100000,
            check_interval_s=20,
            sleep=sleep,
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
        _gate_up(5)
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
        _gate_up(9)
        mgr = _FakeManager([None, None, _status(27)])
        rel = _ReleaseRecorder()
        outcome = await watch_bed_and_clear(
            9, 28.0, manager=mgr, escalate_s=100000, check_interval_s=20, sleep=_noop_sleep, on_release=rel
        )
        assert outcome == "released"
        assert rel.calls == 1
        assert plate_occupancy.is_plate_occupied(9) is True  # monitor never clears the gate itself

    async def test_disconnect_survives_and_later_releases(self):
        # A disconnected tick = an unreadable bed; the watch keeps polling and releases
        # once the printer reconnects with a bed at/below threshold.
        _gate_up(9)
        mgr = _FakeManager([_status(60, connected=False), _status(60, connected=False), _status(27)])
        rel = _ReleaseRecorder()
        outcome = await watch_bed_and_clear(
            9, 28.0, manager=mgr, escalate_s=100000, check_interval_s=20, sleep=_noop_sleep, on_release=rel
        )
        assert outcome == "released"
        assert rel.calls == 1
        assert plate_occupancy.is_plate_occupied(9) is True

    async def test_plateau_near_threshold_releases_not_quarantines(self):
        # Bed asymptotically settles at 30°C — it plateaus (won't cool further) but is
        # only 2°C above the 28°C threshold, within the 3°C eject margin. The two-armed
        # plateau RELEASES (equilibrated at ambient) instead of quarantining.
        _gate_up(7)
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
        _gate_up(7)
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
        _gate_up(7)
        mgr = _FakeManager([_status(50), _status(27)])
        rel = _ReleaseRecorder()
        outcome = await watch_bed_and_clear(
            7,
            28.0,
            manager=mgr,
            escalate_s=100000,
            check_interval_s=20,
            sleep=_ClearAfter(7, after_polls=1),
            on_release=rel,
        )
        assert outcome == "cleared"
        assert rel.calls == 0  # never ejected onto the cleared plate

    async def test_the_gate_is_read_from_the_authority_not_the_manager(self):
        """A manager that still answers "gate up" cannot keep a watch alive.

        The gate moved to the plate-occupancy authority precisely so no second
        store could disagree with it; a manager-shaped answer must be ignored."""
        mgr = SimpleNamespace(
            get_status=lambda pid: _status(27),
            is_awaiting_plate_clear=lambda pid: True,  # the stale second opinion
        )
        rel = _ReleaseRecorder()
        outcome = await watch_bed_and_clear(
            7, 28.0, manager=mgr, escalate_s=100000, check_interval_s=20, sleep=_noop_sleep, on_release=rel
        )
        assert outcome == "cleared"  # the authority says the plate is clear — that is final
        assert rel.calls == 0

    async def test_escalates_once_then_keeps_watching_until_release(self):
        # Hot past the escalate window, THEN cools. escalate_s=40, interval=20 →
        # escalation fires at elapsed==40, watch continues, and the later crossing
        # still dispatches the eject (released).
        _gate_up(5)
        mgr = _FakeManager([_status(60), _status(60), _status(60), _status(27)])
        notify, rel = _NotifyRecorder(), _ReleaseRecorder()
        outcome = await watch_bed_and_clear(
            5, 33.0, manager=mgr, escalate_s=40, check_interval_s=20, sleep=_noop_sleep, notify=notify, on_release=rel
        )
        assert outcome == "released"  # watch did NOT stop at the escalate window
        assert rel.calls == 1
        assert plate_occupancy.is_plate_occupied(5) is True
        assert notify.calls == [5]  # fired exactly once
        assert notify.bed_calls == [60]  # the live bed at fire time rides along

    async def test_escalation_notify_failure_does_not_kill_watch(self):
        _gate_up(6)
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
        _gate_up(9)
        mgr = _FakeManager([_status(60), _status(60), _status(60, connected=False)])
        sleep = _ClearAfter(9, after_polls=3)
        notify = _NotifyRecorder()
        outcome = await watch_bed_and_clear(
            9, 28.0, manager=mgr, escalate_s=100000, check_interval_s=20, sleep=sleep, notify=notify
        )
        assert outcome == "cleared"
        assert sleep.gate_seen == [True, True, True]  # held the gate across the disconnect


class TestWatchGateEscalationOnly:
    """The foreign-deposit gate watch: holds the gate, escalates once, exits ONLY on
    external clear (a disconnected tick no longer aborts) — and NEVER releases the
    gate itself."""

    async def test_exits_when_gate_cleared_externally(self):
        _gate_up(7)
        sleep = _ClearAfter(7, after_polls=1)
        notify = _NotifyRecorder()
        outcome = await watch_gate_escalation_only(7, escalate_s=100, check_interval_s=20, sleep=sleep, notify=notify)
        assert outcome == "cleared"
        assert notify.calls == []  # cleared before the escalation window
        assert sleep.gate_seen == [True]  # the watch itself never released it

    async def test_escalates_once_then_exits_on_external_clear(self):
        _gate_up(9)
        sleep = _ClearAfter(9, after_polls=3)
        notify = _NotifyRecorder()
        outcome = await watch_gate_escalation_only(9, escalate_s=40, check_interval_s=20, sleep=sleep, notify=notify)
        assert outcome == "cleared"
        assert notify.calls == [9]  # fired exactly once
        assert sleep.gate_seen == [True, True, True]

    async def test_cold_bed_does_not_release_gate(self):
        # The KEY difference from watch_bed_and_clear: even a fully cooled bed does
        # NOT auto-release a foreign gate — only the operator (external clear) does.
        # The bed is irrelevant here by construction: this watch takes no ``manager``
        # because it reads no temperature — its only question is whether the plate is
        # still occupied.
        _gate_up(5)
        sleep = _ClearAfter(5, after_polls=3)
        notify = _NotifyRecorder()
        outcome = await watch_gate_escalation_only(5, escalate_s=100, check_interval_s=20, sleep=sleep, notify=notify)
        assert outcome == "cleared"
        assert sleep.gate_seen == [True, True, True]  # held throughout a cold bed

    async def test_disconnected_tick_keeps_polling_then_escalates_and_clears(self):
        # F3: a disconnected/stale tick no longer ABORTS the foreign-gate watch (the
        # old "stale" exit is gone — it stranded printers that briefly dropped off).
        # The gate is held for the whole PHASE: the watch keeps polling across the
        # disconnect, escalates ONCE, and exits only when the gate is cleared.
        #
        # Since the cut-over this is true BY CONSTRUCTION rather than by tolerance:
        # the watch never consults the printer at all, so connectivity cannot end it.
        # The pin stays because the PHASE-not-connectivity lifetime is the contract,
        # however it is achieved.
        _gate_up(4)
        sleep = _ClearAfter(4, after_polls=3)
        notify = _NotifyRecorder()
        outcome = await watch_gate_escalation_only(4, escalate_s=40, check_interval_s=20, sleep=sleep, notify=notify)
        assert outcome == "cleared"
        assert notify.calls == [4]  # escalated exactly once despite the mid-hold disconnect
        assert sleep.gate_seen == [True, True, True]  # the watch itself never releases

    async def test_escalation_notify_failure_does_not_kill_watch(self):
        _gate_up(6)
        sleep = _ClearAfter(6, after_polls=3)
        notify = _NotifyRecorder(raise_exc=True)
        outcome = await watch_gate_escalation_only(6, escalate_s=40, check_interval_s=20, sleep=sleep, notify=notify)
        assert outcome == "cleared"
        assert notify.calls == [6]  # attempted once; exception swallowed


class TestNotifyPlateNotEmpty:
    """``notify_plate_not_empty`` is PUBLIC since the cut-over: three lanes outside
    this module page through it (the escalation hold, the runtime watchdog's stop,
    the start deadline), so it is one implementation with one name."""

    async def test_resolves_the_printer_name_and_forwards_the_source_detail(self, db_session, monkeypatch):
        import contextlib

        from backend.app.models.printer import Printer
        from backend.app.services.notification_service import notification_service

        printer = Printer(
            name="P-NOTIFY",
            serial_number="NOTIFY-1",
            ip_address="10.0.0.1",
            access_code="0000",
            model="H2S",
        )
        db_session.add(printer)
        await db_session.commit()
        await db_session.refresh(printer)

        @contextlib.asynccontextmanager
        async def _fake_session():
            yield db_session

        monkeypatch.setattr("backend.app.core.database.async_session", _fake_session, raising=False)
        seen: dict[str, object] = {}

        async def _fake_notify(printer_id, printer_name, db, difference_percent=None, *, source_detail=""):
            seen["args"] = (printer_id, printer_name, source_detail)

        monkeypatch.setattr(notification_service, "on_plate_not_empty", _fake_notify)

        await notify_plate_not_empty(printer.id, source_detail="the sweep never started")

        assert seen["args"] == (printer.id, "P-NOTIFY", "the sweep never started")


# --------------------------------------------------------------------------- #
# The armed watch's own resolution (threshold publication + release binding)
# --------------------------------------------------------------------------- #
class TestArmedWatchResolution:
    """Phase 4.3c: the armed watch publishes its release threshold so the UI can
    render the cooldown phase. An escalation-only hold and a still-resolving watch
    expose None. The record is dropped when the watch exits."""

    def test_none_when_nothing_armed(self):
        mon = EjectCooldownMonitor()
        assert mon.active_watch(7) is None

    def _seed_armed(self, mon: EjectCooldownMonitor, printer_id: int, policy) -> None:
        """Register the watch record the driver would have created, owned by THIS task.

        ``_watch`` publishes its threshold only onto a record whose task is the
        running one — that identity check is what stops a cancelled predecessor
        from writing over its successor's threshold."""
        mon._armed[printer_id] = _ArmedWatch(policy=policy, task=asyncio.current_task())

    async def test_cooldown_watch_exposes_threshold_then_clears(self, monkeypatch, stub_prep):
        mon = EjectCooldownMonitor()
        seen: dict[str, float | None] = {}

        async def fake_resolve(qid, *, for_first_article=False):
            assert qid == 42
            assert for_first_article is False  # production watch keeps the FA guard
            return 33.0

        async def fake_settings():
            return _settings()

        async def fake_watch(pid, threshold, **kwargs):
            # Mid-watch, the threshold is visible to status consumers.
            seen["mid"] = mon.active_watch(pid)
            return "released"

        monkeypatch.setattr(monitor_mod, "_resolve_eject_threshold", fake_resolve)
        monkeypatch.setattr(monitor_mod, "_resolve_stall_settings", fake_settings)
        monkeypatch.setattr(monitor_mod, "watch_bed_and_clear", fake_watch)
        self._seed_armed(mon, 7, CooldownEject(unit_id=42, run_id=None))

        await mon._watch(7, 42, release_now=asyncio.Event())

        assert seen["mid"] == 33.0
        assert mon.active_watch(7) is None  # record dropped when the watch exits

    async def test_non_eject_item_holds_the_plate_with_an_escalation_watch(self, monkeypatch):
        """A releasing policy over a unit with no usable eject profile must HOLD.

        Returning into silence would leave the plate armless — the outcome
        2026-07-18/07-21 forbids — so the watch falls back to the escalation hold."""
        mon = EjectCooldownMonitor()
        held: list[int] = []

        async def fake_resolve(qid, *, for_first_article=False):
            return None  # not an eject job

        async def fake_escalation(pid, **kwargs):
            held.append(pid)
            return "cleared"

        monkeypatch.setattr(monitor_mod, "_resolve_eject_threshold", fake_resolve)
        monkeypatch.setattr(monitor_mod, "watch_gate_escalation_only", fake_escalation)
        self._seed_armed(mon, 7, CooldownEject(unit_id=42, run_id=None))

        await mon._watch(7, 42, release_now=asyncio.Event())

        assert held == [7]
        assert mon.active_watch(7) is None

    async def test_fa_watch_resolves_fa_threshold_and_releases_into_fa_dispatch(self, monkeypatch, stub_prep):
        """A FirstArticleEject policy → _watch(purpose='fa'): the FA guard is skipped
        (for_first_article=True) and the release action is the FA dispatcher."""
        mon = EjectCooldownMonitor()
        seen: dict[str, object] = {}

        async def fake_resolve(qid, *, for_first_article=False):
            seen["resolve"] = (qid, for_first_article)
            return 33.0

        async def fake_settings():
            return _settings()

        async def fake_watch(pid, threshold, **kwargs):
            seen["threshold"] = threshold
            await kwargs["on_release"]()  # release fires the bound FA dispatch
            return "released"

        async def fake_fa_dispatch(*, printer_id, queue_item_id, run_id, plate_z=None):
            seen["fa_dispatch"] = (printer_id, queue_item_id, run_id)
            seen["fa_plate_z"] = plate_z

        monkeypatch.setattr(monitor_mod, "_resolve_eject_threshold", fake_resolve)
        monkeypatch.setattr(monitor_mod, "_resolve_stall_settings", fake_settings)
        monkeypatch.setattr(monitor_mod, "watch_bed_and_clear", fake_watch)
        monkeypatch.setattr(monitor_mod, "_dispatch_fa_eject", fake_fa_dispatch)
        self._seed_armed(mon, 7, FirstArticleEject(unit_id=42, run_id=9))

        await mon._watch(7, 42, purpose="fa", run_id=9, release_now=asyncio.Event())

        assert seen["resolve"] == (42, True)
        assert seen["threshold"] == 33.0
        assert seen["fa_dispatch"] == (7, 42, 9)
        assert seen["fa_plate_z"] is None  # the stubbed prep held nothing — nothing to seed
        assert mon.active_watch(7) is None  # dropped on exit

    async def test_foreign_watch_uses_direct_threshold_and_releases_into_foreign_dispatch(self, monkeypatch, stub_prep):
        """A ForeignAutoEject policy → _watch(purpose='foreign'): the threshold is
        passed DIRECTLY (no queue item, so _resolve_eject_threshold is NEVER called),
        the watch exposes it, and the release action is the foreign-plate dispatcher
        bound to the chosen profile (F5).

        The dispatcher now lives in ``eject.remote`` (it moved out of ``eject.manual``
        with the cut-over), so the binding is asserted against that module."""
        mon = EjectCooldownMonitor()
        seen: dict[str, object] = {}

        async def fake_resolve(qid, *, for_first_article=False):
            seen["resolve_called"] = True  # must NOT run for the direct-threshold path
            return 99.0

        async def fake_settings():
            return _settings()

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
        monkeypatch.setattr(monitor_mod.eject_remote, "dispatch_identified_foreign_eject", fake_foreign_dispatch)
        self._seed_armed(mon, 7, ForeignAutoEject(profile_id=5, threshold_c=33.0))

        await mon._watch(7, None, purpose="foreign", threshold_override=33.0, profile_id=5, release_now=asyncio.Event())

        assert seen["threshold"] == 33.0
        assert seen["mid"] == 33.0  # foreign watch exposes its release threshold to the UI
        assert seen["dispatch"] == (7, 5)
        assert "resolve_called" not in seen  # direct threshold skips _resolve_eject_threshold
        assert mon.active_watch(7) is None  # dropped on exit

    async def test_stall_settings_read_failure_falls_back_to_schema_defaults(self, monkeypatch):
        """A settings-store failure at arm time must arm with schema defaults,
        never kill the watch (a dead watch strands the plate-clear gate)."""
        import backend.app.core.database as db_mod
        from backend.app.schemas.settings import AppSettings

        def broken_session():
            raise RuntimeError("settings DB unavailable")

        monkeypatch.setattr(db_mod, "async_session", broken_session)
        settings = await monitor_mod._resolve_stall_settings()
        fields = AppSettings.model_fields
        assert settings.stall_window_s == int(fields["farm_cooldown_stall_window_minutes"].default) * 60
        assert settings.stall_epsilon_c == float(fields["farm_cooldown_stall_epsilon_c"].default)
        assert settings.max_hold_s == int(fields["farm_cooldown_max_hold_minutes"].default) * 60
        assert settings.plateau_eject_margin_c == float(fields["farm_cooldown_plateau_eject_margin_c"].default)
        assert settings.aux_fan_enabled is bool(fields["farm_cooldown_aux_fan_enabled"].default)
        assert settings.aux_fan_percent == int(fields["farm_cooldown_aux_fan_percent"].default)
        assert settings.chamber_fan_enabled is bool(fields["farm_cooldown_chamber_fan_enabled"].default)
        assert settings.chamber_fan_percent == int(fields["farm_cooldown_chamber_fan_percent"].default)
        assert settings.chamber_fan_sustain_percent == int(fields["farm_cooldown_chamber_fan_sustain_percent"].default)
        assert settings.hold_enabled is bool(fields["farm_cooldown_hold_enabled"].default)
        assert settings.hold_part_top_mm == int(fields["farm_cooldown_hold_part_top_mm"].default)

    def test_printer_state_payload_helper(self):
        # printer_manager exposes the armed watch as {"threshold_c", "hold_z"} / None.
        from backend.app.services.eject.monitor import eject_cooldown_monitor
        from backend.app.services.printer_manager import _eject_watch_payload

        assert _eject_watch_payload(None) is None
        assert _eject_watch_payload(901) is None
        armed = _ArmedWatch(policy=CooldownEject(unit_id=1, run_id=None), task=_FakeTask("x"), threshold_c=33.0)
        eject_cooldown_monitor._armed[901] = armed
        try:
            # An unheld plate still reports its threshold — hold_z is simply absent.
            assert _eject_watch_payload(901) == {"threshold_c": 33.0, "hold_z": None}
            armed.hold_z = 2.0
            assert _eject_watch_payload(901) == {"threshold_c": 33.0, "hold_z": 2.0}
            armed.threshold_c = None  # escalation-only hold / still resolving
            assert _eject_watch_payload(901) is None
        finally:
            eject_cooldown_monitor._armed.pop(901, None)

    def test_payload_values_are_json_primitives(self):
        """The WS lane serialises this dict with a bare ``json.dumps`` — a non-primitive
        here killed every status broadcast on the socket for a day (2026-08-31)."""
        import json

        from backend.app.services.eject.monitor import eject_cooldown_monitor
        from backend.app.services.printer_manager import _eject_watch_payload

        eject_cooldown_monitor._armed[902] = _ArmedWatch(
            policy=CooldownEject(unit_id=1, run_id=None), task=_FakeTask("x"), threshold_c=33.0, hold_z=2.0
        )
        try:
            assert json.dumps(_eject_watch_payload(902)) == '{"threshold_c": 33.0, "hold_z": 2.0}'
        finally:
            eject_cooldown_monitor._armed.pop(902, None)


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

        self._patch_session(monkeypatch, db_session)
        settings = await monitor_mod._resolve_stall_settings()
        fields = AppSettings.model_fields
        assert settings.stall_window_s == fields["farm_cooldown_stall_window_minutes"].default * 60
        assert settings.stall_epsilon_c == fields["farm_cooldown_stall_epsilon_c"].default
        assert settings.max_hold_s == fields["farm_cooldown_max_hold_minutes"].default * 60
        assert settings.plateau_eject_margin_c == fields["farm_cooldown_plateau_eject_margin_c"].default  # 3.0
        assert settings.aux_fan_enabled is fields["farm_cooldown_aux_fan_enabled"].default is True
        assert settings.aux_fan_percent == fields["farm_cooldown_aux_fan_percent"].default == 100
        assert settings.chamber_fan_enabled is fields["farm_cooldown_chamber_fan_enabled"].default is True
        assert settings.chamber_fan_percent == fields["farm_cooldown_chamber_fan_percent"].default == 100
        # The vendor's own chamber-cooling figure (``M106 P3 S127``).
        assert settings.chamber_fan_sustain_percent == fields["farm_cooldown_chamber_fan_sustain_percent"].default == 50
        assert settings.hold_enabled is fields["farm_cooldown_hold_enabled"].default is True
        assert settings.hold_part_top_mm == fields["farm_cooldown_hold_part_top_mm"].default == 100

    async def test_the_fans_property_composes_the_preps_value(self, db_session, monkeypatch):
        """The ONE place the five settings become the prep's two requests — and the
        aux lane's sustain is its own speed, by construction rather than by a setting."""
        self._patch_session(monkeypatch, db_session)
        fans = (await monitor_mod._resolve_stall_settings()).fans
        prep_mod = monitor_mod.cooldown_prep
        assert fans.aux == prep_mod.FanRequest(True, 100, 100)
        assert fans.chamber == prep_mod.FanRequest(True, 100, 50)
        assert fans.for_fan(prep_mod.AUX_FAN) is fans.aux
        assert fans.for_fan(prep_mod.CHAMBER_FAN) is fans.chamber

    async def test_reads_settings_rows_and_converts_minutes(self, db_session, monkeypatch):
        from backend.app.api.routes.settings import set_setting

        self._patch_session(monkeypatch, db_session)
        await set_setting(db_session, "farm_cooldown_stall_window_minutes", "10")
        await set_setting(db_session, "farm_cooldown_stall_epsilon_c", "2.5")
        await set_setting(db_session, "farm_cooldown_max_hold_minutes", "0")  # 0 disables the cap
        await set_setting(db_session, "farm_cooldown_plateau_eject_margin_c", "4.5")
        await set_setting(db_session, "farm_cooldown_aux_fan_percent", "60")
        await set_setting(db_session, "farm_cooldown_chamber_fan_percent", "80")
        await set_setting(db_session, "farm_cooldown_chamber_fan_sustain_percent", "0")
        await set_setting(db_session, "farm_cooldown_hold_part_top_mm", "-20")
        settings = await monitor_mod._resolve_stall_settings()
        assert settings.stall_window_s == 10 * 60
        assert settings.stall_epsilon_c == 2.5
        assert settings.max_hold_s == 0
        assert settings.plateau_eject_margin_c == 4.5
        assert settings.aux_fan_percent == 60
        assert settings.chamber_fan_percent == 80
        # 0 is a legal SUSTAIN: the chamber fan stops when the air reaches the eject
        # line. It is a step target, not an off switch — that is ``..._enabled``.
        assert settings.chamber_fan_sustain_percent == 0
        assert settings.chamber_fan_enabled is True
        # Negative targets are legal: the top of the part held UNDER the nozzle plane.
        assert settings.hold_part_top_mm == -20

    @pytest.mark.parametrize(
        ("key", "attribute"),
        [
            ("farm_cooldown_hold_enabled", "hold_enabled"),
            ("farm_cooldown_aux_fan_enabled", "aux_fan_enabled"),
            ("farm_cooldown_chamber_fan_enabled", "chamber_fan_enabled"),
        ],
    )
    @pytest.mark.parametrize(
        ("stored", "expected"),
        [("true", True), ("TRUE", True), ("false", False), ("False", False), ("", False), ("1", False)],
    )
    async def test_a_switch_is_read_as_a_string_not_cast(
        self, db_session, monkeypatch, key, attribute, stored, expected
    ):
        """``bool("false")`` is True, so a switch cannot ride ``_setting_num``.

        The store keeps whatever the PUT route wrote ("true"/"false"), and the only
        correct read is the same case-insensitive ``== "true"`` test every other boolean
        setting in the app is read with — anything else is OFF, which is the safe
        direction for a value nothing here wrote. Parametrized across ALL three switches
        because each one is now the sole on/off owner of its actuator: a speed of 0 is
        not an off anywhere since the two-fan wave."""
        from backend.app.api.routes.settings import set_setting

        self._patch_session(monkeypatch, db_session)
        await set_setting(db_session, key, stored)
        settings = await monitor_mod._resolve_stall_settings()
        assert getattr(settings, attribute) is expected

    @pytest.mark.parametrize("attribute", ["hold_enabled", "aux_fan_enabled", "chamber_fan_enabled"])
    async def test_an_absent_switch_row_is_the_schema_default_not_false(self, db_session, monkeypatch, attribute):
        """Fail-open on absence: an install that never wrote the key holds plates and
        cools them — every one of these three defaults to on."""
        self._patch_session(monkeypatch, db_session)
        assert getattr(await monitor_mod._resolve_stall_settings(), attribute) is True


class TestCooldownPrepWiring:
    """The cooldown prep is armed before the bed poll and retired in a ``finally``
    around it — on EVERY exit, because a fan left running under an idle printer and a
    plate left held under no watch are both states nothing else in the farm clears."""

    @staticmethod
    async def _threshold(qid, *, for_first_article=False):
        return 33.0

    @staticmethod
    async def _settings_fans_on():
        return _settings_with_fans()

    def _patch_deps(self, monkeypatch, *, watch, prep: _PrepRecorder):
        """Everything ``_watch`` reaches for, replaced — but ``_watch`` itself is real."""
        prep.install(monkeypatch)
        monkeypatch.setattr(monitor_mod, "_resolve_eject_threshold", self._threshold)
        monkeypatch.setattr(monitor_mod, "_resolve_stall_settings", self._settings_fans_on)
        monkeypatch.setattr(monitor_mod, "watch_bed_and_clear", watch)

    def _install(self, monkeypatch, mon, *, watch, prep: _PrepRecorder):
        self._patch_deps(monkeypatch, watch=watch, prep=prep)
        mon._armed[7] = _ArmedWatch(policy=CooldownEject(unit_id=42, run_id=None), task=asyncio.current_task())

    async def test_waits_for_the_mqtt_session_before_arming(self, monkeypatch):
        """A restart re-arms this watch from its hydrated plate BEFORE the lifespan
        opens the MQTT sessions (``hydrate()`` runs first, deliberately), so arming the
        prep straight away would publish a hold and a fan command into a closed socket
        — observed live as ``not connected — plate not held``. The prep must arm only
        once the wire is up."""
        mon = EjectCooldownMonitor()
        prep = _PrepRecorder()
        answers = [False, False, False, True]

        def fake_connected(printer_id):
            # Recorded into the SAME list as the prep events, so the ordering between
            # "the session came up" and "the prep armed" is proven, not inferred.
            answer = answers.pop(0) if answers else True
            prep.order.append(f"connected={answer}")
            return answer

        async def fake_watch(pid, threshold, **kwargs):
            prep.order.append("poll")
            return "released"

        monkeypatch.setattr(monitor_mod, "_PREP_CONNECT_POLL_S", 0.001)
        monkeypatch.setattr(monitor_mod, "_PREP_CONNECT_WAIT_S", 1.0)
        monkeypatch.setattr(monitor_mod.printer_manager, "is_connected", fake_connected)
        self._install(monkeypatch, mon, watch=fake_watch, prep=prep)
        await mon._watch(7, 42, release_now=asyncio.Event())

        # Four polls — three refusals, then the session — and the arm strictly after it.
        assert prep.order == [
            "connected=False",
            "connected=False",
            "connected=False",
            "connected=True",
            "begin",
            "observe",
            "poll",
            "end",
        ]
        assert answers == []  # every scripted answer was consumed: 4 polls, no more

    async def test_arms_anyway_at_the_bound_and_still_polls_the_bed(self, monkeypatch):
        """A printer that never comes back must not park its watch forever: the bed
        poll's max-hold cap and escalation are the only things that will ever page a
        human about that plate, so the wait is bounded and the prep records an honest
        ``skipped:disconnected`` from there."""
        mon = EjectCooldownMonitor()
        prep = _PrepRecorder()

        async def fake_watch(pid, threshold, **kwargs):
            prep.order.append("poll")
            return "released"

        monkeypatch.setattr(monitor_mod, "_PREP_CONNECT_POLL_S", 0.001)
        monkeypatch.setattr(monitor_mod, "_PREP_CONNECT_WAIT_S", 0.005)
        monkeypatch.setattr(monitor_mod.printer_manager, "is_connected", lambda printer_id: False)
        self._install(monkeypatch, mon, watch=fake_watch, prep=prep)
        await mon._watch(7, 42, release_now=asyncio.Event())

        assert prep.order == ["begin", "observe", "poll", "end"]

    async def test_the_watch_hands_the_prep_the_operators_hold_settings(self, monkeypatch):
        """Resolved ONCE at arm and passed in, so a setting changed mid-cooldown cannot
        move a plate that is already held (or desynchronise the eject's start_z seed
        from it). The prep reads no settings of its own."""
        mon = EjectCooldownMonitor()
        prep = _PrepRecorder()

        async def fake_settings():
            return _settings_with_fans(hold_enabled=False, hold_part_top_mm=-20)

        async def fake_watch(pid, threshold, **kwargs):
            return "released"

        monkeypatch.setattr(monitor_mod.printer_manager, "is_connected", lambda printer_id: True)
        self._install(monkeypatch, mon, watch=fake_watch, prep=prep)
        monkeypatch.setattr(monitor_mod, "_resolve_stall_settings", fake_settings)
        await mon._watch(7, 42, release_now=asyncio.Event())

        assert prep.begun == [(7, 42, FANS_ON)]
        assert prep.holds == [(False, -20)]

    async def test_the_watch_resolves_the_model_and_the_threshold_for_the_prep(self, monkeypatch):
        """The prep gates its chamber lane on the MODEL and steps its boost down at the
        release THRESHOLD, and looks up neither: both are the watch's, resolved once at
        arm beside the settings, so nothing inside the prep can disagree with what this
        cooldown is actually waiting for."""
        mon = EjectCooldownMonitor()
        prep = _PrepRecorder()

        async def fake_watch(pid, threshold, **kwargs):
            return "released"

        monkeypatch.setattr(monitor_mod.printer_manager, "get_model", lambda printer_id: "H2S")
        self._install(monkeypatch, mon, watch=fake_watch, prep=prep)
        await mon._watch(7, 42, release_now=asyncio.Event())

        assert prep.models == ["H2S"]
        # The same threshold the poll below it releases on — one resolution, two users.
        assert prep.thresholds == [33.0]

    async def test_an_uncached_model_still_arms_the_prep(self, monkeypatch):
        """``get_model`` answers None for a printer the manager has not cached; that is
        the prep's ``skipped:unsupported`` to decide, never a reason not to arm."""
        mon = EjectCooldownMonitor()
        prep = _PrepRecorder()

        async def fake_watch(pid, threshold, **kwargs):
            prep.order.append("poll")
            return "released"

        monkeypatch.setattr(monitor_mod.printer_manager, "get_model", lambda printer_id: None)
        self._install(monkeypatch, mon, watch=fake_watch, prep=prep)
        await mon._watch(7, 42, release_now=asyncio.Event())

        assert prep.models == [None]
        assert prep.order == ["begin", "observe", "poll", "end"]

    async def test_the_bed_poll_feeds_the_preps_sampler(self, monkeypatch):
        """ONE poll, two consumers: the reading that decides the release is the same
        reading that ends the chamber boost. No second timer is wired anywhere."""
        mon = EjectCooldownMonitor()
        prep = _PrepRecorder()

        async def fake_watch(pid, threshold, **kwargs):
            prep.order.append("poll")
            kwargs["on_sample"]({"bed": 61.0, "chamber": 38.0})
            kwargs["on_sample"]({"bed": 40.0, "chamber": 33.0})
            return "released"

        self._install(monkeypatch, mon, watch=fake_watch, prep=prep)
        await mon._watch(7, 42, release_now=asyncio.Event())

        assert prep.samples == [{"bed": 61.0, "chamber": 38.0}, {"bed": 40.0, "chamber": 33.0}]

    async def test_a_connected_printer_never_waits(self, monkeypatch):
        """The steady-state path — a terminal on a live session — pays nothing for the
        restart case: the first answer is the only question asked."""
        mon = EjectCooldownMonitor()
        prep = _PrepRecorder()
        slept: list[float] = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        async def fake_watch(pid, threshold, **kwargs):
            prep.order.append("poll")
            return "released"

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(monitor_mod.printer_manager, "is_connected", lambda printer_id: True)
        self._install(monkeypatch, mon, watch=fake_watch, prep=prep)
        await mon._watch(7, 42, release_now=asyncio.Event())

        assert slept == []
        assert prep.order == ["begin", "observe", "poll", "end"]

    async def test_cancel_during_the_connect_wait_arms_nothing(self, monkeypatch):
        """A gate cleared while we wait for the wire cancels the watch exactly as it
        does mid-poll — and nothing was armed, so there is nothing to retire."""
        mon = EjectCooldownMonitor()
        prep = _PrepRecorder()
        waiting = asyncio.Event()

        def never_connected(printer_id):
            waiting.set()
            return False

        async def fake_watch(pid, threshold, **kwargs):
            prep.order.append("poll")
            return "released"

        monkeypatch.setattr(monitor_mod, "_PREP_CONNECT_POLL_S", 0.01)
        monkeypatch.setattr(monitor_mod.printer_manager, "is_connected", never_connected)
        self._patch_deps(monkeypatch, watch=fake_watch, prep=prep)

        task = asyncio.create_task(mon._watch(7, 42, release_now=asyncio.Event()))
        mon._armed[7] = _ArmedWatch(policy=CooldownEject(unit_id=42, run_id=None), task=task)
        await waiting.wait()
        mon._cancel(7, "plate cleared by an operator")
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert prep.begun == []  # nothing was armed …
        assert prep.ended == []  # … so there is nothing to switch back off
        assert prep.order == []
        assert mon._armed.get(7) is None  # and the record is released

    async def test_begins_before_the_poll_and_ends_after_it(self, monkeypatch):
        """The whole point is to shorten the wait the poll is about to sit through, so
        the actuators go on FIRST — and the settings the watch resolved carry the
        operator's fan percent into them."""
        mon = EjectCooldownMonitor()
        prep = _PrepRecorder()

        async def fake_watch(pid, threshold, **kwargs):
            prep.order.append("poll")
            return "released"

        self._install(monkeypatch, mon, watch=fake_watch, prep=prep)
        await mon._watch(7, 42, release_now=asyncio.Event())

        assert prep.order == ["begin", "observe", "poll", "end"]
        assert prep.begun == [(7, 42, FANS_ON)]

    async def test_retired_when_the_poll_raises(self, monkeypatch):
        mon = EjectCooldownMonitor()
        prep = _PrepRecorder()

        async def fake_watch(pid, threshold, **kwargs):
            raise RuntimeError("bed poll exploded")

        self._install(monkeypatch, mon, watch=fake_watch, prep=prep)
        await mon._watch(7, 42, release_now=asyncio.Event())  # swallowed by the watch body

        assert prep.ended == [True]  # nothing succeeded us → the fan goes off

    async def _cancelled_watch(self, monkeypatch, mon, prep: _PrepRecorder) -> asyncio.Task:
        """Start a real watch parked in its bed poll, and return the task."""
        started = asyncio.Event()

        async def fake_watch(pid, threshold, **kwargs):
            started.set()
            await asyncio.sleep(3600)  # park here until the driver cancels us

        prep.install(monkeypatch)
        monkeypatch.setattr(monitor_mod, "_resolve_eject_threshold", self._threshold)
        monkeypatch.setattr(monitor_mod, "_resolve_stall_settings", self._settings_fans_on)
        monkeypatch.setattr(monitor_mod, "watch_bed_and_clear", fake_watch)
        task = asyncio.create_task(mon._watch(7, 42, release_now=asyncio.Event()))
        mon._armed[7] = _ArmedWatch(policy=CooldownEject(unit_id=42, run_id=None), task=task)
        await started.wait()
        return task

    async def test_retired_when_the_watch_is_cancelled(self, monkeypatch):
        mon = EjectCooldownMonitor()
        prep = _PrepRecorder()
        task = await self._cancelled_watch(monkeypatch, mon, prep)

        mon._cancel(7, "plate cleared")  # no successor
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert prep.ended == [True]

    @pytest.mark.parametrize("successor_begins_first", [True, False])
    async def test_cancel_with_a_cooldown_successor_hands_the_fan_over(self, monkeypatch, successor_begins_first):
        """Order-independent by construction: the driver installs the successor's
        RECORD synchronously inside the same ``on_occupancy_change`` call that
        cancelled us, long before our CancelledError is delivered — so whether the
        successor's own ``begin`` published its fan before or after our exit, the
        answer to "does this printer still want its fan" is the same."""
        mon = EjectCooldownMonitor()
        prep = _PrepRecorder()
        task = await self._cancelled_watch(monkeypatch, mon, prep)

        mon._cancel(7, "policy changed to CooldownEject")
        mon._armed[7] = _ArmedWatch(policy=CooldownEject(unit_id=43, run_id=None), task=_FakeTask("successor"))
        if successor_begins_first:
            await monitor_mod.cooldown_prep.begin(
                7, queue_item_id=43, fans=FANS_ON, release_threshold_c=33.0, model="H2S"
            )
        with contextlib.suppress(asyncio.CancelledError):
            await task
        if not successor_begins_first:
            await monitor_mod.cooldown_prep.begin(
                7, queue_item_id=43, fans=FANS_ON, release_threshold_c=33.0, model="H2S"
            )

        assert prep.ended == [False]  # the fan stays on for the successor
        assert prep.order.count("begin") == 2

    async def test_cancel_with_an_escalation_successor_switches_the_fan_off(self, monkeypatch):
        """An escalation-only hold is a plate waiting for a HUMAN — nothing is cooling
        toward a release, so nothing wants the fan."""
        mon = EjectCooldownMonitor()
        prep = _PrepRecorder()
        task = await self._cancelled_watch(monkeypatch, mon, prep)

        mon._cancel(7, "policy changed to EscalationOnly")
        mon._armed[7] = _ArmedWatch(policy=EscalationOnly(), task=_FakeTask("successor"))
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert prep.ended == [True]

    @pytest.mark.parametrize(
        ("hold", "hold_z", "expected"),
        [("sent", 2.0, 2.0), ("skipped:no_numbers", None, None), ("skipped:keepout", None, None)],
    )
    async def test_hold_z_is_published_only_for_a_hold_that_happened(self, monkeypatch, hold, hold_z, expected):
        """The seed and the UI chip come from the same store, and only a SENT hold
        writes it — a computed-but-unsent height would tell the eject's watchdog the
        bed is 120 mm closer to the nozzle than it really is."""
        mon = EjectCooldownMonitor()
        prep = _PrepRecorder(hold=hold, hold_z=hold_z)
        seen: dict[str, object] = {}

        async def fake_watch(pid, threshold, **kwargs):
            seen["mid"] = mon.hold_z(pid)
            await kwargs["on_release"]()
            return "released"

        async def fake_dispatch(*, printer_id, queue_item_id, plate_z=None):
            seen["plate_z"] = plate_z

        self._install(monkeypatch, mon, watch=fake_watch, prep=prep)
        monkeypatch.setattr(monitor_mod, "_dispatch_production_eject", fake_dispatch)
        await mon._watch(7, 42, release_now=asyncio.Event())

        assert seen["mid"] == expected  # visible to the status payload mid-watch
        assert seen["plate_z"] == expected  # and it is what the eject is seeded with
        assert mon.hold_z(7) is None  # the record is dropped when the watch exits

    async def test_hold_z_reaches_the_status_payload(self, monkeypatch):
        """End to end: the watch's own store → ``hold_z()`` → the eject_watch payload
        the printer card renders its "plate raised" chip from."""
        from backend.app.services.printer_manager import _eject_watch_payload

        mon = EjectCooldownMonitor()
        prep = _PrepRecorder(hold="sent", hold_z=2.0)
        seen: dict[str, object] = {}

        async def fake_watch(pid, threshold, **kwargs):
            seen["payload"] = _eject_watch_payload(pid)
            return "released"

        self._install(monkeypatch, mon, watch=fake_watch, prep=prep)
        # The payload helper resolves the singleton lazily, at call time.
        monkeypatch.setattr(monitor_mod, "eject_cooldown_monitor", mon)
        await mon._watch(7, 42, release_now=asyncio.Event())

        assert seen["payload"] == {"threshold_c": 33.0, "hold_z": 2.0}


class TestCooldownArmedArbiter:
    """``_cooldown_armed`` is the ONE answer to "does this printer still want its aux
    fan" at a watch's exit. Four cases, and only one of them keeps the fan on."""

    def test_our_own_record_is_not_a_successor(self):
        mon = EjectCooldownMonitor()
        task = _FakeTask("mine")
        mon._armed[7] = _ArmedWatch(policy=CooldownEject(unit_id=1, run_id=None), task=task)
        assert mon._cooldown_armed(7, other_than=task) is False

    def test_a_cooldown_successor_keeps_the_fan(self):
        mon = EjectCooldownMonitor()
        mon._armed[7] = _ArmedWatch(policy=CooldownEject(unit_id=1, run_id=None), task=_FakeTask("theirs"))
        assert mon._cooldown_armed(7, other_than=_FakeTask("mine")) is True

    def test_a_first_article_successor_keeps_the_fan(self):
        mon = EjectCooldownMonitor()
        mon._armed[7] = _ArmedWatch(policy=FirstArticleEject(unit_id=1, run_id=2), task=_FakeTask("theirs"))
        assert mon._cooldown_armed(7, other_than=_FakeTask("mine")) is True

    def test_a_foreign_successor_keeps_the_fan(self):
        mon = EjectCooldownMonitor()
        mon._armed[7] = _ArmedWatch(policy=ForeignAutoEject(profile_id=5, threshold_c=33.0), task=_FakeTask("t"))
        assert mon._cooldown_armed(7, other_than=_FakeTask("mine")) is True

    def test_an_escalation_only_successor_releases_the_fan(self):
        mon = EjectCooldownMonitor()
        mon._armed[7] = _ArmedWatch(policy=EscalationOnly(), task=_FakeTask("theirs"))
        assert mon._cooldown_armed(7, other_than=_FakeTask("mine")) is False

    def test_no_record_releases_the_fan(self):
        assert EjectCooldownMonitor()._cooldown_armed(7, other_than=_FakeTask("mine")) is False


class TestPlateZThreading:
    """The held height reaches the eject dispatcher, which is the only consumer that
    can turn it into a runtime budget."""

    @staticmethod
    def _patch_session(monkeypatch):
        @contextlib.asynccontextmanager
        async def fake_session():
            yield SimpleNamespace(get=_get)

        async def _get(model, pk):
            return SimpleNamespace(batch_id=9)

        monkeypatch.setattr("backend.app.core.database.async_session", fake_session, raising=False)

    async def test_production_dispatch_forwards_plate_z(self, monkeypatch):
        seen: dict[str, object] = {}

        async def fake_dispatch(db, *, printer_id, queue_item_id, purpose, run_id, plate_z=None):
            seen.update(printer_id=printer_id, purpose=purpose, run_id=run_id, plate_z=plate_z)

        self._patch_session(monkeypatch)
        monkeypatch.setattr(eject_remote, "dispatch_part_present_eject", fake_dispatch)
        await monitor_mod._dispatch_production_eject(printer_id=7, queue_item_id=42, plate_z=2.0)

        assert seen == {"printer_id": 7, "purpose": "production", "run_id": 9, "plate_z": 2.0}

    async def test_fa_dispatch_forwards_plate_z(self, monkeypatch):
        seen: dict[str, object] = {}

        async def fake_dispatch(db, *, printer_id, queue_item_id, purpose, run_id, plate_z=None):
            seen.update(purpose=purpose, run_id=run_id, plate_z=plate_z)

        self._patch_session(monkeypatch)
        monkeypatch.setattr(eject_remote, "dispatch_part_present_eject", fake_dispatch)
        await monitor_mod._dispatch_fa_eject(printer_id=7, queue_item_id=42, run_id=3, plate_z=4.0)

        assert seen == {"purpose": "fa", "run_id": 3, "plate_z": 4.0}

    async def test_unheld_plate_dispatches_unseeded(self, monkeypatch):
        """None is the honest answer for every unheld lane, and the estimator then
        costs the first Z move at its longest possible travel instead."""
        seen: dict[str, object] = {}

        async def fake_dispatch(db, *, printer_id, queue_item_id, purpose, run_id, plate_z=None):
            seen["plate_z"] = plate_z

        self._patch_session(monkeypatch)
        monkeypatch.setattr(eject_remote, "dispatch_part_present_eject", fake_dispatch)
        await monitor_mod._dispatch_production_eject(printer_id=7, queue_item_id=42)

        assert seen["plate_z"] is None


class TestManualReleaseNow:
    """W2: an armed watch's release_now event drives an immediate manual eject
    through the SAME _do_release path, bypassing the cooldown threshold."""

    async def test_preset_event_releases_even_hot(self):
        # Bed 60 is well ABOVE the 30 threshold — a normal poll would NOT release.
        # The pre-set release_now event fires the manual release on the first poll.
        _gate_up(7)
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

    async def test_no_event_falls_through_to_threshold(self):
        # Without release_now, the same hot bed keeps cooling (no manual release).
        _gate_up(7)
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

    async def test_gate_dropped_at_release_boundary_exits_cleared(self):
        # The gate is up at the top-of-poll check and dropped by the time _do_release
        # re-checks — the bed read is the sync seam between the two.
        _gate_up(3)
        mgr = _FakeManager([_status(20)], on_status=lambda: plate_occupancy.clear_plate(3))
        rel = _ReleaseRecorder()
        outcome = await watch_bed_and_clear(
            3, 30.0, manager=mgr, escalate_s=100000, check_interval_s=20, sleep=_noop_sleep, on_release=rel
        )
        assert outcome == "cleared"
        assert rel.calls == 0  # never swept the emptied plate

    async def test_gate_up_throughout_releases_normally(self):
        # Sanity: gate stays up → the same bed ≤ threshold releases.
        _gate_up(3)
        mgr = _FakeManager([_status(20)])
        rel = _ReleaseRecorder()
        outcome = await watch_bed_and_clear(
            3, 30.0, manager=mgr, escalate_s=100000, check_interval_s=20, sleep=_noop_sleep, on_release=rel
        )
        assert outcome == "released"
        assert rel.calls == 1


class _TerminalRefuser:
    """An ``on_release`` that refuses TERMINALLY — the lost-Z-frame shape (2026-09-04).

    A dispatcher raises the authority's refusal through ``_refusal_error``, so the test
    uses the REAL error the real gate produces rather than a hand-built stand-in."""

    def __init__(self, refusal: str = "z_unreferenced"):
        self.calls = 0
        self._refusal = refusal

    async def __call__(self):
        self.calls += 1
        raise eject_remote._refusal_error(self._refusal)


class TestTerminalRefusalHoldsForAHuman:
    """A refusal no waiting can cure pages once and hands the plate to a human.

    The 2026-09-04 002-H2S shape: the printer rebooted with a part on the plate, so its
    Z frame is fiction and the sweep's absolute Z moves would run against it. Retrying
    that three times only delays the page."""

    async def test_it_pages_then_holds_without_striking_or_quarantining(self):
        _gate_up(11)
        mgr = _FakeManager([_status(20)])
        rel, stall = _TerminalRefuser(), _StallRecorder()
        notify = _NotifyRecorder()
        with patch.object(monitor_mod, "notify_plate_not_empty", notify):
            outcome = await watch_bed_and_clear(
                11,
                30.0,
                manager=mgr,
                escalate_s=100000,
                check_interval_s=20,
                sleep=_noop_sleep,
                on_release=rel,
                on_stall=stall,
            )
        assert outcome == "stalled"
        assert rel.calls == 1  # ONE attempt: not three, not a strike
        assert stall.reasons == []  # on_stall is the quarantine lane — never entered
        assert notify.calls == [11]
        assert notify.details == ["z_reference_lost_after_reboot"]
        # The plate is held for a human, and still occupied.
        assert plate_occupancy.is_plate_occupied(11) is True
        assert isinstance(plate_occupancy.snapshot(11).plate_policy, EscalationOnly)

    async def test_the_page_fires_even_though_the_policy_change_cancels_this_watch(self):
        """ORDER PIN. ``set_policy`` fans out to the driver, which CANCELS the watch task
        this coroutine runs inside — so a page issued after it would die at its first
        await and the operator would never be told. Driven through the real driver with
        a real task, so the cancellation actually happens."""
        monitor = EjectCooldownMonitor()
        plate_occupancy.configure(policy_driver=monitor.on_occupancy_change)
        _gate_up(12)
        mgr = _FakeManager([_status(20)])
        notify = _NotifyRecorder()
        rel = _TerminalRefuser()
        order: list[str] = []

        async def _recording_notify(printer_id, *, source_detail=""):
            order.append("page")
            await notify(printer_id, source_detail=source_detail)

        real_set_policy = plate_occupancy.set_policy

        def _recording_set_policy(printer_id, policy):
            order.append("set_policy")
            return real_set_policy(printer_id, policy)

        async def _run():
            return await watch_bed_and_clear(
                12,
                30.0,
                manager=mgr,
                escalate_s=100000,
                check_interval_s=20,
                sleep=_noop_sleep,
                on_release=rel,
            )

        task = asyncio.get_running_loop().create_task(_run())
        # The driver cancels whatever it holds for this printer; register OUR task so
        # the transition really does cancel the coroutine that is paging.
        monitor._armed[12] = _ArmedWatch(policy=CooldownEject(unit_id=1, run_id=None), task=task)
        with (
            patch.object(monitor_mod, "notify_plate_not_empty", _recording_notify),
            patch.object(monitor_mod.plate_occupancy, "set_policy", _recording_set_policy),
            contextlib.suppress(asyncio.CancelledError),
        ):
            await task
        assert order == ["page", "set_policy"], "the page must precede the transition that cancels the watch"
        assert notify.calls == [12]
        plate_occupancy.configure(policy_driver=None)

    async def test_a_transient_refusal_still_retries_three_times(self):
        # The contrast case: ``job_active`` is curable by waiting, so it stays a strike
        # and reaches the ordinary stall path after three consecutive failures.
        _gate_up(13)
        mgr = _FakeManager([_status(20)])
        rel, stall = _TerminalRefuser("job_active"), _StallRecorder()
        outcome = await watch_bed_and_clear(
            13,
            30.0,
            manager=mgr,
            escalate_s=100000,
            check_interval_s=20,
            sleep=_noop_sleep,
            on_release=rel,
            on_stall=stall,
        )
        assert outcome == "stalled"
        assert rel.calls == 3
        assert stall.reasons == ["eject dispatch failed ×3"]
