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
    If nothing works: give up — refill an EMPTY toolhead (whoever emptied it), notify, and
    leave the printer PAUSED for a human, never resume blind.

    SELECTION PRECEDES THE COMMIT (004-H2S 2026-09-17, incident 192). The swap used to
    stamp and unload before it knew a replacement existed, so an honest "no eligible
    spool" verdict left a printer with an empty extruder that no copy described; the
    operator's Resume then printed 4 h of air. With nothing to load there is now no
    unload, and a give-up over an EMPTY toolhead refills it before it pages
    (:func:`_give_up` → :func:`_refill_toolhead`, 2026-10-10) — the machine is symmetric.

    Out-of-rotation stamping/notification is bound to the SWAP-COMMIT boundary (the
    step just before the first unload), NOT to entry: a no-swap firmware self-heal
    (a release verb frees the change on the same spool) must never stamp or announce a
    spool the print keeps running on, and fires its own truthful self-heal
    notification instead (2026-07-20 incident — an entry-time stamp+announce, then a
    same-spool self-heal 90 s later, misled the operator).

    The feeder-stall WEDGE (009-H2S 2026-07-20, 002-H2S 2026-09-11, 012-H2S
    2026-09-23). After a feed fault the AMS can sit mid filament-change (gcode_state
    PAUSE, ``ams_status_main == 1``) holding the PRINT's own change: 012-H2S re-ran its
    own feed on the stalled slot 48 s before any farm command (21:44:48), and every
    command sent into that change was ACKNOWLEDGED and HELD behind it, then run in order
    at the next release (six commands echoed success, moved nothing, and ran after the
    terminal). A held UNLOAD that drains while the print RUNS empties the extruder
    mid-print, so the farm sends NO swap command into the change. While the AMS's own
    state word (``bambu_mqtt.ams_mid_filament_change``, the one predicate) reads
    mid-change, a round runs the RELEASE LADDER instead (:data:`_LEVERS` — every verb the
    wire offers, in evidence order, each pulled up to its own budget per incident and read
    by ONE reader, :func:`_read_after`): the firmware may self-heal on the jammed feeder (no
    swap), release the AMS (the swap goes out at once), re-hold (the next verb), or end
    the print (the driver records that itself). The last rung stops a FARM job that has
    deposited nothing and restarts it (:func:`_restart`: unload, park, requeue — operator
    ruling 2026-09-29); every verb spent with the change still held is
    ``wedge_unreleased``, with NOTHING sent. A non-``1`` non-idle state is NOT a
    wedge: 006-H2S 2026-07-21 faulted at ``ams_status_main == 3`` (assist) with the
    feeder still engaged and the unload was accepted immediately — that round runs the
    swap machine directly. A swap command the AMS acknowledges and HOLDS is never resent:
    held INSIDE a change (the AMS re-entered one before it ran) it is the give-up
    ``swap_held_after_release``; held OUTSIDE a change — the AMS accepted it and has not run
    it, and runs it on its own minutes later (011/014-H2S 2026-10-09/10) — the driver waits
    for it to run and continues the contract (:func:`_await_pending_command`), and never sends
    behind a command still pending, after a farm restart included.

    Repeat-jam quarantine: a printer whose recovery escalates repeatedly within a rolling window is
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
that went out on ONE durable evidence log (:class:`_RecoveryEvidence`, the store's
``printer_incidents.EvidenceLog`` bound to this driver's steps, persisted step by step),
so the give-up reason, the operator page
and a restarted driver's next lever are derived from what was sent and what the wire
said, never from a premise.

SCOPE (2026-08-09). Entry is :func:`on_ams_fault`, and what it acts on comes
from the fault TAXONOMY (``hms_errors.classify_hms_entry`` over both wire
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
  Without an incident nothing consumes this class: those faults wait on the
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
resolved incidents. The old process-lifetime ``_escalated`` latch never expired inside a
process, so a LATER, different fault on the same job could never be recovered; and
because the whole entry gate required a matching FARM queue item, 12 foreign-print
runouts were spent-stamped while nothing alerted, held or resumed.

Replacement selection reuses the same ``spool_selection`` functions the dispatcher
uses (out-of-rotation exclusion is already baked into them); nothing here
duplicates that policy.

Entries (all spawned guarded from ``main``, none ever raises):
:func:`on_ams_fault` (per status push), :func:`note_demand_watch` (the per-push
wire sampler that drives refill auto-resume, closes a hold the moment the
printer runs again, and runs the empty-toolhead detectors D1 / D2 —
:func:`_sample_toolhead`), :func:`on_observed_running`, :func:`on_job_terminal`,
:func:`sweep_open_incidents` (the scheduler-tick close for a hold whose FAULT
cleared — the one lifecycle path that did not exist before 2026-08-29),
:func:`rearm_incidents_on_startup`, and :func:`clear_on_reinsert` (from the
``ams_presence`` presence-GAIN edge). ``clear_hms_errors()`` is NEVER called — the
resume clears the firmware dialog itself and clearing would corrupt main.py's HMS
dedup/grace bookkeeping. From the routes: :func:`resume_paused_print` — THE resume of a
paused print (K9, 2026-10-10), which refills an EMPTY toolhead first (Raymond 2026-10-10:
"when i click resume there MUST be filament loaded"); every refill runs as a registered
driver of an open row (:func:`_refill_episode`, K10), and whether one is owed is
:func:`refill_verdict.refill_owed`'s alone (K7).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
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
    KIND_TOOLHEAD_REFILL,
    REFILL_KINDS,
    RESOLVE_AUTO_RESUME,
    RESOLVE_DRIVER_RESTART,
    RESOLVE_DRIVER_SELF_HEAL,
    RESOLVE_DRIVER_SWAP,
    RESOLVE_OPERATOR,
    RESOLVE_PAUSED_ELSEWHERE,
    RESOLVE_REFILL_RESUMED,
    STATUS_ABORTED,
    STATUS_ESCALATED,
    STATUS_RECOVERING,
    STATUS_RESOLVED,
    PrinterIncident,
)
from backend.app.models.printer_incident_step import STEP_KIND_COMMAND, STEP_KIND_LEVER, PrinterIncidentStep
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.schemas.printer import ResumeRefusalReason
from backend.app.services import (
    ams_command,
    farm_correlation,
    incident_resolution,
    live_reading,
    printer_incidents,
    refill_verdict,
    spool_respool,
    tray_fields,
)
from backend.app.services.bambu_mqtt import AMS_STATUS_IDLE, SentCommand, ams_mid_filament_change
from backend.app.services.hms_errors import (
    ACTIONABLE_CLASSES,
    AmsFaultClass,
    FaultCandidate,
    candidate_fingerprint,
    classify_full_code,
    current_runout_demand,
    fault_tokens,
    fingerprint_tokens,
    live_candidates,
    live_notices,
    power_loss_prompt_standing,
    short_code_ambiguous,
    slot_runout_full_codes,
)
from backend.app.services.incident_resolution import (
    _JOB_OVER_STATES,
    _REPAIR_EVIDENCE_LOAD,
    Context,
    TerminalEvent,
    driver_owns,
    ledger,
)
from backend.app.services.job_identity import same_job
from backend.app.services.plate_occupancy import DepositEvidence
from backend.app.services.printer_incidents import (
    FAULT_RESTART_STEP,
    WAITING_REASON_RECOVERING,
    runout_slot_desc,
    waiting_reason_for,
)
from backend.app.services.printer_manager import printer_manager
from backend.app.services.refill_verdict import RefillReason, RefillTrigger
from backend.app.services.spool_respool import decode_global_tray, encode_global_tray

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.app.services.bambu_mqtt import BambuMQTTClient, JobPeaks, PrinterState

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
# WIDENED 2026-08-09 (operator-ratified partition): the swap machine triggers on
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


def _recorded_words(candidates: frozenset[FaultCandidate]) -> list[str]:
    """The printer's own words for the faults an incident speaks for, as its row records them.

    Recorded when the row opens or is re-classified: a release lever, a stop or the next
    job clears them off the printer while the hold they explain still stands. The
    candidates' OWN full codes, never a short-code round trip: ``0700_0001`` would sweep a
    runout demand standing beside an owned overload into the incident's words."""
    return sorted({c.full_code for c in candidates} - {""})


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

# The LIVENESS bound of the wait on the farm's own pending command (:func:`_await_pending_command`,
# K6): past it — measured from the command's durable send stamp — the driver sends ONE page
# naming the queued command, and keeps waiting. It decides only WHEN a human hears; it decides
# nothing about filament, the row or the driver (operator ruling 2026-10-10: "time is not the
# right signal"). Set above the measured self-run of an accepted pull-back: 274 s (011-H2S
# 2026-10-09 04:31:27 → 04:36:01) and 280 s (014-H2S 2026-10-10 02:45:58 → 02:50:38), so the
# measured case runs before anybody is paged.
_HELD_COMMAND_PAGE_S = 600.0

# Refill auto-resume timing (code constants, NOT operator knobs — precedent
# ``ams_command.UNLOAD_GRACE_S``). The AMS needs to register the freshly-inserted
# filament before a resume can land: a resume published on the presence edge itself
# races the firmware's own tray-state settle and is rejected. 15 s mirrors the
# operator's proven manual gap (``ams_command.UNLOAD_GRACE_S``) — long enough to settle,
# short enough that the operator is still standing at the printer.
_RUNOUT_RESUME_SETTLE_S = 15.0
# Bound on how long we wait for RUNNING after the resume before standing aside.
_RUNOUT_RESUME_CONFIRM_S = 30.0

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
    # The refill episode's three (K10, 2026-10-10 — :func:`_refill_then_resume`). Each page also
    # carries the MEASURED toolhead and what the refill attempted and the AMS answered
    # (:func:`_feeder_clause`, for every kind: :data:`_TOOLHEAD_REASONS`).
    "toolhead_refill_failed": "The farm could not refill the empty toolhead.",
    "toolhead_refill_withheld": (
        "The toolhead reads empty and the farm did not refill it: maintenance mode, a queued farm command, "
        "or no live reading."
    ),
    "toolhead_resume_unconfirmed": "The printer did not report RUNNING after the farm's resume. Resume on the printer.",
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
    # A swap command was acknowledged and HELD behind a filament change the AMS went back into
    # before it ran — after a lever read ``released``, or after a round top read the AMS out of
    # the change. Inside a change only (2026-10-10): a command held OUTSIDE one is the driver's
    # to wait for (``_held_outcome``), never this give-up. The evidence sentence names the ONE
    # held command and what it does at the next release — that is its one place.
    "swap_held_after_release": (
        "The AMS went back into a filament change and holds the farm's swap command behind it. Free the "
        "spool, then press Retry on the printer."
    ),
    # A release verb with an unmeasured effect ENDED the print inside its own read window
    # (the reader's ``ended``). The terminal's own disposition handles the unit.
    "wedge_ended_print": "A release verb ended the print.",
    # The restart rung's give-ups (:func:`_restart`, operator ruling 2026-09-29). The farm
    # STOPPED a job that had deposited nothing, so there is no print to resume and the copy
    # says so: the job's state, the path's, the plate's, the unit's, and the one physical
    # exit — the row is held as a PHYSICAL fault from here (:func:`_give_up`), which a
    # completed load, a print running through the path, or Recover ends. No feeder clause
    # (:data:`_JOB_ENDED_REASONS`): its "then resume on the printer" names a print that is over.
    "restart_unload_failed": (
        "Feed stall at layer 0: the farm stopped the job, and the unload of the stalled feeder did not "
        "complete, so filament may still be in the path. The plate is clear and the unit is back in the queue. "
        "Free the filament path, then load a slot on the printer or press Recover."
    ),
    "restart_no_candidate": (
        "Feed stall at layer 0: the farm stopped the job and unloaded the stalled feeder; no other loaded spool "
        "matches the job's filament. The path is empty, the plate is clear and the unit is back in the queue. "
        "Load a matching spool on the printer or press Recover."
    ),
    # The bound tripped — this plate was already restarted once, or this printer's path
    # stalled again with no print completed since its last restart (013-H2S 2026-09-28:
    # slot 4 stalled in the same tube segment slot 3 had). The path is the suspect, so no
    # spool is parked.
    "restart_refaulted": (
        "Feed stall at layer 0 after a restart: the farm stopped the job and unloaded the stalled feeder, and "
        "parked no spool — the filament path is the suspect, not the spool. The path is empty, the plate is "
        "clear and the unit is back in the queue. Inspect the PTFE path and buffer, then load a slot on the "
        "printer or press Recover."
    ),
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
    # The two classes that reach escalation without ever entering the loop.
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

# ``"print_stop"`` is ``printer_incidents.FAULT_RESTART_STEP`` — the table is keyed by that
# constant, and a Literal can only spell a literal; ``test_spool_recovery`` pins the two equal.
Lever = Literal[
    "resume",
    "resume_then_pause",
    "ams_control_resume",
    "clean_print_error",
    "retry_after_clear",
    "ams_control_abort",
    "ams_control_reset",
    "ams_control_pause",
    "print_stop",
]


@dataclass(frozen=True)
class LeverSpec:
    """One rung of the release ladder: the frame it publishes, whether its read pauses the
    print on the first RUNNING sample with NOTHING at the feeder (the firmware's retract),
    the page's one-line name of it, how many times it may be pulled, when it may be pulled
    at all, and whether its verb ends the job.

    ``publish`` is the wire verb and nothing else — True when the client sent the frame.
    What the wire answered is :func:`_read_after`'s alone, for every verb.

    ``pulls`` — the rung's budget per INCIDENT, pulled back to back before the ladder moves
    on, and spent from the durable step log (:meth:`_RecoveryEvidence.lever_spent`): a
    restarted driver resumes mid-budget, never re-grinding the stalled feeder from the
    first pull. One pull is the rule; the Retry after the dialog is cleared
    (``retry_after_clear``) takes two — why is that rung's own entry in the table's notes.
    Spent is a STEP on the incident's log, never a process-lifetime counter — the counters
    that preceded it were keyed by (printer, job), so 003-H2S 2026-09-19 04:31 met a second
    fault 69 s after a self-heal on the same job and gave up with ZERO CONTINUEs sent — and
    never an in-memory list either, which a restart emptied. A new incident is a new log.

    ``pull_rule`` — asked when the ladder REACHES the rung: ``None`` pulls it, a sentence
    says why not, and a rung it refuses is skipped, NOT spent (:func:`_release_ladder`).

    ``ends_job`` — the verb ends the print by design (the restart rung), so a terminal
    inside its window is the rung's release (``stopped``), never ``ended`` and never the
    operator's cancel echo, which H2S sends for the farm's own remote stop
    (:func:`_terminal_read`).
    """

    publish: Callable[[BambuMQTTClient], bool]
    pause_on_empty_path: bool
    text: str
    pulls: int
    pull_rule: Callable[[RecoveryIncident], str | None]
    ends_job: bool = False


def _pull_always(_incident: RecoveryIncident) -> str | None:
    """The pull rule of every release verb: pulled whenever the ladder reaches it."""
    return None


def _restart_refusal(incident: RecoveryIncident) -> str | None:
    """The restart rung's pull rule (operator ruling 2026-09-29): a FARM unit, and a job
    that has deposited NOTHING. ``None`` pulls the rung; a sentence says why it is skipped.

    A foreign print has no unit to hand back to the queue (``item_id`` None). What is on
    the plate is ``plate_occupancy.DepositEvidence``'s one predicate over the client's one
    peaks reader (``BambuMQTTClient.job_peaks``), read LIVE: this driver compares no layer
    number of its own. The predicate fails closed — peaks that are not a measurement (a
    client that attached mid-job) read as deposited, and so does no client at all — so a
    stall past the first layer, or one nobody can vouch for, still exhausts the ladder to
    ``wedge_unreleased``: stopping there destroys a part and leaves one on the plate.
    """
    if incident.item_id is None:
        return "a foreign print — no farm unit to hand back to the queue"
    client = printer_manager.get_client(incident.printer_id)
    if client is None:
        return "no client to read the job's peaks from"
    if DepositEvidence.live(client.job_peaks()).deposited:
        return "the job has deposited, or its peaks are not a measurement"
    return None


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
#   ``err`` + ``param: "reserve"`` + ``job_id``: since 2026-09-29 ``bambu_mqtt.execute_hms_action``
#   publishes those for the dialog's "Problem solved, resume" / "Ignore" buttons (the modal,
#   and the plate-check episode's press in ``pause_recovery``), and no lever here sends
#   either — a feed fault's dialog offers neither button. Freed
#   009-H2S 2026-07-20 (an EMPTY path) — the one measured remote release, so it stays
#   first; on a LOADED stalled feeder it re-runs the print's own change and re-holds
#   (002-H2S 2026-09-11; 012-H2S 2026-09-23, re-PAUSEd in 47 s; 012-H2S 2026-09-25,
#   incidents 341/343, read ``wedged`` twice).
# * ``resume_then_pause`` — ``print.resume``, then ``print.pause`` on the first RUNNING
#   sample whose feeder reads EMPTY (:func:`_feeding_position` kind ``empty``) — the July
#   2026-07-20 "workable PAUSE", a pause that lands outside the change with nothing
#   loaded. On an EMPTY-path wedge that is the FIRST RUNNING sample (009, unchanged). On
#   a LOADED stall it is the firmware's own retract of the jammed filament: 012-H2S
#   2026-09-25 pulled slot 3 back and re-fed the same slot after every resume, the wire
#   reading ``tray_now`` 2 → 255 → 2 every ~12 s (13:28:34 → 13:29:22), while a pause on
#   the first RUNNING sample (13:32:06, ``tray_now=2``) landed LOADED and read wedged.
#   Second in the pull order: it is the only lever whose pause is timed to a posture the
#   swap can go out from. Dual-nozzle: the feeder is the ACTIVE extruder's own reading
#   (``tray_fields.toolhead_feed``, K1), so an empty active nozzle pauses at once whatever
#   the deputy holds; a retract the per-extruder map never reports degrades to the ``hung``
#   arm at the deadline.
# * ``ams_control_resume`` — ``ams_control("resume")``: the error dialog's CONTINUE for
#   ``07008006`` / ``07008005``, its RETRY, and the extruder-switch panel's "Retry".
#   012-H2S 2026-09-23: echo SUCCESS, re-held in 87 s.
# * ``clean_print_error`` — the dialog's OK / close ("clears whatever error dialog is
#   currently active"). The FRAME only (``BambuMQTTClient.clean_print_error``): a local
#   HMS wipe would fake the quiet reading this ladder is judged by. Unmeasured.
# * ``retry_after_clear`` — ``ams_control("resume")`` again, now with the error dialog
#   cleared, pulled TWICE back to back (:attr:`LeverSpec.pulls`): the order 011-H2S
#   2026-09-29 (incident 419) released in — the dialog's Retry (``ams_control_resume``)
#   re-held, the dialog was cleared (``clean_print_error``), and the next Retry fed slot 3.
#   That Retry was the operator's, at the touchscreen; whether they pressed Resume or Retry
#   is indistinguishable in the logs, and both re-run the same feed of slot 3 — the farm's
#   bare ``print.resume`` had already re-held twice (the first two rungs), so the repeated
#   verb is the dialog's Retry. The vendor catalog offers exactly that button for the codes
#   this ladder meets: ``backend/app/data/hms_actions.json`` maps ``07008006`` and
#   ``07008010`` to CONTINUE (with CHECK_ASSISTANT), and CONTINUE is ``ams_control("resume")``.
#   Two pulls, because 011 needed one after the clear and the second bounds the grinding at
#   one more step window. It sits right after ``clean_print_error``, before the verbs with no
#   vendor call site.
# * ``ams_control_abort`` — the extruder-switch panel's "Quit"
#   (``ExtruderSwithingStatus::on_quit``); the catalog maps it for ``07008036`` only, and
#   its effect on a paused print is undocumented. Unmeasured.
# * ``ams_control_reset`` — a valid firmware param (the ``command_ams_control``
#   allowlist) with no vendor UI call site. The fork's former claim that it "ends the
#   change the paused print is waiting on" was unsourced and is retracted. Unmeasured.
# * ``ams_control_pause`` — a valid firmware param with no vendor UI call site. Unmeasured.
# * ``print_stop`` (:data:`printer_incidents.FAULT_RESTART_STEP`) — ``print.stop``
#   (``BambuMQTTClient.stop_print``, the raw stop the eject lane ends its own sweep with).
#   The LAST rung, and the only one whose verb ends the job: the job terminal is the one
#   measured release of a loaded stall (013-H2S 2026-09-28, incidents 410/411: all seven
#   verbs wedged at layer 0, ``1/5``, ``tray_now=2``, and the job sat ~19 h having printed
#   nothing). Pulled only for a FARM unit that has deposited NOTHING
#   (:func:`_restart_refusal`); its terminal is read ``stopped`` and continues into
#   :func:`_restart` — unload the stalled feeder, park its spool, and let the unit's
#   terminal hand it back to the queue for the backup slot.
#
# ``ams_control("done")`` is deliberately NOT a lever: it is the manual-feed dialog's
# "Filament extruded", a claim that a feed succeeded, and the runout-class hold it
# produces refuses cross-slot loads (2026-07-19, 10 of 10). The job terminal — the one
# measured release — is the driver's to cause ONLY through ``print_stop``, on a job that
# has deposited nothing (operator ruling 2026-09-29, 013-H2S incidents 410/411): a stop
# past the first layer destroys a part and leaves it on the plate, so a mid-print stall
# still exhausts the ladder to ``wedge_unreleased``, and a terminal any OTHER verb
# produces is still read ``ended`` — a release verb is not supposed to end a print.
_LEVERS: dict[Lever, LeverSpec] = {
    "resume": LeverSpec(
        publish=lambda client: client.resume_print(),
        pause_on_empty_path=False,
        text="resume",
        pulls=1,
        pull_rule=_pull_always,
    ),
    "resume_then_pause": LeverSpec(
        publish=lambda client: client.resume_print(),
        pause_on_empty_path=True,
        text="resume then pause",
        pulls=1,
        pull_rule=_pull_always,
    ),
    "ams_control_resume": LeverSpec(
        publish=lambda client: client.ams_control("resume"),
        pause_on_empty_path=False,
        text="ams_control resume",
        pulls=1,
        pull_rule=_pull_always,
    ),
    "clean_print_error": LeverSpec(
        publish=lambda client: client.clean_print_error(),
        pause_on_empty_path=False,
        text="clean_print_error",
        pulls=1,
        pull_rule=_pull_always,
    ),
    "retry_after_clear": LeverSpec(
        publish=lambda client: client.ams_control("resume"),
        pause_on_empty_path=False,
        text="retry after clear",
        pulls=2,
        pull_rule=_pull_always,
    ),
    "ams_control_abort": LeverSpec(
        publish=lambda client: client.ams_control("abort"),
        pause_on_empty_path=False,
        text="ams_control abort",
        pulls=1,
        pull_rule=_pull_always,
    ),
    "ams_control_reset": LeverSpec(
        publish=lambda client: client.ams_control("reset"),
        pause_on_empty_path=False,
        text="ams_control reset",
        pulls=1,
        pull_rule=_pull_always,
    ),
    "ams_control_pause": LeverSpec(
        publish=lambda client: client.ams_control("pause"),
        pause_on_empty_path=False,
        text="ams_control pause",
        pulls=1,
        pull_rule=_pull_always,
    ),
    FAULT_RESTART_STEP: LeverSpec(
        publish=lambda client: client.stop_print(),
        pause_on_empty_path=False,
        text="print stop",
        pulls=1,
        pull_rule=_restart_refusal,
        ends_job=True,
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
#   that the operator's Stop did not produce; ``stopped`` — the terminal of a rung whose
#   verb ends the job (:attr:`LeverSpec.ends_job`, the restart rung): its release;
#   ``no_pause`` — the reader's own pause never landed; ``not_sent`` — the client did not
#   publish the verb; ``abort`` / ``handover`` — :func:`_takeover` (an operator Stop's
#   terminal is ``abort``).
LeverReading = Literal[
    "self_healed", "swapped", "wedged", "released", "ended", "stopped", "no_pause", "not_sent", "abort", "handover"
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
        """An unsent step: the log's ``note`` assigns its ``seq`` and ``at``."""
        return cls(seq=0, lever=lever, outcome=None, moved=None, at=None)

    def entry(self) -> printer_incidents.StepEntry:
        """The ledger row this step is written as: a ``lever`` naming the lever."""
        return printer_incidents.StepEntry(kind=STEP_KIND_LEVER, name=self.lever)

    def sent(self, seq: int, at: datetime) -> LeverStep:
        return replace(self, seq=seq, at=at)

    def answered(self, outcome: str) -> LeverStep:
        """The reader's verdict. A token that is no :data:`LeverReading` RAISES."""
        if not _is_reading(outcome):
            raise LookupError(f"spool_recovery: lever step {self.seq} answered with {outcome!r}")
        return replace(self, outcome=outcome)


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
        """An unsent step: the log's ``note`` assigns its ``seq`` and ``at``."""
        return cls(seq=0, command=command, target=target, feeder=feeder, answer=None, at=None)

    def entry(self) -> printer_incidents.StepEntry:
        """The ledger row this step is written as: a ``command`` with its tray and feeder."""
        return printer_incidents.StepEntry(
            kind=STEP_KIND_COMMAND, name=self.command, target=self.target, feeder=self.feeder
        )

    def sent(self, seq: int, at: datetime) -> CommandStep:
        return replace(self, seq=seq, at=at)

    def answered(self, outcome: str) -> CommandStep:
        """The classifier's answer. A token that is no ``ams_command.Answer`` RAISES."""
        if not _is_answer(outcome):
            raise LookupError(f"spool_recovery: command step {self.seq} answered with {outcome!r}")
        return replace(self, answer=outcome)


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


class _RecoveryEvidence(printer_incidents.EvidenceLog[LeverStep | CommandStep]):
    """THE evidence log of one AMS incident: every lever pulled and every AMS command
    sent, in send order, with what the wire answered — the RECORDS the escalation reason,
    the operator page and a restarted driver's next lever are derived from.

    The store's :class:`~backend.app.services.printer_incidents.EvidenceLog` (its write
    discipline: noted at the send, answered at the read, built only by ``from_row``, the
    ledger's one writer) bound to THIS driver's vocabulary — :class:`LeverStep` and
    :class:`CommandStep` — plus every reading this driver derives from its log. Durable,
    because on a feeder-stall wedge every release verb's consequence is identical
    (012-H2S 2026-09-23), so a driver restarted from the wire alone re-grinds the stall
    from the first lever. Every counter is DERIVED from ``steps``; nothing is counted
    twice.

    The 009-H2S incident reported ``no_eligible_spool`` after four failed loads — a lie
    that sent the operator looking for spools instead of at the feed path. The reason is
    read off this log, never off a position in the code.
    """

    @classmethod
    def _step_of(cls, row: PrinterIncidentStep) -> LeverStep | CommandStep:
        """Hydrate ONE ledger row. A token this module's vocabulary cannot name RAISES — a
        ledger that says something the driver cannot read is drift, never a skipped row."""
        outcome = row.outcome
        if row.kind == STEP_KIND_LEVER:
            if not _is_lever(row.name):
                raise LookupError(
                    f"spool_recovery: incident {row.incident_id} step {row.seq} names no lever: {row.name!r}"
                )
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

    async def answer(self, seq: int, outcome: str, *, moved: bool | None = None) -> None:
        """Fill step ``seq``'s outcome — the reader's verdict for a lever, the classifier's
        answer for a command — through the store's log, then keep a lever's ``moved``: the
        read's own, in memory only (the ledger keeps the verdict, not the motion)."""
        await super().answer(seq, outcome)
        if moved is None:
            return
        for index, step in enumerate(self.steps):
            if step.seq == seq and isinstance(step, LeverStep):
                self.steps[index] = replace(step, moved=moved)

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
        """Distinct trays a load went out for. The give-up's refill load is sent after the
        reason is chosen, so it never feeds one."""
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
    def held_command(self) -> CommandStep | None:
        """The LAST command the firmware acknowledged and held behind the print's change
        (``held``) — it runs at the next release, so nothing may be sent behind it."""
        return next((s for s in reversed(self.command_steps) if s.answer == "held"), None)

    @property
    def pending_command(self) -> CommandStep | None:
        """The farm's motion command that has NOT RUN — the log's LAST command step while it
        pends (``printer_incidents.command_pends``: answered ``held``, or sent and never read).
        The same one rule the open-row projection derives ``printer_incidents.PendingCommand``
        by, over the same ledger rows. Nothing is sent behind it: a second command behind a
        queued one runs too (012-H2S 2026-09-23), and a queued pull-back runs on its own
        (011/014-H2S 2026-10-09/10) — the driver waits for it (:func:`_await_pending_command`)."""
        steps = self.command_steps
        if steps and printer_incidents.command_pends(steps[-1].answer):
            return steps[-1]
        return None

    def command_step(self, seq: int) -> CommandStep:
        """The command step ``seq`` as the log holds it now. A seq the log does not hold as a
        command RAISES: a driver that lost its own step is drift, never a skipped read."""
        step = next((s for s in self.command_steps if s.seq == seq), None)
        if step is None:
            raise LookupError(f"spool_recovery: incident {self.incident_id} has no command step {seq}")
        return step

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
        """Has ``lever`` been pulled its own budget (:attr:`LeverSpec.pulls`) on this
        incident? A step counts from its SEND: a lever sent and never read (a crash between
        the two) is spent, so a restarted driver resumes mid-budget."""
        return sum(1 for s in self.lever_steps if s.lever == lever) >= _lever(lever).pulls

    @property
    def restart_stop(self) -> LeverStep | None:
        """The restart rung's step (:data:`printer_incidents.FAULT_RESTART_STEP`) once it was
        SENT — from here the job is over by the farm's own act, and the incident's work is
        the restart continuation (:func:`_restart`)."""
        return next((s for s in self.lever_steps if s.lever == FAULT_RESTART_STEP), None)

    @property
    def restart_unloaded(self) -> bool:
        """Did an unload sent AFTER the restart stop answer ``complete``? The continuation's
        first half, read off the log so a re-entered driver does not send it again."""
        stop = self.restart_stop
        return stop is not None and any(
            s.command == "unload" and s.answer == "complete" and s.seq > stop.seq for s in self.command_steps
        )

    @property
    def committed_before_restart(self) -> bool:
        """Had this incident crossed the swap-commit boundary (:attr:`swap_committed`)
        BEFORE the restart stop went out? Then its jammed spool was already parked there,
        and the continuation's own unload is not a second crossing."""
        stop = self.restart_stop
        return stop is not None and any(s.seq < stop.seq for s in self.command_steps)

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
    # one place that rule lives. None when no mechanical-feed code is in the candidate set
    # it was built from — a startup re-entry whose wire and recorded codes name none — and
    # then nothing is parked either: which side faulted is unknown, and a missed stamp heals
    # forward while a false one is permanent (invariant 11).
    extruder_side_only: bool | None
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
        extruder_side_only=all(c.extruder_side for c in mechanical) if mechanical else None,
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
# What remains in memory is ONLY what is cheap to rebuild and harmless
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
# (gcode_state, demanded slot or None, live actionable short codes, connection_epoch).
# Edges over this sample drive the refill auto-resume and the "the printer is running
# again" close. The epoch rides along because a sample from a PREVIOUS MQTT session
# cannot be compared for a code DISAPPEARING — a reboot wipes the standing HMS list, so
# every negative edge fires at once (2026-09-04, printer 8). The completed-load edge — a
# ``physical`` hold's repair evidence — is the motion ledger's, over its OWN seed
# (``incident_resolution.MotionLedger``), so no feeder rides here.
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
# Losing it to a restart re-arms exactly once, which is the intent: a fault
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

# --- Durable repeat-jam quarantine (code constants, NOT operator knobs) -------
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
        "restart_unload_failed",  # the job was stopped and the AMS did not unload the stalled feeder
        "restart_refaulted",  # the path stalled again at layer 0 after a restart — the path, not the spool
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
        # inventory, as ``no_eligible_spool``: the path unloaded cleanly and nothing matched
        "restart_no_candidate",
        # The refill episode's (K10): the farm's reaction to an EMPTY toolhead whatever emptied it
        # (a queued pull-back, a screen unload, a resume onto air) — not by itself a buffer /
        # feeder diagnosis; a jam its load raises opens its own incident, which counts.
        "toolhead_refill_failed",
        "toolhead_refill_withheld",
        "toolhead_resume_unconfirmed",
    }
)

# The escalation reasons of the refill episode — whatever the row's kind (an AMS row the refill
# re-entered, or its own ``toolhead_refill`` row), their page states the measured toolhead and the
# refill (:func:`_compose_detail`).
_TOOLHEAD_REASONS: frozenset[str] = frozenset(
    {"toolhead_refill_failed", "toolhead_refill_withheld", "toolhead_resume_unconfirmed"}
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
    :func:`owned_full_codes` so a suppressed alert and a refused entry can never disagree:

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
        survivors = {fp for fp in fingerprints if fingerprint_tokens(fp) <= live_tokens}
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
    each refill and at candidate-loop end, carrying the live telemetry that explains WHY
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
    """The representative candidate of the deciding class: an AMS fault before a holder
    fault, then an UNAMBIGUOUS short code (``hms_errors.short_code_ambiguous``), then the
    lowest — so the code the operator is told is stable across pushes and names ONE fault.
    011-H2S 2026-09-29: the slot overload renders ``0700_0001``, which is also that slot's
    runout demand, so beside ``0700_8010`` the 8010 names the incident."""
    members = sorted(
        (c for c in candidates if c.fault_class is fault_class),
        key=lambda c: (c.external, short_code_ambiguous(c.short_code), c.short_code),
    )
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
       ``last_loaded_tray`` still holds. An IDENTITY reading ("which tray"), read through
       ``tray_fields.valid_feeder`` — allowlisted in
       ``test_code_quality.TestToolheadFeedOwnership``.
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


def _fed_trays(feed: tray_fields.ToolheadFeed) -> list[int]:
    """Every extruder's AMS feeder, in extruder-id order, repeats collapsed — the UNION.

    Read off the one toolhead reader (``tray_fields.toolhead_feed``, K1): per extruder on a
    dual-nozzle machine — its ``h2d_extruder_snow`` map, which the client fills ONLY there —
    and the one ``tray_now`` reading on a single nozzle. ``state.tray_now`` alone describes
    only whichever hotend is active on an H2C/H2D, so a slot feeding the OTHER nozzle read
    as "not feeding" there; the per-extruder map is the honest answer, and no client lookup
    gates it any more (the map's presence IS the topology the state describes).

    The union is the answer to ONE identity question — :func:`slot_was_feeding`'s "was this
    slot feeding" at a presence-loss edge (a slot feeding EITHER nozzle was feeding) — never to
    "is the print fed": that is the ACTIVE extruder's alone (:func:`_feeding_position`),
    because an empty active nozzle beside a loaded one prints air. The external holder and an
    empty or unread extruder contribute nothing. Pure and total.
    """
    return list(dict.fromkeys(reading.tray for _extruder, reading in feed.extruders if reading.tray is not None))


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
    (:func:`_fed_trays`) and treats this as the fallback tier.

    ``tray_now`` here is an IDENTITY reading ("which tray"), read through
    ``tray_fields.valid_feeder`` — allowlisted in ``test_code_quality.TestToolheadFeedOwnership``.
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

    Two witnesses, in order:

    * every extruder's own feeder (:func:`_fed_trays` over ``tray_fields.toolhead_feed`` —
      per extruder on a dual nozzle, the per-extruder map), asked FIRST because a positive
      there is a direct wire statement about a specific hotend, while everything
      ``_feeder_before_edge`` composes is a single-value approximation of a two-value fact.
      On a single nozzle it is the one live ``tray_now`` reading, which can only ADD a match
      — a tray feeding now was feeding — and never take one away, so
      ``_feeder_before_edge``'s "``tray_now`` LAST" order still governs every negative;
    * :func:`_feeder_before_edge`. ``printer_id`` unlocks its SLICER-mapping witness
      (``_job_feeders`` → ``_slicer_mapping``), which the client captures off the request
      topic — missing before 2026-08-20, so the contracted order "``last_loaded_tray`` +
      mapping AHEAD of ``tray_now``" only had its mapping tier for callers that passed an
      ``item``.

    Never raises: an unreadable state answers False (not evidence of a feed).
    """
    try:
        for feeder in _fed_trays(tray_fields.toolhead_feed(state)):
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

# What the refill step did (:func:`_refill_toolhead`, K8 2026-10-10), stated by the driver and
# never inferred from the wire: the page says what was ATTEMPTED and what the AMS answered, and
# the toolhead's state is read separately, live (:func:`_compose_detail`). Five jam-kind reasons
# reach :func:`_escalate` straight from the entry gate with nothing attempted — those carry no
# refill at all (``None``), and the page states only the measured toolhead.


@dataclass(frozen=True)
class RefillLoaded:
    """The refill's load reached the toolhead: ``tray`` answered ``complete`` AND the ACTIVE
    extruder reads fed from it (``tray_fields.toolhead_feed``)."""

    tray: int


@dataclass(frozen=True)
class RefillFailed:
    """The refill of ``tray`` did not reach the toolhead. ``command`` is the step that failed —
    the unload before the load, or the load — and ``answer`` what the AMS answered it: an
    ``ams_command.Answer``, the step's own ``refused`` / ``drying``, or ``not_at_toolhead`` (the
    load answered ``complete`` and the ACTIVE extruder does not read it). ``tray`` is ``None``
    only when the printer left its session before a spool was picked."""

    tray: int | None
    command: ams_command.Command
    answer: str


@dataclass(frozen=True)
class RefillNoCandidate:
    """No spool to load: the job's last feeder is absent or spent, and the swap's own selection
    found nothing either."""


# What the page states about a refill (:func:`_feeder_clause`).
RefillResult = RefillLoaded | RefillFailed | RefillNoCandidate
# What :func:`_refill_toolhead` returns: its result, or a takeover (:func:`_takeover`) — closed,
# dispatched by ``match`` + ``assert_never`` at its one caller.
RefillVerdict = RefillResult | StepTakeover


@dataclass(frozen=True)
class FeederPosition:
    """Where the filament sits right now, read RELATIVE to this incident's jammed tray.

    ONE vocabulary, ONE classifier (:func:`_position_of`, over one extruder's
    ``tray_fields.ExtruderFeed``), ONE reading: :func:`_feeding_position` — what the PRINT is
    fed from, the ACTIVE extruder (K1, 2026-10-10). Every verdict about the print or a command
    reads it: THE reader's (:func:`_read_after` — self-heal, swap, the empty-path pause, the
    swapped tray), the feeder kind a command step records at its send (:func:`_feeder_kind` —
    what the command acts on, which ``ams_command.posture`` keys the answer by and
    ``refill_verdict.ran`` reads back), and the operator's resume on the jammed feeder
    (:func:`_clear_oor_if_resumed_on_jammed_feeder`). On a dual nozzle an empty active hotend
    beside a loaded deputy prints air, so no either-extruder reading of it exists.

    What the OPERATOR PAGE states and what the give-up's refill decides are the active
    extruder's too (``tray_fields.toolhead_feed`` read directly).

    ``kind``:

    * ``jammed``  — the jammed tray is still the feeder (nothing was unloaded, or a
      refill put it back);
    * ``empty``   — the 255 sentinel: NOTHING is feeding. After a feed fault that is
      never "the path is clear" (invariant 8);
    * ``other``   — a different regular tray feeds (a replacement that loaded, or an
      AMS-HT id that has no letter+slot name) — or a fed extruder on an incident that names no
      jammed tray (a ``toolhead_refill`` row whose job had no last feeder): the tray it prints
      from is known, so the command it acts on has a LOADED entry;
    * ``external``— the external spool holder (254): no AMS slot is involved;
    * ``unknown`` — no live state, or an unread feed.

    ``global_tray`` is the slot the operator must be TOLD about — the jammed tray for
    ``jammed``/``empty`` (the one still loaded, or the one that was unloaded), the tray
    now at the feeder for ``other``, and None where there is nothing to name (which is
    why no clause can ever render "tray None").
    """

    kind: FeederKind
    global_tray: int | None


def _position_of(reading: tray_fields.ExtruderFeed, jammed_global_tray: int | None) -> FeederPosition:
    """ONE extruder's reading, classified relative to the jammed tray — the one classifier of
    :class:`FeederPosition`. Fed from the jammed tray → ``jammed``; from any other (none named)
    → ``other``; the rest is the reading's own kind. Pure and total."""
    match reading.kind:
        case "fed":
            kind: FeederKind = "jammed" if reading.tray == jammed_global_tray else "other"
            return FeederPosition(kind, reading.tray)
        case "empty":
            return FeederPosition("empty", jammed_global_tray)
        case "external":
            return FeederPosition("external", None)
        case "unknown":
            return FeederPosition("unknown", None)
        case _:
            assert_never(reading.kind)


def _feeding_position(state, jammed_global_tray: int | None) -> FeederPosition:
    """What the PRINT is fed from — the ACTIVE extruder (``tray_fields.toolhead_feed``, K1),
    relative to the jammed tray. Never raises; no state reads ``unknown``.

    THE reading for every "is the print feeding / self-healed / swapped" verdict and for the
    feeder a command acts on (:class:`FeederPosition`): on an H2C/H2D an empty ACTIVE nozzle
    beside a deputy still holding the jammed tray prints air, and an either-extruder reading
    would call that print ``jammed`` — a self-heal over air. On a dual nozzle the reading is
    the per-extruder map's, never ``tray_now`` (the client's guess onto a unit); a map that
    does not describe the active hotend reads ``unknown``.
    """
    return _position_of(tray_fields.toolhead_feed(state).active, jammed_global_tray)


# The reasons whose feeder clause names the slot and gives NO instruction: the AMS holds
# a change (the print's own, or one it re-entered holding the swap's command), so the
# copy's own last sentence (Retry) is the one action and a "then resume" beside it would
# contradict it — the ``_retract_clause`` lesson.
_CLAUSE_WITHOUT_INSTRUCTION: frozenset[str] = frozenset({"wedge_unreleased", "swap_held_after_release"})
# The reasons that are only ever reached after the job ENDED: a release verb ended the print
# (``wedge_ended_print``), or the restart rung's stop did (the three restart give-ups). The
# other half of "the job is over" is the step ledger's — any give-up once the restart stop
# is on it (:func:`_job_ended`).
_JOB_ENDED_REASONS: frozenset[str] = frozenset(
    {"wedge_ended_print", "restart_unload_failed", "restart_no_candidate", "restart_refaulted"}
)

# A reason reachable on EITHER side of the job's end has a second copy for the ended side,
# because its paused copy's instruction ("resume on the printer") names a print that is
# over. ``printer_offline`` is the one today: the offline bound can be met inside the
# restart continuation, after the driver's own stop.
_ENDED_JOB_DETAIL: dict[str, str] = {
    "printer_offline": (
        "The printer stopped reporting after the farm stopped the job at layer 0, so the farm could not read "
        "what the AMS did. The plate is clear and the unit is back in the queue. Check the printer's connection "
        "and the filament path, then load a slot on the printer or press Recover."
    ),
}


def _job_ended(reason: str, evidence: _RecoveryEvidence | None) -> bool:
    """Is this page composed after the job ENDED? DERIVED, from the reason and the step
    ledger: a reason that is only reached after the end (:data:`_JOB_ENDED_REASONS`), or any
    give-up once the restart stop is on the ledger (the printer_offline bound met inside the
    continuation). Such a page says neither "left PAUSED" nor "resume on the printer": it
    carries no feeder clause (the filament's place is the copy's own, and the next job's
    question), its reason copy is the ended-side one where there are two
    (:data:`_ENDED_JOB_DETAIL`), and the notification wrapper is told so
    (``on_spool_recovery_failed(job_ended=True)``)."""
    return reason in _JOB_ENDED_REASONS or (evidence is not None and evidence.restart_stop is not None)


# Reasons whose paused copy instructs a RESUME (or Continue) — right over a loaded toolhead,
# printing air over an empty one (011/014-H2S 2026-10-09/10: "slot N is loaded … press
# Continue" over a toolhead the queued pull-back had emptied). Each has a second copy for the
# EMPTY side, its instruction dropped: the feeder clause then carries the exit, which loads a
# spool BEFORE the resume (:func:`_feeder_clause`). The ``_ENDED_JOB_DETAIL`` precedent — one
# reason, a copy per side. Asked of the jam kind only, the one whose page reads the toolhead.
_EMPTY_TOOLHEAD_DETAIL: dict[str, str] = {
    "unload_failed": "The unload did not complete. Open the AMS and free the filament at the feeder.",
    "feed_path_blocked": (
        "Replacement spools failed to load repeatedly — the filament path (buffer / PTFE) is likely "
        "blocked. Clear the buffer and PTFE path. Left PAUSED for a human."
    ),
    "printer_offline": (
        "The printer stopped reporting during recovery, so the farm could not read what the AMS did. Check the "
        "printer's connection and the AMS."
    ),
    "recovery_interrupted": (
        "A recovery was interrupted (the server restarted mid-swap) and the printer is still PAUSED with no "
        "fault the wire can still name — check the filament path."
    ),
}


def _slot_name(global_tray: int | None) -> str:
    """A tray's human name through the one slot-name origin (``runout_slot_desc``)."""
    return runout_slot_desc(global_tray) or f"tray {global_tray}"


def _refill_sentence(refill: RefillResult) -> str:
    """What the refill attempted and what the AMS answered — facts, no instruction."""
    match refill:
        case RefillLoaded(tray=tray):
            return f"{_slot_name(tray)} was refilled and no longer reads at the active nozzle."
        case RefillFailed(tray=None, answer=answer):
            return f"Refill failed: {answer.replace('_', ' ')}."
        case RefillFailed(tray=tray, command="unload", answer=answer):
            return f"The unload before the load from {_slot_name(tray)} failed: {answer.replace('_', ' ')}."
        case RefillFailed(tray=tray, answer=answer):
            return f"Load from {_slot_name(tray)} failed: {answer.replace('_', ' ')}."
        case RefillNoCandidate():
            return "No spool to load."
        case _:
            assert_never(refill)


def _feeder_clause(
    feed: tray_fields.ExtruderFeed | None, refill: RefillResult | None, *, jammed: int | None, reason: str
) -> str | None:
    """The sentence a jam escalation appends to its static reason copy, or None.

    The reason copy says WHY the farm gave up; this says what state the printer is in while it
    waits — the MEASURED toolhead (item 4 of 2026-10-10): ``feed`` is the ACTIVE extruder's live
    reading (``tray_fields.toolhead_feed``, the one reader of "is the toolhead fed" — per
    extruder on a dual nozzle), never ``tray_now`` alone. Incident 192 (004-H2S 2026-09-17) was
    the first half of this: the page never said the extruder was empty, so a Resume was the
    natural next click and the print ran four hours on air. 011/014-H2S 2026-10-09/10 was the
    second: the page said "slot N is loaded … press Continue" over a toolhead a queued pull-back
    had emptied.

    * fed from ``N``: "``N`` is loaded" (or, the jammed tray, "The jammed spool is still loaded",
      with its instruction) — only when the active extruder reads fed from ``N``;
    * EMPTY: "Toolhead empty." plus what the refill attempted and what the AMS answered
      (``refill``, the driver's own statement), plus the exit — load a spool, THEN resume —
      never a resume or a Continue over the empty toolhead;
    * external / unknown: nothing (no AMS slot to name, or nothing read).

    ``reason`` gates the instruction half (:data:`_CLAUSE_WITHOUT_INSTRUCTION`), the
    ``_retract_clause`` pattern — one instruction per page, never two. Runout / physical / external
    kinds get no clause at all, and a page composed after the job ended asks for none
    (:func:`_job_ended`, :func:`_compose_detail`).
    """
    if feed is None:
        return None
    instruct = reason not in _CLAUSE_WITHOUT_INSTRUCTION
    match feed.kind:
        case "fed":
            slot = _slot_name(feed.tray)
            refilled = isinstance(refill, RefillLoaded) and refill.tray == feed.tray
            if feed.tray == jammed:
                state = (
                    f"The jammed spool was unloaded and reloaded ({slot})."
                    if refilled
                    else f"The jammed spool is still loaded ({slot})."
                )
                return f"{state} Clear the extruder, then resume on the printer." if instruct else state
            return f"Toolhead refilled from {slot}." if refilled else f"{slot} is loaded."
        case "empty":
            facts = "Toolhead empty." if refill is None else f"Toolhead empty. {_refill_sentence(refill)}"
            if not instruct:
                return facts
            drying = isinstance(refill, RefillFailed) and refill.answer == "drying"
            exit_ = (
                "Load a spool after the drying cycle, then resume on the printer."
                if drying
                else "Load a spool on the printer, then resume."
            )
            return f"{facts} {exit_}"
        case "external" | "unknown":
            return None
        case _:
            assert_never(feed.kind)


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
    refill: RefillResult | None,
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
      :func:`_feeder_clause` — the MEASURED toolhead (the ACTIVE extruder, live) plus the
      driver's own refill statement (incident 192's and 014-H2S's missing half), gated by the
      reason. Over an EMPTY toolhead a reason whose copy instructs a resume takes its empty-side
      copy (:data:`_EMPTY_TOOLHEAD_DETAIL`), so no page says resume or Continue over air.
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

    A page composed after the job ENDED (:func:`_job_ended`) takes the ended-side copy of
    its reason and no feeder clause: every clause instruction ends "resume on the printer",
    and the print is over.
    """
    ended = _job_ended(reason, evidence)
    # Where the filament sits is read off a LIVE wire only (:func:`live_reading.reads_live`): a printer off
    # its session still carries its last session's cached feeder fields, and a clause read from
    # them would describe a printer the farm cannot see. The ACTIVE extruder (K1), never "any".
    feed: tray_fields.ExtruderFeed | None = None
    refill_page = reason in _TOOLHEAD_REASONS
    if (incident.is_feed_fault or refill_page) and not ended:
        st = _get_state(incident.printer_id)
        if live_reading.reads_live(st):
            feed = tray_fields.toolhead_feed(st).active
    empty = feed is not None and feed.kind == "empty"
    detail = (
        (_ENDED_JOB_DETAIL.get(reason) if ended else None)
        or (_EMPTY_TOOLHEAD_DETAIL.get(reason) if empty else None)
        or _ESCALATE_DETAIL.get(reason, reason)
    )
    clauses: list[str] = []
    if incident.is_feed_fault:
        sent = _evidence_sentence(evidence) if evidence is not None else None
        feeder = _feeder_clause(feed, refill, jammed=incident.jammed_global_tray, reason=reason)
        clauses = [clause for clause in (sent, feeder) if clause is not None]
    elif refill_page:
        # A refill on a row of another kind (its own toolhead row, a physical or runout row it
        # re-entered): the measured toolhead and the refill, nothing of a swap.
        feeder = _feeder_clause(feed, refill, jammed=incident.jammed_global_tray, reason=reason)
        clauses = [feeder] if feeder is not None else []
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
        # pull-back. Escalated AT ENTRY (status, not just outcome) — unowned,
        # this class would wait on the pause-stall watchdog.
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


def _new_fault_verdict(row: PrinterIncident, state, printer_id: int) -> incident_resolution.Verdict:
    """Does the fault standing on the wire now END the open AMS row ``row``?

    The rule table's ``new_fault`` occasion, asked with the entry's own live state and
    the ONE liveness store. One spelling for the entry gate (:func:`on_ams_fault`) and
    its mirror (:func:`owned_full_codes`), so a suppressed raw alert and an opened incident can
    never disagree about whether the open row still owns the printer.
    """
    return incident_resolution.resolve(
        row,
        "new_fault",
        Context(
            state=state,
            ledger=ledger,
            driver_live=printer_incidents.driver_live(printer_id),
            command_pending=_pending_on(row),
        ),
    )


def _pending_on(row: PrinterIncident) -> bool:
    """Does the farm's last motion command on ``row`` still pend? The store's DB-free projection
    (``printer_incidents.pending_command``, the ledger's one derivation), handed to the rule table
    as ``Context.command_pending`` by every closer here — the one place each asks it."""
    return printer_incidents.pending_command(row.printer_id, incident_id=row.id) is not None


async def _close_for_new_fault(db: AsyncSession, row: PrinterIncident, verdict: incident_resolution.Verdict) -> None:
    """End the open AMS row the new fault answered — the closers' own two steps.

    ``printer_incidents.close`` (the store's one closer, which logs the ``CLOSED …
    source=`` line) and the unit's hold projection (``printer_incidents.clear_hold_projection``), in
    that order, as every lifecycle closer does; the new incident stamps its own token
    when it opens. A close somebody else won leaves nothing to clear: that closer
    cleared it.
    """
    incident_id, printer_id, item_id, kind, status = row.id, row.printer_id, row.item_id, row.kind, row.status
    if await printer_incidents.close(db, incident_id, status=STATUS_RESOLVED, source=verdict.source) is None:
        return
    cleared = await printer_incidents.clear_hold_projection(db, item_id)
    logger.info(
        "spool_recovery: printer %s new fault — %s incident %s closed (was %s) — %s%s",
        printer_id,
        kind,
        incident_id,
        status,
        verdict.evidence,
        "; hold token cleared" if cleared else "",
    )


async def on_ams_fault(printer_id: int, state) -> asyncio.Task | None:
    """Own the AMS faults standing on this printer right now. Never raises.

    Called (guarded, fire-and-forget) from ``main.on_printer_status_change`` on
    EVERY push that carries HMS — deliberately not on the notify dedup's "new
    codes" edge, which is what made the old machine silent for a code standing
    at restart or flapping inside the 600 s re-notify window (9 runout episodes with
    no incident and no log line).

    Gates, in order — the first three are DURABLE, so a restart changes none of them:

    0. actionable candidates exist, and this exact fault was not evaluated moments
       ago (:func:`_eval_throttled` — a standing fault rides ~1 Hz);
    1. the ``spool_recovery_enabled`` setting;
    2. this printer has NO open AMS incident (the partial unique index is the real
       enforcement; the pre-check is what turns the ordinary case into a log line) —
       or the open one ENDS on this fault: the rule table's ``new_fault`` occasion is
       asked of it FIRST (:func:`_new_fault_verdict`), and a row it closes is closed
       through the store, its hold token cleared, and the fault then opens its own
       incident exactly as if no row had been open (002-H2S 2026-09-15/16: three jams
       swallowed behind pull-back rows the resume had in fact repaired). A row that
       stands keeps the printer, and only a worse class re-classifies it (the upgrade);
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
        # external faults stand together, ``_primary_candidate`` picks the AMS one,
        # which is correct: a real AMS fault beside a holder fault is still an AMS fault
        # to recover.
        external = primary.external if primary is not None else False
        code = primary.short_code if primary is not None else ""
        full_codes = _recorded_words(candidates)

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
            stands = ""
            if existing is not None:
                # Does THIS fault end the open row? Asked before the outrank test, because
                # a row the table closes owns nothing any more: its fault is over, and the
                # new one is its own incident, opened below as if no row had been open.
                ended = _new_fault_verdict(existing, state, printer_id)
                if ended.close:
                    await _close_for_new_fault(db, existing, ended)
                    existing = None
                else:
                    stands = ended.evidence
            if existing is not None and not _outranks(fault_class, existing.kind):
                _note_outcome(
                    printer_id,
                    job_id,
                    fingerprint,
                    "not opened — this printer already has an open AMS incident",
                    detail=f"incident {existing.id} {existing.status} kind={existing.kind}: {stands}",
                )
                return None

            if existing is None and printer_incidents.refill_in_progress(printer_id):
                # The farm is refilling this printer's toolhead on its own row (K10 — an open
                # ``toolhead_refill`` row, ``recovering``: the durable promise, read like a job
                # pause, never liveness). The fault is that refill's reading — its own load answers
                # it and a failure pages on its row — and opening a row here would spawn a SECOND
                # driver onto one AMS. Owned on the next push once the refill has closed its row or
                # handed it to a person, if it still stands; ``owned_full_codes`` mirrors this gate.
                _note_outcome(
                    printer_id,
                    job_id,
                    fingerprint,
                    "not owned — the farm is refilling this printer's toolhead; its own load reads this fault",
                )
                return None

            if existing is None and printer_incidents.job_pause_held(printer_id):
                # The printer PAUSED this job at its own pre-print plate check, and every
                # act this machine owns ends in a resume — a release lever, the swap round's
                # resume, the refill auto-resume. Any of them would restart the print onto
                # the plate the printer refused with no re-check. So no new AMS incident is
                # opened while that question stands: the plate-check episode answers it
                # (``pause_recovery`` — its re-check press, then a stop; a human in its
                # fallback), and a fault still standing after the job pause ends is owned on
                # the very next push (its raw alert reaches the operator meanwhile —
                # ``owned_full_codes`` mirrors this gate). An AMS row ALREADY open keeps its
                # owner; the upgrade below only re-classifies it and resumes nothing.
                _note_outcome(
                    printer_id,
                    job_id,
                    fingerprint,
                    "not owned — the printer paused this job at its plate check; that answer comes first",
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
                # fault resolves by its own wire/repair rule — and ``owned_full_codes`` still
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


async def owned_full_codes(db: AsyncSession, printer_id: int, state) -> frozenset[str]:
    """The FULL codes an AMS incident speaks for on this printer right now.

    (1) It exists so the HMS notify pipeline (main.py) can SUPPRESS the raw per-code
        alert for a code an incident carries — ``error.full_code in owned`` and nothing
        else. The incident's lifecycle notifications (recovering / succeeded /
        self-healed / out-of-rotation / failed / auto-resumed) are the operator-facing
        signal, and a duplicate raw alert double-notifies (one 2026-07-20 feed fault
        produced 4 Discord messages; 011-H2S 2026-09-29, incident 419, paged twice
        while the driver owned its jam).
    (2) FULL codes, never short codes. The short form is many-to-one: ``0700_0001`` is
        the slot-3 overload ``0700_6200_0002_0001`` AND the slot-3 runout demand
        ``0700_2200_0002_0001`` (and, on H2C, "a new AMS detected"), so a short-code set
        owning the overload would silence a runout demand on the same printer. Both wire
        lanes carry a full code (16 hex ``hms[]``, 8 hex ``print_error``); an entry with
        none is never owned.
    (3) Three arms, one authority each:
        * the ACTIONABLE candidates — when an incident owns them, which MIRRORS
          :func:`on_ams_fault`'s durable entry gates: a standing AMS row (the raw alert
          is the duplicate), or the gates a new row opens past (no job paused at its
          plate check, no aborted close of this exact fault). Foreign prints
          included — an incident owns every class of their AMS faults (runouts and
          physical faults since 2026-08-09, mechanical since the 2026-08-10
          origin-agnostic ruling);
        * the slot-attributed RUNOUT words (``hms_errors.slot_runout_full_codes``) —
          only when the incident speaks for a runout: a RUNOUT candidate among the
          owned, or a standing runout row. A jam's messages never speak for a "fill
          slot N" demand, so beside a jam that demand still pages;
        * INFORMATIONAL notices (``hms_errors.live_notices``) on the AMS unit the
          STANDING row names (``slot_global_tray``). The authority is the open row, not
          same-push candidates: the operator's Retry is exactly the push on which the
          actionable code clears and the feed-resistance notice appears (011-H2S
          17:59:18). A row that names no slot names no unit, and its notices page.
    (4) Fails toward NOTIFYING (invariant 10): recovery disabled, or any exception,
        returns the empty set — never suppress on the strength of a predicate that
        errored. A push with neither a candidate nor a notice costs no DB read.
    """
    try:
        candidates = live_candidates(state)
        notices = live_notices(state)
        if not candidates and not notices:
            return frozenset()
        if not await _read_bool(db, "spool_recovery_enabled", _DEFAULT_ENABLED):
            return frozenset()
        # The AMS-kind row: a pause-cause hold is not an AMS fault's owner, and a
        # plate-vision row standing beside a jam must not silence the jam's raw alert.
        # A row the live fault ENDS is closed by the entry and speaks for nothing; the
        # entry then runs its remaining gates, so the candidates fall through to them.
        row = await printer_incidents.get_open(db, printer_id, kinds=AMS_FAULT_KINDS)
        if row is not None and _new_fault_verdict(row, state, printer_id).close:
            row = None
        owned: set[str] = set()
        if candidates and (row is not None or await _entry_would_open(db, printer_id, state, candidates)):
            owned.update(c.full_code for c in candidates)
            if (row is not None and row.kind == KIND_RUNOUT) or any(
                c.fault_class is AmsFaultClass.RUNOUT for c in candidates
            ):
                owned.update(slot_runout_full_codes(getattr(state, "hms_errors", None) or []))
        if row is not None:
            row_unit, _tray = decode_global_tray(row.slot_global_tray)
            if row_unit is not None:
                owned.update(n.full_code for n in notices if n.ams_unit == row_unit)
        owned.discard("")
        return frozenset(owned)
    except Exception:  # noqa: BLE001 — a suppression predicate must never crash the notify path
        logger.exception("spool_recovery: owned-codes predicate failed for printer %s", printer_id)
        return frozenset()


async def _entry_would_open(db: AsyncSession, printer_id: int, state, candidates: frozenset[FaultCandidate]) -> bool:
    """With no standing AMS row, would :func:`on_ams_fault` open one for ``candidates``?

    Its remaining durable gates, mirrored: while the printer's job is paused at its own
    plate check no NEW AMS incident opens, so nothing speaks for the fault and its raw
    alert is the only word the operator gets; and a fault an ABORTED close barred will
    never be owned again — its raw alert goes through rather than into silence. And while the farm
    is refilling the toolhead on its own row (``printer_incidents.refill_in_progress``), the entry
    opens nothing either.
    """
    if printer_incidents.refill_in_progress(printer_id) or printer_incidents.job_pause_held(printer_id):
        return False
    job_id = (getattr(state, "subtask_id", None) or "").strip()
    return not await _fault_already_closed(db, printer_id, job_id, candidate_fingerprint(candidates))


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
        # token held to the terminal — the silent class.
        #
        # Derive-don't-store: nothing records "an edge was deferred"; the LEVEL is
        # re-read once, here, where the slot is already free. A no-op when the row is
        # already closed, and never a second closer — this IS the closer. Only a LIVE
        # RUNNING counts (:func:`live_reading.reads_live`): a printer the driver gave up on as offline
        # still carries its dead session's cached RUNNING, and closing on it would end the
        # very hold that ``printer_offline`` page just raised.
        st = _get_state(pid)
        if st is not None and live_reading.reads_live(st) and _live_state(st) == "RUNNING":
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
    st, _waited = await _live_reading(incident)

    # A driver re-entered on an incident whose restart stop already went out (a deploy
    # mid-restart): the job is over, so there is no PAUSE to wait for and no lever left —
    # the continuation picks up from the log. A job still live under the stop step (the
    # stop never landed) falls through: every rung is spent, and the ladder reads it.
    if evidence.restart_stop is not None and _live_state(st) in _JOB_OVER_STATES:
        await _restart(incident, evidence=evidence)
        return

    # (1) The fault PAUSEs the print; a runout the firmware backup rescued never
    #     PAUSEs → close silently as TRANSIENT. Closed as ``aborted`` with no
    #     resolve_source (no actor can claim an outcome nobody produced), which
    #     also bars re-entry for this exact fault on this job: the firmware can
    #     leave the code standing after healing itself, and the per-push entry
    #     gate would otherwise re-open an incident every throttle window. A session
    #     gap inside the wait is not "never PAUSEd": the wait stops its clock for it.
    if not await _await_live_state(incident, frozenset({"PAUSE"}), incident.settings.step_timeout_s):
        if evidence.pending_command is not None:
            # Not a transient: a re-entered incident whose log holds a farm command the AMS has
            # not run, on a printer somebody resumed while the farm was down. That command can
            # still drain under the RUNNING print, so the row is kept the way every takeover
            # keeps it while one pends (K11) — :func:`_abort`'s one rule, never a second.
            await _abort(incident)
            return
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

        # (0) NEVER SEND BEHIND A PENDING COMMAND (2026-10-10, 011/014-H2S). A driver
        # re-entered after a farm restart can find its log ENDING in a command the AMS has not
        # run — held at its bound, or sent and never read (the process died between the two).
        # A fresh send would queue behind it and both would run (012-H2S 2026-09-23), so the
        # round waits for it (:func:`_await_pending_command`) and continues the contract FROM
        # it: a load that ran → the resume; an unload that ran (or that nothing physical can
        # answer) → the load, the pull-back being that load's unload. Inside a round a held
        # command is waited on in its own confirm, so only a restarted driver meets one here.
        settled: CommandStep | None = None
        pending = evidence.pending_command
        if pending is not None:
            waited = await _await_pending_command(incident, pending, evidence=evidence)
            match waited:
                case "ran" | "undecidable":
                    settled = pending
                case "void":
                    pass  # the reboot emptied the queue: this round starts from the live wire
                case "abort":
                    await _abort(incident)
                    return
                case "handover":
                    _hand_over(incident)
                    return
                case _:
                    assert_never(waited)
            if settled.command == "load" and settled.target is not None:
                tail = await _resume_and_read(incident, settled.target, evidence=evidence)
                match tail:
                    case "over":
                        return
                    case "next_round":
                        continue
                    case _:
                        assert_never(tail)

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
                case "stopped":
                    # The restart rung ended a farm job that had deposited nothing — its
                    # release. The row stays open through the continuation.
                    await _restart(incident, evidence=evidence)
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
        # driver refilled). Whether a stamp is written at all is
        # `_commit_out_of_rotation`'s call: an extruder-side fault commits the SWAP
        # and parks nothing (006-H2S 2026-09-21, incident 289).
        if not evidence.swap_committed and incident.jammed_global_tray is not None:
            await _commit_out_of_rotation(incident, incident.jammed_global_tray, role="jammed")

        # (c) The unload — unconditional before a load (invariant 8), never behind a
        # held command. A pull-back the round top found pending and saw settle IS this load's
        # unload: it went out after the fault, and resending it is the second command behind a
        # queued one the wait exists to prevent.
        unload: UnloadStep
        if settled is not None:
            logger.info(
                "spool_recovery: printer %s the farm's pull-back (step %s) settled %s — it is this load's unload; "
                "not resent",
                pid,
                settled.seq,
                evidence.command_step(settled.seq).answer,
            )
            unload = "complete"
        else:
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
                # Held INSIDE a change: the AMS re-entered one under the driver and
                # acknowledged the unload without moving, so it runs at the next release,
                # which only a human causes there. Nothing may be sent behind it — the page
                # names the held unload, and the row stays open on it. (Held OUTSIDE a change
                # never arrives here: the confirm waited for it to run — :func:`_held_outcome`.)
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
                # refilling the extruder there — never resent, and no refill behind it (the
                # command pends). Held OUTSIDE a change was waited on (:func:`_held_outcome`).
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

        # (d) The resume — the contract's own verb, read by THE reader (:func:`_resume_and_read`).
        tail = await _resume_and_read(incident, target, evidence=evidence)
        match tail:
            case "over":
                return
            case "next_round":
                continue
            case _:
                assert_never(tail)

    # (4) Every candidate exhausted.
    _log_candidate_outcome(incident, gtid=None, verdict="candidates_exhausted")
    await _give_up(incident, evidence.exhaustion_reason(), evidence=evidence)


# How the swap round's resume ended (:func:`_resume_and_read`): the incident's outcome was
# written (``over`` — recovered, ended by the driver, stood aside, handed over), or the round
# moves on to the next candidate (``next_round``).
RoundTail = Literal["over", "next_round"]


async def _resume_and_read(incident: RecoveryIncident, target: int, *, evidence: _RecoveryEvidence) -> RoundTail:
    """The contract's resume once ``target`` is at the toolhead — the SAME reader
    (:func:`_read_after`, ``budgeted=False``: not a release attempt, so the lever budget does not
    apply), reading the contract's own verb for ``swapped``, with one retry (the live 16:21:24 →
    16:22:57 recovery, where the first resume did not stick). Shared by the round's own load and
    by a round whose top saw the previous driver's pending load run (item 2 of 2026-10-10: the
    contract continues at the resume, never a second load)."""
    pid = incident.printer_id
    first = await _read_after(incident, "resume", evidence=evidence, budgeted=False)
    match first.reading:
        case "swapped":
            await _succeed(incident, _swapped_tray(first))
            return "over"
        case "self_healed" | "wedged" | "released":
            # One more resume (mirrors the live 16:21:24 → 16:22:57 recovery where
            # the first resume did not stick): the printer is back at PAUSE.
            pass
        case "ended":
            await _close_ended_by_driver(incident, evidence=evidence)
            return "over"
        case "no_pause" | "not_sent":
            return "next_round"
        case "abort":
            # ``paused_elsewhere`` among the tokens: another actor's quiet PAUSE after
            # this resume ran — stand aside, no second resume, no park.
            await _abort(incident, token=first.takeover)
            return "over"
        case "handover":
            _hand_over(incident)
            return "over"
        case "stopped":
            raise LookupError("the swap round's resume read 'stopped' — only a rung that ends the job can")
        case _:
            assert_never(first.reading)

    retry = await _read_after(incident, "resume", evidence=evidence, budgeted=False)
    match retry.reading:
        case "swapped":
            await _succeed(incident, _swapped_tray(retry))
            return "over"
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
                return "next_round"
            # The print RAN and stopped again — the ONE path on which a replacement
            # may be parked at all. Ask the WIRE which tray it blames (the same
            # evidence order the entry gate used; ``item=None`` because the dispatch
            # mapping now names the replacement itself, so it cannot corroborate
            # anything). The extruder-side rule lives in `_commit_out_of_rotation`.
            st, _waited = await _live_reading(incident)
            jammed_now, _verdict = _resolve_jammed_tray(st, candidates=live_candidates(st), item=None, printer_id=pid)
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
            return "next_round"
        case "ended":
            await _close_ended_by_driver(incident, evidence=evidence)
            return "over"
        case "no_pause" | "not_sent":
            return "next_round"
        case "abort":
            await _abort(incident, token=retry.takeover)
            return "over"
        case "handover":
            _hand_over(incident)
            return "over"
        case "stopped":
            raise LookupError("the swap round's resume read 'stopped' — only a rung that ends the job can")
        case _:
            assert_never(retry.reading)


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
    """THE driver's read of the wire: a LIVE state (:func:`live_reading.reads_live`), and how long the
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
    if st is not None and live_reading.reads_live(st):
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
        if st is not None and live_reading.reads_live(st):
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

# The live states in which the printer positively holds NO job — the rule table's own set
# (``incident_resolution._JOB_OVER_STATES``, imported above) is what the restart
# continuation (:func:`_restart`) works in once the driver's own stop has ended the job;
# anything else (a job preparing, running or paused) on that printer is somebody else's.


TakeoverToken = Literal[
    "reclassified", "job_changed", "job_ended", "hijacked_load", "operator_command", "resumed", "paused_elsewhere"
]

# What a poll loop is waiting in, so :func:`_takeover` can tell the loop's own readings from
# somebody else's act: ``"PAUSE"`` — a step of the swap round, the job paused under it;
# ``None`` — a release verb's window (RUNNING is its expected reading); ``"job_over"`` — the
# restart continuation, where a terminal is the driver's OWN doing (its stop) and a job on
# the printer is not.
Awaiting = Literal["PAUSE", "job_over"] | None


def _takeover(
    incident: RecoveryIncident,
    st: PrinterState,
    *,
    awaiting: Awaiting,
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

    ``awaiting`` is the state THIS step is waiting for (:data:`Awaiting`), so a loop can tell
    its own intermediate reading from someone else's action; ``target`` is the tray this step
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
        The printer is echoing a different ``subtask_id`` (``job_identity.same_job`` answers
        ``other``: both sides name a job). Whatever this driver was recovering, it is not
        what is on the wire now. In the restart continuation (``awaiting == "job_over"``)
        also ANY job on the printer (:data:`_JOB_OVER_STATES` read otherwise): the job the
        driver stopped cannot run again, and the unit's requeue cannot dispatch while this
        row holds the printer.
    ``job_ended``
        The live state is neither PAUSE nor RUNNING — IDLE / FINISH / FAILED / PREPARE.
        Somebody else's act in every loop that asks this predicate; the windows where a
        terminal can be the driver's own doing — a release verb's read, which tests it
        BEFORE asking (:func:`_read_after`, :data:`_DRIVER_STATES`), and the restart
        continuation, whose own stop produced it — never read it as a takeover.
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
    if same_job(getattr(st, "subtask_id", None), incident.job_id) == "other":
        return "job_changed"
    if awaiting == "job_over":
        if live not in _JOB_OVER_STATES:
            return "job_changed"
    elif live not in _DRIVER_STATES:
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


async def _takeover_exit(incident: RecoveryIncident, *, awaiting: Awaiting, step: str) -> bool:
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

    K11 (2026-10-10) asks nothing more of it: a hand-over never closed the row, so a farm
    command still pending on it stays on the row's ledger and projection
    (``printer_incidents.pending_command``) for the hand-over's refill, and the re-classifier
    has escalated the row (every kind that outranks a jam escalates at entry).
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


async def _operator_stopped(incident: RecoveryIncident, st, *, published_at: float | None) -> bool:
    """Did the OPERATOR stop this print? Read at the terminal inside a verb's window — and,
    for a rung whose own verb ends the job, once BEFORE its send (``published_at`` None).
    Two witnesses, either one sufficient:

    * the touchscreen's Stop — the firmware's cancel echo, which the client stamps as
      ``PrinterState.user_cancel_seen_at`` (wall clock, ``time.time()``; cleared at the next
      print start). Only a stamp NEWER than the verb's publish counts: an older one is an
      echo this verb's window did not produce. Before a send (``published_at`` None) any
      stamp this job carries is the operator's — the farm has sent no stop yet. The H2C
      emits no cancel echo (a screen stop there classifies as a genuine failure), so this
      witness is H2S-family only;
    * the Bambuddy UI's Stop — the DURABLE stop request on the job's unit
      (``farm_correlation.operator_stop_requested``, the terminal classifier's own reader),
      which ``print_control.stop_as_operator`` commits BEFORE the stop is sent. It is a fact
      about THIS job, not an instant: a Stop pressed before the verb went out ended the same
      print, and the request is never dropped by the terminal's own callback, so no poll can
      miss it. A FOREIGN print has no unit and so no request — its UI stop is read from the
      echo alone.
    """
    cancel_at = getattr(st, "user_cancel_seen_at", None)
    if cancel_at is not None and (published_at is None or cancel_at > published_at):
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

    A rung whose verb ENDS the job (:attr:`LeverSpec.ends_job` — the restart rung) reads its
    terminal as its release, ``stopped``, whatever else the wire says: the durable step on
    the ledger says the farm sent the stop, and the firmware's cancel echo is no witness
    against it — H2S echoes the farm's OWN remote stop as ``user_cancel_seen_at`` after the
    publish, and H2C echoes nothing. An operator Stop recorded BEFORE that rung was sent is
    asked at the send instead (:func:`_read_after`), where it still reads as theirs.

    FINISH is the print COMPLETING right after the verb: read like RUNNING, with the
    feeder the window last SAW fed standing in for a live one the end-of-print unload
    already emptied. Any other terminal is the operator's Stop when a Stop witness says so
    (:func:`_operator_stopped` — today's ``job_ended`` abort), and otherwise the driver's own
    verb ended the print — ``ended``, which the driver records itself.
    """
    if _lever(lever).ends_job:
        logger.info(
            "spool_recovery: printer %s reached %s inside the %s window — the restart rung's stop ended the job",
            incident.printer_id,
            live,
            lever,
        )
        return LeverRead("stopped", moved=True, position=position)
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
      window). The feeder is the ACTIVE extruder's (:func:`_feeding_position`, K1): on a
      dual nozzle the jammed tray still loaded on the deputy beside an empty active nozzle
      is a print on air, never a self-heal or a swap. For a lever with
      :attr:`LeverSpec.pause_on_empty_path`, a RUNNING sample whose feeder reads ``empty``
      → ``path_empty``, quiet or not: on a loaded stall that is the firmware's own retract
      of the jammed filament (012-H2S 2026-09-25, ``tray_now`` 2 → 255 every ~12 s after a
      resume), on an empty path it is the first sample, and a quiet RUNNING with nothing fed
      prints air (shape 39), so pausing it at once is the safe direction. Still no success
      at the deadline → ``hung`` (the live 009 case, ~2.5 min RUNNING in an incomplete
      change; and the fallback when no retract is ever seen).
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
        position = _feeding_position(st, jammed)
        if position.kind in ("jammed", "other"):
            last_fed = position
        if live not in _DRIVER_STATES:
            # Asked AT the terminal, once: both witnesses are durable for the job's life (the
            # request on the unit row, the echo stamp until the next print starts), so nothing
            # a poll could miss has to be latched between polls. Not asked of a rung whose own
            # verb ends the job: its operator witness was read before its send.
            return _terminal_read(
                incident,
                lever,
                live,
                stopped=not spec.ends_job and await _operator_stopped(incident, st, published_at=published_at),
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
            # A rung whose verb ends the job waits for its terminal: the AMS leaving the
            # change on the way there (the stop ends the change) is not a release, and only
            # a stop that never landed is read off the PAUSE, at the deadline.
            if (left and not spec.ends_job) or at_deadline:
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
    position = _feeding_position(st, incident.jammed_global_tray)
    if landed:
        return LeverRead(_pause_reading(st), moved=True, position=position)
    live = _live_state(st)
    if live not in _DRIVER_STATES:
        return _terminal_read(
            incident,
            lever,
            live,
            stopped=not _lever(lever).ends_job and await _operator_stopped(incident, st, published_at=published_at),
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

    A rung whose verb ENDS the job (:attr:`LeverSpec.ends_job`, the restart rung) asks the
    operator's witnesses ONCE, before anything is noted or sent: a Stop recorded before the
    farm's own stop — or a job already at a terminal — is somebody else's, and the rung is
    not pulled (the ``job_ended`` abort). After its send, every terminal in its window is its
    release (``stopped``).

    Logs ONE ``[spool_recovery] lever=<lever> outcome=<reading>`` line per read — the
    measurement the next wedge is triaged by, greppable per verb.

    The client is the manager's CURRENT one, fetched at each publish: a session gap can
    end with a re-registered client, and a handle held across it would publish into the
    one that is gone.
    """
    spec = _lever(lever)
    pid = incident.printer_id
    if spec.ends_job:
        # A job already at a terminal before this verb goes out ended by somebody else's act
        # (H2C sends no cancel echo, so the state is the only witness there).
        st, _waited = await _live_reading(incident)
        if _live_state(st) not in _DRIVER_STATES or await _operator_stopped(incident, st, published_at=None):
            return LeverRead(
                _note_takeover(incident, "job_ended", f"lever={lever}"),
                moved=False,
                position=_feeding_position(st, incident.jammed_global_tray),
                takeover="job_ended",
            )
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
        read = LeverRead("not_sent", moved=False, position=_feeding_position(st, incident.jammed_global_tray))
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


# How the release ladder ended (:func:`_release_ladder`). Closed. ``stopped`` is the
# restart rung's release: the continuation (:func:`_restart`) takes the incident from there.
LadderExit = Literal[
    "self_healed", "swapped", "released", "unreleased", "withheld", "ended", "stopped", "abort", "handover"
]


async def _release_ladder(incident: RecoveryIncident, *, evidence: _RecoveryEvidence) -> LadderExit:
    """Pull every UNSPENT lever of :data:`_LEVERS`, in table order, until one reads a
    self-heal, a release or a terminal. Called only while the AMS holds a filament change.

    Each rung is pulled back to back until its own budget is spent
    (:attr:`LeverSpec.pulls`, counted off the log — a restarted driver resumes mid-budget):
    ``wedged`` / ``no_pause`` / ``not_sent`` → the next pull, then the next rung (each pull
    is recorded on the log, so each is spent). A rung's pull rule is asked when the ladder
    REACHES it (:attr:`LeverSpec.pull_rule`), and a rung it refuses is skipped, NOT spent —
    the restart rung on a print that has deposited, or a foreign one. The restart rung's
    terminal is ``stopped``, the caller's continuation (:func:`_restart`). Every lever
    spent or refused — this driver's or a previous one's on the same incident — with the
    change still held is ``unreleased``: the caller gives up ``wedge_unreleased`` with
    NOTHING sent into the change.

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
    for lever, spec in _LEVERS.items():
        if evidence.lever_spent(lever):
            continue
        refusal = spec.pull_rule(incident)
        if refusal is not None:
            logger.info("[spool_recovery] lever=%s skipped printer=%s — %s (not spent)", lever, pid, refusal)
            continue
        while not evidence.lever_spent(lever):
            read = await _read_after(incident, lever, evidence=evidence, budgeted=True)
            reading = read.reading
            match reading:
                case "self_healed" | "swapped" | "released" | "ended" | "stopped" | "abort" | "handover":
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
    detail = _compose_detail(incident, "wedge_ended_print", refill=None, evidence=evidence)
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
                    job_ended=_job_ended("wedge_ended_print", evidence),
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


async def _restart(incident: RecoveryIncident, *, evidence: _RecoveryEvidence) -> None:
    """The restart rung's release — the driver's own stop ENDED a farm job that had
    deposited nothing (operator ruling 2026-09-29, 013-H2S incidents 410/411). Unload the
    stalled feeder; with a replacement in hand and neither bound tripped, park the jammed
    spool and close the row ``driver_restart``. The unit is the terminal's: the stop's
    terminal classifies ``fault_restart`` off the open row's projection and ``farm_policy``
    requeues it next in line, where the dispatcher maps it to the backup slot (the parked
    spool is out of rotation).

    The row stays OPEN throughout, and the driver live on it: the row is what keeps the
    dispatcher off this printer (``printer_incidents.hold_blocks_dispatch``) until the path
    is shown empty, and a live driver is what stands the terminal and running-edge closers
    aside (``incident_resolution._wire_job_terminal``).

    1. The stopped job's first fresh reports (:func:`_live_reading`), until the AMS reads out
       of the print's change — the terminal ends it — or the step window runs out: the unload
       is measured whatever it reads. From here the printer is read in the ``job_over``
       phase (:data:`Awaiting`): its terminal is the driver's own, and a job on it is not.
    2. The unload of the stalled feeder, through the swap round's own helper
       (:func:`_unload_and_confirm` → ``ams_command.unload``, dual-nozzle aware, answered by
       ``ams_command.classify``), NEVER skipped (``after_stop``). It goes out before any
       candidate scan: invariant 8's "with no candidate to load there is no unload" rests on
       shape 39 — an extruder emptied under a PAUSED print that a resume then prints air
       from — and a stopped job has no resume, so the tube is emptied whatever follows; the
       out-of-rotation stamp still waits for a replacement in hand (invariant 7's commit
       boundary, step 5). Anything but ``complete`` gives up ``restart_unload_failed``. A
       re-entered driver whose log already holds a completed unload after the stop does not
       send it again (:attr:`_RecoveryEvidence.restart_unloaded`).
    3. The bounds (:func:`_restart_refault`) — this plate's lineage was restarted once
       already, or this printer's path stalled again with no print completed since its last
       restart — give up ``restart_refaulted``: the path, not the spool, so nothing is parked.
    4. A replacement in hand (:func:`_select_replacement`, the swap's own selection; at layer
       0 the first-layer gram floor applies) — else ``restart_no_candidate``, nothing parked.
    5. The swap-commit boundary (:func:`_commit_out_of_rotation` — an extruder-side fault
       parks nothing), crossed once per incident: a swap round that committed before the
       stop parked the jammed spool then (:attr:`_RecoveryEvidence.committed_before_restart`).
       Then the close, ``RESOLVE_DRIVER_RESTART`` (a farm close: ``auto_recovered``). Every
       give-up routes through :func:`_give_up`, which refills nothing after the stop and
       re-classifies the row a physical hold.
    """
    pid = incident.printer_id
    await _await_stop_settled(incident)
    if await _takeover_exit(incident, awaiting="job_over", step="restart"):
        return

    # A continuation re-entered after a farm restart can find its own unload still pending
    # (sent, never read). It is waited for, never resent (item 2 of 2026-10-10): once it runs
    # it is the continuation's unload answered ``complete`` after the stop
    # (:attr:`_RecoveryEvidence.restart_unloaded`), and nothing goes out behind it.
    pending = evidence.pending_command
    if pending is not None:
        waited = await _await_pending_command(incident, pending, evidence=evidence, awaiting="job_over")
        match waited:
            case "ran" | "undecidable" | "void":
                pass
            case "abort":
                await _abort(incident)
                return
            case "handover":
                _hand_over(incident)
                return
            case _:
                assert_never(waited)

    if not evidence.restart_unloaded:
        unload = await _unload_and_confirm(
            incident, evidence=evidence, attempts=incident.settings.max_attempts, after_stop=True
        )
        match unload:
            case "complete":
                pass
            case "abort":
                await _abort(incident)
                return
            case "handover":
                _hand_over(incident)
                return
            case (
                "acted" | "no_movement" | "held" | "undecidable" | "session_changed" | "skipped" | "drying" | "refused"
            ):
                _log_candidate_outcome(incident, gtid=incident.jammed_global_tray, verdict=f"restart_unload_{unload}")
                await _give_up(incident, "restart_unload_failed", evidence=evidence)
                return
            case _:
                assert_never(unload)

    refault = await _restart_refault(incident)
    if refault is not None:
        logger.info("spool_recovery: printer %s restart bound tripped — %s", pid, refault)
        await _give_up(incident, "restart_refaulted", evidence=evidence)
        return

    target, _only_low = await _select_replacement(incident, evidence.tried, awaiting="job_over")
    if target is None:
        if await _takeover_exit(incident, awaiting="job_over", step="select_replacement"):
            return
        await _give_up(incident, "restart_no_candidate", evidence=evidence)
        return

    jammed = incident.jammed_global_tray
    if jammed is not None and not evidence.committed_before_restart:
        await _commit_out_of_rotation(incident, jammed, role="jammed")
    if not await _close_incident(incident, status=STATUS_RESOLVED, source=RESOLVE_DRIVER_RESTART):
        logger.warning(
            "spool_recovery: printer %s incident %s closed under the driver — the restart close stands down",
            pid,
            incident.incident_id,
        )
        return
    from backend.app.core.database import async_session

    spool = "unknown spool"
    try:
        async with async_session() as db:
            await printer_incidents.clear_hold_projection(db, incident.item_id)
            spool = await _describe_slot(db, pid, jammed)
    except Exception:  # noqa: BLE001 — the close stands; only its log line's detail is lost
        logger.exception("spool_recovery: restart bookkeeping failed for printer %s", pid)
    if incident.extruder_side_only is False:
        parked = f"parked spool {spool}"
    elif incident.extruder_side_only:
        parked = f"kept spool {spool} in rotation (extruder-side fault)"
    else:
        parked = f"kept spool {spool} in rotation (the fault's side is unknown)"
    # The unit's requeue is its terminal's (``farm_policy`` on the restart stop verdict); the
    # driver states the hand-back it relies on, not a requeue it did not see.
    logger.info(
        "spool_recovery: printer %s RESTARTED at layer %s — stopped job %s, unloaded tray %s, %s; unit %s handed "
        "back through its terminal (replacement tray %s in hand)",
        pid,
        incident.layer_at_fault,
        incident.job_id or "-",
        jammed,
        parked,
        incident.item_id,
        target,
    )


async def _await_stop_settled(incident: RecoveryIncident) -> None:
    """Read the stopped job's first fresh reports until the AMS is out of the print's
    change (the job's terminal ends it — 013-H2S's loaded ``1/5`` was held until one), or the
    step window runs out. Returns either way: the unload that follows is measured by
    ``ams_command.classify`` in whatever posture it finds, and a ``held`` answer there is a
    give-up, never a resend."""
    deadline = _now() + incident.settings.step_timeout_s
    while True:
        st, waited = await _live_reading(incident)
        deadline += waited
        if not ams_mid_filament_change(st) or _now() >= deadline:
            return
        await asyncio.sleep(_POLL_INTERVAL_S)


async def _restart_refault(incident: RecoveryIncident) -> str | None:
    """The restart's two bounds — the sentence of the one that trips, or None. Both DERIVED,
    never stored.

    * The UNIT (operator ruling 2026-09-29: once per unit): an ancestor of this plate ended
      with the restart verdict (``requeue.fault_restart_spent``, the lineage walk's owner).
    * The PRINTER — the path, not the spool (013-H2S 2026-09-28: after slot 3 stalled, slot 4
      stalled in the same tube segment): its last ``driver_restart`` close
      (``printer_incidents.last_closed_by``), with no print COMPLETED on it since
      (``print_binding.completed_since`` — a completed archive; an eject sweep makes none).
      Not ``incident_resolution.ledger.path_ran_at``: that sighting is process memory a
      deploy empties, and a stalled job's own pre-stall RUNNING samples (heating, levelling on
      a quiet path) qualify as one, so it could never bound a second stall.
    """
    from backend.app.core.database import async_session
    from backend.app.services import print_binding, requeue

    async with async_session() as db:
        item = await db.get(PrintQueueItem, incident.item_id) if incident.item_id is not None else None
        if item is not None and await requeue.fault_restart_spent(db, item):
            return f"unit {item.id}'s plate was already restarted once"
        last_restart = await printer_incidents.last_closed_by(db, incident.printer_id, RESOLVE_DRIVER_RESTART)
        if last_restart is None or last_restart.resolved_at is None:
            return None
        if not await print_binding.completed_since(db, incident.printer_id, last_restart.resolved_at):
            return (
                f"no print completed on this printer since its last restart (incident {last_restart.id}, "
                f"{last_restart.resolved_at.isoformat()})"
            )
    return None


def _unload_skippable(state) -> bool:
    """True only for the genuinely-clean "nothing to unload" state.

    ALL THREE must hold: nothing is feeding at the ACTIVE extruder — the hotend an unload
    acts on (``tray_fields.toolhead_feed``, K1: per extruder on a dual nozzle, where
    ``tray_now`` is the client's single guess) — the AMS state machine is idle
    (``ams_status_main == 0``), and no feed-fault code is standing. That is the restart /
    firmware-already-unloaded path the original short-circuit was written for. An unread
    feed is not "nothing feeding".

    Anything else — above all a live feed fault, or an ``ams_status_main`` stuck at
    ``1`` (filament_change) — means an explicit unload is exactly what the AMS needs,
    because after a feed fault an empty reading says only "nothing is feeding".
    """
    if state is None:
        return False
    if tray_fields.toolhead_feed(state).active.kind != "empty":
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
    """The feeder the command ACTS ON, at its send: the ACTIVE extruder relative to the jammed
    tray (:func:`_feeding_position`) — the entry ``ams_command.posture`` keys the answer by and
    ``refill_verdict.ran`` reads back, so the record and the answer describe one extruder. On an
    H2C the jammed tray on the deputy beside an empty active nozzle records ``empty``, never
    ``jammed`` (an unload then reads as run the moment it is sent); a fed extruder on a row
    that names no jammed tray records ``other``, so its unload's run stays readable."""
    return _feeding_position(_get_state(incident.printer_id), incident.jammed_global_tray).kind


async def _unload_and_confirm(
    incident: RecoveryIncident, *, evidence: _RecoveryEvidence, attempts: int, after_stop: bool = False
) -> UnloadStep:
    """Unload the feeder through ``ams_command.unload`` and read the wire's answer.

    ``skipped`` (nothing to unload — :func:`_unload_skippable`) / ``drying`` (the AMS is
    drying: a doomed lane) / an ``ams_command.Answer`` for the last publish that went out
    / ``refused`` (every publish this call made was an ``ams_command.Refusal`` — each one
    consumes an attempt at once, no confirm wait) / ``abort`` / ``handover``.

    Up to ``attempts`` publishes: an unload the AMS moved and never completed
    (``acted``), one it neither moved on nor acknowledged (``no_movement``) and a refusal
    are resent. ``held`` is NEVER resent — a second command behind a queued one runs too
    (012-H2S 2026-09-23) — and what follows it depends on the posture it was SENT into
    (``bambu_mqtt.ams_mid_filament_change`` over the send's entry snapshot, the one origin):
    held INSIDE the print's own change, it runs at the next release, which only a human can
    cause there (free the spool, Retry) — ``held`` is returned and the round gives up
    ``swap_held_after_release``; held OUTSIDE a change (the AMS accepted it and has not run
    it), it is the driver's to wait for (:func:`_await_pending_command`, 011/014-H2S
    2026-10-09/10) and a run answers ``complete``. ``session_changed`` is not resent HERE
    either: its answer was lost with its session, and the driver's next round re-reads the
    AMS before anything else goes out. Every other answer returns at once. Every publish that
    went out is a :class:`CommandStep` on the evidence log, noted at the send.

    The unload is sent even when ``tray_now`` already reads 255 — the client encodes
    that as ``ams_id/slot_id/target = 255``, byte-for-byte the operator's proven manual
    recovery command (invariant 8: unconditional before a load).

    ``after_stop`` — the restart continuation's unload (:func:`_restart`), once the driver's
    own stop has ended the job: it is NEVER skipped, because the stop's terminal wiped the
    printer's HMS list and :func:`_unload_skippable`'s "no live feed fault" then proves
    nothing (255 is "nothing is feeding", not "the path is clear" — invariant 8), and its
    confirm poll reads the printer in the ``job_over`` phase (:data:`Awaiting`), where the
    terminal is the driver's own.
    """
    st, _waited = await _live_reading(incident)
    if not after_stop and _unload_skippable(st):
        return "skipped"

    unit = _ams_unit_for_tray(getattr(st, "tray_now", None))
    refusal = await _wait_ams_write_window(incident.printer_id, unit)
    if refusal == _REFUSAL_DRYING:
        return "drying"

    awaiting: Awaiting = "job_over" if after_stop else "PAUSE"
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
        answer = await _confirm_unloaded(incident, sent, awaiting=awaiting)
        match answer:
            case "acted" | "no_movement":
                await evidence.answer(seq, answer)
                verdict = answer
            case "complete" | "undecidable" | "session_changed":
                await evidence.answer(seq, answer)
                return answer
            case "held":
                await evidence.answer(seq, answer)
                return await _held_outcome(incident, sent, seq, evidence=evidence, awaiting=awaiting)
            case "abort" | "handover":
                await _settle_cut_short(incident, seq, evidence=evidence)
                return answer
            case _:
                assert_never(answer)
    return verdict


async def _held_outcome(
    incident: RecoveryIncident,
    sent: ams_command.Sent,
    seq: int,
    *,
    evidence: _RecoveryEvidence,
    awaiting: Awaiting,
) -> Literal["held", "complete", "session_changed", "abort", "handover"]:
    """What a command answered ``held`` (step ``seq``, already on the log) comes to — decided by
    the posture it was SENT into, never by a clock.

    * INSIDE the print's own filament change (``bambu_mqtt.ams_mid_filament_change`` over the
      send's entry snapshot — the one origin of "mid-change"): it waits behind the print's head
      and runs at the next release, which only a human causes there (free the spool, Retry) —
      ``held``: the caller gives up ``swap_held_after_release``, the page naming the queued
      command, and the row stays open on it (``printer_incidents.pending_command``).
    * OUTSIDE a change: the AMS accepted it and has not run it, and it runs on its own
      (011/014-H2S 2026-10-09/10) — the driver waits for it (:func:`_await_pending_command`) and a
      run is ``complete``.
    """
    if ams_mid_filament_change(sent.entry):
        return "held"
    waited = await _await_pending_command(incident, evidence.command_step(seq), evidence=evidence, awaiting=awaiting)
    match waited:
        case "ran":
            return "complete"
        case "void":
            # The printer rebooted under it: the queue is gone, the command will never run, and
            # its step reads ``session_changed`` — the round re-reads the wire and sends afresh.
            return "session_changed"
        case "undecidable":
            # A held command was ACKNOWLEDGED with its entry feeder able to move (the classifier
            # holds only those), so the wire always answers whether it ran.
            raise LookupError("a held command settled 'undecidable' — only an unload sent with nothing loaded can")
        case "abort" | "handover":
            return waited
        case _:
            assert_never(waited)


async def _observe_command(
    incident: RecoveryIncident,
    command: ams_command.Command,
    target: int | None,
    sent: ams_command.Sent,
    *,
    step: str,
    awaiting: Awaiting = "PAUSE",
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

    ``awaiting`` is the phase the command runs in (:data:`Awaiting`): the swap round's, the
    job PAUSEd under it, or the restart continuation's, the job ended by the driver's stop.
    """
    observation = ams_command.Observation()
    entry = sent.entry
    deadline_s = incident.settings.step_timeout_s
    blocked_s = 0.0
    while True:
        st, waited = await _live_reading(incident, clock=ams_command.clock)
        blocked_s += waited
        token = _takeover(incident, st, awaiting=awaiting, target=target)
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


# How a wait on the farm's OWN pending command ended (:func:`_await_pending_command`). Closed.
# ``ran`` — the wire shows the command ran (``refill_verdict.ran``), answered ``complete`` on the
# log; ``undecidable`` — an unload sent with nothing loaded, and the toolhead still empty:
# nothing physical can answer it and its running can pull nothing back, answered
# ``undecidable`` (the classifier's own token for that); ``void`` — the printer rebooted (the
# power-loss prompt), which empties the AMS's queue, so the command will never run: answered
# ``session_changed`` (:func:`_void_on_reboot`); ``abort`` / ``handover`` — :func:`_takeover`
# (an operator's act, the job ending, a re-classified row), exactly as it ends every other wait
# of the driver.
PendingWait = Literal["ran", "undecidable", "void", "abort", "handover"]


async def _await_pending_command(
    incident: RecoveryIncident,
    step: CommandStep,
    *,
    evidence: _RecoveryEvidence,
    awaiting: Awaiting = "PAUSE",
) -> PendingWait:
    """Wait for the AMS to RUN a motion command the farm sent and it has not run — and send
    nothing behind it. THE driver's one wait on its own pending command (K6/C2, 011/014-H2S
    2026-10-09/10), for the command held OUTSIDE a change at its step bound
    (:func:`_unload_and_confirm` / :func:`_load_and_confirm`) and for the one a restarted driver
    finds pending on its log.

    The measured reason it waits instead of giving up: an acknowledged, unmoved pull-back sent
    into assist ran ON ITS OWN ~4.5 min after the send — 011-H2S 04:31:27 → 04:36:01, 014-H2S
    02:45:58 → 02:50:38 — with the print paused and no farm publish in between, emptying the
    toolhead. The give-up that used to follow the 90 s window (``unload_failed``, "slot N is
    loaded … press Continue") was the farm walking away from a command still queued, and the
    operator's resume printed air. So the driver stays the owner — the row ``recovering``, the
    driver live — until the command runs, and then continues the contract (the load, the
    resume, the read). NO elapsed time decides anything here (operator ruling 2026-10-10: "time
    is not the right signal"); the wait ends only on an EVENT:

    * the command RAN — ``refill_verdict.ran`` over the step's own ledger columns and the live
      toolhead (``tray_fields.toolhead_feed``, the ACTIVE extruder): the step is re-answered
      ``complete`` through the log's one writer — the classifier's own completion token, which
      is what "held, then ran" settles to (the C1 contract: "it then answers complete"); the
      ``held`` reading stays in the ``[ams-command] answer=held`` line and this module's;
    * a takeover (:func:`_takeover`, asked on EVERY poll exactly as :func:`_observe_command`
      does): somebody else's act, or the job's end — ``abort`` / ``handover``, the caller's to
      answer as everywhere else.

    Every read goes through the session gate (:func:`_live_reading`): a gap is a wait, and past
    the offline bound it raises :class:`_PrinterOffline` (the ``printer_offline`` give-up) — the
    session, never the command, decides that.

    LIVENESS only: once the command has been out :data:`_HELD_COMMAND_PAGE_S` since its send,
    ONE page names it (:func:`_page_pending_command`) — no hand-over, no status change, no
    escalation ledger row — and the wait goes on.
    """
    pid = incident.printer_id
    paged = False
    state_word = "held" if step.answer == printer_incidents.COMMAND_HELD else "unanswered"
    logger.info(
        "[spool_recovery] printer %s waits for the AMS to run its %s %s (step %s%s) — nothing is sent behind it",
        pid,
        state_word,
        step.command,
        step.seq,
        f", target {step.target}" if step.target is not None else "",
    )
    while True:
        st, _waited = await _live_reading(incident)
        token = _takeover(incident, st, awaiting=awaiting, target=step.target if step.command == "load" else None)
        if token is not None:
            await _settle_cut_short(incident, step.seq, evidence=evidence)
            return _note_takeover(incident, token, f"await_{step.command}")
        if power_loss_prompt_standing(getattr(st, "hms_errors", None) or []):
            await _void_on_reboot(incident.printer_id, step.seq, step.command, evidence=evidence)
            return "void"
        settles = refill_verdict.pending_settles(step.entry(), tray_fields.toolhead_feed(st))
        if settles == "undecidable":
            # Only a restart meets this: an unload sent with NOTHING loaded (its recorded
            # feeder names no loaded tray) and nobody read its answer. No reading of the wire
            # can say it ran, and with the toolhead empty its running pulls nothing back — so
            # it settles as the classifier settles that send, and nothing waits on it.
            await evidence.answer(step.seq, "undecidable")
            logger.info(
                "[spool_recovery] printer %s: the %s unload (step %s) was sent with nothing loaded and the toolhead "
                "reads empty — settled undecidable",
                pid,
                state_word,
                step.seq,
            )
            return "undecidable"
        if settles == "complete":
            await evidence.answer(step.seq, "complete")
            logger.info(
                "[spool_recovery] printer %s: the AMS ran the %s %s (step %s) %.0fs after the send "
                "(tray_now=%s ams_status=%s/%s) — recovery continues",
                pid,
                state_word,
                step.command,
                step.seq,
                _outstanding_s(step),
                getattr(st, "tray_now", None),
                getattr(st, "ams_status_main", None),
                getattr(st, "ams_status_sub", None),
            )
            return "ran"
        if not paged and _outstanding_s(step) >= _HELD_COMMAND_PAGE_S:
            paged = True
            await _page_pending_command(incident, step, job_ended=awaiting == "job_over")
        await asyncio.sleep(_POLL_INTERVAL_S)


def _pending_sentence(step: CommandStep) -> str:
    """The page-only bound's sentence: the queued command, and what happens when it runs.
    Sys-admin register — the fact, then what follows; nothing is asked of anyone."""
    queued = "the pull-back" if step.command == "unload" else f"the load of {_command_slot(step)}"
    return f"The AMS accepted {queued} and has not run it. Recovery resumes when it runs."


async def _page_pending_command(incident: RecoveryIncident, step: CommandStep, *, job_ended: bool) -> None:
    """ONE page while the driver waits on its own pending command past :data:`_HELD_COMMAND_PAGE_S`.

    PAGE ONLY — the page-only variant of :func:`_escalate`, through the same notification owner
    and event (``notification_service.on_spool_recovery_failed``, the recovery's own channel and
    audience), WITHOUT the escalation: no ``mark_escalated`` (the row stays ``recovering`` and the
    driver live — it still owns the command), no hold token on the unit, no
    ``recovery_escalation`` row, no quarantine count. Best-effort: a page that fails is logged
    and the wait goes on. Never raises.
    """
    from backend.app.core.database import async_session
    from backend.app.models.printer import Printer
    from backend.app.services.notification_service import notification_service

    detail = _pending_sentence(step)
    try:
        async with async_session() as db:
            printer = await db.get(Printer, incident.printer_id)
            printer_name = (printer.name if printer else None) or f"printer {incident.printer_id}"
            await notification_service.on_spool_recovery_failed(
                printer_id=incident.printer_id,
                printer_name=printer_name,
                job_name=incident.job_name,
                detail=detail,
                db=db,
                kind=incident.kind,
                runout_slot=runout_slot_desc(incident.jammed_global_tray),
                foreign=incident.item_id is None,
                job_ended=job_ended,
            )
        logger.warning(
            "[spool_recovery] printer %s incident %s: the %s (step %s) has not run %.0fs after the send — paged "
            "(page only; the driver keeps waiting)",
            incident.printer_id,
            incident.incident_id,
            step.command,
            step.seq,
            _outstanding_s(step),
        )
    except Exception:  # noqa: BLE001 — a liveness page must never end the wait
        logger.exception("spool_recovery: pending-command page failed for printer %s", incident.printer_id)


async def _void_on_reboot(printer_id: int, seq: int, command: str, *, evidence: _RecoveryEvidence) -> None:
    """Record that a firmware REBOOT voided the farm's pending command ``seq``: a power cycle
    empties the AMS's queue, so a command it had accepted and not run will never run.

    THE one place that fact is written, and its home is the step ledger — durable, through the
    log's one writer (``EvidenceLog.answer``): the step is answered ``session_changed``, the
    classifier's own token for "its answer was lost with its session", which settles it
    (``printer_incidents.command_pends``), so the open-row projection re-derives no pending
    command now and after any later restart. A reconnect alone voids nothing — a network blip
    keeps the firmware's queue — the reboot's witness is the firmware's power-loss prompt
    (``hms_errors.power_loss_prompt_standing``, ``0300_8007``, the one reader the power-loss lane
    reads too). Two callers: the driver's own wait (:func:`_await_pending_command`) and, with
    no driver live, the per-push sampler's lane (:func:`_void_pending_after_reboot`)."""
    await evidence.answer(seq, "session_changed")
    logger.info(
        "[spool_recovery] printer %s rebooted (the power-loss prompt stands) — the farm's queued %s (step %s) is "
        "void: the AMS's queue is gone, so it will never run",
        printer_id,
        command,
        seq,
    )


async def _void_pending_after_reboot(printer_id: int, pending: printer_incidents.PendingCommand) -> None:
    """The DRIVERLESS void: a row the hand-over left OPEN on a pending command, on a printer that
    rebooted. Spawned by :func:`note_demand_watch` (the one per-push entry point) and registered
    in the liveness slot in the same synchronous stretch, so the next push spawns no second one.
    Re-reads the ledger (``from_row``, the one constructor) and voids the step only while it
    still pends; releases the slot on exit. Never raises."""
    from backend.app.core.database import async_session

    try:
        async with async_session() as db:
            evidence = await _RecoveryEvidence.from_row(db, pending.incident_id)
        still = evidence.pending_command
        if still is not None and still.seq == pending.seq:
            await _void_on_reboot(printer_id, pending.seq, still.command, evidence=evidence)
    except Exception:  # noqa: BLE001 — a background lane must never raise
        logger.exception("spool_recovery: voiding the pending command failed for printer %s", printer_id)
    finally:
        task = asyncio.current_task()
        if task is not None:
            printer_incidents.release_driver(printer_id, task)


async def _settle_cut_short(incident: RecoveryIncident, seq: int, *, evidence: _RecoveryEvidence) -> None:
    """A takeover cut command ``seq``'s observation short, so the classifier never answered it.
    Settle what the wire ALREADY shows: a command the toolhead shows RAN (``refill_verdict.ran``
    over the step's own ledger columns and the live ``tray_fields.toolhead_feed``) is answered
    ``complete`` through the log's one writer; anything else stays as it is — unanswered or
    ``held`` — and so stays the farm's pending command (K11), which keeps the row open for the
    hand-over while the job lives (:func:`_abort`). Without it a load that LANDED in the same
    push as an operator's Resume would read as queued, and hold a row open for nothing. A
    printer that is not reporting live settles nothing."""
    st = _get_state(incident.printer_id)
    if st is None or not live_reading.reads_live(st):
        return
    step = evidence.command_step(seq)
    if printer_incidents.command_pends(step.answer) and refill_verdict.ran(step.entry(), tray_fields.toolhead_feed(st)):
        await evidence.answer(seq, "complete")
        logger.info(
            "spool_recovery: printer %s the %s (step %s) cut short by a takeover had already run — answered complete",
            incident.printer_id,
            step.command,
            seq,
        )


def _outstanding_s(step: CommandStep) -> float:
    """How long ``step`` has been out, measured from its durable send stamp (wall clock, naive
    UTC like the ledger's ``sent_at``) — so a restart does not reset it. 0 for a step with none."""
    if step.at is None:
        return 0.0
    return max(0.0, (datetime.utcnow() - step.at).total_seconds())


async def _confirm_unloaded(
    incident: RecoveryIncident, sent: ams_command.Sent, *, awaiting: Awaiting = "PAUSE"
) -> ams_command.Answer | StepTakeover:
    """Read what the wire answered the unload ``sent``, in the ``awaiting`` phase.

    ``tray_now == 255`` alone is never completion — after a feed fault it already reads
    255 before the unload (009-H2S 2026-07-20). Completion, and every other answer, is
    ``ams_command.classify``'s per-posture row: outside a change, idle + nothing fed
    after an observed cycle or held for ``ams_command.UNLOAD_GRACE_S``; mid-change with a
    feeder loaded, the feeder leaving for 255 and holding — or ``held`` when the firmware
    acknowledged it and nothing moved; mid-change with nothing loaded, ``undecidable``
    (nothing physical can answer)."""
    return await _observe_command(incident, "unload", None, sent, step="confirm_unloaded", awaiting=awaiting)


async def _select_replacement(
    incident: RecoveryIncident, tried: set[int], *, awaiting: Awaiting = "PAUSE"
) -> tuple[int | None, bool]:
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
        status2, _token = await _await_bare_tray_configured(incident, forced_slots, awaiting=awaiting)
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

    loaded_all = scheduler._build_loaded_filaments(status)
    candidates = [
        f
        for f in loaded_all
        if not f.get("is_external")
        and f.get("global_tray_id") != incident.jammed_global_tray
        and f.get("global_tray_id") not in tried
        # A tray reporting an explicit non-present state (9 = seated-but-unsensed) dooms a load
        # — the load-viability reading, shared with the refill (:func:`_tray_load_viable`).
        and _tray_load_viable(f.get("state"))
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


async def _await_bare_tray_configured(
    incident: RecoveryIncident, forced_slots: list[tuple[int, int]], *, awaiting: Awaiting = "PAUSE"
):
    """Poll (≤ ``step_timeout_s``) for a forced bare slot to gain a ``tray_type``, read in
    the caller's phase (``awaiting`` — the swap round's PAUSE, or the restart
    continuation's ended job).

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
        token = _takeover(incident, st, awaiting=awaiting)
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
    took): ``acted``, ``no_movement`` and a refusal are resent; ``held`` is NEVER resent —
    inside the print's change it is returned (it runs at the next release), outside one the
    driver waits for it to run (:func:`_held_outcome`).
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
            case "complete" | "session_changed":
                await evidence.answer(seq, answer)
                return answer
            case "held":
                await evidence.answer(seq, answer)
                return await _held_outcome(incident, sent, seq, evidence=evidence, awaiting="PAUSE")
            case "abort" | "handover":
                await _settle_cut_short(incident, seq, evidence=evidence)
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
    itself is not new — the driver already applied it to the REPLACEMENT — but it
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
    if incident.extruder_side_only is None:
        # A re-entered incident whose live wire and recorded codes name no mechanical-feed
        # fault: which side faulted is unknown, and a false stamp is permanent (invariant 11).
        logger.info(
            "spool_recovery: printer %s %s tray %s kept IN rotation — the fault's side is unknown (no "
            "mechanical-feed code on the wire or on record for %s)",
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
    (its copy is swap-and-out-of-rotation framed). ``swapped=False`` is the wedge's
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


def _tray_load_viable(state: object) -> bool:
    """Is a tray whose live ``state`` field reads ``state`` worth a LOAD? The load-viability
    reading of the one tri-state presence rule (``tray_fields.tray_presence`` over
    ``parse_tray_state``): an explicit non-present state (``9`` — seated-but-unsensed) dooms a
    load; unknown or unparseable FAILS OPEN — dialect variance must never exclude a real roll.
    The ``tray_type`` argument is pinned to the asserted-empty ``""`` so a tray's residual config
    never softens a non-present state code. ONE reading for the swap's candidates
    (:func:`_match_candidates`) and the refill's last feeder (:func:`_refill_pick`)."""
    return tray_fields.tray_presence(tray_fields.parse_tray_state(state), "") is not False


async def _bound_spool(printer_id: int, global_tray: int) -> Spool | None:
    """The spool bound to ``global_tray`` on this printer, or ``None`` (unbound, or a tray the
    codec cannot name). Best-effort: a failed read answers ``None``."""
    from backend.app.core.database import async_session

    ams_id, tray_id = decode_global_tray(global_tray)
    if ams_id is None:
        return None
    try:
        async with async_session() as db:
            assignment = await db.scalar(
                select(SpoolAssignment)
                .options(selectinload(SpoolAssignment.spool))
                .where(
                    SpoolAssignment.printer_id == printer_id,
                    SpoolAssignment.ams_id == ams_id,
                    SpoolAssignment.tray_id == tray_id,
                )
            )
    except Exception:  # noqa: BLE001 — a spool lookup must not crash the refill
        logger.exception("spool_recovery: bound-spool read failed for printer %s tray %s", printer_id, global_tray)
        return None
    return assignment.spool if assignment is not None else None


async def _refill_pick(
    incident: RecoveryIncident, evidence: _RecoveryEvidence, status: PrinterState
) -> tuple[int | None, str]:
    """WHICH spool refills the toolhead, and why: ``(global tray | None, source)``.

    1. The job's LAST FEEDER — the incident's jammed tray, the feeder the job was printing from
       when the fault interrupted it (the row's own fact, durable across a restart) — when it is
       physically present (:func:`_tray_load_viable` over its live tray) and its spool is not
       SPENT (``spent_at``, the exhaustion truth — doctrine rule 8). An out-of-rotation stamp
       does NOT disqualify it: Raymond 2026-09-17, "is it not better to reload original spool
       that was faulty raise the gate and let human fix?" — the farm puts the job's own spool
       back, pages, and the human fixes it. Not ``last_loaded_tray`` while a jammed tray is
       known: that wire field names the last tray that REACHED the toolhead — after a committed
       swap, the farm's own replacement, not what the job fed from — and a farm restart re-reads
       it from scratch. It is the fallback only for a row that names no jammed tray (the wire's
       per-job record; on a dual nozzle a bare slot). Not the ledger's last completed load: a
       replacement the farm loaded is not the job's spool, and the swap round tried it already.
    2. Otherwise the swap's own selection (:func:`_select_replacement`): the dispatcher's rules,
       the jammed and already-tried trays excluded, spent / out-of-rotation / archived rolls
       hard-excluded inside the matcher.
    """
    last = await _refill_last_feeder(incident, status)
    if last is not None:
        return last, "the job's last feeder"
    target, _only_low = await _select_replacement(incident, evidence.tried)
    return target, "the swap's selection"


async def _refill_last_feeder(incident: RecoveryIncident, status) -> int | None:
    """Tier 1 of :func:`_refill_pick`: the job's LAST FEEDER (the incident's jammed tray, else the
    wire's per-job ``last_loaded_tray``) when it is physically present and its spool is not spent —
    else ``None``. ONE reading, shared with the resume verb, which names the slot a refill loads
    first before the refill runs (:func:`_start_refill`)."""
    jammed = incident.jammed_global_tray
    last = jammed if jammed is not None else tray_fields.valid_feeder(getattr(status, "last_loaded_tray", None))
    if last is None:
        return None
    ams_id, tray_id = decode_global_tray(last)
    live = _live_tray_dict(status, ams_id, tray_id) if ams_id is not None and tray_id is not None else None
    spool = await _bound_spool(incident.printer_id, last)
    if live is not None and _tray_load_viable(live.get("state")) and (spool is None or spool.spent_at is None):
        return last
    logger.info(
        "spool_recovery: printer %s refill passes over the job's last feeder tray %s (%s)",
        incident.printer_id,
        last,
        "not present" if live is None or not _tray_load_viable(live.get("state")) else "its spool is spent",
    )
    return None


def _refill_failed(
    incident: RecoveryIncident, tray: int | None, command: ams_command.Command, answer: str
) -> RefillFailed:
    """The refill's failure, logged where it is decided (the parseable candidate-outcome line)."""
    _log_candidate_outcome(incident, gtid=tray, verdict=f"refill_failed_{command}_{answer}")
    return RefillFailed(tray=tray, command=command, answer=answer)


async def _refill_toolhead(incident: RecoveryIncident, *, evidence: _RecoveryEvidence) -> RefillVerdict:
    """THE refill step (K8, 2026-10-10): put a spool at an EMPTY toolhead, and say what happened.

    The rule it serves (operator requirement, Raymond 2026-10-10: "when i click resume there MUST
    be filament loaded. It's not the users job to know whether or not there's filament in there,
    the fact that it's unloaded is the farms POOR auto recovery mechanics"): a paused farm job
    never sits with an empty toolhead the firmware will not refill itself. ONE implementation —
    it replaces the give-up's restore of the jammed feeder, which it generalises.

    1. The spool: :func:`_refill_pick` — the job's last feeder when present and not spent (an
       out-of-rotation stamp does not disqualify it), else the swap's selection; none →
       :class:`RefillNoCandidate`.
    2. The unload first, invariant 8's decision, through the swap's own helper
       (:func:`_unload_and_confirm`: skipped only on the genuinely clean path,
       :func:`_unload_skippable` — never after a feed fault, where 255 is "nothing is feeding",
       not "the path is clear"). Anything but ``complete`` / ``undecidable`` / ``skipped`` is
       :class:`RefillFailed` on the unload.
    3. The load, through the swap's own helper (:func:`_load_and_confirm` → ``ams_command.load``,
       its drying pre-flight and its ``note_commanded_load`` mark, answered by
       ``ams_command.classify``; a load the AMS accepted and has not run outside a change is
       waited on there).
    4. "Reached the toolhead" = the load answered ``complete`` AND the ACTIVE extruder reads fed
       from the target (``tray_fields.toolhead_feed`` — on a dual nozzle a load can complete onto
       the other nozzle) → :class:`RefillLoaded`; else :class:`RefillFailed`.

    Every command is a step on this incident's ledger (the helpers note it at the send). Takeovers
    propagate (``abort`` / ``handover``). The caller establishes the preconditions — the toolhead
    reads empty, no command pends, no change is in flight (the AMS owns the toolhead's next motion
    there, and anything sent into it is held: 012-H2S 2026-09-23) — and the printer reporting on a
    live session; a session that drops mid-refill answers :class:`RefillFailed` (``session_changed``),
    never :class:`_PrinterOffline`, so a give-up always reaches its page.

    The refill's load is a command that went out, but it is NOT a candidate in the swap's
    accounting that matters: the give-up chooses its reason before the refill runs.
    """
    pid = incident.printer_id
    tray: int | None = None
    command: ams_command.Command = "load"
    try:
        st, _waited = await _live_reading(incident)
        tray, source = await _refill_pick(incident, evidence, st)
        if tray is None:
            st, _waited = await _live_reading(incident)
            token = _takeover(incident, st, awaiting="PAUSE")
            if token is not None:
                return _note_takeover(incident, token, "refill_pick")
            _log_candidate_outcome(incident, gtid=None, verdict="refill_no_candidate")
            return RefillNoCandidate()
        logger.info("spool_recovery: printer %s refills the toolhead from tray %s (%s)", pid, tray, source)

        command = "unload"
        unload = await _unload_and_confirm(incident, evidence=evidence, attempts=incident.settings.max_attempts)
        match unload:
            case "complete" | "undecidable" | "skipped":
                pass
            case "abort" | "handover":
                return unload
            case "held" | "acted" | "no_movement" | "refused" | "drying" | "session_changed":
                return _refill_failed(incident, tray, command, unload)
            case _:
                assert_never(unload)

        command = "load"
        load = await _load_and_confirm(incident, tray, evidence=evidence)
        match load:
            case "complete":
                pass
            case "abort" | "handover":
                return load
            case "held" | "acted" | "no_movement" | "refused" | "drying" | "session_changed":
                return _refill_failed(incident, tray, command, load)
            case _:
                assert_never(load)
    except _PrinterOffline:
        logger.warning("spool_recovery: printer %s went off its session during the refill — refill unread", pid)
        return _refill_failed(incident, tray, command, "session_changed")

    live = _get_state(pid)
    feed = tray_fields.toolhead_feed(live if live_reading.reads_live(live) else None).active
    if feed.kind == "fed" and feed.tray == tray:
        _log_candidate_outcome(incident, gtid=tray, verdict="refill_loaded")
        return RefillLoaded(tray=tray)
    return _refill_failed(incident, tray, "load", "not_at_toolhead")


async def _give_up(incident: RecoveryIncident, reason: str, *, evidence: _RecoveryEvidence) -> None:
    """THE give-up boundary: refill an EMPTY toolhead, then escalate.

    Every ``_escalate`` reachable inside :func:`_drive_recovery`'s candidate loop and after it
    routes through here, so the refill decision is made ONCE from live state instead of inline
    conditions drifting apart, and the page is composed from the incident's own evidence log and
    the MEASURED toolhead — and so does the offline bound (:class:`_PrinterOffline`, answered in
    :func:`_run_recovery`), whose refill is skipped by the live-session precondition rather than
    by a reason test. The escalations that never enter the loop (the entry gate, the runout
    branch) call :func:`_escalate` directly with no refill and no evidence: they sent nothing;
    the startup re-entry passes the hydrated log.

    **The refill (K8, 2026-10-10; T1 of the plan).** When the ACTIVE extruder reads EMPTY
    (``tray_fields.toolhead_feed``, live), the driver runs :func:`_refill_toolhead` BEFORE it
    pages — the farm never pages with the toolhead empty when a spool will load. Three
    preconditions, each an event-state, never a time:

    * no command PENDS on the log (``_RecoveryEvidence.pending_command``) — a command the AMS
      holds runs when it runs, a refill sent behind it would run too, and its refill is the
      hand-over's when it does (K11);
    * no filament change in flight (``bambu_mqtt.ams_mid_filament_change``) — the AMS owns the
      toolhead's next motion there, and anything sent into it is held behind the print's head
      (012-H2S 2026-09-23); a held refill unload would drain the extruder at the release;
    * no restart stop on the ledger — after the farm's OWN stop (operator ruling R1 2026-09-29,
      shape 45) no resume exists to print air, an unload the continuation completed stays done,
      and the row is held physical with the path empty.

    **A named reversal.** The restore this replaces (``_restore_jammed_feeder``, the 2026-09
    agent design) "undid only the driver's own motion": it reloaded only after a FARM unload
    moved the AMS, so a toolhead the firmware retracted — or a queued pull-back the AMS ran on
    its own, 011/014-H2S 2026-10-09/10 — was paged empty as "slot N is loaded". That rule was an
    agent's design, never an operator ruling, and it is reversed: an empty toolhead is refilled
    whoever emptied it, because the operator's resume prints air either way.

    :func:`_abort` and :func:`_hand_over` deliberately leave the extruder as it is — another
    actor owns the printer, and a takeover is never a give-up. A takeover DURING the refill
    aborts with ``restored=True``: the operator resumed on a feeder the FARM had just loaded.

    A give-up AFTER the restart stop (:attr:`_RecoveryEvidence.restart_stop`) also re-classifies
    the row a PHYSICAL hold before it pages (:func:`_hold_the_path`): a jam row is a wire-class
    JOB hold, which the sweep closes on an idle quiet printer, and the job it held is already
    over — the printer must stay held until the path is shown clear.
    """
    # The counters are read BEFORE the refill: the refill load is not a candidate and must not
    # feed the numbers the reason was chosen from.
    unloads_sent, unloads_confirmed = evidence.unloads_sent, evidence.confirmed_unloads
    loads_attempted, loads_confirmed = evidence.loads_attempted, evidence.loads_confirmed
    refill: RefillVerdict | None = None
    if evidence.restart_stop is None and evidence.pending_command is None:
        st = _get_state(incident.printer_id)
        if (
            st is not None
            and live_reading.reads_live(st)
            and not ams_mid_filament_change(st)
            and tray_fields.toolhead_feed(st).active.kind == "empty"
        ):
            refill = await _refill_toolhead(incident, evidence=evidence)
    result: RefillResult | None
    match refill:
        case "abort":
            # A takeover DURING the refill must not un-stamp the spool: the operator resumed on
            # a feeder the FARM had just loaded, so the click is not the "I declare this spool
            # usable" statement _clear_oor_if_resumed_on_jammed_feeder reads it as.
            await _abort(incident, restored=True)
            return
        case "handover":
            _hand_over(incident)
            return
        case None | RefillLoaded() | RefillFailed() | RefillNoCandidate():
            result = refill
        case _:
            assert_never(refill)
    logger.info(
        "[spool_recovery] give-up printer=%s reason=%s refill=%s unloads_sent=%s unloads_confirmed=%s "
        "loads_attempted=%s loads_confirmed=%s levers=%s",
        incident.printer_id,
        reason,
        result,
        unloads_sent,
        unloads_confirmed,
        loads_attempted,
        loads_confirmed,
        ",".join(f"{s.lever}:{s.outcome}" for s in evidence.lever_steps) or "-",
    )
    if evidence.restart_stop is not None:
        await _hold_the_path(incident)
    await _escalate(incident, reason, refill=result, evidence=evidence)


async def _hold_the_path(incident: RecoveryIncident) -> None:
    """Re-classify the open row a PHYSICAL hold, before a give-up after the restart stop.

    The row is a jam — a WIRE-class hold, a job hold: it closes when its printer runs
    again, when its job ends, and on the sweep once the printer reads positive and quiet
    (``incident_resolution._wire_sweep_tick``). After the driver's own stop the job has
    ENDED and the printer reads IDLE and quiet, so a wire row would close 120 s after the
    page and hand the dispatcher the path the continuation could not clear — or the one
    that just stalled twice. A physical row is the EQUIPMENT hold, closed by the repair
    evidence (a completed load, a print running through the path) or by Recover, which is
    exactly what the restart give-ups' copy names. Same row, same id and ``created_at``,
    through the store's one re-classification verb (``printer_incidents.upgrade``), keeping
    the fault's own code, fingerprint, slot and recorded words. Best-effort: the page must
    still go out if this write fails. A driver that re-reads its row after this sees
    ``reclassified`` — none does: the give-up is its last act.
    """
    from backend.app.core.database import async_session

    try:
        async with async_session() as db:
            row = await printer_incidents.get_open(db, incident.printer_id, kinds=AMS_FAULT_KINDS)
            if row is None or row.id != incident.incident_id or row.kind == KIND_PHYSICAL:
                return
            await printer_incidents.upgrade(
                db,
                row.id,
                kind=KIND_PHYSICAL,
                code=row.code,
                codes=row.codes,
                slot_global_tray=row.slot_global_tray,
                hms_full_codes=(row.hms_full_codes or "").split(","),
            )
    except Exception:  # noqa: BLE001 — a re-classification must never cost the page
        logger.exception(
            "spool_recovery: printer %s incident %s could not be held as a physical fault after the restart stop",
            incident.printer_id,
            incident.incident_id,
        )


async def _escalate(
    incident: RecoveryIncident,
    reason: str,
    *,
    refill: RefillResult | None = None,
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

    ``refill`` is :func:`_give_up`'s statement about the refill it ran over an EMPTY toolhead
    (:func:`_refill_toolhead`), and ``evidence`` its records of what it sent and what the wire
    answered; together with the live toolhead they let the composed detail say what the farm
    did and where the filament is (:func:`_compose_detail`). Both are passed explicitly and
    never inferred: the defaults (``None``) are the truthful answer for every caller that sent
    nothing.

    The KIND of the hold is the ROW's fact, read off the row ``mark_escalated`` returns —
    never the in-memory ``incident.kind``, which is the kind the driver was BUILT with.
    :func:`_give_up` re-classifies a jam row a physical hold after the restart stop
    (:func:`_hold_the_path`), and the page and the unit's token must name the hold the
    row — and the hourly reminder reading it — carries. What stays on the incident is
    what the DRIVER saw and did: the composed detail (its evidence log) and the runout
    spent stamp (paired with the driver's own reason).
    """
    from backend.app.core.database import async_session
    from backend.app.models.printer import Printer
    from backend.app.services.notification_service import notification_service

    # Per-tray diagnostic snapshot on every escalation (the 18:45 forensics gap).
    await _log_tray_snapshot(incident)

    # The DRIVER's account — what it sent and what the wire answered — keyed on the kind
    # it ran as: a jam machine that ended in a physical hold still sent a jam machine's
    # steps, and the page must say so.
    detail = _compose_detail(incident, reason, refill=refill, evidence=evidence)
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
            # The hold's kind, from the row in hand, and its projection onto the farm unit
            # (one token per kind). A foreign print has no unit — the row carries the whole
            # state there.
            kind = row.kind
            token = waiting_reason_for(kind, external=incident.external)
            item = await db.get(PrintQueueItem, incident.item_id) if incident.item_id is not None else None
            # The hold token belongs on the unit the printer is HOLDING. A unit that is no
            # longer printing has no hold to show — the restart give-ups come after the
            # driver's own stop ended the job and its terminal handed the unit back to the
            # queue, and a "recovery failed" token on that ended row would be a claim about
            # a print that is over.
            if item is not None and item.status == "printing":
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
                    kind=kind,
                    runout_slot=slot_hint,
                    foreign=incident.item_id is None,
                    job_ended=_job_ended(reason, evidence),
                )
            except Exception:  # noqa: BLE001 — notification failure is non-fatal
                logger.exception("spool_recovery: failed notification error for printer %s", incident.printer_id)

            # Durably record this escalation and quarantine the printer if its
            # AMS keeps escalating within the window (reuse the SAME session). ONE
            # ledger row per incident: an upgrade re-escalates a row that may already
            # have paged and been recorded, and the ledger — not a flag — says so.
            await _record_escalation_and_maybe_quarantine(db, incident, reason, opened_at=row.created_at)
        logger.warning(
            "spool_recovery: printer %s ESCALATED (%s) — %s",
            incident.printer_id,
            reason,
            "the job has ended; the printer is held" if _job_ended(reason, evidence) else "left PAUSED",
        )
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
        # reason rename cannot quietly point this at a different fault class — the pairing
        # of the DRIVER's reason with the kind the driver ran as, so it reads the incident,
        # not the row: the stamp is about the roll the driver's runout exhausted.
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

    If that actor resumed ON the jammed feeder (live RUNNING with the active extruder fed
    from the jammed global tray), they declared that spool usable — clear its
    out-of-rotation flag the same way a physical re-insert would, so a self-cleared
    jam does not leave the spool excluded from all future dispatch. Any other live
    state keeps the flag (a physical reseat stays the canonical clear).

    ``restored`` is the one thing that reading cannot survive: the driver ITSELF put
    the jammed spool back on the feeder (:func:`_refill_toolhead`), so "they
    resumed on the jammed feeder" says nothing about what the operator thinks of the
    spool — the farm chose that feeder, not them. The stamp stays; remove+re-insert or
    the Inventory control clears it.

    ONE exception to the close (K11, 011/014-H2S 2026-10-09/10): the AMS still holds a motion
    command the farm sent on this row (``printer_incidents.pending_command``) AND the job it
    holds still lives. That command is the farm's until it runs — an accepted pull-back ran on
    its own ~4.5 min after the send and emptied the toolhead under the print — so the row stays
    OPEN, ESCALATED (a human's now), the driver released, no ABORTED close and nothing barred:
    the hand-over's refill reads the row when the command runs, and the closers stand while it
    pends (``incident_resolution.Context.command_pending``). A job that is OVER (a terminal,
    another job — a ``job_ended`` / ``job_changed`` takeover) has no toolhead left to print air
    with: that row closes as it always did, the job terminal's exception to the same rule."""
    from backend.app.core.database import async_session

    pending = printer_incidents.pending_command(incident.printer_id, incident_id=incident.incident_id)
    if pending is not None and _job_lives(incident):
        # Keep the row: the farm's command is queued under a job that can still print air.
        await _hold_for_pending_command(incident)
        source = "-"
        logger.info(
            "spool_recovery: printer %s recovery stood aside (%s) — the AMS still holds farm %s step %s; "
            "incident %s stays OPEN (escalated) until it runs, a reboot voids it, or the job ends",
            incident.printer_id,
            token or "external_interference",
            pending.name,
            pending.seq,
            incident.incident_id,
        )
    else:
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


def _job_lives(incident: RecoveryIncident) -> bool:
    """Does the printer still hold the job this incident interrupted — PAUSE or RUNNING (the
    driver's own procedure states, :data:`_DRIVER_STATES`) on that job (``job_identity.same_job``
    answering anything but ``other``)? Read off the live state the takeover was just read from;
    a terminal, an idle printer, another job or no state at all reads False."""
    st = _get_state(incident.printer_id)
    return _live_state(st) in _DRIVER_STATES and same_job(getattr(st, "subtask_id", None), incident.job_id) != "other"


async def _hold_for_pending_command(incident: RecoveryIncident) -> None:
    """Keep the row OPEN as a human's hold while the farm's command on it still pends (K11):
    ``printer_incidents.mark_escalated`` — the row still open, ``escalated_at`` stamped once —
    and nothing else: no page (a takeover is never a give-up), no ledger row, no bar.
    Best-effort: a failed write leaves the row ``recovering``, which the startup re-entry
    re-drives (its log holds the pending command, so it waits rather than sends)."""
    from backend.app.core.database import async_session

    try:
        async with async_session() as db:
            await printer_incidents.mark_escalated(db, incident.incident_id)
    except Exception:  # noqa: BLE001 — a stand-aside must never crash the driver
        logger.exception(
            "spool_recovery: printer %s incident %s could not be held open for its pending command",
            incident.printer_id,
            incident.incident_id,
        )


async def _clear_oor_if_resumed_on_jammed_feeder(db: AsyncSession, incident: RecoveryIncident) -> None:
    """R3: an operator who resumes ON the jammed feeder (live RUNNING with the ACTIVE
    extruder fed from the jammed global tray — ``tray_fields.toolhead_feed``, K1; on a dual
    nozzle ``tray_now`` is the client's single guess onto a unit) has declared that spool
    usable — clear its out-of-rotation flag the same way a physical re-insert would. Any
    other live state (or a different feeding tray) keeps the flag; a physical reseat stays
    the canonical clear. Best-effort — ``_abort`` must never raise."""
    jammed = incident.jammed_global_tray
    if jammed is None:
        return
    st = _get_state(incident.printer_id)
    if st is None or getattr(st, "state", None) != "RUNNING":
        return
    if _feeding_position(st, jammed).kind != "jammed":
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
# Without it, the ONLY thing that cleared a runout hold was the farm's own
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


async def on_observed_running(printer_id: int) -> bool:
    """The printer is RUNNING again — close whatever incident it was holding.

    Called (guarded) from the per-push wire sampler's transition into RUNNING, which
    covers EVERY resume source: the farm's own auto-resume, an operator pressing
    Resume on the touchscreen, a UI resume, or the firmware recovering by itself.
    That breadth is the point — a hold clearable only by the one
    path that set it strands every other resume.

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
                verdict = incident_resolution.resolve(
                    incident, "running_edge", replace(ctx, command_pending=_pending_on(incident))
                )
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
                    closed.append(
                        (incident.id, kind, status, await printer_incidents.clear_hold_projection(db, item_id))
                    )
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
    job — this only closes incidents, so the two never fight over one row.
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
                verdict = incident_resolution.resolve(
                    incident, "job_terminal", replace(ctx, command_pending=_pending_on(incident))
                )
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
                        Context(state=state, ledger=ledger, driver_live=False, command_pending=_pending_on(incident)),
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
                    cleared = await printer_incidents.clear_hold_projection(db, incident.item_id)
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
    if same_job(getattr(state, "subtask_id", None), incident.job_id) != "same":
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

        driverless: list[tuple[int, int, str]] = []
        async with async_session() as db:
            for incident in await printer_incidents.all_open(db):
                pid = incident.printer_id
                live = printer_incidents.driver_live(pid)
                # The row's step ledger answers the one question the wire cannot: did the
                # driver STOP this job to restart it? A deploy mid-restart then finds the
                # printer IDLE — "not PAUSE" to the wire cell — and must re-enter the
                # continuation, which finishes what the log says is left (the unload, or
                # only the park and the close), never close the row ``startup_rearm``.
                # Asked of THIS driver's rows only: the restart stop is an AMS row's lever,
                # and another kind's ledger is another driver's vocabulary (a plate-check
                # episode's ``dialog`` / ``stop`` steps, ``pause_recovery``), which this
                # driver's log cannot read and must not report as drift.
                #
                # The same log answers the second question the wire cannot: does a motion
                # command the farm sent on this row still pend (K11 — held, or sent and never
                # read)? Read off the hydrated log here, not the projection: the projection is
                # rebuilt from the same ledger below (``rehydrate``), after this loop.
                restart_owed = command_pending = False
                if incident.kind in AMS_FAULT_KINDS | REFILL_KINDS:
                    try:
                        log = await _RecoveryEvidence.from_row(db, incident.id)
                        restart_owed = log.restart_stop is not None
                        command_pending = log.pending_command is not None
                    except LookupError:
                        # A ledger the driver cannot read is drift (``_step_of``); it must not
                        # cost every other row its startup verdict.
                        logger.exception(
                            "spool_recovery: startup — incident %s has an unreadable step ledger", incident.id
                        )
                verdict = incident_resolution.resolve(
                    incident,
                    "startup",
                    Context(
                        state=_get_state(pid),
                        ledger=ledger,
                        driver_live=live,
                        restart_owed=restart_owed,
                        command_pending=command_pending,
                    ),
                )
                if not verdict.close:
                    if driver_owns(incident, live=live):
                        driverless.append((incident.id, pid, incident.kind))
                    continue
                await printer_incidents.close(db, incident.id, status=STATUS_RESOLVED, source=verdict.source)
                await printer_incidents.clear_hold_projection(db, incident.item_id)
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
        # last word on the projection rather than being overwritten by it. A toolhead row is the
        # refill driver's (K10): its own re-entry, never the AMS machine's.
        for incident_id, printer_id, kind in driverless:
            if kind in REFILL_KINDS:
                _reenter_refill_incident(incident_id, printer_id)
            else:
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


def _reenter_refill_incident(incident_id: int, printer_id: int) -> None:
    """Give a driver back to a ``toolhead_refill`` row a restart left ``recovering`` (K10). The row and
    its step ledger are the durable state (``bambu-ams-behavior`` restart-durability): the episode is
    re-spawned on the row and registered in the same synchronous stretch, and it decides from the
    first LIVE reading (:func:`_refill_episode` with no trigger) — a paused print is refilled and
    resumed (T3), a print running on air is paused first (T4) — waiting, never resending, on a
    command its log holds pending. Never raises."""
    from backend.app.core.tasks import spawn_background_task

    try:
        task = spawn_background_task(
            _refill_episode(printer_id, trigger=None, incident_id=incident_id), name=f"toolhead-refill-p{printer_id}"
        )
        printer_incidents.register_driver(printer_id, task, incident_id=incident_id)
        logger.info("spool_recovery: startup — printer %s toolhead refill %s re-entered", printer_id, incident_id)
    except Exception:  # noqa: BLE001 — startup hygiene must never block the lifespan
        logger.exception("spool_recovery: re-entering refill %s failed for printer %s", incident_id, printer_id)


def _recorded_candidates(full_codes: str | None) -> frozenset[FaultCandidate]:
    """The actionable fault candidates a row's RECORDED full codes
    (``printer_incident.hms_full_codes``) classify to.

    A startup re-entry meets a wire with no actionable code left — a CONTINUE, or the
    driver's own stop, emptied it — while the incident's shape (which side faulted, a
    latched pull-back) is a fact about the fault the row opened on. The firmware's own
    identifiers were recorded then, so the fault is re-read from them through the one
    reader of a recorded code (``hms_errors.classify_full_code``), never re-guessed — the
    actionable ones, as the live entry gate would have kept them. An empty set when nothing
    was recorded or nothing classifies: the caller's fields then speak alone, and no side
    is claimed (:attr:`RecoveryIncident.extruder_side_only` None — nothing is parked).
    Applied on EVERY re-entry whose wire names no actionable fault — the restart
    continuation's and the mid-change re-entry's alike (coordinator ruling 2026-09-29).
    """
    recorded = (classify_full_code(code) for code in (full_codes or "").split(","))
    return frozenset(c for c in recorded if c is not None and c.fault_class in ACTIONABLE_CLASSES)


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

    One incident is not the wire's to decide: one whose log holds the restart stop
    (:attr:`_RecoveryEvidence.restart_stop`). Its job was ended by the driver itself, so
    it re-enters on the row's own facts and the driver resumes the restart continuation
    (a deploy mid-restart must not leave the stalled filament in the tube).
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
            item_id = item.id if item is not None else None
            if evidence.restart_stop is not None:
                # The driver had STOPPED this job to restart it, and the restart of this
                # process interrupted the continuation. The job is over: its terminal wiped
                # the HMS list, so the wire has nothing left to re-classify, and its unit is
                # no longer printing, so none resolves. The ROW's facts are the incident —
                # its fault (the codes it recorded, through the taxonomy), its slot, its
                # unit — and the driver picks the continuation up from the log
                # (:func:`_drive_recovery`), whatever of it is left.
                candidates = _recorded_candidates(row.hms_full_codes)
                kind, external = row.kind, False
                code, fingerprint = row.code, row.codes
                tray = row.slot_global_tray
                job_id, item_id = row.job_id, row.item_id
                escalate_reason = None
            elif fault_class is None:
                # No actionable candidate: the row's facts are the evidence left — its
                # recorded codes through the taxonomy for the fault's shape (which side, a
                # latched pull-back), its fields for the rest.
                candidates = _recorded_candidates(row.hms_full_codes)
                kind, external = row.kind, False
                code, fingerprint = row.code, row.codes
                tray = row.slot_global_tray
                if incident_resolution.path_quiet(state) and evidence.pending_command is None:
                    # A quiet path is not "fine" mid-swap — but a log that ends in a command
                    # the AMS has not run is the driver's still (item 2 of 2026-10-10): that
                    # row re-enters the machine below, whose round top waits for the command.
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
                if evidence.swap_committed and row.slot_global_tray is not None:
                    # Past the swap-commit boundary the live feeder is the farm's OWN motion —
                    # an unload, a replacement it loaded, a queued load that ran while the farm
                    # was down — so it no longer names the jammed tray: the ROW's recorded slot
                    # does (it was opened on it). Re-resolving it from ``tray_now`` would hand
                    # the re-entered driver its own replacement as the "jammed" spool, and its
                    # resume read would park and page it.
                    tray = row.slot_global_tray
                if _outranks(fault_class, row.kind):
                    # The live fault OUTRANKS the row's reading: re-classify the row, the
                    # entry gate's own upgrade rule. The kind of the hold is the ROW's fact —
                    # ``_escalate`` pages it, the hourly reminder nags it and the resolution
                    # table closes it by it — so a re-entry that acted on the live kind while
                    # the row kept the milder one would page one fault and hold another.
                    upgraded = await printer_incidents.upgrade(
                        db,
                        row.id,
                        kind=kind,
                        code=code,
                        codes=fingerprint,
                        slot_global_tray=tray,
                        hms_full_codes=_recorded_words(candidates),
                    )
                    if upgraded is None:
                        return None  # closed between the read and the re-classification
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
                item_id=item_id,
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

    The single shared gate for both lanes below — and the 2026-08-09 widening of them:
    the old gate was "a still-``printing`` FARM unit holding
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


# --- K9: THE resume of a paused print (2026-10-10) ------------------------------------
#
# ONE body every resume of a PAUSED print publishes through — the printer card's Resume and the
# printers page's bulk resume (N independent route calls), the HMS dialog's resume buttons, the
# runout refill lane and the repaired-path self-heal — so no resume path bypasses the toolhead
# refill. It generalises the shared evidence-lane body it replaced (``_resume_after_evidence``,
# deleted with it — every caller moved). The operator requirement it enforces, verbatim (Raymond
# 2026-10-10): "when i click resume there MUST be filament loaded. It's not the users job to know
# whether or not there's filament in there, the fact that it's unloaded is the farms POOR auto
# recovery mechanics".
#
# OUTSIDE it, each with its reason (``test_code_quality.TestResumeOwnership``): the release
# ladder's verbs (``_LEVERS``, read by ``_read_after``) — releases INSIDE the driver's own
# segment, where the driver itself reads what the wire answered; the power-loss prompt's answer
# (``pause_recovery`` — the firmware's own post-reboot recovery, ``0300_8007`` accepts the plain
# resume); the plate-check presses (``pause_recovery`` — before the first layer, where the start
# block loads); and CONTINUE / RETRY (``ams_control resume`` — a filament-change release: the
# firmware re-feeds the change it holds).

# Who resumes — every actor is trigger T3 (a person's resume, or the farm resuming on a person's
# behalf). ``operator``: a person's click is its own evidence — no settle, no confirm wait (the HTTP
# request answers at once). ``refill_lane`` / ``repair``: the farm resuming on evidence the lane
# re-asks before AND after a settle (a resume published on the edge itself races the firmware's own
# settle and is rejected), with RUNNING confirmed — the runout refill (006-H2S 2026-07-26) and the
# repaired filament path (003-H2S 2026-09-11).
ResumeActor = Literal["operator", "refill_lane", "repair"]

# Why a resume of a paused print is refused — CLOSED, ``schemas.printer.ResumeRefusalReason`` (the
# DTO layer's one spelling, which the route's 409 validates against): the verb's own two
# (``not_paused``, ``farm_acting``: a recovery driver is live and owns the printer's next motion),
# then the K7 reasons whose resume would print air (:data:`_RESUME_DECISION`'s refusal rows).

# What the verb does for a K7 reason: publish the resume, refill first, or refuse (the refusal IS
# the K7 reason, so a 409 names the fact that decided it).
ResumeDecision = Literal["resume", "refill", "maintenance", "unknown", "command_pending", "physical"]

# TOTAL over :data:`RefillReason` — a reason without a row RAISES at the lookup
# (:func:`_resume_decision`, the ``_LEVERS`` idiom), so a new K7 reason cannot reach a resume by
# default. Read for every trigger that ends in a resume (T3, T4).
_RESUME_DECISION: dict[RefillReason, ResumeDecision] = {
    "owed": "refill",
    "fed": "resume",
    # The FIRMWARE feeds the toolhead itself on this resume — no air:
    "change_in_flight": "resume",  # the print's own filament change is the head; the resume re-runs it
    "runout_demand": "resume",  # the firmware loads the SAME slot it asks for (the refill lane's path)
    "power_loss_prompt": "resume",  # the resume answers the prompt — the firmware's power-loss recovery
    "before_first_layer": "resume",  # the start block's own load is still to come
    "last_layer": "resume",  # T4's: the end-of-print retract, not air
    "eject_sweep": "resume",  # the farm's own sweep is filament-less by design
    # The farm will not load and the toolhead is EMPTY — never resume onto air:
    "maintenance": "maintenance",
    "unknown": "unknown",
    "command_pending": "command_pending",
    "physical": "physical",  # T2's alone — a T3 / T4 verdict never carries it
}

# The sentence a refusal answers with (sys-admin register: the fact, then the action). The 409's
# ``message``; a client keys its own copy off the ``reason`` token.
_RESUME_REFUSAL_COPY: dict[ResumeRefusalReason, str] = {
    "not_paused": "Print is not paused.",
    "farm_acting": "Recovery in progress on this printer. Resume refused.",
    "maintenance": "Toolhead empty. Maintenance mode: the farm loads nothing. Load a slot, then resume.",
    "unknown": "Toolhead not readable: no live report from the printer. Resume refused.",
    "command_pending": (
        "Toolhead empty. The AMS still holds the farm's queued command; the farm refills the toolhead when it runs."
    ),
    "physical": "Toolhead empty. A physical fault holds the filament path. Clear it and load a slot, then resume.",
}


def _resume_decision(reason: RefillReason) -> ResumeDecision:
    try:
        return _RESUME_DECISION[reason]
    except KeyError:
        raise LookupError(
            f"spool_recovery: no resume decision for refill reason {reason!r} — every reason needs its own row"
        ) from None


@dataclass(frozen=True)
class ResumeSent:
    """ONE resume went out. ``sent`` is its frame's handle — the plain resume's, or the dialog
    button the caller pressed — which the caller reads the firmware's ACK by; ``confirmed`` whether
    the printer reported RUNNING inside :data:`_RUNOUT_RESUME_CONFIRM_S` (the evidence lanes ask;
    ``None``: a person's click, not awaited)."""

    sent: SentCommand
    confirmed: bool | None = None


@dataclass(frozen=True)
class RefillStarted:
    """The toolhead reads EMPTY and a refill is owed: the refill runs as the printer's REGISTERED
    driver on ``incident_id`` (the open AMS row, or the ``toolhead_refill`` row it opened) and
    publishes the resume itself once the load reached the toolhead (:func:`_refill_episode`).
    ``slot`` is what it loads first — the job's last feeder when it is present and not spent — or
    ``None`` when the swap's own selection decides."""

    incident_id: int
    slot: str | None


@dataclass(frozen=True)
class ResumeRefused:
    """Nothing was sent: ``reason`` (closed) and the sentence that says so."""

    reason: ResumeRefusalReason
    message: str


@dataclass(frozen=True)
class ResumeNotSent:
    """No resume went out: no client (``not_connected``) or the publish did not go out (``not_sent``)."""

    reason: Literal["not_connected", "not_sent"]


@dataclass(frozen=True)
class ResumeStoodDown:
    """An evidence lane's evidence no longer held — asked before and after the settle — or the
    printer was resumed meanwhile. Nothing was sent; ``why`` is the log's word for it."""

    why: str


ResumeVerdict = ResumeSent | RefillStarted | ResumeRefused | ResumeNotSent | ResumeStoodDown


def _refused(reason: ResumeRefusalReason) -> ResumeRefused:
    return ResumeRefused(reason=reason, message=_RESUME_REFUSAL_COPY[reason])


def _plain_resume(client: BambuMQTTClient) -> SentCommand | None:
    """THE plain resume of a paused print (``print.resume``, BambuStudio's ``command_task_resume``)
    — the K9 body's publisher, and the frame the refill driver resumes with once its load reached
    the toolhead. ``None`` when the publish did not go out."""
    if not client.resume_print():
        return None
    return SentCommand(command="resume", sequence_id=client.last_sent_sequence_id("resume"))


async def _settle_evidence(
    printer_id: int, ready: Callable[[], Awaitable[bool]], *, name: str
) -> ResumeStoodDown | None:
    """The evidence lanes' SHAPE, the part that must not be re-derived per lane: the lane's own
    evidence asked, a settle, the printer re-read (somebody may have resumed it), the evidence
    asked AGAIN — the world moves. ``None`` to go on; else the stand-down, logged."""
    if not await ready():
        return ResumeStoodDown("no evidence")
    # Let the printer register what just happened: a resume sent on the edge itself races the
    # firmware's own tray-state settle and is rejected.
    await asyncio.sleep(_RUNOUT_RESUME_SETTLE_S)
    st = _get_state(printer_id)
    if st is not None and getattr(st, "state", None) == "RUNNING":
        logger.info(
            "spool_recovery: printer %s already RUNNING after the %s (operator resumed) — auto-resume stood down",
            printer_id,
            name,
        )
        return ResumeStoodDown("already running")
    if not await ready():
        logger.info(
            "spool_recovery: printer %s %s auto-resume stood down after settle (state/wire/hold changed)",
            printer_id,
            name,
        )
        return ResumeStoodDown("evidence changed")
    return None


async def resume_paused_print(
    printer_id: int,
    *,
    actor: ResumeActor,
    ready: Callable[[], Awaitable[bool]] | None = None,
    press: Callable[[BambuMQTTClient], SentCommand | None] | None = None,
) -> ResumeVerdict:
    """Resume this printer's PAUSED print — refilling an EMPTY toolhead first. THE one body (K9).
    Never raises (an assist lane must never crash its caller; a failure is logged and answers
    ``not_sent``).

    At publish time it asks K7 (:func:`refill_verdict.refill_owed`, trigger ``T3``) and acts on the one decision
    table (:data:`_RESUME_DECISION`):

    * ``resume`` — a fed toolhead, or the firmware feeding it itself: ONE resume, the dialog button
      ``press`` names (the HMS route's resume buttons) or the plain resume (:func:`_plain_resume`);
      an evidence lane (``ready`` given) confirms RUNNING.
    * ``refill`` — the toolhead is EMPTY: the refill runs as the printer's REGISTERED driver of an
      open row (:func:`_start_refill`, K10) and this answers AT ONCE, ``RefillStarted`` — the refill
      can take minutes, and its outcome reaches a person through the status projection and, on a
      failure, a page naming the slot and what the AMS answered.
    * a refusal — never a resume onto air: ``ResumeRefused`` with the reason, nothing sent.

    Before it: the evidence lanes' settle (:func:`_settle_evidence`), a client, a LIVE reading of a
    PAUSEd print, and no driver live — a live driver owns the printer's next motion.
    """
    name = "refill" if actor == "refill_lane" else "path repair"
    try:
        if ready is not None:
            stood = await _settle_evidence(printer_id, ready, name=name)
            if stood is not None:
                return stood
        client = printer_manager.get_client(printer_id)
        if client is None:
            return ResumeNotSent("not_connected")
        st = _get_state(printer_id)
        if st is None or not live_reading.reads_live(st):
            return _refused("unknown")
        if _live_state(st) != "PAUSE":
            return _refused("not_paused")
        if printer_incidents.driver_live(printer_id):
            return _refused("farm_acting")
        verdict = refill_verdict.refill_owed(printer_id, st, trigger="T3", peaks=_job_peaks(printer_id))
        decision = _resume_decision(verdict.reason)
        match decision:
            case "resume":
                pass
            case "refill":
                return await _start_refill(printer_id, st, trigger="T3")
            case "maintenance" | "unknown" | "command_pending" | "physical":
                logger.info(
                    "spool_recovery: printer %s resume (%s) refused — the toolhead reads empty and the farm will not "
                    "refill it (%s)",
                    printer_id,
                    actor,
                    verdict.reason,
                )
                return _refused(decision)
            case _:
                assert_never(decision)
        sent = (press or _plain_resume)(client)
        if sent is None:
            logger.info(
                "spool_recovery: printer %s resume (%s) send did not go out (offline?) — nothing sent",
                printer_id,
                actor,
            )
            return ResumeNotSent("not_sent")
        logger.info(
            "spool_recovery: printer %s resume (%s) published — the toolhead reads %s",
            printer_id,
            actor,
            verdict.reason,
        )
        if ready is None:
            return ResumeSent(sent)
        confirmed = await printer_manager.await_state(
            printer_id, {"RUNNING"}, _RUNOUT_RESUME_CONFIRM_S, poll_interval_s=_POLL_INTERVAL_S
        )
        if not confirmed:
            logger.info(
                "spool_recovery: printer %s did not reach RUNNING within %.0fs after the %s resume — "
                "standing aside (no retry); the escalation guidance stands",
                printer_id,
                _RUNOUT_RESUME_CONFIRM_S,
                name,
            )
        return ResumeSent(sent, confirmed)
    except Exception:  # noqa: BLE001 — a resume lane must never crash its caller
        logger.exception("spool_recovery: %s resume failed for printer %s", name, printer_id)
        return ResumeNotSent("not_sent")


def _job_peaks(printer_id: int) -> JobPeaks | None:
    """The client's one peaks reader for the refill verdict (``refill_verdict.refill_owed``'s
    ``peaks``): ``None`` with no client registered — the verdict then decides nothing."""
    client = printer_manager.get_client(printer_id)
    return client.job_peaks() if client is not None else None


# --- K10: every refill runs as a REGISTERED driver of an OPEN row ----------------------


async def _refill_row(db: AsyncSession, printer_id: int, state) -> tuple[PrinterIncident | None, bool]:
    """The OPEN row a refill on this printer runs on (K10): the printer's open AMS row (jam /
    physical / runout — the refill re-enters it, its steps on its ledger), else its open
    ``toolhead_refill`` row (a person's next Resume after a failed refill), else a NEW
    ``toolhead_refill`` row, ``recovering`` — the job the printer names, its farm unit (none for a
    foreign print), and the job's last feeder (``last_loaded_tray``, the wire's per-job record) as
    its slot. ``(row, opened)`` — ``opened`` True when THIS call opened it; ``(None, False)`` when
    another lane won the open race."""
    ams = await printer_incidents.get_open(db, printer_id, kinds=AMS_FAULT_KINDS)
    if ams is not None:
        return ams, False
    existing = await printer_incidents.get_open(db, printer_id, kinds=REFILL_KINDS)
    if existing is not None:
        return existing, False
    job_id = (getattr(state, "subtask_id", None) or "").strip()
    item = await _resolve_farm_item(db, printer_id, job_id)
    row = await printer_incidents.open_new(
        db,
        printer_id=printer_id,
        job_id=job_id,
        item_id=item.id if item is not None else None,
        kind=KIND_TOOLHEAD_REFILL,
        code="",
        codes="",
        slot_global_tray=tray_fields.valid_feeder(getattr(state, "last_loaded_tray", None)),
        status=STATUS_RECOVERING,
    )
    return row, row is not None


async def _incident_of_row(db: AsyncSession, row: PrinterIncident, state) -> RecoveryIncident:
    """The driver context of an open row the refill runs on — through THE one construction
    (:func:`_build_incident`), from the ROW's facts: its recorded codes through the taxonomy, its
    kind, slot, job and unit."""
    from backend.app.models.printer import Printer

    settings = await _read_settings(db)
    printer = await db.get(Printer, row.printer_id)
    return _build_incident(
        state,
        _recorded_candidates(row.hms_full_codes),
        incident_id=row.id,
        printer_id=row.printer_id,
        job_id=row.job_id,
        settings=settings,
        item_id=row.item_id,
        kind=row.kind,
        code=row.code,
        fingerprint=row.codes,
        tray=row.slot_global_tray,
        external=printer_incidents.row_external(row),
        printer_name=(printer.name if printer else None) or f"printer {row.printer_id}",
        fallback_code=row.code or None,
    )


async def _start_refill(printer_id: int, state, *, trigger: RefillTrigger) -> RefillStarted | ResumeRefused:
    """Open or re-enter the row (:func:`_refill_row`), name the slot the refill loads first, then
    spawn the refill (:func:`_refill_episode`) and register it as the printer's driver in ONE
    synchronous stretch — after the last await, so a second click that raced the first finds the
    driver live (``farm_acting``) instead of spawning a second one."""
    from backend.app.core.database import async_session
    from backend.app.core.tasks import spawn_background_task

    async with async_session() as db:
        row, _opened = await _refill_row(db, printer_id, state)
        if row is None:
            return _refused("farm_acting")
        incident = await _incident_of_row(db, row, state)
    first = await _refill_last_feeder(incident, state)
    if printer_incidents.driver_live(printer_id):
        return _refused("farm_acting")
    task = spawn_background_task(
        _refill_episode(printer_id, trigger=trigger, incident_id=incident.incident_id),
        name=f"toolhead-refill-p{printer_id}",
    )
    printer_incidents.register_driver(printer_id, task, incident_id=incident.incident_id)
    slot = _slot_name(first) if first is not None else None
    logger.info(
        "spool_recovery: printer %s toolhead EMPTY at the resume (%s) — refilling on %s incident %s%s, then resuming",
        printer_id,
        trigger,
        incident.kind,
        incident.incident_id,
        f" from {slot}" if slot else "",
    )
    return RefillStarted(incident_id=incident.incident_id, slot=slot)


async def _stop_air_print(printer_id: int) -> bool:
    """Pause a print running onto an EMPTY toolhead (T4: the first push that showed it — Raymond
    2026-09-17, "If nothing is feedingg after 20secs the print is ruined already as the printer
    won't go back to reprint the past 20seconds that had empty filament") and wait for the PAUSE to
    land, within one step window. THE one pause publisher of the refill (the release frames' pin):
    the T4 episode's first act, and the refill driver's answer to its own resume reading RUNNING
    with nothing fed. False when the pause did not go out or did not land — a print still running
    is re-detected on the next push."""
    from backend.app.core.database import async_session

    client = printer_manager.get_client(printer_id)
    if client is None or not client.pause_print():
        logger.warning("spool_recovery: printer %s is printing air and the pause did not go out", printer_id)
        return False
    async with async_session() as db:
        settings = await _read_settings(db)
    landed = await printer_manager.await_state(
        printer_id, {"PAUSE"}, settings.step_timeout_s, poll_interval_s=_POLL_INTERVAL_S
    )
    logger.info(
        "spool_recovery: printer %s RUNNING with nothing at the toolhead — paused (%s)",
        printer_id,
        "landed" if landed else "not landed",
    )
    return landed


async def _settle_pending(printer_id: int, pending: printer_incidents.PendingCommand) -> None:
    """Answer the farm's pending motion command the wire now shows settled (:func:`refill_verdict.pending_settles`)
    — through the log's one writer (``EvidenceLog.answer``), so the projection re-derives no pending
    command. Re-reads the ledger (``from_row``) and settles the step only while it still pends: the
    detector saw it on a push, and the world moves."""
    from backend.app.core.database import async_session

    async with async_session() as db:
        evidence = await _RecoveryEvidence.from_row(db, pending.incident_id)
    still = evidence.pending_command
    if still is None or still.seq != pending.seq:
        return
    st = _get_state(printer_id)
    if st is None or not live_reading.reads_live(st):
        return
    settles = refill_verdict.pending_settles(still.entry(), tray_fields.toolhead_feed(st))
    if settles is None:
        return
    await evidence.answer(still.seq, settles)
    logger.info(
        "[spool_recovery] printer %s: the AMS ran the farm's queued %s (step %s) after the hand-over — answered %s",
        printer_id,
        still.command,
        still.seq,
        settles,
    )


async def _refill_episode(
    printer_id: int,
    *,
    trigger: RefillTrigger | None,
    incident_id: int | None = None,
    settle: printer_incidents.PendingCommand | None = None,
) -> None:
    """THE refill driver (K10): one episode on one open row, spawned already REGISTERED as the
    printer's driver (:func:`_start_refill`, :func:`_sample_toolhead`, the startup re-entry). Never
    raises; releases its liveness slot on exit and hands a RUNNING level to the closers (the
    ``_run_recovery`` handover: the running edge stood aside while this driver lived).

    * ``settle`` — the farm's pending command the detector saw RUN (D1 / D2): answered first.
    * the row — ``incident_id`` (the verb opened or re-entered it, or the detector's pending command
      names it), else found or opened now (:func:`_refill_row`): the durable promise, open before
      anything is sent.
    * ``T4`` — the print is RUNNING onto air: pause it (:func:`_stop_air_print`); a pause that does
      not land ends the episode and closes a row it opened (the next push re-detects a print still
      running).
    * ``None`` — the startup re-entry (:func:`_reenter_refill_incident`): the first LIVE reading
      decides — RUNNING with K7(T4) owed is paused first (T4), a PAUSE is T3; anything else is the
      row's closers' (a print running fed, a job over).
    * then :func:`_refill_then_resume`.
    """
    pid = printer_id
    try:
        from backend.app.core.database import async_session

        if settle is not None:
            await _settle_pending(pid, settle)
        st = _get_state(pid)
        async with async_session() as db:
            row, opened = (
                (await db.get(PrinterIncident, incident_id), False)
                if incident_id is not None
                else await _refill_row(db, pid, st)
            )
            if row is None or row.resolved_at is not None:
                logger.info(
                    "spool_recovery: printer %s refill episode (%s) has no open row — nothing to do", pid, trigger
                )
                return
            incident = await _incident_of_row(db, row, st)
            evidence = await _RecoveryEvidence.from_row(db, row.id)
        if trigger == "T4" and not await _stop_air_print(pid):
            # The row is opened BEFORE the pause — the promise the AMS entry reads covers the whole
            # episode — so a pause that never lands closes the row it opened as transient (it never
            # held the printer): a ``recovering`` promise nobody keeps would hold the printer for
            # good. The next push re-detects a print still running on air.
            if opened:
                await _close_incident(incident, status=STATUS_ABORTED, source=None)
            return
        try:
            if trigger is None:
                trigger = await _reentered_trigger(incident)
                if trigger is None:
                    await _close_answered_reentry(incident)
                    return
            await _refill_then_resume(incident, evidence, trigger=trigger)
        except _PrinterOffline as gap:
            logger.warning(
                "spool_recovery: printer %s refill episode — the printer stayed off its session past %.0fs",
                pid,
                gap.bound_s,
            )
            await _escalate(incident, "printer_offline", evidence=evidence)
    except Exception:  # noqa: BLE001 — a driver must never crash the event loop
        logger.exception("spool_recovery: refill episode crashed for printer %s", pid)
    finally:
        task = asyncio.current_task()
        if task is not None:
            printer_incidents.release_driver(pid, task)
        st = _get_state(pid)
        if st is not None and live_reading.reads_live(st) and _live_state(st) == "RUNNING":
            await on_observed_running(pid)


async def _close_answered_reentry(incident: RecoveryIncident) -> None:
    """A re-entered refill whose first LIVE reading owes nothing (a print running fed, a job over):
    the rule table's ``startup`` occasion, asked of THAT reading, closes the row — the startup
    sweep ran before the printer reported, and a ``recovering`` row left driverless is skipped by
    every sweep (``incident_resolution.driver_owns``) and holds the printer off dispatch. A row the
    table keeps (a reading it cannot place) is escalated to a person, never left a driverless
    promise."""
    from backend.app.core.database import async_session

    async with async_session() as db:
        row = await db.get(PrinterIncident, incident.incident_id)
        if row is None or row.resolved_at is not None:
            return
        verdict = incident_resolution.resolve(
            row,
            "startup",
            Context(
                state=_get_state(incident.printer_id),
                ledger=ledger,
                driver_live=False,
                command_pending=_pending_on(row),
            ),
        )
        if not verdict.close:
            await printer_incidents.mark_escalated(db, row.id)
            logger.info(
                "spool_recovery: printer %s re-entered refill %s owes no refill and stands (%s) — a person's hold now",
                incident.printer_id,
                row.id,
                verdict.evidence,
            )
            return
        await printer_incidents.close(db, row.id, status=STATUS_RESOLVED, source=verdict.source)
        await printer_incidents.clear_hold_projection(db, row.item_id)
    logger.info(
        "spool_recovery: printer %s re-entered refill %s closed — %s", incident.printer_id, row.id, verdict.evidence
    )


async def _reentered_trigger(incident: RecoveryIncident) -> RefillTrigger | None:
    """The trigger a re-entered refill episode runs under, from its first LIVE reading: PAUSE → T3
    (every toolhead row ends in a resume); RUNNING with K7(T4) owed → T4, paused first; anything else
    (a print running fed, a job over, a printing-air reading K7 excludes) → ``None``: the row's own
    closers answer it."""
    st, _waited = await _live_reading(incident)
    match _live_state(st):
        case "PAUSE":
            return "T3"
        case "RUNNING" if refill_verdict.refill_owed(
            incident.printer_id, st, trigger="T4", peaks=_job_peaks(incident.printer_id)
        ).owed:
            return "T4" if await _stop_air_print(incident.printer_id) else None
        case _:
            return None


async def _refill_then_resume(
    incident: RecoveryIncident, evidence: _RecoveryEvidence, *, trigger: RefillTrigger
) -> None:
    """The episode's contract on its row: wait for a command the AMS still holds on it (a restarted
    episode's own queued load — never send behind it), ask K7 at publish time, refill when owed
    (:func:`_refill_toolhead`, K8), and for T3 / T4 publish ONE resume and read it.

    * a failed refill (no spool, or none reached the toolhead) escalates the row — a page naming the
      slot and what the AMS answered — and nothing is resumed;
    * a refusal at publish (the world moved: maintenance mode, a queued command, no live reading):
      T2 leaves the print as it is; T3 / T4 hand it to a person (``toolhead_refill_withheld``);
    * T2 (D1) never resumes: the print stays PAUSED, now with filament — the human's hold stands;
    * the resume: RUNNING within :data:`_RUNOUT_RESUME_CONFIRM_S` (else ``toolhead_resume_unconfirmed``)
      with the toolhead FED closes a ``toolhead_refill`` row (``refill_resumed``, the farm's own
      recovery); RUNNING on air is paused again at once and handed to a person — events, never a
      loop: a paused print cannot re-trigger the RUNNING detector."""
    pid = incident.printer_id
    pending = evidence.pending_command
    if pending is not None:
        waited = await _await_pending_command(incident, pending, evidence=evidence)
        match waited:
            case "ran" | "undecidable" | "void":
                pass
            case "abort":
                await _abort(incident, restored=True)
                return
            case "handover":
                _hand_over(incident)
                return
            case _:
                assert_never(waited)
    st, _waited = await _live_reading(incident)
    owed = refill_verdict.refill_owed(pid, st, trigger=trigger, peaks=_job_peaks(pid))
    result: RefillLoaded | None = None
    decision = _resume_decision(owed.reason)
    match decision:
        case "refill":
            refill = await _refill_toolhead(incident, evidence=evidence)
            match refill:
                case "abort":
                    # The operator took the printer during the refill: the stamp stays, the farm chose
                    # the feeder (``_give_up``'s rule).
                    await _abort(incident, restored=True)
                    return
                case "handover":
                    _hand_over(incident)
                    return
                case RefillLoaded():
                    result = refill
                case RefillFailed() | RefillNoCandidate():
                    await _escalate(incident, "toolhead_refill_failed", refill=refill, evidence=evidence)
                    return
                case _:
                    assert_never(refill)
        case "resume":
            pass
        case "maintenance" | "unknown" | "command_pending" | "physical":
            logger.info("spool_recovery: printer %s refill (%s) withheld at publish — %s", pid, trigger, owed.reason)
            if trigger != "T2":
                await _escalate(incident, "toolhead_refill_withheld", evidence=evidence)
            return
        case _:
            assert_never(decision)
    if trigger == "T2":
        logger.info(
            "spool_recovery: printer %s incident %s — the farm's command ran after the hand-over; the toolhead %s; "
            "the print stays PAUSED",
            pid,
            incident.incident_id,
            f"is refilled from tray {result.tray}" if result is not None else f"reads {owed.reason}",
        )
        return
    client = printer_manager.get_client(pid)
    sent = _plain_resume(client) if client is not None else None
    if sent is None or not await _await_live_state(incident, frozenset({"RUNNING"}), _RUNOUT_RESUME_CONFIRM_S):
        await _escalate(incident, "toolhead_resume_unconfirmed", refill=result, evidence=evidence)
        return
    st, _waited = await _live_reading(incident)
    if tray_fields.toolhead_feed(st).active.kind not in ("fed", "external"):
        await _stop_air_print(pid)
        tray = result.tray if result is not None else None
        not_there = RefillFailed(tray=tray, command="load", answer="not_at_toolhead")
        await _escalate(incident, "toolhead_refill_failed", refill=not_there, evidence=evidence)
        return
    if incident.kind == KIND_TOOLHEAD_REFILL:
        await _close_incident(incident, status=STATUS_RESOLVED, source=RESOLVE_REFILL_RESUMED)
        await _clear_hold_projection_of(incident)
    logger.info(
        "spool_recovery: printer %s RESUMED with the toolhead fed (%s, incident %s, %s)",
        pid,
        trigger,
        incident.incident_id,
        result or "fed at publish",
    )


async def _clear_hold_projection_of(incident: RecoveryIncident) -> None:
    """Drop the closed row's hold token from its unit (``printer_incidents.clear_hold_projection``,
    the one clear). Best-effort."""
    from backend.app.core.database import async_session

    try:
        async with async_session() as db:
            await printer_incidents.clear_hold_projection(db, incident.item_id)
    except Exception:  # noqa: BLE001 — bookkeeping must not undo a successful resume
        logger.exception("spool_recovery: hold-token clear failed for printer %s", incident.printer_id)


def _resume_confirmed(verdict: ResumeVerdict, printer_id: int, name: str) -> bool:
    """An evidence lane's reading of the verb: True only on a CONFIRMED RUNNING — the lane owns
    what that success means for its own bookkeeping. Every other verdict stands the lane aside (no
    retry, no quarantine, no stamp, its guidance untouched), logged where the verb did not."""
    match verdict:
        case ResumeSent(confirmed=confirmed):
            return confirmed is True
        case RefillStarted(incident_id=incident_id):
            logger.info(
                "spool_recovery: printer %s %s resume — the toolhead reads empty; the refill on incident %s resumes",
                printer_id,
                name,
                incident_id,
            )
            return False
        case ResumeRefused(reason=reason):
            logger.info("spool_recovery: printer %s %s auto-resume refused (%s)", printer_id, name, reason)
            return False
        case ResumeNotSent(reason=reason):
            logger.info(
                "spool_recovery: printer %s %s resume not sent (%s) — auto-resume stands aside, escalation "
                "guidance stands",
                printer_id,
                name,
                reason,
            )
            return False
        case ResumeStoodDown():
            return False
        case _:
            assert_never(verdict)


async def _resume_after_refill(printer_id: int, slot: tuple[int, int] | None) -> bool:
    """Resume a runout-held print once the wire says the filament is back.

    006-H2S 2026-07-26: the runout escalation leaves the print PAUSEd for a same-slot
    refill, and the refill itself is the operator's "go" — but the print then sat
    waiting for someone to press Resume. Doctrine rule 1 (minimal human interaction):
    the recoverable half of this state is recoverable without hands.

    ONE evidence behind TWO spawn sources — the ``ams_presence`` presence-GAIN edge
    (a roll went in) and :func:`note_demand_watch`'s wire edges (the firmware stopped
    asking). When only the gain edge existed, the 006 class — a demand
    naming a slot that was ALREADY loaded — could never resume, and auto-resume had
    never fired in production at all.

    The resume is THE resume (:func:`resume_paused_print`, K9 — its settle, its re-ask, its one
    publish and its confirm); this owns the evidence (``runout_auto_resume_enabled`` +
    :func:`_resume_ready`) and the success bookkeeping. The open runout row is K7's
    ``runout_demand``: the firmware loads the SAME slot on the resume, so no refill goes out
    (invariant 9). Returns True only on a confirmed RUNNING.
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
            # the log, and it is the first ask — the verb short-circuits before its
            # settle — so this is ONE line per spawn, not one per poll.
            logger.info(
                "spool_recovery: printer %s in maintenance mode — refill seen, not resuming; "
                "resume on the printer or release the hold",
                printer_id,
            )
            return False

        if printer_incidents.job_pause_held(printer_id):
            # The printer ALSO paused this job at its plate check. A refill resume would
            # answer that question too, onto the plate it refused with no re-check — the one
            # predicate every job-resuming lane reads. The runout hold stays open: the
            # plate-check episode's own press (``pause_recovery``) or, in its fallback, a
            # human's resume answers both.
            logger.info(
                "spool_recovery: printer %s is held at a job pause (the printer's plate check) — refill seen, "
                "not resuming; the plate-check episode answers that pause",
                printer_id,
            )
            return False

        from backend.app.core.database import async_session

        async with async_session() as db:
            if not await _read_bool(db, "runout_auto_resume_enabled", _DEFAULT_RUNOUT_AUTO_RESUME):
                return False
            return await _resume_ready(db, printer_id, slot) == "ready"

    verdict = await resume_paused_print(printer_id, actor="refill_lane", ready=_ready)
    if not _resume_confirmed(verdict, printer_id, "refill"):
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
    the fault interrupted, and the rule table's own repair evidence still holds. The resume
    is THE resume (:func:`resume_paused_print`, K9): a toolhead that reads EMPTY at publish —
    a farm pull-back the AMS ran after the operator's load (011/014-H2S 2026-10-09/10) — is
    refilled on this row before it resumes, never resumed onto air. On a confirmed RUNNING
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
            # Same rule as the refill lane: the printer paused this job at its plate check,
            # and a repair resume would answer that pause with no re-check.
            logger.info(
                "spool_recovery: printer %s is held at a job pause (the printer's plate check) — repair seen, "
                "not resuming; the plate-check episode answers that pause",
                printer_id,
            )
            return False

        from backend.app.core.database import async_session

        async with async_session() as db:
            row = await db.get(PrinterIncident, incident_id)
            if row is None or row.resolved_at is not None:
                return False
            if same_job(getattr(st, "subtask_id", None), row.job_id) != "same":
                return False
            # The same question the sweep asks, asked of the same table: does this
            # row's own evidence still hold? A row of any OTHER class answers False
            # here by construction — the printer is PAUSEd, which is every other
            # class's "still held" reading — so the class test is the table's, not a
            # second copy of it spelled in this lane.
            return incident_resolution.resolve(
                row,
                "sweep_tick",
                Context(
                    state=st,
                    ledger=ledger,
                    driver_live=printer_incidents.driver_live(printer_id),
                    command_pending=_pending_on(row),
                ),
            ).close

    verdict = await resume_paused_print(printer_id, actor="repair", ready=_ready)
    if not _resume_confirmed(verdict, printer_id, "path repair"):
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
      MEMBERSHIP instead: no ``runout_external`` code standing, no runout;
    * the empty-toolhead detectors (K11, 2026-10-10 — :func:`_sample_toolhead`): LEVELS, not
      edges, so they run on every push, the first after a restart included — D1, a farm
      command that RAN after the hand-over on a PAUSEd print (refill, stay paused); D2, a
      print RUNNING on nothing fed (pause, refill, resume).

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

        # THE one writer of the motion ledger. This sampler is the only thing in the
        # farm that sees every push, so it hands each reading to the ledger that owns
        # the repair evidence — the completed-load EDGE (session-scoped: the epoch
        # goes with it, because a reconnect re-seeds every wire fact at once) and the
        # "a print FED through the path here" sighting; whether a driver is live is the
        # ledger's own question (K12), never this caller's. Deliberately BEFORE the
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

        # A REBOOT voids the farm's queued command on a row no driver watches (K11): a LEVEL
        # check, on every push — the first after a farm restart included, since the prompt and
        # the pending command are both states, not edges. A live driver voids its own (its wait
        # reads the same prompt); the slot is taken in this synchronous stretch, so the next push
        # cannot spawn a second lane.
        if power_loss_prompt_standing(hms_list) and not printer_incidents.driver_live(printer_id):
            pending = printer_incidents.pending_command(printer_id)
            if pending is not None:
                from backend.app.core.tasks import spawn_background_task

                task = spawn_background_task(
                    _void_pending_after_reboot(printer_id, pending), name=f"void-pending-p{printer_id}"
                )
                printer_incidents.register_driver(printer_id, task, incident_id=pending.incident_id)

        # The K11 detectors (D1 / D2): LEVEL checks, so BEFORE the seed-only return below — a printer
        # printing air when this process first looks is acted on at once.
        _sample_toolhead(printer_id, state, live)

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


def _sample_toolhead(printer_id: int, state, live: str) -> None:
    """The K11 detectors, on one status push (2026-10-10). Sync, DB-free and total — they ride the
    ~1 Hz callback (invariant 10; the caller's guard covers them) — and they read the refill verdict
    (:func:`refill_verdict.refill_owed`, K7), never its predicates. Each spawns :func:`_refill_episode` and
    registers it as the printer's driver in the SAME synchronous stretch (the
    ``pause_recovery._sample_plate_check`` pattern), so the next push finds the driver live and
    spawns no second one. Nothing here decides on elapsed time (Raymond 2026-10-10: "time is not the
    right signal"): every trigger is the wire's own event.

    * **D1 — PAUSE.** An open row's farm command the ledger holds PENDING has RUN
      (:func:`refill_verdict.pending_settles` over the projection and the live ACTIVE extruder) with no driver
      live: the AMS ran it after the hand-over (an accepted pull-back runs on its own ~4.5 min after
      the send — 011-H2S 04:31:27 → 04:36:01, 014-H2S 02:45:58 → 02:50:38). The episode answers the
      step, then refills the now-empty toolhead on that row when K7(T2) owes it; the print STAYS
      paused. A toolhead a HUMAN emptied at the screen with nothing pending is not the farm's: left
      alone.
    * **D2 — RUNNING.** The print runs with nothing fed and K7(T4) owes a refill (no change in flight,
      layer 1 up to below the last, no eject, …): pause, refill, resume — a screen resume onto an empty
      toolhead (T4), or a pending pull-back that drained under a running print after a person's screen
      Retry (T2-running: the step is answered first). A LEVEL, so the first fresh sample after a farm
      restart acts; never twice for one episode, because the registered driver guards it while it
      lives and after it the wire reads fed again (or the print is paused, which D2 never reads)."""
    if printer_incidents.driver_live(printer_id) or not live_reading.reads_live(state):
        return
    pending = printer_incidents.pending_command(printer_id)
    ran = (
        pending is not None
        and refill_verdict.pending_settles(refill_verdict.pending_entry(pending), tray_fields.toolhead_feed(state))
        is not None
    )
    if live == "PAUSE":
        if pending is None or not ran:
            return
        trigger: RefillTrigger = "T2"
    elif live == "RUNNING":
        if not refill_verdict.refill_owed(printer_id, state, trigger="T4", peaks=_job_peaks(printer_id)).owed:
            return
        trigger = "T4"
    else:
        return
    from backend.app.core.tasks import spawn_background_task

    incident_id = pending.incident_id if pending is not None else None
    task = spawn_background_task(
        _refill_episode(printer_id, trigger=trigger, incident_id=incident_id, settle=pending if ran else None),
        name=f"toolhead-refill-p{printer_id}",
    )
    printer_incidents.register_driver(printer_id, task, incident_id=incident_id)
    logger.info(
        "spool_recovery: printer %s %s — %s; the farm %s",
        printer_id,
        "PAUSED and the farm's queued command ran" if trigger == "T2" else "RUNNING with nothing at the toolhead",
        f"pending {pending.name} step {pending.seq} on incident {pending.incident_id}" if pending else "no row",
        "answers it and refills the toolhead" if trigger == "T2" else "pauses, refills and resumes",
    )


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
            await printer_incidents.clear_hold_projection(db, item_id)
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
