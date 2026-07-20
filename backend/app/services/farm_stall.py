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
from backend.app.services.farm_correlation import WAITING_REASON_PLATE_VISION
from backend.app.services.printer_manager import printer_manager
from backend.app.services.spool_recovery import (
    WAITING_REASON_FAILED,
    WAITING_REASON_RECOVERING,
    WAITING_REASON_RUNOUT,
)

if TYPE_CHECKING:
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
# WAITING_REASON_FAILED / WAITING_REASON_RUNOUT STAY: escalation already fired its
# one-shot operator notification and deliberately left the printer PAUSED for a human
# (a jam that couldn't be recovered, or a filament runout that needs a same-slot
# refill). Re-notifying either through the pause-stall watch would just double up on a
# hold a human already owns.
_ATTENDED_PAUSE_REASONS: frozenset[str] = frozenset(
    {WAITING_REASON_PLATE_VISION, WAITING_REASON_FAILED, WAITING_REASON_RUNOUT}
)

_DEFAULT_GRACE_MINUTES = 30
_DEFAULT_PAUSE_GRACE_MINUTES = 15


def _reset_state() -> None:
    """Test hook: clear the module-level edge state between cases."""
    _first_offline_at.clear()
    _stall_notified.clear()
    _first_paused_at.clear()
    _paused_notified.clear()


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
    from backend.app.services import spool_recovery

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
        owned = item.waiting_reason in _ATTENDED_PAUSE_REASONS or spool_recovery.has_live_recovery(pid)
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
