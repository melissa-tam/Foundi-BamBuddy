"""THE read-time verdict: what one printer's condition at one instant MEANS.

The observation log records orthogonal facts and ranks none of them
(:mod:`backend.app.services.fleet_activity`); the incident store records durable
holds. This module is the one place those two evidences are folded into a single
answer — *printing / between prints / idle / down / planned / out of fleet* — and it
is the same function for a stretch of last August and for the live tile, so a printer
can never read one way on the dashboard and another way in its own history.

**The absence of evidence is an INPUT, not a branch.** A caller that has no
observation for an instant says WHY with :data:`NO_SPAN_YET` (nothing was ever
recorded for this printer before that point) or :data:`OBSERVATION_GAP` (recording
had started and this stretch is a hole in it). Both are values of ``seen``, so
"recording began at T" never has to be known here, and the same evidence answers the
same on either side of it.

**A durable hold outranks a missing sample.** A fault or a declared hold is read from
the incident ledger, which no recorder outage can erase, so rules 4 and 5 fire under
both absences: a printer that was broken through a controller restart was broken, and
reporting that stretch as merely unobserved would erase exactly the downtime the
ledger proves. What the ledger cannot do is overrule an OBSERVED physical process —
a print running under a maintenance hold is real output, and a sweep is happening
whatever the ledger says — which is why rules 2 and 3 come first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cache, lru_cache

from backend.app.models.printer_incident import (
    DECLARED_KINDS,
    FAULT_KINDS,
    KIND_PRECEDENCE,
)
from backend.app.models.printer_observation_span import (
    PLATE_PHASE_COOLING,
    PLATE_PHASE_EJECTING,
    PLATE_PHASE_HELD,
)
from backend.app.services.fleet_activity import Observation
from backend.app.services.plate_occupancy import ACTIVE_PRINT_STATES

# ── the class vocabulary ────────────────────────────────────────────────────────────
#
# GROUP is the coarse answer every headline figure is stated in (the five the operator
# reads as printers: printing, between prints, idle, down, in maintenance — plus the
# three that say a printer is not being counted at all). CAUSE is the detail under it,
# and it exists only where a group has more than one way to be true.
GROUP_PRINTING = "printing"
GROUP_CYCLE_OVERHEAD = "cycle_overhead"  # the farm's own between-prints work
GROUP_IDLE = "idle"
GROUP_DOWN = "down"  # unplanned: the printer cannot take work
GROUP_PLANNED = "planned"  # a human declared it out of the automatic lanes
GROUP_OUT_OF_FLEET = "out_of_fleet"  # deactivated, or deleted: not part of the fleet
GROUP_NOT_RECORDED = "not_recorded"  # before this printer's first observation
GROUP_UNOBSERVED = "unobserved"  # a hole in the record after recording began

#: Every group, in the order a stacked chart reads bottom-to-top.
GROUPS: tuple[str, ...] = (
    GROUP_PRINTING,
    GROUP_CYCLE_OVERHEAD,
    GROUP_IDLE,
    GROUP_PLANNED,
    GROUP_DOWN,
    GROUP_OUT_OF_FLEET,
    GROUP_NOT_RECORDED,
    GROUP_UNOBSERVED,
)

CAUSE_OFFLINE = "offline"
CAUSE_PAUSED = "paused"
CAUSE_QUARANTINED = "quarantined"
CAUSE_PLATE_HELD = "plate_held"
CAUSE_MODEL_MISMATCH = "model_mismatch"
CAUSE_NO_USB = "no_usb"
# An equipment fault names its KIND after this prefix (``fault:jam``), so one string
# test tells a fault cause from the six condition causes above and the kind travels
# with it — the UI renders the incident's own label from it.
CAUSE_FAULT_PREFIX = "fault:"

# The printer's own state word is not the farm's vocabulary, so the one set that says
# "a job is on the platen" is DERIVED from the occupancy domain's set rather than
# re-spelled here: a second spelling is how two lanes come to disagree about PAUSE.
# PAUSE is subtracted because a paused print is a printer waiting on somebody — the
# whole point of the down/printing split — while PREPARE and SLICING are the machine
# working on the job it was given.
PAUSE_STATE = "PAUSE"
PRINTING_STATES: frozenset[str] = ACTIVE_PRINT_STATES - {PAUSE_STATE}

# The two plate phases that ARE the farm's between-prints work. ``held`` is deliberately
# not here: a plate nobody has cleared is waiting on a person, which is downtime.
_CYCLE_PHASES: frozenset[str] = frozenset({PLATE_PHASE_COOLING, PLATE_PHASE_EJECTING})

# Where each kind sits when a printer carries several open faults. Read from the
# store's own order so the class a metrics sweep names and the chip the printer card
# shows can never disagree about which fault is THE fault.
_FAULT_RANK: dict[str, int] = {kind: rank for rank, kind in enumerate(KIND_PRECEDENCE)}


@dataclass(frozen=True, slots=True)
class AvailabilityClass:
    """One verdict: a group, an optional cause, and the stable key both project to.

    ``key`` is derived in ``__post_init__`` rather than passed, so a group/cause pair
    has exactly one spelling on the wire and no caller can mint an inconsistent one.
    Instances are interned (:func:`availability_class`), so identity comparison is
    valid and a sweep over a year of spans allocates nothing.
    """

    group: str
    cause: str | None
    key: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", self.group if self.cause is None else f"{self.group}:{self.cause}")

    @property
    def is_down(self) -> bool:
        """Is this class UNPLANNED downtime? (The one test every down figure uses.)"""
        return self.group == GROUP_DOWN


@cache
def _klass(group: str, cause: str | None = None) -> AvailabilityClass:
    """The interned instance for ``(group, cause)``. The ONLY constructor.

    Unbounded on purpose: the vocabulary is closed — eight groups, six condition
    causes and one cause per registered incident kind — so the cache is a handful of
    objects that live as long as the process, not a leak.
    """
    return AvailabilityClass(group=group, cause=cause)


PRINTING = _klass(GROUP_PRINTING)
CYCLE_COOLING = _klass(GROUP_CYCLE_OVERHEAD, PLATE_PHASE_COOLING)
CYCLE_EJECTING = _klass(GROUP_CYCLE_OVERHEAD, PLATE_PHASE_EJECTING)
IDLE = _klass(GROUP_IDLE)
DOWN_OFFLINE = _klass(GROUP_DOWN, CAUSE_OFFLINE)
DOWN_PAUSED = _klass(GROUP_DOWN, CAUSE_PAUSED)
DOWN_QUARANTINED = _klass(GROUP_DOWN, CAUSE_QUARANTINED)
DOWN_PLATE_HELD = _klass(GROUP_DOWN, CAUSE_PLATE_HELD)
DOWN_MODEL_MISMATCH = _klass(GROUP_DOWN, CAUSE_MODEL_MISMATCH)
DOWN_NO_USB = _klass(GROUP_DOWN, CAUSE_NO_USB)
PLANNED = _klass(GROUP_PLANNED)
OUT_OF_FLEET = _klass(GROUP_OUT_OF_FLEET)
NOT_RECORDED = _klass(GROUP_NOT_RECORDED)
UNOBSERVED = _klass(GROUP_UNOBSERVED)

# phase -> the between-prints class it names. A table rather than a branch, so the
# recorder's plate axis and this vocabulary are registered against each other.
_CYCLE_CLASS_BY_PHASE: dict[str, AvailabilityClass] = {
    PLATE_PHASE_COOLING: CYCLE_COOLING,
    PLATE_PHASE_EJECTING: CYCLE_EJECTING,
}


def down_fault(kind: str) -> AvailabilityClass:
    """The down class for an open equipment fault of ``kind``. Interned."""
    return _klass(GROUP_DOWN, f"{CAUSE_FAULT_PREFIX}{kind}")


def fault_kind_of(klass: AvailabilityClass) -> str | None:
    """The incident kind behind a ``down:fault:*`` class, or ``None`` for any other."""
    cause = klass.cause
    if klass.group != GROUP_DOWN or cause is None or not cause.startswith(CAUSE_FAULT_PREFIX):
        return None
    return cause[len(CAUSE_FAULT_PREFIX) :]


@dataclass(frozen=True, slots=True)
class NoSpanYet:
    """No observation had EVER been recorded for this printer at this instant.

    A distinct value from :class:`ObservationGap` because the two are different facts
    about the farm, not two shades of the same one: this one says the instrument did
    not exist yet, which is why a window before the recorder shipped reports honestly
    instead of reporting a fleet that was never down.
    """


@dataclass(frozen=True, slots=True)
class ObservationGap:
    """Recording had begun and this stretch is a HOLE in it.

    A controller restart, a sleeping host or a stalled loop; the recorder deliberately
    leaves the hole rather than extending a span across it, so this is what the hole
    reads as.
    """


#: The singletons — both are empty value objects, so one instance each is the whole set.
NO_SPAN_YET = NoSpanYet()
OBSERVATION_GAP = ObservationGap()

#: What a caller can know about one printer at one instant.
Seen = Observation | NoSpanYet | ObservationGap


@lru_cache(maxsize=512)
def _worst_fault(open_kinds: frozenset[str]) -> str | None:
    """The fault kind that names this printer's downtime, or ``None`` if none is open.

    Cached on the kind SET rather than resolved per interval: a sweep asks this once
    per elementary interval — order 10^6 times for a year of a twelve-printer fleet —
    and a printer carries at most a handful of distinct kind sets in a window.
    """
    faults = open_kinds & FAULT_KINDS
    if not faults:
        return None
    return min(faults, key=lambda kind: _FAULT_RANK.get(kind, len(KIND_PRECEDENCE)))


@lru_cache(maxsize=8192)
def availability_class(seen: Seen, open_kinds: frozenset[str]) -> AvailabilityClass:
    """Fold one instant's evidence into one class. Pure, total, first match wins.

    The order IS the definition of *down*, and it is stated once here:

    1. an observation of a DEACTIVATED printer — the operator withdrew the machine, so
       nothing about its condition is a fact about the fleet;
    2. connected and running a job, unless a sweep owns the platen;
    3. connected and cooling or ejecting — the farm's own between-prints work;
    4. an open equipment FAULT, by the store's precedence (also under either absence);
    5. an open DECLARED hold — planned, not down (also under either absence);
    6. / 7. no evidence at all, reported as the reason it is missing;
    8.–13. the observed conditions that stop work, worst first;
    14. otherwise the printer is available and doing nothing: idle.

    Results are interned and the call is memoised on ``(seen, open_kinds)`` — both
    arguments are frozen value objects, and a run-length-compressed log presents the
    same handful of tuples over and over.
    """
    observation = seen if isinstance(seen, Observation) else None

    if observation is not None:
        if not observation.is_active:
            return OUT_OF_FLEET
        if observation.connected:
            # An ejecting platen is not printing even when the wire still says RUNNING:
            # the sweep IS a job, and counting it as output would credit the farm with
            # a print for every part it pushed off.
            if observation.gcode_state in PRINTING_STATES and observation.plate_phase != PLATE_PHASE_EJECTING:
                return PRINTING
            if observation.plate_phase in _CYCLE_PHASES:
                return _CYCLE_CLASS_BY_PHASE[observation.plate_phase]

    kind = _worst_fault(open_kinds)
    if kind is not None:
        return down_fault(kind)
    # FAULT_KINDS and DECLARED_KINDS partition the store's whole vocabulary by
    # subtraction, so these two rules between them read every open row, and a kind
    # registered later joins one of them without an edit here.
    if open_kinds & DECLARED_KINDS:
        return PLANNED

    if observation is None:
        return NOT_RECORDED if isinstance(seen, NoSpanYet) else UNOBSERVED

    if not observation.connected:
        return DOWN_OFFLINE
    if observation.gcode_state == PAUSE_STATE:
        return DOWN_PAUSED
    if observation.quarantined:
        return DOWN_QUARANTINED
    if observation.plate_phase == PLATE_PHASE_HELD:
        return DOWN_PLATE_HELD
    if observation.model_mismatch:
        return DOWN_MODEL_MISMATCH
    # ``is False`` and not a truth test: NULL means the controller could not tell, and
    # "we could not tell" must never be charged to the printer as a missing drive.
    if observation.usb_present is False:
        return DOWN_NO_USB
    return IDLE
