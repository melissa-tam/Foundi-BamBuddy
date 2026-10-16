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
    held INSIDE a change (the AMS re-entered one before it ran) it runs at the next release,
    held OUTSIDE one it runs on its own minutes later (011/014-H2S 2026-10-09/10) — either way
    the driver waits for the record's answer and continues the contract (:func:`_observe_command`;
    :func:`_await_pending_command` after a farm restart), and never sends behind a command still
    pending.

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

Every verb the driver sends goes through ``services/printer_action`` as the farm's ACTION on the
row, and what the printer answered is the RECORD's (``command_outcome``'s tables: the ACK, the state
reported, the job's terminal, a reboot, a person's act — 2026-10-11). Events decide first, every
one of them, and a late event is still honoured; the STEP BOUND (:func:`step_bound`, the operator's
``spool_recovery_step_timeout_s``, counted in the printer's online seconds by the record) is the
self-heal fallback of every wait production bounded with it: with no event by then the driver takes
production's next self-heal step — the next rung, the next candidate, the give-up that refills the
toolhead (2026-10-12, Raymond: "if it was self heal, it should stay self heal … not rely on timers
only"). The one wait it never bounds is a command the AMS accepted and has not run
(:func:`_await_pending_command`, 2026-10-10). The driver keeps its own poll loops — each asks
:func:`command_outcome.takeover` on every poll, then the record's answer, then the bound — and
records every lever it pulled and every command
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
:func:`reevaluate_session` (a printer's first fresh report of a session, from
``session_start``), and :func:`clear_on_reinsert` (from the
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
import contextlib
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
from backend.app.models.printer_incident_step import STEP_KIND_COMMAND, STEP_KIND_LEVER, STEP_KIND_STOP
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.schemas.printer import ResumeRefusalReason
from backend.app.services import (
    command_outcome,
    farm_correlation,
    incident_resolution,
    live_reading,
    notify_dedup,
    printer_action,
    printer_incidents,
    refill_verdict,
    spool_respool,
    tray_fields,
)
from backend.app.services.bambu_mqtt import AMS_STATUS_IDLE, ams_mid_filament_change
from backend.app.services.command_outcome import (
    ACTOR_FARM,
    ACTOR_OPERATOR,
    Awaiting,
    DriverReading,
    MotionCommand,
    MotionReading,
    TakeoverToken,
)
from backend.app.services.feed_state import ChangeCause, FeedPhase, FeedReading
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
    Context,
    TerminalEvent,
    driver_owns,
)
from backend.app.services.job_identity import same_job
from backend.app.services.plate_occupancy import DepositEvidence
from backend.app.services.printer_action import STOP_SELF_HEAL
from backend.app.services.printer_incidents import (
    AIR_STOP_STEP,
    FAULT_RESTART_STEP,
    WAITING_REASON_RECOVERING,
    runout_slot_desc,
    waiting_reason_for,
)
from backend.app.services.printer_manager import printer_manager
from backend.app.services.refill_verdict import RefillReason, RefillTrigger
from backend.app.services.tray_fields import decode_global_tray, encode_global_tray

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.app.services.bambu_mqtt import PrinterState
    from backend.app.services.printer_events import ActionAnswered, SelfHealDue

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


# --- Code constants, NOT operator knobs (precedent the client-owned settle-wait,
#     bambu_mqtt._IDENTIFY_GATE_S / wait_ams_settle). The per-step resend count and the
#     step bound ARE operator settings (:class:`RecoverySettings`). ---------------------
_POLL_INTERVAL_S = 1.0  # live-state poll spacing during every confirm wait
_MAX_CANDIDATES = 3  # distinct replacement trays tried before escalating

# Refill auto-resume timing (code constants, NOT operator knobs — precedent
# ``command_outcome.UNLOAD_GRACE_S``). The AMS needs to register the freshly-inserted
# filament before a resume can land: a resume published on the presence edge itself
# races the firmware's own tray-state settle and is rejected. 15 s mirrors the
# operator's proven manual gap (``command_outcome.UNLOAD_GRACE_S``) — long enough to settle,
# short enough that the operator is still standing at the printer.
_RUNOUT_RESUME_SETTLE_S = 15.0

# The runout refill resume (2026-10-12, operator ruling "if it was self heal, it should stay self heal"):
# a refill lane's resume the printer has not answered this long, in its ONLINE seconds, is owed again —
# resent on the next refill evidence (the demand clearing, a presence gain) or at the level's own settle
# cadence, up to ``incident_resolution.RUNOUT_RESUME_CAP``; a late RUNNING still reaches it. The record
# owns the clock (``printer_action``): this is only the bound the refill lane hands its send.
_RUNOUT_RESUME_SELF_HEAL = printer_action.SelfHealBound(
    "_RUNOUT_RESUME_SELF_HEAL",
    30.0,
    "production _RUNOUT_RESUME_CONFIRM_S 30 s (337f4954); the 2 measured farm refill resumes (2026-10-10, "
    "printers 3 and 7) reached RUNNING in 7-11 s",
)

# The client's AMS write-refusal reason (a ``bambu_mqtt._AMS_REFUSAL_LOG_TEXT`` key)
# that recovery must NOT try to wait out: a drying cycle holds the lockout for hours,
# so the swap lane is doomed until a human stops it. Every other reason is identify
# contention, which settles in seconds.
_REFUSAL_DRYING = "drying"

# --- Settings defaults (mirror schemas/settings.py) -------------------------
_DEFAULT_ENABLED = True
_DEFAULT_MAX_ATTEMPTS = 2
_DEFAULT_PROTECT_LAYERS = 7
# The step bound (``spool_recovery_step_timeout_s``): production's default, and the setting's own
# range (``schemas/settings.py``: 15–600 s) — a stored value outside it is clamped, never trusted.
_DEFAULT_STEP_TIMEOUT_S = 90
_STEP_TIMEOUT_MIN_S = 15
_STEP_TIMEOUT_MAX_S = 600
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
    # The printer ANSWERED the farm's resume ``refused`` (its own ACK — the record, 2026-10-11): its
    # words follow the copy (``_escalate(printer_words=...)``). On a RUNOUT row only once the refill's
    # resume budget is spent (``incident_resolution.resume_spent``: 3 resumes); every other row at once.
    "resume_refused": "The printer refused the farm's resume. Press Resume.",
    # A runout refill's LAST budgeted resume went unanswered past its self-heal bound
    # (``_RUNOUT_RESUME_SELF_HEAL``): the printer did not carry it out. Its late answer still lands.
    "resume_unanswered": "The printer did not carry out the farm's resume. Press Resume.",
    # A print printed a layer with the toolhead EMPTY (the feed state's AIR) and the printer did not take
    # the farm's stop — refused, or unanswered past ``printer_action.STOP_SELF_HEAL`` — after one resend
    # (:func:`_stop_printing_on_air`). The print may still be running on air.
    "air_stop_not_taken": "Printing with the toolhead empty. The printer did not take the farm's stop. Stop the print.",
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
    # the change — and the step bound passed with the print's head still holding it
    # (:func:`_held_outcome`). Inside a change only: a command held OUTSIDE one is the driver's to
    # wait for (:func:`_await_pending_command`), never this give-up. The evidence sentence names the
    # ONE held command and what it does at the next release — that is its one place.
    "swap_held_after_release": (
        "The AMS went back into a filament change and holds the farm's swap command behind it. Free the "
        "spool, then press Retry on the printer."
    ),
    # A release verb with an unmeasured effect ENDED the print inside its own read window
    # (the reader's ``ended``). The terminal's own disposition handles the unit.
    "wedge_ended_print": "A release verb ended the print.",
    # The fault stood in the printer's END-OF-PRINT sequence (the row's ``fault_layer`` at the job's last
    # layer) and the printer's own dialog answer did not take (2026-10-16, item 6): nothing is left to
    # print, so the farm swaps and loads nothing (004-H2S incident 621).
    "end_of_print_unfinished": (
        "Fault at the last layer, in the end-of-print sequence. The job did not finish after the farm's resume; "
        "no spool was swapped or loaded. Resume or stop the job on the printer."
    ),
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
    # The rounds ran out on commands whose answers were lost with their session — the record
    # voided them (:meth:`_RecoveryEvidence.exhaustion_reason`); a session gap alone is waited
    # for, never this give-up (2026-10-11). Nothing about the AMS is claimed —
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

    ``step_bound_s`` is the STEP BOUND (``spool_recovery_step_timeout_s``, 15–600 s, default
    :data:`_DEFAULT_STEP_TIMEOUT_S`): the self-heal fallback of every wait production bounded with
    it — the fault's own PAUSE, a release verb's read, the reader's pause landing, a motion command,
    the stopped job leaving its change, a forced bare tray's config. Events decide first, every one
    of them, and a late event is still honoured; the bound is only how long the driver waits for one
    before it takes production's next self-heal step (Raymond 2026-10-12: "if it was self heal, it
    should stay self heal … not rely on timers only"). Read as :func:`step_bound`, counted in the
    printer's ONLINE seconds by the record (``printer_action``). A session gap is waited for however
    long it runs (:func:`_live_reading`) and never counts toward a bound."""

    enabled: bool
    max_attempts: int
    step_bound_s: int
    protect_layers: int


def step_bound(settings: RecoverySettings) -> printer_action.SelfHealBound:
    """THE step bound of one incident's driver (``TestSelfHealBoundOwnership``: built only in a
    ``*_bound`` function), from the operator's setting. Its witness is the measurement behind
    production's default and the release verbs' answer times."""
    return printer_action.SelfHealBound(
        name="spool_recovery_step_timeout_s",
        seconds=float(settings.step_bound_s),
        witness=(
            "the operator's spool_recovery_step_timeout_s (production default 90 s); a CONTINUE re-PAUSEd "
            "47-87 s after the send (012-H2S 2026-09-23)"
        ),
    )


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

    command: Callable[[], printer_action.Command]
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
# spelled; a missing key RAISES at the lookup (:func:`_lever`, the rule-table idiom).
# The ONE place the release verbs are built (AST-pinned in ``test_code_quality``); every one is
# sent through the record (``printer_action``) as a farm action on the row. Vendor evidence per verb (BambuStudio ``DeviceManager.cpp`` /
# ``StatusPanel.cpp``, the vendored action catalog ``backend/app/data/hms_actions.json``):
#
# * ``resume`` — the printer's own answer to its fault dialog where the vendored catalog lists
#   "Problem solved, resume" for the standing code (``bambu_mqtt.dialog_resume_button`` —
#   ``0300801E`` lists ``RESUME_PRINTING_PROBELM_SOLVED``): Studio's ``command_hms_resume``, the
#   ``err`` + ``param: "reserve"`` + ``job_id`` frame ``execute_hms_action`` builds, the plate-check
#   press's frame (2026-10-16 — the farm used to send a plain resume, which acknowledges no error:
#   ~40 plain resumes into a stuck post-swap change, 0 freed it). Otherwise ``print.resume``
#   (``BambuMQTTClient.resume_print``): BambuStudio's bare ``command_task_resume``
#   (``{"print": {"command": "resume", "param": ""}}`` — the fork sends the same verb with a
#   sequence id and no empty ``param``). Never the dialog's CONTINUE: for ``07008006`` /
#   ``07008005`` the catalog offers CONTINUE and CHECK_ASSISTANT only, and CONTINUE is
#   ``ams_control resume`` (``ams_control_resume`` below, rung 3, its order unchanged); never an
#   Ignore (it skips the printer's own re-check). Freed
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
#   the deputy holds; a retract the per-extruder map never reports degrades to the ``air``
#   arm once a layer prints with nothing fed.
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
        # The printer's own "Problem solved, resume" where the catalog lists it for the standing fault
        # (2026-10-16 — 0300_801E's dialog does), else the plain resume: ``printer_action.resume``'s
        # ``answer_dialog``. The swap round's own resume reads this rung too (:func:`_resume_and_read`).
        command=lambda: printer_action.resume(answer_dialog=True),
        pause_on_empty_path=False,
        text="resume",
        pulls=1,
        pull_rule=_pull_always,
    ),
    "resume_then_pause": LeverSpec(
        command=printer_action.resume,
        pause_on_empty_path=True,
        text="resume then pause",
        pulls=1,
        pull_rule=_pull_always,
    ),
    "ams_control_resume": LeverSpec(
        command=lambda: printer_action.ams_control("resume"),
        pause_on_empty_path=False,
        text="ams_control resume",
        pulls=1,
        pull_rule=_pull_always,
    ),
    "clean_print_error": LeverSpec(
        command=printer_action.clean_print_error,
        pause_on_empty_path=False,
        text="clean_print_error",
        pulls=1,
        pull_rule=_pull_always,
    ),
    "retry_after_clear": LeverSpec(
        command=lambda: printer_action.ams_control("resume"),
        pause_on_empty_path=False,
        text="retry after clear",
        pulls=2,
        pull_rule=_pull_always,
    ),
    "ams_control_abort": LeverSpec(
        command=lambda: printer_action.ams_control("abort"),
        pause_on_empty_path=False,
        text="ams_control abort",
        pulls=1,
        pull_rule=_pull_always,
    ),
    "ams_control_reset": LeverSpec(
        command=lambda: printer_action.ams_control("reset"),
        pause_on_empty_path=False,
        text="ams_control reset",
        pulls=1,
        pull_rule=_pull_always,
    ),
    "ams_control_pause": LeverSpec(
        command=lambda: printer_action.ams_control("pause"),
        pause_on_empty_path=False,
        text="ams_control pause",
        pulls=1,
        pull_rule=_pull_always,
    ),
    FAULT_RESTART_STEP: LeverSpec(
        command=printer_action.stop,
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
#   publish the verb; ``abort`` / ``handover`` — :func:`command_outcome.takeover` (an operator Stop's
#   terminal is ``abort``); ``air_stopped`` — the print laid down a layer on AIR inside the window and
#   the farm's air stop wrote the row's outcome (:func:`_stop_printing_on_air`: closed after the
#   terminal, or escalated when the printer did not take it) — the driver exits.
LeverReading = Literal[
    "self_healed",
    "swapped",
    "wedged",
    "released",
    "ended",
    "stopped",
    "no_pause",
    "not_sent",
    "abort",
    "handover",
    "air_stopped",
]
_LEVER_READINGS: frozenset[str] = frozenset(get_args(LeverReading))


@dataclass(frozen=True)
class LeverRead:
    """ONE read: the closed verdict, whether the printer left PAUSE (or the AMS its
    state) inside the window, and the feeder the verdict was decided on.

    ``moved`` is what tells "the resume never took" from "it took and re-PAUSEd"
    (002-H2S 2026-09-11: only the second is evidence against a replacement), and
    ``position`` is what a ``swapped`` success names — the tray the reader SAW feeding,
    never one re-read after the verdict. ``takeover`` is :func:`command_outcome.takeover`'s token when one
    ended the read (``abort`` / ``handover``), so the abort that follows names the actor the
    read saw (:func:`_abort` — ``paused_elsewhere`` closes under its own source).
    """

    reading: LeverReading
    moved: bool
    position: FeederPosition
    takeover: TakeoverToken | None = None


# What one AMS motion command came to, as the driver reads it (:func:`_observe_command`). Closed.
# The RECORD's answer first, on an event: ``complete`` — the command ran (``command_outcome.ran``);
# ``no_movement`` — the printer REFUSED it (a fail ACK); ``session_changed`` — the record VOIDED it
# (never received, or a reboot emptied the AMS's queue); ``acted`` — the AMS ran its change and it ended
# without the command completing. With no event by the STEP BOUND, the record's motion reading at the
# bound (``printer_action.Unanswered``, production's deadline answers): ``acted`` (it moved and did not
# complete), ``no_movement`` (nothing moved), ``held`` (acknowledged and unmoved — inside the print's
# change it waits behind the head, outside one the AMS runs it on its own minutes later: 012-H2S
# 2026-09-23, 011/014-H2S 2026-10-09/10), ``undecidable`` (an unload sent into a mid-change AMS with
# nothing loaded — nothing physical can answer it), or ``complete`` / ``session_changed`` as above.
DriverMotion = Literal["complete", "acted", "no_movement", "held", "undecidable", "session_changed"]
# The unload step (:func:`_unload_and_confirm`): a :data:`DriverMotion` for a publish that went out —
# ``held`` only for a command held INSIDE a change past the step bound (:func:`_held_outcome`: one held
# outside a change is waited for, and comes back as what it settled to) — or the step's own ``skipped`` /
# ``drying`` / ``refused`` (every publish did not go out — the record answered it ``refused``), or a
# takeover.
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
# mid-change AMS with nothing loaded can be.
LoadStep = Literal[
    "complete", "acted", "no_movement", "held", "session_changed", "drying", "refused", "abort", "handover"
]
# What a step answers when :func:`command_outcome.takeover` ends it (:func:`_note_takeover`).
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
    action_id: int | None = None

    @classmethod
    def draft(cls, lever: Lever, *, action_id: int | None = None) -> LeverStep:
        """An unsent step: the log's ``note`` assigns its ``seq`` and ``at``. ``action_id`` is the
        lever's recorded ``printer_action`` (recorded BEFORE the note, published after it)."""
        return cls(seq=0, lever=lever, outcome=None, moved=None, at=None, action_id=action_id)

    def entry(self) -> printer_incidents.StepEntry:
        """The ledger row this step is written as: a ``lever`` naming the lever and its action."""
        return printer_incidents.StepEntry(kind=STEP_KIND_LEVER, name=self.lever, action_id=self.action_id)

    def sent(self, seq: int, at: datetime | None) -> LeverStep:
        return replace(self, seq=seq, at=at)

    def answered(self, outcome: str) -> LeverStep:
        """The reader's verdict. A token that is no :data:`LeverReading` RAISES."""
        if not _is_reading(outcome):
            raise LookupError(f"spool_recovery: lever step {self.seq} answered with {outcome!r}")
        return replace(self, outcome=outcome)


@dataclass(frozen=True)
class CommandStep:
    """One AMS motion command that WENT OUT (its ``printer_action`` row — ``action_id`` — the record
    of the send and of the printer's answer), the feeder position read at the send, and the driver's
    step reading (the record's answer, :func:`_observe_command`) — ``None`` until the read, and for good when a
    takeover ended the wait."""

    seq: int
    command: MotionCommand
    target: int | None
    feeder: str | None
    answer: MotionReading | None
    at: datetime | None
    action_id: int | None = None

    @classmethod
    def draft(
        cls, command: MotionCommand, target: int | None, feeder: str | None, *, action_id: int | None = None
    ) -> CommandStep:
        """An unsent step: the log's ``note`` assigns its ``seq`` and ``at``."""
        return cls(seq=0, command=command, target=target, feeder=feeder, answer=None, at=None, action_id=action_id)

    def entry(self) -> printer_incidents.StepEntry:
        """The ledger row this step is written as: a ``command`` with its tray, feeder and action."""
        return printer_incidents.StepEntry(
            kind=STEP_KIND_COMMAND,
            name=self.command,
            target=self.target,
            feeder=self.feeder,
            action_id=self.action_id,
        )

    @property
    def loaded_at_send(self) -> bool | None:
        """Was the active extruder fed from a real AMS feeder at the send — the recorded feeder kind
        (``jammed`` / ``other``); ``None`` when none was read."""
        if self.feeder is None:
            return None
        return self.feeder in _LOADED_FEEDERS

    def sent(self, seq: int, at: datetime | None) -> CommandStep:
        return replace(self, seq=seq, at=at)

    def answered(self, outcome: str) -> CommandStep:
        """The classifier's answer. A token that is no ``command_outcome.MotionReading`` RAISES."""
        if not _is_answer(outcome):
            raise LookupError(f"spool_recovery: command step {self.seq} answered with {outcome!r}")
        return replace(self, answer=outcome)


# What the READ of the farm's air stop answered (:func:`_stop_printing_on_air`). CLOSED, and the plate-check
# stop's own vocabulary token for token (``pause_recovery.PlateCheckStopAnswer``: the record's backfill table
# reads every ``stop`` step through one row per token): ``taken`` — the job ended (the record's ``reached``);
# ``not_taken`` — the printer refused it, or a person's command took it over; ``unconfirmed`` — no event
# answered it by ``printer_action.STOP_SELF_HEAL`` (the action stays outstanding: a late terminal still ends
# the job by the farm's hand); ``not_sent`` — the frame never left the farm, or the record voided it (never
# received, a reboot).
AirStopAnswer = Literal["taken", "not_taken", "unconfirmed", "not_sent"]


@dataclass(frozen=True)
class AirStopStep:
    """One farm ``print.stop`` of a print running on AIR (:func:`_stop_printing_on_air`), written on the
    open row's ledger BEFORE the frame goes out — a ``stop`` step named ``printer_incidents.AIR_STOP_STEP``,
    whose projection (``PAYLOAD_AIR_STOP``) tells the terminal classifier the farm ended the job (the
    ``air_stop`` verdict). ``outcome`` is ``None`` from the send until the read."""

    seq: int
    outcome: AirStopAnswer | None
    at: datetime | None
    action_id: int | None = None

    @classmethod
    def draft(cls, *, action_id: int) -> AirStopStep:
        """An unsent step: the log's ``note`` assigns its ``seq`` and ``at``."""
        return cls(seq=0, outcome=None, at=None, action_id=action_id)

    def entry(self) -> printer_incidents.StepEntry:
        """The ledger row this step is written as: a ``stop`` named :data:`AIR_STOP_STEP` and its action."""
        return printer_incidents.StepEntry(kind=STEP_KIND_STOP, name=AIR_STOP_STEP, action_id=self.action_id)

    def sent(self, seq: int, at: datetime | None) -> AirStopStep:
        return replace(self, seq=seq, at=at)

    def answered(self, outcome: str) -> AirStopStep:
        """The read's answer. A token that is no :data:`AirStopAnswer` RAISES."""
        if not _is_air_stop_answer(outcome):
            raise LookupError(f"spool_recovery: air stop step {self.seq} answered with {outcome!r}")
        return replace(self, outcome=outcome)


RecoveryStep = LeverStep | CommandStep | AirStopStep

_COMMANDS: frozenset[str] = frozenset(get_args(MotionCommand))
_ANSWERS: frozenset[str] = frozenset(get_args(MotionReading))
_AIR_STOP_ANSWERS: frozenset[str] = frozenset(get_args(AirStopAnswer))
#: The feeder kinds a step records at the send that name a real AMS feeder at the extruder.
_LOADED_FEEDERS: frozenset[str] = frozenset({"jammed", "other"})


def _is_lever(token: str) -> TypeGuard[Lever]:
    return token in _LEVERS


def _is_reading(token: str) -> TypeGuard[LeverReading]:
    return token in _LEVER_READINGS


def _is_command(token: str) -> TypeGuard[MotionCommand]:
    return token in _COMMANDS


def _is_answer(token: str) -> TypeGuard[MotionReading]:
    return token in _ANSWERS


def _is_air_stop_answer(token: str) -> TypeGuard[AirStopAnswer]:
    return token in _AIR_STOP_ANSWERS


class _RecoveryEvidence(printer_incidents.EvidenceLog[RecoveryStep]):
    """THE evidence log of one AMS incident: every lever pulled, every AMS command sent and every
    air stop, in send order, with what the wire answered — the RECORDS the escalation reason,
    the operator page and a restarted driver's next lever are derived from.

    The store's :class:`~backend.app.services.printer_incidents.EvidenceLog` (its write
    discipline: noted at the send, answered at the read, built only by ``from_row``, the
    ledger's one writer) bound to THIS driver's vocabulary — :class:`LeverStep`,
    :class:`CommandStep` and :class:`AirStopStep` — plus every reading this driver derives from its log. Durable,
    because on a feeder-stall wedge every release verb's consequence is identical
    (012-H2S 2026-09-23), so a driver restarted from the wire alone re-grinds the stall
    from the first lever. Every counter is DERIVED from ``steps``; nothing is counted
    twice.

    The 009-H2S incident reported ``no_eligible_spool`` after four failed loads — a lie
    that sent the operator looking for spools instead of at the feed path. The reason is
    read off this log, never off a position in the code.
    """

    @classmethod
    def _step_of(cls, row: printer_incidents.LedgerRow) -> RecoveryStep:
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
            return LeverStep(
                seq=row.seq, lever=row.name, outcome=outcome, moved=None, at=row.sent_at, action_id=row.action_id
            )
        if row.kind == STEP_KIND_COMMAND:
            if not _is_command(row.name):
                raise LookupError(
                    f"spool_recovery: incident {row.incident_id} step {row.seq} names no command: {row.name!r}"
                )
            if outcome is not None and not _is_answer(outcome):
                raise LookupError(f"spool_recovery: incident {row.incident_id} step {row.seq} answers {outcome!r}")
            return CommandStep(
                seq=row.seq,
                command=row.name,
                target=row.target,
                feeder=row.feeder,
                answer=outcome,
                at=row.sent_at,
                action_id=row.action_id,
            )
        if row.kind == STEP_KIND_STOP and row.name == AIR_STOP_STEP:
            if outcome is not None and not _is_air_stop_answer(outcome):
                raise LookupError(f"spool_recovery: incident {row.incident_id} step {row.seq} reads {outcome!r}")
            return AirStopStep(seq=row.seq, outcome=outcome, at=row.sent_at, action_id=row.action_id)
        raise LookupError(
            f"spool_recovery: incident {row.incident_id} step {row.seq} has kind {row.kind!r} ({row.name!r})"
        )

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

    @property
    def air_stops(self) -> list[AirStopStep]:
        """Every farm stop of a print running on air this row sent (:func:`_stop_printing_on_air`)."""
        return [s for s in self.steps if isinstance(s, AirStopStep)]

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
        """The farm's motion command that has NOT RUN as far as this driver's log reads — the log's
        LAST command step while it pends (``printer_incidents.command_pends``: answered ``held``, or
        sent and never read). Nothing is sent behind it: a second command behind a queued one runs too
        (012-H2S 2026-09-23), and a queued pull-back runs on its own (011/014-H2S 2026-10-09/10). The
        driver's wait (:func:`_await_pending_command`) settles the step from the RECORD's answer of its
        action first (``printer_action``: an event — the toolhead, a reboot, an operator's command —
        answers it, across a restart too), so the log and the record never disagree for long."""
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

    def _landed_load_step(self) -> CommandStep | None:
        """The incident's last COMPLETED motion when it is a load (see :attr:`landed_load`)."""
        for s in reversed(self.command_steps):
            if s.answer == "complete":
                return s if s.command == "load" else None
        return None

    @property
    def landed_load(self) -> int | None:
        """The tray of this incident's last farm load that LANDED, while it is the incident's last COMPLETED
        motion — the farm's replacement on the path (an unload completed after it took it off again): THE
        cursor of "a farm load landed on this path" (2026-10-16). ``None`` when no farm load landed, or an
        unload followed it. Read by the round verdict (:func:`_round_owed`) and by the kept 009 arm
        (:func:`_empty_path_hang`: never over a path the farm's load landed on)."""
        step = self._landed_load_step()
        return step.target if step is not None else None

    @property
    def release_onto_landed_load(self) -> LeverStep | None:
        """The last RELEASE (a lever read ``released``) sent AFTER the farm's landed load — the AMS let go of
        the print's change with the farm's own replacement on the path (014-H2S #620, 005-H2S #597 / #603, the
        513..540 set: the replacement then sat fed at the nozzle and the round unloaded it or gave up).
        ``None`` with no landed load, or no release after it. Item 5 of 2026-10-16 ("after a release, if the
        farm's new spool is at the nozzle, resume on it") reads it with the feed state and the record
        (:func:`_round_owed`)."""
        landed = self._landed_load_step()
        if landed is None:
            return None
        return next((s for s in reversed(self.lever_steps) if s.outcome == "released" and s.seq > landed.seq), None)

    def lever_spent(self, lever: Lever) -> bool:
        """Has ``lever`` been pulled its own budget (:attr:`LeverSpec.pulls`) on this
        incident? A step counts from its SEND: a lever sent and never read (a crash between
        the two) is spent, so a restarted driver resumes mid-budget. A pull read ``not_sent``
        never reached the printer (the frame did not go out, or the record voided it — a later
        session began with it unacknowledged) and is not spent: the ladder sends it again."""
        return sum(1 for s in self.lever_steps if s.lever == lever and s.outcome != "not_sent") >= _lever(lever).pulls

    @property
    def restart_stop(self) -> LeverStep | None:
        """The restart rung's step (:data:`printer_incidents.FAULT_RESTART_STEP`) once it was
        SENT. A stop read ``not_sent`` never reached the printer, so it ended nothing. SENT is not
        ENDED: whether the stop ended the job is :meth:`restart_ended`'s (2026-10-12, AR-H3) — a
        stop the printer never takes leaves the job PAUSEd, and its incident is still the AMS hold
        over a live job."""
        return next((s for s in self.lever_steps if s.lever == FAULT_RESTART_STEP and s.outcome != "not_sent"), None)

    async def restart_ended(self, state: PrinterState | None) -> bool:
        """Did the restart rung's stop END the job? Read through the RECORD — the stop ACTION's
        answer (``printer_action.settled``) — and the reader's own reading of it:

        * ``reached`` — the printer holds no job (or another one): its terminal answered the stop,
          however late — ended;
        * the step read ``stopped`` — the reader saw the job's terminal inside the rung's window —
          ended; and so does a printer reading a job-over word NOW (a terminal the record has not
          answered yet: a re-entry after a restart, before the session start re-read the action);
        * anything else — the stop still outstanding past the step bound with the job PAUSEd (the
          reader read the PAUSE at the bound: ``wedged`` / ``released``), or a stop the record voided
          or a person took over — the job is LIVE. Its give-up is the paused-job give-up: an empty
          toolhead is refilled (invariant 8, :func:`_refill_toolhead`) and the row stays the AMS hold
          over the paused job, never re-classified physical (:func:`_give_up`). A late terminal still
          classifies ``fault_restart``: the step stays on the open row's ledger (its projection,
          ``printer_incidents.PAYLOAD_FAULT_RESTART_STOP``).

        ``False`` with no restart stop on the log."""
        stop = self.restart_stop
        if stop is None:
            return False
        if stop.outcome == "stopped" or _live_state(state) in _JOB_OVER_STATES:
            return True
        if stop.action_id is None:
            return False
        return await printer_action.settled(stop.action_id) is command_outcome.Answer.REACHED

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
    # When this driver context was built (naive UTC, the record's clock) — the instant the
    # driver's ownership of the printer began. :func:`_driver_reading` reads an operator's AMS
    # command the RECORD holds at or after it (``printer_action.operator_commanded_since``) as
    # the operator taking the printer over, and the step bound of the fault's own PAUSE counts from
    # it (:func:`_await_fault_pause`). Taken by the construction itself, so both construction sites
    # (the entry gate and the session-start re-entry, through :func:`_build_incident`) carry it
    # without restating it.
    started_at: datetime = field(default_factory=lambda: printer_action.now())

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
# ``sweep_open_incidents`` reads a ``recovering`` row as "a live driver has this"
# (``incident_resolution.driver_owns``) — do not turn that reading into a third spelling of
# ownership.

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
# every negative edge fires at once (2026-09-04, printer 8). Edges are what a SESSION
# witnessed: the first sample after a (re)start only seeds. What a restart could not witness —
# a runout refilled while the farm was down — is the open runout row's LEVEL, asked by the sweep
# and the session start (:func:`_maybe_resume_runout`), never this sample's.
_wire_sample: dict[int, tuple[str, tuple[int, int] | None, frozenset[str], int]] = {}

# THE BAR an ABORTED close puts on re-entry for its exact standing fault is the CLOSED ROW itself
# (durable since 2026-10-11): ``printer_incidents.find_closed`` + ``status == aborted`` +
# ``rearmed_at IS NULL``. The loop bound the per-push entry gate needs — without it, a code the
# firmware leaves standing after healing itself (or after an operator took over) would re-open an
# incident every throttle window — and no longer lifted wholesale by a restart.
#
# The wire RE-ARMS a bar the moment it says this is no longer the same fault — an EVENT, written to
# the row (``printer_incidents.rearm_aborted``):
#   * its codes are no longer ALL standing (the fault cleared, wholly or partly), or
#   * the printer was WITNESSED going INTO PAUSE (a transient close said "it never held the
#     printer"; a pause proves that answer is now stale).
# And one LEVEL at the gate (:func:`_fault_already_closed`): a TRANSIENT close (no actor — the
# printer never paused for the fault) whose printer reads PAUSE now has paused since, whoever saw
# it — the reading a restart can make.
#
# This dict is the CACHE of the bars the sampler watches for those edges: (printer_id, job_id) ->
# {fingerprint: the closed row's id}. Filled by the ABORTED close and by the gate when it reads a
# standing bar; a restart empties it and the gate refills it from the row on the first evaluation of
# the standing fault.
_blocked: dict[tuple[int, str], dict[str, int]] = {}

# Closed rows the wire re-armed in THIS process whose ``rearmed_at`` write may not have landed yet
# (:func:`_write_rearm` runs as a task off the sync sampler): the gate reads them as re-armed at once.
# Process memory by design: each id leaves when its write lands; a crash before it lands leaves the
# bar standing on the row — the safe direction, lifted again by the next witnessed edge.
_rearmed: set[int] = set()

# printer_id -> the MQTT session (``connection_epoch``) whose first fresh report this module's
# session start (:func:`reevaluate_session`) has re-evaluated. The incident sweep acts on a printer
# only once its session's own re-evaluation ran — after the downtime reconcile, whose synthesised
# terminal is classified against the holds that stood when the job ended — so a sweep tick between
# the first fresh report and that reconcile can never pre-empt it. Process memory: a restart empties
# it, and the next first fresh report re-evaluates (nothing to rehydrate).
_session_evaluated: dict[int, int] = {}

# printer_id -> the lock the farm's resume of a hold is sent under (:func:`resume_paused_print`): it
# spans the record's once read and the action's insert. Process memory of a critical section; a restart
# has none in flight, so nothing to rehydrate.
_resume_locks: dict[int, asyncio.Lock] = {}

# incident_id -> the LIVE task that will send the row's resume (:func:`_maybe_self_heal_after_repair`):
# liveness of a task between its spawn and its send — the evidence lane's settle sits in between —
# never a decision. Whether the row was ALREADY resumed is the record's (a farm ``resume`` action on the
# row: ``incident_resolution.resume_owed``'s once), durable across the restart that wipes this. A
# restart kills the task; nothing was sent if it died before its send, and the row is then owed again.
_resume_tasks: dict[int, asyncio.Task[bool]] = {}

# incident ids whose OWN job was seen RUNNING since the row opened: a RUNNING edge
# (:func:`on_observed_running`) with no recovery driver live, no eject owning the printer, on the row's
# job or an id-less one (``incident_resolution.own_job_running``). Handed to the rule table as
# ``Context.job_ran`` by the AMS entry's new-fault question (:func:`_new_fault_verdict`): a wire row held
# open past that edge (R27) must not swallow a DIFFERENT fault after a touchscreen resume, because
# production's edge closed the row there and that fault opened its own row with its own driver.
# Rehydrate story: none. Process memory of an edge the wire cannot restate; after a restart the
# new-fault cell falls back to its durable arms (the job's layer past the row's ``fault_layer``, the
# farm's resume on the row reached), and the next RUNNING edge records it again. Pruned by the sweep
# once the row is closed.
_job_ran_since_open: set[int] = set()

# --- The repeat-jam quarantine's VOCABULARY (the decision is ``farm_policy``'s, owner 6) -------
# A printer whose recovery keeps escalating with no completed print between the escalations is
# quarantined: a recurring AMS jam is hardware (buffer / feeder), not a spool the swap machine can
# fix (009-H2S 2026-07-20: three same-fault escalations across the day). WHEN that is — the count
# and the "no completed print between them" rule — is ``farm_policy.record_recovery_escalation``'s,
# the one quarantine decider; this module names which of ITS escalation reasons are evidence.

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
        # the end-of-print sequence faulted and the dialog answer did not take — nothing left to print, no swap
        "end_of_print_unfinished",
        "printer_offline",  # the printer stopped reporting — a network / power gap, not AMS hardware
        # inventory, as ``no_eligible_spool``: the path unloaded cleanly and nothing matched
        "restart_no_candidate",
        # The refill episode's (K10): the farm's reaction to an EMPTY toolhead whatever emptied it
        # (a queued pull-back, a screen unload, a resume onto air) — not by itself a buffer /
        # feeder diagnosis; a jam its load raises opens its own incident, which counts.
        "toolhead_refill_failed",
        "toolhead_refill_withheld",
        # The printer's own answer to the farm's resume or stop — a command not carried out, not AMS hardware.
        "resume_refused",
        "resume_unanswered",
        "air_stop_not_taken",
    }
)

# The escalation reasons of the refill episode — whatever the row's kind (an AMS row the refill
# re-entered, or its own ``toolhead_refill`` row), their page states the measured toolhead and the
# refill (:func:`_compose_detail`).
_TOOLHEAD_REASONS: frozenset[str] = frozenset(
    {"toolhead_refill_failed", "toolhead_refill_withheld", "resume_refused", "resume_unanswered", "air_stop_not_taken"}
)


def _reset_state() -> None:
    """Test hook: clear module-level edge/dedup state between cases. The driver liveness
    slots are the incident store's and its own reset clears them."""
    _last_eval.clear()
    _guidance_sent.clear()
    _wire_sample.clear()
    _blocked.clear()
    _rearmed.clear()
    _session_evaluated.clear()
    _resume_tasks.clear()
    _resume_locks.clear()
    _job_ran_since_open.clear()
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

    ONE predicate — used by the entry gate and by :func:`owned_full_codes` so a suppressed alert
    and a refused entry can never disagree — over the DURABLE bar (see :data:`_blocked`): the latest
    CLOSED incident for this ``(printer, job, fingerprint)`` is ABORTED and the wire has not
    re-armed it (``rearmed_at`` NULL). An aborted close means an external actor took over or the
    fault proved transient; a RESOLVED one deliberately re-arms, because a genuine second tangle in
    one job must still be recovered (the flap cap bounds that loop).

    One level lifts it here: a TRANSIENT close (no actor — the printer never paused for the fault)
    whose printer reads PAUSE now has paused since (the reading a restart can make, where the
    sampler's edge was never witnessed). A standing bar joins the sampler's watch (:data:`_blocked`),
    so its re-arm edge is seen however this process learned of it.
    """
    closed = await printer_incidents.find_closed(db, printer_id, job_id, fingerprint)
    if closed is None or closed.status != STATUS_ABORTED or closed.rearmed_at is not None or closed.id in _rearmed:
        return False
    if closed.resolve_source is None and _live_state(_get_state(printer_id)) == "PAUSE":
        return False
    _blocked.setdefault((printer_id, job_id), {})[fingerprint] = closed.id
    return True


def _rearm_blocked(printer_id: int, live_tokens: frozenset[str], *, paused_edge: bool) -> None:
    """Lift the bars the wire has just re-armed (see :data:`_blocked`): a WITNESSED pause edge
    lifts every bar on the printer, a fault no longer wholly standing lifts its own. The re-arm is
    written to the closed rows (``printer_incidents.rearm_aborted``, spawned — this rides the sync
    per-push sampler), so a restart after it keeps the bar lifted."""
    lifted: set[int] = set()
    for (pid, job), bars in list(_blocked.items()):
        if pid != printer_id:
            continue
        survivors = (
            {} if paused_edge else {fp: row for fp, row in bars.items() if fingerprint_tokens(fp) <= live_tokens}
        )
        lifted.update(row for fp, row in bars.items() if fp not in survivors)
        if survivors:
            _blocked[(pid, job)] = survivors
        else:
            _blocked.pop((pid, job), None)
    if not lifted:
        return
    _rearmed.update(lifted)
    from backend.app.core.tasks import spawn_background_task

    spawn_background_task(_write_rearm(printer_id, frozenset(lifted)), name=f"incident-rearm-p{printer_id}")


async def _write_rearm(printer_id: int, incident_ids: frozenset[int]) -> None:
    """Record the witnessed re-arm on the closed rows (:func:`_rearm_blocked`). Never raises."""
    try:
        from backend.app.core.database import async_session

        async with async_session() as db:
            count = await printer_incidents.rearm_aborted(db, incident_ids)
        _rearmed.difference_update(incident_ids)
        logger.info(
            "spool_recovery: printer %s — the wire re-armed %d aborted fault(s) (incident(s) %s)",
            printer_id,
            count,
            sorted(incident_ids),
        )
    except Exception:  # noqa: BLE001 — a bookkeeping write must never crash the sampler's task
        logger.exception("spool_recovery: recording a re-arm failed for printer %s", printer_id)


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


def _live_report(printer_id: int) -> PrinterState | None:
    """The printer's state when it is evidence about the printer NOW (``live_reading.reads_live``: the
    current session's fresh report), else ``None`` — a reconnect's re-broadcast of the previous
    session's cached state and a disconnected printer say nothing (the incident sweep's gate)."""
    st = _get_state(printer_id)
    return st if st is not None and live_reading.reads_live(st) else None


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
    return RecoverySettings(
        enabled=await _read_bool(db, "spool_recovery_enabled", _DEFAULT_ENABLED),
        max_attempts=await _read_int(db, "spool_recovery_max_attempts", _DEFAULT_MAX_ATTEMPTS),
        step_bound_s=min(
            max(
                await _read_int(db, "spool_recovery_step_timeout_s", _DEFAULT_STEP_TIMEOUT_S),
                _STEP_TIMEOUT_MIN_S,
            ),
            _STEP_TIMEOUT_MAX_S,
        ),
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
    (``tray_fields.encode_global_tray``, invariant 1), which knows the AMS-HT and
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
    3. The feeder at fault time, from the toolhead's FEED STATE (:func:`_feed_reading`, invariant
       16) — never a ``tray_now`` / ``last_loaded_tray`` frame read here: the ACTIVE extruder's
       EFFECTIVE feeder (during a drained roll's TAIL the DRAINING roll, never a ``tray_now``
       pre-flip to the backup that is not fed yet — the jam is on the path that feeds), then that
       extruder's last real feeder (``ExtruderReading.last_fed``: after a feed fault the toolhead
       frequently reads 255 "nothing feeding" while the jam is on the tray that was feeding a
       second earlier — per extruder, so on a dual nozzle it is the active hotend's own). Both
       through ``tray_fields.valid_feeder``: the external holder is no tray to swap.
    4. The single mapped feeder, when the wire named none.
    """
    slot = _candidate_slot(candidates, AmsFaultClass.MECHANICAL_FEED)
    tray = encode_global_tray(*slot) if slot is not None else None
    if tray is not None:
        return tray, "single"

    feeders, _source = _job_feeders(state, item, printer_id)
    if len(feeders) > 1:
        return None, "multi_feeder"

    reading = _feed_reading(printer_id) if printer_id is not None else None
    if reading is not None:
        live = tray_fields.valid_feeder(reading.feeder)
        if live is None and reading.active_extruder is not None:
            live = tray_fields.valid_feeder(reading.extruder(reading.active_extruder).last_fed)
        if live is not None:
            return live, "single"
    if feeders:
        return feeders[0], "single"
    return None, "none"


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
    ``command_outcome.MotionReading``, the step's own ``refused`` / ``drying``, or ``not_at_toolhead`` (the
    load answered ``complete`` and the ACTIVE extruder does not read it). ``tray`` is ``None``
    only when the printer left its session before a spool was picked."""

    tray: int | None
    command: MotionCommand
    answer: str


@dataclass(frozen=True)
class RefillNoCandidate:
    """No spool to load: the job's last feeder is absent or spent, and the swap's own selection
    found nothing either."""


# What the page states about a refill (:func:`_feeder_clause`).
RefillResult = RefillLoaded | RefillFailed | RefillNoCandidate
# What :func:`_refill_toolhead` returns: its result, or a takeover (:func:`command_outcome.takeover`) — closed,
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
    what the command acts on, which ``command_outcome.posture`` keys the answer by and
    ``command_outcome.ran`` reads back), and the operator's resume on the jammed feeder
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
# contradict it — the ``_retract_clause`` lesson. The air stop's give-up is the same shape: its copy's
# own last sentence (Stop the print) is the one action over a print that may still run on air.
_CLAUSE_WITHOUT_INSTRUCTION: frozenset[str] = frozenset(
    {"wedge_unreleased", "swap_held_after_release", "air_stop_not_taken", "end_of_print_unfinished"}
)
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


def _job_ended(reason: str, *, stop_ended: bool) -> bool:
    """Is this page composed after the job ENDED? DERIVED, from the reason and the step
    ledger: a reason that is only reached after the end (:data:`_JOB_ENDED_REASONS`), or any
    give-up once the restart stop ENDED the job (``stop_ended`` —
    :meth:`_RecoveryEvidence.restart_ended`, read through the record; a stop the printer never
    took leaves the job live and its page the paused one). Such a page says neither "left
    PAUSED" nor "resume on the printer": it carries no feeder clause (the filament's place is the
    copy's own, and the next job's question), its reason copy is the ended-side one where there
    are two (:data:`_ENDED_JOB_DETAIL`), and the notification wrapper is told so
    (``on_spool_recovery_failed(job_ended=True)``)."""
    return reason in _JOB_ENDED_REASONS or stop_ended


async def _stop_ended(incident: RecoveryIncident, evidence: _RecoveryEvidence | None) -> bool:
    """Did this incident's restart stop end the job (:meth:`_RecoveryEvidence.restart_ended`, over
    the live state)? ``False`` with no log."""
    return evidence is not None and await evidence.restart_ended(_get_state(incident.printer_id))


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


def _step_label(step: RecoveryStep) -> str:
    """The page's name of one step: the lever table's own text, ``unload``, ``load <slot>``, or
    ``print stop`` (an air stop)."""
    match step:
        case LeverStep():
            return _lever(step.lever).text
        case CommandStep():
            return "unload" if step.command == "unload" else f"load {_command_slot(step)}"
        case AirStopStep():
            return "print stop"
        case _:
            assert_never(step)


def _step_outcome(step: RecoveryStep) -> str:
    """The step's recorded verdict token — the reader's for a lever, the classifier's for a
    command, the read's for an air stop — or ``no answer`` for a step sent and never read."""
    match step:
        case LeverStep():
            token = step.outcome
        case CommandStep():
            token = step.answer
        case AirStopStep():
            token = step.outcome
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
    printer_words: str | None = None,
    stop_ended: bool = False,
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
    and the print is over. ``stop_ended`` is the caller's reading of the restart stop
    (:func:`_stop_ended` — the record's answer, which this sync composer does not read).

    ``printer_words`` — the printer's own reason for refusing the farm's command (the record's
    ``reason``), quoted after the copy.
    """
    ended = _job_ended(reason, stop_ended=stop_ended)
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
    if printer_words:
        clauses.insert(0, f"Printer: {printer_words.rstrip('.')}.")
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
            driver_live=printer_incidents.driver_live(printer_id),
            command_pending=_pending_on(row),
            feed=_feed_reading(printer_id),
            observed_load_at=printer_action.observed_load_at(printer_id),
            job_layer=_printed_layer(printer_id),
            job_ran=row.id in _job_ran_since_open,
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
                    fault_layer=_printed_layer(printer_id),
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
                    fault_layer=_printed_layer(printer_id),
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
    """The recovery driver task: hydrate the incident's evidence log and run the state machine
    (:func:`_drive_recovery`). Never raises; always releases its liveness slot on exit
    (``printer_incidents.release_driver``)."""
    pid = incident.printer_id
    try:
        from backend.app.core.database import async_session

        # The incident's evidence log, as the durable step ledger holds it — empty for a
        # fresh incident, the previous driver's steps for a startup re-entry, so a
        # restarted driver never re-pulls a lever it already spent. Read FIRST: every
        # give-up below pages from it.
        async with async_session() as db:
            evidence = await _RecoveryEvidence.from_row(db, incident.incident_id)
        await _drive_recovery(incident, evidence)
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
        # RUNNING counts (:func:`live_reading.reads_live`): a printer off its session still
        # carries its dead session's cached RUNNING, which says nothing about the printer now.
        st = _get_state(pid)
        if st is not None and live_reading.reads_live(st) and _live_state(st) == "RUNNING":
            await on_observed_running(pid)


async def _drive_recovery(incident: RecoveryIncident, evidence: _RecoveryEvidence) -> None:
    """The recovery state machine (numbered per the plan), inside :func:`_run_recovery`'s
    guard. Every read of the wire goes through the session gate (:func:`_live_reading`): a gap
    is a wait. Every step's answer is an EVENT first — the printer's report, its ACK through the
    record (``printer_action``), a terminal, a person's act — and the step bound
    (:func:`step_bound`) is each bounded step's self-heal fallback (2026-10-12)."""
    pid = incident.printer_id
    # (0) A driver needs a printer that is reporting: a session can drop between the push
    #     that raised the fault and this task starting. Not a close — a wait
    #     (:func:`_live_reading`), answered by the printer's next session.
    st, _waited = await _live_reading(incident)

    # A driver re-entered on an incident whose restart stop already went out (a deploy
    # mid-restart): the job is over, so there is no PAUSE to wait for and no lever left —
    # the continuation picks up from the log. A job still live under the stop step (the
    # stop never landed) falls through: every rung is spent, and the ladder reads it.
    if evidence.restart_stop is not None and _live_state(st) in _JOB_OVER_STATES:
        await _restart(incident, evidence=evidence)
        return

    # (1) The fault PAUSEs the print; a print the firmware rescued itself never PAUSEs
    #     (:func:`_await_fault_pause`: it printed a layer FED on a quiet path — its backup
    #     switch, or a fault that cleared) and a job that ENDED has nothing to recover →
    #     close silently as TRANSIENT. Closed as ``aborted`` with no resolve_source (no
    #     actor can claim an outcome nobody produced), which also bars re-entry for this
    #     exact fault on this job: the firmware can leave the code standing after healing
    #     itself, and the per-push entry gate would otherwise re-open an incident every
    #     throttle window. The printer's events decide first; with none by the step bound the
    #     fault never PAUSEd (production's verdict — :func:`_await_fault_pause`'s ``unpaused``).
    #     A print that printed a layer on AIR under the fault is a ruined print: the farm STOPS it and
    #     the unit is retried (:func:`_stop_printing_on_air`, operator ruling 2026-10-12) — the stop
    #     writes the row's outcome itself (closed after the terminal, or escalated when not taken).
    while (pause := await _await_fault_pause(incident)) == "air":
        match await _stop_printing_on_air(incident, evidence):
            case "stopped" | "escalated":
                return
            case "taken_over":
                await _abort(incident, token="operator_command")
                return
            case "not_air":
                continue
            case _ as never:
                assert_never(never)
    if pause != "paused":
        if evidence.pending_command is not None:
            # Not a transient: a re-entered incident whose log holds a farm command the AMS has
            # not run, on a printer somebody resumed while the farm was down. That command can
            # still drain under the RUNNING print, so the row is kept the way every takeover
            # keeps it while one pends (K11) — :func:`_abort`'s one rule, never a second.
            await _abort(incident)
            return
        await _close_incident(incident, status=STATUS_ABORTED, source=None)
        if pause == "unpaused":
            logger.info("spool_recovery: printer %s never PAUSEd (firmware rescue / transient) — incident closed", pid)
        else:
            logger.info(
                "spool_recovery: printer %s did not PAUSE for the fault (the print ran on fed, or the job ended) — "
                "incident closed",
                pid,
            )
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

        # (a) THE ROUND TOP (F8, 2026-10-16): one verdict of what this round owes — the release ladder
        #     while the AMS holds the print's change (a release comes back here), a resume on the farm's
        #     replacement when the feed state reads it at the nozzle, the printer's dialog answer alone when
        #     the fault stood in the end-of-print sequence, or the swap below.
        match await _round_top(incident, evidence):
            case "swap":
                pass
            case "over":
                return
            case "next_round":
                continue
            case _ as never_top:
                assert_never(never_top)

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
            token = command_outcome.takeover(_driver_reading(incident, st), awaiting="PAUSE")
            if token is not None:
                verdict = _note_takeover(incident, token, "select_replacement")
                if verdict == "handover":
                    _hand_over(incident)
                else:
                    await _abort(incident, token=token)
                return
            # Nothing to load: the round table answers before any give-up (F8) — the farm's own
            # replacement at the nozzle is resumed on, never thrown away as "no other spool".
            if await _resume_on_fed_instead(incident, evidence) == "over":
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
        # (ams_drying, unload_failed, load exhaustion) correctly
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
                # Held INSIDE a change past the step bound: the AMS re-entered one under the
                # driver and acknowledged the unload without moving, so it runs at the next
                # release, which only a human causes there. Nothing may be sent behind it — the
                # page names the held unload, and the row stays open on it. (Held OUTSIDE a change
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
                # Held behind a change the AMS re-entered, past the step bound: it runs at the next
                # release, refilling the extruder there — never resent, and no refill behind it (the
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

    # (4) Every candidate exhausted — the round table answers first (F8): a release in the last round
    #     that left the farm's replacement fed at the nozzle is resumed on, never thrown away.
    if await _resume_on_fed_instead(incident, evidence) == "over":
        return
    _log_candidate_outcome(incident, gtid=None, verdict="candidates_exhausted")
    await _give_up(incident, evidence.exhaustion_reason(), evidence=evidence)


async def _resume_on_fed_instead(incident: RecoveryIncident, evidence: _RecoveryEvidence) -> RoundTail | None:
    """The round table asked where the swap has nothing left to send (no candidate, every round spent —
    F8): when it owes ``resume_on_fed`` (the farm's own landed load reads fed at the nozzle), the resume on it
    (:func:`_resume_and_read`) and its tail; ``None`` when it owes anything else — the caller's give-up
    stands."""
    st, _waited = await _live_reading(incident)
    if _round_owed(incident, evidence, st, _feed_reading(incident.printer_id)) != "resume_on_fed":
        return None
    landed = evidence.landed_load
    if landed is None:
        return None
    logger.info(
        "spool_recovery: printer %s — no candidate left, and the farm's replacement (tray %s) reads fed at the "
        "nozzle; resuming on it instead of giving up",
        incident.printer_id,
        landed,
    )
    return await _resume_and_read(incident, landed, evidence=evidence)


# How the swap round's resume ended (:func:`_resume_and_read`): the incident's outcome was
# written (``over`` — recovered, ended by the driver, stood aside, handed over), or the round
# moves on to the next candidate (``next_round``).
RoundTail = Literal["over", "next_round"]


# --- the round top: ONE verdict of what the round owes (F8, 2026-10-16) ------------------------------
#
# What a round does is decided at its TOP, from three facts, through ONE total table (a missing cell
# RAISES, the ``pause_recovery._OWED`` idiom), consulted only there — at the top of every round, after a
# release (the ladder's ``released`` goes back to the top, never straight into a swap), and where the swap
# finds nothing to load (no candidate, every round spent):
#
# * ``end_of_print`` — the fault stood in the printer's END-OF-PRINT sequence (:func:`_end_of_print`: the
#   row's durable ``fault_layer`` at the job's last layer). Nothing is left to print: ``vendor_answer_only``
#   — the printer's own dialog answered (the resume rung, :data:`_LEVERS` ``resume``), never a swap and
#   never a refill load (004-H2S incident 621: swapped, paused three times by the farm, handed to a person).
# * ``in_change`` — the AMS holds the print's filament change (``bambu_mqtt.ams_mid_filament_change``):
#   ``ladder`` — nothing but release verbs goes into a held change (012-H2S 2026-09-23).
# * ``farm_load_fed`` — AFTER A RELEASE (item 5), the farm's OWN landed load is what the feed state reads at
#   the nozzle: a lever read ``released`` after the load landed (:attr:`_RecoveryEvidence.release_onto_landed_load`),
#   no farm resume sent since that release (the record — ``printer_incidents.last_farm_action``: one resume
#   pair per release, so a replacement that re-jams after it goes back to the swap's next candidate as
#   before), and ``FeedReading.toolhead_fed`` from that tray: ``resume_on_fed`` — the replacement is at the
#   nozzle, so the round resumes on it: no unload, no "no other spool" give-up (014-H2S #620: released
#   02:26:44 with ``changing -> fed feeder=0`` at 02:26:43, then the farm unloaded that fed spool and gave
#   up; 005-H2S #597 / #603 the same; 513/514/528/529/536/538/540 gave up ``candidates_exhausted`` /
#   ``unload_failed`` over the fed replacement). Otherwise ``swap`` — a release on the ORIGINAL spool keeps
#   today's contract: select, commit, unload, load, resume (operator 2026-09-23: release, then swap to a
#   different spool), and so does a replacement the swap round's own resumes left re-jammed with no
#   release (the next candidate, unchanged).
RoundOwed = Literal["ladder", "resume_on_fed", "swap", "vendor_answer_only"]


@dataclass(frozen=True)
class RoundFacts:
    """The three facts the round verdict reads (:data:`_ROUND_OWED`'s key). ``farm_load_fed`` — after a
    release not yet resumed on, the farm's own landed load reads fed at the nozzle."""

    end_of_print: bool
    in_change: bool
    farm_load_fed: bool


# THE round table — TOTAL over :class:`RoundFacts` (``test_spool_recovery`` pins it), every cell spelled.
_ROUND_OWED: dict[RoundFacts, RoundOwed] = {
    RoundFacts(end_of_print=True, in_change=True, farm_load_fed=True): "vendor_answer_only",
    RoundFacts(end_of_print=True, in_change=True, farm_load_fed=False): "vendor_answer_only",
    RoundFacts(end_of_print=True, in_change=False, farm_load_fed=True): "vendor_answer_only",
    RoundFacts(end_of_print=True, in_change=False, farm_load_fed=False): "vendor_answer_only",
    RoundFacts(end_of_print=False, in_change=True, farm_load_fed=True): "ladder",
    RoundFacts(end_of_print=False, in_change=True, farm_load_fed=False): "ladder",
    RoundFacts(end_of_print=False, in_change=False, farm_load_fed=True): "resume_on_fed",
    RoundFacts(end_of_print=False, in_change=False, farm_load_fed=False): "swap",
}


def _round_owed_cell(facts: RoundFacts) -> RoundOwed:
    """The table's answer for ``facts``. A cell the table does not spell out RAISES — a shape nobody wrote
    a rule for is drift to be seen, never a default to fall into."""
    try:
        return _ROUND_OWED[facts]
    except KeyError:
        raise LookupError(f"spool_recovery: no round verdict for {facts!r} — every cell needs its own row") from None


def _round_owed(
    incident: RecoveryIncident, evidence: _RecoveryEvidence, st: PrinterState, reading: FeedReading | None
) -> RoundOwed:
    """What this round owes, read from the live state ``st``, the feed state ``reading``, the incident's
    evidence ledger and the record (:data:`_ROUND_OWED`). Logged once per ask — the round's own triage line."""
    landed = evidence.landed_load
    release = evidence.release_onto_landed_load
    facts = RoundFacts(
        end_of_print=_end_of_print(incident, st),
        in_change=ams_mid_filament_change(st),
        farm_load_fed=(
            landed is not None
            and _release_not_yet_resumed(incident, release)
            and reading is not None
            and reading.toolhead_fed
            and reading.feeder == landed
        ),
    )
    owed = _round_owed_cell(facts)
    logger.info(
        "[spool_recovery] round printer=%s owed=%s (end_of_print=%s in_change=%s farm_load=%s release=%s phase=%s "
        "feeder=%s)",
        incident.printer_id,
        owed,
        facts.end_of_print,
        facts.in_change,
        landed,
        f"{release.lever}@{release.seq}" if release is not None else None,
        reading.phase.value if reading is not None else None,
        reading.feeder if reading is not None else None,
    )
    return owed


def _release_not_yet_resumed(incident: RecoveryIncident, release: LeverStep | None) -> bool:
    """Is ``release`` (a lever read ``released`` after the farm's landed load) one the farm has not resumed
    on yet — no farm ``resume`` ACTION on the row sent after the release's own action (the record's ids are
    its send order; the release's own resume, when the released lever was a resume, is not after it)? The
    once of ``resume_on_fed`` per release, read off the record's DB-free projection
    (``printer_incidents.last_farm_action``). A release with no recorded action decides nothing: ``False``."""
    if release is None or release.action_id is None:
        return False
    last_resume = printer_incidents.last_farm_action(incident.printer_id, incident.incident_id, "resume")
    return last_resume is None or last_resume.action_id <= release.action_id


# How the round top ended (:func:`_round_top`): the round's swap goes out (``swap``), the incident's
# outcome was written (``over``), or the round ended with the next one owed (``next_round``).
RoundTop = Literal["swap", "over", "next_round"]


async def _round_top(incident: RecoveryIncident, evidence: _RecoveryEvidence) -> RoundTop:
    """THE round top (F8, 2026-10-16): ask :func:`_round_owed` and do what it owes — the release ladder
    (``released`` comes back HERE, so a release is decided by the same table, never a fall-through into the
    swap), the resume on the farm's fed replacement (:func:`_resume_and_read`), the printer's dialog answer
    alone at the end of print (:func:`_answer_the_end_of_print`), or the swap (the caller's). A release does
    not spend a round: the candidate budget (:data:`_MAX_CANDIDATES`) stays the swaps'."""
    while True:
        st, _waited = await _live_reading(incident)
        owed = _round_owed(incident, evidence, st, _feed_reading(incident.printer_id))
        match owed:
            case "swap":
                return "swap"
            case "resume_on_fed":
                landed = evidence.landed_load
                if landed is None:  # the table read it a moment ago; a ledger never un-lands a load
                    raise LookupError("spool_recovery: resume_on_fed with no landed farm load on the ledger")
                logger.info(
                    "spool_recovery: printer %s — the farm's replacement (tray %s) reads fed at the nozzle; resuming "
                    "on it (no unload)",
                    incident.printer_id,
                    landed,
                )
                return "over" if await _resume_and_read(incident, landed, evidence=evidence) == "over" else "next_round"
            case "vendor_answer_only":
                answered = await _answer_the_end_of_print(incident, evidence)
                if answered is not None:
                    return answered
                continue
            case "ladder":
                end = await _release_ladder(incident, evidence=evidence)
                ladder = end.exit
                match ladder:
                    case "released":
                        continue  # the AMS is out of the change: the round top decides what follows
                    case "self_healed":
                        await _close_self_healed(incident)
                        return "over"
                    case "swapped":
                        printing = _swapped_tray(end.read) if end.read is not None else None
                        if printing is not None and printing == evidence.landed_load:
                            # The tray printing is the FARM's own replacement (its landed load — 014-H2S #620,
                            # 005-H2S #597 / #603: the release let the printer finish loading it): the SWAP's
                            # success, through its one owner (mapping rewritten, ``driver_swap``, the swap
                            # notification), never a self-heal (2026-10-16).
                            logger.info(
                                "spool_recovery: printer %s RUNNING fed from the farm's replacement (tray %s) after a "
                                "release verb — the swap recovered",
                                incident.printer_id,
                                printing,
                            )
                            await _succeed(incident, printing)
                            return "over"
                        # No farm load is printing, so another tray at the feeder is the FIRMWARE's own switch
                        # (its backup group). The print runs on a quiet path; it is the self-heal close, said
                        # out loud.
                        logger.warning(
                            "spool_recovery: printer %s RUNNING on a quiet path with tray %s feeding and nothing "
                            "committed — the firmware switched feeders itself; closing as a self-heal",
                            incident.printer_id,
                            getattr(_get_state(incident.printer_id), "tray_now", None),
                        )
                        await _close_self_healed(incident)
                        return "over"
                    case "unreleased":
                        await _give_up(incident, "wedge_unreleased", evidence=evidence)
                        return "over"
                    case "withheld":
                        # The farm emptied this extruder and the AMS is in a change again: no verb may resume
                        # over it, and no swap may go into it. The loads are what failed — the reason says so.
                        await _give_up(incident, evidence.exhaustion_reason(), evidence=evidence)
                        return "over"
                    case "ended":
                        await _close_ended_by_driver(incident, evidence=evidence)
                        return "over"
                    case "stopped":
                        # The restart rung ended a farm job that had deposited nothing — its release. The row
                        # stays open through the continuation.
                        await _restart(incident, evidence=evidence)
                        return "over"
                    case "abort":
                        await _abort(incident)
                        return "over"
                    case "handover":
                        _hand_over(incident)
                        return "over"
                    case "air_stopped":
                        return "over"  # the air stop wrote the row's outcome (closed after the terminal, or escalated)
                    case _:
                        assert_never(ladder)
            case _:
                assert_never(owed)


async def _answer_the_end_of_print(incident: RecoveryIncident, evidence: _RecoveryEvidence) -> RoundTop | None:
    """``vendor_answer_only`` (item 6 of 2026-10-16): a fault in the printer's END-OF-PRINT sequence is
    answered with the printer's own dialog answer alone — the resume rung (:data:`_LEVERS` ``resume``:
    "Problem solved, resume" where the catalog lists it for the standing code, else a plain resume), read
    by THE reader, whose success is the job's FINISH (:func:`_terminal_read`). Never a swap, never another
    release verb, never a refill load (``refill_verdict.refill_owed`` answers ``last_layer`` here): nothing is
    left to print. ``None`` — the answer did not take (the printer stayed or went back to PAUSE, or the frame
    never reached it): the round top asks again, and once the resume rung is spent the printer is handed to
    a person (``end_of_print_unfinished``, no refill)."""
    if evidence.lever_spent("resume"):
        logger.info(
            "spool_recovery: printer %s — the fault stood in the end-of-print sequence and the printer's dialog was "
            "answered; it is still paused — handed to a person (no swap, no refill)",
            incident.printer_id,
        )
        await _give_up(incident, "end_of_print_unfinished", evidence=evidence)
        return "over"
    logger.info(
        "spool_recovery: printer %s — the fault stood in the end-of-print sequence (the job's last layer): answering "
        "the printer's dialog only, no swap",
        incident.printer_id,
    )
    read = await _read_after(incident, "resume", evidence=evidence, budgeted=True)
    match read.reading:
        case "self_healed" | "swapped":
            await _close_self_healed(incident)
            return "over"
        case "ended":
            await _close_ended_by_driver(incident, evidence=evidence)
            return "over"
        case "abort":
            await _abort(incident, token=read.takeover)
            return "over"
        case "handover":
            _hand_over(incident)
            return "over"
        case "air_stopped":
            return "over"  # the air stop wrote the row's outcome
        case "wedged" | "released" | "no_pause" | "not_sent":
            return None
        case "stopped":
            raise LookupError("the end-of-print answer read 'stopped' — only a rung that ends the job can")
        case _:
            assert_never(read.reading)


async def _resume_and_read(incident: RecoveryIncident, target: int, *, evidence: _RecoveryEvidence) -> RoundTail:
    """The contract's resume once ``target`` is at the toolhead — the SAME reader
    (:func:`_read_after`, ``budgeted=False``: not a release attempt, so the lever budget does not
    apply), reading the contract's own verb for ``swapped``, with one retry (the live 16:21:24 →
    16:22:57 recovery, where the first resume did not stick). Shared by the round's own load and
    by a round whose top saw the previous driver's pending load run (item 2 of 2026-10-10: the
    contract continues at the resume, never a second load)."""
    pid = incident.printer_id
    first = await _read_after(incident, "resume", evidence=evidence, budgeted=False)
    while first.reading == "not_sent":
        # The resume never reached the printer (it did not go out, or the record voided it): the reader
        # returned on the printer's next session, so it is sent again now — once per session event.
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
        case "air_stopped":
            return "over"  # the air stop wrote the row's outcome
        case "stopped":
            raise LookupError("the swap round's resume read 'stopped' — only a rung that ends the job can")
        case _:
            assert_never(first.reading)

    retry = await _read_after(incident, "resume", evidence=evidence, budgeted=False)
    while retry.reading == "not_sent":
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
        case "air_stopped":
            return "over"  # the air stop wrote the row's outcome
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
# at — and it has NO bound (2026-10-11: "using time as a gate is not the correct way to
# discern status complete or incompleteness"; production gave up ``printer_offline`` past the
# offline-stall window, and the 2026-10-12 restoration keeps the rewrite's "recovery continues after
# an offline gap"): the printer's next session answers it, and a long wait is the attention owner's
# to page, never this driver's to give up on. Offline time never counts toward a step bound either
# (``printer_action.online_seconds_since``). The process dying stays :func:`reevaluate_session`'s
# (the row's next session start re-enters it).


async def _live_reading(incident: RecoveryIncident) -> tuple[PrinterState, float]:
    """THE driver's read of the wire: a LIVE state (:func:`live_reading.reads_live`), and how long the
    read waited for one (``0.0`` when the printer was reporting) — a caller that holds a reading
    across reads (the lever window's held success) drops it after a gap, because a reading seen
    before the gap is not one held through it.

    A reading that is not live is "not evidence yet": the gate polls at :data:`_POLL_INTERVAL_S`
    until the printer reports on a live session again, however long that takes. One INFO line when
    a gap opens and one when it closes, so "why did recovery sit for 4 minutes" is answered in the
    log.
    """
    pid = incident.printer_id
    st = _get_state(pid)
    if st is not None and live_reading.reads_live(st):
        return st, 0.0
    gap_started = _now()
    logger.info(
        "[spool_recovery] printer %s not reporting on a live session (connected=%s epoch=%s report_epoch=%s "
        "state=%s) — recovery waits for its next session",
        pid,
        getattr(st, "connected", None),
        getattr(st, "connection_epoch", None),
        getattr(st, "report_epoch", None),
        getattr(st, "state", None),
    )
    while True:
        await asyncio.sleep(_POLL_INTERVAL_S)
        st = _get_state(pid)
        if st is not None and live_reading.reads_live(st):
            waited = _now() - gap_started
            logger.info(
                "[spool_recovery] printer %s reporting again after %.1fs (epoch=%s) — recovery continues",
                pid,
                waited,
                getattr(st, "connection_epoch", None),
            )
            return st, waited


# --- step helpers -----------------------------------------------------------

# The two live states the driver's PROCEDURE runs in. A lever moves PAUSE→RUNNING and
# the reader's pause moves RUNNING→PAUSE, so every intermediate reading of the machine's
# own verbs lands inside this pair, and everything outside it is a TERMINAL. Whose
# terminal depends on where it is read. In the steps' own loops it is somebody else's
# act (``command_outcome.takeover``'s ``job_ended``). Inside a release verb's window
# (:func:`_read_after`) it is not: a verb with an unmeasured effect (``clean_print_error``,
# ``ams_control`` abort / reset / pause) can end the print, and the driver records that
# itself (``ended``) — unless the operator's Stop mark says the terminal was theirs.
_DRIVER_STATES: tuple[str, ...] = ("PAUSE", "RUNNING")

# The live states in which the printer positively holds NO job — the rule table's own set
# (``incident_resolution._JOB_OVER_STATES``, imported above) is what the restart
# continuation (:func:`_restart`) works in once the driver's own stop has ended the job;
# anything else (a job preparing, running or paused) on that printer is somebody else's.


def _printed_layer(printer_id: int) -> int | None:
    """The tracked job's current layer (``BambuMQTTClient.job_peaks``' job-gated ``layer_num`` — a
    predecessor's republished layer is never this job's), or ``None`` with no client."""
    client = printer_manager.get_client(printer_id)
    if client is None:
        return None
    return int(client.job_peaks().layer_num or 0)


def _prints_fed(printer_id: int, st: PrinterState) -> bool:
    """RUNNING outside a filament change with the toolhead fed (the feed state's ``toolhead_fed`` — FED or
    TAIL, invariant 16): a reading under which an advancing layer is the print printing — the firmware's own
    proof the path carries filament. A fault code still standing does not refute it (the firmware leaves a
    code standing after healing itself), and the firmware's own change never advances a layer."""
    if _live_state(st) != "RUNNING" or ams_mid_filament_change(st):
        return False
    reading = _feed_reading(printer_id)
    return reading is not None and reading.toolhead_fed


# How the fault's own pause ended (:func:`_await_fault_pause`). Closed.
FaultPause = Literal["paused", "rescued", "ended", "air", "unpaused"]


async def _await_fault_pause(incident: RecoveryIncident) -> FaultPause:
    """Read the printer, report by report, until the fault's PAUSE — or an event that says no pause is
    coming. The printer's events decide first, every one of them:

    * ``paused`` — the printer reports PAUSE: the fault paused the print;
    * ``rescued`` — the print ADVANCED A LAYER fed (:func:`_prints_fed`): the firmware rescued it
      itself (its backup switch landed — the feed state's ``auto_switched`` — or the fault cleared)
      and the print prints on;
    * ``ended`` — the job reached a terminal: nothing is left to recover;
    * ``air`` — the print RUNS ON AIR (the feed state's AIR: a layer printed with nothing fed,
      invariant 16) under the fault and the firmware has not paused it: the caller STOPS it
      (:func:`_stop_printing_on_air`) — nobody else will, since the per-push detector defers to this
      live driver.

    The fed run restarts on any other reading and on a session gap (a layer seen before the gap was
    not printed through it). Anything else — the firmware's own change in flight, a print not yet at
    its next layer — is waited for, up to the STEP BOUND (:func:`step_bound`, anchored at the driver's
    start, ``RecoverySettings``) in the printer's online seconds: with none of the events above by
    then, ``unpaused`` — production's verdict (``337f4954``: the PAUSE wait bounded by
    ``step_timeout_s``), the caller's "never PAUSEd — firmware rescue / transient" (2026-10-12,
    Raymond: "if it was self heal, it should stay self heal"). An event the bound's own poll reads
    still wins: the bound is asked only after every event exit.
    """
    pid = incident.printer_id
    bound = step_bound(incident.settings)
    fed_from: int | None = None
    while True:
        st, waited = await _live_reading(incident)
        live = _live_state(st)
        if live == "PAUSE":
            return "paused"
        if live not in _DRIVER_STATES:
            return "ended"
        if _prints_on_air(pid, st):
            logger.info(
                "spool_recovery: printer %s prints on AIR under the fault (tray_now=%s) and the firmware has not "
                "paused it — the farm stops it",
                pid,
                getattr(st, "tray_now", None),
            )
            return "air"
        if waited or not _prints_fed(pid, st):
            fed_from = None
        else:
            layer = _printed_layer(pid)
            if fed_from is None:
                fed_from = layer
            elif layer is not None and layer > fed_from:
                logger.info(
                    "spool_recovery: printer %s printed layer %s fed (tray_now=%s) — the firmware rescued the "
                    "print; no pause is coming",
                    pid,
                    layer,
                    getattr(st, "tray_now", None),
                )
                return "rescued"
        if printer_action.bound_due(pid, bound, incident.started_at):
            printer_action.note_bound_passed(pid, bound, lane="spool_recovery.fault_pause")
            return "unpaused"
        await asyncio.sleep(_POLL_INTERVAL_S)


def _driver_reading(incident: RecoveryIncident, st: PrinterState) -> DriverReading:
    """What :func:`command_outcome.takeover` reads of one LIVE reading (the session gate's,
    :func:`_live_reading` — a disconnect or a new session's cached fields are not somebody else's
    act, they are "not evidence yet", and that is the gate's wait): the live word, this driver's
    job against the wire's, the store's kind for its row, the hijack field, the RECORD's operator
    AMS command since the driver began, and the rule table's quiet-path reading."""
    kind = printer_incidents.cached_kind(incident.printer_id, incident.incident_id)
    return DriverReading(
        live=_live_state(st),
        job=same_job(getattr(st, "subtask_id", None), incident.job_id),
        reclassified=kind is not None and kind != incident.kind,
        pending_tray_target=getattr(st, "pending_tray_target", None),
        operator_commanded=printer_action.operator_commanded_since(incident.printer_id, incident.started_at),
        path_quiet=incident_resolution.path_quiet(st),
    )


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
    """Ask :func:`command_outcome.takeover` at a driver BOUNDARY and, when it answers, end the driver.

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
    token = command_outcome.takeover(_driver_reading(incident, st), awaiting=awaiting)
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

    The record voided the command (``session_changed``: a later session began with it
    unacknowledged, or a reboot emptied the AMS's queue): nobody can tell whether it ran, so
    nothing is inferred — not a success, not a failure, and not a takeover. The step stays on the log answered ``session_changed`` and the driver starts
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
    stick, which is not the contract's success — that read waits for its next event (a
    re-PAUSE, a layer printed on air) like any other.
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


# The reader's arms that end its watch for a farm act (:func:`_read_after`): ``path_empty`` —
# ``resume_then_pause``'s first RUNNING sample with nothing at the feeder (the firmware's retract on a
# loaded stall, the first sample on an empty path), paused; ``air`` — the feed state reads AIR (a layer
# printed with nothing fed, invariant 16), for every lever: the print is STOPPED, never paused
# (:func:`_stop_printing_on_air`, operator ruling 2026-10-12); ``load_not_held`` — the swap contract's own resume
# printed a layer FED from the JAMMED tray: the contract's load did not stick, so the print is paused
# back for the next round; ``hung`` — THE KEPT 009 ARM (:func:`_empty_path_hang`): at the STEP BOUND after a
# RELEASE verb the print RUNS with the ACTIVE extruder EMPTY — nothing fed and no firmware sequence, or the
# change posture standing over an empty extruder no load has reached since the verb went out — on a path
# where no farm load landed (the 009-H2S hang, 2026-07-20: a resume into an EMPTY path, the posture up, hung
# RUNNING ~2.5 min; a farm pause got the AMS out — production's arm at its deadline, ``337f4954``).
#
# What the ``hung`` arm NO LONGER pauses (2026-10-16, operator-approved, reversing the 2026-10-12 restoration
# for the fed path): a print RUNNING inside the printer's OWN sequence with filament on its way or at the
# toolhead — the change after a farm load or resume, a load that landed in the window, the end-of-print
# retract, the start block, the runout switch, a tool change (``FeedReading.firmware_owns_motion``).
# 70 farm "hung RUNNING" pauses in 30 days of production (09-25..10-16) freed a stuck change 0 times: every
# recovery came from the printer finishing its own load on a later resume (122-128 s from load to nozzle,
# the same as an uninterrupted load), from an AMS verb sent while PAUSED, or from the firmware completing
# RUNNING at the read — and the pause itself produced the stuck states (004-H2S #621: three farm pauses
# inside the end-of-print retract at layer 167/167). The reader waits for the printer's event instead; a
# sequence that never ends is the attention owner's page (``farm_stall.check_waiting_on_printers``).
PauseArm = Literal["path_empty", "air", "load_not_held", "hung"]


def _empty_path_hang(
    reading: FeedReading | None, *, budgeted: bool, farm_load_landed: bool, landed_before: int | None
) -> bool:
    """THE kept 009 arm's reading, asked only AT the step bound (F3, 2026-10-16) — True when:

    * the read is a RELEASE verb's (``budgeted``), never the swap round's own resume;
    * NO farm load landed on this path (``farm_load_landed`` False — the incident's last completed motion
      is not a farm load, :attr:`_RecoveryEvidence.landed_load`, read off the evidence ledger. A cursor taken
      at the resume's send would miss the swap round, whose load lands BEFORE its resume: 014-H2S #619 load
      02:15:09, resume 02:15:35, and the printer's own load of it reached the nozzle at 02:17:17);
    * the feed state reads the ACTIVE extruder EMPTY, one of two ways:

      - ``EMPTY_UNCONFIRMED`` — RUNNING, nothing fed, no change posture, runout, tail, start block or end
        retract, and no layer printed (a layer printed empty is AIR, the air stop's);
      - ``CHANGING`` with cause ``POSTURE`` (the AMS's change posture up — the RECORDED 009 shape: the
        rung-1 resume into an empty path with the posture at 1), no feeder at the active extruder
        (``reading.feeder is None``), and no load landed on it since the verb went out (the feed state's
        ``LoadLanded`` event newer than ``landed_before``, its sequence number read at the send — the
        printer's OWN landing inside this verb's window, not the farm-load cursor above). Never a
        ``CHANGING`` of another cause (``RUNOUT_SWITCH`` / ``RUNOUT_RESUME`` — the firmware's runout;
        ``TOOL_CHANGE``), never ``TAIL``, ``RUNOUT_HELD``, ``START_BLOCK`` or ``END_RETRACT``.

    Never one ``tray_now`` frame: a loaded stall's retract cycles ``tray_now`` 2 → 255 → 2 every ~12 s
    (shape 44), and its landing back on the feeder is the event that rules the posture case out. No
    reading is no evidence: False — the reader keeps waiting for an event."""
    if not budgeted or farm_load_landed or reading is None:
        return False
    if reading.phase is FeedPhase.EMPTY_UNCONFIRMED:
        return True
    if (
        reading.phase is not FeedPhase.CHANGING
        or reading.cause is not ChangeCause.POSTURE
        or reading.feeder is not None
    ):
        return False
    landed = reading.load_landed
    return landed is None or (landed_before is not None and landed.seq <= landed_before)


def _landing_seq(printer_id: int) -> int | None:
    """The sequence number of the feed state's last ``LoadLanded`` event (``None`` before the first) — read
    at a verb's send, so the kept 009 arm can tell a load that landed inside the verb's own window
    (:func:`_empty_path_hang`)."""
    reading = _feed_reading(printer_id)
    landed = reading.load_landed if reading is not None else None
    return landed.seq if landed is not None else None


def _total_layers(st) -> int:
    """The job's total layer count off the wire (0 when unknown)."""
    return tray_fields.parse_int_field(getattr(st, "total_layers", None)) or 0


def _end_of_print(incident: RecoveryIncident, st) -> bool:
    """Did this incident's fault stand in the printer's END-OF-PRINT sequence — its row's durable
    ``fault_layer`` at or past the job's ``total_layers`` (``printer_incidents.faulted_at_last_layer``, the one
    predicate; the live ``END_RETRACT`` phase is gone once the printer paused)? Then nothing is left to
    print: the round answers the printer's dialog only (:func:`_round_owed`), and the job's FINISH is the
    success (:func:`_terminal_read`). 004-H2S incident 621, 2026-10-15: an overload during the end-of-print
    retract at layer 167/167, swapped, paused three times by the farm, then handed to a person."""
    return printer_incidents.faulted_at_last_layer(
        incident.printer_id, _total_layers(st), incident_id=incident.incident_id
    )


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
    end_of_print: bool = False,
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
    already emptied. A fault that stood in the END-OF-PRINT sequence (``end_of_print``,
    :func:`_end_of_print`) has nothing left to print, so its FINISH is the success whatever the
    feeder reads — ``self_healed`` (no swap; 2026-10-16, 004-H2S #621). Any other terminal is the
    operator's Stop when a Stop witness says so (:func:`_operator_stopped` — today's ``job_ended``
    abort), and otherwise the driver's own verb ended the print — ``ended``, which the driver
    records itself.
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
        if end_of_print:
            logger.info(
                "spool_recovery: printer %s reached FINISH inside the %s window — the fault stood in the "
                "end-of-print sequence and the job completed",
                incident.printer_id,
                lever,
            )
            return LeverRead("self_healed", moved=True, position=fed if fed is not None else position)
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
    action_id: int,
    sent_at: datetime,
    entry_ams: int | None,
    published_at: float,
    budgeted: bool,
    farm_load_landed: bool,
    landed_before: int | None = None,
    pause_published: bool = False,
) -> LeverRead | PauseArm:
    """Read the wire after a verb went out until a DECISIVE reading — or one of the pause arms, which
    :func:`_read_after` publishes. The printer's events decide first, however late they come (the old
    window read a verb the printer carried out late as one it ignored); the STEP BOUND
    (:func:`step_bound`, anchored at the verb's send, ``sent_at``, in the printer's online seconds) is
    the self-heal fallback production took at its deadline (2026-10-12) — on a PAUSED printer (the
    next rung), never as an act on a printer RUNNING its own sequence (2026-10-16).

    ``farm_load_landed`` — a farm load landed on this incident's path (:attr:`_RecoveryEvidence.landed_load`):
    the kept 009 arm stays off (:func:`_empty_path_hang`). ``landed_before`` — the feed state's last
    ``LoadLanded`` sequence number at the verb's send (:func:`_landing_seq`): a load the printer landed inside
    this window keeps the arm off over a posture change. ``pause_published`` — the re-watch after the
    reader's own pause did not land while the printer ran its own sequence (:func:`_paused_read`): no pause
    arm fires again (the pause is out, and its late answer is still the record's); a RUNNING print with the
    toolhead empty and no firmware sequence then reads ``no_pause``, every other reading waits for its
    event as before.

    Every poll, in order: a terminal first (``ended`` / the operator's ``abort`` / a FINISH success —
    :func:`_terminal_read`), then :func:`command_outcome.takeover` (state lost, re-classified, another
    job, an operator's AMS command), then the RECORD's final answer to the verb itself
    (``printer_action.settled``, action ``action_id``):

    * ``taken_over`` — a person's command took the printer: the ``operator_command`` takeover;
    * ``void`` — the printer never received the verb (a later session began with it unacknowledged)
      or rebooted under it: ``not_sent`` — the pull never reached the printer, so the ladder sends it
      again (:meth:`_RecoveryEvidence.lever_spent` counts only pulls the printer received);
    * ``refused`` at a PAUSE — the printer said no: the PAUSE reading, ``moved`` as seen (spent).

    Then the reading:

    * RUNNING — a real feeder (:func:`_success_reading`) is the success once the print ADVANCES A
      LAYER on it with the toolhead fed (:func:`_prints_fed`: the feed state's FED / TAIL outside a
      change — printing on is the firmware's own proof, and a fault code the firmware left standing
      does not refute it); the feeder is the ACTIVE extruder's (:func:`_feeding_position`, K1). In
      the swap contract's own resume (not ``budgeted``) a layer printed fed from the JAMMED tray is
      no success: the contract's load did not stick → ``load_not_held``.
      For a lever with :attr:`LeverSpec.pause_on_empty_path`, a RUNNING sample whose feeder reads
      ``empty`` → ``path_empty``, quiet or not: on a loaded stall that is the firmware's own retract
      of the jammed filament (012-H2S 2026-09-25, ``tray_now`` 2 → 255 every ~12 s after a resume),
      on an empty path it is the first sample, and a quiet RUNNING with nothing fed prints air
      (shape 39). The feed state reading AIR (a layer printed with nothing fed) → ``air``, for every
      lever — the reader STOPS that print (:func:`_stop_printing_on_air`). AT THE BOUND, production's
      success verdict: a print RUNNING FED on a real feeder on a quiet path whose next layer has not
      come yet reads its success at once ("the stable hold cannot outlast the window" — a jammed tray
      only for a release verb, :func:`_success_reading`; a READING at the bound, never an act on the
      printer); and the kept 009 arm, ``hung`` (:func:`_empty_path_hang`: a release verb, no farm
      load landed, the toolhead empty with no firmware sequence). A print RUNNING inside its OWN
      sequence (``FeedReading.firmware_owns_motion`` — the change after a farm load or resume, the
      end-of-print retract, the start block, the runout switch) is NEVER paused on the clock: the
      reader waits for the printer's event (its PAUSE, filament at the nozzle and a layer, AIR, the
      terminal). Anything else — a fed print on a path still loud — keeps waiting for its event.
    * PAUSE after the printer LEFT it, or with the AMS's state word changed → ``wedged`` /
      ``released`` at once (:func:`_pause_reading`). A verb the record answers by its ACK (the AMS
      levers — ``command_outcome.Family.AMS_LEVER``) reads the PAUSE on the first status push AFTER its
      ACK (the feed state is stepped once per push, so a new reading object is that push), ``moved``
      False. PAUSE never left by the bound — the resume rung, the swap contract's own resume, an
      ACK-answered lever that never ACKed, a restart stop that never landed — reads the PAUSE then,
      ``moved`` as seen (production's deadline reading), and the ladder takes its next rung. In the
      swap contract's own resume read (not ``budgeted``), a PAUSE after the resume ran on a QUIET path
      is another actor's (:func:`command_outcome.takeover`'s ``paused_elsewhere``) — the reader's
      phase ``saw_running`` is what it is handed.

    Every reading is LIVE (:func:`_live_reading`). A session gap drops a held success in progress: a
    layer seen before the gap is not one printed through it; and a gap never counts toward the bound.
    """
    pid = incident.printer_id
    jammed = incident.jammed_global_tray
    bound = step_bound(incident.settings)
    answered_by_ack = command_outcome.spec_of(spec.command().name).family is command_outcome.Family.AMS_LEVER
    # A re-watch begins on a printer the reader already saw RUNNING (its pause did not land).
    left = pause_published
    saw_running = pause_published
    last_fed: FeederPosition | None = None
    # The feeder a fed RUNNING run is printing from, and the layer it was first seen at.
    holding: tuple[str, int] | None = None
    # The feed reading of the push on which the verb's ACK was first seen answered (an ACK-answered
    # verb): the PAUSE is read on the first push after it.
    acked_on: FeedReading | None = None
    ack_seen = False

    def _note_fallback(verdict: str) -> None:
        printer_action.note_bound_passed(
            pid, bound, lane=f"spool_recovery.lever={lever} {verdict}", action_id=action_id
        )

    while True:
        st, waited = await _live_reading(incident)
        if waited:
            holding = None
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
                end_of_print=_end_of_print(incident, st),
            )
        # RUNNING is expected here; a quiet PAUSE after the CONTRACT's resume ran is not.
        token = command_outcome.takeover(
            _driver_reading(incident, st), awaiting=None, after_contract_resume=not budgeted and saw_running
        )
        if token is not None:
            return LeverRead(
                _note_takeover(incident, token, f"lever={lever}"), moved=left, position=position, takeover=token
            )
        answer = await printer_action.settled(action_id)
        if answer is command_outcome.Answer.TAKEN_OVER:
            return LeverRead(
                _note_takeover(incident, "operator_command", f"lever={lever}"),
                moved=left,
                position=position,
                takeover="operator_command",
            )
        if answer is command_outcome.Answer.VOID:
            logger.info(
                "spool_recovery: printer %s lever %s (action %s) void — the printer never received it, or rebooted "
                "under it; the ladder sends it again",
                pid,
                lever,
                action_id,
            )
            return LeverRead("not_sent", moved=left, position=position)
        # The fallback is asked only after every event of this poll: an event still wins at the bound.
        at_bound = printer_action.bound_due(pid, bound, sent_at)
        if live == "RUNNING":
            left = True
            saw_running = True
            quiet = incident_resolution.path_quiet(st)
            success = _success_reading(position, quiet=True, budgeted=budgeted)
            reading = _feed_reading(pid)
            phase = reading.phase if reading is not None else FeedPhase.UNKNOWN
            layer = _printed_layer(pid)
            fed_run = position.kind in ("jammed", "other") and _prints_fed(pid, st) and layer is not None
            if fed_run and layer is not None:
                if holding is None or holding[0] != position.kind:
                    holding = (position.kind, layer)
                elif layer > holding[1]:
                    if success is not None:
                        return LeverRead(success, moved=True, position=position)
                    if pause_published:
                        # The reader's pause is already out and did not land; it is never sent twice.
                        return LeverRead("no_pause", moved=True, position=position)
                    logger.info(
                        "spool_recovery: printer %s printed layer %s from the jammed tray %s after the swap's load "
                        "— the load did not stick; pausing it back",
                        pid,
                        layer,
                        position.global_tray,
                    )
                    return "load_not_held"
                held_success = _success_reading(position, quiet=quiet, budgeted=budgeted)
                if at_bound and held_success is not None:
                    # Production's deadline reading: the print runs fed on a quiet path and its next layer
                    # has not come inside the bound — the stable hold cannot outlast the window.
                    _note_fallback(held_success)
                    return LeverRead(held_success, moved=True, position=position)
            else:
                holding = None
                if spec.pause_on_empty_path and position.kind == "empty" and not pause_published:
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
                if _prints_on_air(pid, st):
                    logger.info(
                        "spool_recovery: printer %s printed a layer with nothing fed after lever %s (tray_now=%s "
                        "ams_status_main=%s quiet=%s) — the print is on air",
                        pid,
                        lever,
                        getattr(st, "tray_now", None),
                        getattr(st, "ams_status_main", None),
                        quiet,
                    )
                    return "air"
                if pause_published:
                    if reading is not None and phase is not FeedPhase.UNKNOWN and not reading.firmware_owns_motion:
                        # The printer's own sequence ended and nothing feeds: the reader's pause did not
                        # land, and the print is no longer the firmware's to finish.
                        logger.info(
                            "spool_recovery: printer %s left its own sequence after lever %s with the farm's pause "
                            "unlanded (phase=%s tray_now=%s) — no_pause",
                            pid,
                            lever,
                            phase.value,
                            getattr(st, "tray_now", None),
                        )
                        return LeverRead("no_pause", moved=True, position=position)
                elif at_bound and _empty_path_hang(
                    reading, budgeted=budgeted, farm_load_landed=farm_load_landed, landed_before=landed_before
                ):
                    # THE KEPT 009 ARM at the step bound: a release verb's resume onto a path where no farm
                    # load landed, RUNNING with the ACTIVE extruder empty (nothing fed and no firmware
                    # sequence, or the change posture over an empty extruder no load reached since the send).
                    # Over filament on its way or at the toolhead — a change after a farm load, a load landed
                    # in the window, the end retract, the start block, a runout — the reader never pauses: it
                    # waits for the printer's event (2026-10-16).
                    logger.info(
                        "spool_recovery: printer %s hung RUNNING after lever %s with the active extruder empty "
                        "(tray_now=%s ams_status_main=%s phase=%s quiet=%s) — pausing it to read the AMS",
                        pid,
                        lever,
                        getattr(st, "tray_now", None),
                        getattr(st, "ams_status_main", None),
                        phase.value,
                        quiet,
                    )
                    _note_fallback("hung")
                    return "hung"
        else:
            holding = None
            if getattr(st, "ams_status_main", None) != entry_ams:
                left = True
            # A rung whose verb ends the job waits for its terminal: the AMS leaving the change on
            # the way there (the stop ends the change) is not a release.
            if left and not spec.ends_job:
                return LeverRead(_pause_reading(st), moved=left, position=position)
            if answer is command_outcome.Answer.REFUSED:
                logger.info(
                    "spool_recovery: printer %s refused lever %s (action %s) — %s",
                    pid,
                    lever,
                    action_id,
                    await printer_action.reason_of(action_id) or "no reason given",
                )
                return LeverRead(_pause_reading(st), moved=left, position=position)
            if answered_by_ack and answer is command_outcome.Answer.REACHED:
                push = _feed_reading(pid)
                if not ack_seen:
                    ack_seen, acked_on = True, push
                elif push is not acked_on:
                    return LeverRead(_pause_reading(st), moved=left, position=position)
            if at_bound:
                # PAUSE never left by the bound (production's deadline reading): a resume that stayed
                # PAUSEd, a lever never ACKed, a stop that never landed. The ladder takes its next rung.
                reading_at_bound = _pause_reading(st)
                _note_fallback(reading_at_bound)
                return LeverRead(reading_at_bound, moved=left, position=position)
        await asyncio.sleep(_POLL_INTERVAL_S)


async def _paused_read(
    incident: RecoveryIncident, lever: Lever, *, landed: bool, published_at: float, budgeted: bool
) -> LeverRead | None:
    """What the reader's own pause brought back. Landed → the PAUSE reading; otherwise a
    terminal, a takeover, ``None`` or ``no_pause``. A LIVE reading (:func:`_live_reading`). The
    PAUSE here is the reader's OWN, so :func:`command_outcome.takeover` is never handed the contract
    phase: its quiet PAUSE is not another actor's.

    ``None`` — KEEP WATCHING (item 3 of 2026-10-16, F7): the pause did not land (refused, or unanswered by
    the step bound) and the printer RUNS its own sequence (``FeedReading.firmware_owns_motion`` — a change,
    the end-of-print retract, the start block, a runout). ``no_pause`` there would send the ladder's next
    verb into the printer's own routine; the caller re-watches the printer for its event instead
    (:func:`_watch_lever` with ``pause_published``). ``no_pause`` stays the answer of a print running with
    no firmware sequence."""
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
            end_of_print=_end_of_print(incident, st),
        )
    token = command_outcome.takeover(_driver_reading(incident, st), awaiting=None)
    if token is not None:
        return LeverRead(
            _note_takeover(incident, token, f"lever={lever}"), moved=True, position=position, takeover=token
        )
    reading = _feed_reading(pid)
    if live == "RUNNING" and reading is not None and reading.firmware_owns_motion:
        logger.info(
            "spool_recovery: printer %s — the farm's pause after lever %s did not land and the printer runs its own "
            "sequence (phase=%s); the reader waits for its event, no next verb",
            pid,
            lever,
            reading.phase.value,
        )
        return None
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

    The verb is the farm's ACTION on the row (``printer_action``) and the reading is the first
    decisive EVENT after it (:func:`_watch_lever` — the witness CONTINUEs re-PAUSEd at 47–87 s,
    012-H2S 2026-09-23, and a verb the printer carries out late is read when it does), with the
    STEP BOUND as the self-heal fallback production took at its deadline (2026-10-12). The pause
    arms publish the farm pause HERE (:func:`_farm_pause`, the one pause body) and read the PAUSE it
    brings back (:func:`_paused_read`): a pause the printer does not answer within the step bound
    has not landed (production's ``no_pause``, unless a terminal or a takeover reads otherwise), and
    its late answer is still the record's. The air arm STOPS the print instead
    (:func:`_stop_printing_on_air`): a stop that wrote the row's outcome reads ``air_stopped``, a
    person's command ``abort``, and a print no longer on air at the send is read as it stands. A frame that never left the farm
    (no client, a failed publish) reads ``not_sent`` once the printer's NEXT session begins
    (``printer_action.await_next_session``) — the pull never reached the printer, so the ladder sends it
    again then: once per session event, never per clock.

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
    # Recorded FIRST (its row id is the frame's sequence id), so the step that spends the lever names
    # the action before the frame goes out — a terminal the stop produces always meets a ledger that
    # already says the farm sent it.
    prepared = await printer_action.prepare(pid, spec.command(), actor=ACTOR_FARM, incident_id=incident.incident_id)
    seq = await evidence.note(LeverStep.draft(lever, action_id=prepared.action_id)) if budgeted else None
    st, _waited = await _live_reading(incident)
    entry_ams = getattr(st, "ams_status_main", None)
    entry_sub = getattr(st, "ams_status_sub", None)
    entry_tray = getattr(st, "tray_now", None)
    # The kept 009 arm's cursor (F3, 2026-10-16): a farm load that landed on this incident's path, off the
    # evidence ledger — never a seq taken here (the swap round's load lands BEFORE its resume).
    farm_load_landed = evidence.landed_load is not None
    # The printer's own landings counted from here on are ones inside this verb's window (the posture case of
    # the kept 009 arm: a load that reached the toolhead keeps it off).
    landed_before = _landing_seq(pid)
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
    went_out = (await printer_action.publish(prepared)).published
    if not went_out:
        logger.warning(
            "spool_recovery: printer %s lever %s did not go out (no client, or the publish failed) — it is sent "
            "again on the printer's next session",
            pid,
            lever,
        )
        read = LeverRead("not_sent", moved=False, position=_feeding_position(st, incident.jammed_global_tray))
    else:
        # ``pause_published``: the reader's own pause went out and did not land while the printer ran its
        # own sequence (:func:`_paused_read` answered ``None``) — the reader keeps watching for the
        # printer's event, and no pause arm fires twice (F7, 2026-10-16: no next verb into the printer's
        # own routine).
        pause_published = False
        pending: LeverRead | None = None
        while pending is None:
            watched = await _watch_lever(
                incident,
                spec,
                lever,
                action_id=prepared.action_id,
                sent_at=prepared.sent_at,
                entry_ams=entry_ams,
                published_at=published_at,
                budgeted=budgeted,
                farm_load_landed=farm_load_landed,
                landed_before=landed_before,
                pause_published=pause_published,
            )
            match watched:
                case LeverRead():
                    pending = watched
                case "path_empty" | "load_not_held" | "hung":
                    # Production's landing (``337f4954``): the pause the printer does not answer within the
                    # step bound has not landed.
                    paused, _reason = await _farm_pause(incident, bound=step_bound(incident.settings))
                    landed = paused is command_outcome.Answer.REACHED
                    pending = await _paused_read(
                        incident, lever, landed=landed, published_at=published_at, budgeted=budgeted
                    )
                    pause_published = True
                case "air":
                    # A layer printed on air: the print is ruined, so it is STOPPED and the unit retried
                    # (operator ruling 2026-10-12) — the air policy's one exit, not a step of the ladder.
                    stopped = await _stop_printing_on_air(incident, evidence)
                    position = _feeding_position(_get_state(pid), incident.jammed_global_tray)
                    match stopped:
                        case "stopped" | "escalated":
                            pending = LeverRead("air_stopped", moved=True, position=position)
                        case "taken_over":
                            pending = LeverRead(
                                _note_takeover(incident, "operator_command", f"lever={lever}"),
                                moved=True,
                                position=position,
                                takeover="operator_command",
                            )
                        case "not_air":
                            st, _waited = await _live_reading(incident)
                            pending = await _paused_read(
                                incident,
                                lever,
                                landed=_live_state(st) == "PAUSE",
                                published_at=published_at,
                                budgeted=budgeted,
                            )
                        case _:
                            assert_never(stopped)
                case _:
                    assert_never(watched)
        read = pending
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
    if not went_out:
        # The session the frame failed to go out on — read at the send, never after it: a session that
        # turned in between is the very event this wait is for.
        await printer_action.await_next_session(
            incident.printer_id, prepared.entry.connection_epoch, recheck_s=_POLL_INTERVAL_S
        )
    return read


# How the release ladder ended (:func:`_release_ladder`). Closed. ``stopped`` is the
# restart rung's release: the continuation (:func:`_restart`) takes the incident from there.
LadderExit = Literal[
    "self_healed",
    "swapped",
    "released",
    "unreleased",
    "withheld",
    "ended",
    "stopped",
    "abort",
    "handover",
    "air_stopped",
]


@dataclass(frozen=True)
class LadderEnd:
    """How the release ladder ended (:func:`_release_ladder`): the closed exit, and the lever read that
    decided it when one did — what a ``swapped`` exit SAW feeding (:func:`_swapped_tray`), so the round top
    can tell the farm's own replacement printing (the swap's success) from the firmware's own backup switch
    (a self-heal). ``None`` for an exit decided after the rungs (``released`` / ``unreleased`` / a takeover
    / ``withheld``)."""

    exit: LadderExit
    read: LeverRead | None = None


async def _release_ladder(incident: RecoveryIncident, *, evidence: _RecoveryEvidence) -> LadderEnd:
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
        return LadderEnd("withheld")
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
                case (
                    "self_healed" | "swapped" | "released" | "ended" | "stopped" | "abort" | "handover" | "air_stopped"
                ):
                    return LadderEnd(reading, read)
                case "wedged" | "no_pause" | "not_sent":
                    continue
                case _:
                    assert_never(reading)
    st, _waited = await _live_reading(incident)
    token = command_outcome.takeover(_driver_reading(incident, st), awaiting="PAUSE")
    if token is not None:
        return LadderEnd(_note_takeover(incident, token, "release_ladder"))
    if not ams_mid_filament_change(st):
        return LadderEnd("released")
    logger.warning(
        "[spool_recovery] ladder exhausted printer=%s levers=%s — every release verb pulled and the AMS still "
        "holds the print's filament change (ams_status=%s/%s tray_now=%s); nothing sent into it",
        pid,
        ",".join(f"{s.lever}:{s.outcome}" for s in evidence.lever_steps) or "-",
        getattr(st, "ams_status_main", None),
        getattr(st, "ams_status_sub", None),
        getattr(st, "tray_now", None),
    )
    return LadderEnd("unreleased")


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
                    job_ended=_job_ended("wedge_ended_print", stop_ended=False),
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

    1. The stop's terminal (the reading that brought the driver here) is the release; from here
       the printer is read in the ``job_over`` phase (:data:`Awaiting`): its terminal is the
       driver's own, and a job on it is not.
    2. The unload of the stalled feeder, through the swap round's own helper
       (:func:`_unload_and_confirm` → ``printer_action.unload``, dual-nozzle aware, answered by
       the record — sent into a change the terminal has not ended yet, it is held and runs at the
       release), NEVER skipped (``after_stop``). It goes out before any
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
    """Read the stopped job's reports until the AMS is out of the print's change — the job's terminal
    ends it (013-H2S's loaded ``1/5`` was held until one) — or the STEP BOUND passes (:func:`step_bound`,
    anchored here, in the printer's online seconds). Returns either way (production, ``337f4954``): the
    unload that follows is answered in whatever posture it finds, and a command held inside the change
    there is a give-up (``restart_unload_failed``), never a resend. The event decides first; the bound
    only ends the wait for it."""
    pid = incident.printer_id
    bound = step_bound(incident.settings)
    anchor = printer_action.now()
    while True:
        st, _waited = await _live_reading(incident)
        if not ams_mid_filament_change(st):
            return
        if printer_action.bound_due(pid, bound, anchor):
            printer_action.note_bound_passed(pid, bound, lane="spool_recovery.stop_settled")
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
      Not a sighting of the path running: a stalled job's own pre-stall RUNNING samples
      (heating, levelling on a quiet path) would qualify as one, so it could never bound a
      second stall.
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
    pre-flight (the record refuses a send with no client itself).
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
    tray (:func:`_feeding_position`) — the entry ``command_outcome.posture`` keys the answer by and
    ``command_outcome.ran`` reads back, so the record and the answer describe one extruder. On an
    H2C the jammed tray on the deputy beside an empty active nozzle records ``empty``, never
    ``jammed`` (an unload then reads as run the moment it is sent); a fed extruder on a row
    that names no jammed tray records ``other``, so its unload's run stays readable."""
    return _feeding_position(_get_state(incident.printer_id), incident.jammed_global_tray).kind


async def _unload_and_confirm(
    incident: RecoveryIncident, *, evidence: _RecoveryEvidence, attempts: int, after_stop: bool = False
) -> UnloadStep:
    """Unload the feeder through ``printer_action.unload`` and read the wire's answer.

    ``skipped`` (nothing to unload — :func:`_unload_skippable`) / ``drying`` (the AMS is
    drying: a doomed lane) / a :data:`DriverMotion` for the last publish that went out
    / ``refused`` (every publish this call made was an refused send (``printer_action``) — each one
    consumes an attempt at once, no confirm wait) / ``abort`` / ``handover``.

    Up to ``attempts`` publishes: an unload the AMS ran and never completed (``acted``), one the
    printer refused or nothing moved on by the step bound (``no_movement``) are resent. ``held`` is
    NEVER resent — a second command behind a queued one runs too (012-H2S 2026-09-23) — and what
    follows it is decided by the posture it was SENT into (:func:`_held_outcome`). ``session_changed``
    is not resent HERE either: the record voided it, and the driver's next round re-reads the AMS
    before anything else goes out. Every other answer returns at once. Every publish that went out is
    a :class:`CommandStep` on the evidence log, noted at the send.

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
        sent = await printer_action.send(
            incident.printer_id,
            printer_action.unload(),
            actor=ACTOR_FARM,
            incident_id=incident.incident_id,
            bound=step_bound(incident.settings),
        )
        if not sent.published:
            logger.warning(
                "spool_recovery: printer %s unload not sent (%s) — attempt consumed",
                incident.printer_id,
                sent.refusal,
            )
            verdict = "refused"
            continue
        seq = await evidence.note(CommandStep.draft("unload", None, _feeder_kind(incident), action_id=sent.action_id))
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
    sent: printer_action.Sent,
    seq: int,
    *,
    evidence: _RecoveryEvidence,
    awaiting: Awaiting,
) -> Literal["held", "complete", "session_changed", "abort", "handover"]:
    """What a command read ``held`` at the step bound (step ``seq``, already on the log) comes to —
    decided by the posture it was SENT into (``bambu_mqtt.ams_mid_filament_change`` over the send's
    entry snapshot, the one origin of "mid-change"), production's rule (``337f4954``):

    * INSIDE the print's own filament change: it waits behind the print's head and runs at the next
      release, which only a human causes there (free the spool, Retry). The step bound was its wait for
      the head to run it — ``held``: the caller gives up (``swap_held_after_release``; in the restart
      continuation ``restart_unload_failed``), the page naming the queued command, and the row stays
      open on it (``printer_incidents.pending_command``).
    * OUTSIDE a change: the AMS accepted it and has not run it, and it runs on its own (011/014-H2S
      2026-10-09/10) — the driver waits for it (:func:`_await_pending_command`, UNBOUNDED by the
      operator's 2026-10-10 ruling: nothing is sent behind a command still queued, and a give-up there
      walked away from a pull-back that later emptied the toolhead) and a run is ``complete``.
    """
    if ams_mid_filament_change(sent.entry):
        return "held"
    waited = await _await_pending_command(incident, evidence.command_step(seq), evidence=evidence, awaiting=awaiting)
    match waited:
        case "ran":
            return "complete"
        case "void":
            # The printer rebooted under it, or the record voided it: the queue is gone, the command
            # will never run, and its step reads ``session_changed`` — the round re-reads the wire.
            return "session_changed"
        case "undecidable":
            # A held command was ACKNOWLEDGED with its entry feeder able to move (the motion reading
            # holds only those), so the wire always answers whether it ran.
            raise LookupError("a held command settled 'undecidable' — only an unload sent with nothing loaded can")
        case "abort" | "handover":
            return waited
        case _:
            assert_never(waited)


async def _observe_command(
    incident: RecoveryIncident,
    command: MotionCommand,
    target: int | None,
    sent: printer_action.Sent,
    *,
    step: str,
    awaiting: Awaiting = "PAUSE",
) -> DriverMotion | StepTakeover:
    """THE driver's read of one published AMS motion command: the RECORD's answer to it
    (``printer_action``, over ``command_outcome``'s motion cells) on an event, and — with none by the
    STEP BOUND the command was sent with (:func:`step_bound`, armed on the action by the record) — the
    record's motion reading at the bound, production's deadline answer (2026-10-12; ``337f4954``
    bounded this read with ``step_timeout_s``). A late answer still lands on the record.

    Every poll asks :func:`command_outcome.takeover` first, then the record's final answer:

    * ``reached`` — the toolhead shows it ran (``command_outcome.ran``, or the feed state's landing
      of the target): ``complete``. An unload sent from OUTSIDE a change is complete once the AMS is
      out of the change its own cycle ran (the next load is never sent into it); an unload sent into
      a mid-change AMS with nothing loaded reads ``undecidable`` — nothing physical answered it.
    * ``refused`` — the printer said no (a fail ACK): ``no_movement``.
    * ``void`` — the printer never received it (a later session began unacknowledged) or rebooted
      under it (the AMS's queue is gone): ``session_changed`` — the round re-reads the wire first.
      The record's VOID carries why: a command sent OUTSIDE a change whose own change cycle RAN and
      ENDED without completing (``command_outcome.UNFINISHED_CYCLE``, read on a push that applied a
      fresh AMS block) is ``acted`` — the AMS ran it and it did not complete.

    Then the bound: once the record passed it with the command still unanswered
    (``printer_action.await_outcome(sent, bound=...)`` answers ``Unanswered`` at once), its reading is
    the step's — ``acted`` / ``no_movement`` (the caller resends, tries the next candidate, gives up),
    ``held`` (decided by the posture it was sent into, :func:`_held_outcome`), ``undecidable``,
    ``complete`` or ``session_changed``. The reading is the record's own (it reads the motion over the
    entry snapshot it took at the publish); the driver re-derives none.

    ``awaiting`` is the phase the command runs in (:data:`Awaiting`): the swap round's, the job
    PAUSEd under it, or the restart continuation's, the job ended by the driver's stop. One
    ``answer=`` line per command (``command_outcome.log_answer``, the one format of it).
    """
    entry = sent.entry
    outside = not ams_mid_filament_change(entry)
    bound = step_bound(incident.settings)
    # An unload sent into a mid-change AMS with nothing loaded at the active extruder: nothing physical can
    # answer it.
    nothing_to_answer = command == "unload" and not outside and entry.feed.kind != "fed"
    while True:
        st, _waited = await _live_reading(incident)
        token = command_outcome.takeover(_driver_reading(incident, st), awaiting=awaiting, target=target)
        if token is not None:
            return _note_takeover(incident, token, step)
        in_change = ams_mid_filament_change(st)
        answer = await printer_action.settled(sent.action_id)
        reading: DriverMotion | None = None
        match answer:
            case None:
                if printer_action.bound_passed(sent.action_id):
                    reading = await _motion_at_bound(sent, bound)
            case command_outcome.Answer.VOID if (
                await printer_action.reason_of(sent.action_id)
            ) == command_outcome.UNFINISHED_CYCLE:
                reading = "acted"  # the record voided it: its own change cycle ended without completing
            case command_outcome.Answer.REACHED:
                if nothing_to_answer:
                    reading = "undecidable"
                elif not (command == "unload" and outside and in_change):
                    reading = "complete"
            case command_outcome.Answer.REFUSED:
                reading = "no_movement"
            case command_outcome.Answer.VOID:
                reading = "session_changed"
            case command_outcome.Answer.TAKEN_OVER:
                # The record never answers a farm motion command ``taken_over`` (a person's AMS command
                # leaves it outstanding — K11, ``command_outcome``'s table): a read that sees one is a
                # table that changed shape, never a verdict.
                raise LookupError(f"the record answered the farm's {command} (action {sent.action_id}) taken_over")
            case _:
                assert_never(answer)
        if reading is not None:
            now = command_outcome.snapshot(st)
            command_outcome.log_answer(
                actor="driver",
                printer_id=incident.printer_id,
                command=command,
                target=target,
                entry=entry,
                now=now,
                answer=reading,
                elapsed_s=now.taken_at - entry.taken_at,
            )
            return reading
        await asyncio.sleep(_POLL_INTERVAL_S)


async def _motion_at_bound(sent: printer_action.Sent, bound: printer_action.SelfHealBound) -> DriverMotion | None:
    """The record's reading of a motion command whose step bound passed unanswered
    (``printer_action.await_outcome(sent, bound=...)`` — its ``Unanswered.reading``, the motion
    reading the record took over the entry snapshot it captured at the publish), or ``None`` when an
    event answered it in the meantime (the caller reads that answer on its next poll). A reading that is
    no motion reading (a non-motion action's ``sent`` / ``acked``) is a record that changed shape: it
    RAISES."""
    outcome = await printer_action.await_outcome(sent, bound=bound)
    if isinstance(outcome, command_outcome.Answer):
        return None
    reading = outcome.reading
    if isinstance(reading, str) and _is_answer(reading):
        logger.info(
            "spool_recovery: printer %s %s (action %s) unanswered by %s — the record reads %s",
            sent.printer_id,
            sent.command,
            sent.action_id,
            outcome.bound_name,
            reading,
        )
        return reading
    raise LookupError(f"the record read the farm's {sent.command} (action {sent.action_id}) {reading!r} at its bound")


# How a wait on the farm's OWN pending command ended (:func:`_await_pending_command`). Closed.
# ``ran`` — the wire shows the command ran (``command_outcome.ran``), answered ``complete`` on the
# log; ``undecidable`` — an unload sent with nothing loaded, and the toolhead still empty:
# nothing physical can answer it and its running can pull nothing back, answered
# ``undecidable`` (the classifier's own token for that); ``void`` — the printer rebooted (the
# power-loss prompt) or the record answered it void / refused, so the command will never run:
# answered ``session_changed``; ``abort`` / ``handover`` — ``command_outcome.takeover`` (or the
# record's ``taken_over``)
# (an operator's act, the job ending, a re-classified row), exactly as it ends every other wait
# of the driver.
PendingWait = Literal["ran", "undecidable", "void", "abort", "handover"]

# The record's final answer of a pending command, as the driver's wait reads it: reached → it ran;
# void / refused → it will never run (a reboot emptied the AMS's queue, the printer refused it);
# taken over → an operator's AMS command took the AMS (the driver's ``operator_command`` takeover).
_PENDING_BY_ANSWER: dict[command_outcome.Answer, PendingWait] = {
    command_outcome.Answer.REACHED: "ran",
    command_outcome.Answer.VOID: "void",
    command_outcome.Answer.REFUSED: "void",
    command_outcome.Answer.TAKEN_OVER: "abort",
}


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
    resume, the read). This is the driver's ONE deliberately UNBOUNDED wait (operator ruling
    2026-10-10, "time is not the right signal", kept by the 2026-10-12 restoration of every other
    step bound): no fallback can be taken over a command the AMS still holds — anything sent behind
    it runs too, and walking away from it is what left the toolhead empty. It ends only on an EVENT:

    * the command RAN — ``command_outcome.ran`` over the step's own ledger columns and the live
      toolhead (``tray_fields.toolhead_feed``, the ACTIVE extruder): the step is re-answered
      ``complete`` through the log's one writer — the classifier's own completion token, which
      is what "held, then ran" settles to (the C1 contract: "it then answers complete"); the
      ``held`` reading stays in the ``[ams-command] answer=held`` line and this module's;
    * a takeover (:func:`command_outcome.takeover`, asked on EVERY poll exactly as :func:`_observe_command`
      does): somebody else's act, or the job's end — ``abort`` / ``handover``, the caller's to
      answer as everywhere else.

    Every read goes through the session gate (:func:`_live_reading`): a gap is a wait, however
    long; the printer's next session answers it.

    It decides nothing on a clock and pages nothing: paging is the attention owner's (``farm_stall``,
    over the record's unanswered actions).
    """
    pid = incident.printer_id
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
        token = command_outcome.takeover(
            _driver_reading(incident, st), awaiting=awaiting, target=step.target if step.command == "load" else None
        )
        if token is not None:
            await _settle_cut_short(incident, step.seq, evidence=evidence)
            return _note_takeover(incident, token, f"await_{step.command}")
        settles = command_outcome.pending_settles(
            step.command, step.target, step.loaded_at_send, tray_fields.toolhead_feed(st).active
        )
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
        answered = await _record_answer(step)
        if answered is not None:
            waited = _PENDING_BY_ANSWER[answered]
            await evidence.answer(step.seq, "complete" if waited == "ran" else "session_changed")
            logger.info(
                "[spool_recovery] printer %s: the record answered the %s %s (step %s, action %s) %s",
                pid,
                state_word,
                step.command,
                step.seq,
                step.action_id,
                answered.value,
            )
            return waited
        if command_outcome.rebooted(getattr(st, "hms_errors", None)):
            # A step with no action (written before the record): the reboot witness voids it here.
            await evidence.answer(step.seq, "session_changed")
            logger.info(
                "[spool_recovery] printer %s rebooted (the power-loss prompt stands) — the farm's queued %s (step %s) "
                "is void: the AMS's queue is gone, so it will never run",
                pid,
                step.command,
                step.seq,
            )
            return "void"
        await asyncio.sleep(_POLL_INTERVAL_S)


async def _record_answer(step: CommandStep) -> command_outcome.Answer | None:
    """The record's final answer of a pending command step's action — ``None`` while it is outstanding,
    or for a step with no action (written before the record existed)."""
    if step.action_id is None or printer_action.outstanding(step.action_id):
        return None
    return await printer_action.answer_of(step.action_id)


async def _settle_cut_short(incident: RecoveryIncident, seq: int, *, evidence: _RecoveryEvidence) -> None:
    """A takeover cut command ``seq``'s observation short, so the classifier never answered it.
    Settle what the wire ALREADY shows: a command the toolhead shows RAN (``command_outcome.ran``
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
    if printer_incidents.command_pends(step.answer) and command_outcome.ran(
        step.command, step.target, step.loaded_at_send, tray_fields.toolhead_feed(st).active
    ):
        await evidence.answer(seq, "complete")
        logger.info(
            "spool_recovery: printer %s the %s (step %s) cut short by a takeover had already run — answered complete",
            incident.printer_id,
            step.command,
            seq,
        )


def _outstanding_s(step: CommandStep) -> float:
    """How long ``step`` has been out, measured from its durable send stamp on the record's clock
    (``printer_action.now``, naive UTC like the ledger's ``sent_at``) — so a restart does not reset it.
    A log figure only; 0 for a step with none."""
    if step.at is None:
        return 0.0
    return max(0.0, (printer_action.now() - step.at).total_seconds())


async def _confirm_unloaded(
    incident: RecoveryIncident, sent: printer_action.Sent, *, awaiting: Awaiting = "PAUSE"
) -> DriverMotion | StepTakeover:
    """Read what the record answered the unload ``sent``, in the ``awaiting`` phase
    (:func:`_observe_command`). ``tray_now == 255`` alone is never completion — after a feed fault it
    already reads 255 before the unload (009-H2S 2026-07-20): the record reaches an unload sent with
    a feeder loaded when that feeder empties, and one sent with nothing loaded on the printer's ACK
    over an empty toolhead."""
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
      enrolls any present-but-bare tray, waits for it to gain a ``tray_type`` in live
      telemetry or for the config lane to answer its write
      (:func:`_await_bare_tray_configured`), and re-scans once before escalating.
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
    # (bypassing only the retry window), wait for one to gain a tray_type (or for the
    # lane's answer to every write), then re-scan a single time. Still nothing →
    # escalate exactly as before.
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
    """Read the printer, report by report, until a forced bare slot ADOPTS its config (a non-empty
    ``tray_type`` live) — or the config lane has answered every forced slot without one: its write
    epoch ENDED (``spool_tagless.autoconfig_ended`` — a ``fail`` ACK, or the lane's own adoption
    verdict), so nothing more is coming. Read in the caller's phase (``awaiting`` — the swap round's
    PAUSE, or the restart continuation's ended job). With neither by the STEP BOUND
    (:func:`step_bound`, anchored at the wait's start, in the printer's online seconds) the sweep
    yields nothing — production's plain timeout (``337f4954``); the events decide first.

    Returns ``(live state, takeover token)``: the state on success, else ``(None, token)`` when
    :func:`command_outcome.takeover` ended the wait and ``(None, None)`` when every forced slot's
    write was answered without an adoption, or the bound passed. The caller's own path is unchanged —
    a ``None`` state means "no candidate from this sweep" either way — but the TOKEN is what names WHY
    the sweep stopped. Every poll is LIVE (:func:`_live_reading`)."""
    from backend.app.services import spool_tagless

    pid = incident.printer_id
    bound = step_bound(incident.settings)
    anchor = printer_action.now()
    while True:
        st, _waited = await _live_reading(incident)
        token = command_outcome.takeover(_driver_reading(incident, st), awaiting=awaiting)
        if token is not None:
            _note_takeover(incident, token, "await_bare_tray_configured")
            return None, token
        if _any_slot_configured(st, forced_slots):
            return st, None
        if all(spool_tagless.autoconfig_ended(pid, ams_id, tray_id) is not None for ams_id, tray_id in forced_slots):
            logger.info(
                "spool_recovery: printer %s forced bare-tray config on %s answered without an adoption",
                pid,
                forced_slots,
            )
            return None, None
        if printer_action.bound_due(pid, bound, anchor):
            printer_action.note_bound_passed(pid, bound, lane="spool_recovery.bare_tray_config")
            logger.info(
                "spool_recovery: printer %s forced bare-tray config on %s not adopted by the step bound — no "
                "candidate from this sweep",
                pid,
                forced_slots,
            )
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
    """Load ``target`` through ``printer_action.load`` and read the wire's answer.

    ``drying`` (a drying target unit is a doomed lane — reported so the caller escalates
    instead of burning attempts) / a :data:`DriverMotion` for the last publish that
    went out / ``refused`` (every publish was an refused send (``printer_action``) — each consumes
    an attempt at once, no confirm wait) / ``abort`` / ``handover``.

    Up to ``max_attempts`` publishes (the live incident needed two sends before the load
    took): ``acted``, ``no_movement`` (refused, or nothing moved by the step bound) and a send that
    did not go out are resent; ``held`` is NEVER resent — inside the print's change it is returned (it
    runs at the next release), outside one the driver waits for it to run (:func:`_held_outcome`).
    The record (``printer_action``) owns the send and the entry snapshot, and a landing of the
    commanded tray is attributed to the load there (never a firmware switch); a
    ``pending_tray_target`` that turns into something other than ``target`` is
    ``command_outcome.takeover``'s ``hijacked_load``. Every publish that went out is a
    :class:`CommandStep` on the evidence log, noted at the send. ``session_changed``
    returns at once, never resent here: the driver's next round re-reads the AMS first.
    """
    refusal = await _wait_ams_write_window(incident.printer_id, _ams_unit_for_tray(target))
    if refusal == _REFUSAL_DRYING:
        return "drying"

    verdict: LoadStep = "refused"
    for _ in range(max(1, incident.settings.max_attempts)):
        sent = await printer_action.send(
            incident.printer_id,
            printer_action.load(target),
            actor=ACTOR_FARM,
            incident_id=incident.incident_id,
            bound=step_bound(incident.settings),
        )
        if not sent.published:
            logger.warning(
                "spool_recovery: printer %s load of tray %s not sent (%s) — attempt consumed",
                incident.printer_id,
                target,
                sent.refusal,
            )
            verdict = "refused"
            continue
        seq = await evidence.note(CommandStep.draft("load", target, _feeder_kind(incident), action_id=sent.action_id))
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
                # Only an unload into an empty change reads this; a read that changed shape must
                # fail loudly, never be read as a verdict.
                raise LookupError("the driver read a load 'undecidable' — only an unload into an empty change can")
            case _:
                assert_never(answer)
    return verdict


async def _confirm_loaded(
    incident: RecoveryIncident, target: int, sent: printer_action.Sent
) -> DriverMotion | StepTakeover:
    """Read what the record answered the load of ``target`` ``sent`` (:func:`_observe_command`):
    ``complete`` once the active feeder IS the target, in every posture; at the step bound with no
    event, the record's motion reading (``held`` / ``acted`` / ``no_movement``)."""
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
        # Bar re-entry for this exact fault until the wire re-arms it (see _blocked): the closed row
        # IS the bar; the cache lets the sampler watch for its re-arm edge. Stamped BEFORE the await so
        # a DB failure cannot leave the loop unbounded within this process.
        _blocked.setdefault((incident.printer_id, incident.job_id), {})[incident.fingerprint] = incident.incident_id
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
       that was faulty raise the gate and let human fix?" — and its purpose, in his own words
       (2026-10-10): "this was said solely to prevent empty tool head resumes for all
       scenarios". The farm puts the job's own spool BACK so that no resume, from any source,
       prints air: a RELOAD, never a rule that a person must fix what the farm can reload.
       Not ``last_loaded_tray`` while a jammed tray is
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


def _refill_failed(incident: RecoveryIncident, tray: int | None, command: MotionCommand, answer: str) -> RefillFailed:
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
    3. The load, through the swap's own helper (:func:`_load_and_confirm` → ``printer_action.load``,
       its drying pre-flight, sent through the record as a farm action and answered by it; a load
       the AMS accepted and has not run is waited on there).
    4. "Reached the toolhead" = the load answered ``complete`` AND the ACTIVE extruder reads fed
       from the target (``tray_fields.toolhead_feed`` — on a dual nozzle a load can complete onto
       the other nozzle) → :class:`RefillLoaded`; else :class:`RefillFailed`.

    Every command is a step on this incident's ledger (the helpers note it at the send). Takeovers
    propagate (``abort`` / ``handover``). The caller establishes the preconditions — the toolhead
    reads empty, no command pends, no change is in flight (the AMS owns the toolhead's next motion
    there, and anything sent into it is held: 012-H2S 2026-09-23). A session gap is waited for
    (:func:`_live_reading`); a command the record voids across it answers :class:`RefillFailed`
    (``session_changed``).

    The refill's load is a command that went out, but it is NOT a candidate in the swap's
    accounting that matters: the give-up chooses its reason before the refill runs.
    """
    pid = incident.printer_id
    command: MotionCommand
    st, _waited = await _live_reading(incident)
    tray, source = await _refill_pick(incident, evidence, st)
    if tray is None:
        st, _waited = await _live_reading(incident)
        token = command_outcome.takeover(_driver_reading(incident, st), awaiting="PAUSE")
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
    the MEASURED toolhead. The escalations that never enter the loop (the entry gate, the runout
    branch) call :func:`_escalate` directly with no refill and no evidence: they sent nothing;
    the startup re-entry passes the hydrated log.

    **The refill (K8, 2026-10-10; T1 of the plan).** When a person's Resume would print AIR, the
    driver runs :func:`_refill_toolhead` BEFORE it pages — the farm never pages with the toolhead
    empty when a spool will load. Whether it would is K7's (:func:`refill_verdict.refill_owed`,
    trigger ``T3`` — the give-up's question IS "would a person's Resume print air"), never a
    private subset of its exclusions: the toolhead's feed state reads empty (``EMPTY_PAUSED`` /
    ``AIR``), no filament change in flight (``CHANGING`` — the AMS owns the toolhead's next motion
    there and anything sent into it is held behind the print's head, 012-H2S 2026-09-23), no runout
    demand, no queued farm command, no reboot prompt, no maintenance mode, a live session. Two DRIVER
    preconditions sit before it, each an event-state, never a time:

    * no command PENDS on the driver's OWN log (``_RecoveryEvidence.pending_command``) — a command
      the AMS holds runs when it runs, a refill sent behind it would run too, and its refill is the
      hand-over's when it does (K11). Stricter than K7's ``command_pending`` on purpose: that reads
      the open-row projection SETTLED against the wire (a pull-back the toolhead shows ran is no
      longer a queue to a detector), while a step this log still holds unanswered is the driver's
      own, and a step that ran is answered through the log's one writer by the hand-over's D1 (T2)
      and refilled there — never refilled behind here with the step left pending;
    * no restart stop that ENDED the job (:meth:`_RecoveryEvidence.restart_ended`, read ONCE here
      through the record) — after the farm's OWN stop ended it (operator ruling R1 2026-09-29,
      shape 45) no resume exists to print air, an unload the continuation completed stays done,
      and the row is held physical with the path empty. A stop the printer never TOOK (still
      outstanding past the step bound, the job PAUSEd — 2026-10-12, AR-H3) ended nothing: the job
      is live, so this is the paused-job give-up — the refill, and the AMS hold over that job.

    Asking K7 has one deliberate consequence: before the first layer (``START_BLOCK``) it answers
    ``before_first_layer``, so a give-up there refills nothing — the start block loads the
    filament itself, as on every other resume.

    **A named reversal.** The restore this replaces (``_restore_jammed_feeder``, the 2026-09
    agent design) "undid only the driver's own motion": it reloaded only after a FARM unload
    moved the AMS, so a toolhead the firmware retracted — or a queued pull-back the AMS ran on
    its own, 011/014-H2S 2026-10-09/10 — was paged empty as "slot N is loaded". That rule was an
    agent's design, never an operator ruling, and it is reversed: an empty toolhead is refilled
    whoever emptied it, because the operator's resume prints air either way.

    :func:`_abort` and :func:`_hand_over` deliberately leave the extruder as it is — another
    actor owns the printer, and a takeover is never a give-up. A takeover DURING the refill
    aborts with ``restored=True``: the operator resumed on a feeder the FARM had just loaded.

    A give-up after the restart stop ENDED the job also re-classifies the row a PHYSICAL hold
    before it pages (:func:`_hold_the_path`): a jam row is a wire-class JOB hold, which the sweep
    closes on an idle quiet printer, and the job it held is already over — the printer must stay
    held until the path is shown clear. A stop that ended nothing keeps the row the AMS hold.
    """
    # The counters are read BEFORE the refill: the refill load is not a candidate and must not
    # feed the numbers the reason was chosen from.
    unloads_sent, unloads_confirmed = evidence.unloads_sent, evidence.confirmed_unloads
    loads_attempted, loads_confirmed = evidence.loads_attempted, evidence.loads_confirmed
    # The restart continuation's own reasons are reached only after the stop ended the job.
    stop_ended = reason in _JOB_ENDED_REASONS or await _stop_ended(incident, evidence)
    refill: RefillVerdict | None = None
    if not stop_ended and evidence.pending_command is None:
        pid = incident.printer_id
        if refill_verdict.refill_owed(pid, _get_state(pid), trigger="T3", reading=_feed_reading(pid)).owed:
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
    if stop_ended:
        await _hold_the_path(incident)
    await _escalate(incident, reason, refill=result, evidence=evidence, stop_ended=stop_ended)


async def _hold_the_path(incident: RecoveryIncident) -> None:
    """Re-classify the open row a PHYSICAL hold, before a give-up after the restart stop ENDED the job
    (:meth:`_RecoveryEvidence.restart_ended` — the caller's one reading of the stop's answer; a stop
    the printer never took leaves the job live and this is never called for it).

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
    printer_words: str | None = None,
    stop_ended: bool | None = None,
) -> None:
    """Give up: hold the incident ESCALATED, project the token, notify, leave PAUSED.

    NEVER resumes — a human must intervene. The incident stays OPEN (an escalation is
    a live hold, not a closed fault), which is what makes the printer un-re-enterable
    by a sibling code, keeps the printer-card chip lit, and arms the hourly attention
    reminder. It closes by its class's rule (``incident_resolution``): for a wire row, the
    print RUNNING again and printed past the fault (R27), or the job's terminal — never
    by a timer.

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

    ``stop_ended`` — whether the restart stop ended the job, as :func:`_give_up` read it once for
    its own refill and hold decisions (so the page and the hold never disagree); ``None`` reads it
    here (:func:`_stop_ended`).
    """
    from backend.app.core.database import async_session
    from backend.app.models.printer import Printer
    from backend.app.services.notification_service import notification_service

    # Per-tray diagnostic snapshot on every escalation (the 18:45 forensics gap).
    await _log_tray_snapshot(incident)

    if stop_ended is None:
        stop_ended = await _stop_ended(incident, evidence)
    ended = _job_ended(reason, stop_ended=stop_ended)
    # The DRIVER's account — what it sent and what the wire answered — keyed on the kind
    # it ran as: a jam machine that ended in a physical hold still sent a jam machine's
    # steps, and the page must say so.
    detail = _compose_detail(
        incident, reason, refill=refill, evidence=evidence, printer_words=printer_words, stop_ended=stop_ended
    )
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
                    job_ended=ended,
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
            "the job has ended; the printer is held" if ended else "left PAUSED",
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
    """Hand one escalation to the quarantine decider (``farm_policy.record_recovery_escalation``, owner 6):
    it records the durable ``recovery_escalation`` row (once per incident) and decides the quarantine on
    events — :data:`_JAM_QUARANTINE_REASONS` escalations with no completed print between them. This module
    supplies only its own vocabulary: which reasons are evidence of AMS hardware.

    Called from :func:`_escalate` and the wedge-ended-print give-up only — an operator takeover
    (:func:`_abort`) deliberately records nothing. Best-effort: any failure here must NOT break the
    escalation that called it (the printer is already left PAUSED regardless).
    """
    from backend.app.services import farm_policy

    try:
        await farm_policy.record_recovery_escalation(
            db,
            printer_id=incident.printer_id,
            reason=reason,
            code=incident.code or None,
            opened_at=opened_at,
            counting_reasons=_JAM_QUARANTINE_REASONS,
        )
    except Exception:  # noqa: BLE001 — quarantine bookkeeping must never break the escalation
        logger.exception("spool_recovery: escalation bookkeeping failed for printer %s", incident.printer_id)


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

    ``token`` is :func:`command_outcome.takeover`'s verdict where the caller has it, so the closing
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
        await _leave_open(incident)
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


async def _leave_open(incident: RecoveryIncident) -> None:
    """A driver stands aside and leaves its row OPEN for others to answer:
    ``printer_incidents.mark_escalated`` — the row still open, no longer the ``recovering`` promise
    that only a live driver keeps (``incident_resolution.driver_owns``: the sweep skips such a row),
    ``escalated_at`` stamped once — and nothing else: no page (a stand-aside is never a give-up), no
    ledger row, no bar. Two callers:

    * :func:`_abort`, while the farm's command on the row still pends (K11) — a human's hold now;
    * the refill episode once its resume reads the FIRMWARE owning the toolhead (a change, the start
      block, the end retract) or a PAUSE over a standing fault (:func:`_refill_then_resume`) — the
      row's closers and that fault's own lane answer it.

    Best-effort: a failed write leaves the row ``recovering``, which the startup re-entry re-drives
    (its log holds any pending command, so it waits rather than sends)."""
    from backend.app.core.database import async_session

    try:
        async with async_session() as db:
            await printer_incidents.mark_escalated(db, incident.incident_id)
    except Exception:  # noqa: BLE001 — a stand-aside must never crash the driver
        logger.exception(
            "spool_recovery: printer %s incident %s could not be left open for its closers",
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
            driver_live=printer_incidents.driver_live(printer_id),
            feed=_feed_reading(printer_id),
            observed_load_at=printer_action.observed_load_at(printer_id),
            job_layer=_printed_layer(printer_id),
        )
        closed: list[tuple[int, str, str, bool]] = []
        async with async_session() as db:
            for incident in await printer_incidents.open_rows(db, printer_id):
                if incident_resolution.own_job_running(incident, ctx):
                    _job_ran_since_open.add(incident.id)
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
            driver_live=printer_incidents.driver_live(printer_id),
            terminal=terminal,
            feed=_feed_reading(printer_id),
            observed_load_at=printer_action.observed_load_at(printer_id),
            job_layer=_printed_layer(printer_id),
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


async def sweep_open_incidents() -> int:
    """Close ESCALATED incidents whose own evidence says they are over. Returns the count.

    The missing lifecycle path. An incident closes when the printer is seen RUNNING
    (:func:`on_observed_running` — for a wire row, once printed past the fault: R27, which
    this sweep also reads on every live report), when its job reaches a terminal
    (:func:`on_job_terminal`), or when a restart's rearm finds the printer positive. Nothing closed an incident because its FAULT
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
    2. WHAT the printer is saying — only a LIVE report (``live_reading.reads_live``: the
       current session's fresh report) is evidence; a reconnect re-broadcasts the previous
       session's cached state, and a disconnected printer says nothing — both read ``None``.
    3. WHEN — after the deciders the events belong to, never after a window (2026-10-11: the
       120 s dwell is gone). A printer whose session start has not re-evaluated its holds yet
       (:data:`_session_evaluated` — the downtime reconcile runs first there) is not swept, and
       while the printer's terminal is being handled the table's looking cells stand
       (``Context.terminal_in_flight``), because that terminal reads the holds to classify the
       stop and the plate before it closes them.

    And the RUNOUT level the per-push edges cannot see across a restart: an open runout row whose
    refill the wire now shows is resumed (:func:`_maybe_resume_runout`).

    The sweep also RESUMES ONCE (doctrine rule 1; Raymond 2026-10-11: "will i have to click resume
    on a filament swap after this is shipped? the answer should be no"): for every row it visits it
    asks the rule table whether a resume is owed (:func:`_maybe_self_heal_after_repair` over
    ``incident_resolution.resume_owed``) — the operator freed the path and reloaded a slot by hand,
    or an empty toolhead reads fed again, and the printer is still PAUSEd on the very job the hold
    is for. One resume per row, by the record (the once is a farm ``resume`` action on the row).

    Guarded end to end and per incident: this runs from the scheduler tick and must
    never kill it (invariant 10).
    """
    closed = 0
    try:
        from backend.app.core.database import async_session

        async with async_session() as db:
            rows = await printer_incidents.all_open(db)
            live_ids = {inc.id for inc in rows}
            for stale in [iid for iid in _resume_tasks if iid not in live_ids]:
                _resume_tasks.pop(stale, None)
            _job_ran_since_open.intersection_update(live_ids)

            for incident in rows:
                try:
                    pid = incident.printer_id
                    if driver_owns(incident, live=printer_incidents.driver_live(pid)):
                        continue
                    state = _live_report(pid)
                    if state is not None and _session_evaluated.get(pid) != getattr(state, "connection_epoch", None):
                        # The session's own re-evaluation has not run yet (the session start runs
                        # it, after the downtime reconcile): it decides this printer first.
                        continue
                    ctx = Context(
                        state=state,
                        driver_live=False,
                        command_pending=_pending_on(incident),
                        feed=_feed_reading(pid) if state is not None else None,
                        observed_load_at=printer_action.observed_load_at(pid),
                        job_layer=_printed_layer(pid) if state is not None else None,
                        terminal_in_flight=printer_manager.terminal_in_flight(pid),
                    )
                    # The resume-once spawner asks EVERY visited row, not only a row whose close
                    # verdict fired: a toolhead hold stands while its print is PAUSEd, fed or not.
                    await _maybe_self_heal_after_repair(incident, ctx)
                    await _maybe_resume_runout(incident, state)
                    verdict = incident_resolution.resolve(incident, "sweep_tick", ctx)
                    if not verdict.close:
                        continue
                    live = (getattr(state, "state", None) or "") if state is not None else ""
                    await printer_incidents.close(db, incident.id, status=STATUS_RESOLVED, source=verdict.source)
                    cleared = await printer_incidents.clear_hold_projection(db, incident.item_id)
                    _resume_tasks.pop(incident.id, None)
                    closed += 1
                    logger.info(
                        "spool_recovery: printer %s incident %s (%s) closed — %s (state=%s, %s)%s",
                        pid,
                        incident.id,
                        incident.kind,
                        verdict.source,
                        live,
                        verdict.evidence,
                        "; hold token cleared" if cleared else "",
                    )
                except Exception:  # noqa: BLE001 — one bad incident must not abort the sweep
                    logger.exception("spool_recovery: incident sweep failed for incident %s", incident.id)
    except Exception:  # noqa: BLE001 — a scheduler-tick watch must never kill the tick
        logger.exception("spool_recovery: wire-clear incident sweep failed")
    return closed


async def _maybe_self_heal_after_repair(incident: PrinterIncident, ctx: Context) -> None:
    """THE one spawner of the farm's resume of a print the PRINTER or a PERSON put the toolhead
    back for (doctrine rule 1; Raymond 2026-10-11: "will i have to click resume on a filament swap
    after this is shipped? the answer should be no"). Asked of every row by the sweep and by the
    printer's session start (:func:`reevaluate_session`); both guard it per row.

    Whether a resume is owed is the rule table's ONE answer (``incident_resolution.resume_owed``): a
    ``physical`` hold whose path somebody freed and loaded by hand, or a ``toolhead`` hold whose
    toolhead reads fed again — the row's own job PAUSEd either way — never in maintenance mode, at a
    plate-check pause or beside a runout row, and ONCE: a farm ``resume`` action on the row already
    spends it (the record — the process set that was the once before 2026-10-11 died with every
    restart). A resume the printer never received (``void``, or a frame that never left) is owed
    again, and the next sweep or session start sends it.

    The row still closes the ordinary way, through the sweep's own cells. A task still on its way to the send is not spawned twice
    (:data:`_resume_tasks`, liveness only). The body is the resume's one (:func:`_resume_after_repair`
    → :func:`resume_paused_print`, K9), which asks K7 again at publish time; the printer's answer to
    it is an event (:func:`on_action_answered`).
    """
    running = _resume_tasks.get(incident.id)
    if running is not None and not running.done():
        return
    owed = incident_resolution.resume_owed(incident, ctx)
    if not owed.owed:
        return
    from backend.app.core.tasks import spawn_background_task

    logger.info(
        "spool_recovery: printer %s %s incident %s — %s; resuming it",
        incident.printer_id,
        incident.kind,
        incident.id,
        owed.evidence,
    )
    _resume_tasks[incident.id] = spawn_background_task(
        _resume_after_repair(incident.printer_id, incident.id),
        name=f"repair-resume-p{incident.printer_id}",
    )


async def _maybe_resume_runout(incident: PrinterIncident, state: PrinterState | None) -> None:
    """The open RUNOUT row's refill, read as a LEVEL on the printer's live report — asked by the sweep
    and by the session start, beside the per-push edges (:func:`note_demand_watch`), which see only what
    a session WITNESSED. A roll refilled while the farm was down left no edge for anybody to see (the
    first sample after a restart only seeds), so the print sat PAUSEd for a button nobody pressed.

    Owed when, all on the live report: the row's own job is PAUSEd; the wire says the filament is back
    (:func:`_refill_ready` — the demand family clear, or the demanded slot reading loaded; never an
    absence while the reboot prompt stands, which proves nothing); no maintenance mode and no plate
    check holds the job; the operator's ``runout_auto_resume_enabled``; and the row's resume budget,
    off the record (``incident_resolution.resume_spent``: a farm resume out and awaiting the printer,
    one reached, or one a person took over spends it; a REFUSED one, or one the record passed its
    self-heal bound with no answer, is owed again until the cap — 3 resumes — then the give-up page; a
    void one is owed again). The resume is the refill lane's own (:func:`_resume_after_refill`, which
    re-asks all of it before it publishes, after its settle — the 15 s cadence of a re-owed resume); a
    task still on its way to the send is not spawned twice (:data:`_resume_tasks`, liveness only).
    Also the re-own of a refused or unanswered resume (:func:`_reown_runout_resume`). Never raises."""
    try:
        if incident.kind != KIND_RUNOUT or state is None or _live_state(state) != "PAUSE":
            return
        if same_job(getattr(state, "subtask_id", None), incident.job_id) != "same":
            return
        running = _resume_tasks.get(incident.id)
        if running is not None and not running.done():
            return
        if not _refill_ready(state, None):
            return
        if printer_incidents.automation_held(incident.printer_id) or printer_incidents.job_pause_held(
            incident.printer_id
        ):
            return
        if (
            incident_resolution.resume_spent(
                incident.printer_id, incident.id, unanswered=_unanswered_resumes(incident.printer_id, incident.id)
            )
            is not None
        ):
            return
        from backend.app.core.database import async_session

        async with async_session() as db:
            if not await _read_bool(db, "runout_auto_resume_enabled", _DEFAULT_RUNOUT_AUTO_RESUME):
                return
        slot = decode_global_tray(incident.slot_global_tray) if incident.slot_global_tray is not None else None
        demanded = slot if slot is not None and slot[0] is not None and slot[1] is not None else None
        from backend.app.core.tasks import spawn_background_task

        logger.info(
            "spool_recovery: printer %s runout incident %s — the wire reads the filament back on the live "
            "report; resuming it",
            incident.printer_id,
            incident.id,
        )
        _resume_tasks[incident.id] = spawn_background_task(
            _resume_after_refill(incident.printer_id, demanded), name=f"runout-level-resume-p{incident.printer_id}"
        )
    except Exception:  # noqa: BLE001 — one row's level read must not cost the sweep its others
        logger.exception("spool_recovery: runout level read failed for incident %s", incident.id)


async def reevaluate_session(printer_id: int) -> int:
    """SESSION START: re-evaluate every hold open on ``printer_id`` against the printer's first
    FRESH report of a new MQTT session — a restart's, a reconnect's. Returns how many it closed.

    Called once per session by ``session_start`` (``printer_events.FirstFreshReport``), never from
    the lifespan: a restart proves nothing about a hold on a PHYSICAL printer — it may have been
    resumed, finished, or still sit PAUSEd exactly as we left it — and the only witness is the
    printer's own report, which a startup sweep run before ``init_printer_connections`` never had.
    That ordering stranded 001-H2S incident 574 for a night (2026-10-11): every printer read
    "unreported", nothing closed, and nothing re-evaluated the hold once its printer reported.

    Per row, the rule table's ``startup`` occasion — the sweep's own ladder with NO DWELL, because
    a first fresh report re-derives every wire fact from scratch, so a printer already running has
    answered the question:

    * a row a LIVE driver owns is left alone (the sweep reads the same ``driver_owns``): a session
      boundary mid-recovery is the driver's to wait out, never a second decider's;
    * any other row is first asked whether a RESUME is owed (:func:`_maybe_self_heal_after_repair`,
      the one spawner — the sweep asks the same, once per row), then whether it closes;
    * a row still reading ``recovering`` with no live task is a broken PROMISE (a restart killed
      its task, or the task died): it is re-entered (:func:`_reenter_recovering_incident`, or the
      refill driver's :func:`_reenter_refill_incident`) — left alone it would sit open forever, and
      because the AMS kinds are mutually exclusive it would block every future AMS incident there.

    The row's step ledger answers what the wire cannot: whether the driver STOPPED this job to
    restart it (a deploy mid-restart finds the printer IDLE — "not PAUSE" to the wire cell — and
    must re-enter the continuation, never close the row), and whether a motion command the farm
    sent still pends (K11 — held, or sent and never read). Asked of THIS driver's rows only:
    another kind's ledger is another driver's vocabulary (a plate-check episode's ``dialog`` /
    ``stop`` steps, ``pause_recovery``), which this driver's log cannot read.

    The projection needs no rebuild here: ``printer_incidents.close`` drops the row it closes, and
    the lifespan's ``printer_incidents.rehydrate`` was the one rebuild. Never raises — it rides
    the session start, which must re-evaluate the printer's other lanes too.
    """
    closed = 0
    session = getattr(_get_state(printer_id), "connection_epoch", None)
    try:
        from backend.app.core.database import async_session

        if printer_incidents.driver_live(printer_id):
            # A live driver owns the outcome of every row on this printer (``driver_owns``, read
            # the same way by the sweep): a session boundary mid-recovery is the driver's to wait
            # out (``_live_reading``), never a second decider's.
            return 0
        driverless: list[tuple[int, str]] = []
        async with async_session() as db:
            for incident in await printer_incidents.open_rows(db, printer_id):
                try:
                    if await _reevaluate_row(db, incident):
                        closed += 1
                    elif driver_owns(incident, live=False):
                        driverless.append((incident.id, incident.kind))
                except Exception:  # noqa: BLE001 — one bad row must not cost the others their verdict
                    logger.exception("spool_recovery: session start — incident %s re-evaluation failed", incident.id)
        # After the closes, so a re-entry's own escalate/close writes are the last word. A toolhead
        # row is the refill driver's (K10): its own re-entry, never the AMS machine's.
        for incident_id, kind in driverless:
            if kind in REFILL_KINDS:
                _reenter_refill_incident(incident_id, printer_id)
            else:
                await _reenter_recovering_incident(incident_id, printer_id)
        if closed or driverless:
            logger.info(
                "spool_recovery: session start — printer %s: %d hold(s) closed, %d re-entered",
                printer_id,
                closed,
                len(driverless),
            )
    except Exception:  # noqa: BLE001 — the session start must re-evaluate the printer's other lanes
        logger.exception("spool_recovery: session-start re-evaluation failed for printer %s", printer_id)
    finally:
        # From here the sweep may act on this printer for this session (:data:`_session_evaluated`).
        if session is not None:
            _session_evaluated[printer_id] = session
    return closed


async def _reevaluate_row(db, incident: PrinterIncident) -> bool:
    """One open row at its printer's session start (:func:`reevaluate_session`, no driver live):
    True when it closed. A row no driver promised is first asked whether a resume is owed (the one
    spawner, :func:`_maybe_self_heal_after_repair`), then every row whether it closes."""
    printer_id = incident.printer_id
    state = _get_state(printer_id)
    restart_owed = command_pending = False
    if incident.kind in AMS_FAULT_KINDS | REFILL_KINDS:
        try:
            log = await _RecoveryEvidence.from_row(db, incident.id)
            # Owed only once the stop ENDED the job: a stop the printer never took leaves a live
            # job, which no continuation owes anything.
            restart_owed = await log.restart_ended(state)
            command_pending = log.pending_command is not None
        except LookupError:
            # A ledger the driver cannot read is drift (``_step_of``); it must not cost the row
            # its session-start verdict.
            logger.exception("spool_recovery: session start — incident %s has an unreadable step ledger", incident.id)
    ctx = Context(
        state=state,
        driver_live=False,
        restart_owed=restart_owed,
        command_pending=command_pending,
        feed=_feed_reading(printer_id) if state is not None else None,
        observed_load_at=printer_action.observed_load_at(printer_id),
        job_layer=_printed_layer(printer_id) if state is not None else None,
        terminal_in_flight=printer_manager.terminal_in_flight(printer_id),
    )
    if not driver_owns(incident, live=False):
        await _maybe_self_heal_after_repair(incident, ctx)
        await _maybe_resume_runout(incident, state)
    verdict = incident_resolution.resolve(incident, "startup", ctx)
    if not verdict.close:
        return False
    await printer_incidents.close(db, incident.id, status=STATUS_RESOLVED, source=verdict.source)
    await printer_incidents.clear_hold_projection(db, incident.item_id)
    _resume_tasks.pop(incident.id, None)
    logger.info(
        "spool_recovery: session start — printer %s incident %s (%s) closed — %s",
        printer_id,
        incident.id,
        incident.kind,
        verdict.evidence,
    )
    return True


def _reenter_refill_incident(incident_id: int, printer_id: int) -> None:
    """Give a driver back to a ``toolhead_refill`` row a restart left ``recovering`` (K10). The row and
    its step ledger are the durable state (``bambu-ams-behavior`` restart-durability): the episode is
    re-spawned on the row and registered in the same synchronous stretch, and it decides from the
    first LIVE reading (:func:`_refill_episode` with no trigger) — a paused print is refilled and
    resumed (T3), a print running on air is stopped (T4) — waiting, never resending, on a command its
    log holds pending. Never raises."""
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

    One incident is not the wire's to decide: one whose restart stop ENDED its job
    (:meth:`_RecoveryEvidence.restart_ended`). Its job was ended by the driver itself, so
    it re-enters on the row's own facts and the driver resumes the restart continuation
    (a deploy mid-restart must not leave the stalled filament in the tube). A stop the printer
    never took ended nothing: that job is live and PAUSEd, and the wire decides it like any other.
    """
    try:
        state = _get_state(printer_id)
        if state is None or not live_reading.reads_live(state):
            # The re-entry is decided by the printer's report, never by its absence: the session
            # start that calls this runs on the first FRESH report, and a printer that is not
            # reporting right now is re-entered at its next one (owner 7 of the 2026-10-11 plan —
            # no row is escalated off a missing reading).
            logger.info(
                "spool_recovery: printer %s incident %s left mid-swap — not reporting live; re-entered at its next "
                "session start",
                printer_id,
                incident_id,
            )
            return None
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
            if await evidence.restart_ended(state):
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
                        fault_layer=_printed_layer(printer_id),
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
    # RUNNING with nothing fed and no firmware sequence YET (the first frames of a runout
    # auto-switch read exactly so). RUNNING-only, so the verb answers ``not_paused`` before K7 and
    # never reaches this row; a refill episode that does (the print resumed under it) refuses —
    # the one row that can never send a motion INTO the firmware's own sequence, where a refill's
    # unload or load is held behind the change and drains the extruder at its release (012-H2S
    # 2026-09-23). ``unknown``: the farm has no reading to act on yet; the per-push detector D2
    # acts the moment the feed state reads AIR.
    "unconfirmed": "unknown",
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
    button the caller pressed — which the caller reads the firmware's ACK by. What the printer
    answered is the RECORD's, on an event, however late (``printer_action``: RUNNING reaches it, a
    fail ACK refuses it): an evidence lane's resume is answered by :func:`on_action_answered`, never
    by a window this verb waits out."""

    sent: printer_action.Sent


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
    press: printer_action.Command | None = None,
    incident_id: int | None = None,
) -> ResumeVerdict:
    """Resume this printer's PAUSED print — refilling an EMPTY toolhead first. THE one body (K9).
    Never raises (an assist lane must never crash its caller; a failure is logged and answers
    ``not_sent``).

    At publish time it asks K7 (:func:`refill_verdict.refill_owed`, trigger ``T3``) and acts on the one decision
    table (:data:`_RESUME_DECISION`):

    * ``resume`` — a fed toolhead, or the firmware feeding it itself: ONE resume, the dialog button
      ``press`` names (the HMS route's resume buttons) or the plain resume
      (``printer_action.resume``), sent through the record as the ``actor``'s action (a person's
      Resume is the operator's; the evidence lanes' are the farm's, on ``incident_id``). Its answer
      is the record's, on an event (:func:`on_action_answered`): nothing here waits for RUNNING.
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
        verdict = refill_verdict.refill_owed(printer_id, st, trigger="T3", reading=_feed_reading(printer_id))
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
        # The farm's resume budget on a hold is decided AT the publish: the record's farm resumes on the
        # row (``incident_resolution.resume_spent`` — once per row, three for a runout row's refill) are
        # read under the printer's resume lock, which spans that read and the action's insert — so of two
        # lanes acting on one hold (the demand edge, the presence gain, the sweep's level, a re-own), the
        # second finds the first's action out and stands down. A person's Resume is theirs: never gated.
        once = incident_id is not None and actor != "operator"
        async with _resume_locks.setdefault(printer_id, asyncio.Lock()) if once else contextlib.nullcontext():
            if once and incident_id is not None:
                spent = incident_resolution.resume_spent(
                    printer_id, incident_id, unanswered=_unanswered_resumes(printer_id, incident_id)
                )
                if spent is not None:
                    logger.info(
                        "spool_recovery: printer %s resume (%s) on incident %s stands down — %s",
                        printer_id,
                        actor,
                        incident_id,
                        spent.why,
                    )
                    return ResumeStoodDown(spent.why)
            sent = await printer_action.send(
                printer_id,
                press if press is not None else printer_action.resume(),
                actor=ACTOR_OPERATOR if actor == "operator" else ACTOR_FARM,
                incident_id=incident_id,
                client=client,
                # The refill lane's resume carries its self-heal bound: unanswered that long online, the
                # runout row is owed it again (``resume_spent``) and the lane resends it.
                bound=_RUNOUT_RESUME_SELF_HEAL if actor == "refill_lane" else None,
            )
        if not sent.published:
            logger.info(
                "spool_recovery: printer %s resume (%s) send did not go out (offline?) — nothing sent",
                printer_id,
                actor,
            )
            return ResumeNotSent("not_sent")
        logger.info(
            "spool_recovery: printer %s resume (%s) published as action %s — the toolhead reads %s",
            printer_id,
            actor,
            sent.action_id,
            verdict.reason,
        )
        return ResumeSent(sent)
    except Exception:  # noqa: BLE001 — a resume lane must never crash its caller
        logger.exception("spool_recovery: %s resume failed for printer %s", name, printer_id)
        return ResumeNotSent("not_sent")


def _feed_reading(printer_id: int) -> FeedReading | None:
    """THE recovery driver's read of the toolhead's FEED STATE (``BambuMQTTClient.feed()``,
    invariant 16) — the one reader every consumer here asks: the refill verdict's ``reading``, the
    motion ledger's push, the rule table's ``Context.feed``, the jam's last-feeder tier and the
    refill episode's wait. ``None`` with no client registered: nothing is then read as fed, and
    the verdict decides nothing."""
    client = printer_manager.get_client(printer_id)
    return client.feed() if client is not None else None


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


async def _send_until_published(
    incident: RecoveryIncident, command: printer_action.Command, *, bound: printer_action.SelfHealBound | None = None
) -> printer_action.Sent:
    """Send ``command`` as the farm's action on the row until its frame goes out: a send that did not
    leave the farm (no client, a publish that failed — the record answers it ``refused`` with its
    ``refusal``) waits for the printer's next session (``printer_action.await_next_session``) and is sent
    again.
    The printer never saw it, so the episode still owes it — one recorded send per session event.
    ``bound`` — the action's self-heal bound, armed by the record at the send that goes out."""
    pid = incident.printer_id
    while True:
        sent = await printer_action.send(pid, command, actor=ACTOR_FARM, incident_id=incident.incident_id, bound=bound)
        if sent.published:
            return sent
        # The session the send was attempted on (its entry snapshot), never one read after it.
        epoch = sent.entry.connection_epoch
        logger.info(
            "spool_recovery: printer %s incident %s — the farm's %s did not go out (%s); sent again on the next "
            "session",
            pid,
            incident.incident_id,
            command.name,
            sent.refusal,
        )
        await printer_action.await_next_session(pid, epoch, recheck_s=_POLL_INTERVAL_S)


async def _await_action(incident: RecoveryIncident, action_id: int) -> command_outcome.Answer:
    """The record's final answer to the farm's action ``action_id`` (``printer_action.settled``): an
    EVENT decides it — the ACK, the state the printer reports, the job's terminal, a reboot, a person's
    act — however late, with no fallback of its own: its caller is a wait production never bounded with
    the step bound (the refill episode's resume). The driver reads the printer at
    its push cadence through the session gate (:func:`_live_reading`) while it waits, as its other waits
    do: a gap is a wait."""
    while True:
        answer = await printer_action.settled(action_id)
        if answer is not None:
            return answer
        await _live_reading(incident)
        await asyncio.sleep(_POLL_INTERVAL_S)


async def _await_bounded(
    incident: RecoveryIncident, sent: printer_action.Sent, bound: printer_action.SelfHealBound
) -> command_outcome.Answer | printer_action.Unanswered:
    """The record's final answer to the farm's action ``sent`` — or, once the record passed the action's
    ``bound`` with no event answering it, its :class:`printer_action.Unanswered` reading
    (``printer_action.await_outcome(sent, bound=...)``, which then answers at once). Events first: the
    answer is asked before the bound on every poll, and a late answer still lands on the record. Read at
    the driver's push cadence through the session gate (:func:`_live_reading`)."""
    while True:
        answer = await printer_action.settled(sent.action_id)
        if answer is not None:
            return answer
        if printer_action.bound_passed(sent.action_id):
            return await printer_action.await_outcome(sent, bound=bound)
        await _live_reading(incident)
        await asyncio.sleep(_POLL_INTERVAL_S)


async def _farm_pause(
    incident: RecoveryIncident, *, bound: printer_action.SelfHealBound
) -> tuple[command_outcome.Answer | printer_action.Unanswered, str | None]:
    """THE farm pause: ``print.pause`` sent as the farm's action on the row (until its frame goes out,
    :func:`_send_until_published`) and the printer's answer read off the record; returns that answer and
    the record's reason. The one body behind every pause this module publishes: the release reader's
    arms (:func:`_read_after` — the empty path, the 009 hang, the swap's load that did not hold). A print
    found on AIR is never paused: it is stopped (:func:`_stop_printing_on_air`, operator ruling
    2026-10-12).

    ``bound`` — the step bound (production's landing, ``337f4954``): a pause no event answers by then
    returns :class:`printer_action.Unanswered` (:func:`_await_bounded`), which the reader reads as not
    landed; its late answer is still the record's."""
    sent = await _send_until_published(incident, printer_action.pause(), bound=bound)
    answer = await _await_bounded(incident, sent, bound)
    reason = await printer_action.reason_of(sent.action_id)
    logger.info(
        "spool_recovery: printer %s — the farm's pause (action %s) answered %s%s",
        incident.printer_id,
        sent.action_id,
        answer.value if isinstance(answer, command_outcome.Answer) else f"nothing by {answer.bound_name}",
        f" ({reason})" if reason else "",
    )
    return answer, reason


# --- AIR → stop and retry (operator ruling 2026-10-12) ----------------------------------
#
# "printing on air = ruined print already … pause after 6 seconds is what got 001's issue in the first
# place, the printer was still doing something and the farm moved onto the next step after 6 seconds."
# Settled the same day: on PROOF of air — a layer printed with nothing fed, the feed state's AIR
# (invariant 16; a firmware change, runout switch or runout hold never advances a layer, so CHANGING /
# RUNOUT_HELD / TAIL / START_BLOCK are never air) — the farm STOPS the ruined print and the unit is
# RETRIED: the ordinary failed disposition (the unit's one retry, the consecutive-failure count, the plate
# gate of a deposit). The terminal reads the farm's stop off the row's ledger (``printer_incidents
# .PAYLOAD_AIR_STOP`` → ``farm_correlation``'s ``air_stop`` verdict → ``terminal_outcome``: ``failed``,
# ``printed_without_filament``), never off the H2S cancel echo the stop produces.

# What the farm's stop of a print running on AIR came to (:func:`_stop_printing_on_air`). Closed:
# ``stopped`` — the printer took it: the job ended, its terminal was handled, and the row is closed by the
# driver (``printer_incidents.RESOLVE_DRIVER_ENDED``); ``escalated`` — the printer did not take it (refused,
# or unanswered by ``printer_action.STOP_SELF_HEAL``) after ONE resend, and the row is a person's
# (``air_stop_not_taken``, one page); ``taken_over`` — a person's command took the printer: the caller's
# stand-aside; ``not_air`` — at the send the print no longer reads as running on air (the phase moved, a
# guard holds, the job ended): nothing was stopped by this call.
AirStop = Literal["stopped", "escalated", "taken_over", "not_air"]

# How many times a stop the printer did NOT take is sent again before the farm hands the print to a person:
# once (the plan's "resend once, then escalate once" — no page loop: the per-push detector never stops a
# row whose farm stop was not taken, ``printer_incidents.air_stop_sent``).
_AIR_STOP_RESENDS = 1


def _prints_on_air(printer_id: int, st: PrinterState) -> bool:
    """Is this print RUNNING on air the farm acts on — the feed state's AIR with no policy guard (K7 asked
    as T4: no eject, no queued farm command, no reboot prompt, no open RUNOUT row — invariant 9, a runout
    is never an air stop — no maintenance mode)? The verdict's, never a frame read here."""
    return (
        _live_state(st) == "RUNNING"
        and refill_verdict.refill_owed(printer_id, st, trigger="T4", reading=_feed_reading(printer_id)).owed
    )


async def _stop_printing_on_air(incident: RecoveryIncident, evidence: _RecoveryEvidence) -> AirStop:
    """STOP a print that printed a layer with nothing fed — THE one exit for every print the farm finds on
    AIR: the refill episode's T4 (the per-push detector, a resume onto an empty toolhead, a queued pull-back
    drained under a running print), its own resume read back as AIR, the AMS jam driver's step 1 (a standing
    fault the firmware did not pause for) and the release reader's air arm (:func:`_read_after`).

    Per send, in order: the print is re-read (:func:`_prints_on_air` — the firmware's own change or runout
    is never stopped: a runout row, a change, a tail are not air); the action is RECORDED
    (``printer_action.prepare``, its row id the frame's sequence id) and the step :class:`AirStopStep` is
    noted on the open row's ledger BEFORE ``print.stop`` goes out, so the terminal it produces always
    meets a projection that already says the farm sent it (``PAYLOAD_AIR_STOP``); the printer's answer is
    the record's (``await_outcome(sent, bound=STOP_SELF_HEAL)``, read at the driver's push cadence through
    the session gate, :func:`_await_bounded`):

    * ``reached`` — the job ended: the terminal is waited out (:func:`_close_after_air_stop`) and the row
      closed ``driver_ended`` → ``stopped``;
    * ``taken_over`` — a person's command → ``taken_over``;
    * ``void`` (never received, a reboot) or a frame that never left the farm — the printer never saw it:
      sent again, once per session event, while the print still reads on air;
    * ``refused``, or no event by ``printer_action.STOP_SELF_HEAL`` (the action stays outstanding — a late
      terminal still ends the job by the farm's hand and classifies ``air_stop``) — not taken: sent again
      ONCE (:data:`_AIR_STOP_RESENDS`), then escalated ONCE (``air_stop_not_taken``, the printer's words) →
      ``escalated``. No loop: the detector never stops a row whose farm stop was not taken.

    A job that is OVER at a re-read after one of this call's stops went out is that stop landing late
    (the row carries it, so the terminal classifies ``air_stop`` whatever the echo): ``stopped``, the row
    closed once the terminal is handled — never read as a print that is simply "no longer on air"."""
    pid = incident.printer_id
    not_taken = 0
    sent_one = False
    while True:
        st, _waited = await _live_reading(incident)
        if sent_one and _live_state(st) in _JOB_OVER_STATES:
            logger.info(
                "spool_recovery: printer %s incident %s — the job is over (%s) after the farm's air stop; it landed",
                pid,
                incident.incident_id,
                _live_state(st),
            )
            await _close_after_air_stop(incident)
            return "stopped"
        if not _prints_on_air(pid, st):
            logger.info(
                "spool_recovery: printer %s incident %s — the print no longer reads as running on air (state=%s) — "
                "no stop sent",
                pid,
                incident.incident_id,
                _live_state(st),
            )
            return "not_air"
        prepared = await printer_action.prepare(
            pid, printer_action.stop(), actor=ACTOR_FARM, incident_id=incident.incident_id, bound=STOP_SELF_HEAL
        )
        seq = await evidence.note(AirStopStep.draft(action_id=prepared.action_id))
        logger.warning(
            "spool_recovery: printer %s printed a layer with nothing fed (incident %s, layer %s) — the farm stops "
            "the print (action %s); the unit is retried",
            pid,
            incident.incident_id,
            _printed_layer(pid),
            prepared.action_id,
        )
        sent = await printer_action.publish(prepared)
        sent_one = sent_one or sent.published
        if not sent.published:
            await evidence.answer(seq, "not_sent")
            logger.info(
                "spool_recovery: printer %s — the farm's air stop (action %s) did not go out (%s); sent again on the "
                "next session",
                pid,
                sent.action_id,
                sent.refusal,
            )
            await printer_action.await_next_session(pid, sent.entry.connection_epoch, recheck_s=_POLL_INTERVAL_S)
            continue
        answer = await _await_bounded(incident, sent, STOP_SELF_HEAL)
        reason = await printer_action.reason_of(sent.action_id)
        if isinstance(answer, printer_action.Unanswered):
            await evidence.answer(seq, "unconfirmed")
        else:
            match answer:
                case command_outcome.Answer.REACHED:
                    await evidence.answer(seq, "taken")
                    logger.info(
                        "spool_recovery: printer %s — the farm's air stop (action %s) was taken; the terminal "
                        "classifies the job air_stop",
                        pid,
                        sent.action_id,
                    )
                    await _close_after_air_stop(incident)
                    return "stopped"
                case command_outcome.Answer.TAKEN_OVER:
                    await evidence.answer(seq, "not_taken")
                    logger.info(
                        "spool_recovery: printer %s — a person's command took the farm's air stop (action %s) over",
                        pid,
                        sent.action_id,
                    )
                    return "taken_over"
                case command_outcome.Answer.VOID:
                    await evidence.answer(seq, "not_sent")
                    logger.info(
                        "spool_recovery: printer %s — the farm's air stop (action %s) was void (%s); the printer "
                        "never received it",
                        pid,
                        sent.action_id,
                        reason or "no reason",
                    )
                    continue
                case command_outcome.Answer.REFUSED:
                    await evidence.answer(seq, "not_taken")
                case _:
                    assert_never(answer)
        not_taken += 1
        how = (
            f"no answer by {answer.bound_name}"
            if isinstance(answer, printer_action.Unanswered)
            else f"refused{f': {reason}' if reason else ''}"
        )
        if not_taken > _AIR_STOP_RESENDS:
            logger.warning(
                "spool_recovery: printer %s — the farm's air stop was not taken after %d send(s) (%s); handed to a "
                "person",
                pid,
                not_taken,
                how,
            )
            await _escalate(incident, "air_stop_not_taken", evidence=evidence, printer_words=reason)
            return "escalated"
        logger.warning(
            "spool_recovery: printer %s — the farm's air stop (action %s) was not taken (%s); sent once more",
            pid,
            sent.action_id,
            how,
        )


async def _close_after_air_stop(incident: RecoveryIncident) -> None:
    """The farm's air stop was taken: wait while the job's terminal is being handled
    (``printer_manager.terminal_in_flight`` — a liveness read, never a window: it is up from the push that
    showed the job over until ``main.on_print_complete`` returns), so the terminal classifies the stop off
    this row's projection (``PAYLOAD_AIR_STOP`` → ``air_stop``) before the row leaves it, then close the row
    as the driver's own (``RESOLVE_DRIVER_ENDED``: a print the farm's own verb ended is not a recovery) and
    drop its hold token. The terminal's closer stood aside while this driver was live, so the row is the
    driver's to close. No page: the unit's failed disposition is the terminal's."""
    pid = incident.printer_id
    while printer_manager.terminal_in_flight(pid):
        await asyncio.sleep(_POLL_INTERVAL_S)
    if await _close_incident(incident, status=STATUS_RESOLVED, source=printer_incidents.RESOLVE_DRIVER_ENDED):
        await _clear_hold_projection_of(incident)
        logger.info(
            "spool_recovery: printer %s incident %s closed by the driver (%s) — the farm's air stop ended the print",
            pid,
            incident.incident_id,
            printer_incidents.RESOLVE_DRIVER_ENDED,
        )


async def _settle_pending(printer_id: int, incident_id: int, action_id: int) -> None:
    """Answer the step of the farm's command the RECORD answered ``reached`` after the hand-over
    (``printer_action``: the AMS ran it) — through the log's one writer (``EvidenceLog.answer``), so the
    driver's own log reads it run. Re-reads the ledger (``from_row``) and answers the step only while
    it still pends."""
    from backend.app.core.database import async_session

    async with async_session() as db:
        evidence = await _RecoveryEvidence.from_row(db, incident_id)
    step = next((s for s in reversed(evidence.command_steps) if s.action_id == action_id), None)
    if step is None or not printer_incidents.command_pends(step.answer):
        return
    await evidence.answer(step.seq, "complete")
    logger.info(
        "[spool_recovery] printer %s: the AMS ran the farm's queued %s (step %s, action %s) after the hand-over — "
        "answered complete",
        printer_id,
        step.command,
        step.seq,
        action_id,
    )


async def _refill_episode(
    printer_id: int,
    *,
    trigger: RefillTrigger | None,
    incident_id: int | None = None,
    settle: int | None = None,
) -> None:
    """THE refill driver (K10): one episode on one open row, spawned already REGISTERED as the
    printer's driver (:func:`_start_refill`, :func:`_sample_toolhead`, the startup re-entry). Never
    raises; releases its liveness slot on exit and hands a RUNNING level to the closers (the
    ``_run_recovery`` handover: the running edge stood aside while this driver lived).

    * ``settle`` — the action of the farm's pending command the record answered ``reached`` (D1, the
      record's answer event): its step is answered first.
    * the row — ``incident_id`` (the verb opened or re-entered it, or the detector's pending command
      names it), else found or opened now (:func:`_refill_row`): the durable promise, open before
      anything is sent.
    * ``T4`` — the print printed a layer on AIR: it is ruined, so the farm STOPS it and the unit is
      retried (:func:`_stop_printing_on_air`, operator ruling 2026-10-12) — no refill, no resume. The
      stop writes the row's outcome (closed after the terminal, or escalated when the printer did not
      take it); a person's command, or a print no longer on air, leaves the row to its closers.
    * ``None`` — the startup re-entry (:func:`_reenter_refill_incident`): the first LIVE reading whose
      feed state has left UNKNOWN decides (:func:`_reentered_trigger`) — RUNNING with K7(T4) owed is
      T4 (stopped), a PAUSE is T3; anything else is the row's closers' (a print running fed, a job
      over).
    * then, for T2 / T3, :func:`_refill_then_resume`.
    """
    pid = printer_id
    try:
        from backend.app.core.database import async_session

        if settle is not None and incident_id is not None:
            await _settle_pending(pid, incident_id, settle)
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
        if opened:
            logger.info("spool_recovery: printer %s opened toolhead refill %s (%s)", pid, row.id, trigger)
        if trigger is None:
            trigger = await _reentered_trigger(incident)
            if trigger is None:
                await _close_answered_reentry(incident)
                return
        if trigger == "T4":
            match await _stop_printing_on_air(incident, evidence):
                case "stopped" | "escalated":
                    pass  # the stop wrote the row's outcome
                case "taken_over" | "not_air":
                    await _leave_open(incident)
                case _ as never:
                    assert_never(never)
            return
        await _refill_then_resume(incident, evidence, trigger=trigger)
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
                driver_live=False,
                command_pending=_pending_on(row),
                feed=_feed_reading(incident.printer_id),
                observed_load_at=printer_action.observed_load_at(incident.printer_id),
                job_layer=_printed_layer(incident.printer_id),
                terminal_in_flight=printer_manager.terminal_in_flight(incident.printer_id),
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
    """The trigger a re-entered refill episode runs under, from its first LIVE reading whose feed state
    has LEFT UNKNOWN — the feed state's reading is an event of the push, so the episode waits for the
    report that gives one rather than deciding on none: PAUSE → T3 (every toolhead row ends in a
    resume); RUNNING with K7(T4) owed — the feed state reads AIR — → T4 (the episode stops the print);
    anything else (a print running fed, a job over, an empty reading K7 does not owe —
    ``unconfirmed`` included: the firmware's own sequence, or not air YET, which the per-push detector
    D2 acts on once it is) → ``None``: the row's own closers answer it."""
    pid = incident.printer_id
    while True:
        st, _waited = await _live_reading(incident)
        reading = _feed_reading(pid)
        if reading is not None and reading.phase is not FeedPhase.UNKNOWN:
            break
        await asyncio.sleep(_POLL_INTERVAL_S)
    match _live_state(st):
        case "PAUSE":
            return "T3"
        case "RUNNING" if refill_verdict.refill_owed(pid, st, trigger="T4", reading=reading).owed:
            return "T4"
        case _:
            return None


async def _refill_then_resume(
    incident: RecoveryIncident, evidence: _RecoveryEvidence, *, trigger: Literal["T2", "T3"]
) -> None:
    """The episode's contract on its row: wait for a command the AMS still holds on it (a restarted
    episode's own queued load — never send behind it), ask K7 at publish time, refill when owed
    (:func:`_refill_toolhead`, K8), and for T3 publish ONE resume and read it. (T4 — a print on air —
    never reaches here: it is stopped, :func:`_refill_episode`.)

    * a failed refill (no spool, or none reached the toolhead) escalates the row — a page naming the
      slot and what the AMS answered — and nothing is resumed;
    * a refusal at publish (the world moved: maintenance mode, a queued command, no live reading):
      T2 leaves the print as it is; T3 hands it to a person (``toolhead_refill_withheld``);
    * T2 (D1) never resumes: the print stays PAUSED, now with filament — the human's hold stands;
    * the resume is the farm's ACTION on the row (:func:`_resume_row` — one already outstanding on the
      row is waited for, never sent behind), answered by an EVENT however late (2026-10-11: the old
      30 s window escalated a resume the printer carried out at 45 s): ``reached`` → the feed state
      reads it (below); ``refused`` by the printer → escalated with its words (``resume_refused``, a
      page: "Resume refused"); ``taken_over`` by a person's command → the row is left open for its
      closers; ``void`` (never received, a reboot, the job ended) → a print still PAUSEd on the row's
      job is owed the resume again and the episode goes round once more, anything else is the
      closers'. An answer nothing gives simply waits (the attention owner pages it);
    * once reached, the toolhead's FEED STATE answers it (:func:`_await_resumed_feed`, invariant 16 —
      never one raw frame: the first frames after a resume into the firmware's own change read
      exactly like air, and reading one of them stranded 015-H2S and 001-H2S PAUSED on 2026-10-10).
      Its exits: fed → a ``toolhead_refill`` row closes ``refill_resumed`` (the farm's own recovery);
      the firmware owning the toolhead (a change, the start block, the end retract, a job closed) or
      a PAUSE over a standing fault → the row is left OPEN for its closers and that fault's own lane
      (:func:`_leave_open`); AIR — the resumed print laid down a layer with nothing fed — → the print
      is STOPPED and the unit retried (:func:`_stop_printing_on_air`, operator ruling 2026-10-12: the
      ordinary failed disposition, no page of its own); a takeover → :func:`_abort` /
      :func:`_hand_over`, as at every other wait."""
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
    result: RefillLoaded | None = None
    while True:
        st, _waited = await _live_reading(incident)
        owed = refill_verdict.refill_owed(pid, st, trigger=trigger, reading=_feed_reading(pid))
        if owed.reason == "power_loss_prompt" and trigger != "T2":
            # A reboot: the power-loss recovery (``pause_recovery``) owns the rebooted printer's resume.
            _leave_for_power_loss(incident)
            await _leave_open(incident)
            return
        decision = _resume_decision(owed.reason)
        match decision:
            case "refill":
                refill = await _refill_toolhead(incident, evidence=evidence)
                match refill:
                    case "abort":
                        # The operator took the printer during the refill: the stamp stays, the farm
                        # chose the feeder (``_give_up``'s rule).
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
                logger.info(
                    "spool_recovery: printer %s refill (%s) withheld at publish — %s", pid, trigger, owed.reason
                )
                if trigger != "T2":
                    await _escalate(incident, "toolhead_refill_withheld", evidence=evidence)
                return
            case _:
                assert_never(decision)
        if trigger == "T2":
            logger.info(
                "spool_recovery: printer %s incident %s — the farm's command ran after the hand-over; the toolhead "
                "%s; the print stays PAUSED",
                pid,
                incident.incident_id,
                f"is refilled from tray {result.tray}" if result is not None else f"reads {owed.reason}",
            )
            return
        answer, reason = await _resume_row(incident, printer_action.resume())
        match answer:
            case command_outcome.Answer.REACHED:
                break
            case command_outcome.Answer.REFUSED:
                await _escalate(incident, "resume_refused", refill=result, evidence=evidence, printer_words=reason)
                return
            case command_outcome.Answer.TAKEN_OVER:
                await _leave_open(incident)
                return
            case command_outcome.Answer.VOID:
                st, _waited = await _live_reading(incident)
                if power_loss_prompt_standing(getattr(st, "hms_errors", None) or []):
                    _leave_for_power_loss(incident)
                    await _leave_open(incident)
                    return
                if _live_state(st) == "PAUSE" and same_job(getattr(st, "subtask_id", None), incident.job_id) == "same":
                    logger.info(
                        "spool_recovery: printer %s incident %s — the farm's resume was void (%s); the print is still "
                        "PAUSED, so the resume is owed again",
                        pid,
                        incident.incident_id,
                        reason,
                    )
                    continue
                await _leave_open(incident)
                return
            case _:
                assert_never(answer)
    settled, token = await _await_resumed_feed(incident)
    match settled:
        case "fed":
            pass
        case "air":
            logger.info(
                "spool_recovery: printer %s resumed (%s, incident %s, %s) and printed a layer with nothing fed",
                pid,
                trigger,
                incident.incident_id,
                result or "fed at publish",
            )
            match await _stop_printing_on_air(incident, evidence):
                case "stopped" | "escalated":
                    pass  # the stop wrote the row's outcome
                case "taken_over" | "not_air":
                    await _leave_open(incident)
                case _ as never:
                    assert_never(never)
            return
        case "closers" | "paused":
            reading = _feed_reading(pid)
            logger.info(
                "spool_recovery: printer %s resumed (%s, incident %s) and the feed state reads %s — %s; the row is "
                "left open for its closers",
                pid,
                trigger,
                incident.incident_id,
                reading.phase.value if reading is not None else "nothing",
                "the firmware owns the toolhead" if settled == "closers" else "PAUSEd again over a standing fault",
            )
            await _leave_open(incident)
            return
        case "abort":
            await _abort(incident, token=token, restored=result is not None)
            return
        case "handover":
            _hand_over(incident)
            return
        case _:
            assert_never(settled)
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


def _leave_for_power_loss(incident: RecoveryIncident) -> None:
    """The refill episode never sends its own resume at the firmware's post-reboot prompt (``0300_8007``):
    the power-loss recovery (``pause_recovery``) answers it and resumes the interrupted print. The row is
    left open; its closers close it on the RUNNING-fed edge."""
    logger.info(
        "spool_recovery: printer %s incident %s — the power-loss prompt stands; the power-loss recovery resumes "
        "the print, the row is left open for its closers",
        incident.printer_id,
        incident.incident_id,
    )


async def _resume_row(
    incident: RecoveryIncident, command: printer_action.Command
) -> tuple[command_outcome.Answer, str | None]:
    """The refill episode's ONE resume on its row, and the printer's answer to it with the record's
    reason. A farm resume already OUTSTANDING on the row (a restarted episode's, sent before the
    restart) is waited for, never sent behind — the once is the record's. Otherwise ``command`` (the
    episode's resume frame) is sent until its frame goes out (:func:`_send_until_published`) and its
    answer read (:func:`_await_action`)."""
    last = printer_incidents.last_farm_action(incident.printer_id, incident.incident_id, "resume")
    if last is not None and last.outstanding:
        action_id = last.action_id
        logger.info(
            "spool_recovery: printer %s incident %s — the farm's resume (action %s) is still outstanding; waiting "
            "for the printer's answer",
            incident.printer_id,
            incident.incident_id,
            action_id,
        )
    else:
        action_id = (await _send_until_published(incident, command)).action_id
    answer = await _await_action(incident, action_id)
    return answer, await printer_action.reason_of(action_id)


# What the toolhead's FEED STATE answered the refill episode's resume (:func:`_await_resumed_feed`)
# — CLOSED:
#
# * ``fed``      — FED or TAIL: filament reaches the nozzle (the refill's load, or a toolhead fed at
#                  publish);
# * ``air``      — AIR: the print runs on nothing — a layer printed while empty: it is STOPPED;
# * ``closers``  — the FIRMWARE owns the toolhead's next motion (CHANGING: its posture, its runout
#                  switch or resume, a tool change; START_BLOCK: the start block's own load;
#                  END_RETRACT: the end-of-print retract), or the job's lifecycle closed (IDLE) —
#                  none of them air, all of them the row's closers' to answer once fed or over;
# * ``paused``   — the printer PAUSEd again over a standing fault, a runout demand or a change (a
#                  QUIET pause is another actor's: :func:`command_outcome.takeover`'s ``paused_elsewhere``);
# * ``abort`` / ``handover`` — a takeover (:func:`command_outcome.takeover`), as at every other wait of the driver.
ResumedFeed = Literal["fed", "air", "closers", "paused", "abort", "handover"]

# THE wait's phase cells — TOTAL over ``FeedPhase`` (a missing cell RAISES at :func:`_resumed_feed`;
# ``test_spool_recovery`` pins completeness). ``None`` keeps waiting: RUNNING with nothing fed and no
# firmware sequence YET (the first frames of an auto-switch read exactly so), or no reading. The two
# PAUSE-only phases are answered by the live state before the table is read (``paused``); their cells
# say the same, so the table stays total.
_RESUMED_FEED: dict[FeedPhase, ResumedFeed | None] = {
    FeedPhase.FED: "fed",
    FeedPhase.TAIL: "fed",
    FeedPhase.AIR: "air",
    FeedPhase.CHANGING: "closers",
    FeedPhase.START_BLOCK: "closers",
    FeedPhase.END_RETRACT: "closers",
    FeedPhase.IDLE: "closers",
    FeedPhase.RUNOUT_HELD: "paused",
    FeedPhase.EMPTY_PAUSED: "paused",
    FeedPhase.EMPTY_UNCONFIRMED: None,
    FeedPhase.UNKNOWN: None,
}


def _resumed_feed(phase: FeedPhase) -> ResumedFeed | None:
    try:
        return _RESUMED_FEED[phase]
    except KeyError:
        raise LookupError(
            f"spool_recovery: no resume-read answer for feed phase {phase!r} — every phase needs its own row"
        ) from None


async def _await_resumed_feed(incident: RecoveryIncident) -> tuple[ResumedFeed, TakeoverToken | None]:
    """The refill episode's read of its OWN resume, once the printer reported RUNNING: wait for the
    toolhead's feed state (invariant 16) to answer — :data:`_RESUMED_FEED` — and return that answer
    with the takeover token that ended the wait, if one did. It replaces the one raw ``toolhead_feed``
    read that stranded 015-H2S and 001-H2S PAUSED on 2026-10-10: the farm had resumed into the
    firmware's own runout switch, whose first frames read RUNNING, ``tray_now = 255``, no posture.

    Event-driven on the pushes, the way the driver's other waits are: every poll reads through the
    session gate (:func:`_live_reading`: a gap is a wait), asks :func:`command_outcome.takeover`
    first (``awaiting=None``: RUNNING is this wait's own reading; ``after_contract_resume``: a QUIET
    PAUSE after the farm's resume is another actor's, ``paused_elsewhere``), then a PAUSE
    (``paused``), then the phase. NO elapsed time decides anything (Raymond 2026-10-10: "time is not
    the right signal"), and nothing here pages: a print RUNNING with nothing fed is the attention
    owner's level reader (``farm_stall``), however this wait came to be in it (2026-10-11)."""
    pid = incident.printer_id
    while True:
        st, _waited = await _live_reading(incident)
        token = command_outcome.takeover(_driver_reading(incident, st), awaiting=None, after_contract_resume=True)
        if token is not None:
            return _note_takeover(incident, token, "resume_read"), token
        if _live_state(st) == "PAUSE":
            return "paused", None
        reading = _feed_reading(pid)
        settled = _resumed_feed(reading.phase if reading is not None else FeedPhase.UNKNOWN)
        if settled is not None:
            return settled, None
        await asyncio.sleep(_POLL_INTERVAL_S)


async def _clear_hold_projection_of(incident: RecoveryIncident) -> None:
    """Drop the closed row's hold token from its unit (``printer_incidents.clear_hold_projection``,
    the one clear). Best-effort."""
    from backend.app.core.database import async_session

    try:
        async with async_session() as db:
            await printer_incidents.clear_hold_projection(db, incident.item_id)
    except Exception:  # noqa: BLE001 — bookkeeping must not undo a successful resume
        logger.exception("spool_recovery: hold-token clear failed for printer %s", incident.printer_id)


def _resume_went_out(verdict: ResumeVerdict, printer_id: int, name: str) -> bool:
    """An evidence lane's reading of the verb: True when ONE resume went out — the printer's answer
    to it is an event the record reads (:func:`on_action_answered`), never a window this lane waits
    out. Every other verdict stands the lane aside (no retry, no quarantine, no stamp, its guidance
    untouched), logged where the verb did not."""
    match verdict:
        case ResumeSent():
            return True
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

    The resume is THE resume (:func:`resume_paused_print`, K9 — its settle, its re-ask and its one
    publish, as the farm's action on the runout row); this owns the evidence
    (``runout_auto_resume_enabled`` + :func:`_resume_ready`). The open runout row is K7's
    ``runout_demand``: the firmware loads the SAME slot on the resume, so no refill goes out
    (invariant 9). Returns True when the resume went out; the operator is told once the printer answers
    (:func:`on_action_answered`: the RUNNING report reaches the action, however late), and the runout row
    closes through the rule table once the print prints a layer past the fault, or at the job's terminal
    (R27).
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

    runout_row = printer_incidents.snapshot(printer_id, kind=KIND_RUNOUT)
    verdict = await resume_paused_print(
        printer_id, actor="refill_lane", ready=_ready, incident_id=runout_row.get("id") if runout_row else None
    )
    if not _resume_went_out(verdict, printer_id, "refill"):
        return False
    logger.info(
        "spool_recovery: printer %s resume sent after the filament refill (slot %s) — the runout hold closes once "
        "the print prints past the fault",
        printer_id,
        slot,
    )
    return True


async def _resume_after_repair(printer_id: int, incident_id: int) -> bool:
    """Resume a print whose toolhead the printer or a person put back — a filament path the
    operator repaired by hand, or an empty toolhead loaded again with no farm motion acting
    (:func:`_maybe_self_heal_after_repair`, the one spawner). Never raises.

    The 003-H2S self-heal, and the other half of what the ``repair`` resolution class
    means: the commonest fix for a physical fault on this farm is somebody freeing
    the path and loading a slot at the printer — 23 of the AMS-side physical rows
    closed on a resume with no filament change at all — after which the print sits
    PAUSEd for a button nobody is standing there to press. The ``toolhead`` class is the
    same shape (operator-ratified 2026-10-10, P2).

    Deliberately NOT gated on ``runout_auto_resume_enabled``: that setting is the
    operator's control over the RUNOUT lane (a refill they may want to inspect before
    the print continues), and reading it here would make one switch govern two
    different decisions. The evidence is the gate.

    Its ``ready`` re-asks everything that could have moved: the printer is still PAUSEd, the row is
    still open, and the rule table's ONE resume verdict (``incident_resolution.resume_owed`` — the
    row's class, its evidence, its own job PAUSEd, maintenance mode, the plate check, the once) still
    holds. The resume is THE resume (:func:`resume_paused_print`, K9): a toolhead that reads EMPTY at
    publish — a farm pull-back the AMS ran after the operator's load (011/014-H2S 2026-10-09/10) — is
    refilled on this row before it resumes, never resumed onto air. Once it went out the printer's
    answer is an event (:func:`on_action_answered`: a refusal escalates with the printer's words), and
    the sweep's own cells close the row on the running edge (a print feeding through the path, a print
    running fed), so there stays ONE closer.
    """

    async def _ready() -> bool:
        st = _get_state(printer_id)
        if st is None or (getattr(st, "state", None) or "") != "PAUSE":
            return False
        from backend.app.core.database import async_session

        async with async_session() as db:
            row = await db.get(PrinterIncident, incident_id)
            if row is None or row.resolved_at is not None:
                return False
            # The same question the spawner asked, asked of the same verdict after the settle: is a
            # resume still owed on this row? The class test and the shared guards are the table's.
            owed = incident_resolution.resume_owed(
                row,
                Context(
                    state=st,
                    driver_live=printer_incidents.driver_live(printer_id),
                    command_pending=_pending_on(row),
                    feed=_feed_reading(printer_id),
                    observed_load_at=printer_action.observed_load_at(printer_id),
                    job_layer=_printed_layer(printer_id),
                ),
            )
        if not owed.owed:
            logger.info(
                "spool_recovery: printer %s incident %s — the resume is no longer owed (%s); not resuming",
                printer_id,
                incident_id,
                owed.evidence,
            )
        return owed.owed

    verdict = await resume_paused_print(printer_id, actor="repair", ready=_ready, incident_id=incident_id)
    if not _resume_went_out(verdict, printer_id, "path repair"):
        return False
    logger.info(
        "spool_recovery: printer %s resume sent (incident %s) — the printer's answer is the record's",
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
      print the feed state reads on AIR (stopped, the unit retried — operator ruling 2026-10-12).

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
        # "this is no longer the same standing fault" (see :data:`_blocked`). A pause edge is
        # one this session WITNESSED: the first sample after a (re)start has no previous state
        # to have left, and a PAUSE it finds is the gate's level to read, not an edge.
        _rearm_blocked(
            printer_id,
            fault_tokens(candidates),
            paused_edge=(live == "PAUSE" and prev is not None and prev[0] != "PAUSE"),
        )

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

    * **D1 — PAUSE** is no longer a per-push level: it is :func:`on_action_answered`, the record's
      answer that the farm's queued command RAN. A toolhead a HUMAN emptied at the screen with nothing
      pending is not the farm's: left alone.
    * **D2 — RUNNING.** The print runs on AIR and K7(T4) owes it: the toolhead's feed state reads
      ``AIR`` (invariant 16 — a layer printed while empty) and no policy guard holds (no eject, no
      queued farm command, no reboot prompt, no open runout row — invariant 9, a runout is never an air
      stop — no maintenance mode): the print is ruined, so the farm STOPS it and the unit is retried
      (operator ruling 2026-10-12: "printing on air = ruined print already") — a screen resume onto
      an empty toolhead (T4, shape 39), or a pending pull-back that drained under a running print after
      a person's screen Retry (T2-running: the step is answered first; shape 46). Never over a row whose
      farm air stop the printer did not take (``printer_incidents.air_stop_sent``): the stop was resent
      once and handed to a person, and the detector never loops on it. Never the first empty
      frame: that is also the first frame of the firmware's runout AUTO-SWITCH (RUNNING,
      ``tray_now = 255``, the posture not yet up — 005/015/001-H2S 2026-10-10, broken into by this
      detector when it read that frame), which the feed state reads ``EMPTY_UNCONFIRMED`` and then
      ``CHANGING``. A LEVEL on the phase, never the ``air_began`` event, so a deferral (a live driver,
      a stale session) can never swallow it: the first fresh sample after a farm restart acts; never
      twice for one episode, because the registered driver guards it while it lives and after it the
      toolhead reads fed again (or the print is paused, which D2 never reads)."""
    if printer_incidents.driver_live(printer_id) or not live_reading.reads_live(state):
        return
    if not _prints_on_air(printer_id, state):
        return
    if printer_incidents.air_stop_sent(printer_id):
        return
    from backend.app.core.tasks import spawn_background_task

    incident_id, settle = _ran_farm_motion(printer_id, state)
    task = spawn_background_task(
        _refill_episode(printer_id, trigger="T4", incident_id=incident_id, settle=settle),
        name=f"toolhead-refill-p{printer_id}",
    )
    printer_incidents.register_driver(printer_id, task, incident_id=incident_id)
    logger.info(
        "spool_recovery: printer %s printed a layer with nothing at the toolhead — %s; the farm stops the print",
        printer_id,
        f"incident {incident_id}" if incident_id is not None else "no row",
    )


def _ran_farm_motion(printer_id: int, state) -> tuple[int | None, int | None]:
    """The row and the action of a farm motion command that RAN after the hand-over — what D2 answers
    first (T2 under a RUNNING print: a pending pull-back drained under it, shape 46). The pending
    command the toolhead now shows ran (``command_outcome.pending_settles``); else a farm motion the
    RECORD already answered ``reached`` whose step still pends. ``(pending row, None)`` when the
    pending command has not run; ``(None, None)`` when no farm motion is in play."""
    pending = printer_incidents.pending_command(printer_id)
    if pending is not None:
        live = tray_fields.toolhead_feed(state).active
        ran = command_outcome.pending_settles(pending.name, pending.target, pending.loaded_at_send, live)
        return pending.incident_id, (pending.action_id if ran is not None else None)
    for row in printer_incidents.snapshots(printer_id):
        incident_id = row.get("id")
        if not isinstance(incident_id, int):
            continue
        motions = [a for a in printer_incidents.farm_actions(printer_id, incident_id) if a.command in _COMMANDS]
        if motions and motions[-1].answer == command_outcome.Answer.REACHED.value:
            if printer_incidents.command_pends(motions[-1].step_reading):
                return incident_id, motions[-1].action_id
    return None, None


def on_action_answered(event: ActionAnswered) -> None:
    """The RECORD's answer to a FARM action on an open row (``printer_events.ActionAnswered``,
    2026-10-11) — an event, so a deferral can never swallow it: a restart re-reads the outstanding
    action at the printer's session start and answers it then. A live driver reads the same answer
    in its own wait, so nothing here acts while one lives. Sync, guarded (invariant 10).

    * A farm ``resume`` (the evidence lanes' — :func:`_on_farm_resume_answered`).
    * D1 — a FARM AMS motion command the printer answered ``reached`` — the AMS RAN it — with the print
      PAUSED: the AMS ran it after the hand-over (an accepted pull-back runs on its own ~4.5 min after
      the send — 011-H2S 04:31:27 → 04:36:01, 014-H2S 02:45:58 → 02:50:38). The episode answers its
      step, then refills the now-empty toolhead on that row when K7(T2) owes it; the print STAYS
      paused (:func:`_await_pending_command` is a live driver's read of the same answer)."""
    try:
        if event.actor != ACTOR_FARM or event.incident_id is None:
            return
        if event.command == "resume":
            _on_farm_resume_answered(event)
            return
        if event.answer != command_outcome.Answer.REACHED.value or event.command not in _COMMANDS:
            return
        pid = event.printer_id
        if printer_incidents.driver_live(pid) or printer_incidents.cached_kind(pid, event.incident_id) is None:
            return
        state = _get_state(pid)
        if state is None or not live_reading.reads_live(state) or _live_state(state) != "PAUSE":
            return
        from backend.app.core.tasks import spawn_background_task

        task = spawn_background_task(
            _refill_episode(pid, trigger="T2", incident_id=event.incident_id, settle=event.action_id),
            name=f"toolhead-refill-p{pid}",
        )
        printer_incidents.register_driver(pid, task, incident_id=event.incident_id)
        logger.info(
            "spool_recovery: printer %s PAUSED and the farm's queued %s ran (action %s on incident %s); the farm "
            "answers it and refills the toolhead",
            pid,
            event.command,
            event.action_id,
            event.incident_id,
        )
    except Exception:  # noqa: BLE001 — invariant 10: a farm-side failure must not break the event chain
        logger.exception("spool_recovery: the record's answer of action %s could not be read", event.action_id)


def _on_farm_resume_answered(event: ActionAnswered) -> None:
    """The printer answered an evidence lane's resume on an open row (the repair / toolhead self-heal,
    the runout refill lane — :func:`resume_paused_print`), however late:

    * ``reached`` on a RUNOUT row — the refill lane's resume landed: the operator is told
      (:func:`_tell_runout_resumed`). The row itself closes through its own cells like every other row
      (one closer): once the print has printed a layer past the fault, or at the job's terminal (R27) —
      a runout that flaps around the RUNNING edge re-pauses into the same row, whose resume is spent.
    * ``refused`` — the PRINTER refused it. The row's resume budget decides
      (``incident_resolution.resume_spent``, the one decider): still owed — a RUNOUT row's refill under
      its cap of 3 — it is resent on the refill evidence (:func:`_reown_runout_resume`: the level read,
      after its settle); spent, the farm gives up — the row escalates with the printer's words
      (``resume_refused``, a page) and the hold chip reads "Resume refused" off the record. Every other
      row's budget is one resume, so its refusal pages at once.
    * ``void`` — the printer never received it (a later session began with it unacknowledged, a reboot,
      the job ended): the row is owed its resume again, and the next sweep or session start sends it.
    * ``taken_over`` — a person's command took the printer: theirs now.

    A resume the printer leaves UNANSWERED past its self-heal bound is :func:`on_self_heal_due`'s.
    Stands aside while a driver is live — the refill episode reads its own resume (:func:`_resume_row`)."""
    pid = event.printer_id
    if printer_incidents.driver_live(pid):
        return
    from backend.app.core.tasks import spawn_background_task

    match event.answer:
        case command_outcome.Answer.REACHED.value:
            spawn_background_task(_tell_runout_resumed(event), name=f"runout-resumed-p{pid}")
        case command_outcome.Answer.REFUSED.value:
            spawn_background_task(
                _after_unexecuted_resume(pid, event.incident_id, "resume_refused", printer_words=event.reason),
                name=f"resume-refused-p{pid}",
            )
        case _:
            logger.info(
                "spool_recovery: printer %s incident %s — the farm's resume (action %s) answered %s%s",
                pid,
                event.incident_id,
                event.action_id,
                event.answer,
                f" ({event.reason})" if event.reason else "",
            )


# The durable once-guard of the refill lane's "resumed after the refill" notice (``notify_dedup``'s ledger,
# keyed by the runout row): a refill resume the printer left unanswered past its self-heal bound is resent,
# and ONE RUNNING report then reaches both resumes — the operator is told once.
_RUNOUT_RESUMED_SCOPE = "runout_auto_resumed"


async def _tell_runout_resumed(event: ActionAnswered) -> None:
    """The refill lane's resume REACHED: tell the operator the print resumed after the refill — ONCE per
    row, under the printer's resume lock and the durable once-guard (:data:`_RUNOUT_RESUMED_SCOPE`): an
    unanswered resume the lane re-sent is reached by the same RUNNING report as its successor.

    The row is NOT closed here (R27, 2026-10-12). Production closed it on this confirm, and a runout that
    flapped around the RUNNING edge then re-opened as a new row owed a fresh farm resume; the row (and the
    unit's hold token, its projection) now ends through the rule table — on the first layer the print
    prints past the fault (the sweep), or at the job's terminal. Best-effort."""
    from backend.app.core.database import async_session
    from backend.app.models.printer import Printer
    from backend.app.services.notification_service import notification_service

    try:
        async with _resume_locks.setdefault(event.printer_id, asyncio.Lock()), async_session() as db:
            row = await db.get(PrinterIncident, event.incident_id)
            if row is None or row.kind != KIND_RUNOUT:
                return
            if await notify_dedup.last_sent_at(db, _RUNOUT_RESUMED_SCOPE, str(row.id)) is not None:
                return  # an earlier resume on this hold already told the operator
            printer = await db.get(Printer, event.printer_id)
            printer_name = (printer.name if printer else None) or f"printer {event.printer_id}"
            st = _get_state(event.printer_id)
            job_name = (getattr(st, "subtask_name", None) or "").strip() or "print"
            await notification_service.on_runout_auto_resumed(
                printer_id=event.printer_id,
                printer_name=printer_name,
                job_name=job_name,
                slot_desc=runout_slot_desc(row.slot_global_tray) or "the filament slot",
                db=db,
            )
            await notify_dedup.record_sent(db, _RUNOUT_RESUMED_SCOPE, str(row.id))
        logger.info(
            "spool_recovery: printer %s RESUMED after the filament refill (action %s reached) — runout hold %s "
            "closes once the print prints past the fault",
            event.printer_id,
            event.action_id,
            event.incident_id,
        )
    except Exception:  # noqa: BLE001 — bookkeeping must not undo a successful resume
        logger.exception("spool_recovery: post-resume notice failed for printer %s", event.printer_id)


def on_self_heal_due(event: SelfHealDue) -> None:
    """The RECORD passed a farm resume's self-heal bound with no answer (``printer_events.SelfHealDue``) —
    the refill lane's resume on a RUNOUT row (``_RUNOUT_RESUME_SELF_HEAL``, 30 s online; the only farm
    resume that carries a bound): the printer has not carried it out. The row's resume budget decides
    (:func:`_after_unexecuted_resume`): owed again under the cap — resent on the refill evidence — else the
    give-up page (``resume_unanswered``). Its late answer still lands on the record (a RUNNING report
    closes the row). Sync, guarded (invariant 10); stands aside while a driver is live."""
    try:
        if event.command != "resume" or event.incident_id is None:
            return
        pid = event.printer_id
        if printer_incidents.driver_live(pid) or printer_incidents.cached_kind(pid, event.incident_id) != KIND_RUNOUT:
            return
        from backend.app.core.tasks import spawn_background_task

        spawn_background_task(
            _after_unexecuted_resume(pid, event.incident_id, "resume_unanswered"), name=f"resume-unanswered-p{pid}"
        )
    except Exception:  # noqa: BLE001 — invariant 10: a farm-side failure must not break the event chain
        logger.exception("spool_recovery: the self-heal bound of action %s could not be read", event.action_id)


def rehydrated_runout_resume_bound(
    action: printer_action.OutstandingAction,
) -> tuple[printer_action.SelfHealBound, datetime] | None:
    """The record's bound factory for a REHYDRATED farm resume (``printer_action.register_bound_factory``,
    registered by ``printer_event_wiring``): a refill lane's resume on a RUNOUT row that was still out when
    the farm restarted gets its self-heal bound back, anchored at its send (the record counts online
    seconds from the printer's first fresh report in the new process), so a resume the printer accepted and
    never carried out is owed again rather than outstanding forever. Any other resume carries none."""
    if action.actor != ACTOR_FARM or action.incident_id is None:
        return None
    if printer_incidents.cached_kind(action.printer_id, action.incident_id) != KIND_RUNOUT:
        return None
    return _RUNOUT_RESUME_SELF_HEAL, action.sent_at


def _unanswered_resumes(printer_id: int, incident_id: int) -> frozenset[int]:
    """The row's farm resumes the RECORD holds outstanding with their self-heal bound passed — what
    ``incident_resolution.resume_spent`` (which never imports the record's owner) is handed: such a resume
    no longer holds the row's budget awaiting the printer. The row's farm actions off the projection,
    each asked of the record in memory (``printer_action.bound_passed``)."""
    return frozenset(
        action.action_id
        for action in printer_incidents.farm_actions(printer_id, incident_id)
        if action.command == "resume" and printer_action.bound_passed(action.action_id)
    )


async def _after_unexecuted_resume(
    printer_id: int, incident_id: int | None, reason: str, *, printer_words: str | None = None
) -> None:
    """A farm resume on ``incident_id`` the printer did not carry out — ``resume_refused`` (it answered
    ``refused``, with ``printer_words``) or ``resume_unanswered`` (the record passed its self-heal bound).
    ONE decider of what follows, the row's resume budget (``incident_resolution.resume_spent``):

    * still owed (a RUNOUT row's refill under its cap) → resent on the refill evidence
      (:func:`_reown_runout_resume`);
    * spent with the give-up owed (``gave_up``) → ONE escalation of the row with the printer's words
      (``reason`` — a page: "Press Resume."), through THE escalation (:func:`_escalate`) on the row's own
      driver context — production's refill lane had no page of its own here (it stood aside, the
      runout escalation's guidance standing), so this is the rewrite's page, after the cap;
    * spent otherwise (another resume is out awaiting the printer, one reached, a person took it over) →
      nothing.

    Best-effort; a row already closed is left alone."""
    if incident_id is None:
        return
    from backend.app.core.database import async_session

    try:
        spent = incident_resolution.resume_spent(
            printer_id, incident_id, unanswered=_unanswered_resumes(printer_id, incident_id)
        )
        if spent is None:
            logger.info(
                "spool_recovery: printer %s incident %s — the farm's resume was not carried out (%s); it is owed again",
                printer_id,
                incident_id,
                reason,
            )
            await _reown_runout_resume(printer_id, incident_id)
            return
        if not spent.gave_up:
            logger.info(
                "spool_recovery: printer %s incident %s — a farm resume was not carried out (%s); %s",
                printer_id,
                incident_id,
                reason,
                spent.why,
            )
            return
        async with async_session() as db:
            row = await db.get(PrinterIncident, incident_id)
            if row is None or row.resolved_at is not None:
                return
            incident = await _incident_of_row(db, row, _get_state(printer_id))
        logger.warning(
            "spool_recovery: printer %s incident %s — %s; the farm gives up", printer_id, incident_id, spent.why
        )
        await _escalate(incident, reason, printer_words=printer_words)
    except Exception:  # noqa: BLE001 — invariant 10
        logger.exception("spool_recovery: the unexecuted-resume follow-up failed for printer %s", printer_id)


async def _reown_runout_resume(printer_id: int, incident_id: int) -> None:
    """A runout row owed its refill resume again: the LEVEL read (:func:`_maybe_resume_runout` — the row's
    job PAUSEd, the refill evidence on the live report, the holds, the operator's setting, the budget) on
    the live report now, which spawns the refill lane (its 15 s settle, then the resend). The next refill
    edge (the demand clearing, a presence gain) and the sweep read the same level; whichever comes first
    sends it, the resume lock and the budget make it one."""
    from backend.app.core.database import async_session

    async with async_session() as db:
        row = await db.get(PrinterIncident, incident_id)
    if row is None or row.resolved_at is not None:
        return
    await _maybe_resume_runout(row, _live_report(printer_id))


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
