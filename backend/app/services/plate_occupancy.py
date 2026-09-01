"""The ONE authority for "who owns this printer, and what is on its plate?" (WS1).

Until this module, that question had FIVE independent answers — the plate-clear
flag (5 raise sites, 8 clear sites), the pending-eject dict, the scheduler's
dispatch holds, the per-tick DB busy set, and the monitor's watch registry —
coupled only by ad-hoc cross-checks. Every interlock wave since 2026-08-25 added
another cross-check, and the night of 2026-08-29 → 08-30 cashed four of the seams
in one cascade across printers 1-6:

* prints that survived a restart re-attached with their layer/progress peaks lost
  (peaks live in process memory), so their genuine ``FINISH/completed`` terminals
  classified as "no-deposit — not gating queue": no gate, no eject, the unit
  recorded ``cancelled`` though it physically completed, and the next unit
  dispatched 1-5 s later onto the finished part;
* gates that survived the reboot re-armed escalation-only, and the operator's only
  way out dead-ended;
* the clear-plate → eject workaround lost a 1-3 s race to the scheduler, sweeping
  onto already-starting prints, which the firmware silently ignores — the pending
  ejects stayed registered forever and every later eject 409'd ``eject_in_flight``
  (8 consecutive on printer 4, 01:46-01:49);
* an operator ``declare_occupied`` on printer 4 at 01:06:57 landed between the
  scheduler's claim (01:06:56.8) and its ``start_print`` (01:06:59.8) and was erased
  by an unconditional gate clear on the dispatch path.

Every one of those is a seam, not a bug in any single store. So the stores collapse
into one record per printer and one set of transitions over it.

**Store only what neither the wire nor the DB can see, plus the durable plate.**

    OccupancyRecord(
      plate: PlateOccupied | None,   # a deposit is on the plate, plus what happens next
      lease: DispatchLease | None,   # a unit dispatch DECIDED but not yet settled on the wire
      eject: PendingEject | None,    # an eject dispatched, until its terminal
    )

Everything else is **evidence the caller supplies** and is never stored, never
hydrated: :class:`Evidence` carries ``live_state`` (the wire) and ``db_claim`` (a
``print_queue.status='printing'`` row, the scheduler's per-tick query). Keeping the
derived facts derived is what stops a sixth store from growing here.

**I/O-free by contract.** Stdlib only: no session, no models, no ``await``, no event
loop, no imports from ``backend.app``. Every transition is a synchronous pure
mutation of the in-memory record plus one fan-out. If this module ever needs an
``await``, the design is wrong — the I/O half is ``plate_occupancy_store`` and the
four side effects are injected through :meth:`PlateOccupancy.configure`.

**One error model.** Every transition returns a :data:`TransitionRefusal` (a closed
``Literal``) or ``None``; nothing raises for a refusal, and there is no English in
this module. Controllers own the reason → copy map — the 2026-08-20 ``slot_recheck``
precedent, where a verdict token and its sentence are deliberately different layers.

**Concurrency contract.** Transitions are synchronous and run on the single asyncio
thread, so two transitions can never interleave; the only re-entrancy possible is a
nested transition called from INSIDE one of the injected callables (the monitor's
release path reaching ``claim_for_eject``, say). A per-printer guard DEFERS such a
nested fan-out until the outer one drains, so ``kick`` can never read a record that
changed under it. The record itself is already consistent when the nested call runs
— only the notification is deferred, never the mutation.

**Lease settlement is a read-time function, never a transition.** A committed lease
counts as in-flight until the wire moves past its pre-dispatch snapshot and
``min_hold_s`` has elapsed, or ``max_hold_s`` has elapsed outright — exactly the
self-expiring shape of the scheduler's old ``_printer_in_dispatch_hold``. Making a
settle a transition would fan persist/broadcast/kick out for a non-event, on a tick
that returns early on an empty queue, so an idle queue could strand a lease past its
own ceiling.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Literal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

# Bambu firmware states that mean a project_file has been accepted and the printer
# is now processing / running / paused mid-print. SLICING is included because some
# firmwares park briefly between PREPARE and RUNNING while parsing the g-code.
#
# This set lives HERE because the occupancy domain owns "what counts as an active
# job": it is the JOB owner in the ownership projection, the ``job_active`` refusal
# on both the dispatch and the eject side, and the transition test the dispatch
# lease settles against. It was previously ``print_scheduler.ACTIVE_PRINT_STATES``
# (public since 2026-08-29 so ``farm_stall`` could ask the same question); the
# scheduler re-exports it from here, because a second spelling is how two lanes come
# to disagree about PAUSE — which is the difference between leaving a native-vision
# hold alone and double-dispatching onto an occupied plate.
ACTIVE_PRINT_STATES: frozenset[str] = frozenset({"PREPARE", "SLICING", "RUNNING", "PAUSE"})


TransitionRefusal = Literal[
    # The plate carries a deposit; nothing may be dispatched onto it.
    "plate_occupied",
    # The plate is clear (or was never declared) and the caller needed it occupied.
    "not_occupied",
    # A unit dispatch is decided-but-unsettled on this printer.
    "dispatch_in_flight",
    # The wire says a job is running / paused / preparing.
    "job_active",
    # An eject sweep owns this printer.
    "eject_in_flight",
    # A transition that acts on the pending eject found none.
    "no_eject",
    # The lease was revoked (an operator declared the plate occupied under it).
    "lease_revoked",
    # The lease offered is not the one this printer holds.
    "lease_unknown",
]

# Who holds the printer. A projection, never stored — see :class:`OccupancyView`.
OccupancyOwner = Literal["none", "dispatch", "job", "eject"]

# Why the record changed. Closed on purpose: the injected callables branch on it
# (``kick`` maps it to a scheduler reason, ``policy_driver`` to a watch), so the set
# is a public contract — add to it deliberately, never silently.
NotifyCause = Literal[
    "hydrate",
    "claim_for_dispatch",
    "commit_dispatch",
    "release_dispatch",
    "terminal",
    "plate_detected",
    "declare_occupied",
    "clear_plate",
    "operator_recover",
    "claim_for_eject",
    "eject_started",
    "eject_runtime_exceeded",
    "eject_completed",
    "eject_unverified",
    "eject_never_started",
    "drop_hydrated_eject",
    "set_policy",
]

# The one cause whose fan-out must not echo back to the DB it was just read from.
HYDRATE_CAUSE: NotifyCause = "hydrate"

EjectPurpose = Literal["fa", "production", "manual"]

EjectOutcome = Literal["completed", "unverified"]


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    """Derived facts the CALLER supplies; this module never stores or hydrates them.

    ``live_state`` is the wire's ``gcode_state``; a value in
    :data:`ACTIVE_PRINT_STATES` means a job owns the printer.

    ``db_claim`` is "a ``print_queue`` row on this printer reads ``printing``", and
    it **is consulted by :meth:`PlateOccupancy.dispatchable` ONLY**. It exists to
    stop a double dispatch during the IDLE→RUNNING lag, where the row is the only
    witness that a unit is already on its way. The eject-side checks
    (:meth:`PlateOccupancy.ejectable`, :meth:`PlateOccupancy.claim_for_eject`)
    derive ``dispatch_in_flight`` from the unrevoked LEASE alone, because a
    ``printing`` row on an IDLE printer is the 2026-08-29 dead-claim class — item
    1010 on 001-H2S sat ``printing`` for 15 h behind a print that never started, and
    the watchdog releases such a row only after 600 s of age plus a 120 s dwell.
    Deriving an eject refusal from it would lock the operator out of a provably idle
    plate for ≥12 minutes, which is strictly worse than the behaviour it replaces
    (the manual eject lane never read the queue row at all).
    """

    live_state: str | None = None
    db_claim: bool = False


@dataclass(frozen=True)
class DepositEvidence:
    """Did this terminal job leave something on the plate?

    The 2026-08-29 restart cascade is the whole reason ``peaks_reliable`` exists.
    ``last_layer_num`` / ``last_progress`` are peaks tracked in the MQTT client's
    PROCESS MEMORY; a client born mid-print (a redeploy, a host reboot) re-tracks
    from an unknown baseline and reports zeros for a print that is physically
    three-quarters done. Six such prints finished ``completed`` that night, each
    read as "produced zero layers → nothing on the plate", so no gate went up, no
    eject was armed, and the next unit dispatched onto the finished part 1-5 s
    later. Unknown peaks therefore **fail closed**: absence of measurement is not
    measurement of absence.

    The three rules, in order:

    * a dry run never deposits (the eject dry-run file is motion-only, by design);
    * a ``completed`` print deposited its part — peaks are irrelevant to a job the
      printer itself says it finished;
    * unknown peaks are treated as a deposit;

    and only then is the MEASURED zero (zero layers AND zero progress) allowed to
    say "nothing was left behind".
    """

    final_status: str
    is_dry_run: bool
    peaks_reliable: bool
    last_layer_num: int | None
    last_progress: float | None

    @property
    def deposited(self) -> bool:
        """True when the plate must be assumed to carry a part after this terminal."""
        if self.is_dry_run:
            return False
        if self.final_status == "completed":
            return True
        if not self.peaks_reliable:
            return True
        return not ((self.last_layer_num or 0) == 0 and (self.last_progress or 0) == 0)

    @classmethod
    def from_terminal_payload(cls, data: Mapping[str, Any], *, is_dry_run: bool) -> DepositEvidence:
        """Read the evidence out of the MQTT client's ``on_print_complete`` payload.

        ``peaks_reliable`` defaults to **False** when the key is absent: an older
        client, a virtual printer that has not caught up, or any payload shaped
        before that key existed must all land on the fail-closed side.
        """
        return cls(
            final_status=str(data.get("status", "completed")),
            is_dry_run=bool(is_dry_run),
            peaks_reliable=bool(data.get("peaks_reliable", False)),
            last_layer_num=data.get("last_layer_num"),
            last_progress=data.get("last_progress"),
        )

    @classmethod
    def unknown(cls, final_status: str) -> DepositEvidence:
        """Evidence for a terminal nobody observed — a downtime reconcile's synthesis.

        There are no peaks to be reliable about, so this is the fail-closed form by
        construction: anything but a dry run deposits.
        """
        return cls(
            final_status=final_status,
            is_dry_run=False,
            peaks_reliable=False,
            last_layer_num=None,
            last_progress=None,
        )


@dataclass(frozen=True)
class CooldownEject:
    """The production loop owns this plate: wait for the bed, then sweep the unit off."""

    unit_id: int
    run_id: int | None


@dataclass(frozen=True)
class FirstArticleEject:
    """A first article is on the plate: the sweep waits on an operator approval."""

    unit_id: int
    run_id: int | None


@dataclass(frozen=True)
class ForeignAutoEject:
    """A positively-identified farm-own file the farm did not dispatch: cooldown, then sweep."""

    profile_id: int
    threshold_c: float


@dataclass(frozen=True)
class EscalationOnly:
    """Nothing can be swept automatically; a human must clear the plate.

    The never-armless floor from 2026-07-18/07-21 and the repair target when a
    policy fails to arm: an occupied plate is ALWAYS attached to some policy, and
    when the farm cannot verify what should happen next it escalates rather than
    going quiet.
    """


OccupancyPolicy = CooldownEject | FirstArticleEject | ForeignAutoEject | EscalationOnly


@dataclass(frozen=True)
class PendingEject:
    """An in-flight server-dispatched eject awaiting its terminal status.

    Moved here from ``services/eject/remote.py`` with its semantics intact: it is
    the eject lane's claim on the printer, and it is what the terminal handler
    matches a job's echoed ``subtask_id`` against. The plate gate is NOT cleared
    when this is stored — it drops only when the eject's terminal is positively
    matched (:meth:`PlateOccupancy.resolve_eject`).

    ``expected_runtime_s`` (from the build) and ``started_at`` (stamped when the
    printer echoes the sweep's START) are what the in-flight runtime watchdog arms
    on. ``drop_span_s``, ``sweep_span_s`` and ``tail_s`` (also from the build) are the
    per-phase budgets that arm the watchdog's edge lane, which bounds each phase on its
    own rather than the whole job.

    ``runtime_exceeded_at`` is that watchdog's verdict, and the watchdog is the ONE
    authority on eject runtime: the mark is stamped the moment a deadline passes,
    BEFORE the stop is even sent, so a terminal racing in must already see it. The
    terminal handler only HONORS the mark and never re-computes a runtime judgement
    of its own — the machine cannot be stopped on one criterion and judged on
    another (the 2026-07-31 gouged-plate ordering).

    ``hydrated`` marks a record rebuilt at startup from the queue unit's
    ``eject_dispatched_at`` stamp rather than minted by a live dispatch. Such a
    record is None on ``expected_runtime_s``, ``started_at`` and every phase budget by
    construction — the durable mirror is a single timestamp column, not the built
    artifact — so no watchdog can arm and the farm has already admitted it cannot
    verify the sweep. That is why an operator eject SUPERSEDES a hydrated pending
    instead of being refused by it (see :meth:`PlateOccupancy.claim_for_eject`).
    """

    purpose: EjectPurpose
    run_id: int | None
    queue_item_id: int | None
    expected_runtime_s: float | None = None
    started_at: datetime | None = None
    runtime_exceeded_at: datetime | None = None
    drop_span_s: float | None = None
    sweep_span_s: float | None = None
    tail_s: float | None = None
    dispatched_at: datetime | None = None
    hydrated: bool = False


@dataclass(frozen=True)
class EjectIdentity:
    """What the runtime watchdog and terminal matching compare against.

    A projection, not the record: the watchdog re-checks identity before it stops a
    job, and it must compare against the eject THIS printer holds NOW — never the
    one it captured when it armed. ``runtime_exceeded_at`` rides along because the
    terminal handler's whole job is to HONOR that mark, and it must be able to read
    the verdict without reaching into the record.
    """

    purpose: EjectPurpose
    queue_item_id: int | None
    started_at: datetime | None
    dispatched_at: datetime | None
    hydrated: bool
    runtime_exceeded_at: datetime | None


@dataclass
class DispatchLease:
    """A unit dispatch DECIDED but not yet settled on the wire.

    Minted at PLAN time (before the upload) and committed just before the print
    command goes out; the window between the two is the seconds-long gap the
    2026-08-30 01:06:57 race lived in. It is mutable on exactly two flags —
    ``committed_at_mono`` and ``revoked`` — because a lease is an identity the
    scheduler holds across an await and hands back to :meth:`commit_dispatch`; a
    frozen copy would break that ``is`` check, which is the whole point of
    ``lease_unknown``.

    The hold clock is ``time.monotonic`` (never wall time — an NTP step must not
    settle or extend a lease) and runs from ``committed_at_mono``: upload duration
    is not echo lag and must not be charged against the hold window.
    """

    unit_id: int
    pre_state: str | None
    pre_subtask: str | None
    min_hold_s: float
    max_hold_s: float
    minted_at_mono: float
    committed_at_mono: float | None = None
    revoked: bool = False


@dataclass
class PlateOccupied:
    """A deposit is on the plate, and ``policy`` is what happens to it next.

    Mutable on ``policy`` alone, and deliberately so: the notify's policy repair
    (see :meth:`PlateOccupancy._fan_out`) rewrites it to :class:`EscalationOnly`
    IN PLACE, because a repair must not re-enter the transition machinery it is
    running inside.
    """

    source_subtask_id: str | None
    policy: OccupancyPolicy
    since: datetime


@dataclass(frozen=True)
class TerminalDisposition:
    """A terminal job, already classified, handed to the authority as one value.

    Built by ``farm_correlation`` (which owns the correlation rules and the
    ``require_plate_clear`` / farm-involvement raise guard); only the type lives
    here, so the core stays free of the correlation lane. ``raise_gate`` carries
    that guard: a non-farm terminal with the toggle off still raises nothing.
    """

    queue_item_id: int | None
    source_subtask_id: str | None
    evidence: DepositEvidence
    policy: OccupancyPolicy
    raise_gate: bool


@dataclass
class OccupancyRecord:
    """The three stored facts for one printer. Internal — callers read views."""

    plate: PlateOccupied | None = None
    lease: DispatchLease | None = None
    eject: PendingEject | None = None


@dataclass(frozen=True)
class OccupancyView:
    """An immutable snapshot of one printer's occupancy, plus the owner projection.

    A true snapshot: every field is a scalar or an immutable policy copied out of
    the record, so a view captured BEFORE a mutation still reads the old state
    afterwards. That is what lets ``_notify`` compute release edges honestly.

    ``lease_active`` is ``None`` when the caller supplied no :class:`Evidence` —
    "not evaluated against your evidence", not "no lease". The ``owner`` projection
    still resolves in that case, using an empty ``Evidence``: with no ``live_state``
    no transition is observable, so a lease can only be over-reported as active,
    never under-reported. Over-reporting a claim is the safe direction.
    """

    plate_occupied: bool
    plate_source_subtask_id: str | None
    plate_policy: OccupancyPolicy | None
    plate_since: datetime | None
    lease_unit_id: int | None
    lease_active: bool | None
    lease_age_s: float | None
    eject_purpose: EjectPurpose | None
    eject_started: bool
    eject_age_s: float | None
    eject_hydrated: bool
    owner: OccupancyOwner

    @property
    def eject_present(self) -> bool:
        """True when an eject — live or hydrated — is registered on this printer."""
        return self.eject_purpose is not None


# ---------------------------------------------------------------------------
# Injected side effects
# ---------------------------------------------------------------------------

PersistCallable = Callable[[int, OccupancyView], None]
BroadcastCallable = Callable[[int], None]
KickCallable = Callable[[int, str], None]
PolicyDriverCallable = Callable[[int, OccupancyView, str], None]
EscalateCallable = Callable[[int, BaseException], None]


def _noop_persist(printer_id: int, view: OccupancyView) -> None:
    """Default sink, so the core is fully usable — and unit-testable — unconfigured."""


def _noop_broadcast(printer_id: int) -> None:
    """Default sink; see :func:`_noop_persist`."""


def _noop_kick(printer_id: int, cause: str) -> None:
    """Default sink; see :func:`_noop_persist`."""


def _noop_policy_driver(printer_id: int, view: OccupancyView, cause: str) -> None:
    """Default sink; see :func:`_noop_persist`."""


def _noop_escalate(printer_id: int, error: BaseException) -> None:
    """Default sink; see :func:`_noop_persist`."""


# ---------------------------------------------------------------------------
# Clocks (module-level so tests can substitute them without an injection seam)
# ---------------------------------------------------------------------------


def _now() -> datetime:
    """Timezone-aware UTC wall clock — what every stored timestamp is measured in."""
    return datetime.now(timezone.utc)


def _now_mono() -> float:
    """Monotonic seconds — what every HOLD window is measured in. Never wall time."""
    return time.monotonic()


def _lease_active(lease: DispatchLease, ev: Evidence, now_mono: float) -> bool:
    """Is this lease still in flight, as of *now_mono* and the caller's evidence?

    Pure, and evaluated on READ — never written back as a "settled" state. Three
    cases:

    * **revoked** — not active. An operator declared the plate occupied under it, so
      it no longer holds the printer; :meth:`PlateOccupancy.commit_dispatch` still
      finds it and refuses ``lease_revoked``, which is how the scheduler learns to
      unwind its row instead of printing onto a declared plate.
    * **uncommitted** — always active. The upload is in progress; there is nothing
      on the wire to transition yet, and the printer is unambiguously spoken for.
    * **committed** — active until the wire moved past the pre-dispatch snapshot AND
      ``min_hold_s`` elapsed, or ``max_hold_s`` elapsed outright.

    The transition test is ``ev.live_state`` differing from ``pre_state``. The core
    has no subtask evidence beyond the live state, so the old scheduler's second
    limb (a subtask id advance) folds into the same test; an empty or absent
    ``live_state`` is silence, not a transition. With a falsy ``pre_state`` no
    transition is detectable at all and ``min_hold_s`` alone decides — the same
    fallback the scheduler's sentinel empty string encoded.
    """
    if lease.revoked:
        return False
    if lease.committed_at_mono is None:
        return True

    elapsed = now_mono - lease.committed_at_mono
    if elapsed >= lease.max_hold_s:
        return False
    if not lease.pre_state:
        return elapsed < lease.min_hold_s

    transitioned = ev.live_state not in (None, "", lease.pre_state)
    return not (transitioned and elapsed >= lease.min_hold_s)


# ---------------------------------------------------------------------------
# The authority
# ---------------------------------------------------------------------------


class PlateOccupancy:
    """The only writer of plate / lease / eject state, fleet-wide.

    Transitions return :data:`TransitionRefusal` or ``None``; queries return views
    or refusals and never mutate anything an observer can see (they DO prune a
    settled lease lazily, which is the expiry itself, not a state change — see
    :meth:`_lease_in_flight`).

    **The fan-out** runs after every state change, in a fixed order, through the
    five callables injected by :meth:`configure`:

    1. ``persist(printer_id, view)`` — the durable mirror;
    2. ``broadcast(printer_id)`` — the websocket status push;
    3. ``kick(printer_id, cause)`` — the scheduler wake-up, on RELEASE EDGES ONLY
       (plate OCCUPIED→CLEAR, or an eject dropped): those are the only two changes
       that can make a printer newly dispatchable, and kicking on the others would
       wake the scheduler into a printer it must not touch;
    4. ``policy_driver(printer_id, view, cause)`` — the eject cooldown monitor.

    (1)-(3) swallow their exceptions with a WARNING: a failed broadcast must not
    unwind an occupancy fact that already happened. **(4) never swallows.** A policy
    that fails to arm is the armless-gate class from 2026-07-18/07-21, so the record
    is REPAIRED to :class:`EscalationOnly` in place and the driver is called once
    more; if that fails too the fifth callable, ``escalate``, pages a human. The
    repair is deliberately a direct mutation and not a transition — a nested
    ``_notify`` from inside the fan-out is exactly what the re-entrancy guard exists
    to prevent, and a repair that queued itself behind its own failure would never
    run.

    ``cause="hydrate"`` skips persist, broadcast and kick — the state was just READ
    from the DB, so echoing it back is a write with no information in it, and a kick
    would wake the scheduler mid-startup, before the reconcilers have decided what
    the hydrated records mean. The policy driver IS called, because an occupied
    plate rebuilt at startup still needs its watch armed.
    """

    def __init__(self) -> None:
        self._records: dict[int, OccupancyRecord] = {}
        self._persist: PersistCallable = _noop_persist
        self._broadcast: BroadcastCallable = _noop_broadcast
        self._kick: KickCallable = _noop_kick
        self._policy_driver: PolicyDriverCallable = _noop_policy_driver
        self._escalate: EscalateCallable = _noop_escalate
        # Re-entrancy: printers whose fan-out is currently running, and the
        # notifications queued behind each of them.
        self._notifying: set[int] = set()
        self._deferred: dict[int, list[tuple[OccupancyView, OccupancyView, NotifyCause]]] = {}

    # -- configuration ------------------------------------------------------

    def configure(
        self,
        *,
        persist: PersistCallable | None = None,
        broadcast: BroadcastCallable | None = None,
        kick: KickCallable | None = None,
        policy_driver: PolicyDriverCallable | None = None,
        escalate: EscalateCallable | None = None,
    ) -> None:
        """Inject the side effects at lifespan (dependency inversion).

        Every argument is optional and ``None`` means "leave this one as it is", so
        a caller may wire the lanes it owns without silently un-wiring the others.
        Un-wiring is :meth:`reset_for_tests`' business.
        """
        if persist is not None:
            self._persist = persist
        if broadcast is not None:
            self._broadcast = broadcast
        if kick is not None:
            self._kick = kick
        if policy_driver is not None:
            self._policy_driver = policy_driver
        if escalate is not None:
            self._escalate = escalate

    def reset_for_tests(self) -> None:
        """Drop every record and un-wire every callable. Test-support only."""
        self._records.clear()
        self._notifying.clear()
        self._deferred.clear()
        self._persist = _noop_persist
        self._broadcast = _noop_broadcast
        self._kick = _noop_kick
        self._policy_driver = _noop_policy_driver
        self._escalate = _noop_escalate

    # -- startup hydration --------------------------------------------------

    def hydrate_plate(self, printer_id: int, source_subtask_id: str | None, policy: OccupancyPolicy) -> None:
        """Rebuild an occupied plate from the durable columns at startup.

        Not a transition: it asserts a fact that was already true before this
        process existed, so it must not be persisted back or broadcast, and above
        all must not kick the scheduler. The policy driver still runs — the whole
        point of rebuilding the plate is to re-arm what happens to it.
        """
        before = self._view(printer_id, None)
        record = self._record(printer_id)
        record.plate = PlateOccupied(source_subtask_id=source_subtask_id, policy=policy, since=_now())
        self._notify(printer_id, before, self._view(printer_id, None), HYDRATE_CAUSE)

    def hydrate_eject(self, printer_id: int, pending: PendingEject) -> None:
        """Rebuild a pending eject from the queue unit's durable stamp at startup.

        ``hydrated`` is forced True whatever the caller passed: provenance is a
        property of HOW the record was born, and every downstream rule that treats a
        hydrated eject differently (no watchdog, never expired for a missing start,
        superseded by an operator) depends on it being honest.
        """
        before = self._view(printer_id, None)
        record = self._record(printer_id)
        record.eject = replace(pending, hydrated=True)
        self._notify(printer_id, before, self._view(printer_id, None), HYDRATE_CAUSE)

    # -- dispatch lane ------------------------------------------------------

    def claim_for_dispatch(
        self,
        printer_id: int,
        unit_id: int,
        *,
        pre_state: str | None,
        pre_subtask: str | None,
        min_hold_s: float,
        max_hold_s: float,
        ev: Evidence,
    ) -> DispatchLease | TransitionRefusal:
        """Claim a printer for a unit dispatch at PLAN time, before the upload.

        Gated by exactly :meth:`dispatchable`, so the check the scheduler makes when
        it picks a printer and the check that actually mints the claim cannot drift
        apart. Returns the lease on success; a :data:`TransitionRefusal` string
        otherwise (test with ``isinstance(result, DispatchLease)``).

        ``pre_state`` / ``pre_subtask`` are the wire snapshot taken NOW: settlement
        later asks whether the printer moved off them, which is the only evidence
        available that a print command actually landed.
        """
        refusal = self.dispatchable(printer_id, ev)
        if refusal is not None:
            return refusal

        before = self._view(printer_id, None)
        lease = DispatchLease(
            unit_id=unit_id,
            pre_state=pre_state,
            pre_subtask=pre_subtask,
            min_hold_s=min_hold_s,
            max_hold_s=max_hold_s,
            minted_at_mono=_now_mono(),
        )
        self._record(printer_id).lease = lease
        self._notify(printer_id, before, self._view(printer_id, None), "claim_for_dispatch")
        return lease

    def commit_dispatch(self, printer_id: int, lease: DispatchLease) -> TransitionRefusal | None:
        """Start the echo-lag hold window, immediately before the print command goes out.

        The identity check is ``is``, not equality: the caller has held this object
        across an FTPS upload, and a DIFFERENT lease on the printer means the world
        moved under it (released, superseded). ``lease_revoked`` is the 2026-08-30
        01:06:57 shape — an operator declared the plate occupied mid-upload, and the
        scheduler must unwind its row rather than print onto a declared plate.
        """
        record = self._records.get(printer_id)
        if record is None or record.lease is not lease:
            return "lease_unknown"
        if lease.revoked:
            return "lease_revoked"
        if record.plate is not None:
            return "plate_occupied"

        before = self._view(printer_id, None)
        lease.committed_at_mono = _now_mono()
        self._notify(printer_id, before, self._view(printer_id, None), "commit_dispatch")
        return None

    def release_dispatch(self, printer_id: int, reason: str) -> None:
        """Drop the lease. Idempotent, never refuses — a release must always be reachable.

        Purely in-memory. ``queue_transitions.release_unstarted_claim`` remains the
        one writer of the queue row's status, and the two callers that un-make a
        dispatch (the scheduler's start watchdog, the dead-claim watch) call BOTH:
        this drops the printer claim, that drops the row claim.
        """
        record = self._records.get(printer_id)
        if record is None or record.lease is None:
            return

        before = self._view(printer_id, None)
        logger.info("[occupancy] p%d: dispatch lease released (unit %d, %s)", printer_id, record.lease.unit_id, reason)
        record.lease = None
        self._notify(printer_id, before, self._view(printer_id, None), "release_dispatch")

    # -- terminals and detections ------------------------------------------

    def note_terminal(self, printer_id: int, disposition: TerminalDisposition) -> None:
        """Record a job's terminal. Legal from ANY owner — the wire does not ask permission.

        **The plate is never CLEARED here.** A terminal that deposited nothing is
        not evidence that the plate is empty: the job may have been an operator stop
        on a plate that already carried a previous part, or a foreign print whose
        deposit this farm never saw. Clearing on a no-deposit terminal is how a
        standing gate silently disappeared and the next unit dispatched onto an
        occupied plate. The gate drops only where a human or a matched eject says
        so.

        A deposit-bearing terminal whose disposition says ``raise_gate`` SETS or
        REPLACES the plate — replacing source and policy both, matching the old
        ``set_awaiting_plate_clear(True, subtask)``: the newest deposit is the one
        an eject would sweep, so its identity must be the one the gate carries.

        The lease is consumed only when the terminal names the unit that holds it;
        any other terminal (a foreign job, a stale echo) leaves the lease to its own
        timers rather than handing an unrelated print the power to un-claim a
        printer. An eject owner is untouched for the same reason — a mismatched or
        foreign terminal keeps the pending eject exactly as it does today.
        """
        record = self._record(printer_id)
        before = self._view(printer_id, None)
        changed = False

        if record.lease is not None and disposition.queue_item_id == record.lease.unit_id:
            record.lease = None
            changed = True

        if disposition.evidence.deposited and disposition.raise_gate:
            record.plate = PlateOccupied(
                source_subtask_id=disposition.source_subtask_id,
                policy=disposition.policy,
                since=_now(),
            )
            changed = True

        if changed:
            self._notify(printer_id, before, self._view(printer_id, None), "terminal")

    def note_plate_detected(self, printer_id: int, source_detail: str) -> None:
        """The printer itself reported something on the plate (the native vision check).

        Legal from any owner because the trip fires MID-JOB: the H2-series pre-print
        vision check pauses the job and the plate is occupied whatever the farm
        believed. Only a human can clear that, so the policy is always
        :class:`EscalationOnly` and the source id is None — there is no job identity
        behind a vision trip to sweep against.

        Idempotent when the plate already reads exactly that: the trip can re-fire
        on repeated pushes, and re-stamping ``since`` would both churn the fan-out
        and lie about when the plate became occupied.
        """
        record = self._record(printer_id)
        plate = record.plate
        if plate is not None and plate.source_subtask_id is None and isinstance(plate.policy, EscalationOnly):
            return

        before = self._view(printer_id, None)
        record.plate = PlateOccupied(source_subtask_id=None, policy=EscalationOnly(), since=_now())
        logger.info("[occupancy] p%d: plate detected by the printer (%s)", printer_id, source_detail)
        self._notify(printer_id, before, self._view(printer_id, None), "plate_detected")

    # -- operator statements ------------------------------------------------

    def declare_occupied(self, printer_id: int, ev: Evidence) -> TransitionRefusal | None:
        """The operator states that this plate carries a part. The operator is the authority.

        Refused only where the statement cannot be acted on: a running job would be
        depositing onto it anyway, and a LIVE eject is already sweeping it. A
        HYDRATED eject does not refuse — the farm has admitted it cannot verify that
        record, so it cannot use it to contradict a human standing in front of the
        machine.

        Already occupied is an idempotent success: the operator asked for the plate
        to be treated as occupied, and it is. The policy is deliberately NOT reset —
        re-declaring must not demote a cooldown watch that is already doing the
        right thing.

        An unrevoked lease is REVOKED here, and that is the whole 2026-08-30 01:06:57
        cure: the declaration landed between the scheduler's claim and its
        ``start_print`` and was erased by an unconditional gate clear on the dispatch
        path. Revoking makes :meth:`commit_dispatch` refuse ``lease_revoked``, so the
        scheduler unwinds the row instead of printing onto the declared plate.
        """
        if ev.live_state in ACTIVE_PRINT_STATES:
            return "job_active"
        if self._live_eject(printer_id) is not None:
            return "eject_in_flight"

        record = self._record(printer_id)
        if record.plate is not None:
            return None

        before = self._view(printer_id, None)
        record.plate = PlateOccupied(source_subtask_id=None, policy=EscalationOnly(), since=_now())
        if record.lease is not None and not record.lease.revoked:
            record.lease.revoked = True
            logger.info(
                "[occupancy] p%d: dispatch lease (unit %d) revoked by an operator plate declaration",
                printer_id,
                record.lease.unit_id,
            )
        self._notify(printer_id, before, self._view(printer_id, None), "declare_occupied")
        return None

    def clear_plate(self, printer_id: int) -> TransitionRefusal | None:
        """The plate is empty and the printer may take work again.

        Refused while a LIVE eject owns the printer: the gate is that eject's own
        completion signal, and clearing it under the sweep would release the printer
        into a dispatch that lands on a plate the sweep is still crossing. A
        HYDRATED eject does not refuse — it is exactly the unverifiable record the
        startup reconciler exists to dispose of, and letting it block the operator's
        cure is the 2026-08-30 dead-end.
        """
        record = self._records.get(printer_id)
        if record is None or record.plate is None:
            return "not_occupied"
        if self._live_eject(printer_id) is not None:
            return "eject_in_flight"

        before = self._view(printer_id, None)
        record.plate = None
        self._notify(printer_id, before, self._view(printer_id, None), "clear_plate")
        return None

    def operator_recover(self, printer_id: int) -> None:
        """Explicit override: forget everything this module believes about the printer.

        Recover means "an operator inspected the machine", which outranks every
        stored belief — so unlike :meth:`clear_plate` it refuses nothing and drops
        the eject and the lease as well as the plate. What was dropped is logged at
        WARNING, because discarding an in-flight eject record is exactly the kind of
        state loss that must be visible in triage afterwards.
        """
        record = self._records.get(printer_id)
        if record is None or (record.plate is None and record.lease is None and record.eject is None):
            return

        before = self._view(printer_id, None)
        if record.eject is not None:
            logger.warning(
                "[occupancy] p%d: operator recover dropped a %s eject (item %s, started=%s)",
                printer_id,
                record.eject.purpose,
                record.eject.queue_item_id,
                record.eject.started_at,
            )
        if record.lease is not None:
            logger.warning(
                "[occupancy] p%d: operator recover dropped the dispatch lease for unit %d",
                printer_id,
                record.lease.unit_id,
            )
        record.plate = None
        record.lease = None
        record.eject = None
        self._notify(printer_id, before, self._view(printer_id, None), "operator_recover")

    # -- eject lane ---------------------------------------------------------

    def claim_for_eject(self, printer_id: int, pending: PendingEject, ev: Evidence) -> TransitionRefusal | None:
        """Claim the printer for an eject sweep, after the printer accepted the start.

        Gated by exactly :meth:`ejectable`, so the pre-flight check the dispatcher
        makes before it spends seconds building and uploading a file is the same
        gate that mints the claim.

        **A hydrated eject is SUPERSEDED, not protected.** It has ``started_at=None``
        by construction, no watchdog, no live identity, and ends in
        :meth:`drop_hydrated_eject` anyway; refusing an operator in order to protect
        a record the farm already admits it cannot verify is backwards. That refusal
        is the 2026-08-30 "8 consecutive eject 409s" class — printer 4, 01:46-01:49,
        after which the operator hand-jogged the toolhead. It is dropped here with a
        WARNING and the new pending takes the slot.

        ``dispatched_at`` is stamped here (not by the caller) and ``hydrated`` is
        forced False: a claim is a LIVE dispatch by definition, and the age this
        stamp yields is what the operator is shown while a sweep has not started.
        """
        refusal = self.ejectable(printer_id, ev)
        if refusal is not None:
            return refusal

        record = self._record(printer_id)
        before = self._view(printer_id, None)
        if record.eject is not None:
            logger.warning(
                "[occupancy] p%d: superseded_by_operator — dropping a hydrated %s eject (item %s)",
                printer_id,
                record.eject.purpose,
                record.eject.queue_item_id,
            )
        record.eject = replace(pending, dispatched_at=_now(), hydrated=False)
        self._notify(printer_id, before, self._view(printer_id, None), "claim_for_eject")
        return None

    def note_eject_started(self, printer_id: int) -> None:
        """Stamp the sweep's START echo. First write wins; a no-op without an eject.

        First-write-wins because the wire can repeat a start (a re-published push, a
        reconnect replay) and the runtime watchdog measures from this stamp: moving
        it forward would silently extend a deadline that exists to bound a stall.

        ``hydrated`` is deliberately left alone — provenance does not change because
        a start was observed. A hydrated eject that IS running is protected by
        ``job_active`` (the printer is running the sweep), not by the eject slot.
        """
        record = self._records.get(printer_id)
        if record is None or record.eject is None or record.eject.started_at is not None:
            return

        before = self._view(printer_id, None)
        record.eject = replace(record.eject, started_at=_now())
        self._notify(printer_id, before, self._view(printer_id, None), "eject_started")

    def note_eject_runtime_exceeded(self, printer_id: int, fired_at: datetime, stage: str) -> None:
        """Stamp the runtime watchdog's verdict IN PLACE. First write wins.

        Stamped BEFORE the stop command is sent, so a terminal racing the watchdog
        must already see the verdict — the ordering the 2026-07-31 gouged-plate
        incident fixed. A stall left the bed high, the sweep gouged the plate, and
        the job still echoed ``completed``: the terminal handler must therefore HONOR
        this mark rather than re-deriving a runtime judgement of its own, or the
        machine is stopped on one criterion and judged on another.
        """
        record = self._records.get(printer_id)
        if record is None or record.eject is None or record.eject.runtime_exceeded_at is not None:
            return

        before = self._view(printer_id, None)
        record.eject = replace(record.eject, runtime_exceeded_at=fired_at)
        logger.warning("[occupancy] p%d: eject runtime exceeded at stage=%s (%s)", printer_id, stage, fired_at)
        self._notify(printer_id, before, self._view(printer_id, None), "eject_runtime_exceeded")

    def resolve_eject(self, printer_id: int, outcome: EjectOutcome) -> TransitionRefusal | None:
        """Retire the pending eject on its terminal.

        ``completed`` is the ONLY path on which the gate auto-clears: a positively
        matched eject terminal is the farm's own statement that the sweep ran.
        ``unverified`` covers everything else — a watchdog-stopped sweep, a failure,
        a terminal that cannot be trusted — and leaves the plate OCCUPIED under
        :class:`EscalationOnly`, because the part may be anywhere on the bed and only
        a human can say.

        On ``unverified`` the plate's ``since`` is preserved when the plate was
        already occupied: the plate has genuinely carried a deposit since then, and
        re-stamping it would tell the operator the part appeared when the sweep
        failed.
        """
        record = self._records.get(printer_id)
        if record is None or record.eject is None:
            return "no_eject"

        before = self._view(printer_id, None)
        record.eject = None
        if outcome == "completed":
            record.plate = None
            self._notify(printer_id, before, self._view(printer_id, None), "eject_completed")
            return None

        record.plate = self._escalation_plate(record.plate)
        self._notify(printer_id, before, self._view(printer_id, None), "eject_unverified")
        return None

    def expire_eject_start(self, printer_id: int) -> bool:
        """Retire an eject the printer never started. True iff it fired.

        The firmware silently ignores a ``project_file`` sent while it is busy, so a
        dispatched eject can simply never happen — no error, no terminal, nothing.
        That is how the 2026-08-30 pendings stayed registered forever and every later
        eject 409'd. The start deadline is the only signal that shape produces.

        A HYDRATED eject never expires here: its ``started_at`` is None by
        construction, so an expiry rule keyed on it would fire on every restart and
        discard a record the reconciler is still deciding about. Hydrated pendings
        are the reconciler's business (:meth:`drop_hydrated_eject`).
        """
        record = self._records.get(printer_id)
        if record is None or record.eject is None:
            return False
        if record.eject.started_at is not None or record.eject.hydrated:
            return False

        before = self._view(printer_id, None)
        logger.warning(
            "[occupancy] p%d: eject_never_started — dropping a %s eject dispatched at %s",
            printer_id,
            record.eject.purpose,
            record.eject.dispatched_at,
        )
        record.eject = None
        record.plate = self._escalation_plate(record.plate)
        self._notify(printer_id, before, self._view(printer_id, None), "eject_never_started")
        return True

    def drop_hydrated_eject(self, printer_id: int, reason: str) -> bool:
        """Dispose of a hydrated eject on the startup reconciler's verdict. True iff dropped.

        The plate is untouched: the reconciler decides only what became of the EJECT,
        and a plate that survived a restart keeps whatever the durable columns said
        until a human or a matched terminal moves it.
        """
        record = self._records.get(printer_id)
        if record is None or record.eject is None or not record.eject.hydrated:
            return False

        before = self._view(printer_id, None)
        logger.info("[occupancy] p%d: dropping hydrated %s eject (%s)", printer_id, record.eject.purpose, reason)
        record.eject = None
        self._notify(printer_id, before, self._view(printer_id, None), "drop_hydrated_eject")
        return True

    # -- policy -------------------------------------------------------------

    def set_policy(self, printer_id: int, policy: OccupancyPolicy) -> TransitionRefusal | None:
        """Swap what happens to an occupied plate (a first-article approval, a startup ladder).

        Refused on a clear plate: a policy with no deposit to act on is an armed
        watch over nothing, which is how a watch outlives the plate it was arming for.
        """
        record = self._records.get(printer_id)
        if record is None or record.plate is None:
            return "not_occupied"

        before = self._view(printer_id, None)
        record.plate.policy = policy
        self._notify(printer_id, before, self._view(printer_id, None), "set_policy")
        return None

    # -- queries ------------------------------------------------------------

    def snapshot(self, printer_id: int, ev: Evidence | None = None) -> OccupancyView:
        """The printer's occupancy as a value. Without *ev*, ``lease_active`` is ``None``."""
        return self._view(printer_id, ev)

    def dispatchable(self, printer_id: int, ev: Evidence) -> TransitionRefusal | None:
        """May a unit be dispatched onto this printer? ``None`` means yes.

        Priority is deliberate and is the order in which a refusal is USEFUL: the
        plate first (a deposit blocks everything and needs a human or an eject), then
        an eject (LIVE **or** HYDRATED — a hydrated eject still means "the startup
        reconciler owns this printer", and dispatching under it would race the
        verdict), then a dispatch already in flight, then the wire.

        ``db_claim`` is honoured here and ONLY here: during the IDLE→RUNNING lag the
        queue row is the only witness that a unit is already on its way.
        """
        record = self._records.get(printer_id)
        if record is not None and record.plate is not None:
            return "plate_occupied"
        if record is not None and record.eject is not None:
            return "eject_in_flight"
        if self._lease_in_flight(printer_id, ev) or ev.db_claim:
            return "dispatch_in_flight"
        if ev.live_state in ACTIVE_PRINT_STATES:
            return "job_active"
        return None

    def ejectable(self, printer_id: int, ev: Evidence) -> TransitionRefusal | None:
        """May an eject sweep be dispatched onto this printer? ``None`` means yes.

        Priority is the reverse shape of :meth:`dispatchable`, because the questions
        are opposite: the wire first (a running job is the one thing an eject can
        physically collide with), then an in-flight LEASE — never ``db_claim``, see
        :class:`Evidence` — then a LIVE eject, and only then the plate.

        ``not_occupied`` comes last on purpose: it is the one refusal the operator
        can cure without waiting for anything (the manual lane declares occupancy
        first), so reporting it ahead of a real conflict would send them to fix the
        wrong thing.
        """
        if ev.live_state in ACTIVE_PRINT_STATES:
            return "job_active"
        if self._lease_in_flight(printer_id, ev):
            return "dispatch_in_flight"
        if self._live_eject(printer_id) is not None:
            return "eject_in_flight"
        record = self._records.get(printer_id)
        if record is None or record.plate is None:
            return "not_occupied"
        return None

    def plate_source(self, printer_id: int) -> str | None:
        """The gate's source ``subtask_id``, or None. The ONE reader of that id."""
        record = self._records.get(printer_id)
        return record.plate.source_subtask_id if record is not None and record.plate is not None else None

    def is_plate_occupied(self, printer_id: int) -> bool:
        """Thin projection for the many callers that only ask "is the gate up?"."""
        record = self._records.get(printer_id)
        return record is not None and record.plate is not None

    def pending_eject_view(self, printer_id: int) -> PendingEject | None:
        """The FULL pending eject this printer holds, or None. A read-only peek.

        :meth:`eject_identity` is the IDENTITY projection — what the watchdog and the
        terminal matcher compare against — and it deliberately carries no build
        figures. The runtime watchdog needs those figures (``expected_runtime_s`` and
        the per-phase budgets) to compute its deadlines, and the eject terminal logs them
        beside the measured runtime, so this hands back the record itself.

        Safe to expose because :class:`PendingEject` is frozen: a caller can read it
        but cannot mutate the record through it. Prefer :meth:`eject_identity`
        wherever identity alone answers the question — this exists for the two
        consumers that genuinely need the build figures.
        """
        record = self._records.get(printer_id)
        return record.eject if record is not None else None

    def eject_identity(self, printer_id: int) -> EjectIdentity | None:
        """What the watchdog and terminal matching compare against, or None."""
        record = self._records.get(printer_id)
        if record is None or record.eject is None:
            return None
        pending = record.eject
        return EjectIdentity(
            purpose=pending.purpose,
            queue_item_id=pending.queue_item_id,
            started_at=pending.started_at,
            dispatched_at=pending.dispatched_at,
            hydrated=pending.hydrated,
            runtime_exceeded_at=pending.runtime_exceeded_at,
        )

    def printers_with_lease_or_eject(self) -> set[int]:
        """Printers this module claims, to augment the scheduler's DB-derived busy set.

        Evaluated with no evidence, so a committed lease counts as in flight until
        its ``max_hold_s`` ceiling — the safe direction for a busy set, since
        over-reporting delays a dispatch by seconds while under-reporting sends one
        onto a printer that is already taking a job.
        """
        ev = Evidence()
        claimed: set[int] = set()
        for printer_id, record in list(self._records.items()):
            if record.eject is not None or self._lease_in_flight(printer_id, ev):
                claimed.add(printer_id)
        return claimed

    # -- internals ----------------------------------------------------------

    def _record(self, printer_id: int) -> OccupancyRecord:
        """The printer's record, created empty on first touch."""
        record = self._records.get(printer_id)
        if record is None:
            record = OccupancyRecord()
            self._records[printer_id] = record
        return record

    def _live_eject(self, printer_id: int) -> PendingEject | None:
        """The eject the farm can actually verify — a hydrated one is not one."""
        record = self._records.get(printer_id)
        if record is None or record.eject is None or record.eject.hydrated:
            return None
        return record.eject

    def _lease_in_flight(self, printer_id: int, ev: Evidence) -> bool:
        """Settle the lease on READ, pruning it once it is spent.

        The prune is the expiry itself, not a state change, so it deliberately does
        NOT notify: fanning persist/broadcast/kick out for a lease that simply timed
        out would write on a tick that may not even have work to do. Only a
        COMMITTED lease is pruned — an uncommitted one is always in flight, and a
        revoked-but-uncommitted one must survive to be found by
        :meth:`commit_dispatch` so it can refuse ``lease_revoked``.
        """
        record = self._records.get(printer_id)
        if record is None or record.lease is None:
            return False

        active = _lease_active(record.lease, ev, _now_mono())
        if not active and record.lease.committed_at_mono is not None:
            record.lease = None
        return active

    @staticmethod
    def _escalation_plate(plate: PlateOccupied | None) -> PlateOccupied:
        """An escalation-only plate, keeping an existing gate's source id and ``since``.

        The two eject retirements that leave a part behind (an unverified terminal, a
        sweep that never started) must not invent a new occupancy: if the gate was
        already up, the plate has carried this deposit since then and the source id
        still names the job that produced it.
        """
        if plate is None:
            return PlateOccupied(source_subtask_id=None, policy=EscalationOnly(), since=_now())
        return PlateOccupied(source_subtask_id=plate.source_subtask_id, policy=EscalationOnly(), since=plate.since)

    def _view(self, printer_id: int, ev: Evidence | None) -> OccupancyView:
        """Project the record (settled against *ev*) into an immutable snapshot."""
        effective = ev if ev is not None else Evidence()
        # Settle FIRST: a spent lease must not appear in the view it was pruned from.
        lease_in_flight = self._lease_in_flight(printer_id, effective)
        record = self._records.get(printer_id)
        plate = record.plate if record is not None else None
        lease = record.lease if record is not None else None
        eject = record.eject if record is not None else None

        if eject is not None:
            owner: OccupancyOwner = "eject"
        elif lease_in_flight:
            owner = "dispatch"
        elif effective.live_state in ACTIVE_PRINT_STATES:
            owner = "job"
        else:
            owner = "none"

        now_mono = _now_mono()
        now = _now()
        return OccupancyView(
            plate_occupied=plate is not None,
            plate_source_subtask_id=plate.source_subtask_id if plate is not None else None,
            plate_policy=plate.policy if plate is not None else None,
            plate_since=plate.since if plate is not None else None,
            lease_unit_id=lease.unit_id if lease is not None else None,
            lease_active=lease_in_flight if ev is not None else None,
            lease_age_s=(now_mono - lease.minted_at_mono) if lease is not None else None,
            eject_purpose=eject.purpose if eject is not None else None,
            eject_started=eject is not None and eject.started_at is not None,
            eject_age_s=(
                (now - eject.dispatched_at).total_seconds()
                if eject is not None and eject.dispatched_at is not None
                else None
            ),
            eject_hydrated=eject is not None and eject.hydrated,
            owner=owner,
        )

    def _notify(
        self,
        printer_id: int,
        before: OccupancyView,
        after: OccupancyView,
        cause: NotifyCause,
    ) -> None:
        """Fan a completed state change out to the injected callables.

        Re-entrant by DEFERRAL: a nested notification (a callable that ran a
        transition of its own) is queued and drained only after the outer fan-out
        finishes, so no callable can observe a record that changed underneath it
        mid-notification. The drain runs while the guard is still held, so a
        notification nested inside the drain queues behind it too.
        """
        if printer_id in self._notifying:
            self._deferred.setdefault(printer_id, []).append((before, after, cause))
            return

        self._notifying.add(printer_id)
        try:
            self._fan_out(printer_id, before, after, cause)
            queued = self._deferred.get(printer_id)
            while queued:
                self._fan_out(printer_id, *queued.pop(0))
                queued = self._deferred.get(printer_id)
        finally:
            self._notifying.discard(printer_id)
            self._deferred.pop(printer_id, None)

    def _fan_out(
        self,
        printer_id: int,
        before: OccupancyView,
        after: OccupancyView,
        cause: NotifyCause,
    ) -> None:
        """One pass of the fixed-order side effects. See the class docstring."""
        if cause != HYDRATE_CAUSE:
            try:
                self._persist(printer_id, after)
            except Exception:
                logger.warning("[occupancy] p%d: persist failed (%s)", printer_id, cause, exc_info=True)

            try:
                self._broadcast(printer_id)
            except Exception:
                logger.warning("[occupancy] p%d: broadcast failed (%s)", printer_id, cause, exc_info=True)

            # Release edges only: these are the two changes that can make a printer
            # newly dispatchable.
            released = (before.plate_occupied and not after.plate_occupied) or (
                before.eject_present and not after.eject_present
            )
            if released:
                try:
                    self._kick(printer_id, cause)
                except Exception:
                    logger.warning("[occupancy] p%d: dispatch kick failed (%s)", printer_id, cause, exc_info=True)

        try:
            self._policy_driver(printer_id, after, cause)
            return
        except Exception:
            # A policy that failed to arm leaves the plate ARMLESS, which is the one
            # outcome 2026-07-18/07-21 forbids. Repair in place (never a transition —
            # a nested _notify from inside the fan-out would only queue behind this
            # very failure) and try once more with the escalation-only floor.
            logger.warning(
                "[occupancy] p%d: policy driver failed (%s) — repairing to escalation-only",
                printer_id,
                cause,
                exc_info=True,
            )
            record = self._records.get(printer_id)
            if record is not None and record.plate is not None:
                record.plate.policy = EscalationOnly()
            repaired = self._view(printer_id, None)

        try:
            self._policy_driver(printer_id, repaired, cause)
        except Exception as repair_error:
            logger.warning(
                "[occupancy] p%d: escalation-only policy ALSO failed (%s) — escalating",
                printer_id,
                cause,
                exc_info=True,
            )
            try:
                self._escalate(printer_id, repair_error)
            except Exception:
                logger.warning("[occupancy] p%d: escalation failed (%s)", printer_id, cause, exc_info=True)


# The fork's singleton convention: one authority per process, wired at lifespan.
plate_occupancy = PlateOccupancy()
