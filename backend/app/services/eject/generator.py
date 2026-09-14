"""Eject G-code generator.

Produces the machine-end EJECT BLOCK for a given :class:`EjectProfile`, part
height and printer model. The block runs *after* the printer's stock shutdown (bed
dropped ~Z123, motors M18-disabled) OR after the server-side cooldown HOLD
(:func:`cooldown_hold_lines`) has parked the plate near the nozzle plane with the
toolhead off the bed, so it re-engages the motors, commands the bed heater off, and
then makes ONE Z move from wherever the plate happens to be — always to the LIFT
height, the height the sweep itself runs from — before homing X/Y and sweeping the
part off the front (door side). The bed-drop release assist, when on, is a round trip
DOWN from that height and back to it, and it runs AFTER the home.

The X/Y home therefore sits AFTER the block's first Z move and BEFORE the drop. Two
independent reasons, both load-bearing:

* CLEARANCE. At the lift height the part top sits exactly ``clearance_mm`` under the
  nozzle plane — the vendor's own precondition for crossing a loaded plate (the stock
  end block runs its ``G150.3`` at ``max_layer_z + 10``, and the fleet's
  ``clearance_mm`` IS that 10; see :func:`cooldown_hold_lines` and
  ``h2s-gcode-dialect.md``), and the same gap this block's own sweep-entry transit and
  park traverse already accept on every production eject. Homing FIRST, before any Z
  move, would run the home beside a plate the cooldown hold left at ~2 mm with the part
  standing ~48 mm ABOVE the nozzle plane: a NEGATIVE clearance, a guaranteed strike.
  Homing direction does not rescue that case — operator eyewitness 2026-09-13
  established that Y homes rearward AND that the hotend crosses the rear of the plate
  while homing, so a rearward home is not self-clearing. Homing safety is Z-clearance
  and nothing else.
* FRAME INTEGRITY. The bed-drop is the ONLY move in the block that drives toward a
  possible obstruction (debris under the plate). A stall there loses steps in the
  dangerous direction — the plate ends HIGHER than the firmware believes — and every
  absolute Z after it is read in that corrupted frame. That is the 012-H2S 2026-07-31
  gouged-plate incident, recorded verbatim beside
  :data:`~backend.app.services.eject.remote.EJECT_ABORT_MARGIN_FRAC`. ``G28 X`` /
  ``G28 Y`` are sensorless-stall moves, so a home run in that corrupted frame can read
  contact with the part as its endstop and corrupt X/Y on top of Z, after which the
  sweep runs in a frame the envelope guard never validated. Homing BEFORE the drop
  keeps the home in the frame the print left — the only frame this block ever has
  evidence for.

The block ENDS with the bed parked proportional to part height — the toolhead
sits at ``max(max_z_height + clearance_mm, PARK_Z_MM)`` above the plate (bed
dropped clear FIRST, then a straight traverse to centre), so a part that
survived the sweep stays clear of the nozzle instead of being dragged into.

Every coordinate is derived from the profile plus the model's bed dimensions,
then clamped into the model's proven-safe machine travel envelope so no
generated move can trip the firmware soft limits — nothing is hardcoded. The bed
rectangle and the envelope both arrive as a :class:`ModelGeometry` resolved from
the ``printer_model_geometry`` registry (``services.eject.geometry``), so adding
a printer model is a DB row, not a code change.

The module also owns :func:`estimate_runtime_segments` (and its total-only façade
:func:`estimate_runtime_s`), the EXPECTED execution time of a generated block. There
is no Z telemetry in the MQTT feed, so an eject whose bed-drop stalls against an
obstruction (lost steps, bed returning too high) still reports ``completed`` — job
RUNTIME is the only observable signature of that failure. The estimate is what the
in-flight runtime watchdog turns into an abort deadline
(``eject.remote.eject_abort_deadline_s``), so an eject still executing at that
deadline is STOPPED mid-job instead of judged after the fact.

A whole-job deadline can only catch a stall long enough to push the WHOLE eject past
its margin (~59 s on the production profile), so the block also emits M73 PHASE
BEACONS (:data:`PHASE_BEACON_LIFTED` / :data:`PHASE_BEACON_SWEEP` /
:data:`PHASE_BEACON_PARK`). ``mc_percent`` is entirely M73-driven and resets to 0 at
job start, so those three commands are the ONLY in-band phase signal the wire carries
(``mc_print_line_number`` is absent on the H2S). The watchdog times the EDGES between
them against the per-phase spans of :class:`EjectRuntimeSegments`, so each phase is
bounded on its own: the sweep is provably unreached while ``mc_percent`` sits below the
sweep beacon, and provably OVER once it reaches the park beacon.

Two optional tunings narrow the sweep: an X sub-band (``sweep_x_min_mm`` /
``sweep_x_max_mm``) confines the lanes to part of the bed width instead of the
full width, and ``sweep_start_frac`` starts the descending sweep at a fraction
of the part height instead of at the part top. The prologue clearance move
still clears the full part top regardless of either tuning. A third tuning,
``final_skim`` (default True), gates the trailing slow skim pass at the
z_offset floor — set it False to push exactly once (e.g. one mid-height lane
for a tall part).

A model whose SECOND hardware-ladder gate is open (``z_reference_validated``, default
False everywhere) additionally opens its block with a contact-free Z RE-REFERENCE:
the bed is driven to its bottom stop under the vendor's own guarded ``G380 S2``
primitive with soft end stops off, and the stop is DECLARED as ``z_travel_mm`` before
anything else in the block moves. It exists because the block's absolute Z moves rely on
a RETAINED Z datum that a power cycle destroys (002-H2S, 2026-09-04, eyewitnessed: the
bed drove past the Z floor). See :func:`z_reference_prologue_lines` for what each line
rests on and, in particular, which parts are proven and which are hypotheses the ladder
witnesses.

An optional ``bed_drop_clearance_mm`` (NULL = off) adds a mechanical release
assist after the home: the bed drives all the way DOWN from the lift height to
the machine bottom minus that clearance (bigger Z = bed farther from the
nozzle), then returns to the lift height before the sweep runs — jolting a
stuck part loose without changing the sweep itself. The machine bottom is the
target model's ``z_travel_mm`` (:func:`drop_z`, from the geometry registry, never
hardcoded); a profile that enables the assist against a model with no ``z_travel_mm``
fails closed. Two further tunings act AT that drop floor, emitted home → drop →
jitter → dwell → return: ``bed_drop_jitter_cycles``/``bed_drop_jitter_mm`` oscillate the
bed up-then-back (up FIRST, so no move passes the drop target) and
``bed_drop_dwell_s`` holds there for whole seconds as ``M400 S<n>`` — the LAST thing
that happens at the floor. Both are NULL = off and both fail closed without the drop
itself. A ``bed_drop_clearance_mm`` of exactly 0 aims the drop at the declared machine
bottom and is WARNED about, never refused (:func:`bottom_target_warning`).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from backend.app.utils.printer_models import DUAL_NOZZLE_HOME, is_bedslinger_model, is_dual_nozzle_model

if TYPE_CHECKING:
    from backend.app.models.eject_profile import EjectProfile
    from backend.app.services.eject.geometry import ModelGeometry

logger = logging.getLogger(__name__)

# Minimum width (mm) of an explicit X sweep sub-band. Narrower than this the
# toolhead cannot reliably clear a part across the band, so a tighter band is a
# safety error (schema-validated and re-checked here + in the validator).
SWEEP_BAND_MIN_WIDTH_MM = 10.0

# Minimum safe park Z (mm) — the FLOOR the toolhead parks at after the sweep. The
# block now parks the bed proportional to part height (``max(lift_z, PARK_Z_MM)``,
# so a tall part ends farther from the nozzle), and this constant is the lower
# bound so a tiny part with ``clearance_mm=0`` still parks at least this clear. A
# module constant (not an inline literal) so the same value is the validator's
# park-Z floor for the upper-Z ceiling guard (``_fmt(10.0)`` == "10", byte-identical).
PARK_Z_MM = 10.0

# The FLOOR (mm) of the cooldown hold — the lowest Z :func:`hold_placement` will ever
# ask for, whatever the operator's target.
#
# The hold happens with the toolhead parked AT THE CHUTE (the vendor's own ``G150.3``
# position), off the plate entirely, so the plate never touches the nozzle there and
# this number is not a part-to-nozzle clearance calculation. What it has to survive is
# the frame's own slop — thermal contraction as the bed cools, plus whatever the
# retained Z datum is off by — so it is sized as ten first layers (10 x 0.2 mm): small
# enough that the hold still buys the eject its whole one-flow Z move, large enough
# that no plausible drift closes it.
HOLD_Z_MIN_MM = 2.0


# Which of the three constraints decided a hold's Z. Reported rather than re-derived:
# the operator's target, the model's measured clear height, and the plate floor all
# produce a legal Z, and only the LABEL says whether the number in the log is the one
# the operator asked for or the one a clamp gave back.
HoldBound = Literal["target", "ceiling", "floor"]


@dataclass(frozen=True)
class HoldPlacement:
    """Where a cooldown hold puts the plate, and what decided it. See :func:`hold_placement`."""

    # The commanded hold Z: how far the bed sits BELOW the nozzle plane.
    z: float
    # ``max_z_height - z`` — where the part's top ACTUALLY ends up, signed: positive is
    # that far above the nozzle plane, negative that far below it. Never the target: it
    # is what the two clamps left, which is the only figure worth logging.
    part_top_mm: float
    bound: HoldBound


# Seconds :func:`estimate_runtime_segments` books for each ``G28`` line it walks.
#
# BOUNDED, NOT MEASURED, and deliberately so: a home's duration depends on where the
# toolhead starts. From the chute park it is a few centimetres plus the endstop
# back-off; from anywhere at all it is at most one full X/Y travel at the firmware's
# homing feed, which no bed in the fleet turns into ten seconds. Over-stating a move
# whose true cost this process cannot know is the estimator's own standing doctrine
# (the same rule that counts ``G380`` at its full commanded distance): the estimate
# feeds an ABORT deadline, so over-stating can only make a deadline too patient, while
# under-stating kills healthy ejects.
HOMING_ALLOWANCE_S = 10.0

# Non-motion seconds every eject job spends regardless of its geometry: the
# firmware's job spin-up before the first move, the finish-chime epilogue
# (``M1006`` melody, which the motion math cannot see) and the FINISH publish
# latency that lands the terminal in our callback. CALIBRATED FROM PRODUCTION: the
# motion math below scores the current single-pass profile at ~57 s while eight
# nominal ejects executed in 80-83 s wall clock. Added once to every estimate — it
# is deliberately generous, because the guard that consumes the estimate must
# never fire on a healthy sweep.
#
# WHAT THE Z RE-REFERENCE PROLOGUE DOES AND DOES NOT CHANGE HERE (2026-09-04). The
# calibration population above is entirely blocks WITHOUT that prologue, and it stays
# valid for them: the constant is unchanged and every existing golden is byte-identical.
# For a block WITH it, the drive's own commanded time is scored into ``reference_s`` and
# flows into ``total_s``, so the whole-job deadline grows by the drive and by nothing
# else — this constant is NOT re-fitted to cover it, because job spin-up and the finish
# chime did not get longer.
#
# The residue that is honestly UNMEASURED: whatever fixed cost the firmware adds for
# ``M211`` / ``G92`` / the ``G380`` guard's own settle, plus however early the guarded
# drive terminates against the stop (which SHORTENS the real job while the estimate
# counts the full commanded distance — see the domain rules). Both push in the same
# direction as this constant's existing generosity, i.e. toward a deadline that is too
# patient rather than too tight. The instrument that resolves it is the ladder's rung 2
# and the terminal's ``ran Ns (expected Ns)`` line on the first flag-on model; no number
# here is fitted to a block nobody has run yet.
EJECT_RUNTIME_OVERHEAD_S = 25.0

# The dual-nozzle homing forms (``DUAL_NOZZLE_HOME`` / ``DUAL_NOZZLE_FULL_HOME``)
# live in ``utils.printer_models`` — the single canonical source of truth shared
# with the dry-run wrapper and ``BambuMQTTClient.home_axes``. The generator's
# eject prologue homes X/Y only (never Z — a part sits on the plate), so it uses
# ``DUAL_NOZZLE_HOME`` (the X/Y torque pair) directly.

# Marker comments wrapping the generated block so it is unambiguously locatable
# in an injected file (and greppable in dry-run downloads).
BLOCK_START_PREFIX = "; ===== FARM EJECT BLOCK profile="
BLOCK_END_MARKER = "; ===== FARM EJECT BLOCK END ====="

# The sweep section's marker comment — the ONE origin shared by the emitter and by
# the beacon that must sit immediately above it (a second literal would let the two
# drift and silently move the drop/sweep boundary the watchdog times).
SWEEP_PHASE_MARKER = "; --- sweep: push part off the front edge ---"

# M73 PHASE BEACONS. ``mc_percent`` is M73-driven end to end and resets to 0 when the
# eject job starts, so these commands publish the block's phase boundaries over
# the only in-band channel the wire has (the H2S publishes no ``mc_print_line_number``).
# The percentages are arbitrary ORDERED markers, not progress: the watchdog only asks
# "is the reported percent still below the sweep beacon?". The completion epilogue's
# stock ``M73 P100 R0`` closes the series and is emitted verbatim with it.
PHASE_BEACON_REFERENCED_PCT = 2  # Z re-reference done (emitted ONLY by that prologue)
# Drop phase begins. The NAME is accurate again — the block lifts, homes, then drops —
# but it is P-NUMBER keyed either way (the watchdog speaks await_p5/await_p50/await_p75),
# so the constant's name was never what the wire carried. Emitted BEFORE the block's
# first Z move, so the drop-span deadline it opens covers that move, the home and the
# whole drop round trip.
PHASE_BEACON_LIFTED_PCT = 5
PHASE_BEACON_SWEEP_PCT = 50  # bed-drop done, sweep begins
PHASE_BEACON_PARK_PCT = 75  # sweep done, park begins
PHASE_BEACON_REFERENCED = f"M73 P{PHASE_BEACON_REFERENCED_PCT}"
PHASE_BEACON_LIFTED = f"M73 P{PHASE_BEACON_LIFTED_PCT}"
PHASE_BEACON_SWEEP = f"M73 P{PHASE_BEACON_SWEEP_PCT}"
PHASE_BEACON_PARK = f"M73 P{PHASE_BEACON_PARK_PCT}"

# Extra relative travel (mm) commanded on the guarded Z re-reference drive, ON TOP of
# the model's full ``z_travel_mm``.
#
# DERIVATION (not a measurement — there is nothing here to measure): the drive must be
# guaranteed to REACH the bottom stop from wherever the firmware's post-boot frame
# happens to think the bed is, so it must exceed the maximum remaining travel PLUS any
# plausible error in that frame. Full travel covers the first term exactly. 50 mm covers
# the second with room to spare: the largest standing offset the farm itself ever
# creates is the idle deep park at 75% of ``z_travel`` (~255 mm on an H2S), and a frame
# wrong by even that much still lands on the stop, because the drive is bottom-stop
# guarded and simply stops when it arrives. Overshoot costs SECONDS, never damage —
# undershoot silently declares a wrong frame, which is the failure this whole recipe
# exists to prevent. The asymmetry is why the number is generous rather than tight.
Z_REFERENCE_OVERTRAVEL_MM = 50.0

# Feedrate (mm/min) of the guarded drive. The vendor's own value for exactly this move:
# the stock H2S machine-start block issues ``G380 S2 Z32 F1200`` / ``G380 S2 Z-12 F1200``
# under ";===== avoid end stop =====", and ``G380 S2 Z30 F1200 ; lower heatbed to move
# toolhead" — copied rather than chosen.
Z_REFERENCE_FEED_MM_MIN = 1200

# Completion epilogue — the stock machine-end FINISH TAIL, copied verbatim from a
# production H2S plate (foundi-FarmManager/Print Files/
# _6_Half_Shell_PCO-M18-2656_top_surface_gcode.3mf → Metadata/plate_3.gcode), the
# segment from the feedrate/acc/time resets through the final `M73 P100 R0`.
#
# The eject sweep is now a STANDALONE, server-dispatched motion-only job whose file
# REPLACES the plate G-code entirely — it no longer splices after a real print's
# stock machine-end block. A standalone file WITHOUT that block ends FAILED at EOF
# even after clean motion (cosmetic, live-observed on a real H2S 2026-07-04). This
# tail is the firmware's job-completion handshake — progress/feedrate/accel resets,
# the air-filtration `M1002 judge_flag` conditional (J1/J2 fire only when the
# firmware set the flag; otherwise skipped), the finish chime, then `M400`/`M18` —
# so appending it makes the eject job register FINISH instead of FAILED-at-EOF.
# Verbatim: no commands the stock file lacks are invented; only insignificant
# trailing whitespace on the melody lines is normalised.
COMPLETION_EPILOGUE = """\
M220 S100  ; Reset feedrate magnitude
M201.2 K1.0 ; Reset acc magnitude
M73.2   R1.0 ;Reset left time magnitude

M1015.4 S0 K0 ;disable air printing detect

;=====printer finish air purification=========
M622.1 S0
M1002 judge_flag print_finish_air_filt_flag

M622 J1
M1002 gcode_claim_action : 66
M145 P1
M106 P6 S255
M400 S180
M106 P6 S0
M623

M622 J2
M1002 gcode_claim_action : 66
M145 P0
M106 P3 S127
M400 S180
M106 P3 S0
M623
;=====printer finish air purification=========

;=====printer finish  sound=========
M17
M400 S1
M1006 S1
M1006 A53 B10 L99 C53 D10 M99 E53 F10 N99
M1006 A57 B10 L99 C57 D10 M99 E57 F10 N99
M1006 A0 B15 L0 C0 D15 M0 E0 F15 N0
M1006 A53 B10 L99 C53 D10 M99 E53 F10 N99
M1006 A57 B10 L99 C57 D10 M99 E57 F10 N99
M1006 A0 B15 L0 C0 D15 M0 E0 F15 N0
M1006 A48 B10 L99 C48 D10 M99 E48 F10 N99
M1006 A0 B15 L0 C0 D15 M0 E0 F15 N0
M1006 A60 B10 L99 C60 D10 M99 E60 F10 N99
M1006 W
;=====printer finish  sound=========
M400
M18

M73 P100 R0"""

# Seconds of job-completion tail the estimator deliberately does NOT count, and which a
# post-sweep deadline must therefore tolerate before it may call a job wedged.
#
# It lives here, beside :data:`COMPLETION_EPILOGUE` (which holds the two ``M400 S180``
# dwells) and the ``M622``/``M623`` skip rule in :func:`estimate_runtime_segments`, so the
# fact and its numeric compensation can only ever change together.
#
# EVIDENCE (30 days of rotated prod logs, read 2026-08-31):
#
# * 24 watchdog kills since the M73 beacons shipped (2026-08-18). 23 fired with the
#   bed-drop phase already OBSERVED clear 46-56 s earlier; the 24th ran the beacon-blind
#   fallback (no phase attribution). Not one fired inside the phase the drop guard bounds.
# * Healthy ejects run ``ran ≈ expected ± 3 s``. A bimodal slow mode adds >=25-30 s
#   ENTIRELY AFTER the drop clears (drop-clear→terminal: healthy 23-27 s, killed 49-59 s
#   and TRUNCATED by the kill, so 25-30 s is a floor, not the size of the tail).
# * The mode appears on every printer in the fleet and in all three profile eras, which
#   is why the allowance is flat rather than derived per profile: it models firmware
#   behaviour, not geometry.
#
# THE MEASUREMENT ABOVE IS THE AUTHORITY. The SIZING hypothesis is one firing of the
# conditional air-purification purge (``M400 S180`` = 180 s) plus chime/publish slop
# (~20 s) — a hypothesis this constant does not depend on and cannot confirm. The
# instrument that settles it is the ``ran Ns (expected Ns)`` terminal INFO once
# post-sweep ejects are allowed to run to FINISH: a mode at +25-35 s refutes the purge,
# a mode near +180 s confirms it.
UNMODELLED_EPILOGUE_ALLOWANCE_S = 200.0


class EjectGenerationError(ValueError):
    """Raised when an eject block cannot be safely generated for the inputs."""


def z_reference_prologue_lines(geometry: ModelGeometry) -> list[str]:
    """The contact-free Z RE-REFERENCE prologue for ``geometry``, or ``[]``.

    Emitted ONLY when ``geometry.z_reference_validated`` is True — False for every
    seeded model, so this returns ``[]`` everywhere until a model's own hardware ladder
    flips the flag through ``PUT /model-geometry``. A default-constructed
    :class:`~backend.app.services.eject.geometry.ModelGeometry` therefore cannot produce
    a block (pinned by a test): a new motion must never appear by omission.

    WHY IT EXISTS (2026-09-04, eyewitnessed on 002-H2S). The eject block homes X/Y only
    — Z-homing probes the bed centre, under the part — so it relies on a RETAINED Z
    datum. A power cycle destroys that datum, and the operator's post-outage eject drove
    the bed DOWN past the Z floor because every absolute Z move ran against the
    firmware's fabricated post-boot frame. Escalating to a human is not a recovery:
    nobody can re-home Z with a part on the plate.

    THE SEQUENCE, and what each line rests on::

        M211 Z0                 ; soft end stop OFF — the frame is untrusted, and a
                                ;   clamp against an untrusted frame IS the 002-H2S
                                ;   failure (the move was refused/clipped, not the fall)
        G91                     ; relative
        G380 S2 Z<travel+50>    ; drive the bed DOWN, guarded
        G90                     ; absolute again, before any G0/G1 can run
        G92 Z<z_travel_mm>      ; declare the stop
        M211 Z1                 ; soft end stop ON again, now against a real frame
        M73 P2                  ; phase beacon: the drive is bounded by its own deadline

    **The load-bearing safety argument is DIRECTION, not the guard.** +Z moves the bed
    AWAY from the nozzle on every bed-on-Z model in the fleet, so this drive physically
    cannot touch a part on the plate however far it travels or however wrong the frame
    is. Everything else is a bonus.

    **"``S2`` guards on the bottom end stop" is a HYPOTHESIS, not a proven fact.** The
    vendor's stock H2S machine-start block supports it — ``G380 S2 Z32`` / ``G380 S2
    Z-12`` appear under the literal comment ";===== avoid end stop =====", and ``G380 S2
    Z30 F1200 ; lower heatbed to move toolhead" runs before any homing, which is only
    sensible if the bed may already be sitting on that stop — but it does NOT prove it,
    because the same block issues ``G380 S2 Z-12`` in the opposite direction, where a
    bottom-stop guard would be meaningless. Rung 1 of the hardware ladder is this
    hypothesis's only witness, which is why the flag exists at all.

    **``G92 Z<z_travel_mm>`` is DECLARED, not measured**, and the declaration is safe by
    a reachability argument rather than by calibration: every H2S in the fleet commands
    ``G1 Z340`` on every eject today and reaches it, so the physical stop sits AT or
    BEYOND ``z_travel_mm`` fleet-wide. Declaring the stop to be ``z_travel_mm``
    therefore errs by ``≤ 0`` — i.e. strictly on the MORE-clearance side. The bottom-stop
    height is a per-MACHINE assembly fact, so it is deliberately not a per-model measured
    constant.

    **NO ``G28 Z``, no probe, ever** (operator rule 2026-09-04): a part is on the plate.

    Refuses (like the bed-drop assist) on a bedslinger — its bed carries no Z, so a
    "drive the bed to its stop" recipe is a different recipe for a different ladder —
    and on a model with no ``z_travel_mm``, since the declaration has nothing to say.
    """
    if not geometry.z_reference_validated:
        return []
    if is_bedslinger_model(geometry.model_key):
        raise EjectGenerationError(
            f"Z re-reference is enabled but {geometry.model_key!r} is a bedslinger (the gantry carries Z, "
            "not the bed) — a gantry-Z reference is a different recipe and needs its own hardware ladder"
        )
    if geometry.z_travel_mm is None:
        raise EjectGenerationError(
            f"Z re-reference is enabled but model {geometry.model_key!r} has no z_travel_mm — the drive has "
            "no stop to declare; set it via PUT /model-geometry"
        )
    drive_mm = geometry.z_travel_mm + Z_REFERENCE_OVERTRAVEL_MM
    return [
        "; --- Z re-reference: contact-free, bottom-stop guarded (bed moves AWAY from the part) ---",
        "M211 Z0",
        "G91",
        f"G380 S2 Z{_fmt(drive_mm)} F{Z_REFERENCE_FEED_MM_MIN}",
        "G90",
        f"G92 Z{_fmt(geometry.z_travel_mm)}",
        "M211 Z1",
        f"{PHASE_BEACON_REFERENCED} ; phase beacon: Z re-referenced - eject runtime watchdog",
    ]


def _fmt(value: float) -> str:
    """Format a coordinate/temperature for G-code: trim trailing zeros, no exponent."""
    return f"{value:g}"


def _clamp(value: float, lo: float, hi: float) -> float:
    """Constrain `value` to the closed interval [lo, hi]."""
    return max(lo, min(value, hi))


def _linspace(start: float, end: float, count: int) -> list[float]:
    """`count` values evenly spaced from `start` to `end` (both inclusive)."""
    if count <= 1:
        return [start]
    step = (end - start) / (count - 1)
    return [start + step * i for i in range(count)]


def block_start_marker(profile: EjectProfile) -> str:
    """The exact block-start marker comment for `profile` (also used by validator)."""
    return f"{BLOCK_START_PREFIX}{profile.name} ====="


def part_height_error(max_z_height: float, profile: EjectProfile) -> str | None:
    """The over-height refusal for ``max_z_height`` under ``profile``, or ``None``.

    The ONE origin of that sentence. :func:`generate_eject_gcode` raises it as an
    :class:`EjectGenerationError`, :func:`cooldown_hold_lines` raises the same, and the
    validator's part-height guard appends it verbatim — so the three refuse the same
    part in the same words, and the operator reads one message however the refusal
    reached them.
    """
    if max_z_height > profile.max_part_height_mm:
        return (
            f"Part height {max_z_height} mm exceeds profile max_part_height_mm "
            f"{profile.max_part_height_mm} mm — refusing to generate eject block"
        )
    return None


def lift_z(max_z_height: float, profile: EjectProfile) -> float:
    """The LIFT height (mm): the part top plus the profile's clearance.

    The height the sweep runs from and the height the bed-drop assist returns to. One
    origin, because the validator's Z-ceiling guard has to bound exactly the number the
    generator emitted, not a re-derivation of it.
    """
    return max_z_height + profile.clearance_mm


def park_z(max_z_height: float, profile: EjectProfile) -> float:
    """The block's END-STATE park height (mm): :func:`lift_z` floored at :data:`PARK_Z_MM`.

    The floor exists so a tiny part under a legal ``clearance_mm=0`` still ends the
    block with the bed a usable distance from the nozzle.
    """
    return max(lift_z(max_z_height, profile), PARK_Z_MM)


def drop_z(profile: EjectProfile, geometry: ModelGeometry) -> float | None:
    """The bed-drop release assist's FLOOR target (mm), or ``None`` when it is off.

    The machine bottom (``geometry.z_travel_mm``) minus the profile's
    ``bed_drop_clearance_mm``. Bigger Z = bed farther from the nozzle, so the clearance
    is how far the drop stops SHORT of the declared bottom.

    ONE origin, shared with the validator, for the same reason :func:`lift_z` is: the
    validator opens its Z ceiling to exactly this number, and two copies of a coordinate
    rule is how a guard ends up bounding a number the generator never emitted.

    ``None`` covers both "assist off" (NULL clearance) and "no registered
    ``z_travel_mm``" — the second is a fail-closed REFUSAL at both call sites, never a
    silent skip, so this function stays a pure derivation and each caller raises or
    appends in its own idiom. Read defensively (``getattr``) so a transient profile
    built without the column still generates.
    """
    bed_drop = getattr(profile, "bed_drop_clearance_mm", None)
    if bed_drop is None or geometry.z_travel_mm is None:
        return None
    return geometry.z_travel_mm - bed_drop


def degenerate_drop_error(max_z_height: float, profile: EjectProfile, geometry: ModelGeometry) -> str | None:
    """The degenerate-drop refusal for this profile/model pair, or ``None``.

    The ONE origin of that sentence, alongside :func:`part_height_error`: a drop target
    that is not BELOW the lift height is not a drop at all — it would raise the bed
    toward the part instead of away from it, and the validator's Z ceiling would open to
    a number above the block's own sweep height. Generator raises it, validator appends
    it, so both refuse the same profile in the same words.
    """
    drop = drop_z(profile, geometry)
    if drop is None:
        return None
    lift = lift_z(max_z_height, profile)
    if drop > lift:
        return None
    bed_drop = profile.bed_drop_clearance_mm
    return (
        f"bed-drop target Z{drop:g} (z_travel {geometry.z_travel_mm:g} - clearance "
        f"{bed_drop:g}) is not below the lift height Z{lift:g} — degenerate drop"
    )


def bottom_target_warning(profile: EjectProfile, geometry: ModelGeometry) -> str | None:
    """A WARNING when the drop aims at the declared machine bottom itself, else ``None``.

    ``bed_drop_clearance_mm = 0`` puts the drop target exactly on ``z_travel_mm``, which
    means the move is COMMANDED to reach the mechanical stop. Every mm of clearance is a
    mm of debris under the plate that cannot corrupt the block's Z frame (see the module
    docstring's FRAME INTEGRITY note); at zero there is no margin left, and a drop that
    falls short against an obstruction looks exactly like a normal cycle — the bed stops
    early either way, and there is no Z telemetry to tell the two apart.

    Deliberately a warning and NOT an error: the fleet's live profile runs 0 today, and
    an error would 409 it out of production. The fix is a profile value taken through the
    hardware ladder, not a refusal. Both surfaces (the generator's build INFO line and
    :func:`~backend.app.services.eject.validator.validate_eject_gcode`'s ``warnings``)
    read this ONE predicate.
    """
    drop = drop_z(profile, geometry)
    if drop is None or drop != geometry.z_travel_mm:
        return None
    return (
        f"drop target Z{drop:g} is the declared machine bottom (z_travel_mm) — a short-fall on an "
        "obstruction is indistinguishable from a normal cycle; set bed_drop_clearance_mm > 0"
    )


def hold_placement(max_z_height: float, clear_above_mm: float, part_top_mm: float) -> HoldPlacement:
    """Where to hold a part of ``max_z_height``: the operator's target under two clamps.

    ``part_top_mm`` is the OPERATOR's target, and it names where the part's TOP should
    sit relative to the nozzle plane while the plate is held — positive = that far ABOVE
    the plane, up into the clear zone, so the aux fan's stream (centred on the plane)
    hits the part's flank with the plate itself in the stream; 0 = flush with the plane,
    so the fan blows across the top surface; negative = that far BELOW the plane, so the
    fan blows above the part and circulates air over it. One signed number expresses
    every intent, and ``z = max_z_height - part_top`` converts it (bed Z is measured
    downward from the nozzle, so a bigger Z is a plate FARTHER from the plane).

    It is expressed relative to the PART TOP rather than as a plate height because that
    is what makes ONE fleet-wide setting admissible: "top 20 mm above the plane" means
    the same physical thing for a 20 mm part and a 55 mm one, while "plate at Z30" does
    not. Two clamps then keep every value safe on their own:

    * the CEILING — ``clear_above_mm``, the model's MEASURED clearance above the nozzle
      plane with the toolhead parked at the chute. The part never rises past it, however
      large the target. Consumed with NO margin against ``max_z_height``, which is the
      slicer's MODELLED height and not a measurement: a warped or failed part stands
      taller and nothing re-checks it;
    * the FLOOR — :data:`HOLD_Z_MIN_MM`, the closest the plate itself may come to the
      plane whatever the target asks for.

    The table (H2S, ``clear_above_mm`` 100 measured 2026-09-10)::

        max_z   target   clear    z      part top    bound
        50.1    100      100      2.0    48.1        floor     <- the fleet's parts today
        50.1    0        100      50.1   0           target    <- fan across the top
        50.1    -20      100      70.1   -20         target    <- fan above the part
        100     100      100      2.0    98          floor
        120     150      100      20     100         ceiling   <- never more than 100
        50.1    100      51       2.0    48.1        floor     <- the old placeholder

    Invariant for EVERY input: ``max_z_height - z <= clear_above_mm``. It survives the
    floor because the floor can only bind when ``max_z_height < target + 2`` and the
    target is already at most the ceiling, so the part top it leaves is below it too.

    There is deliberately NO ``z_travel_mm`` bound: the setting's own ``ge=-50`` keeps
    every reachable hold above the vendor's end-block park (``max_z/2 + 98``, ≥ 108 for
    any admitted part, against ``z <= max_z + 50 <= 105``), so a hold is always a RAISED
    plate and a travel bound would be a guard for a state no input can reach.

    Both scalars arrive as arguments rather than as a geometry attribute and a settings
    read on purpose — the registry column and the settings key that carry them are owned
    elsewhere, and the generator must not take a dependency on either name to compute a
    height.
    """
    target = min(part_top_mm, clear_above_mm)  # ceiling: never past the witnessed clear zone
    z = max_z_height - target
    bound: HoldBound = "target" if part_top_mm <= clear_above_mm else "ceiling"
    if z < HOLD_Z_MIN_MM:  # floor: the plate never closer to the plane than this
        z, bound = HOLD_Z_MIN_MM, "floor"
    return HoldPlacement(z=z, part_top_mm=max_z_height - z, bound=bound)


def cooldown_hold_lines(
    max_z_height: float, profile: EjectProfile, clear_above_mm: float, part_top_mm: float
) -> tuple[list[str], HoldPlacement]:
    """The COOLDOWN HOLD command block: transit clear, park at the chute, hold, release.

        Sent over the G-code line channel while a finished plate waits out its cooldown, so
        that when the eject job finally runs the plate is already near the nozzle plane and
        the eject's first Z move is ONE flow from there.

        The lines, and what each rests on::

            M17                     ; motors back on after the stock shutdown's M18
            G90                     ; absolute
            G1 Z{park_z} F900       ; TRANSIT: max(lift_z, PARK_Z_MM) — the same height the
                                    ;   eject block's own park uses, so the toolhead's
                                    ;   traverse to the chute clears the part exactly as the
                                    ;   post-sweep traverse does. It is never TIGHTER than
                                    ;   the vendor's own precondition either: the stock end
                                    ;   block runs its ``G150.3`` at ``max_layer_z + 10``.
            M400
            G150.3                  ; vendor macro: toolhead to the chute park
            M400
            G1 Z{placement.z} F900  ; HOLD (see :func:`hold_placement`)
            M400
            M18                     ; motors off again — the hold is a parked state, not a
                                    ;   held position under current

    The placement is evaluated ONCE, here, and returned beside the lines: the caller
    records what was SENT rather than re-deriving it, so the commanded Z, the Z the eject
    estimator is seeded from and the Z the operator's card renders can never diverge.

        ``G150.3`` is the farm's FIRST proprietary-macro emission, and it is admissible only
        because it copies the vendor's own verbatim end-block state: by cooldown time the
        stock end block has already run ``T65535`` and ``G150.2``, and the vendor's own
        ``G150.3`` runs at exactly that machine state. It is COMMANDED rather than assumed
        because :class:`PrinterState` carries no toolhead XY — a screen jog, or a re-entry
        into the hold from an already-held plate, would otherwise move the bed toward a
        toolhead nobody can see, and fail silently.

        This block deliberately does NOT pass :func:`~backend.app.services.eject.validator.
        validate_eject_gcode`: that validator's subject is the eject BLOCK (a home, a sweep,
        a park, an envelope), none of which this is. Its safety comes from the same two
        functions the eject itself uses for every coordinate it emits — :func:`park_z` and
        :func:`hold_placement` — and its one refusal is :func:`part_height_error`, the same
        sentence the generator raises.

        A target below ``-clearance_mm`` holds the plate LOWER than the eject block's own
        ``lift_z``, so that block's first Z move then travels toward the nozzle instead of
        away from it. Safe either way: the toolhead is at the chute for the whole hold, and
        the target keeps the part top at least ``clearance_mm`` under the lift height the
        block moves to — which is the height its X/Y home runs at, so the direction of that
        first move never changes what the home clears.

        Raises:
            EjectGenerationError: the part is taller than the profile's guard.
    """
    height_error = part_height_error(max_z_height, profile)
    if height_error is not None:
        raise EjectGenerationError(height_error)
    placement = hold_placement(max_z_height, clear_above_mm, part_top_mm)
    lines = [
        "M17",
        "G90",
        f"G1 Z{_fmt(park_z(max_z_height, profile))} F900 ; transit: never tighter than the vendor's own "
        "G150.3 precondition",
        "M400",
        "G150.3 ; vendor macro: toolhead to the chute park (the end block's own last travel)",
        "M400",
        f"G1 Z{_fmt(placement.z)} F900 ; hold: part top {_fmt(placement.part_top_mm)} mm above the nozzle plane "
        f"(target {_fmt(part_top_mm)}, clear {_fmt(clear_above_mm)}, bound {placement.bound})",
        "M400",
        "M18",
    ]
    return lines, placement


@dataclass(frozen=True)
class EjectRuntimeSegments:
    """One eject block's commanded time, split at all three M73 phase beacons.

    The split exists because the whole-job deadline can only catch a stall big enough
    to overrun the ENTIRE eject's margin. ``drop_span_s`` bounds the bed-drop phase by
    itself, which is the phase that stalls (2026-07-31 gouged plate, 2026-08-15 009-H2S)
    and the one that must be caught BEFORE the sweep touches the plate. Since the block
    became ONE Z flow that span also holds the block's FIRST Z move and the X/Y home:
    both now sit between the P5 and P50 beacons.

    ``sweep_span_s`` bounds the PLATE-CONTACT phase the same way, and ``tail_s`` is what
    remains once the toolhead can no longer reach the part. The boundary between them is
    :data:`PHASE_BEACON_PARK`, which :func:`generate_eject_gcode` emits at exactly ONE
    site — immediately above the park move, unconditionally, with the assist on or off —
    so a reported percent at/above it can never describe a job still executing sweep
    lanes. Every rule that treats the post-park window as harmless rests on that single
    unconditional emission; a second emission site, or one behind a profile flag, would
    silently make a mid-sweep job look finished.

    ``reference_s`` is the guarded Z re-reference drive's own budget, and it is ``None``
    for a block that does not emit :data:`PHASE_BEACON_REFERENCED` at all — i.e. every
    block on every model whose ladder has not flipped ``z_reference_validated``.
    ``None`` (rather than 0.0) is what lets the watchdog tell "this block has no such
    phase" from "this block's drive is instant", so it disarms the lane instead of
    arming a zero-length deadline.

    ``reference_s``, ``pre_s``, ``drop_span_s``, ``sweep_span_s`` and ``tail_s`` carry no
    share of :data:`EJECT_RUNTIME_OVERHEAD_S` — the overhead is job spin-up plus the
    finish chime, neither of which belongs to a phase — so only ``total_s`` includes it.
    """

    # Motion before the M73 P5 beacon. 0 for every block this generator emits — the
    # beacon is emitted BEFORE the first Z move, so the prologue holds no motion at all
    # (the Z re-reference drive, when present, is its own ``reference_s`` segment ahead
    # of it). Non-zero only for a hand-edited or legacy block that moves in its prologue.
    pre_s: float
    drop_span_s: float  # P5 → P50: the first Z move, the home, the drop, the floor behaviours, the return
    sweep_span_s: float  # commanded time between the P50 and P75 beacons (the sweep lanes)
    tail_s: float  # the P75 beacon onward (park + completion epilogue)
    total_s: float  # every span above + EJECT_RUNTIME_OVERHEAD_S
    reference_s: float | None = None  # block start → the M73 P2 beacon; None = no such phase


def _unknown_z_travel_mm(target: float, z_travel_mm: float | None) -> float:
    """The LONGEST travel an absolute ``Z<target>`` could be, from an unknown position.

    The bed is somewhere in ``[0, z_travel_mm]``, so the move is at most
    ``max(target, z_travel_mm - target)``. With no ``z_travel_mm`` the only bound left
    is ``target`` itself (the distance from Z0).
    """
    return max(target, (z_travel_mm - target) if z_travel_mm is not None else 0.0)


def estimate_runtime_s(gcode: str, *, start_z: float | None = None, z_travel_mm: float | None = None) -> float:
    """Expected wall-clock execution time (seconds) of an eject block.

    Total-only façade over :func:`estimate_runtime_segments` — the same single walk, so
    the two can never disagree about what the machine was told to do. ``start_z`` and
    ``z_travel_mm`` mean exactly what they mean there."""
    return estimate_runtime_segments(gcode, start_z=start_z, z_travel_mm=z_travel_mm).total_s


def estimate_runtime_segments(
    gcode: str, *, start_z: float | None = None, z_travel_mm: float | None = None
) -> EjectRuntimeSegments:
    """Per-phase expected execution time of an eject block.

    A deliberately small kinematic model — constant-velocity moves at the modal
    feedrate, no acceleration/jerk profile — because it feeds an abort deadline, not a
    progress bar. It systematically UNDER-states a real machine (which must accelerate
    into every move), which is the safe direction: the
    :data:`EJECT_RUNTIME_OVERHEAD_S` constant absorbs the difference and the guard
    margins absorb the rest.

    Segment boundaries are the EXACT beacon lines :data:`PHASE_BEACON_REFERENCED`,
    :data:`PHASE_BEACON_LIFTED`, :data:`PHASE_BEACON_SWEEP` and :data:`PHASE_BEACON_PARK`
    (after comment stripping). Matching the literal — not "any M73" — is what keeps the
    epilogue's stock ``M73 P100 R0`` inside ``tail_s`` where it belongs. Boundaries only
    ever advance, so a repeated beacon cannot reopen a closed segment. M73 itself
    contributes no time (it falls through the motion branches, as it always has).

    Domain rules baked in:

    * **``M622``/``M623`` conditional blocks are skipped entirely.** The stock finish
      tail's air-purification blocks each hold an ``M400 S180``, and whether either
      fires is decided by ``print_finish_air_filt_flag`` — firmware state this process
      cannot read, on the wire or anywhere else. Counting both would add 360 s to every
      estimate and make the guard structurally unable to fire, so the estimate is the
      floor: what the machine was told to do MINUS whatever the firmware may add here.
      :data:`UNMODELLED_EPILOGUE_ALLOWANCE_S` is that omission's numeric compensation and
      carries the measurement behind its size.
    * **``G28`` (any dialect — ``G28 X Y``, the dual-nozzle torque forms
      ``G28 X T300`` / ``G28 Y T300``) zeroes X and Y and books
      :data:`HOMING_ALLOWANCE_S`** into whichever segment it sits in — the drop span, in
      every block this generator emits. It never homes Z in an eject block (a part sits
      on the plate), so it leaves the Z position exactly as it found it: UNKNOWN unless
      seeded or declared (below).
    * **``G91``/``G90`` switch the distance mode, and a ``G0/G1`` in relative mode is
      counted as a DISPLACEMENT** (its parameters are deltas, not coordinates) rather
      than differenced against the last position, which would score a 10 mm relative
      step as a 330 mm move. The generator emits no relative ``G0/G1`` — the validator
      forbids one outright — so this rule exists to keep the model HONEST about the
      dialect it walks rather than to score anything the farm currently emits.
    * **``G380 S2 Z<d>`` is a guarded relative move and is counted at its FULL commanded
      distance.** That deliberately OVER-states it: the drive stops early, at the bottom
      stop, by design — usually well before ``d``. Over-stating a drive is the safe
      direction for a deadline (it can only be too patient), and there is no way to know
      the remaining travel from here, because the frame it would be measured in is the
      fabricated one this drive exists to replace.
    * **``G92 Z<v>`` DECLARES the Z position** — after it, Z is known and subsequent
      absolute Z moves are measurable. It WINS over ``start_z``: the Z re-reference
      prologue declares the frame the rest of the block runs in, and a caller's belief
      about where the plate was cannot outrank the machine's own declaration.
    * **An absolute Z move from an UNKNOWN Z counts the LONGEST travel it could be** —
      ``max(target, z_travel_mm - target)`` (:func:`_unknown_z_travel_mm`). The block's
      FIRST Z move is its lift, and the bed drop that follows it inside the same span is
      the move that stalls, so the old "unknown Z contributes 0 mm" rule would understate
      the drop span by hundreds of mm — 14-23 s at F900 — and the ``stage=drop`` deadline
      would kill healthy ejects. Over-stating an unknown move is the safe direction and the same
      doctrine ``G380`` is counted under. ``start_z`` removes the guess entirely when
      the caller KNOWS where the plate is (the cooldown hold parked it there), making
      that first move exact; without it the walk is a bound, not a measurement.
    * **A move on an X/Y axis with no known prior position contributes 0 mm.** A
      ``G28`` precedes every X/Y move in every block this generator emits, so the rule
      can only ever apply to a hand-edited one.
    * ``M400 S<n>`` outside a skipped block dwells ``n`` seconds; a bare ``M400``
      (queue drain) dwells 0.
    * A move emitted before any feedrate has been seen contributes no time
      (defensive — the generator always emits an explicit F).
    """
    # Index 0 = the Z re-reference drive (or, in a block without that phase, the plain
    # prologue), 1 = pre, 2 = drop span, 3 = sweep span, 4 = tail. The beacons advance
    # `segment`; which of 0/1 the prologue landed in is resolved after the walk, from
    # whether the P2 beacon was present at all.
    segments = [0.0, 0.0, 0.0, 0.0, 0.0]
    segment = 0
    saw_reference_beacon = False
    feed_mm_min: float | None = None
    # None = position not yet known on that axis (see the G28/G92/unknown-axis rules).
    # Z starts at the caller's seed when there is one: the cooldown hold parks the plate
    # at a height the server COMMANDED, so the block's first Z move is exactly known.
    pos: dict[str, float | None] = {"X": None, "Y": None, "Z": start_z}
    relative = False
    in_conditional = False

    for raw_line in gcode.splitlines():
        code = raw_line.split(";", 1)[0].strip()
        if not code:
            continue
        # ``M622.1 S0`` (the conditional PREPARE) also opens the skip: everything
        # from it to the next ``M623`` is firmware-conditional either way, and the
        # dwell we must not count lives inside.
        if code.startswith("M622"):
            in_conditional = True
            continue
        if in_conditional:
            if code.startswith("M623"):
                in_conditional = False
            continue
        if code == PHASE_BEACON_REFERENCED:
            saw_reference_beacon = True
            segment = max(segment, 1)
            continue
        if code == PHASE_BEACON_LIFTED:
            segment = max(segment, 2)
            continue
        if code == PHASE_BEACON_SWEEP:
            segment = max(segment, 3)
            continue
        if code == PHASE_BEACON_PARK:
            segment = max(segment, 4)
            continue

        tokens = code.split()
        word = tokens[0].upper()

        if word == "G91":
            relative = True
            continue
        if word == "G90":
            relative = False
            continue

        if word == "G92":
            # A declaration, not a motion: it costs no time and makes Z measurable.
            for token in tokens[1:]:
                axis = token[0].upper()
                if axis in pos:
                    try:
                        pos[axis] = float(token[1:])
                    except ValueError:
                        pass
            continue

        if word == "G380":
            # Guarded relative move — counted at its full commanded distance (see the
            # domain rule above) and, being guarded, it moves the machine to a position
            # this model cannot know. Z stays/becomes unknown; the ``G92`` that follows
            # is what makes it knowable again.
            distance = 0.0
            for token in tokens[1:]:
                axis = token[0].upper()
                try:
                    value = float(token[1:])
                except ValueError:
                    continue
                if axis == "F":
                    feed_mm_min = value
                elif axis in pos:
                    distance += abs(value)
                    pos[axis] = None
            if feed_mm_min and distance > 0:
                segments[segment] += distance / feed_mm_min * 60.0
            continue

        if word.startswith("G28"):
            pos["X"] = 0.0
            pos["Y"] = 0.0
            segments[segment] += HOMING_ALLOWANCE_S
            continue

        if word == "M400":
            for token in tokens[1:]:
                if token[0].upper() == "S":
                    try:
                        segments[segment] += float(token[1:])
                    except ValueError:
                        pass
                    break
            continue

        if word not in ("G0", "G1"):
            continue

        # Per-axis distance. In ABSOLUTE mode, against the LAST KNOWN position — an axis
        # whose prior position is unknown contributes 0 and becomes known after the move.
        # In RELATIVE mode the parameters ARE the displacement, and the resulting
        # position is only knowable if the prior one was.
        squared = 0.0
        for token in tokens[1:]:
            axis = token[0].upper()
            try:
                value = float(token[1:])
            except ValueError:
                continue
            if axis == "F":
                feed_mm_min = value
            elif axis in pos:
                prior = pos[axis]
                if relative:
                    squared += value**2
                    pos[axis] = None if prior is None else prior + value
                else:
                    if prior is not None:
                        squared += (value - prior) ** 2
                    elif axis == "Z":
                        squared += _unknown_z_travel_mm(value, z_travel_mm) ** 2
                    pos[axis] = value
        if feed_mm_min and squared > 0:
            segments[segment] += math.sqrt(squared) / feed_mm_min * 60.0

    # Which of slots 0/1 held the prologue depends on whether the block has a Z
    # re-reference phase at all. With the beacon present, slot 0 IS the guarded drive and
    # slot 1 is the rest of the prologue. Without it, nothing ever advanced past 0, so
    # the two sum to the prologue and the phase is reported as absent — the distinction
    # the watchdog needs to disarm the lane rather than arm a zero-length deadline.
    reference_s = segments[0] if saw_reference_beacon else None
    pre_s = segments[1] if saw_reference_beacon else segments[0] + segments[1]
    _, _, drop_span_s, sweep_span_s, tail_s = segments
    return EjectRuntimeSegments(
        pre_s=pre_s,
        drop_span_s=drop_span_s,
        sweep_span_s=sweep_span_s,
        tail_s=tail_s,
        # Summed in this exact left-to-right order, with the new span as a leading 0.0
        # when it is absent: adding zero is exact, so a block without the re-reference
        # phase yields the SAME float — to the bit — that it did before this phase
        # existed. ``sum()`` over the slot list does not (it differs by one ULP), and the
        # invariant "the total IS the sum of the spans" is asserted with exact equality.
        total_s=(reference_s or 0.0) + pre_s + drop_span_s + sweep_span_s + tail_s + EJECT_RUNTIME_OVERHEAD_S,
        reference_s=reference_s,
    )


def generate_eject_gcode(
    profile: EjectProfile,
    max_z_height: float,
    geometry: ModelGeometry,
) -> str:
    """Build the MOTION-ONLY eject G-code block for `profile` at part height `max_z_height`.

    The block is a self-contained, self-completing eject-only job. Anatomy, in
    emission order::

        ; ===== FARM EJECT BLOCK profile={name} =====
        M17                                   ; re-engage the motors
        <Z re-reference prologue>             ; dormant per model (z_reference_validated)
        G90
        M73 P5                                ; PHASE BEACON: drop phase begins — emitted
                                              ;   BEFORE the first Z move, so the drop-span
                                              ;   deadline covers it
        M140 S0                               ; bed heater off (defensive)
        M106 P2 S0                            ; aux fan off (the cooldown prep runs it)
        M106 P3 S0                            ; chamber fan off (ditto; the stock end
                                              ;   block's own line — duct mode untouched)
        G1 Z{lift_z} F900                     ; the block's FIRST Z move: the sweep height
        <G28 X Y | DUAL_NOZZLE_HOME>          ; home X/Y (NEVER Z) at the sweep height,
                                              ;   clear of the part by clearance_mm, and
                                              ;   BEFORE the drop — in the frame the
                                              ;   print left
        G1 Z{drop_z} F900                     ; assist only: the drop, a round trip DOWN
        <jitter strokes>                      ; assist only, at the drop floor
        M400 S{dwell}                         ; assist only: the LAST thing at the floor
        G1 Z{lift_z} F900                     ; assist only: return to the sweep height
        M73 P50                               ; PHASE BEACON: sweep begins
        <descending sweep lanes>
        M73 P75                               ; PHASE BEACON: sweep done, park begins
        G1 Z{park_z} F900 / G1 X.. Y.. F9000  ; park: bed clear FIRST, then centre
        <COMPLETION_EPILOGUE>                 ; stock finish tail — the job ends FINISH
        ; ===== FARM EJECT BLOCK END =====

    There is exactly ONE Z flow: the first move goes straight to the LIFT height — the
    height the sweep runs from — from wherever the plate happens to be (the vendor's
    parked ~Z123 after a stock shutdown, or the cooldown hold's ~Z2,
    :func:`cooldown_hold_lines`). The X/Y home follows that move immediately, so it
    never runs beside a held plate whose part stands above the nozzle plane, and it
    PRECEDES the bed-drop, so it never runs in a frame a silent drop stall may have
    corrupted. The drop, when the assist is on, is a round trip down from the lift
    height and back to it. This docstring's diagram is the canonical statement of that
    order; the validator's home-order guard is the canonical statement of the rule.

    The block's END STATE parks the bed at :func:`park_z` (the lift height, floored at
    :data:`PARK_Z_MM`), Z before XY, so a part that survived the sweep sits clear of
    the nozzle.

    There is NO in-file cooldown wait: the bed-cooldown gate moved OUT of the
    G-code into the eject monitor, which holds the plate-clear gate until the live
    ``bed_temper`` reaches the profile's ``cooldown_temp_c`` and only THEN dispatches
    this motion-only job. ``M140 S0`` (heater off) is still emitted defensively, as are
    the ``M106 P2 S0`` / ``M106 P3 S0`` pair that stops the cooldown fans the prep ran
    (aux and chamber exhaust); the old ``M190 R`` thermal wait is gone.

    Args:
        profile: the eject profile (all tunable parameters).
        max_z_height: parsed part top Z from the 3MF gcode header (mm).
        geometry: the target model's :class:`~backend.app.services.eject.geometry.ModelGeometry`
            (bed rectangle + travel envelope), resolved from the registry by the
            caller. Pure input — the generator does no DB / model-string lookup.

    Returns:
        The complete eject block as a newline-terminated string.

    Raises:
        EjectGenerationError: part taller than the profile's ``max_part_height_mm``
            guard; a degenerate sweep after the travel-envelope clamp; or the
            bed-drop release assist is enabled but the model has no
            ``z_travel_mm`` in its geometry row, or the drop target is not below
            the lift height (degenerate drop).
    """
    bed_x, bed_y = geometry.bed
    x_min, x_max, y_min, y_max = geometry.envelope

    height_error = part_height_error(max_z_height, profile)
    if height_error is not None:
        raise EjectGenerationError(height_error)

    # Lane Y endpoints: profile intent is front = -front_overhang, back =
    # bed_y + back_overhang, but the machine cannot travel past its soft limits,
    # so both are clamped into the travel envelope. Clamping is silent (intent is
    # preserved as closely as the machine allows); only a collapse is fatal.
    front_y = _clamp(-profile.front_overhang_mm, y_min, y_max)
    back_y = _clamp(bed_y + profile.back_overhang_mm, y_min, y_max)
    if front_y >= back_y:
        raise EjectGenerationError(
            f"Eject sweep degenerate after travel-envelope clamp: front Y {front_y:g} "
            f">= back Y {back_y:g} (envelope Y [{y_min:g}, {y_max:g}])"
        )

    # X lanes: an explicit sub-band [min, max] when BOTH are set, else the full
    # margin-inset bed width (unchanged default). Exactly one bound set, an
    # inverted/too-narrow band, or a band past the bed edge is a safety error.
    band_lo = profile.sweep_x_min_mm
    band_hi = profile.sweep_x_max_mm
    if (band_lo is None) != (band_hi is None):
        raise EjectGenerationError("sweep_x_min_mm and sweep_x_max_mm must both be set or both be null")
    if band_lo is not None:
        if not (0 <= band_lo < band_hi):
            raise EjectGenerationError(
                f"Invalid sweep band [{band_lo}, {band_hi}] mm: need 0 <= sweep_x_min_mm < sweep_x_max_mm"
            )
        if band_hi - band_lo < SWEEP_BAND_MIN_WIDTH_MM:
            raise EjectGenerationError(
                f"Sweep band width {band_hi - band_lo} mm is below the {SWEEP_BAND_MIN_WIDTH_MM} mm minimum"
            )
        if band_hi > bed_x:
            raise EjectGenerationError(
                f"sweep_x_max_mm {band_hi} mm exceeds bed width {bed_x} mm for {geometry.model_key}"
            )
        lane_lo, lane_hi = band_lo, band_hi
    else:
        lane_lo, lane_hi = profile.x_margin_mm, bed_x - profile.x_margin_mm
    # Clamp the lane span into the travel envelope (silently — see the Y note
    # above). Fail-fast only if the clamp collapses the span to zero width.
    lane_lo = _clamp(lane_lo, x_min, x_max)
    lane_hi = _clamp(lane_hi, x_min, x_max)
    if lane_lo >= lane_hi:
        raise EjectGenerationError(
            f"Eject sweep degenerate after travel-envelope clamp: X span "
            f"[{lane_lo:g}, {lane_hi:g}] collapsed (envelope X [{x_min:g}, {x_max:g}])"
        )
    x_lanes = _linspace(lane_lo, lane_hi, profile.x_passes)

    # Top sweep level: begin the descent at a fraction of the part height, never
    # below the z_offset floor. sweep_start_frac defaults to 1.0 (part top); a
    # transient profile with the attribute unset is treated as 1.0.
    start_frac = profile.sweep_start_frac if profile.sweep_start_frac is not None else 1.0
    sweep_top = max(max_z_height * start_frac, profile.z_offset_mm)
    z_levels = _linspace(sweep_top, profile.z_offset_mm, profile.descent_steps)

    lines: list[str] = [block_start_marker(profile)]

    # --- prologue: re-engage after stock shutdown -------------------------
    # NEVER G28 (all axes) or G28 Z: Z-homing probes the bed centre where the part
    # still sits. The X/Y home is NOT emitted here either — it runs after the first Z
    # move has taken the plate to the sweep height, because a home emitted here would
    # run beside a plate the cooldown hold left at ~2 mm with the part standing ~48 mm
    # ABOVE the nozzle plane (negative clearance, guaranteed strike), and the home
    # direction does not help: Y homes rearward, but the hotend crosses the rear of the
    # plate while homing (operator eyewitness 2026-09-13). All this prologue does is
    # re-engage the motors, optionally re-reference Z, and set the absolute dialect
    # every coordinate below is read in.
    lines.append("; --- prologue: re-engage motors ---")
    lines.append("M17")
    # Contact-free Z re-reference, FIRST — before anything can move, because every
    # absolute Z below is read in the frame this declares. Empty for every model whose
    # ladder has not flipped ``z_reference_validated`` — i.e. everywhere, until one does.
    lines.extend(z_reference_prologue_lines(geometry))
    lines.append("G90")
    # Phase beacon consumed by the eject runtime watchdog, emitted BEFORE the first Z
    # move so the drop-span deadline covers that move (mc_percent is M73-driven and
    # resets to 0 at job start). Nothing between the block start and here moves, so the
    # estimator's ``pre_s`` is 0 for every block this generator emits.
    lines.append(f"{PHASE_BEACON_LIFTED} ; phase beacon: drop phase begins - eject runtime watchdog")

    # --- bed heater off, cooldown fans off --------------------------------
    # Command the bed heater off defensively. The cooldown WAIT is no longer in
    # the G-code — the eject monitor already held the plate gate until the live
    # bed reached cooldown_temp_c before dispatching this motion-only job — so no
    # M190 R loop is emitted here.
    #
    # The two fan lines stop the fans the cooldown prep runs during that wait: the
    # AUX fan (``M106 P2 S0``) and the CHAMBER exhaust fan (``M106 P3 S0``, the stock
    # end block's own line). The prep's own server-side OFF is best-effort (a restart
    # between the prep and this job orphans both fans with nothing left to switch them
    # off), and this file is the one writer that cannot be lost.
    #
    # The duct MODE (``M145``) is deliberately NOT touched here: the prep leaves the
    # duct in cooling exactly as the vendor's own finish tail does, and the next print's
    # start block sets the mode it wants. Note that the appended vendor epilogue's
    # ``M622 J2`` branch may itself re-run ``M145 P0`` / ``M106 P3 S127`` for 180 s
    # AFTER these lines on printers whose finish-filtration setting takes that branch —
    # vendor behaviour inside the vendor tail, not ours to suppress.
    #
    # Non-motion: both lines cost the drop span nothing.
    lines.append("; --- bed heater off, cooldown fans off ---")
    lines.append("M140 S0")
    lines.append("M106 P2 S0")
    lines.append("M106 P3 S0")

    # --- bed-drop release assist (optional) -------------------------------
    # Drive the bed all the way DOWN from the lift height to the machine bottom minus
    # the profile's clearance (bigger Z = bed farther from the nozzle), then return to
    # the lift height — a mechanical jolt to release a part the sweep alone can't
    # shift. NULL clearance = assist off. Preconditions are checked HERE, before a
    # single line is emitted, so a refusal costs no motion.
    bed_drop = profile.bed_drop_clearance_mm
    # Drop-FLOOR behaviours (both optional, NULL = off). Read defensively: a
    # transient profile built without these attributes still generates (mirrors the
    # sweep_start_frac / final_skim None handling below).
    dwell_s: int | None = getattr(profile, "bed_drop_dwell_s", None)
    jitter_cycles: int | None = getattr(profile, "bed_drop_jitter_cycles", None)
    jitter_mm: float | None = getattr(profile, "bed_drop_jitter_mm", None)
    drop_target: float | None = None
    # The lift height: where the sweep runs from, where the assist returns to, and the
    # validator's expected Z ceiling for a non-drop block.
    lift = lift_z(max_z_height, profile)
    if bed_drop is None and (dwell_s is not None or jitter_cycles is not None or jitter_mm is not None):
        # Both behaviours are motions AT the drop floor — without the drop there is
        # no floor. Fail closed instead of silently discarding configured motion.
        raise EjectGenerationError(
            "bed-drop dwell/jitter require the bed-drop release assist — set bed_drop_clearance_mm, "
            "or clear bed_drop_dwell_s / bed_drop_jitter_cycles / bed_drop_jitter_mm in this profile"
        )
    if bed_drop is not None:
        if is_bedslinger_model(geometry.model_key):
            # A bed-slinger's bed is fixed in Z (the gantry carries Z), so there is
            # no bed-on-Z travel to open a part↔nozzle gap — the drop is physically
            # meaningless and driving Z would move the TOOLHEAD toward the part.
            raise EjectGenerationError(
                f"bed-drop release assist is enabled but {geometry.model_key!r} is a bedslinger "
                "(bed does not move in Z) — disable bed_drop_clearance_mm in this profile or pick a bed-on-Z model"
            )
        if geometry.z_travel_mm is None:
            raise EjectGenerationError(
                f"bed-drop release assist is enabled but model {geometry.model_key!r} has no "
                "z_travel_mm — set it via PUT /model-geometry before ejecting with this profile"
            )
        # The ONE derivation, shared with the validator (:func:`drop_z`). Not None here:
        # the missing-z_travel_mm case is refused two lines above.
        drop_target = drop_z(profile, geometry)
        degenerate = degenerate_drop_error(max_z_height, profile, geometry)
        if degenerate is not None:
            raise EjectGenerationError(degenerate)
        if (jitter_cycles is None) != (jitter_mm is None):
            raise EjectGenerationError(
                "bed-drop jitter needs bed_drop_jitter_cycles and bed_drop_jitter_mm both set or both null"
            )
        if jitter_mm is not None and drop_target is not None and jitter_mm >= drop_target - lift:
            raise EjectGenerationError(
                f"bed-drop jitter {jitter_mm:g} mm reaches Z{drop_target - jitter_mm:g} from the drop target "
                f"Z{drop_target:g} — oscillation would cross the lift height Z{lift:g}"
            )

    # --- first Z move: to the SWEEP HEIGHT, from wherever the plate is -----
    # "Wherever" is either the vendor's parked ~Z123 after a stock shutdown or the
    # cooldown hold's ~Z2 (``cooldown_hold_lines``) — the block never assumes which.
    # It is UNCONDITIONAL and it always targets the lift height, assist or no assist:
    # this is the move that buys the home its clearance, and it is the last thing that
    # happens before the toolhead is allowed to move at all.
    lines.append(
        "; --- first Z move: to the sweep height, from wherever the plate is (vendor park or cooldown hold) ---"
    )
    lines.append(f"G1 Z{_fmt(lift)} F900")

    # --- home X/Y (never Z): at the sweep height, BEFORE the drop ----------
    # Placement rests on two independent facts, either of which alone decides it:
    #
    #   CLEARANCE — at the lift height the part top is exactly ``clearance_mm`` under
    #   the nozzle plane, the vendor's own precondition for crossing a loaded plate
    #   (the stock end block runs ``G150.3`` at ``max_layer_z + 10``; the fleet's
    #   clearance IS that 10) and the same gap this block's own sweep-entry transit and
    #   park traverse accept on every production eject. Homing BEFORE the first Z move
    #   would instead home beside a held plate with the part ~48 mm ABOVE the plane —
    #   negative clearance. Y homing rearward does not rescue that: the hotend crosses
    #   the REAR of the plate while homing (operator eyewitness 2026-09-13), so a
    #   rearward home is not self-clearing and Z-clearance is the whole question.
    #
    #   FRAME INTEGRITY — the drop below is the only move in the block that drives
    #   toward a possible obstruction (debris under the plate). A stall there loses
    #   steps with the plate ending HIGHER than the firmware believes, and every
    #   absolute Z after it runs in that corrupted frame (012-H2S 2026-07-31,
    #   gouged plate; see ``remote.EJECT_ABORT_MARGIN_FRAC``). ``G28 X``/``G28 Y`` are
    #   sensorless-stall homes, so a home run in that frame can read contact with the
    #   part as its endstop and corrupt X/Y too. Homing here keeps the home in the
    #   frame the print left — the only frame the block has evidence for.
    lines.append("; --- home X/Y (never Z): at the sweep height, clear of the part, before the drop ---")
    if is_dual_nozzle_model(geometry.model_key):
        # Dual-nozzle firmware stall-loops on unparameterized homing (see
        # DUAL_NOZZLE_HOME) — home X then Y with the stock parameterized forms.
        lines.extend(DUAL_NOZZLE_HOME)
    else:
        lines.append("G28 X Y")

    if drop_target is not None:
        # The bed-drop release assist: ONE block, so every floor behaviour and the
        # return out of the floor sit in one place. Round trip — down from the lift
        # height, jitter, dwell, back up to the lift height.
        lines.append(f"G1 Z{_fmt(drop_target)} F900")
        if jitter_cycles is not None and jitter_mm is not None:
            # Every stroke rises AWAY from the machine bottom first and returns to the
            # drop target, so no move passes it — the block's Z ceiling is unchanged
            # and the validator needs no new case.
            lines.append(f"; --- bed-drop jitter: {jitter_cycles} x {_fmt(jitter_mm)}mm at the drop floor ---")
            for _ in range(jitter_cycles):
                lines.append(f"G1 Z{_fmt(drop_target - jitter_mm)} F900")
                lines.append(f"G1 Z{_fmt(drop_target)} F900")
        if dwell_s is not None:
            # `M400 S<n>` (whole seconds) is the verified dwell dialect AND the only
            # form estimate_runtime_s counts; G4 is invisible to it, and the in-flight
            # abort watchdog consumes that estimate as its deadline. It is the LAST
            # thing that happens at the floor — nothing runs between it and the return.
            lines.append(f"; --- bed-drop dwell: hold {dwell_s}s at the floor to peel the part ---")
            lines.append(f"M400 S{dwell_s}")
        lines.append("; --- return to the sweep height ---")
        lines.append(f"G1 Z{_fmt(lift)} F900")

    # --- sweep: push the part off the FRONT (door side) -------------------
    # Phase beacon consumed by the eject runtime watchdog: ONE emission site, directly
    # above the sweep marker, so "percent below this beacon" always means the sweep has
    # not started — with the bed-drop assist on or off.
    lines.append(f"{PHASE_BEACON_SWEEP} ; phase beacon: sweep begins - eject runtime watchdog")
    lines.append(SWEEP_PHASE_MARKER)
    # Park behind the part (rear service area) at the first lane.
    lines.append(f"G1 X{_fmt(x_lanes[0])} Y{_fmt(back_y)} F9000")

    def sweep_level(z: float, feed: int) -> None:
        lines.append(f"G1 Z{_fmt(z)} F600")
        # Reset X to the first lane (moving along the rear, clear of the part).
        lines.append(f"G1 X{_fmt(x_lanes[0])} F9000")
        for i, _x in enumerate(x_lanes):
            lines.append(f"G1 Y{_fmt(front_y)} F{feed}")  # push through, off the front
            lines.append(f"G1 Y{_fmt(back_y)} F9000")  # return to the rear
            if i < len(x_lanes) - 1:
                lines.append(f"G1 X{_fmt(x_lanes[i + 1])} F9000")  # advance to next lane

    for z in z_levels:
        sweep_level(z, profile.eject_speed_mm_min)
    # Final slow skim right above the plate to clear thin remnants. Gated by the
    # profile's final_skim toggle: True (default, prior behaviour) appends the
    # skim; False pushes exactly once. A transient profile with the attribute
    # unset is treated as True (mirrors the sweep_start_frac None handling).
    final_skim = profile.final_skim if profile.final_skim is not None else True
    if final_skim:
        lines.append("; --- final skim ---")
        sweep_level(profile.z_offset_mm, profile.skim_speed_mm_min)

    # --- park centre at a part-clear Z ------------------------------------
    # The block's END STATE. Park the bed proportional to part height so a part
    # that survived the sweep stays clear of the nozzle: the same ``park_z`` the
    # cooldown hold transits at (the lift height, floored at PARK_Z_MM for a tiny part
    # with clearance 0). Drop the bed clear FIRST (toolhead still at the rear,
    # off the bed), THEN traverse to centre — never a low-Z diagonal across the
    # bed interior that would drag the nozzle through a surviving part.
    # Phase beacon consumed by the eject runtime watchdog: the sweep is over, so a
    # percent at/above it can never be read as a job still executing sweep lanes.
    lines.append(f"{PHASE_BEACON_PARK} ; phase beacon: sweep done - eject runtime watchdog")
    park = park_z(max_z_height, profile)
    park_x = _clamp(bed_x / 2, x_min, x_max)
    park_y = _clamp(bed_y / 2, y_min, y_max)
    lines.append(f"G1 Z{_fmt(park)} F900")
    lines.append(f"G1 X{_fmt(park_x)} Y{_fmt(park_y)} F9000")

    # --- completion epilogue ----------------------------------------------
    # Stock machine-end finish tail so this standalone motion-only file ends
    # FINISH, not FAILED-at-EOF (see COMPLETION_EPILOGUE). Emitted verbatim.
    lines.append("; --- completion epilogue: stock machine-end finish tail (job ends FINISH) ---")
    lines.append(COMPLETION_EPILOGUE)

    lines.append(BLOCK_END_MARKER)

    # The ONE place these numbers are recorded. The emitted G-code is never
    # persisted (the artifact is a temp file, the block only ever lived inside it),
    # so before this line the 2026-07-31 gouged-plate incident could only be
    # reconstructed by re-deriving the geometry through the preview endpoint. Every
    # figure a post-incident reader needs about what the machine was told to do —
    # the bed-drop target above all — is here, per built file. ``warn=`` carries the
    # zero-margin drop warning (:func:`bottom_target_warning`) so the built-file record
    # says the same thing the preview's ``warnings`` list does.
    logger.info(
        "eject.generator: built block profile=%r model=%s max_z=%smm z_ref=%s lift_z=%s drop_z=%s "
        "dwell=%s jitter=%s sweep_z=%s lanes=%d span=[%s, %s] warn=%s",
        profile.name,
        geometry.model_key,
        _fmt(max_z_height),
        "on" if geometry.z_reference_validated else "off",
        _fmt(lift),
        _fmt(drop_target) if drop_target is not None else "off",
        dwell_s if dwell_s is not None else "off",
        f"{jitter_cycles}x{_fmt(jitter_mm)}mm" if jitter_cycles is not None and jitter_mm is not None else "off",
        [_fmt(z) for z in z_levels],
        len(x_lanes),
        _fmt(lane_lo),
        _fmt(lane_hi),
        bottom_target_warning(profile, geometry) or "none",
    )
    return "\n".join(lines) + "\n"
