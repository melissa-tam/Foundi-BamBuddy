"""Automatic mid-print spool-jam recovery (fork farm feature).

An AMS feed fault (a tangled roll / stuck spool overloading the assist motor)
PAUSEs a running print with no firmware self-recovery — every tangle is a silent
multi-hour stall that breaks the lights-out promise (production incident
2026-07-16 lost ~6 h of printer capacity). Bambu's "AMS filament backup" only
auto-switches on RUNOUT; the ``07xx_8010`` tangle family always pauses and sits.

This module is the single owner of the recovery state machine. On a recoverable
HMS (feed fault, or a runout the firmware backup failed to rescue) during a FARM
print it reproduces the operator's proven manual recovery sequence:

    (printer already PAUSEd) → unload → confirm ``tray_now == 255`` → mark the
    jammed spool out of rotation → select the next eligible loaded spool → load
    it → confirm ``tray_now == target`` (the first load may not take — resend) →
    resume → confirm RUNNING and hold stable (a lingering fault may need one
    extra pause/resume cycle) → SUCCESS. If nothing works: escalate — notify and
    leave the printer PAUSED for a human, never resume blind.

Trigger scope is ``RECOVERABLE_HMS_CODES`` (:data:`FEED_FAULT_HMS_CODES` —
AMS-side + extruder-side feed faults — plus the reused-tag
:data:`RUNOUT_HMS_CODES`). An EXTRUDER-side fault (the main extruder overloaded,
not the AMS assist motor) still swaps, but a re-jam after the swap keeps the
replacement IN rotation because the extruder is the common factor
(``extruder_side_only``). A runout-triggered incident SKIPS the out-of-rotation
marking (that spool is SPENT — ``spool_respool.mark_spent_on_runout`` already
stamps its ledger) and closes silently as transient if the firmware backup
rescued the print (it never PAUSEs). Replacement selection reuses the same
``spool_selection`` functions the dispatcher uses (out-of-rotation exclusion is
already baked into them); nothing here duplicates that policy.

Two latches bound the loop hazards a widened trigger set arms: after an escalate
or an abort we latch ``(printer, job)`` so a sibling code from the SAME physical
fault cannot restart recovery behind the operator's back; and a per-job success
cap bounds the jam→recover→jam ping-pong a dying extruder could otherwise sustain
all day. A SUCCESSFUL recovery re-arms the dedup key so a genuine second tangle
in the same job is still handled.

Entry: :func:`on_feed_fault_hms` (spawned from ``main.on_printer_status_change``,
never raises). Persistence clear: :func:`clear_on_reinsert` (from the
``ams_presence`` presence-GAIN edge). ``clear_hms_errors()`` is NEVER called — the
resume clears the firmware dialog itself and clearing would corrupt main.py's HMS
dedup/grace bookkeeping.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.models.print_batch import PrintBatch
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services.hms_errors import hms_short_code
from backend.app.services.printer_manager import printer_manager
from backend.app.services.spool_respool import RUNOUT_HMS_CODES, _decode_global_tray

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.app.services.bambu_mqtt import PrinterState

logger = logging.getLogger(__name__)

# --- Trigger code sets ------------------------------------------------------
# Feed-fault HMS short codes ("MMMM_CCCC", uppercase hex — matches hms_short_code
# output), split by WHERE the fault sits so recovery can reason about the common
# factor on a re-jam (see extruder_side_only below).
#
# AMS-side (assist-motor / feed-path) faults — a fresh spool on a healthy feed
# path usually clears them: the 07xx family is "AMS assist motor overloaded /
# entangled filament / stuck spool"; the 18xx family is the AMS-HT equivalent;
# the 12xx family is "Filament or spool may be stuck" (hms_errors.py). All carry
# stuck-spool semantics and all PAUSE the print, so a false positive cannot fire
# on a healthy print (acting requires a PAUSE anyway). The pull-out/pull-back
# siblings (07xx_8003/8004/8006) stay OUT — those can need physical extruder work
# an auto-load could grind.
AMS_FEED_FAULT_HMS_CODES: frozenset[str] = frozenset(
    {
        "0700_8010",
        "0701_8010",
        "0702_8010",
        "0703_8010",
        "0704_8010",
        "0705_8010",
        "0706_8010",
        "0707_8010",
        "1800_8010",
        "1801_8010",
        "1802_8010",
        "1200_8010",
        "1201_8010",
        "1202_8010",
        "1203_8010",
        "12FF_8010",
    }
)

# Extruder-side (main extruder motor overloaded) — the H2S code a tangle/stuck
# spool emits when it overloads the MAIN extruder instead of the AMS assist motor
# (production incident 2026-07-17: 004-H2S sat PAUSEd ~2h40m with no reaction
# because this code was outside the trigger set). A swap still helps (fresh
# filament often clears the immediate overload), but the extruder — not the
# spool — is the common factor, so a re-jam after the swap must NOT penalize the
# healthy replacement (extruder_side_only). Clog-leaning siblings
# (0300_801A/801C/8016, 0300_4006) stay OUT — a swap cannot fix a clog; the
# pause-stall watchdog escalates those instead.
EXTRUDER_FEED_FAULT_HMS_CODES: frozenset[str] = frozenset({"0300_801E"})

# Public union — kept as the single name the rest of the module (and main.py's
# import) reads, so RECOVERABLE_HMS_CODES / _primary_code / is_feed_fault are
# unchanged. Widening either family later is a one-line frozenset edit.
FEED_FAULT_HMS_CODES: frozenset[str] = AMS_FEED_FAULT_HMS_CODES | EXTRUDER_FEED_FAULT_HMS_CODES

# Codes that trigger a recovery attempt: feed faults PLUS the reused-tag runout
# family (import — do NOT duplicate the runout set). A runout that the firmware
# backup already rescued never PAUSEs, so the state machine closes it as
# transient; a stuck runout runs recovery WITHOUT the out-of-rotation marking.
RECOVERABLE_HMS_CODES: frozenset[str] = FEED_FAULT_HMS_CODES | RUNOUT_HMS_CODES

# --- waiting_reason tokens (rendered by the queue UI, mapped in waitingReason.ts)
WAITING_REASON_RECOVERING = "spool_jam_recovering"
WAITING_REASON_FAILED = "spool_jam_recovery_failed"

# --- Safety bounds (code constants, NOT operator knobs — precedent
#     ams_presence._RFID_REREAD_SPACING_S). The unload/load/resume confirm
#     timeout and per-step resend count ARE operator settings. -----------------
_POLL_INTERVAL_S = 1.0  # live-state poll spacing during every confirm wait
_POST_RESUME_STABLE_S = 60  # RUNNING must hold this long after a resume = success
_REPAUSE_WATCH_S = 120  # ceiling on how long we wait for RUNNING after a resume
_MAX_CANDIDATES = 3  # distinct replacement trays tried before escalating

# tray_now sentinel: no filament fed (unloaded). 255 on H2-series.
_NO_FILAMENT = 255

# --- Settings defaults (mirror schemas/settings.py) -------------------------
_DEFAULT_ENABLED = True
_DEFAULT_MAX_ATTEMPTS = 2
_DEFAULT_STEP_TIMEOUT_S = 90
_DEFAULT_PROTECT_LAYERS = 7

# Human-facing escalation reasons for the failed notification.
_ESCALATE_DETAIL: dict[str, str] = {
    "multi_feeder_job": (
        "Multi-filament job — a mid-print tray swap is unsound (the firmware re-loads the "
        "originally mapped slot at the next filament change). Left PAUSED for a human."
    ),
    "jammed_tray_unresolved": "Could not identify which spool jammed. Left PAUSED for a human.",
    "no_eligible_spool": "No other loaded spool matched the jammed filament. Left PAUSED for a human.",
    "only_low_spools_in_protected_layers": (
        "The only matching spool is below the minimum-start weight this early in the print. Left PAUSED for a human."
    ),
    "candidates_exhausted": "Tried every eligible replacement spool without a stable resume. Left PAUSED for a human.",
    "unload_failed": "The jammed spool would not unload. Left PAUSED for a human.",
    "repeated_jams": (
        "Auto-recovered several times this job but the fault keeps returning — likely an "
        "extruder-side problem, not the spool. Left PAUSED for a human."
    ),
}


@dataclass(frozen=True)
class RecoverySettings:
    """The four operator-tunable knobs, read once per incident."""

    enabled: bool
    max_attempts: int
    step_timeout_s: float
    protect_layers: int


@dataclass(frozen=True)
class RecoveryIncident:
    """Immutable context for one recovery attempt, resolved at the entry gate."""

    printer_id: int
    job_id: str
    codes: frozenset[str]
    item_id: int
    settings: RecoverySettings
    jammed_global_tray: int | None
    is_feed_fault: bool
    # True when EVERY recoverable code is extruder-side (main extruder overloaded).
    # A re-jam then keeps the replacement in rotation — the extruder, not the
    # spool, is the common factor.
    extruder_side_only: bool
    layer_at_fault: int
    code: str
    printer_name: str
    job_name: str


# --- Module edge state (matches the fork's other event-edge bookkeeping,
#     e.g. ams_presence._last_presence). ALL of it is process-lifetime — lost on
#     restart. The persisted feed_fault_at survives, and the HMS re-fires
#     post-restart so recovery is re-attempted once per process lifetime (the
#     latch/success-cap counters below reset too, which is safe: post-restart the
#     bounded single re-attempt runs again). ------------------------------------

# One active incident per printer — a second HMS while recovering is ignored.
_active_tasks: dict[int, asyncio.Task] = {}

# Dedup: (printer_id, job_id, frozenset(codes)) already handled this lifetime.
# A SUCCESSFUL recovery (and a transient close) discards its key so a genuine
# second tangle in the same job re-arms; escalate/abort keep their keys.
_handled: set[tuple[int, str, frozenset[str]]] = set()

# Escalation/abort latch: (printer_id, job_id) we've already given up on (or that
# an external actor took over). A sibling code from the SAME physical fault must
# not restart recovery behind the operator's back — the pause-stall watchdog
# still escalates if the print keeps sitting, so coverage is not lost.
_escalated: set[tuple[int, str]] = set()

# Per-job success counter: (printer_id, job_id) -> successful recoveries so far.
# Bounds the jam→recover→jam ping-pong a dying extruder could sustain all day.
_success_counts: dict[tuple[int, str], int] = {}

# Flap bound: after this many successful recoveries in ONE job, the next fault
# escalates instead of swapping again (code constant, precedent _MAX_CANDIDATES).
_MAX_SUCCESSES_PER_JOB = 3


def _reset_state() -> None:
    """Test hook: clear module-level edge/dedup state between cases."""
    _active_tasks.clear()
    _handled.clear()
    _escalated.clear()
    _success_counts.clear()


def has_live_recovery(printer_id: int) -> bool:
    """True when a recovery task is actively running for ``printer_id``.

    Public liveness signal over the module-level ``_active_tasks`` registry — the
    single source of truth for "is spool-recovery still handling this pause". A
    missing slot OR a task that has already finished (``.done()``) both read as no
    live recovery. The pause-stall watchdog uses this instead of the
    ``spool_jam_recovering`` token string: a token orphaned by a mid-recovery
    restart/crash has no live task, so it is no longer mistaken for "owned by
    another handler" and left to stall the printer forever (R1)."""
    task = _active_tasks.get(printer_id)
    return task is not None and not task.done()


def _log_gate_out(reason: str, printer_id: int, recoverable: frozenset[str]) -> None:
    """INFO trail for an entry-gate return-None while recoverable codes are live —
    makes 'why didn't recovery fire' answerable from the log."""
    logger.info(
        "spool_recovery: printer %s recoverable HMS %s NOT recovered — %s",
        printer_id,
        sorted(recoverable),
        reason,
    )


# --- small helpers ----------------------------------------------------------


def _now() -> float:
    return asyncio.get_running_loop().time()


def _get_state(printer_id: int) -> PrinterState | None:
    return printer_manager.get_status(printer_id)


def _primary_code(codes: frozenset[str]) -> str:
    """The representative short code for feed_fault_code / notifications — a
    feed-fault code when present, else the lowest recoverable code."""
    feed = sorted(codes & FEED_FAULT_HMS_CODES)
    if feed:
        return feed[0]
    ordered = sorted(codes)
    return ordered[0] if ordered else ""


def _active_recoverable_codes(state) -> set[str]:
    """Recoverable HMS short codes currently live on the printer state."""
    out: set[str] = set()
    for e in getattr(state, "hms_errors", None) or []:
        try:
            out.add(hms_short_code(e.attr, e.code))
        except Exception:  # noqa: BLE001 — a malformed HMS entry must not crash recovery
            continue
    return out & RECOVERABLE_HMS_CODES


def _spool_label(spool: Spool) -> str:
    """Short human description for notifications ("Polymaker PETG Jade")."""
    bits = [spool.brand, spool.material, spool.color_name]
    label = " ".join(b for b in bits if b)
    return label or f"spool #{spool.id}"


def _rewrite_mapping(raw: str | None, jammed: int | None, target: int) -> str | None:
    """Rewrite the item's ams_mapping so the jammed global tray id becomes the
    replacement — keeps a later runout resolution honest. Untouched on parse
    failure or a null jammed id."""
    if not raw or jammed is None:
        return raw
    try:
        mapping = json.loads(raw)
    except (ValueError, TypeError):
        return raw
    if not isinstance(mapping, list):
        return raw
    rewritten = [target if (isinstance(v, (int, float)) and int(v) == jammed) else v for v in mapping]
    return json.dumps(rewritten)


# --- settings ---------------------------------------------------------------


async def _read_bool(db: AsyncSession, key: str, default: bool) -> bool:
    from backend.app.api.routes.settings import get_setting

    raw = await get_setting(db, key)
    if raw is None:
        return default
    return raw.strip().lower() == "true"


async def _read_int(db: AsyncSession, key: str, default: int) -> int:
    from backend.app.api.routes.settings import get_setting

    raw = await get_setting(db, key)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


async def _read_settings(db: AsyncSession) -> RecoverySettings:
    return RecoverySettings(
        enabled=await _read_bool(db, "spool_recovery_enabled", _DEFAULT_ENABLED),
        max_attempts=await _read_int(db, "spool_recovery_max_attempts", _DEFAULT_MAX_ATTEMPTS),
        step_timeout_s=float(await _read_int(db, "spool_recovery_step_timeout_s", _DEFAULT_STEP_TIMEOUT_S)),
        protect_layers=await _read_int(db, "spool_recovery_protect_layers", _DEFAULT_PROTECT_LAYERS),
    )


# --- resolution -------------------------------------------------------------


async def _resolve_farm_item(db: AsyncSession, printer_id: int, job_id: str) -> PrintQueueItem | None:
    """The printing FARM queue item whose dispatch_subtask_id matches the live
    subtask id (farm-dispatched only — a foreign/local print never matches).
    Mirrors farm_correlation's id-equality + farm-batch predicate."""
    if not job_id:
        return None
    result = await db.execute(
        select(PrintQueueItem)
        .join(PrintBatch, PrintQueueItem.batch_id == PrintBatch.id)
        .where(PrintQueueItem.printer_id == printer_id)
        .where(PrintQueueItem.status == "printing")
        .where(PrintQueueItem.dispatch_subtask_id == job_id)
        .where(PrintBatch.sku_file_id.is_not(None))
        .order_by(PrintQueueItem.started_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def _resolve_jammed_tray(item: PrintQueueItem, state) -> tuple[int | None, str]:
    """Which global tray jammed, and the feeder verdict.

    ``single`` — a deterministic single-feeder farm job (mapping feeder, else the
    live ``tray_now``). ``multi_feeder`` — >1 mapped feeder: a tray swap is
    unsound (firmware re-loads the original slot), so the caller escalates.
    ``none`` — nothing resolvable. Adapts ``spool_respool._resolve_exhausted_tray``.
    """
    feeders: list[int] = []
    if item.ams_mapping:
        try:
            mapping = json.loads(item.ams_mapping)
            feeders = [int(v) for v in mapping if isinstance(v, (int, float)) and int(v) >= 0]
        except (ValueError, TypeError):
            feeders = []
    tray_now = getattr(state, "tray_now", None)
    live_ok = tray_now is not None and 0 <= tray_now <= 253
    if len(feeders) == 1:
        # Prefer the live feeding tray over the (possibly stale) single-feeder
        # mapping; the mapping is the fallback when tray_now is unloaded/unknown.
        return (tray_now if live_ok else feeders[0]), "single"
    if len(feeders) > 1:
        return None, "multi_feeder"
    if live_ok:
        return tray_now, "single"
    return None, "none"


# --- entry ------------------------------------------------------------------


async def on_feed_fault_hms(printer_id: int, new_short_codes, state) -> asyncio.Task | None:
    """Gate + spawn the recovery driver for a NEW recoverable HMS. Never raises.

    Gates in order: not already escalated/aborted (latch) → enabled setting →
    dedup (printer, job, codes) → no active incident on this printer → a farm item
    is printing here → single-feeder job → under the per-job success cap. A
    multi-feeder job, an unresolvable jammed tray, or a job over the success cap
    escalates immediately. Every return-None gate with recoverable codes live logs
    INFO so post-hoc triage of "why didn't recovery fire" is possible. Returns the
    spawned ``asyncio.Task`` (so tests can await it) or ``None`` when gated out; the
    ``main`` caller ignores the return.
    """
    try:
        recoverable = frozenset(new_short_codes) & RECOVERABLE_HMS_CODES
        if not recoverable:
            return None

        job_id = (getattr(state, "subtask_id", None) or "").strip()
        dedup_key = (printer_id, job_id, recoverable)

        # Latch gate (before opening a session): once we've escalated or aborted
        # THIS (printer, job), a sibling code from the same fault is a no-op.
        if (printer_id, job_id) in _escalated:
            _log_gate_out("escalation/abort latch (already given up on this job)", printer_id, recoverable)
            return None

        from backend.app.core.database import async_session
        from backend.app.models.printer import Printer

        incident: RecoveryIncident | None = None
        escalate_reason: str | None = None
        async with async_session() as db:
            settings = await _read_settings(db)
            if not settings.enabled:
                _log_gate_out("recovery disabled by setting", printer_id, recoverable)
                return None
            if dedup_key in _handled:
                _log_gate_out("dedup — incident already handled this job", printer_id, recoverable)
                return None
            existing = _active_tasks.get(printer_id)
            if existing is not None and not existing.done():
                _log_gate_out("an active recovery is already running on this printer", printer_id, recoverable)
                return None
            item = await _resolve_farm_item(db, printer_id, job_id)
            if item is None:
                _log_gate_out("no farm unit is printing here (foreign / non-farm job)", printer_id, recoverable)
                return None
            # We own this incident now — dedup so a repeated HMS push is a no-op.
            _handled.add(dedup_key)

            jammed, verdict = _resolve_jammed_tray(item, state)
            printer = await db.get(Printer, printer_id)
            printer_name = (printer.name if printer else None) or f"printer {printer_id}"
            job_name = (getattr(state, "subtask_name", None) or "").strip() or "print"
            feed = recoverable & FEED_FAULT_HMS_CODES
            incident = RecoveryIncident(
                printer_id=printer_id,
                job_id=job_id,
                codes=recoverable,
                item_id=item.id,
                settings=settings,
                jammed_global_tray=jammed,
                is_feed_fault=bool(feed),
                extruder_side_only=bool(feed) and feed <= EXTRUDER_FEED_FAULT_HMS_CODES,
                layer_at_fault=int(getattr(state, "layer_num", 0) or 0),
                code=_primary_code(recoverable),
                printer_name=printer_name,
                job_name=job_name,
            )
            if verdict == "multi_feeder":
                escalate_reason = "multi_feeder_job"
            elif jammed is None:
                escalate_reason = "jammed_tray_unresolved"
            elif _success_counts.get((printer_id, job_id), 0) >= _MAX_SUCCESSES_PER_JOB:
                # Flap bound reached: recovery keeps landing but the fault keeps
                # returning — an extruder-side problem a swap won't fix.
                escalate_reason = "repeated_jams"

        # Session closed — escalate/spawn outside it (helpers open their own).
        if escalate_reason is not None:
            if escalate_reason == "repeated_jams":
                _log_gate_out("per-job success cap reached — escalating", printer_id, recoverable)
            await _escalate(incident, escalate_reason)
            return None

        task = asyncio.create_task(_run_recovery(incident))
        _active_tasks[printer_id] = task
        return task
    except Exception:  # noqa: BLE001 — entry hook must never crash the status flow
        logger.exception("spool_recovery: on_feed_fault_hms failed for printer %s", printer_id)
        return None


# --- driver -----------------------------------------------------------------


async def _run_recovery(incident: RecoveryIncident) -> None:
    """The recovery state machine (numbered per the plan). Never raises; always
    clears its active-task slot on exit."""
    pid = incident.printer_id
    try:
        client = printer_manager.get_client(pid)
        if client is None:
            logger.info("spool_recovery: printer %s has no client — incident closed", pid)
            return

        # (1) The feed fault PAUSEs the print; a runout the firmware backup
        #     rescued never PAUSEs → close silently as transient. Re-arm dedup so
        #     a later genuine fault in the same job is still handled.
        if not await _await_state(pid, {"PAUSE"}, incident.settings.step_timeout_s):
            _handled.discard((incident.printer_id, incident.job_id, incident.codes))
            logger.info("spool_recovery: printer %s never PAUSEd (firmware rescue / transient) — incident closed", pid)
            return

        # (2) Show the operator a live status; take the jammed spool out of
        #     rotation (feed-fault incidents only — a runout spool is SPENT).
        await _stamp_recovering(incident)
        if incident.is_feed_fault and incident.jammed_global_tray is not None:
            await _mark_out_of_rotation(incident, incident.jammed_global_tray, notify=True)

        # (3) Try up to _MAX_CANDIDATES replacement trays.
        tried: set[int] = set()
        for _round in range(_MAX_CANDIDATES):
            unload = await _unload_and_confirm(incident, client)
            if unload == "abort":
                await _abort(incident)
                return
            if unload == "fail":
                await _escalate(incident, "unload_failed")
                return

            target, only_low = await _select_replacement(incident, tried)
            if target is None:
                # An external RESUME during the (possibly bounded) selection /
                # forced bare-tray sweep means someone took over — abort rather
                # than escalate, mirroring the other steps' abort semantics.
                st = _get_state(pid)
                if st is not None and getattr(st, "state", None) == "RUNNING":
                    await _abort(incident)
                    return
                await _escalate(incident, "only_low_spools_in_protected_layers" if only_low else "no_eligible_spool")
                return
            tried.add(target)

            load = await _load_and_confirm(incident, client, target)
            if load == "abort":
                await _abort(incident)
                return
            if load == "fail":
                continue  # load never confirmed — try the next candidate

            resume = await _resume_and_confirm(incident, client, target)
            if resume == "abort":
                await _abort(incident)
                return
            if resume == "success":
                await _succeed(incident, target)
                return

            # resume == "repause": one extra pause/resume cycle (mirrors the live
            # 16:21:24 → 16:22:57 recovery where the first resume didn't stick).
            if client.pause_print():
                await _await_state(pid, {"PAUSE"}, incident.settings.step_timeout_s)
            else:
                # Offline / rejected send: PAUSE will never arrive — skip the
                # confirm wait rather than burn a full step_timeout on a no-op.
                logger.warning(
                    "spool_recovery: printer %s pause_print send returned False (offline?) — skipping PAUSE confirm wait",
                    pid,
                )
            resume2 = await _resume_and_confirm(incident, client, target)
            if resume2 == "abort":
                await _abort(incident)
                return
            if resume2 == "success":
                await _succeed(incident, target)
                return

            # Still stuck → this replacement re-jammed too. Only take it out of
            # rotation when the fault is AMS-side; on an extruder-side fault the
            # extruder is the common factor, so the replacement is probably
            # healthy — keep it in rotation (``tried`` already bars re-selecting it
            # this job). Then try the next candidate.
            if not incident.extruder_side_only:
                await _mark_out_of_rotation(incident, target, notify=True)
            else:
                logger.info(
                    "spool_recovery: printer %s replacement tray %s kept IN rotation — "
                    "extruder-side fault %s is the common factor, not the spool",
                    pid,
                    target,
                    incident.code,
                )

        # (4) Every candidate exhausted.
        await _escalate(incident, "candidates_exhausted")
    except Exception:  # noqa: BLE001 — the driver must never crash the event loop
        logger.exception("spool_recovery: recovery driver crashed for printer %s", pid)
    finally:
        _active_tasks.pop(pid, None)


async def _await_state(printer_id: int, targets: set[str], timeout_s: float) -> bool:
    """Poll live ``gcode_state`` until it is one of ``targets`` or timeout."""
    deadline = _now() + timeout_s
    while _now() < deadline:
        st = _get_state(printer_id)
        if st is not None and getattr(st, "state", None) in targets:
            return True
        await asyncio.sleep(_POLL_INTERVAL_S)
    st = _get_state(printer_id)
    return st is not None and getattr(st, "state", None) in targets


# --- step helpers -----------------------------------------------------------


async def _unload_and_confirm(incident: RecoveryIncident, client) -> str:
    """Unload until ``tray_now == 255``. ``ok`` / ``fail`` / ``abort``.

    Already-255 short-circuits (the restart path, or a firmware that unloaded on
    the fault) with no command sent. An external RESUME mid-unload aborts.
    """
    st = _get_state(incident.printer_id)
    if st is not None and getattr(st, "tray_now", None) == _NO_FILAMENT:
        return "ok"
    for _ in range(max(1, incident.settings.max_attempts)):
        if not client.ams_unload_filament():
            # Offline / rejected send: a no-op that never confirms — consume the
            # attempt and advance immediately instead of burning a full confirm wait.
            logger.warning(
                "spool_recovery: printer %s ams_unload_filament send returned False (offline?) — attempt consumed",
                incident.printer_id,
            )
            continue
        verdict = await _confirm_unloaded(incident)
        if verdict != "timeout":
            return verdict
    return "fail"


async def _confirm_unloaded(incident: RecoveryIncident) -> str:
    deadline = _now() + incident.settings.step_timeout_s
    while _now() < deadline:
        st = _get_state(incident.printer_id)
        if st is None:
            return "abort"
        if getattr(st, "state", None) == "RUNNING":
            return "abort"  # someone else resumed the print
        if getattr(st, "tray_now", None) == _NO_FILAMENT:
            return "ok"
        await asyncio.sleep(_POLL_INTERVAL_S)
    return "timeout"


async def _select_replacement(incident: RecoveryIncident, tried: set[int]) -> tuple[int | None, bool]:
    """Pick the next eligible loaded tray for the jammed filament, reusing the
    dispatcher's own selection functions. Returns ``(global_tray_id | None,
    only_low)`` — ``only_low`` True when the only match was withheld by the
    layer-conditional minimum-start floor. External / jammed / already-tried
    trays are excluded; out-of-rotation exclusion is inside the matcher.

    Two robustness paths added after the 18:45 runout incident (a full spool sat
    unusable in a BARE tray while recovery escalated ``no_eligible_spool`` in
    200 ms):

    * The requirement is resolved INDEPENDENTLY of the loaded-tray membership
      lookup (live jammed telemetry → jammed tray's DB spool → dispatched file),
      so a BARE jammed tray no longer ends recovery before any candidate scan.
    * When no configured tray matches, one forced bare-tray autoconfig sweep
      enrolls any present-but-bare tray, waits bounded for it to gain a
      ``tray_type`` in live telemetry, and re-scans once before escalating.
    """
    status = _get_state(incident.printer_id)
    if status is None:
        return None, False

    requirement = await _build_requirement(incident, status)
    if requirement is None:
        await _log_tray_snapshot(incident)
        return None, False

    pick, only_low = await _match_candidates(incident, status, requirement, tried)
    if pick is not None:
        return pick, only_low

    # No configured tray matched → force-config present-but-bare trays once
    # (bypassing only the retry window), wait bounded for one to gain a tray_type,
    # then re-scan a single time. Still nothing → escalate exactly as before.
    forced_slots = await _force_bare_tray_config(incident, status)
    if forced_slots:
        status2 = await _await_bare_tray_configured(incident, forced_slots)
        if status2 is not None:
            pick2, only_low2 = await _match_candidates(incident, status2, requirement, tried)
            if pick2 is not None:
                return pick2, only_low2
            only_low = only_low or only_low2

    await _log_tray_snapshot(incident)
    return None, only_low


def _requirement_from_loaded(jammed: dict) -> dict:
    """Build a matcher requirement from a live loaded-tray dict."""
    return {
        "slot_id": 1,
        "type": jammed.get("type"),
        "color": jammed.get("color"),
        "tray_info_idx": jammed.get("tray_info_idx"),
        "nozzle_id": jammed.get("extruder_id"),
    }


async def _build_requirement(incident: RecoveryIncident, status) -> dict | None:
    """Resolve the filament requirement for the jammed feeder, independent of
    whether the jammed tray is currently a configured (non-bare) tray.

    Source order: (1) live jammed-tray telemetry, (2) the jammed tray's DB
    ``SpoolAssignment`` → ``Spool`` (material / rgba), (3) the dispatched file's
    filament requirement. ``None`` only when nothing resolves.
    """
    from backend.app.services.print_scheduler import scheduler

    loaded_all = scheduler._build_loaded_filaments(status)
    jammed = next((f for f in loaded_all if f.get("global_tray_id") == incident.jammed_global_tray), None)
    if jammed is not None:
        return _requirement_from_loaded(jammed)

    req = await _requirement_from_assignment(incident)
    if req is not None:
        return req
    return await _requirement_from_file(incident)


async def _requirement_from_assignment(incident: RecoveryIncident) -> dict | None:
    """Requirement from the DB spool bound to the jammed global tray (material +
    rgba). ``None`` when the tray decodes to no AMS slot or has no bound spool."""
    if incident.jammed_global_tray is None:
        return None
    ams_id, tray_id = _decode_global_tray(incident.jammed_global_tray)
    if ams_id is None:
        return None
    from backend.app.core.database import async_session

    try:
        async with async_session() as db:
            res = await db.execute(
                select(SpoolAssignment)
                .options(selectinload(SpoolAssignment.spool))
                .where(
                    SpoolAssignment.printer_id == incident.printer_id,
                    SpoolAssignment.ams_id == ams_id,
                    SpoolAssignment.tray_id == tray_id,
                )
            )
            sa = res.scalar_one_or_none()
            if sa is not None and sa.spool is not None:
                sp = sa.spool
                return {
                    "slot_id": 1,
                    "type": sp.material,
                    "color": sp.rgba or "",
                    "tray_info_idx": sp.slicer_filament or "",
                    "nozzle_id": None,
                }
    except Exception:  # noqa: BLE001 — a requirement lookup must not crash recovery
        logger.exception("spool_recovery: requirement-from-assignment failed for printer %s", incident.printer_id)
    return None


async def _requirement_from_file(incident: RecoveryIncident) -> dict | None:
    """Requirement parsed from the dispatched 3MF (last resort). Uses the first
    filament requirement — single-feeder farm jobs carry exactly one."""
    from backend.app.core.database import async_session
    from backend.app.services.print_scheduler import scheduler

    try:
        async with async_session() as db:
            item = await db.get(PrintQueueItem, incident.item_id)
            if item is None:
                return None
            reqs = await scheduler._get_filament_requirements(db, item)
    except Exception:  # noqa: BLE001 — file parse must not crash recovery
        logger.exception("spool_recovery: requirement-from-file failed for printer %s", incident.printer_id)
        return None
    if not reqs:
        return None
    r = reqs[0]
    return {
        "slot_id": 1,
        "type": r.get("type"),
        "color": r.get("color", ""),
        "tray_info_idx": r.get("tray_info_idx", ""),
        "nozzle_id": None,
    }


async def _match_candidates(
    incident: RecoveryIncident, status, requirement: dict, tried: set[int]
) -> tuple[int | None, bool]:
    """Run the dispatcher's selection over the currently-configured trays for the
    given requirement. Returns ``(global_tray_id | None, only_low)``."""
    from backend.app.api.routes.settings import get_setting
    from backend.app.core.database import async_session
    from backend.app.services.print_scheduler import scheduler
    from backend.app.services.spool_selection import (
        _read_min_start_g,
        build_slot_inventory,
        effective_policy,
        match_filaments_to_slots,
    )

    loaded_all = scheduler._build_loaded_filaments(status)
    candidates = [
        f
        for f in loaded_all
        if not f.get("is_external")
        and f.get("global_tray_id") != incident.jammed_global_tray
        and f.get("global_tray_id") not in tried
    ]
    if not candidates:
        return None, False

    backup_on = getattr(status, "ams_filament_backup", None)
    async with async_session() as db:
        inv = await build_slot_inventory(db, incident.printer_id, candidates)
        base_min = await _read_min_start_g(db)
        policy_setting = await get_setting(db, "spool_selection_policy")

    # The layer rule is a floor PARAMETER, not new floor logic: below the
    # protected-layer threshold a low spool stays a backup donor; at/after it a
    # low spool is a valid mid-print replacement.
    min_start_g = 0 if incident.layer_at_fault >= incident.settings.protect_layers else base_min
    policy = effective_policy(policy_setting, backup_on)

    outcome = match_filaments_to_slots(
        [requirement], candidates, policy=policy, inv=inv, backup_on=backup_on, min_start_g=min_start_g
    )
    mapping = outcome.mapping
    if mapping and mapping[0] is not None and mapping[0] >= 0:
        return mapping[0], False
    return None, bool(outcome.start_blocked_slots)


def _iter_live_trays(status) -> list[tuple[int, dict]]:
    """``[(ams_id, tray_dict)]`` for every regular AMS tray in live telemetry."""
    out: list[tuple[int, dict]] = []
    raw = getattr(status, "raw_data", None)
    units = raw.get("ams") if isinstance(raw, dict) else None
    if not isinstance(units, list):
        return out
    for unit in units:
        if not isinstance(unit, dict):
            continue
        try:
            ams_id = int(unit.get("id", -1))
        except (TypeError, ValueError):
            continue
        if ams_id < 0:
            continue
        for tray in unit.get("tray", []) or []:
            if isinstance(tray, dict):
                out.append((ams_id, tray))
    return out


def _live_tray_dict(status, ams_id: int, tray_id: int) -> dict | None:
    """The live AMS tray dict for a specific ``(ams_id, tray_id)`` — for the
    tag-identity fallback of the out-of-rotation clear. ``None`` when absent."""
    for a_id, tray in _iter_live_trays(status):
        if a_id != ams_id:
            continue
        try:
            t_id = int(tray.get("id", -1))
        except (TypeError, ValueError):
            continue
        if t_id == tray_id:
            return tray
    return None


async def _force_bare_tray_config(incident: RecoveryIncident, status) -> list[tuple[int, int]]:
    """Force one bare-tray autoconfig sweep across this printer's present-but-bare
    trays (bypassing only the retry window). Returns the ``(ams_id, tray_id)`` of
    every slot a config push was attempted on."""
    from backend.app.core.database import async_session
    from backend.app.services import spool_tagless
    from backend.app.services.spool_tag_matcher import is_valid_tag

    forced: list[tuple[int, int]] = []
    async with async_session() as db:
        for ams_id, tray in _iter_live_trays(status):
            if (tray.get("tray_type") or "").strip():
                continue  # already configured — not bare
            if not spool_tagless.tray_present(tray):
                continue
            if is_valid_tag(tray.get("tag_uid", "") or "", tray.get("tray_uuid", "") or ""):
                continue  # RFID tray — not tagless
            try:
                tray_id = int(tray.get("id", -1))
            except (TypeError, ValueError):
                continue
            if tray_id < 0:
                continue
            try:
                did = await spool_tagless.maybe_autoconfigure_bare_tray(
                    db, incident.printer_id, ams_id, tray_id, tray, force=True
                )
            except Exception:  # noqa: BLE001 — a config push must not crash recovery
                logger.exception(
                    "spool_recovery: forced bare-tray config failed for printer %s AMS%d-T%d",
                    incident.printer_id,
                    ams_id,
                    tray_id,
                )
                did = False
            if did:
                forced.append((ams_id, tray_id))
    if forced:
        logger.info(
            "spool_recovery: printer %s forced bare-tray config on %s — awaiting firmware apply",
            incident.printer_id,
            forced,
        )
    return forced


def _any_slot_configured(status, slots: list[tuple[int, int]]) -> bool:
    """True when any of ``slots`` now reports a non-empty ``tray_type`` live."""
    wanted = set(slots)
    for ams_id, tray in _iter_live_trays(status):
        try:
            tray_id = int(tray.get("id", -1))
        except (TypeError, ValueError):
            continue
        if (ams_id, tray_id) in wanted and (tray.get("tray_type") or "").strip():
            return True
    return False


async def _await_bare_tray_configured(incident: RecoveryIncident, forced_slots: list[tuple[int, int]]):
    """Poll (≤ ``step_timeout_s``) for a forced bare slot to gain a ``tray_type``.
    Returns the live state on success, or ``None`` on timeout / lost state / an
    external RESUME (the driver then aborts rather than escalates)."""
    deadline = _now() + incident.settings.step_timeout_s
    while _now() < deadline:
        st = _get_state(incident.printer_id)
        if st is None:
            return None
        if getattr(st, "state", None) == "RUNNING":
            return None  # external actor resumed — driver aborts
        if _any_slot_configured(st, forced_slots):
            return st
        await asyncio.sleep(_POLL_INTERVAL_S)
    st = _get_state(incident.printer_id)
    if st is not None and _any_slot_configured(st, forced_slots):
        return st
    return None


async def _log_tray_snapshot(incident: RecoveryIncident) -> None:
    """One parseable INFO line: per-AMS-tray state/type/color/remain + the
    DB-assigned spool id. Emitted whenever recovery can't find a replacement or
    escalates, so 'why was nothing usable' is answerable from the log."""
    try:
        status = _get_state(incident.printer_id)
        if status is None:
            logger.info(
                "[spool_recovery] tray snapshot printer=%s jammed=%s <no live state>",
                incident.printer_id,
                incident.jammed_global_tray,
            )
            return
        from backend.app.core.database import async_session

        async with async_session() as db:
            res = await db.execute(select(SpoolAssignment).where(SpoolAssignment.printer_id == incident.printer_id))
            by_slot = {(a.ams_id, a.tray_id): a.spool_id for a in res.scalars().all()}
        rows: list[str] = []
        for ams_id, tray in _iter_live_trays(status):
            try:
                tray_id = int(tray.get("id", -1))
            except (TypeError, ValueError):
                continue
            global_tray = ams_id if ams_id >= 128 else ams_id * 4 + tray_id
            tt = (tray.get("tray_type") or "") or "-"
            col = tray.get("tray_color") or "-"
            rows.append(
                f"g{global_tray}(st={tray.get('state')},type={tt},col={col},"
                f"rem={tray.get('remain')},spool={by_slot.get((ams_id, tray_id))})"
            )
        logger.info(
            "[spool_recovery] tray snapshot printer=%s jammed=%s %s",
            incident.printer_id,
            incident.jammed_global_tray,
            " ".join(rows) if rows else "<no trays>",
        )
    except Exception:  # noqa: BLE001 — a diagnostic log must never crash recovery
        logger.exception("spool_recovery: tray snapshot failed for printer %s", incident.printer_id)


async def _load_and_confirm(incident: RecoveryIncident, client, target: int) -> str:
    """Load ``target`` until ``tray_now == target``. ``ok`` / ``fail`` / ``abort``.

    The live incident needed two sends before the load took, hence the resend
    loop. A ``pending_tray_target`` that becomes something other than our target
    means another actor issued a load → abort.
    """
    for _ in range(max(1, incident.settings.max_attempts)):
        if not client.ams_load_filament(target):
            # Offline / rejected send: a no-op that never confirms — consume the
            # attempt and advance immediately instead of burning a full confirm wait.
            logger.warning(
                "spool_recovery: printer %s ams_load_filament(%s) send returned False (offline?) — attempt consumed",
                incident.printer_id,
                target,
            )
            continue
        verdict = await _confirm_loaded(incident, target)
        if verdict != "timeout":
            return verdict
    return "fail"


async def _confirm_loaded(incident: RecoveryIncident, target: int) -> str:
    deadline = _now() + incident.settings.step_timeout_s
    while _now() < deadline:
        st = _get_state(incident.printer_id)
        if st is None:
            return "abort"
        ptt = getattr(st, "pending_tray_target", None)
        if ptt is not None and ptt != target:
            return "abort"  # operator/other actor hijacked the load
        if getattr(st, "tray_now", None) == target:
            return "ok"
        await asyncio.sleep(_POLL_INTERVAL_S)
    return "timeout"


async def _resume_and_confirm(incident: RecoveryIncident, client, target: int) -> str:
    """Resume and confirm RUNNING held stable. ``success`` / ``repause`` / ``abort``.

    A re-PAUSE while a recoverable code is still live ⇒ ``repause`` (the caller
    runs one extra pause/resume cycle). A re-PAUSE with no recoverable code, or a
    ``pending_tray_target`` hijack, ⇒ ``abort`` (an external actor is in control).
    """
    if not client.resume_print():
        # Offline / rejected send: RUNNING will never arrive — treat it as a
        # resume that did not take (``repause``) without burning the confirm wait.
        # The caller's extra pause/resume cycle then next-candidate path is the
        # existing fail route; no new escalation reason is introduced.
        logger.warning(
            "spool_recovery: printer %s resume_print send returned False (offline?) — resume not taken",
            incident.printer_id,
        )
        return "repause"

    # Phase 1: reach RUNNING.
    reach_deadline = _now() + min(incident.settings.step_timeout_s, _REPAUSE_WATCH_S)
    reached = False
    while _now() < reach_deadline:
        st = _get_state(incident.printer_id)
        if st is None:
            return "abort"
        ptt = getattr(st, "pending_tray_target", None)
        if ptt is not None and ptt != target:
            return "abort"
        s = getattr(st, "state", None)
        if s == "RUNNING":
            reached = True
            break
        if s == "FINISH":
            return "success"  # completed during the resume window
        await asyncio.sleep(_POLL_INTERVAL_S)
    if not reached:
        return "repause"  # resume didn't take — give the extra cycle a chance

    # Phase 2: hold RUNNING stable.
    hold_deadline = _now() + _POST_RESUME_STABLE_S
    while _now() < hold_deadline:
        st = _get_state(incident.printer_id)
        if st is None:
            return "abort"
        ptt = getattr(st, "pending_tray_target", None)
        if ptt is not None and ptt != target:
            return "abort"
        s = getattr(st, "state", None)
        if s == "PAUSE":
            return "repause" if _active_recoverable_codes(st) else "abort"
        if s == "FINISH":
            return "success"
        await asyncio.sleep(_POLL_INTERVAL_S)
    return "success"


# --- DB-mutating terminal steps (each opens its own session) ----------------


async def _stamp_recovering(incident: RecoveryIncident) -> None:
    from backend.app.core.database import async_session

    try:
        async with async_session() as db:
            item = await db.get(PrintQueueItem, incident.item_id)
            if item is not None:
                item.waiting_reason = WAITING_REASON_RECOVERING
                await db.commit()
    except Exception:  # noqa: BLE001 — a status stamp must not crash recovery
        logger.exception("spool_recovery: stamp recovering failed for printer %s", incident.printer_id)


async def _mark_out_of_rotation(incident: RecoveryIncident, global_tray: int, *, notify: bool) -> None:
    """Stamp ``feed_fault_at``/``feed_fault_code`` on the spool bound to
    ``global_tray`` (unbound slot → proceed anyway), broadcast inventory_changed,
    and optionally fire the out-of-rotation notification."""
    from backend.app.core.database import async_session
    from backend.app.core.websocket import ws_manager
    from backend.app.models.printer import Printer
    from backend.app.services.notification_service import notification_service

    ams_id, tray_id = _decode_global_tray(global_tray)
    slot_desc = f"AMS{ams_id} slot {tray_id}" if ams_id is not None else f"tray {global_tray}"
    spool_desc = f"tray {global_tray}"
    try:
        async with async_session() as db:
            if ams_id is not None:
                res = await db.execute(
                    select(SpoolAssignment)
                    .options(selectinload(SpoolAssignment.spool))
                    .where(
                        SpoolAssignment.printer_id == incident.printer_id,
                        SpoolAssignment.ams_id == ams_id,
                        SpoolAssignment.tray_id == tray_id,
                    )
                )
                sa = res.scalar_one_or_none()
                if sa is not None and sa.spool is not None:
                    sa.spool.feed_fault_at = datetime.utcnow()
                    sa.spool.feed_fault_code = incident.code
                    spool_desc = _spool_label(sa.spool)
                    await db.commit()
                else:
                    logger.info(
                        "spool_recovery: no spool bound to %s on printer %s — OOR mark skipped, recovery proceeds",
                        slot_desc,
                        incident.printer_id,
                    )

            try:
                await ws_manager.broadcast({"type": "inventory_changed"})
            except Exception:  # noqa: BLE001 — a WS hiccup must not abort recovery
                logger.exception(
                    "spool_recovery: inventory_changed broadcast failed for printer %s", incident.printer_id
                )

            if notify:
                printer = await db.get(Printer, incident.printer_id)
                printer_name = (printer.name if printer else None) or f"printer {incident.printer_id}"
                try:
                    await notification_service.on_spool_out_of_rotation(
                        printer_id=incident.printer_id,
                        printer_name=printer_name,
                        spool_desc=spool_desc,
                        slot_desc=slot_desc,
                        code=incident.code,
                        db=db,
                    )
                except Exception:  # noqa: BLE001 — notification failure is non-fatal
                    logger.exception("spool_recovery: OOR notification failed for printer %s", incident.printer_id)
    except Exception:  # noqa: BLE001 — marking is best-effort; recovery continues
        logger.exception("spool_recovery: mark_out_of_rotation failed for printer %s", incident.printer_id)


async def _describe_slot(db: AsyncSession, printer_id: int, global_tray: int | None) -> str:
    """Human description of the spool bound to a slot (for notifications)."""
    if global_tray is None:
        return "unknown spool"
    ams_id, tray_id = _decode_global_tray(global_tray)
    if ams_id is None:
        return f"tray {global_tray}"
    res = await db.execute(
        select(SpoolAssignment)
        .options(selectinload(SpoolAssignment.spool))
        .where(
            SpoolAssignment.printer_id == printer_id,
            SpoolAssignment.ams_id == ams_id,
            SpoolAssignment.tray_id == tray_id,
        )
    )
    sa = res.scalar_one_or_none()
    if sa is not None and sa.spool is not None:
        return _spool_label(sa.spool)
    return f"AMS{ams_id} slot {tray_id}"


async def _succeed(incident: RecoveryIncident, target: int) -> None:
    """Recovery landed: clear waiting_reason, rewrite the item's ams_mapping
    (jammed → replacement), and fire the success notification.

    Re-arms dedup (a genuine second tangle in the same job must be handled) and
    counts the success toward the per-job flap cap.
    """
    from backend.app.core.database import async_session
    from backend.app.models.printer import Printer
    from backend.app.services.notification_service import notification_service

    _handled.discard((incident.printer_id, incident.job_id, incident.codes))
    _success_counts[(incident.printer_id, incident.job_id)] = (
        _success_counts.get((incident.printer_id, incident.job_id), 0) + 1
    )

    try:
        async with async_session() as db:
            from_desc = await _describe_slot(db, incident.printer_id, incident.jammed_global_tray)
            to_desc = await _describe_slot(db, incident.printer_id, target)
            item = await db.get(PrintQueueItem, incident.item_id)
            if item is not None:
                item.waiting_reason = None
                item.ams_mapping = _rewrite_mapping(item.ams_mapping, incident.jammed_global_tray, target)
                await db.commit()
            printer = await db.get(Printer, incident.printer_id)
            printer_name = (printer.name if printer else None) or f"printer {incident.printer_id}"
            try:
                await notification_service.on_spool_recovery_succeeded(
                    printer_id=incident.printer_id,
                    printer_name=printer_name,
                    job_name=incident.job_name,
                    layer=incident.layer_at_fault,
                    from_spool=from_desc,
                    to_spool=to_desc,
                    db=db,
                )
            except Exception:  # noqa: BLE001 — notification failure is non-fatal
                logger.exception("spool_recovery: success notification failed for printer %s", incident.printer_id)
        logger.info(
            "spool_recovery: printer %s RECOVERED at layer %s — swapped %s → %s and resumed",
            incident.printer_id,
            incident.layer_at_fault,
            incident.jammed_global_tray,
            target,
        )
    except Exception:  # noqa: BLE001 — never crash the driver
        logger.exception("spool_recovery: succeed handler failed for printer %s", incident.printer_id)


async def _escalate(incident: RecoveryIncident, reason: str) -> None:
    """Give up: stamp waiting_reason FAILED, notify, and leave the printer PAUSED.
    NEVER resumes — a human must intervene."""
    from backend.app.core.database import async_session
    from backend.app.models.printer import Printer
    from backend.app.services.notification_service import notification_service

    # Latch FIRST — even if the DB stamp/notify below fails, a sibling code from
    # the same fault must not restart recovery after we've given up.
    _escalated.add((incident.printer_id, incident.job_id))

    # Per-tray diagnostic snapshot on every escalation (the 18:45 forensics gap).
    await _log_tray_snapshot(incident)

    detail = _ESCALATE_DETAIL.get(reason, reason)
    try:
        async with async_session() as db:
            item = await db.get(PrintQueueItem, incident.item_id)
            if item is not None:
                item.waiting_reason = WAITING_REASON_FAILED
                await db.commit()
            printer = await db.get(Printer, incident.printer_id)
            printer_name = (printer.name if printer else None) or f"printer {incident.printer_id}"
            try:
                await notification_service.on_spool_recovery_failed(
                    printer_id=incident.printer_id,
                    printer_name=printer_name,
                    job_name=incident.job_name,
                    detail=detail,
                    db=db,
                    is_feed_fault=incident.is_feed_fault,
                )
            except Exception:  # noqa: BLE001 — notification failure is non-fatal
                logger.exception("spool_recovery: failed notification error for printer %s", incident.printer_id)
        logger.warning("spool_recovery: printer %s ESCALATED (%s) — left PAUSED", incident.printer_id, reason)
    except Exception:  # noqa: BLE001 — never crash the driver
        logger.exception("spool_recovery: escalate handler failed for printer %s", incident.printer_id)


async def _abort(incident: RecoveryIncident) -> None:
    """Silent abort — an external actor took over mid-recovery. Stop acting and
    drop our stale ``recovering`` flag (the print is being handled elsewhere).

    If that actor resumed ON the jammed feeder (live RUNNING with ``tray_now`` ==
    the jammed global tray), they declared that spool usable — clear its
    out-of-rotation flag the same way a physical re-insert would, so a self-cleared
    jam does not leave the spool excluded from all future dispatch. Any other live
    state keeps the flag (a physical reseat stays the canonical clear)."""
    from backend.app.core.database import async_session

    # Latch FIRST (before any await that could fail): an external actor owns this
    # (printer, job) now — a sibling code must not restart recovery under them.
    _escalated.add((incident.printer_id, incident.job_id))

    logger.info("spool_recovery: printer %s recovery aborted (external interference)", incident.printer_id)
    try:
        async with async_session() as db:
            item = await db.get(PrintQueueItem, incident.item_id)
            if item is not None and item.waiting_reason == WAITING_REASON_RECOVERING:
                item.waiting_reason = None
                await db.commit()
            await _clear_oor_if_resumed_on_jammed_feeder(db, incident)
    except Exception:  # noqa: BLE001 — cleanup is best-effort
        logger.exception("spool_recovery: abort cleanup failed for printer %s", incident.printer_id)


async def _clear_oor_if_resumed_on_jammed_feeder(db: AsyncSession, incident: RecoveryIncident) -> None:
    """R3: an operator who resumes ON the jammed feeder (live RUNNING with
    ``tray_now`` == the jammed global tray) has declared that spool usable — clear
    its out-of-rotation flag the same way a physical re-insert would. Any other live
    state (or a different feeding tray) keeps the flag; a physical reseat stays the
    canonical clear. Best-effort — ``_abort`` must never raise."""
    jammed = incident.jammed_global_tray
    if jammed is None:
        return
    st = _get_state(incident.printer_id)
    if st is None or getattr(st, "state", None) != "RUNNING":
        return
    if getattr(st, "tray_now", None) != jammed:
        return
    ams_id, tray_id = _decode_global_tray(jammed)
    if ams_id is None or tray_id is None:
        return
    tray = _live_tray_dict(st, ams_id, tray_id) or {}
    try:
        await _clear_out_of_rotation_for_slot(db, incident.printer_id, ams_id, tray_id, tray)
    except Exception:  # noqa: BLE001 — best-effort; abort must never raise
        logger.exception("spool_recovery: self-resume out-of-rotation clear failed for printer %s", incident.printer_id)


# --- out-of-rotation clear (from the ams_presence presence-GAIN edge) --------


async def clear_on_reinsert(db: AsyncSession, printer_id: int, ams_id: int, tray_id: int, tray: dict) -> None:
    """Clear a spool's out-of-rotation flag when it is physically re-inserted.

    Called from ``ams_presence`` on an observed absent→present edge (NOT the
    post-restart seed, NOT idle-gated). Delegates to the shared resolver+clear
    (:func:`_clear_out_of_rotation_for_slot`). A no-op when no out-of-rotation spool
    matches the slot.
    """
    await _clear_out_of_rotation_for_slot(db, printer_id, ams_id, tray_id, tray)


async def _clear_out_of_rotation_for_slot(
    db: AsyncSession, printer_id: int, ams_id: int, tray_id: int, tray: dict
) -> bool:
    """Resolve the out-of-rotation spool bound to a slot and clear its feed-fault
    flag. The single owner of the out-of-rotation clear — shared by
    :func:`clear_on_reinsert` (physical presence-GAIN edge) and :func:`_abort`
    (operator resumed ON the jammed feeder).

    Resolves assignment-first (the binding survives a removal), then by RFID tag
    identity from the live ``tray`` payload; NULLs both feed-fault columns, commits,
    and broadcasts inventory_changed. Returns True when a spool was cleared, False
    when nothing out-of-rotation matched the slot.
    """
    from backend.app.core.websocket import ws_manager
    from backend.app.services.spool_tag_matcher import is_valid_tag
    from backend.app.utils.tag_normalization import normalize_tag_uid, normalize_tray_uuid

    spool: Spool | None = None

    # (1) Assignment-bound (survives the removal) — the authoritative path.
    res = await db.execute(
        select(SpoolAssignment)
        .options(selectinload(SpoolAssignment.spool))
        .where(
            SpoolAssignment.printer_id == printer_id,
            SpoolAssignment.ams_id == ams_id,
            SpoolAssignment.tray_id == tray_id,
        )
    )
    sa = res.scalar_one_or_none()
    if sa is not None and sa.spool is not None and sa.spool.feed_fault_at is not None:
        spool = sa.spool

    # (2) Tag-identity fallback — a re-insert into a different slot / after an
    #     unbind still clears via the physical tag on the tray.
    if spool is None:
        tag_uid = tray.get("tag_uid", "") or ""
        tray_uuid = tray.get("tray_uuid", "") or ""
        if is_valid_tag(tag_uid, tray_uuid):
            norm_uid = normalize_tag_uid(tag_uid)
            norm_uuid = normalize_tray_uuid(tray_uuid)
            conds = []
            if norm_uid:
                conds.append(Spool.tag_uid == norm_uid)
            if norm_uuid:
                conds.append(Spool.tray_uuid == norm_uuid)
            if conds:
                from sqlalchemy import or_

                res2 = await db.execute(
                    select(Spool).where(Spool.feed_fault_at.is_not(None)).where(or_(*conds)).limit(1)
                )
                spool = res2.scalar_one_or_none()

    if spool is None:
        return False

    spool.feed_fault_at = None
    spool.feed_fault_code = None
    await db.commit()
    logger.info(
        "spool_recovery: cleared out-of-rotation on spool %d — printer %d AMS%d-T%d",
        spool.id,
        printer_id,
        ams_id,
        tray_id,
    )
    try:
        await ws_manager.broadcast({"type": "inventory_changed"})
    except Exception:  # noqa: BLE001 — a WS hiccup must not break the caller
        logger.exception("spool_recovery: inventory_changed broadcast failed for printer %d", printer_id)
    return True
