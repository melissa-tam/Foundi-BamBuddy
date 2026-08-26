"""Print scheduler service - processes the print queue."""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import settings
from backend.app.core.database import async_session, run_with_retry
from backend.app.core.tasks import spawn_background_task
from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.settings import Settings
from backend.app.models.smart_plug import SmartPlug
from backend.app.services import notify_dedup
from backend.app.services.bambu_ftp import (
    cache_3mf_download,
    cleanup_downloaded_3mf,
    delete_file_async,
    get_ftp_retry_settings,
    upload_file_async,
    with_ftp_retry,
)
from backend.app.services.dispatch_kick import DispatchKick, dispatch_kick
from backend.app.services.eject import progress as dispatch_progress
from backend.app.services.farm_staging import build_staged_reason, maybe_release_periodic
from backend.app.services.filament_deficit import (
    _extruder_side_for_ams,
    _get_printer_backup_context,
    compute_deficit_for_queue_item,
    live_unread_slots,
    request_unread_reads,
)
from backend.app.services.notification_service import notification_service
from backend.app.services.printer_manager import (
    printer_manager,
    supports_drying,
    supports_drying_while_printing,
)
from backend.app.services.queue_transitions import claim_pending_for_dispatch
from backend.app.services.smart_plug_manager import smart_plug_manager
from backend.app.services.spool_selection import (
    DEFAULT_MIN_START_SPOOL_G,
    DEFAULT_SELECTION_POLICY,
    SELECTION_POLICIES,
    WAITING_REASON_PINNED_UNAVAILABLE,
    WAITING_REASON_UNREAD_PENDING,
    MatchOutcome,
    SlotInventory,
    backup_partner_gap,
    build_slot_inventory,
    colors_are_similar,
    dominant_start_block,
    effective_policy,
    mapping_json,
    match_filaments_to_slots,
    normalize_color_for_compare,
    parse_pins,
)
from backend.app.services.stagger import stagger_policy
from backend.app.services.tray_fields import parse_tray_state, tray_presence, tray_unread
from backend.app.services.usb_storage import upload_in_flight
from backend.app.utils.filament_types import canonical_filament_type as _canonical_filament_type
from backend.app.utils.filename import derive_remote_filename
from backend.app.utils.printer_models import normalize_printer_model

logger = logging.getLogger(__name__)

# Bambu firmware states that mean the project_file has actually been accepted
# and the printer is now processing / running / paused mid-print. Used by the
# dispatch watchdog (#1370): a transition into one of these states means the
# print landed, anything else (e.g. FINISH -> IDLE after the user dismisses
# a post-print prompt) is NOT a valid "command landed" signal even though the
# state value did change. SLICING is included because some firmwares park
# briefly in SLICING between PREPARE and RUNNING while parsing the g-code.
_ACTIVE_PRINT_STATES: frozenset[str] = frozenset({"PREPARE", "SLICING", "RUNNING", "PAUSE"})

# Dispatch precondition: the queued item's source file is gone from disk. Held as a
# WAIT (see ``_hold_dispatch_precondition``) rather than failed — 2026-08-14, a farm
# bug deleted a library file out from under 22 pending units and every dispatch
# insta-failed server-side, each failure counting toward the printer's
# consecutive-failure streak until the whole fleet had quarantined itself overnight.
# The frontend renders this token via ``utils/waitingReason.ts``.
WAITING_REASON_LIBRARY_FILE_MISSING = "library_file_missing"

# USB pre-flight: the H2 fleet reports USB presence (state.sdcard) ONLY inside a
# full status report, which we must explicitly request (request_status_update →
# MQTT pushall). The old fixed 2.5 s settle sleep is gone (latency Phase A): if a
# full report already landed within ``usb_preflight_fresh_window_seconds`` we read
# the cached flag with no request and no wait; otherwise we request and wait on the
# client's full-report Event up to ``usb_preflight_max_wait_seconds`` (event, not a
# fixed sleep — it proceeds the instant the fresh report merges).

# Filament-type equivalence + canonicalisation is shared with the farm capability
# gate — single source of truth in ``utils.filament_types`` (imported above as
# ``_canonical_filament_type`` to preserve the existing call sites).


def _derive_estimated_time(archive, library_file) -> int | None:
    """Estimated print time (seconds) for a job-started notification, from whichever
    source the dispatched item has.

    Prefers the archive's parsed ``print_time_seconds`` column; falls back to the
    library file, whose estimate lives in its ``file_metadata`` JSON — ``LibraryFile``
    has NO ``print_time_seconds`` column, so reading one raised ``AttributeError`` and
    crashed dispatch for any file whose metadata carried no estimate (the established
    read is ``file_metadata.get("print_time_seconds")``; see ``routes/library.py``).
    ``file_metadata`` itself may be ``None``. Returns ``None`` when neither source has a
    (truthy) estimate."""
    if archive is not None and archive.print_time_seconds:
        return archive.print_time_seconds
    if library_file is not None:
        meta = library_file.file_metadata or {}
        return meta.get("print_time_seconds") or None
    return None


def _present_candidates(loaded: list[dict]) -> list[dict]:
    """Drop loaded-filament entries whose live tray reads EMPTY on the wire.

    The presence gate for DISPATCH candidates, applied at the two call sites that
    match filaments to slots — deliberately NOT inside ``_build_loaded_filaments``,
    because ``spool_recovery`` reads the JAMMED tray's identity out of that same
    builder and must keep seeing it.

    Gates on ``present is False`` only (``tray_fields.tray_presence``, one origin),
    so it fails OPEN: a partial push, an offline dialect that never reports
    presence, or the 004-H2S state-9-while-feeding shape all read UNKNOWN and stay
    eligible. Excluding those would block dispatch on healthy hardware, which is a
    far worse failure than the one this guards.

    In today's builder output nothing is dropped — an entry is only emitted for a
    tray with a NON-empty ``tray_type``, and the cleared-tray shape requires an
    asserted-empty one. That is the point: the two rules agree, and this makes the
    agreement explicit and single-origin instead of implicit in a truthiness test,
    so a future builder change cannot silently make stale trays dispatchable.
    """
    return [f for f in loaded if tray_presence(parse_tray_state(f.get("state")), f.get("type")) is not False]


@dataclass(frozen=True)
class _PlannedDispatch:
    """One (queue item, printer, decided mapping) dispatch planned by this tick.

    The mapping travels WITH the plan instead of being persisted on the item first:
    ``ams_mapping`` is the operator's pin until the moment a print actually starts, so
    writing the computed decision over it before the dispatch gates (stagger / USB /
    capability) have cleared would destroy the instruction AND let the decision be
    re-read as a pin on the next tick — the exact stale-materialised-decision shape
    the 2026-08-11 external-spool incident came from. ``_start_print`` records it on
    the item at the point of no return.
    """

    item_id: int
    printer_id: int
    ams_mapping: list[int] | None


class PrintScheduler:
    """Background scheduler that processes the print queue."""

    # Built-in drying presets per filament type (from BambuStudio filament profiles)
    # Format: { n3f_temp, n3s_temp, n3f_hours, n3s_hours }
    DEFAULT_DRYING_PRESETS: dict[str, dict[str, int]] = {
        "PLA": {"n3f": 45, "n3s": 45, "n3f_hours": 12, "n3s_hours": 12},
        "PETG": {"n3f": 65, "n3s": 65, "n3f_hours": 12, "n3s_hours": 12},
        "TPU": {"n3f": 65, "n3s": 75, "n3f_hours": 12, "n3s_hours": 18},
        "ABS": {"n3f": 65, "n3s": 80, "n3f_hours": 12, "n3s_hours": 8},
        "ASA": {"n3f": 65, "n3s": 80, "n3f_hours": 12, "n3s_hours": 8},
        "PA": {"n3f": 65, "n3s": 85, "n3f_hours": 12, "n3s_hours": 12},
        "PC": {"n3f": 65, "n3s": 80, "n3f_hours": 12, "n3s_hours": 8},
        "PVA": {"n3f": 65, "n3s": 85, "n3f_hours": 12, "n3s_hours": 18},
    }

    def __init__(self):
        self._running = False
        self._power_on_wait_time = 180  # seconds to wait for printer after power on (3 min)
        self._power_on_check_interval = 10  # seconds between connection checks
        self._min_drying_seconds = 1800  # 30 minutes minimum before humidity re-check can stop drying
        # Track which printers are currently auto-drying (printer_id -> start timestamp)
        self._drying_in_progress: dict[int, float] = {}
        # Defensive in-memory dispatch hold (#1157): a printer that just received
        # a project_file command must not get a second dispatch until either it
        # transitions out of pre_state OR the hard timeout expires. The H2D Pro
        # can take 80–210 s to flip FINISH→PREPARE after project_file, and
        # during that window the DB busy_printers seed is empirically unreliable
        # (multi-plate batches double-/triple-dispatched onto the same printer
        # 30 s apart). Keyed by printer_id; cleared by the watchdog on success
        # or revert.
        # printer_id -> (monotonic_started_at, pre_state, pre_subtask_id)
        self._dispatch_holds: dict[int, tuple[float, str, str | None]] = {}
        # Minimum cooldown between dispatches to the same printer (covers the
        # H2D's project_file digestion window).
        self._dispatch_min_cooldown = 60.0
        # Hard timeout — drop the hold even if we never observed a transition,
        # so a lost MQTT session can't lock a printer out of the queue forever.
        # Matches the watchdog timeout (90 s) plus a safety margin so the
        # watchdog runs first on the unhappy path.
        self._dispatch_max_hold = 180.0
        # Queue-item ids whose scheduler-made pin was released by a hold gate
        # (USB pre-flight / capability) in _start_print. While an id is in here,
        # the model path suppresses the per-assignment on_queue_job_assigned
        # notification — a sole-idle sick printer is re-selected every 30 s tick,
        # and without this once-guard each re-assignment notifies (an "assigned"
        # message every 30 s, for hours, in a lights-out farm). In-memory only:
        # lost on restart, worst case one duplicate assigned-notification after a
        # server restart — acceptable. Discarded on real dispatch; pruned each
        # tick against the pending set so terminal items drop out.
        self._hold_unpinned_items: set[int] = set()

    async def run(self):
        """Main loop — event-driven with a periodic timeout as the fallback poll.

        Instead of an unconditional ``sleep(interval)`` between passes, the loop
        waits on ``dispatch_kick`` up to ``queue_check_interval_seconds``: a kick
        (enqueue / manual start / plate-gate release / freed printer …) wakes it
        immediately; the interval timeout is only the safety-net poll that
        behaves exactly as the old fixed tick. The interval is re-read every
        iteration so an operator can retune it live.
        """
        self._running = True
        logger.info("Print scheduler started")

        while self._running:
            try:
                await self.check_queue()
            except Exception as e:
                logger.error("Scheduler error: %s", e)

            interval = await self._read_check_interval()
            woke = await dispatch_kick.wait(timeout=interval)
            if woke:
                # Coalesce a burst: a short debounce lets several near-simultaneous
                # kicks collapse into a single check_queue pass. Clear the event
                # BEFORE looping back into check_queue so a kick landing mid-check
                # re-sets it and triggers exactly one follow-up pass (no lost wakeup).
                debounce = await self._read_kick_debounce()
                await asyncio.sleep(debounce)
                dispatch_kick.clear()
                reasons = dispatch_kick.drain_reasons()
                logger.info("Scheduler woken by: %s", DispatchKick.summarize(reasons))

    def stop(self):
        """Stop the scheduler."""
        self._running = False
        logger.info("Print scheduler stopped")

    async def check_queue(self):
        """Check for prints ready to start."""
        async with async_session() as db:
            # Offline-stall watch (Phase 3.2): flag farm units still 'printing'
            # whose printer has been offline past the grace window. One guarded
            # call, mirroring the stagger consumer — a stall-check failure must
            # never kill the dispatch tick. Runs before the pending-item gate so a
            # stall with no pending work is still caught.
            try:
                from backend.app.services.farm_stall import check_stalled_prints

                await check_stalled_prints(db)
            except Exception:
                logger.exception("Offline-stall watch failed (non-fatal)")

            # Pause-stall watch: flag farm units still 'printing' whose CONNECTED
            # printer has sat unattended-PAUSEd past the grace window (an HMS
            # outside the recovery sets, door-open, forgotten manual pause). Its
            # OWN guarded try/except so one watch can't starve the other.
            try:
                from backend.app.services.farm_stall import check_paused_prints

                await check_paused_prints(db)
            except Exception:
                logger.exception("Pause-stall watch failed (non-fatal)")

            # Foreign-pause watch (WS2b): a print the farm did NOT dispatch, PAUSEd
            # past the same grace with no farm unit and no AMS incident owning it.
            # Every other watch starts from a farm queue item, so a vision trip on a
            # LAN print used to sit until a human noticed. Own guard, same as the
            # sibling watches.
            try:
                from backend.app.services.farm_stall import check_foreign_paused_printers

                await check_foreign_paused_printers(db)
            except Exception:
                logger.exception("Foreign-pause watch failed (non-fatal)")

            # Attention-reminder nag (W3): the offline / pause-stall / recovery /
            # runout escalations each alert only ONCE per incident, so a printer left
            # PAUSEd needing a human went silent for hours (2026-07-20). Re-fire the
            # ORIGINAL escalation notification once per hour while the hold persists.
            # Own guard, same as the sibling watches — it must not kill the tick.
            try:
                from backend.app.services.farm_stall import check_attention_reminders

                await check_attention_reminders(db)
            except Exception:
                logger.exception("Attention-reminder watch failed (non-fatal)")

            # Slot-config reconcile (2026-07-24): a refused AMS config write (identify
            # gate / drying) had NO durable retry — the AMS callback is change-gated
            # (bambu_mqtt ams-hash), so once the AMS settles the refused write was lost
            # and the slot stayed unconfigured (bare tray) or on drifted calibration.
            # State-derived re-push each tick; internally throttled + per-slot windowed.
            try:
                from backend.app.services.spool_tagless import reconcile_slot_config

                await reconcile_slot_config(db)
            except Exception:
                logger.exception("Slot-config reconcile failed (non-fatal)")

            # Staged-completeness safety net (D8): the low-spool release triggers
            # (AMS change / run resume / banner button) can all MISS — a
            # fully-loaded printer going idle fires no event, stranding staged
            # units (the incident: two sat for hours). Re-check fleet-wide each
            # tick behind a time debounce; free in steady state (empty staged set
            # short-circuits before any 3MF parse). Own guard so it can't kill
            # dispatch. Runs BEFORE the pending-item query so a freed unit
            # dispatches this same tick.
            try:
                await maybe_release_periodic(db)
            except Exception:
                logger.exception("Periodic staged-release pass failed (non-fatal)")

            # Check if shortest-job-first scheduling is enabled
            sjf_enabled = await self._get_bool_setting(db, "queue_shortest_first")

            # Get all pending items, ordered by printer and position (or SJF order)
            if sjf_enabled:
                # SJF: group by printer (and target_model for model-based jobs),
                # then items already jumped get top priority (starvation guard),
                # then sort by print_time ascending. Items with no print time go last.
                result = await db.execute(
                    select(PrintQueueItem)
                    .where(PrintQueueItem.status == "pending")
                    .order_by(
                        PrintQueueItem.printer_id,
                        PrintQueueItem.target_model,
                        PrintQueueItem.been_jumped.desc(),
                        PrintQueueItem.print_time_seconds.asc().nullslast(),
                        PrintQueueItem.position,
                    )
                )
            else:
                result = await db.execute(
                    select(PrintQueueItem)
                    .where(PrintQueueItem.status == "pending")
                    .order_by(PrintQueueItem.printer_id, PrintQueueItem.position)
                )
            items = list(result.scalars().all())

            # Prune the hold-unpinned once-guard against the live pending set so
            # it can't grow unbounded — cancelled/failed/completed ids drop out
            # automatically (their items are no longer pending).
            self._hold_unpinned_items &= {i.id for i in items}

            if not items:
                # No pending items — still check auto-drying on idle printers
                await self._check_auto_drying(db, [], set())
                return

            logger.info(
                "Queue check: found %d pending items: %s",
                len(items),
                [(i.id, i.printer_id, i.archive_id, i.library_file_id) for i in items],
            )

            # Seed busy_printers with printers that already have an item in 'printing'
            # status. _is_printer_idle() alone is not sufficient as a dispatch gate —
            # on H2D / P1 series the MQTT state transition from IDLE to RUNNING can
            # lag several seconds behind the print command, so the next check_queue
            # tick still sees IDLE and would double-dispatch onto the same printer.
            # Without this guard, two pending items targeting the same printer
            # (e.g. a batch with quantity>1) both end up in 'printing' status —
            # surfaced via the "BUG: Multiple queue items" warning in on_print_complete.
            busy_result = await db.execute(
                select(PrintQueueItem.printer_id)
                .where(PrintQueueItem.status == "printing")
                .where(PrintQueueItem.printer_id.is_not(None))
            )
            busy_printers: set[int] = {pid for (pid,) in busy_result.all() if pid is not None}

            # Defense-in-depth (#1157): augment busy_printers with any printer
            # still in its post-dispatch hold window. Empirically, the DB seed
            # above can miss in-flight items in a multi-plate batch — same-file
            # plates were being dispatched 30 s apart while the H2D was still
            # digesting the first project_file. The hold is keyed in-memory and
            # released by the watchdog on the success path, so it adds a layer
            # that doesn't depend on DB row visibility or completion-callback
            # timing.
            for held_printer_id in list(self._dispatch_holds.keys()):
                if self._printer_in_dispatch_hold(held_printer_id):
                    busy_printers.add(held_printer_id)

            # Power-stagger budget (#Phase4 / Phase E): how many more prints may
            # BEGIN heating this tick. Owned by the stagger_policy module: budget =
            # group_size − (in-flight + still-ramping recent starts), so the bed-
            # temperature dynamic release frees a slot the moment a bed reaches
            # target (the time window stays the hard ceiling). Restart-safe — it
            # re-derives from durable started_at rows + live status. The local
            # ``stagger_remaining`` below is only the INTRA-tick gate; the module's
            # in-flight set (armed by note_dispatch_planned at each plan site) is
            # the cross-tick source of truth that stops a kick landing mid-gather
            # from over-admitting a heater.
            stagger_remaining = await stagger_policy.budget(db)

            # Log skip reasons once per queue check (not per item)
            skip_reasons: dict[str, int] = {}

            # Tick-local head-of-line state (2026-07-12 fix): printers found short
            # on filament THIS tick are excluded from further candidate searches so
            # one short low-id printer no longer swallows the whole model-based run.
            # Never persisted — it dies with the tick, so a spool swap re-opens the
            # printer on the very next tick (this is what makes recovery automatic).
            deficit_blocked: set[int] = set()
            # (batch_id, target_model) groups already sent an all-short waiting
            # notification this tick — so a 20-unit run sends ONE notification, not
            # one per unit (the incident sent 10).
            notified_short_groups: set[tuple[int | None, str | None]] = set()

            # Tick-local dispatch plan (latency Phase B): selection/gating below
            # stays sequential on THIS session (it mutates busy_printers, stagger
            # budget, deficit bookkeeping and persists AMS mappings), but instead of
            # awaiting the slow _start_print inline it records (queue_item_id,
            # printer_id) pairs here. After both selection loops finish, the plan is
            # dispatched concurrently so one printer's slow FTPS upload no longer
            # pushes another printer's dispatch to a later tick. ``planned_printers``
            # enforces one printer per tick's plan (point 6).
            dispatch_plan: list[_PlannedDispatch] = []
            planned_printers: set[int] = set()

            for item in items:
                # Check scheduled time first (scheduled_time is stored in UTC from ISO string)
                if item.scheduled_time:
                    sched = item.scheduled_time
                    if sched.tzinfo is None:
                        sched = sched.replace(tzinfo=timezone.utc)
                    if sched > datetime.now(timezone.utc):
                        skip_reasons["scheduled_future"] = skip_reasons.get("scheduled_future", 0) + 1
                        continue

                # Skip items that require manual start
                if item.manual_start:
                    skip_reasons["manual_start"] = skip_reasons.get("manual_start", 0) + 1
                    continue

                if item.printer_id:
                    # Specific printer assignment (existing behavior)
                    if item.printer_id in busy_printers:
                        continue

                    # Check if printer is idle. The plate-clear gate is now
                    # unconditional (Phase 1, P1-B) — a raised gate blocks dispatch
                    # regardless of the global convenience toggle.
                    printer_idle = self._is_printer_idle(item.printer_id)
                    printer_connected = printer_manager.is_connected(item.printer_id)

                    # If printer not connected, try to power on via smart plug
                    if not printer_connected:
                        plugs = await self._get_smart_plugs(db, item.printer_id)
                        auto_on_plugs = [p for p in plugs if p.auto_on and p.enabled]
                        if auto_on_plugs:
                            logger.info("Printer %s offline, attempting to power on via smart plug(s)", item.printer_id)
                            # Power on using the first auto_on plug (the printer power plug)
                            powered_on = await self._power_on_and_wait(auto_on_plugs[0], item.printer_id, db)
                            if powered_on:
                                # Also turn on any remaining auto_on plugs (e.g., filter)
                                for extra_plug in auto_on_plugs[1:]:
                                    try:
                                        service = await smart_plug_manager.get_service_for_plug(extra_plug, db)
                                        await service.turn_on(extra_plug)
                                        logger.info(
                                            "Also powered on plug '%s' for printer %s", extra_plug.name, item.printer_id
                                        )
                                    except Exception as e:
                                        logger.warning("Failed to power on extra plug '%s': %s", extra_plug.name, e)
                                printer_connected = True
                                printer_idle = self._is_printer_idle(item.printer_id)
                            else:
                                logger.warning("Could not power on printer %s via smart plug", item.printer_id)
                                busy_printers.add(item.printer_id)
                                continue
                        else:
                            # No plug or auto_on disabled
                            busy_printers.add(item.printer_id)
                            continue

                    # Check if printer is idle (busy with another print)
                    if not printer_idle:
                        # If printer is drying (not truly busy), handle based on queue_drying_block
                        if self._drying_in_progress.get(item.printer_id):
                            block_for_drying = await self._get_bool_setting(db, "queue_drying_block")
                            if block_for_drying:
                                # Drying blocks queue — skip this printer
                                busy_printers.add(item.printer_id)
                                continue
                            else:
                                # Print takes priority — stop drying
                                await self._stop_drying(item.printer_id)
                                # Re-check idle after stopping drying
                                printer_idle = self._is_printer_idle(item.printer_id)
                                if not printer_idle:
                                    busy_printers.add(item.printer_id)
                                    continue
                        else:
                            busy_printers.add(item.printer_id)
                            continue

                    # Check condition (previous print success)
                    if item.require_previous_success:
                        if not await self._check_previous_success(db, item):
                            item.status = "skipped"
                            item.error_message = "Previous print failed or was aborted"
                            # Machine code for the UI (Phase 4.3f): the queue
                            # banner matches this, never the English message.
                            item.waiting_reason = "previous_print_failed"
                            item.completed_at = datetime.now(timezone.utc)
                            await db.commit()
                            logger.info("Skipped queue item %s - previous print failed", item.id)

                            # Send notification
                            job_name = await self._get_job_name(db, item)
                            printer = await self._get_printer(db, item.printer_id)
                            await notification_service.on_queue_job_skipped(
                                job_name=job_name,
                                printer_id=item.printer_id,
                                printer_name=printer.name if printer else "Unknown",
                                reason="Previous print failed or was aborted",
                                db=db,
                            )
                            continue

                    # Decide the AMS mapping — EVERY tick, against live tray state.
                    # There is no "use the stored one" branch: a stored value is the
                    # operator's PIN (an input the matcher honours), never a previous
                    # result to replay (2026-08-11 external-spool incident).
                    outcome = await self._compute_ams_mapping_for_printer(db, item.printer_id, item)
                    # Four ways a decision can refuse to dispatch, each reading its OWN
                    # record on the outcome and each owning its lane. Ordered by which
                    # answer is most true, not by convenience: evidence first (a read may
                    # dissolve the whole question), then a named tray that is simply
                    # absent, then the floor's own vocabulary, then the residue.
                    if (outcome.start_blocked_slots or outcome.pin_missing or not outcome.is_total) and (
                        await self._hold_for_unread(db, item, [item.printer_id])
                    ):
                        # A requirement went unmatched (or matched only a below-floor
                        # donor) while the printer still holds a seated-but-unidentified
                        # roll — the material may be right there. Hold for the read;
                        # nothing is written to the item, so the read that fixes it
                        # decides the next tick.
                        continue
                    if await self._hold_for_pinned_tray(db, item, outcome):
                        continue
                    if outcome.start_blocked_slots:
                        # No matching spool can be PROVEN to clear the minimum-start
                        # floor — hold the job with a distinct reason naming which
                        # proof failed (below the floor: they stay loaded as firmware
                        # backup donors; unpriced: the farm has no weight for them).
                        # Do NOT persist a mapping. Notify once per transition,
                        # mirroring the filament-deficit path.
                        # Already low-spool staged? The durable FLAG is the
                        # transition signal (token-independent now the reason
                        # is a rich string) — dedup the once-per-transition
                        # notification off it.
                        was_blocked = bool(item.filament_short)
                        prior_reason = item.waiting_reason
                        printer = await self._get_printer(db, item.printer_id)
                        stage_reason = build_staged_reason(
                            printer.name if printer else "", start_block=outcome.start_block_kind
                        )
                        await self._stage_filament_short(db, item, unpin=False, reason=stage_reason)
                        logger.info(
                            "Queue item %s: no startable spool on printer %s (slots %s, reason %s) — staged",
                            item.id,
                            item.printer_id,
                            outcome.start_blocked_slots,
                            outcome.start_block_kind,
                        )
                        if self._hold_is_new(was_held=was_blocked, prior_reason=prior_reason, reason=stage_reason):
                            await self._notify_queue_waiting(
                                db, item, stage_reason, (printer.model if printer else "") or ""
                            )
                        continue
                    if not outcome.is_total:
                        # Some requirement resolved to nothing at all (no roll of that
                        # material is loaded and startable). The outcome contract is
                        # TOTAL: dispatching now would send a partial ``-1`` mapping,
                        # which on the wire means "nothing feeds this extruder". Carry it
                        # on the same staging lane a grams shortage uses — same operator
                        # action (put the right roll in), same release path.
                        was_blocked = bool(item.filament_short)
                        prior_reason = item.waiting_reason
                        printer = await self._get_printer(db, item.printer_id)
                        stage_reason = build_staged_reason(printer.name if printer else "")
                        await self._stage_filament_short(db, item, unpin=False, reason=stage_reason)
                        logger.info(
                            "Queue item %s: printer %s has no loaded match for slot(s) %s — staged "
                            "(a partial mapping is never dispatched)",
                            item.id,
                            item.printer_id,
                            list(outcome.unmatched_slots),
                        )
                        if self._hold_is_new(was_held=was_blocked, prior_reason=prior_reason, reason=stage_reason):
                            await self._notify_queue_waiting(
                                db, item, stage_reason, (printer.model if printer else "") or ""
                            )
                        continue

                    if outcome.mapping:
                        logger.info(
                            "Queue item %s: Computed AMS mapping for printer %s: %s",
                            item.id,
                            item.printer_id,
                            outcome.mapping,
                        )
                        # Advisory: does the picked tray actually have a firmware
                        # backup partner? (010-H2S shape 33 — never blocks dispatch.)
                        await self._warn_backup_group_gap(db, item.printer_id, outcome.mapping)

                    # Filament-deficit pre-dispatch check (#1496). If the
                    # assigned spool can't satisfy any required slot grams,
                    # promote the item to manual_start so the user must
                    # acknowledge via the ▶ button (which re-checks live). Checked
                    # against THIS tick's decision (the override), never against the
                    # stored field — which now holds pins, not a mapping.
                    if await self._block_on_filament_deficit(db, item, ams_mapping_override=mapping_json(outcome)):
                        continue

                    # Power-stagger gate: hold if this window's start budget is
                    # spent. The item stays pending, marked stagger_hold for UI
                    # visibility (self-clearing token — NEVER notified), and is
                    # retried next tick.
                    if stagger_remaining <= 0:
                        skip_reasons["stagger_window"] = skip_reasons.get("stagger_window", 0) + 1
                        if item.waiting_reason != "stagger_hold":
                            item.waiting_reason = "stagger_hold"
                            await db.commit()
                        logger.debug("Queue item %s: holding — stagger window budget exhausted", item.id)
                        continue

                    # Clear a stale stagger_hold before dispatch (self-clearing token;
                    # _start_print commits it). Other tokens are managed elsewhere.
                    if item.waiting_reason == "stagger_hold":
                        item.waiting_reason = None
                    # The printer targeted by this dispatch, captured for the SJF
                    # been_jumped marking below. Under Phase B the actual dispatch
                    # (and any USB/capability un-pin it performs) runs later on its
                    # own session, so this tick session's item.printer_id is stable
                    # through the marking — but keep the local for clarity and parity
                    # with the model path.
                    dispatch_printer_id = item.printer_id
                    # Plan the dispatch (latency Phase B): the slow FTPS upload +
                    # start command runs concurrently AFTER both selection loops
                    # finish (in _start_print_by_id on its own session), so one
                    # printer's slow upload no longer delays another's dispatch to a
                    # later tick. Selection stays sequential, so busy_printers /
                    # stagger budget / SJF bookkeeping below still observe every
                    # prior pick this tick. A user-pinned item's printer_id is already
                    # persisted; the decided mapping rides the PLAN (see
                    # _PlannedDispatch) rather than the item, so the pin survives until
                    # the print actually starts.
                    self._plan_dispatch(dispatch_plan, planned_printers, item.id, dispatch_printer_id, outcome.mapping)
                    busy_printers.add(dispatch_printer_id)
                    # Enter the module in-flight set (Phase E): this is the durable-
                    # across-kicks record of an admitted-but-not-yet-started heater
                    # and arms the bed-at-target ramp-watch. The local decrement
                    # below stays as the intra-tick gate.
                    stagger_policy.note_dispatch_planned(dispatch_printer_id, item.id)
                    # Consume a stagger-window slot at PLAN time: the gate below must
                    # see this pick when deciding later items THIS tick, and the real
                    # "printing" outcome isn't known until the concurrent phase.
                    stagger_remaining -= 1

                    # SJF starvation guard: mark items that were jumped. Compare against
                    # the captured pre-dispatch printer id — never item.printer_id, which
                    # a held model unit will have nulled (None would match every
                    # still-unassigned item and wrongly flag it been_jumped).
                    if sjf_enabled and item.print_time_seconds is not None:
                        for other in items:
                            if (
                                other.id != item.id
                                and other.status == "pending"
                                and other.printer_id == dispatch_printer_id
                                and not other.been_jumped
                                and other.position < item.position
                                and (
                                    other.print_time_seconds is None
                                    or other.print_time_seconds > item.print_time_seconds
                                )
                            ):
                                other.been_jumped = True
                        await db.commit()

                elif item.target_model:
                    # Model-based assignment - find any idle printer of matching model
                    # Parse required filament types if present
                    required_types = None
                    if item.required_filament_types:
                        try:
                            required_types = json.loads(item.required_filament_types)
                        except json.JSONDecodeError:
                            pass  # Ignore malformed filament types; treat as no constraint

                    # Parse filament overrides if present
                    filament_overrides = None
                    if item.filament_overrides:
                        try:
                            filament_overrides = json.loads(item.filament_overrides)
                        except json.JSONDecodeError:
                            pass

                    # If overrides exist, use override types for validation instead
                    effective_types = required_types
                    if filament_overrides:
                        override_types = sorted({o["type"] for o in filament_overrides if "type" in o})
                        if override_types:
                            # Merge: keep original types for non-overridden slots, add override types
                            effective_types = sorted(set(required_types or []) | set(override_types))

                    # Head-of-line fix (2026-07-12): evaluate candidate printers one
                    # at a time, excluding both busy printers and any candidate found
                    # short on filament THIS tick. A candidate is only claimed once it
                    # passes its OWN deficit check, so a short low-id printer no longer
                    # swallows the whole run by staging every unit onto itself.
                    assigned_printer_id: int | None = None
                    assigned_mapping: list[int] | None = None
                    last_waiting_reason: str | None = None
                    candidates_deficit_blocked = 0
                    candidates_start_blocked = 0
                    # Candidates rejected because a requirement resolved to NOTHING on
                    # them (no loaded match) or because an operator PIN named a tray
                    # they do not hold. Counted apart from the grams lanes: the first
                    # shares the "put the right roll in" staging answer, the second is
                    # a missing NAMED roll and gets its own honest token.
                    candidates_unmatched = 0
                    candidates_pin_blocked = 0
                    pin_missing_seen: dict[int, int] = {}
                    # START_BLOCK_* kinds seen across the candidates this pass —
                    # collapsed into the ONE kind that words the staged reason.
                    start_block_kinds: set[str] = set()
                    # Short candidates found THIS item's pass — named in the
                    # staged reason (D9) so the banner tells the operator which
                    # machines to top up.
                    blocked_candidate_ids: list[int] = []
                    while True:
                        candidate_id, last_waiting_reason = await self._find_idle_printer_for_model(
                            db,
                            item.target_model,
                            busy_printers | deficit_blocked,
                            effective_types,
                            item.target_location,
                            filament_overrides=filament_overrides,
                        )
                        if not candidate_id:
                            break

                        # Decide THIS candidate's AMS mapping against its own live tray
                        # state, WITHOUT persisting it — a losing candidate must leave
                        # no trace on the item, and there is no printer-agnostic stored
                        # mapping to reuse (a mapping is meaningful only for the printer
                        # it was decided on: reusing one across the fleet is what put a
                        # printer-1 external pick on nine other machines).
                        outcome = await self._compute_ams_mapping_for_printer(db, candidate_id, item)
                        candidate_mapping = mapping_json(outcome)
                        candidate_start_blocked = outcome.start_blocked_slots
                        candidate_block_kind = outcome.start_block_kind

                        # Deficit-check against THIS candidate via the override params
                        # (the item is never mutated). Print-Anyway skips the check.
                        if item.skip_filament_check:
                            deficit: list = []
                        else:
                            deficit = await self._compute_deficit_safe(
                                db,
                                item,
                                printer_id_override=candidate_id,
                                ams_mapping_override=candidate_mapping,
                            )
                        if deficit:
                            deficit_blocked.add(candidate_id)
                            blocked_candidate_ids.append(candidate_id)
                            candidates_deficit_blocked += 1
                            logger.info(
                                "Queue item %s: candidate printer %s short on filament (%d slot(s)) — trying next",
                                item.id,
                                candidate_id,
                                len(deficit),
                            )
                            continue

                        # Start-spool floor: none of this candidate's matching spool(s)
                        # can be proven to clear the minimum-start weight. Skip it like a
                        # deficit (it can still finish other prints / serve as a backup
                        # donor).
                        if candidate_start_blocked and not item.skip_filament_check:
                            deficit_blocked.add(candidate_id)
                            blocked_candidate_ids.append(candidate_id)
                            candidates_start_blocked += 1
                            if candidate_block_kind:
                                start_block_kinds.add(candidate_block_kind)
                            logger.info(
                                "Queue item %s: candidate printer %s has no startable spool "
                                "(slots %s, reason %s) — trying next",
                                item.id,
                                candidate_id,
                                candidate_start_blocked,
                                candidate_block_kind,
                            )
                            continue

                        # Operator PIN naming a tray this candidate does not hold. Not
                        # a shortage — skip the candidate and keep searching; another
                        # printer may well have the pinned tray.
                        if outcome.pin_missing:
                            deficit_blocked.add(candidate_id)
                            candidates_pin_blocked += 1
                            pin_missing_seen.update(outcome.pin_missing)
                            logger.info(
                                "Queue item %s: candidate printer %s does not hold pinned tray(s) %s — trying next",
                                item.id,
                                candidate_id,
                                sorted(set(outcome.pin_missing.values())),
                            )
                            continue

                        # Total-outcome contract: a candidate that leaves ANY
                        # requirement unresolved is not a candidate. Dispatching it
                        # would send a partial ``-1`` mapping ("nothing feeds this
                        # extruder") to the printer.
                        if not outcome.is_total:
                            deficit_blocked.add(candidate_id)
                            blocked_candidate_ids.append(candidate_id)
                            candidates_unmatched += 1
                            logger.info(
                                "Queue item %s: candidate printer %s has no loaded match for slot(s) %s — trying next",
                                item.id,
                                candidate_id,
                                list(outcome.unmatched_slots),
                            )
                            continue

                        assigned_printer_id = candidate_id
                        assigned_mapping = outcome.mapping
                        break

                    if assigned_printer_id:
                        # Power-stagger gate — now AFTER candidate selection + deficit
                        # check so shortages surface even during held windows. A held
                        # item is marked stagger_hold (self-clearing, NEVER notified).
                        if stagger_remaining <= 0:
                            skip_reasons["stagger_window"] = skip_reasons.get("stagger_window", 0) + 1
                            if item.waiting_reason != "stagger_hold":
                                item.waiting_reason = "stagger_hold"
                                await db.commit()
                            logger.debug(
                                "Queue item %s: holding model-based dispatch — stagger window budget exhausted",
                                item.id,
                            )
                            continue

                        # Check condition (previous print success) before assigning
                        if item.require_previous_success:
                            if not await self._check_previous_success(db, item):
                                item.status = "skipped"
                                item.error_message = "Previous print failed or was aborted"
                                # Machine code for the UI (Phase 4.3f) — see the
                                # assigned-printer skip site above.
                                item.waiting_reason = "previous_print_failed"
                                item.completed_at = datetime.now(timezone.utc)
                                await db.commit()
                                logger.info("Skipped queue item %s - previous print failed", item.id)

                                # Send notification
                                job_name = await self._get_job_name(db, item)
                                printer = await self._get_printer(db, assigned_printer_id)
                                await notification_service.on_queue_job_skipped(
                                    job_name=job_name,
                                    printer_id=assigned_printer_id,
                                    printer_name=printer.name if printer else "Unknown",
                                    reason="Previous print failed or was aborted",
                                    db=db,
                                )
                                continue

                        # Assign the printer. Clear a stale
                        # assignment-time waiting reason, but PRESERVE a live
                        # "no_usb_drive" hold: _start_print owns that token and
                        # self-clears it on a successful dispatch (past the capability
                        # gate below). When a model unit keeps landing on USB-less
                        # printers, preserving it here keeps the USB hold's
                        # once-per-transition waiting notification deduped across ticks —
                        # this optimistic clear would otherwise make every tick look like
                        # a fresh transition and re-notify.
                        item.printer_id = assigned_printer_id
                        if assigned_mapping:
                            logger.info(
                                "Queue item %s: Computed AMS mapping for printer %s: %s",
                                item.id,
                                assigned_printer_id,
                                assigned_mapping,
                            )
                            # Advisory: does the picked tray actually have a firmware
                            # backup partner? (010-H2S shape 33 — never blocks dispatch.)
                            await self._warn_backup_group_gap(db, assigned_printer_id, assigned_mapping)
                        if item.waiting_reason != "no_usb_drive":
                            item.waiting_reason = None
                        logger.info(
                            "Model-based assignment: queue item %s assigned to printer %s",
                            item.id,
                            assigned_printer_id,
                        )

                        # Send assignment notification — suppressed while the item
                        # sits in the hold-unpinned once-guard: a sole-idle sick
                        # printer is re-selected every tick after the hold gates
                        # release the pin, and on_queue_job_assigned has no dedupe
                        # of its own. First assignment notified; re-assignments
                        # born from a hold-release stay silent.
                        if item.id not in self._hold_unpinned_items:
                            job_name = await self._get_job_name(db, item)
                            printer = await self._get_printer(db, assigned_printer_id)
                            await notification_service.on_queue_job_assigned(
                                job_name=job_name,
                                printer_id=assigned_printer_id,
                                printer_name=printer.name if printer else "Unknown",
                                target_model=item.target_model,
                                db=db,
                            )

                        # Persist the assignment (printer_id) BEFORE planning: the
                        # concurrent _start_print_by_id re-fetches this item on its OWN
                        # session, so an uncommitted assignment would be invisible
                        # there. The decided mapping is NOT persisted here — it rides
                        # the plan and is recorded when the print actually starts, so a
                        # unit held at a later gate keeps its operator pin intact.
                        await db.commit()
                        self._plan_dispatch(
                            dispatch_plan, planned_printers, item.id, assigned_printer_id, assigned_mapping
                        )
                        busy_printers.add(assigned_printer_id)
                        # Enter the module in-flight set + arm the ramp-watch (Phase
                        # E; see direct-path note).
                        stagger_policy.note_dispatch_planned(assigned_printer_id, item.id)
                        # Consume a stagger slot at plan time (see direct-path note).
                        stagger_remaining -= 1

                        # SJF starvation guard: mark model-based items that were jumped
                        if sjf_enabled and item.print_time_seconds is not None:
                            for other in items:
                                if (
                                    other.id != item.id
                                    and other.status == "pending"
                                    and other.printer_id is None
                                    and other.target_model
                                    and other.target_model.upper() == item.target_model.upper()
                                    and not other.been_jumped
                                    and other.position < item.position
                                    and (
                                        other.print_time_seconds is None
                                        or other.print_time_seconds > item.print_time_seconds
                                    )
                                ):
                                    other.been_jumped = True
                            await db.commit()

                    elif candidates_pin_blocked > 0 and candidates_deficit_blocked == 0:
                        # Every candidate that could have run was rejected for the
                        # pinned tray it does not hold (and nothing was merely short).
                        # A missing NAMED roll is not a shortage, so it takes the honest
                        # token rather than the "Low filament" staging lane: the item
                        # stays pending and un-promoted, and the tick it is loaded on is
                        # the tick it dispatches.
                        await self._hold_for_pinned_tray(
                            db, item, MatchOutcome(mapping=None, pin_missing=pin_missing_seen)
                        )
                    elif candidates_deficit_blocked > 0 or candidates_start_blocked > 0 or candidates_unmatched > 0:
                        # Every candidate that could have run was blocked on filament →
                        # stage the item UNPINNED so a later tick re-runs the full
                        # candidate search once any printer's spool is topped up. One
                        # notification per (batch, model) group per tick — the incident
                        # sent one per unit (10 for a 10-plate run). D9: NAME the short
                        # machines in the persisted reason (the model log named nothing
                        # persistent) so the queue banner tells the operator which
                        # printer to top up. Purely start-floor blocks read "below
                        # minimum"; a mix stays generic ("needs more filament").
                        # Same phantom-deficit guard as the pinned path, applied to the
                        # candidates that were actually blocked: if any of them holds a
                        # seated-but-unidentified roll, the run is not short — the farm
                        # just cannot see the material yet. Hold un-staged and un-pinned
                        # (the item was never pinned on this branch) for the read.
                        if not await self._hold_for_unread(db, item, list(blocked_candidate_ids)):
                            # "Below minimum" only when the floor is the WHOLE story —
                            # a grams shortage or an unmatched requirement anywhere in
                            # the candidate set makes the generic wording the true one.
                            start_min_only = (
                                candidates_deficit_blocked == 0
                                and candidates_unmatched == 0
                                and candidates_start_blocked > 0
                            )
                            blocked_names = await self._resolve_printer_names(db, blocked_candidate_ids)
                            who = ", ".join(blocked_names) if blocked_names else f"{item.target_model} printers"
                            stage_reason = build_staged_reason(
                                who,
                                start_block=dominant_start_block(start_block_kinds) if start_min_only else None,
                            )
                            await self._stage_model_item_filament_short(
                                db, item, notified_short_groups, reason=stage_reason
                            )

                    else:
                        # No eligible printer for a non-filament reason (all busy /
                        # offline / none configured). Preserve the transition-notify
                        # behaviour; the self-clearing tokens never notify.
                        if item.waiting_reason != last_waiting_reason:
                            was_waiting = item.waiting_reason is not None
                            item.waiting_reason = last_waiting_reason
                            await db.commit()

                            # Send waiting notification only when transitioning to
                            # waiting and the reason requires user action.
                            if last_waiting_reason and not was_waiting and not self._is_busy_only(last_waiting_reason):
                                await self._notify_queue_waiting(db, item, last_waiting_reason, item.target_model)

            # Concurrent dispatch (latency Phase B): selection above ran
            # sequentially and recorded (queue_item_id, printer_id) pairs; now fire
            # the slow per-printer work (FTPS delete+upload + start command) in
            # parallel — bounded by dispatch_parallel_limit — so a slow upload to
            # printer A no longer delays printer B's dispatch to the next tick. Each
            # task opens its OWN session and re-fetches, so the tick session's
            # in-loop assignments must be durable first: read the limit, then a
            # single final commit releases this session's transaction and persists
            # any pending assignment/waiting_reason writes before the re-fetch. The
            # gather awaits here, so check_queue's single-flight invariant holds.
            if dispatch_plan:
                limit = await self._read_dispatch_parallel_limit(db)
                await db.commit()
                sem = asyncio.Semaphore(limit)
                await asyncio.gather(
                    *(
                        self._start_print_by_id(planned.item_id, planned.printer_id, sem, planned.ams_mapping)
                        for planned in dispatch_plan
                    ),
                    return_exceptions=True,
                )

            # Log summary of skip reasons (helps diagnose why queue items aren't starting)
            if skip_reasons:
                logger.info("Queue skip summary: %s", skip_reasons)
            if busy_printers:
                # Log why each printer was busy (first time it was checked)
                for pid in busy_printers:
                    state = printer_manager.get_status(pid)
                    connected = printer_manager.is_connected(pid)
                    awaiting = printer_manager.is_awaiting_plate_clear(pid)
                    state_name = state.state if state else "NO_STATUS"
                    logger.info(
                        "Queue: printer %d not available — connected=%s, state=%s, awaiting_plate_clear=%s",
                        pid,
                        connected,
                        state_name,
                        awaiting,
                    )

            # Auto-drying: start drying on idle printers that have no pending queue items
            await self._check_auto_drying(db, items, busy_printers)

    async def _find_idle_printer_for_model(
        self,
        db: AsyncSession,
        model: str,
        exclude_ids: set[int],
        required_filament_types: list[str] | None = None,
        target_location: str | None = None,
        filament_overrides: list[dict] | None = None,
    ) -> tuple[int | None, str | None]:
        """Find an idle, connected printer matching the model with compatible filaments.

        Args:
            db: Database session
            model: Printer model to match (e.g., "X1C", "P1S")
            exclude_ids: Printer IDs to exclude (already busy)
            required_filament_types: Optional list of filament types needed (e.g., ["PLA", "PETG"])
                                     If provided, only printers with all required types loaded will match.
            target_location: Optional location filter. If provided, only printers in this location are considered.
            filament_overrides: Optional list of override dicts. Each entry may include
                                 ``force_color_match: true`` to require an exact type+color match
                                 on the printer for that slot. Without the flag the existing
                                 colour-preference logic applies.

        Returns:
            Tuple of (printer_id, waiting_reason):
            - (printer_id, None) if a matching printer was found
            - (None, reason) if no printer is available, with explanation
        """
        # Normalize model name and use case-insensitive matching
        normalized_model = normalize_printer_model(model) or model
        query = (
            select(Printer)
            .where(func.lower(Printer.model) == normalized_model.lower())
            .where(Printer.is_active == True)  # noqa: E712
            .where(Printer.quarantined == False)  # noqa: E712 — farm quarantine excludes from dispatch
        )

        # Add location filter if specified
        if target_location:
            query = query.where(Printer.location == target_location)

        result = await db.execute(query)
        printers = list(result.scalars().all())

        location_suffix = f" in {target_location}" if target_location else ""
        if not printers:
            return None, f"No active {normalized_model} printers{location_suffix} configured"

        # Separate force-matched overrides from preference-only overrides
        force_overrides = [o for o in (filament_overrides or []) if o.get("force_color_match")]
        pref_overrides = [o for o in (filament_overrides or []) if not o.get("force_color_match")]

        # Track reasons for skipping printers
        printers_busy = []
        printers_offline = []
        printers_missing_filament: list[tuple[str, list[str]]] = []
        candidates: list[tuple[int, int]] = []  # (printer_id, color_match_count)

        for printer in printers:
            if printer.id in exclude_ids:
                # Printer is already claimed by another job in this scheduling run.
                # For force-color jobs, still check if the color would match — if not,
                # report it as a color mismatch rather than plain "Busy" so the user
                # knows the job needs a filament change, not just to wait for availability.
                if force_overrides and not pref_overrides:
                    missing_colors = self._get_missing_force_color_slots(printer.id, force_overrides)
                    if missing_colors:
                        printers_missing_filament.append((printer.name, missing_colors))
                        continue
                printers_busy.append(printer.name)
                continue

            is_connected = printer_manager.is_connected(printer.id)
            is_idle = self._is_printer_idle(printer.id) if is_connected else False

            if not is_connected:
                printers_offline.append(printer.name)
                continue

            if not is_idle:
                # Printer is currently printing.  For force-color jobs, check whether the
                # loaded color would satisfy the requirement — if not, surface it as a
                # color-mismatch reason rather than plain "Busy" so the user understands
                # that the job is waiting for a filament change, not just printer availability.
                if force_overrides and not pref_overrides:
                    missing_colors = self._get_missing_force_color_slots(printer.id, force_overrides)
                    if missing_colors:
                        printers_missing_filament.append((printer.name, missing_colors))
                        logger.debug(
                            "Printer %s (%s) is busy but also has wrong force-color: %s",
                            printer.id,
                            printer.name,
                            missing_colors,
                        )
                        continue
                printers_busy.append(printer.name)
                continue

            # Validate filament compatibility if required types are specified
            if required_filament_types:
                missing = self._get_missing_filament_types(printer.id, required_filament_types)
                if missing:
                    # When force_overrides are present, enrich missing entries with color info
                    # so the "Waiting on" message includes "TYPE (color)" instead of just "TYPE"
                    if force_overrides:
                        force_color_map = {
                            (o.get("type") or "").upper(): o.get("color_name") or o.get("color", "?")
                            for o in force_overrides
                        }
                        missing_enriched = [
                            f"{t} ({force_color_map[t_upper]})" if (t_upper := t.upper()) in force_color_map else t
                            for t in missing
                        ]
                        printers_missing_filament.append((printer.name, missing_enriched))
                    else:
                        printers_missing_filament.append((printer.name, missing))
                    logger.debug("Skipping printer %s (%s) - missing filaments: %s", printer.id, printer.name, missing)
                    continue

            # Force color match: ALL flagged slots must have an exact type+color match
            if force_overrides:
                missing_colors = self._get_missing_force_color_slots(printer.id, force_overrides)
                if missing_colors:
                    printers_missing_filament.append((printer.name, missing_colors))
                    logger.debug(
                        "Skipping printer %s (%s) - missing force-matched colors: %s",
                        printer.id,
                        printer.name,
                        missing_colors,
                    )
                    continue

            # If preference-only overrides exist, rank by color matches (existing behaviour)
            if pref_overrides:
                color_matches = self._count_override_color_matches(printer.id, pref_overrides)
                if color_matches > 0:
                    candidates.append((printer.id, color_matches))
                else:
                    override_colors = [f"{o.get('type', '?')} ({o.get('color', '?')})" for o in pref_overrides]
                    printers_missing_filament.append((printer.name, override_colors))
                    logger.debug("Skipping printer %s (%s) - no matching override colors", printer.id, printer.name)
                    continue
            elif force_overrides:
                # Passed all force checks — immediately eligible (no preference ordering needed)
                return printer.id, None
            else:
                # No overrides at all - take first available (existing behavior)
                return printer.id, None

        # If we have candidates from preference override matching, pick the one with most color matches
        if candidates:
            candidates.sort(key=lambda c: c[1], reverse=True)
            return candidates[0][0], None

        # Build waiting reason from what we found
        reasons = []
        if printers_missing_filament:
            # Filament/color mismatch is most actionable - show first
            if force_overrides and not pref_overrides:
                # All mismatches are force-color failures — use descriptive message only;
                # but only if there are no busy printers that DO have the matching color.
                # If a printer has the right color but is busy, surface "Busy" instead so
                # the user knows the job will start automatically once that printer is free.
                if not printers_busy:
                    all_missing = sorted({c for _, cols in printers_missing_filament for c in cols})
                    return None, f"No matching material/color. Waiting on {', '.join(all_missing)}"
                # else: fall through — printers_busy will be appended below
            else:
                names_and_missing = [
                    f"{name} (needs {', '.join(missing)})" for name, missing in printers_missing_filament
                ]
                reasons.append(f"Waiting for filament: {'; '.join(names_and_missing)}")
        if printers_busy:
            reasons.append(f"Busy: {', '.join(printers_busy)}")
        if printers_offline:
            reasons.append(f"Offline: {', '.join(printers_offline)}")

        return None, " | ".join(reasons) if reasons else f"No available {model} printers{location_suffix}"

    async def _resolve_printer_names(self, db: AsyncSession, printer_ids: list[int]) -> list[str]:
        """Resolve printer ids to names, de-duplicated and input-order preserving.

        Names the short candidates in a model-based low-spool staging reason so
        the operator knows WHICH machines to top up (the D9 incident: the model
        staging log named nothing persistent). Missing rows are skipped; an empty
        input returns ``[]``.
        """
        if not printer_ids:
            return []
        result = await db.execute(select(Printer.id, Printer.name).where(Printer.id.in_(printer_ids)))
        names_by_id = dict(result.all())
        seen: set[int] = set()
        out: list[str] = []
        for pid in printer_ids:
            if pid in names_by_id and pid not in seen:
                seen.add(pid)
                out.append(names_by_id[pid])
        return out

    @staticmethod
    def _is_busy_only(waiting_reason: str) -> bool:
        """Check if the waiting reason only contains 'Busy' entries.

        When all matching printers are simply busy printing, the queued job
        will start automatically once a printer finishes — no user action
        is required, so we skip the notification.
        """
        parts = [p.strip() for p in waiting_reason.split(" | ")]
        return all(p.startswith("Busy:") for p in parts)

    def _get_missing_force_color_slots(self, printer_id: int, force_overrides: list[dict]) -> list[str]:
        """Return descriptive strings for force_color_match slots not satisfied by the printer.

        Each entry in ``force_overrides`` must have ``type`` and ``color`` fields and is expected
        to carry ``force_color_match: True``.  The printer must have **every** such slot loaded
        with an exact type+color match.

        Returns:
            List of ``"TYPE (color)"`` strings for unmatched slots (empty list means all match).
        """
        status = printer_manager.get_status(printer_id)
        if not status:
            return [f"{o.get('type', '?')} ({o.get('color_name') or o.get('color', '?')})" for o in force_overrides]

        # Build set of loaded type+colour pairs from AMS and external spool
        loaded: set[tuple[str, str]] = set()
        for ams_unit in status.raw_data.get("ams", []):
            for tray in ams_unit.get("tray", []):
                tray_type = tray.get("tray_type")
                tray_color = tray.get("tray_color", "")
                if tray_type:
                    color_norm = tray_color.replace("#", "").lower()[:6]
                    loaded.add((_canonical_filament_type(tray_type), color_norm))
        for vt in status.raw_data.get("vt_tray") or []:
            vt_type = vt.get("tray_type")
            if vt_type:
                color_norm = (vt.get("tray_color", "") or "").replace("#", "").lower()[:6]
                loaded.add((_canonical_filament_type(vt_type), color_norm))

        missing = []
        for o in force_overrides:
            o_type = _canonical_filament_type(o.get("type") or "")
            o_color = (o.get("color") or "").replace("#", "").lower()[:6]
            if (o_type, o_color) not in loaded:
                color_label = o.get("color_name") or o.get("color", "?")
                missing.append(f"{o_type} ({color_label})")
        return missing

    def _get_missing_filament_types(self, printer_id: int, required_types: list[str]) -> list[str]:
        """Get the list of required filament types that are not loaded on the printer.

        Args:
            printer_id: The printer ID
            required_types: List of filament types needed (e.g., ["PLA", "PETG"])

        Returns:
            List of missing filament types (empty if all are loaded)
        """
        status = printer_manager.get_status(printer_id)
        if not status:
            return required_types  # Can't determine, assume all missing

        # Collect all filament types loaded on this printer (AMS units + external spool)
        # Use canonical types so equivalence groups (e.g. PA-CF/PA12-CF/PAHT-CF) match.
        loaded_types: set[str] = set()

        # Check AMS units (stored in raw_data["ams"])
        ams_data = status.raw_data.get("ams", [])
        if ams_data:
            for ams_unit in ams_data:
                for tray in ams_unit.get("tray", []):
                    tray_type = tray.get("tray_type")
                    if tray_type:
                        loaded_types.add(_canonical_filament_type(tray_type))

        # Check external spool(s) (virtual tray, stored in raw_data["vt_tray"] as list)
        for vt in status.raw_data.get("vt_tray") or []:
            vt_type = vt.get("tray_type")
            if vt_type:
                loaded_types.add(_canonical_filament_type(vt_type))

        # Find which required types are missing (using canonical type for equivalence)
        missing = []
        for req_type in required_types:
            if _canonical_filament_type(req_type) not in loaded_types:
                missing.append(req_type)

        return missing

    def _count_override_color_matches(self, printer_id: int, overrides: list[dict]) -> int:
        """Count how many filament overrides have an exact color match on the printer.

        Used to prefer printers that already have the desired override colors loaded.
        """
        status = printer_manager.get_status(printer_id)
        if not status:
            return 0

        # Collect loaded filaments' type+color pairs
        loaded: set[tuple[str, str]] = set()
        for ams_unit in status.raw_data.get("ams", []):
            for tray in ams_unit.get("tray", []):
                tray_type = tray.get("tray_type")
                tray_color = tray.get("tray_color", "")
                if tray_type:
                    color_norm = tray_color.replace("#", "").lower()[:6]
                    loaded.add((tray_type.upper(), color_norm))
        for vt in status.raw_data.get("vt_tray") or []:
            vt_type = vt.get("tray_type")
            if vt_type:
                color_norm = (vt.get("tray_color", "") or "").replace("#", "").lower()[:6]
                loaded.add((vt_type.upper(), color_norm))

        matches = 0
        for o in overrides:
            o_type = (o.get("type") or "").upper()
            o_color = (o.get("color") or "").replace("#", "").lower()[:6]
            if (o_type, o_color) in loaded:
                matches += 1
        return matches

    async def _compute_ams_mapping_for_printer(
        self, db: AsyncSession, printer_id: int, item: PrintQueueItem
    ) -> MatchOutcome:
        """Decide the AMS mapping + block outcome for a printer, from LIVE state.

        THE decision point, run for EVERY dispatch evaluation — pinned items, each
        candidate of a model-based item, and every re-check a release path makes.
        There is no stored-mapping short-circuit: a mapping is only true of the tray
        state and the printer it was decided against, so replaying one hours later (or
        on a different machine) is exactly how a printer-1 external pick reached nine
        other printers on 2026-08-11.

        ``item.ams_mapping`` IS read here — as the operator's PIN input to the matcher
        (:func:`spool_selection.match_filaments_to_slots`), never as a previous result.
        Applies the configured spool-selection policy (``spool_selection_policy``) and
        the minimum-start-weight floor (``min_start_spool_g``) to pinned and
        auto-matched slots alike.

        Args:
            db: Database session
            printer_id: The printer to decide against
            item: The queue item (contains archive_id or library_file_id)

        Returns:
            A ``MatchOutcome`` whose ``mapping`` is the AMS mapping array (or None
            when no mapping is needed/possible), whose ``start_blocked_slots`` names
            any slot held back purely by the minimum-start floor, whose ``pin_missing``
            names any slot whose pinned tray is absent, and whose ``is_total`` says
            whether the item may dispatch at all.
        """
        # Get printer status
        status = printer_manager.get_status(printer_id)
        if not status:
            logger.warning("Cannot compute AMS mapping: printer %s status unavailable", printer_id)
            return MatchOutcome(mapping=None)

        # Resolve the selection policy + minimum-start floor once. Print-Anyway
        # (skip_filament_check) disables the floor so an acknowledged low spool
        # can still start. The AMS-Backup gate (#1766) is applied by
        # ``effective_policy``.
        policy_raw = await self._get_setting(db, "spool_selection_policy")
        policy = policy_raw if policy_raw in SELECTION_POLICIES else DEFAULT_SELECTION_POLICY
        min_start_g = await self._get_int_setting(db, "min_start_spool_g", default=DEFAULT_MIN_START_SPOOL_G)
        if item.skip_filament_check:
            min_start_g = 0
        eff_policy = effective_policy(policy, status.ams_filament_backup)

        # Get filament requirements from source file
        filament_reqs = await self._get_filament_requirements(db, item)
        if not filament_reqs:
            # When the 3MF can't be read but force-color overrides are present, build a
            # direct mapping from the overrides so the printer uses the correct AMS slot.
            if item.filament_overrides:
                try:
                    overrides = json.loads(item.filament_overrides)
                    force_overrides = [o for o in overrides if o.get("force_color_match")]
                    if force_overrides:
                        logger.info(
                            "Queue item %s: No filament reqs from 3MF; building AMS mapping from %d "
                            "force-color override(s)",
                            item.id,
                            len(force_overrides),
                        )
                        return await self._build_override_direct_mapping(
                            db,
                            printer_id,
                            force_overrides,
                            status,
                            eff_policy,
                            min_start_g,
                            parse_pins(item.ams_mapping),
                        )
                except (json.JSONDecodeError, KeyError, TypeError) as e:
                    logger.warning("Queue item %s: Force-color fallback mapping failed: %s", item.id, e)
            logger.debug("No filament requirements found for queue item %s", item.id)
            return MatchOutcome(mapping=None)

        # Apply filament overrides if present
        if item.filament_overrides:
            try:
                overrides = json.loads(item.filament_overrides)
                override_map = {o["slot_id"]: o for o in overrides}
                for req in filament_reqs:
                    if req["slot_id"] in override_map:
                        override = override_map[req["slot_id"]]
                        req["type"] = override["type"]
                        req["color"] = override["color"]
                        # Clear tray_info_idx so matching uses type+color instead of
                        # the original 3MF's tray_info_idx (which would match the old filament)
                        req["tray_info_idx"] = ""
                        logger.debug(
                            "Queue item %s: Override slot %d -> %s %s",
                            item.id,
                            req["slot_id"],
                            override["type"],
                            override["color"],
                        )
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.warning("Failed to apply filament overrides for queue item %s: %s", item.id, e)

        # Build loaded filaments from printer status, then drop any candidate whose
        # live tray reads the cleared-tray shape (see _present_candidates).
        loaded_filaments = _present_candidates(self._build_loaded_filaments(status))
        if not loaded_filaments:
            logger.debug("No filaments loaded on printer %s", printer_id)
            return MatchOutcome(mapping=None)

        # Inventory facts (remaining grams + first-loaded ordinal + the
        # out-of-rotation / spent hard-exclude flags) are ALWAYS built: even under
        # slot_order with the floor disabled, the matcher needs the inventory to
        # keep a jammed or spent spool from starting a print (skip_filament_check
        # forces the floor to 0, so the old conditional let an unusable spool through
        # on that path). ``spool_recovery._match_candidates`` builds it unconditionally
        # too — same precedent.
        inv: dict[int, SlotInventory] = await build_slot_inventory(db, printer_id, loaded_filaments)

        # ``require_known_grams``: this is the START lane, so a candidate the ledger
        # cannot price is reserved rather than started (it may hold 5 g). The
        # mid-print donor lane in ``spool_recovery`` keeps the fail-open reading.
        # ``pins``: the operator's stored slot instruction, narrowing (never
        # bypassing) the candidate set inside this one matcher.
        return match_filaments_to_slots(
            filament_reqs,
            loaded_filaments,
            policy=eff_policy,
            inv=inv,
            backup_on=status.ams_filament_backup,
            min_start_g=min_start_g,
            require_known_grams=True,
            pins=parse_pins(item.ams_mapping),
        )

    async def _build_override_direct_mapping(
        self,
        db: AsyncSession,
        printer_id: int,
        force_overrides: list[dict],
        status,
        policy: str,
        min_start_g: int,
        pins: list[int] | None = None,
    ) -> MatchOutcome:
        """Build an AMS mapping directly from force-color overrides without a 3MF.

        Used when ``_get_filament_requirements`` returns nothing (e.g. the 3MF's
        slice_info is missing or unreadable) but ``force_color_match`` overrides
        are present. Each override's ``slot_id``, ``type``, and ``color`` are
        treated as the filament requirement for that slot and matched against the
        current AMS state of the printer, threading the same policy / floor / operator
        pins as the normal path — one contract, no fallback-only exemptions.

        Returns a ``MatchOutcome`` (mapping None when the AMS has no filaments).
        """
        loaded = _present_candidates(self._build_loaded_filaments(status))
        if not loaded:
            return MatchOutcome(mapping=None)

        reqs = [
            {
                "slot_id": o["slot_id"],
                "type": o.get("type", ""),
                "color": o.get("color", ""),
                "tray_info_idx": "",
            }
            for o in force_overrides
        ]
        # Always build inventory (see _compute_ams_mapping_for_printer): the matcher
        # must hard-exclude jammed / spent spools even under slot_order + floor 0.
        inv: dict[int, SlotInventory] = await build_slot_inventory(db, printer_id, loaded)
        return match_filaments_to_slots(
            reqs,
            loaded,
            policy=policy,
            inv=inv,
            backup_on=getattr(status, "ams_filament_backup", None),
            min_start_g=min_start_g,
            require_known_grams=True,
            pins=pins,
        )

    async def _get_filament_requirements(self, db: AsyncSession, item: PrintQueueItem) -> list[dict] | None:
        """Resolve the queue item's source 3MF and parse the per-slot
        filament requirements out of it. Thin DB-resolver wrapper around
        ``filament_requirements.extract_filament_requirements`` so the VP
        queue-mode write path (#1188) can reuse the same parser at upload
        time.
        """
        from backend.app.services.filament_requirements import extract_filament_requirements

        file_path: Path | None = None
        if item.archive_id:
            result = await db.execute(select(PrintArchive).where(PrintArchive.id == item.archive_id))
            archive = result.scalar_one_or_none()
            if archive:
                file_path = settings.base_dir / archive.file_path
        elif item.library_file_id:
            result = await db.execute(LibraryFile.active().where(LibraryFile.id == item.library_file_id))
            library_file = result.scalar_one_or_none()
            if library_file:
                lib_path = Path(library_file.file_path)
                file_path = lib_path if lib_path.is_absolute() else settings.base_dir / library_file.file_path

        if not file_path or not file_path.exists():
            return None

        filaments = extract_filament_requirements(file_path, plate_id=item.plate_id)
        return filaments if filaments else None

    def _build_loaded_filaments(self, status) -> list[dict]:
        """Build list of loaded filaments from printer status.

        Emits one entry per AMS tray that either carries a filament type OR reads
        SEATED-but-UNIDENTIFIED (``tray_fields.tray_unread`` — a roll is in the tray and
        the wire asserts no type, no preset id and no tag). The unread entry carries an
        empty ``type`` and ``unread: True``.

        Emitting the unread ones is the point: dropping them made every downstream layer
        price a physically-loaded slot as EMPTY (the 12-tray fleet incident). They can
        never be selected — ``spool_selection.match_filaments_to_slots`` hard-excludes
        ``unread`` by name — but they are now VISIBLE, so the inventory layer can refuse
        to quote their ledger grams and the scheduler can hold the job for a read
        instead of staging it behind a phantom "Low filament".

        A cleared tray (asserted-empty type beside a non-present state) is still NOT
        emitted: that shape reads presence ``False``, which is a release fact, not an
        unknown one.

        Args:
            status: PrinterState from printer_manager

        Returns:
            List of loaded filament dicts with type, color, ams_id, tray_id,
            global_tray_id and the ``unread`` wire verdict.
        """
        filaments = []

        # Get ams_extruder_map for dual-nozzle printers (H2D, H2D Pro)
        ams_extruder_map = status.raw_data.get("ams_extruder_map", {})

        # Parse AMS units from raw_data
        ams_data = status.raw_data.get("ams", [])
        for ams_unit in ams_data:
            ams_id = int(ams_unit.get("id", 0))
            trays = ams_unit.get("tray", [])
            is_ht = len(trays) == 1  # AMS-HT has single tray

            for tray in trays:
                tray_type = tray.get("tray_type")
                # Seated with no identity of ANY kind (canonical rule, never a
                # re-derivation from tray_type emptiness). Only asked when the tray
                # carries no type, so an identified tray never pays for the check.
                unread = not tray_type and tray_unread(tray)
                if tray_type or unread:
                    tray_id = int(tray.get("id", 0))
                    tray_color = tray.get("tray_color", "")
                    # tray_info_idx identifies the specific spool (e.g., "GFA00", "P4d64437")
                    tray_info_idx = tray.get("tray_info_idx", "")
                    # Normalize color: remove alpha, add hash
                    color = self._normalize_color(tray_color)
                    # Calculate global tray ID
                    # AMS-HT units have IDs starting at 128 with a single tray
                    global_tray_id = ams_id if ams_id >= 128 else ams_id * 4 + tray_id

                    filaments.append(
                        {
                            "type": tray_type or "",
                            "color": color,
                            "tray_info_idx": tray_info_idx,
                            "ams_id": ams_id,
                            "tray_id": tray_id,
                            "is_ht": is_ht,
                            "is_external": False,
                            "global_tray_id": global_tray_id,
                            "extruder_id": ams_extruder_map.get(str(ams_id)),
                            "remain": tray.get("remain", -1),
                            # Raw firmware tray state (10/11 present, 9 empty, ...) —
                            # consumed by spool_recovery's presence filter to drop a
                            # seated-but-unsensed candidate. Additive key; no existing
                            # consumer reads it.
                            "state": tray.get("state"),
                            # Wire verdict: this tray holds a roll nobody has identified.
                            # Hard-excluded from selection, and the signal the deficit
                            # lane holds a job on instead of staging it.
                            "unread": unread,
                        }
                    )

        # Check external spool(s) (vt_tray is a list)
        for idx, vt in enumerate(status.raw_data.get("vt_tray") or []):
            if vt.get("tray_type"):
                color = self._normalize_color(vt.get("tray_color", ""))
                tray_id = int(vt.get("id", 254))
                filaments.append(
                    {
                        "type": vt["tray_type"],
                        "color": color,
                        "tray_info_idx": vt.get("tray_info_idx", ""),
                        "ams_id": -1,
                        "tray_id": idx,
                        "is_ht": False,
                        "is_external": True,
                        "global_tray_id": tray_id,
                        "extruder_id": (255 - tray_id) if ams_extruder_map else None,
                        "remain": vt.get("remain", -1),
                        "state": vt.get("state"),
                        # The external holder is only emitted when it declares a type,
                        # so it is never the unknown-roll case (and AMS backup never
                        # spans it) — the key stays for a uniform candidate shape.
                        "unread": False,
                    }
                )

        return filaments

    def _normalize_color(self, color: str | None) -> str:
        """Normalize color to #RRGGBB format."""
        if not color:
            return "#808080"
        hex_color = color.replace("#", "")[:6]
        return f"#{hex_color}"

    def _normalize_color_for_compare(self, color: str | None) -> str:
        """Normalize color for comparison (lowercase, no hash). Delegates to the
        canonical ``spool_selection`` implementation."""
        return normalize_color_for_compare(color)

    def _colors_are_similar(self, color1: str | None, color2: str | None, threshold: int = 40) -> bool:
        """Check if two colors are visually similar within a threshold. Delegates
        to the canonical ``spool_selection`` implementation."""
        return colors_are_similar(color1, color2, threshold)

    def _mark_printer_dispatched(
        self,
        printer_id: int,
        pre_state: str | None,
        pre_subtask_id: str | None,
    ) -> None:
        """Record that a print command was just sent to ``printer_id``.

        Held until either the watchdog observes a state/subtask transition
        (success path) or the hard timeout expires. See ``_dispatch_holds``.
        """
        if not pre_state:
            # No pre_state means we can't detect a transition — fall back to a
            # pure time-based hold using empty string as a sentinel that won't
            # match any real printer state.
            pre_state = ""
        self._dispatch_holds[printer_id] = (time.monotonic(), pre_state, pre_subtask_id)

    def _release_dispatch_hold(self, printer_id: int) -> None:
        """Drop the dispatch hold for ``printer_id`` (called by the watchdog)."""
        self._dispatch_holds.pop(printer_id, None)

    def _printer_in_dispatch_hold(self, printer_id: int) -> bool:
        """True if ``printer_id`` is still inside its post-dispatch hold window.

        Returns False (and clears the hold) once any of these are true:
          - hard timeout (``_dispatch_max_hold``) has elapsed
          - the printer has transitioned out of pre_state and we're past the
            minimum cooldown
          - the printer's subtask_id has advanced past pre_subtask_id and we're
            past the minimum cooldown
        Otherwise the printer is held — caller should treat it as busy.
        """
        entry = self._dispatch_holds.get(printer_id)
        if not entry:
            return False
        started_at, pre_state, pre_subtask_id = entry
        elapsed = time.monotonic() - started_at

        if elapsed >= self._dispatch_max_hold:
            self._dispatch_holds.pop(printer_id, None)
            return False

        # Without a pre_state we can't detect a transition — fall back to the
        # min cooldown alone, then drop the hold.
        if not pre_state:
            if elapsed >= self._dispatch_min_cooldown:
                self._dispatch_holds.pop(printer_id, None)
                return False
            return True

        status = printer_manager.get_status(printer_id)
        current_state = getattr(status, "state", None) if status else None
        current_subtask_id = getattr(status, "subtask_id", None) if status else None
        transitioned = (current_state is not None and current_state != pre_state) or (
            pre_subtask_id is not None and current_subtask_id is not None and current_subtask_id != pre_subtask_id
        )

        if transitioned and elapsed >= self._dispatch_min_cooldown:
            self._dispatch_holds.pop(printer_id, None)
            return False

        return True

    def _is_printer_idle(self, printer_id: int) -> bool:
        """Check if a printer is connected and idle."""
        if not printer_manager.is_connected(printer_id):
            logger.debug("Printer %d: not connected", printer_id)
            return False

        # Quarantined printers (farm failure policy) are excluded from ALL
        # dispatch until an operator clears the quarantine (#Phase3).
        if printer_manager.is_quarantined(printer_id):
            logger.debug("Printer %d: not idle — quarantined", printer_id)
            return False

        # Device-vs-declared model mismatch (Phase 2): eject geometry keyed on the
        # wrong model could drive the toolhead outside the real bed, so block ALL
        # dispatch until the registration is corrected (mirrors the quarantine gate).
        if printer_manager.is_model_mismatch(printer_id):
            logger.debug("Printer %d: not idle — model mismatch", printer_id)
            return False

        state = printer_manager.get_status(printer_id)
        if not state:
            logger.debug("Printer %d: no status available", printer_id)
            return False

        # Plate-clear gate (unconditional — Phase 1, P1-B): if the printer finished/
        # failed a previous print and the plate hasn't been acknowledged clear, the
        # queue must NOT dispatch the next job even if the printer reports IDLE. This
        # no longer keys on the global require_plate_clear convenience toggle — the
        # gate is only ever RAISED when it should be (farm work involved, or the
        # toggle on), so honouring it here whenever set is the farm loop's safety
        # contract, not a preference. After Auto Off cycles the printer it boots back
        # into IDLE with no memory of the finish; the persisted flag survives (#961).
        if printer_manager.is_awaiting_plate_clear(printer_id):
            logger.debug(
                "Printer %d: not idle — awaiting plate-clear acknowledgment (state=%s)",
                printer_id,
                state.state,
            )
            return False

        idle = state.state in ("IDLE", "FINISH", "FAILED")
        if not idle:
            logger.debug("Printer %d: not idle — state=%s", printer_id, state.state)
        return idle

    async def _get_setting(self, db: AsyncSession, key: str) -> str | None:
        """Read a setting value from the database."""
        result = await db.execute(select(Settings).where(Settings.key == key))
        setting = result.scalar_one_or_none()
        return setting.value if setting else None

    async def _get_bool_setting(self, db: AsyncSession, key: str, default: bool = False) -> bool:
        """Read a boolean setting from the database."""
        result = await db.execute(select(Settings).where(Settings.key == key))
        setting = result.scalar_one_or_none()
        if setting:
            return setting.value.lower() == "true"
        return default

    async def _get_int_setting(self, db: AsyncSession, key: str, default: int) -> int:
        """Read an integer setting from the database, falling back to ``default``."""
        result = await db.execute(select(Settings).where(Settings.key == key))
        setting = result.scalar_one_or_none()
        if setting and setting.value is not None:
            try:
                return int(setting.value)
            except (TypeError, ValueError):
                return default
        return default

    async def _get_float_setting(self, db: AsyncSession, key: str, default: float) -> float:
        """Read a float setting from the database, falling back to ``default``."""
        result = await db.execute(select(Settings).where(Settings.key == key))
        setting = result.scalar_one_or_none()
        if setting and setting.value is not None:
            try:
                return float(setting.value)
            except (TypeError, ValueError):
                return default
        return default

    async def _read_check_interval(self) -> int:
        """Fallback poll interval for the main loop (``queue_check_interval_seconds``, clamped 5-300)."""
        async with async_session() as db:
            value = await self._get_int_setting(db, "queue_check_interval_seconds", default=30)
        return max(5, min(300, value))

    async def _read_kick_debounce(self) -> float:
        """Burst-coalescing debounce after a kick (``dispatch_kick_debounce_seconds``, clamped 0.2-10)."""
        async with async_session() as db:
            value = await self._get_float_setting(db, "dispatch_kick_debounce_seconds", default=1.0)
        return max(0.2, min(10.0, value))

    async def _read_dispatch_parallel_limit(self, db: AsyncSession) -> int:
        """Max concurrent per-printer dispatches per tick (``dispatch_parallel_limit``, clamped 1-10).

        Read once per tick on the tick session (latency Phase B). A limit of 1
        preserves the pre-Phase-B serial dispatch order exactly.
        """
        value = await self._get_int_setting(db, "dispatch_parallel_limit", default=3)
        return max(1, min(10, value))

    async def _get_drying_presets(self, db: AsyncSession) -> dict[str, dict[str, int]]:
        """Get drying presets (user-configured or built-in defaults)."""
        result = await db.execute(select(Settings).where(Settings.key == "drying_presets"))
        setting = result.scalar_one_or_none()
        if setting and setting.value:
            try:
                presets = json.loads(setting.value)
                if isinstance(presets, dict) and presets:
                    return presets
            except json.JSONDecodeError:
                pass
        return self.DEFAULT_DRYING_PRESETS

    async def _get_humidity_thresholds(self, db: AsyncSession) -> dict[str, int]:
        """Per-filament humidity thresholds (#1605).

        Returns the user-configured overrides map keyed by normalized filament
        type (uppercase base, e.g. ``PLA``, ``ASA``) plus a ``default`` key for
        unknown / unmapped types. Empty / unset → empty dict, in which case
        callers fall back to ``ams_humidity_fair``.
        """
        result = await db.execute(select(Settings).where(Settings.key == "ams_humidity_thresholds"))
        setting = result.scalar_one_or_none()
        if not setting or not setting.value:
            return {}
        try:
            data = json.loads(setting.value)
        except json.JSONDecodeError:
            return {}
        if not isinstance(data, dict):
            return {}
        out: dict[str, int] = {}
        for key, value in data.items():
            try:
                out[str(key).upper() if key != "default" else "default"] = int(value)
            except (TypeError, ValueError):
                continue
        return out

    @staticmethod
    def resolve_humidity_threshold(trays: list[dict], thresholds: dict[str, int], fallback: int) -> int:
        """Resolve the effective humidity threshold for an AMS unit (#1605).

        For mixed filament types loaded into one AMS, returns the most
        restrictive (lowest) threshold across all loaded tray types — matches
        the conservative-params strategy already used for drying temp/hours.
        Empty / unloaded trays contribute no constraint. Unknown types use the
        ``default`` key, falling through to ``fallback`` (= ``ams_humidity_fair``)
        when no per-type map is configured at all.
        """
        default = thresholds.get("default", fallback)
        if not thresholds:
            return fallback
        candidates: list[int] = []
        for tray in trays:
            tray_type = str(tray.get("tray_type") or "").strip()
            if not tray_type:
                continue
            base_type = tray_type.split()[0].upper()
            candidates.append(thresholds.get(base_type, default))
        if not candidates:
            return default
        return min(candidates)

    def _get_conservative_drying_params(
        self, trays: list[dict], module_type: str, presets: dict[str, dict[str, int]]
    ) -> tuple[int, int, str] | None:
        """Get the most conservative drying params for mixed filament types in an AMS unit.

        Returns (temp, duration_hours, filament_type) or None if no drying-eligible filaments.
        """
        temp_key = module_type if module_type in ("n3f", "n3s") else "n3f"
        hours_key = f"{temp_key}_hours"

        min_temp = None
        max_hours = None
        filament_type = ""

        for tray in trays:
            tray_type = tray.get("tray_type", "")
            if not tray_type:
                continue
            # Normalize filament type for preset lookup (e.g., "PLA Basic" -> "PLA")
            base_type = tray_type.split()[0].upper()
            preset = presets.get(base_type)
            if not preset:
                continue

            temp = preset.get(temp_key, 55)
            hours = preset.get(hours_key, 12)

            # Conservative: lowest temp, longest duration
            if min_temp is None or temp < min_temp:
                min_temp = temp
            if max_hours is None or hours > max_hours:
                max_hours = hours
            if not filament_type:
                filament_type = base_type

        if min_temp is None:
            return None
        return (min_temp, max_hours or 12, filament_type)

    async def _check_auto_drying(
        self,
        db: AsyncSession,
        queue_items: list[PrintQueueItem],
        busy_printers: set[int],
    ):
        """Start drying on idle printers based on humidity.

        Three modes (can all be enabled independently):
        - queue_drying_enabled: Dry between scheduled queue prints
        - ambient_drying_enabled: Dry any idle printer when humidity is high, regardless of queue
        - print_drying_enabled: Also evaluate printers that are currently printing,
          when model+firmware supports "Print While Drying" (gated by
          supports_drying_while_printing). Drying temperature is capped at
          max(40, preset_temp - 5) to protect spools mid-print.
        """
        queue_drying_enabled = await self._get_bool_setting(db, "queue_drying_enabled")
        ambient_drying_enabled = await self._get_bool_setting(db, "ambient_drying_enabled")
        print_drying_enabled = await self._get_bool_setting(db, "print_drying_enabled")
        if not queue_drying_enabled and not ambient_drying_enabled:
            # Stop active drying on all printers if both features disabled
            if self._drying_in_progress:
                for pid in list(self._drying_in_progress):
                    logger.info("Auto-drying: printer %d — stopping, auto-drying disabled", pid)
                    await self._stop_drying(pid)
            return

        # Update drying state from printer status (handles backend restart)
        self._sync_drying_state()

        # Find printers with scheduled items (for queue drying mode)
        printers_with_scheduled: set[int] = set()
        printers_with_items: set[int] = set()
        for item in queue_items:
            if item.printer_id:
                printers_with_items.add(item.printer_id)
                if item.scheduled_time and not item.manual_start:
                    printers_with_scheduled.add(item.printer_id)

        # If only queue mode is on and no printers have scheduled items, stop drying
        # (but skip this short-circuit when print_drying_enabled is on — busy printers
        # may still be eligible for mid-print drying regardless of queue state).
        if not ambient_drying_enabled and not printers_with_scheduled and not print_drying_enabled:
            for pid in list(self._drying_in_progress):
                logger.info("Auto-drying: printer %d — stopping, no scheduled prints in queue", pid)
                await self._stop_drying(pid)
            return

        # Get humidity threshold (global fallback)
        result = await db.execute(select(Settings).where(Settings.key == "ams_humidity_fair"))
        setting = result.scalar_one_or_none()
        global_humidity_threshold = int(setting.value) if setting else 60

        # Per-filament humidity threshold overrides (#1605). Empty → fall back
        # to the global threshold for every AMS unit.
        per_type_thresholds = await self._get_humidity_thresholds(db)

        # Get drying presets
        presets = await self._get_drying_presets(db)

        # Determine if drying should be skipped for printers with pending items
        block_for_drying = await self._get_bool_setting(db, "queue_drying_block")

        # Get all active printers
        all_printers = await db.execute(select(Printer).where(Printer.is_active.is_(True)))
        for printer in all_printers.scalars():
            pid = printer.id

            # Resolve model+firmware up front — needed to decide whether this printer
            # qualifies for mid-print drying (busy printer on capable hardware).
            state = printer_manager.get_status(pid)
            if not state:
                logger.debug("Auto-drying: printer %d skipped — no state", pid)
                continue
            model = printer_manager.get_model(pid)
            firmware = state.firmware_version

            mid_print = (
                pid in busy_printers and print_drying_enabled and supports_drying_while_printing(model, firmware)
            )

            if pid in busy_printers and not mid_print:
                logger.debug("Auto-drying: printer %d skipped — busy", pid)
                continue

            if not mid_print:
                # In queue-only mode, only dry printers that have scheduled prints
                if not ambient_drying_enabled and pid not in printers_with_scheduled:
                    if self._drying_in_progress.get(pid):
                        logger.info("Auto-drying: printer %d — stopping, no scheduled prints for this printer", pid)
                        await self._stop_drying(pid)
                    logger.debug("Auto-drying: printer %d skipped — no scheduled prints", pid)
                    continue
                # When block mode is on, don't START new drying on printers with pending items.
                # But allow already-drying printers through so humidity auto-stop logic still runs.
                if block_for_drying and pid in printers_with_items and not self._drying_in_progress.get(pid):
                    logger.debug("Auto-drying: printer %d skipped — has pending items (block mode)", pid)
                    continue
            if not printer_manager.is_connected(pid):
                logger.debug("Auto-drying: printer %d skipped — not connected", pid)
                continue
            if not mid_print and not self._is_printer_idle(pid):
                logger.debug("Auto-drying: printer %d skipped — not idle", pid)
                continue

            # Check drying capability. For mid-print path, supports_drying_while_printing
            # was already verified when computing mid_print above.
            if not mid_print and not supports_drying(model, firmware):
                logger.debug("Auto-drying: printer %d skipped — model %s does not support drying", pid, model)
                continue

            # Check each AMS unit from raw_data
            ams_list = state.raw_data.get("ams", [])
            logger.debug("Auto-drying: printer %d — checking %d AMS units", pid, len(ams_list))
            for ams_data in ams_list:
                module_type = str(ams_data.get("module_type") or "")
                ams_id = int(ams_data.get("id", 0))
                # Only n3f/n3s support drying
                if module_type not in ("n3f", "n3s"):
                    logger.debug("Auto-drying: printer %d AMS %d skipped — module_type=%s", pid, ams_id, module_type)
                    continue

                # Resolve per-filament humidity threshold for this AMS unit (#1605).
                # Most-restrictive of all loaded tray types; falls back to the
                # global threshold when no overrides are configured.
                trays = ams_data.get("tray", []) or []
                humidity_threshold = self.resolve_humidity_threshold(
                    trays, per_type_thresholds, global_humidity_threshold
                )

                dry_time = int(ams_data.get("dry_time") or 0)

                # Read humidity — prefer humidity_raw (actual %) over humidity (index 1-5)
                humidity = None
                h_raw = ams_data.get("humidity_raw")
                if h_raw is not None:
                    try:
                        humidity = int(h_raw)
                    except (ValueError, TypeError):
                        pass
                if humidity is None:
                    h_idx = ams_data.get("humidity")
                    if h_idx is not None:
                        try:
                            humidity = int(h_idx)
                        except (ValueError, TypeError):
                            pass
                # Already drying — check if humidity dropped below threshold (with minimum drying time)
                if dry_time > 0:
                    if pid not in self._drying_in_progress:
                        # Drying we didn't start (manual or from before restart) — track but don't stop
                        self._drying_in_progress[pid] = time.monotonic()
                    started_at = self._drying_in_progress[pid]
                    elapsed = time.monotonic() - started_at
                    if humidity is not None and humidity <= humidity_threshold and elapsed >= self._min_drying_seconds:
                        logger.info(
                            "Auto-drying: printer %d AMS %d — humidity %d%% <= threshold %d%% after %dm, stopping drying",
                            pid,
                            ams_id,
                            humidity,
                            humidity_threshold,
                            int(elapsed / 60),
                        )
                        printer_manager.send_drying_command(pid, ams_id, temp=0, duration=0, mode=0)
                    else:
                        logger.debug(
                            "Auto-drying: printer %d AMS %d — drying (%dm left, humidity %s%%, elapsed %dm/%dm min)",
                            pid,
                            ams_id,
                            dry_time,
                            humidity,
                            int(elapsed / 60),
                            self._min_drying_seconds // 60,
                        )
                    continue

                # Humidity below threshold — no need to start drying
                if humidity is None or humidity <= humidity_threshold:
                    logger.debug(
                        "Auto-drying: printer %d AMS %d skipped — humidity %s <= threshold %d",
                        pid,
                        ams_id,
                        humidity,
                        humidity_threshold,
                    )
                    continue

                # Check cannot-dry reasons (power constraints etc.)
                sf_reasons = ams_data.get("dry_sf_reason", [])
                if sf_reasons:
                    logger.debug(
                        "Auto-drying: printer %d AMS %d skipped — cannot dry reasons: %s",
                        pid,
                        ams_id,
                        sf_reasons,
                    )
                    continue

                # Get conservative drying params for mixed filaments
                params = self._get_conservative_drying_params(trays, module_type, presets)
                if not params:
                    logger.debug(
                        "Auto-drying: printer %d AMS %d skipped — no drying-eligible filaments in trays", pid, ams_id
                    )
                    continue

                temp, duration_hours, filament_type = params

                # Mid-print drying: cap drying temperature to protect spools (Bambu warns
                # "drying temperature must not exceed the filament's softening temperature"
                # for Print While Drying). Floor at 40 degC — below that the dryer is
                # ineffective and firmware will reject anyway.
                if mid_print:
                    temp = max(40, temp - 5)

                # Start drying
                logger.info(
                    "Auto-drying: printer %d AMS %d — humidity %d%% > threshold %d%%, "
                    "starting %s drying at %d°C for %dh%s",
                    pid,
                    ams_id,
                    humidity,
                    humidity_threshold,
                    filament_type,
                    temp,
                    duration_hours,
                    " (mid-print)" if mid_print else "",
                )
                success = printer_manager.send_drying_command(
                    pid, ams_id, temp, duration_hours, mode=1, filament=filament_type
                )
                if success:
                    self._drying_in_progress[pid] = time.monotonic()

    def _sync_drying_state(self):
        """Sync in-memory drying state with actual printer status.

        Handles backend restart — if a printer is drying but we don't know about it,
        update our state. If we think it's drying but it's not, clear it.
        """
        to_remove = []
        for pid in self._drying_in_progress:
            state = printer_manager.get_status(pid)
            if not state:
                to_remove.append(pid)
                continue
            # Check if any AMS unit is still drying
            ams_list = state.raw_data.get("ams", [])
            any_drying = any(int(a.get("dry_time") or 0) > 0 for a in ams_list)
            if not any_drying:
                to_remove.append(pid)
        for pid in to_remove:
            self._drying_in_progress.pop(pid, None)

    async def _stop_drying(self, printer_id: int):
        """Stop all active drying on a printer (print takes priority)."""
        state = printer_manager.get_status(printer_id)
        if not state:
            self._drying_in_progress.pop(printer_id, None)
            return

        ams_list = state.raw_data.get("ams", [])
        for ams_data in ams_list:
            dry_time = int(ams_data.get("dry_time") or 0)
            if dry_time > 0:
                ams_id = int(ams_data.get("id", 0))
                logger.info(
                    "Auto-drying: stopping drying on printer %d AMS %d — print takes priority",
                    printer_id,
                    ams_id,
                )
                printer_manager.send_drying_command(printer_id, ams_id, 0, 0, mode=0)
        self._drying_in_progress.pop(printer_id, None)

    async def _get_smart_plugs(self, db: AsyncSession, printer_id: int) -> list[SmartPlug]:
        """Get all smart plugs associated with a printer."""
        result = await db.execute(select(SmartPlug).where(SmartPlug.printer_id == printer_id))
        return list(result.scalars().all())

    async def _power_on_and_wait(self, plug: SmartPlug, printer_id: int, db: AsyncSession) -> bool:
        """Turn on smart plug and wait for printer to connect.

        Returns True if printer connected successfully within timeout.
        """
        # Get the appropriate service for the plug type (Tasmota or Home Assistant)
        service = await smart_plug_manager.get_service_for_plug(plug, db)

        # Check current plug state
        status = await service.get_status(plug)
        if not status.get("reachable"):
            logger.warning("Smart plug '%s' is not reachable", plug.name)
            return False

        # Turn on if not already on
        if status.get("state") != "ON":
            success = await service.turn_on(plug)
            if not success:
                logger.warning("Failed to turn on smart plug '%s'", plug.name)
                return False
            logger.info("Powered on smart plug '%s' for printer %s", plug.name, printer_id)

        # Get printer from database for connection
        result = await db.execute(select(Printer).where(Printer.id == printer_id))
        printer = result.scalar_one_or_none()
        if not printer:
            logger.error("Printer %s not found in database", printer_id)
            return False

        # Wait for printer to boot (give it some time before trying to connect)
        logger.info("Waiting 30s for printer %s to boot...", printer_id)
        await asyncio.sleep(30)

        # Try to connect to the printer periodically
        elapsed = 30  # Already waited 30s
        while elapsed < self._power_on_wait_time:
            # Try to connect
            logger.info("Attempting to connect to printer %s...", printer_id)
            try:
                connected = await printer_manager.connect_printer(printer)
                if connected:
                    logger.info("Printer %s connected after %ss", printer_id, elapsed)
                    # Give it a moment to stabilize and get status
                    await asyncio.sleep(5)
                    return True
            except Exception as e:
                logger.debug("Connection attempt failed: %s", e)

            await asyncio.sleep(self._power_on_check_interval)
            elapsed += self._power_on_check_interval
            logger.debug("Waiting for printer %s to connect... (%ss)", printer_id, elapsed)

        logger.warning("Printer %s did not connect within %ss after power on", printer_id, self._power_on_wait_time)
        return False

    async def _check_previous_success(self, db: AsyncSession, item: PrintQueueItem) -> bool:
        """Check if the previous print on this printer succeeded.

        A user-cancelled predecessor is treated as neutral — `cancelled` is a
        deliberate action, not a failure, so subsequent items should still
        dispatch (#1667). `skipped` is excluded from the lookback entirely:
        a skip isn't an actual print attempt, so it must not gate downstream
        items — counting it as a failed predecessor was the cascade bug that
        let a single cancellation block 18 items over 3 days for the reporter.
        Only `failed` and `aborted` — real print-attempt failures — block.

        Failures with `gate_acknowledged=True` (set by the per-printer Resume
        action — #1818) are also excluded from the lookback so the user can
        clear the gate after fixing the physical issue without having to
        re-queue every downstream job.
        """
        result = await db.execute(
            select(PrintQueueItem)
            .where(PrintQueueItem.printer_id == item.printer_id)
            .where(PrintQueueItem.id != item.id)
            .where(PrintQueueItem.status.in_(["completed", "failed", "cancelled", "aborted"]))
            .where(PrintQueueItem.gate_acknowledged == False)  # noqa: E712
            .order_by(PrintQueueItem.completed_at.desc())
            .limit(1)
        )
        prev_item = result.scalar_one_or_none()

        # If no previous item, assume success (first in queue)
        if not prev_item:
            return True

        return prev_item.status in ("completed", "cancelled")

    async def _power_off_if_needed(self, db: AsyncSession, item: PrintQueueItem):
        """Power off printer if auto_off_after is enabled (waits for cooldown)."""
        if not item.auto_off_after:
            return

        plugs = await self._get_smart_plugs(db, item.printer_id)
        plug_ids = [p.id for p in plugs if p.enabled]
        if plug_ids:
            logger.info("Auto-off: Waiting for printer %s to cool down before power off...", item.printer_id)
            # Wait for cooldown (up to 10 minutes)
            await printer_manager.wait_for_cooldown(item.printer_id, target_temp=50.0, timeout=600)
            # Re-fetch plugs in a fresh session after the long cooldown wait
            async with async_session() as new_db:
                for plug_id in plug_ids:
                    try:
                        result = await new_db.execute(select(SmartPlug).where(SmartPlug.id == plug_id))
                        plug = result.scalar_one_or_none()
                        if plug and plug.enabled:
                            logger.info("Auto-off: Powering off plug '%s' for printer %s", plug.name, item.printer_id)
                            service = await smart_plug_manager.get_service_for_plug(plug, new_db)
                            await service.turn_off(plug)
                    except Exception as e:
                        logger.warning(
                            "Auto-off: Failed to power off plug %s for printer %s: %s", plug_id, item.printer_id, e
                        )

    @staticmethod
    def _hold_is_new(*, was_held: bool, prior_reason: str | None, reason: str) -> bool:
        """Whether a hold just STARTED (or changed shape) and so deserves an alert.

        The scheduler re-evaluates every held item on every 30 s tick, so "the item
        is held" is true on every tick and is NOT an event. The event is the
        not-held → held transition, plus a change of the persisted reason (which
        NAMES the blocking printers — when that set changes the operator is looking
        at a different problem). Both inputs are durable columns
        (``filament_short`` / ``waiting_reason``) read BEFORE staging overwrites
        them; no parallel in-memory flag exists to drift.
        """
        return not was_held or reason != prior_reason

    async def _notify_queue_waiting(
        self,
        db: AsyncSession,
        item: PrintQueueItem,
        reason: str,
        target_model: str,
    ) -> None:
        """Single emit path for the queue_job_waiting event (all scheduler holds).

        Composes the per-item ``dedup_key`` (the chokepoint's re-notify floor) and
        swallows notification failures — a hold is already persisted on the item,
        so a provider outage must never break the dispatch tick.
        """
        job_name = await self._get_job_name(db, item)
        try:
            await notification_service.on_queue_job_waiting(
                job_name=job_name,
                target_model=target_model or "",
                waiting_reason=reason,
                db=db,
                dedup_key=str(item.id),
            )
        except Exception as e:
            logger.debug("queue-waiting notification failed for item %s: %s", item.id, e)

    async def _hold_dispatch_precondition(
        self,
        db: AsyncSession,
        item: PrintQueueItem,
        printer: Printer | None,
        reason: str,
    ) -> None:
        """Hold an item on an unmet dispatch PRECONDITION — a WAIT, never a failure.

        The shared body behind the USB pre-flight and the missing-source-file gate:
        conditions the printer/environment must satisfy before a dispatch can even be
        attempted, and which no amount of failing the item would fix. The item stays
        pending — no ``manual_start``, no retry burn, nothing terminal — so the next
        tick re-checks and self-clears the hold when the precondition is met (the
        dispatch commit drops the token once the item actually starts).

        A model-targeted unit releases its scheduler-made printer pin: ``target_model``
        set means the pin was made THIS tick by the model-based path, never by a user,
        so a sick-but-idle printer must not become the unit's permanent home — leaving
        it pinned funnels the whole run onto one broken printer, one unit per tick,
        until the pool drains. Always commit the un-pin (even when already waiting) so
        a unit held tick N and re-held tick N+1 still ends unpinned, and once-guard the
        model path's per-assignment notification so re-selecting the same sick printer
        every tick doesn't re-notify "assigned". A user-pinned unit (no
        ``target_model``) keeps its printer and commits only on the transition INTO the
        hold. ``ams_mapping`` is deliberately NOT touched in either case: it is the
        operator's slot instruction, and an unmet precondition is no reason to discard
        what a human asked for.

        The waiting notification fires once per transition into the hold, so a
        precondition unmet across many ticks notifies once.
        """
        already_waiting = item.waiting_reason == reason
        if item.target_model:
            item.printer_id = None
            item.waiting_reason = reason
            self._hold_unpinned_items.add(item.id)
            await db.commit()
        elif not already_waiting:
            item.waiting_reason = reason
            await db.commit()
        if not already_waiting:
            await self._notify_queue_waiting(db, item, reason, (printer.model if printer else "") or "")

    async def _get_job_name(self, db: AsyncSession, item: PrintQueueItem) -> str:
        """Get a human-readable name for a queue item."""
        if item.archive_id:
            result = await db.execute(select(PrintArchive).where(PrintArchive.id == item.archive_id))
            archive = result.scalar_one_or_none()
            if archive:
                return archive.filename.replace(".gcode.3mf", "").replace(".3mf", "")
        if item.library_file_id:
            result = await db.execute(LibraryFile.active().where(LibraryFile.id == item.library_file_id))
            library_file = result.scalar_one_or_none()
            if library_file:
                return library_file.filename.replace(".gcode.3mf", "").replace(".3mf", "")
        return f"Job #{item.id}"

    async def _get_printer(self, db: AsyncSession, printer_id: int) -> Printer | None:
        """Get printer by ID."""
        result = await db.execute(select(Printer).where(Printer.id == printer_id))
        return result.scalar_one_or_none()

    async def _warn_backup_group_gap(self, db: AsyncSession, printer_id: int, mapping: list[int] | None) -> None:
        """WARN (and page once) when this dispatch's tray has no firmware backup partner.

        Wiring only — the rule is ``spool_selection.backup_partner_gap``. This reads the
        printer's live trays, scopes the peer set to the picked tray's extruder side
        (the firmware cannot cross extruders even with backup ON), and hands the pure
        function one picked tray plus its peers per mapped requirement slot. Only the
        AMS units are read, so the external ``vt_tray`` holder is excluded by
        construction — the same deliberate scope ``filament_deficit._live_tray_identities``
        takes, because AMS Filament Backup never spans it. ``slot=`` in the WARN is the
        REQUIREMENT slot (the mapping position), matching the matcher's own
        ``[spool-select] slot=…`` trace; the human AMS slot name is in the operator copy.

        Why it exists (010-H2S 2026-08-21, incident shape 33): the firmware pairs backup
        slots byte-exactly on preset and colour, the farm's matcher pairs filaments
        within 40 per colour channel, and where those two readings disagree
        the farm dispatches believing in a backup that does not exist. The reconcile
        lane rewrites the farm's OWN tagless slots onto the canonical identity; this
        covers the residue it cannot touch — an RFID or operator-bound tray, a refused
        AMS write, or a dispatch that lands before the first idle window.

        Advisory by construction. It never blocks, re-routes or re-matches: a split
        group is a degraded runout rescue, not an unsafe print, and the operator's
        one-field edit on the printer is the fix. The whole body is guarded to a DEBUG
        line (invariant 10 — no farm-side check may crash a dispatch path), and it stands
        down silently whenever the printer state is missing or backup is not explicitly
        ON, because with backup OFF there is no group to be split.

        The WARN fires on every dispatch that sees the gap (it is the triage record);
        only the page is deduped, on the two backup-group KEYS rather than the printer,
        so fixing one pair does not mute a second one — ``notify_dedup`` is the right
        clock because this is an alert-class event, not a state decision.
        """
        try:
            if not mapping:
                return
            backup_on, ams_extruder_map, is_dual = await _get_printer_backup_context(printer_id)
            if not backup_on:
                return

            from backend.app.services.spool_recovery import runout_slot_desc
            from backend.app.services.spool_respool import decode_global_tray, encode_global_tray

            status = printer_manager.get_status(printer_id)
            raw = getattr(status, "raw_data", None)
            if not isinstance(raw, dict):
                return

            # Live trays by (ams_id, tray_id), each stamped with its own global id.
            # Copies, never the live dicts: this lane must not mutate printer state.
            trays: dict[tuple[int, int], dict] = {}
            for unit in raw.get("ams") or []:
                if not isinstance(unit, dict):
                    continue
                try:
                    ams_id = int(unit["id"])
                except (KeyError, TypeError, ValueError):
                    continue
                for tray in unit.get("tray") or []:
                    if not isinstance(tray, dict):
                        continue
                    try:
                        tray_id = int(tray["id"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    gtid = encode_global_tray(ams_id, tray_id)
                    if gtid is None:
                        continue
                    trays[(ams_id, tray_id)] = {**tray, "global_tray_id": gtid}
            if not trays:
                return

            for slot_id, picked_gtid in enumerate(mapping, start=1):
                if not isinstance(picked_gtid, int) or picked_gtid < 0:
                    continue
                picked_ams, picked_tray = decode_global_tray(picked_gtid)
                if picked_ams is None or picked_tray is None:
                    continue
                picked = trays.get((picked_ams, picked_tray))
                if picked is None:
                    continue
                side = _extruder_side_for_ams(picked_ams, ams_extruder_map, is_dual)
                others = [
                    tray
                    for (ams_id, tray_id), tray in trays.items()
                    if (ams_id, tray_id) != (picked_ams, picked_tray)
                    and _extruder_side_for_ams(ams_id, ams_extruder_map, is_dual) == side
                ]
                gap = backup_partner_gap(picked, others)
                if gap is None:
                    continue

                logger.warning(
                    "[spool-select] printer=%d slot=%d picked gtid=%d has NO firmware backup partner — "
                    "gtid=%d is the same filament but differs in %s (%s vs %s)",
                    printer_id,
                    slot_id,
                    picked_gtid,
                    gap.partner_global_tray_id,
                    gap.dimension,
                    gap.picked_value,
                    gap.partner_value,
                )

                if not notify_dedup.allow(
                    "backup_group_split",
                    f"{printer_id}:{gap.picked_key}|{gap.partner_key}",
                    time.time(),
                    6 * 3600.0,
                ):
                    continue
                printer = await self._get_printer(db, printer_id)
                await notification_service.on_backup_group_split(
                    printer_id=printer_id,
                    printer_name=(printer.name if printer else "") or f"Printer {printer_id}",
                    slot=runout_slot_desc(picked_gtid) or f"tray {picked_gtid}",
                    partner_slot=runout_slot_desc(gap.partner_global_tray_id) or f"tray {gap.partner_global_tray_id}",
                    dimension=gap.dimension,
                    picked_value=gap.picked_value,
                    partner_value=gap.partner_value,
                    db=db,
                )
        except Exception:
            logger.debug(
                "[spool-select] backup-partner check failed for printer %s (dispatch unaffected)",
                printer_id,
                exc_info=True,
            )

    async def _compute_deficit_safe(
        self,
        db: AsyncSession,
        item: PrintQueueItem,
        *,
        printer_id_override: int | None = None,
        ams_mapping_override: str | None = None,
    ) -> list:
        """Deficit compute that never wedges the queue on a flaky check.

        Returns the per-slot shortfall list (empty = clear to dispatch). Any
        exception (e.g. a Spoolman timeout) is logged and treated as "no
        deficit" — the PrintModal-side check still runs on the manual paths.
        The optional overrides let the model-based candidate loop check a
        printer without mutating the item.
        """
        try:
            return await compute_deficit_for_queue_item(
                db,
                item,
                printer_id_override=printer_id_override,
                ams_mapping_override=ams_mapping_override,
            )
        except Exception as e:
            logger.warning("Filament deficit check failed for item %s: %s", item.id, e)
            return []

    async def _hold_for_unread(
        self,
        db: AsyncSession,
        item: PrintQueueItem,
        printer_ids: list[int | None],
    ) -> bool:
        """Hold a filament-blocked item for EVIDENCE instead of staging it (D2).

        Returns True when the item was held — the caller must then skip its staging
        branch and move on; the next tick re-decides.

        The trigger is "this item's requirement is uncovered by the IDENTIFIED slots
        while ``printer_ids`` still hold ≥1 SEATED-but-UNIDENTIFIED tray". Such a tray
        physically contains filament, so neither of the two answers the scheduler used
        to give is true: it is not a shortage ("Low filament" staging, which promotes to
        ``manual_start`` and needs a human press per row — #1496's guard, the one that
        swallowed 12 fleet trays' worth of work) and it is not clear-to-dispatch. The
        honest answer is "unknown — read the slot", so the item stays PENDING and
        un-promoted with ``waiting_reason = filament_unread_pending`` and the deficit
        lane asks the printer for the read.

        ONE canonical path: no shadow mode, no timer, no retry counter. The hold is
        re-derived from live state on every pass and simply stops happening the moment
        the read lands — after which normal dispatch or a GENUINE deficit takes over.
        The token self-clears exactly like ``stagger_hold`` / the capability tokens
        (``_start_print`` drops any waiting reason once the item is cleared to
        dispatch; a real deficit overwrites it via ``_stage_filament_short``).

        Print-Anyway (``skip_filament_check``) bypasses the hold: the operator has
        already accepted dispatching on unverified filament, and re-holding them would
        reopen the #1698 bounce.

        Deliberately does NOT notify. The hold is an evidence round-trip measured in one
        tick, not an operator action — paging for it would be the 2026-07-20 alert-spam
        shape. A slot that STAYS unread is escalated by the AMS side's own
        standing-unknown broadcast, which is where that signal belongs.
        """
        if item.skip_filament_check:
            return False

        unread_by_printer: dict[int, set[tuple[int, int]]] = {}
        for pid in dict.fromkeys(p for p in printer_ids if p is not None):
            slots = live_unread_slots(pid)
            if slots:
                unread_by_printer[pid] = slots
        if not unread_by_printer:
            return False

        newly_held = item.waiting_reason != WAITING_REASON_UNREAD_PENDING
        if newly_held:
            item.waiting_reason = WAITING_REASON_UNREAD_PENDING
            await db.commit()
        log = logger.info if newly_held else logger.debug
        log(
            "Queue item %s: held for unidentified filament — %s (not staged; awaiting an AMS read)",
            item.id,
            ", ".join(f"printer {pid} slots {sorted(slots)}" for pid, slots in unread_by_printer.items()),
        )

        # Ask each printer to resolve its unread slots. Episode-paced inside, and
        # guarded here because an evidence request must never break a dispatch tick.
        for pid, slots in unread_by_printer.items():
            try:
                request_unread_reads(pid, slots)
            except Exception:  # noqa: BLE001 — asking is best-effort; the hold stands
                logger.debug("unread-read request failed for printer %s (non-fatal)", pid, exc_info=True)
        return True

    async def _hold_for_pinned_tray(
        self,
        db: AsyncSession,
        item: PrintQueueItem,
        outcome: MatchOutcome,
    ) -> bool:
        """Hold an item whose operator PIN names a tray the printer is not offering.

        Returns True when the item was held — the caller skips its remaining branches
        and the next tick re-decides.

        A pin miss is NOT a shortage: nothing needs topping up, a specific roll is
        simply not in the machine (pulled, never loaded, unread, out of rotation, on
        another nozzle, or claimed by an earlier slot). Staging it as "Low filament"
        would send the operator to weigh spools; the honest instruction is "load that
        tray, or change the pick". So the item stays PENDING and un-promoted under
        :data:`WAITING_REASON_PINNED_UNAVAILABLE` — self-clearing exactly like the
        unread hold, because the matcher re-decides every tick and the moment the tray
        appears the job dispatches with no further human step.

        Notified once per transition (the same ``_hold_is_new`` rule the filament holds
        use): unlike the unread hold's one-tick evidence round-trip, this one waits on
        a human, and an unnoticed pin miss is a run that never starts.
        """
        if not outcome.pin_missing:
            return False
        prior_reason = item.waiting_reason
        newly_held = prior_reason != WAITING_REASON_PINNED_UNAVAILABLE
        if newly_held:
            item.waiting_reason = WAITING_REASON_PINNED_UNAVAILABLE
            await db.commit()
        log = logger.info if newly_held else logger.debug
        log(
            "Queue item %s: held — pinned tray(s) %s unavailable on printer %s (slots %s)",
            item.id,
            sorted(set(outcome.pin_missing.values())),
            item.printer_id,
            outcome.pinned_unavailable_slots,
        )
        if newly_held:
            printer = await self._get_printer(db, item.printer_id) if item.printer_id else None
            await self._notify_queue_waiting(
                db,
                item,
                WAITING_REASON_PINNED_UNAVAILABLE,
                (printer.model if printer else item.target_model) or "",
            )
        return True

    async def _stage_filament_short(
        self, db: AsyncSession, item: PrintQueueItem, *, unpin: bool, reason: str = "filament_short"
    ) -> None:
        """Mark a queue item low-spool staged (#1496 / #Phase4).

        ``unpin=False`` keeps the item on its assigned printer (pinned path).
        ``unpin=True`` clears the PRINTER assignment — used by the model-based path
        when EVERY eligible printer is short, so ``farm_staging`` releases it and the
        next tick re-runs the full candidate search across the fleet. The item's
        ``ams_mapping`` (the operator's slot instruction) is never cleared here: it is
        not a per-printer derivation to invalidate, and staging must not silently
        discard a human's pick. ``reason`` is the persisted ``waiting_reason`` — callers build a
        human-readable :func:`farm_staging.build_staged_reason` string that NAMES
        the blocked machine(s) (D9); the ``"filament_short"`` default is a legacy
        fallback for a caller that passes none.
        """
        item.filament_short = True
        item.manual_start = True
        item.waiting_reason = reason
        if unpin:
            item.printer_id = None
        await db.commit()

    async def _stage_model_item_filament_short(
        self,
        db: AsyncSession,
        item: PrintQueueItem,
        notified_groups: set,
        reason: str = "filament_short",
    ) -> None:
        """Stage a model-based item UNPINNED when all candidates are blocked, and
        notify AT MOST ONCE per (batch_id, target_model) group per tick.

        The incident sent one waiting notification per unit (10 for a 10-plate
        run); dedup by group keeps a large run to a single notification. ``reason``
        is the persisted + notified waiting reason — a rich
        :func:`farm_staging.build_staged_reason` string from the caller naming the
        short machines (D9).

        ``notified_groups`` is a PER-TICK set, so on its own it collapsed a run to
        one alert *per tick* and a hold lasting an hour still sent ~120 of them
        (the 2026-07-20 spam). The durable transition guard below is what makes the
        alert fire once per hold; the group set keeps its own (still needed)
        one-per-run-per-tick job. Transition first, so a group slot is never
        consumed by an already-held unit while a genuinely new one stays silent.
        """
        was_blocked = bool(item.filament_short)
        prior_reason = item.waiting_reason
        await self._stage_filament_short(db, item, unpin=True, reason=reason)
        logger.info(
            "Queue item %s: every eligible %s printer blocked (%s) — staged UNPINNED",
            item.id,
            item.target_model,
            reason,
        )
        if not self._hold_is_new(was_held=was_blocked, prior_reason=prior_reason, reason=reason):
            return
        group_key = (item.batch_id, item.target_model)
        if group_key in notified_groups:
            return
        notified_groups.add(group_key)
        await self._notify_queue_waiting(db, item, reason, item.target_model or "")

    async def _block_on_filament_deficit(
        self,
        db: AsyncSession,
        item: PrintQueueItem,
        *,
        ams_mapping_override: str | None = None,
    ) -> bool:
        """Promote the pinned item to manual_start when the assigned spool is short (#1496).

        Returns True when this dispatch attempt was blocked, False when the
        item is clear to start. A previously-flagged item whose spool has
        since been swapped to one with enough material clears the flag here
        so the next scheduler tick dispatches it. (The model-based path checks
        candidates inline via ``_compute_deficit_safe`` and does not call this.)

        ``ams_mapping_override`` is THIS dispatch's decided mapping. The dispatch
        caller always passes it: the grams question is "can the trays this print will
        actually feed from cover it?", and since the stored field became an operator
        pin the item can no longer answer that itself (a partial pin would price only
        the pinned slots, an absent one nothing at all).
        """
        # User has explicitly acknowledged the deficit ("Print Anyway") —
        # don't re-flag, don't even compute. Without this short-circuit the
        # scheduler bounces between "user said anyway" (route clears
        # manual_start) and "scheduler re-blocked" (this method re-flags it
        # on identical spool state) (#1698-followup).
        if item.skip_filament_check:
            # #1762 diagnostic: surface the short-circuit at INFO so a
            # future "Print Anyway didn't work" report (e.g. issue #1762
            # comment 3) has actionable evidence in the support bundle
            # without needing DEBUG enabled.
            logger.info(
                "Queue item %s honouring user's Print Anyway acknowledgement — skipping deficit check",
                item.id,
            )
            return False

        deficit = await self._compute_deficit_safe(db, item, ams_mapping_override=ams_mapping_override)

        if deficit and await self._hold_for_unread(db, item, [item.printer_id]):
            # PHANTOM-DEFICIT GUARD: the printer holds a seated-but-unidentified roll,
            # so this shortfall is computed against material the farm simply cannot see
            # yet. Hold for the read instead of promoting to manual_start — a phantom
            # "Low filament" here is what parked real work behind physically loaded
            # trays. Blocked (return True), but NOT staged.
            return True

        if deficit:
            # The deficit re-evaluates on EVERY tick, so notifying unconditionally
            # here re-sent the identical "Low filament" alert every 30 s (the
            # 2026-07-20 incident: 16+ sends in 8 min). Capture the durable hold
            # state BEFORE staging overwrites it and alert only on the transition —
            # the same rule the start-minimum sibling above already applied.
            was_blocked = bool(item.filament_short)
            prior_reason = item.waiting_reason
            printer = await self._get_printer(db, item.printer_id) if item.printer_id else None
            stage_reason = build_staged_reason(printer.name if printer else "")
            await self._stage_filament_short(db, item, unpin=False, reason=stage_reason)
            logger.info(
                "Queue item %s blocked on filament deficit (%d slot(s)) — promoted to manual_start",
                item.id,
                len(deficit),
            )
            if self._hold_is_new(was_held=was_blocked, prior_reason=prior_reason, reason=stage_reason):
                await self._notify_queue_waiting(db, item, stage_reason, (printer.model if printer else "") or "")
            return True

        # No deficit — clear any stale flag from a previous tick.
        if item.filament_short:
            item.filament_short = False
            await db.commit()
        return False

    async def _propagate_owner_to_printer_manager(self, db: AsyncSession, item: PrintQueueItem) -> None:
        """Hand the queue item's owner to printer_manager so the
        print-complete callback can credit the user in PrintLogEntry (#1670).

        No-ops when the item has no `created_by_id` or the referenced user
        row is missing (e.g. user deleted between queue-add and dispatch —
        in that case the print log row falls back to the existing un-credited
        behaviour rather than crashing the dispatch).
        """
        if not item.created_by_id:
            return
        from backend.app.models.user import User

        owner = await db.get(User, item.created_by_id)
        if owner:
            printer_manager.set_current_print_user(item.printer_id, owner.id, owner.username)

    async def _fail_queue_item(self, db: AsyncSession, item: PrintQueueItem, error_message: str) -> None:
        """Mark a queue item terminally failed and route it through farm policy (R5).

        Every dispatch-time failure site in ``_start_print`` funnels through here so a
        farm unit that fails BEFORE the print runs (printer gone, file missing,
        upload/command failure, eject-injection refusal) reaches the same
        ``farm_policy.on_terminal`` hook as a mid-print failure — enabling
        retry/quarantine/pause instead of only counting toward quarantine. Non-farm
        items early-return inside ``on_terminal``, so this is transparent for the
        standard queue. The policy hook is best-effort (mirrors
        ``main.on_print_complete``): a policy error must never abort dispatch.
        """
        item.status = "failed"
        item.error_message = error_message
        item.completed_at = datetime.now(timezone.utc)
        # Terminal-transition hygiene (W4b): NULL any stale hold token in the SAME
        # update that sets the terminal status, so a dispatch-time failure can't
        # leave e.g. a capability-block waiting_reason on a now-failed row.
        item.waiting_reason = None
        await db.commit()
        # Dispatch-progress telemetry: EVERY dispatch-time failure funnels through
        # here, so a single emit covers all failure paths (C4-backend).
        dispatch_progress.emit_queue_item_status(
            item_id=item.id,
            batch_id=item.batch_id,
            printer_id=item.printer_id,
            status="failed",
            phase="failed",
            detail=error_message,
        )
        try:
            from backend.app.services.farm_policy import on_terminal

            await on_terminal(db, item.printer_id, item.id, "failed")
        except Exception as farm_err:  # noqa: BLE001 — policy must never break dispatch
            logger.warning("Queue item %s: farm policy hook (dispatch failure) failed: %s", item.id, farm_err)

    def _plan_dispatch(
        self,
        dispatch_plan: list[_PlannedDispatch],
        planned_printers: set[int],
        item_id: int,
        printer_id: int,
        ams_mapping: list[int] | None = None,
    ) -> None:
        """Record a planned (queue_item, printer, decided mapping) dispatch.

        ``ams_mapping`` is THIS tick's decision for that printer — carried to the
        concurrent dispatch phase instead of being written to the item, so the
        operator's pin stays intact until a print actually starts (see
        :class:`_PlannedDispatch`).

        One printer may appear at most once per tick's plan (point 6): the
        selection loop already guarantees this by adding each pick to
        busy_printers before the next candidate search, so a second entry for the
        same printer would be a selection bug — guard cheaply and drop it rather
        than double-dispatch onto one machine.
        """
        if printer_id in planned_printers:
            logger.error(
                "Dispatch plan already targets printer %s (item %s) — dropping duplicate plan entry",
                printer_id,
                item_id,
            )
            return
        planned_printers.add(printer_id)
        dispatch_plan.append(_PlannedDispatch(item_id=item_id, printer_id=printer_id, ams_mapping=ams_mapping))

    async def _start_print_by_id(
        self, item_id: int, printer_id: int, sem: asyncio.Semaphore, ams_mapping: list[int] | None = None
    ) -> None:
        """Run one planned dispatch concurrently on its OWN session (latency Phase B).

        Bounded by ``sem`` (``dispatch_parallel_limit``). A fresh session lets the
        slow FTPS-upload + start work for one printer overlap another printer's —
        the tick's selection loop already committed every assignment, so this
        re-fetches the item by id. Guards:

        - Idempotency: proceed only if the re-fetched item is still ``pending``. A
          concurrent completion/cancel/failure (or a duplicate plan entry) means
          someone else owns it — log and skip, never re-dispatch.
        - Isolation: one task's crash must neither kill the ``gather`` nor strand
          the item in 'pending'. Any unexpected exception routes through the same
          ``_fail_queue_item`` path ``_start_print`` uses for its own failures. The
          hold paths inside ``_start_print`` (USB pre-flight / capability) leave
          the item pending by design and are NOT failures — they return normally.
        """
        # Phase E: guarantee the stagger in-flight slot frees on EVERY exit path —
        # success (started_at/status flip lets the durable window record take
        # over), failure/skip/hold (slot frees immediately) — so a kick can't
        # over-admit a heater against a stale in-flight entry. The ramp-watch is
        # left armed (it fires when the bed reaches target while printing).
        try:
            async with sem, async_session() as session:
                item = await session.get(PrintQueueItem, item_id)
                if item is None:
                    logger.warning("Dispatch (printer %s): queue item %s vanished — skipping", printer_id, item_id)
                    return
                if item.status != "pending":
                    logger.info(
                        "Dispatch (printer %s): queue item %s no longer pending (status=%s) — skipping",
                        printer_id,
                        item_id,
                        item.status,
                    )
                    return
                try:
                    await self._start_print(session, item, ams_mapping=ams_mapping)
                except Exception as exc:  # noqa: BLE001 — one task must not kill the gather
                    logger.exception("Dispatch (printer %s): queue item %s crashed during start", printer_id, item_id)
                    try:
                        # _start_print may have rolled the session back mid-failure;
                        # re-fetch before routing through the terminal-failure path.
                        fresh = await session.get(PrintQueueItem, item_id)
                        if fresh is not None and fresh.status == "pending":
                            await self._fail_queue_item(session, fresh, f"Dispatch error: {exc}")
                    except Exception:
                        logger.exception(
                            "Dispatch (printer %s): could not fail item %s after crash", printer_id, item_id
                        )
                    return

                # Outcome-based bookkeeping (moved from the old inline loop, now that
                # dispatch runs concurrently): a real dispatch ("printing") ends this
                # unit's hold-unpinned notification-suppression window — a later hold on
                # a NEW assignment is a fresh transition. A USB/capability HOLD leaves
                # the item pending and keeps the guard. Re-read the status durably
                # rather than touching a possibly-expired attribute after the commits.
                outcome = await session.get(PrintQueueItem, item_id)
                if outcome is not None and outcome.status == "printing":
                    self._hold_unpinned_items.discard(item_id)
        finally:
            stagger_policy.note_dispatch_settled(item_id)

    async def _start_print(self, db: AsyncSession, item: PrintQueueItem, *, ams_mapping: list[int] | None = None):
        """Upload file and start print for a queue item.

        Supports two sources:
        - archive_id: Print from an existing archive
        - library_file_id: Print from a library file (file manager)

        ``ams_mapping`` is the mapping the scheduler DECIDED for this dispatch
        (``_compute_ams_mapping_for_printer`` against live tray state, pins honoured).
        It is passed in rather than read off the item because ``item.ams_mapping``
        holds the operator's PIN until a print starts — the decision is recorded onto
        the item at the point of no return below, where it becomes the durable record
        of what actually ran (items are single-dispatch, so it never round-trips as a
        cache). ``None`` = a mapping-free dispatch: nothing is sent and nothing is
        recorded, which is also what the hold paths above leave behind when they
        return early.
        """
        logger.info("Starting queue item %s", item.id)

        # Dispatch-progress telemetry (C3): the unit is picked and dispatch is
        # starting. `uploading`/`sent` follow below; failures emit via _fail_queue_item.
        dispatch_progress.emit_queue_item_status(
            item_id=item.id,
            batch_id=item.batch_id,
            printer_id=item.printer_id,
            status="pending",
            phase="assigned",
        )

        # Get printer first (needed for both paths)
        result = await db.execute(select(Printer).where(Printer.id == item.printer_id))
        printer = result.scalar_one_or_none()
        if not printer:
            await self._fail_queue_item(db, item, "Printer not found")
            logger.error("Queue item %s: Printer %s not found", item.id, item.printer_id)
            await self._power_off_if_needed(db, item)
            return

        # Check printer is connected
        if not printer_manager.is_connected(item.printer_id):
            await self._fail_queue_item(db, item, "Printer not connected")
            logger.error("Queue item %s: Printer %s not connected", item.id, item.printer_id)
            await self._power_off_if_needed(db, item)
            return

        # USB pre-flight (every item — farm and non-farm; the USB stick is
        # universal). The H2 fleet has NO usable internal storage for LAN
        # dispatch, so an absent USB drive turns every FTPS upload into an
        # opaque 553. The firmware only reports USB presence (state.sdcard) in a
        # FULL status report, which Bambuddy requests on connect / manual
        # refresh — so a stick pulled while the printer idles goes unnoticed
        # until dispatch fails. Fail-OPEN: ONLY an explicit False (drive confirmed
        # absent) holds dispatch; None/missing (never reported / stale) proceeds,
        # mirroring the UI chip's fail-safe. This is a WAIT, not a failure — the item
        # stays pending, no manual_start, no retry burn; the next tick re-checks and
        # self-clears it when the drive returns (via the capability gate's existing
        # waiting_reason reset below, since this block sits BEFORE it on the path).
        # Smart pre-flight (latency Phase A): the firmware reports USB presence only
        # inside a FULL status report. If one already landed within the fresh window
        # we trust the cached flag (no request, no wait). Otherwise request a fresh
        # report and wait on the client's full-report Event — which fires the instant
        # a report carrying `sdcard` merges — up to the max-wait cap, then read.
        fresh_window = max(
            0, min(120, await self._get_int_setting(db, "usb_preflight_fresh_window_seconds", default=10))
        )
        max_wait = max(0.0, min(10.0, await self._get_float_setting(db, "usb_preflight_max_wait_seconds", default=2.5)))
        usb_status = printer_manager.get_status(item.printer_id)
        last_report_at = getattr(usb_status, "last_full_report_at", 0.0) if usb_status is not None else 0.0
        is_fresh = (
            isinstance(last_report_at, (int, float))
            and last_report_at > 0.0
            and (time.monotonic() - last_report_at) <= fresh_window
        )
        if not is_fresh:
            client = printer_manager.get_client(item.printer_id)
            arm = getattr(client, "arm_full_report_wait", None) if client is not None else None
            report_event = arm() if arm is not None else None
            printer_manager.request_status_update(item.printer_id)
            if report_event is not None and max_wait > 0.0:
                try:
                    await asyncio.wait_for(report_event.wait(), max_wait)
                except asyncio.TimeoutError:
                    pass
            usb_status = printer_manager.get_status(item.printer_id)
        if usb_status is not None and getattr(usb_status, "sdcard", None) is False:
            # Read the pin before the hold releases it, so the log names the printer
            # that actually lacked the stick.
            held_printer_id = item.printer_id
            await self._hold_dispatch_precondition(db, item, printer, "no_usb_drive")
            logger.info(
                "Queue item %s: USB pre-flight held dispatch — no USB drive in printer %s",
                item.id,
                held_printer_id,
            )
            return

        # Farm capability-matching gate (#Phase4). Non-farm items bypass it. A
        # BLOCK is NOT a failure: record the reason on waiting_reason (surfaced in
        # the queue UI), leave the item pending, and let a later tick re-evaluate
        # (a swapped spool / corrected assignment clears it). This is the single
        # call from the scheduler's dispatch path.
        from backend.app.services.capability_gate import check_dispatch_capability

        capability = await check_dispatch_capability(db, item, printer)
        if not capability.ok:
            # Same un-pin rationale as the USB hold above: a capability BLOCK on a
            # sick-but-idle printer (nozzle/model/filament mismatch) must not pin a
            # model-targeted unit onto it, or the run funnels one unit per tick onto
            # the mismatched printer. Release the scheduler-made printer pin so the
            # model path re-evaluates the fleet next tick; always commit the un-pin so
            # a re-held unit still ends unpinned. The operator's ``ams_mapping``
            # instruction is left alone — the matcher re-decides per candidate anyway,
            # and a capability block is not a reason to forget a human's slot choice. A
            # user-pinned unit (no target_model) holds in place, committing only on a
            # reason change, exactly as before.
            if item.target_model:
                item.printer_id = None
                item.waiting_reason = capability.reason
                # Once-guard for the model path's per-assignment notification
                # (see _hold_unpinned_items in __init__).
                self._hold_unpinned_items.add(item.id)
                await db.commit()
            elif item.waiting_reason != capability.reason:
                item.waiting_reason = capability.reason
                await db.commit()
            logger.info("Queue item %s: capability gate held dispatch — %s", item.id, capability.reason)
            return
        if capability.warn:
            logger.warning("Queue item %s: capability warn-dispatch — %s", item.id, capability.reason)

        # Determine source: archive or library file
        archive = None
        library_file = None
        file_path = None
        filename = None
        cleanup_disk_paths: list[Path] = []

        if item.archive_id:
            # Print from archive
            result = await db.execute(select(PrintArchive).where(PrintArchive.id == item.archive_id))
            archive = result.scalar_one_or_none()
            if not archive:
                await self._fail_queue_item(db, item, "Archive not found")
                logger.error("Queue item %s: Archive %s not found", item.id, item.archive_id)
                await self._power_off_if_needed(db, item)
                return

            file_path = settings.base_dir / archive.file_path
            filename = archive.filename

        elif item.library_file_id:
            # Print from library file (file manager)
            result = await db.execute(LibraryFile.active().where(LibraryFile.id == item.library_file_id))
            library_file = result.scalar_one_or_none()
            if not library_file:
                await self._fail_queue_item(db, item, "Library file not found")
                logger.error("Queue item %s: Library file %s not found", item.id, item.library_file_id)
                await self._power_off_if_needed(db, item)
                return
            # Library files store absolute paths
            lib_path = Path(library_file.file_path)
            file_path = lib_path if lib_path.is_absolute() else settings.base_dir / library_file.file_path
            filename = library_file.filename

            # The source file must exist BEFORE anything tries to copy it. A library
            # row whose file is gone from disk is an unmet dispatch PRECONDITION, not
            # a print failure: nothing about the printer is wrong, and failing the
            # item neither restores the file nor stops the next unit trying. It used
            # to reach the archive step first and surface as a FileNotFoundError there
            # (handled below as a genuine IO failure) — on 2026-08-14 that insta-failed
            # all 22 pending units of a deleted file, and two consecutive insta-fails
            # quarantined each healthy printer in turn until the fleet had shut itself
            # down. Held here as a WAIT instead, so restoring or re-uploading the file
            # self-clears it on the next pass. The generic on-disk check further down
            # stays as the archive-source equivalent.
            if not file_path.exists():
                await self._hold_dispatch_precondition(db, item, printer, WAITING_REASON_LIBRARY_FILE_MISSING)
                logger.warning(
                    "Queue item %s: dispatch held — library file %s missing from disk (%s)",
                    item.id,
                    item.library_file_id,
                    file_path,
                )
                return

            # Create archive from library file so usage tracking has access to the 3MF
            queue_item_id = item.id
            try:
                from backend.app.services.archive import ArchiveService

                archive_service = ArchiveService(db)
                archive = await archive_service.archive_print(
                    printer_id=item.printer_id,
                    source_file=file_path,
                    original_filename=filename,
                    created_by_id=item.created_by_id,
                    project_id=item.project_id,
                    # Scope the parse to the plate this farm unit prints (#1697).
                    # Farm production units carry library_file_id + plate_id
                    # (production_run.py sets plate_id=sku_file.plate_index), so
                    # without this the dispatch-time archive stores the summed-
                    # across-plates totals for a single-plate print. source_file
                    # here is the ORIGINAL library file — any G-code injection
                    # happens later (~:2415), after this parse.
                    plate_id=item.plate_id,
                )
                if archive:
                    item.archive_id = archive.id
                    # Reaping the source needs BOTH the dispatch's intent and the
                    # ROW's own truth. ``cleanup_library_after_dispatch`` arrives on
                    # the create-queue-item request, so on its own it let any API
                    # caller aim this deletion at a pre-existing user library entry;
                    # ``transient`` is stamped only where the Direct-Print lane
                    # creates the row it is about to print, and is what actually
                    # authorises the delete. They answer different questions — may
                    # THIS dispatch reap, and is THIS row reapable — so both are
                    # required, and a flag pointed at an ordinary library file is
                    # refused out loud rather than silently obeyed.
                    if item.cleanup_library_after_dispatch:
                        if not library_file.transient:
                            logger.warning(
                                "Queue item %s: refusing cleanup_library_after_dispatch — library file %s is not a "
                                "transient upload; only the row's own origin authorises deleting it",
                                item.id,
                                library_file.id,
                            )
                        elif not library_file.is_external:
                            item.library_file_id = None
                            cleanup_disk_paths.append(file_path)
                            if library_file.thumbnail_path:
                                thumb_path = Path(library_file.thumbnail_path)
                                if not thumb_path.is_absolute():
                                    thumb_path = settings.base_dir / library_file.thumbnail_path
                                cleanup_disk_paths.append(thumb_path)
                            await db.delete(library_file)
                            file_path = settings.base_dir / archive.file_path
                            filename = archive.filename
                    await db.flush()
                    logger.info(
                        "Queue item %s: Created archive %s from library file %s",
                        item.id,
                        archive.id,
                        item.library_file_id,
                    )
            except Exception as e:
                logger.warning(
                    "Queue item %s: Failed to create archive from library file: %s",
                    queue_item_id,
                    e,
                    exc_info=True,
                )
                await db.rollback()
                item = await db.get(PrintQueueItem, queue_item_id)
                if item:
                    await self._fail_queue_item(db, item, "Failed to create archive from library file")
                    await self._power_off_if_needed(db, item)
                return

            if not archive:
                await self._fail_queue_item(db, item, "Failed to create archive from library file")
                logger.error("Queue item %s: Archive creation from library file returned no archive", item.id)
                await self._power_off_if_needed(db, item)
                return

        else:
            # Neither archive nor library file specified
            await self._fail_queue_item(db, item, "No source file specified")
            logger.error("Queue item %s: No archive_id or library_file_id specified", item.id)
            await self._power_off_if_needed(db, item)
            return

        # Check file exists on disk
        if not file_path.exists():
            await self._fail_queue_item(db, item, "Source file not found on disk")
            logger.error("Queue item %s: File not found: %s", item.id, file_path)
            await self._power_off_if_needed(db, item)
            return

        # The DURABLE on-disk copy of what this dispatch uploads — the library file,
        # or the archive copy when a transient library row was cleaned up above. Held
        # separately because ``file_path`` is about to be rebound to the injected
        # system-temp file, which is unlinked right after the upload: caching THAT in
        # the cover cache publishes an already-deleted path and silently defeats the
        # #1166 FTP-free cover on every injected dispatch.
        durable_path = file_path

        # G-code injection for auto-print systems (#422): the upstream global
        # per-model start/end snippets only. Farm auto-eject is NOT injected here
        # anymore (it is a separate server-dispatched motion-only job).
        injected_path = None
        start_gc: str | None = None
        end_gc: str | None = None
        if item.gcode_injection:
            try:
                snippets_raw = await self._get_setting(db, "gcode_snippets")
                if snippets_raw:
                    snippets = json.loads(snippets_raw)
                    model_snippets = snippets.get(printer.model, {})
                    start_gc = (model_snippets.get("start_gcode") or "").strip() or None
                    end_gc = (model_snippets.get("end_gcode") or "").strip() or None
            except Exception as e:
                logger.warning("Queue item %s: G-code snippet load failed, using original: %s", item.id, e)
                start_gc = end_gc = None

        # Farm auto-eject no longer injects anything here: the eject sweep is a
        # SEPARATE server-dispatched motion-only job (the eject monitor dispatches
        # it after the unit's cooldown gate releases). Print files ship UNMODIFIED
        # apart from the upstream global per-model start/end snippets below.
        if start_gc or end_gc:
            try:
                from backend.app.utils.threemf_tools import inject_gcode_into_3mf

                injected_path = inject_gcode_into_3mf(file_path, item.plate_id or 1, start_gc, end_gc)
            except Exception as e:
                injected_path = None
                logger.warning("Queue item %s: G-code injection failed: %s", item.id, e)

            if injected_path:
                file_path = injected_path
                logger.info("Queue item %s: G-code injected for model %s", item.id, printer.model)
            else:
                logger.warning("Queue item %s: G-code injection returned no result, using original", item.id)

        # Upload to root directory (not /cache/) - the start_print command references
        # files by name only (ftp://{filename}), so they must be in the root
        remote_filename = derive_remote_filename(filename)
        remote_path = f"/{remote_filename}"

        # Get FTP retry settings
        ftp_retry_enabled, ftp_retry_count, ftp_retry_delay, ftp_timeout = await get_ftp_retry_settings()

        logger.info(
            f"Queue item {item.id}: FTP upload starting - printer={printer.name} ({printer.model}), "
            f"ip={printer.ip_address}, file={remote_filename}, local_path={file_path}, "
            f"retry_enabled={ftp_retry_enabled}, retry_count={ftp_retry_count}, timeout={ftp_timeout}"
        )

        # Delete existing file if present (avoids 553 error on overwrite)
        try:
            logger.debug("Queue item %s: Deleting existing file %s if present...", item.id, remote_path)
            delete_result = await delete_file_async(
                printer.ip_address,
                printer.access_code,
                remote_path,
                socket_timeout=ftp_timeout,
                printer_model=printer.model,
            )
            logger.debug("Queue item %s: Delete result: %s", item.id, delete_result)
        except Exception as e:
            logger.debug("Queue item %s: Delete failed (may not exist): %s", item.id, e)

        # An FTPS upload makes the H2S firmware transiently report sdcard=false;
        # mark the printer upload-in-flight so the USB-drop verifier treats that edge
        # as a dispatch blip, not a genuine drop.
        # Dispatch-progress telemetry (C3): the FTP callback fires on the executor
        # thread, so marshal each tick back onto the loop before emitting. The
        # callback must never raise (a raise aborts the upload).
        _loop = asyncio.get_running_loop()
        _prog_item_id, _prog_batch_id, _prog_printer_id = item.id, item.batch_id, item.printer_id

        def _on_upload_progress(uploaded_bytes: int, total_bytes: int) -> None:
            pct = round(uploaded_bytes / total_bytes * 100.0, 1) if total_bytes else None
            _loop.call_soon_threadsafe(
                lambda: dispatch_progress.emit_queue_item_status(
                    item_id=_prog_item_id,
                    batch_id=_prog_batch_id,
                    printer_id=_prog_printer_id,
                    status="pending",
                    phase="uploading",
                    progress_pct=pct,
                )
            )

        try:
            async with upload_in_flight(printer.id):
                if ftp_retry_enabled:
                    uploaded = await with_ftp_retry(
                        upload_file_async,
                        printer.ip_address,
                        printer.access_code,
                        file_path,
                        remote_path,
                        socket_timeout=ftp_timeout,
                        printer_model=printer.model,
                        progress_callback=_on_upload_progress,
                        max_retries=ftp_retry_count,
                        retry_delay=ftp_retry_delay,
                        operation_name=f"Upload print to {printer.name}",
                    )
                else:
                    uploaded = await upload_file_async(
                        printer.ip_address,
                        printer.access_code,
                        file_path,
                        remote_path,
                        socket_timeout=ftp_timeout,
                        printer_model=printer.model,
                        progress_callback=_on_upload_progress,
                    )
        except Exception as e:
            uploaded = False
            logger.error("Queue item %s: FTP error: %s (type: %s)", item.id, e, type(e).__name__)

        # Clean up injected temp file after upload attempt
        cleanup_downloaded_3mf(injected_path)

        if not uploaded:
            error_msg = (
                "Failed to upload file to printer. Check if SD card is inserted and properly formatted (FAT32/exFAT). "
                "See server logs for detailed diagnostics."
            )
            await self._fail_queue_item(db, item, error_msg)
            logger.error(
                f"Queue item {item.id}: FTP upload failed - printer={printer.name}, model={printer.model}, "
                f"ip={printer.ip_address}. Check logs above for storage diagnostics and specific error codes."
            )

            # Send failure notification
            await notification_service.on_queue_job_failed(
                job_name=filename.replace(".gcode.3mf", "").replace(".3mf", ""),
                printer_id=printer.id,
                printer_name=printer.name,
                reason="Failed to upload file to printer",
                db=db,
            )
            await self._power_off_if_needed(db, item)
            return

        # Register as expected print so we don't create a duplicate archive
        # Only applicable for archive-based prints
        if archive:
            from backend.app.main import register_expected_print

            register_expected_print(
                item.printer_id,
                remote_filename,
                archive.id,
                ams_mapping=ams_mapping,
                created_by_id=item.created_by_id,
                plate_id=item.plate_id,
            )

        # Propagate the queue item's owner into printer_manager so the
        # print-complete callback can credit the user in the PrintLogEntry
        # (#1670). `created_by_id` is set either at queue-add time (UI-added
        # items) or when the user clicks the manual-start button.
        await self._propagate_owner_to_printer_manager(db, item)

        # IMPORTANT: Set status to "printing" BEFORE sending the print command.
        # This prevents phantom reprints if the backend crashes/restarts after the
        # print command is sent but before the status update is committed.
        # If we crash after this commit but before start_print(), the item will be
        # in "printing" status without actually printing - but that's safer than
        # accidentally reprinting the same file hours later.
        #
        # The write is the canonical transition (``queue_transitions``), not four ORM
        # assignments: ``item`` was loaded before a multi-second FTPS upload, so "it
        # was pending" is a stale read of a row other sessions can move. An operator
        # cancel landing in that gap used to be overwritten straight back to
        # 'printing' and the print sent anyway. ``pending`` now lives in the UPDATE's
        # WHERE, so the database decides. The columns and their reasons are unchanged:
        #   - ``waiting_reason`` is cleared to dispatch, HERE and not at the capability
        #     gate further up — every gate BELOW that gate re-reads ``waiting_reason``
        #     to tell a NEW hold from one already standing, and clearing it early made
        #     each of them look new on every tick (re-notifying an operator once per
        #     tick about one unchanged hold). Every non-dispatch exit from here on
        #     either sets its own reason or goes through ``_fail_queue_item``, which
        #     NULLs it.
        #   - ``ams_mapping`` records what this dispatch actually feeds from. Before
        #     this write the field is the operator's INSTRUCTION (a pin, or nothing);
        #     from here on it is the RECORD of the decision that ran, which is what the
        #     ledger, usage tracking and recovery lanes read back for a
        #     printing/completed unit. Written here — past the USB and capability holds
        #     — so a held unit keeps its pin and no decision can ever be re-read as one
        #     on a later tick.
        item_id = item.id
        claimed = await claim_pending_for_dispatch(
            db,
            item_id=item_id,
            started_at=datetime.now(timezone.utc),
            ams_mapping=json.dumps(ams_mapping) if ams_mapping else None,
        )
        if not claimed:
            # Someone moved the row while we were uploading — an operator cancel
            # (queue/batch/run abort), or any other terminal. Not a failure of THIS
            # unit and not ours to mark: the actor that moved it owns its lifecycle,
            # so nothing terminal, no retry burn, no quarantine contribution. Nothing
            # is held to release either — the upload's in-flight marker exited with
            # its context manager, the stagger slot frees in ``_start_print_by_id``'s
            # finally, and the post-dispatch printer hold is only taken once the print
            # command has actually gone out.
            #
            # The upload itself IS undone, for the same "prevent phantom prints" reason
            # the send-failure branch below deletes its own: a cancelled unit's file
            # left sitting on the USB is one screen-tap away from being started as a
            # FOREIGN print, which is precisely the class this transition exists to
            # close. Best-effort — a delete that fails is named in the warning, never
            # raised, because the row is already someone else's business.
            current_status = await db.scalar(select(PrintQueueItem.status).where(PrintQueueItem.id == item_id))
            try:
                removed = bool(
                    await delete_file_async(
                        printer.ip_address,
                        printer.access_code,
                        remote_path,
                        printer_model=printer.model,
                    )
                )
            except Exception as cleanup_err:  # noqa: BLE001 — best-effort, must not raise
                removed = False
                logger.debug("Queue item %s: USB cleanup after a refused claim failed: %s", item_id, cleanup_err)
            logger.warning(
                "Queue item %s: left 'pending' during dispatch (now %r) — print command NOT sent; "
                "uploaded file %s %s printer %s",
                item_id,
                current_status,
                remote_filename,
                "removed from" if removed else "COULD NOT BE REMOVED from",
                item.printer_id,
            )
            # Discard this dispatch's uncommitted work (the archive link, and a
            # transient library row it was about to reap) rather than letting the
            # session close decide: none of it describes a print that will happen.
            await db.rollback()
            return
        # The UPDATE ran with ``synchronize_session=False`` and this fork's sessions
        # do not expire on commit, so the in-memory row still says 'pending'. Every
        # read below (and the ``dispatch_subtask_id`` stamp, which writes through this
        # same instance) must see the row the database now holds.
        await db.refresh(item)
        await db.commit()

        for cleanup_path in cleanup_disk_paths:
            try:
                if cleanup_path.exists():
                    cleanup_path.unlink()
            except OSError as cleanup_err:
                logger.warning(
                    "TRANSIENT_LIBRARY_FILE_ORPHAN %s",
                    json.dumps(
                        {
                            "queue_item_id": item.id,
                            "path": str(cleanup_path),
                            "error": str(cleanup_err),
                        },
                        sort_keys=True,
                    ),
                )

        # Clear the awaiting-plate-clear flag now that we're starting a new print
        printer_manager.set_awaiting_plate_clear(item.printer_id, False)
        logger.info("Queue item %s: Status set to 'printing', sending print command...", item.id)

        # Capture state before dispatch so the watchdog can detect whether the
        # printer actually transitioned (#967). Also capture subtask_id so the
        # watchdog can recognise "command landed but state hasn't flipped yet"
        # on slow H2D transitions (#1078).
        pre_status = printer_manager.get_status(item.printer_id)
        pre_state = getattr(pre_status, "state", None) if pre_status else None
        pre_subtask_id = getattr(pre_status, "subtask_id", None) if pre_status else None
        pre_gcode_file = getattr(pre_status, "gcode_file", None) if pre_status else None

        # #1721: respect the user's explicit timelapse choice. The #1397
        # force-on at dispatch was removed because it caused per-layer nozzle
        # parking on slicer profiles with Timelapse Type = Smooth. Finish-photo
        # capture is now driven by the stg_cur=22 transition in bambu_mqtt.py
        # ("Filament unloading", toolhead parked, bed not yet dropped) with a
        # FINISH-state fallback — no need to force a video.
        effective_timelapse = bool(item.timelapse)

        # Start the print with AMS mapping, plate_id and print options.
        # nozzle_mapping rides through verbatim — JSON string captured from
        # Bambu Studio's project_file on VP intake (#1780); the MQTT layer
        # parses + injects it only for dual-nozzle models so a null on every
        # other model is a transparent pass-through.
        started = printer_manager.start_print(
            item.printer_id,
            remote_filename,
            plate_id=item.plate_id or 1,
            ams_mapping=ams_mapping,
            bed_levelling=item.bed_levelling,
            flow_cali=item.flow_cali,
            vibration_cali=item.vibration_cali,
            layer_inspect=item.layer_inspect,
            timelapse=effective_timelapse,
            use_ams=item.use_ams,
            nozzle_offset_cali=item.nozzle_offset_cali,
            nozzle_mapping=item.nozzle_mapping,
        )

        if started:
            logger.info("Queue item %s: Print started successfully - %s", item.id, filename)

            # Dispatch-progress telemetry (C3): the start command was accepted. The
            # watchdog emits `preparing`/`printing` as the printer transitions.
            dispatch_progress.emit_queue_item_status(
                item_id=item.id,
                batch_id=item.batch_id,
                printer_id=item.printer_id,
                status="printing",
                phase="sent",
            )

            # Correlation (Phase 1, P1-A): stamp the subtask_id minted for THIS
            # dispatch so a terminal MQTT status can be bound back to this exact
            # queue item (not a printer_id-only lookup). start_print set it on the
            # client synchronously above; commit it with the already-'printing' row.
            item.dispatch_subtask_id = getattr(
                printer_manager.get_client(item.printer_id), "last_dispatch_subtask_id", None
            )
            await db.commit()

            # Register the local 3MF in the cover-cache so /cover skips FTP
            # (#1166 follow-up). Always the DURABLE copy, never the injected temp
            # file — that one is already unlinked by the time a cover is requested.
            if durable_path is not None:
                cache_3mf_download(item.printer_id, remote_filename, durable_path)

            # Hold the printer against further dispatches until the watchdog
            # confirms the printer transitioned (or until the hard timeout).
            # Prevents multi-plate batches from triple-dispatching onto the
            # same H2D Pro while it digests the first project_file (#1157).
            self._mark_printer_dispatched(item.printer_id, pre_state, pre_subtask_id)

            # Watchdog: if the printer never transitions out of pre_state AND
            # never advances subtask_id, the MQTT publish was accepted locally but
            # didn't reach the printer (half-broken session — same shape as
            # #887/#936). Revert the queue item so the next dispatch can pick it
            # up instead of leaving it stuck in "printing" (#967). subtask_id
            # check avoids false reverts on slow H2D FINISH→PREPARE transitions
            # that would otherwise cause the item to re-dispatch as a reprint
            # of the just-finished job (#1078).
            if pre_state:
                spawn_background_task(
                    self._watchdog_print_start(
                        item.id,
                        item.printer_id,
                        pre_state,
                        pre_subtask_id,
                        pre_gcode_file,
                        item.batch_id,
                    ),
                    name=f"watchdog-print-start-{item.id}",
                )

            # Get estimated time for notification
            estimated_time = _derive_estimated_time(archive, library_file)

            # Send job started notification
            await notification_service.on_queue_job_started(
                job_name=filename.replace(".gcode.3mf", "").replace(".3mf", ""),
                printer_id=printer.id,
                printer_name=printer.name,
                db=db,
                estimated_time=estimated_time,
            )

            # MQTT relay - publish queue job started
            try:
                from backend.app.services.mqtt_relay import mqtt_relay

                await mqtt_relay.on_queue_job_started(
                    job_id=item.id,
                    filename=filename,
                    printer_id=printer.id,
                    printer_name=printer.name,
                    printer_serial=printer.serial_number,
                )
            except Exception:
                pass  # Don't fail if MQTT fails
        else:
            # Clean up uploaded file from SD card to prevent phantom prints
            try:
                await delete_file_async(
                    printer.ip_address,
                    printer.access_code,
                    remote_path,
                    printer_model=printer.model,
                )
            except Exception:
                pass  # Best-effort — don't fail the error handler

            # Print command failed - revert status
            await self._fail_queue_item(db, item, "Failed to send print command to printer")
            logger.error(
                f"Queue item {item.id}: Failed to start print on {printer.name} ({printer.model}) - "
                f"printer_manager.start_print() returned False. "
                f"This may indicate: printer not connected, MQTT error, unsupported model configuration, or firmware issue. "
                f"Check printer status and backend logs for details."
            )

            # Send failure notification
            await notification_service.on_queue_job_failed(
                job_name=filename.replace(".gcode.3mf", "").replace(".3mf", ""),
                printer_id=printer.id,
                printer_name=printer.name,
                reason="Failed to send print command to printer - check printer connection and status",
                db=db,
            )

            await self._power_off_if_needed(db, item)

    @staticmethod
    async def _watchdog_print_start(
        queue_item_id: int,
        printer_id: int,
        pre_state: str,
        pre_subtask_id: str | None = None,
        pre_gcode_file: str | None = None,
        batch_id: int | None = None,
        timeout: float = 90.0,
        phase_b_timeout: float = 180.0,
        poll_interval: float = 3.0,
    ) -> None:
        """Revert a queue item if the printer never acknowledges the start command.

        Bambuddy optimistically marks the queue item as "printing" right after the
        MQTT project_file publish succeeds locally. The watchdog runs in two phases:

        Phase A (up to ``timeout``): wait for either an active-state transition
        or a ``subtask_id`` advance past ``pre_subtask_id``. State alone is the
        primary signal; subtask_id advance handles the H2D case where state can
        sit at FINISH for ~50 s after the printer accepted ``project_file``
        before flipping to PREPARE (#1078). If neither happens, the MQTT publish
        was lost on a half-broken session (#887/#936) — revert and force
        reconnect (the #967 recovery path).

        Phase B (up to ``phase_b_timeout``, only if Phase A exited on subtask_id
        alone): keep watching for the active-state transition. subtask_id alone
        proves the file landed but not that the printer started — and a printer
        that accepts the command but stays at IDLE/FINISH indefinitely (e.g.
        cloud+LAN re-auth dance after a power cycle on old firmware, #1678)
        used to leave the queue item stuck in 'printing' forever because the
        old watchdog returned success as soon as subtask_id advanced. If Phase
        B times out, revert the queue item so the user can retry without
        restarting Bambuddy. Skip ``force_reconnect`` here: the file landed and
        a forced reconnect mid-parse triggers 0500_4003 (#1150).

        Phase A timeout raised from 45 s → 90 s as belt-and-braces for slow
        transitions that also don't emit an early subtask_id tick.
        """
        last_status = None
        landed_on_subtask = False
        # Dispatch-progress telemetry (C3): emit `preparing`/`printing` the FIRST time
        # each is observed (deduped here so a phase is broadcast once, not per poll).
        _emitted_phases: set[str] = set()

        def _emit_observed_phase(state: str | None) -> None:
            ph = dispatch_progress.phase_for_observed_state(state)
            if ph and ph not in _emitted_phases:
                _emitted_phases.add(ph)
                dispatch_progress.emit_queue_item_status(
                    item_id=queue_item_id,
                    batch_id=batch_id,
                    printer_id=printer_id,
                    status="printing",
                    phase=ph,
                )

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(poll_interval)
            status = printer_manager.get_status(printer_id)
            if not status:
                # Printer disconnected — don't mess with the DB. Drop the
                # in-memory dispatch hold too so a fresh dispatch can retry
                # once the printer comes back; the hard timeout would
                # otherwise hold the printer unnecessarily.
                scheduler._release_dispatch_hold(printer_id)
                return
            last_status = status
            _emit_observed_phase(status.state)
            if status.state in _ACTIVE_PRINT_STATES:
                # Printer is actively processing the job — release the
                # post-dispatch hold so the next pending item for this printer
                # can be evaluated normally. We do NOT accept arbitrary state
                # transitions: a printer going FINISH -> IDLE (user dismissed
                # the post-print prompt without accepting our project_file)
                # would otherwise look like "command landed" and leave the
                # queue item stuck in 'printing' forever (#1370).
                scheduler._release_dispatch_hold(printer_id)
                return
            if pre_subtask_id is not None and status.subtask_id is not None and status.subtask_id != pre_subtask_id:
                # Phase A exit — printer accepted the file (subtask_id flipped
                # to our submission id). Don't return yet: the printer may
                # have accepted the command but never actually start (e.g.
                # cloud+LAN re-auth dance after a power cycle, #1678). Phase
                # B watches for the active-state transition.
                landed_on_subtask = True
                break

        if landed_on_subtask:
            phase_b_deadline = time.monotonic() + phase_b_timeout
            while time.monotonic() < phase_b_deadline:
                await asyncio.sleep(poll_interval)
                status = printer_manager.get_status(printer_id)
                if not status:
                    scheduler._release_dispatch_hold(printer_id)
                    return
                last_status = status
                _emit_observed_phase(status.state)
                if status.state in _ACTIVE_PRINT_STATES:
                    scheduler._release_dispatch_hold(printer_id)
                    return

        # No active-state transition. Revert the item so the scheduler can retry.
        # Drop the in-memory hold so the retry isn't blocked by it.
        scheduler._release_dispatch_hold(printer_id)

        # Three outcomes from the revert attempt, each routed differently:
        #   "reverted":          row flipped from printing -> pending, run recovery
        #   "already_moved_on":  item.status != 'printing' (completed/cancelled by
        #                        on_print_complete or user). Skip recovery entirely
        #                        — the print clearly landed somewhere even if the
        #                        watchdog didn't see the active-state transition.
        #   "revert_failed":     SQLite contention exhausted retries. Still run
        #                        recovery so the MQTT session gets a fresh client_id
        #                        on the half-broken-session path.
        async def _do_revert(db):
            item = await db.get(PrintQueueItem, queue_item_id)
            if not item or item.status != "printing":
                return "already_moved_on"
            item.status = "pending"
            item.started_at = None
            # Drop the dispatch RECORD written at start: that dispatch never landed,
            # so the field must not describe it. Leaving it would also hand the next
            # tick's matcher a decision dressed as an operator pin — the one thing the
            # contract forbids. The retry re-decides from live state. printer_id
            # pinning is deliberately left untouched.
            item.ams_mapping = None
            await db.commit()
            return "reverted"

        try:
            revert_outcome = await run_with_retry(_do_revert, label=f"watchdog revert item={queue_item_id}")
        except Exception as e:
            logger.warning(
                "Queue item %s: failed to revert to 'pending' (printer %d): %s — "
                "scheduler may keep treating this item as in-flight",
                queue_item_id,
                printer_id,
                e,
            )
            revert_outcome = "revert_failed"

        if revert_outcome == "already_moved_on":
            # Preserves the pre-#1370 early-return: if on_print_complete (or any
            # other path) already moved the item past 'printing', don't run the
            # MQTT session-recovery logic below — a forced reconnect on a healthy
            # session breaks ongoing prints on the same printer.
            return

        total_timeout = timeout + (phase_b_timeout if landed_on_subtask else 0.0)
        if revert_outcome == "reverted":
            if landed_on_subtask:
                logger.warning(
                    "Queue item %s: printer %d accepted project_file (subtask_id "
                    "advanced) but never transitioned to an active state within "
                    "%.0fs — printer wedged post-acceptance; reverted to 'pending' "
                    "for retry (#1678)",
                    queue_item_id,
                    printer_id,
                    total_timeout,
                )
            else:
                logger.warning(
                    "Queue item %s: printer %d did not respond to print command within "
                    "%.0fs (state still %s, subtask_id still %s) — reverted to 'pending' "
                    "for retry (#967)",
                    queue_item_id,
                    printer_id,
                    timeout,
                    pre_state,
                    pre_subtask_id,
                )

        # Phase B was entered iff subtask_id advanced, which means the
        # project_file landed on the printer. A forced reconnect at this point
        # would interrupt the printer's parse and trigger 0500_4003 (#1150) —
        # skip the recovery entirely.
        if landed_on_subtask:
            return

        # Phase A timeout path: if the printer's gcode_file changed since
        # pre-dispatch, the project_file command landed and the printer is
        # parsing — a forced reconnect mid-parse triggers 0500_4003 (#1150).
        # If gcode_file is unchanged, the publish was silently swallowed
        # (#887/#936) and force_reconnect recovery is what we want.
        client = printer_manager.get_client(printer_id)
        current_gcode_file = getattr(last_status, "gcode_file", None) if last_status else None
        publish_landed = current_gcode_file is not None and current_gcode_file != pre_gcode_file
        if publish_landed:
            logger.warning(
                "Queue item %s: gcode_file changed to %r (was %r) — printer "
                "received the command and is parsing slowly. Skipping forced "
                "MQTT reconnect to avoid 0500_4003 mid-parse (#1150).",
                queue_item_id,
                current_gcode_file,
                pre_gcode_file,
            )
        elif client and hasattr(client, "force_reconnect_stale_session"):
            client.force_reconnect_stale_session(
                f"queue print command unacknowledged after {timeout:.0f}s "
                f"(state still {pre_state}, gcode_file {current_gcode_file!r})"
            )


# Global scheduler instance
scheduler = PrintScheduler()
