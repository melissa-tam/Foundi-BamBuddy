"""``services/camera_stills`` — the one owner of a printer's camera still.

What it promises, each driven through its public entry points with only the camera's own I/O
(``camera.capture_camera_frame_bytes``, ``external_camera.capture_frame``) and the stream registry
stood in for:

* the camera a still comes from, per POLICY: VIEW (a person looking — the snapshot route's rule: the
  external camera when it is enabled with a URL) and READING (a plate reading — the #1359 rule: enabled
  with a URL and a type; the caller's ``use_external`` override); never a fallback from the external
  camera to the built-in one;
* the cadence: 1 → 5 → 15 min while the printer's signature holds, back to 1 min on a change;
* captures per printer are independent of how many callers ask — cached between due slots, a failed
  refresh is not retried by the next poll, concurrent requests share one capture;
* an anchored read (``not_before``) is never served the cache from before its anchor;
* a live stream is the only built-in source while it stands (#1348): never a second socket, and a
  stream with no frame yet yields no still. VIEW serves its buffered frame whatever its age; READING
  only a fresh one taken after the anchor.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services import camera_stills
from backend.app.services.camera_stills import READING, VIEW
from backend.app.services.plate_occupancy import Evidence, plate_occupancy

_JPEG_A = b"\xff\xd8\xff\xe0A-frame"
_JPEG_B = b"\xff\xd8\xff\xe0B-frame"
_CAPTURE = "backend.app.services.camera.capture_camera_frame_bytes"
_EXTERNAL = "backend.app.services.external_camera.capture_frame"
_PID = 41


def _printer(**overrides) -> SimpleNamespace:
    fields = {
        "id": _PID,
        "ip_address": "10.0.0.41",
        "access_code": "code",
        "model": "H2S",
        "external_camera_enabled": False,
        "external_camera_url": None,
        "external_camera_type": None,
        "external_camera_snapshot_url": None,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _source(**overrides) -> camera_stills.CameraSource:
    return camera_stills.CameraSource.of(_printer(**overrides))


@pytest.fixture
def clock(monkeypatch):
    """The owner's two clocks, moved by the test: ``[mono, wall]``."""
    now = [1_000.0, 1_760_000_000.0]
    monkeypatch.setattr(camera_stills, "_now_mono", lambda: now[0])
    monkeypatch.setattr(camera_stills, "_now_wall", lambda: now[1])

    def advance(seconds: float) -> None:
        now[0] += seconds
        now[1] += seconds

    return advance


@pytest.fixture
def stream():
    """The camera routes' stream registry, empty: a test registers a stream / buffers a frame."""
    active: dict = {}
    frames: dict = {}
    times: dict = {}
    with (
        patch("backend.app.api.routes.camera._active_streams", active),
        patch("backend.app.api.routes.camera._last_frames", frames),
        patch("backend.app.api.routes.camera._last_frame_times", times),
    ):
        yield SimpleNamespace(active=active, frames=frames, times=times)


class TestWhichCamera:
    _TYPED = {
        "external_camera_enabled": True,
        "external_camera_url": "http://cam/mjpeg",
        "external_camera_type": "mjpeg",
    }

    def test_a_typed_enabled_external_camera_is_read_by_both_policies(self):
        """#1359: omitted → the printer's setting, so the manual check, the calibration and the
        print-start check read the same camera the snapshot shows."""
        source = _source(**self._TYPED)
        expected = camera_stills.ExternalCamera("http://cam/mjpeg", "mjpeg", None)
        assert source.external_for(READING) == expected
        assert source.external_for(VIEW) == expected

    def test_a_reading_needs_the_type_a_view_does_not(self):
        """The two pre-change rules, kept: the plate check's (enabled + URL + type) and the snapshot
        route's (enabled + URL) — a snapshot URL alone does not make a reading's camera external."""
        for untyped in (
            {"external_camera_enabled": True, "external_camera_url": "http://cam/x"},
            {
                "external_camera_enabled": True,
                "external_camera_url": "http://cam/x",
                "external_camera_snapshot_url": "http://cam/frame.jpeg",
            },
        ):
            source = _source(**untyped)
            assert source.external_for(READING) is None
            assert source.external_for(VIEW) is not None

    def test_an_explicit_false_reads_the_builtin_camera(self):
        printer = _printer(**self._TYPED)
        assert camera_stills.CameraSource.of(printer, use_external=False).external_for(READING) is None

    def test_true_without_an_external_camera_reads_the_builtin_one(self):
        assert camera_stills.CameraSource.of(_printer(), use_external=True).external_for(READING) is None

    def test_a_disabled_external_camera_is_not_used(self):
        source = _source(**{**self._TYPED, "external_camera_enabled": False})
        assert source.external_for(READING) is None and source.external_for(VIEW) is None

    @pytest.mark.parametrize("policy", [VIEW, READING], ids=["view", "reading"])
    async def test_a_failed_external_capture_never_falls_back_to_the_builtin_camera(self, clock, stream, policy):
        with (
            patch(_EXTERNAL, new_callable=AsyncMock, return_value=None),
            patch(_CAPTURE, new_callable=AsyncMock, return_value=_JPEG_A) as builtin,
        ):
            result = await camera_stills.capture(_source(**self._TYPED), policy=policy)
        assert result == camera_stills.NoStill("capture_failed")
        builtin.assert_not_called()


class TestCadence:
    def test_one_then_five_then_fifteen_minutes_while_nothing_changes(self):
        cadence = camera_stills.Cadence()
        sig = ("IDLE",)
        assert cadence.due(sig, 0.0)
        cadence.note(sig, 0.0)
        assert not cadence.due(sig, 59.0) and cadence.due(sig, 60.0)
        cadence.note(sig, 60.0)
        assert not cadence.due(sig, 359.0) and cadence.due(sig, 360.0)
        cadence.note(sig, 360.0)
        assert not cadence.due(sig, 1259.0) and cadence.due(sig, 1260.0)
        cadence.note(sig, 1260.0)
        assert not cadence.due(sig, 2159.0) and cadence.due(sig, 2160.0)  # stays at 15 min

    def test_any_change_resets_to_one_minute(self):
        cadence = camera_stills.Cadence()
        for at in (0.0, 60.0, 360.0):
            cadence.note(("IDLE",), at)
        assert not cadence.due(("IDLE",), 400.0)  # 15 min while unchanged
        assert cadence.due(("FINISH",), 420.0)  # a change: one minute after the last attempt
        cadence.note(("FINISH",), 420.0)
        assert cadence.step == 0 and cadence.next_due_mono() == 480.0


class TestLatestStill:
    async def test_callers_between_due_slots_share_the_cached_still(self, clock, stream):
        source = _source()
        with patch(_CAPTURE, new_callable=AsyncMock, side_effect=[_JPEG_A, _JPEG_B]) as capture:
            first = await camera_stills.latest_still(_PID, policy=VIEW, source=source)
            for _ in range(5):
                clock(10)
                assert await camera_stills.latest_still(_PID, policy=VIEW, source=source) == first
            assert capture.await_count == 1
            clock(10)  # 60 s after the first capture
            second = await camera_stills.latest_still(_PID, policy=VIEW, source=source)
        assert capture.await_count == 2
        assert (first.jpeg, second.jpeg) == (_JPEG_A, _JPEG_B)
        assert second.captured_at - first.captured_at == timedelta(seconds=60)

    async def test_a_failed_refresh_serves_the_last_still_and_is_not_retried_by_the_next_poll(self, clock, stream):
        source = _source()
        with patch(_CAPTURE, new_callable=AsyncMock, side_effect=[_JPEG_A, None, _JPEG_B]) as capture:
            first = await camera_stills.latest_still(_PID, policy=VIEW, source=source)
            clock(60)
            assert await camera_stills.latest_still(_PID, policy=VIEW, source=source) == first  # the refresh failed
            clock(30)
            assert await camera_stills.latest_still(_PID, policy=VIEW, source=source) == first  # not retried yet
            assert capture.await_count == 2

    async def test_a_printer_change_brings_the_next_still_forward(self, clock, stream):
        source = _source()
        with patch(_CAPTURE, new_callable=AsyncMock, return_value=_JPEG_A) as capture:
            for _ in range(3):
                await camera_stills.latest_still(_PID, policy=VIEW, source=source)
                clock(900)
            assert capture.await_count == 3  # now on the 15-minute step
            clock(-840)  # 60 s after the third capture
            await camera_stills.latest_still(_PID, policy=VIEW, source=source)
            assert capture.await_count == 3  # unchanged: not due for 15 min
            assert plate_occupancy.declare_occupied(_PID, Evidence(live_state="IDLE")) is None  # a change
            await camera_stills.latest_still(_PID, policy=VIEW, source=source)
            assert capture.await_count == 4

    async def test_concurrent_requests_share_one_capture(self, clock, stream):
        source = _source()
        release = asyncio.Event()

        async def slow_capture(*_args, **_kwargs):
            await release.wait()
            return _JPEG_A

        with patch(_CAPTURE, new=AsyncMock(side_effect=slow_capture)) as capture:
            readers = [
                asyncio.ensure_future(camera_stills.latest_still(_PID, policy=VIEW, source=source)) for _ in range(4)
            ]
            await asyncio.sleep(0)
            release.set()
            stills = await asyncio.gather(*readers)
        assert capture.await_count == 1
        assert {still.jpeg for still in stills} == {_JPEG_A}

    async def test_an_anchored_read_is_never_served_a_frame_from_before_its_anchor(self, clock, stream):
        source = _source()
        with patch(_CAPTURE, new_callable=AsyncMock, side_effect=[_JPEG_A, _JPEG_B]) as capture:
            cached = await camera_stills.latest_still(_PID, policy=READING, source=source)
            clock(5)
            anchor = cached.captured_at + timedelta(seconds=1)
            fresh = await camera_stills.latest_still(_PID, policy=READING, not_before=anchor, source=source)
            assert capture.await_count == 2
            assert fresh.jpeg == _JPEG_B and fresh.taken_after(anchor)
            # A frame already taken after the anchor answers it without a capture.
            assert await camera_stills.latest_still(_PID, policy=READING, not_before=anchor, source=source) == fresh
            assert capture.await_count == 2


class TestTheInstant:
    def test_a_frame_is_never_after_its_own_instant(self):
        """A float epoch with sub-microsecond digits once read as newer than its own ``captured_at`` —
        the same buffered frame then answered two readings. One canonical instant."""
        still = camera_stills.Still.at(_JPEG_A, 1_760_593_689.1093183, "buffered")
        assert not still.taken_after(still.captured_at)
        assert still.taken_after(still.captured_at - timedelta(microseconds=1))


class TestTheLiveStreamRule:
    @pytest.mark.parametrize("policy", [VIEW, READING], ids=["view", "reading"])
    async def test_a_stream_with_no_frame_yet_yields_no_still_and_opens_no_socket(self, clock, stream, policy):
        """#1348, under both policies."""
        stream.active[f"{_PID}-fanout"] = MagicMock()
        with patch(_CAPTURE, new_callable=AsyncMock, return_value=_JPEG_A) as capture:
            result = await camera_stills.latest_still(_PID, policy=policy, source=_source())
        assert result == camera_stills.NoStill("stream_stale")
        capture.assert_not_called()

    @pytest.mark.parametrize("policy", [VIEW, READING], ids=["view", "reading"])
    async def test_a_fresh_buffered_frame_is_the_still(self, clock, stream, policy):
        stream.active[f"{_PID}-fanout"] = MagicMock()
        stream.frames[_PID] = _JPEG_B
        stream.times[_PID] = camera_stills._now_wall() - 0.5
        with patch(_CAPTURE, new_callable=AsyncMock) as capture:
            result = await camera_stills.latest_still(_PID, policy=policy, source=_source())
        assert isinstance(result, camera_stills.Still)
        assert (result.jpeg, result.source) == (_JPEG_B, "buffered")
        capture.assert_not_called()

    async def test_a_reading_never_takes_a_stalled_streams_last_frame(self, clock, stream):
        stream.active[f"{_PID}-fanout"] = MagicMock()
        stream.frames[_PID] = _JPEG_B
        stream.times[_PID] = camera_stills._now_wall() - (camera_stills.BUFFERED_MAX_AGE_S + 1)
        with patch(_CAPTURE, new_callable=AsyncMock) as capture:
            result = await camera_stills.latest_still(_PID, policy=READING, source=_source())
        assert result == camera_stills.NoStill("stream_stale")
        capture.assert_not_called()

    async def test_a_view_takes_the_streams_frame_whatever_its_age(self, clock, stream):
        """Today's snapshot behaviour: the buffered frame, however old, and no socket beside the stream —
        also for a frame the anchored snapshot (``not_before`` = the request) arrived after."""
        stream.active[f"{_PID}-fanout"] = MagicMock()
        stream.frames[_PID] = _JPEG_B
        stamp = camera_stills._now_wall() - 600
        stream.times[_PID] = stamp
        anchor = datetime.fromtimestamp(camera_stills._now_wall(), tz=timezone.utc)
        with patch(_CAPTURE, new_callable=AsyncMock) as capture:
            result = await camera_stills.latest_still(_PID, policy=VIEW, not_before=anchor, source=_source())
        assert isinstance(result, camera_stills.Still) and result.jpeg == _JPEG_B
        assert result.captured_at == datetime.fromtimestamp(stamp, tz=timezone.utc)
        capture.assert_not_called()

    async def test_a_view_takes_a_frame_buffered_with_no_recorded_time(self, clock, stream):
        stream.active[f"{_PID}-fanout"] = MagicMock()
        stream.frames[_PID] = _JPEG_B
        with patch(_CAPTURE, new_callable=AsyncMock) as capture:
            view = await camera_stills.latest_still(_PID, policy=VIEW, source=_source())
            camera_stills._reset_state()
            reading = await camera_stills.latest_still(_PID, policy=READING, source=_source())
        assert isinstance(view, camera_stills.Still) and view.jpeg == _JPEG_B
        assert reading == camera_stills.NoStill("stream_stale")
        capture.assert_not_called()

    async def test_an_anchored_reading_waits_for_the_streams_next_frame(self, stream):
        """Real clocks: the stream buffers a frame 0.2 s after the anchor; the reading takes that one."""
        stream.active[f"{_PID}-fanout"] = MagicMock()
        stream.frames[_PID] = _JPEG_A
        stream.times[_PID] = time.time() - 0.3
        anchor = datetime.now(timezone.utc)

        async def next_frame():
            await asyncio.sleep(0.2)
            stream.frames[_PID] = _JPEG_B
            stream.times[_PID] = time.time()

        feeder = asyncio.ensure_future(next_frame())
        with patch(_CAPTURE, new_callable=AsyncMock) as capture:
            result = await camera_stills.latest_still(_PID, policy=READING, not_before=anchor, source=_source())
        await feeder
        assert isinstance(result, camera_stills.Still) and result.jpeg == _JPEG_B
        capture.assert_not_called()


class TestTheStillRoute:
    async def test_captures_do_not_grow_with_the_number_of_callers(self, async_client, printer_factory):
        printer = await printer_factory()
        with patch(_CAPTURE, new_callable=AsyncMock, return_value=_JPEG_A) as capture:
            first = await async_client.get(f"/api/v1/printers/{printer.id}/camera/still")
            for _ in range(4):
                again = await async_client.get(f"/api/v1/printers/{printer.id}/camera/still")
                assert again.status_code == 200 and again.content == _JPEG_A
        assert capture.await_count == 1
        assert first.status_code == 200
        assert first.headers["content-type"] == "image/jpeg"
        captured_at = datetime.fromisoformat(first.headers["x-captured-at"])
        next_at = datetime.fromisoformat(first.headers["x-next-capture-at"])
        assert timedelta(seconds=59) <= next_at - captured_at <= timedelta(seconds=61)

    async def test_an_unchanged_still_answers_304(self, async_client, printer_factory):
        printer = await printer_factory()
        with patch(_CAPTURE, new_callable=AsyncMock, return_value=_JPEG_A):
            first = await async_client.get(f"/api/v1/printers/{printer.id}/camera/still")
            again = await async_client.get(
                f"/api/v1/printers/{printer.id}/camera/still", headers={"If-None-Match": first.headers["etag"]}
            )
        assert again.status_code == 304
        assert again.content == b""

    async def test_unknown_printer_is_404_and_no_still_is_503(self, async_client, printer_factory):
        assert (await async_client.get("/api/v1/printers/99999/camera/still")).status_code == 404
        printer = await printer_factory()
        with patch(_CAPTURE, new_callable=AsyncMock, return_value=None):
            response = await async_client.get(f"/api/v1/printers/{printer.id}/camera/still")
        assert response.status_code == 503
