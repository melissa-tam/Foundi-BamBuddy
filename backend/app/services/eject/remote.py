"""Shared part-present eject dispatcher + the events (and the self-heal bounds) that bound a dispatched sweep.

The eject sweep is a SEPARATE, server-dispatched, motion-only job — used by two
callers that share this ONE path:

- **Production loop**: the eject monitor, once the live bed reaches the release
  threshold, dispatches the eject for the finished unit (``purpose="production"``).
- **First article**: an operator approval with ``eject_remotely`` dispatches the
  eject for the FA plate (``purpose="fa"``).

Both build a standalone motion-only ``.gcode.3mf`` (``build_part_present_eject_file``),
FTPS-upload it and ``project_file``-dispatch it through the record (``printer_action.eject_start``)
with EVERY pre-print calibration OFF (never bed-probe / shake with a part on the
plate), then CLAIM the printer through the plate-occupancy authority
(``claim_for_eject``) so the terminal handler can match the job's echoed
``subtask_id`` and act on completion. The plate-clear gate is NOT cleared here — it
drops only when the eject job's terminal is positively matched.

Since the 2026-08-30 cut-over this module holds NO eject state of its own: the one
pending-eject record per printer lives in ``services/plate_occupancy`` (the authority)
and its durable mirror is written by ``plate_occupancy_store``. What stays here are
the two DISPATCHERS and what bounds a dispatched sweep. EVENTS decide first; production's
two self-heal bounds are the fallback when the printer stays silent (operator ruling
2026-10-12: "The behaviour should stay the same for the most part, im not asking to change
behaviour, i was asking to not rely on timers only as it's finnikey. so if it was self
heal, it should stay self heal."):

* did the printer ever begin it? — the record's answer to the sweep's ``eject_start``
  action (:func:`on_action_answered`): a start the printer refused, never received or
  dropped retires the claim at once. The fallback is the START bound
  (:data:`_EJECT_START_SELF_HEAL`, 180 s of the printer's ONLINE time from the send, armed
  on the action itself): a start the record still holds unanswered past it, with no PRINT
  START echo, is retired by :func:`on_self_heal_due` exactly as production retired it
  (claim dropped, plate gated for a person, page, sweep file removed);
* how far has it got? — the block's M73 phase beacons and the job's terminal, behind
  :func:`_runtime_watchdog`. Two stops on elapsed time remain, both protective and
  fail-closed (a stopped sweep resolves ``unverified``; the plate stays gated): the bed-drop
  guard (no Z telemetry exists, so the drop's budget is the only pre-sweep signal a stalled
  drop gives) and the WHOLE-JOB bound (:func:`_whole_job_bound`: expected runtime + clamp(25 %,
  20 s, 60 s) from the PRINT START echo, read through ``printer_action.bound_due``), which
  binds until a fresh park-beacon (P75) sample proves the sweep is over (a block with no
  bed-drop phase has no beacon lane: until its terminal) — production's rule since 2026-07-31.

Both write their verdicts back through the authority's transitions.

Process memory and its rehydrate story: :data:`_runtime_watchdogs` (one watch task per
printer with a STARTED sweep). None is needed: a pending rebuilt at startup is ``hydrated``
and carries no expected runtime, so no watch arms over it, and the session start's eject
reconcile (``eject.monitor.reconcile_pending_eject``) decides it against the printer's own
report. The START bound adds no process memory of this module's: it lives on the record's
``eject_start`` action. ACROSS A RESTART the start bound is not re-armed (no bound factory is
registered for ``eject_start``): the pending comes back ``hydrated`` with no start action,
which ``plate_occupancy.expire_eject_start`` refuses anyway, and the reconcile resolves it on
the printer's first fresh report — production's restart behaviour (its timer task died with
the process and the startup reconciler owned the record). The whole-job bound counts ONLINE
seconds from ``PendingEject.started_at``; offline time is not counted.

Failures raise :class:`EjectDispatchError` (a plain domain error carrying an HTTP
status hint plus a stable machine ``code``); the FA route wraps it in an
``HTTPException`` while the monitor lets it propagate as a dispatch failure it retries.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, assert_never

from backend.app.core.tasks import spawn_background_task
from backend.app.models.eject_profile import EjectProfile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services import printer_action
from backend.app.services.command_outcome import Answer
from backend.app.services.eject import progress as eject_progress
from backend.app.services.eject.dispatch import build_part_present_eject_file
from backend.app.services.eject.generator import (
    EJECT_RUNTIME_OVERHEAD_S,
    PHASE_BEACON_LIFTED_PCT,
    PHASE_BEACON_PARK_PCT,
    PHASE_BEACON_REFERENCED_PCT,
    PHASE_BEACON_SWEEP_PCT,
)
from backend.app.services.eject.geometry import GeometryUnavailable, get_geometry_required
from backend.app.services.plate_occupancy import (
    EjectIdentity,
    EjectPurpose,
    Evidence,
    PendingEject,
    TransitionRefusal,
    plate_occupancy,
)
from backend.app.services.printer_manager import printer_manager
from backend.app.services.usb_uploads import upload_in_flight
from backend.app.utils.filename import derive_remote_filename

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.app.services.farm_correlation import DispatchDonor
    from backend.app.services.printer_events import ActionAnswered, SelfHealDue

logger = logging.getLogger(__name__)

# Re-exported so the eject lane's own callers keep one import site for the vocabulary
# they dispatch with. The definitions live in the authority — this module stores none
# of it.
__all__ = [
    "EjectDispatchError",
    "EjectPurpose",
    "PendingEject",
    "dispatch_foreign_eject",
    "dispatch_identified_foreign_eject",
    "dispatch_part_present_eject",
    "eject_abort_deadline_s",
    "expected_eject_stem",
    "is_eject_job_name",
    "matches_pending_eject",
    "on_action_answered",
    "on_eject_start_echo",
    "on_self_heal_due",
    "parse_eject_job_name",
    "redrive_eject_stop",
    "z_reference_evidence",
]

# printer_id -> the armed runtime watchdog for that printer's in-flight eject.
# One entry at most: an eject is one-per-printer by construction, and every path
# that ends an eject (resolution, or a new dispatch) drops the entry.
_runtime_watchdogs: dict[int, asyncio.Task] = {}

# The START bound (restored 2026-10-12, operator ruling: "if it was self heal, it should stay
# self heal") — how long a dispatched eject may go without the printer echoing its PRINT START
# before the farm concludes the sweep will never happen. The record's answer to the start
# decides first (:func:`on_action_answered`); this is the fallback for a printer that says
# NOTHING, armed on the ``eject_start`` action at its send and counted in the printer's ONLINE
# seconds by the record (:func:`on_self_heal_due` takes the fallback).
#
# The firmware silently IGNORES a ``project_file`` sent while it is busy — no error, no
# terminal, nothing — which is how the 2026-08-30 ejects on printers 2/3/4
# (``eject_manual_p2`` / ``p3`` / ``p4``, dispatched into prints that had just started) stayed
# registered forever and made every later eject 409 ``eject_in_flight``, until the operator
# hand-jogged the toolhead.
#
# The fleet's start echo is ~45 s from dispatch (44 s and 43 s measured on 2026-08-30), so 4×
# that tolerates a slow FTPS pass plus a missed status push and still frees the printer inside
# three minutes. Production's figure, unchanged.
_EJECT_START_SELF_HEAL = printer_action.SelfHealBound(
    "_EJECT_START_SELF_HEAL",
    180.0,
    "fleet start echo ~45 s from dispatch (44/43 s measured 2026-08-30); production EJECT_START_TIMEOUT_S",
)

# The WHOLE-JOB bound (restored 2026-10-12, the same ruling) — how far past its estimate an
# eject may run before the watchdog STOPS the job. Production's rule since 2026-07-31.
#
# Calibrated on the 2026-07-31 gouged-plate incident (012-H2S): an ejected part lodged under
# the heatbed, the next eject's bed-drop — an OPEN-LOOP absolute move, since the validator
# forbids re-homing Z with a part on the plate — stalled against it, lost steps, and returned
# the bed too high, so the sweep gouged the build plate. The job still reported ``completed``
# (179 s against an 80-83 s band): there is no Z telemetry in MQTT and no HMS fires for a
# silent stall, leaving RUNTIME as the only observable signature.
#
# 11 measured production ejects all ran 80-83 s against an 83 s estimate (the estimator lands
# within ~3 s of the machine), so that profile's deadline is ~104 s. Nominal ejects finish with
# ~20 s of margin to spare.
#
# The 20 s FLOOR protects against TELEMETRY, not motion: it is the printer's FINISH echo over
# MQTT that ends the watch, so on a short profile the floor is what keeps an ordinary
# connection hiccup from aborting a perfectly healthy sweep. The 60 s CAP stops a long
# multi-pass eject (~10 min) from inheriting minutes of scraping margin, which a pure ×1.5
# factor would hand it.
#
# It binds in every phase where the plate may still be at risk — until a FRESH sample at the
# park beacon (P75) proves the sweep is over; a block with no bed-drop phase is watched in
# production's single-deadline form, until its terminal — and needs no evidence at all. The epilogue
# deadline that once replaced it after P75 stays DELETED: it killed harmless firmware tails
# into human gates (23 of 24 beacon-era kills, 2026-08-31).
EJECT_ABORT_MARGIN_FRAC = 0.25
EJECT_ABORT_MARGIN_MIN_S = 20.0
EJECT_ABORT_MARGIN_MAX_S = 60.0

# NAMED EXCEPTION (the plan's named exceptions: no wire event exists; it decides only a
# protective, fail-closed act) — how far past its own budget the BED-DROP PHASE may run
# before the watchdog stops the job. Behaviour unchanged since 2026-08-31.
#
# Why a phase clock is admissible here: the 2026-07-31
# gouged-plate incident (H2S) — an ejected part lodged under the heatbed, the next eject's
# bed-drop (an OPEN-LOOP absolute move: the validator forbids re-homing Z with a part on
# the plate) stalled against it, lost steps and returned the bed too high, so the sweep
# gouged the build plate. The job still reported ``completed``: there is no Z telemetry in
# MQTT and no HMS fires for a silent stall, so the drop phase's RUNTIME is the only
# pre-sweep signature that failure has. The guard claims nothing — it stops the motion and
# the stopped sweep's terminal resolves ``unverified`` (the plate stays gated for a person).
#
# Measured from an OBSERVED edge (the M73 P5 beacon reflected in `mc_percent`), not from
# dispatch: upload, job spin-up and start-echo latency are all excluded. The 8 s FLOOR is
# the sum of what can still be off — the estimator's ~±3 s, the printer's ~1 Hz push
# cadence and this watchdog's own poll interval — and the 20 s CAP keeps a long multi-pass
# drop from inheriting minutes of stall budget.
EJECT_DROP_MARGIN_FRAC = 0.25
EJECT_DROP_MARGIN_MIN_S = 8.0
EJECT_DROP_MARGIN_MAX_S = 20.0

# Poll CADENCE of the phase-edge lane (decides nothing by itself): it reads an in-memory
# field the MQTT session already maintains — no wire traffic, no DB — so the cost is a
# wake-up per printer with a sweep in flight.
_PROGRESS_POLL_S = 2.0

# The drop guard's EVIDENCE gate (part of the named exception above, never an outcome): the
# maximum age of a `mc_percent` sample that may be treated as a phase report. Pushes arrive
# at ~1 Hz while a job runs, so anything older is a silent link: it can never advance a
# phase or justify a stop — stale evidence always lands on the side of NOT stopping.
_PROGRESS_FRESH_S = 30.0

# Why a mid-flight stop fired. "total" = the whole-job bound passed (:func:`_whole_job_bound`,
# production's 2026-07-31 rule, restored 2026-10-12); "drop" = the bed-drop phase overran with
# the sweep still unreached; "drop_late" = the sweep beacon did arrive, but only after the drop
# phase had already overrun. Those three are the stops made on elapsed time; there is no
# epilogue stage (its deadline stays deleted: it killed harmless firmware tails).
#
# There is deliberately no "sweep" token: the sweep lane MEASURES and does not kill (the
# P50→P75 distribution it logs is what a calibrated sweep guard would be armed from), so a job
# overrunning its sweep budget is stopped by the whole-job bound under "total" — production's
# timing, unchanged.
#
# "power_loss" is the one stage NO deadline produces: it is the pause-recovery lane
# re-driving a kill the watchdog already decided but could not deliver, because the
# printer was off the wire when it fired (2026-09-04 outage). The stage exists so the
# kill's copy can say what actually happened rather than borrowing a deadline's wording.
#
# "service_hold" and "reconnect" are the other two deadline-less stages, both added
# 2026-09-12 off the 001/009-H2S maintenance incident. "service_hold" is an operator
# entering maintenance mode on a printer mid-sweep — the farm stops the sweep while it
# still has a session, rather than letting the toggle orphan it. "reconnect" is the
# other half of that shape: the printer came back with the sweep still running after
# its stop had already been decided and could not be delivered, so the kill is
# re-driven on the session that just opened. Each gets its own wording for the same
# reason power_loss does — a stage that borrows a deadline's sentence reports timing
# evidence nobody measured.
EjectStopStage = Literal["total", "drop", "drop_late", "power_loss", "service_hold", "reconnect"]

# Where the phase poller is in the block. Each is handled by its OWN branch that
# terminates the iteration — a phase falling through into another phase's rules is how a
# percent at the PARK beacon could be answered with a bed-drop kill. A fresh park beacon ENDS
# the poll: past it no motion over the plate remains, so nothing is left for the drop guard or
# the whole-job bound to protect, and the job's terminal answers the rest.
_WatchPhase = Literal["await_p2", "await_p5", "await_p50", "await_p75"]

# Reason string for the evidence pushall requested when the phase lane goes blind.
_EVIDENCE_REASON = "eject_phase_beacon_stale"


def _clamped_margin_s(base_s: float, frac: float, min_s: float, max_s: float) -> float:
    """``base_s * frac`` clamped to [``min_s``, ``max_s``] — the ONE clamp the margins use."""
    return min(max(base_s * frac, min_s), max_s)


def _abort_margin_s(expected_total_s: float) -> float:
    """Grace added to the whole-job estimate before the eject is aborted (25%, [20 s, 60 s])."""
    return _clamped_margin_s(
        expected_total_s, EJECT_ABORT_MARGIN_FRAC, EJECT_ABORT_MARGIN_MIN_S, EJECT_ABORT_MARGIN_MAX_S
    )


def eject_abort_deadline_s(expected_runtime_s: float) -> float:
    """Seconds of execution after which an eject is aborted mid-flight.

    ``expected`` plus a margin of 25% of the estimate, clamped to [20 s, 60 s] — production's
    figure since 2026-07-31 (the gouged plate)."""
    return expected_runtime_s + _abort_margin_s(expected_runtime_s)


def _whole_job_bound(expected_runtime_s: float) -> printer_action.SelfHealBound:
    """The sweep's WHOLE-JOB self-heal bound: :func:`eject_abort_deadline_s` of its build's estimate,
    counted in the printer's online seconds from the PRINT START echo (``PendingEject.started_at``).

    Built per sweep because the estimate is the build's; anchored at the echo — the printer's own
    statement that it BEGAN the sweep — never at the start action's REACHED answer, which the
    start family's any-word rule can give at a FINISH / IDLE / PREPARE frame before the sweep
    runs."""
    return printer_action.SelfHealBound(
        "_EJECT_WHOLE_JOB_SELF_HEAL",
        eject_abort_deadline_s(expected_runtime_s),
        "2026-07-31 gouged plate (012-H2S: a stalled bed-drop ran 179 s against an 80-83 s band and still "
        "echoed completed); 11 production ejects ran 80-83 s against an 83 s estimate",
    )


def _drop_margin_s(drop_span_s: float) -> float:
    """Grace added to the bed-drop phase's budget before the eject is aborted (25%, [8 s, 20 s])."""
    return _clamped_margin_s(drop_span_s, EJECT_DROP_MARGIN_FRAC, EJECT_DROP_MARGIN_MIN_S, EJECT_DROP_MARGIN_MAX_S)


def _sweep_margin_s(sweep_span_s: float) -> float:
    """Grace added to the sweep phase's budget before the overrun is REPORTED (25%, [8 s, 20 s]).

    Borrows the drop lane's triple on purpose. The P50→P75 span has never been measured
    — the five kills that read 50% were "mid-sweep or stale", indistinguishable before
    the park beacon was consumed — so no calibrated figure exists yet and inventing one
    would be a third constant with no evidence behind it. Nothing is stopped on this
    number: it only decides when a WARNING is worth emitting, and the
    distribution those warnings produce is what a calibrated floor will be set from.
    """
    return _clamped_margin_s(sweep_span_s, EJECT_DROP_MARGIN_FRAC, EJECT_DROP_MARGIN_MIN_S, EJECT_DROP_MARGIN_MAX_S)


# The canonical eject-job-name convention, minted at dispatch. Two shapes:
#   * queue-item-bound (production / first-article): ``eject_{purpose}_item{queue_item_id}``
#   * foreign-plate manual eject (no queue item): ``eject_manual_p{printer_id}``
# Case-insensitive; the printer echoes the stem verbatim as ``subtask_name`` (it is
# derived from the dispatched filename in ``bambu_mqtt.start_print``). For the manual
# form the trailing integer is the PRINTER id (there is no queue item).
_EJECT_NAME_RE = re.compile(r"^eject_(?:(fa|production)_item|manual_p)(\d+)$", re.IGNORECASE)


async def unit_releasable(queue_item_id: int, *, for_first_article: bool = False) -> bool:
    """May the queue item that raised the gate be swept off automatically?

    True only when that SPECIFIC item (``db.get``, never "the most recently started item
    on the printer" — S4/P1-A) still exists and carries an eject profile that still
    exists. The temperature it waits for is NOT an item fact (2026-09-25, user ruling: ONE
    value — :mod:`~backend.app.services.eject.shop_air` owns the line), so this answers only
    the identity half.

    ``for_first_article=False`` (production watch) answers False for an FA item: a
    finished first article holds for operator approval, never auto-ejects.
    ``for_first_article=True`` is the approve-with-remote-eject path, where the operator
    HAS approved, so the FA guard is skipped for its deferred (cooldown-gated) eject.

    Lives in the eject lane module every reader already imports — the cooldown watch, the manual
    eject and ``farm_policy``'s first-article approval — so ``farm_policy`` imports no eject
    watch (owner 6, 2026-10-11: the ``farm_policy`` ↔ ``eject.monitor`` cycle is gone)."""
    from backend.app.core.database import async_session

    async with async_session() as db:
        item = await db.get(PrintQueueItem, queue_item_id)
        if item is None or item.eject_profile_id is None:
            return False
        # First-article items carry an eject profile but must hold on the plate
        # for inspection — the production watch must NOT auto-eject them. Only
        # the explicit post-approval FA path may release them.
        if getattr(item, "first_article", False) and not for_first_article:
            return False
        return await db.get(EjectProfile, item.eject_profile_id) is not None


def _eject_name_stem(name: str) -> str:
    """Strip any leading path + repeated ``.gcode.3mf`` / ``.3mf`` / ``.gcode``
    suffixes, leaving the bare job stem for name matching."""
    base = str(name).strip().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    while True:
        low = base.lower()
        if low.endswith(".gcode.3mf"):
            base = base[:-10]
        elif low.endswith(".3mf"):
            base = base[:-4]
        elif low.endswith(".gcode"):
            base = base[:-6]
        else:
            break
    return base


def parse_eject_job_name(name: str | None) -> tuple[EjectPurpose, int] | None:
    """``(purpose, id)`` parsed from an eject job's echoed name/filename, or None
    when ``name`` is not one of our eject jobs.

    The trailing int is the QUEUE-ITEM id for ``fa``/``production`` jobs and the
    PRINTER id for a ``manual`` (foreign-plate) job. Every consumer uses only the
    truthiness of this result (``is_eject_job_name``), so the id's meaning per
    purpose does not leak."""
    if not name:
        return None
    m = _EJECT_NAME_RE.match(_eject_name_stem(name))
    if not m:
        return None
    purpose = m.group(1).lower() if m.group(1) else "manual"
    return (purpose, int(m.group(2)))  # type: ignore[return-value]


def is_eject_job_name(name: str | None) -> bool:
    """True when ``name`` (a subtask_name or filename) is one of our eject jobs."""
    return parse_eject_job_name(name) is not None


def expected_eject_stem(pending: PendingEject | EjectIdentity, printer_id: int) -> str | None:
    """THE eject job stem: the one builder of the name a sweep is uploaded, started, matched and
    removed under (the convention above :data:`_EJECT_NAME_RE`).

    ``eject_{purpose}_item{queue_item_id}`` for a unit-bound sweep, ``eject_manual_p{printer_id}``
    for the foreign-plate manual sweep, which has no unit. Accepts the record or its identity
    projection — both carry the two fields the stem is minted from, and the matcher and the start
    deadline only ever hold the projection. None when a unit-bound record carries no unit id: such
    a record cannot name a file, so nothing is matched or removed by name.
    """
    if pending.purpose == "manual":
        return f"eject_manual_p{printer_id}"
    if pending.queue_item_id is None:
        return None
    return f"eject_{pending.purpose}_item{pending.queue_item_id}"


def _dispatch_stem(pending: PendingEject, printer_id: int) -> str:
    """The stem a sweep is dispatched under — :func:`expected_eject_stem`, refused when it cannot
    name the file (a unit-bound sweep with no unit), before anything reaches the printer."""
    stem = expected_eject_stem(pending, printer_id)
    if stem is None:
        raise EjectDispatchError(f"A {pending.purpose} eject needs its queue unit to name its file", status_code=409)
    return stem


def _eject_remote_path(job_stem: str) -> str:
    """The storage path a sweep of this stem is uploaded to — the dispatcher's own naming rule."""
    return f"/{derive_remote_filename(f'{job_stem}.gcode.3mf')}"


# --------------------------------------------------------------------------- #
# The sweep's start: its echo, and the record's answer to its eject_start action
# --------------------------------------------------------------------------- #
def on_eject_start_echo(printer_id: int) -> None:
    """The printer echoed PRINT START for the eject sweep on ``printer_id``.

    Called from the print-START callback — i.e. when the printer says it BEGAN
    executing the eject file, not when we uploaded or commanded it. Only that edge
    measures machine time: upload + job spin-up vary with file size and FTPS
    conditions and would otherwise be charged to the sweep.

    Two duties, in order:

    1. stamp the start through the authority (:meth:`note_eject_started`, first-write
       -wins, so a replayed echo can never shorten a measured runtime). The stamp is what
       ends the START bound's reach (the authority refuses to expire a started sweep) and
       is the WHOLE-JOB bound's anchor;
    2. ARM the runtime watch (:func:`_runtime_watchdog`), this being the first moment a
       deadline can be measured from.

    The watch arms on the FIRST echo only, and only for a verifiable sweep: a pending
    whose ``started_at`` was already stamped is a duplicate echo, a pending with no
    ``expected_runtime_s`` is a rehydrated post-restart record (nothing to judge against —
    the session start's reconcile owns those, fail-closed), and a printer that already
    carries a watch is not given a second one.

    An echo with no record is IGNORED (production's path): a sweep the start bound already
    retired gets no stamp and no watch, and its plate stays gated for the person the expiry
    paged — the late sweep's terminal finds no eject to resolve and clears nothing.
    """
    before = plate_occupancy.eject_identity(printer_id)
    if before is None:
        return
    plate_occupancy.note_eject_started(printer_id)

    after = plate_occupancy.eject_identity(printer_id)
    if after is None or after.started_at is None or before.started_at is not None:
        # Not the first echo (or the record went away under us) — never re-arm.
        return
    pending = plate_occupancy.pending_eject_view(printer_id)
    if pending is None or pending.expected_runtime_s is None or printer_id in _runtime_watchdogs:
        return
    _runtime_watchdogs[printer_id] = spawn_background_task(
        _runtime_watchdog(printer_id, pending), name=f"eject-runtime-watchdog-{printer_id}"
    )


def cancel_eject_timers(printer_id: int) -> None:
    """Drop the runtime watch for ``printer_id``. Synchronous, idempotent, never raises.

    THE one deregistration point, called from the occupancy policy driver on every
    transition that leaves the printer with no eject — a matched terminal, an
    unverified resolve, a start retired never-started (the record's answer or the start
    bound), the session reconcile's disposal, an operator recover. Routing all of them through one
    level-triggered check is what stops a resolved eject leaving a task armed to stop a
    printer that already finished; the watch's own identity re-check remains the belt
    for a cancel that lands after its deadline has already elapsed.
    """
    watchdog = _runtime_watchdogs.pop(printer_id, None)
    if watchdog is not None and not watchdog.done():
        watchdog.cancel()


# The record's answers that mean the printer NEVER began the sweep (``command_outcome``'s start
# family): its fail ACK or a frame that never left (REFUSED), a session that began with the start
# unacknowledged, a reboot, or the busy job the start was sent into ending without ours (VOID), and
# another job taking the printer (TAKEN_OVER).
_NEVER_STARTED: frozenset[str] = frozenset({Answer.REFUSED.value, Answer.VOID.value, Answer.TAKEN_OVER.value})


async def on_action_answered(event: ActionAnswered) -> None:
    """HUB SUBSCRIBER (``printer_event_wiring``: ``ActionAnswered``): the record answered a sweep's
    ``eject_start`` — when the printer never began it, the claim is retired (:func:`_retire_never_started`).

    The event decides first: the firmware silently IGNORES a ``project_file`` sent while it is busy
    (2026-08-30: ``eject_manual_p2`` / ``p3`` / ``p4``, dispatched into prints that had just started,
    sat registered and 409'd every later eject ``eject_in_flight``), and the record answers exactly
    that shape — the busy job's end with ours never reported is VOID — however soon it comes. A start
    the printer has not answered at all is the START bound's (:func:`on_self_heal_due`). A reached
    start needs nothing here — its echo (:func:`on_eject_start_echo`) armed the runtime watch.
    Never raises."""
    if event.command != "eject_start" or event.answer not in _NEVER_STARTED:
        return
    try:
        pending = plate_occupancy.pending_eject_view(event.printer_id)
        if pending is None or pending.start_action_id != event.action_id:
            return  # not this printer's live sweep (resolved, superseded, or a hydrated record)
        await _retire_answered_start(event.printer_id, Answer(event.answer), event.reason)
    except Exception:  # noqa: BLE001 — the hub must keep delivering
        logger.exception("eject.remote: applying eject start action %s failed", event.action_id)


async def on_self_heal_due(event: SelfHealDue) -> None:
    """HUB SUBSCRIBER (``printer_event_wiring``: ``SelfHealDue``): a sweep's ``eject_start`` is still
    unanswered past :data:`_EJECT_START_SELF_HEAL` (180 s of the printer's online time from the send) —
    production's fallback: the eject the printer never started is retired, a person is paged and the
    sweep file is removed (:func:`_retire_never_started`); the plate stays gated.

    The authority decides whether the expiry fires: it refuses a STARTED record (the PRINT START echo
    stamped ``started_at``) and a hydrated one, so a bound passing over a sweep that did begin — or a
    start whose claim is gone or superseded — changes nothing. The action itself stays outstanding: a
    late answer still lands on the record, and a late PRINT START echo then finds no record
    (:func:`on_eject_start_echo` ignores it, production's path). Never raises."""
    if event.command != "eject_start" or event.bound_name != _EJECT_START_SELF_HEAL.name:
        return
    try:
        pending = plate_occupancy.pending_eject_view(event.printer_id)
        if pending is None or pending.start_action_id != event.action_id:
            return  # not this printer's live sweep (resolved, superseded, or a hydrated record)
        seconds = _EJECT_START_SELF_HEAL.seconds
        await _retire_never_started(
            event.printer_id,
            situation=(
                f"never echoed a PRINT START for its eject within {seconds:.0f}s — the firmware ignores a "
                "project_file sent while it is busy, so the sweep never ran"
            ),
            clause=f"within {seconds:.0f}s",
            remove_file=True,
        )
    except Exception:  # noqa: BLE001 — the hub must keep delivering
        logger.exception("eject.remote: the eject start bound of action %s failed", event.action_id)


async def _retire_answered_start(printer_id: int, answer: Answer, reason: str | None) -> None:
    """Retire an eject whose start the record answered never-started (:data:`_NEVER_STARTED`): the copy
    names the answer, and the sweep file is removed when the record proves the file never ran —
    REFUSED or VOID. A TAKEN_OVER start keeps its file: the job now on the printer may BE that file,
    started from the screen under a firmware-minted id, and a file a job is running is never deleted
    under it."""
    why = reason or {
        Answer.REFUSED: "the printer refused the start",
        Answer.VOID: "the printer never received it",
        Answer.TAKEN_OVER: "another job took the printer",
    }.get(answer, answer.value)
    await _retire_never_started(
        printer_id,
        situation=f"never started its eject sweep — the start was {answer.value} ({why})",
        clause=f"({why})",
        remove_file=answer is not Answer.TAKEN_OVER,
    )


async def _retire_never_started(printer_id: int, *, situation: str, clause: str, remove_file: bool) -> None:
    """THE retirement of an eject the printer never started, whichever decided it — the record's answer
    (:func:`_retire_answered_start`) or the START bound (:func:`on_self_heal_due`): the authority's
    ``expire_eject_start`` drops the claim and leaves the plate gated as a person's, a page goes out at
    once, and — when ``remove_file`` — the sweep file, this lane's own footprint that will never run, is
    removed. ``situation`` completes the WARNING, ``clause`` the page's "never started it …" sentence.

    The authority decides whether the expiry fires (it refuses a started or hydrated record), so a late
    or duplicate trigger is harmless. The file is named from the record through
    :func:`expected_eject_stem` (the one builder), never re-spelled here."""
    # Read BEFORE the expiry drops the record: the stem names the file to remove afterwards.
    identity = plate_occupancy.eject_identity(printer_id)
    if not plate_occupancy.expire_eject_start(printer_id):
        return
    logger.warning(
        "eject.remote: printer %s %s; pending dropped and the plate stays gated for a human", printer_id, situation
    )
    from backend.app.services.eject.monitor import notify_plate_not_empty

    await notify_plate_not_empty(
        printer_id,
        source_detail=(
            f"the eject sweep was dispatched but the printer never started it {clause} — the plate has NOT "
            "been swept. Clear it by hand, or eject again now that the printer is free."
        ),
    )
    stem = expected_eject_stem(identity, printer_id) if identity is not None else None
    if stem is None or not remove_file:
        return
    from backend.app.core.database import async_session
    from backend.app.services.bambu_ftp import remove_abandoned_upload

    async with async_session() as db:
        printer = await db.get(Printer, printer_id)
    if printer is not None:
        await remove_abandoned_upload(
            printer, _eject_remote_path(stem), lane="eject", why="the printer never started the sweep"
        )


def _live_phase_telemetry(printer_id: int) -> tuple[int | None, float | None, float | None]:
    """``(mc_print_line_number, mc_percent, progress_wire_at)`` off the live session.

    Every element is None-tolerant: None whenever there is no live session, and
    ``mc_print_line_number`` is additionally absent on the H2S wire — it stays a
    stall-localization breadcrumb for the models that do publish it and is NEVER a
    decision input.

    ``mc_percent`` IS a decision input (the eject block's M73 phase beacons are what
    drive it), which is exactly why ``progress_wire_at`` travels with it: the percent
    field always holds *some* value, so only its recency stamp can say whether that
    value describes the phase running now or one that was reported before this eject
    even started.
    """
    client = printer_manager.get_client(printer_id)
    state = getattr(client, "state", None)
    if state is None:
        return None, None, None
    return (
        getattr(state, "mc_print_line_number", None),
        getattr(state, "progress", None),
        getattr(state, "progress_wire_at", None),
    )


def _elapsed_since_start_s(pending: PendingEject | EjectIdentity) -> float:
    """Seconds of machine time since the printer echoed this eject's START (0.0 if unstamped)."""
    if pending.started_at is None:
        return 0.0
    return (datetime.now(timezone.utc) - pending.started_at).total_seconds()


def _watchdog_still_owns(printer_id: int, armed: PendingEject) -> EjectIdentity | None:
    """The printer's eject IDENTITY iff it is still the eject this watchdog armed for.

    None means resolved or superseded — a terminal was handled while we slept, or a new
    eject was dispatched onto the printer. Stopping on either would abort someone else's
    job, so every rule re-checks this before it can act. Compared against the authority
    (never a copy this task holds), because the record it must not act on is precisely
    one that changed underneath it."""
    current = plate_occupancy.eject_identity(printer_id)
    if current is None or (current.queue_item_id, current.purpose, current.started_at) != (
        armed.queue_item_id,
        armed.purpose,
        armed.started_at,
    ):
        return None
    return current


def _whole_job_passed(printer_id: int, bound: printer_action.SelfHealBound, current: EjectIdentity) -> bool:
    """Has the sweep's whole-job bound passed — in the printer's ONLINE seconds since its PRINT START
    echo (``started_at``, tz-aware UTC, handed to the record as naive UTC)? The elapsed time is the
    record's (``printer_action.bound_due``); a pass is logged once through ``note_bound_passed``, and the
    caller stops the sweep at once, so it is never asked again. An unstamped record (never the case
    for a watch, which arms on the echo) has nothing to count from."""
    if current.started_at is None:
        return False
    anchor = current.started_at.astimezone(timezone.utc).replace(tzinfo=None)
    if not printer_action.bound_due(printer_id, bound, anchor):
        return False
    printer_action.note_bound_passed(printer_id, bound, lane="eject whole-job")
    return True


def _stop_reason(
    stage: EjectStopStage,
    *,
    phase_elapsed_s: float,
    drop_span_s: float | None,
    deadline_s: float = 0.0,
    expected_s: float = 0.0,
    sweep_span_s: float | None = None,
) -> tuple[str, str]:
    """(diagnostic clause for the kill WARNING, operator sentence for the page).

    THE one origin of kill copy. Both renderings say the same thing to two audiences, so
    a stage cannot be honest in the log and wrong in the page. Only the timed stops carry
    runtime figures: ``drop_span_s`` / ``sweep_span_s`` are the phase budgets IN FORCE when
    the stop fired (a phase budget is in force only once that phase was OBSERVED to open),
    ``deadline_s`` is the deadline that fired, in seconds since the start echo, and
    ``phase_elapsed_s`` is measured from the edge that opened the phase the stage names —
    the whole job's elapsed time where no phase edge was observed. The other three stages
    are a person's act or a re-drive, and invent no timing evidence.

    The two "total" renderings are production's: a sweep budget in force can only have been
    armed by an on-time P50 edge, so the bed-drop demonstrably cleared and blaming an
    under-bed obstruction would be a guess the wire contradicts; with no beacons observed the
    obstruction wording stands.
    """
    match stage:
        case "total":
            if sweep_span_s is not None:
                return (
                    f"the bed-drop phase cleared on budget and the job was still sweeping {phase_elapsed_s:.0f}s "
                    f"after the sweep beacon (budget {sweep_span_s:.0f}s) when the whole-job deadline "
                    f"({deadline_s:.0f}s) fired — suspect a part that did not release",
                    f"the eject sweep was STOPPED at its whole-job deadline after {phase_elapsed_s:.0f}s of sweeping "
                    f"(a {sweep_span_s:.0f}s sweep budget) — the bed-drop phase had already cleared, so the part "
                    "may not have released from the plate. Inspect the build plate before clearing it.",
                )
            return (
                "suspect an under-bed obstruction or Z steps lost during the bed-drop",
                f"the eject sweep was STOPPED mid-job after {phase_elapsed_s:.0f}s against an expected "
                f"{expected_s:.0f}s — it may have stalled against an obstruction. "
                "Check under the heatbed and inspect the build plate before clearing it.",
            )
        case "power_loss":
            # No deadline fired and no phase was timed: the printer LEFT THE WIRE with a
            # sweep in flight, so every runtime figure describes a machine nobody was
            # watching. Saying anything about spans here would be inventing evidence.
            return (
                "eject interrupted by a power loss; sweep unverified",
                "power_loss_eject_interrupted",
            )
        case "service_hold":
            # The operator took the printer for maintenance while the sweep was running.
            # Nothing timed out — a human decided — so the copy says who stopped it and
            # what it costs (the plate stays gated, because a stopped sweep is unverified).
            return (
                "eject stopped by the operator entering maintenance mode; sweep unverified",
                "the eject sweep was stopped by the operator entering maintenance mode — the sweep is unverified, "
                "so the plate stays gated. Check the build plate before clearing it.",
            )
        case "reconnect":
            # A timed stop had already fired and went undelivered; the printer is
            # back and STILL running the sweep. No new timing evidence exists — the figures
            # belong to the original kill — so this names the reconnect, not a budget.
            return (
                "printer reconnected with the sweep still running after its deadline had fired; stop re-driven",
                "the printer reconnected with its eject sweep still running after the farm had already decided to "
                "stop it, so the sweep was stopped. The plate has NOT been verified — inspect the build plate "
                "before clearing it.",
            )
        case "drop":
            drop_budget = f"{drop_span_s:.0f}s" if drop_span_s is not None else "its"
            return (
                f"the bed-drop phase has run {phase_elapsed_s:.0f}s against a {drop_budget} budget with the sweep "
                "still unreached — suspect an under-bed obstruction or Z steps lost during the bed-drop",
                f"the eject was STOPPED DURING the bed-drop phase, {phase_elapsed_s:.0f}s into a drop budgeted at "
                f"{drop_budget} with the sweep not yet started, so the bed may be stalled against an obstruction. "
                "Check under the heatbed and inspect the build plate before clearing it.",
            )
        case "drop_late":
            drop_budget = f"{drop_span_s:.0f}s" if drop_span_s is not None else "its"
            return (
                f"the sweep beacon arrived only after the bed-drop phase overran its {drop_budget} budget "
                f"({phase_elapsed_s:.0f}s in) — a long drop is the lost-steps signature, and the sweep opens with "
                "rear positioning at lift height, so this stop still precedes plate contact",
                f"the eject was STOPPED AFTER a bed-drop phase that overran ({phase_elapsed_s:.0f}s in, against a "
                f"{drop_budget} bed-drop budget) — a drop that runs long can lose Z steps and return the bed too "
                "high for the sweep. Check under the heatbed and inspect the build plate before clearing it.",
            )
        case _:
            assert_never(stage)


# What the page says when the stop send found no live MQTT session. One origin, no
# stage variants: the stage decided why the farm wanted the sweep stopped, and this
# sentence is about the one fact that outranks it — the command never reached the
# machine, so nothing about the sweep is settled.
_UNDELIVERED_STOP_DETAIL = (
    "the farm tried to STOP this printer's eject sweep and could not reach the printer (no live connection), "
    "so the sweep may still be running. The plate stays gated and the stop is re-driven when the printer "
    "reconnects. Check the printer before clearing the plate."
)


async def _stamp_stop_and_page(
    printer_id: int,
    *,
    stage: EjectStopStage,
    diagnostic: str,
    source_detail: str,
    situation: str,
) -> None:
    """Stamp the runtime verdict, stop the job mid-flight, page the operator.

    THE one kill path. Both entries — the runtime watch's deadlines (:func:`_stop_and_page`)
    and the re-drive (:func:`redrive_eject_stop`, three callers) — end here, so the ordering
    guarantees hold whatever decided the kill. Callers supply the RENDERED copy
    (``diagnostic`` / ``source_detail`` from :func:`_stop_reason`) and a ``situation``
    clause for the log; nothing here re-derives a judgement of its own.

    The ONE thing only this function knows is whether the stop was actually
    DELIVERED, and the page says so. When the send found no live MQTT session the
    caller's sentence is false — the sweep is still running — so the page is replaced
    by :data:`_UNDELIVERED_STOP_DETAIL`. The stop is never retried on a clock
    (2026-10-11): it is re-sent on the printer's NEXT session, by the session start's
    eject reconcile (``eject.monitor.reconcile_pending_eject`` → :func:`redrive_eject_stop`,
    stage ``reconnect``), which finds the stamped pending unowned (``unowned_eject``) and
    re-drives this same path the moment the printer reports again.

    No escalation watch is armed here: while an eject owns the printer the plate carries
    no watch by construction. The escalation hold arrives one step later and from one
    place — the stopped sweep's terminal resolves ``unverified``, which puts the plate
    under ``EscalationOnly`` and the policy driver arms the hold off THAT."""
    # Stamp the mark FIRST. A terminal racing this task must find the verdict already
    # set: the mark is what keeps the plate gated, so a terminal that slipped past an
    # unmarked pending would release the gate onto a plate the printer was about to be
    # stopped over. First-write-wins in the authority, so a re-drive over an already
    # stamped pending is a no-op here and the stop below is what actually gets redone.
    #
    # RECORDED REFUSAL: this stamp is unconditional across stages, so every stop resolves
    # the eject `unverified` and leaves the plate gated under EscalationOnly. Deliberate,
    # per the 2026-07-31 contract: sweep motion complete is not part off plate, and a farm
    # that clears a gate on a job it had to stop is judging on a criterion other than the
    # one it stopped for.
    plate_occupancy.note_eject_runtime_exceeded(printer_id, datetime.now(timezone.utc), stage)
    logger.warning("eject.remote: %s — %s; stopping the eject job mid-flight", situation, diagnostic)

    # Deliberately NOT via print_control.stop_as_operator: this is not an operator
    # stop, and nothing downstream may read the echoed status as intent. The mark
    # — not whatever terminal the printer reports — drives terminal handling.
    delivered = (
        await printer_action.send(printer_id, printer_action.stop(), actor=printer_action.ACTOR_FARM)
    ).published
    # Proceed either way: an undelivered stop leaves the sweep running, but the mark
    # already guarantees no terminal can release the gate, the operator is paged below,
    # and the next session's reconcile re-drives the stop.
    logger.warning(
        "eject.remote: mid-flight eject stop on printer %s %s",
        printer_id,
        "delivered" if delivered else "COULD NOT BE DELIVERED — re-sent on the printer's next session",
    )

    # Lazy import: the monitor imports this module, so a module-level import here
    # is a cycle (same precedent as farm_policy's monitor imports).
    from backend.app.services.eject.monitor import notify_plate_not_empty

    try:
        await notify_plate_not_empty(printer_id, source_detail=source_detail if delivered else _UNDELIVERED_STOP_DETAIL)
    except Exception:  # noqa: BLE001 — a notify failure must never kill the watch
        logger.exception("eject.remote: mid-flight abort notification failed for printer %s", printer_id)


async def _stop_and_page(
    printer_id: int,
    current: EjectIdentity,
    armed: PendingEject,
    *,
    stage: EjectStopStage,
    phase_elapsed_s: float,
    progress: float | None,
    line_number: int | None,
    deadline_s: float,
    drop_span_s: float | None,
    sweep_span_s: float | None,
) -> None:
    """The runtime watch's entry into the kill path.

    Renders the deadline's own evidence into copy and hands it to
    :func:`_stamp_stop_and_page`. ``progress`` and ``line_number`` are the deciding rule's
    OWN telemetry sample, handed down rather than re-read here: a second read would
    report the state of the printer a moment later than the one the kill was decided on,
    which is the one thing a post-incident reader must be able to trust.
    ``deadline_s`` is the deadline that fired (seconds since the start echo) and
    ``drop_span_s`` / ``sweep_span_s`` the phase budgets in force at that moment
    (:func:`_stop_reason`)."""
    diagnostic, source_detail = _stop_reason(
        stage,
        phase_elapsed_s=phase_elapsed_s,
        drop_span_s=drop_span_s,
        deadline_s=deadline_s,
        expected_s=armed.expected_runtime_s or 0.0,
        sweep_span_s=sweep_span_s,
    )
    situation = (
        f"eject on printer {printer_id} still running at {_elapsed_since_start_s(current):.0f}s "
        f"(expected {armed.expected_runtime_s or 0.0:.0f}s, stage={stage}, deadline {deadline_s:.0f}s, "
        f"bed-drop span {armed.drop_span_s}, gcode line {line_number}, {progress}% done)"
    )
    await _stamp_stop_and_page(
        printer_id,
        stage=stage,
        diagnostic=diagnostic,
        source_detail=source_detail,
        situation=situation,
    )


async def redrive_eject_stop(printer_id: int, *, stage: EjectStopStage) -> bool:
    """Re-drive the ONE eject kill path for a pending sweep whose stop never landed.

    True iff a pending eject was found and the kill was re-driven; False when the
    printer holds no eject (nothing to stop, and nothing to say).

    THREE callers, each naming its own ``stage``: the pause-recovery lane after an
    outage (``power_loss``), the reconnect reconcile when a printer comes back still
    running a sweep whose stop had been decided (``reconnect``), and the service-hold
    quiesce when an operator takes the printer for maintenance mid-sweep
    (``service_hold``). All three are the same act — stop a sweep the farm can no
    longer account for — which is why they share this entry rather than composing
    three kills.

    The 2026-09-04 outage is the shape this exists for: the watchdog fired while the
    printer was off the wire, the stop sends found no session, the task logged and
    exited — and nothing re-sent on reconnect. The pending and its
    ``runtime_exceeded_at`` stayed registered, so every later eject on that printer
    refused ``eject_in_flight`` forever.

    It is a RE-DRIVE, never a second implementation: the stamp-before-stop ordering
    and the page come from :func:`_stamp_stop_and_page`, and the stamp is
    first-write-wins, so re-driving a pending the watch already marked keeps the
    ORIGINAL verdict time and merely redoes the delivery. Nothing here resolves the
    eject: the stopped job's terminal resolves ``unverified`` through
    ``farm_policy.on_terminal``, which is still the one ``resolve_eject`` caller.

    An eject is NEVER resumed (the 2026-07-31 gouged-plate class): a sweep whose
    machine state is unknown may only be stopped."""
    if plate_occupancy.pending_eject_view(printer_id) is None:
        return False
    diagnostic, source_detail = _stop_reason(stage, phase_elapsed_s=0.0, drop_span_s=None)
    await _stamp_stop_and_page(
        printer_id,
        stage=stage,
        diagnostic=diagnostic,
        source_detail=source_detail,
        situation=f"re-driving the eject stop on printer {printer_id} (stage={stage})",
    )
    return True


async def _runtime_watchdog(
    printer_id: int,
    armed: PendingEject,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Stop the eject on ``printer_id`` when it overruns the deadline in force for its phase.

    The machine-stopping half of the 2026-07-31 gouged-plate response, and it acts DURING the
    job rather than at its terminal: the stall accrues in the bed drop/return phase, which
    finishes before the sweep begins, so a job stopped while that phase is still executing has
    not yet dragged the toolhead across the plate. Every stop claims nothing — the stopped
    sweep resolves ``unverified`` and the plate stays gated for a person. Events answer the
    rest: the M73 phase beacons say how far the sweep got, and its terminal ends it.

    Two deadlines, one kill path:

    * The WHOLE-JOB bound (:func:`_whole_job_bound`, restored 2026-10-12 — operator ruling: "if
      it was self heal, it should stay self heal") binds in every phase where the plate may
      still be at risk and needs no evidence at all: expected runtime + clamp(25 %, 20 s, 60 s),
      counted by the record in the printer's ONLINE seconds since the PRINT START echo. It stops
      binding only on a FRESH park-beacon (P75) sample — the sweep provably over — and, as in
      production, only in the phase-edge form: a block with no bed-drop phase has no edge lane,
      so its whole-job bound binds until the terminal (:func:`_watch_whole_job`).
    * The BED-DROP guard (:data:`EJECT_DROP_MARGIN_FRAC`, a named exception: no Z telemetry
      exists) needs ``drop_span_s`` plus the beacons reflected in ``mc_percent``, and is the
      only rule that can fire BEFORE the sweep on a stall too short to overrun the whole job.

    The normal exit is a fresh park beacon or CANCELLATION: :func:`cancel_eject_timers` —
    driven off the occupancy transition that retires the eject — cancels this task the
    instant the eject's terminal is consumed. The identity re-check on every wake-up is the
    belt for the races cancellation loses. ``sleep`` and ``clock`` are injectable together —
    they must come from the same time source — so tests can drive the whole sequence without
    wall-clock waits; the whole-job bound reads the record's clock (``printer_action``)."""
    try:
        if armed.expected_runtime_s is None:
            # A rehydrated pending carries no build figures: nothing to judge against. The arming gate
            # (:func:`on_eject_start_echo`) refuses one; this is the belt.
            return
        whole_job = _whole_job_bound(armed.expected_runtime_s)
        if armed.drop_span_s is None:
            # A drop-less block has no phase edge worth timing — production's single-deadline form.
            await _watch_whole_job(printer_id, armed, whole_job=whole_job, sleep=sleep)
            return
        await _watch_phase_edges(
            printer_id,
            armed,
            reference_s=armed.reference_s,
            drop_span_s=armed.drop_span_s,
            sweep_span_s=armed.sweep_span_s,
            expected_s=armed.expected_runtime_s,
            whole_job=whole_job,
            armed_at=clock(),
            sleep=sleep,
            clock=clock,
        )
    finally:
        _runtime_watchdogs.pop(printer_id, None)


async def _watch_whole_job(
    printer_id: int,
    armed: PendingEject,
    *,
    whole_job: printer_action.SelfHealBound,
    sleep: Callable[[float], Awaitable[None]],
) -> None:
    """The single-deadline form (a block with no bed-drop phase): wake at the poll cadence, stand down
    when the eject is resolved or superseded, stop the sweep under ``"total"`` once the whole-job bound
    passes. No beacon is read, so nothing loosens it before the terminal — production's rule for this
    form. Returns on cancellation, on resolution/supersession, or after the stop."""
    while True:
        await sleep(_PROGRESS_POLL_S)
        current = _watchdog_still_owns(printer_id, armed)
        if current is None:
            return
        if not _whole_job_passed(printer_id, whole_job, current):
            continue
        line_number, percent, _wire_at = _live_phase_telemetry(printer_id)
        # No phase was ever observed here, so no phase budget was in force and the whole job is
        # the only span this kill can honestly speak about.
        await _stop_and_page(
            printer_id,
            current,
            armed,
            stage="total",
            phase_elapsed_s=_elapsed_since_start_s(current),
            progress=percent,
            line_number=line_number,
            deadline_s=whole_job.seconds,
            drop_span_s=None,
            sweep_span_s=None,
        )
        return


async def _watch_phase_edges(
    printer_id: int,
    armed: PendingEject,
    *,
    reference_s: float | None,
    drop_span_s: float,
    sweep_span_s: float | None,
    expected_s: float,
    whole_job: printer_action.SelfHealBound,
    armed_at: float,
    sleep: Callable[[float], Awaitable[None]],
    clock: Callable[[], float],
) -> None:
    """Poll ``mc_percent``, time the M73 phase edges, and enforce the deadline each phase owns: the
    whole-job bound in every phase, the bed-drop guard while the drop runs.

    One branch per :data:`_WatchPhase`, each terminating the iteration. That shape is
    load-bearing, not stylistic: the predecessor skipped the phase rules for a TUPLE of
    states and let everything else fall through into the bed-drop branch, so a phase
    added without also editing that tuple would have answered a percent at the PARK
    beacon with a bed-drop kill.

    Returns on a fresh park beacon, on cancellation, on resolution/supersession, or after a
    kill; the caller owns deregistration."""
    # A block with a Z re-reference phase opens at its beacon; every other block opens
    # where it always did.
    phase: _WatchPhase = "await_p2" if reference_s is not None else "await_p5"
    # Monotonic time of the first fresh sample at/above each beacon, plus the budgets
    # derived from them. Each stays None until its own edge is observed.
    t5: float | None = None
    t50: float | None = None
    drop_deadline: float | None = None
    sweep_budget_deadline: float | None = None
    # The guarded drive's own budget, measured from the start echo (there is no earlier
    # edge to measure from — it is the first thing the block does). It decides only when
    # a WARNING is worth emitting.
    p2_budget = (
        armed_at + EJECT_RUNTIME_OVERHEAD_S + reference_s + _drop_margin_s(reference_s)
        if reference_s is not None
        else None
    )
    # The drop guard cannot arm without the P5 beacon. A healthy block beacons within seconds of
    # its first move, so no P5 past the fixed job spin-up plus the whole-job grace means the
    # beacons are not reaching us (production's line, restored with the whole-job bound so the
    # warning precedes that bound's stop): ONE warning (the dry-run rung's liveness witness), and
    # the guard keeps waiting for a late P5. It decides nothing — the whole-job bound still binds.
    # The re-reference drive genuinely delays P5, so its budget is added — otherwise enabling the
    # prologue would look like beacon-death on every job.
    beacons_silent_at = armed_at + EJECT_RUNTIME_OVERHEAD_S + (reference_s or 0.0) + _abort_margin_s(expected_s)
    warned_beacons_silent = False
    asked_for_evidence = False
    warned_sweep_overrun = False
    warned_reference_overrun = False

    while True:
        await sleep(_PROGRESS_POLL_S)
        now = clock()
        current = _watchdog_still_owns(printer_id, armed)
        if current is None:
            return
        line_number, percent, progress_wire_at = _live_phase_telemetry(printer_id)
        # FRESH = published by a push that landed after this eject started AND recently
        # enough to describe the phase running now. Anything else is not evidence.
        fresh = (
            percent is not None
            and progress_wire_at is not None
            and progress_wire_at >= armed_at
            and now - progress_wire_at <= _PROGRESS_FRESH_S
        )

        # The whole-job bound, evaluated before any phase rule (production's one deadline-selection
        # point), so exactly one rule can decide that this eject has run out of time. ACCEPTED
        # RESIDUE: an edge sample landing on the same poll as the bound's pass resolves in the
        # BOUND's favour — the window is one poll, and that bias stops a machine that may be in
        # trouble rather than granting it the next phase.
        if _whole_job_passed(printer_id, whole_job, current):
            sweeping = phase == "await_p75" and t50 is not None
            await _stop_and_page(
                printer_id,
                current,
                armed,
                stage="total",
                phase_elapsed_s=now - t50 if sweeping and t50 is not None else _elapsed_since_start_s(current),
                progress=percent,
                line_number=line_number,
                deadline_s=whole_job.seconds,
                # A phase budget is IN FORCE only from the edge that opened its phase.
                drop_span_s=drop_span_s if phase in ("await_p50", "await_p75") else None,
                sweep_span_s=sweep_span_s if sweeping else None,
            )
            return

        if phase in ("await_p2", "await_p5") and now >= beacons_silent_at and not warned_beacons_silent:
            warned_beacons_silent = True
            logger.warning(
                "eject.remote: printer %s — M73 phase beacons not reflected in mc_percent (%s%% at %.0fs after the "
                "start echo) — the bed-drop guard cannot arm without the P5 beacon; the whole-job deadline "
                "governs",
                printer_id,
                percent,
                now - armed_at,
            )

        if phase == "await_p2":
            # The guarded Z re-reference drive. This lane MEASURES and WARNs; it never
            # kills, for two independent reasons.
            #
            # 1. EVIDENCE: no P2 span has ever been observed on hardware (the recipe is
            #    dormant until a model's ladder opens it), so there is no calibrated
            #    figure to kill on, and at the FIRST beacon a stuck percent cannot be
            #    told apart from firmware that does not reflect M73 at all.
            # 2. MECHANISM, which is the decisive one: a stop here would land INSIDE the
            #    prologue's own unguarded window — soft end stops are off (`M211 Z0`) and
            #    the frame has not been declared yet (`G92`) — leaving the machine in a
            #    worse state than letting the drive finish. And the drive moves the bed
            #    AWAY from the nozzle, so there is no plate contact to pre-empt: nothing
            #    is bought by stopping early.
            #
            # The whole-job bound (checked above) remains binding throughout.
            if fresh and percent >= PHASE_BEACON_REFERENCED_PCT:
                logger.info(
                    "eject.remote: printer %s Z re-reference cleared in <=%.1fs (budget %.0fs) — homing X/Y",
                    printer_id,
                    now - armed_at,
                    reference_s,
                )
                phase = "await_p5"
                continue
            if p2_budget is not None and now > p2_budget and not warned_reference_overrun:
                warned_reference_overrun = True
                # THIS line is the distribution a calibrated re-reference guard would be
                # armed from — the ladder's rung 2 and the first flag-on model's
                # production ejects are what fill it in.
                logger.warning(
                    "eject.remote: printer %s Z re-reference running %.0fs against a %.0fs budget — the guarded "
                    "drive may be stalling short of the bottom stop; no phase kill fires here, the whole-job "
                    "deadline governs",
                    printer_id,
                    now - armed_at,
                    reference_s,
                )
            if not fresh and p2_budget is not None and now > p2_budget and not asked_for_evidence:
                # Blind past the budget: ask once for a fresh report rather than judge on
                # silence (the drop lane's rule, same reasoning).
                printer_manager.request_evidence_pushall(printer_id, _EVIDENCE_REASON)
                asked_for_evidence = True
            continue

        if phase == "await_p5":
            if fresh and percent >= PHASE_BEACON_LIFTED_PCT:
                # The span is timed from the FIRST fresh sample at/above the beacon, not
                # from the beacon itself: the real edge fell somewhere in the preceding
                # push+poll window, so every span measured here UNDERSTATES the true one
                # — the bias runs against a false kill, which is the only safe direction.
                t5 = now
                drop_deadline = t5 + drop_span_s + _drop_margin_s(drop_span_s)
                phase = "await_p50"
            continue

        if phase == "await_p50":
            # The pre-sweep guarantee. A percent below the sweep beacon PROVES the
            # printer is still executing drop-phase lines, so the plate is untouched and
            # the stop lands before any lane can scrape it.
            if not fresh:
                if drop_deadline is not None and now > drop_deadline and not asked_for_evidence:
                    # Blind past the deadline: ask once for a fresh report rather than
                    # kill on silence — stale evidence never stops a sweep here; the
                    # whole-job bound remains the backstop.
                    printer_manager.request_evidence_pushall(printer_id, _EVIDENCE_REASON)
                    asked_for_evidence = True
                continue
            if percent >= PHASE_BEACON_SWEEP_PCT:
                if drop_deadline is not None and now > drop_deadline:
                    # The sweep DID start, but only after the drop overran — a drop that
                    # runs long is the lost-steps signature, and the sweep opens with
                    # rear positioning at lift height, so the stop still precedes plate
                    # contact. That is exactly what a post-park kill could NOT claim,
                    # which is why the sweep lane below never mirrors this rule.
                    await _stop_and_page(
                        printer_id,
                        current,
                        armed,
                        stage="drop_late",
                        phase_elapsed_s=now - t5 if t5 is not None else 0.0,
                        progress=percent,
                        line_number=line_number,
                        deadline_s=drop_deadline - armed_at,
                        drop_span_s=drop_span_s,
                        sweep_span_s=None,
                    )
                    return
                logger.info(
                    "eject.remote: printer %s bed-drop phase cleared in <=%.1fs (budget %.0fs, deadline %.0fs) "
                    "— sweeping",
                    printer_id,
                    now - t5 if t5 is not None else 0.0,
                    drop_span_s,
                    drop_span_s + _drop_margin_s(drop_span_s),
                )
                t50 = now
                # With no sweep figure there is no budget line to warn on; the lane still waits for
                # the park beacon, and the whole-job bound still binds until it arrives.
                if sweep_span_s is not None:
                    sweep_budget_deadline = t50 + sweep_span_s + _sweep_margin_s(sweep_span_s)
                phase = "await_p75"
                continue
            if drop_deadline is not None and now > drop_deadline:
                await _stop_and_page(
                    printer_id,
                    current,
                    armed,
                    stage="drop",
                    phase_elapsed_s=now - t5 if t5 is not None else 0.0,
                    progress=percent,
                    line_number=line_number,
                    deadline_s=drop_deadline - armed_at,
                    drop_span_s=drop_span_s,
                    sweep_span_s=None,
                )
                return
            continue

        # await_p75: the sweep — the hazard phase (a part that fails to release presents as a
        # sweep overrun), so it is never given patience: the whole-job bound checked above stays
        # binding here, which is production's stuck-part timing unchanged, and this lane only
        # MEASURES (no calibrated floor exists: the P50→P75 span had never been observed before
        # this lane logged it). That bound also covers the fabricated-late-t50 shape: a link blind
        # from P5 that comes back at P50 anchors t50 late and would otherwise hand a nearly
        # finished sweep a whole fresh budget.
        if not fresh:
            continue
        if percent >= PHASE_BEACON_PARK_PCT:
            overran = sweep_budget_deadline is not None and now > sweep_budget_deadline
            # Timed from t50, so it UNDERSTATES the true span exactly as the drop
            # measurement does. This line IS the P50→P75 distribution a calibrated sweep
            # guard would be armed from. A FRESH park sample is the one thing that ends the
            # whole-job bound: it can be read as THIS job's progress because
            # ``bambu_mqtt._stale_predecessor_reading`` withholds a republished predecessor
            # percent (and its ``progress_wire_at``) until the job reports below its
            # predecessor's final. Past it no motion over the plate remains, so the watch's
            # work is done: the job's terminal answers the rest (the epilogue deadline that
            # once followed stays deleted).
            logger.log(
                logging.WARNING if overran else logging.INFO,
                "eject.remote: printer %s sweep phase cleared in <=%.1fs (budget %s)%s — the job's terminal "
                "ends the sweep",
                printer_id,
                now - t50 if t50 is not None else 0.0,
                f"{sweep_span_s:.0f}s" if sweep_span_s is not None else "none",
                " but OVERRAN it" if overran else "",
            )
            return
        if sweep_budget_deadline is not None and now > sweep_budget_deadline and not warned_sweep_overrun:
            warned_sweep_overrun = True
            logger.warning(
                "eject.remote: printer %s sweep phase running %.0fs against a %.0fs budget — the part may "
                "not have released; no phase kill fires here, the whole-job deadline governs",
                printer_id,
                now - t50 if t50 is not None else 0.0,
                sweep_span_s,
            )
        continue


def matches_pending_eject(
    printer_id: int, completed_subtask_id: str | None, *, subtask_name: str | None = None
) -> bool:
    """True when a :class:`PendingEject` is registered for ``printer_id`` AND the
    terminal/echoed identity does not POSITIVELY mismatch the dispatched eject.

    The single origin of the "is this terminal (or start) our server-dispatched
    eject?" decision, shared by ``farm_policy.on_terminal`` (which still pops the
    registry itself) and the ``main.py`` start/complete callbacks. A positive
    mismatch exists when EITHER:

    * BOTH ``completed_subtask_id`` and the client's ``last_dispatch_subtask_id`` are
      truthy AND unequal (the historical id check — a missing id on either side is a
      lenient match, since a standalone eject file can echo nothing / "0"); OR
    * ``subtask_name`` is truthy AND its stem does not equal ``expected_eject_stem``
      of the pending. This closes the post-restart hole: after a restart the
      client's ``last_dispatch_subtask_id`` is gone, so id-matching turns lenient and
      ANY terminal would otherwise consume a HYDRATED pending and clear our gate — the
      name check re-establishes positive identity from the echoed job name.

    Name evidence alone (no claimed eject) NEVER makes this return True — see
    :func:`is_eject_job_name` for the suppress-only name signal. This function is a
    pure QUERY: retiring the eject is the authority's business, never the matcher's.
    """
    pending = plate_occupancy.eject_identity(printer_id)
    if pending is None:
        return False
    client = printer_manager.get_client(printer_id)
    expected_subtask = getattr(client, "last_dispatch_subtask_id", None) if client else None
    id_mismatch = bool(completed_subtask_id and expected_subtask and completed_subtask_id != expected_subtask)
    name_mismatch = False
    if subtask_name:
        # A manual (foreign-plate) eject carries no queue item, so its stem is keyed
        # by PRINTER id — close the queue_item_id-None leniency for that purpose by
        # name-checking the printer-keyed stem instead.
        expected_stem = expected_eject_stem(pending, printer_id)
        if expected_stem is not None:
            name_mismatch = _eject_name_stem(subtask_name).lower() != expected_stem.lower()
    return not (id_mismatch or name_mismatch)


class EjectDispatchError(RuntimeError):
    """A part-present eject could not be dispatched.

    Carries an HTTP ``status_code`` hint (409 precondition / 502 transport) so the
    FA route can translate it to an ``HTTPException`` without this module importing
    FastAPI. The monitor ignores the hint and treats any raise as a dispatch failure.

    ``code`` is the stable machine-readable reason the UI branches on. It carries the
    authority's own :data:`~backend.app.services.plate_occupancy.TransitionRefusal`
    token verbatim when an occupancy check refused the sweep (``job_active``,
    ``dispatch_in_flight``, ``eject_in_flight``, ``not_occupied``,
    ``z_unreferenced``), so the operator is told which of them held — one refusal
    vocabulary, from the state machine to the dialog. Everything else keeps the
    generic default.

    ``terminal`` says whether RETRYING can ever help. It is set from
    :data:`_TERMINAL_REFUSALS` in :func:`_refusal_error` — a property of the refusal
    VOCABULARY, never a flag a raiser chooses — so an automatic lane (the cooldown
    watch) can tell "come back in 20 s" apart from "a human must act", without
    sniffing tokens at the point of failure.
    """

    def __init__(
        self, message: str, *, status_code: int = 409, code: str = "eject_dispatch_failed", terminal: bool = False
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.terminal = terminal


# The refusal → operator sentence map for the eject lane. The authority speaks only
# in tokens (no English in the core — the 2026-08-20 ``slot_recheck`` precedent), so
# the copy lives here, at the boundary that raises. The route's one verdict→copy map
# is its intended home; until then the message rides the error and the token rides
# ``EjectDispatchError.code``, so the wire contract is already the final one.
_EJECT_REFUSAL_MESSAGES: dict[str, str] = {
    "job_active": "Printer is running a job; wait for it to finish or stop it, then eject",
    "dispatch_in_flight": "A queued unit is being sent to this printer; retry in a few seconds",
    "eject_in_flight": "An eject is already in flight on this printer",
    "not_occupied": "Printer is not awaiting plate clear; nothing to eject",
    "z_unreferenced": (
        "Printer restarted with a part on the plate and its Z reference is lost — remove the part by hand "
        "and Mark plate cleared"
    ),
}

# Refusals no amount of waiting can cure. Terminal-ness is a property of the refusal
# VOCABULARY, declared here beside the copy, rather than a flag each raiser sets: the
# automatic lanes (the cooldown watch above all) must be able to tell a transient
# conflict — a job finishing, an upload landing, a sweep completing — from a state that
# needs a HUMAN, and deriving that at each raise is how the two drift apart.
#
# A terminal refusal is therefore not a retry strike: the cooldown watch pages and
# converts the plate to an escalation hold instead of burning its three dispatch
# attempts against a wall.
_TERMINAL_REFUSALS: frozenset[TransitionRefusal] = frozenset({"z_unreferenced"})

# The ``notify_plate_not_empty`` source_detail a terminal refusal escalates under —
# beside the vocabulary it belongs to, so a lane that must page for one of these never
# has to know which one it got. Same token style as the event's existing
# ``printer_vision`` / ``camera_cv`` / ``cooldown_timeout`` details.
_TERMINAL_REFUSAL_DETAILS: dict[TransitionRefusal, str] = {
    "z_unreferenced": "z_reference_lost_after_reboot",
}


def terminal_refusal_detail(code: str) -> str:
    """The escalation ``source_detail`` for a terminal refusal ``code``.

    Falls back to the token itself, so a refusal added to :data:`_TERMINAL_REFUSALS`
    without its copy pages under a name a reader can still grep — never silently under
    another refusal's wording."""
    return _TERMINAL_REFUSAL_DETAILS.get(code, code)  # type: ignore[arg-type]


def _refusal_error(refusal: TransitionRefusal) -> EjectDispatchError:
    """The 409 for an occupancy refusal, carrying the refusal token as its ``code``."""
    return EjectDispatchError(
        _EJECT_REFUSAL_MESSAGES.get(refusal, f"Eject refused ({refusal})"),
        status_code=409,
        code=refusal,
        terminal=refusal in _TERMINAL_REFUSALS,
    )


def z_reference_evidence(printer_id: int) -> bool | None:
    """Is this printer's Z frame still trustworthy? ``False`` = it is fiction.

    THE one origin of :attr:`~backend.app.services.plate_occupancy.Evidence.z_reference`,
    consumed by BOTH eject Evidence builders (this module's :func:`_live_evidence` and
    the manual lane's), so the manual "Eject plate" dialog refuses with the same
    sentence the automatic lane pages with.

    Sync and DB-free by construction — it reads ``printer_incidents.snapshot``, the
    same process-cache projection the printer card's chip renders — so it can be called
    from the pre-flight of a dispatcher without an await.

    Returns ``False`` ONLY on an open ``z_reference_lost`` incident (a printer that
    rebooted with a part on its plate: the firmware's Z datum after boot is a
    fabrication and every absolute Z move in the eject block would execute against it —
    002-H2S drove the bed past the Z floor that way on 2026-09-04). Otherwise ``None``:
    the farm has no positive evidence that Z IS referenced, and claiming True would be
    inventing one. ``None`` passes the gate, so absence of the hold is absence of the
    refusal — exactly today's behaviour."""
    from backend.app.models.printer_incident import KIND_Z_REFERENCE_LOST
    from backend.app.services import printer_incidents

    # Asked BY KIND (2026-09-11): a printer can now hold a lost-Z frame BESIDE an
    # AMS fault, and the single-slot reading used to hand back whichever row ranked
    # higher — which is how the hold could be open and this still answer None.
    if printer_incidents.snapshot(printer_id, kind=KIND_Z_REFERENCE_LOST) is not None:
        return False
    return None


def _live_evidence(printer_id: int) -> Evidence:
    """The wire snapshot the eject lane checks against.

    ``db_claim`` is deliberately absent: a ``printing`` queue row on an IDLE printer
    can be a start nobody answered (the 2026-08-29 class, item 1010), and refusing an
    eject on it would lock the operator out of a provably idle plate — strictly worse
    than the behaviour this replaces, where the eject lane never read the queue row at
    all. The dispatch LEASE — held until the start is answered — is what an eject can
    physically collide with, and the authority derives that from its own record.

    The eject lane is also ungated by live HMS, deliberately (2026-08-29): an
    eject is filament-less, and holding the plate behind an AMS fault would deadlock
    the very plate that holds the printer. ``z_reference`` is the deliberate exception
    and is not an HMS gate at all: it is a durable HOLD saying the machine's own
    coordinate frame is wrong, which is the one condition under which the sweep's
    motion is the hazard.
    """
    state = printer_manager.get_status(printer_id)
    return Evidence(
        live_state=getattr(state, "state", None) if state is not None else None,
        z_reference=z_reference_evidence(printer_id),
    )


async def _resolve_source_donor(db: AsyncSession, item: PrintQueueItem) -> DispatchDonor:
    """The donor ``item`` printed from — file AND validated plate — or a 409.

    Thin adapter over ``farm_correlation.resolve_item_donor``, which is THE
    "which file did this unit print, and which plate of it" resolver — the same
    question the print-start archive capture and the cooldown hold ask, answered once.
    This lane's only difference is that a missing donor is a dispatch precondition
    failure rather than a fall-back-to-guessing, so it raises instead of returning
    None.

    The whole donor is returned rather than just its path because the PLATE is the half
    that was getting lost: this lane used to take the file from here and then re-read
    ``item.plate_id or 1`` for the firmware, so a donor that did not carry the unit's
    plate still produced a dispatch (2026-09-17, 005-H2S). The resolver now validates
    the plate against the container, so "there is a donor" and "it carries this unit's
    plate" are one answer.
    """
    from backend.app.services.farm_correlation import resolve_item_donor

    donor = await resolve_item_donor(db, item)
    if donor is None:
        raise EjectDispatchError(
            "Eject source file not found on disk for the finished unit, or it does not carry that unit's plate",
            status_code=409,
        )
    return donor


async def dispatch_part_present_eject(
    db: AsyncSession,
    *,
    printer_id: int,
    queue_item_id: int,
    purpose: EjectPurpose,
    run_id: int | None,
    plate_z: float | None = None,
) -> None:
    """Build + FTPS-upload + dispatch a part-present motion-only eject for one unit.

    Resolves the profile / geometry / source file from ``queue_item_id`` and the
    target printer, builds the standalone eject-only file, uploads it (honouring the
    FTP retry settings) and starts it with EVERY pre-print calibration OFF, then
    CLAIMS the printer for the sweep. Does NOT touch the plate-clear gate — that
    clears only when the eject job's terminal arrives.

    The occupancy check runs BEFORE the build, because building and uploading an
    eject costs seconds of FTPS work that a refused sweep must not spend; the claim
    afterwards re-runs the identical gate, so a race that opened during the upload is
    still caught.

    ``plate_z`` is where the bed IS when the sweep starts, when the caller knows (the
    cooldown hold parked the plate there). It changes no G-code — only whether the
    watchdog's deadlines are computed from a MEASUREMENT of the block's first Z move or
    from a BOUND on it. Callers that cannot answer pass nothing.

    Raises :class:`EjectDispatchError` on any precondition (409) or transport (502)
    failure, leaving no half state (nothing is claimed unless ``start_print``
    was accepted).
    """
    item = await db.get(PrintQueueItem, queue_item_id)
    if item is None:
        raise EjectDispatchError(f"Queue item {queue_item_id} not found; cannot eject", status_code=409)
    if item.eject_profile_id is None:
        raise EjectDispatchError("Unit has no eject profile; cannot eject remotely", status_code=409)

    printer = await db.get(Printer, printer_id)
    if printer is None:
        raise EjectDispatchError("Eject printer not found", status_code=409)
    if not printer_manager.is_connected(printer.id):
        raise EjectDispatchError("Printer is not connected; cannot eject remotely", status_code=409)

    ev = _live_evidence(printer_id)
    refusal = plate_occupancy.ejectable(printer_id, ev)
    if refusal is not None:
        raise _refusal_error(refusal)

    # Fail-closed on a model with no geometry row or a row not hardware-validated —
    # a production eject must never drive an unvalidated envelope.
    try:
        geometry = await get_geometry_required(db, printer.model, require_validated=True)
    except GeometryUnavailable as exc:
        raise EjectDispatchError(exc.reason, status_code=409) from exc

    profile = await db.get(EjectProfile, item.eject_profile_id)
    if profile is None:
        raise EjectDispatchError("Eject profile not found", status_code=409)

    donor = await _resolve_source_donor(db, item)
    eject_progress.emit_eject_progress(printer_id=printer.id, queue_item_id=queue_item_id, phase="building")
    try:
        built = await build_part_present_eject_file(
            donor.local_path, donor.plate_id, profile, geometry, plate_z=plate_z
        )
    except Exception as exc:  # noqa: BLE001 — generation/validation/repack → actionable 409
        eject_progress.emit_eject_progress(printer_id=printer.id, queue_item_id=queue_item_id, phase="failed")
        raise EjectDispatchError(f"Failed to build part-present eject file: {exc}", status_code=409) from exc

    # Carry the build's runtime figures into the pending: the watchdog is the only
    # place they can be used, and by then the built file is long deleted.
    pending = PendingEject(
        purpose=purpose,
        run_id=run_id,
        queue_item_id=queue_item_id,
        expected_runtime_s=built.expected_runtime_s,
        reference_s=built.reference_s,
        drop_span_s=built.drop_span_s,
        sweep_span_s=built.sweep_span_s,
        tail_s=built.tail_s,
        start_z=built.start_z,
    )
    # The eject file's FTPS upload transiently drops the H2S sdcard flag; mark the
    # printer upload-in-flight so the USB-drop verifier ignores that dispatch blip.
    async with upload_in_flight(printer.id):
        await _upload_start_claim_eject(
            printer=printer,
            eject_path=built.path,
            job_stem=_dispatch_stem(pending, printer.id),
            plate_id=built.plate_id,
            pending=pending,
            ev=ev,
        )
    logger.info(
        "eject.remote: dispatched %s eject for item %s (run %s) on printer %s",
        purpose,
        queue_item_id,
        run_id,
        printer.id,
    )


async def dispatch_foreign_eject(
    db: AsyncSession,
    *,
    printer_id: int,
    profile_id: int,
    source_path: Path,
    plate_id: int,
    max_z_override: float | None = None,
) -> None:
    """Build + FTPS-upload + dispatch a part-present eject for a FOREIGN plate.

    The two-step "Eject now" confirm for a plate the farm did not dispatch (started
    from Bambu Studio): the manual-eject service resolved the donor ``source_path`` +
    ``plate_id`` from the foreign print's archive and picked an ``eject_profile_id``,
    and this shares ``dispatch_part_present_eject``'s upload→start→claim tail. It is
    NOT queue-item-bound — it claims a ``purpose="manual"`` :class:`PendingEject`
    with ``queue_item_id=None``. Having no durable mirror is DELIBERATE: a manual eject
    is not restart-durable, so a mid-eject restart leaves the plate gate raised
    (fail-closed).

    Geometry is fail-closed (``require_validated=True``); the caller owns cleanup of
    ``source_path`` (it may be a temp FTPS re-fetch). ``max_z_override`` is the
    operator's confirmed part height, superseding the donor header in the build — the
    donor may be an assumed fallback rather than the print on the plate. Raises
    :class:`EjectDispatchError` on any precondition (409) or transport (502) failure,
    leaving nothing claimed unless ``start_print`` was accepted.
    """
    printer = await db.get(Printer, printer_id)
    if printer is None:
        raise EjectDispatchError("Eject printer not found", status_code=409)
    if not printer_manager.is_connected(printer.id):
        raise EjectDispatchError("Printer is not connected; cannot eject remotely", status_code=409)

    ev = _live_evidence(printer_id)
    refusal = plate_occupancy.ejectable(printer_id, ev)
    if refusal is not None:
        raise _refusal_error(refusal)

    try:
        geometry = await get_geometry_required(db, printer.model, require_validated=True)
    except GeometryUnavailable as exc:
        raise EjectDispatchError(exc.reason, status_code=409) from exc

    profile = await db.get(EjectProfile, profile_id)
    if profile is None:
        raise EjectDispatchError("Eject profile not found", status_code=409)

    eject_progress.emit_eject_progress(printer_id=printer.id, queue_item_id=None, phase="building")
    try:
        built = await build_part_present_eject_file(
            Path(source_path), plate_id, profile, geometry, max_z_override=max_z_override
        )
    except Exception as exc:  # noqa: BLE001 — generation/validation/repack → actionable 409
        eject_progress.emit_eject_progress(printer_id=printer.id, queue_item_id=None, phase="failed")
        raise EjectDispatchError(f"Failed to build part-present eject file: {exc}", status_code=409) from exc

    # A manual/foreign sweep carries the estimate too — it never gates the plate
    # (an operator is present, and this path owns no run), but the terminal handler
    # logs the comparison so a foreign donor's ejects join the same runtime series.
    pending = PendingEject(
        purpose="manual",
        run_id=None,
        queue_item_id=None,
        expected_runtime_s=built.expected_runtime_s,
        reference_s=built.reference_s,
        drop_span_s=built.drop_span_s,
        sweep_span_s=built.sweep_span_s,
        tail_s=built.tail_s,
        start_z=built.start_z,
    )
    # Same as the production path: the FTPS upload transiently drops the H2S sdcard
    # flag; mark the printer upload-in-flight so the USB-drop verifier ignores the blip.
    async with upload_in_flight(printer.id):
        await _upload_start_claim_eject(
            printer=printer,
            eject_path=built.path,
            job_stem=_dispatch_stem(pending, printer_id),
            plate_id=built.plate_id,
            pending=pending,
            ev=ev,
        )
    logger.info(
        "eject.remote: dispatched manual (foreign-plate) eject on printer %s (plate %s, profile %s)",
        printer_id,
        plate_id,
        profile_id,
    )


async def _upload_start_claim_eject(
    *,
    printer: Printer,
    eject_path: Path,
    job_stem: str,
    plate_id: int,
    pending: PendingEject,
    ev: Evidence,
) -> None:
    """Shared eject tail: FTPS-upload the built eject file (honouring the FTP retry
    settings), start it with EVERY pre-print calibration OFF, then CLAIM the printer
    for the sweep, linked to its start's record id. The built ``eject_path`` is always
    cleaned up; nothing is claimed unless ``start_print`` was accepted. Raises
    :class:`EjectDispatchError` (502) on upload / start failure.

    The ``start_print`` file, the MQTT ``project_file`` param (plate path, keyed by
    ``plate_id``) and the eventual SD cleanup all key off the SAME ``remote_filename``
    (the bare ``job_stem``).

    ``plate_id`` is ALWAYS the caller's ``BuiltEject.plate_id`` — the member the build
    packed the sweep into. "Which plate was packed" and "which plate is commanded" are
    one value by construction; a second read of the queue row is what let them diverge
    into an unparseable ``project_file`` on 2026-09-17 (005-H2S).

    ``ev`` is the PRE-DISPATCH wire snapshot, carried down from the dispatcher rather
    than re-read here on purpose: by the time this claims, the printer may already
    have accepted the sweep, and a freshly-read ACTIVE state would make our own eject
    refuse its own claim with ``job_active``. Every refusal that matters — a dispatch
    lease minted during the upload, another eject — is derived from the authority's
    record, not from ``ev``, so the race this exists to catch is still caught.

    The durable mirror is no longer written here: the authority's persist callable
    writes ``print_queue.eject_dispatched_at`` off the claim transition, which is what
    makes the in-memory record and its durable half impossible to disagree.
    """
    from backend.app.services.bambu_ftp import (
        cleanup_downloaded_3mf,
        get_ftp_retry_settings,
        upload_file_async,
        with_ftp_retry,
    )

    remote_path = _eject_remote_path(job_stem)
    remote_filename = remote_path.removeprefix("/")
    ftp_retry_enabled, ftp_retry_count, ftp_retry_delay, ftp_timeout = await get_ftp_retry_settings()

    # Upload progress rides the FTP callback, which fires on the executor thread —
    # marshal each tick back onto the loop before emitting (never touch the socket
    # from another thread). The callback must never raise (it would abort the upload).
    loop = asyncio.get_running_loop()

    def _on_upload_progress(uploaded_bytes: int, total_bytes: int) -> None:
        pct = round(uploaded_bytes / total_bytes * 100.0, 1) if total_bytes else None
        loop.call_soon_threadsafe(
            lambda: eject_progress.emit_eject_progress(
                printer_id=printer.id, queue_item_id=pending.queue_item_id, phase="uploading", progress_pct=pct
            )
        )

    try:
        if ftp_retry_enabled:
            uploaded = await with_ftp_retry(
                upload_file_async,
                printer.ip_address,
                printer.access_code,
                eject_path,
                remote_path,
                socket_timeout=ftp_timeout,
                printer_model=printer.model,
                progress_callback=_on_upload_progress,
                max_retries=ftp_retry_count,
                retry_delay=ftp_retry_delay,
                operation_name=f"Upload {pending.purpose} eject to {printer.name}",
            )
        else:
            uploaded = await upload_file_async(
                printer.ip_address,
                printer.access_code,
                eject_path,
                remote_path,
                socket_timeout=ftp_timeout,
                printer_model=printer.model,
                progress_callback=_on_upload_progress,
            )
    except Exception as exc:  # noqa: BLE001
        uploaded = False
        logger.error("eject.remote: %s eject upload error: %s", pending.purpose, exc)
    finally:
        cleanup_downloaded_3mf(eject_path)

    # Every abandonment exit below removes this lane's own upload: a sweep file left on the USB
    # is one screen-tap away from a foreign start, and it fills the drive.
    from backend.app.services.bambu_ftp import remove_abandoned_upload

    if not uploaded:
        # A failed or partial upload may still have left bytes on the drive.
        await remove_abandoned_upload(printer, remote_path, lane="eject", why="the upload failed")
        eject_progress.emit_eject_progress(printer_id=printer.id, queue_item_id=pending.queue_item_id, phase="failed")
        raise EjectDispatchError("Failed to upload the eject file to the printer", status_code=502)

    # EVERY pre-print calibration OFF — never bed-probe / shake / re-level with a
    # part on the plate (the old FA call omitted bed_levelling/vibration_cali and
    # defaulted them True — a hazard this closes). The START bound rides the action from its
    # send (:data:`_EJECT_START_SELF_HEAL`): the record counts its online seconds and, if no
    # event answers the start first, hands the fallback to :func:`on_self_heal_due`.
    sent = await printer_action.send(
        printer.id,
        printer_action.eject_start(
            remote_filename,
            plate_id=plate_id,
            bed_levelling=False,
            flow_cali=False,
            vibration_cali=False,
            layer_inspect=False,
            timelapse=False,
            use_ams=False,
        ),
        actor=printer_action.ACTOR_FARM,
        queue_item_id=pending.queue_item_id,
        bound=_EJECT_START_SELF_HEAL,
    )
    if not sent.published:
        await remove_abandoned_upload(printer, remote_path, lane="eject", why="the start command was refused")
        eject_progress.emit_eject_progress(printer_id=printer.id, queue_item_id=pending.queue_item_id, phase="failed")
        raise EjectDispatchError("Failed to send the eject command to the printer", status_code=502)

    eject_progress.emit_eject_progress(printer_id=printer.id, queue_item_id=pending.queue_item_id, phase="sent")

    # Claim the printer. The gate this passed before the build is re-run here, so a
    # dispatch lease or another eject that appeared during the upload still refuses —
    # and then the sweep we just started must be stopped, because the file IS on the
    # printer and the firmware may act on it.
    # The claim carries its start's record id, so the record's answer to THIS start is what retires
    # it (:func:`on_action_answered`) — never another eject's.
    refusal = plate_occupancy.claim_for_eject(printer.id, replace(pending, start_action_id=sent.action_id), ev)
    if refusal is not None:
        logger.warning(
            "eject.remote: printer %s claim refused (%s) AFTER the eject start was accepted — "
            "stopping the sweep; the printer was taken by something else during the upload",
            printer.id,
            refusal,
        )
        await printer_action.send(printer.id, printer_action.stop(), actor=printer_action.ACTOR_FARM)
        await remove_abandoned_upload(printer, remote_path, lane="eject", why=f"the claim was refused ({refusal})")
        eject_progress.emit_eject_progress(printer_id=printer.id, queue_item_id=pending.queue_item_id, phase="failed")
        raise _refusal_error(refusal)

    # Whether the printer begins the sweep is the RECORD's answer to its start, delivered on the hub
    # (:func:`on_action_answered`), with the START bound as the fallback (:func:`on_self_heal_due`).
    # An answer can land before the claim above (a fail ACK inside the publish's own round trip),
    # when no claim existed for the subscriber to retire, so the record is asked once more now that
    # one does.
    answer = await printer_action.settled(sent.action_id)
    if answer is not None and answer.value in _NEVER_STARTED:
        await _retire_answered_start(printer.id, answer, await printer_action.reason_of(sent.action_id))


async def dispatch_identified_foreign_eject(*, printer_id: int, profile_id: int) -> None:
    """``on_release`` for the AUTO foreign-eject watch: resolve the foreign donor FRESH
    and dispatch the sweep exactly as the manual foreign confirm does (minus the thermal
    gate — the cooldown watch already waited for the bed to reach the threshold).

    Opens its own session (module convention); RAISES on any failure so
    ``watch_bed_and_clear`` counts a dispatch failure (retry, then stall after three)
    rather than silently dropping the sweep.

    It lives HERE, beside the dispatcher it calls, rather than in ``eject.manual``:
    with it there, the cooldown monitor had to reach into the manual-eject service at
    call time while that service imports the monitor at module load — a cycle held
    open by a lazy import. Since the donor extraction the resolution it needs is
    not the manual lane's at all: it walks :data:`AUTO_DONOR_CHAIN` — the gate-archive
    tier ALONE. The operator lane's assumed (last-farm-item) and anonymous (container)
    tiers must never reach an unattended sweep, and declaring that as a composition
    rather than a call-site condition is what makes it unforgettable.
    """
    from backend.app.core.database import async_session
    from backend.app.services.eject.donor import AUTO_DONOR_CHAIN, DonorContext, release_donor, resolve_donor

    async with async_session() as db:
        printer = await db.get(Printer, printer_id)
        if printer is None:
            raise EjectDispatchError(f"Printer {printer_id} not found; cannot eject", status_code=404, code="not_found")
        source = await resolve_donor(
            AUTO_DONOR_CHAIN,
            DonorContext(db=db, printer=printer, plate_source=plate_occupancy.plate_source(printer_id), item=None),
        )
        if source is None:
            # RAISES rather than returning: ``watch_bed_and_clear`` must count this as a
            # dispatch failure (retry, then stall after three), never silently drop the
            # sweep and leave the plate gated with nothing watching it.
            raise EjectDispatchError(
                "Could not resolve the donor file for the plate on this printer", status_code=409, code="no_donor"
            )
        try:
            await dispatch_foreign_eject(
                db,
                printer_id=printer_id,
                profile_id=profile_id,
                source_path=source.path,
                plate_id=source.plate_id,
            )
            logger.info(
                "eject.remote: printer %s auto foreign-plate eject dispatched (plate %s, profile %s)",
                printer_id,
                source.plate_id,
                profile_id,
            )
        finally:
            release_donor(source)
