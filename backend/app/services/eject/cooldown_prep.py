"""Cooldown prep — the ONE owner of what the farm does to a printer while its
finished plate cools.

Three actuators, all armed by :func:`begin` at the top of the cooldown watch and
retired by :meth:`CooldownPrep.end` when that watch exits:

* the **plate hold** (production / first-article units only) — the bed is raised so the
  part top sits where ``farm_cooldown_hold_part_top_mm`` asks for it relative to the
  nozzle plane, with the toolhead parked at the chute;
* the **auxiliary fan** — ``M106 P2``, forced convection over the part that the hold
  has just brought into its stream; and
* the **chamber exhaust fan** — ``M106 P3``, behind the vendor's own ``M145 P0``
  duct-to-cooling prelude, on the models that have one.

**Why the plate is moved at all.** The aux fan is a fixed duct on the left wall
aimed at the layer being printed: its stream is centred on the nozzle plane (Z 0),
and the stock H2 end block parks the bed by the vendor template
``max_layer_z + 100 - max_layer_z/2`` then ``+98`` (file-verified: a 50.1 mm H2S part
ends at Z123.05). A finished part therefore cools with its top ~73 mm and its plate
~123 mm under the stream — blowing on it is blowing on nothing. The hold brings the
plate TO the stream, which is what makes the fan worth switching on. Baseline the
set is measured against: 84 armed→dispatch pairs 2026-09-05→09-10, median 63.4 min
(p90 81.7) of pure natural convection, ~17 % of every production cycle; then 42
aux-fan-and-hold cooldowns 2026-09-10→09-11, median 44 min.

**Two fans, two mechanisms — and why only one of them steps down.** Newton's law of
cooling is ``dT/dt = -k(T - T_air)``, so the excess decays as ``E0 * e^(-kt)`` and the
time to the eject temperature is ``t = (1/k) * ln(E0/E_eject)`` — LOGARITHMIC in the
excess. Two consequences the fan policy is built on. Every halving of the excess costs
the same minutes at a given fan speed, so the minutes a boost SAVES per boost-minute
are the constant ``k_boost/k_sustain - 1``, identical in the first minute of the wait
and the last. A fan that works by raising ``k`` — the AUX fan, forced convection over
the part — therefore has no step-down point that is better than any other: the shortest
wait is one speed for the whole wait, and a lower speed costs the same throughput
whenever it is applied. That is why there is no aux sustain SETTING and why
``FanRequest.sustain_percent == boost_percent`` on that lane by construction.

The CHAMBER exhaust fan is different in mechanism, not merely in degree. It barely
changes ``k``; it lowers ``T_air`` by exhausting the chamber. With the chamber air at
35-36 °C and the eject line at 33 °C the bed's asymptote sits ABOVE the eject line —
which is the source of the "still above 33 °C after 5400 s" escalations. Once the
chamber air reads at or below the eject threshold the exhaust's UNIQUE work is done,
and holding the air there needs a fraction of full speed (the vendor's own
chamber-cooling figure is 50 %, ``M106 P3 S127``). That is a genuinely front-loaded
reward with a MEASURABLE end, so the chamber lane boosts and then steps down — on the
first bed poll whose chamber reading is ``<= release_threshold_c``, the same threshold
the operator already keeps ~2 °C above shop ambient. No fraction constant, no stored
minute count and no second timer: one comparison per sample of the poll that was
already running (:meth:`CooldownPrep.note_sample`). If the sensor never reads at or
under the threshold the boost simply runs the whole wait, which errs toward cooling.

**MEASURED** (production printer 1, 2026-09-11, 20 s sampler, aux 100 % + plate hold,
chamber fan OFF throughout, eject threshold 33 °C, shop ~30-31 °C): FINISH at bed 61 /
chamber 38; +7 min bed 46 / chamber 35; +16.5 min bed 40 / chamber 33 (the chamber air
reached the eject line by convection alone only then); +23.5 min bed 36 / chamber 32;
+30 min bed 34 / chamber 31; dispatch at bed 33 after 36 min. The first 21 °C of bed
excess took 16.5 min; the LAST 7 °C took 19.5 min while the chamber air crept 33 → 31 —
that tail is what the exhaust is aimed at. During the print itself the firmware's own
``M142`` autocooling ran the chamber fan at ~30 % (wire ``big_fan2_speed`` 27-33) and
the vendor end block switched it off (``M106 P3 S0``) before the cooldown began.
**UNMEASURED**, and stated as such rather than guessed: how many minutes 100 % exhaust
takes to bring the chamber air down to the eject line. Single digits is the
expectation — the air mass is small and the vendor's own post-print exhaust is 180 s at
50 % — but the ``chamber boost ended after N s`` INFO line is the instrument, and the
first production day is the measurement.

**The duct prelude is vendor-verbatim, and load-bearing.** File-verified on the farm's
own H2S and H2C sliced files: the START block's cooling branch is
``M145 P0 ; set airduct mode to cooling`` → ``M106 P2 S178`` → ``M106 P3 S127``, and
the finish tail's ``M622 J2`` branch is ``M145 P0 / M106 P3 S127 / M400 S180 /
M106 P3 S0``. The sibling ``J1`` branch (``M145 P1`` plus the purifier) leaves the duct
in HEATING/recirculation with the top flap closed — so a printer whose finish-filtration
setting took J1 is sitting in heating mode at exactly the moment this module arms, and
spinning the exhaust up without opening the flap would move air around a closed box.
The lane therefore sends :data:`AIRDUCT_COOLING_GCODE` BEFORE the fan and on the SAME
G-code queue, so the flap cannot lose the race, and reports ``skipped:airduct`` if that
half does not land: the pair is the actuator's own two-command contract, and a pair that
half-lands is not-landed. ``generator.COMPLETION_EPILOGUE`` carries the same ``M145 P0``
verbatim.

**Two per-model questions, not one.** "Does this machine have this fan?"
(``CooldownFan.supported``) and "does the vendor pair this fan with a duct prelude
here?" (``CooldownFan.airduct_cooling``) are different facts, and the second is the
narrower: ``AIRDUCT_MODELS`` is a strict SUBSET of ``CHAMBER_FAN_MODELS`` (pinned by a
test in ``printer_manager``). An enclosed machine with a FIXED duct — X1, X1C, X1E,
P1S — has a chamber fan and no flap to open, so its lane spins the fan with no prelude
at all; sending one anyway would be a command firmware swallows, and a swallowed
command is indistinguishable on the wire from a working one. The chamber lane's own
capability gate is
:func:`~backend.app.services.printer_manager.has_chamber_fan`: on an open-frame machine
it refuses rather than pretends. The AUX lane is deliberately UNGATED on both questions
— it never had a prelude, and every model seeded in the geometry registry has an
auxiliary fan, the A-series (the family that does not) not being seeded. An A-series
capability set is the named next step, and it belongs beside the other predicates in
``printer_manager`` rather than here.

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
every other model NULL, which means fans only). Red line 2 (the hardware ladder) was
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
simply starts from farther away, and the drop-span deadline follows the seed. The same
resolve-once rule covers the fans and the printer's MODEL: both arrive as arguments,
and this module opens no settings session of its own.

**Re-entry is safe by construction.** A server restart re-arms the watch, so
``begin`` can run again on a plate that is ALREADY held at ~Z2. That needs no special
case: the hold's FIRST move is the transit to ``park_z``, which lowers an
already-held plate back to the height the vendor's own end block runs ``G150.3`` at,
BEFORE the toolhead is asked to move anywhere. Held, vendor-parked, or wherever a
screen jog left it — every entry passes through the same clear transit height.

Every failure is one log line naming the reason and a cooldown that proceeds without
that actuator. :func:`begin` never raises: losing a fan or the hold costs minutes,
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
from backend.app.services.printer_manager import has_chamber_fan, printer_manager, supports_airduct
from backend.app.utils.printer_models import is_bedslinger_model

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

logger = logging.getLogger(__name__)

FanName = Literal["aux", "chamber"]


@dataclass(frozen=True)
class CooldownFan:
    """One fan this module can run during a cooldown — the machine facts, not the policy.

    Everything here is a property of the HARDWARE and its wire encoding, so the two
    lanes below are the same code parameterised by a frozen value rather than two
    near-identical functions. What speed it runs at and whether it runs at all is
    :class:`FanRequest`, which comes from the operator's settings.
    """

    name: FanName
    index: int  # ``M106 P<index>``: 1 = part cooling, 2 = auxiliary, 3 = chamber
    witness: str  # the ``PrinterState`` field the wire reports it back on
    supported: Callable[[str | None], bool]  # per-MODEL capability: has this fan at all
    # Per-MODEL too, and a DIFFERENT question from ``supported``: does the vendor pair
    # this fan with an ``M145 P0`` duct-to-cooling prelude on this machine? Only where a
    # SWITCHABLE duct exists. ``AIRDUCT_MODELS`` is a strict subset of
    # ``CHAMBER_FAN_MODELS`` (pinned by a test in ``printer_manager``), so an enclosed
    # model with a FIXED duct — X1, X1C, X1E, P1S — runs its chamber fan with no
    # prelude at all, because there is no flap to open and firmware would swallow the
    # command.
    airduct_cooling: Callable[[str | None], bool]
    # Does this lane have a sustain speed of its own to step down to? A property of the
    # FAN (the aux lane has no such setting, by the cooling-law argument in the module
    # docstring), never of the numbers an operator happened to enter — so the summary
    # line's shape per lane is stable and greppable even when a chamber sustain is set
    # equal to its boost.
    steps: bool


# The aux fan is deliberately ungated by model — see the module docstring's "Model
# capability" note for why, and for the A-series set that is the named next step.
AUX_FAN = CooldownFan(
    name="aux",
    index=2,
    witness="big_fan1_speed",
    supported=lambda _model: True,
    airduct_cooling=lambda _model: False,
    steps=False,
)
CHAMBER_FAN = CooldownFan(
    name="chamber",
    index=3,
    witness="big_fan2_speed",
    supported=has_chamber_fan,
    airduct_cooling=supports_airduct,
    steps=True,
)
# Emission order on the wire: the aux fan first (it needs no prelude and is the one
# every model has), then the duct, then the chamber fan.
COOLDOWN_FANS = (AUX_FAN, CHAMBER_FAN)

# Vendor-verbatim, from the start block's cooling branch and the finish tail's J2
# branch alike. ``P0`` is cooling (top flap open); ``P1`` is heating/recirculation.
AIRDUCT_COOLING_GCODE = "M145 P0"

# There is no boost-end CONSTANT in this module, by design. The chamber lane steps
# down the first poll whose chamber reading is at or under the eject threshold — a
# live measurement of this cooldown, never a stored minute count. See the module
# docstring for the cooling-law derivation, and for why the aux lane never steps.


@dataclass(frozen=True)
class FanRequest:
    """What the operator asked of one fan: the switch and the two speeds.

    ``boost_percent`` is 1..100 by the input schema — the on/off decision is
    ``enabled``, the ONE switch, so a stored legacy ``0`` needs no special arm here and
    simply publishes ``S0``. ``sustain_percent`` is 0..100, where 0 means the fan STOPS
    when the boost ends: a step target, not a second switch. On the aux lane
    ``sustain_percent == boost_percent`` by construction (no setting exists), which
    makes its step a numeric no-op (``skipped:same``) — but whether a lane RENDERS a
    step at all is ``CooldownFan.steps``, a property of the fan, so these numbers never
    change the shape of the log line.
    """

    enabled: bool
    boost_percent: int
    sustain_percent: int


@dataclass(frozen=True)
class CooldownFanSettings:
    """Both lanes' requests as ONE value, composed once by the watch that resolves them."""

    aux: FanRequest
    chamber: FanRequest

    def for_fan(self, fan: CooldownFan) -> FanRequest:
        return self.aux if fan.name == "aux" else self.chamber


# What arming one fan did. Every value but ``sent`` is a cooldown that ran without
# that lane — never an error the caller has to handle.
FanStartOutcome = Literal[
    "sent",
    "skipped:disabled",  # the operator's switch is off — INFO, a deliberate state
    "skipped:unsupported",  # this model has no such fan (firmware would swallow it)
    "skipped:active",
    "skipped:no_client",
    "skipped:airduct",  # the duct-to-cooling half of the pair did not land
    "skipped:publish",
    "skipped:error",
]

# What the boost→sustain step did, when the chamber reached the eject threshold.
FanStepOutcome = Literal[
    "sent",
    "skipped:same",  # sustain == boost: the aux lane, which has no step by construction
    "skipped:not_published",  # this lane never started, so there is nothing to step
    "skipped:retired",  # this prep has been retired — no actuator after retirement
    "skipped:operator_off",  # a human switched this fan off; a step would re-assert it
    "skipped:active",
    "skipped:no_client",
    "skipped:publish",
]

# What the end-of-cooldown fan OFF did. ``skipped:active`` is the normal release-path
# value: by the time the watch exits, the eject job it dispatched is usually already
# running — and that job's own prologue carries redundant ``M106 P2 S0`` / ``M106 P3 S0``
# lines. ``skipped:already_off`` is the lane whose sustain was 0: the step stopped it.
FanOffOutcome = Literal[
    "sent",
    "skipped:not_wanted",
    "skipped:not_published",
    "skipped:already_off",
    "skipped:active",
    "skipped:no_client",
    "skipped:publish",
]


@dataclass
class FanLane:
    """One fan's whole story for one cooldown: what was asked, and what each step did.

    ``start`` is set by :func:`begin`; ``step`` only if the chamber reached the eject
    threshold while this prep was live; ``off`` by :meth:`CooldownPrep.end`. Both
    later fields stay None when their moment never came, and the summary line renders
    that as ``none`` rather than inventing an outcome.

    ``witnessed_on`` / ``operator_off`` are the OPERATOR's half of this lane's story,
    decided from the wire witness in :meth:`CooldownPrep.note_sample`: the wire is the
    only origin that also sees the touchscreen, so it is the only way to notice that a
    human switched a fan the farm published back off (a touchscreen tap, or
    ``POST /printers/{id}/fan-speed``). Zero is the ONE unambiguous witness value — the
    wire reports a quantised 0-15 level, so "lowered but still running" is NOT detected
    and is not claimed to be — and the detector only ever arms after the lane was
    witnessed RUNNING, so a model that never reports the field (or the virtual printer,
    which reports ``0`` unconditionally) decides nothing.
    """

    fan: CooldownFan
    request: FanRequest
    start: FanStartOutcome
    step: FanStepOutcome | None = None
    off: FanOffOutcome | None = None
    # Has the wire ever reported this lane's fan actually turning after we published it?
    witnessed_on: bool = False
    # …and then reported it at exactly 0 without this lane having commanded that 0.
    operator_off: bool = False

    @property
    def published(self) -> bool:
        """Did this lane actually command its fan ON? The only licence to command it OFF."""
        return self.start == "sent"


# What the plate hold did. Every value but ``sent`` is a cooldown that ran with the
# plate where the end block left it — never an error the caller has to handle.
HoldOutcome = Literal[
    "sent",
    "skipped:foreign",  # queue_item_id is None — the foreign auto-eject watch never holds
    "skipped:disabled",  # farm_cooldown_hold_enabled is off — INFO, a deliberate operator state
    "skipped:service_hold",  # maintenance mode: the hold MOVES the machine, hands may be in it
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


def _mm(value: float | None) -> str:
    """A millimetre figure for the log line, or ``none``."""
    return "none" if value is None else f"{value:.2f}"


def _c(value: float | None) -> str:
    """A temperature figure for the log line, or ``none``."""
    return "none" if value is None else f"{value:.1f}"


def _speed(value: int | None) -> str:
    """An observed fan speed for the log line, or ``none`` when the wire said nothing."""
    return "none" if value is None else str(value)


def _reading(temperatures: Mapping[str, float | None], key: str) -> float | None:
    """One numeric temperature out of the live map, or None when it is not a number.

    ``PrinterState.temperatures`` is a loose dict that also carries flags
    (``chamber_heating``) and bookkeeping stamps, and a model with no chamber sensor
    simply has no ``chamber`` key. None means "this cooldown has no such reading", which
    every caller here treats as "decide nothing".
    """
    value = temperatures.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _live_state(printer_id: int) -> str | None:
    """The printer's live ``gcode_state``, or None when nothing is readable.

    None is NOT active: an unreadable printer is one whose job cannot be observed to
    own it, and every actuator here is safe on an idle machine. The active-state set
    is the plate-occupancy authority's own (:data:`ACTIVE_PRINT_STATES`) — the one
    place that defines "a job owns this printer".
    """
    state = printer_manager.get_status(printer_id)
    return getattr(state, "state", None) if state is not None else None


def _observed_fan(printer_id: int, fan: CooldownFan) -> int | None:
    """The live speed the wire reports for ``fan``, or None when unreadable.

    A WITNESS, never a confirmation. The wire reports a 0-15 LEVEL that
    ``parse_fan_speed`` rescales to percent, so an observation is quantised to
    multiples of ~6.67 and need not equal the percent that was commanded. Nothing in
    this module (or its tests) may assert equality between the two.

    ``airduct_mode`` is deliberately NOT logged beside it as a witness of the chamber
    lane's prelude: its default 0 is "cooling" AND "nothing reported", so it cannot
    distinguish a flap that opened from a printer that never mentioned one. The aux
    witness is honest but coarse; that one would be honest-looking and empty.
    """
    state = printer_manager.get_status(printer_id)
    return getattr(state, fan.witness, None) if state is not None else None


def _fan_summary(lane: FanLane, observed: int | None) -> str:
    """One lane's segment of the summary line. The ONE renderer, so the two cannot drift.

    The shape is decided by the FAN (``CooldownFan.steps``), never by the numbers an
    operator happened to enter. A non-stepping lane — the aux fan, which has no sustain
    setting at all — renders one speed and no ``step=`` field. A stepping lane always
    renders ``boost%→sustain%`` and its step outcome, even when the two speeds are
    equal: an operator who sets the chamber sustain to its boost still gets
    ``chamber=100%→100% … step=skipped:same``, because a per-lane grep shape that
    changed with a setting would make the measurement unreadable exactly when someone
    had been experimenting with it.
    """
    request = lane.request
    if lane.fan.steps:
        speeds = f"{request.boost_percent}%→{request.sustain_percent}%"
        step = f" step={lane.step if lane.step is not None else 'none'}"
    else:
        speeds = f"{request.boost_percent}%"
        step = ""
    return (
        f"{lane.fan.name}={speeds} start={lane.start}{step} "
        f"off={lane.off if lane.off is not None else 'none'} observed={_speed(observed)}"
    )


@dataclass
class CooldownPrep:
    """What :func:`begin` actually managed to arm, and how to retire it.

    Deliberately a plain value the caller holds for the life of its watch: there is
    no holder token, no module-level registry and no reconcile loop, because the ONE
    thing that knows whether a printer is still cooling is the watch task itself. If
    this process dies mid-cooldown the fans keep running and the plate stays held —
    all three are recovered by the next watch's ``begin`` (re-entrant by construction)
    or by the eject block's own ``M106 P2 S0`` / ``M106 P3 S0``, not by anything
    remembered here.

    **RETIRED is a class invariant: no actuator is driven after :meth:`end`.** The prep's
    lifetime is the COOLING EPISODE, which can now end BEFORE the watch does — a service
    hold withholds the eject, so the watch retires the actuators at the cooldown's end and
    goes on polling. Without the invariant the sampler would re-start the chamber fan at
    its sustain speed on the first chamber-under-threshold sample AFTER that retirement
    (the bed reaches the eject line before the chamber does on the measured trace), which
    is the fans-run-for-hours shape again from the other direction.
    """

    printer_id: int
    hold: HoldOutcome
    # The Z the plate is being HELD at — set only when the hold was actually sent, so
    # a caller can seed the eject estimator with a measurement instead of a bound.
    hold_z: float | None
    # The part height read from the donor, whenever it got that far (diagnostic: it is
    # what distinguishes an over-height refusal from a plate nobody could measure).
    max_z: float | None
    # One per fan, in :data:`COOLDOWN_FANS` order.
    fans: tuple[FanLane, ...]
    # The eject threshold this cooldown is waiting for — the chamber lane's step-down
    # line, resolved once by the watch and handed in with everything else.
    release_threshold_c: float
    started_at: float  # time.monotonic()
    # The first sample's readings, diagnostic: they are what makes a boost that ended
    # "after 0 s" readable as a late re-arm rather than a broken comparison.
    chamber_at_arm_c: float | None = None
    bed_at_arm_c: float | None = None
    sampled: bool = False
    # Has this cooling episode ended? Set by :meth:`end`, which is the ONE retirement
    # path; every other entry point refuses on a retired prep (the invariant above).
    retired: bool = False
    # When the chamber first read at or under the threshold (monotonic), or None for a
    # cooldown whose chamber never got there. THE measurement this wave exists to take.
    boost_ended_at: float | None = None
    # The arm-time witness's wait, carried here (not awaited inside :func:`begin`) so
    # that NO await sits between "fans commanded ON" and the caller holding this handle:
    # a watch cancelled during the settle then still reaches its ``finally`` and
    # :meth:`end`, which is what decides whether the fans go back off. Injected for
    # tests only; production waits on the event loop's own sleep.
    settle_s: float = 3.0
    sleep: Callable[[float], Awaitable[None]] = field(default=asyncio.sleep, repr=False)

    def lane(self, name: FanName) -> FanLane:
        """The lane for one fan by name. Raises for an unknown name — there are two."""
        for lane in self.fans:
            if lane.fan.name == name:
                return lane
        raise KeyError(name)

    async def observe_start(self) -> None:
        """Log the ONE arm-time witness of the fan commands, after the wire settles.

        The only evidence that a published fan command reached the hardware at all,
        sampled after ``settle_s`` because a read taken before the next status push
        would witness the OLD value. Logged and never asserted on — see
        :func:`_observed_fan` for why the numbers legitimately differ. A no-op when
        NEITHER lane published. Cancellation propagates (the caller's ``finally`` owns
        the retirement); every other failure is swallowed.
        """
        if not any(lane.published for lane in self.fans):
            return
        await self.sleep(self.settle_s)
        try:
            witnesses = " ".join(
                f"{lane.fan.name}={_speed(_observed_fan(self.printer_id, lane.fan))}" for lane in self.fans
            )
            logger.info("[cooldown-prep] printer %s: fan_observed_at_start %s", self.printer_id, witnesses)
        except Exception:  # noqa: BLE001 — a witness read never costs the cooldown its watch
            logger.exception("[cooldown-prep] printer %s: fan witness read failed", self.printer_id)

    def note_sample(self, temperatures: Mapping[str, float | None]) -> None:
        """Feed one bed-poll sample in. Sync, and never raises.

        The ONE consumer of the chamber temperature, and the ONE place the boost ends:
        the first sample reading at or under ``release_threshold_c`` stamps
        :attr:`boost_ended_at`, logs THE measurement line, and steps every lane to its
        sustain speed. Called from the poll that was already running, so there is no
        second timer and no cadence of its own.

        Three honest edge cases, all fail-open toward more cooling: a chamber already
        under the threshold at arm (a restart re-arming late in a cooldown) steps on
        the first sample and says so with ``after 0 s``; a chamber that never reads
        under it never steps, and the summary says ``never``; a model with no chamber
        reading at all is the same case as the second.

        Also the ONE place the operator's own hand is noticed (:meth:`_note_fan_witnesses`),
        read from the same poll and BEFORE the boost logic, because "a human switched this
        fan off" changes what the step below is allowed to do. A RETIRED prep decides
        nothing at all: the cooling episode is over, and a sample arriving afterwards
        (this watch is still polling, because a service hold withheld its eject) must not
        bring a fan back.
        """
        if self.retired:
            return
        try:
            self._note_fan_witnesses()
        except Exception:  # noqa: BLE001 — a witness read never costs the step its decision
            logger.exception("[cooldown-prep] printer %s: fan witness read failed", self.printer_id)
        try:
            chamber = _reading(temperatures, "chamber")
            if not self.sampled:
                self.sampled = True
                self.chamber_at_arm_c = chamber
                self.bed_at_arm_c = _reading(temperatures, "bed")
            if self.boost_ended_at is not None:
                return  # stamped once, by the FIRST qualifying sample
            if chamber is None or chamber > self.release_threshold_c:
                return
            self.boost_ended_at = time.monotonic()
            logger.info(
                "[cooldown-prep] printer %s: chamber boost ended after %.0f s "
                "(chamber=%s chamber_at_arm=%s bed=%s threshold=%s)",
                self.printer_id,
                self.boost_ended_at - self.started_at,
                _c(chamber),
                _c(self.chamber_at_arm_c),
                _c(_reading(temperatures, "bed")),
                _c(self.release_threshold_c),
            )
            for lane in self.fans:
                lane.step = self._step_fan(lane)
        except Exception:  # noqa: BLE001 — a sampler must never kill the watch that feeds it
            logger.exception("[cooldown-prep] printer %s: cooldown sample failed", self.printer_id)

    def _note_fan_witnesses(self) -> None:
        """Read both lanes' wire witnesses and decide whether a HUMAN turned one off.

        Two facts per lane, in order, because the second is only meaningful after the
        first: ``witnessed_on`` (the wire has reported this fan actually running since we
        published it) and then ``operator_off`` (it now reports exactly 0, and this lane
        did not command that 0). Only a lane the farm PUBLISHED is watched — a fan an
        operator started themselves was never ours to decide about.

        Decided here rather than at the step, and independently of ``boost_ended_at``, so
        the fact is recorded when it happens rather than at whatever later moment the
        chamber air happens to cross the line. Our OWN zero — a step to a sustain of 0 —
        is excluded by construction; anything else at 0 after running is a hand on the
        machine, and the farm stands aside for it (one INFO line, once per lane).
        """
        for lane in self.fans:
            if not lane.published:
                continue
            observed = _observed_fan(self.printer_id, lane.fan)
            if observed is None:
                continue  # nothing reported: a witness that says nothing decides nothing
            if observed > 0:
                lane.witnessed_on = True
                continue
            if not lane.witnessed_on or lane.operator_off:
                continue
            if lane.step == "sent" and lane.request.sustain_percent == 0:
                continue  # that zero is OURS — the boost-end step stopped this lane
            lane.operator_off = True
            logger.info(
                "[cooldown-prep] printer %s: %s fan observed off — released to the operator, not re-commanded",
                self.printer_id,
                lane.fan.name,
            )

    def _step_fan(self, lane: FanLane) -> FanStepOutcome:
        """Take one lane from its boost speed to its sustain speed, or say why not.

        No duct write: the farm owns the duct mode for the whole cooldown and it is
        already in cooling — the prelude is an ON-time precondition, not a per-command
        one. A sustain of 0 publishes ``S0`` and leaves the lane off from here, which
        :meth:`_switch_fan_off` then reports as ``skipped:already_off``.

        Two refusals that are about OWNERSHIP rather than transport: a retired prep drives
        no actuator at all (the class invariant), and a lane a human has switched off is
        theirs — a step is a RE-ASSERTION of a speed, and re-asserting one over an
        operator's off would be the farm arguing with the person at the machine.
        """
        if self.retired:
            return "skipped:retired"
        if lane.request.sustain_percent == lane.request.boost_percent:
            return "skipped:same"
        if not lane.published:
            # Never commanded this fan ON, so stepping it would be this module driving
            # a fan it does not own.
            return "skipped:not_published"
        if lane.operator_off:
            return "skipped:operator_off"
        if _live_state(self.printer_id) in ACTIVE_PRINT_STATES:
            logger.warning(
                "[cooldown-prep] printer %s: a job is active — %s fan not stepped to %s%%",
                self.printer_id,
                lane.fan.name,
                lane.request.sustain_percent,
            )
            return "skipped:active"
        client = printer_manager.get_client(self.printer_id)
        if client is None:
            logger.warning(
                "[cooldown-prep] printer %s: no MQTT client — %s fan not stepped", self.printer_id, lane.fan.name
            )
            return "skipped:no_client"
        if not client.set_fan_percent(lane.fan.index, lane.request.sustain_percent):
            logger.warning("[cooldown-prep] printer %s: %s fan step publish refused", self.printer_id, lane.fan.name)
            return "skipped:publish"
        return "sent"

    def end(self, *, fan_off: bool) -> None:
        """Retire the prep and log the ONE summary line. Sync, and never raises.

        ``fan_off`` is the caller's answer to "does this printer still want its
        cooldown fans" — the monitor asks whether a successor cooldown-class watch is
        armed, so a watch that was cancelled to make way for another printer's-plate
        policy hands the fans over instead of switching them off under its successor.
        It is ONE answer for both lanes because it is a question about the PRINTER.

        Each lane is retired inside its own guard, so a throw on one can never skip the
        other's OFF — the two fans are independent hardware and a fan left running under
        an idle printer is a state nothing else in the farm clears.

        The plate is deliberately NOT lowered here. It stays held exactly where the
        hold left it and the eject block's own first Z move takes it from there in one
        flow; on any other exit (an operator clearing the gate, a stall) the plate is
        parked at a safe height with the steppers released, which is the same state
        the stock end block leaves behind.

        **Called twice on a deferred cooldown, and the two calls are different acts.** A
        service hold retires the actuators when the COOLING ends (the watch goes on
        polling), and the watch's exit ``finally`` calls this again later. The
        MEASUREMENT is idempotent — one summary line per cooling episode, from the first
        call — but the fans-off is not: :meth:`_retry_fan_off` re-attempts any lane whose
        OFF did not actually land, because a ``skipped:active`` / ``skipped:no_client`` /
        ``skipped:publish`` is not a retirement, and a deferral has no eject job whose
        prologue would carry the durable ``M106 P2 S0``.
        """
        if self.retired:
            self._retry_fan_off(fan_off)
            return
        self.retired = True
        segments: list[str] = []
        for lane in self.fans:
            try:
                lane.off = self._switch_fan_off(lane, fan_off)
            except Exception:  # noqa: BLE001 — the other lane, and the summary, must still run
                logger.exception("[cooldown-prep] printer %s: %s fan off failed", self.printer_id, lane.fan.name)
                # The publish is the only thing that can throw here, and it did not land.
                lane.off = "skipped:publish"
            try:
                observed = _observed_fan(self.printer_id, lane.fan)
            except Exception:  # noqa: BLE001 — an unreadable witness is not a failure
                observed = None
            segments.append(_fan_summary(lane, observed))
        boosted = "never" if self.boost_ended_at is None else f"{self.boost_ended_at - self.started_at:.0f} s"
        # THE line the wave is measured by: one per cooldown, greppable as
        # ``[cooldown-prep]``, carrying every decision this module made.
        logger.info(
            "[cooldown-prep] printer %s: cooldown ended after %.0f s "
            "(hold=%s max_z=%s hold_z=%s, chamber_boost_ended_after=%s chamber_at_arm=%s bed_at_arm=%s, %s)",
            self.printer_id,
            time.monotonic() - self.started_at,
            self.hold,
            _mm(self.max_z),
            _mm(self.hold_z),
            boosted,
            _c(self.chamber_at_arm_c),
            _c(self.bed_at_arm_c),
            ", ".join(segments),
        )

    def _retry_fan_off(self, fan_off: bool) -> None:
        """Re-attempt the fans-off for a lane whose retirement never landed. Never raises.

        Reached only from a SECOND :meth:`end` on an already-retired prep — the deferred
        cooldown's shape, where the cooling ended under a service hold and the watch
        exited later. A lane is re-attempted when it was published and its recorded ``off``
        is neither ``sent`` nor ``skipped:already_off``: those two are the only outcomes
        that mean the fan is actually stopped. ``skipped:not_wanted`` is included on
        purpose — the first call may have handed the fans to a successor that has since
        gone — and is filtered by ``fan_off`` here instead.

        Deliberately NOT guarded by :attr:`retired`: this IS the retirement path, and the
        invariant it enforces is "no actuator is driven after the episode", never "the OFF
        is published at most once". No second summary line — the measurement was taken by
        the first call, and a retry that lands says so in one line of its own.
        """
        if not fan_off:
            return
        for lane in self.fans:
            if not lane.published or lane.off in ("sent", "skipped:already_off"):
                continue
            first_attempt = lane.off
            try:
                lane.off = self._switch_fan_off(lane, True)
            except Exception:  # noqa: BLE001 — the other lane must still be re-attempted
                logger.exception("[cooldown-prep] printer %s: %s fan off retry failed", self.printer_id, lane.fan.name)
                lane.off = "skipped:publish"
            if lane.off == "sent":
                logger.info(
                    "[cooldown-prep] printer %s: %s fan OFF landed on the retirement retry (first attempt %s)",
                    self.printer_id,
                    lane.fan.name,
                    first_attempt,
                )

    def _switch_fan_off(self, lane: FanLane, fan_off: bool) -> FanOffOutcome:
        """Switch one fan off, or say why not. The only I/O :meth:`end` does.

        Deliberately NOT suppressed by ``lane.operator_off``: the end-of-cooldown OFF
        retires a fan the FARM published, and an operator who switched it back on gets it
        switched off with the cooldown rather than left running on an idle printer. Only
        the STEP (a re-assertion of a speed mid-cooldown) stands aside for a human.
        """
        if not fan_off:
            return "skipped:not_wanted"
        if not lane.published:
            # Never turned it on — so turning it off would be this module commanding a
            # fan it does not own (an operator's manual /fan-speed, say).
            return "skipped:not_published"
        if lane.step == "sent" and lane.request.sustain_percent == 0:
            # The boost-end step already stopped this fan. Publishing S0 again would be
            # a second command with no effect to report.
            return "skipped:already_off"
        if _live_state(self.printer_id) in ACTIVE_PRINT_STATES:
            # The eject sweep this cooldown released into is already running. Its own
            # prologue carries the fan OFF; commanding one now would race that job.
            return "skipped:active"
        client = printer_manager.get_client(self.printer_id)
        if client is None:
            return "skipped:no_client"
        return "sent" if client.set_fan_percent(lane.fan.index, 0) else "skipped:publish"


async def begin(
    printer_id: int,
    *,
    queue_item_id: int | None,
    fans: CooldownFanSettings,
    release_threshold_c: float,
    model: str | None,
    hold_enabled: bool,
    hold_part_top_mm: int,
    held: bool = False,
    settle_s: float = 3.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> CooldownPrep:
    """Arm the cooldown actuators for ``printer_id``. NEVER raises.

    Order is hold first, then the fans: the hold is the one that moves the machine, and
    it wants the printer in exactly the idle state the end block left — the fans change
    nothing about that, but doing them first would put publishes between the terminal
    and the motion for no reason.

    ``queue_item_id`` is None for the foreign auto-eject watch, whose plate carries no
    farm unit: no unit means no donor, no profile and no measured part height, so
    there is nothing to hold the plate SAFELY at and the foreign lane gets the fans
    alone. ``fans``, ``release_threshold_c``, ``model``, ``hold_enabled`` and
    ``hold_part_top_mm`` are all resolved ONCE by the watch that arms this prep and
    never re-read here — this module opens no settings session and asks the manager for
    no model. ``settle_s``/``sleep`` are injected only so tests need not wait.

    ``held`` is the printer's service hold, read once by the watch that arms this prep: it
    refuses the plate HOLD and nothing else. Motion is an action — the hold drives the bed
    and the toolhead (``G150.3``) and a human's hands may be inside the machine — while the
    fans are air, and a held printer's plate has to cool like any other. The cost is stated
    rather than hidden: a cooldown that arms under a hold is FAN-ONLY and therefore slower
    (the aux stream is centred on the nozzle plane, ~73 mm above a vendor-parked part), and
    it is NOT re-attempted when the hold lifts — the eject's first Z move handles either
    plate position, and an unseeded estimator over-states the drop span, which is the safe
    direction.
    """
    started_at = time.monotonic()
    hold, hold_z, max_z = await _hold_plate(
        printer_id, queue_item_id, hold_enabled=hold_enabled, part_top_mm=hold_part_top_mm, held=held
    )
    # The fan publishes are the LAST thing before the handle is returned — nothing is
    # awaited after them (the witness wait lives in ``CooldownPrep.observe_start``), so
    # a cancellation can never separate an ON that was sent from the ``end`` that
    # decides whether it stays on.
    lanes = tuple(_start_fans(printer_id, fans, model))
    return CooldownPrep(
        printer_id=printer_id,
        hold=hold,
        hold_z=hold_z,
        max_z=max_z,
        fans=lanes,
        release_threshold_c=release_threshold_c,
        started_at=started_at,
        settle_s=settle_s,
        sleep=sleep,
    )


def _start_fans(printer_id: int, fans: CooldownFanSettings, model: str | None) -> list[FanLane]:
    """Arm every lane, in :data:`COOLDOWN_FANS` order. Sync, and never raises."""
    lanes: list[FanLane] = []
    for fan in COOLDOWN_FANS:
        request = fans.for_fan(fan)
        lanes.append(FanLane(fan=fan, request=request, start=_start_fan(printer_id, fan, request, model)))
    return lanes


def _start_fan(printer_id: int, fan: CooldownFan, request: FanRequest, model: str | None) -> FanStartOutcome:
    """Run one fan at its boost speed. Returns what happened, and never raises.

    Published, not confirmed: MQTT publish acceptance is all the wire offers
    synchronously. Deliberately SYNC — the arm-time witness is read later by
    :meth:`CooldownPrep.observe_start`, so this function has no await for a
    cancellation to land in between the ON and the handle that retires it.

    Admission is this FAN's own predicate — an idle printer and a live client — and
    NEVER the hold's ``ejectable()``: a printer that may not be swept (a lost Z
    reference, say) is still a printer whose plate has to cool, and suppressing the
    fans there would lengthen exactly the wait a human is already standing over.
    """
    if not request.enabled:
        logger.info("[cooldown-prep] printer %s: %s fan switched off — not commanded", printer_id, fan.name)
        return "skipped:disabled"
    try:
        if not fan.supported(model):
            # INFO, not WARN: a model without this fan is a permanent by-design state,
            # and firmware would silently swallow the command rather than report it.
            logger.info(
                "[cooldown-prep] printer %s: model %s has no %s fan — not commanded",
                printer_id,
                model or "unknown",
                fan.name,
            )
            return "skipped:unsupported"
        if _live_state(printer_id) in ACTIVE_PRINT_STATES:
            logger.warning("[cooldown-prep] printer %s: a job is active — %s fan not commanded", printer_id, fan.name)
            return "skipped:active"
        client = printer_manager.get_client(printer_id)
        if client is None:
            logger.warning("[cooldown-prep] printer %s: no MQTT client — %s fan not commanded", printer_id, fan.name)
            return "skipped:no_client"
        if fan.airduct_cooling(model) and not client.send_gcode(AIRDUCT_COOLING_GCODE):
            # The vendor's pair is this actuator's two-command contract: spinning the
            # exhaust up behind a closed flap moves air around a closed box, so a pair
            # that half-lands reports not-landed and the fan is never published.
            logger.warning(
                "[cooldown-prep] printer %s: airduct-to-cooling publish refused — %s fan not commanded",
                printer_id,
                fan.name,
            )
            return "skipped:airduct"
        if not client.set_fan_percent(fan.index, request.boost_percent):
            logger.warning("[cooldown-prep] printer %s: %s fan publish refused", printer_id, fan.name)
            return "skipped:publish"
        return "sent"
    except Exception:  # noqa: BLE001 — a fan failure never costs the cooldown its watch
        logger.exception("[cooldown-prep] printer %s: %s fan start failed", printer_id, fan.name)
        return "skipped:error"


async def _hold_plate(
    printer_id: int, queue_item_id: int | None, *, hold_enabled: bool, part_top_mm: int, held: bool = False
) -> tuple[HoldOutcome, float | None, float | None]:
    """Send the plate hold, or return the reason it was skipped.

    Returns ``(outcome, hold_z, max_z)``. ``hold_z`` is non-None ONLY on ``sent`` —
    it is what the eject estimator is seeded from, and seeding it from a hold that
    did not happen would tell the runtime watchdog the bed is 120 mm closer to the
    nozzle than it is. ``max_z`` is returned whenever it was read, sent or not.

    The gate order is cheapest-and-most-permanent first: the three refusals that need no
    state at all come before the session is even opened, a model with no registry
    numbers can never hold so it is not worth opening a 3MF for, and the admission pair
    (connected → ``ejectable``) is last because it is the only fact that can change
    between now and the publish, so it is asked as late as it can be.

    **A donor that cannot supply this unit's plate ends the lane fan-only** — the plate
    is not raised at all — rather than holding at some other plate's height. It shows up
    as ``skipped:donor`` (the shared resolver refused the donor outright) or
    ``skipped:max_z`` (the plate is in the container but its header carries no height).
    Until 2026-09-17 the chain here read ``source.plate_id or item.plate_id or 1`` and
    the plate reader fell back to the first G-code member, so a stranger donor produced
    a height and the bed rose to it (005-H2S).
    """
    if queue_item_id is None:
        return "skipped:foreign", None, None
    if not hold_enabled:
        # INFO, not WARN: an operator who switched the hold off gets the cooldown they
        # asked for (fans only), and a warning would put a chosen state in the channel
        # they triage. The switch exists because red line 2 is waived for this motion —
        # it has to be reachable from the Farm tab in the minute a hold misbehaves.
        logger.info("[cooldown-prep] printer %s: plate hold switched off — fans only", printer_id)
        return "skipped:disabled", None, None
    if held:
        # Maintenance mode. The plate is cooled by the fans alone: this is the one
        # actuator here that MOVES the machine (the bed, and the toolhead's ``G150.3``),
        # and the operator whose hands may be inside it is exactly who the hold exists
        # for. INFO, not WARN — a declared state, like the switch above. Asked before any
        # session is opened, because no amount of state could change the answer.
        logger.info("[cooldown-prep] printer %s: printer in maintenance mode — fans only, plate not held", printer_id)
        return "skipped:service_hold", None, None

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
                    "[cooldown-prep] printer %s: model %s has no cooldown-hold clearance — fans only",
                    printer_id,
                    geometry.model_key,
                )
                return "skipped:no_numbers", None, None
            if is_bedslinger_model(geometry.model_key):
                # On a bedslinger Z moves the TOOLHEAD toward a fixed bed: "raise the
                # plate toward the nozzle plane" is the nozzle descending onto the part.
                logger.info(
                    "[cooldown-prep] printer %s: model %s is a bedslinger — fans only",
                    printer_id,
                    geometry.model_key,
                )
                return "skipped:bedslinger", None, None

            source = await farm_correlation.resolve_item_donor(db, item)
            if source is None:
                logger.warning(
                    "[cooldown-prep] printer %s: no donor file for unit %s (or it does not carry that unit's "
                    "plate) — plate not held",
                    printer_id,
                    queue_item_id,
                )
                return "skipped:donor", None, None
            # The ONE resolver's plate — already validated against the donor's own
            # G-code members. There is no second read of the queue row here: the hold
            # raises the plate to a HEIGHT read off this plate's header, so a plate the
            # donor does not carry must end the lane, never be replaced by plate 1.
            plate_id = source.plate_id

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
