"""Auto plate-clear: a NORMAL leftover's gate clears itself once the camera shows the plate empty.

Operator request 2026-10-15 ("auto-clear = normal leftovers only"), Settings → Farm
``farm_plate_autoclear_enabled`` (default off). THE one owner of the automatic clear.

**Who is eligible** is ONE derived, TOTAL table — :data:`ELIGIBILITY`, over the gate's origin
(``plate_occupancy.PlateOrigin``) × the printer's holds × its state — never stored, built over every
value of all three vocabularies (an origin without a line in :data:`LEFTOVER_ORIGINS` fails at import,
a key outside the table raises). Eligible = all of:

* the gate's ORIGIN is a normal leftover — a print the farm did not dispatch (screen, Studio, LAN), an
  operator's stop of a print that had deposited, a deposit no unit owns, the operator's "Mark plate
  occupied". Never a refused plate, a first article, a farm part awaiting its eject, an eject that was
  stopped / unverified / never started, a terminal nobody saw (a restart), or an origin nobody can vouch
  for (``unknown``);
* NO hold of any kind is open on the printer, maintenance mode (``automation_held``) named apart;
* the printer is connected with a fresh report, runs no job, has no automatic eject armed over the
  plate, has no eject / dispatch / bed motion in flight, and its camera is calibrated.

**A reading** (:func:`_read`, one spawned task per printer, never awaited by the scheduler pass):

1. the first reading of an EPISODE (one gate: its ``since`` and origin) drives the bed to its bottom
   stop and lifts it to the held-bed height (``eject.bed_motion.lift_held_bed`` — a CLAIMED motion,
   the height the calibration reads from) and waits the claim's window: no end-of-motion report exists
   on the wire, so the motion has settled when its window lapses;
2. the chamber light on (``plate_detection.chamber_light_for_read``; restored after), and ONE reading
   of a frame taken after the light settled and after the previous reading's frame
   (``plate_detection.check_plate_empty(not_before=…)``) — so a frame never counts twice;
3. ``unknown`` counts as occupied. Two consecutive ``empty`` readings clear the gate through the plate
   transition the button uses (``plate_occupancy.clear_plate``: persisted, broadcast, the scheduler
   kicked) — never ``pause_recovery.on_plate_cleared``, the human-evidence closer (a camera is not a
   person, and no hold may stand anyway). No page; one INFO line, attributed ``auto``.

Readings follow the still owner's cadence (``camera_stills.Cadence`` over ``printer_signature``):
1 → 5 → 15 min while nothing about the printer changes, back to 1 min on any change.

**Restart:** episodes and their reading tasks are process memory. A restart starts a fresh episode
(the bed is positioned again, two new readings) — the safe direction.

Per scheduler pass: one settings read; for the g printers with a gate up, an in-memory eligibility
(their calibration is a file check) and at most one spawned task each when a reading is due.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Literal, get_args

from backend.app.core.tasks import spawn_background_task
from backend.app.schemas.settings import AppSettings
from backend.app.services import camera_stills, live_reading, plate_detection, printer_incidents
from backend.app.services.eject import bed_motion
from backend.app.services.plate_occupancy import (
    ACTIVE_PRINT_STATES,
    ORIGIN_UNKNOWN,
    CooldownEject,
    Evidence,
    FirstArticleEject,
    ForeignAutoEject,
    OccupancyView,
    PlateOrigin,
    plate_occupancy,
)
from backend.app.services.printer_manager import printer_manager

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

SETTING_KEY = "farm_plate_autoclear_enabled"
_ENABLED_DEFAULT: bool = bool(AppSettings.model_fields[SETTING_KEY].default)

# Consecutive ``empty`` readings of distinct frames that clear a gate.
EMPTY_READINGS_TO_CLEAR = 2

# The printer's holds, as eligibility reads them: none, maintenance mode, any other open hold.
Hold = Literal["none", "maintenance", "hold"]
# The printer's state, as eligibility reads it, first refusal first.
PrinterReading = Literal["ready", "disconnected", "job_active", "eject_armed", "in_flight", "uncalibrated"]
# The table's answer: eligible, or the dimension that refused.
Eligibility = Literal[
    "eligible",
    "origin",
    "maintenance",
    "hold",
    "disconnected",
    "job_active",
    "eject_armed",
    "in_flight",
    "uncalibrated",
]

# Is a gate of this origin a NORMAL leftover (operator answer 2026-10-15)? One line per origin: an
# origin added to the vocabulary without a line here fails the table's construction at import.
LEFTOVER_ORIGINS: dict[str, bool] = {
    "foreign": True,
    "operator_stop": True,
    "unattributed": True,
    "operator_declared": True,
    "farm_deposit": False,
    "first_article": False,
    "farm_unit": False,
    "plate_refused": False,
    "eject_unverified": False,
    "eject_never_started": False,
    "restart": False,
    "unknown": False,
}

# The policies under which the farm itself will sweep the plate: an automatic eject is armed.
_ARMED_EJECTS = (CooldownEject, FirstArticleEject, ForeignAutoEject)


def _cell(origin: str, hold: Hold, reading: PrinterReading) -> Eligibility:
    if not LEFTOVER_ORIGINS[origin]:
        return "origin"
    if hold != "none":
        return hold
    if reading != "ready":
        return reading
    return "eligible"


ELIGIBILITY: dict[tuple[str, Hold, PrinterReading], Eligibility] = {
    (origin, hold, reading): _cell(origin, hold, reading)
    for origin in get_args(PlateOrigin)
    for hold in get_args(Hold)
    for reading in get_args(PrinterReading)
}


@dataclass
class _Episode:
    """One gate's auto-clear episode. ``gate`` names the gate (its ``since`` and origin)."""

    gate: tuple[datetime | None, str | None]
    anchored: bool = False
    unanchorable: bool = False
    empty_streak: int = 0
    readings: int = 0
    last_frame_at: datetime | None = None
    cadence: camera_stills.Cadence = field(default_factory=camera_stills.Cadence)


_episodes: dict[int, _Episode] = {}
_tasks: dict[int, asyncio.Task[None]] = {}


def _reset_state() -> None:
    """Test support: forget every episode and task (the conftest's one process-state reset)."""
    _episodes.clear()
    _tasks.clear()


def _now_mono() -> float:
    return time.monotonic()


def _hold_of(printer_id: int) -> Hold:
    if printer_incidents.automation_held(printer_id):
        return "maintenance"
    return "hold" if printer_incidents.open_kinds(printer_id) else "none"


def _reading_of(printer_id: int, view: OccupancyView) -> PrinterReading:
    state = printer_manager.get_status(printer_id)
    if not printer_manager.is_connected(printer_id) or not live_reading.is_fresh(state):
        return "disconnected"
    if (getattr(state, "state", None) or "").upper() in ACTIVE_PRINT_STATES:
        return "job_active"
    if isinstance(view.plate_policy, _ARMED_EJECTS):
        return "eject_armed"
    if view.eject_present or view.lease_active or view.bed_motion_active:
        return "in_flight"
    if not plate_detection.get_calibration_status(printer_id).get("calibrated"):
        return "uncalibrated"
    return "ready"


def eligibility(printer_id: int) -> tuple[Eligibility, OccupancyView]:
    """THE eligibility of ``printer_id``'s standing gate — :data:`ELIGIBILITY` over (origin, hold, state),
    with the occupancy view it was read from (DB-free; the calibration is a file check)."""
    state = printer_manager.get_status(printer_id)
    view = plate_occupancy.snapshot(printer_id, Evidence(live_state=getattr(state, "state", None)))
    origin = view.plate_origin or ORIGIN_UNKNOWN
    return ELIGIBILITY[(origin, _hold_of(printer_id), _reading_of(printer_id, view))], view


async def _enabled(db: AsyncSession) -> bool:
    from backend.app.api.routes.settings import get_setting

    raw = await get_setting(db, SETTING_KEY)
    return _ENABLED_DEFAULT if raw is None else raw.strip().lower() == "true"


async def run_pass(db: AsyncSession) -> None:
    """The scheduler pass's hook (``print_scheduler.check_queue``): spawn a reading for every printer whose
    gate is eligible and whose next reading is due. Never awaits a reading."""
    if not await _enabled(db):
        if _episodes:
            logger.info("[plate-autoclear] setting off — %d episode(s) dropped", len(_episodes))
        _episodes.clear()
        return
    occupied = plate_occupancy.occupied_printers()
    for printer_id in [pid for pid in _episodes if pid not in occupied]:
        del _episodes[printer_id]
    for printer_id in occupied:
        task = _tasks.get(printer_id)
        if task is not None and not task.done():
            continue
        _consider(printer_id)


def _consider(printer_id: int) -> None:
    verdict, view = eligibility(printer_id)
    episode = _episodes.get(printer_id)
    if verdict != "eligible":
        if episode is not None:
            del _episodes[printer_id]
            logger.info("[plate-autoclear] printer %s: episode ended — not eligible (%s)", printer_id, verdict)
        return
    gate = (view.plate_since, view.plate_origin)
    if episode is None or episode.gate != gate:
        episode = _Episode(gate=gate)
        _episodes[printer_id] = episode
        logger.info("[plate-autoclear] printer %s: episode opened (gate origin %s)", printer_id, view.plate_origin)
    if episode.unanchorable:
        return
    if not episode.cadence.due(camera_stills.printer_signature(printer_id), _now_mono()):
        return
    _tasks[printer_id] = spawn_background_task(_read(printer_id, episode), name=f"plate-autoclear-p{printer_id}")


def _current(printer_id: int, episode: _Episode) -> bool:
    """Is ``episode`` still the printer's live episode, its gate still eligible and the same gate?"""
    if _episodes.get(printer_id) is not episode:
        return False
    verdict, view = eligibility(printer_id)
    return verdict == "eligible" and (view.plate_since, view.plate_origin) == episode.gate


async def _read(printer_id: int, episode: _Episode) -> None:
    """One reading of the episode (module docstring); the first one positions the bed. No session is held
    across a wait: one for the motion, one to load the printer row."""
    try:
        from backend.app.core.database import async_session
        from backend.app.models.printer import Printer

        if not _current(printer_id, episode):
            return
        if not episode.anchored:
            async with async_session() as db:
                motion = await bed_motion.lift_held_bed(db, printer_id)
            if isinstance(motion, bed_motion.MotionSkipped):
                if motion.permanent:
                    episode.unanchorable = True
                    logger.info(
                        "[plate-autoclear] printer %s: no bed position to read from (%s) — no auto-clear",
                        printer_id,
                        motion.reason,
                    )
                else:
                    episode.cadence.note(camera_stills.printer_signature(printer_id), _now_mono())
                    logger.info(
                        "[plate-autoclear] printer %s: bed not positioned (%s) — next try at the cadence",
                        printer_id,
                        motion.reason,
                    )
                return
            await asyncio.sleep(motion.seconds)
            episode.anchored = True
            logger.info(
                "[plate-autoclear] printer %s: bed at the held-bed height (%.1f mm off the bottom stop)",
                printer_id,
                motion.height_mm,
            )
            if not _current(printer_id, episode):
                return
        async with async_session() as db:
            printer = await db.get(Printer, printer_id)
        if printer is None:
            return
        async with plate_detection.chamber_light_for_read(printer_id) as lit_at:
            not_before = lit_at if episode.last_frame_at is None else max(lit_at, episode.last_frame_at)
            verdict = await plate_detection.check_plate_empty(printer, not_before=not_before)
        _note_reading(printer_id, episode, verdict)
    except Exception:  # noqa: BLE001 — a reading must never escape its task
        logger.warning("[plate-autoclear] printer %s: reading failed", printer_id, exc_info=True)


def _note_reading(printer_id: int, episode: _Episode, verdict: plate_detection.PlateVerdict) -> None:
    """Count the reading, and clear the gate on the second consecutive empty one."""
    episode.cadence.note(camera_stills.printer_signature(printer_id), _now_mono())
    episode.readings += 1
    if verdict.captured_at is not None:
        episode.last_frame_at = verdict.captured_at
    episode.empty_streak = episode.empty_streak + 1 if verdict.kind == "empty" else 0
    logger.info(
        "[plate-autoclear] printer %s: reading %d — %s%s (diff %.1f%%, frame %s; %d/%d empty)",
        printer_id,
        episode.readings,
        verdict.kind,
        f" ({verdict.reason})" if verdict.reason else "",
        verdict.difference_percent,
        verdict.captured_at.isoformat() if verdict.captured_at else "none",
        episode.empty_streak,
        EMPTY_READINGS_TO_CLEAR,
    )
    if episode.empty_streak < EMPTY_READINGS_TO_CLEAR:
        return
    if not _current(printer_id, episode):
        logger.info("[plate-autoclear] printer %s: plate read empty, but the gate changed — not cleared", printer_id)
        return
    refusal = plate_occupancy.clear_plate(printer_id)
    if refusal is not None:
        logger.warning("[plate-autoclear] printer %s: clear refused (%s)", printer_id, refusal)
        return
    _episodes.pop(printer_id, None)
    logger.info(
        "[plate-autoclear] printer %s: plate cleared (auto) — %d consecutive empty readings, gate origin %s",
        printer_id,
        EMPTY_READINGS_TO_CLEAR,
        episode.gate[1],
    )
