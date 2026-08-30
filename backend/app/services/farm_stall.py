"""Stall watches for farm units stuck in ``printing`` (Phase 3.2 + pause-stall).

Two sibling watches, one module, one scheduler tick. Both record a per-printer
edge timestamp, flag the unit past a grace window, fire a ONE-shot notification,
and NEVER write a terminal status — the item stays ``printing`` and the Phase-1
reconcile / operator resolves the true outcome.

* ``check_stalled_prints`` — the printer went OFFLINE mid-print, so the queue item
  sits at ``printing`` indefinitely, invisible on the run surface (scenario S8).
  Flags ``waiting_reason="printer_offline_stalled"`` and fires ``on_print_stalled``.

* ``check_paused_prints`` — the printer is CONNECTED but PAUSEd (an HMS outside the
  recovery sets, a door-open, an AI-spaghetti pause, a forgotten manual pause) and
  nothing else owns the pause. The 004-H2S incident (2026-07-17) sat PAUSEd ~2h40m
  with no farm reaction because the offline watch only covers offline printers.
  Flags ``waiting_reason="print_paused_stalled"`` and fires ``on_print_paused_stalled``.
  SKIPS a pause already owned by another handler (native-vision plate hold, an
  active/failed spool-recovery, or a live recovery task) so the two features never
  double-notify — and restarts its grace timer when such a pause later becomes
  unattended.

* ``check_dead_dispatch_claims`` — the printer is CONNECTED and demonstrably NOT
  printing, yet a unit still claims it. The one watch here that WRITES the queue
  row's status, and the charter above survives it intact: ``printing → pending`` is
  un-claiming a dispatch that never landed, not fabricating an outcome. Nothing else
  could retire such a row — no terminal echo ever arrives for a print that never
  began (2026-08-29, 001-H2S item 1010: 15 h, seven units queued behind it).

Invoked as guarded calls from the scheduler's ``check_queue`` tick (mirroring the
stagger consumer), so there is no new periodic loop / lifespan task. State (edge
timestamps + notified sets) is module-level, matching the other event-edge
bookkeeping in the fork.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from sqlalchemy import select

from backend.app.core.websocket import broadcast_production_run_changed
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_incident import KIND_JAM, KIND_PHYSICAL, KIND_RUNOUT, STATUS_ESCALATED
from backend.app.services import notify_dedup
from backend.app.services.farm_correlation import WAITING_REASON_PLATE_VISION
from backend.app.services.hms_errors import current_runout_demand
from backend.app.services.plate_occupancy import plate_occupancy
from backend.app.services.printer_manager import printer_manager
from backend.app.services.spool_recovery import (
    RECOVERY_WAITING_REASONS,
    WAITING_REASON_RECOVERING,
    runout_slot_desc,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# printer_id -> monotonic-ish timestamp the printer was FIRST seen offline while a
# unit was printing on it. Cleared on reconnect or when nothing is printing there.
_first_offline_at: dict[int, float] = {}
# printer_ids we've already fired on_print_stalled for this incident (dedup).
_stall_notified: set[int] = set()

# printer_id -> timestamp a printing unit's printer was FIRST seen unattended-PAUSEd.
# Cleared when the pause ends, when another handler owns the pause, or when nothing
# is printing there.
_first_paused_at: dict[int, float] = {}
# printer_ids we've already fired on_print_paused_stalled for this incident (dedup).
_paused_notified: set[int] = set()

# This module's own token (pattern: spool_recovery.py WAITING_REASON_*). The other
# tokens in the skip set are imported from their single origins above.
WAITING_REASON_PAUSED = "print_paused_stalled"

# A PAUSE carrying one of these TOKENS is already owned by another handler — the
# pause-stall watch must not double-flag or double-notify it.
#
# WAITING_REASON_RECOVERING is deliberately NOT here (R1): a spool-recovery pause is
# "owned" only while a LIVE recovery task exists (spool_recovery.has_live_recovery),
# not by the token string. The recovery task's state is process-lifetime in-memory,
# so a server restart mid-recovery orphans the DB token — treating the token alone
# as ownership would leave the printer PAUSEd forever with the watchdog silenced.
# The orphan is instead reclaimed below.
#
# The escalated AMS tokens STAY: the escalation already fired its one-shot operator
# notification and deliberately left the printer PAUSED for a human (a jam that
# couldn't be recovered, a runout needing a same-slot refill, a physical fault needing
# hands). Re-notifying any of them through the pause-stall watch would just double up
# on a hold a human already owns — and since WS2b the OPEN INCIDENT is the primary
# ownership signal (it covers a foreign print, which has no token to read).
#
# DERIVED from spool_recovery's own token set rather than re-listed, so a new hold
# token (the external-spool runout was one) cannot be born un-attended and start
# double-notifying the moment it ships.
_ATTENDED_PAUSE_REASONS: frozenset[str] = frozenset({WAITING_REASON_PLATE_VISION}) | (
    RECOVERY_WAITING_REASONS - {WAITING_REASON_RECOVERING}
)

_DEFAULT_GRACE_MINUTES = 30
_DEFAULT_PAUSE_GRACE_MINUTES = 15

# W3 attention reminders: how long a down printer's ORIGINAL escalation alert may go
# un-repeated before this watch re-fires it. The offline / pause-stall / recovery /
# runout escalations each alert EXACTLY ONCE per incident, so a printer left PAUSEd
# for hours produced a single Discord message (2026-07-20: 009-H2S jam-escalated
# 07:56, 010-H2S vision-paused ~09:08 — both silent until an operator noticed at
# 13:30). A code constant (like _HMS_RENOTIFY_ABSENT_SECONDS), not an operator knob.
_ATTENTION_REMINDER_S = 3600.0

# (printer_id, escalated_reason) -> the ts the reminder loop FIRST saw the condition
# in its remindable form (ACTIVE + CONNECTED + live PAUSE + escalated reason). Key
# presence also marks that the notify_dedup "attention" window has been seeded, so
# the first REMINDER lands one full window later (the first alert stays owned by the
# original escalation path). Cleared the moment the condition lifts.
_attention_first_seen: dict[tuple[int, str], float] = {}


def _reset_state() -> None:
    """Test hook: clear the module-level edge state between cases."""
    _first_offline_at.clear()
    _stall_notified.clear()
    _first_paused_at.clear()
    _paused_notified.clear()
    _attention_first_seen.clear()
    _foreign_paused_at.clear()
    _foreign_notified.clear()
    _dead_claim_since.clear()


async def _grace_seconds(db: AsyncSession, key: str, default: int) -> float:
    """Resolve a stall grace window (seconds) from a settings ``key``. Shared by
    both watches — no parallel resolver."""
    from backend.app.api.routes.settings import get_setting

    raw = await get_setting(db, key)
    try:
        minutes = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        minutes = default
    return max(1, minutes) * 60.0


async def _notify_run_changed(db: AsyncSession, item: PrintQueueItem) -> None:
    """Fire ``production_run_changed`` when the flagged item belongs to a farm run.

    The stall watch covers ALL printing items; only batches with a
    ``sku_file_id`` are production runs, so resolve that before broadcasting —
    the event contract carries a run id, not an arbitrary batch id.
    """
    if item.batch_id is None:
        return
    from backend.app.models.print_batch import PrintBatch

    result = await db.execute(select(PrintBatch.sku_file_id).where(PrintBatch.id == item.batch_id))
    if result.scalar_one_or_none() is not None:
        broadcast_production_run_changed(item.batch_id)


async def _job_name(db: AsyncSession, item: PrintQueueItem) -> str:
    """A human label for the stalled job (archive/library name), best-effort."""
    if item.archive_id is not None:
        from backend.app.models.archive import PrintArchive

        archive = await db.get(PrintArchive, item.archive_id)
        if archive is not None:
            name = archive.print_name or archive.filename
            if name:
                return name
    if item.library_file_id is not None:
        from backend.app.models.library import LibraryFile

        lib = await db.get(LibraryFile, item.library_file_id)
        if lib is not None and lib.filename:
            return lib.filename
    return f"item {item.id}"


async def check_stalled_prints(db: AsyncSession, *, manager=printer_manager, now: float | None = None) -> None:
    """Flag farm units whose printer has been offline past the grace window.

    For every queue item in ``printing`` status with a ``printer_id``:
      * printer CONNECTED  → clear the offline edge state and, if the item still
        carries the stall ``waiting_reason``, clear it (the reconcile will resolve
        the true outcome);
      * printer OFFLINE     → record the first-offline instant; once it has stayed
        offline ``farm_offline_stall_minutes`` → set
        ``waiting_reason="printer_offline_stalled"`` and fire ``on_print_stalled``
        exactly ONCE per incident.

    Never writes a terminal status. Injectable ``manager``/``now`` for tests.
    """
    now = time.time() if now is None else now
    grace_s = await _grace_seconds(db, "farm_offline_stall_minutes", _DEFAULT_GRACE_MINUTES)

    result = await db.execute(
        select(PrintQueueItem).where(PrintQueueItem.status == "printing").where(PrintQueueItem.printer_id.is_not(None))
    )
    items = list(result.scalars().all())
    seen_printers: set[int] = set()
    dirty = False

    for item in items:
        pid = item.printer_id
        if pid is None:
            continue
        seen_printers.add(pid)

        if manager.is_connected(pid):
            _first_offline_at.pop(pid, None)
            _stall_notified.discard(pid)
            if item.waiting_reason == "printer_offline_stalled":
                item.waiting_reason = None
                dirty = True
                await _notify_run_changed(db, item)
            continue

        first = _first_offline_at.get(pid)
        if first is None:
            _first_offline_at[pid] = now
            continue
        if now - first < grace_s or pid in _stall_notified:
            continue

        # Past grace, first time this incident → flag + notify once.
        item.waiting_reason = "printer_offline_stalled"
        _stall_notified.add(pid)
        dirty = True
        await _notify_run_changed(db, item)
        minutes = int((now - first) // 60)
        try:
            from backend.app.models.printer import Printer
            from backend.app.services.notification_service import notification_service

            printer = await db.get(Printer, pid)
            printer_name = printer.name if printer is not None else f"printer {pid}"
            job_name = await _job_name(db, item)
            await notification_service.on_print_stalled(pid, printer_name, job_name, minutes, db)
        except Exception:  # noqa: BLE001 — a notify failure must not abort the watch
            logger.exception("farm_stall: on_print_stalled notification failed for printer %s", pid)
        logger.warning(
            "farm_stall: printer %s offline %d min with unit %s still printing — flagged (not terminated)",
            pid,
            minutes,
            item.id,
        )

    # Drop edge state for printers that are no longer printing anything (so a NEW
    # print later re-arms the grace timer from scratch).
    for pid in list(_first_offline_at.keys()):
        if pid not in seen_printers:
            _first_offline_at.pop(pid, None)
            _stall_notified.discard(pid)

    if dirty:
        await db.commit()


async def check_paused_prints(db: AsyncSession, *, manager=printer_manager, now: float | None = None) -> None:
    """Flag farm units whose CONNECTED printer has sat unattended-PAUSEd past grace.

    For every queue item in ``printing`` status with a ``printer_id``:
      * printer OFFLINE      → drop the pause edge state and skip (the offline watch
        owns it — a paused-then-offline printer must not be double-counted here);
      * live state != PAUSE  → drop the pause edge state and clear a stale
        ``print_paused_stalled`` flag (the pause ended, resumed, or the state read
        raced to ``None`` at startup — read as not-PAUSE);
      * orphaned ``spool_jam_recovering`` token with NO live recovery task (the
        owning task died with a server restart/crash) → clear the token to None with
        a WARNING and notify run-changed, REGARDLESS of the printer's current state
        (an orphan on a RUNNING printer must not sit in the UI forever), then let the
        normal unattended-pause flow below run for a still-PAUSEd printer;
      * PAUSE already owned by another handler (native-vision plate hold, an already-
        escalated spool-recovery FAILED, or a LIVE recovery task) → drop the edge
        timer so grace RESTARTS from the moment the pause becomes unattended, and
        skip (no double-notify);
      * PAUSE, unattended, past ``farm_pause_stall_minutes`` → set
        ``waiting_reason="print_paused_stalled"`` and fire ``on_print_paused_stalled``
        exactly ONCE per incident.

    Never writes a terminal status. Injectable ``manager``/``now`` for tests.
    """
    now = time.time() if now is None else now
    grace_s = await _grace_seconds(db, "farm_pause_stall_minutes", _DEFAULT_PAUSE_GRACE_MINUTES)
    # Local import (matches the fork's cycle-avoidance convention here) — the sole
    # ownership signal for a spool-recovery pause is a LIVE task, not the token.
    from backend.app.services import printer_incidents, spool_recovery

    # Printers whose pause an AMS incident already owns. Read ONCE per tick (the
    # durable successor of "the item carries an escalated token": it also covers a
    # foreign print, which has no queue row to carry one).
    incident_printers = {inc.printer_id for inc in await printer_incidents.all_open(db)}

    result = await db.execute(
        select(PrintQueueItem).where(PrintQueueItem.status == "printing").where(PrintQueueItem.printer_id.is_not(None))
    )
    items = list(result.scalars().all())
    seen_printers: set[int] = set()
    dirty = False

    for item in items:
        pid = item.printer_id
        if pid is None:
            continue
        seen_printers.add(pid)

        # Offline printers belong to the offline watch — drop our edge and move on.
        if not manager.is_connected(pid):
            _first_paused_at.pop(pid, None)
            _paused_notified.discard(pid)
            continue

        # Orphaned recovery token (R1): a ``spool_jam_recovering`` token with NO live
        # recovery task means the owning task died with a server restart/crash. Clear
        # it REGARDLESS of the printer's current state (an orphan on a RUNNING printer
        # must not sit in the UI forever), then fall through so a still-PAUSEd printer
        # re-enters the normal unattended-pause grace flow below (operator notified
        # after farm_pause_stall_minutes).
        if item.waiting_reason == WAITING_REASON_RECOVERING and not spool_recovery.has_live_recovery(pid):
            item.waiting_reason = None
            dirty = True
            logger.warning(
                "farm_stall: printer %s carried orphaned '%s' token with no live recovery task "
                "(restart/crash) — cleared; unattended-pause watch resumes ownership",
                pid,
                WAITING_REASON_RECOVERING,
            )
            await _notify_run_changed(db, item)

        st = manager.get_status(pid)
        if getattr(st, "state", None) != "PAUSE":
            # Not paused (incl. a startup-race ``None`` read): drop the edge and a
            # stale pause flag so the run surface stops showing the hold.
            _first_paused_at.pop(pid, None)
            _paused_notified.discard(pid)
            if item.waiting_reason == WAITING_REASON_PAUSED:
                item.waiting_reason = None
                dirty = True
                await _notify_run_changed(db, item)
            continue

        # PAUSE owned by another handler? Restart the grace timer so it counts only
        # unattended pause time, and skip (that handler / its own notification owns
        # this pause). ONE definition: a muted token (plate-vision / already-escalated
        # FAILED) OR a LIVE recovery task. RECOVERING is proven only by the live task,
        # never the token (an orphaned token was already reclaimed above).
        owned = (
            item.waiting_reason in _ATTENDED_PAUSE_REASONS
            or spool_recovery.has_live_recovery(pid)
            or pid in incident_printers
        )
        if owned:
            _first_paused_at.pop(pid, None)
            _paused_notified.discard(pid)
            continue

        first = _first_paused_at.get(pid)
        if first is None:
            _first_paused_at[pid] = now
            continue
        if now - first < grace_s or pid in _paused_notified:
            continue

        # Past grace, first time this incident → flag + notify once.
        item.waiting_reason = WAITING_REASON_PAUSED
        _paused_notified.add(pid)
        dirty = True
        await _notify_run_changed(db, item)
        minutes = int((now - first) // 60)
        try:
            from backend.app.models.printer import Printer
            from backend.app.services.notification_service import notification_service

            printer = await db.get(Printer, pid)
            printer_name = printer.name if printer is not None else f"printer {pid}"
            job_name = await _job_name(db, item)
            await notification_service.on_print_paused_stalled(pid, printer_name, job_name, minutes, db)
        except Exception:  # noqa: BLE001 — a notify failure must not abort the watch
            logger.exception("farm_stall: on_print_paused_stalled notification failed for printer %s", pid)
        logger.warning(
            "farm_stall: printer %s PAUSEd unattended %d min with unit %s still printing — flagged (not terminated)",
            pid,
            minutes,
            item.id,
        )

    # Drop edge state for printers that are no longer printing anything (so a NEW
    # print later re-arms the grace timer from scratch).
    for pid in list(_first_paused_at.keys()):
        if pid not in seen_printers:
            _first_paused_at.pop(pid, None)
            _paused_notified.discard(pid)

    if dirty:
        await db.commit()


# --------------------------------------------------------------------------- #
# The dead dispatch claim: a ``printing`` row whose print never started
# --------------------------------------------------------------------------- #
# item_id -> the ts every dead-claim guard was FIRST seen holding together. Popped
# the moment any guard breaks, and pruned against the live ``printing`` set, so the
# dwell measures one continuous dead shape rather than an accumulation of glimpses.
#
# Process-lifetime (derive-don't-store): a restart re-derives the whole shape from
# the DB row plus the live wire on the next tick, and the dwell simply restarts —
# the safe direction, worst cost one extra 120 s before a stranded unit is freed.
_dead_claim_since: dict[int, float] = {}

# Clock A: how old a claim must be before it can be called dead. Comfortably past
# the dispatch watchdog's own full budget (90 s Phase A + 180 s Phase B = 270 s) plus
# the slowest observed H2D digestion, so the watchdog is ALWAYS the first responder
# and this watch only ever sees what it missed.
_DEAD_CLAIM_MIN_AGE_S = 600.0
# Clock B: how long the dead shape must hold continuously. A printer's live state and
# subtask echo both flap around a dispatch; one poll is not evidence.
_DEAD_CLAIM_DWELL_S = 120.0


async def check_dead_dispatch_claims(db: AsyncSession, *, manager=printer_manager, now: float | None = None) -> None:
    """Release a ``printing`` claim whose print demonstrably never started.

    ``printing`` is written optimistically: the dispatcher commits it BEFORE the
    print command goes out, so that a crash in between leaves a unit wrongly marked
    printing rather than one that silently reprints hours later. The cost of that
    (correct) choice is a row that can outlive the dispatch it describes — and
    nothing on the farm could ever retire one. No terminal echo will arrive for a
    print that never began; the downtime reconcile is archive-scoped and an archive
    row only reaches ``printing`` at PRINT START; the dispatch watchdog releases its
    hold on any transition into an active state, PAUSE included. Production
    2026-08-29 on 001-H2S: item 1010 was dispatched into a standing AMS fault at
    01:25:13, the print never started, and the row seeded ``busy_printers`` every
    30 s for 15 hours with seven pending units queued behind it.

    Six guards, ALL required, because the failure mode of a wrong release is a
    DOUBLE DISPATCH onto an occupied plate:

    1. the printer is CONNECTED (an offline printer belongs to
       :func:`check_stalled_prints`, which owns that story and its own token);
    2. its live state exists and is NOT in
       ``print_scheduler.ACTIVE_PRINT_STATES`` — one origin, imported at call time.
       PAUSE counts as ACTIVE on purpose: a native-vision trip pauses at print start
       with the plate occupied, and releasing that unit would re-dispatch onto it;
    3. the print never started, EVIDENCE-LED: no ``PrintArchive`` on this printer
       reads ``printing`` (hard disjointness with ``main.reconcile_stale_active_prints``
       — the two reconcilers can never both act on one printer), and when the item
       carries a ``dispatch_subtask_id``, the printer's live ``subtask_id`` differs
       from it. That id test is CORROBORATION, never a precondition: a NULL id proves
       nothing either way, and requiring one would strand exactly the rows that need
       this most. A live subtask that MATCHES our dispatch id, on the other hand, is
       proof the print landed — the watch stands down;
    4. the claim is at least :data:`_DEAD_CLAIM_MIN_AGE_S` old, measured from
       ``started_at`` (naive stamps read as UTC, as ``stagger`` does). A claim with no
       ``started_at`` at all is left alone: without it the age is unknowable, and an
       unknowable age is not evidence;
    5. the dead shape has held for :data:`_DEAD_CLAIM_DWELL_S`;
    6. nobody else owns the printer — no OPEN incident and no LIVE recovery task.
       This is why the wire-clear sweep runs first in the tick: it can only ever
       DELAY a release, never cause a wrong one.

    The module's "never writes a terminal status" charter is intact — ``pending`` is
    un-claiming, not an outcome. The unit goes back where it came from and the
    scheduler re-dispatches it, so there is no new notification event: the
    run-changed broadcast and the WARNING below are the operator surface.

    Injectable ``manager``/``now`` (epoch seconds — it drives both clocks) for tests.
    """
    now = time.time() if now is None else now
    from datetime import datetime, timezone

    from backend.app.models.archive import PrintArchive
    from backend.app.services import printer_incidents, spool_recovery
    from backend.app.services.print_scheduler import ACTIVE_PRINT_STATES
    from backend.app.services.queue_transitions import release_unstarted_claim

    wall = datetime.fromtimestamp(now, tz=timezone.utc)

    result = await db.execute(
        select(PrintQueueItem).where(PrintQueueItem.status == "printing").where(PrintQueueItem.printer_id.is_not(None))
    )
    items = list(result.scalars().all())
    live_ids = {item.id for item in items}
    for stale in [iid for iid in _dead_claim_since if iid not in live_ids]:
        _dead_claim_since.pop(stale, None)
    if not items:
        return

    # Guard 3's disjointness half, read ONCE per tick: which printers the archive
    # side believes are mid-print.
    archive_printers = {
        pid
        for (pid,) in (
            await db.execute(
                select(PrintArchive.printer_id)
                .where(PrintArchive.status == "printing")
                .where(PrintArchive.printer_id.is_not(None))
            )
        ).all()
    }

    for item in items:
        pid = item.printer_id
        if pid is None:
            continue
        try:
            if not manager.is_connected(pid) or item.waiting_reason == "printer_offline_stalled":
                _dead_claim_since.pop(item.id, None)
                continue

            st = manager.get_status(pid)
            live = (getattr(st, "state", None) or "").upper()
            if not live or live in ACTIVE_PRINT_STATES:
                _dead_claim_since.pop(item.id, None)
                continue

            if pid in archive_printers:
                _dead_claim_since.pop(item.id, None)
                continue

            dispatch_id = (item.dispatch_subtask_id or "").strip()
            live_subtask = (getattr(st, "subtask_id", None) or "").strip()
            if dispatch_id and live_subtask and live_subtask == dispatch_id:
                _dead_claim_since.pop(item.id, None)
                continue

            started_at = item.started_at
            if started_at is None:
                _dead_claim_since.pop(item.id, None)
                continue
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=timezone.utc)
            age_s = (wall - started_at).total_seconds()
            if age_s < _DEAD_CLAIM_MIN_AGE_S:
                _dead_claim_since.pop(item.id, None)
                continue

            if await printer_incidents.get_open(db, pid) is not None or spool_recovery.has_live_recovery(pid):
                _dead_claim_since.pop(item.id, None)
                continue

            first = _dead_claim_since.get(item.id)
            if first is None:
                _dead_claim_since[item.id] = now
                continue
            if now - first < _DEAD_CLAIM_DWELL_S:
                continue

            if not await release_unstarted_claim(db, item_id=item.id):
                # It moved between the query and the write — somebody else owns it.
                _dead_claim_since.pop(item.id, None)
                continue
            await db.commit()
            # The row claim and the PRINTER claim are two different things with two
            # different writers, and un-making a dispatch means dropping both: the
            # conditional UPDATE above releases the queue row, this releases the
            # occupancy lease that would otherwise hold the printer out of the queue.
            plate_occupancy.release_dispatch(pid, "dead dispatch claim")
            _dead_claim_since.pop(item.id, None)
            await _notify_run_changed(db, item)
            logger.warning(
                "farm_stall: printer %s unit %s claimed 'printing' %.0f min ago but never started "
                "(state=%s, dispatch subtask=%s, live subtask=%s) — released to 'pending' for re-dispatch",
                pid,
                item.id,
                age_s / 60.0,
                live,
                dispatch_id or "-",
                live_subtask or "-",
            )
        except Exception:  # noqa: BLE001 — one bad item must not abort the watch
            logger.exception("farm_stall: dead-claim watch failed for printer %s item %s", pid, item.id)


# --------------------------------------------------------------------------- #
# WS2b: the foreign-print pause watch
# --------------------------------------------------------------------------- #
# printer_id -> the ts a FOREIGN print was first seen PAUSEd (episode start), and
# the printers already notified for the current episode. An episode is one
# continuous PAUSE: leaving PAUSE clears both, so the next pause nags afresh.
_foreign_paused_at: dict[int, float] = {}
_foreign_notified: set[int] = set()


async def check_foreign_paused_printers(db: AsyncSession, *, manager=printer_manager, now: float | None = None) -> None:
    """One-shot alert for a print the farm did NOT dispatch, left PAUSEd past grace.

    Every other watch in this module starts from a ``printing`` farm queue item, so
    the whole of the fleet's foreign work — a Bambu Studio LAN print, a screen
    restart, a re-run off the USB — was invisible to all of them: a vision trip or a
    door-open on a foreign print sat until a human happened to look. The AMS-incident
    lane covers the faults it can classify; this is the catch-all behind everything
    else, and it deliberately says only what is true (something stopped, nothing is
    recovering it) rather than promising a farm reaction.

    Fires when ALL hold: the printer is CONNECTED and in live PAUSE, NO farm unit is
    printing on it, NO AMS incident is open for it (that hold has its own alert and
    its own hourly nag), and the pause has lasted past ``farm_pause_stall_minutes``.
    One notification per pause EPISODE. Mutates nothing.
    """
    now = time.time() if now is None else now
    grace_s = await _grace_seconds(db, "farm_pause_stall_minutes", _DEFAULT_PAUSE_GRACE_MINUTES)
    from backend.app.models.printer import Printer
    from backend.app.services import printer_incidents

    incident_printers = {inc.printer_id for inc in await printer_incidents.all_open(db)}
    farm_busy = {
        pid
        for (pid,) in (
            await db.execute(
                select(PrintQueueItem.printer_id)
                .where(PrintQueueItem.status == "printing")
                .where(PrintQueueItem.printer_id.is_not(None))
            )
        ).all()
    }

    printers = list((await db.execute(select(Printer))).scalars().all())
    seen: set[int] = set()
    for printer in printers:
        pid = printer.id
        try:
            if not manager.is_connected(pid):
                continue
            st = manager.get_status(pid)
            if getattr(st, "state", None) != "PAUSE":
                continue
            seen.add(pid)
            if pid in farm_busy or pid in incident_printers:
                # Owned elsewhere: restart the episode clock so this watch only ever
                # counts time the pause was genuinely unowned.
                _foreign_paused_at.pop(pid, None)
                _foreign_notified.discard(pid)
                seen.discard(pid)
                continue

            first = _foreign_paused_at.get(pid)
            if first is None:
                _foreign_paused_at[pid] = now
                continue
            if now - first < grace_s or pid in _foreign_notified:
                continue

            _foreign_notified.add(pid)
            minutes = int((now - first) // 60)
            job_name = (getattr(st, "subtask_name", None) or "").strip() or "an unknown print"
            from backend.app.services.notification_service import notification_service

            await notification_service.on_foreign_print_paused(pid, printer.name, job_name, minutes, db)
            logger.warning(
                "farm_stall: printer %s PAUSEd %d min on FOREIGN print '%s' with nothing owning it — operator alerted",
                pid,
                minutes,
                job_name,
            )
        except Exception:  # noqa: BLE001 — one bad printer must not abort the watch
            logger.exception("farm_stall: foreign-pause watch failed for printer %s", pid)

    # Episode reset: any printer no longer in an unowned PAUSE starts over.
    for pid in list(_foreign_paused_at.keys()):
        if pid not in seen:
            _foreign_paused_at.pop(pid, None)
            _foreign_notified.discard(pid)


# --------------------------------------------------------------------------- #
# W3: hourly attention reminders for a printer left down needing a human
# --------------------------------------------------------------------------- #
# Reminder copy — a re-fire is the SAME notification EVENT the original escalation
# produced (no new event types / templates / channels), so the operator sees a
# familiar alert; "STILL" frames it as a nag, not a fresh incident.
_JAM_REMINDER_DETAIL = "Spool jam STILL not recovered — the printer is still PAUSED and needs a human."
_RUNOUT_REMINDER_DETAIL = (
    "Filament runout STILL not resolved — the printer is still PAUSED awaiting a same-slot refill."
)
_PLATE_VISION_REMINDER_DETAIL = (
    "Printer vision STILL reports foreign objects on the heatbed — the job is still PAUSED. "
    "Clear the bed, then Resume on the printer screen."
)


async def _remind_paused(db, notif, printer_id, printer_name, job_name, minutes, *, state=None) -> None:
    """Re-fire the pause-stall escalation's own event (WAITING_REASON_PAUSED)."""
    await notif.on_print_paused_stalled(printer_id, printer_name, job_name, minutes, db)


_PHYSICAL_REMINDER_DETAIL = (
    "A physical filament fault is STILL unresolved — the printer is still PAUSED and no swap can clear it."
)

# incident kind -> the reminder's detail copy. One line per kind, so the nag reads
# like the alert it repeats.
_INCIDENT_REMINDER_DETAIL: dict[str, str] = {
    KIND_JAM: _JAM_REMINDER_DETAIL,
    KIND_RUNOUT: _RUNOUT_REMINDER_DETAIL,
    KIND_PHYSICAL: _PHYSICAL_REMINDER_DETAIL,
}

# The same three holds when the printer is NOT paused. Every line above asserts
# "still PAUSED", which is simply false for the shape that motivated this arm: a
# printer sitting IDLE, physically clean or not, still held by an escalated incident
# and taking no work. Saying "still PAUSED" there would send an operator looking for
# a paused job that does not exist — so the non-PAUSE copy states what IS true (a
# standing hold, no work being taken) and what ends it (the fault clearing).
_IDLE_HELD_SUFFIX = " The printer is not paused — it is idle and will take no work until the fault clears on the wire."
_INCIDENT_REMINDER_DETAIL_UNPAUSED: dict[str, str] = {
    KIND_JAM: "Spool jam STILL not recovered — the printer remains held for a human." + _IDLE_HELD_SUFFIX,
    KIND_RUNOUT: (
        "Filament runout STILL not resolved — the printer remains held awaiting a same-slot refill." + _IDLE_HELD_SUFFIX
    ),
    KIND_PHYSICAL: (
        "A physical filament fault is STILL unresolved — the printer remains held and no swap can clear it."
        + _IDLE_HELD_SUFFIX
    ),
}


def _live_runout_slot(state) -> str | None:
    """Human name of the slot the firmware is CURRENTLY demanding filament in, from
    the live printer state — or ``None`` when it names none. Formatting comes from
    ``spool_recovery.runout_slot_desc`` (one origin for the wording)."""
    demand = current_runout_demand(getattr(state, "hms_errors", None) or [])
    if demand is None:
        return None
    return runout_slot_desc(demand[0] * 4 + demand[1])


async def _remind_plate_vision(db, notif, printer_id, printer_name, job_name, minutes, *, state=None) -> None:
    """Re-fire the native-vision plate hold's own event (WAITING_REASON_PLATE_VISION)."""
    await notif.on_plate_not_empty(printer_id, printer_name, db, source_detail=_PLATE_VISION_REMINDER_DETAIL)


# reason -> the callable that RE-FIRES that reason's original notification event.
# This dict IS the single source of the remindable reason set (_ATTENTION_REASONS
# below), so adding a reason is a one-line edit that cannot drift from the pin.
#
# The AMS holds (jam / runout / physical) are deliberately ABSENT: since WS2b they
# are reminded from their OPEN ESCALATED INCIDENT (:func:`_remind_open_incidents`),
# not from a queue item's token. A token can only exist for a farm print, so the
# token lane could never nag about a foreign print left holding — which is exactly
# the class of hold that sat silent for hours.
_ATTENTION_DISPATCH: dict[str, Callable[..., Awaitable[None]]] = {
    WAITING_REASON_PAUSED: _remind_paused,
    WAITING_REASON_PLATE_VISION: _remind_plate_vision,
}
# The ESCALATED waiting_reason tokens a down-printer reminder re-fires for. Each of
# these was already alerted ONCE by the code that set it and then left the printer
# PAUSED for a human — the reminder nags hourly while the hold persists.
_ATTENTION_REASONS: frozenset[str] = frozenset(_ATTENTION_DISPATCH)


async def _remind_open_incidents(
    db: AsyncSession,
    notif,
    *,
    manager,
    now: float,
    held_keys: set[tuple[int, str]],
) -> None:
    """Hourly nag for every OPEN ESCALATED AMS incident still holding its printer.

    Printer-scoped by design (WS2b): an incident is a fact about the PRINTER, so this
    arm reminds identically whether the held print is a farm unit or a foreign one —
    the token-driven arm it replaces could only ever see farm units, and a foreign
    hold nagged nobody. Re-fires the escalation's OWN event with kind-aware copy; no
    new event types.

    The runout slot is re-decoded LIVE each tick (006-H2S 2026-07-26) so the nag
    SELF-CORRECTS when the firmware's demand moves, rather than repeating the slot
    that was right an hour ago.

    **A hold does not have to be PAUSED to be a hold (2026-08-29).** This arm used to
    skip any printer not reading ``PAUSE``, which silently exempted the worst shape
    there is: 001-H2S incident #60 held an IDLE printer for 15 h with seven pending
    units behind it and fired ZERO notifications — it was found by eye. The evidence
    rule is now the same one the rearm uses: skip only when the printer is
    DISCONNECTED or reports no state at all (``""`` / ``UNKNOWN``), because those are
    absences of evidence rather than readings. Everything else nags, and with the
    wire-clear sweep landing first, what survives to be nagged in a non-PAUSE state
    is exactly a printer whose fault is still live (or one inside the sweep's 120 s
    dwell, which the first-sighting seed absorbs).
    """
    from backend.app.models.printer import Printer
    from backend.app.services import printer_incidents

    for incident in await printer_incidents.all_open(db):
        if incident.status != STATUS_ESCALATED:
            continue  # 'recovering' is the machine acting — not a hold to nag about
        pid = incident.printer_id
        key = (pid, f"incident:{incident.id}")
        try:
            if not manager.is_connected(pid):
                continue
            st = manager.get_status(pid)
            live = (getattr(st, "state", None) or "").upper()
            if not live or live == "UNKNOWN":
                continue
            held_keys.add(key)

            akey = f"{pid}:incident:{incident.id}"
            first = _attention_first_seen.get(key)
            if first is None:
                # First remindable sighting: seed the window so the first reminder
                # lands one full window later (the escalation delivered the first).
                _attention_first_seen[key] = now
                notify_dedup.allow("attention", akey, now, _ATTENTION_REMINDER_S)
                continue
            if not notify_dedup.allow("attention", akey, now, _ATTENTION_REMINDER_S):
                continue

            minutes = int((now - first) // 60)
            printer = await db.get(Printer, pid)
            printer_name = printer.name if printer is not None else f"printer {pid}"
            job_name = (getattr(st, "subtask_name", None) or "").strip() or "print"
            slot = None
            if incident.kind == KIND_RUNOUT:
                slot = _live_runout_slot(st) or runout_slot_desc(incident.slot_global_tray)
            elif incident.slot_global_tray is not None:
                slot = runout_slot_desc(incident.slot_global_tray)
            copy = _INCIDENT_REMINDER_DETAIL if live == "PAUSE" else _INCIDENT_REMINDER_DETAIL_UNPAUSED
            await notif.on_spool_recovery_failed(
                printer_id=pid,
                printer_name=printer_name,
                job_name=job_name,
                detail=copy.get(incident.kind, _JAM_REMINDER_DETAIL),
                db=db,
                kind=incident.kind,
                runout_slot=slot,
                foreign=incident.item_id is None,
            )
            logger.warning(
                "farm_stall: printer %s STILL held by %s incident %s (%d min, state=%s%s) — "
                "attention reminder re-fired",
                pid,
                incident.kind,
                incident.id,
                minutes,
                live,
                "" if incident.item_id is not None else ", foreign print",
            )
        except Exception:  # noqa: BLE001 — one bad printer must not abort the watch
            logger.exception("farm_stall: incident attention reminder failed for printer %s", pid)


async def check_attention_reminders(db: AsyncSession, *, manager=printer_manager, now: float | None = None) -> None:
    """Re-fire the ORIGINAL escalation notification for a printer left down (W3).

    The offline / pause-stall / spool-recovery / runout escalations each alert
    EXACTLY ONCE per incident and then leave the printer PAUSED for a human, so a
    hold that a human doesn't clear for hours produced a single Discord message —
    the 2026-07-20 incident sat 5+ h that way on two printers. This watch nags,
    through TWO arms that never overlap:

    * every OPEN ESCALATED AMS incident (:func:`_remind_open_incidents`) — printer
      scoped, so a FOREIGN print's hold nags exactly like a farm one. Before WS2b
      this arm read farm queue tokens, and a foreign hold had none to read;
    * every still-``printing`` farm unit carrying one of the remaining non-AMS
      ESCALATED tokens in :data:`_ATTENTION_REASONS` (plate-vision, pause-stall),
      re-firing THAT reason's own notification event via :data:`_ATTENTION_DISPATCH`
      — no new event types.

    Each nags once per :data:`_ATTENTION_REMINDER_S` window until the hold lifts.

    Cadence: the first alert stays owned by the original escalation path, which
    never touches the ``"attention"`` :func:`notify_dedup.allow` scope. So the loop
    SEEDS the window the first tick it sees the condition and the first REMINDER
    lands one full window later — then once per window while still held. Tracking
    resets the moment the condition lifts (pause ends, the unit is no longer
    ``printing``, or the reason changes) so a future incident nags afresh.

    Never writes a terminal status and mutates no queue item — a reminder is purely
    a re-notification. Per-printer guarded (one bad printer must not kill the tick).
    Runs on the same scheduler tick as the two watchdogs. Injectable
    ``manager``/``now`` for tests.
    """
    now = time.time() if now is None else now
    from backend.app.services.notification_service import notification_service

    result = await db.execute(
        select(PrintQueueItem).where(PrintQueueItem.status == "printing").where(PrintQueueItem.printer_id.is_not(None))
    )
    # (printer_id, reason) pairs still in the remindable condition THIS tick. Only
    # these are retained below; every other tracked key is reset. The incident arm
    # shares the ledger, keyed ``incident:{id}`` — one reset pass, one contract.
    held_keys: set[tuple[int, str]] = set()
    await _remind_open_incidents(db, notification_service, manager=manager, now=now, held_keys=held_keys)

    for item in list(result.scalars().all()):
        pid = item.printer_id
        reason = item.waiting_reason
        if pid is None or reason not in _ATTENTION_REASONS:
            continue
        key = (pid, reason)
        try:
            # Remindable only while CONNECTED and in live PAUSE. An OFFLINE printer
            # belongs to the offline watch; a RESUMED one (or a startup-race None
            # read) is no longer held. A key not remindable this tick is left out of
            # held_keys, so the reset pass drops it — matching "pause ends / not
            # printing / reason changed → reset".
            if not manager.is_connected(pid):
                continue
            st = manager.get_status(pid)
            if getattr(st, "state", None) != "PAUSE":
                continue
            held_keys.add(key)

            akey = f"{pid}:{reason}"
            first = _attention_first_seen.get(key)
            if first is None:
                # First remindable sighting: record it and SEED the allow() window so
                # the first reminder fires ONE window later, not now (the original
                # escalation already delivered the first alert).
                _attention_first_seen[key] = now
                notify_dedup.allow("attention", akey, now, _ATTENTION_REMINDER_S)
                continue
            if not notify_dedup.allow("attention", akey, now, _ATTENTION_REMINDER_S):
                continue

            minutes = int((now - first) // 60)
            from backend.app.models.printer import Printer

            printer = await db.get(Printer, pid)
            printer_name = printer.name if printer is not None else f"printer {pid}"
            job_name = await _job_name(db, item)
            # ``st`` is the live status this tick already read above — passed, never
            # re-fetched, so the runout reminder can name the CURRENTLY demanded slot.
            await _ATTENTION_DISPATCH[reason](db, notification_service, pid, printer_name, job_name, minutes, state=st)
            logger.warning(
                "farm_stall: printer %s STILL held (%s) %d min with unit %s printing — attention reminder re-fired",
                pid,
                reason,
                minutes,
                item.id,
            )
        except Exception:  # noqa: BLE001 — one bad printer must not abort the watch
            logger.exception("farm_stall: attention reminder failed for printer %s", pid)

    # Reset tracking for any (printer, reason) no longer in the remindable condition
    # so a future incident seeds + reminds from scratch.
    for key in list(_attention_first_seen.keys()):
        if key not in held_keys:
            _attention_first_seen.pop(key, None)
