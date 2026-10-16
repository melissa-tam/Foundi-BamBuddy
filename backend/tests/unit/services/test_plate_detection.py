"""Unit tests for the plate detection service.

A reading answers three ways (``PlateVerdict``): ``empty``, ``occupied``, or ``unknown`` with its
reason — a failure used to read as "empty". Driven through ``check_plate_empty`` with real OpenCV
over synthetic frames and real reference images (in a temporary calibration dir); only the camera's
own I/O (``camera.capture_camera_frame_bytes``) and the stream registry are stood in for.

The module is imported once and never reloaded: the reload-with-a-mocked-cv2 these tests used to do
left the MagicMock inside the module for every later test in the same worker.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import cv2
import numpy as np
import pytest

from backend.app.services import camera_stills, plate_detection
from backend.app.services.eject import bed_motion

_CAPTURE = "backend.app.services.camera.capture_camera_frame_bytes"
_PID = 52


def _frame(*objects: tuple[int, int, int, int]) -> bytes:
    """A 640x360 'plate' (a horizontal gradient) with dark ``(x, y, w, h)`` objects on it, as JPEG."""
    img = np.zeros((360, 640, 3), np.uint8)
    img[:, :, :] = np.linspace(70, 190, 640, dtype=np.uint8)[None, :, None]
    for x, y, w, h in objects:
        img[y : y + h, x : x + w] = 15
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 95])
    assert ok
    return buf.tobytes()


_EMPTY = _frame()
_OCCUPIED = _frame((250, 120, 160, 140))


def _printer(**overrides) -> SimpleNamespace:
    fields = {
        "id": _PID,
        "ip_address": "10.0.0.52",
        "access_code": "code",
        "model": "H2S",
        "external_camera_enabled": False,
        "external_camera_url": None,
        "external_camera_type": None,
        "external_camera_snapshot_url": None,
        "plate_detection_roi_x": None,
        "plate_detection_roi_y": None,
        "plate_detection_roi_w": None,
        "plate_detection_roi_h": None,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


@pytest.fixture
def calibration_dir(tmp_path, monkeypatch):
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "plate_calibration_dir", tmp_path)
    return tmp_path


@pytest.fixture
def calibrated(calibration_dir):
    """The printer carries one reference: its empty plate."""
    success, _message, index = plate_detection.PlateDetector().calibrate(_EMPTY, _PID)
    assert success and index == 0


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TestTheVerdict:
    async def test_uncalibrated_is_unknown_and_no_frame_is_taken(self, calibration_dir):
        with patch(_CAPTURE, new_callable=AsyncMock, return_value=_EMPTY) as capture:
            verdict = await plate_detection.check_plate_empty(_printer(), not_before=_now())
        assert (verdict.kind, verdict.reason) == ("unknown", "uncalibrated")
        assert verdict.needs_calibration and not verdict.is_empty
        capture.assert_not_called()

    async def test_a_failed_capture_is_unknown(self, calibrated):
        with patch(_CAPTURE, new_callable=AsyncMock, return_value=None):
            verdict = await plate_detection.check_plate_empty(_printer(), not_before=_now())
        assert (verdict.kind, verdict.reason) == ("unknown", "capture_failed")

    async def test_a_frame_that_will_not_decode_is_unknown(self, calibrated):
        with patch(_CAPTURE, new_callable=AsyncMock, return_value=b"not a jpeg"):
            verdict = await plate_detection.check_plate_empty(_printer(), not_before=_now())
        assert (verdict.kind, verdict.reason) == ("unknown", "decode_failed")

    async def test_no_frame_after_the_anchor_is_unknown_and_opens_no_socket(self, calibrated, monkeypatch):
        """A live stream whose only frame predates the reading's anchor: no second socket (#1348), no
        reading from the old frame."""
        import time

        monkeypatch.setattr(camera_stills, "_BUFFER_WAIT_S", 0.2)
        with (
            patch("backend.app.api.routes.camera._active_streams", {f"{_PID}-fanout": MagicMock()}),
            patch("backend.app.api.routes.camera._last_frames", {_PID: _EMPTY}),
            patch("backend.app.api.routes.camera._last_frame_times", {_PID: time.time() - 1.0}),
            patch(_CAPTURE, new_callable=AsyncMock, return_value=_EMPTY) as capture,
        ):
            verdict = await plate_detection.check_plate_empty(_printer(), not_before=_now())
        assert (verdict.kind, verdict.reason) == ("unknown", "stale_frame")
        capture.assert_not_called()

    async def test_opencv_missing_is_unknown(self, calibrated, monkeypatch):
        monkeypatch.setattr(plate_detection, "OPENCV_AVAILABLE", False)
        verdict = await plate_detection.check_plate_empty(_printer(), not_before=_now())
        assert (verdict.kind, verdict.reason) == ("unknown", "opencv_unavailable")

    async def test_an_empty_plate_reads_empty_with_its_frames_instant(self, calibrated):
        anchor = _now()
        with patch(_CAPTURE, new_callable=AsyncMock, return_value=_EMPTY):
            verdict = await plate_detection.check_plate_empty(_printer(), not_before=anchor)
        assert verdict.kind == "empty" and verdict.reason is None and verdict.is_empty
        assert verdict.captured_at is not None and verdict.captured_at > anchor
        assert verdict.message.startswith("[captured] ")

    async def test_an_object_on_the_plate_reads_occupied(self, calibrated):
        with patch(_CAPTURE, new_callable=AsyncMock, return_value=_OCCUPIED):
            verdict = await plate_detection.check_plate_empty(_printer(), not_before=_now())
        assert verdict.kind == "occupied"
        assert verdict.difference_percent > plate_detection.PlateDetector.DEFAULT_DIFFERENCE_THRESHOLD

    def test_the_wire_shape(self):
        verdict = plate_detection.PlateVerdict(kind="unknown", reason="capture_failed", message="m")
        assert verdict.to_dict() == {
            "verdict": "unknown",
            "reason": "capture_failed",
            "is_empty": False,
            "confidence": 0.0,
            "difference_percent": 0.0,
            "message": "m",
            "has_debug_image": False,
            "needs_calibration": False,
        }


class TestTheComparedArea:
    def test_the_default_is_the_whole_frame(self, monkeypatch):
        assert plate_detection.DEFAULT_ROI == (0.0, 0.0, 1.0, 1.0)
        assert plate_detection.PlateDetector().roi == plate_detection.DEFAULT_ROI

    def test_a_printers_own_area_is_read_whole_zeros_included(self):
        printer = _printer(
            plate_detection_roi_x=0.0, plate_detection_roi_y=0.0, plate_detection_roi_w=0.5, plate_detection_roi_h=1.0
        )
        assert plate_detection.roi_of(printer) == (0.0, 0.0, 0.5, 1.0)

    def test_a_partial_area_is_no_area(self):
        assert plate_detection.roi_of(_printer(plate_detection_roi_x=0.1)) is None

    def test_detector_accepts_a_custom_area(self):
        assert plate_detection.PlateDetector(roi=(0.1, 0.2, 0.8, 0.6)).roi == (0.1, 0.2, 0.8, 0.6)

    def test_the_detector_refuses_without_opencv(self, monkeypatch):
        monkeypatch.setattr(plate_detection, "OPENCV_AVAILABLE", False)
        with pytest.raises(RuntimeError, match="OpenCV is not installed"):
            plate_detection.PlateDetector()


class TestCalibrationStatus:
    def test_status_without_opencv(self, monkeypatch):
        monkeypatch.setattr(plate_detection, "OPENCV_AVAILABLE", False)
        status = plate_detection.get_calibration_status(1)
        assert status["available"] is False and status["calibrated"] is False

    def test_availability_follows_opencv(self, monkeypatch):
        monkeypatch.setattr(plate_detection, "OPENCV_AVAILABLE", True)
        assert plate_detection.is_plate_detection_available() is True
        monkeypatch.setattr(plate_detection, "OPENCV_AVAILABLE", False)
        assert plate_detection.is_plate_detection_available() is False

    def test_delete_without_opencv(self, monkeypatch):
        monkeypatch.setattr(plate_detection, "OPENCV_AVAILABLE", False)
        assert plate_detection.delete_calibration(1) is False


class _Client:
    """A connected client: records the bed G-code and the chamber-light frames."""

    def __init__(self, *, light_on: bool = False) -> None:
        self.state = SimpleNamespace(chamber_light=light_on, connected=True)
        self.gcode: list[str] = []
        self.light: list[bool] = []

    def send_gcode(self, gcode: str, *, sequence_id=None) -> bool:
        self.gcode.append(gcode)
        return True

    def set_chamber_light(self, on: bool, *, sequence_id=None) -> bool:
        self.light.append(on)
        return True


@pytest.fixture
def sleeps(monkeypatch):
    """The motion and light-settle waits, recorded instead of slept."""
    waits: list[float] = []

    async def _sleep(delay):
        waits.append(delay)

    monkeypatch.setattr(plate_detection, "asyncio", SimpleNamespace(sleep=_sleep))
    return waits


@pytest.fixture
async def h2s_geometry(db_session):
    """The H2S registry row (the test DB is built with ``create_all`` only, so no seed rows exist)."""
    from backend.app.models.printer_model_geometry import PrinterModelGeometry

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
    await db_session.commit()


async def _row(db, model="H2S"):
    from backend.app.models.printer import Printer

    printer = Printer(
        name=f"PD-{model}", serial_number=f"SPD{model}", ip_address="1.2.3.4", access_code="x", model=model
    )
    db.add(printer)
    await db.commit()
    await db.refresh(printer)
    return printer


class TestCalibrationPositionsTheBedFirst:
    async def test_the_held_bed_lift_runs_then_a_frame_after_it_is_saved(
        self, db_session, h2s_geometry, calibration_dir, sleeps
    ):
        printer = await _row(db_session)
        client = _Client()
        with (
            patch.object(bed_motion.printer_manager, "get_client", return_value=client),
            patch(_CAPTURE, new_callable=AsyncMock, return_value=_EMPTY),
        ):
            success, message, index = await plate_detection.calibrate_plate(db_session, printer)

        assert success and index == 0, message
        assert client.gcode == [bed_motion.held_bed_lift_gcode(12.0)]
        assert sleeps == [pytest.approx(bed_motion.held_bed_lift_seconds(12.0))]
        assert plate_detection.PlateDetector().has_calibration(printer.id)

    async def test_a_busy_printer_refuses_the_calibration(self, db_session, h2s_geometry, calibration_dir, sleeps):
        printer = await _row(db_session)
        client = _Client()
        with (
            patch.object(bed_motion.printer_manager, "get_client", return_value=client),
            patch.object(bed_motion.printer_manager, "get_status", return_value=SimpleNamespace(state="RUNNING")),
            patch(_CAPTURE, new_callable=AsyncMock, return_value=_EMPTY) as capture,
        ):
            success, message, index = await plate_detection.calibrate_plate(db_session, printer)

        assert (success, index) == (False, -1)
        assert message == "Calibration refused: a job is running on the printer."
        assert client.gcode == [] and sleeps == []
        capture.assert_not_called()
        assert not plate_detection.PlateDetector().has_calibration(printer.id)

    async def test_a_printer_with_no_bed_on_z_calibrates_where_it_is(self, db_session, calibration_dir, sleeps):
        printer = await _row(db_session, model="A1")
        client = _Client()
        with (
            patch.object(bed_motion.printer_manager, "get_client", return_value=client),
            patch(_CAPTURE, new_callable=AsyncMock, return_value=_EMPTY),
        ):
            success, _message, _index = await plate_detection.calibrate_plate(db_session, printer)
        assert success
        assert client.gcode == [] and sleeps == []


class TestTheChamberLight:
    async def test_a_dark_chamber_is_lit_for_the_reading_and_restored(self, sleeps):
        client = _Client(light_on=False)
        with patch.object(plate_detection.printer_manager, "get_client", return_value=client):
            async with plate_detection.chamber_light_for_read(_PID) as anchor:
                assert client.light == [True]
                assert sleeps == [plate_detection.LIGHT_SETTLE_S]
            assert client.light == [True, False]
        assert anchor.tzinfo is not None

    async def test_a_lit_chamber_is_left_alone(self, sleeps):
        client = _Client(light_on=True)
        with patch.object(plate_detection.printer_manager, "get_client", return_value=client):
            async with plate_detection.chamber_light_for_read(_PID):
                pass
        assert client.light == [] and sleeps == []


class TestThePrintStartCheck:
    """``main.on_print_start``: only an OCCUPIED reading pauses the print; ``unknown`` never blocks it."""

    async def _start(self, verdict: plate_detection.PlateVerdict):
        from backend.app.main import on_print_start
        from backend.app.services import printer_action
        from backend.app.services.foreign_archive import ThreeMFLookup
        from backend.tests.unit.services.test_foreign_print_accounting import _foreign_printer, _start_harness

        printer = _foreign_printer()
        printer.plate_detection_enabled = True
        patches = _start_harness(printer, added=[])
        patches += [
            patch("backend.app.main.ws_manager", MagicMock(broadcast=AsyncMock(), send_print_start=AsyncMock())),
            patch(
                "backend.app.main.locate_3mf_for_print",
                new=AsyncMock(
                    return_value=ThreeMFLookup(local_path=None, filename=None, subtask_name="x", expected_plate=1)
                ),
            ),
            patch.object(plate_detection, "check_plate_empty", new=AsyncMock(return_value=verdict)),
            patch.object(printer_action, "send", new=AsyncMock(return_value=SimpleNamespace(published=True))),
        ]
        for p in patches:
            p.start()
        try:
            await on_print_start(3, {"filename": "x.gcode", "subtask_name": "x", "raw_data": {"subtask_id": "F-1"}})
            return printer_action.send
        finally:
            for p in reversed(patches):
                p.stop()

    @pytest.mark.parametrize("reason", ["uncalibrated", "capture_failed", "stale_frame", "decode_failed"])
    async def test_an_unknown_reading_never_pauses_the_print(self, reason):
        send = await self._start(plate_detection.PlateVerdict(kind="unknown", reason=reason, message="m"))
        assert "pause" not in [call.args[1].name for call in send.await_args_list if len(call.args) > 1]

    async def test_an_occupied_reading_pauses_the_print(self):
        send = await self._start(plate_detection.PlateVerdict(kind="occupied", message="m", difference_percent=7.0))
        assert "pause" in [call.args[1].name for call in send.await_args_list if len(call.args) > 1]
