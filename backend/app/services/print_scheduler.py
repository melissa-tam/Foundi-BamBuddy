"""Print scheduler service - processes the print queue."""

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
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
from backend.app.services.bambu_ftp import (
    cache_3mf_download,
    delete_file_async,
    get_ftp_retry_settings,
    upload_file_async,
    with_ftp_retry,
)
from backend.app.services.farm_staging import build_staged_reason, maybe_release_periodic
from backend.app.services.filament_deficit import compute_deficit_for_queue_item
from backend.app.services.notification_service import notification_service
from backend.app.services.printer_manager import (
    printer_manager,
    supports_drying,
    supports_drying_while_printing,
)
from backend.app.services.smart_plug_manager import smart_plug_manager
from backend.app.services.spool_selection import (
    DEFAULT_MIN_START_SPOOL_G,
    DEFAULT_SELECTION_POLICY,
    SELECTION_POLICIES,
    MatchOutcome,
    SlotInventory,
    build_slot_inventory,
    colors_are_similar,
    effective_policy,
    match_filaments_to_slots,
    normalize_color_for_compare,
)
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

# USB pre-flight settle window. The H2 fleet reports USB presence (state.sdcard)
# ONLY inside a full status report, which we must explicitly request
# (request_status_update → MQTT pushall) — so after asking we wait briefly for
# the fresh report to land before reading the flag. 2.5 s comfortably covers the
# observed pushall round-trip on H2S/H2C without materially delaying dispatch.
_USB_PREFLIGHT_WAIT_S = 2.5

# Filament-type equivalence + canonicalisation is shared with the farm capability
# gate — single source of truth in ``utils.filament_types`` (imported above as
# ``_canonical_filament_type`` to preserve the existing call sites).


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
        self._check_interval = 30  # seconds
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
        """Main loop - check queue every interval."""
        self._running = True
        logger.info("Print scheduler started")

        while self._running:
            try:
                await self.check_queue()
            except Exception as e:
                logger.error("Scheduler error: %s", e)

            await asyncio.sleep(self._check_interval)

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

            # Power-stagger budget (#Phase4): how many more prints may BEGIN
            # heating this tick without exceeding stagger_group_size starts inside
            # the current stagger_interval_minutes window. Derived by query from
            # started_at so it's restart-safe. Decremented on each real start
            # below; when it hits 0 the remaining eligible items simply wait for a
            # later tick (logged at debug).
            stagger_remaining = await self._stagger_budget(db)

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

                    # Compute AMS mapping if not already set
                    if not item.ams_mapping:
                        outcome = await self._compute_ams_mapping_for_printer(db, item.printer_id, item)
                        if outcome.start_blocked_slots:
                            # The only matching spool(s) sit below the minimum-start
                            # floor — hold the job with a distinct reason (they stay
                            # loaded as firmware backup donors). Do NOT persist a
                            # mapping. Notify once per transition, mirroring the
                            # filament-deficit path.
                            # Already low-spool staged? The durable FLAG is the
                            # transition signal (token-independent now the reason
                            # is a rich string) — dedup the once-per-transition
                            # notification off it.
                            was_blocked = bool(item.filament_short)
                            printer = await self._get_printer(db, item.printer_id)
                            stage_reason = build_staged_reason(printer.name if printer else "", start_min=True)
                            await self._stage_filament_short(db, item, unpin=False, reason=stage_reason)
                            logger.info(
                                "Queue item %s: start spool below minimum on printer %s (slots %s) — staged",
                                item.id,
                                item.printer_id,
                                outcome.start_blocked_slots,
                            )
                            if not was_blocked:
                                job_name = await self._get_job_name(db, item)
                                try:
                                    await notification_service.on_queue_job_waiting(
                                        job_name=job_name,
                                        target_model=(printer.model if printer else "") or "",
                                        waiting_reason=stage_reason,
                                        db=db,
                                    )
                                except Exception as e:
                                    logger.debug("start-min notification failed for item %s: %s", item.id, e)
                            continue
                        if outcome.mapping:
                            item.ams_mapping = json.dumps(outcome.mapping)
                            logger.info(
                                f"Queue item {item.id}: Computed AMS mapping for printer {item.printer_id}: {outcome.mapping}"
                            )
                            await db.commit()

                    # Filament-deficit pre-dispatch check (#1496). If the
                    # assigned spool can't satisfy any required slot grams,
                    # promote the item to manual_start so the user must
                    # acknowledge via the ▶ button (which re-checks live).
                    if await self._block_on_filament_deficit(db, item):
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
                    # Capture the assignment BEFORE dispatch: _start_print releases a
                    # model-targeted unit's scheduler-made pin (printer_id→None) when it
                    # holds at the USB/capability gate, so item.printer_id may be None on
                    # return. For a genuinely user-pinned unit (no target_model) this
                    # local equals item.printer_id throughout, so behaviour is unchanged.
                    dispatch_printer_id = item.printer_id
                    # Start the print
                    await self._start_print(db, item)
                    busy_printers.add(dispatch_printer_id)
                    if item.status == "printing":
                        stagger_remaining -= 1
                        # A legacy-pinned model unit whose printer recovered
                        # dispatches through THIS path — end its hold-unpinned
                        # notification-suppression window too.
                        self._hold_unpinned_items.discard(item.id)

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
                    assigned_mapping: str | None = None
                    last_waiting_reason: str | None = None
                    candidates_deficit_blocked = 0
                    candidates_start_blocked = 0
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

                        # Compute this candidate's AMS mapping WITHOUT persisting it —
                        # a losing candidate must leave no trace on the item.
                        if item.ams_mapping:
                            candidate_mapping = item.ams_mapping
                            candidate_start_blocked: list[int] = []
                        else:
                            outcome = await self._compute_ams_mapping_for_printer(db, candidate_id, item)
                            candidate_mapping = json.dumps(outcome.mapping) if outcome.mapping else None
                            candidate_start_blocked = outcome.start_blocked_slots

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

                        # Start-spool floor: this candidate's only matching spool(s)
                        # are below the minimum-start weight. Skip it like a deficit
                        # (it can still finish other prints / serve as a backup donor).
                        if candidate_start_blocked and not item.skip_filament_check:
                            deficit_blocked.add(candidate_id)
                            blocked_candidate_ids.append(candidate_id)
                            candidates_start_blocked += 1
                            logger.info(
                                "Queue item %s: candidate printer %s start spool below minimum (slots %s) — trying next",
                                item.id,
                                candidate_id,
                                candidate_start_blocked,
                            )
                            continue

                        assigned_printer_id = candidate_id
                        assigned_mapping = candidate_mapping
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

                        # Assign printer + persist its mapping. Clear a stale
                        # assignment-time waiting reason, but PRESERVE a live
                        # "no_usb_drive" hold: _start_print owns that token and
                        # self-clears it on a successful dispatch (past the capability
                        # gate below). When a model unit keeps landing on USB-less
                        # printers, preserving it here keeps the USB hold's
                        # once-per-transition waiting notification deduped across ticks —
                        # this optimistic clear would otherwise make every tick look like
                        # a fresh transition and re-notify.
                        item.printer_id = assigned_printer_id
                        if assigned_mapping and not item.ams_mapping:
                            item.ams_mapping = assigned_mapping
                            logger.info(
                                "Queue item %s: Computed AMS mapping for printer %s: %s",
                                item.id,
                                assigned_printer_id,
                                assigned_mapping,
                            )
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

                        await self._start_print(db, item)
                        busy_printers.add(assigned_printer_id)
                        if item.status == "printing":
                            stagger_remaining -= 1
                            # Real dispatch ends the suppression window — a later
                            # hold on a NEW assignment is a new transition.
                            self._hold_unpinned_items.discard(item.id)

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

                    elif candidates_deficit_blocked > 0 or candidates_start_blocked > 0:
                        # Every candidate that could have run was blocked on filament →
                        # stage the item UNPINNED so a later tick re-runs the full
                        # candidate search once any printer's spool is topped up. One
                        # notification per (batch, model) group per tick — the incident
                        # sent one per unit (10 for a 10-plate run). D9: NAME the short
                        # machines in the persisted reason (the model log named nothing
                        # persistent) so the queue banner tells the operator which
                        # printer to top up. Purely start-floor blocks read "below
                        # minimum"; a mix stays generic ("needs more filament").
                        start_min_only = candidates_deficit_blocked == 0 and candidates_start_blocked > 0
                        blocked_names = await self._resolve_printer_names(db, blocked_candidate_ids)
                        who = ", ".join(blocked_names) if blocked_names else f"{item.target_model} printers"
                        stage_reason = build_staged_reason(who, start_min=start_min_only)
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
                                job_name = await self._get_job_name(db, item)
                                await notification_service.on_queue_job_waiting(
                                    job_name=job_name,
                                    target_model=item.target_model,
                                    waiting_reason=last_waiting_reason,
                                    db=db,
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
        """Compute the AMS mapping + start-block outcome for a printer.

        Called when a queue item has no ams_mapping set — either for model-based
        items after printer assignment, or printer-specific items (e.g. from VP).
        Applies the configured spool-selection policy (``spool_selection_policy``)
        and the minimum-start-weight floor (``min_start_spool_g``) via the
        ``spool_selection`` module.

        Args:
            db: Database session
            printer_id: The assigned printer ID
            item: The queue item (contains archive_id or library_file_id)

        Returns:
            A ``MatchOutcome`` whose ``mapping`` is the AMS mapping array (or None
            when no mapping is needed/possible) and whose ``start_blocked_slots``
            names any slot held back purely by the minimum-start floor.
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
                            db, printer_id, force_overrides, status, eff_policy, min_start_g
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

        # Build loaded filaments from printer status
        loaded_filaments = self._build_loaded_filaments(status)
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

        return match_filaments_to_slots(
            filament_reqs,
            loaded_filaments,
            policy=eff_policy,
            inv=inv,
            backup_on=status.ams_filament_backup,
            min_start_g=min_start_g,
        )

    async def _build_override_direct_mapping(
        self,
        db: AsyncSession,
        printer_id: int,
        force_overrides: list[dict],
        status,
        policy: str,
        min_start_g: int,
    ) -> MatchOutcome:
        """Build an AMS mapping directly from force-color overrides without a 3MF.

        Used when ``_get_filament_requirements`` returns nothing (e.g. the 3MF's
        slice_info is missing or unreadable) but ``force_color_match`` overrides
        are present. Each override's ``slot_id``, ``type``, and ``color`` are
        treated as the filament requirement for that slot and matched against the
        current AMS state of the printer, threading the same policy / floor as the
        normal path.

        Returns a ``MatchOutcome`` (mapping None when the AMS has no filaments).
        """
        loaded = self._build_loaded_filaments(status)
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

        Args:
            status: PrinterState from printer_manager

        Returns:
            List of loaded filament dicts with type, color, ams_id, tray_id, global_tray_id
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
                if tray_type:
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
                            "type": tray_type,
                            "color": color,
                            "tray_info_idx": tray_info_idx,
                            "ams_id": ams_id,
                            "tray_id": tray_id,
                            "is_ht": is_ht,
                            "is_external": False,
                            "global_tray_id": global_tray_id,
                            "extruder_id": ams_extruder_map.get(str(ams_id)),
                            "remain": tray.get("remain", -1),
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

    async def _build_inventory_remain_overrides(
        self, db: AsyncSession, printer_id: int, loaded: list[dict]
    ) -> dict[int, float]:
        """Thin delegate: ``{global_tray_id: remaining_grams}`` for inventory-bound
        AMS slots, sourced from ``spool_selection.build_slot_inventory`` (single
        source with the dispatcher). Kept for external callers that only need the
        remaining-grams map; slots without a known remaining are omitted.
        """
        inv = await build_slot_inventory(db, printer_id, loaded)
        return {gtid: si.remaining_g for gtid, si in inv.items() if si.remaining_g is not None}

    def _match_filaments_to_slots(self, required: list[dict], loaded: list[dict]) -> list[int] | None:
        """Thin delegate to ``spool_selection.match_filaments_to_slots`` for the
        default (slot-order, no floor) case — bucket precedence unchanged. Policy
        selection and the minimum-start floor live in
        ``_compute_ams_mapping_for_printer``; this preserves the simple
        two-argument entry point external callers use. Returns the mapping array.
        """
        return match_filaments_to_slots(
            required, loaded, policy="slot_order", inv={}, backup_on=True, min_start_g=0
        ).mapping

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

    async def _stagger_budget(self, db: AsyncSession) -> int:
        """How many more prints may START this stagger window (power management).

        Consumes the persisted ``stagger_group_size`` (max printers allowed to
        begin heating within one window) and ``stagger_interval_minutes`` (window
        length). Recent starts are derived BY QUERY from ``print_queue.started_at``
        so a backend restart can't unleash a thundering herd — the sliding window
        is reconstructed from durable state, not in-memory counters. A large
        ``stagger_group_size`` effectively disables staggering (the window budget
        is never reached). Returns the remaining budget (>= 0) for this tick.
        """
        group_size = await self._get_int_setting(db, "stagger_group_size", default=2)
        interval_minutes = await self._get_int_setting(db, "stagger_interval_minutes", default=3)
        if group_size <= 0 or interval_minutes <= 0:
            # Defensive: schema clamps these to >=1, but a hand-edited row could
            # slip through — treat non-positive config as "staggering off".
            return group_size if group_size > 0 else 1_000_000
        window_start = datetime.now(timezone.utc) - timedelta(minutes=interval_minutes)
        recent_starts = await db.scalar(
            select(func.count(PrintQueueItem.id)).where(PrintQueueItem.started_at >= window_start)
        )
        return max(0, group_size - int(recent_starts or 0))

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

    async def _stage_filament_short(
        self, db: AsyncSession, item: PrintQueueItem, *, unpin: bool, reason: str = "filament_short"
    ) -> None:
        """Mark a queue item low-spool staged (#1496 / #Phase4).

        ``unpin=False`` keeps the item on its assigned printer (pinned path).
        ``unpin=True`` clears the pin and its stale mapping — used by the
        model-based path when EVERY eligible printer is short, so ``farm_staging``
        releases it and the next tick re-runs the full candidate search across the
        fleet. ``reason`` is the persisted ``waiting_reason`` — callers build a
        human-readable :func:`farm_staging.build_staged_reason` string that NAMES
        the blocked machine(s) (D9); the ``"filament_short"`` default is a legacy
        fallback for a caller that passes none.
        """
        item.filament_short = True
        item.manual_start = True
        item.waiting_reason = reason
        if unpin:
            item.printer_id = None
            item.ams_mapping = None
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
        """
        await self._stage_filament_short(db, item, unpin=True, reason=reason)
        logger.info(
            "Queue item %s: every eligible %s printer blocked (%s) — staged UNPINNED",
            item.id,
            item.target_model,
            reason,
        )
        group_key = (item.batch_id, item.target_model)
        if group_key in notified_groups:
            return
        notified_groups.add(group_key)
        job_name = await self._get_job_name(db, item)
        try:
            await notification_service.on_queue_job_waiting(
                job_name=job_name,
                target_model=item.target_model or "",
                waiting_reason=reason,
                db=db,
            )
        except Exception as e:
            logger.debug("filament_short notification failed for item %s: %s", item.id, e)

    async def _block_on_filament_deficit(
        self,
        db: AsyncSession,
        item: PrintQueueItem,
    ) -> bool:
        """Promote the pinned item to manual_start when the assigned spool is short (#1496).

        Returns True when this dispatch attempt was blocked, False when the
        item is clear to start. A previously-flagged item whose spool has
        since been swapped to one with enough material clears the flag here
        so the next scheduler tick dispatches it. (The model-based path checks
        candidates inline via ``_compute_deficit_safe`` and does not call this.)
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

        deficit = await self._compute_deficit_safe(db, item)

        if deficit:
            printer = await self._get_printer(db, item.printer_id) if item.printer_id else None
            stage_reason = build_staged_reason(printer.name if printer else "", start_min=False)
            await self._stage_filament_short(db, item, unpin=False, reason=stage_reason)
            job_name = await self._get_job_name(db, item)
            logger.info(
                "Queue item %s blocked on filament deficit (%d slot(s)) — promoted to manual_start",
                item.id,
                len(deficit),
            )
            try:
                await notification_service.on_queue_job_waiting(
                    job_name=job_name,
                    target_model=(printer.model if printer else "") or "",
                    waiting_reason=stage_reason,
                    db=db,
                )
            except Exception as e:
                logger.debug("filament_short notification failed for item %s: %s", item.id, e)
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
        await db.commit()
        try:
            from backend.app.services.farm_policy import on_terminal

            await on_terminal(db, item.printer_id, item.id, "failed")
        except Exception as farm_err:  # noqa: BLE001 — policy must never break dispatch
            logger.warning("Queue item %s: farm policy hook (dispatch failure) failed: %s", item.id, farm_err)

    async def _start_print(self, db: AsyncSession, item: PrintQueueItem):
        """Upload file and start print for a queue item.

        Supports two sources:
        - archive_id: Print from an existing archive
        - library_file_id: Print from a library file (file manager)
        """
        logger.info("Starting queue item %s", item.id)

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
        # until dispatch fails. Ask for a fresh full report, wait briefly for it
        # to land, then read the live flag. Fail-OPEN: ONLY an explicit False
        # (drive confirmed absent) holds dispatch; None/missing (never reported /
        # stale) proceeds, mirroring the UI chip's fail-safe. This is a WAIT, not
        # a failure — the item stays pending, no manual_start, no retry burn; the
        # next tick requests another fresh report and self-clears it when the
        # drive returns (via the capability gate's existing waiting_reason reset
        # below, since this block sits BEFORE it on the dispatch path).
        printer_manager.request_status_update(item.printer_id)
        await asyncio.sleep(_USB_PREFLIGHT_WAIT_S)
        usb_status = printer_manager.get_status(item.printer_id)
        if usb_status is not None and getattr(usb_status, "sdcard", None) is False:
            # Dedupe like the low-spool waiting notification: only fire on the
            # transition INTO the hold (waiting_reason wasn't already
            # no_usb_drive), so a stick left out across many ticks notifies once.
            # We can't reuse the low-spool path's manual_start-based once-guard
            # because this must stay a self-clearing pending wait.
            already_waiting = item.waiting_reason == "no_usb_drive"
            held_printer_id = item.printer_id
            # Release a model-targeted unit's scheduler-made assignment before
            # holding. target_model set ⇒ the pin was made THIS tick by the
            # model-based path, never a user choice — so a sick-but-idle printer
            # (here: no USB stick) must not become the unit's permanent home.
            # Leaving the pin turns the unit into a "specific printer" item the
            # model path never rebalances, funnelling the whole run onto one broken
            # printer, one unit per tick, until the pool drains. Clear ams_mapping
            # too: it was computed for THIS printer's AMS slot layout and the model
            # path recomputes it per candidate. Always commit the un-pin (even when
            # already waiting) so a unit held tick N and re-held tick N+1 still ends
            # unpinned. A user-pinned unit (no target_model) keeps its printer and,
            # exactly as before, commits only on the transition into the hold.
            if item.target_model:
                item.printer_id = None
                item.ams_mapping = None
                item.waiting_reason = "no_usb_drive"
                # Once-guard for the model path's per-assignment notification —
                # re-selection of the same sick printer every tick must not
                # re-notify "assigned" (see _hold_unpinned_items in __init__).
                self._hold_unpinned_items.add(item.id)
                await db.commit()
            elif not already_waiting:
                item.waiting_reason = "no_usb_drive"
                await db.commit()
            logger.info(
                "Queue item %s: USB pre-flight held dispatch — no USB drive in printer %s",
                item.id,
                held_printer_id,
            )
            if not already_waiting:
                job_name = await self._get_job_name(db, item)
                try:
                    await notification_service.on_queue_job_waiting(
                        job_name=job_name,
                        target_model=(printer.model if printer else "") or "",
                        waiting_reason="no_usb_drive",
                        db=db,
                    )
                except Exception as e:
                    logger.debug("no_usb_drive notification failed for item %s: %s", item.id, e)
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
            # the mismatched printer. Release the scheduler-made pin + its
            # per-printer AMS mapping so the model path re-evaluates the fleet next
            # tick; always commit the un-pin so a re-held unit still ends unpinned. A
            # user-pinned unit (no target_model) holds in place, committing only on a
            # reason change, exactly as before.
            if item.target_model:
                item.printer_id = None
                item.ams_mapping = None
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
        if item.waiting_reason:
            # Cleared to dispatch: drop any stale capability waiting reason.
            item.waiting_reason = None

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
                    if item.cleanup_library_after_dispatch and not library_file.is_external:
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
                    )
        except Exception as e:
            uploaded = False
            logger.error("Queue item %s: FTP error: %s (type: %s)", item.id, e, type(e).__name__)

        # Clean up injected temp file after upload attempt
        if injected_path and injected_path.exists():
            injected_path.unlink(missing_ok=True)

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

        # Parse AMS mapping if stored
        ams_mapping = None
        if item.ams_mapping:
            try:
                ams_mapping = json.loads(item.ams_mapping)
            except json.JSONDecodeError:
                logger.warning("Queue item %s: Invalid AMS mapping JSON, ignoring", item.id)

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
        item.status = "printing"
        item.started_at = datetime.now(timezone.utc)
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

            # Correlation (Phase 1, P1-A): stamp the subtask_id minted for THIS
            # dispatch so a terminal MQTT status can be bound back to this exact
            # queue item (not a printer_id-only lookup). start_print set it on the
            # client synchronously above; commit it with the already-'printing' row.
            item.dispatch_subtask_id = getattr(
                printer_manager.get_client(item.printer_id), "last_dispatch_subtask_id", None
            )
            await db.commit()

            # Register the local 3MF in the cover-cache so /cover skips FTP
            # (#1166 follow-up). file_path was resolved earlier from either the
            # archive or the library file row.
            if file_path is not None:
                cache_3mf_download(item.printer_id, remote_filename, file_path)

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
                    ),
                    name=f"watchdog-print-start-{item.id}",
                )

            # Get estimated time for notification
            estimated_time = None
            if archive and archive.print_time_seconds:
                estimated_time = archive.print_time_seconds
            elif library_file and library_file.print_time_seconds:
                estimated_time = library_file.print_time_seconds

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
