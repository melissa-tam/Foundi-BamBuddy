"""The farm's server-side BED MOTIONS: the ONE builder of their G-code and the ONE claimed send.

Two motions exist, each a named function, and nothing else in the app spells their G-code:

* **the held-bed lift** (:func:`held_bed_lift_gcode`) — the vendor's own guarded pair from the stock
  machine-start block's ``;===== avoid end stop =====``: ``G380 S2 Z<+32>`` drives the bed DOWN onto
  its bottom stop (a relative move guarded by that stop, so a bed already on it truncates to ~0 and a
  bed above it moves AWAY from the nozzle), then ``G380 S2 Z-<hold_lift_mm>`` lifts it off the stop by
  the model's registry figure, clear of the plate-release aids. It reads NO coordinate of the
  firmware's Z frame, which is why it is the one motion allowed over an occupied plate: a refused
  plate's terminal (``farm_policy``), the auto plate-clear's camera position (``plate_autoclear``) and
  the plate-detection calibration (``plate_detection.calibrate_plate``) all take it, so the camera
  reads every plate from the same height;
* **the idle deep-park** (:func:`idle_deep_park_gcode`) — an ABSOLUTE ``G1 Z`` to a share of the
  model's Z travel, sent only after a clean production eject released the plate (``farm_policy``,
  UNCLAIMED, as before the extraction: it holds no dispatch off). An absolute Z is only as good as
  the frame it runs in, so it is never sent over a gated plate (the 002-H2S bed-into-the-floor shape).

``M400`` before ``M18`` in both: drain the motion queue before the steppers are released, or the bed
drops the rest of the way under its own weight.

**The held-bed lift is CLAIMED** on the plate authority (``plate_occupancy.claim_bed_motion``) for its
own motion time before the send (:func:`lift_held_bed` → :func:`send_claimed`), and sent through the
command record (``printer_action.gcode`` — a ``gcode_line`` frame, recorded, no echo by design). The claim is refused under a running job, a
dispatch or an eject in flight, or another motion; a send the printer did not take releases it at once
(``release_bed_motion``). There is no end-of-motion report on the wire (open item), so "the motion
settled" is the claim's window lapsing — read-time, the way ``dispatchable`` already reads it.

The G-code of both motions is byte-identical to the inline builders it replaced in ``farm_policy``
(pinned by ``test_bed_motion.py``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from sqlalchemy import select

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services import printer_action
from backend.app.services.eject import geometry as eject_geometry
from backend.app.services.eject.generator import Z_REFERENCE_FEED_MM_MIN
from backend.app.services.plate_occupancy import Evidence, TransitionRefusal, plate_occupancy
from backend.app.services.printer_manager import printer_manager
from backend.app.utils.printer_models import is_bedslinger_model

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# The vendor's own pre-home Z primitive, from the stock H2S machine-start block's
# ``;===== avoid end stop =====`` sequence (``G380 S2 Z32 F1200`` / ``G380 S2 Z-12 F1200``).
# 32 mm is the vendor's figure, kept rather than re-derived: its only job is to reach the bottom
# stop from wherever the bed sits. The FEED of both moves is the same stock block's F1200, whose one
# origin is ``eject.generator.Z_REFERENCE_FEED_MM_MIN``.
VISION_HOLD_PROBE_MM = 32.0

# The idle deep-park's feed (mm/min): the eject block's own Z feed, the speed every farm Z move over a
# cleared plate has always used.
IDLE_PARK_FEED_MM_MIN = 900

# Why a motion was not sent. ``no_printer`` / ``bedslinger`` / ``no_z_travel`` are PERMANENT for the
# printer (no bed-on-Z to move); ``no_client`` / ``not_sent`` and the authority's refusals are not.
MotionSkip = Literal["no_printer", "bedslinger", "no_z_travel", "no_client", "not_sent"] | TransitionRefusal
PERMANENT_SKIPS: frozenset[str] = frozenset({"no_printer", "bedslinger", "no_z_travel"})


@dataclass(frozen=True)
class MotionSent:
    """The motion went out under a claim lasting ``seconds`` (its own motion time). ``height_mm`` is
    where it takes the bed: the lift's height off the bottom stop."""

    seconds: float
    height_mm: float


@dataclass(frozen=True)
class MotionSkipped:
    """Nothing moved; ``reason`` says why (:data:`MotionSkip`)."""

    reason: MotionSkip

    @property
    def permanent(self) -> bool:
        """True when this printer can never take the motion (no bed-on-Z axis)."""
        return self.reason in PERMANENT_SKIPS


MotionOutcome = MotionSent | MotionSkipped


def held_bed_lift_gcode(hold_lift_mm: float) -> str:
    """The held-bed lift: guarded DOWN onto the bottom stop, then guarded UP by ``hold_lift_mm``."""
    feed = Z_REFERENCE_FEED_MM_MIN
    return (
        f"M17\nG91\nG380 S2 Z{VISION_HOLD_PROBE_MM:.1f} F{feed}\nG380 S2 Z-{hold_lift_mm:.1f} F{feed}\nG90\nM400\nM18"
    )


def held_bed_lift_seconds(hold_lift_mm: float) -> float:
    """The lift's motion time: both guarded moves at their FULL commanded distance over the one feed —
    counted in full, as the eject runtime model counts a ``G380`` (a guarded move that stops early only
    finishes sooner)."""
    return (VISION_HOLD_PROBE_MM + hold_lift_mm) / (Z_REFERENCE_FEED_MM_MIN / 60.0)


def idle_deep_park_gcode(park_z: float) -> str:
    """The idle deep-park: an absolute ``G1 Z`` to ``park_z`` (only ever over a cleared plate)."""
    return f"M17\nG90\nG1 Z{park_z:.1f} F{IDLE_PARK_FEED_MM_MIN}\nM400\nM18"


async def lift_held_bed(db: AsyncSession, printer_id: int) -> MotionOutcome:
    """Claim and send the held-bed lift on ``printer_id``. The one verb every caller takes.

    Skipped (permanently) on a bedslinger — the gantry carries Z, there is no bed travel to lift —
    and on a model with no geometry row or no ``z_travel_mm`` (no proven Z axis to move).
    """
    printer = await db.get(Printer, printer_id)
    if printer is None:
        return MotionSkipped("no_printer")
    if is_bedslinger_model(printer.model):
        return MotionSkipped("bedslinger")
    geometry = await eject_geometry.get_geometry(db, printer.model)
    if geometry is None or geometry.z_travel_mm is None:
        return MotionSkipped("no_z_travel")
    return await send_claimed(
        db,
        printer_id,
        held_bed_lift_gcode(geometry.hold_lift_mm),
        seconds=held_bed_lift_seconds(geometry.hold_lift_mm),
        height_mm=geometry.hold_lift_mm,
    )


async def send_claimed(
    db: AsyncSession, printer_id: int, gcode_text: str, *, seconds: float, height_mm: float
) -> MotionOutcome:
    """Claim a bed motion of ``seconds`` on the plate authority, then send ``gcode_text`` through the
    record. A refused claim sends nothing; a send the printer did not take releases the claim.

    The claim's evidence is the wire's word and the scheduler's durable witness of a dispatch already
    on its way (a ``printing`` row on this printer) — a print starting here drives the bed off the stop
    with its own start block, so a farm motion is never owed under it.
    """
    client = printer_manager.get_client(printer_id)
    if client is None:
        return MotionSkipped("no_client")
    status = printer_manager.get_status(printer_id)
    printing_row = await db.scalar(
        select(PrintQueueItem.id)
        .where(PrintQueueItem.printer_id == printer_id)
        .where(PrintQueueItem.status == "printing")
        .limit(1)
    )
    refusal = plate_occupancy.claim_bed_motion(
        printer_id,
        Evidence(live_state=getattr(status, "state", None), db_claim=printing_row is not None),
        hold_s=seconds,
    )
    if refusal is not None:
        return MotionSkipped(refusal)
    sent = await printer_action.send(
        printer_id, printer_action.gcode(gcode_text), actor=printer_action.ACTOR_FARM, client=client
    )
    if not sent.published:
        plate_occupancy.release_bed_motion(printer_id)
        return MotionSkipped("not_sent")
    return MotionSent(seconds=seconds, height_mm=height_mm)
