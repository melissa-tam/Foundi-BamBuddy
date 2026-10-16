"""``services/plate_autoclear`` — a normal leftover's gate clears itself once the camera shows it empty.

Driven through the scheduler's hook (``run_pass``) over a real database, the real occupancy
authority, the real plate detection (OpenCV over synthetic frames, a real reference image) and the
real still owner. Stood in for: the printer's MQTT client (records the bed G-code and the light
frames), the camera's own capture, the waits (recorded), and the spawn seam (so a test can await the
reading it caused).
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import get_args
from unittest.mock import AsyncMock, patch

import cv2
import numpy as np
import pytest

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_incident import KIND_RUNOUT, KIND_SERVICE_HOLD
from backend.app.models.printer_model_geometry import PrinterModelGeometry
from backend.app.services import (
    camera_stills,
    farm_correlation,
    pause_recovery,
    plate_autoclear,
    plate_detection,
    printer_incidents,
)
from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.eject import bed_motion
from backend.app.services.plate_occupancy import (
    CooldownEject,
    DepositEvidence,
    EscalationOnly,
    Evidence,
    PendingEject,
    PlateOrigin,
    plate_occupancy,
)

_CAPTURE = "backend.app.services.camera.capture_camera_frame_bytes"


def _frame(*objects: tuple[int, int, int, int]) -> bytes:
    img = np.zeros((360, 640, 3), np.uint8)
    img[:, :, :] = np.linspace(70, 190, 640, dtype=np.uint8)[None, :, None]
    for x, y, w, h in objects:
        img[y : y + h, x : x + w] = 15
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 95])
    assert ok
    return buf.tobytes()


_EMPTY = _frame()
_OCCUPIED = _frame((250, 120, 160, 140))


class _Client:
    """The printer's MQTT client: connected, its chamber light off; records what the farm sends."""

    def __init__(self, state: PrinterState) -> None:
        self.state = state
        self.gcode: list[str] = []
        self.light: list[bool] = []

    def send_gcode(self, gcode: str, *, sequence_id=None) -> bool:
        self.gcode.append(gcode)
        return True

    def set_chamber_light(self, on: bool, *, sequence_id=None) -> bool:
        self.light.append(on)
        return True

    def feed(self):
        return None


class _World(SimpleNamespace):
    async def run(self, db) -> list[asyncio.Task]:
        """One scheduler pass; awaits the readings it spawned and returns them."""
        before = len(self.tasks)
        await plate_autoclear.run_pass(db)
        spawned = self.tasks[before:]
        if spawned:
            await asyncio.gather(*spawned)
        return spawned

    def advance(self, seconds: float) -> None:
        self.mono[0] += seconds


@pytest.fixture
async def world(db_session, own_session_factory, monkeypatch, tmp_path):
    from backend.app.api.routes.settings import set_setting
    from backend.app.core.config import settings
    from backend.app.services.printer_manager import printer_manager

    monkeypatch.setattr("backend.app.core.database.async_session", own_session_factory)
    monkeypatch.setattr(settings, "plate_calibration_dir", tmp_path)

    db_session.add(
        PrinterModelGeometry(
            model_key="H2S",
            bed_x=340,
            bed_y=320,
            env_x_min=0,
            env_x_max=340,
            env_y_min=-16,
            env_y_max=325,
            max_part_height_mm=42,
            z_travel_mm=340,
            validated=True,
            notes="test seed",
        )
    )
    printer = Printer(name="AC-1", serial_number="SAC1", ip_address="10.0.0.9", access_code="x", model="H2S")
    db_session.add(printer)
    await set_setting(db_session, plate_autoclear.SETTING_KEY, "true")
    await db_session.commit()
    await db_session.refresh(printer)
    assert plate_detection.PlateDetector().calibrate(_EMPTY, printer.id)[0]

    state = PrinterState(connected=True, state="FINISH", connection_epoch=1, report_epoch=1)
    client = _Client(state)
    monkeypatch.setattr(printer_manager, "get_client", lambda pid: client if pid == printer.id else None)
    monkeypatch.setattr(printer_manager, "get_status", lambda pid: state if pid == printer.id else None)
    monkeypatch.setattr(printer_manager, "is_connected", lambda pid: pid == printer.id and state.connected)

    # One monotonic clock for the episode's cadence and the authority's motion window; a recorded wait
    # moves it on by its own length, as a real one would.
    from backend.app.services import plate_occupancy as po

    mono = [50_000.0]
    monkeypatch.setattr(plate_autoclear, "_now_mono", lambda: mono[0])
    monkeypatch.setattr(po, "_now_mono", lambda: mono[0])
    waits: list[float] = []

    async def _sleep(delay):
        waits.append(delay)
        mono[0] += delay

    monkeypatch.setattr(plate_autoclear, "asyncio", SimpleNamespace(sleep=_sleep))
    monkeypatch.setattr(plate_detection, "asyncio", SimpleNamespace(sleep=_sleep))

    tasks: list[asyncio.Task] = []

    def _spawn(coro, *, name=None):
        task = asyncio.ensure_future(coro)
        tasks.append(task)
        return task

    monkeypatch.setattr(plate_autoclear, "spawn_background_task", _spawn)

    kicks: list[str] = []
    plate_occupancy.configure(kick=lambda pid, cause: kicks.append(cause))

    return _World(printer=printer, state=state, client=client, waits=waits, mono=mono, tasks=tasks, kicks=kicks)


def _leftover(world, origin: str = "operator_declared") -> None:
    """Raise the gate the way its origin does (both through the authority's own transitions)."""
    if origin == "operator_declared":
        assert plate_occupancy.declare_occupied(world.printer.id, Evidence(live_state="FINISH")) is None
        return
    plate_occupancy.note_terminal(
        world.printer.id,
        farm_correlation.terminal_disposition(
            verdict="foreign",
            item_id=None,
            eject_profile_id=None,
            first_article=False,
            batch_id=None,
            source_subtask_id="F-1",
            evidence=DepositEvidence.unknown("completed"),
            raise_gate=True,
            stop_verdict=None,
            reconciled=False,
        ),
    )


class TestTheEligibilityTable:
    def test_the_table_is_total_over_origin_hold_and_state(self):
        product = {
            (origin, hold, reading)
            for origin in get_args(PlateOrigin)
            for hold in get_args(plate_autoclear.Hold)
            for reading in get_args(plate_autoclear.PrinterReading)
        }
        assert set(plate_autoclear.ELIGIBILITY) == product
        assert set(plate_autoclear.LEFTOVER_ORIGINS) == set(get_args(PlateOrigin))
        with pytest.raises(KeyError):
            plate_autoclear.ELIGIBILITY[("a_new_origin", "none", "ready")]

    def test_only_normal_leftovers_are_eligible(self):
        eligible = {origin for (origin, hold, reading), v in plate_autoclear.ELIGIBILITY.items() if v == "eligible"}
        assert eligible == {"foreign", "operator_stop", "unattributed", "operator_declared"}

    @pytest.mark.parametrize(
        "origin",
        [
            "plate_refused",
            "first_article",
            "farm_deposit",
            "farm_unit",
            "eject_unverified",
            "eject_never_started",
            "restart",
            "unknown",
        ],
    )
    def test_never_these_origins(self, origin):
        for hold in get_args(plate_autoclear.Hold):
            for reading in get_args(plate_autoclear.PrinterReading):
                assert plate_autoclear.ELIGIBILITY[(origin, hold, reading)] == "origin"

    @pytest.mark.parametrize("origin", ["foreign", "operator_stop", "unattributed", "operator_declared"])
    def test_a_leftover_needs_no_hold_and_a_ready_printer(self, origin):
        table = plate_autoclear.ELIGIBILITY
        assert table[(origin, "none", "ready")] == "eligible"
        for reading in ("disconnected", "job_active", "eject_armed", "in_flight", "uncalibrated"):
            assert table[(origin, "none", reading)] == reading
        for reading in get_args(plate_autoclear.PrinterReading):
            assert table[(origin, "maintenance", reading)] == "maintenance"
            assert table[(origin, "hold", reading)] == "hold"


class TestEligibilityNow:
    async def test_a_ready_leftover_is_eligible(self, world):
        _leftover(world)
        assert plate_autoclear.eligibility(world.printer.id)[0] == "eligible"

    async def test_maintenance_mode_blocks(self, world, db_session):
        _leftover(world)
        assert await printer_incidents.open_declared(db_session, world.printer.id, kind=KIND_SERVICE_HOLD)
        assert plate_autoclear.eligibility(world.printer.id)[0] == "maintenance"

    async def test_any_open_hold_blocks(self, world, db_session):
        _leftover(world)
        await printer_incidents.open_new(
            db_session,
            printer_id=world.printer.id,
            job_id="",
            item_id=None,
            kind=KIND_RUNOUT,
            code="0700_8011",
            codes="0700_8011",
            slot_global_tray=None,
        )
        assert plate_autoclear.eligibility(world.printer.id)[0] == "hold"

    async def test_a_running_job_blocks(self, world):
        _leftover(world)
        world.state.state = "RUNNING"
        assert plate_autoclear.eligibility(world.printer.id)[0] == "job_active"

    async def test_a_stale_session_blocks(self, world):
        _leftover(world)
        world.state.connection_epoch = 2  # reconnected, first report not applied yet
        assert plate_autoclear.eligibility(world.printer.id)[0] == "disconnected"

    async def test_an_eject_in_flight_or_a_bed_motion_blocks(self, world):
        _leftover(world)
        assert plate_occupancy.claim_bed_motion(world.printer.id, Evidence(live_state="FINISH"), hold_s=10.0) is None
        assert plate_autoclear.eligibility(world.printer.id)[0] == "in_flight"

    async def test_an_armed_eject_blocks(self, world):
        _leftover(world, origin="foreign")
        assert farm_correlation.upgrade_to_foreign_auto_eject(world.printer.id, profile_id=1)
        assert plate_autoclear.eligibility(world.printer.id)[0] == "eject_armed"

    async def test_an_uncalibrated_camera_blocks(self, world):
        _leftover(world)
        assert plate_detection.delete_calibration(world.printer.id)
        assert plate_autoclear.eligibility(world.printer.id)[0] == "uncalibrated"


class TestTheEpisode:
    async def test_the_setting_off_reads_nothing(self, world, db_session):
        from backend.app.api.routes.settings import set_setting

        await set_setting(db_session, plate_autoclear.SETTING_KEY, "false")
        await db_session.commit()
        _leftover(world)
        assert await world.run(db_session) == []
        assert world.client.gcode == []

    async def test_two_empty_readings_clear_the_gate_through_the_plate_transition(self, world, db_session, caplog):
        _leftover(world, origin="foreign")
        with (
            patch(_CAPTURE, new_callable=AsyncMock, return_value=_EMPTY) as capture,
            patch.object(pause_recovery, "on_plate_cleared", new_callable=AsyncMock) as human_closer,
            caplog.at_level(logging.INFO, logger=plate_autoclear.logger.name),
        ):
            assert len(await world.run(db_session)) == 1
            # Reading 1: the bed positioned first (claimed, its window waited out), the light on and back.
            assert world.client.gcode == [bed_motion.held_bed_lift_gcode(12.0)]
            assert world.waits[0] == pytest.approx(bed_motion.held_bed_lift_seconds(12.0))
            assert world.client.light == [True, False]
            assert plate_occupancy.is_plate_occupied(world.printer.id)

            assert await world.run(db_session) == []  # the next reading is a minute away
            world.advance(60)
            assert len(await world.run(db_session)) == 1

        assert capture.await_count == 2
        assert world.client.gcode == [bed_motion.held_bed_lift_gcode(12.0)]  # positioned once per episode
        assert world.client.light == [True, False, True, False]
        assert plate_occupancy.is_plate_occupied(world.printer.id) is False
        assert world.kicks == ["clear_plate"]  # the authority's release edge kicks the scheduler
        human_closer.assert_not_awaited()
        assert f"printer {world.printer.id}: plate cleared (auto)" in caplog.text

    async def test_unknown_counts_as_occupied_and_the_cadence_backs_off(self, world, db_session):
        _leftover(world)
        frames = [_EMPTY, None, _OCCUPIED, _EMPTY, _EMPTY]
        with patch(_CAPTURE, new_callable=AsyncMock, side_effect=frames):
            await world.run(db_session)  # empty
            for step in (60, 300, 900):
                world.advance(step - 1)
                assert await world.run(db_session) == [], step
                world.advance(1)
                assert len(await world.run(db_session)) == 1, step
                assert plate_occupancy.is_plate_occupied(world.printer.id)
            # unknown, then occupied, then one empty: still not two in a row.
            world.advance(900)
            await world.run(db_session)
        assert plate_occupancy.is_plate_occupied(world.printer.id) is False

    async def test_a_printer_change_resets_the_cadence_to_a_minute(self, world, db_session):
        _leftover(world)
        with patch(_CAPTURE, new_callable=AsyncMock, return_value=_OCCUPIED) as capture:
            for step in (0, 60, 300):
                world.advance(step)
                await world.run(db_session)
            assert capture.await_count == 3  # now on the 15-minute step
            world.advance(60)
            assert await world.run(db_session) == []
            world.state.hms_errors = [SimpleNamespace(code="0500_4038", full_code="0500040000020038")]
            assert len(await world.run(db_session)) == 1
        assert capture.await_count == 4

    async def test_a_buffered_frame_never_counts_twice(self, world, db_session, monkeypatch):
        """A live stream whose frame does not move on: the first reading takes it, the second has no frame
        after the first one's — ``unknown`` — so the gate stays."""
        import time
        from unittest.mock import MagicMock

        monkeypatch.setattr(camera_stills, "_BUFFER_WAIT_S", 0.05)
        _leftover(world)
        with (
            patch("backend.app.api.routes.camera._active_streams", {f"{world.printer.id}-fanout": MagicMock()}),
            patch("backend.app.api.routes.camera._last_frames", {world.printer.id: _EMPTY}),
            patch("backend.app.api.routes.camera._last_frame_times", {world.printer.id: time.time() + 5.0}),
            patch(_CAPTURE, new_callable=AsyncMock) as socket,
        ):
            await world.run(db_session)
            world.advance(60)
            await world.run(db_session)
        socket.assert_not_called()
        assert plate_occupancy.is_plate_occupied(world.printer.id)

    async def test_an_ineligible_origin_is_never_read(self, world, db_session):
        plate_occupancy.note_terminal(
            world.printer.id,
            farm_correlation.terminal_disposition(
                verdict="matched",
                item_id=7,
                eject_profile_id=3,
                first_article=False,
                batch_id=1,
                source_subtask_id="SUB-7",
                evidence=DepositEvidence.unknown("completed"),
                raise_gate=True,
                stop_verdict=None,
                reconciled=False,
            ),
        )
        assert isinstance(plate_occupancy.snapshot(world.printer.id).plate_policy, CooldownEject)
        with patch(_CAPTURE, new_callable=AsyncMock, return_value=_EMPTY) as capture:
            assert await world.run(db_session) == []
        capture.assert_not_called()
        assert world.client.gcode == []

    async def test_a_refused_bed_motion_reads_nothing_and_retries_at_the_cadence(self, world, db_session):
        """A print already on its way (a ``printing`` row): the claim refuses ``dispatch_in_flight``."""
        _leftover(world)
        db_session.add(
            PrintQueueItem(printer_id=world.printer.id, status="printing", plate_id=1, position=1, first_article=False)
        )
        await db_session.commit()
        with patch(_CAPTURE, new_callable=AsyncMock, return_value=_EMPTY) as capture:
            assert len(await world.run(db_session)) == 1
            assert await world.run(db_session) == []
            world.advance(60)
            assert len(await world.run(db_session)) == 1
        capture.assert_not_called()
        assert world.client.gcode == [] and world.client.light == []

    async def test_an_eject_in_flight_is_never_read(self, world, db_session):
        _leftover(world)
        assert plate_occupancy.claim_for_eject(world.printer.id, PendingEject("manual", None, None), Evidence()) is None
        with patch(_CAPTURE, new_callable=AsyncMock, return_value=_EMPTY) as capture:
            assert await world.run(db_session) == []
        capture.assert_not_called()

    async def test_a_restart_starts_a_fresh_episode(self, world, db_session):
        _leftover(world)
        with patch(_CAPTURE, new_callable=AsyncMock, return_value=_EMPTY):
            await world.run(db_session)
            plate_autoclear._reset_state()  # a new process: no episode survives
            world.advance(60)
            await world.run(db_session)
            assert plate_occupancy.is_plate_occupied(world.printer.id)  # one empty reading of the new episode
            assert world.client.gcode == [bed_motion.held_bed_lift_gcode(12.0)] * 2  # positioned again
            world.advance(60)
            await world.run(db_session)
        assert plate_occupancy.is_plate_occupied(world.printer.id) is False

    async def test_a_printer_with_no_bed_on_z_is_never_read(self, world, db_session):
        world.printer.model = "A1"
        db_session.add(world.printer)
        await db_session.commit()
        _leftover(world)
        with patch(_CAPTURE, new_callable=AsyncMock, return_value=_EMPTY) as capture:
            await world.run(db_session)
            world.advance(3600)
            assert await world.run(db_session) == []
        capture.assert_not_called()

    async def test_a_gate_cleared_by_a_person_ends_the_episode(self, world, db_session):
        _leftover(world)
        with patch(_CAPTURE, new_callable=AsyncMock, return_value=_EMPTY):
            await world.run(db_session)
            assert plate_occupancy.clear_plate(world.printer.id) is None
            _leftover(world)  # a NEW gate: a new episode, positioned again
            world.advance(60)
            await world.run(db_session)
        assert len(world.client.gcode) == 2
        assert plate_occupancy.is_plate_occupied(world.printer.id)

    async def test_the_escalation_floor_is_not_an_armed_eject(self, world):
        _leftover(world)
        assert isinstance(plate_occupancy.snapshot(world.printer.id).plate_policy, EscalationOnly)
