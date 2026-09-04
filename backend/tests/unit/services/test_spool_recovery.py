"""Unit tests for the automatic mid-print spool-jam recovery state machine.

Drives the whole feature through the public entry ``on_feed_fault_hms`` (which
returns the spawned driver task so the test can await it) against a real
``PrinterState`` mutated by a scripted ``FakeClient``. Covers the happy swap, the
production-log replays (load-needs-resend, resume-needs-second-cycle), candidate
escalation, external-interference aborts, the entry gates (disabled / non-farm /
multi-feeder / dedup), runout handling, the layer-conditional floor, the restart
short-circuit, and the presence-edge ``clear_on_reinsert``.
"""

import asyncio
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
from backend.app.services import printer_incidents, spool_recovery
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

_NONE_TAG = "0000000000000000"
_NONE_UUID = "0" * 32


# --- fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset():
    spool_recovery._reset_state()
    yield
    spool_recovery._reset_state()


@pytest.fixture(autouse=True)
def _fast_timing(monkeypatch):
    monkeypatch.setattr(spool_recovery, "_POLL_INTERVAL_S", 0.005)
    monkeypatch.setattr(spool_recovery, "_POST_RESUME_STABLE_S", 0.02)
    monkeypatch.setattr(spool_recovery, "_REPAUSE_WATCH_S", 0.03)
    # The unload settle dwell is wall-clock by nature; zero it so the confirm
    # resolves on the first idle+empty poll. The dwell itself is pinned with a fake
    # clock in TestUnloadGraceDwell.
    monkeypatch.setattr(spool_recovery, "_UNLOAD_GRACE_S", 0.0)
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


class FakeClient:
    """Records unload/load/resume/pause/execute_hms_action and mutates the shared
    PrinterState to simulate the printer's response, with scripted stalls."""

    def __init__(
        self,
        state,
        *,
        unload_after=1,
        load_after=1,
        resume_repauses=0,
        external_resume_on_unload=False,
        external_resume_tray=None,
        hijack_on_load=False,
        unload_ret=True,
        load_ret=True,
        resume_ret=True,
        pause_ret=True,
        unload_stuck=False,
        write_refusal=None,
        refusal_clears_on_settle=False,
    ):
        self.state = state
        self.unload_after = unload_after
        self.load_after = load_after
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
        # write_refusal: what ams_write_refusal() reports (None = wire is clear).
        self.write_refusal = write_refusal
        self.refusal_clears_on_settle = refusal_clears_on_settle
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
        if self._load >= self.load_after:
            self.state.tray_now = tray_id
        return True

    def resume_print(self):
        self._resume += 1
        self.calls.append(("resume",))
        if not self.resume_ret:
            return False
        self.state.state = "PAUSE" if self._resume <= self.resume_repauses else "RUNNING"
        return True

    def pause_print(self):
        self.calls.append(("pause",))
        if not self.pause_ret:
            return False
        self.state.state = "PAUSE"
        return True

    def execute_hms_action(self, print_error, action, job_id=None):
        self.calls.append(("hms_action", action))
        return self.resume_print()


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
    client = FakeClient(state, resume_repauses=2)
    _wire(monkeypatch, state, client)

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
    """The H2S main-extruder-overload code (0300_801E) now triggers recovery."""
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

    assert ("load", 1) in client.calls
    assert state.state == "RUNNING"
    db_session.expunge_all()
    refreshed = await db_session.get(PrintQueueItem, item.id)
    assert refreshed.waiting_reason is None  # cleared on success
    jammed_after = await db_session.get(Spool, jammed.id)
    assert jammed_after.feed_fault_at is not None  # original still marked at the swap-commit boundary
    assert jammed_after.feed_fault_code == "0300_801E"


async def test_extruder_side_rejam_keeps_replacement_in_rotation(
    db_session, printer_factory, install_settings, monkeypatch
):
    """On an extruder-side fault the extruder is the common factor: a re-jam after
    the swap keeps the replacement IN rotation (feed_fault_at NULL) and tries the
    next candidate. The ORIGINAL jammed tray is still marked."""
    install_settings()
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    original = await _bind_spool(db_session, printer.id, 0, 0)  # jammed tray0
    replacement = await _bind_spool(db_session, printer.id, 0, 1)  # tray1 re-jams
    state = _make_state(trays=[_ams_tray(0), _ams_tray(1), _ams_tray(2)], hms=[_extruder_hms()])
    client = FakeClient(state, resume_repauses=2)  # tray1 re-jams both cycles; tray2 succeeds
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    await task

    assert ("load", 1) in client.calls and ("load", 2) in client.calls
    assert state.state == "RUNNING"
    db_session.expunge_all()
    replacement_after = await db_session.get(Spool, replacement.id)
    assert replacement_after.feed_fault_at is None  # extruder-side → kept in rotation
    original_after = await db_session.get(Spool, original.id)
    assert original_after.feed_fault_at is not None  # original marked at the swap-commit boundary
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
    assert await spool_recovery.on_job_terminal(printer.id) is True

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
    runout_incident = replace(_incident(printer.id, step_timeout_s=0.05), kind=spool_recovery.KIND_RUNOUT)

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
    only_near_empty_spools reason, not the protected-layer one."""
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
    assert "effectively empty" in failed.call_args.kwargs["detail"]  # only_near_empty_spools
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

    await spool_recovery._run_recovery(_incident(printer.id, step_timeout_s=0.05, item_id=item.id))

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
    db_session, printer_factory, install_settings, monkeypatch
):
    """THE INCIDENT PIN: PAUSE + tray_now 255 + ams_status_main 1 (filament_change)
    + a live 0700_8010 → an unload MUST be published before the first load."""
    install_settings()
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    await _bind_spool(db_session, printer.id, 0, 0)
    state = _make_state(tray_now=255, ams_status_main=1, trays=[_ams_tray(0), _ams_tray(1)])
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    await task

    motion = [c for c in client.calls if c[0] in ("unload", "load")]
    assert motion, "recovery sent no AMS motion at all"
    assert motion[0] == ("unload",), f"the first AMS command must be the unload, got {motion}"
    assert ("load", 1) in client.calls
    assert state.state == "RUNNING"  # self-healed, no human needed


async def test_every_candidate_round_unloads_again_after_a_failed_load(
    db_session, printer_factory, install_settings, monkeypatch
):
    """A `load_fail` round is followed by a REAL unload cycle in the next round —
    with the short-circuit gone, rounds 2..N are no longer unload-free."""
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

    # Three rounds ran (two candidates + the exhausted round) — each one unloaded.
    assert client.calls.count(("unload",)) == 3
    assert ("load", 1) in client.calls and ("load", 2) in client.calls


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


async def test_unload_stuck_non_idle_never_confirms_and_never_loads(
    db_session, printer_factory, install_settings, monkeypatch, caplog
):
    """An AMS state machine still non-idle at the step timeout is NOT a confirmed
    unload: the send is retried, then recovery escalates `unload_failed` — it never
    loads into a busy AMS."""
    install_settings(max_attempts=2, step_timeout_s=0.05)
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    _spy(monkeypatch, "on_spool_recovery_failed")
    state = _make_state(tray_now=255, ams_status_main=1, trays=[_ams_tray(0), _ams_tray(1)])
    client = FakeClient(state, unload_stuck=True)  # never returns to idle
    _wire(monkeypatch, state, client)

    with caplog.at_level(logging.WARNING, logger="backend.app.services.spool_recovery"):
        task = await on_ams_fault(printer.id, state)
        await task

    assert client.calls.count(("unload",)) == 2  # both attempts resent
    assert not any(c[0] == "load" for c in client.calls)  # never loaded unconfirmed
    assert _escalated_reasons(caplog) == ["unload_failed"]
    assert state.state == "PAUSE"
    db_session.expunge_all()
    assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason == WAITING_REASON_FAILED


# ===========================================================================
# Unload settle dwell: with no observed filament-change cycle (command latency or
# a no-op unload), idle+empty must HOLD for _UNLOAD_GRACE_S before the load starts.
# Driven on a fake clock — no test ever sleeps for real.
# ===========================================================================


def _incident(
    printer_id: int, *, step_timeout_s: float, max_attempts: int = 2, incident_id: int = 0, item_id: int | None = 1
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
        extruder_side_only=False,
        layer_at_fault=50,
        code="0700_8010",
        printer_name="009-H2S",
        job_name="SKU007",
    )


class TestUnloadGraceDwell:
    """`_confirm_unloaded`'s no-cycle-observed path. The operator's proven manual
    recovery left 16 s between the unload and the load that worked; the machine now
    gives the spool at least `_UNLOAD_GRACE_S` of held idle+empty before loading."""

    @pytest.fixture
    def clock(self, monkeypatch):
        class _Clock:
            """Monotonic fake clock advanced only by the module's poll sleep."""

            def __init__(self, step: float = 2.0):
                self.t = 0.0
                self.step = step

            def now(self) -> float:
                return self.t

            async def sleep(self, _delay):
                self.t += self.step

        c = _Clock()
        monkeypatch.setattr(spool_recovery, "_now", c.now)
        monkeypatch.setattr(spool_recovery.asyncio, "sleep", c.sleep)
        monkeypatch.setattr(spool_recovery, "_UNLOAD_GRACE_S", 15.0)  # the production value
        return c

    async def test_ok_only_after_the_grace_dwell_elapsed(self, clock, monkeypatch):
        state = _make_state(tray_now=255, ams_status_main=0)
        _wire(monkeypatch, state, FakeClient(state))

        verdict = await spool_recovery._confirm_unloaded(_incident(1, step_timeout_s=60.0))

        assert verdict == "ok"
        assert clock.t >= spool_recovery._UNLOAD_GRACE_S  # never confirmed early

    async def test_timeout_when_the_dwell_cannot_fit_in_the_step_timeout(self, clock, monkeypatch):
        state = _make_state(tray_now=255, ams_status_main=0)
        _wire(monkeypatch, state, FakeClient(state))

        verdict = await spool_recovery._confirm_unloaded(_incident(1, step_timeout_s=10.0))

        assert verdict == "timeout"  # idle+empty held, but not long enough
        assert clock.t < spool_recovery._UNLOAD_GRACE_S

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

        verdict = await spool_recovery._confirm_unloaded(_incident(1, step_timeout_s=60.0))

        assert verdict == "ok"
        assert clock.t < spool_recovery._UNLOAD_GRACE_S  # confirmed by the cycle, not the dwell


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
    state = _make_state(trays=[_ams_tray(0)])  # only the jammed tray is loaded
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    with caplog.at_level(logging.WARNING, logger="backend.app.services.spool_recovery"):
        task = await on_ams_fault(printer.id, state)
        await task

    assert not any(c[0] == "load" for c in client.calls)
    assert _escalated_reasons(caplog) == ["no_eligible_spool"]


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

    with caplog.at_level(logging.WARNING, logger="backend.app.services.spool_recovery"):
        await spool_recovery._run_recovery(_incident(printer.id, step_timeout_s=0.05, item_id=item.id))

    assert ("unload",) not in client.calls  # genuinely clean state → skipped
    assert ("load", 1) in client.calls
    assert _escalated_reasons(caplog) == ["candidate_loads_failed"]


async def test_confirmed_unloads_with_every_load_failing_escalates_feed_path_blocked(
    db_session, printer_factory, install_settings, monkeypatch, caplog
):
    """The AMS unloaded cleanly every round and still nothing would feed — the
    blockage is downstream of the spool (buffer / PTFE), so say so."""
    install_settings()
    printer = await printer_factory()
    await _farm_item(db_session, printer.id)
    _spy(monkeypatch, "on_spool_recovery_failed")
    _spy(monkeypatch, "on_spool_out_of_rotation")
    state = _make_state(trays=[_ams_tray(0), _ams_tray(1), _ams_tray(2)])
    client = FakeClient(state, load_after=9999)
    _wire(monkeypatch, state, client)

    with caplog.at_level(logging.WARNING, logger="backend.app.services.spool_recovery"):
        task = await on_ams_fault(printer.id, state)
        await task

    assert client.calls.count(("unload",)) == 3  # every round confirmed an unload
    assert _escalated_reasons(caplog) == ["feed_path_blocked"]


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
    # W1: the wedged-change reasons point the operator at the physical fix instead
    # (the AMS is left paused regardless, but the copy names the part to inspect).
    for reason in ("unload_failed", "stuck_reset_failed"):
        assert reason in spool_recovery._ESCALATE_DETAIL
        assert spool_recovery._ESCALATE_DETAIL[reason].endswith("(check the filament buffer/feeder).")


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
# W1: stuck-change firmware reset (009-H2S 2026-07-20).
#
# After a feed fault the AMS can sit WEDGED mid filament-change (PAUSE +
# ams_status_main non-idle) where it silently ignores unloads. The ONLY verb that
# freed it live was a resume (the touchscreen CONTINUE). Every candidate round now
# runs _reset_stuck_change FIRST.
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
    """The AMS ignores even the reset resume: the send is accepted but the state
    machine never moves (still PAUSE, still non-idle) — a genuinely dead AMS."""

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

    verdict = await spool_recovery._reset_stuck_change(_incident(7, step_timeout_s=1.0), client)

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
    assert kwargs["slot_desc"] == "AMS0 slot 0"  # jammed global tray 0 → AMS0 slot 0
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
    """Reset (d): the AMS ignores even the reset resume (state never leaves PAUSE +
    non-idle) → recovery escalates the new stuck_reset_failed reason, never touching
    the unload. The feeder is genuinely wedged, so the jammed spool IS taken out of
    rotation at this commit boundary — before the escalation."""
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

    assert ("resume",) in client.calls  # the reset was attempted
    assert ("unload",) not in client.calls  # escalated before the unload
    assert _escalated_reasons(caplog) == ["stuck_reset_failed"]
    assert "buffer/feeder" in failed.call_args.kwargs["detail"]  # points at the physical fix
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

    verdict = await spool_recovery._reset_stuck_change(_incident(7, step_timeout_s=1.0), client)

    assert verdict == "skipped"
    assert client.calls == []  # nothing published on an idle AMS


async def test_stuck_reset_budget_spent_no_second_resume(monkeypatch):
    """The reset budget is one per incident: a second wedged round publishes NO
    second resume — it returns 'skipped' (the round then falls through to the unload
    exactly as an idle-AMS round would)."""
    state = _make_state(tray_now=255, ams_status_main=1)
    client = _WedgedClient(state)  # resume never moves the state machine
    _wire(monkeypatch, state, client)
    incident = _incident(7, step_timeout_s=0.02)

    v1 = await spool_recovery._reset_stuck_change(incident, client)
    assert v1 == "fail"  # wedged: the reset did not free it
    assert client.calls.count(("resume",)) == 1  # the first round published the reset

    v2 = await spool_recovery._reset_stuck_change(incident, client)
    assert v2 == "skipped"  # budget spent
    assert client.calls.count(("resume",)) == 1  # NO second resume


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
    assert jammed_after.feed_fault_at is not None  # jammed spool taken out of rotation at the swap-commit boundary
    assert jammed_after.feed_fault_code == "0300_801E"


@pytest.mark.parametrize("ams_main", [2, 3, 4])
async def test_reset_skipped_for_non_filament_change_states(ams_main, monkeypatch):
    """Only ams_status_main == 1 (filament_change) is resume-resettable (009 evidence).
    assist(3), identifying(2) and calibration(4) are NOT stuck changes: the reset is a
    no-op ('skipped') that publishes NO resume and spends NO reset budget, so the
    unload→swap machine owns the round (006-H2S 2026-07-21)."""
    state = _make_state(tray_now=3, ams_status_main=ams_main)
    client = FakeClient(state)
    _wire(monkeypatch, state, client)
    incident = _incident(7, step_timeout_s=1.0)

    verdict = await spool_recovery._reset_stuck_change(incident, client)

    assert verdict == "skipped"
    assert client.calls == []  # nothing published on a non-filament-change AMS
    assert (7, "task-1") not in spool_recovery._stuck_resets  # no reset budget spent


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

    verdict = await spool_recovery._confirm_unloaded(_incident(7, step_timeout_s=1.0))

    assert verdict == "ok"


# ===========================================================================
# W2: durable repeat-jam quarantine off the recovery_escalation ledger.
# ===========================================================================


async def test_two_escalations_within_window_quarantines(db_session, printer_factory, install_settings, monkeypatch):
    """Two recovery escalations for one printer within _JAM_QUARANTINE_WINDOW_H
    hours quarantine it, with failure_count == the in-window escalation count. The
    first escalation (count 1) is under the threshold and does not."""
    from backend.app.services import farm_policy

    install_settings()
    printer = await printer_factory()
    _spy(monkeypatch, "on_spool_recovery_failed")
    q = AsyncMock(return_value=True)
    monkeypatch.setattr(farm_policy, "quarantine_printer", q)

    incident = _incident(printer.id, step_timeout_s=0.05)
    await spool_recovery._escalate(incident, "unload_failed")
    q.assert_not_called()  # one escalation is under the threshold

    await spool_recovery._escalate(incident, "stuck_reset_failed")
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

    await spool_recovery._escalate(_incident(printer.id, step_timeout_s=0.05), "stuck_reset_failed")

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
        incident = _incident(printer.id, step_timeout_s=0.05)

        await spool_recovery._escalate(incident, "jammed_tray_unresolved")
        quarantine.assert_not_called()
        await spool_recovery._escalate(incident, "jammed_tray_unresolved")

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
        incident = _incident(printer.id, step_timeout_s=0.05)

        await spool_recovery._escalate(incident, "runout_needs_refill")
        await spool_recovery._escalate(incident, "jammed_tray_unresolved")

        quarantine.assert_not_called()  # ONE countable escalation, not two

    async def test_two_external_feed_faults_do_not_quarantine(
        self, db_session, printer_factory, install_settings, monkeypatch, quarantine
    ):
        """No AMS took part in either — twice."""
        install_settings()
        printer = await printer_factory()
        _spy(monkeypatch, "on_spool_recovery_failed")
        incident = _incident(printer.id, step_timeout_s=0.05)

        await spool_recovery._escalate(incident, "external_feed_fault")
        await spool_recovery._escalate(incident, "external_feed_fault")

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
        incident = _incident(printer.id, step_timeout_s=0.05)

        await spool_recovery._escalate(incident, "unload_failed")
        await spool_recovery._escalate(incident, "runout_needs_refill")

        quarantine.assert_not_called()

    async def test_every_escalation_still_records_its_row(
        self, db_session, printer_factory, install_settings, monkeypatch, quarantine
    ):
        """The ledger stays a COMPLETE forensic record — only the COUNT is filtered."""
        install_settings()
        printer = await printer_factory()
        _spy(monkeypatch, "on_spool_recovery_failed")
        incident = _incident(printer.id, step_timeout_s=0.05)

        for reason in ("runout_needs_refill", "external_feed_fault", "ams_drying", "recovery_interrupted"):
            await spool_recovery._escalate(incident, reason)

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


async def test_extruder_side_stamps_feeding_spool_at_commit(db_session, printer_factory, install_settings, monkeypatch):
    """An extruder-side-only fault still commits the swap: the FEEDING spool is taken
    out of rotation at the commit boundary. A re-jam of the replacement keeps that
    replacement IN rotation (the extruder is the common factor, not the spool), so no
    second OOR notify fires."""
    install_settings()
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id)
    original = await _bind_spool(db_session, printer.id, 0, 0)  # feeding tray0
    replacement = await _bind_spool(db_session, printer.id, 0, 1)  # tray1 re-jams
    oor = _spy(monkeypatch, "on_spool_out_of_rotation")
    state = _make_state(trays=[_ams_tray(0), _ams_tray(1), _ams_tray(2)], hms=[_extruder_hms()])
    client = FakeClient(state, resume_repauses=2)  # tray1 re-jams both cycles; tray2 succeeds
    _wire(monkeypatch, state, client)

    task = await on_ams_fault(printer.id, state)
    await task

    assert state.state == "RUNNING"
    oor.assert_awaited_once()  # only the feeding spool announced — the replacement stays in rotation
    db_session.expunge_all()
    assert (await db_session.get(Spool, original.id)).feed_fault_at is not None  # feeding spool stamped at commit
    assert (await db_session.get(Spool, replacement.id)).feed_fault_at is None  # extruder-side → kept in rotation
    refreshed = await db_session.get(PrintQueueItem, item.id)
    assert json.loads(refreshed.ams_mapping) == [2, -1, -1, -1]  # landed on tray2


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
    assert spool_recovery.runout_slot_desc(2) == "AMS A slot 3"


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
# The trigger sets are VIEWS of the hms_errors AMS fault taxonomy rather than
# literals in spool_recovery. The pins below spell out the RATIFIED membership
# independently of the module's own data, so a taxonomy edit that would change what
# this machine ACTS on fails here rather than moving with it.
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


def test_ams_feed_fault_set_matches_the_ratified_partition():
    assert spool_recovery.AMS_FEED_FAULT_HMS_CODES == _RATIFIED_AMS_FEED


def test_extruder_feed_fault_set_matches_the_ratified_partition():
    assert spool_recovery.EXTRUDER_FEED_FAULT_HMS_CODES == _RATIFIED_EXTRUDER


def test_feed_fault_union_matches_the_ratified_partition():
    assert spool_recovery.FEED_FAULT_HMS_CODES == _RATIFIED_AMS_FEED | _RATIFIED_EXTRUDER


def test_recoverable_set_matches_the_ratified_partition():
    assert spool_recovery.RECOVERABLE_HMS_CODES == (_RATIFIED_AMS_FEED | _RATIFIED_EXTRUDER | _RATIFIED_RUNOUT)


def test_the_swap_set_is_exactly_the_mechanical_feed_class():
    """The widening's real contract: no marker sits between the taxonomy and the
    machine any more, so a code classified mechanical_feed IS a swap trigger.

    The one thing that DOES sit between them is hardware: an external-holder member
    of the same class is routed away from the machine by ``_route_fault``, not by
    being kept out of this vocabulary (a second membership list is exactly the
    drift doctrine invariant 1 forbids)."""
    from backend.app.services.hms_errors import mechanical_feed_short_codes

    assert mechanical_feed_short_codes() == spool_recovery.FEED_FAULT_HMS_CODES
    for short in ("0700_8005", "0700_8006", "0700_8028", "07FF_8005"):
        assert short in spool_recovery.RECOVERABLE_HMS_CODES


def test_the_legacy_swap_marker_is_gone():
    """Hard cutover: the WS2a behavior pin must not survive its consumer wave."""
    import backend.app.services.hms_errors as hms_errors

    assert not hasattr(hms_errors, "legacy_swap_short_codes")
    assert not hasattr(hms_errors, "_LEGACY_SWAP_SHORTS")


def test_external_spool_runouts_are_not_recoverable():
    """No AMS slot means no sibling tray — the swap machine has nothing to swap to."""
    from backend.app.services.hms_errors import runout_external_short_codes

    assert not (runout_external_short_codes() & spool_recovery.RECOVERABLE_HMS_CODES)


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

    # Fault 1: a physical fault escalates and holds.
    state = _make_state(hms=[_physical_wire_hms()])
    _wire(monkeypatch, state, FakeClient(state))
    await on_ams_fault(printer.id, state)
    assert (await _incident_row(db_session, printer.id)).kind == "physical"

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

    assert await spool_recovery.on_job_terminal(printer.id) is True

    assert await _incident_row(db_session, printer.id) is None
    assert await spool_recovery.on_job_terminal(printer.id) is False  # nothing left to close


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
        await spool_recovery.on_job_terminal(printer.id)  # the hold ends

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
