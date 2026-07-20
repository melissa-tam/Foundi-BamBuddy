"""Mid-run AMS refill recognition (presence + terminal RFID re-read).

Single owner of AMS presence-transition policy. Everything downstream of
``main.on_ams_change`` already handles refills (RFID auto-create/assign, reused-
tag respool, tagless auto-mint/auto-config in ``services.spool_tagless``, low-
spool staged-unit auto-release), but that pipeline only fires when the AMS
change-hash changes. This module closes the gap the hash alone cannot see — a
spool inserted while idle or mid-print that the firmware never auto-reads:

* :func:`on_ams_change` — presence-transition tracking, called from
  ``main.on_ams_change`` inside the existing per-printer lock. On a presence GAIN
  while the printer is idle it fires an immediate per-slot RFID re-read so a Bambu
  spool resolves via the normal tag path within seconds. It NEVER prompts: a
  tagless spool is now silently minted/configured by ``services.spool_tagless``
  (there is no more ``new_spool_detected`` event). A presence LOSS only updates
  the last-presence map — NO silent auto-unassign (a spool pulled for drying keeps
  its assignment and gram history).

* :func:`on_printer_terminal` — the auto RFID re-read sweep, called from
  ``main.on_print_complete`` (skipped for eject-job terminals). When a print ends
  it re-reads every eligible unidentified slot so a mid-print refill is recognized
  within seconds. A slot bound to an auto-minted tagless spool (``data_origin ==
  "ams_auto"``) IS swept — so a Bambu roll swapped into it mid-print gets
  identified at print end — while an operator-bound slot stays protected. Results
  flow the normal RFID pipeline; this module does not duplicate it.

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


def _reset_state() -> None:
    """Test hook: clear all module-level edge state between cases."""
    _last_presence.clear()
    _absent_since.clear()
    _primed.clear()
    _swept_subtasks.clear()
    _echo_pending.clear()


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
    All three commanders (idle gain re-read, terminal sweep, manual refresh) route
    through here so the present-at-command-time guard lives in one place.
    """
    state = printer_manager.get_status(printer_id)
    for ams_unit in _iter_ams_units(state):
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
            if tid != tray_id:
                continue
            if _tray_present(tray):
                _echo_pending[(printer_id, ams_id, tray_id)] = time.monotonic()
            return  # matched the slot (armed or not) — nothing further to scan


def identify_in_flight(printer_id: int, ams_id: int, tray_id: int) -> bool:
    """True while a commanded identify may still be running on this slot, or the
    AMS unit is actively identifying any tray. Read-only (never pops the flag)."""
    state = printer_manager.get_status(printer_id)
    if getattr(state, "ams_status_main", 0) == AMS_STATUS_IDENTIFYING:
        return True
    ts = _echo_pending.get((printer_id, ams_id, tray_id))
    return ts is not None and time.monotonic() - ts < _IDENTIFY_ACTIVE_S


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


# --- Assignment context ----------------------------------------------------


async def _spoolman_active(db: AsyncSession) -> bool:
    from backend.app.api.routes.settings import get_setting

    val = await get_setting(db, "spoolman_enabled")
    return bool(val) and val.lower() == "true"


async def _slot_assignment_context(
    db: AsyncSession, printer_id: int, ams_id: int, tray_id: int, spoolman_active: bool
) -> tuple[bool, bool, str | None]:
    """Resolve (has_assignment, assigned_spool_spent, spool_data_origin) for a slot.

    The internal ``SpoolAssignment`` is the source of truth; when Spoolman mode is
    active a ``SpoolmanSlotAssignment`` also counts as an assignment (Spoolman does
    not track ``spent_at`` or ``data_origin``, so both trailing fields are
    False/None). ``data_origin`` lets the terminal sweep tell an auto-minted
    tagless slot (sweepable) from an operator-bound one (protected).
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
        spent = sa.spool is not None and getattr(sa.spool, "spent_at", None) is not None
        origin = getattr(sa.spool, "data_origin", None) if sa.spool is not None else None
        return True, spent, origin

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
            return True, False, None

    return False, False, None


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
                    absent_at = _absent_since.pop(key, None)
                    physical_cycle = absent_at is not None and (time.monotonic() - absent_at) >= _MIN_PHYSICAL_ABSENT_S

                    # Genuine physical re-insert: clear any feed-fault out-of-
                    # rotation flag. NOT idle-gated (a spool untangled and re-
                    # seated mid-print clears too) and NOT on the first-push seed.
                    # Gated on NOT drying: a drying cycle flaps tray presence
                    # (state → 10) with no physical event, and a jammed spool must
                    # not silently re-enter rotation from a drying flap.
                    # Best-effort — a failure must never break the AMS callback.
                    if not unit_drying(printer_id, ams_id):
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

                # Steady state: act only on a genuine presence GAIN, and only while
                # the printer is idle. Firing ams_get_rfid during a print is unsafe;
                # the terminal sweep handles mid-print refills. A LOSS only updates
                # the map above (NO auto-unassign). Skip while drying — a drying flap
                # is not a real insert and a re-read would fail the cycle.
                if present and not prev and not running and not unit_drying(printer_id, ams_id):
                    # An already-identified tray needs no re-read (re-reading would
                    # only re-flap it); the feed-fault clear above still ran.
                    if is_valid_tag(tray.get("tag_uid", "") or "", tray.get("tray_uuid", "") or ""):
                        continue
                    client = printer_manager.get_client(printer_id)
                    if client is not None:
                        try:
                            ok, _msg = client.ams_refresh_tray(ams_id, tray_id)
                            # Arm the echo flag ONLY on success: a refused command
                            # (filament loaded) runs no identify cycle → no echo →
                            # must not arm.
                            if ok:
                                record_reread(printer_id, ams_id, tray_id)
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
    """Re-read unidentified AMS slots when a print reaches a terminal state.

    Called from ``main.on_print_complete`` (skipped for eject-job terminals so
    each unit cycle sweeps once at the PRINT terminal, not again at the eject
    terminal). One-shot per RUNNING/PAUSE→terminal transition; sequential, each
    read gated on the client's ``wait_ams_settle`` so identifies never overlap.
    Never raises. Results flow the normal RFID pipeline.
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

        eligible: list[tuple[int, int]] = []
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
                        "[Printer %s] terminal RFID re-read: skipping AMS%d — unit is drying",
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
                    # state 9/10/11 eligible — state 9 INCLUDED because a mid-print
                    # refill sometimes stays state=9 until re-read. state 0/None
                    # (unknown dialect / no data) EXCLUDED.
                    if _norm_state(tray.get("state")) not in (9, 10, 11):
                        continue
                    if is_valid_tag(tray.get("tag_uid", "") or "", tray.get("tray_uuid", "") or ""):
                        continue  # already identified — nothing to re-read
                    has_assignment, _spent, origin = await _slot_assignment_context(
                        db, printer_id, ams_id, tray_id, spoolman_active
                    )
                    if has_assignment and origin != "ams_auto":
                        # An operator-bound slot encodes deliberate intent — don't
                        # churn a manual third-party setup. An auto-minted tagless
                        # slot (origin == "ams_auto") IS swept, so a Bambu roll
                        # swapped into it mid-print is identified at print end.
                        continue
                    eligible.append((ams_id, tray_id))

        if not eligible:
            return

        logger.info("[Printer %s] terminal RFID re-read sweep: %d unidentified slot(s)", printer_id, len(eligible))
        for ams_id, tray_id in eligible:
            # Settle-wait FIRST (including before the first read): the client blocks
            # until its AMS is not identifying AND our per-printer identify gate has
            # cleared, so sequential re-reads never overlap. This event-informed pace
            # (poll of the client's own state; the gate-clear runs on the paho thread)
            # replaces the old fixed inter-read spacing loop.
            await client.wait_ams_settle()
            # Skip a slot whose identify is already in flight — a concurrent idle
            # gain re-read on THIS slot armed the echo flag, and commanding a second
            # ams_get_rfid now is the witnessed gain-vs-sweep double command that
            # fails the read. Checked at COMMAND time, not during eligibility
            # collection, so a gain that arms the flag mid-sweep is still caught.
            ts = _echo_pending.get((printer_id, ams_id, tray_id))
            if ts is not None and time.monotonic() - ts < _IDENTIFY_ACTIVE_S:
                logger.debug(
                    "[Printer %s] terminal RFID re-read: skipping AMS%d slot%d — identify already in flight",
                    printer_id,
                    ams_id,
                    tray_id,
                )
                continue
            try:
                ok, _msg = client.ams_refresh_tray(ams_id, tray_id)
                # Arm the echo flag ONLY on success: the identify cycle this
                # command starts flaps the tray present→9→present, and that
                # settle-back gain must be swallowed, not answered with another
                # re-read (the loop this fix kills). A refused command (filament
                # loaded) starts no identify → no echo → nothing to arm.
                if ok:
                    record_reread(printer_id, ams_id, tray_id)
                logger.info("[Printer %s] terminal RFID re-read: AMS%d slot%d", printer_id, ams_id, tray_id)
            except Exception:  # noqa: BLE001 — one failed read must not stop the sweep
                logger.exception("[Printer %s] terminal RFID re-read failed: AMS%d slot%d", printer_id, ams_id, tray_id)
    except Exception:  # noqa: BLE001 — the sweep must never crash the completion callback
        logger.exception("AMS terminal RFID re-read sweep failed for printer %s", printer_id)
