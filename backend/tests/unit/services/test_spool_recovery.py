"""Unit tests for the automatic mid-print spool-jam recovery state machine.

Drives the whole feature through the public entry ``on_feed_fault_hms`` (which
returns the spawned driver task so the test can await it) against a real
``PrinterState`` mutated by a scripted ``FakeClient``. Covers the happy swap, the
production-log replays (load-needs-resend, resume-needs-second-cycle), candidate
escalation, external-interference aborts, the entry gates (disabled / non-farm /
multi-feeder / dedup), runout handling, the layer-conditional floor, the restart
short-circuit, and the presence-edge ``clear_on_reinsert``.
"""

import ast
import asyncio
import inspect
import json
import logging
from dataclasses import replace
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from backend.app.models.print_batch import PrintBatch
from backend.app.models.print_queue import PrintQueueItem

# Imported at module level so the test-engine's create_all registers this new table
# (conftest builds the schema from Base.metadata, not the models/__init__ list).
from backend.app.models.printer_incident import KIND_PLATE_VISION, PrinterIncident
from backend.app.models.recovery_escalation import RecoveryEscalation
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services import ams_command, incident_resolution, printer_incidents, spool_recovery
from backend.app.services.bambu_mqtt import HMSError, PrinterState
from backend.app.services.printer_incidents import (
    WAITING_REASON_FAILED,
    WAITING_REASON_RECOVERING,
    WAITING_REASON_RUNOUT,
)
from backend.app.services.spool_recovery import (
    clear_on_reinsert,
    on_ams_fault,
)
from backend.tests._fixtures.clock import FakeClock

_NONE_TAG = "0000000000000000"
_NONE_UUID = "0" * 32


# --- fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset():
    spool_recovery._reset_state()
    # The operator-attribution stamps the driver's takeover predicate reads live in
    # ams_command (its owner), so a stamp one case made must not reach the next.
    ams_command._reset_state()
    yield
    spool_recovery._reset_state()
    ams_command._reset_state()


@pytest.fixture(autouse=True)
def _fast_timing(monkeypatch):
    monkeypatch.setattr(spool_recovery, "_POLL_INTERVAL_S", 0.005)
    monkeypatch.setattr(spool_recovery, "_POST_RESUME_STABLE_S", 0.02)
    monkeypatch.setattr(spool_recovery, "_REPAUSE_WATCH_S", 0.03)
    # The unload settle dwell is wall-clock by nature; zero it so the classifier's
    # grace-held rows answer on the first qualifying poll. The dwell itself is pinned
    # with a fake clock in TestUnloadGraceDwell (and row by row in test_ams_command).
    monkeypatch.setattr(ams_command, "UNLOAD_GRACE_S", 0.0)
    # The entry-gate throttle is a per-push cost bound, not a decision: zero it so
    # every case exercises the DURABLE gates. Its own behaviour is pinned in
    # TestEntryThrottle.
    monkeypatch.setattr(spool_recovery, "_EVAL_THROTTLE_S", 0.0)


@pytest.fixture(autouse=True)
def _own_sessions(test_engine, monkeypatch):
    """Point spool_recovery's own-session openers (every DB step) at the test
    engine — mirrors ams_presence's terminal-sweep fixture."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    import backend.app.core.database as core_db

    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(core_db, "async_session", maker)
    return maker


@pytest.fixture
def install_settings(monkeypatch):
    """Install a fast RecoverySettings so the confirm loops don't wall-clock."""

    def _install(*, enabled=True, max_attempts=2, step_timeout_s=0.05, protect_layers=7):
        async def _fake(_db):
            return spool_recovery.RecoverySettings(
                enabled=enabled,
                max_attempts=max_attempts,
                step_timeout_s=step_timeout_s,
                protect_layers=protect_layers,
            )

        monkeypatch.setattr(spool_recovery, "_read_settings", _fake)

    return _install


# --- scripted printer ------------------------------------------------------


def _feed_fault_hms():
    # attr>>16 == 0x0700, code == 0x8010 -> short code "0700_8010" (feed fault).
    return HMSError(code="8010", attr=0x07000000, module=7, severity=2)


def _runout_hms():
    # attr>>16 == 0x0300, code == 0x8004 -> "0300_8004" (reused-tag runout).
    return HMSError(code="8004", attr=0x03000000, module=3, severity=2)


# --- 006-H2S 2026-07-26 runout wire shapes ---------------------------------
# The slot-attributed DEMAND ("AMS A Slot N filament has run out. Please insert a
# new filament."). Its SHORT code is "0700_0001", which is deliberately NOT a
# trigger — the bare 8011 below is what fires recovery, while this entry is what
# names the slot the firmware actually wants.
def _runout_demand_hms(ams_id=0, tray_id=2):
    attr = 0x07000000 | (ams_id << 16) | ((0x20 + tray_id) << 8)
    return HMSError(code="0x20001", attr=attr, module=7, severity=2, full_code=f"{attr:08X}00020001")


def _runout_autoswitched_hms(ams_id=0, tray_id=0):
    """ "…has run out and automatically switched…" — INFO, never a demand."""
    attr = 0x07000000 | (ams_id << 16) | ((0x20 + tray_id) << 8)
    return HMSError(code="0x30002", attr=attr, module=7, severity=3, full_code=f"{attr:08X}00030002")


def _runout_same_slot_hms():
    """The slot-agnostic 0700_8011 "insert into the SAME AMS slot" runout — the
    code that actually triggers recovery, and which names no slot at all."""
    return HMSError(code="8011", attr=0x07000000, module=7, severity=2, full_code="0700000000008011")


def _extruder_hms():
    # attr>>16 == 0x0300, code == 0x801E -> "0300_801E" (main extruder overloaded).
    return HMSError(code="801E", attr=0x03000000, module=3, severity=2)


def _ams_tray(tid, *, ttype="PETG", color="00FF00FF", tii="GFG99", state=11, tag=_NONE_TAG, uuid=_NONE_UUID):
    return {
        "id": tid,
        "tray_type": ttype,
        "tray_color": color,
        "tray_info_idx": tii,
        "remain": 100,
        "state": state,
        "tag_uid": tag,
        "tray_uuid": uuid,
    }


def _make_state(
    *,
    subtask="task-1",
    tray_now=0,
    layer=50,
    gcode_state="PAUSE",
    trays=None,
    hms=None,
    backup=True,
    ams_status_main=0,
):
    st = PrinterState()
    st.state = gcode_state
    st.subtask_id = subtask
    st.subtask_name = "SKU007"
    st.tray_now = tray_now
    st.ams_status_main = ams_status_main
    st.layer_num = layer
    st.pending_tray_target = None
    st.ams_filament_backup = backup
    st.hms_errors = hms if hms is not None else [_feed_fault_hms()]
    st.raw_data = {"ams": [{"id": 0, "tray": trays if trays is not None else [_ams_tray(0), _ams_tray(1)]}]}
    return st


def _stamp_load_edge(printer_id, when):
    """Put a completed-load edge in the motion ledger at ``when``.

    The ledger's ONE writer is the per-push sampler; a unit test that needs the
    evidence to already exist reaches for the store directly rather than simulating a
    tray transition it is not testing.
    """
    incident_resolution.ledger._load_completed_at[printer_id] = when  # noqa: SLF001


def _terminal(*, status="completed", eject=False, job_id="task-1"):
    """The terminal ``main.on_print_complete`` hands the closer.

    ``status`` is the FIRMWARE's own word (captured before the operator-UI rewrite),
    ``eject`` is that callback's sweep flag and ``job_id`` the wire ``subtask_id`` —
    the three facts the repair class's completed-arm is qualified on.
    """
    from backend.app.services.incident_resolution import TerminalEvent

    return TerminalEvent(status=status, eject=eject, job_id=job_id)


class FakeClient:
    """Records unload/load/resume/pause/execute_hms_action and mutates the shared
    PrinterState to simulate the printer's response, with scripted stalls."""

    # The nozzle TOPOLOGY question every feeder reader asks through the client
    # (``_dual_nozzle_feeders``). Single-nozzle by default, like the H2S fleet the
    # replays come from; a dual-nozzle case brings its own stub.
    is_dual_nozzle = False

    def __init__(
        self,
        state,
        *,
        unload_after=1,
        load_after=1,
        load_ok_targets=None,
        resume_repauses=0,
        external_resume_on_unload=False,
        external_resume_tray=None,
        hijack_on_load=False,
        unload_ret=True,
        load_ret=True,
        resume_ret=True,
        pause_ret=True,
        unload_stuck=False,
        unload_deaf=False,
        unload_sub_only=False,
        unload_keeps_change=False,
        write_refusal=None,
        refusal_clears_on_settle=False,
        resume_unwedges=True,
        ams_control_unwedges=False,
    ):
        self.state = state
        self.unload_after = unload_after
        self.load_after = load_after
        # load_ok_targets: WHICH tray a load takes for, instead of WHICH SEND it takes
        # on. `load_after` is a global send counter and install_settings defaults
        # max_attempts=2, so "every candidate fails but the jammed tray reloads" cannot
        # be scripted by counting sends. When this set is given it decides alone (a load
        # confirms iff its tray is in it); when it is None the `load_after` counter keeps
        # its existing behaviour.
        self.load_ok_targets = load_ok_targets
        self.resume_repauses = resume_repauses
        self.external_resume_on_unload = external_resume_on_unload
        self.external_resume_tray = external_resume_tray
        self.hijack_on_load = hijack_on_load
        # Per-command send-return overrides: False simulates an offline printer
        # (the real MQTT client returns False when not connected) — the method
        # records the call but does NOT mutate state.
        self.unload_ret = unload_ret
        self.load_ret = load_ret
        self.resume_ret = resume_ret
        self.pause_ret = pause_ret
        # unload_stuck: the AMS accepts the command but stays mid-filament-change
        # (ams_status_main == 1) — the live 009-H2S state machine.
        self.unload_stuck = unload_stuck
        # The three other unload answers ``ams_command.classify`` tells apart:
        # unload_deaf — the publish goes out and NOTHING on the wire moves
        #   (``no_movement``, or ``undecidable`` into an empty mid-change AMS);
        # unload_sub_only — the AMS moves (``ams_status_sub`` steps) and never completes
        #   (``acted``);
        # unload_keeps_change — the feeder empties (tray_now → 255) while the AMS stays in
        #   the change the print still owes (the ``mid_change_loaded`` ``complete``).
        self.unload_deaf = unload_deaf
        self.unload_sub_only = unload_sub_only
        self.unload_keeps_change = unload_keeps_change
        # write_refusal: what ams_write_refusal() reports (None = wire is clear).
        self.write_refusal = write_refusal
        self.refusal_clears_on_settle = refusal_clears_on_settle
        # Did the firmware CONTINUE actually free a wedged filament-change? The two
        # tiers are scripted independently because that is the 002-H2S question:
        # print.resume left ams_status_main at 1 (every load and unload after it was
        # dropped) while ams_control("resume") had never been sent at all.
        self.resume_unwedges = resume_unwedges
        self.ams_control_unwedges = ams_control_unwedges
        self.calls: list[tuple] = []
        self._unload = 0
        self._load = 0
        self._resume = 0

    def ams_write_refusal(self, ams_id):
        self.calls.append(("refusal_check", ams_id))
        return self.write_refusal

    async def wait_ams_settle(self):
        self.calls.append(("settle",))
        if self.refusal_clears_on_settle:
            self.write_refusal = None
        return True

    def ams_unload_filament(self):
        self._unload += 1
        self.calls.append(("unload",))
        if not self.unload_ret:
            return False
        if self.external_resume_on_unload:
            self.state.state = "RUNNING"  # an external actor resumed mid-recovery
            if self.external_resume_tray is not None:
                self.state.tray_now = self.external_resume_tray  # ...on a specific feeder
            return True
        if self.unload_stuck:
            self.state.ams_status_main = 1  # filament_change never completes
            return True
        if self.unload_deaf:
            return True  # published; the AMS does not move
        if self.unload_sub_only:
            self.state.ams_status_sub += 1  # the AMS moved; the feeder never empties
            return True
        if self.unload_keeps_change:
            self.state.tray_now = 255  # the feeder emptied; ams_status_main untouched
            return True
        self.state.ams_status_main = 0  # the change state machine returned to idle
        if self._unload >= self.unload_after:
            self.state.tray_now = 255
        return True

    def ams_load_filament(self, tray_id, extruder_id=None):
        self._load += 1
        self.calls.append(("load", tray_id))
        if not self.load_ret:
            return False
        self.state.pending_tray_target = tray_id
        if self.hijack_on_load:
            self.state.pending_tray_target = 999  # someone else issued a load
            return True
        if self.load_ok_targets is not None:
            if tray_id in self.load_ok_targets:
                self.state.tray_now = tray_id
            return True
        if self._load >= self.load_after:
            self.state.tray_now = tray_id
        return True

    def resume_print(self):
        self._resume += 1
        self.calls.append(("resume",))
        if not self.resume_ret:
            return False
        if self.resume_unwedges and self.state.ams_status_main == 1:
            self.state.ams_status_main = 0  # the CONTINUE moved the filament change on
        self.state.state = "PAUSE" if self._resume <= self.resume_repauses else "RUNNING"
        return True

    def ams_control(self, action, *, request_pushall=False):
        self.calls.append(("ams_control", action))
        if self.ams_control_unwedges and action == "resume":
            self.state.ams_status_main = 0  # the AMS-side CONTINUE freed the change
        return True

    def pause_print(self):
        self.calls.append(("pause",))
        if not self.pause_ret:
            return False
        self.state.state = "PAUSE"
        return True

    def execute_hms_action(self, print_error, action, job_id=None):
        """The HMS modal's CONTINUE is an ``ams_control`` frame, not ``print.resume``.

        The real client dispatches ``HMSAction.CONTINUE`` (and its RETRY siblings) to
        ``ams_control("resume", request_pushall=True)`` — a stub that answered with a
        print resume put a different command on the wire from the one production
        sends, which is exactly the distinction the 002-H2S incident turns on."""
        self.calls.append(("hms_action", action))
        return self.ams_control("resume", request_pushall=True)


def _wire(monkeypatch, state, client, *, on_poll=None):
    """Point the recovery module's live-state/client lookups at the scripted pair.

    ``on_poll(n, state)`` (optional) runs on every live-state read, so a test can
    drive AMS telemetry that changes *between* polls (the filament-change cycle) and
    assert what the machine had done by then.
    """
    polls = {"n": 0}

    def _status(_pid):
        polls["n"] += 1
        if on_poll is not None:
            on_poll(polls["n"], state)
        return state

    monkeypatch.setattr(spool_recovery.printer_manager, "get_status", _status)
    monkeypatch.setattr(spool_recovery.printer_manager, "get_client", lambda _pid: client)
    return polls


def _repause_after_running(times=1, *, hms=None):
    """``on_poll`` that re-PAUSEs the print the poll AFTER the driver saw RUNNING.

    THE RE-JAM SHAPE, and it is not interchangeable with ``resume_repauses``: there
    the resume never takes at all (the driver never sees RUNNING), here it takes and
    the fault re-PAUSEs the print underneath it. The two are what WS2 separates —
    only the second is evidence against the replacement spool. Flipping on the FIRST
    RUNNING poll would script the first shape while claiming the second, because
    ``_wire`` runs this hook BEFORE handing the state back.

    ``hms`` replaces the live error list at each flip, so a pin can say WHICH tray the
    firmware blamed for the re-PAUSE.
    """
    seen = {"running": 0, "left": times}

    def _poll(_n, st):
        if st.state != "RUNNING" or seen["left"] <= 0:
            return
        seen["running"] += 1
        if seen["running"] < 2:
            return  # this is the poll that BREAKS phase 1 — let the driver have it
        seen["running"] = 0
        seen["left"] -= 1
        st.state = "PAUSE"
        if hms is not None:
            st.hms_errors = list(hms)

    return _poll


def _spy(monkeypatch, name):
    from backend.app.services.notification_service import notification_service

    m = AsyncMock()
    monkeypatch.setattr(notification_service, name, m)
    return m


def _spy_ws(monkeypatch):
    from backend.app.core.websocket import ws_manager

    calls: list[dict] = []

    async def _b(msg):
        calls.append(msg)

    monkeypatch.setattr(ws_manager, "broadcast", _b)
    return calls


def _count_sleeps(monkeypatch):
    """Count asyncio.sleep invocations during the driver run. A confirm-wait poll
    sleeps; an offline no-op send must NOT enter a confirm wait, so a fully-offline
    recovery records zero sleeps."""
    real_sleep = asyncio.sleep
    n = {"count": 0}

    async def _sleep(delay):
        n["count"] += 1
        await real_sleep(0)

    monkeypatch.setattr(spool_recovery.asyncio, "sleep", _sleep)
    return n


# --- DB helpers -------------------------------------------------------------


async def _farm_item(db, printer_id, *, subtask="task-1", ams_mapping="[0, -1, -1, -1]"):
    batch = PrintBatch(name="run", sku_file_id=1, status="active")
    db.add(batch)
    await db.flush()
    item = PrintQueueItem(
        printer_id=printer_id,
        batch_id=batch.id,
        status="printing",
        dispatch_subtask_id=subtask,
        ams_mapping=ams_mapping,
        started_at=datetime.utcnow(),
    )
    db.add(item)
    await db.commit()
    return item


async def _bind_spool(db, printer_id, ams_id, tray_id, *, weight_used=0.0, feed_fault_at=None, feed_fault_code=None):
    sp = Spool(
        material="PETG",
        color_name="Green",
        brand="Bambu",
        label_weight=1000,
        core_weight=250,
        weight_used=weight_used,
        feed_fault_at=feed_fault_at,
        feed_fault_code=feed_fault_code,
    )
    sp.k_profiles = []
    sp.assignments = []
    db.add(sp)
    await db.flush()
    db.add(SpoolAssignment(spool_id=sp.id, printer_id=printer_id, ams_id=ams_id, tray_id=tray_id))
    await db.commit()
    return sp


# ===========================================================================
# Happy path + production replays
# ===========================================================================


async def test_happy_path(db_session, printer_factory, install_settings, monkeypatch):
    install_settings()
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    jammed = await _bind_spool(db_session, printer.id, 0, 0)  # jammed tray0
    state = _make_state()
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    assert task is not None
    await task

    assert ("unload",) in client.calls
    assert ("load", 1) in client.calls
    assert client.calls.count(("resume",)) == 1

    db_session.expunge_all()
    refreshed = await db_session.get(PrintQueueItem, item.id)
    assert refreshed.waiting_reason is None  # cleared on success
    assert json.loads(refreshed.ams_mapping) == [1, -1, -1, -1]  # jammed 0 -> replacement 1
    jammed_after = await db_session.get(Spool, jammed.id)
    assert jammed_after.feed_fault_at is not None  # jammed spool put out of rotation
    assert jammed_after.feed_fault_code == "0700_8010"


async def test_load_needs_resend(db_session, printer_factory, install_settings, monkeypatch):
    """Replays 16:20:19 -> 16:20:59: the first load didn't take, the resend did."""
    install_settings()
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    state = _make_state()
    client = FakeClient(state, load_after=2)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    await task

    assert client.calls.count(("load", 1)) == 2  # needed a second send
    assert state.state == "RUNNING"


async def test_resume_needs_second_cycle(db_session, printer_factory, install_settings, monkeypatch):
    """Replays 16:21:07 -> 16:22:57: resume didn't stick, one pause/resume fixed it."""
    install_settings()
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    state = _make_state()
    client = FakeClient(state, resume_repauses=1)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    await task

    assert client.calls.count(("resume",)) == 2
    assert client.calls.count(("pause",)) == 1
    assert state.state == "RUNNING"
    db_session.expunge_all()
    assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason is None


# ===========================================================================
# Candidate escalation
# ===========================================================================


async def test_replacement_rejams_tries_next_candidate(db_session, printer_factory, install_settings, monkeypatch):
    install_settings()
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    rejam = await _bind_spool(db_session, printer.id, 0, 1)  # replacement tray1 (will re-jam)
    state = _make_state(trays=[_ams_tray(0), _ams_tray(1), _ams_tray(2)])
    # First candidate (tray1) re-jams through both resume attempts; tray2 succeeds.
    # A RE-JAM, not a resume that never took: the print reaches RUNNING and the
    # standing fault re-PAUSEs it, which is the only shape that may stamp a spool.
    client = FakeClient(state)
    _wire(monkeypatch, state, client, on_poll=_repause_after_running(2))

    task = await on_ams_fault(printer.id, state)
    await task

    assert ("load", 1) in client.calls and ("load", 2) in client.calls
    assert state.state == "RUNNING"
    db_session.expunge_all()
    rejam_after = await db_session.get(Spool, rejam.id)
    assert rejam_after.feed_fault_at is not None  # re-jammed replacement marked
    refreshed = await db_session.get(PrintQueueItem, item.id)
    assert refreshed.waiting_reason is None
    assert json.loads(refreshed.ams_mapping) == [2, -1, -1, -1]  # landed on tray2


async def test_candidates_exhausted_escalates(db_session, printer_factory, install_settings, monkeypatch):
    install_settings()
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    succeeded = _spy(monkeypatch, "on_spool_recovery_succeeded")
    state = _make_state(trays=[_ams_tray(0), _ams_tray(1), _ams_tray(2), _ams_tray(3)])
    client = FakeClient(state, resume_repauses=99)  # every replacement re-jams
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    await task

    succeeded.assert_not_awaited()
    failed.assert_awaited_once()
    assert state.state == "PAUSE"  # never resumed blind
    db_session.expunge_all()
    assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason == WAITING_REASON_FAILED


# ===========================================================================
# External-interference aborts
# ===========================================================================


async def test_external_resume_aborts(db_session, printer_factory, install_settings, monkeypatch):
    install_settings()
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    succeeded = _spy(monkeypatch, "on_spool_recovery_succeeded")
    state = _make_state()
    client = FakeClient(state, external_resume_on_unload=True)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    await task

    succeeded.assert_not_awaited()
    assert state.state == "RUNNING"  # the external actor's resume stands
    db_session.expunge_all()
    assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason is None  # stale flag dropped


async def test_pending_tray_target_hijack_aborts(db_session, printer_factory, install_settings, monkeypatch):
    install_settings()
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    succeeded = _spy(monkeypatch, "on_spool_recovery_succeeded")
    state = _make_state()
    client = FakeClient(state, hijack_on_load=True)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    await task

    succeeded.assert_not_awaited()
    db_session.expunge_all()
    assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason is None


# ===========================================================================
# Entry gates
# ===========================================================================


async def test_disabled_setting_noop(db_session, printer_factory, monkeypatch):
    # Uses the REAL settings read; the toggle is off.
    from backend.app.api.routes.settings import set_setting

    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    await set_setting(db_session, "spool_recovery_enabled", "false")
    await db_session.commit()
    state = _make_state()
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    assert task is None
    assert client.calls == []


async def test_foreign_subtask_leaves_the_farm_unit_alone(db_session, printer_factory, install_settings, monkeypatch):
    """The live print echoes a DIFFERENT subtask id than the farm unit on this
    printer, so the fault is FOREIGN: the machine still runs (2026-08-10 ruling), but
    the other job's queue row is never projected onto."""
    install_settings()
    printer = await printer_factory()
    other = await _farm_item(db_session, printer.id, subtask="task-1")
    state = _make_state(subtask="foreign-999")
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    assert task is not None
    await task

    # The swap ran to a resolved close, and the incident it opened was FOREIGN.
    assert ("unload",) in client.calls
    assert await _incident_row(db_session, printer.id) is None  # resolved
    closed = (await db_session.execute(select(PrinterIncident))).scalars().all()
    assert [(c.item_id, c.job_id, c.status) for c in closed] == [(None, "foreign-999", "resolved")]
    db_session.expunge_all()
    assert (await db_session.get(PrintQueueItem, other.id)).waiting_reason is None


async def test_non_farm_no_item_still_recovers(db_session, printer_factory, install_settings, monkeypatch):
    """No queue item at ALL — the shape a touchscreen / Bambu Studio print has. The
    wire still names the jammed feeder (tray_now), so the swap machine runs."""
    install_settings()
    printer = await printer_factory()  # no queue item at all
    state = _make_state()
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    assert task is not None
    await task

    assert ("unload",) in client.calls
    assert ("load", 1) in client.calls


async def test_multi_feeder_escalates_immediately(db_session, printer_factory, install_settings, monkeypatch):
    install_settings()
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id, ams_mapping="[0, 1, -1, -1]")  # two feeders
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    state = _make_state()
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    assert task is None  # no driver spawned — escalated inline
    assert client.calls == []  # no unload/load/resume on a multi-feeder job
    failed.assert_awaited_once()
    db_session.expunge_all()
    assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason == WAITING_REASON_FAILED


async def test_dedup_blocks_while_incident_active(db_session, printer_factory, install_settings, monkeypatch):
    """While a recovery is IN PROGRESS, a repeat of the same (printer, job, codes)
    is a no-op — the dedup key is added synchronously at the entry gate."""
    install_settings(step_timeout_s=5.0)
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    state = _make_state()
    # Unload never confirms (tray_now never reaches 255) → task1 stays busy in the
    # confirm loop, so the incident is genuinely ACTIVE when the duplicate arrives.
    client = FakeClient(state, unload_after=9999)
    _wire(monkeypatch, state, client)

    task1 = await on_ams_fault(printer.id, state)
    assert task1 is not None
    task2 = await on_ams_fault(printer.id, state)
    assert task2 is None  # dedup: same incident still live
    task1.cancel()
    try:
        await task1
    except asyncio.CancelledError:
        pass


async def test_success_rearms_same_code(db_session, printer_factory, install_settings, monkeypatch):
    """A SUCCESSFUL recovery discards the dedup key so a genuine second tangle in
    the same job (same code) spawns a NEW recovery task."""
    install_settings()
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    await _bind_spool(db_session, printer.id, 0, 0)  # tray0 → marked OOR by round 1
    # Three loaded trays so a SECOND jam (now on tray1) still has an eligible spool.
    state = _make_state(trays=[_ams_tray(0), _ams_tray(1), _ams_tray(2)])
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task1 = await on_ams_fault(printer.id, state)
    assert task1 is not None
    await task1
    assert state.state == "RUNNING"

    # Fresh pause on the same job + same code -> NEW task (dedup re-armed on success).
    state.state = "PAUSE"
    state.tray_now = 1  # currently on the replacement chosen in round 1
    task2 = await on_ams_fault(printer.id, state)
    assert task2 is not None
    await task2
    assert state.state == "RUNNING"


async def test_transient_close_rearms(db_session, printer_factory, install_settings, monkeypatch):
    """A never-PAUSEd transient close re-arms too — a later genuine PAUSE with the
    same code spawns a new task."""
    install_settings()
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    await _bind_spool(db_session, printer.id, 0, 0)
    state = _make_state(gcode_state="RUNNING")  # firmware rescued — never PAUSEs
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task1 = await on_ams_fault(printer.id, state)
    assert task1 is not None
    await task1
    assert client.calls == []  # closed as transient, never acted

    state.state = "PAUSE"  # a real jam this time
    # Production drives the wire sampler on EVERY push, and the RUNNING→PAUSE edge
    # is what re-arms a transient close: "it never held the printer" is exactly the
    # answer a pause invalidates.
    spool_recovery.note_demand_watch(printer.id, state)
    task2 = await on_ams_fault(printer.id, state)
    assert task2 is not None
    await task2
    assert state.state == "RUNNING"


# ===========================================================================
# Extruder-side feed fault (0300_801E)
# ===========================================================================


async def test_extruder_overload_triggers_recovery(db_session, printer_factory, install_settings, monkeypatch):
    """The H2S main-extruder-overload code (0300_801E) now triggers recovery.

    The SWAP commits — and parks nothing. 006-H2S 2026-09-21 (incident 289): the fault
    the driver is reacting to says the EXTRUDER overloaded, so the roll that happened to
    be feeding is not the suspect, and the stamp this test used to assert took a healthy
    spool out of rotation 12 ms after the fault and paged the operator about it.
    """
    install_settings()
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    jammed = await _bind_spool(db_session, printer.id, 0, 0)  # jammed tray0
    state = _make_state(hms=[_extruder_hms()])
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    assert task is not None
    await task

    assert ("unload",) in client.calls  # the swap still commits...
    assert ("load", 1) in client.calls
    assert state.state == "RUNNING"
    db_session.expunge_all()
    refreshed = await db_session.get(PrintQueueItem, item.id)
    assert refreshed.waiting_reason is None  # cleared on success
    jammed_after = await db_session.get(Spool, jammed.id)
    assert jammed_after.feed_fault_at is None  # ...and the extruder-side fault parks no spool
    assert jammed_after.feed_fault_code is None


async def test_extruder_side_rejam_keeps_replacement_in_rotation(
    db_session, printer_factory, install_settings, monkeypatch
):
    """On an extruder-side fault the extruder is the common factor: a re-jam after
    the swap keeps the replacement IN rotation (feed_fault_at NULL) and tries the
    next candidate. Since 006-H2S 2026-09-21 the ORIGINAL is kept in rotation too —
    one rule, one home (`_commit_out_of_rotation`), whichever spool was feeding."""
    install_settings()
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    original = await _bind_spool(db_session, printer.id, 0, 0)  # jammed tray0
    replacement = await _bind_spool(db_session, printer.id, 0, 1)  # tray1 re-jams
    state = _make_state(trays=[_ams_tray(0), _ams_tray(1), _ams_tray(2)], hms=[_extruder_hms()])
    client = FakeClient(state)  # tray1 re-jams both cycles; tray2 succeeds
    _wire(monkeypatch, state, client, on_poll=_repause_after_running(2))

    task = await on_ams_fault(printer.id, state)
    await task

    assert ("load", 1) in client.calls and ("load", 2) in client.calls
    assert state.state == "RUNNING"
    db_session.expunge_all()
    replacement_after = await db_session.get(Spool, replacement.id)
    assert replacement_after.feed_fault_at is None  # extruder-side → kept in rotation
    original_after = await db_session.get(Spool, original.id)
    assert original_after.feed_fault_at is None  # ...and so is the original (incident 289)
    refreshed = await db_session.get(PrintQueueItem, item.id)
    assert json.loads(refreshed.ams_mapping) == [2, -1, -1, -1]  # landed on tray2


# ===========================================================================
# Escalation / abort latch (sibling-code re-entry guard)
# ===========================================================================


async def test_escalation_latch_blocks_sibling_code(db_session, printer_factory, install_settings, monkeypatch):
    """After recovery escalates for a job, a sibling code from the SAME physical
    fault must not restart recovery behind the operator's back."""
    install_settings()
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    _spy(monkeypatch, "on_spool_recovery_failed")
    state = _make_state(trays=[_ams_tray(0)])  # only the jammed tray loaded → escalate
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task1 = await on_ams_fault(printer.id, state)
    assert task1 is not None
    await task1
    assert state.state == "PAUSE"  # escalated, left paused
    calls_after = len(client.calls)

    task2 = await on_ams_fault(printer.id, state)
    assert task2 is None  # latched
    assert len(client.calls) == calls_after  # no new interaction


async def test_abort_latch_blocks_sibling_code(db_session, printer_factory, install_settings, monkeypatch):
    """After an external-interference abort, a sibling code must not restart
    recovery under the actor who took over."""
    install_settings()
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    state = _make_state()
    client = FakeClient(state, external_resume_on_unload=True)  # external actor resumes mid-recovery
    _wire(monkeypatch, state, client)

    task1 = await on_ams_fault(printer.id, state)
    assert task1 is not None
    await task1
    assert state.state == "RUNNING"  # the external actor's resume stands
    calls_after = len(client.calls)

    task2 = await on_ams_fault(printer.id, state)
    assert task2 is None  # latched
    assert len(client.calls) == calls_after


async def test_escalation_hold_does_not_outlive_its_job(db_session, printer_factory, install_settings, monkeypatch):
    """An escalated hold is scoped to its job: once that job reaches a terminal the
    incident CLOSES, and the next job on the same printer recovers normally.

    Pre-WS2b this was a process-lifetime ``_escalated`` latch keyed (printer, job),
    which never expired — finding (c): a later, different fault on the SAME job could
    never be recovered. Now the hold is an open incident and the lifecycle ends it."""
    install_settings()
    printer = await printer_factory()
    _spy(monkeypatch, "on_spool_recovery_failed")

    # Job 1 escalates (no eligible replacement).
    await _farm_item(db_session, printer.id, subtask="task-1")
    state1 = _make_state(subtask="task-1", trays=[_ams_tray(0)])
    client1 = FakeClient(state1)
    _wire(monkeypatch, state1, client1)
    task1 = await on_ams_fault(printer.id, state1)
    await task1
    assert state1.state == "PAUSE"

    # Job 1 reaches its terminal — the hold closes with it (in production this is
    # main.on_print_complete's per-print reset; the printer's RUNNING edge closes it
    # too).
    assert await spool_recovery.on_job_terminal(printer.id, _terminal()) is True

    # Job 2 on the same printer is a fresh incident — recovers normally.
    await _farm_item(db_session, printer.id, subtask="task-2")
    await _bind_spool(db_session, printer.id, 0, 0)
    state2 = _make_state(subtask="task-2")
    client2 = FakeClient(state2)
    _wire(monkeypatch, state2, client2)
    task2 = await on_ams_fault(printer.id, state2)
    assert task2 is not None
    await task2
    assert state2.state == "RUNNING"


# ===========================================================================
# Per-job success cap (flap bound)
# ===========================================================================


async def test_success_cap_escalates(db_session, printer_factory, install_settings, monkeypatch):
    """Once a job has hit the per-job success cap, the next fault escalates with
    the repeated_jams reason instead of swapping again."""
    install_settings()
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id, subtask="task-1")
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    # Simulate the flap cap already reached this job: the durable ledger counts
    # RESOLVED jam incidents, so a restart no longer hands a sick printer a fresh
    # budget the way the in-memory counter did.
    from backend.app.services import printer_incidents

    for n in range(spool_recovery._MAX_SUCCESSES_PER_JOB):
        seeded = await _seed_incident(
            db_session, printer.id, kind="jam", code="0700_8010", codes=f"seeded-{n}", status="recovering"
        )
        await printer_incidents.close(db_session, seeded.id, status="resolved", source="observed_running")
    state = _make_state(subtask="task-1")
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    assert task is None  # escalated inline, no driver spawned
    assert client.calls == []  # never touched the printer
    failed.assert_awaited_once()
    assert "keeps returning" in failed.call_args.kwargs["detail"]  # repeated_jams detail
    db_session.expunge_all()
    assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason == WAITING_REASON_FAILED


# ===========================================================================
# Gate-out observability
# ===========================================================================


async def test_gate_out_logging(db_session, printer_factory, install_settings, monkeypatch, caplog):
    """A gated return-None with recoverable codes live logs INFO with the codes."""
    install_settings(enabled=False)  # disabled gate
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    state = _make_state()
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    with caplog.at_level(logging.INFO, logger="backend.app.services.spool_recovery"):
        task = await on_ams_fault(printer.id, state)
    assert task is None
    # One line per OUTCOME CHANGE per (printer, job) — naming the fault fingerprint
    # and why nothing owned it (the entry gate is per-push now, so a line per push
    # would be a log storm).
    assert any("0700_8010" in r.getMessage() and "not owned" in r.getMessage() for r in caplog.records)


# ===========================================================================
# Runout handling
# ===========================================================================


async def test_runout_rescued_by_firmware_transient_close(db_session, printer_factory, install_settings, monkeypatch):
    install_settings()
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    # Backup switched: the print never PAUSEs.
    state = _make_state(gcode_state="RUNNING", hms=[_runout_hms()])
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    assert task is not None
    await task

    assert client.calls == []  # never acted — closed as transient
    db_session.expunge_all()
    assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason is None


async def test_runout_escalates_immediately_zero_loads(db_session, printer_factory, install_settings, monkeypatch):
    """W2: a stuck runout PAUSE escalates IMMEDIATELY with the runout token and
    ZERO ams_change_filament (load) sends — firmware refuses cross-slot loads in the
    8011 insert-same-slot state, so the swap machine never runs. Even with an
    eligible replacement present, recovery does not try to load it."""
    install_settings()
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    jammed = await _bind_spool(db_session, printer.id, 0, 0)  # the "ran out" tray
    await _bind_spool(db_session, printer.id, 0, 1)  # a same-material replacement IS loaded
    oor = _spy(monkeypatch, "on_spool_out_of_rotation")
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    state = _make_state(hms=[_runout_hms()], trays=[_ams_tray(0), _ams_tray(1)])  # PAUSE, runout code
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    await task

    assert state.state == "PAUSE"  # never resumed — left for a same-slot refill
    assert not any(c[0] == "load" for c in client.calls)  # ZERO cross-slot load commands
    assert ("unload",) not in client.calls  # the whole swap machine was skipped
    failed.assert_awaited_once()
    assert failed.call_args.kwargs["kind"] == "runout"  # runout copy branch
    oor.assert_not_awaited()  # runout spool is SPENT — never marked out-of-rotation
    db_session.expunge_all()
    jammed_after = await db_session.get(Spool, jammed.id)
    assert jammed_after.feed_fault_at is None  # no feed-fault marking on a runout
    assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason == WAITING_REASON_RUNOUT


async def test_runout_escalation_detail_names_slot_refill(db_session, printer_factory, install_settings, monkeypatch):
    """The runout escalation carries the runout_needs_refill detail (same-slot refill
    guidance), not a jam reason."""
    install_settings()
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    state = _make_state(hms=[_runout_hms()])
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    await task

    failed.assert_awaited_once()
    assert "same" in failed.call_args.kwargs["detail"].lower()  # "insert into the SAME slot"


# --- WS2b: the escalation carries the DURABLE spent stamp --------------------
#
# The wire-edge spent lanes are re-seeded by every restart, so a hold spanning a deploy
# would never stamp. The escalation is the durable, incident-anchored event, so it hands
# the exhaustion to spool_respool — which stays the ONE spent writer. What matters here
# is the GATE: which escalations may claim a roll ran out, and which may not.


def _spy_hold_stamp(monkeypatch):
    """Record every ``mark_spent_on_runout_hold`` call the escalation makes."""
    from backend.app.services import spool_respool

    calls: list[tuple] = []

    async def _stamp(printer_id, state, *, subtask_id, session_factory=None):
        calls.append((printer_id, subtask_id, state))

    monkeypatch.setattr(spool_respool, "mark_spent_on_runout_hold", _stamp)
    return calls


async def test_runout_escalation_invokes_hold_stamp(db_session, printer_factory, install_settings, monkeypatch):
    """A held AMS runout (``runout_needs_refill``) hands the stamp exactly one call,
    naming this printer, this job, and the LIVE state — the resolver has to re-read the
    wire itself rather than trust the incident row's stored tray (the 185/205 class)."""
    install_settings()
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    _spy(monkeypatch, "on_spool_recovery_failed")
    calls = _spy_hold_stamp(monkeypatch)
    state = _make_state(hms=[_runout_same_slot_hms()])
    _wire(monkeypatch, state, FakeClient(state))

    task = await on_ams_fault(printer.id, state)
    await task

    assert [(pid, job) for pid, job, _st in calls] == [(printer.id, "task-1")]
    assert calls[0][2] is state


async def test_external_runout_escalation_does_not_stamp(db_session, printer_factory, install_settings, monkeypatch):
    """The spool HOLDER ran dry (``external_spool_runout``). External rows are bindable
    (ams_id 255), but which vt-tray a dual-holder model's fault names is unconfirmed —
    and a wrong-side stamp is permanent, so v1 stamps nothing here."""
    install_settings()
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    _spy(monkeypatch, "on_spool_recovery_failed")
    calls = _spy_hold_stamp(monkeypatch)
    state = _make_state(hms=[_external_runout_hms()])
    _wire(monkeypatch, state, FakeClient(state))

    task = await on_ams_fault(printer.id, state)
    await task

    assert calls == []


async def test_recovery_interrupted_escalation_does_not_stamp(
    db_session, printer_factory, install_settings, monkeypatch
):
    """``recovery_interrupted`` is a RESTART ARTIFACT — a zombie ``recovering`` row whose
    wire carries no actionable fault at all. It escalates because a half-executed swap is
    not "fine", not because a roll ran out; there is no live runout evidence to stamp
    from, whatever kind the stale row happened to carry."""
    install_settings()
    printer = await printer_factory()
    _spy(monkeypatch, "on_spool_recovery_failed")
    calls = _spy_hold_stamp(monkeypatch)
    _wire(monkeypatch, _make_state(hms=[_runout_same_slot_hms()]), None)
    runout_incident = await _owned_incident(
        db_session, printer.id, kind=spool_recovery.KIND_RUNOUT, step_timeout_s=0.05
    )

    await spool_recovery._escalate(runout_incident, "recovery_interrupted")

    assert calls == []


# ===========================================================================
# Layer-conditional minimum-start floor
# ===========================================================================


async def test_low_spool_in_protected_layers_escalates(db_session, printer_factory, install_settings, monkeypatch):
    install_settings(protect_layers=7)
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    await _bind_spool(db_session, printer.id, 0, 1, weight_used=950.0)  # replacement, remaining 50g
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    state = _make_state(layer=5)  # below the protected-layer threshold
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    await task

    failed.assert_awaited_once()
    assert state.state == "PAUSE"
    db_session.expunge_all()
    assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason == WAITING_REASON_FAILED


async def test_low_spool_after_protected_layers_selected(db_session, printer_factory, install_settings, monkeypatch):
    install_settings(protect_layers=7)
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    await _bind_spool(db_session, printer.id, 0, 1, weight_used=950.0)  # remaining 50g
    state = _make_state(layer=8)  # at/after the threshold -> low spool IS eligible
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    await task

    assert ("load", 1) in client.calls
    assert state.state == "RUNNING"
    db_session.expunge_all()
    assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason is None


async def test_near_empty_spool_after_protected_layers_escalates_near_empty(
    db_session, printer_factory, install_settings, monkeypatch
):
    """W2 hard floor: past the protected layers a low-but-not-empty spool loads, but
    a KNOWN-EMPTY one (≤ _RECOVERY_HARD_MIN_G) never does — it escalates the new
    only_near_empty_spools reason, not the protected-layer one.

    And the verdict arrives BEFORE the swap commits (004-H2S 2026-09-17): nothing is
    unloaded, so the page says the jammed spool is still loaded."""
    install_settings(protect_layers=7)
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    await _bind_spool(db_session, printer.id, 0, 1, weight_used=997.0)  # remaining 3g < 5g floor
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    state = _make_state(layer=8)  # at/after the threshold
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    await task

    assert not any(c[0] == "load" for c in client.calls)  # never loaded the empty spool
    failed.assert_awaited_once()
    detail = failed.call_args.kwargs["detail"]
    assert "effectively empty" in detail  # only_near_empty_spools
    assert ("unload",) not in client.calls  # the swap never committed
    assert "The jammed spool is still loaded (AMS A slot 1)" in detail
    assert state.state == "PAUSE"
    db_session.expunge_all()
    assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason == WAITING_REASON_FAILED


async def test_below_protected_layers_uses_protected_layer_reason(
    db_session, printer_factory, install_settings, monkeypatch
):
    """Below the protected layers a low spool escalates the protected-layer reason
    (NOT only_near_empty_spools) — the ordinary minimum-start floor still applies."""
    install_settings(protect_layers=7)
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    await _bind_spool(db_session, printer.id, 0, 1, weight_used=950.0)  # remaining 50g < 120g min-start
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    state = _make_state(layer=3)  # below the threshold
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    await task

    failed.assert_awaited_once()
    assert "this early in the print" in failed.call_args.kwargs["detail"]  # only_low_spools_in_protected_layers


# ===========================================================================
# W2 presence filter: a seated-but-unsensed candidate (state 9) is excluded;
# a None/unparseable state fails OPEN (kept).
# ===========================================================================


async def test_state9_candidate_excluded(db_session, printer_factory, install_settings, monkeypatch):
    """A candidate tray reporting state 9 (seated but unsensed) is dropped from the
    replacement scan — a load there is doomed — so recovery escalates."""
    install_settings()
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    # tray0 jammed (state 11), tray1 the only other loaded tray but state 9.
    state = _make_state(trays=[_ams_tray(0), _ams_tray(1, state=9)])
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    await task

    assert not any(c == ("load", 1) for c in client.calls)  # state-9 tray never loaded
    failed.assert_awaited_once()
    assert state.state == "PAUSE"
    db_session.expunge_all()
    assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason == WAITING_REASON_FAILED


async def test_state_none_candidate_kept(db_session, printer_factory, install_settings, monkeypatch):
    """A candidate whose state is None/unparseable fails OPEN (kept) — dialect
    variance must never exclude a real replacement."""
    install_settings()
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    state = _make_state(trays=[_ams_tray(0), _ams_tray(1, state=None)])  # tray1 state unknown
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    await task

    assert ("load", 1) in client.calls  # kept and loaded despite unknown state
    assert state.state == "RUNNING"
    db_session.expunge_all()
    assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason is None


# ===========================================================================
# W3: every recovery load send is marked as ours (note_commanded_load) so the
# backup-swap detector can't spend the departed spool.
# ===========================================================================


async def test_load_step_notes_commanded_load(db_session, printer_factory, install_settings, monkeypatch):
    """The load step stamps note_commanded_load(printer_id, target) before each send."""
    from backend.app.services import spool_respool

    install_settings()
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    await _bind_spool(db_session, printer.id, 0, 0)  # jammed tray0
    noted: list[tuple[int, int]] = []
    monkeypatch.setattr(spool_respool, "note_commanded_load", lambda pid, tray: noted.append((pid, tray)))
    state = _make_state(trays=[_ams_tray(0), _ams_tray(1)])
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    await task

    assert (printer.id, 1) in noted  # the replacement load was marked as ours
    assert state.state == "RUNNING"


# ===========================================================================
# Restart short-circuit + unbound slot
# ===========================================================================


async def test_restart_clean_state_skips_unload(db_session, printer_factory, install_settings, monkeypatch):
    """The ONE state the unload skip survives for: nothing feeding (tray_now 255),
    the AMS state machine idle, and no feed-fault code standing — a post-restart
    re-fire of a fault the firmware already unloaded out of. The jammed tray is still
    identified from the item's single-feeder ams_mapping."""
    install_settings()
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    # No feed-fault code STANDING — which is the whole premise, and since WS2b the
    # entry gate derives its candidates from the live wire, so this state can only be
    # reached by an incident already in flight. Driven through the driver directly.
    state = _make_state(tray_now=255, ams_status_main=0, hms=[])
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    await spool_recovery._run_recovery(
        await _owned_incident(db_session, printer.id, step_timeout_s=0.05, item_id=item.id)
    )

    assert ("unload",) not in client.calls  # short-circuited
    assert ("load", 1) in client.calls
    assert state.state == "RUNNING"
    db_session.expunge_all()
    assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason is None


async def test_unbound_jammed_slot_proceeds(db_session, printer_factory, install_settings, monkeypatch):
    install_settings()
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)  # jammed tray0 has NO assignment
    state = _make_state()
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    await task

    # Marking is a no-op with no bound spool, but recovery proceeds and succeeds.
    assert state.state == "RUNNING"
    db_session.expunge_all()
    assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason is None


# ===========================================================================
# clear_on_reinsert (presence-edge persistence clear)
# ===========================================================================


async def test_clear_on_reinsert_assignment_bound(db_session, printer_factory, monkeypatch):
    printer = await printer_factory()
    spool = await _bind_spool(
        db_session, printer.id, 0, 0, feed_fault_at=datetime.utcnow(), feed_fault_code="0700_8010"
    )
    ws = _spy_ws(monkeypatch)

    await clear_on_reinsert(db_session, printer.id, 0, 0, _ams_tray(0))

    db_session.expunge_all()
    cleared = await db_session.get(Spool, spool.id)
    assert cleared.feed_fault_at is None
    assert cleared.feed_fault_code is None
    assert {"type": "inventory_changed"} in ws


async def test_clear_on_reinsert_tag_identity_fallback(db_session, printer_factory, monkeypatch):
    printer = await printer_factory()
    tag = "AABBCCDD11223344"
    uuid = "AABBCCDD11223344AABBCCDD11223344"
    # Out-of-rotation spool with a tag identity but NO current assignment.
    spool = Spool(
        material="PETG",
        label_weight=1000,
        core_weight=250,
        tag_uid=tag,
        tray_uuid=uuid,
        feed_fault_at=datetime.utcnow(),
        feed_fault_code="0701_8010",
    )
    spool.k_profiles = []
    spool.assignments = []
    db_session.add(spool)
    await db_session.commit()
    ws = _spy_ws(monkeypatch)

    await clear_on_reinsert(db_session, printer.id, 0, 2, _ams_tray(2, tag=tag, uuid=uuid))

    db_session.expunge_all()
    cleared = await db_session.get(Spool, spool.id)
    assert cleared.feed_fault_at is None
    assert {"type": "inventory_changed"} in ws


async def test_clear_on_reinsert_noop_when_nothing_flagged(db_session, printer_factory, monkeypatch):
    printer = await printer_factory()
    await _bind_spool(db_session, printer.id, 0, 0)  # in rotation (feed_fault_at NULL)
    ws = _spy_ws(monkeypatch)

    await clear_on_reinsert(db_session, printer.id, 0, 0, _ams_tray(0))

    assert ws == []  # nothing to clear -> no broadcast


# ===========================================================================
# Present-but-bare tray recovery (18:45 runout: full spool sat unusable in a
# bare tray while recovery escalated no_eligible_spool in ~200 ms)
# ===========================================================================


def _bare_tray(tid, *, state=11):
    """A present-but-BARE tray: seated (state 10/11) with an empty tray_type and
    no RFID tag — invisible to the loaded-tray scan until it is configured."""
    return {
        "id": tid,
        "tray_type": "",
        "tray_color": "",
        "tray_info_idx": "",
        "remain": -1,
        "state": state,
        "tag_uid": _NONE_TAG,
        "tray_uuid": _NONE_UUID,
    }


async def test_bare_candidate_forced_autoconfig_then_loads(db_session, printer_factory, install_settings, monkeypatch):
    """A present-but-BARE candidate tray (invisible to the loaded scan) is
    force-configured, becomes visible in live telemetry, and recovery loads it."""
    install_settings()
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    await _bind_spool(db_session, printer.id, 0, 1)  # matching DB spool on the bare candidate slot
    state = _make_state(trays=[_ams_tray(0), _bare_tray(1)])  # tray0 jammed+configured, tray1 bare
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    from backend.app.services import spool_tagless

    seen: dict = {}

    async def _fake_autoconfig(db, pid, ams_id, tray_id, tray, *, force=False):
        seen["force"] = force
        seen["slot"] = (ams_id, tray_id)
        # Simulate the firmware applying the pushed config: the bare tray gains a
        # tray_type/color in live telemetry.
        for unit in state.raw_data["ams"]:
            for t in unit["tray"]:
                if t["id"] == tray_id:
                    t["tray_type"] = "PETG"
                    t["tray_color"] = "00FF00FF"
        return True

    monkeypatch.setattr(spool_tagless, "maybe_autoconfigure_bare_tray", _fake_autoconfig)

    task = await on_ams_fault(printer.id, state)
    await task

    assert seen.get("force") is True  # forced sweep bypassed the retry window
    assert seen.get("slot") == (0, 1)
    assert ("load", 1) in client.calls  # loaded the now-visible tray
    assert state.state == "RUNNING"


async def test_bare_jammed_tray_requirement_from_db_assignment(
    db_session, printer_factory, install_settings, monkeypatch
):
    """A BARE jammed tray no longer ends recovery before the candidate scan: the
    requirement falls back to the jammed tray's DB spool and the scan proceeds."""
    install_settings()
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)  # single-feeder mapping [0]
    await _bind_spool(db_session, printer.id, 0, 0)  # jammed tray0's DB spool = PETG
    # tray0 (jammed) is BARE; tray1 is a configured PETG candidate.
    state = _make_state(tray_now=255, trays=[_bare_tray(0), _ams_tray(1)])
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    await task

    assert ("load", 1) in client.calls  # scan proceeded off the DB-derived requirement
    assert state.state == "RUNNING"


async def test_still_bare_after_forced_sweep_escalates(db_session, printer_factory, install_settings, monkeypatch):
    """If the forced bare-tray sweep never yields a configured tray, recovery
    escalates no_eligible_spool exactly as before."""
    install_settings()
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    state = _make_state(trays=[_ams_tray(0), _bare_tray(1)])  # jammed configured, one bare tray
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    from backend.app.services import spool_tagless

    seen: dict = {}

    async def _fake_autoconfig(db, pid, ams_id, tray_id, tray, *, force=False):
        seen["force"] = force
        return True  # forced, but the config never lands in telemetry

    monkeypatch.setattr(spool_tagless, "maybe_autoconfigure_bare_tray", _fake_autoconfig)

    task = await on_ams_fault(printer.id, state)
    await task

    assert seen.get("force") is True
    failed.assert_awaited_once()
    assert failed.call_args.kwargs["kind"] == "jam"  # feed fault chooses the jam copy
    assert state.state == "PAUSE"  # never resumed blind
    db_session.expunge_all()
    assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason == WAITING_REASON_FAILED


# ===========================================================================
# Observability: tray snapshot + runout-vs-jam escalation copy
# ===========================================================================


async def test_escalation_emits_tray_snapshot(db_session, printer_factory, install_settings, monkeypatch, caplog):
    """Every escalation logs one parseable per-tray snapshot line."""
    install_settings()
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    _spy(monkeypatch, "on_spool_recovery_failed")
    state = _make_state(trays=[_ams_tray(0)])  # only the jammed tray loaded → escalate
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    with caplog.at_level(logging.INFO, logger="backend.app.services.spool_recovery"):
        task = await on_ams_fault(printer.id, state)
        await task

    snapshots = [r for r in caplog.records if "[spool_recovery] tray snapshot" in r.getMessage()]
    assert snapshots  # at least one snapshot emitted on the escalation
    assert "g0(" in snapshots[-1].getMessage()  # the jammed tray appears in the snapshot


def _capture_notifications(monkeypatch):
    """Drive the REAL on_spool_recovery_failed but capture the built (title,
    message) at the send boundary, so the runout-vs-jam copy branch is exercised."""
    from backend.app.services.notification_service import notification_service

    sent: list[tuple[str, str]] = []

    async def _providers(_db, _event, _pid):
        return ["provider"]

    async def _send(
        providers, title, message, db, event_type, printer_id, printer_name, *, force_immediate=False, variables=None
    ):
        sent.append((title, message))

    monkeypatch.setattr(notification_service, "_get_providers_for_event", _providers)
    monkeypatch.setattr(notification_service, "_send_to_providers", _send)
    return sent


async def test_runout_escalation_uses_runout_copy(db_session, printer_factory, install_settings, monkeypatch):
    install_settings()
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    sent = _capture_notifications(monkeypatch)
    state = _make_state(hms=[_runout_hms()], trays=[_ams_tray(0)])  # stuck runout, no replacement
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    await task

    assert state.state == "PAUSE"
    assert sent, "a failed notification was sent"
    title, message = sent[-1]
    assert "runout" in title.lower()  # runout-framed title, not "Spool jam"
    assert "ran out" in message.lower()


async def test_feed_fault_escalation_uses_jam_copy(db_session, printer_factory, install_settings, monkeypatch):
    install_settings()
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    sent = _capture_notifications(monkeypatch)
    state = _make_state(trays=[_ams_tray(0)])  # feed fault, no replacement
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    await task

    assert state.state == "PAUSE"
    assert sent
    title, message = sent[-1]
    assert "runout" not in title.lower()  # jam copy, not runout
    assert "ran out" not in message.lower()


# ===========================================================================
# R3: abort clears the out-of-rotation flag ONLY when the operator resumed ON
# the jammed feeder (declared the self-cleared spool usable). Otherwise the flag
# is retained — a physical reseat stays the canonical clear.
# ===========================================================================


async def test_abort_clears_oor_when_resumed_on_jammed_feeder(
    db_session, printer_factory, install_settings, monkeypatch
):
    """An external actor resumes ON the jammed feeder (RUNNING + tray_now == the
    jammed global tray): the out-of-rotation flag stamped at the swap-commit boundary
    is cleared."""
    install_settings()
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)  # single-feeder mapping [0]
    jammed = await _bind_spool(db_session, printer.id, 0, 0)  # jammed tray0, gets OOR-marked
    _spy(monkeypatch, "on_spool_out_of_rotation")
    ws = _spy_ws(monkeypatch)
    state = _make_state(tray_now=0)  # after the external resume it stays on tray0
    client = FakeClient(state, external_resume_on_unload=True)  # resume mid-recovery, same feeder
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    await task

    assert state.state == "RUNNING"  # the external actor's resume stands
    db_session.expunge_all()
    cleared = await db_session.get(Spool, jammed.id)
    assert cleared.feed_fault_at is None  # resumed on jammed feeder -> flag cleared
    assert cleared.feed_fault_code is None
    assert {"type": "inventory_changed"} in ws
    refreshed = await db_session.get(PrintQueueItem, item.id)
    assert refreshed.waiting_reason is None  # stale recovering flag dropped by abort


async def test_abort_retains_oor_when_resumed_on_other_feeder(
    db_session, printer_factory, install_settings, monkeypatch
):
    """An external actor resumes on a DIFFERENT feeder (tray_now != jammed tray):
    the jammed spool's out-of-rotation flag is RETAINED — a physical reseat stays
    the canonical clear."""
    install_settings()
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)  # single-feeder mapping [0]
    jammed = await _bind_spool(db_session, printer.id, 0, 0)  # jammed tray0, gets OOR-marked
    _spy(monkeypatch, "on_spool_out_of_rotation")
    state = _make_state(tray_now=0, trays=[_ams_tray(0), _ams_tray(1)])
    client = FakeClient(state, external_resume_on_unload=True, external_resume_tray=1)  # resumed on tray1
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    await task

    assert state.state == "RUNNING"
    db_session.expunge_all()
    retained = await db_session.get(Spool, jammed.id)
    assert retained.feed_fault_at is not None  # resumed on a DIFFERENT feeder -> flag stays
    assert retained.feed_fault_code == "0700_8010"


# ===========================================================================
# 4.2: offline (send-returns-False) sites consume the attempt WITHOUT entering a
# confirm wait, so recovery reaches the existing fail path fast instead of burning
# a full step_timeout per silent no-op.
# ===========================================================================


async def test_offline_unload_escalates_without_confirm_waits(
    db_session, printer_factory, install_settings, monkeypatch
):
    """Every MQTT send returns False (printer offline). The unload site consumes
    both attempts with NO confirm wait, then the existing unload_failed escalation
    fires — no full step_timeout confirm poll ran."""
    install_settings(max_attempts=2, step_timeout_s=5.0)  # a real wait would be 5s each
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    _spy(monkeypatch, "on_spool_out_of_rotation")
    sleeps = _count_sleeps(monkeypatch)
    state = _make_state()  # PAUSE, tray_now=0 (not unloaded)
    client = FakeClient(state, unload_ret=False, load_ret=False, resume_ret=False, pause_ret=False)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    await task

    assert client.calls.count(("unload",)) == 2  # both attempts consumed
    assert not any(c[0] == "load" for c in client.calls)  # escalated at unload — never reached load
    failed.assert_awaited_once()
    assert client.calls and state.state == "PAUSE"  # left paused, never resumed blind
    assert sleeps["count"] == 0  # NO confirm-wait poll ran on the offline sends
    db_session.expunge_all()
    assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason == WAITING_REASON_FAILED


async def test_offline_load_advances_without_confirm_wait(db_session, printer_factory, install_settings, monkeypatch):
    """Unload confirms but every load send returns False: both load attempts are
    consumed with no confirm wait, the round advances, and recovery escalates
    (no eligible replacement remains) with zero confirm-wait polls."""
    install_settings(max_attempts=2, step_timeout_s=5.0)
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    _spy(monkeypatch, "on_spool_out_of_rotation")
    sleeps = _count_sleeps(monkeypatch)
    state = _make_state(trays=[_ams_tray(0), _ams_tray(1)])  # tray0 jammed, tray1 candidate
    client = FakeClient(state, load_ret=False)  # unload OK, load always a no-op
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    await task

    assert client.calls.count(("load", 1)) == 2  # both load attempts consumed, then advanced
    failed.assert_awaited_once()
    assert state.state == "PAUSE"
    assert sleeps["count"] == 0  # no confirm-wait poll on the offline load sends
    db_session.expunge_all()
    assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason == WAITING_REASON_FAILED


async def test_offline_resume_advances_without_confirm_wait(db_session, printer_factory, install_settings, monkeypatch):
    """Unload+load confirm but resume/pause sends return False: resume is treated
    as not-taken without a confirm wait, the extra pause/resume cycle skips its
    PAUSE wait, and recovery escalates with zero confirm-wait polls."""
    install_settings(max_attempts=2, step_timeout_s=5.0)
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    _spy(monkeypatch, "on_spool_out_of_rotation")
    sleeps = _count_sleeps(monkeypatch)
    state = _make_state(trays=[_ams_tray(0), _ams_tray(1)])  # tray0 jammed, tray1 candidate
    client = FakeClient(state, resume_ret=False, pause_ret=False)  # unload+load OK, resume/pause no-op
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    await task

    assert ("load", 1) in client.calls  # load reached and confirmed
    assert client.calls.count(("resume",)) == 2  # resume + the extra-cycle resume, both no-op
    failed.assert_awaited_once()
    assert state.state == "PAUSE"  # never resumed blind
    assert sleeps["count"] == 0  # no confirm-wait poll on the offline resume/pause sends
    db_session.expunge_all()
    assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason == WAITING_REASON_FAILED


# ===========================================================================
# R1a: has_live_recovery — the public liveness signal the pause-stall watchdog
# uses instead of the token string, so a restart-orphaned RECOVERING token (no
# live task) is no longer mistaken for "owned".
# ===========================================================================


class _FakeRecoveryTask:
    def __init__(self, done: bool):
        self._done = done

    def done(self) -> bool:
        return self._done


def test_has_live_recovery_no_task():
    """No task registered for the printer → no live recovery."""
    assert spool_recovery.has_live_recovery(4242) is False


def test_has_live_recovery_done_task():
    """A finished task no longer owns the pause (orphan-reclaim territory)."""
    spool_recovery._active_tasks[7] = _FakeRecoveryTask(done=True)
    assert spool_recovery.has_live_recovery(7) is False


def test_has_live_recovery_live_task():
    """A still-running task owns the pause."""
    spool_recovery._active_tasks[7] = _FakeRecoveryTask(done=False)
    assert spool_recovery.has_live_recovery(7) is True


# ===========================================================================
# 009-H2S 2026-07-20: the unload short-circuit that made self-heal impossible.
#
# The AMS sat stuck mid-filament-change (ams_status_main == 1) with tray_now
# already 255 and a standing 0700_8010. The old `tray_now == 255` short-circuit
# meant the machine sent ZERO unloads across four candidate loads — all of which
# were doomed — and escalated to a human. The operator then recovered the identical
# state in 90 s with an explicit unload -> load -> resume. These pins hold that line.
# ===========================================================================


def _escalated_reasons(caplog) -> list[str]:
    """The reason tokens from `_escalate`'s WARNING trail, in order."""
    return [
        r.getMessage().split("ESCALATED (", 1)[1].split(")", 1)[0]
        for r in caplog.records
        if "ESCALATED (" in r.getMessage()
    ]


async def test_incident_pin_unloads_before_first_load_when_ams_stuck_mid_change(
    db_session, printer_factory, install_settings, monkeypatch, caplog
):
    """THE INCIDENT PIN: PAUSE + tray_now 255 + ams_status_main 1 (filament_change)
    + a live 0700_8010 → an unload MUST be published before the first load, and its
    answer is MEASURED (``ams_command.classify``), never assumed.

    Here the firmware CONTINUE frees the change first, so the unload goes into an idle
    AMS and completes on the classifier's idle-and-empty row. What 009 measured about
    the unloads sent BEFORE its CONTINUE — four at ``tray_now == 255``, all silent — is
    that nothing loaded cannot move: undecidable, not "the firmware drops every unload"
    (see test_an_undecidable_unload_proceeds_to_the_load)."""
    install_settings()
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    await _bind_spool(db_session, printer.id, 0, 0)
    state = _make_state(tray_now=255, ams_status_main=1, trays=[_ams_tray(0), _ams_tray(1)])
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    with caplog.at_level(logging.INFO, logger="backend.app.services.spool_recovery"):
        task = await on_ams_fault(printer.id, state)
        await task

    motion = [c for c in client.calls if c[0] in ("unload", "load")]
    assert motion, "recovery sent no AMS motion at all"
    assert motion[0] == ("unload",), f"the first AMS command must be the unload, got {motion}"
    assert ("load", 1) in client.calls
    assert state.state == "RUNNING"  # self-healed, no human needed
    answers = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[ams-command] actor=driver")]
    assert any("command=unload" in line and "answer=complete" in line for line in answers)
    assert any("command=load" in line and "answer=complete" in line for line in answers)


async def test_every_candidate_round_unloads_again_after_a_failed_load(
    db_session, printer_factory, install_settings, monkeypatch
):
    """A `load_fail` round is followed by a REAL unload cycle in the next round —
    with the short-circuit gone, rounds 2..N are no longer unload-free.

    ONE unload per ATTEMPTED LOAD since 004-H2S 2026-09-17 (incident 192): the third
    round finds no candidate and abandons at selection, so it commits no unload. The
    claim this pins is unchanged — every load is preceded by its own unload cycle."""
    install_settings()
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    _spy(monkeypatch, "on_spool_recovery_failed")
    _spy(monkeypatch, "on_spool_out_of_rotation")
    state = _make_state(trays=[_ams_tray(0), _ams_tray(1), _ams_tray(2)])
    client = FakeClient(state, load_after=9999)  # no load ever confirms
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    await task

    # Two candidate rounds ran, each with its own unload; the third escalated at
    # selection with nothing to load, and therefore nothing to unload.
    assert client.calls.count(("unload",)) == 2
    assert ("load", 1) in client.calls and ("load", 2) in client.calls
    motion = [c for c in client.calls if c[0] in ("unload", "load")]
    assert motion.index(("unload",)) < motion.index(("load", 1))
    assert motion[motion.index(("load", 1)) :].count(("unload",)) == 1  # round 2 unloaded again


async def test_unload_confirms_only_after_the_ams_returns_to_idle(
    db_session, printer_factory, install_settings, monkeypatch
):
    """A filament-change cycle observed going non-idle confirms only on its return
    to idle — and NO load is published while the AMS is still busy.

    The round BEGINS with the AMS idle (``ams_status_main=0``) so the W1 stuck-change
    reset is a no-op (a wedged AMS at round-top is now the reset's domain, covered by
    its own tests); it is the UNLOAD itself (``unload_stuck``) that drives the AMS
    non-idle here, which is exactly what ``_confirm_unloaded`` must wait out."""
    install_settings(step_timeout_s=5.0)
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    state = _make_state(tray_now=255, ams_status_main=0, trays=[_ams_tray(0), _ams_tray(1)])
    client = FakeClient(state, unload_stuck=True)  # the unload leaves the AMS busy
    busy_polls = {"n": 0}

    def _on_poll(n, st):
        if st.ams_status_main != 0:
            busy_polls["n"] += 1
            assert not any(c[0] == "load" for c in client.calls), "loaded while the AMS was still busy"
        if n >= 5:
            st.ams_status_main = 0  # the change cycle completes

    _wire(monkeypatch, state, client, on_poll=_on_poll)

    task = await on_ams_fault(printer.id, state)
    await task

    assert busy_polls["n"] >= 1  # the busy window was actually observed
    assert ("unload",) in client.calls
    assert ("load", 1) in client.calls  # only after the AMS went idle again
    assert state.state == "RUNNING"


@pytest.mark.parametrize(
    "unload_knob,answer",
    [
        # The AMS moved (ams_status_sub stepped) and never completed: resent within
        # max_attempts, then given up.
        ("unload_sub_only", "acted"),
        # Nothing on the wire moved, outside a filament change: the ladder's second entry
        # has nothing to pull (``skipped``), the unload is resent ONCE, then given up.
        ("unload_deaf", "no_movement"),
    ],
)
async def test_an_unload_that_never_completed_never_leads_to_a_load(
    db_session, printer_factory, install_settings, monkeypatch, caplog, unload_knob, answer
):
    """An unload the wire answered ``acted`` or ``no_movement`` is NOT a completed
    unload: it is resent, then recovery escalates ``unload_failed`` — no load is ever
    published behind it (invariant 8), and the page names what the wire answered."""
    install_settings(max_attempts=2, step_timeout_s=0.05)
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    _spy(monkeypatch, "on_spool_out_of_rotation")
    # The feeder is engaged at an idle AMS (posture ``idle``): no filament change, so no
    # lever is ever pulled — the unload's own answer decides.
    state = _make_state(tray_now=0, ams_status_main=0, trays=[_ams_tray(0), _ams_tray(1)])
    client = FakeClient(state, **{unload_knob: True})
    _wire(monkeypatch, state, client)

    with caplog.at_level(logging.INFO, logger="backend.app.services.spool_recovery"):
        task = await on_ams_fault(printer.id, state)
        await task

    assert client.calls.count(("unload",)) == 2
    assert not any(c[0] == "load" for c in client.calls)  # never loaded behind an unfinished unload
    assert _escalated_reasons(caplog) == ["unload_failed"]
    lines = [r.getMessage() for r in caplog.records]
    assert sum(1 for m in lines if "command=unload" in m and f"answer={answer}" in m) == 2
    assert not any("lever=" in m for m in lines)
    assert "Sent: unload ×2 (" in failed.call_args.kwargs["detail"]
    assert state.state == "PAUSE"
    db_session.expunge_all()
    assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason == WAITING_REASON_FAILED


# ===========================================================================
# Unload settle dwell: with no observed filament-change cycle (command latency or
# a no-op unload), idle+empty must HOLD for ams_command.UNLOAD_GRACE_S before the
# classifier calls the unload complete. Driven on a fake clock — no test ever sleeps
# for real.
# ===========================================================================


def _incident(
    printer_id: int,
    *,
    step_timeout_s: float,
    max_attempts: int = 2,
    incident_id: int = 0,
    item_id: int | None = 1,
    extruder_side_only: bool = False,
    retract_failure: bool = False,
):
    """A driver context for the step-helper tests.

    ``incident_id=0`` names no durable row on purpose: these cases drive the unload /
    load / resume helpers, and the incident-store calls those helpers' terminal steps
    make are no-ops for a missing row (``close``/``mark_escalated`` return None). The
    lifecycle itself is pinned through the real entry point elsewhere.
    """
    return spool_recovery.RecoveryIncident(
        incident_id=incident_id,
        printer_id=printer_id,
        job_id="task-1",
        codes=frozenset({"0700_8010"}),
        fingerprint="mechanical_feed:0700_8010",
        item_id=item_id,
        settings=spool_recovery.RecoverySettings(
            enabled=True, max_attempts=max_attempts, step_timeout_s=step_timeout_s, protect_layers=7
        ),
        jammed_global_tray=0,
        kind=spool_recovery.KIND_JAM,
        external=False,
        extruder_side_only=extruder_side_only,
        retract_failure=retract_failure,
        layer_at_fault=50,
        code="0700_8010",
        printer_name="009-H2S",
        job_name="SKU007",
    )


def _unload_record(answer="complete", posture="idle"):
    """One unload the driver's evidence records as having gone out."""
    return spool_recovery.CommandRecord(command="unload", target=None, posture=posture, answer=answer, at=0.0)


async def _ladder(incident, client, *, evidence=None, entry="round_start"):
    """Drive ONE ladder entry with a driver's evidence (a fresh driver when omitted)."""
    return await spool_recovery._reset_stuck_change(
        incident,
        client,
        evidence=evidence if evidence is not None else spool_recovery._RecoveryEvidence(),
        entry=entry,
    )


class TestUnloadGraceDwell:
    """`_confirm_unloaded` reads the unload through ``ams_command.classify``, whose
    no-cycle-observed row needs idle + nothing fed HELD for ``UNLOAD_GRACE_S`` (the
    operator's proven manual recovery left 16 s between the unload and the load that
    worked). The row itself is pinned in test_ams_command; these pin that the DRIVER's
    loop honours it — elapsed measured from the entry snapshot on ``ams_command``'s
    clock, the step timeout as the deadline."""

    @pytest.fixture
    def clock(self, monkeypatch):
        # Advanced only by the module's poll sleep. No loop yield inside the sleep: the
        # stand-in IS ``asyncio.sleep`` here, so yielding through it would recurse.
        c = FakeClock(start=0.0, step=2.0, sleep_mode="step", yield_on_sleep=False)
        monkeypatch.setattr(ams_command, "_monotonic", c)
        monkeypatch.setattr(spool_recovery.asyncio, "sleep", c.sleep)
        monkeypatch.setattr(ams_command, "UNLOAD_GRACE_S", 15.0)  # the production value
        return c

    async def test_complete_only_after_the_grace_dwell_elapsed(self, clock, monkeypatch):
        state = _make_state(tray_now=255, ams_status_main=0)
        _wire(monkeypatch, state, FakeClient(state))

        verdict = await spool_recovery._confirm_unloaded(_incident(1, step_timeout_s=60.0), ams_command.snapshot(state))

        assert verdict == "complete"
        assert clock.t >= ams_command.UNLOAD_GRACE_S  # never confirmed early

    async def test_no_movement_when_the_dwell_cannot_fit_in_the_step_timeout(self, clock, monkeypatch):
        state = _make_state(tray_now=255, ams_status_main=0)
        _wire(monkeypatch, state, FakeClient(state))

        verdict = await spool_recovery._confirm_unloaded(_incident(1, step_timeout_s=10.0), ams_command.snapshot(state))

        assert verdict == "no_movement"  # idle+empty held, but not long enough — and nothing moved
        assert clock.t < ams_command.UNLOAD_GRACE_S

    async def test_dwell_restarts_when_the_ams_goes_busy_again(self, clock, monkeypatch):
        """Any contrary poll restarts the dwell — and once a cycle IS observed, the
        return to idle confirms immediately (no second dwell)."""
        state = _make_state(tray_now=255, ams_status_main=0)

        def _on_poll(n, st):
            if n == 3:
                st.ams_status_main = 1  # a change cycle starts late
            if n >= 5:
                st.ams_status_main = 0

        _wire(monkeypatch, state, FakeClient(state), on_poll=_on_poll)

        verdict = await spool_recovery._confirm_unloaded(_incident(1, step_timeout_s=60.0), ams_command.snapshot(state))

        assert verdict == "complete"
        assert clock.t < ams_command.UNLOAD_GRACE_S  # confirmed by the cycle, not the dwell


# ===========================================================================
# Honest escalation reasons: chosen by what the loop actually achieved, never by
# position in the code. The 009 incident reported `no_eligible_spool` after four
# failed loads — that reason is now narrowed to a genuinely empty candidate set.
# ===========================================================================


async def test_zero_loads_attempted_escalates_no_eligible_spool(
    db_session, printer_factory, install_settings, monkeypatch, caplog
):
    install_settings()
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    _spy(monkeypatch, "on_spool_recovery_failed")
    oor = _spy(monkeypatch, "on_spool_out_of_rotation")
    jammed = await _bind_spool(db_session, printer.id, 0, 0)
    state = _make_state(trays=[_ams_tray(0)])  # only the jammed tray is loaded
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    with caplog.at_level(logging.WARNING, logger="backend.app.services.spool_recovery"):
        task = await on_ams_fault(printer.id, state)
        await task

    assert not any(c[0] == "load" for c in client.calls)
    assert _escalated_reasons(caplog) == ["no_eligible_spool"]
    # Nothing to load ⇒ nothing was committed: no unload and no out-of-rotation stamp
    # (004-H2S 2026-09-17 — the swap used to commit both before it asked).
    assert ("unload",) not in client.calls
    oor.assert_not_awaited()
    db_session.expunge_all()
    assert (await db_session.get(Spool, jammed.id)).feed_fault_at is None


async def test_loads_failed_without_a_confirmed_unload_escalates_candidate_loads_failed(
    db_session, printer_factory, install_settings, monkeypatch, caplog
):
    """Clean restart state (unload legitimately skipped every round) but no
    replacement would load → the candidate set, not the feed path, is the story."""
    install_settings()
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    _spy(monkeypatch, "on_spool_recovery_failed")
    # Clean state with NO code standing (see test_restart_clean_state_skips_unload):
    # unreachable through the wire-derived entry gate, so driven through the driver.
    state = _make_state(tray_now=255, ams_status_main=0, hms=[], trays=[_ams_tray(0), _ams_tray(1)])
    client = FakeClient(state, load_after=9999)
    _wire(monkeypatch, state, client)

    incident = await _owned_incident(db_session, printer.id, step_timeout_s=0.05, item_id=item.id)
    with caplog.at_level(logging.WARNING, logger="backend.app.services.spool_recovery"):
        await spool_recovery._run_recovery(incident)

    assert ("unload",) not in client.calls  # genuinely clean state → skipped
    assert ("load", 1) in client.calls
    assert _escalated_reasons(caplog) == ["candidate_loads_failed"]


async def test_confirmed_unloads_with_every_load_failing_escalates_feed_path_blocked(
    db_session, printer_factory, install_settings, monkeypatch, caplog
):
    """The AMS unloaded cleanly every round and still nothing would feed — the
    blockage is downstream of the spool (buffer / PTFE), so say so.

    One unload per attempted load (004-H2S 2026-09-17): the third round finds no
    candidate and abandons at selection without unloading, and the give-up then tries to
    put the jammed spool BACK. The AMS does not move on that reload either — so the page
    states the left-behind state and the wire's answer instead of implying it, and the
    reason is unchanged (the restore is not a candidate and is not counted as evidence)."""
    install_settings()
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    _spy(monkeypatch, "on_spool_out_of_rotation")
    state = _make_state(trays=[_ams_tray(0), _ams_tray(1), _ams_tray(2)])
    client = FakeClient(state, load_after=9999)
    _wire(monkeypatch, state, client)

    with caplog.at_level(logging.WARNING, logger="backend.app.services.spool_recovery"):
        task = await on_ams_fault(printer.id, state)
        await task

    assert client.calls.count(("unload",)) == 2  # one per attempted load
    assert _escalated_reasons(caplog) == ["feed_path_blocked"]
    assert ("load", 0) in client.calls  # the restore tried to reload the jammed tray
    assert state.tray_now == 255  # ...and it did not take: nothing is loaded
    assert (
        "No filament is loaded: AMS A slot 1 was unloaded; the reload was sent and the AMS did not move."
        in failed.call_args.kwargs["detail"]
    )


async def test_drying_refusal_escalates_ams_drying_without_burning_attempts(
    db_session, printer_factory, install_settings, monkeypatch, caplog
):
    install_settings()
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    _spy(monkeypatch, "on_spool_recovery_failed")
    state = _make_state(trays=[_ams_tray(0), _ams_tray(1)])
    client = FakeClient(state, write_refusal="drying")
    _wire(monkeypatch, state, client)

    with caplog.at_level(logging.WARNING, logger="backend.app.services.spool_recovery"):
        task = await on_ams_fault(printer.id, state)
        await task

    assert ("unload",) not in client.calls  # a doomed lane is never written to
    assert ("settle",) not in client.calls  # drying is not waited out
    assert _escalated_reasons(caplog) == ["ams_drying"]
    assert state.state == "PAUSE"
    db_session.expunge_all()
    assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason == WAITING_REASON_FAILED


async def test_identify_refusal_is_absorbed_by_the_settle_wait(
    db_session, printer_factory, install_settings, monkeypatch
):
    """Identify contention is transient — the client's settle wait absorbs it and
    the recovery proceeds, instead of escalating to a human."""
    install_settings()
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    state = _make_state(trays=[_ams_tray(0), _ams_tray(1)])
    client = FakeClient(state, write_refusal="identify_in_flight", refusal_clears_on_settle=True)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    await task

    assert ("settle",) in client.calls
    assert ("unload",) in client.calls
    assert ("load", 1) in client.calls
    assert state.state == "RUNNING"


def test_every_escalation_reason_has_operator_facing_copy():
    """No reason token may reach a notification without human-facing detail."""
    for reason in ("no_eligible_spool", "candidate_loads_failed", "feed_path_blocked", "ams_drying"):
        assert reason in spool_recovery._ESCALATE_DETAIL
        assert spool_recovery._ESCALATE_DETAIL[reason].endswith("Left PAUSED for a human.")
    # The three reasons a jam reaches after the farm SENT its CONTINUEs and swap
    # commands (012-H2S 2026-09-22, incident 304). The copy never instructs the action
    # the driver just proved ineffective — no bare "press Retry/Continue" — never
    # restates the retracted premise ("ams_status_main=1: the firmware drops every
    # load and unload"), and ends with the PHYSICAL exit first. What was sent and what
    # the wire answered is the evidence sentence's job (_compose_detail), not the copy's.
    for reason in ("unload_failed", "stuck_reset_failed", "swap_dropped_wedged"):
        assert reason in spool_recovery._ESCALATE_DETAIL
        detail = spool_recovery._ESCALATE_DETAIL[reason]
        assert "Retry" not in detail
        assert "ams_status_main" not in detail
        assert "drops every" not in detail
        assert detail.endswith("Open the AMS and free the filament at the feeder, then press Continue on the printer.")


# ===========================================================================
# Jam attribution: the 8010 family carries NO slot in its attr (hms_errors fails
# closed there), so the jammed tray comes from live telemetry.
# ===========================================================================


async def test_jam_attributed_to_live_tray_when_attr_carries_no_slot(
    db_session, printer_factory, install_settings, monkeypatch
):
    """attr 0x07008210 + code 0x8010 names no slot → attribution falls back to the
    live feeding tray (tray_now = 1), NOT the stale single-feeder mapping ([0])."""
    install_settings()
    printer = await printer_factory()
    await _farm_item(db_session, printer.id, ams_mapping="[0, -1, -1, -1]")
    on_tray0 = await _bind_spool(db_session, printer.id, 0, 0)
    on_tray1 = await _bind_spool(db_session, printer.id, 0, 1)
    _spy(monkeypatch, "on_spool_out_of_rotation")
    jam = HMSError(code="8010", attr=0x07008210, module=7, severity=2)
    state = _make_state(tray_now=1, hms=[jam], trays=[_ams_tray(0), _ams_tray(1)])
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    await task

    db_session.expunge_all()
    assert (await db_session.get(Spool, on_tray1.id)).feed_fault_at is not None  # global tray 1 blamed
    assert (await db_session.get(Spool, on_tray0.id)).feed_fault_at is None


# ===========================================================================
# W1: the wedge lever ladder (009-H2S 2026-07-20).
#
# After a feed fault the AMS can sit mid filament-change (PAUSE + ams_status_main 1).
# On 009 a resume (the touchscreen CONTINUE) freed it; the unloads sent before it went
# out with nothing loaded, so their silence was undecidable — not proof the firmware
# ignores an unload there. Every candidate round runs _reset_stuck_change FIRST, and
# whatever the ladder reads, the swap commands then go out and are measured.
# ===========================================================================


class _SelfHealClient(FakeClient):
    """The reset resume fully self-heals: RUNNING, fault cleared, AMS idle, and the
    pending change completed onto the jammed feeder (tray_now == 0). No swap needed."""

    def resume_print(self):
        self.calls.append(("resume",))
        self._resume += 1
        self.state.state = "RUNNING"
        self.state.hms_errors = []  # fault cleared by the firmware
        self.state.ams_status_main = 0  # change machine returned to idle
        self.state.tray_now = 0  # the firmware finished loading the jammed slot
        return True


class _WedgedClient(FakeClient):
    """The printer never moves on the resume: the send is accepted but the state
    machine stays PAUSE at the same ``ams_status`` — outcome (d), ``never_moved``."""

    def resume_print(self):
        self.calls.append(("resume",))
        self._resume += 1
        return True  # no state change


async def test_incident_pin_resume_first_then_hung_self_pause_then_swap(
    db_session, printer_factory, install_settings, monkeypatch
):
    """THE W1 LIVE-INCIDENT PIN (009-H2S 2026-07-20): PAUSE + tray_now 255 +
    ams_status_main 1 + a standing 0700_8010. The FIRST published command is the
    reset RESUME (before any unload). The change stays hung RUNNING, so recovery
    self-PAUSEs at the reset deadline, then the normal unload → select → load →
    resume round runs and _succeed fires when the swap confirms. Zero human touch.

    The base FakeClient IS the hung case: resume takes the printer RUNNING but
    leaves ams_status_main / the fault / tray_now unchanged until we re-pause it."""
    install_settings()
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    jammed = await _bind_spool(db_session, printer.id, 0, 0)  # jammed tray0
    state = _make_state(tray_now=255, ams_status_main=1, trays=[_ams_tray(0), _ams_tray(1)])
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    assert task is not None
    await task

    published = [c for c in client.calls if c[0] in ("resume", "pause", "unload", "load")]
    assert published[0] == ("resume",), f"the first published command must be the reset resume, got {published}"
    r_idx = published.index(("resume",))
    u_idx = published.index(("unload",))
    assert r_idx < u_idx  # the reset resume precedes the first unload
    assert ("pause",) in client.calls  # self-paused the hung change at the reset deadline
    assert ("load", 1) in client.calls  # the swap round ran after the self-pause
    assert state.state == "RUNNING"  # self-healed via the swap, no human

    db_session.expunge_all()
    refreshed = await db_session.get(PrintQueueItem, item.id)
    assert refreshed.waiting_reason is None
    assert json.loads(refreshed.ams_mapping) == [1, -1, -1, -1]  # swapped 0 → 1
    assert (await db_session.get(Spool, jammed.id)).feed_fault_at is not None  # jammed left OOR (a real swap)


async def test_reset_outcome_auto_refault_returns_ok_no_self_pause(monkeypatch):
    """Reset (a): the firmware moves (RUNNING) then re-faults and auto-PAUSEs on its
    own → 'ok', and recovery does NOT publish a self-pause."""
    state = _make_state(tray_now=255, ams_status_main=1)

    def _on_poll(n, st):
        # After the loop has OBSERVED the resume take the printer RUNNING (poll 2),
        # the firmware re-faults back to PAUSE on its own.
        if n >= 3:
            st.state = "PAUSE"

    client = FakeClient(state)
    _wire(monkeypatch, state, client, on_poll=_on_poll)

    verdict = await _ladder(_incident(7, step_timeout_s=1.0), client)

    assert verdict == "ok"
    assert ("resume",) in client.calls  # the reset resume was published
    assert ("pause",) not in client.calls  # (a) never self-pauses


async def test_reset_recovered_self_heals_without_swap(db_session, printer_factory, install_settings, monkeypatch):
    """Reset (b): the firmware reset fully self-heals (fault clears, RUNNING stable,
    the change completed on the jammed feeder). Recovery ends success with NO swap and
    NO out-of-rotation: the swap-commit boundary is never reached, so the jammed spool
    is never stamped (``feed_fault_at`` stays None throughout) and the swap-framed
    alert is never sent — the dedicated self-heal notification fires exactly once, the
    per-job flap counter increments, and no unload/load is ever sent."""
    install_settings(step_timeout_s=1.0)
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    jammed = await _bind_spool(db_session, printer.id, 0, 0)  # jammed tray0 — never OOR-stamped on a self-heal
    succeeded = _spy(monkeypatch, "on_spool_recovery_succeeded")
    oor = _spy(monkeypatch, "on_spool_out_of_rotation")
    self_healed = _spy(monkeypatch, "on_spool_recovery_self_healed")
    state = _make_state(tray_now=0, ams_status_main=1, trays=[_ams_tray(0), _ams_tray(1)])
    client = _SelfHealClient(state)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    assert task is not None
    await task

    assert not any(c[0] in ("unload", "load") for c in client.calls)  # NO swap performed
    assert client.calls.count(("resume",)) == 1  # only the reset resume
    assert state.state == "RUNNING"
    from backend.app.services import printer_incidents

    # Counted toward the flap cap — the durable ledger IS the counter now.
    assert await printer_incidents.count_resolved(db_session, printer.id, "task-1", "jam") == 1
    succeeded.assert_not_awaited()  # a no-swap self-heal never sends the swap-framed alert
    oor.assert_not_awaited()  # nothing taken out of rotation — the commit boundary was never reached

    # The dedicated self-heal notification fires exactly once, carrying the incident.
    self_healed.assert_awaited_once()
    kwargs = self_healed.call_args.kwargs
    assert kwargs["printer_id"] == printer.id
    assert kwargs["job_name"] == "SKU007"  # incident.job_name
    assert kwargs["layer"] == 50  # incident.layer_at_fault
    assert kwargs["code"] == "0700_8010"
    # ONE origin for the slot name (``runout_slot_desc``): global tray 0 is the slot the
    # firmware, the chip and the out-of-rotation page all call "AMS A slot 1". The
    # 0-indexed "AMS0 slot 0" this asserted was the third of three renderings
    # (004-H2S 2026-09-17, incident 192).
    assert kwargs["slot_desc"] == "AMS A slot 1"
    assert kwargs["spool_desc"] == "Bambu PETG Green"  # _spool_label of the jammed spool

    db_session.expunge_all()
    unstamped = await db_session.get(Spool, jammed.id)
    assert unstamped.feed_fault_at is None  # never stamped (no commit boundary on a self-heal)
    assert unstamped.feed_fault_code is None
    refreshed = await db_session.get(PrintQueueItem, item.id)
    assert refreshed.waiting_reason is None
    assert json.loads(refreshed.ams_mapping) == [0, -1, -1, -1]  # mapping unchanged (no swap)


async def test_reset_never_moves_escalates_stuck_reset_failed(
    db_session, printer_factory, install_settings, monkeypatch, caplog
):
    """Outcome (d): the printer never moves on EITHER CONTINUE (state never leaves
    PAUSE at the same ams_status) → ``fail`` → ``stuck_reset_failed``, never touching the
    unload. ``fail`` is narrow: a CONTINUE that could not be sent or never moved the
    printer — a still-wedged AMS after a CONTINUE that DID move it is ``wedged`` and the
    swap round runs. The jammed spool IS taken out of rotation at this commit boundary,
    and the page carries what was sent and what the wire answered."""
    install_settings(step_timeout_s=0.05)
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    jammed = await _bind_spool(db_session, printer.id, 0, 0)  # jammed tray0
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    oor = _spy(monkeypatch, "on_spool_out_of_rotation")
    state = _make_state(tray_now=255, ams_status_main=1, trays=[_ams_tray(0), _ams_tray(1)])
    client = _WedgedClient(state)
    _wire(monkeypatch, state, client)

    with caplog.at_level(logging.WARNING, logger="backend.app.services.spool_recovery"):
        task = await on_ams_fault(printer.id, state)
        await task

    assert ("resume",) in client.calls  # tier 1 was attempted
    assert ("ams_control", "resume") in client.calls  # ...and tier 2: the AMS was still in the change
    assert ("unload",) not in client.calls  # escalated before the unload
    assert _escalated_reasons(caplog) == ["stuck_reset_failed"]
    detail = failed.call_args.kwargs["detail"]
    assert detail.startswith(spool_recovery._ESCALATE_DETAIL["stuck_reset_failed"])
    assert "Sent: resume (printer did not move); ams_control resume (printer did not move)." in detail
    oor.assert_awaited_once()  # the wedged feeder's spool taken out of rotation at the commit boundary
    assert state.state == "PAUSE"  # never resumed blind
    db_session.expunge_all()
    assert (await db_session.get(Spool, jammed.id)).feed_fault_at is not None  # stamped before escalating
    assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason == WAITING_REASON_FAILED


async def test_reset_skipped_when_ams_idle(monkeypatch):
    """An idle AMS at round-top: the reset is a no-op ('skipped') that publishes
    nothing — the pre-W1 flow is byte-identical."""
    state = _make_state(ams_status_main=0)  # idle
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    verdict = await _ladder(_incident(7, step_timeout_s=1.0), client)

    assert verdict == "skipped"
    assert client.calls == []  # nothing published on an idle AMS


async def test_levers_are_records_per_driver_not_per_job(monkeypatch):
    """Each lever is pulled at most once per DRIVER, and "spent" is a LeverRecord in
    that driver's evidence — never a counter keyed by the job.

    A second wedged round of the SAME driver publishes neither CONTINUE again and reads
    ``wedged`` (the swap commands then go out and are measured) — not the retracted
    "budget spent, the firmware drops every command" ``fail``. A NEW incident on the
    same job is a new driver with fresh levers: 003-H2S 2026-09-19 04:31 met a second
    fault 69 s after a self-heal and gave up with ZERO CONTINUEs sent ("reset budget
    spent (1/1)") because the counter was keyed (printer, job)."""
    state = _make_state(tray_now=255, ams_status_main=1)
    client = FakeClient(state, resume_unwedges=False)  # moves the printer; the AMS stays in the change
    _wire(monkeypatch, state, client)
    incident = _incident(7, step_timeout_s=0.02)
    first_driver = spool_recovery._RecoveryEvidence()

    v1 = await _ladder(incident, client, evidence=first_driver)
    assert v1 == "wedged"  # tier 1 moved it and it re-paused still wedged; tier 2 did not move it
    assert [(r.lever, r.outcome) for r in first_driver.levers] == [
        ("resume", "wedged"),
        ("ams_control_resume", "never_moved"),
    ]
    assert client.calls.count(("resume",)) == 1
    assert client.calls.count(("ams_control", "resume")) == 1

    v2 = await _ladder(incident, client, evidence=first_driver)
    assert v2 == "wedged"  # every lever this entry may pull is spent: still wedged, never a give-up
    assert client.calls.count(("resume",)) == 1  # NO second pull of either lever
    assert client.calls.count(("ams_control", "resume")) == 1
    assert len(first_driver.levers) == 2

    second_driver = spool_recovery._RecoveryEvidence()  # a new incident on the same job
    v3 = await _ladder(replace(incident, incident_id=8), client, evidence=second_driver)
    assert v3 == "wedged"
    assert client.calls.count(("resume",)) == 2  # its own tier 1
    assert client.calls.count(("ams_control", "resume")) == 2  # ...and its own tier 2


async def test_incident_pin_engaged_feeder_assist_fault_skips_reset_and_swaps(
    db_session, printer_factory, install_settings, monkeypatch
):
    """THE 006-H2S INCIDENT PIN (2026-07-21 04:14): an extruder-side 0300_801E feed
    fault mid-print with the feeder still ENGAGED — gcode_state PAUSE, tray_now 3,
    ams_status_main 3 (assist). There is no interrupted filament-change for a resume
    to continue, so the W1 reset MUST be skipped: the first command on the wire is the
    unload (not a resume), and the proven unload → load → resume swap machine runs —
    exactly the sequence the operator used to recover by hand. Zero human touch.

    (Regression pin for the pre-fix bug where ams_status_main != 0 unconditionally
    entered the reset, which predictably failed and escalated stuck_reset_failed
    without ever trying the swap the AMS would have accepted.)"""
    install_settings()
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id, ams_mapping="[3, -1, -1, -1]")
    jammed = await _bind_spool(db_session, printer.id, 0, 3)  # jammed feeder = global tray 3
    succeeded = _spy(monkeypatch, "on_spool_recovery_succeeded")
    # tray_now 3 (engaged) + assist(3); replacement tray1 is the only other loaded tray.
    state = _make_state(tray_now=3, ams_status_main=3, trays=[_ams_tray(1), _ams_tray(3)], hms=[_extruder_hms()])
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    assert task is not None
    await task

    published = [c for c in client.calls if c[0] in ("resume", "pause", "unload", "load")]
    assert published, "recovery published no AMS commands at all"
    assert published[0] == ("unload",), f"reset skipped → the first wire command must be the unload, got {published}"
    assert published.index(("unload",)) < published.index(("resume",))  # unload precedes the swap's resume
    assert client.calls.count(("resume",)) == 1  # only the swap resume — no reset resume was ever published
    assert ("pause",) not in client.calls  # no stuck-change self-pause on an assist fault
    assert ("load", 1) in client.calls  # swapped onto the replacement tray
    assert state.state == "RUNNING"  # self-healed via the swap
    succeeded.assert_awaited_once()  # closed as a swapped success

    db_session.expunge_all()
    refreshed = await db_session.get(PrintQueueItem, item.id)
    assert refreshed.waiting_reason is None
    assert json.loads(refreshed.ams_mapping) == [1, -1, -1, -1]  # jammed 3 → replacement 1
    jammed_after = await db_session.get(Spool, jammed.id)
    # The swap-commit boundary was crossed, but an extruder-side fault parks no spool
    # (006-H2S 2026-09-21, incident 289) — the extruder is the common factor here too.
    assert jammed_after.feed_fault_at is None
    assert jammed_after.feed_fault_code is None


@pytest.mark.parametrize("ams_main", [2, 3, 4])
async def test_reset_skipped_for_non_filament_change_states(ams_main, monkeypatch):
    """Only ams_status_main == 1 (filament_change) is resume-resettable (009 evidence).
    assist(3), identifying(2) and calibration(4) are NOT stuck changes: the ladder is a
    no-op ('skipped') that publishes NO resume and records NO lever, so the
    unload→swap machine owns the round (006-H2S 2026-07-21)."""
    state = _make_state(tray_now=3, ams_status_main=ams_main)
    client = FakeClient(state)
    _wire(monkeypatch, state, client)
    evidence = spool_recovery._RecoveryEvidence()

    verdict = await _ladder(_incident(7, step_timeout_s=1.0), client, evidence=evidence)

    assert verdict == "skipped"
    assert client.calls == []  # nothing published on a non-filament-change AMS
    assert evidence.levers == []  # no lever spent


async def test_confirm_unloaded_ok_after_engaged_assist_returns_to_idle(monkeypatch):
    """`_confirm_unloaded` path (a) for the 006-H2S engaged-feeder case: the AMS starts
    non-idle at ams_status_main 3 with tray_now 3 (filament still engaged); the change
    cycle is observed running and completion is its return to idle with nothing feeding
    (the operator's unload settled tray_now 3 → 255 in seconds)."""
    state = _make_state(tray_now=3, ams_status_main=3)

    def _on_poll(n, st):
        if n >= 3:  # the commanded unload's change cycle completes
            st.ams_status_main = 0
            st.tray_now = 255

    client = FakeClient(state)
    _wire(monkeypatch, state, client, on_poll=_on_poll)

    verdict = await spool_recovery._confirm_unloaded(_incident(7, step_timeout_s=1.0), ams_command.snapshot(state))

    assert verdict == "complete"


# ===========================================================================
# W2: durable repeat-jam quarantine off the recovery_escalation ledger.
# ===========================================================================


async def test_two_escalations_within_window_quarantines(db_session, printer_factory, install_settings, monkeypatch):
    """Two GENUINE recovery escalations for one printer within _JAM_QUARANTINE_WINDOW_H
    hours quarantine it, with failure_count == the in-window escalation count. The
    first escalation (count 1) is under the threshold and does not.

    "Genuine" is now structural: each escalation owns its own incident row, and the
    row is closed between them the way a lifecycle event closes it. Two escalations
    against ONE still-open row is the 006-H2S duplicate, and F3 stands it down."""
    from backend.app.services import farm_policy

    install_settings()
    printer = await printer_factory()
    _spy(monkeypatch, "on_spool_recovery_failed")
    q = AsyncMock(return_value=True)
    monkeypatch.setattr(farm_policy, "quarantine_printer", q)

    await _genuine_escalation(db_session, printer.id, "unload_failed")
    q.assert_not_called()  # one escalation is under the threshold

    await _genuine_escalation(db_session, printer.id, "stuck_reset_failed")
    q.assert_awaited_once()
    assert q.await_args.kwargs["failure_count"] == 2
    assert "Repeated AMS jam" in q.await_args.args[2]  # positional reason text

    db_session.expunge_all()
    rows = (
        (await db_session.execute(select(RecoveryEscalation).where(RecoveryEscalation.printer_id == printer.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 2  # both escalations durably recorded


async def test_two_escalations_outside_window_no_quarantine(db_session, printer_factory, install_settings, monkeypatch):
    """Escalations spread beyond the window do not accumulate: an old (25 h) row
    plus a fresh one leaves only ONE in-window → no quarantine."""
    from datetime import timedelta

    from backend.app.services import farm_policy

    install_settings()
    printer = await printer_factory()
    _spy(monkeypatch, "on_spool_recovery_failed")
    q = AsyncMock(return_value=True)
    monkeypatch.setattr(farm_policy, "quarantine_printer", q)

    db_session.add(
        RecoveryEscalation(
            printer_id=printer.id,
            created_at=datetime.utcnow() - timedelta(hours=25),  # outside the 24 h window
            reason="unload_failed",
            code="0700_8010",
        )
    )
    await db_session.commit()

    await _genuine_escalation(db_session, printer.id, "stuck_reset_failed")

    q.assert_not_called()  # only one escalation is inside the window
    db_session.expunge_all()
    rows = (
        (await db_session.execute(select(RecoveryEscalation).where(RecoveryEscalation.printer_id == printer.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 2  # both persisted, but only one is in-window


class TestQuarantineReasonAllowlist:
    """W2 counting is an ALLOWLIST (003-H2S 2026-08-11).

    The quarantine prints a DIAGNOSIS — "Repeated AMS jam escalations (N in 24h) —
    AMS hardware suspected (buffer/feeder)" — so only the jam machine's own
    hardware-suspect outcomes may be counted toward it. The incident: a 05:49
    filament RUNOUT and a 21:45 EXTERNAL-spool fault reached 2-in-24h on a printer
    whose AMS took no part in either, and the farm took it out of production.
    """

    @pytest.fixture
    def quarantine(self, monkeypatch):
        from backend.app.services import farm_policy

        q = AsyncMock(return_value=True)
        monkeypatch.setattr(farm_policy, "quarantine_printer", q)
        return q

    async def test_two_jam_reasons_still_quarantine(
        self, db_session, printer_factory, install_settings, monkeypatch, quarantine
    ):
        """The behaviour the allowlist must PRESERVE — the liveness half of the fix."""
        install_settings()
        printer = await printer_factory()
        _spy(monkeypatch, "on_spool_recovery_failed")
        await _genuine_escalation(db_session, printer.id, "jammed_tray_unresolved")
        quarantine.assert_not_called()
        await _genuine_escalation(db_session, printer.id, "jammed_tray_unresolved")

        quarantine.assert_awaited_once()
        assert quarantine.await_args.kwargs["failure_count"] == 2

    async def test_a_runout_plus_a_jam_does_not_quarantine(
        self, db_session, printer_factory, install_settings, monkeypatch, quarantine
    ):
        """THE 003-H2S false positive, exactly: the morning runout must not be
        evidence that the AMS hardware is failing."""
        install_settings()
        printer = await printer_factory()
        _spy(monkeypatch, "on_spool_recovery_failed")
        await _genuine_escalation(db_session, printer.id, "runout_needs_refill")
        await _genuine_escalation(db_session, printer.id, "jammed_tray_unresolved")

        quarantine.assert_not_called()  # ONE countable escalation, not two

    async def test_two_external_feed_faults_do_not_quarantine(
        self, db_session, printer_factory, install_settings, monkeypatch, quarantine
    ):
        """No AMS took part in either — twice."""
        install_settings()
        printer = await printer_factory()
        _spy(monkeypatch, "on_spool_recovery_failed")
        await _genuine_escalation(db_session, printer.id, "external_feed_fault")
        await _genuine_escalation(db_session, printer.id, "external_feed_fault")

        quarantine.assert_not_called()

    async def test_a_non_counting_reason_cannot_tip_an_earlier_jam_over(
        self, db_session, printer_factory, install_settings, monkeypatch, quarantine
    ):
        """The reverse order of the incident: a countable row already sits in the
        window and the NEW escalation is a runout. The trigger reads the current
        reason too, so nothing fires — the runout is not the second jam."""
        install_settings()
        printer = await printer_factory()
        _spy(monkeypatch, "on_spool_recovery_failed")
        await _genuine_escalation(db_session, printer.id, "unload_failed")
        await _genuine_escalation(db_session, printer.id, "runout_needs_refill")

        quarantine.assert_not_called()

    async def test_every_escalation_still_records_its_row(
        self, db_session, printer_factory, install_settings, monkeypatch, quarantine
    ):
        """The ledger stays a COMPLETE forensic record — only the COUNT is filtered."""
        install_settings()
        printer = await printer_factory()
        _spy(monkeypatch, "on_spool_recovery_failed")
        for reason in ("runout_needs_refill", "external_feed_fault", "ams_drying", "recovery_interrupted"):
            await _genuine_escalation(db_session, printer.id, reason)

        db_session.expunge_all()
        rows = (
            (await db_session.execute(select(RecoveryEscalation).where(RecoveryEscalation.printer_id == printer.id)))
            .scalars()
            .all()
        )
        assert sorted(r.reason for r in rows) == [
            "ams_drying",
            "external_feed_fault",
            "recovery_interrupted",
            "runout_needs_refill",
        ]
        quarantine.assert_not_called()

    def test_the_partition_mirrors_the_reason_vocabulary(self):
        """Every reason token is classified EXACTLY once, and the two halves cover
        the whole vocabulary. A future token cannot silently rejoin the count (or
        silently leave it) — adding one to ``_ESCALATE_DETAIL`` without deciding
        which half it belongs to fails here."""
        counting = spool_recovery._JAM_QUARANTINE_REASONS
        never = spool_recovery._NON_QUARANTINE_REASONS

        assert set(spool_recovery._ESCALATE_DETAIL) == counting | never
        assert not (counting & never)

    def test_the_wording_is_true_of_every_counted_reason(self):
        """The quarantine asserts "AMS hardware suspected (buffer/feeder)". Each
        counted token must be an outcome of the jam machine acting on an AMS."""
        assert set(spool_recovery._JAM_QUARANTINE_REASONS) == {
            "jammed_tray_unresolved",
            "feed_path_blocked",
            "unload_failed",
            "stuck_reset_failed",
            "swap_dropped_wedged",
            "repeated_jams",
            "candidates_exhausted",
            "candidate_loads_failed",
        }


async def test_abort_records_no_escalation_row(db_session, printer_factory, install_settings, monkeypatch):
    """_abort (operator takeover) must NOT write a recovery_escalation row — a
    takeover is not a give-up and must never count toward quarantine."""
    install_settings()
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    state = _make_state(gcode_state="RUNNING")  # an external actor already resumed
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    await spool_recovery._abort(_incident(printer.id, step_timeout_s=0.05))

    db_session.expunge_all()
    rows = (
        (await db_session.execute(select(RecoveryEscalation).where(RecoveryEscalation.printer_id == printer.id)))
        .scalars()
        .all()
    )
    assert rows == []  # abort never records an escalation


# ===========================================================================
# Truth-ordered out-of-rotation (2026-07-20): stamping/notification is bound to the
# SWAP-COMMIT boundary (right before the first unload), NOT to entry — so a no-swap
# firmware self-heal never stamps or announces a spool the print keeps using, and a
# post-commit escalation correctly KEEPS the stamp.
# ===========================================================================


async def test_oor_stamped_once_at_swap_commit(db_session, printer_factory, install_settings, monkeypatch):
    """The jammed spool is taken out of rotation exactly ONCE, at the swap-commit
    boundary (right before the first unload) — never at entry and never re-stamped on
    a later candidate round. A first-round load that never confirms forces a second
    round; the commit-stamp guard keeps the jammed-spool OOR notify at a single call."""
    install_settings(max_attempts=2, step_timeout_s=0.05)
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    jammed = await _bind_spool(db_session, printer.id, 0, 0)  # jammed tray0
    state = _make_state(trays=[_ams_tray(0), _ams_tray(1), _ams_tray(2)])
    # load_after=3 with max_attempts=2 → round 1's two load sends never confirm (advance
    # to round 2), round 2's third send confirms and the swap resumes.
    client = FakeClient(state, load_after=3)
    oor = _spy(monkeypatch, "on_spool_out_of_rotation")
    unload_seen_at_oor: list[bool] = []

    async def _record(*_a, **_k):
        unload_seen_at_oor.append(any(c[0] == "unload" for c in client.calls))

    oor.side_effect = _record
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    await task

    assert state.state == "RUNNING"  # the swap landed after the extra round
    assert client.calls.count(("unload",)) == 2  # two candidate rounds ran
    oor.assert_awaited_once()  # the jammed spool taken out of rotation exactly once
    assert unload_seen_at_oor == [False]  # ...and BEFORE the first unload
    db_session.expunge_all()
    assert (await db_session.get(Spool, jammed.id)).feed_fault_at is not None  # jammed left OOR
    refreshed = await db_session.get(PrintQueueItem, item.id)
    assert json.loads(refreshed.ams_mapping) == [2, -1, -1, -1]  # landed on tray2


async def test_pre_commit_abort_leaves_no_stamp(db_session, printer_factory, install_settings, monkeypatch):
    """External interference DURING the reset wait (live state disappears before the
    swap-commit boundary) → abort. Nothing is committed, so the jammed spool is NEVER
    taken out of rotation and no OOR notification is sent."""
    install_settings(step_timeout_s=0.05)
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    jammed = await _bind_spool(db_session, printer.id, 0, 0)  # jammed tray0
    oor = _spy(monkeypatch, "on_spool_out_of_rotation")
    state = _make_state(tray_now=255, ams_status_main=1, trays=[_ams_tray(0), _ams_tray(1)])
    client = _WedgedClient(state)  # the reset resume is accepted but the AMS never moves

    # Live state is present for the PAUSE-wait poll and the reset's wedge check, then
    # disappears during the reset WAIT (a disconnect) → _reset_stuck_change returns
    # "abort" before the commit boundary.
    polls = {"n": 0}

    def _status(_pid):
        polls["n"] += 1
        return state if polls["n"] <= 2 else None

    monkeypatch.setattr(spool_recovery.printer_manager, "get_status", _status)
    monkeypatch.setattr(spool_recovery.printer_manager, "get_client", lambda _pid: client)

    task = await on_ams_fault(printer.id, state)
    await task

    assert ("unload",) not in client.calls  # aborted before the swap round
    oor.assert_not_awaited()  # nothing committed → no out-of-rotation notify
    db_session.expunge_all()
    assert (await db_session.get(Spool, jammed.id)).feed_fault_at is None  # never stamped


async def test_extruder_side_commits_the_swap_and_parks_nothing(
    db_session, printer_factory, install_settings, monkeypatch, caplog
):
    """006-H2S 2026-09-21, incident 289 — the rule this test used to assert the
    opposite of.

    An extruder-side-only fault still COMMITS the swap (unload → load → resume, in that
    order), because fresh filament often clears the immediate overload. What it must not
    do is blame a spool: neither the one that was feeding when the extruder overloaded
    nor the replacement that re-jams behind it. The driver had applied that rule to the
    replacement since WS2 and stamped the feeding roll anyway — one fault, two opposite
    conclusions about the same evidence. Both now read `_commit_out_of_rotation`.

    No stamp means no page: the out-of-rotation notification fires inside the stamp.
    """
    install_settings()
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    original = await _bind_spool(db_session, printer.id, 0, 0)  # feeding tray0
    replacement = await _bind_spool(db_session, printer.id, 0, 1)  # tray1 re-jams
    oor = _spy(monkeypatch, "on_spool_out_of_rotation")
    state = _make_state(trays=[_ams_tray(0), _ams_tray(1), _ams_tray(2)], hms=[_extruder_hms()])
    client = FakeClient(state)  # tray1 re-jams both cycles; tray2 succeeds
    _wire(monkeypatch, state, client, on_poll=_repause_after_running(2))

    with caplog.at_level(logging.INFO, logger="backend.app.services.spool_recovery"):
        task = await on_ams_fault(printer.id, state)
        await task

    assert state.state == "RUNNING"
    # The swap itself is untouched: unload before the first load, resume after it.
    published = [c for c in client.calls if c[0] in ("unload", "load", "resume")]
    assert published.index(("unload",)) < published.index(("load", 1)) < published.index(("resume",))
    oor.assert_not_awaited()  # nothing parked → nothing announced
    db_session.expunge_all()
    assert (await db_session.get(Spool, original.id)).feed_fault_at is None  # the feeding roll is not the suspect
    assert (await db_session.get(Spool, replacement.id)).feed_fault_at is None  # nor is the replacement
    refreshed = await db_session.get(PrintQueueItem, item.id)
    assert json.loads(refreshed.ams_mapping) == [2, -1, -1, -1]  # landed on tray2
    # One line per spool kept in rotation, in the wording the replacement rule already had.
    kept = [
        r.getMessage()
        for r in caplog.records
        if "kept IN rotation — extruder-side fault 0300_801E is the common factor, not the spool" in r.getMessage()
    ]
    assert any(" jammed tray 0 " in m for m in kept)
    assert any(" replacement tray 1 " in m for m in kept)


async def test_an_ams_side_jam_still_stamps_exactly_once(db_session, printer_factory, install_settings, monkeypatch):
    """The other half of the partition — the rule narrows nothing for an AMS-side jam.

    The same swap the extruder-side case above runs, driven by ``0700_8010`` instead:
    here the fault IS about the roll, so the jammed spool is parked at the commit
    boundary and announced — once, from the one verb, with its code on the row."""
    install_settings()
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    original = await _bind_spool(db_session, printer.id, 0, 0)  # jammed tray0
    oor = _spy(monkeypatch, "on_spool_out_of_rotation")
    state = _make_state(trays=[_ams_tray(0), _ams_tray(1)])  # default hms = 0700_8010
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    await task

    assert state.state == "RUNNING"
    oor.assert_awaited_once()
    db_session.expunge_all()
    stamped = await db_session.get(Spool, original.id)
    assert stamped.feed_fault_at is not None
    assert stamped.feed_fault_code == "0700_8010"


async def test_selection_excludes_the_jammed_tray_even_unstamped(
    db_session, printer_factory, install_settings, monkeypatch
):
    """The stamp was never what kept the driver off the jammed tray this round.

    Replacement selection excludes the jammed slot by ID (`_select_replacement` passes
    it as the excluded tray), so dropping the extruder-side stamp cannot make the swap
    reload the very spool it just unloaded — which is the one thing the stamp might
    plausibly have been load-bearing for."""
    install_settings()
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    jammed = await _bind_spool(db_session, printer.id, 0, 0)
    state = _make_state(trays=[_ams_tray(0), _ams_tray(1)], hms=[_extruder_hms()])
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    await task

    loads = [c for c in client.calls if c[0] == "load"]
    assert loads and all(c[1] != 0 for c in loads), f"the jammed tray must never be a candidate: {loads}"
    db_session.expunge_all()
    assert (await db_session.get(Spool, jammed.id)).feed_fault_at is None


async def test_ams_drying_escalation_keeps_commit_stamp(
    db_session, printer_factory, install_settings, monkeypatch, caplog
):
    """A post-commit escalation KEEPS the out-of-rotation stamp: the jammed spool is
    committed out of rotation right before the unload, then the unload finds the AMS
    drying → escalate ams_drying. The commit-boundary stamp means 'recovery is
    abandoning this spool', so it correctly stays."""
    install_settings()
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    jammed = await _bind_spool(db_session, printer.id, 0, 0)  # jammed tray0
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    oor = _spy(monkeypatch, "on_spool_out_of_rotation")
    state = _make_state(trays=[_ams_tray(0), _ams_tray(1)])
    client = FakeClient(state, write_refusal="drying")  # the AMS is drying → writes refused
    _wire(monkeypatch, state, client)

    with caplog.at_level(logging.WARNING, logger="backend.app.services.spool_recovery"):
        task = await on_ams_fault(printer.id, state)
        await task

    assert ("unload",) not in client.calls  # a drying lane is never written to
    assert _escalated_reasons(caplog) == ["ams_drying"]
    oor.assert_awaited_once()  # the commit stamp fired before the drying refusal
    failed.assert_awaited_once()
    db_session.expunge_all()
    assert (await db_session.get(Spool, jammed.id)).feed_fault_at is not None  # stamp KEPT across the escalation
    assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason == WAITING_REASON_FAILED


# ===========================================================================
# will_own: the public predicate the HMS notify pipeline uses to SUPPRESS a raw
# per-code alert for a fault recovery will OWN (its lifecycle notifications carry the
# incident). Mirrors only the on_feed_fault_hms entry gates whose failure means
# "nobody will notify".
# ===========================================================================


async def test_will_own_true_when_enabled_and_farm_item_printing(db_session, printer_factory):
    printer = await printer_factory()
    await _farm_item(db_session, printer.id, subtask="task-1")
    state = _make_state(subtask="task-1")

    assert await spool_recovery.will_own(db_session, printer.id, state) is True


async def test_will_own_false_when_setting_disabled(db_session, printer_factory, monkeypatch):
    printer = await printer_factory()
    await _farm_item(db_session, printer.id, subtask="task-1")
    state = _make_state(subtask="task-1")

    async def _disabled(_db, key, default):
        return False if key == "spool_recovery_enabled" else default

    monkeypatch.setattr(spool_recovery, "_read_bool", _disabled)

    assert await spool_recovery.will_own(db_session, printer.id, state) is False


async def test_will_own_false_when_the_fault_was_already_closed_as_aborted(db_session, printer_factory):
    """A fault an ABORTED close barred will never be owned again — so the raw alert
    must be let through rather than suppressed into silence."""
    printer = await printer_factory()
    await _farm_item(db_session, printer.id, subtask="task-1")
    state = _make_state(subtask="task-1")
    fingerprint = spool_recovery.candidate_fingerprint(spool_recovery.live_candidates(state))
    incident = await _seed_incident(
        db_session, printer.id, kind="jam", code="0700_8010", codes=fingerprint, status="recovering"
    )
    from backend.app.services import printer_incidents

    await printer_incidents.close(db_session, incident.id, status="aborted", source="operator")
    spool_recovery._blocked[(printer.id, "task-1")] = {fingerprint}

    assert await spool_recovery.will_own(db_session, printer.id, state) is False


async def test_will_own_true_for_a_foreign_print(db_session, printer_factory):
    """WS2b: an incident owns a foreign print's AMS fault too, so its raw per-code
    alert is the duplicate and must still be suppressed.

    This assertion is INVERTED from the pre-WS2b pin, deliberately: requiring a farm
    queue item here is exactly what left 12 foreign-print runouts spent-stamped with
    no alert, no hold and no resume."""
    printer = await printer_factory()
    # No farm item dispatched for this subtask → a foreign / non-farm job.
    state = _make_state(subtask="foreign-task")

    assert await spool_recovery.will_own(db_session, printer.id, state) is True


async def test_will_own_false_when_db_read_raises(db_session, printer_factory, monkeypatch):
    """Fail toward notifying: any exception in the predicate returns False so a raw
    alert is never suppressed on the strength of a read that errored."""
    printer = await printer_factory()
    state = _make_state(subtask="task-1")

    async def _boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(spool_recovery.printer_incidents, "get_open", _boom)

    assert await spool_recovery.will_own(db_session, printer.id, state) is False


# ===========================================================================
# F2 — the escalation names the slot the FIRMWARE demands (006-H2S 2026-07-26)
# ===========================================================================


async def test_runout_escalation_names_the_firmware_demanded_slot(
    db_session, printer_factory, install_settings, monkeypatch
):
    """THE incident pin. Dispatch mapping [0], tray_now 255 (nothing feeding), and a
    standing 0700_2200_0002_0001 demand for slot 3. The old resolver answered the
    mapping's global tray 0 and told the operator "AMS A slot 1" — a slot the printer
    was not asking for and would not have resumed on. Firmware demand is primary now."""
    install_settings()
    printer = await printer_factory()
    await _farm_item(db_session, printer.id, ams_mapping="[0, -1, -1, -1]")
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    state = _make_state(
        tray_now=255,  # nothing feeding — the live-tray fallback has nothing to say
        hms=[_runout_autoswitched_hms(0, 0), _runout_demand_hms(0, 2), _runout_same_slot_hms()],
    )
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    assert task is not None
    await task

    failed.assert_awaited_once()
    assert failed.call_args.kwargs["runout_slot"] == "AMS A slot 3"
    assert failed.call_args.kwargs["kind"] == "runout"


def _resolve_jam(item, state, *, candidates=(), printer_id=None):
    """The tray resolution the entry gate runs for a MECHANICAL fault."""
    from backend.app.models.printer_incident import KIND_JAM

    return spool_recovery._resolve_fault_tray(
        item, state, kind=KIND_JAM, external=False, candidates=candidates, printer_id=printer_id
    )


def _resolve_runout(item, state, *, candidates=(), printer_id=None):
    """The tray resolution the entry gate runs for an AMS-slot RUNOUT."""
    from backend.app.models.printer_incident import KIND_RUNOUT

    return spool_recovery._resolve_fault_tray(
        item, state, kind=KIND_RUNOUT, external=False, candidates=candidates, printer_id=printer_id
    )


def test_resolve_runout_tray_prefers_the_demand_over_mapping_and_tray_now():
    """Unit-level pin on the resolver itself: mapping [0] + tray_now 255 + a slot-3
    demand resolves to GLOBAL TRAY 2 (= AMS A slot 3), verdict single."""
    from types import SimpleNamespace

    item = SimpleNamespace(ams_mapping="[0, -1, -1, -1]")
    state = SimpleNamespace(tray_now=255, hms_errors=[_runout_demand_hms(0, 2), _runout_same_slot_hms()])

    assert _resolve_runout(item, state) == (2, "single")
    assert printer_incidents.runout_slot_desc(2) == "AMS A slot 3"


def test_resolve_jam_tray_ignores_any_demand():
    """A FEED FAULT is never re-attributed by a stale runout demand — the 8010 family
    carries no slot attribution, so the feeder evidence answers it."""
    from types import SimpleNamespace

    item = SimpleNamespace(ams_mapping="[0, -1, -1, -1]")
    state = SimpleNamespace(tray_now=255, hms_errors=[_runout_demand_hms(0, 2)])

    assert _resolve_jam(item, state) == (0, "single")


def test_resolve_runout_tray_falls_back_when_no_demand():
    """No demand on the wire (bare 8011 only) → the feeder evidence is the answer."""
    from types import SimpleNamespace

    item = SimpleNamespace(ams_mapping="[1, -1, -1, -1]")
    state = SimpleNamespace(tray_now=255, hms_errors=[_runout_same_slot_hms()])

    assert _resolve_runout(item, state) == (1, "single")


# ===========================================================================
# C1 — the wire-first jammed-feeder resolution, origin-agnostic
# ===========================================================================


def _wire_state(**kw):
    """A minimal live-state stub for the pure resolver cases."""
    from types import SimpleNamespace

    kw.setdefault("tray_now", 255)
    kw.setdefault("hms_errors", [])
    return SimpleNamespace(**kw)


def _mech_wire_hms(ams_id=0, tray_id=1):
    """hms[] lane: "AMS A slot 2 feeder unit motor is stalled…" (0x00020012) — a
    MECHANICAL fault whose attr names the tray, the 2026-08-06 cascade's second code."""
    attr = 0x07000000 | (ams_id << 16) | ((0x20 + tray_id) << 8)
    return HMSError(code="0x20012", attr=attr, module=7, severity=2, full_code=f"{attr:08X}00020012")


class TestJammedFeederResolution:
    """The evidence ladder every print now walks, farm-dispatched or not."""

    def test_the_faults_own_slot_attribution_wins(self):
        """Tier 1: when the attr names the tray, nothing else is consulted — not the
        mapping that says feeder 0, not the tray_now that says feeder 3."""
        from types import SimpleNamespace

        item = SimpleNamespace(ams_mapping="[0, -1, -1, -1]")
        candidates = spool_recovery.live_candidates(_wire_state(hms_errors=[_mech_wire_hms(0, 1)]))

        assert _resolve_jam(item, _wire_state(tray_now=3), candidates=candidates) == (1, "single")

    def test_a_stable_live_feeder_answers_without_any_mapping(self):
        """Tier 2, and the whole point of the ruling: a foreign print carries no
        mapping, and the feeding tray identifies the jam on its own."""
        assert _resolve_jam(None, _wire_state(tray_now=2)) == (2, "single")

    def test_last_loaded_tray_answers_when_tray_now_reads_unloaded(self):
        """After a feed fault tray_now frequently reads 255 "nothing feeding" while
        the jam is on the tray that fed a second earlier."""
        assert _resolve_jam(None, _wire_state(tray_now=255, last_loaded_tray=1)) == (1, "single")

    def test_external_and_unloaded_sentinels_are_not_feeders(self):
        """254 (external) and 255 (nothing fed) are sentinels, not trays."""
        assert _resolve_jam(None, _wire_state(tray_now=254, last_loaded_tray=255)) == (None, "none")

    def test_conflicting_feeders_escalate_as_multi_feeder(self):
        """A multi-material job: the swap cannot hold (the firmware re-loads the
        originally mapped slot at the next filament change), so the verdict wins even
        though tray_now names a perfectly good feeder."""
        from types import SimpleNamespace

        item = SimpleNamespace(ams_mapping="[0, 1, -1, -1]")

        assert _resolve_jam(item, _wire_state(tray_now=0)) == (None, "multi_feeder")

    def test_nothing_derivable_is_unresolved(self):
        assert _resolve_jam(None, _wire_state()) == (None, "none")

    def test_the_slicer_mapping_carries_the_verdict_for_a_foreign_print(self, monkeypatch):
        """A foreign multi-colour print DOES make a pre-fault statement about how many
        filaments it maps — the ams_mapping Studio/Orca sent on the request topic."""
        monkeypatch.setattr(spool_recovery, "_slicer_mapping", lambda _pid: [0, 2])

        assert _resolve_jam(None, _wire_state(tray_now=0), printer_id=7) == (None, "multi_feeder")

    def test_a_single_slicer_feeder_is_corroboration_not_a_conflict(self, monkeypatch):
        monkeypatch.setattr(spool_recovery, "_slicer_mapping", lambda _pid: [3, -1])

        assert _resolve_jam(None, _wire_state(tray_now=255), printer_id=7) == (3, "single")

    def test_a_one_way_feeder_move_is_not_multi_material(self):
        """THE regression this ladder must not cause: a firmware auto-refill (or an
        earlier recovery swap) moves the feeder exactly once. Reading that as
        "multi-material" would take the swap machine away from every farm print the
        moment a backup slot took over."""
        state = _wire_state(tray_now=1, tray_change_log=[(0, 0), (1, 40)])

        assert _resolve_jam(None, state) == (1, "single")

    def test_a_feeder_returned_to_is_multi_material(self):
        """Alternation is the fingerprint a one-way move cannot forge."""
        state = _wire_state(tray_now=0, tray_change_log=[(0, 0), (1, 40), (0, 80)])

        assert _resolve_jam(None, state) == (None, "multi_feeder")

    def test_the_dispatch_mapping_outranks_the_fed_log(self):
        """Witnesses do not union: the farm's own mapping says single-feeder, so a
        log that recorded a backup switch cannot overrule it into an escalation."""
        from types import SimpleNamespace

        item = SimpleNamespace(ams_mapping="[0, -1, -1, -1]")
        state = _wire_state(tray_now=1, tray_change_log=[(0, 0), (1, 40), (0, 80)])

        assert _resolve_jam(item, state) == (1, "single")


# ===========================================================================
# F2 — guidance refresh when the firmware's demand MOVES
# ===========================================================================


async def _runout_held_item(db, printer_id, *, subtask="task-1"):
    """A farm unit already ESCALATED on a runout (the state the refresh acts on)."""
    item = await _farm_item(db, printer_id, subtask=subtask)
    item.waiting_reason = WAITING_REASON_RUNOUT
    await db.commit()
    # The hold itself is the durable INCIDENT since WS2b; the token on the unit is
    # only its projection. Both guidance lanes and the auto-resume gate on the
    # incident, which is what lets them work for a foreign print too.
    await _seed_incident(db, printer_id, job_id=subtask, item_id=item.id)
    return item


async def _seed_incident(
    db,
    printer_id,
    *,
    job_id="task-1",
    kind=None,
    status=None,
    item_id=None,
    code="0700_8011",
    codes=None,
    slot_global_tray=None,
):
    """Open a durable incident the way the entry gate would."""
    from backend.app.models.printer_incident import KIND_RUNOUT, STATUS_ESCALATED
    from backend.app.services import printer_incidents

    return await printer_incidents.open_new(
        db,
        printer_id=printer_id,
        job_id=job_id,
        item_id=item_id,
        kind=kind or KIND_RUNOUT,
        code=code,
        codes=codes or f"runout:{code}",
        slot_global_tray=slot_global_tray,
        status=status or STATUS_ESCALATED,
    )


async def test_demand_move_refreshes_guidance_once(db_session, printer_factory, monkeypatch):
    """13:51: a slot-2 demand is APPENDED while the unit sits escalated on the slot-3
    guidance. Exactly one refresh notification, carrying the NEW slot."""
    printer = await printer_factory()
    await _runout_held_item(db_session, printer.id)
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    moved = _runout_demand_hms(0, 1)
    state = _make_state(hms=[_runout_demand_hms(0, 2), _runout_same_slot_hms(), moved])

    fired = await spool_recovery.maybe_refresh_runout_guidance(printer.id, {moved.full_code}, state)

    assert fired is True
    failed.assert_awaited_once()
    assert failed.call_args.kwargs["runout_slot"] == "AMS A slot 2"
    assert failed.call_args.kwargs["kind"] == "runout"
    assert "NOW asking" in failed.call_args.kwargs["detail"]


async def test_same_demand_again_does_not_re_notify(db_session, printer_factory, monkeypatch):
    """A standing demand re-delivered on later pushes is one continuing incident."""
    printer = await printer_factory()
    await _runout_held_item(db_session, printer.id)
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    moved = _runout_demand_hms(0, 1)
    state = _make_state(hms=[_runout_demand_hms(0, 2), moved])

    await spool_recovery.maybe_refresh_runout_guidance(printer.id, {moved.full_code}, state)
    second = await spool_recovery.maybe_refresh_runout_guidance(printer.id, {moved.full_code}, state)

    assert second is False
    assert failed.await_count == 1


async def test_refresh_leaves_the_escalation_latch_untouched(db_session, printer_factory, monkeypatch):
    """Guidance only: the refresh must never re-arm recovery on a job it gave up on,
    and must never clear the hold token."""
    printer = await printer_factory()
    item = await _runout_held_item(db_session, printer.id)
    _spy(monkeypatch, "on_spool_recovery_failed")
    moved = _runout_demand_hms(0, 1)
    state = _make_state(hms=[moved])

    assert await spool_recovery.maybe_refresh_runout_guidance(printer.id, {moved.full_code}, state) is True

    from backend.app.services import printer_incidents

    held = await printer_incidents.get_open(db_session, printer.id)
    assert held is not None and held.status == "escalated"  # the hold stands
    assert printer.id not in spool_recovery._active_tasks  # no recovery re-entry
    db_session.expunge_all()
    assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason == WAITING_REASON_RUNOUT


async def test_no_refresh_without_a_runout_held_unit(db_session, printer_factory, monkeypatch):
    """Nothing is escalated here, so there is no guidance to correct."""
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)  # printing, but no runout hold
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    moved = _runout_demand_hms(0, 1)

    fired = await spool_recovery.maybe_refresh_runout_guidance(printer.id, {moved.full_code}, _make_state(hms=[moved]))

    assert fired is False
    failed.assert_not_awaited()


async def test_no_refresh_when_the_new_code_is_not_a_demand(db_session, printer_factory, monkeypatch):
    """An unrelated NEW code arriving alongside a STANDING demand must not
    re-announce it — only a demand arrival does."""
    printer = await printer_factory()
    await _runout_held_item(db_session, printer.id)
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    unrelated = HMSError(code="0x4025", attr=0x07010000, module=7, severity=2, full_code="0701000000004025")
    state = _make_state(hms=[_runout_demand_hms(0, 2), unrelated])

    fired = await spool_recovery.maybe_refresh_runout_guidance(printer.id, {unrelated.full_code}, state)

    assert fired is False
    failed.assert_not_awaited()


# ===========================================================================
# F3 — refill auto-resume (default ON)
# ===========================================================================


@pytest.fixture
def _fast_resume(monkeypatch):
    """Zero the AMS settle dwell so the two-phase gate runs without wall-clock."""
    monkeypatch.setattr(spool_recovery, "_RUNOUT_RESUME_SETTLE_S", 0.0)
    monkeypatch.setattr(spool_recovery, "_RUNOUT_RESUME_CONFIRM_S", 0.05)


def _runout_paused_state(*, tray_id=2, gcode_state="PAUSE"):
    return _make_state(
        gcode_state=gcode_state,
        tray_now=255,
        hms=[_runout_demand_hms(0, tray_id), _runout_same_slot_hms()],
    )


async def test_refill_on_the_demanded_slot_resumes_once(db_session, printer_factory, monkeypatch, _fast_resume):
    """The operator refills the slot the printer asked for; the farm resumes instead
    of making them walk back and press a button (doctrine rule 1)."""
    printer = await printer_factory()
    item = await _runout_held_item(db_session, printer.id)
    resumed = _spy(monkeypatch, "on_runout_auto_resumed")
    state = _runout_paused_state(tray_id=2)
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    assert await spool_recovery.maybe_auto_resume_on_refill(printer.id, 0, 2) is True

    assert client.calls.count(("resume",)) == 1  # exactly one resume published
    assert state.state == "RUNNING"
    resumed.assert_awaited_once()
    assert resumed.call_args.kwargs["slot_desc"] == "AMS A slot 3"
    db_session.expunge_all()
    # The hold is no longer true — a RUNNING print must not keep a phantom hold token.
    assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason is None


async def test_refill_on_a_different_slot_does_nothing(db_session, printer_factory, monkeypatch, _fast_resume):
    """A gain on a slot the firmware is NOT demanding is not the refill this print is
    waiting for — resuming on it would restart straight into the same runout."""
    printer = await printer_factory()
    item = await _runout_held_item(db_session, printer.id)
    resumed = _spy(monkeypatch, "on_runout_auto_resumed")
    state = _runout_paused_state(tray_id=2)  # firmware demands slot index 2
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    assert await spool_recovery.maybe_auto_resume_on_refill(printer.id, 0, 1) is False

    assert client.calls == []
    assert state.state == "PAUSE"
    resumed.assert_not_awaited()
    db_session.expunge_all()
    assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason == WAITING_REASON_RUNOUT


async def test_operator_resumed_during_the_settle_is_success_not_failure(
    db_session, printer_factory, monkeypatch, _fast_resume
):
    """The state moved to RUNNING before our resume landed — the print is going, so
    stand down silently. No second resume, no failure handling."""
    printer = await printer_factory()
    await _runout_held_item(db_session, printer.id)
    resumed = _spy(monkeypatch, "on_runout_auto_resumed")
    state = _runout_paused_state(tray_id=2)
    client = FakeClient(state)

    polls = {"n": 0}

    def _status(_pid):
        polls["n"] += 1
        if polls["n"] > 1:  # the operator hit Resume between the two gate passes
            state.state = "RUNNING"
        return state

    monkeypatch.setattr(spool_recovery.printer_manager, "get_status", _status)
    monkeypatch.setattr(spool_recovery.printer_manager, "get_client", lambda _pid: client)

    assert await spool_recovery.maybe_auto_resume_on_refill(printer.id, 0, 2) is False

    assert client.calls == []  # never published on top of a running print
    resumed.assert_not_awaited()


async def test_setting_off_disables_the_assist(db_session, printer_factory, monkeypatch, _fast_resume):
    from backend.app.api.routes.settings import set_setting

    printer = await printer_factory()
    await _runout_held_item(db_session, printer.id)
    await set_setting(db_session, "runout_auto_resume_enabled", "false")
    await db_session.commit()
    state = _runout_paused_state(tray_id=2)
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    assert await spool_recovery.maybe_auto_resume_on_refill(printer.id, 0, 2) is False
    assert client.calls == []
    assert state.state == "PAUSE"


async def test_resume_send_rejected_stands_aside(db_session, printer_factory, monkeypatch, _fast_resume):
    """An offline/rejected send: no retry, no quarantine, no out-of-rotation stamp —
    the escalation's guidance stays exactly as it was so the manual path still works."""
    printer = await printer_factory()
    item = await _runout_held_item(db_session, printer.id)
    resumed = _spy(monkeypatch, "on_runout_auto_resumed")
    oor = _spy(monkeypatch, "on_spool_out_of_rotation")
    state = _runout_paused_state(tray_id=2)
    client = FakeClient(state, resume_ret=False)
    _wire(monkeypatch, state, client)

    assert await spool_recovery.maybe_auto_resume_on_refill(printer.id, 0, 2) is False

    assert client.calls.count(("resume",)) == 1  # tried exactly once, never retried
    assert state.state == "PAUSE"
    resumed.assert_not_awaited()
    oor.assert_not_awaited()
    db_session.expunge_all()
    assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason == WAITING_REASON_RUNOUT


async def test_no_runout_hold_means_no_assist(db_session, printer_factory, monkeypatch, _fast_resume):
    """A PAUSE with a demand but no ESCALATED farm unit is not ours to resume."""
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)  # printing, no runout hold token
    state = _runout_paused_state(tray_id=2)
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    assert await spool_recovery.maybe_auto_resume_on_refill(printer.id, 0, 2) is False
    assert client.calls == []


async def test_assist_never_raises_on_a_broken_client(db_session, printer_factory, monkeypatch, _fast_resume):
    """Presence-edge hook: invariant 10 — it may never crash the AMS callback."""
    printer = await printer_factory()
    await _runout_held_item(db_session, printer.id)
    state = _runout_paused_state(tray_id=2)

    class _Boom:
        def resume_print(self):
            raise RuntimeError("wire down")

    monkeypatch.setattr(spool_recovery.printer_manager, "get_status", lambda _pid: state)
    monkeypatch.setattr(spool_recovery.printer_manager, "get_client", lambda _pid: _Boom())

    assert await spool_recovery.maybe_auto_resume_on_refill(printer.id, 0, 2) is False


# --- Trigger-set derivation -------------------------------------------------
# What this machine acts on is the hms_errors AMS fault TAXONOMY, read by class — the
# short-code sets spool_recovery used to hold are deleted (they could not see an
# attr-lane-only code such as 0700_0012). The pins below spell out the RATIFIED
# membership of the taxonomy's own views independently of its data, so a taxonomy edit
# that would change what this machine ACTS on fails here rather than moving with it.
#
# WIDENED 2026-08-09 (WS2b, operator-ratified partition): the swap machine's trigger
# vocabulary is now the WHOLE mechanical-feed class. WS2a had pinned it to the
# ``legacy_swap`` subset (8010 family + 0300_801E) purely to stay behavior-neutral
# while the taxonomy landed; this wave spends that marker, so the send-out (8005),
# feed-into-extruder (8006) and feed-to-extruder (8028) families — the same physical
# obstruction one step further along the path, and all PAUSE-raising — join it.

_AMS_UNIT_MODULES = ("0700", "0701", "0702", "0703", "0704", "0705", "0706", "0707")


def _fam(modules, suffix):
    return {f"{m}_{suffix}" for m in modules}


_EXTERNAL_HOLDER_MODULES = ("07FF", "07FE")

# The holder's own feed family. It is part of the mechanical-feed CLASS (so it rides
# these derived sets), but it is never a swap trigger — since 2026-08-11 an external
# feed fault routes to its own escalation before the jam machine asks its first
# question. The routing pin lives in TestExternalFeedFaultLane.
_RATIFIED_EXTERNAL_FEED = frozenset(
    _fam(_EXTERNAL_HOLDER_MODULES, "8005")
    | _fam(_EXTERNAL_HOLDER_MODULES, "8006")
    | _fam(_EXTERNAL_HOLDER_MODULES, "8028")
    | _fam(_EXTERNAL_HOLDER_MODULES, "C006")
)

_RATIFIED_AMS_FEED = frozenset(
    _fam(_AMS_UNIT_MODULES, "8005")
    | _fam(_AMS_UNIT_MODULES, "8006")
    | {"0700_8028"}
    | _RATIFIED_EXTERNAL_FEED
    | {
        "0700_8010",
        "0701_8010",
        "0702_8010",
        "0703_8010",
        "0704_8010",
        "0705_8010",
        "0706_8010",
        "0707_8010",
        "1800_8010",
        "1801_8010",
        "1802_8010",
        "1200_8010",
        "1201_8010",
        "1202_8010",
        "1203_8010",
        "12FF_8010",
    }
)
_RATIFIED_EXTRUDER = frozenset({"0300_801E"})
_RATIFIED_RUNOUT = frozenset(
    {
        "0300_8004",
        "0700_8011",
        "0701_8011",
        "0702_8011",
        "0703_8011",
        "0704_8011",
        "0705_8011",
        "0706_8011",
        "0707_8011",
    }
)


def _short_lane_hms(short: str) -> HMSError:
    """A short-code-lane entry for ``MMMM_CCCC`` (module word in the attr high half)."""
    module, code = short.split("_")
    return HMSError(code=code, attr=int(module, 16) << 16, module=int(module[:2], 16), severity=2)


def test_ams_feed_fault_set_matches_the_ratified_partition():
    from backend.app.services.hms_errors import extruder_side_short_codes, mechanical_feed_short_codes

    assert mechanical_feed_short_codes() - extruder_side_short_codes() == _RATIFIED_AMS_FEED


def test_extruder_feed_fault_set_matches_the_ratified_partition():
    from backend.app.services.hms_errors import extruder_side_short_codes, mechanical_feed_short_codes

    assert mechanical_feed_short_codes() & extruder_side_short_codes() == _RATIFIED_EXTRUDER


def test_feed_fault_union_matches_the_ratified_partition():
    from backend.app.services.hms_errors import mechanical_feed_short_codes

    assert mechanical_feed_short_codes() == _RATIFIED_AMS_FEED | _RATIFIED_EXTRUDER


def test_recoverable_set_matches_the_ratified_partition():
    from backend.app.services.hms_errors import AmsFaultClass, runout_short_codes

    assert runout_short_codes() == _RATIFIED_RUNOUT
    # The driver's live-fault vocabulary is the two CLASSES those views are drawn from.
    assert frozenset({AmsFaultClass.MECHANICAL_FEED}) == spool_recovery._FEED_FAULT_CLASSES
    assert frozenset({AmsFaultClass.MECHANICAL_FEED, AmsFaultClass.RUNOUT}) == (
        spool_recovery._RECOVERABLE_FAULT_CLASSES
    )


def test_the_swap_set_is_exactly_the_mechanical_feed_class():
    """The widening's real contract: no marker sits between the taxonomy and the
    machine, so a code classified mechanical_feed IS a live feed fault to the driver.

    The one thing that DOES sit between them is hardware: an external-holder member
    of the same class is routed away from the machine by ``_route_fault``, not by
    being kept out of this vocabulary (a second membership list is exactly the
    drift doctrine invariant 1 forbids)."""
    for short in ("0700_8005", "0700_8006", "0700_8028", "07FF_8005"):
        state = _make_state(hms=[_short_lane_hms(short)])
        assert spool_recovery._feed_fault_live(state), short
        assert short in spool_recovery._active_recoverable_codes(state), short


def test_an_attr_lane_only_feeder_stall_is_a_live_feed_fault():
    """012-H2S 2026-09-22 (incident 304): ``0700_0012`` — AMS A slot 4 feeder motor
    stalled — has ONLY a code-word row (the attr lane). The deleted short-code sets could
    not see it, so the self-heal test and the repause-vs-abort choice read "no fault"
    while it stood alone. Read by class over both lanes, it is live."""
    state = _make_state(tray_now=3, ams_status_main=1, hms=[_mech_wire_hms(0, 3)])

    assert spool_recovery._feed_fault_live(state)
    assert spool_recovery._active_recoverable_codes(state) == frozenset({"0700_0012"})
    assert not spool_recovery._unload_skippable(state)  # a standing feed fault is never "clean"


def test_the_legacy_swap_marker_is_gone():
    """Hard cutover: the WS2a behavior pin must not survive its consumer wave."""
    import backend.app.services.hms_errors as hms_errors

    assert not hasattr(hms_errors, "legacy_swap_short_codes")
    assert not hasattr(hms_errors, "_LEGACY_SWAP_SHORTS")


def test_external_spool_runouts_are_not_recoverable():
    """No AMS slot means no sibling tray — the swap machine has nothing to swap to."""
    from backend.app.services.hms_errors import AmsFaultClass

    assert AmsFaultClass.RUNOUT_EXTERNAL not in spool_recovery._RECOVERABLE_FAULT_CLASSES
    state = _make_state(hms=[_short_lane_hms("07FF_8011")])
    assert spool_recovery._active_recoverable_codes(state) == frozenset()


# ===========================================================================
# WS2b — the incident lanes: foreign prints, physical faults, hold lifecycle,
# the widened swap set, and the two-source refill auto-resume.
#
# Everything below exists because the pre-WS2b machine could only be reached
# through a matching FARM queue item and only ever spawned on the NOTIFICATION
# dedup's new-code edge. In production that meant: 12 foreign-print runouts
# spent-stamped with no alert / no hold / no resume, 9 runout episodes that never
# even logged, an escalation latch that outlived its fault, and a hold only the
# farm's own auto-resume could clear.
# ===========================================================================


def _physical_wire_hms(ams_id=0, tray_id=2):
    """hms[] lane: "AMS A Slot 3's filament may be broken in AMS." (0x00020003)."""
    attr = 0x07000000 | (ams_id << 16) | ((0x20 + tray_id) << 8)
    return HMSError(code="0x20003", attr=attr, module=7, severity=2, full_code=f"{attr:08X}00020003")


def _physical_short_hms():
    """print_error lane: 0700_8003 "Failed to pull out the filament from the extruder"."""
    return HMSError(code="8003", attr=0x07008003, module=7, severity=3, full_code="07008003")


def _sendout_hms():
    """0700_8005 "The AMS failed to send out filament" — mechanical_feed, and a swap
    trigger since the 2026-08-09 ratified widening (it was classified-but-unowned)."""
    return HMSError(code="8005", attr=0x07000000, module=7, severity=2, full_code="0700000000008005")


def _external_runout_hms():
    """07FF_8011 — the EXTERNAL spool holder ran dry. No AMS slot, no sibling tray."""
    return HMSError(code="8011", attr=0x07FF0000, module=7, severity=2, full_code="07FF000000008011")


async def _incident_row(db, printer_id):
    from backend.app.services import printer_incidents

    return await printer_incidents.get_open(db, printer_id)


def _capture_spawns(monkeypatch):
    """Capture what the sync wire sampler fires and forgets, so a test can await it."""
    import backend.app.core.tasks as core_tasks

    spawned = []

    def _spawn(coro, name=None):
        spawned.append(coro)
        return None

    monkeypatch.setattr(core_tasks, "spawn_background_task", _spawn)
    return spawned


# --- FOREIGN prints ---------------------------------------------------------


async def test_foreign_runout_opens_an_incident_and_alerts(db_session, printer_factory, install_settings, monkeypatch):
    """A runout on a print the farm did NOT dispatch: incident + hold + guidance,
    and NOT ONE queue row touched.

    The production shape this closes: 12 foreign-print runouts got spent stamps but
    no alert, no hold and no resume, because the entry gate required a matching farm
    unit and returned before anything else could happen."""
    install_settings()
    printer = await printer_factory()
    # A farm unit exists on this printer but for a DIFFERENT job — the live print is
    # foreign, which is exactly how a screen-started job looks.
    other = await _farm_item(db_session, printer.id, subtask="task-other")
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    state = _make_state(subtask="foreign-job", hms=[_runout_same_slot_hms()])
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    assert task is not None
    await task

    row = await _incident_row(db_session, printer.id)
    assert row is not None
    assert row.item_id is None  # FOREIGN — no queue unit owns it
    assert row.kind == "runout"
    assert row.status == "escalated"  # held for a same-slot refill
    failed.assert_awaited_once()
    assert failed.call_args.kwargs["kind"] == "runout"
    assert failed.call_args.kwargs["foreign"] is True
    # The swap machine is never entered for a runout (doctrine invariant 9).
    assert not any(c[0] in ("unload", "load") for c in client.calls)
    # And no farm row was mutated — a foreign incident owns nothing in the queue.
    db_session.expunge_all()
    assert (await db_session.get(PrintQueueItem, other.id)).waiting_reason is None


def _cascade_20260806():
    """The 2026-08-06 printer-4 mechanical cascade, verbatim.

    ``0700_8005`` "The AMS failed to send out filament" beside ``0700_0012`` "AMS A
    slot 2 feeder unit motor is stalled, cannot rotate the spool" — the pair that got
    ZERO recovery because the print was screen-started. The 0012 entry is
    slot-attributed, so it is also the tier-1 witness the resolution now reads.
    """
    return [_sendout_hms(), _mech_wire_hms(0, 1)]


@pytest.mark.parametrize("origin", ["farm", "foreign"])
async def test_the_20260806_cascade_recovers_in_both_origins(
    db_session, printer_factory, install_settings, monkeypatch, origin
):
    """THE ruling, pinned: identical wire, two origins, ONE machine.

    Both must open an incident and run the same unload → load → resume sequence. The
    ONLY difference allowed is the queue-row projection, which a foreign print has
    nothing to write to."""
    install_settings()
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id) if origin == "farm" else None
    await _bind_spool(db_session, printer.id, 0, 1)  # the jammed feeder
    await _bind_spool(db_session, printer.id, 0, 0)  # the replacement
    state = _make_state(subtask="task-1" if origin == "farm" else "screen-start", hms=_cascade_20260806())
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    assert task is not None  # the swap machine, not an escalation
    await task

    # The SAME sequence on both origins.
    assert ("unload",) in client.calls
    assert ("load", 0) in client.calls
    assert client.calls.count(("resume",)) == 1

    row = await _incident_row(db_session, printer.id)
    assert row is None  # resolved: the swap landed
    if origin == "farm":
        db_session.expunge_all()
        assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason is None
    else:
        # Nothing to project onto, and nothing invented.
        rows = (await db_session.execute(select(PrintQueueItem))).scalars().all()
        assert rows == []


@pytest.mark.parametrize("origin", ["farm", "foreign"])
async def test_the_recovering_projection_is_farm_only(
    db_session, printer_factory, install_settings, monkeypatch, origin
):
    """``waiting_reason`` is written for a farm unit and for nothing else — the
    projection is what origin decides, now that routing no longer is."""
    install_settings(step_timeout_s=5.0)
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id) if origin == "farm" else None
    state = _make_state(subtask="task-1" if origin == "farm" else "screen-start")
    client = FakeClient(state, unload_after=9999)  # park the driver inside the confirm
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    assert task is not None
    await asyncio.sleep(0.05)  # let the driver reach its recovering stamp

    if origin == "farm":
        db_session.expunge_all()
        assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason == WAITING_REASON_RECOVERING
    else:
        assert (await db_session.execute(select(PrintQueueItem))).scalars().all() == []

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_a_foreign_jam_with_no_derivable_feeder_still_escalates(
    db_session, printer_factory, install_settings, monkeypatch
):
    """Ambiguity is still refused — it is just measured now instead of assumed from
    the print's origin. Nothing feeding, no mapping, no attribution ⇒ no swap."""
    install_settings()
    printer = await printer_factory()
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    state = _make_state(subtask="foreign-job", tray_now=255)  # default hms = 0700_8010
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    assert await on_ams_fault(printer.id, state) is None  # escalated at entry

    row = await _incident_row(db_session, printer.id)
    assert row.kind == "jam" and row.status == "escalated" and row.item_id is None
    assert client.calls == []  # ZERO AMS commands published
    failed.assert_awaited_once()
    assert failed.call_args.kwargs["foreign"] is True
    assert "Could not identify which spool jammed" in failed.call_args.kwargs["detail"]


async def test_a_foreign_multi_colour_jam_escalates_on_the_slicer_mapping(
    db_session, printer_factory, install_settings, monkeypatch
):
    """A mid-print tray swap is unsound on a multi-material job whoever started it —
    the firmware re-loads the ORIGINALLY MAPPED slot at the next filament change."""
    install_settings()
    printer = await printer_factory()
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    state = _make_state(subtask="foreign-job", tray_now=0)
    client = FakeClient(state)
    client.captured_ams_mapping = [0, 1]  # what Studio sent on the request topic
    _wire(monkeypatch, state, client)

    assert await on_ams_fault(printer.id, state) is None

    row = await _incident_row(db_session, printer.id)
    assert row.kind == "jam" and row.status == "escalated"
    assert client.calls == []
    assert "Multi-filament job" in failed.call_args.kwargs["detail"]


async def test_a_foreign_incident_still_records_the_escalation_ledger(
    db_session, printer_factory, install_settings, monkeypatch
):
    """Hardware suspicion is printer-scoped by nature, so the quarantine ledger
    counts a foreign escalation exactly like a farm one."""
    install_settings()
    printer = await printer_factory()
    _spy(monkeypatch, "on_spool_recovery_failed")
    state = _make_state(subtask="foreign-job", hms=[_physical_wire_hms()])
    _wire(monkeypatch, state, FakeClient(state))

    await on_ams_fault(printer.id, state)

    rows = (await db_session.execute(select(RecoveryEscalation))).scalars().all()
    assert [r.printer_id for r in rows] == [printer.id]


# --- PHYSICAL faults (the class nothing used to consume) --------------------


@pytest.mark.parametrize(
    "hms_factory,expected_slot",
    [
        (_physical_wire_hms, 2),  # hms[] lane: the attr names the tray
        (_physical_short_hms, None),  # print_error lane: the short form has no slot
    ],
)
async def test_physical_fault_escalates_immediately_and_never_swaps(
    db_session, printer_factory, install_settings, monkeypatch, hms_factory, expected_slot
):
    """Broken filament / a clog / a failed pull-back needs hands: no swap is
    attempted, the unit takes its own token, and the alert says so.

    Before WS2b this whole class was consumed by NOTHING — those faults waited on
    the generic pause-stall watchdog."""
    install_settings()
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    await _bind_spool(db_session, printer.id, 0, 1)  # a healthy replacement IS available
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    state = _make_state(hms=[hms_factory()])
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    assert await on_ams_fault(printer.id, state) is None  # escalated at entry, no driver

    row = await _incident_row(db_session, printer.id)
    assert row.kind == "physical"
    assert row.status == "escalated"
    assert row.slot_global_tray == expected_slot
    assert client.calls == []  # the swap loop is never entered
    failed.assert_awaited_once()
    assert failed.call_args.kwargs["kind"] == "physical"
    db_session.expunge_all()
    assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason == printer_incidents.WAITING_REASON_PHYSICAL


async def test_physical_outranks_a_mechanical_sibling(db_session, printer_factory, install_settings, monkeypatch):
    """A breakage standing beside a feed fault means hands are needed whatever else
    is true — acting on the milder classification would send the swap machine at a
    fault it cannot fix."""
    install_settings()
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    await _bind_spool(db_session, printer.id, 0, 1)
    _spy(monkeypatch, "on_spool_recovery_failed")
    state = _make_state(hms=[_feed_fault_hms(), _physical_wire_hms()])
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    await on_ams_fault(printer.id, state)

    assert (await _incident_row(db_session, printer.id)).kind == "physical"
    assert client.calls == []


# --- the ratified swap-set widening -----------------------------------------


async def test_a_send_out_fault_now_enters_the_swap_loop(db_session, printer_factory, install_settings, monkeypatch):
    """0700_8005 was classified mechanical_feed by WS2a and owned by NOTHING. Since
    the 2026-08-09 operator-ratified partition it is a swap trigger like the 8010
    family — same physical obstruction, one step further along the path."""
    install_settings()
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    await _bind_spool(db_session, printer.id, 0, 0)
    state = _make_state(hms=[_sendout_hms()])
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    assert task is not None
    await task

    assert ("unload",) in client.calls
    assert ("load", 1) in client.calls
    assert state.state == "RUNNING"
    db_session.expunge_all()
    assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason is None


# --- hold lifecycle ---------------------------------------------------------


async def test_a_screen_resume_closes_the_hold_and_clears_the_token(
    db_session, printer_factory, install_settings, monkeypatch
):
    """THE (d) pin: any resume source ends the hold.

    An operator who walked to the printer and pressed Resume used to leave
    ``filament_runout_recovery_failed`` on the unit forever — only the farm's own
    auto-resume cleared it — so the run page showed a phantom hold and the hourly
    reminder kept nagging about a print that had been running for hours."""
    printer = await printer_factory()
    item = await _runout_held_item(db_session, printer.id)
    state = _make_state(gcode_state="RUNNING", hms=[])
    _wire(monkeypatch, state, FakeClient(state))

    assert await spool_recovery.on_observed_running(printer.id) is True

    assert await _incident_row(db_session, printer.id) is None
    db_session.expunge_all()
    assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason is None


async def test_the_running_edge_is_what_calls_it(db_session, printer_factory, monkeypatch):
    """The sampler turns a PAUSE→RUNNING transition into the close — this is the
    production path, since nothing else observes a screen resume."""
    printer = await printer_factory()
    await _runout_held_item(db_session, printer.id)
    spawned = _capture_spawns(monkeypatch)
    state = _make_state(hms=[_runout_same_slot_hms()])
    _wire(monkeypatch, state, FakeClient(state))

    spool_recovery.note_demand_watch(printer.id, state)  # seed the sample
    state.state, state.hms_errors = "RUNNING", []  # the operator pressed Resume
    spool_recovery.note_demand_watch(printer.id, state)

    assert len(spawned) == 1
    assert await spawned[0] is True
    assert await _incident_row(db_session, printer.id) is None


async def test_a_hold_never_survives_a_non_recovery_token(db_session, printer_factory, monkeypatch):
    """Only INCIDENT-owned tokens are cleared: a unit staged for low filament keeps its
    own reason, because a resume says nothing about that hold.

    The token used here was ``plate_not_empty_printer_detected`` until 2026-09-04, when
    the plate check became an incident KIND and its token joined the owned set — so a
    resume now legitimately clears it (pinned by the sibling test below). The class this
    test protects is unchanged: a hold raised by an owner OUTSIDE the incident machine
    (a filament deficit, a stagger wait, a capability block) must survive."""
    printer = await printer_factory()
    item = await _runout_held_item(db_session, printer.id)
    item.waiting_reason = "filament_unread_pending"
    await db_session.commit()
    state = _make_state(gcode_state="RUNNING", hms=[])
    _wire(monkeypatch, state, FakeClient(state))

    await spool_recovery.on_observed_running(printer.id)

    db_session.expunge_all()
    assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason == "filament_unread_pending"


async def test_every_incident_kinds_token_is_owned_and_cleared(db_session, printer_factory, monkeypatch):
    """A resume clears the projection of EVERY incident kind, including the three
    pause-cause kinds added 2026-09-04.

    The owned-token set is derived from the one kind -> token table
    (``printer_incidents._WAITING_REASON_BY_KIND``) rather than hand-listed, which is
    what stops a newly registered kind from projecting a token nothing can clear — the
    unit would then render a hold forever while the printer printed on."""
    owned = set(printer_incidents._WAITING_REASON_BY_KIND.values())
    assert owned <= printer_incidents.RECOVERY_WAITING_REASONS

    printer = await printer_factory()
    item = await _runout_held_item(db_session, printer.id)
    item.waiting_reason = printer_incidents.waiting_reason_for(KIND_PLATE_VISION)
    await db_session.commit()
    state = _make_state(gcode_state="RUNNING", hms=[])
    _wire(monkeypatch, state, FakeClient(state))

    await spool_recovery.on_observed_running(printer.id)

    db_session.expunge_all()
    assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason is None


async def test_startup_closes_a_stale_hold_on_a_running_printer(db_session, printer_factory, monkeypatch):
    """A restart proves nothing about a physical hold, so the sweep is evidence-led:
    a printer now RUNNING was resumed while we were down."""
    printer = await printer_factory()
    await _runout_held_item(db_session, printer.id)
    state = _make_state(gcode_state="RUNNING", hms=[])
    _wire(monkeypatch, state, FakeClient(state))

    assert await spool_recovery.rearm_incidents_on_startup() == 1

    assert await _incident_row(db_session, printer.id) is None


async def test_startup_keeps_a_hold_on_a_still_paused_printer(db_session, printer_factory, monkeypatch):
    printer = await printer_factory()
    await _runout_held_item(db_session, printer.id)
    state = _make_state(gcode_state="PAUSE", hms=[_runout_same_slot_hms()])
    _wire(monkeypatch, state, FakeClient(state))

    assert await spool_recovery.rearm_incidents_on_startup() == 0

    row = await _incident_row(db_session, printer.id)
    assert row is not None and row.status == "escalated"
    # ...and the chip projection is rebuilt, so the hold is visible again.
    from backend.app.services import printer_incidents

    assert printer_incidents.snapshot(printer.id)["kind"] == "runout"


async def test_startup_leaves_a_hold_alone_when_the_printer_has_not_reported(db_session, printer_factory, monkeypatch):
    printer = await printer_factory()
    await _runout_held_item(db_session, printer.id)
    monkeypatch.setattr(spool_recovery.printer_manager, "get_status", lambda _pid: None)

    assert await spool_recovery.rearm_incidents_on_startup() == 0
    assert await _incident_row(db_session, printer.id) is not None


# ===========================================================================
# C2 — a zombie ``recovering`` row always gets a driver back
#
# ``recovering`` is a PROMISE that a task is acting on the row. A restart breaks it,
# and because exclusivity is one-open-incident-per-printer, the orphan then blocked
# EVERY future incident on that printer for good.
# ===========================================================================


class TestZombieRecoveringRearm:
    @pytest.fixture(autouse=True)
    def _settings(self, install_settings):
        install_settings()

    async def _zombie(self, db, printer_id, *, item_id=None):
        from backend.app.models.printer_incident import KIND_JAM, STATUS_RECOVERING

        return await _seed_incident(
            db,
            printer_id,
            kind=KIND_JAM,
            status=STATUS_RECOVERING,
            item_id=item_id,
            code="0700_8010",
            codes="mechanical_feed:0700_8010",
        )

    async def test_a_paused_printer_with_a_live_fault_re_enters_the_machine(
        self, db_session, printer_factory, monkeypatch
    ):
        """The wire still names an actionable fault, so the swap machine picks the
        incident back up rather than the row sitting there driverless."""
        printer = await printer_factory()
        await self._zombie(db_session, printer.id)
        await _bind_spool(db_session, printer.id, 0, 0)
        await _bind_spool(db_session, printer.id, 0, 1)
        state = _make_state(gcode_state="PAUSE")  # default hms = the 0700_8010 jam
        client = FakeClient(state)
        _wire(monkeypatch, state, client)

        assert await spool_recovery.rearm_incidents_on_startup() == 0
        task = spool_recovery._active_tasks.get(printer.id)
        assert task is not None
        await task

        assert ("unload",) in client.calls
        assert ("load", 1) in client.calls
        assert await _incident_row(db_session, printer.id) is None  # it RESOLVED

    async def test_a_paused_printer_with_no_actionable_fault_is_escalated(
        self, db_session, printer_factory, monkeypatch
    ):
        """No fault left on the wire is NOT "fine": the printer is PAUSEd with a swap
        half-executed. It becomes a hold a human — or a RUNNING transition — clears."""
        printer = await printer_factory()
        await self._zombie(db_session, printer.id)
        failed = _spy(monkeypatch, "on_spool_recovery_failed")
        state = _make_state(gcode_state="PAUSE", hms=[])
        client = FakeClient(state)
        _wire(monkeypatch, state, client)

        assert await spool_recovery.rearm_incidents_on_startup() == 0

        row = await _incident_row(db_session, printer.id)
        assert row is not None and row.status == "escalated"
        assert client.calls == []
        failed.assert_awaited_once()
        assert "interrupted" in failed.call_args.kwargs["detail"]

    async def test_a_printer_that_has_not_reported_is_escalated_not_left_driverless(
        self, db_session, printer_factory, monkeypatch
    ):
        """The startup sweep runs before printers report, so this is the common shape.
        An ESCALATED row is still open — it just has an owner and an hourly reminder,
        and the wire sampler closes it the moment the printer is seen RUNNING."""
        printer = await printer_factory()
        await self._zombie(db_session, printer.id)
        _spy(monkeypatch, "on_spool_recovery_failed")
        monkeypatch.setattr(spool_recovery.printer_manager, "get_status", lambda _pid: None)
        monkeypatch.setattr(spool_recovery.printer_manager, "get_client", lambda _pid: None)

        assert await spool_recovery.rearm_incidents_on_startup() == 0

        row = await _incident_row(db_session, printer.id)
        assert row is not None and row.status == "escalated"

    async def test_a_running_printer_still_resolves_before_any_re_entry(self, db_session, printer_factory, monkeypatch):
        """Existing behaviour wins first: a printer now RUNNING was resumed while we
        were down, so the row closes and nothing is re-entered."""
        printer = await printer_factory()
        await self._zombie(db_session, printer.id)
        state = _make_state(gcode_state="RUNNING", hms=[])
        client = FakeClient(state)
        _wire(monkeypatch, state, client)

        assert await spool_recovery.rearm_incidents_on_startup() == 1

        assert await _incident_row(db_session, printer.id) is None
        assert client.calls == []
        assert spool_recovery._active_tasks.get(printer.id) is None

    async def test_an_escalated_row_is_never_re_entered(self, db_session, printer_factory, monkeypatch):
        """Only ``recovering`` is a broken promise. An ESCALATED hold already has an
        owner (a human) and must not have a machine started behind them."""
        printer = await printer_factory()
        await _runout_held_item(db_session, printer.id)
        state = _make_state(gcode_state="PAUSE", hms=[_runout_same_slot_hms()])
        client = FakeClient(state)
        _wire(monkeypatch, state, client)

        assert await spool_recovery.rearm_incidents_on_startup() == 0

        assert client.calls == []
        assert spool_recovery._active_tasks.get(printer.id) is None

    async def test_re_entry_re_projects_onto_the_live_farm_unit(self, db_session, printer_factory, monkeypatch):
        """A farm zombie keeps its projection duties across the restart."""
        printer = await printer_factory()
        item = await _farm_item(db_session, printer.id)
        await self._zombie(db_session, printer.id, item_id=item.id)
        _spy(monkeypatch, "on_spool_recovery_failed")
        state = _make_state(gcode_state="PAUSE", tray_now=255, hms=[_physical_wire_hms()])
        _wire(monkeypatch, state, FakeClient(state))

        await spool_recovery.rearm_incidents_on_startup()

        db_session.expunge_all()
        assert (
            await db_session.get(PrintQueueItem, item.id)
        ).waiting_reason == printer_incidents.WAITING_REASON_PHYSICAL


# ===========================================================================
# C4 — the EXTERNAL-spool runout takes its own operator copy
# ===========================================================================


async def test_external_runout_projects_its_own_waiting_reason(
    db_session, printer_factory, install_settings, monkeypatch
):
    """The AMS copy says "refill the AMS slot", which is the one instruction that
    cannot work here: there is no slot — the roll on the spool HOLDER must be
    replaced."""
    install_settings()
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    state = _make_state(hms=[_external_runout_hms()])
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    assert task is not None
    await task

    db_session.expunge_all()
    assert (
        await db_session.get(PrintQueueItem, item.id)
    ).waiting_reason == printer_incidents.WAITING_REASON_EXTERNAL_RUNOUT
    assert failed.call_args.kwargs["runout_slot"] == "the external spool holder"


async def test_an_ams_runout_keeps_the_ams_token(db_session, printer_factory, install_settings, monkeypatch):
    """The split is external-vs-AMS, not runout-vs-everything."""
    install_settings()
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    _spy(monkeypatch, "on_spool_recovery_failed")
    state = _make_state(hms=[_runout_same_slot_hms()])
    _wire(monkeypatch, state, FakeClient(state))

    task = await on_ams_fault(printer.id, state)
    await task

    db_session.expunge_all()
    assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason == WAITING_REASON_RUNOUT


def test_the_external_token_is_owned_and_attended():
    """Owned: an incident close clears it. Attended: the pause-stall watchdog must
    not double-escalate a hold that already alerted."""
    from backend.app.services import farm_stall

    assert printer_incidents.WAITING_REASON_EXTERNAL_RUNOUT in printer_incidents.RECOVERY_WAITING_REASONS
    assert printer_incidents.WAITING_REASON_EXTERNAL_RUNOUT in farm_stall._ATTENDED_PAUSE_REASONS


async def test_a_later_different_fault_on_the_same_job_is_recovered(
    db_session, printer_factory, install_settings, monkeypatch
):
    """THE (c) pin — the latch death.

    ``_escalated`` was a per-(printer, job) set that never expired inside a process,
    so once ANY fault on a job gave up, a LATER, DIFFERENT fault on that same job
    could never be recovered. The hold is now scoped to the incident."""
    install_settings()
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    _spy(monkeypatch, "on_spool_recovery_failed")

    # Fault 1: a runout escalates and holds. A WIRE-resolved kind on purpose — since
    # 2026-09-11 a physical fault is REPAIR-resolved and a resume alone no longer
    # ends it (pinned in TestPhysicalHoldsOutliveTheJob); the latch this test kills
    # is about the JOB, and a runout is the kind whose hold a resume does end.
    state = _make_state(hms=[_runout_hms()])
    _wire(monkeypatch, state, FakeClient(state))
    task1 = await on_ams_fault(printer.id, state)
    if task1 is not None:
        await task1
    assert (await _incident_row(db_session, printer.id)).kind == "runout"

    # The operator clears it and the print runs again — the hold ends with it.
    running = _make_state(gcode_state="RUNNING", hms=[])
    _wire(monkeypatch, running, FakeClient(running))
    await spool_recovery.on_observed_running(printer.id)

    # Fault 2 on the SAME job: a jam, which must be recovered normally.
    await _bind_spool(db_session, printer.id, 0, 0)
    state2 = _make_state()
    client2 = FakeClient(state2)
    _wire(monkeypatch, state2, client2)
    task = await on_ams_fault(printer.id, state2)
    assert task is not None
    await task
    assert ("unload",) in client2.calls


async def test_the_same_fault_after_an_abort_is_not_re_entered(
    db_session, printer_factory, install_settings, monkeypatch
):
    """An external actor took over: re-entering would fight the operator. The bar
    lifts only when the wire says this is no longer the same standing fault."""
    install_settings()
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    await _bind_spool(db_session, printer.id, 0, 0)
    state = _make_state()
    client = FakeClient(state, external_resume_on_unload=True)  # someone resumed mid-recovery
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    await task
    row = (await db_session.execute(select(PrinterIncident))).scalars().all()[-1]
    assert row.status == "aborted"

    # The same fault, still standing, must NOT re-open an incident.
    state.state = "PAUSE"
    assert await on_ams_fault(printer.id, state) is None
    assert await _incident_row(db_session, printer.id) is None


async def test_the_wire_re_arms_a_barred_fault_when_it_clears(
    db_session, printer_factory, install_settings, monkeypatch
):
    """The bar is an EDGE ledger, not a latch: once the codes stop standing, the next
    occurrence is a new fault and is owned again."""
    install_settings()
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    await _bind_spool(db_session, printer.id, 0, 0)
    state = _make_state()
    client = FakeClient(state, external_resume_on_unload=True)
    _wire(monkeypatch, state, client)
    await (await on_ams_fault(printer.id, state))

    # The fault clears from the wire...
    cleared = _make_state(gcode_state="RUNNING", hms=[])
    spool_recovery.note_demand_watch(printer.id, cleared)
    # ...and comes back later in the same job.
    state2 = _make_state()
    client2 = FakeClient(state2)
    _wire(monkeypatch, state2, client2)

    task = await on_ams_fault(printer.id, state2)
    assert task is not None
    await task
    assert ("unload",) in client2.calls


async def test_a_terminal_closes_the_hold(db_session, printer_factory, monkeypatch):
    printer = await printer_factory()
    await _runout_held_item(db_session, printer.id)
    state = _make_state(gcode_state="FINISH", hms=[])
    _wire(monkeypatch, state, FakeClient(state))

    assert await spool_recovery.on_job_terminal(printer.id, _terminal()) is True

    assert await _incident_row(db_session, printer.id) is None
    assert await spool_recovery.on_job_terminal(printer.id, _terminal()) is False  # nothing left to close


# --- refill auto-resume: two spawn sources, one body ------------------------


async def test_demand_clearing_while_paused_resumes(db_session, printer_factory, monkeypatch, _fast_resume):
    """Spawn source 2 (NEW): the firmware stops asking for filament.

    Nothing watched for this before WS2b — auto-resume rode the presence-GAIN edge
    alone, and in production it had never fired."""
    printer = await printer_factory()
    item = await _runout_held_item(db_session, printer.id)
    resumed = _spy(monkeypatch, "on_runout_auto_resumed")
    spawned = _capture_spawns(monkeypatch)
    # ONE state object, mutated in place — a live PrinterState is updated by each
    # push, and the resume must land on the same object the client drives.
    state = _runout_paused_state(tray_id=2)
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    spool_recovery.note_demand_watch(printer.id, state)  # seed: demand standing
    state.hms_errors = []  # the firmware answered: nothing to fill
    spool_recovery.note_demand_watch(printer.id, state)

    assert len(spawned) == 1
    assert await spawned[0] is True

    assert client.calls.count(("resume",)) == 1
    row = (await db_session.execute(select(PrinterIncident))).scalars().all()[-1]
    assert row.status == "resolved" and row.resolve_source == "auto_resume"
    resumed.assert_awaited_once()
    db_session.expunge_all()
    assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason is None


async def test_a_still_standing_demand_on_a_loaded_slot_resumes(db_session, printer_factory, monkeypatch, _fast_resume):
    """THE 006-H2S pin: the firmware LATCHES a bogus demand for a slot that never ran
    dry, so "wait for the demand to clear" can wait forever. A demanded slot that
    physically READS LOADED is the second admissible evidence of the same fact."""
    printer = await printer_factory()
    await _runout_held_item(db_session, printer.id)
    state = _make_state(
        gcode_state="PAUSE",
        tray_now=255,
        hms=[_runout_demand_hms(0, 1), _runout_same_slot_hms()],
        trays=[_ams_tray(0), _ams_tray(1)],  # the demanded slot 1 IS seated
    )
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    # The operator refilled a DIFFERENT slot; the gain edge fires there.
    assert await spool_recovery.maybe_auto_resume_on_refill(printer.id, 0, 0) is True

    assert client.calls.count(("resume",)) == 1


async def test_no_evidence_stands_down(db_session, printer_factory, monkeypatch, _fast_resume):
    """Neither witness: the demand still stands and the slot it names is EMPTY."""
    printer = await printer_factory()
    await _runout_held_item(db_session, printer.id)
    state = _make_state(
        gcode_state="PAUSE",
        tray_now=255,
        hms=[_runout_demand_hms(0, 3), _runout_same_slot_hms()],  # slot 3 — not in the tray list
        trays=[_ams_tray(0), _ams_tray(1)],
    )
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    assert await spool_recovery.maybe_auto_resume_on_refill(printer.id, 0, 1) is False

    assert client.calls == []


async def test_an_external_runout_resumes_when_its_code_clears(db_session, printer_factory, monkeypatch, _fast_resume):
    """The demand decoder covers AMS slots only, so the external lane watches its own
    code: no code, no runout."""
    printer = await printer_factory()
    item = await _runout_held_item(db_session, printer.id)
    spawned = _capture_spawns(monkeypatch)
    state = _make_state(gcode_state="PAUSE", tray_now=255, hms=[_external_runout_hms()])
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    spool_recovery.note_demand_watch(printer.id, state)
    state.hms_errors = []  # the operator loaded the spool holder
    spool_recovery.note_demand_watch(printer.id, state)

    assert len(spawned) == 1
    assert await spawned[0] is True
    assert client.calls.count(("resume",)) == 1
    db_session.expunge_all()
    assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason is None


async def test_auto_resume_needs_an_open_runout_incident(db_session, printer_factory, monkeypatch, _fast_resume):
    """No hold, nothing to resume — a PAUSE the farm does not own is not ours to end."""
    printer = await printer_factory()
    state = _runout_paused_state(tray_id=2)
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    assert await spool_recovery.maybe_auto_resume_on_refill(printer.id, 0, 2) is False
    assert client.calls == []


# --- liveness: the whole journey, end to end --------------------------------


async def test_liveness_runout_hold_to_confirmed_running(
    db_session, printer_factory, install_settings, monkeypatch, _fast_resume
):
    """The paired LIVENESS probe (memory: a cured storm and a starved deadlock look
    identical on absence metrics). Scripted wire sequence, asserting the EVENT
    HAPPENS rather than "no error":

        8011 arrives while PAUSE → incident + hold + alert
        → the firmware demands a slot → guidance names it
        → the operator refills; the demand clears → auto-resume publishes one resume
        → RUNNING is confirmed → the incident closes and the hold token goes.
    """
    install_settings()
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    resumed = _spy(monkeypatch, "on_runout_auto_resumed")
    spawned = _capture_spawns(monkeypatch)

    # 1. The unrescued runout lands on a PAUSEd printer.
    state = _make_state(gcode_state="PAUSE", tray_now=255, hms=[_runout_same_slot_hms()])
    client = FakeClient(state)
    _wire(monkeypatch, state, client)
    spool_recovery.note_demand_watch(printer.id, state)
    task = await on_ams_fault(printer.id, state)
    assert task is not None
    await task

    row = await _incident_row(db_session, printer.id)
    assert row is not None and row.kind == "runout" and row.status == "escalated"
    failed.assert_awaited_once()
    db_session.expunge_all()
    assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason == WAITING_REASON_RUNOUT

    # 2. The firmware names the slot it wants filled; the guidance re-announces it.
    demand = _runout_demand_hms(0, 2)
    state.hms_errors = [_runout_same_slot_hms(), demand]
    spool_recovery.note_demand_watch(printer.id, state)
    assert await spool_recovery.maybe_refresh_runout_guidance(printer.id, {demand.full_code}, state) is True
    assert failed.await_args.kwargs["runout_slot"] == "AMS A slot 3"

    # 3. The operator refills — the demand disappears from the wire.
    state.hms_errors = []
    spool_recovery.note_demand_watch(printer.id, state)
    assert len(spawned) == 1  # the sampler fired the resume lane
    assert await spawned[0] is True

    # 4. One resume, RUNNING confirmed, hold gone — the event HAPPENED.
    assert client.calls.count(("resume",)) == 1
    assert state.state == "RUNNING"
    resumed.assert_awaited_once()
    assert await _incident_row(db_session, printer.id) is None
    db_session.expunge_all()
    assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason is None


# --- the per-push entry throttle -------------------------------------------


class TestEntryThrottle:
    """The entry gate runs on EVERY push now (that decoupling is the silent-class
    fix), so a standing fault must not re-query the durable gates every second —
    while a CHANGED fault is never delayed."""

    async def test_an_unchanged_fault_is_throttled(self, db_session, printer_factory, install_settings, monkeypatch):
        install_settings()
        monkeypatch.setattr(spool_recovery, "_EVAL_THROTTLE_S", 60.0)
        printer = await printer_factory()
        # A fault that escalates AT ENTRY, so the throttle is measured without a
        # driver task running concurrently against the same rows.
        state = _make_state(subtask="foreign-job", hms=[_physical_wire_hms()])
        _wire(monkeypatch, state, FakeClient(state))
        _spy(monkeypatch, "on_spool_recovery_failed")
        calls = {"n": 0}
        real = spool_recovery.printer_incidents.get_open

        async def _counting(db, pid):
            calls["n"] += 1
            return await real(db, pid)

        monkeypatch.setattr(spool_recovery.printer_incidents, "get_open", _counting)

        await on_ams_fault(printer.id, state)  # opens the incident
        before = calls["n"]
        for _ in range(5):
            await on_ams_fault(printer.id, state)

        assert calls["n"] == before  # not one extra durable read

    async def test_a_changed_fault_is_never_throttled(self, db_session, printer_factory, install_settings, monkeypatch):
        install_settings()
        monkeypatch.setattr(spool_recovery, "_EVAL_THROTTLE_S", 60.0)
        printer = await printer_factory()
        _spy(monkeypatch, "on_spool_recovery_failed")
        state = _make_state(subtask="foreign-job", hms=[_runout_same_slot_hms()])
        _wire(monkeypatch, state, FakeClient(state))
        task = await on_ams_fault(printer.id, state)
        if task is not None:
            await task  # settle the driver before the next session touches the row
        await spool_recovery.on_job_terminal(printer.id, _terminal(job_id="foreign-job"))  # the hold ends

        # A DIFFERENT fault on the next push must be evaluated immediately.
        state2 = _make_state(subtask="foreign-job", hms=[_physical_wire_hms()])
        _wire(monkeypatch, state2, FakeClient(state2))
        await on_ams_fault(printer.id, state2)

        row = await _incident_row(db_session, printer.id)
        assert row is not None and row.kind == "physical"


# ===========================================================================
# The 003-H2S external-spool incident (2026-08-11).
#
# A print mapped to an UNCONFIGURED external holder paused demanding external
# filament. Three separate mechanisms then failed, in order:
#
#   21:43  07FF_0002 "External filament is missing" — invisible to the taxonomy,
#          so the honest firmware demand raised no incident at all;
#   21:45  07FF_8006 "feed filament into the PTFE tube" — classified mechanical_feed
#          and routed into the AMS jam machine, which invented a jammed tray,
#          escalated `jammed_tray_unresolved` and quarantined the printer for "AMS
#          hardware" off a count that included the morning's runout;
#   throughout, the farm's OWN manual queue item was attributed FOREIGN, because
#          `_resolve_farm_item` demanded a batch with a SKU file.
# ===========================================================================


def _external_missing_hms():
    """``07FF_2000_0002_0002`` — "External filament is missing; please load a new
    filament." The 21:43 demand: the holder is empty and the print is held."""
    attr = 0x07FF2000
    return HMSError(code="0x20002", attr=attr, module=7, severity=2, full_code=f"{attr:08X}00020002")


def _external_feed_hms():
    """``07FF_8006`` — "Please feed filament into the PTFE tube until it can not be
    pushed any farther." The code standing on the printer at the 21:45 PAUSE."""
    return HMSError(code="8006", attr=0x07FF0000, module=7, severity=2, full_code="07FF000000008006")


def _ams_feed_8006_hms():
    """``0700_8006`` — the SAME fault text on AMS hardware. The liveness half of
    every pin below: the split must be by hardware, never a blanket suppression."""
    return HMSError(code="8006", attr=0x07000000, module=7, severity=2, full_code="0700000000008006")


async def _manual_item(db, printer_id, *, subtask="task-1", ams_mapping=None):
    """A printing queue item with NO batch — the farm own manual print.

    Exactly the shape the pre-fix attribution dropped: the resolver joined
    ``print_batch`` and required ``sku_file_id IS NOT NULL``, so this item resolved
    ``None`` and its own incident was logged and notified as foreign.
    """
    item = PrintQueueItem(
        printer_id=printer_id,
        batch_id=None,
        status="printing",
        dispatch_subtask_id=subtask,
        ams_mapping=ams_mapping,
        started_at=datetime.utcnow(),
    )
    db.add(item)
    await db.commit()
    return item


class TestFarmItemAttribution:
    """S4: the dispatch id IS the identity — the same predicate farm_correlation uses."""

    async def test_a_manual_item_with_no_batch_matches(self, db_session, printer_factory):
        printer = await printer_factory()
        item = await _manual_item(db_session, printer.id, subtask="task-1")

        found = await spool_recovery._resolve_farm_item(db_session, printer.id, "task-1")

        assert found is not None and found.id == item.id

    async def test_a_batch_without_a_sku_file_matches(self, db_session, printer_factory):
        """A run-less batch is still the farm own dispatch."""
        printer = await printer_factory()
        batch = PrintBatch(name="ad-hoc", sku_file_id=None, status="active")
        db_session.add(batch)
        await db_session.flush()
        item = PrintQueueItem(
            printer_id=printer.id,
            batch_id=batch.id,
            status="printing",
            dispatch_subtask_id="task-1",
            started_at=datetime.utcnow(),
        )
        db_session.add(item)
        await db_session.commit()

        found = await spool_recovery._resolve_farm_item(db_session, printer.id, "task-1")

        assert found is not None and found.id == item.id

    async def test_a_different_job_id_is_foreign(self, db_session, printer_factory):
        """The id is minted per dispatch: a different one is a different print,
        however much else matches."""
        printer = await printer_factory()
        await _manual_item(db_session, printer.id, subtask="task-1")

        assert await spool_recovery._resolve_farm_item(db_session, printer.id, "foreign-999") is None

    async def test_no_job_id_resolves_nothing(self, db_session, printer_factory):
        printer = await printer_factory()
        await _manual_item(db_session, printer.id, subtask="task-1")

        assert await spool_recovery._resolve_farm_item(db_session, printer.id, "") is None

    async def test_an_item_that_is_not_printing_is_not_a_match(self, db_session, printer_factory):
        printer = await printer_factory()
        item = await _manual_item(db_session, printer.id, subtask="task-1")
        item.status = "completed"
        await db_session.commit()

        assert await spool_recovery._resolve_farm_item(db_session, printer.id, "task-1") is None

    async def test_a_manual_items_fault_is_attributed_not_foreign(
        self, db_session, printer_factory, install_settings, monkeypatch
    ):
        """The whole-lane consequence: the incident owns the unit, the hold projects
        onto the queue row the operator is watching, and the alert stops claiming the
        farm did not dispatch this print."""
        install_settings()
        printer = await printer_factory()
        item = await _manual_item(db_session, printer.id)
        failed = _spy(monkeypatch, "on_spool_recovery_failed")
        state = _make_state(hms=[_external_missing_hms()])
        _wire(monkeypatch, state, FakeClient(state))

        task = await on_ams_fault(printer.id, state)
        assert task is not None
        await task

        rows = (await db_session.execute(select(PrinterIncident))).scalars().all()
        assert [r.item_id for r in rows] == [item.id]  # ATTRIBUTED
        assert failed.call_args.kwargs["foreign"] is False
        db_session.expunge_all()
        assert (
            await db_session.get(PrintQueueItem, item.id)
        ).waiting_reason == printer_incidents.WAITING_REASON_EXTERNAL_RUNOUT


class TestExternalRunoutLane:
    """The 21:43 code: honest, invisible, and now owned."""

    async def test_the_missing_filament_demand_opens_a_runout_incident(
        self, db_session, printer_factory, install_settings, monkeypatch
    ):
        install_settings()
        printer = await printer_factory()
        await _manual_item(db_session, printer.id)
        failed = _spy(monkeypatch, "on_spool_recovery_failed")
        state = _make_state(hms=[_external_missing_hms()])
        client = FakeClient(state)
        _wire(monkeypatch, state, client)

        task = await on_ams_fault(printer.id, state)
        assert task is not None
        await task

        row = (await db_session.execute(select(PrinterIncident))).scalars().all()[-1]
        assert row.kind == "runout"  # a holder with nothing on it is a runout, not a jam
        assert row.status == "escalated"
        assert row.slot_global_tray is None  # no AMS slot exists to name
        # The swap machine is never entered for a runout (doctrine invariant 9),
        # and least of all for one with no sibling tray.
        assert client.calls == []
        assert failed.call_args.kwargs["runout_slot"] == "the external spool holder"
        assert "spool holder" in failed.call_args.kwargs["detail"]

    def test_the_verdict_is_external_and_slotless(self):
        """The taxonomy fact the whole lane rests on."""
        from backend.app.services.hms_errors import classify_hms_entry

        verdict = classify_hms_entry(_external_missing_hms())
        assert verdict.fault_class.value == "runout_external"
        assert verdict.external is True and verdict.slot is None

    async def test_it_still_auto_resumes_when_the_code_clears(
        self, db_session, printer_factory, monkeypatch, _fast_resume
    ):
        """Scope UNCHANGED: an external runout keeps its code-clear auto-resume — the
        operator loading the holder IS the "go" (doctrine rule 1)."""
        printer = await printer_factory()
        item = await _runout_held_item(db_session, printer.id)
        spawned = _capture_spawns(monkeypatch)
        state = _make_state(gcode_state="PAUSE", tray_now=255, hms=[_external_missing_hms()])
        client = FakeClient(state)
        _wire(monkeypatch, state, client)

        spool_recovery.note_demand_watch(printer.id, state)  # seed: the holder is empty
        state.hms_errors = []  # the operator loaded it
        spool_recovery.note_demand_watch(printer.id, state)

        assert len(spawned) == 1
        assert await spawned[0] is True
        assert client.calls.count(("resume",)) == 1
        db_session.expunge_all()
        assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason is None


class TestExternalFeedFaultLane:
    """The 21:45 code: a feed fault on hardware the jam machine cannot touch."""

    async def test_it_escalates_external_feed_fault_without_the_swap_machine(
        self, db_session, printer_factory, install_settings, monkeypatch
    ):
        install_settings()
        printer = await printer_factory()
        item = await _manual_item(db_session, printer.id)
        seated = await _bind_spool(db_session, printer.id, 0, 0)  # the live feeder, innocent
        await _bind_spool(db_session, printer.id, 0, 1)  # an eligible replacement
        failed = _spy(monkeypatch, "on_spool_recovery_failed")
        oor = _spy(monkeypatch, "on_spool_out_of_rotation")
        state = _make_state(tray_now=0, hms=[_external_feed_hms()])
        client = FakeClient(state)
        _wire(monkeypatch, state, client)

        task = await on_ams_fault(printer.id, state)

        assert task is None  # escalated at entry — no driver was ever spawned
        assert client.calls == []  # no unload, no load, no resume: the swap machine never ran
        row = (await db_session.execute(select(PrinterIncident))).scalars().all()[-1]
        assert row.kind == "jam" and row.status == "escalated"
        # No tray invention: `tray_now` names a real AMS feeder, and the old lane took
        # it for the jammed tray of a fault that happened on the spool holder.
        assert row.slot_global_tray is None
        # ...so nothing was taken out of rotation either.
        db_session.expunge_all()
        assert (await db_session.get(Spool, seated.id)).feed_fault_at is None
        oor.assert_not_awaited()
        assert (
            await db_session.get(PrintQueueItem, item.id)
        ).waiting_reason == printer_incidents.WAITING_REASON_EXTERNAL_FEED
        assert "No AMS is involved" in failed.call_args.kwargs["detail"]

    async def test_the_same_fault_on_ams_hardware_still_swaps(
        self, db_session, printer_factory, install_settings, monkeypatch
    ):
        """THE liveness pair (memory `liveness-paired-verification`): a cured
        misroute and a starved machine are identical on absence metrics. The SAME
        8006 text on an AMS unit must still run the full unload then load then resume."""
        install_settings()
        printer = await printer_factory()
        await _manual_item(db_session, printer.id)
        await _bind_spool(db_session, printer.id, 0, 0)
        await _bind_spool(db_session, printer.id, 0, 1)
        _spy(monkeypatch, "on_spool_out_of_rotation")
        state = _make_state(tray_now=0, hms=[_ams_feed_8006_hms()])
        client = FakeClient(state)
        _wire(monkeypatch, state, client)

        task = await on_ams_fault(printer.id, state)
        assert task is not None
        await task

        assert ("unload",) in client.calls
        assert ("load", 1) in client.calls

    async def test_an_ams_fault_beside_a_holder_fault_is_still_recovered(
        self, db_session, printer_factory, install_settings, monkeypatch
    ):
        """Both hardware paths faulting at once: the AMS one decides (it is the one a
        swap can fix), so the external flag must not disarm the machine."""
        install_settings()
        printer = await printer_factory()
        await _manual_item(db_session, printer.id)
        await _bind_spool(db_session, printer.id, 0, 0)
        await _bind_spool(db_session, printer.id, 0, 1)
        _spy(monkeypatch, "on_spool_out_of_rotation")
        state = _make_state(tray_now=0, hms=[_external_feed_hms(), _ams_feed_8006_hms()])
        client = FakeClient(state)
        _wire(monkeypatch, state, client)

        task = await on_ams_fault(printer.id, state)
        assert task is not None
        await task

        assert ("unload",) in client.calls

    async def test_it_does_not_auto_resume_when_its_code_clears(
        self, db_session, printer_factory, monkeypatch, _fast_resume
    ):
        """Scope pin: the external FEED prompt is an interactive firmware dialogue
        answered ON the printer ("feed filament into the PTFE tube..."), so its code
        clearing is not the farm cue to publish a resume. Those holds end on an
        observed RUNNING or the job terminal."""
        printer = await printer_factory()
        await _runout_held_item(db_session, printer.id)
        spawned = _capture_spawns(monkeypatch)
        state = _make_state(gcode_state="PAUSE", tray_now=255, hms=[_external_feed_hms()])
        client = FakeClient(state)
        _wire(monkeypatch, state, client)

        spool_recovery.note_demand_watch(printer.id, state)
        state.hms_errors = []
        spool_recovery.note_demand_watch(printer.id, state)

        assert spawned == []
        assert client.calls == []

    async def test_the_hold_still_ends_when_the_printer_runs_again(
        self, db_session, printer_factory, install_settings, monkeypatch
    ):
        """...and the liveness half of THAT: the hold is not a dead end. Whoever
        resumes — here the operator, at the printer — closes it."""
        install_settings()
        printer = await printer_factory()
        item = await _manual_item(db_session, printer.id)
        _spy(monkeypatch, "on_spool_recovery_failed")
        state = _make_state(hms=[_external_feed_hms()])
        _wire(monkeypatch, state, FakeClient(state))
        await on_ams_fault(printer.id, state)

        running = _make_state(gcode_state="RUNNING", hms=[])
        _wire(monkeypatch, running, FakeClient(running))
        assert await spool_recovery.on_observed_running(printer.id) is True

        assert await _incident_row(db_session, printer.id) is None
        db_session.expunge_all()
        assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason is None

    def test_the_token_is_owned_and_attended(self):
        """Owned: an incident close clears it. Attended: the pause-stall watchdog
        must not double-escalate a hold that already alerted."""
        from backend.app.services import farm_stall

        assert printer_incidents.WAITING_REASON_EXTERNAL_FEED in printer_incidents.RECOVERY_WAITING_REASONS
        assert printer_incidents.WAITING_REASON_EXTERNAL_FEED in farm_stall._ATTENDED_PAUSE_REASONS

    def test_the_escalation_reason_has_operator_facing_copy(self):
        detail = spool_recovery._ESCALATE_DETAIL["external_feed_fault"]
        assert "PTFE" in detail and "no swap" in detail

    def test_it_never_counts_toward_the_ams_quarantine(self):
        assert "external_feed_fault" in spool_recovery._NON_QUARANTINE_REASONS
        assert "external_feed_fault" not in spool_recovery._JAM_QUARANTINE_REASONS


# --- The printer-8 misread (2026-09-04 fleet outage) ------------------------
# 010-H2S held an ESCALATED runout (slot 1 empty) when a power cut rebooted the whole
# fleet. The reboot WIPED the standing HMS list; the wire sampler read the demand's
# disappearance as a demand-CLEAR edge, `_refill_ready`'s `demand is None -> True`
# confirmed it, and the farm resumed a print into an empty slot at 09:28:33. It ran
# ~2 min and re-raised the runout. Nothing had been refilled.
#
# The distinguishing fact is on the wire the whole time: the reboot leaves the
# firmware's own power-loss prompt `0300_8007` standing, which is what "the list was
# wiped" looks like versus "the firmware answered".


def _power_loss_prompt_hms():
    """`0300_8007` — "There was an unfinished print job when the printer lost power."

    The exact code every one of the 11 active printers raised first after the
    2026-09-04 reconnect. attr>>16 == 0x0300, code == 0x8007."""
    return HMSError(code="8007", attr=0x03000000, module=3, severity=3, full_code="0300000000008007")


async def test_a_reboot_wiping_the_demand_is_not_a_refill(db_session, printer_factory, monkeypatch, _fast_resume):
    """THE printer-8 PIN: a new MQTT session re-seeds the sampler, so the demand
    vanishing across a reboot spawns NO resume."""
    printer = await printer_factory()
    await _runout_held_item(db_session, printer.id)
    spawned = _capture_spawns(monkeypatch)
    state = _runout_paused_state(tray_id=1)
    state.connection_epoch = 4
    _wire(monkeypatch, state, FakeClient(state))

    spool_recovery.note_demand_watch(printer.id, state)  # seed: demand standing

    # The reboot: everything the firmware was saying is gone, replaced by the
    # power-loss prompt, and the transport has minted a new session.
    state.hms_errors = [_power_loss_prompt_hms()]
    state.connection_epoch = 5
    spool_recovery.note_demand_watch(printer.id, state)

    assert spawned == []


async def test_the_demand_clear_edge_still_fires_within_one_session(db_session, printer_factory, monkeypatch):
    """The liveness half: the epoch guard must not silence the real edge it sits on.

    Same frames, same wiped list, same printer — only the session is unchanged, and the
    resume spawns. A suppression fix that also suppresses the working case is
    indistinguishable from the bug on absence metrics alone."""
    printer = await printer_factory()
    await _runout_held_item(db_session, printer.id)
    spawned = _capture_spawns(monkeypatch)
    state = _runout_paused_state(tray_id=1)
    state.connection_epoch = 4
    _wire(monkeypatch, state, FakeClient(state))

    spool_recovery.note_demand_watch(printer.id, state)
    state.hms_errors = []
    spool_recovery.note_demand_watch(printer.id, state)

    assert len(spawned) == 1
    spawned[0].close()


async def test_a_running_edge_still_closes_a_hold_across_a_reboot(db_session, printer_factory, monkeypatch):
    """The epoch guard covers NEGATIVE edges only.

    "The printer is RUNNING again" is positive evidence a reconnect cannot fabricate —
    and it is how a hold the operator cleared during the outage gets closed. Suppressing
    it on the session boundary would strand exactly those holds, because the sampler's
    next sample would already read RUNNING and see no transition at all."""
    printer = await printer_factory()
    await _runout_held_item(db_session, printer.id)
    spawned = _capture_spawns(monkeypatch)
    state = _runout_paused_state(tray_id=1)
    state.connection_epoch = 4
    _wire(monkeypatch, state, FakeClient(state))

    spool_recovery.note_demand_watch(printer.id, state)  # seed: PAUSEd
    state.state = "RUNNING"
    state.hms_errors = []
    state.connection_epoch = 5  # the operator resumed it at the screen, then it reconnected
    spool_recovery.note_demand_watch(printer.id, state)

    assert len(spawned) == 1
    assert await spawned[0] is True


class TestRefillReadyUnderThePowerLossPrompt:
    """`_refill_ready` is pure and DB-free; these pin the one branch the outage added."""

    def test_no_demand_plus_the_standing_prompt_is_not_evidence(self):
        state = _make_state(hms=[_power_loss_prompt_hms()], tray_now=255)
        assert spool_recovery._refill_ready(state) is False

    def test_a_gain_on_a_loaded_tray_is_admissible_while_the_prompt_stands(self):
        """HARDWARE evidence, not the absence of a code. A gain on a non-demanded slot
        resumes and the firmware simply re-declares the runout — bounded and
        self-correcting, unlike a resume on an absence."""
        state = _make_state(
            hms=[_power_loss_prompt_hms()],
            tray_now=255,
            trays=[_ams_tray(0), _ams_tray(1)],
        )
        assert spool_recovery._refill_ready(state, (0, 1)) is True

    def test_an_empty_list_with_no_prompt_still_reads_as_answered(self):
        """The ordinary demand-clear path is untouched: with no prompt standing, an
        absent demand is the firmware having answered."""
        assert spool_recovery._refill_ready(_make_state(hms=[], tray_now=255)) is True


# ===========================================================================
# 006-H2S 2026-09-04: ONE jam became TWO recovery tasks and a false quarantine.
#
# The module states two facts about ownership: the OPEN INCIDENT row answers "is
# this printer owned" (the DB partial unique index enforces it) and ``_active_tasks``
# answers "is the machine ACTING right now". The observed-running closer only ever
# asked the first. While a driver is in flight a RUNNING sample is an intermediate
# reading of ITS OWN procedure — the W1 reset publishes a resume whose documented
# outcomes include "re-faults and auto-PAUSEs after moving", and the swap round's
# ``_resume_and_confirm`` publishes one too — so the closer freed the row from under
# a live task, the re-PAUSE found no open incident, and a SECOND driver spawned onto
# the same AMS: unload publishes in PAIRS 0.6 s apart, two "Spool out of rotation"
# notifications, two escalations, two ledger rows and a "Repeated AMS jam escalations
# (2 in 24h)" quarantine out of ONE jam.
#
# Every pin below drives the DRIVER and the SAMPLER on ONE state (memory
# `liveness-paired-verification`). ``_capture_spawns`` cannot serve them: it captures
# the closer WITHOUT scheduling it, so the closer would run only after the task ended
# and the pins would pass for the wrong reason.
# ===========================================================================


def _assist_stall_hms(ams_id=0, tray_id=0):
    """0700_0021 (006-H2S 17:22:31) — "AMS A slot 1 assist motor is stalled". The
    real wire form is slot-attributed: attr tray byte 0x20 (AMS 0, tray 0) and code
    word 0x00020021, which the taxonomy classifies MECHANICAL (extruder-side) — prod
    logged it as ``mechanical_feed:0700_0021@0-0`` beside ``0700_8006``. These pins
    reproduce that wire, not a curated pair of codes.

    Parameterised over the slot (the attr tray byte, same shape as
    :func:`_runout_demand_hms`) because the re-jam verdict turns on WHICH tray the
    firmware named — the replacement, or something else. ``code`` carries the 32-bit
    code WORD like every other ``hms[]``-lane entry (``_runout_demand_hms`` does the
    same): the taxonomy reads the word off ``code``, so a 16-bit ``"0021"`` here
    classified as nothing at all and the helper named a slot no reader could see."""
    attr = 0x07000000 | (ams_id << 16) | ((0x20 + tray_id) << 8)
    return HMSError(code="0x20021", attr=attr, module=7, severity=2, full_code=f"{attr:08X}00020021")


def _feed_into_extruder_hms():
    """0700_8006 (006-H2S 17:22:31) — unable to feed into the extruder. A
    MECHANICAL_FEED trigger since the 2026-08-09 operator-ratified widening."""
    return HMSError(code="8006", attr=0x07000000, module=7, severity=2, full_code="0700000000008006")


def _schedule_spawns(monkeypatch):
    """Spawn stub that SCHEDULES what the sync wire sampler fires and forgets.

    Beside :func:`_capture_spawns`, which captures without scheduling — right for the
    lanes whose gates a test drives by hand, and exactly wrong here: the closer must
    run at the DRIVER's next ``asyncio.sleep`` yield, mid-procedure, as it did at
    17:22:33. Keeps the (name, task) pairs so a pin can read what each one answered.
    """
    import backend.app.core.tasks as core_tasks

    spawned: list[tuple[str | None, asyncio.Task]] = []

    def _spawn(coro, name=None):
        task = asyncio.get_running_loop().create_task(coro)
        spawned.append((name, task))
        return task

    monkeypatch.setattr(core_tasks, "spawn_background_task", _spawn)
    return spawned


def _closer_answers(spawned) -> list:
    """What every ``on_observed_running`` the sampler spawned returned."""
    return [t.result() for name, t in spawned if (name or "").startswith("incident-running")]


def _stand_aside_lines(caplog) -> int:
    """How many times the closer deferred to a live driver (the F1 line)."""
    return sum(1 for r in caplog.records if "closer stands aside" in r.getMessage())


def _gate_refusals(caplog) -> int:
    """How many re-entries the DURABLE open-incident gate refused (not the throttle)."""
    return sum(1 for r in caplog.records if "already has an open AMS incident" in r.getMessage())


async def _incident_rows(db, printer_id) -> list:
    """EVERY incident row for this printer, whatever its status."""
    db.expunge_all()
    res = await db.execute(select(PrinterIncident).where(PrinterIncident.printer_id == printer_id))
    return list(res.scalars().all())


async def _escalation_rows(db, printer_id) -> list:
    db.expunge_all()
    res = await db.execute(select(RecoveryEscalation).where(RecoveryEscalation.printer_id == printer_id))
    return list(res.scalars().all())


async def _open_incident_row(db, printer_id, *, kind=None, item_id=None, job_id="task-1"):
    """A REAL open incident, the way the entry gate would have left it.

    Since the outcome sinks verify OWNERSHIP at write time, an escalation or a
    success against ``incident_id=0`` is now a stand-down — that is the contract, not
    a harness detail — so every pin that asserts what a sink WRITES must own a row.
    """
    from backend.app.models.printer_incident import KIND_JAM, STATUS_RECOVERING

    return await _seed_incident(
        db,
        printer_id,
        job_id=job_id,
        kind=kind or KIND_JAM,
        status=STATUS_RECOVERING,
        item_id=item_id,
        code="0700_8010",
        codes="mechanical_feed:0700_8010",
    )


async def _owned_incident(db, printer_id, *, kind=None, item_id=None, job_id="task-1", **incident_kw):
    """A driver context over a REAL open row — :func:`_incident` with an owner."""
    from backend.app.models.printer_incident import KIND_JAM

    kind = kind or KIND_JAM
    row = await _open_incident_row(db, printer_id, kind=kind, item_id=item_id, job_id=job_id)
    return replace(
        _incident(printer_id, item_id=item_id, **incident_kw),
        incident_id=row.id,
        job_id=job_id,
        kind=kind,
    )


async def _close_row(db, printer_id):
    """Close whatever this printer holds open, the way a lifecycle event would."""
    db.expunge_all()
    return await printer_incidents.close_open_for_printer(db, printer_id, source="terminal")


async def _genuine_escalation(db, printer_id, reason, *, item_id=None):
    """ONE genuine give-up: a fresh open row, the escalation, then the close.

    Two of these is the honest shape of "two escalations inside the window". A
    second ``_escalate`` against the SAME still-open row is not a second give-up —
    it is the duplicate 006-H2S produced, and since F3 it stands down rather than
    writing a second ledger row the quarantine would count.
    """
    incident = await _owned_incident(db, printer_id, step_timeout_s=0.05, item_id=item_id)
    await spool_recovery._escalate(incident, reason)
    await _close_row(db, printer_id)


def _only_driver(slots: list) -> set:
    """The distinct non-empty occupants of the printer's liveness slot."""
    return {id(s) for s in slots if s is not None}


class TestTheDriverOwnsItsOutcomeWhileItLives:
    """The 006-H2S coupling, driven end to end: driver + per-push sampler + re-entry."""

    async def test_the_w1_reset_running_edge_never_opens_a_second_incident(
        self, db_session, printer_factory, install_settings, monkeypatch, caplog
    ):
        """THE INCIDENT PIN (17:22:31 → 17:26:58).

        PAUSE, ``ams_status_main == 1`` (wedged mid filament-change), tray_now 0,
        ``0700_0021`` + ``0700_8006`` standing, a farm unit printing. The W1 reset
        publishes its resume, the printer goes RUNNING for a moment and re-PAUSEs
        with the fault still standing — and the per-push sampler turns that RUNNING
        into an ``on_observed_running`` while the driver is still inside its reset
        loop.

        ONE fault must produce ONE incident, ONE driver, ONE unload, ONE escalation,
        ONE ledger row, ONE page and NO quarantine.

        The re-entry reaches the DURABLE gate, not the throttle: the autouse
        ``_fast_timing`` fixture zeroes ``_EVAL_THROTTLE_S``, and the caplog
        assertion below names the open-incident refusal so a throttled no-op could
        never be mistaken for the fix.
        """
        install_settings(max_attempts=1, step_timeout_s=0.5)
        printer = await printer_factory()
        item = await _farm_item(db_session, printer.id)
        failed = _spy(monkeypatch, "on_spool_recovery_failed")
        _spy(monkeypatch, "on_spool_out_of_rotation")
        _spy_ws(monkeypatch)
        from backend.app.services import farm_policy

        quarantine = AsyncMock(return_value=True)
        monkeypatch.setattr(farm_policy, "quarantine_printer", quarantine)
        spawned = _schedule_spawns(monkeypatch)

        state = _make_state(tray_now=0, ams_status_main=1, hms=[_assist_stall_hms(), _feed_into_extruder_hms()])
        client = FakeClient(state, unload_stuck=True)  # the AMS ignores the unload — the 006 shape

        reentries: list[asyncio.Task] = []
        driver_slots: list = []
        row_open: list[bool] = []
        running_polls = {"n": 0}

        def _poll(_n, st):
            # Sampled BEFORE the sampler runs, so a closer firing mid-poll cannot hide
            # what the liveness slot and the row looked like while the driver was live.
            driver_slots.append(spool_recovery._active_tasks.get(printer.id))
            row_open.append(printer_incidents.snapshot(printer.id) is not None)
            spool_recovery.note_demand_watch(printer.id, st)
            if st.state != "RUNNING":
                return
            running_polls["n"] += 1
            if running_polls["n"] == 2:
                st.state = "PAUSE"  # 17:23:55 — re-PAUSEd, the fault still standing
                reentries.append(asyncio.ensure_future(on_ams_fault(printer.id, st)))

        _wire(monkeypatch, state, client, on_poll=_poll)

        with caplog.at_level(logging.INFO, logger="backend.app.services.spool_recovery"):
            task = await on_ams_fault(printer.id, state)
            assert task is not None
            await task
            for pending in reentries:
                second = await pending
                if second is not None:
                    await second  # await the duplicate driver, so its damage is visible
            for _name, closer in spawned:
                await closer

        # One fault, one record, open for the whole run, and the re-entry refused.
        assert len(await _incident_rows(db_session, printer.id)) == 1
        assert row_open and all(row_open)
        assert [t.result() for t in reentries] == [None]

        # One driver in the liveness slot — and one AMS conversation.
        assert _only_driver(driver_slots) == {id(task)}
        assert client.calls.count(("unload",)) == 1

        # One give-up, and NOT the 17:26:58 quarantine.
        assert len(await _escalation_rows(db_session, printer.id)) == 1
        failed.assert_awaited_once()
        quarantine.assert_not_called()
        db_session.expunge_all()
        assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason is not None

        # The edge FIRED and was deferred — not "it never fired" — and the re-entry
        # was refused by the DURABLE gate rather than by the throttle.
        assert _stand_aside_lines(caplog) == 1
        assert _closer_answers(spawned) == [False]
        assert _gate_refusals(caplog) == 1

    async def test_the_swap_rounds_own_resume_is_not_a_close_either(
        self, db_session, printer_factory, install_settings, monkeypatch, caplog
    ):
        """The sibling shape: ``_resume_and_confirm`` publishes a resume too. Phase 1
        waits for RUNNING and phase 2 returns ``repause`` on a re-PAUSE with a
        recoverable code — so an ordinary jam, with no wedge anywhere, produces the
        same RUNNING edge under the same live driver.
        """
        install_settings(step_timeout_s=0.5)
        printer = await printer_factory()
        await _farm_item(db_session, printer.id)
        await _bind_spool(db_session, printer.id, 0, 1)  # the replacement
        _spy(monkeypatch, "on_spool_recovery_succeeded")
        _spy(monkeypatch, "on_spool_out_of_rotation")
        _spy(monkeypatch, "on_spool_recovery_failed")
        _spy_ws(monkeypatch)
        spawned = _schedule_spawns(monkeypatch)

        state = _make_state(tray_now=0, ams_status_main=0)  # no wedge: the reset is skipped
        client = FakeClient(state)

        reentries: list[asyncio.Task] = []
        driver_slots: list = []
        row_open: list[bool] = []
        repaused = {"done": False}
        running_polls = {"n": 0}

        def _poll(_n, st):
            live = spool_recovery._active_tasks.get(printer.id)
            driver_slots.append(live)
            # Sampled only while the driver HOLDS the liveness slot: the one close it
            # must make itself is the success, and the handover poll after that
            # legitimately finds the row gone.
            if live is not None:
                row_open.append(printer_incidents.snapshot(printer.id) is not None)
            spool_recovery.note_demand_watch(printer.id, st)
            if st.state != "RUNNING" or repaused["done"]:
                return
            running_polls["n"] += 1
            if running_polls["n"] == 2:
                repaused["done"] = True
                st.state = "PAUSE"  # the first resume did not stick — the repause path
                reentries.append(asyncio.ensure_future(on_ams_fault(printer.id, st)))

        _wire(monkeypatch, state, client, on_poll=_poll)

        with caplog.at_level(logging.INFO, logger="backend.app.services.spool_recovery"):
            task = await on_ams_fault(printer.id, state)
            assert task is not None
            await task
            for pending in reentries:
                second = await pending
                if second is not None:
                    await second
            for _name, closer in spawned:
                await closer

        # The driver's own repause path ran on a row it still OWNED at every step,
        # and closed it itself at the success — under its OWN token (2026-09-11), so
        # the outcome ledger can tell this from a touchscreen resume.
        assert row_open and all(row_open)
        assert [t.result() for t in reentries] == [None]
        assert _only_driver(driver_slots) == {id(task)}
        rows = await _incident_rows(db_session, printer.id)
        assert [(r.status, r.resolve_source) for r in rows] == [("resolved", "driver_swap")]
        # TWO edges, both the driver's own: the resume that did not stick and the one
        # after the extra pause/resume cycle. Every one of them was deferred.
        answers = _closer_answers(spawned)
        assert answers == [False, False]
        assert _stand_aside_lines(caplog) == len(answers)

    async def test_an_edge_deferred_during_the_escalation_is_re_read_at_the_handover(
        self, db_session, printer_factory, install_settings, monkeypatch
    ):
        """F2. ``note_demand_watch`` is EDGE-triggered, so an edge deferred while the
        driver sits inside ``_escalate`` (tray snapshot, DB, page, quarantine, spent
        stamp) has no second chance: the row would stay ESCALATED on a demonstrably
        RUNNING printer until the 120 s sweep dwell, holding the chip, the hourly nag
        and the queue token. The handover re-reads the LEVEL once instead of storing
        the edge."""
        install_settings(step_timeout_s=0.05)
        printer = await printer_factory()
        item = await _farm_item(db_session, printer.id)
        _spy(monkeypatch, "on_spool_out_of_rotation")
        _spy_ws(monkeypatch)

        state = _make_state(tray_now=0, ams_status_main=0, trays=[_ams_tray(0)])  # only the jammed tray
        client = FakeClient(state)
        _wire(monkeypatch, state, client)

        answers: list[bool] = []

        async def _page(**_kwargs):
            # The operator presses Resume exactly while the driver is escalating.
            state.state = "RUNNING"
            answers.append(await spool_recovery.on_observed_running(printer.id))

        from backend.app.services.notification_service import notification_service

        monkeypatch.setattr(notification_service, "on_spool_recovery_failed", _page)

        task = await on_ams_fault(printer.id, state)
        assert task is not None
        await task

        assert answers == [False]  # the driver still owned the outcome
        rows = await _incident_rows(db_session, printer.id)
        assert [(r.status, r.resolve_source) for r in rows] == [("resolved", "observed_running")]
        db_session.expunge_all()
        assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason is None

    async def test_escalating_a_row_a_terminal_already_closed_stands_down(
        self, db_session, printer_factory, install_settings, monkeypatch, caplog
    ):
        """F3. ``on_job_terminal`` closes a ``recovering`` row on an operator STOP
        mid-round; the driver then times out its unload and escalates. Everything the
        escalation writes — the queue token, the page, the durable ledger row that
        feeds the 2-in-24 h quarantine — is a claim about an incident it no longer
        owns."""
        install_settings()
        printer = await printer_factory()
        item = await _farm_item(db_session, printer.id)
        row = await _open_incident_row(db_session, printer.id, item_id=item.id)
        failed = _spy(monkeypatch, "on_spool_recovery_failed")
        from backend.app.services import farm_policy

        quarantine = AsyncMock(return_value=True)
        monkeypatch.setattr(farm_policy, "quarantine_printer", quarantine)
        state = _make_state()
        _wire(monkeypatch, state, FakeClient(state))

        assert await spool_recovery.on_job_terminal(printer.id, _terminal()) is True

        incident = replace(_incident(printer.id, step_timeout_s=0.05, item_id=item.id), incident_id=row.id)
        with caplog.at_level(logging.WARNING, logger="backend.app.services.spool_recovery"):
            await spool_recovery._escalate(incident, "unload_failed")

        assert any("escalation (unload_failed) stands down" in r.getMessage() for r in caplog.records)
        failed.assert_not_awaited()
        quarantine.assert_not_called()
        assert await _escalation_rows(db_session, printer.id) == []
        db_session.expunge_all()
        assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason is None

    async def test_succeeding_on_a_row_a_terminal_already_closed_stands_down(
        self, db_session, printer_factory, install_settings, monkeypatch, caplog
    ):
        """F3, the other sink. A success writes the swapped ``ams_mapping`` back onto
        the unit, clears its hold token and pages "recovered" — all claims about an
        incident somebody else has already given a verdict."""
        install_settings()
        printer = await printer_factory()
        item = await _farm_item(db_session, printer.id)
        row = await _open_incident_row(db_session, printer.id, item_id=item.id)
        succeeded = _spy(monkeypatch, "on_spool_recovery_succeeded")
        state = _make_state()
        _wire(monkeypatch, state, FakeClient(state))

        assert await spool_recovery.on_job_terminal(printer.id, _terminal()) is True

        incident = replace(_incident(printer.id, step_timeout_s=0.05, item_id=item.id), incident_id=row.id)
        with caplog.at_level(logging.WARNING, logger="backend.app.services.spool_recovery"):
            await spool_recovery._succeed(incident, 1)

        assert any("success stands down" in r.getMessage() for r in caplog.records)
        succeeded.assert_not_awaited()
        db_session.expunge_all()
        refreshed = await db_session.get(PrintQueueItem, item.id)
        assert refreshed.ams_mapping == "[0, -1, -1, -1]"  # never rewritten

    async def test_the_closer_still_closes_when_no_driver_is_live(self, db_session, printer_factory):
        """THE LIVENESS PAIR (memory `liveness-paired-verification`). A cured storm and
        a starved lane are identical on absence metrics, so the deferral is pinned
        beside the thing it must NOT suppress: the 17:35:42 shape — an escalated hold
        with no task alive — still closes on the next resume, and so does an R1 orphan
        (a ``.done()`` task left in ``_active_tasks`` by a mid-recovery crash).

        Green before the fix as well as after, deliberately: it pins preserved
        behaviour, not the defect.
        """
        printer = await printer_factory()
        item = await _farm_item(db_session, printer.id)
        row = await _open_incident_row(db_session, printer.id, item_id=item.id)
        db_session.expunge_all()
        held = await db_session.get(PrintQueueItem, item.id)
        held.waiting_reason = spool_recovery.WAITING_REASON_RECOVERING
        await db_session.commit()

        assert await spool_recovery.on_observed_running(printer.id) is True
        rows = await _incident_rows(db_session, printer.id)
        assert [(r.id, r.status, r.resolve_source) for r in rows] == [(row.id, "resolved", "observed_running")]
        db_session.expunge_all()
        assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason is None

        # R1: a finished task in the slot is not a live driver.
        second = await _open_incident_row(db_session, printer.id, job_id="task-2")
        spool_recovery._active_tasks[printer.id] = _FakeRecoveryTask(done=True)
        assert await spool_recovery.on_observed_running(printer.id) is True
        rows = await _incident_rows(db_session, printer.id)
        assert {r.id: r.status for r in rows}[second.id] == "resolved"

    async def test_a_second_driver_over_a_live_one_is_logged_as_an_invariant_violation(
        self, db_session, printer_factory, install_settings, monkeypatch, caplog
    ):
        """F4. Observability, not a gate: entry exclusivity stays the DB's partial
        unique index (the module's own design statement). But 17:23:55 produced no
        line at all, and the 08-29 W5 lesson is that the one line naming the moment is
        what turns a 15 h triage into a grep."""
        install_settings(step_timeout_s=0.05)
        printer = await printer_factory()
        await _farm_item(db_session, printer.id)
        _spy(monkeypatch, "on_spool_recovery_failed")
        _spy(monkeypatch, "on_spool_out_of_rotation")
        _spy_ws(monkeypatch)
        state = _make_state(tray_now=0, ams_status_main=0)
        _wire(monkeypatch, state, FakeClient(state))

        # A driver is already live for this printer when the entry gate spawns.
        spool_recovery._active_tasks[printer.id] = _FakeRecoveryTask(done=False)
        with caplog.at_level(logging.WARNING, logger="backend.app.services.spool_recovery"):
            task = await on_ams_fault(printer.id, state)
            assert task is not None
            await task

        assert sum(1 for r in caplog.records if "invariant violated" in r.getMessage()) == 1


# ===========================================================================
# 002-H2S 2026-09-11: the driver that outlived its own job, and the CONTINUE it
# never sent.
#
# A layer-0 jam left the AMS PAUSEd mid filament-change (`ams_status_main == 1`).
# The W1 reset published `print.resume`; the printer retried the same slot, stalled
# and re-PAUSEd STILL in the change — and the reader called that "ok" without a line
# saying the AMS never left it. The swap round's two unloads went out with nothing
# loaded (an undecidable posture: nothing physical can move), and it escalated
# `unload_failed`. The fork's SECOND spelling of the
# firmware CONTINUE, `ams_control("resume")`, had never been sent. Then the operator
# STOPPED the print: `on_job_terminal` closed the incident row and nothing told the
# driver, which loaded slot 1 itself, published resume/pause/resume against a FAILED
# printer and — because "RUNNING never arrived" was spelled the same as "it
# re-jammed" — stamped the operator's healthy slot-1 spool out of rotation with zero
# fault evidence, then unloaded it twice.
# ===========================================================================


class TestTheDriverIsBoundToItsJob:
    """WS1: ONE takeover predicate, asked once per poll by every step."""

    async def test_operator_stop_mid_unload_confirm_ends_the_driver(
        self, db_session, printer_factory, install_settings, monkeypatch, caplog
    ):
        """THE 002 PIN (22:07). The print goes FAILED while the unload confirm polls:
        the driver stops publishing THERE — no load, no resume, no pause — and no
        replacement is ever stamped."""
        install_settings(max_attempts=2, step_timeout_s=1.0)
        printer = await printer_factory()
        await _farm_item(db_session, printer.id)
        await _bind_spool(db_session, printer.id, 0, 0)  # jammed tray0
        replacement = await _bind_spool(db_session, printer.id, 0, 1)
        _spy(monkeypatch, "on_spool_out_of_rotation")
        _spy(monkeypatch, "on_spool_recovery_failed")
        state = _make_state(trays=[_ams_tray(0), _ams_tray(1)])
        # unload_after=2 keeps tray_now feeding, so the confirm is still polling when
        # the operator's stop lands.
        client = FakeClient(state, unload_after=2)

        def _poll(_n, st):
            if ("unload",) in client.calls and st.state == "PAUSE":
                st.state = "FAILED"  # the operator stopped the print

        _wire(monkeypatch, state, client, on_poll=_poll)

        with caplog.at_level(logging.INFO, logger="backend.app.services.spool_recovery"):
            task = await on_ams_fault(printer.id, state)
            assert task is not None
            await task

        assert [c[0] for c in client.calls if c[0] in ("unload", "load", "resume", "pause")] == ["unload"]
        assert any("recovery aborted (job_ended)" in r.getMessage() for r in caplog.records)
        db_session.expunge_all()
        assert (await db_session.get(Spool, replacement.id)).feed_fault_at is None  # never touched
        rows = await _incident_rows(db_session, printer.id)
        assert [r.status for r in rows] == ["aborted"]

    async def test_a_changed_subtask_mid_unload_confirm_ends_the_driver(
        self, db_session, printer_factory, install_settings, monkeypatch, caplog
    ):
        """A different job is on the wire — whatever this driver was recovering, it is
        not what the printer is doing now."""
        install_settings(max_attempts=2, step_timeout_s=1.0)
        printer = await printer_factory()
        await _farm_item(db_session, printer.id)
        await _bind_spool(db_session, printer.id, 0, 0)
        _spy(monkeypatch, "on_spool_out_of_rotation")
        state = _make_state(trays=[_ams_tray(0), _ams_tray(1)])
        client = FakeClient(state, unload_after=2)

        def _poll(_n, st):
            if ("unload",) in client.calls:
                st.subtask_id = "task-2"

        _wire(monkeypatch, state, client, on_poll=_poll)

        with caplog.at_level(logging.INFO, logger="backend.app.services.spool_recovery"):
            task = await on_ams_fault(printer.id, state)
            await task

        assert [c[0] for c in client.calls if c[0] in ("unload", "load", "resume", "pause")] == ["unload"]
        assert any("recovery aborted (job_changed)" in r.getMessage() for r in caplog.records)

    async def test_a_reclassified_row_is_handed_over_not_aborted(
        self, db_session, printer_factory, install_settings, monkeypatch, caplog
    ):
        """The one takeover that is NOT an abort: the store upgraded this row's KIND
        under the driver. The row keeps its own lifecycle — still open, still
        ``recovering``, nothing closed, nothing stamped, nothing barred from re-entry
        — and the driver simply stops acting."""
        from backend.app.models.printer_incident import KIND_PHYSICAL, STATUS_RECOVERING

        install_settings(max_attempts=2, step_timeout_s=1.0)
        printer = await printer_factory()
        await _farm_item(db_session, printer.id)
        await _bind_spool(db_session, printer.id, 0, 0)
        _spy(monkeypatch, "on_spool_out_of_rotation")
        _spy(monkeypatch, "on_spool_recovery_failed")
        state = _make_state(trays=[_ams_tray(0), _ams_tray(1)])
        client = FakeClient(state, unload_after=2)

        def _poll(_n, st):
            cached = printer_incidents._open_cache.get(printer.id)
            if cached and ("unload",) in client.calls:
                for incident_id, payload in cached.items():
                    cached[incident_id] = {**payload, "kind": KIND_PHYSICAL}

        _wire(monkeypatch, state, client, on_poll=_poll)

        with caplog.at_level(logging.INFO, logger="backend.app.services.spool_recovery"):
            task = await on_ams_fault(printer.id, state)
            await task

        assert [c[0] for c in client.calls if c[0] in ("unload", "load", "resume", "pause")] == ["unload"]
        assert any("re-classified under the driver" in r.getMessage() for r in caplog.records)
        assert spool_recovery._blocked == {}  # a handover bars nothing from re-entry
        rows = await _incident_rows(db_session, printer.id)
        assert len(rows) == 1
        assert rows[0].resolved_at is None  # still OPEN — not ours to close
        assert rows[0].status == STATUS_RECOVERING

    async def test_the_drivers_own_verbs_are_never_a_takeover(
        self, db_session, printer_factory, install_settings, monkeypatch, caplog
    ):
        """The regression guard the predicate needs: the W1 reset moves PAUSE→RUNNING,
        the self-pause moves RUNNING→PAUSE and the swap's repause cycle does both —
        all inside the allowed pair, so a full wedge→swap recovery logs NO takeover at
        all."""
        install_settings(step_timeout_s=0.1)
        printer = await printer_factory()
        await _farm_item(db_session, printer.id)
        await _bind_spool(db_session, printer.id, 0, 0)
        _spy(monkeypatch, "on_spool_out_of_rotation")
        _spy(monkeypatch, "on_spool_recovery_succeeded")
        state = _make_state(tray_now=255, ams_status_main=1, trays=[_ams_tray(0), _ams_tray(1)])
        client = FakeClient(state)
        _wire(monkeypatch, state, client, on_poll=_repause_after_running(1))

        with caplog.at_level(logging.INFO, logger="backend.app.services.spool_recovery"):
            task = await on_ams_fault(printer.id, state)
            await task

        assert state.state == "RUNNING"  # recovered by the swap
        assert not [r for r in caplog.records if "recovery aborted (" in r.getMessage()]


class TestTheResumeVocabulary:
    """WS2: "the resume never took" and "the replacement re-jammed" are different
    statements, and only the second is evidence against a spool."""

    async def test_a_resume_that_never_took_keeps_the_replacement_in_rotation(
        self, db_session, printer_factory, install_settings, monkeypatch, caplog
    ):
        """RUNNING never arrives, twice — no fault ever re-PAUSEd anything, so there
        is no evidence against the replacement. It stays in rotation and the next
        candidate is tried."""
        install_settings()
        printer = await printer_factory()
        await _farm_item(db_session, printer.id)
        replacement = await _bind_spool(db_session, printer.id, 0, 1)
        _spy(monkeypatch, "on_spool_out_of_rotation")
        state = _make_state(trays=[_ams_tray(0), _ams_tray(1), _ams_tray(2)])
        client = FakeClient(state, resume_repauses=2)  # both resumes for tray1 are no-ops
        _wire(monkeypatch, state, client)

        with caplog.at_level(logging.INFO, logger="backend.app.services.spool_recovery"):
            task = await on_ams_fault(printer.id, state)
            await task

        assert ("load", 2) in client.calls  # moved on to the next candidate
        assert any("resume never took" in r.getMessage() for r in caplog.records)
        db_session.expunge_all()
        assert (await db_session.get(Spool, replacement.id)).feed_fault_at is None

    async def test_a_rejam_the_firmware_blames_on_the_replacement_stamps_it(
        self, db_session, printer_factory, install_settings, monkeypatch
    ):
        """The resume TAKES, then a mechanical fault naming the replacement's own slot
        re-PAUSEs the print. That IS evidence — out of rotation."""
        install_settings()
        printer = await printer_factory()
        await _farm_item(db_session, printer.id)
        replacement = await _bind_spool(db_session, printer.id, 0, 1)
        _spy(monkeypatch, "on_spool_out_of_rotation")
        state = _make_state(trays=[_ams_tray(0), _ams_tray(1), _ams_tray(2)])
        client = FakeClient(state)
        _wire(
            monkeypatch,
            state,
            client,
            on_poll=_repause_after_running(2, hms=[_feed_fault_hms(), _assist_stall_hms(0, 1)]),
        )

        task = await on_ams_fault(printer.id, state)
        await task

        assert ("load", 2) in client.calls
        db_session.expunge_all()
        assert (await db_session.get(Spool, replacement.id)).feed_fault_at is not None

    async def test_a_rejam_the_firmware_blames_on_another_slot_keeps_it_in_rotation(
        self, db_session, printer_factory, install_settings, monkeypatch, caplog
    ):
        """Same re-PAUSE, but the firmware names the ORIGINAL jammed slot. The
        replacement is not what faulted, so it stays in rotation."""
        install_settings()
        printer = await printer_factory()
        await _farm_item(db_session, printer.id)
        replacement = await _bind_spool(db_session, printer.id, 0, 1)
        _spy(monkeypatch, "on_spool_out_of_rotation")
        state = _make_state(trays=[_ams_tray(0), _ams_tray(1), _ams_tray(2)])
        client = FakeClient(state)
        _wire(
            monkeypatch,
            state,
            client,
            on_poll=_repause_after_running(2, hms=[_feed_fault_hms(), _assist_stall_hms(0, 0)]),
        )

        with caplog.at_level(logging.INFO, logger="backend.app.services.spool_recovery"):
            task = await on_ams_fault(printer.id, state)
            await task

        assert any("not the replacement" in r.getMessage() for r in caplog.records)
        db_session.expunge_all()
        assert (await db_session.get(Spool, replacement.id)).feed_fault_at is None


class TestTheUnwedgeReaderChecksTheAms:
    """WS3: every ``ok`` the reader returns is gated on the AMS having LEFT the
    filament change. "The state machine moved" was never the same statement — and a
    still-wedged reading is a MEASUREMENT the round then takes, never a give-up."""

    async def test_a_repause_that_is_still_wedged_reads_wedged(self, monkeypatch, caplog):
        """002's second wedge: the printer moved, retried, stalled and re-PAUSEd with
        ``ams_status_main`` still 1. The reader says so (``wedged``) — it is not ``ok``,
        and since 2026-09-23 it is not ``fail`` either: whether the AMS acts on the swap
        commands in that posture is what the round measures next."""
        state = _make_state(tray_now=255, ams_status_main=1)
        client = FakeClient(state, resume_unwedges=False)
        # The printer MOVES (the driver observes RUNNING), then re-PAUSEs — and the
        # AMS is still in the filament change underneath it.
        _wire(monkeypatch, state, client, on_poll=_repause_after_running(1))
        evidence = spool_recovery._RecoveryEvidence()

        with caplog.at_level(logging.INFO, logger="backend.app.services.spool_recovery"):
            verdict = await _ladder(_incident(7, step_timeout_s=0.1), client, evidence=evidence)

        assert verdict == "wedged"
        assert evidence.levers[0].lever == "resume" and evidence.levers[0].outcome == "wedged"
        lines = [r.getMessage() for r in caplog.records]
        assert any("with the AMS still mid filament-change" in m for m in lines)
        assert any("lever=resume outcome=wedged" in m for m in lines)
        assert not any("drops every" in m for m in lines)  # the retracted premise is never logged

    async def test_an_undecidable_unload_proceeds_to_the_load(
        self, db_session, printer_factory, install_settings, monkeypatch, caplog
    ):
        """The driver half (case iv): both CONTINUEs leave the AMS in the change with
        NOTHING loaded (the 009 posture). The round no longer gives up unsent — it runs
        selection, commits, and SENDS the unload. Nothing physical can answer an unload
        with nothing loaded, so the classifier says ``undecidable``; the unload WAS sent
        (invariant 8 holds) and the load is the next measurement — here it lands."""
        install_settings(step_timeout_s=0.05)
        printer = await printer_factory()
        await _farm_item(db_session, printer.id)
        await _bind_spool(db_session, printer.id, 0, 0)
        succeeded = _spy(monkeypatch, "on_spool_recovery_succeeded")
        _spy(monkeypatch, "on_spool_out_of_rotation")
        state = _make_state(tray_now=255, ams_status_main=1, trays=[_ams_tray(0), _ams_tray(1)])
        client = FakeClient(state, resume_unwedges=False, unload_deaf=True)
        _wire(monkeypatch, state, client)

        with caplog.at_level(logging.INFO, logger="backend.app.services.spool_recovery"):
            task = await on_ams_fault(printer.id, state)
            await task

        motion = [c for c in client.calls if c[0] in ("unload", "load")]
        assert motion == [("unload",), ("load", 1)]  # sent once, answered, then the load
        lines = [r.getMessage() for r in caplog.records]
        assert any(
            "command=unload" in m and "posture=mid_change_empty" in m and "answer=undecidable" in m for m in lines
        )
        assert any("command=load" in m and "answer=complete" in m for m in lines)
        assert state.state == "RUNNING"
        succeeded.assert_awaited_once()


class TestTheSecondUnwedgeTier:
    """WS6: the fork's other spelling of the firmware CONTINUE — the ladder's second
    lever, pulled only while the AMS is still in the change after the first."""

    async def test_tier2_unwedges_and_the_swap_round_runs(
        self, db_session, printer_factory, install_settings, monkeypatch
    ):
        """Tier 1 (``print.resume``) leaves the AMS wedged; tier 2
        (``ams_control("resume")``) frees it, and the proven unload -> load -> resume
        round runs to a success. Zero human touch — which is the whole point."""
        install_settings(step_timeout_s=0.05)
        printer = await printer_factory()
        item = await _farm_item(db_session, printer.id)
        jammed = await _bind_spool(db_session, printer.id, 0, 0)
        succeeded = _spy(monkeypatch, "on_spool_recovery_succeeded")
        _spy(monkeypatch, "on_spool_out_of_rotation")
        state = _make_state(tray_now=255, ams_status_main=1, trays=[_ams_tray(0), _ams_tray(1)])
        client = FakeClient(state, resume_unwedges=False, ams_control_unwedges=True)
        _wire(monkeypatch, state, client)

        task = await on_ams_fault(printer.id, state)
        await task

        assert client.calls.count(("ams_control", "resume")) == 1
        assert ("unload",) in client.calls and ("load", 1) in client.calls
        assert state.state == "RUNNING"
        succeeded.assert_awaited_once()
        db_session.expunge_all()
        assert (await db_session.get(Spool, jammed.id)).feed_fault_at is not None  # a real swap
        assert json.loads((await db_session.get(PrintQueueItem, item.id)).ams_mapping) == [1, -1, -1, -1]

    async def test_tier2_still_wedged_sends_the_swap_and_escalates_once(
        self, db_session, printer_factory, install_settings, monkeypatch, caplog
    ):
        """Both CONTINUEs left it wedged: the round SENDS the swap and measures it. The
        unload is undecidable (nothing loaded), the load goes into the change and the AMS
        does not move — ``swap_dropped_wedged``, with one escalation, one ledger row and
        one out-of-rotation stamp, and both levers on the record."""
        install_settings(step_timeout_s=0.05)
        printer = await printer_factory()
        await _farm_item(db_session, printer.id)
        jammed = await _bind_spool(db_session, printer.id, 0, 0)
        failed = _spy(monkeypatch, "on_spool_recovery_failed")
        oor = _spy(monkeypatch, "on_spool_out_of_rotation")
        _spy_ws(monkeypatch)
        state = _make_state(tray_now=255, ams_status_main=1, trays=[_ams_tray(0), _ams_tray(1)])
        client = FakeClient(state, resume_unwedges=False, unload_deaf=True, load_ok_targets=set())
        _wire(monkeypatch, state, client)

        with caplog.at_level(logging.INFO, logger="backend.app.services.spool_recovery"):
            task = await on_ams_fault(printer.id, state)
            await task

        assert client.calls.count(("ams_control", "resume")) == 1
        assert ("unload",) in client.calls and ("load", 1) in client.calls
        assert _escalated_reasons(caplog) == ["swap_dropped_wedged"]
        failed.assert_awaited_once()
        oor.assert_awaited_once()
        assert len(await _escalation_rows(db_session, printer.id)) == 1
        lines = [r.getMessage() for r in caplog.records]
        assert any("lever=resume outcome=wedged" in m for m in lines)
        assert any("lever=ams_control_resume outcome=never_moved" in m for m in lines)
        db_session.expunge_all()
        assert (await db_session.get(Spool, jammed.id)).feed_fault_at is not None

    async def test_tier1_success_never_sends_the_second_tier(
        self, db_session, printer_factory, install_settings, monkeypatch
    ):
        """A ``print.resume`` that frees the change ends it — the second verb is for
        the case the first one could not answer."""
        install_settings(step_timeout_s=0.05)
        printer = await printer_factory()
        await _farm_item(db_session, printer.id)
        await _bind_spool(db_session, printer.id, 0, 0)
        _spy(monkeypatch, "on_spool_out_of_rotation")
        state = _make_state(tray_now=255, ams_status_main=1, trays=[_ams_tray(0), _ams_tray(1)])
        client = FakeClient(state)  # the reset resume frees the change
        _wire(monkeypatch, state, client)

        task = await on_ams_fault(printer.id, state)
        await task

        assert not [c for c in client.calls if c[0] == "ams_control"]
        assert ("unload",) in client.calls and state.state == "RUNNING"

    async def test_a_failed_reset_send_never_sends_the_second_tier(self, monkeypatch):
        """Tier 1 failed because nothing went out (offline). A second publish on the
        same dead client is not a second tier — it is the same silence twice."""
        state = _make_state(tray_now=255, ams_status_main=1)
        client = FakeClient(state, resume_ret=False)
        _wire(monkeypatch, state, client)
        evidence = spool_recovery._RecoveryEvidence()

        verdict = await _ladder(_incident(7, step_timeout_s=0.05), client, evidence=evidence)

        assert verdict == "fail"
        assert not [c for c in client.calls if c[0] == "ams_control"]
        assert [(r.lever, r.outcome) for r in evidence.levers] == [("resume", "not_sent")]

    async def test_one_pull_of_each_continue_per_driver(self, monkeypatch):
        """A second wedged round in the SAME driver publishes neither CONTINUE again —
        and with nothing left to pull it reads ``wedged``, never a give-up on a budget."""
        state = _make_state(tray_now=255, ams_status_main=1)
        client = _WedgedClient(state)  # neither verb moves the state machine
        _wire(monkeypatch, state, client)
        incident = _incident(7, step_timeout_s=0.02)
        evidence = spool_recovery._RecoveryEvidence()

        assert await _ladder(incident, client, evidence=evidence) == "fail"  # no lever moved it at all (d)
        assert await _ladder(incident, client, evidence=evidence) == "wedged"  # all spent: nothing sent

        assert client.calls.count(("resume",)) == 1
        assert client.calls.count(("ams_control", "resume")) == 1


# ===========================================================================
# 012-H2S 2026-09-22 (incident 304): the wedge is MEASURED, not assumed.
#
# PAUSEd at layer 6 with `0700_0012` (AMS A slot 4 feeder motor stalled — attr lane
# only) + `0700_8006` (unable to feed into the extruder), `tray_now=3` (the jammed slot
# IS the loaded one), `ams_status_main=1 / sub=5`, three other loaded PETG slots. The
# 2026-09-11 premise "the firmware drops every load and unload in state 1" made the
# driver give up `stuck_reset_failed` 90 s later with `unloads_sent=0 loads_attempted=0`
# — three printers in four days, each recovered by a human hours later. That premise
# was measured only for a LOAD into an EMPTY path; an unload with filament LOADED behind
# a mechanical fault had never been sent. Now it is sent, and the wire's answer decides.
# ===========================================================================


def _wedge_012(**client_kw):
    """The incident-304 wire, and a FakeClient whose CONTINUEs leave the AMS in the
    change (tier 1 moves the printer and it re-pauses still wedged; tier 2 moves
    nothing) — so every case below reaches the swap commands through ``wedged``."""
    state = _make_state(
        tray_now=3,
        ams_status_main=1,
        layer=6,
        hms=[_mech_wire_hms(0, 3), _feed_into_extruder_hms()],
        trays=[_ams_tray(0), _ams_tray(1), _ams_tray(2), _ams_tray(3)],
    )
    state.ams_status_sub = 5
    client = FakeClient(state, resume_unwedges=False, **client_kw)
    return state, client


def _loads(client) -> list[int]:
    return [c[1] for c in client.calls if c[0] == "load"]


class TestTheWedgeIsMeasured:
    async def test_a_loaded_feeder_unload_completes_mid_change_and_the_swap_recovers(
        self, db_session, printer_factory, install_settings, monkeypatch, caplog
    ):
        """Case (i). The unload goes into ``mid_change_loaded`` and the feeder leaves for
        255 while the AMS STAYS in state 1 (the change the print still owes): the
        classifier's mid-change-loaded row calls that ``complete``. The load then lands,
        the resume holds — RECOVERED by a swap, zero human touch."""
        install_settings(step_timeout_s=0.05)
        printer = await printer_factory()
        item = await _farm_item(db_session, printer.id, ams_mapping="[3, -1, -1, -1]")
        await _bind_spool(db_session, printer.id, 0, 3)
        succeeded = _spy(monkeypatch, "on_spool_recovery_succeeded")
        failed = _spy(monkeypatch, "on_spool_recovery_failed")
        _spy(monkeypatch, "on_spool_out_of_rotation")
        state, client = _wedge_012(unload_keeps_change=True)
        _wire(monkeypatch, state, client)

        with caplog.at_level(logging.INFO, logger="backend.app.services.spool_recovery"):
            task = await on_ams_fault(printer.id, state)
            assert task is not None
            await task

        (target,) = _loads(client)
        assert target in (0, 1, 2)
        motion = [c for c in client.calls if c[0] in ("unload", "load")]
        assert motion == [("unload",), ("load", target)]
        lines = [r.getMessage() for r in caplog.records]
        assert any(
            "command=unload" in m and "posture=mid_change_loaded" in m and "answer=complete" in m and "→255" in m
            for m in lines
        )
        assert any(f"swapped 3 → {target}" in m for m in lines)
        assert state.state == "RUNNING"
        succeeded.assert_awaited_once()
        failed.assert_not_awaited()
        db_session.expunge_all()
        assert json.loads((await db_session.get(PrintQueueItem, item.id)).ams_mapping)[0] == target

    async def test_an_unload_the_ams_does_not_move_on_pulls_the_workable_pause_then_gives_up_measured(
        self, db_session, printer_factory, install_settings, monkeypatch, caplog
    ):
        """Case (ii). The unload goes into ``mid_change_loaded`` and nothing moves →
        ``no_movement`` → the ladder's second entry pulls tier 3 (resume, then pause on
        the first RUNNING sample — the July "workable PAUSE") → the unload is resent ONCE
        → still ``no_movement`` → ``swap_dropped_wedged``. The page carries what was sent
        and what the wire said, then where the filament is; the jammed spool is still
        loaded, so there is nothing to restore and no load is ever sent."""
        install_settings(step_timeout_s=0.05)
        printer = await printer_factory()
        await _farm_item(db_session, printer.id, ams_mapping="[3, -1, -1, -1]")
        await _bind_spool(db_session, printer.id, 0, 3)
        failed = _spy(monkeypatch, "on_spool_recovery_failed")
        _spy(monkeypatch, "on_spool_out_of_rotation")
        state, client = _wedge_012(unload_stuck=True)
        _wire(monkeypatch, state, client)

        with caplog.at_level(logging.INFO, logger="backend.app.services.spool_recovery"):
            task = await on_ams_fault(printer.id, state)
            await task

        assert _escalated_reasons(caplog) == ["swap_dropped_wedged"]
        assert client.calls.count(("unload",)) == 2  # the first, and the one resend after tier 3
        assert _loads(client) == []  # never a load behind an unload that did not move
        assert state.tray_now == 3  # the jammed spool is still loaded
        assert client.calls.count(("pause",)) == 2  # tier 1's hung self-pause, then tier 3's
        lines = [r.getMessage() for r in caplog.records]
        assert any("lever=resume_then_pause outcome=paused_wedged" in m for m in lines)
        assert not any("verdict=restore_" in m for m in lines)  # nothing to restore
        detail = failed.call_args.kwargs["detail"]
        assert detail == (
            spool_recovery._ESCALATE_DETAIL["swap_dropped_wedged"]
            + " Sent: resume (re-paused, still mid-change); ams_control resume (printer did not move); "
            "resume-then-pause (paused, still mid-change); unload ×2 (AMS did not move)."
            + " The jammed spool is still loaded (AMS A slot 4). Clear the extruder, then resume on the printer."
        )

    async def test_an_accepted_unload_then_a_dropped_load_restores_and_never_pulls_tier3(
        self, db_session, printer_factory, install_settings, monkeypatch, caplog
    ):
        """Case (iii). The unload completes (the feeder left, the AMS stayed in state 1);
        the load goes into the change and the AMS does not move → ``swap_dropped_wedged``
        at once (another candidate costs a step timeout for the same answer). The give-up
        ATTEMPTS the restore — the reload is sent and measured, ``dropped`` — and the page
        says so. NO tier 3: the extruder is one the FARM emptied (2026-09-17 ruling)."""
        install_settings(step_timeout_s=0.05)
        printer = await printer_factory()
        await _farm_item(db_session, printer.id, ams_mapping="[3, -1, -1, -1]")
        await _bind_spool(db_session, printer.id, 0, 3)
        failed = _spy(monkeypatch, "on_spool_recovery_failed")
        _spy(monkeypatch, "on_spool_out_of_rotation")
        state, client = _wedge_012(unload_keeps_change=True, load_ok_targets=set())
        _wire(monkeypatch, state, client)

        with caplog.at_level(logging.INFO, logger="backend.app.services.spool_recovery"):
            task = await on_ams_fault(printer.id, state)
            await task

        assert _escalated_reasons(caplog) == ["swap_dropped_wedged"]
        loads = _loads(client)
        assert len(loads) == 2 and loads[1] == 3  # one candidate (not resent), then the restore of slot 4
        assert client.calls.count(("resume",)) == 1  # tier 1 only — never a resume over the emptied extruder
        lines = [r.getMessage() for r in caplog.records]
        assert not any("lever=resume_then_pause" in m for m in lines)
        assert any("verdict=restore_dropped" in m for m in lines)
        detail = failed.call_args.kwargs["detail"]
        assert "unload (completed)" in detail
        assert detail.endswith(
            "No filament is loaded: AMS A slot 4 was unloaded; the reload was sent and the AMS did not move. "
            "Free the filament at the feeder, load a spool, then resume on the printer."
        )

    async def test_no_lever_is_pulled_over_an_extruder_the_farm_emptied(self, monkeypatch, caplog):
        """The 2026-09-17 ruling as a PREVENTION: while the driver's last completed motion
        is an unload, the ladder publishes nothing at either entry — tier 3 included —
        and reads ``wedged`` (the swap commands then go out and are measured)."""
        state = _make_state(tray_now=255, ams_status_main=1)
        client = FakeClient(state, resume_unwedges=False)
        _wire(monkeypatch, state, client)
        evidence = spool_recovery._RecoveryEvidence(
            commands=[
                spool_recovery.CommandRecord(
                    command="unload", target=None, posture="mid_change_loaded", answer="complete", at=0.0
                )
            ]
        )

        with caplog.at_level(logging.INFO, logger="backend.app.services.spool_recovery"):
            for entry in ("round_start", "after_unload_no_movement"):
                assert await _ladder(_incident(7, step_timeout_s=0.05), client, evidence=evidence, entry=entry) == (
                    "wedged"
                )

        assert client.calls == []  # not one resume, pause or ams_control frame
        assert evidence.levers == []
        assert sum(1 for r in caplog.records if "ladder withheld" in r.getMessage()) == 2

    async def test_tier3_first_running_sample_healthy_self_heals_without_a_pause(self, monkeypatch):
        """Case (viii). Tier 3's first RUNNING sample is already healthy (change
        completed, fault clear): the read falls into the stable-hold self-heal —
        ``recovered`` — and ``print.pause`` is never published."""
        state = _make_state(tray_now=255, ams_status_main=1)
        client = _SelfHealClient(state)
        _wire(monkeypatch, state, client)
        evidence = spool_recovery._RecoveryEvidence(
            levers=[
                spool_recovery.LeverRecord(lever="resume", outcome="wedged", at=0.0),
                spool_recovery.LeverRecord(lever="ams_control_resume", outcome="never_moved", at=0.0),
            ]
        )

        verdict = await _ladder(
            _incident(7, step_timeout_s=1.0), client, evidence=evidence, entry="after_unload_no_movement"
        )

        assert verdict == "recovered"
        assert ("pause",) not in client.calls
        assert client.calls.count(("resume",)) == 1  # tier 3's resume, nothing else
        assert (evidence.levers[-1].lever, evidence.levers[-1].outcome) == ("resume_then_pause", "recovered")

    async def test_an_operator_command_inside_a_confirm_window_is_a_takeover(
        self, db_session, printer_factory, install_settings, monkeypatch, caplog
    ):
        """Case (v). With the 409 gone an operator's Unload can land inside the driver's
        own confirm window. ``_takeover`` reads it (``operator_command``) and the driver
        stands down: the operator owns the printer, and the wire's next reading is THEIR
        command's answer, never the driver's."""
        install_settings(max_attempts=2, step_timeout_s=1.0)
        printer = await printer_factory()
        await _farm_item(db_session, printer.id)
        await _bind_spool(db_session, printer.id, 0, 0)
        _spy(monkeypatch, "on_spool_out_of_rotation")
        failed = _spy(monkeypatch, "on_spool_recovery_failed")
        state = _make_state(trays=[_ams_tray(0), _ams_tray(1)])
        client = FakeClient(state, unload_deaf=True)  # keeps the driver's window open
        fired = {"done": False}

        def _poll(_n, _st):
            if ("unload",) in client.calls and not fired["done"]:
                fired["done"] = True
                sent = ams_command.unload(printer.id, actor="operator")  # the operator's click
                assert not isinstance(sent, ams_command.Refusal)

        _wire(monkeypatch, state, client, on_poll=_poll)

        with caplog.at_level(logging.INFO, logger="backend.app.services.spool_recovery"):
            task = await on_ams_fault(printer.id, state)
            await task

        assert any("recovery aborted (operator_command)" in r.getMessage() for r in caplog.records)
        assert client.calls.count(("unload",)) == 2  # the driver's, then the operator's
        assert _loads(client) == []
        failed.assert_not_awaited()  # a takeover is never a give-up
        rows = await _incident_rows(db_session, printer.id)
        assert [r.status for r in rows] == ["aborted"]

    async def test_a_session_change_inside_a_confirm_window_aborts(
        self, db_session, printer_factory, install_settings, monkeypatch, caplog
    ):
        """Case (ix). The MQTT session the unload went out on is gone (a reconnect bumps
        ``connection_epoch``): nothing read on the new session answers that command —
        ``session_changed`` — so the driver stands down as for a lost state."""
        install_settings(max_attempts=2, step_timeout_s=1.0)
        printer = await printer_factory()
        await _farm_item(db_session, printer.id)
        await _bind_spool(db_session, printer.id, 0, 0)
        _spy(monkeypatch, "on_spool_out_of_rotation")
        failed = _spy(monkeypatch, "on_spool_recovery_failed")
        state = _make_state(trays=[_ams_tray(0), _ams_tray(1)])
        client = FakeClient(state, unload_deaf=True)
        bumped = {"done": False}

        def _poll(_n, st):
            if ("unload",) in client.calls and not bumped["done"]:
                bumped["done"] = True
                st.connection_epoch += 1  # the printer reconnected

        _wire(monkeypatch, state, client, on_poll=_poll)

        with caplog.at_level(logging.INFO, logger="backend.app.services.spool_recovery"):
            task = await on_ams_fault(printer.id, state)
            await task

        lines = [r.getMessage() for r in caplog.records]
        assert any("command=unload" in m and "answer=session_changed" in m for m in lines)
        assert any("recovery aborted (state_lost) during unload" in m for m in lines)
        assert client.calls.count(("unload",)) == 1
        assert _loads(client) == []
        failed.assert_not_awaited()
        rows = await _incident_rows(db_session, printer.id)
        assert [r.status for r in rows] == ["aborted"]


# --- WS7: an AMS physical fault is an EQUIPMENT record (2026-09-11, 003-H2S) -------


class TestPhysicalHoldsOutliveTheJob:
    """The row is the equipment-fault record; the job's terminal ends the JOB hold, never
    the fault. 003-H2S: an operator stop closed the physical row, the firmware wiped
    its HMS list at the terminal, and the scheduler dispatched the next unit onto the
    same stuck filament — three times."""

    async def _physical_hold(self, db, printer, monkeypatch):
        _spy(monkeypatch, "on_spool_recovery_failed")
        state = _make_state(hms=[_physical_wire_hms()])
        client = FakeClient(state)
        _wire(monkeypatch, state, client)
        assert await on_ams_fault(printer.id, state) is None  # escalated at entry
        row = await _incident_row(db, printer.id)
        assert (row.kind, row.status) == ("physical", "escalated")
        return state, client, row

    async def test_an_operator_stop_does_not_launder_the_hold(
        self, db_session, printer_factory, install_settings, monkeypatch
    ):
        install_settings()
        printer = await printer_factory()
        await _farm_item(db_session, printer.id)
        await self._physical_hold(db_session, printer, monkeypatch)

        # The operator's STOP, in the firmware's own word. Shape 38's pin: a stop /
        # failed / aborted terminal is very often somebody ending the very print the
        # fault broke, and it launders nothing.
        assert await spool_recovery.on_job_terminal(printer.id, _terminal(status="failed")) is False

        row = await _incident_row(db_session, printer.id)
        assert row is not None and row.resolved_at is None
        assert printer_incidents.hold_blocks_dispatch(printer.id) is True

    async def test_a_running_edge_alone_never_closes_it(
        self, db_session, printer_factory, install_settings, monkeypatch
    ):
        """An eject sweep makes the same PREPARE->RUNNING edge and moves no filament."""
        install_settings()
        printer = await printer_factory()
        await _farm_item(db_session, printer.id)
        await self._physical_hold(db_session, printer, monkeypatch)
        running = _make_state(gcode_state="RUNNING", hms=[])
        _wire(monkeypatch, running, FakeClient(running))

        assert await spool_recovery.on_observed_running(printer.id) is False
        assert (await _incident_row(db_session, printer.id)).resolved_at is None

    async def test_a_restart_onto_an_idle_printer_keeps_it(
        self, db_session, printer_factory, install_settings, monkeypatch
    ):
        """After a restart the completed-load ledger is empty by construction; a stale
        ``tray_now`` LEVEL is what a stuck-filament printer reports too."""
        install_settings()
        printer = await printer_factory()
        await _farm_item(db_session, printer.id)
        await self._physical_hold(db_session, printer, monkeypatch)
        idle = _make_state(gcode_state="IDLE", hms=[], tray_now=1)
        _wire(monkeypatch, idle, FakeClient(idle))
        incident_resolution.ledger.reset()

        assert await spool_recovery.rearm_incidents_on_startup() == 0
        assert (await _incident_row(db_session, printer.id)).resolved_at is None

    async def test_recover_closes_it_but_a_routine_clear_plate_does_not(
        self, db_session, printer_factory, install_settings, monkeypatch
    ):
        from backend.app.services import pause_recovery

        install_settings()
        printer = await printer_factory()
        await _farm_item(db_session, printer.id)
        await self._physical_hold(db_session, printer, monkeypatch)

        assert await pause_recovery.on_plate_cleared(printer.id) == []
        assert (await _incident_row(db_session, printer.id)).resolved_at is None

        assert [kind for _id, kind in await pause_recovery.on_plate_cleared(printer.id, recover=True)] == ["physical"]
        rows = await _incident_rows(db_session, printer.id)
        assert rows[0].resolved_at is not None
        assert rows[0].resolve_source == "operator"
        assert printer_incidents.hold_blocks_dispatch(printer.id) is False

    # --- the completed-after-resume evidence, end to end (2026-09-17, 011-H2S) ------
    #
    # The table's own cases live in ``test_incident_resolution.py`` and hand the ledger
    # its contents. These drive the REAL chain instead — the per-push sampler stamps the
    # ledger, ``main.on_print_complete``'s own TerminalEvent reaches the closer, and the
    # row moves in the database — because that is the join the 011-H2S incident actually
    # broke: every part worked, and nothing connected the print completing to the hold.

    async def _running_push(self, printer, state, monkeypatch):
        """Resume the printer the way production does: a PAUSE sample, then RUNNING.

        Two pushes, because the sampler is EDGE-triggered and the seed is what makes the
        second one an edge. The ledger's ``running_seen_at`` is stamped by the real
        :func:`spool_recovery.note_demand_watch` — nothing here writes it.
        """
        spawned = _capture_spawns(monkeypatch)
        spool_recovery.note_demand_watch(printer.id, state)  # seed: still PAUSEd on the fault
        state.state, state.hms_errors = "RUNNING", []  # the operator repaired it and resumed
        spool_recovery.note_demand_watch(printer.id, state)
        for coro in spawned:
            await coro  # the RUNNING-edge closer runs for real; a repair row stands
        return spawned

    async def test_the_interrupted_job_completing_closes_the_hold(
        self, db_session, printer_factory, install_settings, monkeypatch
    ):
        """THE 011-H2S chain. The operator cleared the path at the printer and resumed;
        the print then ran to ``completed``, which means filament fed through the
        repaired path to the end of that job. 23 of the 40 physical rows in this farm's
        history ended exactly this way and not one of them was ever counted."""
        install_settings()
        printer = await printer_factory()
        await _farm_item(db_session, printer.id)
        state, _client, row = await self._physical_hold(db_session, printer, monkeypatch)

        await self._running_push(printer, state, monkeypatch)
        assert incident_resolution.ledger.running_seen_at(printer.id) is not None

        assert await spool_recovery.on_job_terminal(printer.id, _terminal(job_id=row.job_id)) is True

        rows = await _incident_rows(db_session, printer.id)
        assert rows[0].resolved_at is not None
        assert rows[0].resolve_source == "repair_completed"
        # The point of the whole wave: dispatch is unblocked without a human.
        assert printer_incidents.hold_blocks_dispatch(printer.id) is False

    async def test_an_aborted_terminal_after_the_same_resume_keeps_it(
        self, db_session, printer_factory, install_settings, monkeypatch
    ):
        """Shape 38's pin, through the same chain: the print ran, and then somebody
        stopped it. A stop is not "filament fed to the end of this job"."""
        install_settings()
        printer = await printer_factory()
        await _farm_item(db_session, printer.id)
        state, _client, row = await self._physical_hold(db_session, printer, monkeypatch)
        await self._running_push(printer, state, monkeypatch)

        assert await spool_recovery.on_job_terminal(printer.id, _terminal(status="aborted")) is False

        held = await _incident_row(db_session, printer.id)
        assert held is not None and held.status == "escalated"
        assert printer_incidents.hold_blocks_dispatch(printer.id) is True

    async def test_another_jobs_completion_launders_nothing(
        self, db_session, printer_factory, install_settings, monkeypatch
    ):
        """The row blocks the DISPATCHER, not the touchscreen. A print somebody started
        from the screen, completing on another slot, must not clear a blocked shared
        path — so the terminal is bound to the job the fault interrupted."""
        install_settings()
        printer = await printer_factory()
        await _farm_item(db_session, printer.id)
        state, _client, row = await self._physical_hold(db_session, printer, monkeypatch)
        await self._running_push(printer, state, monkeypatch)

        assert await spool_recovery.on_job_terminal(printer.id, _terminal(job_id="a-screen-job")) is False

        held = await _incident_row(db_session, printer.id)
        assert held is not None and held.resolved_at is None

    async def test_a_completed_eject_sweep_launders_nothing(
        self, db_session, printer_factory, install_settings, monkeypatch
    ):
        """A sweep is filament-LESS and completes on every production cycle, so without
        the eject flag the farm's own eject would clear every physical hold it met."""
        install_settings()
        printer = await printer_factory()
        await _farm_item(db_session, printer.id)
        state, _client, row = await self._physical_hold(db_session, printer, monkeypatch)
        await self._running_push(printer, state, monkeypatch)

        assert await spool_recovery.on_job_terminal(printer.id, _terminal(eject=True)) is False

        held = await _incident_row(db_session, printer.id)
        assert held is not None and held.resolved_at is None

    async def test_a_completion_with_no_running_sighting_keeps_it(
        self, db_session, printer_factory, install_settings, monkeypatch
    ):
        """The pull-back-at-unload shape: the fault is raised while the job is ENDING,
        so the job completes with nothing ever having run through the path afterwards.
        No push is sampled here at all, which is what that looks like on the wire."""
        install_settings()
        printer = await printer_factory()
        await _farm_item(db_session, printer.id)
        state, _client, row = await self._physical_hold(db_session, printer, monkeypatch)
        # The wire goes QUIET at the terminal — the firmware wipes its standing HMS list
        # there — WITHOUT any push ever having read RUNNING. Clearing it is what isolates
        # the running-sighting qualifier: leave the fault standing and ``path_quiet``
        # refuses first, and this case would pass without testing anything (caught by
        # mutation, 2026-09-17).
        state.hms_errors = []
        assert incident_resolution.ledger.running_seen_at(printer.id) is None

        assert await spool_recovery.on_job_terminal(printer.id, _terminal(job_id=row.job_id)) is False

        held = await _incident_row(db_session, printer.id)
        assert held is not None and held.status == "escalated"
        assert printer_incidents.hold_blocks_dispatch(printer.id) is True

    async def test_a_running_eject_never_enters_the_ledger(
        self, db_session, printer_factory, install_settings, monkeypatch
    ):
        """The eject exclusion is asked at STAMP time, not only at the terminal.

        A sweep drives the printer RUNNING like any other job. If that reading entered
        the ledger, a later completion would find a "print ran through the path"
        sighting that was never a print — so the sampler asks the plate authority before
        it writes, and the evidence simply never exists.
        """
        from datetime import datetime as _dt, timezone as _tz

        from backend.app.services.plate_occupancy import EscalationOnly, PendingEject, plate_occupancy

        install_settings()
        printer = await printer_factory()
        await _farm_item(db_session, printer.id)
        state, _client, row = await self._physical_hold(db_session, printer, monkeypatch)

        plate_occupancy.hydrate_plate(printer.id, row.job_id, EscalationOnly())
        plate_occupancy.hydrate_eject(
            printer.id,
            PendingEject(
                purpose="manual",
                run_id=None,
                queue_item_id=None,
                dispatched_at=_dt.now(_tz.utc),
                started_at=None,
                hydrated=True,
            ),
        )

        await self._running_push(printer, state, monkeypatch)

        assert incident_resolution.ledger.running_seen_at(printer.id) is None, (
            "an eject sweep's RUNNING must never be recorded as a print through the path"
        )
        # ...and the completion that follows it therefore closes nothing.
        assert await spool_recovery.on_job_terminal(printer.id, _terminal(job_id=row.job_id)) is False
        held = await _incident_row(db_session, printer.id)
        assert held is not None and held.resolved_at is None

    async def test_a_later_push_upgrades_a_jam_and_the_driver_hands_over(
        self, db_session, printer_factory, install_settings, monkeypatch, caplog
    ):
        """003-H2S's second episode: ``0700_0012`` arrives first and opens a jam, the
        physical ``0700_8004`` 1.2 s later. The row is UPGRADED in place (same id,
        escalated, one page, one ledger row), and the live driver hands over before
        it stamps a spool or publishes a single unload."""
        from backend.app.models.recovery_escalation import RecoveryEscalation

        install_settings(step_timeout_s=1.0)
        printer = await printer_factory()
        await _farm_item(db_session, printer.id)
        jammed = await _bind_spool(db_session, printer.id, 0, 0)
        await _bind_spool(db_session, printer.id, 0, 1)
        failed = _spy(monkeypatch, "on_spool_recovery_failed")
        oor = _spy(monkeypatch, "on_spool_out_of_rotation")
        # RUNNING first, so the driver parks in its PAUSE wait while the second push lands.
        state = _make_state(gcode_state="RUNNING", hms=[_feed_fault_hms()], trays=[_ams_tray(0), _ams_tray(1)])
        client = FakeClient(state)
        _wire(monkeypatch, state, client)

        with caplog.at_level(logging.INFO, logger="backend.app.services.spool_recovery"):
            task = await on_ams_fault(printer.id, state)
            assert task is not None  # a jam, with a driver
            first = await _incident_row(db_session, printer.id)
            state.hms_errors = [_feed_fault_hms(), _physical_wire_hms()]  # the later push
            assert await on_ams_fault(printer.id, state) is None  # upgraded + escalated, no second driver
            state.state = "PAUSE"  # the driver wakes into a re-classified row
            await task

        db_session.expunge_all()  # the upgrade committed in the store's own session
        row = await _incident_row(db_session, printer.id)
        assert row.id == first.id  # the same equipment fault, better understood
        assert (row.kind, row.status, row.resolved_at) == ("physical", "escalated", None)
        assert [c for c in client.calls if c[0] in ("unload", "load", "resume", "pause")] == []
        oor.assert_not_awaited()
        db_session.expunge_all()
        assert (await db_session.get(Spool, jammed.id)).feed_fault_at is None
        failed.assert_awaited_once()
        assert failed.call_args.kwargs["kind"] == "physical"
        assert len((await db_session.execute(select(RecoveryEscalation))).scalars().all()) == 1
        assert spool_recovery._blocked == {}
        assert any("re-classified under the driver" in r.getMessage() for r in caplog.records)
        assert any("UPGRADED jam->physical" in r.getMessage() for r in caplog.records)

    async def test_an_escalated_jam_upgraded_later_pages_again_but_records_once(
        self, db_session, printer_factory, install_settings, monkeypatch
    ):
        """The ledger is one row per INCIDENT by derivation: the second page tells the
        operator the fault is now physical, the 2-in-24 h counter sees one row."""
        from backend.app.models.recovery_escalation import RecoveryEscalation

        install_settings(step_timeout_s=0.05, max_attempts=1)
        printer = await printer_factory()
        await _farm_item(db_session, printer.id)
        await _bind_spool(db_session, printer.id, 0, 0)
        failed = _spy(monkeypatch, "on_spool_recovery_failed")
        _spy(monkeypatch, "on_spool_out_of_rotation")
        state = _make_state(hms=[_feed_fault_hms()], trays=[_ams_tray(0)])
        client = FakeClient(state, unload_stuck=True)  # the round fails; the jam ESCALATES
        _wire(monkeypatch, state, client)
        task = await on_ams_fault(printer.id, state)
        await task
        first = await _incident_row(db_session, printer.id)
        assert (first.kind, first.status) == ("jam", "escalated")
        assert failed.await_count == 1

        state.hms_errors = [_feed_fault_hms(), _physical_wire_hms()]
        assert await on_ams_fault(printer.id, state) is None

        db_session.expunge_all()  # the upgrade committed in the store's own session
        row = await _incident_row(db_session, printer.id)
        assert (row.id, row.kind, row.status) == (first.id, "physical", "escalated")
        assert failed.await_count == 2
        assert failed.call_args.kwargs["kind"] == "physical"
        assert len((await db_session.execute(select(RecoveryEscalation))).scalars().all()) == 1

    async def test_a_repaired_path_resumes_the_paused_job_once(
        self, db_session, printer_factory, install_settings, monkeypatch
    ):
        """Doctrine rule 1: the operator freed the path and loaded a slot by hand — the
        farm publishes ONE resume, and the sweep closes the row on the same evidence
        after its dwell (one closer)."""
        from datetime import timedelta

        install_settings()
        monkeypatch.setattr(spool_recovery, "_RUNOUT_RESUME_SETTLE_S", 0.0)
        monkeypatch.setattr(spool_recovery, "_RUNOUT_RESUME_CONFIRM_S", 0.5)
        monkeypatch.setattr(spool_recovery.printer_manager, "is_connected", lambda _pid: True)
        spawned = _schedule_spawns(monkeypatch)
        printer = await printer_factory()
        await _farm_item(db_session, printer.id)
        state, client, row = await self._physical_hold(db_session, printer, monkeypatch)

        # The path is freed and a slot loads cleanly: the fault leaves the wire and the
        # sampler stamps a completed load AFTER the row opened.
        state.hms_errors = []
        _stamp_load_edge(printer.id, row.created_at + timedelta(seconds=1))

        assert await spool_recovery.sweep_open_incidents(now=0.0) == 0  # dwell seeded; the resume goes out now
        for _name, task in spawned:
            await task
        assert client.calls == [("resume",)]
        assert state.state == "RUNNING"

        assert await spool_recovery.sweep_open_incidents(now=1.0) == 0  # inside the dwell: no second resume
        for _name, task in spawned:
            await task
        assert client.calls == [("resume",)]

        assert await spool_recovery.sweep_open_incidents(now=spool_recovery._HOLD_OVER_DWELL_S + 1) == 1
        rows = await _incident_rows(db_session, printer.id)
        assert rows[0].resolve_source == "repair_observed"

    async def test_a_repaired_path_does_not_resume_a_printer_in_maintenance_mode(
        self, db_session, printer_factory, install_settings, monkeypatch
    ):
        """The held twin of the pin above — and the sharpest case for the hold.

        The evidence this lane resumes on IS the maintenance operator's own work: they
        freed the filament path and loaded a slot by hand. Without the gate, repairing a
        printer would restart its print under the hands that repaired it. Nothing is
        published, the print stays where they left it, and the physical row stays open —
        it closes the ordinary way, on its own repair rule, once they resume or the hold
        lifts.
        """
        from datetime import timedelta

        from backend.app.models.printer_incident import KIND_SERVICE_HOLD

        install_settings()
        monkeypatch.setattr(spool_recovery, "_RUNOUT_RESUME_SETTLE_S", 0.0)
        monkeypatch.setattr(spool_recovery, "_RUNOUT_RESUME_CONFIRM_S", 0.5)
        monkeypatch.setattr(spool_recovery.printer_manager, "is_connected", lambda _pid: True)
        spawned = _schedule_spawns(monkeypatch)
        printer = await printer_factory()
        await _farm_item(db_session, printer.id)
        state, client, row = await self._physical_hold(db_session, printer, monkeypatch)
        # The operator takes the printer to work on it, THEN repairs the path.
        assert await printer_incidents.open_declared(db_session, printer.id, kind=KIND_SERVICE_HOLD) is not None

        state.hms_errors = []
        _stamp_load_edge(printer.id, row.created_at + timedelta(seconds=1))

        assert await spool_recovery.sweep_open_incidents(now=0.0) == 0
        for _name, task in spawned:
            await task

        assert client.calls == [], "no resume may be published under the hands that repaired the path"
        assert state.state == "PAUSE"
        rows = await _incident_rows(db_session, printer.id)
        physical = next(r for r in rows if r.kind == "physical")
        assert physical.resolved_at is None, "the hold suppresses the ACT; the fault record stands"

    async def test_will_own_ignores_a_pause_cause_row(self, db_session, printer_factory, install_settings, monkeypatch):
        """A plate-vision hold beside a jam must not silence the jam's raw alert."""
        from backend.app.models.printer_incident import STATUS_ESCALATED

        install_settings()
        printer = await printer_factory()
        assert (
            await printer_incidents.open_new(
                db_session,
                printer_id=printer.id,
                job_id="task-1",
                item_id=None,
                kind=KIND_PLATE_VISION,
                code="0500_808C",
                codes="0500_808C",
                slot_global_tray=None,
                status=STATUS_ESCALATED,
            )
            is not None
        )
        state = _make_state(hms=[_feed_fault_hms()])

        # Nothing AMS-side owns the printer, so the predicate falls through to the
        # aborted-close bar — which is empty — and answers True (it WILL own it).
        assert await spool_recovery.will_own(db_session, printer.id, state) is True


class TestMaintenanceModeRecordsAndStandsDown:
    """An AMS fault on a printer a human has taken (2026-09-12 maintenance mode).

    The hold removes the ACT, never the RECORD: the row still opens — that is what makes
    ``hold_blocks_dispatch`` refuse work after the hold lifts, until the fault resolves by
    its own wire/repair rule — and ``will_own`` still answers True, so the duplicate raw
    HMS page stays suppressed. What must not happen is a swap, an unload, a resume or a
    driver at all, on a machine somebody has their hands in.

    And the fault keeps its own NAME. ``service_hold`` is the reason a DRIVER-BOUND fault
    takes instead of running the machine; a runout still escalates as
    ``runout_needs_refill``, which is what stamps the exhausted roll spent — that stamp is
    observation, and the ledger keeps it through a maintenance window.
    """

    async def _hold(self, db, printer_id):
        from backend.app.models.printer_incident import KIND_SERVICE_HOLD

        row = await printer_incidents.open_declared(db, printer_id, kind=KIND_SERVICE_HOLD)
        assert row is not None
        assert printer_incidents.automation_held(printer_id) is True

    async def test_a_jam_opens_an_escalated_row_and_spawns_no_driver(
        self, db_session, printer_factory, install_settings, monkeypatch
    ):
        from backend.app.models.printer_incident import KIND_JAM, STATUS_ESCALATED

        install_settings()
        printer = await printer_factory()
        await _farm_item(db_session, printer.id)
        await _bind_spool(db_session, printer.id, 0, 0)
        await self._hold(db_session, printer.id)
        state = _make_state()
        client = FakeClient(state)
        _wire(monkeypatch, state, client)

        task = await on_ams_fault(printer.id, state)

        assert task is None, "a held printer must get no recovery driver"
        assert client.calls == [], "nothing may be commanded on a printer a human holds"
        assert state.state == "PAUSE", "the print is left exactly where the operator found it"
        rows = {row.kind: row for row in await printer_incidents.open_rows(db_session, printer.id)}
        assert rows[KIND_JAM].status == STATUS_ESCALATED, "the fault is recorded, and recorded as a human's"
        # A farm mechanical jam is DRIVER-BOUND work — the swap machine is exactly what
        # the hold is refusing — so this is the fault that reads ``service_hold``.
        assert [row.reason for row in await _escalation_rows(db_session, printer.id)] == ["service_hold"]
        # The fault outlives the hold: releasing maintenance mode does not release the jam.
        assert printer_incidents.hold_blocks_dispatch(printer.id) is True

    async def test_a_runout_keeps_its_own_reason_and_still_stamps_the_roll_spent(
        self, db_session, printer_factory, install_settings, monkeypatch
    ):
        """A runout on a held printer is NOT a ``service_hold`` escalation.

        Its reason is what ``_escalate`` gates the durable spent stamp on
        (``kind == runout and reason == runout_needs_refill``), and stamping an exhausted
        roll is OBSERVATION — the ledger has to survive a maintenance window, or a roll
        that ran out during one is silently still full in the inventory. What the hold
        removes here is the driver that would have confirmed the PAUSE and the
        auto-resume that would have followed the refill.
        """
        from backend.app.models.printer_incident import KIND_RUNOUT, STATUS_ESCALATED

        install_settings()
        printer = await printer_factory()
        await _farm_item(db_session, printer.id)
        _spy(monkeypatch, "on_spool_recovery_failed")
        stamped = _spy_hold_stamp(monkeypatch)
        await self._hold(db_session, printer.id)
        state = _make_state(hms=[_runout_same_slot_hms()])
        client = FakeClient(state)
        _wire(monkeypatch, state, client)

        task = await on_ams_fault(printer.id, state)

        assert task is None, "no driver — the hold refuses the act, not the record"
        assert client.calls == []
        rows = {row.kind: row for row in await printer_incidents.open_rows(db_session, printer.id)}
        assert rows[KIND_RUNOUT].status == STATUS_ESCALATED
        assert [row.reason for row in await _escalation_rows(db_session, printer.id)] == ["runout_needs_refill"]
        # The roll is recorded exhausted, naming this printer and this job.
        assert [(pid, job) for pid, job, _st in stamped] == [(printer.id, "task-1")]

    async def test_a_refill_on_a_held_printer_does_not_resume(
        self, db_session, printer_factory, monkeypatch, _fast_resume
    ):
        """The operator refills the demanded slot while the printer is in maintenance.

        The unheld twin of this case (``test_refill_on_the_demanded_slot_resumes_once``)
        resumes the print. Here nothing is published: a print restarting while somebody
        has their hands in the machine is the surprise the hold exists to prevent. The
        hold token stays on the unit and the runout incident stays open — the operator's
        own Resume closes it on the wire, exactly as it does today.
        """
        printer = await printer_factory()
        item = await _runout_held_item(db_session, printer.id)
        resumed = _spy(monkeypatch, "on_runout_auto_resumed")
        await self._hold(db_session, printer.id)
        state = _runout_paused_state(tray_id=2)
        client = FakeClient(state)
        _wire(monkeypatch, state, client)

        assert await spool_recovery.maybe_auto_resume_on_refill(printer.id, 0, 2) is False

        assert client.calls == [], "no resume may be published onto a held printer"
        assert state.state == "PAUSE"
        resumed.assert_not_awaited()
        db_session.expunge_all()
        assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason == WAITING_REASON_RUNOUT

    async def test_the_raw_hms_page_stays_suppressed(self, db_session, printer_factory, install_settings, monkeypatch):
        """``will_own`` is unchanged by the hold. It answers "does an incident own these
        codes", and one does — the row this lane just opened."""
        install_settings()
        printer = await printer_factory()
        await _farm_item(db_session, printer.id)
        await self._hold(db_session, printer.id)
        state = _make_state()

        assert await spool_recovery.will_own(db_session, printer.id, state) is True

    async def test_the_startup_reentry_hands_back_no_driver(
        self, db_session, printer_factory, install_settings, monkeypatch
    ):
        """A restart must not give a driver back to a printer somebody is working on."""
        from backend.app.models.printer_incident import KIND_JAM, STATUS_ESCALATED, STATUS_RECOVERING

        install_settings()
        printer = await printer_factory()
        await _farm_item(db_session, printer.id)
        await _bind_spool(db_session, printer.id, 0, 0)
        row = await printer_incidents.open_new(
            db_session,
            printer_id=printer.id,
            job_id="task-1",
            item_id=None,
            kind=KIND_JAM,
            code="0700_8010",
            codes="jam:0700_8010",
            slot_global_tray=0,
            status=STATUS_RECOVERING,
        )
        assert row is not None
        await self._hold(db_session, printer.id)
        state = _make_state()
        client = FakeClient(state)
        _wire(monkeypatch, state, client)

        task = await spool_recovery._reenter_recovering_incident(row.id, printer.id)

        assert task is None
        assert client.calls == []
        db_session.expunge_all()
        assert (await db_session.get(PrinterIncident, row.id)).status == STATUS_ESCALATED

    async def test_the_page_is_suppressed_while_the_row_is_still_written(
        self, db_session, printer_factory, notification_provider_factory, install_settings, monkeypatch
    ):
        """The record and the page are INDEPENDENT channels, and the hold silences only one.

        The sibling tests spy ``on_spool_recovery_failed`` itself, which stops short of the
        gate that actually suppresses the page — ``notification_service``'s ONE fan-out. This
        one lets the real emitter run against a real provider row and watches the
        single-provider sender instead, so a change that moved the hold check upstream into
        the fault handler — making the fault vanish from BOTH channels at once — fails here.
        """
        from backend.app.models.printer_incident import KIND_JAM, STATUS_ESCALATED

        install_settings()
        await notification_provider_factory()
        printer = await printer_factory()
        await _farm_item(db_session, printer.id)
        await _bind_spool(db_session, printer.id, 0, 0)
        await self._hold(db_session, printer.id)
        from backend.app.services.notification_service import notification_service

        # A real return shape, so a regression fails on the assertion below rather than
        # on the caller unpacking a bare mock.
        sent = AsyncMock(return_value=(True, None))
        monkeypatch.setattr(notification_service, "_send_to_provider", sent)
        digest = _spy(monkeypatch, "_queue_for_digest")
        state = _make_state()
        client = FakeClient(state)
        _wire(monkeypatch, state, client)

        assert await on_ams_fault(printer.id, state) is None

        # The page: silenced in both channels — a page an operator should never have
        # received is not worth summarising tomorrow either.
        sent.assert_not_awaited()
        digest.assert_not_awaited()
        # The record: written anyway, and written as a human's.
        rows = {row.kind: row for row in await printer_incidents.open_rows(db_session, printer.id)}
        assert rows[KIND_JAM].status == STATUS_ESCALATED

    async def test_the_same_page_reaches_a_provider_when_nothing_is_held(
        self, db_session, printer_factory, notification_provider_factory, monkeypatch
    ):
        """The liveness half of the test above: its silence is a live gate refusing, not a
        provider row that was never going to be asked in the first place."""
        from backend.app.services.notification_service import notification_service

        provider = await notification_provider_factory()
        printer = await printer_factory()
        sent = AsyncMock(return_value=(True, None))
        monkeypatch.setattr(notification_service, "_send_to_provider", sent)

        await notification_service.on_spool_recovery_failed(
            printer.id, printer.name, "job-1", "ran out", db_session, kind="runout"
        )

        sent.assert_awaited_once()
        assert sent.await_args.args[0].id == provider.id


# ===========================================================================
# 004-H2S 2026-09-17, incident 192: the swap committed before it knew it could swap.
#
# 15:22:38 slot 1 ran out → the firmware auto-switched to slot 2 → 15:23:20 the
# switch-in load jammed the extruder (0300_801E) → the driver stamped spool 702 out
# of rotation and UNLOADED it → 15:23:39 selection then answered honestly that every
# remaining match was empty → ESCALATED with nothing loaded, which no copy said. At
# 15:25:52 the operator pressed Resume and the print ran 4 h on air.
#
# Two structural answers, both in this module: selection runs BEFORE the commit, and a
# driver that DID empty the extruder puts the jammed spool back before it pages.
# ===========================================================================


async def test_the_004_h2s_replay_never_commits_without_a_candidate(
    db_session, printer_factory, install_settings, monkeypatch
):
    """THE INCIDENT REPLAY. Jammed tray 0 loaded; tray 1 physically empty (state 9);
    tray 2 holding a roll the ledger prices past its label (1012.8 g used on a 1000 g
    label → below the hard recovery floor); past the protected layers.

    The verdict (`only_near_empty_spools`) is unchanged and honest — what changes is
    that it arrives with the jammed spool STILL LOADED and NOT stamped: no unload, no
    out-of-rotation, and a page that says where the filament is."""
    install_settings(protect_layers=7)
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    jammed = await _bind_spool(db_session, printer.id, 0, 0)  # spool 702's stand-in
    await _bind_spool(db_session, printer.id, 0, 2, weight_used=1012.0)  # spool 684's
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    oor = _spy(monkeypatch, "on_spool_out_of_rotation")
    state = _make_state(layer=50, trays=[_ams_tray(0), _ams_tray(1, state=9), _ams_tray(2)])
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    assert task is not None
    await task

    assert ("unload",) not in client.calls  # nothing was committed...
    assert not any(c[0] == "load" for c in client.calls)
    assert state.tray_now == 0  # ...so the jammed spool is still the feeder
    oor.assert_not_awaited()
    failed.assert_awaited_once()
    detail = failed.call_args.kwargs["detail"]
    assert "effectively empty" in detail  # the verdict itself is unchanged
    assert "The jammed spool is still loaded (AMS A slot 1)" in detail  # the missing half
    db_session.expunge_all()
    assert (await db_session.get(Spool, jammed.id)).feed_fault_at is None  # never stamped
    assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason == WAITING_REASON_FAILED
    assert (await _incident_row(db_session, printer.id)).status == "escalated"


# --- the derived reading: where is the filament, and what do we say about it ---


def _position(kind, global_tray):
    return spool_recovery.FeederPosition(kind, global_tray)


def test_feeder_position_table():
    """The ONE classifier, over the module's existing sentinel vocabulary."""
    assert spool_recovery._feeder_position(_make_state(tray_now=0), 0, None) == _position("jammed", 0)
    assert spool_recovery._feeder_position(_make_state(tray_now=255), 0, None) == _position("empty", 0)
    assert spool_recovery._feeder_position(_make_state(tray_now=3), 0, None) == _position("other", 3)
    assert spool_recovery._feeder_position(_make_state(tray_now=254), 0, None) == _position("external", None)
    # No live state, no jammed tray to be relative to, or a tray_now that names
    # neither a tray nor a sentinel: the farm says nothing rather than guessing.
    assert spool_recovery._feeder_position(None, 0, None) == _position("unknown", None)
    assert spool_recovery._feeder_position(_make_state(tray_now=0), None, None) == _position("unknown", None)
    assert spool_recovery._feeder_position(_make_state(tray_now=None), 0, None) == _position("unknown", None)
    # An AMS-HT id (128-191) is a real feeder with no letter+slot name — "other",
    # and the clause names it "tray 130", never "tray None".
    ht = spool_recovery._feeder_position(_make_state(tray_now=130), 0, None)
    assert ht == _position("other", 130)
    assert spool_recovery._feeder_clause(ht, None) == "tray 130 is loaded."


def test_feeder_position_reads_the_dual_nozzle_map_first(monkeypatch):
    """On an O1C2/H2D `tray_now` is SINGLE-valued and describes only the active hotend.

    The jammed slot feeding the DEPUTY nozzle would read "empty" off `tray_now` alone,
    and the give-up would then load on top of filament that never left — so the
    per-extruder map answers first, exactly as `slot_was_feeding` orders its witnesses."""

    class _DualNozzle:
        is_dual_nozzle = True

    monkeypatch.setattr(spool_recovery.printer_manager, "get_client", lambda _pid: _DualNozzle())
    state = _make_state(tray_now=255)
    state.h2d_extruder_snow = {0: 255, 1: 3}  # the deputy hotend is fed from tray 3

    position = spool_recovery._feeder_position(state, 3, 7)

    assert position == _position("jammed", 3)
    assert "The jammed spool is still loaded (AMS A slot 4)" in spool_recovery._feeder_clause(position, None)
    # A hotend fed from a tray that is NOT the jammed one is "other", never "empty".
    assert spool_recovery._feeder_position(state, 0, 7) == _position("other", 3)


def test_feeder_clause_table():
    """The sentence per (position, restore verdict). `restore` is the DRIVER's own
    statement about an extruder it emptied — never inferred from the wire."""
    jammed = _position("jammed", 0)
    empty = _position("empty", 0)

    assert spool_recovery._feeder_clause(jammed, None) == (
        "The jammed spool is still loaded (AMS A slot 1). Clear the extruder, then resume on the printer."
    )
    assert spool_recovery._feeder_clause(jammed, "ok") == (
        "The jammed spool was unloaded and reloaded (AMS A slot 1). Clear the extruder, then resume on the printer."
    )
    assert spool_recovery._feeder_clause(empty, "fail") == (
        "No filament is loaded: AMS A slot 1 was unloaded and the reload failed. "
        "Check the filament path, load a spool, then resume on the printer."
    )
    assert spool_recovery._feeder_clause(empty, "dropped") == (
        "No filament is loaded: AMS A slot 1 was unloaded; the reload was sent and the AMS did not move. "
        "Free the filament at the feeder, load a spool, then resume on the printer."
    )
    assert spool_recovery._feeder_clause(empty, "skipped_drying") == (
        "No filament is loaded: AMS A slot 1 was unloaded; the AMS is drying, so no reload was attempted. "
        "Load a spool after the cycle, then resume on the printer."
    )
    # 255 with no restore verdict: the farm did not empty it and cannot say why it
    # reads empty (invariant 8 — 255 is "nothing is feeding", not "the path is clear").
    assert spool_recovery._feeder_clause(empty, None) is None
    assert spool_recovery._feeder_clause(_position("other", 1), None) == "AMS A slot 2 is loaded."
    # No AMS slot to name, or nothing to be relative to: no clause at any verdict.
    for kind in ("external", "unknown"):
        for restore in (None, "ok", "fail", "dropped", "skipped_drying"):
            assert spool_recovery._feeder_clause(_position(kind, None), restore) is None


async def test_a_runout_escalation_carries_no_feeder_clause(db_session, printer_factory, monkeypatch):
    """Kind gate: only the machine that MOVES filament says where it is. A runout's
    copy already names the slot to refill, and nothing unloaded it."""
    from backend.app.models.printer_incident import KIND_RUNOUT

    printer = await printer_factory()
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    state = _make_state(tray_now=255)
    _wire(monkeypatch, state, FakeClient(state))
    incident = await _owned_incident(db_session, printer.id, kind=KIND_RUNOUT, step_timeout_s=0.05)

    await spool_recovery._escalate(incident, "external_spool_runout")

    assert failed.call_args.kwargs["detail"] == spool_recovery._ESCALATE_DETAIL["external_spool_runout"]


async def test_the_slot_label_has_one_origin(db_session, printer_factory, monkeypatch):
    """`runout_slot_desc` names every slot this module renders. Global tray 1 is the
    slot the firmware, the incident chip and the escalation all call "AMS A slot 2";
    the out-of-rotation page used to call it "AMS0 slot 1" (004-H2S incident 192)."""
    printer = await printer_factory()
    oor = _spy(monkeypatch, "on_spool_out_of_rotation")
    _spy_ws(monkeypatch)
    incident = _incident(printer.id, step_timeout_s=0.05)

    await spool_recovery._mark_out_of_rotation(incident, 1)

    oor.assert_awaited_once()
    assert oor.call_args.kwargs["slot_desc"] == "AMS A slot 2"
    # The notification describer's bare-slot fallback (no spool bound) renders the same.
    assert await spool_recovery._describe_slot(db_session, printer.id, 1) == "AMS A slot 2"
    assert await spool_recovery._describe_slot(db_session, printer.id, 130) == "tray 130"


# --- the give-up boundary: restore what the swap moved, then page ------------


async def test_the_give_up_restores_the_jammed_feeder(
    db_session, printer_factory, install_settings, monkeypatch, caplog
):
    """Every candidate failed to load and the round after them found none left — the
    residual shape in which the extruder is empty at a give-up. The driver puts the
    jammed spool back BEFORE it pages, so no resume can print air.

    The stamp stays (it did jam; the flag only bars auto-selection) and the escalation
    reason is untouched — the restore is not a candidate and buys no evidence."""
    install_settings(max_attempts=2, step_timeout_s=0.05)
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    jammed = await _bind_spool(db_session, printer.id, 0, 0)
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    _spy(monkeypatch, "on_spool_out_of_rotation")
    state = _make_state(trays=[_ams_tray(0), _ams_tray(1), _ams_tray(2)])
    client = FakeClient(state, load_ok_targets={0})  # only the jammed tray will load
    _wire(monkeypatch, state, client)

    with caplog.at_level(logging.INFO, logger="backend.app.services.spool_recovery"):
        task = await on_ams_fault(printer.id, state)
        await task

    loads = [c for c in client.calls if c[0] == "load"]
    assert loads == [("load", 1), ("load", 1), ("load", 2), ("load", 2), ("load", 0)]
    assert state.tray_now == 0  # the jammed spool is back on the feeder
    assert _escalated_reasons(caplog) == ["feed_path_blocked"]  # evidence unchanged
    assert "The jammed spool was unloaded and reloaded (AMS A slot 1)" in failed.call_args.kwargs["detail"]
    assert any("verdict=restore_ok" in r.getMessage() for r in caplog.records)
    db_session.expunge_all()
    assert (await db_session.get(Spool, jammed.id)).feed_fault_at is not None  # the stamp STAYS


@pytest.mark.parametrize(
    "client_kw,verdict,clause",
    [
        # The reload went out and the AMS did not move: ``no_movement`` → ``dropped``.
        (
            {"load_ok_targets": set()},
            "dropped",
            "No filament is loaded: AMS A slot 1 was unloaded; the reload was sent and the AMS did not move.",
        ),
        # The reload never went out (every publish refused): ``fail``.
        ({"load_ret": False}, "fail", "No filament is loaded: AMS A slot 1 was unloaded and the reload failed."),
    ],
)
async def test_the_give_up_pages_a_restore_that_did_not_land(
    db_session, printer_factory, install_settings, monkeypatch, caplog, client_kw, verdict, clause
):
    """The odd case is PAGED, not machined: the reload was attempted and did not land,
    so the printer really is empty — and the page says what the wire answered."""
    install_settings(max_attempts=2, step_timeout_s=0.05)
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    await _bind_spool(db_session, printer.id, 0, 0)
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    _spy(monkeypatch, "on_spool_out_of_rotation")
    state = _make_state(trays=[_ams_tray(0), _ams_tray(1), _ams_tray(2)])
    client = FakeClient(state, **client_kw)  # nothing loads, not even the restore
    _wire(monkeypatch, state, client)

    with caplog.at_level(logging.INFO, logger="backend.app.services.spool_recovery"):
        task = await on_ams_fault(printer.id, state)
        await task

    assert client.calls.count(("load", 0)) == 2  # both attempts spent on the restore
    assert state.tray_now == 255  # ...and nothing is loaded
    assert _escalated_reasons(caplog) == ["feed_path_blocked"]  # the reason is unchanged
    assert clause in failed.call_args.kwargs["detail"]
    assert any(f"verdict=restore_{verdict}" in r.getMessage() for r in caplog.records)


async def test_a_takeover_during_the_restore_keeps_the_stamp(
    db_session, printer_factory, install_settings, monkeypatch
):
    """The operator resumes mid-confirm, on the feeder the FARM just restored.

    `_clear_oor_if_resumed_on_jammed_feeder` reads a resume on the jammed feeder as
    "the operator declared this spool usable" — true when THEY chose the feeder
    (test_abort_clears_oor_when_resumed_on_jammed_feeder), false here, because the
    driver put it there. The abort keeps the stamp."""
    install_settings(max_attempts=2, step_timeout_s=0.05)
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    jammed = await _bind_spool(db_session, printer.id, 0, 0)
    _spy(monkeypatch, "on_spool_out_of_rotation")
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    state = _make_state(trays=[_ams_tray(0), _ams_tray(1)])
    client = FakeClient(state, load_ok_targets={0})

    def _poll(_n, st):
        if ("load", 0) in client.calls and st.tray_now == 0:
            st.state = "RUNNING"  # 15:25:52 — the operator pressed Resume

    _wire(monkeypatch, state, client, on_poll=_poll)

    task = await on_ams_fault(printer.id, state)
    await task

    assert ("load", 0) in client.calls  # the restore went out
    failed.assert_not_awaited()  # a takeover is never a give-up — no page
    db_session.expunge_all()
    rows = await _incident_rows(db_session, printer.id)
    assert [r.status for r in rows] == ["aborted"]
    assert (await db_session.get(Spool, jammed.id)).feed_fault_at is not None  # stamp kept


async def test_a_wedged_ams_gets_the_restore_sent_and_measured(
    db_session, printer_factory, install_settings, monkeypatch, caplog
):
    """`ams_status_main == 1` under the driver, after the farm emptied the extruder: the
    reload is SENT anyway and its answer decides the page — here the AMS does not move,
    so the restore reads ``dropped`` and the page says the reload went out and did not
    land. Pre-2026-09-23 this refused the reload outright on "the firmware drops every
    load", measured only for a load into an empty path. And no resume is published over
    the extruder the farm emptied: the ladder is withheld (2026-09-17 ruling)."""
    install_settings(max_attempts=2, step_timeout_s=0.05)
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    await _bind_spool(db_session, printer.id, 0, 0)
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    _spy(monkeypatch, "on_spool_out_of_rotation")
    state = _make_state(trays=[_ams_tray(0), _ams_tray(1)])
    client = FakeClient(state, load_ok_targets=set(), resume_unwedges=False)

    def _poll(_n, st):
        if client.calls.count(("load", 1)) >= 2:
            st.ams_status_main = 1  # the AMS wedges mid-change under the driver

    _wire(monkeypatch, state, client, on_poll=_poll)

    with caplog.at_level(logging.INFO, logger="backend.app.services.spool_recovery"):
        task = await on_ams_fault(printer.id, state)
        await task

    assert client.calls.count(("load", 0)) == 1  # the restore went out, once: no resend into the change
    assert ("resume",) not in client.calls  # no CONTINUE over the extruder the farm emptied
    assert _escalated_reasons(caplog) == ["feed_path_blocked"]
    lines = [r.getMessage() for r in caplog.records]
    assert any("ladder withheld" in m for m in lines)
    assert any("verdict=restore_dropped" in m for m in lines)
    assert (
        "No filament is loaded: AMS A slot 1 was unloaded; the reload was sent and the AMS did not move."
        in failed.call_args.kwargs["detail"]
    )


async def test_a_drying_ams_at_the_unload_refuses_the_restore(
    db_session, printer_factory, install_settings, monkeypatch, caplog
):
    """The drying lockout arrives between rounds: round 2's unload pre-flight answers
    `drying`, so the round gives up — and the restore's own pre-flight answers the same,
    because it is the SAME `_load_and_confirm` the swap uses."""
    install_settings(max_attempts=2, step_timeout_s=0.05)
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    await _bind_spool(db_session, printer.id, 0, 0)
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    _spy(monkeypatch, "on_spool_out_of_rotation")
    state = _make_state(trays=[_ams_tray(0), _ams_tray(1), _ams_tray(2)])
    client = FakeClient(state, load_ok_targets=set())

    def _poll(_n, _st):
        if client.calls.count(("load", 1)) >= 1:
            client.write_refusal = "drying"  # the AMS starts a dry cycle mid-recovery

    _wire(monkeypatch, state, client, on_poll=_poll)

    with caplog.at_level(logging.INFO, logger="backend.app.services.spool_recovery"):
        task = await on_ams_fault(printer.id, state)
        await task

    assert client.calls.count(("unload",)) == 1  # round 2 never wrote to a drying AMS
    assert ("load", 2) not in client.calls and ("load", 0) not in client.calls
    assert _escalated_reasons(caplog) == ["ams_drying"]
    assert any("verdict=restore_skipped_drying" in r.getMessage() for r in caplog.records)
    assert "the AMS is drying, so no reload was attempted" in failed.call_args.kwargs["detail"]


async def test_a_drying_ams_between_the_unload_and_the_load_refuses_the_restore(
    db_session, printer_factory, install_settings, monkeypatch, caplog
):
    """The other drying site: the unload confirmed and the dry cycle started before the
    replacement load. The extruder IS empty (the driver emptied it) and no reload can be
    commanded until the cycle ends — the page states both."""
    install_settings(max_attempts=2, step_timeout_s=0.05)
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    await _bind_spool(db_session, printer.id, 0, 0)
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    _spy(monkeypatch, "on_spool_out_of_rotation")
    state = _make_state(trays=[_ams_tray(0), _ams_tray(1)])
    client = FakeClient(state)

    def _poll(_n, _st):
        if ("unload",) in client.calls:
            client.write_refusal = "drying"

    _wire(monkeypatch, state, client, on_poll=_poll)

    with caplog.at_level(logging.INFO, logger="backend.app.services.spool_recovery"):
        task = await on_ams_fault(printer.id, state)
        await task

    assert ("unload",) in client.calls  # the unload landed...
    assert not any(c[0] == "load" for c in client.calls)  # ...and no load followed it
    assert _escalated_reasons(caplog) == ["ams_drying"]
    assert any("verdict=restore_skipped_drying" in r.getMessage() for r in caplog.records)
    assert "the AMS is drying, so no reload was attempted" in failed.call_args.kwargs["detail"]


@pytest.mark.parametrize("ams_main", [3, 1])
async def test_the_restore_is_attempted_in_every_posture(db_session, printer_factory, monkeypatch, caplog, ams_main):
    """The reload is SENT whatever ``ams_status_main`` reads — assist (3, the steady
    state of every RUNNING H2S) and mid filament-change (1) alike — and the wire's answer
    decides the verdict. The wedge refusal this replaces rested on an unmeasured premise."""
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    _spy(monkeypatch, "on_spool_recovery_failed")
    state = _make_state(tray_now=255, ams_status_main=ams_main)
    client = FakeClient(state, load_ret=False)  # a publish that does not go out: no confirm wait
    _wire(monkeypatch, state, client)
    incident = await _owned_incident(db_session, printer.id, step_timeout_s=0.05, item_id=item.id)

    with caplog.at_level(logging.INFO, logger="backend.app.services.spool_recovery"):
        await spool_recovery._give_up(
            incident,
            client,
            "feed_path_blocked",
            evidence=spool_recovery._RecoveryEvidence(loads_attempted=1, commands=[_unload_record()]),
        )

    assert ("load", 0) in client.calls
    assert any("verdict=restore_fail" in r.getMessage() for r in caplog.records)


async def test_a_firmware_wedge_with_no_candidate_is_left_alone(
    db_session, printer_factory, install_settings, monkeypatch, caplog
):
    """Round 1 meets an AMS the FIRMWARE wedged mid-change (`ams_status_main=1`, and
    `tray_now` already 255 after the fault); the CONTINUEs do not free it, and there is
    nothing to swap to.

    ``wedged`` proceeds to selection like ``ok`` — and with no candidate there is no
    commit: no stamp, no unload, no restore (shape 39). The page is the reason copy plus
    what the farm sent; the wire alone could not tell this 255 from an extruder the
    driver emptied, so no clause claims an unload."""
    install_settings(step_timeout_s=0.05)
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    jammed = await _bind_spool(db_session, printer.id, 0, 0)
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    oor = _spy(monkeypatch, "on_spool_out_of_rotation")
    state = _make_state(tray_now=255, ams_status_main=1, trays=[_ams_tray(0)])
    client = FakeClient(state, resume_unwedges=False)
    _wire(monkeypatch, state, client, on_poll=_repause_after_running(1))

    with caplog.at_level(logging.INFO, logger="backend.app.services.spool_recovery"):
        task = await on_ams_fault(printer.id, state)
        await task

    assert ("unload",) not in client.calls  # nothing to load → nothing to unload
    assert not any(c[0] == "load" for c in client.calls)
    assert _escalated_reasons(caplog) == ["no_eligible_spool"]
    assert failed.call_args.kwargs["detail"] == (
        spool_recovery._ESCALATE_DETAIL["no_eligible_spool"]
        + " Sent: resume (re-paused, still mid-change); ams_control resume (printer did not move)."
    )
    assert any("restore=None" in r.getMessage() for r in caplog.records)
    oor.assert_not_awaited()  # no replacement in hand → the swap never committed
    db_session.expunge_all()
    assert (await db_session.get(Spool, jammed.id)).feed_fault_at is None


async def test_a_firmware_wedge_with_a_candidate_sends_the_swap_and_reads_swap_dropped_wedged(
    db_session, printer_factory, install_settings, monkeypatch, caplog
):
    """The same firmware wedge with a replacement in hand. Pre-2026-09-23 this gave up
    ``stuck_reset_failed`` with nothing sent. Now the swap commits and the commands go
    out: the unload is ``undecidable`` (nothing loaded), the load goes into the change
    and the AMS does not move → ``swap_dropped_wedged``. The farm DID publish an unload,
    so the give-up attempts the restore — sent, measured, ``dropped``."""
    install_settings(step_timeout_s=0.05)
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    jammed = await _bind_spool(db_session, printer.id, 0, 0)
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    oor = _spy(monkeypatch, "on_spool_out_of_rotation")
    state = _make_state(tray_now=255, ams_status_main=1, trays=[_ams_tray(0), _ams_tray(1)])
    client = FakeClient(state, resume_unwedges=False, unload_deaf=True, load_ok_targets=set())
    _wire(monkeypatch, state, client, on_poll=_repause_after_running(1))

    with caplog.at_level(logging.INFO, logger="backend.app.services.spool_recovery"):
        task = await on_ams_fault(printer.id, state)
        await task

    assert [c for c in client.calls if c[0] in ("unload", "load")] == [("unload",), ("load", 1), ("load", 0)]
    assert _escalated_reasons(caplog) == ["swap_dropped_wedged"]
    detail = failed.call_args.kwargs["detail"]
    assert detail.startswith(spool_recovery._ESCALATE_DETAIL["swap_dropped_wedged"])
    assert (
        "unload (nothing was loaded); load AMS A slot 2 (AMS did not move); load AMS A slot 1 (AMS did not move)."
        in detail
    )
    assert detail.endswith(
        "No filament is loaded: AMS A slot 1 was unloaded; the reload was sent and the AMS did not move. "
        "Free the filament at the feeder, load a spool, then resume on the printer."
    )
    oor.assert_awaited_once()  # the swap committed with a replacement in hand
    db_session.expunge_all()
    assert (await db_session.get(Spool, jammed.id)).feed_fault_at is not None


async def test_a_no_candidate_give_up_at_255_never_loads_what_it_did_not_unload(
    db_session, printer_factory, install_settings, monkeypatch, caplog
):
    """The firmware retracted after the fault, so `tray_now` reads 255 before the farm
    touches anything — and no replacement exists to swap to.

    Nothing is committed and nothing is restored: a load with no explicit unload behind
    it is the one a post-fault AMS drops (invariant 8), and the printer is left exactly
    as the firmware left it."""
    install_settings()
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    jammed = await _bind_spool(db_session, printer.id, 0, 0)
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    oor = _spy(monkeypatch, "on_spool_out_of_rotation")
    # 255 with the feed fault STANDING is not the clean restart state (_unload_skippable
    # needs a quiet wire), and tray 0 is the only loaded tray.
    state = _make_state(tray_now=255, ams_status_main=0, trays=[_ams_tray(0)])
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    with caplog.at_level(logging.INFO, logger="backend.app.services.spool_recovery"):
        task = await on_ams_fault(printer.id, state)
        await task

    assert ("unload",) not in client.calls
    assert not any(c[0] == "load" for c in client.calls)
    oor.assert_not_awaited()
    assert _escalated_reasons(caplog) == ["no_eligible_spool"]
    assert failed.call_args.kwargs["detail"] == spool_recovery._ESCALATE_DETAIL["no_eligible_spool"]
    db_session.expunge_all()
    assert (await db_session.get(Spool, jammed.id)).feed_fault_at is None


async def test_an_unconfirmed_unload_still_counts_as_the_farm_moving_the_feeder(
    db_session, printer_factory, install_settings, monkeypatch, caplog
):
    """`unloads_sent` counts the COMMAND, not the confirmation: the first unload moved
    the AMS into the change and never completed (``acted``), the resend went into it with
    nothing loaded (``undecidable``) — neither is ``complete``, both went out, so the farm
    DID move this feeder and the give-up owes the operator the restore and a statement
    about it. The load into the change did not move: ``swap_dropped_wedged``."""
    install_settings(max_attempts=2, step_timeout_s=0.05)
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    await _bind_spool(db_session, printer.id, 0, 0)
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    _spy(monkeypatch, "on_spool_out_of_rotation")
    state = _make_state(tray_now=255, ams_status_main=0, trays=[_ams_tray(0), _ams_tray(1)])
    client = FakeClient(state, unload_stuck=True, resume_unwedges=False, load_ok_targets=set())
    _wire(monkeypatch, state, client)

    with caplog.at_level(logging.INFO, logger="backend.app.services.spool_recovery"):
        task = await on_ams_fault(printer.id, state)
        await task

    assert client.calls.count(("unload",)) == 2  # both attempts published, neither complete
    assert _escalated_reasons(caplog) == ["swap_dropped_wedged"]
    lines = [r.getMessage() for r in caplog.records]
    assert any("unloads_sent=2 unloads_confirmed=0" in m for m in lines)
    assert any("verdict=restore_dropped" in m for m in lines)
    assert ("load", 0) in client.calls  # the restore was sent into the change, and measured
    assert (
        "No filament is loaded: AMS A slot 1 was unloaded; the reload was sent and the AMS did not move."
        in failed.call_args.kwargs["detail"]
    )


# --- causality and single-origin pins ---------------------------------------


async def test_the_clause_never_claims_an_unload_the_driver_did_not_do(
    db_session, printer_factory, monkeypatch, caplog
):
    """After a feed fault `tray_now` frequently already reads 255 BEFORE the farm does
    anything, and five jam reasons escalate straight from the entry gate with nothing
    unloaded. The clause therefore reads the driver's own restore verdict, never the
    wire: no verdict, no claim about an unload."""
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    state = _make_state(tray_now=255)
    client = FakeClient(state, load_ret=False)
    _wire(monkeypatch, state, client)

    for reason in ("multi_feeder_job", "jammed_tray_unresolved"):
        incident = await _owned_incident(db_session, printer.id, step_timeout_s=0.05, item_id=item.id)
        await spool_recovery._escalate(incident, reason)
        detail = failed.call_args.kwargs["detail"]
        assert detail == spool_recovery._ESCALATE_DETAIL[reason]
        assert "was unloaded" not in detail
        await _close_row(db_session, printer.id)

    # The same wire state, after the driver's OWN completed unload and a refused
    # reload, does carry it.
    incident = await _owned_incident(db_session, printer.id, step_timeout_s=0.05, item_id=item.id)
    with caplog.at_level(logging.INFO, logger="backend.app.services.spool_recovery"):
        await spool_recovery._give_up(
            incident,
            client,
            "feed_path_blocked",
            evidence=spool_recovery._RecoveryEvidence(loads_attempted=1, commands=[_unload_record()]),
        )
    assert "AMS A slot 1 was unloaded and the reload failed" in failed.call_args.kwargs["detail"]


async def test_the_stamp_lands_after_selection_and_before_the_unload(
    db_session, printer_factory, install_settings, monkeypatch
):
    """Invariant 7's boundary, in order, recorded from the stamp WRITER itself.

    Driving the WRITER rather than the page keeps this pin about ORDER: the page is now
    unconditional inside the stamp (the `notify` switch nobody ever threw is deleted),
    so a spy on the notification would test the same thing one layer further away and
    would stop distinguishing "stamped late" from "stamped and not announced".

    The swap-commit boundary is now "a replacement is in hand": selection, then the
    out-of-rotation stamp, then the first unload — exactly once."""
    install_settings()
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    await _bind_spool(db_session, printer.id, 0, 0)
    _spy(monkeypatch, "on_spool_out_of_rotation")
    state = _make_state()
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    select_orig = spool_recovery._select_replacement
    oor_orig = spool_recovery._mark_out_of_rotation

    async def _select(incident, tried):
        picked = await select_orig(incident, tried)
        client.calls.append(("select", picked[0]))
        return picked

    async def _oor(incident, global_tray):
        client.calls.append(("oor",))
        await oor_orig(incident, global_tray)

    monkeypatch.setattr(spool_recovery, "_select_replacement", _select)
    monkeypatch.setattr(spool_recovery, "_mark_out_of_rotation", _oor)

    task = await on_ams_fault(printer.id, state)
    await task

    assert client.calls.count(("oor",)) == 1
    assert client.calls.index(("select", 1)) < client.calls.index(("oor",)) < client.calls.index(("unload",))


async def test_the_clause_follows_the_classifier_not_tray_now(db_session, printer_factory, monkeypatch):
    """`_feeder_position` is the ONE reading behind the clause: monkeypatch it and the
    escalation follows it even though `tray_now` says the opposite."""
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    state = _make_state(tray_now=0)  # the wire says the jammed tray is feeding
    _wire(monkeypatch, state, FakeClient(state))
    monkeypatch.setattr(spool_recovery, "_feeder_position", lambda *_a, **_k: _position("empty", 0))
    incident = await _owned_incident(db_session, printer.id, step_timeout_s=0.05, item_id=item.id)

    await spool_recovery._escalate(incident, "feed_path_blocked", restore="fail")

    assert (
        "No filament is loaded: AMS A slot 1 was unloaded and the reload failed" in (failed.call_args.kwargs["detail"])
    )


@pytest.mark.parametrize("kind,expect_load", [("empty", True), ("jammed", False)])
async def test_the_restore_follows_the_classifier_not_tray_now(
    db_session, printer_factory, monkeypatch, kind, expect_load
):
    """Same pin on the other projection: the restore decision reads the classifier, so
    a `tray_now` that disagrees changes nothing."""
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    # The wire is set to the OPPOSITE of the classifier's verdict in both cases.
    state = _make_state(tray_now=255 if kind == "jammed" else 0)
    client = FakeClient(state, load_ret=False)
    _wire(monkeypatch, state, client)
    monkeypatch.setattr(spool_recovery, "_feeder_position", lambda *_a, **_k: _position(kind, 0))
    incident = await _owned_incident(db_session, printer.id, step_timeout_s=0.05, item_id=item.id)

    verdict = await spool_recovery._restore_jammed_feeder(incident, client, evidence=spool_recovery._RecoveryEvidence())

    assert (("load", 0) in client.calls) is expect_load
    assert verdict == ("fail" if expect_load else None)


def _module_ast():
    return ast.parse(inspect.getsource(spool_recovery))


def _function_node(name):
    for node in ast.walk(_module_ast()):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in spool_recovery")


def test_every_give_up_inside_the_loop_goes_through_one_boundary():
    """ONE give-up boundary, not six inline conditions: every `_escalate` reachable in
    the candidate loop and after it routes through `_give_up`, which decides the restore
    once. The runout branch escalates directly and is the ONLY direct call left — it
    unloads nothing, so it has nothing to restore."""
    run_recovery = _function_node("_run_recovery")
    escalates = sorted(
        n.lineno
        for n in ast.walk(run_recovery)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_escalate"
    )
    give_ups = sorted(
        n.lineno
        for n in ast.walk(run_recovery)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_give_up"
    )

    assert len(escalates) == 1, f"only the runout branch may escalate directly, found {len(escalates)}"
    assert give_ups, "the loop escalates through _give_up"
    assert min(give_ups) > escalates[0], "no direct _escalate after the runout branch"
    # The eight sites: the ladder's ``fail`` at each of its two entries, no candidate,
    # an unload that never completed, unload drying, a load the wedged AMS did not move
    # on, load drying, post-loop exhaustion.
    assert len(give_ups) == 8


def test_no_second_slot_rendering_in_the_module():
    """One origin for the human slot name. The owner is `printer_incidents.runout_slot_desc`
    (the incident-resolution wave moved it there, 2026-09-17), so NO f-string in this module
    may spell a slot — three 0-indexed renderings here were the defect this wave closed."""
    module = _module_ast()
    offenders = [
        ast.unparse(n) for n in ast.walk(module) if isinstance(n, ast.JoinedStr) and "slot {" in ast.unparse(n)
    ]

    assert offenders == [], f"slot names must render through runout_slot_desc: {offenders}"


def test_only_the_commit_verb_parks_a_spool():
    """ONE home for "an extruder-side fault never parks a spool" (006-H2S 2026-09-21,
    incident 289).

    The rule is not new — the driver has applied it to the REPLACEMENT since WS2 — but
    it lived at that one call site, so the three OTHER call sites of the stamp writer
    drew the opposite conclusion from the same fault and parked a healthy roll. A
    SOURCE pin, because a fourth direct call would be a perfectly well-formed stamp
    that no behaviour test looks for."""
    module = _module_ast()
    verb = _function_node("_commit_out_of_rotation")
    inside = range(verb.lineno, (verb.end_lineno or verb.lineno) + 1)
    strays = sorted(
        n.lineno
        for n in ast.walk(module)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_mark_out_of_rotation"
        and n.lineno not in inside
    )

    assert strays == [], f"only _commit_out_of_rotation may park a spool; direct writer calls at {strays}"
    # Liveness: a pin that finds no strays is also satisfied by a verb that stopped
    # calling the writer at all.
    assert any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_mark_out_of_rotation"
        for n in ast.walk(verb)
    )


# ===========================================================================
# 006-H2S 2026-09-21, incident 289 — the extruder overload, the farm's own unload,
# and the pull-back it latched.
#
# 04:57:34 `0300_801E` + `0300_0001@0x09` (extruder overload, the only fault standing)
# 04:57:35 incident opened kind=jam; the driver committed the swap and unloaded
# 04:58:30 `0700_8003` "Failed to pull out the filament from the extruder" + the
#          slot-attributed `0700_0017@0-0` — the failure of that unload, +56 s
# 04:58:31 row UPGRADED jam->physical, the driver handed over, PAUSED ~11.5 h
#
# Three defects, three fixes, all pinned below: a healthy roll was parked by an
# extruder-side fault; the upgrade dropped the slot the mechanical sibling named; and
# the page said "then resume" on a screen whose only button repeats the pull-back.
# ===========================================================================


def _tube_stall_hms(ams_id=0, tray_id=0):
    """0700_0017@0-0 — "AMS A slot 1 assist motor is stalled, due to excessive
    resistance in the tube between AMS and the printer". MECHANICAL_FEED, and
    slot-attributed: the only entry in the 006 set that names a tray at all."""
    attr = 0x07000000 | (ams_id << 16) | ((0x20 + tray_id) << 8)
    return HMSError(code="0x20017", attr=attr, module=7, severity=2, full_code=f"{attr:08X}00020017")


def _006_candidates():
    """The candidate set standing at 04:58:30, through the real classifier."""
    from backend.app.services.hms_errors import live_candidates

    return live_candidates(_make_state(hms=[_tube_stall_hms(), _physical_short_hms()]))


def _settings_for_factory():
    return spool_recovery.RecoverySettings(enabled=True, max_attempts=2, step_timeout_s=1.0, protect_layers=7)


def _built(candidates, *, kind, code):
    """`_build_incident` with the caller-resolved half held constant, so a case varies
    only the thing it is about: the candidate set."""
    return spool_recovery._build_incident(
        _make_state(),
        candidates,
        incident_id=1,
        printer_id=7,
        job_id="task-1",
        settings=_settings_for_factory(),
        item_id=None,
        kind=kind,
        code=code,
        fingerprint="fp",
        tray=0,
        external=False,
        printer_name="006-H2S",
    )


class TestTheIncidentFactory:
    """ONE construction of the recovery context, so the entry gate and the startup
    re-entry cannot read one fault two ways."""

    def test_the_factory_freezes_both_fault_shape_flags(self):
        candidates = _006_candidates()
        assert {c.short_code for c in candidates} == {"0700_0017", "0700_8003"}

        incident = _built(candidates, kind=spool_recovery.KIND_PHYSICAL, code="0700_8003")

        # The physical candidate is a latched pull-back…
        assert incident.retract_failure is True
        # …and the mechanical sibling is not extruder-side, so the swap rule is off.
        assert incident.extruder_side_only is False
        assert incident.codes == {"0700_0017", "0700_8003"}

    def test_the_retract_flag_reads_the_physical_candidates_only(self):
        """The flag answers "is the printer holding a failed pull-back", and only that
        class can be one — so it is a property of the FAULT, not of the routing, and a
        mechanical-only set can never raise it however the caller labelled the kind."""
        from backend.app.services.hms_errors import live_candidates

        assert _built(_006_candidates(), kind=spool_recovery.KIND_JAM, code="0700_0017").retract_failure is True
        mechanical_only = live_candidates(_make_state(hms=[_tube_stall_hms()]))
        assert _built(mechanical_only, kind=spool_recovery.KIND_JAM, code="0700_0017").retract_failure is False

    async def test_entry_and_startup_re_entry_agree_on_every_fact(
        self, db_session, printer_factory, install_settings, monkeypatch
    ):
        """The behaviour half of the one-factory pin: the same wire, read twice by the
        two callers that open an incident, must produce the same context — otherwise a
        restart resolves a fault differently from the push that raised it."""
        install_settings()
        printer = await printer_factory()
        await _farm_item(db_session, printer.id)
        built: list = []

        async def _record(incident):
            built.append(incident)

        monkeypatch.setattr(spool_recovery, "_run_recovery", _record)
        state = _make_state(hms=[_extruder_hms()])
        _wire(monkeypatch, state, FakeClient(state))

        task = await on_ams_fault(printer.id, state)
        assert task is not None
        await task

        row = await _incident_row(db_session, printer.id)
        assert row.status == "recovering"  # the driver was stubbed out, so the row stands
        reentry = await spool_recovery._reenter_recovering_incident(row.id, printer.id)
        assert reentry is not None
        await reentry

        entry_incident, reentry_incident = built
        # ``started_at`` is not a wire fact: it is each driver's own start (the instant an
        # operator's AMS command begins to count as a takeover), read at construction.
        assert entry_incident.started_at <= reentry_incident.started_at
        assert replace(entry_incident, started_at=0.0) == replace(reentry_incident, started_at=0.0)
        assert entry_incident.extruder_side_only is True
        assert entry_incident.retract_failure is False


class TestTheEscalationDetailComposer:
    """The page's two halves: what the fault IS (the reason copy) and what the one
    button in front of the operator will DO (the kind-keyed clause)."""

    _RETRACT = (
        "The printer is holding a failed filament pull-back: Retry on the screen repeats the pull-back. "
        "Free the filament at the extruder first, then press Retry."
    )

    async def _escalate_physical(self, db, printer_id, *, retract_failure):
        from backend.app.models.printer_incident import KIND_PHYSICAL

        incident = await _owned_incident(
            db, printer_id, kind=KIND_PHYSICAL, step_timeout_s=0.05, retract_failure=retract_failure
        )
        await spool_recovery._escalate(incident, "physical_fault")

    async def test_a_latched_pull_back_names_what_retry_does(self, db_session, printer_factory, monkeypatch):
        """006-H2S incident 289: the printer was latched in the farm's own unload and
        the page said "then resume" — there was no Resume on that screen, and Retry
        would have re-run the pull-back against the stuck filament."""
        printer = await printer_factory()
        failed = _spy(monkeypatch, "on_spool_recovery_failed")
        state = _make_state(hms=[_tube_stall_hms(), _physical_short_hms()])
        _wire(monkeypatch, state, FakeClient(state))

        await self._escalate_physical(db_session, printer.id, retract_failure=True)

        detail = failed.call_args.kwargs["detail"]
        assert detail == f"{spool_recovery._ESCALATE_DETAIL['physical_fault']} {self._RETRACT}"
        assert "then resume" not in detail.lower()

    async def test_a_plain_physical_fault_still_says_resume(self, db_session, printer_factory, monkeypatch):
        """The otherwise-arm carries the resume instruction the static copy gave up, so
        exactly one of the two sentences is ever rendered."""
        printer = await printer_factory()
        failed = _spy(monkeypatch, "on_spool_recovery_failed")
        state = _make_state(hms=[_physical_wire_hms()])
        _wire(monkeypatch, state, FakeClient(state))

        await self._escalate_physical(db_session, printer.id, retract_failure=False)

        detail = failed.call_args.kwargs["detail"]
        assert detail == f"{spool_recovery._ESCALATE_DETAIL['physical_fault']} Then resume on the printer."
        assert self._RETRACT not in detail

    def test_the_static_copy_makes_no_promise_about_the_screen(self):
        """The reason copy is shared by every physical escalation, so the sentence that
        depends on the screen's state cannot live in it."""
        copy = spool_recovery._ESCALATE_DETAIL["physical_fault"]
        assert copy == (
            "A physical filament fault (broken filament, a clog, or a failed pull-back) — fresh filament "
            "cannot clear it, so the farm will not swap. Check the filament path at the printer."
        )
        assert "resume" not in copy.lower()

    async def test_recovery_interrupted_on_a_physical_row_says_resume_once(
        self, db_session, printer_factory, monkeypatch
    ):
        """The physical clause is REASON-gated, and this is the reason it is.

        ``recovery_interrupted`` is minted by the startup re-entry when the wire has no
        actionable fault left, and its own copy already ends "check the filament path
        and resume on the printer". Rendering the otherwise-arm on top printed the same
        instruction twice — the shape a page composed from two independent halves
        invites, and the reason the halves are now partitioned by reason as well as by
        kind."""
        from backend.app.models.printer_incident import KIND_PHYSICAL

        printer = await printer_factory()
        failed = _spy(monkeypatch, "on_spool_recovery_failed")
        state = _make_state(hms=[])
        _wire(monkeypatch, state, FakeClient(state))
        incident = await _owned_incident(db_session, printer.id, kind=KIND_PHYSICAL, step_timeout_s=0.05)

        await spool_recovery._escalate(incident, "recovery_interrupted")

        detail = failed.call_args.kwargs["detail"]
        assert detail == spool_recovery._ESCALATE_DETAIL["recovery_interrupted"]
        assert detail.lower().count("resume on the printer") == 1

    def test_the_composer_owns_both_clauses_and_nothing_else_composes(self):
        """The clauses are kind-keyed and mutually exclusive, and `_escalate` no longer
        assembles a detail of its own."""
        composer = _function_node("_compose_detail")
        called = {n.func.id for n in ast.walk(composer) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert {"_feeder_clause", "_retract_clause"} <= called

        escalate = _function_node("_escalate")
        assert not [
            n
            for n in ast.walk(escalate)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id in {"_feeder_clause", "_retract_clause"}
        ]


class TestTheUpgradeKeepsTheSlotTheEventNamed:
    """`_resolve_fault_tray`'s PHYSICAL branch. 006's `UPGRADED … slot=None` threw away
    the only attribution the wire offered."""

    def _candidate(self, fault_class, short, slot, *, retract=False):
        from backend.app.services.hms_errors import FaultCandidate

        return FaultCandidate(
            fault_class=fault_class, short_code=short, slot=slot, extruder_side=False, retract_failure=retract
        )

    def _resolve(self, candidates):
        return spool_recovery._resolve_fault_tray(
            None,
            _make_state(),
            kind=spool_recovery.KIND_PHYSICAL,
            external=False,
            candidates=frozenset(candidates),
            printer_id=1,
        )

    def test_a_single_co_standing_mechanical_slot_carries(self):
        """THE 006 REPLAY, at the resolver: `0700_8003` names no slot, `0700_0017@0-0`
        does, and they are one physical event on one path."""
        from backend.app.services.hms_errors import AmsFaultClass

        assert self._resolve(
            {
                self._candidate(AmsFaultClass.PHYSICAL_FAULT, "0700_8003", None, retract=True),
                self._candidate(AmsFaultClass.MECHANICAL_FEED, "0700_0017", (0, 0)),
            }
        ) == (0, "single")

    def test_two_distinct_mechanical_slots_refuse_to_arbitrate(self):
        """`_candidate_slot` answers with the LOWEST slot, which would be a confident
        lie about which tray a human should go and look at."""
        from backend.app.services.hms_errors import AmsFaultClass

        assert self._resolve(
            {
                self._candidate(AmsFaultClass.PHYSICAL_FAULT, "0700_8003", None, retract=True),
                self._candidate(AmsFaultClass.MECHANICAL_FEED, "0700_0017", (0, 0)),
                self._candidate(AmsFaultClass.MECHANICAL_FEED, "0700_0018", (0, 2)),
            }
        ) == (None, "single")

    def test_two_entries_naming_ONE_slot_still_carry(self):
        """Distinctness, not entry count: two members of the tube-resistance ladder on
        one tray are one slot, and refusing there would drop an unambiguous answer."""
        from backend.app.services.hms_errors import AmsFaultClass

        assert self._resolve(
            {
                self._candidate(AmsFaultClass.PHYSICAL_FAULT, "0700_8003", None, retract=True),
                self._candidate(AmsFaultClass.MECHANICAL_FEED, "0700_0017", (0, 1)),
                self._candidate(AmsFaultClass.MECHANICAL_FEED, "0700_0018", (0, 1)),
            }
        ) == (1, "single")

    def test_the_physical_candidates_own_slot_still_wins(self):
        """The fallback is a fallback: a physical fault that names its own tray is not
        overruled by a mechanical sibling naming another."""
        from backend.app.services.hms_errors import AmsFaultClass

        assert self._resolve(
            {
                self._candidate(AmsFaultClass.PHYSICAL_FAULT, "0700_8003", (0, 3)),
                self._candidate(AmsFaultClass.MECHANICAL_FEED, "0700_0017", (0, 0)),
            }
        ) == (3, "single")

    def test_no_mechanical_sibling_still_answers_none(self):
        from backend.app.services.hms_errors import AmsFaultClass

        assert self._resolve({self._candidate(AmsFaultClass.PHYSICAL_FAULT, "0700_8003", None, retract=True)}) == (
            None,
            "single",
        )

    def test_an_ams_ht_slot_is_encoded_by_the_codec_not_by_hand(self):
        """Invariant 1: `spool_respool.encode_global_tray` is THE codec, and it knows
        conventions a bare ``ams_id * 4 + tray_id`` silently drops.

        An AMS-HT unit is a SINGLE-tray device whose unit id IS its global tray id
        (128 here). The hand-rolled arithmetic answered 512 — not this slot, and on a
        fleet with enough units a well-formed id belonging to somebody else's tray.
        A wrong tray is worse than none on every surface this answer reaches."""
        from backend.app.services.hms_errors import AmsFaultClass
        from backend.app.services.spool_respool import encode_global_tray

        assert encode_global_tray(128, 0) == 128  # the codec's own statement
        tray, verdict = self._resolve({self._candidate(AmsFaultClass.PHYSICAL_FAULT, "1800_8003", (128, 0))})
        assert (tray, verdict) == (128, "single")
        assert tray != 128 * 4 + 0

    def test_a_slot_the_codec_cannot_name_falls_through(self):
        """The fail-closed half: AMS-HT carries ONE tray, so unit 128 slot 2 is not a
        slot at all. It must read exactly as "no slot" — never as an id the arithmetic
        would happily have produced."""
        from backend.app.services.hms_errors import AmsFaultClass
        from backend.app.services.spool_respool import encode_global_tray

        assert encode_global_tray(128, 2) is None
        assert self._resolve({self._candidate(AmsFaultClass.PHYSICAL_FAULT, "1800_8003", (128, 2))}) == (
            None,
            "single",
        )

    def test_an_unnameable_physical_slot_still_reaches_the_mechanical_fallback(self):
        """Falling through means falling through to the NEXT TIER, not to None: the 006
        borrow is still available when the physical candidate's own slot is unusable."""
        from backend.app.services.hms_errors import AmsFaultClass

        assert self._resolve(
            {
                self._candidate(AmsFaultClass.PHYSICAL_FAULT, "1800_8003", (128, 2)),  # unnameable
                self._candidate(AmsFaultClass.MECHANICAL_FEED, "0700_0017", (0, 0)),
            }
        ) == (0, "single")


async def test_the_006_replay_end_to_end(db_session, printer_factory, install_settings, monkeypatch, caplog):
    """THE INCIDENT REPLAY (04:57:34 → 04:58:31).

    An extruder overload opens a jam; the driver commits the swap and publishes ONE
    unload; 56 s later the failure of that unload appears on the wire as `0700_8003`
    + `0700_0017@0-0`; the row is UPGRADED to physical in place, keeping the slot the
    mechanical sibling named; the live driver hands over without a second unload; and
    the page tells the operator what Retry will do.

    The spool is never parked — on 006 a healthy roll was stamped 12 ms after the
    fault and the operator was paged about it.
    """
    install_settings(step_timeout_s=2.0)
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    feeding = await _bind_spool(db_session, printer.id, 0, 0)
    await _bind_spool(db_session, printer.id, 0, 1)
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    oor = _spy(monkeypatch, "on_spool_out_of_rotation")
    state = _make_state(hms=[_extruder_hms()], trays=[_ams_tray(0), _ams_tray(1)])
    # The filament physically would not come out: tray_now never reaches 255, so the
    # driver sits in its unload confirm exactly as it did on 006.
    client = FakeClient(state, unload_after=99)
    pushed: list = []

    def _the_second_push(_n, st):
        """04:58:30 — the wire answers the farm's unload with its failure."""
        if pushed or ("unload",) not in client.calls:
            return
        st.hms_errors = [_tube_stall_hms(), _physical_short_hms()]
        pushed.append(asyncio.ensure_future(on_ams_fault(printer.id, st)))

    _wire(monkeypatch, state, client, on_poll=_the_second_push)

    with caplog.at_level(logging.INFO, logger="backend.app.services.spool_recovery"):
        task = await on_ams_fault(printer.id, state)
        assert task is not None  # a jam, with a driver
        await task
        assert pushed, "the second push never landed — the driver never reached its unload"
        assert await pushed[0] is None  # upgraded + escalated, no second driver

    assert client.calls.count(("unload",)) == 1, "a latched pull-back must not be answered with a second unload"
    assert not any(c[0] == "load" for c in client.calls)

    db_session.expunge_all()  # the upgrade committed in the store's own session
    row = await _incident_row(db_session, printer.id)
    assert (row.kind, row.status) == ("physical", "escalated")
    assert row.slot_global_tray == 0, "the upgrade must keep the slot 0700_0017@0-0 named"
    assert row.code == "0700_8003"

    oor.assert_not_awaited()
    assert (await db_session.get(Spool, feeding.id)).feed_fault_at is None

    failed.assert_awaited_once()
    assert failed.call_args.kwargs["kind"] == "physical"
    assert "Retry on the screen repeats the pull-back" in failed.call_args.kwargs["detail"]
    assert any("UPGRADED jam->physical" in r.getMessage() for r in caplog.records)
