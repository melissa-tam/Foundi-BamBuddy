"""Eject G-code generator.

Produces the machine-end EJECT BLOCK for a given :class:`EjectProfile`, part
height and printer model. The block runs *after* the printer's stock shutdown
(bed dropped ~Z123, motors M18-disabled), so it re-engages the motors and homes
only X/Y before cooling the bed and sweeping the part off the front (door side).

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
primitive with soft end stops off, the stop is DECLARED as ``z_travel_mm``, and only
then does the XY re-engage run. It exists because the block's absolute Z moves rely on
a RETAINED Z datum that a power cycle destroys (002-H2S, 2026-09-04, eyewitnessed: the
bed drove past the Z floor). See :func:`z_reference_prologue_lines` for what each line
rests on and, in particular, which parts are proven and which are hypotheses the ladder
witnesses.

An optional ``bed_drop_clearance_mm`` (NULL = off) adds a mechanical release
assist after the bed heater is commanded off: the bed drives all the way DOWN
to the machine bottom minus that clearance (bigger Z = bed farther from the
nozzle), then returns to the lift height before the sweep runs — jolting a
stuck part loose without changing the sweep itself. The machine bottom is the
target model's ``z_travel_mm`` (from the geometry registry, never hardcoded);
a profile that enables the assist against a model with no ``z_travel_mm`` fails
closed. Two further tunings act AT that drop floor, emitted drop → jitter →
dwell → return: ``bed_drop_jitter_cycles``/``bed_drop_jitter_mm`` oscillate the
bed up-then-back (up FIRST, so no move passes the drop target) and
``bed_drop_dwell_s`` holds there for whole seconds as ``M400 S<n>``. Both are
NULL = off and both fail closed without the drop itself.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

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
PHASE_BEACON_LIFTED_PCT = 5  # prologue done, bed-drop phase begins
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


@dataclass(frozen=True)
class EjectRuntimeSegments:
    """One eject block's commanded time, split at all three M73 phase beacons.

    The split exists because the whole-job deadline can only catch a stall big enough
    to overrun the ENTIRE eject's margin. ``drop_span_s`` bounds the bed-drop phase by
    itself, which is the phase that stalls (2026-07-31 gouged plate, 2026-08-15 009-H2S)
    and the one that must be caught BEFORE the sweep touches the plate.

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

    pre_s: float  # motion before the M73 P5 beacon (prologue lift, unmeasurable Z aside)
    drop_span_s: float  # commanded time between the P5 and P50 beacons (motion + M400 S dwells)
    sweep_span_s: float  # commanded time between the P50 and P75 beacons (the sweep lanes)
    tail_s: float  # the P75 beacon onward (park + completion epilogue)
    total_s: float  # every span above + EJECT_RUNTIME_OVERHEAD_S
    reference_s: float | None = None  # block start → the M73 P2 beacon; None = no such phase


def estimate_runtime_s(gcode: str) -> float:
    """Expected wall-clock execution time (seconds) of an eject block.

    Total-only façade over :func:`estimate_runtime_segments` — the same single walk, so
    the two can never disagree about what the machine was told to do."""
    return estimate_runtime_segments(gcode).total_s


def estimate_runtime_segments(gcode: str) -> EjectRuntimeSegments:
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
      ``G28 X T300`` / ``G28 Y T300``) zeroes X and Y.** It never homes Z in an eject
      block (a part sits on the plate), so it leaves the Z position exactly as it found
      it — UNKNOWN unless the Z re-reference prologue declared it (below).
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
      absolute Z moves are measurable. Before it, and in a block without it, Z is
      unknown and the prologue's first ``G1 Z<lift> F900`` contributes 0 mm: real, but
      unmeasurable. It becomes known after that move either way, so the bed-drop pair
      that follows — the move that stalled in the 2026-07-31 under-bed-obstruction
      incident — IS fully counted in both dialects.
    * **A move on an axis with no known prior position contributes 0 mm.**
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
    pos: dict[str, float | None] = {"X": None, "Y": None, "Z": None}
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

    The block is a self-contained, self-completing eject-only job: prologue
    (re-engage + home X/Y), bed-heater off, the descending sweep + park, then the
    :data:`COMPLETION_EPILOGUE` (stock machine-end finish tail) so the standalone
    file ends FINISH rather than FAILED-at-EOF.

    The block's END STATE parks the bed at ``max(max_z_height + clearance_mm,
    PARK_Z_MM)`` (part height + clearance, floored at :data:`PARK_Z_MM`), Z
    before XY, so a part that survived the sweep sits clear of the nozzle.

    There is NO in-file cooldown wait: the bed-cooldown gate moved OUT of the
    G-code into the eject monitor, which holds the plate-clear gate until the live
    ``bed_temper`` reaches the profile's ``cooldown_temp_c`` and only THEN dispatches
    this motion-only job. ``M140 S0`` (heater off) is still emitted defensively; the
    old ``M106``/``M190 R`` thermal block is gone.

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

    if max_z_height > profile.max_part_height_mm:
        raise EjectGenerationError(
            f"Part height {max_z_height} mm exceeds profile max_part_height_mm "
            f"{profile.max_part_height_mm} mm — refusing to generate eject block"
        )

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
    # NEVER G28 (all axes) or G28 Z: Z-homing probes the bed centre where the
    # part still sits. Home X/Y only, then lift the bed clear of the part.
    lines.append("; --- prologue: re-engage motors, home X/Y (never Z) ---")
    lines.append("M17")
    # Contact-free Z re-reference, BEFORE the XY re-engage: the drive ends with the bed
    # at its bottom stop, so the toolhead's homing travel afterwards is as clear of the
    # part as it can possibly be. Empty for every model whose ladder has not flipped
    # ``z_reference_validated`` — i.e. everywhere, until one does.
    lines.extend(z_reference_prologue_lines(geometry))
    if is_dual_nozzle_model(geometry.model_key):
        # Dual-nozzle firmware stall-loops on unparameterized homing (see
        # DUAL_NOZZLE_HOME) — home X then Y with the stock parameterized forms.
        lines.extend(DUAL_NOZZLE_HOME)
    else:
        lines.append("G28 X Y")
    lines.append("G90")
    # Lift the bed clear of the part; reused as the return height of the optional
    # bed-drop assist and as the validator's expected Z ceiling for a non-drop block.
    lift_z = max_z_height + profile.clearance_mm
    lines.append(f"G1 Z{_fmt(lift_z)} F900")
    # Phase beacon consumed by the eject runtime watchdog: the edge it times the
    # bed-drop span from (mc_percent is M73-driven and resets to 0 at job start).
    lines.append(f"{PHASE_BEACON_LIFTED} ; phase beacon: prologue done - eject runtime watchdog")

    # --- bed heater off ---------------------------------------------------
    # Command the bed heater off defensively. The cooldown WAIT is no longer in
    # the G-code — the eject monitor already held the plate gate until the live
    # bed reached cooldown_temp_c before dispatching this motion-only job — so no
    # fan / M190 R loop is emitted here.
    lines.append("; --- bed heater off ---")
    lines.append("M140 S0")

    # --- bed-drop release assist (optional) -------------------------------
    # Drive the bed all the way DOWN to the machine bottom minus the profile's
    # clearance (bigger Z = bed farther from the nozzle), then return to the lift
    # height — a mechanical jolt to release a part the sweep alone can't shift.
    # NULL clearance = assist off (the 5 golden fixtures stay byte-identical).
    bed_drop = profile.bed_drop_clearance_mm
    # Drop-FLOOR behaviours (both optional, NULL = off). Read defensively: a
    # transient profile built without these attributes still generates (mirrors the
    # sweep_start_frac / final_skim None handling below).
    dwell_s: int | None = getattr(profile, "bed_drop_dwell_s", None)
    jitter_cycles: int | None = getattr(profile, "bed_drop_jitter_cycles", None)
    jitter_mm: float | None = getattr(profile, "bed_drop_jitter_mm", None)
    drop_z: float | None = None
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
        drop_z = geometry.z_travel_mm - bed_drop
        if drop_z <= lift_z:
            raise EjectGenerationError(
                f"bed-drop target Z{drop_z:g} (z_travel {geometry.z_travel_mm:g} - clearance "
                f"{bed_drop:g}) is not below the lift height Z{lift_z:g} — degenerate drop"
            )
        if (jitter_cycles is None) != (jitter_mm is None):
            raise EjectGenerationError(
                "bed-drop jitter needs bed_drop_jitter_cycles and bed_drop_jitter_mm both set or both null"
            )
        if jitter_mm is not None and jitter_mm >= drop_z - lift_z:
            raise EjectGenerationError(
                f"bed-drop jitter {jitter_mm:g} mm reaches Z{drop_z - jitter_mm:g} from the drop target "
                f"Z{drop_z:g} — oscillation would cross the lift height Z{lift_z:g}"
            )
        lines.append("; --- bed-drop release assist: full down + return ---")
        lines.append(f"G1 Z{_fmt(drop_z)} F900")
        if jitter_cycles is not None and jitter_mm is not None:
            # Every stroke rises AWAY from the machine bottom first and returns to the
            # drop target, so no move passes drop_z — the block's Z ceiling is
            # unchanged and the validator needs no new case.
            lines.append(f"; --- bed-drop jitter: {jitter_cycles} x {_fmt(jitter_mm)}mm at the drop floor ---")
            for _ in range(jitter_cycles):
                lines.append(f"G1 Z{_fmt(drop_z - jitter_mm)} F900")
                lines.append(f"G1 Z{_fmt(drop_z)} F900")
        if dwell_s is not None:
            # `M400 S<n>` (whole seconds) is the verified dwell dialect AND the only
            # form estimate_runtime_s counts; G4 is invisible to it, and the in-flight
            # abort watchdog consumes that estimate as its deadline.
            lines.append(f"; --- bed-drop dwell: hold {dwell_s}s at the floor to peel the part ---")
            lines.append(f"M400 S{dwell_s}")
        lines.append(f"G1 Z{_fmt(lift_z)} F900")

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
    # that survived the sweep stays clear of the nozzle: reuse the prologue lift
    # height (``max_z_height + clearance_mm``), floored at PARK_Z_MM for a tiny
    # part with clearance 0. Drop the bed clear FIRST (toolhead still at the rear,
    # off the bed), THEN traverse to centre — never a low-Z diagonal across the
    # bed interior that would drag the nozzle through a surviving part.
    # Phase beacon consumed by the eject runtime watchdog: the sweep is over, so a
    # percent at/above it can never be read as a job still executing sweep lanes.
    lines.append(f"{PHASE_BEACON_PARK} ; phase beacon: sweep done - eject runtime watchdog")
    park_z = max(lift_z, PARK_Z_MM)
    park_x = _clamp(bed_x / 2, x_min, x_max)
    park_y = _clamp(bed_y / 2, y_min, y_max)
    lines.append(f"G1 Z{_fmt(park_z)} F900")
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
    # the bed-drop target above all — is here, per built file.
    logger.info(
        "eject.generator: built block profile=%r model=%s max_z=%smm z_ref=%s lift_z=%s drop_z=%s "
        "dwell=%s jitter=%s sweep_z=%s lanes=%d span=[%s, %s]",
        profile.name,
        geometry.model_key,
        _fmt(max_z_height),
        "on" if geometry.z_reference_validated else "off",
        _fmt(lift_z),
        _fmt(drop_z) if drop_z is not None else "off",
        dwell_s if dwell_s is not None else "off",
        f"{jitter_cycles}x{_fmt(jitter_mm)}mm" if jitter_cycles is not None and jitter_mm is not None else "off",
        [_fmt(z) for z in z_levels],
        len(x_lanes),
        _fmt(lane_lo),
        _fmt(lane_hi),
    )
    return "\n".join(lines) + "\n"
