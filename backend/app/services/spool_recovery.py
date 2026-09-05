"""Automatic mid-print spool-jam recovery (fork farm feature).

An AMS feed fault (a tangled roll / stuck spool overloading the assist motor)
PAUSEs a running print with no firmware self-recovery — every tangle is a silent
multi-hour stall that breaks the lights-out promise (production incident
2026-07-16 lost ~6 h of printer capacity). Bambu's "AMS filament backup" only
auto-switches on RUNOUT; the ``07xx_8010`` tangle family always pauses and sits.

This module is the single owner of the recovery state machine. On a recoverable
HMS (feed fault, or a runout the firmware backup failed to rescue) — during ANY
print, however it was started — it reproduces the operator's proven manual recovery
sequence:

    (printer already PAUSEd) → [reset a wedged filament-change] → (SWAP COMMIT: take
    the jammed spool out of rotation) → unload → confirm the AMS finished the unload
    cycle (see :func:`_confirm_unloaded`) → select the next eligible loaded spool →
    load it → confirm ``tray_now == target`` (the first load may not take — resend) →
    resume → confirm RUNNING and hold stable (a lingering fault may need one extra
    pause/resume cycle) → SUCCESS. If nothing works: escalate — notify and leave
    the printer PAUSED for a human, never resume blind.

    Out-of-rotation stamping/notification is bound to the SWAP-COMMIT boundary (the
    step just before the first unload), NOT to entry: a no-swap firmware self-heal
    (the reset frees the change on the same spool) must never stamp or announce a
    spool the print keeps running on, and fires its own truthful self-heal
    notification instead (2026-07-20 incident — an entry-time stamp+announce, then a
    same-spool self-heal 90 s later, misled the operator).

    W1 (009-H2S 2026-07-20): after a feed fault the AMS can sit WEDGED mid
    filament-change (gcode_state PAUSE, ``ams_status_main == 1``) where it silently
    ignores every unload — recovery's two AND the operator's two were all no-ops for
    hours. The ONLY verb that freed it was a ``resume`` (the touchscreen CONTINUE for
    the standing 07008010). So every candidate round now runs
    :func:`_reset_stuck_change` FIRST: on any other ``ams_status_main`` it is a no-op;
    on a wedged filament-change it re-issues the firmware CONTINUE and reads the
    outcome — the firmware may self-heal outright (no swap), re-fault and re-pause on
    its own, or hang RUNNING in an incomplete change (the live case) which we re-pause
    before the swap round. A non-``1`` non-idle state is NOT a wedge: 006-H2S
    2026-07-21 faulted at ``ams_status_main == 3`` (assist) with the feeder still
    engaged and the unload was accepted immediately — that round skips the reset and
    runs the swap machine directly.

    W2: a printer whose recovery escalates repeatedly within a rolling window is
    quarantined off the durable ``recovery_escalation`` ledger — a recurring AMS jam
    is hardware (buffer / feeder), not a spool the swap machine can fix.

The unload is UNCONDITIONAL after a feed fault, even when ``tray_now`` already
reads 255. Production incident 009-H2S 2026-07-20: an earlier ``tray_now == 255``
short-circuit made the machine send ZERO unloads across four candidate loads while
the AMS sat stuck mid-filament-change (``ams_status_main == 1``); every load was
doomed and it escalated to a human. The operator then recovered the identical
state in 90 s with the commands the machine already had — an explicit unload (sent
at ``tray_now == 255``), a load, a resume. After a feed fault 255 means "nothing is
feeding", NOT "the path is clear": only an explicit unload resets the stuck change
state machine. The short-circuit now survives ONLY for the genuinely-clean restart
case it was written for (see :func:`_unload_skippable`).

SCOPE (WS2b, 2026-08-09). Entry is :func:`on_ams_fault`, and what it acts on comes
from the WS2a fault TAXONOMY (``hms_errors.classify_hms_entry`` over both wire
lanes), not from a hand-kept code list and not from the notification dedup:

* **mechanical_feed** → the swap machine below, on ANY print. The trigger set is
  now the WHOLE class (the 2026-08-09 operator-ratified widening): the send-out
  8005, feed-into-extruder 8006 and feed-to-extruder 8028 families joined the 8010
  / 801E ones the machine has always acted on. An EXTRUDER-side fault still swaps,
  but a re-jam keeps the replacement IN rotation — the extruder is the common
  factor (``extruder_side_only``).
* **runout** / **runout_external** → hold + same-slot refill guidance + refill
  auto-resume. NEVER the swap machine (doctrine invariant 9). A runout incident
  skips the out-of-rotation marking (that spool is SPENT —
  ``spool_respool.mark_spent_on_runout`` stamps its ledger) and closes as transient
  if the firmware backup rescued the print (it never PAUSEs).
* **physical_fault** → immediate escalation with a hold. A swap cannot fix a broken
  filament, a clogged extruder or a failed pull-back, so it never enters the loop.
  Before WS2b nothing consumed this class at all: those faults waited on the
  generic pause-stall watchdog.

ORIGIN-AGNOSTIC (2026-08-10 operator ruling). ONE machine serves every print. A
farm queue unit controls the queue-row PROJECTIONS (``waiting_reason``) and the
retry bookkeeping — never which machine runs, and never whether one runs at all.
The gate that used to route ``jam + no farm item`` straight to an escalation is
gone: it made auto-recovery unreachable for most real workload, and it is why
printer 4 rode a full mechanical cascade on 2026-08-06 (``0700_8005`` +
``0700_0012``, then ``0700_8006`` + ``0700_0018`` 107 s later) with ZERO recovery
while a screen-started print sat PAUSEd.

What replaced it is EVIDENCE, not origin: :func:`_resolve_jammed_tray` reads the
wire first (the fault's own slot attribution, then the live feeder), and only then
asks a mapping to corroborate and to answer the one question a single feeder cannot
— is this job multi-material, where a mid-print tray swap is unsound because the
firmware re-loads the originally mapped slot at the next filament change. A foreign
print HAS such a statement (the slicer's captured ``ams_mapping``), and when
nothing answers, the fault escalates on ``jammed_tray_unresolved`` /
``multi_feeder_job`` exactly as a farm print with the same ambiguity would.

STATE IS DURABLE. The lifecycle lives in ``printer_incident`` rows (see
``services/printer_incidents``), not in module dicts: one OPEN incident per printer
(a partial unique index, not a dict a restart empties), an already-handled test
keyed by ``(printer, job, fault fingerprint)``, and a jam flap cap counted from
resolved incidents. The pre-WS2b ``_escalated`` latch never expired inside a
process, so a LATER, different fault on the same job could never be recovered; and
because the whole entry gate required a matching FARM queue item, 12 foreign-print
runouts were spent-stamped while nothing alerted, held or resumed.

Replacement selection reuses the same ``spool_selection`` functions the dispatcher
uses (out-of-rotation exclusion is already baked into them); nothing here
duplicates that policy.

Entries (all spawned guarded from ``main``, none ever raises):
:func:`on_ams_fault` (per status push), :func:`note_demand_watch` (the per-push
wire sampler that drives refill auto-resume and closes a hold the moment the
printer runs again), :func:`on_observed_running`, :func:`on_job_terminal`,
:func:`sweep_open_incidents` (the scheduler-tick close for a hold whose FAULT
cleared — the one lifecycle path that did not exist before 2026-08-29),
:func:`rearm_incidents_on_startup`, and :func:`clear_on_reinsert` (from the
``ams_presence`` presence-GAIN edge). ``clear_hms_errors()`` is NEVER called — the
resume clears the firmware dialog itself and clearing would corrupt main.py's HMS
dedup/grace bookkeeping.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_incident import (
    KIND_JAM,
    KIND_PHYSICAL,
    KIND_RUNOUT,
    RESOLVE_AUTO_RESUME,
    RESOLVE_OBSERVED_RUNNING,
    RESOLVE_OPERATOR,
    RESOLVE_TERMINAL,
    RESOLVE_WIRE_CLEAR,
    STATUS_ABORTED,
    STATUS_ESCALATED,
    STATUS_RECOVERING,
    STATUS_RESOLVED,
)
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services import printer_incidents, spool_respool, tray_fields
from backend.app.services.bambu_mqtt import AMS_STATUS_FILAMENT_CHANGE, AMS_STATUS_IDLE
from backend.app.services.hms_errors import (
    RUNOUT_HMS_CODES,
    AmsFaultClass,
    classify_hms_entry,
    current_runout_demand,
    extruder_side_short_codes,
    hms_short_code,
    mechanical_feed_short_codes,
    power_loss_prompt_standing,
)
from backend.app.services.printer_incidents import (
    RECOVERY_WAITING_REASONS,
    WAITING_REASON_RECOVERING,
    waiting_reason_for,
)
from backend.app.services.printer_manager import printer_manager
from backend.app.services.spool_respool import decode_global_tray, encode_global_tray

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.app.services.bambu_mqtt import PrinterState

logger = logging.getLogger(__name__)

# --- Trigger code sets ------------------------------------------------------
# DERIVED from the AMS fault taxonomy (``hms_errors``), never re-listed here: the
# classification of a firmware code is that module's job, and a second literal copy
# is exactly how two modules come to disagree about one code (doctrine invariant 1).
# The two names below are ONE class — MECHANICAL_FEED — split by WHERE the fault
# sits, so recovery can reason about the common factor on a re-jam (see
# extruder_side_only below). Nothing else narrows them.
#
# WIDENED 2026-08-09 (WS2b, operator-ratified partition): the swap machine now
# triggers on the whole mechanical-feed class. It previously acted on the
# ``legacy_swap`` subset (the 8010 family + 0300_801E) while the taxonomy already
# classified the send-out 8005, feed-into-extruder 8006 and feed-to-extruder 8028
# families the same way — a marker that existed solely to keep WS2a
# behavior-neutral, and that this wave deliberately spends. The widened members
# describe the same physical obstruction one step further along the path, and every
# one of them PAUSEs the print, so the machine can only ever act on a printer that
# is already stopped.
#
# The deliberate EXCLUSIONS that used to be recorded in this block now live with
# the classification they qualify, in the ``hms_errors`` taxonomy tables: the
# ``0700_0001`` ban (short-code header), the ``0700_0025`` precursor
# (INFORMATIONAL, code word 0x00020025), the pull-out/pull-back families
# (PHYSICAL_FAULT, 8003/8004) and the clog family (PHYSICAL_FAULT,
# 0300_801A/801C/8016 + 0300_4006). Those are no longer unowned: since WS2b the
# PHYSICAL_FAULT class escalates loudly with a hold instead of waiting for the
# generic pause-stall watchdog — it just never enters the swap loop.
AMS_FEED_FAULT_HMS_CODES: frozenset[str] = mechanical_feed_short_codes() - extruder_side_short_codes()

# Extruder-side (the MAIN extruder motor overloaded, not the AMS assist motor).
# Production incident 2026-07-17: 004-H2S sat PAUSEd ~2h40m with no reaction
# because this code was outside the trigger set. A swap still helps (fresh
# filament often clears the immediate overload), but the extruder — not the
# spool — is the common factor, so a re-jam after the swap must NOT penalize the
# healthy replacement (extruder_side_only).
EXTRUDER_FEED_FAULT_HMS_CODES: frozenset[str] = mechanical_feed_short_codes() & extruder_side_short_codes()

# Public union — kept as the single name the rest of the module (and main.py's
# import) reads, so RECOVERABLE_HMS_CODES / _primary_code / is_feed_fault are
# unchanged.
FEED_FAULT_HMS_CODES: frozenset[str] = AMS_FEED_FAULT_HMS_CODES | EXTRUDER_FEED_FAULT_HMS_CODES

# The DRIVER's live-fault vocabulary — "is the fault this incident is working on
# still standing on the wire" (:func:`_active_recoverable_codes`, read by the
# resume-confirm's repause-vs-abort decision and by :func:`_feed_fault_live`).
# Deliberately NOT the entry vocabulary: what the machine may be STARTED by is the
# taxonomy's classification of the live entries (:func:`live_candidates`), which
# also covers physical faults and external runouts the swap driver never touches.
RECOVERABLE_HMS_CODES: frozenset[str] = FEED_FAULT_HMS_CODES | RUNOUT_HMS_CODES

# The fault classes an incident owns. Everything else the taxonomy names
# (RFID_READ, INFORMATIONAL, and the unclassified None) belongs to the generic
# notify lane — an incident would add a hold nobody can clear.
ACTIONABLE_CLASSES: frozenset[AmsFaultClass] = frozenset(
    {
        AmsFaultClass.RUNOUT,
        AmsFaultClass.RUNOUT_EXTERNAL,
        AmsFaultClass.MECHANICAL_FEED,
        AmsFaultClass.PHYSICAL_FAULT,
    }
)

# class -> incident kind. ONE mapping; the model's KIND_* constants are the
# vocabulary and this is the only place a fault class becomes one of them.
_KIND_BY_CLASS: dict[AmsFaultClass, str] = {
    AmsFaultClass.MECHANICAL_FEED: KIND_JAM,
    AmsFaultClass.RUNOUT: KIND_RUNOUT,
    AmsFaultClass.RUNOUT_EXTERNAL: KIND_RUNOUT,
    AmsFaultClass.PHYSICAL_FAULT: KIND_PHYSICAL,
}

# Which class decides the incident when several are live at once. PHYSICAL first:
# a broken filament or a clogged extruder standing beside a feed fault means hands
# are needed whatever else is true, and acting on the milder classification would
# send the swap machine at a fault it cannot fix. RUNOUT outranks MECHANICAL for
# doctrine invariant 9 — a runout must never be routed into the swap machine, and a
# runout chain can raise a mechanical sibling while the roll is being purged.
_CLASS_PRECEDENCE: tuple[AmsFaultClass, ...] = (
    AmsFaultClass.PHYSICAL_FAULT,
    AmsFaultClass.RUNOUT,
    AmsFaultClass.RUNOUT_EXTERNAL,
    AmsFaultClass.MECHANICAL_FEED,
)

# --- waiting_reason tokens -------------------------------------------------
# The kind -> token table and its tokens live in ``printer_incidents`` (the store that
# owns the kinds) since the pause-cause kinds joined them; imported here because this
# module STAMPS the projection and the seven names below are read all over it.


# --- Safety bounds (code constants, NOT operator knobs — precedent the client-
#     owned settle-wait, bambu_mqtt._IDENTIFY_GATE_S / wait_ams_settle). The
#     unload/load/resume confirm timeout and per-step resend count ARE operator
#     settings. -----------------------------------------------------------------
_POLL_INTERVAL_S = 1.0  # live-state poll spacing during every confirm wait
_POST_RESUME_STABLE_S = 60  # RUNNING must hold this long after a resume = success
_REPAUSE_WATCH_S = 120  # ceiling on how long we wait for RUNNING after a resume
_MAX_CANDIDATES = 3  # distinct replacement trays tried before escalating
# Minimum time the AMS must hold idle + empty before an unload counts as complete
# when NO filament-change cycle was observed at all (command latency, or an unload
# the firmware treats as a no-op). The operator's proven manual recovery left 16 s
# between the unload and the load that worked; 15 s is that gap, floored.
_UNLOAD_GRACE_S = 15.0
# Absolute floor a replacement spool must clear even past the protected layers —
# never load a known-empty spool. A ledger ≤ 5 g is empty for replacement purposes.
_RECOVERY_HARD_MIN_G = 5
# Stuck-change firmware resets allowed per incident. The resume that unwedges a
# stuck filament-change is the touchscreen CONTINUE action (the vendored
# hms_actions.json 093-family action for 07008010 is ["CHECK_ASSISTANT","CONTINUE"])
# and it worked ONCE on 009-H2S 2026-07-20 after four unloads were silently
# ignored. Bounded-by-evidence at 1: a reset that did not free the AMS means it is
# genuinely wedged and needs hands, so a second resume would only loop, not heal.
_MAX_STUCK_RESETS = 1

# Refill auto-resume timing (code constants, NOT operator knobs — precedent
# _UNLOAD_GRACE_S). The AMS needs to register the freshly-inserted filament before a
# resume can land: a resume published on the presence edge itself races the firmware's
# own tray-state settle and is rejected. 15 s mirrors the operator's proven manual
# gap (_UNLOAD_GRACE_S) — long enough to settle, short enough that the operator is
# still standing at the printer.
_RUNOUT_RESUME_SETTLE_S = 15.0
# Bound on how long we wait for RUNNING after the resume before standing aside.
_RUNOUT_RESUME_CONFIRM_S = 30.0

# tray_now sentinel: no filament fed (unloaded). 255 on H2-series.
_NO_FILAMENT = 255

# The client's AMS write-refusal reason (a ``bambu_mqtt._AMS_REFUSAL_LOG_TEXT`` key)
# that recovery must NOT try to wait out: a drying cycle holds the lockout for hours,
# so the swap lane is doomed until a human stops it. Every other reason is identify
# contention, which settles in seconds.
_REFUSAL_DRYING = "drying"

# --- Settings defaults (mirror schemas/settings.py) -------------------------
_DEFAULT_ENABLED = True
_DEFAULT_MAX_ATTEMPTS = 2
_DEFAULT_STEP_TIMEOUT_S = 90
_DEFAULT_PROTECT_LAYERS = 7
# Refill auto-resume (006-H2S 2026-07-26). Default ON: the runout escalation leaves
# the printer PAUSEd for a same-slot refill, and the refill itself is the operator's
# "go" — making them walk back to a screen afterwards is exactly the deferral
# doctrine rule 1 forbids.
_DEFAULT_RUNOUT_AUTO_RESUME = True

# Human-facing escalation reasons for the failed notification.
_ESCALATE_DETAIL: dict[str, str] = {
    "multi_feeder_job": (
        "Multi-filament job — a mid-print tray swap is unsound (the firmware re-loads the "
        "originally mapped slot at the next filament change). Left PAUSED for a human."
    ),
    "jammed_tray_unresolved": "Could not identify which spool jammed. Left PAUSED for a human.",
    # Narrow by design: this reason is only honest when the candidate set was
    # genuinely EMPTY and no load was ever attempted. The 009-H2S incident reported
    # it after four failed loads — the two reasons below now carry those cases.
    "no_eligible_spool": "No other loaded spool matched the jammed filament. Left PAUSED for a human.",
    "candidate_loads_failed": (
        "Eligible replacement spools were found but none would load — check the filament path. Left PAUSED for a human."
    ),
    "feed_path_blocked": (
        "Replacement spools failed to load repeatedly — the filament path (buffer / PTFE) is likely "
        "blocked. Clear the buffer and PTFE path, then resume on the printer. Left PAUSED for a human."
    ),
    "ams_drying": (
        "The AMS is running a drying cycle, so no filament change can be commanded without failing it. "
        "Left PAUSED for a human."
    ),
    "only_low_spools_in_protected_layers": (
        "The only matching spool is below the minimum-start weight this early in the print. Left PAUSED for a human."
    ),
    "only_near_empty_spools": "Every matching spool is effectively empty. Left PAUSED for a human.",
    "runout_needs_refill": (
        "Filament ran out and the printer only accepts new filament in the SAME slot — "
        "insert filament and resume on the printer."
    ),
    "candidates_exhausted": "Tried every eligible replacement spool without a stable resume. Left PAUSED for a human.",
    "unload_failed": (
        "The AMS ignored the unload while stuck mid filament-change and the firmware reset (resume) did not "
        "free it — physical intervention at the printer is likely required (check the filament buffer/feeder)."
    ),
    "stuck_reset_failed": (
        "The AMS is stuck mid filament-change and did not respond to the firmware reset (resume) — physical "
        "intervention at the printer is likely required (check the filament buffer/feeder)."
    ),
    "repeated_jams": (
        "Auto-recovered several times this job but the fault keeps returning — likely an "
        "extruder-side problem, not the spool. Left PAUSED for a human."
    ),
    # WS2b: the two classes that reach escalation without ever entering the loop.
    "physical_fault": (
        "A physical filament fault (broken filament, a clog, or a failed pull-back) — fresh filament "
        "cannot clear it, so no swap was attempted. Check the filament path at the printer, then resume."
    ),
    "recovery_interrupted": (
        "A recovery was interrupted (the server restarted mid-swap) and the printer is still PAUSED with no "
        "fault the wire can still name — check the filament path and resume on the printer."
    ),
    "external_spool_runout": (
        "The EXTERNAL spool holder ran out — there is no AMS slot to swap to. Load new filament on the "
        "spool holder and resume on the printer."
    ),
    # 003-H2S 2026-08-11: this fault used to enter the AMS jam machine, invent a
    # jammed tray it could not find, escalate "jammed_tray_unresolved" and quarantine
    # the printer for AMS hardware — while the actual hardware involved was a spool
    # holder with nothing loaded on it.
    "external_feed_fault": (
        "The EXTERNAL spool path failed to feed — check the spool and filament on the holder, feed filament "
        "into the PTFE tube if asked, then resume on the printer. No AMS is involved and no swap will be "
        "attempted."
    ),
}


@dataclass(frozen=True)
class RecoverySettings:
    """The four operator-tunable knobs, read once per incident."""

    enabled: bool
    max_attempts: int
    step_timeout_s: float
    protect_layers: int


@dataclass
class _RecoveryEvidence:
    """What the candidate loop actually achieved, so the escalation reason is chosen
    by evidence instead of by position in the code.

    The 009-H2S incident reported ``no_eligible_spool`` after four failed loads —
    a lie that sent the operator looking for spools instead of at the feed path.
    """

    confirmed_unloads: int = 0  # unload cycles the AMS confirmed complete
    loads_attempted: int = 0  # candidates we sent an ams_change_filament for
    loads_confirmed: int = 0  # loads the printer confirmed on tray_now

    def exhaustion_reason(self) -> str:
        """The honest escalation reason for running out of candidates/rounds."""
        if self.loads_attempted == 0:
            return "no_eligible_spool"  # genuinely nothing to try
        if self.loads_confirmed:
            return "candidates_exhausted"  # loads worked; the print wouldn't hold
        if self.confirmed_unloads:
            # The AMS unloaded cleanly every round and STILL nothing would feed —
            # the blockage is downstream of the spool.
            return "feed_path_blocked"
        return "candidate_loads_failed"


@dataclass(frozen=True)
class FaultCandidate:
    """One live AMS fault the taxonomy classified as actionable.

    The tuple the entry gate reasons over: WHAT kind of fault (``fault_class``),
    WHICH code names it (``short_code`` — what the notifications say), WHERE it is
    (``slot``, only the attr-aware ``hms[]`` lane can supply one), whether the
    EXTRUDER is the common factor, and whether the hardware is the EXTERNAL spool
    holder rather than an AMS (``external``, straight from the taxonomy's verdict —
    never re-derived from the code string here, doctrine invariant 1).
    """

    fault_class: AmsFaultClass
    short_code: str
    slot: tuple[int, int] | None
    extruder_side: bool
    external: bool = False


@dataclass(frozen=True)
class RecoveryIncident:
    """Immutable context for one incident, resolved at the entry gate.

    ``incident_id`` is the durable ``printer_incident`` row this context belongs to —
    every close, escalate and guidance dedup goes through it, so nothing in the
    lifecycle depends on this object surviving a restart.
    """

    incident_id: int
    printer_id: int
    job_id: str
    codes: frozenset[str]
    # The sorted, slot-qualified fingerprint of the triggering candidate set —
    # the identity used to tell "this fault, again" from "a different fault".
    fingerprint: str
    # None = a FOREIGN print: no farm queue unit, so no waiting_reason projection
    # and no swap machine (its single-feeder verdict needs the dispatch mapping).
    item_id: int | None
    settings: RecoverySettings
    jammed_global_tray: int | None
    kind: str
    # True when the deciding fault is on the EXTERNAL spool holder (any class): no
    # AMS slot, no sibling tray, no swap machine, different operator copy. Carried
    # from the taxonomy verdict of the PRIMARY candidate, never re-derived.
    external: bool
    # True when EVERY mechanical-feed code is extruder-side (main extruder
    # overloaded). A re-jam then keeps the replacement in rotation — the extruder,
    # not the spool, is the common factor.
    extruder_side_only: bool
    layer_at_fault: int
    code: str
    printer_name: str
    job_name: str

    @property
    def is_feed_fault(self) -> bool:
        """Does the swap machine own this incident? (Derived — never stored twice.)"""
        return self.kind == KIND_JAM


# --- Module edge state -------------------------------------------------------
# What remains in memory after WS2b is ONLY what is cheap to rebuild and harmless
# to lose. Every DECISION (already handled? printer already owned? flap cap spent?)
# now reads ``printer_incident`` rows — the four dicts that used to hold them
# (``_handled`` / ``_escalated`` / ``_success_counts`` / ``_runout_guidance_sent``)
# are DELETED: a restart emptied them while the standing HMS came straight back,
# and ``_escalated`` never expired inside a process, so a later different fault on
# the same job could never be recovered.

# Live swap-driver tasks, for :func:`has_live_recovery` only — "is the machine
# ACTING right now", which is a different question from "is this printer owned"
# (that one is the open incident). Entry exclusivity is the DB's partial unique
# index, never this dict.
_active_tasks: dict[int, asyncio.Task] = {}

# Flap bound: after this many RESOLVED jam incidents in ONE job, the next fault
# escalates instead of swapping again (code constant, precedent _MAX_CANDIDATES).
# Counted from the durable ledger, so a restart no longer hands a sick printer a
# fresh budget.
_MAX_SUCCESSES_PER_JOB = 3

# (printer_id, job_id) -> (fault fingerprint, monotonic ts, outcome) of the last
# ENTRY EVALUATION. Two jobs, both cheap:
#
# 1. Throttle. A standing fault rides every ~1 Hz status push and the entry gate is
#    now per-push (decoupled from the notify dedup, which is what silenced 9 runout
#    episodes) — re-reading the durable gates every push would be a query per second
#    per printer. A CHANGED fingerprint always evaluates immediately, so liveness is
#    bounded by the WIRE, not by this timer.
# 2. Outcome-change logging. Every silent path must produce at least one line, but
#    not one per push: a gate-out logs when the outcome CHANGES for this
#    (printer, job), which is exactly the "why didn't recovery fire" trail.
_last_eval: dict[tuple[int, str], tuple[str, float, str]] = {}
_EVAL_THROTTLE_S = 10.0

# (incident_id, global_tray) demand moves already announced. Per INCIDENT, so a
# restart re-announces at most once per incident — which is desirable: the operator
# who missed the first message still needs the slot.
_guidance_sent: set[tuple[int, int]] = set()

# printer_id -> the last per-push wire sample :func:`note_demand_watch` took, as
# (gcode_state, demanded slot or None, live actionable short codes, connection_epoch).
# Edges over this sample drive the refill auto-resume and the "the printer is running
# again" close. The epoch rides along because a sample from a PREVIOUS MQTT session
# cannot be compared for a code DISAPPEARING — a reboot wipes the standing HMS list, so
# every negative edge fires at once (2026-09-04, printer 8).
_wire_sample: dict[int, tuple[str, tuple[int, int] | None, frozenset[str], int]] = {}

# (printer_id, job_id) -> fault fingerprints an ABORTED close barred from re-entry,
# while they are STILL the same standing fault. The loop bound the per-push entry
# gate needs: without it, a code the firmware leaves standing after healing itself
# (or after an operator took over) would re-open an incident every throttle window.
#
# It is an EDGE ledger, not a latch — the sampler re-arms a fingerprint the moment
# the wire says this is no longer the same fault:
#   * its codes are no longer ALL standing (the fault cleared, wholly or partly), or
#   * the printer transitioned INTO PAUSE (a transient close said "it never held the
#     printer"; a pause proves that answer is now stale).
# Losing it to a restart re-arms exactly once, which is the WS2b intent: a fault
# still standing across a restart deserves one incident and one alert.
_blocked: dict[tuple[int, str], set[str]] = {}

# Per-job stuck-change reset budget: (printer_id, job_id) -> firmware resets
# (resumes) already published this incident. Bounds the wedged-change reset to
# _MAX_STUCK_RESETS; a frozen RecoveryIncident cannot carry a mutable counter, so
# this follows the module's per-(printer, job) dict idiom.
# Process-lifetime like the rest of this block — a post-restart re-fire gets its
# one bounded reset again, which is safe.
_stuck_resets: dict[tuple[int, str], int] = {}

# incident_id -> the monotonic instant :func:`sweep_open_incidents` FIRST saw every
# one of its close guards hold. The dwell that stops a momentary reading closing a
# real hold: an incident closes only once the whole constellation (connected,
# positive non-PAUSE state, ZERO actionable faults) has stood continuously for
# :data:`_HOLD_OVER_DWELL_S`. Popped the instant any guard breaks, so the wait
# restarts rather than accumulating across flaps.
#
# Process-lifetime by design (derive-don't-store): the dwell simply restarts after a
# restart, which is the SAFE direction — the worst cost is one extra 120 s before a
# curable incident closes, and the startup rearm already closes the clear-cut cases
# without any dwell at all.
_hold_over_since: dict[int, float] = {}

# How long the close constellation must hold before the sweep acts. Sized against
# the false positive that OPENED incident #60: a fault re-evaluated in the seconds
# either side of a dispatch, on a printer momentarily reading a non-PAUSE state. A
# code constant, not an operator knob (precedent: _EVAL_THROTTLE_S).
_HOLD_OVER_DWELL_S = 120.0

# --- W2 durable repeat-jam quarantine (code constants, NOT operator knobs) ----
# A printer whose recovery escalates _JAM_QUARANTINE_THRESHOLD times within
# _JAM_QUARANTINE_WINDOW_H hours is quarantined: a recurring AMS jam is hardware
# (buffer / feeder), not a spool the swap machine can fix. Counted from the durable
# recovery_escalation ledger so it survives the restarts this in-memory state does
# not (009-H2S 2026-07-20: three same-fault escalations across the day).
_JAM_QUARANTINE_WINDOW_H = 24
_JAM_QUARANTINE_THRESHOLD = 2

# WHICH escalations may count toward that quarantine — an ALLOWLIST, because the
# quarantine's own sentence is a DIAGNOSIS ("AMS hardware suspected (buffer/feeder)")
# and only the jam machine's own hardware-suspect outcomes are evidence for it.
# 003-H2S 2026-08-11 proved a count over ALL reasons is a different statement from
# the one it prints: the morning's filament RUNOUT plus an evening EXTERNAL-spool
# fault reached 2-in-24h and quarantined a printer whose AMS was never involved in
# either. Every escalation still records its row — the ledger is forensic and stays
# complete; this governs only what the counter reads.
#
# COUNTS — the swap machine tried, or refused to try, and the filament PATH is the
# suspect (buffer / feeder / PTFE / the tray it could not name):
_JAM_QUARANTINE_REASONS: frozenset[str] = frozenset(
    {
        "jammed_tray_unresolved",  # a mechanical fault whose feeder no witness could name
        "feed_path_blocked",  # clean unloads every round and still nothing would feed
        "unload_failed",  # the AMS ignored the unload and the reset did not free it
        "stuck_reset_failed",  # wedged mid filament-change, unresponsive to the firmware reset
        "repeated_jams",  # recovered repeatedly this job and the fault keeps returning
        "candidates_exhausted",  # every replacement loaded and none held a stable resume
        "candidate_loads_failed",  # eligible spools were found and none of them would load
    }
)

# NEVER COUNTS — everything whose cause is a consumable, the inventory, a lockout,
# the JOB's shape, the external holder, or a restart artifact. Enumerated rather than
# derived so a NEW reason token cannot join the count by default: the partition is
# mirrored against ``_ESCALATE_DETAIL`` by test, and an unclassified token fails it.
_NON_QUARANTINE_REASONS: frozenset[str] = frozenset(
    {
        "runout_needs_refill",  # a roll ran out — a consumable, not hardware (the 003-H2S row #1)
        "external_spool_runout",  # the spool HOLDER ran out; no AMS took part
        "external_feed_fault",  # the external feed path failed; no AMS took part
        "physical_fault",  # breakage / clog / failed pull-back — hands, but not a buffer/feeder jam
        "multi_feeder_job",  # the JOB's shape refused the swap; the AMS is fine
        "no_eligible_spool",  # inventory: nothing to swap to, no load was ever attempted
        "only_low_spools_in_protected_layers",  # inventory + the grams floor
        "only_near_empty_spools",  # inventory: every match is effectively empty
        "ams_drying",  # a lockout the farm declined to fight; the AMS is healthy
        "recovery_interrupted",  # a restart artifact — kind-ambiguous, evidence of nothing
    }
)


def _reset_state() -> None:
    """Test hook: clear module-level edge/dedup state between cases."""
    _active_tasks.clear()
    _stuck_resets.clear()
    _last_eval.clear()
    _guidance_sent.clear()
    _wire_sample.clear()
    _blocked.clear()
    _hold_over_since.clear()
    printer_incidents._reset_state()


def has_live_recovery(printer_id: int) -> bool:
    """True when a swap-recovery TASK is actively running for ``printer_id``.

    Liveness only — "is the machine acting right now". Ownership ("may another
    handler treat this pause as covered") is the OPEN INCIDENT, which outlives the
    task and survives a restart; the pause-stall watchdog checks both. A missing
    slot OR a task that has already finished (``.done()``) both read as no live
    recovery, so a token orphaned by a mid-recovery restart/crash is reclaimed
    instead of silencing the watchdog forever (R1)."""
    task = _active_tasks.get(printer_id)
    return task is not None and not task.done()


def _note_outcome(printer_id: int, job_id: str, fingerprint: str, outcome: str, *, detail: str = "") -> None:
    """Record this entry evaluation and log it when the OUTCOME CHANGED.

    The entry gate runs per status push, so a standing fault would otherwise write
    the same "not recovered — X" line every second. Logging on the outcome EDGE per
    (printer, job) keeps the full "why didn't recovery fire" trail — every silent
    path still produces at least one line — at one line per decision.
    """
    key = (printer_id, job_id)
    prev = _last_eval.get(key)
    changed = prev is None or prev[0] != fingerprint or prev[2] != outcome
    if prev is None:
        # A printer only ever runs ONE job at a time, so a new job's first
        # evaluation retires the previous job's ledgers rather than letting a
        # process-lifetime dict grow one entry per job forever (the durable record
        # of what happened is the incident table, not these).
        for stale in [k for k in _last_eval if k[0] == printer_id and k[1] != job_id]:
            _last_eval.pop(stale, None)
        for stale in [k for k in _blocked if k[0] == printer_id and k[1] != job_id]:
            _blocked.pop(stale, None)
    _last_eval[key] = (fingerprint, _monotonic(), outcome)
    if changed:
        logger.info(
            "spool_recovery: printer %s job %s fault %s — %s%s",
            printer_id,
            job_id or "-",
            fingerprint or "(none)",
            outcome,
            f" ({detail})" if detail else "",
        )


async def _fault_already_closed(db: AsyncSession, printer_id: int, job_id: str, fingerprint: str) -> bool:
    """Did we already finish with THIS fault, and is it still the same standing one?

    ONE predicate, two clauses that must both hold — used by the entry gate and by
    :func:`will_own` so a suppressed alert and a refused entry can never disagree:

    * the durable half: a CLOSED incident for this ``(printer, job, fingerprint)``
      whose status is ABORTED. An aborted close means an external actor took over or
      the fault proved transient; a RESOLVED one deliberately re-arms, because a
      genuine second tangle in one job must still be recovered (the flap cap bounds
      that loop);
    * the liveness half: :data:`_blocked` still holds the fingerprint, i.e. the wire
      has not re-armed it by clearing the codes or by pausing the printer.
    """
    if fingerprint not in _blocked.get((printer_id, job_id), ()):
        return False
    closed = await printer_incidents.find_closed(db, printer_id, job_id, fingerprint)
    return closed is not None and closed.status == STATUS_ABORTED


def _rearm_blocked(printer_id: int, live_tokens: frozenset[str], *, paused_edge: bool) -> None:
    """Drop the blocks the wire has just invalidated (see :data:`_blocked`)."""
    for (pid, job), fingerprints in list(_blocked.items()):
        if pid != printer_id:
            continue
        if paused_edge:
            _blocked.pop((pid, job), None)
            continue
        survivors = {fp for fp in fingerprints if set(fp.split(",")) <= live_tokens}
        if survivors:
            _blocked[(pid, job)] = survivors
        else:
            _blocked.pop((pid, job), None)


def _eval_throttled(printer_id: int, job_id: str, fingerprint: str) -> bool:
    """Should this push skip the durable gates? (Same fault, evaluated recently.)

    A CHANGED fingerprint is never throttled — a new fault must be seen on the push
    that carries it, not up to :data:`_EVAL_THROTTLE_S` later.
    """
    prev = _last_eval.get((printer_id, job_id))
    if prev is None or prev[0] != fingerprint:
        return False
    return (_monotonic() - prev[1]) < _EVAL_THROTTLE_S


def _log_candidate_outcome(incident: RecoveryIncident, *, gtid: int | None, verdict: str) -> None:
    """One parseable INFO line after a non-ok unload/load confirm and at candidate-
    loop end, carrying the live telemetry that explains WHY a step didn't take —
    candidate global tray, verdict, live tray_now, ams_status_main/sub, and the
    pending tray target the firmware is honoring."""
    st = _get_state(incident.printer_id)
    logger.info(
        "[spool_recovery] candidate outcome printer=%s gtid=%s verdict=%s tray_now=%s "
        "ams_status=%s/%s pending_target=%s",
        incident.printer_id,
        gtid,
        verdict,
        getattr(st, "tray_now", None) if st is not None else None,
        getattr(st, "ams_status_main", None) if st is not None else None,
        getattr(st, "ams_status_sub", None) if st is not None else None,
        getattr(st, "pending_tray_target", None) if st is not None else None,
    )


# --- small helpers ----------------------------------------------------------


def _now() -> float:
    return asyncio.get_running_loop().time()


def _monotonic() -> float:
    """Wall-independent clock for the entry-gate throttle / outcome ledger.

    Deliberately NOT :func:`_now`: those two are patched together by the driver's
    fake-clock tests to fast-forward confirm waits, and fast-forwarding the entry
    throttle with them would hide exactly the per-push behaviour it bounds. Also
    callable outside a running loop (the wire sampler is sync)."""
    return time.monotonic()


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


def live_candidates(state) -> frozenset[FaultCandidate]:
    """Every ACTIONABLE AMS fault standing on the printer right now.

    Derived from ALL live ``state.hms_errors`` entries through the WS2a taxonomy
    (``hms_errors.classify_hms_entry``, which resolves the two wire lanes), not from
    the notification dedup's "new codes". That decoupling is the fix for the silent
    class: a code STANDING at restart (``notify_dedup.seed_standing`` marks it
    already-seen) or one flapping inside the 600 s re-notify window never appeared in
    ``new_error_codes``, so the old spawn never fired and never logged — 9 runout
    episodes passed in total silence.

    Pure and DB-free: this runs on every status push, and a malformed entry is
    skipped rather than raised (invariant 10).
    """
    out: set[FaultCandidate] = set()
    for e in getattr(state, "hms_errors", None) or []:
        verdict = classify_hms_entry(e)
        if verdict is None or verdict.fault_class not in ACTIONABLE_CLASSES:
            continue
        try:
            short = hms_short_code(e.attr, e.code)
        except Exception:  # noqa: BLE001 — a malformed HMS entry must not break the scan
            continue
        out.add(
            FaultCandidate(
                fault_class=verdict.fault_class,
                short_code=short,
                slot=verdict.slot,
                extruder_side=verdict.extruder_side,
                external=verdict.external,
            )
        )
    return frozenset(out)


def fault_tokens(candidates) -> frozenset[str]:
    """The individual ``class:short[@ams-tray]`` tokens a fingerprint is built from.

    Slot-QUALIFIED on purpose: the short code alone is slot-agnostic
    (``0700_8011`` is "an AMS slot ran dry", not "slot 3 ran dry"), so a second roll
    emptying later in the same job would have looked like the fault already closed
    and been swallowed.
    """
    return frozenset(
        f"{c.fault_class.value}:{c.short_code}" + (f"@{c.slot[0]}-{c.slot[1]}" if c.slot is not None else "")
        for c in candidates
    )


def candidate_fingerprint(candidates) -> str:
    """The stable identity of a set of live faults, for the already-handled test.

    Sorted so set iteration order can never change it, and truncated to the column
    width — deterministically, so a truncated fingerprint still matches itself."""
    return ",".join(sorted(fault_tokens(candidates)))[:256]


def _dominant_class(candidates) -> AmsFaultClass | None:
    """Which class decides the incident when several are live (:data:`_CLASS_PRECEDENCE`)."""
    live = {c.fault_class for c in candidates}
    for fault_class in _CLASS_PRECEDENCE:
        if fault_class in live:
            return fault_class
    return None


def _primary_candidate(candidates, fault_class: AmsFaultClass) -> FaultCandidate | None:
    """The representative candidate of the deciding class — lowest short code wins,
    so the code the operator is told is stable across pushes."""
    members = sorted((c for c in candidates if c.fault_class is fault_class), key=lambda c: c.short_code)
    return members[0] if members else None


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


def runout_slot_desc(global_tray: int | None) -> str | None:
    """Human slot name for the runout notification ("AMS A slot 1") from a regular
    AMS global tray (letter = A + g//4, slot = g%4 + 1). ``None`` for AMS-HT /
    external / unresolved trays (no clean letter+slot mapping).

    PUBLIC because ``farm_stall``'s hourly runout reminder renders the same slot
    name (006-H2S 2026-07-26) — one origin for the wording, so the escalation, the
    guidance refresh and the reminder can never disagree about what a slot is called."""
    if global_tray is None or not (0 <= global_tray <= 127):
        return None
    return f"AMS {chr(ord('A') + global_tray // 4)} slot {global_tray % 4 + 1}"


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
    """The printing queue item this job IS, by dispatch id — or ``None`` (foreign).

    The SAME predicate ``farm_correlation`` resolves a terminal with: this printer, a
    ``printing`` row, ``dispatch_subtask_id`` equality, newest-first. The id is minted
    per dispatch and stamped on EVERY dispatched item, so equality alone is the whole
    identity test — a print the farm did not dispatch cannot match one, whatever else
    is true about it.

    003-H2S 2026-08-11: this used to add a ``print_batch`` join plus
    ``sku_file_id IS NOT NULL`` on top, which no other id consumer applies. A farm
    item started from a plain file (no SKU) therefore resolved ``None``, and its own
    incident was logged, alerted and notified as **foreign** — no waiting_reason
    projection on the unit the operator was watching, and no dispatch evidence for
    the feeder resolution that needed it. Origin is what the id says it is.
    """
    if not job_id:
        return None
    result = await db.execute(
        select(PrintQueueItem)
        .where(PrintQueueItem.printer_id == printer_id)
        .where(PrintQueueItem.status == "printing")
        .where(PrintQueueItem.dispatch_subtask_id == job_id)
        .order_by(PrintQueueItem.started_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def _candidate_slot(candidates, fault_class: AmsFaultClass) -> tuple[int, int] | None:
    """The slot the FIRMWARE named for the deciding class, if any.

    Only the attr-aware ``hms[]`` lane carries one (the short-code lane discarded the
    attr low byte), and only the per-slot families use it — a jam's 8010 code names
    the AMS unit, never the tray. Lowest slot first so the answer is stable.
    """
    slots = sorted(c.slot for c in candidates if c.fault_class is fault_class and c.slot is not None)
    return slots[0] if slots else None


def _resolve_fault_tray(
    item: PrintQueueItem | None,
    state,
    *,
    kind: str,
    external: bool,
    candidates,
    printer_id: int | None = None,
) -> tuple[int | None, str]:
    """Which global tray the fault names, and the feeder verdict.

    Ordering by kind, strongest evidence first:

    * EXTERNAL (any class) — there is no AMS slot at all, and inventing one from the
      mapping would send the operator to a tray that never fed this print. 003-H2S
      2026-08-11 is the jam-class proof: the swap machine took the printer's live
      feeder for the "jammed tray" of a fault that happened on the spool holder.
    * RUNOUT — the firmware's own CURRENT DEMAND, then the slot its attr named, then
      the wire-first resolution below. 006-H2S 2026-07-26: mapping ``[0]``,
      ``tray_now`` 255 and a standing ``0700_2200_0002_0001`` demand for slot 3 — the
      inference answered global tray 0 and the escalation told the operator to refill
      "AMS A slot 1", which the printer was not asking for and would not resume on. A
      demand also settles the verdict as ``single``: it names ONE exact slot to
      refill, honest guidance even on a multi-feeder job (a runout never enters the
      swap machine anyway, doctrine invariant 9).
    * PHYSICAL — the attr-named slot only. These escalate either way, so a missing
      slot costs a less specific message, never a wrong one.
    * JAM — :func:`_resolve_jammed_tray`; the 8010 family carries no slot attribution.
    """
    if external:
        return None, "single"
    if kind == KIND_RUNOUT:
        demand = current_runout_demand(getattr(state, "hms_errors", None) or [])
        if demand is not None:
            return demand[0] * 4 + demand[1], "single"
    if kind in (KIND_RUNOUT, KIND_PHYSICAL):
        slot = _candidate_slot(
            candidates, AmsFaultClass.RUNOUT if kind == KIND_RUNOUT else AmsFaultClass.PHYSICAL_FAULT
        )
        if slot is not None:
            return slot[0] * 4 + slot[1], "single"
        if kind == KIND_PHYSICAL:
            return None, "single"
    return _resolve_jammed_tray(state, candidates=candidates, item=item, printer_id=printer_id)


def _valid_feeder(value) -> int | None:
    """A tray value that names a real AMS feeder (0..253), else ``None``.

    254 (external spool) and 255 (nothing fed) are sentinels, not trays: neither is
    something the swap machine can unload, nor a slot an operator can be sent to.
    """
    try:
        tray = int(value)
    except (TypeError, ValueError):
        return None
    return tray if 0 <= tray <= 253 else None


def _mapping_feeders(mapping) -> list[int]:
    """The distinct feeders a filament→tray mapping names, in mapping order.

    Accepts both shapes one exists in: the farm item's JSON string and the raw list
    the client captured off the request topic. ``-1`` (external) and anything
    unparseable contribute nothing — a mapping we cannot read must not invent a
    second feeder and escalate a recoverable job as multi-material.
    """
    if isinstance(mapping, str):
        try:
            mapping = json.loads(mapping)
        except (ValueError, TypeError):
            return []
    if not isinstance(mapping, (list, tuple)):
        return []
    feeders = [int(v) for v in mapping if isinstance(v, (int, float)) and not isinstance(v, bool) and int(v) >= 0]
    return list(dict.fromkeys(feeders))


def _slicer_mapping(printer_id: int | None):
    """The ``ams_mapping`` the SLICER sent when it started the live print, or ``None``.

    ``bambu_mqtt`` captures it off the request topic (a ``project_file`` command
    Bambuddy did not send) and clears it at the job terminal, which makes it the only
    PRE-FAULT statement a foreign print makes about how many filaments it maps. Read
    defensively: a client that never saw the request topic (the broker refuses that
    subscription on some firmware) simply contributes no evidence.
    """
    if printer_id is None:
        return None
    client = printer_manager.get_client(printer_id)
    return getattr(client, "captured_ams_mapping", None) if client is not None else None


def _fed_feeders(state) -> list[int]:
    """Feeders this job has actually FED from, in order, consecutive repeats collapsed.

    The client's ``tray_change_log`` is a temporal record — it answers "which tray was
    active", and a REPEAT in it is the fingerprint of alternation. Consecutive
    duplicates are collapsed first because they are bookkeeping, not motion (the log
    is seeded at print start and ``last_loaded_tray`` resets with it, so the first
    real report can echo the seed).
    """
    runs: list[int] = []
    for entry in getattr(state, "tray_change_log", None) or []:
        tray = _valid_feeder(entry[0] if isinstance(entry, (tuple, list)) and entry else entry)
        if tray is not None and (not runs or runs[-1] != tray):
            runs.append(tray)
    return runs


def _job_feeders(state, item: PrintQueueItem | None, printer_id: int | None) -> tuple[list[int], str]:
    """The feeders this JOB is known to use, and which witness answered.

    Ordered by evidence strength, and the FIRST witness that speaks decides alone — a
    union across witnesses would let a healed one-way move masquerade as a second
    mapped filament, escalating ``multi_feeder_job`` on a job that is single-material
    and recoverable:

    * ``dispatch`` — the farm item's ``ams_mapping``: what WE told the printer to feed.
    * ``slicer`` — the mapping Studio/Orca sent with a foreign ``project_file`` start.
    * ``fed`` — the ``tray_change_log``, last resort. It contributes to the MULTI-feeder
      verdict only when it shows the job RETURNING to a feeder it already left, which
      is the signature of a multi-material job and which no one-way move can produce.
      A firmware auto-refill and an earlier recovery swap both move exactly once, and
      reading either as "multi-material" would take the whole farm-dispatched jam path
      away the first time a backup slot took over.
    """
    mapped = _mapping_feeders(getattr(item, "ams_mapping", None) if item is not None else None)
    if mapped:
        return mapped, "dispatch"
    mapped = _mapping_feeders(_slicer_mapping(printer_id))
    if mapped:
        return mapped, "slicer"
    runs = _fed_feeders(state)
    distinct = list(dict.fromkeys(runs))
    if len(runs) > len(distinct):
        return distinct, "fed"  # a feeder was returned to → genuinely multi-material
    return distinct[:1], "fed"


def _resolve_jammed_tray(
    state,
    *,
    candidates=(),
    item: PrintQueueItem | None = None,
    printer_id: int | None = None,
) -> tuple[int | None, str]:
    """Which global tray jammed, and the feeder verdict. WIRE FIRST, origin-agnostic.

    ``single`` — one deterministic feeder. ``multi_feeder`` — the job maps more than
    one filament, so a mid-print tray swap is unsound (the firmware re-loads the
    ORIGINALLY MAPPED slot at the next filament change) and the caller escalates.
    ``none`` — nothing resolvable, also an escalation.

    Evidence order, strongest first:

    1. The fault's OWN slot attribution — only the attr-aware ``hms[]`` lane carries
       one, and when it does it is the firmware naming the tray directly.
    2. The multi-feeder verdict from :func:`_job_feeders`, evaluated BEFORE the live
       feeder: a live ``tray_now`` on a multi-material job is a true answer to the
       wrong question, because the swap it would authorise cannot hold.
    3. The active feeder at fault time — ``tray_now``, then ``last_loaded_tray`` (the
       last tray that actually fed THIS job, reset at every print start). After a feed
       fault ``tray_now`` frequently reads 255 "nothing feeding" while the jam is on
       the tray that was feeding a second earlier, which is exactly what
       ``last_loaded_tray`` still holds.
    4. The single mapped feeder, when the wire named none.
    """
    slot = _candidate_slot(candidates, AmsFaultClass.MECHANICAL_FEED)
    if slot is not None:
        return slot[0] * 4 + slot[1], "single"

    feeders, _source = _job_feeders(state, item, printer_id)
    if len(feeders) > 1:
        return None, "multi_feeder"

    live = _valid_feeder(getattr(state, "tray_now", None))
    if live is None:
        live = _valid_feeder(getattr(state, "last_loaded_tray", None))
    if live is not None:
        return live, "single"
    if feeders:
        return feeders[0], "single"
    return None, "none"


def _dual_nozzle_feeders(state, printer_id: int | None) -> list[int]:
    """Every extruder's own feeder on a DUAL-NOZZLE machine, from the per-extruder map.

    ``state.tray_now`` is a SINGLE value, and on an H2C/H2D it describes only whichever
    hotend is active — so a slot feeding the OTHER nozzle reads as "not feeding" and every
    consumer of :func:`_feeder_before_edge` silently answers False for it. The wire carries
    the honest answer: ``bambu_mqtt`` normalizes ``device.extruder.info[N].snow`` into
    ``PrinterState.h2d_extruder_snow`` (``{extruder_id: global_tray}``) on exactly these
    machines. A slot feeding EITHER nozzle was feeding.

    Gated on ``BambuMQTTClient.is_dual_nozzle`` — the public topology question, which is
    also the fork's stated rule that "the nozzle TOPOLOGY changes what a wire field is
    allowed to mean". Read through the client because the flag is runtime wire state and
    ``PrinterState`` does not carry it; ``printer_id`` is therefore required for this
    witness, and a caller that cannot supply one (or a disconnected client) simply
    contributes nothing here and falls back to the single-tray path.

    Empty list = no per-extruder evidence: a single-nozzle printer, a dual-nozzle one whose
    firmware has not sent the extruder block yet, or an unreadable client. Never raises.
    """
    if printer_id is None:
        return []
    try:
        client = printer_manager.get_client(printer_id)
        if client is None or not client.is_dual_nozzle:
            return []
        snow = getattr(state, "h2d_extruder_snow", None) or {}
        feeders: list[int] = []
        for value in snow.values():
            tray = _valid_feeder(value)
            if tray is not None and tray not in feeders:
                feeders.append(tray)
        return feeders
    except Exception:  # noqa: BLE001 — an unreadable topology contributes no evidence
        logger.exception("spool_recovery: per-extruder feeder read failed for printer %s", printer_id)
        return []


def _feeder_before_edge(state, *, item: PrintQueueItem | None = None, printer_id: int | None = None) -> int | None:
    """Which global tray was feeding IMMEDIATELY BEFORE an AMS presence edge, or None.

    A DIFFERENT question from :func:`_resolve_jammed_tray`'s, asked over the same
    witnesses — so the witnesses are shared and only the ORDER changes. The jam case
    asks "which tray is the fault on, right now"; this asks "which tray was feeding a
    moment ago", and the two disagree on exactly the witness the answer turns on:

    * ``last_loaded_tray`` FIRST — the last tray that actually fed THIS job (reset at
      every print start), which is the closest thing the wire has to a record of the
      instant before the edge;
    * then the job's single mapped feeder (:func:`_job_feeders` — the farm item's
      dispatch mapping, the slicer's mapping, then the fed log), which speaks for the
      whole job and therefore also for the instant before the edge;
    * ``tray_now`` LAST, and only as a fallback. At a loss edge it may ALREADY have
      moved (a firmware auto-refill switches to a backup slot the instant the first one
      runs dry), and after a stopped feed it reads the 255 sentinel, which means
      "nothing is feeding" and never "the path is clear" (invariant 8). Trusting it
      first is how "was this slot feeding?" would answer False for the very slot that
      just ran out.

    A multi-feeder job with no ``last_loaded_tray`` answers None rather than guessing:
    an ambiguous answer here would either accuse a healthy slot or exonerate the
    draining one, and the caller's fallback (the live-HMS runout evidence, once the
    firmware speaks ~3 minutes later) is the honest second chance.

    SINGLE-tray by contract, and that is why it cannot answer a dual-nozzle machine on its
    own: two hotends can be feeding at once, so "the feeder" is not a well-formed question
    there. :func:`slot_was_feeding` asks the per-extruder witness separately
    (:func:`_dual_nozzle_feeders`) and treats this as the fallback tier.
    """
    fed = _valid_feeder(getattr(state, "last_loaded_tray", None))
    if fed is not None:
        return fed
    feeders, _source = _job_feeders(state, item, printer_id)
    if len(feeders) == 1:
        return feeders[0]
    return _valid_feeder(getattr(state, "tray_now", None))


def slot_was_feeding(
    state, ams_id: int, tray_id: int, *, item: PrintQueueItem | None = None, printer_id: int | None = None
) -> bool:
    """Was ``(ams_id, tray_id)`` the active feeder, as of just before now?

    The public verb ``ams_presence`` asks at a presence-LOSS edge (its de-bounce lane's
    runout-suspect stamp). It lives HERE because feeder resolution is this module's, and
    a second resolver in the presence lane would be the drift the fork forbids —
    :func:`_feeder_before_edge` composes the same witnesses, and the comparison rides
    ``spool_respool.decode_global_tray``, the one origin for the global-tray encoding.

    ``printer_id`` unlocks two witnesses that are unreachable from ``state`` alone, and
    both were silently missing before 2026-08-20:

    * the job's SLICER mapping (``_job_feeders`` → ``_slicer_mapping``), which the client
      captures off the request topic — so the contracted order "``last_loaded_tray`` +
      mapping AHEAD of ``tray_now``" only actually had its mapping tier for callers that
      passed an ``item``;
    * the DUAL-NOZZLE per-extruder feeders (:func:`_dual_nozzle_feeders`), asked FIRST
      because a positive there is a direct wire statement about a specific hotend, while
      everything ``_feeder_before_edge`` composes is a single-value approximation of a
      two-value fact.

    Never raises: an unreadable state answers False (not evidence of a feed).
    """
    try:
        for feeder in _dual_nozzle_feeders(state, printer_id):
            if decode_global_tray(feeder) == (ams_id, tray_id):
                return True
        feeder = _feeder_before_edge(state, item=item, printer_id=printer_id)
    except Exception:  # noqa: BLE001 — a predicate for a callback may never raise
        logger.exception("spool_recovery: feeder resolution failed for AMS%d-T%d", ams_id, tray_id)
        return False
    if feeder is None:
        return False
    return decode_global_tray(feeder) == (ams_id, tray_id)


async def _route_fault(
    db: AsyncSession,
    *,
    printer_id: int,
    job_id: str,
    kind: str,
    external: bool,
    verdict: str,
    tray: int | None,
) -> str | None:
    """The escalation reason this fault routes to, or ``None`` to run the machine.

    ONE routing table, shared by the entry gate and the startup re-entry, so a
    restart can never resolve a fault differently from the push that raised it.

    Deliberately BLIND to whether a farm unit is printing here. Origin decides the
    queue-row projections and the retry bookkeeping, never the routing: a
    ``jam + no farm item`` branch used to escalate before any evidence was weighed,
    which made auto-recovery unreachable for most real workload (printer 4's
    2026-08-06 mechanical cascade got no recovery at all). The AMBIGUITY that branch
    was standing in for is still refused — it is just measured now.
    """
    if kind == KIND_PHYSICAL:
        # Hands needed: a swap cannot clear a breakage, a clog or a failed
        # pull-back. Escalated AT ENTRY (status, not just outcome) — before WS2b
        # nothing owned this class at all and it waited on the pause-stall watchdog.
        return "physical_fault"
    if kind != KIND_JAM:
        # A runout holds for a same-slot refill; the driver escalates it after
        # confirming the PAUSE, and the guidance/auto-resume lanes take it from there.
        return None
    if external:
        # A feed fault on the EXTERNAL spool holder. Decided BEFORE any jam-machine
        # question is asked, because every one of them presumes an AMS: there is no
        # tray to resolve (so ``jammed_tray_unresolved`` would be a lie about a fault
        # whose location is perfectly well known), nothing to unload, nothing to swap
        # to, and no spool to take out of rotation. 003-H2S 2026-08-11 asked them all
        # anyway and answered with the printer's own quarantine.
        return "external_feed_fault"
    if verdict == "multi_feeder":
        return "multi_feeder_job"
    if tray is None:
        return "jammed_tray_unresolved"
    if await printer_incidents.count_resolved(db, printer_id, job_id, KIND_JAM) >= _MAX_SUCCESSES_PER_JOB:
        # Flap bound: recovery keeps landing but the fault keeps coming back — an
        # extruder-side problem a swap won't fix.
        return "repeated_jams"
    return None


# --- entry ------------------------------------------------------------------


async def on_ams_fault(printer_id: int, state) -> asyncio.Task | None:
    """Own the AMS faults standing on this printer right now. Never raises.

    Called (guarded, fire-and-forget) from ``main.on_printer_status_change`` on
    EVERY push that carries HMS — deliberately not on the notify dedup's "new
    codes" edge, which is what made the pre-WS2b machine silent for a code standing
    at restart or flapping inside the 600 s re-notify window (9 runout episodes with
    no incident and no log line).

    Gates, in order — the first three are DURABLE, so a restart changes none of them:

    0. actionable candidates exist, and this exact fault was not evaluated moments
       ago (:func:`_eval_throttled` — a standing fault rides ~1 Hz);
    1. the ``spool_recovery_enabled`` setting;
    2. this printer has NO open incident (the partial unique index is the real
       enforcement; the pre-check is what turns the ordinary case into a log line);
    3. this exact ``(printer, job, fingerprint)`` did not already close as ABORTED.
       A RESOLVED close deliberately does NOT bar re-entry — a genuine second tangle
       in one job must still be recovered, and the flap cap below is what bounds it.

    Routing is then :func:`_route_fault`'s alone and follows the fault CLASS and the
    wire EVIDENCE, never the print's origin: a mechanical fault whose jammed feeder
    resolves runs the swap machine, a runout holds for a same-slot refill, and a
    physical fault — or a jam whose feeder is ambiguous — escalates at entry with the
    hold. A farm queue unit only decides what gets PROJECTED onto the queue.

    Returns the spawned driver task (so tests can await it) or ``None`` when the
    incident was gated out or escalated at entry; ``main`` ignores the return.
    """
    try:
        candidates = live_candidates(state)
        if not candidates:
            return None

        job_id = (getattr(state, "subtask_id", None) or "").strip()
        fingerprint = candidate_fingerprint(candidates)
        if _eval_throttled(printer_id, job_id, fingerprint):
            return None

        fault_class = _dominant_class(candidates)
        if fault_class is None:  # pragma: no cover — non-empty candidates always decide
            return None
        kind = _KIND_BY_CLASS[fault_class]
        primary = _primary_candidate(candidates, fault_class)
        # The HARDWARE the deciding fault sits on, taken from the taxonomy's verdict
        # for the very candidate whose code the operator is told about — so the copy,
        # the routing and the message can never name different hardware. When AMS and
        # external faults stand together, ``_primary_candidate``'s lowest-short-code
        # order picks the AMS one (``0700_…`` < ``07FF_…``), which is correct: a real
        # AMS fault beside a holder fault is still an AMS fault to recover.
        external = primary.external if primary is not None else False
        code = primary.short_code if primary is not None else ""

        from backend.app.core.database import async_session
        from backend.app.models.printer import Printer

        incident: RecoveryIncident | None = None
        escalate_reason: str | None = None
        async with async_session() as db:
            settings = await _read_settings(db)
            if not settings.enabled:
                _note_outcome(printer_id, job_id, fingerprint, "not owned — recovery disabled by setting")
                return None

            existing = await printer_incidents.get_open(db, printer_id)
            if existing is not None:
                _note_outcome(
                    printer_id,
                    job_id,
                    fingerprint,
                    "not opened — this printer already has an open incident",
                    detail=f"incident {existing.id} {existing.status} kind={existing.kind}",
                )
                return None

            if await _fault_already_closed(db, printer_id, job_id, fingerprint):
                # Aborted = an external actor took over, or the fault proved transient
                # (it never held the printer). Re-entering would fight the operator, or
                # loop on a code the firmware leaves standing after it healed itself.
                # The wire re-arms it the moment the fault clears or the printer pauses.
                _note_outcome(
                    printer_id,
                    job_id,
                    fingerprint,
                    "not re-entered — this exact fault already closed as aborted on this job",
                )
                return None

            item = await _resolve_farm_item(db, printer_id, job_id)
            tray, verdict = _resolve_fault_tray(
                item, state, kind=kind, external=external, candidates=candidates, printer_id=printer_id
            )
            escalate_reason = await _route_fault(
                db, printer_id=printer_id, job_id=job_id, kind=kind, external=external, verdict=verdict, tray=tray
            )

            row = await printer_incidents.open_new(
                db,
                printer_id=printer_id,
                job_id=job_id,
                item_id=item.id if item is not None else None,
                kind=kind,
                code=code,
                codes=fingerprint,
                slot_global_tray=tray,
                status=STATUS_ESCALATED if escalate_reason is not None else STATUS_RECOVERING,
            )
            if row is None:
                _note_outcome(printer_id, job_id, fingerprint, "not opened — lost the open-incident race")
                return None

            printer = await db.get(Printer, printer_id)
            printer_name = (printer.name if printer else None) or f"printer {printer_id}"
            job_name = (getattr(state, "subtask_name", None) or "").strip() or "print"
            mechanical = {c for c in candidates if c.fault_class is AmsFaultClass.MECHANICAL_FEED}
            incident = RecoveryIncident(
                incident_id=row.id,
                printer_id=printer_id,
                job_id=job_id,
                codes=frozenset(c.short_code for c in candidates),
                fingerprint=fingerprint,
                item_id=item.id if item is not None else None,
                settings=settings,
                jammed_global_tray=tray,
                kind=kind,
                external=external,
                extruder_side_only=bool(mechanical) and all(c.extruder_side for c in mechanical),
                layer_at_fault=int(getattr(state, "layer_num", 0) or 0),
                code=code,
                printer_name=printer_name,
                job_name=job_name,
            )

        _note_outcome(
            printer_id,
            job_id,
            fingerprint,
            f"incident {incident.incident_id} opened",
            detail=f"kind={kind} {'foreign' if incident.item_id is None else f'item={incident.item_id}'}"
            + (f" escalating={escalate_reason}" if escalate_reason else ""),
        )

        # Session closed — escalate/spawn outside it (helpers open their own).
        if escalate_reason is not None:
            await _escalate(incident, escalate_reason)
            return None

        task = asyncio.create_task(_run_recovery(incident))
        _active_tasks[printer_id] = task
        return task
    except Exception:  # noqa: BLE001 — entry hook must never crash the status flow
        logger.exception("spool_recovery: on_ams_fault failed for printer %s", printer_id)
        return None


async def will_own(db: AsyncSession, printer_id: int, state) -> bool:
    """Would an incident own the AMS faults standing on this printer right now?

    (1) It exists so the HMS notify pipeline (main.py) can SUPPRESS the raw per-code
        alert for a fault an incident carries — the incident's lifecycle
        notifications (recovering / succeeded / self-healed / out-of-rotation /
        failed / auto-resumed) are the operator-facing signal, and a duplicate raw
        alert would double-notify (one 2026-07-20 feed fault produced 4 Discord
        messages).
    (2) It MIRRORS :func:`on_ams_fault`'s durable entry gates — including the ones
        that mean "an incident ALREADY owns this", where suppression stays correct
        because the raw alert is the duplicate.
    (3) Foreign prints included: an incident owns EVERY class of their AMS faults —
        runouts and physical faults since WS2b, mechanical ones since the 2026-08-10
        origin-agnostic ruling — so their raw alerts are duplicates in exactly the
        same way.

    Fails toward NOTIFYING: any exception returns False (never suppress a raw alert
    on the strength of a predicate that errored).
    """
    try:
        candidates = live_candidates(state)
        if not candidates:
            return False
        if not await _read_bool(db, "spool_recovery_enabled", _DEFAULT_ENABLED):
            return False
        if await printer_incidents.get_open(db, printer_id) is not None:
            return True
        job_id = (getattr(state, "subtask_id", None) or "").strip()
        # A barred fault will never be owned again — let the raw alert through
        # rather than suppressing into silence.
        return not await _fault_already_closed(db, printer_id, job_id, candidate_fingerprint(candidates))
    except Exception:  # noqa: BLE001 — a suppression predicate must never crash the notify path
        logger.exception("spool_recovery: will_own predicate failed for printer %s", printer_id)
        return False


def owned_short_codes(state) -> frozenset[str]:
    """The short codes an incident speaks for — what the notify lane suppresses.

    Exact rather than "everything recoverable": only the codes THIS push classified
    as actionable are covered by an incident's messages, so an unrelated fault
    standing beside them still raises its own alert.
    """
    return frozenset(c.short_code for c in live_candidates(state))


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

        # (1) The fault PAUSEs the print; a runout the firmware backup rescued never
        #     PAUSEs → close silently as TRANSIENT. Closed as ``aborted`` with no
        #     resolve_source (no actor can claim an outcome nobody produced), which
        #     also bars re-entry for this exact fault on this job: the firmware can
        #     leave the code standing after healing itself, and the per-push entry
        #     gate would otherwise re-open an incident every throttle window.
        if not await printer_manager.await_state(
            pid, {"PAUSE"}, incident.settings.step_timeout_s, poll_interval_s=_POLL_INTERVAL_S
        ):
            await _close_incident(incident, status=STATUS_ABORTED, source=None)
            logger.info("spool_recovery: printer %s never PAUSEd (firmware rescue / transient) — incident closed", pid)
            return

        # (2) Show the operator a live status. Out-of-rotation stamping/notification
        #     is DEFERRED to the swap-commit boundary in the loop below (feed-fault
        #     incidents only — a runout spool is SPENT): a no-swap firmware self-heal
        #     must neither stamp nor announce a spool the print keeps using.
        await _stamp_recovering(incident)

        # A filament RUNOUT escalates IMMEDIATELY — no unload/select/load. Firmware
        # refuses cross-slot ams_change_filament in the 8011 "insert into the SAME
        # slot" state (2026-07-19 incident: 10 cross-slot loads across two printers
        # executed ZERO times while the operator confirmed the target slots held
        # filament), so the whole swap machine is futile here — the fix is a
        # same-slot refill by a human. The feed-fault branch below keeps the proven
        # unload→swap→resume machine (006 recovery #1).
        if not incident.is_feed_fault:
            await _escalate(incident, "external_spool_runout" if incident.external else "runout_needs_refill")
            return

        # (3) Try up to _MAX_CANDIDATES replacement trays. Every round runs a
        #     stuck-change reset first (W1) and then its own unload cycle — the
        #     009-H2S fix: after a feed fault the AMS can sit wedged mid-change,
        #     silently ignoring unloads; only a firmware CONTINUE (resume) frees it,
        #     and a round that follows a failed load must reset the AMS before
        #     loading again.
        tried: set[int] = set()
        evidence = _RecoveryEvidence()
        # The jammed spool is taken out of rotation exactly ONCE, at the swap-commit
        # boundary (a reset that leaves recovery committed to a swap or wedged), never
        # at entry — so a no-swap self-heal leaves the spool untouched.
        oor_stamped = False
        for _round in range(_MAX_CANDIDATES):
            # W1: reset an AMS wedged mid filament-change before touching the unload.
            reset = await _reset_stuck_change(incident, client)
            if reset == "abort":
                await _abort(incident)
                return
            if reset == "fail":
                # The feeder is genuinely wedged — hands are needed and the jammed
                # spool is legitimately out of rotation. Commit the stamp (once) at
                # this boundary, THEN escalate.
                if not oor_stamped and incident.jammed_global_tray is not None:
                    await _mark_out_of_rotation(incident, incident.jammed_global_tray, notify=True)
                    oor_stamped = True
                await _escalate(incident, "stuck_reset_failed")
                return
            if reset == "recovered":
                # Same-feeder self-heal: the firmware reset cleared the jam with no
                # swap. The swap-commit boundary was never reached, so THIS incident
                # stamped nothing; the _clear_oor call below is now a safety net for a
                # flag a PREVIOUS incident on this feeder may have left. Count the
                # self-heal toward the per-job flap cap and close as a no-swap success
                # (which fires the truthful self-heal notification) — never resume/swap
                # on top of a running print.
                from backend.app.core.database import async_session

                async with async_session() as db:
                    await _clear_oor_if_resumed_on_jammed_feeder(db, incident)
                await _succeed(incident, incident.jammed_global_tray, swapped=False)
                return
            # reset in ("skipped", "ok") → the swap is COMMITTED: take the jammed spool
            # out of rotation ONCE, right before the first unload. Boundary semantics:
            # the stamp means "recovery is abandoning this spool", so every escalation
            # that can follow this point (ams_drying, unload_failed, load exhaustion)
            # correctly KEEPS the stamp; only a clean swap-and-resume or an
            # external-takeover abort resolves it (the latter's _clear reverses it when
            # the operator resumed on the jammed feeder).
            if not oor_stamped and incident.jammed_global_tray is not None:
                await _mark_out_of_rotation(incident, incident.jammed_global_tray, notify=True)
                oor_stamped = True
            unload = await _unload_and_confirm(incident, client)
            if unload not in ("ok", "skipped"):
                _log_candidate_outcome(incident, gtid=incident.jammed_global_tray, verdict=f"unload_{unload}")
            if unload == "abort":
                await _abort(incident)
                return
            if unload == "drying":
                await _escalate(incident, "ams_drying")
                return
            if unload == "fail":
                await _escalate(incident, "unload_failed")
                return
            if unload == "ok":
                evidence.confirmed_unloads += 1

            target, only_low = await _select_replacement(incident, tried)
            if target is None:
                # An external RESUME during the (possibly bounded) selection /
                # forced bare-tray sweep means someone took over — abort rather
                # than escalate, mirroring the other steps' abort semantics.
                st = _get_state(pid)
                if st is not None and getattr(st, "state", None) == "RUNNING":
                    await _abort(incident)
                    return
                if only_low:
                    # At/after the protected layers the floor is the hard minimum, so
                    # "only low" there means every match is effectively empty; below
                    # them it means below the ordinary minimum-start weight.
                    reason = (
                        "only_near_empty_spools"
                        if incident.layer_at_fault >= incident.settings.protect_layers
                        else "only_low_spools_in_protected_layers"
                    )
                else:
                    reason = evidence.exhaustion_reason()
                await _escalate(incident, reason)
                return
            tried.add(target)

            evidence.loads_attempted += 1
            load = await _load_and_confirm(incident, client, target)
            if load != "ok":
                _log_candidate_outcome(incident, gtid=target, verdict=f"load_{load}")
            if load == "abort":
                await _abort(incident)
                return
            if load == "drying":
                await _escalate(incident, "ams_drying")
                return
            if load == "fail":
                continue  # load never confirmed — try the next candidate
            evidence.loads_confirmed += 1

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
                await printer_manager.await_state(
                    pid, {"PAUSE"}, incident.settings.step_timeout_s, poll_interval_s=_POLL_INTERVAL_S
                )
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
        _log_candidate_outcome(incident, gtid=None, verdict="candidates_exhausted")
        await _escalate(incident, evidence.exhaustion_reason())
    except Exception:  # noqa: BLE001 — the driver must never crash the event loop
        logger.exception("spool_recovery: recovery driver crashed for printer %s", pid)
    finally:
        _active_tasks.pop(pid, None)


# --- step helpers -----------------------------------------------------------


def _feed_fault_live(state) -> bool:
    """True while a FEED-FAULT HMS code is standing on the live printer state."""
    return bool(_active_recoverable_codes(state) & FEED_FAULT_HMS_CODES)


def _change_completed(state) -> bool:
    """True when the pending filament-change resolved to a real feeding tray —
    ``tray_now`` is a concrete slot (0..253), not the 255 "nothing fed" sentinel."""
    tray = getattr(state, "tray_now", None)
    return tray is not None and 0 <= tray <= 253


async def _reset_stuck_change(incident: RecoveryIncident, client) -> str:
    """Reset an AMS wedged mid filament-change by re-issuing the firmware's own
    CONTINUE (a ``resume``), then read the outcome. Runs at the TOP of every
    candidate round, BEFORE the unload.

    Returns one of:

    ``skipped`` — not applicable this round: the AMS is idle (a normal round, no
        wedge) OR the per-incident reset budget (:data:`_MAX_STUCK_RESETS`) is
        spent. The caller falls through to the unload UNCHANGED — zero behaviour
        change on a healthy AMS.
    ``ok`` — the state machine moved and is back at PAUSE: proceed to the swap
        round. Covers the firmware re-faulting and auto-pausing on its own (a) and
        the hung case (c) where the change never completed so we re-paused it.
    ``recovered`` — the firmware fully self-healed: fault gone, RUNNING stable, and
        the pending change completed (a real tray feeds) or the AMS returned to
        idle. No swap needed — the caller clears the jammed spool's out-of-rotation
        flag and closes the incident as a success.
    ``fail`` — the reset did NOT free the AMS (still wedged at the deadline, or the
        resume send failed): the caller escalates ``stuck_reset_failed``.
    ``abort`` — live state was lost mid-reset (disconnect / external actor).

    Entry gate (else ``skipped``): the wedge must be LIVE — gcode_state PAUSE AND
    ``ams_status_main == 1`` (filament_change). Non-idle alone is NOT a wedge: only
    main=1 is resume-resettable. Wire evidence (009-H2S 2026-07-20): a resume is the
    ONLY verb that unwedged the stuck change after FOUR unloads (recovery's two + the
    operator's two) were silently ignored — it is literally the touchscreen CONTINUE
    for the standing 07008010. Counter-evidence (006-H2S 2026-07-21): an extruder
    ``0300_801E`` fault with ``ams_status_main == 3`` (assist) and the feeder still
    engaged is NOT a stuck change — the unload is accepted immediately, so that round
    skips this reset and runs the unload→swap machine directly.
    """
    pid = incident.printer_id
    key = (pid, incident.job_id)

    if _stuck_resets.get(key, 0) >= _MAX_STUCK_RESETS:
        return "skipped"  # budget spent — one bounded reset per incident

    st = _get_state(pid)
    if st is None:
        return "skipped"
    if getattr(st, "state", None) != "PAUSE":
        return "skipped"
    initial_ams = getattr(st, "ams_status_main", None)
    if initial_ams != AMS_STATUS_FILAMENT_CHANGE:
        # Only main=1 is resume-resettable (009-H2S 2026-07-20 evidence). idle(0),
        # assist(3), identifying(2), calibration(4) and any unknown value are NOT
        # stuck filament-changes — the unload→swap machine owns those rounds and its
        # verbs ARE accepted there (006-H2S 2026-07-21: 0300_801E with the feeder
        # still engaged at ams_status_main=3 — the operator's unload/load/resume all
        # took while this branch had been giving up on a resume that could not help).
        return "skipped"

    # The AMS is wedged mid-change. Spend a reset and re-issue the firmware CONTINUE.
    _stuck_resets[key] = _stuck_resets.get(key, 0) + 1
    logger.info(
        "spool_recovery: printer %s AMS wedged mid filament-change (state=PAUSE ams_status_main=%s tray_now=%s) "
        "— publishing resume to reset the stuck state machine (reset %d/%d)",
        pid,
        initial_ams,
        getattr(st, "tray_now", None),
        _stuck_resets[key],
        _MAX_STUCK_RESETS,
    )
    if not client.resume_print():
        logger.warning(
            "spool_recovery: printer %s reset resume_print send returned False (offline?) — reset failed", pid
        )
        return "fail"

    deadline = _now() + incident.settings.step_timeout_s
    saw_leave = False  # observed the machine leave PAUSE or change ams_status — the (a)/(d) discriminator
    healthy_since: float | None = None
    while _now() < deadline:
        st = _get_state(pid)
        if st is None:
            return "abort"
        state = getattr(st, "state", None)
        ams = getattr(st, "ams_status_main", None)
        if state != "PAUSE" or ams != initial_ams:
            saw_leave = True
        # (b) healthy self-heal — RUNNING, fault clear, change done or AMS idle,
        #     held stable for _POST_RESUME_STABLE_S (mirrors _resume_and_confirm).
        if state == "RUNNING" and not _feed_fault_live(st) and (_change_completed(st) or ams == AMS_STATUS_IDLE):
            if healthy_since is None:
                healthy_since = _now()
            elif _now() - healthy_since >= _POST_RESUME_STABLE_S:
                logger.info(
                    "spool_recovery: printer %s firmware reset self-healed the change — RUNNING stable, fault clear, "
                    "tray_now=%s ams_status_main=%s; no swap needed",
                    pid,
                    getattr(st, "tray_now", None),
                    ams,
                )
                return "recovered"
        else:
            healthy_since = None
            # (a) the firmware re-faulted and auto-paused on its own AFTER moving.
            if state == "PAUSE" and saw_leave:
                logger.info(
                    "spool_recovery: printer %s state machine moved then re-PAUSEd after the reset "
                    "(ams_status_main=%s) — proceeding to the swap round",
                    pid,
                    ams,
                )
                return "ok"
        await asyncio.sleep(_POLL_INTERVAL_S)

    # Deadline reached.
    st = _get_state(pid)
    if st is None:
        return "abort"
    state = getattr(st, "state", None)
    ams = getattr(st, "ams_status_main", None)
    if state == "RUNNING":
        if not _feed_fault_live(st) and (_change_completed(st) or ams == AMS_STATUS_IDLE):
            logger.info(
                "spool_recovery: printer %s firmware reset left the print RUNNING and healthy at the deadline "
                "(tray_now=%s ams_status_main=%s); no swap needed",
                pid,
                getattr(st, "tray_now", None),
                ams,
            )
            return "recovered"
        # (c) THE LIVE 009 HUNG CASE: RUNNING but the change never completed and the
        #     fault stands (it sat like this ~2.5 min). Re-pause it ourselves so the
        #     proven unload→swap→resume round can run.
        logger.info(
            "spool_recovery: printer %s hung RUNNING in an incomplete filament-change after the reset "
            "(tray_now=%s ams_status_main=%s fault_live=%s) — self-pausing to run the swap round",
            pid,
            getattr(st, "tray_now", None),
            ams,
            _feed_fault_live(st),
        )
        if client.pause_print() and await printer_manager.await_state(
            pid, {"PAUSE"}, incident.settings.step_timeout_s, poll_interval_s=_POLL_INTERVAL_S
        ):
            return "ok"
        logger.warning("spool_recovery: printer %s could not re-pause after the reset — escalating", pid)
        return "fail"
    if state == "PAUSE" and saw_leave:
        return "ok"  # re-faulted and re-paused right at the deadline
    # (d) the state machine never moved — still wedged (PAUSE + non-idle) at the
    #     deadline without ever leaving. A resume cannot free it; hands are needed.
    logger.warning(
        "spool_recovery: printer %s AMS never left the wedged change after the reset (state=%s ams_status_main=%s) "
        "— escalating stuck_reset_failed",
        pid,
        state,
        ams,
    )
    return "fail"


def _unload_skippable(state) -> bool:
    """True only for the genuinely-clean "nothing to unload" state.

    ALL THREE must hold: nothing is feeding (``tray_now == 255``), the AMS state
    machine is idle (``ams_status_main == 0``), and no feed-fault code is standing.
    That is the restart / firmware-already-unloaded path the original short-circuit
    was written for.

    Anything else — above all a live feed fault, or an ``ams_status_main`` stuck at
    ``1`` (filament_change) — means an explicit unload is exactly what the AMS needs,
    because after a feed fault ``tray_now == 255`` says only "nothing is feeding".
    """
    if state is None:
        return False
    if getattr(state, "tray_now", None) != _NO_FILAMENT:
        return False
    if getattr(state, "ams_status_main", None) != AMS_STATUS_IDLE:
        return False
    return not _feed_fault_live(state)


def _ams_unit_for_tray(global_tray: int | None) -> int:
    """The AMS unit a global tray belongs to, for the pre-flight refusal check.

    Reuses the module's existing decoder; external / unloaded / unresolvable trays
    have no unit and map to the client's own 255 sentinel, for which the unit-scoped
    drying hazard is vacuously false.
    """
    ams_id, _tray_id = decode_global_tray(global_tray)
    return 255 if ams_id is None else ams_id


async def _wait_ams_write_window(client, ams_id: int) -> str | None:
    """Pre-flight the AMS wire for a recovery load/unload on unit ``ams_id``.

    Returns the refusal reason that STILL stands after giving the wire a chance to
    settle, or None when it is clear. Drying is a doomed lane — it is reported
    immediately so the caller can escalate instead of burning attempts on writes the
    client will refuse for the whole cycle. An identify-contention refusal is
    transient by construction, so the client's own settle wait absorbs it (the same
    idiom the terminal sweep uses) rather than ending the recovery.

    This is advisory only: the client re-evaluates at publish time, which is the
    check that actually closes the race.
    """
    refusal = client.ams_write_refusal(ams_id)
    if refusal is None or refusal == _REFUSAL_DRYING:
        return refusal
    await client.wait_ams_settle()
    return client.ams_write_refusal(ams_id)


async def _unload_and_confirm(incident: RecoveryIncident, client) -> str:
    """Unload the feeder and confirm the AMS finished the cycle.

    ``ok`` (a confirmed unload cycle ran) / ``skipped`` (nothing to unload — see
    :func:`_unload_skippable`) / ``drying`` (the AMS is drying: a doomed lane) /
    ``fail`` / ``abort`` (an external RESUME mid-unload).

    The unload is sent even when ``tray_now`` already reads 255 — the client encodes
    that as ``ams_id/slot_id/target = 255``, which is byte-for-byte the operator's
    proven manual recovery command and the only thing that resets a stuck
    filament-change state machine.
    """
    st = _get_state(incident.printer_id)
    if _unload_skippable(st):
        return "skipped"

    unit = _ams_unit_for_tray(getattr(st, "tray_now", None) if st is not None else None)
    refusal = await _wait_ams_write_window(client, unit)
    if refusal == _REFUSAL_DRYING:
        return "drying"

    for _ in range(max(1, incident.settings.max_attempts)):
        if not client.ams_unload_filament():
            # Offline / refused / rejected send: a no-op that never confirms —
            # consume the attempt and advance immediately instead of burning a full
            # confirm wait.
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
    """Wait for the commanded unload to COMPLETE. ``ok`` / ``timeout`` / ``abort``.

    ``tray_now == 255`` alone is not completion: after a feed fault it already reads
    255 before the unload, so the old criterion returned instantly and the load then
    raced an AMS that was still mid-cycle (009-H2S 2026-07-20). Two evidence paths,
    both bounded by ``step_timeout_s``:

    (a) The AMS is observed going NON-idle — the change cycle started. Completion is
        its return to idle with nothing feeding.
    (b) No non-idle transition is ever observed (command latency, or an unload the
        firmware no-ops). Then idle + empty must hold across consecutive polls for
        :data:`_UNLOAD_GRACE_S` before it counts; any contrary poll restarts the dwell.

    A state machine still stuck non-idle at the deadline returns ``timeout`` — the
    caller resends, and ultimately escalates ``unload_failed`` rather than loading
    into a busy AMS.
    """
    deadline = _now() + incident.settings.step_timeout_s
    saw_cycle = False
    settled_since: float | None = None
    while True:
        st = _get_state(incident.printer_id)
        if st is None:
            return "abort"
        if getattr(st, "state", None) == "RUNNING":
            return "abort"  # someone else resumed the print
        idle = getattr(st, "ams_status_main", None) == AMS_STATUS_IDLE
        empty = getattr(st, "tray_now", None) == _NO_FILAMENT
        if not idle:
            saw_cycle = True  # the change cycle is running
            settled_since = None
        elif not empty:
            settled_since = None  # idle but still feeding — not unloaded
        elif saw_cycle:
            return "ok"  # cycle ran and returned to idle with nothing feeding
        else:
            # No cycle observed yet: idle + empty must HOLD for the grace dwell.
            if settled_since is None:
                settled_since = _now()
            if _now() - settled_since >= _UNLOAD_GRACE_S:
                return "ok"
        if _now() >= deadline:
            return "timeout"
        await asyncio.sleep(_POLL_INTERVAL_S)


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
    # An UNREAD entry is telemetry about PRESENCE, not about material: the builder now
    # emits seated-but-unidentified trays so the dispatch layers stop pricing them as
    # empty, and such an entry carries no type / colour / preset id. Reading a
    # requirement out of it would silently ask the matcher for "" filament and skip the
    # DB fallback that exists for exactly this bare-jammed-tray case.
    jammed = next(
        (f for f in loaded_all if f.get("global_tray_id") == incident.jammed_global_tray and not f.get("unread")),
        None,
    )
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
    ams_id, tray_id = decode_global_tray(incident.jammed_global_tray)
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
    filament requirement — single-feeder farm jobs carry exactly one.

    FARM-ONLY by nature: the tier reads the queue unit's own file, and a foreign
    print has no unit to read it from. Its two predecessors (live jammed telemetry,
    then the tray's bound spool) are origin-agnostic and carry that case.
    """
    if incident.item_id is None:
        return None

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

    def _present(f: dict) -> bool:
        # Drop a candidate whose tray reports an explicit non-present state (e.g.
        # 9 = seated-but-unsensed): a LOAD there is doomed. FAIL OPEN when the state
        # is None/unparseable — dialect variance must never exclude a real candidate.
        #
        # This is the LOAD-VIABILITY reading of the shared rule, deliberately
        # stricter than the release/consumer reading: for a replacement load the
        # tray's residual config must not soften a non-present state code (a
        # configured-but-unsensed tray is precisely the doomed case), so the
        # ``tray_type`` argument is pinned to the asserted-empty ``""``. The state
        # vocabulary and its parse still come from ONE origin
        # (``tray_fields.tray_presence`` / ``TRAY_PRESENT_STATES``), and the
        # tri-state is mapped fail-open with ``is not False`` exactly as before.
        return tray_fields.tray_presence(tray_fields.parse_tray_state(f.get("state")), "") is not False

    loaded_all = scheduler._build_loaded_filaments(status)
    candidates = [
        f
        for f in loaded_all
        if not f.get("is_external")
        and f.get("global_tray_id") != incident.jammed_global_tray
        and f.get("global_tray_id") not in tried
        and _present(f)
    ]
    if not candidates:
        return None, False

    backup_on = getattr(status, "ams_filament_backup", None)
    async with async_session() as db:
        inv = await build_slot_inventory(db, incident.printer_id, candidates)
        base_min = await _read_min_start_g(db)
        policy_setting = await get_setting(db, "spool_selection_policy")

    # The layer rule is a floor PARAMETER, not new floor logic: below the
    # protected-layer threshold a low spool stays a backup donor; at/after it the
    # floor drops to the hard minimum — a low-but-not-empty spool is a valid
    # mid-print replacement, but a known-empty one (≤ _RECOVERY_HARD_MIN_G) never is.
    min_start_g = _RECOVERY_HARD_MIN_G if incident.layer_at_fault >= incident.settings.protect_layers else base_min
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
            # Invariant 1: the codec is the one origin for this arithmetic. It knows the
            # AMS-HT and vt_tray conventions a bare ``ams_id * 4 + tray_id`` drops, and
            # fails CLOSED (``None``) on a slot it cannot name rather than fabricating a
            # label that would compare equal to some real slot in this very line.
            global_tray = encode_global_tray(ams_id, tray_id)
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
    """Load ``target`` until ``tray_now == target``. ``ok`` / ``drying`` / ``fail`` /
    ``abort``.

    The live incident needed two sends before the load took, hence the resend
    loop. A ``pending_tray_target`` that becomes something other than our target
    means another actor issued a load → abort. A drying target unit is a doomed lane
    (the client refuses every write to it) — reported so the caller escalates instead
    of burning attempts.
    """
    from backend.app.services import spool_respool

    refusal = await _wait_ams_write_window(client, _ams_unit_for_tray(target))
    if refusal == _REFUSAL_DRYING:
        return "drying"

    for _ in range(max(1, incident.settings.max_attempts)):
        # Mark every load send as ours BEFORE it goes out, so the backup-swap
        # detector suppresses the resulting tray_now edge instead of spending the
        # departed spool (the 006 self-inflicted false-stamp mode).
        spool_respool.note_commanded_load(incident.printer_id, target)
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
    """Project the live incident onto the farm queue unit. No-op on a foreign print
    (no unit to project onto — the incident row IS the state there)."""
    from backend.app.core.database import async_session

    if incident.item_id is None:
        return
    try:
        async with async_session() as db:
            item = await db.get(PrintQueueItem, incident.item_id)
            if item is not None:
                item.waiting_reason = WAITING_REASON_RECOVERING
                await db.commit()
    except Exception:  # noqa: BLE001 — a status stamp must not crash recovery
        logger.exception("spool_recovery: stamp recovering failed for printer %s", incident.printer_id)


async def _close_incident(incident: RecoveryIncident, *, status: str, source: str | None) -> None:
    """Close the incident row. Best-effort — a bookkeeping failure must never turn a
    finished recovery into a crash, and the startup sweep re-resolves a row left
    open by one."""
    from backend.app.core.database import async_session

    if status == STATUS_ABORTED:
        # Bar re-entry for this exact fault until the wire re-arms it (see _blocked).
        # Stamped BEFORE the await so a DB failure cannot leave the loop unbounded.
        _blocked.setdefault((incident.printer_id, incident.job_id), set()).add(incident.fingerprint)
    try:
        async with async_session() as db:
            await printer_incidents.close(db, incident.incident_id, status=status, source=source)
    except Exception:  # noqa: BLE001 — never crash the driver on bookkeeping
        logger.exception(
            "spool_recovery: closing incident %s failed for printer %s", incident.incident_id, incident.printer_id
        )


async def _mark_out_of_rotation(incident: RecoveryIncident, global_tray: int, *, notify: bool) -> None:
    """Stamp ``feed_fault_at``/``feed_fault_code`` on the spool bound to
    ``global_tray`` (unbound slot → proceed anyway), broadcast inventory_changed,
    and optionally fire the out-of-rotation notification."""
    from backend.app.core.database import async_session
    from backend.app.core.websocket import ws_manager
    from backend.app.models.printer import Printer
    from backend.app.services.notification_service import notification_service

    ams_id, tray_id = decode_global_tray(global_tray)
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
    ams_id, tray_id = decode_global_tray(global_tray)
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


async def _succeed(incident: RecoveryIncident, target: int, *, swapped: bool = True) -> None:
    """Recovery landed: close the incident RESOLVED and clear the hold projection.

    Closing re-arms this fault for the job (a genuine second tangle must still be
    handled) and counts toward the per-job flap cap — the bookkeeping is identical
    whether or not a swap happened.

    ``swapped`` (default True) is the ordinary jammed → replacement swap: rewrite
    the item's ams_mapping and fire the ``spool_recovery_succeeded`` notification
    (its copy is swap-and-out-of-rotation framed). ``swapped=False`` is the W1
    no-swap self-heal (a firmware reset freed the wedged change on the SAME feeder,
    ``target == jammed``): the mapping is unchanged and, because the swap-framed
    template would falsely claim both a swap and an out-of-rotation donor, the
    dedicated ``spool_recovery_self_healed`` notification is sent instead — truthful
    "cleared on the same spool, still printing, no action needed" copy.
    """
    from backend.app.core.database import async_session
    from backend.app.models.printer import Printer
    from backend.app.services.notification_service import notification_service

    # The durable close IS the bookkeeping: a RESOLVED incident re-arms this fault
    # for the job (a genuine second tangle must still be recovered) and counts toward
    # the per-job flap cap, which ``printer_incidents.count_resolved`` reads back.
    # ``observed_running`` is the literal evidence: _resume_and_confirm watched the
    # printer reach RUNNING and hold it.
    await _close_incident(incident, status=STATUS_RESOLVED, source=RESOLVE_OBSERVED_RUNNING)

    try:
        async with async_session() as db:
            item = await db.get(PrintQueueItem, incident.item_id) if incident.item_id is not None else None
            if item is not None:
                item.waiting_reason = None
                if swapped:
                    item.ams_mapping = _rewrite_mapping(item.ams_mapping, incident.jammed_global_tray, target)
                await db.commit()
            if swapped:
                from_desc = await _describe_slot(db, incident.printer_id, incident.jammed_global_tray)
                to_desc = await _describe_slot(db, incident.printer_id, target)
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
            else:
                # No-swap self-heal: nothing was swapped and nothing is out of
                # rotation, so send the truthful self-heal alert (the swap-framed
                # succeeded copy would be false). slot_desc mirrors _mark_out_of_
                # rotation's "AMS{ams_id} slot {tray_id}" format; a null jammed tray
                # falls back to a safe generic.
                jammed = incident.jammed_global_tray
                if jammed is None:
                    slot_desc = "the same slot"
                else:
                    ams_id, tray_id = decode_global_tray(jammed)
                    slot_desc = f"AMS{ams_id} slot {tray_id}" if ams_id is not None else f"tray {jammed}"
                spool_desc = await _describe_slot(db, incident.printer_id, jammed)
                printer = await db.get(Printer, incident.printer_id)
                printer_name = (printer.name if printer else None) or f"printer {incident.printer_id}"
                try:
                    await notification_service.on_spool_recovery_self_healed(
                        printer_id=incident.printer_id,
                        printer_name=printer_name,
                        job_name=incident.job_name,
                        layer=incident.layer_at_fault,
                        spool_desc=spool_desc,
                        slot_desc=slot_desc,
                        code=incident.code,
                        db=db,
                    )
                except Exception:  # noqa: BLE001 — notification failure is non-fatal
                    logger.exception(
                        "spool_recovery: self-heal notification failed for printer %s", incident.printer_id
                    )
        if swapped:
            logger.info(
                "spool_recovery: printer %s RECOVERED at layer %s — swapped %s → %s and resumed",
                incident.printer_id,
                incident.layer_at_fault,
                incident.jammed_global_tray,
                target,
            )
        else:
            logger.info(
                "spool_recovery: printer %s RECOVERED at layer %s — firmware reset self-healed the wedged change "
                "on feeder %s (no swap)",
                incident.printer_id,
                incident.layer_at_fault,
                incident.jammed_global_tray,
            )
    except Exception:  # noqa: BLE001 — never crash the driver
        logger.exception("spool_recovery: succeed handler failed for printer %s", incident.printer_id)


async def _escalate(incident: RecoveryIncident, reason: str) -> None:
    """Give up: hold the incident ESCALATED, project the token, notify, leave PAUSED.

    NEVER resumes — a human must intervene. The incident stays OPEN (an escalation is
    a live hold, not a closed fault), which is what makes the printer un-re-enterable
    by a sibling code, keeps the printer-card chip lit, and arms the hourly attention
    reminder. It closes when the printer is observed RUNNING again, at the job's
    terminal, or when the refill auto-resume lands — never by a timer.

    A HELD AMS runout additionally carries the DURABLE spent stamp for the exhausted
    roll (``spool_respool.mark_spent_on_runout_hold``, guarded) — see the call below for
    why an escalation, and not a wire edge, is what a hold spanning a deploy can rely on.
    """
    from backend.app.core.database import async_session
    from backend.app.models.printer import Printer
    from backend.app.services.notification_service import notification_service

    # Per-tray diagnostic snapshot on every escalation (the 18:45 forensics gap).
    await _log_tray_snapshot(incident)

    detail = _ESCALATE_DETAIL.get(reason, reason)
    # The hold's projection onto the farm unit, one token per kind. A foreign print
    # has no unit — the incident row carries the whole state there.
    token = waiting_reason_for(incident.kind, external=incident.external)
    slot_hint = "the external spool holder" if incident.external else runout_slot_desc(incident.jammed_global_tray)
    try:
        async with async_session() as db:
            # Hold FIRST — even if the stamp/notify below fails, the incident must
            # already read ESCALATED so nothing restarts recovery behind the operator.
            await printer_incidents.mark_escalated(db, incident.incident_id)
            item = await db.get(PrintQueueItem, incident.item_id) if incident.item_id is not None else None
            if item is not None:
                item.waiting_reason = token
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
                    kind=incident.kind,
                    runout_slot=slot_hint,
                    foreign=incident.item_id is None,
                )
            except Exception:  # noqa: BLE001 — notification failure is non-fatal
                logger.exception("spool_recovery: failed notification error for printer %s", incident.printer_id)

            # W2: durably record this escalation and quarantine the printer if its
            # AMS keeps escalating within the window (reuse the SAME session).
            await _record_escalation_and_maybe_quarantine(db, incident, reason)
        logger.warning("spool_recovery: printer %s ESCALATED (%s) — left PAUSED", incident.printer_id, reason)
        # A HELD runout's escalation IS the durable exhaustion record. The three
        # edge-driven spent lanes hang off `hms_edges` appearance edges, which every
        # restart re-seeds, so a hold that spans a deploy (or a standing PAUSE code
        # re-escalated at boot) would otherwise never stamp. This escalation derives
        # from the LIVE HMS list on a durable incident row, so it always fires — and
        # STAMPING stays spool_respool's job (one writer for spent_at, invariant 1).
        #
        # Gated on the REASON, which excludes both siblings deliberately:
        # `external_spool_runout` because the holder's left/right vt-tray attribution
        # convention is unconfirmed (a wrong-side stamp on a dual-holder model is
        # permanent), and `recovery_interrupted` because it is a restart artifact with
        # no live runout evidence behind it. The kind check pins the pairing so a future
        # reason rename cannot quietly point this at a different fault class.
        if incident.kind == KIND_RUNOUT and reason == "runout_needs_refill":
            try:
                await spool_respool.mark_spent_on_runout_hold(
                    incident.printer_id,
                    _get_state(incident.printer_id),
                    subtask_id=incident.job_id,
                )
            except Exception:  # noqa: BLE001 — an escalation must never fail because a stamp did
                logger.warning(
                    "spool_recovery: runout-hold spent stamp failed for printer %s", incident.printer_id, exc_info=True
                )
    except Exception:  # noqa: BLE001 — never crash the driver
        logger.exception("spool_recovery: escalate handler failed for printer %s", incident.printer_id)


async def _record_escalation_and_maybe_quarantine(db: AsyncSession, incident: RecoveryIncident, reason: str) -> None:
    """Record one durable ``recovery_escalation`` row, then quarantine the printer
    when its JAM-MACHINE escalations have crossed :data:`_JAM_QUARANTINE_THRESHOLD`
    within :data:`_JAM_QUARANTINE_WINDOW_H` hours — a recurring AMS jam is hardware
    (buffer / feeder), not a spool the swap machine can fix. Counting from the
    durable ledger survives the restarts the in-memory latch cannot.

    EVERY escalation records its row: the ledger is the forensic record of every
    give-up and stays complete. The COUNT reads only :data:`_JAM_QUARANTINE_REASONS`,
    and the trigger additionally requires THIS escalation to be one of them — so a
    printer is quarantined for repeated jams only by repeated jams, and an
    allowlisted row can never be tipped over the threshold by unrelated history.
    003-H2S 2026-08-11: a 05:49 filament runout and a 21:45 external-spool fault made
    "Repeated AMS jam escalations (2 in 24h) — AMS hardware suspected" about a
    printer whose AMS had not been part of either fault.

    Called from :func:`_escalate` only — an operator takeover (:func:`_abort`)
    deliberately records nothing. Best-effort: any failure here must NOT break the
    escalation that called it (the printer is already left PAUSED regardless).
    ``farm_policy`` is lazy-imported (function-level service import, the module's
    idiom, and it keeps the quarantine path off the import graph).
    """
    from datetime import timedelta

    from sqlalchemy import func as sa_func

    from backend.app.models.recovery_escalation import RecoveryEscalation
    from backend.app.services import farm_policy

    try:
        now = datetime.utcnow()
        db.add(
            RecoveryEscalation(
                printer_id=incident.printer_id,
                created_at=now,
                reason=reason,
                code=incident.code or None,
            )
        )
        await db.commit()

        if reason not in _JAM_QUARANTINE_REASONS:
            # Recorded, never counted — and it cannot tip an earlier jam over the
            # threshold either, since the trigger is THIS escalation's own reason.
            return

        window_start = now - timedelta(hours=_JAM_QUARANTINE_WINDOW_H)
        count = int(
            await db.scalar(
                select(sa_func.count())
                .select_from(RecoveryEscalation)
                .where(RecoveryEscalation.printer_id == incident.printer_id)
                .where(RecoveryEscalation.created_at >= window_start)
                .where(RecoveryEscalation.reason.in_(_JAM_QUARANTINE_REASONS))
            )
            or 0
        )
        if count >= _JAM_QUARANTINE_THRESHOLD:
            q_reason = (
                f"Repeated AMS jam escalations ({count} in {_JAM_QUARANTINE_WINDOW_H}h) — AMS hardware "
                "suspected (buffer/feeder). Inspect the filament path, then Recover & resume."
            )
            await farm_policy.quarantine_printer(db, incident.printer_id, q_reason, failure_count=count)
    except Exception:  # noqa: BLE001 — quarantine bookkeeping must never break the escalation
        logger.exception("spool_recovery: repeat-jam quarantine bookkeeping failed for printer %s", incident.printer_id)


async def _abort(incident: RecoveryIncident) -> None:
    """Silent abort — an external actor took over mid-recovery. Stop acting and
    drop our stale ``recovering`` flag (the print is being handled elsewhere).

    If that actor resumed ON the jammed feeder (live RUNNING with ``tray_now`` ==
    the jammed global tray), they declared that spool usable — clear its
    out-of-rotation flag the same way a physical re-insert would, so a self-cleared
    jam does not leave the spool excluded from all future dispatch. Any other live
    state keeps the flag (a physical reseat stays the canonical clear)."""
    from backend.app.core.database import async_session

    # Close FIRST (before any await that could fail): an external actor owns this
    # printer now — an ABORTED close both frees the open slot and bars re-entry for
    # this exact fault on this job, so a sibling code cannot restart recovery under
    # them. ``operator`` is the source: an external actor is the only thing that
    # reaches this path.
    await _close_incident(incident, status=STATUS_ABORTED, source=RESOLVE_OPERATOR)

    logger.info("spool_recovery: printer %s recovery aborted (external interference)", incident.printer_id)
    try:
        async with async_session() as db:
            item = await db.get(PrintQueueItem, incident.item_id) if incident.item_id is not None else None
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
    ams_id, tray_id = decode_global_tray(jammed)
    if ams_id is None or tray_id is None:
        return
    tray = _live_tray_dict(st, ams_id, tray_id) or {}
    try:
        await _clear_out_of_rotation_for_slot(db, incident.printer_id, ams_id, tray_id, tray)
    except Exception:  # noqa: BLE001 — best-effort; abort must never raise
        logger.exception("spool_recovery: self-resume out-of-rotation clear failed for printer %s", incident.printer_id)


# --- hold lifecycle: the incident closes on ANY resume, from any source -------
# Pre-WS2b the ONLY thing that could clear a runout hold was the farm's own
# auto-resume: an operator who walked to the printer and pressed Resume left
# ``waiting_reason="filament_runout_recovery_failed"`` on the unit forever, and the
# hourly attention reminder kept nagging about a print that had been running for
# hours. The lifecycle below is source-AGNOSTIC — it reacts to the printer running
# again, however that happened.


def _clearable(reason: str | None) -> bool:
    """May this hold token be cleared by an incident close?

    Only the tokens this module owns (:data:`RECOVERY_WAITING_REASONS`). A unit
    holding for a plate-vision trip or a filament deficit keeps its own reason — a
    resume observed on the wire says nothing about those.
    """
    return reason in RECOVERY_WAITING_REASONS


async def _clear_hold_projection(db: AsyncSession, item_id: int | None) -> bool:
    """Drop the incident's ``waiting_reason`` projection from a farm unit."""
    if item_id is None:
        return False
    item = await db.get(PrintQueueItem, item_id)
    if item is None or not _clearable(item.waiting_reason):
        return False
    item.waiting_reason = None
    await db.commit()
    return True


async def on_observed_running(printer_id: int) -> bool:
    """The printer is RUNNING again — close whatever incident it was holding.

    Called (guarded) from the per-push wire sampler's transition into RUNNING, which
    covers EVERY resume source: the farm's own auto-resume, an operator pressing
    Resume on the touchscreen, a UI resume, or the firmware recovering by itself.
    That breadth is the point — the pre-WS2b hold could only be cleared by the one
    path that set it.

    Returns True when an incident was closed. Never raises (invariant 10).
    """
    try:
        from backend.app.core.database import async_session

        async with async_session() as db:
            incident = await printer_incidents.get_open(db, printer_id)
            if incident is None:
                return False
            item_id = incident.item_id
            kind, status = incident.kind, incident.status
            await printer_incidents.close(db, incident.id, status=STATUS_RESOLVED, source=RESOLVE_OBSERVED_RUNNING)
            cleared = await _clear_hold_projection(db, item_id)
        logger.info(
            "spool_recovery: printer %s observed RUNNING — %s incident closed (was %s)%s",
            printer_id,
            kind,
            status,
            "; hold token cleared" if cleared else "",
        )
        return True
    except Exception:  # noqa: BLE001 — a lifecycle hook must never crash the status flow
        logger.exception("spool_recovery: observed-running close failed for printer %s", printer_id)
        return False


async def on_job_terminal(printer_id: int) -> bool:
    """The print on this printer reached a terminal — close any open incident.

    A fault cannot outlive the job it interrupted: whatever the outcome (completed,
    failed, cancelled), the hold is over and the next print must not inherit it. The
    farm unit's own ``waiting_reason`` hygiene is ``farm_policy.on_terminal``'s job
    (W4b) — this only closes the incident, so the two never fight over one row.

    ONE exception, by kind: an incident whose ``RESOLVES_ON`` is ``"operator"`` stays
    open. Those kinds are held on a PHYSICAL fact a print ending cannot change — a part
    still on the plate after a confirmed plate-check trip, a Z datum destroyed by a
    reboot — and the terminal is frequently the farm's OWN stop of that very print, so
    closing here would let the hold close itself seconds after being raised and take the
    chip dark with the plate still occupied. They close when the human acts
    (``clear_plate`` / operator recover).

    Called (guarded) from ``main.on_print_complete``'s per-print reset block.
    """
    try:
        from backend.app.core.database import async_session

        async with async_session() as db:
            open_incident = await printer_incidents.get_open(db, printer_id)
            if open_incident is None:
                return False
            if printer_incidents.resolves_on_operator(open_incident.kind):
                logger.info(
                    "spool_recovery: printer %s reached a terminal — %s incident %s left OPEN "
                    "(resolves on an operator act, not on a job ending)",
                    printer_id,
                    open_incident.kind,
                    open_incident.id,
                )
                return False
            closed = await printer_incidents.close(
                db, open_incident.id, status=STATUS_RESOLVED, source=RESOLVE_TERMINAL
            )
        if closed is None:
            return False
        logger.info(
            "spool_recovery: printer %s reached a terminal — %s incident %s closed",
            printer_id,
            closed.kind,
            closed.id,
        )
        return True
    except Exception:  # noqa: BLE001 — a lifecycle hook must never crash the completion flow
        logger.exception("spool_recovery: terminal close failed for printer %s", printer_id)
        return False


def _hold_over(incident, state) -> tuple[bool, str]:
    """Does the printer's LIVE STATE say this incident's hold is over?

    ONE predicate, two occasions — the startup rearm (:func:`rearm_incidents_on_startup`)
    and the per-tick sweep (:func:`sweep_open_incidents`). They ask the identical
    question about the identical evidence, and before this was extracted the rearm
    was the only place that could answer it at all, which is why a hold whose fault
    cleared while the process was UP had no close path (001-H2S incident #60,
    2026-08-29: escalated 01:25, printer clean and IDLE by 15:30, still holding at
    16:00 and blocking every future incident on that printer).

    True only for a POSITIVE non-PAUSE state. ``""`` / ``UNKNOWN`` are not evidence
    of anything (the printer has not reported), and ``PAUSE`` is the hold itself.
    Returns ``(verdict, live state as reported)`` — the second element is what both
    callers log, so the sentence they print names the same reading the verdict used.

    Pure and DB-free; ``incident`` is taken so the call sites read as a question
    about THIS hold rather than about the printer in the abstract.
    """
    live = (getattr(state, "state", None) or "") if state is not None else ""
    return bool(live) and live.upper() not in ("", "UNKNOWN", "PAUSE"), live


async def sweep_open_incidents(*, now: float | None = None) -> int:
    """Close ESCALATED incidents the WIRE says are over. Returns the count closed.

    The missing lifecycle path. An incident closes when the printer is seen RUNNING
    (:func:`on_observed_running`), when its job reaches a terminal
    (:func:`on_job_terminal`), when the refill auto-resume lands, or when a restart's
    rearm finds the printer positive. Nothing closed an incident because its FAULT
    CLEARED — so an operator who cleared a jam on an idle printer left the hold
    standing until the next print happened to run (the 2026-08-19 fleet event: seven
    printers held up to 426 min each), or forever when there was no next print,
    because the hold is what blocks dispatch (001-H2S #60).

    SIX guards, every one required, evaluated in this order:

    0. The kind resolves on the WIRE (``printer_incidents.resolves_on_operator`` False).
       An operator-resolved kind is held on a fact the wire never reports, so the
       "clean and idle" evidence below is its normal reading, not its clearance.
    1. ``escalated`` only. A ``recovering`` row has a live driver — or, after a
       restart, ``_reenter_recovering_incident``'s own lane — and closing it from
       underneath would race the machine that is acting on it.
    2. CONNECTED, with a live state. A cached state read during a disconnect is a
       memory, not evidence; the printer must be speaking to us now.
    3. :func:`_hold_over` — a positive non-PAUSE live state.
    4. :func:`live_candidates` EMPTY. NOT "the incident's own code is gone": the
       incident that motivated this carried the pair ``0700_8006`` + ``0700_0006``,
       and a per-code test would have closed the hold with the sibling fault still
       standing. Whole-printer, through the ONE taxonomy classifier the entry gate
       uses (invariant 1 — never a new HMS frozenset). A physical fault still live on
       an IDLE printer is a REAL hold and keeps holding.
    5. Dwell — all four held continuously for :data:`_HOLD_OVER_DWELL_S`. The fault
       that opened #60 was evaluated 78 ms after a dispatch, when the printer read
       non-PAUSE for an instant; a level-triggered close with no dwell would make the
       same mistake in the opposite direction.

    Guarded end to end and per incident: this runs from the scheduler tick and must
    never kill it (invariant 10).
    """
    now = _monotonic() if now is None else now
    closed = 0
    try:
        from backend.app.core.database import async_session

        async with async_session() as db:
            open_rows = await printer_incidents.all_open(db)
            live_ids = {inc.id for inc in open_rows}
            for stale in [iid for iid in _hold_over_since if iid not in live_ids]:
                _hold_over_since.pop(stale, None)

            for incident in open_rows:
                try:
                    if incident.status != STATUS_ESCALATED:
                        _hold_over_since.pop(incident.id, None)
                        continue
                    if printer_incidents.resolves_on_operator(incident.kind):
                        # Guard 0 (kind): the wire cannot answer this hold at all. A
                        # part on the plate and a lost Z datum are physical facts no HMS
                        # list reports, so "IDLE with no actionable fault" — the very
                        # shape the four guards below look for — is the NORMAL reading
                        # of a printer holding for exactly those reasons. Closing on it
                        # would take the chip dark with the plate still occupied.
                        _hold_over_since.pop(incident.id, None)
                        continue
                    pid = incident.printer_id
                    state = _get_state(pid) if printer_manager.is_connected(pid) else None
                    over, live = _hold_over(incident, state)
                    faults = live_candidates(state) if over else frozenset()
                    if not over or faults:
                        _hold_over_since.pop(incident.id, None)
                        continue

                    first = _hold_over_since.get(incident.id)
                    if first is None:
                        _hold_over_since[incident.id] = now
                        continue
                    if now - first < _HOLD_OVER_DWELL_S:
                        continue

                    await printer_incidents.close(db, incident.id, status=STATUS_RESOLVED, source=RESOLVE_WIRE_CLEAR)
                    cleared = await _clear_hold_projection(db, incident.item_id)
                    _hold_over_since.pop(incident.id, None)
                    closed += 1
                    logger.info(
                        "spool_recovery: printer %s incident %s (%s) closed — wire clear "
                        "(state=%s, no actionable fault for %.0fs)%s",
                        pid,
                        incident.id,
                        incident.kind,
                        live,
                        now - first,
                        "; hold token cleared" if cleared else "",
                    )
                except Exception:  # noqa: BLE001 — one bad incident must not abort the sweep
                    logger.exception("spool_recovery: wire-clear sweep failed for incident %s", incident.id)
    except Exception:  # noqa: BLE001 — a scheduler-tick watch must never kill the tick
        logger.exception("spool_recovery: wire-clear incident sweep failed")
    return closed


async def rearm_incidents_on_startup() -> int:
    """Reconcile incidents left open by a restart, then rehydrate the chip cache.

    An incident is a hold on a PHYSICAL printer, so the restart itself proves
    nothing about it — the printer may have been resumed, finished, or still be
    sitting PAUSEd exactly as we left it. Three outcomes, evidence-led:

    * live state is a positive NON-PAUSE (RUNNING / FINISH / IDLE …) → the hold is
      over; close it ``observed_running``. Without this a stale open row would block
      every future incident on that printer (one-open-per-printer). The reading is
      :func:`_hold_over`, shared with the per-tick :func:`sweep_open_incidents` — one
      predicate, two occasions, so the restart and the running process can never
      disagree about what "the hold is over" means. The startup occasion deliberately
      needs no fault-liveness guard and no dwell: a restart re-derives every wire
      fact from scratch, and a printer already running has answered the question;
    * live state is PAUSE → leave it OPEN. The hold is real; the hourly attention
      reminder re-arms off the incident and nags until a human clears it;
    * no live state at all (the printer has not reported yet) → leave it OPEN and
      decide later. The wire sampler closes it on the first RUNNING transition.

    A row still reading ``recovering`` is the one shape none of those three answers
    fits, and it is handled separately (:func:`_reenter_recovering_incident`): that
    status is a PROMISE that a task is acting, and the restart broke it. Left alone
    the row would sit open forever with nothing driving it — and because exclusivity
    is one-open-per-printer, it would also block every future incident on that
    printer for good.

    Returns the number of incidents closed. Never raises — startup must not block.
    """
    closed = 0
    try:
        from backend.app.core.database import async_session

        driverless: list[tuple[int, int]] = []
        async with async_session() as db:
            for incident in await printer_incidents.all_open(db):
                st = _get_state(incident.printer_id)
                over, live = _hold_over(incident, st)
                if not over:
                    if incident.status == STATUS_RECOVERING:
                        driverless.append((incident.id, incident.printer_id))
                    continue
                await printer_incidents.close(db, incident.id, status=STATUS_RESOLVED, source=RESOLVE_OBSERVED_RUNNING)
                await _clear_hold_projection(db, incident.item_id)
                closed += 1
                logger.info(
                    "spool_recovery: startup — printer %s incident %s (%s) closed, printer is %s not PAUSE",
                    incident.printer_id,
                    incident.id,
                    incident.kind,
                    live,
                )
            open_now = await printer_incidents.rehydrate(db)
        # After the cache rebuild, so a re-entry's own escalate/close writes are the
        # last word on the projection rather than being overwritten by it.
        for incident_id, printer_id in driverless:
            await _reenter_recovering_incident(incident_id, printer_id)
        if closed or open_now:
            logger.info(
                "spool_recovery: startup incident sweep — %d closed, %d still held, %d re-entered",
                closed,
                open_now,
                len(driverless),
            )
    except Exception:  # noqa: BLE001 — startup hygiene must never block the lifespan
        logger.exception("spool_recovery: startup incident sweep failed")
    return closed


async def _reenter_recovering_incident(incident_id: int, printer_id: int) -> asyncio.Task | None:
    """Give a driver back to an incident a restart left mid-swap. Never raises.

    Re-entry is decided by the WIRE, exactly as the entry gate is: the live HMS set
    is re-classified through the taxonomy and routed through :func:`_route_fault`, so
    the fault either goes back to the swap machine or becomes an escalated hold that
    a human, a RUNNING transition or the job's terminal can clear. The one outcome
    that is not available is the one that used to happen — an open ``recovering`` row
    with no task behind it.

    A wire with NO actionable fault left is still not "fine": the printer is PAUSEd
    (or has not reported) with a swap half-executed — possibly with the jammed feeder
    unloaded and nothing loaded in its place — so it escalates rather than closing.
    The lifecycle closes it for free the moment the printer is seen RUNNING.
    """
    try:
        state = _get_state(printer_id)
        candidates = live_candidates(state)

        from backend.app.core.database import async_session
        from backend.app.models.printer import Printer

        async with async_session() as db:
            row = await printer_incidents.get_open(db, printer_id)
            if row is None or row.id != incident_id or row.status != STATUS_RECOVERING:
                return None  # another lane resolved it between the sweep and here
            settings = await _read_settings(db)
            job_id = (getattr(state, "subtask_id", None) or "").strip() or row.job_id
            item = await _resolve_farm_item(db, printer_id, job_id)
            fault_class = _dominant_class(candidates)
            if fault_class is None:
                kind, external = row.kind, False
                code, fingerprint = row.code, row.codes
                tray = row.slot_global_tray
                escalate_reason = "recovery_interrupted"
            else:
                kind = _KIND_BY_CLASS[fault_class]
                primary = _primary_candidate(candidates, fault_class)
                external = primary.external if primary is not None else False
                code = primary.short_code if primary is not None else row.code
                # The LIVE fingerprint, not the stored one: it is what an aborted
                # close must bar and what the wire sampler re-arms, and the two would
                # loop against each other if they named different faults.
                fingerprint = candidate_fingerprint(candidates)
                tray, verdict = _resolve_fault_tray(
                    item, state, kind=kind, external=external, candidates=candidates, printer_id=printer_id
                )
                escalate_reason = await _route_fault(
                    db, printer_id=printer_id, job_id=job_id, kind=kind, external=external, verdict=verdict, tray=tray
                )
            printer = await db.get(Printer, printer_id)
            printer_name = (printer.name if printer else None) or f"printer {printer_id}"
            mechanical = {c for c in candidates if c.fault_class is AmsFaultClass.MECHANICAL_FEED}
            incident = RecoveryIncident(
                incident_id=incident_id,
                printer_id=printer_id,
                job_id=job_id,
                codes=frozenset(c.short_code for c in candidates) or frozenset({row.code}),
                fingerprint=fingerprint,
                item_id=item.id if item is not None else None,
                settings=settings,
                jammed_global_tray=tray,
                kind=kind,
                external=external,
                extruder_side_only=bool(mechanical) and all(c.extruder_side for c in mechanical),
                layer_at_fault=int(getattr(state, "layer_num", 0) or 0),
                code=code,
                printer_name=printer_name,
                job_name=(getattr(state, "subtask_name", None) or "").strip() or "print",
            )

        logger.info(
            "spool_recovery: startup — printer %s incident %s was left mid-swap, re-entering (%s)",
            printer_id,
            incident_id,
            f"escalating={escalate_reason}" if escalate_reason else f"kind={kind} tray={tray}",
        )
        if escalate_reason is not None:
            await _escalate(incident, escalate_reason)
            return None
        task = asyncio.create_task(_run_recovery(incident))
        _active_tasks[printer_id] = task
        return task
    except Exception:  # noqa: BLE001 — startup hygiene must never block the lifespan
        logger.exception("spool_recovery: re-entering incident %s failed for printer %s", incident_id, printer_id)
        return None


# --- runout guidance + refill auto-resume (006-H2S 2026-07-26) --------------
# Both lanes below are GUIDANCE/ASSIST layered on top of an ALREADY ESCALATED
# runout. Neither re-enters the swap machine: a runout escalates for a same-slot
# refill (doctrine invariant 9), and that verdict stands. They only stop the
# operator being told the wrong slot, and stop a correctly-refilled printer sitting
# PAUSEd waiting for a button press nobody is there to give.


async def _open_runout_incident(db: AsyncSession, printer_id: int):
    """The printer's OPEN runout incident, or None.

    The single shared gate for both lanes below — and the WS2b widening of them:
    the pre-WS2b gate was "a still-``printing`` FARM unit holding
    WAITING_REASON_RUNOUT", so a foreign print's runout could be neither re-guided
    nor auto-resumed however clearly the wire said what it needed.
    """
    incident = await printer_incidents.get_open(db, printer_id)
    if incident is None or incident.kind != KIND_RUNOUT:
        return None
    return incident


async def maybe_refresh_runout_guidance(printer_id: int, new_full_codes, state) -> bool:
    """Re-announce the runout escalation when the firmware's DEMAND MOVES to a new slot.

    006-H2S 2026-07-26: after the escalation, a fresh slot-attributed runout arrived
    on a DIFFERENT slot while the printer sat held. The recovery latch correctly
    suppressed a second recovery attempt — but it also suppressed every trace of the
    change, so the operator's only guidance stayed the original (and, per F2, wrong)
    slot for 12 h. This lane closes that gap: guidance-only, it re-fires the
    escalation's OWN ``on_spool_recovery_failed`` event carrying the FRESH slot. The
    incident is deliberately left ESCALATED — recovery has given up on this fault and
    must stay given-up.

    Fires only when a NEW code this push is demand-family AND this printer holds an
    open runout incident. Deduped per ``(incident, global_tray)`` so one demand move
    notifies once no matter how many pushes carry it, while a LATER move to another
    slot announces again. Returns True when it notified.

    Called (guarded) from ``main.on_printer_status_change``'s HMS pipeline. Never
    raises — invariant 10.
    """
    try:
        hms_list = getattr(state, "hms_errors", None) or []
        new_codes = set(new_full_codes or ())
        if not new_codes or not hms_list:
            return False
        # Trigger: at least one of THIS push's new codes is a demand. Without this a
        # standing demand would re-announce on every unrelated HMS arrival.
        fresh = [e for e in hms_list if getattr(e, "full_code", None) in new_codes]
        if current_runout_demand(fresh) is None:
            return False
        # Guidance: the CURRENT demand across the whole list (last wins) — the slot
        # the printer is asking for right now, which is what the operator must fill.
        demand = current_runout_demand(hms_list)
        if demand is None:
            return False
        global_tray = demand[0] * 4 + demand[1]

        from backend.app.core.database import async_session
        from backend.app.models.printer import Printer
        from backend.app.services.notification_service import notification_service

        async with async_session() as db:
            incident = await _open_runout_incident(db, printer_id)
            if incident is None:
                return False
            key = (incident.id, global_tray)
            if key in _guidance_sent:
                return False
            printer = await db.get(Printer, printer_id)
            printer_name = (printer.name if printer else None) or f"printer {printer_id}"
            job_name = (getattr(state, "subtask_name", None) or "").strip() or "print"
            slot_desc = runout_slot_desc(global_tray)
            _guidance_sent.add(key)
            await notification_service.on_spool_recovery_failed(
                printer_id=printer_id,
                printer_name=printer_name,
                job_name=job_name,
                detail=(
                    f"The printer is NOW asking for filament in {slot_desc or 'a different slot'} — "
                    "the slot it needs has CHANGED since the first alert. Insert filament there "
                    "(the print resumes by itself once the AMS sees it)."
                ),
                db=db,
                kind=KIND_RUNOUT,
                runout_slot=slot_desc,
                foreign=incident.item_id is None,
            )
        logger.info(
            "spool_recovery: printer %s runout demand moved to global tray %s (%s) — guidance refreshed",
            printer_id,
            global_tray,
            slot_desc,
        )
        return True
    except Exception:  # noqa: BLE001 — a guidance hook must never crash the status flow
        logger.exception("spool_recovery: runout guidance refresh failed for printer %s", printer_id)
        return False


def _slot_reads_loaded(state, slot: tuple[int, int] | None) -> bool:
    """Does the demanded slot physically hold filament right now?

    Presence through the ONE tri-state rule (``tray_fields.tray_presence``), and
    STRICTLY ``is True`` — an unknown presence (a mid-print reduced AMS report) is
    not evidence of a refill.
    """
    if slot is None:
        return False
    tray = _live_tray_dict(state, slot[0], slot[1])
    if tray is None:
        return False
    # The MERGED-dict form of the one tri-state rule (the caller holds
    # printer_manager status, not a raw pre-merge push).
    return tray_fields.tray_presence_from_dict(tray) is True


def _refill_ready(state, gained: tuple[int, int] | None = None) -> bool:
    """Is the WIRE saying the runout hold's filament is back? (Pure, DB-free.)

    Three admissible evidences of ONE physical fact, any one sufficient:

    * the DEMAND FAMILY is CLEAR — the firmware has stopped asking for filament, the
      most direct statement it can make;
    * the caller's own presence-GAIN edge IS the demanded slot — the edge is
      hardware evidence in its own right, and it must not be second-guessed by
      re-reading a tray the printer reports coarsely while PAUSEd; or
    * the demanded slot READS PRESENT-LOADED — the roll is in, whatever the demand
      still says.

    Neither alone is reliable, which is why both are admitted. 006-H2S proved the
    firmware LATCHES a bogus demand for a slot that never ran dry (a UI load during
    the hold resurfaced 12 h later as a demand for the latched slot), so waiting for
    the demand to clear can wait forever; and an H2S in PAUSE reduces its AMS report,
    so presence alone can read unknown for a slot that really was refilled. Precedent
    for the two-witness shape: the eject gate's in-G-code ``M190 R`` plus MQTT
    ``bed_temper`` confirmation.

    ONE disqualifier, added 2026-09-04: while the firmware's power-loss prompt stands
    (``hms_errors.power_loss_prompt_standing``) an ABSENT demand proves nothing, because
    a reboot wipes the standing HMS list wholesale. See the branch comment below. The
    check stays a pure read of the state already in hand — no incident lookup, no second
    demand source (the 08-13 D3 ruling: ``current_runout_demand`` is the ONE decoder).
    """
    hms_list = getattr(state, "hms_errors", None) or []
    demand = current_runout_demand(hms_list)
    if demand is None and power_loss_prompt_standing(hms_list):
        # "No demand" is evidence only when the firmware was in a position to HAVE one.
        # A printer that rebooted mid-print comes back with an EMPTY standing HMS list
        # carrying the power-loss prompt: its runout code was WIPED, not answered.
        # Printer 8 (010-H2S) resumed into an empty slot on 2026-09-04 on exactly this
        # reading, ran ~2 min and re-raised the runout — nothing had been refilled.
        # While the prompt stands, the only admissible evidence is HARDWARE: the
        # caller's own presence-GAIN edge on a tray that reads loaded. A gain on a
        # non-demanded slot resumes and the firmware simply re-declares the runout —
        # self-correcting and bounded, unlike a resume on an absence.
        return gained is not None and _slot_reads_loaded(state, gained)
    if demand is None:
        return True
    if gained is not None and tuple(gained) == demand:
        return True
    return _slot_reads_loaded(state, demand)


async def _resume_ready(db: AsyncSession, printer_id: int, gained: tuple[int, int] | None = None) -> str:
    """``"ready"`` / ``"running"`` (already resumed — nothing to do) / ``"no"``.

    Evaluated twice: on the spawn edge and again after the settle wait, because the
    operator may have resumed on the screen meanwhile."""
    st = _get_state(printer_id)
    live = getattr(st, "state", None) if st is not None else None
    if live == "RUNNING":
        return "running"
    if live != "PAUSE":
        return "no"
    if not _refill_ready(st, gained):
        return "no"
    return "ready" if await _open_runout_incident(db, printer_id) is not None else "no"


async def _resume_after_refill(printer_id: int, slot: tuple[int, int] | None) -> bool:
    """Resume a runout-held print once the wire says the filament is back.

    006-H2S 2026-07-26: the runout escalation leaves the print PAUSEd for a same-slot
    refill, and the refill itself is the operator's "go" — but the print then sat
    waiting for someone to press Resume. Doctrine rule 1 (minimal human interaction):
    the recoverable half of this state is recoverable without hands.

    ONE body behind TWO spawn sources — the ``ams_presence`` presence-GAIN edge (a
    roll went in) and :func:`note_demand_watch`'s wire edges (the firmware stopped
    asking). Before WS2b only the gain edge existed, so the 006 class — a demand
    naming a slot that was ALREADY loaded — could never resume, and auto-resume had
    never fired in production at all.

    Acts only when :func:`_resume_ready` holds on the edge AND again after the
    settle. On any failure it stands aside — no retry, no quarantine, no
    out-of-rotation stamp, and the escalation's guidance stays exactly as it was, so
    the operator's manual resume is still the reliable path. Returns True only on a
    confirmed RUNNING. Never raises — invariant 10.
    """
    try:
        # Cheap, DB-free pre-gate FIRST. This runs on every physical spool insert on
        # every printer, and almost none of them are a runout refill — opening a
        # session before knowing that is pure waste (it showed up as a measurable
        # slowdown across the suite).
        st = _get_state(printer_id)
        if st is None or getattr(st, "state", None) != "PAUSE" or not _refill_ready(st, slot):
            return False

        from backend.app.core.database import async_session

        async with async_session() as db:
            if not await _read_bool(db, "runout_auto_resume_enabled", _DEFAULT_RUNOUT_AUTO_RESUME):
                return False
            if await _resume_ready(db, printer_id, slot) != "ready":
                return False

        # Let the AMS register the insert; a resume published on the edge itself
        # races the firmware's tray-state settle and is simply rejected.
        await asyncio.sleep(_RUNOUT_RESUME_SETTLE_S)

        async with async_session() as db:
            verdict = await _resume_ready(db, printer_id, slot)
        if verdict == "running":
            logger.info(
                "spool_recovery: printer %s already RUNNING after the refill (operator resumed) — "
                "auto-resume stood down",
                printer_id,
            )
            return False
        if verdict != "ready":
            logger.info(
                "spool_recovery: printer %s refill auto-resume stood down after settle "
                "(state/wire/hold changed) — slot %s",
                printer_id,
                slot,
            )
            return False

        client = printer_manager.get_client(printer_id)
        if client is None:
            logger.info("spool_recovery: printer %s has no client — refill auto-resume skipped", printer_id)
            return False
        if not client.resume_print():
            logger.info(
                "spool_recovery: printer %s resume_print send returned False (offline?) — "
                "refill auto-resume stands aside, escalation guidance stands",
                printer_id,
            )
            return False
        if not await printer_manager.await_state(
            printer_id, {"RUNNING"}, _RUNOUT_RESUME_CONFIRM_S, poll_interval_s=_POLL_INTERVAL_S
        ):
            logger.info(
                "spool_recovery: printer %s did not reach RUNNING within %.0fs after the refill resume — "
                "standing aside (no retry); the escalation guidance stands",
                printer_id,
                _RUNOUT_RESUME_CONFIRM_S,
            )
            return False

        await _close_runout_hold_and_notify(printer_id, slot)
        logger.info(
            "spool_recovery: printer %s RESUMED automatically after the filament refill (slot %s)",
            printer_id,
            slot,
        )
        return True
    except Exception:  # noqa: BLE001 — an assist lane must never crash its caller
        logger.exception("spool_recovery: refill auto-resume failed for printer %s", printer_id)
        return False


async def maybe_auto_resume_on_refill(printer_id: int, ams_id: int, tray_id: int) -> bool:
    """Presence-GAIN entry point: a roll went into ``AMS{ams_id}-T{tray_id}``.

    Spawned fire-and-forget from ``ams_presence``'s gain edge, so it fires once per
    physical insert. The gained slot is passed for the notification's wording only —
    the DECISION is :func:`_refill_ready`'s wire evidence, deliberately not "the
    gained slot is the demanded slot" (that equality was the 006-H2S dead end: the
    firmware demanded an already-loaded slot, so the refill that mattered happened
    somewhere else and the gate never opened).
    """
    return await _resume_after_refill(printer_id, (ams_id, tray_id))


def note_demand_watch(printer_id: int, state) -> None:
    """Per-push wire sampler: the second spawn source, and the hold's close edge.

    Sync, DB-free and in-memory — it rides the ~1 Hz status push beside
    ``hms_edges.note_push``/``spool_respool.sample_status_push`` and only ever SPAWNS
    work on an EDGE:

    * a transition INTO ``RUNNING`` → :func:`on_observed_running` (the printer is
      going again, whoever resumed it — this is what kills the forever-hold);
    * while PAUSEd, the DEMAND disappearing → :func:`_resume_after_refill` for the
      slot that was demanded (the firmware answered "filament is back");
    * while PAUSEd, an EXTERNAL-runout code disappearing → the same, with no slot.
      The demand decoder covers AMS slots only, so the external lane watches CLASS
      MEMBERSHIP instead: no ``runout_external`` code standing, no runout.

    That watch is scoped to the runout CLASS on purpose, not to "external faults".
    An external FEED fault (``external_feed_fault``) is an interactive firmware
    prompt — "feed filament into the PTFE tube until it can not be pushed any
    farther", answered ON the printer — so its code clearing means the operator is
    mid-dialogue, not that the print may be driven from here. Those resolve the
    ordinary way: an observed RUNNING transition (above) or the job's terminal.

    Both DISAPPEARANCE edges are additionally scoped to ONE MQTT session
    (``PrinterState.connection_epoch``): see the comment block at the negative-edge
    boundary below.

    Never raises (invariant 10) and never touches the DB — every spawned lane
    re-checks its own gates against durable state.
    """
    try:
        live = (getattr(state, "state", None) or "").upper()
        hms_list = getattr(state, "hms_errors", None) or []
        demand = current_runout_demand(hms_list)
        # Classified ONCE — this runs at ~1 Hz per printer.
        candidates = live_candidates(state)
        # The RUNOUT class only — an external feed fault is a firmware dialogue the
        # farm must not double-drive (see the docstring).
        externals = frozenset(c.short_code for c in candidates if c.fault_class is AmsFaultClass.RUNOUT_EXTERNAL)
        epoch = int(getattr(state, "connection_epoch", 0) or 0)
        prev = _wire_sample.get(printer_id)
        _wire_sample[printer_id] = (live, demand, externals, epoch)

        # Re-arm the faults an aborted close barred, on either wire edge that proves
        # "this is no longer the same standing fault" (see :data:`_blocked`).
        _rearm_blocked(
            printer_id,
            fault_tokens(candidates),
            paused_edge=(live == "PAUSE" and (prev is None or prev[0] != "PAUSE")),
        )

        if prev is None:
            # First sample after a (re)start seeds only — a demand that was already
            # gone before we looked is not an edge we witnessed.
            return
        prev_state, prev_demand, prev_externals, prev_epoch = prev

        from backend.app.core.tasks import spawn_background_task

        if live == "RUNNING" and prev_state != "RUNNING":
            spawn_background_task(on_observed_running(printer_id), name=f"incident-running-p{printer_id}")
            return
        if live != "PAUSE":
            return

        # --- from here down the edges are NEGATIVE ("a standing code went away") ---
        #
        # A new MQTT session re-seeds them all. A session boundary can FAKE every
        # negative edge at once: a rebooted printer arrives with an empty standing HMS
        # list, so the demand and the external-runout codes are gone without anything
        # having been refilled (2026-09-04 — printer 8 resumed into an empty slot on
        # exactly this reading, ran ~2 min and re-raised the runout). A reconnect is not
        # a firmware answer.
        #
        # The RUNNING edge above is deliberately OUTSIDE this guard: it is POSITIVE
        # evidence (the printer is demonstrably printing again, whoever resumed it) and
        # a reconnect cannot fabricate it — suppressing it would strand a hold that the
        # operator cleared during the outage. Same asymmetry, same reason, as
        # `hms_edges` keeping its own first-frame seed rather than riding the epoch:
        # a reboot cannot fabricate the APPEARANCE of a code that is not standing.
        if epoch != prev_epoch:
            return
        if prev_demand is not None and demand is None:
            spawn_background_task(
                _resume_after_refill(printer_id, prev_demand),
                name=f"runout-demand-clear-resume-p{printer_id}",
            )
            return
        if prev_externals and not externals:
            spawn_background_task(
                _resume_after_refill(printer_id, None),
                name=f"external-runout-clear-resume-p{printer_id}",
            )
    except Exception:  # noqa: BLE001 — a per-push sampler must never crash the status flow
        logger.exception("spool_recovery: wire sampler failed for printer %s", printer_id)


async def _close_runout_hold_and_notify(printer_id: int, slot: tuple[int, int] | None) -> None:
    """Post-resume bookkeeping: close the incident, drop the now-false hold token and
    tell the operator. Clearing matters — a RUNNING print still carrying the hold
    shows a phantom stop on the run page and re-arms the hourly attention reminder
    the moment the printer pauses again for any reason. Best-effort; a failure here
    must not turn a successful resume into an error."""
    from backend.app.core.database import async_session
    from backend.app.models.printer import Printer
    from backend.app.services.notification_service import notification_service

    try:
        async with async_session() as db:
            incident = await _open_runout_incident(db, printer_id)
            item_id = incident.item_id if incident is not None else None
            tray = incident.slot_global_tray if incident is not None else None
            if slot is not None:
                tray = slot[0] * 4 + slot[1]
            slot_desc = runout_slot_desc(tray) or "the filament slot"
            if incident is not None:
                await printer_incidents.close(db, incident.id, status=STATUS_RESOLVED, source=RESOLVE_AUTO_RESUME)
            await _clear_hold_projection(db, item_id)
            printer = await db.get(Printer, printer_id)
            printer_name = (printer.name if printer else None) or f"printer {printer_id}"
            st = _get_state(printer_id)
            job_name = (getattr(st, "subtask_name", None) or "").strip() or "print"
            await notification_service.on_runout_auto_resumed(
                printer_id=printer_id,
                printer_name=printer_name,
                job_name=job_name,
                slot_desc=slot_desc,
                db=db,
            )
    except Exception:  # noqa: BLE001 — bookkeeping must not undo a successful resume
        logger.exception("spool_recovery: post-resume bookkeeping failed for printer %s", printer_id)


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
