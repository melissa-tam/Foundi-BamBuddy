"""Mid-run AMS refill recognition (presence + terminal RFID re-read).

Single owner of AMS presence-transition policy. Everything downstream of
``main.on_ams_change`` already handles refills (RFID auto-create/assign, reused-
tag respool, tagless auto-mint/auto-config in ``services.spool_tagless``, low-
spool staged-unit auto-release), but that pipeline only fires when the AMS
change-hash changes. This module closes the gap the hash alone cannot see — a
spool inserted while idle or mid-print that the firmware never auto-reads:

* :func:`on_ams_change` — presence-transition tracking, called from
  ``main.on_ams_change`` inside the existing per-printer lock. A genuine presence
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

  - ``"rfid_refresh"`` — the slot is live-tagged, or DB-bound to a spool that
    carries a tag identity. The read SUCCEEDS and refreshes ``remain`` (tagless gram
    tracking's corroboration + reused-core respool detection), so it is always worth
    issuing.
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

Every ``ams_get_rfid`` the farm issues goes through the single commander
:func:`command_identify`, which owns the need check, the echo-consume arming, the
read bookkeeping and the discovery stamp.

Presence is POSITIVE-evidence-only: ``state ∈ {10, 11}`` is present; state 9,
None, and unknown dialect codes (H2C idle empties report ``state=0``) all read
absent, so an H2C never reads as phantom spools.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.services import hms_errors
from backend.app.services.bambu_mqtt import AMS_STATUS_IDENTIFYING, TRAY_PRESENT_STATES
from backend.app.services.printer_manager import printer_manager
from backend.app.services.spool_tag_matcher import is_valid_tag

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

# Printers whose first on_ams_change (post-restart) has been processed. The first
# push only seeds the presence map (no re-read); later pushes act on gains.
_primed: set[int] = set()

# printer_id -> subtask_id already swept at its terminal. Dedupes duplicate
# on_print_complete callbacks for the same print (one-shot per RUNNING→terminal).
_swept_subtasks: dict[int, str] = {}

# (printer_id, ams_id, tray_id) -> time.monotonic() at which a re-read command was
# issued on a PRESENT slot. A commanded re-read (ams_get_rfid) on an occupied slot
# makes the firmware run a ~20 s identify cycle during which the tray state flaps
# present→9→present; the settle-back is a fresh absent→present GAIN edge that
# on_ams_change would answer with ANOTHER re-read — a self-sustaining ~22 s loop.
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


def _reset_state() -> None:
    """Test hook: clear all module-level edge state between cases."""
    _last_presence.clear()
    _absent_since.clear()
    _primed.clear()
    _swept_subtasks.clear()
    _echo_pending.clear()
    _gain_at.clear()
    _physical_cycle_at.clear()
    _slot_read_at.clear()
    _discovery_read_at.clear()


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
    own command's echo and swallowed exactly once (see :func:`on_ams_change`).

    An identify on an EMPTY (state 9/absent) slot produces NO edge at all, so an
    empty slot is deliberately NOT armed: arming it would eat a real insertion
    made right after a print ends — the exact operator flow this design protects.
    All commanders route through :func:`command_identify`, which calls this, so the
    present-at-command-time guard lives in one place.
    """
    tray = _find_tray(printer_id, ams_id, tray_id)
    if tray is not None and _tray_present(tray):
        _echo_pending[(printer_id, ams_id, tray_id)] = time.monotonic()


def identify_in_flight(printer_id: int, ams_id: int, tray_id: int) -> bool:
    """True while a commanded identify may still be running on this slot, or the
    AMS unit is actively identifying any tray. Read-only (never pops the flag)."""
    state = printer_manager.get_status(printer_id)
    if getattr(state, "ams_status_main", 0) == AMS_STATUS_IDENTIFYING:
        return True
    ts = _echo_pending.get((printer_id, ams_id, tray_id))
    return ts is not None and time.monotonic() - ts < _IDENTIFY_ACTIVE_S


# --- Change-evidence ledger -----------------------------------------------


def _note_gain(printer_id: int, ams_id: int, tray_id: int, *, qualified: bool) -> None:
    """Record a genuine presence GAIN on a slot (never an echo or a first-push seed).

    ``qualified`` marks a gain that is NOT a sub-``_MIN_PHYSICAL_ABSENT_S`` flap, i.e.
    a real physical roll movement rather than the runout-instant state flap a firmware
    backup switch produces. Only a qualified gain becomes discovery evidence; an
    unqualified one still updates the gain stamp (:func:`recent_gain_age`).
    """
    now = time.monotonic()
    key = (printer_id, ams_id, tray_id)
    _gain_at[key] = now
    if qualified:
        _physical_cycle_at[key] = now


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


# --- Assignment context ----------------------------------------------------


async def _spoolman_active(db: AsyncSession) -> bool:
    from backend.app.api.routes.settings import get_setting

    val = await get_setting(db, "spoolman_enabled")
    return bool(val) and val.lower() == "true"


async def _slot_assignment_context(
    db: AsyncSession, printer_id: int, ams_id: int, tray_id: int, spoolman_active: bool
) -> tuple[bool, bool]:
    """Resolve ``(has_assignment, bound_spool_tagged)`` for a slot.

    The internal ``SpoolAssignment`` is the source of truth; when Spoolman mode is
    active a ``SpoolmanSlotAssignment`` also counts as an assignment. Tag identity is
    decided by the ONE predicate the whole fork uses,
    ``spool_tag_matcher.is_valid_tag``, applied to the bound spool's stored
    ``tag_uid``/``tray_uuid`` — a Spoolman binding is therefore never "tagged" (the
    Spoolman mirror stores no RFID identity), so such a slot can only qualify for a
    re-read through its LIVE tag or a discovery cycle.

    ``bound_spool_tagged`` is what makes a slot worth re-reading at every terminal:
    the read succeeds and refreshes ``remain`` for gram tracking and reused-core
    detection. The old ``data_origin``/``spent`` fields are gone with the
    origin-based sweep rule they served.
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
        return True, tagged

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
            return True, False

    return False, False


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
      tag identity. A tag can only be re-read from a SEATED spool, so this reason
      additionally requires presence: commanding a read on a slot whose bound tagged
      spool has been pulled would fail exactly like a tagless read and raise a
      never-clearing ``0700_2X00_0001_0081``.
    * ``"discovery"`` — a qualified physical cycle is unanswered since the slot's
      identity was last learned and the tray is still unidentified. One read settles
      it; a failure is the answer "no tag ⇒ tagless" and is suppressed farm-side.
      Checked BEFORE the DB-bound-tagged rule on an untagged tray on purpose: once
      something physically changed in the slot, the DB's idea of what is in it is a
      hypothesis, and the read must be treated as one that may legitimately fail.
    * ``None`` — everything else. An untouched tagless slot lands here, which is the
      entire fix for the standing "failed to read the filament information" errors.

    Pure predicate: it never commands anything and never mutates the ledgers.
    """
    if _norm_state(tray.get("state")) not in _IDENTIFIABLE_STATES:
        return None  # unknown dialect / no data — never acted on

    present = _tray_present(tray)
    if present and _tray_tagged(tray):
        return "rfid_refresh"

    if _unanswered_cycle(printer_id, ams_id, tray_id):
        return "discovery"

    if present:
        _has_assignment, bound_tagged = await _slot_assignment_context(db, printer_id, ams_id, tray_id, spoolman_active)
        if bound_tagged:
            return "rfid_refresh"

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


async def on_ams_change(printer_id: int, ams_data: list, db: AsyncSession) -> None:
    """Track presence transitions for a printer's AMS trays.

    Called from ``main.on_ams_change`` inside the per-printer assignment lock with
    the merged tray state and an open session. On a presence GAIN while the
    printer is idle it fires an immediate per-slot RFID re-read (so a Bambu spool
    resolves via the tag path fast; mid-print refills are handled by the terminal
    sweep). Never raises — a farm-side failure must never break the AMS callback
    chain.
    """
    try:
        is_first = printer_id not in _primed
        _primed.add(printer_id)

        state = printer_manager.get_status(printer_id)
        running = _printer_running(state)

        for ams_unit in ams_data or []:
            if not isinstance(ams_unit, dict):
                continue
            try:
                ams_id = int(ams_unit.get("id", 0))
            except (TypeError, ValueError):
                continue
            for tray in ams_unit.get("tray", []) or []:
                if not isinstance(tray, dict):
                    continue
                try:
                    tray_id = int(tray.get("id", 0))
                except (TypeError, ValueError):
                    continue

                key = (printer_id, ams_id, tray_id)
                present = _tray_present(tray)
                prev = _last_presence.get(key)
                _last_presence[key] = present

                if is_first:
                    # First push after a (re)start only seeds the presence map so
                    # a refill done while down doesn't read as a fresh gain.
                    continue

                if not present and prev:
                    # PRESENT→ABSENT: stamp the absence start so a later genuine GAIN
                    # can tell a real physical roll swap (≥ _MIN_PHYSICAL_ABSENT_S)
                    # from a runout-instant state flap (sub-second).
                    _absent_since[key] = time.monotonic()

                if present and not prev:
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
                    # or two edges coalesced into one payload) has UNKNOWN duration,
                    # not a flap — it qualifies. spool_tagless keeps the stricter
                    # measured-only rule below, because minting a spool row on a false
                    # positive is expensive while a suppressed discovery read is not.
                    absent_at = _absent_since.pop(key, None)
                    absent_for = None if absent_at is None else time.monotonic() - absent_at
                    physical_cycle = absent_for is not None and absent_for >= _MIN_PHYSICAL_ABSENT_S

                    # Genuine physical re-insert: clear any feed-fault out-of-
                    # rotation flag. NOT idle-gated (a spool untangled and re-
                    # seated mid-print clears too) and NOT on the first-push seed.
                    # Gated on NOT drying: a drying cycle flaps tray presence
                    # (state → 10) with no physical event, and a jammed spool must
                    # not silently re-enter rotation from a drying flap.
                    # Best-effort — a failure must never break the AMS callback.
                    if not unit_drying(printer_id, ams_id):
                        # Record the change for the identify lanes. Drying-gated with
                        # the rest: a drying cycle disengages trays with no physical
                        # event, and change evidence must mean somebody moved a spool.
                        _note_gain(
                            printer_id,
                            ams_id,
                            tray_id,
                            qualified=absent_for is None or absent_for >= _MIN_PHYSICAL_ABSENT_S,
                        )

                        try:
                            from backend.app.services.spool_recovery import clear_on_reinsert

                            await clear_on_reinsert(db, printer_id, ams_id, tray_id, tray)
                        except Exception:  # noqa: BLE001 — best-effort clear
                            logger.exception(
                                "AMS presence: feed-fault clear failed for printer %d AMS%d-T%d",
                                printer_id,
                                ams_id,
                                tray_id,
                            )

                        # W1/W5: a genuine gain after a QUALIFIED physical absence is a
                        # real roll swap. Record the cycle so the spent-binding latch
                        # releases (branch 3 / bare-tray) + the fresh-roll prompt fires.
                        # Guarded like the clear above; never break the AMS callback.
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

                # The firmware answered for this slot: its identity is current, so an
                # older physical cycle is no longer unanswered evidence and the
                # terminal sweep must not spend a discovery read on it. Stamped on
                # ANY push carrying a valid tag, not just gains — a tag that lands
                # seconds after an insert is exactly the answer we were waiting for.
                if _tray_tagged(tray):
                    note_identity_learned(printer_id, ams_id, tray_id)

                # Steady state: act only on a genuine presence GAIN, and only while
                # the printer is idle. Firing ams_get_rfid during a print is unsafe;
                # the terminal sweep handles mid-print refills. A LOSS only updates
                # the map above (NO auto-unassign). Skip while drying — a drying flap
                # is not a real insert and a re-read would fail the cycle. The need
                # check (an untouched tagless slot must never be read) lives in
                # identify_needed, evaluated with the tray we were just handed.
                #
                # This lane spends DISCOVERY reads only. The other verdict,
                # "rfid_refresh", is a between-prints policy: at a gain the firmware
                # has usually just read the tag itself (which is why the tray already
                # carries one), so commanding a read here would only re-flap a slot
                # whose identity is current. The terminal sweep does that refresh.
                if present and not prev and not running and not unit_drying(printer_id, ams_id):
                    try:
                        # Settings read stays on the gain path only — a physical
                        # insert, not something every status push pays for.
                        reason = await identify_needed(
                            db, printer_id, ams_id, tray_id, tray, await _spoolman_active(db)
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


async def on_printer_terminal(printer_id: int) -> None:
    """Reconcile a printer's AMS slots when a print reaches a terminal state.

    Called from ``main.on_print_complete`` (skipped for eject-job terminals so
    each unit cycle sweeps once at the PRINT terminal, not again at the eject
    terminal). One-shot per RUNNING/PAUSE→terminal transition; sequential, each
    read gated on the client's ``wait_ams_settle`` so identifies never overlap.
    Never raises. Results flow the normal RFID pipeline.

    Eligibility is :func:`identify_needed` and nothing else — tagged slots to refresh
    their ``remain``, physically-changed slots for one discovery read, and NOTHING for
    a slot nobody has touched.
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
