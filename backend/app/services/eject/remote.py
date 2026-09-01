"""Shared part-present eject dispatcher + the two timers that bound a dispatched sweep.

The eject sweep is a SEPARATE, server-dispatched, motion-only job — used by two
callers that share this ONE path:

- **Production loop**: the eject monitor, once the live bed reaches the release
  threshold, dispatches the eject for the finished unit (``purpose="production"``).
- **First article**: an operator approval with ``eject_remotely`` dispatches the
  eject for the FA plate (``purpose="fa"``).

Both build a standalone motion-only ``.gcode.3mf`` (``build_part_present_eject_file``),
FTPS-upload it and ``project_file``-dispatch it via ``printer_manager.start_print``
with EVERY pre-print calibration OFF (never bed-probe / shake with a part on the
plate), then CLAIM the printer through the plate-occupancy authority
(``claim_for_eject``) so the terminal handler can match the job's echoed
``subtask_id`` and act on completion. The plate-clear gate is NOT cleared here — it
drops only when the eject job's terminal is positively matched.

Since the 2026-08-30 cut-over this module holds NO eject state of its own: the one
pending-eject record per printer lives in ``services/plate_occupancy`` (the authority)
and its durable mirror is written by ``plate_occupancy_store``. What stays here are
the two DISPATCHERS and the two TIMERS that bound a dispatched sweep — the start
deadline (did the printer ever begin it?) and the runtime watchdog (is it still
running long past its estimate?) — both of which write their verdicts back through
the authority's transitions.

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
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from backend.app.core.tasks import spawn_background_task
from backend.app.models.eject_profile import EjectProfile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services.eject import progress as eject_progress
from backend.app.services.eject.dispatch import build_part_present_eject_file
from backend.app.services.eject.generator import (
    EJECT_RUNTIME_OVERHEAD_S,
    PHASE_BEACON_LIFTED_PCT,
    PHASE_BEACON_PARK_PCT,
    PHASE_BEACON_SWEEP_PCT,
    UNMODELLED_EPILOGUE_ALLOWANCE_S,
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
from backend.app.services.usb_storage import upload_in_flight

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Re-exported so the eject lane's own callers keep one import site for the vocabulary
# they dispatch with. The definitions live in the authority — this module stores none
# of it.
__all__ = [
    "EJECT_START_TIMEOUT_S",
    "EjectDispatchError",
    "EjectPurpose",
    "PendingEject",
    "dispatch_foreign_eject",
    "dispatch_identified_foreign_eject",
    "dispatch_part_present_eject",
    "expected_eject_stem",
    "is_eject_job_name",
    "matches_pending_eject",
    "on_eject_start_echo",
    "parse_eject_job_name",
]

# printer_id -> the armed runtime watchdog for that printer's in-flight eject.
# One entry at most: an eject is one-per-printer by construction, and every path
# that ends an eject (resolution, or a new dispatch) drops the entry.
_runtime_watchdogs: dict[int, asyncio.Task] = {}

# printer_id -> the armed START-deadline timer for that printer's dispatched eject.
_start_deadlines: dict[int, asyncio.Task] = {}

# How long a dispatched eject may go without the printer echoing its PRINT START
# before the farm concludes the sweep will never happen.
#
# The firmware silently IGNORES a ``project_file`` sent while it is busy — no error,
# no terminal, nothing — which is how the 2026-08-30 ejects on printers 2/3/4
# (``eject_manual_p2`` / ``p3`` / ``p4``, dispatched into prints that had just
# started) stayed registered forever and made every later eject 409
# ``eject_in_flight``, until the operator hand-jogged the toolhead.
#
# The fleet's start echo is ~45 s from dispatch (44 s and 43 s measured on
# 2026-08-30), so 4× that tolerates a slow FTPS pass plus a missed status push and
# still frees the printer inside three minutes. A pending past this deadline with no
# start echo is DEAD: nothing else about that shape produces a signal.
EJECT_START_TIMEOUT_S = 180.0

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
# sweep beacon did arrive, but only after the drop phase had already overrun; "epilogue"
# = the sweep was OBSERVED complete and the job then failed to finish its firmware tail.
#
# There is deliberately no "sweep" token: the sweep lane MEASURES this wave and does not
# kill (the P50→P75 distribution it logs is what arms a calibrated sweep kill next wave),
# so a job overrunning its sweep budget is still stopped by the whole-job deadline under
# "total" — today's timing, unchanged.
EjectStopStage = Literal["total", "drop", "drop_late", "epilogue"]

# Where the phase poller is in the block. Typed beside the stop vocabulary because the
# two are read together: the phase decides which deadline binds, and the deadline names
# the stage a kill is attributed to. Each is handled by its OWN branch that terminates
# the iteration — a phase falling through into another phase's rules is how a percent
# at the PARK beacon could be answered with a bed-drop kill.
_WatchPhase = Literal["await_p5", "await_p50", "await_p75", "epilogue", "deadline_only"]

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


def _sweep_margin_s(sweep_span_s: float) -> float:
    """Grace added to the sweep phase's budget before the overrun is REPORTED (25%, [8 s, 20 s]).

    Borrows the drop lane's triple on purpose. The P50→P75 span has never been measured
    — the five kills that read 50% were "mid-sweep or stale", indistinguishable before
    the park beacon was consumed — so no calibrated figure exists yet and inventing one
    would be a third constant with no evidence behind it. Nothing is stopped on this
    number this wave: it only decides when a WARNING is worth emitting, and the
    distribution those warnings produce is what a calibrated floor will be set from.
    """
    return _clamped_margin_s(sweep_span_s, EJECT_DROP_MARGIN_FRAC, EJECT_DROP_MARGIN_MIN_S, EJECT_DROP_MARGIN_MAX_S)


def eject_abort_deadline_s(expected_runtime_s: float) -> float:
    """Seconds of execution after which an eject is aborted mid-flight.

    ``expected`` plus a margin of 25% of the estimate, clamped to [20 s, 60 s]."""
    return expected_runtime_s + _abort_margin_s(expected_runtime_s)


def _binding_deadline(
    phase: _WatchPhase,
    *,
    armed_at: float,
    total_deadline_s: float,
    t75: float | None,
    tail_s: float | None,
) -> tuple[float, EjectStopStage]:
    """The ONE deadline in force for ``phase``, as (monotonic instant, stage it kills under).

    Evaluated once per poll, before any phase rule runs, so exactly one rule can ever
    decide that an eject has run out of time — and ``total_deadline_s`` is a parameter
    that is never mutated, which is what keeps "the whole-job deadline" meaning the same
    number from arming to kill.

    In every phase but ``epilogue`` that number is the whole-job deadline, unchanged
    since 2026-07-31: a job whose sweep may still be crossing the plate gets today's
    timing exactly, whether or not the beacons are reaching us.

    ``epilogue`` is entered ONLY on fresh wire evidence that the sweep completed, and it
    is the one window where the deadline loosens: the estimator does not model the
    firmware's job-completion tail (see :data:`UNMODELLED_EPILOGUE_ALLOWANCE_S`), so a
    job that has provably finished sweeping and is merely slow to report FINISH was
    being killed by a deadline measuring work it had already done. The ``max`` is the
    invariant that this replacement can only ever LOOSEN — at a smaller future allowance
    it silently reverts to the whole-job rule rather than becoming tighter than it.
    Missing figures fail closed the same way (the whole-job deadline still binds).
    """
    whole_job_at = armed_at + total_deadline_s
    if phase == "epilogue" and t75 is not None and tail_s is not None:
        return max(whole_job_at, t75 + tail_s + UNMODELLED_EPILOGUE_ALLOWANCE_S), "epilogue"
    return whole_job_at, "total"


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


def expected_eject_stem(pending: PendingEject | EjectIdentity) -> str:
    """The eject job stem THIS pending eject was dispatched under.

    Accepts the record or its identity projection — both carry the two fields the
    stem is minted from, and the matcher only ever holds the projection.
    """
    return f"eject_{pending.purpose}_item{pending.queue_item_id}"


# --------------------------------------------------------------------------- #
# Start echo + the two timers that bound a dispatched sweep
# --------------------------------------------------------------------------- #
def on_eject_start_echo(printer_id: int) -> None:
    """The printer echoed PRINT START for the eject sweep on ``printer_id``.

    Called from the print-START callback — i.e. when the printer says it BEGAN
    executing the eject file, not when we uploaded or commanded it. Only that edge
    measures machine time: upload + job spin-up vary with file size and FTPS
    conditions and would otherwise be charged to the sweep.

    Three duties, in order:

    1. stamp the start through the authority (:meth:`note_eject_started`, first-write
       -wins, so a replayed echo can never shorten a measured runtime);
    2. cancel the START DEADLINE — the sweep demonstrably started, so the "the
       firmware ignored our project_file" timer has nothing left to catch;
    3. ARM the in-flight runtime watchdog, this being the first moment a deadline can
       be measured from.

    The watchdog arms on the FIRST echo only, and only for a verifiable sweep: a
    pending whose ``started_at`` was already stamped is a duplicate echo, a pending
    with no ``expected_runtime_s`` is a rehydrated post-restart record (nothing to
    judge against — the startup reconciler owns those, fail-closed), and a printer
    that already carries a watchdog is not given a second one.
    """
    before = plate_occupancy.eject_identity(printer_id)
    if before is None:
        return
    plate_occupancy.note_eject_started(printer_id)
    _cancel_start_deadline(printer_id)

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


def _arm_start_deadline(printer_id: int) -> None:
    """Arm (replacing any predecessor) the never-started deadline for ``printer_id``."""
    _cancel_start_deadline(printer_id)
    _start_deadlines[printer_id] = spawn_background_task(
        _start_deadline(printer_id), name=f"eject-start-deadline-{printer_id}"
    )


def _cancel_start_deadline(printer_id: int) -> None:
    """Cancel + deregister ``printer_id``'s start deadline, if one is armed."""
    task = _start_deadlines.pop(printer_id, None)
    if task is not None and not task.done():
        task.cancel()


def cancel_eject_timers(printer_id: int) -> None:
    """Drop both timers for ``printer_id``. Synchronous, idempotent, never raises.

    THE one deregistration point, called from the occupancy policy driver on every
    transition that leaves the printer with no eject — a matched terminal, an
    unverified resolve, a start expiry, the startup reconciler's disposal, an
    operator recover. Routing all of them through one level-triggered check is what
    stops a resolved eject leaving a task armed to stop a printer that already
    finished; the watchdog's own identity re-check remains the belt for a cancel that
    lands after its deadline has already elapsed.
    """
    _cancel_start_deadline(printer_id)
    watchdog = _runtime_watchdogs.pop(printer_id, None)
    if watchdog is not None and not watchdog.done():
        watchdog.cancel()


async def _start_deadline(
    printer_id: int,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    timeout_s: float = EJECT_START_TIMEOUT_S,
) -> None:
    """Retire an eject the printer never started, and page a human.

    See :data:`EJECT_START_TIMEOUT_S` for why the shape exists at all. The authority
    decides whether the expiry actually fires (it refuses on a started or hydrated
    record), so this task can wake late or spuriously without consequence — a False
    return means somebody else already resolved the eject and there is nothing to
    say."""
    try:
        await sleep(timeout_s)
        if not plate_occupancy.expire_eject_start(printer_id):
            return
        logger.warning(
            "eject.remote: printer %s never echoed a PRINT START for its eject within %.0fs — the firmware "
            "ignores a project_file sent while it is busy, so the sweep never ran; pending dropped and the "
            "plate stays gated for a human",
            printer_id,
            timeout_s,
        )
        from backend.app.services.eject.monitor import notify_plate_not_empty

        await notify_plate_not_empty(
            printer_id,
            source_detail=(
                f"the eject sweep was dispatched but the printer never started it within {timeout_s:.0f}s — "
                "the plate has NOT been swept. Clear it by hand, or eject again now that the printer is free."
            ),
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — a deadline failure must never escape the task
        logger.exception("eject.remote: eject start deadline failed for printer %s", printer_id)
    finally:
        if _start_deadlines.get(printer_id) is asyncio.current_task():
            _start_deadlines.pop(printer_id, None)


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


def _stop_reason(
    stage: EjectStopStage,
    *,
    fired_deadline_s: float,
    phase_elapsed_s: float,
    expected_s: float,
    drop_span_s: float | None,
    sweep_span_s: float | None,
) -> tuple[str, str]:
    """(diagnostic clause for the kill WARNING, operator sentence for the page).

    THE one origin of kill copy. Both renderings say the same thing to two audiences, so
    a stage cannot be honest in the log and wrong in the page — which is what the single
    hardcoded "suspect an under-bed obstruction" clause was, for every stage that is not
    a bed-drop kill.

    ``drop_span_s`` and ``sweep_span_s`` are the budgets that were IN FORCE when the
    deadline fired, NOT the build's figures: a phase budget is in force only once that
    phase has been OBSERVED to open. That is what separates the two "total" kills — a
    sweep budget in force can only have been armed by an on-time P50 edge, so the
    bed-drop phase demonstrably cleared and blaming an under-bed obstruction would be a
    guess contradicted by the wire. With no beacons observed there is no such evidence
    and the historical obstruction wording stands.

    ``phase_elapsed_s`` is measured from the edge that opened the phase named by
    ``stage``; where no phase was ever observed it is the whole job's elapsed time,
    because then the whole job IS the only phase the farm can speak about.
    """
    drop_budget = f"{drop_span_s:.0f}s" if drop_span_s is not None else "its"
    if stage == "drop":
        return (
            f"the bed-drop phase has run {phase_elapsed_s:.0f}s against a {drop_budget} budget with the sweep "
            "still unreached — suspect an under-bed obstruction or Z steps lost during the bed-drop",
            f"the eject was STOPPED DURING the bed-drop phase, {phase_elapsed_s:.0f}s into a drop budgeted at "
            f"{drop_budget} with the sweep not yet started, so the bed may be stalled against an obstruction. "
            "Check under the heatbed and inspect the build plate before clearing it.",
        )
    if stage == "drop_late":
        return (
            f"the sweep beacon arrived only after the bed-drop phase overran its {drop_budget} budget "
            f"({phase_elapsed_s:.0f}s in) — a long drop is the lost-steps signature, and the sweep opens with "
            "rear positioning at lift height, so this stop still precedes plate contact",
            f"the eject was STOPPED AFTER a bed-drop phase that overran ({phase_elapsed_s:.0f}s in, against a "
            f"{drop_budget} bed-drop budget) — a drop that runs long can lose Z steps and return the bed too "
            "high for the sweep. Check under the heatbed and inspect the build plate before clearing it.",
        )
    if stage == "epilogue":
        return (
            f"the bed-drop and sweep phases both cleared on the wire; the job then sat {phase_elapsed_s:.0f}s "
            f"past its park beacon without finishing its firmware tail (deadline {fired_deadline_s:.0f}s)",
            f"the eject's sweep COMPLETED and the job then failed to finish, {phase_elapsed_s:.0f}s past the "
            "point where the sweep was reported done — the part was swept but the plate has NOT been verified. "
            "Inspect the build plate before clearing it.",
        )
    if sweep_span_s is not None:
        return (
            f"the bed-drop phase cleared on budget and the job was still sweeping {phase_elapsed_s:.0f}s after "
            f"the sweep beacon (budget {sweep_span_s:.0f}s) when the whole-job deadline "
            f"({fired_deadline_s:.0f}s) fired — suspect a part that did not release",
            f"the eject sweep was STOPPED at its whole-job deadline after {phase_elapsed_s:.0f}s of sweeping "
            f"(a {sweep_span_s:.0f}s sweep budget) — the bed-drop phase had already cleared, so the part may "
            "not have released from the plate. Inspect the build plate before clearing it.",
        )
    return (
        "suspect an under-bed obstruction or Z steps lost during the bed-drop",
        f"the eject sweep was STOPPED mid-job after {phase_elapsed_s:.0f}s against an expected "
        f"{expected_s:.0f}s — it may have stalled against an obstruction. "
        "Check under the heatbed and inspect the build plate before clearing it.",
    )


async def _stop_and_page(
    printer_id: int,
    current: EjectIdentity,
    armed: PendingEject,
    *,
    sleep: Callable[[float], Awaitable[None]],
    stage: EjectStopStage,
    fired_deadline_s: float,
    phase_elapsed_s: float,
    progress: float | None,
    line_number: int | None,
    drop_span_s: float | None,
    sweep_span_s: float | None,
) -> None:
    """Stamp the runtime verdict, stop the job mid-flight, page the operator.

    The ONE kill path — every rule in :func:`_runtime_watchdog` ends here, so the
    ordering guarantees below hold whichever deadline fired. ``progress`` and
    ``line_number`` are the deciding rule's OWN telemetry sample, handed down rather
    than re-read here: a second read would report the state of the printer a moment
    later than the one the kill was decided on, which is the one thing a post-incident
    reader must be able to trust. ``drop_span_s`` / ``sweep_span_s`` are the budgets in
    force at that moment (see :func:`_stop_reason`).

    No escalation watch is armed here, unlike the pre-cut-over code: while an eject
    owns the printer the plate carries no watch by construction (an armed cooldown
    over a plate a sweep is crossing is the double-dispatch the authority exists to
    forbid). The escalation hold arrives one step later and from one place — the
    stopped sweep's terminal resolves ``unverified``, which puts the plate under
    ``EscalationOnly`` and the policy driver arms the hold off THAT."""
    # Stamp the mark FIRST. A terminal racing this task must find the verdict already
    # set: the mark is what keeps the plate gated, so a terminal that slipped past an
    # unmarked pending would release the gate onto a plate the printer was about to be
    # stopped over.
    #
    # RECORDED REFUSAL: this stamp is unconditional across stages, so even an "epilogue"
    # kill — where the wire said the sweep COMPLETED — resolves the eject `unverified`
    # and leaves the plate gated under EscalationOnly. Deliberate, per the 2026-07-31
    # contract: sweep motion complete is not part off plate, and a farm that clears a
    # gate on a job it had to stop is judging on a criterion other than the one it
    # stopped for.
    elapsed_s = _elapsed_since_start_s(current)
    diagnostic, source_detail = _stop_reason(
        stage,
        fired_deadline_s=fired_deadline_s,
        phase_elapsed_s=phase_elapsed_s,
        expected_s=armed.expected_runtime_s or 0.0,
        drop_span_s=drop_span_s,
        sweep_span_s=sweep_span_s,
    )
    fired_at = datetime.now(timezone.utc)
    plate_occupancy.note_eject_runtime_exceeded(printer_id, fired_at, stage)
    logger.warning(
        "eject.remote: eject on printer %s still running at %.0fs (expected %.0fs, stage=%s, deadline %.0fs, "
        "bed-drop span %s, gcode line %s, %s%% done) — %s; stopping the eject job mid-flight",
        printer_id,
        elapsed_s,
        armed.expected_runtime_s,
        stage,
        fired_deadline_s,
        armed.drop_span_s,
        line_number,
        progress,
        diagnostic,
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
    from backend.app.services.eject.monitor import notify_plate_not_empty

    try:
        await notify_plate_not_empty(printer_id, source_detail=source_detail)
    except Exception:  # noqa: BLE001 — a notify failure must never kill the watchdog
        logger.exception("eject.remote: mid-flight abort notification failed for printer %s", printer_id)


async def _runtime_watchdog(
    printer_id: int,
    armed: PendingEject,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Stop the eject on ``printer_id`` when it overruns the deadline in force for its phase.

    This is the machine-stopping half of the 2026-07-31 gouged-plate response, and it
    acts DURING the job rather than at its terminal: the stall accrues in the bed
    drop/return phase, which finishes before the sweep begins, so a job stopped while
    that phase is still executing has not yet dragged the toolhead across the plate.
    Judging the same evidence at the terminal — as the mechanism this replaces did —
    can only ever report a scrape that already happened.

    A deadline per phase, one kill path:

    * The WHOLE-JOB deadline (:func:`eject_abort_deadline_s`) is enforced on elapsed
      time in every phase where the plate may still be at risk, and needs no evidence at
      all. It is the original rule and the unconditional backstop.
    * The BED-DROP deadline needs ``drop_span_s`` plus the M73 phase beacons reflected
      in ``mc_percent``, and it is the only rule that can fire BEFORE the sweep on a
      stall too short to overrun the whole job (~59 s on the production profile). It
      fires only on FRESH samples; stale evidence advances nothing.
    * The EPILOGUE deadline replaces the whole-job one ONLY after a fresh sample proves
      the sweep completed. Until 2026-08-31 the whole-job deadline was the sole rule
      left once the sweep began, and 23 of the 24 kills in the beacon era fired there —
      after the phase they were protecting, on jobs whose parts had physically been
      ejected — converting a successful sweep into a gated plate and a page. Patience
      past that point costs downtime and risks nothing: no motion over the plate remains.
    * The SWEEP phase is measured and never given patience (a part that fails to release
      shows up as a sweep overrun): it keeps the whole-job deadline and only WARNs.

    The normal exit is CANCELLATION: :func:`cancel_eject_timers` — driven off the
    occupancy transition that retires the eject — cancels this task the instant the
    eject's terminal is consumed, so a nominal sweep never reaches a kill.
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
            line, percent, _wire_at = _live_phase_telemetry(printer_id)
            # No phase was ever observed here, so no phase budget was in force and the
            # whole job is the only span this kill can honestly speak about.
            await _stop_and_page(
                printer_id,
                current,
                armed,
                sleep=sleep,
                stage="total",
                fired_deadline_s=total_deadline_s,
                phase_elapsed_s=_elapsed_since_start_s(current),
                progress=percent,
                line_number=line,
                drop_span_s=None,
                sweep_span_s=None,
            )
            return
        await _watch_phase_edges(
            printer_id,
            armed,
            drop_span_s=drop_span_s,
            sweep_span_s=armed.sweep_span_s,
            tail_s=armed.tail_s,
            expected_s=expected_s,
            total_deadline_s=total_deadline_s,
            armed_at=armed_at,
            sleep=sleep,
            clock=clock,
        )
    finally:
        _runtime_watchdogs.pop(printer_id, None)


def _phase_elapsed_s(
    phase: _WatchPhase, now: float, *, t50: float | None, t75: float | None, current: EjectIdentity
) -> float:
    """Seconds since the observed edge that opened ``phase``.

    Falls back to the whole job's elapsed time wherever no phase edge was ever observed,
    because there the whole job IS the only span the farm can honestly speak about."""
    if phase == "await_p75" and t50 is not None:
        return now - t50
    if phase == "epilogue" and t75 is not None:
        return now - t75
    return _elapsed_since_start_s(current)


async def _watch_phase_edges(
    printer_id: int,
    armed: PendingEject,
    *,
    drop_span_s: float,
    sweep_span_s: float | None,
    tail_s: float | None,
    expected_s: float,
    total_deadline_s: float,
    armed_at: float,
    sleep: Callable[[float], Awaitable[None]],
    clock: Callable[[], float],
) -> None:
    """Poll ``mc_percent``, time the M73 phase edges, enforce the deadline each phase owns.

    One branch per :data:`_WatchPhase`, each terminating the iteration. That shape is
    load-bearing, not stylistic: the predecessor skipped the phase rules for a TUPLE of
    states and let everything else fall through into the bed-drop branch, so a phase
    added without also editing that tuple would have answered a percent at the PARK
    beacon with a bed-drop kill.

    Returns on cancellation, on resolution/supersession, or after a kill; the caller owns
    deregistration."""
    phase: _WatchPhase = "await_p5"
    # Monotonic time of the first fresh sample at/above each beacon, plus the budgets
    # derived from them. Each stays None until its own edge is observed.
    t5: float | None = None
    t50: float | None = None
    t75: float | None = None
    drop_deadline: float | None = None
    sweep_budget_deadline: float | None = None
    # No P5 by here means the beacons are not reaching us at all: it clears the fixed job
    # spin-up plus the whole-job grace, while a healthy block beacons within seconds of
    # its first move.
    p5_deadline = armed_at + EJECT_RUNTIME_OVERHEAD_S + _abort_margin_s(expected_s)
    asked_for_evidence = False
    warned_sweep_overrun = False

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
        # A phase budget is IN FORCE only from the edge that opened its phase — what
        # :func:`_stop_reason` renders honest copy from.
        drop_in_force = drop_span_s if phase in ("await_p50", "await_p75", "epilogue") else None
        sweep_in_force = sweep_span_s if phase in ("await_p75", "epilogue") else None

        # ONE deadline-selection point, evaluated before any phase rule, so exactly one
        # rule can decide that this eject has run out of time.
        #
        # ACCEPTED RESIDUE: an edge sample landing on the same poll tick as a deadline
        # expiry resolves in the DEADLINE's favour, since the deadline is tested first.
        # The window is one poll tick, and that bias stops a machine that may be in
        # trouble rather than granting it the next phase's patience.
        deadline_at, deadline_stage = _binding_deadline(
            phase, armed_at=armed_at, total_deadline_s=total_deadline_s, t75=t75, tail_s=tail_s
        )
        if now >= deadline_at:
            await _stop_and_page(
                printer_id,
                current,
                armed,
                sleep=sleep,
                stage=deadline_stage,
                fired_deadline_s=deadline_at - armed_at,
                phase_elapsed_s=_phase_elapsed_s(phase, now, t50=t50, t75=t75, current=current),
                progress=percent,
                line_number=line_number,
                drop_span_s=drop_in_force,
                sweep_span_s=sweep_in_force,
            )
            return

        if phase == "deadline_only":
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

        if phase == "await_p50":
            # The pre-sweep guarantee. A percent below the sweep beacon PROVES the
            # printer is still executing drop-phase lines, so the plate is untouched and
            # the stop lands before any lane can scrape it.
            if not fresh:
                if drop_deadline is not None and now > drop_deadline and not asked_for_evidence:
                    # Blind past the deadline: ask once for a fresh report rather than
                    # kill on silence. The whole-job deadline remains the backstop.
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
                        sleep=sleep,
                        stage="drop_late",
                        fired_deadline_s=drop_deadline - armed_at,
                        phase_elapsed_s=now - t5 if t5 is not None else 0.0,
                        progress=percent,
                        line_number=line_number,
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
                if sweep_span_s is None:
                    # Nothing left to time (a rehydrated pending carries no build
                    # figures), so the whole-job deadline is the only rule that remains.
                    phase = "deadline_only"
                    continue
                sweep_budget_deadline = t50 + sweep_span_s + _sweep_margin_s(sweep_span_s)
                phase = "await_p75"
                continue
            if drop_deadline is not None and now > drop_deadline:
                await _stop_and_page(
                    printer_id,
                    current,
                    armed,
                    sleep=sleep,
                    stage="drop",
                    fired_deadline_s=drop_deadline - armed_at,
                    phase_elapsed_s=now - t5 if t5 is not None else 0.0,
                    progress=percent,
                    line_number=line_number,
                    drop_span_s=drop_span_s,
                    sweep_span_s=None,
                )
                return
            continue

        if phase == "await_p75":
            # The sweep is the hazard phase — a part that fails to release presents as a
            # sweep overrun — so it is never given patience: the whole-job deadline
            # selected above stays binding here, which is today's stuck-part timing
            # unchanged, and this lane only MEASURES (no calibrated floor exists yet:
            # the P50→P75 span had never been observed before this wave). That backstop
            # also bounds the fabricated-late-t50 shape: a link blind from P5 that comes
            # back at P50 anchors t50 late and would otherwise hand a nearly-finished
            # sweep a whole fresh budget.
            if not fresh:
                continue
            if percent >= PHASE_BEACON_PARK_PCT:
                overran = sweep_budget_deadline is not None and now > sweep_budget_deadline
                # Timed from t50, so it UNDERSTATES the true span exactly as the drop
                # measurement does. This line IS the P50→P75 distribution a calibrated
                # sweep kill will be armed from.
                #
                # A late arrival is RECORDED, never killed — deliberately asymmetric with
                # `drop_late`, which stops a job whose sweep is about to begin and so
                # still pre-empts plate contact. By the park beacon the contact has
                # happened; stopping the job here would cost the plate a verified eject
                # and buy nothing back.
                logger.log(
                    logging.WARNING if overran else logging.INFO,
                    "eject.remote: printer %s sweep phase cleared in <=%.1fs (budget %.0fs)%s — epilogue",
                    printer_id,
                    now - t50 if t50 is not None else 0.0,
                    sweep_span_s,
                    " but OVERRAN it" if overran else "",
                )
                # A fresh sample at/above the park beacon is the ONLY thing that buys
                # patience, and it can be read as THIS job's progress because
                # ``bambu_mqtt._stale_predecessor_reading`` discards a republished
                # predecessor percent — withholding ``progress_wire_at`` with it — until
                # the job publishes a reading strictly below its predecessor's final. A
                # finished print's 100% therefore cannot present here as a park beacon.
                #
                # Anchoring at ``t75 = now`` REVERSES this module's understating-span
                # bias into a patience-maximising one: the true edge fell earlier, so the
                # epilogue window starts later than it should. Taken deliberately —
                # every other span biases against a false kill because the plate is at
                # risk, and past the park beacon the only thing exposed is downtime.
                t75 = now
                phase = "epilogue"
                continue
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

        # epilogue: the sweep is over, so nothing here can protect the plate any more.
        # The only question left is whether the job ever finishes, and the epilogue
        # deadline selected above is what answers it.
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
      of the pending. This closes the post-restart hole (W1/R2): after a restart the
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
        expected_stem: str | None = None
        if pending.purpose == "manual":
            expected_stem = f"eject_manual_p{printer_id}"
        elif pending.queue_item_id is not None:
            expected_stem = expected_eject_stem(pending)
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
    ``dispatch_in_flight``, ``eject_in_flight``, ``not_occupied``), so the operator
    is told which of them held — one refusal vocabulary, from the state machine to
    the dialog. Everything else keeps the generic default.
    """

    def __init__(self, message: str, *, status_code: int = 409, code: str = "eject_dispatch_failed") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


# The refusal → operator sentence map for the eject lane. The authority speaks only
# in tokens (no English in the core — the 2026-08-20 ``slot_recheck`` precedent), so
# the copy lives here, at the boundary that raises. WS4 folds this into the route's
# one verdict→copy map; until then the message rides the error and the token rides
# ``EjectDispatchError.code``, so the wire contract is already the final one.
_EJECT_REFUSAL_MESSAGES: dict[str, str] = {
    "job_active": "Printer is running a job; wait for it to finish or stop it, then eject",
    "dispatch_in_flight": "A queued unit is being sent to this printer; retry in a few seconds",
    "eject_in_flight": "An eject is already in flight on this printer",
    "not_occupied": "Printer is not awaiting plate clear; nothing to eject",
}


def _refusal_error(refusal: TransitionRefusal) -> EjectDispatchError:
    """The 409 for an occupancy refusal, carrying the refusal token as its ``code``."""
    return EjectDispatchError(
        _EJECT_REFUSAL_MESSAGES.get(refusal, f"Eject refused ({refusal})"),
        status_code=409,
        code=refusal,
    )


def _live_evidence(printer_id: int) -> Evidence:
    """The wire snapshot the eject lane checks against.

    ``db_claim`` is deliberately absent: a ``printing`` queue row on an IDLE printer
    is the 2026-08-29 dead-claim class, released only after 600 s of age plus a
    120 s dwell, and refusing an eject on it would lock the operator out of a
    provably idle plate for ≥12 minutes — strictly worse than the behaviour this
    replaces, where the eject lane never read the queue row at all. The dispatch
    LEASE is the seconds-long window an eject can physically collide with, and the
    authority derives that from its own record.

    The eject lane is also ungated by live HMS, deliberately (2026-08-29 W4): an
    eject is filament-less, and holding the plate behind an AMS fault would deadlock
    the very plate that holds the printer.
    """
    state = printer_manager.get_status(printer_id)
    return Evidence(live_state=getattr(state, "state", None) if state is not None else None)


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
    CLAIMS the printer for the sweep. Does NOT touch the plate-clear gate — that
    clears only when the eject job's terminal arrives.

    The occupancy check runs BEFORE the build, because building and uploading an
    eject costs seconds of FTPS work that a refused sweep must not spend; the claim
    afterwards re-runs the identical gate, so a race that opened during the upload is
    still caught.

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
        sweep_span_s=built.sweep_span_s,
        tail_s=built.tail_s,
    )
    # The eject file's FTPS upload transiently drops the H2S sdcard flag; mark the
    # printer upload-in-flight so the USB-drop verifier ignores that dispatch blip.
    async with upload_in_flight(printer.id):
        await _upload_start_claim_eject(
            printer=printer,
            eject_path=built.path,
            job_stem=f"eject_{purpose}_item{queue_item_id}",
            plate_id=plate_id,
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
        drop_span_s=built.drop_span_s,
        sweep_span_s=built.sweep_span_s,
        tail_s=built.tail_s,
    )
    # Same as the production path: the FTPS upload transiently drops the H2S sdcard
    # flag; mark the printer upload-in-flight so the USB-drop verifier ignores the blip.
    async with upload_in_flight(printer.id):
        await _upload_start_claim_eject(
            printer=printer,
            eject_path=built.path,
            job_stem=f"eject_manual_p{printer_id}",
            plate_id=plate_id,
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
    for the sweep and arm its start deadline. The built ``eject_path`` is always
    cleaned up; nothing is claimed unless ``start_print`` was accepted. Raises
    :class:`EjectDispatchError` (502) on upload / start failure.

    The ``start_print`` file, the MQTT ``project_file`` param (plate path, keyed by
    ``plate_id``) and the eventual SD cleanup all key off the SAME ``remote_filename``
    (the bare ``job_stem``).

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

    # Claim the printer. The gate this passed before the build is re-run here, so a
    # dispatch lease or another eject that appeared during the upload still refuses —
    # and then the sweep we just started must be stopped, because the file IS on the
    # printer and the firmware may act on it.
    refusal = plate_occupancy.claim_for_eject(printer.id, pending, ev)
    if refusal is not None:
        logger.warning(
            "eject.remote: printer %s claim refused (%s) AFTER the eject start was accepted — "
            "stopping the sweep; the printer was taken by something else during the upload",
            printer.id,
            refusal,
        )
        printer_manager.stop_print(printer.id)
        eject_progress.emit_eject_progress(printer_id=printer.id, queue_item_id=pending.queue_item_id, phase="failed")
        raise _refusal_error(refusal)

    # The firmware silently ignores a project_file sent while it is busy, so a claim
    # that never sees a PRINT START echo is a sweep that will never happen. Arm the
    # deadline that says so (cancelled by the start echo, superseded by a new claim).
    _arm_start_deadline(printer.id)


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
    open by a lazy import. Since the WS4 donor extraction the resolution it needs is
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
