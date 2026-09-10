"""Cooldown prep — the ONE owner of what the farm does to a printer while its
finished plate cools.

Two actuators, both armed by :func:`begin` at the top of the cooldown watch and
retired by :meth:`CooldownPrep.end` when that watch exits:

* the **plate hold** (production / first-article units only) — the bed is raised so the
  part top sits where ``farm_cooldown_hold_part_top_mm`` asks for it relative to the
  nozzle plane, with the toolhead parked at the chute; and
* the **auxiliary fan** — ``M106 P2`` at ``farm_cooldown_aux_fan_percent``.

**Why the plate is moved at all.** The aux fan is a fixed duct on the left wall
aimed at the layer being printed: its stream is centred on the nozzle plane (Z 0),
and the stock H2 end block parks the bed by the vendor template
``max_layer_z + 100 - max_layer_z/2`` then ``+98`` (file-verified: a 50.1 mm H2S part
ends at Z123.05). A finished part therefore cools with its top ~73 mm and its plate
~123 mm under the stream — blowing on it is blowing on nothing. The hold brings the
plate TO the stream, which is what makes the fan worth switching on. Baseline the
pair is measured against: 84 armed→dispatch pairs 2026-09-05→09-10, median 63.4 min
(p90 81.7) of pure natural convection, ~17 % of every production cycle.

**The measured precondition.** ``begin`` runs on a printer whose job has just ended,
and the FINISH terminal it rides in on arrives AFTER the stock end block has run to
completion — measured 2026-08-31 as a 23-27 s drop-clear→terminal tail. At this
moment the bed is parked at the vendor's height, the toolhead has already made its
own ``G150.3`` travel to the chute, the steppers are released (``M18``) and the
printer is idle. That is why the hold can be nine lines over the G-code command
channel instead of a job: it re-engages the motors, moves, and releases them again.

**Why ``G150.3`` is COMMANDED rather than assumed**, and where every emitted
coordinate comes from: see
:func:`~backend.app.services.eject.generator.cooldown_hold_lines`, which owns that
reasoning. Nothing here computes a height — this module decides WHETHER to hold, the
generator decides WHERE.

**The operator rulings this rests on** (eyewitness, 2026-09-10, authoritative): with
the toolhead parked at the chute the space above the nozzle plane over the part area
is clear to 100 mm, and that clearance is a PHYSICAL machine limit rather than an
operator setting — so both numbers live in the ``printer_model_geometry`` registry as
seed-only columns (H2S ``clear_above_mm=100.0`` MEASURED / ``keepout_y_mm=285.0``;
every other model NULL, which means fan only). Red line 2 (the hardware ladder) was
WAIVED by the operator for this wave: the first production cooldown + eject is the
witness, with the eject runtime watchdog and the human-clear plate gate as the net.
Do not re-ladder it. The standing operator rule while a plate is held: do not jog the
toolhead from the touchscreen — the whole safety case is that the toolhead is AT the
chute.

**What the operator chooses, and what the machine keeps.** The registry number is the
CEILING; where inside it the part is held is ``farm_cooldown_hold_part_top_mm``, the
signed height of the part's TOP above the nozzle plane (100 = as high as the part
allows, the plate right at the fan; 0 = the fan blows across the top; negative = the
fan blows above the part), and ``farm_cooldown_hold_enabled`` switches the hold off
altogether without a deploy. Both are read ONCE per arm, by the watch, and handed in
here — and the record below stores what was actually SENT, so an operator changing the
setting mid-cooldown can never desynchronise the eject's ``start_z`` seed from the
plate's real position. A lower hold costs the eject nothing either: its first Z move
simply starts from farther away, and the drop-span deadline follows the seed.

**Re-entry is safe by construction.** A server restart re-arms the watch, so
``begin`` can run again on a plate that is ALREADY held at ~Z2. That needs no special
case: the hold's FIRST move is the transit to ``park_z``, which lowers an
already-held plate back to the height the vendor's own end block runs ``G150.3`` at,
BEFORE the toolhead is asked to move anywhere. Held, vendor-parked, or wherever a
screen jog left it — every entry passes through the same clear transit height.

Every failure is one log line naming the reason and a cooldown that proceeds without
that actuator. :func:`begin` never raises: losing the fan or the hold costs minutes,
while an exception out of the watch's arm path would strand the plate-clear gate
behind a dead watch — the armless-gate outcome 2026-07-18 / 07-21 forbids.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from backend.app.services.eject import donor, generator, remote as eject_remote
from backend.app.services.eject.generator import EjectGenerationError
from backend.app.services.eject.geometry import GeometryUnavailable, get_geometry_required
from backend.app.services.plate_occupancy import ACTIVE_PRINT_STATES, plate_occupancy
from backend.app.services.printer_manager import printer_manager
from backend.app.utils.printer_models import is_bedslinger_model

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# The auxiliary fan's index on the wire (1 = part cooling, 2 = auxiliary, 3 = chamber).
# Named because it appears three times below and a wrong index blows on the wrong thing.
_AUX_FAN = 2

# What the plate hold did. Every value but ``sent`` is a cooldown that ran with the
# plate where the end block left it — never an error the caller has to handle.
HoldOutcome = Literal[
    "sent",
    "skipped:foreign",  # queue_item_id is None — the foreign auto-eject watch never holds
    "skipped:disabled",  # farm_cooldown_hold_enabled is off — INFO, a deliberate operator state
    "skipped:item_missing",
    "skipped:profile_missing",
    "skipped:geometry",  # GeometryUnavailable (no row, or not hardware-validated)
    "skipped:no_numbers",  # either registry number NULL → fan-only model (H2C today)
    "skipped:bedslinger",  # G1 Z moves the GANTRY toward the part, not the bed away from it
    "skipped:donor",
    "skipped:max_z",
    "skipped:over_height",  # EjectGenerationError from cooldown_hold_lines
    "skipped:keepout",  # bbox unreadable / multi-filament / a part inside the chute strip
    "skipped:disconnected",
    "skipped:not_ejectable",  # ejectable() refused (the refusal token goes in the WARN)
    "skipped:publish",  # no client, or send_gcode returned False
    "skipped:error",  # any other exception (logged with its traceback)
]

# What the end-of-cooldown fan OFF did. ``skipped:active`` is the normal release-path
# value: by the time the watch exits, the eject job it dispatched is usually already
# running — and that job's own prologue carries a redundant ``M106 P2 S0``.
FanOffOutcome = Literal[
    "sent",
    "skipped:not_wanted",
    "skipped:not_published",
    "skipped:active",
    "skipped:no_client",
    "skipped:publish",
]


def _mm(value: float | None) -> str:
    """A millimetre figure for the log line, or ``none``."""
    return "none" if value is None else f"{value:.2f}"


def _live_state(printer_id: int) -> str | None:
    """The printer's live ``gcode_state``, or None when nothing is readable.

    None is NOT active: an unreadable printer is one whose job cannot be observed to
    own it, and both actuators here are safe on an idle machine. The active-state set
    is the plate-occupancy authority's own (:data:`ACTIVE_PRINT_STATES`) — the one
    place that defines "a job owns this printer".
    """
    state = printer_manager.get_status(printer_id)
    return getattr(state, "state", None) if state is not None else None


def _observed_fan(printer_id: int) -> int | None:
    """The live ``big_fan1_speed`` (aux fan), or None when unreadable.

    A WITNESS, never a confirmation. The wire reports a 0-15 LEVEL that
    ``parse_fan_speed`` rescales to percent, so an observation is quantised to
    multiples of ~6.67 and need not equal the percent that was commanded. Nothing in
    this module (or its tests) may assert equality between the two.
    """
    state = printer_manager.get_status(printer_id)
    return getattr(state, "big_fan1_speed", None) if state is not None else None


@dataclass
class CooldownPrep:
    """What :func:`begin` actually managed to arm, and how to retire it.

    Deliberately a plain value the caller holds for the life of its watch: there is
    no holder token, no module-level registry and no reconcile loop, because the ONE
    thing that knows whether a printer is still cooling is the watch task itself. If
    this process dies mid-cooldown the fan keeps running and the plate stays held —
    both are recovered by the next watch's ``begin`` (re-entrant by construction) or
    by the eject block's own ``M106 P2 S0``, not by anything remembered here.
    """

    printer_id: int
    hold: HoldOutcome
    # The Z the plate is being HELD at — set only when the hold was actually sent, so
    # a caller can seed the eject estimator with a measurement instead of a bound.
    hold_z: float | None
    # The part height read from the donor, whenever it got that far (diagnostic: it is
    # what distinguishes an over-height refusal from a plate nobody could measure).
    max_z: float | None
    fan_percent: int
    fan_published: bool
    started_at: float  # time.monotonic()
    # The arm-time witness's wait, carried here (not awaited inside :func:`begin`) so
    # that NO await sits between "fan commanded ON" and the caller holding this handle:
    # a watch cancelled during the settle then still reaches its ``finally`` and
    # :meth:`end`, which is what decides whether the fan goes back off. Injected for
    # tests only; production waits on the event loop's own sleep.
    settle_s: float = 3.0
    sleep: Callable[[float], Awaitable[None]] = field(default=asyncio.sleep, repr=False)

    async def observe_start(self) -> None:
        """Log the ONE arm-time witness of the fan command, after the wire settles.

        The only evidence that a published fan command reached the hardware at all,
        sampled after ``settle_s`` because a read taken before the next status push
        would witness the OLD value. Logged and never asserted on — see
        :func:`_observed_fan` for why the two numbers legitimately differ. A no-op when
        nothing was published. Cancellation propagates (the caller's ``finally`` owns
        the retirement); every other failure is swallowed.
        """
        if not self.fan_published:
            return
        await self.sleep(self.settle_s)
        try:
            logger.info(
                "[cooldown-prep] printer %s: fan_observed_at_start=%s", self.printer_id, _observed_fan(self.printer_id)
            )
        except Exception:  # noqa: BLE001 — a witness read never costs the cooldown its watch
            logger.exception("[cooldown-prep] printer %s: fan witness read failed", self.printer_id)

    def end(self, *, fan_off: bool) -> None:
        """Retire the prep and log the ONE summary line. Sync, and never raises.

        ``fan_off`` is the caller's answer to "does this printer still want its aux
        fan" — the monitor asks whether a successor cooldown-class watch is armed, so
        a watch that was cancelled to make way for another printer's-plate policy
        hands the fan over instead of switching it off under its successor.

        The plate is deliberately NOT lowered here. It stays held exactly where the
        hold left it and the eject block's own first Z move takes it from there in one
        flow; on any other exit (an operator clearing the gate, a stall) the plate is
        parked at a safe height with the steppers released, which is the same state
        the stock end block leaves behind.
        """
        try:
            fan_off_outcome = self._switch_fan_off(fan_off)
        except Exception:  # noqa: BLE001 — the summary line must still be written
            logger.exception("[cooldown-prep] printer %s: fan off failed", self.printer_id)
            # The publish is the only thing that can throw here, and it did not land.
            fan_off_outcome = "skipped:publish"
        try:
            observed = _observed_fan(self.printer_id)
        except Exception:  # noqa: BLE001 — an unreadable witness is not a failure
            observed = None
        # THE line the wave is measured by: one per cooldown, greppable as
        # ``[cooldown-prep]``, carrying every decision this module made.
        logger.info(
            "[cooldown-prep] printer %s: cooldown ended after %.0f s "
            "(hold=%s max_z=%s hold_z=%s, fan=%s%%, fan_published=%s, fan_off=%s, fan_observed=%s)",
            self.printer_id,
            time.monotonic() - self.started_at,
            self.hold,
            _mm(self.max_z),
            _mm(self.hold_z),
            self.fan_percent,
            self.fan_published,
            fan_off_outcome,
            "none" if observed is None else observed,
        )

    def _switch_fan_off(self, fan_off: bool) -> FanOffOutcome:
        """Switch the aux fan off, or say why not. The only I/O :meth:`end` does."""
        if not fan_off:
            return "skipped:not_wanted"
        if not self.fan_published:
            # Never turned it on — so turning it off would be this module commanding a
            # fan it does not own (an operator's manual /fan-speed, say).
            return "skipped:not_published"
        if _live_state(self.printer_id) in ACTIVE_PRINT_STATES:
            # The eject sweep this cooldown released into is already running. Its own
            # prologue carries the fan OFF; commanding one now would race that job.
            return "skipped:active"
        client = printer_manager.get_client(self.printer_id)
        if client is None:
            return "skipped:no_client"
        return "sent" if client.set_fan_percent(_AUX_FAN, 0) else "skipped:publish"


async def begin(
    printer_id: int,
    *,
    queue_item_id: int | None,
    aux_fan_percent: int,
    hold_enabled: bool,
    hold_part_top_mm: int,
    settle_s: float = 3.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> CooldownPrep:
    """Arm the cooldown actuators for ``printer_id``. NEVER raises.

    Order is hold first, then fan: the hold is the one that moves the machine, and it
    wants the printer in exactly the idle state the end block left — the fan changes
    nothing about that, but doing it first would put a publish between the terminal
    and the motion for no reason.

    ``queue_item_id`` is None for the foreign auto-eject watch, whose plate carries no
    farm unit: no unit means no donor, no profile and no measured part height, so
    there is nothing to hold the plate SAFELY at and the foreign lane gets the fan
    alone. ``hold_enabled``/``hold_part_top_mm`` are the operator's switch and target,
    resolved once by the watch that arms this prep and never re-read here.
    ``settle_s``/``sleep`` are injected only so tests need not wait.
    """
    started_at = time.monotonic()
    hold, hold_z, max_z = await _hold_plate(
        printer_id, queue_item_id, hold_enabled=hold_enabled, part_top_mm=hold_part_top_mm
    )
    # The fan publish is the LAST thing before the handle is returned — nothing is
    # awaited after it (the witness wait lives in ``CooldownPrep.observe_start``), so
    # a cancellation can never separate an ON that was sent from the ``end`` that
    # decides whether it stays on.
    fan_published = _start_aux_fan(printer_id, aux_fan_percent)
    return CooldownPrep(
        printer_id=printer_id,
        hold=hold,
        hold_z=hold_z,
        max_z=max_z,
        fan_percent=aux_fan_percent,
        fan_published=fan_published,
        started_at=started_at,
        settle_s=settle_s,
        sleep=sleep,
    )


async def _hold_plate(
    printer_id: int, queue_item_id: int | None, *, hold_enabled: bool, part_top_mm: int
) -> tuple[HoldOutcome, float | None, float | None]:
    """Send the plate hold, or return the reason it was skipped.

    Returns ``(outcome, hold_z, max_z)``. ``hold_z`` is non-None ONLY on ``sent`` —
    it is what the eject estimator is seeded from, and seeding it from a hold that
    did not happen would tell the runtime watchdog the bed is 120 mm closer to the
    nozzle than it is. ``max_z`` is returned whenever it was read, sent or not.

    The gate order is cheapest-and-most-permanent first: the two refusals that need no
    state at all come before the session is even opened, a model with no registry
    numbers can never hold so it is not worth opening a 3MF for, and the admission pair
    (connected → ``ejectable``) is last because it is the only fact that can change
    between now and the publish, so it is asked as late as it can be.
    """
    if queue_item_id is None:
        return "skipped:foreign", None, None
    if not hold_enabled:
        # INFO, not WARN: an operator who switched the hold off gets the cooldown they
        # asked for (fan only), and a warning would put a chosen state in the channel
        # they triage. The switch exists because red line 2 is waived for this motion —
        # it has to be reachable from the Farm tab in the minute a hold misbehaves.
        logger.info("[cooldown-prep] printer %s: plate hold switched off — fan only", printer_id)
        return "skipped:disabled", None, None

    from backend.app.core.database import async_session
    from backend.app.models.eject_profile import EjectProfile
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.printer import Printer
    from backend.app.services import farm_correlation

    max_z: float | None = None
    try:
        async with async_session() as db:
            item = await db.get(PrintQueueItem, queue_item_id)
            if item is None:
                logger.warning(
                    "[cooldown-prep] printer %s: no queue item %s — plate not held", printer_id, queue_item_id
                )
                return "skipped:item_missing", None, None
            profile = await db.get(EjectProfile, item.eject_profile_id) if item.eject_profile_id is not None else None
            if profile is None:
                logger.warning(
                    "[cooldown-prep] printer %s: unit %s has no eject profile — plate not held",
                    printer_id,
                    queue_item_id,
                )
                return "skipped:profile_missing", None, None

            printer = await db.get(Printer, printer_id)
            # A missing printer row resolves to a missing model, which the geometry
            # resolver already refuses in the one fail-closed place — no second branch.
            try:
                geometry = await get_geometry_required(
                    db, printer.model if printer is not None else None, require_validated=True
                )
            except GeometryUnavailable as exc:
                logger.warning("[cooldown-prep] printer %s: %s — plate not held", printer_id, exc.reason)
                return "skipped:geometry", None, None

            keepout_y = geometry.cooldown_hold_keepout_y_mm
            clear_above = geometry.cooldown_hold_clear_above_mm
            if keepout_y is None or clear_above is None:
                # The fan-only models. INFO, not WARN: this is the state every model
                # but H2S ships in until its clearance is MEASURED, so warning would
                # put a permanent by-design condition in the channel operators triage.
                logger.info(
                    "[cooldown-prep] printer %s: model %s has no cooldown-hold clearance — fan only",
                    printer_id,
                    geometry.model_key,
                )
                return "skipped:no_numbers", None, None
            if is_bedslinger_model(geometry.model_key):
                # On a bedslinger Z moves the TOOLHEAD toward a fixed bed: "raise the
                # plate toward the nozzle plane" is the nozzle descending onto the part.
                logger.info(
                    "[cooldown-prep] printer %s: model %s is a bedslinger — fan only",
                    printer_id,
                    geometry.model_key,
                )
                return "skipped:bedslinger", None, None

            source = await farm_correlation.resolve_item_donor(db, item)
            if source is None:
                logger.warning(
                    "[cooldown-prep] printer %s: no donor file for unit %s — plate not held",
                    printer_id,
                    queue_item_id,
                )
                return "skipped:donor", None, None
            # Same precedence the eject dispatcher uses: the FARM's dispatched plate
            # first, anything parsed out of the file never.
            plate_id = source.plate_id or item.plate_id or 1

            max_z = donor.read_max_z(source.local_path, plate_id)
            if max_z is None:
                logger.warning(
                    "[cooldown-prep] printer %s: plate %s of %s carries no max_z_height — plate not held",
                    printer_id,
                    plate_id,
                    source.filename,
                )
                return "skipped:max_z", None, None

            box = donor.read_plate_bbox(source.local_path, plate_id)
            keepout_skip = _keepout_refusal(box, keepout_y)
            if keepout_skip is not None:
                logger.warning(
                    "[cooldown-prep] printer %s: %s (keepout_y=%s) — plate not held",
                    printer_id,
                    keepout_skip,
                    _mm(keepout_y),
                )
                return "skipped:keepout", None, max_z

            try:
                lines, placement = generator.cooldown_hold_lines(max_z, profile, clear_above, part_top_mm)
            except EjectGenerationError as exc:
                logger.warning("[cooldown-prep] printer %s: %s — plate not held", printer_id, exc)
                return "skipped:over_height", None, max_z

            # Admission: the eject's OWN pair, because the hold moves the same machine
            # under the same hazards (a running job, a Z datum the printer lost).
            if not printer_manager.is_connected(printer_id):
                logger.warning("[cooldown-prep] printer %s: not connected — plate not held", printer_id)
                return "skipped:disconnected", None, max_z
            refusal = plate_occupancy.ejectable(printer_id, eject_remote._live_evidence(printer_id))
            if refusal is not None:
                logger.warning("[cooldown-prep] printer %s: %s — plate not held", printer_id, refusal)
                return "skipped:not_ejectable", None, max_z

            client = printer_manager.get_client(printer_id)
            if client is None:
                logger.warning("[cooldown-prep] printer %s: no MQTT client — plate not held", printer_id)
                return "skipped:publish", None, max_z
            if not client.send_gcode("\n".join(lines)):
                logger.warning("[cooldown-prep] printer %s: hold publish refused — plate not held", printer_id)
                return "skipped:publish", None, max_z

            # The placement the generator EVALUATED for the lines just published — not a
            # second evaluation, so the recorded Z is by construction the commanded one.
            logger.info(
                "[cooldown-prep] printer %s: plate hold sent "
                "(max_z=%s hold_z=%s part_top=%s target=%s bound=%s clear_above=%s keepout_y=%s bbox_y_max=%s)",
                printer_id,
                _mm(max_z),
                _mm(placement.z),
                _mm(placement.part_top_mm),
                part_top_mm,
                placement.bound,
                _mm(clear_above),
                _mm(keepout_y),
                _mm(box[0][3] if box is not None else None),
            )
            return "sent", placement.z, max_z
    except Exception:  # noqa: BLE001 — a hold failure never costs the cooldown its watch
        logger.exception("[cooldown-prep] printer %s: plate hold failed", printer_id)
        return "skipped:error", None, max_z


def _keepout_refusal(box: tuple[tuple[float, float, float, float], int] | None, keepout_y: float) -> str | None:
    """Why this plate may not be held under the chute keep-out, or None to proceed.

    Three refusals, one gate, because they are one question — "do I know that nothing
    on this plate sits under the parked toolhead?":

    * no readable ``bbox_all`` ⇒ nothing is known about where the parts are;
    * anything but exactly ONE filament (0 means UNKNOWN, per ``read_plate_bbox``) ⇒
      the box covers OBJECTS only, and a multi-filament plate puts a purge tower on
      the bed that the box does not describe;
    * ``y_max`` past the keep-out line ⇒ a part stands where the toolhead parks.

    Fail-closed in all three: the alternative to holding is a cooldown that takes
    longer, which is not a hazard.
    """
    if box is None:
        return "plate bbox unreadable"
    (_x_min, _y_min, _x_max, y_max), n_filaments = box
    if n_filaments != 1:
        return f"plate is not single-filament (filaments={n_filaments})"
    if y_max > keepout_y:
        return f"plate bbox y_max={_mm(y_max)} reaches the chute keep-out strip"
    return None


def _start_aux_fan(printer_id: int, percent: int) -> bool:
    """Run the auxiliary fan at ``percent``. Returns whether the command was PUBLISHED.

    Published, not confirmed: MQTT publish acceptance is all the wire offers
    synchronously. Deliberately SYNC — the arm-time witness is read later by
    :meth:`CooldownPrep.observe_start`, so this function has no await for a
    cancellation to land in between the ON and the handle that retires it.
    """
    if percent <= 0:
        return False  # 0 = the operator switched the aux fan off for the fleet
    try:
        if _live_state(printer_id) in ACTIVE_PRINT_STATES:
            logger.warning(
                "[cooldown-prep] printer %s: a job is active — aux fan not commanded",
                printer_id,
            )
            return False
        client = printer_manager.get_client(printer_id)
        if client is None:
            logger.warning("[cooldown-prep] printer %s: no MQTT client — aux fan not commanded", printer_id)
            return False
        if not client.set_fan_percent(_AUX_FAN, percent):
            logger.warning("[cooldown-prep] printer %s: aux fan publish refused", printer_id)
            return False
        return True
    except Exception:  # noqa: BLE001 — a fan failure never costs the cooldown its watch
        logger.exception("[cooldown-prep] printer %s: aux fan start failed", printer_id)
        return False
