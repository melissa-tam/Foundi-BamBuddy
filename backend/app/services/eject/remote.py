"""Shared part-present eject dispatcher + the pending-eject registry.

The eject sweep is a SEPARATE, server-dispatched, motion-only job — used by two
callers that share this ONE path:

- **Production loop**: the eject monitor, once the live bed reaches the release
  threshold, dispatches the eject for the finished unit (``purpose="production"``).
- **First article**: an operator approval with ``eject_remotely`` dispatches the
  eject for the FA plate (``purpose="fa"``).

Both build a standalone motion-only ``.gcode.3mf`` (``build_part_present_eject_file``),
FTPS-upload it and ``project_file``-dispatch it via ``printer_manager.start_print``
with EVERY pre-print calibration OFF (never bed-probe / shake with a part on the
plate), then register a :class:`PendingEject` so the terminal handler can match the
job's echoed ``subtask_id`` and act on completion. The plate-clear gate is NOT
cleared here — it drops only when the eject job's terminal arrives.

Failures raise :class:`EjectDispatchError` (a plain domain error carrying an HTTP
status hint); the FA route wraps it in an ``HTTPException`` while the monitor lets
it propagate as a dispatch failure it retries.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from sqlalchemy import select

from backend.app.core.tasks import spawn_background_task
from backend.app.models.eject_profile import EjectProfile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services.eject import progress as eject_progress
from backend.app.services.eject.dispatch import build_part_present_eject_file
from backend.app.services.eject.generator import (
    EJECT_RUNTIME_OVERHEAD_S,
    PHASE_BEACON_LIFTED_PCT,
    PHASE_BEACON_SWEEP_PCT,
)
from backend.app.services.eject.geometry import GeometryUnavailable, get_geometry_required
from backend.app.services.printer_manager import printer_manager
from backend.app.services.usb_storage import upload_in_flight

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

EjectPurpose = Literal["fa", "production", "manual"]


@dataclass(frozen=True)
class PendingEject:
    """An in-flight server-dispatched eject awaiting its terminal status.

    Held in-memory (``_pending_eject``) for the live-callback fast path AND mirrored
    durably onto the owning queue unit's ``eject_dispatched_at`` stamp so a restart
    between dispatch and terminal can rehydrate the entry (W1). The plate gate is
    persisted independently and only auto-clears once the eject's terminal (live or
    reconciled) is positively matched.

    ``expected_runtime_s`` (from the build) and ``started_at`` (stamped when the
    printer echoes the sweep's START) are what the in-flight runtime watchdog arms
    on — see :func:`_runtime_watchdog`. Both default to None and BOTH are None on a
    rehydrated entry: the durable mirror is a single timestamp column on the queue
    unit, not the built artifact, so a restart between dispatch and terminal cannot
    reconstruct either. The watchdog then never arms, which is the intended degrade —
    a post-restart eject already falls into the unverifiable-path handling
    (``rearm_on_startup`` degrades those gates to escalation-only holds).

    ``drop_span_s`` (also from the build) is the bed-drop phase's own budget and arms
    the watchdog's EDGE lane, which bounds that phase alone instead of the whole job.
    None — a drop-less profile, or a rehydrated entry — leaves the watchdog on the
    whole-job deadline only.

    ``runtime_exceeded_at`` is that watchdog's verdict, and the watchdog is the ONE
    authority on eject runtime: it stamps this mark the moment a deadline passes
    (before it even sends the stop, so a terminal racing in must already see it). The
    terminal handler only HONORS the mark — it never re-computes a runtime judgement of
    its own, so the machine cannot be stopped on one criterion and then judged on
    another.
    """

    purpose: EjectPurpose
    run_id: int | None
    queue_item_id: int | None
    expected_runtime_s: float | None = None
    started_at: datetime | None = None
    runtime_exceeded_at: datetime | None = None
    drop_span_s: float | None = None


# printer_id -> the one in-flight eject on that printer.
_pending_eject: dict[int, PendingEject] = {}

# printer_id -> the armed runtime watchdog for that printer's in-flight eject.
# One entry at most: an eject is one-per-printer by construction, and every path
# that ends an eject (resolution, or a new dispatch) drops the entry.
_runtime_watchdogs: dict[int, asyncio.Task] = {}

# Rows whose durable eject stamp is older than this at startup are treated as a
# crash that never cleared and are dropped (NULLed) with a WARNING rather than
# rehydrated — no eject stays "in flight" across a day-long outage.
_PENDING_EJECT_STALE_TTL_H = 24

# How far past its estimate an eject may run before the watchdog STOPS the job.
#
# Calibrated on the 2026-07-31 gouged-plate incident (H2S): an ejected part lodged
# under the heatbed, the next eject's bed-drop — an OPEN-LOOP absolute move, since
# the validator forbids re-homing Z with a part on the plate — stalled against it,
# lost steps, and returned the bed too high, so the sweep gouged the build plate.
# The job still reported ``completed``: there is no Z telemetry in MQTT and no HMS
# fires for a silent stall, leaving RUNTIME as the only observable signature.
#
# 11 measured production ejects all ran 80-83 s against an 83 s estimate (the
# estimator lands within ~3 s of the machine), so that profile's deadline is ~104 s.
# The stall accrues in the drop/return phase, which precedes the sweep at ~45 s in,
# so firing at +21 s pre-empts any pre-sweep stall longer than 59 s — the incident's
# was ~97 s. Nominal ejects finish with ~20 s of margin to spare.
#
# The 20 s FLOOR protects against TELEMETRY, not motion: it is the printer's FINISH
# echo over MQTT that cancels the watchdog, so on a short profile the floor is what
# keeps an ordinary connection hiccup from aborting a perfectly healthy sweep. The
# 60 s CAP stops a long multi-pass eject (~10 min) from inheriting minutes of
# scraping margin, which a pure ×1.5 factor would hand it.
EJECT_ABORT_MARGIN_FRAC = 0.25
EJECT_ABORT_MARGIN_MIN_S = 20.0
EJECT_ABORT_MARGIN_MAX_S = 60.0

# How far past its own budget the BED-DROP PHASE may run before the watchdog stops the
# job — the pre-sweep guarantee the whole-job deadline above cannot give.
#
# Tighter than the total margin on both ends because it is measured from an OBSERVED
# edge (the M73 P5 beacon reflected in `mc_percent`), not from dispatch: upload, job
# spin-up and start-echo latency are all excluded, so none of that variance has to be
# paid for here. The 8 s FLOOR is the sum of what can still be off — the estimator's
# ~±3 s, the printer's ~1 Hz push cadence and this watchdog's own poll interval — and
# the 20 s CAP keeps a long multi-pass drop from inheriting minutes of stall budget.
EJECT_DROP_MARGIN_FRAC = 0.25
EJECT_DROP_MARGIN_MIN_S = 8.0
EJECT_DROP_MARGIN_MAX_S = 20.0

# Poll cadence of the phase-edge lane. Reads an in-memory field the MQTT session
# already maintains — no wire traffic, no DB — so the cost is a wake-up per printer.
_PROGRESS_POLL_S = 2.0

# Maximum age of a `mc_percent` sample that may be treated as evidence. Pushes arrive at
# ~1 Hz while a job runs, so anything older than this is a silent link, not a phase
# report: it can never advance a state or justify a stop.
_PROGRESS_FRESH_S = 30.0

# Minimum lead the drop deadline must have over the total deadline for the edge lane to
# be worth arming. Inside this, the phase rule would fire within seconds of the whole-job
# rule and only add ways to be wrong, so such a profile runs the deadline-only form.
_EDGE_LANE_MIN_LEAD_S = 5.0

# Delay before the single stop-command retry when the first send was not delivered.
_STOP_RETRY_DELAY_S = 5.0

# Why a mid-flight stop fired. "total" = the whole-job deadline (the original rule);
# "drop" = the bed-drop phase overran with the sweep still unreached; "drop_late" = the
# sweep beacon did arrive, but only after the drop phase had already overrun.
EjectStopStage = Literal["total", "drop", "drop_late"]

# Reason string for the evidence pushall requested when the phase lane goes blind.
_EVIDENCE_REASON = "eject_phase_beacon_stale"


def _clamped_margin_s(base_s: float, frac: float, min_s: float, max_s: float) -> float:
    """``base_s * frac`` clamped to [``min_s``, ``max_s``] — the ONE clamp both margins use."""
    return min(max(base_s * frac, min_s), max_s)


def _abort_margin_s(expected_total_s: float) -> float:
    """Grace added to the whole-job estimate before the eject is aborted (25%, [20 s, 60 s])."""
    return _clamped_margin_s(
        expected_total_s, EJECT_ABORT_MARGIN_FRAC, EJECT_ABORT_MARGIN_MIN_S, EJECT_ABORT_MARGIN_MAX_S
    )


def _drop_margin_s(drop_span_s: float) -> float:
    """Grace added to the bed-drop phase's budget before the eject is aborted (25%, [8 s, 20 s])."""
    return _clamped_margin_s(drop_span_s, EJECT_DROP_MARGIN_FRAC, EJECT_DROP_MARGIN_MIN_S, EJECT_DROP_MARGIN_MAX_S)


def eject_abort_deadline_s(expected_runtime_s: float) -> float:
    """Seconds of execution after which an eject is aborted mid-flight.

    ``expected`` plus a margin of 25% of the estimate, clamped to [20 s, 60 s]."""
    return expected_runtime_s + _abort_margin_s(expected_runtime_s)


# The canonical eject-job-name convention, minted at dispatch. Two shapes:
#   * queue-item-bound (production / first-article): ``eject_{purpose}_item{queue_item_id}``
#   * foreign-plate manual eject (no queue item): ``eject_manual_p{printer_id}``
# Case-insensitive; the printer echoes the stem verbatim as ``subtask_name`` (it is
# derived from the dispatched filename in ``bambu_mqtt.start_print``). For the manual
# form the trailing integer is the PRINTER id (there is no queue item).
_EJECT_NAME_RE = re.compile(r"^eject_(?:(fa|production)_item|manual_p)(\d+)$", re.IGNORECASE)


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


def expected_eject_stem(pending: PendingEject) -> str:
    """The eject job stem THIS pending eject was dispatched under."""
    return f"eject_{pending.purpose}_item{pending.queue_item_id}"


def register_pending_eject(printer_id: int, pending: PendingEject) -> None:
    # A watchdog left over from a previous eject on this printer must never survive
    # into the new one — it would judge THIS sweep against the PREVIOUS deadline and
    # stop a healthy job. Cancelling here is the synchronous half (this function is
    # called off the dispatch path, which cannot await); the coroutine's own
    # ``finally`` deregisters, and its identity re-check is the belt for a cancel
    # that lands after the deadline already elapsed.
    stale = _runtime_watchdogs.pop(printer_id, None)
    if stale is not None:
        stale.cancel()
    _pending_eject[printer_id] = pending


def pop_pending_eject(printer_id: int) -> PendingEject | None:
    return _pending_eject.pop(printer_id, None)


def peek_pending_eject(printer_id: int) -> PendingEject | None:
    return _pending_eject.get(printer_id)


def pending_eject_printer_ids() -> list[int]:
    """Printer ids that currently have a pending eject (live or hydrated)."""
    return list(_pending_eject.keys())


def mark_pending_eject_started(printer_id: int) -> None:
    """Stamp the pending eject on ``printer_id`` with the moment the sweep STARTED.

    Called from the print-START callback, i.e. when the printer has echoed that it
    began executing the eject file — not when we uploaded or commanded it. Only that
    edge measures machine time: upload + job spin-up vary with file size and FTPS
    conditions and would otherwise be charged to the sweep.

    IDEMPOTENT by first-write-wins: a duplicate/replayed start echo keeps the
    original stamp, so a chatty printer can never shorten a measured runtime into
    looking nominal. A no-op when nothing is registered (a non-eject start, or a
    hydrated entry whose printer re-echoes).

    This edge also ARMS the in-flight runtime watchdog, because it is the first
    moment a deadline can be measured from. Two cases never arm: a pending with no
    ``expected_runtime_s`` (a rehydrated post-restart entry — there is no estimate to
    judge against, and the startup reconciler already owns those gates fail-closed),
    and a printer that already has a watchdog registered."""
    pending = _pending_eject.get(printer_id)
    if pending is None or pending.started_at is not None:
        return
    started = dataclasses.replace(pending, started_at=datetime.now(timezone.utc))
    _pending_eject[printer_id] = started
    if started.expected_runtime_s is None or printer_id in _runtime_watchdogs:
        return
    _runtime_watchdogs[printer_id] = spawn_background_task(
        _runtime_watchdog(printer_id, started), name=f"eject-runtime-watchdog-{printer_id}"
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


def _elapsed_since_start_s(pending: PendingEject) -> float:
    """Seconds of machine time since the printer echoed this eject's START (0.0 if unstamped)."""
    if pending.started_at is None:
        return 0.0
    return (datetime.now(timezone.utc) - pending.started_at).total_seconds()


def _watchdog_still_owns(printer_id: int, armed: PendingEject) -> PendingEject | None:
    """The registered pending IF it is still the eject this watchdog was armed for.

    None means resolved or superseded — a terminal was handled while we slept, or a new
    eject was dispatched onto the printer. Stopping on either would abort someone else's
    job, so every rule re-checks this before it can act."""
    current = peek_pending_eject(printer_id)
    if current is None or (current.queue_item_id, current.purpose, current.started_at) != (
        armed.queue_item_id,
        armed.purpose,
        armed.started_at,
    ):
        return None
    return current


def _stop_source_detail(stage: EjectStopStage, elapsed_s: float, expected_s: float, drop_span_s: float | None) -> str:
    """The operator-facing 'why the plate is not empty' sentence for a mid-flight stop."""
    if stage == "total":
        return (
            f"the eject sweep was STOPPED mid-job after {elapsed_s:.0f}s against an expected "
            f"{expected_s:.0f}s — it may have stalled against an obstruction. "
            "Check under the heatbed and inspect the build plate before clearing it."
        )
    budget = f"{drop_span_s:.0f}s" if drop_span_s is not None else "its"
    if stage == "drop":
        return (
            f"the eject was STOPPED DURING the bed-drop phase after {elapsed_s:.0f}s — the bed drop ran past its "
            f"{budget} budget and the sweep had not started, so the bed may be stalled against an obstruction. "
            "Check under the heatbed and inspect the build plate before clearing it."
        )
    return (
        f"the eject was STOPPED AFTER a bed-drop phase that overran ({elapsed_s:.0f}s in, against a {budget} "
        "bed-drop budget) — a drop that runs long can lose Z steps and return the bed too high for the sweep. "
        "Check under the heatbed and inspect the build plate before clearing it."
    )


async def _stop_and_page(
    printer_id: int,
    current: PendingEject,
    armed: PendingEject,
    *,
    sleep: Callable[[float], Awaitable[None]],
    stage: EjectStopStage,
    elapsed_s: float,
    progress: float | None,
) -> None:
    """Stamp the runtime verdict, stop the job mid-flight, page the operator, escalate.

    The ONE kill path — every rule in :func:`_runtime_watchdog` ends here, so the
    ordering guarantees below hold whichever deadline fired. ``progress`` is the sample
    the deciding rule acted on (None when there was none)."""
    # Stamp the mark FIRST. A terminal racing this task must find the verdict already
    # set: the mark is what keeps the plate gated, so a terminal that slipped past an
    # unmarked pending would release the gate onto a plate the printer was about to be
    # stopped over.
    fired_at = datetime.now(timezone.utc)
    _pending_eject[printer_id] = dataclasses.replace(current, runtime_exceeded_at=fired_at)
    line_number, _percent, _wire_at = _live_phase_telemetry(printer_id)
    logger.warning(
        "eject.remote: eject on printer %s still running at %.0fs (expected %.0fs, stage=%s, bed-drop span %s, "
        "gcode line %s, %s%% done) — suspect an under-bed obstruction or Z steps lost during the bed-drop; "
        "stopping the eject job mid-flight before the sweep can scrape the plate",
        printer_id,
        elapsed_s,
        armed.expected_runtime_s,
        stage,
        armed.drop_span_s,
        line_number,
        progress,
    )

    # Deliberately NOT via mark_printer_stopped_by_user: this is not an operator
    # stop, and nothing downstream may read the echoed status as intent. The mark
    # — not whatever terminal the printer reports — drives terminal handling.
    delivered = printer_manager.stop_print(printer_id)
    if not delivered:
        logger.warning(
            "eject.remote: stop command for printer %s was NOT delivered (no live MQTT session) — retrying once",
            printer_id,
        )
        await sleep(_STOP_RETRY_DELAY_S)
        delivered = printer_manager.stop_print(printer_id)
    # Proceed either way: an undelivered stop leaves the sweep running, but the
    # mark already guarantees no terminal can release the gate, and the operator
    # is being paged below.
    logger.warning(
        "eject.remote: mid-flight eject stop on printer %s %s",
        printer_id,
        "delivered" if delivered else "COULD NOT BE DELIVERED — the job may still be running",
    )

    # Lazy import: the monitor imports this module, so a module-level import here
    # is a cycle (same precedent as farm_policy's monitor imports).
    from backend.app.services.eject.monitor import _default_notify_plate_not_empty, eject_cooldown_monitor

    try:
        await _default_notify_plate_not_empty(
            printer_id,
            source_detail=_stop_source_detail(
                stage,
                elapsed_s,
                armed.expected_runtime_s or 0.0,
                armed.drop_span_s,
            ),
        )
    except Exception:  # noqa: BLE001 — a notify failure must never kill the watchdog
        logger.exception("eject.remote: mid-flight abort notification failed for printer %s", printer_id)
    # Re-escalate on the standard cadence if the alert is ignored. Self-deduping,
    # so an escalation-only watch already holding this gate is left alone.
    eject_cooldown_monitor.start_escalation_only_watch(printer_id)


async def _runtime_watchdog(
    printer_id: int,
    armed: PendingEject,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Stop the eject on ``printer_id`` when it overruns — as a whole, or in its bed-drop phase.

    This is the machine-stopping half of the 2026-07-31 gouged-plate response, and it
    acts DURING the job rather than at its terminal: the stall accrues in the bed
    drop/return phase, which finishes before the sweep begins, so a job stopped while
    that phase is still executing has not yet dragged the toolhead across the plate.
    Judging the same evidence at the terminal — as the mechanism this replaces did —
    can only ever report a scrape that already happened.

    Two deadlines, one kill path:

    * The WHOLE-JOB deadline (:func:`eject_abort_deadline_s`) is enforced on elapsed
      time in every state and needs no evidence at all. It is the original rule and the
      unconditional backstop.
    * The BED-DROP deadline needs ``drop_span_s`` plus the M73 phase beacons reflected
      in ``mc_percent``, and it is the only rule that can fire BEFORE the sweep on a
      stall too short to overrun the whole job (~59 s on the production profile). It
      fires only on FRESH samples; stale evidence advances nothing.

    The normal exit is CANCELLATION: :func:`clear_pending_eject` cancels this task the
    instant the eject's terminal is consumed, so a nominal sweep never reaches a kill.
    The identity re-check on every wake-up is the belt for the races cancellation loses
    (a terminal handled between wake-up and re-check, or a new eject dispatched onto the
    printer). ``sleep`` and ``clock`` are injectable together — they must come from the
    same time source — so tests can drive the whole sequence without wall-clock waits."""
    # The arming gate refuses a pending with no estimate, so this is always a float.
    expected_s: float = armed.expected_runtime_s  # type: ignore[assignment]
    total_deadline_s = eject_abort_deadline_s(expected_s)
    armed_at = clock()
    try:
        drop_span_s = armed.drop_span_s
        # A drop-less block (or a rehydrated pending) has no phase to bound, and a drop
        # whose deadline would not meaningfully precede the whole-job one adds only ways
        # to be wrong — both run the original single-sleep form.
        if drop_span_s is None or drop_span_s + _drop_margin_s(drop_span_s) >= total_deadline_s - _EDGE_LANE_MIN_LEAD_S:
            await sleep(total_deadline_s)
            current = _watchdog_still_owns(printer_id, armed)
            if current is None:
                return
            _line, percent, _wire_at = _live_phase_telemetry(printer_id)
            await _stop_and_page(
                printer_id,
                current,
                armed,
                sleep=sleep,
                stage="total",
                elapsed_s=_elapsed_since_start_s(current),
                progress=percent,
            )
            return
        await _watch_phase_edges(
            printer_id,
            armed,
            drop_span_s=drop_span_s,
            expected_s=expected_s,
            total_deadline_s=total_deadline_s,
            armed_at=armed_at,
            sleep=sleep,
            clock=clock,
        )
    finally:
        _runtime_watchdogs.pop(printer_id, None)


async def _watch_phase_edges(
    printer_id: int,
    armed: PendingEject,
    *,
    drop_span_s: float,
    expected_s: float,
    total_deadline_s: float,
    armed_at: float,
    sleep: Callable[[float], Awaitable[None]],
    clock: Callable[[], float],
) -> None:
    """Poll ``mc_percent`` and time the M73 phase edges (see :func:`_runtime_watchdog`).

    Returns on cancellation, on resolution/supersession, or after a kill; the caller owns
    deregistration."""
    # "await_p5" → "await_p50" → "sweeping"; "deadline_only" is the beacon-dead fallback,
    # where the whole-job deadline below is the only rule left.
    phase = "await_p5"
    # Monotonic time of the first fresh sample at/above the lifted beacon, and the
    # deadline derived from it. Both stay None until that edge is observed.
    t5: float | None = None
    drop_deadline: float | None = None
    # No P5 by here means the beacons are not reaching us at all: it clears the fixed job
    # spin-up plus the whole-job grace, while a healthy block beacons within seconds of
    # its first move.
    p5_deadline = armed_at + EJECT_RUNTIME_OVERHEAD_S + _abort_margin_s(expected_s)
    asked_for_evidence = False

    while True:
        await sleep(_PROGRESS_POLL_S)
        now = clock()
        current = _watchdog_still_owns(printer_id, armed)
        if current is None:
            return
        _line, percent, progress_wire_at = _live_phase_telemetry(printer_id)
        # FRESH = published by a push that landed after this eject started AND recently
        # enough to describe the phase running now. Anything else is not evidence.
        fresh = (
            percent is not None
            and progress_wire_at is not None
            and progress_wire_at >= armed_at
            and now - progress_wire_at <= _PROGRESS_FRESH_S
        )

        # The whole-job deadline is unconditional — it holds in every state and needs no
        # progress evidence, exactly as it did before the phase lane existed.
        if now - armed_at >= total_deadline_s:
            await _stop_and_page(
                printer_id,
                current,
                armed,
                sleep=sleep,
                stage="total",
                elapsed_s=_elapsed_since_start_s(current),
                progress=percent,
            )
            return
        if phase in ("sweeping", "deadline_only"):
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
            elif now >= p5_deadline:
                logger.warning(
                    "eject.remote: printer %s — M73 phase beacons not reflected in mc_percent (%s%% at %.0fs "
                    "after the start echo) — falling back to the total-deadline watchdog",
                    printer_id,
                    percent,
                    now - armed_at,
                )
                phase = "deadline_only"
                continue
            else:
                continue

        # await_p50: the pre-sweep guarantee. A percent below the sweep beacon PROVES the
        # printer is still executing drop-phase lines, so the plate is untouched and the
        # stop lands before any lane can scrape it.
        if not fresh:
            if drop_deadline is not None and now > drop_deadline and not asked_for_evidence:
                # Blind past the deadline: ask once for a fresh report rather than kill on
                # silence. The whole-job deadline remains the backstop either way.
                printer_manager.request_evidence_pushall(printer_id, _EVIDENCE_REASON)
                asked_for_evidence = True
            continue
        if percent >= PHASE_BEACON_SWEEP_PCT:
            if drop_deadline is not None and now > drop_deadline:
                # The sweep DID start, but only after the drop overran — a drop that runs
                # long is the lost-steps signature, and the sweep opens with rear
                # positioning at lift height, so the stop still precedes plate contact.
                await _stop_and_page(
                    printer_id,
                    current,
                    armed,
                    sleep=sleep,
                    stage="drop_late",
                    elapsed_s=_elapsed_since_start_s(current),
                    progress=percent,
                )
                return
            logger.info(
                "eject.remote: printer %s bed-drop phase cleared in <=%.1fs (budget %.0fs, deadline %.0fs) — sweeping",
                printer_id,
                now - t5 if t5 is not None else 0.0,
                drop_span_s,
                drop_span_s + _drop_margin_s(drop_span_s),
            )
            phase = "sweeping"
            continue
        if drop_deadline is not None and now > drop_deadline:
            await _stop_and_page(
                printer_id,
                current,
                armed,
                sleep=sleep,
                stage="drop",
                elapsed_s=_elapsed_since_start_s(current),
                progress=percent,
            )
            return


async def cancel_runtime_watchdog(printer_id: int) -> None:
    """Cancel and deregister ``printer_id``'s runtime watchdog, if one is armed."""
    task = _runtime_watchdogs.pop(printer_id, None)
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass  # Expected — the eject resolved before its abort deadline.


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
      of the pending. This closes the post-restart hole (W1/R2): after a restart the
      client's ``last_dispatch_subtask_id`` is gone, so id-matching turns lenient and
      ANY terminal would otherwise consume a HYDRATED pending and clear our gate — the
      name check re-establishes positive identity from the echoed job name.

    Name evidence alone (empty registry) NEVER makes this return True — see
    :func:`is_eject_job_name` for the suppress-only name signal. This function NEVER
    pops the registry; callers own the pop.
    """
    pending = peek_pending_eject(printer_id)
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
        expected_stem: str | None = None
        if pending.purpose == "manual":
            expected_stem = f"eject_manual_p{printer_id}"
        elif pending.queue_item_id is not None:
            expected_stem = expected_eject_stem(pending)
        if expected_stem is not None:
            name_mismatch = _eject_name_stem(subtask_name).lower() != expected_stem.lower()
    return not (id_mismatch or name_mismatch)


async def persist_pending_eject(db: AsyncSession, printer_id: int, pending: PendingEject) -> None:
    """Stamp ``eject_dispatched_at`` on the eject's owning queue unit (durable mirror).

    Same-session write on the caller's ``db`` (NOT a new/fire-and-forget session),
    committed here so the mirror is durable the instant dispatch is accepted. A
    missing / queue-item-less pending is a no-op (nothing to mirror)."""
    if pending.queue_item_id is None:
        return
    item = await db.get(PrintQueueItem, pending.queue_item_id)
    if item is None:
        return
    item.eject_dispatched_at = datetime.now(timezone.utc)
    await db.commit()


async def clear_pending_eject(db: AsyncSession, printer_id: int) -> PendingEject | None:
    """Resolve the pending eject on ``printer_id``: pop the in-memory registry AND
    NULL every in-flight eject stamp on that printer (atomic with resolution).

    Printer-scoped NULL (not just the popped entry's item) so a crash that stamped
    more than one row for a printer can't leave an orphan stamp behind. Returns the
    popped :class:`PendingEject` (or None). Commits only when a stamp was cleared.

    This is also where the in-flight runtime watchdog is cancelled: EVERY consumed
    eject terminal funnels through here, so one cancel covers all of them and no
    resolved eject can leave a task waiting to stop a printer that already finished."""
    pending = pop_pending_eject(printer_id)
    await cancel_runtime_watchdog(printer_id)
    result = await db.execute(
        select(PrintQueueItem).where(
            PrintQueueItem.printer_id == printer_id,
            PrintQueueItem.eject_dispatched_at.is_not(None),
        )
    )
    changed = False
    for item in result.scalars().all():
        item.eject_dispatched_at = None
        changed = True
    if changed:
        await db.commit()
    return pending


async def hydrate_pending_ejects_from_db() -> int:
    """Rebuild the in-memory pending-eject registry from durable stamps at startup.

    Selects every ``eject_dispatched_at IS NOT NULL`` unit (newest first) and rebuilds
    :class:`PendingEject` keyed by ``printer_id`` (``purpose`` from ``first_article``,
    ``run_id`` from ``batch_id``). Stamps older than ``_PENDING_EJECT_STALE_TTL_H`` are
    dropped (NULLed) with a WARNING; if two unresolved rows resolve to one printer
    (only possible via a crash between cycles), the newest stamp is kept and the rest
    NULLed with a WARNING — the registry is one-per-printer by construction. Returns
    the number of pending ejects rehydrated."""
    from backend.app.core.database import async_session

    cutoff = datetime.now(timezone.utc) - timedelta(hours=_PENDING_EJECT_STALE_TTL_H)
    hydrated = 0
    async with async_session() as db:
        result = await db.execute(
            select(PrintQueueItem)
            .where(PrintQueueItem.eject_dispatched_at.is_not(None))
            .order_by(PrintQueueItem.eject_dispatched_at.desc())
        )
        rows = list(result.scalars().all())
        seen_printers: set[int] = set()
        changed = False
        for item in rows:
            stamp = item.eject_dispatched_at
            if stamp is not None and stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            if stamp is None or stamp < cutoff:
                logger.warning(
                    "eject.remote: dropping stale pending-eject stamp on item %s (dispatched %s, TTL %sh)",
                    item.id,
                    item.eject_dispatched_at,
                    _PENDING_EJECT_STALE_TTL_H,
                )
                item.eject_dispatched_at = None
                changed = True
                continue
            if item.printer_id is None:
                logger.warning("eject.remote: dropping pending-eject stamp on item %s — no printer_id", item.id)
                item.eject_dispatched_at = None
                changed = True
                continue
            if item.printer_id in seen_printers:
                logger.warning(
                    "eject.remote: multiple pending ejects for printer %s — NULLing older stamp on item %s",
                    item.printer_id,
                    item.id,
                )
                item.eject_dispatched_at = None
                changed = True
                continue
            seen_printers.add(item.printer_id)
            register_pending_eject(
                item.printer_id,
                PendingEject(
                    purpose="fa" if item.first_article else "production",
                    run_id=item.batch_id,
                    queue_item_id=item.id,
                ),
            )
            hydrated += 1
        if changed:
            await db.commit()
    if hydrated:
        logger.info("eject.remote: hydrated %d pending eject(s) from durable stamps", hydrated)
    return hydrated


class EjectDispatchError(RuntimeError):
    """A part-present eject could not be dispatched.

    Carries an HTTP ``status_code`` hint (409 precondition / 502 transport) so the
    FA route can translate it to an ``HTTPException`` without this module importing
    FastAPI. The monitor ignores the hint and treats any raise as a dispatch failure.
    """

    def __init__(self, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


async def _resolve_source_path(db: AsyncSession, item: PrintQueueItem) -> Path:
    """The on-disk source ``.gcode.3mf`` for ``item`` (the file it printed), or a 409.

    Thin adapter over ``farm_correlation.resolve_item_donor``, which is THE
    "which file did this unit print" resolver — the same question the print-start
    archive capture asks, answered once. This lane's only difference is that a
    missing donor is a dispatch precondition failure rather than a fall-back-to-
    guessing, so it raises instead of returning None.
    """
    from backend.app.services.farm_correlation import resolve_item_donor

    donor = await resolve_item_donor(db, item)
    if donor is None:
        raise EjectDispatchError("Eject source file not found on disk for the finished unit", status_code=409)
    return donor.local_path


async def dispatch_part_present_eject(
    db: AsyncSession,
    *,
    printer_id: int,
    queue_item_id: int,
    purpose: EjectPurpose,
    run_id: int | None,
) -> None:
    """Build + FTPS-upload + dispatch a part-present motion-only eject for one unit.

    Resolves the profile / geometry / source file from ``queue_item_id`` and the
    target printer, builds the standalone eject-only file, uploads it (honouring the
    FTP retry settings) and starts it with EVERY pre-print calibration OFF, then
    registers a :class:`PendingEject`. Does NOT touch the plate-clear gate — that
    clears only when the eject job's terminal arrives.

    Raises :class:`EjectDispatchError` on any precondition (409) or transport (502)
    failure, leaving no half state (nothing is registered unless ``start_print``
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

    # Fail-closed on a model with no geometry row or a row not hardware-validated —
    # a production eject must never drive an unvalidated envelope.
    try:
        geometry = await get_geometry_required(db, printer.model, require_validated=True)
    except GeometryUnavailable as exc:
        raise EjectDispatchError(exc.reason, status_code=409) from exc

    profile = await db.get(EjectProfile, item.eject_profile_id)
    if profile is None:
        raise EjectDispatchError("Eject profile not found", status_code=409)

    source_path = await _resolve_source_path(db, item)
    plate_id = item.plate_id or 1
    eject_progress.emit_eject_progress(printer_id=printer.id, queue_item_id=queue_item_id, phase="building")
    try:
        built = await build_part_present_eject_file(source_path, plate_id, profile, geometry)
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
        drop_span_s=built.drop_span_s,
    )
    # The eject file's FTPS upload transiently drops the H2S sdcard flag; mark the
    # printer upload-in-flight so the USB-drop verifier ignores that dispatch blip.
    async with upload_in_flight(printer.id):
        await _upload_start_register_eject(
            db,
            printer=printer,
            eject_path=built.path,
            job_stem=f"eject_{purpose}_item{queue_item_id}",
            plate_id=plate_id,
            pending=pending,
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
    and this shares ``dispatch_part_present_eject``'s upload→start→register tail. It is
    NOT queue-item-bound — it registers a ``purpose="manual"`` :class:`PendingEject`
    with ``queue_item_id=None``. That no-op mirror is DELIBERATE: a manual eject is not
    restart-durable, so a mid-eject restart leaves the plate gate raised (fail-closed).

    Geometry is fail-closed (``require_validated=True``); the caller owns cleanup of
    ``source_path`` (it may be a temp FTPS re-fetch). ``max_z_override`` is the
    operator's confirmed part height, superseding the donor header in the build — the
    donor may be an assumed fallback rather than the print on the plate. Raises
    :class:`EjectDispatchError` on any precondition (409) or transport (502) failure,
    leaving nothing registered unless ``start_print`` was accepted.
    """
    printer = await db.get(Printer, printer_id)
    if printer is None:
        raise EjectDispatchError("Eject printer not found", status_code=409)
    if not printer_manager.is_connected(printer.id):
        raise EjectDispatchError("Printer is not connected; cannot eject remotely", status_code=409)

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
        drop_span_s=built.drop_span_s,
    )
    # Same as the production path: the FTPS upload transiently drops the H2S sdcard
    # flag; mark the printer upload-in-flight so the USB-drop verifier ignores the blip.
    async with upload_in_flight(printer.id):
        await _upload_start_register_eject(
            db,
            printer=printer,
            eject_path=built.path,
            job_stem=f"eject_manual_p{printer_id}",
            plate_id=plate_id,
            pending=pending,
        )
    logger.info(
        "eject.remote: dispatched manual (foreign-plate) eject on printer %s (plate %s, profile %s)",
        printer_id,
        plate_id,
        profile_id,
    )


async def _upload_start_register_eject(
    db: AsyncSession,
    *,
    printer: Printer,
    eject_path: Path,
    job_stem: str,
    plate_id: int,
    pending: PendingEject,
) -> None:
    """Shared eject tail: FTPS-upload the built eject file (honouring the FTP retry
    settings), start it with EVERY pre-print calibration OFF, then register + durably
    mirror the pending eject. The built ``eject_path`` is always cleaned up; nothing is
    registered unless ``start_print`` was accepted. Raises :class:`EjectDispatchError`
    (502) on upload / start failure.

    The ``start_print`` file, the MQTT ``project_file`` param (plate path, keyed by
    ``plate_id``) and the eventual SD cleanup all key off the SAME ``remote_filename``
    (the bare ``job_stem``).
    """
    from backend.app.services.bambu_ftp import (
        cleanup_downloaded_3mf,
        get_ftp_retry_settings,
        upload_file_async,
        with_ftp_retry,
    )
    from backend.app.utils.filename import derive_remote_filename

    remote_filename = derive_remote_filename(f"{job_stem}.gcode.3mf")
    remote_path = f"/{remote_filename}"
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

    if not uploaded:
        eject_progress.emit_eject_progress(printer_id=printer.id, queue_item_id=pending.queue_item_id, phase="failed")
        raise EjectDispatchError("Failed to upload the eject file to the printer", status_code=502)

    # EVERY pre-print calibration OFF — never bed-probe / shake / re-level with a
    # part on the plate (the old FA call omitted bed_levelling/vibration_cali and
    # defaulted them True — a hazard this closes).
    started = printer_manager.start_print(
        printer.id,
        remote_filename,
        plate_id=plate_id,
        bed_levelling=False,
        flow_cali=False,
        vibration_cali=False,
        layer_inspect=False,
        timelapse=False,
        use_ams=False,
    )
    if not started:
        eject_progress.emit_eject_progress(printer_id=printer.id, queue_item_id=pending.queue_item_id, phase="failed")
        raise EjectDispatchError("Failed to send the eject command to the printer", status_code=502)

    eject_progress.emit_eject_progress(printer_id=printer.id, queue_item_id=pending.queue_item_id, phase="sent")

    register_pending_eject(printer.id, pending)
    # Durable mirror: stamp the owning unit so the eject survives a restart between
    # here and its terminal (W1). A manual/foreign eject has no queue item, so this is
    # a deliberate no-op — the gate stays raised across a restart (fail-closed).
    await persist_pending_eject(db, printer.id, pending)
