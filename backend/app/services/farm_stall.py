"""Stall watches for farm units stuck in ``printing`` (offline stall + pause stall).

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

* ``check_unowned_ejects`` — a pending eject whose runtime verdict fired and whose
  terminal never arrived, on a printer that never dropped its session (so the printer's
  session start can never see it): the eject reconciler is re-driven. (Its former sibling,
  the dead dispatch-claim release, is gone: a claim whose print never started is decided by
  its start's own answer on the record — ``services/dispatch_start`` — never by an age and a
  dwell, 2026-10-11.)

* ``check_waiting_on_printers`` — THE attention owner (owner 5): it pages a person about a wait on
  the printer, once and then hourly, and never resolves, escalates or acts — every page bound lives
  here (``TestAttentionOwnership``). It pages a command the printer has not carried out (the record's
  unanswered actions, aged in ONLINE seconds), a printer RUNNING inside a firmware change or with
  nothing fed, an unreadable feed, a dispatch held on a stalled upload, a cooldown running long and a
  plate only a person may clear. An action whose self-heal bound passed is SKIPPED — its lane took
  production's fallback and pages its own hand-over (2026-10-12: "if it was self heal, it should stay
  self heal") — except a farm motion command still pending on its row, which nothing else pages.

* ``check_ams_wedged_idle`` — the printer is CONNECTED, IDLE and taking no work
  because its AMS is parked mid filament-change (``ams_status_main == 1``): the
  dispatcher refuses this printer while the AMS is mid-change, and the farm has not
  commanded anything here. Nothing else can see it: no print, no incident row, no
  HMS code once the jam that caused it clears (002-H2S 2026-09-11). One WARNING +
  one ``on_ams_wedged_idle`` page per episode. What a load or unload does in this
  posture is measured per command by ``command_outcome.classify`` (``[ams-command] … answer=``).

Invoked as guarded calls from the scheduler's ``check_queue`` tick (mirroring the
stagger consumer), so there is no new periodic loop / lifespan task. State (edge
timestamps + notified sets) is module-level, matching the other event-edge
bookkeeping in the fork.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import select

from backend.app.core.websocket import broadcast_production_run_changed
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_incident import (
    AMS_FAULT_KINDS,
    KIND_JAM,
    KIND_PHYSICAL,
    KIND_PLATE_VISION,
    KIND_POWER_LOSS,
    KIND_RUNOUT,
    KIND_TOOLHEAD_REFILL,
    KIND_Z_REFERENCE_LOST,
    STATUS_ESCALATED,
)
from backend.app.services import notify_dedup
from backend.app.services.hms_errors import current_runout_demand
from backend.app.services.plate_occupancy import ACTIVE_PRINT_STATES, plate_occupancy
from backend.app.services.printer_incidents import (
    RECOVERY_WAITING_REASONS,
    WAITING_REASON_RECOVERING,
    runout_slot_desc,
)
from backend.app.services.printer_manager import printer_manager

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.app.models.printer_incident import PrinterIncident

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
# "owned" only while a LIVE recovery task exists (printer_incidents.driver_live),
# not by the token string. The recovery task's state is process-lifetime in-memory,
# so a server restart mid-recovery orphans the DB token — treating the token alone
# as ownership would leave the printer PAUSEd forever with the watchdog silenced.
# The orphan is instead reclaimed below.
#
# The escalated AMS tokens STAY: the escalation already fired its one-shot operator
# notification and deliberately left the printer PAUSED for a human (a jam that
# couldn't be recovered, a runout needing a same-slot refill, a physical fault needing
# hands). Re-notifying any of them through the pause-stall watch would just double up
# on a hold a human already owns — and the OPEN INCIDENT is the primary
# ownership signal (it covers a foreign print, which has no token to read).
#
# DERIVED from the incident store's own token set rather than re-listed, so a new hold
# token (the external-spool runout was one) cannot be born un-attended and start
# double-notifying the moment it ships. The plate-vision token used to be unioned in by
# hand here; since every incident KIND registers its token in one table
# (``printer_incidents._WAITING_REASON_BY_KIND``), that hand-listing is gone and the
# three pause-cause kinds are attended by construction.
_ATTENDED_PAUSE_REASONS: frozenset[str] = RECOVERY_WAITING_REASONS - {WAITING_REASON_RECOVERING}

_DEFAULT_GRACE_MINUTES = 30
_DEFAULT_PAUSE_GRACE_MINUTES = 15

# Attention reminders: how long a down printer's ORIGINAL escalation alert may go
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
    _ams_wedged_since.clear()
    _ams_wedged_paged.clear()
    _eject_verdict_reconciled.clear()
    _waiting_since.clear()


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


async def offline_stall_seconds(db: AsyncSession) -> float:
    """How long a printer may stay off its session before a person is told — THE reading of
    ``farm_offline_stall_minutes``, and the session-gap page's bound: this module's stall watch
    flags a printing unit past it and pages. Nothing waiting out a gap gives up on it (2026-10-11:
    a recovery driver waits for the printer's next session however long the gap runs)."""
    return await _grace_seconds(db, "farm_offline_stall_minutes", _DEFAULT_GRACE_MINUTES)


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
    grace_s = await offline_stall_seconds(db)

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
    from backend.app.services import printer_incidents

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
        if item.waiting_reason == WAITING_REASON_RECOVERING and not printer_incidents.driver_live(pid):
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
            or printer_incidents.driver_live(pid)
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
# The unowned eject: a pending eject whose terminal never arrived
# --------------------------------------------------------------------------- #
# The unowned eject's dwell: the wait is for the LIVE terminal handler, the first responder to a
# stopped sweep (the eject lane's own clock — Lane H of the event-not-clock plan).
_UNOWNED_EJECT_DWELL_S = 120.0

# printer_id -> the eject runtime VERDICT this watch has already re-triggered the
# reconciler on. The trigger below is level-shaped (an unowned eject stays unowned
# until something retires it), so without this the reconcile — and the stop and the
# page it can drive — would re-fire every tick for as long as the record stands. Keyed
# by the verdict STAMP, so a later eject that exceeds its own deadline is a new
# occasion on the same printer. Process-lifetime like the dwell maps beside it: a
# restart makes the STARTUP sweep the answer instead, which is the same act.
_eject_verdict_reconciled: dict[int, object] = {}


async def _reconcile_unowned_ejects(*, manager, now: float) -> None:
    """Re-drive the eject reconciler for a LOST TERMINAL ECHO on a connected printer.

    The second trigger of the one reconciler (the printer's session start is the other),
    and the only one for the shape the session start cannot see: the printer never dropped
    its session, so there is no first fresh report to ride and no restart to wait for — the
    eject's terminal echo simply never arrived (or arrived
    while the watchdog was mid-kill) and the record sits registered, refusing every
    later eject with ``eject_in_flight``.

    Two conditions, both required. The printer must be CONNECTED (a disconnected one is
    the connected-edge trigger's business, and reconciling against a dead session can
    only guess), and the watchdog's verdict must be at least
    :data:`_UNOWNED_EJECT_DWELL_S` old — because the LIVE terminal handler is the first
    responder to a stopped sweep and this must only ever see what it missed. A record
    with no verdict at all (a hydrated one its session start left in flight) is
    deliberately not touched here: nothing has judged it, so there is nothing to
    conclude.

    Spawned as a background task per printer, never awaited: the reconcile replays a
    terminal through ``farm_policy`` and may re-drive a stop, and the tick must not
    wait on either.
    """
    from backend.app.core.tasks import spawn_background_task
    from backend.app.services.eject.monitor import reconcile_pending_eject

    wall = datetime.fromtimestamp(now, tz=timezone.utc)
    for pid in list(plate_occupancy.printers_with_lease_or_eject()):
        try:
            pending = plate_occupancy.pending_eject_view(pid)
            if pending is None:
                _eject_verdict_reconciled.pop(pid, None)
                continue
            verdict = pending.runtime_exceeded_at
            if verdict is None or not plate_occupancy.unowned_eject(pid):
                continue
            if not manager.is_connected(pid):
                continue
            if (wall - verdict).total_seconds() < _UNOWNED_EJECT_DWELL_S:
                continue
            if _eject_verdict_reconciled.get(pid) == verdict:
                continue
            _eject_verdict_reconciled[pid] = verdict
            logger.warning(
                "farm_stall: printer %s still holds a %s eject whose runtime verdict fired at %s and whose "
                "terminal never arrived — re-driving the eject reconcile",
                pid,
                pending.purpose,
                verdict,
            )
            spawn_background_task(reconcile_pending_eject(pid), name=f"eject-pending-reconcile-{pid}")
        except Exception:  # noqa: BLE001 — one printer must not abort the sweep
            logger.exception("farm_stall: unowned-eject reconcile trigger failed for printer %s", pid)


async def check_unowned_ejects(*, manager=printer_manager, now: float | None = None) -> None:
    """The scheduler tick's unowned-eject watch (:func:`_reconcile_unowned_ejects`): a pending eject whose
    runtime verdict fired and whose terminal never arrived, on a printer that never dropped its session.
    Guarded — it must never kill the tick. Injectable ``manager``/``now`` (epoch seconds) for tests."""
    now = time.time() if now is None else now
    try:
        await _reconcile_unowned_ejects(manager=manager, now=now)
    except Exception:  # noqa: BLE001 — the tick must never die on this watch
        logger.exception("farm_stall: unowned-eject reconcile sweep failed")


# --------------------------------------------------------------------------- #
# The AMS wedged mid filament-change on an idle printer (2026-09-11, 002-H2S)
# --------------------------------------------------------------------------- #
# printer_id -> the ts the wedge was first observed (episode start), and the
# printers already paged for the current episode. An EPISODE is the predicate
# holding continuously: the moment it drops, both are popped, so a wedge that
# clears and recurs pages again. Process-lifetime, like the sibling dwells — a
# restart just restarts the dwell, which is the safe direction.
_ams_wedged_since: dict[int, float] = {}
_ams_wedged_paged: set[int] = set()

# How long the wedge must hold before it is worth a human's attention. Generous on
# purpose: a legitimate filament change occupies this state for tens of seconds,
# and the recovery driver's own rounds pass through it.
_AMS_WEDGED_IDLE_DWELL_S = 600.0


async def check_ams_wedged_idle(db: AsyncSession, *, manager=printer_manager, now: float | None = None) -> None:
    """One bounded page for an AMS latched mid filament-change on an IDLE printer.

    At ``ams_status_main == 1`` (:func:`bambu_mqtt.ams_mid_filament_change`) the
    scheduler's idle gate refuses to dispatch (since 2026-09-11), and with no incident
    open and no recovery driver live the farm has not commanded anything here. What a
    load or unload does in this posture is not assumed: :mod:`command_outcome` measures
    each one and logs the wire's answer. The refusal is correct and it is also SILENT:
    with no print running and no incident open, a parked AMS holds the printer out of
    the queue with nothing anywhere saying so
    — literally incident #60's shape (2026-08-29: 15 h, seven pending units, zero
    notifications) transplanted into a new field. A gate that can hold forever needs a
    watch that says so once.

    The predicate is ALL of: connected with a live state; that state NOT in
    ``print_scheduler.ACTIVE_PRINT_STATES`` (value 1 during a print is the ordinary
    mid-print change, the firmware's own business); the AMS reading mid-change; and
    nobody else already owning the printer — no OPEN ``printer_incident`` and no live
    recovery task, whose resume rounds pass through this state by design.

    Never writes: no queue row, no incident, no quarantine. The WARNING and the single
    ``on_ams_wedged_idle`` page are the whole surface. The page names two exits: the
    printer's Continue, and the printer card's Unload, which reports whether the AMS
    moved.
    """
    now = time.time() if now is None else now

    from backend.app.models.printer import Printer
    from backend.app.services import printer_incidents
    from backend.app.services.bambu_mqtt import ams_mid_filament_change
    from backend.app.services.notification_service import notification_service
    from backend.app.services.print_scheduler import ACTIVE_PRINT_STATES

    def _drop(printer_id: int) -> None:
        _ams_wedged_since.pop(printer_id, None)
        _ams_wedged_paged.discard(printer_id)

    result = await db.execute(select(Printer).where(Printer.is_active.is_(True)))
    for printer in result.scalars().all():
        pid = printer.id
        try:
            if not manager.is_connected(pid):
                _drop(pid)
                continue

            st = manager.get_status(pid)
            if st is None or (getattr(st, "state", None) or "").upper() in ACTIVE_PRINT_STATES:
                _drop(pid)
                continue

            if not ams_mid_filament_change(st):
                _drop(pid)
                continue

            if await printer_incidents.get_open(db, pid) is not None or printer_incidents.driver_live(pid):
                _drop(pid)
                continue

            first = _ams_wedged_since.get(pid)
            if first is None:
                _ams_wedged_since[pid] = now
                continue
            if now - first < _AMS_WEDGED_IDLE_DWELL_S or pid in _ams_wedged_paged:
                continue

            minutes = (now - first) / 60.0
            _ams_wedged_paged.add(pid)
            logger.warning(
                "farm_stall: printer %s AMS parked mid filament-change for %.0f min with no print running and "
                "no incident open (state=%s tray_now=%s pending_tray_target=%s) — the dispatcher refuses this "
                "printer while the AMS is mid-change; the farm has not commanded anything here",
                pid,
                minutes,
                getattr(st, "state", None),
                getattr(st, "tray_now", None),
                getattr(st, "pending_tray_target", None),
            )
            await notification_service.on_ams_wedged_idle(
                printer_id=pid,
                printer_name=printer.name,
                minutes=int(minutes),
                db=db,
            )
        except Exception:  # noqa: BLE001 — one bad printer must not abort the watch
            logger.exception("farm_stall: AMS wedged-idle watch failed for printer %s", pid)


# --------------------------------------------------------------------------- #
# The foreign-print pause watch
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
# Hourly attention reminders for a printer left down needing a human
# --------------------------------------------------------------------------- #
# Reminder copy — a re-fire is the SAME notification EVENT the original escalation
# produced (no new event types / templates / channels), so the operator sees a
# familiar alert; "STILL" frames it as a nag, not a fresh incident.
_JAM_REMINDER_DETAIL = "Spool jam STILL not recovered — the printer is still PAUSED and needs a human."
_RUNOUT_REMINDER_DETAIL = (
    "Filament runout STILL not resolved — the printer is still PAUSED awaiting a same-slot refill."
)
# The printer's pre-print plate check PAUSED the job and the hold is a person's: the decision
# hold (operator ruling 2026-10-05 — the farm stops the print when its window runs out, so a
# reminder reaches only a window longer than the reminder's) or the FALLBACK (the farm's stop did
# not land). Both are answered the same way, and the farm offers both exits.
_PLATE_VISION_REMINDER_DETAIL = "Print paused at the plate check. Ignore and resume, or stop the print."
_POWER_LOSS_REMINDER_REASON = "The farm could not answer the prompt; it is STILL waiting."
_Z_REFERENCE_REMINDER_DETAIL = (
    "This printer STILL has a part on its plate after a restart — its Z reference is lost. "
    "Remove the part by hand, then Mark plate cleared. Ejects are refused until then."
)


async def _remind_paused(db, notif, printer_id, printer_name, job_name, minutes, *, state=None) -> None:
    """Re-fire the pause-stall escalation's own event (WAITING_REASON_PAUSED)."""
    await notif.on_print_paused_stalled(printer_id, printer_name, job_name, minutes, db)


_PHYSICAL_REMINDER_DETAIL = (
    "A physical filament fault is STILL unresolved — the printer is still PAUSED and no swap can clear it."
)
# K10 (2026-10-10): the farm could not refill the paused job's empty toolhead. The exit is
# filament at the toolhead BEFORE the resume — a Bambuddy Resume retries the refill itself.
_TOOLHEAD_REMINDER_DETAIL = (
    "Toolhead STILL empty — the print is still PAUSED and the farm could not load a spool. "
    "Load a spool on the printer, or press Resume to retry the refill."
)

# incident kind -> the reminder's detail copy. One line per kind, so the nag reads
# like the alert it repeats. EVERY FAULT kind must appear: the lookup below falls back
# to the jam sentence, so a missing row does not fail loudly — it nags an operator about
# a spool jam on a printer holding for a lost Z reference.
#
# The DECLARED kinds (``printer_incident.DECLARED_KINDS``) are EXEMPT, here and in the
# unpaused twin below, and the exemption is structural rather than an oversight:
# :func:`_remind_open_incidents` never reaches the lookup for them, because a printer
# carrying a declared hold is skipped whole (``printer_incidents.automation_held``).
# Giving maintenance mode a reminder sentence would be writing copy for a branch that
# cannot execute — and the hour a hold is released, the nag resumes off the FAULT rows
# with their own copy. Add a row here only if a future declared kind is meant to nag.
_INCIDENT_REMINDER_DETAIL: dict[str, str] = {
    KIND_JAM: _JAM_REMINDER_DETAIL,
    KIND_RUNOUT: _RUNOUT_REMINDER_DETAIL,
    KIND_PHYSICAL: _PHYSICAL_REMINDER_DETAIL,
    KIND_POWER_LOSS: _POWER_LOSS_REMINDER_REASON,
    KIND_PLATE_VISION: _PLATE_VISION_REMINDER_DETAIL,
    KIND_Z_REFERENCE_LOST: _Z_REFERENCE_REMINDER_DETAIL,
    KIND_TOOLHEAD_REFILL: _TOOLHEAD_REMINDER_DETAIL,
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
    # The REPAIR class (2026-09-11): the wire never reports a repaired path, so the
    # suffix's "until the fault clears on the wire" would send the operator to wait for
    # an event that cannot happen. Name the two exits instead.
    KIND_PHYSICAL: (
        "A physical filament fault is STILL unresolved — the printer remains held and no swap can clear it. "
        "The printer is not paused — it is idle and will take no work until a filament change completes "
        "through the path (load a slot on the printer) or an operator presses Recover."
    ),
    # The pause-cause kinds' unpaused copy is not a variant of a paused sentence and
    # deliberately does not borrow the "until the fault clears on the wire" suffix: no wire
    # fact ends these holds. A lost Z reference holds an IDLE printer as its NORMAL shape
    # (there may never have been a print running), and only the operator ends it. A
    # plate-check episode is a PAUSE hold that ends with its job: the farm re-checks in
    # place, and on a failed re-check STOPS the print to retry it, or leaves it paused for a
    # person's decision and stops it when the window runs out — the terminal of that stop
    # closes the row, and a refused plate is then nagged from its GATE (the refused-plate arm
    # below), not from this row. So a plate-check row still open on a printer that is not
    # paused is only ever the short gap before that terminal or the sweep's job-ended-unseen
    # close; its copy says only what is true then.
    KIND_POWER_LOSS: (
        "This printer is STILL held at its power-loss prompt and will take no work until the prompt is answered."
    ),
    # Not paused, so "resume the print" would send the operator looking for a job that is
    # not waiting: state only what is true — the hold stands and blocks work.
    KIND_PLATE_VISION: "Plate check hold still open, but the printer is not paused. It takes no work while the hold stands.",
    KIND_Z_REFERENCE_LOST: _Z_REFERENCE_REMINDER_DETAIL,
    # A toolhead hold ends with its job (``incident_resolution``'s toolhead cells): open on a printer
    # that is not paused, it is only the gap before the terminal or the sweep's close.
    KIND_TOOLHEAD_REFILL: "Toolhead hold still open, but the printer is not paused. It takes no work while the hold stands.",
}


# A job PRINTING — the plate authority's active states (the one origin of "a job owns the
# printer") less the PAUSE every recovery hold is reminded in.
_PRINTING_STATES: frozenset[str] = ACTIVE_PRINT_STATES - {"PAUSE"}

# The kinds whose reminder re-fires an event of their OWN (not the recovery's
# ``on_spool_recovery_failed``), with state-true copy of their own: a power-loss prompt and a plate
# check both close on their job printing again, and a lost Z frame is a fact about the PLATE that
# no running print answers.
_OWN_EVENT_KINDS: frozenset[str] = frozenset({KIND_POWER_LOSS, KIND_PLATE_VISION, KIND_Z_REFERENCE_LOST})


def _live_runout_slot(state) -> str | None:
    """Human name of the slot the firmware is CURRENTLY demanding filament in, from
    the live printer state — or ``None`` when it names none. Formatting comes from
    ``printer_incidents.runout_slot_desc`` (one origin for the wording)."""
    demand = current_runout_demand(getattr(state, "hms_errors", None) or [])
    if demand is None:
        return None
    return runout_slot_desc(demand[0] * 4 + demand[1])


# reason -> the callable that RE-FIRES that reason's original notification event.
# This dict IS the single source of the remindable reason set (_ATTENTION_REASONS
# below), so adding a reason is a one-line edit that cannot drift from the pin.
#
# Every INCIDENT-backed hold is deliberately ABSENT: they are reminded from
# their OPEN ESCALATED INCIDENT (:func:`_remind_open_incidents`), not from a queue
# item's token. A token can only exist for a farm print, so the token lane could never
# nag about a foreign print left holding — exactly the class of hold that sat silent for
# hours. The plate-vision row was the LAST token-driven reminder and went the same way
# when the plate check became an incident kind (2026-09-04), which is what gives it
# foreign-print parity for free. Only the generic pause-stall pager remains here: it is
# the catch-all for a PAUSE nobody claimed, so by definition it has no incident to read.
_ATTENTION_DISPATCH: dict[str, Callable[..., Awaitable[None]]] = {
    WAITING_REASON_PAUSED: _remind_paused,
}
# The ESCALATED waiting_reason tokens a down-printer reminder re-fires for. Each of
# these was already alerted ONCE by the code that set it and then left the printer
# PAUSED for a human — the reminder nags hourly while the hold persists.
_ATTENTION_REASONS: frozenset[str] = frozenset(_ATTENTION_DISPATCH)


async def _remind_open_incidents(
    db: AsyncSession,
    notif,
    *,
    open_rows: Sequence[PrinterIncident],
    manager,
    now: float,
    held_keys: set[tuple[int, str]],
) -> None:
    """Hourly nag for every OPEN ESCALATED AMS incident still holding its printer.

    Printer-scoped by design: an incident is a fact about the PRINTER, so this
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

    for incident in open_rows:
        if incident.status != STATUS_ESCALATED:
            continue  # 'recovering' is the machine acting — not a hold to nag about
        pid = incident.printer_id
        key = (pid, f"incident:{incident.id}")
        try:
            if printer_incidents.automation_held(pid):
                # The operator is standing at this machine with it in maintenance mode.
                # ONE read covers both halves: the declared row itself (nagging someone
                # hourly about the hold they are holding is noise) and any fault
                # recorded WHILE they work — a jam code raised by their own filament
                # change is not an unattended hold. The rows stay open and nag again
                # the hour after the hold is released, because the hold suppressed the
                # reminder, not the fault.
                continue
            if not manager.is_connected(pid):
                continue
            st = manager.get_status(pid)
            live = (getattr(st, "state", None) or "").upper()
            if not live or live == "UNKNOWN":
                continue
            if live in _PRINTING_STATES and incident.kind not in _OWN_EVENT_KINDS:
                # A job is PRINTING over the hold (2026-10-10). The recovery event's copy says the
                # job is left PAUSED, or has ended, and the printer idle — none of it is true of a
                # running print, and nothing waits on a person: every such row is answered by the
                # running print's own evidence (a wire row's running edge, a repair row's running
                # sighting after the sweep's dwell, a toolhead row's fed print), or the FARM still
                # owns it — K11 keeps a row OPEN over a print a person's screen Retry resumed while
                # the farm's pull-back is queued, and the per-push detector refills the toolhead the
                # moment that command runs. Skipped BEFORE the key is held, so a print that pauses
                # again (a re-jam) is nagged one full window after that pause, never at once.
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
            detail = copy.get(incident.kind, _JAM_REMINDER_DETAIL)
            # The EVENT is chosen by kind, not just the copy. An AMS-fault event carrying
            # power-loss text would page whoever asked to hear about spool recoveries and
            # stay silent for whoever asked about outages — the toggle would be lying
            # about what it controls. Each kind re-fires the event its own escalation
            # raised, which is what keeps a reminder a reminder.
            if incident.kind == KIND_POWER_LOSS:
                await notif.on_power_loss_hold(
                    printer_id=pid,
                    printer_name=printer_name,
                    job_name=job_name,
                    # None, not `minutes`: `minutes` is how long the HOLD has run, and
                    # the outage ended when the printer reconnected hours ago. The
                    # sentence is dropped rather than filled with a plausible lie.
                    outage_minutes=None,
                    reason=detail,
                    db=db,
                )
            elif incident.kind == KIND_PLATE_VISION:
                await notif.on_plate_not_empty(pid, printer_name, db, source_detail=detail)
            elif incident.kind == KIND_Z_REFERENCE_LOST:
                await notif.on_z_reference_lost(printer_id=pid, printer_name=printer_name, db=db)
            else:
                # A hold on a printer that is NOT paused outlived the job it was raised on
                # (a physical fault after its job ended, a restart give-up after the farm's
                # own stop): the wrapper must not call that job "left PAUSED".
                await notif.on_spool_recovery_failed(
                    printer_id=pid,
                    printer_name=printer_name,
                    job_name=job_name,
                    detail=detail,
                    db=db,
                    kind=incident.kind,
                    runout_slot=slot,
                    foreign=incident.item_id is None,
                    job_ended=live != "PAUSE",
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


async def _remind_refused_plates(
    db: AsyncSession,
    *,
    manager,
    now: float,
    held_keys: set[tuple[int, str]],
) -> None:
    """Hourly nag for every plate the printer's plate check REFUSED and a human must clear.

    The escalation end of the plate-check ladder (operator ruling 2026-09-29: "escalate …
    make sure the bed is not at the bottom"): the terminal gated the plate with the
    printer's words (``EscalationOnly`` carrying a ``PlateRefusal``), the eject monitor sent
    the FIRST page at once (the refusal is its event — ``eject.monitor.watch_gate_escalation_only``), and this arm repeats
    it once per :data:`_ATTENTION_REMINDER_S` until the gate clears. The plate-check ROW was
    closed by that terminal, so the incident arm cannot see this hold; the GATE is the fact,
    read off the plate authority's view.

    Same contract as the other two arms: remindable only on a connected printer; the first
    sighting SEEDS the ``"attention"`` window (the monitor delivered the first page), then
    one page per window; tracking resets when the gate clears or its refusal is gone. One
    key per GATE (``plate_since``), so a gate cleared and raised again between two ticks is
    a new episode rather than a continuation. The sentence is the monitor's own
    (:func:`eject.monitor.escalation_sentence`) — one origin for the words — and a printer
    in maintenance mode is skipped, as the monitor withholds its own page there.
    """
    from backend.app.models.printer import Printer
    from backend.app.services import printer_incidents
    from backend.app.services.eject.monitor import escalation_sentence, notify_plate_not_empty
    from backend.app.services.plate_occupancy import EscalationOnly

    result = await db.execute(select(Printer).where(Printer.is_active.is_(True)))
    for printer in result.scalars().all():
        pid = printer.id
        try:
            view = plate_occupancy.snapshot(pid)
            policy = view.plate_policy
            if not view.plate_occupied or not isinstance(policy, EscalationOnly) or policy.refusal is None:
                continue
            if printer_incidents.automation_held(pid) or not manager.is_connected(pid):
                continue
            since = view.plate_since.isoformat() if view.plate_since is not None else ""
            key = (pid, f"plate_refused:{since}")
            held_keys.add(key)

            akey = f"{pid}:plate_refused:{since}"
            first = _attention_first_seen.get(key)
            if first is None:
                # First remindable sighting: seed the window so the first reminder lands one
                # full window later (the monitor paged the refusal the moment it was gated).
                _attention_first_seen[key] = now
                notify_dedup.allow("attention", akey, now, _ATTENTION_REMINDER_S)
                continue
            if not notify_dedup.allow("attention", akey, now, _ATTENTION_REMINDER_S):
                continue

            await notify_plate_not_empty(
                pid, source_detail=escalation_sentence(farm_source=False, refusal=policy.refusal)
            )
            logger.warning(
                "farm_stall: printer %s refused plate STILL gated (%d min) — attention reminder re-fired",
                pid,
                int((now - first) // 60),
            )
        except Exception:  # noqa: BLE001 — one bad printer must not abort the watch
            logger.exception("farm_stall: refused-plate attention reminder failed for printer %s", pid)


async def check_attention_reminders(db: AsyncSession, *, manager=printer_manager, now: float | None = None) -> None:
    """Re-fire the ORIGINAL escalation notification for a printer left down.

    The offline / pause-stall / spool-recovery / runout escalations each alert
    EXACTLY ONCE per incident and then leave the printer PAUSED for a human, so a
    hold that a human doesn't clear for hours produced a single Discord message —
    the 2026-07-20 incident sat 5+ h that way on two printers. This watch nags,
    through THREE arms that never overlap:

    * every OPEN ESCALATED AMS incident (:func:`_remind_open_incidents`) — printer
      scoped, so a FOREIGN print's hold nags exactly like a farm one. A queue
      token cannot serve here: a foreign hold has none to read;
    * every plate the printer's plate check REFUSED (:func:`_remind_refused_plates`) —
      the gate, not a row: the terminal that refused the plate closed its plate-check row;
    * every still-``printing`` farm unit carrying one of the remaining non-AMS
      ESCALATED tokens in :data:`_ATTENTION_REASONS` (plate-vision, pause-stall),
      re-firing THAT reason's own notification event via :data:`_ATTENTION_DISPATCH`
      — no new event types.

    Each nags once per :data:`_ATTENTION_REMINDER_S` window until the hold lifts.

    The same pass re-arms a plate-check decision hold's deadline timer when the hold is past
    its deadline with no timer running (``pause_recovery.rearm_overdue_decision_deadlines``,
    operator ruling 2026-10-05): the timer is process memory, and a hold that lost it would
    otherwise sit paused and hot past the window the operator set. It reads the open rows this
    pass already read, so it costs nothing while no such hold exists.

    Cadence: the first alert stays owned by the original escalation path, which
    never touches the ``"attention"`` :func:`notify_dedup.allow` scope. So the loop
    SEEDS the window the first tick it sees the condition and the first REMINDER
    lands one full window later — then once per window while still held. Tracking
    resets the moment the condition lifts (pause ends, the unit is no longer
    ``printing``, the reason changes, or a refused plate's gate clears) so a future
    incident nags afresh.

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
    from backend.app.services import pause_recovery, printer_incidents

    open_rows = await printer_incidents.all_open(db)
    await pause_recovery.rearm_overdue_decision_deadlines(db, open_rows)
    await _remind_open_incidents(
        db, notification_service, open_rows=open_rows, manager=manager, now=now, held_keys=held_keys
    )
    await _remind_refused_plates(db, manager=manager, now=now, held_keys=held_keys)

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


# --------------------------------------------------------------------------- #
# Waiting on the printer — THE attention owner (owner 5, 2026-10-11)
# --------------------------------------------------------------------------- #

# THE page bound for a command the printer has not carried out and for a printer stuck in a state the
# farm waits on: ``farm_attention_page_minutes`` (Settings → Farm). It decides only WHEN a person hears —
# never an outcome. The default sits above the slowest measured answer a healthy printer gives: an
# accepted AMS pull-back that ran on its own 274-280 s after the send (011-H2S 2026-10-09, 014-H2S
# 2026-10-10), a plate-check stop answered in 56-62 s (014-H2S 2026-10-05), an H2D start that sat at
# FINISH ~50 s before PREPARE (#1078).
_PAGE_BOUND_KEY = "farm_attention_page_minutes"
_DEFAULT_PAGE_BOUND_MINUTES = 10

# Bounds whose physics set their own, owned here with every other page bound:
# * a print RUNNING with nothing fed and no firmware sequence (the feed state's EMPTY_UNCONFIRMED): the
#   tray field leads the change posture by ~3 s (shape 41), so a printer reporting normally answers
#   long before this; a print in this state is a print about to lay down air;
_NOTHING_FED_PAGE_S = 60.0
# * a cooldown running long — the cooldown watch's published wait; a person often clears a finished
#   part on their own well inside it;
_COOLDOWN_PAGE_S = 5400.0
# * a plate only a person may clear (an ``EscalationOnly`` gate with no refusal: a farm part whose eject
#   never ran, a foreign deposit, a stalled cooldown) — the same patience, from the gate's own stamp.
#   A plate the printer REFUSED is paged by its event (the escalation-only watch) and repeated hourly
#   by :func:`_remind_refused_plates`.
_GATED_PLATE_PAGE_S = 5400.0

# Durable once-guard for every page below (``notify_dedup``'s ledger, so a restart neither re-pages an
# hour-old wait nor forgets one): one key per waiting EPISODE, re-paged once per hour while it stands.
_WAITING_SCOPE = "waiting"

# (kind, printer_id[, …]) -> wall time this process FIRST saw a level condition (an episode's start).
# Process memory, page-only: a restart restarts the episode, so its page lands one bound later — the
# safe direction for a page, and nothing is decided by it.
_waiting_since: dict[tuple, float] = {}


def _family_noun(command: str, target: int | None) -> str:
    """What was sent, in the operator's words."""
    from backend.app.services.command_outcome import Family, spec_of

    if command == "unload":
        return "the pull-back"
    if command == "load":
        slot = runout_slot_desc(target)
        return f"the load of {slot}" if slot else "the load"
    try:
        family = spec_of(command).family
    except LookupError:
        return f"the {command} command"
    nouns = {
        Family.RESUME: "the resume",
        Family.PAUSE: "the pause",
        Family.STOP: "the stop",
        Family.POWER_LOSS_RESUME: "the resume after the power loss",
        Family.PRINT_START: "the print start",
        Family.EJECT_START: "the eject start",
        Family.AMS_LEVER: "the AMS command",
        Family.DIALOG: "the dialog button",
    }
    return nouns.get(family, f"the {command.replace('_', ' ')} command")


def _action_sentence(action, state, minutes: int) -> str:
    """The page for one outstanding action — what was sent, by whom, what the printer did with it, for how
    long — in the printer's terms (sys-admin register: the fact, then what follows)."""
    from backend.app.services.bambu_mqtt import ams_mid_filament_change

    noun = _family_noun(action.command, action.target)
    connected = bool(getattr(state, "connected", False)) if state is not None else False
    live = (getattr(state, "state", None) or "").upper() if connected else ""
    if action.command in ("load", "unload") and action.acked:
        if connected and ams_mid_filament_change(state):
            # Capsule 3's copy: the AMS holds the farm's swap command behind its own change.
            return (
                "The AMS went back into a filament change and holds the farm's swap command behind it. "
                "Free the spool, then press Retry on the printer."
            )
        return f"The AMS accepted {noun} {minutes} min ago and has not run it. Recovery resumes when it runs."
    who = "A person sent" if action.actor == "operator" else "The farm sent"
    if action.acked:
        sentence = f"The printer accepted {noun} {minutes} min ago and has not carried it out."
    else:
        sentence = f"{who} {noun} {minutes} min ago and the printer has not answered."
    if not connected:
        return f"{sentence} The printer is not connected."
    return f"{sentence} The printer reads {live or 'no state'}." if live else sentence


def _held_on_its_row(action) -> bool:
    """Is ``action`` the farm AMS motion command its open row still holds PENDING
    (``printer_incidents.pending_command`` — the projection over the record; accepted and not yet run, or
    not yet read)? Such a command passed its step bound and sits in the recovery driver's deliberately
    unbounded wait for it to run (``spool_recovery._await_pending_command``, ruling 2026-10-10), which
    pages nothing — so the attention owner keeps paging it past its self-heal bound, at the page bound."""
    from backend.app.services import printer_incidents

    if action.incident_id is None:
        return False
    pending = printer_incidents.pending_command(action.printer_id, incident_id=action.incident_id)
    return pending is not None and pending.action_id == action.action_id


def _level(key: tuple, present: bool, now: float) -> float | None:
    """An episode's start for a LEVEL condition: stamped on its first sighting, dropped when it ends."""
    if not present:
        _waiting_since.pop(key, None)
        return None
    return _waiting_since.setdefault(key, now)


def _ledger_ts(stamp) -> float:
    """A ledger stamp (naive UTC, ``notify_dedup``) as epoch seconds."""
    return stamp.replace(tzinfo=timezone.utc).timestamp() if stamp.tzinfo is None else stamp.timestamp()


async def _due(db: AsyncSession, key: str, now: float) -> bool:
    """Is a page owed for this episode key: never sent, or last sent a full :data:`_ATTENTION_REMINDER_S`
    ago? The durable ledger is the once-guard, so a restart neither repeats nor forgets a page."""
    last = await notify_dedup.last_sent_at(db, _WAITING_SCOPE, key)
    return last is None or now - _ledger_ts(last) >= _ATTENTION_REMINDER_S


async def _page_waiting(db: AsyncSession, notif, printer_id: int | None, key: str, detail: str, now: float) -> None:
    """ONE ``on_printer_waiting`` page per episode key, then once per :data:`_ATTENTION_REMINDER_S` while it
    stands."""
    from backend.app.models.printer import Printer

    if not await _due(db, key, now):
        return
    printer = await db.get(Printer, printer_id) if printer_id is not None else None
    printer_name = printer.name if printer is not None else (f"printer {printer_id}" if printer_id else "farm")
    await notif.on_printer_waiting(printer_id, printer_name, detail, db)
    await notify_dedup.record_sent(db, _WAITING_SCOPE, key, now)
    logger.warning("farm_stall: printer %s — waiting page (%s): %s", printer_id, key, detail)


async def check_waiting_on_printers(db: AsyncSession, *, manager=printer_manager, now: float | None = None) -> None:
    """Page every wait the farm decides by the printer's own EVENT, past its bound — THE attention owner
    (owner 5, 2026-10-11). It reads, it pages; it never resolves, escalates or acts.

    * The record's UNANSWERED actions (``printer_action.outstanding_actions`` — in memory, no query),
      after the record evaluates its self-heal bounds (``printer_action.check_bounds``): a command out
      longer than the page bound (``farm_attention_page_minutes``) in the printer's ONLINE seconds
      (``printer_action.online_seconds_since``), named by its family and by whether the printer accepted
      it — an AMS command held behind the printer's own filament change takes capsule 3's copy. This
      covers every command the farm (or a person through Bambuddy) sent and the printer has not carried
      out: a resume, a stop, a start, an eject start, a plate-check press, a power-loss resume, a lever, a
      dialog button. An action whose self-heal bound passed is skipped: its lane took the fallback and
      pages its own hand-over. The one exception is a farm AMS motion command its row still holds pending
      (:func:`_held_on_its_row`): past its step bound it sits in the recovery driver's deliberately
      unbounded wait for it to run, which pages nothing, so it is paged here at the same bound (the
      2026-10-10 ruling: "One page goes out if it has not run 10 min after the send"; production paged at
      600 s). A command with no echo by design is never outstanding, so it is never paged.
    * The LEVELS (one in-memory read per connected printer): a print RUNNING inside a firmware filament
      change past the bound (the 009 hang); a print RUNNING in its end-of-print sequence at the last layer
      past the bound; a print RUNNING in its start sequence past the bound while an AMS row is open on the
      printer (2026-10-16: the farm never acts inside the printer's own sequences, it waits for their
      event — a sequence that never ends is paged, never acted on); a print RUNNING with nothing fed and no firmware sequence past
      :data:`_NOTHING_FED_PAGE_S`; an unreadable feed while a job is on the printer; a print that resumed
      at its plate check and has not reached layer 1; a unit held on a stalled upload (one query); a
      cooldown running long (``eject.monitor.cooldown_waits``); a plate only a person may clear (the plate
      authority's view). A commanded RFID read is never paged: its self-heal bound
      (``ams_presence._IDENTIFY_SELF_HEAL``, 30 s online) concludes it well inside the page bound (production
      paged none).

    A printer in maintenance mode is never paged (the person is at the machine). Cost per tick: no DB
    read for the actions or the levels, one query for the stalled-upload holds, one active-printer query,
    and one ledger read per candidate PAST its bound (zero in a healthy farm). Guarded per printer and per
    item: one bad reading never costs the others their page.
    """
    from backend.app.services import printer_action, printer_incidents
    from backend.app.services.eject.monitor import cooldown_waits, escalation_sentence
    from backend.app.services.feed_state import FeedPhase
    from backend.app.services.notification_service import notification_service
    from backend.app.services.plate_occupancy import EscalationOnly

    now = time.time() if now is None else now
    bound_s = await _grace_seconds(db, _PAGE_BOUND_KEY, _DEFAULT_PAGE_BOUND_MINUTES)
    utc_now = datetime.utcnow()
    notif = notification_service

    # 0. the record's self-heal bounds, for the printer that reports rarely (the record evaluates them on
    #    every status push too) — first, so an action whose bound passes now is its lane's below
    try:
        printer_action.check_bounds()
    except Exception:  # noqa: BLE001 — the record's evaluation must not cost the pages theirs
        logger.exception("farm_stall: the record's self-heal bounds could not be evaluated")

    # 1. the record's unanswered actions, aged in the printer's ONLINE seconds (offline time is no wait on
    #    the printer); one whose self-heal bound passed is its lane's, which pages its own hand-over — except
    #    a farm AMS motion command its row still holds pending (:func:`_held_on_its_row`)
    for action in printer_action.outstanding_actions():
        try:
            if action.bound_passed and not _held_on_its_row(action):
                continue
            age = printer_action.online_seconds_since(action.printer_id, action.sent_at)
            if age < bound_s or printer_incidents.automation_held(action.printer_id):
                continue
            state = manager.get_status(action.printer_id)
            await _page_waiting(
                db,
                notif,
                action.printer_id,
                f"action:{action.action_id}",
                _action_sentence(action, state, int(age // 60)),
                now,
            )
        except Exception:  # noqa: BLE001 — one action's page must not cost the others theirs
            logger.exception("farm_stall: waiting page failed for action %s", action.action_id)

    # 2. the levels, per connected printer
    from backend.app.models.printer import Printer

    printers = (await db.execute(select(Printer.id).where(Printer.is_active.is_(True)))).scalars().all()
    waits = cooldown_waits()
    for pid in printers:
        try:
            if printer_incidents.automation_held(pid):
                continue
            state = manager.get_status(pid) if manager.is_connected(pid) else None
            live = (getattr(state, "state", None) or "").upper() if state is not None else ""
            client = manager.get_client(pid) if state is not None else None
            reading = client.feed() if client is not None else None
            phase = reading.phase if reading is not None else None
            running = live == "RUNNING"

            since = _level(("changing", pid), running and phase is FeedPhase.CHANGING, now)
            if since is not None and now - since >= bound_s:
                await _page_waiting(
                    db,
                    notif,
                    pid,
                    f"changing:{pid}:{int(since)}",
                    f"The printer has been RUNNING inside a filament change for {int((now - since) // 60)} min "
                    "with no layer printed. Check the AMS and the toolhead.",
                    now,
                )
            # The printer's own END-OF-PRINT retract (at the last layer) and its START sequence (before
            # layer 1) are its sequences: the farm never acts inside one on a clock (2026-10-16) and waits
            # for the printer's event — so one that never ends is a person's to hear about (F9,
            # operator-approved): the retract as a bare level; the start sequence only while an AMS row is
            # open on the printer (a start block runs minutes on every healthy print). The END_RETRACT phase
            # spans printing the whole last layer, so the retract's level is anchored on the end-of-print
            # unload: the active extruder reads EMPTY (no feeder) — a last layer still printing fed never
            # starts it, however long it takes.
            retracting = (
                running and reading is not None and reading.phase is FeedPhase.END_RETRACT and reading.feeder is None
            )
            since = _level(("end_retract", pid), retracting, now)
            if since is not None and now - since >= bound_s:
                await _page_waiting(
                    db,
                    notif,
                    pid,
                    f"end_retract:{pid}:{int(since)}",
                    f"The printer has been RUNNING in its end-of-print retract at the last layer for "
                    f"{int((now - since) // 60)} min. Check the toolhead.",
                    now,
                )
            ams_hold = bool(printer_incidents.open_kinds(pid) & AMS_FAULT_KINDS)
            since = _level(("start_block", pid), running and phase is FeedPhase.START_BLOCK and ams_hold, now)
            if since is not None and now - since >= bound_s:
                await _page_waiting(
                    db,
                    notif,
                    pid,
                    f"start_block:{pid}:{int(since)}",
                    f"The printer has been RUNNING in its start sequence for {int((now - since) // 60)} min with "
                    "an AMS hold open and no layer printed. Check the AMS and the toolhead.",
                    now,
                )
            since = _level(("nothing_fed", pid), running and phase is FeedPhase.EMPTY_UNCONFIRMED, now)
            if since is not None and now - since >= _NOTHING_FED_PAGE_S:
                await _page_waiting(
                    db,
                    notif,
                    pid,
                    f"nothing_fed:{pid}:{int(since)}",
                    "The printer is RUNNING with the toolhead empty and no AMS filament change reported. "
                    "The farm stops the print at the first layer printed empty.",
                    now,
                )
            in_job = live in ("RUNNING", "PAUSE")
            since = _level(("unreadable_feed", pid), in_job and phase is FeedPhase.UNKNOWN, now)
            if since is not None and now - since >= bound_s:
                await _page_waiting(
                    db,
                    notif,
                    pid,
                    f"unreadable_feed:{pid}:{int(since)}",
                    f"The printer's toolhead feed has been unreadable for {int((now - since) // 60)} min with a "
                    "job on it. The farm decides nothing about the feed until it reads again.",
                    now,
                )
            plate_check = printer_incidents.snapshot(pid, kind=KIND_PLATE_VISION)
            before_layer_1 = (
                running
                and plate_check is not None
                and plate_check.get("status") == "recovering"
                and int(getattr(state, "layer_num", 0) or 0) < 1
            )
            since = _level(("plate_check_layer", pid), before_layer_1, now)
            if since is not None and now - since >= bound_s:
                await _page_waiting(
                    db,
                    notif,
                    pid,
                    f"plate_check_layer:{pid}:{int(since)}",
                    f"The print resumed at its plate check {int((now - since) // 60)} min ago and has not reached "
                    "layer 1.",
                    now,
                )
            wait = waits.get(pid)
            cooling_long = wait is not None and not wait.held and wait.waited_s >= _COOLDOWN_PAGE_S
            since = _level(("cooldown", pid), cooling_long, now)
            if since is not None and wait is not None:
                key = f"cooldown:{pid}:{int(since)}"
                if await _due(db, key, now):
                    from backend.app.models.printer import Printer as _Printer

                    printer = await db.get(_Printer, pid)
                    await notif.on_cooldown_escalation(
                        pid,
                        printer.name if printer is not None else f"printer {pid}",
                        bed_c=wait.bed_c,
                        line_c=wait.line_c,
                        db=db,
                    )
                    await notify_dedup.record_sent(db, _WAITING_SCOPE, key, now)
                    logger.warning(
                        "farm_stall: printer %s cooldown still unreleased after %ss — paged", pid, wait.waited_s
                    )
            # An eject sweep running long past its estimate: nothing stops a sweep on elapsed time any
            # more (2026-10-11 — its beacons and its terminal answer it; only the bed-drop guard can stop
            # one), so a sweep still running its estimate plus the page bound after the printer started it
            # is a person's to look at. A sweep the farm already decided to stop is the kill path's page.
            pending = plate_occupancy.pending_eject_view(pid)
            if (
                pending is not None
                and pending.started_at is not None
                and pending.expected_runtime_s is not None
                and pending.runtime_exceeded_at is None
            ):
                ran_s = (datetime.now(timezone.utc) - pending.started_at).total_seconds()
                if ran_s >= pending.expected_runtime_s + bound_s:
                    await _page_waiting(
                        db,
                        notif,
                        pid,
                        f"eject:{pid}:{pending.started_at.isoformat()}",
                        f"The eject sweep has run {int(ran_s // 60)} min against an expected "
                        f"{pending.expected_runtime_s:.0f} s and has not finished. The plate stays gated until it "
                        "does; check the printer.",
                        now,
                    )
            view = plate_occupancy.snapshot(pid)
            policy = view.plate_policy
            deposit = view.plate_occupied and isinstance(policy, EscalationOnly) and policy.refusal is None
            if deposit and view.plate_since is not None:
                gated_s = (utc_now - view.plate_since.replace(tzinfo=None)).total_seconds()
                if gated_s >= _GATED_PLATE_PAGE_S:
                    from backend.app.services.eject.monitor import notify_plate_not_empty

                    key = f"plate:{pid}:{view.plate_since.isoformat()}"
                    if await _due(db, key, now):
                        await notify_plate_not_empty(
                            pid,
                            source_detail=escalation_sentence(
                                farm_source=view.plate_source_subtask_id is not None, refusal=None
                            ),
                        )
                        await notify_dedup.record_sent(db, _WAITING_SCOPE, key, now)
                        logger.warning("farm_stall: printer %s plate held for a person — paged", pid)
        except Exception:  # noqa: BLE001 — one printer's page must not cost the others theirs
            logger.exception("farm_stall: waiting levels failed for printer %s", pid)

    # 3. a unit held on a stalled upload (one query)
    from backend.app.services.print_scheduler import WAITING_REASON_UPLOAD_STALLED

    held_items = (
        await db.execute(
            select(PrintQueueItem.id, PrintQueueItem.printer_id).where(
                PrintQueueItem.waiting_reason == WAITING_REASON_UPLOAD_STALLED
            )
        )
    ).all()
    live_items = {item_id for item_id, _pid in held_items}
    for key in [k for k in _waiting_since if k[0] == "upload_stalled" and k[1] not in live_items]:
        _waiting_since.pop(key, None)
    for item_id, item_printer in held_items:
        try:
            since = _level(("upload_stalled", item_id), True, now)
            if since is None or now - since < bound_s:
                continue
            await _page_waiting(
                db,
                notif,
                item_printer,
                f"upload_stalled:{item_id}:{int(since)}",
                f"Queue unit {item_id}: upload to its pinned printer stalled; unit held {int((now - since) // 60)} "
                "min. Check the printer's network and USB drive.",
                now,
            )
        except Exception:  # noqa: BLE001
            logger.exception("farm_stall: stalled-upload page failed for unit %s", item_id)
