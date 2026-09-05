"""Branch matrix for the pause-recovery lane (2026-09-04 fleet outage + plate vision).

Drives the real entry points — the ~1 Hz sampler ``note_status_push``, the vision hook
``on_plate_vision_trip`` and the operator's ``on_plate_cleared`` — against a real
``PrinterState`` mutated by a scripted ``FakeClient``, in the ``test_spool_recovery``
idiom. Every branch of ``_recover_power_loss`` gets a case, in its own order; the
lost-Z outage arm gets the burst / single-reconnect / laddered-model triple; and the
vision lane pins the ordering contract that everything downstream rests on — the
farm-abort mark is on disk BEFORE the stop goes out.
"""

import asyncio
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from backend.app.models.print_batch import PrintBatch
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer

# Imported at module level so the test engine's create_all registers these tables.
from backend.app.models.printer_incident import (
    KIND_PLATE_VISION,
    KIND_POWER_LOSS,
    KIND_RUNOUT,
    KIND_Z_REFERENCE_LOST,
    STATUS_ESCALATED,
    STATUS_RECOVERING,
    PrinterIncident,
)
from backend.app.models.printer_model_geometry import PrinterModelGeometry
from backend.app.services import pause_recovery, printer_incidents
from backend.app.services.bambu_mqtt import HMSError, PrinterState
from backend.app.services.plate_occupancy import EscalationOnly, PendingEject, plate_occupancy
from backend.app.services.printer_incidents import WAITING_REASON_POWER_LOSS

pytestmark = pytest.mark.asyncio


# --- fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset():
    pause_recovery._reset_state()
    printer_incidents._reset_state()
    yield
    pause_recovery._reset_state()
    printer_incidents._reset_state()


@pytest.fixture(autouse=True)
def _fast_timing(monkeypatch):
    """Collapse every wall-clock wait. Each constant's own MEANING is pinned by the
    branch that reads it (a retry happens / does not happen), never by its value."""
    monkeypatch.setattr(pause_recovery, "_POWER_LOSS_SETTLE_S", 0.0)
    monkeypatch.setattr(pause_recovery, "_POWER_LOSS_RETRY_S", 0.0)
    monkeypatch.setattr(pause_recovery, "_POWER_LOSS_CONFIRM_S", 0.05)
    monkeypatch.setattr(pause_recovery, "_POWER_LOSS_POLL_S", 0.005)
    monkeypatch.setattr(pause_recovery, "_SUMMARY_WINDOW_S", 0.01)
    monkeypatch.setattr(pause_recovery, "_VISION_STOP_RETRY_S", 0.0)


@pytest.fixture(autouse=True)
def _own_sessions(test_engine, monkeypatch):
    """Point the lane's own-session openers at the test engine."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    import backend.app.core.database as core_db

    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(core_db, "async_session", maker)
    return maker


# --- wire builders ----------------------------------------------------------


def _prompt_hms():
    """0300_8007 — "There was an unfinished print job when the printer lost power"."""
    return HMSError(code="8007", attr=0x03000000, module=3, severity=3, full_code="0300000000008007")


def _resume_failed_hms():
    """0300_400D — "Resume failed after power loss"."""
    return HMSError(code="400D", attr=0x03000000, module=3, severity=2, full_code="030000000000400D")


def _vision_hms():
    """0500_808C — the build-plate offset / debris check, the dominant prod trip."""
    return HMSError(code="808C", attr=0x05000000, module=5, severity=2, full_code="050000000000808C")


def _make_state(
    *,
    subtask="task-1",
    gcode_state="PAUSE",
    hms=None,
    connected=True,
    epoch=1,
    disconnected_at=None,
):
    st = PrinterState()
    st.connected = connected
    st.state = gcode_state
    st.subtask_id = subtask
    st.subtask_name = "SKU007"
    st.connection_epoch = epoch
    st.disconnected_at = disconnected_at
    st.hms_errors = hms if hms is not None else [_prompt_hms()]
    return st


class FakeClient:
    """Records resume/stop sends and mutates the shared PrinterState in reply."""

    def __init__(self, state, *, resume_ret=True, resume_reaches_running=True, resume_false_first=0):
        self.state = state
        self.resume_ret = resume_ret
        self.resume_reaches_running = resume_reaches_running
        self.resume_false_first = resume_false_first
        self.calls: list[tuple] = []
        self._resume = 0

    def resume_print(self):
        self._resume += 1
        self.calls.append(("resume",))
        if not self.resume_ret or self._resume <= self.resume_false_first:
            return False
        if self.resume_reaches_running:
            self.state.state = "RUNNING"
        return True


def _wire(monkeypatch, state, client=None, *, fleet=None):
    """Point the lane's live-state/client/fleet lookups at the scripted objects."""
    calls: list[tuple] = []

    monkeypatch.setattr(pause_recovery.printer_manager, "get_status", lambda _pid: state)
    monkeypatch.setattr(pause_recovery.printer_manager, "get_client", lambda _pid: client)
    monkeypatch.setattr(pause_recovery.printer_manager, "get_all_statuses", lambda: fleet or {})

    def _stop(pid):
        calls.append(("stop", pid))
        return True

    monkeypatch.setattr(pause_recovery.printer_manager, "stop_print", _stop)
    return calls


def _spy(monkeypatch, name):
    from backend.app.services.notification_service import notification_service

    m = AsyncMock()
    monkeypatch.setattr(notification_service, name, m)
    return m


async def _drive(printer_id, state):
    """Run one sampler push and await whatever driver it spawned."""
    pause_recovery.note_status_push(printer_id, state)
    task = pause_recovery._in_flight.get(printer_id)
    if task is not None:
        await task
    # Let any z-arm / summary task the push spawned reach its first await.
    await asyncio.sleep(0)


async def _drain_z_arm(printer_id):
    """Await the z-reference arm the reconnect edge fired and forgot, if it fired one.

    It is spawned through ``core.tasks.spawn_background_task`` (not ``_in_flight``), so
    a test used to sleep a fixed 10 ms for it — and on a loaded gate machine the arm's
    DB writes took longer than that, the assertion ran first, and the incident opened
    during TEARDOWN (ship run 20260904-225728-0603, this file's only red id). A no-op
    when no arm was spawned: then nothing can open an incident, so there is no race.
    """
    import backend.app.core.tasks as core_tasks

    name = f"z-reference-arm-p{printer_id}"
    pending = [t for t in list(core_tasks._background_tasks) if t.get_name() == name and not t.done()]
    if pending:
        await asyncio.gather(*pending)


async def _drain_summary():
    task = pause_recovery._summary_task
    if task is not None:
        await task


# --- DB helpers -------------------------------------------------------------


async def _printer(db, printer_id, *, name="001-H2S", model="H2S"):
    p = Printer(
        id=printer_id,
        name=name,
        model=model,
        ip_address="10.0.0.1",
        access_code="x",
        serial_number=f"SN{printer_id}",
    )
    db.add(p)
    await db.commit()
    return p


async def _farm_item(db, printer_id, *, subtask="task-1", status="printing", eject_profile_id=5):
    batch = PrintBatch(name="run", sku_file_id=1, status="active")
    db.add(batch)
    await db.flush()
    item = PrintQueueItem(
        printer_id=printer_id,
        batch_id=batch.id,
        status=status,
        dispatch_subtask_id=subtask,
        eject_profile_id=eject_profile_id,
        started_at=datetime.utcnow(),
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


async def _geometry(db, model_key="H2S", *, z_reference_validated=False):
    row = PrinterModelGeometry(
        model_key=model_key,
        bed_x=340.0,
        bed_y=320.0,
        env_x_min=0.0,
        env_x_max=340.0,
        env_y_min=0.0,
        env_y_max=320.0,
        max_part_height_mm=42.0,
        z_travel_mm=340.0,
        validated=True,
        z_reference_validated=z_reference_validated,
    )
    db.add(row)
    await db.commit()
    return row


async def _open_incidents(db, printer_id):
    from sqlalchemy import select

    result = await db.execute(
        select(PrinterIncident).where(PrinterIncident.printer_id == printer_id).order_by(PrinterIncident.id)
    )
    return list(result.scalars().all())


# --- (a) stand down ---------------------------------------------------------


class TestStandDown:
    """Branch (a): the state we decided on is no longer the state on the wire."""

    async def test_disconnected_printer_is_not_resumed(self, monkeypatch, db_session):
        state = _make_state()
        client = FakeClient(state)
        _wire(monkeypatch, state, client)
        pause_recovery.note_status_push(1, state)
        state.connected = False  # dropped between the trip and the settle
        await pause_recovery._in_flight[1]

        assert client.calls == []
        assert await _open_incidents(db_session, 1) == []

    async def test_no_longer_paused_is_not_resumed(self, monkeypatch, db_session):
        state = _make_state()
        client = FakeClient(state)
        _wire(monkeypatch, state, client)
        pause_recovery.note_status_push(2, state)
        state.state = "RUNNING"  # the operator resumed at the screen
        await pause_recovery._in_flight[2]

        assert client.calls == []
        assert await _open_incidents(db_session, 2) == []

    async def test_prompt_gone_is_not_resumed(self, monkeypatch, db_session):
        state = _make_state()
        client = FakeClient(state)
        _wire(monkeypatch, state, client)
        pause_recovery.note_status_push(3, state)
        state.hms_errors = []  # the prompt was answered elsewhere
        await pause_recovery._in_flight[3]

        assert client.calls == []
        assert await _open_incidents(db_session, 3) == []

    async def test_job_changed_is_not_resumed(self, monkeypatch, db_session):
        """The printer moved on: the id we decided on is not the id it holds now."""
        state = _make_state(subtask="task-1")
        client = FakeClient(state)
        _wire(monkeypatch, state, client)
        pause_recovery.note_status_push(4, state)
        state.subtask_id = "task-9"
        await pause_recovery._in_flight[4]

        assert client.calls == []
        assert await _open_incidents(db_session, 4) == []

    async def test_operator_beat_us_raises_no_page(self, monkeypatch, db_session):
        page = _spy(monkeypatch, "on_power_loss_hold")
        state = _make_state()
        client = FakeClient(state)
        _wire(monkeypatch, state, client)
        pause_recovery.note_status_push(5, state)
        state.state = "RUNNING"
        await pause_recovery._in_flight[5]

        page.assert_not_awaited()
        assert pause_recovery._summary is None  # nothing was decided, so no window opened


# --- (b) interrupted eject --------------------------------------------------


class TestInterruptedEject:
    """Branch (b): a sweep is stopped, never resumed — and the plate is held."""

    @staticmethod
    def _claim_eject(printer_id):
        plate_occupancy.hydrate_eject(
            printer_id,
            PendingEject(
                purpose="production",
                run_id=None,
                queue_item_id=None,
                dispatched_at=datetime.now(timezone.utc),
                started_at=None,
                hydrated=True,
            ),
        )

    async def test_delegates_to_the_eject_lane_and_never_resumes(self, monkeypatch, db_session):
        await _printer(db_session, 6)
        await _geometry(db_session)
        redrive = AsyncMock(return_value=True)
        monkeypatch.setattr("backend.app.services.eject.remote.redrive_eject_stop", redrive)
        _spy(monkeypatch, "on_z_reference_lost")
        state = _make_state()
        client = FakeClient(state)
        _wire(monkeypatch, state, client)
        self._claim_eject(6)

        await _drive(6, state)

        redrive.assert_awaited_once()
        assert redrive.await_args.kwargs["stage"] == "power_loss"
        assert client.calls == []  # a sweep is NEVER resumed
        # The lane writes no occupancy state of its own — the eject lane owns that.
        assert plate_occupancy.eject_identity(6) is not None

    async def test_also_holds_the_lost_z_reference(self, monkeypatch, db_session):
        await _printer(db_session, 7)
        await _geometry(db_session)  # z_reference_validated False
        monkeypatch.setattr("backend.app.services.eject.remote.redrive_eject_stop", AsyncMock(return_value=True))
        page = _spy(monkeypatch, "on_z_reference_lost")
        state = _make_state()
        _wire(monkeypatch, state, FakeClient(state))
        self._claim_eject(7)

        await _drive(7, state)

        rows = await _open_incidents(db_session, 7)
        assert [r.kind for r in rows] == [KIND_Z_REFERENCE_LOST]
        assert rows[0].status == STATUS_ESCALATED
        assert rows[0].item_id is None
        page.assert_awaited_once()

    async def test_a_laddered_model_earns_no_lost_z_hold(self, monkeypatch, db_session):
        await _printer(db_session, 8)
        await _geometry(db_session, z_reference_validated=True)
        monkeypatch.setattr("backend.app.services.eject.remote.redrive_eject_stop", AsyncMock(return_value=True))
        page = _spy(monkeypatch, "on_z_reference_lost")
        state = _make_state()
        _wire(monkeypatch, state, FakeClient(state))
        self._claim_eject(8)

        await _drive(8, state)

        assert await _open_incidents(db_session, 8) == []
        page.assert_not_awaited()


# --- (c) another lane owns the printer --------------------------------------


class TestOpenIncidentStandsAside:
    async def test_any_open_incident_stops_the_lane_opening_anything(self, monkeypatch, db_session):
        await _printer(db_session, 9)
        await printer_incidents.open_new(
            db_session,
            printer_id=9,
            job_id="task-1",
            item_id=None,
            kind=KIND_RUNOUT,
            code="0700_8011",
            codes="0700_8011",
            slot_global_tray=None,
            status=STATUS_ESCALATED,
        )
        page = _spy(monkeypatch, "on_power_loss_hold")
        state = _make_state()
        client = FakeClient(state)
        _wire(monkeypatch, state, client)

        await _drive(9, state)

        assert client.calls == []  # the refill lane's resume answers the prompt too
        rows = await _open_incidents(db_session, 9)
        assert [r.kind for r in rows] == [KIND_RUNOUT]
        page.assert_not_awaited()
        assert pause_recovery._summary.held_by_fault == {9}


# --- (d) resume -------------------------------------------------------------


class TestResume:
    async def test_farm_unit_resumes_with_one_command_and_no_incident(self, monkeypatch, db_session, caplog):
        await _printer(db_session, 10)
        await _farm_item(db_session, 10)
        page = _spy(monkeypatch, "on_power_loss_hold")
        state = _make_state()
        client = FakeClient(state)
        _wire(monkeypatch, state, client)

        with caplog.at_level("INFO"):
            await _drive(10, state)

        assert client.calls == [("resume",)]
        assert state.state == "RUNNING"
        assert await _open_incidents(db_session, 10) == []
        page.assert_not_awaited()
        assert "RESUMED after power loss" in caplog.text
        assert "origin farm" in caplog.text
        assert pause_recovery._summary.resumed == {10}

    async def test_a_foreign_print_gets_the_same_resume(self, monkeypatch, db_session, caplog):
        """Operator ruling 2026-09-04: parity — a print the farm did not dispatch is
        resumed under exactly the same guards."""
        await _printer(db_session, 11)
        state = _make_state()
        client = FakeClient(state)
        _wire(monkeypatch, state, client)

        with caplog.at_level("INFO"):
            await _drive(11, state)

        assert client.calls == [("resume",)]
        assert await _open_incidents(db_session, 11) == []
        assert "origin foreign" in caplog.text

    async def test_a_refused_send_is_retried_once_and_then_succeeds(self, monkeypatch, db_session):
        """A False return is a send that did not go out (session mid-churn), not a
        firmware refusal — one retry, then the ordinary confirm."""
        await _printer(db_session, 12)
        state = _make_state()
        client = FakeClient(state, resume_false_first=1)
        _wire(monkeypatch, state, client)

        await _drive(12, state)

        assert client.calls == [("resume",), ("resume",)]
        assert state.state == "RUNNING"
        assert await _open_incidents(db_session, 12) == []

    async def test_two_refused_sends_hold_without_a_third(self, monkeypatch, db_session):
        await _printer(db_session, 13)
        item = await _farm_item(db_session, 13)
        page = _spy(monkeypatch, "on_power_loss_hold")
        state = _make_state()
        client = FakeClient(state, resume_ret=False)
        _wire(monkeypatch, state, client)

        await _drive(13, state)

        assert client.calls == [("resume",), ("resume",)]
        rows = await _open_incidents(db_session, 13)
        assert [(r.kind, r.status) for r in rows] == [(KIND_POWER_LOSS, STATUS_ESCALATED)]
        assert rows[0].item_id == item.id
        await db_session.refresh(item)
        assert item.waiting_reason == WAITING_REASON_POWER_LOSS
        assert page.await_args.kwargs["reason"] == pause_recovery._HOLD_REASON_COPY["resume_refused"]

    async def test_the_firmware_declaring_the_resume_failed_holds_with_no_retry(self, monkeypatch, db_session):
        """0300_400D is the catalog's own verdict on the command just sent: a second
        resume cannot un-fail the firmware's own recovery."""
        await _printer(db_session, 14)
        page = _spy(monkeypatch, "on_power_loss_hold")
        state = _make_state()
        client = FakeClient(state, resume_reaches_running=False)

        def _status(_pid):
            if client.calls:
                state.hms_errors = [_prompt_hms(), _resume_failed_hms()]
            return state

        monkeypatch.setattr(pause_recovery.printer_manager, "get_status", _status)
        monkeypatch.setattr(pause_recovery.printer_manager, "get_client", lambda _pid: client)
        monkeypatch.setattr(pause_recovery.printer_manager, "get_all_statuses", dict)

        await _drive(14, state)

        assert client.calls == [("resume",)]  # accepted, then declared failed — no retry
        rows = await _open_incidents(db_session, 14)
        assert [r.kind for r in rows] == [KIND_POWER_LOSS]
        assert rows[0].code == "0300_8007"
        assert "0300_400D" in rows[0].codes
        assert page.await_args.kwargs["reason"] == pause_recovery._HOLD_REASON_COPY["resume_failed"]

    async def test_an_accepted_resume_that_never_runs_holds_and_pages(self, monkeypatch, db_session):
        await _printer(db_session, 15)
        page = _spy(monkeypatch, "on_power_loss_hold")
        state = _make_state()
        client = FakeClient(state, resume_reaches_running=False)
        _wire(monkeypatch, state, client)

        await _drive(15, state)

        assert client.calls == [("resume",)]
        rows = await _open_incidents(db_session, 15)
        assert [r.kind for r in rows] == [KIND_POWER_LOSS]
        assert rows[0].codes == "0300_8007"  # 400D never stood, so it is not recorded
        page.assert_awaited_once()
        assert pause_recovery._summary.held == {15}

    async def test_a_hold_never_quarantines(self, monkeypatch, db_session):
        """The printer is left exactly where the firmware put it. A power-loss prompt
        is a PAUSE, not a failure, and nothing about it is the printer's fault."""
        from backend.app.services.printer_manager import printer_manager as real_manager

        await _printer(db_session, 16)
        _spy(monkeypatch, "on_power_loss_hold")
        state = _make_state()
        _wire(monkeypatch, state, FakeClient(state, resume_reaches_running=False))

        await _drive(16, state)

        assert real_manager.is_quarantined(16) is False


# --- sampler behaviour ------------------------------------------------------


class TestSampler:
    async def test_one_driver_per_printer_while_the_prompt_stands(self, monkeypatch, db_session):
        await _printer(db_session, 17)
        state = _make_state()
        client = FakeClient(state, resume_reaches_running=False)
        _wire(monkeypatch, state, client)
        _spy(monkeypatch, "on_power_loss_hold")

        for _ in range(5):  # five ~1 Hz pushes with the prompt standing
            pause_recovery.note_status_push(17, state)
        await pause_recovery._in_flight[17]

        assert client.calls == [("resume",)]  # ONE driver, not five

    async def test_a_prompt_that_keeps_standing_after_the_decision_spawns_nothing_more(self, monkeypatch, db_session):
        """After the driver decided (here: the resume was refused -> a HOLD), the prompt
        still stands on every later push. Those pushes are LEVEL, not edge: no new
        driver, no second resume, no second page. A prompt that clears and comes
        back is a new edge and earns a new decision."""
        await _printer(db_session, 23)
        state = _make_state()
        client = FakeClient(state, resume_reaches_running=False)
        _wire(monkeypatch, state, client)
        paged = _spy(monkeypatch, "on_power_loss_hold")

        await _drive(23, state)
        assert client.calls == [("resume",)]
        assert paged.await_count == 1

        for _ in range(20):  # twenty more ~1 Hz pushes, the prompt still standing
            pause_recovery.note_status_push(23, state)
            assert pause_recovery._in_flight.get(23) is None
        assert client.calls == [("resume",)]
        assert paged.await_count == 1

        # The prompt clears (operator resumed on the screen) and later re-appears:
        # a fresh edge — but the hold row on disk makes the new driver stand aside.
        cleared = _make_state(gcode_state="RUNNING", hms=[])
        pause_recovery.note_status_push(23, cleared)
        await _drive(23, state)
        assert client.calls == [("resume",)]  # stood aside on the open incident

    async def test_a_restart_re_derives_from_the_wire_and_makes_one_attempt(self, monkeypatch, db_session):
        """Empty ``_in_flight`` plus a standing prompt is exactly what a restart sees;
        the incident row is what "already decided" means, and there is none yet."""
        await _printer(db_session, 18)
        state = _make_state()
        client = FakeClient(state)
        _wire(monkeypatch, state, client)

        assert pause_recovery._in_flight == {}
        await _drive(18, state)

        assert client.calls == [("resume",)]

    async def test_a_restart_with_the_hold_already_on_disk_stands_aside(self, monkeypatch, db_session):
        await _printer(db_session, 19)
        await printer_incidents.open_new(
            db_session,
            printer_id=19,
            job_id="task-1",
            item_id=None,
            kind=KIND_POWER_LOSS,
            code="0300_8007",
            codes="0300_8007",
            slot_global_tray=None,
            status=STATUS_ESCALATED,
        )
        state = _make_state()
        client = FakeClient(state)
        _wire(monkeypatch, state, client)

        await _drive(19, state)

        assert client.calls == []
        assert len(await _open_incidents(db_session, 19)) == 1

    async def test_a_healthy_push_spawns_nothing(self, monkeypatch, db_session):
        state = _make_state(gcode_state="RUNNING", hms=[])
        _wire(monkeypatch, state, FakeClient(state))

        pause_recovery.note_status_push(20, state)

        assert pause_recovery._in_flight == {}

    async def test_the_sampler_never_raises(self, monkeypatch, caplog):
        """Invariant 10: no farm-side failure may crash the MQTT status flow."""

        class Exploding:
            @property
            def connection_epoch(self):
                raise RuntimeError("wire is on fire")

        with caplog.at_level("ERROR"):
            pause_recovery.note_status_push(21, Exploding())

        assert "status sampler failed" in caplog.text


# --- the lost-Z outage arm --------------------------------------------------


class TestOutageBurst:
    """W6a's opening half: the discriminator is a FLEET fact, not a session boundary."""

    @staticmethod
    def _fleet(anchor, n):
        return {pid: _make_state(disconnected_at=anchor + pid) for pid in range(1, n + 1)}

    async def test_a_burst_holds_an_occupied_unvalidated_printer(self, monkeypatch, db_session):
        await _printer(db_session, 22)
        await _geometry(db_session)
        page = _spy(monkeypatch, "on_z_reference_lost")
        anchor = time.time() - 300.0
        state = _make_state(gcode_state="IDLE", hms=[], epoch=2, disconnected_at=anchor)
        _wire(monkeypatch, state, None, fleet=self._fleet(anchor, 4))
        plate_occupancy.note_plate_detected(22, "part on the plate")

        pause_recovery.note_status_push(22, _make_state(gcode_state="IDLE", hms=[], epoch=1, disconnected_at=anchor))
        pause_recovery.note_status_push(22, state)  # epoch advanced == the reconnect edge
        await _drain_z_arm(22)

        rows = await _open_incidents(db_session, 22)
        assert [r.kind for r in rows] == [KIND_Z_REFERENCE_LOST]
        assert rows[0].status == STATUS_ESCALATED
        page.assert_awaited_once()
        # The gate the operator must clear is still the human-clear-only one.
        assert isinstance(plate_occupancy.snapshot(22).plate_policy, EscalationOnly)

    async def test_a_single_printer_reconnect_never_opens_it(self, monkeypatch, db_session):
        await _printer(db_session, 23)
        await _geometry(db_session)
        page = _spy(monkeypatch, "on_z_reference_lost")
        anchor = time.time() - 300.0
        state = _make_state(gcode_state="IDLE", hms=[], epoch=2, disconnected_at=anchor)
        _wire(monkeypatch, state, None, fleet={23: state})
        plate_occupancy.note_plate_detected(23, "part on the plate")

        pause_recovery.note_status_push(23, _make_state(gcode_state="IDLE", hms=[], epoch=1, disconnected_at=anchor))
        pause_recovery.note_status_push(23, state)
        await _drain_z_arm(23)

        assert await _open_incidents(db_session, 23) == []
        page.assert_not_awaited()

    async def test_a_clear_plate_earns_no_hold(self, monkeypatch, db_session):
        await _printer(db_session, 24)
        await _geometry(db_session)
        anchor = time.time() - 300.0
        state = _make_state(gcode_state="IDLE", hms=[], epoch=2, disconnected_at=anchor)
        _wire(monkeypatch, state, None, fleet=self._fleet(anchor, 4))

        pause_recovery.note_status_push(24, _make_state(gcode_state="IDLE", hms=[], epoch=1, disconnected_at=anchor))
        pause_recovery.note_status_push(24, state)
        await _drain_z_arm(24)

        assert await _open_incidents(db_session, 24) == []

    async def test_a_laddered_model_earns_no_hold(self, monkeypatch, db_session):
        await _printer(db_session, 25)
        await _geometry(db_session, z_reference_validated=True)
        anchor = time.time() - 300.0
        state = _make_state(gcode_state="IDLE", hms=[], epoch=2, disconnected_at=anchor)
        _wire(monkeypatch, state, None, fleet=self._fleet(anchor, 4))
        plate_occupancy.note_plate_detected(25, "part on the plate")

        pause_recovery.note_status_push(25, _make_state(gcode_state="IDLE", hms=[], epoch=1, disconnected_at=anchor))
        pause_recovery.note_status_push(25, state)
        await _drain_z_arm(25)

        assert await _open_incidents(db_session, 25) == []

    async def test_an_active_job_owns_the_printer_instead(self, monkeypatch, db_session):
        """A printer at the power-loss prompt is PAUSE and belongs to the resume
        branch; a resumed print re-homes its own axes before it moves again."""
        await _printer(db_session, 26)
        await _geometry(db_session)
        anchor = time.time() - 300.0
        state = _make_state(gcode_state="PAUSE", epoch=2, disconnected_at=anchor)
        client = FakeClient(state)
        _wire(monkeypatch, state, client, fleet=self._fleet(anchor, 4))
        plate_occupancy.note_plate_detected(26, "part on the plate")

        pause_recovery.note_status_push(26, _make_state(gcode_state="IDLE", hms=[], epoch=1, disconnected_at=anchor))
        await _drive(26, state)
        await _drain_z_arm(26)

        assert [r.kind for r in await _open_incidents(db_session, 26)] == []


# --- the fleet summary ------------------------------------------------------


class TestSummary:
    async def test_one_page_per_outage_window_with_the_counts(self, monkeypatch, db_session):
        # A window long enough to span two printers' decisions — the aggregation IS
        # what this test is about, so the collapsed default would defeat it.
        monkeypatch.setattr(pause_recovery, "_SUMMARY_WINDOW_S", 0.5)
        await _printer(db_session, 27, name="a")
        await _printer(db_session, 28, name="b")
        summary = _spy(monkeypatch, "on_power_loss_recovery_summary")
        _spy(monkeypatch, "on_power_loss_hold")

        good = _make_state()
        _wire(monkeypatch, good, FakeClient(good))
        await _drive(27, good)

        bad = _make_state()
        _wire(monkeypatch, bad, FakeClient(bad, resume_reaches_running=False))
        await _drive(28, bad)

        await _drain_summary()

        summary.assert_awaited_once()
        kwargs = summary.await_args.kwargs
        assert (kwargs["resumed"], kwargs["held"]) == (1, 1)
        assert (kwargs["stopped_ejects"], kwargs["held_by_fault"]) == (0, 0)

    async def test_the_window_close_always_logs_even_when_nothing_pages(self, monkeypatch, db_session, caplog):
        """A suppressed page and a dead lane must not be indistinguishable (F9)."""
        await _printer(db_session, 29)
        monkeypatch.setattr(
            "backend.app.services.notification_service.notification_service.on_power_loss_recovery_summary",
            AsyncMock(side_effect=RuntimeError("no providers, and then some")),
        )
        state = _make_state()
        _wire(monkeypatch, state, FakeClient(state))

        with caplog.at_level("INFO"):
            await _drive(29, state)
            await _drain_summary()

        assert "outage window closed" in caplog.text
        assert "resumed=[29]" in caplog.text

    async def test_the_outage_duration_is_measured_from_the_reconnect_edge(self, monkeypatch, db_session):
        await _printer(db_session, 30)
        summary = _spy(monkeypatch, "on_power_loss_recovery_summary")
        anchor = time.time() - 600.0
        first = _make_state(gcode_state="IDLE", hms=[], epoch=1, disconnected_at=anchor)
        state = _make_state(epoch=2, disconnected_at=anchor)
        _wire(monkeypatch, state, FakeClient(state), fleet={30: state})

        pause_recovery.note_status_push(30, first)
        await _drive(30, state)
        await _drain_summary()

        assert summary.await_args.kwargs["outage_minutes"] == 10

    async def test_an_unwitnessed_outage_reports_no_duration(self, monkeypatch, db_session):
        """A restart mid-outage erases the measurement; None renders as "no sentence"
        rather than as a plausible wrong number."""
        await _printer(db_session, 31)
        summary = _spy(monkeypatch, "on_power_loss_recovery_summary")
        state = _make_state()
        _wire(monkeypatch, state, FakeClient(state))

        await _drive(31, state)
        await _drain_summary()

        assert summary.await_args.kwargs["outage_minutes"] is None


# --- the plate-vision lane --------------------------------------------------


class TestPlateVisionTrip:
    async def test_the_mark_is_on_disk_before_the_stop_goes_out(self, monkeypatch, db_session, _own_sessions):
        """The ordering contract every downstream decision rests on: the terminal the
        stop produces must find the farm's abort already recorded."""
        await _printer(db_session, 32)
        item = await _farm_item(db_session, 32)
        state = _make_state(gcode_state="PAUSE", hms=[_vision_hms()])
        calls = _wire(monkeypatch, state, None)

        seen = {}
        real = pause_recovery._stop_for_vision

        async def _spy_stop(pid):
            async with _own_sessions() as db:
                row = await db.get(PrintQueueItem, item.id)
                seen["mark_at_stop"] = row.stop_source
            return await real(pid)

        monkeypatch.setattr(pause_recovery, "_stop_for_vision", _spy_stop)

        assert await pause_recovery.on_plate_vision_trip(32, {"0500_808C"}) is True

        assert seen["mark_at_stop"] == "farm_vision_abort"
        assert calls == [("stop", 32)]

    async def test_the_trip_opens_a_recovering_incident_bound_to_the_unit(self, monkeypatch, db_session):
        await _printer(db_session, 33)
        item = await _farm_item(db_session, 33)
        state = _make_state(gcode_state="PAUSE", hms=[_vision_hms()])
        _wire(monkeypatch, state, None)

        await pause_recovery.on_plate_vision_trip(33, {"0500_808C", "0500_806E"})

        rows = await _open_incidents(db_session, 33)
        assert [(r.kind, r.status) for r in rows] == [(KIND_PLATE_VISION, STATUS_RECOVERING)]
        assert rows[0].item_id == item.id
        assert rows[0].job_id == "task-1"
        assert rows[0].code == "0500_806E"  # sorted first
        assert rows[0].codes == "0500_806E,0500_808C"

    async def test_a_foreign_trip_records_the_intent_on_the_incident(self, monkeypatch, db_session):
        """No row to stamp — the incident IS the record, and item_id NULL is the
        established foreign shape."""
        await _printer(db_session, 34)
        state = _make_state(gcode_state="PAUSE", hms=[_vision_hms()], subtask="foreign-9")
        calls = _wire(monkeypatch, state, None)

        assert await pause_recovery.on_plate_vision_trip(34, {"0500_808C"}) is True

        rows = await _open_incidents(db_session, 34)
        assert [(r.kind, r.item_id, r.job_id) for r in rows] == [(KIND_PLATE_VISION, None, "foreign-9")]
        assert calls == [("stop", 34)]

    async def test_a_refused_stop_is_retried_once(self, monkeypatch, db_session):
        await _printer(db_session, 35)
        await _farm_item(db_session, 35)
        state = _make_state(gcode_state="PAUSE", hms=[_vision_hms()])
        _wire(monkeypatch, state, None)
        sends = []

        def _stop(pid):
            sends.append(pid)
            return len(sends) > 1

        monkeypatch.setattr(pause_recovery.printer_manager, "stop_print", _stop)

        await pause_recovery.on_plate_vision_trip(35, {"0500_808C"})

        assert sends == [35, 35]

    async def test_the_lane_never_raises_a_plate_gate(self, monkeypatch, db_session):
        """The gate belongs AFTER the terminal (farm_policy), which is the only lane
        that can tell a first trip from a confirmed one — and raising it here lets
        note_terminal replace it with a CooldownEject built for the wrong part."""
        await _printer(db_session, 36)
        await _farm_item(db_session, 36)
        state = _make_state(gcode_state="PAUSE", hms=[_vision_hms()])
        _wire(monkeypatch, state, None)

        await pause_recovery.on_plate_vision_trip(36, {"0500_808C"})

        assert plate_occupancy.is_plate_occupied(36) is False

    async def test_an_open_incident_makes_the_lane_stand_aside(self, monkeypatch, db_session):
        await _printer(db_session, 37)
        await printer_incidents.open_new(
            db_session,
            printer_id=37,
            job_id="task-1",
            item_id=None,
            kind=KIND_RUNOUT,
            code="0700_8011",
            codes="0700_8011",
            slot_global_tray=None,
            status=STATUS_ESCALATED,
        )
        item = await _farm_item(db_session, 37)
        state = _make_state(gcode_state="PAUSE", hms=[_vision_hms()])
        calls = _wire(monkeypatch, state, None)

        assert await pause_recovery.on_plate_vision_trip(37, {"0500_808C"}) is False

        assert calls == []
        await db_session.refresh(item)
        assert item.stop_source is None

    async def test_the_hook_never_raises(self, monkeypatch, caplog):
        monkeypatch.setattr(
            pause_recovery.printer_manager,
            "get_status",
            lambda _pid: (_ for _ in ()).throw(RuntimeError("wire is on fire")),
        )
        with caplog.at_level("ERROR"):
            assert await pause_recovery.on_plate_vision_trip(38, {"0500_808C"}) is False
        assert "plate-vision trip handling failed" in caplog.text


# --- the operator's clear ---------------------------------------------------


class TestOnPlateCleared:
    async def test_it_closes_an_operator_resolved_hold(self, db_session):
        await _printer(db_session, 39)
        await printer_incidents.open_new(
            db_session,
            printer_id=39,
            job_id="",
            item_id=None,
            kind=KIND_Z_REFERENCE_LOST,
            code="power_loss",
            codes="",
            slot_global_tray=None,
            status=STATUS_ESCALATED,
        )

        assert await pause_recovery.on_plate_cleared(39) is True

        assert await printer_incidents.get_open(db_session, 39) is None

    async def test_it_leaves_a_wire_resolved_hold_alone(self, db_session):
        """Clearing a plate is not evidence that a runout was refilled."""
        await _printer(db_session, 40)
        await printer_incidents.open_new(
            db_session,
            printer_id=40,
            job_id="",
            item_id=None,
            kind=KIND_RUNOUT,
            code="0700_8011",
            codes="0700_8011",
            slot_global_tray=None,
            status=STATUS_ESCALATED,
        )

        assert await pause_recovery.on_plate_cleared(40) is False

        assert (await printer_incidents.get_open(db_session, 40)) is not None

    async def test_no_open_incident_is_not_an_error(self, db_session):
        assert await pause_recovery.on_plate_cleared(41) is False
