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
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from backend.app.models.print_batch import PrintBatch
from backend.app.models.print_queue import PrintQueueItem

# Imported at module level so the test-engine's create_all registers this new table
# (conftest builds the schema from Base.metadata, not the models/__init__ list).
from backend.app.models.recovery_escalation import RecoveryEscalation
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services import spool_recovery
from backend.app.services.bambu_mqtt import HMSError, PrinterState
from backend.app.services.spool_recovery import (
    WAITING_REASON_FAILED,
    WAITING_REASON_RECOVERING,
    WAITING_REASON_RUNOUT,
    clear_on_reinsert,
    on_feed_fault_hms,
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

    task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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

    task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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

    task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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

    task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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

    task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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

    task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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

    task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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

    task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
    assert task is None
    assert client.calls == []


async def test_foreign_subtask_noop(db_session, printer_factory, install_settings, monkeypatch):
    install_settings()
    printer = await printer_factory()
    await _farm_item(db_session, printer.id, subtask="task-1")
    # The live print echoes a DIFFERENT subtask id -> not a farm-dispatched unit.
    state = _make_state(subtask="foreign-999")
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
    assert task is None
    assert client.calls == []


async def test_non_farm_no_item_noop(db_session, printer_factory, install_settings, monkeypatch):
    install_settings()
    printer = await printer_factory()  # no queue item at all
    state = _make_state()
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
    assert task is None
    assert client.calls == []


async def test_multi_feeder_escalates_immediately(db_session, printer_factory, install_settings, monkeypatch):
    install_settings()
    printer = await printer_factory()
    item = await _farm_item(db_session, printer.id, ams_mapping="[0, 1, -1, -1]")  # two feeders
    failed = _spy(monkeypatch, "on_spool_recovery_failed")
    state = _make_state()
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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

    task1 = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
    assert task1 is not None
    task2 = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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

    task1 = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
    assert task1 is not None
    await task1
    assert state.state == "RUNNING"

    # Fresh pause on the same job + same code -> NEW task (dedup re-armed on success).
    state.state = "PAUSE"
    state.tray_now = 1  # currently on the replacement chosen in round 1
    task2 = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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

    task1 = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
    assert task1 is not None
    await task1
    assert client.calls == []  # closed as transient, never acted

    state.state = "PAUSE"  # a real jam this time
    task2 = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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

    task = await on_feed_fault_hms(printer.id, {"0300_801E"}, state)
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

    task = await on_feed_fault_hms(printer.id, {"0300_801E"}, state)
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

    task1 = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
    assert task1 is not None
    await task1
    assert state.state == "PAUSE"  # escalated, left paused
    calls_after = len(client.calls)

    task2 = await on_feed_fault_hms(printer.id, {"0300_801E"}, state)
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

    task1 = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
    assert task1 is not None
    await task1
    assert state.state == "RUNNING"  # the external actor's resume stands
    calls_after = len(client.calls)

    task2 = await on_feed_fault_hms(printer.id, {"0300_801E"}, state)
    assert task2 is None  # latched
    assert len(client.calls) == calls_after


async def test_escalation_latch_scoped_to_job(db_session, printer_factory, install_settings, monkeypatch):
    """The latch is per (printer, job): a NEW job on the same printer recovers
    normally after a prior job escalated."""
    install_settings()
    printer = await printer_factory()
    _spy(monkeypatch, "on_spool_recovery_failed")

    # Job 1 escalates (no eligible replacement).
    await _farm_item(db_session, printer.id, subtask="task-1")
    state1 = _make_state(subtask="task-1", trays=[_ams_tray(0)])
    client1 = FakeClient(state1)
    _wire(monkeypatch, state1, client1)
    task1 = await on_feed_fault_hms(printer.id, {"0700_8010"}, state1)
    await task1
    assert state1.state == "PAUSE"

    # Job 2 on the same printer is a fresh incident — recovers normally.
    await _farm_item(db_session, printer.id, subtask="task-2")
    await _bind_spool(db_session, printer.id, 0, 0)
    state2 = _make_state(subtask="task-2")
    client2 = FakeClient(state2)
    _wire(monkeypatch, state2, client2)
    task2 = await on_feed_fault_hms(printer.id, {"0700_8010"}, state2)
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
    # Simulate the flap cap already reached this job.
    spool_recovery._success_counts[(printer.id, "task-1")] = spool_recovery._MAX_SUCCESSES_PER_JOB
    state = _make_state(subtask="task-1")
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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
        task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
    assert task is None
    assert any("0700_8010" in r.getMessage() and "NOT recovered" in r.getMessage() for r in caplog.records)


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

    task = await on_feed_fault_hms(printer.id, {"0300_8004"}, state)
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

    task = await on_feed_fault_hms(printer.id, {"0300_8004"}, state)
    await task

    assert state.state == "PAUSE"  # never resumed — left for a same-slot refill
    assert not any(c[0] == "load" for c in client.calls)  # ZERO cross-slot load commands
    assert ("unload",) not in client.calls  # the whole swap machine was skipped
    failed.assert_awaited_once()
    assert failed.call_args.kwargs["is_feed_fault"] is False  # runout copy branch
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

    task = await on_feed_fault_hms(printer.id, {"0300_8004"}, state)
    await task

    failed.assert_awaited_once()
    assert "same" in failed.call_args.kwargs["detail"].lower()  # "insert into the SAME slot"


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

    task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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

    task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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

    task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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

    task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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

    task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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

    task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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

    task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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
    state = _make_state(tray_now=255, ams_status_main=0, hms=[])
    client = FakeClient(state)
    _wire(monkeypatch, state, client)

    task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
    await task

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

    task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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

    task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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

    task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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

    task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
    await task

    assert seen.get("force") is True
    failed.assert_awaited_once()
    assert failed.call_args.kwargs["is_feed_fault"] is True  # feed-fault chooses jam copy
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
        task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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

    task = await on_feed_fault_hms(printer.id, {"0300_8004"}, state)
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

    task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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

    task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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

    task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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

    task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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

    task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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

    task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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

    task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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

    task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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

    task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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
        task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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


def _incident(printer_id: int, *, step_timeout_s: float, max_attempts: int = 2):
    return spool_recovery.RecoveryIncident(
        printer_id=printer_id,
        job_id="task-1",
        codes=frozenset({"0700_8010"}),
        item_id=1,
        settings=spool_recovery.RecoverySettings(
            enabled=True, max_attempts=max_attempts, step_timeout_s=step_timeout_s, protect_layers=7
        ),
        jammed_global_tray=0,
        is_feed_fault=True,
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
        task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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
    await _farm_item(db_session, printer.id)
    _spy(monkeypatch, "on_spool_recovery_failed")
    state = _make_state(tray_now=255, ams_status_main=0, hms=[], trays=[_ams_tray(0), _ams_tray(1)])
    client = FakeClient(state, load_after=9999)
    _wire(monkeypatch, state, client)

    with caplog.at_level(logging.WARNING, logger="backend.app.services.spool_recovery"):
        task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
        await task

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
        task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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
        task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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

    task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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

    task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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

    task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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

    task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
    assert task is not None
    await task

    assert not any(c[0] in ("unload", "load") for c in client.calls)  # NO swap performed
    assert client.calls.count(("resume",)) == 1  # only the reset resume
    assert state.state == "RUNNING"
    assert spool_recovery._success_counts[(printer.id, "task-1")] == 1  # counted toward the flap cap
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
        task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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

    task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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

    task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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

    task = await on_feed_fault_hms(printer.id, {"0300_801E"}, state)
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
        task = await on_feed_fault_hms(printer.id, {"0700_8010"}, state)
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


async def test_will_own_false_when_escalation_latched(db_session, printer_factory):
    printer = await printer_factory()
    await _farm_item(db_session, printer.id, subtask="task-1")
    state = _make_state(subtask="task-1")
    spool_recovery._escalated.add((printer.id, "task-1"))  # already given up on this (printer, job)

    assert await spool_recovery.will_own(db_session, printer.id, state) is False


async def test_will_own_false_when_no_farm_item(db_session, printer_factory):
    printer = await printer_factory()
    # No farm item dispatched for this subtask → a foreign / non-farm job.
    state = _make_state(subtask="foreign-task")

    assert await spool_recovery.will_own(db_session, printer.id, state) is False


async def test_will_own_false_when_db_read_raises(db_session, printer_factory, monkeypatch):
    """Fail toward notifying: any exception in the predicate returns False so a raw
    alert is never suppressed on the strength of a read that errored."""
    printer = await printer_factory()
    state = _make_state(subtask="task-1")

    async def _boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(spool_recovery, "_resolve_farm_item", _boom)

    assert await spool_recovery.will_own(db_session, printer.id, state) is False
