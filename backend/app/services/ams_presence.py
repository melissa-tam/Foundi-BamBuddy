"""Mid-run AMS refill recognition (presence + terminal RFID re-read).

Single owner of AMS presence-transition policy. Everything downstream of
``main.on_ams_change`` already handles refills (RFID auto-create/assign, reused-
tag respool, tagless auto-mint/auto-config in ``services.spool_tagless``, low-
spool staged-unit auto-release), but that pipeline only fires when the AMS
change-hash changes. This module closes the gap the hash alone cannot see — a
spool inserted while idle or mid-print that the firmware never auto-reads:

* :func:`on_tray_observations` — presence-transition tracking, called from
  ``printer_manager._run_slot_pipeline_pass`` on the RAW observation stream, one pass
  ahead of the slot pipeline that resolves the same push (E1, 2026-08-07: the merged
  lane could be BLIND to a genuine insert, so presence edges moved to the lane that
  already owns identity). A genuine presence
  GAIN is a CHANGE: it records the physical cycle that becomes the discovery lane's
  evidence, and while the printer is idle it immediately spends that evidence on one
  read so a Bambu spool resolves via the normal tag path within seconds. It NEVER
  prompts: a
  tagless spool is now silently minted/configured by ``services.spool_tagless``
  (there is no more ``new_spool_detected`` event). A presence LOSS only updates
  the last-presence map — NO silent auto-unassign (a spool pulled for drying keeps
  its assignment and gram history).

* :func:`on_printer_terminal` — the NEED-DRIVEN reconcile sweep, called from
  ``main.on_print_complete`` (skipped for eject-job terminals). Production runs are
  continuous and the AMS does not always read a mid-print insert before the next
  print starts, so the between-prints window is where the farm reconciles. What it
  may command is decided per slot by :func:`identify_needed`:

  - ``"rfid_refresh"`` — the slot is live-tagged, or DB-bound to a spool that carries a
    tag identity, AND a read OCCASION is open for it. The read SUCCEEDS and restores
    ``remain`` (rule 8's wire truth for a tagged row, which the W6 ledger-decrease
    repair has nothing to work from without). A healthy tagged slot publishes ``remain``
    on every ordinary AMS push, so the occasion — a qualified physical cycle, or the
    terminal's between-prints policy (:func:`_terminal_read_occasion`: wire remain
    missing / ledger past label / a spent-latched binding under an unidentified roll) —
    is what keeps this from becoming a per-pass read loop.
  - ``"discovery"`` — a qualified physical cycle was recorded for the slot since its
    last commanded/observed read and it is still unidentified: something changed and
    the farm does not know what. ONE read answers it either way — a tag gives full
    data, and a FAILED read is itself the answer "no tag ⇒ tagless ⇒ the Bambu Black
    PETG default assumption stands". That expected failure is suppressed farm-side
    (:func:`is_expected_read_failure`), so discovery costs the operator nothing.
  - ``None`` — everything else, and in particular an UNTOUCHED tagless slot is never
    read. A commanded RFID read on a slot with no tag can only fail, and the firmware
    reports that failure as HMS ``0700_2X00_0001_0081`` / ``0700_4025`` ("the AMS main
    board may be malfunctioning") — which can NEVER self-clear on a tagless slot.
    Re-reading untouched tagless slots after every print was the standing-error
    factory this module previously was; it is gone.

  Results flow the normal RFID pipeline; this module does not duplicate it.

* :func:`maybe_command_owed_identify` — the DRAIN, called per slot from
  ``spool_tagless.reconcile_slot_config``'s scheduler-tick walk. Both lanes above are
  event-shaped and can miss a ``"discovery"`` verdict forever (a gain during a print is
  never re-read; the terminal sweep defers every slot while filament is engaged, which
  on a continuously-loaded printer is always). This one supplies the missing OCCASION —
  the same need check, the same wire-safety refusals, discovery reads only.

Every ``ams_get_rfid`` the farm issues goes through the single commander
:func:`command_identify`, which owns the need check, the echo-consume arming, the
read bookkeeping and the discovery stamp.

Presence is TRI-STATE, owned by ``tray_fields.tray_presence`` (one origin for every
consumer): ``state ∈ {10, 11}`` is present; a non-present state whose push ALSO asserts
an empty ``tray_type`` is the verified prod cleared shape and reads absent; everything
else — a partial push with no parseable state, an unknown dialect code (H2C idle empties
report ``state=0``), a state-9 slot still asserting a filament type — reads UNKNOWN and
derives NO edge. So an H2C never reads as phantom spools, and silence about a slot is
never mistaken for an empty one.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, NamedTuple

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.core.websocket import ws_manager
from backend.app.services import hms_errors
from backend.app.services.bambu_mqtt import AMS_STATUS_IDENTIFYING, TRAY_PRESENT_STATES
from backend.app.services.printer_manager import printer_manager
from backend.app.services.spool_tag_matcher import is_valid_tag
from backend.app.services.tray_fields import parse_int_field, tray_identity_asserted
from backend.app.services.tray_observation import TrayObservation, observation_tray_dict

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# An identify cycle runs ≤~25 s, so an ``_echo_pending`` flag fresher than this
# means the commanded identify is (or may still be) in flight — used by
# :func:`identify_in_flight` and the terminal sweep's command-time skip to keep at
# most one identify per slot running. This is DELIBERATELY tighter than
# ``_ECHO_PENDING_STALE_S`` (120): that value is only a GC bound for a command lost
# to a race, not a statement that an identify is still active.
_IDENTIFY_ACTIVE_S = 30

# The ``tray_now`` sentinel bambu_mqtt.PrinterState uses for "no filament engaged"
# (``tray_now: int = 255``; the client's ``ams_refresh_tray`` guard is literally
# ``tray_now != 255``). bambu_mqtt exposes no named constant or predicate for it, so —
# per the fork's mirror-don't-duplicate rule — the value lives ONCE here, consumed only
# by :func:`_filament_engaged`, whose docstring names the client guard it mirrors. If
# bambu_mqtt later grows a ``TRAY_UNLOADED`` constant / ``filament_engaged`` predicate,
# import it and delete this (the single-origin treatment ``TRAY_PRESENT_STATES`` and
# ``AMS_STATUS_IDENTIFYING`` already get above).
_TRAY_UNLOADED = 255

# --- Module-level edge state (matches the fork's other event-edge bookkeeping,
#     e.g. farm_staging._tray_signatures). Lost on restart; startup priming and
#     the first-push seeding tolerate that. -----------------------------------

# (printer_id, ams_id, tray_id) -> last observed physical presence (bool).
_last_presence: dict[tuple[int, int, int], bool] = {}

# (printer_id, ams_id, tray_id) -> time.monotonic() at which the slot last went
# PRESENT→ABSENT. On a later genuine GAIN the elapsed absence tells a real physical
# roll swap (≥ _MIN_PHYSICAL_ABSENT_S) apart from the runout-instant state flap that
# a firmware backup switch produces (sub-second). Drives the W1 spent-binding latch
# release / W5 fresh-roll prompt via spool_tagless.note_physical_cycle.
_absent_since: dict[tuple[int, int, int], float] = {}

# A physical roll swap keeps the slot empty for at least this long (pull the old
# roll, seat a new one); a firmware runout state flap is sub-second. A code
# constant, not operator-tunable.
_MIN_PHYSICAL_ABSENT_S = 5.0

# How long a slot may read ABSENT and still have its returning roll treated as the
# roll that left — the DE-BOUNCE window (``slot_pipeline._debounce_candidate``,
# table row 4c).
#
# **This is a GLITCH FILTER, and it is NOT the 5-SECOND flap filter above.** The two
# answer different questions and must never be conflated: ``_MIN_PHYSICAL_ABSENT_S``
# asks "did the wire really lose this tray, or did the state flap?", while this asks
# "is the release the farm acted on explicable as a SPURIOUS one?". Neither decides
# identity positively (doctrine rule 7 as amended 2026-08-19): outside this window
# the farm asserts NOTHING about the roll and the table MINTS; inside it a reclaim is
# admitted as a de-bounce for a release that most likely never happened, bounded to
# the slot's single last occupant, untagged, MEASURED, and disqualified by CAUSE
# whenever the release has a physical explanation (a runout, a mid-print pull).
#
# 300 s is read off the fleet, not chosen: over 8 days, 52 reclaims had a matched
# prior release on the same slot. Eleven returned in under 2 minutes (four at 0.0 min,
# on four different printers inside one minute — no human does that; those are the
# spurious releases this lane silently repairs) and three more between 2 and 5 min.
# The short cluster ENDS at 1.7 min and the next reclaim in the whole record is at
# 23 min, so five minutes sits in an empty valley: every gap the fleet actually
# produces is either far inside it or far outside it.
_RESEAT_WINDOW_S = 300.0

# (printer_id, ams_id, tray_id) -> whether the slot's CURRENT absence began under
# identify activity (:func:`_identify_explains_absence` at the PRESENT→ABSENT edge).
# An RFID identify UNLOADS the tray for ~10–20 s, so that absence is a read flap and
# NOT a roll swap: its later gain must be recorded UNQUALIFIED however long it ran —
# the ≥ _MIN_PHYSICAL_ABSENT_S filter measures duration, NEVER identity. Parallel to
# _absent_since (set and popped in lockstep with it); a missing entry reads as "not
# identify-explained", the same conservative default every edge map here takes.
_absent_under_identify: dict[tuple[int, int, int], bool] = {}

# (printer_id, ams_id, tray_id) -> whether the slot was the ACTIVE FEEDER of a LIVE
# print at the PRESENT→ABSENT edge (:func:`_slot_was_active_feeder`, evaluated at that
# edge while the wire evidence is freshest). The third stamp beside the two above, and
# the reason it exists is the ~3-minute gap the 2026-08-13 wave measured: **the AMS
# clears a drained slot's exist bit ~3 min BEFORE it declares the runout**. Inside that
# gap the departed row is released but not yet ``spent_at``-stamped, so nothing in the
# de-bounce donor query excludes it and a refill made during the gap would re-bind the
# EXHAUSTED row onto the fresh roll (the 08-13 resurrection, shape 31, one incident
# short of repeating).
#
# A slot losing presence WHILE IT IS FEEDING is running out or being pulled mid-print.
# Neither is a glitch, so its return is a REFILL and must mint. Decided by CAUSE, never
# by timing (doctrine rule 6 / invariant 6) — the same discipline as
# :data:`_absent_under_identify`, which it is set and popped in lockstep with.
_absent_under_active_feed: dict[tuple[int, int, int], bool] = {}


class _Reseat(NamedTuple):
    """What the slot's most recent presence GAIN measured about the absence before it.

    ``absent_for`` is the MEASURED absence in seconds, or ``None`` when its start was
    never observed (a restart's first batch, a tray first seen in a later partial, two
    coalesced edges). ``None`` is not "short" — it is UNKNOWN, and an unknown duration
    can never license a de-bounce (scenario T11).

    ``under_active_feed`` is :data:`_absent_under_active_feed` as it stood at the loss
    edge that opened this absence.
    """

    absent_for: float | None
    under_active_feed: bool


# (printer_id, ams_id, tray_id) -> the slot's most recent :class:`_Reseat`. Written at
# the GAIN edge, dropped at the next LOSS edge (a slot that is absent again has no
# standing return to speak for), and read — never consumed — by the slot pipeline's
# de-bounce lane. Non-consuming on purpose, exactly like ``_physical_cycle_at``: the
# pipeline may DEFER a slot for several pushes (a settle window, a drying unit) and the
# fact it needs is a property of the wire's last edge pair, not of any one pass.
_reseat: dict[tuple[int, int, int], _Reseat] = {}

# Printers whose first observation batch (post-restart) has been processed. The first
# batch only seeds the presence map (no re-read); later pushes act on gains. The connect
# pushall carries every tray, so that batch is comprehensive; a tray first SEEN in a
# later partial is a genuine gain with an unobserved absence start (see
# :func:`on_tray_observations`).
_primed: set[int] = set()

# printer_id -> subtask_id already swept at its terminal. Dedupes duplicate
# on_print_complete callbacks for the same print (one-shot per RUNNING→terminal).
_swept_subtasks: dict[int, str] = {}

# (printer_id, ams_id, tray_id) -> time.monotonic() at which a re-read command was
# issued on a PRESENT slot. A commanded re-read (ams_get_rfid) on an occupied slot
# makes the firmware run a ~20 s identify cycle during which the tray state flaps
# present→9→present; the settle-back is a fresh absent→present GAIN edge that
# on_tray_observations would answer with ANOTHER re-read — a self-sustaining ~22 s loop.
# This one-shot flag lets the NEXT gain on the slot be recognized as our own
# command's echo and swallowed exactly once. It is NOT a time-suppression window:
# empty slots never arm (see record_reread), so a real insertion made right after
# a print ends is never eaten; only the identify echo is.
_echo_pending: dict[tuple[int, int, int], float] = {}

# Echo-consume window == the identify-cycle bound (_IDENTIFY_ACTIVE_S). Within this
# window a presence GAIN on a slot we just re-read is the firmware's identify flap
# settling back and is swallowed; BEYOND it a gain is a REAL physical event (a
# genuine pull+reseat), so the flag is GC'd and the gain acts normally — including
# its feed-fault clear. The old 120 s value swallowed a real reseat made 30–120 s
# after a re-read together with its feed-fault clear; that was a defect (F3).
# Suppresses nothing by itself — an expired flag reads as no flag. A code constant,
# not operator-tunable, like _IDENTIFY_ACTIVE_S above.
_ECHO_PENDING_STALE_S = 30.0

# (printer_id, ams_id, tray_id) -> time.monotonic() of the last genuine presence GAIN
# (echoes and the first-push seed excluded). NON-consuming: read by
# :func:`recent_gain_age` to tell "the firmware is probably still reading this fresh
# insert" from "nothing happened here recently".
_gain_at: dict[tuple[int, int, int], float] = {}

# (printer_id, ams_id, tray_id) -> time.monotonic() of the last QUALIFIED physical
# cycle (a genuine gain whose preceding absence was not a sub-_MIN_PHYSICAL_ABSENT_S
# flap). NON-consuming: read by :func:`last_physical_cycle_age`, and paired with
# _slot_read_at below to answer "did the slot change since we last learned its
# identity?" — the discovery lane's evidence.
_physical_cycle_at: dict[tuple[int, int, int], float] = {}

# (printer_id, ams_id, tray_id) -> time.monotonic() at which the slot's identity was
# last LEARNED: either we commanded a read (:func:`command_identify`) or the firmware
# published a valid tag for it. A cycle older than this stamp has already been
# answered, so it is no longer evidence — this is what makes discovery ONE read per
# change instead of one per print end.
_slot_read_at: dict[tuple[int, int, int], float] = {}

# (printer_id, ams_id, tray_id) -> time.monotonic() at which WE commanded a read whose
# reason was "discovery", i.e. a read the slot may legitimately be unable to answer.
# A filament-read-failure HMS naming that slot within _DISCOVERY_READ_WINDOW_S is the
# expected answer "no tag", not a fault report (see :func:`is_expected_read_failure`).
_discovery_read_at: dict[tuple[int, int, int], float] = {}

# How long after our own discovery read a filament-read-failure HMS still counts as
# that read's answer. Generous relative to the ~20 s identify cycle so the firmware's
# post-cycle fault push is covered, short enough that an UNRELATED read failure
# minutes later still notifies (that one means a genuinely failing AMS reader).
_DISCOVERY_READ_WINDOW_S = 60.0

# Tray states a slot can be identified in: 9 (empty/unread — a mid-print refill
# sometimes stays 9 until read), 10/11 (seated/loaded). State 0/None is an unknown
# dialect or missing data (H2C idle empties report 0) and is never acted on.
_IDENTIFIABLE_STATES = (9, *TRAY_PRESENT_STATES)

# The SEATED member of ``TRAY_PRESENT_STATES``: the roll sits in the tray and its tag
# faces the reader. State 11 is the same roll with its filament threaded on to the hub —
# present, but the shape a commanded read cannot answer without unloading first (which
# is why the client refuses one while filament is engaged). The between-prints
# remain-refresh and the spent-occupied discovery arm therefore both require 10
# specifically; presence in general still means either (rule 5 / invariant 3).
_TRAY_SEATED_STATE = 10

# (printer_id, ams_id, tray_id) -> time.monotonic() at which a READ OCCASION opened for
# the slot. An occasion is the farm's permission to spend ONE commanded read on
# standing (as opposed to event-shaped) evidence — the ``rfid_refresh`` verdicts and the
# spent-occupied discovery arm. Opened by exactly three causes and nothing else:
#
#   1. a new QUALIFIED physical cycle (:func:`_note_gain`) — somebody moved a roll;
#   2. a terminal sweep whose between-prints policy has a reason for the slot
#      (:func:`_terminal_read_occasion`);
#   3. an operator/manual command (``enforce_need=False`` bypasses the need check
#      entirely, so it needs no entry here).
#
# CONSUMED by :func:`command_identify` the moment a read is accepted by the client.
# Without this, a STANDING condition (a DB binding whose tagged spool the tray does not
# show) re-derived the same ``rfid_refresh`` verdict on every reconcile/pipeline pass and
# the farm read the slot every ~31 s forever — the client's identify gate being the only
# thing pacing it (2026-08-07: 1000 reads on one printer in a day, 367/368 on two more,
# each command holding that printer's 30 s identify gate so the slot pipeline deferred
# EVERY decision on the printer). Doctrine rule 6/7: the fix is BY CAUSE — a re-derivation
# of the same evidence is not a new occasion — never a timer on top of the verdict.
_read_occasion_at: dict[tuple[int, int, int], float] = {}

# (printer_id, ams_id, tray_id) -> time.monotonic() at which WE commanded a read,
# recorded for EVERY accepted command regardless of the tray's state at the time.
# Deliberately NOT ``_echo_pending`` (which arms only on a state-10/11 slot, because an
# identify on an empty slot leaves no presence edge to swallow): a read commanded on a
# state-9 slot still runs a firmware identify cycle, and the tray flap that cycle
# produces must be disqualified as a physical cycle just the same. This map is the
# CAUSE record; ``_echo_pending`` remains the one-shot edge-swallow.
_commanded_read_at: dict[tuple[int, int, int], float] = {}

# printer_id -> time.monotonic() at which the unit was last OBSERVED reporting
# ``ams_status_main == AMS_STATUS_IDENTIFYING``. A firmware-AUTONOMOUS read carries no
# command of ours at all, so neither ``_echo_pending`` nor ``_commanded_read_at`` sees
# it; sampling the flag on every AMS push turns it into a cause that outlives the
# instant it was raised (the live check alone misses a read that started and finished
# between two pushes, which is precisely the window a tray flap lives in).
_identifying_seen_at: dict[int, float] = {}

# printer_id -> time.monotonic() of the printer's last gcode_state transition INTO
# RUNNING (stamped by :func:`note_running_edge` from main.py's print-start callback).
# Starting a print engages the AMS: trays disengage and re-seat as filament is pulled
# through, which the presence map sees as a loss/gain pair with nobody having touched a
# roll (2026-08-07 15:12:03, phantom cycles on three printers coinciding with print
# starts). NOT stamped on a PAUSE→RUNNING resume — bambu_mqtt's ``_was_running`` guard
# suppresses on_print_start there — so a refill made during a runout PAUSE keeps its
# fully qualified gain, which ``spool_recovery.maybe_auto_resume_on_refill`` and
# ``clear_on_reinsert`` both depend on.
_running_edge_at: dict[int, float] = {}

# How long after a print-start RUNNING edge an AMS presence edge is still attributable
# to the engage transient. A CAUSE window (like ``_IDENTIFY_ACTIVE_S`` and
# ``_ECHO_PENDING_STALE_S``), not a duration filter on the gain itself: outside it the
# edge is judged exactly as before, and inside it nothing but the print start is
# claimed. Doctrine rule 6 forbids a timer that decides IDENTITY — this decides
# CAUSATION, which is the distinction the whole suppression tier rests on.
_RUNNING_EDGE_TRANSIENT_S = 10.0

# (printer_id, ams_id, tray_id) -> time.monotonic() of the last "this slot's owed
# discovery read is still blocked" WARNING. An owed read defers QUIETLY by design
# (one DEBUG), which is why a slot whose identity was unknown for six hours produced
# no operator-visible signal at all (2026-07-25). Past _OWED_READ_WARN_AFTER_S the
# defer stops being routine pacing and becomes a standing unknown, so it warns —
# once per _OWED_READ_REWARN_S per slot, never per pass.
_owed_read_warned_at: dict[tuple[int, int, int], float] = {}

# How long a slot may sit physically-changed-but-unidentified before the deferral
# is worth a WARNING. Generous: an ordinary engaged-filament defer resolves at the
# next terminal (one print), so anything past 10 minutes means the printer never
# got there — the incident's shape.
_OWED_READ_WARN_AFTER_S = 600.0

# Per-slot re-warn spacing. A continuously-printing farm can hold an owed read for
# hours; one line an hour keeps it visible without becoming the log's own noise.
_OWED_READ_REWARN_S = 3600.0


def _reset_state() -> None:
    """Test hook: clear all module-level edge state between cases."""
    _last_presence.clear()
    _absent_since.clear()
    _absent_under_identify.clear()
    _absent_under_active_feed.clear()
    _reseat.clear()
    _primed.clear()
    _swept_subtasks.clear()
    _echo_pending.clear()
    _gain_at.clear()
    _physical_cycle_at.clear()
    _slot_read_at.clear()
    _discovery_read_at.clear()
    _owed_read_warned_at.clear()
    _read_occasion_at.clear()
    _episode_occasion_epoch.clear()
    _no_tag_answer_closed.clear()
    _commanded_read_at.clear()
    _identifying_seen_at.clear()
    _running_edge_at.clear()


# --- Tray / state predicates ----------------------------------------------


def _norm_state(raw: object) -> int | None:
    """Normalize a tray ``state`` (may arrive as int or str) to int or None."""
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _tray_present(tray: dict) -> bool:
    """Positive-evidence-only presence: seated/loaded (state 10/11) only.

    Keyed off ``bambu_mqtt.TRAY_PRESENT_STATES`` so presence and the client's
    stale-clear guard share one origin for the present-state set.
    """
    return _norm_state(tray.get("state")) in TRAY_PRESENT_STATES


def _printer_running(state) -> bool:
    return state is not None and getattr(state, "state", None) in ("RUNNING", "PAUSE")


def printer_running(state) -> bool:
    """Public read-only view of the RUNNING/PAUSE predicate (:func:`_printer_running`).

    This module already owns the running-state reading every AMS-side guard shares
    (``spool_tagless._printer_busy`` delegates here); the slot pipeline needs the same
    answer for ``ResolutionContext.busy``, and a second copy is exactly the drift this
    fork avoids. ``None`` (disconnected / never connected) is not evidence of a print.
    """
    return _printer_running(state)


def _iter_ams_units(state) -> list:
    """Yield the AMS unit dicts from a printer state's merged raw_data."""
    if state is None:
        return []
    raw = getattr(state, "raw_data", None) or {}
    ams = raw.get("ams", [])
    if isinstance(ams, dict):
        ams = ams.get("ams", [])
    return ams if isinstance(ams, list) else []


def _find_tray(printer_id: int, ams_id: int, tray_id: int) -> dict | None:
    """The live tray dict for a slot from the printer's merged state, or None.

    One scan of the merged AMS payload, shared by every caller that needs the CURRENT
    tray for a slot (echo arming, command-time re-validation) rather than the tray a
    callback was handed — the two differ exactly when the firmware answered in the
    meantime, which is the difference the sweep must respect.
    """
    for ams_unit in _iter_ams_units(printer_manager.get_status(printer_id)):
        if not isinstance(ams_unit, dict):
            continue
        try:
            unit_id = int(ams_unit.get("id", 0))
        except (TypeError, ValueError):
            continue
        if unit_id != ams_id:
            continue
        for tray in ams_unit.get("tray", []) or []:
            if not isinstance(tray, dict):
                continue
            try:
                tid = int(tray.get("id", 0))
            except (TypeError, ValueError):
                continue
            if tid == tray_id:
                return tray
        return None  # matched the unit, no such tray — nothing further to scan
    return None


def _tray_tagged(tray: dict) -> bool:
    """True when the LIVE tray payload carries a valid RFID identity."""
    return is_valid_tag(tray.get("tag_uid", "") or "", tray.get("tray_uuid", "") or "")


# --- Echo-consume flag -----------------------------------------------------


def record_reread(printer_id: int, ams_id: int, tray_id: int) -> None:
    """Arm the one-shot echo-consume flag for a commanded RFID re-read.

    Call this immediately after a re-read command (``ams_get_rfid``) is accepted
    by the client. It arms ``_echo_pending`` for the slot ONLY when the slot is
    present (state 10/11) at command time, so the next presence GAIN on that slot
    — the settle-back of the firmware's ~20 s identify flap — is recognized as our
    own command's echo and swallowed exactly once (see :func:`on_tray_observations`).

    An identify on an EMPTY (state 9/absent) slot produces NO edge at all, so an
    empty slot is deliberately NOT armed: arming it would eat a real insertion
    made right after a print ends — the exact operator flow this design protects.
    All commanders route through :func:`command_identify`, which calls this, so the
    present-at-command-time guard lives in one place.
    """
    tray = _find_tray(printer_id, ams_id, tray_id)
    if tray is not None and _tray_present(tray):
        _echo_pending[(printer_id, ams_id, tray_id)] = time.monotonic()


def live_tray(printer_id: int, ams_id: int, tray_id: int) -> dict | None:
    """Public view of :func:`_find_tray` — the live tray dict for a slot, or None.

    One scan, one place. Exposed for the operator lanes that need the CURRENT tray outside a
    push callback (the "Re-check slot" endpoint's seated/identity preconditions) rather than
    having each of them re-walk the merged payload with its own field parsing.
    """
    return _find_tray(printer_id, ams_id, tray_id)


def identify_in_flight(printer_id: int, ams_id: int, tray_id: int) -> bool:
    """True while a commanded identify may still be running on this slot, or the
    AMS unit is actively identifying any tray. Read-only (never pops the flag)."""
    state = printer_manager.get_status(printer_id)
    if getattr(state, "ams_status_main", 0) == AMS_STATUS_IDENTIFYING:
        return True
    ts = _echo_pending.get((printer_id, ams_id, tray_id))
    return ts is not None and time.monotonic() - ts < _IDENTIFY_ACTIVE_S


def _identify_explains_absence(printer_id: int, ams_id: int, tray_id: int) -> bool:
    """True when an identify — not a human — explains this slot's absence RIGHT NOW.

    An RFID identify UNLOADS the tray for ~10–20 s, so the slot reads ABSENT and then
    PRESENT again: a gain whose preceding absence clears the ≥ ``_MIN_PHYSICAL_ABSENT_S``
    flap filter yet moved no roll. Recorded at the PRESENT→ABSENT edge (where the signal
    is freshest) and re-checked at the GAIN edge, so such a gain is never banked as a
    QUALIFIED physical cycle — the discovery / W1-mint / FIFO-restamp evidence a HUMAN
    roll swap leaves. This narrows what counts as a human roll movement; it never widens
    it, and it never overrides the ≥5 s duration filter — the two gate the gain together.

    Delegates to :func:`identify_in_flight`, whose two arms are precisely the "an identify
    is happening NOW" signals: SLOT-scoped ``_echo_pending`` fresh (a read WE commanded on
    a then-present slot — the common case, and the one that keeps a genuine swap on a
    DIFFERENT slot from being suppressed) and, as a secondary, unit-scoped
    ``ams_status_main == AMS_STATUS_IDENTIFYING`` — the ONLY signal a state-9
    seated-yet-unread commanded read leaves (``record_reread`` deliberately never arms the
    echo on a state-9 slot, so the incident's flap slips the echo lane) and the ONLY signal
    a firmware-AUTONOMOUS re-read, which carries no command at all, leaves at all.

    Both arms are LIVE / self-clearing on purpose. A lingering "we last read this slot N s
    ago" timestamp (``_slot_read_at`` / a commanded-read stamp) is deliberately NOT used:
    it cannot tell an identify still unloading the tray from a genuine human pull made
    AFTER that identify already settled — the identify's own flap having been consumed by
    then — and would over-suppress the reseat that follows. ``ams_status_main`` returns to
    idle the moment the identify ends, which is exactly that distinction. Never raises."""
    return identify_in_flight(printer_id, ams_id, tray_id)


def note_running_edge(printer_id: int) -> None:
    """Stamp that ``printer_id``'s gcode_state has just transitioned INTO RUNNING.

    Called from ``main.on_print_start`` — the one place the fork already observes that
    transition (``bambu_mqtt`` fires the print-start callback on the RUNNING edge, and
    its ``_was_running`` guard deliberately suppresses it for a PAUSE→RUNNING resume).
    Starting a print engages the AMS and momentarily disengages trays, producing
    presence loss/gain pairs that no human caused; :func:`_identify_explains_gain` uses
    this stamp to disqualify those from the ACTION tiers. Presence itself still updates —
    presence is truth (invariant 3); only the tiers that assume a human moved a roll are
    withheld. Sync, in-memory, never raises: it is one dict write on a callback path.
    """
    _running_edge_at[printer_id] = time.monotonic()


def _identify_explains_gain(
    printer_id: int,
    ams_id: int,
    tray_id: int,
    *,
    absent_at: float | None,
    commanded_at: float | None,
    paused: bool,
) -> bool:
    """True when a NON-PHYSICAL cause — not a human — explains this presence GAIN.

    The suppression is BY CAUSE and never by duration (doctrine rule 6 / invariant 6):
    an edge with a machine explanation is disqualified however long its absence ran, and
    an edge with none is judged on the ordinary ≥5 s flap filter alone. Four causes, each
    closing a leak the 2026-07-21 wave left open:

    a. an identify is explaining the absence RIGHT NOW — the slot-scoped echo flag or the
       unit's live ``AMS_STATUS_IDENTIFYING`` (:func:`_identify_explains_absence`, kept
       verbatim: it is the freshest signal, and it is also what the loss edge captured);
    b. WE commanded a read on the slot, whatever the tray's state was at command time.
       ``record_reread`` arms the echo flag only for a state-10/11 slot (an identify on an
       empty slot leaves no edge to swallow), so a read commanded on a state-9 tray slipped
       the whole suppression lane and its flap banked as a physical cycle. ``commanded_at``
       is passed in ALREADY CONSUMED by the caller: a command explains at most ONE flap.
       A lingering "we read this slot N s ago" stamp cannot tell an identify still
       unloading the tray from a genuine human pull made after that identify settled —
       :func:`_identify_explains_absence`'s docstring is explicit about that, and
       one-shot consumption is what honours it;
    c. the unit was OBSERVED identifying DURING the absence (``_identifying_seen_at``
       stamped strictly after the absence began). A firmware-AUTONOMOUS read carries no
       command at all and its flag can rise and fall between two edges, so neither (a)
       nor (b) sees it. Window-scoped rather than time-boxed on purpose: an observation
       from BEFORE this absence explains nothing about it;
    d. the printer started a print inside :data:`_RUNNING_EDGE_TRANSIENT_S` before the
       edge (``_running_edge_at``) — the AMS engage transient.

    ``paused`` vetoes (d) outright: a gain while the printer sits in PAUSE is the runout
    refill / jam reinsert that ``spool_recovery.maybe_auto_resume_on_refill`` and
    ``clear_on_reinsert`` are waiting for, and it stays FULLY qualified. Never raises.
    """
    if _identify_explains_absence(printer_id, ams_id, tray_id):
        return True
    now = time.monotonic()
    if commanded_at is not None and (
        now - commanded_at < _IDENTIFY_ACTIVE_S or (absent_at is not None and commanded_at >= absent_at)
    ):
        return True
    identifying_at = _identifying_seen_at.get(printer_id)
    if identifying_at is not None and absent_at is not None and identifying_at > absent_at:
        return True
    if not paused:
        edge = _running_edge_at.get(printer_id)
        if edge is not None and now - edge <= _RUNNING_EDGE_TRANSIENT_S:
            return True
    return False


# --- read-occasion ledger --------------------------------------------------


def open_read_occasion(printer_id: int, ams_id: int, tray_id: int) -> None:
    """Open a read occasion for a slot — the farm may spend ONE read on standing evidence.

    See :data:`_read_occasion_at` for what may call this and why nothing else may. Idempotent
    within an occasion: re-opening an already-open occasion still buys exactly one read,
    because :func:`command_identify` consumes it.
    """
    _read_occasion_at[(printer_id, ams_id, tray_id)] = time.monotonic()


def _read_occasion_open(printer_id: int, ams_id: int, tray_id: int) -> bool:
    """Is a read occasion open for this slot? Non-consuming."""
    return (printer_id, ams_id, tray_id) in _read_occasion_at


def _consume_read_occasion(printer_id: int, ams_id: int, tray_id: int) -> None:
    """Spend the slot's read occasion. Called only from :func:`command_identify`."""
    _read_occasion_at.pop((printer_id, ams_id, tray_id), None)


# (printer_id, ams_id, tray_id, cause) -> the EPOCH token whose occasion has already been
# bought. Some standing constellations are themselves the cause of a read — the table sees
# them on every push and nothing else will ever open an occasion for them — so they buy
# their own, once per EPISODE. The epoch token is whatever identifies "the same situation"
# for that cause (see the two openers below); when it changes, a new episode has begun and
# a fresh occasion is due. Keyed on the token rather than a timestamp because the read may
# already have been CONSUMED — an episode whose read is spent must not re-open, which a
# "is the occasion still open?" test cannot tell from "was one never opened".
_episode_occasion_epoch: dict[tuple[int, int, int, str], object] = {}


def _open_episode_occasion(printer_id: int, ams_id: int, tray_id: int, *, cause: str, epoch: object) -> None:
    """Buy the ONE read an EPISODE of ``cause`` is entitled to. Idempotent within it.

    The 2026-08-07 liveness hole (spool 226, 001-H2S slot 1) generalized: read occasions
    open only on a qualified physical cycle, the terminal sweep's between-prints policy,
    or a manual command. A verdict the decision table re-derives from STANDING evidence
    therefore has no entitlement at all when those edges were missed — a request nothing
    can ever grant, and the slot parks. The cure is that the emitting constellation is
    itself the cause (doctrine rule 6/7 — pacing BY CAUSE, never a timer over the
    verdict), and the epoch token is what keeps it to one read per episode rather than
    one read per push. Sync, in-memory, never raises.
    """
    key = (printer_id, ams_id, tray_id, cause)
    if key in _episode_occasion_epoch and _episode_occasion_epoch[key] == epoch:
        return
    _episode_occasion_epoch[key] = epoch
    open_read_occasion(printer_id, ams_id, tray_id)


def open_spent_occupied_occasion(printer_id: int, ams_id: int, tray_id: int, spool_id: int) -> None:
    """The spent-occupied constellation's one read: a spent binding under a seated tray
    the wire cannot identify.

    Episode = the BOUND SPOOL. Resolution (a swap, a displacement) changes it, so a new
    spent constellation re-opens naturally while a re-derived verdict for the same one
    buys nothing.

    An EMPTY slot can never reach this — the emitting verdict requires ``present is True``
    and ``identify_needed``'s spent-occupied arm additionally requires a seated state — so
    the 008-H2C empty-slot read-loop class stays closed.
    """
    _open_episode_occasion(printer_id, ams_id, tray_id, cause="spent_occupied", epoch=spool_id)


def open_ambiguity_occasion(printer_id: int, ams_id: int, tray_id: int, spool_id: int | None, tag: str | None) -> None:
    """The identity-ambiguity constellation's one read: a tag disagreeing with a bound row
    that also claims an identity (``slot_state`` 2.2).

    Episode = the (bound row, disagreeing tag) PAIR. The push asserts that tag at ~1 Hz for
    as long as the roll sits there, so without an epoch the "buy the answer" defer would
    re-buy it every second; with one, a genuinely new disagreement — a different roll, or
    the same roll over a different binding — still earns its own read.

    The read this buys is answerable by construction (the tray is tagged and present), and
    the one thing that can stop it is engaged filament: ``command_identify`` defers on that
    QUIETLY and consumes nothing, so the occasion survives to be spent on the idle edge.
    """
    _open_episode_occasion(printer_id, ams_id, tray_id, cause="identity_ambiguous", epoch=(spool_id, tag))


# --- Re-seat evidence (the de-bounce lane's two inputs) --------------------


def _slot_was_active_feeder(printer_id: int, ams_id: int, tray_id: int, state, running: bool) -> bool:
    """Was this slot FEEDING a live print at the moment it lost presence?

    Evaluated at the PRESENT→ABSENT edge and stamped into
    :data:`_absent_under_active_feed`, because this question has an answer only while
    the evidence is fresh: seconds later the firmware has moved on, and minutes later
    (the ~3-minute bay-clear→HMS gap) the exhaustion evidence that would have explained
    the departure has not even arrived yet.

    A slot that goes empty while it is feeding is running out, or is being pulled
    mid-print. Neither is a spurious release, so its return is a REFILL and must mint a
    fresh row rather than de-bounce onto the row that just drained (scenarios T7/T8).

    **The resolution order is the REVERSE of the jam case's, and getting it backwards
    would make this condition silently never fire.** ``spool_recovery`` owns the one
    wire-first feeder resolution and is asked here through its public
    :func:`spool_recovery.slot_was_feeding`, which orders ``last_loaded_tray`` and the
    job's mapping AHEAD of ``tray_now``: the question here is which slot was feeding
    IMMEDIATELY BEFORE this edge, and at that instant ``tray_now`` may already have
    moved (a firmware auto-refill switches to a backup slot) or read the 255 sentinel,
    which means "nothing is feeding" and never "the path is clear" (invariant 8).

    Idle printer ⇒ False: ``last_loaded_tray`` and the mapping are per-JOB state, and
    reading them between prints would attribute a stale feeder to an operator's
    ordinary roll change. Never raises — an unresolvable answer is "not suspect", and
    the pipeline's live-HMS half of the runout-suspect test covers the same ground once
    the firmware has spoken.
    """
    if not running:
        return False
    try:
        from backend.app.services.spool_recovery import slot_was_feeding

        return slot_was_feeding(state, ams_id, tray_id)
    except Exception:  # noqa: BLE001 — an unresolvable feeder is not evidence of a runout
        logger.exception(
            "AMS presence: active-feeder resolution failed for printer %d AMS%d-T%d", printer_id, ams_id, tray_id
        )
        return False


def reseat_within_window(printer_id: int, ams_id: int, tray_id: int) -> bool:
    """Did this slot's last presence GAIN return a roll inside the de-bounce window?

    True only when the absence was MEASURED and shorter than :data:`_RESEAT_WINDOW_S`.
    An absence whose start was never observed answers False, deliberately: ``None`` is
    UNKNOWN, not "short", and the farm asserts nothing about a roll it did not watch
    leave (scenario T11 — a restart while the roll is out mints on re-seat).

    PEEK, never consuming (like :func:`last_physical_cycle_age`): the pipeline may defer
    a slot for several pushes, and the fact is a property of the wire's last edge pair.
    """
    entry = _reseat.get((printer_id, ams_id, tray_id))
    return entry is not None and entry.absent_for is not None and entry.absent_for < _RESEAT_WINDOW_S


def reseat_under_active_feed(printer_id: int, ams_id: int, tray_id: int) -> bool:
    """Was this slot the active feeder of a live print when its current occupancy began?

    The loss-edge half of the de-bounce lane's runout-suspect test
    (:func:`_slot_was_active_feeder`), read back at the gain the release preceded. PEEK.
    """
    entry = _reseat.get((printer_id, ams_id, tray_id))
    return entry is not None and entry.under_active_feed


def reseat_absence(printer_id: int, ams_id: int, tray_id: int) -> float | None:
    """The MEASURED absence (seconds) before this slot's last gain, or None if unknown.

    For the log lines only — every decision reads the two predicates above, so a caller
    can never re-derive its own window from this number.
    """
    entry = _reseat.get((printer_id, ams_id, tray_id))
    return entry.absent_for if entry is not None else None


# --- Change-evidence ledger -----------------------------------------------


def _note_gain(printer_id: int, ams_id: int, tray_id: int, *, qualified: bool) -> None:
    """Record a genuine presence GAIN on a slot (never an echo or a first-push seed).

    ``qualified`` marks a gain that is NOT a sub-``_MIN_PHYSICAL_ABSENT_S`` flap, i.e.
    a real physical roll movement rather than the runout-instant state flap a firmware
    backup switch produces. Only a qualified gain becomes discovery evidence AND opens a
    read occasion (the standing-evidence arms' permission to spend one read); an
    unqualified one still updates the gain stamp (:func:`recent_gain_age`).
    """
    now = time.monotonic()
    key = (printer_id, ams_id, tray_id)
    _gain_at[key] = now
    if qualified:
        _physical_cycle_at[key] = now
        # Somebody moved a roll here: whatever the DB believes about this slot is now a
        # hypothesis, so the standing-evidence arms get their one read back.
        _read_occasion_at[key] = now


def note_identity_learned(printer_id: int, ams_id: int, tray_id: int) -> None:
    """Stamp that the slot's identity is current as of now.

    Called when a read is COMMANDED (:func:`command_identify`) and when the firmware
    PUBLISHES a valid tag for the slot. Either way the question "what is in this
    slot?" has been put; an older physical cycle is no longer unanswered evidence,
    which is what keeps discovery to one read per change instead of one per print end.
    """
    _slot_read_at[(printer_id, ams_id, tray_id)] = time.monotonic()


def _unanswered_cycle(printer_id: int, ams_id: int, tray_id: int) -> bool:
    """True when a qualified physical cycle was recorded SINCE the slot's identity was
    last learned — the discovery lane's whole evidence test."""
    key = (printer_id, ams_id, tray_id)
    cycle_at = _physical_cycle_at.get(key)
    if cycle_at is None:
        return False
    read_at = _slot_read_at.get(key)
    return read_at is None or cycle_at > read_at


def identity_unanswered(printer_id: int, ams_id: int, tray_id: int) -> bool:
    """This slot physically changed and the farm has not yet resolved what is in it.

    Public read-only view of the discovery lane's evidence (:func:`_unanswered_cycle`)
    for consumers OUTSIDE the identify lane — notably ``spool_tagless``'s config-settle
    gate, which must never publish an identity into a slot whose own identity is still
    an open question (a config write landing inside the firmware's insert-read window
    destroys the RFID-detected state and the firmware never retries). Non-consuming:
    asking never spends the evidence.
    """
    return _unanswered_cycle(printer_id, ams_id, tray_id)


def read_answered_since_seating(printer_id: int, ams_id: int, tray_id: int) -> bool:
    """Has this slot's identity question already been PUT since its current seating?

    True when a read was commanded (or the firmware published a tag) for the slot AND no
    qualified physical cycle has happened since — i.e. the answer on file, whatever it
    was, still describes the roll that is in the tray now. A slot nobody has ever read
    answers False, so a first ask is never blocked.

    Non-consuming public accessor over the same ``_slot_read_at`` / ``_physical_cycle_at``
    pair the discovery lane uses (:func:`_unanswered_cycle`), for the ONE consumer that
    needs the complementary question: ``filament_deficit.request_unread_reads``, whose
    per-episode ask must not re-put a question the identify lane already put. It cannot
    use ``not identity_unanswered(...)`` — that reads True for a slot with no cycle on
    record at all, which is precisely the never-asked slot the ask exists for.
    """
    key = (printer_id, ams_id, tray_id)
    return _slot_read_at.get(key) is not None and not _unanswered_cycle(printer_id, ams_id, tray_id)


def last_physical_cycle_age(printer_id: int, ams_id: int, tray_id: int) -> float | None:
    """Seconds since the slot's last QUALIFIED physical cycle, or None if never seen.

    Non-consuming read accessor: unlike the discovery evidence test it is unaffected
    by reads, so a consumer can ask "was this slot physically touched recently?"
    independently of what the identify lane did. Lost on restart like every edge map
    here (a missing stamp reads as "no recent cycle", the conservative answer).
    """
    ts = _physical_cycle_at.get((printer_id, ams_id, tray_id))
    return None if ts is None else time.monotonic() - ts


def recent_gain_age(printer_id: int, ams_id: int, tray_id: int) -> float | None:
    """Seconds since the slot's last genuine presence GAIN, or None if never seen.

    Non-consuming, and deliberately wider than :func:`last_physical_cycle_age`: it
    includes gains too short to qualify as a physical cycle, because "the tray just
    became present" is the signal for "the firmware's own insert-read is probably
    still in flight — hold off".
    """
    ts = _gain_at.get((printer_id, ams_id, tray_id))
    return None if ts is None else time.monotonic() - ts


def unit_drying(printer_id: int, ams_id: int) -> bool:
    """True while AMS unit ``ams_id`` on ``printer_id`` is running a drying cycle.

    Delegates to the client's :meth:`ams_unit_drying` (per-unit ``dry_time`` plus a
    monotonic latch). Drying disengages trays — the presence bit flaps to state 10
    with no physical event — and any concurrent identify / config write fails the
    cycle (HMS 0700_C069). Presence and tagless flows gate on this. Never raises: an
    unreachable client reads as not-drying."""
    try:
        client = printer_manager.get_client(printer_id)
        return bool(client and client.ams_unit_drying(ams_id))
    except Exception:  # noqa: BLE001 — must never break the AMS callback chain
        return False


def _filament_engaged(printer_id: int) -> bool:
    """True while filament is loaded in the extruder path — a MIRROR of the client's own
    ``BambuMQTTClient.ams_refresh_tray`` guard ``self.state.tray_now != 255``.

    A commanded ``ams_get_rfid`` has to move filament, so the client REFUSES one (with a
    WARNING) whenever any tray is engaged — regardless of which slot the read targets.
    Its refusal message even names the ENGAGED slot (decoded from ``tray_now``), not the
    slot asked for, so two eligible tagged slots swept while one is engaged produce two
    IDENTICAL warnings in the same instant (the live 07-20 double log). Pre-checking the
    same predicate here lets the need-driven sweep / idle-gain re-read defer QUIETLY
    instead of provoking that (doubled) WARNING after the fact.

    Reads the live ``PrinterState.tray_now`` via ``printer_manager.get_status`` — the
    exact field the client guards on (``get_status`` returns ``client.state``), so this
    stays single-origin with the guard. A missing/None value reads as unloaded so a
    partial state never false-blocks a read; the client's own guard remains the backstop.
    Never raises — an unreadable state is treated as not-engaged."""
    try:
        tray_now = getattr(printer_manager.get_status(printer_id), "tray_now", _TRAY_UNLOADED)
    except Exception:  # noqa: BLE001 — must never break the identify path
        return False
    return tray_now is not None and tray_now != _TRAY_UNLOADED


def read_unavailable_reason(printer_id: int, ams_id: int, tray_id: int) -> str | None:
    """Why a commanded identify for this slot CANNOT run this epoch — or None if it can.

    Public read-only view of the identify lane's wire-safety refusals for consumers
    OUTSIDE it — currently ``spool_tagless``'s config-settle gate, which waits on an
    identity ANSWER it will never get while the tray is engaged (2026-08-12, 009-H2S:
    an untagged insert the AMS immediately engaged sat the full ``_CONFIG_SETTLE_MAX_S``
    before minting, because no read could run and no firmware read was coming — the
    autonomous ones happen at insert/load, so an identity still in flight would already
    be asserting itself on the wire).

    Reports ONLY causes that cannot self-clear while the printer sits idle: engaged
    filament (:func:`_filament_engaged`) needs an unload somebody has to command.
    Deliberately EXCLUDED are drying (:func:`unit_drying`) and an identify already in
    flight (:func:`identify_in_flight`) — both clear on their own well inside a settle
    window, and drying additionally refuses the tagless config write itself at the
    client, so concluding a settle on either would only spend
    ``spool_tagless._AUTOCONFIG_MAX_PUBLISHES`` strikes against a wire that is going to
    answer ``fail`` (the shape-28 lesson: an unconsumed refusal is what turns a bounded
    retry into ~40k writes a day).

    Takes the full slot signature although today's only cause is printer-wide: the
    QUESTION is per-slot ("can THIS slot be read now?"), and a future per-unit cause
    slots in behind the same signature without an API break.

    Non-consuming, and that is load-bearing: a refusal is not an ANSWER, and invariant 13
    closes read entitlements only on answers. Asking here must therefore move no state at
    all — no occasion consumption, no identity-learned stamp, no echo arm — because the
    entitlement it preserves is exactly what lets a later DISENGAGED read discover the
    roll and correct a slot the tagless default mis-identified. Never raises (invariant
    10): an unreadable printer state reads as not-engaged, i.e. a read is available.
    """
    return "filament_engaged" if _filament_engaged(printer_id) else None


# --- Assignment context ----------------------------------------------------


async def _spoolman_active(db: AsyncSession) -> bool:
    from backend.app.api.routes.settings import get_setting

    val = await get_setting(db, "spoolman_enabled")
    return bool(val) and val.lower() == "true"


async def _slot_assignment_context(
    db: AsyncSession, printer_id: int, ams_id: int, tray_id: int, spoolman_active: bool
) -> tuple[bool, bool, bool]:
    """Resolve ``(has_assignment, bound_spool_tagged, bound_spool_spent)`` for a slot.

    The internal ``SpoolAssignment`` is the source of truth; when Spoolman mode is
    active a ``SpoolmanSlotAssignment`` also counts as an assignment. Tag identity is
    decided by the ONE predicate the whole fork uses,
    ``spool_tag_matcher.is_valid_tag``, applied to the bound spool's stored
    ``tag_uid``/``tray_uuid`` — a Spoolman binding is therefore never "tagged" (the
    Spoolman mirror stores no RFID identity), so such a slot can only qualify for a
    re-read through its LIVE tag or a discovery cycle.

    ``bound_spool_tagged`` is what makes a slot worth re-reading when a read occasion is
    open: the read succeeds and refreshes ``remain`` for gram tracking and reused-core
    detection. ``bound_spool_spent`` is ``spent_at`` — hardware exhaustion truth (doctrine
    rule 8), never ``label − weight_used`` — and drives the spent-occupied discovery arm.
    """
    from backend.app.models.spool import Spool  # noqa: F401 — selectinload target
    from backend.app.models.spool_assignment import SpoolAssignment

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
    if sa is not None:
        tagged = sa.spool is not None and is_valid_tag(
            getattr(sa.spool, "tag_uid", "") or "", getattr(sa.spool, "tray_uuid", "") or ""
        )
        spent = sa.spool is not None and getattr(sa.spool, "spent_at", None) is not None
        return True, tagged, spent

    if spoolman_active:
        from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment

        res2 = await db.execute(
            select(SpoolmanSlotAssignment.id).where(
                SpoolmanSlotAssignment.printer_id == printer_id,
                SpoolmanSlotAssignment.ams_id == ams_id,
                SpoolmanSlotAssignment.tray_id == tray_id,
            )
        )
        if res2.first() is not None:
            return True, False, False

    return False, False, False


# --- Identify need + the single identify commander -------------------------


async def identify_needed(
    db: AsyncSession,
    printer_id: int,
    ams_id: int,
    tray_id: int,
    tray: dict,
    spoolman_active: bool,
) -> str | None:
    """Why this slot needs an RFID identify right now — or None if it does not.

    The single eligibility authority for every commanded ``ams_get_rfid``. Returns:

    * ``"rfid_refresh"`` — the slot is live-tagged, or DB-bound to a spool carrying a
      tag identity, AND a read occasion is open. A tag can only be re-read from a SEATED
      spool, so this reason additionally requires presence: commanding a read on a slot
      whose bound tagged spool has been pulled would fail exactly like a tagless read and
      raise a never-clearing ``0700_2X00_0001_0081``. A DB binding that outlives its roll
      is a RELEASE problem (doctrine rule 9), never a read reason.
    * ``"discovery"`` — any of: a qualified physical cycle is unanswered since the slot's
      identity was last learned and the tray is still unidentified; the binding is
      spent-latched under an unidentified replacement roll; or the slot is UNBOUND and
      the seated roll asserts no identity at all (see below). One read settles it; a
      failure is the answer "no tag ⇒ tagless" and is suppressed farm-side.
      The cycle arm is checked BEFORE the DB rules on an untagged tray on purpose: once
      something physically changed in the slot, the DB's idea of what is in it is a
      hypothesis, and the read must be treated as one that may legitimately fail.
    * ``None`` — everything else. An untouched tagless slot lands here, which is the
      entire fix for the standing "failed to read the filament information" errors, and
      so does every re-derivation of standing evidence whose occasion is already spent.

    OCCASION PACING (2026-08-07). Every arm that rests on STANDING evidence — the two
    ``rfid_refresh`` arms, the spent-occupied arm and the unbound-unread arm — is gated on
    an open read occasion (:data:`_read_occasion_at`), which a commanded read consumes.
    The unanswered-cycle
    arm needs no separate gate: it already carries its own one-read-per-change pacing
    through ``_slot_read_at``. Without this, a standing condition re-derived on every
    reconcile / pipeline pass re-owed the same read forever, and the resulting per-printer
    identify-gate saturation deferred every slot decision on the printer. Doctrine rule
    6/7: paced BY CAUSE (a fresh occasion), never by a timer laid over the verdict.

    Pure predicate: it never commands anything and never mutates the ledgers.
    """
    state = _norm_state(tray.get("state"))
    if state not in _IDENTIFIABLE_STATES:
        return None  # unknown dialect / no data — never acted on (H2C idle empties report 0)

    present = _tray_present(tray)
    live_tagged = present and _tray_tagged(tray)
    occasion = _read_occasion_open(printer_id, ams_id, tray_id)

    if live_tagged and occasion:
        return "rfid_refresh"

    if _unanswered_cycle(printer_id, ams_id, tray_id):
        return "discovery"

    if present and occasion:
        has_assignment, bound_tagged, bound_spent = await _slot_assignment_context(
            db, printer_id, ams_id, tray_id, spoolman_active
        )
        # Spent-occupied FIRST: a spent-latched binding under a seated roll the wire
        # cannot identify means the roll in the slot is a NEWCOMER the farm has never
        # named. The read may legitimately find no tag, so it must be classified
        # ``discovery`` — that is what suppresses the expected read failure farm-side
        # (rule 5 / invariant 4). Classifying it ``rfid_refresh`` because the SPENT row
        # happens to carry a tag would both mis-name the read and let its expected
        # failure escape as a fault. BOTH present states: this arm's job is to grant the
        # read a decision table asked for on ``present is True``, and refusing it for
        # state 11 made that verdict ungrantable — the table emitted a reason on evidence
        # the need authority would not accept, so the request stood forever and resolved
        # nothing. State 11 IS the shape a read cannot answer while the filament is
        # threaded on to the hub, but that is a WIRE-SAFETY fact and it is enforced where
        # every other wire-safety fact is: ``command_identify``'s engaged-filament
        # pre-check defers it QUIETLY and spends nothing, so the entitlement survives to
        # the idle edge instead of never existing. One attempt per occupancy epoch —
        # every failed read leaves a printer-side 0700_0081 that only a power-cycle
        # clears (rule 1: minimal human interaction cuts both ways — do not manufacture
        # work for the operator).
        if bound_spent and not live_tagged and state in TRAY_PRESENT_STATES:
            return "discovery"
        if bound_tagged:
            return "rfid_refresh"
        # UNBOUND + UNREAD: a roll sits in a slot the farm has no binding for and the
        # wire asserts no identity at all — no filament type, no preset id, no tag. The
        # DB knows nothing, the wire says nothing, so ONE read is the only way the slot
        # can ever be priced; until then the dispatch layers treat it as unknown
        # material (``filament_deficit.live_unread_slots``) and hold work behind it.
        # Classified ``discovery`` because the answer may legitimately be "no tag",
        # which the farm-side suppression then recognizes as OURS (rule 5 / invariant 4:
        # the deficit block IS the reason to read, so this is not a read of an untouched
        # tagless slot).
        #
        # STORM SAFETY, in three independent brakes:
        #   1. it fires only with an OPEN occasion, and ``command_identify`` consumes the
        #      occasion — so a re-derivation of the same standing evidence buys nothing;
        #   2. the only NEW opener is the deficit lane's ask, which is once per unread
        #      EPISODE per slot and additionally checks
        #      :func:`read_answered_since_seating` before spending one;
        #   3. either present state (mirroring the spent-occupied arm): a state-11 slot is
        #      threaded on to the hub and its read waits for the idle edge in
        #      ``command_identify``'s engaged-filament defer, which spends nothing —
        #      wire safety belongs there, not in the need verdict.
        # A genuinely tagless roll therefore costs exactly ONE read per seating, whose
        # expected failure is suppressed farm-side and never reaches the operator, and
        # whose NO-TAG answer closes the entitlement outright (:func:`close_answered_read`).
        #
        # ``tray_identity_asserted`` is the whole identity test (type / preset id / tag),
        # so it subsumes ``live_tagged`` here — a live-tagged tray already took the
        # ``rfid_refresh`` arm above and could not reach this line unbound anyway.
        if not has_assignment and state in TRAY_PRESENT_STATES and not tray_identity_asserted(tray):
            return "discovery"

    return None


async def command_identify(
    printer_id: int,
    ams_id: int,
    tray_id: int,
    *,
    source: str,
    reason: str | None = None,
    enforce_need: bool = True,
    db: AsyncSession | None = None,
) -> tuple[bool, str]:
    """Command ONE RFID identify on a slot. The only ``ams_get_rfid`` commander.

    Owns everything that must happen around a commanded read, so no caller can get a
    subset of it right: the need check, the echo-consume arming, the identity-learned
    stamp and the discovery stamp that later suppresses the read's expected failure.

    Args:
        source: caller tag for the log line (``terminal_sweep`` / ``idle_gain`` /
            ``manual_refresh``).
        reason: an already-resolved :func:`identify_needed` verdict. Callers that
            evaluated the need with the tray in hand pass it, which is both cheaper
            and more accurate than re-deriving it from live state here.
        enforce_need: when True (the default) a read is only issued for a non-None
            reason — resolved from ``db`` + live state when the caller passed none,
            and fail-closed (no read) when it cannot be resolved at all. Operator
            actions pass False: explicit intent bypasses NEED, never wire safety —
            drying / identifying / identify-gate refusals are the client's and stay.

    Pacing is deliberately NOT this function's job: the terminal sweep awaits
    ``wait_ams_settle`` itself because the wait must precede its in-flight and
    firmware-answered guards (a gain landing during the wait changes both answers),
    while the idle-gain and operator paths run inside a callback lock / an HTTP
    request and must never block on it at all.

    Returns ``(ok, message)`` straight from the client, or ``(False, reason)`` when no
    read was issued.
    """
    key = (printer_id, ams_id, tray_id)

    if enforce_need and reason is None:
        if db is None:
            return False, "identify need not evaluated"
        tray = _find_tray(printer_id, ams_id, tray_id)
        if tray is None:
            return False, "slot not in live state"
        reason = await identify_needed(db, printer_id, ams_id, tray_id, tray, await _spoolman_active(db))
        if reason is None:
            return False, "no identify needed"

    client = printer_manager.get_client(printer_id)
    if client is None:
        return False, "Printer not connected"

    # Engaged-filament pre-check — NEED-driven paths only (terminal sweep, idle gain).
    # The client refuses an ams_get_rfid while any filament is loaded (tray_now != 255)
    # and logs a WARNING that names the engaged slot — twice when two tagged slots are
    # eligible (see :func:`_filament_engaged`). Defer QUIETLY here and stamp NOTHING (no
    # identity-learned, no echo arm, no discovery stamp): the slot's eligibility is left
    # untouched so the NEXT terminal retries it once filament is no longer engaged —
    # ``rfid_refresh`` re-derives from the live tag, ``discovery`` keeps its unanswered
    # cycle. Operator bypass (``enforce_need=False``) is deliberately NOT pre-checked:
    # explicit intent still reaches the client and gets its verbatim "Please unload
    # filament first" refusal, never a silent skip — engaged-filament is a wire-safety
    # refusal like drying/identifying, which the doctrine keeps with the client there.
    if enforce_need and _filament_engaged(printer_id):
        logger.debug(
            "[Printer %s] identify deferred: AMS%d slot%d (source=%s, reason=%s) — "
            "filament engaged; eligibility preserved for the next terminal",
            printer_id,
            ams_id,
            tray_id,
            source,
            reason or "operator",
        )
        return False, "filament engaged"

    ok, msg = client.ams_refresh_tray(ams_id, tray_id)
    if ok:
        # Arm the echo flag ONLY on success: the identify cycle this command starts
        # flaps the tray present→9→present, and that settle-back gain must be
        # swallowed, not answered with another read. A refused command starts no
        # identify → no echo → nothing to arm, and no identity was learned either.
        record_reread(printer_id, ams_id, tray_id)
        note_identity_learned(printer_id, ams_id, tray_id)
        # The read is spent: the slot's occasion closes until a NEW cause opens one (a
        # qualified physical cycle or the next terminal's between-prints policy). This is
        # what makes a standing condition cost ONE read instead of one per pass.
        _consume_read_occasion(printer_id, ams_id, tray_id)
        # Cause record for the presence-edge suppression tier — stamped for EVERY
        # accepted command, including one on a state-9 tray, which ``record_reread``
        # deliberately does not arm (an empty slot leaves no edge to swallow, but the
        # identify cycle it starts still flaps the tray).
        _commanded_read_at[key] = time.monotonic()
        if reason == "discovery":
            # The slot may legitimately have no tag: mark the read so the firmware's
            # "failed to read the filament information" answer is recognized as ours.
            _discovery_read_at[key] = time.monotonic()
        logger.info(
            "[Printer %s] identify commanded: AMS%d slot%d (source=%s, reason=%s)",
            printer_id,
            ams_id,
            tray_id,
            source,
            reason or "operator",
        )
    else:
        logger.debug(
            "[Printer %s] identify refused: AMS%d slot%d (source=%s) — %s",
            printer_id,
            ams_id,
            tray_id,
            source,
            msg,
        )
    return ok, msg


async def broadcast_standing_unknown(
    db: AsyncSession, printer_id: int, ams_id: int, tray_id: int, *, case: str
) -> None:
    """Push a slot the farm cannot resolve to the operator UI (same bus as ``tagless_fresh_prompt``).

    A log WARNING is invisible to the person who can actually fix the slot, which is why
    the 2026-07-25 six-hour unknown produced no operator signal at all. Rides the shared
    ``ws_manager.broadcast`` helper — one bus, one payload shape. Never raises: an
    unreachable websocket layer must not abort the caller, which is a scheduler-tick lane
    walking the whole fleet.

    TWO emitters, both in ``spool_tagless``'s reconcile walk and both once per episode:

    * ``_age_bound_presence_stale``'s escalation rung (``case="bound_presence_unknown"``)
      — a binding whose slot presence has resolved neither way after the whole ask ladder,
      i.e. the farm no longer knows whether the roll it thinks it has is there;
    * ``_age_spent_swap_park`` (``case="spent_swap_park"``, 2026-08-19) — a SPENT binding
      under a tray that IS answering (present and configured) with no qualified cycle and
      no answered read to release the W1 latch, so ``slot_state`` row 4a keeps returning
      ``spent_latch`` and the slot parks silently forever.

    They are siblings, not one lane with a flag: their predicates are opposite on
    spent-ness, on presence and on configuration, and only the SURFACE is shared so the
    console keeps one vocabulary for "this slot is standing unresolved". ``case`` is
    REQUIRED and names WHAT is unresolved so the frontend can word the toast for the
    situation rather than for the event; the owed-read lane that used to pass a third case
    is log-only now (:func:`_warn_owed_read_blocked`).

    NO dedup here, deliberately. The hourly per-slot gate this function used to carry
    existed to fold two lanes' toasts into ONE signal for one physical slot by TIME; both
    surviving lanes pace themselves by EPISODE instead, which is stricter and honest about
    who owns the pacing — the presence ladder needs a full ladder (maturity at 900 s, then
    +600 s and +3600 s) to reach its rung, and the park alerts exactly once for as long as
    the same spent row holds the same seated slot. A gate that can never fire is a lie
    about where the pacing lives.
    """
    try:
        from backend.app.models.printer import Printer

        name = await db.scalar(select(Printer.name).where(Printer.id == printer_id))
        await ws_manager.broadcast(
            {
                "type": "slot_standing_unknown",
                "printer_id": printer_id,
                "printer_name": name or f"Printer {printer_id}",
                "ams_id": ams_id,
                "tray_id": tray_id,
                "case": case,
            }
        )
    except Exception:  # noqa: BLE001 — observability must never break the calling lane
        logger.exception(
            "AMS presence: standing-unknown broadcast failed for printer %s AMS%d-T%d (case=%s)",
            printer_id,
            ams_id,
            tray_id,
            case,
        )


def _warn_owed_read_blocked(printer_id: int, ams_id: int, tray_id: int, blocker: str) -> None:
    """WARN once per :data:`_OWED_READ_REWARN_S` that a long-owed discovery read is blocked.

    Only for a slot whose last qualified physical cycle is older than
    :data:`_OWED_READ_WARN_AFTER_S`: below that the defer is ordinary pacing (the next
    terminal drains it). Above it, the farm has been carrying an unknown identity for
    the whole window — the 2026-07-25 shape, where the read stayed owed for six hours
    behind a permanently-engaged extruder and said nothing.

    LOG-ONLY: the operator ruling of 2026-08-11 demoted the toast this lane used to raise,
    because the mid-job blocker is non-actionable — the read physically cannot run until
    the printer idles, so a toast could only nag someone who has nothing to do about it.
    79 of the 80 firings in the week before the ruling carried exactly that blocker (38 in
    one day, across four printers, after ordinary roll swaps). Nothing is broken meanwhile:
    the slot runs on the tagless default filament (doctrine rule 2), dispatch is not held,
    and the read drains at the printer's next idle window. The operator surfaces for the
    physical situation behind it — a roll swapped while the printer was mid-job — are the
    tagless "Fresh roll?" prompt and the slot's own seated-unread render; this WARN is the
    durable record for whoever reads the log afterwards.
    """
    key = (printer_id, ams_id, tray_id)
    age = last_physical_cycle_age(printer_id, ams_id, tray_id)
    if age is None or age <= _OWED_READ_WARN_AFTER_S:
        return
    now = time.monotonic()
    last = _owed_read_warned_at.get(key)
    if last is not None and now - last < _OWED_READ_REWARN_S:
        return
    _owed_read_warned_at[key] = now
    logger.warning(
        "[Printer %s] AMS%d slot%d physically changed %.0fs ago, identity unknown, discovery read deferred: %s",
        printer_id,
        ams_id,
        tray_id,
        age,
        blocker,
    )


async def maybe_command_owed_identify(
    db: AsyncSession,
    printer_id: int,
    ams_id: int,
    tray_id: int,
    tray: dict,
    state,
    *,
    spoolman_active: bool = False,
) -> bool:
    """Spend an OWED discovery read on a slot when the wire is safe. Returns True if commanded.

    The third lane that can drain :func:`identify_needed`'s ``"discovery"`` verdict,
    after the idle presence gain and the terminal sweep. Both of those can miss it
    indefinitely: a gain that lands while the printer is RUNNING is never re-read, and
    the terminal sweep's engaged-filament pre-check defers every slot while filament
    sits in the extruder path — which on a continuously-loaded printer is always
    (2026-07-25: a slot changed at 19:39 was still unidentified at 01:55, six hours and
    many terminals later). Called from ``spool_tagless.reconcile_slot_config``'s
    state-derived walk, which supplies the missing OCCASION and nothing else.

    It also spends a STANDING OCCASION that could not be commanded when it was opened —
    the terminal sweep's between-prints policy and the spent-occupied constellation
    (:func:`open_spent_occupied_occasion`) both open one from a lane that may be refused on
    the spot (engaged filament, a busy wire), and before 2026-08-07 nothing drained those:
    the pre-check required an unanswered CYCLE, which a standing constellation does not
    have. An occasion is one read by construction, so draining it here cannot loop.

    DISCOVERY ONLY — deliberately narrower than :func:`identify_needed`'s full verdict
    set. ``"rfid_refresh"`` is a between-prints policy (the terminal sweep's job): a
    tagged slot always yields it, so honouring it on a ~20 s reconcile cadence would
    re-flap every tagged tray in the fleet forever. Same reasoning the idle-gain lane
    states for itself.

    Refuses (silently, spending nothing) while the printer is RUNNING/PAUSE, while
    filament is engaged, while the unit is drying, and whenever the client's own
    pre-flight (:meth:`ams_write_refusal`) says the wire is unsafe — the eligibility is
    left untouched for the next pass. The two "printer is busy with a job" refusals
    escalate to a WARNING once the read has been owed past
    :data:`_OWED_READ_WARN_AFTER_S`. The read itself goes through
    :func:`command_identify`, so echo arming / identity-learned / discovery stamps stay
    identical to every other lane's. Never raises — the caller is a scheduler tick.
    """
    try:
        # Cheap evidence pre-check: discovery is the only verdict this lane spends, so a
        # slot with neither of its two entitlements costs no DB query at all. An unanswered
        # cycle is the event-shaped one; a STANDING OPEN OCCASION is the other, and it was
        # missing — a slot whose occasion the terminal sweep or the spent-occupied
        # constellation opened but could not command at open time (engaged filament, a
        # refused wire, a missed presence edge) had NO drain at all, because the cycle test
        # alone is False for it. That is the 2026-08-07 spool 226 shape. Cost for a slot
        # WITH an open occasion: one ``identify_needed`` evaluation (with its DB peek) per
        # reconcile pass, bounded — the first accepted command consumes the occasion.
        if not _unanswered_cycle(printer_id, ams_id, tray_id) and not _read_occasion_open(printer_id, ams_id, tray_id):
            return False
        reason = await identify_needed(db, printer_id, ams_id, tray_id, tray, spoolman_active)
        if reason != "discovery":
            return False

        if _printer_running(state):
            _warn_owed_read_blocked(printer_id, ams_id, tray_id, "printer is mid-job")
            return False
        if _filament_engaged(printer_id):
            _warn_owed_read_blocked(printer_id, ams_id, tray_id, "filament engaged")
            return False
        if unit_drying(printer_id, ams_id):
            return False
        client = printer_manager.get_client(printer_id)
        if client is None:
            return False
        # DEFER on a refused wire, never WAIT for it (invariant 2: callers may defer,
        # never pre-approve). This lane runs inside a scheduler tick that walks the whole
        # fleet, and the gate it would be waiting on is armed by our OWN previous
        # identify — awaiting ``wait_ams_settle`` here would turn the drain into a
        # self-clocked command loop at exactly the gate's period, which is the 2026-08-07
        # storm's cadence. The occasion is untouched, so the next pass retries for free.
        refusal = client.ams_write_refusal(ams_id)
        if refusal is not None:
            logger.debug(
                "[Printer %s] owed discovery read deferred: AMS%d slot%d — %s",
                printer_id,
                ams_id,
                tray_id,
                refusal,
            )
            return False

        ok, _msg = await command_identify(printer_id, ams_id, tray_id, source="reconcile", reason=reason)
        return ok
    except Exception:  # noqa: BLE001 — a scheduler-tick lane must never raise
        logger.exception("Owed discovery read failed for printer %s AMS%d-T%d", printer_id, ams_id, tray_id)
        return False


# How long after a commanded DISCOVERY read the absence of a tag counts as that read's
# ANSWER. The identify cycle a command starts runs ~10-20 s (hard-bounded by
# :data:`_IDENTIFY_ACTIVE_S`), so 15 s is late enough that a read which WILL answer with a
# tag has answered — the answer re-stamps ``_slot_read_at`` through
# :func:`note_identity_learned` — and early enough that a parked slot resolves on the next
# reconcile pass rather than the next print. A CAUSE window like every other constant here,
# not a duration filter on identity.
_NO_TAG_ANSWER_SETTLE_S = 15.0


def read_answered_no_tag(printer_id: int, ams_id: int, tray_id: int, *, tray_seated: bool, tray_bare: bool) -> bool:
    """Hardware evidence that the seated object is NOT the bound tagged roll.

    The other half of the 2026-08-07 spool 226 deadlock. The owed discovery read DID
    eventually fire (20:03 prod) and answered NO TAG — the expected answer for the tagless
    roll the operator had inserted — and nothing consumed it: the tray stayed bare, so the
    tagless lane's row 4 was unreachable, and the slot parked with the spent tagged binding
    forever. This is the accessor that turns that silence into a fact the decision table can
    conclude on (``spent_swap_no_tag_read``).

    True iff ALL of:

    * we ASKED this slot a question that can answer "no tag" — ``_discovery_read_at`` holds
      a stamp for it (only :func:`command_identify` stamps it, only for a ``discovery``
      read);
    * at least :data:`_NO_TAG_ANSWER_SETTLE_S` has elapsed since that stamp;
    * no identify is in flight on the slot (:func:`identify_in_flight`);
    * ``_slot_read_at`` is NOT newer than the discovery stamp. A tag landing re-stamps it
      through :func:`note_identity_learned` on the firmware's publish, so a newer value
      means the read answered WITH a tag (or a newer read owns the slot) — either way this
      is not a no-tag answer. Equal stamps are the command's own pair (identity-learned is
      stamped immediately before the discovery stamp), not an answer;
    * the caller's ``tray_seated`` and ``tray_bare`` are both True.

    The tray facts are CALLER-SUPPLIED on purpose: the observation lane that declared the
    debt adjudicates it, and this module must not re-derive presence from the merged view
    (the merge can present a chimera — that is the whole reason observations exist).

    The settle constant is a FLOOR, not the whole pacing: a read commanded on a slot that was
    already PRESENT also armed the echo flag, so :func:`identify_in_flight` holds True for
    :data:`_IDENTIFY_ACTIVE_S` (30 s) and the answer actually lands then. Both gates are
    conservative in the same direction (later, never earlier), which is the correct bias for
    a fact that authorizes retiring a ledger row.

    A false positive — a transient read failure on a genuinely tagged roll — self-corrects:
    the next successful read finds the tag and the identity lane displaces the never-fed
    minted row. Pure in-memory peek, NON-consuming (``_discovery_read_at`` is never popped;
    it also drives the ``0700_0081`` HMS suppression below), never raises.
    """
    if not (tray_seated and tray_bare):
        return False
    key = (printer_id, ams_id, tray_id)
    stamp = _discovery_read_at.get(key)
    if stamp is None:
        return False
    if time.monotonic() - stamp < _NO_TAG_ANSWER_SETTLE_S:
        return False
    if identify_in_flight(printer_id, ams_id, tray_id):
        return False
    read_at = _slot_read_at.get(key)
    return not (read_at is not None and read_at > stamp)


# (printer_id, ams_id, tray_id) -> the ``_discovery_read_at`` stamp whose NO-TAG answer has
# already been closed out by :func:`close_answered_read`. Not a dedup for politeness: the
# closure pops ledgers, so repeating it is harmless, but the INFO line that records WHY a
# slot stopped earning reads must fire once per answer and not at the push cadence.
_no_tag_answer_closed: dict[tuple[int, int, int], float] = {}


def close_answered_read(printer_id: int, ams_id: int, tray_id: int, *, tray_seated: bool, tray_bare: bool) -> bool:
    """A commanded read that answered NO TAG closes the entitlements that bought it.

    "No tag" is an ANSWER, not a failure to answer — for a tagless roll it is the only
    answer there is — and an answered question must stop being a reason to ask. Before
    this, exactly one consumer concluded on it (``slot_state`` row 5a, spent + TAGGED
    bindings) and every other constellation went on re-earning reads from the same
    evidence. The loop is self-feeding, which is what made it expensive: an identify
    cycle flaps the tray present→9→present, and any flap the echo-swallow and the
    identify-explains suppression do not catch is banked as a fresh qualified gain — a
    new cycle AND a new occasion, manufactured by the read itself (2026-08-07: 419
    identifies on one slot, 1000 on one printer in a day, each holding that printer's
    30 s identify gate so every slot decision on it deferred).

    So the two READ entitlements are spent here, and they are spent for EVERY binding
    state — bound, spent, tagless, unbound, or none at all, because the ledgers this
    touches know nothing about bindings:

    * the unanswered-cycle arm, by dropping the slot's physical-cycle stamp — the cycle
      HAS been answered now;
    * the open read occasion.

    A new read then needs a NEW cause: a fresh physical cycle, the terminal's
    between-prints policy, an episode occasion, or an operator.

    Deliberately NOT consumed: ``spool_tagless``'s pending physical cycle. That is a
    different currency — the SWAP evidence a bare tray's auto-configure and the table's
    spent-swap arms live on — and spending it here would starve the very transitions the
    answer enables. Deliberately NOT re-stamped: ``_slot_read_at``, because
    :func:`read_answered_no_tag` reads it and row 5a must still be able to conclude on
    this answer in the same pass.

    Returns True on the pass that closes the answer. Pure in-memory, never raises.
    """
    if not read_answered_no_tag(printer_id, ams_id, tray_id, tray_seated=tray_seated, tray_bare=tray_bare):
        return False
    key = (printer_id, ams_id, tray_id)
    stamp = _discovery_read_at.get(key)
    if _no_tag_answer_closed.get(key) == stamp:
        return False
    _no_tag_answer_closed[key] = stamp
    _physical_cycle_at.pop(key, None)
    _read_occasion_at.pop(key, None)
    logger.info(
        "AMS presence: read answered NO TAG for printer %d AMS%d-T%d — read entitlements spent, "
        "a further read needs a new cause",
        printer_id,
        ams_id,
        tray_id,
    )
    return True


def is_expected_read_failure(printer_id: int, attr: int, code: int) -> bool:
    """True when a filament-read-failure HMS is the answer to OUR OWN discovery read.

    A discovery read asks a slot that may have no tag; the firmware answers a missing
    tag with ``0700_2X00_0001_0081`` / ``07XX_4025`` ("Failed to read the filament
    information… the AMS main board may be malfunctioning"). That answer means "no tag
    ⇒ tagless ⇒ the default filament assumption stands" — not a fault, and worth no
    notification. Matching requires OUR read on that exact slot within
    ``_DISCOVERY_READ_WINDOW_S``; the ``07XX_4025`` shape names no slot, so it is
    matched against a fresh discovery read on the same AMS UNIT instead.

    An UNMATCHED read failure still notifies — that one is a genuinely failing reader.
    Classification and attr decoding are delegated to ``services.hms_errors`` (the
    single owner of HMS layout knowledge). Never raises: the caller is the status
    notification path.
    """
    try:
        if not hms_errors.is_filament_read_failure(attr, code):
            return False
        now = time.monotonic()
        slot = hms_errors.filament_read_failure_slot(attr, code)
        if slot is not None:
            ts = _discovery_read_at.get((printer_id, slot[0], slot[1]))
            return ts is not None and now - ts < _DISCOVERY_READ_WINDOW_S
        unit = hms_errors.ams_unit_from_attr(attr)
        if unit is None:
            return False
        return any(
            pid == printer_id and aid == unit and now - ts < _DISCOVERY_READ_WINDOW_S
            for (pid, aid, _tray), ts in _discovery_read_at.items()
        )
    except Exception:  # noqa: BLE001 — must never break the HMS notification path
        logger.exception("AMS presence: expected-read-failure check failed for printer %s", printer_id)
        return False


# --- presence-gain RFID re-read -------------------------------------------


async def on_tray_observations(printer_id: int, observations: list[TrayObservation], db: AsyncSession) -> None:
    """Track presence transitions for a printer's AMS trays, from the RAW push (E1).

    Called from ``printer_manager._run_slot_pipeline_pass`` with the observations of
    ONE raw push and an open session, immediately BEFORE ``run_slot_pipeline`` resolves
    that same push. On a presence GAIN while the printer is idle it fires an immediate
    per-slot RFID re-read (so a Bambu spool resolves via the tag path fast; mid-print
    refills are handled by the terminal sweep). Never raises — a farm-side failure must
    never break the AMS callback chain.

    WHY THE RAW LANE (2026-08-07, 001-H2S slot 1). Presence edges used to be fed from
    ``main.on_ams_change`` with the MERGED payload, which runs only when bambu_mqtt's
    change hash flips — and the merged view can be BLIND: a stale ``tray_exist_bits``
    cache demoted a genuinely inserted roll back to "empty" on every push for 38
    minutes, so the operator's insert AND their pull+reinsert produced NO edges at all
    (no qualified cycle, no read occasion, no FIFO stamp) while the raw observation
    lane — which has owned identity and binding since the 2026-08-02 cutover — saw the
    roll the whole time. That cutover moved identity to the raw lane but left presence
    behind; this finishes it. Edges and binding decisions now read the SAME push, so
    they can no longer disagree about what the wire said, and the merged lane keeps
    display + ledger-consumer duties only.

    PRESENCE IS TRI-STATE HERE, and that is new-and-correct rather than a port defect.
    The merged lane's ``_tray_present`` answered a BOOLEAN for every tray, so "this push
    said nothing about the slot" was indistinguishable from "the slot is empty" and a
    reduced mid-print push could manufacture a loss edge out of silence.
    ``obs.present is None`` now means UNKNOWN: the tray's presence duties are skipped
    entirely — ``_last_presence`` is left exactly as it was and NO edge is derived, so a
    later real edge still has an honest ``prev`` to compare against. Unknown never
    manufactures an edge (the doctrine's gate-on-``is False``-only rule, applied to
    edges instead of releases).

    FIRST-BATCH SEEDING. The first batch for a printer only seeds ``_last_presence`` —
    a refill done while the server was down must not read as a fresh gain — and the
    connect pushall carries every tray, so that batch is comprehensive. A tray that
    first APPEARS in a LATER partial with ``prev`` unset and ``present is True`` is a
    genuine gain whose absence START was never observed: ``absent_at`` is None, so it
    takes the existing ``qualified=True, physical_cycle=False`` path — it OPENS a read
    occasion but banks no measured cycle. That closes the boot-seeding hole (such a
    gain used to fire nothing at all) without letting an unmeasured absence mint a
    spool row or prompt the operator.
    """
    try:
        is_first = printer_id not in _primed
        _primed.add(printer_id)

        state = printer_manager.get_status(printer_id)
        running = _printer_running(state)
        # A PAUSEd printer's gains are the ones the recovery machines are waiting for
        # (runout refill / jam reinsert), so the print-start suppression never applies
        # to them — pinned by test, because both machines break silently if it does.
        paused = (getattr(state, "state", None) or "") == "PAUSE"

        # Sample the unit's identify flag on every AMS push. A firmware-AUTONOMOUS read
        # carries no command of ours, and its flag can rise and fall entirely between two
        # pushes — so the LIVE check alone cannot explain a tray flap it caused. Sampling
        # turns it into a cause with a window (:func:`_identify_explains_gain`).
        if getattr(state, "ams_status_main", 0) == AMS_STATUS_IDENTIFYING:
            _identifying_seen_at[printer_id] = time.monotonic()

        for obs in observations:
            ams_id = obs.ams_id
            tray_id = obs.tray_id
            key = obs.slot
            present = obs.present
            prev = _last_presence.get(key)

            # Only an ANSWER about presence updates the map (see the tri-state note in
            # the docstring). ``None`` leaves ``prev`` standing and derives no edge.
            if present is not None:
                _last_presence[key] = present

            if is_first:
                # First batch after a (re)start only seeds the presence map so
                # a refill done while down doesn't read as a fresh gain.
                continue

            if present is False and prev:
                # PRESENT→ABSENT: stamp the absence start so a later genuine GAIN
                # can tell a real physical roll swap (≥ _MIN_PHYSICAL_ABSENT_S)
                # from a runout-instant state flap (sub-second). Alongside it,
                # record — while the signal is freshest — whether an identify
                # explains this absence: an identify unloads the tray for ~10–20 s,
                # a read flap the later gain must never bank as a QUALIFIED cycle
                # however long it runs (duration ≠ identity, the doctrine's rule 6).
                _absent_since[key] = time.monotonic()
                _absent_under_identify[key] = _identify_explains_absence(printer_id, ams_id, tray_id)
                # …and the third stamp: was this slot FEEDING when it emptied? Recorded
                # here for the same reason as the one above — the answer exists only
                # while the evidence is fresh, and inside the ~3-minute bay-clear→HMS
                # gap the firmware has not yet said why the bay went empty. A slot that
                # empties mid-feed is running out or being pulled, never glitching, so
                # its return is a refill and the de-bounce lane must refuse it.
                _absent_under_active_feed[key] = _slot_was_active_feeder(printer_id, ams_id, tray_id, state, running)
                # The slot is absent again: whatever its last return measured no longer
                # describes it.
                _reseat.pop(key, None)

            if present is True and not prev:
                # Consume the slot's commanded-read cause on the FIRST gain that
                # follows it, whichever gain that is: the identify's own echo (about
                # to be swallowed below) or the settle-back the qualifier judges. One
                # command explains one flap — leaving the stamp behind would suppress
                # the genuine pull+reseat that comes after the identify settled, the
                # exact over-suppression _identify_explains_absence warns about.
                commanded_at = _commanded_read_at.pop(key, None)

                # Echo-consume FIRST: a re-read we commanded on this present
                # slot flaps the firmware's tray state present→9→present
                # (~20 s); the settle-back arrives here as a fresh gain. If a
                # flag is armed for the slot, THIS gain is our command's own
                # echo — pop it and swallow the whole edge (no re-read AND no
                # feed-fault clear; the spool never physically moved). Popped
                # regardless of ``running`` — an echo can land as a print
                # starts. A stale flag (identify never ran) reads as no flag →
                # the gain acts normally.
                ts = _echo_pending.pop(key, None)
                if ts is not None and time.monotonic() - ts < _ECHO_PENDING_STALE_S:
                    logger.debug(
                        "AMS presence: swallowed re-read echo for printer %d AMS%d-T%d",
                        printer_id,
                        ams_id,
                        tray_id,
                    )
                    continue

                # Consume the absence stamp for this genuine gain (an echo above
                # never reaches here). ≥ _MIN_PHYSICAL_ABSENT_S ⇒ a real physical
                # roll swap; a firmware runout state flap is sub-second → no cycle.
                # An absence we never saw START (slot absent since the first push,
                # a tray first appearing in a later partial, or two edges coalesced
                # into one payload) has UNKNOWN duration, not a flap — it qualifies.
                # spool_tagless keeps the stricter measured-only rule below, because
                # minting a spool row on a false positive is expensive while a
                # suppressed discovery read is not.
                absent_at = _absent_since.pop(key, None)
                absent_for = None if absent_at is None else time.monotonic() - absent_at
                # Bank what this return MEASURED, for the slot pipeline's de-bounce lane
                # (``slot_state`` row 4c). Popped in lockstep with ``_absent_since``, and
                # recorded regardless of ``identify_explained`` below: an identify flap is
                # precisely a case where the roll never moved, so a release it provoked is
                # exactly the spurious one the de-bounce exists to repair. Duration is
                # only ever a NEGATIVE here — outside the window the table mints — so this
                # never becomes a timer deciding identity (doctrine rule 7 as amended).
                _reseat[key] = _Reseat(absent_for, _absent_under_active_feed.pop(key, False))
                # An identify unloads the tray, and a print start engages the AMS:
                # an absence that BEGAN under identify activity (flag captured at its
                # start edge, freshest there), or one explained by ANY non-physical
                # cause inside the absence/gain window (a read we commanded whatever
                # the tray state was, an observed firmware-autonomous identify, or the
                # printer entering RUNNING), is a machine flap and never a physical
                # cycle — no matter how long it ran. The ≥5 s filter measures duration,
                # never identity (rule 6); the two gate the gain together below.
                identify_explained = _absent_under_identify.pop(key, False) or _identify_explains_gain(
                    printer_id,
                    ams_id,
                    tray_id,
                    absent_at=absent_at,
                    commanded_at=commanded_at,
                    paused=paused,
                )
                physical_cycle = (
                    absent_for is not None and absent_for >= _MIN_PHYSICAL_ABSENT_S and not identify_explained
                )

                # Genuine physical re-insert: clear any feed-fault out-of-
                # rotation flag. NOT idle-gated (a spool untangled and re-
                # seated mid-print clears too) and NOT on the first-batch seed.
                # Gated on NOT drying: a drying cycle flaps tray presence
                # (state → 10) with no physical event, and a jammed spool must
                # not silently re-enter rotation from a drying flap.
                # Best-effort — a failure must never break the AMS callback.
                if not unit_drying(printer_id, ams_id):
                    # ``qualified`` = a genuine gain that is NOT a MEASURED sub-5 s
                    # flap: the preceding absence either lasted >= _MIN_PHYSICAL_ABSENT_S
                    # OR its start was never observed (boot-seed / first sight in a later
                    # partial / two coalesced edges), so its duration is UNKNOWN — not a
                    # flap, it qualifies (see the absence-consume comment above).
                    # ``physical_cycle`` is the STRICTER measured->=5 s subset. The two
                    # gate different actions below.
                    # An identify-explained absence is a read flap, never a human roll
                    # movement, so it disqualifies BOTH tiers (the invariant this fix
                    # adds); a missing/unknown-duration absence still qualifies ONLY
                    # when no identify explains it.
                    qualified = (absent_for is None or absent_for >= _MIN_PHYSICAL_ABSENT_S) and not identify_explained

                    # Record the change for the identify lanes. Drying-gated with
                    # the rest: a drying cycle disengages trays with no physical
                    # event, and change evidence must mean somebody moved a spool.
                    _note_gain(printer_id, ams_id, tray_id, qualified=qualified)

                    try:
                        from backend.app.services.spool_recovery import clear_on_reinsert

                        await clear_on_reinsert(db, printer_id, ams_id, tray_id, observation_tray_dict(obs))
                    except Exception:  # noqa: BLE001 — best-effort clear
                        logger.exception(
                            "AMS presence: feed-fault clear failed for printer %d AMS%d-T%d",
                            printer_id,
                            ams_id,
                            tray_id,
                        )

                    # Refill auto-resume (006-H2S 2026-07-26): this gain may be the
                    # same-slot refill a runout-escalated print is PAUSEd waiting
                    # for. Spawned, not awaited — it sleeps out an AMS settle window
                    # before resuming and must not hold the AMS callback. Through
                    # spawn_background_task (core/tasks.py: the one sanctioned
                    # create_task call site) so the sleeping task keeps a strong
                    # reference and cannot be GC'd mid-wait. The service owns every
                    # gate (setting, live PAUSE, the runout hold, and "the firmware
                    # is demanding THIS slot") and never raises.
                    try:
                        from backend.app.core.tasks import spawn_background_task
                        from backend.app.services.spool_recovery import maybe_auto_resume_on_refill

                        spawn_background_task(
                            maybe_auto_resume_on_refill(printer_id, ams_id, tray_id),
                            name=f"runout-refill-resume-p{printer_id}-ams{ams_id}-t{tray_id}",
                        )
                    except Exception:  # noqa: BLE001 — best-effort assist
                        logger.exception(
                            "AMS presence: refill auto-resume spawn failed for printer %d AMS%d-T%d",
                            printer_id,
                            ams_id,
                            tray_id,
                        )

                    # W1/W5 spent-binding-latch release + fresh-roll prompt fire ONLY on
                    # the STRICT ``physical_cycle`` (a MEASURED >= 5 s absence). Minting a
                    # spool row or prompting on a false positive is expensive, so the
                    # unknown-duration case is deliberately excluded here. Guarded like
                    # the clear above; never break the AMS callback.
                    if physical_cycle:
                        try:
                            from backend.app.services import spool_tagless

                            await spool_tagless.note_physical_cycle(printer_id, ams_id, tray_id)
                        except Exception:  # noqa: BLE001 — best-effort physical-cycle note
                            logger.exception(
                                "AMS presence: physical-cycle note failed for printer %d AMS%d-T%d",
                                printer_id,
                                ams_id,
                                tray_id,
                            )

                    # Re-stampable FIFO ordinal (006-H2S) fires on the WIDER
                    # ``qualified`` gate — a boot-spanning / unknown-duration re-seat
                    # must still adjudicate to honour rule 2's restart-durability
                    # contract, and a wrong re-stamp is only a FIFO demotion, never an
                    # expensive mint/prompt (the deliberate two-tier asymmetry vs the
                    # strict physical_cycle gate above). A MEASURED sub-5 s flap still
                    # fires nothing. The grams-state + identity DECISION inside the
                    # adjudicator consults no timing — this gate is only the wire-flap
                    # debounce. Best-effort — never break the AMS callback.
                    if qualified:
                        try:
                            from backend.app.services.spool_binding import stamp_loaded_for_slot

                            await stamp_loaded_for_slot(db, printer_id, ams_id, tray_id)
                        except Exception:  # noqa: BLE001 — best-effort loaded_at re-stamp
                            logger.exception(
                                "AMS presence: loaded_at re-stamp failed for printer %d AMS%d-T%d",
                                printer_id,
                                ams_id,
                                tray_id,
                            )

            # The firmware answered for this slot: its identity is current, so an
            # older physical cycle is no longer unanswered evidence and the
            # terminal sweep must not spend a discovery read on it. Stamped on
            # ANY push carrying a valid tag, not just gains — a tag that lands
            # seconds after an insert is exactly the answer we were waiting for —
            # and deliberately INDEPENDENT of presence: a reduced push, or an
            # always-``state=3`` dialect (A1 family / P1S), answers about identity
            # while saying nothing about presence, and that answer still counts.
            # ``identity_asserted`` is a STRONGER source than the merged lane's
            # ``_tray_tagged``: it is the observation's ATOMIC pair, so a chimera
            # (this push's tag beside a previous push's uuid, the 001-T3 class) can
            # never be read as an answer here.
            if obs.identity_asserted:
                note_identity_learned(printer_id, ams_id, tray_id)

            # …and the OTHER answer a commanded read can give. A read whose settle window
            # passed with no identity learned has answered NO TAG, and that answer spends
            # the entitlements it was bought with, whatever the slot is bound to. Runs on
            # every observation, not only gains: the answer lands ~15 s after the command,
            # by which time the slot is emitting steady-state pushes. Non-destructive to
            # the row-5a conclusion the pipeline draws from the same fact one step later.
            close_answered_read(
                printer_id,
                ams_id,
                tray_id,
                tray_seated=obs.present is True,
                tray_bare=not tray_identity_asserted(observation_tray_dict(obs)),
            )

            # Steady state: act only on a genuine presence GAIN, and only while
            # the printer is idle. Firing ams_get_rfid during a print is unsafe;
            # the terminal sweep handles mid-print refills. A LOSS only updates
            # the map above (NO auto-unassign). Skip while drying — a drying flap
            # is not a real insert and a re-read would fail the cycle. The need
            # check (an untouched tagless slot must never be read) lives in
            # identify_needed, evaluated with the tray this push asserted.
            #
            # This lane spends DISCOVERY reads only. The other verdict,
            # "rfid_refresh", is a between-prints policy: at a gain the firmware
            # has usually just read the tag itself (which is why the tray already
            # carries one), so commanding a read here would only re-flap a slot
            # whose identity is current. The terminal sweep does that refresh.
            if present is True and not prev and not running and not unit_drying(printer_id, ams_id):
                try:
                    # Settings read stays on the gain path only — a physical
                    # insert, not something every status push pays for.
                    reason = await identify_needed(
                        db, printer_id, ams_id, tray_id, observation_tray_dict(obs), await _spoolman_active(db)
                    )
                    if reason == "discovery":
                        await command_identify(printer_id, ams_id, tray_id, source="idle_gain", reason=reason)
                except Exception:  # noqa: BLE001 — best-effort re-read
                    logger.exception(
                        "AMS presence: immediate re-read failed for printer %d AMS%d-T%d",
                        printer_id,
                        ams_id,
                        tray_id,
                    )
    except Exception:  # noqa: BLE001 — must never crash the AMS callback chain
        logger.exception("AMS presence tracking failed for printer %s", printer_id)


# --- terminal RFID re-read sweep -------------------------------------------


async def _terminal_read_occasion(
    db: AsyncSession, printer_id: int, ams_id: int, tray_id: int, tray: dict
) -> str | None:
    """The BETWEEN-PRINTS policy: does this terminal open a read occasion for the slot?

    Doctrine rule 5 makes the terminal sweep load-bearing and need-driven in the same
    breath: reconcile tagged slots' remain / identity, discover CHANGED slots, and read
    nothing else — "never read-everything-every-print (that was the ``0700_0081``
    factory)". This predicate is the "need" half for slots whose evidence is STANDING
    rather than event-shaped; the changed-slot half is the unanswered physical cycle,
    which carries its own occasion.

    Two causes, both requiring state 10 (seated, tag facing the reader — state 11 is the
    same roll threaded on to the hub, which a read cannot answer without unloading):

    * ``wire_remain_missing`` / ``ledger_over_label`` — the bound roll is TAGGED, its
      weight is not operator-locked, and either the wire never delivered a usable
      ``remain`` (the full RFID data read did not complete: the tray reports ``-1`` /
      nothing, often together with no ``tag_uid`` at all) or the ledger has run PAST the
      label. Doctrine rule 8 makes wire remain% the truth for a tagged row, and
      ``usage_tracker.maybe_reconcile_tagged_ledger_decrease`` (the W6 auto-repair) has
      nothing to repair FROM until that read lands — which is how a live spool reached
      1899.9 g used against a 1000 g label. A healthy tagged slot publishes ``remain`` on
      every ordinary AMS push, so it needs no commanded read at all: that routine refresh
      was pure cost, and it is what this predicate replaces.
    * ``spent_occupied`` — the binding is spent-latched (rule 8: ``spent_at`` is the
      exhaustion truth) and the seated roll carries no wire identity, i.e. an
      unidentified NEWCOMER. :func:`identify_needed` classifies that read ``discovery``,
      so its expected no-tag failure is suppressed farm-side.

    Returns a short cause label for the log, or None. Read-only; the caller opens the
    occasion. Never raises into the sweep — an unresolvable slot simply owes nothing.
    """
    from backend.app.models.spool import Spool  # noqa: F401 — selectinload target
    from backend.app.models.spool_assignment import SpoolAssignment

    if _norm_state(tray.get("state")) != _TRAY_SEATED_STATE:
        return None

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
    spool = sa.spool if sa is not None else None
    if spool is None:
        return None

    live_tagged = _tray_tagged(tray)
    if getattr(spool, "spent_at", None) is not None and not live_tagged:
        return "spent_occupied"

    if not is_valid_tag(getattr(spool, "tag_uid", "") or "", getattr(spool, "tray_uuid", "") or ""):
        return None  # a tagless row has no wire remain to restore (rule 8)
    if getattr(spool, "weight_locked", False):
        return None  # the operator owns this row's weight; a wire refresh must not fight it

    remain = parse_int_field(tray.get("remain"))
    if remain is None or remain < 0:
        return "wire_remain_missing"

    label = spool.label_weight or 0
    used = spool.weight_used or 0
    if label > 0 and used > label:
        return "ledger_over_label"
    return None


async def on_printer_terminal(printer_id: int) -> None:
    """Reconcile a printer's AMS slots when a print reaches a terminal state.

    Called from ``main.on_print_complete`` (skipped for eject-job terminals so
    each unit cycle sweeps once at the PRINT terminal, not again at the eject
    terminal). One-shot per RUNNING/PAUSE→terminal transition; sequential, each
    read gated on the client's ``wait_ams_settle`` so identifies never overlap.
    Never raises. Results flow the normal RFID pipeline.

    Eligibility is :func:`identify_needed` and nothing else. What the terminal adds is
    the between-prints OCCASION (:func:`_terminal_read_occasion`) — a bound tagged roll
    whose wire ``remain`` never landed or whose ledger has run past its label, and a
    spent-latched binding under an unidentified roll. Physically-changed slots bring
    their own occasion; a slot nobody has touched, whose tagged roll is reporting a sane
    ``remain`` on the ordinary AMS pushes, gets NOTHING.
    """
    try:
        # Dedup duplicate terminal callbacks: on_print_complete can fire several
        # times for one ending. Key on the print's subtask_id (unique per
        # dispatch). The get/set is synchronous (no await between) so racing
        # create_task()d sweeps for the same terminal collapse to one.
        state = printer_manager.get_status(printer_id)
        subtask = (getattr(state, "subtask_id", None) or "") if state is not None else ""
        if _swept_subtasks.get(printer_id) == subtask:
            return
        _swept_subtasks[printer_id] = subtask

        client = printer_manager.get_client(printer_id)
        if state is None or client is None:
            return

        from backend.app.core.database import async_session

        eligible: list[tuple[int, int, str]] = []
        async with async_session() as db:
            spoolman_active = await _spoolman_active(db)
            for ams_unit in _iter_ams_units(state):
                if not isinstance(ams_unit, dict):
                    continue
                try:
                    ams_id = int(ams_unit.get("id", 0))
                except (TypeError, ValueError):
                    continue
                # Skip a drying unit: re-reading its slots disengages the trays and
                # fails the drying cycle (HMS 0700_C069). A later terminal or idle
                # gain re-reads once drying ends.
                if unit_drying(printer_id, ams_id):
                    logger.debug(
                        "[Printer %s] terminal AMS reconcile: skipping AMS%d — unit is drying",
                        printer_id,
                        ams_id,
                    )
                    continue
                for tray in ams_unit.get("tray", []) or []:
                    if not isinstance(tray, dict):
                        continue
                    try:
                        tray_id = int(tray.get("id", 0))
                    except (TypeError, ValueError):
                        continue
                    # The between-prints policy is the terminal's own OCCASION source
                    # (:data:`_read_occasion_at`); ``identify_needed`` stays the single
                    # need authority that turns it into a verdict. Splitting it this way
                    # keeps ONE eligibility rule for every lane — the sweep supplies the
                    # occasion, never a private permission.
                    cause = await _terminal_read_occasion(db, printer_id, ams_id, tray_id, tray)
                    if cause is not None:
                        open_read_occasion(printer_id, ams_id, tray_id)
                        logger.debug(
                            "[Printer %s] terminal AMS reconcile: AMS%d slot%d read occasion opened — %s",
                            printer_id,
                            ams_id,
                            tray_id,
                            cause,
                        )
                    reason = await identify_needed(db, printer_id, ams_id, tray_id, tray, spoolman_active)
                    if reason is None:
                        continue
                    eligible.append((ams_id, tray_id, reason))

        if not eligible:
            return

        logger.info("[Printer %s] terminal AMS reconcile: %d slot(s) need an identify", printer_id, len(eligible))
        for ams_id, tray_id, reason in eligible:
            # Settle-wait FIRST (including before the first read): the client blocks
            # until its AMS is not identifying AND our per-printer identify gate has
            # cleared, so sequential re-reads never overlap AND the firmware's own
            # auto-read of a freshly seated spool gets to finish first — the
            # command-time re-check below then sees the tag it produced. This
            # event-informed pace (poll of the client's own state; the gate-clear runs
            # on the paho thread) replaces the old fixed inter-read spacing loop.
            await client.wait_ams_settle()
            # Skip a slot whose identify is already in flight — a concurrent idle
            # gain re-read on THIS slot armed the echo flag, and commanding a second
            # ams_get_rfid now is the witnessed gain-vs-sweep double command that
            # fails the read. Checked at COMMAND time, not during eligibility
            # collection, so a gain that arms the flag mid-sweep is still caught.
            ts = _echo_pending.get((printer_id, ams_id, tray_id))
            if ts is not None and time.monotonic() - ts < _IDENTIFY_ACTIVE_S:
                logger.debug(
                    "[Printer %s] terminal AMS reconcile: skipping AMS%d slot%d — identify already in flight",
                    printer_id,
                    ams_id,
                    tray_id,
                )
                continue
            # The firmware may have answered while we waited: a discovery read exists
            # only to find out what is in the slot, so a tag that has landed since
            # collection makes it pointless. Spend nothing — the next terminal re-reads
            # it as an rfid_refresh if it is still there.
            if reason == "discovery":
                live_tray = _find_tray(printer_id, ams_id, tray_id)
                if live_tray is not None and _tray_tagged(live_tray):
                    note_identity_learned(printer_id, ams_id, tray_id)
                    logger.debug(
                        "[Printer %s] terminal AMS reconcile: skipping AMS%d slot%d — firmware answered",
                        printer_id,
                        ams_id,
                        tray_id,
                    )
                    continue
            try:
                await command_identify(printer_id, ams_id, tray_id, source="terminal_sweep", reason=reason)
            except Exception:  # noqa: BLE001 — one failed read must not stop the sweep
                logger.exception(
                    "[Printer %s] terminal AMS reconcile failed: AMS%d slot%d", printer_id, ams_id, tray_id
                )
    except Exception:  # noqa: BLE001 — the sweep must never crash the completion callback
        logger.exception("AMS terminal RFID re-read sweep failed for printer %s", printer_id)
