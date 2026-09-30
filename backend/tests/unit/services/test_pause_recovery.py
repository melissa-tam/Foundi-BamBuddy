"""Branch matrix for the pause-recovery lane (2026-09-04 fleet outage + the plate-check ladder).

Drives the real entry points — the ~1 Hz sampler ``note_status_push`` (the ONLY trigger of
both causes) and the operator's ``on_plate_cleared`` — against a real ``PrinterState``
mutated by a scripted client, in the ``test_spool_recovery`` idiom. Every branch of
``_recover_power_loss`` gets a case, in its own order; the lost-Z outage arm gets the burst /
single-reconnect / laddered-model triple; and the plate-check episode is walked down the
ladder of operator rulings 2026-09-04 / 2026-09-29 — the trip, "Problem solved, resume" and
its ACK, the re-check's pass / fail / hand-over, the farm's stop and its answers, the
fallback hold, and re-entry after a restart from the step ledger alone.
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
from backend.app.services.plate_occupancy import EscalationOnly, Evidence, PendingEject, plate_occupancy
from backend.app.services.printer_incidents import WAITING_REASON_POWER_LOSS
from backend.app.services.terminal_outcome import PLATE_RECHECK_WINDOW_S, PlateCheckFacts

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
    monkeypatch.setattr(pause_recovery, "_RECHECK_ACK_S", 0.05)
    monkeypatch.setattr(pause_recovery, "_RECHECK_ACK_POLL_S", 0.005)
    monkeypatch.setattr(pause_recovery, "_RECHECK_CONFIRM_S", 0.1)
    monkeypatch.setattr(pause_recovery, "_RECHECK_FIRST_LAYER_S", 0.2)
    monkeypatch.setattr(pause_recovery, "_STOP_CONFIRM_S", 0.1)
    monkeypatch.setattr(pause_recovery, "_VISION_STOP_RETRY_S", 0.0)
    monkeypatch.setattr(pause_recovery, "_EPISODE_POLL_S", 0.005)


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
    st.report_epoch = epoch  # this session's first report applied (``print_reconcile.is_fresh``)
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


def _occupy_plate(printer_id):
    """A part on the plate, by the operator's own statement (the short way)."""
    assert plate_occupancy.declare_occupied(printer_id, Evidence()) is None


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

    async def test_the_lost_z_hold_opens_beside_an_ams_fault(self, monkeypatch, db_session):
        """Multi-alarm rule (2026-09-11). Before it, ``open_new`` handed this lane
        ``None`` on a printer already holding a jam, and ``z_reference_evidence`` then
        let an eject run against a fabricated Z datum (the 2026-09-04 bed-past-the-
        floor mechanism)."""
        from backend.app.models.printer_incident import KIND_JAM
        from backend.app.services.eject.remote import z_reference_evidence

        await _printer(db_session, 10)
        await _geometry(db_session)
        assert (
            await printer_incidents.open_new(
                db_session,
                printer_id=10,
                job_id="task-1",
                item_id=None,
                kind=KIND_JAM,
                code="0700_8010",
                codes="0700_8010",
                slot_global_tray=None,
                status=STATUS_ESCALATED,
            )
            is not None
        )
        monkeypatch.setattr("backend.app.services.eject.remote.redrive_eject_stop", AsyncMock(return_value=True))
        page = _spy(monkeypatch, "on_z_reference_lost")
        state = _make_state()
        _wire(monkeypatch, state, FakeClient(state))
        self._claim_eject(10)

        await _drive(10, state)

        assert {r.kind for r in await _open_incidents(db_session, 10)} == {KIND_JAM, KIND_Z_REFERENCE_LOST}
        page.assert_awaited_once()
        assert z_reference_evidence(10) is False  # asked BY KIND, so the jam cannot hide it

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


class TestAJobPauseStandsThePowerLossDriverAside:
    """No other lane resumes a job its plate check paused. The printer's plate check paused
    this job, and a power cut landed during that pause: answering the firmware's prompt with a
    resume would answer the plate question too, onto the plate the printer refused, with no
    re-check. The plate-check episode (here escalated: the human's) answers it. Liveness-paired:
    the same prompt with NO job-pause hold still resumes."""

    async def _plate_check_hold(self, db, printer_id):
        await printer_incidents.open_new(
            db,
            printer_id=printer_id,
            job_id="task-1",
            item_id=None,
            kind=KIND_PLATE_VISION,
            code="0500_808C",
            codes="0500_808C",
            slot_global_tray=None,
            status=STATUS_ESCALATED,
        )

    async def test_the_prompt_during_a_plate_check_pause_is_left_to_the_operator(self, monkeypatch, db_session):
        await _printer(db_session, 60)
        await self._plate_check_hold(db_session, 60)
        state = _make_state(hms=[_prompt_hms(), _vision_hms()])
        client = FakeClient(state)
        _wire(monkeypatch, state, client)

        await _drive(60, state)

        assert client.calls == []
        assert [r.kind for r in await _open_incidents(db_session, 60)] == [KIND_PLATE_VISION]
        assert pause_recovery._summary.held_by_fault == {60}

    async def test_without_a_job_pause_the_same_prompt_still_resumes(self, monkeypatch, db_session):
        await _printer(db_session, 61)
        state = _make_state(hms=[_prompt_hms()])
        client = FakeClient(state)
        _wire(monkeypatch, state, client)

        await _drive(61, state)

        assert client.calls == [("resume",)]
        assert pause_recovery._summary.resumed == {61}


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
    """Opening the lost-Z-reference hold: the discriminator is a FLEET fact, not a session boundary."""

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
        _occupy_plate(22)

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
        _occupy_plate(23)

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
        _occupy_plate(25)

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
        _occupy_plate(26)

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


# --- the plate-check episode (the ladder, operator rulings 2026-09-04 / 2026-09-29) ---

# The build-plate offset dialog, the dominant production trip — as ``print_error`` carries it.
_PLATE_ERR = 0x0500808C
# The firmware's power-loss prompt, as ``print_error`` carries it after a reboot.
_POWER_LOSS_ERR = 0x03008007


def _plate_state(*, subtask="task-1", print_error=_PLATE_ERR, gcode_state="PAUSE", hms=None, fresh=True):
    """A printer PAUSEd at its plate check: the dialog on ``print_error`` (the wire production
    sees), a fresh report."""
    st = _make_state(subtask=subtask, gcode_state=gcode_state, hms=hms if hms is not None else [])
    st.print_error = print_error
    if not fresh:
        st.report_epoch = None
    return st


def _set(**fields):
    """One scripted wire answer: set these fields (``layer`` is the client's job layer, read
    through its ``job_peaks``)."""

    def apply(wire):
        for name, value in fields.items():
            if name == "layer":
                wire.layer = value
            else:
                setattr(wire.state, name, value)

    return apply


class PlateWire:
    """The scripted printer of the plate-check ladder: one live ``PrinterState``, the client the
    episode presses and reads through, and the raw stop.

    ``after_press`` / ``after_stop`` / ``after_refused_stop`` are the wire's answers to a send:
    mutations applied one per ``get_status`` read after it (the last one holds), so a test
    scripts what the driver's own polls see, never a clock."""

    def __init__(
        self,
        state,
        *,
        press_sent=True,
        ack="success",
        layer=1,
        stop_sent=(True,),
        after_press=(_set(state="RUNNING", print_error=0),),
        after_stop=(_set(state="IDLE", print_error=0),),
        after_refused_stop=(),
    ):
        self.state = state
        self.press_sent = press_sent
        self.ack = ack
        self.layer = layer
        self.stop_sent = stop_sent
        self.after_press = list(after_press)
        self.after_stop = list(after_stop)
        self.after_refused_stop = list(after_refused_stop)
        self.presses: list[tuple] = []
        self.stops: list[int] = []
        self.resumes = 0
        self._pending: list = []

    # --- printer_manager ---
    def get_status(self, _pid):
        if self._pending:
            self._pending.pop(0)(self)
        return self.state

    def stop_print(self, printer_id):
        sent = self.stop_sent[min(len(self.stops), len(self.stop_sent) - 1)]
        self.stops.append(printer_id)
        self._pending = list(self.after_stop if sent else self.after_refused_stop)
        return sent

    # --- the client ---
    def execute_hms_action(self, print_error, action, job_id):
        from backend.app.services.bambu_mqtt import SentCommand

        self.presses.append((print_error, action, job_id))
        if not self.press_sent:
            return None
        self._pending = list(self.after_press)
        return SentCommand(command="resume", sequence_id=str(len(self.presses)))

    async def await_ack(self, sent, budget_s, poll_s):
        from backend.app.services.bambu_mqtt import CommandAck

        if self.ack is None:
            return None
        return CommandAck(command=sent.command, sequence_id=sent.sequence_id, result=self.ack, reason=None, at=0.0)

    def job_peaks(self):
        """The client's one peaks reader: ``layer`` None is a reading this client did not
        measure (``reliable`` False — it attached the job mid-flight)."""
        from backend.app.services.bambu_mqtt import JobPeaks

        return JobPeaks(
            last_progress=0.0,
            last_layer_num=0,
            progress=0.0,
            layer_num=self.layer or 0,
            reliable=self.layer is not None,
        )

    def resume_print(self):
        """The power-loss lane's plain resume (the hand-over re-arm test)."""
        self.resumes += 1
        self.state.state = "RUNNING"
        return True

    def install(self, monkeypatch):
        monkeypatch.setattr(pause_recovery.printer_manager, "get_status", self.get_status)
        monkeypatch.setattr(pause_recovery.printer_manager, "get_client", lambda _pid: self)
        monkeypatch.setattr(pause_recovery.printer_manager, "get_all_statuses", dict)
        monkeypatch.setattr(pause_recovery.printer_manager, "stop_print", self.stop_print)
        return self


async def _drain_episode(printer_id):
    """Await the plate-check driver the sampler spawned, if it spawned one."""
    import backend.app.core.tasks as core_tasks

    name = f"plate-check-p{printer_id}"
    pending = [t for t in list(core_tasks._background_tasks) if t.get_name() == name and not t.done()]
    if pending:
        await asyncio.gather(*pending)


async def _episode_run(printer_id, state):
    """One sampler push, then the episode driver it spawned, to its end."""
    pause_recovery.note_status_push(printer_id, state)
    await _drain_episode(printer_id)


async def _plate_rows(db, printer_id):
    rows = await _open_incidents(db, printer_id)
    for row in rows:
        await db.refresh(row)
    return [row for row in rows if row.kind == KIND_PLATE_VISION]


async def _steps(db, incident_id):
    return [
        (s.kind, s.name, s.outcome) for s in (await pause_recovery._PlateCheckEvidence.from_row(db, incident_id)).steps
    ]


async def _restart(db):
    """What a deploy's restart leaves: no process state, the incident projection rebuilt from
    the rows (``rehydrate``), no driver."""
    pause_recovery._reset_state()
    printer_incidents._reset_state()
    await printer_incidents.rehydrate(db)


class TestPlateCheckTrip:
    """The ONE trigger: ``note_status_push`` on a fresh report of a printer PAUSEd with the plate
    dialog on ``print_error`` — never an HMS edge, never a leftover ``hms_errors`` entry."""

    async def test_a_plate_check_pause_opens_one_episode_bound_to_the_unit(self, monkeypatch, db_session, caplog):
        await _printer(db_session, 80)
        item = await _farm_item(db_session, 80)
        wire = PlateWire(_plate_state()).install(monkeypatch)

        with caplog.at_level("INFO", logger=pause_recovery.logger.name):
            await _episode_run(80, wire.state)

        (row,) = await _plate_rows(db_session, 80)
        assert (row.kind, row.job_id, row.item_id) == (KIND_PLATE_VISION, "task-1", item.id)
        assert (row.code, row.codes) == ("0500_808C", "0500_808C")
        assert f"plate check tripped 0500_808C (unit {item.id}, job task-1) — episode {row.id} opened" in caplog.text

    async def test_a_foreign_print_is_driven_the_same_way(self, monkeypatch, db_session):
        """No unit to bind (``item_id`` NULL) — the episode acts on the printer all the same."""
        await _printer(db_session, 79)
        wire = PlateWire(_plate_state(subtask="foreign-9")).install(monkeypatch)

        await _episode_run(79, wire.state)

        (row,) = await _plate_rows(db_session, 79)
        assert (row.job_id, row.item_id) == ("foreign-9", None)
        assert wire.presses == [("0500808C", "PROBLEM_SOLVED_RESUME", "foreign-9")]

    async def test_the_trip_records_the_printers_own_code_and_projects_the_hold(
        self, monkeypatch, db_session, _own_sessions
    ):
        """The dialog's full code (``print_error`` merged onto ``hms_errors``) is the printer's
        words after the dialog is gone; the unit carries the hold's waiting reason while the
        episode acts."""
        from backend.app.services.bambu_mqtt import HMSError

        await _printer(db_session, 81)
        item = await _farm_item(db_session, 81)
        merged = HMSError(code="0x808c", attr=_PLATE_ERR, module=5, severity=3, full_code="0500808C")
        seen: dict = {}
        wire = PlateWire(_plate_state(hms=[merged])).install(monkeypatch)
        real_ack = wire.await_ack

        async def _ack_and_read(sent, budget_s, poll_s):
            # Read while the press is out, before the re-check answers.
            async with _own_sessions() as s:
                seen["waiting_reason"] = (await s.get(PrintQueueItem, item.id)).waiting_reason
                seen["full"] = (await printer_incidents.get_open(s, 81, kinds={KIND_PLATE_VISION})).hms_full_codes
            return await real_ack(sent, budget_s, poll_s)

        wire.await_ack = _ack_and_read

        await _episode_run(81, wire.state)

        assert seen["full"] == "0500808C"
        assert seen["waiting_reason"] == "plate_not_empty_printer_detected"

    async def test_a_leftover_hms_plate_entry_with_no_dialog_does_not_trip(self, monkeypatch, db_session):
        """``hms_errors`` can outlive the dialog that raised it; ``print_error`` 0 says the dialog
        is gone."""
        await _printer(db_session, 82)
        wire = PlateWire(_plate_state(print_error=0, hms=[_vision_hms()])).install(monkeypatch)

        pause_recovery.note_status_push(82, wire.state)
        assert printer_incidents.driver_live(82) is False  # no driver was even spawned
        await _drain_episode(82)

        assert wire.presses == []
        assert await _plate_rows(db_session, 82) == []

    async def test_a_running_printer_does_not_trip(self, monkeypatch, db_session):
        await _printer(db_session, 83)
        wire = PlateWire(_plate_state(gcode_state="RUNNING")).install(monkeypatch)

        pause_recovery.note_status_push(83, wire.state)
        assert printer_incidents.driver_live(83) is False  # no driver was even spawned
        await _drain_episode(83)

        assert wire.presses == []
        assert await _plate_rows(db_session, 83) == []

    @pytest.mark.parametrize("guard", ["stale_report", "service_hold", "eject_owned", "live_driver"])
    async def test_the_lane_stands_aside(self, monkeypatch, db_session, guard):
        """A stale report (a reconnect re-broadcasting the last session's PAUSE), a service hold
        (the operator is at the screen), an eject owning the printer, or a live driver: nothing
        is opened and nothing is pressed."""
        from backend.app.models.printer_incident import KIND_SERVICE_HOLD

        await _printer(db_session, 84)
        wire = PlateWire(_plate_state(fresh=guard != "stale_report")).install(monkeypatch)
        blocker = None
        if guard == "service_hold":
            assert await printer_incidents.open_declared(db_session, 84, kind=KIND_SERVICE_HOLD) is not None
        elif guard == "eject_owned":
            TestInterruptedEject._claim_eject(84)
        elif guard == "live_driver":
            blocker = asyncio.get_running_loop().create_task(asyncio.sleep(3600))
            printer_incidents.register_driver(84, blocker, incident_id=None)

        for _ in range(3):
            await _episode_run(84, wire.state)

        assert wire.presses == []
        assert wire.stops == []
        assert await _plate_rows(db_session, 84) == []
        if blocker is not None:
            blocker.cancel()

    async def test_one_driver_per_printer_the_spawn_race(self, monkeypatch, db_session):
        """Five pushes land before the first driver has opened its row: the sampler registered
        that driver at its spawn, so the four after it read a live driver and spawn nothing."""
        await _printer(db_session, 85)
        wire = PlateWire(_plate_state()).install(monkeypatch)

        for _ in range(5):
            pause_recovery.note_status_push(85, wire.state)
        assert printer_incidents.driver_live(85) is True
        await _drain_episode(85)

        assert len(wire.presses) == 1
        assert len(await _open_incidents(db_session, 85)) == 1
        assert printer_incidents.driver_live(85) is False


class TestPlateCheckRungOne:
    """Rung 1: press the printer's own "Problem solved, resume" — never "Ignore" — read its ACK,
    and watch the wire for the re-check's answer."""

    async def test_the_press_is_the_vendor_button_for_this_dialog_and_job(self, monkeypatch, db_session, caplog):
        await _printer(db_session, 86)
        await _farm_item(db_session, 86)
        wire = PlateWire(_plate_state()).install(monkeypatch)

        with caplog.at_level("INFO", logger=pause_recovery.logger.name):
            await _episode_run(86, wire.state)

        assert wire.presses == [("0500808C", "PROBLEM_SOLVED_RESUME", "task-1")]
        # The decimal ``err`` the frame carries is the one the log names (83918988).
        assert "problem-solved resume sent (seq 1, err 83918988)" in caplog.text
        assert "problem-solved resume answer=success" in caplog.text

    @pytest.mark.parametrize(
        ("ack", "press_sent", "answer", "stopped"),
        [
            ("success", True, "success", False),
            (None, True, "no_ack", False),
            ("fail", True, "fail", True),
            ("success", False, "not_sent", True),
        ],
        ids=["success-watches", "no-ack-watches", "fail-stops", "not-sent-stops"],
    )
    async def test_the_ack_answer_is_on_the_ledger_and_only_fail_or_not_sent_skip_the_watch(
        self, monkeypatch, db_session, ack, press_sent, answer, stopped
    ):
        await _printer(db_session, 87)
        await _farm_item(db_session, 87)
        _spy(monkeypatch, "on_plate_not_empty")
        # A refused press leaves the job paused at its dialog; an accepted one lets it print.
        after_press = () if stopped else (_set(state="RUNNING", print_error=0),)
        wire = PlateWire(_plate_state(), ack=ack, press_sent=press_sent, after_press=after_press).install(monkeypatch)

        await _episode_run(87, wire.state)

        rows = await _open_incidents(db_session, 87)
        steps = await _steps(db_session, rows[0].id)
        assert steps[0] == ("dialog", "PROBLEM_SOLVED_RESUME", answer)
        assert (wire.stops != []) is stopped
        if not stopped:
            await db_session.refresh(rows[0])
            assert rows[0].resolve_source == "recheck_passed"

    async def test_a_pass_needs_running_and_a_layer_of_this_job(self, monkeypatch, db_session, caplog):
        """RUNNING at layer 0 is still the start block (and the re-check may yet re-pause): the
        driver keeps watching. Layer 1 of THIS job passes: the row closes ``recheck_passed`` — a
        farm close, ``auto_recovered`` — and the unit's hold projection is cleared."""
        await _printer(db_session, 88)
        item = await _farm_item(db_session, 88)
        wire = PlateWire(
            _plate_state(),
            layer=0,
            after_press=(
                _set(state="RUNNING", print_error=0),
                _set(state="RUNNING"),
                _set(layer=1),
            ),
        ).install(monkeypatch)

        with caplog.at_level("INFO", logger=pause_recovery.logger.name):
            await _episode_run(88, wire.state)

        (row,) = await _open_incidents(db_session, 88)
        await db_session.refresh(row)
        assert (row.status, row.resolve_source) == ("resolved", "recheck_passed")
        assert printer_incidents.outcome_of(row) == "auto_recovered"
        assert "re-check passed at layer 1" in caplog.text
        await db_session.refresh(item)
        assert (item.status, item.waiting_reason) == ("printing", None)
        assert wire.stops == []

    async def test_a_layer_this_client_cannot_measure_never_passes(self, monkeypatch, db_session):
        """A client that attached the job mid-flight (``job_peaks`` not ``reliable``: a restart
        baseline or a predecessor's republish) proves nothing: the driver exits WITHOUT closing — the sweep
        closes a job RUNNING for its dwell, and a plate dialog that comes back re-enters."""
        await _printer(db_session, 89)
        wire = PlateWire(_plate_state(), layer=None).install(monkeypatch)

        await _episode_run(89, wire.state)

        (row,) = await _plate_rows(db_session, 89)
        assert (row.status, row.resolved_at) == (STATUS_RECOVERING, None)
        assert wire.stops == []
        assert printer_incidents.driver_live(89) is False

    async def test_a_job_that_never_reaches_layer_one_is_not_watched_forever(self, monkeypatch, db_session, caplog):
        """RUNNING at layer 0 past ``_RECHECK_FIRST_LAYER_S``: the layer reading is not coming.
        The driver exits without closing — never a pass it did not read, never a stop of a
        running job — and the sweep closes the row on the job RUNNING."""
        await _printer(db_session, 78)
        wire = PlateWire(_plate_state(), layer=0).install(monkeypatch)

        with caplog.at_level("INFO", logger=pause_recovery.logger.name):
            await _episode_run(78, wire.state)

        (row,) = await _plate_rows(db_session, 78)
        assert (row.status, row.resolved_at) == (STATUS_RECOVERING, None)
        assert wire.stops == []
        assert "without reaching layer 1" in caplog.text

    @pytest.mark.parametrize(
        ("after_press", "why"),
        [
            (
                (_set(state="RUNNING", print_error=0, layer=0), _set(state="PAUSE", print_error=_PLATE_ERR)),
                "the plate dialog came back on the same job",
            ),
            ((_set(print_error=0), _set(print_error=_PLATE_ERR)), "the plate dialog came back on the same job"),
            ((), "still paused"),
        ],
        ids=["running-then-dialog-again", "dialog-cleared-then-again", "never-left-the-pause"],
    )
    async def test_a_failed_recheck_goes_to_the_stop(self, monkeypatch, db_session, caplog, after_press, why):
        await _printer(db_session, 90)
        await _farm_item(db_session, 90)
        wire = PlateWire(_plate_state(), after_press=after_press).install(monkeypatch)

        with caplog.at_level("INFO", logger=pause_recovery.logger.name):
            await _episode_run(90, wire.state)

        assert f"re-check failed ({why}" in caplog.text
        assert "— stopping the print" in caplog.text
        assert wire.stops == [90]
        (row,) = await _plate_rows(db_session, 90)
        assert [kind for kind, _name, _outcome in await _steps(db_session, row.id)] == ["dialog", "stop"]

    async def test_another_dialog_hands_over_and_rearms_the_power_loss_edge(self, monkeypatch, db_session, caplog):
        """After the press the job re-PAUSEs on the firmware's power-loss prompt (a reboot): it is
        not the plate check's. The row closes ``handed_over``, the unit's projection is cleared,
        and the power-loss rising edge is re-armed — its driver stood aside on the job pause, so
        without the re-arm the prompt would be stranded. The next push answers it."""
        await _printer(db_session, 91)
        item = await _farm_item(db_session, 91)
        # The prompt already stands on the HMS list, and the power-loss sampler already saw it
        # (its driver stood aside on the job pause) — so its edge is spent.
        wire = PlateWire(
            _plate_state(hms=[_prompt_hms()]),
            after_press=(_set(print_error=_POWER_LOSS_ERR),),
        ).install(monkeypatch)
        pause_recovery._seen[91] = pause_recovery._WireSample(epoch=1, outage_s=None, at_prompt=True)

        with caplog.at_level("INFO", logger=pause_recovery.logger.name):
            await _episode_run(91, wire.state)

        (row,) = await _open_incidents(db_session, 91)
        await db_session.refresh(row)
        assert (row.status, row.resolve_source) == ("resolved", "handed_over")
        assert "handed over (the job is paused for 0300_8007, not the plate check)" in caplog.text
        await db_session.refresh(item)
        assert item.waiting_reason is None
        assert pause_recovery._seen[91].at_prompt is False
        assert wire.stops == []

        # Liveness: the next push is the prompt's rising edge, and the power-loss lane resumes.
        await _drive(91, wire.state)
        assert wire.resumes == 1


class TestPlateCheckRungTwo:
    """Rung 2: the farm STOPS the print (heaters off) — the level re-read before every send —
    and the stop's answer decides between the terminal's verdict and the FALLBACK hold."""

    async def test_the_level_is_re_read_before_the_stop(self, monkeypatch, db_session):
        """The press was refused, and by the time the stop would go out the job is RUNNING (an
        operator resumed it): nothing is stopped."""
        await _printer(db_session, 92)
        wire = PlateWire(_plate_state(), ack="fail", after_press=(_set(state="RUNNING", print_error=0),)).install(
            monkeypatch
        )

        await _episode_run(92, wire.state)

        assert wire.stops == []
        (row,) = await _plate_rows(db_session, 92)
        assert [kind for kind, _name, _outcome in await _steps(db_session, row.id)] == ["dialog"]

    async def test_a_stop_that_did_not_go_out_is_retried_once_after_a_re_read(self, monkeypatch, db_session):
        await _printer(db_session, 93)
        wire = PlateWire(_plate_state(), ack="fail", after_press=(), stop_sent=(False, True)).install(monkeypatch)

        await _episode_run(93, wire.state)

        assert wire.stops == [93, 93]
        (row,) = await _plate_rows(db_session, 93)
        assert (await _steps(db_session, row.id))[-1] == ("stop", "stop", "taken")

    async def test_the_retry_re_reads_the_level_and_hands_over(self, monkeypatch, db_session):
        """While the refused send waited, the job re-PAUSEd on another dialog: no second stop, the
        stop step answers ``not_sent``, and the pause is handed over."""
        await _printer(db_session, 94)
        wire = PlateWire(
            _plate_state(),
            ack="fail",
            after_press=(),
            stop_sent=(False,),
            after_refused_stop=(_set(print_error=_POWER_LOSS_ERR),),
        ).install(monkeypatch)

        await _episode_run(94, wire.state)

        assert wire.stops == [94]
        (row,) = await _open_incidents(db_session, 94)
        await db_session.refresh(row)
        assert row.resolve_source == "handed_over"
        assert (await _steps(db_session, row.id))[-1] == ("stop", "stop", "not_sent")

    async def test_a_taken_stop_keeps_the_driver_live_until_the_row_closes(self, monkeypatch, db_session):
        """The job's terminal closes the row (its closer, not the driver); until it does the
        driver stays live, so no other closer reads the episode as nobody's."""
        await _printer(db_session, 95)
        monkeypatch.setattr(pause_recovery, "_STOP_CONFIRM_S", 30.0)
        wire = PlateWire(_plate_state(), ack="fail", after_press=()).install(monkeypatch)

        pause_recovery.note_status_push(95, wire.state)
        for _ in range(2000):
            if wire.stops:
                (row,) = await _plate_rows(db_session, 95)
                if (await _steps(db_session, row.id))[-1] == ("stop", "stop", "taken"):
                    break
            await asyncio.sleep(0.005)
        else:
            pytest.fail("the farm's stop was never answered taken")
        assert printer_incidents.driver_live(95) is True

        await printer_incidents.close(db_session, row.id, status="resolved", source="terminal")  # the terminal
        await _drain_episode(95)

        assert printer_incidents.driver_live(95) is False

    @pytest.mark.parametrize(
        ("stop_sent", "after_stop", "answer", "sends"),
        [((True,), (), "not_taken", 1), ((False, False), (), "not_sent", 2)],
        ids=["not_taken", "not_sent"],
    )
    async def test_a_stop_the_farm_could_not_take_escalates_to_the_paused_hold(
        self, monkeypatch, db_session, caplog, stop_sent, after_stop, answer, sends
    ):
        """The FALLBACK: the row turns ESCALATED — the human's for good — and ONE page asks them
        to fix the plate and resume, in the printer's own words."""
        await _printer(db_session, 96)
        await _farm_item(db_session, 96)
        page = _spy(monkeypatch, "on_plate_not_empty")
        wire = PlateWire(
            _plate_state(), ack="fail", after_press=(), stop_sent=stop_sent, after_stop=after_stop
        ).install(monkeypatch)

        with caplog.at_level("INFO", logger=pause_recovery.logger.name):
            await _episode_run(96, wire.state)

        assert len(wire.stops) == sends
        (row,) = await _plate_rows(db_session, 96)
        assert (row.status, row.resolved_at) == (STATUS_ESCALATED, None)
        assert (await _steps(db_session, row.id))[-1] == ("stop", "stop", answer)
        assert f"stop answer={answer}" in caplog.text
        assert "print left PAUSED for a human" in caplog.text
        page.assert_awaited_once()
        detail = page.await_args.kwargs["source_detail"]
        assert "Detected build plate offset" in detail
        assert detail.endswith("Print paused — fix the plate, then resume.")

        # ...and the farm never acts on it again: the pause keeps standing, push after push.
        for _ in range(5):
            await _episode_run(96, wire.state)
        assert len(wire.presses) == 1
        assert len(wire.stops) == sends
        page.assert_awaited_once()


class TestPlateCheckReEntry:
    """A restart kills the driver and keeps the row + its ledger. While the job is still PAUSEd
    the episode is re-entered, and the owed rung is read from the ledger ALONE — never a second
    "Problem solved, resume"."""

    async def test_no_press_on_the_ledger_presses(self, monkeypatch, db_session):
        await _printer(db_session, 97)
        await _episode(db_session, 97, job_id="task-1")
        await _restart(db_session)
        wire = PlateWire(_plate_state()).install(monkeypatch)

        await _episode_run(97, wire.state)

        assert len(wire.presses) == 1
        assert len(await _open_incidents(db_session, 97)) == 1  # adopted, not a second row

    async def test_a_press_on_the_ledger_stops_and_never_presses_again(self, monkeypatch, db_session, caplog):
        await _printer(db_session, 98)
        row = await _episode(db_session, 98, job_id="task-1", steps=(pause_recovery.PlateCheckStep.dialog(),))
        await _restart(db_session)
        wire = PlateWire(_plate_state()).install(monkeypatch)

        with caplog.at_level("INFO", logger=pause_recovery.logger.name):
            await _episode_run(98, wire.state)

        assert wire.presses == []
        assert wire.stops == [98]
        assert f"plate-check episode {row.id} re-entered" in caplog.text
        assert [kind for kind, _name, _outcome in await _steps(db_session, row.id)] == ["dialog", "stop"]

    @pytest.mark.parametrize("gcode_state", ["RUNNING", "IDLE"])
    async def test_only_while_the_job_is_paused(self, monkeypatch, db_session, gcode_state):
        await _printer(db_session, 99)
        await _episode(db_session, 99, job_id="task-1")
        await _restart(db_session)
        wire = PlateWire(_plate_state(gcode_state=gcode_state, print_error=0)).install(monkeypatch)

        await _episode_run(99, wire.state)

        assert (wire.presses, wire.stops) == ([], [])

    async def test_a_reboot_prompt_on_re_entry_is_handed_over(self, monkeypatch, db_session):
        """ANY pause of the same job re-enters: after a printer reboot ``print_error`` is the
        power-loss prompt, and the episode must hand over rather than leave its row standing."""
        await _printer(db_session, 100)
        row = await _episode(db_session, 100, job_id="task-1", steps=(pause_recovery.PlateCheckStep.dialog(),))
        await _restart(db_session)
        wire = PlateWire(_plate_state(print_error=_POWER_LOSS_ERR, hms=[_prompt_hms()])).install(monkeypatch)
        # The prompt's edge is spent (its driver stood aside on the job pause), so this push
        # spawns only the episode.
        pause_recovery._seen[100] = pause_recovery._WireSample(epoch=1, outage_s=None, at_prompt=True)

        await _episode_run(100, wire.state)

        assert (wire.presses, wire.stops) == ([], [])
        await db_session.refresh(row)
        assert row.resolve_source == "handed_over"
        assert pause_recovery._seen[100].at_prompt is False  # re-armed for the prompt

    async def test_another_jobs_episode_is_not_re_entered(self, monkeypatch, db_session):
        await _printer(db_session, 101)
        await _episode(db_session, 101, job_id="task-OLD")
        await _restart(db_session)
        wire = PlateWire(_plate_state(print_error=0)).install(monkeypatch)

        await _episode_run(101, wire.state)

        assert (wire.presses, wire.stops) == ([], [])


class TestThePauseLaneStopsOnlyAsTheFarm:
    """Module-scope AST pin. The plate-check ladder's stop is the FARM ending its own job — a raw
    ``stop_print``, allowlisted in ``test_code_quality._RAW_STOP_CALLERS`` with the rulings — so
    this lane never sends the OPERATOR's stop (``stop_as_operator`` / ``stamp_operator_stop``):
    a farm stop dressed as an operator's would classify its terminal as a human's and never
    retry."""

    async def test_the_pause_lane_sends_no_operator_stop(self):
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(pause_recovery))
        called = {
            node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        }
        assert called & {"stop_as_operator", "stamp_operator_stop"} == set()
        # Liveness: the ladder's own stop is still there.
        assert "stop_print" in called


# --- the operator's clear ---------------------------------------------------


class TestOnPlateCleared:
    async def test_a_paused_plate_check_is_not_answered_by_a_plate_act(self, db_session):
        """``job_pause``: the answer is resuming or stopping the job — neither verb
        (routine clear or Recover) ends it."""
        await _printer(db_session, 44)
        await printer_incidents.open_new(
            db_session,
            printer_id=44,
            job_id="task-1",
            item_id=None,
            kind=KIND_PLATE_VISION,
            code="0500_808C",
            codes="0500_808C",
            slot_global_tray=None,
            status=STATUS_ESCALATED,
        )

        assert await pause_recovery.on_plate_cleared(44) == []
        assert await pause_recovery.on_plate_cleared(44, recover=True) == []
        assert (await printer_incidents.get_open(db_session, 44)) is not None

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

        # It reports WHAT it closed — both verbs carry the list to the operator.
        assert [kind for _id, kind in await pause_recovery.on_plate_cleared(39)] == [KIND_Z_REFERENCE_LOST]

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

        assert await pause_recovery.on_plate_cleared(40) == []

        assert (await printer_incidents.get_open(db_session, 40)) is not None

    async def test_no_open_incident_is_not_an_error(self, db_session):
        assert await pause_recovery.on_plate_cleared(41) == []


class TestMaintenanceModeStandsAside:
    """Maintenance mode (2026-09-12): the prompt in front of the operator is theirs.

    The sampler has two acts and they are held down DIFFERENTLY, because only one of
    them is the farm ACTING:

    * the power-loss driver is an act — it resumes or stops somebody else's print — so a
      held printer gets none of it. Nothing is resumed, nothing is stopped, and no
      ``power_loss`` row is opened: a human is at the screen, and the farm answering the
      prompt behind them is exactly the surprise the hold exists to prevent;
    * the lost-Z arm is a REFUSAL RECORD (2026-09-19). It opens no driver and moves
      nothing; all it does is make every LATER eject refuse until a human clears the
      plate. An outage destroys the Z datum whether or not a hold happens to stand at
      that moment, so skipping the record would leave the printer looking trustworthy
      when it is not — the 2026-09-04 bed-past-the-floor mechanism, arrived at by
      omission.
    """

    async def _hold(self, db, printer_id):
        from backend.app.models.printer_incident import KIND_SERVICE_HOLD

        assert await printer_incidents.open_declared(db, printer_id, kind=KIND_SERVICE_HOLD) is not None

    async def test_the_power_loss_prompt_is_left_to_the_operator(self, monkeypatch, db_session):
        await _printer(db_session, 41)
        await self._hold(db_session, 41)
        state = _make_state()
        client = FakeClient(state)
        calls = _wire(monkeypatch, state, client)
        paged = _spy(monkeypatch, "on_power_loss_hold")

        for _ in range(5):  # five ~1 Hz pushes with the prompt standing
            await _drive(41, state)

        assert pause_recovery._in_flight.get(41) is None, "no driver may be spawned for a held printer"
        assert client.calls == [], "nothing resumed"
        assert calls == [], "nothing stopped"
        paged.assert_not_awaited()
        # Only the hold itself is on disk — no power-loss row was opened.
        from backend.app.models.printer_incident import KIND_SERVICE_HOLD

        assert [row.kind for row in await _open_incidents(db_session, 41)] == [KIND_SERVICE_HOLD]

    async def test_an_outage_during_the_hold_still_opens_the_lost_z_row(self, monkeypatch, db_session):
        """LIVENESS — the assertion that used to be its own opposite (2026-09-19).

        The row is a refusal record, not a recovery act: a power cycle destroys the Z
        datum regardless of who owns the machine, and an eject dispatched after the hold
        lifts would drive a sweep against a Z frame that is fiction. Nothing is resumed
        and nothing is stopped — only the record is written.
        """
        from backend.app.models.printer_incident import KIND_SERVICE_HOLD, KIND_Z_REFERENCE_LOST

        await _printer(db_session, 42)
        await _geometry(db_session)  # z_reference_validated False — no witnessed Z stop
        await self._hold(db_session, 42)
        page = _spy(monkeypatch, "on_z_reference_lost")
        anchor = time.time() - 300.0
        state = _make_state(gcode_state="IDLE", hms=[], epoch=2, disconnected_at=anchor)
        client = FakeClient(state)
        calls = _wire(
            monkeypatch, state, client, fleet={pid: _make_state(disconnected_at=anchor + pid) for pid in range(1, 5)}
        )
        _occupy_plate(42)

        pause_recovery.note_status_push(42, _make_state(gcode_state="IDLE", hms=[], epoch=1, disconnected_at=anchor))
        pause_recovery.note_status_push(42, state)  # the reconnect edge
        await _drain_z_arm(42)

        assert sorted(row.kind for row in await _open_incidents(db_session, 42)) == sorted(
            [KIND_SERVICE_HOLD, KIND_Z_REFERENCE_LOST]
        )
        # The page is deliberately NOT a suppressed farm-reaction event: the hold it
        # names outlives the maintenance window and refuses every later eject.
        page.assert_awaited_once()
        # ...and the acting half of the sampler still stood aside.
        assert client.calls == [], "nothing resumed"
        assert calls == [], "nothing stopped"

    async def test_releasing_the_hold_does_not_replay_the_edge(self, monkeypatch, db_session):
        """The wire sample is still RECORDED while held, so an edge consumed during the
        hold is not waiting to fire a SECOND time at the printer the moment it is
        released — the lost-Z row was already written at the edge itself, once."""
        from backend.app.models.printer_incident import KIND_SERVICE_HOLD, KIND_Z_REFERENCE_LOST, STATUS_RESOLVED

        await _printer(db_session, 43)
        await _geometry(db_session)
        await self._hold(db_session, 43)
        page = _spy(monkeypatch, "on_z_reference_lost")
        anchor = time.time() - 300.0
        after = _make_state(gcode_state="IDLE", hms=[], epoch=2, disconnected_at=anchor)
        _wire(monkeypatch, after, None, fleet={pid: _make_state(disconnected_at=anchor + pid) for pid in range(1, 5)})
        _occupy_plate(43)

        pause_recovery.note_status_push(43, _make_state(gcode_state="IDLE", hms=[], epoch=1, disconnected_at=anchor))
        pause_recovery.note_status_push(43, after)  # the reconnect edge
        await _drain_z_arm(43)
        assert page.await_count == 1

        row = await printer_incidents.get_open(db_session, 43, kinds={KIND_SERVICE_HOLD})
        assert row is not None
        await printer_incidents.close(db_session, row.id, status=STATUS_RESOLVED, source="operator")

        pause_recovery.note_status_push(43, after)  # same epoch: no edge left to replay
        await _drain_z_arm(43)

        # Still exactly ONE lost-Z row and one page: the release replayed nothing.
        kinds = [row.kind for row in await _open_incidents(db_session, 43)]
        assert kinds.count(KIND_Z_REFERENCE_LOST) == 1
        assert page.await_count == 1


# --- the plate-check episode: its step vocabulary and its terminal facts -----


async def _episode(db, printer_id, *, job_id, steps=(), created_at=None, close=False):
    """One ``plate_vision`` episode of ``job_id`` whose driver sent ``steps`` — seeded through
    the episode's own evidence log, the ledger's one writer. ``close`` ends it the way its
    terminal does, so another episode can open on the same printer."""
    from backend.app.models.printer_incident import STATUS_RESOLVED

    row = await printer_incidents.open_new(
        db,
        printer_id=printer_id,
        job_id=job_id,
        item_id=None,
        kind=KIND_PLATE_VISION,
        code="0500_808C",
        codes="0500_808C",
        slot_global_tray=None,
        hms_full_codes=["0500808C"],
        status=STATUS_RECOVERING,
    )
    log = await pause_recovery._PlateCheckEvidence.from_row(db, row.id)
    for step in steps:
        await log.note(step)
    if created_at is not None:
        row.created_at = created_at
        await db.commit()
    if close:
        await printer_incidents.close(db, row.id, status=STATUS_RESOLVED, source="terminal")
    return row


class TestPlateCheckStepVocabulary:
    """The episode's two ledger kinds and their closed answers — the vocabulary the episode
    driver writes and re-entry reads (operator ruling 2026-09-29)."""

    async def test_the_ladder_presses_problem_solved_resume_and_stops(self):
        press = pause_recovery.PlateCheckStep.dialog()
        stop = pause_recovery.PlateCheckStep.stop()
        assert (press.kind, press.name) == ("dialog", "PROBLEM_SOLVED_RESUME")
        assert (stop.kind, stop.name) == ("stop", "stop")

    async def test_ignore_is_never_a_ladder_press(self):
        """ "Ignore and resume" suppresses the re-check the ladder exists to run."""
        with pytest.raises(LookupError):
            pause_recovery.PlateCheckStep.dialog("IGNORE_RESUME")

    @pytest.mark.parametrize(
        ("kind", "accepted", "refused"),
        [
            ("dialog", ["success", "fail", "no_ack", "not_sent"], ["taken", "not_taken", "maybe"]),
            ("stop", ["taken", "not_taken", "not_sent"], ["success", "fail", "no_ack"]),
        ],
    )
    async def test_each_kind_answers_only_in_its_own_vocabulary(self, kind, accepted, refused):
        step = pause_recovery.PlateCheckStep.dialog() if kind == "dialog" else pause_recovery.PlateCheckStep.stop()
        for token in accepted:
            assert step.answered(token).outcome == token
        for token in refused:
            with pytest.raises(LookupError):
                step.answered(token)

    async def test_the_log_round_trips_and_re_entry_reads_what_was_sent(self, db_session):
        await _printer(db_session, 60)
        row = await _episode(db_session, 60, job_id="JOB-1")
        log = await pause_recovery._PlateCheckEvidence.from_row(db_session, row.id)

        pressed = await log.note(pause_recovery.PlateCheckStep.dialog())
        await log.answer(pressed, "fail")
        await log.note(pause_recovery.PlateCheckStep.stop())

        rebuilt = await pause_recovery._PlateCheckEvidence.from_row(db_session, row.id)
        assert [(s.seq, s.kind, s.name, s.outcome) for s in rebuilt.steps] == [
            (1, "dialog", "PROBLEM_SOLVED_RESUME", "fail"),
            (2, "stop", "stop", None),
        ]
        assert rebuilt.has_step("dialog") and rebuilt.has_step("stop")

    @pytest.mark.parametrize(
        ("kind", "name", "outcome"),
        [("lever", "resume", None), ("dialog", "IGNORE_RESUME", None), ("stop", "stop", "success")],
        ids=["another-drivers-kind", "a-button-the-ladder-never-presses", "a-dialog-answer-on-a-stop"],
    )
    async def test_a_ledger_row_the_lane_cannot_name_raises_at_hydration(self, db_session, kind, name, outcome):
        await _printer(db_session, 61)
        row = await _episode(db_session, 61, job_id="JOB-1")
        await printer_incidents.note_step(db_session, row.id, seq=1, kind=kind, name=name)
        if outcome is not None:
            await printer_incidents.answer_step(db_session, row.id, 1, outcome=outcome)

        with pytest.raises(LookupError):
            await pause_recovery._PlateCheckEvidence.from_row(db_session, row.id)


class TestPlateCheckFacts:
    """``plate_check_facts`` — what a terminal owes its plate-check episode, read off the
    snapshot ``main.on_print_complete`` took before any closer: did the FARM stop this job,
    and how many OTHER episodes did it stop on this printer inside the window."""

    async def test_a_job_with_no_episode_has_no_facts(self, db_session):
        await _printer(db_session, 62)
        assert await pause_recovery.plate_check_facts(62, [], "JOB-1") is None
        # Another job's episode, and a hold of another kind, say nothing about this terminal.
        await _episode(db_session, 62, job_id="JOB-OTHER", steps=(pause_recovery.PlateCheckStep.stop(),))
        runout = {"id": 99, "kind": KIND_RUNOUT, "job_id": "JOB-1"}
        snapshot = [*printer_incidents.snapshots(62), runout]
        assert await pause_recovery.plate_check_facts(62, snapshot, "JOB-1") is None

    async def test_a_farm_stop_step_is_farm_stopped(self, db_session):
        await _printer(db_session, 63)
        await _episode(
            db_session,
            63,
            job_id="JOB-1",
            steps=(pause_recovery.PlateCheckStep.dialog(), pause_recovery.PlateCheckStep.stop()),
        )

        facts = await pause_recovery.plate_check_facts(63, printer_incidents.snapshots(63), "JOB-1")

        assert facts == PlateCheckFacts(farm_stopped=True, stops_in_window=0)

    @pytest.mark.parametrize(
        ("answer", "farm_stopped"),
        [(None, True), ("taken", True), ("not_taken", False), ("not_sent", False)],
        ids=["unanswered", "taken", "not_taken", "not_sent"],
    )
    async def test_only_a_stop_that_ended_the_job_is_the_farms(self, db_session, answer, farm_stopped):
        """The terminal usually lands while the driver is still watching for its stop's answer,
        so an UNANSWERED stop is the farm's. A stop answered as not having ended the job made
        the driver hand the paused print to a human — whoever ends it later is that human, and
        the terminal escalates rather than retries."""
        await _printer(db_session, 67)
        row = await _episode(db_session, 67, job_id="JOB-1", steps=(pause_recovery.PlateCheckStep.dialog(),))
        log = await pause_recovery._PlateCheckEvidence.from_row(db_session, row.id)
        seq = await log.note(pause_recovery.PlateCheckStep.stop())
        if answer is not None:
            await log.answer(seq, answer)

        facts = await pause_recovery.plate_check_facts(67, printer_incidents.snapshots(67), "JOB-1")

        assert facts == PlateCheckFacts(farm_stopped=farm_stopped, stops_in_window=0)

    async def test_a_press_alone_is_not_a_farm_stop(self, db_session):
        """An operator who stops the print mid-episode leaves the farm's press on the log and
        no farm ``stop`` — so the terminal escalates."""
        await _printer(db_session, 64)
        await _episode(db_session, 64, job_id="JOB-1", steps=(pause_recovery.PlateCheckStep.dialog(),))

        facts = await pause_recovery.plate_check_facts(64, printer_incidents.snapshots(64), "JOB-1")

        assert facts == PlateCheckFacts(farm_stopped=False, stops_in_window=0)

    async def test_another_jobs_stopped_episode_counts_inside_the_window_only(self, db_session):
        from datetime import timedelta

        await _printer(db_session, 65)
        stop = pause_recovery.PlateCheckStep.stop()
        long_ago = datetime.utcnow() - timedelta(seconds=PLATE_RECHECK_WINDOW_S + 600)
        await _episode(db_session, 65, job_id="JOB-OLD", steps=(stop,), created_at=long_ago, close=True)
        await _episode(db_session, 65, job_id="JOB-PASSED", steps=(pause_recovery.PlateCheckStep.dialog(),), close=True)
        await _episode(db_session, 65, job_id="JOB-PREV", steps=(stop,), close=True)  # the retry's parent
        await _episode(db_session, 65, job_id="JOB-1", steps=(stop,))

        facts = await pause_recovery.plate_check_facts(65, printer_incidents.snapshots(65), "JOB-1")

        # Only JOB-PREV: JOB-OLD is outside the window, JOB-PASSED was never stopped, and
        # JOB-1 is this terminal's own episode.
        assert facts == PlateCheckFacts(farm_stopped=True, stops_in_window=1)

    async def test_an_unreadable_ledger_fails_closed_to_facts_that_escalate(self, db_session, monkeypatch, caplog):
        import logging

        import backend.app.core.database as core_db

        await _printer(db_session, 66)
        await _episode(db_session, 66, job_id="JOB-1", steps=(pause_recovery.PlateCheckStep.stop(),))
        snapshot = printer_incidents.snapshots(66)

        def _broken():
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(core_db, "async_session", _broken)
        with caplog.at_level(logging.WARNING, logger=pause_recovery.logger.name):
            facts = await pause_recovery.plate_check_facts(66, snapshot, "JOB-1")

        assert facts is not None and facts.farm_stopped is False
        assert "plate-check facts for job JOB-1 unreadable" in caplog.text
