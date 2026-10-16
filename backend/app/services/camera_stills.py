"""The ONE owner of a printer's camera STILL: the capture, its cache, and when the next one is due.

Every reader of "a picture of this printer now" asks here — the plate check (``plate_detection``:
the print-start check, the manual check, the calibration, the auto plate-clear) and the camera
routes (the snapshot, and ``GET /printers/{id}/camera/still`` for the touch page's tiles). So there
is ONE capture implementation (:func:`capture`) and ONE cache per printer.

**How a still is taken** (:func:`capture`), in order, under one of two POLICIES (:class:`StillPolicy`)
— :data:`VIEW` for a person looking (the snapshot route, the touch page's stills: today's camera
behaviour), :data:`READING` for a plate reading (plate detection, its calibration, the auto
plate-clear: the strict form):

* an EXTERNAL camera, when the printer configures one (:meth:`CameraSource.external_for`), is the
  only source used — a still never falls back from the external camera to the built-in one. VIEW
  takes it on ``external_camera_enabled`` with a URL (the snapshot route's rule); READING also needs
  its type (the #1359 rule: the manual check, the calibration and the print-start check read the SAME
  camera, and a plate check compares against references captured from ONE camera);
* a live stream registered for the printer (a viewer is watching) is the only built-in source while
  it stands, and no socket is ever opened beside it — most Bambu firmwares allow one camera
  connection, and a second one kicks the viewer or tears the fan-out down (#1348). A stream with no
  frame yet yields no still. VIEW serves the stream's buffered frame whatever its age; READING only a
  FRESH one (buffered within :data:`BUFFERED_MAX_AGE_S` and after the read's ``not_before``), waiting
  briefly for the stream's next frame;
* otherwise one fresh frame over the camera's own protocol (``camera.capture_camera_frame_bytes``).

**When a still is due** (:class:`Cadence`): every minute, backing off to 5 then 15 minutes while
nothing about the printer changes, and back to every minute on ANY change of its signature
(:func:`printer_signature`: the job state, the hold chip, the plate gate and its origin, the
cooldown phase, the HMS set, the farm's last action on a hold). One rule for the still cache here
and for the auto plate-clear's readings (``plate_autoclear``), each with its own clock.

**Captures per printer are independent of how many browsers look** (:func:`latest_still`): an
un-anchored request returns the cached still and captures only when the cadence says one is due,
counted from the last ATTEMPT (a capture that failed is not retried by the next poll); requests are
coalesced — one capture in flight per printer, every concurrent caller waits for it. An ANCHORED
request (``not_before``: a plate reading after the bed settled and the light came on, a person's
click) is served only by a frame taken after the anchor.

**Restart:** the cache, the cadence clocks and the in-flight captures are process memory with
nothing to rehydrate — a restart's first request finds no still and takes one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

from backend.app.services.plate_occupancy import plate_occupancy
from backend.app.services.printer_manager import printer_manager

if TYPE_CHECKING:
    from backend.app.models.printer import Printer

logger = logging.getLogger(__name__)

# The cadence: a still (or an auto plate-clear reading) every minute, backing off to 5 then 15
# minutes while the printer's signature stays the same; any change goes back to the first step.
CADENCE_S: tuple[float, ...] = (60.0, 300.0, 900.0)

# A live stream's buffered frame older than this is a STALLED stream, never "now" — the camera
# status route's own stall line (``camera_status``: no frame for more than 10 s).
BUFFERED_MAX_AGE_S = 10.0

# An anchored read on a printer whose stream is live waits at most this long for the stream's next
# buffered frame (the fan-out buffers a frame several times a second), polling the buffer at
# :data:`_BUFFER_POLL_S`. Past it the read has no still — never a socket beside the stream.
_BUFFER_WAIT_S = 2.0
_BUFFER_POLL_S = 0.1

# One capture's timeout (seconds): the snapshot route's figure.
_CAPTURE_TIMEOUT_S = 15

StillSource = Literal["external", "buffered", "captured"]
# Why a read has no still: the capture failed (camera off, ffmpeg error, a timeout), a live stream
# holds the camera with no frame fresh enough (no second socket, #1348), or the printer is gone.
StillMiss = Literal["capture_failed", "stream_stale", "no_printer"]


@dataclass(frozen=True)
class NoStill:
    """A read that produced no still, and why (:data:`StillMiss`)."""

    reason: StillMiss


@dataclass(frozen=True)
class StillPolicy:
    """How strict a read is about the frame it is given (module docstring). Two instances only:
    :data:`VIEW` and :data:`READING`."""

    name: Literal["view", "reading"]
    # The external camera counts only with its type (#1359).
    external_needs_type: bool
    # A stream's buffered frame must be fresh and taken after the read's ``not_before``.
    fresh_buffered_only: bool


VIEW = StillPolicy("view", external_needs_type=False, fresh_buffered_only=False)
READING = StillPolicy("reading", external_needs_type=True, fresh_buffered_only=True)


@dataclass(frozen=True)
class ExternalCamera:
    """An external camera's address, as the printer row configures it."""

    url: str
    camera_type: str | None
    snapshot_url: str | None


@dataclass(frozen=True)
class CameraSource:
    """Where a printer's still comes from. Built only by :meth:`of` — the one reader of the camera
    columns of a printer row."""

    printer_id: int
    ip_address: str
    access_code: str
    model: str | None
    # The external camera the printer ENABLES with a URL, whatever its type (None: none enabled).
    external: ExternalCamera | None
    # The caller's override: None takes the printer's own setting; False reads the built-in camera.
    use_external: bool | None

    @classmethod
    def of(cls, printer: Printer, *, use_external: bool | None = None) -> CameraSource:
        """The printer's camera configuration; :meth:`external_for` picks the camera a policy reads."""
        external = None
        if printer.external_camera_enabled and printer.external_camera_url:
            external = ExternalCamera(
                url=printer.external_camera_url,
                camera_type=printer.external_camera_type,
                snapshot_url=printer.external_camera_snapshot_url,
            )
        return cls(
            printer_id=printer.id,
            ip_address=printer.ip_address,
            access_code=printer.access_code,
            model=printer.model,
            external=external,
            use_external=use_external,
        )

    def external_for(self, policy: StillPolicy) -> ExternalCamera | None:
        """The external camera ``policy`` reads, or None for the built-in one. VIEW: the enabled camera
        (the snapshot route's rule). READING: the #1359 rule — enabled, with a URL AND a type, unless the
        caller says ``use_external=False``."""
        if self.external is None or self.use_external is False:
            return None
        if policy.external_needs_type and not self.external.camera_type:
            return None
        return self.external


@dataclass(frozen=True)
class Still:
    """One camera frame and the instant it was taken.

    ``captured_at`` is THE instant (UTC, microsecond resolution) every comparison reads — a frame's own
    instant is never "after" itself, which a float epoch round-tripped through a ``datetime`` (sub-
    microsecond digits lost) was: the same buffered frame read as newer than the reading it answered.
    Build one with :meth:`at`."""

    jpeg: bytes
    captured_at: datetime
    source: StillSource

    @classmethod
    def at(cls, jpeg: bytes, epoch_s: float, source: StillSource) -> Still:
        return cls(jpeg, datetime.fromtimestamp(epoch_s, tz=timezone.utc), source)

    @property
    def captured_ts(self) -> float:
        return self.captured_at.timestamp()

    @property
    def external(self) -> bool:
        return self.source == "external"

    def taken_after(self, moment: datetime | None) -> bool:
        """Was this frame taken strictly after ``moment`` (always, with no moment)?"""
        return moment is None or self.captured_at > moment


# The printer-changed signature: what a person looking at the printer would call "something changed".
Signature = tuple[object, ...]


@dataclass
class Cadence:
    """1 → 5 → 15 min while the signature stays the same; any change resets to 1 min (:data:`CADENCE_S`).

    ``note`` records an attempt (a capture, a reading) under the signature it was made at; ``due`` says
    whether the next one is owed under the signature NOW: the first is owed at once, a changed
    signature owes one a minute after the last attempt, an unchanged one the current step's interval.
    """

    step: int = 0
    signature: Signature | None = None
    last_mono: float | None = None

    def due(self, signature: Signature, now_mono: float) -> bool:
        if self.last_mono is None:
            return True
        interval = CADENCE_S[0] if signature != self.signature else CADENCE_S[self.step]
        return now_mono - self.last_mono >= interval

    def next_due_mono(self) -> float | None:
        """When the next attempt is owed while the signature holds (None: owed now)."""
        return None if self.last_mono is None else self.last_mono + CADENCE_S[self.step]

    def note(self, signature: Signature, now_mono: float) -> None:
        if self.last_mono is not None and signature == self.signature:
            self.step = min(self.step + 1, len(CADENCE_S) - 1)
        else:
            self.step = 0
        self.signature = signature
        self.last_mono = now_mono


@dataclass
class _Slot:
    """One printer's cached still, its cadence clock, and the lock that coalesces its captures."""

    still: Still | None = None
    cadence: Cadence = field(default_factory=Cadence)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_slots: dict[int, _Slot] = {}


def _reset_state() -> None:
    """Test support: forget every cached still, clock and lock (the conftest's one process-state reset)."""
    _slots.clear()


def _now_mono() -> float:
    return time.monotonic()


def _now_wall() -> float:
    return time.time()


def printer_signature(printer_id: int) -> Signature:
    """THE "printer changed" signature: the job state, the hold chip, the plate gate and its origin, the
    cooldown phase, the HMS set and the farm's last action on an open hold. DB-free (every part is a
    projection the status frame already reads); the farm's own camera commands (the chamber light, a
    bed motion) are not in it, so a reading never resets its own cadence."""
    from backend.app.services import printer_incidents
    from backend.app.services.eject.monitor import eject_cooldown_monitor
    from backend.app.services.printer_manager import hold_chip_payload

    state = printer_manager.get_status(printer_id)
    view = plate_occupancy.snapshot(printer_id)
    chip = hold_chip_payload(printer_id, state)
    watch = eject_cooldown_monitor.cooling_watch(printer_id)
    hms = frozenset(
        str(getattr(error, "full_code", "") or getattr(error, "code", ""))
        for error in (state.hms_errors if state else [])
    )
    farm_action_ids = [
        action.action_id
        for row in printer_incidents.snapshots(printer_id)
        if isinstance(row.get("id"), int)
        for action in printer_incidents.farm_actions(printer_id, row["id"])
    ]
    return (
        getattr(state, "state", None),
        json.dumps(chip, sort_keys=True, default=str) if chip is not None else None,
        view.plate_occupied,
        view.plate_origin,
        watch is not None,
        eject_cooldown_monitor.hold_z(printer_id) if watch is not None else None,
        eject_cooldown_monitor.deferred(printer_id) if watch is not None else None,
        hms,
        max(farm_action_ids, default=None),
    )


async def capture(source: CameraSource, *, policy: StillPolicy, not_before: datetime | None = None) -> Still | NoStill:
    """THE capture of one still from ``source`` under ``policy`` (module docstring), or why none could be
    had. ``not_before`` (READING): a buffered stream frame must have been taken after it (a fresh
    capture always is)."""
    external = source.external_for(policy)
    if external is not None:
        from backend.app.services.external_camera import capture_frame

        try:
            jpeg = await capture_frame(
                external.url,
                external.camera_type or "",
                timeout=_CAPTURE_TIMEOUT_S,
                snapshot_url=external.snapshot_url,
            )
        except Exception:  # noqa: BLE001 — a camera failure is "no still", never an exception to the reader
            logger.warning(
                "[camera-stills] printer %s: external camera capture failed", source.printer_id, exc_info=True
            )
            jpeg = None
        return Still.at(jpeg, _now_wall(), "external") if jpeg else NoStill("capture_failed")

    from backend.app.api.routes.camera import is_stream_active

    if is_stream_active(source.printer_id):
        return await _buffered_still(source.printer_id, not_before, policy)

    from backend.app.services.camera import capture_camera_frame_bytes

    try:
        jpeg = await capture_camera_frame_bytes(
            source.ip_address, source.access_code, source.model, timeout=_CAPTURE_TIMEOUT_S
        )
    except Exception:  # noqa: BLE001 — see above
        logger.warning("[camera-stills] printer %s: camera capture failed", source.printer_id, exc_info=True)
        jpeg = None
    return Still.at(jpeg, _now_wall(), "captured") if jpeg else NoStill("capture_failed")


async def _buffered_still(printer_id: int, not_before: datetime | None, policy: StillPolicy) -> Still | NoStill:
    """The live stream's buffered frame. Never a socket beside the stream (#1348): no frame, no still.

    VIEW: the frame there is, whatever its age (a frame the stream buffered with no recorded time — never
    in production, the stream records both — is stamped when read). READING: only a FRESH frame taken
    after ``not_before``; an anchored read waits up to :data:`_BUFFER_WAIT_S` for the stream's next one."""
    from backend.app.api.routes.camera import is_stream_active, live_buffered_frame

    deadline = _now_mono() + _BUFFER_WAIT_S
    while True:
        frame = live_buffered_frame(printer_id)
        if frame is not None:
            jpeg, stamp = frame
            if not policy.fresh_buffered_only:
                return Still.at(jpeg, stamp if stamp is not None else _now_wall(), "buffered")
            if stamp is not None and _now_wall() - stamp <= BUFFERED_MAX_AGE_S:
                still = Still.at(jpeg, stamp, "buffered")
                if still.taken_after(not_before):
                    return still
        waits = policy.fresh_buffered_only and not_before is not None
        if not waits or _now_mono() >= deadline or not is_stream_active(printer_id):
            logger.info(
                "[camera-stills] printer %s: a camera stream is live with no fresh frame — no still (no second socket)",
                printer_id,
            )
            return NoStill("stream_stale")
        await asyncio.sleep(_BUFFER_POLL_S)


async def _source_of(printer_id: int) -> CameraSource | None:
    from backend.app.core.database import async_session
    from backend.app.models.printer import Printer

    async with async_session() as db:
        printer = await db.get(Printer, printer_id)
    return CameraSource.of(printer) if printer is not None else None


def _serves(still: Still, source: CameraSource, policy: StillPolicy, not_before: datetime | None) -> bool:
    """Does the cached ``still`` answer a ``policy`` request for ``source`` anchored at ``not_before``?"""
    return still.external == (source.external_for(policy) is not None) and still.taken_after(not_before)


async def latest_still(
    printer_id: int,
    *,
    policy: StillPolicy,
    not_before: datetime | None = None,
    source: CameraSource | None = None,
) -> Still | NoStill:
    """The printer's still under ``policy`` (module docstring; every caller states its policy).
    ``not_before`` None: the cached still, refreshed only when the cadence says one is due — a failed
    refresh serves the last still. ``not_before`` set: never the cache from before it — a new capture
    (READING: a frame taken after it, or why there is none; VIEW: the stream's frame, whatever its age).
    ``source`` is the printer's camera (read from its row when omitted)."""
    slot = _slots.setdefault(printer_id, _Slot())
    async with slot.lock:
        if source is None:
            source = await _source_of(printer_id)
            if source is None:
                return NoStill("no_printer")
        signature = printer_signature(printer_id)
        anchored = not_before is not None
        cached = slot.still
        if cached is not None and _serves(cached, source, policy, not_before):
            if anchored or not slot.cadence.due(signature, _now_mono()):
                return cached
        result = await capture(source, policy=policy, not_before=not_before)
        slot.cadence.note(signature, _now_mono())
        if isinstance(result, Still):
            slot.still = result
            return result
        if not anchored and cached is not None and _serves(cached, source, policy, None):
            return cached
        return result


def next_capture_at(printer_id: int) -> datetime | None:
    """When the cached still is next refreshed while the printer's signature holds (None: none cached)."""
    slot = _slots.get(printer_id)
    if slot is None:
        return None
    due_mono = slot.cadence.next_due_mono()
    if due_mono is None:
        return None
    return datetime.fromtimestamp(_now_wall() + max(0.0, due_mono - _now_mono()), tz=timezone.utc)
