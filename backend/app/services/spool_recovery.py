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

    (printer already PAUSEd) → [while the AMS holds the print's filament change: the
    RELEASE LADDER, one verb at a time, until the AMS is out of the change] → select the
    next eligible loaded spool → (SWAP COMMIT: take the jammed spool out of rotation) →
    unload → confirm the AMS finished the unload cycle (see :func:`_confirm_unloaded`)
    → load the replacement → confirm ``tray_now == target`` (the first load may not
    take — resend) → resume → the reader sees the print RUNNING, the path quiet and the
    replacement feeding, held stable (a lingering fault may need one retry) → SUCCESS.
    If nothing works: give up — restore what the swap moved, notify, and leave the
    printer PAUSED for a human, never resume blind.

    SELECTION PRECEDES THE COMMIT (004-H2S 2026-09-17, incident 192). The swap used to
    stamp and unload before it knew a replacement existed, so an honest "no eligible
    spool" verdict left a printer with an empty extruder that no copy described; the
    operator's Resume then printed 4 h of air. With nothing to load there is now no
    unload, and a driver that DID empty the extruder reloads the jammed spool before it
    pages (:func:`_give_up`) — the machine is symmetric.

    Out-of-rotation stamping/notification is bound to the SWAP-COMMIT boundary (the
    step just before the first unload), NOT to entry: a no-swap firmware self-heal
    (a release verb frees the change on the same spool) must never stamp or announce a
    spool the print keeps running on, and fires its own truthful self-heal
    notification instead (2026-07-20 incident — an entry-time stamp+announce, then a
    same-spool self-heal 90 s later, misled the operator).

    W1 — the feeder-stall WEDGE (009-H2S 2026-07-20, 002-H2S 2026-09-11, 012-H2S
    2026-09-23). After a feed fault the AMS can sit mid filament-change (gcode_state
    PAUSE, ``ams_status_main == 1``) holding the PRINT's own change: 012-H2S re-ran its
    own feed on the stalled slot 48 s before any farm command (21:44:48), and every
    command sent into that change was ACKNOWLEDGED and HELD behind it, then run in order
    at the next release (six commands echoed success, moved nothing, and ran after the
    terminal). A held UNLOAD that drains while the print RUNS empties the extruder
    mid-print, so the farm sends NO swap command into the change. While the AMS's own
    state word (``bambu_mqtt.ams_mid_filament_change``, the one predicate) reads
    mid-change, a round runs the RELEASE LADDER instead (:data:`_LEVERS` — every verb the
    wire offers, in evidence order, each pulled at most once per incident and read by ONE
    reader, :func:`_read_after`): the firmware may self-heal on the jammed feeder (no
    swap), release the AMS (the swap goes out at once), re-hold (the next verb), or end
    the print (the driver records that itself). Every verb spent with the change still
    held is ``wedge_unreleased``, with NOTHING sent. A non-``1`` non-idle state is NOT a
    wedge: 006-H2S 2026-07-21 faulted at ``ams_status_main == 3`` (assist) with the
    feeder still engaged and the unload was accepted immediately — that round runs the
    swap machine directly. A swap command the AMS acknowledges and HOLDS after a release
    (the AMS re-entered a change before it ran) is never resent: ``swap_held_after_release``.

    W2: a printer whose recovery escalates repeatedly within a rolling window is
    quarantined off the durable ``recovery_escalation`` ledger — a recurring AMS jam
    is hardware (buffer / feeder), not a spool the swap machine can fix.

The unload is UNCONDITIONAL after a feed fault, even when ``tray_now`` already
reads 255. Production incident 009-H2S 2026-07-20: an earlier ``tray_now == 255``
short-circuit made the machine send ZERO unloads across four candidate loads while
the AMS sat mid filament-change (``ams_status_main == 1``); it escalated to a human.
The operator then recovered the identical state in 90 s with the commands the machine
already had — an explicit unload (sent at ``tray_now == 255``), a load, a resume. After
a feed fault 255 means "nothing is feeding", NOT "the path is clear" (doctrine
invariant 8), so the unload always goes out before a load. The short-circuit survives
ONLY for the genuinely-clean restart case it was written for (see
:func:`_unload_skippable`).

Every AMS load and unload goes through ``services/ams_command`` (the verbs publish;
``ams_command.classify`` is the ONE reading of what the wire answered, fed the
firmware's ACK for each send). The driver keeps its own poll loops — each asks
:func:`_takeover` on every poll — and records every lever it pulled and every command
that went out on ONE durable evidence log (:class:`_RecoveryEvidence`, persisted step by
step through ``printer_incidents.note_step``), so the give-up reason, the operator page
and a restarted driver's next lever are derived from what was sent and what the wire
said, never from a premise.

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
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Literal, TypeGuard, assert_never, get_args

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_incident import (
    AMS_FAULT_KINDS,
    KIND_JAM,
    KIND_PHYSICAL,
    KIND_RUNOUT,
    RESOLVE_AUTO_RESUME,
    RESOLVE_DRIVER_SELF_HEAL,
    RESOLVE_DRIVER_SWAP,
    RESOLVE_OPERATOR,
    RESOLVE_PAUSED_ELSEWHERE,
    STATUS_ABORTED,
    STATUS_ESCALATED,
    STATUS_RECOVERING,
    STATUS_RESOLVED,
    PrinterIncident,
)
from backend.app.models.printer_incident_step import STEP_KIND_COMMAND, STEP_KIND_LEVER, PrinterIncidentStep
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services import (
    ams_command,
    farm_correlation,
    incident_resolution,
    print_reconcile,
    printer_incidents,
    spool_respool,
    tray_fields,
)
from backend.app.services.bambu_mqtt import AMS_STATUS_IDLE, ams_mid_filament_change
from backend.app.services.hms_errors import (
    AmsFaultClass,
    FaultCandidate,
    candidate_fingerprint,
    current_runout_demand,
    fault_tokens,
    full_codes_of,
    live_candidates,
    power_loss_prompt_standing,
)
from backend.app.services.incident_resolution import (
    _REPAIR_EVIDENCE_LOAD,
    Context,
    TerminalEvent,
    driver_owns,
    ledger,
)
from backend.app.services.printer_incidents import (
    RECOVERY_WAITING_REASONS,
    WAITING_REASON_RECOVERING,
    runout_slot_desc,
    waiting_reason_for,
)
from backend.app.services.printer_manager import printer_manager
from backend.app.services.spool_respool import decode_global_tray, encode_global_tray

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.app.services.bambu_mqtt import BambuMQTTClient, PrinterState

logger = logging.getLogger(__name__)

# --- The live-fault vocabulary --------------------------------------------
# DERIVED from the AMS fault taxonomy (``hms_errors``), by CLASS, never from a
# short-code list: the classification of a firmware code is that module's job, and a
# second literal copy is exactly how two modules come to disagree about one code
# (doctrine invariant 1). What the machine may be STARTED by is the taxonomy's
# classification of the live entries (:func:`live_candidates`, both wire lanes); the
# DRIVER asks the same classification a narrower question — "is the fault this incident
# is working on still standing" (:func:`_active_recoverable_codes`,
# :func:`_feed_fault_live`).
#
# By class and over BOTH lanes, because the short-code sets this block used to hold
# could not see an attr-lane-only code: ``0700_0012`` (AMS A slot N feeder motor
# stalled — 012-H2S 2026-09-22, incident 304) has only a code-word row, so a driver
# reading short codes saw "no fault" while it stood alone, and the self-heal test and
# the repause-vs-abort choice answered on a fault they could not see.
#
# WIDENED 2026-08-09 (WS2b, operator-ratified partition): the swap machine triggers on
# the whole mechanical-feed class — the send-out 8005, feed-into-extruder 8006 and
# feed-to-extruder 8028 families beside the 8010 / 801E ones. The deliberate EXCLUSIONS
# live with the classification they qualify, in the ``hms_errors`` taxonomy tables: the
# ``0700_0001`` ban (short-code header), the ``0700_0025`` precursor (INFORMATIONAL),
# the pull-out/pull-back families (PHYSICAL_FAULT, 8003/8004) and the clog family
# (PHYSICAL_FAULT, 0300_801A/801C/8016 + 0300_4006) — the PHYSICAL_FAULT class escalates
# with a hold and never enters the swap loop.
_FEED_FAULT_CLASSES: frozenset[AmsFaultClass] = frozenset({AmsFaultClass.MECHANICAL_FEED})
# Feed faults plus the AMS-slot runout: the codes a re-PAUSE after a resume may stand
# on and still be a FAULT (``repause``) rather than somebody pausing the print (``abort``).
_RECOVERABLE_FAULT_CLASSES: frozenset[AmsFaultClass] = frozenset({AmsFaultClass.MECHANICAL_FEED, AmsFaultClass.RUNOUT})

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

# kind -> the BEST class that maps onto it — the inverse of _KIND_BY_CLASS, built
# from it so the two cannot drift, and taking the first (highest-precedence) class
# for a kind two classes share (``runout`` is reached by both RUNOUT and
# RUNOUT_EXTERNAL). Used only to RANK an open row against a live classification:
# :func:`_outranks`.
_CLASS_BY_KIND: dict[str, AmsFaultClass] = {}
for _class in _CLASS_PRECEDENCE:
    _CLASS_BY_KIND.setdefault(_KIND_BY_CLASS[_class], _class)


def _outranks(fault_class: AmsFaultClass, kind: str) -> bool:
    """Is a live ``fault_class`` WORSE than the kind an open row already carries?

    The upgrade test. 003-H2S 2026-09-11: ``0700_0012`` (mechanical) arrived 1.2 s
    before ``0700_8004`` (physical), so the incident opened ``jam`` — and because an
    open incident was never re-classified, the swap machine spent the whole hold
    acting on the milder reading: a firmware CONTINUE, an out-of-rotation stamp on a
    healthy spool, and two unloads against filament that cannot retract.
    """
    existing = _CLASS_BY_KIND.get(kind)
    if existing is None:
        return False
    return _CLASS_PRECEDENCE.index(fault_class) < _CLASS_PRECEDENCE.index(existing)


# --- waiting_reason tokens -------------------------------------------------
# The kind -> token table and its tokens live in ``printer_incidents`` (the store that
# owns the kinds) since the pause-cause kinds joined them; imported here because this
# module STAMPS the projection and the seven names below are read all over it.


# --- Safety bounds (code constants, NOT operator knobs — precedent the client-
#     owned settle-wait, bambu_mqtt._IDENTIFY_GATE_S / wait_ams_settle). The
#     unload/load/resume confirm timeout and per-step resend count ARE operator
#     settings. -----------------------------------------------------------------
_POLL_INTERVAL_S = 1.0  # live-state poll spacing during every confirm wait
_POST_RESUME_STABLE_S = 60  # a quiet RUNNING on a real feeder must hold this long = success
_MAX_CANDIDATES = 3  # distinct replacement trays tried before escalating
# How many times each lever of the release ladder (:data:`_LEVERS`) may be pulled per
# INCIDENT, judged from the durable evidence log (:class:`_RecoveryEvidence`): a second
# pull of the same verb against the same wedge measures nothing the first did not
# (012-H2S 2026-09-23: both CONTINUE spellings re-ran the stalled feed and re-held).
# Spent is a STEP on the incident's log, never a process-lifetime counter — the
# counters that preceded it were keyed by (printer, job), so 003-H2S 2026-09-19 04:31
# met a second fault 69 s after a self-heal on the same job and gave up with ZERO
# CONTINUEs sent — and never an in-memory list either, which a restart emptied and so
# re-ground the stalled feeder from the first lever. A new incident is a new log.
_MAX_LEVER_PULLS = 1

# Refill auto-resume timing (code constants, NOT operator knobs — precedent
# ``ams_command.UNLOAD_GRACE_S``). The AMS needs to register the freshly-inserted
# filament before a resume can land: a resume published on the presence edge itself
# races the firmware's own tray-state settle and is rejected. 15 s mirrors the
# operator's proven manual gap (``ams_command.UNLOAD_GRACE_S``) — long enough to settle,
# short enough that the operator is still standing at the printer.
_RUNOUT_RESUME_SETTLE_S = 15.0
# Bound on how long we wait for RUNNING after the resume before standing aside.
_RUNOUT_RESUME_CONFIRM_S = 30.0

# tray_now sentinel: no filament fed (unloaded). The value lives with the tray
# vocabulary (``tray_fields``, one origin per magic value); this is the module's name for it.
_NO_FILAMENT = tray_fields.TRAY_NOW_NOTHING_FED

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
    "runout_needs_refill": (
        "Filament ran out and the printer only accepts new filament in the SAME slot — "
        "insert filament and resume on the printer."
    ),
    "candidates_exhausted": "Tried every eligible replacement spool without a stable resume. Left PAUSED for a human.",
    # Maintenance mode: the farm recorded the fault and did nothing about it, on purpose.
    # The page this detail rides is itself suppressed for a held printer (the notification
    # fan-out's held-printer gate), so this is what the incident ledger and the log read.
    "service_hold": (
        "Printer is in maintenance mode — the fault is recorded and no recovery was attempted. "
        "It stays held until the fault is cleared."
    ),
    # The reasons a jam reaches after the farm SENT its release verbs or its swap
    # commands. Each states what the wire answered; what was sent and what the wire said
    # is composed from the driver's evidence log (:func:`_compose_detail`), never
    # restated here — the pre-2026-09-23 copy instructed the very action the page's own
    # evidence sentence recorded as ineffective (012-H2S 2026-09-22, incident 304).
    "unload_failed": (
        "The unload did not complete. Open the AMS and free the filament at the feeder, then press Continue on "
        "the printer."
    ),
    # Every lever this incident could pull is spent — sent or not sent alike — and the AMS
    # still holds the print's own filament change (012-H2S 2026-09-23). No placeholder:
    # the slot is the feeder clause's, the verbs are the evidence sentence's.
    "wedge_unreleased": (
        "The AMS is holding the paused print's filament change and no release verb the firmware accepts "
        "released it. Free the spool, then press Retry on the printer."
    ),
    # A lever read ``released`` and the swap command that followed was acknowledged and
    # HELD: the AMS re-entered a change before it ran. The evidence sentence names the ONE
    # held command and what it does at the next release — that is its one place.
    "swap_held_after_release": (
        "The AMS released the paused print's filament change, then held the farm's swap command. Free the "
        "spool, then press Retry on the printer."
    ),
    # A release verb with an unmeasured effect ENDED the print inside its own read window
    # (the reader's ``ended``). The terminal's own disposition handles the unit.
    "wedge_ended_print": "A release verb ended the print.",
    # The printer stopped reporting on a live session while the driver worked: either it
    # stayed off past ``farm_offline_stall_minutes`` (:class:`_PrinterOffline`), or the
    # rounds ran out on commands whose answers were lost with their session
    # (:meth:`_RecoveryEvidence.exhaustion_reason`). Nothing about the AMS is claimed —
    # the evidence sentence names what went out unanswered, and no feeder clause is read
    # off a wire that is not reporting (:func:`_compose_detail`).
    "printer_offline": (
        "The printer stopped reporting during recovery, so the farm could not read what the AMS did. Check the "
        "printer's connection and the AMS, then resume on the printer."
    ),
    "repeated_jams": (
        "Auto-recovered several times this job but the fault keeps returning — likely an "
        "extruder-side problem, not the spool. Left PAUSED for a human."
    ),
    # WS2b: the two classes that reach escalation without ever entering the loop.
    # The resume instruction is deliberately NOT here — it lives in
    # :func:`_retract_clause`, because on a latched pull-back "resume" is the wrong
    # next action and the copy must be able to say so (006-H2S 2026-09-21, incident 289).
    "physical_fault": (
        "A physical filament fault (broken filament, a clog, or a failed pull-back) — fresh filament "
        "cannot clear it, so the farm will not swap. Check the filament path at the printer."
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
    """The operator-tunable knobs the driver reads, once per incident.

    ``offline_bound_s`` is not a recovery knob of its own: it is the farm's offline-stall
    window (``farm_offline_stall_minutes``, read through ``farm_stall.offline_stall_seconds``
    — the one origin), and it bounds how long the driver waits out a session gap before it
    gives up ``printer_offline`` (:func:`_live_reading`)."""

    enabled: bool
    max_attempts: int
    step_timeout_s: float
    protect_layers: int
    offline_bound_s: float


# --- the release ladder: every verb the wire offers, pulled in evidence order ------

Lever = Literal[
    "resume",
    "resume_then_pause",
    "ams_control_resume",
    "clean_print_error",
    "ams_control_abort",
    "ams_control_reset",
    "ams_control_pause",
]


@dataclass(frozen=True)
class LeverSpec:
    """One release verb: the frame it publishes, whether its read pauses the print on
    the first RUNNING sample with NOTHING at the feeder (the firmware's retract), and the
    page's one-line name of it.

    ``publish`` is the wire verb and nothing else — True when the client sent the frame.
    What the wire answered is :func:`_read_after`'s alone, for every verb.
    """

    publish: Callable[[BambuMQTTClient], bool]
    pause_on_empty_path: bool
    text: str


# THE LADDER, in pull order — the dict's order IS the ladder's. Explicit, every lever
# spelled; a missing key RAISES at the lookup (:func:`_lever`, the ``ams_command._row``
# idiom). The ONE place the release verbs are published (AST-pinned in
# ``test_code_quality``). Vendor evidence per verb (BambuStudio ``DeviceManager.cpp`` /
# ``StatusPanel.cpp``, the vendored action catalog ``backend/app/data/hms_actions.json``):
#
# * ``resume`` — ``print.resume`` (``BambuMQTTClient.resume_print``): BambuStudio's bare
#   ``command_task_resume`` (``{"print": {"command": "resume", "param": ""}}`` — the fork
#   sends the same verb with a sequence id and no empty ``param``). It is NOT the error
#   dialog's CONTINUE: for ``07008006`` / ``07008005`` the catalog offers CONTINUE and
#   CHECK_ASSISTANT only, and CONTINUE is ``ams_control resume`` (``ams_control_resume``
#   below; the fork's own dialog dispatcher, ``bambu_mqtt`` ``HMSAction.CONTINUE``, maps
#   it so). Nor is it Studio's ``command_hms_resume`` / ``command_hms_ignore``, which carry
#   ``err`` + ``param: "reserve"`` + ``job_id``: no fork publisher has ever sent that shape
#   (the ``bambu_mqtt`` dialog helper NAMED ``hms_resume`` sends the plain resume). Freed
#   009-H2S 2026-07-20 (an EMPTY path) — the one measured remote release, so it stays
#   first; on a LOADED stalled feeder it re-runs the print's own change and re-holds
#   (002-H2S 2026-09-11; 012-H2S 2026-09-23, re-PAUSEd in 47 s; 012-H2S 2026-09-25,
#   incidents 341/343, read ``wedged`` twice).
# * ``resume_then_pause`` — ``print.resume``, then ``print.pause`` on the first RUNNING
#   sample whose feeder reads EMPTY (:func:`_feeder_position` kind ``empty``) — the July
#   2026-07-20 "workable PAUSE", a pause that lands outside the change with nothing
#   loaded. On an EMPTY-path wedge that is the FIRST RUNNING sample (009, unchanged). On
#   a LOADED stall it is the firmware's own retract of the jammed filament: 012-H2S
#   2026-09-25 pulled slot 3 back and re-fed the same slot after every resume, the wire
#   reading ``tray_now`` 2 → 255 → 2 every ~12 s (13:28:34 → 13:29:22), while a pause on
#   the first RUNNING sample (13:32:06, ``tray_now=2``) landed LOADED and read wedged.
#   Second in the pull order: it is the only lever whose pause is timed to a posture the
#   swap can go out from. Dual-nozzle: ``_feeder_position`` prefers the per-extruder map,
#   which may never read empty through a retract — that case degrades to the ``hung``
#   arm at the deadline.
# * ``ams_control_resume`` — ``ams_control("resume")``: the error dialog's CONTINUE for
#   ``07008006`` / ``07008005``, its RETRY, and the extruder-switch panel's "Retry".
#   012-H2S 2026-09-23: echo SUCCESS, re-held in 87 s.
# * ``clean_print_error`` — the dialog's OK / close ("clears whatever error dialog is
#   currently active"). The FRAME only (``BambuMQTTClient.clean_print_error``): a local
#   HMS wipe would fake the quiet reading this ladder is judged by. Unmeasured.
# * ``ams_control_abort`` — the extruder-switch panel's "Quit"
#   (``ExtruderSwithingStatus::on_quit``); the catalog maps it for ``07008036`` only, and
#   its effect on a paused print is undocumented. Unmeasured.
# * ``ams_control_reset`` — a valid firmware param (the ``command_ams_control``
#   allowlist) with no vendor UI call site. The fork's former claim that it "ends the
#   change the paused print is waiting on" was unsourced and is retracted. Unmeasured.
# * ``ams_control_pause`` — a valid firmware param with no vendor UI call site. Unmeasured.
#
# ``ams_control("done")`` is deliberately NOT a lever: it is the manual-feed dialog's
# "Filament extruded", a claim that a feed succeeded, and the runout-class hold it
# produces refuses cross-slot loads (2026-07-19, 10 of 10). The job terminal — the one
# measured release — is forbidden by ruling: the driver never ends a print.
_LEVERS: dict[Lever, LeverSpec] = {
    "resume": LeverSpec(
        publish=lambda client: client.resume_print(),
        pause_on_empty_path=False,
        text="resume",
    ),
    "resume_then_pause": LeverSpec(
        publish=lambda client: client.resume_print(),
        pause_on_empty_path=True,
        text="resume then pause",
    ),
    "ams_control_resume": LeverSpec(
        publish=lambda client: client.ams_control("resume"),
        pause_on_empty_path=False,
        text="ams_control resume",
    ),
    "clean_print_error": LeverSpec(
        publish=lambda client: client.clean_print_error(),
        pause_on_empty_path=False,
        text="clean_print_error",
    ),
    "ams_control_abort": LeverSpec(
        publish=lambda client: client.ams_control("abort"),
        pause_on_empty_path=False,
        text="ams_control abort",
    ),
    "ams_control_reset": LeverSpec(
        publish=lambda client: client.ams_control("reset"),
        pause_on_empty_path=False,
        text="ams_control reset",
    ),
    "ams_control_pause": LeverSpec(
        publish=lambda client: client.ams_control("pause"),
        pause_on_empty_path=False,
        text="ams_control pause",
    ),
}


def _lever(name: Lever) -> LeverSpec:
    try:
        return _LEVERS[name]
    except KeyError:
        raise LookupError(
            f"spool_recovery: no lever-table row for {name!r} — every lever must be given its own row in _LEVERS"
        ) from None


# What ONE read of a verb answered (:func:`_read_after`, THE reader). Closed.
#
# ``self_healed`` / ``swapped`` — the print RUNS (or FINISHED) on a quiet path with the
#   jammed / another tray feeding, held stable; ``wedged`` / ``released`` — back at PAUSE
#   with the AMS still in / out of the change; ``ended`` — a terminal inside the window
#   that the operator's Stop did not produce; ``no_pause`` — the reader's own pause
#   never landed; ``not_sent`` — the client did not publish the verb; ``abort`` /
#   ``handover`` — :func:`_takeover` (an operator Stop's terminal is ``abort``).
LeverReading = Literal[
    "self_healed", "swapped", "wedged", "released", "ended", "no_pause", "not_sent", "abort", "handover"
]
_LEVER_READINGS: frozenset[str] = frozenset(get_args(LeverReading))


@dataclass(frozen=True)
class LeverRead:
    """ONE read: the closed verdict, whether the printer left PAUSE (or the AMS its
    state) inside the window, and the feeder the verdict was decided on.

    ``moved`` is what tells "the resume never took" from "it took and re-PAUSEd"
    (002-H2S 2026-09-11: only the second is evidence against a replacement), and
    ``position`` is what a ``swapped`` success names — the tray the reader SAW feeding,
    never one re-read after the verdict. ``takeover`` is :func:`_takeover`'s token when one
    ended the read (``abort`` / ``handover``), so the abort that follows names the actor the
    read saw (:func:`_abort` — ``paused_elsewhere`` closes under its own source).
    """

    reading: LeverReading
    moved: bool
    position: FeederPosition
    takeover: TakeoverToken | None = None


# The unload step (:func:`_unload_and_confirm`): an ``ams_command.Answer`` for a publish
# that went out, or the step's own ``skipped`` / ``drying`` / ``refused`` (every publish
# was an ``ams_command.Refusal``), or a takeover.
UnloadStep = Literal[
    "complete",
    "acted",
    "no_movement",
    "held",
    "undecidable",
    "session_changed",
    "skipped",
    "drying",
    "refused",
    "abort",
    "handover",
]
# The load step (:func:`_load_and_confirm`). No ``undecidable``: only an unload into a
# mid-change AMS with nothing loaded can be (``ams_command``'s table).
LoadStep = Literal[
    "complete", "acted", "no_movement", "held", "session_changed", "drying", "refused", "abort", "handover"
]
# What a step answers when :func:`_takeover` ends it (:func:`_note_takeover`).
StepTakeover = Literal["abort", "handover"]


# --- the evidence log: every lever pulled and every command sent, in send order -----


@dataclass(frozen=True)
class LeverStep:
    """One release verb this incident's drivers pulled. ``outcome`` is ``None`` from the
    send until the read (a crash between the two leaves it ``None`` for good — the lever
    is spent and nobody saw what it did). ``moved`` is the read's own, and ``None`` on a
    step hydrated from the ledger (the table keeps the verdict, not the motion)."""

    seq: int
    lever: Lever
    outcome: LeverReading | None
    moved: bool | None
    at: datetime | None

    @classmethod
    def draft(cls, lever: Lever) -> LeverStep:
        """An unsent step: :meth:`_RecoveryEvidence.note` assigns its ``seq`` and ``at``."""
        return cls(seq=0, lever=lever, outcome=None, moved=None, at=None)


@dataclass(frozen=True)
class CommandStep:
    """One AMS motion command that WENT OUT (an ``ams_command`` verb returned its
    ``Sent``), the feeder position read at the send, and the classifier's answer —
    ``None`` until the read, and for good when a takeover ended the wait."""

    seq: int
    command: ams_command.Command
    target: int | None
    feeder: str | None
    answer: ams_command.Answer | None
    at: datetime | None

    @classmethod
    def draft(cls, command: ams_command.Command, target: int | None, feeder: str | None) -> CommandStep:
        """An unsent step: :meth:`_RecoveryEvidence.note` assigns its ``seq`` and ``at``."""
        return cls(seq=0, command=command, target=target, feeder=feeder, answer=None, at=None)


_COMMANDS: frozenset[str] = frozenset(get_args(ams_command.Command))
_ANSWERS: frozenset[str] = frozenset(get_args(ams_command.Answer))


def _is_lever(token: str) -> TypeGuard[Lever]:
    return token in _LEVERS


def _is_reading(token: str) -> TypeGuard[LeverReading]:
    return token in _LEVER_READINGS


def _is_command(token: str) -> TypeGuard[ams_command.Command]:
    return token in _COMMANDS


def _is_answer(token: str) -> TypeGuard[ams_command.Answer]:
    return token in _ANSWERS


def _step_of(row: PrinterIncidentStep) -> LeverStep | CommandStep:
    """Hydrate ONE ledger row. A token this module's vocabulary cannot name RAISES — a
    ledger that says something the driver cannot read is drift, never a skipped row."""
    outcome = row.outcome
    if row.kind == STEP_KIND_LEVER:
        if not _is_lever(row.name):
            raise LookupError(f"spool_recovery: incident {row.incident_id} step {row.seq} names no lever: {row.name!r}")
        if outcome is not None and not _is_reading(outcome):
            raise LookupError(f"spool_recovery: incident {row.incident_id} step {row.seq} reads {outcome!r}")
        return LeverStep(seq=row.seq, lever=row.name, outcome=outcome, moved=None, at=row.sent_at)
    if row.kind == STEP_KIND_COMMAND:
        if not _is_command(row.name):
            raise LookupError(
                f"spool_recovery: incident {row.incident_id} step {row.seq} names no command: {row.name!r}"
            )
        if outcome is not None and not _is_answer(outcome):
            raise LookupError(f"spool_recovery: incident {row.incident_id} step {row.seq} answers {outcome!r}")
        return CommandStep(
            seq=row.seq, command=row.name, target=row.target, feeder=row.feeder, answer=outcome, at=row.sent_at
        )
    raise LookupError(f"spool_recovery: incident {row.incident_id} step {row.seq} has kind {row.kind!r}")


@dataclass
class _RecoveryEvidence:
    """THE evidence log of one incident: every lever pulled and every AMS command sent,
    in send order, with what the wire answered — the RECORDS the escalation reason, the
    operator page and a restarted driver's next lever are derived from.

    Durable, because the wire cannot restate it: on a feeder-stall wedge every release
    verb's consequence is identical (012-H2S 2026-09-23 — both CONTINUE spellings re-ran
    the stalled feed and re-held in ``1/5``), so a driver restarted from the wire alone
    re-grinds the stall from the first lever. Every step is persisted at the SEND
    (:meth:`note`) and answered at the READ (:meth:`answer`) through
    ``printer_incidents.note_step`` / ``answer_step`` — this class is their ONE caller —
    and the log is built ONLY by :meth:`from_row` (both spawn paths, AST-pinned). Every
    counter is DERIVED from ``steps``; nothing is counted twice.

    The 009-H2S incident reported ``no_eligible_spool`` after four failed loads — a lie
    that sent the operator looking for spools instead of at the feed path. The reason is
    read off this log, never off a position in the code.
    """

    incident_id: int
    steps: list[LeverStep | CommandStep] = field(default_factory=list)

    @classmethod
    async def from_row(cls, db: AsyncSession, incident_id: int) -> _RecoveryEvidence:
        """The log of ``incident_id`` as the ledger holds it — empty for a fresh incident,
        the previous driver's steps for a re-entered one."""
        return cls(
            incident_id=incident_id, steps=[_step_of(row) for row in await printer_incidents.steps_of(db, incident_id)]
        )

    async def note(self, step: LeverStep | CommandStep) -> int:
        """Append ``step`` (a :meth:`LeverStep.draft` / :meth:`CommandStep.draft`) with the
        next ``seq`` and PERSIST it, in its own session, at the send. Returns the seq.

        Best-effort on the write, like every bookkeeping step of this driver: a failed
        commit is logged and the in-memory step stands, so the live driver's own budget
        still holds — what is lost is only the durability a re-entry would read.
        """
        from backend.app.core.database import async_session

        seq = max((s.seq for s in self.steps), default=0) + 1
        at = datetime.utcnow()
        try:
            async with async_session() as db:
                match step:
                    case LeverStep():
                        row = await printer_incidents.note_step(
                            db, self.incident_id, seq=seq, kind=STEP_KIND_LEVER, name=step.lever
                        )
                    case CommandStep():
                        row = await printer_incidents.note_step(
                            db,
                            self.incident_id,
                            seq=seq,
                            kind=STEP_KIND_COMMAND,
                            name=step.command,
                            target=step.target,
                            feeder=step.feeder,
                        )
                    case _:
                        assert_never(step)
                at = row.sent_at
        except Exception:  # noqa: BLE001 — a ledger write must never crash the driver
            logger.exception("spool_recovery: incident %s step %s could not be persisted", self.incident_id, seq)
        self.steps.append(replace(step, seq=seq, at=at))
        return seq

    async def answer(self, seq: int, outcome: LeverReading | ams_command.Answer, *, moved: bool | None = None) -> None:
        """Fill step ``seq``'s outcome — the reader's verdict (with ``moved``) for a lever,
        the classifier's answer for a command — in memory AND on the ledger."""
        from backend.app.core.database import async_session

        index = next((i for i, s in enumerate(self.steps) if s.seq == seq), None)
        if index is None:
            raise LookupError(f"spool_recovery: incident {self.incident_id} has no step {seq} to answer")
        step = self.steps[index]
        match step:
            case LeverStep():
                if not _is_reading(outcome):
                    raise LookupError(f"spool_recovery: lever step {seq} answered with {outcome!r}")
                self.steps[index] = replace(step, outcome=outcome, moved=moved)
            case CommandStep():
                if not _is_answer(outcome):
                    raise LookupError(f"spool_recovery: command step {seq} answered with {outcome!r}")
                self.steps[index] = replace(step, answer=outcome)
            case _:
                assert_never(step)
        try:
            async with async_session() as db:
                await printer_incidents.answer_step(db, self.incident_id, seq, outcome=outcome)
        except Exception:  # noqa: BLE001 — a ledger write must never crash the driver
            logger.exception("spool_recovery: incident %s step %s answer could not be persisted", self.incident_id, seq)

    # --- derived: every reading below is a projection of ``steps`` ---------------------

    @property
    def lever_steps(self) -> list[LeverStep]:
        return [s for s in self.steps if isinstance(s, LeverStep)]

    @property
    def command_steps(self) -> list[CommandStep]:
        return [s for s in self.steps if isinstance(s, CommandStep)]

    def _loads(self) -> list[CommandStep]:
        return [s for s in self.command_steps if s.command == "load"]

    def _unloads(self) -> list[CommandStep]:
        return [s for s in self.command_steps if s.command == "unload"]

    @property
    def tried(self) -> set[int]:
        """Every tray this incident sent a load for — the selection's exclusion, durable
        across a restart — except a load whose answer was LOST with its MQTT session
        (``session_changed``: nothing moved, and nobody can tell whether it ever ran). That
        tray was not tried; it was asked and not heard, so the fresh round after a reconnect
        selects it again and its resend is a new step. A load sent and never read at all (a
        crash between the two) stays tried — the conservative reading of a step nobody
        answered."""
        return {s.target for s in self._loads() if s.target is not None and s.answer != "session_changed"}

    @property
    def swap_committed(self) -> bool:
        """Has this incident crossed the swap-commit boundary? DERIVED from the log: the
        commit is made immediately before the swap's first command, so a command on the log
        IS the crossing — an unload, or, when there was nothing to unload
        (:func:`_unload_skippable`), the load that followed. A driver re-entered at startup
        reads the same log, so it never re-stamps or re-pages the jammed spool."""
        return bool(self.command_steps)

    @property
    def answer_lost(self) -> bool:
        """Is the LAST command this incident sent one whose answer was lost with its MQTT
        session (``session_changed``)? What an exhaustion that ran its rounds on reconnects
        must say instead of blaming the spools or the path."""
        steps = self.command_steps
        return bool(steps) and steps[-1].answer == "session_changed"

    @property
    def loads_attempted(self) -> int:
        """Distinct trays a load went out for. The give-up's restore reload is sent after
        the reason is chosen, so it never feeds one."""
        return len(self.tried)

    @property
    def loads_confirmed(self) -> int:
        """Distinct trays a load of was answered ``complete``."""
        return len({s.target for s in self._loads() if s.answer == "complete"})

    @property
    def unloads_sent(self) -> int:
        """Unload publishes that WENT OUT, answered or not."""
        return len(self._unloads())

    @property
    def confirmed_unloads(self) -> int:
        """Unloads the wire answered ``complete``. Since the swap waits for a replacement to
        be in hand (004-H2S 2026-09-17), every one of them PRECEDED a load attempt — which
        is why ``feed_path_blocked`` still means what it says: the AMS unloaded cleanly and
        the path still would not feed."""
        return sum(1 for s in self._unloads() if s.answer == "complete")

    @property
    def unload_moved_ams(self) -> bool:
        """Did a FARM unload MOVE the AMS (``complete`` or ``acted``)? The give-up's restore
        precondition: the restore undoes the driver's own motion, and an unload that was
        held, answered nothing, or moved nothing emptied nothing."""
        return any(s.answer in ("complete", "acted") for s in self._unloads())

    @property
    def held_command(self) -> CommandStep | None:
        """The LAST command the firmware acknowledged and held behind the print's change
        (``held``) — it runs at the next release, so nothing may be sent behind it."""
        return next((s for s in reversed(self.command_steps) if s.answer == "held"), None)

    @property
    def extruder_emptied_by_farm(self) -> bool:
        """Is this incident's last COMPLETED motion an unload — an extruder the FARM emptied
        and has not refilled? No release verb is pulled over it (2026-09-17 ruling: a
        resume over an extruder the farm emptied prints air)."""
        for s in reversed(self.command_steps):
            if s.answer == "complete":
                return s.command == "unload"
        return False

    def lever_spent(self, lever: Lever) -> bool:
        """Has ``lever`` been pulled its :data:`_MAX_LEVER_PULLS` times on this incident?
        A step counts from its SEND: a lever sent and never read (a crash between the two)
        is spent."""
        return sum(1 for s in self.lever_steps if s.lever == lever) >= _MAX_LEVER_PULLS

    def exhaustion_reason(self) -> str:
        """The honest escalation reason for running out of candidates/rounds."""
        if self.answer_lost:
            # The rounds ended on a command the printer's reconnect swallowed: nothing
            # about the spools or the path was measured by it.
            return "printer_offline"
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
    # not the spool, is the common factor, and since 006-H2S 2026-09-21 (incident 289)
    # neither does the JAMMED spool get parked: :func:`_commit_out_of_rotation` is the
    # one place that rule lives.
    extruder_side_only: bool
    # True when ANY physical-fault candidate is a pull-back the firmware could not
    # finish (``hms_errors`` ``retract_failure``). FROZEN at entry from the candidate
    # set that opened the incident, like every other fact here: the escalation copy is
    # composed later, possibly after the wire has moved on, and it must describe the
    # fault the operator was paged about. 006-H2S 2026-09-21, incident 289 — the copy
    # said "check the filament path, then resume" on a screen whose only button repeats
    # the pull-back against the stuck filament.
    retract_failure: bool
    layer_at_fault: int
    code: str
    printer_name: str
    job_name: str
    # The :func:`time.monotonic` reading when this driver context was built — the
    # instant the driver's ownership of the printer began. :func:`_takeover` reads an
    # operator's AMS command stamped at or after it
    # (``ams_command.operator_commanded_since``) as the operator taking the printer
    # over. Taken by the construction itself, so both construction sites (the entry
    # gate and the startup re-entry, through :func:`_build_incident`) carry it without
    # restating it.
    started_at: float = field(default_factory=time.monotonic)

    @property
    def is_feed_fault(self) -> bool:
        """Does the swap machine own this incident? (Derived — never stored twice.)"""
        return self.kind == KIND_JAM


def _build_incident(
    state,
    candidates: frozenset[FaultCandidate],
    *,
    incident_id: int,
    printer_id: int,
    job_id: str,
    settings: RecoverySettings,
    item_id: int | None,
    kind: str,
    code: str,
    fingerprint: str,
    tray: int | None,
    external: bool,
    printer_name: str,
    fallback_code: str | None = None,
) -> RecoveryIncident:
    """THE one construction of :class:`RecoveryIncident` (pinned by AST test).

    Two callers open an incident — the per-push entry gate (:func:`on_ams_fault`) and
    the startup re-entry (:func:`_reenter_recovering_incident`) — and they must produce
    the SAME facts from the same candidate set, or a restart silently re-reads a fault
    differently from the push that raised it. They had been two 17-field literals, each
    re-deriving ``extruder_side_only`` inline; the drift that invites is exactly the
    class the routing table (:func:`_route_fault`) was factored out to close.

    Everything DERIVED from the wire lives here — the code set, the two fault-shape
    flags, the layer and the job name — while everything the caller RESOLVED (the row
    id, the routed kind, the tray, the primary code) is passed in. ``fallback_code`` is
    the re-entry's one asymmetry: a wire with no actionable fault left still names the
    fault the stored row carried, because that row is all the evidence there is.
    """
    mechanical = {c for c in candidates if c.fault_class is AmsFaultClass.MECHANICAL_FEED}
    return RecoveryIncident(
        incident_id=incident_id,
        printer_id=printer_id,
        job_id=job_id,
        codes=frozenset(c.short_code for c in candidates)
        or (frozenset({fallback_code}) if fallback_code else frozenset()),
        fingerprint=fingerprint,
        item_id=item_id,
        settings=settings,
        jammed_global_tray=tray,
        kind=kind,
        external=external,
        extruder_side_only=bool(mechanical) and all(c.extruder_side for c in mechanical),
        # Read off the PHYSICAL candidates only: the flag answers "is the printer
        # holding a failed pull-back", and only that class can be one. A mechanical
        # sibling standing beside it (006's ``0700_0017``) says nothing about it.
        retract_failure=any(c.retract_failure for c in candidates if c.fault_class is AmsFaultClass.PHYSICAL_FAULT),
        layer_at_fault=int(getattr(state, "layer_num", 0) or 0),
        code=code,
        printer_name=printer_name,
        job_name=(getattr(state, "subtask_name", None) or "").strip() or "print",
    )


# --- Module edge state -------------------------------------------------------
# What remains in memory after WS2b is ONLY what is cheap to rebuild and harmless
# to lose. Every DECISION (already handled? printer already owned? flap cap spent?)
# now reads ``printer_incident`` rows — the four dicts that used to hold them
# (``_handled`` / ``_escalated`` / ``_success_counts`` / ``_runout_guidance_sent``)
# are DELETED: a restart emptied them while the standing HMS came straight back,
# and ``_escalated`` never expired inside a process, so a later different fault on
# the same job could never be recovered.

# Driver LIVENESS ("is the machine ACTING right now") lives in ONE store,
# ``printer_incidents`` (``register_driver`` / ``release_driver`` / ``driver_live``),
# beside the durable PROMISE it qualifies: the open incident row ("this printer is
# owned, and somebody will produce an outcome"). Entry exclusivity is the DB's partial
# unique index, never the liveness slot.
#
# Both questions have consumers and they are not interchangeable. The pause-stall
# watchdog asks both. Every OUTCOME WRITER asks liveness too, because the row alone
# cannot tell a driver that a closer freed it mid-procedure: while a driver lives it
# owns the outcome — at the running edge AND at a job terminal — and after it hands
# over the closers do (006-H2S 2026-09-04).
#
# ``sweep_open_incidents`` reads a ``recovering`` row as "a live driver has this",
# which is sound only because its ``_HOLD_OVER_DWELL_S`` dwell outlives any sink's
# window — do not turn that reading into a third spelling of ownership.

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
# (gcode_state, demanded slot or None, live actionable short codes, connection_epoch,
# feeding tray or None). Edges over this sample drive the refill auto-resume, the
# "the printer is running again" close, and the completed-load edge that is a
# ``physical`` hold's repair evidence. The epoch rides along because a sample from a
# PREVIOUS MQTT session cannot be compared for a code DISAPPEARING — a reboot wipes
# the standing HMS list, so every negative edge fires at once (2026-09-04, printer 8)
# — and for the same reason it cannot be compared for a tray CHANGING.
_wire_sample: dict[int, tuple[str, tuple[int, int] | None, frozenset[str], int, int | None]] = {}

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

# The MOTION evidence a ``repair`` hold ends on lives in
# ``incident_resolution.ledger`` — the rule that reads it owns it, and this module's
# per-push sampler stays its ONE writer (:func:`note_demand_watch`).

# Incidents whose self-heal resume has already gone out. One per incident: the
# resume is published on the FIRST sighting of repair evidence, before the sweep's
# dwell, and the row then closes on that same evidence ~120 s later — so without
# this the dwell window would publish one resume per tick.
_repair_resume_sent: set[int] = set()

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
        "unload_failed",  # the AMS did not complete the unload the farm sent
        "wedge_unreleased",  # every release verb pulled and the AMS still holds the print's change
        "swap_held_after_release",  # released, then the AMS held the swap command behind a change
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
        "ams_drying",  # a lockout the farm declined to fight; the AMS is healthy
        "recovery_interrupted",  # a restart artifact — kind-ambiguous, evidence of nothing
        "service_hold",  # the farm never tried: a human holds the printer, and the AMS is not the suspect
        "wedge_ended_print",  # a release verb ended the print — the verb's effect, not the feeder's
        "printer_offline",  # the printer stopped reporting — a network / power gap, not AMS hardware
    }
)


def _reset_state() -> None:
    """Test hook: clear module-level edge/dedup state between cases. The driver liveness
    slots are the incident store's and its own reset clears them."""
    _last_eval.clear()
    _guidance_sent.clear()
    _wire_sample.clear()
    _blocked.clear()
    _hold_over_since.clear()
    _repair_resume_sent.clear()
    ledger.reset()
    printer_incidents._reset_state()


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
    """One parseable INFO line after an unload/load answer other than ``complete``, at
    each restore and at candidate-loop end, carrying the live telemetry that explains WHY
    a step didn't take — candidate global tray, verdict, live tray_now,
    ams_status_main/sub, the pending tray target the firmware is honoring, and the
    recoverable codes STILL standing (without them a wedge line says the AMS did not
    move but never which fault it is holding)."""
    st = _get_state(incident.printer_id)
    logger.info(
        "[spool_recovery] candidate outcome printer=%s gtid=%s verdict=%s tray_now=%s "
        "ams_status=%s/%s pending_target=%s codes=%s",
        incident.printer_id,
        gtid,
        verdict,
        getattr(st, "tray_now", None) if st is not None else None,
        getattr(st, "ams_status_main", None) if st is not None else None,
        getattr(st, "ams_status_sub", None) if st is not None else None,
        getattr(st, "pending_tray_target", None) if st is not None else None,
        sorted(_active_recoverable_codes(st)),
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


def _active_recoverable_codes(state) -> frozenset[str]:
    """The short codes of the recoverable faults live on the printer state — every
    candidate the taxonomy classifies :data:`_RECOVERABLE_FAULT_CLASSES`, over both wire
    lanes (so the attr-lane-only ``0700_0012`` counts). Never raises:
    :func:`live_candidates` skips a malformed entry."""
    return frozenset(c.short_code for c in live_candidates(state) if c.fault_class in _RECOVERABLE_FAULT_CLASSES)


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
    from backend.app.services import farm_stall

    return RecoverySettings(
        enabled=await _read_bool(db, "spool_recovery_enabled", _DEFAULT_ENABLED),
        max_attempts=await _read_int(db, "spool_recovery_max_attempts", _DEFAULT_MAX_ATTEMPTS),
        step_timeout_s=float(await _read_int(db, "spool_recovery_step_timeout_s", _DEFAULT_STEP_TIMEOUT_S)),
        protect_layers=await _read_int(db, "spool_recovery_protect_layers", _DEFAULT_PROTECT_LAYERS),
        offline_bound_s=await farm_stall.offline_stall_seconds(db),
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


def _candidate_slots(candidates, fault_class: AmsFaultClass) -> list[tuple[int, int]]:
    """Every DISTINCT slot the FIRMWARE named for one class, lowest first.

    Only the attr-aware ``hms[]`` lane carries a slot (the short-code lane discarded
    the attr low byte), and only the per-slot families use it — a jam's 8010 code names
    the AMS unit, never the tray. Sorted so the answer is stable; de-duplicated because
    two entries of the same family on one slot are one slot, and the callers below are
    asking HOW MANY slots the class names as much as which.
    """
    return sorted({c.slot for c in candidates if c.fault_class is fault_class and c.slot is not None})


def _candidate_slot(candidates, fault_class: AmsFaultClass) -> tuple[int, int] | None:
    """The LOWEST slot the class names, or None. Stable, and arbitrary when it is not
    alone — which is why a caller that must not arbitrate asks
    :func:`_sole_candidate_slot` instead."""
    slots = _candidate_slots(candidates, fault_class)
    return slots[0] if slots else None


def _sole_candidate_slot(candidates, fault_class: AmsFaultClass) -> tuple[int, int] | None:
    """The slot the class names when it names EXACTLY one; ``None`` when it names
    several (or none).

    The difference from :func:`_candidate_slot` is the whole point: that one picks the
    lowest of several, which is a deterministic answer to a question nobody asked. A
    caller borrowing another class's attribution (the PHYSICAL fallback below) must
    refuse to arbitrate — a slot chosen by sort order would be a confident lie about
    which tray a human should go and look at.
    """
    slots = _candidate_slots(candidates, fault_class)
    return slots[0] if len(slots) == 1 else None


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
    * PHYSICAL — the attr-named slot, then the slot a CO-STANDING mechanical-feed
      candidate names, and only when those name exactly ONE distinct slot. A physical
      fault rarely carries its own attribution (006-H2S 2026-09-21: ``0700_8003``
      names none) while the mechanical sibling it arrived with does (``0700_0017@0-0``)
      — they are one physical event on one path, so borrowing that attribution is
      reading the evidence, not inventing it. Two distinct slots means the wire is
      describing more than one tray and the fallback REFUSES rather than arbitrating;
      the lowest-sorted slot would be a confident lie. These escalate either way, so a
      missing slot costs a less specific message, never a wrong one.
    * JAM — :func:`_resolve_jammed_tray`; the 8010 family carries no slot attribution.

    Every ``(ams_id, tray_id)`` becomes a global tray through the ONE codec
    (``spool_respool.encode_global_tray``, invariant 1), which knows the AMS-HT and
    vt_tray conventions a bare ``ams_id * 4 + tray_id`` silently drops. A slot the
    codec cannot NAME falls through to the next tier exactly as a missing slot does —
    that is the fail-closed reading, not a new one: an AMS-HT unit encoded by hand
    produced a well-formed id belonging to some OTHER real slot, and a wrong tray is
    worse than none on every surface this answer reaches (the operator's refill
    instruction, the out-of-rotation stamp, the incident row).
    """
    if external:
        return None, "single"
    if kind == KIND_RUNOUT:
        demand = current_runout_demand(getattr(state, "hms_errors", None) or [])
        if demand is not None:
            tray = encode_global_tray(*demand)
            if tray is not None:
                return tray, "single"
    if kind in (KIND_RUNOUT, KIND_PHYSICAL):
        slot = _candidate_slot(
            candidates, AmsFaultClass.RUNOUT if kind == KIND_RUNOUT else AmsFaultClass.PHYSICAL_FAULT
        )
        tray = encode_global_tray(*slot) if slot is not None else None
        if tray is not None:
            return tray, "single"
        if kind == KIND_PHYSICAL:
            # The 006 shape: the physical code names no slot, but the mechanical-feed
            # candidate standing WITH it does. One event, one path — so its attribution
            # carries, and only when it is unambiguous.
            slot = _sole_candidate_slot(candidates, AmsFaultClass.MECHANICAL_FEED)
            tray = encode_global_tray(*slot) if slot is not None else None
            return tray, "single"
    return _resolve_jammed_tray(state, candidates=candidates, item=item, printer_id=printer_id)


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
        tray = tray_fields.valid_feeder(entry[0] if isinstance(entry, (tuple, list)) and entry else entry)
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
       one, and when it does it is the firmware naming the tray directly. Encoded
       through the one codec, so a slot it cannot NAME falls to tier 2 rather than
       becoming a well-formed id belonging to a different tray.
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
    tray = encode_global_tray(*slot) if slot is not None else None
    if tray is not None:
        return tray, "single"

    feeders, _source = _job_feeders(state, item, printer_id)
    if len(feeders) > 1:
        return None, "multi_feeder"

    live = tray_fields.valid_feeder(getattr(state, "tray_now", None))
    if live is None:
        live = tray_fields.valid_feeder(getattr(state, "last_loaded_tray", None))
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
            tray = tray_fields.valid_feeder(value)
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
    fed = tray_fields.valid_feeder(getattr(state, "last_loaded_tray", None))
    if fed is not None:
        return fed
    feeders, _source = _job_feeders(state, item, printer_id)
    if len(feeders) == 1:
        return feeders[0]
    return tray_fields.valid_feeder(getattr(state, "tray_now", None))


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


# --- where the filament is, and what to tell the operator about it -----------

FeederKind = Literal["jammed", "empty", "other", "external", "unknown"]

# What the DRIVER did about an extruder IT emptied, stated by the driver and never
# inferred from the wire (:func:`_restore_jammed_feeder`). Five jam-kind reasons reach
# :func:`_escalate` straight from the entry gate with nothing unloaded, and after a feed
# fault ``tray_now`` frequently already reads 255 — so a wire-inferred "was unloaded"
# would attribute an act to the farm that never happened, and a bare boolean would claim
# a failed reload on the give-ups where none is attempted. ``fail`` = every reload that
# never reached ``complete`` — one the AMS did not move on, or held behind the print's
# change, included (the page's evidence sentence names a held one).
RestoreVerdict = Literal["ok", "fail", "skipped_drying"]


@dataclass(frozen=True)
class FeederPosition:
    """Where the filament sits right now, read RELATIVE to this incident's jammed tray.

    ONE owner for that question. Nine one-line ``tray_now`` comparisons in this module
    each ask a different one ("is the AMS unloaded", "did the load take", "did the
    operator resume on the jammed feeder"); this is the only one that classifies the
    feeder for the operator page and for the give-up's restore decision, and neither
    projection may re-derive it from ``tray_now`` itself (pinned by test).

    ``kind``:

    * ``jammed``  — the jammed tray is still the feeder (nothing was unloaded, or a
      restore put it back);
    * ``empty``   — the 255 sentinel: NOTHING is feeding. After a feed fault that is
      never "the path is clear" (invariant 8);
    * ``other``   — a different regular tray feeds (a replacement that loaded, or an
      AMS-HT id that has no letter+slot name);
    * ``external``— the external spool holder (254): no AMS slot is involved;
    * ``unknown`` — no live state, no jammed tray to be relative to, or a ``tray_now``
      that names neither a tray nor a sentinel.

    ``global_tray`` is the slot the operator must be TOLD about — the jammed tray for
    ``jammed``/``empty`` (the one still loaded, or the one that was unloaded), the tray
    now at the feeder for ``other``, and None where there is nothing to name (which is
    why no clause can ever render "tray None").
    """

    kind: FeederKind
    global_tray: int | None


def _feeder_position(state, jammed_global_tray: int | None, printer_id: int | None) -> FeederPosition:
    """Classify the feeder against ``jammed_global_tray``. Never raises.

    Witness order is :func:`slot_was_feeding`'s, for its reason: on dual-nozzle
    hardware ``tray_now`` is SINGLE-valued and describes only the active hotend, so a
    jammed slot feeding the other nozzle would read ``empty`` here and a restore would
    load on top of filament that never left. The per-extruder map answers first, and
    only a machine that carries none falls through to ``tray_now``.
    """
    if state is None or jammed_global_tray is None:
        return FeederPosition("unknown", None)
    feeders = _dual_nozzle_feeders(state, printer_id)
    if jammed_global_tray in feeders:
        return FeederPosition("jammed", jammed_global_tray)
    if feeders:
        # A hotend IS fed, just not from the jammed slot — mapping order decides which
        # one the page names, exactly as the mapping witnesses order themselves.
        return FeederPosition("other", feeders[0])
    raw = getattr(state, "tray_now", None)
    feeder = tray_fields.valid_feeder(raw)
    if feeder is not None:
        return FeederPosition("jammed" if feeder == jammed_global_tray else "other", feeder)
    if raw == _NO_FILAMENT:
        return FeederPosition("empty", jammed_global_tray)
    if raw == tray_fields.TRAY_NOW_EXTERNAL_SPOOL:
        return FeederPosition("external", None)
    return FeederPosition("unknown", None)


# The reasons whose feeder clause names the slot and gives NO instruction: the AMS holds
# a change (the print's own, or one it re-entered holding the swap's command), so the
# copy's own last sentence (Retry) is the one action and a "then resume" beside it would
# contradict it — the ``_retract_clause`` lesson.
_CLAUSE_WITHOUT_INSTRUCTION: frozenset[str] = frozenset({"wedge_unreleased", "swap_held_after_release"})
# The reasons that carry no feeder clause at all: the print is over, so where the
# filament sits is the next job's question, never an instruction about this one.
_NO_FEEDER_CLAUSE: frozenset[str] = frozenset({"wedge_ended_print"})


def _feeder_clause(position: FeederPosition, restore: RestoreVerdict | None, *, reason: str) -> str | None:
    """The sentence a jam escalation appends to its static reason copy, or None.

    The reason copy says WHY the farm gave up; this says what state the printer is in
    while it waits, which is the half incident 192 (004-H2S 2026-09-17) was missing:
    the page never said the extruder was empty, so a Resume was the natural next click
    and the print ran four hours on air.

    ``restore`` is the driver's own statement about an extruder IT emptied, so a
    give-up that unloaded nothing renders nothing about unloading. Runout / physical /
    external kinds get no clause at all (their copy already carries the slot
    instruction, and the swap machine never moved their filament). ``reason`` gates the
    instruction half (:data:`_CLAUSE_WITHOUT_INSTRUCTION`, :data:`_NO_FEEDER_CLAUSE`),
    the ``_retract_clause`` pattern — one instruction per page, never two.
    """
    if reason in _NO_FEEDER_CLAUSE:
        return None
    instruct = reason not in _CLAUSE_WITHOUT_INSTRUCTION
    slot = runout_slot_desc(position.global_tray) or f"tray {position.global_tray}"
    if position.kind == "jammed":
        state = (
            f"The jammed spool was unloaded and reloaded ({slot})."
            if restore == "ok"
            else f"The jammed spool is still loaded ({slot})."
        )
        return f"{state} Clear the extruder, then resume on the printer." if instruct else state
    if position.kind == "empty":
        if restore == "fail":
            state = f"No filament is loaded: {slot} was unloaded and the reload failed."
            return f"{state} Check the filament path, load a spool, then resume on the printer." if instruct else state
        if restore == "skipped_drying":
            state = f"No filament is loaded: {slot} was unloaded; the AMS is drying, so no reload was attempted."
            return f"{state} Load a spool after the cycle, then resume on the printer." if instruct else state
        # The farm did not empty it and cannot say why it reads empty — 255 is
        # "nothing is feeding", not "the path is clear" (invariant 8). The reason
        # copy stands on its own.
        return None
    if position.kind == "other":
        return f"{slot} is loaded."
    return None


def _retract_clause(incident: RecoveryIncident) -> str:
    """The sentence a PHYSICAL escalation appends: what to do at the screen.

    The physical class's static copy says what the fault IS; this says what the one
    button in front of the operator will do. 006-H2S 2026-09-21 (incident 289) is why
    the distinction is load-bearing: the printer sat latched in a pull-back the farm's
    own unload had commanded, and the page said "check the filament path, then resume"
    — but there was no Resume on that screen. Retry/CONTINUE on a ``0700_8003`` re-runs
    the pull-back, against the same filament that would not come out, which is the
    action most likely to break it off inside the extruder.

    Reads the FROZEN :attr:`RecoveryIncident.retract_failure`, never the live wire: the
    page describes the fault the operator is being called about, and by the time the
    detail is composed the firmware may already have moved on.

    The otherwise-arm carries the resume instruction that the ``physical_fault`` copy
    gives up, so exactly one of the two sentences is ever rendered — which is also why
    :func:`_compose_detail` calls this for that reason ALONE: a reason whose own copy
    still ends in "resume on the printer" would then say it twice.
    """
    if incident.retract_failure:
        return (
            "The printer is holding a failed filament pull-back: Retry on the screen repeats the pull-back. "
            "Free the filament at the extruder first, then press Retry."
        )
    return "Then resume on the printer."


def _command_slot(step: CommandStep) -> str:
    """The slot a command step named, through the one slot-name origin."""
    return runout_slot_desc(step.target) or f"tray {step.target}"


def _step_label(step: LeverStep | CommandStep) -> str:
    """The page's name of one step: the lever table's own text, ``unload``, or
    ``load <slot>``."""
    match step:
        case LeverStep():
            return _lever(step.lever).text
        case CommandStep():
            return "unload" if step.command == "unload" else f"load {_command_slot(step)}"
        case _:
            assert_never(step)


def _step_outcome(step: LeverStep | CommandStep) -> str:
    """The step's recorded verdict token — the reader's for a lever, the classifier's for a
    command — or ``no answer`` for a step sent and never read."""
    match step:
        case LeverStep():
            token = step.outcome
        case CommandStep():
            token = step.answer
        case _:
            assert_never(step)
    return token if token is not None else "no answer"


def _held_sentence(step: CommandStep) -> str:
    """What a HELD command will do: it runs, in order, at the next release (012-H2S
    2026-09-23 — six held commands ran after the terminal). A held unload empties the
    extruder at that release, which is what the operator must be told before pressing
    Retry."""
    if step.command == "unload":
        return (
            "An unload is held in the AMS and runs at the next release; load a slot when the printer asks for filament."
        )
    return f"A load of {_command_slot(step)} is held in the AMS and runs at the next release."


def _evidence_sentence(evidence: _RecoveryEvidence) -> str | None:
    """ONE sentence of what the driver sent and what the wire answered, from its LOG alone
    — every step in send order, consecutive identical ``label: outcome`` runs collapsed:
    ``Sent: resume: wedged; ams_control resume: released; unload: held.`` — then, when the
    firmware holds a command, what that command will do. None when nothing was sent."""
    runs: list[tuple[str, str, int]] = []
    for step in sorted(evidence.steps, key=lambda s: s.seq):
        label, outcome = _step_label(step), _step_outcome(step)
        if runs and runs[-1][0] == label and runs[-1][1] == outcome:
            runs[-1] = (label, outcome, runs[-1][2] + 1)
        else:
            runs.append((label, outcome, 1))
    if not runs:
        return None
    sentence = (
        "Sent: "
        + "; ".join(f"{label}{f' ×{count}' if count > 1 else ''}: {outcome}" for label, outcome, count in runs)
        + "."
    )
    held = evidence.held_command
    return sentence if held is None else f"{sentence} {_held_sentence(held)}"


def _compose_detail(
    incident: RecoveryIncident,
    reason: str,
    *,
    restore: RestoreVerdict | None,
    evidence: _RecoveryEvidence | None,
) -> str:
    """THE human detail of an escalation: static reason copy plus the kind's clauses.

    The reason copy says WHY the farm gave up — one string per reason token, shared by
    every kind that can reach it. The clauses say what the farm DID and what the
    operator is walking up to, and those ARE kind-specific, so they are keyed on the
    incident's kind rather than woven into the reason table (which would multiply out to
    reason × kind copies).

    Exactly two kinds carry any:

    * JAM → :func:`_evidence_sentence` (what the driver sent and what the wire
      answered — ``evidence`` is a PARAMETER, the incident's own log, never re-read
      from the wire here; ``None`` for the entry escalations, which sent nothing), then
      :func:`_feeder_clause` — where the filament sits, read off the live wire plus the
      driver's own restore verdict (incident 192's missing half), gated by the reason.
    * PHYSICAL, and only under the ``physical_fault`` reason → :func:`_retract_clause`
      — what the screen's Retry will do (incident 289's missing half).

    A RUNOUT or an EXTERNAL fault gets none: their copy already carries the slot
    instruction, and the swap machine never moved their filament.

    The physical clause is REASON-gated where the feed one is not, and the asymmetry is
    the evidence, not caution. ``physical_fault`` is the one physical reason a live
    candidate set produced, so it is the one whose ``retract_failure`` reading means
    anything; the other reason a physical row can carry is ``recovery_interrupted``,
    minted by the startup re-entry when the wire has NO actionable fault left — the
    flag is False by construction there, and appending the otherwise-arm printed "and
    resume on the printer. Then resume on the printer." One instruction, once.
    """
    detail = _ESCALATE_DETAIL.get(reason, reason)
    clauses: list[str] = []
    if incident.is_feed_fault:
        sent = _evidence_sentence(evidence) if evidence is not None else None
        # Where the filament sits is read off a LIVE wire only (:func:`_reads_live`): a
        # printer off its session still carries its last session's cached ``tray_now``,
        # and a clause read from it would describe a printer the farm cannot see.
        st = _get_state(incident.printer_id)
        feeder = _feeder_clause(
            _feeder_position(st if _reads_live(st) else None, incident.jammed_global_tray, incident.printer_id),
            restore,
            reason=reason,
        )
        clauses = [clause for clause in (sent, feeder) if clause is not None]
    elif incident.kind == KIND_PHYSICAL and reason == "physical_fault":
        clauses = [_retract_clause(incident)]
    return " ".join([detail, *clauses])


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


def _held_escalate_reason(kind: str, *, external: bool) -> str:
    """The reason a fault escalates with on a printer in MAINTENANCE MODE.

    Only consulted when :func:`_route_fault` produced no reason of its own, i.e. when
    the fault was going to run the machine. A fault that already escalates keeps ITS
    reason — ``physical_fault``, ``external_feed_fault``, ``multi_feeder_job``,
    ``repeated_jams`` — because the hold changes what the farm DOES, never what the
    fault WAS, and the operator reading the ledger during a maintenance window needs the
    diagnosis rather than a note that they were holding the printer.

    A RUNOUT is the case that makes this a function instead of a constant. It routes
    ``None`` only because the DRIVER escalates it after confirming the PAUSE, so with no
    driver its held-runout reason has to be supplied directly (the same reason, and the
    same line, as the upgrade branch in :func:`on_ams_fault`) — and it MATTERS which one:
    ``_escalate`` stamps the exhausted roll spent only for ``runout_needs_refill``, and
    that stamp is the durable exhaustion record a hold spanning a deploy relies on.
    Stamping it is OBSERVATION; a maintenance hold suppresses ACTS, never the ledger.

    Everything left is driver-bound work the hold is refusing — a farm mechanical jam,
    a wedged filament change — and that is what ``service_hold`` names.
    """
    if kind == KIND_RUNOUT:
        return "external_spool_runout" if external else "runout_needs_refill"
    return "service_hold"


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
        # The printer's own words for the faults this incident will speak for, recorded
        # now: a release lever, a stop or the next job clears them off the printer while
        # the hold they explain still stands.
        full_codes = full_codes_of(getattr(state, "hms_errors", None) or [], {c.short_code for c in candidates})

        from backend.app.core.database import async_session
        from backend.app.models.printer import Printer

        incident: RecoveryIncident | None = None
        escalate_reason: str | None = None
        async with async_session() as db:
            settings = await _read_settings(db)
            if not settings.enabled:
                _note_outcome(printer_id, job_id, fingerprint, "not owned — recovery disabled by setting")
                return None

            # The AMS-kind row only: a pause-cause hold (power loss, plate vision, a
            # lost Z frame) standing beside an AMS fault never blocks the fault from
            # being owned — that is the multi-alarm rule.
            existing = await printer_incidents.get_open(db, printer_id, kinds=AMS_FAULT_KINDS)
            if existing is not None and not _outranks(fault_class, existing.kind):
                _note_outcome(
                    printer_id,
                    job_id,
                    fingerprint,
                    "not opened — this printer already has an open AMS incident",
                    detail=f"incident {existing.id} {existing.status} kind={existing.kind}",
                )
                return None

            if existing is None and printer_incidents.job_pause_held(printer_id):
                # The printer PAUSED this job to ask a human about the plate (its own
                # pre-print check), and every act this machine owns ends in a resume — a
                # release lever, the swap round's resume, the refill auto-resume. Any of
                # them would restart the print onto the plate the printer refused. So no
                # new AMS incident is opened while that question stands: the human's
                # resume ends the job pause, and a fault still standing then is owned on
                # the very next push (and its raw alert reaches them meanwhile —
                # ``will_own`` mirrors this gate). An AMS row ALREADY open keeps its
                # owner; the upgrade below only re-classifies it and resumes nothing.
                _note_outcome(
                    printer_id,
                    job_id,
                    fingerprint,
                    "not owned — the printer paused this job for a human (plate check); the answer comes first",
                )
                return None

            # The upgrade branch below runs BEFORE the aborted-re-entry bar on purpose:
            # an upgrade escalates the row that already owns the printer, it never
            # re-enters a machine, so a barred fingerprint is not a reason to leave
            # the row on its milder reading.
            if existing is None and await _fault_already_closed(db, printer_id, job_id, fingerprint):
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
            if printer_incidents.automation_held(printer_id):
                # MAINTENANCE MODE: hands are in this machine, so the farm records the
                # fault and touches nothing. The row still OPENS — that is what makes
                # ``hold_blocks_dispatch`` refuse work after the hold lifts, until the
                # fault resolves by its own wire/repair rule — and ``will_own`` still
                # suppresses the duplicate raw HMS page, because this incident is the
                # record of that fault. What a hold removes is the ACT: no ``_run_recovery``
                # driver, no swap, no auto-resume. Read HERE, right after the routing
                # table, so the row opens ESCALATED rather than as a ``recovering`` row
                # with no driver behind it.
                #
                # ``or``, never an override: a fault that already escalates keeps its own
                # reason, so the ledger and the guidance still name the diagnosis and a
                # runout still stamps its roll spent. ``service_hold`` is only what a
                # DRIVER-BOUND fault takes instead of ``_run_recovery``.
                escalate_reason = escalate_reason or _held_escalate_reason(kind, external=external)

            upgraded_from: str | None = None
            if existing is not None:
                # UPGRADE: the live set now carries a class that OUTRANKS the open row
                # (003-H2S 2026-09-11 — ``0700_0012`` opened a jam 1.2 s before
                # ``0700_8004`` said the filament was physically stuck in the path,
                # and the swap machine spent the whole hold on the milder reading).
                # Every kind that outranks a jam escalates AT ENTRY, so an upgrade
                # always lands ESCALATED with one page and never a driverless
                # ``recovering`` row: a runout routes ``None`` only because the DRIVER
                # escalates it after confirming the PAUSE, and no driver is spawned
                # here, so its held-runout reason is supplied directly.
                if escalate_reason is None:
                    escalate_reason = "external_spool_runout" if external else "runout_needs_refill"
                # Read BEFORE the CAS: ``existing`` is the same identity-mapped row the
                # store is about to mutate in this session.
                upgraded_from = existing.kind
                row = await printer_incidents.upgrade(
                    db,
                    existing.id,
                    kind=kind,
                    code=code,
                    codes=fingerprint,
                    slot_global_tray=tray,
                    hms_full_codes=full_codes,
                )
                if row is None:
                    _note_outcome(printer_id, job_id, fingerprint, "not upgraded — the open incident closed underneath")
                    return None
            else:
                row = await printer_incidents.open_new(
                    db,
                    printer_id=printer_id,
                    job_id=job_id,
                    item_id=item.id if item is not None else None,
                    kind=kind,
                    code=code,
                    codes=fingerprint,
                    slot_global_tray=tray,
                    hms_full_codes=full_codes,
                    status=STATUS_ESCALATED if escalate_reason is not None else STATUS_RECOVERING,
                )
                if row is None:
                    _note_outcome(printer_id, job_id, fingerprint, "not opened — lost the open-incident race")
                    return None

            printer = await db.get(Printer, printer_id)
            incident = _build_incident(
                state,
                candidates,
                incident_id=row.id,
                printer_id=printer_id,
                job_id=job_id,
                settings=settings,
                item_id=item.id if item is not None else None,
                kind=kind,
                code=code,
                fingerprint=fingerprint,
                tray=tray,
                external=external,
                printer_name=(printer.name if printer else None) or f"printer {printer_id}",
            )

        _note_outcome(
            printer_id,
            job_id,
            fingerprint,
            f"incident {incident.incident_id} "
            + (f"UPGRADED {upgraded_from}->{kind}" if upgraded_from is not None else "opened"),
            detail=f"kind={kind} {'foreign' if incident.item_id is None else f'item={incident.item_id}'}"
            + (f" escalating={escalate_reason}" if escalate_reason else ""),
        )

        # Session closed — escalate/spawn outside it (helpers open their own).
        if escalate_reason is not None:
            await _escalate(incident, escalate_reason)
            return None

        task = asyncio.create_task(_run_recovery(incident))
        printer_incidents.register_driver(printer_id, task, incident_id=incident.incident_id)
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
        # The AMS-kind row: a pause-cause hold is not an AMS fault's owner, and a
        # plate-vision row standing beside a jam must not silence the jam's raw alert.
        if await printer_incidents.get_open(db, printer_id, kinds=AMS_FAULT_KINDS) is not None:
            return True
        # Mirrors the entry gate: while the printer's job is paused for a human (its own
        # plate check), no NEW AMS incident is opened, so nothing speaks for this fault
        # and its raw alert is the only word the operator gets about it.
        if printer_incidents.job_pause_held(printer_id):
            return False
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
    """The recovery driver task: hydrate the incident's evidence log, run the state machine
    (:func:`_drive_recovery`), and answer its one exception — the offline bound
    (:class:`_PrinterOffline`) — with the ``printer_offline`` give-up. Never raises;
    always releases its liveness slot on exit (``printer_incidents.release_driver``)."""
    pid = incident.printer_id
    try:
        from backend.app.core.database import async_session

        # The incident's evidence log, as the durable step ledger holds it — empty for a
        # fresh incident, the previous driver's steps for a startup re-entry, so a
        # restarted driver never re-pulls a lever it already spent. Read FIRST: every
        # give-up below, the offline one included, pages from it.
        async with async_session() as db:
            evidence = await _RecoveryEvidence.from_row(db, incident.incident_id)
        try:
            await _drive_recovery(incident, evidence)
        except _PrinterOffline as gap:
            logger.warning(
                "spool_recovery: printer %s incident %s — the printer stayed off its session past the offline bound "
                "(%.0fs); giving up printer_offline",
                pid,
                incident.incident_id,
                gap.bound_s,
            )
            await _give_up(incident, "printer_offline", evidence=evidence)
    except Exception:  # noqa: BLE001 — the driver must never crash the event loop
        logger.exception("spool_recovery: recovery driver crashed for printer %s", pid)
    finally:
        task = asyncio.current_task()
        if task is not None:
            printer_incidents.release_driver(pid, task)
        # HANDOVER. :func:`on_observed_running` defers to a live driver, and the wire
        # sampler that calls it is EDGE-triggered — so an edge deferred while this
        # driver sat inside ``_escalate`` (tray snapshot, DB, page, quarantine, spent
        # stamp — seconds) has no second chance, and would leave an ESCALATED row open
        # on a demonstrably RUNNING printer with nothing left to close it but
        # ``sweep_open_incidents``' 120 s dwell (and, for a code the firmware leaves
        # standing, not even that): the chip lit, the hourly nag armed and the queue
        # token held to the terminal — the pre-WS2b class.
        #
        # Derive-don't-store: nothing records "an edge was deferred"; the LEVEL is
        # re-read once, here, where the slot is already free. A no-op when the row is
        # already closed, and never a second closer — this IS the closer. Only a LIVE
        # RUNNING counts (:func:`_reads_live`): a printer the driver gave up on as offline
        # still carries its dead session's cached RUNNING, and closing on it would end the
        # very hold that ``printer_offline`` page just raised.
        st = _get_state(pid)
        if st is not None and _reads_live(st) and _live_state(st) == "RUNNING":
            await on_observed_running(pid)


async def _drive_recovery(incident: RecoveryIncident, evidence: _RecoveryEvidence) -> None:
    """The recovery state machine (numbered per the plan), inside :func:`_run_recovery`'s
    guard. Every read of the wire goes through the session gate (:func:`_live_reading`),
    whose :class:`_PrinterOffline` is the caller's to answer."""
    pid = incident.printer_id
    # (0) A driver needs a printer that is reporting: a startup re-entry can run before
    #     its client connects, and a session can drop between the push that raised the
    #     fault and this task starting. Not a close — a wait (:func:`_live_reading`);
    #     past the offline bound it is the ``printer_offline`` give-up.
    await _live_reading(incident)

    # (1) The fault PAUSEs the print; a runout the firmware backup rescued never
    #     PAUSEs → close silently as TRANSIENT. Closed as ``aborted`` with no
    #     resolve_source (no actor can claim an outcome nobody produced), which
    #     also bars re-entry for this exact fault on this job: the firmware can
    #     leave the code standing after healing itself, and the per-push entry
    #     gate would otherwise re-open an incident every throttle window. A session
    #     gap inside the wait is not "never PAUSEd": the wait stops its clock for it.
    if not await _await_live_state(incident, frozenset({"PAUSE"}), incident.settings.step_timeout_s):
        await _close_incident(incident, status=STATUS_ABORTED, source=None)
        logger.info("spool_recovery: printer %s never PAUSEd (firmware rescue / transient) — incident closed", pid)
        return

    # The PAUSE landed. Before the FIRST write about this incident (the projection
    # onto the unit), ask whether the row is still ours to write about: an upgrade
    # that arrived while we waited (003-H2S) has already projected the WORSE kind's
    # token, and a stop that arrived (002-H2S) has already ended the job.
    if await _takeover_exit(incident, awaiting="PAUSE", step="paused"):
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

    # (3) Up to _MAX_CANDIDATES rounds, ONE body for every posture. While the AMS holds
    #     the print's own filament change the round runs the RELEASE LADDER and sends
    #     NO swap command into the change (012-H2S 2026-09-23: every command sent into
    #     it was acknowledged, held, and run at the next release — a held unload
    #     empties the extruder mid-print). Out of the change, the doctrine contract
    #     runs: select, commit, unload, load, resume — each answer read through ONE
    #     reader. Every verdict is a closed Literal dispatched by ``match`` with
    #     ``assert_never``, so a verdict nobody handled cannot fall through into the
    #     next step (a bare ``str`` once fell past ``if load == "fail"`` into a resume).
    #
    # The commit boundary (a replacement in hand) is crossed exactly ONCE per incident,
    # never at entry — so a no-swap self-heal leaves the spool untouched. What the
    # crossing DOES about the spool is `_commit_out_of_rotation`'s alone: on an
    # extruder-side fault it parks nothing, which is why the boundary is read off the
    # evidence log (:attr:`_RecoveryEvidence.swap_committed` — a swap command went out)
    # rather than off a stamp, and never off a local flag a startup re-entry would lose.
    #
    # A command whose answer was lost with its MQTT session (``session_changed``) ends
    # the round, never the incident: the next round starts from the live wire exactly as
    # a fresh round does, and its resend is a new step on the log.
    for _round in range(_MAX_CANDIDATES):
        if await _takeover_exit(incident, awaiting="PAUSE", step="round_start"):
            return

        # (a) The AMS's own state word decides whether the swap may go out at all.
        st, _waited = await _live_reading(incident)
        if ams_mid_filament_change(st):
            ladder = await _release_ladder(incident, evidence=evidence)
            match ladder:
                case "self_healed":
                    await _close_self_healed(incident)
                    return
                case "swapped":
                    # Before any commit the farm loaded nothing, so another tray at the
                    # feeder is the FIRMWARE's own switch (its backup group). The print
                    # runs on a quiet path; it is the self-heal close, said out loud.
                    logger.warning(
                        "spool_recovery: printer %s RUNNING on a quiet path with tray %s feeding and nothing "
                        "committed — the firmware switched feeders itself; closing as a self-heal",
                        pid,
                        getattr(_get_state(pid), "tray_now", None),
                    )
                    await _close_self_healed(incident)
                    return
                case "released":
                    pass  # the AMS is out of the change: the swap may go out
                case "unreleased":
                    await _give_up(incident, "wedge_unreleased", evidence=evidence)
                    return
                case "withheld":
                    # The farm emptied this extruder and the AMS is in a change again:
                    # no verb may resume over it, and no swap may go into it. The loads
                    # are what failed — the reason says so.
                    await _give_up(incident, evidence.exhaustion_reason(), evidence=evidence)
                    return
                case "ended":
                    await _close_ended_by_driver(incident, evidence=evidence)
                    return
                case "abort":
                    await _abort(incident)
                    return
                case "handover":
                    _hand_over(incident)
                    return
                case _:
                    assert_never(ladder)

        # (b) LOOK BEFORE YOU LEAP. Selection runs here, ahead of the stamp and the
        # unload, because everything below it COMMITS the swap: 004-H2S 2026-09-17
        # (incident 192) stamped a spool out of rotation and unloaded it, and only
        # then asked what to load — the honest answer was "nothing", and the printer
        # was handed to a human with an empty extruder that no copy described.
        # Selection touches no feeder (it reads tray telemetry and may force a
        # bare-tray config write), so running it first costs the swap nothing and
        # buys the round the right to abandon cleanly: with no candidate to load there
        # is no unload, and invariants 7 (the stamp is the swap-commit boundary) and 8
        # (an unload is unconditional BEFORE A LOAD) both hold literally (shape 39).
        target, only_low = await _select_replacement(incident, evidence.tried)
        if target is None:
            # A takeover during the (possibly bounded) selection / forced
            # bare-tray sweep means someone else is in control — abort rather
            # than escalate, mirroring every other step.
            st, _waited = await _live_reading(incident)
            token = _takeover(incident, st, awaiting="PAUSE")
            if token is not None:
                verdict = _note_takeover(incident, token, "select_replacement")
                if verdict == "handover":
                    _hand_over(incident)
                else:
                    await _abort(incident, token=token)
                return
            # ``only_low`` is reachable only INSIDE the protected layers: at or after
            # them the ledger floor is off (:func:`_match_candidates`), so a present,
            # not-spent spare is never withheld for its grams.
            reason = "only_low_spools_in_protected_layers" if only_low else evidence.exhaustion_reason()
            await _give_up(incident, reason, evidence=evidence)
            return

        # A replacement is in hand → the swap COMMITS, ONCE, right before the first
        # unload. Boundary semantics: a stamp written here means "recovery is
        # abandoning this spool", so every give-up that can follow this point
        # (ams_drying, unload_failed, swap_held_after_release, load exhaustion) correctly
        # KEEPS it; only a clean swap-and-resume, a self-heal on the jammed feeder or
        # an external-takeover abort resolves it (the latter's _clear reverses it
        # when the operator resumed on the jammed feeder — never on a feeder the
        # driver restored). Whether a stamp is written at all is
        # `_commit_out_of_rotation`'s call: an extruder-side fault commits the SWAP
        # and parks nothing (006-H2S 2026-09-21, incident 289).
        if not evidence.swap_committed and incident.jammed_global_tray is not None:
            await _commit_out_of_rotation(incident, incident.jammed_global_tray, role="jammed")

        # (c) The unload — unconditional before a load (invariant 8), never behind a
        # held command.
        unload = await _unload_and_confirm(incident, evidence=evidence, attempts=incident.settings.max_attempts)
        if unload not in ("complete", "skipped"):
            _log_candidate_outcome(incident, gtid=incident.jammed_global_tray, verdict=f"unload_{unload}")
        match unload:
            case "complete" | "undecidable" | "skipped":
                # ``undecidable``: the unload WAS sent (invariant 8 holds) into a
                # mid-change AMS with nothing loaded — nothing physical could answer
                # it, so the load is the next measurement.
                pass
            case "held":
                # The AMS re-entered a change under the driver and acknowledged the
                # unload without moving: it runs at the next release. Nothing may be
                # sent behind it — the page names the held unload.
                await _give_up(incident, "swap_held_after_release", evidence=evidence)
                return
            case "acted" | "refused" | "no_movement":
                await _give_up(incident, "unload_failed", evidence=evidence)
                return
            case "drying":
                await _give_up(incident, "ams_drying", evidence=evidence)
                return
            case "session_changed":
                _note_answer_lost(incident, "unload")
                continue
            case "abort":
                await _abort(incident)
                return
            case "handover":
                _hand_over(incident)
                return
            case _:
                assert_never(unload)

        load = await _load_and_confirm(incident, target, evidence=evidence)
        if load != "complete":
            _log_candidate_outcome(incident, gtid=target, verdict=f"load_{load}")
        match load:
            case "complete":
                pass
            case "held":
                # Held behind a change the AMS re-entered: it runs at the next release,
                # refilling the extruder there — never resent, and no restore behind it.
                await _give_up(incident, "swap_held_after_release", evidence=evidence)
                return
            case "acted" | "no_movement":
                continue  # the load never completed — try the next candidate
            case "refused":
                # Every publish of this load was refused before it went out (no
                # session, or a runout hold latching loads): the next candidate's load
                # cannot go out either, and a load that never went out leaves no step
                # to exclude this tray by.
                await _give_up(incident, "candidate_loads_failed", evidence=evidence)
                return
            case "drying":
                await _give_up(incident, "ams_drying", evidence=evidence)
                return
            case "session_changed":
                # Nobody can tell whether this load ran: the tray is NOT tried
                # (:attr:`_RecoveryEvidence.tried`), so the fresh round selects it again.
                _note_answer_lost(incident, "load")
                continue
            case "abort":
                await _abort(incident)
                return
            case "handover":
                _hand_over(incident)
                return
            case _:
                assert_never(load)

        # (d) The resume — the SAME reader, reading the contract's own verb for
        #     ``swapped``. Not a release attempt, so the lever budget does not apply.
        first = await _read_after(incident, "resume", evidence=evidence, budgeted=False)
        match first.reading:
            case "swapped":
                await _succeed(incident, _swapped_tray(first))
                return
            case "self_healed" | "wedged" | "released":
                # One more resume (mirrors the live 16:21:24 → 16:22:57 recovery where
                # the first resume did not stick): the printer is back at PAUSE.
                pass
            case "ended":
                await _close_ended_by_driver(incident, evidence=evidence)
                return
            case "no_pause" | "not_sent":
                continue
            case "abort":
                # ``paused_elsewhere`` among the tokens: another actor's quiet PAUSE after
                # this resume ran — stand aside, no second resume, no park.
                await _abort(incident, token=first.takeover)
                return
            case "handover":
                _hand_over(incident)
                return
            case _:
                assert_never(first.reading)

        retry = await _read_after(incident, "resume", evidence=evidence, budgeted=False)
        match retry.reading:
            case "swapped":
                await _succeed(incident, _swapped_tray(retry))
                return
            case "self_healed" | "wedged" | "released":
                if not retry.moved:
                    # The print never ran, so nothing faulted and there is NOTHING to
                    # conclude about this spool. 002-H2S 2026-09-11: this branch used to
                    # be spelled the same as a re-jam and stamped the operator's healthy
                    # slot-1 roll out of rotation against a printer they had just stopped.
                    logger.info(
                        "spool_recovery: printer %s resume never took on tray %s — no fault evidence, "
                        "replacement kept in rotation; trying the next candidate",
                        pid,
                        target,
                    )
                    continue
                # The print RAN and stopped again — the ONE path on which a replacement
                # may be parked at all. Ask the WIRE which tray it blames (the same
                # evidence order the entry gate used; ``item=None`` because the dispatch
                # mapping now names the replacement itself, so it cannot corroborate
                # anything). The extruder-side rule lives in `_commit_out_of_rotation`.
                st, _waited = await _live_reading(incident)
                jammed_now, _verdict = _resolve_jammed_tray(
                    st, candidates=live_candidates(st), item=None, printer_id=pid
                )
                if jammed_now == target:
                    await _commit_out_of_rotation(incident, target, role="replacement")
                else:
                    logger.info(
                        "spool_recovery: printer %s re-PAUSEd with the fault attributed to tray %s, not the "
                        "replacement %s — kept in rotation",
                        pid,
                        jammed_now,
                        target,
                    )
            case "ended":
                await _close_ended_by_driver(incident, evidence=evidence)
                return
            case "no_pause" | "not_sent":
                continue
            case "abort":
                await _abort(incident, token=retry.takeover)
                return
            case "handover":
                _hand_over(incident)
                return
            case _:
                assert_never(retry.reading)

    # (4) Every candidate exhausted.
    _log_candidate_outcome(incident, gtid=None, verdict="candidates_exhausted")
    await _give_up(incident, evidence.exhaustion_reason(), evidence=evidence)


# --- the session gate: a gap in the printer's session is a WAIT, never an exit ---------
#
# Nothing in ``PrinterState`` is reset on a disconnect, and ``_on_connect`` re-broadcasts
# the PREVIOUS session's cached fields before the new session's first report lands
# (``PrinterState.report_epoch``). A driver that read those fields as the printer's answer
# held a dead session's cached RUNNING to a ``swapped`` success, and one that met no state
# at all aborted and barred the fault (the retired ``state_lost`` token). The architect
# review ruled the exit-and-re-enter alternative out: re-entering from a sweep races the
# entry and upgrade windows and misreads half-finished commands. So the gap is a wait of
# the driver itself, answered in ONE place for every loop and boundary it reads the wire
# at, and bounded by the farm's offline-stall window; the process dying stays
# ``rearm_incidents_on_startup``'s.


def _reads_live(st: PrinterState | None) -> bool:
    """Is ``st`` evidence about the printer RIGHT NOW? THE driver's one freshness question.

    ``print_reconcile.is_fresh`` — connected, and the current session's first report
    APPLIED — plus the rearm's own evidence rule (:func:`_hold_over`): a ``gcode_state``
    the printer has not positively reported (``""`` / ``UNKNOWN``) is not a reading either.
    No state at all (no client registered — a startup before the printer connects, a
    re-registration) is not fresh by the same test.
    """
    return print_reconcile.is_fresh(st) and _live_state(st) not in ("", "UNKNOWN")


class _PrinterOffline(Exception):
    """The printer did not come back to a live session within
    :attr:`RecoverySettings.offline_bound_s`. Raised by :func:`_live_reading` from whatever
    loop was waiting, and answered ONCE, in :func:`_run_recovery`: the ``printer_offline``
    give-up — paged, never a silent abort and never a bar on the fault."""

    def __init__(self, *, waited_s: float, bound_s: float) -> None:
        super().__init__(f"no live report within {bound_s:.0f}s (waited {waited_s:.0f}s)")
        self.waited_s = waited_s
        self.bound_s = bound_s


async def _live_reading(
    incident: RecoveryIncident, *, clock: Callable[[], float] | None = None
) -> tuple[PrinterState, float]:
    """THE driver's read of the wire: a LIVE state (:func:`_reads_live`), and how long the
    read waited for one — measured on ``clock``, the caller's own window clock (default
    :func:`_now`), so the caller moves its deadline by exactly that much and a session gap
    never burns a step's read window.

    A reading that is not live is "not evidence yet": the gate polls at
    :data:`_POLL_INTERVAL_S` until the printer reports on a live session again. The gap is
    measured from its first non-live reading; past ``offline_bound_s`` it raises
    :class:`_PrinterOffline`. One INFO line when a gap opens and one when it closes, so
    "why did recovery sit for 4 minutes" is answered in the log.
    """
    pid = incident.printer_id
    st = _get_state(pid)
    if st is not None and _reads_live(st):
        return st, 0.0
    window_clock = clock or _now
    window_started = window_clock()
    gap_started = _now()
    bound_s = incident.settings.offline_bound_s
    logger.info(
        "[spool_recovery] printer %s not reporting on a live session (connected=%s epoch=%s report_epoch=%s "
        "state=%s) — recovery waits for it (up to %.0fs)",
        pid,
        getattr(st, "connected", None),
        getattr(st, "connection_epoch", None),
        getattr(st, "report_epoch", None),
        getattr(st, "state", None),
        bound_s,
    )
    while True:
        await asyncio.sleep(_POLL_INTERVAL_S)
        st = _get_state(pid)
        if st is not None and _reads_live(st):
            waited = window_clock() - window_started
            logger.info(
                "[spool_recovery] printer %s reporting again after %.1fs (epoch=%s) — recovery continues",
                pid,
                _now() - gap_started,
                getattr(st, "connection_epoch", None),
            )
            return st, waited
        gap_s = _now() - gap_started
        if gap_s >= bound_s:
            logger.warning(
                "[spool_recovery] printer %s did not report on a live session within %.0fs "
                "(farm_offline_stall_minutes) — recovery gives up",
                pid,
                bound_s,
            )
            raise _PrinterOffline(waited_s=gap_s, bound_s=bound_s)


async def _await_live_state(incident: RecoveryIncident, states: frozenset[str], timeout_s: float) -> bool:
    """Wait, read through the session gate, for the printer to report one of ``states``
    within ``timeout_s`` of LIVE time. The driver's own wait for a PAUSE it caused or is
    owed — not ``printer_manager.await_state``, which reads whatever the cache holds and
    answers an absent state as False: here a gap is a wait that does not spend the
    timeout (:func:`_live_reading`), and a cached state from a dead session is no answer."""
    deadline = _now() + timeout_s
    while True:
        st, waited = await _live_reading(incident)
        deadline += waited
        if _live_state(st) in states:
            return True
        if _now() >= deadline:
            return False
        await asyncio.sleep(_POLL_INTERVAL_S)


# --- step helpers -----------------------------------------------------------

# The two live states the driver's PROCEDURE runs in. A lever moves PAUSE→RUNNING and
# the reader's pause moves RUNNING→PAUSE, so every intermediate reading of the machine's
# own verbs lands inside this pair, and everything outside it is a TERMINAL. Whose
# terminal depends on where it is read. In the steps' own loops it is somebody else's
# act (``_takeover``'s ``job_ended``). Inside a release verb's window
# (:func:`_read_after`) it is not: a verb with an unmeasured effect (``clean_print_error``,
# ``ams_control`` abort / reset / pause) can end the print, and the driver records that
# itself (``ended``) — unless the operator's Stop mark says the terminal was theirs.
_DRIVER_STATES: tuple[str, ...] = ("PAUSE", "RUNNING")


TakeoverToken = Literal[
    "reclassified", "job_changed", "job_ended", "hijacked_load", "operator_command", "resumed", "paused_elsewhere"
]


def _takeover(
    incident: RecoveryIncident,
    st: PrinterState,
    *,
    awaiting: str | None,
    target: int | None = None,
    after_contract_resume: bool = False,
) -> TakeoverToken | None:
    """Has this driver lost the job it was bound to? The token that says so, else None.

    ONE predicate for every poll loop in the machine (precedent :func:`_hold_over` —
    pure, DB-free, and taking the incident so the call site reads as a question about
    THIS recovery rather than about the printer in the abstract). Before it there were
    seven inline spellings of "somebody else is in control", and the set of things they
    noticed differed per step: one checked ``st is None``, another added RUNNING, a
    third a ``pending_tray_target`` hijack, and NONE of them asked whether the job was
    still there. 002-H2S 2026-09-11 is what that costs — the operator STOPPED the
    print, ``on_job_terminal`` closed the incident row, nothing told the driver, and it
    went on to load a slot, publish resume/pause/resume at a FAILED printer and stamp
    the operator's healthy spool out of rotation.

    ``st`` is a LIVE reading — the session gate's (:func:`_live_reading`), never a raw
    cache read: a disconnect or a new session's cached fields are not somebody else's act,
    they are "not evidence yet", and that is the gate's wait, not a token here (the retired
    ``state_lost`` token aborted on it and barred the fault).

    ``awaiting`` is the state THIS step is waiting for, so a loop can tell its own
    intermediate reading from someone else's action; ``target`` is the tray this step
    commanded, when it commanded one; ``after_contract_resume`` is the reader's PHASE,
    passed in so this stays a function of its arguments: True only inside the swap
    contract's own resume read (``_read_after(budgeted=False)``) once that resume LEFT
    PAUSE (the reader saw RUNNING).

    Tokens, FIRST match wins:

    ``reclassified``
        The durable row this context names is open with a DIFFERENT kind — the store
        upgraded it under us (a jam the taxonomy re-read as a physical fault). The one
        token that is NOT an abort; see :func:`_hand_over`.
    ``job_changed``
        The printer is echoing a different ``subtask_id``. Whatever this driver was
        recovering, it is not what is on the wire now.
    ``job_ended``
        The live state is neither PAUSE nor RUNNING — IDLE / FINISH / FAILED / PREPARE.
        Somebody else's act in every loop that asks this predicate; the one window where
        a terminal can be the driver's own doing — a release verb's read — tests it
        BEFORE asking (:func:`_read_after`, :data:`_DRIVER_STATES`).
    ``hijacked_load``
        The firmware is honouring a load for a tray that is not the one we commanded.
    ``operator_command``
        An operator's AMS load/unload went out on this printer since this driver began
        (``ams_command.operator_commanded_since`` against
        :attr:`RecoveryIncident.started_at`, session-epoch scoped). The operator owns the
        printer now: the Load / Unload buttons no longer refuse a mid-change AMS, so a
        click can land inside a confirm window, and a driver's verdict must never be read
        off an operator's move.
    ``resumed``
        A step waiting for PAUSE found the printer RUNNING: someone resumed the print
        while we were mid-procedure.
    ``paused_elsewhere``
        After the swap contract's resume ran (``after_contract_resume``), the printer is
        back at PAUSE on a QUIET path (``incident_resolution.path_quiet``: no actionable
        candidate on either wire lane, the AMS not mid-change). A re-jam PAUSEs with its
        code standing or inside a change, and still reads as one; a quiet PAUSE is another
        actor's — a touchscreen or UI Pause, the power-loss prompt, AI spaghetti detection
        (``0300_8003``, outside the AMS taxonomy). The driver stands aside: no resume over
        it, no replacement parked, and the row closes under its own source
        (:func:`_abort`). The baseline this restores: ``7fdfc0a8``'s contract resume read "a
        PAUSE with no recoverable code → abort". Never asked of a RELEASE verb's read
        (there a quiet PAUSE after RUNNING is ``released``), and never before the resume
        left PAUSE.
    """
    live = _live_state(st)
    kind = printer_incidents.cached_kind(incident.printer_id, incident.incident_id)
    if kind is not None and kind != incident.kind:
        return "reclassified"
    job = (getattr(st, "subtask_id", None) or "").strip()
    if incident.job_id and job and job != incident.job_id:
        return "job_changed"
    if live not in _DRIVER_STATES:
        return "job_ended"
    ptt = getattr(st, "pending_tray_target", None)
    if target is not None and ptt is not None and ptt != target:
        return "hijacked_load"
    if ams_command.operator_commanded_since(incident.printer_id, incident.started_at):
        return "operator_command"
    if awaiting == "PAUSE" and live == "RUNNING":
        return "resumed"
    if after_contract_resume and live == "PAUSE" and incident_resolution.path_quiet(st):
        return "paused_elsewhere"
    return None


def _note_takeover(incident: RecoveryIncident, token: TakeoverToken, step: str) -> StepTakeover:
    """Log a takeover ONCE, where it was noticed, and map it onto the step's verdict.

    Every step answers a takeover the same way — ``abort``, except the one token that
    means the row is still ours to leave alone. The line names the token and the step
    because "why did recovery stop" was previously answerable only by reading the code
    for which inline test that particular helper happened to carry.
    """
    logger.info("spool_recovery: printer %s recovery aborted (%s) during %s", incident.printer_id, token, step)
    return "handover" if token == "reclassified" else "abort"


async def _takeover_exit(incident: RecoveryIncident, *, awaiting: str | None, step: str) -> bool:
    """Ask :func:`_takeover` at a driver BOUNDARY and, when it answers, end the driver.

    True means the caller must ``return``: the takeover was logged, and the row was
    either aborted or handed over (``reclassified``). Read through the session gate, so
    a boundary met during a session gap waits for the printer instead of exiting. The
    step helpers
    ask inside their own poll loops; this is for the two boundaries where the driver
    is about to WRITE something about a spool or a unit — right after the PAUSE it
    waited for lands, and at the top of every round — because a takeover that lands
    BETWEEN two steps must not be answered by the next step's first write. 002-H2S
    2026-09-11: the operator's stop landed between a load and a resume and the
    machine stamped a healthy spool out of rotation; 003-H2S: an upgrade must beat
    the round's out-of-rotation stamp and first unload, or the swap machine acts on
    the milder reading for one more round.
    """
    st, _waited = await _live_reading(incident)
    token = _takeover(incident, st, awaiting=awaiting)
    if token is None:
        return False
    if _note_takeover(incident, token, step) == "handover":
        _hand_over(incident)
    else:
        await _abort(incident, token=token)
    return True


def _hand_over(incident: RecoveryIncident) -> None:
    """The one takeover that is NOT an abort: the store re-classified this row.

    No abort, no stamp, no close, no re-entry bar — the row stays OPEN under its NEW
    kind and that kind's owner produces the outcome. Aborting here would close a row
    that is no longer ours and bar re-entry for a fault nobody has handled (the
    ``_blocked`` ledger is keyed by the fault, not by who gave up on it).
    """
    logger.info(
        "spool_recovery: printer %s incident %s re-classified under the driver — handing over (no abort, no stamp)",
        incident.printer_id,
        incident.incident_id,
    )


def _feed_fault_live(state) -> bool:
    """True while a FEED fault (:data:`_FEED_FAULT_CLASSES`, either wire lane) stands on
    the live printer state."""
    return any(c.fault_class in _FEED_FAULT_CLASSES for c in live_candidates(state))


async def _close_self_healed(incident: RecoveryIncident) -> None:
    """Same-feeder self-heal: a release verb cleared the jam with no swap.

    The ladder reads it at the top of a round. In the first round the swap-commit
    boundary was never reached, so THIS incident stamped nothing and the clear below is
    a safety net for a flag a PREVIOUS incident on this feeder may have left; in a later
    round the boundary WAS crossed and the clear is what returns the jammed spool the
    print is running on. Counted toward the per-job flap cap and closed as a no-swap
    success (the truthful self-heal notification) — never a resume or a swap on top of
    a running print.
    """
    from backend.app.core.database import async_session

    async with async_session() as db:
        await _clear_oor_if_resumed_on_jammed_feeder(db, incident)
    await _succeed(incident, incident.jammed_global_tray, swapped=False)


def _note_answer_lost(incident: RecoveryIncident, step: str) -> None:
    """Log, where it was noticed, a command whose answer was LOST with its MQTT session.

    ``ams_command.classify`` has already read the new session's first fresh report and
    found no motion that answers the command (``session_changed``): nobody can tell
    whether it ran, so nothing is inferred — not a success, not a failure, and not a
    takeover. The step stays on the log answered ``session_changed`` and the driver starts
    the next round from the live wire, the way a fresh round would."""
    logger.info(
        "spool_recovery: printer %s %s answer lost with the MQTT session — re-reading the AMS for a fresh round",
        incident.printer_id,
        step,
    )


def _live_state(st) -> str:
    """The printer's reported ``gcode_state``, upper-cased; ``""`` when there is none."""
    return (getattr(st, "state", None) or "").upper() if st is not None else ""


def _pause_reading(st) -> Literal["wedged", "released"]:
    """A printer at PAUSE, read by the AMS's own state word — ``bambu_mqtt.
    ams_mid_filament_change``, THE predicate; the driver never re-derives it from a
    posture (002-H2S 2026-09-11: "the state machine moved" was read as "the AMS left the
    change", and ``ams_status_main`` still read 1)."""
    return "wedged" if ams_mid_filament_change(st) else "released"


def _success_reading(
    position: FeederPosition, *, quiet: bool, budgeted: bool
) -> Literal["self_healed", "swapped"] | None:
    """The success a QUIET path on this feeder reads as, or None.

    ``empty`` / ``unknown`` / ``external`` are NEVER a success: a print RUNNING with
    nothing fed prints air (shape 39 — an operator resume printed 4 h of it). The jammed
    tray is a self-heal only for a RELEASE verb (``budgeted``): after the contract's own
    load of another tray, the jammed tray back at the feeder means the load did not
    stick, which is not the contract's success — that read falls to the hung arm and its
    PAUSE is retried like any other.
    """
    if not quiet:
        return None
    match position.kind:
        case "other":
            return "swapped"
        case "jammed":
            return "self_healed" if budgeted else None
        case "empty" | "unknown" | "external":
            return None
        case _:
            assert_never(position.kind)


async def _operator_stopped(incident: RecoveryIncident, st, *, published_at: float) -> bool:
    """Did the OPERATOR stop this print? Read at the terminal inside a verb's window. Two
    witnesses, either one sufficient:

    * the touchscreen's Stop — the firmware's cancel echo, which the client stamps as
      ``PrinterState.user_cancel_seen_at`` (wall clock, ``time.time()``; cleared at the next
      print start). Only a stamp NEWER than the verb's publish counts: an older one is an
      echo this verb's window did not produce. The H2C emits no cancel echo (a screen
      stop there classifies as a genuine failure), so this witness is H2S-family only;
    * the Bambuddy UI's Stop — the DURABLE stop request on the job's unit
      (``farm_correlation.operator_stop_requested``, the terminal classifier's own reader),
      which ``print_control.stop_as_operator`` commits BEFORE the stop is sent. It is a fact
      about THIS job, not an instant: a Stop pressed before the verb went out ended the same
      print, and the request is never dropped by the terminal's own callback, so no poll can
      miss it. A FOREIGN print has no unit and so no request — its UI stop is read from the
      echo alone.
    """
    cancel_at = getattr(st, "user_cancel_seen_at", None)
    if cancel_at is not None and cancel_at > published_at:
        return True
    from backend.app.core.database import async_session

    async with async_session() as db:
        return await farm_correlation.operator_stop_requested(db, incident.printer_id, incident.job_id)


# The reader's two arms that publish ``print.pause`` (:func:`_read_after`):
# ``resume_then_pause``'s first RUNNING sample with nothing at the feeder (the firmware's
# retract on a loaded stall, the first sample on an empty path), and a change hung
# RUNNING at the deadline — the fallback when no retract is ever seen.
PauseArm = Literal["path_empty", "hung"]


def _terminal_read(
    incident: RecoveryIncident,
    lever: Lever,
    live: str,
    *,
    stopped: bool,
    quiet: bool,
    position: FeederPosition,
    last_fed: FeederPosition | None,
    budgeted: bool,
) -> LeverRead:
    """A terminal inside a verb's window.

    FINISH is the print COMPLETING right after the verb: read like RUNNING, with the
    feeder the window last SAW fed standing in for a live one the end-of-print unload
    already emptied. Any other terminal is the operator's Stop when a Stop witness says so
    (:func:`_operator_stopped` — today's ``job_ended`` abort), and otherwise the driver's own
    verb ended the print — ``ended``, which the driver records itself.
    """
    if live == "FINISH":
        fed = position if position.kind in ("jammed", "other") else last_fed
        success = _success_reading(fed, quiet=quiet, budgeted=budgeted) if fed is not None else None
        if success is not None and fed is not None:
            return LeverRead(success, moved=True, position=fed)
    if stopped:
        return LeverRead(
            _note_takeover(incident, "job_ended", f"lever={lever}"), moved=True, position=position, takeover="job_ended"
        )
    logger.warning(
        "spool_recovery: printer %s reached %s inside the %s window with no operator Stop — the release verb "
        "ended the print",
        incident.printer_id,
        live,
        lever,
    )
    return LeverRead("ended", moved=True, position=position)


async def _watch_lever(
    incident: RecoveryIncident,
    spec: LeverSpec,
    lever: Lever,
    *,
    entry_ams: int | None,
    published_at: float,
    budgeted: bool,
) -> LeverRead | PauseArm:
    """Poll the wire after a verb went out, bounded by ``step_timeout_s``, until a
    DECISIVE reading — or one of the two pause arms, which :func:`_read_after` publishes.

    Every poll, in order: a terminal first (``ended`` / the operator's ``abort`` / a
    FINISH success — :func:`_terminal_read`), then :func:`_takeover` (state lost,
    re-classified, another job, an operator's AMS command), then the reading:

    * RUNNING — a quiet path (``incident_resolution.path_quiet``) on a real feeder
      (:func:`_success_reading`) held for :data:`_POST_RESUME_STABLE_S` is the success;
      at the deadline the success reads at once (the stable hold cannot outlast the
      window). For a lever with :attr:`LeverSpec.pause_on_empty_path`, a RUNNING sample
      whose feeder reads ``empty`` (:func:`_feeder_position`) → ``path_empty``, quiet or
      not: on a loaded stall that is the firmware's own retract of the jammed filament
      (012-H2S 2026-09-25, ``tray_now`` 2 → 255 every ~12 s after a resume), on an empty
      path it is the first sample, and a quiet RUNNING with nothing fed prints air
      (shape 39), so pausing it at once is the safe direction. Still no success at the
      deadline → ``hung`` (the live 009 case, ~2.5 min RUNNING in an incomplete change;
      and the fallback when no retract is ever seen — a dual-nozzle per-extruder map may
      never read empty).
    * PAUSE after the printer LEFT it, or with the AMS's state word changed →
      ``wedged`` / ``released`` at once (:func:`_pause_reading`); PAUSE never left →
      the same reading at the deadline, with ``moved`` False. In the swap contract's own
      resume read (not ``budgeted``), a PAUSE after the resume ran on a QUIET path is
      another actor's (:func:`_takeover`'s ``paused_elsewhere``) — the reader's phase
      ``saw_running`` is what it is handed, because ``left`` also counts an AMS state-word
      change the print never left PAUSE for.

    Every reading is LIVE (:func:`_live_reading`). A session gap moves the deadline by
    the time it took and drops a stable hold in progress: RUNNING seen before the gap is
    not RUNNING held through it, so a dead session's cached RUNNING is never held to a
    success.
    """
    pid = incident.printer_id
    jammed = incident.jammed_global_tray
    deadline = _now() + incident.settings.step_timeout_s
    left = False
    saw_running = False
    last_fed: FeederPosition | None = None
    holding: tuple[Literal["self_healed", "swapped"], float] | None = None
    while True:
        st, waited = await _live_reading(incident)
        if waited:
            deadline += waited
            holding = None
        at_deadline = _now() >= deadline
        live = _live_state(st)
        position = _feeder_position(st, jammed, pid)
        if position.kind in ("jammed", "other"):
            last_fed = position
        if live not in _DRIVER_STATES:
            # Asked AT the terminal, once: both witnesses are durable for the job's life (the
            # request on the unit row, the echo stamp until the next print starts), so nothing
            # a poll could miss has to be latched between polls.
            return _terminal_read(
                incident,
                lever,
                live,
                stopped=await _operator_stopped(incident, st, published_at=published_at),
                quiet=incident_resolution.path_quiet(st),
                position=position,
                last_fed=last_fed,
                budgeted=budgeted,
            )
        # RUNNING is expected here; a quiet PAUSE after the CONTRACT's resume ran is not.
        token = _takeover(incident, st, awaiting=None, after_contract_resume=not budgeted and saw_running)
        if token is not None:
            return LeverRead(
                _note_takeover(incident, token, f"lever={lever}"), moved=left, position=position, takeover=token
            )
        if live == "RUNNING":
            left = True
            saw_running = True
            quiet = incident_resolution.path_quiet(st)
            success = _success_reading(position, quiet=quiet, budgeted=budgeted)
            if success is None:
                holding = None
                if spec.pause_on_empty_path and position.kind == "empty":
                    logger.info(
                        "spool_recovery: printer %s RUNNING with nothing at the feeder after lever %s (tray_now=%s "
                        "ams_status_main=%s quiet=%s) — publishing pause (the workable PAUSE, timed to the retract)",
                        pid,
                        lever,
                        getattr(st, "tray_now", None),
                        getattr(st, "ams_status_main", None),
                        quiet,
                    )
                    return "path_empty"
                if at_deadline:
                    logger.info(
                        "spool_recovery: printer %s hung RUNNING after lever %s (tray_now=%s ams_status_main=%s "
                        "quiet=%s) — pausing it to read the AMS",
                        pid,
                        lever,
                        getattr(st, "tray_now", None),
                        getattr(st, "ams_status_main", None),
                        quiet,
                    )
                    return "hung"
            else:
                if holding is None or holding[0] != success:
                    holding = (success, _now())
                if at_deadline or _now() - holding[1] >= _POST_RESUME_STABLE_S:
                    return LeverRead(success, moved=True, position=position)
        else:
            holding = None
            if getattr(st, "ams_status_main", None) != entry_ams:
                left = True
            if left or at_deadline:
                return LeverRead(_pause_reading(st), moved=left, position=position)
        await asyncio.sleep(_POLL_INTERVAL_S)


async def _paused_read(
    incident: RecoveryIncident, lever: Lever, *, landed: bool, published_at: float, budgeted: bool
) -> LeverRead:
    """What the reader's own pause brought back. Landed → the PAUSE reading; otherwise a
    terminal, a takeover, or ``no_pause``. A LIVE reading (:func:`_live_reading`). The
    PAUSE here is the reader's OWN, so :func:`_takeover` is never handed the contract
    phase: its quiet PAUSE is not another actor's."""
    pid = incident.printer_id
    st, _waited = await _live_reading(incident)
    position = _feeder_position(st, incident.jammed_global_tray, pid)
    if landed:
        return LeverRead(_pause_reading(st), moved=True, position=position)
    live = _live_state(st)
    if live not in _DRIVER_STATES:
        return _terminal_read(
            incident,
            lever,
            live,
            stopped=await _operator_stopped(incident, st, published_at=published_at),
            quiet=incident_resolution.path_quiet(st),
            position=position,
            last_fed=None,
            budgeted=budgeted,
        )
    token = _takeover(incident, st, awaiting=None)
    if token is not None:
        return LeverRead(
            _note_takeover(incident, token, f"lever={lever}"), moved=True, position=position, takeover=token
        )
    logger.warning("spool_recovery: printer %s could not be brought back to PAUSE after lever %s", pid, lever)
    return LeverRead("no_pause", moved=True, position=position)


async def _read_after(
    incident: RecoveryIncident,
    lever: Lever,
    *,
    evidence: _RecoveryEvidence,
    budgeted: bool,
) -> LeverRead:
    """Publish ONE verb of the lever table and read what the wire answered — THE reader,
    for every release verb AND for the swap contract's own resume, so no verb can grow a
    second, subtly different reading of the wire.

    ONE window for every verb, ``step_timeout_s``: the witness CONTINUEs re-PAUSEd at
    47–87 s (012-H2S 2026-09-23), a release moves the AMS's state word within the push
    cadence, and :func:`_watch_lever` exits early on any decisive transition. The reading
    table (:data:`LeverReading`) is :func:`_watch_lever`'s; the two pause arms publish
    ``print.pause`` HERE and read the PAUSE it brings back (:func:`_paused_read`).

    ``budgeted`` — a RELEASE attempt: the step is noted on the evidence log AT THE SEND
    (so a crash before the read still spends the lever) and answered with the verdict
    and ``moved``. The contract's resume after a load is not one — it is not noted, and
    its jammed-feeder reading is not a success (:func:`_success_reading`).

    A terminal inside the window is the OPERATOR's when a Stop witness says so
    (:func:`_operator_stopped`: the durable UI stop request on the job's unit, or the
    firmware's cancel echo stamped after ``published_at``, the wall-clock instant recorded
    immediately before the publish) — the ``job_ended`` abort, never ``ended``. KNOWN GAP: the H2C emits NO
    cancel echo on a screen stop (a screen stop there classifies as a genuine failure), so
    on that model a touchscreen Stop inside a lever window still reads ``ended``, and the
    driver closes the row as its own and pages ``wedge_ended_print``.

    Logs ONE ``[spool_recovery] lever=<lever> outcome=<reading>`` line per read — the
    measurement the next wedge is triaged by, greppable per verb.

    The client is the manager's CURRENT one, fetched at each publish: a session gap can
    end with a re-registered client, and a handle held across it would publish into the
    one that is gone.
    """
    spec = _lever(lever)
    pid = incident.printer_id
    seq = await evidence.note(LeverStep.draft(lever)) if budgeted else None
    st, _waited = await _live_reading(incident)
    entry_ams = getattr(st, "ams_status_main", None)
    entry_sub = getattr(st, "ams_status_sub", None)
    entry_tray = getattr(st, "tray_now", None)
    scope = "" if budgeted else " (the swap round's resume, not a release attempt)"
    started = _now()
    logger.info(
        "[spool_recovery] lever=%s publishing printer=%s (state=%s ams_status=%s/%s tray_now=%s codes=%s)%s",
        lever,
        pid,
        getattr(st, "state", None),
        entry_ams,
        entry_sub,
        entry_tray,
        sorted(_active_recoverable_codes(st)),
        scope,
    )
    read: LeverRead
    # Wall clock, the cancel-echo stamp's own clock. Recorded BEFORE the publish, so an echo
    # that lands between the two counts as the operator's — the safe direction.
    published_at = time.time()
    client = printer_manager.get_client(pid)
    if client is None or not spec.publish(client):
        logger.warning("spool_recovery: printer %s lever %s send returned False (offline?)", pid, lever)
        read = LeverRead("not_sent", moved=False, position=_feeder_position(st, incident.jammed_global_tray, pid))
    else:
        watched = await _watch_lever(
            incident, spec, lever, entry_ams=entry_ams, published_at=published_at, budgeted=budgeted
        )
        match watched:
            case LeverRead():
                read = watched
            case "path_empty" | "hung":
                client = printer_manager.get_client(pid)
                landed = (
                    client is not None
                    and client.pause_print()
                    and await _await_live_state(incident, frozenset({"PAUSE"}), incident.settings.step_timeout_s)
                )
                read = await _paused_read(incident, lever, landed=landed, published_at=published_at, budgeted=budgeted)
            case _:
                assert_never(watched)
    if seq is not None:
        await evidence.answer(seq, read.reading, moved=read.moved)
    st = _get_state(pid)
    logger.info(
        "[spool_recovery] lever=%s outcome=%s printer=%s after %.1fs (state=%s ams_status %s/%s→%s/%s "
        "tray_now %s→%s codes=%s)%s",
        lever,
        read.reading,
        pid,
        _now() - started,
        getattr(st, "state", None),
        entry_ams,
        entry_sub,
        getattr(st, "ams_status_main", None),
        getattr(st, "ams_status_sub", None),
        entry_tray,
        getattr(st, "tray_now", None),
        sorted(_active_recoverable_codes(st)),
        scope,
    )
    return read


# How the release ladder ended (:func:`_release_ladder`). Closed.
LadderExit = Literal["self_healed", "swapped", "released", "unreleased", "withheld", "ended", "abort", "handover"]


async def _release_ladder(incident: RecoveryIncident, *, evidence: _RecoveryEvidence) -> LadderExit:
    """Pull every UNSPENT lever of :data:`_LEVERS`, in table order, until one reads a
    self-heal, a release or a terminal. Called only while the AMS holds a filament change.

    ``wedged`` / ``no_pause`` / ``not_sent`` → the next lever (each is recorded on the
    log, so each is spent). Every lever spent — this driver's or a previous one's on the
    same incident — with the change still held is ``unreleased``: the caller gives up
    ``wedge_unreleased`` with NOTHING sent into the change.

    ``withheld``: no lever is pulled while the incident's last COMPLETED motion is an
    unload (:attr:`_RecoveryEvidence.extruder_emptied_by_farm`) — a resume over an
    extruder the farm emptied prints air (2026-09-17 ruling), prevented here, never
    detected after.

    The resumes this publishes are the driver's OWN, and the printer answers them on the
    wire like any other: the per-push sampler sees PAUSE→RUNNING and hands it to
    :func:`on_observed_running`, which stands aside while this driver is live — RUNNING
    here is an intermediate reading of the procedure (006-H2S 2026-09-04: the row closed
    at the resume, and a second driver ran the swap round on the same AMS).
    """
    pid = incident.printer_id
    if evidence.extruder_emptied_by_farm:
        st = _get_state(pid)
        logger.info(
            "[spool_recovery] ladder withheld printer=%s — the farm emptied this extruder (last completed motion: "
            "unload); no release verb over it (ams_status=%s/%s tray_now=%s)",
            pid,
            getattr(st, "ams_status_main", None),
            getattr(st, "ams_status_sub", None),
            getattr(st, "tray_now", None),
        )
        return "withheld"
    for lever in _LEVERS:
        if evidence.lever_spent(lever):
            continue
        read = await _read_after(incident, lever, evidence=evidence, budgeted=True)
        reading = read.reading
        match reading:
            case "self_healed" | "swapped" | "released" | "ended" | "abort" | "handover":
                return reading
            case "wedged" | "no_pause" | "not_sent":
                continue
            case _:
                assert_never(reading)
    st, _waited = await _live_reading(incident)
    token = _takeover(incident, st, awaiting="PAUSE")
    if token is not None:
        return _note_takeover(incident, token, "release_ladder")
    if not ams_mid_filament_change(st):
        return "released"
    logger.warning(
        "[spool_recovery] ladder exhausted printer=%s levers=%s — every release verb pulled and the AMS still "
        "holds the print's filament change (ams_status=%s/%s tray_now=%s); nothing sent into it",
        pid,
        ",".join(f"{s.lever}:{s.outcome}" for s in evidence.lever_steps) or "-",
        getattr(st, "ams_status_main", None),
        getattr(st, "ams_status_sub", None),
        getattr(st, "tray_now", None),
    )
    return "unreleased"


def _swapped_tray(read: LeverRead) -> int:
    """The tray a ``swapped`` reading SAW feeding — the one :func:`_succeed` writes into
    the mapping. A reading that names none is a reader that changed shape: it raises."""
    tray = read.position.global_tray
    if read.reading != "swapped" or tray is None:
        raise LookupError(f"spool_recovery: a {read.reading!r} reading names no swapped tray ({read.position})")
    return tray


async def _close_ended_by_driver(incident: RecoveryIncident, *, evidence: _RecoveryEvidence) -> None:
    """A release verb's window read a terminal the operator's Stop did not produce: the
    driver's own verb ENDED the print, and the driver records it itself.

    The job-terminal closer stands aside while a driver is live (review F2 — a terminal
    the driver's own verb produced is a reading of that procedure), so the row is this
    driver's to close: RESOLVED with ``printer_incidents.RESOLVE_DRIVER_ENDED``, then ONE
    page (``wedge_ended_print``) carrying the evidence log, and one forensic ledger row
    that never counts toward the quarantine. Nothing is requeued here: the terminal's own
    disposition handles the unit, as for any farm terminal. The close is the ownership
    test — a row somebody else already closed stands the page down, as in ``_escalate``.
    Best-effort end to end.
    """
    from backend.app.core.database import async_session
    from backend.app.models.printer import Printer
    from backend.app.services.notification_service import notification_service

    await _log_tray_snapshot(incident)
    detail = _compose_detail(incident, "wedge_ended_print", restore=None, evidence=evidence)
    try:
        async with async_session() as db:
            row = await printer_incidents.close(
                db, incident.incident_id, status=STATUS_RESOLVED, source=printer_incidents.RESOLVE_DRIVER_ENDED
            )
            if row is None:
                logger.warning(
                    "spool_recovery: printer %s incident %s closed under the driver — the ended-print page stands down",
                    incident.printer_id,
                    incident.incident_id,
                )
                return
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
                    runout_slot=runout_slot_desc(incident.jammed_global_tray),
                    foreign=incident.item_id is None,
                )
            except Exception:  # noqa: BLE001 — notification failure is non-fatal
                logger.exception("spool_recovery: ended-print notification failed for printer %s", incident.printer_id)
            await _record_escalation_and_maybe_quarantine(db, incident, "wedge_ended_print", opened_at=row.created_at)
        logger.warning(
            "spool_recovery: printer %s incident %s closed by the driver (%s) — a release verb ended the print",
            incident.printer_id,
            incident.incident_id,
            printer_incidents.RESOLVE_DRIVER_ENDED,
        )
    except Exception:  # noqa: BLE001 — never crash the driver
        logger.exception("spool_recovery: ended-print close failed for printer %s", incident.printer_id)


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


async def _wait_ams_write_window(printer_id: int, ams_id: int) -> str | None:
    """Pre-flight the AMS wire for a recovery load/unload on unit ``ams_id``.

    Returns the refusal reason that STILL stands after giving the wire a chance to
    settle, or None when it is clear. Drying is a doomed lane — it is reported
    immediately so the caller can escalate instead of burning attempts on writes the
    client will refuse for the whole cycle. An identify-contention refusal is
    transient by construction, so the client's own settle wait absorbs it (the same
    idiom the terminal sweep uses) rather than ending the recovery.

    This is advisory only: the client re-evaluates at publish time, which is the
    check that actually closes the race — and with no client there is nothing to
    pre-flight (the ``ams_command`` verb refuses ``not_connected`` itself).
    """
    client = printer_manager.get_client(printer_id)
    if client is None:
        return None
    refusal = client.ams_write_refusal(ams_id)
    if refusal is None or refusal == _REFUSAL_DRYING:
        return refusal
    await client.wait_ams_settle()
    return client.ams_write_refusal(ams_id)


def _feeder_kind(incident: RecoveryIncident) -> str:
    """The feeder position AT a send (:func:`_feeder_position`), for the command's step."""
    return _feeder_position(_get_state(incident.printer_id), incident.jammed_global_tray, incident.printer_id).kind


async def _unload_and_confirm(incident: RecoveryIncident, *, evidence: _RecoveryEvidence, attempts: int) -> UnloadStep:
    """Unload the feeder through ``ams_command.unload`` and read the wire's answer.

    ``skipped`` (nothing to unload — :func:`_unload_skippable`) / ``drying`` (the AMS is
    drying: a doomed lane) / an ``ams_command.Answer`` for the last publish that went out
    / ``refused`` (every publish this call made was an ``ams_command.Refusal`` — each one
    consumes an attempt at once, no confirm wait) / ``abort`` / ``handover``.

    Up to ``attempts`` publishes: an unload the AMS moved and never completed
    (``acted``), one it neither moved on nor acknowledged (``no_movement``) and a refusal
    are resent. ``held`` is NEVER resent: the firmware acknowledged it and holds it behind
    the print's change, where it runs at the next release — so a second command behind
    it would run too (012-H2S 2026-09-23). ``session_changed`` is not resent HERE either:
    its answer was lost with its session, and the driver's next round re-reads the AMS
    before anything else goes out. Every other answer returns at once. Every publish that
    went out is a :class:`CommandStep` on the evidence log, noted at the send.

    The unload is sent even when ``tray_now`` already reads 255 — the client encodes
    that as ``ams_id/slot_id/target = 255``, byte-for-byte the operator's proven manual
    recovery command (invariant 8: unconditional before a load).
    """
    st, _waited = await _live_reading(incident)
    if _unload_skippable(st):
        return "skipped"

    unit = _ams_unit_for_tray(getattr(st, "tray_now", None))
    refusal = await _wait_ams_write_window(incident.printer_id, unit)
    if refusal == _REFUSAL_DRYING:
        return "drying"

    verdict: UnloadStep = "refused"
    for _ in range(max(1, attempts)):
        sent = ams_command.unload(incident.printer_id, actor="driver")
        if isinstance(sent, ams_command.Refusal):
            logger.warning(
                "spool_recovery: printer %s unload not sent (%s) — attempt consumed",
                incident.printer_id,
                sent.reason,
            )
            verdict = "refused"
            continue
        seq = await evidence.note(CommandStep.draft("unload", None, _feeder_kind(incident)))
        answer = await _confirm_unloaded(incident, sent)
        match answer:
            case "acted" | "no_movement":
                await evidence.answer(seq, answer)
                verdict = answer
            case "complete" | "held" | "undecidable" | "session_changed":
                await evidence.answer(seq, answer)
                return answer
            case "abort" | "handover":
                return answer
            case _:
                assert_never(answer)
    return verdict


async def _observe_command(
    incident: RecoveryIncident,
    command: ams_command.Command,
    target: int | None,
    sent: ams_command.Sent,
    *,
    step: str,
) -> ams_command.Answer | StepTakeover:
    """THE driver's confirm loop for one published AMS motion command.

    Asks :func:`_takeover` on EVERY poll, and judges the wire ONLY through
    ``ams_command.classify`` — one ``Observation`` per publish, with the firmware's ACK
    for THIS send (``ams_command.ack_of``) folded into it on every poll BEFORE the
    snapshot is read (invariant 14; the order ``ams_command.observe`` keeps, so an ACK is
    never paired with a snapshot older than itself). The fold is what lets the classifier
    answer ``held``. ``elapsed_s`` is measured from the entry snapshot (the send) on
    ``ams_command``'s own clock, the step timeout is the deadline, and the classifier
    always answers at the deadline, so the loop needs no clock of its own. One
    ``answer=`` line per command (``ams_command.log_answer``, the one format of it).

    Every poll is LIVE (:func:`_live_reading`), and the time a session gap took is kept
    OUT of ``elapsed_s`` — measured on the snapshots' own clock (``ams_command.clock``) —
    so a gap never burns the command's window. A reconnect is answered by the classifier
    itself, on the new session's first fresh report (``ams_command._across_sessions``).
    """
    observation = ams_command.Observation()
    entry = sent.entry
    deadline_s = incident.settings.step_timeout_s
    blocked_s = 0.0
    while True:
        st, waited = await _live_reading(incident, clock=ams_command.clock)
        blocked_s += waited
        token = _takeover(incident, st, awaiting="PAUSE", target=target)
        if token is not None:
            return _note_takeover(incident, token, step)
        observation.fold_ack(ams_command.ack_of(incident.printer_id, sent))
        now = ams_command.snapshot(st)
        elapsed_s = now.taken_at - entry.taken_at - blocked_s
        answer = ams_command.classify(
            command, target, entry, now, observation=observation, elapsed_s=elapsed_s, deadline_s=deadline_s
        )
        if answer is not None:
            ams_command.log_answer(
                actor="driver",
                printer_id=incident.printer_id,
                command=command,
                target=target,
                entry=entry,
                now=now,
                answer=answer,
                elapsed_s=elapsed_s,
            )
            return answer
        await asyncio.sleep(_POLL_INTERVAL_S)


async def _confirm_unloaded(incident: RecoveryIncident, sent: ams_command.Sent) -> ams_command.Answer | StepTakeover:
    """Read what the wire answered the unload ``sent``.

    ``tray_now == 255`` alone is never completion — after a feed fault it already reads
    255 before the unload (009-H2S 2026-07-20). Completion, and every other answer, is
    ``ams_command.classify``'s per-posture row: outside a change, idle + nothing fed
    after an observed cycle or held for ``ams_command.UNLOAD_GRACE_S``; mid-change with a
    feeder loaded, the feeder leaving for 255 and holding — or ``held`` when the firmware
    acknowledged it and nothing moved; mid-change with nothing loaded, ``undecidable``
    (nothing physical can answer)."""
    return await _observe_command(incident, "unload", None, sent, step="confirm_unloaded")


async def _select_replacement(incident: RecoveryIncident, tried: set[int]) -> tuple[int | None, bool]:
    """Pick the next eligible loaded tray for the jammed filament, reusing the
    dispatcher's own selection functions. Returns ``(global_tray_id | None,
    only_low)`` — ``only_low`` True when the only match was withheld by the
    minimum-start floor, which applies only inside the protected layers
    (:func:`_match_candidates`). External / jammed / already-tried trays are
    excluded; out-of-rotation / spent / archived exclusion is inside the matcher.

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
    status, _waited = await _live_reading(incident)
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
        # The token is the sweep's own reason for stopping (already logged); the
        # driver re-derives the takeover from LIVE state at its own check below, so
        # nothing here acts on a reading this old.
        status2, _token = await _await_bare_tray_configured(incident, forced_slots)
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

    # The layer rule is a floor PARAMETER, not new floor logic. Inside the protected
    # layers the ordinary minimum-start floor holds (doctrine rule 4: the first layers
    # are a print START, and a low roll there stays a backup donor, never the feeder).
    # At or after them the floor is OFF (``min_start_g=0``, the matcher's own "no floor"):
    # the gram ledger never vetoes a present, not-spent spare. The ledger is the
    # untrustworthy half here (rule 8) — 004-H2S 2026-09-17 refused spool 684 at 1013 g
    # used of a 1000 g label and it then printed ~23 h to a real runout; 011-H2S refused
    # rows that ran 16 h and 3 days more. Exhaustion is ``spent_at``, and spent,
    # out-of-rotation and archived rolls stay hard-excluded inside the matcher whatever
    # the floor; tray presence (``_present`` above) guards the load itself — and is the
    # only guard in Spoolman mode, which carries none of those flags.
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

    Returns ``(live state, takeover token)``: the state on success, else ``(None,
    token)`` when :func:`_takeover` ended the wait and ``(None, None)`` on a plain
    timeout. The caller's own path is unchanged — a ``None`` state means "no candidate
    from this sweep" either way — but the TOKEN is what names WHY the sweep stopped,
    which is otherwise lost in a helper that answers with an absence. Every poll is LIVE
    (:func:`_live_reading`), and a session gap moves the deadline by its length."""
    deadline = _now() + incident.settings.step_timeout_s
    while True:
        st, waited = await _live_reading(incident)
        deadline += waited
        token = _takeover(incident, st, awaiting="PAUSE")
        if token is not None:
            _note_takeover(incident, token, "await_bare_tray_configured")
            return None, token
        if _any_slot_configured(st, forced_slots):
            return st, None
        if _now() >= deadline:
            return None, None
        await asyncio.sleep(_POLL_INTERVAL_S)


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


async def _load_and_confirm(incident: RecoveryIncident, target: int, *, evidence: _RecoveryEvidence) -> LoadStep:
    """Load ``target`` through ``ams_command.load`` and read the wire's answer.

    ``drying`` (a drying target unit is a doomed lane — reported so the caller escalates
    instead of burning attempts) / an ``ams_command.Answer`` for the last publish that
    went out / ``refused`` (every publish was an ``ams_command.Refusal`` — each consumes
    an attempt at once, no confirm wait) / ``abort`` / ``handover``.

    Up to ``max_attempts`` publishes (the live incident needed two sends before the load
    took): ``acted``, ``no_movement`` and a refusal are resent; ``held`` is NEVER resent
    (the firmware holds it behind the print's change and runs it at the next release).
    ``ams_command.load`` owns the pre-send ``note_commanded_load`` mark and the entry
    snapshot; a ``pending_tray_target`` that turns into something other than ``target``
    is :func:`_takeover`'s ``hijacked_load``. Every publish that went out is a
    :class:`CommandStep` on the evidence log, noted at the send. ``session_changed``
    returns at once, never resent here: the driver's next round re-reads the AMS first.
    """
    refusal = await _wait_ams_write_window(incident.printer_id, _ams_unit_for_tray(target))
    if refusal == _REFUSAL_DRYING:
        return "drying"

    verdict: LoadStep = "refused"
    for _ in range(max(1, incident.settings.max_attempts)):
        sent = ams_command.load(incident.printer_id, target, actor="driver")
        if isinstance(sent, ams_command.Refusal):
            logger.warning(
                "spool_recovery: printer %s load of tray %s not sent (%s) — attempt consumed",
                incident.printer_id,
                target,
                sent.reason,
            )
            verdict = "refused"
            continue
        seq = await evidence.note(CommandStep.draft("load", target, _feeder_kind(incident)))
        answer = await _confirm_loaded(incident, target, sent)
        match answer:
            case "acted" | "no_movement":
                await evidence.answer(seq, answer)
                verdict = answer
            case "complete" | "held" | "session_changed":
                await evidence.answer(seq, answer)
                return answer
            case "abort" | "handover":
                return answer
            case "undecidable":
                # ``ams_command``'s table gives no load row this answer; a classifier
                # that changed shape must fail loudly, never be read as a verdict.
                raise LookupError("ams_command answered a load 'undecidable' — only an unload into an empty change can")
            case _:
                assert_never(answer)
    return verdict


async def _confirm_loaded(
    incident: RecoveryIncident, target: int, sent: ams_command.Sent
) -> ams_command.Answer | StepTakeover:
    """Read what the wire answered the load of ``target`` ``sent``: ``complete`` on
    ``tray_now == target`` in every posture (``ams_command``'s load row), ``held`` when a
    mid-change AMS acknowledged it and nothing moved, ``acted`` / ``no_movement`` at the
    step timeout."""
    return await _observe_command(incident, "load", target, sent, step="confirm_loaded")


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


async def _close_incident(incident: RecoveryIncident, *, status: str, source: str | None) -> bool:
    """Close the incident row. True when THIS CALL closed it.

    False means the driver no longer owns this incident — another closer got there
    first (an operator STOP through :func:`on_job_terminal`, a resume, the wire
    sweep), or the bookkeeping failed. Callers that go on to WRITE an outcome read
    it; :func:`_abort` deliberately does not, because its ``_blocked`` stamp and its
    cleanup are correct whoever closed the row.

    Best-effort — a bookkeeping failure must never turn a finished recovery into a
    crash, and the startup sweep re-resolves a row left open by one.
    """
    from backend.app.core.database import async_session

    if status == STATUS_ABORTED:
        # Bar re-entry for this exact fault until the wire re-arms it (see _blocked).
        # Stamped BEFORE the await so a DB failure cannot leave the loop unbounded.
        _blocked.setdefault((incident.printer_id, incident.job_id), set()).add(incident.fingerprint)
    try:
        async with async_session() as db:
            return await printer_incidents.close(db, incident.incident_id, status=status, source=source) is not None
    except Exception:  # noqa: BLE001 — never crash the driver on bookkeeping
        logger.exception(
            "spool_recovery: closing incident %s failed for printer %s", incident.incident_id, incident.printer_id
        )
        return False


async def _commit_out_of_rotation(
    incident: RecoveryIncident, global_tray: int, *, role: Literal["jammed", "replacement"]
) -> None:
    """THE one verb that parks a spool for this incident (pinned by AST test).

    It exists so "an extruder-side fault never parks a spool" is stated ONCE. The rule
    itself is not new — the driver has applied it to the REPLACEMENT since WS2 — but it
    lived at that one call site, so the JAMMED spool at the swap-commit boundary was
    parked by the same fault the rule says is not the spool's doing. 006-H2S 2026-09-21
    (incident 289): a ``0300_801E`` extruder overload, and 12 ms later a healthy roll
    was stamped out of rotation and the operator paged about it. Printer 8 on 09-11/12
    shows where that ends — the same fault parked trays 2→1→3→0 across one job and the
    run finished on ``no_eligible_spool``.

    ``role`` names WHICH spool for the log line only; both take the same rule, because
    the fault is the same fault whichever tray was feeding when it fired.

    No stamp means no page: the out-of-rotation notification fires INSIDE
    :func:`_mark_out_of_rotation`, so suppressing the write suppresses the announcement
    by construction rather than by a second condition. Everything downstream tolerates
    an unwritten stamp already — the clears (:func:`_clear_out_of_rotation_for_slot`)
    resolve nothing and return False, and no closer reads "was it stamped".
    """
    if incident.extruder_side_only:
        logger.info(
            "spool_recovery: printer %s %s tray %s kept IN rotation — extruder-side fault %s is the "
            "common factor, not the spool",
            incident.printer_id,
            role,
            global_tray,
            incident.code,
        )
        return
    await _mark_out_of_rotation(incident, global_tray)


async def _mark_out_of_rotation(incident: RecoveryIncident, global_tray: int) -> None:
    """Stamp ``feed_fault_at``/``feed_fault_code`` on the spool bound to
    ``global_tray`` (unbound slot → proceed anyway), broadcast inventory_changed, and
    fire the out-of-rotation notification.

    The WRITER only. Whether a spool may be parked at all is
    :func:`_commit_out_of_rotation`'s question, and it is this function's sole caller.

    The page is NOT optional and has no flag: parking a spool silently would take a
    roll out of every future dispatch with nothing telling the operator why their
    inventory shrank. The old ``notify`` parameter was single-valued at all three
    historical call sites — a switch nobody ever threw is a path nobody ever tested."""
    from backend.app.core.database import async_session
    from backend.app.core.websocket import ws_manager
    from backend.app.models.printer import Printer
    from backend.app.services.notification_service import notification_service

    ams_id, tray_id = decode_global_tray(global_tray)
    # ONE origin for the human slot name (:func:`runout_slot_desc`), so the
    # out-of-rotation page, the incident chip, the escalation and the firmware all say
    # "AMS A slot 2". The three renderings this module carried were 0-indexed and
    # disagreed with every other surface (004-H2S 2026-09-17, incident 192).
    slot_desc = runout_slot_desc(global_tray) or f"tray {global_tray}"
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
    return runout_slot_desc(global_tray) or f"tray {global_tray}"


async def _succeed(incident: RecoveryIncident, target: int, *, swapped: bool = True) -> None:
    """Recovery landed: close the incident RESOLVED and clear the hold projection.

    Closing re-arms this fault for the job (a genuine second tangle must still be
    handled) and counts toward the per-job flap cap — the bookkeeping is identical
    whether or not a swap happened.

    ``swapped`` (default True) is the ordinary jammed → replacement swap: rewrite
    the item's ams_mapping and fire the ``spool_recovery_succeeded`` notification
    (its copy is swap-and-out-of-rotation framed). ``swapped=False`` is the W1
    no-swap self-heal (a release verb freed the wedged change on the SAME feeder,
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
    # The source names WHO produced the outcome — the driver, by a swap or by the
    # firmware CONTINUE self-healing the same feeder — so the outcome ledger can tell
    # the farm's own recoveries from a touchscreen resume (``outcome_of``).
    #
    # It is also the OWNERSHIP test. Everything below is a claim about this incident
    # — the swapped ``ams_mapping`` written back onto the unit, its hold token
    # cleared, a "recovered" page — and a row somebody else already closed (an
    # operator STOP mid-round, say) has had its verdict given by them.
    source = RESOLVE_DRIVER_SWAP if swapped else RESOLVE_DRIVER_SELF_HEAL
    if not await _close_incident(incident, status=STATUS_RESOLVED, source=source):
        logger.warning(
            "spool_recovery: printer %s incident %s closed under the driver — success stands down",
            incident.printer_id,
            incident.incident_id,
        )
        return

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
                # succeeded copy would be false). slot_desc renders through the one
                # origin :func:`runout_slot_desc`, like every other slot name; a null
                # jammed tray falls back to a safe generic.
                jammed = incident.jammed_global_tray
                if jammed is None:
                    slot_desc = "the same slot"
                else:
                    slot_desc = runout_slot_desc(jammed) or f"tray {jammed}"
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
                "spool_recovery: printer %s RECOVERED at layer %s — a release verb self-healed the wedged change "
                "on feeder %s (no swap)",
                incident.printer_id,
                incident.layer_at_fault,
                incident.jammed_global_tray,
            )
    except Exception:  # noqa: BLE001 — never crash the driver
        logger.exception("spool_recovery: succeed handler failed for printer %s", incident.printer_id)


async def _restore_jammed_feeder(
    incident: RecoveryIncident, *, evidence: _RecoveryEvidence
) -> RestoreVerdict | StepTakeover | None:
    """Put the jammed spool back when the swap emptied the extruder and failed.

    The invariant the driver owns: it never hands a printer to a human with NOTHING
    loaded when the AMS would load something. After the selection reorder the only way
    the extruder is empty at a give-up is the residual — the swap committed, a
    replacement load did not complete, and no further candidate exists — and at that
    boundary a reload is state RESTORATION, not re-selection: invariant 7 holds (the
    stamp neither moves nor clears; it did jam, and the flag only bars auto-selection)
    and invariant 8 holds (this incident's unload precedes the reload).

    ``None`` = not needed (the feeder is not empty, or there is no jammed tray to load
    back). The outcomes are :data:`RestoreVerdict`; ``abort`` / ``handover`` propagate
    exactly as they do from every other step.

    The reload is ATTEMPTED in every posture, a mid filament-change included, and the
    wire's answer is what the page reports: ``complete`` → ``ok``; ``drying`` →
    ``skipped_drying``; anything else that never completed — the AMS not moving, or
    holding the reload behind the print's change — → ``fail`` (the evidence sentence
    names a held one). The pre-2026-09-23 refusal in a mid-change AMS rested on "the
    firmware drops every load" — measured only for a load into an empty path while the
    change-error modal stood; this sends and reads.

    Called ONLY by :func:`_give_up`, and only once that has established that a farm
    unload MOVED the AMS and that no command is held
    (``_RecoveryEvidence.unload_moved_ams`` / ``held_command``) — this function reads the
    WIRE, and the wire cannot tell an extruder the farm emptied from one the firmware
    retracted. What it adds is the other half: a driver that unloaded but whose jammed
    tray reads loaded again has nothing left to restore.

    It reuses :func:`_load_and_confirm` whole, so the drying pre-flight, the
    ``ams_command`` verb (its ``note_commanded_load`` mark — the backup-swap detector
    must not spend the departed spool) and the takeover handling are the swap's own, not
    a second copy. The reload is recorded as a command that went out, but it is NOT a
    candidate: :attr:`_RecoveryEvidence.loads_attempted` does not count it, and the
    escalation reason is chosen before it runs.

    A restore is a command the farm sends AND reads, so it needs a printer reporting on a
    live session (:func:`_reads_live`): a give-up met while the printer is off its session
    — the ``printer_offline`` one above all — restores nothing (``None``), and a reload
    whose answer the session took with it, or whose printer went offline mid-read, never
    demonstrably completed: ``fail``, never inferred ``ok``. It never raises
    :class:`_PrinterOffline`, so a give-up always reaches its page.
    """
    jammed = incident.jammed_global_tray
    state = _get_state(incident.printer_id)
    if jammed is None or not _reads_live(state):
        return None
    if _feeder_position(state, jammed, incident.printer_id).kind != "empty":
        return None
    try:
        load: LoadStep = await _load_and_confirm(incident, jammed, evidence=evidence)
    except _PrinterOffline:
        logger.warning(
            "spool_recovery: printer %s went off its session during the restore reload — restore unread",
            incident.printer_id,
        )
        load = "session_changed"
    verdict: RestoreVerdict
    match load:
        case "abort" | "handover":
            return load
        case "complete":
            verdict = "ok"
        case "drying":
            verdict = "skipped_drying"
        case "acted" | "no_movement" | "held" | "refused" | "session_changed":
            verdict = "fail"
        case _:
            assert_never(load)
    _log_candidate_outcome(incident, gtid=jammed, verdict=f"restore_{verdict}")
    return verdict


async def _give_up(incident: RecoveryIncident, reason: str, *, evidence: _RecoveryEvidence) -> None:
    """THE give-up boundary: restore what the swap moved, then escalate.

    Every ``_escalate`` reachable inside :func:`_drive_recovery`'s candidate loop and
    after it routes through here, so the restore decision is made ONCE from live state
    instead of inline conditions drifting apart, and the page is composed from the
    incident's own evidence log — and so does the offline bound (:class:`_PrinterOffline`,
    answered in :func:`_run_recovery`), whose restore is skipped by the restore's own
    live-session precondition rather than by a reason test. The escalations that never
    enter the loop (the entry gate, the runout branch) call :func:`_escalate` directly
    with ``restore=None`` and no evidence: they sent nothing, so they have nothing to
    restore and nothing to claim; the startup re-entry passes the hydrated log and
    restores nothing.

    :func:`_abort` and :func:`_hand_over` deliberately leave the extruder as it is —
    another actor owns the printer, and a takeover is never a give-up.
    """
    # The counters are read BEFORE the restore: the restore reload is not a candidate
    # and must not feed the numbers the reason was chosen from.
    unloads_sent, unloads_confirmed = evidence.unloads_sent, evidence.confirmed_unloads
    loads_attempted, loads_confirmed = evidence.loads_attempted, evidence.loads_confirmed
    # The restore exists to undo the DRIVER's OWN motion — nothing else. A give-up whose
    # unloads moved nothing (a CONTINUE-only ladder, a no-candidate verdict while
    # ``tray_now`` already read 255 because the firmware retracted after the fault — see
    # :func:`_resolve_jammed_tray` — or an unload the AMS held) leaves the printer exactly
    # as the firmware left it: moving filament there would be the farm acting on a state
    # it never created (invariant 8). The wire position alone cannot tell those apart,
    # because 255 is what they all look like. And never behind a HELD command: it runs at
    # the next release, so a reload sent behind it would run too — a held candidate load
    # already refills the extruder there.
    verdict = (
        await _restore_jammed_feeder(incident, evidence=evidence)
        if evidence.unload_moved_ams and evidence.held_command is None
        else None
    )
    if verdict == "abort":
        # A takeover DURING the restore must not un-stamp the spool: the operator
        # resumed on a feeder the FARM had just reloaded, so the click is not the
        # "I declare this spool usable" statement _clear_oor_if_resumed_on_jammed_feeder
        # reads it as.
        await _abort(incident, restored=True)
        return
    if verdict == "handover":
        _hand_over(incident)
        return
    logger.info(
        "[spool_recovery] give-up printer=%s reason=%s restore=%s unloads_sent=%s unloads_confirmed=%s "
        "loads_attempted=%s loads_confirmed=%s levers=%s",
        incident.printer_id,
        reason,
        verdict,
        unloads_sent,
        unloads_confirmed,
        loads_attempted,
        loads_confirmed,
        ",".join(f"{s.lever}:{s.outcome}" for s in evidence.lever_steps) or "-",
    )
    await _escalate(incident, reason, restore=verdict, evidence=evidence)


async def _escalate(
    incident: RecoveryIncident,
    reason: str,
    *,
    restore: RestoreVerdict | None = None,
    evidence: _RecoveryEvidence | None = None,
) -> None:
    """Give up: hold the incident ESCALATED, project the token, notify, leave PAUSED.

    NEVER resumes — a human must intervene. The incident stays OPEN (an escalation is
    a live hold, not a closed fault), which is what makes the printer un-re-enterable
    by a sibling code, keeps the printer-card chip lit, and arms the hourly attention
    reminder. It closes when the printer is observed RUNNING again, at the job's
    terminal, or when the refill auto-resume lands — never by a timer.

    A HELD AMS runout additionally carries the DURABLE spent stamp for the exhausted
    roll (``spool_respool.mark_spent_on_runout_hold``, guarded) — see the call below for
    why an escalation, and not a wire edge, is what a hold spanning a deploy can rely on.

    ``restore`` is :func:`_give_up`'s statement about an extruder the DRIVER emptied,
    and ``evidence`` its records of what it sent and what the wire answered; together
    they let the composed detail say what the farm did and where the filament is
    (:func:`_compose_detail`). Both are passed explicitly and never inferred: the
    defaults (``None``) are the truthful answer for every caller that sent nothing.
    """
    from backend.app.core.database import async_session
    from backend.app.models.printer import Printer
    from backend.app.services.notification_service import notification_service

    # Per-tray diagnostic snapshot on every escalation (the 18:45 forensics gap).
    await _log_tray_snapshot(incident)

    detail = _compose_detail(incident, reason, restore=restore, evidence=evidence)
    # The hold's projection onto the farm unit, one token per kind. A foreign print
    # has no unit — the incident row carries the whole state there.
    token = waiting_reason_for(incident.kind, external=incident.external)
    slot_hint = "the external spool holder" if incident.external else runout_slot_desc(incident.jammed_global_tray)
    try:
        async with async_session() as db:
            # Hold FIRST — even if the stamp/notify below fails, the incident must
            # already read ESCALATED so nothing restarts recovery behind the operator.
            #
            # It doubles as the OWNERSHIP test, which is why its answer is read.
            # ``mark_escalated`` returns None for a row that is already CLOSED, and
            # every line below writes a claim about this incident: the unit's hold
            # token, the operator page, and the durable ``recovery_escalation`` row
            # that feeds the 2-in-24 h quarantine. Writing them for a row another
            # closer already gave a verdict is how ONE 006-H2S jam produced two
            # escalations, two pages, two ledger rows and "Repeated AMS jam
            # escalations (2 in 24h)". Standing down here is also what makes that
            # ledger one-row-per-incident by construction, for EVERY closer that can
            # strike a live driver — no schema change, no incident key on the row.
            row = await printer_incidents.mark_escalated(db, incident.incident_id)
            if row is None:
                logger.warning(
                    "spool_recovery: printer %s incident %s closed under the driver — escalation (%s) stands down",
                    incident.printer_id,
                    incident.incident_id,
                    reason,
                )
                return
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
            # AMS keeps escalating within the window (reuse the SAME session). ONE
            # ledger row per incident: an upgrade re-escalates a row that may already
            # have paged and been recorded, and the ledger — not a flag — says so.
            await _record_escalation_and_maybe_quarantine(db, incident, reason, opened_at=row.created_at)
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


async def _record_escalation_and_maybe_quarantine(
    db: AsyncSession, incident: RecoveryIncident, reason: str, *, opened_at: datetime | None = None
) -> None:
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
        if opened_at is not None:
            # One row per INCIDENT, derived from the ledger itself (derive-don't-store):
            # a row for this printer stamped at or after the incident opened IS this
            # incident's row — the AMS kinds are mutually exclusive per printer, so no
            # other incident can have written one inside that window. An UPGRADE
            # re-escalates the same row with a worse kind and a fresh page (the operator
            # must learn the fault is now physical), but must not count twice toward
            # the 2-in-24 h quarantine — that is how 006-H2S (shape 36) was quarantined
            # for one jam.
            prior = await db.scalar(
                select(RecoveryEscalation.id)
                .where(RecoveryEscalation.printer_id == incident.printer_id)
                .where(RecoveryEscalation.created_at >= opened_at)
                .limit(1)
            )
            if prior is not None:
                logger.info(
                    "spool_recovery: printer %s incident %s re-escalated (%s) — page sent, ledger row %s already "
                    "records this incident; the row and the quarantine count are not repeated",
                    incident.printer_id,
                    incident.incident_id,
                    reason,
                    prior,
                )
                return
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


def _abort_source(token: TakeoverToken | None) -> str:
    """The ``resolve_source`` a stand-aside close records, per takeover token.

    ``paused_elsewhere`` is its own (``RESOLVE_PAUSED_ELSEWHERE``): the wire saw a quiet
    PAUSE, not a hand, and the actor it names is unknown. Every other token names an act
    only an operator produces on this farm — a Stop, a resume, a job change, their own
    AMS command, a load for another tray — and so does the verdict-only caller (no
    token), which is what ``operator`` has always recorded."""
    return RESOLVE_PAUSED_ELSEWHERE if token == "paused_elsewhere" else RESOLVE_OPERATOR


async def _abort(incident: RecoveryIncident, *, token: TakeoverToken | None = None, restored: bool = False) -> None:
    """Silent abort — somebody else owns this printer now. Stop acting and drop our
    stale ``recovering`` flag (the print is being handled elsewhere).

    ``token`` is :func:`_takeover`'s verdict where the caller has it, so the closing
    line names the same fact the step's own line named and the close records the
    source that token implies (:func:`_abort_source` — ONE abort path, the source per
    token). ``None`` covers the callers that only ever see the step VERDICT.

    If that actor resumed ON the jammed feeder (live RUNNING with ``tray_now`` ==
    the jammed global tray), they declared that spool usable — clear its
    out-of-rotation flag the same way a physical re-insert would, so a self-cleared
    jam does not leave the spool excluded from all future dispatch. Any other live
    state keeps the flag (a physical reseat stays the canonical clear).

    ``restored`` is the one thing that reading cannot survive: the driver ITSELF put
    the jammed spool back on the feeder (:func:`_restore_jammed_feeder`), so "they
    resumed on the jammed feeder" says nothing about what the operator thinks of the
    spool — the farm chose that feeder, not them. The stamp stays; remove+re-insert or
    the Inventory control clears it."""
    from backend.app.core.database import async_session

    # Close FIRST (before any await that could fail): an external actor owns this
    # printer now — an ABORTED close both frees the open slot and bars re-entry for
    # this exact fault on this job, so a sibling code cannot restart recovery under
    # them. An external actor is the only thing that reaches this path; the token
    # says which kind (:func:`_abort_source`).
    source = _abort_source(token)
    await _close_incident(incident, status=STATUS_ABORTED, source=source)

    logger.info(
        "spool_recovery: printer %s recovery aborted (%s) source=%s",
        incident.printer_id,
        token or "external_interference",
        source,
    )
    try:
        async with async_session() as db:
            item = await db.get(PrintQueueItem, incident.item_id) if incident.item_id is not None else None
            if item is not None and item.waiting_reason == WAITING_REASON_RECOVERING:
                item.waiting_reason = None
                await db.commit()
            if not restored:
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
#
# ONE exception, and it is about WHO owns the outcome rather than about the source:
# while a recovery driver is live, the driver owns it (006-H2S 2026-09-04). Behaviour
# delta from that — small, bounded, and more honest than what it replaced: a screen
# resume landing inside the driver's own confirm wait no longer closes the incident
# as ``resolved``/``observed_running`` from underneath it. The driver observes the
# same RUNNING itself, reads it as the external takeover it is, and ends the incident
# as ``aborted``/``operator``, which additionally bars re-entry for that exact fault
# (its fingerprint sits in ``_blocked``) until the wire re-arms it — the fault
# clearing, or the printer pausing again. That is the truthful record of what
# happened: a human took the printer, not "recovery succeeded".


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

    WHICH rows a RUNNING edge ends is :mod:`incident_resolution`'s answer, per row,
    not a class chain spelled here (a printer can hold several holds, each with its
    own return-to-normal rule). This closer supplies the occasion and the two facts
    only it knows: the live state, and whether a recovery DRIVER is live
    (``printer_incidents.driver_live``, ``.done()``-aware, so an R1 orphan left by a
    mid-recovery crash never defers a close).

    Returns True when at least one incident was closed. Never raises (invariant 10).
    """
    try:
        from backend.app.core.database import async_session

        ctx = Context(
            state=_get_state(printer_id),
            ledger=ledger,
            driver_live=printer_incidents.driver_live(printer_id),
        )
        closed: list[tuple[int, str, str, bool]] = []
        async with async_session() as db:
            for incident in await printer_incidents.open_rows(db, printer_id):
                verdict = incident_resolution.resolve(incident, "running_edge", ctx)
                if not verdict.close:
                    logger.info(
                        "spool_recovery: printer %s RUNNING edge — %s incident %s left OPEN: %s",
                        printer_id,
                        incident.kind,
                        incident.id,
                        verdict.evidence,
                    )
                    continue
                item_id, kind, status = incident.item_id, incident.kind, incident.status
                if await printer_incidents.close(db, incident.id, status=STATUS_RESOLVED, source=verdict.source):
                    closed.append((incident.id, kind, status, await _clear_hold_projection(db, item_id)))
        for incident_id, kind, status, cleared in closed:
            logger.info(
                "spool_recovery: printer %s observed RUNNING — %s incident %s closed (was %s)%s",
                printer_id,
                kind,
                incident_id,
                status,
                "; hold token cleared" if cleared else "",
            )
        return bool(closed)
    except Exception:  # noqa: BLE001 — a lifecycle hook must never crash the status flow
        logger.exception("spool_recovery: observed-running close failed for printer %s", printer_id)
        return False


async def on_job_terminal(printer_id: int, terminal: TerminalEvent) -> bool:
    """A print reached a terminal — close the holds that terminal answers.

    A JOB HOLD cannot outlive the job; an EQUIPMENT FAULT can. That distinction is
    the whole of 003-H2S 2026-09-11: the row was both at once, so the operator's stop
    ended the equipment fault along with the job — the firmware wiped its HMS list at
    the same terminal, and two minutes later the scheduler dispatched the next unit
    onto the same printer and the same filament, still stuck in the shared PTFE path.
    Three times.

    Which rows it answers is :mod:`incident_resolution`'s table, per row: a ``wire``
    hold closes, and since 2026-09-17 a ``repair`` hold closes too when THE JOB THE
    FAULT INTERRUPTED ran to ``completed`` — the commonest fleet repair, and the
    evidence 011-H2S produced and nobody counted. The ``operator`` and ``declared``
    classes take neither.

    ``terminal`` carries the firmware's OWN word (``main.on_print_complete``'s
    ``_raw_status``, captured before the operator-UI rewrite), that callback's eject
    flag, and the ``subtask_id``. All three are facts only the completion callback
    holds, which is why they are passed rather than re-derived here.

    The farm unit's own ``waiting_reason`` hygiene stays ``farm_policy.on_terminal``'s
    job (W4b) — this only closes incidents, so the two never fight over one row.
    """
    try:
        from backend.app.core.database import async_session

        ctx = Context(
            state=_get_state(printer_id),
            ledger=ledger,
            driver_live=printer_incidents.driver_live(printer_id),
            terminal=terminal,
        )
        closed: list[tuple[int, str, str]] = []
        async with async_session() as db:
            for incident in await printer_incidents.open_rows(db, printer_id):
                verdict = incident_resolution.resolve(incident, "job_terminal", ctx)
                if not verdict.close:
                    logger.info(
                        "spool_recovery: printer %s reached a terminal (%s) — %s incident %s left OPEN: %s",
                        printer_id,
                        terminal.status,
                        incident.kind,
                        incident.id,
                        verdict.evidence,
                    )
                    continue
                row = await printer_incidents.close(db, incident.id, status=STATUS_RESOLVED, source=verdict.source)
                if row is not None:
                    closed.append((row.id, row.kind, verdict.evidence))
        for incident_id, kind, evidence in closed:
            logger.info(
                "spool_recovery: printer %s reached a terminal — %s incident %s closed — %s",
                printer_id,
                kind,
                incident_id,
                evidence,
            )
        return bool(closed)
    except Exception:  # noqa: BLE001 — a lifecycle hook must never crash the completion flow
        logger.exception("spool_recovery: terminal close failed for printer %s", printer_id)
        return False


async def sweep_open_incidents(*, now: float | None = None) -> int:
    """Close ESCALATED incidents whose own evidence says they are over. Returns the count.

    The missing lifecycle path. An incident closes when the printer is seen RUNNING
    (:func:`on_observed_running`), when its job reaches a terminal
    (:func:`on_job_terminal`), when the refill auto-resume lands, or when a restart's
    rearm finds the printer positive. Nothing closed an incident because its FAULT
    CLEARED — so an operator who cleared a jam on an idle printer left the hold
    standing until the next print happened to run (the 2026-08-19 fleet event: seven
    printers held up to 426 min each), or forever when there was no next print,
    because the hold is what blocks dispatch (001-H2S #60).

    What counts as "over" is per RESOLUTION CLASS and belongs to
    :mod:`incident_resolution` — "the wire went quiet" means opposite things for
    different kinds, and for an AMS ``physical`` fault it means nothing at all (the
    firmware wipes its HMS list at every terminal, so a stuck filament reads exactly
    like a repaired one). This sweep owns the three things the table cannot know:

    1. WHO may be acted on — :func:`incident_resolution.driver_owns`. A row reading
       ``recovering`` has a live driver, or the restart lane's own re-entry, and
       closing it from underneath would race the machine acting on it.
    2. WHAT the printer is saying — a cached state read during a disconnect is a
       memory, not evidence, so a disconnected printer reports ``None``.
    3. The DWELL. A verdict carrying ``dwell`` must hold continuously for
       :data:`_HOLD_OVER_DWELL_S` before it acts: the fault that OPENED #60 was
       evaluated 78 ms after a dispatch, when the printer read non-PAUSE for an
       instant, and a level-triggered close with no dwell would make the same mistake
       in the opposite direction. :data:`_hold_over_since` is that timer and stays
       here — it is the sweep's own clock, not evidence.

    The repair lane also SELF-HEALS (doctrine rule 1): when the evidence is a
    completed load and the printer is still PAUSEd on the very job the fault
    interrupted, the operator freed the path and reloaded a slot by hand, and making
    them walk back to a screen is the deferral that rule forbids. One resume per
    incident, published on the FIRST sighting; the close still waits out the dwell.

    Guarded end to end and per incident: this runs from the scheduler tick and must
    never kill it (invariant 10).
    """
    now = _monotonic() if now is None else now
    closed = 0
    try:
        from backend.app.core.database import async_session

        async with async_session() as db:
            rows = await printer_incidents.all_open(db)
            live_ids = {inc.id for inc in rows}
            for stale in [iid for iid in _hold_over_since if iid not in live_ids]:
                _hold_over_since.pop(stale, None)
            for stale in [iid for iid in _repair_resume_sent if iid not in live_ids]:
                _repair_resume_sent.discard(stale)

            for incident in rows:
                try:
                    pid = incident.printer_id
                    if driver_owns(incident, live=printer_incidents.driver_live(pid)):
                        _hold_over_since.pop(incident.id, None)
                        continue
                    state = _get_state(pid) if printer_manager.is_connected(pid) else None
                    verdict = incident_resolution.resolve(
                        incident,
                        "sweep_tick",
                        Context(state=state, ledger=ledger, driver_live=False),
                    )
                    if not verdict.close:
                        _hold_over_since.pop(incident.id, None)
                        continue
                    live = (getattr(state, "state", None) or "") if state is not None else ""
                    await _maybe_self_heal_after_repair(incident, state, evidence=verdict.evidence, live=live.upper())

                    if verdict.dwell:
                        first = _hold_over_since.get(incident.id)
                        if first is None:
                            _hold_over_since[incident.id] = now
                            continue
                        if now - first < _HOLD_OVER_DWELL_S:
                            continue
                    else:
                        first = now

                    await printer_incidents.close(db, incident.id, status=STATUS_RESOLVED, source=verdict.source)
                    cleared = await _clear_hold_projection(db, incident.item_id)
                    _hold_over_since.pop(incident.id, None)
                    _repair_resume_sent.discard(incident.id)
                    closed += 1
                    logger.info(
                        "spool_recovery: printer %s incident %s (%s) closed — %s (state=%s, %s for %.0fs)%s",
                        pid,
                        incident.id,
                        incident.kind,
                        verdict.source,
                        live,
                        verdict.evidence,
                        now - first,
                        "; hold token cleared" if cleared else "",
                    )
                except Exception:  # noqa: BLE001 — one bad incident must not abort the sweep
                    logger.exception("spool_recovery: incident sweep failed for incident %s", incident.id)
    except Exception:  # noqa: BLE001 — a scheduler-tick watch must never kill the tick
        logger.exception("spool_recovery: wire-clear incident sweep failed")
    return closed


async def _maybe_self_heal_after_repair(incident, state, *, evidence: str, live: str) -> None:
    """Resume a print whose path the operator just repaired by hand. Never raises.

    The shape: a ``physical`` hold, the printer still PAUSEd on the very job the fault
    interrupted, and a filament change completed onto a real feeder AFTER the fault
    opened. That is somebody freeing the path at the printer and loading a slot — the
    repair itself is their "go", and doctrine rule 1 says the farm does not make them
    walk back to a screen for the button.

    Bounded to ONE resume per incident (:data:`_repair_resume_sent`): the evidence
    holds for the whole of the sweep's dwell, so an unbounded arm would publish one
    resume per tick. It is deliberately sent BEFORE the dwell — the dwell exists to
    stop a momentary reading CLOSING a hold, not to delay acting on a repair — and
    the row still closes the ordinary way, through the sweep, on the same evidence.

    Evidence (b) is excluded by construction: it requires RUNNING, and this requires
    PAUSE.
    """
    if evidence != _REPAIR_EVIDENCE_LOAD or live != "PAUSE":
        return
    job = (getattr(state, "subtask_id", None) or "").strip()
    if not job or job != (incident.job_id or ""):
        return
    if incident.id in _repair_resume_sent:
        return
    _repair_resume_sent.add(incident.id)
    from backend.app.core.tasks import spawn_background_task

    logger.info(
        "spool_recovery: printer %s incident %s — the filament path was repaired and the print is still "
        "PAUSEd on the same job; resuming it",
        incident.printer_id,
        incident.id,
    )
    spawn_background_task(
        _resume_after_repair(incident.printer_id, incident.id),
        name=f"repair-resume-p{incident.printer_id}",
    )


async def rearm_incidents_on_startup() -> int:
    """Reconcile incidents left open by a restart, then rehydrate the chip cache.

    An incident is a hold on a PHYSICAL printer, so the restart itself proves nothing
    about it — the printer may have been resumed, finished, or still be sitting PAUSEd
    exactly as we left it. The evidence question is
    :mod:`incident_resolution`'s ``startup`` occasion, which is the per-tick sweep's
    own ladder with NO DWELL: a restart re-derives every wire fact from scratch, so a
    printer already running has answered the question and there is no momentary
    reading to wait out.

    Two consequences of the restart worth stating, because they read as gaps:

    * the motion ledger is EMPTY by construction, so the only repair evidence
      available here is a print actually RUNNING — ``tray_now`` naming a slot is a
      LEVEL a stuck-filament printer reports just as readily (003-H2S);
    * a printer that has not reported yet closes nothing and is decided later, by the
      wire sampler's first RUNNING transition.

    A row still reading ``recovering`` and NOT closed is the one shape no evidence
    answers: that status is a PROMISE that a task is acting, and the restart broke it.
    It is re-entered separately (:func:`_reenter_recovering_incident`) — left alone it
    would sit open forever with nothing driving it, and because the AMS kinds are
    mutually exclusive it would also block every future AMS incident on that printer
    for good. ``driver_owns`` is the ONE spelling of "a driver owns this row"; at
    startup no task can be live yet, so it reads exactly the ``recovering`` status.

    Returns the number of incidents closed. Never raises — startup must not block.
    """
    closed = 0
    try:
        from backend.app.core.database import async_session

        driverless: list[tuple[int, int]] = []
        async with async_session() as db:
            for incident in await printer_incidents.all_open(db):
                pid = incident.printer_id
                live = printer_incidents.driver_live(pid)
                verdict = incident_resolution.resolve(
                    incident,
                    "startup",
                    Context(state=_get_state(pid), ledger=ledger, driver_live=live),
                )
                if not verdict.close:
                    if driver_owns(incident, live=live):
                        driverless.append((incident.id, pid))
                    continue
                await printer_incidents.close(db, incident.id, status=STATUS_RESOLVED, source=verdict.source)
                await _clear_hold_projection(db, incident.item_id)
                closed += 1
                logger.info(
                    "spool_recovery: startup — printer %s incident %s (%s) closed — %s",
                    pid,
                    incident.id,
                    incident.kind,
                    verdict.evidence,
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

    A wire with no actionable fault left is asked the PATH question
    (``incident_resolution.path_quiet``), never the HMS list alone: a CONTINUE empties the
    HMS list while the AMS still holds the change it re-ran (012-H2S 2026-09-23), so a
    mid-change AMS is NOT quiet — the row's own facts stand in for the wire's and the
    driver re-enters, its evidence log hydrated from the step ledger, at the next UNPULLED
    lever. A quiet path is still not "fine": the printer is PAUSEd (or has not reported)
    with a swap half-executed — possibly with the jammed feeder unloaded and nothing
    loaded in its place — so it escalates ``recovery_interrupted`` rather than closing,
    carrying the hydrated log. The lifecycle closes it for free the moment the printer is
    seen RUNNING.
    """
    try:
        state = _get_state(printer_id)
        candidates = live_candidates(state)

        from backend.app.core.database import async_session
        from backend.app.models.printer import Printer

        async with async_session() as db:
            row = await printer_incidents.get_open(db, printer_id, kinds=AMS_FAULT_KINDS)
            if row is None or row.id != incident_id or row.status != STATUS_RECOVERING:
                return None  # another lane resolved it between the sweep and here
            settings = await _read_settings(db)
            job_id = (getattr(state, "subtask_id", None) or "").strip() or row.job_id
            item = await _resolve_farm_item(db, printer_id, job_id)
            evidence = await _RecoveryEvidence.from_row(db, incident_id)
            fault_class = _dominant_class(candidates)
            if fault_class is None:
                # No actionable candidate: the row's facts are the evidence left.
                kind, external = row.kind, False
                code, fingerprint = row.code, row.codes
                tray = row.slot_global_tray
                if incident_resolution.path_quiet(state):
                    escalate_reason = "recovery_interrupted"
                else:
                    escalate_reason = await _route_fault(
                        db,
                        printer_id=printer_id,
                        job_id=job_id,
                        kind=kind,
                        external=external,
                        verdict="single",
                        tray=tray,
                    )
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
            if printer_incidents.automation_held(printer_id):
                # Same rule at the startup re-entry as at the entry gate: a restart must
                # not hand a driver back to a printer a human is standing in front of,
                # and the same ``or`` — the no-candidate branch above keeps
                # ``recovery_interrupted``, a live runout keeps ``runout_needs_refill``
                # (and its spent stamp), and only a driver-bound fault reads
                # ``service_hold``.
                escalate_reason = escalate_reason or _held_escalate_reason(kind, external=external)
            printer = await db.get(Printer, printer_id)
            incident = _build_incident(
                state,
                candidates,
                incident_id=incident_id,
                printer_id=printer_id,
                job_id=job_id,
                settings=settings,
                item_id=item.id if item is not None else None,
                kind=kind,
                code=code,
                fingerprint=fingerprint,
                tray=tray,
                external=external,
                printer_name=(printer.name if printer else None) or f"printer {printer_id}",
                fallback_code=row.code,
            )

        logger.info(
            "spool_recovery: startup — printer %s incident %s was left mid-swap, re-entering (%s)",
            printer_id,
            incident_id,
            f"escalating={escalate_reason}" if escalate_reason else f"kind={kind} tray={tray}",
        )
        if escalate_reason is not None:
            await _escalate(incident, escalate_reason, evidence=evidence)
            return None
        task = asyncio.create_task(_run_recovery(incident))
        printer_incidents.register_driver(printer_id, task, incident_id=incident_id)
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
    return await printer_incidents.get_open(db, printer_id, kinds={KIND_RUNOUT})


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
        # The one codec (invariant 1). A slot it cannot NAME stands down exactly as a
        # missing demand does: this message's whole job is to tell the operator WHICH
        # slot to refill, and a hand-rolled id would send them to a different tray.
        global_tray = encode_global_tray(*demand)
        if global_tray is None:
            return False

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


async def _resume_after_evidence(printer_id: int, *, ready, name: str) -> bool:
    """Settle, re-check, publish ONE resume, confirm RUNNING. Never raises.

    The shared body behind every evidence-driven auto-resume — the runout refill
    (006-H2S 2026-07-26) and the repaired filament path (003-H2S 2026-09-11). What it
    owns is the SHAPE, which is the part that must not be re-derived per lane: settle
    before publishing (a resume sent on the edge itself races the firmware's own
    settle and is simply rejected), re-ask the caller's evidence AFTER the settle
    because the world moves, send exactly ONE resume, and stand aside on ANY doubt —
    no retry, no quarantine, no stamp, the escalation's guidance untouched, so the
    operator's manual resume stays the reliable path.

    ``ready`` is the lane's own evidence, asked twice; ``name`` names the occasion in
    every stand-aside line. Returns True only on a CONFIRMED RUNNING — the caller
    owns what that success means for its own bookkeeping.
    """
    try:
        if not await ready():
            return False

        # Let the printer register what just happened; see the docstring.
        await asyncio.sleep(_RUNOUT_RESUME_SETTLE_S)

        st = _get_state(printer_id)
        if st is not None and getattr(st, "state", None) == "RUNNING":
            logger.info(
                "spool_recovery: printer %s already RUNNING after the %s (operator resumed) — auto-resume stood down",
                printer_id,
                name,
            )
            return False
        if not await ready():
            logger.info(
                "spool_recovery: printer %s %s auto-resume stood down after settle (state/wire/hold changed)",
                printer_id,
                name,
            )
            return False

        client = printer_manager.get_client(printer_id)
        if client is None:
            logger.info("spool_recovery: printer %s has no client — %s auto-resume skipped", printer_id, name)
            return False
        if not client.resume_print():
            logger.info(
                "spool_recovery: printer %s resume_print send returned False (offline?) — "
                "%s auto-resume stands aside, escalation guidance stands",
                printer_id,
                name,
            )
            return False
        if not await printer_manager.await_state(
            printer_id, {"RUNNING"}, _RUNOUT_RESUME_CONFIRM_S, poll_interval_s=_POLL_INTERVAL_S
        ):
            logger.info(
                "spool_recovery: printer %s did not reach RUNNING within %.0fs after the %s resume — "
                "standing aside (no retry); the escalation guidance stands",
                printer_id,
                _RUNOUT_RESUME_CONFIRM_S,
                name,
            )
            return False
        return True
    except Exception:  # noqa: BLE001 — an assist lane must never crash its caller
        logger.exception("spool_recovery: %s auto-resume failed for printer %s", name, printer_id)
        return False


async def _resume_after_refill(printer_id: int, slot: tuple[int, int] | None) -> bool:
    """Resume a runout-held print once the wire says the filament is back.

    006-H2S 2026-07-26: the runout escalation leaves the print PAUSEd for a same-slot
    refill, and the refill itself is the operator's "go" — but the print then sat
    waiting for someone to press Resume. Doctrine rule 1 (minimal human interaction):
    the recoverable half of this state is recoverable without hands.

    ONE evidence behind TWO spawn sources — the ``ams_presence`` presence-GAIN edge
    (a roll went in) and :func:`note_demand_watch`'s wire edges (the firmware stopped
    asking). Before WS2b only the gain edge existed, so the 006 class — a demand
    naming a slot that was ALREADY loaded — could never resume, and auto-resume had
    never fired in production at all.

    The resume MECHANICS are :func:`_resume_after_evidence`'s; this owns the evidence
    (``runout_auto_resume_enabled`` + :func:`_resume_ready`) and the success
    bookkeeping. Returns True only on a confirmed RUNNING.
    """

    async def _ready() -> bool:
        # Cheap, DB-free pre-gate FIRST. This runs on every physical spool insert on
        # every printer, and almost none of them are a runout refill — opening a
        # session before knowing that is pure waste (it showed up as a measurable
        # slowdown across the suite).
        st = _get_state(printer_id)
        if st is None or getattr(st, "state", None) != "PAUSE" or not _refill_ready(st, slot):
            return False

        if printer_incidents.automation_held(printer_id):
            # MAINTENANCE MODE. Resuming a print is an ACT, and this is the one place
            # this lane decides to perform it — BOTH spawn sources (the presence-GAIN
            # edge and ``note_demand_watch``'s wire edges) reach the resume through
            # here, so one read covers them. The demand SAMPLER above is deliberately
            # not gated: watching the wire is observation, and the hold's whole shape
            # is "keep looking, do nothing".
            #
            # The runout incident stays OPEN and its guidance stands: the operator
            # resuming on the screen closes it on the wire exactly as it does today.
            # Logged because a refill that visibly does not resume needs a reason in
            # the log, and it is the first ask — ``_resume_after_evidence`` short-circuits
            # before its settle — so this is ONE line per spawn, not one per poll.
            logger.info(
                "spool_recovery: printer %s in maintenance mode — refill seen, not resuming; "
                "resume on the printer or release the hold",
                printer_id,
            )
            return False

        if printer_incidents.job_pause_held(printer_id):
            # The printer ALSO paused this job to ask a human about the plate. A refill
            # resume would answer that question too, onto the plate it refused — the one
            # predicate every job-resuming lane reads. The runout hold stays open and the
            # human's resume closes both.
            logger.info(
                "spool_recovery: printer %s is held at a job pause (the printer's plate check) — refill seen, "
                "not resuming; the operator resumes on the printer",
                printer_id,
            )
            return False

        from backend.app.core.database import async_session

        async with async_session() as db:
            if not await _read_bool(db, "runout_auto_resume_enabled", _DEFAULT_RUNOUT_AUTO_RESUME):
                return False
            return await _resume_ready(db, printer_id, slot) == "ready"

    if not await _resume_after_evidence(printer_id, ready=_ready, name="refill"):
        return False
    await _close_runout_hold_and_notify(printer_id, slot)
    logger.info(
        "spool_recovery: printer %s RESUMED automatically after the filament refill (slot %s)",
        printer_id,
        slot,
    )
    return True


async def _resume_after_repair(printer_id: int, incident_id: int) -> bool:
    """Resume a print whose filament path the operator repaired by hand. Never raises.

    The 003-H2S self-heal, and the other half of what the ``repair`` resolution class
    means: the commonest fix for a physical fault on this farm is somebody freeing
    the path and loading a slot at the printer — 23 of the AMS-side physical rows
    closed on a resume with no filament change at all — after which the print sits
    PAUSEd for a button nobody is standing there to press.

    Deliberately NOT gated on ``runout_auto_resume_enabled``: that setting is the
    operator's control over the RUNOUT lane (a refill they may want to inspect before
    the print continues), and reading it here would make one switch govern two
    different decisions. The evidence is the gate.

    Its ``ready`` re-asks everything that could have moved: the printer is still
    PAUSEd, the row is still open and still repair-resolved, it is still the SAME job
    the fault interrupted, and the rule table's own repair evidence still holds. On a
    confirmed RUNNING
    it logs and does nothing else — the sweep's own (b) arm closes the row after the
    dwell, so there stays ONE closer.
    """

    async def _ready() -> bool:
        st = _get_state(printer_id)
        if st is None or (getattr(st, "state", None) or "") != "PAUSE":
            return False

        if printer_incidents.automation_held(printer_id):
            # MAINTENANCE MODE, and this lane is the sharpest case for it: the evidence
            # it resumes on IS the maintenance operator's own work — they freed the path
            # and loaded a slot by hand — so without this read, repairing a printer would
            # restart its print under the hands that repaired it.
            #
            # Same shape and same one-read rule as the refill lane: after the cheap
            # DB-free pre-gate, before the session, at the one point this lane decides to
            # act. Reaching here already means the spawner saw the repair (it requires
            # the load evidence, PAUSE and the same job, and dedups per incident), so the
            # line is true when it prints and prints once per incident.
            #
            # The physical incident stays OPEN and closes the ordinary way — its own
            # repair rule, on the wire, when the operator resumes or after the hold lifts.
            logger.info(
                "spool_recovery: printer %s in maintenance mode — repair seen, not resuming; "
                "resume on the printer or release the hold",
                printer_id,
            )
            return False

        if printer_incidents.job_pause_held(printer_id):
            # Same rule as the refill lane: the printer paused this job for a human's
            # answer about the plate, and a repair resume would answer it for them.
            logger.info(
                "spool_recovery: printer %s is held at a job pause (the printer's plate check) — repair seen, "
                "not resuming; the operator resumes on the printer",
                printer_id,
            )
            return False

        job = (getattr(st, "subtask_id", None) or "").strip()

        from backend.app.core.database import async_session

        async with async_session() as db:
            row = await db.get(PrinterIncident, incident_id)
            if row is None or row.resolved_at is not None:
                return False
            if not job or job != (row.job_id or ""):
                return False
            # The same question the sweep asks, asked of the same table: does this
            # row's own evidence still hold? A row of any OTHER class answers False
            # here by construction — the printer is PAUSEd, which is every other
            # class's "still held" reading — so the class test is the table's, not a
            # second copy of it spelled in this lane.
            return incident_resolution.resolve(
                row,
                "sweep_tick",
                Context(state=st, ledger=ledger, driver_live=printer_incidents.driver_live(printer_id)),
            ).close

    if not await _resume_after_evidence(printer_id, ready=_ready, name="path repair"):
        return False
    logger.info(
        "spool_recovery: printer %s RESUMED automatically after the path repair (incident %s)",
        printer_id,
        incident_id,
    )
    return True


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
        feeder = tray_fields.valid_feeder(getattr(state, "tray_now", None))
        prev = _wire_sample.get(printer_id)
        _wire_sample[printer_id] = (live, demand, externals, epoch, feeder)

        # THE one writer of the motion ledger. This sampler is the only thing in the
        # farm that sees every push, so it hands each reading to the ledger that owns
        # the repair evidence — the completed-load EDGE (session-scoped: the epoch
        # goes with it, because a reconnect re-seeds every wire fact at once) and the
        # "a non-eject print was RUNNING here" sighting. Deliberately BEFORE the
        # first-sample return below: the ledger keeps its own seed, so the two facts
        # it derives never depend on which of this sampler's edges ran first.
        ledger.observe(printer_id, state, epoch)

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
        prev_state, prev_demand, prev_externals, prev_epoch, prev_feeder = prev

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
                # The live slot OVERRIDES the stored one — but only when the codec can
                # name it (invariant 1). An unnameable slot leaves the incident's own
                # tray standing rather than blanking a good answer with a bad one.
                encoded = encode_global_tray(*slot)
                if encoded is not None:
                    tray = encoded
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


def clear_out_of_rotation(spool: Spool) -> bool:
    """Return a spool to rotation: NULL both feed-fault columns. True when it changed.

    THE one owner of what "back in rotation" WRITES — the flag and the code it was
    stamped with, together. The operator's "Return to rotation" (``PATCH
    /inventory/spools/{id} {"feed_fault_at": null}``) used to null only the column it
    named, leaving ``feed_fault_code`` standing as a stale diagnosis on a healthy
    roll (002-H2S's spool 599, 2026-09-11). Callers own the commit and the
    ``inventory_changed`` broadcast — they already do.
    """
    if spool.feed_fault_at is None and spool.feed_fault_code is None:
        return False
    spool.feed_fault_at = None
    spool.feed_fault_code = None
    return True


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

    clear_out_of_rotation(spool)
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
