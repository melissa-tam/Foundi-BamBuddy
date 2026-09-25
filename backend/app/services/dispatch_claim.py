"""Who owns a dispatch that has not started yet — and when the answer is "nobody".

A unit is marked ``printing`` BEFORE the print command goes out (deliberately: a crash
in between must leave a row wrongly marked printing rather than one that silently
reprints hours later). Two things can then own that claim while the print is still
landing, and exactly one of them is a task:

* the **start watchdog** (``print_scheduler._watchdog_print_start``) — spawned at the
  dispatch, it polls for the active-state transition and un-claims the row itself if the
  command never lands. While it lives, the answer is *it owns this*;
* **nobody** — the watchdog exited on a state that later went away, or the process
  restarted and took every watchdog with it. That claim is what
  ``farm_stall.check_dead_dispatch_claims`` exists to release.

**This module is the ownership HANDOFF, and it replaced a timer that guessed at it.**
``farm_stall`` used to require a claim to be 600 s old before it would look, with the
comment that the figure existed so the watchdog was "ALWAYS the first responder" — i.e.
one task inferring another task's liveness from a clock, with no evidence and a wide
margin for safety. The watchdog's real budget is 90 s + 180 s, it exits on ANY active
state, and it dies with every restart, so precisely the cases with NO watchdog — the
ones nothing else can retire — waited the full ~12 minutes while the UI said "printing"
over a demonstrably idle printer. Operators stopped those units by hand first, which is
the farm losing an argument with its own bookkeeping.

So liveness is ASKED rather than assumed (:func:`has_live_start_watchdog`, the same
shape as ``printer_incidents.driver_live``), and what is left of the clock is one
honest number: :data:`DISPATCH_START_BUDGET_S`, the measured window in which a printer
that has ACCEPTED a job may still report a non-active state. It is the watchdog's own
Phase A timeout — ONE origin, read by the watchdog as its ``timeout`` default — so the
two lanes can never come to disagree about how long a start is allowed to take.

**Leaf by construction.** It imports no farm service and holds no DB handle, so both
consumers (``print_scheduler`` for the registry, ``farm_stall`` for the judge) import it
at MODULE level — no call-time import to dodge a cycle, because there is no cycle. The
two exceptions are stdlib-only owners of a definition a second spelling would fork:
``ACTIVE_PRINT_STATES``, taken from the plate-occupancy authority that OWNS "what counts
as an active job" (a second spelling of the set is how two lanes come to disagree about
PAUSE — which here is the difference between leaving a native-vision hold alone and
double-dispatching onto an occupied plate); and ``job_identity.same_job``, the ONE
comparison of two subtask ids.

:func:`judge` is pure, I/O-free and table-testable: every guard that used to live in a
docstring paragraph is a row it can be asked about. The GATHERING (DB reads, wire reads)
and the APPLYING (the dwell, the release, the broadcast) stay with ``farm_stall``, which
owns the tick.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

from backend.app.services.job_identity import same_job
from backend.app.services.plate_occupancy import ACTIVE_PRINT_STATES

logger = logging.getLogger(__name__)

#: How long a printer that has ACCEPTED a job may still report a non-active state.
#: THE one origin of the start budget: ``print_scheduler._watchdog_print_start`` reads it
#: as its Phase A ``timeout`` default, and :func:`judge` uses it as the floor under which
#: an unwatched claim is simply too fresh to call dead.
#:
#: 90 s is the watchdog's own measured figure (raised from 45 s for slow transitions that
#: emit no early ``subtask_id`` tick; an H2D can sit at FINISH ~50 s after accepting a
#: ``project_file``). It is the RIGHT floor for the unwatched case too, because the
#: question is identical — "could this printer still be digesting the job?" — and after a
#: restart it is the ONLY thing standing between a claim and a release.
DISPATCH_START_BUDGET_S = 90.0

#: What a claim's evidence adds up to. Closed on purpose: a seventh reading of "is this
#: dispatch alive" has to be added here, where the table can be asked about it, rather
#: than appearing as a new ``continue`` in the middle of a sweep.
ClaimVerdict = Literal[
    "started",  # the print landed — the three double-dispatch guards
    "offline",  # the wire cannot be read (disconnected, stale, or already flagged)
    "watchdog_owns",  # a live start watchdog is going to answer this itself
    "too_fresh",  # inside the start budget, or of unknowable age
    "recovery_acting",  # somebody is acting on the printer right now
    "dead",  # nobody owns it and nothing started — release it
]

# item_id -> the live start-watchdog task for that dispatch. Process-lifetime by
# nature: the task IS the process, so a restart's empty registry is not a gap in the
# record, it is the record (no watchdogs exist after a restart, which is exactly the
# case the dead-claim watch has to answer for).
_start_watchdogs: dict[int, asyncio.Task] = {}


def register_start_watchdog(item_id: int, task: asyncio.Task) -> None:
    """Record that ``task`` is watching queue item ``item_id``'s dispatch start.

    Called at the ONE spawn site in ``print_scheduler``. The done-callback pops the
    entry — and pops only ITS OWN, so a re-dispatch that registered a newer watchdog
    for the same item cannot be un-registered by the older task finishing.

    A task that is already done is still recorded: :func:`has_live_start_watchdog`
    asks ``.done()`` rather than trusting membership, exactly as
    ``printer_incidents.driver_live`` does, so the two answers cannot diverge on a
    callback that has not run yet.
    """
    _start_watchdogs[item_id] = task
    task.add_done_callback(lambda finished: _forget_start_watchdog(item_id, finished))


def _forget_start_watchdog(item_id: int, task: asyncio.Task) -> None:
    """Drop ``item_id``'s registry slot iff ``task`` is still the one in it."""
    if _start_watchdogs.get(item_id) is task:
        del _start_watchdogs[item_id]


def has_live_start_watchdog(item_id: int) -> bool:
    """True while a start watchdog is actively watching ``item_id``'s dispatch.

    The ownership handoff, in one question. A missing slot OR a task that has already
    finished (``.done()``) both read as no live watchdog, so a registry entry orphaned
    by a cancelled loop cannot silence the dead-claim watch forever.
    """
    task = _start_watchdogs.get(item_id)
    return task is not None and not task.done()


def _reset_state() -> None:
    """Test hook: empty the watchdog registry between cases."""
    _start_watchdogs.clear()


@dataclass(frozen=True)
class ClaimEvidence:
    """Everything :func:`judge` is allowed to know about one ``printing`` claim.

    Gathered by ``farm_stall`` from the DB row, the live wire and the two liveness
    registries; frozen so a verdict can be re-derived from the same facts in a test,
    and logged verbatim beside the release it produced.
    """

    #: The printer answers over MQTT (``printer_manager.is_connected``).
    connected: bool
    #: ...and that answer is CURRENT (``BambuMQTTClient.is_stale()`` is False). A stale
    #: state is a cached snapshot, which is no better evidence than no state at all.
    state_fresh: bool
    #: The printer's live gcode state, upper-cased; ``""`` when it has none yet.
    live_state: str
    #: The row already carries the offline-stall token — that watch owns this story.
    offline_stalled: bool
    #: Some ``PrintArchive`` on this printer reads ``printing``.
    archive_printing: bool
    #: The submission id the dispatcher minted for this unit; ``""`` when it has none.
    dispatch_subtask: str
    #: The job id the printer is echoing right now; ``""`` when it names none.
    live_subtask: str
    #: Seconds since the claim was made, or ``None`` when ``started_at`` is NULL.
    claim_age_s: float | None
    #: A start watchdog is alive for this item (:func:`has_live_start_watchdog`).
    watchdog_live: bool
    #: A recovery driver is acting on this printer (a ``recovering`` incident row or a
    #: live ``spool_recovery`` task).
    recovery_acting: bool


def judge(evidence: ClaimEvidence) -> ClaimVerdict:
    """Read one claim's evidence into a verdict. Pure — no DB, no wire, no clock.

    The rows, in precedence order, and each one's reason for being ahead of the next:

    1. **offline** — the wire cannot be read: disconnected, STALE (a cached snapshot),
       or already flagged ``printer_offline_stalled``. That story has its own watch and
       its own token; a decision made against state nobody is refreshing is a guess.
    2. **started**, three ways, all of them double-dispatch guards, because the cost of
       a wrong release is a print onto an occupied plate:

       * the live state is ACTIVE — **PAUSE included**: a native-vision trip pauses at
         print start with the plate occupied, and releasing that unit would re-dispatch
         onto it. An EMPTY state reads as started too: no state is not evidence;
       * a ``PrintArchive`` on this printer reads ``printing`` — hard disjointness with
         ``main.reconcile_stale_active_prints``, so the two reconcilers can never both
         act on one printer;
       * the printer is echoing OUR dispatch id. That test is corroboration, never a
         precondition: a NULL id proves nothing either way, and requiring one would
         strand exactly the rows that need this most.
    3. **watchdog_owns** — a live start watchdog will answer this itself, AT ANY AGE.
       This is the row that replaced the 600 s guess: ownership asked instead of
       inferred, so a watchdog that is genuinely still working keeps its full 90/270 s
       and one that never existed costs nothing.
    4. **too_fresh** — no watchdog, and the claim is younger than
       :data:`DISPATCH_START_BUDGET_S` (or of unknowable age, which fails closed the
       same way: an unknowable age is not evidence). This is the post-restart floor —
       the case with no watchdog to ask — and it is measured rather than padded.
    5. **recovery_acting** — a ``recovering`` incident row or a live recovery task. An
       ESCALATED hold deliberately does NOT appear here: that is a human's, not an
       actor that can land a print, and under the equipment-fault model it can be
       permanent (003-H2S item 1988 sat behind one). Ranked last of the non-dead rows
       because it can only ever DELAY a release, never cause a wrong one.
    6. **dead** — nobody owns it, nothing started. ``farm_stall`` still holds this for
       its dwell before acting: one poll is not evidence.
    """
    if not evidence.connected or not evidence.state_fresh or evidence.offline_stalled:
        return "offline"

    if not evidence.live_state or evidence.live_state in ACTIVE_PRINT_STATES:
        return "started"
    if evidence.archive_printing:
        return "started"
    # Only a positive ``same``: an id missing on either side is ``unknown`` — no
    # corroboration, never a reason to read the claim as started.
    if same_job(evidence.live_subtask, evidence.dispatch_subtask) == "same":
        return "started"

    if evidence.watchdog_live:
        return "watchdog_owns"
    if evidence.claim_age_s is None or evidence.claim_age_s < DISPATCH_START_BUDGET_S:
        return "too_fresh"
    if evidence.recovery_acting:
        return "recovery_acting"
    return "dead"
